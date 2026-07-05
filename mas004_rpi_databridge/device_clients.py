from __future__ import annotations

import contextlib
import contextvars
import queue
import os
import re
import socket
import threading
import time
from dataclasses import dataclass
from typing import Iterable, Optional

from ping3 import ping

from mas004_rpi_databridge.device_protocols import (
    ZBC_START,
    ZBC_FLAG_ACK,
    ZBC_FLAG_NAK,
    build_ultimate_command,
    build_zbc_ack,
    build_zbc_message,
    build_zbc_packet,
    parse_ultimate_result,
    parse_zbc_message,
    parse_zbc_packet,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows/local development fallback.
    fcntl = None


POSITIONAL_MOTOR_IDS = frozenset({1, 2, 4, 5, 6, 7, 8, 9})
MOTOR_RUNTIME_WRITABLE_CONFIG_KEYS = frozenset(
    {
        "speed_mm_s",
        "current_pct",
        "run_current_pct",
        "drive_current_pct",
        "base_current_pct",
        "hold_current_pct",
        "stop_current_pct",
        "accel_mm_s2",
        "decel_mm_s2",
    }
)
MOTOR_SETUP_ONLY_OPS = frozenset(
    {
        "SAVE",
        "ZERO",
        "SET_MIN",
        "SET_MAX",
        "SETUP_WRITE_ARM",
        "MACHINE_SETUP_WRITE_ARM",
        "WRITE_NV",
        "CONFIGURE",
    }
)
MOTOR_SETUP_ONLY_ASSIGNMENTS = frozenset(
    {
        "SET_POSITION_MM",
        "CAPTURE_POSITION_MM",
        "SET_DRIVE_RESOLUTION_PR",
    }
)
_MOTOR_SETUP_WRITE_ALLOWED = contextvars.ContextVar("motor_setup_write_allowed", default=False)
_MOTOR_SETUP_WRITE_REASON = contextvars.ContextVar("motor_setup_write_reason", default="")
_MOTOR_COMMAND_RE = re.compile(r"^\s*MOTOR\s+(\d+)\s+(.+?)\s*$", re.IGNORECASE)


@contextlib.contextmanager
def motor_setup_write_context(reason: str = "machine_setup_motors"):
    token_allowed = _MOTOR_SETUP_WRITE_ALLOWED.set(True)
    token_reason = _MOTOR_SETUP_WRITE_REASON.set(str(reason or "machine_setup_motors"))
    try:
        yield
    finally:
        _MOTOR_SETUP_WRITE_REASON.reset(token_reason)
        _MOTOR_SETUP_WRITE_ALLOWED.reset(token_allowed)


def _motor_setup_write_context_active() -> bool:
    return bool(_MOTOR_SETUP_WRITE_ALLOWED.get(False))


def _motor_setup_write_block_reason(line: str) -> str:
    text = str(line or "").strip()
    match = _MOTOR_COMMAND_RE.match(text)
    if not match:
        return ""
    motor_id = int(match.group(1))
    if motor_id not in POSITIONAL_MOTOR_IDS:
        return ""

    rest = match.group(2).strip()
    rest_upper = rest.upper()
    op = rest_upper.split(None, 1)[0].split("=", 1)[0]
    if op in MOTOR_SETUP_ONLY_OPS:
        return op
    if op in MOTOR_SETUP_ONLY_ASSIGNMENTS:
        return op
    if rest_upper.startswith("SET "):
        protected_keys: list[str] = []
        for token in rest[4:].replace(",", " ").replace(";", " ").split():
            key = token.split("=", 1)[0].strip().lower()
            if key and key not in MOTOR_RUNTIME_WRITABLE_CONFIG_KEYS:
                protected_keys.append(key)
        if protected_keys:
            return "SET " + ",".join(sorted(set(protected_keys)))
    return ""


class _EndpointState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.fail_count = 0
        self.next_allowed_at = 0.0
        self.sock: Optional[socket.socket] = None
        self.broker: Optional["_EspCommandBroker"] = None
        self.exchange_count = 0
        self.connect_count = 0
        self.reconnect_count = 0
        self.last_ok_at = 0.0
        self.last_error = ""
        self.priority_until_at = 0.0

    def close_socket(self, *, count_reconnect: bool = True) -> None:
        sock = self.sock
        self.sock = None
        if sock is not None:
            if count_reconnect:
                self.reconnect_count += 1
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass


_ESP_ENDPOINTS_GUARD = threading.Lock()
_ESP_ENDPOINTS: dict[tuple[str, int], _EndpointState] = {}
_ESP_COMMAND_IDLE_REUSE_S = 0.75
_ESP_COMMAND_MAX_ATTEMPTS = 5
_ESP_COMMAND_CLOSE_AFTER_RESPONSE = True
_ESP_COMMAND_MIN_SPACING_S = 0.08
_ESP_COMMAND_BROKER_ENABLED = True
_ESP_COMMAND_BROKER_QUEUE_MAX = 256
_ESP_COMMAND_BROKER_KEEPALIVE_S = 2.0
_ESP_COMMAND_BROKER_MODE_COMMAND = "TCP BROKER=1"
_ESP_COMMAND_BROKER_SETUP_TIMEOUT_S = 1.0
_ULTIMATE_ENDPOINTS_GUARD = threading.Lock()
_ULTIMATE_ENDPOINTS: dict[tuple[str, int], _EndpointState] = {}
_ZBC_ENDPOINTS_GUARD = threading.Lock()
_ZBC_ENDPOINTS: dict[tuple[str, int], _EndpointState] = {}


def _endpoint_state(
    pool: dict[tuple[str, int], _EndpointState],
    guard: threading.Lock,
    host: str,
    port: int,
) -> _EndpointState:
    key = ((host or "").strip(), int(port or 0))
    with guard:
        state = pool.get(key)
        if state is None:
            state = _EndpointState()
            pool[key] = state
        return state


def _esp_endpoint_state(host: str, port: int) -> _EndpointState:
    return _endpoint_state(_ESP_ENDPOINTS, _ESP_ENDPOINTS_GUARD, host, port)


def _esp_lock_path(host: str, port: int) -> str:
    safe_host = re.sub(r"[^A-Za-z0-9_.-]+", "_", (host or "esp").strip()) or "esp"
    base_dir = "/run/lock" if os.path.isdir("/run/lock") else "/tmp"
    return os.path.join(base_dir, f"mas004-esp-plc-{safe_host}-{int(port or 0)}.lock")


class _EspInterprocessLockHandle:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.fd: int | None = None

    def acquire(self, timeout_s: float) -> None:
        if fcntl is None:
            return
        if self.fd is not None:
            return
        path = _esp_lock_path(self.host, self.port)
        deadline = time.monotonic() + max(0.2, float(timeout_s or 0.2))
        try:
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o666)
            try:
                os.fchmod(fd, 0o666)
            except Exception:
                pass
        except PermissionError:
            fd = os.open(path, os.O_RDONLY)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.fd = fd
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    try:
                        os.close(fd)
                    except Exception:
                        pass
                    raise TimeoutError(f"ESP command broker interprocess busy >{timeout_s:.1f}s")
                time.sleep(0.05)

    def release(self) -> None:
        fd = self.fd
        self.fd = None
        if fd is None:
            return
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except Exception:
                pass
        try:
            os.close(fd)
        except Exception:
            pass


@contextlib.contextmanager
def _esp_interprocess_lock(host: str, port: int, timeout_s: float):
    if fcntl is None:
        yield
        return

    path = _esp_lock_path(host, port)
    deadline = time.monotonic() + max(0.2, float(timeout_s or 0.2))
    fd: int | None = None
    try:
        try:
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o666)
            try:
                os.fchmod(fd, 0o666)
            except Exception:
                pass
        except PermissionError:
            fd = os.open(path, os.O_RDONLY)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"ESP command channel interprocess busy >{timeout_s:.1f}s")
                time.sleep(0.05)
        yield
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                os.close(fd)
            except Exception:
                pass


class _EspBrokerRequest:
    def __init__(
        self,
        *,
        line: str,
        read_timeout_s: float,
        read_limit: int,
        deadline: float,
        priority: int,
    ) -> None:
        self.line = str(line or "").strip()
        self.read_timeout_s = float(read_timeout_s or 1.0)
        self.read_limit = int(read_limit or 8192)
        self.deadline = float(deadline)
        self.priority = int(priority)
        self.event = threading.Event()
        self.response: str | None = None
        self.exception: Exception | None = None
        self.created_at = time.monotonic()

    def complete(self, response: str) -> None:
        self.response = response
        self.event.set()

    def fail(self, exc: Exception) -> None:
        self.exception = exc
        self.event.set()


def _esp_command_priority(line: str, explicit_priority: bool) -> int:
    text = str(line or "").strip().upper()
    if explicit_priority:
        return 0
    if not text:
        return 90
    if any(token in text for token in (" STOP", " CANCEL", "RESET", "MOVE_VEL_MM_S=0")):
        return 0
    if text.startswith("PROCESS PRODUCTION") or text.startswith("PROCESS WICKLER") or text.startswith("PROCESS INDEXED"):
        return 10
    if text.startswith("PROCESS SETUP_MEASURE") or text.startswith("MOTOR 3 "):
        return 15
    if text.startswith("SYNC "):
        return 30
    if text in {"PING", "STATUS?", "INFO", "IP?"}:
        return 70
    if text.endswith("LIST?") or text.endswith("SNAPSHOT?") or text.endswith("VISUALIZATION?") or text.endswith("LOG?"):
        return 80
    return 50


class _EspCommandBroker:
    def __init__(self, host: str, port: int, timeout_s: float, state: _EndpointState) -> None:
        self.host = (host or "").strip()
        self.port = int(port or 0)
        self.timeout_s = float(timeout_s or 1.0)
        self.state = state
        self._queue: "queue.PriorityQueue[tuple[int, int, _EspBrokerRequest]]" = queue.PriorityQueue(
            maxsize=_ESP_COMMAND_BROKER_QUEUE_MAX
        )
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._file_lock = _EspInterprocessLockHandle(self.host, self.port)
        self._broker_supported: bool | None = None
        self._broker_confirmed_at = 0.0
        self._last_keepalive_at = 0.0
        self._active_line = ""
        self._active_req: _EspBrokerRequest | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run,
            name=f"esp-command-broker-{self.host}:{self.port}",
            daemon=True,
        )
        self._thread.start()

    def submit(
        self,
        line: str,
        *,
        read_timeout_s: float,
        read_limit: int,
        priority: int,
        wait_timeout_s: float,
    ) -> str:
        self.start()
        deadline = time.monotonic() + max(0.5, float(wait_timeout_s or 0.5))
        req = _EspBrokerRequest(
            line=line,
            read_timeout_s=read_timeout_s,
            read_limit=read_limit,
            deadline=deadline,
            priority=priority,
        )
        with self._seq_lock:
            self._seq += 1
            seq = self._seq
        try:
            self._queue.put((int(priority), seq, req), timeout=min(0.5, max(0.05, deadline - time.monotonic())))
        except queue.Full as exc:
            raise TimeoutError("ESP command broker queue full") from exc
        remaining = max(0.05, deadline - time.monotonic())
        if not req.event.wait(timeout=remaining):
            req.fail(TimeoutError("ESP command broker request timed out"))
            if self._active_req is req:
                self.close_socket()
            raise TimeoutError("ESP command broker request timed out")
        if req.exception is not None:
            raise req.exception
        return str(req.response or "")

    def close_socket(self) -> None:
        with self.state.lock:
            self.state.close_socket()
            self._active_line = ""
            self._file_lock.release()

    def diagnostics(self) -> dict[str, object]:
        thread = self._thread
        with self.state.lock:
            return {
                "host": self.host,
                "port": self.port,
                "enabled": True,
                "thread_alive": bool(thread and thread.is_alive()),
                "connected": self.state.sock is not None,
                "broker_supported": self._broker_supported,
                "broker_confirmed_at": self._broker_confirmed_at,
                "queue_depth": self._queue.qsize(),
                "active_line": self._active_line,
                "connect_count": self.state.connect_count,
                "reconnect_count": self.state.reconnect_count,
                "exchange_count": self.state.exchange_count,
                "fail_count": self.state.fail_count,
                "last_ok_at": self.state.last_ok_at,
                "last_error": self.state.last_error,
                "priority_until_at": self.state.priority_until_at,
            }

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                _prio, _seq, req = self._queue.get(timeout=_ESP_COMMAND_BROKER_KEEPALIVE_S)
            except queue.Empty:
                self._keepalive()
                continue
            try:
                if req.event.is_set():
                    continue
                if time.monotonic() >= req.deadline:
                    req.fail(TimeoutError("ESP command broker request expired before send"))
                    continue
                self._active_line = req.line
                self._active_req = req
                reply = self._execute_with_retries(req)
                if not req.event.is_set():
                    req.complete(reply)
            except Exception as exc:
                if not req.event.is_set():
                    req.fail(exc)
            finally:
                if self._active_req is req:
                    self._active_req = None
                self._active_line = ""
                self._queue.task_done()

    def _execute_with_retries(self, req: _EspBrokerRequest) -> str:
        last_error: Exception | None = None
        for attempt in range(_ESP_COMMAND_MAX_ATTEMPTS):
            if time.monotonic() >= req.deadline:
                break
            try:
                reply = self._exchange_once(req.line, req.read_timeout_s, req.read_limit, req.deadline)
                if reply.strip().upper() == "NAK_BUSY" and attempt < (_ESP_COMMAND_MAX_ATTEMPTS - 1):
                    raise RuntimeError("ESP endpoint busy")
                return reply
            except Exception as exc:
                last_error = exc
                with self.state.lock:
                    self.state.last_error = repr(exc)
                    self.state.fail_count += 1
                    self.state.next_allowed_at = time.monotonic() + min(
                        0.8,
                        0.15 * (2 ** min(self.state.fail_count, 3)),
                    )
                self.close_socket()
                time.sleep(min(0.15, max(0.0, req.deadline - time.monotonic())))
        if last_error is not None:
            raise last_error
        raise TimeoutError("ESP endpoint command deadline exceeded")

    def _ensure_socket(self, deadline: float) -> socket.socket:
        with self.state.lock:
            if self.state.sock is not None:
                return self.state.sock
        lock_timeout_s = max(0.2, min(5.0, deadline - time.monotonic()))
        self._file_lock.acquire(lock_timeout_s)
        try:
            created_socket = False
            with self.state.lock:
                if self.state.sock is None:
                    sock = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
                    _tune_command_socket(sock)
                    self.state.sock = sock
                    self.state.connect_count += 1
                    created_socket = True
                sock = self.state.sock
            self._enable_broker_mode_if_needed(sock, deadline, force=created_socket)
            with self.state.lock:
                sock = self.state.sock
                if sock is None:
                    raise RuntimeError("ESP broker mode unsupported")
                return sock
        except Exception:
            self.close_socket()
            raise

    def _enable_broker_mode_if_needed(self, sock: socket.socket, deadline: float, *, force: bool = False) -> None:
        if self._broker_supported is False:
            return
        if self._broker_supported is True and not force:
            return
        timeout_s = max(0.2, min(_ESP_COMMAND_BROKER_SETUP_TIMEOUT_S, deadline - time.monotonic()))
        try:
            sock.settimeout(timeout_s)
            sock.sendall((_ESP_COMMAND_BROKER_MODE_COMMAND + "\n").encode("utf-8"))
            reply = _recv_line(sock, limit=512).strip()
        except Exception:
            if self._broker_supported is False:
                pass
            elif self._broker_supported is True:
                # Broker support is a firmware property; a reconnect timeout
                # must not permanently downgrade a previously confirmed ESP.
                self._broker_supported = True
            else:
                self._broker_supported = None
            raise
        upper = reply.upper()
        if upper.startswith("ACK_TCP_BROKER") or upper.startswith("ACK_TCP_PERSISTENT"):
            self._broker_supported = True
            self._broker_confirmed_at = time.monotonic()
            with self.state.lock:
                self.state.exchange_count += 1
                self.state.last_ok_at = time.monotonic()
                self.state.last_error = ""
            return
        self._broker_supported = False
        self.close_socket()

    def _exchange_once(self, line: str, read_timeout_s: float, read_limit: int, deadline: float) -> str:
        now = time.monotonic()
        with self.state.lock:
            next_allowed_at = self.state.next_allowed_at
            last_ok_at = self.state.last_ok_at
        if next_allowed_at > now:
            time.sleep(min(next_allowed_at - now, max(0.0, deadline - now)))
        quiet_for_s = time.monotonic() - last_ok_at if last_ok_at > 0.0 else 999.0
        if quiet_for_s < _ESP_COMMAND_MIN_SPACING_S:
            time.sleep(min(_ESP_COMMAND_MIN_SPACING_S - quiet_for_s, max(0.0, deadline - time.monotonic())))
        sock = self._ensure_socket(deadline)
        sock.settimeout(float(read_timeout_s or self.timeout_s))
        sock.sendall((str(line or "").strip() + "\n").encode("utf-8"))
        reply = _recv_line(sock, limit=read_limit).strip()
        if not reply:
            raise RuntimeError("ESP endpoint empty reply")
        with self.state.lock:
            self.state.fail_count = 0
            self.state.next_allowed_at = 0.0
            self.state.exchange_count += 1
            self.state.last_ok_at = time.monotonic()
            self.state.last_error = ""
        if self._broker_supported is not True and _ESP_COMMAND_CLOSE_AFTER_RESPONSE:
            self.close_socket()
        return reply

    def _keepalive(self) -> None:
        if self._broker_supported is not True:
            return
        with self.state.lock:
            connected = self.state.sock is not None
            last_ok_at = self.state.last_ok_at
        if not connected:
            return
        if (time.monotonic() - max(last_ok_at, self._last_keepalive_at)) < _ESP_COMMAND_BROKER_KEEPALIVE_S:
            return
        deadline = time.monotonic() + max(2.0, self.timeout_s + 1.0)
        req = _EspBrokerRequest(
            line="PING",
            read_timeout_s=max(1.0, self.timeout_s),
            read_limit=512,
            deadline=deadline,
            priority=90,
        )
        try:
            self._exchange_once(req.line, req.read_timeout_s, req.read_limit, req.deadline)
            self._last_keepalive_at = time.monotonic()
        except Exception as exc:
            with self.state.lock:
                self.state.last_error = repr(exc)
            self.close_socket()


def _esp_command_broker(host: str, port: int, timeout_s: float) -> _EspCommandBroker:
    state = _esp_endpoint_state(host, port)
    with state.lock:
        broker = state.broker
        if broker is None:
            broker = _EspCommandBroker(host, port, timeout_s, state)
            state.broker = broker
        else:
            broker.timeout_s = float(timeout_s or broker.timeout_s)
        broker.start()
        return broker


def start_esp_command_broker(host: str, port: int, timeout_s: float = 1.0) -> dict[str, object]:
    if not (host or "").strip() or int(port or 0) <= 0:
        return {"enabled": False, "error": "endpoint missing"}
    broker = _esp_command_broker(host, int(port), timeout_s)
    try:
        reply = broker.submit(
            "PING",
            read_timeout_s=max(0.5, float(timeout_s or 1.0)),
            read_limit=512,
            priority=_esp_command_priority("PING", False),
            wait_timeout_s=max(3.0, float(timeout_s or 1.0) + 2.0),
        )
        diag = broker.diagnostics()
        diag["warmup_reply"] = reply
        return diag
    except Exception as exc:
        diag = broker.diagnostics()
        diag["warmup_error"] = repr(exc)
        return diag


def _ultimate_endpoint_state(host: str, port: int) -> _EndpointState:
    return _endpoint_state(_ULTIMATE_ENDPOINTS, _ULTIMATE_ENDPOINTS_GUARD, host, port)


def _zbc_endpoint_state(host: str, port: int) -> _EndpointState:
    return _endpoint_state(_ZBC_ENDPOINTS, _ZBC_ENDPOINTS_GUARD, host, port)


@dataclass
class DeviceWatchdog:
    host: str
    timeout_s: float
    down_after: int = 3
    _fails: int = 0

    def check(self) -> bool:
        if not self.host:
            return True
        try:
            ok = ping(self.host, timeout=self.timeout_s, unit="s") is not None
        except Exception:
            ok = False
        self._fails = 0 if ok else (self._fails + 1)
        return self._fails < max(1, int(self.down_after))


class EspPlcClient:
    def __init__(self, host: str, port: int, timeout_s: float):
        self.host = (host or "").strip()
        self.port = int(port or 0)
        self.timeout_s = float(timeout_s or 1.0)

    def exchange_line(
        self,
        line: str,
        read_timeout_s: float | None = None,
        *,
        read_limit: int = 8192,
        priority: bool = False,
    ) -> str:
        if not self.host or self.port <= 0:
            raise RuntimeError("ESP endpoint missing")
        protected_reason = _motor_setup_write_block_reason(line)
        if protected_reason and not _motor_setup_write_context_active():
            raise RuntimeError(
                "ESP motor setup write blocked: "
                f"{protected_reason} fuer Positionsmotoren ID1/2/4-9 ist nur ueber "
                "/ui/machine-setup/motors erlaubt"
            )

        read_timeout_s = float(read_timeout_s or self.timeout_s)
        if _ESP_COMMAND_BROKER_ENABLED:
            state = _esp_endpoint_state(self.host, self.port)
            if priority:
                state.priority_until_at = max(state.priority_until_at, time.monotonic() + 2.0)
            else:
                quiet_until = state.priority_until_at
                if quiet_until > time.monotonic():
                    time.sleep(min(quiet_until - time.monotonic(), 0.5))
            command_window_s = max(
                3.0,
                min(self.timeout_s + (read_timeout_s * _ESP_COMMAND_MAX_ATTEMPTS) + 2.0, 18.0),
            )
            wait_timeout_s = max(command_window_s, min(self.timeout_s + read_timeout_s + 20.0, 60.0))
            broker = _esp_command_broker(self.host, self.port, self.timeout_s)
            reply = broker.submit(
                line,
                read_timeout_s=read_timeout_s,
                read_limit=read_limit,
                priority=_esp_command_priority(line, priority),
                wait_timeout_s=wait_timeout_s,
            )
            if priority:
                state.priority_until_at = max(state.priority_until_at, time.monotonic() + 0.4)
            return reply

        return self._exchange_line_direct(
            line,
            read_timeout_s=read_timeout_s,
            read_limit=read_limit,
            priority=priority,
        )

    def _exchange_line_direct(
        self,
        line: str,
        *,
        read_timeout_s: float,
        read_limit: int,
        priority: bool,
    ) -> str:
        payload = ((line or "").strip() + "\n").encode("utf-8")
        state = _esp_endpoint_state(self.host, self.port)
        if priority:
            state.priority_until_at = max(state.priority_until_at, time.monotonic() + 2.0)
        else:
            quiet_until = state.priority_until_at
            if quiet_until > time.monotonic():
                time.sleep(min(quiet_until - time.monotonic(), 0.5))
        lock_wait_s = max(0.5, min(self.timeout_s + read_timeout_s + 20.0, 60.0))
        if not state.lock.acquire(timeout=lock_wait_s):
            raise TimeoutError(f"ESP command channel busy >{lock_wait_s:.1f}s")
        try:
            with _esp_interprocess_lock(self.host, self.port, lock_wait_s):
                deadline = time.monotonic() + max(
                    3.0,
                    min(self.timeout_s + (read_timeout_s * _ESP_COMMAND_MAX_ATTEMPTS) + 2.0, 18.0),
                )
                last_error: Optional[Exception] = None
                for attempt in range(_ESP_COMMAND_MAX_ATTEMPTS):
                    now = time.monotonic()
                    if state.next_allowed_at > now:
                        time.sleep(min(state.next_allowed_at - now, max(0.0, deadline - now)))
                    if time.monotonic() >= deadline:
                        break
                    if state.last_ok_at > 0.0:
                        quiet_for_s = time.monotonic() - state.last_ok_at
                        min_spacing_s = 0.0 if priority else _ESP_COMMAND_MIN_SPACING_S
                        if quiet_for_s < min_spacing_s:
                            time.sleep(
                                min(
                                    min_spacing_s - quiet_for_s,
                                    max(0.0, deadline - time.monotonic()),
                                )
                            )
                    try:
                        if (
                            state.sock is not None
                            and state.last_ok_at > 0.0
                            and (time.monotonic() - state.last_ok_at) > _ESP_COMMAND_IDLE_REUSE_S
                        ):
                            # The ESP firmware closes an answered command socket
                            # after its own idle window. Close a little earlier on
                            # the Raspi so the next command does not first hit a
                            # half-closed W5500 socket.
                            state.close_socket()
                        if state.sock is None:
                            state.sock = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
                            _tune_command_socket(state.sock)
                            state.connect_count += 1
                        state.sock.settimeout(read_timeout_s)
                        state.sock.sendall(payload)
                        reply = _recv_line(state.sock, limit=read_limit).strip()
                        if not reply:
                            raise RuntimeError("ESP endpoint empty reply")
                        if reply.strip().upper() == "NAK_BUSY" and attempt < (_ESP_COMMAND_MAX_ATTEMPTS - 1):
                            # NAK_Busy can be emitted before the command is
                            # processed when another short-lived process still
                            # owns the ESP single-client TCP slot. Treat the
                            # first occurrences as transport contention and let
                            # the final attempt return a real process NAK_Busy.
                            raise RuntimeError("ESP endpoint busy")
                        state.fail_count = 0
                        state.next_allowed_at = 0.0
                        state.exchange_count += 1
                        state.last_ok_at = time.monotonic()
                        state.last_error = ""
                        if priority:
                            state.priority_until_at = max(state.priority_until_at, time.monotonic() + 0.4)
                        if _ESP_COMMAND_CLOSE_AFTER_RESPONSE:
                            state.close_socket(count_reconnect=False)
                        return reply
                    except Exception as exc:
                        last_error = exc
                        state.last_error = repr(exc)
                        state.fail_count += 1
                        state.next_allowed_at = time.monotonic() + min(0.8, 0.15 * (2 ** min(state.fail_count, 3)))
                        state.close_socket()
                        time.sleep(min(0.15, max(0.0, deadline - time.monotonic())))
                if last_error is not None:
                    raise last_error
                raise TimeoutError("ESP endpoint command deadline exceeded")
        finally:
            state.lock.release()

    def close(self) -> None:
        state = _esp_endpoint_state(self.host, self.port)
        broker = state.broker
        if broker is not None:
            broker.close_socket()
            return
        with state.lock:
            state.close_socket()

    def diagnostics(self) -> dict[str, object]:
        state = _esp_endpoint_state(self.host, self.port)
        broker = state.broker
        if broker is not None:
            return broker.diagnostics()
        with state.lock:
            return {
                "host": self.host,
                "port": self.port,
                "enabled": False,
                "connected": state.sock is not None,
                "connect_count": state.connect_count,
                "reconnect_count": state.reconnect_count,
                "exchange_count": state.exchange_count,
                "fail_count": state.fail_count,
                "last_ok_at": state.last_ok_at,
                "last_error": state.last_error,
                "priority_until_at": state.priority_until_at,
            }


class UltimateClient:
    def __init__(self, host: str, port: int, timeout_s: float):
        self.host = (host or "").strip()
        self.port = int(port or 0)
        self.timeout_s = float(timeout_s or 1.0)

    def command(self, command: str, args: Iterable[str] | None = None) -> tuple[bool, str, list[str]]:
        if not self.host or self.port <= 0:
            raise RuntimeError("Ultimate endpoint missing")

        payload = build_ultimate_command(command, args)
        state = _ultimate_endpoint_state(self.host, self.port)
        with state.lock:
            last_error: Optional[Exception] = None
            for _attempt in range(2):
                try:
                    if state.sock is None:
                        state.sock = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
                        _tune_command_socket(state.sock)
                        state.connect_count += 1
                    state.sock.settimeout(self.timeout_s)
                    state.sock.sendall(payload)
                    raw = _recv_until(state.sock, b"\r\n", limit=65536)
                    if not raw:
                        raise RuntimeError("Ultimate endpoint empty reply")
                    state.fail_count = 0
                    state.next_allowed_at = 0.0
                    state.exchange_count += 1
                    state.last_ok_at = time.monotonic()
                    state.last_error = ""
                    return parse_ultimate_result(raw)
                except Exception as exc:
                    last_error = exc
                    state.last_error = repr(exc)
                    state.close_socket()
                    state.fail_count += 1
                    time.sleep(min(0.2, 0.05 * state.fail_count))
            assert last_error is not None
            raise last_error

    def close(self) -> None:
        state = _ultimate_endpoint_state(self.host, self.port)
        with state.lock:
            state.close_socket()


class ZipherClient:
    def __init__(self, host: str, port: int, timeout_s: float):
        self.host = (host or "").strip()
        self.port = int(port or 0)
        self.timeout_s = float(timeout_s or 1.0)
        self._trx = 0
        self._lock = threading.Lock()

    def transact(self, message_id: int, body: bytes = b"") -> tuple[int, bytes]:
        if not self.host or self.port <= 0:
            raise RuntimeError("ZBC endpoint missing")

        state = _zbc_endpoint_state(self.host, self.port)
        with state.lock:
            last_error: Optional[Exception] = None
            for _attempt in range(2):
                if state.sock is None:
                    state.sock = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
                    _tune_command_socket(state.sock)
                    state.connect_count += 1
                sock = state.sock
                trx = self._next_trx()
                msg = build_zbc_message(message_id, body or b"")
                pkt = build_zbc_packet(flags=0x03, transaction_id=trx, sequence_id=0, payload=msg, force_checksum=True)
                try:
                    sock.settimeout(self.timeout_s)
                    sock.sendall(pkt)

                    first = self._read_packet(sock)
                    if first.flags & ZBC_FLAG_NAK:
                        raise RuntimeError("ZBC transport NAK")

                    response = first
                    if (first.flags & ZBC_FLAG_ACK) and not first.payload:
                        response = self._read_packet(sock)
                        if response.flags & ZBC_FLAG_NAK:
                            raise RuntimeError("ZBC payload NAK")

                    if response.payload:
                        ack = build_zbc_ack(response.flags, response.transaction_id, response.sequence_id)
                        sock.sendall(ack)

                    state.fail_count = 0
                    state.next_allowed_at = 0.0
                    state.exchange_count += 1
                    state.last_ok_at = time.monotonic()
                    state.last_error = ""
                    return parse_zbc_message(response.payload)
                except Exception as exc:
                    last_error = exc
                    state.last_error = repr(exc)
                    state.close_socket()
                    state.fail_count += 1
                    time.sleep(min(0.2, 0.05 * state.fail_count))
            assert last_error is not None
            raise last_error

    def close(self) -> None:
        state = _zbc_endpoint_state(self.host, self.port)
        with state.lock:
            state.close_socket()

    def _next_trx(self) -> int:
        with self._lock:
            self._trx = (self._trx + 1) & 0xFFFF
            return self._trx

    def _read_packet(self, sock: socket.socket):
        start = _recv_exact(sock, 1)
        while start and start[0] != ZBC_START:
            start = _recv_exact(sock, 1)

        hdr_rest = _recv_exact(sock, 9)
        size = int.from_bytes(hdr_rest[1:3], "little", signed=False)
        remaining = size - 10
        if remaining < 0:
            raise RuntimeError("ZBC packet size invalid")
        tail = _recv_exact(sock, remaining) if remaining else b""
        full = start + hdr_rest + tail
        return parse_zbc_packet(full)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    out = bytearray()
    while len(out) < n:
        chunk = sock.recv(n - len(out))
        if not chunk:
            raise RuntimeError("socket closed during read")
        out.extend(chunk)
    return bytes(out)


def _recv_until(sock: socket.socket, marker: bytes, limit: int = 65536) -> bytes:
    buf = bytearray()
    while True:
        chunk = sock.recv(1024)
        if not chunk:
            break
        buf.extend(chunk)
        if marker in buf:
            break
        if len(buf) >= limit:
            raise RuntimeError("response too large")
    return bytes(buf)


def _recv_line(sock: socket.socket, limit: int = 8192) -> str:
    data = _recv_until(sock, b"\n", limit=limit)
    first_line = data.split(b"\n", 1)[0]
    return first_line.decode("utf-8", errors="replace").strip()


def _tune_command_socket(sock: socket.socket) -> None:
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception:
        pass
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except Exception:
        pass

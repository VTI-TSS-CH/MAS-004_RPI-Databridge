from __future__ import annotations

import contextlib
import contextvars
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
        self.exchange_count = 0
        self.connect_count = 0
        self.reconnect_count = 0
        self.last_ok_at = 0.0
        self.last_error = ""

    def close_socket(self, *, count_reconnect: bool = True) -> None:
        sock = self.sock
        self.sock = None
        if sock is not None:
            if count_reconnect:
                self.reconnect_count += 1
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

        payload = ((line or "").strip() + "\n").encode("utf-8")
        read_timeout_s = float(read_timeout_s or self.timeout_s)
        state = _esp_endpoint_state(self.host, self.port)
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
                        if quiet_for_s < _ESP_COMMAND_MIN_SPACING_S:
                            time.sleep(
                                min(
                                    _ESP_COMMAND_MIN_SPACING_S - quiet_for_s,
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
        with state.lock:
            state.close_socket()

    def diagnostics(self) -> dict[str, object]:
        state = _esp_endpoint_state(self.host, self.port)
        with state.lock:
            return {
                "host": self.host,
                "port": self.port,
                "connected": state.sock is not None,
                "connect_count": state.connect_count,
                "reconnect_count": state.reconnect_count,
                "exchange_count": state.exchange_count,
                "fail_count": state.fail_count,
                "last_ok_at": state.last_ok_at,
                "last_error": state.last_error,
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

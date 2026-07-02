from __future__ import annotations

import queue as queue_module
import socket
import threading
from typing import Callable

from mas004_rpi_databridge.config import Settings
from mas004_rpi_databridge.db import DB
from mas004_rpi_databridge.device_bridge import DeviceBridge
from mas004_rpi_databridge.io_master import IoStore
from mas004_rpi_databridge.logstore import LogStore
from mas004_rpi_databridge.machine_runtime import (
    BAND_BREAK_ERROR_KEYS,
    MachineRuntime,
    PROCESS_SENSOR_FAULT_STATES,
    PRODUCTION_START_BLOCK_CODE,
    PRODUCTION_START_BLOCK_REASON,
    band_break_monitoring_active,
    microtom_state_queue_options,
    parse_machine_event_line,
    production_start_motion_enabled,
)
from mas004_rpi_databridge.outbox import Outbox
from mas004_rpi_databridge.params import ParamStore
from mas004_rpi_databridge.production_logs import ProductionLogManager
from mas004_rpi_databridge.peers import peer_urls
from mas004_rpi_databridge.protocol import parse_operation_line, parse_param_line
from mas004_rpi_databridge.state_dedupe import ValueDedupeStore, stored_value_equals
from mas004_rpi_databridge.vj6530_poller import Vj6530Poller
from mas004_rpi_databridge.vj6530_runtime import RUNTIME as VJ6530_RUNTIME


RPI_AUTHORITATIVE_MA_KEYS = {
    "MAS0001",
    "MAS0002",
    "MAS0028",
    "MAS0030",
}

POSITION_ACTUAL_STATUS_KEYS = frozenset(
    {
        "MAS0011",
        "MAS0012",
        "MAS0013",
        "MAS0014",
        "MAS0015",
        "MAS0016",
        "MAS0017",
        "MAS0031",
        "MAS0032",
    }
)
POSITION_ACTUAL_RELEVANT_DELTA_TENTHS_MM = 10

ESP_PUSH_CONNECTION_LOG = False


class _MachineEventDispatcher:
    def __init__(self, maxsize: int = 512):
        self._queue: queue_module.Queue[tuple[Settings, dict, str]] = queue_module.Queue(maxsize=maxsize)
        self._thread = threading.Thread(target=self._run, daemon=True, name="esp-machine-event-dispatcher")
        self._thread.start()

    def submit(self, cfg: Settings, event: dict, raw_line: str) -> bool:
        try:
            self._queue.put_nowait((cfg, dict(event or {}), str(raw_line or "")))
            return True
        except queue_module.Full:
            return False

    def wait_idle(self, timeout_s: float = 5.0) -> bool:
        done = threading.Event()

        def waiter():
            self._queue.join()
            done.set()

        threading.Thread(target=waiter, daemon=True).start()
        return done.wait(max(0.0, float(timeout_s)))

    def _run(self):
        while True:
            cfg, machine_event, raw_line = self._queue.get()
            try:
                db = DB(cfg.db_path)
                params = ParamStore(db)
                outbox = Outbox(db)
                logs = LogStore(db)
                logs.log("esp-plc", "in", f"esp->raspi: {raw_line}")
                logs.log("raspi", "in", f"esp-plc push: {raw_line}")
                runtime = MachineRuntime(cfg, db, params, IoStore(db), logs, outbox)
                result = runtime.handle_event(machine_event)
                resp = "ACK_EVT" if result.get("ok") else "NAK_EVT_ASYNC"
                logs.log("esp-plc", "out", f"raspi->esp: {resp}")
            except Exception as exc:
                try:
                    LogStore(DB(cfg.db_path)).log("esp-plc", "error", f"async event dispatch failed: {repr(exc)}")
                except Exception:
                    pass
            finally:
                self._queue.task_done()


_EVENT_DISPATCHER = _MachineEventDispatcher()


def _is_ipv4(s: str) -> bool:
    try:
        parts = [int(p) for p in (s or "").split(".")]
        return len(parts) == 4 and all(0 <= p <= 255 for p in parts)
    except Exception:
        return False


def _configure_socket(sock: socket.socket, *, set_timeout: bool = True):
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception:
        pass
    if set_timeout:
        sock.settimeout(1.0)


def _channel_for_ptype(ptype: str) -> str:
    ptype = (ptype or "").upper()
    if ptype.startswith("TT"):
        return "vj6530"
    if ptype.startswith("LS"):
        return "vj3350"
    if ptype.startswith("MA"):
        return "esp-plc"
    return "raspi"


def _truthy_value(value: object) -> bool:
    text = str(value or "").strip().lower()
    return text not in ("", "0", "false", "off", "no", "none", "null")


def _current_machine_state(params: ParamStore) -> int:
    try:
        return int(float(str(params.get_effective_value("MAS0001") or "1").strip()))
    except Exception:
        return 1


def _duplicate_inactive_fault_clear(params: ParamStore, pkey: str, value: object) -> bool:
    if _truthy_value(value):
        return False
    if not (pkey.startswith("MAE") or pkey.startswith("MAW")):
        return False
    try:
        return not _truthy_value(params.get_effective_value(pkey))
    except Exception:
        return False


def _duplicate_position_actual(params: ParamStore, pkey: str, value: object) -> bool:
    if pkey not in POSITION_ACTUAL_STATUS_KEYS:
        return False
    try:
        current = str(params.get_effective_value(pkey)).strip()
        incoming = str(value).strip()
        if current == incoming:
            return True
        return abs(int(float(current)) - int(float(incoming))) < POSITION_ACTUAL_RELEVANT_DELTA_TENTHS_MM
    except Exception:
        return False


def _duplicate_stored_value(params: ParamStore, pkey: str, value: object) -> bool:
    if _truthy_value(value) and (pkey == "MAE0027" or pkey in BAND_BREAK_ERROR_KEYS):
        return False
    return stored_value_equals(params, pkey, value)


class EspPushListener:
    def __init__(self, cfg: Settings, log: Callable[[str], None]):
        self.cfg = cfg
        self.log = log
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None

    def start(self) -> bool:
        bind_ip = (self.cfg.eth1_ip or "").strip()
        bind_port = int(self.cfg.esp_port or 0)
        if bool(self.cfg.esp_simulation) or not _is_ipv4(bind_ip) or bind_port <= 0:
            self.log(
                f"[ESP-PUSH] disabled sim={self.cfg.esp_simulation} bind_ip={bind_ip!r} bind_port={bind_port}"
            )
            return False

        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            _configure_socket(s, set_timeout=False)
            s.bind((bind_ip, bind_port))
            s.listen(32)
            self._sock = s
        except Exception as exc:
            self.log(f"[ESP-PUSH] FAIL bind {bind_ip}:{bind_port} err={repr(exc)}")
            return False

        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()
        self.log(f"[ESP-PUSH] listen {bind_ip}:{bind_port}")
        return True

    def stop(self):
        self._stop.set()
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass

    def _accept_loop(self):
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                client, addr = self._sock.accept()
            except OSError:
                break
            except Exception:
                continue
            _configure_socket(client)
            threading.Thread(target=self._handle_client, args=(client, addr), daemon=True).start()

    def _handle_client(self, client: socket.socket, addr):
        peer = f"{addr[0]}:{addr[1]}"
        buffer = b""
        if ESP_PUSH_CONNECTION_LOG:
            self.log(f"[ESP-PUSH] open {peer}")
        try:
            while not self._stop.is_set():
                try:
                    chunk = client.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    resp = self._process_line(line)
                    client.sendall((resp + "\n").encode("utf-8"))
        except Exception as exc:
            self.log(f"[ESP-PUSH] client error {peer} err={repr(exc)}")
        finally:
            try:
                client.close()
            except Exception:
                pass
            if ESP_PUSH_CONNECTION_LOG:
                self.log(f"[ESP-PUSH] close {peer}")

    def _process_line(self, line: str) -> str:
        machine_event = parse_machine_event_line(line)
        if machine_event is not None:
            if _EVENT_DISPATCHER.submit(self.cfg, machine_event, line):
                return "ACK_EVT"
            return "NAK_EVT_QUEUE_FULL"

        db = DB(self.cfg.db_path)
        params = ParamStore(db)
        outbox = Outbox(db)
        logs = LogStore(db)
        bridge = DeviceBridge(self.cfg, params, logs)
        production_logs = ProductionLogManager(db, cfg=self.cfg, outbox=outbox)

        parsed = parse_operation_line(line)
        if not parsed:
            logs.log("esp-plc", "in", f"esp->raspi: {line}")
            logs.log("raspi", "in", f"esp-plc push: {line}")
            logs.log("esp-plc", "out", "raspi->esp: NAK_Syntax")
            return "NAK_Syntax"

        ptype, pid, op, value = parsed
        pkey = f"{ptype}{pid}"
        if op == "write":
            if _duplicate_position_actual(params, pkey, value) or _duplicate_stored_value(params, pkey, value):
                return f"ACK_{pkey}={value}"

        logs.log("esp-plc", "in", f"esp->raspi: {line}")
        logs.log("raspi", "in", f"esp-plc push: {line}")

        if not params.get_meta(pkey):
            resp = f"{pkey}=NAK_UnknownParam"
            logs.log("esp-plc", "out", f"raspi->esp: {resp}")
            return resp

        dev = _channel_for_ptype(ptype)
        if dev == "raspi":
            resp = f"{pkey}=NAK_UnsupportedParamType"
            logs.log("esp-plc", "out", f"raspi->esp: {resp}")
            return resp

        if op == "read" and dev == "esp-plc":
            if pkey == "MAS0030":
                production_logs.ready_manifest()
            ok, msg = params.validate_read(pkey, actor="esp32")
            resp = f"{pkey}={params.get_effective_value(pkey)}" if ok else f"{pkey}={msg}"
            logs.log("esp-plc", "out", f"raspi->esp: {resp}")
            return resp

        if op == "read":
            resp = bridge.execute(device=dev, pkey=pkey, ptype=ptype, op="read", value="?", actor="esp32")
            logs.log("esp-plc", "out", f"raspi->esp: {resp}")
            return resp

        if pkey == "MAS0002" and str(value).strip() == "1":
            if not production_start_motion_enabled():
                resp = f"{pkey}={PRODUCTION_START_BLOCK_CODE}"
                logs.log("machine", "warning", f"Start blockiert: {PRODUCTION_START_BLOCK_REASON}")
                logs.log("esp-plc", "out", f"raspi->esp: {resp}")
                return resp
            allowed, reason = production_logs.can_start_new_production()
            if not allowed:
                resp = f"{pkey}={reason}"
                logs.log("raspi", "info", "start blocked: production logfiles of previous batch are still pending")
                logs.log("esp-plc", "out", f"raspi->esp: {resp}")
                return resp

        if pkey in RPI_AUTHORITATIVE_MA_KEYS:
            # The ESP receives these values mirrored from the Raspi. If it
            # echoes an old state/purge bit back later, it must not overwrite
            # the Raspi runtime latch and restart setup as a false purge. In
            # Purge scenario B only Microtom/DIClient may terminate MAS0028.
            current = params.get_effective_value(pkey)
            resp = f"ACK_{pkey}={current}"
            logs.log(
                "raspi",
                "info",
                f"ignored ESP authoritative echo {pkey}={value}; Raspi value is {current}",
            )
            logs.log("esp-plc", "out", f"raspi->esp: {resp}")
            return resp

        if pkey == "MAE0027" and _truthy_value(value) and _current_machine_state(params) not in PROCESS_SENSOR_FAULT_STATES:
            params.apply_device_value("MAE0027", "0")
            resp = "ACK_MAE0027=0"
            logs.log("raspi", "info", "ignored ESP MAE0027=1 outside process sensor states")
            logs.log("esp-plc", "out", f"raspi->esp: {resp}")
            return resp

        if (
            pkey in BAND_BREAK_ERROR_KEYS
            and _truthy_value(value)
            and not band_break_monitoring_active(_current_machine_state(params))
        ):
            params.apply_device_value(pkey, "0")
            resp = f"ACK_{pkey}=0"
            logs.log("raspi", "info", f"ignored ESP {pkey}=1 outside band-break monitor states")
            logs.log("esp-plc", "out", f"raspi->esp: {resp}")
            return resp

        if _duplicate_inactive_fault_clear(params, pkey, value):
            resp = f"ACK_{pkey}=0"
            logs.log("raspi", "info", f"ignored duplicate ESP clear {pkey}=0")
            logs.log("esp-plc", "out", f"raspi->esp: {resp}")
            return resp

        if ptype.startswith("MA"):
            ok, msg = params.apply_device_value(pkey, value)
            if not ok:
                resp = f"{pkey}={msg}"
                logs.log("esp-plc", "out", f"raspi->esp: {resp}")
                return resp
            event = production_logs.handle_param_change(pkey, value)
            if event and event.get("event") == "start":
                logs.log("raspi", "info", f"production logging started: {event.get('production_label')}")
            elif event and event.get("event") == "stop":
                logs.log("raspi", "info", f"production logging ready: {event.get('production_label')}")
            forwarded_line = line
        else:
            resp = bridge.execute(device=dev, pkey=pkey, ptype=ptype, op="write", value=value, actor="esp32")
            logs.log(dev, "out", f"{dev}->raspi: {resp}")
            if "NAK" in resp.upper():
                logs.log("esp-plc", "out", f"raspi->esp: {resp}")
                return resp
            event = production_logs.handle_param_change(pkey, value)
            if event and event.get("event") == "start":
                logs.log("raspi", "info", f"production logging started: {event.get('production_label')}")
            elif event and event.get("event") == "stop":
                logs.log("raspi", "info", f"production logging ready: {event.get('production_label')}")
            parsed_resp = parse_param_line(resp)
            if not parsed_resp or parsed_resp.ptype is None or parsed_resp.value is None:
                logs.log("esp-plc", "out", f"raspi->esp: {resp}")
                return resp
            forwarded_line = f"{parsed_resp.ptype}{parsed_resp.pid}={parsed_resp.value}"

        targets = peer_urls(self.cfg, "/api/inbox")
        if not targets:
            logs.log("raspi", "error", f"no peer_base_url configured; cannot forward ESP push {line}")
        elif not ValueDedupeStore(db).should_send("microtom", pkey, value):
            pass
        else:
            dedupe_key = None
            replace_existing = False
            if ptype in {"MAS", "MAE", "MAW"}:
                dedupe_key, replace_existing = microtom_state_queue_options(pkey, value)
            for url in targets:
                outbox.enqueue(
                    "POST",
                    url,
                    {},
                    {"msg": forwarded_line, "source": "raspi", "origin": "esp-plc"},
                    None,
                    priority=100,
                    dedupe_key=dedupe_key,
                    drop_if_duplicate=bool(dedupe_key),
                    replace_existing=replace_existing,
                )
            logs.log("raspi", "out", f"forward to microtom: {forwarded_line}")

        if dev == "vj6530" and op == "write":
            try:
                result = Vj6530Poller(self.cfg, params, logs, outbox).poll_once(force=True)
                if int(result.get("changed", 0) or 0) > 0:
                    logs.log(
                        "raspi",
                        "info",
                        f"vj6530 post-write sync for {pkey}: changed={result.get('changed', 0)} forwarded={result.get('forwarded', 0)}",
                    )
            except Exception as exc:
                logs.log("raspi", "error", f"vj6530 post-write sync failed for {pkey}: {repr(exc)}")

        resp = f"ACK_{pkey}={value}"
        logs.log("esp-plc", "out", f"raspi->esp: {resp}")
        return resp


class EspPushListenerManager:
    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self.listener: EspPushListener | None = None
        self._lock = threading.Lock()

    @staticmethod
    def _sig(cfg: Settings):
        return ((cfg.eth1_ip or "").strip(), int(cfg.esp_port or 0), bool(cfg.esp_simulation))

    def start(self):
        with self._lock:
            self._apply(self.cfg)

    def reconcile(self, cfg: Settings):
        with self._lock:
            if self._sig(cfg) == self._sig(self.cfg):
                return
            self.cfg = cfg
            print("[ESP-PUSH] reconcile listener", flush=True)
            self._apply(cfg)

    def stop(self):
        with self._lock:
            if self.listener:
                self.listener.stop()
                self.listener = None

    def _apply(self, cfg: Settings):
        if self.listener:
            self.listener.stop()
            self.listener = None
        listener = EspPushListener(cfg, print)
        if listener.start():
            self.listener = listener

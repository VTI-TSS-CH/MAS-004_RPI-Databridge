import json
from typing import Optional, Tuple

from mas004_rpi_databridge.config import Settings
from mas004_rpi_databridge.device_bridge import DeviceBridge
from mas004_rpi_databridge.inbox import Inbox
from mas004_rpi_databridge.machine_runtime import (
    BAND_BREAK_ERROR_KEYS,
    PROCESS_SENSOR_FAULT_STATES,
    band_break_monitoring_active,
    mark_external_purge_clear,
    recent_external_purge_clear,
)
from mas004_rpi_databridge.outbox import Outbox
from mas004_rpi_databridge.params import ParamStore
from mas004_rpi_databridge.production_logs import ProductionLogManager
from mas004_rpi_databridge.logstore import LogStore
from mas004_rpi_databridge.protocol import parse_operation_line, parse_param_line
from mas004_rpi_databridge.peers import peer_urls
from mas004_rpi_databridge.vj6530_poller import Vj6530Poller
from mas004_rpi_databridge.vj6530_runtime import RUNTIME as VJ6530_RUNTIME


RPI_AUTHORITATIVE_MA_KEYS = {
    # Machine state and high-level runtime latches are owned by the Raspi
    # runtime. The ESP receives mirrored values, but Microtom reads must never
    # be answered from a stale ESP mirror.
    "MAS0001",
    "MAS0002",
    "MAS0028",
    "MAS0030",
}


def _channel_for_operation(params: ParamStore, ptype: str, pid: str, op: str = "") -> str:
    ptype = (ptype or "").upper()
    if ptype.startswith("TT"):  # TTP/TTE/TTW
        return "vj6530"
    if ptype.startswith("LS"):  # LSE/LSW
        return "vj3350"
    if ptype.startswith("MA"):  # MAP/MAS/MAE/MAW
        pkey = f"{ptype}{pid}"
        if pkey in RPI_AUTHORITATIVE_MA_KEYS:
            return "raspi"
        meta = params.get_meta(pkey)
        esp_access = params.actor_access(pkey, actor="esp32") if meta else "N"
        if meta and esp_access in {"N", "R"}:
            return "raspi"
        return "esp-plc"
    return "raspi"


def _extract_msg_line(body_json: Optional[str]) -> Optional[str]:
    if body_json is None:
        return None
    try:
        obj = json.loads(body_json)
    except Exception:
        s = str(body_json).strip()
        return s if s else None

    if isinstance(obj, str):
        return obj.strip() if obj.strip() else None
    if isinstance(obj, dict):
        for k in ("msg", "line", "text", "cmd"):
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None
    return None


def _is_device_source(source: Optional[str]) -> bool:
    source_l = (source or "").strip().lower()
    if not source_l:
        return False
    return any(
        token in source_l
        for token in (
            "smartwickler",
            "wickler",
            "unwinder",
            "rewinder",
            "abwickler",
            "aufwickler",
            "esp-plc",
            "esp32",
            "vj6530",
            "vj3350",
            "zbc",
            "laser",
            "tto",
        )
    )


def _is_esp_source(source: Optional[str]) -> bool:
    source_l = (source or "").strip().lower()
    return "esp" in source_l or "plc" in source_l


def _truthy_value(value: object) -> bool:
    text = str(value or "").strip().lower()
    return text not in ("", "0", "false", "off", "no", "none", "null")


def _current_machine_state(params: ParamStore) -> int:
    try:
        return int(float(str(params.get_effective_value("MAS0001") or "1").strip()))
    except Exception:
        return 1


class Router:
    def __init__(self, cfg: Settings, inbox: Inbox, outbox: Outbox, params: ParamStore, logs: LogStore):
        self.cfg = cfg
        self.inbox = inbox
        self.outbox = outbox
        self.params = params
        self.logs = logs
        self.device_bridge = DeviceBridge(cfg, params, logs)
        self.production_logs = ProductionLogManager(params.db, cfg=cfg, outbox=outbox)

    def _enqueue_to_microtom(
        self,
        line: str,
        correlation: Optional[str] = None,
        origin: Optional[str] = None,
        dedupe_key: Optional[str] = None,
        replace_existing: bool = False,
    ):
        targets = peer_urls(self.cfg, "/api/inbox")
        if not targets:
            self.logs.log("raspi", "error", "no peer_base_url configured; cannot enqueue message to microtom")
            return

        headers = {}
        if correlation:
            headers["X-Correlation-Id"] = correlation

        body = {"msg": line, "source": "raspi"}
        if origin:
            body["origin"] = origin

        for url in targets:
            self.outbox.enqueue(
                "POST",
                url,
                headers,
                body,
                None,
                priority=10,
                dedupe_key=dedupe_key,
                drop_if_duplicate=bool(dedupe_key),
                replace_existing=replace_existing,
            )

    def handle_microtom_line(self, line: str, correlation: Optional[str]) -> Optional[str]:
        parsed = parse_operation_line(line)
        if not parsed:
            return None

        ptype, pid, op, value = parsed
        pkey = f"{ptype}{pid}"
        dev = _channel_for_operation(self.params, ptype, pid, op=op)

        self.logs.log("raspi", "in", f"microtom: {line}")
        self.logs.log(dev, "in", f"raspi-> {dev}: {line}")

        if pkey == "MAS0002" and op == "write" and str(value).strip() == "1":
            allowed, reason = self.production_logs.can_start_new_production()
            if not allowed:
                resp = f"{pkey}={reason}"
                self.logs.log("raspi", "info", "start blocked: production logfiles of previous batch are still pending")
                self.logs.log(dev, "out", f"{dev}->raspi: {resp}")
                self.logs.log("raspi", "out", f"to microtom: {resp}")
                self._enqueue_to_microtom(resp, correlation=correlation)
                return resp

        if pkey == "MAS0030" and op == "read":
            self.production_logs.ready_manifest()

        resp = self.device_bridge.execute(device=dev, pkey=pkey, ptype=ptype, op=op, value=value, actor="microtom")
        self.logs.log(dev, "out", f"{dev}->raspi: {resp}")
        self.logs.log("raspi", "out", f"to microtom: {resp}")
        if op == "write" and "NAK" not in resp.upper():
            if pkey == "MAS0028" and str(value).strip() == "0":
                mark_external_purge_clear(self.params.db, source="microtom")
                deleted = self.outbox.delete_status_updates("MAS0028")
                if deleted:
                    self.logs.log("raspi", "info", f"cleared {deleted} pending stale MAS0028 status callback(s)")
            event = self.production_logs.handle_param_change(pkey, value)
            if event and event.get("event") == "start":
                self.logs.log("raspi", "info", f"production logging started: {event.get('production_label')}")
            elif event and event.get("event") == "stop":
                self.logs.log("raspi", "info", f"production logging ready: {event.get('production_label')}")
        self._enqueue_to_microtom(resp, correlation=correlation)
        self._mirror_success_to_esp(pkey, resp)
        self._refresh_vj6530_state_after_success(dev, op, pkey)
        return resp

    def handle_device_line(self, line: str, source: Optional[str], correlation: Optional[str]) -> Optional[str]:
        parsed = parse_operation_line(line)
        if not parsed:
            return None

        ptype, pid, op, value = parsed
        pkey = f"{ptype}{pid}"
        device_source = (source or "device").strip() or "device"
        self.logs.log(device_source, "out", f"{device_source}->raspi: {line}")

        if op != "write":
            self.logs.log("raspi", "info", f"device message ignored (read op): {line}")
            return None

        if pkey == "MAS0028" and _truthy_value(value) and recent_external_purge_clear(self.params.db):
            self.logs.log(
                "raspi",
                "info",
                f"ignored stale device-origin MAS0028=1 from {device_source} immediately after external clear",
            )
            return f"ACK_{pkey}=1"

        if pkey == "MAE0027" and _truthy_value(value) and _current_machine_state(self.params) not in PROCESS_SENSOR_FAULT_STATES:
            self.params.apply_device_value("MAE0027", "0", promote_default=False)
            self.logs.log(
                "raspi",
                "info",
                f"ignored stale device-origin MAE0027=1 from {device_source} outside process sensor states",
            )
            return "ACK_MAE0027=0"

        if (
            pkey in BAND_BREAK_ERROR_KEYS
            and _truthy_value(value)
            and not band_break_monitoring_active(_current_machine_state(self.params))
        ):
            self.params.apply_device_value(pkey, "0", promote_default=False)
            self.logs.log(
                "raspi",
                "info",
                f"ignored stale device-origin {pkey}=1 from {device_source} outside band-break monitor states",
            )
            return f"ACK_{pkey}=0"

        ok, detail = self.params.apply_device_value(pkey, str(value), promote_default=False)
        if not ok:
            self.logs.log("raspi", "error", f"device value rejected for {pkey} from {device_source}: {detail}")
            return f"{pkey}={detail}"

        self.logs.log("raspi", "in", f"{device_source}: {line}")

        response_line: Optional[str] = None
        if self.params.can_actor_read(pkey, actor="microtom"):
            response_line = f"{pkey}={value}"
            self.logs.log("raspi", "out", f"to microtom: {response_line}")
            state_signal = ptype in {"MAS", "MAE", "MAW"}
            dedupe = f"state:{pkey}" if state_signal else None
            self._enqueue_to_microtom(
                response_line,
                correlation=correlation,
                origin=device_source,
                dedupe_key=dedupe,
                replace_existing=state_signal,
            )
        else:
            self.logs.log("raspi", "info", f"skip microtom forward for {pkey}: microtom access=N")

        if self.params.can_actor_read(pkey, actor="esp32") and not _is_esp_source(device_source):
            try:
                mirror_ok, mirror_detail = self.device_bridge.mirror_to_esp(pkey, str(value))
            except Exception as exc:
                mirror_ok, mirror_detail = False, repr(exc)
            if mirror_ok:
                self.logs.log("raspi", "out", f"forward device value to esp-plc: {pkey}={value}")
            else:
                self.logs.log("raspi", "info", f"skip esp mirror for device value {pkey}: {mirror_detail}")

        return response_line

    def _mirror_success_to_esp(self, pkey: str, response_line: str):
        parsed = parse_param_line(response_line)
        if not parsed or parsed.ptype is None or parsed.value is None:
            return
        if parsed.value.upper().startswith("NAK"):
            return
        if not self.params.can_actor_read(pkey, actor="esp32"):
            return
        ok, detail = self.device_bridge.mirror_to_esp(pkey, parsed.value)
        if ok:
            self.logs.log("raspi", "out", f"forward to esp-plc: {pkey}={parsed.value}")
        else:
            self.logs.log("raspi", "info", f"skip esp mirror for {pkey}: {detail}")

    def _refresh_vj6530_state_after_success(self, device: str, op: str, pkey: str):
        if device != "vj6530" or op != "write":
            return
        try:
            result = Vj6530Poller(self.cfg, self.params, self.logs, self.outbox).poll_once(force=True)
            if int(result.get("changed", 0) or 0) > 0:
                self.logs.log(
                    "raspi",
                    "info",
                    f"vj6530 post-write sync for {pkey}: changed={result.get('changed', 0)} forwarded={result.get('forwarded', 0)}",
                )
        except Exception as exc:
            self.logs.log("raspi", "error", f"vj6530 post-write sync failed for {pkey}: {repr(exc)}")

    def tick_once(self) -> bool:
        msg = self.inbox.claim_next_pending()
        if not msg:
            return False

        line = _extract_msg_line(msg.body_json)
        if not line:
            self.logs.log("raspi", "info", f"microtom msg id={msg.id} ohne 'msg/line/text/cmd' -> ignoriert")
            self.inbox.ack(msg.id)
            return True

        try:
            if _is_device_source(msg.source):
                self.handle_device_line(line, source=msg.source, correlation=msg.idempotency_key)
            else:
                self.handle_microtom_line(line, correlation=msg.idempotency_key)
        except Exception as e:
            self.logs.log("raspi", "error", f"router error for inbox id={msg.id}: {repr(e)}")
        finally:
            self.inbox.ack(msg.id)

        return True

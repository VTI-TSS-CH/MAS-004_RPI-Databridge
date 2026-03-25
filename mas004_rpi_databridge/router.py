import json
from typing import Optional, Tuple

from mas004_rpi_databridge.config import Settings
from mas004_rpi_databridge.device_bridge import DeviceBridge
from mas004_rpi_databridge.inbox import Inbox
from mas004_rpi_databridge.outbox import Outbox
from mas004_rpi_databridge.params import ParamStore
from mas004_rpi_databridge.production_logs import ProductionLogManager
from mas004_rpi_databridge.logstore import LogStore
from mas004_rpi_databridge.protocol import parse_operation_line, parse_param_line
from mas004_rpi_databridge.peers import peer_urls
from mas004_rpi_databridge.vj6530_poller import Vj6530Poller
from mas004_rpi_databridge.vj6530_runtime import RUNTIME as VJ6530_RUNTIME


def _channel_for_operation(params: ParamStore, ptype: str, pid: str) -> str:
    ptype = (ptype or "").upper()
    if ptype.startswith("TT"):  # TTP/TTE/TTW
        return "vj6530"
    if ptype.startswith("LS"):  # LSE/LSW
        return "vj3350"
    if ptype.startswith("MA"):  # MAP/MAS/MAE/MAW
        pkey = f"{ptype}{pid}"
        meta = params.get_meta(pkey)
        if meta and params.actor_access(pkey, actor="esp32") == "N":
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

class Router:
    def __init__(self, cfg: Settings, inbox: Inbox, outbox: Outbox, params: ParamStore, logs: LogStore):
        self.cfg = cfg
        self.inbox = inbox
        self.outbox = outbox
        self.params = params
        self.logs = logs
        self.device_bridge = DeviceBridge(cfg, params, logs)
        self.production_logs = ProductionLogManager(params.db, cfg=cfg, outbox=outbox)

    def _enqueue_to_microtom(self, line: str, correlation: Optional[str] = None):
        targets = peer_urls(self.cfg, "/api/inbox")
        if not targets:
            self.logs.log("raspi", "error", "no peer_base_url configured; cannot enqueue message to microtom")
            return

        headers = {}
        if correlation:
            headers["X-Correlation-Id"] = correlation

        for url in targets:
            self.outbox.enqueue(
                "POST",
                url,
                headers,
                {"msg": line, "source": "raspi"},
                None,
                priority=10,
            )

    def handle_microtom_line(self, line: str, correlation: Optional[str]) -> Optional[str]:
        parsed = parse_operation_line(line)
        if not parsed:
            return None

        ptype, pid, op, value = parsed
        pkey = f"{ptype}{pid}"
        dev = _channel_for_operation(self.params, ptype, pid)

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

        resp = self.device_bridge.execute(device=dev, pkey=pkey, ptype=ptype, op=op, value=value, actor="microtom")
        self.logs.log(dev, "out", f"{dev}->raspi: {resp}")
        self.logs.log("raspi", "out", f"to microtom: {resp}")
        if op == "write" and "NAK" not in resp.upper():
            event = self.production_logs.handle_param_change(pkey, value)
            if event and event.get("event") == "start":
                self.logs.log("raspi", "info", f"production logging started: {event.get('production_label')}")
            elif event and event.get("event") == "stop":
                self.logs.log("raspi", "info", f"production logging ready: {event.get('production_label')}")
        self._enqueue_to_microtom(resp, correlation=correlation)
        self._mirror_success_to_esp(pkey, resp)
        self._refresh_vj6530_state_after_success(dev, op, pkey)
        return resp

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
        if bool(getattr(self.cfg, "vj6530_async_enabled", True)) and VJ6530_RUNTIME.session_active():
            return
        try:
            result = Vj6530Poller(self.cfg, self.params, self.logs, self.outbox).poll_once()
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
            self.handle_microtom_line(line, correlation=msg.idempotency_key)
        except Exception as e:
            self.logs.log("raspi", "error", f"router error for inbox id={msg.id}: {repr(e)}")
        finally:
            self.inbox.ack(msg.id)

        return True

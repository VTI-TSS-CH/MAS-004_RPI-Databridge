from __future__ import annotations

from typing import Callable

from mas004_rpi_databridge._vj6530_bridge import ZbcBridgeClient
from mas004_rpi_databridge.config import Settings
from mas004_rpi_databridge.device_bridge import DeviceBridge
from mas004_rpi_databridge.logstore import LogStore
from mas004_rpi_databridge.outbox import Outbox
from mas004_rpi_databridge.params import ParamStore
from mas004_rpi_databridge.peers import peer_urls
from mas004_rpi_databridge.vj6530_runtime import RUNTIME as VJ6530_RUNTIME


class Vj6530Poller:
    def __init__(
        self,
        cfg: Settings,
        params: ParamStore,
        logs: LogStore,
        outbox: Outbox,
        client_factory: Callable[..., ZbcBridgeClient] = ZbcBridgeClient,
    ):
        self.cfg = cfg
        self.params = params
        self.logs = logs
        self.outbox = outbox
        self.client_factory = client_factory

    def poll_once(self) -> dict[str, int]:
        if (
            bool(getattr(self.cfg, "vj6530_async_enabled", True))
            and VJ6530_RUNTIME.session_active()
            and VJ6530_RUNTIME.async_recent(max(6.0, float(getattr(self.cfg, "http_timeout_s", 5.0) or 5.0) + 1.0))
        ):
            self.logs.log("raspi", "info", "skip vj6530 poll: async owner healthy")
            return {"checked": 0, "changed": 0, "forwarded": 0}
        if bool(getattr(self.cfg, "vj6530_async_enabled", True)) and VJ6530_RUNTIME.async_event_recent(10.0):
            self.logs.log("raspi", "info", "skip vj6530 poll: recent async event")
            return {"checked": 0, "changed": 0, "forwarded": 0}

        rows = []
        rows.extend(self.params.list_params(ptype="TTP", limit=5000, offset=0))
        rows.extend(self.params.list_params(ptype="TTE", limit=5000, offset=0))
        rows.extend(self.params.list_params(ptype="TTW", limit=5000, offset=0))
        rows.extend(self.params.list_params(ptype="TTS", limit=5000, offset=0))

        mapping_by_key: dict[str, str] = {}
        current_by_key: dict[str, str] = {}
        for row in rows:
            pkey = str(row.get("pkey") or "").strip()
            mapping = str(row.get("zbc_mapping") or "").strip()
            upper = mapping.upper()
            if not pkey or not mapping:
                continue
            if not (upper.startswith("STATUS[") or upper.startswith("STS[") or upper.startswith("IRQ{")):
                continue
            mapping_by_key[pkey] = mapping
            current_by_key[pkey] = str(row.get("effective_v") if row.get("effective_v") is not None else "0")

        if not mapping_by_key:
            return {"checked": 0, "changed": 0, "forwarded": 0}

        if bool(getattr(self.cfg, "vj6530_async_enabled", True)) and VJ6530_RUNTIME.session_active():
            resolved = VJ6530_RUNTIME.submit_session_request(
                "read_mapped_values",
                mapping_by_key,
                timeout_s=max(3.0, float(self.cfg.http_timeout_s or 5.0) + 2.0),
            )
        else:
            client = self.client_factory(self.cfg.vj6530_host, self.cfg.vj6530_port, timeout_s=self.cfg.http_timeout_s)
            resolved = client.read_mapped_values(mapping_by_key)
        if bool(getattr(self.cfg, "vj6530_async_enabled", True)) and VJ6530_RUNTIME.async_event_recent(10.0):
            self.logs.log("raspi", "info", "discard vj6530 poll result: async event won the race")
            return {"checked": len(mapping_by_key), "changed": 0, "forwarded": 0}
        device_bridge = None

        targets = peer_urls(self.cfg, "/api/inbox")
        changed = 0
        forwarded = 0
        esp_mirroring_available = bool(str(getattr(self.cfg, "esp_host", "") or "").strip()) and int(getattr(self.cfg, "esp_port", 0) or 0) > 0

        for pkey, new_value in resolved.items():
            if new_value is None:
                continue
            new_text = str(new_value)
            old_text = current_by_key.get(pkey, "0")
            if new_text == old_text:
                continue

            ok, msg = self.params.apply_device_value(pkey, new_text, promote_default=True)
            if not ok:
                self.logs.log("raspi", "error", f"vj6530 poll persist failed for {pkey}: {msg}")
                continue

            line = f"{pkey}={new_text}"
            self.logs.log("vj6530", "in", f"poll: {line}")
            self.logs.log("raspi", "in", f"vj6530 poll: {line}")

            if targets and self.params.can_actor_read(pkey, actor="microtom"):
                for url in targets:
                    self.outbox.enqueue(
                        "POST",
                        url,
                        {},
                        {"msg": line, "source": "raspi", "origin": "vj6530"},
                        None,
                        priority=100,
                        dedupe_key=f"vj6530:{pkey}",
                        drop_if_duplicate=True,
                    )
                    forwarded += 1
                self.logs.log("raspi", "out", f"forward to microtom: {line}")
            elif not self.params.can_actor_read(pkey, actor="microtom"):
                self.logs.log("raspi", "info", f"skip microtom forward for {pkey}: microtom-no-access")
            else:
                self.logs.log("raspi", "error", f"no peer_base_url configured; cannot forward VJ6530 poll {line}")

            if esp_mirroring_available and self.params.can_actor_read(pkey, actor="esp32"):
                if device_bridge is None:
                    device_bridge = DeviceBridge(self.cfg, self.params, self.logs)
                ok, detail = device_bridge.mirror_to_esp(pkey, new_text)
                if ok:
                    self.logs.log("raspi", "out", f"forward to esp-plc: {line}")
                else:
                    self.logs.log("raspi", "info", f"skip esp mirror for {pkey}: {detail}")

            changed += 1

        return {"checked": len(mapping_by_key), "changed": changed, "forwarded": forwarded}

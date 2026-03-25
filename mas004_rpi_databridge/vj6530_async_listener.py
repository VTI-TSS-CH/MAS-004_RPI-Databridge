from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
import socket
import time

from mas004_rpi_databridge._vj6530_bridge import (
    AsyncSubscriptionId,
    MessageId,
    VJ6530_TCP_NO_CRC_PROFILE,
    ZbcClient,
    resolve_summary_mappings,
    snapshot_to_status_values,
)
from mas004_rpi_databridge.config import Settings
from mas004_rpi_databridge.device_bridge import DeviceBridge
from mas004_rpi_databridge.logstore import LogStore
from mas004_rpi_databridge.outbox import Outbox
from mas004_rpi_databridge.params import ParamStore
from mas004_rpi_databridge.peers import peer_urls
from mas004_rpi_databridge.vj6530_runtime import RUNTIME as VJ6530_RUNTIME

_AIS_FLAG_HIGH_PRIORITY = 0x00000080
_ASYNC_SUBSCRIPTIONS = [
    (int(AsyncSubscriptionId.PRINTER_IS_ONLINE), _AIS_FLAG_HIGH_PRIORITY),
    (int(AsyncSubscriptionId.PRINTER_IS_OFFLINE), _AIS_FLAG_HIGH_PRIORITY),
    (int(AsyncSubscriptionId.PRINTER_ENTERS_WARNING), _AIS_FLAG_HIGH_PRIORITY),
    (int(AsyncSubscriptionId.PRINTER_LEAVES_WARNING), _AIS_FLAG_HIGH_PRIORITY),
    (int(AsyncSubscriptionId.PRINTER_ENTERS_FAULT), _AIS_FLAG_HIGH_PRIORITY),
    (int(AsyncSubscriptionId.PRINTER_LEAVES_FAULT), _AIS_FLAG_HIGH_PRIORITY),
    (int(AsyncSubscriptionId.PRINTER_IS_BUSY), 0),
    (int(AsyncSubscriptionId.PRINTER_IS_NOT_BUSY), 0),
    (int(AsyncSubscriptionId.PRINTER_STARTS_PRINTING), 0),
    (int(AsyncSubscriptionId.PRINTER_FINISHES_PRINTING), 0),
    (int(AsyncSubscriptionId.PRINT_FAILED), _AIS_FLAG_HIGH_PRIORITY),
]
_ASYNC_SUMMARY_SETTLE_S = 3.0
_ASYNC_SUMMARY_RETRY_S = 0.25
_ASYNC_KEEPALIVE_S = 5.0
_ASYNC_RESPONSE_TIMEOUT_S = 1.0
_ASYNC_SUMMARY_RESPONSE_TIMEOUT_S = 6.0
_ASYNC_SESSION_READ_RESPONSE_TIMEOUT_S = 6.0
_ASYNC_SESSION_WRITE_RESPONSE_TIMEOUT_S = 60.0


class Vj6530AsyncListener:
    def __init__(self, cfg: Settings, params: ParamStore, logs: LogStore, outbox: Outbox):
        self.cfg = cfg
        self.params = params
        self.logs = logs
        self.outbox = outbox
        self.device_bridge = DeviceBridge(cfg, params, logs)
        self._status_snapshot: dict[str, bool | str] = {}

    def run_session(self, session_s: float = 30.0):
        client = self._open_async_client()
        try:
            try:
                client.negotiate_host_version()
            except Exception as exc:
                self.logs.log("vj6530", "info", f"async HCV negotiation skipped: {repr(exc)}")
            msg_id, _ = client.subscribe_async(_ASYNC_SUBSCRIPTIONS)
            if int(msg_id) != int(MessageId.NUL):
                raise RuntimeError(f"6530 async subscribe failed with 0x{int(msg_id):04X}")

            profile_name = getattr(getattr(client, "profile", None), "name", "unknown")
            self.logs.log("vj6530", "info", f"async subscription active profile={profile_name}")
            VJ6530_RUNTIME.mark_async_ok()
            VJ6530_RUNTIME.mark_session_active(True)
            try:
                self._sync_summary_until_stable(client, settle_s=_ASYNC_SUMMARY_SETTLE_S)
            except Exception as exc:
                self.logs.log("vj6530", "info", f"async startup summary skipped: {repr(exc)}")

            deadline = None
            if float(session_s or 0.0) > 0.0:
                deadline = time.monotonic() + max(5.0, float(session_s or 30.0))
            last_keepalive_ts = time.monotonic()
            while deadline is None or time.monotonic() < deadline:
                handled_request = self._drain_session_requests(client)
                if handled_request:
                    last_keepalive_ts = time.monotonic()
                    continue
                if (time.monotonic() - last_keepalive_ts) >= _ASYNC_KEEPALIVE_S:
                    client.request_info([])
                    VJ6530_RUNTIME.mark_async_ok()
                    last_keepalive_ts = time.monotonic()
                    continue
                try:
                    msg_id, response = client.receive_unsolicited()
                    VJ6530_RUNTIME.mark_async_ok()
                except socket.timeout:
                    VJ6530_RUNTIME.mark_async_ok()
                    continue
                except TimeoutError:
                    VJ6530_RUNTIME.mark_async_ok()
                    continue
                except OSError:
                    raise

                if int(msg_id) != int(MessageId.AIR):
                    continue

                tag_ids = [int(getattr(tag, "tag_id", 0) or 0) for tag in getattr(response, "tags", [])]
                if not tag_ids:
                    continue

                self.logs.log("vj6530", "in", f"async tags={','.join(f'0x{tag_id:04X}' for tag_id in tag_ids)}")
                self._status_snapshot.update(_status_updates_from_async(tag_ids))
                VJ6530_RUNTIME.mark_async_event()
                self._sync_from_snapshot()

                if _needs_summary_sync(tag_ids):
                    try:
                        self._sync_summary_until_stable(client, settle_s=_ASYNC_SUMMARY_SETTLE_S)
                    except Exception as exc:
                        self.logs.log("vj6530", "error", f"async summary refresh failed: {repr(exc)}")
        finally:
            VJ6530_RUNTIME.mark_session_active(False)
            client.close()

    def _open_async_client(self) -> ZbcClient:
        timeout_s = max(1.0, float(self.cfg.http_timeout_s or 5.0))
        async_profile = replace(
            VJ6530_TCP_NO_CRC_PROFILE,
            ack_timeout_s=min(VJ6530_TCP_NO_CRC_PROFILE.ack_timeout_s, 1.0),
            response_timeout_s=_ASYNC_RESPONSE_TIMEOUT_S,
        )
        preferred = ZbcClient(
            self.cfg.vj6530_host,
            self.cfg.vj6530_port,
            timeout_s=timeout_s,
            profile=async_profile,
            cache_ttl_s=0.0,
        )
        try:
            preferred.connect()
            return preferred
        except Exception as exc:
            preferred.close()
            self.logs.log("vj6530", "info", f"async preferred profile failed, fallback to autodetect: {repr(exc)}")

        fallback = ZbcClient(
            self.cfg.vj6530_host,
            self.cfg.vj6530_port,
            timeout_s=timeout_s,
            profile=async_profile,
            cache_ttl_s=0.0,
        )
        fallback.connect()
        return fallback

    def _drain_session_requests(self, client: ZbcClient) -> bool:
        handled = False
        while True:
            request = VJ6530_RUNTIME.next_session_request(timeout_s=0.0)
            if request is None:
                return handled
            handled = True
            try:
                fn = getattr(client, request.operation)
                kwargs = dict(request.kwargs)
                if request.operation == "write_mapped_value":
                    kwargs.pop("verify_readback", None)
                response_timeout_s = _session_request_response_timeout_s(self.cfg, request.operation)
                with _temporary_client_timeouts(client, response_timeout_s=response_timeout_s):
                    result = fn(*request.args, **kwargs)
                VJ6530_RUNTIME.mark_async_ok()
                if request.operation.startswith("write_"):
                    request.set_result(result)
                    try:
                        self._sync_summary_until_stable(client, settle_s=_ASYNC_SUMMARY_SETTLE_S)
                    except Exception as exc:
                        self.logs.log("vj6530", "info", f"async post-write summary skipped: {repr(exc)}")
                    continue
                request.set_result(result)
            except Exception as exc:
                request.set_error(exc)

    def _sync_summary_until_stable(self, client: ZbcClient, settle_s: float) -> int:
        deadline = time.monotonic() + max(0.0, float(settle_s or 0.0))
        total_changed = 0
        stable_reads = 0
        response_timeout_s = max(_ASYNC_SUMMARY_RESPONSE_TIMEOUT_S, float(self.cfg.http_timeout_s or 5.0) + 1.0)
        with _temporary_client_timeouts(client, response_timeout_s=response_timeout_s):
            while True:
                changed = self._sync_from_summary(client.request_summary_info(force_refresh=True))
                total_changed += changed
                if changed > 0:
                    stable_reads = 0
                else:
                    stable_reads += 1
                if stable_reads >= 2 or time.monotonic() >= deadline:
                    return total_changed
                time.sleep(_ASYNC_SUMMARY_RETRY_S)

    def _iter_vj6530_rows(self):
        rows = []
        rows.extend(self.params.list_params(ptype="TTP", limit=5000, offset=0))
        rows.extend(self.params.list_params(ptype="TTE", limit=5000, offset=0))
        rows.extend(self.params.list_params(ptype="TTW", limit=5000, offset=0))
        rows.extend(self.params.list_params(ptype="TTS", limit=5000, offset=0))
        return rows

    def _sync_from_snapshot(self) -> int:
        status_values = snapshot_to_status_values(self._status_snapshot)
        resolved: dict[str, str | None] = {}
        for row in self._iter_vj6530_rows():
            pkey = str(row.get("pkey") or "").strip()
            mapping = str(row.get("zbc_mapping") or "").strip()
            status_name = _status_mapping_name(mapping)
            if not pkey or not status_name:
                continue
            resolved[pkey] = _status_value_as_text(status_values, status_name)
        return self._apply_resolved_values(resolved)

    def _sync_from_summary(self, summary) -> int:
        mapping_by_key: dict[str, str] = {}
        for row in self._iter_vj6530_rows():
            pkey = str(row.get("pkey") or "").strip()
            mapping = str(row.get("zbc_mapping") or "").strip()
            upper = mapping.upper()
            if not pkey or not mapping:
                continue
            if not (upper.startswith("STATUS[") or upper.startswith("STS[") or upper.startswith("IRQ{")):
                continue
            mapping_by_key[pkey] = mapping

        if not mapping_by_key:
            return 0

        resolved = resolve_summary_mappings(mapping_by_key, summary, snapshot=self._status_snapshot)
        return self._apply_resolved_values(resolved)

    def _apply_resolved_values(self, resolved: dict[str, str | None]) -> int:
        changed_lines: list[tuple[str, str]] = []
        for pkey, new_value in resolved.items():
            if new_value is None:
                continue
            new_text = str(new_value)
            current_text = str(self.params.get_effective_value(pkey) or "0")
            if current_text == new_text:
                continue

            ok, msg = self.params.apply_device_value(pkey, new_text, promote_default=True)
            if not ok:
                self.logs.log("raspi", "error", f"vj6530 async persist failed for {pkey}: {msg}")
                continue

            line = f"{pkey}={new_text}"
            self.logs.log("vj6530", "in", f"async: {line}")
            self.logs.log("raspi", "in", f"vj6530 async: {line}")
            changed_lines.append((pkey, new_text))

        self._forward_changed_lines(changed_lines)
        return len(changed_lines)

    def _forward_changed_lines(self, changed_lines: list[tuple[str, str]]):
        targets = peer_urls(self.cfg, "/api/inbox")
        for pkey, new_text in changed_lines:
            line = f"{pkey}={new_text}"
            if self.params.can_actor_read(pkey, actor="microtom"):
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
            if targets and self.params.can_actor_read(pkey, actor="microtom"):
                self.logs.log("raspi", "out", f"forward to microtom: {line}")

        for pkey, new_text in changed_lines:
            line = f"{pkey}={new_text}"
            if self.params.can_actor_read(pkey, actor="esp32"):
                ok, detail = self.device_bridge.mirror_to_esp(pkey, new_text)
                if ok:
                    self.logs.log("raspi", "out", f"forward to esp-plc: {line}")
                else:
                    self.logs.log("raspi", "info", f"skip esp mirror for {pkey}: {detail}")


def _needs_summary_sync(tag_ids: list[int]) -> bool:
    return any(
        tag_id
        in {
            int(AsyncSubscriptionId.PRINTER_IS_ONLINE),
            int(AsyncSubscriptionId.PRINTER_IS_OFFLINE),
            int(AsyncSubscriptionId.PRINTER_ENTERS_WARNING),
            int(AsyncSubscriptionId.PRINTER_LEAVES_WARNING),
            int(AsyncSubscriptionId.PRINTER_ENTERS_FAULT),
            int(AsyncSubscriptionId.PRINTER_LEAVES_FAULT),
            int(AsyncSubscriptionId.PRINTER_IS_BUSY),
            int(AsyncSubscriptionId.PRINTER_IS_NOT_BUSY),
            int(AsyncSubscriptionId.PRINTER_STARTS_PRINTING),
            int(AsyncSubscriptionId.PRINTER_FINISHES_PRINTING),
            int(AsyncSubscriptionId.PRINT_FAILED),
        }
        for tag_id in tag_ids
    )


def _status_updates_from_async(tag_ids: list[int]) -> dict[str, bool]:
    updates: dict[str, bool] = {}
    for tag_id in tag_ids:
        if tag_id == int(AsyncSubscriptionId.PRINTER_IS_ONLINE):
            updates["printer_online"] = True
        elif tag_id == int(AsyncSubscriptionId.PRINTER_IS_OFFLINE):
            updates["printer_online"] = False
        elif tag_id == int(AsyncSubscriptionId.PRINTER_ENTERS_WARNING):
            updates["printer_warning"] = True
        elif tag_id == int(AsyncSubscriptionId.PRINTER_LEAVES_WARNING):
            updates["printer_warning"] = False
        elif tag_id == int(AsyncSubscriptionId.PRINTER_ENTERS_FAULT):
            updates["printer_fault"] = True
        elif tag_id == int(AsyncSubscriptionId.PRINTER_LEAVES_FAULT):
            updates["printer_fault"] = False
        elif tag_id == int(AsyncSubscriptionId.PRINTER_IS_BUSY):
            updates["printer_busy"] = True
        elif tag_id == int(AsyncSubscriptionId.PRINTER_IS_NOT_BUSY):
            updates["printer_busy"] = False
        elif tag_id == int(AsyncSubscriptionId.PRINTER_STARTS_PRINTING):
            updates["printer_printing"] = True
            updates["printer_busy"] = True
        elif tag_id in (int(AsyncSubscriptionId.PRINTER_FINISHES_PRINTING), int(AsyncSubscriptionId.PRINT_FAILED)):
            updates["printer_printing"] = False
    return updates


def _temporary_client_timeouts(client: ZbcClient, *, response_timeout_s: float | None = None, ack_timeout_s: float | None = None):
    override = getattr(client, "temporary_timeouts", None)
    if callable(override):
        return override(response_timeout_s=response_timeout_s, ack_timeout_s=ack_timeout_s)
    return nullcontext()


def _session_request_response_timeout_s(cfg: Settings, operation: str) -> float:
    op = str(operation or "").strip().lower()
    base = float(getattr(cfg, "http_timeout_s", 5.0) or 5.0)
    if op.startswith("write_"):
        return max(_ASYNC_SESSION_WRITE_RESPONSE_TIMEOUT_S, base + 5.0)
    if op.startswith("read_"):
        return max(_ASYNC_SESSION_READ_RESPONSE_TIMEOUT_S, base + 1.0)
    return max(_ASYNC_SUMMARY_RESPONSE_TIMEOUT_S, base + 1.0)


def _status_mapping_name(mapping: str) -> str | None:
    text = str(mapping or "").strip()
    upper = text.upper()
    if not (upper.startswith("STATUS[") or upper.startswith("STS[")):
        return None
    start = text.find("[")
    end = text.find("]", start + 1)
    if start < 0 or end <= start:
        return None
    name = text[start + 1:end].strip().upper()
    return name or None


def _status_value_as_text(status_values: dict[str, object], name: str) -> str | None:
    key = (name or "").strip().upper()
    value = {
        "PRINTER_ONLINE": status_values.get("printer_online"),
        "PRINTER_POWERED_DOWN": status_values.get("printer_powered_down"),
        "PRINTER_FAULT": status_values.get("printer_fault"),
        "PRINTER_WARNING": status_values.get("printer_warning"),
        "PRINTER_IMAGING": status_values.get("printer_imaging"),
        "PRINTER_BUSY": status_values.get("printer_busy"),
        "PRINTER_PRINTING": status_values.get("printer_printing"),
        "PRINTER_ACTIVE_ERROR_TYPE": status_values.get("printer_active_error_type"),
        "PRINTER_ACTIVE_ERROR_STRING": status_values.get("printer_active_error_string"),
        "PRINTER_STATE_TEXT": status_values.get("printer_state_text"),
        "PRINTER_STATE_CODE": status_values.get("printer_state_code"),
    }.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)

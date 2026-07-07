from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from mas004_rpi_databridge.config import Settings
from mas004_rpi_databridge.device_clients import EspPlcClient
from mas004_rpi_databridge.io_master import IoStore
from mas004_rpi_databridge.moxa_iologik import MoxaE1211Client, MoxaE1213Client, MoxaProtocolError


_RPIPLC_MODULE = None
_RPIPLC_MODEL = ""
_RPIPLC_ERROR = ""
_RPIPLC_LOCK = threading.RLock()
_MOXA_ENDPOINT_LOCKS: dict[tuple[str, int], threading.Lock] = {}
_MOXA_ENDPOINT_LOCKS_GUARD = threading.Lock()
_MOXA_CLIENTS: dict[tuple[str, int, str, float], Any] = {}
_MOXA_COOLDOWN_UNTIL: dict[tuple[str, int], float] = {}
_MOXA_COOLDOWN_ERROR: dict[tuple[str, int], str] = {}
_ESP_IO_COOLDOWN_UNTIL = 0.0
_ESP_IO_COOLDOWN_ERROR = ""
_DEVICE_OFFLINE_FAILURES: dict[str, int] = {}
_DEVICE_LAST_LIVE_MONOTONIC: dict[str, float] = {}
_MOXA_DEVICE_CODES = {
    "moxa_e1211_1",
    "moxa_e1211_2",
    "moxa_e1213_1",
    "moxa_e1213_2",
    "moxa_e1213_3",
}
_RPIPLC21_ANALOG_DIGITAL_INPUTS = {"I0.7", "I0.8", "I0.9", "I0.10", "I0.11", "I0.12"}


class _MoxaEndpointCooldown(RuntimeError):
    pass


def _to_int01(value: Any) -> int:
    if isinstance(value, bool):
        return 1 if value else 0
    try:
        return 1 if int(str(value).strip()) else 0
    except Exception:
        text = str(value).strip().lower()
        return 1 if text in {"true", "on", "high"} else 0


def _ensure_rpiplc(model: str):
    global _RPIPLC_MODULE, _RPIPLC_MODEL, _RPIPLC_ERROR
    with _RPIPLC_LOCK:
        if _RPIPLC_MODULE is not None and _RPIPLC_MODEL == model:
            return _RPIPLC_MODULE, ""
        try:
            from rpiplc_lib import rpiplc  # type: ignore

            rpiplc.init(model)
            _RPIPLC_MODULE = rpiplc
            _RPIPLC_MODEL = model
            _RPIPLC_ERROR = ""
            return _RPIPLC_MODULE, ""
        except Exception as first_exc:
            try:
                from mas004_rpi_databridge import rpiplc_compat as rpiplc

                rpiplc.init(model)
                _RPIPLC_MODULE = rpiplc
                _RPIPLC_MODEL = model
                _RPIPLC_ERROR = ""
                return _RPIPLC_MODULE, ""
            except Exception as fallback_exc:
                _RPIPLC_MODULE = None
                _RPIPLC_MODEL = ""
                _RPIPLC_ERROR = f"{first_exc}; fallback failed: {fallback_exc}"
                return None, _RPIPLC_ERROR


def _is_rpiplc21_analog_digital_input(cfg: Settings, pin_label: str) -> bool:
    return (
        str(getattr(cfg, "raspi_plc_model", "RPIPLC_21") or "RPIPLC_21").strip() == "RPIPLC_21"
        and str(pin_label or "").strip() in _RPIPLC21_ANALOG_DIGITAL_INPUTS
    )


def _read_raspi_input(rpiplc: Any, cfg: Settings, pin_label: str) -> int:
    pin = str(pin_label or "").strip()
    with _RPIPLC_LOCK:
        if _is_rpiplc21_analog_digital_input(cfg, pin):
            raw = int(rpiplc.analog_read(pin))
            threshold = cfg.get_float("raspi_analog_input_high_threshold", 1000.0)
            return 1 if raw >= threshold else 0
        rpiplc.pin_mode(pin, rpiplc.INPUT)
        return _to_int01(rpiplc.digital_read(pin))


class IoRuntime:
    def __init__(self, cfg: Settings, store: IoStore):
        self.cfg = cfg
        self.store = store

    def refresh(self, *, include_points: bool = True, device_codes: Optional[set[str]] = None) -> Dict[str, Any]:
        wanted_devices = {str(code or "") for code in (device_codes or set()) if str(code or "")}
        if len(wanted_devices) == 1:
            points = self.store.list_points(device_code=next(iter(wanted_devices)), include_reserved=True)
        else:
            points = self.store.list_points(include_reserved=True)
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for point in points:
            if wanted_devices and str(point.get("device_code") or "") not in wanted_devices:
                continue
            grouped.setdefault(point["device_code"], []).append(point)

        devices: List[Dict[str, Any]] = []
        changed = 0

        for device_code, device_points in sorted(
            grouped.items(),
            key=lambda item: (
                item[0] != "esp32_plc58",
                item[0] == "raspi_plc21",
                item[0],
            ),
        ):
            device_result = self._refresh_device(device_code, device_points)
            devices.append(device_result)
            changed += int(device_result.get("changed", 0) or 0)

        result = {
            "ok": True,
            "changed": changed,
            "devices": devices,
        }
        if include_points:
            result["points"] = self.store.list_points(include_reserved=True)
        return result

    def write_output(
        self,
        io_key: str,
        enabled: bool,
        *,
        force: bool = False,
        source: str = "runtime",
        best_effort: bool = False,
        override_owner: bool = False,
    ) -> Dict[str, Any]:
        point = self.store.get_point(io_key)
        if not point:
            raise RuntimeError(f"Unknown IO point '{io_key}'")
        if point["io_dir"] not in {"output", "gpio"}:
            raise RuntimeError(f"IO point '{point['pin_label']}' is not writable")
        requested_value = 1 if enabled else 0
        if bool(point.get("override_active")) and not override_owner:
            override_value = _to_int01(point.get("override_value", point.get("value", "0")))
            override_source = str(point.get("override_source") or "manual-ui")
            self.store.upsert_value(io_key, override_value, "override", override_source)
            result: Dict[str, Any] = {
                "ok": True,
                "overridden": True,
                "requested_value": requested_value,
                "value": override_value,
                "source": source,
                "override_source": override_source,
            }
            if force or requested_value != override_value or _to_int01(point.get("value", "0")) != override_value:
                sync_result = self._write_physical_output(
                    point,
                    bool(override_value),
                    source=override_source,
                    best_effort=True,
                    record_quality="override",
                    record_source=override_source,
                )
                result["override_sync"] = sync_result
            return result
        if self._is_unchanged_live_output(point, requested_value, force=force):
            return {
                "ok": True,
                "skipped_unchanged": True,
                "value": requested_value,
                "source": source,
            }

        return self._write_physical_output(point, bool(enabled), source=source, best_effort=best_effort)

    def _write_physical_output(
        self,
        point: Dict[str, Any],
        enabled: bool,
        *,
        source: str,
        best_effort: bool = False,
        record_quality: str | None = None,
        record_source: str | None = None,
    ) -> Dict[str, Any]:
        io_key = str(point["io_key"])
        requested_value = 1 if enabled else 0
        device_code = point["device_code"]
        if device_code == "esp32_plc58":
            if bool(self.cfg.esp_simulation) or not (self.cfg.esp_host or "").strip() or int(self.cfg.esp_port or 0) <= 0:
                self.store.upsert_value(
                    io_key,
                    requested_value,
                    record_quality or "simulation",
                    record_source or "esp-sim",
                )
                return {"ok": True, "simulation": True, "value": requested_value}
            try:
                connect_timeout_s = self.cfg.get_float("esp_connect_timeout_s", 1.5)
                command_timeout_s = self.cfg.get_float("esp_command_timeout_s", 8.0)
                if best_effort:
                    connect_timeout_s = max(0.2, min(connect_timeout_s, 0.6))
                    command_timeout_s = max(
                        0.2,
                        min(command_timeout_s, self.cfg.get_float("esp_best_effort_write_timeout_s", 1.0)),
                    )
                client = EspPlcClient(
                    self.cfg.esp_host,
                    self.cfg.esp_port,
                    timeout_s=connect_timeout_s,
                )
                response = client.exchange_line(
                    f"IO SET {point['pin_label']}={requested_value}",
                    read_timeout_s=command_timeout_s,
                )
                if "NAK" in (response or "").upper():
                    raise RuntimeError(response)
                self.store.upsert_value(
                    io_key,
                    requested_value,
                    record_quality or "live",
                    record_source or "esp32",
                )
                return {"ok": True, "simulation": False, "response": response, "value": requested_value}
            except Exception as exc:
                if best_effort:
                    return {
                        "ok": False,
                        "simulation": False,
                        "value": _to_int01(point.get("value", "0")),
                        "error": str(exc),
                        "best_effort": True,
                    }
                raise

        if device_code in _MOXA_DEVICE_CODES:
            host, port, simulation = self._moxa_target(device_code)
            if simulation or not host or int(port or 0) <= 0:
                self.store.upsert_value(
                    io_key,
                    requested_value,
                    record_quality or "simulation",
                    record_source or device_code,
                )
                return {"ok": True, "simulation": True, "value": requested_value}
            try:
                def _write():
                    client = self._moxa_client(device_code, host, int(port))
                    return client.write_output_label(str(point["pin_label"]), enabled)

                self._moxa_call(device_code, _write)
                self.store.upsert_value(
                    io_key,
                    requested_value,
                    record_quality or "live",
                    record_source or device_code,
                )
                return {"ok": True, "simulation": False, "value": requested_value}
            except Exception as exc:
                if record_quality:
                    self.store.upsert_value(io_key, point.get("value", "0"), record_quality, record_source or source)
                else:
                    self.store.upsert_value(io_key, point.get("value", "0"), "offline", device_code)
                if best_effort:
                    return {
                        "ok": False,
                        "simulation": False,
                        "value": _to_int01(point.get("value", "0")),
                        "error": str(exc),
                        "best_effort": True,
                    }
                raise

        if device_code == "raspi_plc21":
            if bool(getattr(self.cfg, "raspi_io_simulation", True)):
                self.store.upsert_value(
                    io_key,
                    requested_value,
                    record_quality or "simulation",
                    record_source or "raspi",
                )
                return {"ok": True, "simulation": True, "value": requested_value}
            try:
                rpiplc, error = _ensure_rpiplc(str(getattr(self.cfg, "raspi_plc_model", "RPIPLC_21") or "RPIPLC_21"))
                if rpiplc is None:
                    raise RuntimeError(f"rpiplc unavailable: {error}")
                pin = point["pin_label"]
                with _RPIPLC_LOCK:
                    rpiplc.pin_mode(pin, rpiplc.OUTPUT)
                    rpiplc.digital_write(pin, rpiplc.HIGH if enabled else rpiplc.LOW)
                self.store.upsert_value(
                    io_key,
                    requested_value,
                    record_quality or "live",
                    record_source or "raspi",
                )
                return {"ok": True, "simulation": False, "value": requested_value}
            except Exception as exc:
                if best_effort:
                    return {
                        "ok": False,
                        "simulation": False,
                        "value": _to_int01(point.get("value", "0")),
                        "error": str(exc),
                        "best_effort": True,
                    }
                raise

        raise RuntimeError(f"Unsupported writable IO device '{device_code}'")

    def override_output(self, io_key: str, enabled: bool, source: str = "manual-ui") -> Dict[str, Any]:
        point = self.store.get_point(io_key)
        if not point:
            raise RuntimeError(f"Unknown IO point '{io_key}'")
        if point["io_dir"] not in {"output", "gpio"}:
            raise RuntimeError(f"IO point '{point['pin_label']}' is not writable")
        if self._is_pulse_only(point):
            raise RuntimeError(f"IO point '{point['pin_label']}' is pulse-only and cannot be overridden")
        previous_override_active = bool(point.get("override_active"))
        previous_override_value = point.get("override_value")
        previous_override_source = str(point.get("override_source") or "manual-ui")
        override_result = self.store.set_override(io_key, 1 if enabled else 0, source=source)
        try:
            write_result = self.write_output(
                io_key,
                enabled,
                force=True,
                source=source,
                override_owner=True,
            )
        except Exception:
            if previous_override_active:
                self.store.set_override(io_key, previous_override_value, source=previous_override_source)
            else:
                self.store.release_override(io_key)
            raise
        write_result.update(override_result)
        return write_result

    def release_override(self, io_key: str) -> Dict[str, Any]:
        point = self.store.get_point(io_key)
        if not point:
            raise RuntimeError(f"Unknown IO point '{io_key}'")
        return self.store.release_override(io_key)

    def release_all_overrides(self) -> Dict[str, Any]:
        return self.store.release_all_overrides()

    def _refresh_device(self, device_code: str, device_points: List[Dict[str, Any]]) -> Dict[str, Any]:
        if device_code == "esp32_plc58":
            return self._refresh_esp(device_points)
        if device_code == "raspi_plc21":
            return self._refresh_raspi(device_points)
        if device_code in _MOXA_DEVICE_CODES:
            return self._refresh_moxa(device_code, device_points)
        return self._apply_static_quality(device_code, device_points, "offline", device_code, "Unsupported IO device")

    def _refresh_esp(self, device_points: List[Dict[str, Any]]) -> Dict[str, Any]:
        global _ESP_IO_COOLDOWN_UNTIL, _ESP_IO_COOLDOWN_ERROR
        if bool(self.cfg.esp_simulation):
            return self._apply_static_quality("esp32_plc58", device_points, "simulation", "esp32", "")
        host = (self.cfg.esp_host or "").strip()
        port = int(self.cfg.esp_port or 0)
        if not host or port <= 0:
            return self._apply_static_quality("esp32_plc58", device_points, "offline", "esp32", "ESP endpoint missing")
        now_m = time.monotonic()
        if _ESP_IO_COOLDOWN_UNTIL > now_m:
            result = self._device_result(
                "esp32_plc58",
                device_points,
                False,
                False,
                f"ESP IO cooldown active after {_ESP_IO_COOLDOWN_ERROR}",
                0,
            )
            result["debounced"] = True
            result["cooldown"] = True
            return result
        client: EspPlcClient | None = None
        try:
            client = EspPlcClient(
                host,
                port,
                timeout_s=max(0.25, min(self.cfg.get_float("esp_connect_timeout_s", 1.5), 0.6)),
            )
            diag = client.diagnostics()
            if (
                int(diag.get("queue_depth") or 0) > 0
                or str(diag.get("active_line") or "").strip()
                or float(diag.get("priority_until_at") or 0.0) > time.monotonic()
            ):
                result = self._device_result(
                    "esp32_plc58",
                    device_points,
                    False,
                    False,
                    "ESP IO snapshot skipped: command broker busy/priority active",
                    0,
                )
                result["debounced"] = True
                result["skipped"] = "broker_busy"
                return result
            snapshot_timeout_s = max(
                0.25,
                min(
                    self.cfg.get_float("esp_io_snapshot_timeout_s", 0.75),
                    self.cfg.get_float("esp_read_timeout_s", 2.0),
                    1.0,
                ),
            )
            snapshot_wait_timeout_s = max(
                0.5,
                min(
                    max(0.25, min(self.cfg.get_float("esp_connect_timeout_s", 1.5), 0.6))
                    + snapshot_timeout_s
                    + 0.25,
                    1.2,
                ),
            )
            raw = client.exchange_line(
                "IO SNAPSHOT?",
                read_timeout_s=snapshot_timeout_s,
                wait_timeout_s=snapshot_wait_timeout_s,
            )
            payload = json.loads(raw or "{}")
            snapshot = payload.get("points") if isinstance(payload, dict) else {}
            override_enforced = self._enforce_snapshot_overrides("esp32_plc58", device_points, snapshot or {})
            changed = self._upsert_runtime_values(
                (
                    point,
                    _to_int01((snapshot or {}).get(point["pin_label"], point.get("value", "0"))),
                    "live",
                    "esp32",
                )
                for point in device_points
            )
            _ESP_IO_COOLDOWN_UNTIL = 0.0
            _ESP_IO_COOLDOWN_ERROR = ""
            self._record_device_live("esp32_plc58")
            result = self._device_result("esp32_plc58", device_points, False, True, "", changed)
            if override_enforced:
                result["override_enforced"] = override_enforced
            return result
        except Exception as exc:
            if client is not None and (
                isinstance(exc, TimeoutError)
                or "ESP command broker request timed out" in str(exc)
                or "ESP endpoint command deadline exceeded" in str(exc)
            ):
                try:
                    client.close()
                except Exception:
                    pass
            _ESP_IO_COOLDOWN_ERROR = str(exc)
            _ESP_IO_COOLDOWN_UNTIL = time.monotonic() + max(
                1.0,
                min(self.cfg.get_float("esp_io_error_cooldown_s", 5.0), 30.0),
            )
            return self._apply_static_quality("esp32_plc58", device_points, "offline", "esp32", str(exc))

    def _refresh_raspi(self, device_points: List[Dict[str, Any]]) -> Dict[str, Any]:
        if bool(getattr(self.cfg, "raspi_io_simulation", True)):
            return self._apply_static_quality("raspi_plc21", device_points, "simulation", "raspi", "")
        rpiplc, error = _ensure_rpiplc(str(getattr(self.cfg, "raspi_plc_model", "RPIPLC_21") or "RPIPLC_21"))
        if rpiplc is None:
            return self._apply_static_quality("raspi_plc21", device_points, "offline", "raspi", f"rpiplc unavailable: {error}")
        values: list[tuple[Dict[str, Any], Any, str, str]] = []
        for point in device_points:
            pin = point["pin_label"]
            try:
                if point["io_dir"] == "input":
                    value = _read_raspi_input(rpiplc, self.cfg, pin)
                else:
                    # Raspberry-PLC outputs are write-owned. Reading them back in the
                    # refresh loop can race with blinking/status writes and is not
                    # needed for the HMI state, which is updated by write_output().
                    continue
                values.append((point, value, "live", "raspi"))
            except Exception:
                values.append((point, point.get("value", "0"), "offline", "raspi"))
        changed = self._upsert_runtime_values(values)
        self._record_device_live("raspi_plc21")
        return self._device_result("raspi_plc21", device_points, False, True, "", changed)

    def _refresh_moxa(self, device_code: str, device_points: List[Dict[str, Any]]) -> Dict[str, Any]:
        host, port, simulation = self._moxa_target(device_code)
        if simulation:
            return self._apply_static_quality(device_code, device_points, "simulation", device_code, "")
        if not host or int(port or 0) <= 0:
            return self._apply_static_quality(device_code, device_points, "offline", device_code, "MOXA endpoint missing")
        try:
            def _read():
                client = self._moxa_client(device_code, host, int(port))
                labels = [
                    str(point.get("pin_label") or "").strip()
                    for point in device_points
                    if str(point.get("pin_label") or "").strip()
                ]
                return client.read_outputs(labels=labels)

            snapshot = self._moxa_call(device_code, _read)
            override_enforced = self._enforce_snapshot_overrides(device_code, device_points, snapshot or {})
            changed = self._upsert_runtime_values(
                (
                    point,
                    _to_int01(snapshot.get(point["pin_label"], point.get("value", "0"))),
                    "live",
                    device_code,
                )
                for point in device_points
            )
            self._record_device_live(device_code)
            result = self._device_result(device_code, device_points, False, True, "", changed)
            if override_enforced:
                result["override_enforced"] = override_enforced
            return result
        except _MoxaEndpointCooldown as exc:
            result = self._device_result(device_code, device_points, False, False, str(exc), 0)
            result["debounced"] = True
            result["cooldown"] = True
            return result
        except Exception as exc:
            return self._apply_static_quality(device_code, device_points, "offline", device_code, str(exc))

    def _apply_static_quality(
        self,
        device_code: str,
        device_points: List[Dict[str, Any]],
        quality: str,
        source: str,
        error: str,
    ) -> Dict[str, Any]:
        if quality == "offline" and self._debounce_transient_offline(device_code, device_points):
            result = self._device_result(device_code, device_points, False, False, error, 0)
            result["debounced"] = True
            return result
        values: list[tuple[Dict[str, Any], Any, str, str]] = []
        for point in device_points:
            fallback = point.get("value", "0")
            if (
                device_code == "esp32_plc58"
                and quality == "simulation"
                and str(point.get("pin_label") or "") in {"I0.7", "I0.8"}
            ):
                # On the Microtom test Raspi no ESP32-PLC hardware is present.
                # Safety OK inputs are active-high; keep them high in simulation
                # so a missing PLC does not create a permanent fake Not-Aus.
                fallback = "1"
            values.append((point, fallback, quality, source))
        changed = self._upsert_runtime_values(values)
        return self._device_result(device_code, device_points, quality == "simulation", False, error, changed)

    def _enforce_snapshot_overrides(
        self,
        device_code: str,
        device_points: List[Dict[str, Any]],
        snapshot: Dict[str, Any],
    ) -> list[Dict[str, Any]]:
        enforced: list[Dict[str, Any]] = []
        for point in device_points:
            if not bool(point.get("override_active")):
                continue
            if point.get("io_dir") not in {"output", "gpio"}:
                continue
            pin_label = str(point.get("pin_label") or "").strip()
            if not pin_label or pin_label not in snapshot:
                continue
            override_value = _to_int01(point.get("override_value", point.get("value", "0")))
            observed_value = _to_int01(snapshot.get(pin_label))
            if observed_value == override_value:
                continue
            override_source = str(point.get("override_source") or "manual-ui")
            write_result = self._write_physical_output(
                point,
                bool(override_value),
                source=override_source,
                best_effort=True,
                record_quality="override",
                record_source=override_source,
            )
            write_result.update(
                {
                    "io_key": point.get("io_key"),
                    "device_code": device_code,
                    "pin_label": pin_label,
                    "observed_value": observed_value,
                    "override_value": override_value,
                }
            )
            enforced.append(write_result)
        return enforced

    def _record_device_live(self, device_code: str) -> None:
        _DEVICE_OFFLINE_FAILURES.pop(str(device_code or ""), None)
        _DEVICE_LAST_LIVE_MONOTONIC[str(device_code or "")] = time.monotonic()

    def _debounce_transient_offline(self, device_code: str, device_points: List[Dict[str, Any]]) -> bool:
        code = str(device_code or "")
        failures = int(_DEVICE_OFFLINE_FAILURES.get(code, 0) or 0) + 1
        _DEVICE_OFFLINE_FAILURES[code] = failures
        threshold = max(1, int(self.cfg.get_float("io_offline_debounce_failures", 3.0)))
        grace_s = max(0.0, min(self.cfg.get_float("io_offline_grace_s", 8.0), 60.0))
        if failures >= threshold:
            return False

        now_m = time.monotonic()
        last_live_m = float(_DEVICE_LAST_LIVE_MONOTONIC.get(code, 0.0) or 0.0)
        if last_live_m > 0.0 and (now_m - last_live_m) <= grace_s:
            return True

        now_ts = time.time()
        for point in device_points:
            quality = str(point.get("quality") or "").lower()
            if quality not in {"live", "override"}:
                continue
            updated_ts = float(point.get("updated_ts") or 0.0)
            if updated_ts > 0.0 and (now_ts - updated_ts) <= grace_s:
                return True
        return False

    def _upsert_runtime_values(self, values) -> int:
        items: list[tuple[str, Any, str, str]] = []
        for point, value, quality, source in values:
            if bool(point.get("override_active")):
                items.append(
                    (
                        point["io_key"],
                        _to_int01(point.get("override_value", point.get("value", "0"))),
                        "override",
                        point.get("override_source") or "manual-ui",
                    )
                )
            else:
                items.append((point["io_key"], value, quality, source))
        return self.store.upsert_values(items)

    def _upsert_runtime_value(self, point: Dict[str, Any], value: Any, quality: str, source: str) -> bool:
        if bool(point.get("override_active")):
            return self.store.upsert_value(
                point["io_key"],
                _to_int01(point.get("override_value", point.get("value", "0"))),
                "override",
                point.get("override_source") or "manual-ui",
            )
        return self.store.upsert_value(point["io_key"], value, quality, source)

    def _is_pulse_only(self, point: Dict[str, Any]) -> bool:
        return False

    def _device_result(
        self,
        device_code: str,
        device_points: List[Dict[str, Any]],
        simulation: bool,
        reachable: bool,
        error: str,
        changed: int,
    ) -> Dict[str, Any]:
        sample = device_points[0] if device_points else {}
        host, port = self._device_address(device_code)
        return {
            "device_code": device_code,
            "device_label": sample.get("device_label", device_code),
            "host": host,
            "port": port,
            "simulation": simulation,
            "reachable": reachable,
            "error": error,
            "changed": changed,
            "point_count": len(device_points),
        }

    def _device_address(self, device_code: str) -> Tuple[str, int]:
        if device_code == "esp32_plc58":
            return str(self.cfg.esp_host or ""), int(self.cfg.esp_port or 0)
        if device_code == "moxa_e1211_1":
            return str(getattr(self.cfg, "moxa1_host", "") or ""), int(getattr(self.cfg, "moxa1_port", 0) or 0)
        if device_code == "moxa_e1211_2":
            return str(getattr(self.cfg, "moxa2_host", "") or ""), int(getattr(self.cfg, "moxa2_port", 0) or 0)
        if device_code == "moxa_e1213_1":
            return str(getattr(self.cfg, "moxa1_host", "") or ""), int(getattr(self.cfg, "moxa1_port", 0) or 0)
        if device_code == "moxa_e1213_2":
            return str(getattr(self.cfg, "moxa2_host", "") or ""), int(getattr(self.cfg, "moxa2_port", 0) or 0)
        if device_code == "moxa_e1213_3":
            return str(getattr(self.cfg, "moxa3_host", "") or ""), int(getattr(self.cfg, "moxa3_port", 0) or 0)
        if device_code == "raspi_plc21":
            return str(self.cfg.eth0_ip or "127.0.0.1"), 0
        return "", 0

    def _moxa_target(self, device_code: str) -> Tuple[str, int, bool]:
        if device_code == "moxa_e1211_1":
            return (
                str(getattr(self.cfg, "moxa1_host", "") or "").strip(),
                int(getattr(self.cfg, "moxa1_port", 0) or 0),
                bool(getattr(self.cfg, "moxa1_simulation", True)),
            )
        if device_code == "moxa_e1211_2":
            return (
                str(getattr(self.cfg, "moxa2_host", "") or "").strip(),
                int(getattr(self.cfg, "moxa2_port", 0) or 0),
                bool(getattr(self.cfg, "moxa2_simulation", True)),
            )
        if device_code == "moxa_e1213_1":
            return (
                str(getattr(self.cfg, "moxa1_host", "") or "").strip(),
                int(getattr(self.cfg, "moxa1_port", 0) or 0),
                bool(getattr(self.cfg, "moxa1_simulation", True)),
            )
        if device_code == "moxa_e1213_2":
            return (
                str(getattr(self.cfg, "moxa2_host", "") or "").strip(),
                int(getattr(self.cfg, "moxa2_port", 0) or 0),
                bool(getattr(self.cfg, "moxa2_simulation", True)),
            )
        if device_code == "moxa_e1213_3":
            return (
                str(getattr(self.cfg, "moxa3_host", "") or "").strip(),
                int(getattr(self.cfg, "moxa3_port", 0) or 0),
                bool(getattr(self.cfg, "moxa3_simulation", True)),
            )
        raise RuntimeError(f"Unsupported MOXA device '{device_code}'")

    def _moxa_client(self, device_code: str, host: str, port: int):
        model = "e1213" if "e1213" in str(device_code).lower() else "e1211"
        timeout_s = self._moxa_timeout_s()
        key = (str(host or ""), int(port), model, float(timeout_s))
        with _MOXA_ENDPOINT_LOCKS_GUARD:
            client = _MOXA_CLIENTS.get(key)
            if client is None:
                if model == "e1213":
                    client = MoxaE1213Client(host, int(port), timeout_s=timeout_s)
                else:
                    client = MoxaE1211Client(host, int(port), timeout_s=timeout_s)
                _MOXA_CLIENTS[key] = client
            return client

    def _moxa_timeout_s(self) -> float:
        # MOXA lives on the local eth1 machine subnet. Keep this much shorter
        # than external HTTP peer timeouts so unreachable I/O modules cannot
        # stall the machine runtime or ESP motor command path for many seconds.
        return max(0.2, min(self.cfg.get_float("moxa_timeout_s", 1.5), 0.6))

    def _moxa_error_cooldown_s(self) -> float:
        # After a timeout, do not hammer the MOXA every machine-runtime tick.
        # A later IO poll or state change will retry after this short window.
        return max(1.0, min(self.cfg.get_float("moxa_error_cooldown_s", 5.0), 30.0))

    def _moxa_lock(self, host: str, port: int) -> threading.Lock:
        key = (str(host or ""), int(port or 0))
        with _MOXA_ENDPOINT_LOCKS_GUARD:
            lock = _MOXA_ENDPOINT_LOCKS.get(key)
            if lock is None:
                lock = threading.Lock()
                _MOXA_ENDPOINT_LOCKS[key] = lock
            return lock

    def _moxa_call(self, device_code: str, operation: Callable[[], Any]) -> Any:
        host, port, _simulation = self._moxa_target(device_code)
        key = (str(host or ""), int(port or 0))
        now = time.monotonic()
        until = float(_MOXA_COOLDOWN_UNTIL.get(key, 0.0) or 0.0)
        if until > now:
            reason = _MOXA_COOLDOWN_ERROR.get(key, "previous MOXA error")
            raise _MoxaEndpointCooldown(f"MOXA cooldown active for {host}:{port} after {reason}")
        lock = self._moxa_lock(host, port)
        with lock:
            try:
                result = operation()
                _MOXA_COOLDOWN_UNTIL.pop(key, None)
                _MOXA_COOLDOWN_ERROR.pop(key, None)
                return result
            except Exception as exc:
                if isinstance(exc, MoxaProtocolError):
                    raise
                _MOXA_COOLDOWN_UNTIL[key] = time.monotonic() + self._moxa_error_cooldown_s()
                _MOXA_COOLDOWN_ERROR[key] = str(exc)
                raise

    def _is_unchanged_live_output(self, point: Dict[str, Any], requested_value: int, *, force: bool) -> bool:
        if force:
            return False
        if point.get("io_dir") not in {"output", "gpio"}:
            return False
        quality = str(point.get("quality") or "").lower()
        if quality not in {"live", "simulation", "override"}:
            return False
        return _to_int01(point.get("value", "0")) == int(requested_value)

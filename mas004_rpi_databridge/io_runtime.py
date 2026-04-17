from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from mas004_rpi_databridge.config import Settings
from mas004_rpi_databridge.device_clients import EspPlcClient
from mas004_rpi_databridge.io_master import IoStore
from mas004_rpi_databridge.moxa_iologik import MoxaE1211Client


_RPIPLC_MODULE = None
_RPIPLC_MODEL = ""
_RPIPLC_ERROR = ""


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
    if _RPIPLC_MODULE is not None and _RPIPLC_MODEL == model:
        return _RPIPLC_MODULE, ""
    try:
        from rpiplc_lib import rpiplc  # type: ignore

        rpiplc.init(model)
        _RPIPLC_MODULE = rpiplc
        _RPIPLC_MODEL = model
        _RPIPLC_ERROR = ""
        return _RPIPLC_MODULE, ""
    except Exception as exc:
        _RPIPLC_MODULE = None
        _RPIPLC_MODEL = ""
        _RPIPLC_ERROR = str(exc)
        return None, _RPIPLC_ERROR


class IoRuntime:
    def __init__(self, cfg: Settings, store: IoStore):
        self.cfg = cfg
        self.store = store

    def refresh(self) -> Dict[str, Any]:
        points = self.store.list_points(include_reserved=True)
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for point in points:
            grouped.setdefault(point["device_code"], []).append(point)

        devices: List[Dict[str, Any]] = []
        changed = 0

        for device_code, device_points in grouped.items():
            device_result = self._refresh_device(device_code, device_points)
            devices.append(device_result)
            changed += int(device_result.get("changed", 0) or 0)

        return {
            "ok": True,
            "changed": changed,
            "devices": devices,
            "points": self.store.list_points(include_reserved=True),
        }

    def write_output(self, io_key: str, enabled: bool) -> Dict[str, Any]:
        point = self.store.get_point(io_key)
        if not point:
            raise RuntimeError(f"Unknown IO point '{io_key}'")
        if point["io_dir"] not in {"output", "gpio"}:
            raise RuntimeError(f"IO point '{point['pin_label']}' is not writable")

        device_code = point["device_code"]
        if device_code == "esp32_plc58":
            if bool(self.cfg.esp_simulation) or not (self.cfg.esp_host or "").strip() or int(self.cfg.esp_port or 0) <= 0:
                self.store.upsert_value(io_key, 1 if enabled else 0, "simulation", "esp-sim")
                return {"ok": True, "simulation": True, "value": 1 if enabled else 0}
            client = EspPlcClient(self.cfg.esp_host, self.cfg.esp_port, timeout_s=self.cfg.http_timeout_s)
            response = client.exchange_line(f"IO SET {point['pin_label']}={1 if enabled else 0}")
            if "NAK" in (response or "").upper():
                raise RuntimeError(response)
            self.store.upsert_value(io_key, 1 if enabled else 0, "live", "esp32")
            return {"ok": True, "simulation": False, "response": response, "value": 1 if enabled else 0}

        if device_code in {"moxa_e1211_1", "moxa_e1211_2"}:
            host, port, simulation = self._moxa_target(device_code)
            if simulation or not host or int(port or 0) <= 0:
                self.store.upsert_value(io_key, 1 if enabled else 0, "simulation", device_code)
                return {"ok": True, "simulation": True, "value": 1 if enabled else 0}
            client = MoxaE1211Client(host, int(port), timeout_s=self.cfg.http_timeout_s)
            client.write_output(int(point["channel_no"] or 0), enabled)
            self.store.upsert_value(io_key, 1 if enabled else 0, "live", device_code)
            return {"ok": True, "simulation": False, "value": 1 if enabled else 0}

        if device_code == "raspi_plc21":
            if bool(getattr(self.cfg, "raspi_io_simulation", True)):
                self.store.upsert_value(io_key, 1 if enabled else 0, "simulation", "raspi")
                return {"ok": True, "simulation": True, "value": 1 if enabled else 0}
            rpiplc, error = _ensure_rpiplc(str(getattr(self.cfg, "raspi_plc_model", "RPIPLC_21") or "RPIPLC_21"))
            if rpiplc is None:
                raise RuntimeError(f"rpiplc unavailable: {error}")
            pin = point["pin_label"]
            rpiplc.pin_mode(pin, rpiplc.OUTPUT)
            rpiplc.digital_write(pin, rpiplc.HIGH if enabled else rpiplc.LOW)
            self.store.upsert_value(io_key, 1 if enabled else 0, "live", "raspi")
            return {"ok": True, "simulation": False, "value": 1 if enabled else 0}

        raise RuntimeError(f"Unsupported writable IO device '{device_code}'")

    def _refresh_device(self, device_code: str, device_points: List[Dict[str, Any]]) -> Dict[str, Any]:
        if device_code == "esp32_plc58":
            return self._refresh_esp(device_points)
        if device_code == "raspi_plc21":
            return self._refresh_raspi(device_points)
        if device_code in {"moxa_e1211_1", "moxa_e1211_2"}:
            return self._refresh_moxa(device_code, device_points)
        return self._apply_static_quality(device_code, device_points, "offline", device_code, "Unsupported IO device")

    def _refresh_esp(self, device_points: List[Dict[str, Any]]) -> Dict[str, Any]:
        if bool(self.cfg.esp_simulation):
            return self._apply_static_quality("esp32_plc58", device_points, "simulation", "esp32", "")
        host = (self.cfg.esp_host or "").strip()
        port = int(self.cfg.esp_port or 0)
        if not host or port <= 0:
            return self._apply_static_quality("esp32_plc58", device_points, "offline", "esp32", "ESP endpoint missing")
        try:
            client = EspPlcClient(host, port, timeout_s=self.cfg.http_timeout_s)
            raw = client.exchange_line("IO SNAPSHOT?", read_timeout_s=max(1.0, float(getattr(self.cfg, "esp_io_poll_interval_s", 1.0) or 1.0) + 1.0))
            payload = json.loads(raw or "{}")
            snapshot = payload.get("points") if isinstance(payload, dict) else {}
            changed = 0
            for point in device_points:
                value = _to_int01((snapshot or {}).get(point["pin_label"], point.get("value", "0")))
                if self.store.upsert_value(point["io_key"], value, "live", "esp32"):
                    changed += 1
            return self._device_result("esp32_plc58", device_points, False, True, "", changed)
        except Exception as exc:
            return self._apply_static_quality("esp32_plc58", device_points, "offline", "esp32", str(exc))

    def _refresh_raspi(self, device_points: List[Dict[str, Any]]) -> Dict[str, Any]:
        if bool(getattr(self.cfg, "raspi_io_simulation", True)):
            return self._apply_static_quality("raspi_plc21", device_points, "simulation", "raspi", "")
        rpiplc, error = _ensure_rpiplc(str(getattr(self.cfg, "raspi_plc_model", "RPIPLC_21") or "RPIPLC_21"))
        if rpiplc is None:
            return self._apply_static_quality("raspi_plc21", device_points, "offline", "raspi", f"rpiplc unavailable: {error}")
        changed = 0
        for point in device_points:
            pin = point["pin_label"]
            try:
                if point["io_dir"] == "input":
                    rpiplc.pin_mode(pin, rpiplc.INPUT)
                    value = _to_int01(rpiplc.digital_read(pin))
                else:
                    rpiplc.pin_mode(pin, rpiplc.OUTPUT)
                    try:
                        value = _to_int01(rpiplc.digital_read(pin))
                    except Exception:
                        value = _to_int01(point.get("value", "0"))
                if self.store.upsert_value(point["io_key"], value, "live", "raspi"):
                    changed += 1
            except Exception:
                if self.store.upsert_value(point["io_key"], point.get("value", "0"), "offline", "raspi"):
                    changed += 1
        return self._device_result("raspi_plc21", device_points, False, True, "", changed)

    def _refresh_moxa(self, device_code: str, device_points: List[Dict[str, Any]]) -> Dict[str, Any]:
        host, port, simulation = self._moxa_target(device_code)
        if simulation:
            return self._apply_static_quality(device_code, device_points, "simulation", device_code, "")
        if not host or int(port or 0) <= 0:
            return self._apply_static_quality(device_code, device_points, "offline", device_code, "MOXA endpoint missing")
        try:
            client = MoxaE1211Client(host, int(port), timeout_s=self.cfg.http_timeout_s)
            snapshot = client.read_outputs()
            changed = 0
            for point in device_points:
                value = _to_int01(snapshot.get(point["pin_label"], point.get("value", "0")))
                if self.store.upsert_value(point["io_key"], value, "live", device_code):
                    changed += 1
            return self._device_result(device_code, device_points, False, True, "", changed)
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
        changed = 0
        for point in device_points:
            fallback = point.get("value", "0")
            if self.store.upsert_value(point["io_key"], fallback, quality, source):
                changed += 1
        return self._device_result(device_code, device_points, quality == "simulation", False, error, changed)

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
        return (
            str(getattr(self.cfg, "moxa2_host", "") or "").strip(),
            int(getattr(self.cfg, "moxa2_port", 0) or 0),
            bool(getattr(self.cfg, "moxa2_simulation", True)),
        )

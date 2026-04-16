from __future__ import annotations

import json
from typing import Any

from mas004_rpi_databridge.config import Settings
from mas004_rpi_databridge.device_clients import EspPlcClient


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        text = f"{value:.6f}".rstrip("0").rstrip(".")
        return text or "0"
    return str(value)


class EspMotorClient:
    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self._esp = EspPlcClient(cfg.esp_host, cfg.esp_port, timeout_s=cfg.http_timeout_s)

    def available(self) -> bool:
        return bool((self.cfg.esp_host or "").strip()) and int(self.cfg.esp_port or 0) > 0 and not bool(self.cfg.esp_simulation)

    def _exchange(self, line: str) -> str:
        if not self.available():
            raise RuntimeError("ESP motor endpoint missing or simulation enabled")
        return self._esp.exchange_line(line, read_timeout_s=max(2.0, float(self.cfg.http_timeout_s or 1.0) + 2.0)).strip()

    def _json(self, line: str) -> dict[str, Any]:
        raw = self._exchange(line)
        payload = raw[5:].strip() if raw.upper().startswith("JSON ") else raw
        try:
            return json.loads(payload)
        except Exception as exc:
            raise RuntimeError(f"ESP returned non-JSON reply for '{line}': {raw}") from exc

    def _ack(self, line: str) -> dict[str, Any]:
        raw = self._exchange(line)
        return {"ok": "NAK" not in raw.upper(), "reply": raw}

    def list_motors(self) -> dict[str, Any]:
        return self._json("MOTOR LIST?")

    def status(self, motor_id: int) -> dict[str, Any]:
        return self._json(f"MOTOR {int(motor_id)} STATUS?")

    def config(self, motor_id: int) -> dict[str, Any]:
        return self._json(f"MOTOR {int(motor_id)} CONFIG?")

    def set_config(self, motor_id: int, values: dict[str, Any]) -> dict[str, Any]:
        tokens = [f"{key}={_format_value(value)}" for key, value in values.items() if value is not None]
        if not tokens:
            return {"ok": True, "reply": "NOOP"}
        return self._ack(f"MOTOR {int(motor_id)} SET " + " ".join(tokens))

    def save(self, motor_id: int) -> dict[str, Any]:
        return self._ack(f"MOTOR {int(motor_id)} SAVE")

    def move_relative_steps(self, motor_id: int, delta_steps: int) -> dict[str, Any]:
        return self._ack(f"MOTOR {int(motor_id)} MOVE_REL_STEPS={int(delta_steps)}")

    def move_relative_mm(self, motor_id: int, delta_mm: float) -> dict[str, Any]:
        return self._ack(f"MOTOR {int(motor_id)} MOVE_REL_MM={_format_value(delta_mm)}")

    def move_absolute_mm(self, motor_id: int, absolute_mm: float) -> dict[str, Any]:
        return self._ack(f"MOTOR {int(motor_id)} MOVE_ABS_MM={_format_value(absolute_mm)}")

    def zero(self, motor_id: int) -> dict[str, Any]:
        return self._ack(f"MOTOR {int(motor_id)} ZERO")

    def set_min(self, motor_id: int) -> dict[str, Any]:
        return self._ack(f"MOTOR {int(motor_id)} SET_MIN")

    def set_max(self, motor_id: int) -> dict[str, Any]:
        return self._ack(f"MOTOR {int(motor_id)} SET_MAX")

    def reset_alarm(self, motor_id: int) -> dict[str, Any]:
        return self._ack(f"MOTOR {int(motor_id)} RESET_ALARM")

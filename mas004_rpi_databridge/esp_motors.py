from __future__ import annotations

import json
from typing import Any

from mas004_rpi_databridge.config import Settings
from mas004_rpi_databridge.device_clients import EspPlcClient, motor_setup_write_context


POSITIONAL_MOTOR_IDS = frozenset({1, 2, 4, 5, 6, 7, 8, 9})
MOTOR_SETUP_PROTECTED_CONFIG_KEYS = frozenset(
    {
        "steps_per_mm",
        "invert_direction",
        "zero_offset_steps",
        "min_tenths_mm",
        "max_tenths_mm",
        "min_enabled",
        "max_enabled",
    }
)
MOTOR_RUNTIME_ALLOWED_CONFIG_KEYS = frozenset(
    {
        "speed_mm_s",
        "current_pct",
        "hold_current_pct",
        "accel_mm_s2",
        "decel_mm_s2",
    }
)


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
        connect_timeout = cfg.get_float("esp_connect_timeout_s", 1.5)
        self._esp = EspPlcClient(cfg.esp_host, cfg.esp_port, timeout_s=connect_timeout)

    def available(self) -> bool:
        return bool((self.cfg.esp_host or "").strip()) and int(self.cfg.esp_port or 0) > 0 and not bool(self.cfg.esp_simulation)

    def _exchange(self, line: str) -> str:
        if not self.available():
            raise RuntimeError("ESP motor endpoint missing or simulation enabled")
        return self._esp.exchange_line(
            line,
            read_timeout_s=max(2.0, self.cfg.get_float("esp_command_timeout_s", 8.0)),
            read_limit=65536,
        ).strip()

    def _json(self, line: str) -> dict[str, Any]:
        raw = self._exchange(line)
        if raw.upper().startswith("NAK"):
            return {"ok": False, "error": raw, "reply": raw}
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

    def poll_state(self) -> dict[str, Any]:
        return self._json("MOTOR POLL?")

    def set_poll(self, enabled: bool) -> dict[str, Any]:
        return self._ack(f"MOTOR POLL={'1' if enabled else '0'}")

    def apply_eto_recovery(self) -> dict[str, Any]:
        return self._ack("MOTOR APPLY_ETO_RECOVERY")

    def recover_eto(self) -> dict[str, Any]:
        return self._ack("MOTOR RECOVER_ETO")

    def recover_eto_motor(self, motor_id: int) -> dict[str, Any]:
        return self._ack(f"MOTOR {int(motor_id)} RECOVER_ETO")

    def status(self, motor_id: int) -> dict[str, Any]:
        return self._json(f"MOTOR {int(motor_id)} STATUS?")

    def refresh(self, motor_id: int) -> dict[str, Any]:
        return self._json(f"MOTOR {int(motor_id)} REFRESH")

    def config(self, motor_id: int) -> dict[str, Any]:
        return self._json(f"MOTOR {int(motor_id)} CONFIG?")

    def _require_machine_setup_write(
        self,
        motor_id: int,
        action: str,
        *,
        allow_machine_setup_write: bool,
    ) -> None:
        if int(motor_id) in POSITIONAL_MOTOR_IDS and not allow_machine_setup_write:
            raise RuntimeError(
                f"{action} fuer Positionsmotor {int(motor_id)} ist nur ueber "
                "/ui/machine-setup/motors erlaubt"
            )

    def _arm_machine_setup_write(
        self,
        motor_id: int,
        *,
        allow_machine_setup_write: bool,
    ) -> None:
        if int(motor_id) not in POSITIONAL_MOTOR_IDS or not allow_machine_setup_write:
            return
        result = self._ack(f"MOTOR {int(motor_id)} SETUP_WRITE_ARM")
        if not bool(result.get("ok", False)):
            raise RuntimeError(
                "ESP Machine-Setup-Schreibfreigabe fehlgeschlagen: "
                + str(result.get("reply") or result)
            )

    def _machine_setup_ack(self, motor_id: int, action: str, line: str) -> dict[str, Any]:
        with motor_setup_write_context(f"motor_{int(motor_id)}:{action}"):
            self._arm_machine_setup_write(
                motor_id,
                allow_machine_setup_write=True,
            )
            return self._ack(line)

    def set_config(
        self,
        motor_id: int,
        values: dict[str, Any],
        *,
        allow_machine_setup_write: bool = False,
    ) -> dict[str, Any]:
        raw_values = values or {}
        config_keys = {str(key) for key, value in raw_values.items() if value is not None}
        if int(motor_id) in POSITIONAL_MOTOR_IDS and not allow_machine_setup_write:
            forbidden_keys = sorted(config_keys - MOTOR_RUNTIME_ALLOWED_CONFIG_KEYS)
            if forbidden_keys:
                raise RuntimeError(
                    "SET_CONFIG(" + ",".join(forbidden_keys) + ") fuer Positionsmotor "
                    f"{int(motor_id)} ist nur ueber /ui/machine-setup/motors erlaubt; "
                    "ausserhalb sind nur speed_mm_s,current_pct,hold_current_pct,"
                    "accel_mm_s2,decel_mm_s2 erlaubt"
                )
        protected_keys = MOTOR_SETUP_PROTECTED_CONFIG_KEYS.intersection(config_keys)
        if protected_keys:
            self._require_machine_setup_write(
                motor_id,
                "SET_CONFIG(" + ",".join(sorted(protected_keys)) + ")",
                allow_machine_setup_write=allow_machine_setup_write,
            )
        tokens = [f"{key}={_format_value(value)}" for key, value in raw_values.items() if value is not None]
        if not tokens:
            return {"ok": True, "reply": "NOOP"}
        if int(motor_id) in POSITIONAL_MOTOR_IDS and allow_machine_setup_write:
            return self._machine_setup_ack(
                motor_id,
                "SET_CONFIG",
                f"MOTOR {int(motor_id)} SET " + " ".join(tokens),
            )
        return self._ack(f"MOTOR {int(motor_id)} SET " + " ".join(tokens))

    def save(self, motor_id: int, *, allow_machine_setup_write: bool = False) -> dict[str, Any]:
        self._require_machine_setup_write(
            motor_id,
            "SAVE",
            allow_machine_setup_write=allow_machine_setup_write,
        )
        if int(motor_id) in POSITIONAL_MOTOR_IDS and allow_machine_setup_write:
            return self._machine_setup_ack(motor_id, "SAVE", f"MOTOR {int(motor_id)} SAVE")
        return self._ack(f"MOTOR {int(motor_id)} SAVE")

    def move_relative_steps(self, motor_id: int, delta_steps: int) -> dict[str, Any]:
        return self._ack(f"MOTOR {int(motor_id)} MOVE_REL_STEPS={int(delta_steps)}")

    def move_relative_mm(self, motor_id: int, delta_mm: float) -> dict[str, Any]:
        return self._ack(f"MOTOR {int(motor_id)} MOVE_REL_MM={_format_value(delta_mm)}")

    def move_absolute_mm(self, motor_id: int, absolute_mm: float) -> dict[str, Any]:
        return self._ack(f"MOTOR {int(motor_id)} MOVE_ABS_MM={_format_value(absolute_mm)}")

    def move_absolute_set_mm(self, targets_mm: dict[int, float]) -> dict[str, Any]:
        tokens = [
            f"{int(motor_id)}={_format_value(float(target_mm))}"
            for motor_id, target_mm in sorted((targets_mm or {}).items())
        ]
        if not tokens:
            return {"ok": True, "results": []}
        return self._json("MOTOR MOVE_ABS_SET " + " ".join(tokens))

    def set_current_position_mm(
        self,
        motor_id: int,
        absolute_mm: float,
        *,
        allow_machine_setup_write: bool = False,
    ) -> dict[str, Any]:
        self._require_machine_setup_write(
            motor_id,
            "SET_POSITION_MM",
            allow_machine_setup_write=allow_machine_setup_write,
        )
        if int(motor_id) in POSITIONAL_MOTOR_IDS and allow_machine_setup_write:
            return self._machine_setup_ack(
                motor_id,
                "SET_POSITION_MM",
                f"MOTOR {int(motor_id)} SET_POSITION_MM={_format_value(absolute_mm)}",
            )
        return self._ack(f"MOTOR {int(motor_id)} SET_POSITION_MM={_format_value(absolute_mm)}")

    def zero(self, motor_id: int, *, allow_machine_setup_write: bool = False) -> dict[str, Any]:
        self._require_machine_setup_write(
            motor_id,
            "ZERO",
            allow_machine_setup_write=allow_machine_setup_write,
        )
        if int(motor_id) in POSITIONAL_MOTOR_IDS and allow_machine_setup_write:
            return self._machine_setup_ack(motor_id, "ZERO", f"MOTOR {int(motor_id)} ZERO")
        return self._ack(f"MOTOR {int(motor_id)} ZERO")

    def set_min(self, motor_id: int, *, allow_machine_setup_write: bool = False) -> dict[str, Any]:
        self._require_machine_setup_write(
            motor_id,
            "SET_MIN",
            allow_machine_setup_write=allow_machine_setup_write,
        )
        if int(motor_id) in POSITIONAL_MOTOR_IDS and allow_machine_setup_write:
            return self._machine_setup_ack(motor_id, "SET_MIN", f"MOTOR {int(motor_id)} SET_MIN")
        return self._ack(f"MOTOR {int(motor_id)} SET_MIN")

    def set_max(self, motor_id: int, *, allow_machine_setup_write: bool = False) -> dict[str, Any]:
        self._require_machine_setup_write(
            motor_id,
            "SET_MAX",
            allow_machine_setup_write=allow_machine_setup_write,
        )
        if int(motor_id) in POSITIONAL_MOTOR_IDS and allow_machine_setup_write:
            return self._machine_setup_ack(motor_id, "SET_MAX", f"MOTOR {int(motor_id)} SET_MAX")
        return self._ack(f"MOTOR {int(motor_id)} SET_MAX")

    def reset_alarm(self, motor_id: int) -> dict[str, Any]:
        return self._ack(f"MOTOR {int(motor_id)} RESET_ALARM")

from __future__ import annotations

import json
import time
from typing import Any

from mas004_rpi_databridge.config import Settings
from mas004_rpi_databridge.db import DB
from mas004_rpi_databridge.device_clients import EspPlcClient
from mas004_rpi_databridge.params import ParamStore
from mas004_rpi_databridge.smart_wickler_client import SmartWicklerClient


DIAG_PARAM_KEYS = (
    "MAS0001",
    "MAS0028",
    "MAE0048",
    "MAE0025",
    "MAE0026",
    "MAE0027",
    "MAE0028",
    "MAE0029",
    "MAE0030",
    "MAE0032",
    "MAE0033",
    "MAE0034",
    "MAP0002",
    "MAP0004",
    "MAP0006",
    "MAP0014",
    "MAP0016",
    "MAP0018",
    "MAP0019",
    "MAP0040",
    "MAP0069",
    "MAP0070",
)


def _truthy(raw: Any) -> bool:
    text = str(raw or "").strip().lower()
    return text not in ("", "0", "false", "off", "no", "none", "null")


def _safe_float(raw: Any, default: float = 0.0) -> float:
    try:
        return float(str(raw or "").strip())
    except Exception:
        return float(default)


def _safe_int(raw: Any, default: int = 0) -> int:
    try:
        return int(float(str(raw or "").strip()))
    except Exception:
        return int(default)


def _esp_json(client: EspPlcClient, command: str, *, read_limit: int = 65536) -> dict[str, Any]:
    reply = client.exchange_line(command, read_timeout_s=2.0, read_limit=read_limit).strip()
    if reply.upper().startswith("JSON "):
        reply = reply[5:].strip()
    if reply.upper().startswith("NAK"):
        raise RuntimeError(f"{command}: {reply}")
    return json.loads(reply or "{}")


def _recent_log_lines(db: DB, limit: int = 120) -> list[dict[str, Any]]:
    patterns = (
        "%MAE0048%",
        "%print_registration%",
        "%production_registration%",
        "%registration%",
        "%production_print%",
        "%post-positioning%",
        "%stop tolerance%",
        "%Stopptoleranz%",
    )
    where = " OR ".join(["message LIKE ?"] * len(patterns))
    items: list[dict[str, Any]] = []
    try:
        with db._conn() as conn:
            rows = conn.execute(
                f"""SELECT ts, channel, direction, message
                    FROM logs
                    WHERE {where}
                    ORDER BY id DESC
                    LIMIT ?""",
                (*patterns, int(limit)),
            ).fetchall()
            event_where = " OR ".join(["event_type LIKE ? OR message LIKE ? OR payload_json LIKE ?"] * len(patterns))
            event_args: list[Any] = []
            for pattern in patterns:
                event_args.extend([pattern, pattern, pattern])
            event_rows = conn.execute(
                f"""SELECT ts, event_type, severity, message
                    FROM machine_events
                    WHERE {event_where}
                    ORDER BY id DESC
                    LIMIT ?""",
                (*event_args, int(limit)),
            ).fetchall()
    except Exception:
        return []
    for row in rows:
        items.append(
            {
                "ts": float(row[0] or 0.0),
                "channel": row[1],
                "direction": row[2],
                "message": row[3],
            }
        )
    for row in event_rows:
        items.append(
            {
                "ts": float(row[0] or 0.0),
                "channel": "machine_event",
                "direction": row[2],
                "message": f"{row[1]}: {row[3]}",
            }
        )
    return sorted(items, key=lambda item: float(item.get("ts") or 0.0))[-int(limit):]


def _param_snapshot(params: ParamStore) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in DIAG_PARAM_KEYS:
        try:
            out[key] = str(params.get_effective_value(key))
        except Exception:
            out[key] = ""
    return out


def _fetch_wickler(cfg: Settings, role: str) -> dict[str, Any]:
    client = SmartWicklerClient(cfg, role)
    if not client.available():
        return {"ok": False, "role": role, "error": "endpoint missing or simulation active"}
    try:
        state = client.fetch_state(timeout_s=2.0)
        telemetry = dict(state.get("telemetry") or {})
        drive = dict(state.get("drive") or {})
        values = dict(state.get("values") or {})
        return {
            "ok": True,
            "role": role,
            "mode": telemetry.get("modeLabel"),
            "wipe_percent": telemetry.get("wipePercent"),
            "external_stop": telemetry.get("externalStopActive"),
            "indexed_mode": telemetry.get("indexedModeEnabled"),
            "indexed_move": telemetry.get("indexedMoveActive"),
            "drive_ready": drive.get("ready"),
            "drive_move": drive.get("move"),
            "drive_alarm": drive.get("alarm"),
            "continuous_ready": drive.get("continuousModeReady"),
            "mae_blocked": values.get("maeBlocked"),
            "mae_high": values.get("maeHigh"),
            "mae_low": values.get("maeLow"),
            "push": state.get("push"),
        }
    except Exception as exc:
        return {"ok": False, "role": role, "error": repr(exc)}


def _motor3_summary(motor_payload: dict[str, Any]) -> dict[str, Any]:
    motor = dict((motor_payload or {}).get("motor") or {})
    state = dict(motor.get("state") or {})
    config = dict(motor.get("config") or {})
    steps_per_mm = _safe_float(config.get("steps_per_mm"), 0.0)
    command_steps = state.get("command_steps")
    feedback_steps = state.get("feedback_steps")
    step_error = None
    step_error_mm = None
    if command_steps is not None and feedback_steps is not None:
        step_error = _safe_int(command_steps) - _safe_int(feedback_steps)
        if steps_per_mm > 0:
            step_error_mm = step_error / steps_per_mm
    return {
        "ok": bool(motor_payload.get("ok", True)),
        "ready": state.get("ready"),
        "busy": state.get("busy"),
        "move": state.get("move"),
        "in_pos": state.get("in_pos"),
        "alarm": state.get("alarm"),
        "alarm_code": state.get("alarm_code"),
        "velocity_mode": state.get("velocity_mode"),
        "target_speed_mm_s": state.get("target_speed_mm_s"),
        "feedback_tenths_mm": state.get("feedback_tenths_mm"),
        "command_tenths_mm": state.get("command_tenths_mm"),
        "target_tenths_mm": state.get("target_tenths_mm"),
        "feedback_steps": feedback_steps,
        "command_steps": command_steps,
        "command_feedback_step_error": step_error,
        "command_feedback_error_mm": step_error_mm,
        "steps_per_mm": steps_per_mm,
        "invert_direction": config.get("invert_direction"),
        "zero_offset_steps": config.get("zero_offset_steps"),
        "last_reply": state.get("last_reply") or motor.get("last_reply"),
    }


def _derive_findings(params: dict[str, str], registration: dict[str, Any], motor3: dict[str, Any]) -> list[str]:
    findings: list[str] = []
    if _truthy(params.get("MAE0048")):
        findings.append("MAE0048 ist aktuell aktiv.")
    reason = str(registration.get("reason") or "").strip()
    if reason:
        findings.append(f"ESP-Registrierdiagnose meldet Grund: {reason}.")
    if registration.get("position_mode"):
        commanded = "kommandiert" if registration.get("position_commanded") is True else "noch nicht kommandiert"
        findings.append(
            "Druckpositionierung laeuft als AZD-Zielweg "
            f"({commanded}, Zielweg {_safe_float(registration.get('position_command_mm'), 0.0):.3f} mm)."
        )
    error = _safe_float(registration.get("abs_error_mm"), 0.0)
    tolerance = max(0.0, _safe_float(registration.get("tolerance_mm"), 0.05))
    max_correction = max(0.0, _safe_float(registration.get("max_correction_mm"), 5.0))
    if error > tolerance:
        findings.append(f"Positionsfehler {error:.4f} mm liegt ausserhalb +/-{tolerance:.4f} mm.")
    if max_correction > 0 and error > max_correction:
        findings.append(f"Positionsfehler liegt ueber dem Korrekturfenster von {max_correction:.3f} mm.")
    attempts = _safe_int(registration.get("registration_attempts"), 0)
    max_attempts = _safe_int(registration.get("max_attempts"), 3)
    if max_attempts > 0 and attempts >= max_attempts and error > tolerance:
        findings.append(f"{attempts}/{max_attempts} Korrekturversuche verbraucht.")
    speed = abs(_safe_float(registration.get("infeed_speed_mm_s"), 0.0))
    settle_speed = max(0.0, _safe_float(registration.get("settle_speed_mm_s"), 2.0))
    if settle_speed > 0 and speed > settle_speed:
        findings.append(f"Einlaufencoder war noch nicht ruhig: {speed:.3f} mm/s > {settle_speed:.3f} mm/s.")
    if registration.get("motor_busy") is True or motor3.get("busy") is True:
        findings.append("Motor 3 war laut Status noch busy.")
    if registration.get("motor_ready") is False or motor3.get("ready") is False:
        findings.append("Motor 3 war laut Status nicht ready.")
    if motor3.get("alarm"):
        findings.append(f"Motor 3 meldet Alarm {motor3.get('alarm_code')}.")
    if not findings:
        findings.append("Keine aktive MAE0048-Ursache im aktuellen Snapshot sichtbar; letzte Events/Logs pruefen.")
    return findings


def collect_mae0048_diagnostics(cfg: Settings | None = None, db: DB | None = None) -> dict[str, Any]:
    cfg = cfg or Settings.load()
    db = db or DB(cfg.db_path)
    params_store = ParamStore(db)
    params = _param_snapshot(params_store)
    result: dict[str, Any] = {
        "ok": True,
        "ts": time.time(),
        "params": params,
        "esp": {},
        "registration": {},
        "motor3": {},
        "wicklers": {},
        "logs": _recent_log_lines(db),
        "errors": [],
    }
    if bool(getattr(cfg, "esp_simulation", False)):
        result["errors"].append("ESP-Simulation aktiv")
    else:
        try:
            client = EspPlcClient(cfg.esp_host, int(cfg.esp_port), timeout_s=cfg.get_float("esp_connect_timeout_s", 1.5))
            result["esp"]["production"] = _esp_json(client, "PROCESS PRODUCTION STATUS?")
            result["registration"] = _esp_json(client, "PROCESS PRODUCTION REG_DIAG?")
            result["esp"]["visualization"] = _esp_json(client, "PROCESS VISUALIZATION?", read_limit=65536)
            result["motor3_raw"] = _esp_json(client, "MOTOR 3 STATUS?", read_limit=65536)
            result["motor3"] = _motor3_summary(result["motor3_raw"])
        except Exception as exc:
            result["errors"].append(f"ESP Diagnose nicht lesbar: {repr(exc)}")
    result["wicklers"] = {
        "unwinder": _fetch_wickler(cfg, "unwinder"),
        "rewinder": _fetch_wickler(cfg, "rewinder"),
    }
    result["findings"] = _derive_findings(params, result.get("registration") or {}, result.get("motor3") or {})
    result["ok"] = not bool(result["errors"])
    return result

from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable, Optional

from mas004_rpi_databridge.db import DB, now_ts
from mas004_rpi_databridge.device_bridge import DeviceBridge
from mas004_rpi_databridge.device_clients import EspPlcClient
from mas004_rpi_databridge.esp_motors import EspMotorClient
from mas004_rpi_databridge.format_semantics import build_format_plan
from mas004_rpi_databridge.io_master import IoStore
from mas004_rpi_databridge.io_runtime import IoRuntime
from mas004_rpi_databridge.logstore import LogStore
from mas004_rpi_databridge.machine_semantics import (
    BUTTON_INPUTS,
    BUTTON_LED_OUTPUTS,
    STATUS_LAMP_OUTPUTS,
    TRANSITION_FINALS,
    action_for_button,
    button_led_plan,
    button_to_command,
    command_to_target_state,
    lamp_outputs_for_state,
    pack_label_status_word,
    parse_button_mask,
    settle_machine_state,
    state_actions,
    state_label,
    target_state_for_button,
)
from mas004_rpi_databridge.motor_master_sync import apply_motor_setup_master_config_to_client
from mas004_rpi_databridge.motor_setup_lock import (
    clear_motor_setup_manual_lock,
    motor_setup_manual_lock_status,
)
from mas004_rpi_databridge.outbox import Outbox
from mas004_rpi_databridge.params import ParamStore
from mas004_rpi_databridge.peers import peer_urls
from mas004_rpi_databridge.production_logs import (
    DEFAULT_PRODUCTION_LOG_DIR,
    ProductionLogManager,
    sanitize_production_label,
)
from mas004_rpi_databridge.setup_wickler_orchestrator import SetupWicklerOrchestrator
from mas004_rpi_databridge.smart_wickler_client import SmartWicklerClient
from mas004_rpi_databridge.state_dedupe import ValueDedupeStore


def _truthy(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw or "").strip().lower()
    if text in ("", "0", "false", "off", "no", "none", "null"):
        return False
    return True


def _safe_int(raw: Any, default: int = 0) -> int:
    try:
        if raw is None:
            return int(default)
        text = str(raw).strip()
        if text == "":
            return int(default)
        return int(float(text))
    except Exception:
        return int(default)


def _safe_float(raw: Any, default: float = 0.0) -> float:
    try:
        if raw is None:
            return float(default)
        text = str(raw).strip()
        if text == "":
            return float(default)
        return float(text)
    except Exception:
        return float(default)


def _event_float_key(raw: Any, digits: int = 3) -> str:
    try:
        return f"{float(raw):.{int(digits)}f}"
    except Exception:
        return str(raw or "")


def _active_mae_keys(param_map: dict[str, str]) -> list[str]:
    return sorted(str(key) for key, value in (param_map or {}).items() if str(key).startswith("MAE") and _truthy(value))


PAUSE_ERROR_KEYS = {"MAE0025", "MAE0026"}
POSITION_AXIS_MAE_BY_MOTOR = {
    1: "MAE0004",
    2: "MAE0005",
    3: "MAE0046",
    4: "MAE0047",
    5: "MAE0010",
    6: "MAE0006",
    7: "MAE0007",
    8: "MAE0009",
    9: "MAE0008",
}
RESETTABLE_SAFETY_ERROR_KEYS = {
    "MAE0001",  # Not-Aus
    "MAE0024",  # Etikettenbandriss
    "MAE0027",  # Etikettensensor prellt
    "MAE0028",  # Abwickler Taenzerarm blockiert
    "MAE0029",  # Abwickler Taenzerarm zu hoch
    "MAE0030",  # Abwickler Taenzerarm zu tief
    "MAE0032",  # Aufwickler Taenzerarm blockiert
    "MAE0033",  # Aufwickler Taenzerarm zu hoch
    "MAE0034",  # Aufwickler Taenzerarm zu tief
    "MAE0048",  # Etikettenantrieb Nachpositionierung fehlgeschlagen
}
WICKLER_DANCER_ERROR_KEYS = {
    "MAE0028",  # Abwickler Taenzerarm blockiert
    "MAE0029",  # Abwickler Taenzerarm zu hoch
    "MAE0030",  # Abwickler Taenzerarm zu tief
    "MAE0032",  # Aufwickler Taenzerarm blockiert
    "MAE0033",  # Aufwickler Taenzerarm zu hoch
    "MAE0034",  # Aufwickler Taenzerarm zu tief
}
WICKLER_ROLE_DANCER_MAE = {
    "unwinder": {"blocked": "MAE0028", "high": "MAE0029", "low": "MAE0030"},
    "rewinder": {"blocked": "MAE0032", "high": "MAE0033", "low": "MAE0034"},
}
PROCESS_SENSOR_FAULT_STATES = {2, 3, 4, 5, 10, 11, 12, 13, 16, 17}
# Bandriss-/Entnahmesensorik ist erst im echten Produktionsfenster
# prozesskritisch. Die Zwischenzustaende 4/6 duerfen keine alten oder noch
# nicht geteachten Sensorbits verriegeln, bevor der Runner sauber startet bzw.
# bevor der Setup-to-Pause-Uebergang abgeschlossen ist.
PROCESS_BAND_BREAK_MONITOR_STATES = {5, 10, 11, 12, 13, 16, 17}
ESP_CRITICAL_IO_MAX_AGE_S = 0.75
ESP_CRITICAL_IO_PINS = {"I0.4", "I0.7", "I0.8", "I0.11"}
ESP_BAND_BREAK_IO_PINS = {"I0.4", "I0.11"}
WICKLER_DANCER_MONITOR_STATES = {2, 3, 4, 5, 6, 7, 10, 11, 12, 13, 16, 17}
BAND_BREAK_ERROR_KEYS = {"MAE0008", "MAE0009", "MAE0024"}
CONDITIONAL_RESETTABLE_SAFETY_ERRORS = {
    # These are latched machine errors. Clear them only if the matching live
    # inputs are quiet while the process sensor monitoring window is active.
    # In Not-Stop/Stop/reset the web sensors may be out of position, so the
    # inputs must not keep a purge latch alive.
    "MAE0008": ("esp32_plc58", "I0.4"),
    "MAE0009": ("esp32_plc58", "I0.11"),
}
SAFETY_RESET_BUTTON = "start_pause"
SAFETY_PHASE_LATCHED = "latched"
SAFETY_PHASE_RESETTING = "resetting"
SAFETY_PHASE_READY = "ready"
SAFETY_PHASE_FAILED = "failed"
ESP_RESET_PULSE_HIGH_S = 0.2
ESP_RESET_PULSE_GAP_S = 1.0
LASER_SYSTEM_READY_PIN = "I0.12"
LASER_READY_PIN = "I0.2"
LASER_START_PIN = "Q0.3"
LASER_SAFETY_RESET_IO_KEY = "moxa_e1213_1__DIO3"
LASER_SAFETY_RESET_PULSE_HIGH_S = 0.2
LASER_START_PULSE_HIGH_S = 0.1
LASER_READY_WAIT_TIMEOUT_S = 20.0
LASER_READY_WAIT_POLL_S = 0.2
LIGHT_CURTAIN_AUTO_RESET_INTERVAL_S = 5.0
LIGHT_CURTAIN_WICKLER_RECOVERY_RETRY_INTERVAL_S = 5.0
LIGHT_CURTAIN_WICKLER_RECOVERY_WINDOW_S = 120.0
PURGE_EXTERNAL_CLEAR_GRACE_S = 3.0
_SAFETY_RESET_LOCK = threading.Lock()
_RESET_MOTION_RECOVERY_LOCK = threading.Lock()
_LIGHT_CURTAIN_WICKLER_RECOVERY_LOCK = threading.Lock()
_SETUP_WICKLER_LOCK = threading.Lock()
_PRODUCTION_MOTION_LOCK = threading.Lock()
_TTO_PRINTER_STATE_LOCK = threading.Lock()
_MACHINE_REFRESH_LOCK = threading.RLock()

_SETUP_PAUSE_RECOVERY_WINDOW_S = 2 * 60 * 60
MACHINE_STATE_HEARTBEAT_WRITE_INTERVAL_S = 5.0
PRODUCTION_RUNTIME_INFO_KEY = "production_runtime"
PRODUCTION_RUNTIME_EVENT_TYPES = {
    "label_complete",
    "production_fault",
    "production_registration_late",
    "production_registration_fault",
    "production_registration_correction",
    "production_registration_correction_effect",
    "production_velocity_stop_for_print",
    "production_first_print_position_commanded",
    "production_first_print_position_reached",
    "production_print_position_commanded",
    "production_print_position_reached",
    "production_wickler_indexed_ready",
    "production_wickler_runline_released",
    "production_print_trigger",
    "production_print_resolved",
    "production_print_position_failed",
}
PRODUCTION_START_MOTION_ENABLED = True
PRODUCTION_START_BLOCK_CODE = "NAK_ProductionRuntimeNotReleased"
PRODUCTION_START_BLOCK_REASON = (
    "Produktionsablauf noch nicht freigegeben: "
    "vollstaendige Label-/Druck-/Wickler-Runtime fehlt"
)
PRODUCTION_START_REQUIRED_RUNTIME = (
    "kontinuierlicher Vorzug mit Label-Schieberegister",
    "Druckpositions-Stopp mit Nachkorrektur",
    "Kamera-/Drucker-Bypasslogik",
    "synchronisierte Wickler-Regelung",
    "Entnahme-/Kontrollsensor-Ablauf",
)
PRODUCTION_MOTOR3_RAMP_MM_S2 = 300.0
PRODUCTION_WICKLER_STANDBY_PERCENT = 50.0
PRODUCTION_WICKLER_MIN_PERCENT = 8.0
PRODUCTION_WICKLER_MAX_PERCENT = 92.0
PRODUCTION_WICKLER_INDEXED_MAX_TRAVEL_MM = 1200.0
PRODUCTION_WICKLER_POST_START_VERIFY_DELAY_S = 0.35
PRODUCTION_WICKLER_MONITOR_INTERVAL_S = 0.5
PRODUCTION_WICKLER_MONITOR_COMM_MAX_MISSES = 3
PRODUCTION_ESP_MONITOR_INTERVAL_S = 0.5
PRODUCTION_ESP_MONITOR_COMM_MAX_MISSES = 3
PRODUCTION_ESP_FIRST_READY_FALLBACK_INTERVAL_S = 0.75
PRODUCTION_ESP_SYNC_KEYS = (
    "MAP0001",
    "MAP0002",
    "MAP0003",
    "MAP0004",
    "MAP0005",
    "MAP0006",
    "MAP0011",
    "MAP0012",
    "MAP0014",
    "MAP0016",
    "MAP0017",
    "MAP0018",
    "MAP0019",
    "MAP0020",
    "MAP0021",
    "MAP0035",
    "MAP0036",
    "MAP0037",
    "MAP0038",
    "MAP0040",
    "MAP0041",
    "MAP0042",
    "MAP0043",
    "MAP0044",
    "MAP0045",
    "MAP0046",
    "MAP0066",
    "MAP0067",
    "MAP0068",
    "MAP0069",
    "MAP0070",
    "MAP0071",
    "MAP0072",
    "MAP0073",
    "MAP0074",
    "MAP0075",
    "MAP0076",
    "MAP0079",
)
PRODUCTION_ESP_START_READBACK_KEYS = (
    "MAP0016",
    "MAP0018",
    "MAP0019",
    "MAP0004",
    "MAP0006",
    "MAP0020",
    "MAP0021",
    "MAP0035",
    "MAP0036",
    "MAP0037",
    "MAP0038",
    "MAP0067",
    "MAP0068",
    "MAP0069",
    "MAP0070",
    "MAP0079",
)
TTO_PRINTER_STATE_PKEY = "TTS0001"
TTO_PRINTER_OFFLINE_CODE = "0"
TTO_PRINTER_ONLINE_CODE = "3"
WICKLER_HARD_ENDSTOP_LOW_PERCENT = 2.0
WICKLER_HARD_ENDSTOP_HIGH_PERCENT = 98.0
WICKLER_HARD_ENDSTOP_MONITOR_INTERVAL_S = 1.0
STOP_MODE_AXIS_TARGETS_MM = {
    5: 0.0,    # Material-Kontrollkamera TV1
    6: -20.0,  # Sensor Etikettenerfassung
    7: -20.0,  # Sensor Auswurfkontrolle
    8: 100.0,  # Etikettenanschlag links
    9: 100.0,  # Etikettenanschlag vorne/rechts
}
STOP_MODE_POSITION_TOLERANCE_TENTHS = 5
STOP_MODE_POSITION_RETRY_S = 60.0
STOP_MODE_POSITION_MAX_ATTEMPTS = 3
STOP_MODE_POSITION_VERIFY_TIMEOUT_S = 8.0
STOP_MODE_POSITION_VERIFY_POLL_S = 0.1
STOP_MODE_POSITION_LOGIC_VERSION = 10
STOP_MODE_POSITION_LIMIT_MARGIN_TENTHS = 1
SETUP_AXIS_POSITION_TOLERANCE_TENTHS = 1
SETUP_AXIS_POSITION_VERIFY_TIMEOUT_S = 45.0
SETUP_AXIS_POSITION_VERIFY_POLL_S = 0.25
SETUP_AXIS_MOVE_SET_MAX_ATTEMPTS = 3
SETUP_AXIS_MOVE_SET_SHORT_VERIFY_TIMEOUT_S = 3.0
MOTOR_HARDWARE_FEEDBACK_IO = {
    # AZD DOUT0 is commissioned as MOVE (function 134).  Where OUT1 is wired,
    # AZD DOUT1 stays IN-POS (function 138) for the positioning-complete edge.
    1: {"move": ("esp32_plc58", "I1.0"), "in_pos": ("esp32_plc58", "I1.1")},
    2: {"move": ("esp32_plc58", "I1.2"), "in_pos": ("esp32_plc58", "I1.3")},
    4: {"move": ("esp32_plc58", "I0.9")},
    5: {"move": ("esp32_plc58", "I0.10")},
    6: {"move": ("esp32_plc58", "I2.0")},
    7: {"move": ("esp32_plc58", "I2.1")},
    8: {"move": ("esp32_plc58", "I2.3")},
    9: {"move": ("esp32_plc58", "I2.2")},
}


def production_start_motion_enabled() -> bool:
    return bool(PRODUCTION_START_MOTION_ENABLED)


def microtom_state_queue_options(pkey: str, value: object) -> tuple[str | None, bool]:
    key = str(pkey or "").strip().upper()
    ptype = key[:3]
    if ptype not in {"MAS", "MAE", "MAW"}:
        return None, False
    if ptype in {"MAE", "MAW"} or key == "MAS0028":
        suffix = "active" if _truthy(value) else "clear"
        return f"state:{key}:{suffix}", not _truthy(value)
    return f"state:{key}", True


def _command_action_name(command: int, current_state: int) -> str | None:
    if int(command or 0) == 1:
        return "start"
    if int(command or 0) == 2:
        return "stop"
    if int(command or 0) == 3:
        return "setup"
    if int(command or 0) == 4:
        return "sync"
    if int(command or 0) == 5:
        return "empty"
    if int(command or 0) == 6:
        return "rewind"
    if int(command or 0) == 7:
        return "pause"
    return None


_LIGHT_CURTAIN_BLOCKED_ACTIONS = {"start", "setup", "sync", "empty", "rewind"}


def mark_external_purge_clear(db: DB, *, source: str = "microtom"):
    state = _machine_state_row_from_db(db)
    info = dict(state.get("info") or {})
    purge_info = dict(info.get("purge") or {})
    purge_info["external_clear_ts"] = now_ts()
    purge_info["external_clear_source"] = str(source or "microtom")
    purge_info.pop("external_active_ts", None)
    purge_info.pop("external_active_source", None)
    info["purge"] = purge_info
    safety_info = dict(info.get("safety") or {})
    if safety_info.get("latched") or safety_info.get("phase") in (SAFETY_PHASE_LATCHED, SAFETY_PHASE_FAILED):
        safety_info = {
            **safety_info,
            "latched": False,
            "phase": SAFETY_PHASE_READY,
            "external_clear_ts": purge_info["external_clear_ts"],
            "external_clear_source": purge_info["external_clear_source"],
        }
        info["safety"] = safety_info
    try:
        ParamStore(db).apply_device_value("MAS0028", "0", promote_default=True)
    except Exception:
        pass
    _write_machine_state_to_db(
        db,
        current_state=state["current_state"],
        requested_state=state["requested_state"],
        state_source=state["state_source"],
        warning_active=state["warning_active"],
        purge_active=False,
        production_label=state["production_label"],
        last_label_no=state["last_label_no"],
        info=info,
    )


def mark_external_purge_start(db: DB, *, source: str = "microtom"):
    state = _machine_state_row_from_db(db)
    info = dict(state.get("info") or {})
    purge_info = dict(info.get("purge") or {})
    purge_info["external_active_ts"] = now_ts()
    purge_info["external_active_source"] = str(source or "microtom")
    purge_info.pop("external_clear_ts", None)
    purge_info.pop("external_clear_source", None)
    info["purge"] = purge_info
    try:
        ParamStore(db).apply_device_value("MAS0028", "1", promote_default=True)
    except Exception:
        pass
    _write_machine_state_to_db(
        db,
        current_state=state["current_state"],
        requested_state=state["requested_state"],
        state_source=state["state_source"],
        warning_active=state["warning_active"],
        purge_active=True,
        production_label=state["production_label"],
        last_label_no=state["last_label_no"],
        info=info,
    )


def external_purge_active(info: dict[str, Any] | None) -> bool:
    purge_info = dict((info or {}).get("purge") or {})
    active_ts, clear_ts = _purge_marker_times(purge_info)
    return active_ts > 0.0 and active_ts >= clear_ts


def _purge_marker_times(purge_info: dict[str, Any] | None) -> tuple[float, float]:
    purge_info = dict(purge_info or {})
    try:
        active_ts = float(purge_info.get("external_active_ts") or 0.0)
    except Exception:
        active_ts = 0.0
    try:
        clear_ts = float(purge_info.get("external_clear_ts") or 0.0)
    except Exception:
        clear_ts = 0.0
    return active_ts, clear_ts


def _merge_newer_purge_info(base_info: dict[str, Any], latest_info: dict[str, Any]) -> dict[str, Any]:
    info = dict(base_info or {})
    latest_purge = dict((latest_info or {}).get("purge") or {})
    if not latest_purge:
        return info
    current_purge = dict(info.get("purge") or {})
    latest_ts = max(_purge_marker_times(latest_purge))
    current_ts = max(_purge_marker_times(current_purge))
    if latest_ts > current_ts:
        info["purge"] = latest_purge
    return info


def recent_external_purge_clear(db: DB, *, max_age_s: float = PURGE_EXTERNAL_CLEAR_GRACE_S) -> bool:
    state = _machine_state_row_from_db(db)
    try:
        clear_ts = float(((state.get("info") or {}).get("purge") or {}).get("external_clear_ts") or 0.0)
    except Exception:
        clear_ts = 0.0
    return clear_ts > 0.0 and (now_ts() - clear_ts) <= max(0.1, float(max_age_s))


def band_break_monitoring_active(machine_state: int) -> bool:
    return int(machine_state or 0) in PROCESS_BAND_BREAK_MONITOR_STATES


def quick_setup_band_break_bypass_active(info: dict[str, Any] | None) -> bool:
    setup_info = dict((info or {}).get("setup") or {})
    result = setup_info.get("last_result") if isinstance(setup_info.get("last_result"), dict) else {}
    return bool((result or {}).get("quick_setup_band_break_bypass"))


def quick_setup_log_bypass_active(info: dict[str, Any] | None) -> bool:
    setup_info = dict((info or {}).get("setup") or {})
    result = setup_info.get("last_result") if isinstance(setup_info.get("last_result"), dict) else {}
    return bool((result or {}).get("quick_setup_log_bypass"))


def _machine_state_row_from_db(db: DB) -> dict[str, Any]:
    with db._conn() as c:
        row = c.execute(
            """SELECT current_state,requested_state,state_source,warning_active,purge_active,
                      production_label,last_label_no,info_json,updated_ts
               FROM machine_state WHERE singleton_id=1"""
        ).fetchone()
    if not row:
        return {
            "current_state": 1,
            "requested_state": 1,
            "state_source": "runtime",
            "warning_active": False,
            "purge_active": False,
            "production_label": "",
            "last_label_no": 0,
            "info": {},
            "updated_ts": 0.0,
        }
    try:
        info = json.loads(row[7] or "{}")
    except Exception:
        info = {}
    return {
        "current_state": int(row[0] or 1),
        "requested_state": int(row[1] or 1),
        "state_source": str(row[2] or "runtime"),
        "warning_active": bool(row[3]),
        "purge_active": bool(row[4]),
        "production_label": str(row[5] or ""),
        "last_label_no": int(row[6] or 0),
        "info": info,
        "updated_ts": float(row[8] or 0.0),
    }


def _write_machine_state_to_db(
    db: DB,
    *,
    current_state: int,
    requested_state: int,
    state_source: str,
    warning_active: bool,
    purge_active: bool,
    production_label: str,
    last_label_no: int,
    info: dict[str, Any],
):
    with db._conn() as c:
        c.execute(
            """INSERT INTO machine_state(
                   singleton_id,current_state,requested_state,state_source,warning_active,purge_active,
                   production_label,last_label_no,info_json,updated_ts
               ) VALUES(1,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(singleton_id) DO UPDATE SET
                 current_state=excluded.current_state,
                 requested_state=excluded.requested_state,
                 state_source=excluded.state_source,
                 warning_active=excluded.warning_active,
                 purge_active=excluded.purge_active,
                 production_label=excluded.production_label,
                 last_label_no=excluded.last_label_no,
                 info_json=excluded.info_json,
                 updated_ts=excluded.updated_ts""",
            (
                int(current_state),
                int(requested_state),
                str(state_source or "runtime"),
                int(bool(warning_active)),
                int(bool(purge_active)),
                str(production_label or ""),
                int(last_label_no or 0),
                json.dumps(info, ensure_ascii=False),
                now_ts(),
            ),
        )


class MachineRuntime:
    def __init__(
        self,
        cfg: Any,
        db: DB,
        params: ParamStore,
        io_store: IoStore,
        logs: LogStore,
        outbox: Outbox | None = None,
    ):
        self.cfg = cfg
        self.db = db
        self.params = params
        self.io_store = io_store
        self.logs = logs
        self.outbox = outbox
        self._button_led_points_cache: list[tuple[str, dict[str, Any]]] | None = None
        self._button_led_last_plan: dict[str, bool] | None = None
        self._status_lamp_points_cache: dict[str, dict[str, Any]] | None = None
        self._status_lamp_last_plan: dict[str, bool] | None = None
        production_log_dir = getattr(getattr(logs, "_production", None), "log_dir", None)
        self.production_logs = ProductionLogManager(
            db,
            cfg=cfg,
            outbox=outbox,
            log_dir=production_log_dir or DEFAULT_PRODUCTION_LOG_DIR,
        )

    def refresh(self, *, include_snapshot: bool = True) -> dict[str, Any]:
        with _MACHINE_REFRESH_LOCK:
            return self._refresh_unlocked(include_snapshot=include_snapshot)

    def _refresh_unlocked(self, *, include_snapshot: bool = True) -> dict[str, Any]:
        refresh_entered_ts = now_ts()
        snapshot = self._state_row()
        info = dict(snapshot.get("info") or {})
        esp_critical_io_refresh = self._refresh_esp_critical_io_if_stale()
        if esp_critical_io_refresh:
            info["esp_critical_io_refresh"] = esp_critical_io_refresh
        io_map = self._io_values()
        param_map = self._param_values_by_prefix(("MAP", "MAS", "MAE", "MAW"))
        ts = now_ts()

        monitor_state = _safe_int(param_map.get("MAS0001", snapshot["current_state"]), _safe_int(snapshot["current_state"], 0))
        wickler_hard_monitor = self._monitor_wickler_hard_endstops(info, monitor_state, ts)
        if wickler_hard_monitor and not bool(wickler_hard_monitor.get("ok", True)):
            param_map = self._param_values_by_prefix(("MAP", "MAS", "MAE", "MAW"))
        if self._clear_setup_uncalibrated_wickler_latches(
            monitor_state,
            wickler_hard_monitor,
            param_map,
        ):
            param_map = self._param_values_by_prefix(("MAP", "MAS", "MAE", "MAW"))

        button_mask = parse_button_mask(param_map.get("MAP0065", "1111111"))
        warning_active = self._warning_active(param_map)
        pause_active, pause_reasons = self._pause_state(param_map)
        band_break_bypass = quick_setup_band_break_bypass_active(info)
        format_plan = build_format_plan(param_map)
        info["laser_reset_interlock"] = self._laser_reset_interlock_status(
            param_map=param_map,
            io_map=io_map,
            refresh=False,
        )
        safety_status = self._safety_status(io_map)
        pause_light_curtain_safety_drop = self._pause_light_curtain_safety_drop(
            snapshot=snapshot,
            safety_status=safety_status,
            info=info,
        )
        if pause_light_curtain_safety_drop:
            safety_status = self._mask_estop_for_pause_light_curtain(safety_status)
        critical_active, critical_reasons = self._critical_state(
            io_map,
            param_map,
            band_break_bypass=band_break_bypass,
            ignore_estop=pause_light_curtain_safety_drop,
        )
        if recent_external_purge_clear(self.db) and not _truthy(param_map.get("MAS0028", "0")) and critical_active:
            cleared_after_external_purge = self._clear_resettable_fault_latches_after_external_purge_clear(
                io_map=io_map,
                param_map=param_map,
                critical_reasons=critical_reasons,
                ts=ts,
            )
            if cleared_after_external_purge:
                param_map = self._param_values_by_prefix(("MAP", "MAS", "MAE", "MAW"))
                warning_active = self._warning_active(param_map)
                critical_active, critical_reasons = self._critical_state(
                    io_map,
                    param_map,
                    band_break_bypass=band_break_bypass,
                    ignore_estop=pause_light_curtain_safety_drop,
                )
        button_inputs = self._button_inputs(io_map)
        previous_button_inputs = info.get("button_inputs") or {}
        safety_info = dict(info.get("safety") or {})
        blocking_safety_active = self._blocking_safety_active(safety_status)
        mas0028_active = _truthy(param_map.get("MAS0028", "0"))
        safety_latched = bool(safety_info.get("latched")) or blocking_safety_active

        requested_command = _safe_int(param_map.get("MAS0002", info.get("requested_command", 0)), info.get("requested_command", 0))
        production_info = dict(info.get(PRODUCTION_RUNTIME_INFO_KEY) or {})
        reset_command_active = requested_command == 2
        reset_command_ts = self._param_updated_ts("MAS0002") if reset_command_active else 0.0
        reset_command_seen_ts = float(safety_info.get("mas0002_reset_seen_ts") or 0.0)
        reset_command_rising = reset_command_active and reset_command_ts > reset_command_seen_ts
        forced_state: int | None = None
        forced_source: str | None = None
        if self._stale_light_curtain_only_latch(
            safety_status=safety_status,
            critical_reasons=critical_reasons,
            safety_info=safety_info,
            info=info,
            mas0028_active=mas0028_active,
        ):
            self.params.apply_device_value("MAS0028", "0", promote_default=True)
            param_map["MAS0028"] = "0"
            mas0028_active = False
            safety_latched = False
            safety_info = {
                **safety_info,
                "latched": False,
                "phase": SAFETY_PHASE_READY,
                "last_reasons": [],
                "stale_light_curtain_latch_cleared_ts": ts,
            }
            info["safety"] = safety_info
            if int(snapshot["current_state"] or 0) == 21:
                forced_state = 9
                forced_source = "stale_light_curtain_latch_cleared"
        # Scenario B: a purge started by Microtom/DIClient remains active until
        # Microtom/DIClient sends MAS0028=0.  Do not auto-clear MAS0028 merely
        # because the local safety inputs are quiet; stale local latches are
        # cleared by the explicit reset path instead.
        reset_needed = (
            bool(safety_latched)
            or blocking_safety_active
            or bool(critical_active)
            or mas0028_active
            or int(snapshot["current_state"] or 0) in (20, 21)
        )
        if requested_command == 2 and not reset_needed and int(snapshot["current_state"] or 0) == 9:
            self.params.apply_device_value("MAS0002", "0", promote_default=True)
            requested_command = 0
            reset_command_active = False
            reset_command_ts = 0.0
        physical_reset_seen_ts = float(safety_info.get("physical_reset_seen_ts") or 0.0)
        safety_reset_requested = bool(reset_needed) and bool(reset_command_rising)
        light_curtain_auto_reset_result = None
        if (
            not safety_reset_requested
            and self._light_curtain_auto_reset_due(
                safety_status=safety_status,
                critical_reasons=critical_reasons,
                safety_info=safety_info,
                info=info,
                mas0028_active=mas0028_active,
                ts=ts,
            )
        ):
            safety_info = dict(safety_info)
            light_curtain_auto_reset_result = self._perform_light_curtain_auto_reset(ts)
            self._remember_light_curtain_auto_reset(
                safety_info,
                ts,
                light_curtain_auto_reset_result,
            )
        hard_stop_reasons = list(critical_reasons)
        if mas0028_active:
            hard_stop_reasons.append("MAS0028")
        if hard_stop_reasons:
            self._force_stop_process_motion_on_fault(info, hard_stop_reasons, ts)
        else:
            info.pop("last_fault_motion_stop_signature", None)
            info.pop("last_fault_motion_stop_ts", None)
            info.pop("fault_motion_stop_state", None)

        if safety_reset_requested:
            if not _SAFETY_RESET_LOCK.acquire(blocking=False):
                safety_latched = True
                forced_state = 21
                forced_source = "safety_reset_in_progress"
                safety_info = {
                    **safety_info,
                    "latched": True,
                    "phase": SAFETY_PHASE_RESETTING,
                    "last_reasons": list(safety_status.get("reasons") or safety_info.get("last_reasons") or []),
                    "last_reset": {
                        "ok": False,
                        "in_progress": True,
                        "source": "manual",
                        "started_ts": (
                            safety_info.get("last_reset", {}).get("started_ts")
                            if isinstance(safety_info.get("last_reset"), dict)
                            else None
                        ),
                    },
                    "mas0002_reset_seen": reset_command_active,
                    "mas0002_reset_seen_ts": reset_command_seen_ts if reset_command_active else 0.0,
                    "physical_reset_seen_ts": physical_reset_seen_ts,
                }
            else:
                try:
                    reset_command_consumed = bool(reset_command_active)
                    reset_result = self._perform_safety_reset(safety_status, ts)
                    self.params.apply_device_value("MAS0002", "0", promote_default=True)
                    requested_command = 0
                    reset_command_active = False
                    io_map = self._io_values()
                    param_map = self._param_values_by_prefix(("MAP", "MAS", "MAE", "MAW"))
                    critical_active, critical_reasons = self._critical_state(io_map, param_map)
                    safety_info = {
                        "latched": bool(critical_active or _truthy(param_map.get("MAS0028", "0"))),
                        "phase": SAFETY_PHASE_READY if reset_result.get("ok") else SAFETY_PHASE_FAILED,
                        "last_reasons": list(safety_status.get("reasons") or []),
                        "last_reset": reset_result,
                        "mas0002_reset_seen": reset_command_consumed,
                        "mas0002_reset_seen_ts": reset_command_ts if reset_command_consumed else 0.0,
                        "physical_reset_seen_ts": physical_reset_seen_ts,
                    }
                    if reset_result.get("ok"):
                        safety_latched = False
                        if critical_active:
                            safety_latched = True
                            safety_info["latched"] = True
                            safety_info["phase"] = SAFETY_PHASE_LATCHED
                            safety_info["post_reset_critical_reasons"] = list(critical_reasons)
                            forced_state = 21
                            forced_source = "safety_reset_critical_active"
                        else:
                            forced_state = 9
                            forced_source = "safety_reset_ready"
                    else:
                        safety_latched = bool(critical_active or _truthy(param_map.get("MAS0028", "0")))
                        forced_state = 21
                        forced_source = "safety_reset_failed"
                finally:
                    _SAFETY_RESET_LOCK.release()
        elif safety_latched:
            safety_info = {
                **safety_info,
                "latched": True,
                "phase": SAFETY_PHASE_LATCHED,
                "last_reasons": list(safety_status.get("reasons") or safety_info.get("last_reasons") or []),
                "mas0002_reset_seen": reset_command_active,
                "mas0002_reset_seen_ts": reset_command_seen_ts if reset_command_active else 0.0,
                "physical_reset_seen_ts": physical_reset_seen_ts,
            }
            forced_state = 21
            forced_source = "safety_latched"
        else:
            if requested_command:
                clear_motor_setup_manual_lock(self.db, reason=f"machine_command:{requested_command}")
            if safety_info.get("phase") not in (SAFETY_PHASE_READY,):
                auto_reset_fields = {
                    key: value
                    for key, value in safety_info.items()
                    if key.startswith("light_curtain_auto_reset") or key == "last_auto_reset"
                }
                safety_info = {"latched": False, "phase": "idle", "last_reasons": [], **auto_reset_fields}
            safety_info["mas0002_reset_seen"] = reset_command_active
            safety_info["mas0002_reset_seen_ts"] = reset_command_ts if reset_command_active else 0.0
            safety_info["physical_reset_seen_ts"] = physical_reset_seen_ts
            if light_curtain_auto_reset_result is not None:
                safety_info["last_auto_reset"] = {
                    **dict(safety_info.get("last_auto_reset") or {}),
                    "state_changed": False,
                    "purge_changed": False,
                }

        if forced_state is None and not safety_latched and requested_command not in (0, 2):
            command_action = _command_action_name(requested_command, int(snapshot["current_state"] or 0))
            if command_action:
                if self._motion_action_blocked_by_live_light_curtain(
                    command_action,
                    {**info, "safety_status": safety_status},
                ):
                    self.logs.log(
                        "machine",
                        "warning",
                        f"MAS0002={requested_command} ignored while light curtain is active ({command_action})",
                    )
                    self._record_event(
                        "machine_command_blocked",
                        "warning",
                        f"Maschinenkommando {command_action} wegen aktivem Lichtgitter blockiert",
                        {
                            "command": int(requested_command),
                            "action": command_action,
                            "from_state": int(snapshot["current_state"] or 0),
                            "reason": "light_curtain_active",
                        },
                    )
                    self.params.apply_device_value("MAS0002", "0", promote_default=True)
                    requested_command = 0
                    command_action = None
            if command_action:
                allowed_actions = state_actions(snapshot["current_state"])
                if not allowed_actions.get(command_action, False):
                    self.logs.log(
                        "machine",
                        "warning",
                        f"MAS0002={requested_command} ignored in state {snapshot['current_state']} ({command_action} not allowed)",
                    )
                    self.params.apply_device_value("MAS0002", "0", promote_default=True)
                    requested_command = 0
                elif not button_mask.get(command_action, False):
                    self.logs.log(
                        "machine",
                        "info",
                        f"MAS0002={requested_command} ignored by MAP0065 ({command_action})",
                    )
                    self.params.apply_device_value("MAS0002", "0", promote_default=True)
                    requested_command = 0
                elif requested_command == 1:
                    if not production_start_motion_enabled():
                        reason = PRODUCTION_START_BLOCK_REASON
                        self.logs.log("machine", "warning", f"Start blockiert: {reason}")
                        self._record_event(
                            "production_start_blocked",
                            "warning",
                            f"Produktionsstart blockiert: {reason}",
                            {
                                "reason": reason,
                                "required_runtime": list(PRODUCTION_START_REQUIRED_RUNTIME),
                            },
                        )
                        self.params.apply_device_value("MAS0002", "0", promote_default=True)
                        production_info["last_start"] = {
                            "ok": False,
                            "blocked": True,
                            "reason": reason,
                            "ts": now_ts(),
                        }
                        production_info.pop("pending_start", None)
                        requested_command = 0
                    else:
                        quick_setup_log_bypass = quick_setup_log_bypass_active(info)
                        if quick_setup_log_bypass:
                            allowed, reason = True, "OK_QUICK_SETUP_LOG_BYPASS"
                        else:
                            allowed, reason = self.production_logs.can_start_new_production()
                        if not allowed:
                            self.logs.log("machine", "warning", f"Start blockiert: {reason}")
                            self._record_event(
                                "production_start_blocked",
                                "warning",
                                f"Produktionsstart blockiert: {reason}",
                                {"reason": reason},
                            )
                            self.params.apply_device_value("MAS0002", "0", promote_default=True)
                            production_info["last_start"] = {
                                "ok": False,
                                "blocked": True,
                                "reason": reason,
                                "ts": now_ts(),
                            }
                            production_info.pop("pending_start", None)
                            requested_command = 0
                        else:
                            event = None
                            if quick_setup_log_bypass:
                                production_info["quick_setup_log_bypass"] = True
                            else:
                                production_info.pop("quick_setup_log_bypass", None)
                                event = self.production_logs.handle_param_change("MAS0002", "1")
                            if event and event.get("event") == "start":
                                self.logs.log("raspi", "info", f"production logging started: {event.get('production_label')}")
                            cleared_pause_errors = self._clear_pause_errors_for_production_start()
                            if cleared_pause_errors:
                                for pkey in cleared_pause_errors:
                                    param_map[pkey] = "0"
                                pause_active, pause_reasons = self._pause_state(param_map)
                            production_info["pending_start"] = {
                                "request_ts": now_ts(),
                                "command_ts": self._param_updated_ts("MAS0002"),
                                "from_state": int(snapshot["current_state"] or 0),
                                "cleared_pause_errors": cleared_pause_errors,
                            }
                            self._record_event(
                                "production_start_accepted",
                                "info",
                                "Produktionsstart akzeptiert: Wechsel nach Produktionsbetrieb wird vorbereitet",
                                dict(production_info["pending_start"]),
                            )
                            production_info.pop("last_stop", None)
                        # MAS0002 is a command byte. Once Start is accepted
                        # from Pause, consume it immediately so the next
                        # refresh in transition state 4 does not re-interpret
                        # the same stale value as a new, invalid Start request.
                        self.params.apply_device_value("MAS0002", "0", promote_default=True)
            if requested_command in (2, 7):
                production_info.pop("pending_start", None)

        setup_info = dict(info.get("setup") or {})
        setup_command_active = requested_command == 3
        requested_state_override: int | None = None
        setup_command_ts = self._param_updated_ts("MAS0002") if setup_command_active else 0.0
        setup_seen_ts = float(setup_info.get("mas0002_setup_seen_ts") or 0.0)
        # On software start/deploy while MAS0002 is already 3, do not start a
        # motion workflow implicitly. The next fresh Einrichten command still
        # has a newer param timestamp and will run the calibration sequence.
        stale_setup_command = (
            setup_command_active
            and setup_seen_ts == 0.0
            and setup_command_ts > 0.0
            and (refresh_entered_ts - setup_command_ts) > 5.0
        )
        if stale_setup_command:
            setup_seen_ts = setup_command_ts
            setup_info["mas0002_setup_seen_ts"] = setup_seen_ts
            setup_info["stale_setup_command_dropped_ts"] = now_ts()
            self.params.apply_device_value("MAS0002", "0", promote_default=True)
            requested_command = 0
            setup_command_active = False
            self.logs.log("machine", "warning", "Alter Einrichten-Befehl nach Neustart verworfen")
        setup_rising = setup_command_active and setup_command_ts > setup_seen_ts
        if setup_rising:
            setup_info["mas0002_setup_seen_ts"] = setup_command_ts
            setup_info["last_request_ts"] = now_ts()
            if forced_state is not None or safety_latched or critical_active or _truthy(param_map.get("MAS0028", "0")):
                setup_info["last_result"] = {
                    "ok": False,
                    "skipped": True,
                    "reason": "safety_or_purge_active",
                    "ts": now_ts(),
                }
                self.logs.log("machine", "warning", "Einrichten-Wicklerworkflow wegen Safety/Purge nicht gestartet")
            else:
                # Consume the command before the long-running setup workflow.
                # Otherwise a second refresh/UI poll can see the same MAS0002=3
                # while the first setup run is still moving axes and start a
                # competing setup attempt.  MAS0001=2 keeps the orchestrator's
                # setup-active guard true while MAS0002 is already idle.
                setup_info.pop("completed_ts", None)
                setup_info.pop("failed_ts", None)
                setup_info.pop("pause_pending", None)
                setup_info.pop("pause_pending_ts", None)
                setup_info.pop("pause_completed_ts", None)
                setup_info["last_result"] = {
                    "running": True,
                    "response": "SETUP_WICKLER=RUNNING",
                    "started_ts": now_ts(),
                }
                self.params.apply_device_value("MAS0001", "2", promote_default=True)
                self._notify_microtom("MAS0001", "2", dedupe_key="machine:MAS0001")
                self.params.apply_device_value("MAS0002", "0", promote_default=True)
                info["setup"] = setup_info
                self._write_state(
                    current_state=2,
                    requested_state=3,
                    state_source="setup_started",
                    warning_active=warning_active,
                    purge_active=False,
                    production_label=self._current_production_label(),
                    last_label_no=snapshot["last_label_no"],
                    info=info,
                )
                setup_info["last_result"] = self._perform_setup_wickler_calibration()
                if bool((setup_info.get("last_result") or {}).get("ok")):
                    format_ready, missing_format = self._format_ready_for_pause(param_map)
                    setup_info["parameters_ready"] = format_ready
                    setup_info["missing_parameters"] = missing_format
                    if format_ready:
                        # This is an internal setup-complete transition, not a
                        # Microtom/user Pause command. Keep MAS0002 idle so the
                        # next refresh does not reject a stale MAS0002=7 while
                        # the state machine is still in transition state 2.
                        self.params.apply_device_value("MAS0002", "0", promote_default=True)
                        requested_command = 0
                        requested_state_override = 7
                        setup_info["completed_ts"] = now_ts()
                        setup_info["pause_pending"] = True
                        setup_info["pause_pending_ts"] = setup_info["completed_ts"]
                        self._record_event(
                            "setup_complete",
                            "info",
                            "Einrichten abgeschlossen: Formatparameter gueltig, Wechsel zu Pause freigegeben",
                            {"target_state": 7},
                        )
                    else:
                        self.logs.log(
                            "machine",
                            "warning",
                            "Einrichten abgeschlossen, aber Formatparameter noch nicht vollstaendig: "
                            + ",".join(missing_format),
                        )
                else:
                    result = setup_info.get("last_result") or {}
                    if not bool(result.get("skipped")):
                        # A failed measuring/setup workflow must not leave the
                        # machine trapped in transition state 2. Fall back to
                        # Produktions-Stop so the operator can retry after the
                        # reported cause is fixed.
                        self.params.apply_device_value("MAS0002", "2", promote_default=True)
                        requested_command = 2
                        setup_info["failed_ts"] = now_ts()
                        self._record_event(
                            "setup_failed_return_stop",
                            "warning",
                            "Einrichten fehlgeschlagen: Rueckkehr zu Produktions-Stop freigegeben",
                            {"target_state": 9, "result": result},
                        )
        info["setup"] = setup_info

        setup_complete_ts = max(
            _safe_float(setup_info.get("completed_ts"), 0.0),
            self._latest_machine_event_ts("setup_complete"),
        )
        setup_seen_ts = _safe_float(setup_info.get("mas0002_setup_seen_ts"), 0.0)
        setup_pause_recovery_due = (
            requested_state_override is None
            and requested_command == 0
            and int(snapshot["current_state"] or 0) in (2, 3, 6)
            and setup_complete_ts > 0.0
            and setup_complete_ts >= setup_seen_ts
            and (ts - setup_complete_ts) <= _SETUP_PAUSE_RECOVERY_WINDOW_S
            and forced_state is None
            and not safety_latched
            and not critical_active
            and not _truthy(param_map.get("MAS0028", "0"))
        )
        if setup_pause_recovery_due:
            requested_state_override = 7
            setup_info["pause_pending"] = True
            setup_info["pause_pending_ts"] = setup_complete_ts
            setup_info["pause_recovery_ts"] = ts
            info["setup"] = setup_info

        requested_state = (
            requested_state_override
            if requested_state_override is not None
            else command_to_target_state(requested_command, snapshot["current_state"])
        )
        if requested_command == 0 and requested_state_override is None:
            try:
                transition_state = int(snapshot["current_state"] or 0)
                previous_requested_state = int(snapshot.get("requested_state") or 0)
            except Exception:
                transition_state = 0
                previous_requested_state = 0
            expected_final_state = TRANSITION_FINALS.get(transition_state)
            if expected_final_state is not None and previous_requested_state == expected_final_state:
                requested_state = expected_final_state
        pending_start = production_info.get("pending_start") if isinstance(production_info.get("pending_start"), dict) else {}
        start_transition_pending = bool(pending_start) and int(snapshot["current_state"] or 0) == 4
        pause_reasons_are_label_only = bool(pause_reasons) and set(str(reason) for reason in pause_reasons).issubset(
            PAUSE_ERROR_KEYS
        )
        pause_forces_state = (
            pause_active
            and int(snapshot["current_state"] or 0) in (4, 5, 6)
            and not (start_transition_pending and pause_reasons_are_label_only)
        )
        if pause_forces_state:
            pause_signature = ",".join(str(reason) for reason in pause_reasons)
            if (
                int(snapshot["current_state"] or 0) in (4, 5)
                and pause_signature
                and str(production_info.get("last_pause_reason_signature") or "") != pause_signature
            ):
                reason_text = self._describe_runtime_reasons(pause_reasons)
                self.logs.log("machine", "warning", f"Produktionspause angefordert: {reason_text}")
                self._record_event(
                    "production_pause_requested",
                    "warning",
                    f"Produktionspause angefordert: {reason_text}",
                    {
                        "pause_reasons": list(pause_reasons),
                        "reason_text": reason_text,
                        "from_state": int(snapshot["current_state"] or 0),
                        "requested_state_before_pause": requested_state,
                    },
                )
                production_info["last_pause_reason_signature"] = pause_signature
                production_info["last_pause_reason_ts"] = ts
            requested_state = 7
        elif not pause_active:
            production_info.pop("last_pause_reason_signature", None)
            production_info.pop("last_pause_reason_ts", None)

        purge_active = critical_active or _truthy(param_map.get("MAS0028", "0"))
        if purge_active != _truthy(param_map.get("MAS0028", "0")):
            self.params.apply_device_value("MAS0028", "1" if purge_active else "0", promote_default=True)
            self._notify_microtom("MAS0028", "1" if purge_active else "0", dedupe_key="machine:MAS0028")

        if forced_state is not None:
            new_state = forced_state
            state_source = str(forced_source or "safety")
            requested_state = forced_state
            purge_active = bool(critical_active or _truthy(param_map.get("MAS0028", "0")))
        else:
            new_state, state_source = settle_machine_state(
                requested_state,
                snapshot["current_state"],
                estop_ok=not bool(safety_status["estop_active"]),
                light_curtain_ok=not bool(safety_status["light_curtain_active"]),
                ups_ok=bool(safety_status.get("ups_ok", True)),
                purge_active=purge_active,
            )
            if state_source == "light_curtain_pause":
                requested_state = 7

        previous_state = int(snapshot["current_state"] or 0)
        production_state_synced_to_esp = False
        pending_start_ts = _safe_float((pending_start or {}).get("request_ts"), 0.0)
        pending_start_age_s = max(0.0, float(ts) - pending_start_ts) if pending_start_ts > 0.0 else 0.0
        if pending_start and (pending_start_age_s > 120.0 or int(new_state or 0) not in (4, 5)):
            if pending_start_age_s <= 120.0:
                self._record_event(
                    "production_start_aborted_before_runner",
                    "warning",
                    (
                        "Produktionsstart vor Runner-Start abgebrochen: "
                        f"Status {previous_state} -> {int(new_state or 0)}, "
                        f"Pausegruende {self._describe_runtime_reasons(pause_reasons) if pause_reasons else '-'}"
                    ),
                    {
                        "pending_start": dict(pending_start),
                        "from_state": previous_state,
                        "to_state": int(new_state or 0),
                        "state_source": state_source,
                        "pause_reasons": list(pause_reasons),
                        "critical_reasons": list(critical_reasons),
                        "safety_status": dict(safety_status),
                    },
                )
            production_info.pop("pending_start", None)
            pending_start = {}

        light_curtain_motion_pause_due = (
            state_source == "light_curtain_pause"
            and previous_state in (5, 10, 11)
            and int(new_state or 0) == 7
        )
        production_stop_due = (
            (previous_state == 5 and int(new_state or 0) != 5)
            or (bool(production_info.get("active")) and int(new_state or 0) not in (4, 5))
            or light_curtain_motion_pause_due
        )
        if production_stop_due:
            stop_reason = f"state_{previous_state}_to_{int(new_state or 0)}"
            controlled_pause_stop = False
            if light_curtain_motion_pause_due:
                stop_reason = "light_curtain_pause"
                controlled_pause_stop = True
            elif pause_active and int(new_state or 0) in (6, 7):
                stop_reason = "pause:" + (",".join(pause_reasons) or "unknown")
                controlled_pause_stop = True
            elif (
                requested_command == 7
                and previous_state == 5
                and int(requested_state or 0) == 7
                and int(new_state or 0) in (6, 7)
            ):
                stop_reason = "operator_pause"
                controlled_pause_stop = True
            stop_result = self._stop_production_motion(
                reason=stop_reason,
                target_state=int(new_state or 0),
            )
            production_info["last_stop"] = stop_result
            production_info["active"] = False
            production_info.pop("pending_start", None)
            if controlled_pause_stop and bool(stop_result.get("ok")):
                production_info["paused"] = True
                production_info["paused_ts"] = now_ts()
                production_info["pause_reason"] = stop_reason
                production_info["resume_from_state"] = int(new_state or 0)
                if requested_command == 7:
                    self.params.apply_device_value("MAS0002", "0", promote_default=True)
                    requested_command = 0
            else:
                event = self.production_logs.handle_param_change("MAS0002", "2")
                if event and event.get("event") == "stop":
                    self.logs.log("raspi", "info", f"production logging ready: {event.get('production_label')}")
            if not bool(stop_result.get("ok")):
                self.params.apply_device_value("MAS0028", "1", promote_default=True)
                self._notify_microtom("MAS0028", "1", dedupe_key="machine:MAS0028")
                new_state = 21
                requested_state = 21
                state_source = "production_stop_failed"
                purge_active = True
                self._record_event(
                    "production_stop_failed",
                    "error",
                    "Produktionsstop nicht vollstaendig bestaetigt: Motion- oder Wickler-Stopverifikation fehlgeschlagen",
                    stop_result,
                )

        production_start_due = int(new_state or 0) == 5 and bool(pending_start)
        if production_start_due:
            start_result = self._start_production_motion(param_map, format_plan)
            production_info["last_start"] = start_result
            production_info.pop("pending_start", None)
            production_state_synced_to_esp = bool(start_result.get("synced_state") == 5)
            if bool(start_result.get("ok")):
                production_info["active"] = True
                production_info["active_since_ts"] = now_ts()
                production_info["plan"] = dict(start_result.get("plan") or {})
                production_info.pop("last_wickler_observed_travel", None)
                production_info.pop("paused", None)
                production_info.pop("paused_ts", None)
                production_info.pop("pause_reason", None)
                production_info.pop("resume_from_state", None)
            else:
                self.logs.log("machine", "error", f"Produktionsstart fehlgeschlagen: {start_result.get('error')}")
                self._record_event(
                    "production_start_failed",
                    "error",
                    f"Produktionsstart fehlgeschlagen: {start_result.get('error')}",
                    start_result,
                )
                fail_stop = self._stop_production_motion(reason="production_start_failed", target_state=7)
                production_info["last_stop"] = fail_stop
                production_info["active"] = False
                event = self.production_logs.handle_param_change("MAS0002", "2")
                if event and event.get("event") == "stop":
                    self.logs.log("raspi", "info", f"production logging ready: {event.get('production_label')}")
                new_state = 7
                requested_state = 7
                state_source = "production_start_failed"
                purge_active = bool(critical_active or _truthy(param_map.get("MAS0028", "0")))

        if bool(production_info.get("active")) and int(new_state or 0) == 5:
            esp_monitor = self._monitor_active_production_esp(production_info, ts)
            if esp_monitor is not None:
                production_info["active"] = False
                production_info["last_stop"] = dict(esp_monitor.get("stop") or {})
                production_info.pop("pending_start", None)
                new_state = 21
                requested_state = 21
                state_source = "production_esp_runner_fault"
                purge_active = True
            else:
                wickler_monitor = self._monitor_active_production_wicklers(production_info, ts)
                if wickler_monitor is not None:
                    production_info["active"] = False
                    production_info["last_stop"] = dict(wickler_monitor.get("stop") or {})
                    production_info.pop("pending_start", None)
                    new_state = 21
                    requested_state = 21
                    state_source = "production_wickler_fault"
                    purge_active = True

        esp_machine_state_sync = dict(info.get("esp_machine_state_sync") or {})
        esp_sync_failed_state = _safe_int(esp_machine_state_sync.get("failed_state"), -1)
        esp_sync_failed_ts = _safe_float(esp_machine_state_sync.get("failed_ts"), 0.0)
        esp_sync_retry_due = (
            esp_sync_failed_state != int(new_state or 0)
            or esp_sync_failed_ts <= 0.0
            or (float(ts) - esp_sync_failed_ts) >= 10.0
        )
        esp_state_sync_due = (
            int(new_state or 0) not in (4, 5)
            and _safe_int(esp_machine_state_sync.get("state"), -1) != int(new_state or 0)
            and esp_sync_retry_due
        )
        state_changed = new_state != snapshot["current_state"]
        mas0001_value_changed = str(new_state) != str(param_map.get("MAS0001", ""))
        if light_curtain_auto_reset_result is not None:
            last_auto_reset = dict(safety_info.get("last_auto_reset") or {})
            if last_auto_reset:
                last_auto_reset["state_changed"] = bool(state_source == "light_curtain_pause" and state_changed)
                last_auto_reset["purge_changed"] = False
                safety_info["last_auto_reset"] = last_auto_reset
        if self._light_curtain_wickler_recovery_due(
            current_state=int(new_state or 0),
            requested_state=int(requested_state or 0),
            safety_status=safety_status,
            critical_reasons=critical_reasons,
            safety_info=safety_info,
            info=info,
            mas0028_active=bool(purge_active or _truthy(param_map.get("MAS0028", "0"))),
            ts=ts,
        ):
            last_auto_reset = dict(safety_info.get("last_auto_reset") or {})
            auto_reset_ts = _safe_float(last_auto_reset.get("ts"), 0.0)
            recovery_start = self._start_light_curtain_wickler_recovery_background(auto_reset_ts)
            safety_info = dict(safety_info)
            safety_info["light_curtain_wickler_recovery_running"] = bool(
                recovery_start.get("queued") or recovery_start.get("in_progress")
            )
            safety_info["light_curtain_wickler_recovery_last_attempt_ts"] = ts
            safety_info["last_light_curtain_wickler_recovery_start"] = recovery_start
        if state_changed or mas0001_value_changed:
            self.params.apply_device_value("MAS0001", str(new_state), promote_default=True)
            self._notify_microtom("MAS0001", str(new_state), dedupe_key="machine:MAS0001")
        if (state_changed or mas0001_value_changed or esp_state_sync_due) and not production_state_synced_to_esp:
            if self._sync_esp_machine_state(int(new_state)):
                esp_machine_state_sync = {"state": int(new_state), "ts": now_ts()}
            elif esp_state_sync_due:
                esp_machine_state_sync = {
                    **esp_machine_state_sync,
                    "failed_state": int(new_state or 0),
                    "failed_ts": now_ts(),
                }
        elif production_state_synced_to_esp:
            esp_machine_state_sync = {"state": int(new_state), "ts": now_ts()}
        if state_changed:
            self._record_event(
                "state_change",
                "info",
                f"Maschinenstatus gewechselt: {snapshot['current_state']} -> {new_state} ({state_label(new_state)})",
                {
                    "from_state": snapshot["current_state"],
                    "to_state": new_state,
                    "source": state_source,
                    "purge_active": bool(purge_active),
                    "critical_reasons": list(critical_reasons),
                    "pause_reasons": list(pause_reasons),
                    "requested_command": int(requested_command or 0),
                    "requested_state": int(requested_state or 0),
                    "mas0028": str(param_map.get("MAS0028", "")),
                    "safety_status": dict(safety_status),
                },
            )
            self.logs.log("machine", "info", f"state {snapshot['current_state']} -> {new_state} ({state_source})")
            if int(new_state or 0) != 5:
                tto_state_sync = self._queue_tto_printer_state_sync(
                    int(new_state or 0),
                    param_map,
                    reason=f"state_change:{state_source}",
                )
                if tto_state_sync.get("queued") or not tto_state_sync.get("skipped"):
                    info["last_tto_printer_state_sync"] = tto_state_sync

        pending_hmi_command = info.get("pending_hmi_command")
        if isinstance(pending_hmi_command, dict):
            pending_target = _safe_int(pending_hmi_command.get("target_state"), 0)
            pending_age_s = float(ts) - _safe_float(pending_hmi_command.get("queued_ts"), float(ts))
            pending_transition = next(
                (transition for transition, final_state in TRANSITION_FINALS.items() if int(final_state) == pending_target),
                None,
            )
            if (
                int(new_state or 0) == pending_target
                or (pending_transition is not None and int(new_state or 0) != int(pending_transition))
                or pending_age_s > 20.0
            ):
                info.pop("pending_hmi_command", None)

        if int(new_state or 0) == 7 and bool(setup_info.get("pause_pending")):
            setup_info["pause_pending"] = False
            setup_info["pause_completed_ts"] = ts
            info["setup"] = setup_info

        actions = state_actions(new_state)
        self._apply_stop_mode_axis_targets(new_state, info, state_changed=state_changed, ts=ts)
        button_leds = button_led_plan(new_state, button_mask, ts=ts)
        if self._safety_led_override_active(new_state, safety_info):
            button_leds.update(self._safety_button_led_plan(str(safety_info.get("phase") or ""), ts, button_mask))
        # Physical button LEDs are driven by the dedicated button LED tick in
        # service.py.  The machine refresh only publishes the calculated plan
        # for the HMI/snapshot; writing here as well makes the blink cadence
        # race against the tick when refresh() work takes longer than usual.
        self._apply_status_lamp(new_state, warning_active=warning_active, ts=ts)

        machine_label = self._current_production_label()
        info[PRODUCTION_RUNTIME_INFO_KEY] = production_info
        info["esp_machine_state_sync"] = esp_machine_state_sync
        info.update(
            {
                "requested_command": requested_command,
                "button_mask": button_mask,
                "allowed_actions": actions,
                "button_inputs": self._button_inputs(io_map),
                "critical_reasons": critical_reasons,
                "safety": safety_info,
                "safety_status": safety_status,
                "pause_reasons": pause_reasons,
                "warning_keys": sorted(pkey for pkey, value in param_map.items() if pkey.startswith("MAW") and _truthy(value)),
                "error_keys": sorted(pkey for pkey, value in param_map.items() if pkey.startswith("MAE") and _truthy(value)),
                "status_lamp": lamp_outputs_for_state(new_state, warning_active=warning_active, ts=ts),
                "button_leds": button_leds,
                "format_plan": format_plan,
            }
        )
        self._write_state(
            current_state=new_state,
            requested_state=requested_state,
            state_source=state_source,
            warning_active=warning_active,
            purge_active=purge_active,
            production_label=machine_label,
            last_label_no=snapshot["last_label_no"],
            info=info,
        )

        if include_snapshot:
            return self.snapshot()
        return {
            "ok": True,
            "current_state": int(new_state or 0),
            "requested_state": int(requested_state or 0),
            "state_source": state_source,
        }

    def refresh_button_led_outputs(self, *, ts: float | None = None) -> dict[str, Any]:
        ts = now_ts() if ts is None else float(ts)
        snapshot = self._state_row()
        state = int(snapshot.get("current_state") or 1)
        info = dict(snapshot.get("info") or {})
        button_mask = parse_button_mask(self.params.get_effective_value("MAP0065"))
        safety_info = dict(info.get("safety") or {})

        button_leds = button_led_plan(state, button_mask, ts=ts)
        if self._safety_led_override_active(state, safety_info):
            button_leds.update(self._safety_button_led_plan(str(safety_info.get("phase") or ""), ts, button_mask))

        self._apply_button_led_plan(button_leds, force=True, source="button-led-tick")

        if dict(info.get("button_leds") or {}) != button_leds:
            info["button_leds"] = button_leds
            self._write_state(
                current_state=state,
                requested_state=int(snapshot.get("requested_state") or state),
                state_source=str(snapshot.get("state_source") or "runtime"),
                warning_active=bool(snapshot.get("warning_active")),
                purge_active=bool(snapshot.get("purge_active")),
                production_label=str(snapshot.get("production_label") or ""),
                last_label_no=int(snapshot.get("last_label_no") or 0),
                info=info,
            )

        return {
            "ok": True,
            "state": state,
            "button_mask": button_mask,
            "button_leds": button_leds,
            "safety_override": self._safety_led_override_active(state, safety_info),
        }

    def refresh_physical_button_inputs(
        self,
        *,
        previous_inputs: dict[str, Any] | None = None,
        ts: float | None = None,
        refresh_hardware: bool = True,
    ) -> dict[str, Any]:
        ts = now_ts() if ts is None else float(ts)
        button_pin_labels = {pin_label for device_code, pin_label in BUTTON_INPUTS.values() if device_code == "raspi_plc21"}
        if refresh_hardware:
            self._refresh_single_io_device("raspi_plc21", pin_labels=button_pin_labels)
        io_map = self._io_values_for_pins(BUTTON_INPUTS.values())
        current_inputs = self._button_inputs(io_map)
        return self.process_physical_button_inputs(
            current_inputs=current_inputs,
            previous_inputs=previous_inputs,
            ts=ts,
        )

    def process_physical_button_inputs(
        self,
        *,
        current_inputs: dict[str, Any],
        previous_inputs: dict[str, Any] | None = None,
        ts: float | None = None,
    ) -> dict[str, Any]:
        ts = now_ts() if ts is None else float(ts)
        snapshot = self._state_row()
        info = dict(snapshot.get("info") or {})
        normalized_current = {name: bool(current_inputs.get(name)) for name in BUTTON_INPUTS}
        io_map = {
            (device_code, pin_label.upper()): ("1" if normalized_current.get(name) else "0")
            for name, (device_code, pin_label) in BUTTON_INPUTS.items()
        }
        current_inputs = self._button_inputs(io_map)
        previous = dict(previous_inputs if previous_inputs is not None else (info.get("button_inputs") or {}))
        button_mask = parse_button_mask(self.params.get_effective_value("MAP0065"))

        request: dict[str, Any] | None = self._physical_button_request(
            snapshot=snapshot,
            info=info,
            io_map=io_map,
            previous_inputs=previous,
            button_mask=button_mask,
        )
        accepted = False
        error = ""
        if request is not None:
            ok, msg = self.params.set_value("MAS0002", str(request["command"]), actor="physical-panel")
            if ok:
                accepted = True
                self.logs.log(
                    "machine",
                    "info",
                    f"physical button {request['button']} -> MAS0002={request['command']}",
                )
                self._record_event(
                    "physical_button",
                    "info",
                    f"Physische Taste {request['button']} ausgeloest -> {state_label(request['target_state'])}",
                    {
                        **request,
                        "actor": "physical-panel",
                    },
                )
                info["last_physical_button_request"] = {**request, "ts": ts}
                if int(request.get("command") or 0) == 2:
                    safety_info = dict(info.get("safety") or {})
                    safety_info["physical_reset_seen_ts"] = ts
                    info["safety"] = safety_info
            else:
                error = str(msg)
                self.logs.log(
                    "machine",
                    "warning",
                    f"physical button {request['button']} rejected: {msg}",
                )

        if current_inputs != dict(info.get("button_inputs") or {}) or request is not None:
            info["button_inputs"] = current_inputs
            self._write_state(
                current_state=int(snapshot.get("current_state") or 1),
                requested_state=int(snapshot.get("requested_state") or snapshot.get("current_state") or 1),
                state_source=str(snapshot.get("state_source") or "runtime"),
                warning_active=bool(snapshot.get("warning_active")),
                purge_active=bool(snapshot.get("purge_active")),
                production_label=str(snapshot.get("production_label") or ""),
                last_label_no=int(snapshot.get("last_label_no") or 0),
                info=info,
            )

        return {
            "ok": True,
            "current_inputs": current_inputs,
            "previous_inputs": previous,
            "request": request,
            "accepted": accepted,
            "error": error,
        }

    def snapshot(self) -> dict[str, Any]:
        row = self._state_row()
        recent_events = self._recent_events(limit=30)
        recent_labels = self._recent_labels(limit=20)
        return {
            "ok": True,
            "current_state": row["current_state"],
            "current_state_label": state_label(row["current_state"]),
            "requested_state": row["requested_state"],
            "requested_state_label": state_label(row["requested_state"]),
            "warning_active": bool(row["warning_active"]),
            "purge_active": bool(row["purge_active"]),
            "production_label": row["production_label"],
            "last_label_no": row["last_label_no"],
            "info": row["info"],
            "events": recent_events,
            "labels": recent_labels,
        }

    def handle_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        event_type = str((payload or {}).get("type") or "").strip().lower()
        stale_production_event = self._stale_production_event_result(event_type, payload)
        if stale_production_event is not None:
            return stale_production_event
        if event_type == "label_complete":
            result = self._handle_label_complete(payload)
            return {"ok": True, "accepted": True, "event": event_type, "result": result}
        if event_type == "label_length_fault":
            result = self._handle_label_length_fault(payload)
            return {"ok": True, "accepted": True, "event": event_type, "result": result}
        if event_type == "production_fault":
            result = self._handle_production_fault(payload)
            return {"ok": True, "accepted": True, "event": event_type, "result": result}
        if event_type in ("production_registration_late", "production_registration_fault"):
            result = self._handle_registration_diagnostic_event(event_type, payload)
            return {"ok": True, "accepted": True, "event": event_type, "result": result}
        if event_type == "production_registration_correction":
            label_no = _safe_int(payload.get("label_no"), 0)
            attempt = _safe_int(payload.get("attempt"), 0)
            error_mm = _safe_float(payload.get("error_mm"), 0.0)
            command_mm = _safe_float(payload.get("command_mm"), 0.0)
            bias_delta_mm = _safe_float(payload.get("bias_delta_mm"), 0.0)
            print_bias_mm = _safe_float(payload.get("print_bias_mm"), 0.0)
            event_result = self._record_production_event_once(
                event_type,
                "info",
                f"ID3-Registrierkorrektur: Label {label_no}, Versuch {attempt}, "
                f"Fehler {error_mm:.4f}mm, Befehl {command_mm:.4f}mm, "
                f"Bias-Delta {bias_delta_mm:.4f}mm, Bias {print_bias_mm:.4f}mm",
                dict(payload or {}),
            )
            return {"ok": True, "accepted": True, "event": event_type, **event_result}
        if event_type == "production_registration_correction_effect":
            label_no = _safe_int(payload.get("label_no"), 0)
            command_seq = _safe_int(payload.get("command_seq"), 0)
            accepted = bool(payload.get("accepted"))
            expected_encoder = _safe_int(payload.get("expected_encoder_counts"), 0)
            actual_encoder = _safe_int(payload.get("actual_encoder_delta_counts"), 0)
            expected_motor = _safe_int(payload.get("expected_motor_steps"), 0)
            actual_motor = _safe_int(payload.get("actual_motor_feedback_delta_steps"), 0)
            reason = str(payload.get("reason") or ("accepted" if accepted else "rejected"))
            severity = "info" if accepted else "warning"
            event_result = self._record_production_event_once(
                event_type,
                severity,
                "ID3-Korrekturwirkung: "
                f"Label {label_no}, Seq {command_seq}, {reason}, "
                f"Encoder {actual_encoder}/{expected_encoder} counts, "
                f"Motor {actual_motor}/{expected_motor} steps",
                dict(payload or {}),
                dedupe_window_s=30.0,
            )
            return {"ok": True, "accepted": True, "event": event_type, **event_result}
        if event_type == "production_velocity_stop_for_print":
            label_no = _safe_int(payload.get("label_no"), 0)
            remaining = _safe_float(payload.get("remaining_mm"), 0.0)
            speed = _safe_float(payload.get("infeed_speed_mm_s"), 0.0)
            event_result = self._record_production_event_once(
                event_type,
                "info",
                f"Velocity-Handover vor Druckposition: Label {label_no}, Restweg {remaining:.3f}mm, "
                f"Einlaufgeschwindigkeit {speed:.3f}mm/s",
                dict(payload or {}),
            )
            return {
                "ok": True,
                "accepted": True,
                "event": event_type,
                "wickler_prepare": {
                    "ok": True,
                    "skipped": "wait_until_print_position_reached",
                    "label_no": label_no,
                },
                **event_result,
            }
        if event_type == "production_first_print_position_commanded":
            target = _safe_float(payload.get("target_abs_mm"), 0.0)
            lead = _safe_float(payload.get("first_label_lead_mm"), 0.0)
            event_result = self._record_production_event_once(
                event_type,
                "info",
                f"Erste Druckposition direkt befohlen: Ziel {target:.3f}mm, erstes Label +{lead:.3f}mm",
                dict(payload or {}),
            )
            return {"ok": True, "accepted": True, "event": event_type, **event_result}
        if event_type == "production_first_print_position_reached":
            label_no = _safe_int(payload.get("label_no"), 0)
            error_mm = _safe_float(payload.get("target_error_mm"), 0.0)
            ready_key = self._first_print_wickler_ready_key(label_no=label_no, payload=payload)
            if self._first_print_wickler_ready_already_sent(ready_key):
                event_result = self._record_production_event_once(
                    event_type,
                    "info",
                    f"Erste Druckposition bereits behandelt: Label {label_no}, Restfehler {error_mm:.3f}mm",
                    dict(payload or {}),
                    dedupe_window_s=300.0,
                )
                return {
                    "ok": True,
                    "accepted": True,
                    "event": event_type,
                    "wickler_takt": {
                        "ok": True,
                        "skipped": "duplicate_first_print_position_reached",
                        "label_no": label_no,
                    },
                    "esp_ready": {
                        "ok": True,
                        "skipped": "already_handled",
                        "label_no": label_no,
                    },
                    **event_result,
                }
            event_result = self._record_production_event_once(
                event_type,
                "info",
                f"Erste Druckposition erreicht: Label {label_no}, Restfehler {error_mm:.3f}mm; Wickler werden auf Takt vorbereitet",
                dict(payload or {}),
                dedupe_window_s=30.0,
            )
            self._remember_first_print_wickler_ready_attempt(
                ready_key,
                label_no=label_no,
                payload=payload,
                status="started",
            )
            wickler_takt = self._prepare_next_production_wickler_takt(
                label_no=label_no,
                reason="first_print_position_reached",
            )
            esp_ready: dict[str, Any] = {"ok": False, "skipped": "wickler_takt_not_ready"}
            if bool(wickler_takt.get("ok")):
                try:
                    response = self._production_esp_retry(
                        f"PROCESS PRODUCTION WICKLER_READY LABEL_NO={int(label_no)}",
                        read_timeout_s=8.0,
                        attempts=5,
                        settle_s=0.2,
                        priority=True,
                    )
                    esp_ready = {"ok": True, "response": response}
                    self._remember_first_print_wickler_ready_sent(
                        ready_key,
                        label_no=label_no,
                        payload=payload,
                        wickler_takt=wickler_takt,
                        esp_ready=esp_ready,
                    )
                except Exception as exc:
                    esp_ready = {"ok": False, "error": repr(exc)}
                    self.logs.log(
                        "esp-plc",
                        "warning",
                        f"Wickler-Takt bereit, aber ESP-Freigabe fuer Label {label_no} fehlgeschlagen: {repr(exc)}",
                    )
            self._remember_first_print_wickler_ready_attempt(
                ready_key,
                label_no=label_no,
                payload=payload,
                status="finished",
                wickler_takt=wickler_takt,
                esp_ready=esp_ready,
            )
            return {
                "ok": True,
                "accepted": True,
                "event": event_type,
                "wickler_takt": wickler_takt,
                "esp_ready": esp_ready,
                **event_result,
            }
        if event_type == "production_print_position_commanded":
            label_no = _safe_int(payload.get("label_no"), 0)
            target = _safe_float(payload.get("target_abs_mm"), 0.0)
            remaining = _safe_float(payload.get("remaining_mm"), 0.0)
            event_result = self._record_production_event_once(
                event_type,
                "info",
                f"Druckposition befohlen: Label {label_no}, Ziel {target:.3f}mm, Restweg {remaining:.3f}mm",
                dict(payload or {}),
            )
            self._remember_production_wickler_observed_travel(
                label_no=label_no,
                remaining_mm=remaining,
                payload=payload,
            )
            wickler_reprepare: dict[str, Any] = {"ok": True, "skipped": "duplicate_or_invalid_commanded_position"}
            if bool(event_result.get("recorded")) and 10.0 <= remaining <= PRODUCTION_WICKLER_INDEXED_MAX_TRAVEL_MM:
                wickler_reprepare = self._prepare_next_production_wickler_takt(
                    label_no=label_no,
                    reason="print_position_commanded_remaining_mm",
                )
            return {
                "ok": True,
                "accepted": True,
                "event": event_type,
                "wickler_reprepare": wickler_reprepare,
                **event_result,
            }
        if event_type == "production_print_position_reached":
            label_no = _safe_int(payload.get("label_no"), 0)
            infeed_speed = _safe_float(payload.get("infeed_speed_mm_s"), 0.0)
            event_result = self._record_production_event_once(
                event_type,
                "info",
                f"Druckposition erreicht: Label {label_no}, Einlaufgeschwindigkeit {infeed_speed:.3f}mm/s",
                dict(payload or {}),
                dedupe_window_s=600.0,
            )
            next_wickler_takt = (
                self._prepare_next_production_wickler_takt(
                    label_no=label_no,
                    reason="after_print_position_reached",
                )
                if bool(event_result.get("recorded"))
                else {"ok": True, "skipped": "duplicate_print_position_reached", "label_no": label_no}
            )
            return {"ok": True, "accepted": True, "event": event_type, "next_wickler_takt": next_wickler_takt, **event_result}
        if event_type == "production_wickler_indexed_ready":
            label_no = _safe_int(payload.get("label_no"), 0)
            error_mm = _safe_float(payload.get("target_error_mm"), 0.0)
            event_result = self._record_production_event_once(
                event_type,
                "info",
                f"Wickler-Takt bereit: Label {label_no}, ID3-Nachpositionierung freigegeben, Restfehler {error_mm:.3f}mm",
                dict(payload or {}),
            )
            return {"ok": True, "accepted": True, "event": event_type, **event_result}
        if event_type == "production_wickler_runline_released":
            label_no = _safe_int(payload.get("label_no"), 0)
            speed = _safe_float(payload.get("infeed_speed_mm_s"), 0.0)
            event_result = self._record_production_event_once(
                event_type,
                "info",
                f"Wickler-Runline vor Druckstop geloest: Label {label_no}, Einlaufgeschwindigkeit {speed:.3f}mm/s",
                dict(payload or {}),
            )
            return {"ok": True, "accepted": True, "event": event_type, **event_result}
        if event_type == "production_print_trigger":
            label_no = _safe_int(payload.get("label_no"), 0)
            bypass = bool(payload.get("bypass"))
            duration = _safe_int(payload.get("duration_ms"), 0)
            error_mm = _safe_float(payload.get("position_error_mm"), 0.0)
            mode = "Bypass" if bypass else "real"
            suffix = f", Dauer {duration}ms" if duration > 0 else ""
            event_result = self._record_production_event_once(
                event_type,
                "info",
                f"Drucktrigger {mode}: Label {label_no}, Positionsfehler {error_mm:.4f}mm{suffix}",
                dict(payload or {}),
            )
            return {"ok": True, "accepted": True, "event": event_type, **event_result}
        if event_type == "production_print_resolved":
            label_no = _safe_int(payload.get("label_no"), 0)
            bypass = bool(payload.get("bypass"))
            mode = "Bypass" if bypass else "real"
            event_result = self._record_production_event_once(
                event_type,
                "info",
                f"Druck abgeschlossen {mode}: Label {label_no}",
                dict(payload or {}),
            )
            return {"ok": True, "accepted": True, "event": event_type, **event_result}
        if event_type == "production_print_position_failed":
            label_no = _safe_int(payload.get("label_no"), 0)
            reason = str(payload.get("reason") or "unknown")
            remaining = _safe_float(payload.get("remaining_mm"), 0.0)
            self._record_event(
                event_type,
                "warning",
                f"Druckposition nicht erreichbar: Label {label_no}, {reason}, Restweg {remaining:.3f}mm",
                dict(payload or {}),
            )
            return {"ok": True, "accepted": True, "event": event_type}
        if event_type == "setup_measure_absolute_commanded":
            phase_name = str(payload.get("phase_name") or payload.get("phase") or "unknown")
            target = _safe_float(payload.get("azd_target_mm"), 0.0)
            self._record_event(
                event_type,
                "info",
                f"Einrichten ID3-Absolutziel: {phase_name}, Ziel {target:.3f}mm",
                dict(payload or {}),
            )
            return {"ok": True, "accepted": True, "event": event_type}
        if event_type == "machine_state":
            state = _safe_int(payload.get("state"), 1)
            self.params.apply_device_value("MAS0001", str(state), promote_default=True)
            self._notify_microtom("MAS0001", str(state), dedupe_key="machine:MAS0001")
            self._record_event("machine_state", "info", f"ESP meldet Maschinenstatus {state} ({state_label(state)})", payload)
            return {"ok": True, "accepted": True, "event": event_type, "state": state}
        if event_type:
            self._record_event("machine_event", "info", f"Maschinenereignis empfangen: {event_type}", payload)
            return {"ok": True, "accepted": True, "event": event_type}
        return {"ok": False, "accepted": False, "detail": "missing event type"}

    def _handle_production_fault(self, payload: dict[str, Any]) -> dict[str, Any]:
        fault = str(payload.get("fault") or "").strip() or "unknown"
        if fault == "wickler_indexed_ready_timeout" and self._wickler_indexed_ready_seen_recently(payload):
            label_no = _safe_int(payload.get("label_no"), 0)
            message = (
                "Veralteter Wickler-Ready-Timeout ignoriert: "
                f"Label {label_no}, Ready wurde bereits verarbeitet"
            )
            ignored_payload = dict(payload or {})
            ignored_payload["ignored"] = "wickler_ready_already_seen"
            self._record_event("production_fault_ignored", "info", message, ignored_payload)
            return {"fault": fault, "ignored": "wickler_ready_already_seen", "message": message}
        if fault == "label_edge_timeout":
            acquire = _safe_float(payload.get("label_acquire_mm"), 0.0)
            limit = _safe_float(payload.get("label_acquire_limit_mm"), 0.0)
            timeout_ms = _safe_int(payload.get("label_acquire_timeout_ms"), 0)
            initial_level = _safe_int(payload.get("initial_label_level"), 0)
            label_sensor = _safe_int(payload.get("label_sensor"), 0)
            message = (
                "Produktionsfehler Labelkante nicht erkannt: "
                f"{acquire:.1f}mm von {limit:.1f}mm, Timeout {timeout_ms}ms, "
                f"Startpegel I0.5={initial_level}, aktueller I0.5={label_sensor}"
            )
        else:
            message = f"Produktionsfehler: {fault}"
        self._record_event("production_fault", "warning", message, dict(payload or {}))
        return {"fault": fault, "message": message}

    def _wickler_indexed_ready_seen_recently(self, payload: dict[str, Any] | None) -> bool:
        label_no = _safe_int((payload or {}).get("label_no"), 0)
        if label_no <= 0:
            return False
        cutoff = now_ts() - 20.0
        try:
            with self.db._conn() as c:
                rows = c.execute(
                    """SELECT ts,payload_json
                         FROM machine_events
                        WHERE event_type='production_wickler_indexed_ready' AND ts>=?
                        ORDER BY id DESC LIMIT 8""",
                    (cutoff,),
                ).fetchall()
        except Exception:
            rows = []
        for row in rows:
            try:
                previous = json.loads(row[1] or "{}")
            except Exception:
                previous = {}
            if _safe_int(previous.get("label_no"), 0) == label_no:
                return True

        state_info = dict(self._state_row().get("info") or {})
        production_info = dict(state_info.get(PRODUCTION_RUNTIME_INFO_KEY) or {})
        for key in ("first_print_wickler_ready", "first_print_wickler_ready_attempt"):
            item = production_info.get(key)
            if not isinstance(item, dict):
                continue
            if _safe_int(item.get("label_no"), 0) != label_no:
                continue
            if _safe_float(item.get("ts"), 0.0) < cutoff:
                continue
            if bool(item.get("wickler_takt_ok")):
                return True
        return False

    def _handle_registration_diagnostic_event(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        diag = dict((payload or {}).get("diag") or {})
        reason = str((payload or {}).get("reason") or diag.get("reason") or "").strip() or "unknown"
        label_no = _safe_int(diag.get("label_no"), 0)
        error = _safe_float(diag.get("error_mm"), 0.0)
        abs_error = _safe_float(diag.get("abs_error_mm"), abs(error))
        tolerance = _safe_float(diag.get("tolerance_mm"), 0.05)
        target = _safe_float(diag.get("target_mm"), 0.0)
        progressed = _safe_float(diag.get("progressed_mm"), 0.0)
        attempts = _safe_int(diag.get("registration_attempts"), 0)
        max_attempts = _safe_int(diag.get("max_attempts"), 3)
        infeed_speed = _safe_float(diag.get("infeed_speed_mm_s"), 0.0)
        motor_busy = bool(diag.get("motor_busy"))
        motor_ready = bool(diag.get("motor_ready", True))
        message = (
            f"MAE0048 Diagnose {reason}: Label {label_no}, "
            f"Abweichung {error:.4f}mm (abs {abs_error:.4f}mm, Tol +/-{tolerance:.4f}mm), "
            f"Weg {progressed:.3f}/{target:.3f}mm, "
            f"Korrekturen {attempts}/{max_attempts}, "
            f"Infeed {infeed_speed:.3f}mm/s, Motor busy={int(motor_busy)} ready={int(motor_ready)}"
        )
        self._record_event(event_type, "warning", message, dict(payload or {}))
        stop_result = self._latch_registration_fault(
            event_type=event_type,
            reason=reason,
            message=message,
            payload=payload,
            diag=diag,
        )
        return {"reason": reason, "message": message, "diag": diag, "stop": stop_result}

    def _stale_production_event_result(self, event_type: str, payload: dict[str, Any] | None) -> dict[str, Any] | None:
        if event_type not in PRODUCTION_RUNTIME_EVENT_TYPES:
            return None
        state = self._state_row()
        current_state = _safe_int(state.get("current_state"), 1)
        requested_state = _safe_int(state.get("requested_state"), current_state)
        info = dict(state.get("info") or {})
        production_info = dict(info.get(PRODUCTION_RUNTIME_INFO_KEY) or {})
        production_active = bool(production_info.get("active"))
        pending_start = production_info.get("pending_start") if isinstance(production_info.get("pending_start"), dict) else {}
        last_start = production_info.get("last_start") if isinstance(production_info.get("last_start"), dict) else {}
        last_stop = production_info.get("last_stop") if isinstance(production_info.get("last_stop"), dict) else {}
        has_runtime_context = bool(
            production_active
            or pending_start
            or last_start
            or last_stop
            or production_info.get("active_since_ts")
        )
        if event_type == "label_complete" and not has_runtime_context:
            return None
        last_start_age_s = now_ts() - _safe_float(last_start.get("started_ts"), 0.0)
        start_in_transition = (
            current_state == 4
            and requested_state == 5
            and (
                bool(pending_start)
                or production_active
                or (bool(last_start.get("ok")) and 0.0 <= last_start_age_s <= 30.0)
            )
        )
        if production_active and current_state in (4, 5):
            return None
        if start_in_transition:
            return None
        if current_state == 5 and event_type in {
            "production_fault",
            "production_registration_late",
            "production_registration_fault",
        }:
            return None

        label_no = _safe_int((payload or {}).get("label_no"), 0)
        stale_payload = {
            "type": "production_stale_event_ignored",
            "stale_event_type": event_type,
            "label_no": label_no,
            "machine_state": current_state,
            "requested_state": requested_state,
            "production_active": production_active,
        }
        event_result = self._record_production_event_once(
            "production_stale_event_ignored",
            "info",
            f"Veraltete Produktionsevents ignoriert: {event_type} in {state_label(current_state)}",
            stale_payload,
            dedupe_window_s=600.0,
        )
        return {
            "ok": True,
            "accepted": False,
            "event": event_type,
            "ignored": "stale_production_event",
            "machine_state": current_state,
            "production_active": production_active,
            **event_result,
        }

    def _first_print_wickler_ready_key(self, *, label_no: int, payload: dict[str, Any] | None) -> str:
        state_info = dict(self._state_row().get("info") or {})
        production_info = dict(state_info.get(PRODUCTION_RUNTIME_INFO_KEY) or {})
        run_marker = _safe_float(production_info.get("active_since_ts"), 0.0)
        if run_marker <= 0.0:
            last_start = production_info.get("last_start") if isinstance(production_info.get("last_start"), dict) else {}
            run_marker = _safe_float(last_start.get("started_ts"), 0.0)
        target_key = _event_float_key((payload or {}).get("target_abs_mm"), 3)
        error_key = _event_float_key((payload or {}).get("target_error_mm"), 3)
        return "|".join(
            [
                f"run={run_marker:.3f}" if run_marker > 0.0 else "run=unknown",
                f"label={int(label_no)}",
                f"target={target_key}",
                f"error={error_key}",
            ]
        )

    def _first_print_wickler_ready_already_sent(self, ready_key: str) -> bool:
        if not ready_key:
            return False
        state_info = dict(self._state_row().get("info") or {})
        production_info = dict(state_info.get(PRODUCTION_RUNTIME_INFO_KEY) or {})
        ready_info = production_info.get("first_print_wickler_ready")
        if not isinstance(ready_info, dict):
            attempt_info = production_info.get("first_print_wickler_ready_attempt")
            if not isinstance(attempt_info, dict):
                return False
            return str(attempt_info.get("key") or "") == str(ready_key)
        if str(ready_info.get("key") or "") == str(ready_key):
            return True
        attempt_info = production_info.get("first_print_wickler_ready_attempt")
        return isinstance(attempt_info, dict) and str(attempt_info.get("key") or "") == str(ready_key)

    def _remember_first_print_wickler_ready_attempt(
        self,
        ready_key: str,
        *,
        label_no: int,
        payload: dict[str, Any] | None,
        status: str,
        wickler_takt: dict[str, Any] | None = None,
        esp_ready: dict[str, Any] | None = None,
    ) -> None:
        if not ready_key:
            return
        state = self._state_row()
        info = dict(state.get("info") or {})
        production_info = dict(info.get(PRODUCTION_RUNTIME_INFO_KEY) or {})
        production_info["first_print_wickler_ready_attempt"] = {
            "key": str(ready_key),
            "label_no": int(label_no),
            "target_abs_mm": _safe_float((payload or {}).get("target_abs_mm"), 0.0),
            "target_error_mm": _safe_float((payload or {}).get("target_error_mm"), 0.0),
            "status": str(status or ""),
            "ts": now_ts(),
            "wickler_takt_ok": bool((wickler_takt or {}).get("ok")) if wickler_takt is not None else None,
            "esp_ready_ok": bool((esp_ready or {}).get("ok")) if esp_ready is not None else None,
        }
        info[PRODUCTION_RUNTIME_INFO_KEY] = production_info
        self._write_state(
            current_state=int(state.get("current_state") or 1),
            requested_state=int(state.get("requested_state") or state.get("current_state") or 1),
            state_source=str(state.get("state_source") or "runtime"),
            warning_active=bool(state.get("warning_active")),
            purge_active=bool(state.get("purge_active")),
            production_label=str(state.get("production_label") or ""),
            last_label_no=int(state.get("last_label_no") or 0),
            info=info,
        )

    def _remember_first_print_wickler_ready_sent(
        self,
        ready_key: str,
        *,
        label_no: int,
        payload: dict[str, Any] | None,
        wickler_takt: dict[str, Any],
        esp_ready: dict[str, Any],
    ) -> None:
        if not ready_key:
            return
        state = self._state_row()
        info = dict(state.get("info") or {})
        production_info = dict(info.get(PRODUCTION_RUNTIME_INFO_KEY) or {})
        production_info["first_print_wickler_ready"] = {
            "key": str(ready_key),
            "label_no": int(label_no),
            "target_abs_mm": _safe_float((payload or {}).get("target_abs_mm"), 0.0),
            "target_error_mm": _safe_float((payload or {}).get("target_error_mm"), 0.0),
            "ts": now_ts(),
            "wickler_takt_ok": bool((wickler_takt or {}).get("ok")),
            "esp_ready_ok": bool((esp_ready or {}).get("ok")),
        }
        info[PRODUCTION_RUNTIME_INFO_KEY] = production_info
        self._write_state(
            current_state=int(state.get("current_state") or 1),
            requested_state=int(state.get("requested_state") or state.get("current_state") or 1),
            state_source=str(state.get("state_source") or "runtime"),
            warning_active=bool(state.get("warning_active")),
            purge_active=bool(state.get("purge_active")),
            production_label=str(state.get("production_label") or ""),
            last_label_no=int(state.get("last_label_no") or 0),
            info=info,
        )

    def _remember_production_wickler_observed_travel(
        self,
        *,
        label_no: int,
        remaining_mm: float,
        payload: dict[str, Any] | None,
    ) -> None:
        travel_mm = float(remaining_mm)
        if travel_mm < 10.0 or travel_mm > PRODUCTION_WICKLER_INDEXED_MAX_TRAVEL_MM:
            return
        state = self._state_row()
        info = dict(state.get("info") or {})
        production_info = dict(info.get(PRODUCTION_RUNTIME_INFO_KEY) or {})
        production_info["last_wickler_observed_travel"] = {
            "label_no": int(label_no),
            "travel_mm": travel_mm,
            "target_abs_mm": _safe_float((payload or {}).get("target_abs_mm"), 0.0),
            "speed_mm_s": _safe_float((payload or {}).get("speed_mm_s"), 0.0),
            "ramp_mm_s2": _safe_float((payload or {}).get("ramp_mm_s2"), 0.0),
            "ts": now_ts(),
            "source": "esp_production_print_position_commanded.remaining_mm",
        }
        info[PRODUCTION_RUNTIME_INFO_KEY] = production_info
        self._write_state(
            current_state=int(state.get("current_state") or 1),
            requested_state=int(state.get("requested_state") or state.get("current_state") or 1),
            state_source=str(state.get("state_source") or "runtime"),
            warning_active=bool(state.get("warning_active")),
            purge_active=bool(state.get("purge_active")),
            production_label=str(state.get("production_label") or ""),
            last_label_no=int(state.get("last_label_no") or 0),
            info=info,
        )

    def _production_wickler_base_travel(self, plan: dict[str, Any]) -> tuple[float, str]:
        fallback_mm = float(plan["travel_mm"])
        state_info = dict(self._state_row().get("info") or {})
        production_info = dict(state_info.get(PRODUCTION_RUNTIME_INFO_KEY) or {})
        observed = production_info.get("last_wickler_observed_travel")
        if isinstance(observed, dict):
            observed_mm = _safe_float(observed.get("travel_mm"), 0.0)
            if 10.0 <= observed_mm <= PRODUCTION_WICKLER_INDEXED_MAX_TRAVEL_MM:
                label_no = _safe_int(observed.get("label_no"), 0)
                return observed_mm, f"last_esp_remaining_label_{label_no}"
        return fallback_mm, "format_plan_map0002_plus_map0076"

    def _latch_registration_fault(
        self,
        *,
        event_type: str,
        reason: str,
        message: str,
        payload: dict[str, Any] | None,
        diag: dict[str, Any],
    ) -> dict[str, Any]:
        self.params.apply_device_value("MAE0048", "1", promote_default=True)
        self._notify_microtom("MAE0048", "1", dedupe_key="machine:MAE0048")
        state = self._state_row()
        current_state = int(state.get("current_state") or 1)
        already_stopped = current_state not in (4, 5, 6)
        target_state = 21 if current_state == 21 or _truthy(self.params.get_effective_value("MAS0028")) else 7
        stop_result: dict[str, Any]
        if already_stopped:
            stop_result = {"ok": True, "skipped": f"machine_state={current_state}", "target_state": target_state}
        else:
            stop_result = self._stop_production_motion(reason=f"registration_fault:{reason}", target_state=target_state)

        info = dict(state.get("info") or {})
        production_info = dict(info.get(PRODUCTION_RUNTIME_INFO_KEY) or {})
        production_info["active"] = False
        production_info["last_registration_fault"] = {
            "event_type": str(event_type),
            "reason": str(reason),
            "message": str(message),
            "diag": dict(diag or {}),
            "payload": dict(payload or {}),
            "stop": dict(stop_result or {}),
            "ts": now_ts(),
        }
        if not already_stopped:
            production_info["last_stop"] = dict(stop_result or {})
        production_info.pop("pending_start", None)
        info[PRODUCTION_RUNTIME_INFO_KEY] = production_info

        if target_state != current_state:
            self.params.apply_device_value("MAS0001", str(target_state), promote_default=True)
            self._notify_microtom("MAS0001", str(target_state), dedupe_key="machine:MAS0001")
            self._sync_esp_machine_state(int(target_state), required=False)
            self._write_state(
                current_state=target_state,
                requested_state=target_state,
                state_source="production_registration_fault",
                warning_active=bool(state.get("warning_active")),
                purge_active=bool(target_state == 21 or state.get("purge_active")),
                production_label=str(state.get("production_label") or self._current_production_label()),
                last_label_no=int(state.get("last_label_no") or _safe_int(diag.get("label_no"), 0)),
                info=info,
            )
            self._record_event(
                "state_change",
                "info",
                f"Maschinenstatus gewechselt: {current_state} -> {target_state} ({state_label(target_state)})",
                {
                    "from_state": current_state,
                    "to_state": target_state,
                    "source": "production_registration_fault",
                    "reason": reason,
                    "mae": "MAE0048",
                },
            )
        else:
            self._write_state(
                current_state=current_state,
                requested_state=int(state.get("requested_state") or current_state),
                state_source=str(state.get("state_source") or "runtime"),
                warning_active=bool(state.get("warning_active")),
                purge_active=bool(state.get("purge_active")),
                production_label=str(state.get("production_label") or self._current_production_label()),
                last_label_no=int(state.get("last_label_no") or _safe_int(diag.get("label_no"), 0)),
                info=info,
            )
        return stop_result

    def _handle_label_length_fault(self, payload: dict[str, Any]) -> dict[str, Any]:
        label_no = _safe_int(payload.get("label_no"), 0)
        label = self._current_production_label() or sanitize_production_label(self.params.get_effective_value("MAS0029"))
        fault = str(payload.get("fault") or "").strip()
        measured = _safe_float(payload.get("measured_length_mm"), 0.0)
        expected = _safe_float(payload.get("expected_length_mm"), 0.0)
        tolerance = _safe_float(payload.get("tolerance_mm"), 0.0)
        direction = "zu kurz" if fault == "too_short" else ("zu lang" if fault == "too_long" else fault or "ungueltig")
        message = (
            f"Label {label_no} Laengenfehler {direction}: "
            f"Ist {measured:.3f}mm, Soll {expected:.3f}mm +/- {tolerance:.3f}mm"
        )
        with self.db._conn() as c:
            c.execute(
                "INSERT INTO label_events(ts,production_label,label_no,event_type,payload_json) VALUES(?,?,?,?,?)",
                (now_ts(), label, label_no, "label_length_fault", json.dumps(payload, ensure_ascii=False)),
            )
        self.production_logs.append_line("labels", label, f"[label_length_fault] {message}\n")
        self._record_event(
            "label_length_fault",
            "warning",
            message,
            {"production_label": label, **dict(payload)},
        )
        return {
            "production_label": label,
            "label_no": label_no,
            "fault": fault,
            "measured_length_mm": measured,
            "expected_length_mm": expected,
            "tolerance_mm": tolerance,
        }

    def _normalize_machine_button(self, button: str) -> str:
        button_name = str(button or "").strip().lower().replace("-", "_")
        if button_name == "start":
            button_name = "start_pause"
        if button_name == "pause":
            button_name = "start_pause"
        valid = {"start_pause", "stop", "setup", "sync", "empty", "rewind"}
        if button_name not in valid:
            raise RuntimeError(f"unknown machine button: {button}")
        return button_name

    def _button_reset_context(self, snapshot: dict[str, Any], info: dict[str, Any]) -> bool:
        safety_info = dict((info or {}).get("safety") or {})
        return (
            int((snapshot or {}).get("current_state") or 1) in (20, 21)
            or bool((snapshot or {}).get("purge_active"))
            or bool(safety_info.get("latched"))
        )

    def _motion_action_blocked_by_live_light_curtain(self, action_name: str | None, info: dict[str, Any]) -> bool:
        if str(action_name or "") not in _LIGHT_CURTAIN_BLOCKED_ACTIONS:
            return False
        safety_status = dict((info or {}).get("safety_status") or {})
        return bool(safety_status.get("light_curtain_active"))

    def _resolve_button_press(
        self,
        button: str,
        *,
        snapshot: dict[str, Any],
        info: dict[str, Any],
        button_mask: dict[str, bool],
    ) -> dict[str, Any]:
        button_name = self._normalize_machine_button(button)
        current_state = int(snapshot["current_state"] or 1)
        reset_context = self._button_reset_context(snapshot, info)
        allowed_actions = state_actions(current_state)

        command = button_to_command(button_name, current_state)
        action_name = action_for_button(button_name, current_state, reset_context=reset_context)
        if button_name == "start_pause" and reset_context:
            command = 2
        elif reset_context:
            raise RuntimeError(f"button {button_name} is blocked during reset/safety context")
        if command is None:
            raise RuntimeError(f"button {button_name} is not valid in state {current_state}")
        if action_name is None:
            raise RuntimeError(f"button {button_name} has no mapped action in state {current_state}")
        if not reset_context and self._motion_action_blocked_by_live_light_curtain(action_name, info):
            raise RuntimeError(f"button {button_name} blocked while light curtain is active")
        if not reset_context and not allowed_actions.get(action_name, False):
            raise RuntimeError(f"button {button_name} is not allowed in state {current_state}")
        reset_button = button_name == "start_pause" and reset_context
        if reset_button:
            self._ensure_laser_reset_interlock_clear(source=f"button:{button_name}")
        if not reset_button and not button_mask.get(action_name, False):
            raise RuntimeError(f"button {button_name} blocked by MAP0065")

        target_state = command_to_target_state(command, current_state)
        return {
            "button": button_name,
            "action": action_name,
            "command": int(command),
            "from_state": current_state,
            "target_state": target_state,
            "reset_context": reset_context,
        }

    def press_virtual_button(self, button: str, *, actor: str = "machine-ui") -> dict[str, Any]:
        snapshot = self._state_row()
        info = dict(snapshot.get("info") or {})
        try:
            info["safety_status"] = self._safety_status(self._io_values())
        except Exception:
            pass
        param_map = self._param_values_by_prefix(("MAP", "MAS", "MAE", "MAW"))
        button_mask = parse_button_mask(param_map.get("MAP0065", "1111111"))
        request = self._resolve_button_press(
            button,
            snapshot=snapshot,
            info=info,
            button_mask=button_mask,
        )

        ok, msg = self.params.set_value("MAS0002", str(request["command"]), actor=actor)
        if not ok:
            raise RuntimeError(msg)
        payload = {
            "actor": actor,
            **request,
        }
        self.logs.log(
            "machine",
            "info",
            f"virtual button {request['button']} -> MAS0002={request['command']} ({state_label(request['target_state'])})",
        )
        self._record_event(
            "virtual_button",
            "info",
            f"Virtuelle Taste {request['button']} ausgeloest -> {state_label(request['target_state'])}",
            payload,
        )
        queued_snapshot = self._state_row()
        queued_info = dict(queued_snapshot.get("info") or {})
        queued_info["pending_hmi_command"] = {
            "button": request["button"],
            "action": request["action"],
            "command": int(request["command"]),
            "from_state": int(request["from_state"]),
            "target_state": int(request["target_state"]),
            "queued_ts": now_ts(),
            "actor": actor,
        }
        self._write_state(
            current_state=int(queued_snapshot.get("current_state") or request["from_state"]),
            requested_state=int(request["target_state"]),
            state_source=str(queued_snapshot.get("state_source") or "runtime"),
            warning_active=bool(queued_snapshot.get("warning_active")),
            purge_active=bool(queued_snapshot.get("purge_active")),
            production_label=str(queued_snapshot.get("production_label") or ""),
            last_label_no=int(queued_snapshot.get("last_label_no") or 0),
            info=queued_info,
        )
        # The virtual HMI button must behave like the physical panel: it only
        # queues MAS0002.  The central service loop owns the runtime transition
        # and motion start, avoiding competing refreshes from web/API workers.
        snapshot = self.snapshot()
        return {
            "ok": True,
            "button": request["button"],
            "command": request["command"],
            "target_state": request["target_state"],
            "queued": True,
            "snapshot": snapshot,
        }

    def _handle_label_complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        label_no = _safe_int(payload.get("label_no"), 0)
        if label_no <= 0:
            raise RuntimeError("label_no missing")
        label = self._current_production_label()
        if not label:
            label = sanitize_production_label(self.params.get_effective_value("MAS0029"))
        material_ok = bool(int(payload.get("material_ok", 1)))
        print_ok = bool(int(payload.get("print_ok", 1)))
        verify_ok = bool(int(payload.get("verify_ok", 1)))
        removed = bool(int(payload.get("removed", 0)))
        production_ok = bool(int(payload.get("production_ok", 1 if (material_ok and print_ok and verify_ok and not removed) else 0)))
        zero_mm = float(payload.get("zero_mm", 0.0) or 0.0)
        exit_mm = float(payload.get("exit_mm", 0.0) or 0.0)
        compact_payload = {
            "type": "label_complete",
            "label_no": label_no,
            "zero_mm": zero_mm,
            "exit_mm": exit_mm,
            "measured_length_mm": _safe_float(payload.get("measured_length_mm"), 0.0),
            "material_ok": material_ok,
            "print_ok": print_ok,
            "verify_ok": verify_ok,
            "removed": removed,
            "production_ok": production_ok,
            "length_too_short": bool(payload.get("length_too_short")),
            "length_too_long": bool(payload.get("length_too_long")),
            "registration_late": bool(payload.get("registration_late")),
            "registration_attempts": _safe_int(payload.get("registration_attempts"), 0),
            "print_error_mm": _safe_float(payload.get("print_error_mm"), 0.0),
        }
        packed = pack_label_status_word(
            label_no=label_no,
            material_ok=material_ok,
            print_ok=print_ok,
            verify_ok=verify_ok,
            removed=removed,
            production_ok=production_ok,
        )
        with self.db._conn() as c:
            c.execute(
                """INSERT INTO label_register(
                       production_label,label_no,created_ts,completed_ts,zero_mm,exit_mm,
                       material_ok,print_ok,verify_ok,removed,production_ok,emitted_to_microtom,payload_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(production_label,label_no) DO UPDATE SET
                     completed_ts=excluded.completed_ts,
                     zero_mm=excluded.zero_mm,
                     exit_mm=excluded.exit_mm,
                     material_ok=excluded.material_ok,
                     print_ok=excluded.print_ok,
                     verify_ok=excluded.verify_ok,
                     removed=excluded.removed,
                     production_ok=excluded.production_ok,
                     payload_json=excluded.payload_json""",
                (
                    label,
                    label_no,
                    now_ts(),
                    now_ts(),
                    zero_mm,
                    exit_mm,
                    int(material_ok),
                    int(print_ok),
                    int(verify_ok),
                    int(removed),
                    int(production_ok),
                    0,
                    json.dumps(compact_payload, ensure_ascii=False),
                ),
            )
            c.execute(
                "INSERT INTO label_events(ts,production_label,label_no,event_type,payload_json) VALUES(?,?,?,?,?)",
                (now_ts(), label, label_no, "label_complete", json.dumps(compact_payload, ensure_ascii=False)),
            )

        self.params.apply_device_value("MAS0003", str(packed), promote_default=True)
        self._notify_microtom("MAS0003", str(packed), dedupe_key=None)
        self.production_logs.append_line(
            "labels",
            label,
            (
                f"[label_complete] label={label_no} packed={packed} material_ok={int(material_ok)} "
                f"print_ok={int(print_ok)} verify_ok={int(verify_ok)} removed={int(removed)} "
                f"production_ok={int(production_ok)} zero_mm={zero_mm:.3f} exit_mm={exit_mm:.3f}\n"
            ),
        )
        snapshot = self._state_row()
        info = dict(snapshot.get("info") or {})
        info["last_label_payload"] = dict(compact_payload)
        self._write_state(
            current_state=snapshot["current_state"],
            requested_state=snapshot["requested_state"],
            state_source=snapshot["state_source"],
            warning_active=bool(snapshot["warning_active"]),
            purge_active=bool(snapshot["purge_active"]),
            production_label=label,
            last_label_no=max(snapshot["last_label_no"], label_no),
            info=info,
        )
        self._record_event(
            "label_complete",
            "info",
            f"Label {label_no} abgeschlossen -> MAS0003={packed}",
            {
                "production_label": label,
                "label_no": label_no,
                "packed": packed,
                "material_ok": material_ok,
                "print_ok": print_ok,
                "verify_ok": verify_ok,
                "removed": removed,
                "production_ok": production_ok,
            },
        )
        return {
            "production_label": label,
            "label_no": label_no,
            "packed": packed,
        }

    def _state_row(self) -> dict[str, Any]:
        with self.db._conn() as c:
            row = c.execute(
                """SELECT current_state,requested_state,state_source,warning_active,purge_active,
                          production_label,last_label_no,info_json,updated_ts
                   FROM machine_state WHERE singleton_id=1"""
            ).fetchone()
        if not row:
            return {
                "current_state": 1,
                "requested_state": 1,
                "state_source": "runtime",
                "warning_active": False,
                "purge_active": False,
                "production_label": "",
                "last_label_no": 0,
                "info": {},
                "updated_ts": 0.0,
            }
        try:
            info = json.loads(row[7] or "{}")
        except Exception:
            info = {}
        return {
            "current_state": int(row[0] or 1),
            "requested_state": int(row[1] or 1),
            "state_source": str(row[2] or "runtime"),
            "warning_active": bool(row[3]),
            "purge_active": bool(row[4]),
            "production_label": str(row[5] or ""),
            "last_label_no": int(row[6] or 0),
            "info": info,
            "updated_ts": float(row[8] or 0.0),
        }

    def _write_state(
        self,
        *,
        current_state: int,
        requested_state: int,
        state_source: str,
        warning_active: bool,
        purge_active: bool,
        production_label: str,
        last_label_no: int,
        info: dict[str, Any],
    ):
        latest_row = _machine_state_row_from_db(self.db)
        latest_info = dict(latest_row.get("info") or {})
        info = _merge_newer_purge_info(info, latest_info)
        ts = now_ts()
        if (
            int(latest_row.get("current_state") or 0) == int(current_state)
            and int(latest_row.get("requested_state") or 0) == int(requested_state)
            and str(latest_row.get("state_source") or "runtime") == str(state_source or "runtime")
            and bool(latest_row.get("warning_active")) == bool(warning_active)
            and bool(latest_row.get("purge_active")) == bool(purge_active)
            and str(latest_row.get("production_label") or "") == str(production_label or "")
            and int(latest_row.get("last_label_no") or 0) == int(last_label_no or 0)
            and dict(latest_row.get("info") or {}) == dict(info or {})
            and (ts - float(latest_row.get("updated_ts") or 0.0)) < MACHINE_STATE_HEARTBEAT_WRITE_INTERVAL_S
        ):
            return
        with self.db._conn() as c:
            c.execute(
                """INSERT INTO machine_state(
                       singleton_id,current_state,requested_state,state_source,warning_active,purge_active,
                       production_label,last_label_no,info_json,updated_ts
                   ) VALUES(1,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(singleton_id) DO UPDATE SET
                     current_state=excluded.current_state,
                     requested_state=excluded.requested_state,
                     state_source=excluded.state_source,
                     warning_active=excluded.warning_active,
                     purge_active=excluded.purge_active,
                     production_label=excluded.production_label,
                     last_label_no=excluded.last_label_no,
                     info_json=excluded.info_json,
                     updated_ts=excluded.updated_ts""",
                (
                    int(current_state),
                    int(requested_state),
                    str(state_source or "runtime"),
                    int(bool(warning_active)),
                    int(bool(purge_active)),
                    str(production_label or ""),
                    int(last_label_no or 0),
                    json.dumps(info, ensure_ascii=False),
                    ts,
                ),
            )

    def _record_event(self, event_type: str, severity: str, message: str, payload: dict[str, Any] | None = None):
        with self.db._conn() as c:
            c.execute(
                "INSERT INTO machine_events(ts,event_type,severity,message,payload_json) VALUES(?,?,?,?,?)",
                (now_ts(), event_type, severity, message, json.dumps(payload or {}, ensure_ascii=False)),
            )

    def _finalize_production_logging_stop(self, reason: str) -> Optional[dict[str, Any]]:
        event = self.production_logs.handle_param_change("MAS0002", "2")
        if event and event.get("event") == "stop":
            self.logs.log(
                "raspi",
                "info",
                f"production logging ready: {event.get('production_label')} ({reason})",
            )
        return event

    def _record_production_event_once(
        self,
        event_type: str,
        severity: str,
        message: str,
        payload: dict[str, Any] | None = None,
        *,
        dedupe_window_s: float = 2.0,
    ) -> dict[str, Any]:
        payload = dict(payload or {})
        signature = self._production_event_signature(event_type, payload)
        if signature:
            cutoff = now_ts() - max(0.1, float(dedupe_window_s))
            with self.db._conn() as c:
                rows = c.execute(
                    """SELECT payload_json
                         FROM machine_events
                        WHERE event_type=? AND ts>=?
                        ORDER BY id DESC LIMIT 12""",
                    (str(event_type), cutoff),
                ).fetchall()
            for row in rows:
                try:
                    previous_payload = json.loads(row[0] or "{}")
                except Exception:
                    previous_payload = {}
                if self._production_event_signature(event_type, previous_payload) == signature:
                    return {"recorded": False, "deduped": True, "dedupe_signature": signature}
        self._record_event(event_type, severity, message, payload)
        return {"recorded": True, "deduped": False, "dedupe_signature": signature}

    @staticmethod
    def _production_event_signature(event_type: str, payload: dict[str, Any] | None) -> str:
        payload = dict(payload or {})
        event_type = str(event_type or "").strip().lower()
        label_no = _safe_int(payload.get("label_no"), 0)
        if event_type == "production_stale_event_ignored":
            return "|".join(
                [
                    event_type,
                    str(payload.get("stale_event_type") or ""),
                    str(_safe_int(payload.get("machine_state"), 0)),
                ]
            )
        if label_no <= 0:
            return ""
        if event_type == "production_print_position_commanded":
            return "|".join(
                [
                    event_type,
                    str(label_no),
                    _event_float_key(payload.get("target_abs_mm"), 3),
                    _event_float_key(payload.get("remaining_mm"), 3),
                ]
            )
        if event_type == "production_velocity_stop_for_print":
            return "|".join(
                [
                    event_type,
                    str(label_no),
                    _event_float_key(payload.get("remaining_mm"), 3),
                ]
            )
        if event_type == "production_first_print_position_reached":
            return "|".join([event_type, str(label_no)])
        if event_type == "production_print_position_reached":
            return "|".join(
                [
                    event_type,
                    str(label_no),
                    str(_safe_int(payload.get("run_ms"), 0)),
                    _event_float_key(payload.get("target_abs_mm") or payload.get("position_command_mm"), 3),
                    str(_safe_int(payload.get("print_target_count"), 0)),
                ]
            )
        if event_type == "production_wickler_indexed_ready":
            return "|".join([event_type, str(label_no)])
        if event_type == "production_wickler_runline_released":
            return "|".join([event_type, str(label_no)])
        if event_type == "production_registration_correction":
            return "|".join(
                [
                    event_type,
                    str(label_no),
                    str(_safe_int(payload.get("attempt"), 0)),
                    _event_float_key(payload.get("error_mm"), 4),
                    _event_float_key(payload.get("command_mm"), 4),
                ]
            )
        if event_type == "production_registration_correction_effect":
            return "|".join(
                [
                    event_type,
                    str(label_no),
                    str(_safe_int(payload.get("command_seq"), 0)),
                    str(int(bool(payload.get("accepted")))),
                    str(payload.get("reason") or ""),
                    str(_safe_int(payload.get("expected_encoder_counts"), 0)),
                    str(_safe_int(payload.get("actual_encoder_delta_counts"), 0)),
                    str(_safe_int(payload.get("expected_motor_steps"), 0)),
                    str(_safe_int(payload.get("actual_motor_feedback_delta_steps"), 0)),
                ]
            )
        if event_type == "production_print_trigger":
            return "|".join(
                [
                    event_type,
                    str(label_no),
                    str(int(bool(payload.get("bypass")))),
                    str(int(bool(payload.get("use_laser")))),
                    str(_safe_int(payload.get("duration_ms"), 0)),
                ]
            )
        if event_type == "production_print_resolved":
            return "|".join([event_type, str(label_no), str(int(bool(payload.get("bypass"))))])
        return ""

    def _latest_machine_event_ts(self, event_type: str) -> float:
        with self.db._conn() as c:
            row = c.execute(
                "SELECT ts FROM machine_events WHERE event_type=? ORDER BY ts DESC LIMIT 1",
                (str(event_type),),
            ).fetchone()
        if not row:
            return 0.0
        return _safe_float(row[0], 0.0)

    def _latest_motor_setup_master_ts(self, motor_id: int) -> float:
        try:
            with self.db._conn() as c:
                row = c.execute(
                    "SELECT updated_ts FROM motor_setup_master WHERE motor_id=?",
                    (int(motor_id),),
                ).fetchone()
        except Exception:
            return 0.0
        if not row:
            return 0.0
        return _safe_float(row[0], 0.0)

    def _latest_motor_position_reference_suspect_event(self, motor_id: int) -> dict[str, Any] | None:
        try:
            with self.db._conn() as c:
                rows = c.execute(
                    """SELECT ts,event_type,message,payload_json
                       FROM machine_events
                       WHERE event_type IN ('motor_setup_position_restored','motor_position_reference_suspect')
                       ORDER BY ts DESC
                       LIMIT 100"""
                ).fetchall()
        except Exception:
            return None
        for ts, event_type, message, payload_json in rows:
            try:
                payload = json.loads(payload_json or "{}")
            except Exception:
                payload = {}
            try:
                payload_motor_id = int(payload.get("motor_id"))
            except Exception:
                continue
            if payload_motor_id != int(motor_id):
                continue
            return {
                "ts": _safe_float(ts, 0.0),
                "event_type": str(event_type or ""),
                "message": str(message or ""),
                "payload": payload if isinstance(payload, dict) else {},
            }
        return None

    def _position_axis_reference_suspect(self, motor_id: int) -> dict[str, Any] | None:
        suspect_event = self._latest_motor_position_reference_suspect_event(motor_id)
        if not suspect_event:
            return None
        suspect_ts = _safe_float(suspect_event.get("ts"), 0.0)
        master_ts = self._latest_motor_setup_master_ts(motor_id)
        if suspect_ts <= master_ts:
            return None
        return {
            "motor_id": int(motor_id),
            "blocked": True,
            "reason": "previous_automatic_position_restore_after_last_machine_setup",
            "suspect_ts": suspect_ts,
            "motor_setup_master_ts": master_ts,
            "suspect_event": suspect_event,
        }

    def _recent_events(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.db._conn() as c:
            rows = c.execute(
                "SELECT ts,event_type,severity,message,payload_json FROM machine_events ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        out = []
        for ts, event_type, severity, message, payload_json in rows:
            try:
                payload = json.loads(payload_json or "{}")
            except Exception:
                payload = {}
            out.append(
                {
                    "ts": float(ts or 0.0),
                    "event_type": event_type,
                    "severity": severity,
                    "message": message,
                    "payload": payload,
                }
            )
        return out

    def _recent_labels(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.db._conn() as c:
            rows = c.execute(
                """SELECT production_label,label_no,created_ts,completed_ts,material_ok,print_ok,
                          verify_ok,removed,production_ok,payload_json
                   FROM label_register ORDER BY created_ts DESC LIMIT ?""",
                (int(limit),),
            ).fetchall()
        out = []
        for row in rows:
            try:
                payload = json.loads(row[9] or "{}")
            except Exception:
                payload = {}
            out.append(
                {
                    "production_label": row[0],
                    "label_no": int(row[1] or 0),
                    "created_ts": float(row[2] or 0.0),
                    "completed_ts": float(row[3] or 0.0),
                    "material_ok": bool(row[4]),
                    "print_ok": bool(row[5]),
                    "verify_ok": bool(row[6]),
                    "removed": bool(row[7]),
                    "production_ok": bool(row[8]),
                    "payload": payload,
                }
            )
        return out

    def _param_values_by_prefix(self, prefixes: tuple[str, ...]) -> dict[str, str]:
        placeholders = ",".join("?" for _ in prefixes)
        with self.db._conn() as c:
            rows = c.execute(
                f"""SELECT p.pkey, COALESCE(v.value, p.default_v, '0')
                    FROM params p
                    LEFT JOIN param_values v ON v.pkey = p.pkey
                    WHERE p.ptype IN ({placeholders})""",
                tuple(prefixes),
            ).fetchall()
        return {str(row[0]): str(row[1] if row[1] is not None else "0") for row in rows}

    def _param_updated_ts(self, pkey: str) -> float:
        with self.db._conn() as c:
            row = c.execute("SELECT updated_ts FROM param_values WHERE pkey=?", (str(pkey),)).fetchone()
        try:
            return float(row[0] or 0.0) if row else 0.0
        except Exception:
            return 0.0

    def _format_ready_for_pause(self, param_map: dict[str, str]) -> tuple[bool, list[str]]:
        plan = build_format_plan(param_map)
        missing: list[str] = []
        if _safe_int(param_map.get("MAP0001"), 0) <= 0:
            missing.append("MAP0001")
        if _safe_int(param_map.get("MAP0002"), 0) <= 0:
            missing.append("MAP0002")
        if _safe_int(param_map.get("MAP0014"), 0) <= 0:
            missing.append("MAP0014")
        if _safe_int((plan.get("printer") or {}).get("stop_distance_tenths_mm"), 0) <= 0:
            missing.append(str((plan.get("printer") or {}).get("distance_param") or "MAP0018/MAP0019"))
        if not sanitize_production_label(param_map.get("MAS0029", "")):
            missing.append("MAS0029")
        return len(missing) == 0, missing

    def _perform_setup_wickler_calibration(self) -> dict[str, Any]:
        started_ts = now_ts()
        if not _SETUP_WICKLER_LOCK.acquire(blocking=False):
            return {"ok": False, "skipped": True, "reason": "already_running", "started_ts": started_ts}
        try:
            param_map = self._param_values_by_prefix(("MAP", "MAS", "MAE", "MAW"))
            format_plan = build_format_plan(param_map)
            self.logs.log(
                "machine",
                "info",
                "Einrichten: Formatachsen starten, Wickler parallel einmessen und Messfahrt vorbereiten",
            )
            self._record_event(
                "setup_wickler_calibration",
                "info",
                "Einrichten gestartet: Formatachsen positionieren parallel zur Wickler-Einmessung, "
                "danach Messfahrt ausfuehren und Durchmesser uebernehmen",
                {},
            )
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="setup-format-axes") as executor:
                axis_future = executor.submit(self._position_setup_format_axes, format_plan)

                def wait_for_format_axes() -> dict[str, Any]:
                    self.logs.log(
                        "machine",
                        "info",
                        "Einrichten: Wickler-Einmessung erreicht ID3-Messfahrt; warte auf Formatachsen",
                    )
                    return axis_future.result()

                controller = SetupWicklerOrchestrator(self.cfg, self.params, self.logs)
                workflow = controller.run(wait_for_format_axes=wait_for_format_axes)
                axis_result = axis_future.result()
            ok = bool(workflow.get("ok"))
            result = {
                "ok": ok,
                "response": "ACK_SETUP_WICKLER",
                "format_axes": axis_result,
                "workflow": workflow,
                "started_ts": started_ts,
                "finished_ts": now_ts(),
            }
            result_message = (
                "Einrichten-Wicklerworkflow abgeschlossen"
                if ok
                else f"Einrichten-Wicklerworkflow fehlgeschlagen: {result.get('response')}"
            )
            self._record_event(
                "setup_wickler_calibration",
                "info" if ok else "error",
                result_message,
                result,
            )
            return result
        except Exception as exc:
            result = {
                "ok": False,
                "response": "SETUP_WICKLER=NAK_DeviceComm",
                "error": str(exc),
                "started_ts": started_ts,
                "finished_ts": now_ts(),
            }
            self.logs.log("machine", "error", f"Einrichten-Wicklerworkflow fehlgeschlagen: {repr(exc)}")
            self._record_event(
                "setup_wickler_calibration",
                "error",
                f"Einrichten-Wicklerworkflow fehlgeschlagen: {exc}",
                result,
            )
            return result
        finally:
            _SETUP_WICKLER_LOCK.release()

    def _describe_runtime_reasons(self, reasons: list[str] | tuple[str, ...]) -> str:
        cleaned = [str(reason).strip() for reason in (reasons or []) if str(reason).strip()]
        if not cleaned:
            return "keine Ursache angegeben"
        labels = {
            "notaus": "Not-Aus",
            "lichtgitter": "Lichtgitter",
            "usv_not_ok": "USV nicht OK",
            "bahnriss_einlauf": "Bandriss Einlauf",
            "bahnriss_auswurf": "Bandriss Auswurf",
            "MAS0028": "Purge/Safety-Latch MAS0028",
        }
        meta: dict[str, dict[str, Any]] = {}
        param_keys = [reason for reason in cleaned if reason.startswith(("MAE", "MAW", "MAS", "MAP"))]
        if param_keys:
            try:
                placeholders = ",".join("?" for _ in param_keys)
                with self.db._conn() as c:
                    rows = c.execute(
                        f"SELECT pkey,name,message,possible_cause,remedy FROM params WHERE pkey IN ({placeholders})",
                        tuple(param_keys),
                    ).fetchall()
                meta = {
                    str(row[0]): {
                        "name": row[1],
                        "message": row[2],
                        "cause": row[3],
                        "remedy": row[4],
                    }
                    for row in rows
                }
            except Exception:
                meta = {}
        parts: list[str] = []
        for reason in cleaned:
            item = meta.get(reason) or {}
            title = str(item.get("name") or labels.get(reason) or reason).strip()
            msg = str(item.get("message") or "").strip()
            if msg and msg not in title:
                parts.append(f"{reason} {title}: {msg}")
            else:
                parts.append(f"{reason} {title}" if title != reason else reason)
        return "; ".join(parts)

    def _clear_pause_errors_for_production_start(self) -> list[str]:
        cleared: list[str] = []
        for pkey in sorted(PAUSE_ERROR_KEYS):
            try:
                if not _truthy(self.params.get_effective_value(pkey)):
                    continue
                self.params.apply_device_value(pkey, "0", promote_default=True)
                self._notify_microtom(pkey, "0", dedupe_key=f"machine:{pkey}")
                cleared.append(pkey)
            except Exception as exc:
                self.logs.log("machine", "warning", f"Start konnte Pausebit {pkey} nicht loeschen: {exc}")
        if cleared:
            reason_text = self._describe_runtime_reasons(cleared)
            self.logs.log("machine", "warning", f"Produktionsstart quittiert Pausefehler: {reason_text}")
            self._record_event(
                "production_pause_errors_cleared_for_start",
                "warning",
                f"Produktionsstart quittiert Pausefehler: {reason_text}",
                {"cleared": cleared, "reason_text": reason_text},
            )
        return cleared

    def _force_stop_process_motion_on_fault(self, info: dict[str, Any], reasons: list[str], ts: float) -> None:
        # Red machine faults are not just UI state: they must remove motion
        # authority from Motor 3 and both Wicklers. Keep this idempotent because
        # refresh() runs cyclically while the fault remains latched.
        reason_set = {str(reason).strip() for reason in reasons if str(reason).strip()}
        signature = ",".join(sorted(reason_set)) or "fault"
        last_signature = str(info.get("last_fault_motion_stop_signature") or "")
        stop_state = dict(info.get("fault_motion_stop_state") or {})
        previous_reasons = {str(reason).strip() for reason in (stop_state.get("reasons") or []) if str(reason).strip()}
        new_real_reasons = reason_set.difference(previous_reasons).difference({"MAS0028"})
        try:
            last_ts = float(info.get("last_fault_motion_stop_ts") or 0.0)
        except Exception:
            last_ts = 0.0
        if bool(stop_state.get("ok")) and not new_real_reasons:
            stop_state["latest_reasons"] = sorted(reason_set)
            stop_state["latest_signature"] = signature
            stop_state["latest_ts"] = float(ts)
            info["fault_motion_stop_state"] = stop_state
            return
        if signature == last_signature and bool(stop_state.get("ok")):
            return
        if signature == last_signature and (float(ts) - last_ts) < 2.0:
            return

        ok = False
        reason_text = self._describe_runtime_reasons(sorted(reason_set))
        try:
            if bool(getattr(self.cfg, "esp_simulation", False)):
                self.logs.log("machine", "warning", f"Fault motion stop simulated: {reason_text}")
            else:
                SetupWicklerOrchestrator(self.cfg, self.params, self.logs).stop_all_motion()
                self.logs.log("machine", "warning", f"Fault motion stop executed: {reason_text}")
            ok = True
            self._record_event(
                "fault_motion_stop",
                "warning",
                f"Kritischer Fehler: Motor 3 und beide Wickler wurden gestoppt ({reason_text})",
                {"reasons": sorted(reason_set), "reason_text": reason_text},
            )
        except Exception as exc:
            self.logs.log("machine", "error", f"Fault motion stop failed for {reason_text}: {repr(exc)}")
            self._record_event(
                "fault_motion_stop",
                "error",
                f"Kritischer Fehler: Bewegungsstop konnte nicht vollstaendig gesendet werden: {exc}",
                {"reasons": sorted(reason_set), "reason_text": reason_text},
            )
        finally:
            info["last_fault_motion_stop_signature"] = signature
            info["last_fault_motion_stop_ts"] = float(ts)
            info["fault_motion_stop_state"] = {
                "ok": ok,
                "signature": signature,
                "reasons": sorted(reason_set),
                "ts": float(ts),
            }

    def _production_motion_plan(self, param_map: dict[str, str], format_plan: dict[str, Any]) -> dict[str, Any]:
        label_plan = dict((format_plan or {}).get("label") or {})
        printer_plan = dict((format_plan or {}).get("printer") or {})
        length_tenths = _safe_float(label_plan.get("length_tenths_mm", param_map.get("MAP0002")), 0.0)
        compensation_tenths = _safe_float(
            ((format_plan or {}).get("process") or {}).get(
                "label_length_compensation_tenths_mm",
                param_map.get("MAP0076"),
            ),
            0.0,
        )
        nominal_travel_mm = length_tenths / 10.0
        label_length_compensation_mm = compensation_tenths / 10.0
        travel_mm = (length_tenths + compensation_tenths) / 10.0
        if travel_mm < 1.0 or travel_mm > PRODUCTION_WICKLER_INDEXED_MAX_TRAVEL_MM:
            raise RuntimeError(
                f"Etikettenlaenge ausserhalb ESP-Grenze: {travel_mm:.3f}mm "
                f"(MAP0002={nominal_travel_mm:.3f}mm, MAP0076={label_length_compensation_mm:.3f}mm)"
            )
        first_print_tenths = _safe_float(printer_plan.get("stop_distance_tenths_mm"), 0.0)
        first_wickler_travel_mm = first_print_tenths / 10.0
        if first_wickler_travel_mm < 1.0:
            first_wickler_travel_mm = travel_mm
        if first_wickler_travel_mm > PRODUCTION_WICKLER_INDEXED_MAX_TRAVEL_MM:
            raise RuntimeError(
                f"Erster Wickler-Takt ausserhalb Grenze: {first_wickler_travel_mm:.3f}mm "
                f"(max {PRODUCTION_WICKLER_INDEXED_MAX_TRAVEL_MM:.1f}mm)"
            )
        speed_mm_s = abs(_safe_float(param_map.get("MAP0014"), 100.0))
        speed_mm_s = max(1.0, min(250.0, speed_mm_s))
        ramp_mm_s2 = PRODUCTION_MOTOR3_RAMP_MM_S2
        return {
            "travel_mm": travel_mm,
            "nominal_travel_mm": nominal_travel_mm,
            "label_length_compensation_mm": label_length_compensation_mm,
            "first_wickler_travel_mm": first_wickler_travel_mm,
            "speed_mm_s": speed_mm_s,
            "ramp_mm_s2": ramp_mm_s2,
            "wickler_standby_percent": PRODUCTION_WICKLER_STANDBY_PERCENT,
        }

    def _production_esp(
        self,
        line: str,
        *,
        read_timeout_s: float | None = None,
        read_limit: int = 8192,
        priority: bool = False,
    ) -> str:
        command = str(line or "").strip()
        if bool(getattr(self.cfg, "esp_simulation", False)):
            response = f"SIM_{command}"
            self.logs.log("esp-plc", "info", f"production motion simulation: {command} -> {response}")
            return response
        client = EspPlcClient(
            self.cfg.esp_host,
            self.cfg.esp_port,
            timeout_s=self.cfg.get_float("esp_connect_timeout_s", 1.5),
        )
        response = client.exchange_line(
            command,
            read_timeout_s=read_timeout_s or self.cfg.get_float("esp_command_timeout_s", 8.0),
            read_limit=max(512, int(read_limit or 8192)),
            priority=priority,
        )
        self.logs.log("esp-plc", "info", f"production motion: {command} -> {response}")
        if str(response or "").strip().upper().startswith("NAK"):
            raise RuntimeError(f"ESP rejected '{command}': {response}")
        return response

    def _production_esp_retry(
        self,
        line: str,
        *,
        read_timeout_s: float | None = None,
        attempts: int = 2,
        settle_s: float = 0.2,
        read_limit: int = 8192,
        priority: bool = False,
    ) -> str:
        command = str(line or "").strip()
        errors: list[str] = []
        for attempt in range(1, max(1, int(attempts)) + 1):
            try:
                return self._production_esp(
                    command,
                    read_timeout_s=read_timeout_s,
                    read_limit=read_limit,
                    priority=priority,
                )
            except Exception as exc:
                errors.append(repr(exc))
                if attempt >= max(1, int(attempts)):
                    break
                self.logs.log(
                    "esp-plc",
                    "warning",
                    f"production motion retry {attempt + 1}/{max(1, int(attempts))}: {command} after {repr(exc)}",
                )
                time.sleep(max(0.0, float(settle_s)) * attempt)
        raise RuntimeError(f"ESP command failed after {max(1, int(attempts))} attempts: {command}; errors={errors}")

    def _production_status_after_start_error(self, start_command: str, exc: Exception) -> tuple[bool, str]:
        errors: list[str] = [repr(exc)]
        try:
            for attempt in range(1, 6):
                try:
                    response = self._production_esp("PROCESS PRODUCTION STATUS?", read_timeout_s=5.0, priority=True)
                    text = str(response or "").strip()
                    payload = json.loads(text.removeprefix("JSON ").strip())
                    if bool(payload.get("running")):
                        self.logs.log(
                            "esp-plc",
                            "warning",
                            f"production start ACK missing, ESP runner is running after {repr(exc)}",
                        )
                        return True, f"ACK_PROCESS_PRODUCTION_START_STATUS_RUNNING after {repr(exc)}"
                    return False, f"{repr(exc)}; status={payload}"
                except Exception as status_exc:
                    errors.append(f"status_attempt_{attempt}:{repr(status_exc)}")
                    time.sleep(0.35 * attempt)
        except Exception as outer_exc:
            errors.append(f"status_outer:{repr(outer_exc)}")
        return False, f"{repr(exc)}; status_errors={errors}; command={start_command}"

    def _sync_esp_machine_state(self, state: int, *, required: bool = False) -> bool:
        try:
            self._production_esp_retry(
                f"SYNC MAS0001={int(state)}",
                read_timeout_s=5.0 if required else 3.0,
                attempts=4 if required else 3,
                settle_s=0.35,
                priority=required,
            )
            return True
        except Exception as exc:
            level = "error" if required else "warning"
            self.logs.log("esp-plc", level, f"MAS0001 sync to ESP failed for state {state}: {repr(exc)}")
            if required:
                raise
            return False

    @staticmethod
    def _tto_printer_settled_codes(target_code: str) -> set[str]:
        target = str(target_code or "").strip()
        if target == TTO_PRINTER_OFFLINE_CODE:
            return {"0", "1", "2"}
        if target == TTO_PRINTER_ONLINE_CODE:
            return {"3", "4", "5"}
        return {target}

    def _tto_printer_state_sync_plan(self, machine_state: int, param_map: dict[str, str]) -> dict[str, Any]:
        target_code = TTO_PRINTER_ONLINE_CODE if int(machine_state or 0) == 5 else TTO_PRINTER_OFFLINE_CODE
        result: dict[str, Any] = {
            "pkey": TTO_PRINTER_STATE_PKEY,
            "machine_state": int(machine_state or 0),
            "target_code": target_code,
        }
        params = dict(param_map or {})
        if _safe_int(params.get("MAP0016", self.params.get_effective_value("MAP0016")), 0) != 0:
            return {**result, "ok": True, "skipped": "tto_not_selected"}
        if _truthy(params.get("MAP0035", self.params.get_effective_value("MAP0035"))):
            return {**result, "ok": True, "skipped": "tto_print_bypass_active"}
        if bool(getattr(self.cfg, "vj6530_simulation", True)):
            return {**result, "ok": True, "skipped": "vj6530_simulation"}
        if not str(getattr(self.cfg, "vj6530_host", "") or "").strip() or _safe_int(
            getattr(self.cfg, "vj6530_port", 0),
            0,
        ) <= 0:
            return {**result, "ok": True, "skipped": "vj6530_not_configured"}
        if not self.params.get_meta(TTO_PRINTER_STATE_PKEY):
            return {**result, "ok": True, "skipped": "tts0001_missing"}
        mapping = self.params.get_device_map(TTO_PRINTER_STATE_PKEY)
        if not str((mapping or {}).get("zbc_mapping") or "").strip():
            return {**result, "ok": True, "skipped": "tts0001_zbc_mapping_missing"}
        cached = str(self.params.get_effective_value(TTO_PRINTER_STATE_PKEY) or "").strip()
        result["cached_code"] = cached
        if target_code == TTO_PRINTER_OFFLINE_CODE and cached in self._tto_printer_settled_codes(target_code):
            return {**result, "ok": True, "skipped": "already_offline"}
        return result

    def _sync_tto_printer_for_machine_state(
        self,
        machine_state: int,
        param_map: dict[str, str],
        *,
        reason: str,
        required: bool = False,
    ) -> dict[str, Any]:
        plan = self._tto_printer_state_sync_plan(machine_state, param_map)
        if plan.get("skipped"):
            return plan
        acquired = _TTO_PRINTER_STATE_LOCK.acquire(blocking=bool(required))
        if not acquired:
            return {**plan, "ok": True, "skipped": "sync_in_progress"}
        target_code = str(plan.get("target_code") or "")
        try:
            response = DeviceBridge(self.cfg, self.params, self.logs).execute(
                "vj6530",
                TTO_PRINTER_STATE_PKEY,
                "TTS",
                "write",
                target_code,
                actor="esp32",
            )
            response_text = str(response or "").strip()
            actual = response_text.split("=", 1)[1].strip() if "=" in response_text else ""
            ok = response_text.upper().startswith(f"ACK_{TTO_PRINTER_STATE_PKEY}=".upper()) and actual in (
                self._tto_printer_settled_codes(target_code)
            )
            if not ok:
                raise RuntimeError(f"TTO printer state write failed: target={target_code}, response={response_text!r}")
            result = {
                **plan,
                "ok": True,
                "reason": str(reason or ""),
                "response": response_text,
                "actual_code": actual,
            }
            if (
                target_code == TTO_PRINTER_ONLINE_CODE
                and _truthy(param_map.get("MAP0079", self.params.get_effective_value("MAP0079")))
            ):
                result["laser_parallel_start"] = self._pulse_io_output(
                    f"esp32_plc58__{LASER_START_PIN.replace('.', '_')}",
                    high_s=LASER_START_PULSE_HIGH_S,
                    source="laser-parallel-tto-online",
                )
            self._record_event(
                "tto_printer_state_sync",
                "info",
                (
                    "TTO Drucker Online gesetzt"
                    if target_code == TTO_PRINTER_ONLINE_CODE
                    else "TTO Drucker Offline gesetzt"
                ),
                result,
            )
            return result
        except Exception as exc:
            result = {
                **plan,
                "ok": False,
                "reason": str(reason or ""),
                "error": str(exc),
            }
            self._record_event(
                "tto_printer_state_sync_failed",
                "error" if required else "warning",
                (
                    "TTO Drucker konnte nicht online gesetzt werden"
                    if target_code == TTO_PRINTER_ONLINE_CODE
                    else "TTO Drucker konnte nicht offline gesetzt werden"
                ),
                result,
            )
            if required:
                raise
            return result
        finally:
            _TTO_PRINTER_STATE_LOCK.release()

    def _queue_tto_printer_state_sync(
        self,
        machine_state: int,
        param_map: dict[str, str],
        *,
        reason: str,
    ) -> dict[str, Any]:
        plan = self._tto_printer_state_sync_plan(machine_state, param_map)
        if plan.get("skipped"):
            return plan
        params = dict(param_map or {})

        def _worker():
            self._sync_tto_printer_for_machine_state(
                int(machine_state or 0),
                params,
                reason=reason,
                required=False,
            )

        thread = threading.Thread(target=_worker, name="mas004-tto-printer-state-sync", daemon=True)
        thread.start()
        return {**plan, "ok": True, "queued": True, "reason": str(reason or "")}

    def _production_esp_sync_values(self, param_map: dict[str, str]) -> dict[str, str]:
        values: dict[str, str] = {}
        for key in PRODUCTION_ESP_SYNC_KEYS:
            if key == "MAP0067" and not _truthy(param_map.get("MAP0036", "0")):
                continue
            if key == "MAP0068" and not _truthy(param_map.get("MAP0037", "0")):
                continue
            if key in {"MAP0069", "MAP0070"} and not _truthy(param_map.get("MAP0035", "0")):
                continue
            value = str(param_map.get(key, self.params.get_effective_value(key) or "")).strip()
            if not value:
                continue
            values[key] = value
        return values

    def _production_esp_sync_reference(self, state_info: dict[str, Any]) -> dict[str, str]:
        setup_info = dict((state_info or {}).get("setup") or {})
        setup_result = setup_info.get("last_result") if isinstance(setup_info.get("last_result"), dict) else {}
        production_info = dict((state_info or {}).get(PRODUCTION_RUNTIME_INFO_KEY) or {})
        candidates = (
            setup_result.get("production_param_sync_values") if isinstance(setup_result, dict) else None,
            production_info.get("production_param_sync_values"),
            (production_info.get("last_start") or {}).get("synced_param_values")
            if isinstance(production_info.get("last_start"), dict)
            else None,
        )
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate:
                return {str(key): str(value) for key, value in candidate.items()}
        return {}

    @staticmethod
    def _normalize_esp_param_value(value: Any) -> str:
        text = str(value if value is not None else "").strip()
        if re.fullmatch(r"[+-]?\d+", text):
            return str(int(text, 10))
        if re.fullmatch(r"[+-]?\d+\.0+", text):
            return str(int(float(text)))
        return text

    @staticmethod
    def _parse_esp_param_readback(key: str, response: str) -> str:
        text = str(response or "").strip()
        if text.upper().startswith("JSON "):
            try:
                payload = json.loads(text.removeprefix("JSON ").strip())
            except Exception as exc:
                raise RuntimeError(f"{key} readback JSON invalid: {text[:160]!r}") from exc
            for candidate in (key, key.lower(), "value"):
                if candidate in payload:
                    return str(payload.get(candidate))
            raise RuntimeError(f"{key} readback JSON missing value: {text[:160]!r}")
        prefix = f"{key}="
        if text.upper().startswith(prefix.upper()):
            return text.split("=", 1)[1].strip()
        raise RuntimeError(f"{key} readback unexpected response: {text[:160]!r}")

    def _sync_production_params_to_esp(
        self,
        param_map: dict[str, str],
        *,
        previous_values: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        values = self._production_esp_sync_values(param_map)
        previous = {str(key): str(value) for key, value in (previous_values or {}).items()}
        synced: list[str] = []
        skipped: list[str] = []
        forced: list[str] = []
        readback: dict[str, str] = {}
        readback_errors: dict[str, str] = {}
        required = tuple(key for key in PRODUCTION_ESP_START_READBACK_KEYS if key in values)
        for key, value in values.items():
            force = key in PRODUCTION_ESP_START_READBACK_KEYS
            if force:
                forced.append(key)
            if not force and previous.get(key) == value:
                skipped.append(key)
                continue
            self._production_esp_retry(f"SYNC {key}={value}", read_timeout_s=5.0, attempts=3, priority=force)
            synced.append(key)
        if not bool(getattr(self.cfg, "esp_simulation", False)):
            mismatches: list[dict[str, str]] = []
            for key in required:
                expected = self._normalize_esp_param_value(values.get(key, ""))
                actual = ""
                for attempt in range(1, 3):
                    if attempt > 1:
                        self._production_esp_retry(
                            f"SYNC {key}={values[key]}",
                            read_timeout_s=5.0,
                            attempts=3,
                            settle_s=0.25,
                            priority=True,
                        )
                    try:
                        response = self._production_esp_retry(
                            f"{key}=?",
                            read_timeout_s=5.0,
                            attempts=3,
                            settle_s=0.25,
                            priority=True,
                        )
                        actual = self._normalize_esp_param_value(self._parse_esp_param_readback(key, response))
                        readback[key] = actual
                        if actual == expected:
                            readback_errors.pop(key, None)
                            break
                    except Exception as exc:
                        readback_errors[key] = repr(exc)
                        actual = ""
                if actual != expected:
                    mismatches.append(
                        {
                            "key": key,
                            "expected": expected,
                            "actual": actual or readback_errors.get(key, ""),
                        }
                    )
            if mismatches:
                details = ", ".join(
                    f"{item['key']} expected {item['expected']} got {item['actual']}" for item in mismatches
                )
                raise RuntimeError(f"ESP production parameter readback mismatch before START: {details}")
        return {
            "synced": synced,
            "skipped": skipped,
            "forced": forced,
            "readback": readback,
            "readback_errors": readback_errors,
            "values": values,
        }

    def _verify_wickler_production_state(
        self,
        role: str,
        state: dict[str, Any],
        *,
        require_indexed_mode: bool | None = False,
    ) -> dict[str, Any]:
        telemetry = dict((state or {}).get("telemetry") or {})
        drive = dict((state or {}).get("drive") or {})
        values = dict((state or {}).get("values") or {})
        device = dict((state or {}).get("device") or {})
        mode = str(telemetry.get("modeLabel") or "")
        mode_css = str(telemetry.get("modeCss") or "")
        communication_error = bool(device) and device.get("reachable") is False and not bool(device.get("simulation"))
        device_error = str(device.get("error") or "").strip()
        try:
            wipe_percent = float(telemetry.get("wipePercent"))
        except Exception:
            wipe_percent = 50.0
        errors: list[str] = []
        mae_keys: set[str] = set()
        role_keys = WICKLER_ROLE_DANCER_MAE.get(role, {})
        requires_calibration = _truthy(telemetry.get("requiresCalibration")) if "requiresCalibration" in telemetry else False
        calibrated = _truthy(telemetry.get("calibrated")) if "calibrated" in telemetry else True
        indexed_enabled = bool((state.get("master") or {}).get("indexedModeEnabled")) or bool(
            telemetry.get("indexedModeEnabled")
        )
        if communication_error:
            errors.append("communication_error" + (f": {device_error}" if device_error else ""))
        elif mode not in {"Bereit", "Warnung"}:
            errors.append(f"mode={mode or '-'}")
        if not communication_error and (requires_calibration or not calibrated):
            errors.append("Wippe nicht eingemessen")
            if role_keys.get("blocked"):
                mae_keys.add(role_keys["blocked"])
        if not communication_error and mode_css.lower() == "fault":
            errors.append(f"modeCss={mode_css}")
        if not communication_error and bool(telemetry.get("externalStopActive")):
            errors.append("externalStopActive")
        if not communication_error and drive.get("online") is False:
            errors.append("drive offline")
        if not communication_error and bool(drive.get("alarm")):
            errors.append(f"drive alarm {drive.get('alarmCode')}")
        if not communication_error and require_indexed_mode is not True and not indexed_enabled and drive.get("continuousModeReady") is False:
            errors.append("continuousModeReady=false")
        if not communication_error and drive.get("lastCommandOk") is False:
            errors.append("lastCommandOk=false")
        if not communication_error and (wipe_percent <= PRODUCTION_WICKLER_MIN_PERCENT or wipe_percent >= PRODUCTION_WICKLER_MAX_PERCENT):
            errors.append(f"Wippe {wipe_percent:.1f}%")
            if wipe_percent <= PRODUCTION_WICKLER_MIN_PERCENT and role_keys.get("low"):
                mae_keys.add(role_keys["low"])
            if wipe_percent >= PRODUCTION_WICKLER_MAX_PERCENT and role_keys.get("high"):
                mae_keys.add(role_keys["high"])
        for key, text in (
            ("maeLow", "Taenzerarm zu tief"),
            ("maeHigh", "Taenzerarm zu hoch"),
            ("maeBlocked", "Taenzerarm blockiert"),
        ):
            if not communication_error and bool(values.get(key)):
                errors.append(text)
                if key == "maeLow" and role_keys.get("low"):
                    mae_keys.add(role_keys["low"])
                elif key == "maeHigh" and role_keys.get("high"):
                    mae_keys.add(role_keys["high"])
                elif key == "maeBlocked" and role_keys.get("blocked"):
                    mae_keys.add(role_keys["blocked"])
        indexed_errors: list[str] = []
        if not communication_error and require_indexed_mode is True and not indexed_enabled:
            indexed_errors.append("indexedModeEnabled=false")
        indexed_command_seq = _safe_int(telemetry.get("indexedCommandSeq"), 0)
        if not communication_error and require_indexed_mode is True and indexed_command_seq <= 0:
            indexed_errors.append("indexedCommandSeq=0")
        if not communication_error and require_indexed_mode is False and indexed_enabled:
            indexed_errors.append("indexedModeEnabled=true")
        if not communication_error and require_indexed_mode is None and indexed_enabled and indexed_command_seq <= 0:
            indexed_errors.append("indexedModeEnabled=true,indexedCommandSeq=0")
        return {
            "role": role,
            "ok": not errors and not indexed_errors,
            "errors": errors + indexed_errors,
            "mode": mode,
            "calibrated": calibrated,
            "requires_calibration": requires_calibration,
            "wipe_percent": wipe_percent,
            "drive_ready": drive.get("ready"),
            "drive_move": drive.get("move"),
            "drive_alarm": drive.get("alarm"),
            "continuous_mode_ready": drive.get("continuousModeReady"),
            "indexed_mode_enabled": indexed_enabled,
            "indexed_command_seq": indexed_command_seq,
            "indexed_move_active": bool(telemetry.get("indexedMoveActive")) or bool(drive.get("move")),
            "indexed_mode_required": bool(require_indexed_mode),
            "communication_error": communication_error,
            "device_reachable": device.get("reachable"),
            "device_error": device_error,
            "mae_keys": sorted(mae_keys),
        }

    def _production_wickler_indexed_payload(self, plan: dict[str, Any], travel_mm: float) -> dict[str, str]:
        safe_travel_mm = max(1.0, min(PRODUCTION_WICKLER_INDEXED_MAX_TRAVEL_MM, float(travel_mm)))
        return {
            "indexedModeEnabled": "1",
            "indexedTravelMm": f"{safe_travel_mm:.3f}",
            "indexedSpeedMmS": f"{float(plan['speed_mm_s']):.3f}",
            "indexedAccelMmS2": f"{float(plan['ramp_mm_s2']):.3f}",
            "indexedDecelMmS2": f"{float(plan['ramp_mm_s2']):.3f}",
            "indexedStandbyPercent": f"{float(plan['wickler_standby_percent']):.1f}",
        }

    def _prepare_production_wicklers_continuous(
        self,
        plan: dict[str, Any],
        *,
        reason: str = "production_start_continuous_feed",
        timeout_s: float = 5.0,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        master_payload = {
            "indexedModeEnabled": "0",
            "indexedSpeedMmS": f"{float(plan['speed_mm_s']):.3f}",
            "indexedAccelMmS2": f"{float(plan['ramp_mm_s2']):.3f}",
            "indexedDecelMmS2": f"{float(plan['ramp_mm_s2']):.3f}",
            "indexedStandbyPercent": f"{float(plan['wickler_standby_percent']):.1f}",
        }
        for role in ("unwinder", "rewinder"):
            client = SmartWicklerClient(self.cfg, role)
            if not client.available():
                if bool(getattr(self.cfg, client.descriptor.simulation_attr, True)):
                    results.append({"role": role, "ok": True, "simulation": True})
                    continue
                raise RuntimeError(f"{client.descriptor.label} endpoint missing")
            master_reply = client.post_master(master_payload, timeout_s=timeout_s)
            if not master_reply.get("ok", True):
                raise RuntimeError(f"{client.descriptor.label} continuous master failed: {master_reply}")
            ready_reply = client.release_for_continuous_motion(timeout_s=timeout_s)
            if not ready_reply.get("ok", True):
                raise RuntimeError(f"{client.descriptor.label} ready failed: {ready_reply}")
            state = client.fetch_state(timeout_s=min(max(0.2, float(timeout_s)), 1.0))
            verify = self._verify_wickler_production_state(role, state, require_indexed_mode=False)
            result = {
                "role": role,
                "reason": reason,
                "mode": "continuous",
                "ready": ready_reply,
                "master": master_reply,
                "verify": verify,
            }
            results.append(result)
            if not verify.get("ok"):
                raise RuntimeError(f"{client.descriptor.label} nicht kontinuierlich produktionsbereit: {verify}")
        return results

    def _wait_production_wickler_indexed_prepared(
        self,
        client: SmartWicklerClient,
        role: str,
        *,
        timeout_s: float,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        deadline = time.monotonic() + max(0.2, float(timeout_s))
        last_state: dict[str, Any] = {}
        last_verify: dict[str, Any] = {}
        while True:
            last_state = client.fetch_state(timeout_s=min(max(0.2, float(timeout_s)), 1.0))
            last_verify = self._verify_wickler_production_state(role, last_state, require_indexed_mode=True)
            if bool(last_verify.get("ok")) and not bool(last_verify.get("indexed_move_active")):
                return last_state, last_verify
            if time.monotonic() >= deadline:
                return last_state, last_verify
            time.sleep(0.05)

    def _prepare_production_wicklers(
        self,
        plan: dict[str, Any],
        *,
        travel_mm: float | None = None,
        travel_source: str = "explicit",
        reason: str = "production_start",
        timeout_s: float = 5.0,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        planned_travel_mm = float(travel_mm if travel_mm is not None else plan.get("first_wickler_travel_mm", plan["travel_mm"]))
        master_payload = self._production_wickler_indexed_payload(plan, planned_travel_mm)
        for role in ("unwinder", "rewinder"):
            client = SmartWicklerClient(self.cfg, role)
            if not client.available():
                if bool(getattr(self.cfg, client.descriptor.simulation_attr, True)):
                    results.append({"role": role, "ok": True, "simulation": True})
                    continue
                raise RuntimeError(f"{client.descriptor.label} endpoint missing")
            master_reply = client.post_master(master_payload, timeout_s=timeout_s)
            if not master_reply.get("ok", True):
                raise RuntimeError(f"{client.descriptor.label} indexed master failed: {master_reply}")
            state, verify = self._wait_production_wickler_indexed_prepared(
                client,
                role,
                timeout_s=timeout_s,
            )
            telemetry = dict((state or {}).get("telemetry") or {})
            master = dict((state or {}).get("master") or {})
            indexed_plan = {
                "base_travel_mm": planned_travel_mm,
                "master_travel_mm": _safe_float(master.get("indexedTravelMm"), planned_travel_mm),
                "prepared_travel_mm": _safe_float(telemetry.get("indexedTravelMm"), planned_travel_mm),
                "prepared_trim_mm": _safe_float(telemetry.get("indexedTrimMm"), 0.0),
                "next_trim_mm": _safe_float(telemetry.get("indexedNextTrimMm"), 0.0),
                "trim_state_mm": _safe_float(telemetry.get("indexedTrimStateMm"), 0.0),
                "trim_delta_mm": _safe_float(telemetry.get("indexedTrimDeltaMm"), 0.0),
                "role_error_percent": _safe_float(telemetry.get("indexedRoleErrorPercent"), 0.0),
                "trim_mm_per_percent": _safe_float(telemetry.get("indexedTrimMmPerPercent"), 0.0),
                "prepare_frozen": _truthy(telemetry.get("indexedPrepareFrozen")),
                "wipe_percent": _safe_float(telemetry.get("wipePercent"), 0.0),
                "standby_percent": _safe_float(
                    telemetry.get("indexedStandbyPercent"),
                    float(plan.get("wickler_standby_percent", PRODUCTION_WICKLER_STANDBY_PERCENT)),
                ),
                "command_seq": _safe_int(telemetry.get("indexedCommandSeq"), 0),
            }
            result = {
                "ok": bool(verify.get("ok")),
                "role": role,
                "reason": reason,
                "travel_mm": planned_travel_mm,
                "travel_source": travel_source,
                "indexed_plan": indexed_plan,
                "ready": {"ok": True, "unchanged": True, "source": "hardware_start_only"},
                "master": master_reply,
                "verify": verify,
            }
            results.append(result)
            self.logs.log(
                "machine",
                "info",
                (
                    f"Wickler-Takt vorbereitet {client.descriptor.label}: "
                    f"Basis {indexed_plan['base_travel_mm']:.3f}mm, "
                    f"Quelle {travel_source}, "
                    f"Wippe {indexed_plan['wipe_percent']:.1f}%, "
                    f"Korrektur {indexed_plan['prepared_trim_mm']:.3f}mm "
                    f"(naechste {indexed_plan['next_trim_mm']:.3f}mm, "
                    f"Regler {indexed_plan['trim_state_mm']:.3f}mm, "
                    f"Delta {indexed_plan['trim_delta_mm']:.3f}mm), "
                    f"effektiv {indexed_plan['prepared_travel_mm']:.3f}mm, "
                    f"Freeze {1 if indexed_plan['prepare_frozen'] else 0}, "
                    f"Seq {indexed_plan['command_seq']}"
                ),
            )
            if not verify.get("ok"):
                raise RuntimeError(f"{client.descriptor.label} nicht produktionsbereit: {verify}")
        return results

    def _prepare_next_production_wickler_takt(self, *, label_no: int, reason: str) -> dict[str, Any]:
        try:
            machine_state = _safe_int(self.params.get_effective_value("MAS0001"), 0)
            if machine_state != 5:
                return {"ok": True, "skipped": f"machine_state={machine_state}"}
            param_map = self._param_values_by_prefix(("MAP", "MAS", "MAE", "MAW"))
            plan = self._production_motion_plan(param_map, build_format_plan(param_map))
            base_travel_mm, base_source = self._production_wickler_base_travel(plan)
            results = self._prepare_production_wicklers(
                plan,
                travel_mm=base_travel_mm,
                travel_source=base_source,
                reason=reason,
                timeout_s=1.2,
            )
            return {
                "ok": all(bool(item.get("ok", True)) for item in results),
                "label_no": int(label_no),
                "base_travel_mm": base_travel_mm,
                "base_source": base_source,
                "results": results,
            }
        except Exception as exc:
            self.logs.log("machine", "warning", f"Folge-Wicklertakt konnte nicht vorbereitet werden: {exc}")
            return {"ok": False, "label_no": int(label_no), "error": repr(exc)}

    def _production_wickler_verifications(
        self,
        *,
        timeout_s: float = 2.0,
        require_indexed_mode: bool | None = None,
    ) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        errors: list[str] = []
        for role in ("unwinder", "rewinder"):
            client = SmartWicklerClient(self.cfg, role)
            if not client.available():
                if bool(getattr(self.cfg, client.descriptor.simulation_attr, True)):
                    results.append({"role": role, "ok": True, "simulation": True})
                    continue
                error = f"{client.descriptor.label} endpoint missing"
                results.append({"role": role, "ok": False, "error": error})
                errors.append(error)
                continue
            try:
                state = client.fetch_state(timeout_s=timeout_s)
                verify = self._verify_wickler_production_state(
                    role,
                    state,
                    require_indexed_mode=require_indexed_mode,
                )
                ok = bool(verify.get("ok"))
                results.append({"role": role, "ok": ok, "verify": verify})
                if not ok:
                    errors.append(f"{client.descriptor.label}: {', '.join(str(e) for e in verify.get('errors') or ['not_ready'])}")
            except Exception as exc:
                error = f"{client.descriptor.label}: {repr(exc)}"
                results.append({
                    "role": role,
                    "ok": False,
                    "error": error,
                    "communication_error": True,
                    "verify": {
                        "role": role,
                        "ok": False,
                        "errors": [error],
                        "communication_error": True,
                        "device_reachable": False,
                        "device_error": repr(exc),
                        "mae_keys": [],
                    },
                })
                errors.append(error)
        return {"ok": not errors, "results": results, "errors": errors}

    @staticmethod
    def _wickler_monitor_item_is_communication_error(item: dict[str, Any]) -> bool:
        verify = dict((item or {}).get("verify") or {})
        return bool((item or {}).get("communication_error")) or bool(verify.get("communication_error"))

    def _wickler_monitor_is_communication_only(self, monitor: dict[str, Any]) -> bool:
        failed = [dict(item or {}) for item in (monitor or {}).get("results") or [] if not bool((item or {}).get("ok", True))]
        return bool(failed) and all(self._wickler_monitor_item_is_communication_error(item) for item in failed)

    @staticmethod
    def _wickler_monitor_signature(monitor: dict[str, Any]) -> str:
        parts: list[str] = []
        for item in (monitor or {}).get("results") or []:
            if bool((item or {}).get("ok", True)):
                continue
            role = str((item or {}).get("role") or "?")
            verify = dict((item or {}).get("verify") or {})
            errors = verify.get("errors") or [(item or {}).get("error") or "not_ready"]
            parts.append(role + ":" + ",".join(str(error) for error in errors))
        return "|".join(sorted(parts))

    def _latch_wickler_monitor_faults(self, monitor: dict[str, Any]) -> list[str]:
        latched: list[str] = []
        for item in monitor.get("results") or []:
            verify = dict((item or {}).get("verify") or {})
            for pkey in verify.get("mae_keys") or []:
                key = str(pkey or "").strip().upper()
                if key not in WICKLER_DANCER_ERROR_KEYS:
                    continue
                self.params.apply_device_value(key, "1", promote_default=True)
                self._notify_microtom(key, "1", dedupe_key=f"machine:{key}")
                latched.append(key)
        return sorted(set(latched))

    @staticmethod
    def _wickler_state_is_calibrating(telemetry: dict[str, Any]) -> bool:
        mode = str(telemetry.get("modeLabel") or "").strip().lower()
        return mode in {"einmessen", "calibrate", "calibration", "kalibrieren"}

    @staticmethod
    def _wickler_state_is_calibrated(telemetry: dict[str, Any]) -> bool:
        if "requiresCalibration" in telemetry and _truthy(telemetry.get("requiresCalibration")):
            return False
        if "calibrated" in telemetry:
            return _truthy(telemetry.get("calibrated"))
        return True

    def _monitor_wickler_hard_endstops(
        self,
        info: dict[str, Any],
        machine_state: int,
        ts: float,
    ) -> Optional[dict[str, Any]]:
        monitor_info = dict(info.get("wickler_hard_endstop_monitor") or {})
        if int(machine_state or 0) not in WICKLER_DANCER_MONITOR_STATES:
            info["wickler_hard_endstop_monitor"] = {
                **monitor_info,
                "active": False,
                "last_state": int(machine_state or 0),
            }
            return None
        last_ts = _safe_float(monitor_info.get("last_ts"), 0.0)
        if last_ts > 0.0 and (float(ts) - last_ts) < WICKLER_HARD_ENDSTOP_MONITOR_INTERVAL_S:
            return None

        results: list[dict[str, Any]] = []
        latched: list[str] = []
        faults: list[str] = []
        for role in ("unwinder", "rewinder"):
            client = SmartWicklerClient(self.cfg, role)
            role_keys = WICKLER_ROLE_DANCER_MAE.get(role, {})
            item: dict[str, Any] = {"role": role}
            if not client.available():
                item["ok"] = True
                item["skipped"] = "simulation_or_endpoint_missing"
                results.append(item)
                continue
            try:
                state = client.fetch_state(timeout_s=0.6)
            except Exception as exc:
                item["ok"] = True
                item["skipped"] = "state_read_failed"
                item["error"] = repr(exc)
                results.append(item)
                continue

            telemetry = dict((state or {}).get("telemetry") or {})
            drive = dict((state or {}).get("drive") or {})
            values = dict((state or {}).get("values") or {})
            item["mode"] = telemetry.get("modeLabel")
            item["fault"] = telemetry.get("faultReason")
            item["calibrated"] = telemetry.get("calibrated")
            item["requires_calibration"] = telemetry.get("requiresCalibration")
            item["drive_alarm"] = drive.get("alarm")
            try:
                wipe_percent = float(telemetry.get("wipePercent"))
            except Exception:
                wipe_percent = 50.0
            item["wipe_percent"] = wipe_percent

            if self._wickler_state_is_calibrating(telemetry):
                item["ok"] = True
                item["skipped"] = "calibrating"
                results.append(item)
                continue
            if not self._wickler_state_is_calibrated(telemetry):
                item["ok"] = True
                item["skipped"] = "requires_calibration"
                results.append(item)
                continue

            pkey: str | None = None
            text: str | None = None
            if wipe_percent <= WICKLER_HARD_ENDSTOP_LOW_PERCENT:
                pkey = role_keys.get("low")
                text = f"Wippe unten {wipe_percent:.1f}%"
            elif wipe_percent >= WICKLER_HARD_ENDSTOP_HIGH_PERCENT:
                pkey = role_keys.get("high")
                text = f"Wippe oben {wipe_percent:.1f}%"
            elif bool(values.get("maeLow")):
                pkey = role_keys.get("low")
                text = "Taenzerarm zu tief"
            elif bool(values.get("maeHigh")):
                pkey = role_keys.get("high")
                text = "Taenzerarm zu hoch"
            elif bool(values.get("maeBlocked")):
                pkey = role_keys.get("blocked")
                text = "Taenzerarm blockiert"

            if pkey and text:
                item["ok"] = False
                item["mae_key"] = pkey
                item["error"] = text
                faults.append(f"{client.descriptor.label}: {text}")
                self.params.apply_device_value(pkey, "1", promote_default=True)
                self._notify_microtom(pkey, "1", dedupe_key=f"machine:{pkey}")
                latched.append(pkey)
            else:
                item["ok"] = True
            results.append(item)

        monitor = {
            "ok": not faults,
            "active": True,
            "ts": float(ts),
            "machine_state": int(machine_state or 0),
            "results": results,
            "latched_mae": sorted(set(latched)),
            "faults": faults,
        }
        monitor_info.update(monitor)
        monitor_info["last_ts"] = float(ts)
        signature = "|".join(sorted(faults))
        if faults:
            self.params.apply_device_value("MAS0028", "1", promote_default=True)
            self._notify_microtom("MAS0028", "1", dedupe_key="machine:MAS0028")
            if signature and signature != str(monitor_info.get("last_fault_signature") or ""):
                self._record_event(
                    "wickler_hard_endstop_fault",
                    "error",
                    "Wickler-Wippe im harten Endbereich: " + "; ".join(faults),
                    monitor,
                )
                self.logs.log("machine", "error", "Wickler-Wippe im harten Endbereich: " + "; ".join(faults))
            monitor_info["last_fault_signature"] = signature
            monitor_info["last_fault_ts"] = float(ts)
        elif not signature:
            monitor_info.pop("last_fault_signature", None)
        info["wickler_hard_endstop_monitor"] = monitor_info
        return monitor

    def _clear_setup_uncalibrated_wickler_latches(
        self,
        machine_state: int,
        monitor: Optional[dict[str, Any]],
        param_map: dict[str, str],
    ) -> bool:
        if int(machine_state or 0) not in (2, 3):
            return False
        items = list((monitor or {}).get("results") or [])
        if not items:
            items = self._setup_uncalibrated_wickler_status_items()
        changed = False
        uncalibrated_roles: set[str] = set()
        for item in items:
            role = str((item or {}).get("role") or "")
            role_keys = WICKLER_ROLE_DANCER_MAE.get(role, {})
            if not role_keys:
                continue
            requires_calibration = (
                str((item or {}).get("skipped") or "") == "requires_calibration"
                or str((item or {}).get("skipped") or "") == "calibrating"
                or _truthy((item or {}).get("requires_calibration"))
                or _truthy((item or {}).get("calibrated")) is False
            )
            if not requires_calibration:
                continue
            uncalibrated_roles.add(role)
            for key in role_keys.values():
                if key and _truthy(param_map.get(key, "0")):
                    self.params.apply_device_value(key, "0", promote_default=True)
                    param_map[key] = "0"
                    changed = True
        active_mae = {str(key) for key, value in (param_map or {}).items() if str(key).startswith("MAE") and _truthy(value)}
        if uncalibrated_roles and _truthy(param_map.get("MAS0028", "0")) and active_mae.issubset(WICKLER_DANCER_ERROR_KEYS):
            self.params.apply_device_value("MAS0028", "0", promote_default=True)
            param_map["MAS0028"] = "0"
            changed = True
        return changed

    def _setup_uncalibrated_wickler_status_items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for role in ("unwinder", "rewinder"):
            client = SmartWicklerClient(self.cfg, role)
            item: dict[str, Any] = {"role": role}
            if not client.available():
                item["skipped"] = "simulation_or_endpoint_missing"
                item["ok"] = True
                items.append(item)
                continue
            try:
                state = client.fetch_state(timeout_s=0.45)
            except Exception as exc:
                item["skipped"] = "state_read_failed"
                item["error"] = repr(exc)
                item["ok"] = True
                items.append(item)
                continue
            telemetry = dict((state or {}).get("telemetry") or {})
            item["mode"] = telemetry.get("modeLabel")
            item["calibrated"] = telemetry.get("calibrated")
            item["requires_calibration"] = telemetry.get("requiresCalibration")
            if self._wickler_state_is_calibrating(telemetry):
                item["skipped"] = "calibrating"
            elif not self._wickler_state_is_calibrated(telemetry):
                item["skipped"] = "requires_calibration"
            item["ok"] = True
            items.append(item)
        return items

    @staticmethod
    def _parse_esp_json_reply(reply: str) -> dict[str, Any]:
        text = str(reply or "").strip()
        if text.upper().startswith("JSON "):
            text = text[5:].strip()
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError(f"ESP JSON reply is not an object: {reply!r}")
        return payload

    @staticmethod
    def _production_esp_diag_summary(diag: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "active",
            "running",
            "phase",
            "reason",
            "last_error",
            "label_no",
            "labels_printed",
            "position_commanded",
            "position_command_mm",
            "target_mm",
            "error_mm",
            "abs_error_mm",
            "registration_attempts",
            "max_attempts",
            "wickler_ready_accepted",
            "motor_busy",
            "motor_ready",
            "infeed_speed_mm_s",
            "drive_speed_mm_s",
        )
        return {key: diag.get(key) for key in keys if key in diag}

    @staticmethod
    def _production_esp_waits_for_first_wickler_ready(diag: dict[str, Any]) -> bool:
        reason = str(diag.get("reason") or "").strip()
        phase = _safe_int(diag.get("phase"), -1)
        label_no = _safe_int(diag.get("label_no"), 0)
        ready_accepted = bool(diag.get("wickler_ready_accepted"))
        last_error = str(diag.get("last_error") or "").strip()
        return (
            label_no > 0
            and not ready_accepted
            and not last_error
            and (
                reason == "first_print_position_reached_wait_wickler"
                or phase == 9
            )
        )

    @staticmethod
    def _normalize_production_monitor_diag(diag: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(diag or {})
        if "label_no" not in normalized and "current_label_no" in normalized:
            normalized["label_no"] = normalized.get("current_label_no")
        if "position_commanded" not in normalized and "position_issued" in normalized:
            normalized["position_commanded"] = normalized.get("position_issued")
        if "error_mm" not in normalized and "target_error_mm" in normalized:
            normalized["error_mm"] = normalized.get("target_error_mm")
        if "abs_error_mm" not in normalized and "error_mm" in normalized:
            normalized["abs_error_mm"] = abs(_safe_float(normalized.get("error_mm"), 0.0))
        if "active" not in normalized:
            normalized["active"] = True
        if "reason" not in normalized:
            phase = _safe_int(normalized.get("phase"), -1)
            if phase == 5:
                normalized["reason"] = "first_print_position_reached_wait_wickler"
            elif phase == 3:
                normalized["reason"] = "registering"
            elif phase == 2:
                normalized["reason"] = "next_label_print_position"
            elif phase == 4:
                normalized["reason"] = "initial_positioning"
            elif phase == 1:
                normalized["reason"] = "feed"
            elif phase == 9:
                normalized["reason"] = "error"
            else:
                normalized["reason"] = "production_status"
        return normalized

    def _read_production_monitor_diag(self) -> dict[str, Any]:
        try:
            response = self._production_esp_retry(
                "PROCESS PRODUCTION MONITOR?",
                read_timeout_s=0.8,
                read_limit=4096,
                attempts=1,
                settle_s=0.05,
                priority=True,
            )
            return self._normalize_production_monitor_diag(self._parse_esp_json_reply(response))
        except Exception as primary_exc:
            primary_repr = repr(primary_exc)
            primary_upper = primary_repr.upper()
            if (
                "NAK_SYNTAX" not in primary_upper
                and "NAK_UNKNOWN" not in primary_upper
                and "JSONDECODEERROR" not in primary_upper
            ):
                raise
            response = self._production_esp_retry(
                "PROCESS PRODUCTION STATUS?",
                read_timeout_s=0.8,
                read_limit=4096,
                attempts=1,
                settle_s=0.05,
                priority=True,
            )
            return self._normalize_production_monitor_diag(self._parse_esp_json_reply(response))

    def _monitor_active_production_esp(self, production_info: dict[str, Any], ts: float) -> Optional[dict[str, Any]]:
        if bool(getattr(self.cfg, "esp_simulation", False)):
            return None
        last_ts = _safe_float(production_info.get("last_esp_monitor_ts"), 0.0)
        if last_ts > 0.0 and (float(ts) - last_ts) < PRODUCTION_ESP_MONITOR_INTERVAL_S:
            return None
        production_info["last_esp_monitor_ts"] = float(ts)
        try:
            diag = self._read_production_monitor_diag()
        except Exception as exc:
            return self._handle_production_esp_monitor_comm_error(production_info, ts, exc)

        monitor: dict[str, Any] = {
            "ok": True,
            "ts": float(ts),
            "diag": self._production_esp_diag_summary(diag),
        }
        production_info["last_esp_monitor"] = monitor
        production_info.pop("esp_monitor_pending_fault", None)

        last_error = str(diag.get("last_error") or "").strip()
        last_error_benign = last_error.lower() in {"", "reset", "stopped", "completed"}
        active = bool(diag.get("active"))
        running = bool(diag.get("running"))
        if active and not running and last_error and not last_error_benign:
            monitor["ok"] = False
            monitor["fault"] = last_error
            stop_result = self._stop_production_motion(
                reason=f"production_esp_runner_fault:{last_error}",
                target_state=21,
            )
            monitor["stop"] = stop_result
            log_event = self._finalize_production_logging_stop("production_esp_runner_fault")
            if log_event:
                monitor["production_log"] = log_event
            self.params.apply_device_value("MAS0028", "1", promote_default=True)
            self._notify_microtom("MAS0028", "1", dedupe_key="machine:MAS0028")
            self._record_event(
                "production_esp_runner_fault",
                "error",
                f"Produktionslauf gestoppt: ESP-Runner meldet {last_error}",
                monitor,
            )
            return monitor

        if active and running and self._production_esp_waits_for_first_wickler_ready(diag):
            fallback = self._production_first_wickler_ready_diag_fallback(production_info, diag, ts)
            monitor["first_wickler_ready_fallback"] = fallback
            production_info["last_esp_monitor"] = monitor
        return None

    def _handle_production_esp_monitor_comm_error(
        self,
        production_info: dict[str, Any],
        ts: float,
        exc: Exception,
    ) -> Optional[dict[str, Any]]:
        monitor: dict[str, Any] = {
            "ok": False,
            "ts": float(ts),
            "communication_error": True,
            "error": repr(exc),
        }
        pending = dict(production_info.get("esp_monitor_pending_fault") or {})
        same_signature = str(pending.get("signature") or "") == str(monitor["error"])
        count = (_safe_int(pending.get("count"), 0) + 1) if same_signature else 1
        first_ts = _safe_float(pending.get("first_ts"), float(ts)) if same_signature else float(ts)
        monitor["consecutive_failures"] = count
        monitor["required_failures"] = PRODUCTION_ESP_MONITOR_COMM_MAX_MISSES
        monitor["first_failure_ts"] = first_ts
        production_info["last_esp_monitor"] = monitor
        production_info["esp_monitor_pending_fault"] = {
            "kind": "communication",
            "signature": str(monitor["error"]),
            "count": count,
            "first_ts": first_ts,
            "last_ts": float(ts),
            "monitor": monitor,
        }
        if count < PRODUCTION_ESP_MONITOR_COMM_MAX_MISSES:
            self._record_event(
                "production_esp_monitor_transient",
                "warning",
                "ESP-Produktionsdiagnose kurzzeitig fehlgeschlagen; Produktionslauf bleibt aktiv",
                monitor,
            )
            return None

        stop_result = self._stop_production_motion(reason="production_esp_monitor_comm_failed", target_state=21)
        monitor["stop"] = stop_result
        log_event = self._finalize_production_logging_stop("production_esp_monitor_comm_failed")
        if log_event:
            monitor["production_log"] = log_event
        self.params.apply_device_value("MAS0028", "1", promote_default=True)
        self._notify_microtom("MAS0028", "1", dedupe_key="machine:MAS0028")
        self._record_event(
            "production_esp_runner_fault",
            "error",
            "Produktionslauf gestoppt: ESP-Produktionsdiagnose nicht erreichbar",
            monitor,
        )
        return monitor

    def _production_first_wickler_ready_diag_fallback(
        self,
        production_info: dict[str, Any],
        diag: dict[str, Any],
        ts: float,
    ) -> dict[str, Any]:
        label_no = _safe_int(diag.get("label_no"), 0)
        target_abs = _safe_float(diag.get("target_mm"), _safe_float(diag.get("position_command_mm"), 0.0))
        target_error = _safe_float(diag.get("error_mm"), 0.0)
        key = "|".join(
            (
                f"label={label_no}",
                f"target={target_abs:.3f}",
                f"error={target_error:.3f}",
            )
        )
        previous = dict(production_info.get("esp_first_wickler_ready_fallback") or {})
        if (
            str(previous.get("key") or "") == key
            and (float(ts) - _safe_float(previous.get("ts"), 0.0)) < PRODUCTION_ESP_FIRST_READY_FALLBACK_INTERVAL_S
        ):
            return {
                "ok": True,
                "skipped": "cooldown",
                "key": key,
                "label_no": label_no,
                "previous": previous,
            }

        payload = {
            "type": "production_first_print_position_reached",
            "label_no": label_no,
            "target_abs_mm": target_abs,
            "target_error_mm": target_error,
            "infeed_speed_mm_s": _safe_float(diag.get("infeed_speed_mm_s"), 0.0),
            "drive_speed_mm_s": _safe_float(diag.get("drive_speed_mm_s"), 0.0),
            "source": "esp_diag_monitor",
        }
        ready_key = self._first_print_wickler_ready_key(label_no=label_no, payload=payload)
        result: dict[str, Any] = {
            "ok": False,
            "key": key,
            "ready_key": ready_key,
            "label_no": label_no,
            "target_abs_mm": target_abs,
            "target_error_mm": target_error,
            "ts": float(ts),
        }
        production_info["esp_first_wickler_ready_fallback"] = dict(result, status="started")
        self._remember_first_print_wickler_ready_attempt(
            ready_key,
            label_no=label_no,
            payload=payload,
            status="started_diag_fallback",
        )

        wickler_takt = self._prepare_next_production_wickler_takt(
            label_no=label_no,
            reason="esp_diag_first_print_wait_fallback",
        )
        esp_ready: dict[str, Any] = {"ok": False, "skipped": "wickler_takt_not_ready"}
        if bool(wickler_takt.get("ok")):
            try:
                response = self._production_esp_retry(
                    f"PROCESS PRODUCTION WICKLER_READY LABEL_NO={int(label_no)}",
                    read_timeout_s=8.0,
                    attempts=4,
                    settle_s=0.15,
                    priority=True,
                )
                esp_ready = {"ok": True, "response": response}
                self._remember_first_print_wickler_ready_sent(
                    ready_key,
                    label_no=label_no,
                    payload=payload,
                    wickler_takt=wickler_takt,
                    esp_ready=esp_ready,
                )
            except Exception as exc:
                esp_ready = {"ok": False, "error": repr(exc)}
                self.logs.log(
                    "esp-plc",
                    "warning",
                    f"ESP-Diag-Fallback: Wickler-Ready fuer Label {label_no} fehlgeschlagen: {repr(exc)}",
                )

        result.update(
            {
                "ok": bool(wickler_takt.get("ok")) and bool(esp_ready.get("ok")),
                "status": "finished",
                "wickler_takt": wickler_takt,
                "esp_ready": esp_ready,
                "wickler_takt_ok": bool(wickler_takt.get("ok")),
                "esp_ready_ok": bool(esp_ready.get("ok")),
            }
        )
        production_info["esp_first_wickler_ready_fallback"] = dict(result)
        production_info["first_print_wickler_ready_attempt"] = {
            "key": ready_key,
            "label_no": label_no,
            "target_abs_mm": target_abs,
            "target_error_mm": target_error,
            "status": "finished_diag_fallback",
            "ts": now_ts(),
            "wickler_takt_ok": bool(wickler_takt.get("ok")),
            "esp_ready_ok": bool(esp_ready.get("ok")),
        }
        if bool(result["ok"]):
            production_info["first_print_wickler_ready"] = {
                "key": ready_key,
                "label_no": label_no,
                "target_abs_mm": target_abs,
                "target_error_mm": target_error,
                "ts": now_ts(),
                "wickler_takt_ok": True,
                "esp_ready_ok": True,
                "source": "esp_diag_monitor",
            }
        self._remember_first_print_wickler_ready_attempt(
            ready_key,
            label_no=label_no,
            payload=payload,
            status="finished_diag_fallback",
            wickler_takt=wickler_takt,
            esp_ready=esp_ready,
        )
        severity = "info" if result["ok"] else "warning"
        message = (
            f"ESP-Diag-Fallback Wickler-Ready Label {label_no}: "
            f"Wickler ok={int(bool(wickler_takt.get('ok')))}, ESP ok={int(bool(esp_ready.get('ok')))}"
        )
        self._record_event("production_first_print_ready_fallback", severity, message, result)
        return result

    def _monitor_active_production_wicklers(self, production_info: dict[str, Any], ts: float) -> Optional[dict[str, Any]]:
        last_ts = _safe_float(production_info.get("last_wickler_monitor_ts"), 0.0)
        if last_ts > 0.0 and (float(ts) - last_ts) < PRODUCTION_WICKLER_MONITOR_INTERVAL_S:
            return None
        production_info["last_wickler_monitor_ts"] = float(ts)
        monitor = self._production_wickler_verifications(timeout_s=1.0, require_indexed_mode=None)
        monitor["ts"] = float(ts)
        production_info["last_wickler_monitor"] = monitor
        if monitor.get("ok"):
            production_info.pop("wickler_monitor_pending_fault", None)
            return None
        if self._wickler_monitor_is_communication_only(monitor):
            signature = self._wickler_monitor_signature(monitor)
            pending = dict(production_info.get("wickler_monitor_pending_fault") or {})
            same_signature = str(pending.get("signature") or "") == signature
            count = (_safe_int(pending.get("count"), 0) + 1) if same_signature else 1
            first_ts = _safe_float(pending.get("first_ts"), float(ts)) if same_signature else float(ts)
            monitor["communication_only"] = True
            monitor["consecutive_failures"] = count
            monitor["required_failures"] = PRODUCTION_WICKLER_MONITOR_COMM_MAX_MISSES
            monitor["first_failure_ts"] = first_ts
            production_info["wickler_monitor_pending_fault"] = {
                "kind": "communication",
                "signature": signature,
                "count": count,
                "first_ts": first_ts,
                "last_ts": float(ts),
                "monitor": monitor,
            }
            if count < PRODUCTION_WICKLER_MONITOR_COMM_MAX_MISSES:
                self._record_event(
                    "production_wickler_monitor_transient",
                    "warning",
                    "Wickler-Statusabfrage kurzzeitig fehlgeschlagen; Produktionslauf bleibt aktiv",
                    monitor,
                )
                return None
        else:
            production_info.pop("wickler_monitor_pending_fault", None)
        monitor["latched_mae"] = self._latch_wickler_monitor_faults(monitor)
        stop_result = self._stop_production_motion(reason="production_wickler_monitor_failed", target_state=21)
        monitor["stop"] = stop_result
        log_event = self._finalize_production_logging_stop("production_wickler_monitor_failed")
        if log_event:
            monitor["production_log"] = log_event
        self.params.apply_device_value("MAS0028", "1", promote_default=True)
        self._notify_microtom("MAS0028", "1", dedupe_key="machine:MAS0028")
        self._record_event(
            "production_wickler_fault",
            "error",
            "Produktionslauf gestoppt: Wickler nicht mehr im Produktions-Taktfenster",
            monitor,
        )
        return monitor

    def _set_production_wicklers_idle(
        self,
        *,
        target_state: int,
        recover_after_safety: bool = False,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        keep_ready = int(target_state or 0) in (6, 7)
        for role in ("unwinder", "rewinder"):
            client = SmartWicklerClient(self.cfg, role)
            if not client.available():
                if bool(getattr(self.cfg, client.descriptor.simulation_attr, True)):
                    results.append({"role": role, "ok": True, "simulation": True})
                    continue
                results.append({"role": role, "ok": False, "error": "endpoint missing"})
                continue
            item: dict[str, Any] = {"role": role}
            errors: list[str] = []
            try:
                item["indexed_disable"] = client.post_master({"indexedModeEnabled": "0"}, timeout_s=1.0)
                if not item["indexed_disable"].get("ok", True):
                    errors.append(f"indexed_disable rejected: {item['indexed_disable']}")
            except Exception as exc:
                item["indexed_disable_error"] = repr(exc)
                errors.append(f"indexed_disable_error={repr(exc)}")
            if keep_ready and recover_after_safety:
                for mode in ("resetAlarm", "etoRecovery"):
                    try:
                        reply = client.post_mode(mode, timeout_s=1.5)
                        item[mode] = reply
                        if not reply.get("ok", True):
                            errors.append(f"{mode} rejected: {reply}")
                    except Exception as exc:
                        item[f"{mode}_error"] = repr(exc)
                        errors.append(f"{mode}_error={repr(exc)}")
            try:
                item["mode"] = (
                    client.release_for_continuous_motion(timeout_s=1.0)
                    if keep_ready
                    else client.post_mode("stop", timeout_s=1.0)
                )
                if not item["mode"].get("ok", True):
                    errors.append(f"mode rejected: {item['mode']}")
            except Exception as exc:
                item["mode_error"] = repr(exc)
                errors.append(f"mode_error={repr(exc)}")
            if keep_ready:
                try:
                    verify: dict[str, Any] = {"ok": False, "errors": ["not_checked"]}
                    state: dict[str, Any] = {}
                    for attempt in range(1, 5):
                        try:
                            state = client.fetch_state(timeout_s=0.6)
                            verify = self._verify_wickler_production_state(role, state)
                        except Exception as exc:
                            verify = {
                                "ok": False,
                                "errors": [f"fetch_state_error={repr(exc)}"],
                                "attempt": attempt,
                            }
                        verify["attempt"] = attempt
                        if verify.get("ok"):
                            break
                        errors_now = [str(error) for error in verify.get("errors") or []]
                        transient_errors = {
                            "continuousModeReady=false",
                            "mode=Offline",
                            "mode=-",
                            "modeCss=fault",
                            "drive offline",
                        }
                        if not errors_now or any(error.startswith("fetch_state_error=") for error in errors_now):
                            pass
                        elif not all(error in transient_errors for error in errors_now):
                            break
                        time.sleep(0.1)
                    errors_now = [str(error) for error in verify.get("errors") or []]
                    if errors_now == ["continuousModeReady=false"]:
                        telemetry = dict((state or {}).get("telemetry") or {})
                        drive = dict((state or {}).get("drive") or {})
                        if (
                            str(telemetry.get("modeLabel") or "") == "Bereit"
                            and not bool(drive.get("move"))
                            and not bool(drive.get("alarm"))
                            and not bool(telemetry.get("indexedModeEnabled"))
                        ):
                            verify = dict(verify)
                            verify["ok"] = True
                            verify["soft_accepted"] = "idle_ready_continuous_bit_lag"
                    if errors_now == ["indexedModeEnabled=true"]:
                        telemetry = dict((state or {}).get("telemetry") or {})
                        drive = dict((state or {}).get("drive") or {})
                        if (
                            str(telemetry.get("modeLabel") or "") == "Bereit"
                            and not bool(drive.get("move"))
                            and not bool(drive.get("alarm"))
                            and not bool(telemetry.get("indexedMoveActive"))
                        ):
                            verify = dict(verify)
                            verify["ok"] = True
                            verify["soft_accepted"] = "idle_ready_indexed_bit_lag"
                    item["verify"] = verify
                    if not verify.get("ok"):
                        errors.extend(str(error) for error in verify.get("errors") or ["not_ready"])
                except Exception as exc:
                    item["verify_error"] = repr(exc)
                    errors.append(f"verify_error={repr(exc)}")
            item["ok"] = not errors
            if errors:
                item["errors"] = errors
            results.append(item)
        return results

    def _start_production_motion(self, param_map: dict[str, str], format_plan: dict[str, Any]) -> dict[str, Any]:
        started_ts = now_ts()
        if not _PRODUCTION_MOTION_LOCK.acquire(blocking=False):
            return {"ok": False, "error": "production_motion_start_already_running", "started_ts": started_ts}
        try:
            plan = self._production_motion_plan(param_map, format_plan)
            state_info = dict(self._state_row().get("info") or {})
            previous_sync_values = self._production_esp_sync_reference(state_info)
            self._sync_esp_machine_state(5, required=True)
            tto_printer = self._sync_tto_printer_for_machine_state(
                5,
                param_map,
                reason="production_start",
                required=True,
            )
            # Let the ESP consume the MAS0001 transition reset before the real
            # production start, otherwise pollStateTransitions can clear it again.
            time.sleep(0.05)
            sync_info = self._sync_production_params_to_esp(
                param_map,
                previous_values=previous_sync_values,
            )
            self._production_esp_retry("PROCESS WICKLER CANCEL", read_timeout_s=2.0, attempts=2, priority=True)
            self._production_esp_retry("PROCESS PROFILE STOP", read_timeout_s=2.0, attempts=2, priority=True)
            self._production_esp_retry("PROCESS INDEXED STOP", read_timeout_s=2.0, attempts=2, priority=True)
            self._production_esp_retry("PROCESS PRODUCTION STOP", read_timeout_s=2.0, attempts=2, priority=True)
            self._production_esp_retry("PROCESS PRODUCTION_RESET", read_timeout_s=5.0, attempts=2, priority=True)
            self._production_esp_retry("MOTOR 3 RESET_ALARM", read_timeout_s=5.0, attempts=2, priority=True)
            self._production_esp_retry("MOTOR 3 RECOVER_ETO", read_timeout_s=5.0, attempts=2, priority=True)
            motor3_zero = self._zero_motor3_for_production_start()
            self._production_esp_retry(
                "MOTOR 3 SET "
                f"speed_mm_s={float(plan['speed_mm_s']):.3f} "
                f"accel_mm_s2={float(plan['ramp_mm_s2']):.3f} "
                f"decel_mm_s2={float(plan['ramp_mm_s2']):.3f}",
                read_timeout_s=5.0,
                attempts=2,
                priority=True,
            )
            wicklers = self._prepare_production_wicklers_continuous(plan)
            # The ESP command endpoint is single-client and the production start
            # immediately touches the AZD bus. Give the freshly prepared Wickler
            # HTTP writes and the ESP parameter sync a short quiet window, then
            # verify the command channel once before the critical START.
            time.sleep(0.05)
            self._production_esp_retry(
                "PROCESS PRODUCTION STATUS?",
                read_timeout_s=1.2,
                attempts=3,
                settle_s=0.1,
                priority=True,
            )
            setup_info = dict(state_info.get("setup") or {})
            setup_result = setup_info.get("last_result") if isinstance(setup_info.get("last_result"), dict) else {}
            quick_band_break_bypass = quick_setup_band_break_bypass_active(state_info)
            start_command = (
                "PROCESS PRODUCTION START "
                f"SPEED_MM_S={float(plan['speed_mm_s']):.3f} "
                f"RAMP_MM_S2={float(plan['ramp_mm_s2']):.3f}"
            )
            if quick_band_break_bypass:
                start_command += " BAND_BREAK_BYPASS=1"
            try:
                response = self._production_esp(start_command, read_timeout_s=15.0, priority=True)
            except Exception as exc:
                running, detail = self._production_status_after_start_error(start_command, exc)
                if not running:
                    raise RuntimeError(f"PROCESS PRODUCTION START failed: {detail}") from exc
                response = detail
            time.sleep(PRODUCTION_WICKLER_POST_START_VERIFY_DELAY_S)
            post_start_wicklers = self._production_wickler_verifications(
                timeout_s=2.0,
                require_indexed_mode=False,
            )
            if not post_start_wicklers.get("ok"):
                stop_result = self._stop_production_motion(
                    reason="production_wickler_post_start_verify_failed",
                    target_state=7,
                )
                post_start_wicklers["stop"] = stop_result
                raise RuntimeError(
                    "Wickler nach Produktionsstart nicht stabil: "
                    + "; ".join(str(error) for error in post_start_wicklers.get("errors") or ["unknown"])
                )
            result = {
                "ok": True,
                "started_ts": started_ts,
                "finished_ts": now_ts(),
                "synced_state": 5,
                "synced_params": list(sync_info.get("synced") or []),
                "skipped_synced_params": list(sync_info.get("skipped") or []),
                "synced_param_values": dict(sync_info.get("values") or {}),
                "plan": plan,
                "band_break_bypass": quick_band_break_bypass,
                "motor3_zero": motor3_zero,
                "wicklers": wicklers,
                "post_start_wicklers": post_start_wicklers,
                "tto_printer": tto_printer,
                "command": start_command,
                "response": response,
            }
            self._record_event(
                "production_motion_started",
                "info",
                "Produktionslauf gestartet: ESP Produktionsrunner und kontinuierliche Wicklerregelung bereit",
                result,
            )
            return result
        except Exception as exc:
            return {
                "ok": False,
                "started_ts": started_ts,
                "finished_ts": now_ts(),
                "synced_state": 5,
                "error": str(exc),
            }
        finally:
            _PRODUCTION_MOTION_LOCK.release()

    def _zero_motor3_for_production_start(self) -> dict[str, Any]:
        command = "MOTOR 3 SET_POSITION_MM=0.000"
        response = self._production_esp_retry(command, read_timeout_s=5.0, attempts=2, priority=True)
        result: dict[str, Any] = {"ok": True, "command": command, "response": response}
        if bool(getattr(self.cfg, "esp_simulation", False)):
            result["simulated"] = True
            return result
        time.sleep(0.1)
        status_attempts: list[dict[str, Any]] = []
        last_valid: dict[str, Any] | None = None

        for status_command in ("MOTOR 3 REFRESH", "MOTOR 3 STATUS?"):
            for attempt in range(1, 4):
                try:
                    status_response = self._production_esp_retry(
                        status_command,
                        read_timeout_s=5.0,
                        attempts=1,
                        priority=True,
                    )
                    text = str(status_response or "").strip()
                    if not text.startswith("JSON "):
                        raise RuntimeError(f"non_json_status={text[:160]!r}")
                    payload = json.loads(text.removeprefix("JSON ").strip())
                    state = dict(((payload.get("motor") or {}).get("state") or {}))
                    if not state:
                        raise RuntimeError("missing_motor_state")
                    if "feedback_tenths_mm" not in state:
                        raise RuntimeError("missing_feedback_tenths_mm")
                    feedback_raw = state.get("feedback_tenths_mm")
                    try:
                        feedback_tenths = float(str(feedback_raw).strip())
                    except Exception as exc:
                        raise RuntimeError(f"invalid_feedback_tenths_mm={feedback_raw!r}") from exc
                    move = bool(state.get("move", False))
                    alarm = bool(state.get("alarm", False))
                    ready = bool(state.get("ready", True))
                    snapshot = {
                        "command": status_command,
                        "attempt": attempt,
                        "ok": True,
                        "feedback_tenths_mm": feedback_tenths,
                        "ready": ready,
                        "move": move,
                        "alarm": alarm,
                    }
                    status_attempts.append(snapshot)
                    last_valid = snapshot
                    result.update(
                        {
                            "status_command": status_command,
                            "status_response": status_response,
                            "status_attempts": status_attempts,
                            "feedback_tenths_mm": feedback_tenths,
                            "ready": ready,
                            "move": move,
                            "alarm": alarm,
                        }
                    )
                    if abs(feedback_tenths) <= 5.0 and not move and not alarm:
                        return result
                except Exception as exc:
                    status_attempts.append(
                        {
                            "command": status_command,
                            "attempt": attempt,
                            "ok": False,
                            "error": repr(exc),
                        }
                    )
                time.sleep(0.15 * attempt)

        result["status_attempts"] = status_attempts
        if last_valid is not None:
            raise RuntimeError(
                "Motor 3 Produktionsnullpunkt nicht bestaetigt: "
                f"feedback_tenths_mm={last_valid['feedback_tenths_mm']}, "
                f"move={last_valid['move']}, alarm={last_valid['alarm']}, "
                f"status_attempts={status_attempts}"
            )
        raise RuntimeError(
            "Motor 3 Produktionsnullpunkt nach SET_POSITION_MM=0 nicht lesbar: "
            f"status_attempts={status_attempts}"
        )

    def _stop_production_motion(self, *, reason: str, target_state: int) -> dict[str, Any]:
        started_ts = now_ts()
        commands: list[dict[str, Any]] = []
        critical_commands = (
            ("PROCESS PRODUCTION STOP", 1.2),
            ("MOTOR 3 MOVE_VEL_MM_S=0", 0.8),
        )
        cleanup_commands = (
            ("PROCESS WICKLER CANCEL", 1.0),
            ("PROCESS INDEXED STOP", 1.0),
            ("PROCESS PROFILE STOP", 1.0),
        )
        for command, timeout_s in critical_commands:
            try:
                commands.append(
                    {
                        "command": command,
                        "ok": True,
                        "critical": True,
                        "response": self._production_esp(command, read_timeout_s=timeout_s, priority=True),
                    }
                )
            except Exception as exc:
                commands.append({"command": command, "ok": False, "critical": True, "error": repr(exc)})
        motor3_stop = self._verify_motor3_stopped_after_production_stop(commands)
        for command, timeout_s in cleanup_commands:
            try:
                commands.append(
                    {
                        "command": command,
                        "ok": True,
                        "critical": False,
                        "response": self._production_esp(command, read_timeout_s=timeout_s),
                    }
                )
            except Exception as exc:
                commands.append({"command": command, "ok": False, "critical": False, "error": repr(exc)})
        wicklers = self._set_production_wicklers_idle(target_state=target_state)
        self._sync_esp_machine_state(int(target_state or 0), required=False)
        tto_printer = self._queue_tto_printer_state_sync(
            int(target_state or 0),
            self._param_values_by_prefix(("MAP", "TTS")),
            reason=f"production_stop:{reason}",
        )
        critical_commands_ok = all(item.get("ok") for item in commands if item.get("critical"))
        wicklers_ok = all(item.get("ok") for item in wicklers)
        motion_safe = critical_commands_ok and bool(motor3_stop.get("ok"))
        pause_target = int(target_state or 0) in (6, 7)
        pause_stop_reason = (
            str(reason or "").startswith("operator_pause")
            or str(reason or "").startswith("pause:")
            or str(reason or "") == "light_curtain_pause"
            or str(reason or "").startswith("state_5_to_6")
            or str(reason or "").startswith("state_5_to_7")
        )
        ok = motion_safe and (wicklers_ok or (pause_target and pause_stop_reason))
        result = {
            "ok": ok,
            "reason": reason,
            "target_state": int(target_state or 0),
            "started_ts": started_ts,
            "finished_ts": now_ts(),
            "commands": commands,
            "critical_commands_ok": critical_commands_ok,
            "motion_safe": motion_safe,
            "wicklers_ok": wicklers_ok,
            "motor3_stop": motor3_stop,
            "wicklers": wicklers,
            "tto_printer": tto_printer,
        }
        if ok and not wicklers_ok:
            result["accepted_wickler_warning"] = True
            result["accepted_wickler_warning_reason"] = "pause_motion_safe"
        self._record_event(
            "production_motion_stopped",
            "info" if result["ok"] else "warning",
            (
                f"Produktionslauf gestoppt ({reason})"
                if result["ok"]
                else f"Produktionslauf mit Warnungen gestoppt ({reason})"
            ),
            result,
        )
        return result

    def _verify_motor3_stopped_after_production_stop(self, commands: list[dict[str, Any]]) -> dict[str, Any]:
        snapshots: list[dict[str, Any]] = []
        if not bool(getattr(self.cfg, "esp_simulation", False)):
            try:
                diag = self._read_production_monitor_diag()
                monitor_snapshot = {
                    "attempt": 0,
                    "ok": True,
                    "source": "production_monitor",
                    "active": bool(diag.get("active")),
                    "running": bool(diag.get("running")),
                    "phase": diag.get("phase"),
                    "reason": diag.get("reason"),
                    "last_error": diag.get("last_error"),
                }
                snapshots.append(monitor_snapshot)
                if not bool(diag.get("active")) and not bool(diag.get("running")):
                    return {"ok": True, "source": "production_monitor", "snapshots": snapshots}
            except Exception as exc:
                snapshots.append({"attempt": 0, "ok": False, "source": "production_monitor", "error": repr(exc)})

        for attempt in range(1, 4):
            try:
                response = self._production_esp("MOTOR 3 REFRESH", read_timeout_s=1.0, priority=True)
                payload = json.loads(str(response or "").removeprefix("JSON ").strip())
                state = dict(((payload.get("motor") or {}).get("state") or {}))
                velocity_mode = bool(state.get("velocity_mode"))
                target_speed_mm_s = _safe_float(state.get("target_speed_mm_s"), 0.0)
                active = bool(state.get("move")) or bool(state.get("busy"))
                snapshot = {
                    "attempt": attempt,
                    "ok": True,
                    "active": active,
                    "move": state.get("move"),
                    "busy": state.get("busy"),
                    "velocity_mode": velocity_mode,
                    "target_speed_mm_s": state.get("target_speed_mm_s"),
                    "stale_velocity_fields": bool(not active and (velocity_mode or abs(target_speed_mm_s) > 0.05)),
                    "feedback_tenths_mm": state.get("feedback_tenths_mm"),
                }
                snapshots.append(snapshot)
                if not active:
                    return {"ok": True, "snapshots": snapshots}
            except Exception as exc:
                snapshots.append({"attempt": attempt, "ok": False, "error": repr(exc)})
                if bool(getattr(self.cfg, "esp_simulation", False)):
                    return {"ok": True, "simulated": True, "snapshots": snapshots}
            for command, timeout_s in (
                ("PROCESS PRODUCTION STOP", 2.0),
                ("MOTOR 3 MOVE_VEL_MM_S=0", 1.0),
            ):
                try:
                    commands.append(
                        {
                            "command": f"verify:{command}",
                            "ok": True,
                            "response": self._production_esp(command, read_timeout_s=timeout_s, priority=True),
                        }
                    )
                except Exception as exc:
                    commands.append({"command": f"verify:{command}", "ok": False, "error": repr(exc)})
            time.sleep(0.1)
        return {"ok": False, "snapshots": snapshots}

    def _motor_feedback_io_snapshot(self, motor_ids: Any) -> dict[int, dict[str, dict[str, Any]]]:
        requested = {int(motor_id) for motor_id in motor_ids if int(motor_id) in MOTOR_HARDWARE_FEEDBACK_IO}
        if not requested:
            return {}

        points_by_key: dict[str, dict[str, Any]] = {}
        for motor_id in sorted(requested):
            for device_code, pin_label in MOTOR_HARDWARE_FEEDBACK_IO[motor_id].values():
                io_key = f"{device_code}__{pin_label.replace('.', '_')}"
                point = self.io_store.get_point(io_key)
                if point:
                    points_by_key[io_key] = point

        esp_points = [point for point in points_by_key.values() if point.get("device_code") == "esp32_plc58"]
        if esp_points:
            try:
                IoRuntime(self.cfg, self.io_store)._refresh_device("esp32_plc58", esp_points)
            except Exception:
                # Hardware feedback is an acceleration/diagnostic path.  The
                # RS485 motor status remains authoritative if the ESP IO
                # snapshot is temporarily unavailable.
                pass

        snapshot: dict[int, dict[str, dict[str, Any]]] = {}
        for motor_id in sorted(requested):
            motor_signals: dict[str, dict[str, Any]] = {}
            for signal, (device_code, pin_label) in MOTOR_HARDWARE_FEEDBACK_IO[motor_id].items():
                io_key = f"{device_code}__{pin_label.replace('.', '_')}"
                point = self.io_store.get_point(io_key)
                if not point:
                    continue
                value = str(point.get("value") if point.get("value") is not None else "0")
                motor_signals[signal] = {
                    "io_key": io_key,
                    "pin": pin_label,
                    "value": value,
                    "active": _truthy(value),
                    "quality": point.get("quality"),
                    "source": point.get("source"),
                }
            if motor_signals:
                snapshot[motor_id] = motor_signals
        return snapshot

    def _setup_format_axis_phases(self, format_plan: dict[str, Any]) -> list[tuple[str, dict[int, float]]]:
        axes = dict((format_plan.get("axes") or {}) if isinstance(format_plan, dict) else {})

        def target_mm(key: str) -> float:
            return _safe_int(axes.get(key), 0) / 10.0

        return [
            (
                "format_axes_parallel",
                {
                    9: target_mm("label_guide_infeed_target_tenths_mm"),
                    8: target_mm("label_guide_outfeed_target_tenths_mm"),
                    6: target_mm("label_detect_sensor_target_tenths_mm"),
                    7: target_mm("label_control_sensor_target_tenths_mm"),
                    5: target_mm("material_camera_x_target_tenths_mm"),
                },
            ),
        ]

    def _position_setup_format_axes(
        self,
        format_plan: dict[str, Any],
        motor_ids: Iterable[int] | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"ok": False, "skipped": False, "phases": []}
        phases = self._setup_format_axis_phases(format_plan)
        if motor_ids is not None:
            allowed_motor_ids = {int(motor_id) for motor_id in motor_ids}
            phases = [
                (phase_name, {motor_id: target for motor_id, target in targets.items() if motor_id in allowed_motor_ids})
                for phase_name, targets in phases
            ]
            phases = [(phase_name, targets) for phase_name, targets in phases if targets]
        result["targets_mm"] = {str(motor_id): target for _, targets in phases for motor_id, target in targets.items()}

        client = EspMotorClient(self.cfg)
        if not client.available():
            result.update({"ok": True, "skipped": True, "reason": "esp_motor_endpoint_unavailable_or_simulation"})
            return result

        for phase_name, targets_mm in phases:
            phase_result: dict[str, Any] = {
                "phase": phase_name,
                "ok": False,
                "targets_mm": dict(targets_mm),
                "preflight": [],
                "moves": [],
            }
            result["phases"].append(phase_result)

            errors: list[str] = []
            for motor_id in sorted(targets_mm):
                restore = apply_motor_setup_master_config_to_client(
                    self.params,
                    client,
                    motor_id,
                    restore_position=False,
                )
                phase_result.setdefault("master_restore", []).append({"motor_id": motor_id, **restore})
                if not bool(restore.get("ok", False)):
                    errors.append(
                        f"Motor {motor_id}: Motor-Setup-Master konnte nicht angewendet werden: "
                        f"{restore.get('reply') or restore.get('reason') or restore}"
                    )

            if errors:
                phase_result.update({"ok": False, "errors": errors})
                raise RuntimeError(f"Einrichten Formatachsen {phase_name} Master-Setup verweigert: " + "; ".join(errors))

            for motor_id, target_mm in sorted(targets_mm.items()):
                preflight = self._motor_preflight_for_position_move(client, motor_id, target_mm)
                phase_result["preflight"].append(preflight)
                if not preflight.get("ok"):
                    latch = self._latch_position_axis_preflight_fault(
                        motor_id,
                        preflight,
                        context=f"Einrichten Formatachsen {phase_name}",
                    )
                    if latch:
                        preflight["latched_mae"] = latch
                    errors.append(f"Motor {motor_id}: {preflight.get('reason') or 'Positionierfreigabe verweigert'}")

            if errors:
                phase_result.update({"ok": False, "errors": errors})
                raise RuntimeError(f"Einrichten Formatachsen {phase_name} verweigert: " + "; ".join(errors))

            for motor_id in sorted(targets_mm):
                try:
                    client.reset_alarm(motor_id)
                    client.recover_eto_motor(motor_id)
                except Exception as exc:
                    phase_result["moves"].append({"motor_id": motor_id, "ok": False, "error": repr(exc)})
                    errors.append(f"Motor {motor_id}: {exc}")

            move_warnings: list[str] = []
            verification: dict[str, Any] = {"ok": False, "results": [], "errors": ["Positionssatz nicht gesendet"]}
            if not errors:
                verification_attempts: list[dict[str, Any]] = []
                for attempt in range(1, SETUP_AXIS_MOVE_SET_MAX_ATTEMPTS + 1):
                    attempt_warnings: list[str] = []
                    move_acknowledged = False
                    try:
                        # The ESP loads all AZD direct-data records first and then
                        # triggers the axes as one positioning set. This avoids the
                        # visible axis-by-axis movement from single MOVE_ABS_MM
                        # commands whose trigger is executed immediately.
                        reply = client.move_absolute_set_mm(targets_mm)
                        move_acknowledged = bool(reply.get("ok"))
                        move_result = {
                            "attempt": attempt,
                            "motor_ids": sorted(targets_mm),
                            "targets_mm": dict(targets_mm),
                            "ok": move_acknowledged,
                            "reply": reply,
                        }
                        phase_result["moves"].append(move_result)
                        if not move_result["ok"]:
                            attempt_warnings.append(f"Positionssatz: {reply}")
                    except Exception as exc:
                        phase_result["moves"].append(
                            {
                                "attempt": attempt,
                                "motor_ids": sorted(targets_mm),
                                "ok": False,
                                "error": repr(exc),
                                "targets_mm": dict(targets_mm),
                            }
                        )
                        attempt_warnings.append(f"Positionssatz: {exc}")

                    move_warnings.extend(attempt_warnings)
                    verify_timeout = (
                        SETUP_AXIS_POSITION_VERIFY_TIMEOUT_S
                        if move_acknowledged
                        else SETUP_AXIS_MOVE_SET_SHORT_VERIFY_TIMEOUT_S
                    )
                    verification = self._verify_axis_targets(
                        client,
                        targets_mm,
                        tolerance_tenths=SETUP_AXIS_POSITION_TOLERANCE_TENTHS,
                        timeout_s=verify_timeout,
                        poll_s=SETUP_AXIS_POSITION_VERIFY_POLL_S,
                    )
                    verification_attempts.append(
                        {
                            "attempt": attempt,
                            "move_acknowledged": move_acknowledged,
                            "verify_timeout_s": verify_timeout,
                            "ok": bool(verification.get("ok")),
                            "errors": list(verification.get("errors") or []),
                        }
                    )
                    if verification.get("ok"):
                        break
                    if attempt >= SETUP_AXIS_MOVE_SET_MAX_ATTEMPTS:
                        break
                    self.logs.log(
                        "machine",
                        "warning",
                        f"Einrichten Formatachsen {phase_name}: Positionssatz Versuch {attempt} "
                        "nicht verifiziert, wiederhole",
                    )
                    time.sleep(min(1.0, 0.35 * attempt))
                phase_result["verification_attempts"] = verification_attempts
                if move_warnings:
                    phase_result["move_warnings"] = move_warnings

            phase_result["verification"] = verification
            if not verification.get("ok"):
                errors = [*move_warnings, *[str(item) for item in verification.get("errors") or []]]
                phase_result.update({"ok": False, "errors": errors})
                raise RuntimeError(f"Einrichten Formatachsen {phase_name} Ziel nicht erreicht: " + "; ".join(errors))

            phase_result.update({"ok": True, "errors": [], "warnings": move_warnings})

        result.update({"ok": True, "finished_ts": now_ts()})
        self.logs.log("machine", "info", "Einrichten: Formatachsen stehen auf Rezeptposition")
        self._record_event(
            "setup_format_axes",
            "info",
            "Einrichten: Formatachsen stehen auf Rezeptposition",
            result,
        )
        return result

    def _stop_mode_target_key(self) -> str:
        return ";".join(f"{motor_id}:{target_mm:.3f}" for motor_id, target_mm in sorted(STOP_MODE_AXIS_TARGETS_MM.items()))

    def _block_position_axis_if_live_outside_limits(
        self,
        motor_id: int,
        motor_cfg: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any] | None:
        motor_id = int(motor_id)
        if motor_id == 3:
            return None
        try:
            feedback_tenths = int(float(state.get("feedback_tenths_mm")))
        except Exception:
            return None
        min_enabled = bool(motor_cfg.get("min_enabled", True))
        max_enabled = bool(motor_cfg.get("max_enabled", True))
        min_tenths = _safe_int(motor_cfg.get("min_tenths_mm"), -2_147_483_648)
        max_tenths = _safe_int(motor_cfg.get("max_tenths_mm"), 2_147_483_647)
        below_min = bool(min_enabled and feedback_tenths < min_tenths)
        above_max = bool(max_enabled and feedback_tenths > max_tenths)
        if not (below_min or above_max):
            return None
        block: dict[str, Any] = {
            "ok": False,
            "motor_id": motor_id,
            "before_feedback_tenths_mm": feedback_tenths,
            "min_tenths_mm": min_tenths,
            "max_tenths_mm": max_tenths,
            "reason": "live_position_outside_limits",
            "blocked": True,
        }
        if bool(state.get("alarm")) or bool(state.get("hwto")):
            block["reason"] = "alarm_or_hwto_active"
        self.logs.log(
            "machine",
            "warning",
            (
                f"Motor {motor_id}: Live-Istposition {feedback_tenths / 10.0:.1f}mm ausserhalb "
                f"Limits {min_tenths / 10.0:.1f}..{max_tenths / 10.0:.1f}mm - automatische "
                "Positionsuebernahme gesperrt"
            ),
        )
        self._record_event(
            "motor_setup_position_write_blocked",
            "warning",
            f"Motor {motor_id}: Positionszaehler-Restore ausserhalb Machine-Setup blockiert",
            block,
        )
        return block

    def _motor_preflight_for_position_move(
        self,
        client: EspMotorClient,
        motor_id: int,
        target_mm: float,
    ) -> dict[str, Any]:
        """Read live motor state and block unsafe automatic positioning.

        This is intentionally conservative: if a positional axis is in alarm,
        in HWTO, or already outside its active limits, the automatic stop-mode
        routine must not reset and continue moving it. Manual Machine Setup is
        then required to recover the axis deliberately.
        """
        result: dict[str, Any] = {"motor_id": int(motor_id), "target_mm": float(target_mm), "ok": False}
        cfg_payload = client.config(motor_id)
        motor_cfg = (cfg_payload or {}).get("config") or {}
        refresh_payload = client.refresh(motor_id)
        motor = (refresh_payload or {}).get("motor") or {}
        state = motor.get("state") or motor or {}

        result["config"] = motor_cfg
        result["state"] = state

        target_tenths = int(round(float(target_mm) * 10.0))
        try:
            feedback_tenths = int(float(state.get("feedback_tenths_mm")))
        except Exception:
            result["reason"] = "Istposition nicht lesbar"
            return result

        min_enabled = bool(motor_cfg.get("min_enabled", True))
        max_enabled = bool(motor_cfg.get("max_enabled", True))
        min_tenths = _safe_int(motor_cfg.get("min_tenths_mm"), -2_147_483_648)
        max_tenths = _safe_int(motor_cfg.get("max_tenths_mm"), 2_147_483_647)
        result.update(
            {
                "feedback_tenths_mm": feedback_tenths,
                "target_tenths_mm": target_tenths,
                "min_enabled": min_enabled,
                "max_enabled": max_enabled,
                "min_tenths_mm": min_tenths,
                "max_tenths_mm": max_tenths,
            }
        )

        if bool(state.get("alarm")):
            result["reason"] = f"Achse im Alarm {state.get('alarm_code')}"
            return result
        if bool(state.get("hwto")):
            result["reason"] = "HWTO/Sicherheitskreis aktiv"
            return result
        reference_suspect = self._position_axis_reference_suspect(motor_id)
        if reference_suspect is not None:
            result["position_reference_suspect"] = reference_suspect
            result["reason"] = (
                "Positionsreferenz nach frueherem automatischem Restore unsicher; "
                "Achse zuerst ueber /ui/machine-setup/motors neu kalibrieren und speichern"
            )
            return result
        position_block = self._block_position_axis_if_live_outside_limits(motor_id, motor_cfg, state)
        if position_block is not None:
            result["position_write_block"] = position_block
        if min_enabled and feedback_tenths < min_tenths:
            result["reason"] = (
                f"Istposition {feedback_tenths / 10.0:.1f}mm unter Min {min_tenths / 10.0:.1f}mm"
            )
            return result
        if max_enabled and feedback_tenths > max_tenths:
            result["reason"] = (
                f"Istposition {feedback_tenths / 10.0:.1f}mm ueber Max {max_tenths / 10.0:.1f}mm"
            )
            return result
        if min_enabled and target_tenths < min_tenths:
            result["reason"] = f"Ziel {target_mm:.1f}mm unter Min {min_tenths / 10.0:.1f}mm"
            return result
        if max_enabled and target_tenths > max_tenths:
            result["reason"] = f"Ziel {target_mm:.1f}mm ueber Max {max_tenths / 10.0:.1f}mm"
            return result

        result["ok"] = True
        result["reason"] = "safe"
        return result

    def _latch_position_axis_preflight_fault(
        self,
        motor_id: int,
        preflight: dict[str, Any],
        *,
        context: str,
    ) -> dict[str, Any] | None:
        pkey = POSITION_AXIS_MAE_BY_MOTOR.get(int(motor_id))
        if not pkey:
            return None
        state = dict((preflight or {}).get("state") or {})
        reason = str((preflight or {}).get("reason") or "Positionierfreigabe verweigert")
        latch = (
            bool(state.get("alarm"))
            or bool(state.get("hwto"))
            or "unter Min" in reason
            or "ueber Max" in reason
            or "Positionsreferenz" in reason
        )
        if not latch:
            return None
        self.params.apply_device_value(pkey, "1", promote_default=True)
        self._notify_microtom(pkey, "1", dedupe_key=f"machine:{pkey}")
        detail = {
            "pkey": pkey,
            "motor_id": int(motor_id),
            "reason": reason,
            "context": str(context or ""),
            "alarm": bool(state.get("alarm")),
            "alarm_code": state.get("alarm_code"),
            "feedback_tenths_mm": state.get("feedback_tenths_mm"),
        }
        self.logs.log("machine", "warning", f"{context}: Motor {int(motor_id)} blockiert ({reason}), {pkey}=1")
        return detail

    def _stop_mode_command_target_mm(self, target_mm: float, preflight: dict[str, Any]) -> tuple[float, dict[str, Any] | None]:
        """Keep automatic stop moves just inside active AZD soft limits.

        The operator-facing stop target remains unchanged and verification still
        uses the configured target with tolerance.  The small inward command
        margin prevents AZD alarms when an automatic stop/abort path asks a
        positioning drive to land exactly on an active min/max boundary.
        """
        try:
            target_tenths = int(float(preflight.get("target_tenths_mm")))
        except Exception:
            return float(target_mm), None
        command_tenths = target_tenths
        min_enabled = bool(preflight.get("min_enabled", True))
        max_enabled = bool(preflight.get("max_enabled", True))
        min_tenths = _safe_int(preflight.get("min_tenths_mm"), -2_147_483_648)
        max_tenths = _safe_int(preflight.get("max_tenths_mm"), 2_147_483_647)
        margin = int(STOP_MODE_POSITION_LIMIT_MARGIN_TENTHS)
        if min_enabled and target_tenths <= min_tenths:
            command_tenths = max(command_tenths, min_tenths + margin)
        if max_enabled and target_tenths >= max_tenths:
            command_tenths = min(command_tenths, max_tenths - margin)
        if min_enabled and command_tenths < min_tenths:
            command_tenths = min_tenths
        if max_enabled and command_tenths > max_tenths:
            command_tenths = max_tenths
        if command_tenths == target_tenths:
            return float(target_mm), None
        command_mm = command_tenths / 10.0
        return command_mm, {
            "reason": "stop_mode_limit_margin",
            "requested_target_tenths_mm": target_tenths,
            "command_target_tenths_mm": command_tenths,
            "requested_target_mm": float(target_mm),
            "command_target_mm": command_mm,
            "margin_tenths_mm": margin,
            "min_enabled": min_enabled,
            "max_enabled": max_enabled,
            "min_tenths_mm": min_tenths,
            "max_tenths_mm": max_tenths,
        }

    def _apply_stop_mode_axis_targets(
        self,
        state: int,
        info: dict[str, Any],
        *,
        state_changed: bool,
        ts: float,
    ) -> None:
        stop_info = dict(info.get("stop_positions") or {})
        target_key = self._stop_mode_target_key()
        if int(state or 0) != 9:
            if stop_info.get("active"):
                stop_info["active"] = False
                stop_info["left_stop_ts"] = ts
                info["stop_positions"] = stop_info
            return

        manual_lock = motor_setup_manual_lock_status(self.db, now=ts)
        if bool(manual_lock.get("active")):
            stop_info.update(
                {
                    "active": True,
                    "ok": False,
                    "skipped": True,
                    "reason": "motor_setup_manual_lock_active",
                    "manual_lock": manual_lock,
                    "last_skipped_ts": ts,
                }
            )
            info["stop_positions"] = stop_info
            return

        last_attempt_ts = float(stop_info.get("last_attempt_ts") or 0.0)
        attempt_count = int(stop_info.get("attempt_count") or 0)
        reset_attempt_counter = (
            bool(state_changed)
            or stop_info.get("target_key") != target_key
            or stop_info.get("logic_version") != STOP_MODE_POSITION_LOGIC_VERSION
        )
        retry_due = attempt_count < STOP_MODE_POSITION_MAX_ATTEMPTS and (ts - last_attempt_ts) >= STOP_MODE_POSITION_RETRY_S
        should_apply = (
            bool(state_changed)
            or stop_info.get("target_key") != target_key
            or stop_info.get("logic_version") != STOP_MODE_POSITION_LOGIC_VERSION
            or "verification" not in stop_info
            or (not bool(stop_info.get("ok")) and retry_due)
        )
        if not should_apply:
            stop_info["active"] = True
            info["stop_positions"] = stop_info
            return

        client = EspMotorClient(self.cfg)
        stop_info = {
            **stop_info,
            "active": True,
            "ok": False,
            "logic_version": STOP_MODE_POSITION_LOGIC_VERSION,
            "attempt_count": (0 if reset_attempt_counter else int(stop_info.get("attempt_count") or 0)) + 1,
            "target_key": target_key,
            "last_attempt_ts": ts,
            "targets_mm": dict(STOP_MODE_AXIS_TARGETS_MM),
            "results": [],
        }
        if not client.available():
            stop_info.update({"skipped": True, "reason": "esp_motor_endpoint_unavailable_or_simulation"})
            info["stop_positions"] = stop_info
            return

        errors: list[str] = []
        accepted_targets_mm: dict[int, float] = {}
        for motor_id, target_mm in sorted(STOP_MODE_AXIS_TARGETS_MM.items()):
            try:
                restore = apply_motor_setup_master_config_to_client(
                    self.params,
                    client,
                    motor_id,
                    restore_position=False,
                )
                if not bool(restore.get("ok", False)):
                    reason = str(restore.get("reply") or restore.get("reason") or "Motor-Setup-Master nicht anwendbar")
                    result = {
                        "motor_id": motor_id,
                        "target_mm": target_mm,
                        "ok": False,
                        "master_restore": restore,
                        "error": reason,
                    }
                    stop_info["results"].append(result)
                    errors.append(f"Motor {motor_id}: Motor-Setup-Master konnte nicht angewendet werden: {reason}")
                    continue
                preflight = self._motor_preflight_for_position_move(client, motor_id, target_mm)
                if not preflight.get("ok"):
                    reason = str(preflight.get("reason") or "Positionierfreigabe verweigert")
                    latch = self._latch_position_axis_preflight_fault(
                        motor_id,
                        preflight,
                        context="Produktions-Stop Positionssatz",
                    )
                    if latch:
                        preflight["latched_mae"] = latch
                    result = {
                        "motor_id": motor_id,
                        "target_mm": target_mm,
                        "ok": False,
                        "master_restore": restore,
                        "preflight": preflight,
                        "error": reason,
                    }
                    stop_info["results"].append(result)
                    errors.append(f"Motor {motor_id}: {reason}")
                    continue
                try:
                    feedback_tenths = int(float(preflight.get("feedback_tenths_mm")))
                    target_tenths = int(float(preflight.get("target_tenths_mm")))
                except Exception:
                    feedback_tenths = None
                    target_tenths = None
                if (
                    feedback_tenths is not None
                    and target_tenths is not None
                    and abs(target_tenths - feedback_tenths) <= STOP_MODE_POSITION_TOLERANCE_TENTHS
                ):
                    result = {
                        "motor_id": motor_id,
                        "target_mm": target_mm,
                        "ok": True,
                        "master_restore": restore,
                        "preflight": preflight,
                        "queued": False,
                        "already_in_position": True,
                    }
                    stop_info["results"].append(result)
                    continue
                client.reset_alarm(motor_id)
                client.recover_eto_motor(motor_id)
                command_target_mm, command_adjustment = self._stop_mode_command_target_mm(target_mm, preflight)
                result = {
                    "motor_id": motor_id,
                    "target_mm": target_mm,
                    "command_target_mm": command_target_mm,
                    "ok": True,
                    "master_restore": restore,
                    "queued": True,
                }
                if command_adjustment is not None:
                    result["command_adjustment"] = command_adjustment
                stop_info["results"].append(result)
                accepted_targets_mm[int(motor_id)] = float(command_target_mm)
            except Exception as exc:
                result = {"motor_id": motor_id, "target_mm": target_mm, "ok": False, "error": repr(exc)}
                stop_info["results"].append(result)
                errors.append(f"Motor {motor_id}: {exc}")

        move_set_warnings: list[str] = []
        if accepted_targets_mm and not errors:
            try:
                reply = client.move_absolute_set_mm(accepted_targets_mm)
                stop_info["move_set"] = {"ok": bool(reply.get("ok")), "reply": reply, "targets_mm": accepted_targets_mm}
                if not bool(reply.get("ok")):
                    move_set_warnings.append(f"Positionssatz: {reply}")
            except Exception as exc:
                stop_info["move_set"] = {"ok": False, "error": repr(exc), "targets_mm": accepted_targets_mm}
                move_set_warnings.append(f"Positionssatz: {exc}")

        verification = self._verify_stop_mode_axis_targets(client)
        stop_info["verification"] = verification
        if not verification.get("ok"):
            errors.extend(move_set_warnings)
            errors.extend(str(item) for item in verification.get("errors") or [])
        elif move_set_warnings:
            stop_info["move_set_warnings"] = move_set_warnings

        if errors:
            stop_info.update({"ok": False, "errors": errors, "finished_ts": now_ts()})
            self.logs.log("machine", "warning", "Stop-Positionssatz unvollstaendig: " + "; ".join(errors))
            self._record_event(
                "stop_mode_axis_targets",
                "warning",
                "Stop-Positionssatz konnte nicht vollstaendig gesendet werden",
                stop_info,
            )
        else:
            stop_info.update({"ok": True, "errors": [], "finished_ts": now_ts()})
            self.logs.log("machine", "info", "Stop-Positionssatz gesendet: ID5=0mm, ID6/7=-20mm, ID8/9=100mm")
            self._record_event(
                "stop_mode_axis_targets",
                "info",
                "Stop-Positionssatz gesendet: ID5=0mm, ID6/7=-20mm, ID8/9=100mm",
                stop_info,
            )
        info["stop_positions"] = stop_info

    def _verify_axis_targets(
        self,
        client: EspMotorClient,
        targets_mm: dict[int, float],
        *,
        tolerance_tenths: int,
        timeout_s: float,
        poll_s: float,
    ) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        errors: list[str] = []
        pending = {int(motor_id): float(target_mm) for motor_id, target_mm in targets_mm.items()}
        last_by_motor: dict[int, dict[str, Any]] = {}
        stationary_since: dict[int, float] = {}
        last_feedback_by_motor: dict[int, int] = {}
        saw_moving_by_motor: dict[int, bool] = {}
        saw_fresh_by_motor: dict[int, bool] = {}
        saw_hardware_move_by_motor: dict[int, bool] = {}
        refresh_error_count_by_motor: dict[int, int] = {}
        deadline = time.time() + float(timeout_s)

        while pending and time.time() < deadline:
            now = time.time()
            hardware_by_motor = self._motor_feedback_io_snapshot(pending.keys())
            completed: list[int] = []
            failed: list[int] = []
            for motor_id, target_mm in sorted(pending.items()):
                target_tenths = int(round(float(target_mm) * 10.0))
                last_result: dict[str, Any] | None = None
                hardware = hardware_by_motor.get(motor_id) or {}
                hardware_move = bool((hardware.get("move") or {}).get("active"))
                hardware_in_pos = bool((hardware.get("in_pos") or {}).get("active"))
                if hardware_move:
                    saw_moving_by_motor[motor_id] = True
                    saw_hardware_move_by_motor[motor_id] = True
                try:
                    payload = client.refresh(motor_id)
                    refresh_error_count_by_motor[motor_id] = 0
                    motor = payload.get("motor") if isinstance(payload, dict) else {}
                    state = (motor or {}).get("state") or motor or {}
                    payload_ok = bool(payload.get("ok", True)) if isinstance(payload, dict) else True
                    link_ok = state.get("link_ok")
                    if not payload_ok or link_ok is False:
                        last_result = {
                            "motor_id": motor_id,
                            "target_tenths_mm": target_tenths,
                            "ok": False,
                            "fresh": False,
                            "payload_ok": payload_ok,
                            "link_ok": link_ok,
                            "last_error": state.get("last_error"),
                            "feedback_tenths_mm": state.get("feedback_tenths_mm"),
                            "hardware": hardware,
                        }
                        last_by_motor[motor_id] = last_result
                        if hardware_in_pos and saw_hardware_move_by_motor.get(motor_id) and not hardware_move:
                            last_result["hardware_at_target"] = True
                            last_result["at_target"] = True
                            last_result["moving"] = False
                            completed.append(motor_id)
                        continue
                    feedback_tenths = int(state.get("feedback_tenths_mm"))
                    moving = bool(state.get("move")) or bool(state.get("busy")) or hardware_move
                    alarm = bool(state.get("alarm"))
                    error_tenths = target_tenths - feedback_tenths
                    feedback_at_target = abs(error_tenths) <= int(tolerance_tenths)
                    try:
                        drive_target_tenths = int(float(state.get("target_tenths_mm")))
                    except Exception:
                        drive_target_tenths = None
                    hardware_at_target = (
                        hardware_in_pos
                        and not hardware_move
                        and (
                            drive_target_tenths == target_tenths
                            or bool(saw_hardware_move_by_motor.get(motor_id))
                        )
                    )
                    at_target = feedback_at_target or hardware_at_target
                    saw_fresh_by_motor[motor_id] = True
                    if moving:
                        saw_moving_by_motor[motor_id] = True
                    last_result = {
                        "motor_id": motor_id,
                        "target_tenths_mm": target_tenths,
                        "feedback_tenths_mm": feedback_tenths,
                        "error_tenths_mm": error_tenths,
                        "moving": moving,
                        "alarm": alarm,
                        "alarm_code": state.get("alarm_code"),
                        "at_target": at_target,
                        "feedback_at_target": feedback_at_target,
                        "hardware_at_target": hardware_at_target,
                        "hardware": hardware,
                        "fresh": True,
                        "payload_ok": payload_ok,
                        "link_ok": link_ok,
                    }
                    last_by_motor[motor_id] = last_result
                    if at_target and not moving and not alarm:
                        completed.append(motor_id)
                        continue
                    if alarm:
                        errors.append(
                            f"Motor {motor_id} Alarm {state.get('alarm_code')} bei {feedback_tenths / 10.0:.1f}mm, "
                            f"Ziel {target_mm:.1f}mm"
                        )
                        failed.append(motor_id)
                        continue
                    if not moving:
                        previous_feedback = last_feedback_by_motor.get(motor_id)
                        if previous_feedback is None or previous_feedback != feedback_tenths:
                            stationary_since[motor_id] = now
                        else:
                            stationary_since.setdefault(motor_id, now)
                    else:
                        stationary_since.pop(motor_id, None)
                    last_feedback_by_motor[motor_id] = feedback_tenths
                except Exception as exc:
                    count = refresh_error_count_by_motor.get(motor_id, 0) + 1
                    refresh_error_count_by_motor[motor_id] = count
                    last_result = {
                        "motor_id": motor_id,
                        "target_mm": target_mm,
                        "ok": False,
                        "error": repr(exc),
                        "refresh_error_count": count,
                        "hardware": hardware,
                    }
                    last_by_motor[motor_id] = last_result
                    if hardware_in_pos and saw_hardware_move_by_motor.get(motor_id) and not hardware_move:
                        last_result["hardware_at_target"] = True
                        last_result["at_target"] = True
                        last_result["moving"] = False
                        completed.append(motor_id)
                        continue
                    if count >= 3:
                        errors.append(f"Motor {motor_id} Refresh nach {count} Versuchen: {exc}")
                        failed.append(motor_id)
            for motor_id in completed + failed:
                pending.pop(motor_id, None)
            if pending:
                time.sleep(float(poll_s))

        if pending:
            for motor_id, target_mm in sorted(pending.items()):
                last = last_by_motor.get(motor_id) or {}
                feedback = last.get("feedback_tenths_mm")
                if not bool(saw_fresh_by_motor.get(motor_id)):
                    detail = str(last.get("last_error") or last.get("error") or "keine frische Antwort")
                    errors.append(
                        f"Motor {motor_id} Status nicht frisch/lesbar vor Zielpruefung: {detail}"
                    )
                    continue
                hardware = last.get("hardware") if isinstance(last.get("hardware"), dict) else {}
                if bool((hardware.get("move") or {}).get("active")):
                    errors.append(
                        f"Motor {motor_id} Hardware-MOVE noch aktiv, Ziel {target_mm:.1f}mm nicht abgeschlossen"
                    )
                    continue
                if feedback is not None and not bool(last.get("moving")) and not bool(saw_moving_by_motor.get(motor_id)):
                    errors.append(
                        f"Motor {motor_id} steht bei {int(feedback) / 10.0:.1f}mm, "
                        f"Ziel {target_mm:.1f}mm, keine Bewegung gemeldet"
                    )
                    continue
                suffix = f"(Ist {int(feedback) / 10.0:.1f}mm)" if feedback is not None else "(Ist unbekannt)"
                errors.append(
                    f"Motor {motor_id} Ziel {target_mm:.1f}mm nach {float(timeout_s):.0f}s nicht erreicht {suffix}"
                )

        for motor_id in sorted(targets_mm):
            if motor_id in last_by_motor:
                results.append(last_by_motor[motor_id])
            else:
                results.append({"motor_id": motor_id, "target_mm": targets_mm[motor_id], "ok": False, "error": "not_polled"})
        return {"ok": not errors, "results": results, "errors": errors}

    def _verify_stop_mode_axis_targets(self, client: EspMotorClient) -> dict[str, Any]:
        return self._verify_axis_targets(
            client,
            STOP_MODE_AXIS_TARGETS_MM,
            tolerance_tenths=STOP_MODE_POSITION_TOLERANCE_TENTHS,
            timeout_s=STOP_MODE_POSITION_VERIFY_TIMEOUT_S,
            poll_s=STOP_MODE_POSITION_VERIFY_POLL_S,
        )

    def _active_param_keys(self, ptype: str) -> list[str]:
        with self.db._conn() as c:
            rows = c.execute(
                """SELECT p.pkey, COALESCE(v.value, p.default_v, '0')
                   FROM params p
                   LEFT JOIN param_values v ON v.pkey = p.pkey
                   WHERE p.ptype = ?""",
                (str(ptype or "").upper(),),
            ).fetchall()
        return [str(pkey) for pkey, value in rows if _truthy(value)]

    def _warning_active(self, param_map: dict[str, str]) -> bool:
        for pkey, value in param_map.items():
            if pkey.startswith("MAW") and _truthy(value):
                return True
        return False

    def _pause_state(self, param_map: dict[str, str]) -> tuple[bool, list[str]]:
        reasons = [pkey for pkey in sorted(PAUSE_ERROR_KEYS) if _truthy(param_map.get(pkey))]
        return bool(reasons), reasons

    def _light_curtain_auto_reset_due(
        self,
        *,
        safety_status: dict[str, Any],
        critical_reasons: list[str],
        safety_info: dict[str, Any],
        info: dict[str, Any],
        mas0028_active: bool,
        ts: float,
    ) -> bool:
        if not bool(getattr(self.cfg, "light_curtain_auto_reset_enabled", True)):
            return False
        if external_purge_active(info):
            return False
        if mas0028_active:
            return False
        if bool(safety_status.get("estop_active")):
            return False
        if not bool(safety_status.get("light_curtain_active")):
            return False

        live_safety_reasons = {str(reason) for reason in (safety_status.get("reasons") or [])}
        if not live_safety_reasons.issubset({"lichtgitter"}):
            return False

        live_critical_reasons = {str(reason) for reason in (critical_reasons or [])}
        if live_critical_reasons:
            return False

        try:
            last_ts = float(safety_info.get("light_curtain_auto_reset_last_ts") or 0.0)
        except Exception:
            last_ts = 0.0
        return (float(ts) - last_ts) >= LIGHT_CURTAIN_AUTO_RESET_INTERVAL_S

    def _blocking_safety_reasons(self, safety_status: dict[str, Any]) -> list[str]:
        # The light curtain has a separate automatic reset path and must not
        # behave like Not-Aus in the software state machine.
        return ["notaus"] if bool(safety_status.get("estop_active")) else []

    def _blocking_safety_active(self, safety_status: dict[str, Any]) -> bool:
        return bool(self._blocking_safety_reasons(safety_status))

    def _stale_light_curtain_only_latch(
        self,
        *,
        safety_status: dict[str, Any],
        critical_reasons: list[str],
        safety_info: dict[str, Any],
        info: dict[str, Any],
        mas0028_active: bool,
    ) -> bool:
        if external_purge_active(info):
            return False
        if self._blocking_safety_active(safety_status):
            return False
        if critical_reasons:
            return False
        live_safety_reasons = {str(reason) for reason in (safety_status.get("reasons") or [])}
        if not live_safety_reasons.issubset({"lichtgitter"}):
            return False
        last_reasons = {str(reason) for reason in (safety_info.get("last_reasons") or [])}
        last_reset = safety_info.get("last_reset") if isinstance(safety_info.get("last_reset"), dict) else {}
        last_reset_reasons = {str(reason) for reason in (last_reset.get("initial_reasons") or [])}
        light_latch_history = last_reasons == {"lichtgitter"} or last_reset_reasons == {"lichtgitter"}
        if not light_latch_history:
            return False
        return bool(safety_info.get("latched") or mas0028_active)

    def _laser_printer_active(self, param_map: dict[str, str] | None = None) -> bool:
        values = param_map if param_map is not None else self._param_values_by_prefix(("MAP",))
        return _truthy((values or {}).get("MAP0016", "0"))

    def _laser_reset_interlock_status(
        self,
        *,
        param_map: dict[str, str] | None = None,
        io_map: dict[tuple[str, str], str] | None = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        laser_active = self._laser_printer_active(param_map)
        status: dict[str, Any] = {
            "laser_active": laser_active,
            "active_printer": "laser" if laser_active else "tto",
            "blocked": False,
        }
        if not laser_active:
            return status

        refresh_result: dict[str, Any] | None = None
        if refresh:
            refresh_result = self._refresh_single_io_device("esp32_plc58", {LASER_SYSTEM_READY_PIN})
        if io_map is None or ("esp32_plc58", LASER_SYSTEM_READY_PIN) not in io_map:
            io_map = self._io_values_for_pins({("esp32_plc58", LASER_SYSTEM_READY_PIN)})

        detail = self._io_point_detail(
            io_map,
            "esp32_plc58",
            LASER_SYSTEM_READY_PIN,
            default=False,
        )
        point_defined = bool(self.io_store.get_point(f"esp32_plc58__{LASER_SYSTEM_READY_PIN.replace('.', '_')}"))
        refresh_ok = True if refresh_result is None else bool(refresh_result.get("ok", False))
        system_ready = bool(detail.get("active")) and point_defined and refresh_ok
        status.update(
            {
                "system_ready": system_ready,
                "system_ready_input": detail,
                "system_ready_defined": point_defined,
            }
        )
        if refresh_result is not None:
            status["refresh"] = refresh_result
        if not refresh_ok:
            status["blocked"] = True
            status["reason"] = "laser_system_ready_refresh_failed"
            status["message"] = (
                f"Laser System Ready ESP32 PLC 58 {LASER_SYSTEM_READY_PIN} konnte nicht live gelesen werden; "
                "Safety-Reset Laser gesperrt"
            )
        elif not system_ready:
            status["blocked"] = True
            status["reason"] = "laser_system_ready_low"
            status["message"] = (
                f"Laser System Ready ESP32 PLC 58 {LASER_SYSTEM_READY_PIN} ist LOW; "
                "Safety-Reset Laser gesperrt"
            )
        return status

    def _ensure_laser_reset_interlock_clear(self, *, source: str) -> dict[str, Any]:
        interlock = self._laser_reset_interlock_status(refresh=True)
        if bool(interlock.get("blocked")):
            message = str(interlock.get("message") or "Laser System Ready fehlt")
            self.logs.log("machine", "warning", f"{source}: {message}")
            self._record_event(
                "laser_safety_reset_blocked",
                "warning",
                message,
                {"source": source, **interlock},
            )
            raise RuntimeError(message)
        return interlock

    def _perform_light_curtain_auto_reset(self, ts: float) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": False,
            "source": "light_curtain_auto_reset",
            "started_ts": now_ts(),
            "steps": [],
        }
        if not _SAFETY_RESET_LOCK.acquire(blocking=False):
            result["in_progress"] = True
            result["error"] = "manual safety reset already in progress"
            return result
        try:
            self._pulse_esp_reset_output()
            result["steps"].append({"step": "esp_q0_2_reset_pulse", "ok": True})
            result["ok"] = True
            result["finished_ts"] = now_ts()
        except Exception as exc:
            result["steps"].append({"step": "esp_q0_2_reset_pulse", "ok": False, "error": str(exc)})
            result["error"] = f"ESP reset pulse failed: {exc}"
        finally:
            _SAFETY_RESET_LOCK.release()
        return result

    def _remember_light_curtain_auto_reset(
        self,
        safety_info: dict[str, Any],
        ts: float,
        reset_result: dict[str, Any],
    ) -> None:
        try:
            count = int(safety_info.get("light_curtain_auto_reset_count") or 0)
        except Exception:
            count = 0
        reset_result["source"] = "light_curtain_auto_reset"
        safety_info["light_curtain_auto_reset_last_ts"] = float(ts)
        safety_info["light_curtain_auto_reset_count"] = count + 1
        safety_info["last_auto_reset"] = {
            "source": "light_curtain_auto_reset",
            "reason": "lichtgitter",
            "ts": float(ts),
            "ok": bool(reset_result.get("ok")),
            "error": reset_result.get("error"),
        }

    def _light_curtain_wickler_recovery_due(
        self,
        *,
        current_state: int,
        requested_state: int,
        safety_status: dict[str, Any],
        critical_reasons: list[str],
        safety_info: dict[str, Any],
        info: dict[str, Any],
        mas0028_active: bool,
        ts: float,
    ) -> bool:
        if external_purge_active(info):
            return False
        if mas0028_active:
            return False
        if critical_reasons:
            return False
        if self._blocking_safety_active(safety_status):
            return False
        if bool(safety_status.get("light_curtain_active")):
            return False
        if int(current_state or 0) != 7 or int(requested_state or 0) != 7:
            return False
        if bool(safety_info.get("light_curtain_wickler_recovery_running")):
            return False

        last_auto_reset = safety_info.get("last_auto_reset")
        if not isinstance(last_auto_reset, dict):
            return False
        if str(last_auto_reset.get("reason") or "") != "lichtgitter":
            return False
        if not bool(last_auto_reset.get("ok")):
            return False
        auto_reset_ts = _safe_float(last_auto_reset.get("ts"), 0.0)
        if auto_reset_ts <= 0.0:
            return False
        if (float(ts) - auto_reset_ts) > LIGHT_CURTAIN_WICKLER_RECOVERY_WINDOW_S:
            return False
        recovered_ts = _safe_float(safety_info.get("light_curtain_wickler_recovery_last_auto_reset_ts"), 0.0)
        if recovered_ts >= auto_reset_ts:
            return False
        last_attempt_ts = _safe_float(safety_info.get("light_curtain_wickler_recovery_last_attempt_ts"), 0.0)
        if last_attempt_ts > 0.0 and (float(ts) - last_attempt_ts) < LIGHT_CURTAIN_WICKLER_RECOVERY_RETRY_INTERVAL_S:
            return False
        return True

    def _start_light_curtain_wickler_recovery_background(self, auto_reset_ts: float) -> dict[str, Any]:
        auto_reset_ts = float(auto_reset_ts or 0.0)
        if not _LIGHT_CURTAIN_WICKLER_RECOVERY_LOCK.acquire(blocking=False):
            return {
                "ok": True,
                "queued": False,
                "in_progress": True,
                "auto_reset_ts": auto_reset_ts,
            }

        def worker():
            payload: dict[str, Any] = {
                "ok": False,
                "source": "light_curtain_wickler_recovery",
                "auto_reset_ts": auto_reset_ts,
                "started_ts": now_ts(),
            }
            try:
                try:
                    wicklers = self._set_production_wicklers_idle(
                        target_state=7,
                        recover_after_safety=True,
                    )
                    payload["wicklers"] = wicklers
                    payload["ok"] = all(bool(item.get("ok")) for item in wicklers)
                    if not bool(payload["ok"]):
                        payload["error"] = "wicklers did not return to ready"
                except Exception as exc:
                    payload["error"] = str(exc)
                payload["finished_ts"] = now_ts()
                payload["duration_s"] = round(
                    max(0.0, float(payload["finished_ts"]) - float(payload["started_ts"])),
                    3,
                )
                ok = bool(payload.get("ok"))
                severity = "info" if ok else "warning"
                message = (
                    "Lichtgitter-Pause: Wickler wieder freigegeben"
                    if ok
                    else "Lichtgitter-Pause: Wicklerfreigabe noch nicht stabil"
                )
                self.logs.log("machine", severity, f"{message}: {payload.get('error') or 'ok'}")
                self._record_event("light_curtain_wickler_recovery", severity, message, payload)
                try:
                    snapshot = self._state_row()
                    info = dict(snapshot.get("info") or {})
                    safety_info = dict(info.get("safety") or {})
                    safety_info["light_curtain_wickler_recovery_running"] = False
                    safety_info["light_curtain_wickler_recovery_last_attempt_ts"] = float(payload["finished_ts"])
                    safety_info["last_light_curtain_wickler_recovery"] = payload
                    if ok:
                        safety_info["light_curtain_wickler_recovery_last_auto_reset_ts"] = auto_reset_ts
                    info["safety"] = safety_info
                    self._write_state(
                        current_state=int(snapshot.get("current_state") or 1),
                        requested_state=int(snapshot.get("requested_state") or snapshot.get("current_state") or 1),
                        state_source=str(snapshot.get("state_source") or "runtime"),
                        warning_active=bool(snapshot.get("warning_active")),
                        purge_active=bool(snapshot.get("purge_active")),
                        production_label=str(snapshot.get("production_label") or ""),
                        last_label_no=int(snapshot.get("last_label_no") or 0),
                        info=info,
                    )
                except Exception as exc:
                    self.logs.log("machine", "info", f"light curtain Wickler recovery state update skipped: {exc}")
            finally:
                _LIGHT_CURTAIN_WICKLER_RECOVERY_LOCK.release()

        thread = threading.Thread(target=worker, name="mas004-light-curtain-wickler-recovery", daemon=True)
        thread.start()
        return {
            "ok": True,
            "queued": True,
            "in_progress": True,
            "auto_reset_ts": auto_reset_ts,
        }

    def _safety_status(self, io_map: dict[tuple[str, str], str]) -> dict[str, Any]:
        estop_ok = self._bool_io(io_map, "esp32_plc58", "I0.7", default=False)
        light_curtain_ok = self._bool_io(io_map, "esp32_plc58", "I0.8", default=False)
        ups_input = self._io_point_detail(io_map, "raspi_plc21", "I0.6", default=True)
        ups_ok = bool(ups_input.get("active"))
        estop_active = not estop_ok
        light_curtain_active = not light_curtain_ok
        reasons: list[str] = []
        if estop_active:
            reasons.append("notaus")
        if light_curtain_active:
            reasons.append("lichtgitter")
        blocking_reasons = ["notaus"] if estop_active else []
        return {
            "active": bool(reasons),
            "blocking_active": bool(blocking_reasons),
            "blocking_reasons": blocking_reasons,
            "estop_active": estop_active,
            "light_curtain_active": light_curtain_active,
            "estop_ok": estop_ok,
            "light_curtain_ok": light_curtain_ok,
            "ups_ok": ups_ok,
            "ups_not_ok": not ups_ok,
            "ups_input": ups_input,
            "reasons": reasons,
        }

    def _pause_light_curtain_safety_drop(
        self,
        *,
        snapshot: dict[str, Any],
        safety_status: dict[str, Any],
        info: dict[str, Any],
    ) -> bool:
        current_state = _safe_int((snapshot or {}).get("current_state"), 0)
        requested_state = _safe_int((snapshot or {}).get("requested_state"), current_state)
        if current_state not in (6, 7):
            return False
        if requested_state not in (7, current_state):
            return False
        if bool((snapshot or {}).get("purge_active")):
            return False
        safety_info = dict((info or {}).get("safety") or {})
        if bool(safety_info.get("latched")) or str(safety_info.get("phase") or "") in {
            SAFETY_PHASE_LATCHED,
            SAFETY_PHASE_RESETTING,
            SAFETY_PHASE_FAILED,
        }:
            return False
        return bool(safety_status.get("light_curtain_active")) and bool(safety_status.get("estop_active"))

    def _mask_estop_for_pause_light_curtain(self, safety_status: dict[str, Any]) -> dict[str, Any]:
        masked = dict(safety_status or {})
        reasons = [str(reason) for reason in (masked.get("reasons") or []) if str(reason) != "notaus"]
        if "lichtgitter" not in reasons:
            reasons.append("lichtgitter")
        masked["raw_estop_active"] = bool(masked.get("estop_active"))
        masked["estop_masked_by_pause_light_curtain"] = True
        masked["estop_active"] = False
        masked["estop_ok"] = True
        masked["blocking_active"] = False
        masked["blocking_reasons"] = []
        masked["reasons"] = reasons
        masked["active"] = bool(reasons)
        return masked

    def _critical_state(
        self,
        io_map: dict[tuple[str, str], str],
        param_map: dict[str, str],
        *,
        band_break_bypass: bool = False,
        ignore_estop: bool = False,
    ) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        machine_state = _safe_int(param_map.get("MAS0001"), 1)
        monitor_band_break = band_break_monitoring_active(machine_state) and not bool(band_break_bypass)
        if not bool(ignore_estop) and not self._bool_io(io_map, "esp32_plc58", "I0.7", default=False):
            reasons.append("notaus")
        if not self._bool_io(io_map, "raspi_plc21", "I0.6", default=True):
            reasons.append("usv_not_ok")
        if monitor_band_break and self._bool_io(io_map, "esp32_plc58", "I0.4", default=False):
            reasons.append("bahnriss_einlauf")
        if monitor_band_break and self._bool_io(io_map, "esp32_plc58", "I0.11", default=False):
            reasons.append("bahnriss_auswurf")
        for pkey, value in param_map.items():
            if pkey in PAUSE_ERROR_KEYS:
                continue
            if pkey in BAND_BREAK_ERROR_KEYS and not monitor_band_break:
                continue
            if pkey in WICKLER_DANCER_ERROR_KEYS and machine_state not in WICKLER_DANCER_MONITOR_STATES:
                continue
            if pkey == "MAE0027" and _safe_int(param_map.get("MAS0001"), 1) not in PROCESS_SENSOR_FAULT_STATES:
                continue
            if pkey.startswith("MAE") and _truthy(value):
                reasons.append(pkey)
        return bool(reasons), reasons

    def _clear_resettable_fault_latches_after_external_purge_clear(
        self,
        *,
        io_map: dict[tuple[str, str], str],
        param_map: dict[str, str],
        critical_reasons: list[str],
        ts: float,
    ) -> list[str]:
        # MAS0028=0 from Microtom/simulator is a deliberate Purge reset.  Do
        # not let stale software MAE bits immediately recreate MAS0028=1; live
        # safety inputs are kept and will relatch the purge in the same tick.
        machine_state = _safe_int(param_map.get("MAS0001"), 1)
        monitor_band_break = band_break_monitoring_active(machine_state)
        live_blockers = {
            "notaus",
            "usv_not_ok",
            "bahnriss_einlauf",
            "bahnriss_auswurf",
        }
        if any(reason in live_blockers for reason in critical_reasons):
            return []

        cleared: list[str] = []
        for pkey in sorted({str(reason).strip().upper() for reason in critical_reasons}):
            if pkey not in RESETTABLE_SAFETY_ERROR_KEYS:
                continue
            live_point = CONDITIONAL_RESETTABLE_SAFETY_ERRORS.get(pkey)
            if live_point and monitor_band_break and self._bool_io(io_map, live_point[0], live_point[1], default=False):
                continue
            if not _truthy(param_map.get(pkey, "0")):
                continue
            self.params.apply_device_value(pkey, "0", promote_default=True)
            self._notify_microtom(pkey, "0", dedupe_key=f"machine:{pkey}")
            cleared.append(pkey)

        if cleared:
            reason_text = self._describe_runtime_reasons(cleared)
            self.logs.log(
                "machine",
                "info",
                f"Externer MAS0028-Reset hat stale Fehler-Latches geloescht: {reason_text}",
            )
            self._record_event(
                "external_purge_clear_fault_latches_cleared",
                "info",
                f"Externer MAS0028-Reset hat stale Fehler-Latches geloescht: {reason_text}",
                {"cleared": cleared, "ts": float(ts)},
            )
        return cleared

    def _perform_safety_reset(self, safety_status: dict[str, Any], ts: float) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": False,
            "started_ts": now_ts(),
            "initial_reasons": list(safety_status.get("reasons") or []),
            "steps": [],
        }
        try:
            laser_interlock = self._ensure_laser_reset_interlock_clear(source="safety_reset")
            result["laser_reset_interlock"] = laser_interlock
        except Exception as exc:
            result["steps"].append({"step": "verify_laser_system_ready", "ok": False, "error": str(exc)})
            result["error"] = str(exc)
            result["finished_ts"] = now_ts()
            return result

        self.logs.log("machine", "info", "safety reset requested")
        self.params.apply_device_value("MAS0001", "8", promote_default=True)
        self._notify_microtom("MAS0001", "8", dedupe_key="machine:MAS0001")
        self._record_event(
            "safety_reset",
            "info",
            "Safety-Reset gestartet: ESP Q0.2 Reset-Sequenz, danach Motor-/Wickler-Recovery im Hintergrund",
            {"initial_reasons": result["initial_reasons"]},
        )
        snapshot = self._state_row()
        reset_info = dict(snapshot.get("info") or {})
        reset_info["safety"] = {
            **dict(reset_info.get("safety") or {}),
            "latched": True,
            "phase": SAFETY_PHASE_RESETTING,
            "last_reasons": list(result["initial_reasons"]),
            "last_reset": {"ok": False, "in_progress": True, "started_ts": result["started_ts"]},
        }
        self._write_state(
            current_state=8,
            requested_state=8,
            state_source="safety_reset_running",
            warning_active=False,
            purge_active=True,
            production_label=str(snapshot.get("production_label") or ""),
            last_label_no=int(snapshot.get("last_label_no") or 0),
            info=reset_info,
        )
        self._apply_status_lamp(8, warning_active=False, ts=ts)

        try:
            self._pulse_esp_reset_output()
            result["steps"].append({"step": "esp_q0_2_reset_pulse", "ok": True})
        except Exception as exc:
            result["steps"].append({"step": "esp_q0_2_reset_pulse", "ok": False, "error": str(exc)})
            result["error"] = f"ESP reset pulse failed: {exc}"
            return result

        refreshed = self._refresh_single_io_device("esp32_plc58")
        result["steps"].append({"step": "refresh_esp_io", "ok": bool(refreshed.get("ok", True)), "detail": refreshed})
        refreshed_io = self._io_values()
        refreshed_safety = self._safety_status(refreshed_io)
        refreshed_blocking_reasons = self._blocking_safety_reasons(refreshed_safety)
        if refreshed_blocking_reasons:
            result["steps"].append(
                {"step": "verify_safety_inputs_high_ok", "ok": False, "reasons": refreshed_blocking_reasons}
            )
            result["error"] = "ESP safety input still LOW/not OK after reset sequence: " + ",".join(refreshed_blocking_reasons)
            return result
        result["steps"].append({"step": "verify_safety_inputs_high_ok", "ok": True})

        if bool((result.get("laser_reset_interlock") or {}).get("laser_active")):
            laser_reset = self._perform_laser_safety_reset_and_start()
            result["steps"].append({"step": "laser_safety_reset_and_start", **laser_reset})
            if not laser_reset.get("ok"):
                result["error"] = laser_reset.get("error") or "Laser safety reset/start sequence failed"
                return result

        process_reset = self._reset_esp_process_runtime()
        result["steps"].append({"step": "esp_process_reset", **process_reset})
        if not process_reset.get("ok"):
            result["error"] = process_reset.get("error") or "ESP process reset failed"
            return result

        # Clear the soft Purge latch as soon as the safety inputs and ESP process
        # latches are quiet. Motion recovery can still fail afterwards, but that
        # must not keep an old MAS0028=1 alive without a real critical reason.
        clear_result = self._clear_resettable_safety_errors(io_map=self._io_values())
        result["steps"].append(
            {
                "step": "clear_resettable_safety_errors",
                "ok": not bool(clear_result.get("kept")),
                **clear_result,
            }
        )
        result["cleared_errors"] = clear_result.get("cleared", [])
        result["kept_errors"] = clear_result.get("kept", [])

        motion_result = self._start_reset_motion_recovery_background()
        result["steps"].append({"step": "reset_motion_devices_background", **motion_result})

        self.params.apply_device_value("MAS0001", "9", promote_default=True)
        self._notify_microtom("MAS0001", "9", dedupe_key="machine:MAS0001")
        self._record_event(
            "safety_reset",
            "info",
            "Safety-Reset abgeschlossen: Motoren geprueft, MAS0001=9",
            motion_result,
        )
        self._apply_status_lamp(9, warning_active=False, ts=now_ts())
        result["ok"] = True
        result["finished_ts"] = now_ts()
        return result

    def _start_reset_motion_recovery_background(self) -> dict[str, Any]:
        if not _RESET_MOTION_RECOVERY_LOCK.acquire(blocking=False):
            return {"ok": True, "queued": False, "in_progress": True}

        def worker():
            try:
                started_ts = now_ts()
                try:
                    motion_result = self._reset_motion_devices()
                except Exception as exc:
                    motion_result = {"ok": False, "error": str(exc), "details": {}}
                finished_ts = now_ts()
                payload = {
                    **motion_result,
                    "started_ts": started_ts,
                    "finished_ts": finished_ts,
                    "duration_s": round(max(0.0, finished_ts - started_ts), 3),
                }
                ok = bool(motion_result.get("ok"))
                severity = "info" if ok else "warning"
                message = (
                    "Safety-Reset Motion-Recovery abgeschlossen"
                    if ok
                    else "Safety-Reset Motion-Recovery mit Warnung abgeschlossen"
                )
                self.logs.log("machine", severity, f"{message}: {motion_result.get('error') or 'ok'}")
                self._record_event("reset_motion_recovery", severity, message, payload)
                try:
                    snapshot = self._state_row()
                    info = dict(snapshot.get("info") or {})
                    safety_info = dict(info.get("safety") or {})
                    safety_info["last_motion_recovery"] = payload
                    current_state = int(snapshot.get("current_state") or 1)
                    requested_state = int(snapshot.get("requested_state") or snapshot.get("current_state") or 1)
                    state_source = str(snapshot.get("state_source") or "runtime")
                    purge_active = bool(snapshot.get("purge_active"))
                    if ok:
                        try:
                            live_io = self._io_values()
                            live_params = self._param_values_by_prefix(("MAP", "MAS", "MAE", "MAW"))
                            live_critical, live_reasons = self._critical_state(
                                live_io,
                                live_params,
                                band_break_bypass=quick_setup_band_break_bypass_active(snapshot.get("info") or {}),
                            )
                            live_purge = _truthy(live_params.get("MAS0028", "0"))
                        except Exception:
                            live_critical = False
                            live_reasons = []
                            live_purge = bool(snapshot.get("purge_active"))
                        if not live_critical and not live_purge:
                            safety_info = {
                                **safety_info,
                                "latched": False,
                                "phase": SAFETY_PHASE_READY,
                                "last_reasons": [],
                            }
                            purge_active = False
                            if current_state in (8, 20, 21):
                                current_state = 9
                                requested_state = 9
                                state_source = "reset_motion_recovery_ready"
                        else:
                            safety_info = {
                                **safety_info,
                                "latched": True,
                                "phase": SAFETY_PHASE_LATCHED,
                                "last_reasons": list(live_reasons),
                            }
                            purge_active = True
                    info["safety"] = safety_info
                    self._write_state(
                        current_state=current_state,
                        requested_state=requested_state,
                        state_source=state_source,
                        warning_active=bool(snapshot.get("warning_active")),
                        purge_active=purge_active,
                        production_label=str(snapshot.get("production_label") or ""),
                        last_label_no=int(snapshot.get("last_label_no") or 0),
                        info=info,
                    )
                except Exception as exc:
                    self.logs.log("machine", "info", f"reset motion recovery state update skipped: {exc}")
            finally:
                _RESET_MOTION_RECOVERY_LOCK.release()

        thread = threading.Thread(target=worker, name="mas004-reset-motion-recovery", daemon=True)
        thread.start()
        return {"ok": True, "queued": True, "in_progress": True}

    def _resettable_position_axis_faults_ready(self) -> tuple[list[str], list[dict[str, Any]]]:
        details: list[dict[str, Any]] = []
        cleared: list[str] = []
        if bool(getattr(self.cfg, "esp_simulation", False)):
            return cleared, [{"ok": True, "skipped": True, "reason": "esp_simulation"}]
        client = EspMotorClient(self.cfg)
        if not client.available():
            return cleared, [{"ok": True, "skipped": True, "reason": "esp_motor_endpoint_unavailable"}]
        for motor_id, pkey in sorted(POSITION_AXIS_MAE_BY_MOTOR.items()):
            try:
                if not _truthy(self.params.get_effective_value(pkey)):
                    continue
                payload = client.refresh(motor_id)
                motor = payload.get("motor") if isinstance(payload, dict) else {}
                state = (motor or {}).get("state") if isinstance(motor, dict) else {}
                state = state if isinstance(state, dict) else {}
                status_ok = bool(payload.get("ok", True)) if isinstance(payload, dict) else False
                ready = self._esp_motor_reset_ready(motor_id, state, status_ok)
                detail = {
                    "motor_id": motor_id,
                    "pkey": pkey,
                    "ok": ready,
                    "status_ok": status_ok,
                    "link_ok": state.get("link_ok"),
                    "ready": state.get("ready"),
                    "alarm": state.get("alarm"),
                    "alarm_code": state.get("alarm_code"),
                    "feedback_tenths_mm": state.get("feedback_tenths_mm"),
                }
                details.append(detail)
                if ready:
                    cleared.append(pkey)
            except Exception as exc:
                details.append({"motor_id": motor_id, "pkey": pkey, "ok": False, "error": str(exc)})
        return cleared, details

    def _clear_resettable_safety_errors(
        self, *, io_map: dict[tuple[str, str], str] | None = None
    ) -> dict[str, Any]:
        state_info = dict(self._state_row().get("info") or {})
        keep_external_purge = external_purge_active(state_info)
        cleared = sorted(RESETTABLE_SAFETY_ERROR_KEYS)
        skipped: list[str] = []
        if keep_external_purge:
            skipped.append("MAS0028")
        else:
            cleared.insert(0, "MAS0028")
        motor_faults_cleared, motor_fault_details = self._resettable_position_axis_faults_ready()
        for pkey in motor_faults_cleared:
            if pkey not in cleared:
                cleared.append(pkey)
        kept: list[str] = []
        machine_state = _safe_int(self._state_row().get("current_state"), 1)
        monitor_band_break = band_break_monitoring_active(machine_state)
        for pkey, (device_code, pin_label) in sorted(CONDITIONAL_RESETTABLE_SAFETY_ERRORS.items()):
            if pkey in motor_faults_cleared:
                continue
            if (
                monitor_band_break
                and io_map is not None
                and self._bool_io(io_map, device_code, pin_label, default=False)
            ):
                kept.append(pkey)
                continue
            cleared.append(pkey)
        for pkey in cleared:
            was_active = _truthy(self.params.get_effective_value(pkey))
            self.params.apply_device_value(pkey, "0", promote_default=True)
            if pkey != "MAS0028" and was_active:
                self._notify_microtom(pkey, "0", dedupe_key=f"machine:{pkey}")
        self.logs.log("machine", "info", "resettable safety errors cleared: " + ",".join(cleared))
        if kept:
            self.logs.log("machine", "warning", "resettable safety errors kept active: " + ",".join(kept))
        result = {"cleared": cleared, "kept": kept}
        if skipped:
            result["skipped"] = skipped
        if motor_fault_details:
            result["position_axis_faults"] = motor_fault_details
        return result

    def _pulse_esp_reset_output(self):
        io_runtime = IoRuntime(self.cfg, self.io_store)
        point = self.io_store.get_point("esp32_plc58__Q0_2")
        if not point:
            raise RuntimeError("ESP reset output Q0.2 is not defined in IO master")
        io_runtime.write_output(point["io_key"], True, force=True, source="safety-reset")
        time.sleep(ESP_RESET_PULSE_HIGH_S)
        io_runtime.write_output(point["io_key"], False, force=True, source="safety-reset")
        time.sleep(ESP_RESET_PULSE_GAP_S)
        io_runtime.write_output(point["io_key"], True, force=True, source="safety-reset")
        time.sleep(ESP_RESET_PULSE_HIGH_S)
        io_runtime.write_output(point["io_key"], False, force=True, source="safety-reset")

    def _pulse_io_output(self, io_key: str, *, high_s: float, source: str) -> dict[str, Any]:
        point = self.io_store.get_point(io_key)
        if not point:
            raise RuntimeError(f"IO output {io_key} is not defined in IO master")
        io_runtime = IoRuntime(self.cfg, self.io_store)
        high_done = False
        try:
            high = io_runtime.write_output(point["io_key"], True, force=True, source=source)
            high_done = True
            time.sleep(max(0.01, float(high_s or 0.0)))
            low = io_runtime.write_output(point["io_key"], False, force=True, source=source)
            return {
                "ok": True,
                "io_key": point["io_key"],
                "device_code": point.get("device_code"),
                "pin_label": point.get("pin_label"),
                "high_s": float(high_s or 0.0),
                "high": high,
                "low": low,
            }
        finally:
            if high_done:
                try:
                    io_runtime.write_output(point["io_key"], False, force=True, source=source)
                except Exception as exc:
                    self.logs.log("machine", "warning", f"{source}: failed to force {io_key} LOW after pulse: {exc}")

    def _wait_for_esp_input_high(self, pin_label: str, *, timeout_s: float, poll_s: float) -> dict[str, Any]:
        pin = str(pin_label or "").upper()
        started = now_ts()
        deadline = started + max(0.1, float(timeout_s or 0.0))
        attempts: list[dict[str, Any]] = []
        while True:
            refresh = self._refresh_single_io_device("esp32_plc58", {pin})
            io_map = self._io_values_for_pins({("esp32_plc58", pin)})
            detail = self._io_point_detail(io_map, "esp32_plc58", pin, default=False)
            attempt = {
                "ts": now_ts(),
                "ok": bool(detail.get("active")),
                "value": detail.get("value"),
                "quality": detail.get("quality"),
                "refresh_ok": bool(refresh.get("ok", False)),
            }
            attempts.append(attempt)
            if bool(detail.get("active")):
                return {
                    "ok": True,
                    "pin_label": pin,
                    "detail": detail,
                    "attempts": attempts[-5:],
                    "duration_s": round(max(0.0, now_ts() - started), 3),
                }
            if now_ts() >= deadline:
                return {
                    "ok": False,
                    "pin_label": pin,
                    "detail": detail,
                    "attempts": attempts[-5:],
                    "duration_s": round(max(0.0, now_ts() - started), 3),
                    "error": f"ESP32 PLC 58 {pin} did not go HIGH within {float(timeout_s or 0.0):.1f}s",
                }
            time.sleep(max(0.02, float(poll_s or 0.0)))

    def _perform_laser_safety_reset_and_start(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": False,
            "started_ts": now_ts(),
            "steps": [],
        }
        try:
            interlock = self._ensure_laser_reset_interlock_clear(source="laser_safety_reset")
            result["interlock"] = interlock
        except Exception as exc:
            result["steps"].append({"step": "verify_laser_system_ready", "ok": False, "error": str(exc)})
            result["error"] = str(exc)
            result["finished_ts"] = now_ts()
            return result

        try:
            pulse = self._pulse_io_output(
                LASER_SAFETY_RESET_IO_KEY,
                high_s=LASER_SAFETY_RESET_PULSE_HIGH_S,
                source="laser-safety-reset",
            )
            result["steps"].append({"step": "laser_safety_reset_pulse", **pulse})
        except Exception as exc:
            result["steps"].append({"step": "laser_safety_reset_pulse", "ok": False, "error": str(exc)})
            result["error"] = f"Laser safety reset pulse failed: {exc}"
            result["finished_ts"] = now_ts()
            return result

        ready = self._wait_for_esp_input_high(
            LASER_READY_PIN,
            timeout_s=LASER_READY_WAIT_TIMEOUT_S,
            poll_s=LASER_READY_WAIT_POLL_S,
        )
        result["steps"].append({"step": "wait_laser_ready", **ready})
        if not ready.get("ok"):
            result["error"] = ready.get("error") or "Laser Ready did not become HIGH"
            result["finished_ts"] = now_ts()
            return result

        try:
            start = self._pulse_io_output(
                f"esp32_plc58__{LASER_START_PIN.replace('.', '_')}",
                high_s=LASER_START_PULSE_HIGH_S,
                source="laser-start",
            )
            result["steps"].append({"step": "laser_start_pulse", **start})
        except Exception as exc:
            result["steps"].append({"step": "laser_start_pulse", "ok": False, "error": str(exc)})
            result["error"] = f"Laser start pulse failed: {exc}"
            result["finished_ts"] = now_ts()
            return result

        result["ok"] = True
        result["finished_ts"] = now_ts()
        result["duration_s"] = round(max(0.0, result["finished_ts"] - result["started_ts"]), 3)
        return result

    def _refresh_single_io_device(self, device_code: str, pin_labels: set[str] | None = None) -> dict[str, Any]:
        wanted_pins = {str(pin or "").upper() for pin in (pin_labels or set())}
        points = [
            point
            for point in self.io_store.list_points(device_code=str(device_code or ""), include_reserved=True)
            if str(point.get("device_code") or "") == str(device_code or "")
            and (not wanted_pins or str(point.get("pin_label") or "").upper() in wanted_pins)
        ]
        if not points:
            return {"ok": False, "error": f"no IO points for {device_code}"}
        try:
            device_result = IoRuntime(self.cfg, self.io_store)._refresh_device(device_code, points)
            return {"ok": True, "device": device_result}
        except Exception as exc:
            self.logs.log("machine", "warning", f"single IO refresh failed for {device_code}: {exc}")
            return {"ok": False, "error": str(exc)}

    def _reset_esp_process_runtime(self) -> dict[str, Any]:
        if bool(getattr(self.cfg, "esp_simulation", True)):
            return {"ok": True, "skipped": True, "reason": "esp_simulation"}
        if not getattr(self.cfg, "esp_host", "") or int(getattr(self.cfg, "esp_port", 0) or 0) <= 0:
            return {"ok": True, "skipped": True, "reason": "esp_endpoint_missing"}
        attempts: list[dict[str, Any]] = []
        read_timeout_s = min(1.5, float(getattr(self.cfg, "esp_command_timeout_s", 8.0) or 8.0))
        for attempt_no in (1, 2):
            try:
                client = EspPlcClient(
                    str(self.cfg.esp_host),
                    int(self.cfg.esp_port),
                    float(getattr(self.cfg, "esp_connect_timeout_s", 1.5) or 1.5),
                )
                reply = client.exchange_line("PROCESS RESET", read_timeout_s=read_timeout_s)
                ok = str(reply or "").strip().upper().startswith("ACK_PROCESS_RESET")
                attempts.append({"attempt": attempt_no, "ok": ok, "reply": reply})
                if ok:
                    return {"ok": True, "reply": reply, "attempts": attempts}
            except Exception as exc:
                attempts.append({"attempt": attempt_no, "ok": False, "error": str(exc)})
            time.sleep(0.2)
        last = attempts[-1] if attempts else {}
        return {
            "ok": False,
            "attempts": attempts,
            "reply": last.get("reply"),
            "error": last.get("error") or f"unexpected ESP reply: {last.get('reply')}",
        }

    @staticmethod
    def _esp_motor_reset_ready(motor_id: int, state: dict[str, Any], status_ok: bool) -> bool:
        if not (
            bool(status_ok)
            and bool(state.get("link_ok") or state.get("linkOk"))
            and not bool(state.get("alarm"))
        ):
            return False
        if int(motor_id) == 3:
            # Motor 3 uses the productive hardware START/STOP input path. On
            # this AZD-CD path the READY diagnostic bit can stay false although
            # the drive is usable; the measuring/production moves are verified
            # by exact stop-position feedback instead.
            return not bool(state.get("hwto"))
        return bool(state.get("ready"))

    @staticmethod
    def _wickler_reset_safe_stop(state_ok: bool, drive: dict[str, Any], mode_label: str, safe_stop_fault: bool) -> bool:
        if not (bool(state_ok) and bool(drive.get("online")) and not bool(drive.get("alarm")) and safe_stop_fault):
            return False
        if bool(drive.get("ready")):
            return True
        # Reset must not start the Wickler regulation. In Stop mode the AZD can
        # report ready=false while the Wickler is intentionally stopped and
        # alarm-free; accept this as safe stop as long as no movement is active.
        return str(mode_label or "").strip().lower() == "stop" and not bool(drive.get("move"))

    def _reset_motion_devices(self) -> dict[str, Any]:
        details: dict[str, Any] = {"esp_motors": [], "wicklers": []}
        hard_failures: list[str] = []

        esp = EspMotorClient(self.cfg)
        if esp.available():
            for label, action in (
                ("apply_eto_recovery", esp.apply_eto_recovery),
                ("recover_eto", esp.recover_eto),
            ):
                try:
                    reply = action()
                    details["esp_motors"].append({"step": label, **reply})
                except Exception as exc:
                    details["esp_motors"].append({"step": label, "ok": False, "error": str(exc)})
            for motor_id in range(1, 10):
                try:
                    reply = esp.reset_alarm(motor_id)
                    details["esp_motors"].append({"step": "reset_alarm", "motor_id": motor_id, **reply})
                except Exception as exc:
                    details["esp_motors"].append(
                        {"step": "reset_alarm", "motor_id": motor_id, "ok": False, "error": str(exc)}
                    )
                try:
                    reply = esp.recover_eto_motor(motor_id)
                    details["esp_motors"].append({"step": "recover_eto", "motor_id": motor_id, **reply})
                except Exception as exc:
                    details["esp_motors"].append(
                        {"step": "recover_eto", "motor_id": motor_id, "ok": False, "error": str(exc)}
                    )
            time.sleep(0.4)
            for motor_id in range(1, 10):
                verify: dict[str, Any] | None = None
                for attempt in range(1, 5):
                    try:
                        status = esp.refresh(motor_id)
                        motor = status.get("motor") if isinstance(status, dict) else {}
                        state = motor.get("state") if isinstance(motor, dict) else {}
                        state = state if isinstance(state, dict) else {}
                        status_ok = bool(status.get("ok"))
                        verified = self._esp_motor_reset_ready(motor_id, state, status_ok)
                        verify = {
                            "step": "verify_ready",
                            "motor_id": motor_id,
                            "attempt": attempt,
                            "ok": verified,
                            "status_ok": status_ok,
                            "ready": bool(state.get("ready")),
                            "ready_required": int(motor_id) != 3,
                            "operable": verified,
                            "link_ok": bool(state.get("link_ok")),
                            "alarm": bool(state.get("alarm")),
                            "alarm_code": state.get("alarm_code"),
                            "input_raw_hex": state.get("input_raw_hex"),
                            "output_raw_hex": state.get("output_raw_hex"),
                            "monitor0179_hex": state.get("monitor0179_hex"),
                            "monitor017b_hex": state.get("monitor017b_hex"),
                            "monitor017d_hex": state.get("monitor017d_hex"),
                            "mps": state.get("mps"),
                            "mbc": state.get("mbc"),
                            "hwto": state.get("hwto"),
                        }
                        details["esp_motors"].append(verify)
                        if verify["ok"]:
                            break
                        if verify["alarm"]:
                            reply = esp.reset_alarm(motor_id)
                            details["esp_motors"].append(
                                {"step": "retry_reset_alarm", "motor_id": motor_id, "attempt": attempt, **reply}
                            )
                        reply = esp.recover_eto_motor(motor_id)
                        details["esp_motors"].append(
                            {"step": "retry_recover_eto", "motor_id": motor_id, "attempt": attempt, **reply}
                        )
                        time.sleep(0.25)
                    except Exception as exc:
                        details["esp_motors"].append(
                            {
                                "step": "verify_ready",
                                "motor_id": motor_id,
                                "attempt": attempt,
                                "ok": False,
                                "error": str(exc),
                            }
                        )
                        verify = {"ok": False, "error": str(exc)}
                        time.sleep(0.25)
                if not verify or not verify.get("ok"):
                    hard_failures.append(
                        "Motor "
                        f"{motor_id} not ready/operable "
                        f"(link={bool((verify or {}).get('link_ok'))}, "
                        f"ready={bool((verify or {}).get('ready'))}, "
                        f"ready_required={bool((verify or {}).get('ready_required'))}, "
                        f"alarm={bool((verify or {}).get('alarm'))}, "
                        f"alarm_code={(verify or {}).get('alarm_code')}, "
                        f"in={(verify or {}).get('input_raw_hex')}, "
                        f"out={(verify or {}).get('output_raw_hex')}, "
                        f"m0179={(verify or {}).get('monitor0179_hex')}, "
                        f"m017b={(verify or {}).get('monitor017b_hex')}, "
                        f"mps={(verify or {}).get('mps')}, "
                        f"mbc={(verify or {}).get('mbc')}, "
                        f"hwto={(verify or {}).get('hwto')})"
                    )
        else:
            details["esp_motors"].append({"step": "skipped", "ok": True, "reason": "simulation_or_endpoint_missing"})

        for role in ("unwinder", "rewinder"):
            client = SmartWicklerClient(self.cfg, role)
            role_detail: dict[str, Any] = {"role": role, "steps": []}
            if not client.available():
                role_detail["steps"].append({"step": "skipped", "ok": True, "reason": "simulation_or_endpoint_missing"})
                details["wicklers"].append(role_detail)
                continue
            try:
                reply = client.post_master({"indexedModeEnabled": "0"}, timeout_s=8.0)
                role_detail["steps"].append({"step": "disable_indexed_mode", "ok": bool(reply.get("ok", True)), "reply": reply})
                if reply.get("ok") is False:
                    hard_failures.append(f"{role} disable_indexed_mode: {reply}")
            except Exception as exc:
                role_detail["steps"].append({"step": "disable_indexed_mode", "ok": False, "error": str(exc)})
                hard_failures.append(f"{role} disable_indexed_mode: {exc}")
            for mode in ("stop", "resetAlarm", "etoRecovery", "stop"):
                try:
                    reply = client.post_mode(mode, timeout_s=8.0)
                    role_detail["steps"].append({"step": mode, "ok": bool(reply.get("ok", True)), "reply": reply})
                    if reply.get("ok") is False:
                        hard_failures.append(f"{role} {mode}: {reply}")
                except Exception as exc:
                    role_detail["steps"].append({"step": mode, "ok": False, "error": str(exc)})
                    hard_failures.append(f"{role} {mode}: {exc}")
            try:
                state = client.fetch_state()
                drive = state.get("drive") if isinstance(state, dict) else {}
                telemetry = state.get("telemetry") if isinstance(state, dict) else {}
                drive = drive if isinstance(drive, dict) else {}
                telemetry = telemetry if isinstance(telemetry, dict) else {}
                mode_label = str(telemetry.get("modeLabel") or "")
                fault_reason = str(telemetry.get("faultReason") or "")
                normalized_fault = fault_reason.strip().lower()
                safe_stop_fault = normalized_fault in (
                    "",
                    "-",
                    "none",
                    "keine",
                    "ok",
                    "externer wickler-stop aktiv",
                    "azd stop eingang aktiv",
                )
                state_ok = bool(state.get("ok", True))
                verified = self._wickler_reset_safe_stop(state_ok, drive, mode_label, safe_stop_fault)
                verify = {
                    "step": "verify_safe_stop",
                    "ok": verified,
                    "state_ok": state_ok,
                    "online": bool(drive.get("online")),
                    "ready": bool(drive.get("ready")),
                    "ready_required": not (str(mode_label or "").strip().lower() == "stop"),
                    "safe_stop": verified,
                    "alarm": bool(drive.get("alarm")),
                    "alarm_code": drive.get("alarmCode"),
                    "move": bool(drive.get("move")),
                    "mode": mode_label,
                    "fault_reason": fault_reason,
                    "raw_output": drive.get("rawOutput"),
                }
                role_detail["steps"].append(verify)
                if not verify["ok"]:
                    hard_failures.append(
                        f"{role} not in safe stop "
                        f"(online={verify['online']}, ready={verify['ready']}, alarm={verify['alarm']}, "
                        f"alarm_code={verify['alarm_code']}, mode={verify['mode']}, "
                        f"move={verify['move']}, fault={verify['fault_reason']}, raw_output={verify['raw_output']})"
                    )
            except Exception as exc:
                role_detail["steps"].append({"step": "verify_ready", "ok": False, "error": str(exc)})
                hard_failures.append(f"{role} verify_ready: {exc}")
            details["wicklers"].append(role_detail)

        if hard_failures:
            return {"ok": False, "error": "; ".join(hard_failures[:5]), "details": details}
        return {"ok": True, "details": details}

    def _refresh_esp_critical_io_if_stale(self) -> dict[str, Any] | None:
        if bool(getattr(self.cfg, "esp_simulation", False)):
            return None
        now = now_ts()
        stale_pins: list[str] = []
        for pin in sorted(ESP_CRITICAL_IO_PINS):
            point = self.io_store.get_point(f"esp32_plc58__{pin.replace('.', '_')}")
            if not point:
                stale_pins.append(pin)
                continue
            age_s = now - _safe_float(point.get("updated_ts"), 0.0)
            if str(point.get("quality") or "").lower() != "live" or age_s > ESP_CRITICAL_IO_MAX_AGE_S:
                stale_pins.append(pin)
        if not stale_pins:
            return None
        result = self._refresh_single_io_device("esp32_plc58", set(ESP_CRITICAL_IO_PINS))
        return {
            "ok": bool(result.get("ok", False)),
            "stale_pins": stale_pins,
            "refresh": result,
            "ts": now,
        }

    def _io_values(self) -> dict[tuple[str, str], str]:
        values: dict[tuple[str, str], str] = {}
        now = now_ts()
        for item in self.io_store.list_points(include_reserved=True):
            device = str(item.get("device_code") or "")
            pin = str(item.get("pin_label") or "").upper()
            value = str(item.get("value") if item.get("value") is not None else "0")
            if device == "esp32_plc58" and pin in ESP_BAND_BREAK_IO_PINS:
                age_s = now - _safe_float(item.get("updated_ts"), 0.0)
                if str(item.get("quality") or "").lower() == "live" and age_s > ESP_CRITICAL_IO_MAX_AGE_S:
                    value = "0"
            values[(device, pin)] = value
        return values

    def _io_values_for_pins(self, pins: Iterable[tuple[str, str]]) -> dict[tuple[str, str], str]:
        wanted = {(str(device or ""), str(pin or "").upper()) for device, pin in pins}
        values: dict[tuple[str, str], str] = {}
        devices = sorted({device for device, _pin in wanted})
        for device in devices:
            for item in self.io_store.list_points(device_code=device, include_reserved=True):
                key = (str(item.get("device_code") or ""), str(item.get("pin_label") or "").upper())
                if key in wanted:
                    values[key] = str(item.get("value") if item.get("value") is not None else "0")
        return values

    def _bool_io(self, io_map: dict[tuple[str, str], str], device_code: str, pin_label: str, *, default: bool) -> bool:
        key = (str(device_code or ""), str(pin_label or "").upper())
        if key not in io_map:
            return bool(default)
        return _truthy(io_map.get(key))

    def _io_point_detail(
        self,
        io_map: dict[tuple[str, str], str],
        device_code: str,
        pin_label: str,
        *,
        default: bool,
    ) -> dict[str, Any]:
        device = str(device_code or "")
        pin = str(pin_label or "").upper()
        io_key = f"{device}__{pin.replace('.', '_')}"
        value = str(io_map.get((device, pin), "1" if default else "0"))
        point = self.io_store.get_point(io_key) or {}
        return {
            "device_code": device,
            "pin_label": pin,
            "io_key": io_key,
            "value": value,
            "active": _truthy(value),
            "quality": point.get("quality"),
            "source": point.get("source"),
            "function_text": point.get("function_text"),
            "updated_ts": point.get("updated_ts"),
        }

    def _needs_fresh_raspi_button_io(self, snapshot: dict[str, Any], info: dict[str, Any]) -> bool:
        safety_info = dict((info or {}).get("safety") or {})
        return (
            int((snapshot or {}).get("current_state") or 0) in (20, 21)
            or bool((snapshot or {}).get("purge_active"))
            or bool(safety_info.get("latched"))
            or str(safety_info.get("phase") or "") in {SAFETY_PHASE_LATCHED, SAFETY_PHASE_FAILED}
        )

    def _button_inputs(self, io_map: dict[tuple[str, str], str]) -> dict[str, bool]:
        return {
            name: self._bool_io(io_map, device_code, pin_label, default=False)
            for name, (device_code, pin_label) in BUTTON_INPUTS.items()
        }

    def _physical_button_request(
        self,
        *,
        snapshot: dict[str, Any],
        info: dict[str, Any],
        io_map: dict[tuple[str, str], str],
        previous_inputs: dict[str, Any],
        button_mask: dict[str, bool],
    ) -> Optional[dict[str, Any]]:
        current_inputs = self._button_inputs(io_map)
        for button_name, active_now in current_inputs.items():
            was_active = bool(previous_inputs.get(button_name))
            if not active_now or was_active:
                continue
            try:
                return self._resolve_button_press(
                    button_name,
                    snapshot=snapshot,
                    info=info,
                    button_mask=button_mask,
                )
            except RuntimeError as exc:
                self.logs.log("machine", "info", f"physical button {button_name} ignored: {exc}")
                continue
        return None

    def _apply_button_leds(self, state: int, button_mask: dict[str, bool], ts: float):
        self._apply_button_led_plan(button_led_plan(state, button_mask, ts=ts))

    def _button_led_points(self) -> list[tuple[str, dict[str, Any]]]:
        if self._button_led_points_cache is not None:
            return self._button_led_points_cache
        points: list[tuple[str, dict[str, Any]]] = []
        for _action, pins in BUTTON_LED_OUTPUTS.items():
            for device_code, pin in pins:
                point = self.io_store.get_point(f"{device_code}__{pin.replace('.', '_')}")
                if point:
                    points.append((pin, point))
        self._button_led_points_cache = points
        return points

    def _apply_button_led_plan(self, led_plan: dict[str, bool], *, force: bool = True, source: str = "button-led"):
        io_runtime = IoRuntime(self.cfg, self.io_store)
        writes: list[tuple[bool, dict[str, Any]]] = []
        desired = {pin: bool(led_plan.get(pin, False)) for pin, _point in self._button_led_points()}
        previous = self._button_led_last_plan
        for pin, point in self._button_led_points():
            enabled = desired.get(pin, False)
            if not force and previous is not None and bool(previous.get(pin, False)) == enabled:
                continue
            writes.append((enabled, point))
        if not writes:
            self._button_led_last_plan = desired
            return

        # On multi-colour illuminated buttons, switch stale colours off before
        # enabling the next colour.  A tiny dark gap is better than a visible
        # mixed red/blue flash on the panel.
        had_error = False
        for enabled, point in sorted(writes, key=lambda item: 1 if item[0] else 0):
            try:
                # Button lamps must reflect the current machine state on
                # the physical panel, even after a restart or manual test
                # left the DB value equal to the requested value while the
                # real output latch is different.
                io_runtime.write_output(
                    point["io_key"],
                    enabled,
                    force=force,
                    source=source,
                )
            except Exception as exc:
                had_error = True
                self.logs.log("machine", "info", f"button-led write skipped for {point['io_key']}: {exc}")
        if not had_error:
            self._button_led_last_plan = desired

    def _safety_led_override_active(self, state: int, safety_info: dict[str, Any]) -> bool:
        phase = str((safety_info or {}).get("phase") or "")
        return int(state or 0) == 21 and phase in {
            SAFETY_PHASE_LATCHED,
            SAFETY_PHASE_RESETTING,
            SAFETY_PHASE_READY,
            SAFETY_PHASE_FAILED,
        }

    def _safety_button_led_plan(
        self,
        phase: str,
        ts: float,
        button_mask: dict[str, bool] | None = None,
    ) -> dict[str, bool]:
        plan = {pin: False for pins in BUTTON_LED_OUTPUTS.values() for _device, pin in pins}
        second_on = int(ts) % 2 == 0
        if phase in {SAFETY_PHASE_LATCHED, SAFETY_PHASE_FAILED}:
            plan["Q0.0"] = second_on
            plan["Q0.2"] = not second_on
        elif phase == SAFETY_PHASE_RESETTING:
            plan["Q0.2"] = second_on
        elif phase == SAFETY_PHASE_READY:
            plan["Q0.2"] = True
        return plan

    def _apply_safety_button_leds(self, state: int, safety_info: dict[str, Any], ts: float):
        if not self._safety_led_override_active(state, safety_info):
            return
        io_runtime = IoRuntime(self.cfg, self.io_store)
        param_map = self._param_values_by_prefix(("MAP",))
        button_mask = parse_button_mask(param_map.get("MAP0065", "1111111"))
        plan = self._safety_button_led_plan(str(safety_info.get("phase") or ""), ts, button_mask)
        for pin in ("Q0.0", "Q0.2"):
            point = self.io_store.get_point(f"raspi_plc21__{pin.replace('.', '_')}")
            if not point:
                continue
            try:
                io_runtime.write_output(
                    point["io_key"],
                    bool(plan.get(pin, False)),
                    force=True,
                    source="safety-led",
                )
            except Exception as exc:
                self.logs.log("machine", "info", f"safety-led write skipped for {point['io_key']}: {exc}")

    def _status_lamp_points(self) -> dict[str, dict[str, Any]]:
        if self._status_lamp_points_cache is not None:
            return self._status_lamp_points_cache
        points: dict[str, dict[str, Any]] = {}
        for color, (device_code, pin) in STATUS_LAMP_OUTPUTS.items():
            point = self.io_store.get_point(f"{device_code}__{pin.replace('.', '_')}")
            if point:
                points[str(color)] = point
        self._status_lamp_points_cache = points
        return points

    def _apply_status_lamp(self, state: int, *, warning_active: bool, ts: float):
        lamp = lamp_outputs_for_state(state, warning_active=warning_active, ts=ts)
        if self._status_lamp_last_plan == lamp:
            return
        io_runtime = IoRuntime(self.cfg, self.io_store)
        had_error = False
        writes = [(bool(enabled), self._status_lamp_points().get(str(color))) for color, enabled in lamp.items()]
        for enabled, point in sorted(writes, key=lambda item: 1 if item[0] else 0):
            if not point:
                continue
            try:
                result = io_runtime.write_output(point["io_key"], enabled, source="status-lamp", best_effort=True)
                if bool(result.get("overridden")):
                    had_error = True
            except Exception as exc:
                had_error = True
                self.logs.log("machine", "info", f"status-lamp write skipped for {point['io_key']}: {exc}")
        if not had_error:
            self._status_lamp_last_plan = dict(lamp)

    def _current_production_label(self) -> str:
        active = self.production_logs.active_state()
        if active:
            return str(active.get("production_label") or "").strip()
        manifest = self.production_logs.ready_manifest()
        label = str(manifest.get("production_label") or "").strip()
        if label:
            return label
        return sanitize_production_label(self.params.get_effective_value("MAS0029"))

    def _notify_microtom(self, pkey: str, value: str, *, dedupe_key: str | None):
        if self.outbox is None:
            return
        if str(pkey or "").strip().upper() == "MAS0028" and not _truthy(value):
            self.logs.log(
                "raspi",
                "info",
                "suppressed machine-runtime MAS0028=0 callback; purge termination is Microtom/DIClient-owned",
            )
            return
        targets = peer_urls(self.cfg, "/api/inbox")
        effective_dedupe, replace_existing = (
            microtom_state_queue_options(pkey, value) if dedupe_key else (None, False)
        )
        line = f"{pkey}={value}"
        if targets and not ValueDedupeStore(self.params.db).should_send("microtom", pkey, value):
            return
        for url in targets:
            self.outbox.enqueue(
                "POST",
                url,
                {},
                {"msg": line, "source": "raspi", "origin": "machine-runtime"},
                None,
                priority=20,
                dedupe_key=effective_dedupe,
                drop_if_duplicate=bool(effective_dedupe),
                replace_existing=replace_existing,
            )
        if targets:
            self.logs.log("raspi", "out", f"to microtom: {line}")


def parse_machine_event_line(line: str) -> dict[str, Any] | None:
    raw = str(line or "").strip()
    if not raw:
        return None
    if not raw.upper().startswith("EVT "):
        return None
    payload_raw = raw[4:].strip()
    if not payload_raw:
        return None
    try:
        payload = json.loads(payload_raw)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def normalize_ai_text(text: str | None) -> str:
    raw = str(text or "").strip()
    raw = re.sub(r"\s+", " ", raw)
    return raw

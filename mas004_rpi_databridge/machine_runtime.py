from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Optional

from mas004_rpi_databridge.db import DB, now_ts
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
from mas004_rpi_databridge.outbox import Outbox
from mas004_rpi_databridge.params import ParamStore
from mas004_rpi_databridge.peers import peer_urls
from mas004_rpi_databridge.production_logs import ProductionLogManager, sanitize_production_label
from mas004_rpi_databridge.setup_wickler_orchestrator import SetupWicklerOrchestrator
from mas004_rpi_databridge.smart_wickler_client import SmartWicklerClient


def _truthy(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw or "").strip().lower()
    if text in ("", "0", "false", "off", "no", "none", "null"):
        return False
    return True


def _safe_int(raw: Any, default: int = 0) -> int:
    try:
        return int(float(str(raw or "").strip()))
    except Exception:
        return int(default)


PAUSE_ERROR_KEYS = {"MAE0025", "MAE0026"}
RESETTABLE_SAFETY_ERROR_KEYS = {
    "MAE0001",  # Not-Aus
    "MAE0024",  # Etikettenbandriss
    "MAE0027",  # Etikettensensor prellt
    "MAE0030",  # Abwickler Taenzerarm zu tief
    "MAE0034",  # Aufwickler Taenzerarm zu tief
    "MAE0048",  # Etikettenantrieb Nachpositionierung fehlgeschlagen
}
PROCESS_SENSOR_FAULT_STATES = {2, 3, 4, 5, 10, 11, 12, 13, 16, 17}
CONDITIONAL_RESETTABLE_SAFETY_ERRORS = {
    # These are latched machine errors. Clear them only if the matching live
    # inputs are quiet after the reset, otherwise the purge must stay active.
    "MAE0008": ("esp32_plc58", "I0.4"),
    "MAE0009": ("esp32_plc58", "I0.11"),
}
SAFETY_RESET_BUTTON = "start_pause"
SAFETY_PHASE_LATCHED = "latched"
SAFETY_PHASE_RESETTING = "resetting"
SAFETY_PHASE_READY = "ready"
SAFETY_PHASE_FAILED = "failed"
PURGE_EXTERNAL_CLEAR_GRACE_S = 3.0
_SAFETY_RESET_LOCK = threading.Lock()
_SETUP_WICKLER_LOCK = threading.Lock()
STOP_MODE_AXIS_TARGETS_MM = {
    5: 0.0,    # Material-Kontrollkamera TV1
    6: -20.0,  # Sensor Etikettenerfassung
    7: -20.0,  # Sensor Auswurfkontrolle
    8: 91.0,   # Etikettenanschlag links
    9: 91.0,   # Etikettenanschlag vorne/rechts
}
STOP_MODE_POSITION_TOLERANCE_TENTHS = 5
STOP_MODE_POSITION_RETRY_S = 60.0
STOP_MODE_POSITION_MAX_ATTEMPTS = 3
STOP_MODE_POSITION_LOGIC_VERSION = 3


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


def mark_external_purge_clear(db: DB, *, source: str = "microtom"):
    state = _machine_state_row_from_db(db)
    info = dict(state.get("info") or {})
    purge_info = dict(info.get("purge") or {})
    purge_info["external_clear_ts"] = now_ts()
    purge_info["external_clear_source"] = str(source or "microtom")
    info["purge"] = purge_info
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


def recent_external_purge_clear(db: DB, *, max_age_s: float = PURGE_EXTERNAL_CLEAR_GRACE_S) -> bool:
    state = _machine_state_row_from_db(db)
    try:
        clear_ts = float(((state.get("info") or {}).get("purge") or {}).get("external_clear_ts") or 0.0)
    except Exception:
        clear_ts = 0.0
    return clear_ts > 0.0 and (now_ts() - clear_ts) <= max(0.1, float(max_age_s))


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
        self.production_logs = ProductionLogManager(db, cfg=cfg, outbox=outbox)

    def refresh(self) -> dict[str, Any]:
        snapshot = self._state_row()
        info = dict(snapshot.get("info") or {})
        io_map = self._io_values()
        param_map = self._param_values_by_prefix(("MAP", "MAS", "MAE", "MAW"))
        ts = now_ts()

        button_mask = parse_button_mask(param_map.get("MAP0065", "1111111"))
        warning_active = self._warning_active(param_map)
        pause_active, pause_reasons = self._pause_state(param_map)
        critical_active, critical_reasons = self._critical_state(io_map, param_map)
        format_plan = build_format_plan(param_map)
        safety_status = self._safety_status(io_map)
        button_inputs = self._button_inputs(io_map)
        previous_button_inputs = info.get("button_inputs") or {}
        reset_button_rising = bool(button_inputs.get(SAFETY_RESET_BUTTON)) and not bool(
            previous_button_inputs.get(SAFETY_RESET_BUTTON)
        )
        safety_info = dict(info.get("safety") or {})
        safety_latched = bool(safety_info.get("latched")) or bool(safety_status["active"])

        requested_command = _safe_int(param_map.get("MAS0002", info.get("requested_command", 0)), info.get("requested_command", 0))
        reset_command_active = requested_command == 2
        reset_command_ts = self._param_updated_ts("MAS0002") if reset_command_active else 0.0
        reset_command_seen_ts = float(safety_info.get("mas0002_reset_seen_ts") or 0.0)
        reset_command_rising = reset_command_active and reset_command_ts > reset_command_seen_ts
        reset_needed = (
            bool(safety_latched)
            or bool(safety_status["active"])
            or bool(critical_active)
            or _truthy(param_map.get("MAS0028", "0"))
            or int(snapshot["current_state"] or 0) in (20, 21)
        )
        safety_reset_requested = bool(reset_needed) and (reset_button_rising or reset_command_rising)
        forced_state: int | None = None
        forced_source: str | None = None

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
                        "started_ts": (
                            safety_info.get("last_reset", {}).get("started_ts")
                            if isinstance(safety_info.get("last_reset"), dict)
                            else None
                        ),
                    },
                    "mas0002_reset_seen": reset_command_active,
                    "mas0002_reset_seen_ts": reset_command_seen_ts if reset_command_active else 0.0,
                }
            else:
                try:
                    reset_result = self._perform_safety_reset(safety_status, ts)
                    io_map = self._io_values()
                    param_map = self._param_values_by_prefix(("MAP", "MAS", "MAE", "MAW"))
                    critical_active, critical_reasons = self._critical_state(io_map, param_map)
                    safety_info = {
                        "latched": bool(critical_active or _truthy(param_map.get("MAS0028", "0"))),
                        "phase": SAFETY_PHASE_READY if reset_result.get("ok") else SAFETY_PHASE_FAILED,
                        "last_reasons": list(safety_status.get("reasons") or []),
                        "last_reset": reset_result,
                        "mas0002_reset_seen": reset_command_active,
                        "mas0002_reset_seen_ts": reset_command_ts if reset_command_active else 0.0,
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
            }
            forced_state = 21
            forced_source = "safety_latched"
        else:
            button_request = self._button_requested_command(
                current_state=snapshot["current_state"],
                io_map=io_map,
                previous_inputs=previous_button_inputs,
                button_mask=button_mask,
            )
            if button_request is not None:
                requested_command = button_request
                self.params.apply_device_value("MAS0002", str(requested_command), promote_default=True)
                self.logs.log("machine", "info", f"button requested command -> {requested_command}")
            if safety_info.get("phase") not in (SAFETY_PHASE_READY,):
                safety_info = {"latched": False, "phase": "idle", "last_reasons": []}
            safety_info["mas0002_reset_seen"] = reset_command_active
            safety_info["mas0002_reset_seen_ts"] = reset_command_ts if reset_command_active else 0.0

        if forced_state is None and not safety_latched and requested_command not in (0, 2):
            command_action = _command_action_name(requested_command, int(snapshot["current_state"] or 0))
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

        setup_info = dict(info.get("setup") or {})
        setup_command_active = requested_command == 3
        setup_command_ts = self._param_updated_ts("MAS0002") if setup_command_active else 0.0
        setup_seen_ts = float(setup_info.get("mas0002_setup_seen_ts") or 0.0)
        setup_is_current_state = int(snapshot["current_state"] or 0) in (2, 3)
        # On software start/deploy while MAS0002 is already 3, do not start a
        # motion workflow implicitly. The next fresh Einrichten command still
        # has a newer param timestamp and will run the calibration sequence.
        stale_setup_command = (
            setup_command_active
            and setup_seen_ts == 0.0
            and setup_is_current_state
            and setup_command_ts > 0.0
            and (ts - setup_command_ts) > 5.0
        )
        if stale_setup_command:
            setup_seen_ts = setup_command_ts
            setup_info["mas0002_setup_seen_ts"] = setup_seen_ts
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
                setup_info["last_result"] = self._perform_setup_wickler_calibration()
                if bool((setup_info.get("last_result") or {}).get("ok")):
                    format_ready, missing_format = self._format_ready_for_pause(param_map)
                    setup_info["parameters_ready"] = format_ready
                    setup_info["missing_parameters"] = missing_format
                    if format_ready:
                        self.params.apply_device_value("MAS0002", "7", promote_default=True)
                        requested_command = 7
                        setup_info["completed_ts"] = now_ts()
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

        requested_state = command_to_target_state(requested_command, snapshot["current_state"])
        if pause_active and int(snapshot["current_state"] or 0) in (4, 5, 6):
            requested_state = 7

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
                ups_ok=self._bool_io(io_map, "raspi_plc21", "I0.6", default=True),
                purge_active=purge_active,
            )

        state_changed = new_state != snapshot["current_state"]
        mas0001_value_changed = str(new_state) != str(param_map.get("MAS0001", ""))
        if state_changed or mas0001_value_changed:
            self.params.apply_device_value("MAS0001", str(new_state), promote_default=True)
            self._notify_microtom("MAS0001", str(new_state), dedupe_key="machine:MAS0001")
        if state_changed:
            self._record_event(
                "state_change",
                "info",
                f"Maschinenstatus gewechselt: {snapshot['current_state']} -> {new_state} ({state_label(new_state)})",
                {
                    "from_state": snapshot["current_state"],
                    "to_state": new_state,
                    "source": state_source,
                },
            )
            self.logs.log("machine", "info", f"state {snapshot['current_state']} -> {new_state} ({state_source})")

        actions = state_actions(new_state)
        self._apply_stop_mode_axis_targets(new_state, info, state_changed=state_changed, ts=ts)
        self._apply_button_leds(new_state, button_mask, ts)
        self._apply_safety_button_leds(new_state, safety_info, ts)
        self._apply_status_lamp(new_state, warning_active=warning_active, ts=ts)
        button_leds = button_led_plan(new_state, button_mask, ts=ts)
        if self._safety_led_override_active(new_state, safety_info):
            button_leds.update(self._safety_button_led_plan(str(safety_info.get("phase") or ""), ts))

        machine_label = self._current_production_label()
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
                "warning_keys": self._active_param_keys("MAW"),
                "error_keys": self._active_param_keys("MAE"),
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

        return self.snapshot()

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
        if event_type == "label_complete":
            result = self._handle_label_complete(payload)
            return {"ok": True, "accepted": True, "event": event_type, "result": result}
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

    def press_virtual_button(self, button: str, *, actor: str = "machine-ui") -> dict[str, Any]:
        button_name = str(button or "").strip().lower().replace("-", "_")
        if button_name == "start":
            button_name = "start_pause"
        if button_name == "pause":
            button_name = "start_pause"
        valid = {"start_pause", "stop", "setup", "sync", "empty", "rewind"}
        if button_name not in valid:
            raise RuntimeError(f"unknown machine button: {button}")

        snapshot = self._state_row()
        current_state = int(snapshot["current_state"] or 1)
        info = dict(snapshot.get("info") or {})
        param_map = self._param_values_by_prefix(("MAP", "MAS", "MAE", "MAW"))
        button_mask = parse_button_mask(param_map.get("MAP0065", "1111111"))
        allowed_actions = state_actions(current_state)
        safety_info = dict(info.get("safety") or {})
        reset_context = current_state in (20, 21) or bool(snapshot.get("purge_active")) or bool(safety_info.get("latched"))

        command = button_to_command(button_name, current_state)
        action_name = "pause" if button_name == "start_pause" and current_state == 5 else (
            "start" if button_name == "start_pause" else button_name
        )
        if button_name == "start_pause" and reset_context:
            command = 2
            action_name = "stop"
        elif reset_context and button_name not in {"start_pause", "stop"}:
            raise RuntimeError(f"button {button_name} is blocked during reset/safety context")
        if command is None:
            raise RuntimeError(f"button {button_name} is not valid in state {current_state}")
        if not reset_context and not allowed_actions.get(action_name, False):
            raise RuntimeError(f"button {button_name} is not allowed in state {current_state}")
        if not reset_context and not button_mask.get(action_name, False):
            raise RuntimeError(f"button {button_name} blocked by MAP0065")

        ok, msg = self.params.set_value("MAS0002", str(command), actor="microtom")
        if not ok:
            raise RuntimeError(msg)
        target_state = command_to_target_state(command, current_state)
        payload = {
            "button": button_name,
            "action": action_name,
            "command": command,
            "from_state": current_state,
            "target_state": target_state,
            "actor": actor,
            "reset_context": reset_context,
        }
        self.logs.log(
            "machine",
            "info",
            f"virtual button {button_name} -> MAS0002={command} ({state_label(target_state)})",
        )
        self._record_event(
            "virtual_button",
            "info",
            f"Virtuelle Taste {button_name} ausgeloest -> {state_label(target_state)}",
            payload,
        )
        refreshed = self.refresh()
        return {"ok": True, "button": button_name, "command": command, "target_state": target_state, "snapshot": refreshed}

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
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            c.execute(
                "INSERT INTO label_events(ts,production_label,label_no,event_type,payload_json) VALUES(?,?,?,?,?)",
                (now_ts(), label, label_no, "label_complete", json.dumps(payload, ensure_ascii=False)),
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
        info["last_label_payload"] = dict(payload)
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
                    now_ts(),
                ),
            )

    def _record_event(self, event_type: str, severity: str, message: str, payload: dict[str, Any] | None = None):
        with self.db._conn() as c:
            c.execute(
                "INSERT INTO machine_events(ts,event_type,severity,message,payload_json) VALUES(?,?,?,?,?)",
                (now_ts(), event_type, severity, message, json.dumps(payload or {}, ensure_ascii=False)),
            )

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
            self.logs.log("machine", "info", "Einrichten: Wickler einmessen und Messfahrt gestartet")
            self._record_event(
                "setup_wickler_calibration",
                "info",
                "Einrichten gestartet: beide Wickler einmessen, Messfahrt ausfuehren und Durchmesser uebernehmen",
                {},
            )
            controller = SetupWicklerOrchestrator(self.cfg, self.params, self.logs)
            workflow = controller.run()
            ok = bool(workflow.get("ok"))
            result = {
                "ok": ok,
                "response": "ACK_SETUP_WICKLER",
                "workflow": workflow,
                "started_ts": started_ts,
                "finished_ts": now_ts(),
            }
            self._record_event(
                "setup_wickler_calibration",
                "info" if ok else "error",
                "Einrichten-Wicklerworkflow abgeschlossen" if ok else f"Einrichten-Wicklerworkflow fehlgeschlagen: {response}",
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

    def _stop_mode_target_key(self) -> str:
        return ";".join(f"{motor_id}:{target_mm:.3f}" for motor_id, target_mm in sorted(STOP_MODE_AXIS_TARGETS_MM.items()))

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

        last_attempt_ts = float(stop_info.get("last_attempt_ts") or 0.0)
        attempt_count = int(stop_info.get("attempt_count") or 0)
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
            "attempt_count": int(stop_info.get("attempt_count") or 0) + 1,
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
        for motor_id, target_mm in sorted(STOP_MODE_AXIS_TARGETS_MM.items()):
            try:
                client.reset_alarm(motor_id)
                client.recover_eto_motor(motor_id)
                reply = client.move_absolute_mm(motor_id, target_mm)
                ok = bool(reply.get("ok"))
                result = {"motor_id": motor_id, "target_mm": target_mm, "ok": ok, "reply": reply.get("reply")}
                stop_info["results"].append(result)
                if not ok:
                    errors.append(f"Motor {motor_id}: {reply.get('reply')}")
            except Exception as exc:
                result = {"motor_id": motor_id, "target_mm": target_mm, "ok": False, "error": repr(exc)}
                stop_info["results"].append(result)
                errors.append(f"Motor {motor_id}: {exc}")

        verification = self._verify_stop_mode_axis_targets(client)
        stop_info["verification"] = verification
        if not verification.get("ok"):
            errors.extend(str(item) for item in verification.get("errors") or [])

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
            self.logs.log("machine", "info", "Stop-Positionssatz gesendet: ID5=0mm, ID6/7=-20mm, ID8/9=91mm")
            self._record_event(
                "stop_mode_axis_targets",
                "info",
                "Stop-Positionssatz gesendet: ID5=0mm, ID6/7=-20mm, ID8/9=91mm",
                stop_info,
            )
        info["stop_positions"] = stop_info

    def _verify_stop_mode_axis_targets(self, client: EspMotorClient) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        errors: list[str] = []
        for motor_id, target_mm in sorted(STOP_MODE_AXIS_TARGETS_MM.items()):
            target_tenths = int(round(float(target_mm) * 10.0))
            try:
                payload = client.refresh(motor_id)
                motor = payload.get("motor") if isinstance(payload, dict) else {}
                state = (motor or {}).get("state") or motor or {}
                feedback_tenths = int(state.get("feedback_tenths_mm"))
                moving = bool(state.get("move")) or bool(state.get("busy"))
                error_tenths = target_tenths - feedback_tenths
                at_target = abs(error_tenths) <= STOP_MODE_POSITION_TOLERANCE_TENTHS
                result = {
                    "motor_id": motor_id,
                    "target_tenths_mm": target_tenths,
                    "feedback_tenths_mm": feedback_tenths,
                    "error_tenths_mm": error_tenths,
                    "moving": moving,
                    "at_target": at_target,
                }
                results.append(result)
                if not at_target and not moving:
                    errors.append(
                        f"Motor {motor_id} steht bei {feedback_tenths / 10.0:.1f}mm, "
                        f"Ziel {target_mm:.1f}mm, keine Bewegung gemeldet"
                    )
            except Exception as exc:
                results.append({"motor_id": motor_id, "target_mm": target_mm, "ok": False, "error": repr(exc)})
                errors.append(f"Motor {motor_id} Refresh: {exc}")
        return {"ok": not errors, "results": results, "errors": errors}

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

    def _safety_status(self, io_map: dict[tuple[str, str], str]) -> dict[str, Any]:
        estop_active = self._bool_io(io_map, "esp32_plc58", "I0.7", default=False)
        light_curtain_active = self._bool_io(io_map, "esp32_plc58", "I0.8", default=False)
        reasons: list[str] = []
        if estop_active:
            reasons.append("notaus")
        if light_curtain_active:
            reasons.append("lichtgitter")
        return {
            "active": bool(reasons),
            "estop_active": estop_active,
            "light_curtain_active": light_curtain_active,
            "reasons": reasons,
        }

    def _critical_state(self, io_map: dict[tuple[str, str], str], param_map: dict[str, str]) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        if self._bool_io(io_map, "esp32_plc58", "I0.7", default=False):
            reasons.append("notaus")
        if self._bool_io(io_map, "esp32_plc58", "I0.8", default=False):
            reasons.append("lichtgitter")
        if not self._bool_io(io_map, "raspi_plc21", "I0.6", default=True):
            reasons.append("usv_not_ok")
        if self._bool_io(io_map, "esp32_plc58", "I0.4", default=False):
            reasons.append("bahnriss_einlauf")
        if self._bool_io(io_map, "esp32_plc58", "I0.11", default=False):
            reasons.append("bahnriss_auswurf")
        for pkey, value in param_map.items():
            if pkey in PAUSE_ERROR_KEYS:
                continue
            if pkey == "MAE0027" and _safe_int(param_map.get("MAS0001"), 1) not in PROCESS_SENSOR_FAULT_STATES:
                continue
            if pkey.startswith("MAE") and _truthy(value):
                reasons.append(pkey)
        return bool(reasons), reasons

    def _perform_safety_reset(self, safety_status: dict[str, Any], ts: float) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": False,
            "started_ts": now_ts(),
            "initial_reasons": list(safety_status.get("reasons") or []),
            "steps": [],
        }
        self.logs.log("machine", "info", "safety reset requested")
        self.params.apply_device_value("MAS0001", "8", promote_default=True)
        self._notify_microtom("MAS0001", "8", dedupe_key="machine:MAS0001")
        self._record_event(
            "safety_reset",
            "info",
            "Safety-Reset gestartet: ESP Q0.2 Reset-Sequenz, danach Motor-/Wickler-Reset",
            {"initial_reasons": result["initial_reasons"]},
        )
        self._apply_status_lamp(8, warning_active=False, ts=ts)
        self._apply_safety_button_leds(8, {"phase": SAFETY_PHASE_RESETTING}, ts)

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
        if refreshed_safety["active"]:
            result["steps"].append(
                {"step": "verify_safety_inputs_low", "ok": False, "reasons": refreshed_safety["reasons"]}
            )
            result["error"] = "ESP safety input still HIGH after reset sequence: " + ",".join(refreshed_safety["reasons"])
            return result
        result["steps"].append({"step": "verify_safety_inputs_low", "ok": True})

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

        motion_result = self._reset_motion_devices()
        result["steps"].append({"step": "reset_motion_devices", **motion_result})
        if not motion_result.get("ok"):
            result["error"] = motion_result.get("error") or "motion device reset failed"
            return result

        self.params.apply_device_value("MAS0001", "9", promote_default=True)
        self._notify_microtom("MAS0001", "9", dedupe_key="machine:MAS0001")
        self._record_event(
            "safety_reset",
            "info",
            "Safety-Reset abgeschlossen: Motoren geprueft, MAS0001=9",
            motion_result,
        )
        self._apply_status_lamp(9, warning_active=False, ts=now_ts())
        self._apply_safety_button_leds(9, {"phase": SAFETY_PHASE_READY}, now_ts())
        result["ok"] = True
        result["finished_ts"] = now_ts()
        return result

    def _clear_resettable_safety_errors(
        self, *, io_map: dict[tuple[str, str], str] | None = None
    ) -> dict[str, list[str]]:
        cleared = ["MAS0028", *sorted(RESETTABLE_SAFETY_ERROR_KEYS)]
        kept: list[str] = []
        for pkey, (device_code, pin_label) in sorted(CONDITIONAL_RESETTABLE_SAFETY_ERRORS.items()):
            if io_map is not None and self._bool_io(io_map, device_code, pin_label, default=False):
                kept.append(pkey)
                continue
            cleared.append(pkey)
        for pkey in cleared:
            self.params.apply_device_value(pkey, "0", promote_default=True)
            self._notify_microtom(pkey, "0", dedupe_key=f"machine:{pkey}")
        self.logs.log("machine", "info", "resettable safety errors cleared: " + ",".join(cleared))
        if kept:
            self.logs.log("machine", "warning", "resettable safety errors kept active: " + ",".join(kept))
        return {"cleared": cleared, "kept": kept}

    def _pulse_esp_reset_output(self):
        io_runtime = IoRuntime(self.cfg, self.io_store)
        point = self.io_store.get_point("esp32_plc58__Q0_2")
        if not point:
            raise RuntimeError("ESP reset output Q0.2 is not defined in IO master")
        io_runtime.write_output(point["io_key"], True, force=True, source="safety-reset")
        time.sleep(0.2)
        io_runtime.write_output(point["io_key"], False, force=True, source="safety-reset")
        time.sleep(0.1)
        io_runtime.write_output(point["io_key"], True, force=True, source="safety-reset")
        time.sleep(0.2)
        io_runtime.write_output(point["io_key"], False, force=True, source="safety-reset")

    def _refresh_single_io_device(self, device_code: str) -> dict[str, Any]:
        points = [
            point
            for point in self.io_store.list_points(include_reserved=True)
            if str(point.get("device_code") or "") == str(device_code or "")
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
        try:
            client = EspPlcClient(
                str(self.cfg.esp_host),
                int(self.cfg.esp_port),
                float(getattr(self.cfg, "esp_connect_timeout_s", 1.5) or 1.5),
            )
            reply = client.exchange_line(
                "PROCESS RESET",
                read_timeout_s=float(getattr(self.cfg, "esp_command_timeout_s", 8.0) or 8.0),
            )
            ok = str(reply or "").strip().upper().startswith("ACK_PROCESS_RESET")
            if not ok:
                return {"ok": False, "reply": reply, "error": f"unexpected ESP reply: {reply}"}
            return {"ok": True, "reply": reply}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

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
                        verify = {
                            "step": "verify_ready",
                            "motor_id": motor_id,
                            "attempt": attempt,
                            "ok": bool(
                                status.get("ok")
                                and state.get("link_ok")
                                and state.get("ready")
                                and not state.get("alarm")
                            ),
                            "ready": bool(state.get("ready")),
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
                        f"{motor_id} not ready "
                        f"(link={bool((verify or {}).get('link_ok'))}, "
                        f"ready={bool((verify or {}).get('ready'))}, "
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
                    "wippe unten",
                    "wippe oben",
                    "externer wickler-stop aktiv",
                )
                verify = {
                    "step": "verify_safe_stop",
                    "ok": bool(
                        state.get("ok")
                        and drive.get("online")
                        and drive.get("ready")
                        and not drive.get("alarm")
                        and safe_stop_fault
                    ),
                    "online": bool(drive.get("online")),
                    "ready": bool(drive.get("ready")),
                    "alarm": bool(drive.get("alarm")),
                    "alarm_code": drive.get("alarmCode"),
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
                        f"fault={verify['fault_reason']}, raw_output={verify['raw_output']})"
                    )
            except Exception as exc:
                role_detail["steps"].append({"step": "verify_ready", "ok": False, "error": str(exc)})
                hard_failures.append(f"{role} verify_ready: {exc}")
            details["wicklers"].append(role_detail)

        if hard_failures:
            return {"ok": False, "error": "; ".join(hard_failures[:5]), "details": details}
        return {"ok": True, "details": details}

    def _io_values(self) -> dict[tuple[str, str], str]:
        values: dict[tuple[str, str], str] = {}
        for item in self.io_store.list_points(include_reserved=True):
            values[(str(item.get("device_code") or ""), str(item.get("pin_label") or "").upper())] = str(
                item.get("value") if item.get("value") is not None else "0"
            )
        return values

    def _bool_io(self, io_map: dict[tuple[str, str], str], device_code: str, pin_label: str, *, default: bool) -> bool:
        key = (str(device_code or ""), str(pin_label or "").upper())
        if key not in io_map:
            return bool(default)
        return _truthy(io_map.get(key))

    def _button_inputs(self, io_map: dict[tuple[str, str], str]) -> dict[str, bool]:
        return {
            name: self._bool_io(io_map, device_code, pin_label, default=False)
            for name, (device_code, pin_label) in BUTTON_INPUTS.items()
        }

    def _button_requested_command(
        self,
        *,
        current_state: int,
        io_map: dict[tuple[str, str], str],
        previous_inputs: dict[str, Any],
        button_mask: dict[str, bool],
    ) -> Optional[int]:
        current_inputs = self._button_inputs(io_map)
        allowed_actions = state_actions(current_state)
        for button_name, active_now in current_inputs.items():
            was_active = bool(previous_inputs.get(button_name))
            if not active_now or was_active:
                continue
            command = button_to_command(button_name, current_state)
            if command is None:
                continue
            action_name = "pause" if button_name == "start_pause" and int(current_state or 0) == 5 else (
                "start" if button_name == "start_pause" else button_name
            )
            if not allowed_actions.get(action_name, False):
                continue
            if not button_mask.get(action_name, False):
                self.logs.log("machine", "info", f"button {button_name} ignored by MAP0065")
                continue
            return command
        return None

    def _apply_button_leds(self, state: int, button_mask: dict[str, bool], ts: float):
        io_runtime = IoRuntime(self.cfg, self.io_store)
        led_plan = button_led_plan(state, button_mask, ts=ts)
        for action, pins in BUTTON_LED_OUTPUTS.items():
            for device_code, pin in pins:
                point = self.io_store.get_point(f"{device_code}__{pin.replace('.', '_')}")
                if not point:
                    continue
                try:
                    io_runtime.write_output(point["io_key"], bool(led_plan.get(pin, False)))
                except Exception as exc:
                    self.logs.log("machine", "info", f"button-led write skipped for {point['io_key']}: {exc}")

    def _safety_led_override_active(self, state: int, safety_info: dict[str, Any]) -> bool:
        phase = str((safety_info or {}).get("phase") or "")
        return int(state or 0) in (8, 9, 21) and phase in {
            SAFETY_PHASE_LATCHED,
            SAFETY_PHASE_RESETTING,
            SAFETY_PHASE_READY,
            SAFETY_PHASE_FAILED,
        }

    def _safety_button_led_plan(self, phase: str, ts: float) -> dict[str, bool]:
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
        plan = self._safety_button_led_plan(str(safety_info.get("phase") or ""), ts)
        for pin in ("Q0.0", "Q0.2"):
            point = self.io_store.get_point(f"raspi_plc21__{pin.replace('.', '_')}")
            if not point:
                continue
            try:
                io_runtime.write_output(point["io_key"], bool(plan.get(pin, False)), force=True, source="safety-led")
            except Exception as exc:
                self.logs.log("machine", "info", f"safety-led write skipped for {point['io_key']}: {exc}")

    def _apply_status_lamp(self, state: int, *, warning_active: bool, ts: float):
        io_runtime = IoRuntime(self.cfg, self.io_store)
        lamp = lamp_outputs_for_state(state, warning_active=warning_active, ts=ts)
        for color, enabled in lamp.items():
            device_code, pin = STATUS_LAMP_OUTPUTS[color]
            point = self.io_store.get_point(f"{device_code}__{pin.replace('.', '_')}")
            if not point:
                continue
            try:
                io_runtime.write_output(point["io_key"], bool(enabled), source="status-lamp", best_effort=True)
            except Exception as exc:
                self.logs.log("machine", "info", f"status-lamp write skipped for {point['io_key']}: {exc}")

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
        targets = peer_urls(self.cfg, "/api/inbox")
        effective_dedupe = f"state:{pkey}" if dedupe_key else None
        for url in targets:
            self.outbox.enqueue(
                "POST",
                url,
                {},
                {"msg": f"{pkey}={value}", "source": "raspi", "origin": "machine-runtime"},
                None,
                priority=20,
                dedupe_key=effective_dedupe,
                drop_if_duplicate=bool(effective_dedupe),
                replace_existing=bool(effective_dedupe),
            )


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

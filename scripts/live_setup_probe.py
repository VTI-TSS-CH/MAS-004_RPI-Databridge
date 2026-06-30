#!/usr/bin/env python3
"""Run one guarded setup probe on the live MAS-004 machine.

The script only queues the same setup button command as the HMI/physical panel.
The running databridge service remains the owner of the setup workflow.  This
probe watches the ESP, wicklers and machine DB and stops motion if a hard guard
trips.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from mas004_rpi_databridge.config import DEFAULT_CFG_PATH, Settings
from mas004_rpi_databridge.db import DB, now_ts
from mas004_rpi_databridge.device_clients import EspPlcClient
from mas004_rpi_databridge.io_master import IoStore
from mas004_rpi_databridge.logstore import LogStore
from mas004_rpi_databridge.machine_runtime import MachineRuntime
from mas004_rpi_databridge.outbox import Outbox
from mas004_rpi_databridge.params import ParamStore
from mas004_rpi_databridge.setup_wickler_orchestrator import SetupWicklerOrchestrator
from mas004_rpi_databridge.smart_wickler_client import SmartWicklerClient


WICKLER_ABORT_LOW_PERCENT = 8.0
WICKLER_ABORT_HIGH_PERCENT = 92.0
POSITION_MOTORS = (1, 2, 4, 5, 6, 7, 8, 9)
WATCH_PARAMS = (
    "MAS0001",
    "MAS0002",
    "MAS0028",
    "MAS0030",
    "MAP0065",
    "MAP0076",
    "MAE0024",
    "MAE0025",
    "MAE0026",
    "MAE0027",
    "MAE0028",
    "MAE0029",
    "MAE0030",
    "MAE0032",
    "MAE0033",
    "MAE0034",
    "MAE0048",
)


def esp_exchange(cfg: Settings, line: str, timeout_s: float = 1.5, attempts: int = 3) -> tuple[bool, Any]:
    client = EspPlcClient(cfg.esp_host, int(cfg.esp_port), cfg.esp_connect_timeout_s)
    last_error = ""
    for attempt in range(1, max(1, int(attempts)) + 1):
        try:
            text = client.exchange_line(line, read_timeout_s=timeout_s, read_limit=8192).strip()
            if text.startswith("JSON "):
                return True, json.loads(text[5:].strip())
            if text:
                return True, text
            last_error = "empty_reply"
        except Exception as exc:
            last_error = repr(exc)
        time.sleep(min(0.8, 0.2 * attempt))
    return False, last_error


def latest_row_id(db: DB, table: str) -> int:
    try:
        with db._conn() as conn:
            row = conn.execute(f"SELECT max(id) FROM {table}").fetchone()
        return int(row[0] or 0)
    except Exception:
        return 0


def rows_since(db: DB, table: str, start_id: int, limit: int = 300) -> list[dict[str, Any]]:
    with db._conn() as conn:
        if table == "machine_events":
            rows = conn.execute(
                """SELECT id, ts, event_type, severity, message, payload_json
                   FROM machine_events WHERE id>? ORDER BY id ASC LIMIT ?""",
                (int(start_id), int(limit)),
            ).fetchall()
            out = []
            for row in rows:
                try:
                    payload = json.loads(row[5] or "{}")
                except Exception:
                    payload = {}
                out.append(
                    {
                        "id": row[0],
                        "ts": row[1],
                        "event_type": row[2],
                        "severity": row[3],
                        "message": row[4],
                        "payload": payload,
                    }
                )
            return out
        rows = conn.execute(
            """SELECT id, ts, channel, direction, message
               FROM logs WHERE id>? ORDER BY id ASC LIMIT ?""",
            (int(start_id), int(limit)),
        ).fetchall()
    return [
        {"id": row[0], "ts": row[1], "channel": row[2], "direction": row[3], "message": row[4]}
        for row in rows
    ]


def machine_state(db: DB) -> dict[str, Any]:
    with db._conn() as conn:
        row = conn.execute(
            """SELECT current_state, requested_state, state_source, purge_active,
                      warning_active, production_label, last_label_no, info_json
               FROM machine_state WHERE singleton_id=1"""
        ).fetchone()
    if row is None:
        return {}
    try:
        info = json.loads(row[7] or "{}")
    except Exception:
        info = {}
    return {
        "current_state": int(row[0] or 0),
        "requested_state": int(row[1] or 0),
        "state_source": row[2],
        "purge_active": bool(row[3]),
        "warning_active": bool(row[4]),
        "production_label": row[5],
        "last_label_no": int(row[6] or 0),
        "info": info,
    }


def param_snapshot(params: ParamStore) -> dict[str, str]:
    return {key: str(params.get_effective_value(key) or "0") for key in WATCH_PARAMS}


def fetch_wicklers(cfg: Settings) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for role in ("unwinder", "rewinder"):
        try:
            out[role] = SmartWicklerClient(cfg, role).fetch_state(timeout_s=0.8)
        except Exception as exc:
            out[role] = {"ok": False, "error": repr(exc)}
    return out


def wickler_summary(wicklers: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for role, state in wicklers.items():
        telemetry = state.get("telemetry") or {}
        drive = state.get("drive") or {}
        values = state.get("values") or {}
        out[role] = {
            "mode": telemetry.get("modeLabel"),
            "modeCss": telemetry.get("modeCss"),
            "wipePercent": telemetry.get("wipePercent"),
            "fillPercent": telemetry.get("fillPercent"),
            "faultReason": telemetry.get("faultReason"),
            "externalStopActive": telemetry.get("externalStopActive"),
            "calibrated": telemetry.get("calibrated"),
            "requiresCalibration": telemetry.get("requiresCalibration"),
            "driveOnline": drive.get("online"),
            "driveReady": drive.get("ready"),
            "driveAlarm": drive.get("alarm"),
            "maeBlocked": values.get("maeBlocked"),
            "maeHigh": values.get("maeHigh"),
            "maeLow": values.get("maeLow"),
            "ok": state.get("ok"),
        }
    return out


def parse_wipe(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "on", "yes", "y"}


def guard_reason(state: dict[str, Any], params: dict[str, str], wicklers: dict[str, Any]) -> str:
    if str(params.get("MAS0028", "0")).strip() not in {"0", "", "False", "false"}:
        return "MAS0028/Purge aktiv"
    if int(state.get("current_state") or 0) == 21:
        return "Maschine in Not-Stop/Stoerung"
    info = state.get("info") or {}
    critical = [str(x) for x in (info.get("critical_reasons") or []) if str(x)]
    if critical:
        return "Kritische Gruende: " + "; ".join(critical)
    for role, item in wicklers.items():
        if item.get("ok") is False:
            return f"{role} Wickler offline: {item.get('error') or 'state fetch failed'}"
        telemetry = item.get("telemetry") or {}
        values = item.get("values") or {}
        drive = item.get("drive") or {}
        mode = str(telemetry.get("modeLabel") or "").lower()
        mode_css = str(telemetry.get("modeCss") or "").lower()
        calibrating = mode in {"einmessen", "calibrate", "calibration", "kalibrierung"} or mode_css == "calibration"
        requires_calibration = truthy(telemetry.get("requiresCalibration")) or not truthy(
            telemetry.get("calibrated", True)
        )
        calibration_pending_only = (
            requires_calibration
            and not truthy(drive.get("alarm"))
        )
        if calibration_pending_only:
            continue
        wipe = parse_wipe(telemetry.get("wipePercent"))
        if (not calibrating) and wipe is not None and wipe <= WICKLER_ABORT_LOW_PERCENT:
            return f"{role} Wippe im unteren Sicherheitsbereich ({wipe:.1f}%)"
        if (not calibrating) and wipe is not None and wipe >= WICKLER_ABORT_HIGH_PERCENT:
            return f"{role} Wippe im oberen Sicherheitsbereich ({wipe:.1f}%)"
        if bool(values.get("maeBlocked")) or bool(values.get("maeHigh")) or bool(values.get("maeLow")):
            return f"{role} Wickler-MAE aktiv"
        if mode_css == "fault" or mode in {"fault", "fehler", "stoerung", "stoerung"}:
            return f"{role} Wickler rot: {telemetry.get('faultReason') or telemetry.get('modeLabel')}"
    return ""


def is_transient_wickler_fault(reason: str) -> bool:
    text = reason.lower()
    return "wickler" in text and ("offline" in text or "timeout" in text or "connect" in text)


def is_initial_wickler_bottom_guard(reason: str) -> bool:
    text = str(reason or "").lower()
    return "wippe im unteren sicherheitsbereich" in text


def safe_stop(cfg: Settings, params: ParamStore, logs: LogStore, reason: str, *, set_purge: bool) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    try:
        SetupWicklerOrchestrator(cfg, params, logs).stop_all_motion()
        result.append({"command": "SetupWicklerOrchestrator.stop_all_motion", "ok": True})
    except Exception as exc:
        result.append({"command": "SetupWicklerOrchestrator.stop_all_motion", "ok": False, "error": repr(exc)})
        for command in (
            "PROCESS PRODUCTION STOP",
            "PROCESS SETUP_MEASURE STOP",
            "MOTOR 3 MOVE_VEL_MM_S=0",
            "PROCESS WICKLER CANCEL",
            "PROCESS INDEXED STOP",
            "PROCESS PROFILE STOP",
        ):
            ok, reply = esp_exchange(cfg, command, timeout_s=1.5)
            result.append({"command": command, "ok": ok, "reply": reply})
    if set_purge:
        try:
            ok, message = params.apply_device_value("MAS0028", "1", promote_default=True)
            result.append({"command": "MAS0028=1", "ok": ok, "reply": message, "reason": reason})
        except Exception as exc:
            result.append({"command": "MAS0028=1", "ok": False, "error": repr(exc), "reason": reason})
    return result


def preflight_axis_status(cfg: Settings) -> list[dict[str, Any]]:
    axes = []
    for motor_id in POSITION_MOTORS:
        ok, reply = esp_exchange(cfg, f"MOTOR {motor_id} REFRESH", timeout_s=5.0)
        item: dict[str, Any] = {"motor_id": motor_id, "ok": ok, "reply": reply}
        if isinstance(reply, dict):
            payload = reply
            if isinstance(reply.get("motor"), dict):
                payload = reply["motor"]
            state = payload.get("state") if isinstance(payload.get("state"), dict) else payload
            config = payload.get("config") if isinstance(payload.get("config"), dict) else payload
            try:
                feedback = int(state.get("feedback_tenths_mm"))
                min_pos = int(config.get("min_tenths_mm", config.get("min_pos_tenths")))
                max_pos = int(config.get("max_tenths_mm", config.get("max_pos_tenths")))
                item.update({"feedback": feedback, "min": min_pos, "max": max_pos})
                item["within_limits"] = min_pos <= feedback <= max_pos
            except Exception:
                item["within_limits"] = None
        axes.append(item)
    return axes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cfg", default=DEFAULT_CFG_PATH)
    parser.add_argument("--duration-s", type=float, default=260.0)
    parser.add_argument("--poll-s", type=float, default=0.5)
    parser.add_argument("--no-start", action="store_true")
    parser.add_argument("--attach", action="store_true", help="monitor an already active setup workflow")
    parser.add_argument("--skip-axis-preflight", action="store_true")
    parser.add_argument("--out", default="/tmp/mas004_live_setup_probe.json")
    args = parser.parse_args()

    cfg = Settings.load(args.cfg)
    db = DB(cfg.db_path)
    params = ParamStore(db)
    logs = LogStore(db)
    outbox = Outbox(db)
    io_store = IoStore(db)
    runtime = MachineRuntime(cfg, db, params, io_store, logs, outbox)

    result: dict[str, Any] = {
        "started_ts": now_ts(),
        "initial_state": machine_state(db),
        "initial_params": param_snapshot(params),
        "initial_wicklers": {},
        "axis_preflight": [],
        "samples": [],
        "start_result": None,
        "stop_result": None,
        "guard_reason": "",
        "completed": False,
        "success": False,
    }
    event_start_id = latest_row_id(db, "machine_events")
    log_start_id = latest_row_id(db, "logs")

    result["initial_wicklers"] = wickler_summary(fetch_wicklers(cfg))
    if not args.skip_axis_preflight and not args.attach:
        result["axis_preflight"] = preflight_axis_status(cfg)
        axis_errors = [
            item
            for item in result["axis_preflight"]
            if (not item.get("ok")) or item.get("within_limits") is False
        ]
        if axis_errors:
            result["abort_before_start"] = {"reason": "position axis preflight failed", "axes": axis_errors}
            Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
            print(json.dumps(result["abort_before_start"], indent=2, sort_keys=True), flush=True)
            return 2

    initial_state = int(result["initial_state"].get("current_state") or 0)
    initial_critical = list((result["initial_state"].get("info") or {}).get("critical_reasons") or [])
    if (not args.attach) and (
        initial_state != 9 or initial_critical or str(result["initial_params"].get("MAS0028", "0")) not in {"0", ""}
    ):
        result["abort_before_start"] = {
            "reason": "machine not in clean production stop",
            "state": initial_state,
            "critical": initial_critical,
            "MAS0028": result["initial_params"].get("MAS0028"),
        }
        Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps(result["abort_before_start"], indent=2, sort_keys=True), flush=True)
        return 2

    initial_guard = guard_reason(result["initial_state"], result["initial_params"], fetch_wicklers(cfg))
    initial_setup_info = (result["initial_state"].get("info") or {}).get("setup") or {}
    initial_setup_running = bool((initial_setup_info.get("last_result") or {}).get("running"))
    if initial_guard and is_initial_wickler_bottom_guard(initial_guard) and (not args.attach or initial_setup_running):
        result["initial_guard_ignored"] = {
            "reason": initial_guard,
            "why": "Wickler-Einmessen darf aus mechanischer 0%-Startlage beginnen",
        }
        initial_guard = ""
    if initial_guard and is_transient_wickler_fault(initial_guard) and initial_setup_running:
        result["initial_guard_ignored"] = {
            "reason": initial_guard,
            "why": "transienter Wickler-HTTP-Lesefehler waehrend laufendem Einrichten",
        }
        initial_guard = ""
    if initial_guard:
        result["abort_before_start"] = {"reason": initial_guard}
        Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps(result["abort_before_start"], indent=2, sort_keys=True), flush=True)
        return 2

    if (not args.attach) and str(result["initial_params"].get("MAP0065", "")).strip() != "1111111":
        ok, message = params.set_value("MAP0065", "1111111", actor="live-setup-probe")
        result["map0065_enable"] = {"ok": ok, "message": message}
        if not ok:
            Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
            print(json.dumps(result["map0065_enable"], indent=2, sort_keys=True), flush=True)
            return 2

    if args.no_start and not args.attach:
        result["completed"] = True
        result["success"] = True
        result["final_state"] = machine_state(db)
        result["final_params"] = param_snapshot(params)
        result["final_wicklers"] = wickler_summary(fetch_wicklers(cfg))
        result["final_setup_status"] = esp_exchange(cfg, "PROCESS SETUP_MEASURE STATUS?", timeout_s=1.5)[1]
        result["events"] = rows_since(db, "machine_events", event_start_id, limit=500)
        result["logs"] = rows_since(db, "logs", log_start_id, limit=500)
        Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
        print(f"preflight_ok result={args.out}", flush=True)
        return 0

    if not args.no_start and not args.attach:
        result["start_result"] = runtime.press_virtual_button("setup", actor="live-setup-probe")

    deadline = time.monotonic() + max(5.0, float(args.duration_s))
    monitor_started = time.monotonic()
    setup_seen = bool(args.attach) or bool((result.get("start_result") or {}).get("queued"))
    setup_measure_seen = False
    last_print = 0.0
    transient_wickler_guard_count = 0

    while time.monotonic() < deadline:
        state = machine_state(db)
        params_now = param_snapshot(params)
        ok_setup, setup_status = esp_exchange(cfg, "PROCESS SETUP_MEASURE STATUS?", timeout_s=1.2)
        ok_prod, prod_status = esp_exchange(cfg, "PROCESS PRODUCTION STATUS?", timeout_s=1.0)
        wicklers_full = fetch_wicklers(cfg)
        wicklers = wickler_summary(wicklers_full)
        sample = {
            "ts": now_ts(),
            "machine_state": state,
            "params": params_now,
            "setup_status": setup_status,
            "setup_status_ok": ok_setup,
            "production_status": prod_status,
            "production_status_ok": ok_prod,
            "wicklers": wicklers,
        }
        result["samples"].append(sample)

        if isinstance(setup_status, dict) and bool(setup_status.get("running")):
            setup_seen = True
            setup_measure_seen = True
        if isinstance(setup_status, dict) and str(setup_status.get("phase_name") or "") not in {"", "idle", "complete"}:
            setup_measure_seen = True
        if int(state.get("current_state") or 0) in (2, 3):
            setup_seen = True

        guard = guard_reason(state, params_now, wicklers_full)
        if guard:
            if is_transient_wickler_fault(guard):
                transient_wickler_guard_count += 1
                if transient_wickler_guard_count < 3:
                    result.setdefault("transient_guard_samples", []).append(
                        {"ts": now_ts(), "reason": guard, "count": transient_wickler_guard_count}
                    )
                    guard = ""
            else:
                transient_wickler_guard_count = 0
        else:
            transient_wickler_guard_count = 0
        if guard:
            if is_initial_wickler_bottom_guard(guard) and setup_seen and not setup_measure_seen:
                result.setdefault("ignored_pre_measure_guards", []).append(
                    {
                        "ts": now_ts(),
                        "reason": guard,
                        "why": "Wickler-Einmessen laeuft/steht bevor ESP-Messfahrt startet",
                    }
                )
                guard = ""
        if guard:
            result["guard_reason"] = guard
            result["stop_result"] = safe_stop(cfg, params, logs, guard, set_purge=True)
            print(f"GUARD {guard}", flush=True)
            break

        if isinstance(setup_status, dict):
            last_error = str(setup_status.get("last_error") or "").strip()
            phase_name = str(setup_status.get("phase_name") or "").strip().lower()
            setup_completed = bool(setup_status.get("completed")) or phase_name == "complete"
            if setup_measure_seen and not bool(setup_status.get("running")) and last_error and not setup_completed:
                result["guard_reason"] = "ESP setup stopped with error: " + last_error
                print(result["guard_reason"], flush=True)
                break
            if setup_seen and int(state.get("current_state") or 0) == 7 and not bool(setup_status.get("running")):
                result["completed"] = True
                result["success"] = True
                print("SETUP_SUCCESS machine state 7 pause", flush=True)
                break
            setup_info = (state.get("info") or {}).get("setup") or {}
            setup_workflow_running = bool((setup_info.get("last_result") or {}).get("running"))
            returned_to_stop_after_setup_activity = (
                setup_measure_seen or (time.monotonic() - monitor_started) >= 20.0
            )
            if (
                setup_seen
                and int(state.get("current_state") or 0) == 9
                and not bool(setup_status.get("running"))
                and not setup_workflow_running
                and returned_to_stop_after_setup_activity
            ):
                result["completed"] = True
                result["success"] = False
                result["guard_reason"] = "setup returned to production stop before pause"
                print(result["guard_reason"], flush=True)
                break

        now = time.monotonic()
        if now - last_print >= 2.0:
            last_print = now
            phase = setup_status.get("phase_name") if isinstance(setup_status, dict) else ""
            running = setup_status.get("running") if isinstance(setup_status, dict) else ""
            infeed = setup_status.get("infeed_mm") if isinstance(setup_status, dict) else ""
            drive = setup_status.get("drive_mm") if isinstance(setup_status, dict) else ""
            wu = wicklers.get("unwinder", {}).get("wipePercent")
            wr = wicklers.get("rewinder", {}).get("wipePercent")
            print(
                "setup_probe "
                f"MAS0001={params_now.get('MAS0001')} state={state.get('current_state')} "
                f"phase={phase} running={running} infeed={infeed} drive={drive} "
                f"wU={wu} wR={wr}",
                flush=True,
            )
        time.sleep(max(0.1, float(args.poll_s)))

    else:
        result["guard_reason"] = f"timeout after {float(args.duration_s):.1f}s"
        result["stop_result"] = safe_stop(cfg, params, logs, result["guard_reason"], set_purge=False)
        print(result["guard_reason"], flush=True)

    result["final_state"] = machine_state(db)
    result["final_params"] = param_snapshot(params)
    result["final_wicklers"] = wickler_summary(fetch_wicklers(cfg))
    result["final_setup_status"] = esp_exchange(cfg, "PROCESS SETUP_MEASURE STATUS?", timeout_s=1.5)[1]
    result["events"] = rows_since(db, "machine_events", event_start_id, limit=500)
    result["logs"] = rows_since(db, "logs", log_start_id, limit=500)
    Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(f"probe_result={args.out}", flush=True)
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())

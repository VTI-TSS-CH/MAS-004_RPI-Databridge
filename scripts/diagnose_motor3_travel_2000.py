from __future__ import annotations

import argparse
import json
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from esp_broker_api import exchange_via_databridge, load_settings


ESP_HOST = "192.168.2.101"
ESP_PORT = 3010
UNWINDER_BASE_URL = "http://10.141.94.216:3011"
REWINDER_BASE_URL = "http://10.141.94.217:3012"
TARGET_MM = 2000.0
DEFAULT_SPEED_MM_S = 100.0
DEFAULT_RAMP_MM_S2 = 300.0
RESULT_PATH = Path("/tmp/mas004_motor3_travel_2000_result.json")
WICKLER_LOW_PERCENT = 8.0
WICKLER_HIGH_PERCENT = 92.0
ESP_TRANSIENT_ERRORS = (ConnectionRefusedError, TimeoutError, socket.timeout, OSError)
NUMERIC_KEYS = {
    "age_ms",
    "infeed_mm",
    "drive_mm",
    "infeed_count",
    "drive_count",
    "infeed_speed_mm_s",
    "drive_speed_mm_s",
    "infeed_invalid_transitions",
    "drive_invalid_transitions",
    "edge_count",
    "label_count",
    "overflow_count",
    "labels_emitted",
}
BOOL_KEYS = {"ok", "running", "completed", "labels_truncated"}
STRING_KEYS = {"last_error"}
_CFG_CACHE = None


def _print(message: str) -> None:
    print(message, flush=True)


def _settings():
    global _CFG_CACHE
    if _CFG_CACHE is None:
        _CFG_CACHE = load_settings()
    return _CFG_CACHE


def _json_from_text(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if raw.upper().startswith("JSON "):
        raw = raw[5:].strip()
    if not raw.startswith("{"):
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start : end + 1]
    if not raw.startswith("{"):
        return {}
    try:
        return dict(json.loads(raw))
    except json.JSONDecodeError:
        return _partial_json_from_text(raw)


def _parse_scalar(raw_value: str) -> Any:
    value = raw_value.strip()
    if value == "true":
        return True
    if value == "false":
        return False
    if value == "null":
        return None
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value


def _partial_json_from_text(raw: str) -> dict[str, Any]:
    """Recover the front part of ESP JSON replies truncated by the line buffer.

    TRAVEL_DIAG can exceed the current ESP response window once several label
    objects are present. The summary fields are emitted before the labels array,
    so keep them usable for diagnostics and parse only complete label objects.
    """
    payload: dict[str, Any] = {"_partial": True, "_raw_len": len(raw)}
    for key in sorted(NUMERIC_KEYS | BOOL_KEYS | STRING_KEYS):
        match = re.search(rf'"{re.escape(key)}"\s*:\s*("([^"\\]|\\.)*"|true|false|null|-?\d+(?:\.\d+)?)', raw)
        if match:
            payload[key] = _parse_scalar(match.group(1))

    labels: list[dict[str, Any]] = []
    marker = '"labels":['
    start = raw.find(marker)
    if start >= 0:
        idx = start + len(marker)
        depth = 0
        obj_start: int | None = None
        in_string = False
        escape = False
        while idx < len(raw):
            ch = raw[idx]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
            else:
                if ch == '"':
                    in_string = True
                elif ch == "{":
                    if depth == 0:
                        obj_start = idx
                    depth += 1
                elif ch == "}":
                    if depth > 0:
                        depth -= 1
                        if depth == 0 and obj_start is not None:
                            try:
                                labels.append(dict(json.loads(raw[obj_start : idx + 1])))
                            except json.JSONDecodeError:
                                pass
                            obj_start = None
                elif ch == "]" and depth == 0:
                    break
            idx += 1
    if labels:
        payload["labels"] = labels
        payload["labels_emitted"] = len(labels)
        payload["labels_truncated"] = True
    return payload


def planned_motion_duration_s(distance_mm: float, speed_mm_s: float, ramp_mm_s2: float) -> float:
    distance = abs(float(distance_mm))
    speed = max(1.0, abs(float(speed_mm_s)))
    ramp = max(1.0, abs(float(ramp_mm_s2)))
    accel_time = speed / ramp
    accel_distance = 0.5 * ramp * accel_time * accel_time
    if 2.0 * accel_distance >= distance:
        return 2.0 * (distance / ramp) ** 0.5
    cruise_distance = distance - (2.0 * accel_distance)
    return (2.0 * accel_time) + (cruise_distance / speed)


def esp(command: str, *, timeout_s: float = 3.0, idle_timeout_s: float = 0.35) -> str:
    reply, _payload = exchange_via_databridge(
        _settings(),
        command,
        read_timeout_s=max(float(timeout_s or 3.0), float(idle_timeout_s or 0.35)),
        read_limit=65536,
        priority=command.strip().upper().startswith(("MOTOR 3 ", "PROCESS TRAVEL_DIAG ")),
        request_timeout_s=max(5.0, float(timeout_s or 3.0) + 2.0),
    )
    return reply.strip()


def esp_json(command: str, *, timeout_s: float = 3.0, idle_timeout_s: float = 0.35) -> dict[str, Any]:
    return _json_from_text(esp(command, timeout_s=timeout_s, idle_timeout_s=idle_timeout_s))


def esp_retry(
    command: str,
    *,
    attempts: int = 3,
    delay_s: float = 1.0,
    timeout_s: float = 3.0,
    idle_timeout_s: float = 0.35,
) -> str:
    last_exc: Exception | None = None
    for attempt in range(1, max(1, int(attempts)) + 1):
        try:
            return esp(command, timeout_s=timeout_s, idle_timeout_s=idle_timeout_s)
        except ESP_TRANSIENT_ERRORS as exc:
            last_exc = exc
            if attempt >= attempts:
                break
            time.sleep(max(0.05, delay_s))
    raise last_exc or RuntimeError(f"ESP command failed: {command}")


def esp_json_retry(
    command: str,
    *,
    attempts: int = 3,
    delay_s: float = 1.0,
    timeout_s: float = 3.0,
    idle_timeout_s: float = 0.35,
) -> dict[str, Any]:
    return _json_from_text(
        esp_retry(
            command,
            attempts=attempts,
            delay_s=delay_s,
            timeout_s=timeout_s,
            idle_timeout_s=idle_timeout_s,
        )
    )


def _require_motor_state(payload: dict[str, Any]) -> dict[str, Any]:
    motor = _motor_state(payload)
    if not motor or (
        "feedback_tenths_mm" not in motor
        and "busy" not in motor
        and "move" not in motor
    ):
        raise RuntimeError(f"empty motor status from ESP: {payload}")
    return motor


def http_json(method: str, url: str, data: dict[str, Any] | None = None, *, timeout_s: float = 5.0) -> dict[str, Any]:
    body = None
    headers: dict[str, str] = {}
    if data is not None:
        body = urllib.parse.urlencode({str(k): str(v) for k, v in data.items()}).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} {url}: {raw}") from exc
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": True, "text": raw}
    return dict(payload or {})


def wickler_state(base_url: str) -> dict[str, Any]:
    return http_json("GET", base_url.rstrip("/") + "/api/state", timeout_s=2.5)


def wickler_mode(base_url: str, mode: str, data: dict[str, Any] | None = None, *, timeout_s: float = 6.0) -> dict[str, Any]:
    payload = {"mode": mode}
    if data:
        payload.update(data)
    return http_json("POST", base_url.rstrip("/") + "/api/mode", payload, timeout_s=timeout_s)


def wickler_master(base_url: str, values: dict[str, Any], *, timeout_s: float = 4.0) -> dict[str, Any]:
    return http_json("POST", base_url.rstrip("/") + "/api/master", values, timeout_s=timeout_s)


def set_wickler_leader(speed_mm_s: float) -> None:
    requested = float(speed_mm_s)
    if abs(requested) > 0.001:
        _print(
            "Wickler leaderSpeedMmS wird nicht gesendet: "
            "kontinuierlicher Modus regelt autonom ueber die Wippe."
        )
    for role, base in (("unwinder", UNWINDER_BASE_URL), ("rewinder", REWINDER_BASE_URL)):
        _print(f"{role} leaderSpeedMmS no-op (requested {requested:.3f})")


def wickler_summary(role: str, state: dict[str, Any]) -> dict[str, Any]:
    telemetry = dict(state.get("telemetry") or {})
    drive = dict(state.get("drive") or {})
    master = dict(state.get("master") or {})
    return {
        "role": role,
        "mode": telemetry.get("modeLabel"),
        "wipe_percent": telemetry.get("wipePercent"),
        "external_stop": telemetry.get("externalStopActive"),
        "fault": telemetry.get("faultReason"),
        "indexed": telemetry.get("indexedModeEnabled", master.get("indexedModeEnabled")),
        "leader_speed_mm_s": telemetry.get("leaderSpeedMmS", master.get("leaderSpeedMmS")),
        "online": drive.get("online"),
        "ready": drive.get("ready"),
        "continuous_ready": drive.get("continuousModeReady"),
        "move": drive.get("move"),
        "alarm": drive.get("alarm"),
        "alarm_code": drive.get("alarmCode"),
        "last_command_ok": drive.get("lastCommandOk"),
    }


def wait_wicklers_ready(*, require_motion_ready: bool, timeout_s: float) -> dict[str, dict[str, Any]]:
    bases = {"unwinder": UNWINDER_BASE_URL, "rewinder": REWINDER_BASE_URL}
    deadline = time.time() + timeout_s
    last: dict[str, dict[str, Any]] = {}
    while time.time() < deadline:
        all_ready = True
        for role, base in bases.items():
            state = wickler_state(base)
            summary = wickler_summary(role, state)
            last[role] = summary
            mode = str(summary.get("mode") or "")
            try:
                wipe = float(summary.get("wipe_percent"))
            except Exception:
                wipe = 50.0
            mode_ready = mode in {"Bereit", "Warnung"} and not bool(summary.get("alarm"))
            motion_ready = (
                mode_ready
                and not bool(summary.get("external_stop"))
                and bool(summary.get("continuous_ready"))
                and bool(summary.get("last_command_ok", True))
            )
            if mode_ready and (wipe <= WICKLER_LOW_PERCENT or wipe >= WICKLER_HIGH_PERCENT):
                raise RuntimeError(f"{role} Wippe ausserhalb Sicherheitsfenster: {wipe:.1f}%")
            all_ready = all_ready and (motion_ready if require_motion_ready else mode_ready)
        if all_ready:
            return last
        time.sleep(0.5)
    suffix = "motion-ready" if require_motion_ready else "ready"
    raise RuntimeError(f"Wickler wurden nicht {suffix}: {json.dumps(last, ensure_ascii=False, sort_keys=True)}")


def prepare(args: argparse.Namespace) -> int:
    _print("== MAS-004 Motor3 2000mm Diagnose: prepare ==")
    _print("Wickler werden gestoppt, Alarme quittiert, eingemessen und fuer kontinuierliche Bewegung freigegeben.")
    for role, base in (("unwinder", UNWINDER_BASE_URL), ("rewinder", REWINDER_BASE_URL)):
        _print(f"-- {role}: master indexedMode=0")
        wickler_master(base, {"indexedModeEnabled": "0"})
        for mode in ("stop", "resetAlarm", "etoRecovery", "calibrate"):
            reply = wickler_mode(base, mode, timeout_s=8.0)
            _print(f"-- {role}: {mode} -> {reply}")

    ready = wait_wicklers_ready(require_motion_ready=False, timeout_s=args.wickler_timeout_s)
    _print("Wickler eingemessen/bereit:")
    _print(json.dumps(ready, ensure_ascii=False, indent=2, sort_keys=True))

    for role, base in (("unwinder", UNWINDER_BASE_URL), ("rewinder", REWINDER_BASE_URL)):
        reply = wickler_mode(base, "ready", {"allowMotion": "1"}, timeout_s=8.0)
        _print(f"-- {role}: ready allowMotion=1 -> {reply}")

    motion_ready = wait_wicklers_ready(require_motion_ready=True, timeout_s=15.0)
    _print("Wickler motion-ready:")
    _print(json.dumps(motion_ready, ensure_ascii=False, indent=2, sort_keys=True))

    for command in (
        "PROCESS TRAVEL_DIAG RESET",
        "MOTOR 3 RESET_ALARM",
        "MOTOR 3 RECOVER_ETO",
        f"MOTOR 3 SET speed_mm_s={args.speed_mm_s:.3f} accel_mm_s2={args.ramp_mm_s2:.3f} decel_mm_s2={args.ramp_mm_s2:.3f}",
        "MOTOR 3 SET_POSITION_MM=0.000",
    ):
        reply = esp(command, timeout_s=5.0)
        _print(f"ESP {command} -> {reply}")

    motor = esp_json("MOTOR 3 REFRESH", timeout_s=5.0)
    snapshot = esp_json("PROCESS TRAVEL_DIAG STATUS?", timeout_s=5.0, idle_timeout_s=0.8)
    _print("Motor 3 bereit fuer manuelle Startfreigabe:")
    _print(json.dumps(motor, ensure_ascii=False, indent=2, sort_keys=True)[:4000])
    _print("Encoder-Nullpunkt:")
    _print(
        json.dumps(
            {
                "infeed_mm": snapshot.get("infeed_mm"),
                "drive_mm": snapshot.get("drive_mm"),
                "infeed_invalid_transitions": snapshot.get("infeed_invalid_transitions"),
                "drive_invalid_transitions": snapshot.get("drive_invalid_transitions"),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    _print("PREPARED: Jetzt keine ID3-Fahrt gestartet. Fuer die Fahrt: run aufrufen.")
    return 0


def _motor_state(payload: dict[str, Any]) -> dict[str, Any]:
    motor = payload.get("motor") if isinstance(payload.get("motor"), dict) else {}
    state = motor.get("state") if isinstance(motor.get("state"), dict) else {}
    merged = dict(motor)
    merged.update(state)
    return merged


def _collect_labels(snapshot: dict[str, Any], collected: dict[int, dict[str, Any]]) -> None:
    labels = snapshot.get("labels")
    if not isinstance(labels, list):
        return
    for label in labels:
        if not isinstance(label, dict):
            continue
        try:
            no = int(label.get("no"))
        except Exception:
            continue
        measured = label.get("measured_length_mm")
        drive = label.get("drive_length_mm")
        if measured is None:
            continue
        try:
            measured_f = float(measured)
        except Exception:
            continue
        if measured_f <= 0.0:
            continue
        item = dict(label)
        item["captured_at_infeed_mm"] = snapshot.get("infeed_mm")
        item["captured_at_drive_mm"] = snapshot.get("drive_mm")
        if drive is not None:
            try:
                item["encoder_delta_mm"] = measured_f - float(drive)
            except Exception:
                pass
        collected.setdefault(no, item)


def _abort_motion(reason: str) -> None:
    _print(f"ABORT: {reason}")
    motor_stop_commands = (
        "MOTOR 3 MOVE_VEL_MM_S=0",
    )
    for command in motor_stop_commands:
        try:
            reply = esp_retry(command, attempts=6, delay_s=0.8, timeout_s=2.0)
            _print(f"ESP {command} -> {reply}")
        except Exception as exc:
            _print(f"ESP {command} failed after retries: {exc}")

    set_wickler_leader(0.0)
    commands = [
        "PROCESS TRAVEL_DIAG STOP",
        "PROCESS PRODUCTION STOP",
        "PROCESS SETUP_MEASURE STOP",
        "PROCESS INDEXED STOP",
        "PROCESS PROFILE STOP",
    ]
    if reason.startswith("SAFETY:"):
        commands.append("SYNC MAS0001=21")
    for command in commands:
        try:
            _print(f"ESP {command} -> {esp_retry(command, attempts=3, delay_s=0.8, timeout_s=2.0)}")
        except Exception as exc:
            _print(f"ESP {command} failed: {exc}")
    for role, base in (("unwinder", UNWINDER_BASE_URL), ("rewinder", REWINDER_BASE_URL)):
        try:
            _print(f"{role} stop -> {wickler_mode(base, 'stop', timeout_s=3.0)}")
        except Exception as exc:
            _print(f"{role} stop failed: {exc}")


def run(args: argparse.Namespace) -> int:
    _print("== MAS-004 Motor3 2000mm Diagnose: run ==")
    _print("Starte genau eine 2000-mm-Absolutfahrt. Safety-Monitor stoppt bei Wickler-Randbereich/Alarm.")
    motion_ready = wait_wicklers_ready(require_motion_ready=True, timeout_s=10.0)
    _print("Wickler vor Start motion-ready:")
    _print(json.dumps(motion_ready, ensure_ascii=False, indent=2, sort_keys=True))

    for command in (
        "PROCESS TRAVEL_DIAG START",
        f"MOTOR 3 SET speed_mm_s={args.speed_mm_s:.3f} accel_mm_s2={args.ramp_mm_s2:.3f} decel_mm_s2={args.ramp_mm_s2:.3f}",
        "MOTOR 3 SET_POSITION_MM=0.000",
    ):
        reply = esp(command, timeout_s=5.0)
        _print(f"ESP {command} -> {reply}")
    # The ESP rebaselines the sensor debounce on TRAVEL_DIAG START and blanks
    # already-present material for 750 ms. Wait here so the measured labels are
    # collected from the actual 2000-mm move, not from stale parked levels.
    time.sleep(0.9)

    start_snapshot = esp_json("PROCESS TRAVEL_DIAG STATUS?", timeout_s=5.0, idle_timeout_s=0.8)
    start_motor = _motor_state(esp_json("MOTOR 3 REFRESH", timeout_s=5.0))
    _print("Start snapshot:")
    _print(
        json.dumps(
            {
                "infeed_mm": start_snapshot.get("infeed_mm"),
                "drive_mm": start_snapshot.get("drive_mm"),
                "motor_feedback_tenths_mm": start_motor.get("feedback_tenths_mm"),
                "steps_per_mm": (start_motor.get("config") or {}).get("steps_per_mm")
                if isinstance(start_motor.get("config"), dict)
                else start_motor.get("steps_per_mm"),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )

    collected: dict[int, dict[str, Any]] = {}
    samples: list[dict[str, Any]] = []
    deadline = time.time() + args.timeout_s
    started = time.time()
    planned_s = planned_motion_duration_s(TARGET_MM, args.speed_mm_s, args.ramp_mm_s2)
    esp_poll_after = started + planned_s + 3.0
    last_esp_ok = 0.0
    poll_interval_s = max(1.0, float(args.sample_s))
    last_motor: dict[str, Any] = {}
    final_snapshot: dict[str, Any] = {}
    try:
        set_wickler_leader(args.speed_mm_s)
        time.sleep(0.15)
        reply = esp_retry(
            f"MOTOR 3 MOVE_ABS_MM={TARGET_MM:.3f}",
            attempts=3,
            delay_s=1.0,
            timeout_s=5.0,
            idle_timeout_s=1.0,
        )
        _print(f"ESP MOTOR 3 MOVE_ABS_MM={TARGET_MM:.3f} -> {reply}")
        while time.time() < deadline:
            wickler_sample: dict[str, Any] = {}
            for role, base in (("unwinder", UNWINDER_BASE_URL), ("rewinder", REWINDER_BASE_URL)):
                state = wickler_summary(role, wickler_state(base))
                wickler_sample[role] = {
                    "wipe_percent": state.get("wipe_percent"),
                    "mode": state.get("mode"),
                    "alarm": state.get("alarm"),
                    "external_stop": state.get("external_stop"),
                }
                wipe = float(state.get("wipe_percent") or 50.0)
                if bool(state.get("alarm")) or wipe <= WICKLER_LOW_PERCENT or wipe >= WICKLER_HIGH_PERCENT:
                    raise RuntimeError(f"SAFETY: {role} {state}")

            now = time.time()
            if now < esp_poll_after:
                samples.append(
                    {
                        "t_s": round(now - started, 3),
                        "phase": "azd_absolute_move_blind_window",
                        "planned_motion_s": round(planned_s, 3),
                        "esp_poll_after_s": round(esp_poll_after - started, 3),
                        "wicklers": wickler_sample,
                    }
                )
                time.sleep(min(0.75, poll_interval_s))
                continue

            try:
                motor_payload = esp_json_retry("MOTOR 3 REFRESH", attempts=2, delay_s=0.8, timeout_s=5.0)
                last_motor = _require_motor_state(motor_payload)
                last_esp_ok = time.time()
            except Exception as exc:
                samples.append(
                    {
                        "t_s": round(time.time() - started, 3),
                        "motor_status_error": repr(exc),
                    }
                )
                if last_esp_ok > 0.0 and (time.time() - last_esp_ok) > float(args.esp_grace_s):
                    raise RuntimeError(f"ESP motor status lost for >{args.esp_grace_s:.1f}s: {exc}") from exc
                if last_esp_ok <= 0.0 and time.time() > esp_poll_after + max(35.0, float(args.esp_grace_s)):
                    raise RuntimeError(f"ESP motor status unavailable after planned move window: {exc}") from exc
                time.sleep(poll_interval_s)
                continue

            try:
                final_snapshot = esp_json_retry(
                    "PROCESS TRAVEL_DIAG STATUS?",
                    attempts=2,
                    delay_s=0.8,
                    timeout_s=5.0,
                    idle_timeout_s=0.8,
                )
            except Exception as exc:
                samples.append(
                    {
                        "t_s": round(time.time() - started, 3),
                        "travel_status_error": repr(exc),
                        "motor_feedback_tenths_mm": last_motor.get("feedback_tenths_mm"),
                        "motor_target_tenths_mm": last_motor.get("target_tenths_mm"),
                        "busy": last_motor.get("busy"),
                        "move": last_motor.get("move"),
                    }
                )
                final_snapshot = final_snapshot or {}
            _collect_labels(final_snapshot, collected)
            samples.append(
                {
                    "t_s": round(time.time() - started, 3),
                    "infeed_mm": final_snapshot.get("infeed_mm"),
                    "drive_mm": final_snapshot.get("drive_mm"),
                    "motor_feedback_tenths_mm": last_motor.get("feedback_tenths_mm"),
                    "motor_target_tenths_mm": last_motor.get("target_tenths_mm"),
                    "busy": last_motor.get("busy"),
                    "move": last_motor.get("move"),
                }
            )
            busy = bool(last_motor.get("busy")) or bool(last_motor.get("move"))
            if time.time() - started > 2.0 and not busy:
                break
            time.sleep(poll_interval_s)
        else:
            raise RuntimeError("Timeout waehrend 2000-mm-Fahrt")
    except Exception as exc:
        _abort_motion(str(exc))
        raise

    set_wickler_leader(0.0)
    esp_retry("PROCESS TRAVEL_DIAG STOP", attempts=3, delay_s=0.8, timeout_s=3.0)
    final_motor = _require_motor_state(esp_json_retry("MOTOR 3 REFRESH", attempts=3, delay_s=0.8, timeout_s=5.0))
    try:
        final_snapshot = esp_json_retry(
            "PROCESS TRAVEL_DIAG STATUS?",
            attempts=3,
            delay_s=0.8,
            timeout_s=5.0,
            idle_timeout_s=0.8,
        )
    except Exception as exc:
        _print(f"PROCESS TRAVEL_DIAG STATUS? final truncated/unavailable: {exc}")
        try:
            setup_status = esp_json_retry(
                "PROCESS SETUP_MEASURE STATUS?",
                attempts=3,
                delay_s=0.8,
                timeout_s=5.0,
                idle_timeout_s=0.8,
            )
        except Exception:
            setup_status = {}
        final_snapshot = dict(final_snapshot or {})
        for key in (
            "infeed_mm",
            "drive_mm",
            "infeed_invalid_transitions",
            "drive_invalid_transitions",
        ):
            if key not in final_snapshot and key in setup_status:
                final_snapshot[key] = setup_status.get(key)
        final_snapshot["status_error"] = repr(exc)
    _collect_labels(final_snapshot, collected)

    labels = [collected[key] for key in sorted(collected)[:10]]
    result = {
        "target_mm": TARGET_MM,
        "speed_mm_s": args.speed_mm_s,
        "ramp_mm_s2": args.ramp_mm_s2,
        "start_snapshot": start_snapshot,
        "final_snapshot": final_snapshot,
        "start_motor": start_motor,
        "final_motor": final_motor,
        "samples": samples,
        "labels_first_10": labels,
        "finished_ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    _print("== Ergebnis 2000-mm-Fahrt ==")
    _print(
        json.dumps(
            {
                "target_mm": TARGET_MM,
                "infeed_mm": final_snapshot.get("infeed_mm"),
                "drive_mm": final_snapshot.get("drive_mm"),
                "motor_feedback_tenths_mm": final_motor.get("feedback_tenths_mm"),
                "motor_target_tenths_mm": final_motor.get("target_tenths_mm"),
                "infeed_invalid_transitions": final_snapshot.get("infeed_invalid_transitions"),
                "drive_invalid_transitions": final_snapshot.get("drive_invalid_transitions"),
                "result_file": str(RESULT_PATH),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    _print("== Erste 10 gemessene Labels ==")
    for label in labels:
        _print(
            "Label {no}: Einlauf {measured_length_mm} mm | Auslauf {drive_length_mm} mm | Delta {encoder_delta_mm} mm".format(
                no=label.get("no"),
                measured_length_mm=label.get("measured_length_mm"),
                drive_length_mm=label.get("drive_length_mm"),
                encoder_delta_mm=label.get("encoder_delta_mm"),
            )
        )
    if not labels:
        _print("Keine abgeschlossenen Labels im Diagnosekontext erfasst.")
    _print("Bitte jetzt die real gefahrene Strecke physisch messen und zurueckmelden.")
    return 0


def status(_: argparse.Namespace) -> int:
    payload = {
        "travel_diag": esp_json("PROCESS TRAVEL_DIAG STATUS?", timeout_s=5.0, idle_timeout_s=0.8),
        "motor3": esp_json("MOTOR 3 REFRESH", timeout_s=5.0),
        "unwinder": wickler_summary("unwinder", wickler_state(UNWINDER_BASE_URL)),
        "rewinder": wickler_summary("rewinder", wickler_state(REWINDER_BASE_URL)),
    }
    _print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)[:12000])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MAS-004 Motor 3 / Encoder 2000-mm Diagnose")
    parser.add_argument("--speed-mm-s", type=float, default=DEFAULT_SPEED_MM_S)
    parser.add_argument("--ramp-mm-s2", type=float, default=DEFAULT_RAMP_MM_S2)
    sub = parser.add_subparsers(dest="cmd", required=True)

    prepare_parser = sub.add_parser("prepare", help="Wickler einmessen und ID3/Encoder fuer Freigabe vorbereiten")
    prepare_parser.add_argument("--wickler-timeout-s", type=float, default=90.0)
    prepare_parser.set_defaults(func=prepare)

    run_parser = sub.add_parser("run", help="Nach Benutzerfreigabe genau 2000 mm fahren und Diagnose sammeln")
    run_parser.add_argument("--timeout-s", type=float, default=60.0)
    run_parser.add_argument("--sample-s", type=float, default=1.2)
    run_parser.add_argument("--esp-grace-s", type=float, default=6.0)
    run_parser.set_defaults(func=run)

    status_parser = sub.add_parser("status", help="Livezustand passiv lesen")
    status_parser.set_defaults(func=status)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

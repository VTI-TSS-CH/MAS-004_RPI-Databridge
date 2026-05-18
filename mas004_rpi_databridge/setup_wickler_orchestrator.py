from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from mas004_rpi_databridge.config import Settings
from mas004_rpi_databridge.device_clients import EspPlcClient
from mas004_rpi_databridge.logstore import LogStore
from mas004_rpi_databridge.params import ParamStore
from mas004_rpi_databridge.smart_wickler_client import SmartWicklerClient


MOTOR3_STOP_TOLERANCE_MM = 0.05
MOTOR3_POSTPOSITION_MAX_ATTEMPTS = 3
MOTOR3_POSTPOSITION_ERROR_PKEY = "MAE0048"


@dataclass(frozen=True)
class SetupWicklerDefaults:
    learn_distance_mm: float = 1000.0
    learn_speed_mm_s: float = 100.0
    learn_ramp_mm_s2: float = 300.0


class SetupWicklerOrchestrator:
    """
    Productive setup helper for the Wickler measuring workflow.

    The external temporary test commands were retired. This class is only called
    by the Raspi machine-state logic when the machine enters setup.
    """

    def __init__(self, cfg: Settings, params: ParamStore, logs: LogStore):
        self.cfg = cfg
        self.params = params
        self.logs = logs
        self.defaults = SetupWicklerDefaults()
        self.esp = EspPlcClient(
            cfg.esp_host,
            cfg.esp_port,
            timeout_s=cfg.get_float("esp_connect_timeout_s", 1.5),
        )

    def _esp(self, line: str, read_timeout_s: float | None = None) -> str:
        response = self.esp.exchange_line(
            line,
            read_timeout_s=read_timeout_s
            or self.cfg.get_float("esp_command_timeout_s", 8.0),
        )
        self.logs.log("esp-plc", "info", f"setup wickler orchestration: {line} -> {response}")
        if response.upper().startswith("NAK"):
            raise RuntimeError(f"ESP rejected '{line}': {response}")
        return response

    def _configure_motor3(self, speed_mm_s: float, ramp_mm_s2: float) -> None:
        speed = max(1.0, abs(float(speed_mm_s)))
        ramp = max(1.0, abs(float(ramp_mm_s2)))
        self._esp(
            f"MOTOR 3 SET speed_mm_s={speed:.3f} accel_mm_s2={ramp:.3f} decel_mm_s2={ramp:.3f}"
        )

    def _motor3_operable(self, motor: dict[str, Any]) -> bool:
        # Some AZD installations do not expose a stable READY bit even though
        # the drive is link-ok, alarm-free and out of HWTO. Treat READY as
        # diagnostic information here; the measuring run is verified by exact
        # target/feedback stop tolerance afterwards.
        return (
            bool(motor.get("link_ok") or motor.get("linkOk"))
            and not bool(motor.get("alarm"))
            and not bool(motor.get("hwto"))
        )

    def _motor3_status(self) -> dict[str, Any]:
        return self._motor3_state_from_status(self._json_response(self._esp("MOTOR 3 REFRESH", read_timeout_s=5.0)))

    def _prepare_motor3_for_measurement(self) -> dict[str, Any]:
        # The measuring run starts from the current physical label position. It
        # is not a tolerance check yet; the tolerance is evaluated after the
        # 1000-mm pass and again after returning to this newly captured zero.
        for command in ("MOTOR 3 RESET_ALARM", "MOTOR 3 RECOVER_ETO"):
            self._esp(command, read_timeout_s=5.0)

        deadline = time.time() + 8.0
        last_state: dict[str, Any] = {}
        while time.time() < deadline:
            last_state = self._motor3_status()
            if self._motor3_operable(last_state):
                self._esp("MOTOR 3 SET_POSITION_MM=0.000", read_timeout_s=5.0)
                zero_state = self._motor3_status()
                self._set_motor3_postposition_error(False)
                return zero_state
            time.sleep(0.4)

        details = {
            key: last_state.get(key)
            for key in (
                "link_ok",
                "ready",
                "move",
                "alarm",
                "alarm_code",
                "hwto",
                "input_raw_hex",
                "output_raw_hex",
                "monitor0179_hex",
                "monitor017b_hex",
                "monitor017d_hex",
                "last_error",
                "last_reply",
            )
            if key in last_state
        }
        raise RuntimeError(f"Motor 3 is not operable for measuring travel: {details}")

    def _json_response(self, response: str) -> dict[str, Any]:
        text = (response or "").strip()
        if text.upper().startswith("JSON "):
            text = text[5:].strip()
        if not text.startswith("{"):
            return {}
        try:
            return dict(json.loads(text))
        except Exception:
            return {}

    def _motor3_state_from_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        motor = payload.get("motor") or {}
        if not isinstance(motor, dict):
            return {}
        state = motor.get("state") or {}
        if isinstance(state, dict):
            merged = dict(motor)
            merged.update(state)
            return merged
        return motor

    def _wait_motor3_idle(self, timeout_s: float = 60.0, min_wait_s: float = 0.0) -> dict[str, Any]:
        started = time.time()
        deadline = time.time() + float(timeout_s)
        seen_busy = False
        last_state: dict[str, Any] = {}
        while time.time() < deadline:
            # Global motor polling stays disabled during normal operation.
            # During a measuring run we need one explicit live refresh for
            # Motor 3, otherwise STATUS? may only return the cached state.
            payload = self._json_response(self._esp("MOTOR 3 REFRESH", read_timeout_s=5.0))
            motor = self._motor3_state_from_status(payload)
            last_state = motor
            busy = bool(motor.get("busy")) or bool(motor.get("move"))
            if busy:
                seen_busy = True
            elapsed = time.time() - started
            if motor and not busy and elapsed >= float(min_wait_s) and (
                seen_busy or elapsed > 2.0
            ):
                return motor
            time.sleep(0.25)
        details = {
            key: last_state.get(key)
            for key in (
                "ready",
                "busy",
                "move",
                "hwto",
                "alarm",
                "alarm_code",
                "feedback_tenths_mm",
                "command_tenths_mm",
                "target_tenths_mm",
                "input_raw_hex",
                "output_raw_hex",
                "last_error",
            )
            if key in last_state
        }
        raise RuntimeError(f"Motor 3 did not reach target during measuring travel: {details}")

    def _motor3_position_error_mm(self, motor: dict[str, Any]) -> float:
        steps = self._motor3_position_error_steps(motor)
        steps_per_mm = self._motor3_steps_per_mm(motor)
        if steps is not None and steps_per_mm > 0:
            return steps / steps_per_mm
        try:
            return (int(motor.get("target_tenths_mm")) - int(motor.get("feedback_tenths_mm"))) / 10.0
        except Exception as exc:
            raise RuntimeError(f"Motor 3 target/feedback unavailable for stop tolerance check: {motor}") from exc

    def _motor3_steps_per_mm(self, motor: dict[str, Any]) -> float:
        cfg = motor.get("config") if isinstance(motor.get("config"), dict) else {}
        raw_value = cfg.get("steps_per_mm") or cfg.get("stepsPerMm") or motor.get("steps_per_mm")
        try:
            steps_per_mm = float(raw_value)
        except Exception:
            return 0.0
        return steps_per_mm if steps_per_mm > 0 else 0.0

    def _motor3_position_error_steps(self, motor: dict[str, Any]) -> int | None:
        steps_per_mm = self._motor3_steps_per_mm(motor)
        feedback_steps = motor.get("feedback_steps", motor.get("feedbackSteps"))
        cfg = motor.get("config") if isinstance(motor.get("config"), dict) else {}
        invert = str(cfg.get("invert_direction", cfg.get("invertDirection", ""))).lower() in {
            "1",
            "true",
            "yes",
            "ja",
        }
        if steps_per_mm > 0 and feedback_steps is not None and motor.get("target_tenths_mm") is not None:
            zero_offset_steps = cfg.get("zero_offset_steps", cfg.get("zeroOffsetSteps"))
            if zero_offset_steps is not None:
                target_tenths_mm = int(motor.get("target_tenths_mm"))
                target_machine_steps = int(round((target_tenths_mm / 10.0) * steps_per_mm))
                feedback_relative_steps = int(feedback_steps) - int(zero_offset_steps)
                feedback_machine_steps = -feedback_relative_steps if invert else feedback_relative_steps
                return target_machine_steps - feedback_machine_steps

        command_steps = motor.get("command_steps", motor.get("commandSteps"))
        if steps_per_mm > 0 and command_steps is not None and feedback_steps is not None:
            controller_delta = int(command_steps) - int(feedback_steps)
            return -controller_delta if invert else controller_delta
        return None

    def _motor3_within_stop_tolerance(self, motor: dict[str, Any]) -> bool:
        # Prefer raw AZD steps here. The 1/10-mm display values are too coarse
        # for the required +/-0.05 mm print-stop tolerance.
        return abs(self._motor3_position_error_mm(motor)) <= MOTOR3_STOP_TOLERANCE_MM

    def _set_motor3_postposition_error(self, active: bool) -> None:
        ok, msg = self.params.apply_device_value(
            MOTOR3_POSTPOSITION_ERROR_PKEY,
            "1" if active else "0",
            promote_default=True,
        )
        if not ok and msg != "NAK_UnknownParam":
            self.logs.log(
                "raspi",
                "warning",
                f"{MOTOR3_POSTPOSITION_ERROR_PKEY} update failed: {msg}",
            )

    def _hold_wicklers_for_motor3_postpositioning(self) -> None:
        try:
            self._esp("PROCESS WICKLER CANCEL", read_timeout_s=5.0)
        except Exception as exc:
            self.logs.log("raspi", "info", f"Motor 3 post-positioning wickler cancel ignored: {repr(exc)}")
        errors: list[str] = []
        for client in self._winder_clients():
            try:
                client.post_master({"indexedModeEnabled": "0"}, timeout_s=5.0)
                client.post_mode("stop", timeout_s=5.0)
            except Exception as exc:
                errors.append(repr(exc))
        if errors:
            raise RuntimeError("Wicklers could not be held during Motor 3 post-positioning: " + "; ".join(errors))

    def _ensure_motor3_stop_tolerance(self, motor: dict[str, Any]) -> dict[str, Any]:
        if self._motor3_within_stop_tolerance(motor):
            self._set_motor3_postposition_error(False)
            return motor

        self._hold_wicklers_for_motor3_postpositioning()
        state = dict(motor)
        for attempt in range(1, MOTOR3_POSTPOSITION_MAX_ATTEMPTS + 1):
            correction_steps = self._motor3_position_error_steps(state)
            correction_mm = self._motor3_position_error_mm(state)
            step_suffix = f" ({correction_steps} steps)" if correction_steps is not None else ""
            self.logs.log(
                "raspi",
                "warning",
                f"Motor 3 post-positioning attempt {attempt}/{MOTOR3_POSTPOSITION_MAX_ATTEMPTS}: "
                f"error={correction_mm:.3f}mm{step_suffix} tolerance=+/-{MOTOR3_STOP_TOLERANCE_MM:.3f}mm",
            )
            if abs(correction_mm) <= MOTOR3_STOP_TOLERANCE_MM:
                self._set_motor3_postposition_error(False)
                return state

            if correction_steps is not None:
                self._esp(f"MOTOR 3 MOVE_REL_STEPS={correction_steps}", read_timeout_s=5.0)
            else:
                self._esp(f"MOTOR 3 MOVE_REL_MM_OP={correction_mm:.3f}", read_timeout_s=5.0)
            move_timeout = max(5.0, abs(correction_mm) / max(1.0, self.defaults.learn_speed_mm_s) + 4.0)
            state = self._wait_motor3_idle(timeout_s=move_timeout, min_wait_s=0.1)
            self._hold_wicklers_for_motor3_postpositioning()

        final_error_mm = self._motor3_position_error_mm(state)
        if self._motor3_within_stop_tolerance(state):
            self._set_motor3_postposition_error(False)
            return state
        self._set_motor3_postposition_error(True)
        raise RuntimeError(
            f"Motor 3 failed stop tolerance after {MOTOR3_POSTPOSITION_MAX_ATTEMPTS} corrections: "
            f"error={final_error_mm:.3f}mm tolerance=+/-{MOTOR3_STOP_TOLERANCE_MM:.3f}mm"
        )

    def _winder_clients(self) -> list[SmartWicklerClient]:
        return [SmartWicklerClient(self.cfg, "unwinder"), SmartWicklerClient(self.cfg, "rewinder")]

    def _run_wickler_commands_parallel(self, action: str, timeout_s: float = 5.0) -> list[dict[str, Any]]:
        clients = self._winder_clients()
        results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=len(clients)) as executor:
            futures = {
                executor.submit(client.post_mode, action, timeout_s=timeout_s): client.descriptor.role
                for client in clients
            }
            for future in as_completed(futures):
                role = futures[future]
                try:
                    reply = future.result()
                    results.append({"role": role, "action": action, "ok": bool(reply.get("ok", True)), "reply": reply})
                except Exception as exc:
                    results.append({"role": role, "action": action, "ok": False, "error": str(exc)})
        errors = [f"{item['role']} {action}: {item.get('error') or item.get('reply')}" for item in results if not item.get("ok")]
        if errors:
            raise RuntimeError("; ".join(errors))
        return results

    def _wait_wicklers_ready(self, timeout_s: float = 90.0) -> None:
        deadline = time.time() + float(timeout_s)
        while time.time() < deadline:
            all_ready = True
            for client in self._winder_clients():
                state = client.fetch_state()
                telemetry = state.get("telemetry") or {}
                drive = state.get("drive") or {}
                mode = str(telemetry.get("modeLabel") or "")
                ready = mode in {"Bereit", "Warnung"} and not bool(drive.get("alarm"))
                all_ready = all_ready and ready
            if all_ready:
                return
            time.sleep(1.0)
        raise RuntimeError("Wicklers did not become ready")

    def run(self) -> dict[str, Any]:
        distance = self.defaults.learn_distance_mm
        speed = self.defaults.learn_speed_mm_s
        ramp = self.defaults.learn_ramp_mm_s2
        self._configure_motor3(speed, ramp)

        for client in self._winder_clients():
            client.post_master({"indexedModeEnabled": "0"}, timeout_s=5.0)
        for action in ("stop", "resetAlarm", "etoRecovery"):
            self._run_wickler_commands_parallel(action, timeout_s=5.0)
        self._run_wickler_commands_parallel("calibrate", timeout_s=5.0)
        self._wait_wicklers_ready()
        self._prepare_motor3_for_measurement()

        forward_candidates = self._learn_diameter_pass(distance, speed)
        reverse_candidates = self._learn_diameter_pass(-distance, speed)
        applied = []
        for role in ("unwinder", "rewinder"):
            values = []
            if role in forward_candidates:
                values.append(forward_candidates[role])
            if role in reverse_candidates:
                values.append(reverse_candidates[role])
            if not values:
                continue
            diameter = sum(values) / float(len(values))
            SmartWicklerClient(self.cfg, role).set_diameter(diameter, persist=True, timeout_s=5.0)
            applied.append(f"{role}:{diameter:.1f}mm")

        if len(applied) < 2:
            raise RuntimeError("Wickler diameter learning did not return both roll diameters")
        self.logs.log("raspi", "info", "setup wickler learn applied: " + ", ".join(applied))
        return {"ok": True, "applied": applied, "distance_mm": distance, "speed_mm_s": speed, "ramp_mm_s2": ramp}

    def _learn_diameter_pass(self, distance_mm: float, speed_mm_s: float) -> dict[str, float]:
        for client in self._winder_clients():
            client.start_diameter_learning(timeout_s=5.0)
        # Diameter learning is an explicit setup move, not a productive takt.
        # Use Operation-Data so the AZD receives a selected operation and a real
        # start in one Modbus write; the Direct-Data path is reserved for the
        # hardware-synchronised Motor-3 takt path.
        self._esp(f"MOTOR 3 MOVE_REL_MM_OP={distance_mm:.3f}", read_timeout_s=5.0)
        abs_distance = abs(float(distance_mm))
        abs_speed = max(1.0, abs(float(speed_mm_s)))
        ramp = max(1.0, self.defaults.learn_ramp_mm_s2)
        ramp_distance = (abs_speed * abs_speed) / ramp
        if abs_distance > ramp_distance:
            expected_move_s = (abs_distance / abs_speed) + (abs_speed / ramp)
        else:
            expected_move_s = 2.0 * ((abs_distance / ramp) ** 0.5)
        motor_state = self._wait_motor3_idle(
            timeout_s=max(30.0, expected_move_s + 20.0),
            min_wait_s=max(0.0, expected_move_s + 0.5),
        )
        time.sleep(1.0)

        candidates: dict[str, float] = {}
        for role in ("unwinder", "rewinder"):
            payload = SmartWicklerClient(self.cfg, role).finish_diameter_learning(
                travel_mm=abs(distance_mm),
                apply=False,
                method="position-local",
                timeout_s=5.0,
            )
            if payload.get("ok"):
                candidate = float(payload.get("candidateDiameterMm") or payload.get("diameterMm") or 0.0)
                if candidate > 0.0:
                    candidates[role] = candidate
                    self.logs.log(
                        "raspi",
                        "info",
                        f"setup diameter candidate {role}: {candidate:.1f}mm after {abs_distance:.1f}mm pass",
                    )
        self._ensure_motor3_stop_tolerance(motor_state)
        return candidates

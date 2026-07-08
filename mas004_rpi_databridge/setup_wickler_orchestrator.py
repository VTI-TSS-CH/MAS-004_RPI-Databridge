from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable

from mas004_rpi_databridge.config import Settings
from mas004_rpi_databridge.device_clients import EspPlcClient
from mas004_rpi_databridge.io_master import IoStore
from mas004_rpi_databridge.io_runtime import IoRuntime
from mas004_rpi_databridge.logstore import LogStore
from mas004_rpi_databridge.motor_master_sync import sync_motor_master_values
from mas004_rpi_databridge.moxa_iologik import MoxaE1213Client
from mas004_rpi_databridge.outbox import Outbox
from mas004_rpi_databridge.params import ParamStore
from mas004_rpi_databridge.peers import peer_urls
from mas004_rpi_databridge.smart_wickler_client import SmartWicklerClient
from mas004_rpi_databridge.state_dedupe import ValueDedupeStore


MOTOR3_STOP_TOLERANCE_MM = 0.05
MOTOR3_POSTPOSITION_MAX_ATTEMPTS = 3
MOTOR3_POSTPOSITION_ERROR_PKEY = "MAE0048"
MOTOR3_MEASUREMENT_START_MAX_ATTEMPTS = 2
MOTOR3_SCALE_CALIBRATION_TARGET_MM = 2000.0
MOTOR3_SCALE_CALIBRATION_TOLERANCE_MM = 0.5
MOTOR3_SCALE_CALIBRATION_MAX_ATTEMPTS = 4
MOTOR3_SCALE_CALIBRATION_MAX_CORRECTION_FACTOR = 0.05
MOTOR3_SETUP_READY_TIMEOUT_S = 12.0
MOTOR3_SETUP_RECOVERY_ATTEMPTS = 4
WICKLER_MOTION_ABORT_LOW_PERCENT = 5.0
WICKLER_MOTION_ABORT_HIGH_PERCENT = 95.0
WICKLER_STATE_SAFETY_TIMEOUT_S = 0.8
SETUP_MEASURE_STATUS_READ_TIMEOUT_S = 4.0
SETUP_MEASURE_STATUS_POLL_S = 1.0
SETUP_MEASURE_STATUS_LOG_INTERVAL_S = 5.0
SETUP_MEASURE_STATUS_MAX_CONSECUTIVE_ERRORS = 8
SETUP_MEASURE_STATUS_RUNNING_COMM_GRACE_S = 30.0
WICKLER_DIAMETER_FINALIZE_ATTEMPTS = 5
WICKLER_DIAMETER_FINALIZE_RETRY_BASE_S = 0.4
INFEED_TEACH_MOXA_TIMEOUT_S = 3.0
INFEED_TEACH_MOXA_ATTEMPTS = 4

@dataclass(frozen=True)
class SetupWicklerDefaults:
    learn_speed_mm_s: float = 100.0
    learn_ramp_mm_s2: float = 300.0
    sensor_teach_min_ms: int = 3500
    sensor_teach_labels: int = 0
    sensor_teach_extra_mm: float = 0.0
    sensor_settle_forward_ms: int = 5000
    control_post_teach_ms: int = 5000
    setup_backoff_mm: float = 10.0
    control_preteach_mm: float = 10.0


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
        self._wickler_offline_fault_counts: dict[str, int] = {}
        self._esp_status_log_state: dict[str, dict[str, Any]] = {}
        self._setup_sync_dedupe = ValueDedupeStore(self.params.db)
        self.esp = EspPlcClient(
            cfg.esp_host,
            cfg.esp_port,
            timeout_s=cfg.get_float("esp_connect_timeout_s", 1.5),
        )

    def _should_log_esp_exchange(self, line: str, response: str) -> tuple[bool, str]:
        upper = str(line or "").upper()
        if "STATUS?" not in upper:
            return True, str(response)
        signature = str(response or "").strip()[:160]
        payload: dict[str, Any] = {}
        last_error = ""
        try:
            text = str(response or "").strip()
            if text.upper().startswith("JSON "):
                text = text[5:].strip()
            payload = json.loads(text or "{}")
            last_error = str(payload.get("last_error") or "").strip()
            signature = "|".join(
                str(payload.get(key, ""))
                for key in (
                    "running",
                    "completed",
                    "phase",
                    "phase_name",
                    "azd_target_mm",
                    "move_commanded",
                    "wickler_run_line_active",
                    "reference_found",
                    "control_teach_started",
                    "control_teach_done",
                    "last_error",
                )
            )
            summary = (
                f"JSON status running={payload.get('running')} completed={payload.get('completed')} "
                f"phase={payload.get('phase')}:{payload.get('phase_name')} "
                f"azd_target={payload.get('azd_target_mm')} move={payload.get('move_commanded')} "
                f"age={payload.get('move_command_age_ms')} wickler_run={payload.get('wickler_run_line_active')} "
                f"ref={payload.get('reference_found')} logical_infeed={payload.get('logical_infeed_mm')} "
                f"return_error={payload.get('return_error_mm')} labels={payload.get('labels_measured')} "
                f"last_error={payload.get('last_error')!r}"
            )
        except Exception:
            summary = str(response)
        now = time.time()
        state = self._esp_status_log_state.get(line) or {}
        last_signature = str(state.get("signature") or "")
        last_ts = float(state.get("ts") or 0.0)
        force_log = bool(payload.get("completed")) or bool(last_error)
        if (
            force_log
            or signature != last_signature
            or (now - last_ts) >= SETUP_MEASURE_STATUS_LOG_INTERVAL_S
        ):
            self._esp_status_log_state[line] = {"signature": signature, "ts": now}
            return True, summary
        return False, summary

    def _esp(self, line: str, read_timeout_s: float | None = None, *, priority: bool = True) -> str:
        try:
            read_timeout = (
                read_timeout_s
                if read_timeout_s is not None
                else self.cfg.get_float("esp_command_timeout_s", 8.0)
            )
            wait_timeout = max(
                0.5,
                min(
                    self.cfg.get_float("esp_connect_timeout_s", 1.5) + float(read_timeout) + 0.5,
                    6.0,
                ),
            )
            response = self.esp.exchange_line(
                line,
                read_timeout_s=read_timeout,
                priority=priority,
                wait_timeout_s=wait_timeout,
            )
        except Exception as exc:
            self.logs.log("esp-plc", "warning", f"setup wickler orchestration failed: {line} -> {repr(exc)}")
            raise
        should_log, log_response = self._should_log_esp_exchange(line, response)
        if should_log:
            self.logs.log("esp-plc", "info", f"setup wickler orchestration: {line} -> {log_response}")
        if response.upper().startswith("NAK"):
            raise RuntimeError(f"ESP rejected '{line}': {response}")
        return response

    def _esp_retry(
        self,
        line: str,
        *,
        read_timeout_s: float | None = None,
        attempts: int = 2,
        settle_s: float = 0.25,
        priority: bool = True,
    ) -> str:
        errors: list[str] = []
        max_attempts = max(1, int(attempts))
        for attempt in range(1, max_attempts + 1):
            try:
                return self._esp(line, read_timeout_s=read_timeout_s, priority=priority)
            except Exception as exc:
                errors.append(repr(exc))
                if attempt >= max_attempts:
                    break
                self.logs.log(
                    "esp-plc",
                    "warning",
                    f"setup wickler orchestration retry {attempt + 1}/{max_attempts}: {line} after {repr(exc)}",
                )
                time.sleep(max(0.0, float(settle_s)) * attempt)
        raise RuntimeError(f"ESP command failed after {max_attempts} attempts: {line}; errors={errors}")

    def _motor_auto_poll_state(self) -> dict[str, Any]:
        return self._json_response(self._esp("MOTOR POLL?", read_timeout_s=2.0))

    def _ensure_motor_auto_poll_disabled_for_setup(self) -> dict[str, Any]:
        if bool(getattr(self.cfg, "esp_simulation", False)):
            return {"ok": True, "skipped": "simulation"}
        status = self._motor_auto_poll_state()
        result: dict[str, Any] = {
            "ok": True,
            "before": status,
            "disabled": False,
        }
        if not bool(status.get("auto_poll")):
            self.logs.log("esp-plc", "info", "setup motor auto-poll already disabled")
            return result
        response = self._esp("MOTOR POLL=0", read_timeout_s=2.0)
        verify = self._motor_auto_poll_state()
        result["disable_response"] = response
        result["after_disable"] = verify
        result["disabled"] = True
        if bool(verify.get("auto_poll")):
            raise RuntimeError(f"ESP motor auto-poll could not be disabled for setup: {result}")
        self.logs.log("esp-plc", "info", "setup motor auto-poll disabled for measurement")
        return result

    def _refresh_motor3_for_setup_status(self) -> dict[str, Any]:
        if bool(getattr(self.cfg, "esp_simulation", False)):
            return {"ok": True, "skipped": "simulation"}
        try:
            payload = self._json_response(self._esp("MOTOR 3 REFRESH", read_timeout_s=2.5))
            return {"ok": bool(payload.get("ok", True)), "motor": payload.get("motor")}
        except Exception as exc:
            self.logs.log("esp-plc", "warning", f"setup Motor 3 targeted refresh failed: {repr(exc)}")
            return {"ok": False, "error": repr(exc)}

    def _setup_abort_reason(self) -> str:
        mas0001 = self._param_int("MAS0001", 0)
        mas0002 = self._param_int("MAS0002", 0)
        mas0028 = self._param_int("MAS0028", 0)
        if mas0028:
            return f"Purge active during setup: MAS0028={mas0028}"
        if mas0001 in {20, 21}:
            return f"Safety/fault state during setup: MAS0001={mas0001}"
        if mas0001 in {2, 3} and mas0002 in {0, 3}:
            return ""
        if mas0002 == 3 and mas0001 not in {20, 21}:
            return ""
        return f"Setup no longer active: MAS0001={mas0001}, MAS0002={mas0002}, MAS0028={mas0028}"

    def _abort_if_not_setup_active(self) -> None:
        reason = self._setup_abort_reason()
        if reason:
            raise RuntimeError(reason)

    def stop_all_motion(self) -> None:
        def _stop_esp() -> None:
            commands: list[tuple[str, float]] = [
                ("PROCESS SETUP_MEASURE STOP", 1.0),
                ("PROCESS PRODUCTION STOP", 1.0),
                ("MOTOR 3 MOVE_VEL_MM_S=0", 1.0),
                ("PROCESS WICKLER CANCEL", 1.0),
                ("PROCESS INDEXED STOP", 1.0),
                ("PROCESS PROFILE STOP", 1.0),
            ]
            for command, timeout_s in commands:
                try:
                    self._esp(command, read_timeout_s=timeout_s)
                except Exception as exc:
                    self.logs.log("raspi", "info", f"setup abort cleanup ignored for {command}: {repr(exc)}")

            # Read status only after the stop sequence. A blocked STATUS? call
            # must never delay the actual Motor-3/Wickler stop commands.
            try:
                status = self._setup_measure_status()
                self.logs.log(
                    "esp-plc",
                    "info",
                    "setup abort status after cleanup: "
                    + json.dumps(status, ensure_ascii=False, sort_keys=True),
                )
            except Exception as exc:
                self.logs.log("raspi", "info", f"setup abort post-status ignored: {repr(exc)}")

            final_commands: list[tuple[str, float]] = (
                [
                    ("MOTOR 3 MOVE_VEL_MM_S=0", 0.8),
                    ("PROCESS WICKLER CANCEL", 0.8),
                ]
            )
            for command, timeout_s in final_commands:
                try:
                    self._esp(command, read_timeout_s=timeout_s)
                except Exception as exc:
                    self.logs.log("raspi", "info", f"setup abort final cleanup ignored for {command}: {repr(exc)}")

        def _stop_wickler(client: SmartWicklerClient) -> None:
            role = self._wickler_role(client)
            try:
                client.cancel_diameter_learning(timeout_s=2.0)
            except Exception as exc:
                self.logs.log(
                    "raspi",
                    "warning",
                    f"setup abort diameter cancel ignored for {role}: {repr(exc)}",
                )
            try:
                client.post_master(
                    {"indexedModeEnabled": "0", **self._wickler_master_threshold_payload()},
                    timeout_s=2.0,
                )
            except Exception as exc:
                self.logs.log(
                    "raspi",
                    "warning",
                    f"setup abort indexed disable ignored for {role}: {repr(exc)}",
                )
            try:
                client.post_mode("stop", timeout_s=2.0)
            except Exception as exc:
                self.logs.log(
                    "raspi",
                    "warning",
                    f"setup abort stop ignored for {role}: {repr(exc)}",
                )

        clients = self._winder_clients()
        with ThreadPoolExecutor(max_workers=1 + len(clients)) as executor:
            futures = [executor.submit(_stop_esp)]
            futures.extend(executor.submit(_stop_wickler, client) for client in clients)
            for future in as_completed(futures):
                future.result()

    def _sync_setup_params_to_esp(self) -> None:
        keys = (
            "MAP0001",  # label width
            "MAP0002",  # label length
            "MAP0014",  # setup/production feed speed fallback
            "MAP0021",  # label-control sensor distance
            "MAP0040",  # label length / slip tolerance
            "MAP0045",  # infeed sensor debounce travel
            "MAP0046",  # control sensor debounce travel
            "MAP0076",  # label length compensation
            "MAP0077",  # infeed encoder effective diameter
            "MAP0078",  # drive/outfeed encoder effective diameter
        )
        for key in keys:
            value = str(self.params.get_effective_value(key) or "").strip()
            if not value:
                continue
            if self._setup_sync_dedupe.is_duplicate("esp-setup-sync", key, value):
                continue
            self._esp_retry(f"SYNC {key}={value}", read_timeout_s=5.0, attempts=3, settle_s=0.35)
            self._setup_sync_dedupe.remember("esp-setup-sync", key, value)

    def _configure_motor3(self, speed_mm_s: float, ramp_mm_s2: float) -> None:
        speed = max(1.0, abs(float(speed_mm_s)))
        ramp = max(1.0, abs(float(ramp_mm_s2)))
        self._esp(
            f"MOTOR 3 SET speed_mm_s={speed:.3f} accel_mm_s2={ramp:.3f} decel_mm_s2={ramp:.3f}"
        )

    def _setup_learn_speed_mm_s(self) -> float:
        try:
            value = self.params.get_effective_value("MAP0014")
            speed = abs(float(str(value or "").strip()))
        except Exception:
            speed = 0.0
        if speed <= 0.0:
            speed = float(self.defaults.learn_speed_mm_s)
        return max(1.0, min(250.0, speed))

    def _motor3_operable(self, motor: dict[str, Any]) -> bool:
        return (
            bool(motor.get("link_ok") or motor.get("linkOk"))
            and not bool(motor.get("alarm"))
            and not bool(motor.get("hwto"))
        )

    def _motor3_ready_for_setup_measurement(self, motor: dict[str, Any]) -> bool:
        return (
            self._motor3_operable(motor)
            and bool(motor.get("ready"))
            and not bool(motor.get("busy"))
            and not bool(motor.get("move"))
        )

    @staticmethod
    def _motor3_setup_ready_details(motor: dict[str, Any]) -> dict[str, Any]:
        return {
            key: motor.get(key)
            for key in (
                "link_ok",
                "ready",
                "busy",
                "move",
                "alarm",
                "alarm_code",
                "hwto",
                "input_raw_hex",
                "output_raw_hex",
                "monitor0179_hex",
                "monitor017b_hex",
                "monitor017d_hex",
                "mps",
                "mbc",
                "edm",
                "last_error",
                "last_reply",
            )
            if key in motor
        }

    def _motor3_status(self) -> dict[str, Any]:
        return self._motor3_state_from_status(self._json_response(self._esp("MOTOR 3 REFRESH", read_timeout_s=5.0)))

    def _recover_motor3_for_setup_measurement(self, reason: str, state: dict[str, Any], attempt: int) -> None:
        self.logs.log(
            "esp-plc",
            "warning",
            "Motor 3 setup recovery "
            f"{attempt}/{MOTOR3_SETUP_RECOVERY_ATTEMPTS} ({reason}): "
            + json.dumps(self._motor3_setup_ready_details(state), ensure_ascii=False, sort_keys=True),
        )
        for command in ("MOTOR 3 MOVE_VEL_MM_S=0", "MOTOR 3 RESET_ALARM", "MOTOR 3 RECOVER_ETO"):
            try:
                self._esp_retry(command, read_timeout_s=5.0, attempts=2, settle_s=0.25)
            except Exception as exc:
                self.logs.log(
                    "esp-plc",
                    "warning",
                    f"Motor 3 setup recovery command failed: {command}: {repr(exc)}",
                )

    def _ensure_motor3_ready_for_setup_measurement(
        self,
        reason: str,
        *,
        timeout_s: float = MOTOR3_SETUP_READY_TIMEOUT_S,
    ) -> dict[str, Any]:
        deadline = time.time() + max(1.0, float(timeout_s))
        last_state: dict[str, Any] = {}
        recoveries = 0
        while time.time() < deadline:
            self._abort_if_not_setup_active()
            last_state = self._motor3_status()
            if self._motor3_ready_for_setup_measurement(last_state):
                return last_state
            if recoveries < MOTOR3_SETUP_RECOVERY_ATTEMPTS:
                recoveries += 1
                self._recover_motor3_for_setup_measurement(reason, last_state, recoveries)
            time.sleep(0.5)
        raise RuntimeError(
            f"Motor 3 is not ready for setup measurement ({reason}): "
            + json.dumps(self._motor3_setup_ready_details(last_state), ensure_ascii=False, sort_keys=True)
        )

    def _prepare_motor3_for_measurement(self) -> dict[str, Any]:
        # The measuring run starts from the current physical label position. It
        # is not a tolerance check yet; the tolerance is evaluated after the
        # 1000-mm pass and again after returning to this newly captured zero.
        def _zero_current_position(motor_state: dict[str, Any]) -> dict[str, Any] | None:
            if not self._motor3_ready_for_setup_measurement(motor_state):
                return None
            try:
                self._esp_retry("MOTOR 3 SET_POSITION_MM=0.000", read_timeout_s=5.0, attempts=2)
            except Exception as exc:
                verify_state = self._motor3_status()
                feedback = int(verify_state.get("feedback_tenths_mm") or 0)
                target = int(verify_state.get("target_tenths_mm") or 0)
                moving = bool(verify_state.get("move")) or bool(verify_state.get("busy"))
                if (
                    feedback != 0
                    or target != 0
                    or moving
                    or not self._motor3_ready_for_setup_measurement(verify_state)
                ):
                    raise
                self.logs.log(
                    "esp-plc",
                    "warning",
                    f"MOTOR 3 SET_POSITION_MM=0.000 ACK missing, verified zero state: {repr(exc)}",
                )
                self._set_motor3_postposition_error(False)
                return verify_state
            zero_state = self._motor3_status()
            if not self._motor3_ready_for_setup_measurement(zero_state):
                return None
            self._set_motor3_postposition_error(False)
            return zero_state

        try:
            initial_state = self._ensure_motor3_ready_for_setup_measurement("before_zero")
            zero_state = _zero_current_position(initial_state)
            if zero_state is not None:
                return zero_state
        except Exception as exc:
            self.logs.log(
                "esp-plc",
                "warning",
                f"Motor 3 preflight status/zero before recovery failed, recovery continues: {repr(exc)}",
            )

        for command in ("MOTOR 3 RESET_ALARM", "MOTOR 3 RECOVER_ETO"):
            try:
                self._esp_retry(command, read_timeout_s=5.0, attempts=2, settle_s=0.4)
            except Exception as exc:
                try:
                    current_state = self._motor3_status()
                    if self._motor3_operable(current_state):
                        self.logs.log(
                            "esp-plc",
                            "warning",
                            f"{command} ACK missing, but Motor 3 is operable; continuing: {repr(exc)}",
                        )
                        break
                except Exception as status_exc:
                    self.logs.log(
                        "esp-plc",
                        "warning",
                        f"{command} failed and status check also failed: {repr(exc)} / {repr(status_exc)}",
                    )
                    raise exc
                raise

        deadline = time.time() + 8.0
        last_state: dict[str, Any] = {}
        while time.time() < deadline:
            self._abort_if_not_setup_active()
            last_state = self._ensure_motor3_ready_for_setup_measurement("before_zero_retry", timeout_s=2.0)
            zero_state = _zero_current_position(last_state)
            if zero_state is not None:
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

    def _prepare_production_pause_baseline(self) -> dict[str, Any]:
        """Clear the realtime label process before setup hands over to Pause."""
        self._abort_if_not_setup_active()
        process_response = ""
        try:
            process_response = self._esp_retry("PROCESS PRODUCTION_RESET", read_timeout_s=5.0, attempts=3)
        except Exception as exc:
            # Older ESP firmware only knows PROCESS RESET. Keep the setup
            # workflow usable, but make the degraded fallback visible in logs.
            self.logs.log("esp-plc", "warning", f"PROCESS PRODUCTION_RESET fallback to PROCESS RESET: {exc}")
            process_response = self._esp_retry("PROCESS RESET", read_timeout_s=5.0, attempts=2)
        try:
            motor_response = self._esp_retry("MOTOR 3 SET_POSITION_MM=0.000", read_timeout_s=5.0, attempts=3)
        except Exception as exc:
            motor_state = self._motor3_status()
            feedback = int(motor_state.get("feedback_tenths_mm") or 0)
            target = int(motor_state.get("target_tenths_mm") or 0)
            if feedback != 0 or target != 0 or bool(motor_state.get("move")) or bool(motor_state.get("busy")):
                raise
            self.logs.log(
                "esp-plc",
                "warning",
                f"MOTOR 3 SET_POSITION_MM=0.000 ACK missing, verified zero state: {repr(exc)}",
            )
            motor_response = "ACK_INFERRED_MOTOR3_ZERO_AFTER_TIMEOUT"
        self._set_motor3_postposition_error(False)
        self._abort_if_not_setup_active()
        cleared_latches: list[str] = []
        for key in ("MAE0024", "MAE0025", "MAE0026", "MAE0027", "MAE0048", "MAS0028"):
            ok, msg = self._set_fault_value(key, "0", origin="setup-baseline")
            if ok:
                cleared_latches.append(key)
            else:
                self.logs.log("raspi", "warning", f"setup baseline could not clear {key}: {msg}")
        if cleared_latches:
            self.logs.log("raspi", "info", "setup baseline cleared process latches: " + ",".join(cleared_latches))
        return {
            "process_response": process_response,
            "motor3_zero_response": motor_response,
            "cleared_latches": cleared_latches,
        }

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

    def _wait_motor3_idle(
        self,
        timeout_s: float = 60.0,
        min_wait_s: float = 0.0,
        *,
        monitor_wicklers: bool = False,
    ) -> dict[str, Any]:
        started = time.time()
        deadline = time.time() + float(timeout_s)
        seen_busy = False
        last_state: dict[str, Any] = {}
        next_wickler_check = started
        while time.time() < deadline:
            self._abort_if_not_setup_active()
            if monitor_wicklers and time.time() >= next_wickler_check:
                self._abort_motor3_if_wickler_faulted()
                next_wickler_check = time.time() + 0.5
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
            if motor and not busy and elapsed >= float(min_wait_s):
                if seen_busy:
                    return motor
                try:
                    if self._motor3_within_stop_tolerance(motor):
                        return motor
                except RuntimeError:
                    pass
                # An ACK without MOVE/BUSY and without target progress is a
                # failed start, not a completed travel. Do not continue into
                # post-positioning with the full measuring distance as error.
            if motor and not busy and not seen_busy and elapsed > 2.0:
                try:
                    target_reached = self._motor3_within_stop_tolerance(motor)
                except RuntimeError:
                    target_reached = False
                if target_reached:
                    time.sleep(0.25)
                    continue
                details = {
                    key: motor.get(key)
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
                        "feedback_steps",
                        "command_steps",
                        "input_raw_hex",
                        "output_raw_hex",
                        "last_reply",
                        "last_error",
                    )
                    if key in motor
                }
                raise RuntimeError(f"Motor 3 move was accepted but did not start/reach target: {details}")
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

    def _infeed_encoder_mm(self) -> float:
        status = self._setup_measure_status()
        raw_value = status.get("infeed_mm")
        if raw_value is None:
            raw_value = status.get("logical_infeed_mm")
        try:
            value = float(raw_value)
        except Exception as exc:
            raise RuntimeError(f"Einlaufencoderwert fuer Motor-3-Skalierung nicht verfuegbar: {status}") from exc
        return value

    def _persist_motor3_steps_per_mm(self, steps_per_mm: float) -> dict[str, Any]:
        value = float(steps_per_mm)
        if value <= 0.0:
            raise RuntimeError(f"Motor 3 steps_per_mm invalid: {value}")
        self._esp(f"MOTOR 3 SET steps_per_mm={value:.6f}", read_timeout_s=5.0)
        self._esp("MOTOR 3 SAVE", read_timeout_s=5.0)
        state = self._motor3_status()
        config = state.get("config") if isinstance(state.get("config"), dict) else {}
        snapshot_state = {key: item for key, item in state.items() if key != "config"}
        sync_motor_master_values(
            self.params,
            self.cfg,
            3,
            {"config": config, "state": snapshot_state},
            {},
            sync_position_default=False,
        )
        return state

    def _calibrate_motor3_scale_against_infeed_encoder(
        self,
        speed_mm_s: float,
        ramp_mm_s2: float,
    ) -> dict[str, Any]:
        target_mm = MOTOR3_SCALE_CALIBRATION_TARGET_MM
        attempts: list[dict[str, Any]] = []
        correction_applied = False

        self.logs.log(
            "raspi",
            "info",
            f"Motor 3 scale calibration start: target={target_mm:.1f}mm tolerance=+/-"
            f"{MOTOR3_SCALE_CALIBRATION_TOLERANCE_MM:.1f}mm",
        )

        for attempt in range(1, MOTOR3_SCALE_CALIBRATION_MAX_ATTEMPTS + 1):
            self._abort_if_not_setup_active()
            self._abort_motor3_if_wickler_faulted()
            self._configure_motor3(speed_mm_s, ramp_mm_s2)
            self._esp("PROCESS PRODUCTION_RESET", read_timeout_s=5.0)
            self._esp("MOTOR 3 SET_POSITION_MM=0.000", read_timeout_s=5.0)
            zero_state = self._motor3_status()
            steps_per_mm = self._motor3_steps_per_mm(zero_state)
            if steps_per_mm <= 0.0:
                raise RuntimeError(f"Motor 3 steps_per_mm unavailable before scale calibration: {zero_state}")

            expected_move_s = self._expected_motor3_move_seconds(target_mm, speed_mm_s)
            self.logs.log(
                "raspi",
                "info",
                f"Motor 3 scale calibration pass {attempt}: move absolute {target_mm:.1f}mm "
                f"with steps_per_mm={steps_per_mm:.6f}",
            )
            motor_state: dict[str, Any] | None = None
            last_start_error: RuntimeError | None = None
            for start_attempt in range(1, MOTOR3_MEASUREMENT_START_MAX_ATTEMPTS + 1):
                try:
                    self._set_wicklers_leader_speed(speed_mm_s)
                    self._esp(f"MOTOR 3 MOVE_ABS_MM={target_mm:.3f}", read_timeout_s=5.0)
                    motor_state = self._wait_motor3_idle(
                        timeout_s=max(45.0, expected_move_s + 20.0),
                        min_wait_s=max(0.0, expected_move_s + 0.5),
                        monitor_wicklers=True,
                    )
                    break
                except RuntimeError as exc:
                    last_start_error = exc
                    if "accepted but did not start/reach target" not in str(exc):
                        raise
                    if start_attempt >= MOTOR3_MEASUREMENT_START_MAX_ATTEMPTS:
                        raise
                    self.logs.log(
                        "raspi",
                        "warning",
                        "Motor 3 scale calibration move did not start, retrying once",
                    )
                    for command in ("MOTOR 3 RESET_ALARM", "MOTOR 3 RECOVER_ETO"):
                        self._esp(command, read_timeout_s=5.0)
                    time.sleep(0.5)
                finally:
                    try:
                        self._set_wicklers_leader_speed(0.0)
                    except Exception as clear_exc:
                        self.logs.log("raspi", "warning", f"Motor 3 scale calibration leader clear failed: {repr(clear_exc)}")
            if motor_state is None:
                raise last_start_error or RuntimeError("Motor 3 scale calibration move failed")
            self._abort_motor3_if_wickler_faulted()

            encoder_mm = self._infeed_encoder_mm()
            if encoder_mm <= 0.0:
                raise RuntimeError(
                    "Einlaufencoder zaehlt bei Vorwaertsfahrt nicht positiv; "
                    f"Motor-3-Skalierung abgebrochen: encoder_mm={encoder_mm:.3f}"
                )
            error_mm = encoder_mm - target_mm
            pass_result = {
                "attempt": attempt,
                "target_mm": target_mm,
                "encoder_mm": encoder_mm,
                "error_mm": error_mm,
                "steps_per_mm": steps_per_mm,
                "motor_feedback_tenths_mm": motor_state.get("feedback_tenths_mm"),
                "motor_target_tenths_mm": motor_state.get("target_tenths_mm"),
            }
            attempts.append(pass_result)
            self.logs.log(
                "raspi",
                "info" if abs(error_mm) <= MOTOR3_SCALE_CALIBRATION_TOLERANCE_MM else "warning",
                f"Motor 3 scale calibration pass {attempt}: encoder={encoder_mm:.3f}mm "
                f"target={target_mm:.3f}mm error={error_mm:.3f}mm",
            )

            if abs(error_mm) <= MOTOR3_SCALE_CALIBRATION_TOLERANCE_MM:
                return {
                    "ok": True,
                    "target_mm": target_mm,
                    "tolerance_mm": MOTOR3_SCALE_CALIBRATION_TOLERANCE_MM,
                    "steps_per_mm": steps_per_mm,
                    "correction_applied": correction_applied,
                    "attempts": attempts,
                }

            factor = target_mm / encoder_mm
            if abs(factor - 1.0) > MOTOR3_SCALE_CALIBRATION_MAX_CORRECTION_FACTOR:
                raise RuntimeError(
                    f"Motor 3 scale correction implausible: factor={factor:.6f}, "
                    f"encoder={encoder_mm:.3f}mm target={target_mm:.3f}mm"
                )
            new_steps_per_mm = steps_per_mm * factor
            pass_result["new_steps_per_mm"] = new_steps_per_mm
            self.logs.log(
                "raspi",
                "warning",
                f"Motor 3 scale correction: steps_per_mm {steps_per_mm:.6f} -> "
                f"{new_steps_per_mm:.6f} (encoder/target={encoder_mm:.3f}/{target_mm:.3f})",
            )
            persisted_state = self._persist_motor3_steps_per_mm(new_steps_per_mm)
            persisted_steps = self._motor3_steps_per_mm(persisted_state)
            pass_result["persisted_steps_per_mm"] = persisted_steps
            correction_applied = True

        last = attempts[-1] if attempts else {}
        raise RuntimeError(
            "Motor 3 scale calibration failed after "
            f"{MOTOR3_SCALE_CALIBRATION_MAX_ATTEMPTS} attempts: last={last}"
        )

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

    def _set_fault_value(self, pkey: str, value: str, *, origin: str) -> tuple[bool, str]:
        try:
            previous_value = self.params.get_effective_value(pkey)
        except Exception:
            previous_value = None
        previous_active = self._fault_value_active(previous_value)
        next_active = self._fault_value_active(value)
        ok, msg = self.params.apply_device_value(pkey, value, promote_default=True)
        if not ok:
            return ok, msg
        if previous_active == next_active:
            return ok, msg
        line = f"{pkey}={value}"
        if not ValueDedupeStore(self.params.db).should_send("microtom", pkey, value):
            return ok, msg
        self.logs.log("raspi", "out", f"to microtom: {line}")
        if next_active:
            self.logs.log("machine", "warning", f"Stoerung gesetzt: {line}")
        targets = peer_urls(self.cfg, "/api/inbox")
        if targets:
            outbox = Outbox(self.params.db)
            for url in targets:
                outbox.enqueue(
                    "POST",
                    url,
                    {},
                    {"msg": line, "source": "raspi", "origin": origin},
                    None,
                    priority=20,
                    dedupe_key=f"state:{pkey}:{'active' if next_active else 'clear'}",
                    drop_if_duplicate=True,
                    replace_existing=not next_active,
                )
        return ok, msg

    def _fault_value_active(self, value: object) -> bool:
        return str(value or "").strip().lower() not in ("", "0", "false", "off", "no", "none", "null")

    def _set_motor3_postposition_error(self, active: bool) -> None:
        ok, msg = self._set_fault_value(MOTOR3_POSTPOSITION_ERROR_PKEY, "1" if active else "0", origin="setup")
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
        master_payload = {"indexedModeEnabled": "0", **self._wickler_master_threshold_payload()}
        for client in self._winder_clients():
            try:
                client.post_master(master_payload, timeout_s=5.0)
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
            move_timeout = max(5.0, abs(correction_mm) / self._setup_learn_speed_mm_s() + 4.0)
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

    def _expected_motor3_move_seconds(self, distance_mm: float, speed_mm_s: float) -> float:
        abs_distance = abs(float(distance_mm))
        abs_speed = max(1.0, abs(float(speed_mm_s)))
        ramp = max(1.0, self.defaults.learn_ramp_mm_s2)
        ramp_distance = (abs_speed * abs_speed) / ramp
        if abs_distance > ramp_distance:
            return (abs_distance / abs_speed) + (abs_speed / ramp)
        return 2.0 * ((abs_distance / ramp) ** 0.5)

    def _run_motor3_measurement_move(self, distance_mm: float, speed_mm_s: float) -> dict[str, Any]:
        expected_move_s = self._expected_motor3_move_seconds(distance_mm, speed_mm_s)
        last_error: RuntimeError | None = None
        for attempt in range(1, MOTOR3_MEASUREMENT_START_MAX_ATTEMPTS + 1):
            self._abort_if_not_setup_active()
            for client in self._winder_clients():
                client.start_diameter_learning(timeout_s=5.0)
            # Diameter learning is an explicit setup move, not a productive takt.
            # MOVE_REL_MM_OP uses the ESP's setup-only Operation-Data start path and
            # avoids the Motor-3 hardware START edge reserved for takt operation.
            try:
                self._set_wicklers_leader_speed(speed_mm_s)
                self._esp(f"MOTOR 3 MOVE_REL_MM_OP={distance_mm:.3f}", read_timeout_s=5.0)
                return self._wait_motor3_idle(
                    timeout_s=max(30.0, expected_move_s + 20.0),
                    min_wait_s=max(0.0, expected_move_s + 0.5),
                    monitor_wicklers=True,
                )
            except RuntimeError as exc:
                last_error = exc
                message = str(exc)
                if "accepted but did not start/reach target" not in message:
                    raise
                if attempt >= MOTOR3_MEASUREMENT_START_MAX_ATTEMPTS:
                    raise
                self.logs.log(
                    "raspi",
                    "warning",
                    f"Motor 3 measurement move did not start, retrying once: distance={distance_mm:.3f}mm",
                )
                for command in ("MOTOR 3 RESET_ALARM", "MOTOR 3 RECOVER_ETO"):
                    self._esp(command, read_timeout_s=5.0)
                time.sleep(0.5)
            finally:
                try:
                    self._set_wicklers_leader_speed(0.0)
                except Exception as clear_exc:
                    self.logs.log("raspi", "warning", f"Motor 3 measurement leader clear failed: {repr(clear_exc)}")
        raise last_error or RuntimeError("Motor 3 measurement move failed")

    def _winder_clients(self) -> list[SmartWicklerClient]:
        return [SmartWicklerClient(self.cfg, "unwinder"), SmartWicklerClient(self.cfg, "rewinder")]

    @staticmethod
    def _wickler_role(client: Any, fallback: str = "wickler") -> str:
        descriptor = getattr(client, "descriptor", None)
        return str(getattr(descriptor, "role", "") or fallback)

    @staticmethod
    def _wickler_label(client: Any, fallback: str = "Wickler") -> str:
        descriptor = getattr(client, "descriptor", None)
        return str(getattr(descriptor, "label", "") or fallback)

    def _fetch_wickler_state_safety(self, client: SmartWicklerClient) -> dict[str, Any]:
        try:
            return client.fetch_state(timeout_s=WICKLER_STATE_SAFETY_TIMEOUT_S)
        except TypeError:
            return client.fetch_state()

    def _sync_wickler_setup_master(self) -> None:
        desired_map0047 = bool(self._param_int("MAP0047", 0))
        desired_map0047_wire = "1" if desired_map0047 else "0"
        threshold_payload = self._wickler_master_threshold_payload()
        for client in self._winder_clients():
            role = self._wickler_role(client)
            payload = {
                "indexedModeEnabled": "0",
                **threshold_payload,
                "map0047": desired_map0047_wire,
            }
            self.logs.log(
                "raspi",
                "info",
                "setup wickler "
                f"{role} master sync: indexedModeEnabled=0 "
                f"map0023={payload['map0023']} map0024={payload['map0024']} "
                f"map0025={payload['map0025']} map0047={desired_map0047_wire}",
            )
            try:
                client.post_master(payload, timeout_s=8.0)
                state = self._fetch_wickler_state_safety(client)
            except Exception as exc:
                raise RuntimeError(f"Wickler {role} master sync failed: {exc}") from exc

            master = state.get("master") if isinstance(state.get("master"), dict) else {}
            if "map0047" not in master:
                raise RuntimeError(f"Wickler {role} master sync failed: map0047 missing in /api/state")
            observed = self._truthy_value(master.get("map0047"))
            if observed != desired_map0047:
                raise RuntimeError(
                    f"Wickler {role} master sync failed: map0047 is {int(observed)}, "
                    f"expected {desired_map0047_wire}"
                )
            threshold_mismatches = self._wickler_master_threshold_mismatches(master, threshold_payload)
            if threshold_mismatches:
                raise RuntimeError(
                    f"Wickler {role} master sync failed: " + "; ".join(threshold_mismatches)
                )

    def _wickler_master_threshold_payload(self) -> dict[str, str]:
        map0023 = max(0, min(100, self._param_int("MAP0023", 5)))
        map0024 = max(0, min(100, self._param_int("MAP0024", 95)))
        map0025_tenths_percent = max(0.0, min(50.0, self._param_float("MAP0025", 10.0)))
        map0025 = map0025_tenths_percent / 10.0
        return {
            "map0023": str(map0023),
            "map0024": str(map0024),
            "map0025": f"{map0025:.1f}",
        }

    @staticmethod
    def _wickler_master_threshold_mismatches(
        master: dict[str, Any],
        expected: dict[str, str],
    ) -> list[str]:
        mismatches: list[str] = []
        for key, expected_value in expected.items():
            if key not in master:
                mismatches.append(f"{key} missing")
                continue
            actual = master.get(key)
            if key in {"map0023", "map0024"}:
                try:
                    if int(float(str(actual))) == int(float(expected_value)):
                        continue
                except Exception:
                    pass
            else:
                try:
                    if abs(float(str(actual)) - float(expected_value)) <= 0.05:
                        continue
                except Exception:
                    pass
            mismatches.append(f"{key} is {actual}, expected {expected_value}")
        return mismatches

    @staticmethod
    def _truthy_value(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        return str(value).strip().lower() in {"1", "true", "yes", "on", "aktiv", "active"}

    @classmethod
    def _wickler_calibrated(cls, telemetry: dict[str, Any]) -> bool:
        if cls._truthy_value(telemetry.get("requiresCalibration")):
            return False
        if "calibrated" in telemetry:
            return cls._truthy_value(telemetry.get("calibrated"))
        return True

    @staticmethod
    def _wickler_mode_key(value: Any) -> str:
        return str(value or "").strip().lower().replace("\u00f6", "oe")

    def _wickler_state_faults(
        self,
        client: SmartWicklerClient,
        state: dict[str, Any],
        *,
        abort_not_calibrated: bool,
        abort_external_stop: bool,
        abort_dancer_range: bool,
    ) -> list[str]:
        role = self._wickler_role(client)
        label = self._wickler_label(client)
        telemetry = state.get("telemetry") or {}
        drive = state.get("drive") or {}
        values = state.get("values") or {}
        mode = str(telemetry.get("modeLabel") or "")
        mode_css = str(telemetry.get("modeCss") or "")
        mode_key = self._wickler_mode_key(mode)
        mode_css_key = self._wickler_mode_key(mode_css)
        reason = str(telemetry.get("faultReason") or "").strip()
        device = state.get("device") or {}
        try:
            wipe_percent = float(telemetry.get("wipePercent"))
        except Exception:
            wipe_percent = 50.0

        role_prefix = f"{label} ({role})"
        faults: list[str] = []
        offline = (
            mode_key == "offline"
            or (state.get("ok") is False and not bool(device.get("reachable", True)))
            or (bool(device) and not bool(device.get("reachable", True)))
        )
        if offline:
            count = self._wickler_offline_fault_counts.get(role, 0) + 1
            self._wickler_offline_fault_counts[role] = count
            if count >= 3:
                faults.append(f"{role_prefix} offline: {reason or device.get('error') or 'Endpoint nicht erreichbar'}")
            return faults

        self._wickler_offline_fault_counts[role] = 0
        if abort_not_calibrated and not self._wickler_calibrated(telemetry):
            faults.append(f"{role_prefix} Wippe nicht eingemessen")
        if abort_dancer_range:
            if wipe_percent <= WICKLER_MOTION_ABORT_LOW_PERCENT:
                faults.append(f"{role_prefix} Wippe im unteren Sicherheitsbereich ({wipe_percent:.1f}%)")
            elif wipe_percent >= WICKLER_MOTION_ABORT_HIGH_PERCENT:
                faults.append(f"{role_prefix} Wippe im oberen Sicherheitsbereich ({wipe_percent:.1f}%)")
        if (
            mode_css_key == "fault"
            or mode_key in {"stoerung", "fault", "fehler"}
            or str(values.get("statusMas") or "").strip() == "4"
        ):
            faults.append(f"{role_prefix} rot: {reason or mode or mode_css or 'MAS-Status 4'}")
        if abort_external_stop and bool(telemetry.get("externalStopActive")):
            faults.append(f"{role_prefix} externer STOP aktiv")
        if drive.get("online") is False:
            faults.append(f"{role_prefix} AZD offline")
        if drive.get("lastCommandOk") is False:
            faults.append(f"{role_prefix} letzter AZD-Befehl fehlgeschlagen")
        if bool(drive.get("alarm")):
            faults.append(f"{role_prefix} AZD alarm {drive.get('alarmCode')}")
        for key, text in (
            ("maeLow", "Taenzerarm zu tief"),
            ("maeHigh", "Taenzerarm zu hoch"),
            ("maeBlocked", "Taenzerarm blockiert"),
        ):
            if bool(values.get(key)):
                faults.append(f"{role_prefix} {text}")
        for key, text in (
            ("faultTooLow", "Taenzerarm zu tief"),
            ("faultTooHigh", "Taenzerarm zu hoch"),
            ("faultBlocked", "Taenzerarm blockiert"),
        ):
            if bool(telemetry.get(key)):
                faults.append(f"{role_prefix} {text}")
        return faults

    def _wickler_faults_during_motor3_motion(self) -> list[str]:
        faults: list[str] = []
        for client in self._winder_clients():
            role = self._wickler_role(client)
            label = self._wickler_label(client)
            state = self._fetch_wickler_state_safety(client)
            telemetry = state.get("telemetry") or {}
            drive = state.get("drive") or {}
            values = state.get("values") or {}
            mode = str(telemetry.get("modeLabel") or "")
            mode_css = str(telemetry.get("modeCss") or "")
            reason = str(telemetry.get("faultReason") or "").strip()
            device = state.get("device") or {}
            try:
                wipe_percent = float(telemetry.get("wipePercent"))
            except Exception:
                wipe_percent = 50.0

            role_prefix = f"{label} ({role})"
            offline = (
                mode.lower() == "offline"
                or (state.get("ok") is False and not bool(device.get("reachable", True)))
                or (bool(device) and not bool(device.get("reachable", True)))
            )
            if offline:
                count = self._wickler_offline_fault_counts.get(role, 0) + 1
                self._wickler_offline_fault_counts[role] = count
                if count >= 3:
                    faults.append(f"{role_prefix} offline: {reason or device.get('error') or 'Endpoint nicht erreichbar'}")
                continue
            self._wickler_offline_fault_counts[role] = 0
            if not self._wickler_calibrated(telemetry):
                faults.append(f"{role_prefix} Wippe nicht eingemessen")
            if wipe_percent <= WICKLER_MOTION_ABORT_LOW_PERCENT:
                faults.append(f"{role_prefix} Wippe im unteren Sicherheitsbereich ({wipe_percent:.1f}%)")
            elif wipe_percent >= WICKLER_MOTION_ABORT_HIGH_PERCENT:
                faults.append(f"{role_prefix} Wippe im oberen Sicherheitsbereich ({wipe_percent:.1f}%)")
            if mode_css.lower() == "fault" or mode.lower() in {"stoerung", "störung", "fault", "fehler"}:
                faults.append(f"{role_prefix} rot: {reason or mode or mode_css}")
            if bool(telemetry.get("externalStopActive")):
                faults.append(f"{role_prefix} externer STOP aktiv")
            if drive.get("online") is False:
                faults.append(f"{role_prefix} AZD offline")
            if drive.get("lastCommandOk") is False:
                faults.append(f"{role_prefix} letzter AZD-Befehl fehlgeschlagen")
            if bool(drive.get("alarm")):
                faults.append(f"{role_prefix} AZD alarm {drive.get('alarmCode')}")
            for key, text in (
                ("maeLow", "Taenzerarm zu tief"),
                ("maeHigh", "Taenzerarm zu hoch"),
                ("maeBlocked", "Taenzerarm blockiert"),
            ):
                if bool(values.get(key)):
                    faults.append(f"{role_prefix} {text}")
            for key, text in (
                ("faultTooLow", "Taenzerarm zu tief"),
                ("faultTooHigh", "Taenzerarm zu hoch"),
                ("faultBlocked", "Taenzerarm blockiert"),
            ):
                if bool(telemetry.get(key)):
                    faults.append(f"{role_prefix} {text}")
        return faults

    def _abort_motor3_if_wickler_faulted(self) -> None:
        faults = self._wickler_faults_during_motor3_motion()
        if not faults:
            return
        reason = "Wickler fault during Motor 3 movement: " + "; ".join(faults)
        self.logs.log("machine", "error", reason)
        self.stop_all_motion()
        self.params.apply_device_value("MAS0028", "1", promote_default=True)
        raise RuntimeError(reason)

    def _run_wickler_commands_parallel(self, action: str, timeout_s: float = 5.0) -> list[dict[str, Any]]:
        if action != "stop":
            self._abort_if_not_setup_active()
        clients = self._winder_clients()
        results: list[dict[str, Any]] = []

        def _post_mode_with_retry(client: SmartWicklerClient) -> dict[str, Any]:
            role = self._wickler_role(client)
            attempts = 1 if action == "stop" else 3
            last_exc: Exception | None = None
            for attempt in range(1, attempts + 1):
                if action != "stop":
                    self._abort_if_not_setup_active()
                try:
                    self.logs.log(
                        "raspi",
                        "info",
                        f"setup wickler {role} command {action} attempt {attempt}/{attempts}",
                    )
                    return client.post_mode(action, timeout_s=timeout_s)
                except Exception as exc:
                    last_exc = exc
                    if attempt >= attempts:
                        break
                    self.logs.log(
                        "raspi",
                        "warning",
                        f"setup wickler {role} {action} retry {attempt}/{attempts}: {repr(exc)}",
                    )
                    time.sleep(0.35 * attempt)
            raise last_exc or RuntimeError(f"{role} {action} failed")

        with ThreadPoolExecutor(max_workers=len(clients)) as executor:
            futures = {
                executor.submit(_post_mode_with_retry, client): self._wickler_role(client)
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

    def _wait_single_wickler_ready(
        self,
        client: SmartWicklerClient,
        timeout_s: float = 90.0,
        *,
        require_motion_ready: bool = False,
    ) -> dict[str, Any]:
        deadline = time.time() + float(timeout_s)
        role = self._wickler_role(client)
        last_state: dict[str, Any] = {}
        while time.time() < deadline:
            self._abort_if_not_setup_active()
            state = self._fetch_wickler_state_safety(client)
            last_state = state
            telemetry = state.get("telemetry") or {}
            drive = state.get("drive") or {}
            mode = str(telemetry.get("modeLabel") or "")
            faults = self._wickler_state_faults(
                client,
                state,
                abort_not_calibrated=False,
                abort_external_stop=require_motion_ready,
                abort_dancer_range=False,
            )
            if faults:
                raise RuntimeError("Wickler-Bereitschaft blockiert: " + "; ".join(faults))
            calibrated = self._wickler_calibrated(telemetry)
            try:
                wipe_percent = float(telemetry.get("wipePercent"))
            except Exception:
                wipe_percent = 50.0
            if mode in {"Bereit", "Warnung"} and calibrated and not bool(drive.get("alarm")):
                if (
                    wipe_percent <= WICKLER_MOTION_ABORT_LOW_PERCENT
                    or wipe_percent >= WICKLER_MOTION_ABORT_HIGH_PERCENT
                ):
                    raise RuntimeError(
                        f"Wickler {self._wickler_label(client)} ready outside safe dancer range: "
                        f"{wipe_percent:.1f}%"
                    )
                if require_motion_ready and (
                    bool(telemetry.get("externalStopActive"))
                    or not bool(drive.get("continuousModeReady"))
                    or not bool(drive.get("lastCommandOk", True))
                ):
                    time.sleep(0.5)
                    continue
                return state
            time.sleep(0.5)
        suffix = "motion-ready" if require_motion_ready else "ready"
        raise RuntimeError(
            f"Wickler {role} did not become {suffix} after calibration: "
            + json.dumps(last_state, ensure_ascii=False, sort_keys=True)
        )

    def _release_single_wickler_for_continuous_measurement(
        self,
        client: SmartWicklerClient,
        timeout_s: float = 8.0,
    ) -> dict[str, Any]:
        self._abort_if_not_setup_active()
        role = self._wickler_role(client)
        last_exc: Exception | None = None
        for attempt in range(1, 4):
            self._abort_if_not_setup_active()
            try:
                self.logs.log(
                    "raspi",
                    "info",
                    f"setup wickler {role} command ready allowMotion=1 after calibration attempt {attempt}/3",
                )
                reply = client.release_for_continuous_motion(timeout_s=timeout_s)
                ready_state = self._wait_single_wickler_ready(
                    client,
                    timeout_s=20.0,
                    require_motion_ready=True,
                )
                telemetry = ready_state.get("telemetry") or {}
                drive = ready_state.get("drive") or {}
                return {
                    "role": role,
                    "action": "ready_motion",
                    "ok": bool(reply.get("ok", True)),
                    "reply": reply,
                    "wipe_percent": telemetry.get("wipePercent"),
                    "continuous_mode_ready": drive.get("continuousModeReady"),
                }
            except Exception as exc:
                last_exc = exc
                if attempt >= 3:
                    break
                self.logs.log(
                    "raspi",
                    "warning",
                    f"setup wickler {role} ready after calibration retry {attempt}/3: {repr(exc)}",
                )
                time.sleep(0.35 * attempt)
        raise last_exc or RuntimeError(f"{role} ready_motion after calibration failed")

    def _calibrate_wicklers_for_setup(self, timeout_s: float = 5.0) -> list[dict[str, Any]]:
        # The two dancer arms are mechanically coupled through the web. Real
        # tests showed that a sequential calibration can pull the already
        # centered opposite arm back to the lower stop. Start both calibration
        # routines together, wait until both are centered, then immediately
        # release both into the proven continuous hold mode.
        calibrate_results = self._run_wickler_commands_parallel("calibrate", timeout_s=timeout_s)
        self._wait_wicklers_ready(timeout_s=90.0)
        release_results = self._release_wicklers_for_continuous_measurement(timeout_s=timeout_s)
        self._wait_wicklers_ready(timeout_s=20.0, require_motion_ready=True)

        release_by_role = {str(item.get("role") or ""): item for item in release_results}
        results: list[dict[str, Any]] = []
        for item in calibrate_results:
            role = str(item.get("role") or "")
            merged = dict(item)
            merged["release"] = release_by_role.get(role, {})
            results.append(merged)
        return results

    def _release_wicklers_for_continuous_measurement(self, timeout_s: float = 8.0) -> list[dict[str, Any]]:
        self._abort_if_not_setup_active()
        clients = self._winder_clients()
        results: list[dict[str, Any]] = []

        def _release_with_retry(client: SmartWicklerClient) -> dict[str, Any]:
            role = self._wickler_role(client)
            last_exc: Exception | None = None
            for attempt in range(1, 4):
                self._abort_if_not_setup_active()
                try:
                    self.logs.log(
                        "raspi",
                        "info",
                        f"setup wickler {role} command ready allowMotion=1 attempt {attempt}/3",
                    )
                    return client.release_for_continuous_motion(timeout_s=timeout_s)
                except Exception as exc:
                    last_exc = exc
                    if attempt >= 3:
                        break
                    self.logs.log(
                        "raspi",
                        "warning",
                        f"setup wickler {role} ready_motion retry {attempt}/3: {repr(exc)}",
                    )
                    time.sleep(0.35 * attempt)
            raise last_exc or RuntimeError(f"{role} ready_motion failed")

        with ThreadPoolExecutor(max_workers=len(clients)) as executor:
            futures = {
                executor.submit(_release_with_retry, client): self._wickler_role(client)
                for client in clients
            }
            for future in as_completed(futures):
                role = futures[future]
                try:
                    reply = future.result()
                    results.append({"role": role, "action": "ready_motion", "ok": bool(reply.get("ok", True)), "reply": reply})
                except Exception as exc:
                    results.append({"role": role, "action": "ready_motion", "ok": False, "error": str(exc)})
        errors = [f"{item['role']} ready_motion: {item.get('error') or item.get('reply')}" for item in results if not item.get("ok")]
        if errors:
            raise RuntimeError("; ".join(errors))
        return results

    def _set_wicklers_leader_speed(self, speed_mm_s: float, timeout_s: float = 2.0) -> list[dict[str, Any]]:
        requested = float(speed_mm_s)
        if abs(requested) > 0.001:
            self._abort_if_not_setup_active()
            self.logs.log(
                "raspi",
                "info",
                "Wickler leaderSpeedMmS not sent: continuous Wickler control remains autonomous/dancer-based",
            )
        clients = self._winder_clients()
        results: list[dict[str, Any]] = []

        def _role(client: SmartWicklerClient, index: int) -> str:
            descriptor = getattr(client, "descriptor", None)
            return str(getattr(descriptor, "role", "") or f"wickler{index + 1}")

        def _noop(client: SmartWicklerClient) -> dict[str, Any]:
            available = getattr(client, "available", None)
            if callable(available) and not available():
                return {"ok": True, "skipped": "endpoint unavailable or simulated"}
            return {
                "ok": True,
                "autonomous": True,
                "note": "leaderSpeedMmS intentionally not written in continuous mode",
            }

        with ThreadPoolExecutor(max_workers=len(clients)) as executor:
            futures = {executor.submit(_noop, client): _role(client, index) for index, client in enumerate(clients)}
            for future in as_completed(futures):
                role = futures[future]
                try:
                    reply = future.result()
                    results.append(
                        {
                            "role": role,
                            "requestedLeaderSpeedMmS": f"{requested:.3f}",
                            "autonomous": True,
                            "ok": bool(reply.get("ok", True)),
                            "reply": reply,
                        }
                    )
                except Exception as exc:
                    results.append(
                        {
                            "role": role,
                            "requestedLeaderSpeedMmS": f"{requested:.3f}",
                            "autonomous": True,
                            "ok": False,
                            "error": str(exc),
                        }
                    )
        errors = [
            f"{item['role']} leaderSpeedMmS request {item.get('requestedLeaderSpeedMmS')}: "
            f"{item.get('error') or item.get('reply')}"
            for item in results
            if not item.get("ok")
        ]
        if errors:
            raise RuntimeError("; ".join(errors))
        return results

    def _wait_wicklers_ready(self, timeout_s: float = 90.0, require_motion_ready: bool = False) -> None:
        deadline = time.time() + float(timeout_s)
        last_states: dict[str, dict[str, Any]] = {}
        while time.time() < deadline:
            self._abort_if_not_setup_active()
            all_ready = True
            for client in self._winder_clients():
                state = self._fetch_wickler_state_safety(client)
                telemetry = state.get("telemetry") or {}
                drive = state.get("drive") or {}
                values = state.get("values") or {}
                mode = str(telemetry.get("modeLabel") or "")
                faults = self._wickler_state_faults(
                    client,
                    state,
                    abort_not_calibrated=False,
                    abort_external_stop=require_motion_ready,
                    abort_dancer_range=False,
                )
                if faults:
                    raise RuntimeError("Wickler-Bereitschaft blockiert: " + "; ".join(faults))
                calibrated = self._wickler_calibrated(telemetry)
                mode_ready = mode in {"Bereit", "Warnung"} and calibrated and not bool(drive.get("alarm"))
                motion_ready = (
                    mode_ready
                    and not bool(telemetry.get("externalStopActive"))
                    and bool(drive.get("continuousModeReady"))
                    and bool(drive.get("lastCommandOk", True))
                )
                try:
                    wipe_percent = float(telemetry.get("wipePercent"))
                except Exception:
                    wipe_percent = 50.0
                if mode_ready and (
                    wipe_percent <= WICKLER_MOTION_ABORT_LOW_PERCENT
                    or wipe_percent >= WICKLER_MOTION_ABORT_HIGH_PERCENT
                ):
                    raise RuntimeError(
                        f"Wickler {self._wickler_label(client)} ready outside safe dancer range: "
                        f"{wipe_percent:.1f}%"
                    )
                ready = motion_ready if require_motion_ready else mode_ready
                last_states[self._wickler_role(client)] = {
                    "mode": mode,
                    "fault": telemetry.get("faultReason"),
                    "wipe_percent": telemetry.get("wipePercent"),
                    "calibrated": telemetry.get("calibrated"),
                    "requires_calibration": telemetry.get("requiresCalibration"),
                    "calibration_phase": telemetry.get("calibrationPhase"),
                    "external_stop": telemetry.get("externalStopActive"),
                    "status_mas": values.get("statusMas"),
                    "mae_low": values.get("maeLow"),
                    "drive_ready": drive.get("ready"),
                    "drive_move": drive.get("move"),
                    "drive_alarm": drive.get("alarm"),
                    "drive_alarm_code": drive.get("alarmCode"),
                    "continuous_mode_ready": drive.get("continuousModeReady"),
                    "last_command_ok": drive.get("lastCommandOk"),
                    "online": drive.get("online"),
                }
                all_ready = all_ready and ready
            if all_ready:
                return
            time.sleep(1.0)
        suffix = "motion-ready" if require_motion_ready else "ready"
        raise RuntimeError(f"Wicklers did not become {suffix}: " + json.dumps(last_states, ensure_ascii=False, sort_keys=True))

    def _param_int(self, key: str, default: int = 0) -> int:
        try:
            return int(float(self._param_text(key, default)))
        except Exception:
            return int(default)

    def _param_float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self._param_text(key, default))
        except Exception:
            return float(default)

    def _param_text(self, key: str, default: object = "") -> str:
        try:
            if self.params.get_meta(key) is None and self.params.get_value(key) is None:
                return str(default)
            value = self.params.get_effective_value(key)
        except Exception:
            value = default
        text = str(value if value is not None else "").strip()
        return text if text else str(default)

    def _sensor_teach_ms(self, speed_mm_s: float) -> int:
        return int(self.defaults.sensor_teach_min_ms)

    def _control_distance_mm(self) -> float:
        return max(50.0, self._param_int("MAP0021", 19300) / 10.0)

    def _slip_tolerance_mm(self) -> float:
        # MAP0040 is the label-length tolerance and is intentionally tighter
        # than the cumulative encoder comparison during the long teach run.
        # The setup run can accumulate several metres of travel while both
        # wicklers settle; keep a small engineering reserve here. The +/-0.05
        # mm print-stop check remains separate in the ESP and is not relaxed.
        return max(20.0, self._param_int("MAP0040", 5) / 10.0)

    def _set_infeed_sensor_teach(
        self,
        enabled: bool,
        *,
        attempts: int = INFEED_TEACH_MOXA_ATTEMPTS,
        verify: bool = True,
    ) -> None:
        pin_label = "DIO3"
        try:
            io_store = IoStore(self.params.db)
            point = io_store.get_point("moxa_e1213_3__DIO3")
            if point and bool(point.get("override_active")):
                result = IoRuntime(self.cfg, io_store).write_output(
                    point["io_key"],
                    bool(enabled),
                    force=True,
                    source="setup-sensor-teach",
                    best_effort=True,
                )
                self.logs.log(
                    "moxa",
                    "warning",
                    f"setup sensor teach: Moxa3 {pin_label} durch IO-Override gesperrt: {result}",
                )
                return
        except Exception as exc:
            self.logs.log("moxa", "warning", f"setup sensor teach override check failed: {repr(exc)}")
        if bool(getattr(self.cfg, "moxa3_simulation", True)):
            self.logs.log("moxa", "info", f"setup sensor teach simulated: Moxa3 {pin_label}={int(enabled)}")
            return
        host = str(getattr(self.cfg, "moxa3_host", "") or "").strip()
        port = int(getattr(self.cfg, "moxa3_port", 0) or 0)
        if not host or port <= 0:
            raise RuntimeError("Moxa3 endpoint missing for infeed sensor teach")
        timeout_s = max(
            INFEED_TEACH_MOXA_TIMEOUT_S,
            float(getattr(self.cfg, "moxa_timeout_s", 1.5) or 1.5),
        )
        requested = 1 if bool(enabled) else 0
        last_exc: Exception | None = None
        total_attempts = max(1, int(attempts or 1))
        for attempt in range(1, total_attempts + 1):
            client = MoxaE1213Client(host, port, timeout_s=timeout_s)
            try:
                client.write_output_label(pin_label, bool(enabled))
                observed = requested
                if verify:
                    observed = int(client.read_outputs([pin_label]).get(pin_label, -1))
                if observed != requested:
                    raise RuntimeError(f"Moxa3 {pin_label} verify mismatch: expected {requested}, observed {observed}")
                self.logs.log(
                    "moxa",
                    "info",
                    f"setup sensor teach: Moxa3 {pin_label}={requested} attempt={attempt}/{total_attempts}",
                )
                return
            except Exception as exc:
                last_exc = exc
                try:
                    client.close()
                except Exception:
                    pass
                if attempt < total_attempts:
                    self.logs.log(
                        "moxa",
                        "warning",
                        f"setup sensor teach retry {attempt}/{total_attempts}: "
                        f"Moxa3 {pin_label}={requested} failed: {repr(exc)}",
                    )
                    time.sleep(min(0.75, 0.15 * attempt))
                    continue
                break
            finally:
                try:
                    client.close()
                except Exception:
                    pass
        raise RuntimeError(
            f"Moxa3 {pin_label}={requested} failed after {total_attempts} attempts: {repr(last_exc)}"
        )

    def _start_infeed_teach_pulse(self, duration_ms: int) -> threading.Timer | None:
        self._set_infeed_sensor_teach(True)

        def _release():
            try:
                self._set_infeed_sensor_teach(False, attempts=INFEED_TEACH_MOXA_ATTEMPTS + 2)
            except Exception as exc:
                self.logs.log("moxa", "warning", f"setup sensor teach release failed: {repr(exc)}")

        timer = threading.Timer(max(0.1, float(duration_ms) / 1000.0), _release)
        timer.daemon = True
        timer.start()
        return timer

    def _teach_infeed_sensor_before_motion(self, duration_ms: int) -> None:
        self._abort_if_not_setup_active()
        self._set_infeed_sensor_teach(True)
        try:
            time.sleep(max(0.1, float(duration_ms) / 1000.0))
        finally:
            self._set_infeed_sensor_teach(False)
        self._abort_if_not_setup_active()

    def _setup_measure_status(self) -> dict[str, Any]:
        self._refresh_motor3_for_setup_status()
        return self._json_response(
            self._esp("PROCESS SETUP_MEASURE STATUS?", read_timeout_s=SETUP_MEASURE_STATUS_READ_TIMEOUT_S)
        )

    def _wait_setup_measurement_complete(self, timeout_s: float = 180.0) -> dict[str, Any]:
        deadline = time.time() + float(timeout_s)
        last_status: dict[str, Any] = {}
        last_status_ok_at = 0.0
        consecutive_status_errors = 0
        while time.time() < deadline:
            self._abort_if_not_setup_active()
            self._abort_motor3_if_wickler_faulted()
            try:
                status = self._setup_measure_status()
            except Exception as exc:
                consecutive_status_errors += 1
                now = time.time()
                last_status_running = bool(last_status.get("running")) and not bool(last_status.get("completed"))
                comm_gap_s = (now - last_status_ok_at) if last_status_ok_at > 0.0 else 999.0
                max_consecutive_errors = (
                    max(
                        SETUP_MEASURE_STATUS_MAX_CONSECUTIVE_ERRORS,
                        int(SETUP_MEASURE_STATUS_RUNNING_COMM_GRACE_S / max(0.1, SETUP_MEASURE_STATUS_POLL_S)),
                    )
                    if last_status_running
                    else SETUP_MEASURE_STATUS_MAX_CONSECUTIVE_ERRORS
                )
                grace_expired = (
                    not last_status_running
                    or comm_gap_s >= SETUP_MEASURE_STATUS_RUNNING_COMM_GRACE_S
                )
                if consecutive_status_errors >= max_consecutive_errors and grace_expired:
                    raise RuntimeError(
                        "ESP setup measurement status communication failed after "
                        f"{consecutive_status_errors} consecutive attempts: {exc!r}; "
                        f"comm_gap_s={comm_gap_s:.1f}; "
                        f"last_status={last_status}"
                    ) from exc
                self.logs.log(
                    "esp-plc",
                    "warning",
                    "setup measurement status read failed "
                    f"({consecutive_status_errors}/{max_consecutive_errors}, "
                    f"gap={comm_gap_s:.1f}s), "
                    f"retrying: {exc!r}",
                )
                time.sleep(SETUP_MEASURE_STATUS_POLL_S)
                continue
            consecutive_status_errors = 0
            last_status = status
            last_status_ok_at = time.time()
            if status.get("completed") and not status.get("running"):
                last_error = str(status.get("last_error") or "").strip()
                if last_error and last_error != "stopped":
                    raise RuntimeError(f"ESP setup measurement failed: {status}")
                return status
            if (not status.get("running")) and status.get("last_error"):
                raise RuntimeError(f"ESP setup measurement failed: {status}")
            time.sleep(SETUP_MEASURE_STATUS_POLL_S)
        raise RuntimeError(f"ESP setup measurement timed out: {last_status}")

    def _run_sensor_referenced_measurement(self, speed_mm_s: float) -> dict[str, Any]:
        teach_ms = self._sensor_teach_ms(speed_mm_s)
        control_teach_ms = teach_ms
        initial_forward_ms = int(self.defaults.sensor_settle_forward_ms)
        control_post_teach_ms = int(self.defaults.control_post_teach_ms)
        control_distance_mm = self._control_distance_mm()
        post_teach_travel_mm = abs(float(speed_mm_s)) * float(control_teach_ms + control_post_teach_ms) / 1000.0
        max_forward_mm = control_distance_mm + max(300.0, post_teach_travel_mm + 250.0)
        try:
            self._abort_if_not_setup_active()
            self._teach_infeed_sensor_before_motion(teach_ms)
            self._ensure_motor3_ready_for_setup_measurement("before_setup_measure_start")
            command = (
                "PROCESS SETUP_MEASURE START "
                f"SPEED_MM_S={speed_mm_s:.3f} "
                f"TEACH_MS={int(teach_ms)} "
                f"CONTROL_TEACH_MS={int(control_teach_ms)} "
                f"INFEED_SETTLE_MS={int(initial_forward_ms)} "
                f"CONTROL_POST_TEACH_MS={int(control_post_teach_ms)} "
                f"BACKOFF_MM={self.defaults.setup_backoff_mm:.3f} "
                f"CONTROL_PRETEACH_MM={self.defaults.control_preteach_mm:.3f} "
                f"CONTROL_DISTANCE_MM={control_distance_mm:.3f} "
                f"SLIP_TOL_MM={self._slip_tolerance_mm():.3f} "
                f"MAX_FORWARD_MM={max_forward_mm:.3f}"
            )
            self.logs.log("esp-plc", "info", f"setup sensor-referenced measurement command: {command}")
            self._abort_if_not_setup_active()
            self._esp(command, read_timeout_s=5.0)
            status = self._wait_setup_measurement_complete(
                timeout_s=max(120.0, (max_forward_mm * 3.0 / max(1.0, speed_mm_s)) + 60.0)
            )
            self.logs.log(
                "esp-plc",
                "info",
                "setup sensor-referenced measurement completed: "
                + json.dumps(status, ensure_ascii=False, sort_keys=True),
            )
            return status
        finally:
            try:
                self._set_infeed_sensor_teach(False, attempts=INFEED_TEACH_MOXA_ATTEMPTS + 2)
            except Exception as exc:
                self.logs.log("moxa", "warning", f"setup sensor teach final release failed: {repr(exc)}")

    def _start_diameter_learning_for_wicklers(self) -> list[dict[str, Any]]:
        self._abort_if_not_setup_active()
        self._wait_wicklers_ready(timeout_s=10.0, require_motion_ready=True)
        clients = self._winder_clients()
        results: list[dict[str, Any]] = []

        def _start_learning_with_retry(client: SmartWicklerClient) -> dict[str, Any]:
            role = self._wickler_role(client)
            last_exc: Exception | None = None
            for attempt in range(1, 4):
                self._abort_if_not_setup_active()
                try:
                    self.logs.log(
                        "raspi",
                        "info",
                        f"setup wickler {role} diameter learning start attempt {attempt}/3",
                    )
                    return client.start_diameter_learning(timeout_s=8.0)
                except Exception as exc:
                    last_exc = exc
                    if attempt >= 3:
                        break
                    self.logs.log(
                        "raspi",
                        "warning",
                        f"setup wickler {role} diameter start retry {attempt}/3: {repr(exc)}",
                    )
                    time.sleep(0.35 * attempt)
            raise last_exc or RuntimeError(f"{role} diameter start failed")

        with ThreadPoolExecutor(max_workers=len(clients)) as executor:
            futures = {
                executor.submit(_start_learning_with_retry, client): self._wickler_role(client)
                for client in clients
            }
            for future in as_completed(futures):
                role = futures[future]
                try:
                    reply = future.result()
                    results.append({"role": role, "ok": bool(reply.get("ok", True)), "reply": reply})
                except Exception as exc:
                    results.append({"role": role, "ok": False, "error": str(exc)})
        errors = [f"{item['role']}: {item.get('error') or item.get('reply')}" for item in results if not item.get("ok")]
        if errors:
            raise RuntimeError("Wickler diameter learning could not be started: " + "; ".join(errors))
        return results

    def _cancel_diameter_learning_for_wicklers(self) -> None:
        for client in self._winder_clients():
            try:
                client.cancel_diameter_learning(timeout_s=5.0)
            except Exception as exc:
                self.logs.log(
                    "raspi",
                    "warning",
                    f"setup diameter learning cancel ignored for {self._wickler_role(client)}: {repr(exc)}",
                )

    def _diameter_learning_travel_mm(self, measurement: dict[str, Any]) -> float:
        for key in ("diameter_learn_travel_mm", "absolute_travel_mm"):
            try:
                travel = abs(float(measurement.get(key) or 0.0))
            except Exception:
                travel = 0.0
            if travel >= 100.0:
                return travel
        raise RuntimeError(
            "ESP setup measurement did not report usable absolute travel for Wickler diameter learning; "
            "flash the ESP32-PLC firmware that exposes diameter_learn_travel_mm"
        )

    def _finish_and_apply_diameter_learning(self, travel_mm: float) -> dict[str, dict[str, Any]]:
        self._abort_if_not_setup_active()
        clients = self._winder_clients()
        results: dict[str, dict[str, Any]] = {}
        errors: list[str] = []

        def _with_diameter_retry(role: str, action: str, func):
            last_exc: Exception | None = None
            for attempt in range(1, WICKLER_DIAMETER_FINALIZE_ATTEMPTS + 1):
                self._abort_if_not_setup_active()
                try:
                    return func()
                except Exception as exc:
                    last_exc = exc
                    if attempt >= WICKLER_DIAMETER_FINALIZE_ATTEMPTS:
                        break
                    self.logs.log(
                        "raspi",
                        "warning",
                        f"setup wickler {role} diameter {action} retry "
                        f"{attempt}/{WICKLER_DIAMETER_FINALIZE_ATTEMPTS}: {repr(exc)}",
                    )
                    time.sleep(min(2.0, WICKLER_DIAMETER_FINALIZE_RETRY_BASE_S * attempt))
            raise last_exc or RuntimeError(f"{role} diameter {action} failed")

        def _finish_one(client: SmartWicklerClient) -> dict[str, Any]:
            role = self._wickler_role(client)
            # The ESP setup measurement is a forward/backward sequence. Use the
            # Wickler's accumulated motor-pulse window plus ESP absolute travel,
            # not net end position or net local travel.
            self.logs.log(
                "raspi",
                "info",
                f"setup wickler {role} diameter finish after {travel_mm:.1f}mm absolute travel",
            )
            payload = _with_diameter_retry(
                role,
                "finish",
                lambda: client.finish_diameter_learning(
                    travel_mm=travel_mm,
                    apply=False,
                    method="motor-accum",
                    timeout_s=8.0,
                ),
            )
            if not payload.get("ok"):
                raise RuntimeError(f"{role} finish returned {payload}")
            candidate = float(payload.get("candidateDiameterMm") or payload.get("diameterMm") or 0.0)
            if candidate <= 0.0:
                raise RuntimeError(f"{role} did not return a valid diameter candidate: {payload}")
            self.logs.log("raspi", "info", f"setup wickler {role} diameter apply {candidate:.1f}mm persist=1")
            apply_reply = _with_diameter_retry(
                role,
                "apply",
                lambda: client.set_diameter(candidate, persist=True, timeout_s=8.0),
            )
            if not apply_reply.get("ok", True):
                raise RuntimeError(f"{role} diameter apply returned {apply_reply}")
            return {
                "role": role,
                "candidate_diameter_mm": candidate,
                "travel_mm": travel_mm,
                "finish": payload,
                "apply": apply_reply,
            }

        with ThreadPoolExecutor(max_workers=max(1, len(clients))) as executor:
            futures = {executor.submit(_finish_one, client): self._wickler_role(client) for client in clients}
            for future in as_completed(futures):
                role = futures[future]
                try:
                    result = future.result()
                    results[role] = result
                    self.logs.log(
                        "raspi",
                        "info",
                        f"setup diameter applied {role}: {result['candidate_diameter_mm']:.1f}mm "
                        f"after {travel_mm:.1f}mm absolute travel",
                    )
                except Exception as exc:
                    errors.append(f"{role}: {exc}")
        if errors:
            raise RuntimeError("Wickler diameter learning failed: " + "; ".join(errors))
        return results

    def _run_sensor_referenced_measurement_with_diameter_learning(
        self,
        speed_mm_s: float,
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        self._start_diameter_learning_for_wicklers()
        try:
            measurement = self._run_sensor_referenced_measurement(speed_mm_s)
        except Exception:
            self._cancel_diameter_learning_for_wicklers()
            raise
        time.sleep(1.0)
        travel_mm = self._diameter_learning_travel_mm(measurement)
        diameter_learning = self._finish_and_apply_diameter_learning(travel_mm)
        return measurement, diameter_learning

    def run(self, wait_for_format_axes: Callable[[], Any] | None = None) -> dict[str, Any]:
        speed = self._setup_learn_speed_mm_s()
        ramp = self.defaults.learn_ramp_mm_s2
        format_axes_ok: bool | None = None
        motor_poll_setup: dict[str, Any] | None = None
        try:
            self._abort_if_not_setup_active()
            self._sync_setup_params_to_esp()
            motor_poll_setup = self._ensure_motor_auto_poll_disabled_for_setup()
            self._configure_motor3(speed, ramp)

            self._sync_wickler_setup_master()
            for action in ("stop", "resetAlarm", "etoRecovery"):
                self._run_wickler_commands_parallel(action, timeout_s=5.0)
            self._calibrate_wicklers_for_setup(timeout_s=5.0)
            self._abort_motor3_if_wickler_faulted()
            if wait_for_format_axes is not None:
                axis_result = wait_for_format_axes()
                if isinstance(axis_result, dict):
                    format_axes_ok = bool(axis_result.get("ok", True))
                else:
                    format_axes_ok = True
            self._prepare_motor3_for_measurement()
            self._abort_motor3_if_wickler_faulted()
            measurement, diameter_learning = self._run_sensor_referenced_measurement_with_diameter_learning(speed)
            applied = [
                f"labels:{int(measurement.get('labels_measured') or 0)}",
                f"reference:{float(measurement.get('reference_mm') or 0.0):.1f}mm",
                f"target:{float(measurement.get('target_mm') or 0.0):.1f}mm",
            ]
            for role, item in sorted(diameter_learning.items()):
                applied.append(f"diameter_{role}:{float(item.get('candidate_diameter_mm') or 0.0):.1f}mm")
            production_baseline = self._prepare_production_pause_baseline()
            applied.append("process_baseline:zeroed")
            final_wickler_ready = self._release_wicklers_for_continuous_measurement(timeout_s=5.0)
            self._wait_wicklers_ready(timeout_s=10.0, require_motion_ready=True)
            applied.append("wicklers:ready")
            self.logs.log("raspi", "info", "setup sensor measurement applied: " + ", ".join(applied))
            return {
                "ok": True,
                "applied": applied,
                "measurement": measurement,
                "diameter_learning": diameter_learning,
                "production_baseline": production_baseline,
                "final_wickler_ready": final_wickler_ready,
                "speed_mm_s": speed,
                "ramp_mm_s2": ramp,
                "teach_ms": self._sensor_teach_ms(speed),
                "format_axes_waited": wait_for_format_axes is not None,
                "format_axes_ok": format_axes_ok,
                "motor_poll_setup": motor_poll_setup,
            }
        except Exception:
            self.stop_all_motion()
            self.params.apply_device_value("MAS0028", "1", promote_default=True)
            raise

    def _learn_diameter_pass(self, distance_mm: float, speed_mm_s: float) -> dict[str, float]:
        abs_distance = abs(float(distance_mm))
        motor_state = self._run_motor3_measurement_move(distance_mm, speed_mm_s)
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

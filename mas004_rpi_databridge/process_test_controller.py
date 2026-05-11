from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from mas004_rpi_databridge.config import Settings
from mas004_rpi_databridge.device_clients import EspPlcClient
from mas004_rpi_databridge.logstore import LogStore
from mas004_rpi_databridge.params import ParamStore
from mas004_rpi_databridge.smart_wickler_client import SmartWicklerClient


@dataclass(frozen=True)
class ProcessCommandDefaults:
    label_length_mm: float = 100.0
    speed_mm_s: float = 100.0
    ramp_mm_s2: float = 300.0
    continuous_length_mm: float = 0.0
    indexed_cycles: int = 20
    indexed_stop_ms: int = 500
    learn_distance_mm: float = 1000.0
    learn_speed_mm_s: float = 100.0
    learn_ramp_mm_s2: float = 300.0


class TemporaryProcessCommandController:
    """
    Temporary IBN/test command surface for Microtom-simulator driven tests.

    MAC0001 is intentionally kept as a Raspi-owned command dispatcher. The
    ESP32-PLC still owns the fast process execution; the Raspi only sends the
    production-like start/configuration commands and service commands to the
    Wicklers.
    """

    def __init__(self, cfg: Settings, params: ParamStore, logs: LogStore):
        self.cfg = cfg
        self.params = params
        self.logs = logs
        self.defaults = ProcessCommandDefaults()
        self.esp = EspPlcClient(
            cfg.esp_host,
            cfg.esp_port,
            timeout_s=cfg.get_float("esp_connect_timeout_s", 1.5),
        )

    def execute(self, command_value: str) -> str:
        command = str(command_value or "").strip()
        try:
            if command == "0":
                self._stop_all()
                return "ACK_MAC0001=0"
            if command == "1":
                allowed, reason = self._wickler_learning_allowed()
                if not allowed:
                    self.logs.log(
                        "raspi",
                        "warning",
                        f"MAC0001=1 rejected: wickler learning requires Einrichten/setup ({reason})",
                    )
                    return f"MAC0001=NAK_{reason}"
                return self._calibrate_wicklers_and_learn()
            if command == "2":
                return self._start_indexed_production()
            if command == "3":
                return self._start_continuous(direction=1)
            if command == "4":
                return self._start_continuous(direction=-1)
        except Exception as exc:
            self.logs.log("raspi", "error", f"MAC0001 command {command} failed: {repr(exc)}")
            return f"MAC0001=NAK_DeviceComm"
        return "MAC0001=NAK_BadValue"

    def _read_float(self, pkey: str, default_value: float) -> float:
        try:
            return float(self.params.get_effective_value(pkey))
        except Exception:
            return float(default_value)

    def _read_int(self, pkey: str, default_value: int) -> int:
        try:
            return int(float(self.params.get_effective_value(pkey)))
        except Exception:
            return int(default_value)

    def _machine_state_snapshot(self) -> dict[str, Any]:
        try:
            with self.params.db._conn() as conn:
                row = conn.execute(
                    """SELECT current_state, requested_state, purge_active, info_json
                       FROM machine_state WHERE singleton_id=1"""
                ).fetchone()
        except Exception:
            row = None
        if not row:
            return {"current_state": 1, "requested_state": 1, "purge_active": False, "info": {}}
        try:
            info = json.loads(row[3] or "{}")
        except Exception:
            info = {}
        return {
            "current_state": int(row[0] or 1),
            "requested_state": int(row[1] or 1),
            "purge_active": bool(row[2]),
            "info": info if isinstance(info, dict) else {},
        }

    def _wickler_learning_allowed(self) -> tuple[bool, str]:
        """
        MAC0001=1 is only a temporary IBN helper. The production rule is that
        Wickler calibration plus diameter measuring run belongs to setup mode;
        reset/purge handling must never start it implicitly.
        """
        snapshot = self._machine_state_snapshot()
        current_state = int(snapshot.get("current_state") or 1)
        requested_state = int(snapshot.get("requested_state") or 1)
        requested_command = self._read_int("MAS0002", 0)
        purge_active = bool(snapshot.get("purge_active")) or self._read_int("MAS0028", 0) != 0
        if purge_active or current_state in (20, 21):
            return False, "PurgeActive"
        if requested_command == 3 or current_state in (2, 3) or requested_state in (2, 3):
            return True, "setup"
        return False, "SetupRequired"

    def _esp(self, line: str, read_timeout_s: float | None = None) -> str:
        response = self.esp.exchange_line(
            line,
            read_timeout_s=read_timeout_s
            or self.cfg.get_float("esp_command_timeout_s", 8.0),
        )
        self.logs.log("esp-plc", "info", f"MAC orchestration: {line} -> {response}")
        if response.upper().startswith("NAK"):
            raise RuntimeError(f"ESP rejected '{line}': {response}")
        return response

    def _configure_motor3(self, speed_mm_s: float, ramp_mm_s2: float) -> None:
        speed = max(1.0, abs(float(speed_mm_s)))
        ramp = max(1.0, abs(float(ramp_mm_s2)))
        self._esp(
            f"MOTOR 3 SET speed_mm_s={speed:.3f} accel_mm_s2={ramp:.3f} decel_mm_s2={ramp:.3f}"
        )

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

    def _wait_motor3_idle(self, timeout_s: float = 60.0, min_wait_s: float = 0.0) -> None:
        started = time.time()
        deadline = time.time() + float(timeout_s)
        seen_busy = False
        while time.time() < deadline:
            payload = self._json_response(self._esp("MOTOR 3 STATUS?", read_timeout_s=5.0))
            motor = payload.get("motor") or {}
            busy = bool(motor.get("busy")) or bool(motor.get("move"))
            feedback = motor.get("feedback_tenths_mm")
            target = motor.get("target_tenths_mm")
            try:
                position_close = abs(int(target) - int(feedback)) <= 2
            except Exception:
                position_close = True
            if busy:
                seen_busy = True
            elapsed = time.time() - started
            if motor and not busy and position_close and elapsed >= float(min_wait_s) and (
                seen_busy or elapsed > 2.0
            ):
                return
            time.sleep(0.25)
        raise RuntimeError("Motor 3 did not become idle")

    def _winder_clients(self) -> list[SmartWicklerClient]:
        return [SmartWicklerClient(self.cfg, "unwinder"), SmartWicklerClient(self.cfg, "rewinder")]

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

    def _stop_all(self) -> None:
        for line in (
            "PROCESS INDEXED STOP",
            "PROCESS PROFILE STOP",
            "PROCESS WICKLER CANCEL",
            "MOTOR 3 MOVE_VEL_MM_S=0",
        ):
            try:
                self._esp(line, read_timeout_s=5.0)
            except Exception as exc:
                self.logs.log("raspi", "info", f"MAC stop ignored error for {line}: {repr(exc)}")
        for client in self._winder_clients():
            try:
                client.post_master({"indexedModeEnabled": "0"}, timeout_s=5.0)
                client.post_mode("stop", timeout_s=5.0)
            except Exception as exc:
                self.logs.log("raspi", "info", f"MAC stop ignored wickler error: {repr(exc)}")

    def _calibrate_wicklers_and_learn(self) -> str:
        distance = self.defaults.learn_distance_mm
        speed = max(1.0, self._read_float("MAC0003", self.defaults.learn_speed_mm_s))
        ramp = max(1.0, self._read_float("MAC0004", self.defaults.learn_ramp_mm_s2))
        self._configure_motor3(speed, ramp)

        for client in self._winder_clients():
            client.post_master({"indexedModeEnabled": "0"}, timeout_s=5.0)
            client.post_mode("stop", timeout_s=5.0)
            client.post_mode("resetAlarm", timeout_s=5.0)
            client.post_mode("etoRecovery", timeout_s=5.0)
            client.post_mode("calibrate", timeout_s=5.0)
        self._wait_wicklers_ready()

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
            return "MAC0001=NAK_DeviceComm"
        self.logs.log("raspi", "info", "MAC0001=1 wickler learn applied: " + ", ".join(applied))
        return "ACK_MAC0001=1"

    def _learn_diameter_pass(self, distance_mm: float, speed_mm_s: float) -> dict[str, float]:
        for client in self._winder_clients():
            client.start_diameter_learning(timeout_s=5.0)
        # Diameter learning is a setup/IBN path, not the deterministic indexed
        # production trigger path. Use the AZD operation-start command here so
        # the full forward/reverse measuring distance is executed independently
        # of the Motor-3 hardware START hold time used later for takt tests.
        self._esp(f"MOTOR 3 MOVE_REL_MM_OP={distance_mm:.3f}", read_timeout_s=5.0)
        abs_distance = abs(float(distance_mm))
        abs_speed = max(1.0, abs(float(speed_mm_s)))
        ramp = max(1.0, self._read_float("MAC0004", self.defaults.learn_ramp_mm_s2))
        ramp_distance = (abs_speed * abs_speed) / ramp
        if abs_distance > ramp_distance:
            expected_move_s = (abs_distance / abs_speed) + (abs_speed / ramp)
        else:
            expected_move_s = 2.0 * ((abs_distance / ramp) ** 0.5)
        self._wait_motor3_idle(
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
                        f"MAC diameter candidate {role}: {candidate:.1f}mm after {abs_distance:.1f}mm pass",
                    )
        return candidates

    def _start_indexed_production(self) -> str:
        travel_mm = max(1.0, self._read_float("MAC0002", self.defaults.label_length_mm * 10.0) / 10.0)
        speed = max(1.0, self._read_float("MAC0003", self.defaults.speed_mm_s))
        ramp = max(1.0, self._read_float("MAC0004", self.defaults.ramp_mm_s2))
        cycles = max(0, self._read_int("MAC0006", self.defaults.indexed_cycles))

        self._configure_motor3(speed, ramp)
        for client in self._winder_clients():
            client.post_master(
                {
                    "indexedModeEnabled": "1",
                    "indexedTravelMm": f"{travel_mm:.3f}",
                    "indexedSpeedMmS": f"{abs(speed):.3f}",
                    "indexedAccelMmS2": f"{abs(ramp):.3f}",
                    "indexedDecelMmS2": f"{abs(ramp):.3f}",
                    "indexedStandbyPercent": "50",
                },
                timeout_s=5.0,
            )
        self._esp(
            f"PROCESS INDEXED START TRAVEL_MM={travel_mm:.3f} CYCLES={cycles} STOP_MS={self.defaults.indexed_stop_ms}",
            read_timeout_s=5.0,
        )
        return "ACK_MAC0001=2"

    def _start_continuous(self, direction: int) -> str:
        speed = max(1.0, self._read_float("MAC0003", self.defaults.speed_mm_s))
        ramp = max(1.0, self._read_float("MAC0004", self.defaults.ramp_mm_s2))
        length_mm = self._read_float("MAC0005", self.defaults.continuous_length_mm * 10.0) / 10.0
        signed_speed = abs(speed) * (1 if direction >= 0 else -1)
        self._configure_motor3(abs(speed), ramp)
        for client in self._winder_clients():
            client.post_master({"indexedModeEnabled": "0"}, timeout_s=5.0)
        if length_mm > 0.0:
            self._esp(f"MOTOR 3 MOVE_REL_MM={abs(length_mm) * (1 if direction >= 0 else -1):.3f}", read_timeout_s=5.0)
        else:
            self._esp(f"MOTOR 3 MOVE_VEL_MM_S={signed_speed:.3f}", read_timeout_s=5.0)
        return f"ACK_MAC0001={'3' if direction >= 0 else '4'}"

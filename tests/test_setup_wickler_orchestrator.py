from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from mas004_rpi_databridge.config import Settings
from mas004_rpi_databridge.db import DB, now_ts
from mas004_rpi_databridge.logstore import LogStore
from mas004_rpi_databridge.params import ParamStore
from mas004_rpi_databridge.setup_wickler_orchestrator import SetupWicklerOrchestrator


def _insert_param(db: DB, pkey: str, default_v: str):
    with db._conn() as conn:
        conn.execute(
            """INSERT INTO params(
                pkey,ptype,pid,min_v,max_v,default_v,unit,rw,esp_rw,dtype,name,format_relevant,
                message,possible_cause,effects,remedy,updated_ts
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                pkey,
                pkey[:3],
                pkey[3:],
                None,
                None,
                default_v,
                "",
                "R/W",
                "W",
                "int",
                pkey,
                "NO",
                None,
                None,
                None,
                None,
                now_ts(),
            ),
        )


def _insert_io_point(
    db: DB,
    device_code: str,
    device_label: str,
    pin_label: str,
    io_dir: str,
    value: str = "0",
):
    io_key = f"{device_code}__{pin_label.replace('.', '_')}"
    with db._conn() as conn:
        conn.execute(
            """INSERT INTO io_points(
                io_key, device_code, device_label, sheet_name, zone_label, pin_label, io_dir,
                channel_no, function_text, is_reserved, is_active, source_row, updated_ts
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                io_key,
                device_code,
                device_label,
                device_label,
                "",
                pin_label,
                io_dir,
                0,
                pin_label,
                0,
                1,
                1,
                now_ts(),
            ),
        )
        conn.execute(
            """INSERT INTO io_values(io_key, value, quality, source, updated_ts)
               VALUES(?,?,?,?,?)
               ON CONFLICT(io_key) DO UPDATE SET value=excluded.value, quality=excluded.quality,
                 source=excluded.source, updated_ts=excluded.updated_ts""",
            (io_key, value, "simulation", "test", now_ts()),
        )


def _set_machine_state(db: DB, current_state: int, requested_state: int, purge_active: bool = False):
    with db._conn() as conn:
        conn.execute(
            """INSERT INTO machine_state(
                   singleton_id,current_state,requested_state,state_source,warning_active,purge_active,
                   production_label,last_label_no,info_json,updated_ts
               ) VALUES(1,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(singleton_id) DO UPDATE SET
                   current_state=excluded.current_state,
                   requested_state=excluded.requested_state,
                   purge_active=excluded.purge_active,
                   updated_ts=excluded.updated_ts""",
            (
                current_state,
                requested_state,
                "test",
                0,
                1 if purge_active else 0,
                "",
                0,
                "{}",
                now_ts(),
            ),
        )


class SetupWicklerOrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "test.db"))
        for key, value in (
            ("MAS0001", "2"),
            ("MAS0002", "3"),
            ("MAS0028", "0"),
            ("MAE0048", "0"),
            ("MAP0036", "1"),
            ("MAP0037", "1"),
        ):
            _insert_param(self.db, key, value)
        self.params = ParamStore(self.db)
        self.logs = LogStore(self.db)
        self.controller = SetupWicklerOrchestrator(Settings(esp_simulation=True), self.params, self.logs)

    def tearDown(self):
        self.tmp.cleanup()

    def test_motor3_idle_rejects_ack_without_motion_or_target_progress(self):
        self.controller._esp = Mock(
            return_value=(
                'JSON {"ok":true,"motor":{"state":{"ready":false,"busy":false,"move":false,'
                '"hwto":false,"alarm":false,"feedback_tenths_mm":0,"command_tenths_mm":1,'
                '"target_tenths_mm":10000,"last_reply":"Move accepted"}}}'
            )
        )

        with patch(
            "mas004_rpi_databridge.setup_wickler_orchestrator.time.time",
            side_effect=[0.0, 0.0, 0.0, 3.1],
        ):
            with self.assertRaisesRegex(RuntimeError, "accepted but did not start"):
                self.controller._wait_motor3_idle(timeout_s=30.0, min_wait_s=0.0)

    def test_motor3_postposition_rejects_nested_status_position_outside_tolerance(self):
        self.controller._esp = Mock(return_value="ACK")
        self.controller._wait_motor3_idle = Mock(
            return_value={"feedback_tenths_mm": 1000, "target_tenths_mm": 2000, "in_pos": True}
        )
        self.controller._hold_wicklers_for_motor3_postpositioning = Mock()

        with self.assertRaisesRegex(RuntimeError, "failed stop tolerance"):
            self.controller._ensure_motor3_stop_tolerance(
                {
                    "feedback_tenths_mm": 1000,
                    "target_tenths_mm": 2000,
                    "in_pos": True,
                }
            )

        self.assertEqual("1", self.params.get_effective_value("MAE0048"))

    def test_motor3_postposition_corrects_inpos_outside_stop_tolerance(self):
        self.controller._esp = Mock(return_value="ACK")
        self.controller._wait_motor3_idle = Mock(
            return_value={"feedback_tenths_mm": 1000, "target_tenths_mm": 1000, "in_pos": True}
        )
        self.controller._hold_wicklers_for_motor3_postpositioning = Mock()

        state = self.controller._ensure_motor3_stop_tolerance(
            {
                "feedback_tenths_mm": 997,
                "target_tenths_mm": 1000,
                "in_pos": True,
            }
        )

        self.assertEqual(1000, state["feedback_tenths_mm"])
        self.assertEqual("0", self.params.get_effective_value("MAE0048"))
        self.assertIn("MOTOR 3 MOVE_REL_MM_OP=0.300", [call.args[0] for call in self.controller._esp.call_args_list])

    def test_motor3_postposition_uses_raw_steps_for_sub_tenth_mm_precision(self):
        self.controller._esp = Mock(return_value="ACK")
        self.controller._wait_motor3_idle = Mock(
            return_value={
                "feedback_tenths_mm": 10000,
                "target_tenths_mm": 10000,
                "feedback_steps": -5217764,
                "command_steps": -5217764,
                "config": {"steps_per_mm": 31.627315, "invert_direction": True},
                "in_pos": True,
            }
        )
        self.controller._hold_wicklers_for_motor3_postpositioning = Mock()

        state = self.controller._ensure_motor3_stop_tolerance(
            {
                "feedback_tenths_mm": 9999,
                "target_tenths_mm": 10000,
                "feedback_steps": -5217761,
                "command_steps": -5217764,
                "config": {"steps_per_mm": 31.627315, "invert_direction": True},
                "in_pos": True,
            }
        )

        self.assertEqual(-5217764, state["feedback_steps"])
        self.assertEqual("0", self.params.get_effective_value("MAE0048"))
        self.assertIn("MOTOR 3 MOVE_REL_STEPS=3", [call.args[0] for call in self.controller._esp.call_args_list])

    def test_motor3_postposition_uses_fixed_target_steps_not_shifted_command_steps(self):
        self.controller._esp = Mock(return_value="ACK")
        self.controller._wait_motor3_idle = Mock(
            return_value={
                "feedback_tenths_mm": 10000,
                "target_tenths_mm": 10000,
                "feedback_steps": -5217764,
                "command_steps": -5217767,
                "config": {
                    "steps_per_mm": 31.627315,
                    "invert_direction": True,
                    "zero_offset_steps": -5186137,
                },
                "in_pos": True,
            }
        )
        self.controller._hold_wicklers_for_motor3_postpositioning = Mock()

        state = self.controller._ensure_motor3_stop_tolerance(
            {
                "feedback_tenths_mm": 9999,
                "target_tenths_mm": 10000,
                "feedback_steps": -5217761,
                "command_steps": -5217764,
                "config": {
                    "steps_per_mm": 31.627315,
                    "invert_direction": True,
                    "zero_offset_steps": -5186137,
                },
                "in_pos": True,
            }
        )

        self.assertEqual(-5217764, state["feedback_steps"])
        self.assertEqual("0", self.params.get_effective_value("MAE0048"))
        self.assertIn("MOTOR 3 MOVE_REL_STEPS=3", [call.args[0] for call in self.controller._esp.call_args_list])

    def test_inactive_motor3_postposition_clear_is_not_sent_to_microtom(self):
        self.controller._set_motor3_postposition_error(False)

        with self.db._conn() as conn:
            rows = conn.execute(
                "SELECT message FROM logs WHERE message LIKE '%to microtom: MAE0048=0%'"
            ).fetchall()
        self.assertEqual([], rows)
        self.assertEqual("0", self.params.get_effective_value("MAE0048"))

    def test_motor3_measurement_preparation_captures_current_position_as_zero(self):
        self.controller._esp = Mock(
            side_effect=[
                'JSON {"ok":true,"motor":{"state":{"link_ok":true,"ready":true,"alarm":false,"hwto":false,"feedback_tenths_mm":1631838,"target_tenths_mm":1641838}}}',
                "ACK_SET_POSITION_MM",
                'JSON {"ok":true,"motor":{"state":{"link_ok":true,"ready":true,"alarm":false,"hwto":false,"feedback_tenths_mm":0,"target_tenths_mm":0}}}',
            ]
        )

        state = self.controller._prepare_motor3_for_measurement()

        self.assertEqual(0, state["feedback_tenths_mm"])
        self.assertEqual(0, state["target_tenths_mm"])
        calls = [call.args[0] for call in self.controller._esp.call_args_list]
        self.assertNotIn("MOTOR 3 RESET_ALARM", calls)
        self.assertNotIn("MOTOR 3 RECOVER_ETO", calls)
        self.assertIn("MOTOR 3 SET_POSITION_MM=0.000", calls)

    def test_motor3_measurement_preparation_rejects_hwto_before_move(self):
        def fake_esp(line: str, **_kwargs) -> str:
            if line == "MOTOR 3 REFRESH":
                return 'JSON {"ok":true,"motor":{"state":{"link_ok":true,"ready":false,"alarm":false,"hwto":true,"output_raw_hex":"43"}}}'
            return "ACK_" + line.replace(" ", "_")

        self.controller._esp = Mock(side_effect=fake_esp)

        with self.assertRaisesRegex(RuntimeError, "hwto"):
            self.controller._prepare_motor3_for_measurement()

        calls = [call.args[0] for call in self.controller._esp.call_args_list]
        self.assertIn("MOTOR 3 RESET_ALARM", calls)
        self.assertIn("MOTOR 3 RECOVER_ETO", calls)
        self.assertNotIn("MOTOR 3 SET_POSITION_MM=0.000", calls)
        self.assertFalse(any(call.startswith("MOTOR 3 MOVE_REL_MM_OP") for call in calls))

    def test_production_pause_baseline_retries_transient_esp_timeout(self):
        calls: list[str] = []

        def fake_esp(line: str, read_timeout_s: float | None = None, **_kwargs) -> str:
            calls.append(line)
            if line == "PROCESS PRODUCTION_RESET" and calls.count(line) == 1:
                raise TimeoutError("timed out")
            return "ACK"

        self.controller._esp = Mock(side_effect=fake_esp)
        self.controller._set_motor3_postposition_error = Mock()
        self.controller._set_fault_value = Mock(return_value=(True, "OK"))

        result = self.controller._prepare_production_pause_baseline()

        self.assertEqual(2, calls.count("PROCESS PRODUCTION_RESET"))
        self.assertIn("MOTOR 3 SET_POSITION_MM=0.000", calls)
        self.assertEqual("ACK", result["process_response"])
        self.assertEqual("ACK", result["motor3_zero_response"])

    def test_production_pause_baseline_accepts_motor3_zero_when_ack_times_out_but_state_is_zero(self):
        calls: list[str] = []

        def fake_esp(line: str, read_timeout_s: float | None = None, **_kwargs) -> str:
            calls.append(line)
            if line == "MOTOR 3 SET_POSITION_MM=0.000":
                raise TimeoutError("timed out")
            return "ACK"

        self.controller._esp = Mock(side_effect=fake_esp)
        self.controller._motor3_status = Mock(
            return_value={"feedback_tenths_mm": 0, "target_tenths_mm": 0, "move": False, "busy": False}
        )
        self.controller._set_motor3_postposition_error = Mock()
        self.controller._set_fault_value = Mock(return_value=(True, "OK"))

        result = self.controller._prepare_production_pause_baseline()

        self.assertEqual("ACK_INFERRED_MOTOR3_ZERO_AFTER_TIMEOUT", result["motor3_zero_response"])
        self.controller._motor3_status.assert_called_once()

    def test_diameter_learning_uses_operation_data_setup_move(self):
        class FakeWicklerClient:
            def start_diameter_learning(self, timeout_s: float | None = None):
                return {"ok": True}

        self.controller._winder_clients = Mock(return_value=[FakeWicklerClient(), FakeWicklerClient()])
        self.controller._esp = Mock(return_value="ACK")
        self.controller._wait_motor3_idle = Mock(
            return_value={"feedback_tenths_mm": 10000, "target_tenths_mm": 10000}
        )
        self.controller._ensure_motor3_stop_tolerance = Mock()

        with patch("mas004_rpi_databridge.setup_wickler_orchestrator.SmartWicklerClient") as client_cls:
            client_cls.return_value.finish_diameter_learning.return_value = {
                "ok": True,
                "candidateDiameterMm": 200.0,
            }
            with patch("mas004_rpi_databridge.setup_wickler_orchestrator.time.sleep"):
                result = self.controller._learn_diameter_pass(1000.0, 100.0)

        self.assertEqual({"unwinder": 200.0, "rewinder": 200.0}, result)
        calls = [call.args[0] for call in self.controller._esp.call_args_list]
        self.assertIn("MOTOR 3 MOVE_REL_MM_OP=1000.000", calls)
        self.assertFalse(any(call == "MOTOR 3 MOVE_REL_MM=1000.000" for call in calls))

    def test_sensor_referenced_measurement_applies_diameter_learning_from_absolute_travel(self):
        class FakeDescriptor:
            def __init__(self, role: str):
                self.role = role

        class FakeWicklerClient:
            def __init__(self, role: str, diameter: float):
                self.descriptor = FakeDescriptor(role)
                self.diameter = diameter
                self.started = False
                self.finished: list[tuple[float, bool, str]] = []
                self.applied: list[tuple[float, bool]] = []

            def start_diameter_learning(self, timeout_s: float | None = None):
                self.started = True
                return {"ok": True}

            def cancel_diameter_learning(self, timeout_s: float | None = None):
                return {"ok": True}

            def finish_diameter_learning(
                self,
                travel_mm: float,
                apply: bool = False,
                method: str = "position-local",
                timeout_s: float | None = None,
            ):
                self.finished.append((travel_mm, apply, method))
                return {"ok": True, "candidateDiameterMm": self.diameter}

            def set_diameter(self, diameter_mm: float, persist: bool = True, timeout_s: float | None = None):
                self.applied.append((diameter_mm, persist))
                return {"ok": True}

            def fetch_state(self):
                return {
                    "telemetry": {
                        "modeLabel": "Bereit",
                        "externalStopActive": False,
                        "wipePercent": 50.0,
                        "calibrationPhase": 1,
                    },
                    "drive": {
                        "alarm": False,
                        "continuousModeReady": True,
                        "lastCommandOk": True,
                    },
                    "values": {},
                }

        clients = [
            FakeWicklerClient("unwinder", 206.5),
            FakeWicklerClient("rewinder", 192.0),
        ]
        self.controller._winder_clients = Mock(return_value=clients)
        self.controller._run_sensor_referenced_measurement = Mock(
            return_value={
                "completed": True,
                "diameter_learn_travel_mm": 2345.6,
                "labels_measured": 4,
                "reference_mm": 123.4,
                "target_mm": 113.4,
            }
        )

        with patch("mas004_rpi_databridge.setup_wickler_orchestrator.time.sleep"):
            measurement, diameter_learning, sea_vision_setup = (
                self.controller._run_sensor_referenced_measurement_with_diameter_learning(100.0)
            )

        self.assertEqual(2345.6, measurement["diameter_learn_travel_mm"])
        self.assertEqual({"unwinder", "rewinder"}, set(diameter_learning))
        self.assertFalse(sea_vision_setup["required"])
        self.assertEqual("sea_vision_bypass_active", sea_vision_setup["machine_run"]["skipped"])
        for client in clients:
            self.assertTrue(client.started)
            self.assertEqual([(2345.6, False, "motor-accum")], client.finished)
            self.assertEqual([(client.diameter, True)], client.applied)

    def test_sea_vision_setup_ready_waits_for_machine_run_when_ocr_not_bypassed(self):
        self.params.apply_device_value("MAP0037", "0", promote_default=True)
        _set_machine_state(self.db, current_state=3, requested_state=3)
        _insert_io_point(self.db, "esp32_plc58", "ESP32 PLC 58", "Q2.3", "output", "0")
        _insert_io_point(self.db, "esp32_plc58", "ESP32 PLC 58", "I2.4", "input", "0")
        writes: list[tuple[str, bool, bool, str]] = []

        class FakeIoRuntime:
            def __init__(self, _cfg, store):
                self.store = store

            def write_output(self, io_key, enabled, *, force=False, source="runtime", **_kwargs):
                writes.append((io_key, bool(enabled), bool(force), source))
                self.store.upsert_value(io_key, "1" if enabled else "0", "live", source)
                return {"ok": True, "value": 1 if enabled else 0}

            def _refresh_device(self, device_code, points):
                self.store.upsert_value("esp32_plc58__I2_4", "1", "live", "test")
                return {"device_code": device_code, "reachable": True, "changed": 1, "point_count": len(points)}

        with patch("mas004_rpi_databridge.setup_wickler_orchestrator.IoRuntime", FakeIoRuntime):
            result = self.controller._prepare_sea_vision_after_setup_measurement()

        self.assertTrue(result["required"], result)
        self.assertTrue(result["ready"]["ok"], result)
        self.assertTrue(result["machine_run"]["ok"], result)
        self.assertEqual(
            [("esp32_plc58__Q2_3", True, True, "setup-sea-vision-ready")],
            writes,
        )
        with self.db._conn() as conn:
            row = conn.execute("SELECT info_json FROM machine_state WHERE singleton_id=1").fetchone()
        info = json.loads(row[0])
        self.assertTrue(info["setup"]["sea_vision_ready_after_measurement"])

    def test_diameter_apply_retries_transient_wickler_http_refusal(self):
        class FakeDescriptor:
            def __init__(self, role: str):
                self.role = role

        class FlakyWicklerClient:
            def __init__(self, role: str, diameter: float):
                self.descriptor = FakeDescriptor(role)
                self.diameter = diameter
                self.apply_attempts = 0

            def finish_diameter_learning(
                self,
                travel_mm: float,
                apply: bool = False,
                method: str = "position-local",
                timeout_s: float | None = None,
            ):
                return {"ok": True, "candidateDiameterMm": self.diameter}

            def set_diameter(self, diameter_mm: float, persist: bool = True, timeout_s: float | None = None):
                self.apply_attempts += 1
                if self.apply_attempts == 1:
                    raise RuntimeError("ConnectError: [Errno 111] Connection refused")
                return {"ok": True, "diameterMm": diameter_mm, "persist": persist}

        clients = [
            FlakyWicklerClient("unwinder", 207.5),
            FlakyWicklerClient("rewinder", 193.0),
        ]
        self.controller._winder_clients = Mock(return_value=clients)

        with patch("mas004_rpi_databridge.setup_wickler_orchestrator.time.sleep") as sleep:
            result = self.controller._finish_and_apply_diameter_learning(2500.0)

        self.assertEqual({"unwinder", "rewinder"}, set(result))
        self.assertEqual([2, 2], [client.apply_attempts for client in clients])
        self.assertTrue(sleep.called)

    def test_sensor_referenced_measurement_rejects_missing_diameter_travel(self):
        with self.assertRaisesRegex(RuntimeError, "diameter_learn_travel_mm"):
            self.controller._diameter_learning_travel_mm({"completed": True})

    def test_sensor_referenced_measurement_teaches_infeed_before_motor3_motion(self):
        events: list[tuple[str, object]] = []

        def fake_esp(line: str, read_timeout_s: float | None = None, **_kwargs) -> str:
            events.append(("esp", line))
            if line == "MOTOR 3 REFRESH":
                return 'JSON {"ok":true,"motor":{"state":{"link_ok":true,"ready":true,"alarm":false,"hwto":false,"busy":false,"move":false}}}'
            if line == "PROCESS SETUP_MEASURE STATUS?":
                return (
                    'JSON {"ok":true,"running":false,"completed":true,"phase":6,'
                    '"diameter_learn_travel_mm":1234.5,"labels_measured":2,'
                    '"reference_mm":80.0,"target_mm":30.0,"last_error":""}'
                )
            return "ACK"

        self.controller._esp = Mock(side_effect=fake_esp)
        self.controller._set_infeed_sensor_teach = Mock(
            side_effect=lambda enabled, **_kwargs: events.append(("teach", bool(enabled)))
        )
        self.controller._abort_motor3_if_wickler_faulted = Mock()

        with patch(
            "mas004_rpi_databridge.setup_wickler_orchestrator.time.sleep",
            side_effect=lambda seconds: events.append(("sleep", seconds)),
        ):
            result = self.controller._run_sensor_referenced_measurement(100.0)

        self.assertEqual(1234.5, result["diameter_learn_travel_mm"])
        self.assertEqual([("teach", True), ("sleep", 3.5), ("teach", False)], events[:3])
        start_commands = [value for kind, value in events if kind == "esp" and "PROCESS SETUP_MEASURE START" in str(value)]
        self.assertEqual(1, len(start_commands))
        command = str(start_commands[0])
        self.assertIn("TEACH_MS=3500", command)
        self.assertIn("CONTROL_TEACH_MS=3500", command)
        self.assertIn("INFEED_SETTLE_MS=5000", command)
        self.assertIn("CONTROL_POST_TEACH_MS=5000", command)
        self.assertIn("BACKOFF_MM=10.000", command)
        self.assertIn("SLIP_TOL_MM=20.000", command)

    def test_infeed_sensor_teach_retries_and_verifies_moxa_output(self):
        class FlakyMoxa:
            instances: list["FlakyMoxa"] = []

            def __init__(self, host: str, port: int, timeout_s: float):
                self.host = host
                self.port = port
                self.timeout_s = timeout_s
                self.index = len(self.instances)
                self.enabled = 0
                self.closed = False
                self.instances.append(self)

            def write_output_label(self, pin_label: str, enabled: bool):
                if self.index == 0:
                    raise TimeoutError("timed out")
                self.enabled = 1 if enabled else 0
                return {pin_label: self.enabled}

            def read_outputs(self, labels):
                return {labels[0]: self.enabled}

            def close(self):
                self.closed = True

        self.controller.cfg.moxa3_simulation = False
        self.controller.cfg.moxa3_host = "moxa3.test"
        self.controller.cfg.moxa3_port = 502
        self.controller.cfg.moxa_timeout_s = 0.1

        with patch("mas004_rpi_databridge.setup_wickler_orchestrator.MoxaE1213Client", FlakyMoxa):
            with patch("mas004_rpi_databridge.setup_wickler_orchestrator.time.sleep") as sleep:
                self.controller._set_infeed_sensor_teach(False, attempts=3)

        self.assertEqual(2, len(FlakyMoxa.instances))
        self.assertTrue(FlakyMoxa.instances[0].closed)
        self.assertTrue(FlakyMoxa.instances[1].closed)
        self.assertTrue(sleep.called)

    def test_setup_measurement_tolerates_transient_esp_status_timeouts(self):
        self.controller._setup_measure_status = Mock(
            side_effect=[
                {"running": True, "completed": False, "phase": 3, "labels_measured": 10},
                TimeoutError("timed out"),
                TimeoutError("timed out"),
                {
                    "running": False,
                    "completed": True,
                    "phase": 6,
                    "labels_measured": 23,
                    "last_error": "",
                },
            ]
        )
        self.controller._abort_motor3_if_wickler_faulted = Mock()

        with patch("mas004_rpi_databridge.setup_wickler_orchestrator.time.sleep"):
            result = self.controller._wait_setup_measurement_complete(timeout_s=10.0)

        self.assertTrue(result["completed"])
        self.assertEqual(23, result["labels_measured"])

    def test_setup_measurement_tolerates_running_esp_reconnect_window(self):
        self.controller._setup_measure_status = Mock(
            side_effect=[
                {"running": True, "completed": False, "phase": 5, "labels_measured": 23},
                *[ConnectionRefusedError(111, "Connection refused") for _ in range(12)],
                {
                    "running": False,
                    "completed": True,
                    "phase": 6,
                    "labels_measured": 23,
                    "last_error": "",
                },
            ]
        )
        self.controller._abort_motor3_if_wickler_faulted = Mock()

        with patch("mas004_rpi_databridge.setup_wickler_orchestrator.time.sleep"):
            result = self.controller._wait_setup_measurement_complete(timeout_s=20.0)

        self.assertTrue(result["completed"])
        self.assertEqual(23, result["labels_measured"])

    def test_setup_measurement_aborts_after_repeated_esp_status_timeouts(self):
        self.controller._setup_measure_status = Mock(side_effect=TimeoutError("timed out"))
        self.controller._abort_motor3_if_wickler_faulted = Mock()

        with patch("mas004_rpi_databridge.setup_wickler_orchestrator.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "status communication failed"):
                self.controller._wait_setup_measurement_complete(timeout_s=10.0)

    def test_motor3_measurement_move_retries_when_operation_data_does_not_start(self):
        class FakeWicklerClient:
            def __init__(self):
                self.learning_starts = 0

            def start_diameter_learning(self, timeout_s: float | None = None):
                self.learning_starts += 1
                return {"ok": True}

        clients = [FakeWicklerClient(), FakeWicklerClient()]
        self.controller._winder_clients = Mock(return_value=clients)
        self.controller._esp = Mock(return_value="ACK")
        self.controller._wait_motor3_idle = Mock(
            side_effect=[
                RuntimeError("Motor 3 move was accepted but did not start/reach target: {}"),
                {"feedback_tenths_mm": 10000, "target_tenths_mm": 10000},
            ]
        )

        with patch("mas004_rpi_databridge.setup_wickler_orchestrator.time.sleep"):
            result = self.controller._run_motor3_measurement_move(1000.0, 100.0)

        self.assertEqual({"feedback_tenths_mm": 10000, "target_tenths_mm": 10000}, result)
        self.assertEqual([2, 2], [client.learning_starts for client in clients])
        calls = [call.args[0] for call in self.controller._esp.call_args_list]
        self.assertEqual(2, calls.count("MOTOR 3 MOVE_REL_MM_OP=1000.000"))
        self.assertIn("MOTOR 3 RESET_ALARM", calls)
        self.assertIn("MOTOR 3 RECOVER_ETO", calls)

    def test_wickler_calibrate_commands_are_started_in_parallel(self):
        barrier = threading.Barrier(2, timeout=1.0)

        class FakeDescriptor:
            def __init__(self, role: str):
                self.role = role

        class FakeWicklerClient:
            def __init__(self, role: str):
                self.descriptor = FakeDescriptor(role)

            def post_mode(self, mode: str, timeout_s: float | None = None):
                barrier.wait()
                return {"ok": True, "mode": mode, "timeout_s": timeout_s}

        self.controller._winder_clients = Mock(
            return_value=[FakeWicklerClient("unwinder"), FakeWicklerClient("rewinder")]
        )

        result = self.controller._run_wickler_commands_parallel("calibrate", timeout_s=5.0)

        self.assertEqual({"unwinder", "rewinder"}, {item["role"] for item in result})
        self.assertTrue(all(item["ok"] for item in result))

    def test_wickler_calibration_is_parallel_then_released_for_continuous_hold(self):
        calls: list[str] = []

        class FakeDescriptor:
            def __init__(self, role: str, label: str):
                self.role = role
                self.label = label

        class FakeWicklerClient:
            def __init__(self, role: str, label: str, wipe: float):
                self.descriptor = FakeDescriptor(role, label)
                self.wipe = wipe

            def post_mode(self, mode: str, timeout_s: float | None = None):
                calls.append(f"{self.descriptor.role}:{mode}")
                return {"ok": True, "mode": mode, "timeout_s": timeout_s}

            def release_for_continuous_motion(self, timeout_s: float | None = None):
                calls.append(f"{self.descriptor.role}:ready_motion")
                return {"ok": True, "timeout_s": timeout_s}

            def fetch_state(self, timeout_s: float | None = None):
                return {
                    "telemetry": {
                        "modeLabel": "Bereit",
                        "wipePercent": self.wipe,
                        "calibrated": True,
                    },
                    "drive": {"alarm": False, "continuousModeReady": True, "lastCommandOk": True},
                    "values": {},
                }

        self.controller._winder_clients = Mock(
            return_value=[
                FakeWicklerClient("unwinder", "Abwickler", 48.0),
                FakeWicklerClient("rewinder", "Aufwickler", 52.0),
            ]
        )

        result = self.controller._calibrate_wicklers_for_setup(timeout_s=5.0)

        self.assertEqual({"unwinder:calibrate", "rewinder:calibrate"}, set(calls[:2]))
        self.assertEqual({"unwinder:ready_motion", "rewinder:ready_motion"}, set(calls[2:]))
        self.assertEqual({"unwinder", "rewinder"}, {item["role"] for item in result})
        self.assertTrue(all(item.get("release", {}).get("ok") for item in result))

    def test_wickler_motion_ready_requires_released_stop_and_speed_mode(self):
        class FakeDescriptor:
            def __init__(self, role: str):
                self.role = role

        class FakeWicklerClient:
            def __init__(self, role: str):
                self.descriptor = FakeDescriptor(role)

            def fetch_state(self):
                return {
                    "telemetry": {
                        "modeLabel": "Bereit",
                        "externalStopActive": True,
                        "wipePercent": 50.0,
                        "calibrationPhase": 1,
                    },
                    "drive": {
                        "alarm": False,
                        "continuousModeReady": True,
                        "lastCommandOk": True,
                    },
                    "values": {},
                }

        self.controller._winder_clients = Mock(
            return_value=[FakeWicklerClient("unwinder"), FakeWicklerClient("rewinder")]
        )

        with patch("mas004_rpi_databridge.setup_wickler_orchestrator.time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "externer STOP aktiv"):
                self.controller._wait_wicklers_ready(timeout_s=0.01, require_motion_ready=True)
        sleep.assert_not_called()

    def test_wickler_ready_wait_aborts_immediately_on_fault_state(self):
        class FakeDescriptor:
            def __init__(self, role: str, label: str):
                self.role = role
                self.label = label

        class FakeWicklerClient:
            def __init__(self, role: str, label: str):
                self.descriptor = FakeDescriptor(role, label)

            def fetch_state(self):
                return {
                    "telemetry": {
                        "modeLabel": "Stoerung",
                        "modeCss": "fault",
                        "faultReason": "Stoerung",
                        "wipePercent": 29.3,
                        "calibrationPhase": 1,
                    },
                    "drive": {
                        "alarm": False,
                        "continuousModeReady": False,
                        "lastCommandOk": True,
                    },
                    "values": {"statusMas": 4},
                }

        self.controller._winder_clients = Mock(
            return_value=[FakeWicklerClient("unwinder", "Abwickler")]
        )

        with patch("mas004_rpi_databridge.setup_wickler_orchestrator.time.sleep") as sleep:
            with self.assertRaisesRegex(
                RuntimeError,
                r"Wickler-Bereitschaft blockiert: Abwickler \(unwinder\) rot",
            ):
                self.controller._wait_wicklers_ready(timeout_s=90.0)

        sleep.assert_not_called()

    def test_wickler_ready_motion_commands_are_started_in_parallel(self):
        barrier = threading.Barrier(2, timeout=1.0)

        class FakeDescriptor:
            def __init__(self, role: str):
                self.role = role

        class FakeWicklerClient:
            def __init__(self, role: str):
                self.descriptor = FakeDescriptor(role)

            def release_for_continuous_motion(self, timeout_s: float | None = None):
                barrier.wait()
                return {"ok": True, "timeout_s": timeout_s}

        self.controller._winder_clients = Mock(
            return_value=[FakeWicklerClient("unwinder"), FakeWicklerClient("rewinder")]
        )

        result = self.controller._release_wicklers_for_continuous_measurement(timeout_s=5.0)

        self.assertEqual({"unwinder", "rewinder"}, {item["role"] for item in result})
        self.assertTrue(all(item["ok"] for item in result))

    def test_setup_abort_stops_motion_when_state_changes(self):
        commands: list[str] = []
        self.controller._esp = Mock(side_effect=lambda line, read_timeout_s=None: commands.append(line) or "ACK")

        _set_machine_state(self.db, current_state=8, requested_state=2)
        self.params.apply_device_value("MAS0001", "8", promote_default=True)
        self.params.apply_device_value("MAS0002", "2", promote_default=True)

        with self.assertRaisesRegex(RuntimeError, "Setup no longer active"):
            self.controller._wait_wicklers_ready(timeout_s=0.1)

        self.controller.stop_all_motion()

        self.assertIn("PROCESS SETUP_MEASURE STOP", commands)
        self.assertIn("PROCESS WICKLER CANCEL", commands)
        self.assertIn("PROCESS INDEXED STOP", commands)
        self.assertIn("PROCESS PROFILE STOP", commands)

    def test_setup_abort_stops_before_post_status_read(self):
        commands: list[str] = []

        def fake_esp(line: str, read_timeout_s=None):
            commands.append(line)
            if line == "PROCESS SETUP_MEASURE STATUS?":
                return 'JSON {"running":false,"last_error":"setup_measure_phase_timeout"}'
            return "ACK"

        self.controller._esp = Mock(side_effect=fake_esp)
        self.controller._winder_clients = Mock(return_value=[])

        self.controller.stop_all_motion()

        self.assertIn("PROCESS SETUP_MEASURE STATUS?", commands)
        self.assertIn("PROCESS SETUP_MEASURE STOP", commands)
        self.assertLess(
            commands.index("PROCESS SETUP_MEASURE STOP"),
            commands.index("PROCESS SETUP_MEASURE STATUS?"),
        )
        self.assertIn("MOTOR 3 MOVE_VEL_MM_S=0", commands)
        self.assertIn("PROCESS WICKLER CANCEL", commands)

    def test_setup_measurement_stops_motor3_when_wickler_turns_red(self):
        class FakeDescriptor:
            def __init__(self, role: str, label: str):
                self.role = role
                self.label = label

        class FakeWicklerClient:
            def __init__(self, role: str, label: str):
                self.descriptor = FakeDescriptor(role, label)

            def fetch_state(self):
                return {
                    "telemetry": {
                        "modeLabel": "Stoerung",
                        "modeCss": "fault",
                        "faultReason": "Taenzerarm oberer Anschlag",
                        "wipePercent": 99.0,
                    },
                    "drive": {"alarm": False},
                    "values": {"maeHigh": True},
                }

        self.controller._winder_clients = Mock(
            return_value=[
                FakeWicklerClient("unwinder", "Abwickler"),
                FakeWicklerClient("rewinder", "Aufwickler"),
            ]
        )
        self.controller._setup_measure_status = Mock(return_value={"running": True, "completed": False})
        self.controller.stop_all_motion = Mock()

        with self.assertRaisesRegex(RuntimeError, "Wickler fault during Motor 3 movement"):
            self.controller._wait_setup_measurement_complete(timeout_s=10.0)

        self.controller.stop_all_motion.assert_called_once()
        self.assertEqual("1", self.params.get_effective_value("MAS0028"))

    def test_setup_measurement_stops_motor3_before_wickler_hits_endstop(self):
        class FakeDescriptor:
            def __init__(self, role: str, label: str):
                self.role = role
                self.label = label

        class FakeWicklerClient:
            def __init__(self, role: str, label: str, wipe_percent: float):
                self.descriptor = FakeDescriptor(role, label)
                self.wipe_percent = wipe_percent

            def fetch_state(self):
                return {
                    "ok": True,
                    "telemetry": {
                        "modeLabel": "Bereit",
                        "modeCss": "ready",
                        "faultReason": "-",
                        "wipePercent": self.wipe_percent,
                    },
                    "drive": {"alarm": False},
                    "values": {},
                    "device": {"reachable": True},
                }

        self.controller._winder_clients = Mock(
            return_value=[
                FakeWicklerClient("unwinder", "Abwickler", 4.5),
                FakeWicklerClient("rewinder", "Aufwickler", 50.0),
            ]
        )

        faults = self.controller._wickler_faults_during_motor3_motion()

        self.assertIn("Abwickler (unwinder) Wippe im unteren Sicherheitsbereich", "; ".join(faults))

    def test_setup_measurement_stops_motor3_before_wickler_hits_upper_endstop(self):
        class FakeDescriptor:
            def __init__(self, role: str, label: str):
                self.role = role
                self.label = label

        class FakeWicklerClient:
            def __init__(self, role: str, label: str, wipe_percent: float):
                self.descriptor = FakeDescriptor(role, label)
                self.wipe_percent = wipe_percent

            def fetch_state(self):
                return {
                    "ok": True,
                    "telemetry": {
                        "modeLabel": "Bereit",
                        "modeCss": "ready",
                        "faultReason": "-",
                        "wipePercent": self.wipe_percent,
                    },
                    "drive": {"alarm": False, "online": True, "lastCommandOk": True},
                    "values": {},
                    "device": {"reachable": True},
                }

        self.controller._winder_clients = Mock(
            return_value=[
                FakeWicklerClient("unwinder", "Abwickler", 50.0),
                FakeWicklerClient("rewinder", "Aufwickler", 96.0),
            ]
        )

        faults = self.controller._wickler_faults_during_motor3_motion()

        self.assertIn("Aufwickler (rewinder) Wippe im oberen Sicherheitsbereich", "; ".join(faults))

    def test_setup_measurement_stops_motor3_when_wickler_not_calibrated(self):
        class FakeDescriptor:
            def __init__(self, role: str, label: str):
                self.role = role
                self.label = label

        class FakeWicklerClient:
            def __init__(self, role: str, label: str):
                self.descriptor = FakeDescriptor(role, label)

            def fetch_state(self):
                return {
                    "ok": True,
                    "telemetry": {
                        "modeLabel": "Bereit",
                        "modeCss": "ready",
                        "faultReason": "Wippe nicht eingemessen",
                        "wipePercent": 50.0,
                        "calibrated": False,
                        "requiresCalibration": True,
                    },
                    "drive": {"alarm": False, "online": True, "lastCommandOk": True},
                    "values": {},
                    "device": {"reachable": True},
                }

        self.controller._winder_clients = Mock(
            return_value=[
                FakeWicklerClient("unwinder", "Abwickler"),
                FakeWicklerClient("rewinder", "Aufwickler"),
            ]
        )

        faults = self.controller._wickler_faults_during_motor3_motion()

        self.assertIn("Abwickler (unwinder) Wippe nicht eingemessen", "; ".join(faults))
        self.assertIn("Aufwickler (rewinder) Wippe nicht eingemessen", "; ".join(faults))

    def test_setup_measurement_stops_motor3_when_wickler_stop_input_becomes_active(self):
        class FakeDescriptor:
            def __init__(self, role: str, label: str):
                self.role = role
                self.label = label

        class FakeWicklerClient:
            def __init__(self, role: str, label: str):
                self.descriptor = FakeDescriptor(role, label)

            def fetch_state(self):
                return {
                    "ok": True,
                    "telemetry": {
                        "modeLabel": "Bereit",
                        "modeCss": "ready",
                        "faultReason": "-",
                        "wipePercent": 50.0,
                        "externalStopActive": True,
                    },
                    "drive": {"alarm": False, "online": True, "lastCommandOk": True},
                    "values": {},
                    "device": {"reachable": True},
                }

        self.controller._winder_clients = Mock(
            return_value=[
                FakeWicklerClient("unwinder", "Abwickler"),
                FakeWicklerClient("rewinder", "Aufwickler"),
            ]
        )

        faults = self.controller._wickler_faults_during_motor3_motion()

        self.assertIn("Abwickler (unwinder) externer STOP aktiv", "; ".join(faults))

    def test_successful_setup_keeps_wicklers_ready_before_returning_pause_baseline(self):
        class FakeWicklerClient:
            def __init__(self):
                self.master_payloads = []

            def post_master(self, payload, timeout_s: float | None = None):
                self.master_payloads.append(dict(payload))
                return {"ok": True, "payload": payload}

            def fetch_state(self):
                return {"master": {"map0023": 5, "map0024": 95, "map0025": 1.0, "map0047": False}}

        self.controller._sync_setup_params_to_esp = Mock()
        self.controller._configure_motor3 = Mock()
        fake_wicklers = [FakeWicklerClient(), FakeWicklerClient()]
        self.controller._winder_clients = Mock(return_value=fake_wicklers)
        self.controller._run_wickler_commands_parallel = Mock(
            side_effect=lambda action, timeout_s=5.0: [{"role": "all", "action": action, "ok": True}]
        )
        self.controller._calibrate_wicklers_for_setup = Mock(
            return_value=[
                {"role": "rewinder", "action": "calibrate", "ok": True},
                {"role": "unwinder", "action": "calibrate", "ok": True},
            ]
        )
        self.controller._wait_wicklers_ready = Mock()
        self.controller._release_wicklers_for_continuous_measurement = Mock(
            return_value=[{"role": "all", "action": "ready_motion", "ok": True}]
        )
        self.controller._abort_motor3_if_wickler_faulted = Mock()
        self.controller._prepare_motor3_for_measurement = Mock()
        self.controller._calibrate_motor3_scale_against_infeed_encoder = Mock(
            return_value={"ok": True, "steps_per_mm": 31.25, "attempts": []}
        )
        self.controller._run_sensor_referenced_measurement_with_diameter_learning = Mock(
            return_value=(
                {"labels_measured": 3, "reference_mm": 12.0, "target_mm": 2.0},
                {"unwinder": {"candidate_diameter_mm": 200.0}},
                {
                    "ok": True,
                    "ready": {"ok": True},
                    "machine_run": {"ok": True, "skipped": "sea_vision_bypass_active"},
                },
            )
        )
        self.controller._prepare_production_pause_baseline = Mock(return_value={"ok": True})

        result = self.controller.run()

        actions = [call.args[0] for call in self.controller._run_wickler_commands_parallel.call_args_list]
        self.assertEqual(["stop", "resetAlarm", "etoRecovery"], actions)
        self.controller._calibrate_wicklers_for_setup.assert_called_once_with(timeout_s=5.0)
        self.assertEqual(1, self.controller._release_wicklers_for_continuous_measurement.call_count)
        self.controller._calibrate_motor3_scale_against_infeed_encoder.assert_not_called()
        self.assertNotIn("motor3_scale:31.250000", result["applied"])
        self.assertIn("wicklers:ready", result["applied"])
        self.assertIn("sea_vision:ready", result["applied"])
        self.assertIn("sea_vision:machine_run_skipped", result["applied"])
        self.assertNotIn("wicklers:stop", result["applied"])
        self.assertIn("sea_vision_setup", result)
        self.assertEqual([{"role": "all", "action": "ready_motion", "ok": True}], result["final_wickler_ready"])
        for fake in fake_wicklers:
            self.assertEqual(
                [
                    {
                        "indexedModeEnabled": "0",
                        "map0023": "5",
                        "map0024": "95",
                        "map0025": "1.0",
                        "map0047": "0",
                    }
                ],
                fake.master_payloads,
            )

    def test_wickler_setup_threshold_payload_converts_map0025_tenths_percent(self):
        _insert_param(self.db, "MAP0025", "1")

        payload = self.controller._wickler_master_threshold_payload()

        self.assertEqual("0.1", payload["map0025"])

    def test_setup_measurement_uses_map0014_speed(self):
        _insert_param(self.db, "MAP0014", "200")

        self.assertEqual(200.0, self.controller._setup_learn_speed_mm_s())

    def test_setup_measurement_slip_tolerance_uses_twenty_mm_floor(self):
        self.assertEqual(20.0, self.controller._slip_tolerance_mm())

    def test_setup_disables_motor_auto_poll_without_restoring_it(self):
        self.controller.cfg.esp_simulation = False
        commands: list[str] = []

        def fake_esp(line, **_kwargs):
            commands.append(str(line))
            if line == "MOTOR POLL?":
                if commands.count("MOTOR POLL?") == 1:
                    return 'JSON {"ok":true,"auto_poll":true}'
                return 'JSON {"ok":true,"auto_poll":false}'
            if line == "MOTOR POLL=0":
                return "ACK_" + line.replace(" ", "_")
            raise AssertionError(line)

        self.controller._esp = Mock(side_effect=fake_esp)

        result = self.controller._ensure_motor_auto_poll_disabled_for_setup()

        self.assertTrue(result["disabled"])
        self.assertEqual(["MOTOR POLL?", "MOTOR POLL=0", "MOTOR POLL?"], commands)

    def test_setup_measurement_status_refreshes_motor3_without_global_poll(self):
        self.controller.cfg.esp_simulation = False
        commands: list[str] = []

        def fake_esp(line, **_kwargs):
            commands.append(str(line))
            if line == "MOTOR 3 REFRESH":
                return 'JSON {"ok":true,"motor":{"state":{"ready":true,"busy":false,"move":false}}}'
            if line == "PROCESS SETUP_MEASURE STATUS?":
                return 'JSON {"ok":true,"running":false,"completed":true,"phase":6,"last_error":""}'
            raise AssertionError(line)

        self.controller._esp = Mock(side_effect=fake_esp)

        status = self.controller._setup_measure_status()

        self.assertTrue(status["completed"])
        self.assertEqual(["MOTOR 3 REFRESH", "PROCESS SETUP_MEASURE STATUS?"], commands)
        self.assertFalse(any(command.startswith("MOTOR POLL=") for command in commands))

    def test_wickler_leader_speed_is_sent_as_direction_hint(self):
        class FakeDescriptor:
            def __init__(self, role: str):
                self.role = role

        class FakeWicklerClient:
            def __init__(self, role: str):
                self.descriptor = FakeDescriptor(role)
                self.master_payloads: list[dict[str, str]] = []

            def available(self):
                return True

            def post_master(self, payload, timeout_s: float | None = None):
                self.master_payloads.append(dict(payload))
                return {"ok": True, "payload": payload, "timeout_s": timeout_s}

        clients = [FakeWicklerClient("unwinder"), FakeWicklerClient("rewinder")]
        self.controller._winder_clients = Mock(return_value=clients)

        result = self.controller._set_wicklers_leader_speed(-200.0, timeout_s=1.25)

        self.assertTrue(all(item["ok"] for item in result))
        self.assertEqual([{"leaderSpeedMmS": "-200.000"}], clients[0].master_payloads)
        self.assertEqual([{"leaderSpeedMmS": "-200.000"}], clients[1].master_payloads)

    def test_setup_measurement_phase_syncs_wickler_leader_direction_once_per_change(self):
        self.controller._set_wicklers_leader_speed = Mock()

        for phase_name in (
            "forward_500_after_infeed_teach",
            "forward_500_after_infeed_teach",
            "rewind_to_zero",
            "forward_2500_control_teach",
            "rewind_to_first_label_minus_backoff",
            "complete",
        ):
            self.controller._sync_wickler_leader_for_setup_measure_status(
                {"running": phase_name != "complete", "phase_name": phase_name},
                200.0,
            )

        self.assertEqual(
            [200.0, -200.0, 200.0, -200.0, 0.0],
            [call.args[0] for call in self.controller._set_wicklers_leader_speed.call_args_list],
        )

    def test_setup_aborts_when_wickler_map0047_sync_is_not_confirmed(self):
        class FakeWicklerClient:
            def post_master(self, payload, timeout_s: float | None = None):
                return {"ok": True}

            def fetch_state(self):
                return {"master": {"map0047": True}}

        self.controller._winder_clients = Mock(return_value=[FakeWicklerClient()])

        with self.assertRaisesRegex(RuntimeError, "map0047 is 1, expected 0"):
            self.controller._sync_wickler_setup_master()

    def test_motor3_scale_calibration_corrects_steps_from_infeed_encoder(self):
        self.controller._configure_motor3 = Mock()
        self.controller._abort_motor3_if_wickler_faulted = Mock()
        self.controller._esp = Mock(return_value="ACK")
        self.controller._motor3_status = Mock(
            side_effect=[
                {"config": {"steps_per_mm": 100.0}},
                {"config": {"steps_per_mm": 99.502488}},
            ]
        )
        self.controller._wait_motor3_idle = Mock(
            return_value={"feedback_tenths_mm": 20000, "target_tenths_mm": 20000}
        )
        self.controller._infeed_encoder_mm = Mock(side_effect=[2010.0, 2000.2])
        self.controller._persist_motor3_steps_per_mm = Mock(
            return_value={"config": {"steps_per_mm": 99.502488}}
        )

        result = self.controller._calibrate_motor3_scale_against_infeed_encoder(100.0, 300.0)

        self.assertTrue(result["ok"])
        self.assertTrue(result["correction_applied"])
        self.assertEqual(2, len(result["attempts"]))
        self.controller._persist_motor3_steps_per_mm.assert_called_once()
        corrected = self.controller._persist_motor3_steps_per_mm.call_args.args[0]
        self.assertAlmostEqual(99.502488, corrected, places=5)
        commands = [call.args[0] for call in self.controller._esp.call_args_list]
        self.assertEqual(2, commands.count("PROCESS PRODUCTION_RESET"))
        self.assertEqual(2, commands.count("MOTOR 3 SET_POSITION_MM=0.000"))
        self.assertEqual(2, commands.count("MOTOR 3 MOVE_ABS_MM=2000.000"))

    def test_single_transient_wickler_offline_sample_does_not_abort_motor3(self):
        class FakeDescriptor:
            def __init__(self, role: str, label: str):
                self.role = role
                self.label = label

        class FakeWicklerClient:
            def __init__(self, role: str, label: str):
                self.descriptor = FakeDescriptor(role, label)
                self.calls = 0

            def fetch_state(self):
                self.calls += 1
                if self.calls == 1:
                    return {
                        "ok": False,
                        "telemetry": {"modeLabel": "Offline", "modeCss": "fault", "faultReason": "timeout"},
                        "drive": {"alarm": False},
                        "values": {},
                        "device": {"reachable": False, "error": "timeout"},
                    }
                return {
                    "ok": True,
                    "telemetry": {"modeLabel": "Bereit", "modeCss": "ready", "faultReason": "-", "wipePercent": 50.0},
                    "drive": {"alarm": False},
                    "values": {},
                    "device": {"reachable": True},
                }

        self.controller._winder_clients = Mock(
            return_value=[
                FakeWicklerClient("unwinder", "Abwickler"),
                FakeWicklerClient("rewinder", "Aufwickler"),
            ]
        )

        self.assertEqual([], self.controller._wickler_faults_during_motor3_motion())
        self.assertEqual([], self.controller._wickler_faults_during_motor3_motion())

    def test_repeated_wickler_offline_samples_abort_motor3(self):
        class FakeDescriptor:
            def __init__(self, role: str, label: str):
                self.role = role
                self.label = label

        class FakeWicklerClient:
            def __init__(self, role: str, label: str):
                self.descriptor = FakeDescriptor(role, label)

            def fetch_state(self):
                return {
                    "ok": False,
                    "telemetry": {"modeLabel": "Offline", "modeCss": "fault", "faultReason": "timeout"},
                    "drive": {"alarm": False},
                    "values": {},
                    "device": {"reachable": False, "error": "timeout"},
                }

        self.controller._winder_clients = Mock(
            return_value=[
                FakeWicklerClient("unwinder", "Abwickler"),
                FakeWicklerClient("rewinder", "Aufwickler"),
            ]
        )

        self.assertEqual([], self.controller._wickler_faults_during_motor3_motion())
        self.assertEqual([], self.controller._wickler_faults_during_motor3_motion())
        faults = self.controller._wickler_faults_during_motor3_motion()
        self.assertIn("Abwickler (unwinder) offline", "; ".join(faults))
        self.assertIn("Aufwickler (rewinder) offline", "; ".join(faults))


if __name__ == "__main__":
    unittest.main()

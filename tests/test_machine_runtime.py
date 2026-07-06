from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
import json
import threading

from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.modules.setdefault("ping3", SimpleNamespace(ping=lambda *_args, **_kwargs: 1.0))

import mas004_rpi_databridge.machine_runtime as machine_runtime_module
from mas004_rpi_databridge.config import Settings
from mas004_rpi_databridge.db import DB, now_ts
from mas004_rpi_databridge.io_master import IoStore
from mas004_rpi_databridge.logstore import LogStore
from mas004_rpi_databridge.machine_runtime import (
    MachineRuntime,
    PRODUCTION_RUNTIME_INFO_KEY,
    PRODUCTION_WICKLER_MONITOR_COMM_MAX_MISSES,
    mark_external_purge_clear,
    mark_external_purge_start,
    recent_external_purge_clear,
)
from mas004_rpi_databridge.machine_semantics import pack_label_status_word
from mas004_rpi_databridge.motor_setup_lock import touch_motor_setup_manual_lock
from mas004_rpi_databridge.outbox import Outbox
from mas004_rpi_databridge.params import ParamStore
from mas004_rpi_databridge.state_dedupe import ValueDedupeStore


def _insert_param(
    db: DB,
    pkey: str,
    ptype: str,
    pid: str,
    default_v: str | None,
    rw: str,
    esp_rw: str = "W",
    dtype: str = "string",
):
    with db._conn() as c:
        c.execute(
            """INSERT INTO params(
                pkey,ptype,pid,min_v,max_v,default_v,unit,rw,esp_rw,dtype,name,format_relevant,
                message,possible_cause,effects,remedy,updated_ts
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                pkey,
                ptype,
                pid,
                None,
                None,
                default_v,
                "",
                rw,
                esp_rw,
                dtype,
                pkey,
                "NO",
                None,
                None,
                None,
                None,
                now_ts(),
            ),
        )


def _insert_device_map(db: DB, pkey: str, zbc_mapping: str):
    with db._conn() as c:
        c.execute(
            """INSERT INTO param_device_map(
                pkey, esp_key, zbc_mapping, zbc_message_id, zbc_command_id, zbc_value_codec,
                zbc_scale, zbc_offset, ultimate_set_cmd, ultimate_get_cmd, ultimate_var_name, updated_ts
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                pkey,
                None,
                zbc_mapping,
                None,
                None,
                None,
                None,
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
    with db._conn() as c:
        c.execute(
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
        c.execute(
            """INSERT INTO io_values(io_key, value, quality, source, updated_ts)
               VALUES(?,?,?,?,?)
               ON CONFLICT(io_key) DO UPDATE SET value=excluded.value, quality=excluded.quality,
                 source=excluded.source, updated_ts=excluded.updated_ts""",
            (io_key, value, "simulation", "test", now_ts()),
        )


class MachineRuntimeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.db = DB(str(base / "db.sqlite3"))
        self.params = ParamStore(self.db)
        self.io_store = IoStore(self.db)
        self.logs = LogStore(self.db, log_dir=str(base / "logs"), production_log_dir=str(base / "production"))
        self.outbox = Outbox(self.db)
        self.cfg = Settings(
            db_path=str(base / "db.sqlite3"),
            peer_base_url="",
            peer_base_url_secondary="",
            shared_secret="",
            esp_simulation=True,
            raspi_io_simulation=True,
            moxa1_simulation=True,
            moxa2_simulation=True,
        )

        for pkey, ptype, pid, default_v, rw, esp_rw, dtype in (
            ("MAP0001", "MAP", "0001", "500", "W", "R", "uint16"),
            ("MAP0002", "MAP", "0002", "1000", "W", "R", "uint16"),
            ("MAP0003", "MAP", "0003", "20", "W", "R", "uint16"),
            ("MAP0004", "MAP", "0004", "100", "W", "R", "uint16"),
            ("MAP0005", "MAP", "0005", "0", "W", "W", "int8"),
            ("MAP0006", "MAP", "0006", "0", "W", "W", "int8"),
            ("MAP0011", "MAP", "0011", "0", "W", "R", "int8"),
            ("MAP0012", "MAP", "0012", "0", "W", "R", "int8"),
            ("MAP0014", "MAP", "0014", "100", "W", "R", "uint16"),
            ("MAP0016", "MAP", "0016", "0", "W", "R", "bool"),
            ("MAP0017", "MAP", "0017", "1050", "W", "R", "uint16"),
            ("MAP0018", "MAP", "0018", "6100", "W", "R", "uint16"),
            ("MAP0019", "MAP", "0019", "11000", "W", "R", "uint16"),
            ("MAP0020", "MAP", "0020", "8500", "W", "R", "uint16"),
            ("MAP0021", "MAP", "0021", "14900", "W", "R", "uint16"),
            ("MAP0035", "MAP", "0035", "0", "W", "R", "bool"),
            ("MAP0036", "MAP", "0036", "0", "W", "R", "bool"),
            ("MAP0037", "MAP", "0037", "0", "W", "R", "bool"),
            ("MAP0038", "MAP", "0038", "0", "W", "R", "bool"),
            ("MAP0040", "MAP", "0040", "5", "W", "R", "uint8"),
            ("MAP0065", "MAP", "0065", "1111111", "W", "R", "uint8"),
            ("MAP0066", "MAP", "0066", "9600", "W", "R", "uint16"),
            ("MAP0071", "MAP", "0071", "5200", "W", "R", "uint16"),
            ("MAP0075", "MAP", "0075", "100", "W", "R", "uint16"),
            ("MAP0079", "MAP", "0079", "0", "W", "R", "bool"),
            ("MAE0004", "MAE", "0004", "0", "R", "W", "bool"),
            ("MAE0005", "MAE", "0005", "0", "R", "W", "bool"),
            ("MAE0006", "MAE", "0006", "0", "R", "W", "bool"),
            ("MAE0007", "MAE", "0007", "0", "R", "W", "bool"),
            ("MAE0008", "MAE", "0008", "0", "R", "W", "bool"),
            ("MAE0009", "MAE", "0009", "0", "R", "W", "bool"),
            ("MAE0010", "MAE", "0010", "0", "R", "W", "bool"),
            ("MAE0025", "MAE", "0025", "0", "R", "W", "bool"),
            ("MAE0026", "MAE", "0026", "0", "R", "W", "bool"),
            ("MAE0027", "MAE", "0027", "0", "R", "W", "bool"),
            ("MAE0028", "MAE", "0028", "0", "R", "W", "bool"),
            ("MAE0029", "MAE", "0029", "0", "R", "W", "bool"),
            ("MAE0030", "MAE", "0030", "0", "R", "W", "bool"),
            ("MAE0032", "MAE", "0032", "0", "R", "W", "bool"),
            ("MAE0033", "MAE", "0033", "0", "R", "W", "bool"),
            ("MAE0034", "MAE", "0034", "0", "R", "W", "bool"),
            ("MAE0048", "MAE", "0048", "0", "R", "W", "bool"),
            ("MAS0001", "MAS", "0001", "1", "R", "W", "uint8"),
            ("MAS0002", "MAS", "0002", "0", "W", "W", "uint8"),
            ("MAS0003", "MAS", "0003", "0", "R", "W", "uint32"),
            ("MAS0028", "MAS", "0028", "0", "W", "W", "bool"),
            ("MAS0029", "MAS", "0029", "JOB_TEST", "W", "N", "string"),
        ):
            _insert_param(self.db, pkey, ptype, pid, default_v, rw, esp_rw, dtype)

        for pin, value in (
            ("I0.6", "1"),
            ("I0.7", "0"),
            ("I0.8", "0"),
            ("I0.9", "0"),
            ("I0.10", "0"),
            ("I0.11", "0"),
            ("I0.12", "0"),
        ):
            _insert_io_point(self.db, "raspi_plc21", "Raspberry PLC 21", pin, "input", value)
        for pin in ("Q0.0", "Q0.1", "Q0.2", "Q0.3", "Q0.4", "Q0.5", "Q0.6", "Q0.7"):
            _insert_io_point(self.db, "raspi_plc21", "Raspberry PLC 21", pin, "output", "0")
        for pin, value in (
            ("I0.2", "0"),
            ("I0.4", "0"),
            ("I0.7", "1"),
            ("I0.8", "1"),
            ("I0.11", "0"),
            ("I0.12", "0"),
        ):
            _insert_io_point(self.db, "esp32_plc58", "ESP32 PLC 58", pin, "input", value)
        _insert_io_point(self.db, "esp32_plc58", "ESP32 PLC 58", "Q0.2", "output", "0")
        _insert_io_point(self.db, "esp32_plc58", "ESP32 PLC 58", "Q0.3", "output", "0")
        _insert_io_point(self.db, "moxa_e1213_1", "Moxa ioLogik E1213 #1", "DIO3", "output", "0")
        for pin in ("DIO0", "DIO1", "DIO2"):
            _insert_io_point(self.db, "moxa_e1213_3", "Moxa ioLogik E1213 #3", pin, "output", "0")

    def tearDown(self):
        self._tmp.cleanup()

    def build_runtime(self) -> MachineRuntime:
        return MachineRuntime(self.cfg, self.db, self.params, self.io_store, self.logs, self.outbox)

    def mark_production_active(self, runtime: MachineRuntime) -> None:
        runtime._write_state(
            current_state=5,
            requested_state=5,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={PRODUCTION_RUNTIME_INFO_KEY: {"active": True, "active_since_ts": 1234.5}},
        )

    def test_microtom_command_is_mapped_to_transition_and_final_state(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=9,
            requested_state=9,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={},
        )

        ok, msg = self.params.set_value("MAS0002", "3", actor="microtom")
        self.assertTrue(ok, msg)

        with patch("mas004_rpi_databridge.machine_runtime.SetupWicklerOrchestrator") as controller_cls:
            controller = controller_cls.return_value
            controller.run.return_value = {"ok": True, "applied": ["unwinder:200.0mm", "rewinder:200.0mm"]}
            first = runtime.refresh()
            controller.run.assert_called_once()
            self.assertTrue(callable(controller.run.call_args.kwargs.get("wait_for_format_axes")))
        self.assertEqual(7, first["current_state"])
        self.assertEqual(7, first["requested_state"])
        self.assertEqual(0, first["info"]["requested_command"])
        self.assertEqual("0", self.params.get_effective_value("MAS0002"))
        self.assertEqual(True, first["info"]["setup"]["last_result"]["ok"])
        self.assertEqual(True, first["info"]["setup"]["parameters_ready"])

        second = runtime.refresh()
        self.assertEqual(7, second["current_state"])
        self.assertEqual(7, second["requested_state"])
        self.assertEqual(9600, second["info"]["format_plan"]["process"]["led_strip_first_led_distance_tenths_mm"])

    def test_virtual_setup_button_sets_transition_status_immediately(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=9,
            requested_state=9,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={},
        )

        result = runtime.press_virtual_button("setup")

        self.assertTrue(result["ok"], result)
        snapshot = runtime.snapshot()
        self.assertEqual(2, snapshot["current_state"])
        self.assertEqual(3, snapshot["requested_state"])
        self.assertEqual("2", self.params.get_effective_value("MAS0001"))
        self.assertEqual("3", self.params.get_effective_value("MAS0002"))

        with patch("mas004_rpi_databridge.machine_runtime.SetupWicklerOrchestrator") as controller_cls:
            controller_cls.return_value.run.return_value = {"ok": True}
            finished = runtime.refresh()

        controller_cls.return_value.run.assert_called_once()
        self.assertEqual(7, finished["current_state"])
        self.assertEqual(7, finished["requested_state"])

    def test_setup_starts_wickler_before_format_axes_finish(self):
        runtime = self.build_runtime()
        axis_started = threading.Event()
        allow_axis_finish = threading.Event()
        workflow_started_before_axis_done = threading.Event()
        wait_hook_called = threading.Event()

        def slow_axis_positioning(_format_plan):
            axis_started.set()
            self.assertTrue(allow_axis_finish.wait(timeout=2.0))
            return {"ok": True, "finished": True}

        def workflow_run(*, wait_for_format_axes):
            self.assertTrue(axis_started.wait(timeout=2.0))
            if not allow_axis_finish.is_set():
                workflow_started_before_axis_done.set()
            allow_axis_finish.set()
            axis_result = wait_for_format_axes()
            wait_hook_called.set()
            return {"ok": True, "axis_result": axis_result}

        with (
            patch.object(runtime, "_position_setup_format_axes", side_effect=slow_axis_positioning),
            patch("mas004_rpi_databridge.machine_runtime.SetupWicklerOrchestrator") as controller_cls,
        ):
            controller_cls.return_value.run.side_effect = workflow_run
            result = runtime._perform_setup_wickler_calibration()

        self.assertTrue(result["ok"], result)
        self.assertTrue(workflow_started_before_axis_done.is_set())
        self.assertTrue(wait_hook_called.is_set())

    def test_recent_setup_complete_recovers_stuck_setup_state_to_pause(self):
        runtime = self.build_runtime()
        setup_seen_ts = now_ts() - 30.0
        runtime._write_state(
            current_state=3,
            requested_state=3,
            state_source="test_stuck_setup",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={
                "setup": {
                    "mas0002_setup_seen_ts": setup_seen_ts,
                    "last_result": {"ok": True},
                    "parameters_ready": True,
                }
            },
        )
        runtime._record_event(
            "setup_complete",
            "info",
            "Einrichten abgeschlossen: Formatparameter gueltig, Wechsel zu Pause freigegeben",
            {"target_state": 7},
        )

        first = runtime.refresh()
        second = runtime.refresh()

        self.assertEqual(6, first["current_state"])
        self.assertEqual(7, first["requested_state"])
        self.assertEqual(7, second["current_state"])
        self.assertEqual(7, second["requested_state"])
        self.assertEqual("7", self.params.get_effective_value("MAS0001"))
        self.assertFalse(second["info"]["setup"]["pause_pending"])

    def test_pause_idle_keeps_wicklers_ready_and_verifies_state(self):
        runtime = self.build_runtime()

        class FakeWicklerClient:
            def __init__(self, _cfg, role):
                self.role = role

            def available(self):
                return True

            def post_master(self, payload, timeout_s=None):
                self.assert_payload = dict(payload)
                return {"ok": True}

            def release_for_continuous_motion(self, timeout_s=None):
                return {"ok": True, "readyMotion": True}

            def fetch_state(self, timeout_s=None):
                return {
                    "master": {"indexedModeEnabled": False},
                    "telemetry": {
                        "modeLabel": "Bereit",
                        "modeCss": "ready",
                        "externalStopActive": False,
                        "indexedModeEnabled": False,
                        "wipePercent": 50.0,
                    },
                    "drive": {
                        "online": True,
                        "ready": True,
                        "move": False,
                        "alarm": False,
                        "continuousModeReady": True,
                        "lastCommandOk": True,
                    },
                    "values": {},
                }

        with patch("mas004_rpi_databridge.machine_runtime.SmartWicklerClient", FakeWicklerClient):
            result = runtime._set_production_wicklers_idle(target_state=7)

        self.assertTrue(all(item["ok"] for item in result), result)
        self.assertTrue(all(item["verify"]["ok"] for item in result), result)

    def test_pause_idle_after_safety_recovers_wicklers_before_ready(self):
        runtime = self.build_runtime()
        calls: list[tuple[str, str, object]] = []

        class FakeWicklerClient:
            def __init__(self, _cfg, role):
                self.role = role

            def available(self):
                return True

            def post_master(self, payload, timeout_s=None):
                calls.append((self.role, "master", dict(payload)))
                return {"ok": True}

            def post_mode(self, mode, timeout_s=None):
                calls.append((self.role, "mode", mode))
                return {"ok": True}

            def release_for_continuous_motion(self, timeout_s=None):
                calls.append((self.role, "ready", bool(timeout_s)))
                return {"ok": True, "readyMotion": True}

            def fetch_state(self, timeout_s=None):
                return {
                    "master": {"indexedModeEnabled": False},
                    "telemetry": {
                        "modeLabel": "Bereit",
                        "modeCss": "ready",
                        "externalStopActive": False,
                        "indexedModeEnabled": False,
                        "wipePercent": 50.0,
                    },
                    "drive": {
                        "online": True,
                        "ready": True,
                        "move": False,
                        "alarm": False,
                        "continuousModeReady": True,
                        "lastCommandOk": True,
                    },
                    "values": {},
                }

        with patch("mas004_rpi_databridge.machine_runtime.SmartWicklerClient", FakeWicklerClient):
            result = runtime._set_production_wicklers_idle(target_state=7, recover_after_safety=True)

        self.assertTrue(all(item["ok"] for item in result), result)
        self.assertEqual(
            [
                ("unwinder", "master", {"indexedModeEnabled": "0"}),
                ("unwinder", "mode", "resetAlarm"),
                ("unwinder", "mode", "etoRecovery"),
                ("unwinder", "ready", True),
                ("rewinder", "master", {"indexedModeEnabled": "0"}),
                ("rewinder", "mode", "resetAlarm"),
                ("rewinder", "mode", "etoRecovery"),
                ("rewinder", "ready", True),
            ],
            calls,
        )

    def test_pause_idle_retries_transient_wickler_offline_after_release(self):
        runtime = self.build_runtime()

        class FakeWicklerClient:
            attempts: dict[str, int] = {"unwinder": 0, "rewinder": 0}

            def __init__(self, _cfg, role):
                self.role = role

            def available(self):
                return True

            def post_master(self, payload, timeout_s=None):
                return {"ok": True}

            def release_for_continuous_motion(self, timeout_s=None):
                return {"ok": True, "readyMotion": True}

            def fetch_state(self, timeout_s=None):
                FakeWicklerClient.attempts[self.role] += 1
                if FakeWicklerClient.attempts[self.role] == 1:
                    return {
                        "master": {"indexedModeEnabled": False},
                        "telemetry": {
                            "modeLabel": "Offline",
                            "modeCss": "fault",
                            "externalStopActive": False,
                            "indexedModeEnabled": False,
                            "wipePercent": 50.0,
                        },
                        "drive": {
                            "online": False,
                            "ready": False,
                            "move": False,
                            "alarm": False,
                            "continuousModeReady": None,
                            "lastCommandOk": True,
                        },
                        "values": {},
                    }
                return {
                    "master": {"indexedModeEnabled": False},
                    "telemetry": {
                        "modeLabel": "Bereit",
                        "modeCss": "ready",
                        "externalStopActive": False,
                        "indexedModeEnabled": False,
                        "wipePercent": 50.0,
                    },
                    "drive": {
                        "online": True,
                        "ready": True,
                        "move": False,
                        "alarm": False,
                        "continuousModeReady": True,
                        "lastCommandOk": True,
                    },
                    "values": {},
                }

        with patch("mas004_rpi_databridge.machine_runtime.SmartWicklerClient", FakeWicklerClient), patch(
            "mas004_rpi_databridge.machine_runtime.time.sleep"
        ):
            result = runtime._set_production_wicklers_idle(target_state=7)

        self.assertTrue(all(item["ok"] for item in result), result)
        self.assertEqual({"unwinder": 2, "rewinder": 2}, FakeWicklerClient.attempts)
        self.assertTrue(all(item["verify"]["attempt"] == 2 for item in result), result)

    def test_pause_idle_reports_wickler_ready_rejection(self):
        runtime = self.build_runtime()

        class FakeWicklerClient:
            def __init__(self, _cfg, role):
                self.role = role

            def available(self):
                return True

            def post_master(self, _payload, timeout_s=None):
                return {"ok": True}

            def release_for_continuous_motion(self, timeout_s=None):
                return {"ok": False, "error": "stop input active"}

            def fetch_state(self, timeout_s=None):
                return {
                    "master": {"indexedModeEnabled": False},
                    "telemetry": {
                        "modeLabel": "Stop",
                        "modeCss": "stop",
                        "externalStopActive": True,
                        "indexedModeEnabled": False,
                        "wipePercent": 50.0,
                    },
                    "drive": {
                        "online": True,
                        "ready": False,
                        "move": False,
                        "alarm": False,
                        "continuousModeReady": False,
                        "lastCommandOk": True,
                    },
                    "values": {},
                }

        with patch("mas004_rpi_databridge.machine_runtime.SmartWicklerClient", FakeWicklerClient):
            result = runtime._set_production_wicklers_idle(target_state=7)

        self.assertFalse(all(item["ok"] for item in result), result)
        self.assertTrue(any("mode rejected" in ";".join(item.get("errors") or []) for item in result), result)
        self.assertTrue(any("externalStopActive" in item.get("errors", []) for item in result), result)

    def test_start_from_pause_blocks_motion_until_production_runtime_is_released(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=7,
            requested_state=7,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={},
        )
        ok, msg = self.params.set_value("MAS0002", "1", actor="microtom")
        self.assertTrue(ok, msg)

        with patch("mas004_rpi_databridge.machine_runtime.PRODUCTION_START_MOTION_ENABLED", False), patch.object(
            runtime, "_start_production_motion"
        ) as start_motion:
            snapshot = runtime.refresh()

        self.assertFalse(start_motion.called)
        self.assertEqual(7, snapshot["current_state"])
        self.assertEqual(7, snapshot["requested_state"])
        self.assertEqual("0", self.params.get_effective_value("MAS0002"))
        production = snapshot["info"]["production_runtime"]
        self.assertFalse(production["last_start"]["ok"])
        self.assertTrue(production["last_start"]["blocked"])
        self.assertIn("vollstaendige Label", production["last_start"]["reason"])

    def test_start_from_pause_starts_production_runner_after_transition(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=7,
            requested_state=7,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={},
        )
        ok, msg = self.params.set_value("MAS0002", "1", actor="microtom")
        self.assertTrue(ok, msg)

        with patch("mas004_rpi_databridge.machine_runtime.PRODUCTION_START_MOTION_ENABLED", True), patch.object(
            runtime,
            "_start_production_motion",
            return_value={"ok": True, "synced_state": 5, "plan": {"travel_mm": 100.0}},
        ) as start_motion:
            first = runtime.refresh()
            self.assertEqual(4, first["current_state"])
            self.assertFalse(start_motion.called)

            second = runtime.refresh()

        start_motion.assert_called_once()
        self.assertEqual(5, second["current_state"])
        self.assertEqual(5, second["requested_state"])
        self.assertEqual("0", self.params.get_effective_value("MAS0002"))
        production = second["info"]["production_runtime"]
        self.assertTrue(production["active"])
        self.assertEqual(100.0, production["plan"]["travel_mm"])

    def test_start_from_pause_clears_stale_label_pause_error_before_transition(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=7,
            requested_state=7,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={},
        )
        self.params.apply_device_value("MAE0025", "1", promote_default=True)
        ok, msg = self.params.set_value("MAS0002", "1", actor="microtom")
        self.assertTrue(ok, msg)

        with patch("mas004_rpi_databridge.machine_runtime.PRODUCTION_START_MOTION_ENABLED", True), patch.object(
            runtime,
            "_start_production_motion",
            return_value={"ok": True, "synced_state": 5, "plan": {"travel_mm": 100.0}},
        ) as start_motion:
            first = runtime.refresh()
            second = runtime.refresh()

        start_motion.assert_called_once()
        self.assertEqual(4, first["current_state"])
        self.assertEqual(5, second["current_state"])
        self.assertEqual("0", self.params.get_effective_value("MAE0025"))
        self.assertEqual(["MAE0025"], first["info"]["production_runtime"]["pending_start"]["cleared_pause_errors"])

    def test_pause_from_production_requests_controlled_pause_after_print(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=5,
            requested_state=5,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"production_runtime": {"active": True}},
        )
        ok, msg = self.params.set_value("MAS0002", "7", actor="microtom")
        self.assertTrue(ok, msg)

        with patch.object(
            runtime,
            "_pause_production_motion_after_print",
            return_value={"ok": True, "target_state": 6},
        ) as pause_motion, patch.object(runtime, "_stop_production_motion") as stop_motion:
            snapshot = runtime.refresh()

        pause_motion.assert_called_once_with(reason="operator_pause", target_state=6)
        stop_motion.assert_not_called()
        self.assertEqual(6, snapshot["current_state"])
        self.assertFalse(snapshot["info"]["production_runtime"]["active"])
        self.assertTrue(snapshot["info"]["production_runtime"]["paused"])
        self.assertEqual("0", self.params.get_effective_value("MAS0002"))

    def test_pause_from_production_accepts_wickler_bit_lag_without_purge(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=5,
            requested_state=5,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"production_runtime": {"active": True}},
        )
        ok, msg = self.params.set_value("MAS0002", "7", actor="microtom")
        self.assertTrue(ok, msg)
        pause_result = {
            "ok": True,
            "target_state": 6,
            "wicklers_ok": False,
            "controlled": True,
        }

        with patch.object(
            runtime,
            "_pause_production_motion_after_print",
            return_value=pause_result,
        ) as pause_motion, patch.object(runtime, "_stop_production_motion") as stop_motion, patch.object(
            runtime.production_logs,
            "handle_param_change",
        ) as log_change:
            snapshot = runtime.refresh()

        pause_motion.assert_called_once_with(reason="operator_pause", target_state=6)
        stop_motion.assert_not_called()
        log_change.assert_not_called()
        self.assertEqual(6, snapshot["current_state"])
        self.assertEqual(7, snapshot["requested_state"])
        self.assertFalse(snapshot["purge_active"])
        self.assertEqual("0", self.params.get_effective_value("MAS0028"))
        self.assertTrue(snapshot["info"]["production_runtime"]["paused"])
        self.assertEqual(pause_result, snapshot["info"]["production_runtime"]["last_stop"])

    def test_production_stop_verifies_motor3_standstill_after_runner_stop(self):
        runtime = self.build_runtime()
        calls: list[str] = []
        status_replies = [
            'JSON {"motor":{"state":{"move":true,"busy":false,"velocity_mode":true,"target_speed_mm_s":100.0,"feedback_tenths_mm":1200}}}',
            'JSON {"motor":{"state":{"move":false,"busy":false,"velocity_mode":false,"target_speed_mm_s":0.0,"feedback_tenths_mm":1200}}}',
        ]

        def fake_esp(command, read_timeout_s=None, **_kwargs):
            calls.append(command)
            if command == "MOTOR 3 REFRESH":
                return status_replies.pop(0)
            return "ACK"

        with patch.object(runtime, "_production_esp", side_effect=fake_esp), patch.object(
            runtime, "_set_production_wicklers_idle", return_value=[{"ok": True}]
        ), patch.object(runtime, "_sync_esp_machine_state"), patch("mas004_rpi_databridge.machine_runtime.time.sleep"):
            result = runtime._stop_production_motion(reason="test", target_state=7)

        self.assertTrue(result["ok"], result)
        self.assertEqual("PROCESS PRODUCTION STOP", calls[0])
        self.assertEqual("MOTOR 3 MOVE_VEL_MM_S=0", calls[1])
        self.assertGreaterEqual(calls.count("PROCESS PRODUCTION STOP"), 2)
        self.assertGreaterEqual(calls.count("MOTOR 3 MOVE_VEL_MM_S=0"), 2)
        self.assertEqual([True, False], [item["active"] for item in result["motor3_stop"]["snapshots"]])

    def test_production_stop_reports_failure_when_motor3_remains_active(self):
        runtime = self.build_runtime()

        def fake_esp(command, read_timeout_s=None, **_kwargs):
            if command == "MOTOR 3 REFRESH":
                return (
                    'JSON {"motor":{"state":{"move":true,"busy":false,'
                    '"velocity_mode":true,"target_speed_mm_s":100.0,"feedback_tenths_mm":1200}}}'
                )
            return "ACK"

        with patch.object(runtime, "_production_esp", side_effect=fake_esp), patch.object(
            runtime, "_set_production_wicklers_idle", return_value=[{"ok": True}]
        ), patch.object(runtime, "_sync_esp_machine_state"), patch("mas004_rpi_databridge.machine_runtime.time.sleep"):
            result = runtime._stop_production_motion(reason="test", target_state=7)

        self.assertFalse(result["ok"], result)
        self.assertFalse(result["motor3_stop"]["ok"])
        self.assertEqual(5, len(result["motor3_stop"]["snapshots"]))

    def test_production_stop_accepts_stale_velocity_fields_when_motor3_is_idle(self):
        runtime = self.build_runtime()

        def fake_esp(command, read_timeout_s=None, **_kwargs):
            if command == "MOTOR 3 REFRESH":
                return (
                    'JSON {"motor":{"state":{"move":false,"busy":false,'
                    '"velocity_mode":true,"target_speed_mm_s":100.0,"feedback_tenths_mm":1200}}}'
                )
            return "ACK"

        with patch.object(runtime, "_production_esp", side_effect=fake_esp), patch.object(
            runtime, "_set_production_wicklers_idle", return_value=[{"ok": True}]
        ), patch.object(runtime, "_sync_esp_machine_state"), patch("mas004_rpi_databridge.machine_runtime.time.sleep"):
            result = runtime._stop_production_motion(reason="test", target_state=7)

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["motor3_stop"]["ok"])
        self.assertTrue(result["motor3_stop"]["snapshots"][0]["stale_velocity_fields"])

    def test_production_pause_accepts_wickler_warning_when_motion_is_safe(self):
        runtime = self.build_runtime()

        with patch.object(runtime, "_production_esp", return_value="ACK"), patch.object(
            runtime,
            "_verify_motor3_stopped_after_production_stop",
            return_value={"ok": True},
        ), patch.object(
            runtime,
            "_set_production_wicklers_idle",
            return_value=[{"role": "unwinder", "ok": False, "errors": ["indexedModeEnabled=true"]}],
        ), patch.object(runtime, "_sync_esp_machine_state"), patch.object(
            runtime,
            "_queue_tto_printer_state_sync",
            return_value={"ok": True, "skipped": "test"},
        ):
            result = runtime._stop_production_motion(reason="operator_pause", target_state=6)

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["motion_safe"])
        self.assertFalse(result["wicklers_ok"])
        self.assertTrue(result["accepted_wickler_warning"])

    def test_controlled_pause_after_print_does_not_hard_stop_runner(self):
        runtime = self.build_runtime()
        calls: list[str] = []
        monitor_replies = [
            {
                "active": True,
                "running": True,
                "phase": "registering",
                "reason": "",
                "label_no": 12,
                "labels_printed": 11,
            },
            {
                "active": False,
                "running": False,
                "phase": "idle",
                "reason": "operator_pause",
                "label_no": 0,
                "labels_printed": 12,
            },
        ]

        def fake_esp(command, read_timeout_s=None, **_kwargs):
            calls.append(command)
            if command.startswith("PROCESS PRODUCTION PAUSE_AFTER_PRINT"):
                return 'JSON {"ok":true,"pause_requested":true}'
            return "ACK"

        with patch.object(runtime, "_production_esp", side_effect=fake_esp), patch.object(
            runtime,
            "_read_production_monitor_diag",
            side_effect=monitor_replies,
        ), patch.object(runtime, "_set_production_wicklers_idle", return_value=[{"ok": True}]), patch.object(
            runtime, "_sync_esp_machine_state"
        ) as sync_state, patch.object(
            runtime, "_queue_tto_printer_state_sync", return_value={"ok": True, "skipped": "test"}
        ), patch("mas004_rpi_databridge.machine_runtime.time.sleep"):
            result = runtime._pause_production_motion_after_print(reason="operator_pause", target_state=7)

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["controlled"])
        self.assertTrue(result["monitor_ok"])
        self.assertTrue(calls[0].startswith("PROCESS PRODUCTION PAUSE_AFTER_PRINT"))
        self.assertNotIn("PROCESS PRODUCTION STOP", calls)
        self.assertNotIn("MOTOR 3 MOVE_VEL_MM_S=0", calls)
        sync_state.assert_called_once_with(7, required=False)

    def test_failed_production_stop_forces_fault_state(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=5,
            requested_state=5,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"production_runtime": {"active": True}},
        )
        ok, msg = self.params.set_value("MAS0002", "7", actor="microtom")
        self.assertTrue(ok, msg)

        with patch.object(
            runtime,
            "_pause_production_motion_after_print",
            return_value={"ok": False, "target_state": 6, "motor3_stop": {"ok": False}},
        ):
            snapshot = runtime.refresh()

        self.assertEqual(21, snapshot["current_state"])
        self.assertEqual(21, snapshot["requested_state"])
        self.assertEqual("1", self.params.get_effective_value("MAS0028"))
        self.assertFalse(snapshot["info"]["production_runtime"]["active"])

    def test_production_start_uses_production_runner_command(self):
        runtime = self.build_runtime()
        param_map = runtime._param_values_by_prefix(("MAP", "MAS", "MAE", "MAW"))
        format_plan = runtime.snapshot()["info"].get("format_plan") or {}
        if not format_plan:
            format_plan = {
                "label": {"length_tenths_mm": 1000},
            }

        with patch("mas004_rpi_databridge.machine_runtime.time.sleep"):
            result = runtime._start_production_motion(param_map, format_plan)

        self.assertTrue(result["ok"], result)
        self.assertEqual(
            "PROCESS PRODUCTION START SPEED_MM_S=100.000 RAMP_MM_S2=300.000",
            result["command"],
        )
        self.assertEqual("MOTOR 3 SET_POSITION_MM=0.000", result["motor3_zero"]["command"])
        self.assertEqual(5, result["synced_state"])
        self.assertTrue(result["post_start_wicklers"]["ok"], result)

    def test_production_start_after_operator_pause_uses_resume_without_reset(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=7,
            requested_state=7,
            state_source="operator_pause",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=12,
            info={
                PRODUCTION_RUNTIME_INFO_KEY: {
                    "active": False,
                    "paused": True,
                    "pause_reason": "operator_pause",
                    "last_start": {"ok": True, "started_ts": now_ts() - 60.0},
                    "last_stop": {"ok": True, "reason": "operator_pause", "finished_ts": now_ts() - 1.0},
                }
            },
        )
        param_map = runtime._param_values_by_prefix(("MAP", "MAS", "MAE", "MAW"))
        format_plan = runtime.snapshot()["info"].get("format_plan") or {"label": {"length_tenths_mm": 1000}}

        with patch("mas004_rpi_databridge.machine_runtime.time.sleep"):
            result = runtime._start_production_motion(param_map, format_plan)

        self.assertTrue(result["ok"], result)
        self.assertEqual("pause", result["resume"])
        self.assertEqual("operator_pause", result["pause_reason"])
        self.assertIn("PROCESS PRODUCTION RESUME SPEED_MM_S=100.000 RAMP_MM_S2=300.000", result["command"])
        self.assertNotIn("motor3_zero", result)

    def test_production_start_after_label_removal_uses_resume_without_reset(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=7,
            requested_state=7,
            state_source="label_removal_required",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=9,
            info={
                PRODUCTION_RUNTIME_INFO_KEY: {
                    "active": False,
                    "paused": True,
                    "pause_reason": "label_removal_required:6,9",
                    "label_removal_pending_labels": [6, 9],
                    "label_removal_request": {"label_no": 6, "label_nos": [6, 9]},
                    "last_start": {"ok": True, "started_ts": now_ts() - 60.0},
                    "last_stop": {"reason": "label_removal_required:6", "finished_ts": now_ts() - 1.0},
                }
            },
        )
        param_map = runtime._param_values_by_prefix(("MAP", "MAS", "MAE", "MAW"))
        format_plan = runtime.snapshot()["info"].get("format_plan") or {"label": {"length_tenths_mm": 1000}}

        with patch("mas004_rpi_databridge.machine_runtime.time.sleep"):
            result = runtime._start_production_motion(param_map, format_plan)

        self.assertTrue(result["ok"], result)
        self.assertEqual("label_removal", result["resume"])
        self.assertEqual([6, 9], result["labels_expected_removed"])
        self.assertIn("PROCESS PRODUCTION RESUME_REMOVED LABELS=6,9", result["command"])
        self.assertNotIn("motor3_zero", result)

    def test_production_start_clears_stale_label_removal_when_esp_register_is_empty(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=7,
            requested_state=7,
            state_source="label_removal_required",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=3,
            info={
                PRODUCTION_RUNTIME_INFO_KEY: {
                    "active": False,
                    "paused": True,
                    "pause_reason": "label_removal_required:3",
                    "label_removal_pending_labels": [3],
                    "label_removal_request": {"label_no": 3},
                    "last_start": {"ok": True, "started_ts": now_ts() - 60.0},
                    "last_stop": {"reason": "label_removal_required:3", "finished_ts": now_ts() - 1.0},
                }
            },
        )
        param_map = runtime._param_values_by_prefix(("MAP", "MAS", "MAE", "MAW"))
        format_plan = runtime.snapshot()["info"].get("format_plan") or {"label": {"length_tenths_mm": 1000}}

        with patch.object(
            runtime,
            "_validate_label_removal_resume_on_esp",
            return_value={
                "ok": True,
                "valid": False,
                "stale": True,
                "labels": [3],
                "labels_active": 0,
                "active_labels": [],
                "missing": [3],
                "not_pending": [],
            },
        ), patch("mas004_rpi_databridge.machine_runtime.time.sleep"):
            result = runtime._start_production_motion(param_map, format_plan)

        self.assertTrue(result["ok"], result)
        self.assertIn("PROCESS PRODUCTION START", result["command"])
        self.assertNotIn("RESUME_REMOVED", result["command"])
        self.assertIn("motor3_zero", result)
        cleared = result.get("cleared_label_removal_state") or {}
        self.assertEqual("esp_register_missing_before_start", cleared.get("reason"))
        self.assertEqual([3], cleared.get("labels"))

    def test_production_start_blocks_laser_when_laser_ready_low_before_state_sync(self):
        runtime = self.build_runtime()
        param_map = runtime._param_values_by_prefix(("MAP", "MAS", "MAE", "MAW"))
        param_map["MAP0016"] = "1"
        self.io_store.upsert_value("esp32_plc58__I0_12", "1", "simulation", "test")
        self.io_store.upsert_value("esp32_plc58__I0_2", "0", "simulation", "test")
        format_plan = runtime.snapshot()["info"].get("format_plan") or {"label": {"length_tenths_mm": 1000}}

        with patch.object(runtime, "_sync_esp_machine_state") as sync_state:
            result = runtime._start_production_motion(param_map, format_plan)

        self.assertFalse(result["ok"], result)
        self.assertEqual(0, result["synced_state"])
        self.assertIn("Laser Ready", result["error"])
        sync_state.assert_not_called()

    def test_production_param_sync_forces_confirmed_start_values(self):
        runtime = self.build_runtime()
        param_map = runtime._param_values_by_prefix(("MAP", "MAS", "MAE", "MAW"))
        previous_values = runtime._production_esp_sync_values(param_map)
        dedupe = ValueDedupeStore(self.db)
        for key, value in previous_values.items():
            dedupe.remember("esp-production-sync", key, value)

        with patch.object(runtime, "_production_esp", return_value="ACK") as esp:
            result = runtime._sync_production_params_to_esp(
                param_map,
                previous_values=previous_values,
            )

        commands = [call.args[0] for call in esp.call_args_list]
        expected_forced = [
            "MAP0004",
            "MAP0006",
            "MAP0011",
            "MAP0012",
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
            "MAP0066",
            "MAP0071",
            "MAP0075",
            "MAP0079",
        ]
        for key in expected_forced:
            self.assertIn(f"SYNC {key}={previous_values[key]}", commands)
        self.assertEqual(expected_forced, result["synced"])
        self.assertEqual(expected_forced, result["forced"])
        self.assertEqual([], result["readback_skipped"])
        self.assertNotIn("MAP0068", result["values"])
        self.assertNotIn("MAP0068", result["skipped"])
        self.assertEqual(previous_values, result["values"])

    def test_production_param_sync_forces_tto_bypass_readback_values(self):
        runtime = self.build_runtime()
        param_map = runtime._param_values_by_prefix(("MAP", "MAS", "MAE", "MAW"))
        previous_values = runtime._production_esp_sync_values(param_map)
        param_map.update({"MAP0035": "1", "MAP0069": "1500", "MAP0070": "2000"})

        with patch.object(runtime, "_production_esp", return_value="ACK") as esp:
            result = runtime._sync_production_params_to_esp(
                param_map,
                previous_values=previous_values,
            )

        commands = [call.args[0] for call in esp.call_args_list]
        self.assertIn("SYNC MAP0035=1", commands)
        self.assertIn("SYNC MAP0069=1500", commands)
        self.assertIn("SYNC MAP0070=2000", commands)
        self.assertIn("MAP0035", result["forced"])
        self.assertIn("MAP0069", result["forced"])
        self.assertIn("MAP0070", result["forced"])

    def test_production_param_sync_rejects_esp_readback_mismatch(self):
        self.cfg.esp_simulation = False
        runtime = self.build_runtime()
        param_map = runtime._param_values_by_prefix(("MAP", "MAS", "MAE", "MAW"))
        param_map["MAP0019"] = "6000"

        def fake_esp(command, read_timeout_s=None, **_kwargs):
            if command.startswith("SYNC "):
                return "ACK"
            if command == "MAP0019=?":
                return "MAP0019=11000"
            if command.endswith("=?"):
                key = command[:-2]
                return f"{key}={param_map[key]}"
            return "ACK"

        with patch.object(runtime, "_production_esp", side_effect=fake_esp):
            with self.assertRaisesRegex(RuntimeError, "MAP0019 expected 6000 got 11000"):
                runtime._sync_production_params_to_esp(param_map, previous_values={})

    def test_wickler_ready_timeout_after_ready_event_is_ignored(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=5,
            requested_state=5,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={PRODUCTION_RUNTIME_INFO_KEY: {"active": True}},
        )
        runtime.handle_event({"type": "production_wickler_indexed_ready", "label_no": 1})

        result = runtime.handle_event(
            {
                "type": "production_fault",
                "fault": "wickler_indexed_ready_timeout",
                "label_no": 1,
                "timeout_ms": 30000,
            }
        )

        self.assertEqual("wickler_ready_already_seen", result["result"]["ignored"])
        events = runtime._recent_events(limit=3)
        self.assertEqual("production_fault_ignored", events[0]["event_type"])

    def test_wickler_ready_timeout_after_failed_ready_attempt_is_not_ignored(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=5,
            requested_state=5,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={
                PRODUCTION_RUNTIME_INFO_KEY: {
                    "active": True,
                    "first_print_wickler_ready_attempt": {
                        "label_no": 1,
                        "ts": now_ts(),
                        "wickler_takt_ok": True,
                        "esp_ready_ok": False,
                    },
                }
            },
        )

        result = runtime.handle_event(
            {
                "type": "production_fault",
                "fault": "wickler_indexed_ready_timeout",
                "label_no": 1,
                "timeout_ms": 30000,
            }
        )

        self.assertNotIn("ignored", result["result"])
        events = runtime._recent_events(limit=3)
        self.assertEqual("production_fault", events[0]["event_type"])

    def test_production_param_sync_includes_simulation_values_only_when_bypass_active(self):
        runtime = self.build_runtime()
        param_map = runtime._param_values_by_prefix(("MAP", "MAS", "MAE", "MAW"))
        param_map.update(
            {
                "MAP0035": "0",
                "MAP0036": "0",
                "MAP0037": "0",
                "MAP0067": "3",
                "MAP0068": "4",
                "MAP0069": "1500",
                "MAP0070": "2000",
            }
        )

        inactive = runtime._production_esp_sync_values(param_map)
        self.assertNotIn("MAP0067", inactive)
        self.assertNotIn("MAP0068", inactive)
        self.assertNotIn("MAP0069", inactive)
        self.assertNotIn("MAP0070", inactive)
        self.assertEqual("0", inactive["MAP0079"])

        param_map.update({"MAP0035": "1", "MAP0036": "1", "MAP0037": "1"})
        active = runtime._production_esp_sync_values(param_map)
        self.assertEqual("3", active["MAP0067"])
        self.assertEqual("4", active["MAP0068"])
        self.assertEqual("1500", active["MAP0069"])
        self.assertEqual("2000", active["MAP0070"])
        self.assertEqual("0", active["MAP0079"])

    def test_tto_printer_syncs_online_and_offline_for_real_tto_without_bypass(self):
        self.cfg.vj6530_simulation = False
        runtime = self.build_runtime()
        _insert_param(self.db, "TTS0001", "TTS", "0001", "0", "R", "W", "enum")
        _insert_device_map(self.db, "TTS0001", "STATUS[PRINTER_STATE_CODE]")
        param_map = {"MAP0016": "0", "MAP0035": "0"}

        with patch("mas004_rpi_databridge.machine_runtime.DeviceBridge") as bridge_cls:
            bridge = bridge_cls.return_value
            bridge.execute.side_effect = ["ACK_TTS0001=3", "ACK_TTS0001=0"]

            online = runtime._sync_tto_printer_for_machine_state(
                5,
                param_map,
                reason="test_start",
                required=True,
            )
            self.params.apply_device_value("TTS0001", "3", promote_default=True)
            offline = runtime._sync_tto_printer_for_machine_state(
                7,
                param_map,
                reason="test_stop",
                required=True,
            )

        self.assertTrue(online["ok"], online)
        self.assertEqual("3", online["actual_code"])
        self.assertTrue(offline["ok"], offline)
        self.assertEqual("0", offline["actual_code"])
        self.assertEqual(
            [
                ("vj6530", "TTS0001", "TTS", "write", "3"),
                ("vj6530", "TTS0001", "TTS", "write", "0"),
            ],
            [call.args for call in bridge.execute.call_args_list],
        )
        self.assertEqual(
            [{"actor": "esp32"}, {"actor": "esp32"}],
            [call.kwargs for call in bridge.execute.call_args_list],
        )

    def test_tto_printer_sync_skips_when_tto_bypass_is_active(self):
        self.cfg.vj6530_simulation = False
        runtime = self.build_runtime()
        _insert_param(self.db, "TTS0001", "TTS", "0001", "0", "R", "W", "enum")
        _insert_device_map(self.db, "TTS0001", "STATUS[PRINTER_STATE_CODE]")

        with patch("mas004_rpi_databridge.machine_runtime.DeviceBridge") as bridge_cls:
            result = runtime._sync_tto_printer_for_machine_state(
                5,
                {"MAP0016": "0", "MAP0035": "1"},
                reason="test_bypass",
                required=True,
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual("tto_print_bypass_active", result["skipped"])
        bridge_cls.assert_not_called()

    def test_tto_printer_online_pulses_laser_start_in_parallel_mode(self):
        self.cfg.vj6530_simulation = False
        runtime = self.build_runtime()
        _insert_param(self.db, "TTS0001", "TTS", "0001", "0", "R", "W", "enum")
        _insert_device_map(self.db, "TTS0001", "STATUS[PRINTER_STATE_CODE]")
        self.io_store.upsert_value("esp32_plc58__I0_2", "1", "simulation", "test")
        pulses: list[tuple[str, float, str]] = []

        def fake_pulse(io_key, *, high_s, source):
            pulses.append((io_key, float(high_s), source))
            return {"ok": True, "io_key": io_key, "high_s": high_s}

        with patch("mas004_rpi_databridge.machine_runtime.DeviceBridge") as bridge_cls, patch.object(
            runtime,
            "_pulse_io_output",
            side_effect=fake_pulse,
        ):
            bridge_cls.return_value.execute.side_effect = ["ACK_TTS0001=3", "ACK_TTS0001=0"]
            result = runtime._sync_tto_printer_for_machine_state(
                5,
                {"MAP0016": "0", "MAP0035": "0", "MAP0079": "1"},
                reason="test_parallel",
                required=True,
            )
            self.params.apply_device_value("TTS0001", "3", promote_default=True)
            offline = runtime._sync_tto_printer_for_machine_state(
                7,
                {"MAP0016": "0", "MAP0035": "0", "MAP0079": "1"},
                reason="test_parallel_offline",
                required=True,
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual("3", result["actual_code"])
        self.assertTrue(offline["ok"], offline)
        self.assertEqual("0", offline["actual_code"])
        self.assertEqual(
            [
                ("esp32_plc58__Q0_3", 0.1, "laser-parallel-tto-state-3"),
                ("esp32_plc58__Q0_3", 0.1, "laser-parallel-tto-state-0"),
            ],
            pulses,
        )
        self.assertTrue(result["laser_parallel_start"]["ok"])
        self.assertTrue(offline["laser_parallel_start"]["ok"])

    def test_tto_printer_online_blocks_laser_parallel_when_laser_ready_low(self):
        self.cfg.vj6530_simulation = False
        runtime = self.build_runtime()
        _insert_param(self.db, "TTS0001", "TTS", "0001", "0", "R", "W", "enum")
        _insert_device_map(self.db, "TTS0001", "STATUS[PRINTER_STATE_CODE]")
        self.io_store.upsert_value("esp32_plc58__I0_2", "0", "simulation", "test")

        with patch("mas004_rpi_databridge.machine_runtime.DeviceBridge") as bridge_cls, patch.object(
            runtime,
            "_pulse_io_output",
        ) as pulse:
            bridge_cls.return_value.execute.return_value = "ACK_TTS0001=3"
            with self.assertRaisesRegex(RuntimeError, "Laser Ready"):
                runtime._sync_tto_printer_for_machine_state(
                    5,
                    {"MAP0016": "0", "MAP0035": "0", "MAP0079": "1"},
                    reason="test_parallel",
                    required=True,
                )

        pulse.assert_not_called()

    def test_motor3_production_zero_falls_back_to_status_after_empty_refresh(self):
        self.cfg.esp_simulation = False
        runtime = self.build_runtime()
        calls: list[str] = []

        def fake_esp(command, read_timeout_s=None, **_kwargs):
            calls.append(command)
            if command == "MOTOR 3 SET_POSITION_MM=0.000":
                return "ACK_SET_POSITION_MM"
            if command == "MOTOR 3 REFRESH":
                return ""
            if command == "MOTOR 3 STATUS?":
                return (
                    'JSON {"motor":{"state":{"ready":true,"move":false,'
                    '"alarm":false,"feedback_tenths_mm":0}}}'
                )
            return "ACK"

        with patch.object(runtime, "_production_esp", side_effect=fake_esp), patch(
            "mas004_rpi_databridge.machine_runtime.time.sleep"
        ):
            result = runtime._zero_motor3_for_production_start()

        self.assertTrue(result["ok"], result)
        self.assertEqual("MOTOR 3 STATUS?", result["status_command"])
        self.assertEqual(0.0, result["feedback_tenths_mm"])
        self.assertIn("MOTOR 3 REFRESH", calls)
        self.assertIn("MOTOR 3 STATUS?", calls)

    def test_production_start_rechecks_wicklers_after_motor_start(self):
        runtime = self.build_runtime()
        param_map = runtime._param_values_by_prefix(("MAP", "MAS", "MAE", "MAW"))
        format_plan = {"label": {"length_tenths_mm": 1000}}
        fetch_calls: list[str] = []
        master_payloads: list[dict[str, str]] = []

        class FakeWicklerClient:
            def __init__(self, _cfg, role):
                self.role = role
                self.descriptor = SimpleNamespace(label=role, simulation_attr=f"{role}_simulation")

            def available(self):
                return True

            def post_master(self, payload, timeout_s=None):
                master_payloads.append(dict(payload))
                return {"ok": True}

            def release_for_continuous_motion(self, timeout_s=None):
                return {"ok": True, "readyMotion": True}

            def fetch_state(self, timeout_s=None):
                fetch_calls.append(self.role)
                return {
                    "master": {"indexedModeEnabled": False},
                    "telemetry": {
                        "modeLabel": "Bereit",
                        "modeCss": "ready",
                        "externalStopActive": False,
                        "indexedModeEnabled": False,
                        "indexedCommandSeq": 0,
                        "wipePercent": 50.0,
                    },
                    "drive": {
                        "online": True,
                        "ready": True,
                        "move": False,
                        "alarm": False,
                        "continuousModeReady": True,
                        "lastCommandOk": True,
                    },
                    "values": {},
                }

        with patch("mas004_rpi_databridge.machine_runtime.SmartWicklerClient", FakeWicklerClient), patch(
            "mas004_rpi_databridge.machine_runtime.time.sleep"
        ):
            result = runtime._start_production_motion(param_map, format_plan)

        self.assertTrue(result["ok"], result)
        self.assertEqual(["unwinder", "rewinder", "unwinder", "rewinder"], fetch_calls)
        self.assertEqual(["0", "0"], [payload.get("indexedModeEnabled") for payload in master_payloads])
        self.assertTrue(all("indexedTravelMm" not in payload for payload in master_payloads))

    def test_indexed_wickler_prepare_does_not_release_motion(self):
        runtime = self.build_runtime()
        release_calls: list[str] = []
        master_payloads: list[dict[str, str]] = []
        plan = {
            "speed_mm_s": 100.0,
            "ramp_mm_s2": 300.0,
            "wickler_standby_percent": 50.0,
            "travel_mm": 100.0,
        }

        class FakeWicklerClient:
            def __init__(self, _cfg, role):
                self.role = role
                self.descriptor = SimpleNamespace(label=role, simulation_attr=f"{role}_simulation")

            def available(self):
                return True

            def post_master(self, payload, timeout_s=None):
                master_payloads.append(dict(payload))
                return {"ok": True}

            def release_for_continuous_motion(self, timeout_s=None):
                release_calls.append(self.role)
                return {"ok": True, "readyMotion": True}

            def fetch_state(self, timeout_s=None):
                return {
                    "master": {"indexedModeEnabled": True},
                    "telemetry": {
                        "modeLabel": "Bereit",
                        "modeCss": "ready",
                        "externalStopActive": False,
                        "indexedModeEnabled": True,
                        "indexedMoveActive": False,
                        "indexedCommandSeq": 1,
                        "wipePercent": 50.0,
                        "calibrated": True,
                        "requiresCalibration": False,
                    },
                    "drive": {
                        "online": True,
                        "ready": True,
                        "move": False,
                        "alarm": False,
                        "continuousModeReady": True,
                        "lastCommandOk": True,
                    },
                    "values": {},
                }

        with patch("mas004_rpi_databridge.machine_runtime.SmartWicklerClient", FakeWicklerClient):
            result = runtime._prepare_production_wicklers(
                plan,
                travel_mm=104.6,
                reason="test_after_print",
                timeout_s=0.2,
            )

        self.assertTrue(all(item["ok"] for item in result), result)
        self.assertEqual([], release_calls)
        self.assertEqual(["1", "1"], [payload.get("indexedModeEnabled") for payload in master_payloads])
        self.assertEqual(["104.600", "104.600"], [payload.get("indexedTravelMm") for payload in master_payloads])
        self.assertTrue(all(item["ready"]["unchanged"] for item in result))

    def test_production_motion_plan_uses_label_length_compensation_for_wickler_travel(self):
        runtime = self.build_runtime()

        plan = runtime._production_motion_plan(
            {"MAP0002": "1000", "MAP0076": "48", "MAP0014": "100"},
            {
                "label": {"length_tenths_mm": 1000},
                "printer": {"stop_distance_tenths_mm": 6200},
                "process": {"label_length_compensation_tenths_mm": 48},
            },
        )

        self.assertAlmostEqual(100.0, plan["nominal_travel_mm"])
        self.assertAlmostEqual(4.8, plan["label_length_compensation_mm"])
        self.assertAlmostEqual(104.8, plan["travel_mm"])

    def test_production_wickler_base_prefers_last_esp_position_command(self):
        runtime = self.build_runtime()
        self.mark_production_active(runtime)
        plan = {"travel_mm": 104.8}

        before_mm, before_source = runtime._production_wickler_base_travel(plan)
        runtime._remember_production_wickler_observed_travel(
            label_no=2,
            remaining_mm=104.782,
            payload={"target_abs_mm": 724.976, "speed_mm_s": 100.0, "ramp_mm_s2": 300.0},
        )
        after_mm, after_source = runtime._production_wickler_base_travel(plan)

        self.assertAlmostEqual(104.8, before_mm)
        self.assertEqual("format_plan_map0002_plus_map0076", before_source)
        self.assertAlmostEqual(104.782, after_mm)
        self.assertEqual("last_esp_remaining_label_2", after_source)

    def test_print_position_commanded_reprepares_wicklers_with_esp_remaining(self):
        runtime = self.build_runtime()
        self.mark_production_active(runtime)
        runtime._prepare_next_production_wickler_takt = Mock(return_value={"ok": True, "prepared": True})

        payload = {
            "type": "production_print_position_commanded",
            "label_no": 2,
            "target_abs_mm": 723.710,
            "remaining_mm": 104.070,
            "speed_mm_s": 100.0,
            "ramp_mm_s2": 300.0,
        }

        first = runtime.handle_event(dict(payload))
        duplicate = runtime.handle_event(dict(payload))
        base_mm, base_source = runtime._production_wickler_base_travel({"travel_mm": 100.8})

        self.assertTrue(first["recorded"])
        self.assertTrue(first["wickler_reprepare"]["ok"])
        self.assertTrue(duplicate["deduped"])
        self.assertEqual("duplicate_or_invalid_commanded_position", duplicate["wickler_reprepare"]["skipped"])
        runtime._prepare_next_production_wickler_takt.assert_called_once_with(
            label_no=2,
            reason="print_position_commanded_remaining_mm",
        )
        self.assertAlmostEqual(104.070, base_mm)
        self.assertEqual("last_esp_remaining_label_2", base_source)

    def test_production_start_fails_if_post_start_wickler_leaves_ready(self):
        runtime = self.build_runtime()
        param_map = runtime._param_values_by_prefix(("MAP", "MAS", "MAE", "MAW"))
        format_plan = {"label": {"length_tenths_mm": 1000}}
        fetch_count = {"value": 0}

        class FakeWicklerClient:
            def __init__(self, _cfg, role):
                self.role = role
                self.descriptor = SimpleNamespace(label=role, simulation_attr=f"{role}_simulation")

            def available(self):
                return True

            def post_master(self, payload, timeout_s=None):
                return {"ok": True}

            def release_for_continuous_motion(self, timeout_s=None):
                return {"ok": True, "readyMotion": True}

            def post_mode(self, mode, timeout_s=None):
                return {"ok": True, "mode": mode}

            def fetch_state(self, timeout_s=None):
                fetch_count["value"] += 1
                ready = fetch_count["value"] <= 2
                return {
                    "master": {"indexedModeEnabled": False},
                    "telemetry": {
                        "modeLabel": "Bereit" if ready else "Stop",
                        "modeCss": "ready" if ready else "stop",
                        "externalStopActive": not ready,
                        "indexedModeEnabled": False,
                        "indexedCommandSeq": 0,
                        "wipePercent": 50.0,
                    },
                    "drive": {
                        "online": True,
                        "ready": ready,
                        "move": False,
                        "alarm": False,
                        "continuousModeReady": ready,
                        "lastCommandOk": True,
                    },
                    "values": {},
                }

        with patch("mas004_rpi_databridge.machine_runtime.SmartWicklerClient", FakeWicklerClient), patch(
            "mas004_rpi_databridge.machine_runtime.time.sleep"
        ):
            result = runtime._start_production_motion(param_map, format_plan)

        self.assertFalse(result["ok"], result)
        self.assertIn("Wickler nach Produktionsstart nicht stabil", result["error"])

    def test_production_wickler_requires_calibrated_dancer_position(self):
        runtime = self.build_runtime()

        result = runtime._verify_wickler_production_state(
            "unwinder",
            {
                "telemetry": {
                    "modeLabel": "Bereit",
                    "modeCss": "ready",
                    "wipePercent": 50.0,
                    "calibrated": False,
                    "requiresCalibration": True,
                },
                "drive": {
                    "alarm": False,
                    "online": True,
                    "continuousModeReady": True,
                    "lastCommandOk": True,
                },
                "values": {},
                "master": {"indexedModeEnabled": False},
            },
        )

        self.assertFalse(result["ok"], result)
        self.assertIn("Wippe nicht eingemessen", result["errors"])
        self.assertIn("MAE0028", result["mae_keys"])

    def test_production_wickler_http_failure_is_only_communication_error(self):
        runtime = self.build_runtime()

        result = runtime._verify_wickler_production_state(
            "rewinder",
            {
                "device": {"reachable": False, "simulation": False, "error": "timed out"},
                "telemetry": {
                    "modeLabel": "Offline",
                    "modeCss": "fault",
                    "wipePercent": 0.0,
                    "calibrated": False,
                    "requiresCalibration": True,
                    "externalStopActive": True,
                    "indexedModeEnabled": False,
                    "indexedCommandSeq": 0,
                },
                "drive": {
                    "alarm": True,
                    "alarmCode": 7,
                    "online": False,
                    "continuousModeReady": False,
                    "lastCommandOk": False,
                },
                "values": {"maeLow": True, "maeHigh": True, "maeBlocked": True},
                "master": {"indexedModeEnabled": False},
            },
            require_indexed_mode=True,
        )

        self.assertFalse(result["ok"], result)
        self.assertTrue(result["communication_error"])
        self.assertEqual(["communication_error: timed out"], result["errors"])
        self.assertEqual([], result["mae_keys"])

    def test_production_wickler_monitor_tolerates_short_communication_gap(self):
        runtime = self.build_runtime()
        runtime._stop_production_motion = Mock(return_value={"ok": True})
        runtime._notify_microtom = Mock()

        def comm_monitor(**_kwargs):
            return {
                "ok": False,
                "results": [
                    {"role": "unwinder", "ok": True, "verify": {"ok": True}},
                    {
                        "role": "rewinder",
                        "ok": False,
                        "verify": {
                            "role": "rewinder",
                            "ok": False,
                            "errors": ["communication_error: timed out"],
                            "communication_error": True,
                            "device_reachable": False,
                            "device_error": "timed out",
                            "mae_keys": [],
                        },
                    },
                ],
                "errors": ["Aufwickler: communication_error: timed out"],
            }

        runtime._production_wickler_verifications = Mock(side_effect=comm_monitor)
        production_info: dict[str, object] = {}

        result = runtime._monitor_active_production_wicklers(production_info, 100.0)

        self.assertIsNone(result)
        self.assertEqual("0", self.params.get_effective_value("MAS0028"))
        self.assertEqual(1, production_info["wickler_monitor_pending_fault"]["count"])
        runtime._stop_production_motion.assert_not_called()
        runtime._notify_microtom.assert_not_called()

    def test_production_wickler_monitor_stops_after_repeated_communication_gap(self):
        runtime = self.build_runtime()
        runtime._stop_production_motion = Mock(return_value={"ok": True})
        runtime._notify_microtom = Mock()

        def comm_monitor(**_kwargs):
            return {
                "ok": False,
                "results": [
                    {
                        "role": "rewinder",
                        "ok": False,
                        "verify": {
                            "role": "rewinder",
                            "ok": False,
                            "errors": ["communication_error: timed out"],
                            "communication_error": True,
                            "device_reachable": False,
                            "device_error": "timed out",
                            "mae_keys": [],
                        },
                    }
                ],
                "errors": ["Aufwickler: communication_error: timed out"],
            }

        runtime._production_wickler_verifications = Mock(side_effect=comm_monitor)
        production_info: dict[str, object] = {}

        self.assertIsNone(runtime._monitor_active_production_wicklers(production_info, 100.0))
        self.assertIsNone(runtime._monitor_active_production_wicklers(production_info, 100.6))
        result = runtime._monitor_active_production_wicklers(production_info, 101.2)

        self.assertIsNotNone(result)
        self.assertTrue(result["communication_only"])
        self.assertEqual(PRODUCTION_WICKLER_MONITOR_COMM_MAX_MISSES, result["consecutive_failures"])
        self.assertEqual("1", self.params.get_effective_value("MAS0028"))
        runtime._stop_production_motion.assert_called_once_with(
            reason="production_wickler_monitor_failed",
            target_state=21,
        )
        runtime._notify_microtom.assert_called_once_with("MAS0028", "1", dedupe_key="machine:MAS0028")

    def test_production_wickler_monitor_stops_immediately_on_hard_fault(self):
        runtime = self.build_runtime()
        runtime._stop_production_motion = Mock(return_value={"ok": True})
        runtime._notify_microtom = Mock()
        runtime._production_wickler_verifications = Mock(
            return_value={
                "ok": False,
                "results": [
                    {
                        "role": "rewinder",
                        "ok": False,
                        "verify": {
                            "role": "rewinder",
                            "ok": False,
                            "errors": ["drive alarm 7"],
                            "communication_error": False,
                            "mae_keys": [],
                        },
                    }
                ],
                "errors": ["Aufwickler: drive alarm 7"],
            }
        )
        production_info: dict[str, object] = {}

        result = runtime._monitor_active_production_wicklers(production_info, 100.0)

        self.assertIsNotNone(result)
        self.assertNotIn("wickler_monitor_pending_fault", production_info)
        self.assertEqual("1", self.params.get_effective_value("MAS0028"))
        runtime._stop_production_motion.assert_called_once()

    def test_setup_positions_format_axes_as_one_set_before_wickler_measurement(self):
        class FakeEspMotorClient:
            def __init__(self, _cfg):
                self.position_tenths = {}
                self.moves = []

            def available(self):
                return True

            def config(self, motor_id):
                return {
                    "config": {
                        "min_enabled": True,
                        "max_enabled": True,
                        "min_tenths_mm": -10000,
                        "max_tenths_mm": 10000,
                    }
                }

            def refresh(self, motor_id):
                return {
                    "motor": {
                        "state": {
                            "feedback_tenths_mm": self.position_tenths.get(int(motor_id), 0),
                            "move": False,
                            "busy": False,
                            "alarm": False,
                            "hwto": False,
                        }
                    }
                }

            def reset_alarm(self, motor_id):
                return {"ok": True, "reply": "ACK"}

            def recover_eto_motor(self, motor_id):
                return {"ok": True, "reply": "ACK"}

            def move_absolute_set_mm(self, targets_mm):
                for motor_id, absolute_mm in sorted(targets_mm.items()):
                    motor_id = int(motor_id)
                    self.position_tenths[motor_id] = int(round(float(absolute_mm) * 10.0))
                    self.moves.append((motor_id, round(float(absolute_mm), 3)))
                return {"ok": True, "results": [{"ok": True, "id": int(motor_id)} for motor_id in sorted(targets_mm)]}

        self.cfg.esp_simulation = False
        for pkey, default_v in (
            ("MAP0008", "23"),
            ("MAP0009", "24"),
            ("MAP0010", "48"),
            ("MAP0029", "2"),
            ("MAP0030", "3"),
            ("MAP0031", "20"),
            ("MAP0032", "30"),
            ("MAP0033", "4"),
        ):
            _insert_param(self.db, pkey, "MAP", pkey[-4:], default_v, "W", "R", "uint16")
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=9,
            requested_state=9,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={},
        )
        ok, msg = self.params.set_value("MAS0002", "3", actor="microtom")
        self.assertTrue(ok, msg)

        fake_client = FakeEspMotorClient(self.cfg)
        with (
            patch("mas004_rpi_databridge.machine_runtime.EspMotorClient", return_value=fake_client),
            patch("mas004_rpi_databridge.machine_runtime.SetupWicklerOrchestrator") as controller_cls,
        ):
            controller = controller_cls.return_value
            controller.run.return_value = {"ok": True, "applied": ["unwinder:200.0mm", "rewinder:200.0mm"]}
            snapshot = runtime.refresh()

        controller.run.assert_called_once()
        self.assertTrue(callable(controller.run.call_args.kwargs.get("wait_for_format_axes")))
        self.assertEqual(True, snapshot["info"]["setup"]["last_result"]["format_axes"]["ok"])
        self.assertEqual(["format_axes_parallel"], [
            phase["phase"] for phase in snapshot["info"]["setup"]["last_result"]["format_axes"]["phases"]
        ])
        self.assertIn((9, 52.0), fake_client.moves)
        self.assertIn((8, 53.0), fake_client.moves)
        self.assertIn((6, 23.2), fake_client.moves)
        self.assertIn((7, 24.3), fake_client.moves)
        self.assertIn((5, 48.4), fake_client.moves)
        self.assertEqual(5, len(fake_client.moves))

    def test_setup_axis_set_timeout_is_accepted_when_targets_verify_ok(self):
        class FakeEspMotorClient:
            def __init__(self, _cfg):
                self.position_tenths = {}

            def available(self):
                return True

            def config(self, _motor_id):
                return {
                    "config": {
                        "min_enabled": True,
                        "max_enabled": True,
                        "min_tenths_mm": -10000,
                        "max_tenths_mm": 10000,
                    }
                }

            def refresh(self, motor_id):
                return {
                    "motor": {
                        "state": {
                            "feedback_tenths_mm": self.position_tenths.get(int(motor_id), 0),
                            "move": False,
                            "busy": False,
                            "alarm": False,
                            "hwto": False,
                        }
                    }
                }

            def reset_alarm(self, _motor_id):
                return {"ok": True, "reply": "ACK"}

            def recover_eto_motor(self, _motor_id):
                return {"ok": True, "reply": "ACK"}

            def move_absolute_set_mm(self, targets_mm):
                for motor_id, absolute_mm in sorted(targets_mm.items()):
                    self.position_tenths[int(motor_id)] = int(round(float(absolute_mm) * 10.0))
                raise TimeoutError("timed out")

        self.cfg.esp_simulation = False
        for pkey, default_v in (
            ("MAP0008", "23"),
            ("MAP0009", "24"),
            ("MAP0010", "48"),
            ("MAP0029", "2"),
            ("MAP0030", "3"),
            ("MAP0031", "20"),
            ("MAP0032", "30"),
            ("MAP0033", "4"),
        ):
            _insert_param(self.db, pkey, "MAP", pkey[-4:], default_v, "W", "R", "uint16")
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=9,
            requested_state=9,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={},
        )
        ok, msg = self.params.set_value("MAS0002", "3", actor="microtom")
        self.assertTrue(ok, msg)

        with (
            patch("mas004_rpi_databridge.machine_runtime.EspMotorClient", return_value=FakeEspMotorClient(self.cfg)),
            patch("mas004_rpi_databridge.machine_runtime.SetupWicklerOrchestrator") as controller_cls,
        ):
            controller = controller_cls.return_value
            controller.run.return_value = {"ok": True, "applied": ["unwinder:200.0mm", "rewinder:200.0mm"]}
            snapshot = runtime.refresh()

        format_axes = snapshot["info"]["setup"]["last_result"]["format_axes"]
        self.assertTrue(format_axes["ok"], format_axes)
        phase = format_axes["phases"][0]
        self.assertTrue(phase["ok"], phase)
        self.assertIn("Positionssatz: timed out", "; ".join(phase["warnings"]))

    def test_setup_axis_set_timeout_retries_when_targets_do_not_move(self):
        class FakeEspMotorClient:
            def __init__(self, _cfg):
                self.position_tenths = {5: 1, 6: -199, 7: -199, 8: 999, 9: 998}
                self.move_attempts = 0

            def available(self):
                return True

            def config(self, _motor_id):
                return {
                    "config": {
                        "min_enabled": True,
                        "max_enabled": True,
                        "min_tenths_mm": -10000,
                        "max_tenths_mm": 10000,
                    }
                }

            def refresh(self, motor_id):
                return {
                    "motor": {
                        "state": {
                            "feedback_tenths_mm": self.position_tenths.get(int(motor_id), 0),
                            "move": False,
                            "busy": False,
                            "alarm": False,
                            "hwto": False,
                        }
                    }
                }

            def reset_alarm(self, _motor_id):
                return {"ok": True, "reply": "ACK"}

            def recover_eto_motor(self, _motor_id):
                return {"ok": True, "reply": "ACK"}

            def move_absolute_set_mm(self, targets_mm):
                self.move_attempts += 1
                if self.move_attempts == 1:
                    raise TimeoutError("timed out")
                for motor_id, absolute_mm in sorted(targets_mm.items()):
                    self.position_tenths[int(motor_id)] = int(round(float(absolute_mm) * 10.0))
                return {"ok": True, "results": [{"id": int(motor_id), "ok": True} for motor_id in targets_mm]}

        self.cfg.esp_simulation = False
        for pkey, default_v in (
            ("MAP0008", "23"),
            ("MAP0009", "24"),
            ("MAP0010", "48"),
            ("MAP0029", "2"),
            ("MAP0030", "3"),
            ("MAP0031", "20"),
            ("MAP0032", "30"),
            ("MAP0033", "4"),
        ):
            _insert_param(self.db, pkey, "MAP", pkey[-4:], default_v, "W", "R", "uint16")
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=9,
            requested_state=9,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={},
        )
        ok, msg = self.params.set_value("MAS0002", "3", actor="microtom")
        self.assertTrue(ok, msg)

        fake_client = FakeEspMotorClient(self.cfg)
        with (
            patch("mas004_rpi_databridge.machine_runtime.EspMotorClient", return_value=fake_client),
            patch("mas004_rpi_databridge.machine_runtime.SetupWicklerOrchestrator") as controller_cls,
            patch("mas004_rpi_databridge.machine_runtime.SETUP_AXIS_MOVE_SET_SHORT_VERIFY_TIMEOUT_S", 0.01),
            patch("mas004_rpi_databridge.machine_runtime.SETUP_AXIS_POSITION_VERIFY_POLL_S", 0.0),
            patch("mas004_rpi_databridge.machine_runtime.time.sleep"),
        ):
            controller = controller_cls.return_value
            controller.run.return_value = {"ok": True, "applied": ["unwinder:200.0mm", "rewinder:200.0mm"]}
            snapshot = runtime.refresh()

        format_axes = snapshot["info"]["setup"]["last_result"]["format_axes"]
        self.assertTrue(format_axes["ok"], format_axes)
        self.assertEqual(2, fake_client.move_attempts)
        phase = format_axes["phases"][0]
        self.assertEqual(2, len(phase["moves"]))
        self.assertIn("Positionssatz: timed out", "; ".join(phase["warnings"]))

    def test_axis_verification_waits_for_late_target_feedback_before_failing_stationary_motor(self):
        runtime = self.build_runtime()

        class FakeClient:
            def __init__(self):
                self.calls = 0

            def refresh(self, _motor_id):
                self.calls += 1
                feedback = -165 if self.calls == 1 else 400
                return {
                    "motor": {
                        "state": {
                            "feedback_tenths_mm": feedback,
                            "move": False,
                            "busy": False,
                            "alarm": False,
                            "alarm_code": 0,
                        }
                    }
                }

        client = FakeClient()
        result = runtime._verify_axis_targets(
            client,
            {7: 40.0},
            tolerance_tenths=1,
            timeout_s=1.0,
            poll_s=0.0,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(2, client.calls)

    def test_axis_verification_ignores_stale_refresh_payload_until_fresh_target(self):
        runtime = self.build_runtime()

        class FakeClient:
            def __init__(self):
                self.calls = 0

            def refresh(self, _motor_id):
                self.calls += 1
                if self.calls <= 3:
                    return {
                        "ok": False,
                        "motor": {
                            "state": {
                                "link_ok": True,
                                "feedback_tenths_mm": -200,
                                "move": False,
                                "busy": False,
                                "alarm": False,
                                "last_error": "stale sample",
                            }
                        },
                    }
                return {
                    "ok": True,
                    "motor": {
                        "state": {
                            "link_ok": True,
                            "feedback_tenths_mm": 400,
                            "move": False,
                            "busy": False,
                            "alarm": False,
                            "alarm_code": 0,
                        }
                    },
                }

        tick = {"value": 0.0}

        def fake_time():
            tick["value"] += 0.35
            return tick["value"]

        client = FakeClient()
        with (
            patch("mas004_rpi_databridge.machine_runtime.time.time", side_effect=fake_time),
            patch("mas004_rpi_databridge.machine_runtime.time.sleep"),
        ):
            result = runtime._verify_axis_targets(
                client,
                {6: 40.0},
                tolerance_tenths=1,
                timeout_s=3.0,
                poll_s=0.0,
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(4, client.calls)
        self.assertTrue(result["results"][0]["fresh"])

    def test_axis_verification_accepts_hardware_move_then_inpos_when_protocol_stale(self):
        runtime = self.build_runtime()

        class FakeClient:
            def refresh(self, _motor_id):
                return {
                    "ok": False,
                    "motor": {
                        "state": {
                            "link_ok": True,
                            "feedback_tenths_mm": -200,
                            "move": False,
                            "busy": False,
                            "alarm": False,
                            "last_error": "stale sample",
                        }
                    },
                }

        runtime._motor_feedback_io_snapshot = Mock(
            side_effect=[
                {1: {"move": {"active": True}, "in_pos": {"active": False}}},
                {1: {"move": {"active": False}, "in_pos": {"active": True}}},
            ]
        )
        tick = {"value": 0.0}

        def fake_time():
            tick["value"] += 0.25
            return tick["value"]

        with (
            patch("mas004_rpi_databridge.machine_runtime.time.time", side_effect=fake_time),
            patch("mas004_rpi_databridge.machine_runtime.time.sleep"),
        ):
            result = runtime._verify_axis_targets(
                FakeClient(),
                {1: 40.0},
                tolerance_tenths=1,
                timeout_s=3.0,
                poll_s=0.0,
            )

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["results"][0]["hardware_at_target"])
        self.assertFalse(result["results"][0]["fresh"])

    def test_failed_setup_workflow_returns_to_stop_instead_of_sticking_in_transition(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=9,
            requested_state=9,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={},
        )

        ok, msg = self.params.set_value("MAS0002", "3", actor="microtom")
        self.assertTrue(ok, msg)

        with patch("mas004_rpi_databridge.machine_runtime.SetupWicklerOrchestrator") as controller_cls:
            controller = controller_cls.return_value
            controller.run.side_effect = RuntimeError("device communication failed")
            snapshot = runtime.refresh()

        self.assertEqual(9, snapshot["current_state"])
        self.assertEqual(9, snapshot["requested_state"])
        self.assertEqual(2, snapshot["info"]["requested_command"])
        self.assertEqual(False, snapshot["info"]["setup"]["last_result"]["ok"])
        self.assertEqual("SETUP_WICKLER=NAK_DeviceComm", snapshot["info"]["setup"]["last_result"]["response"])
        self.assertEqual(9, runtime.refresh()["current_state"])

    def test_setup_start_replaces_stale_result_with_running_status(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=9,
            requested_state=9,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={
                "setup": {
                    "last_result": {"ok": False, "error": "old failed setup"},
                    "completed_ts": 123.0,
                    "failed_ts": 124.0,
                    "pause_pending": True,
                    "pause_pending_ts": 125.0,
                    "pause_completed_ts": 126.0,
                }
            },
        )

        ok, msg = self.params.set_value("MAS0002", "3", actor="microtom")
        self.assertTrue(ok, msg)
        observed: dict[str, object] = {}

        def fake_setup_workflow():
            row = runtime._state_row()
            info = dict(row.get("info") or {})
            observed.update(dict(info.get("setup") or {}))
            return {"ok": False, "skipped": True, "reason": "unit_test"}

        with patch.object(runtime, "_perform_setup_wickler_calibration", side_effect=fake_setup_workflow):
            runtime.refresh()

        self.assertEqual(True, observed["last_result"]["running"])
        self.assertEqual("SETUP_WICKLER=RUNNING", observed["last_result"]["response"])
        self.assertNotIn("ok", observed["last_result"])
        self.assertNotIn("error", observed["last_result"])
        self.assertNotIn("completed_ts", observed)
        self.assertNotIn("failed_ts", observed)
        self.assertNotIn("pause_pending", observed)
        self.assertNotIn("pause_pending_ts", observed)
        self.assertNotIn("pause_completed_ts", observed)

    def test_entering_stop_sends_defined_axis_positions_once(self):
        self.cfg.esp_simulation = False
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=9,
            requested_state=9,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={},
        )
        ok, msg = self.params.set_value("MAS0002", "2", actor="microtom")
        self.assertTrue(ok, msg)

        with patch("mas004_rpi_databridge.machine_runtime.EspMotorClient") as client_cls:
            client = client_cls.return_value
            client.available.return_value = True
            client.config.side_effect = lambda motor_id: {
                "ok": True,
                "config": {
                    "min_enabled": True,
                    "max_enabled": True,
                    "min_tenths_mm": -200,
                    "max_tenths_mm": 1100,
                },
            }
            client.move_absolute_set_mm.return_value = {"ok": True, "results": []}
            def fake_refresh(motor_id):
                targets = {5: 0, 6: -200, 7: -200, 8: 1000, 9: 1000}
                return {
                    "motor": {
                        "state": {
                            "feedback_tenths_mm": targets[int(motor_id)],
                            "move": False,
                            "busy": False,
                            "alarm": False,
                            "hwto": False,
                        }
                    }
                }
            client.refresh.side_effect = fake_refresh

            snapshot = runtime.refresh()

            self.assertEqual(9, snapshot["current_state"])
            self.assertEqual(True, snapshot["info"]["stop_positions"]["ok"])
            self.assertEqual([], [call.args[0] for call in client.reset_alarm.call_args_list])
            self.assertEqual([], [call.args[0] for call in client.recover_eto_motor.call_args_list])
            self.assertTrue(
                all(item.get("already_in_position") for item in snapshot["info"]["stop_positions"]["results"])
            )
            client.move_absolute_set_mm.assert_not_called()

            client.move_absolute_set_mm.reset_mock()
            second = runtime.refresh()
            self.assertEqual(9, second["current_state"])
            client.move_absolute_set_mm.assert_not_called()

    def test_motor_setup_lock_suppresses_automatic_stop_axis_positions(self):
        self.cfg.esp_simulation = False
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=8,
            requested_state=9,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={},
        )
        touch_motor_setup_manual_lock(self.db, motor_id=6, reason="unit_test")

        with patch("mas004_rpi_databridge.machine_runtime.EspMotorClient") as client_cls:
            client = client_cls.return_value
            client.available.return_value = True

            snapshot = runtime.refresh()

        self.assertEqual(9, snapshot["current_state"])
        self.assertEqual("motor_setup_manual_lock_active", snapshot["info"]["stop_positions"]["reason"])
        self.assertEqual(6, snapshot["info"]["stop_positions"]["manual_lock"]["motor_id"])
        client.move_absolute_set_mm.assert_not_called()

    def test_motion_recovery_suppresses_automatic_stop_axis_positions(self):
        self.cfg.esp_simulation = False
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=8,
            requested_state=9,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={},
        )
        self.assertTrue(machine_runtime_module._RESET_MOTION_RECOVERY_LOCK.acquire(blocking=False))
        try:
            with patch("mas004_rpi_databridge.machine_runtime.EspMotorClient") as client_cls:
                snapshot = runtime.refresh()
        finally:
            machine_runtime_module._RESET_MOTION_RECOVERY_LOCK.release()

        self.assertEqual(9, snapshot["current_state"])
        self.assertEqual("reset_motion_recovery_in_progress", snapshot["info"]["stop_positions"]["reason"])
        self.assertFalse(snapshot["info"]["stop_positions"]["ok"])
        client_cls.return_value.move_absolute_set_mm.assert_not_called()

    def test_setup_waits_for_reset_motion_recovery_before_starting(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=9,
            requested_state=9,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={},
        )
        ok, msg = self.params.set_value("MAS0002", "3", actor="microtom")
        self.assertTrue(ok, msg)

        self.assertTrue(machine_runtime_module._RESET_MOTION_RECOVERY_LOCK.acquire(blocking=False))
        try:
            with patch("mas004_rpi_databridge.machine_runtime.SetupWicklerOrchestrator") as controller_cls:
                waiting = runtime.refresh()
                controller_cls.return_value.run.assert_not_called()
        finally:
            machine_runtime_module._RESET_MOTION_RECOVERY_LOCK.release()

        self.assertEqual(9, waiting["current_state"])
        self.assertEqual("0", self.params.get_effective_value("MAS0002"))
        setup = waiting["info"]["setup"]
        self.assertTrue(setup["motion_recovery_pending"])
        self.assertTrue(setup["last_result"]["waiting"])
        self.assertEqual("reset_motion_recovery_in_progress", setup["last_result"]["reason"])

        info = dict(runtime._state_row().get("info") or {})
        safety = dict(info.get("safety") or {})
        safety["last_motion_recovery"] = {"ok": True, "finished_ts": now_ts()}
        info["safety"] = safety
        runtime._write_state(
            current_state=9,
            requested_state=9,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info=info,
        )

        with patch("mas004_rpi_databridge.machine_runtime.SetupWicklerOrchestrator") as controller_cls:
            controller_cls.return_value.run.return_value = {"ok": True}
            finished = runtime.refresh()

        controller_cls.return_value.run.assert_called_once()
        self.assertEqual(7, finished["current_state"])
        self.assertEqual(7, finished["requested_state"])
        self.assertNotIn("motion_recovery_pending", finished["info"]["setup"])
        self.assertEqual("0", self.params.get_effective_value("MAS0002"))

    def test_stop_axis_positions_are_not_ok_when_drive_accepts_but_does_not_move(self):
        self.cfg.esp_simulation = False
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=8,
            requested_state=9,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={},
        )
        ok, msg = self.params.set_value("MAS0002", "2", actor="microtom")
        self.assertTrue(ok, msg)

        with patch("mas004_rpi_databridge.machine_runtime.EspMotorClient") as client_cls:
            client = client_cls.return_value
            client.available.return_value = True
            client.config.side_effect = lambda motor_id: {
                "ok": True,
                "config": {
                    "min_enabled": True,
                    "max_enabled": True,
                    "min_tenths_mm": -200,
                    "max_tenths_mm": 1100,
                },
            }
            client.move_absolute_set_mm.return_value = {"ok": True, "results": []}
            client.refresh.side_effect = lambda motor_id: {
                "motor": {
                    "state": {
                        "feedback_tenths_mm": 300 if int(motor_id) == 6 else 1000,
                        "move": False,
                        "busy": False,
                        "alarm": False,
                        "hwto": False,
                    }
                }
            }

            with (
                patch("mas004_rpi_databridge.machine_runtime.STOP_MODE_POSITION_VERIFY_TIMEOUT_S", 0.05),
                patch("mas004_rpi_databridge.machine_runtime.STOP_MODE_POSITION_VERIFY_POLL_S", 0.0),
            ):
                snapshot = runtime.refresh()

        self.assertEqual(9, snapshot["current_state"])
        self.assertEqual(False, snapshot["info"]["stop_positions"]["ok"])
        self.assertTrue(any("Motor 6 steht" in item for item in snapshot["info"]["stop_positions"]["errors"]))

    def test_existing_stop_state_revalidates_old_stop_position_result(self):
        self.cfg.esp_simulation = False
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=9,
            requested_state=9,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"stop_positions": {"active": True, "ok": True, "target_key": "5:0.000;6:-20.000;7:-20.000;8:91.000;9:91.000"}},
        )

        with patch("mas004_rpi_databridge.machine_runtime.EspMotorClient") as client_cls:
            client = client_cls.return_value
            client.available.return_value = True
            client.config.side_effect = lambda motor_id: {
                "ok": True,
                "config": {
                    "min_enabled": True,
                    "max_enabled": True,
                    "min_tenths_mm": -200,
                    "max_tenths_mm": 1100,
                },
            }
            client.move_absolute_set_mm.return_value = {"ok": True, "results": []}
            client.refresh.side_effect = lambda motor_id: {
                "motor": {
                    "state": {
                        "feedback_tenths_mm": {5: 0, 6: -200, 7: -200, 8: 1000, 9: 1000}[int(motor_id)],
                        "move": False,
                        "busy": False,
                        "alarm": False,
                        "hwto": False,
                    }
                }
            }

            snapshot = runtime.refresh()

        self.assertEqual(True, snapshot["info"]["stop_positions"]["ok"])
        self.assertEqual(10, snapshot["info"]["stop_positions"]["logic_version"])
        self.assertEqual(1, snapshot["info"]["stop_positions"]["attempt_count"])
        client.move_absolute_set_mm.assert_not_called()

    def test_stop_axis_position_commands_inside_active_min_limit(self):
        self.cfg.esp_simulation = False
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=8,
            requested_state=9,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={},
        )
        ok, msg = self.params.set_value("MAS0002", "2", actor="microtom")
        self.assertTrue(ok, msg)
        positions = {5: 380, 6: -200, 7: -200, 8: 1000, 9: 1000}

        with patch("mas004_rpi_databridge.machine_runtime.EspMotorClient") as client_cls:
            client = client_cls.return_value
            client.available.return_value = True

            def fake_config(motor_id):
                motor_id = int(motor_id)
                min_tenths = 0 if motor_id == 5 else (-200 if motor_id in {6, 7} else 100)
                max_tenths = 1000 if motor_id in {5, 8, 9} else 650
                return {
                    "ok": True,
                    "config": {
                        "min_enabled": True,
                        "max_enabled": True,
                        "min_tenths_mm": min_tenths,
                        "max_tenths_mm": max_tenths,
                    },
                }

            def fake_refresh(motor_id):
                motor_id = int(motor_id)
                return {
                    "motor": {
                        "state": {
                            "feedback_tenths_mm": positions[motor_id],
                            "move": False,
                            "busy": False,
                            "alarm": False,
                            "hwto": False,
                        }
                    }
                }

            def fake_move(targets_mm):
                for motor_id, target_mm in targets_mm.items():
                    positions[int(motor_id)] = int(round(float(target_mm) * 10.0))
                return {"ok": True, "results": []}

            client.config.side_effect = fake_config
            client.refresh.side_effect = fake_refresh
            client.move_absolute_set_mm.side_effect = fake_move

            with patch("mas004_rpi_databridge.machine_runtime.STOP_MODE_POSITION_VERIFY_POLL_S", 0.0):
                snapshot = runtime.refresh()

        self.assertEqual(True, snapshot["info"]["stop_positions"]["ok"])
        client.move_absolute_set_mm.assert_called_once()
        sent_targets = client.move_absolute_set_mm.call_args.args[0]
        self.assertEqual(0.1, sent_targets[5])
        motor5_result = [r for r in snapshot["info"]["stop_positions"]["results"] if r["motor_id"] == 5][0]
        self.assertEqual(0.0, motor5_result["target_mm"])
        self.assertEqual(0.1, motor5_result["command_target_mm"])
        self.assertEqual("stop_mode_limit_margin", motor5_result["command_adjustment"]["reason"])

    def test_stop_axis_positions_block_axis_outside_soft_limits(self):
        self.cfg.esp_simulation = False
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=8,
            requested_state=9,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={},
        )
        ok, msg = self.params.set_value("MAS0002", "2", actor="microtom")
        self.assertTrue(ok, msg)

        with patch("mas004_rpi_databridge.machine_runtime.EspMotorClient") as client_cls:
            client = client_cls.return_value
            client.available.return_value = True
            client.config.side_effect = lambda motor_id: {
                "ok": True,
                "config": {
                    "min_enabled": True,
                    "max_enabled": True,
                    "min_tenths_mm": -200,
                    "max_tenths_mm": 1100,
                },
            }

            def fake_refresh(motor_id):
                motor_id = int(motor_id)
                return {
                    "motor": {
                        "state": {
                            "feedback_tenths_mm": -6508 if motor_id == 7 else {5: 0, 6: -200, 8: 1000, 9: 1000}.get(motor_id, 0),
                            "move": False,
                            "busy": False,
                            "alarm": motor_id == 7,
                            "alarm_code": 16 if motor_id == 7 else 0,
                            "hwto": False,
                        }
                    }
                }

            client.refresh.side_effect = fake_refresh
            client.move_absolute_set_mm.return_value = {"ok": True, "results": []}

            snapshot = runtime.refresh()

        self.assertEqual(9, snapshot["current_state"])
        self.assertEqual(False, snapshot["info"]["stop_positions"]["ok"])
        self.assertTrue(any("Motor 7" in item and "Alarm" in item for item in snapshot["info"]["stop_positions"]["errors"]))
        client.move_absolute_set_mm.assert_not_called()

    def test_position_axis_preflight_blocks_when_live_counter_is_outside_limits(self):
        runtime = self.build_runtime()

        class FakeClient:
            def __init__(self):
                self.calls = []

            def config(self, motor_id):
                self.calls.append(("config", motor_id))
                return {
                    "ok": True,
                    "config": {
                        "min_enabled": True,
                        "max_enabled": True,
                        "min_tenths_mm": -200,
                        "max_tenths_mm": 650,
                    },
                }

            def refresh(self, motor_id):
                self.calls.append(("refresh", motor_id))
                return {
                    "ok": True,
                    "motor": {
                        "state": {
                            "feedback_tenths_mm": -6800,
                            "move": False,
                            "busy": False,
                            "alarm": False,
                            "hwto": False,
                        }
                    },
                }

            def set_current_position_mm(self, motor_id, value):
                self.calls.append(("set_current_position_mm", motor_id, value))
                raise AssertionError("Positionsachsen duerfen nicht automatisch neu referenziert werden")

            def save(self, motor_id):
                self.calls.append(("save", motor_id))
                raise AssertionError("Positionsachsen duerfen nicht automatisch gespeichert werden")

        client = FakeClient()
        result = runtime._motor_preflight_for_position_move(client, 7, 40.0)

        self.assertFalse(result["ok"], result)
        self.assertTrue(result["position_write_block"]["blocked"])
        self.assertEqual("live_position_outside_limits", result["position_write_block"]["reason"])
        self.assertIn("unter Min", result["reason"])
        self.assertEqual([("config", 7), ("refresh", 7)], client.calls)

    def test_position_axis_preflight_blocks_after_older_runtime_position_restore_until_setup_resave(self):
        runtime = self.build_runtime()
        base_ts = now_ts()
        with self.db._conn() as c:
            c.execute(
                """INSERT INTO motor_setup_master(motor_id,config_json,state_json,updated_ts)
                   VALUES(?,?,?,?)""",
                (7, "{}", "{}", base_ts - 20.0),
            )
            c.execute(
                "INSERT INTO machine_events(ts,event_type,severity,message,payload_json) VALUES(?,?,?,?,?)",
                (
                    base_ts - 10.0,
                    "motor_setup_position_restored",
                    "warning",
                    "Motor 7: Positionszaehler aus Motor-Setup-Master wiederhergestellt",
                    json.dumps({"motor_id": 7, "before_feedback_tenths_mm": -6800}),
                ),
            )

        class FakeClient:
            def __init__(self):
                self.calls = []

            def config(self, motor_id):
                self.calls.append(("config", motor_id))
                return {
                    "ok": True,
                    "config": {
                        "min_enabled": True,
                        "max_enabled": True,
                        "min_tenths_mm": -200,
                        "max_tenths_mm": 650,
                    },
                }

            def refresh(self, motor_id):
                self.calls.append(("refresh", motor_id))
                return {
                    "ok": True,
                    "motor": {
                        "state": {
                            "feedback_tenths_mm": -200,
                            "move": False,
                            "busy": False,
                            "alarm": False,
                            "hwto": False,
                        }
                    },
                }

        client = FakeClient()
        result = runtime._motor_preflight_for_position_move(client, 7, -20.0)

        self.assertFalse(result["ok"], result)
        self.assertTrue(result["position_reference_suspect"]["blocked"])
        self.assertIn("Positionsreferenz", result["reason"])
        self.assertEqual([("config", 7), ("refresh", 7)], client.calls)

        with self.db._conn() as c:
            c.execute(
                "UPDATE motor_setup_master SET updated_ts=? WHERE motor_id=?",
                (base_ts + 10.0, 7),
            )

        result_after_setup = runtime._motor_preflight_for_position_move(client, 7, -20.0)
        self.assertTrue(result_after_setup["ok"], result_after_setup)

    def test_position_axis_preflight_blocks_after_explicit_reference_suspect_mark(self):
        runtime = self.build_runtime()
        base_ts = now_ts()
        with self.db._conn() as c:
            c.execute(
                """INSERT INTO motor_setup_master(motor_id,config_json,state_json,updated_ts)
                   VALUES(?,?,?,?)""",
                (6, "{}", "{}", base_ts - 20.0),
            )
            c.execute(
                "INSERT INTO machine_events(ts,event_type,severity,message,payload_json) VALUES(?,?,?,?,?)",
                (
                    base_ts - 10.0,
                    "motor_position_reference_suspect",
                    "warning",
                    "Motor 6: Positionsreferenz durch Bedienerhinweis als unsicher markiert",
                    json.dumps({"motor_id": 6, "reason": "operator_reported_ui_mechanical_mismatch"}),
                ),
            )

        class FakeClient:
            def __init__(self):
                self.calls = []

            def config(self, motor_id):
                self.calls.append(("config", motor_id))
                return {
                    "ok": True,
                    "config": {
                        "min_enabled": True,
                        "max_enabled": True,
                        "min_tenths_mm": -200,
                        "max_tenths_mm": 650,
                    },
                }

            def refresh(self, motor_id):
                self.calls.append(("refresh", motor_id))
                return {
                    "ok": True,
                    "motor": {
                        "state": {
                            "feedback_tenths_mm": -200,
                            "move": False,
                            "busy": False,
                            "alarm": False,
                            "hwto": False,
                        }
                    },
                }

        result = runtime._motor_preflight_for_position_move(FakeClient(), 6, -20.0)

        self.assertFalse(result["ok"], result)
        self.assertTrue(result["position_reference_suspect"]["blocked"])
        self.assertEqual(
            "motor_position_reference_suspect",
            result["position_reference_suspect"]["suspect_event"]["event_type"],
        )

    def test_stop_axis_position_verification_waits_for_motion_to_finish(self):
        runtime = self.build_runtime()

        class FakeClient:
            def __init__(self):
                self.motor6_calls = 0

            def refresh(self, motor_id):
                motor_id = int(motor_id)
                if motor_id == 6:
                    self.motor6_calls += 1
                    if self.motor6_calls == 1:
                        return {"motor": {"state": {"feedback_tenths_mm": -150, "move": True, "alarm": False}}}
                targets = {5: 0, 6: -200, 7: -200, 8: 1000, 9: 1000}
                return {"motor": {"state": {"feedback_tenths_mm": targets[motor_id], "move": False, "alarm": False}}}

        with patch("mas004_rpi_databridge.machine_runtime.STOP_MODE_POSITION_VERIFY_POLL_S", 0.0):
            result = runtime._verify_stop_mode_axis_targets(FakeClient())

        self.assertTrue(result["ok"], result)
        motor6 = [item for item in result["results"] if item["motor_id"] == 6][-1]
        self.assertEqual(-200, motor6["feedback_tenths_mm"])
        self.assertFalse(motor6["moving"])

    def test_virtual_start_pause_button_uses_same_mas0002_command_path(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=7,
            requested_state=7,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={},
        )

        result = runtime.press_virtual_button("start_pause")

        self.assertTrue(result["ok"])
        self.assertEqual("start_pause", result["button"])
        self.assertEqual(1, result["command"])
        self.assertEqual(5, result["target_state"])
        self.assertTrue(result["queued"])
        # The virtual HMI button mirrors the physical panel and only queues the
        # command. The central runtime loop consumes MAS0002.
        self.assertEqual("1", self.params.get_effective_value("MAS0002"))

    def test_virtual_start_pause_is_blocked_while_setup_is_not_completed(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=3,
            requested_state=3,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"setup": {"last_result": {"ok": False}}},
        )

        with self.assertRaisesRegex(RuntimeError, "not allowed"):
            runtime.press_virtual_button("start_pause")

        self.assertNotEqual("1", self.params.get_effective_value("MAS0002"))

    def test_direct_start_command_is_ignored_during_setup(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=3,
            requested_state=3,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"setup": {"last_result": {"ok": False}}},
        )
        self.params.set_value("MAS0002", "1", actor="microtom")

        snapshot = runtime.refresh()

        self.assertEqual(3, snapshot["current_state"])
        self.assertEqual("0", self.params.get_effective_value("MAS0002"))

    def test_virtual_setup_is_blocked_when_already_in_setup(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=3,
            requested_state=3,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={},
        )

        with patch("mas004_rpi_databridge.machine_runtime.SetupWicklerOrchestrator") as controller_cls:
            with self.assertRaisesRegex(RuntimeError, "not allowed"):
                runtime.press_virtual_button("setup")
            controller_cls.assert_not_called()

        self.assertNotEqual("3", self.params.get_effective_value("MAS0002"))

    def test_virtual_setup_is_blocked_while_light_curtain_is_active(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=9,
            requested_state=9,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"safety": {"latched": False, "phase": "ready"}},
        )
        self.io_store.upsert_value("esp32_plc58__I0_8", "0", "live", "test")

        with patch("mas004_rpi_databridge.machine_runtime.SetupWicklerOrchestrator") as controller_cls:
            with self.assertRaisesRegex(RuntimeError, "light curtain"):
                runtime.press_virtual_button("setup")
            controller_cls.assert_not_called()

        self.assertNotEqual("3", self.params.get_effective_value("MAS0002"))

    def test_direct_setup_command_is_ignored_while_light_curtain_is_active(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=9,
            requested_state=9,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"safety": {"latched": False, "phase": "ready"}},
        )
        self.io_store.upsert_value("esp32_plc58__I0_8", "0", "live", "test")
        ok, msg = self.params.set_value("MAS0002", "3", actor="panel")
        self.assertTrue(ok, msg)

        with patch("mas004_rpi_databridge.machine_runtime.SetupWicklerOrchestrator") as controller_cls:
            snapshot = runtime.refresh()
            controller_cls.assert_not_called()

        self.assertEqual(9, snapshot["current_state"])
        self.assertEqual("0", self.params.get_effective_value("MAS0002"))
        self.assertEqual(["lichtgitter"], snapshot["info"]["safety_status"]["reasons"])

    def test_direct_setup_command_is_ignored_from_pause(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=7,
            requested_state=7,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={},
        )
        ok, msg = self.params.set_value("MAS0002", "3", actor="microtom")
        self.assertTrue(ok, msg)

        with patch("mas004_rpi_databridge.machine_runtime.SetupWicklerOrchestrator") as controller_cls:
            snapshot = runtime.refresh()
            controller_cls.assert_not_called()

        self.assertEqual(7, snapshot["current_state"])
        self.assertEqual(7, snapshot["requested_state"])
        self.assertEqual("0", self.params.get_effective_value("MAS0002"))

    def test_virtual_start_pause_resets_from_purge_context(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=21,
            requested_state=21,
            state_source="test",
            warning_active=False,
            purge_active=True,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"safety": {"latched": True}},
        )

        result = runtime.press_virtual_button("start")

        self.assertTrue(result["ok"])
        self.assertEqual("start_pause", result["button"])
        self.assertEqual(2, result["command"])
        self.assertEqual("2", self.params.get_effective_value("MAS0002"))

        runtime.refresh()

        self.assertEqual("0", self.params.get_effective_value("MAS0002"))

    def test_stale_reset_command_is_consumed_in_production_stop(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=9,
            requested_state=9,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"safety": {"latched": False, "phase": "ready"}},
        )
        self.params.set_value("MAS0002", "2", actor="microtom")

        snapshot = runtime.refresh()

        self.assertEqual(9, snapshot["current_state"])
        self.assertEqual("0", self.params.get_effective_value("MAS0002"))

    def test_safety_status_exposes_ups_input_detail(self):
        runtime = self.build_runtime()
        self.io_store.upsert_value("raspi_plc21__I0_6", "0", "live", "raspi")

        status = runtime._safety_status(runtime._io_values())

        self.assertFalse(status["ups_ok"])
        self.assertTrue(status["ups_not_ok"])
        self.assertEqual("0", status["ups_input"]["value"])
        self.assertEqual("raspi_plc21__I0_6", status["ups_input"]["io_key"])
        self.assertEqual("live", status["ups_input"]["quality"])

    def test_light_curtain_only_auto_reset_pulses_without_purge_or_state_change(self):
        runtime = self.build_runtime()
        self.params.apply_device_value("MAS0001", "9", promote_default=True)
        runtime._write_state(
            current_state=9,
            requested_state=9,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"safety": {"latched": False, "phase": "ready"}},
        )
        self.io_store.upsert_value("esp32_plc58__I0_8", "0", "live", "test")

        with (
            patch.object(runtime, "_pulse_esp_reset_output", return_value=None) as pulse,
            patch.object(runtime, "_perform_safety_reset", return_value={"ok": True, "steps": []}) as full_reset,
        ):
            snapshot = runtime.refresh()

        pulse.assert_called_once()
        full_reset.assert_not_called()
        self.assertEqual(9, snapshot["current_state"])
        self.assertFalse(snapshot["purge_active"])
        self.assertEqual("0", self.params.get_effective_value("MAS0028"))
        self.assertEqual([], snapshot["info"]["critical_reasons"])
        self.assertEqual(["lichtgitter"], snapshot["info"]["safety_status"]["reasons"])
        self.assertFalse(snapshot["info"]["safety"]["latched"])
        self.assertTrue(snapshot["info"]["safety"]["last_auto_reset"]["ok"])
        self.assertEqual("lichtgitter", snapshot["info"]["safety"]["last_auto_reset"]["reason"])
        self.assertFalse(snapshot["info"]["safety"]["last_auto_reset"]["state_changed"])
        self.assertFalse(snapshot["info"]["safety"]["last_auto_reset"]["purge_changed"])

    def test_light_curtain_in_pause_keeps_pause_when_safety_ok_input_drops(self):
        runtime = self.build_runtime()
        self.params.apply_device_value("MAS0001", "7", promote_default=True)
        runtime._write_state(
            current_state=7,
            requested_state=7,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"safety": {"latched": False, "phase": "ready"}, "production_runtime": {"active": False}},
        )
        self.io_store.upsert_value("esp32_plc58__I0_8", "0", "live", "test")
        self.io_store.upsert_value("esp32_plc58__I0_7", "0", "live", "test")

        with (
            patch.object(runtime, "_pulse_esp_reset_output", return_value=None) as pulse,
            patch.object(runtime, "_perform_safety_reset", return_value={"ok": True, "steps": []}) as full_reset,
            patch.object(runtime, "_stop_production_motion", return_value={"ok": True}) as stop_motion,
            patch.object(runtime, "_apply_stop_mode_axis_targets", return_value=None) as stop_axes,
            patch.object(runtime, "_start_light_curtain_wickler_recovery_background") as wickler_recovery,
        ):
            snapshot = runtime.refresh()

        pulse.assert_called_once()
        full_reset.assert_not_called()
        stop_motion.assert_not_called()
        stop_axes.assert_called_once()
        wickler_recovery.assert_not_called()
        self.assertEqual(7, stop_axes.call_args.args[0])
        self.assertEqual(7, snapshot["current_state"])
        self.assertEqual(7, snapshot["requested_state"])
        self.assertEqual("requested", runtime._state_row()["state_source"])
        self.assertFalse(snapshot["purge_active"])
        self.assertEqual("0", self.params.get_effective_value("MAS0028"))
        self.assertEqual([], snapshot["info"]["critical_reasons"])
        safety_status = snapshot["info"]["safety_status"]
        self.assertEqual(["lichtgitter"], safety_status["reasons"])
        self.assertTrue(safety_status["raw_estop_active"])
        self.assertTrue(safety_status["estop_masked_by_pause_light_curtain"])
        self.assertFalse(safety_status["estop_active"])
        self.assertFalse(safety_status["blocking_active"])
        self.assertTrue(snapshot["info"]["safety"]["last_auto_reset"]["ok"])
        self.assertFalse(snapshot["info"]["safety"]["last_auto_reset"]["state_changed"])
        self.assertFalse(snapshot["info"]["safety"]["last_auto_reset"]["purge_changed"])

    def test_light_curtain_release_in_pause_recovers_wicklers_to_ready(self):
        runtime = self.build_runtime()
        auto_reset_ts = now_ts() - 1.0
        self.params.apply_device_value("MAS0001", "7", promote_default=True)
        runtime._write_state(
            current_state=7,
            requested_state=7,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={
                "safety": {
                    "latched": False,
                    "phase": "ready",
                    "last_auto_reset": {
                        "source": "light_curtain_auto_reset",
                        "reason": "lichtgitter",
                        "ts": auto_reset_ts,
                        "ok": True,
                    },
                },
                "production_runtime": {"active": False},
            },
        )
        self.io_store.upsert_value("esp32_plc58__I0_8", "1", "live", "test")
        self.io_store.upsert_value("esp32_plc58__I0_7", "1", "live", "test")

        with patch.object(
            runtime,
            "_start_light_curtain_wickler_recovery_background",
            return_value={
                "ok": True,
                "queued": True,
                "in_progress": True,
                "auto_reset_ts": auto_reset_ts,
            },
        ) as wickler_recovery:
            snapshot = runtime.refresh()

        wickler_recovery.assert_called_once()
        self.assertAlmostEqual(auto_reset_ts, wickler_recovery.call_args.args[0], places=3)
        self.assertEqual(7, snapshot["current_state"])
        self.assertEqual(7, snapshot["requested_state"])
        self.assertFalse(snapshot["purge_active"])
        safety = snapshot["info"]["safety"]
        self.assertTrue(safety["light_curtain_wickler_recovery_running"])
        self.assertTrue(safety["last_light_curtain_wickler_recovery_start"]["queued"])
        self.assertEqual(auto_reset_ts, safety["last_light_curtain_wickler_recovery_start"]["auto_reset_ts"])

    def test_light_curtain_release_in_pause_does_not_repeat_completed_wickler_recovery(self):
        runtime = self.build_runtime()
        auto_reset_ts = now_ts() - 1.0
        runtime._write_state(
            current_state=7,
            requested_state=7,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={
                "safety": {
                    "latched": False,
                    "phase": "ready",
                    "last_auto_reset": {
                        "source": "light_curtain_auto_reset",
                        "reason": "lichtgitter",
                        "ts": auto_reset_ts,
                        "ok": True,
                    },
                    "light_curtain_wickler_recovery_last_auto_reset_ts": auto_reset_ts,
                }
            },
        )
        self.io_store.upsert_value("esp32_plc58__I0_8", "1", "live", "test")
        self.io_store.upsert_value("esp32_plc58__I0_7", "1", "live", "test")

        with patch.object(runtime, "_start_light_curtain_wickler_recovery_background") as wickler_recovery:
            snapshot = runtime.refresh()

        wickler_recovery.assert_not_called()
        self.assertEqual(7, snapshot["current_state"])
        self.assertFalse(snapshot["purge_active"])

    def test_light_curtain_in_production_pauses_without_purge(self):
        runtime = self.build_runtime()
        self.params.apply_device_value("MAS0001", "5", promote_default=True)
        runtime._write_state(
            current_state=5,
            requested_state=5,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"safety": {"latched": False, "phase": "ready"}, "production_runtime": {"active": True}},
        )
        self.io_store.upsert_value("esp32_plc58__I0_8", "0", "live", "test")

        with (
            patch.object(runtime, "_pulse_esp_reset_output", return_value=None) as pulse,
            patch.object(runtime, "_perform_safety_reset", return_value={"ok": True, "steps": []}) as full_reset,
            patch.object(
                runtime,
                "_pause_production_motion_after_print",
                return_value={"ok": True, "target_state": 7, "controlled": True},
            ) as pause_motion,
            patch.object(runtime, "_stop_production_motion") as stop_motion,
        ):
            snapshot = runtime.refresh()

        pulse.assert_called_once()
        full_reset.assert_not_called()
        pause_motion.assert_called_once()
        stop_motion.assert_not_called()
        self.assertEqual("light_curtain_pause", pause_motion.call_args.kwargs["reason"])
        self.assertEqual(7, pause_motion.call_args.kwargs["target_state"])
        self.assertEqual(7, snapshot["current_state"])
        self.assertEqual(7, snapshot["requested_state"])
        self.assertEqual("light_curtain_pause", runtime._state_row()["state_source"])
        self.assertFalse(snapshot["purge_active"])
        self.assertEqual("0", self.params.get_effective_value("MAS0028"))
        self.assertTrue(snapshot["info"]["safety"]["last_auto_reset"]["state_changed"])
        self.assertFalse(snapshot["info"]["safety"]["last_auto_reset"]["purge_changed"])

    def test_light_curtain_in_rewind_pauses_and_stops_motion_without_purge(self):
        runtime = self.build_runtime()
        self.params.apply_device_value("MAS0001", "11", promote_default=True)
        runtime._write_state(
            current_state=11,
            requested_state=11,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"safety": {"latched": False, "phase": "ready"}, "production_runtime": {"active": False}},
        )
        self.io_store.upsert_value("esp32_plc58__I0_8", "0", "live", "test")

        with (
            patch.object(runtime, "_pulse_esp_reset_output", return_value=None),
            patch.object(runtime, "_perform_safety_reset", return_value={"ok": True, "steps": []}) as full_reset,
            patch.object(runtime, "_stop_production_motion", return_value={"ok": True, "target_state": 7}) as stop_motion,
        ):
            snapshot = runtime.refresh()

        full_reset.assert_not_called()
        stop_motion.assert_called_once()
        self.assertEqual("light_curtain_pause", stop_motion.call_args.kwargs["reason"])
        self.assertEqual(7, snapshot["current_state"])
        self.assertEqual(7, snapshot["requested_state"])
        self.assertEqual("light_curtain_pause", runtime._state_row()["state_source"])
        self.assertFalse(snapshot["purge_active"])
        self.assertEqual("0", self.params.get_effective_value("MAS0028"))

    def test_light_curtain_auto_reset_can_be_disabled(self):
        self.cfg.light_curtain_auto_reset_enabled = False
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=9,
            requested_state=9,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"safety": {"latched": False, "phase": "ready"}},
        )
        self.io_store.upsert_value("esp32_plc58__I0_8", "0", "live", "test")

        with patch.object(runtime, "_pulse_esp_reset_output", return_value=None) as pulse:
            snapshot = runtime.refresh()

        self.assertEqual(9, snapshot["current_state"])
        pulse.assert_not_called()
        self.assertFalse(snapshot["purge_active"])
        self.assertEqual("0", self.params.get_effective_value("MAS0028"))

    def test_light_curtain_auto_reset_waits_for_cooldown(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=9,
            requested_state=9,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={
                "safety": {
                    "latched": False,
                    "phase": "ready",
                    "light_curtain_auto_reset_last_ts": now_ts(),
                }
            },
        )
        self.io_store.upsert_value("esp32_plc58__I0_8", "0", "live", "test")

        with patch.object(runtime, "_pulse_esp_reset_output", return_value=None) as pulse:
            snapshot = runtime.refresh()

        pulse.assert_not_called()
        self.assertEqual(9, snapshot["current_state"])
        self.assertFalse(snapshot["purge_active"])

    def test_stale_light_curtain_only_latch_is_cleared_without_full_reset(self):
        runtime = self.build_runtime()
        self.params.apply_device_value("MAS0001", "21", promote_default=True)
        self.params.apply_device_value("MAS0028", "1", promote_default=True)
        runtime._write_state(
            current_state=21,
            requested_state=21,
            state_source="safety_latched",
            warning_active=False,
            purge_active=True,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"safety": {"latched": True, "phase": "latched", "last_reasons": ["lichtgitter"]}},
        )
        self.io_store.upsert_value("esp32_plc58__I0_8", "0", "live", "test")

        with (
            patch.object(runtime, "_pulse_esp_reset_output", return_value=None) as pulse,
            patch.object(runtime, "_perform_safety_reset", return_value={"ok": True, "steps": []}) as full_reset,
        ):
            snapshot = runtime.refresh()

        pulse.assert_called_once()
        full_reset.assert_not_called()
        self.assertEqual(9, snapshot["current_state"])
        self.assertFalse(snapshot["purge_active"])
        self.assertEqual("0", self.params.get_effective_value("MAS0028"))
        self.assertEqual("stale_light_curtain_latch_cleared", runtime._state_row()["state_source"])
        self.assertEqual("ready", snapshot["info"]["safety"]["phase"])
        self.assertFalse(snapshot["info"]["safety"]["latched"])

    def test_stale_estop_latch_after_successful_reset_is_cleared_without_second_reset(self):
        runtime = self.build_runtime()
        self.params.apply_device_value("MAS0001", "21", promote_default=True)
        self.params.apply_device_value("MAS0028", "0", promote_default=True)
        runtime._write_state(
            current_state=21,
            requested_state=21,
            state_source="safety_latched",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={
                "safety": {
                    "latched": True,
                    "phase": "latched",
                    "last_reasons": ["notaus"],
                    "last_reset": {"ok": True, "initial_reasons": ["notaus"]},
                }
            },
        )
        self.io_store.upsert_value("esp32_plc58__I0_7", "1", "live", "test")
        self.io_store.upsert_value("esp32_plc58__I0_8", "1", "live", "test")

        with patch.object(runtime, "_perform_safety_reset", return_value={"ok": True}) as full_reset:
            snapshot = runtime.refresh()

        full_reset.assert_not_called()
        self.assertEqual(9, snapshot["current_state"])
        self.assertFalse(snapshot["purge_active"])
        self.assertEqual("stale_blocking_safety_latch_cleared", runtime._state_row()["state_source"])
        self.assertEqual("ready", snapshot["info"]["safety"]["phase"])
        self.assertFalse(snapshot["info"]["safety"]["latched"])

    def test_stale_estop_latch_without_successful_reset_stays_latched(self):
        runtime = self.build_runtime()
        self.params.apply_device_value("MAS0001", "21", promote_default=True)
        self.params.apply_device_value("MAS0028", "0", promote_default=True)
        runtime._write_state(
            current_state=21,
            requested_state=21,
            state_source="safety_latched",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"safety": {"latched": True, "phase": "latched", "last_reasons": ["notaus"]}},
        )
        self.io_store.upsert_value("esp32_plc58__I0_7", "1", "live", "test")
        self.io_store.upsert_value("esp32_plc58__I0_8", "1", "live", "test")

        snapshot = runtime.refresh()

        self.assertEqual(21, snapshot["current_state"])
        self.assertEqual("safety_latched", runtime._state_row()["state_source"])

    def test_light_curtain_auto_reset_is_blocked_when_estop_is_active_too(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=9,
            requested_state=9,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"safety": {"latched": False, "phase": "ready"}},
        )
        self.io_store.upsert_value("esp32_plc58__I0_7", "0", "live", "test")
        self.io_store.upsert_value("esp32_plc58__I0_8", "0", "live", "test")

        with patch.object(runtime, "_pulse_esp_reset_output", return_value=None) as pulse:
            snapshot = runtime.refresh()

        pulse.assert_not_called()
        self.assertEqual(21, snapshot["current_state"])
        self.assertTrue(snapshot["purge_active"])
        self.assertEqual("1", self.params.get_effective_value("MAS0028"))

    def test_virtual_reset_ignores_start_button_mask_in_safety_context(self):
        runtime = self.build_runtime()
        self.params.set_value("MAP0065", "0000000", actor="microtom")
        runtime._write_state(
            current_state=21,
            requested_state=21,
            state_source="test",
            warning_active=False,
            purge_active=True,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"safety": {"latched": True}},
        )

        result = runtime.press_virtual_button("start_pause")

        self.assertTrue(result["ok"])
        self.assertEqual(2, result["command"])
        self.assertEqual("2", self.params.get_effective_value("MAS0002"))

    def test_virtual_reset_is_blocked_for_laser_until_system_ready_is_high(self):
        runtime = self.build_runtime()
        self.params.set_value("MAP0016", "1", actor="microtom")
        self.io_store.upsert_value("esp32_plc58__I0_12", "0", "simulation", "test")
        runtime._write_state(
            current_state=21,
            requested_state=21,
            state_source="test",
            warning_active=False,
            purge_active=True,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"safety": {"latched": True}},
        )

        with self.assertRaisesRegex(RuntimeError, "Laser System Ready"):
            runtime.press_virtual_button("start_pause")

        self.assertNotEqual("2", self.params.get_effective_value("MAS0002"))

    def test_direct_reset_command_is_blocked_for_laser_until_system_ready_is_high(self):
        runtime = self.build_runtime()
        self.params.set_value("MAP0016", "1", actor="microtom")
        self.io_store.upsert_value("esp32_plc58__I0_12", "0", "simulation", "test")
        runtime._write_state(
            current_state=21,
            requested_state=21,
            state_source="test",
            warning_active=False,
            purge_active=True,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"safety": {"latched": True}},
        )
        ok, msg = self.params.set_value("MAS0002", "2", actor="microtom")
        self.assertTrue(ok, msg)

        with patch.object(runtime, "_pulse_esp_reset_output", return_value=None) as pulse:
            snapshot = runtime.refresh()

        pulse.assert_not_called()
        self.assertEqual(21, snapshot["current_state"])
        self.assertEqual("failed", snapshot["info"]["safety"]["phase"])
        self.assertIn("Laser System Ready", snapshot["info"]["safety"]["last_reset"]["error"])
        self.assertEqual("0", self.params.get_effective_value("MAS0002"))

    def test_laser_reset_sequence_pulses_separate_reset_without_waiting_laser_ready(self):
        runtime = self.build_runtime()
        self.params.set_value("MAP0016", "1", actor="microtom")
        self.io_store.upsert_value("esp32_plc58__I0_12", "1", "simulation", "test")
        self.io_store.upsert_value("esp32_plc58__I0_2", "0", "simulation", "test")
        runtime._write_state(
            current_state=21,
            requested_state=21,
            state_source="test",
            warning_active=False,
            purge_active=True,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"safety": {"latched": True}},
        )
        self.params.apply_device_value("MAS0028", "1", promote_default=True)

        with patch("mas004_rpi_databridge.machine_runtime.time.sleep"), patch.object(
            runtime,
            "_start_reset_motion_recovery_background",
            return_value={"ok": True, "queued": True, "in_progress": True},
        ):
            result = runtime.press_virtual_button("start_pause")
            self.assertTrue(result["queued"])
            snapshot = runtime.refresh()

        self.assertEqual(9, snapshot["current_state"])
        last_reset = snapshot["info"]["safety"]["last_reset"]
        self.assertTrue(last_reset["ok"], last_reset)
        step = next(item for item in last_reset["steps"] if item.get("step") == "laser_safety_reset_and_start")
        self.assertTrue(step["ok"], step)
        self.assertTrue(any(item.get("step") == "laser_safety_reset_pulse" for item in step["steps"]))
        self.assertFalse(any(item.get("step") == "wait_laser_ready" for item in step["steps"]))
        self.assertFalse(any(item.get("step") == "laser_start_pulse" for item in step["steps"]))
        self.assertTrue(
            any(item.get("step") == "wait_esp_endpoint_after_reset_pulse" and item.get("ok") for item in last_reset["steps"])
        )
        self.assertEqual("0", self.io_store.get_point("moxa_e1213_1__DIO3")["value"])
        self.assertEqual("0", self.io_store.get_point("esp32_plc58__Q0_3")["value"])

    def test_safety_reset_retries_q02_pulse_after_esp_endpoint_recovers(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=21,
            requested_state=21,
            state_source="test",
            warning_active=False,
            purge_active=True,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"safety": {"latched": True}},
        )
        self.params.apply_device_value("MAS0028", "1", promote_default=True)

        wait_results = [
            {"ok": True, "source": "retry", "duration_s": 1.2},
            {"ok": True, "source": "after", "duration_s": 0.3},
        ]
        with patch.object(
            runtime,
            "_pulse_esp_reset_output",
            side_effect=[OSError(113, "No route to host"), {"ok": True}],
        ) as pulse, patch.object(
            runtime,
            "_wait_for_esp_command_endpoint",
            side_effect=wait_results,
        ) as wait, patch.object(
            runtime,
            "_start_reset_motion_recovery_background",
            return_value={"ok": True, "queued": True, "in_progress": True},
        ):
            result = runtime._perform_safety_reset(runtime._safety_status(runtime._io_values()), now_ts())

        self.assertTrue(result["ok"], result)
        self.assertEqual(2, pulse.call_count)
        self.assertEqual(2, wait.call_count)
        self.assertTrue(any(item.get("step") == "wait_esp_endpoint_before_reset_retry" for item in result["steps"]))
        self.assertTrue(any(item.get("step") == "wait_esp_endpoint_after_reset_pulse" for item in result["steps"]))

    def test_esp_endpoint_wait_uses_broker_ping(self):
        self.cfg.esp_simulation = False
        self.cfg.esp_host = "192.168.2.101"
        self.cfg.esp_port = 3010
        runtime = self.build_runtime()
        calls: list[tuple[str, int, float]] = []

        def fake_broker(host: str, port: int, timeout_s: float):
            calls.append((host, port, timeout_s))
            return {
                "warmup_reply": "PONG",
                "connected": True,
                "broker_supported": True,
                "queue_depth": 0,
                "reconnect_count": 1,
            }

        with patch(
            "mas004_rpi_databridge.machine_runtime.start_esp_command_broker",
            side_effect=fake_broker,
        ):
            result = runtime._wait_for_esp_command_endpoint(timeout_s=2.0, poll_s=0.05, source="test")

        self.assertTrue(result["ok"], result)
        self.assertEqual([("192.168.2.101", 3010, 0.2)], calls)
        self.assertEqual("PONG", result["attempts"][-1]["reply"])

    def test_failed_safety_reset_records_failure_event(self):
        runtime = self.build_runtime()

        with patch.object(runtime, "_pulse_esp_reset_output", return_value={"ok": True}), patch.object(
            runtime,
            "_wait_for_esp_command_endpoint",
            return_value={"ok": False, "error": "broker ping timeout"},
        ):
            result = runtime._perform_safety_reset({"reasons": []}, now_ts())

        self.assertFalse(result["ok"], result)
        self.assertEqual("broker ping timeout", result["error"])
        with self.db._conn() as c:
            row = c.execute(
                "SELECT severity,message,payload_json FROM machine_events WHERE event_type='safety_reset' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual("warning", row[0])
        self.assertEqual("Safety-Reset fehlgeschlagen", row[1])
        self.assertIn("broker ping timeout", row[2])

    def test_failed_safety_reset_diagnostics_are_kept_in_state_21(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=21,
            requested_state=21,
            state_source="safety_reset_failed",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={
                "safety": {
                    "latched": False,
                    "phase": "failed",
                    "last_reasons": [],
                    "last_reset": {"ok": False, "error": "broker ping timeout"},
                }
            },
        )

        snapshot = runtime.refresh()

        self.assertEqual(21, snapshot["current_state"])
        self.assertEqual("failed", snapshot["info"]["safety"]["phase"])
        self.assertEqual("broker ping timeout", snapshot["info"]["safety"]["last_reset"]["error"])

    def test_virtual_stop_button_is_not_reset_in_safety_context(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=21,
            requested_state=21,
            state_source="test",
            warning_active=False,
            purge_active=True,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"safety": {"latched": True}},
        )

        with self.assertRaisesRegex(RuntimeError, "blocked during reset"):
            runtime.press_virtual_button("stop")

        self.assertNotEqual("2", self.params.get_effective_value("MAS0002"))

    def test_physical_start_pause_resets_from_safety_context(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=21,
            requested_state=21,
            state_source="test",
            warning_active=False,
            purge_active=True,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"safety": {"latched": True}, "button_inputs": {"start_pause": False}},
        )
        self.io_store.upsert_value("raspi_plc21__I0_7", "1", "simulation", "test")

        with patch.object(runtime, "_perform_safety_reset", return_value={"ok": True, "steps": []}) as reset:
            input_result = runtime.refresh_physical_button_inputs(previous_inputs={"start_pause": False})
            self.assertTrue(input_result["accepted"], input_result)
            snapshot = runtime.refresh()

        reset.assert_called_once()
        self.assertEqual(9, snapshot["current_state"])
        self.assertFalse(snapshot["purge_active"])
        self.assertEqual("ready", snapshot["info"]["safety"]["phase"])
        self.assertEqual("0", self.params.get_effective_value("MAS0002"))
        with self.db._conn() as c:
            row = c.execute(
                "SELECT event_type,payload_json FROM machine_events WHERE event_type='physical_button'"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertIn('"command": 2', row[1])

    def test_physical_stop_button_is_not_reset_in_safety_context(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=21,
            requested_state=21,
            state_source="test",
            warning_active=False,
            purge_active=True,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"safety": {"latched": True}, "button_inputs": {"stop": False}},
        )
        self.io_store.upsert_value("raspi_plc21__I0_8", "1", "simulation", "test")

        with patch.object(runtime, "_perform_safety_reset", return_value={"ok": True, "steps": []}) as reset:
            input_result = runtime.refresh_physical_button_inputs(previous_inputs={"stop": False})
            self.assertFalse(input_result["accepted"], input_result)
            snapshot = runtime.refresh()

        reset.assert_not_called()
        self.assertEqual(21, snapshot["current_state"])
        self.assertNotEqual("2", self.params.get_effective_value("MAS0002"))

    def test_physical_reset_ignores_start_button_mask_in_safety_context(self):
        runtime = self.build_runtime()
        self.params.set_value("MAP0065", "0000000", actor="microtom")
        runtime._write_state(
            current_state=21,
            requested_state=21,
            state_source="test",
            warning_active=False,
            purge_active=True,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"safety": {"latched": True}, "button_inputs": {"start_pause": False}},
        )
        self.io_store.upsert_value("raspi_plc21__I0_7", "1", "simulation", "test")

        with patch.object(runtime, "_perform_safety_reset", return_value={"ok": True, "steps": []}) as reset:
            input_result = runtime.refresh_physical_button_inputs(previous_inputs={"start_pause": False})
            self.assertTrue(input_result["accepted"], input_result)
            snapshot = runtime.refresh()

        reset.assert_called_once()
        self.assertEqual("start_pause", input_result["request"]["button"])
        self.assertEqual(2, input_result["request"]["command"])

    def test_buffered_physical_button_edge_triggers_without_hardware_refresh(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=21,
            requested_state=21,
            state_source="test",
            warning_active=False,
            purge_active=True,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"safety": {"latched": True}, "button_inputs": {"start_pause": False}},
        )

        with patch.object(runtime, "_refresh_single_io_device") as refresh_single:
            result = runtime.process_physical_button_inputs(
                current_inputs={
                    "start_pause": True,
                    "stop": False,
                    "setup": False,
                    "sync": False,
                    "empty": False,
                    "rewind": False,
                },
                previous_inputs={"start_pause": False},
            )

        refresh_single.assert_not_called()
        self.assertTrue(result["accepted"], result)
        self.assertEqual("start_pause", result["request"]["button"])
        self.assertEqual("2", self.params.get_effective_value("MAS0002"))

    def test_button_led_writes_force_physical_sync(self):
        runtime = self.build_runtime()

        with patch("mas004_rpi_databridge.machine_runtime.IoRuntime") as runtime_cls:
            writer = runtime_cls.return_value
            writer.write_output.return_value = {"ok": True}

            runtime._apply_button_leds(21, {"start": True, "pause": True, "stop": True}, ts=0.0)

        self.assertTrue(writer.write_output.called)
        for call in writer.write_output.call_args_list:
            self.assertTrue(call.kwargs.get("force"), call)
            self.assertEqual("button-led", call.kwargs.get("source"))
            self.assertFalse(call.args[1], call)

    def test_button_led_plan_switches_old_colour_off_before_next_colour_on(self):
        runtime = self.build_runtime()
        runtime._button_led_last_plan = {
            "Q0.0": True,
            "Q0.1": False,
            "Q0.2": False,
            "Q0.3": False,
            "Q0.4": False,
            "Q0.5": False,
            "Q0.6": False,
            "Q0.7": False,
        }

        with patch("mas004_rpi_databridge.machine_runtime.IoRuntime") as runtime_cls:
            writer = runtime_cls.return_value
            writer.write_output.return_value = {"ok": True}

            runtime._apply_button_led_plan(
                {
                    "Q0.0": False,
                    "Q0.1": False,
                    "Q0.2": True,
                    "Q0.3": False,
                    "Q0.4": False,
                    "Q0.5": False,
                    "Q0.6": False,
                    "Q0.7": False,
                },
                force=False,
                source="test",
            )

        calls = writer.write_output.call_args_list
        self.assertEqual("raspi_plc21__Q0_0", calls[0].args[0])
        self.assertFalse(calls[0].args[1])
        self.assertEqual("raspi_plc21__Q0_2", calls[1].args[0])
        self.assertTrue(calls[1].args[1])

    def test_button_led_tick_updates_safety_blink_with_force(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=21,
            requested_state=21,
            state_source="test",
            warning_active=False,
            purge_active=True,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"safety": {"latched": True, "phase": "latched"}},
        )

        with patch("mas004_rpi_databridge.machine_runtime.IoRuntime") as runtime_cls:
            writer = runtime_cls.return_value
            writer.write_output.return_value = {"ok": True}

            result = runtime.refresh_button_led_outputs(ts=0.0)

        self.assertTrue(result["ok"])
        self.assertTrue(result["button_leds"]["Q0.0"])
        self.assertFalse(result["button_leds"]["Q0.2"])
        self.assertTrue(writer.write_output.called)
        for call in writer.write_output.call_args_list:
            self.assertTrue(call.kwargs.get("force"), call)
            self.assertEqual("button-led-tick", call.kwargs.get("source"))
        snapshot = runtime.snapshot()
        self.assertTrue(snapshot["info"]["button_leds"]["Q0.0"])
        self.assertFalse(snapshot["info"]["button_leds"]["Q0.2"])

    def test_button_led_tick_does_not_apply_stale_safety_ready_in_production_stop(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=9,
            requested_state=9,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"safety": {"latched": False, "phase": "ready"}},
        )

        with patch("mas004_rpi_databridge.machine_runtime.IoRuntime") as runtime_cls:
            writer = runtime_cls.return_value
            writer.write_output.return_value = {"ok": True}

            result = runtime.refresh_button_led_outputs(ts=0.0)

        self.assertTrue(result["ok"])
        self.assertFalse(result["safety_override"])
        self.assertFalse(result["button_leds"]["Q0.2"])
        self.assertFalse(result["button_leds"]["Q0.3"])
        self.assertTrue(result["button_leds"]["Q0.4"])
        self.assertFalse(result["button_leds"]["Q0.7"])

    def test_refresh_publishes_button_led_plan_without_physical_button_led_write(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=21,
            requested_state=21,
            state_source="test",
            warning_active=False,
            purge_active=True,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"safety": {"latched": True, "phase": "latched"}},
        )

        with patch("mas004_rpi_databridge.machine_runtime.IoRuntime") as runtime_cls:
            writer = runtime_cls.return_value
            writer.write_output.return_value = {"ok": True}

            snapshot = runtime.refresh()

        self.assertIn("button_leds", snapshot["info"])
        sources = [call.kwargs.get("source") for call in writer.write_output.call_args_list]
        self.assertNotIn("button-led", sources)
        self.assertNotIn("button-led-tick", sources)

    def test_unchanged_machine_state_is_written_only_on_slow_heartbeat(self):
        runtime = self.build_runtime()
        payload = {
            "current_state": 7,
            "requested_state": 7,
            "state_source": "test",
            "warning_active": False,
            "purge_active": False,
            "production_label": "JOB_TEST",
            "last_label_no": 0,
            "info": {"button_leds": {"Q0.0": False}},
        }

        with patch("mas004_rpi_databridge.machine_runtime.now_ts", return_value=100.0):
            runtime._write_state(**payload)
        first_ts = runtime._state_row()["updated_ts"]

        with patch("mas004_rpi_databridge.machine_runtime.now_ts", return_value=102.0):
            runtime._write_state(**payload)
        self.assertEqual(first_ts, runtime._state_row()["updated_ts"])

        with patch("mas004_rpi_databridge.machine_runtime.now_ts", return_value=106.0):
            runtime._write_state(**payload)
        self.assertEqual(106.0, runtime._state_row()["updated_ts"])

    def test_held_physical_start_pause_does_not_retrigger_without_new_edge(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=21,
            requested_state=21,
            state_source="test",
            warning_active=False,
            purge_active=True,
            production_label="JOB_TEST",
            last_label_no=0,
            info={
                "safety": {"latched": True, "physical_reset_seen_ts": 1.0},
                "button_inputs": {"start_pause": True},
            },
        )
        self.io_store.upsert_value("raspi_plc21__I0_7", "1", "simulation", "test")

        with patch("mas004_rpi_databridge.machine_runtime.now_ts", return_value=10.0), patch.object(
            runtime, "_perform_safety_reset", return_value={"ok": True, "steps": []}
        ) as reset:
            snapshot = runtime.refresh()

        reset.assert_not_called()
        self.assertEqual(21, snapshot["current_state"])
        self.assertEqual(1.0, snapshot["info"]["safety"]["physical_reset_seen_ts"])

    def test_esp_process_reset_retries_once_after_timeout(self):
        self.cfg.esp_simulation = False
        self.cfg.esp_host = "192.168.2.101"
        self.cfg.esp_port = 3010
        runtime = self.build_runtime()
        calls: list[tuple[str, float | None]] = []

        class FakeEspPlcClient:
            def __init__(self, *_args):
                pass

            def exchange_line(self, line, read_timeout_s=None):
                calls.append((line, read_timeout_s))
                if len(calls) == 1:
                    raise TimeoutError("timed out")
                return "ACK_PROCESS_RESET"

        with patch("mas004_rpi_databridge.machine_runtime.EspPlcClient", FakeEspPlcClient), patch(
            "mas004_rpi_databridge.machine_runtime.time.sleep"
        ):
            result = runtime._reset_esp_process_runtime()

        self.assertTrue(result["ok"], result)
        self.assertEqual(2, len(calls))
        self.assertEqual(["PROCESS RESET", "PROCESS RESET"], [item[0] for item in calls])
        self.assertEqual(False, result["attempts"][0]["ok"])
        self.assertEqual(True, result["attempts"][1]["ok"])

    def test_esp_q02_reset_pulse_uses_one_second_gap_between_pulses(self):
        runtime = self.build_runtime()
        writes: list[tuple[str, bool, bool, str]] = []

        class FakeIoRuntime:
            def __init__(self, *_args):
                pass

            def write_output(self, io_key, enabled, *, force=False, source="runtime", **_kwargs):
                writes.append((io_key, bool(enabled), bool(force), source))
                return {"ok": True}

        with patch("mas004_rpi_databridge.machine_runtime.IoRuntime", FakeIoRuntime), patch(
            "mas004_rpi_databridge.machine_runtime.time.sleep"
        ) as sleep:
            runtime._pulse_esp_reset_output()

        self.assertEqual(
            [
                ("esp32_plc58__Q0_2", True, True, "safety-reset"),
                ("esp32_plc58__Q0_2", False, True, "safety-reset"),
                ("esp32_plc58__Q0_2", True, True, "safety-reset"),
                ("esp32_plc58__Q0_2", False, True, "safety-reset"),
            ],
            writes,
        )
        self.assertEqual([0.2, 1.0, 0.2], [call.args[0] for call in sleep.call_args_list])

    def test_esp_q02_reset_pulses_laser_safety_reset_in_parallel_mode_when_ready(self):
        self.params.apply_device_value("MAP0079", "1", promote_default=True)
        with self.db._conn() as c:
            c.execute(
                "UPDATE io_values SET value=?, quality=?, source=?, updated_ts=? WHERE io_key=?",
                ("1", "simulation", "test", now_ts(), "esp32_plc58__I0_12"),
            )
        runtime = self.build_runtime()
        writes: list[tuple[str, bool, bool, str]] = []

        class FakeIoRuntime:
            def __init__(self, *_args):
                pass

            def write_output(self, io_key, enabled, *, force=False, source="runtime", **_kwargs):
                writes.append((io_key, bool(enabled), bool(force), source))
                return {"ok": True}

        with patch("mas004_rpi_databridge.machine_runtime.IoRuntime", FakeIoRuntime), patch.object(
            runtime,
            "_refresh_single_io_device",
            return_value={"ok": True},
        ), patch("mas004_rpi_databridge.machine_runtime.time.sleep"):
            result = runtime._pulse_esp_reset_output()

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["laser_parallel_reset"]["enabled"])
        self.assertEqual(
            [
                ("moxa_e1213_1__DIO3", True, True, "laser-safety-reset-parallel"),
                ("esp32_plc58__Q0_2", True, True, "safety-reset"),
                ("esp32_plc58__Q0_2", False, True, "safety-reset"),
                ("moxa_e1213_1__DIO3", False, True, "laser-safety-reset-parallel"),
                ("moxa_e1213_1__DIO3", True, True, "laser-safety-reset-parallel"),
                ("esp32_plc58__Q0_2", True, True, "safety-reset"),
                ("esp32_plc58__Q0_2", False, True, "safety-reset"),
                ("moxa_e1213_1__DIO3", False, True, "laser-safety-reset-parallel"),
            ],
            writes,
        )

    def test_virtual_setup_is_blocked_during_reset_context(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=21,
            requested_state=21,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"safety": {"latched": True}},
        )

        with self.assertRaisesRegex(RuntimeError, "blocked during reset"):
            runtime.press_virtual_button("setup")

        self.assertNotEqual("3", self.params.get_effective_value("MAS0002"))

    def test_label_complete_event_updates_register_and_mas0003(self):
        runtime = self.build_runtime()

        result = runtime.handle_event(
            {
                "type": "label_complete",
                "label_no": 12,
                "material_ok": 1,
                "print_ok": 0,
                "verify_ok": 1,
                "removed": 1,
                "production_ok": 0,
                "zero_mm": 0.0,
                "exit_mm": 1940.5,
            }
        )

        self.assertTrue(result["ok"])
        packed = pack_label_status_word(
            label_no=12,
            material_ok=True,
            print_ok=False,
            verify_ok=True,
            removed=True,
            production_ok=False,
        )
        self.assertEqual(str(packed), self.params.get_effective_value("MAS0003"))

        snapshot = runtime.snapshot()
        self.assertEqual(12, snapshot["last_label_no"])
        self.assertEqual(1, len(snapshot["labels"]))
        self.assertEqual(12, snapshot["labels"][0]["label_no"])

    def test_bad_label_complete_pauses_for_operator_removal(self):
        runtime = self.build_runtime()
        self.mark_production_active(runtime)
        runtime._pause_production_motion_after_print = Mock(
            return_value={"ok": True, "reason": "label_removal_required:10", "controlled": True}
        )
        runtime._sync_esp_machine_state = Mock(return_value=True)

        result = runtime.handle_event(
            {
                "type": "label_complete",
                "label_no": 10,
                "material_ok": 1,
                "print_ok": 1,
                "verify_ok": 0,
                "quality_ok": 0,
                "removed": 0,
                "should_remove": 1,
                "removal_pending": 1,
                "verify_bypass": 1,
                "control_bypass": 1,
                "zero_mm": 0.0,
                "exit_mm": 1540.0,
            }
        )

        self.assertTrue(result["ok"])
        runtime._pause_production_motion_after_print.assert_called_once_with(
            reason="label_removal_required:10",
            target_state=7,
        )
        snapshot = runtime.snapshot()
        self.assertEqual(7, snapshot["current_state"])
        self.assertEqual(7, snapshot["requested_state"])
        production_info = snapshot["info"][PRODUCTION_RUNTIME_INFO_KEY]
        self.assertFalse(production_info["active"])
        self.assertTrue(production_info["paused"])
        self.assertEqual(10, production_info["label_removal_request"]["label_no"])
        self.assertEqual("Label 10 entnehmen", production_info["label_removal_request"]["operator_message"])

        packed = pack_label_status_word(
            label_no=10,
            material_ok=True,
            print_ok=True,
            verify_ok=False,
            removed=False,
            production_ok=False,
        )
        self.assertEqual(str(packed), self.params.get_effective_value("MAS0003"))
        self.assertEqual("7", self.params.get_effective_value("MAS0001"))

    def test_label_removal_required_event_pauses_before_label_complete(self):
        runtime = self.build_runtime()
        self.mark_production_active(runtime)
        runtime._pause_production_motion_after_print = Mock(
            return_value={"ok": True, "reason": "label_removal_required:10", "controlled": True}
        )
        runtime._sync_esp_machine_state = Mock(return_value=True)

        result = runtime.handle_event(
            {
                "type": "label_removal_required",
                "label_no": 10,
                "reason": "verify_bypass_nok",
                "material_ok": 1,
                "print_ok": 1,
                "verify_ok": 0,
                "quality_ok": 0,
                "verify_triggered": 1,
                "verify_resolved": 1,
                "verify_bypass": 1,
                "control_bypass": 1,
            }
        )

        self.assertTrue(result["ok"])
        runtime._pause_production_motion_after_print.assert_called_once_with(
            reason="label_removal_required:10",
            target_state=7,
        )
        snapshot = runtime.snapshot()
        self.assertEqual(7, snapshot["current_state"])
        production_info = snapshot["info"][PRODUCTION_RUNTIME_INFO_KEY]
        self.assertEqual(10, production_info["label_removal_request"]["label_no"])
        self.assertEqual("verify_bypass_nok", production_info["label_removal_request"]["payload"]["reason"])

    def test_label_removal_required_during_pause_extends_removal_list(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=7,
            requested_state=7,
            state_source="label_removal_required",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=5,
            info={
                PRODUCTION_RUNTIME_INFO_KEY: {
                    "active": False,
                    "paused": True,
                    "pause_reason": "label_removal_required:5",
                    "label_removal_request": {"label_no": 5, "operator_message": "Label 5 entnehmen"},
                    "last_start": {"ok": True, "started_ts": now_ts() - 60.0},
                    "last_stop": {"reason": "label_removal_required:5", "finished_ts": now_ts() - 1.0},
                }
            },
        )

        result = runtime.handle_event(
            {
                "type": "label_removal_required",
                "label_no": 9,
                "reason": "verify_bypass_nok",
                "verify_ok": 0,
                "quality_ok": 0,
            }
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["accepted"])
        snapshot = runtime.snapshot()
        production_info = snapshot["info"][PRODUCTION_RUNTIME_INFO_KEY]
        self.assertEqual([5, 9], production_info["label_removal_pending_labels"])
        self.assertEqual("Labels 5, 9 entnehmen", production_info["label_removal_request"]["operator_message"])
        with self.db._conn() as c:
            stale_count = c.execute(
                "SELECT COUNT(*) FROM machine_events WHERE event_type='production_stale_event_ignored'"
            ).fetchone()[0]
        self.assertEqual(0, stale_count)

    def test_stale_label_complete_after_removal_pause_is_local_only(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=7,
            requested_state=7,
            state_source="label_removal_required",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=5,
            info={
                PRODUCTION_RUNTIME_INFO_KEY: {
                    "active": False,
                    "paused": True,
                    "pause_reason": "label_removal_required:5",
                    "label_removal_request": {"label_no": 5},
                    "last_start": {"ok": True, "started_ts": now_ts() - 60.0},
                    "last_stop": {"reason": "label_removal_required:5", "finished_ts": now_ts() - 1.0},
                }
            },
        )
        before = self.params.get_effective_value("MAS0003")

        result = runtime.handle_event(
            {
                "type": "label_complete",
                "label_no": 15,
                "material_ok": 1,
                "print_ok": 1,
                "verify_ok": 0,
                "quality_ok": 0,
                "removal_pending": 1,
                "zero_mm": 1474.849,
                "exit_mm": 3015.164,
            }
        )

        self.assertTrue(result["ok"])
        self.assertFalse(result["accepted"])
        self.assertEqual("stale_production_event", result["ignored"])
        self.assertFalse(result["recorded_label_only"]["forwarded_to_microtom"])
        self.assertEqual(before, self.params.get_effective_value("MAS0003"))
        snapshot = runtime.snapshot()
        self.assertEqual(7, snapshot["current_state"])
        self.assertEqual(15, snapshot["last_label_no"])
        with self.db._conn() as c:
            row = c.execute(
                "SELECT verify_ok,payload_json FROM label_register WHERE production_label=? AND label_no=?",
                ("JOB_TEST", 15),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(0, row[0])

    def test_label_removal_pause_supersedes_stale_refresh_snapshot(self):
        runtime = self.build_runtime()
        self.mark_production_active(runtime)
        old_snapshot = runtime._state_row()
        runtime._write_state(
            current_state=7,
            requested_state=7,
            state_source="label_removal_required",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=5,
            info={
                PRODUCTION_RUNTIME_INFO_KEY: {
                    "active": False,
                    "paused": True,
                    "pause_reason": "label_removal_required:5",
                    "label_removal_request": {"label_no": 5},
                }
            },
        )

        superseded = runtime._label_removal_state_superseded_snapshot(old_snapshot, 5)

        self.assertIsNotNone(superseded)
        self.assertEqual(7, superseded["current_state"])
        self.assertEqual("label_removal_required", superseded["state_source"])

    def test_stale_label_complete_after_production_stop_is_not_forwarded(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=21,
            requested_state=21,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=12,
            info={
                PRODUCTION_RUNTIME_INFO_KEY: {
                    "active": False,
                    "last_start": {"ok": True, "started_ts": now_ts() - 60.0},
                    "last_stop": {"reason": "test_stop", "finished_ts": now_ts() - 1.0},
                }
            },
        )
        before = self.params.get_effective_value("MAS0003")

        result = runtime.handle_event({"type": "label_complete", "label_no": 13})
        duplicate = runtime.handle_event({"type": "label_complete", "label_no": 14})

        self.assertTrue(result["ok"])
        self.assertFalse(result["accepted"])
        self.assertEqual("stale_production_event", result["ignored"])
        self.assertTrue(duplicate["deduped"])
        self.assertEqual(before, self.params.get_effective_value("MAS0003"))
        with self.db._conn() as c:
            count = c.execute(
                "SELECT COUNT(*) FROM machine_events WHERE event_type='production_stale_event_ignored'"
            ).fetchone()[0]
        self.assertEqual(1, count)

    def test_production_print_position_reached_dedupes_same_label(self):
        runtime = self.build_runtime()
        self.mark_production_active(runtime)
        runtime._prepare_next_production_wickler_takt = Mock(return_value={"ok": True, "prepared": True})

        first = runtime.handle_event(
            {
                "type": "production_print_position_reached",
                "label_no": 2,
                "infeed_speed_mm_s": 1.834,
                "drive_speed_mm_s": 1.782,
            }
        )
        duplicate = runtime.handle_event(
            {
                "type": "production_print_position_reached",
                "label_no": 2,
                "infeed_speed_mm_s": 1.834,
                "drive_speed_mm_s": 1.782,
            }
        )
        next_label = runtime.handle_event(
            {
                "type": "production_print_position_reached",
                "label_no": 3,
                "infeed_speed_mm_s": 1.763,
                "drive_speed_mm_s": 1.710,
            }
        )

        self.assertTrue(first["recorded"])
        self.assertTrue(duplicate["deduped"])
        self.assertEqual("duplicate_print_position_reached", duplicate["next_wickler_takt"]["skipped"])
        self.assertTrue(next_label["recorded"])
        self.assertEqual(2, runtime._prepare_next_production_wickler_takt.call_count)
        runtime._prepare_next_production_wickler_takt.assert_any_call(label_no=2, reason="after_print_position_reached")
        runtime._prepare_next_production_wickler_takt.assert_any_call(label_no=3, reason="after_print_position_reached")
        with self.db._conn() as c:
            rows = c.execute(
                "SELECT event_type,payload_json FROM machine_events WHERE event_type='production_print_position_reached' ORDER BY id"
            ).fetchall()
        self.assertEqual(2, len(rows))
        payloads = [json.loads(row[1]) for row in rows]
        self.assertEqual([2, 3], [payload["label_no"] for payload in payloads])

    def test_first_print_position_prepares_wicklers_before_esp_ready(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=5,
            requested_state=5,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={PRODUCTION_RUNTIME_INFO_KEY: {"active": True, "active_since_ts": 1234.5}},
        )
        runtime._prepare_next_production_wickler_takt = Mock(return_value={"ok": True, "prepared": True})
        runtime._production_esp_retry = Mock(return_value="ACK_PROCESS_PRODUCTION_WICKLER_READY")

        result = runtime.handle_event(
            {
                "type": "production_first_print_position_reached",
                "label_no": 1,
                "target_error_mm": 0.327,
                "infeed_speed_mm_s": 0.0,
                "drive_speed_mm_s": 0.0,
            }
        )

        self.assertTrue(result["recorded"])
        self.assertTrue(result["wickler_takt"]["ok"])
        self.assertTrue(result["esp_ready"]["ok"])
        runtime._prepare_next_production_wickler_takt.assert_called_once_with(
            label_no=1,
            reason="first_print_position_reached",
        )
        runtime._production_esp_retry.assert_called_once_with(
            "PROCESS PRODUCTION WICKLER_READY LABEL_NO=1",
            read_timeout_s=8.0,
            attempts=5,
            settle_s=0.2,
            priority=True,
        )

    def test_first_print_position_duplicate_does_not_reprepare_wicklers(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=5,
            requested_state=5,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={PRODUCTION_RUNTIME_INFO_KEY: {"active": True, "active_since_ts": 1234.5}},
        )
        runtime._prepare_next_production_wickler_takt = Mock(return_value={"ok": True, "prepared": True})
        runtime._production_esp_retry = Mock(return_value="ACK_PROCESS_PRODUCTION_WICKLER_READY")
        payload = {
            "type": "production_first_print_position_reached",
            "label_no": 1,
            "target_abs_mm": 1120.885,
            "target_error_mm": -0.047,
            "infeed_speed_mm_s": 0.0,
            "drive_speed_mm_s": 0.0,
        }

        first = runtime.handle_event(dict(payload))
        duplicate = runtime.handle_event(dict(payload))

        self.assertTrue(first["esp_ready"]["ok"])
        self.assertEqual("duplicate_first_print_position_reached", duplicate["wickler_takt"]["skipped"])
        self.assertEqual("already_handled", duplicate["esp_ready"]["skipped"])
        runtime._prepare_next_production_wickler_takt.assert_called_once()
        runtime._production_esp_retry.assert_called_once()

    def test_first_print_position_stale_event_in_stop_does_not_prepare_wicklers(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=9,
            requested_state=9,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={PRODUCTION_RUNTIME_INFO_KEY: {"active": False}},
        )
        runtime._prepare_next_production_wickler_takt = Mock(return_value={"ok": True})
        runtime._production_esp_retry = Mock(return_value="ACK_PROCESS_PRODUCTION_WICKLER_READY")

        result = runtime.handle_event(
            {
                "type": "production_first_print_position_reached",
                "label_no": 1,
                "target_abs_mm": 1120.885,
                "target_error_mm": -0.047,
            }
        )

        self.assertFalse(result["accepted"])
        self.assertEqual("stale_production_event", result["ignored"])
        runtime._prepare_next_production_wickler_takt.assert_not_called()
        runtime._production_esp_retry.assert_not_called()

    def test_first_print_position_failed_ready_attempt_is_not_retried_by_duplicate_event(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=5,
            requested_state=5,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={PRODUCTION_RUNTIME_INFO_KEY: {"active": True, "active_since_ts": 1234.5}},
        )
        runtime._prepare_next_production_wickler_takt = Mock(return_value={"ok": False, "error": "rewinder low"})
        runtime._production_esp_retry = Mock(return_value="ACK_PROCESS_PRODUCTION_WICKLER_READY")
        payload = {
            "type": "production_first_print_position_reached",
            "label_no": 1,
            "target_abs_mm": 1120.885,
            "target_error_mm": -0.047,
        }

        first = runtime.handle_event(dict(payload))
        duplicate = runtime.handle_event(dict(payload))

        self.assertFalse(first["wickler_takt"]["ok"])
        self.assertEqual("duplicate_first_print_position_reached", duplicate["wickler_takt"]["skipped"])
        runtime._prepare_next_production_wickler_takt.assert_called_once()
        runtime._production_esp_retry.assert_not_called()

    def test_production_esp_monitor_fallback_sends_first_wickler_ready(self):
        self.cfg.esp_simulation = False
        runtime = self.build_runtime()
        self.mark_production_active(runtime)
        production_info = {"active": True, "active_since_ts": 1234.5}
        runtime._prepare_next_production_wickler_takt = Mock(return_value={"ok": True, "prepared": True})

        def fake_esp(command, **_kwargs):
            if command == "PROCESS PRODUCTION MONITOR?":
                return (
                    'JSON {"active":true,"running":true,"phase":9,'
                    '"reason":"first_print_position_reached_wait_wickler",'
                    '"last_error":"","label_no":1,"wickler_ready_accepted":false,'
                    '"position_command_mm":620.178,"error_mm":-0.3324,'
                    '"infeed_speed_mm_s":0.0,"drive_speed_mm_s":0.0}'
                )
            if command == "PROCESS PRODUCTION WICKLER_READY LABEL_NO=1":
                return "ACK_PROCESS_PRODUCTION_WICKLER_READY"
            raise AssertionError(command)

        runtime._production_esp = Mock(side_effect=fake_esp)

        result = runtime._monitor_active_production_esp(production_info, 100.0)

        self.assertIsNone(result)
        fallback = production_info["esp_first_wickler_ready_fallback"]
        self.assertTrue(fallback["ok"], fallback)
        self.assertTrue(production_info["first_print_wickler_ready"]["esp_ready_ok"])
        runtime._prepare_next_production_wickler_takt.assert_called_once_with(
            label_no=1,
            reason="esp_diag_first_print_wait_fallback",
        )
        commands = [call.args[0] for call in runtime._production_esp.call_args_list]
        self.assertEqual(
            ["PROCESS PRODUCTION MONITOR?", "PROCESS PRODUCTION WICKLER_READY LABEL_NO=1"],
            commands,
        )
        with self.db._conn() as c:
            row = c.execute(
                "SELECT severity,event_type,message FROM machine_events "
                "WHERE event_type='production_first_print_ready_fallback'"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual("info", row[0])
        self.assertIn("Label 1", row[2])

    def test_production_esp_monitor_stops_on_runner_last_error(self):
        self.cfg.esp_simulation = False
        runtime = self.build_runtime()
        self.mark_production_active(runtime)
        started = runtime.production_logs.handle_param_change("MAS0002", "1")
        self.assertEqual("start", started["event"])
        production_info = {"active": True}
        runtime._production_esp = Mock(
            return_value=(
                'JSON {"active":true,"running":false,"phase":9,'
                '"reason":"first_print_position_reached_wait_wickler",'
                '"last_error":"wickler_indexed_ready_timeout","label_no":1}'
            )
        )
        runtime._stop_production_motion = Mock(return_value={"ok": True, "target_state": 21})
        runtime._notify_microtom = Mock()

        result = runtime._monitor_active_production_esp(production_info, 100.0)

        self.assertIsNotNone(result)
        self.assertFalse(result["ok"])
        self.assertEqual("wickler_indexed_ready_timeout", result["fault"])
        runtime._stop_production_motion.assert_called_once_with(
            reason="production_esp_runner_fault:wickler_indexed_ready_timeout",
            target_state=21,
        )
        manifest = runtime.production_logs.ready_manifest()
        self.assertFalse(manifest["active"])
        self.assertTrue(manifest["ready"])
        self.assertEqual("1", self.params.get_effective_value("MAS0028"))
        runtime._notify_microtom.assert_called_with("MAS0028", "1", dedupe_key="machine:MAS0028")
        with self.db._conn() as c:
            row = c.execute(
                "SELECT severity,event_type,message FROM machine_events "
                "WHERE event_type='production_esp_runner_fault'"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual("error", row[0])
        self.assertIn("wickler_indexed_ready_timeout", row[2])

    def test_production_esp_monitor_registration_timeout_pauses_without_purge(self):
        self.cfg.esp_simulation = False
        runtime = self.build_runtime()
        self.mark_production_active(runtime)
        production_info = {"active": True}
        runtime._production_esp = Mock(
            return_value=(
                'JSON {"active":true,"running":false,"phase":4,'
                '"last_error":"first_print_position_timeout","label_no":1,'
                '"initial_move_progress_mm":12.9,"position_command_mm":0.0,'
                '"motor_busy":false,"motor_ready":true}'
            )
        )
        runtime._stop_production_motion = Mock(return_value={"ok": True, "target_state": 7})
        runtime._sync_esp_machine_state = Mock(return_value=True)
        runtime._notify_microtom = Mock()

        result = runtime._monitor_active_production_esp(production_info, 100.0)

        self.assertIsNotNone(result)
        self.assertFalse(result["ok"])
        self.assertTrue(result["registration_fault_pause"])
        self.assertEqual(7, result["target_state"])
        runtime._stop_production_motion.assert_called_once_with(
            reason="registration_fault:first_print_position_timeout",
            target_state=7,
        )
        self.assertEqual("1", self.params.get_effective_value("MAE0048"))
        self.assertEqual("0", self.params.get_effective_value("MAS0028"))
        notify_calls = [(call.args[0], call.args[1]) for call in runtime._notify_microtom.call_args_list]
        self.assertIn(("MAE0048", "1"), notify_calls)
        self.assertNotIn(("MAS0028", "1"), notify_calls)
        with self.db._conn() as c:
            runner_fault = c.execute(
                "SELECT COUNT(*) FROM machine_events WHERE event_type='production_esp_runner_fault'"
            ).fetchone()[0]
            monitor_fault = c.execute(
                "SELECT severity,message FROM machine_events "
                "WHERE event_type='production_esp_monitor_registration_fault'"
            ).fetchone()
        self.assertEqual(0, runner_fault)
        self.assertIsNotNone(monitor_fault)
        self.assertEqual("warning", monitor_fault[0])
        self.assertIn("first_print_position_timeout", monitor_fault[1])

    def test_production_esp_monitor_label_removal_required_pauses_without_purge(self):
        self.cfg.esp_simulation = False
        runtime = self.build_runtime()
        self.mark_production_active(runtime)
        production_info = {"active": True}

        def fake_esp(command, **_kwargs):
            if command == "PROCESS PRODUCTION MONITOR?":
                return (
                    'JSON {"active":true,"running":false,"phase":0,'
                    '"reason":"completed","last_error":"label_removal_required:3",'
                    '"label_no":6,"labels_printed":6}'
                )
            if command in ("PROCESS WICKLER CANCEL", "PROCESS INDEXED STOP", "PROCESS PROFILE STOP"):
                return "ACK"
            raise AssertionError(command)

        runtime._production_esp = Mock(side_effect=fake_esp)
        runtime._stop_production_motion = Mock(return_value={"ok": True, "target_state": 21})
        runtime._set_production_wicklers_idle = Mock(return_value=[{"role": "unwinder", "ok": True}])
        runtime._sync_esp_machine_state = Mock(return_value=True)
        runtime._queue_tto_printer_state_sync = Mock(return_value={"queued": True})
        runtime._notify_microtom = Mock()

        result = runtime._monitor_active_production_esp(production_info, 100.0)

        self.assertIsNotNone(result)
        self.assertTrue(result["label_removal_pause"])
        self.assertEqual(7, result["target_state"])
        self.assertEqual([3], result["labels"])
        self.assertFalse(production_info["active"])
        self.assertTrue(production_info["paused"])
        self.assertEqual("label_removal_required:3", production_info["pause_reason"])
        self.assertEqual("0", self.params.get_effective_value("MAS0028"))
        runtime._stop_production_motion.assert_not_called()
        runtime._notify_microtom.assert_not_called()

    def test_production_esp_monitor_falls_back_to_status_for_old_firmware(self):
        self.cfg.esp_simulation = False
        runtime = self.build_runtime()
        self.mark_production_active(runtime)
        production_info = {"active": True}

        def fake_esp(command, **_kwargs):
            if command == "PROCESS PRODUCTION MONITOR?":
                raise RuntimeError("ESP rejected 'PROCESS PRODUCTION MONITOR?': NAK_Syntax")
            if command == "PROCESS PRODUCTION STATUS?":
                return (
                    'JSON {"running":true,"completed":false,"phase":5,'
                    '"current_label_no":1,"wickler_ready_accepted":false,'
                    '"last_error":"","position_issued":true,'
                    '"target_error_mm":-0.047}'
                )
            if command == "PROCESS PRODUCTION WICKLER_READY LABEL_NO=1":
                return "ACK_PROCESS_PRODUCTION_WICKLER_READY"
            raise AssertionError(command)

        runtime._prepare_next_production_wickler_takt = Mock(return_value={"ok": True, "prepared": True})
        runtime._production_esp = Mock(side_effect=fake_esp)

        result = runtime._monitor_active_production_esp(production_info, 100.0)

        self.assertIsNone(result)
        self.assertTrue(production_info["first_print_wickler_ready"]["esp_ready_ok"])
        commands = [call.args[0] for call in runtime._production_esp.call_args_list]
        self.assertEqual(
            [
                "PROCESS PRODUCTION MONITOR?",
                "PROCESS PRODUCTION STATUS?",
                "PROCESS PRODUCTION WICKLER_READY LABEL_NO=1",
            ],
            commands,
        )

    def test_position_axis_preflight_alarm_latches_axis_mae(self):
        runtime = self.build_runtime()
        runtime._notify_microtom = Mock()

        latch = runtime._latch_position_axis_preflight_fault(
            5,
            {
                "reason": "Achse im Alarm 48",
                "state": {
                    "alarm": True,
                    "alarm_code": 48,
                    "feedback_tenths_mm": 1,
                },
            },
            context="unit-test",
        )

        self.assertEqual("MAE0010", latch["pkey"])
        self.assertEqual("1", self.params.get_effective_value("MAE0010"))
        runtime._notify_microtom.assert_called_with("MAE0010", "1", dedupe_key="machine:MAE0010")

    def test_registration_fault_latches_mae0048_and_leaves_production(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=5,
            requested_state=5,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={PRODUCTION_RUNTIME_INFO_KEY: {"active": True}},
        )
        runtime._stop_production_motion = Mock(return_value={"ok": True, "reason": "registration_fault"})
        runtime._sync_esp_machine_state = Mock(return_value=True)
        runtime._notify_microtom = Mock()

        result = runtime.handle_event(
            {
                "type": "production_registration_fault",
                "reason": "print_registration_failed",
                "diag": {
                    "label_no": 2,
                    "error_mm": 0.0633,
                    "abs_error_mm": 0.0633,
                    "tolerance_mm": 0.05,
                    "target_mm": 610.0,
                    "progressed_mm": 609.937,
                    "registration_attempts": 3,
                    "max_attempts": 3,
                    "infeed_speed_mm_s": -0.004,
                    "motor_busy": False,
                    "motor_ready": True,
                },
            }
        )

        self.assertTrue(result["ok"])
        self.assertEqual("1", self.params.get_effective_value("MAE0048"))
        self.assertEqual("7", self.params.get_effective_value("MAS0001"))
        runtime._stop_production_motion.assert_called_once_with(
            reason="registration_fault:print_registration_failed",
            target_state=7,
        )
        snapshot = runtime.snapshot()
        self.assertEqual(7, snapshot["current_state"])
        self.assertIn("last_registration_fault", snapshot["info"][PRODUCTION_RUNTIME_INFO_KEY])

    def test_velocity_stop_keeps_wicklers_in_continuous_until_print_position_reached(self):
        runtime = self.build_runtime()
        self.mark_production_active(runtime)
        runtime._prepare_next_production_wickler_takt = Mock(return_value={"ok": True, "prepared": True})

        first = runtime.handle_event(
            {
                "type": "production_velocity_stop_for_print",
                "label_no": 1,
                "remaining_mm": 119.423,
                "infeed_speed_mm_s": 100.935,
                "drive_speed_mm_s": 100.802,
            }
        )
        duplicate = runtime.handle_event(
            {
                "type": "production_velocity_stop_for_print",
                "label_no": 1,
                "remaining_mm": 119.423,
                "infeed_speed_mm_s": 100.935,
                "drive_speed_mm_s": 100.802,
            }
        )

        self.assertTrue(first["recorded"])
        self.assertEqual("wait_until_print_position_reached", first["wickler_prepare"]["skipped"])
        self.assertTrue(duplicate["deduped"])
        self.assertEqual("wait_until_print_position_reached", duplicate["wickler_prepare"]["skipped"])
        runtime._prepare_next_production_wickler_takt.assert_not_called()

    def test_production_print_trigger_has_specific_deduped_event(self):
        runtime = self.build_runtime()
        self.mark_production_active(runtime)
        payload = {
            "type": "production_print_trigger",
            "label_no": 4,
            "bypass": True,
            "use_laser": False,
            "duration_ms": 2000,
            "position_error_mm": -0.0317,
        }

        first = runtime.handle_event(dict(payload))
        duplicate = runtime.handle_event(dict(payload))

        self.assertTrue(first["recorded"])
        self.assertTrue(duplicate["deduped"])
        with self.db._conn() as c:
            rows = c.execute(
                "SELECT event_type,message,payload_json FROM machine_events ORDER BY id"
            ).fetchall()
        self.assertEqual(1, len(rows))
        self.assertEqual("production_print_trigger", rows[0][0])
        self.assertIn("Drucktrigger Bypass", rows[0][1])

    def test_production_fault_event_logs_label_edge_timeout_details(self):
        runtime = self.build_runtime()
        self.mark_production_active(runtime)

        result = runtime.handle_event(
            {
                "type": "production_fault",
                "fault": "label_edge_timeout",
                "initial_label_level": 1,
                "label_sensor": 0,
                "label_acquire_mm": 169.873,
                "label_acquire_limit_mm": 250.0,
                "label_acquire_timeout_ms": 4500,
            }
        )

        self.assertTrue(result["ok"])
        self.assertEqual("label_edge_timeout", result["result"]["fault"])
        self.assertIn("Startpegel I0.5=1", result["result"]["message"])
        with self.db._conn() as c:
            row = c.execute(
                "SELECT severity,event_type,message FROM machine_events WHERE event_type='production_fault'"
            ).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual("warning", row[0])
        self.assertIn("169.9mm von 250.0mm", row[2])

    def test_label_length_error_pauses_production_without_purge(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=5,
            requested_state=5,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={},
        )
        self.params.apply_device_value("MAE0025", "1", promote_default=True)

        first = runtime.refresh()
        snapshot = runtime.refresh()

        self.assertEqual(6, first["current_state"])
        self.assertEqual(7, snapshot["current_state"])
        self.assertFalse(snapshot["purge_active"])
        self.assertEqual(["MAE0025"], snapshot["info"]["pause_reasons"])
        self.assertEqual("0", self.params.get_effective_value("MAS0028"))

    def test_registration_error_mae0048_pauses_without_purge(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=7,
            requested_state=7,
            state_source="production_registration_fault",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=1,
            info={PRODUCTION_RUNTIME_INFO_KEY: {"active": False}},
        )
        self.params.apply_device_value("MAS0001", "7", promote_default=True)
        self.params.apply_device_value("MAS0028", "0", promote_default=True)
        self.params.apply_device_value("MAE0048", "1", promote_default=True)

        snapshot = runtime.refresh()

        self.assertEqual(7, snapshot["current_state"])
        self.assertFalse(snapshot["purge_active"])
        self.assertEqual(["MAE0048"], snapshot["info"]["pause_reasons"])
        self.assertEqual([], snapshot["info"]["critical_reasons"])
        self.assertEqual("0", self.params.get_effective_value("MAS0028"))

    def test_esp_safety_ok_low_notaus_forces_state_21(self):
        runtime = self.build_runtime()
        self.io_store.upsert_value("esp32_plc58__I0_7", "0", "simulation", "test")

        snapshot = runtime.refresh()

        self.assertEqual(21, snapshot["current_state"])
        self.assertTrue(snapshot["purge_active"])
        self.assertIn("notaus", snapshot["info"]["critical_reasons"])
        self.assertEqual("1", self.params.get_effective_value("MAS0028"))

    def test_external_purge_clear_marks_runtime_and_clears_soft_latch(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=21,
            requested_state=21,
            state_source="test",
            warning_active=False,
            purge_active=True,
            production_label="JOB_TEST",
            last_label_no=0,
            info={},
        )

        mark_external_purge_clear(self.db)
        snapshot = runtime.snapshot()

        self.assertFalse(snapshot["purge_active"])
        self.assertTrue(recent_external_purge_clear(self.db))
        self.assertEqual("microtom", snapshot["info"]["purge"]["external_clear_source"])

    def test_external_purge_clear_does_not_reassert_from_stale_resettable_mae(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=21,
            requested_state=21,
            state_source="test",
            warning_active=False,
            purge_active=True,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"safety": {"latched": True, "phase": "latched"}},
        )
        self.params.apply_device_value("MAS0028", "1", promote_default=True)
        self.params.apply_device_value("MAE0048", "1", promote_default=True)

        mark_external_purge_clear(self.db, source="esp-plc")
        snapshot = runtime.refresh()

        self.assertFalse(snapshot["purge_active"])
        self.assertEqual("0", self.params.get_effective_value("MAS0028"))
        self.assertEqual("0", self.params.get_effective_value("MAE0048"))
        self.assertTrue(recent_external_purge_clear(self.db))

    def test_external_purge_clear_reasserts_for_live_notaus(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=21,
            requested_state=21,
            state_source="test",
            warning_active=False,
            purge_active=True,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"safety": {"latched": True, "phase": "latched"}},
        )
        self.io_store.upsert_value("esp32_plc58__I0_7", "0", "simulation", "test")
        self.params.apply_device_value("MAS0028", "1", promote_default=True)

        mark_external_purge_clear(self.db, source="esp-plc")
        snapshot = runtime.refresh()

        self.assertTrue(snapshot["purge_active"])
        self.assertIn("notaus", snapshot["info"]["critical_reasons"])
        self.assertEqual("1", self.params.get_effective_value("MAS0028"))

    def test_microtom_started_purge_is_not_stale_cleared_by_runtime(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=9,
            requested_state=9,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={
                "safety": {
                    "latched": False,
                    "phase": "ready",
                    "last_reset": {"ok": True},
                }
            },
        )

        mark_external_purge_start(self.db, source="microtom")
        snapshot = runtime.refresh()

        self.assertTrue(snapshot["purge_active"])
        self.assertEqual("1", self.params.get_effective_value("MAS0028"))
        self.assertEqual(21, snapshot["current_state"])
        self.assertEqual(0, self.outbox.count())

    def test_runtime_write_preserves_newer_external_purge_marker(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=9,
            requested_state=9,
            state_source="test",
            warning_active=False,
            purge_active=False,
            production_label="JOB_TEST",
            last_label_no=0,
            info={},
        )

        mark_external_purge_start(self.db, source="microtom")
        runtime._write_state(
            current_state=21,
            requested_state=21,
            state_source="safety_latched",
            warning_active=False,
            purge_active=True,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"safety": {"latched": True}},
        )

        purge_info = runtime.snapshot()["info"].get("purge") or {}
        self.assertEqual("microtom", purge_info.get("external_active_source"))
        self.assertGreater(float(purge_info.get("external_active_ts") or 0.0), 0.0)

    def test_runtime_suppresses_mas0028_clear_callback(self):
        runtime = self.build_runtime()
        runtime._notify_microtom("MAS0028", "0", dedupe_key="machine:MAS0028")

        self.assertEqual(0, self.outbox.count())

    def test_reset_clears_latched_etikettenfuehrung_errors_when_inputs_are_clear(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=21,
            requested_state=21,
            state_source="test",
            warning_active=False,
            purge_active=True,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"safety": {"latched": True}},
        )
        self.params.apply_device_value("MAS0028", "1", promote_default=True)
        self.params.apply_device_value("MAE0008", "1", promote_default=True)
        self.params.apply_device_value("MAE0009", "1", promote_default=True)

        result = runtime.press_virtual_button("start_pause")
        self.assertTrue(result["queued"])
        snapshot = runtime.refresh()

        self.assertEqual(9, snapshot["current_state"])
        self.assertFalse(snapshot["purge_active"])
        self.assertEqual("0", self.params.get_effective_value("MAS0028"))
        self.assertEqual("0", self.params.get_effective_value("MAE0008"))
        self.assertEqual("0", self.params.get_effective_value("MAE0009"))

    def test_reset_clears_etikettenfuehrung_error_even_when_input_active_outside_process(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=21,
            requested_state=21,
            state_source="test",
            warning_active=False,
            purge_active=True,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"safety": {"latched": True}},
        )
        self.io_store.upsert_value("esp32_plc58__I0_4", "1", "simulation", "test")
        self.params.apply_device_value("MAS0028", "1", promote_default=True)
        self.params.apply_device_value("MAE0008", "1", promote_default=True)

        result = runtime.press_virtual_button("start_pause")
        self.assertTrue(result["queued"])
        snapshot = runtime.refresh()

        self.assertEqual(9, snapshot["current_state"])
        self.assertFalse(snapshot["purge_active"])
        self.assertEqual("0", self.params.get_effective_value("MAS0028"))
        self.assertEqual("0", self.params.get_effective_value("MAE0008"))
        self.assertNotIn("bahnriss_einlauf", snapshot["info"]["critical_reasons"])

    def test_band_break_inputs_are_only_critical_after_setup_until_production_end(self):
        runtime = self.build_runtime()
        self.io_store.upsert_value("esp32_plc58__I0_4", "1", "simulation", "test")
        self.io_store.upsert_value("esp32_plc58__I0_11", "1", "simulation", "test")
        io_map = runtime._io_values()

        for state in (2, 3, 4, 6, 8, 9, 21):
            inactive_critical, inactive_reasons = runtime._critical_state(
                io_map,
                {"MAS0001": str(state), "MAE0008": "1", "MAE0009": "1"},
            )
            self.assertFalse(inactive_critical, f"state {state}")
            self.assertNotIn("bahnriss_einlauf", inactive_reasons)
            self.assertNotIn("bahnriss_auswurf", inactive_reasons)
            self.assertNotIn("MAE0008", inactive_reasons)
            self.assertNotIn("MAE0009", inactive_reasons)

        active_critical, active_reasons = runtime._critical_state(
            io_map,
            {"MAS0001": "5", "MAE0008": "1", "MAE0009": "1"},
        )
        self.assertTrue(active_critical)
        self.assertIn("bahnriss_einlauf", active_reasons)
        self.assertIn("bahnriss_auswurf", active_reasons)
        self.assertIn("MAE0008", active_reasons)
        self.assertIn("MAE0009", active_reasons)

    def test_wickler_dancer_low_is_critical_during_setup_but_not_stop(self):
        runtime = self.build_runtime()
        param_map = {"MAE0030": "1", "MAE0034": "1"}

        for state in (8, 9, 21):
            inactive_critical, inactive_reasons = runtime._critical_state(
                runtime._io_values(),
                {"MAS0001": str(state), **param_map},
            )
            self.assertFalse(inactive_critical, f"state {state}")
            self.assertNotIn("MAE0030", inactive_reasons)
            self.assertNotIn("MAE0034", inactive_reasons)

        for state in (2, 3):
            setup_critical, setup_reasons = runtime._critical_state(
                runtime._io_values(),
                {"MAS0001": str(state), **param_map},
            )
            self.assertTrue(setup_critical, f"state {state}")
            self.assertIn("MAE0030", setup_reasons)
            self.assertIn("MAE0034", setup_reasons)

        active_critical, active_reasons = runtime._critical_state(
            runtime._io_values(),
            {"MAS0001": "5", **param_map},
        )
        self.assertTrue(active_critical)
        self.assertIn("MAE0030", active_reasons)
        self.assertIn("MAE0034", active_reasons)

    def test_wickler_hard_endstop_monitor_latches_purge_during_setup(self):
        runtime = self.build_runtime()

        class FakeWicklerClient:
            def __init__(self, _cfg, role):
                self.role = role
                self.descriptor = SimpleNamespace(
                    label="Abwickler" if role == "unwinder" else "Aufwickler",
                    simulation_attr=f"{role}_simulation",
                )

            def available(self):
                return True

            def fetch_state(self, timeout_s=None):
                return {
                    "ok": True,
                    "telemetry": {
                        "modeLabel": "Bereit",
                        "modeCss": "ready",
                        "wipePercent": 1.5 if self.role == "unwinder" else 50.0,
                        "calibrated": True,
                        "requiresCalibration": False,
                    },
                    "drive": {"online": True, "alarm": False},
                    "values": {},
                }

        info: dict[str, object] = {}
        with patch("mas004_rpi_databridge.machine_runtime.SmartWicklerClient", FakeWicklerClient):
            monitor = runtime._monitor_wickler_hard_endstops(info, 2, now_ts())

        self.assertFalse(monitor["ok"], monitor)
        self.assertEqual("1", self.params.get_effective_value("MAS0028"))
        self.assertEqual("1", self.params.get_effective_value("MAE0030"))
        self.assertIn("MAE0030", monitor["latched_mae"])

    def test_reset_clears_purge_latch_and_queues_motion_recovery_without_blocking(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=21,
            requested_state=21,
            state_source="test",
            warning_active=False,
            purge_active=True,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"safety": {"latched": True}},
        )
        self.params.apply_device_value("MAS0028", "1", promote_default=True)
        self.params.apply_device_value("MAE0027", "1", promote_default=True)

        with patch.object(runtime, "_notify_microtom", wraps=runtime._notify_microtom) as notify, patch.object(
            runtime,
            "_start_reset_motion_recovery_background",
            return_value={"ok": True, "queued": True, "in_progress": True},
        ) as motion_recovery:
            result = runtime.press_virtual_button("start_pause")
            self.assertTrue(result["queued"])
            snapshot = runtime.refresh()

        self.assertEqual(9, snapshot["current_state"])
        self.assertFalse(snapshot["purge_active"])
        self.assertEqual("0", self.params.get_effective_value("MAS0028"))
        self.assertEqual("0", self.params.get_effective_value("MAE0027"))
        self.assertEqual("ready", snapshot["info"]["safety"]["phase"])
        self.assertTrue(snapshot["info"]["safety"]["last_reset"]["ok"])
        self.assertTrue(
            any(step.get("step") == "reset_motion_devices_background" for step in snapshot["info"]["safety"]["last_reset"]["steps"])
        )
        motion_recovery.assert_called_once()
        self.assertNotIn(("MAS0028", "0"), [(call.args[0], call.args[1]) for call in notify.call_args_list])

    def test_stale_purge_latch_is_not_auto_cleared_by_refresh(self):
        runtime = self.build_runtime()
        runtime._write_state(
            current_state=21,
            requested_state=21,
            state_source="test",
            warning_active=False,
            purge_active=True,
            production_label="JOB_TEST",
            last_label_no=0,
            info={"safety": {"latched": True, "phase": "latched", "last_reset": {"ok": True}}},
        )
        self.params.apply_device_value("MAS0028", "1", promote_default=True)

        with patch.object(runtime, "_notify_microtom", wraps=runtime._notify_microtom) as notify:
            snapshot = runtime.refresh()

        self.assertEqual(21, snapshot["current_state"])
        self.assertTrue(snapshot["purge_active"])
        self.assertEqual("1", self.params.get_effective_value("MAS0028"))
        self.assertTrue(snapshot["info"]["safety"]["latched"])
        self.assertEqual("latched", snapshot["info"]["safety"]["phase"])
        self.assertNotIn(("MAS0028", "0"), [(call.args[0], call.args[1]) for call in notify.call_args_list])

    def test_fault_motion_stop_is_one_shot_for_same_latched_reason(self):
        runtime = self.build_runtime()
        info: dict[str, object] = {}

        runtime._force_stop_process_motion_on_fault(info, ["MAS0028"], 1.0)
        runtime._force_stop_process_motion_on_fault(info, ["MAS0028"], 10.0)

        with self.db._conn() as c:
            rows = c.execute(
                "SELECT event_type,payload_json FROM machine_events WHERE event_type='fault_motion_stop'"
            ).fetchall()

        self.assertEqual(1, len(rows))
        self.assertEqual("MAS0028", info["last_fault_motion_stop_signature"])
        self.assertTrue(info["fault_motion_stop_state"]["ok"])

    def test_fault_motion_stop_dedupes_mas0028_signature_variants(self):
        runtime = self.build_runtime()
        info: dict[str, object] = {}

        runtime._force_stop_process_motion_on_fault(info, ["notaus", "lichtgitter"], 1.0)
        runtime._force_stop_process_motion_on_fault(info, ["notaus", "lichtgitter", "MAS0028"], 2.0)
        runtime._force_stop_process_motion_on_fault(info, ["MAS0028"], 3.0)

        with self.db._conn() as c:
            rows = c.execute(
                "SELECT event_type,payload_json FROM machine_events WHERE event_type='fault_motion_stop'"
            ).fetchall()

        self.assertEqual(1, len(rows))
        self.assertEqual(["lichtgitter", "notaus"], info["fault_motion_stop_state"]["reasons"])
        self.assertEqual(["MAS0028"], info["fault_motion_stop_state"]["latest_reasons"])

    def test_mae0027_is_only_critical_in_process_states(self):
        runtime = self.build_runtime()
        self.params.apply_device_value("MAE0027", "1", promote_default=True)

        self.params.apply_device_value("MAS0001", "21", promote_default=True)
        inactive_critical, inactive_reasons = runtime._critical_state(
            runtime._io_values(),
            runtime._param_values_by_prefix(("MAP", "MAS", "MAE", "MAW")),
        )
        self.assertFalse(inactive_critical)
        self.assertNotIn("MAE0027", inactive_reasons)

        self.params.apply_device_value("MAS0001", "5", promote_default=True)
        active_critical, active_reasons = runtime._critical_state(
            runtime._io_values(),
            runtime._param_values_by_prefix(("MAP", "MAS", "MAE", "MAW")),
        )
        self.assertTrue(active_critical)
        self.assertIn("MAE0027", active_reasons)

    def test_motion_reset_rejects_wickler_endstop_fault(self):
        runtime = self.build_runtime()
        calls: dict[str, list[str]] = {"unwinder": [], "rewinder": []}

        class FakeEspMotorClient:
            def __init__(self, _cfg):
                pass

            def available(self):
                return False

        class FakeWicklerClient:
            def __init__(self, _cfg, role):
                self.role = role

            def available(self):
                return True

            def post_mode(self, mode, timeout_s=None):
                calls[self.role].append(mode)
                return {"ok": True}

            def post_master(self, payload, timeout_s=None):
                calls[self.role].append(f"master:{payload}")
                return {"ok": True}

            def fetch_state(self):
                return {
                    "ok": True,
                    "drive": {"online": True, "ready": True, "alarm": False, "alarmCode": 0, "rawOutput": 32},
                    "telemetry": {"modeLabel": "Stop", "faultReason": "Wippe unten"},
                }

        with patch("mas004_rpi_databridge.machine_runtime.EspMotorClient", FakeEspMotorClient), patch(
            "mas004_rpi_databridge.machine_runtime.SmartWicklerClient", FakeWicklerClient
        ):
            result = runtime._reset_motion_devices()

        self.assertFalse(result["ok"], result)
        self.assertIn("not in safe stop", result["error"])
        self.assertEqual(["master:{'indexedModeEnabled': '0'}", "stop", "resetAlarm", "etoRecovery", "stop"], calls["unwinder"])
        self.assertEqual(["master:{'indexedModeEnabled': '0'}", "stop", "resetAlarm", "etoRecovery", "stop"], calls["rewinder"])

    def test_motion_reset_accepts_wickler_stop_even_when_drive_ready_bit_is_false(self):
        runtime = self.build_runtime()

        class FakeEspMotorClient:
            def __init__(self, _cfg):
                pass

            def available(self):
                return False

        class FakeWicklerClient:
            def __init__(self, _cfg, _role):
                pass

            def available(self):
                return True

            def post_mode(self, _mode, timeout_s=None):
                return {"ok": True}

            def post_master(self, _payload, timeout_s=None):
                return {"ok": True}

            def fetch_state(self):
                return {
                    "ok": True,
                    "drive": {"online": True, "ready": False, "move": False, "alarm": False, "alarmCode": 0, "rawOutput": 72},
                    "telemetry": {"modeLabel": "Stop", "faultReason": "AZD STOP Eingang aktiv"},
                }

        with patch("mas004_rpi_databridge.machine_runtime.EspMotorClient", FakeEspMotorClient), patch(
            "mas004_rpi_databridge.machine_runtime.SmartWicklerClient", FakeWicklerClient
        ):
            result = runtime._reset_motion_devices()

        self.assertTrue(result["ok"], result)
        for role_detail in result["details"]["wicklers"]:
            verify = [step for step in role_detail["steps"] if step.get("step") == "verify_safe_stop"][-1]
            self.assertTrue(verify["safe_stop"])
            self.assertFalse(verify["ready"])
            self.assertFalse(verify["move"])

    def test_motion_reset_skips_persistent_eto_apply_and_global_recover_when_readback_ok(self):
        self.cfg.esp_simulation = False
        runtime = self.build_runtime()
        calls: list[str] = []

        class FakeEspMotorClient:
            def __init__(self, _cfg):
                pass

            def available(self):
                return True

            def eto_recovery_status(self):
                calls.append("MOTOR ETO_RECOVERY?")
                return {"ok": True, "all_persisted_ready": True}

            def apply_eto_recovery(self):
                calls.append("MOTOR APPLY_ETO_RECOVERY")
                raise AssertionError("APPLY_ETO_RECOVERY must be skipped when readback is already OK")

            def recover_eto(self):
                calls.append("MOTOR RECOVER_ETO")
                raise AssertionError("global RECOVER_ETO must not be used during normal reset")

            def reset_alarm(self, motor_id):
                calls.append(f"MOTOR {int(motor_id)} RESET_ALARM")
                return {"ok": True, "reply": f"ACK_MOTOR_{int(motor_id)}_RESET_ALARM"}

            def recover_eto_motor(self, motor_id):
                calls.append(f"MOTOR {int(motor_id)} RECOVER_ETO")
                return {"ok": True, "reply": f"ACK_MOTOR_{int(motor_id)}_RECOVER_ETO"}

            def refresh(self, motor_id):
                motor_id = int(motor_id)
                return {
                    "ok": True,
                    "motor": {
                        "state": {
                            "link_ok": True,
                            "ready": motor_id != 3,
                            "alarm": False,
                            "alarm_code": 0,
                            "hwto": False,
                            "input_raw_hex": "4",
                            "output_raw_hex": "40",
                        }
                    },
                }

        class FakeWicklerClient:
            def __init__(self, _cfg, _role):
                pass

            def available(self):
                return False

        with patch("mas004_rpi_databridge.machine_runtime.EspMotorClient", FakeEspMotorClient), patch(
            "mas004_rpi_databridge.machine_runtime.SmartWicklerClient", FakeWicklerClient
        ):
            result = runtime._reset_motion_devices()

        self.assertTrue(result["ok"], result)
        self.assertIn("MOTOR ETO_RECOVERY?", calls)
        self.assertNotIn("MOTOR APPLY_ETO_RECOVERY", calls)
        self.assertNotIn("MOTOR RECOVER_ETO", calls)
        self.assertEqual([], [call for call in calls if call.endswith(" RESET_ALARM")])
        self.assertEqual([], [call for call in calls if call.endswith(" RECOVER_ETO") and call != "MOTOR RECOVER_ETO"])
        self.assertTrue(
            any(item.get("step") == "selective_recovery" for item in result["details"]["esp_motors"])
        )

    def test_motion_reset_accepts_motor3_operable_without_ready_bit(self):
        self.cfg.esp_simulation = False
        runtime = self.build_runtime()

        class FakeEspMotorClient:
            def __init__(self, _cfg):
                pass

            def available(self):
                return True

            def apply_eto_recovery(self):
                return {"ok": True, "reply": "ACK_MOTOR_APPLY_ETO_RECOVERY"}

            def recover_eto(self):
                return {"ok": True, "reply": "ACK_MOTOR_RECOVER_ETO"}

            def reset_alarm(self, motor_id):
                return {"ok": True, "reply": f"ACK_MOTOR_{int(motor_id)}_RESET_ALARM"}

            def recover_eto_motor(self, motor_id):
                return {"ok": True, "reply": f"ACK_MOTOR_{int(motor_id)}_RECOVER_ETO"}

            def refresh(self, motor_id):
                motor_id = int(motor_id)
                return {
                    "ok": True,
                    "motor": {
                        "state": {
                            "link_ok": True,
                            "ready": motor_id != 3,
                            "alarm": False,
                            "alarm_code": 0,
                            "hwto": False,
                            "input_raw_hex": "4",
                            "output_raw_hex": "40",
                        }
                    },
                }

        class FakeWicklerClient:
            def __init__(self, _cfg, _role):
                pass

            def available(self):
                return False

        with patch("mas004_rpi_databridge.machine_runtime.EspMotorClient", FakeEspMotorClient), patch(
            "mas004_rpi_databridge.machine_runtime.SmartWicklerClient", FakeWicklerClient
        ):
            result = runtime._reset_motion_devices()

        self.assertTrue(result["ok"], result)
        motor3_checks = [
            item
            for item in result["details"]["esp_motors"]
            if item.get("step") == "verify_ready" and item.get("motor_id") == 3
        ]
        self.assertTrue(motor3_checks, result)
        self.assertFalse(motor3_checks[-1]["ready"])
        self.assertFalse(motor3_checks[-1]["ready_required"])
        self.assertTrue(motor3_checks[-1]["operable"])

    def test_motion_reset_still_rejects_position_axis_without_ready_bit(self):
        self.cfg.esp_simulation = False
        runtime = self.build_runtime()

        class FakeEspMotorClient:
            def __init__(self, _cfg):
                pass

            def available(self):
                return True

            def apply_eto_recovery(self):
                return {"ok": True, "reply": "ACK_MOTOR_APPLY_ETO_RECOVERY"}

            def recover_eto(self):
                return {"ok": True, "reply": "ACK_MOTOR_RECOVER_ETO"}

            def reset_alarm(self, motor_id):
                return {"ok": True, "reply": f"ACK_MOTOR_{int(motor_id)}_RESET_ALARM"}

            def recover_eto_motor(self, motor_id):
                return {"ok": True, "reply": f"ACK_MOTOR_{int(motor_id)}_RECOVER_ETO"}

            def refresh(self, motor_id):
                motor_id = int(motor_id)
                return {
                    "ok": True,
                    "motor": {
                        "state": {
                            "link_ok": True,
                            "ready": motor_id != 2,
                            "alarm": False,
                            "alarm_code": 0,
                            "hwto": False,
                            "input_raw_hex": "4",
                            "output_raw_hex": "40",
                        }
                    },
                }

        class FakeWicklerClient:
            def __init__(self, _cfg, _role):
                pass

            def available(self):
                return False

        with patch("mas004_rpi_databridge.machine_runtime.EspMotorClient", FakeEspMotorClient), patch(
            "mas004_rpi_databridge.machine_runtime.SmartWicklerClient", FakeWicklerClient
        ):
            result = runtime._reset_motion_devices()

        self.assertFalse(result["ok"], result)
        self.assertIn("Motor 2 not ready/operable", result["error"])

    def test_motion_reset_rejects_position_axis_reference_outside_limits(self):
        self.cfg.esp_simulation = False
        runtime = self.build_runtime()

        class FakeEspMotorClient:
            def __init__(self, _cfg):
                pass

            def available(self):
                return True

            def apply_eto_recovery(self):
                return {"ok": True, "reply": "ACK_MOTOR_APPLY_ETO_RECOVERY"}

            def recover_eto(self):
                return {"ok": True, "reply": "ACK_MOTOR_RECOVER_ETO"}

            def reset_alarm(self, motor_id):
                return {"ok": True, "reply": f"ACK_MOTOR_{int(motor_id)}_RESET_ALARM"}

            def recover_eto_motor(self, motor_id):
                return {"ok": True, "reply": f"ACK_MOTOR_{int(motor_id)}_RECOVER_ETO"}

            def refresh(self, motor_id):
                motor_id = int(motor_id)
                feedback = 3601 if motor_id == 2 else 0
                max_tenths = 990 if motor_id == 2 else 1540
                return {
                    "ok": True,
                    "motor": {
                        "positional": motor_id != 3,
                        "config": {
                            "min_enabled": True,
                            "max_enabled": True,
                            "min_tenths_mm": 0,
                            "max_tenths_mm": max_tenths,
                        },
                        "state": {
                            "link_ok": True,
                            "ready": motor_id != 3,
                            "alarm": False,
                            "alarm_code": 0,
                            "hwto": False,
                            "feedback_tenths_mm": feedback,
                            "input_raw_hex": "4",
                            "output_raw_hex": "40",
                        },
                    },
                }

        class FakeWicklerClient:
            def __init__(self, _cfg, _role):
                pass

            def available(self):
                return False

        with patch("mas004_rpi_databridge.machine_runtime.EspMotorClient", FakeEspMotorClient), patch(
            "mas004_rpi_databridge.machine_runtime.SmartWicklerClient", FakeWicklerClient
        ):
            result = runtime._reset_motion_devices()

        self.assertFalse(result["ok"], result)
        self.assertIn("position_reference_ok=False", result["error"])
        self.assertEqual("1", self.params.get_effective_value("MAE0005"))
        id2_checks = [
            item
            for item in result["details"]["esp_motors"]
            if item.get("step") == "verify_ready" and item.get("motor_id") == 2
        ]
        self.assertTrue(id2_checks, result)
        self.assertFalse(id2_checks[-1]["ok"])
        self.assertFalse(id2_checks[-1]["position_reference_ok"])
        self.assertEqual("position_reference_outside_limits", id2_checks[-1]["position_reference"]["reason"])


if __name__ == "__main__":
    unittest.main()

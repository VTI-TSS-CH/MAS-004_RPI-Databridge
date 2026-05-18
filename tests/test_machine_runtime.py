from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from types import SimpleNamespace
from unittest.mock import patch

sys.modules.setdefault("ping3", SimpleNamespace(ping=lambda *_args, **_kwargs: 1.0))

from mas004_rpi_databridge.config import Settings
from mas004_rpi_databridge.db import DB, now_ts
from mas004_rpi_databridge.io_master import IoStore
from mas004_rpi_databridge.logstore import LogStore
from mas004_rpi_databridge.machine_runtime import MachineRuntime, mark_external_purge_clear, recent_external_purge_clear
from mas004_rpi_databridge.machine_semantics import pack_label_status_word
from mas004_rpi_databridge.outbox import Outbox
from mas004_rpi_databridge.params import ParamStore


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
            ("MAP0014", "MAP", "0014", "100", "W", "R", "uint16"),
            ("MAP0016", "MAP", "0016", "0", "W", "R", "bool"),
            ("MAP0019", "MAP", "0019", "11000", "W", "R", "uint16"),
            ("MAP0040", "MAP", "0040", "5", "W", "R", "uint8"),
            ("MAP0065", "MAP", "0065", "1111111", "W", "R", "uint8"),
            ("MAP0066", "MAP", "0066", "8000", "W", "R", "uint16"),
            ("MAE0008", "MAE", "0008", "0", "R", "W", "bool"),
            ("MAE0009", "MAE", "0009", "0", "R", "W", "bool"),
            ("MAE0025", "MAE", "0025", "0", "R", "W", "bool"),
            ("MAE0026", "MAE", "0026", "0", "R", "W", "bool"),
            ("MAE0027", "MAE", "0027", "0", "R", "W", "bool"),
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
        for pin, value in (("I0.4", "0"), ("I0.7", "0"), ("I0.8", "0"), ("I0.11", "0")):
            _insert_io_point(self.db, "esp32_plc58", "ESP32 PLC 58", pin, "input", value)
        _insert_io_point(self.db, "esp32_plc58", "ESP32 PLC 58", "Q0.2", "output", "0")
        for pin in ("DO4", "DO5", "DO6"):
            _insert_io_point(self.db, "moxa_e1211_2", "Moxa ioLogik E1211 #2", pin, "output", "0")

    def tearDown(self):
        self._tmp.cleanup()

    def build_runtime(self) -> MachineRuntime:
        return MachineRuntime(self.cfg, self.db, self.params, self.io_store, self.logs, self.outbox)

    def test_microtom_command_is_mapped_to_transition_and_final_state(self):
        runtime = self.build_runtime()

        ok, msg = self.params.set_value("MAS0002", "3", actor="microtom")
        self.assertTrue(ok, msg)

        with patch("mas004_rpi_databridge.machine_runtime.TemporaryProcessCommandController") as controller_cls:
            controller = controller_cls.return_value
            controller.execute.return_value = "ACK_MAC0001=1"
            first = runtime.refresh()
            controller.execute.assert_called_once_with("1")
        self.assertEqual(6, first["current_state"])
        self.assertEqual(7, first["requested_state"])
        self.assertEqual(7, first["info"]["requested_command"])
        self.assertEqual(True, first["info"]["setup"]["last_result"]["ok"])
        self.assertEqual(True, first["info"]["setup"]["parameters_ready"])

        second = runtime.refresh()
        self.assertEqual(7, second["current_state"])
        self.assertEqual(7, second["requested_state"])
        self.assertEqual(8000, second["info"]["format_plan"]["process"]["led_strip_first_led_distance_tenths_mm"])

    def test_failed_setup_workflow_returns_to_stop_instead_of_sticking_in_transition(self):
        runtime = self.build_runtime()

        ok, msg = self.params.set_value("MAS0002", "3", actor="microtom")
        self.assertTrue(ok, msg)

        with patch("mas004_rpi_databridge.machine_runtime.TemporaryProcessCommandController") as controller_cls:
            controller = controller_cls.return_value
            controller.execute.return_value = "MAC0001=NAK_DeviceComm"
            snapshot = runtime.refresh()

        self.assertEqual(8, snapshot["current_state"])
        self.assertEqual(9, snapshot["requested_state"])
        self.assertEqual(2, snapshot["info"]["requested_command"])
        self.assertEqual(False, snapshot["info"]["setup"]["last_result"]["ok"])
        self.assertEqual("MAC0001=NAK_DeviceComm", snapshot["info"]["setup"]["last_result"]["response"])
        self.assertEqual(9, runtime.refresh()["current_state"])

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

    def test_virtual_setup_runs_wickler_workflow_even_when_already_in_setup(self):
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

        with patch("mas004_rpi_databridge.machine_runtime.TemporaryProcessCommandController") as controller_cls:
            controller = controller_cls.return_value
            controller.execute.return_value = "ACK_MAC0001=1"
            result = runtime.press_virtual_button("setup")
            controller.execute.assert_called_once_with("1")

        self.assertTrue(result["ok"])
        self.assertEqual("setup", result["button"])
        self.assertEqual(3, result["command"])
        self.assertEqual(True, result["snapshot"]["info"]["setup"]["last_result"]["ok"])

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

    def test_esp_active_high_notaus_forces_state_21(self):
        runtime = self.build_runtime()
        self.io_store.upsert_value("esp32_plc58__I0_7", "1", "simulation", "test")

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

        snapshot = result["snapshot"]
        self.assertEqual(9, snapshot["current_state"])
        self.assertFalse(snapshot["purge_active"])
        self.assertEqual("0", self.params.get_effective_value("MAS0028"))
        self.assertEqual("0", self.params.get_effective_value("MAE0008"))
        self.assertEqual("0", self.params.get_effective_value("MAE0009"))

    def test_reset_keeps_etikettenfuehrung_error_when_input_is_still_active(self):
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

        snapshot = result["snapshot"]
        self.assertEqual(21, snapshot["current_state"])
        self.assertTrue(snapshot["purge_active"])
        self.assertEqual("1", self.params.get_effective_value("MAS0028"))
        self.assertEqual("1", self.params.get_effective_value("MAE0008"))
        self.assertIn("bahnriss_einlauf", snapshot["info"]["critical_reasons"])

    def test_reset_clears_purge_latch_even_if_motion_recovery_fails_without_critical_input(self):
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

        with patch.object(
            runtime,
            "_reset_motion_devices",
            return_value={"ok": False, "error": "simulated motion recovery failure", "details": {}},
        ):
            result = runtime.press_virtual_button("start_pause")

        snapshot = result["snapshot"]
        self.assertEqual(21, snapshot["current_state"])
        self.assertFalse(snapshot["purge_active"])
        self.assertEqual("0", self.params.get_effective_value("MAS0028"))
        self.assertEqual("0", self.params.get_effective_value("MAE0027"))
        self.assertEqual("failed", snapshot["info"]["safety"]["phase"])

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

    def test_motion_reset_leaves_wicklers_in_safe_stop(self):
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

        self.assertTrue(result["ok"], result)
        self.assertEqual(["master:{'indexedModeEnabled': '0'}", "stop", "resetAlarm", "etoRecovery", "stop"], calls["unwinder"])
        self.assertEqual(["master:{'indexedModeEnabled': '0'}", "stop", "resetAlarm", "etoRecovery", "stop"], calls["rewinder"])


if __name__ == "__main__":
    unittest.main()

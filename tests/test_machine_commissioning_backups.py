import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from mas004_rpi_databridge.commissioning import CommissioningStore, STEP_TEMPLATES
from mas004_rpi_databridge.config import Settings
from mas004_rpi_databridge.db import DB, now_ts
from mas004_rpi_databridge.machine_backups import MachineBackupManager


class MachineCommissioningBackupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cfg_path = self.root / "config.json"
        self.db_path = self.root / "databridge.db"
        self.backup_root = self.root / "backups"
        self.params_master = self.root / "master" / "Parameterliste_master.xlsx"
        self.ios_master = self.root / "master" / "SAR41-MAS-004_SPS_I-Os.xlsx"
        self.cfg_path.write_text(
            json.dumps(
                {
                    "db_path": str(self.db_path),
                    "master_params_xlsx_path": str(self.params_master),
                    "master_ios_xlsx_path": str(self.ios_master),
                    "backup_root_path": str(self.backup_root),
                    "machine_serial_number": "MAS004-UT",
                    "machine_name": "Unit Test Rig",
                    "peer_base_url": "http://192.168.210.10:81",
                    "eth0_ip": "192.168.210.20",
                    "eth1_ip": "192.168.2.100",
                    "esp_simulation": True,
                    "moxa1_simulation": True,
                    "moxa2_simulation": True,
                    "vj6530_simulation": True,
                    "vj3350_simulation": True,
                    "smart_unwinder_simulation": True,
                    "smart_rewinder_simulation": True,
                }
            ),
            encoding="utf-8",
        )
        self.cfg = Settings.load(str(self.cfg_path))
        self.db = DB(str(self.db_path))

    def tearDown(self):
        self._tmp.cleanup()

    def test_commissioning_incomplete_run_reuses_success_steps(self):
        store = CommissioningStore(self.db, self.cfg, str(self.cfg_path))

        run1 = store.start_run("full")
        self.assertEqual(len(STEP_TEMPLATES), len(run1["steps"]))

        updated = store.update_step(run1["run_id"], "raspi_identity", "success", note="Seriennummer gepflegt")
        updated = store.auto_check_step(updated["run_id"], "esp_endpoint")
        step_map = {step["step_id"]: step for step in updated["steps"]}
        self.assertEqual("success", step_map["raspi_identity"]["status"])
        self.assertEqual("success", step_map["esp_endpoint"]["status"])

        run2 = store.start_run("incomplete_only")
        step_map_2 = {step["step_id"]: step for step in run2["steps"]}
        self.assertEqual("reused", step_map_2["raspi_identity"]["status"])
        self.assertEqual("reused", step_map_2["esp_endpoint"]["status"])
        self.assertEqual("pending", step_map_2["motor_setup"]["status"])

    def test_commissioning_auto_checks_report_simulation_and_workbooks(self):
        self.params_master.parent.mkdir(parents=True, exist_ok=True)
        self.params_master.write_bytes(b"dummy-params")
        self.ios_master.write_bytes(b"dummy-ios")
        with self.db._conn() as conn:
            conn.execute(
                """INSERT INTO io_points(
                   io_key, device_code, device_label, sheet_name, zone_label, pin_label, io_dir,
                   channel_no, function_text, is_reserved, is_active, source_row, updated_ts
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "ESP:I0.0",
                    "ESP32",
                    "ESP32 PLC58",
                    "ESP32",
                    "Inputs",
                    "I0.0",
                    "IN",
                    0,
                    "Test input",
                    0,
                    1,
                    2,
                    now_ts(),
                ),
            )

        store = CommissioningStore(self.db, self.cfg, str(self.cfg_path))
        run = store.start_run("full")
        run = store.auto_check_step(run["run_id"], "raspi_network")
        run = store.auto_check_step(run["run_id"], "workbooks_loaded")
        run = store.auto_check_step(run["run_id"], "moxa1_endpoint")
        run = store.auto_check_step(run["run_id"], "peer_secondary_health")
        step_map = {step["step_id"]: step for step in run["steps"]}
        self.assertEqual("success", step_map["raspi_network"]["status"])
        self.assertEqual("success", step_map["workbooks_loaded"]["status"])
        self.assertEqual("success", step_map["moxa1_endpoint"]["status"])
        self.assertTrue(step_map["moxa1_endpoint"]["result"]["simulation"])
        self.assertEqual("success", step_map["peer_secondary_health"]["status"])
        self.assertTrue(step_map["peer_secondary_health"]["result"]["optional"])

    def test_settings_backup_contains_runtime_payload(self):
        self.params_master.parent.mkdir(parents=True, exist_ok=True)
        self.params_master.write_bytes(b"dummy-params")
        self.ios_master.write_bytes(b"dummy-ios")
        runtime_root = self.db_path.parent
        (runtime_root / "motor_ui_state.json").write_text('{"motor": true}', encoding="utf-8")
        production_dir = runtime_root / "production_logs"
        production_dir.mkdir(parents=True, exist_ok=True)
        (production_dir / "_production_state.json").write_text('{"prod": true}', encoding="utf-8")

        mgr = MachineBackupManager(self.db, self.cfg, str(self.cfg_path))
        backup = mgr.create_settings_backup("baseline", note="Unit-Test")

        self.assertTrue(Path(backup["file_path"]).exists())
        self.assertEqual("settings", backup["backup_type"])
        with zipfile.ZipFile(backup["file_path"], "r") as zf:
            names = set(zf.namelist())
            self.assertIn("manifest.json", names)
            self.assertIn("config/settings.json", names)
            self.assertIn("db/databridge.db", names)
            self.assertIn("master/Parameterliste_master.xlsx", names)
            self.assertIn("master/SAR41-MAS-004_SPS_I-Os.xlsx", names)
            self.assertIn("state/motor_ui_state.json", names)
            self.assertIn("state/production_state.json", names)

    def test_commissioning_templates_cover_mas004_component_groups(self):
        step_ids = {item.step_id for item in STEP_TEMPLATES}
        for required in {
            "peer_primary_health",
            "esp_io_image",
            "motor_axes_xz",
            "motor_label_drive",
            "encoder_transport_infeed",
            "sensor_label_detect",
            "camera_material_tv1",
            "tto_io_handshake",
            "winders_stop_io",
            "machine_state_flow",
            "production_logging_export",
        }:
            self.assertIn(required, step_ids)


if __name__ == "__main__":
    unittest.main()

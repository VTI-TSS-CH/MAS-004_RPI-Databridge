import tempfile
import unittest
from pathlib import Path

import openpyxl

from mas004_rpi_databridge.db import DB, now_ts
from mas004_rpi_databridge.params import ParamStore, SHEET_NAME, motor_param_master_write_context


class ParamAccessTests(unittest.TestCase):
    def test_microtom_and_esp_access_are_evaluated_separately(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DB(str(Path(tmpdir) / "db.sqlite3"))
            store = ParamStore(db)

            with db._conn() as c:
                c.execute(
                    """INSERT INTO params(
                        pkey,ptype,pid,min_v,max_v,default_v,unit,rw,esp_rw,dtype,name,format_relevant,
                        message,possible_cause,effects,remedy,updated_ts
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "TTP00002",
                        "TTP",
                        "00002",
                        0,
                        100,
                        "55",
                        None,
                        "W",
                        "R",
                        "unsigned int.",
                        "SomeParam",
                        "NO",
                        None,
                        None,
                        None,
                        None,
                        now_ts(),
                    ),
                )

            self.assertTrue(store.can_actor_write("TTP00002", actor="microtom"))
            self.assertTrue(store.can_actor_read("TTP00002", actor="esp32"))
            self.assertFalse(store.can_actor_write("TTP00002", actor="esp32"))
            self.assertEqual((False, "NAK_ReadOnly"), store.validate_write("TTP00002", "10", actor="esp32"))

    def _insert_position_motor_param(self, db: DB, pkey: str, default_v: str = "1000"):
        with db._conn() as c:
            c.execute(
                """INSERT INTO params(
                    pkey,ptype,pid,min_v,max_v,default_v,unit,rw,esp_rw,dtype,name,format_relevant,
                    message,possible_cause,effects,remedy,updated_ts
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    pkey,
                    pkey[:3],
                    pkey[3:],
                    100,
                    1000,
                    default_v,
                    "1/10mm",
                    "R/W",
                    "R/W",
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

    def test_position_motor_params_are_setup_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DB(str(Path(tmpdir) / "db.sqlite3"))
            store = ParamStore(db)
            self._insert_position_motor_param(db, "MAP0063")

            self.assertEqual((False, "NAK_MotorSetupOnly"), store.set_value("MAP0063", "910", actor="microtom"))
            self.assertEqual((False, "NAK_MotorSetupOnly"), store.update_meta("MAP0063", default_v="910", max_v=910))

            ok, msg = store.apply_device_value("MAP0063", "910", promote_default=True)
            self.assertEqual((True, "OK"), (ok, msg))
            self.assertEqual("910", store.get_value("MAP0063"))
            self.assertEqual("1000", store.get_meta("MAP0063")["default_v"])

            with motor_param_master_write_context("test_motor_setup"):
                self.assertEqual((True, "OK"), store.update_meta("MAP0063", default_v="995", max_v=1000))
                self.assertEqual((True, "OK"), store.set_value("MAP0063", "995", actor="microtom"))
            self.assertEqual("995", store.get_meta("MAP0063")["default_v"])
            self.assertEqual("995", store.get_value("MAP0063"))

    def test_import_preserves_existing_position_motor_defaults_and_limits(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db = DB(str(root / "db.sqlite3"))
            store = ParamStore(db)
            self._insert_position_motor_param(db, "MAP0063")

            workbook = root / "params.xlsx"
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = SHEET_NAME
            ws.append(["Params_Type.", "Param ID.", "Min.", "Max.", "Default Value", "R/W:", "ESP32 R/W:"])
            ws.append(["MAP", "0063", 0, 910, "910", "R/W", "R/W"])
            wb.save(workbook)

            result = store.import_xlsx(str(workbook))

            self.assertTrue(result["ok"], result)
            self.assertEqual(1, result["protected_motor_params_preserved"])
            meta = store.get_meta("MAP0063")
            self.assertEqual("1000", meta["default_v"])
            self.assertEqual(100.0, meta["min_v"])
            self.assertEqual(1000.0, meta["max_v"])


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mas004_rpi_databridge.db import DB, now_ts
from mas004_rpi_databridge.logstore import LogStore
from mas004_rpi_databridge.outbox import Outbox
from mas004_rpi_databridge.params import ParamStore
from mas004_rpi_databridge.production_logs import ProductionLogManager, production_file_name


def _insert_param(db: DB, pkey: str, ptype: str, pid: str, default_v: str | None, rw: str, esp_rw: str = "W", dtype: str = "string"):
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


class ProductionLogTests(unittest.TestCase):
    def test_start_stop_creates_ready_manifest_and_notifies_microtom(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            db = DB(str(base / "db.sqlite3"))
            params = ParamStore(db)
            outbox = Outbox(db)
            _insert_param(db, "MAS0002", "MAS", "0002", "0", "W", "W", "uint8")
            _insert_param(db, "MAS0029", "MAS", "0029", "", "W", "R", "string")
            _insert_param(db, "MAS0030", "MAS", "0030", "0", "R", "N", "uint8")

            ok, msg = params.set_value("MAS0029", "JOB_4711", actor="microtom")
            self.assertTrue(ok, msg)

            cfg = SimpleNamespace(peer_base_url="https://peer-a:9090", peer_base_url_secondary="", shared_secret="")
            prod_dir = str(base / "production")
            log_dir = str(base / "daily")
            manager = ProductionLogManager(db, cfg=cfg, outbox=outbox, log_dir=prod_dir)
            logs = LogStore(db, log_dir=log_dir, production_log_dir=prod_dir)

            started = manager.handle_param_change("MAS0002", "1")
            self.assertEqual("start", started["event"])
            self.assertEqual("0", params.get_effective_value("MAS0030"))

            logs.log("raspi", "out", "production test line")
            logs.log("esp-plc", "in", "esp test line")

            stopped = manager.handle_param_change("MAS0002", "2")
            self.assertEqual("stop", stopped["event"])
            self.assertEqual("1", params.get_effective_value("MAS0030"))

            manifest = manager.ready_manifest()
            self.assertTrue(manifest["ready"])
            self.assertEqual("JOB_4711", manifest["production_label"])
            names = {item["name"] for item in manifest["files"]}
            self.assertIn(production_file_name("all", "JOB_4711"), names)
            self.assertIn(production_file_name("esp", "JOB_4711"), names)

            content_all = (base / "production" / production_file_name("all", "JOB_4711")).read_text(encoding="utf-8")
            content_esp = (base / "production" / production_file_name("esp", "JOB_4711")).read_text(encoding="utf-8")
            self.assertIn("production test line", content_all)
            self.assertIn("esp test line", content_all)
            self.assertIn("esp test line", content_esp)

            self.assertEqual(1, outbox.count())
            job = outbox.next_due()
            body = json.loads(job.body_json)
            self.assertEqual("MAS0030=1", body["msg"])

            data_all = manager.consume_ready_file(production_file_name("all", "JOB_4711"))
            self.assertIn(b"production test line", data_all)
            self.assertEqual("1", params.get_effective_value("MAS0030"))

            # Extend the production set so the ready flag stays high until the final download.
            (base / "production" / production_file_name("tto", "JOB_4711")).write_text("tto\n", encoding="utf-8")
            (base / "production" / production_file_name("laser", "JOB_4711")).write_text("laser\n", encoding="utf-8")
            manager.consume_ready_file(production_file_name("esp", "JOB_4711"))
            self.assertEqual("1", params.get_effective_value("MAS0030"))
            manager.consume_ready_file(production_file_name("tto", "JOB_4711"))
            self.assertEqual("1", params.get_effective_value("MAS0030"))

            manager.consume_ready_file(production_file_name("laser", "JOB_4711"))
            manifest_after = manager.ready_manifest()
            self.assertFalse(manifest_after["ready"])
            self.assertEqual("0", params.get_effective_value("MAS0030"))
            self.assertEqual(2, outbox.count())
            remaining = outbox.next_due()
            self.assertIsNotNone(remaining)
            outbox.delete(remaining.id)
            final_job = outbox.next_due()
            final_body = json.loads(final_job.body_json)
            self.assertEqual("MAS0030=0", final_body["msg"])

    def test_start_is_blocked_while_previous_production_files_are_pending(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            db = DB(str(base / "db.sqlite3"))
            _insert_param(db, "MAS0002", "MAS", "0002", "0", "W", "W", "uint8")
            _insert_param(db, "MAS0029", "MAS", "0029", "", "W", "R", "string")
            _insert_param(db, "MAS0030", "MAS", "0030", "1", "R", "N", "uint8")
            cfg = SimpleNamespace(peer_base_url="https://peer-a:9090", peer_base_url_secondary="", shared_secret="")
            manager = ProductionLogManager(db, cfg=cfg, outbox=Outbox(db), log_dir=str(base / "production"))

            state = {
                "active": False,
                "ready": True,
                "production_label": "BATCH_1",
                "production_label_raw": "BATCH_1",
                "started_ts": now_ts(),
                "stopped_ts": now_ts(),
                "files": [production_file_name("all", "BATCH_1")],
            }
            manager._write_state(state)
            (base / "production" / production_file_name("all", "BATCH_1")).write_text("old\n", encoding="utf-8")

            allowed, reason = manager.can_start_new_production()
            self.assertFalse(allowed)
            self.assertEqual("NAK_ProductionLogfilesPending", reason)

    def test_logfiles_include_parameter_name_and_description(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            db = DB(str(base / "db.sqlite3"))
            _insert_param(db, "MAS0025", "MAS", "0025", "20", "R", "W", "uint32")
            with db._conn() as c:
                c.execute(
                    "UPDATE params SET name=?, message=? WHERE pkey=?",
                    ("MAS0025 SPS-IO Status (3)", "IO-Status 3 von RPI-SPS", "MAS0025"),
                )
            logs = LogStore(db, log_dir=str(base / "daily"), production_log_dir=str(base / "production"))
            logs.log("raspi", "out", "to microtom: MAS0025=128")
            txt = (base / "daily" / next((base / "daily").iterdir()).name).read_text(encoding="utf-8")
            self.assertIn("NAME: MAS0025 SPS-IO Status (3)", txt)
            self.assertIn("DESC: IO-Status 3 von RPI-SPS", txt)


if __name__ == "__main__":
    unittest.main()

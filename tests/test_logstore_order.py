import tempfile
import time
import unittest
from pathlib import Path

from mas004_rpi_databridge.db import DB
from mas004_rpi_databridge.logstore import LogStore


class LogStoreOrderTests(unittest.TestCase):
    def test_list_and_download_return_newest_first(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            db = DB(str(base / "db.sqlite3"))
            logs = LogStore(db, log_dir=str(base / "daily"), production_log_dir=str(base / "production"))

            logs.log("raspi", "in", "first line")
            time.sleep(0.01)
            logs.log("raspi", "out", "second line")

            items = logs.list_logs("raspi", limit=10)
            self.assertEqual(2, len(items))
            self.assertIn("second line", items[0]["message"])
            self.assertIn("first line", items[1]["message"])

            txt = logs.read_logfile("raspi")
            lines = [line for line in txt.splitlines() if line.strip()]
            self.assertGreaterEqual(len(lines), 2)
            self.assertIn("second line", lines[0])
            self.assertIn("first line", lines[1])

    def test_audit_entries_are_human_readable_and_downloadable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            db = DB(str(base / "db.sqlite3"))
            logs = LogStore(db, log_dir=str(base / "daily"), production_log_dir=str(base / "production"))

            logs.log("raspi", "OUT", "MAS0002=1")
            time.sleep(0.01)
            with db._conn() as c:
                c.execute(
                    "INSERT INTO machine_events(ts,event_type,severity,message,payload_json) VALUES(?,?,?,?,?)",
                    (time.time(), "virtual_button", "info", "Virtuelle Taste Start ausgeloest", "{}"),
                )

            items = logs.list_audit_entries(hours=1, limit=10)

            self.assertEqual(2, len(items))
            self.assertTrue(any("MAS0002=1" in item["summary"] for item in items))
            self.assertTrue(any(item["category"] == "machine" for item in items))

            txt = logs.read_audit_log(hours=1, limit=10)
            self.assertIn("MAS-004 Machine Audit Log", txt)
            self.assertIn("Virtuelle Taste Start ausgeloest", txt)


if __name__ == "__main__":
    unittest.main()

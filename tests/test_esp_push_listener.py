import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from mas004_rpi_databridge.config import Settings
from mas004_rpi_databridge.db import DB, now_ts
from mas004_rpi_databridge.device_bridge import DeviceBridge
from mas004_rpi_databridge.esp_push_listener import EspPushListener
from mas004_rpi_databridge.machine_runtime import PRODUCTION_START_BLOCK_CODE, mark_external_purge_clear
from mas004_rpi_databridge.outbox import Outbox
from mas004_rpi_databridge.params import ParamStore


def _insert_param(db: DB, pkey: str, ptype: str, pid: str, default_v: str, rw: str, esp_rw: str):
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
                "uint16",
                pkey,
                "NO",
                None,
                None,
                None,
                None,
                now_ts(),
            ),
        )


class EspPushListenerTests(unittest.TestCase):
    def test_esp_start_is_blocked_before_param_write_while_production_runtime_disabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "db.sqlite3"
            db = DB(str(db_path))
            _insert_param(db, "MAS0002", "MAS", "0002", "0", "W", "W")
            cfg = Settings(db_path=str(db_path), peer_base_url="https://peer-a:9090", peer_base_url_secondary="")
            listener = EspPushListener(cfg, lambda _msg: None)

            with patch("mas004_rpi_databridge.esp_push_listener.production_start_motion_enabled", return_value=False):
                self.assertEqual(f"MAS0002={PRODUCTION_START_BLOCK_CODE}", listener._process_line("MAS0002=1"))
            self.assertEqual("0", ParamStore(db).get_effective_value("MAS0002"))
            self.assertEqual(0, Outbox(db).count())

    def test_esp_read_for_ma_param_is_served_locally_without_esp_tcp_callback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "db.sqlite3"
            db = DB(str(db_path))
            _insert_param(db, "MAS0026", "MAS", "0026", "3", "W", "R")
            ParamStore(db).set_value("MAS0026", "7", actor="microtom")
            cfg = Settings(db_path=str(db_path), peer_base_url="", peer_base_url_secondary="")
            listener = EspPushListener(cfg, lambda _msg: None)

            with patch.object(DeviceBridge, "execute", side_effect=AssertionError("must not call ESP TCP")):
                self.assertEqual("MAS0026=7", listener._process_line("MAS0026=?"))

    def test_esp_read_for_no_access_ma_param_returns_nak_without_esp_tcp_callback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "db.sqlite3"
            db = DB(str(db_path))
            _insert_param(db, "MAS0029", "MAS", "0029", "", "W", "N")
            cfg = Settings(db_path=str(db_path), peer_base_url="", peer_base_url_secondary="")
            listener = EspPushListener(cfg, lambda _msg: None)

            with patch.object(DeviceBridge, "execute", side_effect=AssertionError("must not call ESP TCP")):
                self.assertEqual("MAS0029=NAK_NoAccess", listener._process_line("MAS0029=?"))

    def test_stale_esp_purge_echo_is_ignored_after_external_clear(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "db.sqlite3"
            db = DB(str(db_path))
            _insert_param(db, "MAS0028", "MAS", "0028", "0", "W", "W")
            mark_external_purge_clear(db)
            cfg = Settings(db_path=str(db_path), peer_base_url="https://peer-a:9090", peer_base_url_secondary="")
            listener = EspPushListener(cfg, lambda _msg: None)

            self.assertEqual("ACK_MAS0028=0", listener._process_line("MAS0028=1"))
            self.assertEqual("0", ParamStore(db).get_effective_value("MAS0028"))
            self.assertEqual(0, Outbox(db).count())

    def test_esp_purge_clear_is_only_authoritative_echo_in_scenario_b(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "db.sqlite3"
            db = DB(str(db_path))
            _insert_param(db, "MAS0028", "MAS", "0028", "1", "W", "W")
            params = ParamStore(db)
            params.apply_device_value("MAS0028", "1", promote_default=True)
            cfg = Settings(db_path=str(db_path), peer_base_url="https://peer-a:9090", peer_base_url_secondary="")
            listener = EspPushListener(cfg, lambda _msg: None)

            self.assertEqual("ACK_MAS0028=1", listener._process_line("MAS0028=0"))
            self.assertEqual("1", params.get_effective_value("MAS0028"))
            self.assertEqual(0, Outbox(db).count())

    def test_esp_machine_state_echo_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "db.sqlite3"
            db = DB(str(db_path))
            _insert_param(db, "MAS0001", "MAS", "0001", "9", "R", "W")
            params = ParamStore(db)
            params.apply_device_value("MAS0001", "9", promote_default=True)
            cfg = Settings(db_path=str(db_path), peer_base_url="https://peer-a:9090", peer_base_url_secondary="")
            listener = EspPushListener(cfg, lambda _msg: None)

            self.assertEqual("ACK_MAS0001=9", listener._process_line("MAS0001=21"))
            self.assertEqual("9", params.get_effective_value("MAS0001"))
            self.assertEqual(0, Outbox(db).count())

    def test_esp_purge_echo_without_external_clear_is_still_ignored(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "db.sqlite3"
            db = DB(str(db_path))
            _insert_param(db, "MAS0028", "MAS", "0028", "0", "W", "W")
            params = ParamStore(db)
            cfg = Settings(db_path=str(db_path), peer_base_url="https://peer-a:9090", peer_base_url_secondary="")
            listener = EspPushListener(cfg, lambda _msg: None)

            self.assertEqual("ACK_MAS0028=0", listener._process_line("MAS0028=1"))
            self.assertEqual("0", params.get_effective_value("MAS0028"))
            self.assertEqual(0, Outbox(db).count())

    def test_esp_mae0027_is_ignored_outside_process_sensor_states(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "db.sqlite3"
            db = DB(str(db_path))
            _insert_param(db, "MAS0001", "MAS", "0001", "21", "R", "W")
            _insert_param(db, "MAE0027", "MAE", "0027", "0", "R", "W")
            params = ParamStore(db)
            params.apply_device_value("MAS0001", "21", promote_default=True)
            cfg = Settings(db_path=str(db_path), peer_base_url="https://peer-a:9090", peer_base_url_secondary="")
            listener = EspPushListener(cfg, lambda _msg: None)

            self.assertEqual("ACK_MAE0027=0", listener._process_line("MAE0027=1"))
            self.assertEqual("0", params.get_effective_value("MAE0027"))
            self.assertEqual(0, Outbox(db).count())

    def test_esp_band_break_is_ignored_in_stop_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "db.sqlite3"
            db = DB(str(db_path))
            _insert_param(db, "MAS0001", "MAS", "0001", "9", "R", "W")
            _insert_param(db, "MAE0008", "MAE", "0008", "0", "R", "W")
            params = ParamStore(db)
            params.apply_device_value("MAS0001", "9", promote_default=True)
            cfg = Settings(db_path=str(db_path), peer_base_url="https://peer-a:9090", peer_base_url_secondary="")
            listener = EspPushListener(cfg, lambda _msg: None)

            self.assertEqual("ACK_MAE0008=0", listener._process_line("MAE0008=1"))
            self.assertEqual("0", params.get_effective_value("MAE0008"))
            self.assertEqual(0, Outbox(db).count())

    def test_duplicate_inactive_esp_fault_clear_is_not_forwarded_to_microtom(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "db.sqlite3"
            db = DB(str(db_path))
            _insert_param(db, "MAE0026", "MAE", "0026", "0", "R", "W")
            cfg = Settings(db_path=str(db_path), peer_base_url="https://peer-a:9090", peer_base_url_secondary="")
            listener = EspPushListener(cfg, lambda _msg: None)

            self.assertEqual("ACK_MAE0026=0", listener._process_line("MAE0026=0"))
            self.assertEqual("0", ParamStore(db).get_effective_value("MAE0026"))
            self.assertEqual(0, Outbox(db).count())

    def test_active_esp_fault_clear_is_forwarded_once_to_microtom(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "db.sqlite3"
            db = DB(str(db_path))
            _insert_param(db, "MAE0026", "MAE", "0026", "0", "R", "W")
            ParamStore(db).apply_device_value("MAE0026", "1", promote_default=True)
            cfg = Settings(db_path=str(db_path), peer_base_url="https://peer-a:9090", peer_base_url_secondary="")
            listener = EspPushListener(cfg, lambda _msg: None)

            self.assertEqual("ACK_MAE0026=0", listener._process_line("MAE0026=0"))
            self.assertEqual("0", ParamStore(db).get_effective_value("MAE0026"))
            self.assertEqual(1, Outbox(db).count())

    def test_fast_esp_fault_active_and_clear_are_both_forwarded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "db.sqlite3"
            db = DB(str(db_path))
            _insert_param(db, "MAE0026", "MAE", "0026", "0", "R", "W")
            cfg = Settings(db_path=str(db_path), peer_base_url="https://peer-a:9090", peer_base_url_secondary="")
            listener = EspPushListener(cfg, lambda _msg: None)

            self.assertEqual("ACK_MAE0026=1", listener._process_line("MAE0026=1"))
            self.assertEqual("ACK_MAE0026=0", listener._process_line("MAE0026=0"))

            outbox = Outbox(db)
            jobs = []
            while True:
                job = outbox.next_due()
                if not job:
                    break
                jobs.append((json.loads(job.body_json)["msg"], job.dedupe_key))
                outbox.delete(job.id)
            self.assertEqual(
                [("MAE0026=1", "state:MAE0026:active"), ("MAE0026=0", "state:MAE0026:clear")],
                jobs,
            )


if __name__ == "__main__":
    unittest.main()

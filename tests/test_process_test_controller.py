import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from mas004_rpi_databridge.config import Settings
from mas004_rpi_databridge.db import DB, now_ts
from mas004_rpi_databridge.logstore import LogStore
from mas004_rpi_databridge.params import ParamStore
from mas004_rpi_databridge.process_test_controller import TemporaryProcessCommandController


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


class ProcessTestControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "test.db"))
        for key, value in (("MAS0001", "9"), ("MAS0002", "0"), ("MAS0028", "0")):
            _insert_param(self.db, key, value)
        self.params = ParamStore(self.db)
        self.logs = LogStore(self.db)
        self.controller = TemporaryProcessCommandController(Settings(esp_simulation=True), self.params, self.logs)
        self.controller._calibrate_wicklers_and_learn = Mock(return_value="ACK_MAC0001=1")

    def tearDown(self):
        self.tmp.cleanup()

    def test_wickler_learning_rejected_outside_setup(self):
        _set_machine_state(self.db, current_state=9, requested_state=9, purge_active=False)

        self.assertEqual("MAC0001=NAK_SetupRequired", self.controller.execute("1"))
        self.controller._calibrate_wicklers_and_learn.assert_not_called()

    def test_wickler_learning_rejected_during_purge(self):
        _set_machine_state(self.db, current_state=21, requested_state=21, purge_active=True)

        self.assertEqual("MAC0001=NAK_PurgeActive", self.controller.execute("1"))
        self.controller._calibrate_wicklers_and_learn.assert_not_called()

    def test_wickler_learning_allowed_in_setup_state(self):
        _set_machine_state(self.db, current_state=3, requested_state=3, purge_active=False)

        self.assertEqual("ACK_MAC0001=1", self.controller.execute("1"))
        self.controller._calibrate_wicklers_and_learn.assert_called_once()

    def test_wickler_learning_allowed_after_setup_command(self):
        _set_machine_state(self.db, current_state=9, requested_state=9, purge_active=False)
        self.params.apply_device_value("MAS0002", "3", promote_default=True)

        self.assertEqual("ACK_MAC0001=1", self.controller.execute("1"))
        self.controller._calibrate_wicklers_and_learn.assert_called_once()


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

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
        for key, value in (("MAS0001", "9"), ("MAS0002", "0"), ("MAS0028", "0"), ("MAE0048", "0")):
            _insert_param(self.db, key, value)
        self.params = ParamStore(self.db)
        self.logs = LogStore(self.db)
        self.controller = SetupWicklerOrchestrator(Settings(esp_simulation=True), self.params, self.logs)

    def tearDown(self):
        self.tmp.cleanup()

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

    def test_motor3_measurement_preparation_captures_current_position_as_zero(self):
        self.controller._esp = Mock(
            side_effect=[
                "ACK_RESET_ALARM",
                "ACK_RECOVER_ETO",
                'JSON {"ok":true,"motor":{"state":{"link_ok":true,"ready":true,"alarm":false,"feedback_tenths_mm":1631838,"target_tenths_mm":1641838}}}',
                "ACK_SET_POSITION_MM",
                'JSON {"ok":true,"motor":{"state":{"link_ok":true,"ready":true,"alarm":false,"feedback_tenths_mm":0,"target_tenths_mm":0}}}',
            ]
        )

        state = self.controller._prepare_motor3_for_measurement()

        self.assertEqual(0, state["feedback_tenths_mm"])
        self.assertEqual(0, state["target_tenths_mm"])
        calls = [call.args[0] for call in self.controller._esp.call_args_list]
        self.assertEqual("MOTOR 3 RESET_ALARM", calls[0])
        self.assertEqual("MOTOR 3 RECOVER_ETO", calls[1])
        self.assertIn("MOTOR 3 SET_POSITION_MM=0.000", calls)

    def test_motor3_measurement_preparation_rejects_not_ready_drive_before_move(self):
        self.controller._esp = Mock(
            side_effect=[
                "ACK_RESET_ALARM",
                "ACK_RECOVER_ETO",
                'JSON {"ok":true,"motor":{"state":{"link_ok":true,"ready":false,"alarm":false,"output_raw_hex":"43"}}}',
            ]
            + [
                'JSON {"ok":true,"motor":{"state":{"link_ok":true,"ready":false,"alarm":false,"output_raw_hex":"43"}}}'
            ]
            * 30
        )

        with self.assertRaisesRegex(RuntimeError, "not ready"):
            self.controller._prepare_motor3_for_measurement()

        calls = [call.args[0] for call in self.controller._esp.call_args_list]
        self.assertNotIn("MOTOR 3 SET_POSITION_MM=0.000", calls)
        self.assertFalse(any(call.startswith("MOTOR 3 MOVE_REL_MM_OP") for call in calls))


if __name__ == "__main__":
    unittest.main()

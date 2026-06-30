import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import patch

from mas004_rpi_databridge.config import Settings
from mas004_rpi_databridge.db import DB, now_ts
from mas004_rpi_databridge.inbox import Inbox
from mas004_rpi_databridge.logstore import LogStore
from mas004_rpi_databridge.outbox import Outbox
from mas004_rpi_databridge.params import ParamStore
from mas004_rpi_databridge.machine_runtime import PRODUCTION_START_BLOCK_CODE, mark_external_purge_clear

sys.modules.setdefault("ping3", SimpleNamespace(ping=lambda *_args, **_kwargs: 1.0))

from mas004_rpi_databridge import router as router_module
from mas004_rpi_databridge.router import Router


def _insert_param(
    db: DB,
    pkey: str,
    ptype: str,
    pid: str,
    default_v: Optional[str],
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


class RouterEspAccessTests(unittest.TestCase):
    def _make_router(self, base: Path) -> Router:
        db = DB(str(base / "db.sqlite3"))
        cfg = Settings(
            db_path=str(base / "db.sqlite3"),
            peer_base_url="https://peer-a:9090",
            peer_base_url_secondary="",
            esp_simulation=False,
            esp_host="192.168.2.101",
            esp_port=3010,
        )
        return Router(cfg, Inbox(db), Outbox(db), ParamStore(db), LogStore(db, log_dir=str(base / "logs")))

    def test_start_is_blocked_before_param_write_while_production_runtime_disabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            router = self._make_router(base)
            _insert_param(router.params.db, "MAS0002", "MAS", "0002", "0", "W", "W", "uint8")

            with patch("mas004_rpi_databridge.router.production_start_motion_enabled", return_value=False):
                resp = router.handle_microtom_line("MAS0002=1", correlation="start-disabled")

            self.assertEqual(f"MAS0002={PRODUCTION_START_BLOCK_CODE}", resp)
            self.assertEqual("0", router.params.get_effective_value("MAS0002"))
            job = router.outbox.next_due()
            self.assertIsNotNone(job)
            body = json.loads(job.body_json)
            self.assertEqual(f"MAS0002={PRODUCTION_START_BLOCK_CODE}", body["msg"])

    def test_ma_param_with_esp_rw_n_stays_local_even_when_esp_live_is_enabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            router = self._make_router(base)
            _insert_param(router.params.db, "MAS0029", "MAS", "0029", "", "W", "N", "string")

            def _unexpected_live(*_args, **_kwargs):
                raise AssertionError("esp live path must not be used for esp_rw=N")

            router.device_bridge._esp_live = _unexpected_live

            resp = router.handle_microtom_line("MAS0029=JOB_4711", correlation="corr-1")

            self.assertEqual("ACK_MAS0029=JOB_4711", resp)
            self.assertEqual("JOB_4711", router.params.get_effective_value("MAS0029"))

            job = router.outbox.next_due()
            self.assertIsNotNone(job)
            body = json.loads(job.body_json)
            self.assertEqual("ACK_MAS0029=JOB_4711", body["msg"])

    def test_ma_param_with_esp_access_still_uses_esp_live_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            router = self._make_router(base)
            _insert_param(router.params.db, "MAS0026", "MAS", "0026", "20", "W", "W", "uint8")

            calls = []

            def _fake_live(pkey, op, value, actor="microtom"):
                calls.append((pkey, op, value, actor))
                return f"ACK_{pkey}={value}"

            router.device_bridge._esp_live = _fake_live

            resp = router.handle_microtom_line("MAS0026=21", correlation="corr-2")

            self.assertEqual("ACK_MAS0026=21", resp)
            self.assertEqual([("MAS0026", "write", "21", "microtom")], calls)

    def test_machine_state_read_is_answered_from_raspi_not_stale_esp_mirror(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            router = self._make_router(base)
            _insert_param(router.params.db, "MAS0001", "MAS", "0001", "3", "R", "W", "uint8")
            router.params.apply_device_value("MAS0001", "3", promote_default=True)

            def _unexpected_live(*_args, **_kwargs):
                raise AssertionError("MAS0001 read must not use the ESP live path")

            mirrored = []
            router.device_bridge._esp_live = _unexpected_live
            router.device_bridge.mirror_to_esp = lambda pkey, value: mirrored.append((pkey, value)) or (True, "ACK")

            resp = router.handle_microtom_line("MAS0001=?", correlation="corr-state")

            self.assertEqual("MAS0001=3", resp)
            self.assertEqual([("MAS0001", "3")], mirrored)

    def test_mae_read_is_answered_from_raspi_state_not_stale_esp_mirror(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            router = self._make_router(base)
            _insert_param(router.params.db, "MAE0048", "MAE", "0048", "0", "R", "W", "bool")
            router.params.apply_device_value("MAE0048", "1", promote_default=True)

            def _unexpected_live(*_args, **_kwargs):
                raise AssertionError("MAE read must not use the ESP live path")

            mirrored = []
            router.device_bridge._esp_live = _unexpected_live
            router.device_bridge.mirror_to_esp = lambda pkey, value: mirrored.append((pkey, value)) or (True, "ACK")

            resp = router.handle_microtom_line("MAE0048=?", correlation="corr-mae")

            self.assertEqual("MAE0048=1", resp)
            self.assertEqual([("MAE0048", "1")], mirrored)

    def test_device_machine_state_echo_cannot_overwrite_raspi_runtime_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            router = self._make_router(base)
            _insert_param(router.params.db, "MAS0001", "MAS", "0001", "9", "R", "W", "uint8")
            router.params.apply_device_value("MAS0001", "9", promote_default=True)

            resp = router.handle_device_line("MAS0001=21", source="esp-plc", correlation=None)

            self.assertEqual("ACK_MAS0001=9", resp)
            self.assertEqual("9", router.params.get_effective_value("MAS0001"))
            self.assertEqual(0, router.outbox.count())

    def test_device_purge_echo_cannot_reactivate_raspi_purge_latch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            router = self._make_router(base)
            _insert_param(router.params.db, "MAS0028", "MAS", "0028", "0", "W", "W", "bool")
            router.params.apply_device_value("MAS0028", "0", promote_default=True)

            resp = router.handle_device_line("MAS0028=1", source="esp-plc", correlation=None)

            self.assertEqual("ACK_MAS0028=0", resp)
            self.assertEqual("0", router.params.get_effective_value("MAS0028"))
            self.assertEqual(0, router.outbox.count())

    def test_device_purge_clear_cannot_terminate_scenario_b(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            router = self._make_router(base)
            _insert_param(router.params.db, "MAS0028", "MAS", "0028", "1", "W", "W", "bool")
            router.params.apply_device_value("MAS0028", "1", promote_default=True)

            resp = router.handle_device_line("MAS0028=0", source="esp-plc", correlation=None)

            self.assertEqual("ACK_MAS0028=1", resp)
            self.assertEqual("1", router.params.get_effective_value("MAS0028"))
            self.assertEqual(0, router.outbox.count())

    def test_ma_param_with_esp_read_access_is_stored_locally_and_mirrored(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            router = self._make_router(base)
            _insert_param(router.params.db, "MAP0001", "MAP", "0001", "500", "W", "R", "uint16")

            def _unexpected_live(*_args, **_kwargs):
                raise AssertionError("esp live write path must not be used for esp_rw=R")

            mirrored = []
            router.device_bridge._esp_live = _unexpected_live
            router.device_bridge.mirror_to_esp = lambda pkey, value: mirrored.append((pkey, value)) or (True, "ACK_MAP0001=550")

            resp = router.handle_microtom_line("MAP0001=550", correlation="corr-4")

            self.assertEqual("ACK_MAP0001=550", resp)
            self.assertEqual("550", router.params.get_effective_value("MAP0001"))
            self.assertEqual([("MAP0001", "550")], mirrored)

    def test_vj6530_write_triggers_forced_follow_up_sync(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            router = self._make_router(base)
            _insert_param(router.params.db, "TTS0001", "TTS", "0001", "0", "R", "W", "enum")
            with router.params.db._conn() as c:
                c.execute(
                    """INSERT INTO param_device_map(
                        pkey, esp_key, zbc_mapping, zbc_message_id, zbc_command_id, zbc_value_codec,
                        zbc_scale, zbc_offset, ultimate_set_cmd, ultimate_get_cmd, ultimate_var_name, updated_ts
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "TTS0001",
                        None,
                        "STATUS[PRINTER_STATE_CODE]",
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

            calls = []

            class _ForcedPoller:
                def __init__(self, *args, **kwargs):
                    calls.append(("init", args, kwargs))

                def poll_once(self, force: bool = False):
                    calls.append(("poll_once", force))
                    return {"checked": 1, "changed": 1, "forwarded": 1}

            original_poller = router_module.Vj6530Poller
            original_execute = router.device_bridge.execute
            router_module.Vj6530Poller = _ForcedPoller
            router.device_bridge.execute = lambda *args, **kwargs: "ACK_TTS0001=3"
            try:
                resp = router.handle_microtom_line("TTS0001=3", correlation="corr-3")
            finally:
                router_module.Vj6530Poller = original_poller
                router.device_bridge.execute = original_execute

            self.assertEqual("ACK_TTS0001=3", resp)
            self.assertIn(("poll_once", True), calls)

    def test_mirror_to_esp_uses_sync_for_esp_read_only_values(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            router = self._make_router(base)
            _insert_param(router.params.db, "MAP0001", "MAP", "0001", "500", "W", "R", "uint16")
            lines = []

            def _fake_exchange(line, read_timeout_s=0):
                lines.append((line, read_timeout_s))
                return "ACK_MAP0001=550"

            router.device_bridge._esp.exchange_line = _fake_exchange

            ok, detail = router.device_bridge.mirror_to_esp("MAP0001", "550")

            self.assertTrue(ok, detail)
            self.assertEqual([("SYNC MAP0001=550", router.cfg.esp_command_timeout_s)], lines)

    def test_smartwickler_status_push_updates_readonly_value_without_nak(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            router = self._make_router(base)
            _insert_param(router.params.db, "MAS0008", "MAS", "0008", "0", "R", "N", "uint8")

            inserted = router.inbox.store(
                "smartwickler",
                {},
                {"msg": "MAS0008=52", "source": "smartwickler", "origin": "smartwickler"},
                "wickler-1",
            )
            self.assertTrue(inserted)

            self.assertTrue(router.tick_once())

            self.assertEqual("52", router.params.get_effective_value("MAS0008"))
            job = router.outbox.next_due()
            self.assertIsNotNone(job)
            body = json.loads(job.body_json)
            self.assertEqual("MAS0008=52", body["msg"])
            self.assertEqual("smartwickler", body["origin"])
            self.assertNotIn("NAK_ReadOnly", body["msg"])

    def test_microtom_purge_clear_removes_stale_pending_status_callbacks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            router = self._make_router(base)
            _insert_param(router.params.db, "MAS0028", "MAS", "0028", "1", "W", "W", "bool")
            def _fake_esp_live(pkey, op, value, actor="microtom"):
                router.params.set_value(pkey, value, actor=actor)
                return f"ACK_{pkey}={value}"

            router.device_bridge._esp_live = _fake_esp_live
            router.device_bridge.mirror_to_esp = lambda pkey, value: (True, f"ACK_{pkey}={value}")
            router.outbox.enqueue(
                "POST",
                "https://peer-a:9090/api/inbox",
                {},
                {"msg": "MAS0028=1", "source": "raspi", "origin": "esp-plc"},
                dedupe_key="esp-plc:MAS0028",
            )

            resp = router.handle_microtom_line("MAS0028=0", correlation="purge-clear")

            self.assertEqual("ACK_MAS0028=0", resp)
            self.assertEqual("0", router.params.get_effective_value("MAS0028"))
            jobs = []
            while True:
                job = router.outbox.next_due()
                if not job:
                    break
                jobs.append(json.loads(job.body_json)["msg"])
                router.outbox.delete(job.id)
            self.assertEqual(["ACK_MAS0028=0"], jobs)

    def test_microtom_purge_start_removes_stale_pending_clear_callbacks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            router = self._make_router(base)
            _insert_param(router.params.db, "MAS0028", "MAS", "0028", "0", "W", "W", "bool")

            def _fake_raspi_write(pkey, op, value, actor="microtom"):
                router.params.set_value(pkey, value, actor=actor)
                return f"ACK_{pkey}={value}"

            router.device_bridge._simulate = _fake_raspi_write
            router.device_bridge.mirror_to_esp = lambda pkey, value: (True, f"ACK_{pkey}={value}")
            router.outbox.enqueue(
                "POST",
                "https://peer-a:9090/api/inbox",
                {},
                {"msg": "MAS0028=0", "source": "raspi", "origin": "machine-runtime"},
                dedupe_key="state:MAS0028:clear",
            )

            resp = router.handle_microtom_line("MAS0028=1", correlation="purge-start")

            self.assertEqual("ACK_MAS0028=1", resp)
            self.assertEqual("1", router.params.get_effective_value("MAS0028"))
            jobs = []
            while True:
                job = router.outbox.next_due()
                if not job:
                    break
                jobs.append(json.loads(job.body_json)["msg"])
                router.outbox.delete(job.id)
            self.assertEqual(["ACK_MAS0028=1"], jobs)

    def test_device_purge_echo_is_ignored_immediately_after_microtom_clear(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            router = self._make_router(base)
            _insert_param(router.params.db, "MAS0028", "MAS", "0028", "0", "W", "W", "bool")
            mark_external_purge_clear(router.params.db)

            resp = router.handle_device_line("MAS0028=1", source="esp-plc", correlation=None)

            self.assertEqual("ACK_MAS0028=0", resp)
            self.assertEqual("0", router.params.get_effective_value("MAS0028"))
            self.assertEqual(0, router.outbox.count())

    def test_device_band_break_is_ignored_in_stop_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            router = self._make_router(base)
            _insert_param(router.params.db, "MAS0001", "MAS", "0001", "9", "R", "W", "uint8")
            _insert_param(router.params.db, "MAE0009", "MAE", "0009", "0", "R", "W", "bool")
            router.params.apply_device_value("MAS0001", "9", promote_default=True)

            resp = router.handle_device_line("MAE0009=1", source="esp-plc", correlation=None)

            self.assertEqual("ACK_MAE0009=0", resp)
            self.assertEqual("0", router.params.get_effective_value("MAE0009"))
            self.assertEqual(0, router.outbox.count())

    def test_duplicate_inactive_device_fault_clear_is_not_forwarded_to_microtom(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            router = self._make_router(base)
            _insert_param(router.params.db, "MAE0025", "MAE", "0025", "0", "R", "W", "bool")

            resp = router.handle_device_line("MAE0025=0", source="esp-plc", correlation=None)

            self.assertEqual("ACK_MAE0025=0", resp)
            self.assertEqual("0", router.params.get_effective_value("MAE0025"))
            self.assertEqual(0, router.outbox.count())

    def test_device_fault_active_is_not_replaced_by_fast_clear(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            router = self._make_router(base)
            _insert_param(router.params.db, "MAE0026", "MAE", "0026", "0", "R", "W", "bool")

            self.assertEqual("MAE0026=1", router.handle_device_line("MAE0026=1", source="esp-plc", correlation=None))
            self.assertEqual("MAE0026=0", router.handle_device_line("MAE0026=0", source="esp-plc", correlation=None))

            jobs = []
            while True:
                job = router.outbox.next_due()
                if not job:
                    break
                jobs.append((json.loads(job.body_json)["msg"], job.dedupe_key))
                router.outbox.delete(job.id)
            self.assertEqual(
                [("MAE0026=1", "state:MAE0026:active"), ("MAE0026=0", "state:MAE0026:clear")],
                jobs,
            )

    def test_wickler_dancer_clear_stays_latched_until_reset(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            router = self._make_router(base)
            _insert_param(router.params.db, "MAE0030", "MAE", "0030", "0", "R", "W", "bool")
            router.params.apply_device_value("MAE0030", "1", promote_default=True)

            resp = router.handle_device_line("MAE0030=0", source="smartwickler", correlation=None)

            self.assertEqual("ACK_MAE0030=1", resp)
            self.assertEqual("1", router.params.get_effective_value("MAE0030"))
            self.assertEqual(0, router.outbox.count())


if __name__ == "__main__":
    unittest.main()

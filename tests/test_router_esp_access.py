import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mas004_rpi_databridge.config import Settings
from mas004_rpi_databridge.db import DB, now_ts
from mas004_rpi_databridge.inbox import Inbox
from mas004_rpi_databridge.logstore import LogStore
from mas004_rpi_databridge.outbox import Outbox
from mas004_rpi_databridge.params import ParamStore

sys.modules.setdefault("ping3", SimpleNamespace(ping=lambda *_args, **_kwargs: 1.0))

from mas004_rpi_databridge.router import Router


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


if __name__ == "__main__":
    unittest.main()

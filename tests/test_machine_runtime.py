import sys
import tempfile
import unittest
from pathlib import Path

from types import SimpleNamespace

sys.modules.setdefault("ping3", SimpleNamespace(ping=lambda *_args, **_kwargs: 1.0))

from mas004_rpi_databridge.config import Settings
from mas004_rpi_databridge.db import DB, now_ts
from mas004_rpi_databridge.io_master import IoStore
from mas004_rpi_databridge.logstore import LogStore
from mas004_rpi_databridge.machine_runtime import MachineRuntime
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
            ("MAP0065", "MAP", "0065", "1111111", "W", "R", "uint8"),
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
        for pin, value in (("I0.4", "0"), ("I0.7", "1"), ("I0.8", "1"), ("I0.11", "0")):
            _insert_io_point(self.db, "esp32_plc58", "ESP32 PLC 58", pin, "input", value)
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

        first = runtime.refresh()
        self.assertEqual(2, first["current_state"])
        self.assertEqual(3, first["requested_state"])
        self.assertEqual(3, first["info"]["requested_command"])

        second = runtime.refresh()
        self.assertEqual(3, second["current_state"])
        self.assertEqual(3, second["requested_state"])

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


if __name__ == "__main__":
    unittest.main()

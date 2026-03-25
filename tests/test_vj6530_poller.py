import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
import types

sys.modules.setdefault("ping3", types.SimpleNamespace(ping=lambda *args, **kwargs: None))

from mas004_rpi_databridge import vj6530_poller as poller_module
from mas004_rpi_databridge.db import DB, now_ts
from mas004_rpi_databridge.logstore import LogStore
from mas004_rpi_databridge.outbox import Outbox
from mas004_rpi_databridge.params import ParamStore
from mas004_rpi_databridge.vj6530_poller import Vj6530Poller


class FakeBridgeClient:
    def __init__(self, host: str, port: int, timeout_s: float = 0.0):
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self.calls = []

    def read_mapped_values(self, mappings):
        self.calls.append(dict(mappings))
        return {
            "TTE1000": "1",
            "TTW1010": "1",
        }


class FakeBridgeClientWithTts(FakeBridgeClient):
    def read_mapped_values(self, mappings):
        self.calls.append(dict(mappings))
        return {
            "TTS0001": "3",
        }


class RuntimeSessionStub:
    def __init__(self, result):
        self.result = result
        self.calls = []
        self._recent_event = False
        self._recent_ok = False

    def session_active(self) -> bool:
        return True

    def mark_async_event(self):
        self._recent_event = True

    def mark_async_ok(self):
        self._recent_ok = True

    def async_event_recent(self, max_age_s: float) -> bool:
        return self._recent_event

    def async_recent(self, max_age_s: float) -> bool:
        return self._recent_ok

    def submit_session_request(self, operation: str, *args, **kwargs):
        self.calls.append((operation, args, kwargs))
        return self.result


class Vj6530PollerTests(unittest.TestCase):
    def test_poll_once_updates_only_changed_states_and_enqueues_forward(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DB(str(Path(tmpdir) / "db.sqlite3"))
            params = ParamStore(db)
            logs = LogStore(db)
            outbox = Outbox(db)

            with db._conn() as c:
                for pkey, ptype, pid, default_v, mapping in (
                    ("TTE1000", "TTE", "1000", "0", "IRQ{LEI,ERR}/Fault[text^='E1000 ']"),
                    ("TTW1010", "TTW", "1010", "1", "IRQ{LEI,ERR}/Warning[text^='E1010 ']"),
                ):
                    c.execute(
                        """INSERT INTO params(
                            pkey,ptype,pid,min_v,max_v,default_v,unit,rw,dtype,name,format_relevant,
                            message,possible_cause,effects,remedy,updated_ts
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            pkey,
                            ptype,
                            pid,
                            0,
                            1,
                            default_v,
                            "",
                            "R",
                            "Bool",
                            pkey,
                            "NO",
                            None,
                            None,
                            None,
                            None,
                            now_ts(),
                        ),
                    )
                    c.execute(
                        """INSERT INTO param_device_map(
                            pkey, esp_key, zbc_mapping, zbc_message_id, zbc_command_id, zbc_value_codec,
                            zbc_scale, zbc_offset, ultimate_set_cmd, ultimate_get_cmd, ultimate_var_name, updated_ts
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (pkey, None, mapping, None, None, None, None, None, None, None, None, now_ts()),
                    )

            cfg = SimpleNamespace(
                vj6530_host="192.168.2.103",
                vj6530_port=3002,
                http_timeout_s=5.0,
                peer_base_url="https://10.27.67.135:9090",
                peer_base_url_secondary="",
            )

            fake_client = FakeBridgeClient(cfg.vj6530_host, cfg.vj6530_port, timeout_s=cfg.http_timeout_s)
            poller = Vj6530Poller(cfg, params, logs, outbox, client_factory=lambda *args, **kwargs: fake_client)

            result = poller.poll_once()

            self.assertEqual({"checked": 2, "changed": 1, "forwarded": 1}, result)
            self.assertEqual("1", params.get_effective_value("TTE1000"))
            self.assertEqual("1", params.get_effective_value("TTW1010"))
            self.assertEqual(1, outbox.count())

            job = outbox.next_due()
            self.assertIsNotNone(job)
            body = json.loads(job.body_json)
            self.assertEqual("TTE1000=1", body["msg"])
            self.assertEqual("vj6530", body["origin"])

            items = logs.list_logs("raspi", limit=20)
            messages = [entry["message"] for entry in items]
            self.assertTrue(any("vj6530 poll: TTE1000=1" in msg for msg in messages))

    def test_poll_once_updates_tts_state_and_skips_microtom_without_access(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DB(str(Path(tmpdir) / "db.sqlite3"))
            params = ParamStore(db)
            logs = LogStore(db)
            outbox = Outbox(db)

            with db._conn() as c:
                c.execute(
                    """INSERT INTO params(
                        pkey,ptype,pid,min_v,max_v,default_v,unit,rw,esp_rw,dtype,name,format_relevant,
                        message,possible_cause,effects,remedy,updated_ts
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "TTS0001",
                        "TTS",
                        "0001",
                        0,
                        6,
                        "0",
                        "enum",
                        "N",
                        "W",
                        "unsigned int.",
                        "PrinterStateCode",
                        "NO",
                        None,
                        None,
                        None,
                        None,
                        now_ts(),
                    ),
                )
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

            cfg = SimpleNamespace(
                vj6530_host="192.168.2.103",
                vj6530_port=3002,
                http_timeout_s=5.0,
                peer_base_url="https://10.27.67.135:9090",
                peer_base_url_secondary="",
                esp_host="",
                esp_port=0,
            )

            fake_client = FakeBridgeClientWithTts(cfg.vj6530_host, cfg.vj6530_port, timeout_s=cfg.http_timeout_s)
            poller = Vj6530Poller(cfg, params, logs, outbox, client_factory=lambda *args, **kwargs: fake_client)

            result = poller.poll_once()

            self.assertEqual({"checked": 1, "changed": 1, "forwarded": 0}, result)
            self.assertEqual("3", params.get_effective_value("TTS0001"))
            self.assertEqual(0, outbox.count())

            items = logs.list_logs("raspi", limit=20)
            messages = [entry["message"] for entry in items]
            self.assertTrue(any("skip microtom forward for TTS0001" in msg for msg in messages))

    def test_poll_once_uses_runtime_session_when_async_owner_is_active(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DB(str(Path(tmpdir) / "db.sqlite3"))
            params = ParamStore(db)
            logs = LogStore(db)
            outbox = Outbox(db)

            with db._conn() as c:
                c.execute(
                    """INSERT INTO params(
                        pkey,ptype,pid,min_v,max_v,default_v,unit,rw,esp_rw,dtype,name,format_relevant,
                        message,possible_cause,effects,remedy,updated_ts
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "TTS0001",
                        "TTS",
                        "0001",
                        0,
                        6,
                        "0",
                        "enum",
                        "R",
                        "W",
                        "unsigned int.",
                        "PrinterStateCode",
                        "NO",
                        None,
                        None,
                        None,
                        None,
                        now_ts(),
                    ),
                )
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

            cfg = SimpleNamespace(
                vj6530_host="192.168.2.103",
                vj6530_port=3002,
                vj6530_async_enabled=True,
                http_timeout_s=5.0,
                peer_base_url="https://10.27.67.135:9090",
                peer_base_url_secondary="",
                esp_host="",
                esp_port=0,
            )

            runtime = RuntimeSessionStub({"TTS0001": "3"})
            original_runtime = poller_module.VJ6530_RUNTIME
            poller_module.VJ6530_RUNTIME = runtime
            try:
                poller = Vj6530Poller(cfg, params, logs, outbox, client_factory=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("direct client should not be used")))
                result = poller.poll_once()
            finally:
                poller_module.VJ6530_RUNTIME = original_runtime

            self.assertEqual({"checked": 1, "changed": 1, "forwarded": 1}, result)
            self.assertEqual("read_mapped_values", runtime.calls[0][0])

    def test_poll_once_skips_when_async_event_is_recent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DB(str(Path(tmpdir) / "db.sqlite3"))
            params = ParamStore(db)
            logs = LogStore(db)
            outbox = Outbox(db)
            cfg = SimpleNamespace(
                vj6530_host="192.168.2.103",
                vj6530_port=3002,
                vj6530_async_enabled=True,
                http_timeout_s=5.0,
                peer_base_url="https://10.27.67.135:9090",
                peer_base_url_secondary="",
                esp_host="",
                esp_port=0,
            )

            original_runtime = poller_module.VJ6530_RUNTIME
            runtime = RuntimeSessionStub({})
            runtime.mark_async_event()
            poller_module.VJ6530_RUNTIME = runtime
            try:
                poller = Vj6530Poller(
                    cfg,
                    params,
                    logs,
                    outbox,
                    client_factory=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("direct client should not be used")),
                )
                result = poller.poll_once()
            finally:
                poller_module.VJ6530_RUNTIME = original_runtime

            self.assertEqual({"checked": 0, "changed": 0, "forwarded": 0}, result)

    def test_poll_once_skips_when_async_owner_is_healthy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DB(str(Path(tmpdir) / "db.sqlite3"))
            params = ParamStore(db)
            logs = LogStore(db)
            outbox = Outbox(db)
            cfg = SimpleNamespace(
                vj6530_host="192.168.2.103",
                vj6530_port=3002,
                vj6530_async_enabled=True,
                http_timeout_s=5.0,
                peer_base_url="https://10.27.67.135:9090",
                peer_base_url_secondary="",
                esp_host="",
                esp_port=0,
            )

            original_runtime = poller_module.VJ6530_RUNTIME
            runtime = RuntimeSessionStub({})
            runtime.mark_async_ok()
            poller_module.VJ6530_RUNTIME = runtime
            try:
                poller = Vj6530Poller(
                    cfg,
                    params,
                    logs,
                    outbox,
                    client_factory=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("direct client should not be used")),
                )
                result = poller.poll_once()
            finally:
                poller_module.VJ6530_RUNTIME = original_runtime

            self.assertEqual({"checked": 0, "changed": 0, "forwarded": 0}, result)

    def test_poll_once_force_bypasses_async_owner_skip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DB(str(Path(tmpdir) / "db.sqlite3"))
            params = ParamStore(db)
            logs = LogStore(db)
            outbox = Outbox(db)
            with db._conn() as c:
                c.execute(
                    """INSERT INTO params(
                        pkey,ptype,pid,min_v,max_v,default_v,unit,rw,esp_rw,dtype,name,format_relevant,
                        message,possible_cause,effects,remedy,updated_ts
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "TTS0001",
                        "TTS",
                        "0001",
                        0,
                        6,
                        "0",
                        "enum",
                        "R",
                        "W",
                        "unsigned int.",
                        "PrinterStateCode",
                        "NO",
                        None,
                        None,
                        None,
                        None,
                        now_ts(),
                    ),
                )
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

            cfg = SimpleNamespace(
                vj6530_host="192.168.2.103",
                vj6530_port=3002,
                vj6530_async_enabled=True,
                http_timeout_s=5.0,
                peer_base_url="https://10.27.67.135:9090",
                peer_base_url_secondary="",
                esp_host="",
                esp_port=0,
            )

            runtime = RuntimeSessionStub({"TTS0001": "3"})
            runtime.mark_async_ok()
            runtime.mark_async_event()
            original_runtime = poller_module.VJ6530_RUNTIME
            poller_module.VJ6530_RUNTIME = runtime
            try:
                poller = Vj6530Poller(
                    cfg,
                    params,
                    logs,
                    outbox,
                    client_factory=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("direct client should not be used")),
                )
                result = poller.poll_once(force=True)
            finally:
                poller_module.VJ6530_RUNTIME = original_runtime

            self.assertEqual({"checked": 1, "changed": 1, "forwarded": 1}, result)
            self.assertGreaterEqual(len(runtime.calls), 1)
            self.assertEqual("read_mapped_values", runtime.calls[0][0])


if __name__ == "__main__":
    unittest.main()

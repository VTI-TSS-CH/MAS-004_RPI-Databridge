import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
import types
import socket

sys.modules.setdefault("ping3", types.SimpleNamespace(ping=lambda *args, **kwargs: None))

from mas004_rpi_databridge import vj6530_async_listener as async_module
from mas004_rpi_databridge.db import DB, now_ts
from mas004_rpi_databridge.logstore import LogStore
from mas004_rpi_databridge.outbox import Outbox
from mas004_rpi_databridge.params import ParamStore
from mas004_rpi_databridge.vj6530_async_listener import Vj6530AsyncListener
from mas004_rpi_databridge.vj6530_runtime import Vj6530RuntimeState


class FakeDeviceBridge:
    def __init__(self):
        self.calls = []

    def mirror_to_esp(self, pkey: str, value: str):
        self.calls.append((pkey, value))
        return True, "OK"


class FakeLogs:
    def __init__(self):
        self.entries = []

    def log(self, channel: str, direction: str, message: str):
        self.entries.append((channel, direction, message))


class FakeAsyncClient:
    def __init__(self):
        self.profile = SimpleNamespace(name="vj6530-tcp-no-crc")
        self.keepalive_calls = 0
        self.write_calls = []

    def subscribe_async(self, _subscriptions):
        return async_module.MessageId.NUL, object()

    def negotiate_host_version(self):
        return async_module.MessageId.NUL, object()

    def request_info(self, _tags):
        self.keepalive_calls += 1
        return SimpleNamespace(tags=[])

    def write_mapped_value(self, mapping: str, value: str):
        self.write_calls.append((mapping, value))
        return async_module.MessageId.NUL, str(value)

    def receive_unsolicited(self):
        raise socket.timeout()

    def close(self):
        return None


class Vj6530AsyncListenerTests(unittest.TestCase):
    def test_sync_from_summary_pushes_tte_and_tts_to_microtom_and_esp(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DB(str(Path(tmpdir) / "db.sqlite3"))
            params = ParamStore(db)
            logs = LogStore(db)
            outbox = Outbox(db)

            with db._conn() as c:
                for pkey, ptype, pid, rw, esp_rw, mapping in (
                    ("TTE1000", "TTE", "1000", "R", "W", "IRQ{LEI,ERR}/Fault[text^='E1000 ']"),
                    ("TTS0001", "TTS", "0001", "R", "W", "STATUS[PRINTER_STATE_CODE]"),
                ):
                    c.execute(
                        """INSERT INTO params(
                            pkey,ptype,pid,min_v,max_v,default_v,unit,rw,esp_rw,dtype,name,format_relevant,
                            message,possible_cause,effects,remedy,updated_ts
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            pkey,
                            ptype,
                            pid,
                            0,
                            6,
                            "0",
                            "",
                            rw,
                            esp_rw,
                            "unsigned int.",
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
                esp_host="192.168.2.101",
                esp_port=3010,
                esp_watchdog_host="",
                watchdog_timeout_s=1.0,
                watchdog_down_after=3,
                vj3350_host="",
                vj3350_port=0,
                esp_simulation=True,
                vj6530_simulation=False,
                vj3350_simulation=True,
            )

            listener = Vj6530AsyncListener(cfg, params, logs, outbox)
            fake_bridge = FakeDeviceBridge()
            listener.device_bridge = fake_bridge

            original_resolve = async_module.resolve_summary_mappings
            async_module.resolve_summary_mappings = lambda mappings, summary, snapshot=None: {
                "TTE1000": "1",
                "TTS0001": "5",
            }
            try:
                listener._sync_from_summary(object())
            finally:
                async_module.resolve_summary_mappings = original_resolve

            self.assertEqual("1", params.get_effective_value("TTE1000"))
            self.assertEqual("5", params.get_effective_value("TTS0001"))
            self.assertEqual([("TTE1000", "1"), ("TTS0001", "5")], fake_bridge.calls)
            self.assertEqual(2, outbox.count())

            bodies = []
            while True:
                job = outbox.next_due()
                if job is None:
                    break
                bodies.append(json.loads(job.body_json)["msg"])
                outbox.delete(job.id)
            self.assertEqual(["TTE1000=1", "TTS0001=5"], bodies)

    def test_run_session_marks_async_ok_before_startup_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DB(str(Path(tmpdir) / "db.sqlite3"))
            cfg = SimpleNamespace(
                vj6530_host="192.168.2.103",
                vj6530_port=3002,
                http_timeout_s=5.0,
                peer_base_url="",
                peer_base_url_secondary="",
                esp_host="",
                esp_port=0,
                esp_watchdog_host="",
                watchdog_timeout_s=1.0,
                watchdog_down_after=3,
                vj3350_host="",
                vj3350_port=0,
                esp_simulation=True,
                vj6530_simulation=False,
                vj3350_simulation=True,
            )
            listener = Vj6530AsyncListener(cfg, ParamStore(db), LogStore(db), Outbox(db))
            listener.logs = FakeLogs()
            listener.device_bridge = FakeDeviceBridge()

            original_runtime = async_module.VJ6530_RUNTIME
            original_monotonic = async_module.time.monotonic
            runtime = Vj6530RuntimeState()
            values = iter([0.1, 1.0, 1.0, 1.1, 2.5, 7.0, 7.2, 7.4, 8.0, 8.2, 8.4])

            async_module.VJ6530_RUNTIME = runtime
            async_module.time.monotonic = lambda: next(values)
            listener._open_async_client = lambda: FakeAsyncClient()
            listener._sync_summary_until_stable = lambda client, settle_s: (_ for _ in ()).throw(TimeoutError("summary timeout"))
            try:
                listener.run_session(session_s=5.0)
            finally:
                async_module.VJ6530_RUNTIME = original_runtime
                async_module.time.monotonic = original_monotonic

            self.assertGreater(runtime.snapshot()["last_async_ok_ts"], 0.0)
            self.assertFalse(runtime.snapshot()["session_active"])

    def test_drain_session_requests_strips_verify_readback_for_zbc_client(self):
        listener = object.__new__(Vj6530AsyncListener)
        listener.logs = FakeLogs()
        listener._sync_summary_until_stable = lambda client, settle_s: 0
        fake_client = FakeAsyncClient()
        request = async_module.VJ6530_RUNTIME.next_session_request(timeout_s=0.0)
        self.assertIsNone(request)

        runtime = Vj6530RuntimeState()
        original_runtime = async_module.VJ6530_RUNTIME
        async_module.VJ6530_RUNTIME = runtime
        runtime.mark_session_active(True)
        try:
            def worker():
                result = runtime.submit_session_request(
                    "write_mapped_value",
                    "STATUS[PRINTER_STATE_CODE]",
                    "3",
                    verify_readback=True,
                    timeout_s=1.0,
                )
                self.assertEqual((async_module.MessageId.NUL, "3"), result)

            import threading

            thread = threading.Thread(target=worker)
            thread.start()
            handled = listener._drain_session_requests(fake_client)
            thread.join(timeout=1.0)
            self.assertTrue(handled)
            self.assertEqual([("STATUS[PRINTER_STATE_CODE]", "3")], fake_client.write_calls)
        finally:
            async_module.VJ6530_RUNTIME = original_runtime


if __name__ == "__main__":
    unittest.main()

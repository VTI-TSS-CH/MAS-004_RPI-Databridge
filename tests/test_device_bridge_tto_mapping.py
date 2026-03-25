import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
import sys
import types

sys.modules.setdefault("ping3", types.SimpleNamespace(ping=lambda *args, **kwargs: None))

from mas004_rpi_databridge import device_bridge as device_bridge_module
from mas004_rpi_databridge.db import DB, now_ts
from mas004_rpi_databridge.device_bridge import DeviceBridge
from mas004_rpi_databridge.logstore import LogStore
from mas004_rpi_databridge.params import ParamStore


class FakeZbcBridgeClient:
    def __init__(self):
        self.read_calls = []
        self.write_calls = []
        self.invalidated_current_cache = 0
        self.invalidated_summary_cache = 0

    def read_mapped_value(self, mapping: str):
        self.read_calls.append(mapping)
        return "7"

    def write_mapped_value(self, mapping: str, value, verify_readback: bool = True):
        self.write_calls.append((mapping, str(value), verify_readback))
        return 0, str(value)

    def invalidate_current_parameters_cache(self):
        self.invalidated_current_cache += 1

    def invalidate_summary_cache(self):
        self.invalidated_summary_cache += 1


class FailingZbcBridgeClient(FakeZbcBridgeClient):
    def read_mapped_value(self, mapping: str):
        raise TimeoutError("timed out")


class FlakyReadZbcBridgeClient(FakeZbcBridgeClient):
    def __init__(self):
        super().__init__()
        self._failed = False

    def read_mapped_value(self, mapping: str):
        self.read_calls.append(mapping)
        if not self._failed:
            self._failed = True
            raise TimeoutError("timed out")
        return "49"


class FlakyWriteZbcBridgeClient(FakeZbcBridgeClient):
    def __init__(self):
        super().__init__()
        self._failed = False

    def write_mapped_value(self, mapping: str, value, verify_readback: bool = True):
        self.write_calls.append((mapping, str(value), verify_readback))
        if not self._failed:
            self._failed = True
            raise TimeoutError("timed out")
        return 0, str(value)


class RejectingStatusZbcBridgeClient(FakeZbcBridgeClient):
    def write_mapped_value(self, mapping: str, value, verify_readback: bool = True):
        self.write_calls.append((mapping, str(value), verify_readback))
        raise ValueError("printer state code 4 is derived and cannot be set directly")


class RuntimeSessionStub:
    def __init__(self, result, on_submit=None):
        self.result = result
        self.calls = []
        self.on_submit = on_submit

    def session_active(self) -> bool:
        return True

    def submit_session_request(self, operation: str, *args, **kwargs):
        self.calls.append((operation, args, kwargs))
        if self.on_submit is not None:
            self.on_submit(operation, *args, **kwargs)
        return self.result


class DeviceBridgeTtoMappingTests(unittest.TestCase):
    def test_zbc_mapping_read_and_write_use_bridge_client(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DB(str(Path(tmpdir) / "db.sqlite3"))
            params = ParamStore(db)
            logs = LogStore(db)

            with db._conn() as c:
                c.execute(
                    """INSERT INTO params(
                        pkey,ptype,pid,min_v,max_v,default_v,unit,rw,dtype,name,format_relevant,
                        message,possible_cause,effects,remedy,updated_ts
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "TTP00071",
                        "TTP",
                        "00071",
                        0,
                        1000,
                        "0",
                        "ms",
                        "R/W",
                        "unsigned int.",
                        "JobUpdateReplyDelay",
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
                        "TTP00071",
                        None,
                        "FRQ[CURRENT_PARAMETERS]/System/TCPIP/JobUpdateReplyDelay",
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
                esp_host="",
                esp_port=0,
                http_timeout_s=2.0,
                esp_watchdog_host="",
                watchdog_timeout_s=1.0,
                watchdog_down_after=3,
                vj6530_host="10.0.0.5",
                vj6530_port=3002,
                vj3350_host="",
                vj3350_port=0,
                esp_simulation=True,
                vj6530_simulation=False,
                vj3350_simulation=True,
            )

            bridge = DeviceBridge(cfg, params, logs)
            bridge._zbc_bridge = FakeZbcBridgeClient()

            read_resp = bridge.execute("vj6530", "TTP00071", "TTP", "read", "?")
            write_resp = bridge.execute("vj6530", "TTP00071", "TTP", "write", "9")

            self.assertEqual("TTP00071=7", read_resp)
            self.assertEqual("ACK_TTP00071=9", write_resp)
            self.assertEqual("9", params.get_effective_value("TTP00071"))

    def test_status_mapping_read_uses_cached_param_value(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DB(str(Path(tmpdir) / "db.sqlite3"))
            params = ParamStore(db)
            logs = LogStore(db)

            with db._conn() as c:
                c.execute(
                    """INSERT INTO params(
                        pkey,ptype,pid,min_v,max_v,default_v,unit,rw,esp_rw,dtype,name,format_relevant,
                        message,possible_cause,effects,remedy,updated_ts
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "TTP00073",
                        "TTP",
                        "00073",
                        0,
                        1,
                        "0",
                        None,
                        "R",
                        "W",
                        "Bool",
                        "PrinterOnlineState",
                        "NO",
                        None,
                        None,
                        None,
                        None,
                        now_ts(),
                    ),
                )
                c.execute(
                    """INSERT INTO param_values(pkey,value,updated_ts) VALUES(?,?,?)""",
                    ("TTP00073", "1", now_ts()),
                )
                c.execute(
                    """INSERT INTO param_device_map(
                        pkey, esp_key, zbc_mapping, zbc_message_id, zbc_command_id, zbc_value_codec,
                        zbc_scale, zbc_offset, ultimate_set_cmd, ultimate_get_cmd, ultimate_var_name, updated_ts
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "TTP00073",
                        None,
                        "STATUS[PRINTER_ONLINE]",
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
                esp_host="",
                esp_port=0,
                http_timeout_s=2.0,
                esp_watchdog_host="",
                watchdog_timeout_s=1.0,
                watchdog_down_after=3,
                vj6530_host="10.0.0.5",
                vj6530_port=3002,
                vj3350_host="",
                vj3350_port=0,
                esp_simulation=True,
                vj6530_simulation=False,
                vj3350_simulation=True,
            )

            bridge = DeviceBridge(cfg, params, logs)
            fake = FakeZbcBridgeClient()
            bridge._zbc_bridge = fake

            read_resp = bridge.execute("vj6530", "TTP00073", "TTP", "read", "?")

            self.assertEqual("TTP00073=1", read_resp)
            self.assertEqual([], fake.read_calls)

    def test_tts_status_code_write_is_allowed_for_esp_actor(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DB(str(Path(tmpdir) / "db.sqlite3"))
            params = ParamStore(db)
            logs = LogStore(db)

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
                esp_host="",
                esp_port=0,
                http_timeout_s=2.0,
                esp_watchdog_host="",
                watchdog_timeout_s=1.0,
                watchdog_down_after=3,
                vj6530_host="10.0.0.5",
                vj6530_port=3002,
                vj3350_host="",
                vj3350_port=0,
                esp_simulation=True,
                vj6530_simulation=False,
                vj3350_simulation=True,
            )

            bridge = DeviceBridge(cfg, params, logs)
            fake = FakeZbcBridgeClient()
            bridge._zbc_bridge = fake

            write_resp = bridge.execute("vj6530", "TTS0001", "TTS", "write", "3", actor="esp32")

            self.assertEqual("ACK_TTS0001=3", write_resp)
            self.assertEqual([("STATUS[PRINTER_STATE_CODE]", "3", True)], fake.write_calls)
            self.assertEqual("3", params.get_effective_value("TTS0001"))

    def test_tts_status_code_write_rejects_derived_target_cleanly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DB(str(Path(tmpdir) / "db.sqlite3"))
            params = ParamStore(db)
            logs = LogStore(db)

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
                esp_host="",
                esp_port=0,
                http_timeout_s=2.0,
                esp_watchdog_host="",
                watchdog_timeout_s=1.0,
                watchdog_down_after=3,
                vj6530_host="10.0.0.5",
                vj6530_port=3002,
                vj3350_host="",
                vj3350_port=0,
                esp_simulation=True,
                vj6530_simulation=False,
                vj3350_simulation=True,
            )

            bridge = DeviceBridge(cfg, params, logs)
            fake = RejectingStatusZbcBridgeClient()
            bridge._zbc_bridge = fake

            write_resp = bridge.execute("vj6530", "TTS0001", "TTS", "write", "4", actor="esp32")

            self.assertEqual("TTS0001=NAK_DeviceRejected", write_resp)

    def test_runtime_session_handles_live_status_write_without_second_bridge_client(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DB(str(Path(tmpdir) / "db.sqlite3"))
            params = ParamStore(db)
            logs = LogStore(db)

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
                esp_host="",
                esp_port=0,
                http_timeout_s=2.0,
                esp_watchdog_host="",
                watchdog_timeout_s=1.0,
                watchdog_down_after=3,
                vj6530_host="10.0.0.5",
                vj6530_port=3002,
                vj6530_async_enabled=True,
                vj3350_host="",
                vj3350_port=0,
                esp_simulation=True,
                vj6530_simulation=False,
                vj3350_simulation=True,
            )

            bridge = DeviceBridge(cfg, params, logs)
            fake = FakeZbcBridgeClient()
            bridge._zbc_bridge = fake
            runtime = RuntimeSessionStub((0, "3"))
            original_runtime = device_bridge_module.VJ6530_RUNTIME
            device_bridge_module.VJ6530_RUNTIME = runtime
            try:
                write_resp = bridge.execute("vj6530", "TTS0001", "TTS", "write", "3", actor="esp32")
            finally:
                device_bridge_module.VJ6530_RUNTIME = original_runtime

            self.assertEqual("ACK_TTS0001=3", write_resp)
            self.assertEqual([], fake.write_calls)
            self.assertEqual("3", params.get_effective_value("TTS0001"))
            self.assertEqual("write_mapped_value", runtime.calls[0][0])
            self.assertGreaterEqual(float(runtime.calls[0][2].get("timeout_s", 0.0) or 0.0), 72.0)

    def test_runtime_session_prefers_async_observed_state_over_stale_verified_value(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DB(str(Path(tmpdir) / "db.sqlite3"))
            params = ParamStore(db)
            logs = LogStore(db)

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
                    """INSERT INTO param_values(pkey,value,updated_ts) VALUES(?,?,?)""",
                    ("TTS0001", "0", now_ts()),
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
                esp_host="",
                esp_port=0,
                http_timeout_s=0.1,
                esp_watchdog_host="",
                watchdog_timeout_s=1.0,
                watchdog_down_after=3,
                vj6530_host="10.0.0.5",
                vj6530_port=3002,
                vj6530_async_enabled=True,
                vj3350_host="",
                vj3350_port=0,
                esp_simulation=True,
                vj6530_simulation=False,
                vj3350_simulation=True,
            )

            bridge = DeviceBridge(cfg, params, logs)
            fake = FakeZbcBridgeClient()
            bridge._zbc_bridge = fake

            def on_submit(operation, *args, **kwargs):
                params.apply_device_value("TTS0001", "3", promote_default=True)

            runtime = RuntimeSessionStub((0, "0"), on_submit=on_submit)
            original_runtime = device_bridge_module.VJ6530_RUNTIME
            original_sleep = device_bridge_module.time.sleep
            device_bridge_module.VJ6530_RUNTIME = runtime
            device_bridge_module.time.sleep = lambda *_args, **_kwargs: None
            try:
                write_resp = bridge.execute("vj6530", "TTS0001", "TTS", "write", "3", actor="esp32")
            finally:
                device_bridge_module.VJ6530_RUNTIME = original_runtime
                device_bridge_module.time.sleep = original_sleep

            self.assertEqual("ACK_TTS0001=3", write_resp)
            self.assertEqual([], fake.write_calls)
            self.assertEqual("3", params.get_effective_value("TTS0001"))

    def test_current_parameter_read_falls_back_to_cached_value(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DB(str(Path(tmpdir) / "db.sqlite3"))
            params = ParamStore(db)
            logs = LogStore(db)

            with db._conn() as c:
                c.execute(
                    """INSERT INTO params(
                        pkey,ptype,pid,min_v,max_v,default_v,unit,rw,esp_rw,dtype,name,format_relevant,
                        message,possible_cause,effects,remedy,updated_ts
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "TTP00002",
                        "TTP",
                        "00002",
                        0,
                        100,
                        "45",
                        None,
                        "R/W",
                        "W",
                        "unsigned int.",
                        "RadialRibbonWidthLimit",
                        "NO",
                        None,
                        None,
                        None,
                        None,
                        now_ts(),
                    ),
                )
                c.execute(
                    """INSERT INTO param_values(pkey,value,updated_ts) VALUES(?,?,?)""",
                    ("TTP00002", "47", now_ts()),
                )
                c.execute(
                    """INSERT INTO param_device_map(
                        pkey, esp_key, zbc_mapping, zbc_message_id, zbc_command_id, zbc_value_codec,
                        zbc_scale, zbc_offset, ultimate_set_cmd, ultimate_get_cmd, ultimate_var_name, updated_ts
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "TTP00002",
                        None,
                        "FRQ[CURRENT_PARAMETERS]/Devices/PHds/1/Consumables/RadialRibbonWidthLimit",
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
                esp_host="",
                esp_port=0,
                http_timeout_s=2.0,
                esp_watchdog_host="",
                watchdog_timeout_s=1.0,
                watchdog_down_after=3,
                vj6530_host="10.0.0.5",
                vj6530_port=3002,
                vj3350_host="",
                vj3350_port=0,
                esp_simulation=True,
                vj6530_simulation=False,
                vj3350_simulation=True,
            )

            bridge = DeviceBridge(cfg, params, logs)
            bridge._zbc_bridge = FailingZbcBridgeClient()

            read_resp = bridge.execute("vj6530", "TTP00002", "TTP", "read", "?")

            self.assertEqual("TTP00002=47", read_resp)

    def test_current_parameter_read_retries_before_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DB(str(Path(tmpdir) / "db.sqlite3"))
            params = ParamStore(db)
            logs = LogStore(db)

            with db._conn() as c:
                c.execute(
                    """INSERT INTO params(
                        pkey,ptype,pid,min_v,max_v,default_v,unit,rw,esp_rw,dtype,name,format_relevant,
                        message,possible_cause,effects,remedy,updated_ts
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "TTP00002",
                        "TTP",
                        "00002",
                        0,
                        100,
                        "45",
                        None,
                        "R/W",
                        "W",
                        "unsigned int.",
                        "RadialRibbonWidthLimit",
                        "NO",
                        None,
                        None,
                        None,
                        None,
                        now_ts(),
                    ),
                )
                c.execute(
                    """INSERT INTO param_values(pkey,value,updated_ts) VALUES(?,?,?)""",
                    ("TTP00002", "47", now_ts()),
                )
                c.execute(
                    """INSERT INTO param_device_map(
                        pkey, esp_key, zbc_mapping, zbc_message_id, zbc_command_id, zbc_value_codec,
                        zbc_scale, zbc_offset, ultimate_set_cmd, ultimate_get_cmd, ultimate_var_name, updated_ts
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "TTP00002",
                        None,
                        "FRQ[CURRENT_PARAMETERS]/Devices/PHds/1/Consumables/RadialRibbonWidthLimit",
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
                esp_host="",
                esp_port=0,
                http_timeout_s=2.0,
                esp_watchdog_host="",
                watchdog_timeout_s=1.0,
                watchdog_down_after=3,
                vj6530_host="10.0.0.5",
                vj6530_port=3002,
                vj3350_host="",
                vj3350_port=0,
                esp_simulation=True,
                vj6530_simulation=False,
                vj3350_simulation=True,
            )

            bridge = DeviceBridge(cfg, params, logs)
            flaky = FlakyReadZbcBridgeClient()
            bridge._zbc_bridge = flaky

            read_resp = bridge.execute("vj6530", "TTP00002", "TTP", "read", "?")

            self.assertEqual("TTP00002=49", read_resp)
            self.assertEqual(2, len(flaky.read_calls))
            self.assertEqual(1, flaky.invalidated_current_cache)
            self.assertEqual(1, flaky.invalidated_summary_cache)

    def test_current_parameter_write_retries_before_device_comm(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DB(str(Path(tmpdir) / "db.sqlite3"))
            params = ParamStore(db)
            logs = LogStore(db)

            with db._conn() as c:
                c.execute(
                    """INSERT INTO params(
                        pkey,ptype,pid,min_v,max_v,default_v,unit,rw,dtype,name,format_relevant,
                        message,possible_cause,effects,remedy,updated_ts
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "TTP00071",
                        "TTP",
                        "00071",
                        0,
                        1000,
                        "0",
                        "ms",
                        "R/W",
                        "unsigned int.",
                        "JobUpdateReplyDelay",
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
                        "TTP00071",
                        None,
                        "FRQ[CURRENT_PARAMETERS]/System/TCPIP/JobUpdateReplyDelay",
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
                esp_host="",
                esp_port=0,
                http_timeout_s=2.0,
                esp_watchdog_host="",
                watchdog_timeout_s=1.0,
                watchdog_down_after=3,
                vj6530_host="10.0.0.5",
                vj6530_port=3002,
                vj3350_host="",
                vj3350_port=0,
                esp_simulation=True,
                vj6530_simulation=False,
                vj3350_simulation=True,
            )

            bridge = DeviceBridge(cfg, params, logs)
            flaky = FlakyWriteZbcBridgeClient()
            bridge._zbc_bridge = flaky

            write_resp = bridge.execute("vj6530", "TTP00071", "TTP", "write", "9")

            self.assertEqual("ACK_TTP00071=9", write_resp)
            self.assertEqual("9", params.get_effective_value("TTP00071"))
            self.assertEqual(2, len(flaky.write_calls))
            self.assertEqual(1, flaky.invalidated_current_cache)
            self.assertEqual(1, flaky.invalidated_summary_cache)


if __name__ == "__main__":
    unittest.main()

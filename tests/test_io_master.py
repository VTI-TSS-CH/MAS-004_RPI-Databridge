import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from mas004_rpi_databridge.config import Settings
from mas004_rpi_databridge.db import DB
from mas004_rpi_databridge.io_master import IoStore
from mas004_rpi_databridge.io_runtime import IoRuntime


class IoMasterImportTests(unittest.TestCase):
    def build_workbook(self, path: Path):
        wb = Workbook()
        ws = wb.active
        ws.title = "Raspberry PLC 21 PINOUT"
        ws.append(["PLC Pinout", "Function"])
        ws.append(["Raspberry PLC21+ Pinout Digital I/Os", ""])
        ws.append(["I0.6", "USV Status OK"])
        ws.append(["Q0.0", "LED rot Taster Start/Pause"])

        ws = wb.create_sheet("ESP32 PLC 58 PINOUT")
        ws.append(["PLC Pinout", "Function"])
        ws.append(["ESP32 PLC58 Pinout Digital I/Os", ""])
        ws.append(["Zone A", ""])
        ws.append(["GPIO0", "Ansteuerung LED Streifen"])
        ws.append(["I0.0", "TTO Drucker Status"])

        ws = wb.create_sheet("IOLOGIK E1211 PINOUT Modul 1")
        ws.append(["PLC Pinout", "Function"])
        ws.append(["ioLogik E1211 Pinout Digital I/Os", ""])
        ws.append(["DO0", "Start Motor X-Achse (IN0)"])
        ws.append(["DO1", "Reserve"])

        ws = wb.create_sheet("IOLOGIK E1211 PINOUT Modul 2")
        ws.append(["PLC Pinout", "Function"])
        ws.append(["DO4", "LED Maschinenstatus rot"])

        wb.save(path)

    def test_import_xlsx_reads_known_devices_and_reserved_flags(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workbook_path = Path(tmpdir) / "io.xlsx"
            db_path = Path(tmpdir) / "io.sqlite3"
            self.build_workbook(workbook_path)

            store = IoStore(DB(str(db_path)))
            result = store.import_xlsx(str(workbook_path))

            self.assertTrue(result["ok"])
            self.assertEqual(7, result["channels"])
            self.assertEqual(4, result["devices"])

            points = store.list_points()
            by_pin = {item["pin_label"]: item for item in points}
            self.assertEqual("raspi_plc21", by_pin["I0.6"]["device_code"])
            self.assertEqual("gpio", by_pin["GPIO0"]["io_dir"])
            self.assertFalse(by_pin["DO0"]["is_reserved"])
            self.assertTrue(by_pin["DO1"]["is_reserved"])
            self.assertEqual(7, store.master_info()["channel_count"])

    def test_io_override_blocks_normal_runtime_writes_until_release(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workbook_path = Path(tmpdir) / "io.xlsx"
            db_path = Path(tmpdir) / "io.sqlite3"
            self.build_workbook(workbook_path)

            store = IoStore(DB(str(db_path)))
            store.import_xlsx(str(workbook_path))
            runtime = IoRuntime(
                Settings(
                    db_path=str(db_path),
                    peer_base_url="",
                    shared_secret="",
                    esp_simulation=True,
                    raspi_io_simulation=True,
                    moxa1_simulation=True,
                    moxa2_simulation=True,
                ),
                store,
            )

            io_key = "raspi_plc21__Q0_0"
            override_result = runtime.override_output(io_key, True, source="test-ui")
            self.assertTrue(override_result["override_active"])
            self.assertEqual("1", store.get_point(io_key)["override_value"])

            runtime_result = runtime.write_output(io_key, False)
            self.assertTrue(runtime_result["overridden"])
            point = store.get_point(io_key)
            self.assertEqual("1", point["value"])
            self.assertTrue(point["override_active"])

            release_result = runtime.release_override(io_key)
            self.assertTrue(release_result["released"])
            runtime.write_output(io_key, False)
            point = store.get_point(io_key)
            self.assertEqual("0", point["value"])
            self.assertFalse(point["override_active"])

    def test_gpio0_is_pulse_only_and_cannot_be_overridden(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workbook_path = Path(tmpdir) / "io.xlsx"
            db_path = Path(tmpdir) / "io.sqlite3"
            self.build_workbook(workbook_path)

            store = IoStore(DB(str(db_path)))
            store.import_xlsx(str(workbook_path))
            runtime = IoRuntime(
                Settings(
                    db_path=str(db_path),
                    peer_base_url="",
                    shared_secret="",
                    esp_simulation=True,
                    raspi_io_simulation=True,
                    moxa1_simulation=True,
                    moxa2_simulation=True,
                ),
                store,
            )

            with self.assertRaisesRegex(RuntimeError, "pulse-only"):
                runtime.override_output("esp32_plc58__GPIO0", True, source="test-ui")


if __name__ == "__main__":
    unittest.main()

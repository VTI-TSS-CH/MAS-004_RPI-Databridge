import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from mas004_rpi_databridge.db import DB
from mas004_rpi_databridge.io_master import IoStore


class IoMasterImportTests(unittest.TestCase):
    def build_workbook(self, path: Path):
        wb = Workbook()
        ws = wb.active
        ws.title = "Raspberry PLC 21 PINOUT"
        ws.append(["PLC Pinout", "Function"])
        ws.append(["I0.6", "USV Status OK"])
        ws.append(["Q0.0", "LED rot Taster Start/Pause"])

        ws = wb.create_sheet("ESP32 PLC 58 PINOUT")
        ws.append(["PLC Pinout", "Function"])
        ws.append(["Zone A", ""])
        ws.append(["GPIO0", "Ansteuerung LED Streifen"])
        ws.append(["I0.0", "TTO Drucker Status"])

        ws = wb.create_sheet("IOLOGIK E1211 PINOUT Modul 1")
        ws.append(["PLC Pinout", "Function"])
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


if __name__ == "__main__":
    unittest.main()

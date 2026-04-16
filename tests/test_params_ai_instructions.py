import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from mas004_rpi_databridge.db import DB
from mas004_rpi_databridge.params import ParamStore


class ParamImportAiInstructionsTests(unittest.TestCase):
    def test_import_xlsx_reads_ai_instruction_column(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workbook_path = Path(tmpdir) / "params.xlsx"
            db_path = Path(tmpdir) / "db.sqlite3"

            wb = Workbook()
            ws = wb.active
            ws.title = "Parameter"
            ws.append(
                [
                    "Params_Type.:",
                    "Param. ID.:",
                    "Min.:",
                    "Max.:",
                    "Default Value:",
                    "Einheit:",
                    "R/W:",
                    "ESP32 R/W:",
                    "Data Type:",
                    "Name:",
                    "KI-Anweisungen:",
                ]
            )
            ws.append(
                [
                    "MAP",
                    "0056",
                    0,
                    2100,
                    100,
                    "1/10mm",
                    "W",
                    "W",
                    "uint16",
                    "MAP0056 Soll-Position Portalachse X",
                    "Soll-Position (Motor-ID:1) in 1/10mm",
                ]
            )
            wb.save(workbook_path)

            store = ParamStore(DB(str(db_path)))
            result = store.import_xlsx(str(workbook_path))

            self.assertTrue(result["ok"])
            meta = store.get_meta("MAP0056")
            self.assertEqual("Soll-Position (Motor-ID:1) in 1/10mm", meta.get("ai_instructions"))


if __name__ == "__main__":
    unittest.main()

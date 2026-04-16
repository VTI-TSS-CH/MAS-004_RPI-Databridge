import unittest

from mas004_rpi_databridge.motor_bindings import build_motor_bindings


class MotorBindingsTests(unittest.TestCase):
    def test_build_motor_bindings_groups_setpoint_actual_and_fault(self):
        rows = [
            {
                "pkey": "MAP0056",
                "ptype": "MAP",
                "pid": "0056",
                "name": "MAP0056 Soll- Position Portalachse X",
                "unit": "1/10mm",
                "rw": "W",
                "esp_rw": "W",
                "dtype": "uint16",
                "message": "Soll-Position Portalachse X",
                "ai_instructions": "Soll-Position (Motor-ID:1) in 1/10mm",
            },
            {
                "pkey": "MAS0011",
                "ptype": "MAS",
                "pid": "0011",
                "name": "MAS0011 Ist Position Portalachse X",
                "unit": "1/10mm",
                "rw": "R",
                "esp_rw": "W",
                "dtype": "uint16",
                "message": "Ist Position Portalachse X",
                "ai_instructions": "IST-Position (Motor-ID:1) in 1/10mm",
            },
            {
                "pkey": "MAE0004",
                "ptype": "MAE",
                "pid": "0004",
                "name": "MAE0004 Störung Portalachsenmotor X",
                "unit": None,
                "rw": "R",
                "esp_rw": "W",
                "dtype": "bool",
                "message": "Störung Motor X-Achse",
                "ai_instructions": "Sammelstörung vom Oriental Motorcontroller mit Modbus (ID:1) wird High",
            },
        ]

        grouped = build_motor_bindings(rows)

        self.assertEqual(1, len(grouped))
        motor = grouped[0]
        self.assertEqual(1, motor["motor_id"])
        self.assertEqual("MAP0056", motor["setpoint"]["pkey"])
        self.assertEqual("MAS0011", motor["actual"]["pkey"])
        self.assertEqual("MAE0004", motor["fault"]["pkey"])


if __name__ == "__main__":
    unittest.main()

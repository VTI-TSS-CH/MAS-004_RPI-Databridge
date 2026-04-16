import unittest

from mas004_rpi_databridge.motor_catalog import merge_motor_payload


class MotorCatalogTests(unittest.TestCase):
    def test_merge_motor_payload_keeps_catalog_visible_without_live_endpoint(self):
        bindings = {1: {"motor_id": 1, "setpoint": {"pkey": "MAP0056"}}}
        merged = merge_motor_payload(None, bindings)
        self.assertTrue(merged["ok"])
        self.assertFalse(merged["live_available"])
        self.assertEqual(9, len(merged["motors"]))
        self.assertEqual("Motor X-Achse Tisch", merged["motors"][0]["name"])
        self.assertEqual("MAP0056", merged["motors"][0]["bindings"]["setpoint"]["pkey"])
        self.assertEqual("", merged["message"])

    def test_merge_motor_payload_overlays_live_data_onto_catalog(self):
        merged = merge_motor_payload(
            {
                "ok": True,
                "live_available": True,
                "motors": [
                    {
                        "id": 3,
                        "name": "Motor Etikettenantrieb",
                        "state": {"link_ok": True, "feedback_tenths_mm": 123},
                        "config": {"speed_mm_s": 77},
                    }
                ],
            },
            {},
        )
        motor = next(item for item in merged["motors"] if item["id"] == 3)
        self.assertTrue(merged["live_available"])
        self.assertTrue(motor["state"]["link_ok"])
        self.assertEqual(123, motor["state"]["feedback_tenths_mm"])
        self.assertEqual(77, motor["config"]["speed_mm_s"])

    def test_merge_motor_payload_keeps_cached_values_for_simulated_motor(self):
        merged = merge_motor_payload(
            None,
            {},
            simulated_ids={2},
            cached_motors={
                2: {
                    "id": 2,
                    "config": {"speed_mm_s": 55},
                    "state": {"feedback_tenths_mm": 999, "ready": True},
                    "last_reply": "cached",
                }
            },
        )
        motor = next(item for item in merged["motors"] if item["id"] == 2)
        self.assertTrue(motor["simulation"])
        self.assertEqual(999, motor["state"]["feedback_tenths_mm"])
        self.assertEqual(55, motor["config"]["speed_mm_s"])
        self.assertEqual("SIM", motor["state"]["link_ok"])


if __name__ == "__main__":
    unittest.main()

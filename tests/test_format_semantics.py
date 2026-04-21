import unittest

from mas004_rpi_databridge.format_semantics import build_format_plan


class FormatSemanticsTests(unittest.TestCase):
    def test_build_format_plan_applies_new_map_relationships(self):
        plan = build_format_plan(
            {
                "MAP0001": "500",
                "MAP0002": "1000",
                "MAP0003": "20",
                "MAP0004": "100",
                "MAP0005": "5",
                "MAP0006": "-10",
                "MAP0007": "12",
                "MAP0008": "40",
                "MAP0009": "45",
                "MAP0010": "20",
                "MAP0013": "1",
                "MAP0014": "100",
                "MAP0015": "200",
                "MAP0016": "0",
                "MAP0019": "11000",
                "MAP0027": "3",
                "MAP0028": "4",
                "MAP0029": "6",
                "MAP0030": "-2",
                "MAP0031": "11",
                "MAP0032": "12",
                "MAP0033": "-7",
                "MAP0039": "1",
                "MAP0040": "5",
                "MAP0066": "8000",
            }
        )

        self.assertEqual(500, plan["label"]["width_tenths_mm"])
        self.assertEqual(995, plan["label"]["length_min_tenths_mm"])
        self.assertEqual(1005, plan["label"]["length_max_tenths_mm"])
        self.assertEqual("tto", plan["printer"]["active"])
        self.assertEqual("MAP0019", plan["printer"]["distance_param"])
        self.assertEqual(11090, plan["printer"]["stop_distance_tenths_mm"])
        self.assertEqual(-22, plan["table"]["x_target_tenths_mm"])
        self.assertEqual(16, plan["table"]["z_target_tenths_mm"])
        self.assertEqual(406, plan["axes"]["label_detect_sensor_target_tenths_mm"])
        self.assertEqual(448, plan["axes"]["label_control_sensor_target_tenths_mm"])
        self.assertEqual(511, plan["axes"]["label_guide_infeed_target_tenths_mm"])
        self.assertEqual(512, plan["axes"]["label_guide_outfeed_target_tenths_mm"])
        self.assertTrue(plan["process"]["rewind_after_stop"])
        self.assertEqual(8000, plan["process"]["led_strip_first_led_distance_tenths_mm"])
        self.assertEqual("100mm", plan["process"]["roll_core_note"])

    def test_laser_selection_switches_print_distance(self):
        plan = build_format_plan({"MAP0016": "1", "MAP0018": "7000", "MAP0004": "50", "MAP0006": "5"})

        self.assertEqual("laser", plan["printer"]["active"])
        self.assertEqual("MAP0018", plan["printer"]["distance_param"])
        self.assertEqual(7055, plan["printer"]["stop_distance_tenths_mm"])


if __name__ == "__main__":
    unittest.main()

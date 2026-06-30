import unittest

from mas004_rpi_databridge.config import Settings
from mas004_rpi_databridge.esp_motors import EspMotorClient


class FakeEspMotorClient(EspMotorClient):
    def __init__(self):
        super().__init__(Settings(esp_host="127.0.0.1", esp_port=3010, esp_simulation=False))
        self.lines = []

    def _exchange(self, line: str) -> str:
        self.lines.append(line)
        if line == "MOTOR POLL?":
            return 'JSON {"ok":true,"auto_poll":false}'
        if line == "MOTOR POLL=1":
            return "ACK_MOTOR_POLL=1"
        if line == "MOTOR 3 REFRESH":
            return 'JSON {"ok":true,"motor":{"id":3,"state":{"link_ok":true}}}'
        if line == "MOTOR 7 REFRESH":
            return "NAK_MotorBusy"
        return 'JSON {"ok":true}'


class EspMotorClientTests(unittest.TestCase):
    def test_poll_state_and_refresh_commands(self):
        client = FakeEspMotorClient()

        self.assertFalse(client.poll_state()["auto_poll"])
        self.assertTrue(client.set_poll(True)["ok"])
        self.assertTrue(client.apply_eto_recovery()["ok"])
        self.assertTrue(client.recover_eto()["ok"])
        self.assertTrue(client.recover_eto_motor(3)["ok"])
        self.assertTrue(client.set_config(3, {"current_pct": 90, "hold_current_pct": 35})["ok"])
        self.assertTrue(client.set_current_position_mm(3, 12.5)["ok"])
        self.assertTrue(client.move_absolute_mm(3, 42.25)["ok"])
        self.assertTrue(client.refresh(3)["motor"]["state"]["link_ok"])
        self.assertEqual(
            [
                "MOTOR POLL?",
                "MOTOR POLL=1",
                "MOTOR APPLY_ETO_RECOVERY",
                "MOTOR RECOVER_ETO",
                "MOTOR 3 RECOVER_ETO",
                "MOTOR 3 SET current_pct=90 hold_current_pct=35",
                "MOTOR 3 SET_POSITION_MM=12.5",
                "MOTOR 3 MOVE_ABS_MM=42.25",
                "MOTOR 3 REFRESH",
            ],
            client.lines,
        )

    def test_nak_motor_busy_is_structured(self):
        client = FakeEspMotorClient()

        self.assertEqual(
            {"ok": False, "error": "NAK_MotorBusy", "reply": "NAK_MotorBusy"},
            client.refresh(7),
        )

    def test_position_axis_setup_writes_require_machine_setup_authority(self):
        client = FakeEspMotorClient()

        with self.assertRaisesRegex(RuntimeError, "nur ueber /ui/machine-setup/motors"):
            client.set_current_position_mm(7, 40.0)
        with self.assertRaisesRegex(RuntimeError, "nur ueber /ui/machine-setup/motors"):
            client.set_config(7, {"zero_offset_steps": 123})
        with self.assertRaisesRegex(RuntimeError, "nur ueber /ui/machine-setup/motors"):
            client.set_config(7, {"steps_per_mm": 1250})
        with self.assertRaisesRegex(RuntimeError, "nur ueber /ui/machine-setup/motors"):
            client.set_config(7, {"invert_direction": True})
        with self.assertRaisesRegex(RuntimeError, "nur ueber /ui/machine-setup/motors"):
            client.set_config(7, {"min_tenths_mm": -200})
        with self.assertRaisesRegex(RuntimeError, "nur ueber /ui/machine-setup/motors"):
            client.set_config(7, {"max_tenths_mm": 650})
        with self.assertRaisesRegex(RuntimeError, "nur speed_mm_s"):
            client.set_config(7, {"unknown_axis_value": 1})
        with self.assertRaisesRegex(RuntimeError, "nur ueber /ui/machine-setup/motors"):
            client.zero(7)
        with self.assertRaisesRegex(RuntimeError, "nur ueber /ui/machine-setup/motors"):
            client.set_min(7)
        with self.assertRaisesRegex(RuntimeError, "nur ueber /ui/machine-setup/motors"):
            client.set_max(7)
        with self.assertRaisesRegex(RuntimeError, "nur ueber /ui/machine-setup/motors"):
            client.save(7)

        self.assertTrue(
            client.set_config(
                7,
                {
                    "speed_mm_s": 12.5,
                    "current_pct": 70,
                    "hold_current_pct": 40,
                    "accel_mm_s2": 50,
                    "decel_mm_s2": 45,
                },
            )["ok"]
        )
        self.assertTrue(
            client.set_current_position_mm(7, 40.0, allow_machine_setup_write=True)["ok"]
        )
        self.assertTrue(
            client.set_config(7, {"zero_offset_steps": 123}, allow_machine_setup_write=True)["ok"]
        )
        self.assertTrue(client.zero(7, allow_machine_setup_write=True)["ok"])
        self.assertTrue(client.set_min(7, allow_machine_setup_write=True)["ok"])
        self.assertTrue(client.set_max(7, allow_machine_setup_write=True)["ok"])
        self.assertTrue(client.save(7, allow_machine_setup_write=True)["ok"])
        self.assertEqual(
            [
                "MOTOR 7 SET speed_mm_s=12.5 current_pct=70 hold_current_pct=40 accel_mm_s2=50 decel_mm_s2=45",
                "MOTOR 7 SETUP_WRITE_ARM",
                "MOTOR 7 SET_POSITION_MM=40",
                "MOTOR 7 SETUP_WRITE_ARM",
                "MOTOR 7 SET zero_offset_steps=123",
                "MOTOR 7 SETUP_WRITE_ARM",
                "MOTOR 7 ZERO",
                "MOTOR 7 SETUP_WRITE_ARM",
                "MOTOR 7 SET_MIN",
                "MOTOR 7 SETUP_WRITE_ARM",
                "MOTOR 7 SET_MAX",
                "MOTOR 7 SETUP_WRITE_ARM",
                "MOTOR 7 SAVE",
            ],
            client.lines,
        )


if __name__ == "__main__":
    unittest.main()

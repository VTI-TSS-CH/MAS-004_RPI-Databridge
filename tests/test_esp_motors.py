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
        return 'JSON {"ok":true}'


class EspMotorClientTests(unittest.TestCase):
    def test_poll_state_and_refresh_commands(self):
        client = FakeEspMotorClient()

        self.assertFalse(client.poll_state()["auto_poll"])
        self.assertTrue(client.set_poll(True)["ok"])
        self.assertTrue(client.apply_eto_recovery()["ok"])
        self.assertTrue(client.recover_eto()["ok"])
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
                "MOTOR 3 SET current_pct=90 hold_current_pct=35",
                "MOTOR 3 SET_POSITION_MM=12.5",
                "MOTOR 3 MOVE_ABS_MM=42.25",
                "MOTOR 3 REFRESH",
            ],
            client.lines,
        )


if __name__ == "__main__":
    unittest.main()

import unittest
from unittest.mock import patch

import httpx

from mas004_rpi_databridge.config import Settings
from mas004_rpi_databridge.smart_wickler_client import SmartWicklerClient, normalize_winder_role


class SmartWicklerClientTests(unittest.TestCase):
    def test_normalize_winder_role_rejects_unknown_values(self):
        with self.assertRaises(ValueError):
            normalize_winder_role("other")

    def test_simulation_payload_is_returned_when_endpoint_is_disabled(self):
        cfg = Settings()
        cfg.smart_unwinder_host = "192.168.2.104"
        cfg.smart_unwinder_port = 3011
        cfg.smart_unwinder_simulation = True
        payload = SmartWicklerClient(cfg, "unwinder").fetch_state()
        self.assertTrue(payload["device"]["simulation"])
        self.assertFalse(payload["device"]["reachable"])
        self.assertEqual("Abwickler", payload["config"]["roleLabel"])
        self.assertEqual(100.0, payload["telemetry"]["fillPercent"])

    def test_offline_payload_keeps_endpoint_coordinates_when_live_is_selected(self):
        cfg = Settings()
        cfg.smart_rewinder_host = "192.168.2.105"
        cfg.smart_rewinder_port = 3012
        cfg.smart_rewinder_simulation = False
        payload = SmartWicklerClient(cfg, "rewinder").fetch_state()
        self.assertEqual("192.168.2.105", payload["device"]["host"])
        self.assertEqual(3012, payload["device"]["port"])
        self.assertFalse(payload["device"]["simulation"])
        self.assertFalse(payload["device"]["reachable"])

    def test_post_form_wraps_http_timeout_with_role_endpoint_and_payload(self):
        cfg = Settings()
        cfg.smart_unwinder_host = "10.141.94.216"
        cfg.smart_unwinder_port = 3011
        cfg.smart_unwinder_simulation = False

        with patch("mas004_rpi_databridge.smart_wickler_client.httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.post.side_effect = httpx.TimeoutException("timed out")
            with self.assertRaisesRegex(
                RuntimeError,
                r"Abwickler \(unwinder\) POST http://10\.141\.94\.216:3011/api/mode .*mode.*ready.*timeout",
            ):
                SmartWicklerClient(cfg, "unwinder").post_mode("ready", timeout_s=2.5)


if __name__ == "__main__":
    unittest.main()

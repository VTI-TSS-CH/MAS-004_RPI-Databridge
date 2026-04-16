import unittest

from mas004_rpi_databridge.config import Settings
from mas004_rpi_databridge.peers import sender_lanes, url_matches_peer_base


class SenderLaneTests(unittest.TestCase):
    def test_primary_and_aux_lanes_are_split_when_primary_peer_is_configured(self):
        cfg = Settings(
            peer_base_url="http://192.168.210.10:81",
            peer_base_url_secondary="https://192.168.5.2:9090",
        )

        lanes = sender_lanes(cfg)

        self.assertEqual(["primary", "aux"], [lane.name for lane in lanes])
        self.assertEqual(("http://192.168.210.10:81",), lanes[0].url_prefixes)
        self.assertTrue(lanes[0].use_primary_watchdog)
        self.assertEqual(("http://192.168.210.10:81",), lanes[1].exclude_url_prefixes)
        self.assertFalse(lanes[1].use_primary_watchdog)

    def test_default_lane_handles_everything_without_primary_peer(self):
        cfg = Settings(peer_base_url="", peer_base_url_secondary="")

        lanes = sender_lanes(cfg)

        self.assertEqual(["default"], [lane.name for lane in lanes])
        self.assertEqual((), lanes[0].url_prefixes)
        self.assertEqual((), lanes[0].exclude_url_prefixes)
        self.assertFalse(lanes[0].use_primary_watchdog)

    def test_url_matches_peer_base_for_exact_and_nested_paths(self):
        self.assertTrue(url_matches_peer_base("https://192.168.5.2:9090", "https://192.168.5.2:9090"))
        self.assertTrue(url_matches_peer_base("https://192.168.5.2:9090/api/inbox", "https://192.168.5.2:9090"))
        self.assertFalse(url_matches_peer_base("https://192.168.5.3:9090/api/inbox", "https://192.168.5.2:9090"))


if __name__ == "__main__":
    unittest.main()

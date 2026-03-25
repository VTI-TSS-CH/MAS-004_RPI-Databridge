import unittest

from mas004_rpi_databridge.protocol import build_value, parse_operation_line, parse_param_line


class ProtocolParsingTests(unittest.TestCase):
    def test_tts_operation_normalizes_four_digit_pid(self):
        self.assertEqual(("TTS", "0001", "read", "?"), parse_operation_line("TTS1=?"))

    def test_tts_value_and_ack_use_four_digit_pid(self):
        self.assertEqual("TTS0001=3", build_value("TTS", "1", "3"))
        parsed = parse_param_line("ACK_TTS1=3")
        self.assertIsNotNone(parsed)
        self.assertEqual("TTS", parsed.ptype)
        self.assertEqual("0001", parsed.pid)
        self.assertEqual("3", parsed.value)
        self.assertTrue(parsed.is_ack)


if __name__ == "__main__":
    unittest.main()

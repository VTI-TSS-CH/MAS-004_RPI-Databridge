import unittest

from mas004_rpi_databridge.vj6530_async_policy import vj6530_async_reconnect_delay_s


class Vj6530AsyncRuntimeTests(unittest.TestCase):
    def test_socket_closed_reconnects_immediately(self):
        delay_s = vj6530_async_reconnect_delay_s(RuntimeError("socket closed"), 2.0)
        self.assertEqual(0.2, delay_s)

    def test_other_errors_keep_backoff(self):
        delay_s = vj6530_async_reconnect_delay_s(RuntimeError("no working ZBC transport profile detected"), 3.6)
        self.assertEqual(3.6, delay_s)


if __name__ == "__main__":
    unittest.main()

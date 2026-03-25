import unittest

from mas004_rpi_databridge.vj6530_async_policy import vj6530_async_reconnect_delay_s
from mas004_rpi_databridge.vj6530_runtime import Vj6530RuntimeState


class Vj6530AsyncRuntimeTests(unittest.TestCase):
    def test_socket_closed_reconnects_immediately(self):
        delay_s = vj6530_async_reconnect_delay_s(RuntimeError("socket closed"), 2.0)
        self.assertEqual(0.2, delay_s)

    def test_other_errors_keep_backoff(self):
        delay_s = vj6530_async_reconnect_delay_s(RuntimeError("no working ZBC transport profile detected"), 3.6)
        self.assertEqual(3.6, delay_s)

    def test_session_request_roundtrip(self):
        runtime = Vj6530RuntimeState()
        runtime.mark_session_active(True)
        seen = []

        def worker():
            request = runtime.next_session_request(timeout_s=1.0)
            seen.append(request.operation)
            request.set_result("ok")

        import threading

        thread = threading.Thread(target=worker)
        thread.start()
        result = runtime.submit_session_request("read_mapped_values", {"TTS0001": "STATUS[PRINTER_STATE_CODE]"}, timeout_s=1.0)
        thread.join(timeout=1.0)

        self.assertEqual("ok", result)
        self.assertEqual(["read_mapped_values"], seen)


if __name__ == "__main__":
    unittest.main()

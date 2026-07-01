import socket
import threading
import unittest

from mas004_rpi_databridge.device_clients import EspPlcClient, motor_setup_write_context


class _LineServer:
    def __init__(self, response: str, *, keep_open: bool = True):
        self.response = response.encode("utf-8")
        self.keep_open = bool(keep_open)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.host, self.port = self.sock.getsockname()
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self.connection_count = 0
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def close(self):
        self._closed.set()
        try:
            socket.create_connection((self.host, self.port), timeout=0.2).close()
        except Exception:
            pass
        self.thread.join(timeout=1)
        self.sock.close()

    def _run(self):
        while not self._closed.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with self._lock:
                self.connection_count += 1
            with conn:
                keep_client_open = True
                while keep_client_open and not self._closed.is_set():
                    data = b""
                    while b"\n" not in data:
                        chunk = conn.recv(1024)
                        if not chunk:
                            keep_client_open = False
                            break
                        data += chunk
                    if b"\n" in data:
                        conn.sendall(self.response)
                        if not self.keep_open:
                            keep_client_open = False


class EspPlcClientTests(unittest.TestCase):
    def test_exchange_line_reads_large_single_line_when_limit_allows_it(self):
        server = _LineServer("JSON " + ("x" * 12000) + "\n")
        try:
            client = EspPlcClient(server.host, server.port, timeout_s=1.0)
            reply = client.exchange_line("PING", read_timeout_s=1.0, read_limit=20000)
            self.assertTrue(reply.startswith("JSON "))
            self.assertGreater(len(reply), 12000)
        finally:
            server.close()

    def test_exchange_line_closes_answered_connection(self):
        server = _LineServer("PONG\n")
        try:
            client = EspPlcClient(server.host, server.port, timeout_s=1.0)
            self.assertEqual("PONG", client.exchange_line("PING", read_timeout_s=1.0))
            self.assertEqual("PONG", client.exchange_line("PING", read_timeout_s=1.0))
            self.assertEqual(2, server.connection_count)
            self.assertFalse(client.diagnostics()["connected"])
        finally:
            client.close()
            server.close()

    def test_exchange_line_reconnects_when_peer_closes(self):
        server = _LineServer("PONG\n", keep_open=False)
        client = EspPlcClient(server.host, server.port, timeout_s=1.0)
        try:
            self.assertEqual("PONG", client.exchange_line("PING", read_timeout_s=1.0))
            self.assertEqual("PONG", client.exchange_line("PING", read_timeout_s=1.0))
            self.assertGreaterEqual(server.connection_count, 2)
        finally:
            client.close()
            server.close()

    def test_position_axis_setup_writes_are_blocked_without_motor_setup_context(self):
        server = _LineServer("ACK\n")
        client = EspPlcClient(server.host, server.port, timeout_s=1.0)
        blocked = [
            "MOTOR 7 SETUP_WRITE_ARM",
            "MOTOR 7 SAVE",
            "MOTOR 7 ZERO",
            "MOTOR 7 SET_MIN",
            "MOTOR 7 SET_MAX",
            "MOTOR 7 SET_POSITION_MM=40",
            "MOTOR 7 SET zero_offset_steps=123",
            "MOTOR 7 SET min_tenths_mm=-200",
            "MOTOR 7 SET max_tenths_mm=650",
            "MOTOR 7 SET steps_per_mm=1250",
            "MOTOR 7 SET invert_direction=1",
        ]
        try:
            for line in blocked:
                with self.subTest(line=line):
                    with self.assertRaisesRegex(RuntimeError, "motor setup write blocked"):
                        client.exchange_line(line, read_timeout_s=1.0)
            self.assertEqual(
                "ACK",
                client.exchange_line(
                    "MOTOR 7 SET speed_mm_s=12 current_pct=70 hold_current_pct=40 accel_mm_s2=50 decel_mm_s2=45",
                    read_timeout_s=1.0,
                ),
            )
        finally:
            client.close()
            server.close()

    def test_position_axis_setup_writes_are_allowed_in_motor_setup_context(self):
        server = _LineServer("ACK\n")
        client = EspPlcClient(server.host, server.port, timeout_s=1.0)
        try:
            with motor_setup_write_context("unit-test"):
                self.assertEqual("ACK", client.exchange_line("MOTOR 7 SETUP_WRITE_ARM", read_timeout_s=1.0))
                self.assertEqual("ACK", client.exchange_line("MOTOR 7 SET zero_offset_steps=123", read_timeout_s=1.0))
        finally:
            client.close()
            server.close()


if __name__ == "__main__":
    unittest.main()

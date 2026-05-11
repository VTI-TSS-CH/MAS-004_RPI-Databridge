import socket
import threading
import unittest

from mas004_rpi_databridge.device_clients import EspPlcClient


class _LineServer:
    def __init__(self, response: str):
        self.response = response.encode("utf-8")
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
                data = b""
                while b"\n" not in data:
                    chunk = conn.recv(1024)
                    if not chunk:
                        break
                    data += chunk
                conn.sendall(self.response)


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

    def test_exchange_line_uses_short_lived_connections(self):
        server = _LineServer("PONG\n")
        try:
            client = EspPlcClient(server.host, server.port, timeout_s=1.0)
            self.assertEqual("PONG", client.exchange_line("PING", read_timeout_s=1.0))
            self.assertEqual("PONG", client.exchange_line("PING", read_timeout_s=1.0))
            self.assertGreaterEqual(server.connection_count, 2)
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()

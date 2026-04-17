import socket
import struct
import threading
import unittest

from mas004_rpi_databridge.moxa_iologik import MoxaE1211Client


class _FakeMoxaServer:
    def __init__(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self.host, self.port = self._sock.getsockname()
        self._sock.listen(5)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._stop = threading.Event()
        self.coils = [1, 0, 1, 0] + [0] * 12

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        try:
            with socket.create_connection((self.host, self.port), timeout=0.2):
                pass
        except Exception:
            pass
        self._sock.close()
        self._thread.join(timeout=1.0)

    def _run(self):
        while not self._stop.is_set():
            try:
                client, _addr = self._sock.accept()
            except Exception:
                break
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket):
        with client:
            try:
                header = self._recv_exact(client, 7)
            except RuntimeError:
                return
            tx_id, proto_id, length, unit_id = struct.unpack(">HHHB", header)
            body = self._recv_exact(client, length - 1)
            function_code = body[0]
            payload = body[1:]
            if function_code == 0x01:
                start, count = struct.unpack(">HH", payload[:4])
                values = self.coils[start : start + count]
                packed = bytearray(max(1, (count + 7) // 8))
                for idx, value in enumerate(values):
                    if value:
                        packed[idx // 8] |= 1 << (idx % 8)
                response_pdu = struct.pack(">BB", function_code, len(packed)) + bytes(packed)
            elif function_code == 0x05:
                address, raw_value = struct.unpack(">HH", payload[:4])
                self.coils[address] = 1 if raw_value == 0xFF00 else 0
                response_pdu = struct.pack(">BHH", function_code, address, raw_value)
            else:
                response_pdu = struct.pack(">BB", function_code | 0x80, 0x01)
            response = struct.pack(">HHHB", tx_id, proto_id, len(response_pdu) + 1, unit_id) + response_pdu
            client.sendall(response)

    @staticmethod
    def _recv_exact(client: socket.socket, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = client.recv(size - len(data))
            if not chunk:
                raise RuntimeError("client closed")
            data.extend(chunk)
        return bytes(data)


class MoxaIoLogikClientTests(unittest.TestCase):
    def test_read_outputs_and_write_single_output(self):
        server = _FakeMoxaServer()
        server.start()
        try:
            client = MoxaE1211Client(server.host, server.port, timeout_s=1.0)
            outputs = client.read_outputs()
            self.assertEqual(1, outputs["DO0"])
            self.assertEqual(0, outputs["DO1"])
            self.assertEqual(1, outputs["DO2"])

            client.write_output(1, True)
            outputs_after = client.read_outputs()
            self.assertEqual(1, outputs_after["DO1"])
        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main()

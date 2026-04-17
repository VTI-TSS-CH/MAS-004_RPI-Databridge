from __future__ import annotations

import socket
import struct
import threading
from typing import Dict, List


class MoxaE1211Client:
    def __init__(self, host: str, port: int = 502, timeout_s: float = 2.0, unit_id: int = 1):
        self.host = (host or "").strip()
        self.port = int(port or 0)
        self.timeout_s = float(timeout_s or 2.0)
        self.unit_id = int(unit_id or 1) & 0xFF
        self._tx_id = 0
        self._lock = threading.Lock()

    def _next_tx_id(self) -> int:
        with self._lock:
            self._tx_id = (self._tx_id + 1) & 0xFFFF
            if self._tx_id == 0:
                self._tx_id = 1
            return self._tx_id

    def _request(self, function_code: int, payload: bytes) -> bytes:
        if not self.host or self.port <= 0:
            raise RuntimeError("MOXA endpoint missing")

        tx_id = self._next_tx_id()
        pdu = struct.pack(">B", function_code) + (payload or b"")
        mbap = struct.pack(">HHHB", tx_id, 0, len(pdu) + 1, self.unit_id)

        with socket.create_connection((self.host, self.port), timeout=self.timeout_s) as sock:
            sock.settimeout(self.timeout_s)
            sock.sendall(mbap + pdu)
            header = self._recv_exact(sock, 7)
            rx_tx_id, _proto, length, _unit = struct.unpack(">HHHB", header)
            if rx_tx_id != tx_id:
                raise RuntimeError("MOXA transaction mismatch")
            body = self._recv_exact(sock, max(0, length - 1))

        if not body:
            raise RuntimeError("MOXA empty response")
        response_fc = body[0]
        if response_fc & 0x80:
            code = body[1] if len(body) > 1 else 0
            raise RuntimeError(f"MOXA exception {code}")
        if response_fc != function_code:
            raise RuntimeError(f"MOXA unexpected function code {response_fc}")
        return body[1:]

    def read_coils(self, start_address: int, count: int) -> List[int]:
        if count <= 0:
            return []
        payload = struct.pack(">HH", int(start_address), int(count))
        body = self._request(0x01, payload)
        if not body:
            raise RuntimeError("MOXA invalid coil response")
        byte_count = body[0]
        raw = body[1 : 1 + byte_count]
        out: List[int] = []
        for idx in range(int(count)):
            byte_idx = idx // 8
            bit_idx = idx % 8
            value = 0
            if byte_idx < len(raw):
                value = 1 if ((raw[byte_idx] >> bit_idx) & 0x01) else 0
            out.append(value)
        return out

    def write_single_coil(self, address: int, enabled: bool) -> bool:
        payload = struct.pack(">HH", int(address), 0xFF00 if bool(enabled) else 0x0000)
        body = self._request(0x05, payload)
        if len(body) < 4:
            raise RuntimeError("MOXA invalid write response")
        written_addr, written_value = struct.unpack(">HH", body[:4])
        return written_addr == int(address) and written_value == (0xFF00 if bool(enabled) else 0x0000)

    def read_outputs(self) -> Dict[str, int]:
        values = self.read_coils(0, 16)
        return {f"DO{idx}": int(values[idx]) for idx in range(len(values))}

    def write_output(self, channel_no: int, enabled: bool) -> Dict[str, int]:
        ok = self.write_single_coil(int(channel_no), bool(enabled))
        if not ok:
            raise RuntimeError(f"MOXA write failed for DO{int(channel_no)}")
        return {f"DO{int(channel_no)}": 1 if enabled else 0}

    @staticmethod
    def _recv_exact(sock: socket.socket, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = sock.recv(size - len(data))
            if not chunk:
                raise RuntimeError("socket closed during read")
            data.extend(chunk)
        return bytes(data)

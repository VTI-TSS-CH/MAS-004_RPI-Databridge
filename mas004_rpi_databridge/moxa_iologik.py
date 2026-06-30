from __future__ import annotations

import re
import socket
import struct
import threading
from typing import Dict, List, Optional, Sequence


class MoxaIoLogikClient:
    E1211_OUTPUT_LABELS = tuple(f"DO{idx}" for idx in range(16))
    E1213_OUTPUT_LABELS = ("DO0", "DO1", "DO2", "DO3", "DIO0", "DIO1", "DIO2", "DIO3")

    def __init__(
        self,
        host: str,
        port: int = 502,
        timeout_s: float = 2.0,
        unit_id: int = 1,
        model: str = "e1211",
    ):
        self.host = (host or "").strip()
        self.port = int(port or 0)
        self.timeout_s = float(timeout_s or 2.0)
        self.unit_id = int(unit_id or 1) & 0xFF
        self.model = str(model or "e1211").strip().lower()
        self._tx_id = 0
        self._lock = threading.RLock()
        self._sock: Optional[socket.socket] = None

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    def _socket_locked(self) -> socket.socket:
        if self._sock is None:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
            sock.settimeout(self.timeout_s)
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except Exception:
                pass
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            except Exception:
                pass
            self._sock = sock
        return self._sock

    def _next_tx_id(self) -> int:
        with self._lock:
            self._tx_id = (self._tx_id + 1) & 0xFFFF
            if self._tx_id == 0:
                self._tx_id = 1
            return self._tx_id

    def _request(self, function_code: int, payload: bytes) -> bytes:
        if not self.host or self.port <= 0:
            raise RuntimeError("MOXA endpoint missing")

        with self._lock:
            last_error: Optional[Exception] = None
            for attempt in range(2):
                tx_id = self._next_tx_id()
                pdu = struct.pack(">B", function_code) + (payload or b"")
                mbap = struct.pack(">HHHB", tx_id, 0, len(pdu) + 1, self.unit_id)
                try:
                    sock = self._socket_locked()
                    sock.sendall(mbap + pdu)
                    header = self._recv_exact(sock, 7)
                    rx_tx_id, _proto, length, _unit = struct.unpack(">HHHB", header)
                    if rx_tx_id != tx_id:
                        raise RuntimeError("MOXA transaction mismatch")
                    body = self._recv_exact(sock, max(0, length - 1))
                    break
                except Exception as exc:
                    last_error = exc
                    self._close_locked()
                    if attempt >= 1:
                        raise
            else:
                raise last_error or RuntimeError("MOXA request failed")

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

    def output_labels(self) -> Sequence[str]:
        if self.model == "e1213":
            return self.E1213_OUTPUT_LABELS
        return self.E1211_OUTPUT_LABELS

    @staticmethod
    def output_address(pin_label: str) -> int:
        pin = str(pin_label or "").strip().upper().replace(" ", "")
        match = re.fullmatch(r"DO(\d+)", pin)
        if match:
            return int(match.group(1))
        match = re.fullmatch(r"DIO(\d+)", pin)
        if match:
            # E1213 exposes the four configurable DIO outputs after DO0..DO3.
            return 4 + int(match.group(1))
        raise RuntimeError(f"Unsupported MOXA output label '{pin_label}'")

    def read_outputs(self, labels: Optional[Sequence[str]] = None) -> Dict[str, int]:
        output_labels = tuple(labels or self.output_labels())
        if not output_labels:
            return {}
        max_address = max(self.output_address(label) for label in output_labels)
        values = self.read_coils(0, max_address + 1)
        return {label: int(values[self.output_address(label)]) for label in output_labels}

    def write_output(self, channel_no: int, enabled: bool) -> Dict[str, int]:
        ok = self.write_single_coil(int(channel_no), bool(enabled))
        if not ok:
            raise RuntimeError(f"MOXA write failed for DO{int(channel_no)}")
        return {f"DO{int(channel_no)}": 1 if enabled else 0}

    def write_output_label(self, pin_label: str, enabled: bool) -> Dict[str, int]:
        address = self.output_address(pin_label)
        ok = self.write_single_coil(address, bool(enabled))
        if not ok:
            raise RuntimeError(f"MOXA write failed for {pin_label}")
        return {str(pin_label).strip().upper(): 1 if enabled else 0}

    @staticmethod
    def _recv_exact(sock: socket.socket, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = sock.recv(size - len(data))
            if not chunk:
                raise RuntimeError("socket closed during read")
            data.extend(chunk)
        return bytes(data)


class MoxaE1211Client(MoxaIoLogikClient):
    def __init__(self, host: str, port: int = 502, timeout_s: float = 2.0, unit_id: int = 1):
        super().__init__(host, port=port, timeout_s=timeout_s, unit_id=unit_id, model="e1211")


class MoxaE1213Client(MoxaIoLogikClient):
    def __init__(self, host: str, port: int = 502, timeout_s: float = 2.0, unit_id: int = 1):
        super().__init__(host, port=port, timeout_s=timeout_s, unit_id=unit_id, model="e1213")

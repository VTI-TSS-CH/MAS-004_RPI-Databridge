from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from typing import Iterable, Optional

from ping3 import ping

from mas004_rpi_databridge.device_protocols import (
    ZBC_START,
    ZBC_FLAG_ACK,
    ZBC_FLAG_NAK,
    build_ultimate_command,
    build_zbc_ack,
    build_zbc_message,
    build_zbc_packet,
    parse_ultimate_result,
    parse_zbc_message,
    parse_zbc_packet,
)


class _EndpointState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.fail_count = 0
        self.next_allowed_at = 0.0
        self.sock: Optional[socket.socket] = None
        self.exchange_count = 0

    def close_socket(self) -> None:
        sock = self.sock
        self.sock = None
        self.exchange_count = 0
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


_ESP_ENDPOINTS_GUARD = threading.Lock()
_ESP_ENDPOINTS: dict[tuple[str, int], _EndpointState] = {}


def _esp_endpoint_state(host: str, port: int) -> _EndpointState:
    key = ((host or "").strip(), int(port or 0))
    with _ESP_ENDPOINTS_GUARD:
        state = _ESP_ENDPOINTS.get(key)
        if state is None:
            state = _EndpointState()
            _ESP_ENDPOINTS[key] = state
        return state


@dataclass
class DeviceWatchdog:
    host: str
    timeout_s: float
    down_after: int = 3
    _fails: int = 0

    def check(self) -> bool:
        if not self.host:
            return True
        try:
            ok = ping(self.host, timeout=self.timeout_s, unit="s") is not None
        except Exception:
            ok = False
        self._fails = 0 if ok else (self._fails + 1)
        return self._fails < max(1, int(self.down_after))


class EspPlcClient:
    def __init__(self, host: str, port: int, timeout_s: float):
        self.host = (host or "").strip()
        self.port = int(port or 0)
        self.timeout_s = float(timeout_s or 1.0)

    def exchange_line(
        self,
        line: str,
        read_timeout_s: float | None = None,
        *,
        read_limit: int = 8192,
    ) -> str:
        if not self.host or self.port <= 0:
            raise RuntimeError("ESP endpoint missing")

        payload = ((line or "").strip() + "\n").encode("utf-8")
        read_timeout_s = float(read_timeout_s or self.timeout_s)
        state = _esp_endpoint_state(self.host, self.port)

        with state.lock:
            deadline = time.monotonic() + max(self.timeout_s + read_timeout_s + 6.0, 8.0)
            last_error: Optional[Exception] = None
            for attempt in range(5):
                now = time.monotonic()
                if state.next_allowed_at > now:
                    time.sleep(min(state.next_allowed_at - now, max(0.0, deadline - now)))
                if time.monotonic() >= deadline:
                    break
                try:
                    if state.sock is None:
                        state.sock = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
                        _tune_short_lived_socket(state.sock)
                    state.sock.settimeout(read_timeout_s)
                    state.sock.sendall(payload)
                    reply = _recv_line(state.sock, limit=read_limit).strip()
                    if not reply:
                        raise RuntimeError("ESP endpoint empty reply")
                    state.exchange_count += 1
                    if state.exchange_count >= 40:
                        state.close_socket()
                        state.next_allowed_at = time.monotonic() + 0.15
                    state.fail_count = 0
                    if state.exchange_count != 0:
                        state.next_allowed_at = time.monotonic() + 0.005
                    return reply
                except Exception as exc:
                    last_error = exc
                    state.close_socket()
                    state.fail_count += 1
                    # Avoid storms against the ESP/W5500 socket while still retrying
                    # inside this call, so a transient stale socket does not lose a command.
                    state.next_allowed_at = time.monotonic() + min(1.0, 0.12 * (2 ** min(state.fail_count, 3)))
            if last_error is not None:
                raise last_error
            raise RuntimeError("ESP endpoint cooldown active")


class UltimateClient:
    def __init__(self, host: str, port: int, timeout_s: float):
        self.host = (host or "").strip()
        self.port = int(port or 0)
        self.timeout_s = float(timeout_s or 1.0)

    def command(self, command: str, args: Iterable[str] | None = None) -> tuple[bool, str, list[str]]:
        if not self.host or self.port <= 0:
            raise RuntimeError("Ultimate endpoint missing")

        payload = build_ultimate_command(command, args)
        with socket.create_connection((self.host, self.port), timeout=self.timeout_s) as sock:
            sock.settimeout(self.timeout_s)
            sock.sendall(payload)
            raw = _recv_until(sock, b"\r\n", limit=65536)
        return parse_ultimate_result(raw)


class ZipherClient:
    def __init__(self, host: str, port: int, timeout_s: float):
        self.host = (host or "").strip()
        self.port = int(port or 0)
        self.timeout_s = float(timeout_s or 1.0)
        self._trx = 0
        self._lock = threading.Lock()

    def transact(self, message_id: int, body: bytes = b"") -> tuple[int, bytes]:
        if not self.host or self.port <= 0:
            raise RuntimeError("ZBC endpoint missing")

        with socket.create_connection((self.host, self.port), timeout=self.timeout_s) as sock:
            sock.settimeout(self.timeout_s)

            trx = self._next_trx()
            msg = build_zbc_message(message_id, body or b"")
            pkt = build_zbc_packet(flags=0x03, transaction_id=trx, sequence_id=0, payload=msg, force_checksum=True)
            sock.sendall(pkt)

            first = self._read_packet(sock)
            if first.flags & ZBC_FLAG_NAK:
                raise RuntimeError("ZBC transport NAK")

            response = first
            if (first.flags & ZBC_FLAG_ACK) and not first.payload:
                response = self._read_packet(sock)
                if response.flags & ZBC_FLAG_NAK:
                    raise RuntimeError("ZBC payload NAK")

            if response.payload:
                ack = build_zbc_ack(response.flags, response.transaction_id, response.sequence_id)
                sock.sendall(ack)

            return parse_zbc_message(response.payload)

    def _next_trx(self) -> int:
        with self._lock:
            self._trx = (self._trx + 1) & 0xFFFF
            return self._trx

    def _read_packet(self, sock: socket.socket):
        start = _recv_exact(sock, 1)
        while start and start[0] != ZBC_START:
            start = _recv_exact(sock, 1)

        hdr_rest = _recv_exact(sock, 9)
        size = int.from_bytes(hdr_rest[1:3], "little", signed=False)
        remaining = size - 10
        if remaining < 0:
            raise RuntimeError("ZBC packet size invalid")
        tail = _recv_exact(sock, remaining) if remaining else b""
        full = start + hdr_rest + tail
        return parse_zbc_packet(full)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    out = bytearray()
    while len(out) < n:
        chunk = sock.recv(n - len(out))
        if not chunk:
            raise RuntimeError("socket closed during read")
        out.extend(chunk)
    return bytes(out)


def _recv_until(sock: socket.socket, marker: bytes, limit: int = 65536) -> bytes:
    buf = bytearray()
    while True:
        chunk = sock.recv(1024)
        if not chunk:
            break
        buf.extend(chunk)
        if marker in buf:
            break
        if len(buf) >= limit:
            raise RuntimeError("response too large")
    return bytes(buf)


def _recv_line(sock: socket.socket, limit: int = 8192) -> str:
    data = _recv_until(sock, b"\n", limit=limit)
    first_line = data.split(b"\n", 1)[0]
    return first_line.decode("utf-8", errors="replace").strip()


def _tune_short_lived_socket(sock: socket.socket) -> None:
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception:
        pass
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except Exception:
        pass

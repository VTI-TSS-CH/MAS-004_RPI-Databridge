from __future__ import annotations

from dataclasses import dataclass, field
import queue
import threading
import time
from typing import Any


@dataclass
class Vj6530SessionRequest:
    operation: str
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: Exception | None = None

    def set_result(self, result: Any):
        self.result = result
        self.done.set()

    def set_error(self, error: Exception):
        self.error = error
        self.done.set()


class Vj6530RuntimeState:
    def __init__(self):
        self._lock = threading.Lock()
        self._last_async_ok_ts = 0.0
        self._last_async_event_ts = 0.0
        self._last_async_error = ""
        self._session_active = False
        self._request_queue: queue.Queue[Vj6530SessionRequest] = queue.Queue()

    def mark_async_ok(self):
        now = time.monotonic()
        with self._lock:
            self._last_async_ok_ts = now
            self._last_async_error = ""

    def mark_async_event(self):
        now = time.monotonic()
        with self._lock:
            self._last_async_ok_ts = now
            self._last_async_event_ts = now
            self._last_async_error = ""

    def mark_async_error(self, detail: str):
        with self._lock:
            self._last_async_error = str(detail or "")

    def mark_session_active(self, active: bool):
        with self._lock:
            self._session_active = bool(active)

    def session_active(self) -> bool:
        with self._lock:
            return bool(self._session_active)

    def async_recent(self, max_age_s: float) -> bool:
        with self._lock:
            last_ok = self._last_async_ok_ts
        return (time.monotonic() - last_ok) <= max(0.0, float(max_age_s or 0.0))

    def async_event_recent(self, max_age_s: float) -> bool:
        with self._lock:
            last_event = self._last_async_event_ts
        if last_event <= 0.0:
            return False
        return (time.monotonic() - last_event) <= max(0.0, float(max_age_s or 0.0))

    def submit_session_request(self, operation: str, *args, timeout_s: float = 10.0, **kwargs):
        if not self.session_active():
            raise RuntimeError("vj6530 async session unavailable")
        request = Vj6530SessionRequest(operation=operation, args=tuple(args), kwargs=dict(kwargs))
        self._request_queue.put(request)
        if not request.done.wait(max(0.1, float(timeout_s or 0.0))):
            raise TimeoutError(f"vj6530 async request timed out: {operation}")
        if request.error is not None:
            raise request.error
        return request.result

    def next_session_request(self, timeout_s: float = 0.0) -> Vj6530SessionRequest | None:
        try:
            if timeout_s and timeout_s > 0.0:
                return self._request_queue.get(timeout=float(timeout_s))
            return self._request_queue.get_nowait()
        except queue.Empty:
            return None

    def snapshot(self) -> dict[str, float | str]:
        with self._lock:
            return {
                "last_async_ok_ts": self._last_async_ok_ts,
                "last_async_event_ts": self._last_async_event_ts,
                "last_async_error": self._last_async_error,
                "session_active": self._session_active,
            }


RUNTIME = Vj6530RuntimeState()

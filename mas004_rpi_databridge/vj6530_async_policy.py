from __future__ import annotations


VJ6530_ASYNC_SESSION_S = 0.0
VJ6530_ASYNC_RECONNECT_MIN_S = 0.2


def vj6530_async_reconnect_delay_s(exc: Exception, current_backoff_s: float) -> float:
    detail = repr(exc).lower()
    if any(token in detail for token in ("socket closed", "connection reset", "broken pipe")):
        return VJ6530_ASYNC_RECONNECT_MIN_S
    return max(VJ6530_ASYNC_RECONNECT_MIN_S, float(current_backoff_s or VJ6530_ASYNC_RECONNECT_MIN_S))

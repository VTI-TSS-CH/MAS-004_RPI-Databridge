import os
import subprocess
import time as time_mod
from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

_TZ_CACHE_NAME: Optional[str] = None
_TZ_CACHE_OBJ = timezone.utc
_TZ_CACHE_UNTIL = 0.0
_TZ_CACHE_TTL_S = 5.0


def _detect_system_timezone_name() -> str:
    try:
        if os.path.exists("/etc/timezone"):
            with open("/etc/timezone", "r", encoding="utf-8") as f:
                name = (f.read() or "").strip()
            if name:
                return name
    except Exception:
        pass

    try:
        proc = subprocess.run(
            ["timedatectl", "show", "-p", "Timezone", "--value"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        name = (proc.stdout or "").strip()
        if name:
            return name
    except Exception:
        pass

    return "UTC"


def system_timezone() -> timezone:
    global _TZ_CACHE_NAME, _TZ_CACHE_OBJ, _TZ_CACHE_UNTIL
    now = time_mod.monotonic()
    if now < _TZ_CACHE_UNTIL and _TZ_CACHE_NAME:
        return _TZ_CACHE_OBJ

    name = _detect_system_timezone_name()
    try:
        tz = ZoneInfo(name)
    except Exception:
        tz = datetime.now().astimezone().tzinfo or timezone.utc

    _TZ_CACHE_NAME = name
    _TZ_CACHE_OBJ = tz
    _TZ_CACHE_UNTIL = now + _TZ_CACHE_TTL_S
    return tz


def system_timezone_name() -> str:
    if _TZ_CACHE_NAME and time_mod.monotonic() < _TZ_CACHE_UNTIL:
        return _TZ_CACHE_NAME
    _ = system_timezone()
    return _TZ_CACHE_NAME or "UTC"


def local_now() -> datetime:
    return datetime.now(system_timezone())


def local_from_timestamp(ts: float) -> datetime:
    return datetime.fromtimestamp(float(ts), tz=system_timezone())


def local_date(ts: Optional[float] = None) -> date:
    if ts is None:
        return local_now().date()
    return local_from_timestamp(ts).date()


def format_local_timestamp(ts: float, include_ms: bool = True) -> str:
    dt = local_from_timestamp(ts)
    if include_ms:
        return f"{dt:%Y-%m-%d %H:%M:%S}.{int(dt.microsecond/1000):03d}"
    return f"{dt:%Y-%m-%d %H:%M:%S}"

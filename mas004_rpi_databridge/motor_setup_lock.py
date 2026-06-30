from __future__ import annotations

import time
from typing import Any, Optional

from mas004_rpi_databridge.db import DB


DEFAULT_MOTOR_SETUP_LOCK_TTL_S = 30.0 * 60.0


def _ensure_table(db: DB) -> None:
    with db._conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS motor_setup_manual_lock (
                 singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                 active_until_ts REAL NOT NULL,
                 motor_id INTEGER,
                 reason TEXT NOT NULL DEFAULT '',
                 source TEXT NOT NULL DEFAULT '',
                 updated_ts REAL NOT NULL
               )"""
        )


def touch_motor_setup_manual_lock(
    db: DB,
    *,
    motor_id: Optional[int] = None,
    reason: str = "motor_setup",
    source: str = "webui",
    ttl_s: float = DEFAULT_MOTOR_SETUP_LOCK_TTL_S,
) -> dict[str, Any]:
    """Reserve Motor-Setup authority for manual commissioning work.

    While this lock is active, automatic runtime stop-position moves must not
    reposition ID5/6/7/8/9 behind the operator's back.
    """

    _ensure_table(db)
    now = time.time()
    until = now + max(1.0, float(ttl_s))
    with db._conn() as c:
        c.execute(
            """INSERT INTO motor_setup_manual_lock(
                 singleton_id,active_until_ts,motor_id,reason,source,updated_ts
               ) VALUES(1,?,?,?,?,?)
               ON CONFLICT(singleton_id) DO UPDATE SET
                 active_until_ts=excluded.active_until_ts,
                 motor_id=excluded.motor_id,
                 reason=excluded.reason,
                 source=excluded.source,
                 updated_ts=excluded.updated_ts""",
            (until, int(motor_id) if motor_id is not None else None, str(reason or ""), str(source or ""), now),
        )
    return {
        "active": True,
        "active_until_ts": until,
        "motor_id": int(motor_id) if motor_id is not None else None,
        "reason": str(reason or ""),
        "source": str(source or ""),
        "updated_ts": now,
    }


def motor_setup_manual_lock_status(db: DB, *, now: Optional[float] = None) -> dict[str, Any]:
    _ensure_table(db)
    ts = time.time() if now is None else float(now)
    with db._conn() as c:
        row = c.execute(
            """SELECT active_until_ts,motor_id,reason,source,updated_ts
               FROM motor_setup_manual_lock WHERE singleton_id=1"""
        ).fetchone()
    if not row:
        return {"active": False, "reason": "no_lock"}
    until = float(row[0] or 0.0)
    return {
        "active": until > ts,
        "active_until_ts": until,
        "remaining_s": max(0.0, until - ts),
        "motor_id": int(row[1]) if row[1] is not None else None,
        "reason": str(row[2] or ""),
        "source": str(row[3] or ""),
        "updated_ts": float(row[4] or 0.0),
    }


def motor_setup_manual_lock_active(db: DB, *, now: Optional[float] = None) -> bool:
    return bool(motor_setup_manual_lock_status(db, now=now).get("active"))


def clear_motor_setup_manual_lock(db: DB, *, reason: str = "machine_command") -> dict[str, Any]:
    _ensure_table(db)
    now = time.time()
    with db._conn() as c:
        c.execute(
            """INSERT INTO motor_setup_manual_lock(
                 singleton_id,active_until_ts,motor_id,reason,source,updated_ts
               ) VALUES(1,0,NULL,?,?,?)
               ON CONFLICT(singleton_id) DO UPDATE SET
                 active_until_ts=0,
                 motor_id=NULL,
                 reason=excluded.reason,
                 source=excluded.source,
                 updated_ts=excluded.updated_ts""",
            (str(reason or ""), "runtime", now),
        )
    return {"active": False, "reason": str(reason or ""), "updated_ts": now}

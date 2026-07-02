from __future__ import annotations

from decimal import Decimal, InvalidOperation

from mas004_rpi_databridge.db import DB, now_ts
from mas004_rpi_databridge.params import ParamStore


def _numeric_value(text: str) -> Decimal | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        return None


def values_effectively_equal(left: object, right: object) -> bool:
    left_text = str(left if left is not None else "").strip()
    right_text = str(right if right is not None else "").strip()
    if left_text == right_text:
        return True
    left_num = _numeric_value(left_text)
    right_num = _numeric_value(right_text)
    if left_num is not None and right_num is not None:
        return left_num == right_num
    return False


def stored_value_equals(params: ParamStore, pkey: str, value: object) -> bool:
    key = str(pkey or "").strip().upper()
    if not key:
        return False
    try:
        if not params.get_meta(key):
            return False
        return values_effectively_equal(params.get_effective_value(key), value)
    except Exception:
        return False


class ValueDedupeStore:
    def __init__(self, db: DB):
        self.db = db
        self._ensure_table()

    def _ensure_table(self) -> None:
        with self.db._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS value_dedupe_state(
                       channel TEXT NOT NULL,
                       pkey TEXT NOT NULL,
                       value TEXT NOT NULL,
                       updated_ts REAL NOT NULL,
                       PRIMARY KEY(channel, pkey)
                   )"""
            )

    def is_duplicate(self, channel: str, pkey: str, value: object) -> bool:
        key = str(pkey or "").strip().upper()
        chan = str(channel or "").strip().lower()
        if not key or not chan:
            return False
        text_value = str(value if value is not None else "").strip()
        with self.db._conn() as c:
            row = c.execute(
                "SELECT value FROM value_dedupe_state WHERE channel=? AND pkey=?",
                (chan, key),
            ).fetchone()
        return bool(row and values_effectively_equal(row[0], text_value))

    def remember(self, channel: str, pkey: str, value: object) -> None:
        key = str(pkey or "").strip().upper()
        chan = str(channel or "").strip().lower()
        if not key or not chan:
            return
        text_value = str(value if value is not None else "").strip()
        with self.db._conn() as c:
            c.execute(
                "INSERT INTO value_dedupe_state(channel,pkey,value,updated_ts) VALUES(?,?,?,?) "
                "ON CONFLICT(channel,pkey) DO UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts",
                (chan, key, text_value, now_ts()),
            )

    def should_send(self, channel: str, pkey: str, value: object) -> bool:
        if self.is_duplicate(channel, pkey, value):
            return False
        self.remember(channel, pkey, value)
        return True

from __future__ import annotations

import json
import re
from typing import Any

from mas004_rpi_databridge.db import DB, now_ts


def normalize_profile_name(name: str) -> str:
    value = (name or "").strip()
    value = re.sub(r"\s+", " ", value)
    if not value:
        raise ValueError("Formatname fehlt")
    if len(value) > 80:
        raise ValueError("Formatname ist zu lang (max. 80 Zeichen)")
    return value


class FormatProfileStore:
    def __init__(self, db: DB):
        self.db = db

    def list_profiles(self) -> list[dict[str, Any]]:
        with self.db._conn() as c:
            rows = c.execute(
                """SELECT name,note,values_json,created_ts,updated_ts
                   FROM format_profiles
                   ORDER BY updated_ts DESC, name ASC"""
            ).fetchall()
        out: list[dict[str, Any]] = []
        for name, note, values_json, created_ts, updated_ts in rows:
            values = self._decode_values(values_json)
            out.append(
                {
                    "name": name,
                    "note": note or "",
                    "param_count": len(values),
                    "created_ts": float(created_ts or 0.0),
                    "updated_ts": float(updated_ts or 0.0),
                }
            )
        return out

    def get_profile(self, name: str) -> dict[str, Any] | None:
        normalized = normalize_profile_name(name)
        with self.db._conn() as c:
            row = c.execute(
                """SELECT name,note,values_json,created_ts,updated_ts
                   FROM format_profiles
                   WHERE name=?""",
                (normalized,),
            ).fetchone()
        if not row:
            return None
        values = self._decode_values(row[2])
        return {
            "name": row[0],
            "note": row[1] or "",
            "values": values,
            "param_count": len(values),
            "created_ts": float(row[3] or 0.0),
            "updated_ts": float(row[4] or 0.0),
        }

    def save_profile(self, name: str, values: dict[str, Any], note: str = "") -> dict[str, Any]:
        normalized = normalize_profile_name(name)
        clean_values = self._clean_values(values)
        ts = now_ts()
        encoded = json.dumps(clean_values, ensure_ascii=False, sort_keys=True)
        with self.db._conn() as c:
            exists = c.execute("SELECT created_ts FROM format_profiles WHERE name=?", (normalized,)).fetchone()
            created_ts = float(exists[0]) if exists else ts
            c.execute(
                """INSERT INTO format_profiles(name,note,values_json,created_ts,updated_ts)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET
                     note=excluded.note,
                     values_json=excluded.values_json,
                     updated_ts=excluded.updated_ts""",
                (normalized, str(note or "").strip(), encoded, created_ts, ts),
            )
        return self.get_profile(normalized) or {
            "name": normalized,
            "note": note or "",
            "values": clean_values,
            "param_count": len(clean_values),
            "created_ts": created_ts,
            "updated_ts": ts,
        }

    def delete_profile(self, name: str) -> dict[str, Any]:
        normalized = normalize_profile_name(name)
        with self.db._conn() as c:
            cur = c.execute("DELETE FROM format_profiles WHERE name=?", (normalized,))
            deleted = int(cur.rowcount or 0)
        return {"ok": True, "deleted": deleted, "name": normalized}

    @staticmethod
    def _decode_values(values_json: str) -> dict[str, str]:
        try:
            data = json.loads(values_json or "{}")
        except Exception:
            return {}
        return FormatProfileStore._clean_values(data)

    @staticmethod
    def _clean_values(values: dict[str, Any]) -> dict[str, str]:
        out: dict[str, str] = {}
        if not isinstance(values, dict):
            return out
        for key, value in values.items():
            pkey = str(key or "").strip().upper()
            if not re.match(r"^[A-Z]{3}[0-9A-Z_]+$", pkey):
                continue
            out[pkey] = "" if value is None else str(value).strip()
        return dict(sorted(out.items()))

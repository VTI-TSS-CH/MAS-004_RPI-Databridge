from __future__ import annotations

import re
from typing import Any


_MOTOR_ID_RE = re.compile(r"(?:motor-id|id)\s*:\s*(\d+)", re.IGNORECASE)


def _parse_motor_id(text: str | None) -> int | None:
    raw = (text or "").strip()
    if not raw:
        return None
    m = _MOTOR_ID_RE.search(raw)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _classify_binding_role(row: dict[str, Any]) -> str | None:
    ptype = str(row.get("ptype") or "").strip().upper()
    ai = str(row.get("ai_instructions") or "").strip().lower()
    if ptype == "MAP" or "soll-position" in ai:
        return "setpoint"
    if ptype == "MAS" or "ist-position" in ai:
        return "actual"
    if ptype == "MAE" or "sammelstörung" in ai or "sammelstoerung" in ai:
        return "fault"
    return None


def build_motor_bindings(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[int, dict[str, Any]] = {}
    for row in rows:
        motor_id = _parse_motor_id(row.get("ai_instructions"))
        if motor_id is None:
            continue

        entry = grouped.setdefault(
            motor_id,
            {
                "motor_id": motor_id,
                "setpoint": None,
                "actual": None,
                "fault": None,
                "related_params": [],
            },
        )
        role = _classify_binding_role(row)
        compact = {
            "pkey": row.get("pkey"),
            "ptype": row.get("ptype"),
            "pid": row.get("pid"),
            "name": row.get("name"),
            "unit": row.get("unit"),
            "rw": row.get("rw"),
            "esp_rw": row.get("esp_rw"),
            "dtype": row.get("dtype"),
            "message": row.get("message"),
            "ai_instructions": row.get("ai_instructions"),
        }
        entry["related_params"].append(compact)
        if role:
            entry[role] = compact

    return [grouped[mid] for mid in sorted(grouped)]

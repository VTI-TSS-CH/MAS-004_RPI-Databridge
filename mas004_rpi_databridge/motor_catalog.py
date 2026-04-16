from __future__ import annotations

from copy import deepcopy
from typing import Any


_CATALOG: list[dict[str, Any]] = [
    {
        "id": 1,
        "name": "Motor X-Achse Tisch",
        "controller": "AZD-CD",
        "motor_type": "AZM911A0C",
        "positional": True,
        "description": "Horizontale Verstellung (X) der Inocon Drucktisch-Verstellachse.",
    },
    {
        "id": 2,
        "name": "Motor Z-Achse Tisch",
        "controller": "AZD-CD",
        "motor_type": "AZM98MC-HP5",
        "positional": True,
        "description": "Vertikale Verstellung (Z) der Inocon Drucktisch-Verstellachse.",
    },
    {
        "id": 3,
        "name": "Motor Etikettenantrieb",
        "controller": "AZD-CD",
        "motor_type": "AZM66M0CH",
        "positional": False,
        "description": "Vor- und Rueckwaertstransport des Etikettenbands.",
    },
    {
        "id": 4,
        "name": "Motor Schutzblech Laser",
        "controller": "AZD-KD",
        "motor_type": "AZM26AK",
        "positional": True,
        "description": "Anpassung des Schutzblechs fuer den Laserschutz.",
    },
    {
        "id": 5,
        "name": "Motor Kamera Materialkontrolle TV1",
        "controller": "AZD-KD",
        "motor_type": "AZM26AK",
        "positional": True,
        "description": "Verstellmotor fuer die Materialkontrollkamera TV1.",
    },
    {
        "id": 6,
        "name": "Motor Sensor Etikettenanwesenheit",
        "controller": "AZD-KD",
        "motor_type": "AZM26AK",
        "positional": True,
        "description": "Verstellmotor fuer den Sensor der Etikettenerfassung.",
    },
    {
        "id": 7,
        "name": "Motor Sensor Auswurfkontrolle",
        "controller": "AZD-KD",
        "motor_type": "AZM26AK",
        "positional": True,
        "description": "Verstellmotor fuer den Sensor der Auswurfkontrolle.",
    },
    {
        "id": 8,
        "name": "Motor Etikettenanschlag links",
        "controller": "AZD-KD",
        "motor_type": "AZM26AK",
        "positional": True,
        "description": "Linker Etiketten-Fuehrungsanschlag am Bandauslauf.",
    },
    {
        "id": 9,
        "name": "Motor Etikettenanschlag vorne",
        "controller": "AZD-KD",
        "motor_type": "AZM26AK",
        "positional": True,
        "description": "Vorderer bzw. rechter Etiketten-Fuehrungsanschlag am Bandeinlauf.",
    },
]


def _default_config() -> dict[str, Any]:
    return {
        "steps_per_mm": 100,
        "speed_mm_s": 25,
        "accel_mm_s2": 100,
        "decel_mm_s2": 100,
        "current_pct": 100,
        "invert_direction": False,
        "min_tenths_mm": 0,
        "max_tenths_mm": 0,
        "min_enabled": False,
        "max_enabled": False,
    }


def _default_state(message: str) -> dict[str, Any]:
    return {
        "link_ok": False,
        "ready": False,
        "move": False,
        "in_pos": False,
        "alarm": False,
        "alarm_code": "",
        "feedback_tenths_mm": "",
        "command_tenths_mm": "",
        "input_raw_hex": "",
        "output_raw_hex": "",
        "last_error": message,
    }


def motor_catalog() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for base in _CATALOG:
        item = deepcopy(base)
        item["config"] = _default_config()
        item["state"] = _default_state("")
        item["bindings"] = None
        item["last_reply"] = ""
        item["simulation"] = False
        items.append(item)
    return items


def _overlay(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    for key, value in extra.items():
        if key in ("config", "state"):
            continue
        base[key] = value
    if isinstance(extra.get("config"), dict):
        merged_cfg = dict(base.get("config") or {})
        merged_cfg.update(extra.get("config") or {})
        base["config"] = merged_cfg
    if isinstance(extra.get("state"), dict):
        merged_state = dict(base.get("state") or {})
        merged_state.update(extra.get("state") or {})
        base["state"] = merged_state
    return base


def merge_motor_payload(
    payload: dict[str, Any] | None,
    bindings: dict[int, dict[str, Any]],
    *,
    simulated_ids: set[int] | None = None,
    cached_motors: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    motors = {int(item["id"]): item for item in motor_catalog()}
    simulated_ids = {int(mid) for mid in (simulated_ids or set())}
    cached_motors = cached_motors or {}
    live_payload = payload or {}
    live_items = live_payload.get("motors") or []
    live_error = str(live_payload.get("error") or "").strip()
    live_available = bool(live_payload.get("live_available")) or bool(live_items)

    for mid, cached in cached_motors.items():
        if not isinstance(cached, dict):
            continue
        base = motors.setdefault(
            int(mid),
            {
                "id": int(mid),
                "name": f"Motor {mid}",
                "controller": "",
                "motor_type": "",
                "positional": True,
                "description": "",
                "config": _default_config(),
                "state": _default_state(""),
                "bindings": None,
                "last_reply": "",
                "simulation": False,
            },
        )
        _overlay(base, cached)

    for live in live_items:
        try:
            mid = int(live.get("id") or 0)
        except Exception:
            continue
        if mid <= 0:
            continue
        base = motors.setdefault(
            mid,
            {
                "id": mid,
                "name": f"Motor {mid}",
                "controller": "",
                "motor_type": "",
                "positional": True,
                "description": "",
                "config": _default_config(),
                "state": _default_state(""),
                "bindings": None,
                "last_reply": "",
                "simulation": False,
            },
        )
        _overlay(base, live)

    for mid, binding in bindings.items():
        base = motors.setdefault(
            int(mid),
            {
                "id": int(mid),
                "name": f"Motor {mid}",
                "controller": "",
                "motor_type": "",
                "positional": True,
                "description": "",
                "config": _default_config(),
                "state": _default_state(""),
                "bindings": None,
                "last_reply": "",
                "simulation": False,
            },
        )
        base["bindings"] = binding

    fallback_msg = live_error or "ESP-Motor-Endpoint nicht erreichbar oder Simulation aktiv"
    for mid, item in motors.items():
        item["simulation"] = mid in simulated_ids
        item.setdefault("state", {})
        if item["simulation"]:
            item["state"]["last_error"] = "Simulation aktiv - letzte bekannte Werte"
            item["state"]["link_ok"] = "SIM"
            if not item.get("last_reply"):
                item["last_reply"] = "Simulation aktiv - letzte bekannte Werte"
            continue
        if not live_available and not item["state"].get("last_error"):
            item["state"]["last_error"] = fallback_msg
        if not live_available and not item.get("last_reply"):
            item["last_reply"] = fallback_msg

    return {
        "ok": True,
        "live_available": live_available,
        "message": "",
        "motors": [motors[mid] for mid in sorted(motors)],
    }

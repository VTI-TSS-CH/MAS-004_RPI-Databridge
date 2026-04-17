from __future__ import annotations

import math
from typing import Any


STATE_LABELS: dict[int, str] = {
    0: "Anlage ausgeschaltet",
    1: "Offline / Startup",
    2: "Wechsle zu Einrichtbetrieb",
    3: "Einrichtbetrieb",
    4: "Wechsle zu Produktionsbetrieb",
    5: "Produktionsbetrieb",
    6: "Wechsle zu Pause",
    7: "Pause",
    8: "Wechsle zu Produktions-Stop",
    9: "Produktions-Stop",
    10: "Wechsle zu Rueckspulung",
    11: "Rueckspulbetrieb",
    12: "Wechsle zu Etikettenentnahme",
    13: "Etikettenentnahme",
    14: "Wechsle zu Spleissen",
    15: "Spleissen",
    16: "Wechsle zu Synchronisieren",
    17: "Synchronisieren",
    18: "Wechsle zu Produktion abgeschlossen",
    19: "Produktion abgeschlossen",
    20: "Abschaltbetrieb",
    21: "Not-Stop / Stoerung",
}

TRANSITION_FINALS: dict[int, int] = {
    2: 3,
    4: 5,
    6: 7,
    8: 9,
    10: 11,
    12: 13,
    14: 15,
    16: 17,
    18: 19,
}

FINAL_TO_TRANSITION: dict[int, int] = {final_state: transition for transition, final_state in TRANSITION_FINALS.items()}

BUTTON_ORDER = [
    "start",
    "pause",
    "stop",
    "setup",
    "sync",
    "empty",
    "rewind",
]

COMMAND_TARGETS: dict[int, int] = {
    0: 1,
    1: 5,
    2: 9,
    3: 3,
    4: 17,
    5: 13,
    6: 11,
    7: 7,
}

BUTTON_INPUTS = {
    "start_pause": ("raspi_plc21", "I0.7"),
    "stop": ("raspi_plc21", "I0.8"),
    "setup": ("raspi_plc21", "I0.9"),
    "sync": ("raspi_plc21", "I0.10"),
    "empty": ("raspi_plc21", "I0.11"),
    "rewind": ("raspi_plc21", "I0.12"),
}

BUTTON_LED_OUTPUTS = {
    "start": [("raspi_plc21", "Q0.1")],
    "pause": [("raspi_plc21", "Q0.0"), ("raspi_plc21", "Q0.2")],
    "stop": [("raspi_plc21", "Q0.3")],
    "setup": [("raspi_plc21", "Q0.4")],
    "sync": [("raspi_plc21", "Q0.5")],
    "empty": [("raspi_plc21", "Q0.6")],
    "rewind": [("raspi_plc21", "Q0.7")],
}

STATUS_LAMP_OUTPUTS = {
    "red": ("moxa_e1211_2", "DO4"),
    "green": ("moxa_e1211_2", "DO5"),
    "blue": ("moxa_e1211_2", "DO6"),
}

PHASE_STEADY = "steady"
PHASE_BLINK = "blink"
PHASE_OFF = "off"

STATE_COLOR_MAP: dict[int, tuple[tuple[int, int, int], str]] = {
    0: ((0, 0, 0), PHASE_OFF),
    1: ((1, 1, 1), PHASE_STEADY),
    2: ((0, 1, 1), PHASE_BLINK),
    3: ((0, 1, 1), PHASE_STEADY),
    4: ((0, 1, 0), PHASE_BLINK),
    5: ((0, 1, 0), PHASE_STEADY),
    6: ((1, 1, 0), PHASE_BLINK),
    7: ((1, 1, 0), PHASE_STEADY),
    8: ((0, 0, 1), PHASE_BLINK),
    9: ((0, 0, 1), PHASE_STEADY),
    10: ((1, 0, 1), PHASE_BLINK),
    11: ((1, 0, 1), PHASE_STEADY),
    12: ((0, 0, 1), PHASE_BLINK),
    13: ((0, 0, 1), PHASE_STEADY),
    14: ((0, 1, 1), PHASE_BLINK),
    15: ((0, 1, 1), PHASE_STEADY),
    16: ((1, 1, 0), PHASE_BLINK),
    17: ((1, 1, 0), PHASE_STEADY),
    18: ((0, 0, 1), PHASE_BLINK),
    19: ((0, 0, 1), PHASE_STEADY),
    20: ((1, 0, 0), PHASE_BLINK),
    21: ((1, 0, 0), PHASE_STEADY),
}


def state_label(state: int | str | None) -> str:
    try:
        code = int(state or 0)
    except Exception:
        code = 0
    return STATE_LABELS.get(code, f"Status {code}")


def parse_button_mask(raw: Any) -> dict[str, bool]:
    text = str(raw or "").strip()
    if not text:
        text = "1111111"
    digits = "".join(ch for ch in text if ch in "01")
    if len(digits) < len(BUTTON_ORDER):
        digits = digits.ljust(len(BUTTON_ORDER), "1")
    elif len(digits) > len(BUTTON_ORDER):
        digits = digits[: len(BUTTON_ORDER)]
    return {name: digits[idx] == "1" for idx, name in enumerate(BUTTON_ORDER)}


def state_actions(state: int | str | None) -> dict[str, bool]:
    try:
        code = int(state or 0)
    except Exception:
        code = 0
    actions = {name: False for name in BUTTON_ORDER}
    if code in (3, 7, 9, 19):
        actions["start"] = True
    if code == 5:
        actions["pause"] = True
        actions["stop"] = True
        actions["empty"] = True
    if code in (3, 5, 7, 9, 11, 13, 15, 17, 19):
        actions["setup"] = True
    if code in (3, 7, 9, 11, 13, 15, 19):
        actions["sync"] = True
    if code in (9, 11, 13, 19):
        actions["empty"] = True
    if code in (7, 9, 11, 13, 19):
        actions["rewind"] = True
    if code in (20, 21):
        return {name: False for name in BUTTON_ORDER}
    return actions


def target_state_for_button(button: str, current_state: int | str | None) -> int | None:
    try:
        code = int(current_state or 0)
    except Exception:
        code = 0
    btn = str(button or "").strip().lower()
    if btn == "start_pause":
        if code == 5:
            return 7
        if code in (3, 7, 9, 19):
            return 5
        return None
    if btn == "stop":
        return 9 if code not in (20, 21) else None
    if btn == "setup":
        return 3
    if btn == "sync":
        return 17
    if btn == "empty":
        return 13
    if btn == "rewind":
        return 11
    return None


def command_to_target_state(command: int | str | None, current_state: int | str | None) -> int:
    try:
        cmd = int(command or 0)
    except Exception:
        cmd = 0
    try:
        current = int(current_state or 1)
    except Exception:
        current = 1
    if cmd == 0:
        return current
    return COMMAND_TARGETS.get(cmd, current)


def button_to_command(button: str, current_state: int | str | None) -> int | None:
    try:
        code = int(current_state or 0)
    except Exception:
        code = 0
    btn = str(button or "").strip().lower()
    if btn == "start_pause":
        return 7 if code == 5 else 1
    if btn == "stop":
        return 2
    if btn == "setup":
        return 3
    if btn == "sync":
        return 4
    if btn == "empty":
        return 5
    if btn == "rewind":
        return 6
    return None


def settle_machine_state(
    requested_state: int | str | None,
    current_state: int | str | None,
    *,
    estop_ok: bool,
    light_curtain_ok: bool,
    ups_ok: bool,
    purge_active: bool,
) -> tuple[int, str]:
    try:
        requested = int(requested_state or current_state or 1)
    except Exception:
        requested = 1
    try:
        current = int(current_state or requested or 1)
    except Exception:
        current = 1

    if not ups_ok:
        return 20, "ups_shutdown"
    if not estop_ok or purge_active:
        return 21, "safety_or_purge"
    if not light_curtain_ok and current in (4, 5, 6):
        return 7, "light_curtain_pause"
    if requested in FINAL_TO_TRANSITION and current not in (requested, FINAL_TO_TRANSITION[requested]):
        return FINAL_TO_TRANSITION[requested], "transition_enter"
    if current in TRANSITION_FINALS:
        expected = TRANSITION_FINALS[current]
        if requested == expected:
            return expected, "transition_complete"
    if requested in STATE_LABELS:
        return requested, "requested"
    return current, "hold"


def blink_on(ts: float, period_s: float = 1.0) -> bool:
    if period_s <= 0:
        return True
    return int(math.floor(ts / max(0.1, period_s / 2.0))) % 2 == 0


def lamp_outputs_for_state(
    state: int | str | None,
    *,
    warning_active: bool,
    ts: float,
    blink_period_s: float = 1.0,
) -> dict[str, int]:
    try:
        code = int(state or 0)
    except Exception:
        code = 0
    base_rgb, phase = STATE_COLOR_MAP.get(code, ((1, 1, 1), PHASE_STEADY))
    active_rgb = base_rgb
    if warning_active:
        orange_rgb = (1, 1, 0)
        active_rgb = orange_rgb if blink_on(ts, blink_period_s) else base_rgb
        phase = PHASE_STEADY
    if phase == PHASE_OFF:
        active = (0, 0, 0)
    elif phase == PHASE_BLINK:
        active = base_rgb if blink_on(ts, blink_period_s) else (0, 0, 0)
    else:
        active = active_rgb
    return {"red": int(active[0]), "green": int(active[1]), "blue": int(active[2])}


def button_led_plan(
    current_state: int | str | None,
    button_mask: dict[str, bool],
    *,
    ts: float,
    blink_period_s: float = 1.0,
) -> dict[str, bool]:
    actions = state_actions(current_state)
    try:
        code = int(current_state or 0)
    except Exception:
        code = 0
    current_action = None
    if code == 5:
        current_action = "start"
    elif code == 7:
        current_action = "pause"
    elif code == 9:
        current_action = "stop"
    elif code == 3:
        current_action = "setup"
    elif code == 17:
        current_action = "sync"
    elif code == 13:
        current_action = "empty"
    elif code == 11:
        current_action = "rewind"

    plan = {pin: False for pins in BUTTON_LED_OUTPUTS.values() for _device, pin in pins}
    for action, pins in BUTTON_LED_OUTPUTS.items():
        allowed = bool(actions.get(action)) and bool(button_mask.get(action, False))
        enabled_now = False
        if current_action == action:
            enabled_now = True
        elif allowed and blink_on(ts, blink_period_s):
            enabled_now = True
        for _device, pin in pins:
            plan[pin] = enabled_now
    return plan


def pack_label_status_word(
    *,
    label_no: int,
    material_ok: bool,
    print_ok: bool,
    verify_ok: bool,
    removed: bool,
    production_ok: bool,
) -> int:
    word = int(label_no) & 0xFFFF
    if bool(material_ok):
        word |= (1 << 16)
    if bool(print_ok):
        word |= (1 << 17)
    if bool(verify_ok):
        word |= (1 << 18)
    if bool(removed):
        word |= (1 << 19)
    if bool(production_ok):
        word |= (1 << 20)
    return word

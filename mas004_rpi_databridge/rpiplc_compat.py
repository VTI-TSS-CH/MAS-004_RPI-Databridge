from __future__ import annotations

import ctypes
from ctypes.util import find_library
from typing import Dict


INPUT = 0
OUTPUT = 1
LOW = 0
HIGH = 1


_RPIPLC21_PINS: Dict[str, int] = {
    "I0.0": 0x00002105,
    "I0.1": 0x00002102,
    "I0.2": 0x00002104,
    "I0.3": 0x00002101,
    "I0.4": 0x00002103,
    "I0.5": 13,
    "I0.6": 12,
    "I0.7": 0x00000806,
    "I0.8": 0x00000805,
    "I0.9": 0x00000807,
    "I0.10": 0x00000A00,
    "I0.11": 0x00000A06,
    "I0.12": 0x00000A03,
    "Q0.0": 0x0000410C,
    "Q0.1": 0x00004000,
    "Q0.2": 0x0000410F,
    "Q0.3": 0x0000410E,
    "Q0.4": 0x00004105,
    "Q0.5": 0x00004104,
    "Q0.6": 0x00004102,
    "Q0.7": 0x00004100,
    "A0.5": 0x00004104,
    "A0.6": 0x00004102,
    "A0.7": 0x00004100,
}


_MODEL_PINS = {
    "RPIPLC_21": _RPIPLC21_PINS,
}

_lib = None
_pins: Dict[str, int] = {}
_model = ""


def _load_library():
    global _lib
    if _lib is not None:
        return _lib

    libname = find_library("rpiplc") or "librpiplc.so"
    lib = ctypes.cdll.LoadLibrary(libname)
    lib.initPins.argtypes = []
    lib.initPins.restype = ctypes.c_int
    lib.pinMode.argtypes = [ctypes.c_uint32, ctypes.c_uint8]
    lib.pinMode.restype = ctypes.c_int
    lib.digitalRead.argtypes = [ctypes.c_uint32]
    lib.digitalRead.restype = ctypes.c_int
    lib.digitalWrite.argtypes = [ctypes.c_uint32, ctypes.c_uint8]
    lib.digitalWrite.restype = ctypes.c_int
    lib.analogRead.argtypes = [ctypes.c_uint32]
    lib.analogRead.restype = ctypes.c_uint16
    lib.analogWrite.argtypes = [ctypes.c_uint32, ctypes.c_uint16]
    lib.analogWrite.restype = ctypes.c_int
    _lib = lib
    return _lib


def _pin(pin_name: str) -> int:
    key = str(pin_name).strip()
    try:
        return _pins[key]
    except KeyError as exc:
        raise ValueError(f"Unsupported Raspberry PLC pin '{pin_name}' for model '{_model or '?'}'") from exc


def init(model_name: str = "RPIPLC_21"):
    global _pins, _model
    model = str(model_name or "RPIPLC_21").strip() or "RPIPLC_21"
    if model not in _MODEL_PINS:
        raise ValueError(f"Unsupported Raspberry PLC model '{model}'")
    lib = _load_library()
    rc = int(lib.initPins())
    _pins = _MODEL_PINS[model]
    _model = model
    return rc


def pin_mode(pin_name: str, mode: int):
    return int(_load_library().pinMode(_pin(pin_name), int(mode)))


def digital_read(pin_name: str) -> int:
    return int(_load_library().digitalRead(_pin(pin_name)))


def digital_write(pin_name: str, value: int):
    return int(_load_library().digitalWrite(_pin(pin_name), int(value)))


def analog_read(pin_name: str) -> int:
    return int(_load_library().analogRead(_pin(pin_name)))


def analog_write(pin_name: str, value: int):
    return int(_load_library().analogWrite(_pin(pin_name), int(value)))

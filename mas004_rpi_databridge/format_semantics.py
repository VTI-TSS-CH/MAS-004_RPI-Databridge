from __future__ import annotations

from typing import Any


def _safe_int(values: dict[str, Any], key: str, default: int = 0) -> int:
    try:
        return int(float(str(values.get(key, default)).strip()))
    except Exception:
        return int(default)


def _safe_bool(values: dict[str, Any], key: str, default: bool = False) -> bool:
    raw = str(values.get(key, "1" if default else "0")).strip().lower()
    if raw in ("", "0", "false", "off", "no", "none", "null"):
        return False
    return True


def _mm_to_tenths(raw_mm: int) -> int:
    return int(raw_mm) * 10


def build_format_plan(values: dict[str, Any]) -> dict[str, Any]:
    """Derive the Raspi-side view of the currently loaded MAP format semantics.

    The ESP still owns the hard realtime path. This helper gives the Raspi/UI,
    logs and commissioning assistant the same deterministic interpretation of
    the master workbook relationships.
    """

    label_width_tenths = _safe_int(values, "MAP0001", 0)
    label_length_tenths = _safe_int(values, "MAP0002", 0)
    label_length_tolerance_tenths = _safe_int(values, "MAP0040", 0)

    print_x_tenths = _safe_int(values, "MAP0003", 0)
    print_y_tenths = _safe_int(values, "MAP0004", 0)
    print_x_correction_tenths = _safe_int(values, "MAP0005", 0)
    print_y_correction_tenths = _safe_int(values, "MAP0006", 0)

    active_printer = "laser" if _safe_bool(values, "MAP0016", False) else "tto"
    active_print_distance_key = "MAP0018" if active_printer == "laser" else "MAP0019"
    active_print_base_tenths = _safe_int(values, active_print_distance_key, 0)

    table_x_zero_tenths = _safe_int(values, "MAP0027", 0)
    table_z_zero_tenths = _safe_int(values, "MAP0028", 0)
    table_x_target_tenths = table_x_zero_tenths - (print_x_tenths + print_x_correction_tenths)
    table_z_target_tenths = table_z_zero_tenths + _safe_int(values, "MAP0007", 0)
    print_stop_tenths = active_print_base_tenths + print_y_tenths + print_y_correction_tenths

    sensor_detect_target_tenths = _mm_to_tenths(_safe_int(values, "MAP0008", 0)) + _safe_int(values, "MAP0029", 0)
    sensor_control_target_tenths = _mm_to_tenths(_safe_int(values, "MAP0009", 0)) + _safe_int(values, "MAP0030", 0)
    material_camera_target_tenths = _mm_to_tenths(_safe_int(values, "MAP0010", 0)) + _safe_int(values, "MAP0033", 0)

    return {
        "label": {
            "width_tenths_mm": label_width_tenths,
            "length_tenths_mm": label_length_tenths,
            "length_tolerance_tenths_mm": label_length_tolerance_tenths,
            "length_min_tenths_mm": label_length_tenths - label_length_tolerance_tenths,
            "length_max_tenths_mm": label_length_tenths + label_length_tolerance_tenths,
        },
        "printer": {
            "active": active_printer,
            "selection_param": "MAP0016",
            "distance_param": active_print_distance_key,
            "base_distance_tenths_mm": active_print_base_tenths,
            "x_position_tenths_mm": print_x_tenths,
            "x_correction_tenths_mm": print_x_correction_tenths,
            "y_position_tenths_mm": print_y_tenths,
            "y_correction_tenths_mm": print_y_correction_tenths,
            "stop_distance_tenths_mm": print_stop_tenths,
        },
        "table": {
            "x_zero_correction_tenths_mm": table_x_zero_tenths,
            "x_target_tenths_mm": table_x_target_tenths,
            "z_zero_correction_tenths_mm": table_z_zero_tenths,
            "z_target_tenths_mm": table_z_target_tenths,
        },
        "axes": {
            "label_detect_sensor_target_tenths_mm": sensor_detect_target_tenths,
            "label_control_sensor_target_tenths_mm": sensor_control_target_tenths,
            "material_camera_x_target_tenths_mm": material_camera_target_tenths,
            "label_guide_infeed_target_tenths_mm": label_width_tenths + _safe_int(values, "MAP0031", 0),
            "label_guide_outfeed_target_tenths_mm": label_width_tenths + _safe_int(values, "MAP0032", 0),
            "laser_guard_zero_correction_tenths_mm": _safe_int(values, "MAP0034", 0),
        },
        "process": {
            "transport_speed_mm_s": _safe_int(values, "MAP0014", 0),
            "rewind_speed_mm_s": _safe_int(values, "MAP0015", 0),
            "rewind_after_stop": _safe_bool(values, "MAP0039", False),
            "led_strip_first_led_distance_tenths_mm": _safe_int(values, "MAP0066", 8000),
            "roll_core_type": _safe_int(values, "MAP0013", 0),
            "roll_core_note": "76mm" if _safe_int(values, "MAP0013", 0) == 0 else "100mm",
        },
        "bypass": {
            "printer": _safe_bool(values, "MAP0035", False),
            "material_camera": _safe_bool(values, "MAP0036", False),
            "ocr_camera": _safe_bool(values, "MAP0037", False),
            "label_control_sensor": _safe_bool(values, "MAP0038", False),
        },
    }

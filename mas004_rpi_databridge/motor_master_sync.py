from __future__ import annotations

import os
import re
from typing import Any

import openpyxl

from mas004_rpi_databridge.config import Settings
from mas004_rpi_databridge.params import ParamStore, SHEET_NAME


DEFAULT_MOTOR_PARAM_BINDINGS: dict[int, dict[str, str]] = {
    1: {"setpoint": "MAP0056", "actual": "MAS0011"},
    2: {"setpoint": "MAP0057", "actual": "MAS0012"},
    3: {"setpoint": "MAP0058", "actual": "MAS0031"},
    4: {"setpoint": "MAP0059", "actual": "MAS0032"},
    5: {"setpoint": "MAP0060", "actual": "MAS0017"},
    6: {"setpoint": "MAP0061", "actual": "MAS0013"},
    7: {"setpoint": "MAP0062", "actual": "MAS0014"},
    8: {"setpoint": "MAP0063", "actual": "MAS0016"},
    9: {"setpoint": "MAP0064", "actual": "MAS0015"},
}


def _to_int_or_none(value: Any) -> int | None:
    try:
        return int(round(float(str(value).strip())))
    except Exception:
        return None


def _to_float_or_none(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except Exception:
        return None


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _norm_header(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", _cell_text(value).lower()).strip("_")


def _col_any(header_map: dict[str, int], *names: str) -> int | None:
    for name in names:
        idx = header_map.get(_norm_header(name))
        if idx is not None:
            return idx
    return None


def _row_pkey(ptype: Any, pid: Any) -> str:
    ptype_text = _cell_text(ptype).upper()
    pid_text = _cell_text(pid)
    if pid_text and pid_text.isdigit() and len(pid_text) < 4:
        pid_text = pid_text.zfill(4)
    return f"{ptype_text}{pid_text}"


def _format_default(value: int | float | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and not value.is_integer():
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return str(int(round(float(value))))


def _motor_binding(bindings: dict[int, dict[str, Any]] | dict[str, Any], motor_id: int) -> dict[str, Any]:
    if not isinstance(bindings, dict):
        return {}
    direct = bindings.get(int(motor_id))
    if isinstance(direct, dict):
        return direct
    as_text = bindings.get(str(int(motor_id)))
    return as_text if isinstance(as_text, dict) else {}


def _pkey_for_role(motor_id: int, binding: dict[str, Any], role: str) -> str | None:
    item = binding.get(role)
    if not isinstance(item, dict):
        return DEFAULT_MOTOR_PARAM_BINDINGS.get(int(motor_id), {}).get(role)
    pkey = _cell_text(item.get("pkey"))
    return pkey or DEFAULT_MOTOR_PARAM_BINDINGS.get(int(motor_id), {}).get(role)


def _unique_existing(paths: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        raw_text = str(raw or "").strip()
        if not raw_text:
            continue
        path = os.path.abspath(raw_text)
        if path in seen or not os.path.isfile(path):
            continue
        seen.add(path)
        out.append(path)
    return out


def _update_workbook(path: str, updates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    wb = openpyxl.load_workbook(path)
    ws = wb[SHEET_NAME] if SHEET_NAME in wb.sheetnames else wb.active
    headers = {_norm_header(ws.cell(1, col).value): col for col in range(1, ws.max_column + 1)}
    col_type = _col_any(headers, "Params_Type.", "Params_Type.:", "Params Type", "Params_Type")
    col_id = _col_any(headers, "Param ID.", "Param. ID.:", "Param ID", "Param_ID", "Param. ID")
    col_min = _col_any(headers, "Min.", "Min.:", "Min")
    col_max = _col_any(headers, "Max.", "Max.:", "Max")
    col_default = _col_any(headers, "Default Value", "Default Value:", "Default")
    if not col_type or not col_id:
        return {"path": path, "updated": 0, "error": "missing parameter id columns"}

    changed = 0
    for row in range(2, ws.max_row + 1):
        pkey = _row_pkey(ws.cell(row, col_type).value, ws.cell(row, col_id).value)
        update = updates.get(pkey)
        if not update:
            continue
        if col_min and update.get("min_v") is not None:
            ws.cell(row, col_min).value = update["min_v"]
            changed += 1
        if col_max and update.get("max_v") is not None:
            ws.cell(row, col_max).value = update["max_v"]
            changed += 1
        if col_default and update.get("default_v") is not None:
            ws.cell(row, col_default).value = update["default_v"]
            changed += 1

    if changed:
        wb.save(path)
    return {"path": path, "updated": changed}


def sync_motor_master_values(
    params: ParamStore,
    cfg: Settings,
    motor_id: int,
    motor: dict[str, Any],
    bindings: dict[int, dict[str, Any]] | dict[str, Any],
    *,
    workbook_paths: list[str] | None = None,
) -> dict[str, Any]:
    """
    Persist the live Motor-Setup values as the project master.

    The motor setup page is the authoritative commissioning source for ID1-9.
    A successful save therefore updates the runtime DB and the master Excel
    files so an older import cannot silently reintroduce stale soft limits.
    """

    motor_id = int(motor_id)
    binding = _motor_binding(bindings, motor_id)
    state = motor.get("state") if isinstance(motor.get("state"), dict) else {}
    config = motor.get("config") if isinstance(motor.get("config"), dict) else {}
    feedback = _to_int_or_none(state.get("feedback_tenths_mm"))
    target = _to_int_or_none(state.get("target_tenths_mm"))
    command = _to_int_or_none(state.get("command_tenths_mm"))
    default_tenths = target if target is not None else (command if command is not None else feedback)
    actual_tenths = feedback if feedback is not None else default_tenths

    min_enabled = bool(config.get("min_enabled", motor_id != 3))
    max_enabled = bool(config.get("max_enabled", motor_id != 3))
    min_v = float(_to_int_or_none(config.get("min_tenths_mm"))) if min_enabled and _to_int_or_none(config.get("min_tenths_mm")) is not None else None
    max_v = float(_to_int_or_none(config.get("max_tenths_mm"))) if max_enabled and _to_int_or_none(config.get("max_tenths_mm")) is not None else None

    # Motor 3 is the transport axis. Its production speed/ramp remains recipe-
    # driven, but its actual position can still be mirrored as a status value.
    if motor_id == 3:
        min_v = None
        max_v = None

    updates: dict[str, dict[str, Any]] = {}
    setpoint_pkey = _pkey_for_role(motor_id, binding, "setpoint")
    actual_pkey = _pkey_for_role(motor_id, binding, "actual")
    if setpoint_pkey:
        updates[setpoint_pkey] = {
            "default_v": _format_default(default_tenths),
            "min_v": min_v,
            "max_v": max_v,
        }
    if actual_pkey:
        updates[actual_pkey] = {
            "default_v": _format_default(actual_tenths),
            "min_v": min_v,
            "max_v": max_v,
        }

    db_updates: dict[str, str] = {}
    db_errors: dict[str, str] = {}
    for pkey, update in updates.items():
        ok, msg = params.update_meta(
            pkey,
            default_v=update.get("default_v"),
            min_v=update.get("min_v"),
            max_v=update.get("max_v"),
        )
        if not ok:
            db_errors[pkey] = msg
            continue
        if update.get("default_v") is not None:
            params.apply_device_value(pkey, str(update["default_v"]), promote_default=True)
        db_updates[pkey] = "OK"

    paths = list(workbook_paths or [])
    if cfg.master_params_xlsx_path:
        paths.append(cfg.master_params_xlsx_path)
    workbook_results = [_update_workbook(path, updates) for path in _unique_existing(paths)]
    return {
        "ok": not db_errors,
        "motor_id": motor_id,
        "updated_pkeys": sorted(db_updates.keys()),
        "db_errors": db_errors,
        "workbooks": workbook_results,
    }

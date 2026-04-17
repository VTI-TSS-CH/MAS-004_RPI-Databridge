from __future__ import annotations

import json
import re
from typing import Any, Optional

from mas004_rpi_databridge.db import DB, now_ts
from mas004_rpi_databridge.io_master import IoStore
from mas004_rpi_databridge.io_runtime import IoRuntime
from mas004_rpi_databridge.logstore import LogStore
from mas004_rpi_databridge.machine_semantics import (
    BUTTON_INPUTS,
    BUTTON_LED_OUTPUTS,
    STATUS_LAMP_OUTPUTS,
    button_led_plan,
    button_to_command,
    command_to_target_state,
    lamp_outputs_for_state,
    pack_label_status_word,
    parse_button_mask,
    settle_machine_state,
    state_actions,
    state_label,
    target_state_for_button,
)
from mas004_rpi_databridge.outbox import Outbox
from mas004_rpi_databridge.params import ParamStore
from mas004_rpi_databridge.peers import peer_urls
from mas004_rpi_databridge.production_logs import ProductionLogManager, sanitize_production_label


def _truthy(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw or "").strip().lower()
    if text in ("", "0", "false", "off", "no", "none", "null"):
        return False
    return True


def _safe_int(raw: Any, default: int = 0) -> int:
    try:
        return int(float(str(raw or "").strip()))
    except Exception:
        return int(default)


class MachineRuntime:
    def __init__(
        self,
        cfg: Any,
        db: DB,
        params: ParamStore,
        io_store: IoStore,
        logs: LogStore,
        outbox: Outbox | None = None,
    ):
        self.cfg = cfg
        self.db = db
        self.params = params
        self.io_store = io_store
        self.logs = logs
        self.outbox = outbox
        self.production_logs = ProductionLogManager(db, cfg=cfg, outbox=outbox)

    def refresh(self) -> dict[str, Any]:
        snapshot = self._state_row()
        info = dict(snapshot.get("info") or {})
        io_map = self._io_values()
        param_map = self._param_values_by_prefix(("MAP", "MAS", "MAE", "MAW"))
        ts = now_ts()

        button_mask = parse_button_mask(param_map.get("MAP0065", "1111111"))
        warning_active = self._warning_active(param_map)
        critical_active, critical_reasons = self._critical_state(io_map, param_map)

        requested_command = _safe_int(param_map.get("MAS0002", info.get("requested_command", 0)), info.get("requested_command", 0))
        button_request = self._button_requested_command(
            current_state=snapshot["current_state"],
            io_map=io_map,
            previous_inputs=info.get("button_inputs") or {},
            button_mask=button_mask,
        )
        if button_request is not None:
            requested_command = button_request
            self.params.apply_device_value("MAS0002", str(requested_command), promote_default=True)
            self.logs.log("machine", "info", f"button requested command -> {requested_command}")

        requested_state = command_to_target_state(requested_command, snapshot["current_state"])

        purge_active = critical_active or _truthy(param_map.get("MAS0028", "0"))
        if purge_active != _truthy(param_map.get("MAS0028", "0")):
            self.params.apply_device_value("MAS0028", "1" if purge_active else "0", promote_default=True)
            self._notify_microtom("MAS0028", "1" if purge_active else "0", dedupe_key="machine:MAS0028")

        new_state, state_source = settle_machine_state(
            requested_state,
            snapshot["current_state"],
            estop_ok=self._bool_io(io_map, "esp32_plc58", "I0.7", default=True),
            light_curtain_ok=self._bool_io(io_map, "esp32_plc58", "I0.8", default=True),
            ups_ok=self._bool_io(io_map, "raspi_plc21", "I0.6", default=True),
            purge_active=purge_active,
        )

        if new_state != snapshot["current_state"]:
            self.params.apply_device_value("MAS0001", str(new_state), promote_default=True)
            self._notify_microtom("MAS0001", str(new_state), dedupe_key="machine:MAS0001")
            self._record_event(
                "state_change",
                "info",
                f"Maschinenstatus gewechselt: {snapshot['current_state']} -> {new_state} ({state_label(new_state)})",
                {
                    "from_state": snapshot["current_state"],
                    "to_state": new_state,
                    "source": state_source,
                },
            )
            self.logs.log("machine", "info", f"state {snapshot['current_state']} -> {new_state} ({state_source})")

        actions = state_actions(new_state)
        self._apply_button_leds(new_state, button_mask, ts)
        self._apply_status_lamp(new_state, warning_active=warning_active, ts=ts)

        machine_label = self._current_production_label()
        info.update(
            {
                "requested_command": requested_command,
                "button_mask": button_mask,
                "allowed_actions": actions,
                "button_inputs": self._button_inputs(io_map),
                "critical_reasons": critical_reasons,
                "warning_keys": self._active_param_keys("MAW"),
                "error_keys": self._active_param_keys("MAE"),
                "status_lamp": lamp_outputs_for_state(new_state, warning_active=warning_active, ts=ts),
                "button_leds": button_led_plan(new_state, button_mask, ts=ts),
            }
        )
        self._write_state(
            current_state=new_state,
            requested_state=requested_state,
            state_source=state_source,
            warning_active=warning_active,
            purge_active=purge_active,
            production_label=machine_label,
            last_label_no=snapshot["last_label_no"],
            info=info,
        )

        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        row = self._state_row()
        recent_events = self._recent_events(limit=30)
        recent_labels = self._recent_labels(limit=20)
        return {
            "ok": True,
            "current_state": row["current_state"],
            "current_state_label": state_label(row["current_state"]),
            "requested_state": row["requested_state"],
            "requested_state_label": state_label(row["requested_state"]),
            "warning_active": bool(row["warning_active"]),
            "purge_active": bool(row["purge_active"]),
            "production_label": row["production_label"],
            "last_label_no": row["last_label_no"],
            "info": row["info"],
            "events": recent_events,
            "labels": recent_labels,
        }

    def handle_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        event_type = str((payload or {}).get("type") or "").strip().lower()
        if event_type == "label_complete":
            result = self._handle_label_complete(payload)
            return {"ok": True, "accepted": True, "event": event_type, "result": result}
        if event_type == "machine_state":
            state = _safe_int(payload.get("state"), 1)
            self.params.apply_device_value("MAS0001", str(state), promote_default=True)
            self._notify_microtom("MAS0001", str(state), dedupe_key="machine:MAS0001")
            self._record_event("machine_state", "info", f"ESP meldet Maschinenstatus {state} ({state_label(state)})", payload)
            return {"ok": True, "accepted": True, "event": event_type, "state": state}
        if event_type:
            self._record_event("machine_event", "info", f"Maschinenereignis empfangen: {event_type}", payload)
            return {"ok": True, "accepted": True, "event": event_type}
        return {"ok": False, "accepted": False, "detail": "missing event type"}

    def _handle_label_complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        label_no = _safe_int(payload.get("label_no"), 0)
        if label_no <= 0:
            raise RuntimeError("label_no missing")
        label = self._current_production_label()
        if not label:
            label = sanitize_production_label(self.params.get_effective_value("MAS0029"))
        material_ok = bool(int(payload.get("material_ok", 1)))
        print_ok = bool(int(payload.get("print_ok", 1)))
        verify_ok = bool(int(payload.get("verify_ok", 1)))
        removed = bool(int(payload.get("removed", 0)))
        production_ok = bool(int(payload.get("production_ok", 1 if (material_ok and print_ok and verify_ok and not removed) else 0)))
        zero_mm = float(payload.get("zero_mm", 0.0) or 0.0)
        exit_mm = float(payload.get("exit_mm", 0.0) or 0.0)
        packed = pack_label_status_word(
            label_no=label_no,
            material_ok=material_ok,
            print_ok=print_ok,
            verify_ok=verify_ok,
            removed=removed,
            production_ok=production_ok,
        )
        with self.db._conn() as c:
            c.execute(
                """INSERT INTO label_register(
                       production_label,label_no,created_ts,completed_ts,zero_mm,exit_mm,
                       material_ok,print_ok,verify_ok,removed,production_ok,emitted_to_microtom,payload_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(production_label,label_no) DO UPDATE SET
                     completed_ts=excluded.completed_ts,
                     zero_mm=excluded.zero_mm,
                     exit_mm=excluded.exit_mm,
                     material_ok=excluded.material_ok,
                     print_ok=excluded.print_ok,
                     verify_ok=excluded.verify_ok,
                     removed=excluded.removed,
                     production_ok=excluded.production_ok,
                     payload_json=excluded.payload_json""",
                (
                    label,
                    label_no,
                    now_ts(),
                    now_ts(),
                    zero_mm,
                    exit_mm,
                    int(material_ok),
                    int(print_ok),
                    int(verify_ok),
                    int(removed),
                    int(production_ok),
                    0,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            c.execute(
                "INSERT INTO label_events(ts,production_label,label_no,event_type,payload_json) VALUES(?,?,?,?,?)",
                (now_ts(), label, label_no, "label_complete", json.dumps(payload, ensure_ascii=False)),
            )

        self.params.apply_device_value("MAS0003", str(packed), promote_default=True)
        self._notify_microtom("MAS0003", str(packed), dedupe_key=None)
        self.production_logs.append_line(
            "labels",
            label,
            (
                f"[label_complete] label={label_no} packed={packed} material_ok={int(material_ok)} "
                f"print_ok={int(print_ok)} verify_ok={int(verify_ok)} removed={int(removed)} "
                f"production_ok={int(production_ok)} zero_mm={zero_mm:.3f} exit_mm={exit_mm:.3f}\n"
            ),
        )
        snapshot = self._state_row()
        info = dict(snapshot.get("info") or {})
        info["last_label_payload"] = dict(payload)
        self._write_state(
            current_state=snapshot["current_state"],
            requested_state=snapshot["requested_state"],
            state_source=snapshot["state_source"],
            warning_active=bool(snapshot["warning_active"]),
            purge_active=bool(snapshot["purge_active"]),
            production_label=label,
            last_label_no=max(snapshot["last_label_no"], label_no),
            info=info,
        )
        self._record_event(
            "label_complete",
            "info",
            f"Label {label_no} abgeschlossen -> MAS0003={packed}",
            {
                "production_label": label,
                "label_no": label_no,
                "packed": packed,
                "material_ok": material_ok,
                "print_ok": print_ok,
                "verify_ok": verify_ok,
                "removed": removed,
                "production_ok": production_ok,
            },
        )
        return {
            "production_label": label,
            "label_no": label_no,
            "packed": packed,
        }

    def _state_row(self) -> dict[str, Any]:
        with self.db._conn() as c:
            row = c.execute(
                """SELECT current_state,requested_state,state_source,warning_active,purge_active,
                          production_label,last_label_no,info_json,updated_ts
                   FROM machine_state WHERE singleton_id=1"""
            ).fetchone()
        if not row:
            return {
                "current_state": 1,
                "requested_state": 1,
                "state_source": "runtime",
                "warning_active": False,
                "purge_active": False,
                "production_label": "",
                "last_label_no": 0,
                "info": {},
                "updated_ts": 0.0,
            }
        try:
            info = json.loads(row[7] or "{}")
        except Exception:
            info = {}
        return {
            "current_state": int(row[0] or 1),
            "requested_state": int(row[1] or 1),
            "state_source": str(row[2] or "runtime"),
            "warning_active": bool(row[3]),
            "purge_active": bool(row[4]),
            "production_label": str(row[5] or ""),
            "last_label_no": int(row[6] or 0),
            "info": info,
            "updated_ts": float(row[8] or 0.0),
        }

    def _write_state(
        self,
        *,
        current_state: int,
        requested_state: int,
        state_source: str,
        warning_active: bool,
        purge_active: bool,
        production_label: str,
        last_label_no: int,
        info: dict[str, Any],
    ):
        with self.db._conn() as c:
            c.execute(
                """INSERT INTO machine_state(
                       singleton_id,current_state,requested_state,state_source,warning_active,purge_active,
                       production_label,last_label_no,info_json,updated_ts
                   ) VALUES(1,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(singleton_id) DO UPDATE SET
                     current_state=excluded.current_state,
                     requested_state=excluded.requested_state,
                     state_source=excluded.state_source,
                     warning_active=excluded.warning_active,
                     purge_active=excluded.purge_active,
                     production_label=excluded.production_label,
                     last_label_no=excluded.last_label_no,
                     info_json=excluded.info_json,
                     updated_ts=excluded.updated_ts""",
                (
                    int(current_state),
                    int(requested_state),
                    str(state_source or "runtime"),
                    int(bool(warning_active)),
                    int(bool(purge_active)),
                    str(production_label or ""),
                    int(last_label_no or 0),
                    json.dumps(info, ensure_ascii=False),
                    now_ts(),
                ),
            )

    def _record_event(self, event_type: str, severity: str, message: str, payload: dict[str, Any] | None = None):
        with self.db._conn() as c:
            c.execute(
                "INSERT INTO machine_events(ts,event_type,severity,message,payload_json) VALUES(?,?,?,?,?)",
                (now_ts(), event_type, severity, message, json.dumps(payload or {}, ensure_ascii=False)),
            )

    def _recent_events(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.db._conn() as c:
            rows = c.execute(
                "SELECT ts,event_type,severity,message,payload_json FROM machine_events ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        out = []
        for ts, event_type, severity, message, payload_json in rows:
            try:
                payload = json.loads(payload_json or "{}")
            except Exception:
                payload = {}
            out.append(
                {
                    "ts": float(ts or 0.0),
                    "event_type": event_type,
                    "severity": severity,
                    "message": message,
                    "payload": payload,
                }
            )
        return out

    def _recent_labels(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.db._conn() as c:
            rows = c.execute(
                """SELECT production_label,label_no,created_ts,completed_ts,material_ok,print_ok,
                          verify_ok,removed,production_ok,payload_json
                   FROM label_register ORDER BY created_ts DESC LIMIT ?""",
                (int(limit),),
            ).fetchall()
        out = []
        for row in rows:
            try:
                payload = json.loads(row[9] or "{}")
            except Exception:
                payload = {}
            out.append(
                {
                    "production_label": row[0],
                    "label_no": int(row[1] or 0),
                    "created_ts": float(row[2] or 0.0),
                    "completed_ts": float(row[3] or 0.0),
                    "material_ok": bool(row[4]),
                    "print_ok": bool(row[5]),
                    "verify_ok": bool(row[6]),
                    "removed": bool(row[7]),
                    "production_ok": bool(row[8]),
                    "payload": payload,
                }
            )
        return out

    def _param_values_by_prefix(self, prefixes: tuple[str, ...]) -> dict[str, str]:
        placeholders = ",".join("?" for _ in prefixes)
        with self.db._conn() as c:
            rows = c.execute(
                f"""SELECT p.pkey, COALESCE(v.value, p.default_v, '0')
                    FROM params p
                    LEFT JOIN param_values v ON v.pkey = p.pkey
                    WHERE p.ptype IN ({placeholders})""",
                tuple(prefixes),
            ).fetchall()
        return {str(row[0]): str(row[1] if row[1] is not None else "0") for row in rows}

    def _active_param_keys(self, ptype: str) -> list[str]:
        with self.db._conn() as c:
            rows = c.execute(
                """SELECT p.pkey, COALESCE(v.value, p.default_v, '0')
                   FROM params p
                   LEFT JOIN param_values v ON v.pkey = p.pkey
                   WHERE p.ptype = ?""",
                (str(ptype or "").upper(),),
            ).fetchall()
        return [str(pkey) for pkey, value in rows if _truthy(value)]

    def _warning_active(self, param_map: dict[str, str]) -> bool:
        for pkey, value in param_map.items():
            if pkey.startswith("MAW") and _truthy(value):
                return True
        return False

    def _critical_state(self, io_map: dict[tuple[str, str], str], param_map: dict[str, str]) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        if not self._bool_io(io_map, "esp32_plc58", "I0.7", default=True):
            reasons.append("notaus")
        if not self._bool_io(io_map, "raspi_plc21", "I0.6", default=True):
            reasons.append("usv_not_ok")
        if self._bool_io(io_map, "esp32_plc58", "I0.4", default=False):
            reasons.append("bahnriss_einlauf")
        if self._bool_io(io_map, "esp32_plc58", "I0.11", default=False):
            reasons.append("bahnriss_auswurf")
        for pkey, value in param_map.items():
            if pkey.startswith("MAE") and _truthy(value):
                reasons.append(pkey)
        return bool(reasons), reasons

    def _io_values(self) -> dict[tuple[str, str], str]:
        values: dict[tuple[str, str], str] = {}
        for item in self.io_store.list_points(include_reserved=True):
            values[(str(item.get("device_code") or ""), str(item.get("pin_label") or "").upper())] = str(
                item.get("value") if item.get("value") is not None else "0"
            )
        return values

    def _bool_io(self, io_map: dict[tuple[str, str], str], device_code: str, pin_label: str, *, default: bool) -> bool:
        key = (str(device_code or ""), str(pin_label or "").upper())
        if key not in io_map:
            return bool(default)
        return _truthy(io_map.get(key))

    def _button_inputs(self, io_map: dict[tuple[str, str], str]) -> dict[str, bool]:
        return {
            name: self._bool_io(io_map, device_code, pin_label, default=False)
            for name, (device_code, pin_label) in BUTTON_INPUTS.items()
        }

    def _button_requested_command(
        self,
        *,
        current_state: int,
        io_map: dict[tuple[str, str], str],
        previous_inputs: dict[str, Any],
        button_mask: dict[str, bool],
    ) -> Optional[int]:
        current_inputs = self._button_inputs(io_map)
        allowed_actions = state_actions(current_state)
        for button_name, active_now in current_inputs.items():
            was_active = bool(previous_inputs.get(button_name))
            if not active_now or was_active:
                continue
            command = button_to_command(button_name, current_state)
            if command is None:
                continue
            action_name = "pause" if button_name == "start_pause" and int(current_state or 0) == 5 else (
                "start" if button_name == "start_pause" else button_name
            )
            if not allowed_actions.get(action_name, False):
                continue
            if not button_mask.get(action_name, False):
                self.logs.log("machine", "info", f"button {button_name} ignored by MAP0065")
                continue
            return command
        return None

    def _apply_button_leds(self, state: int, button_mask: dict[str, bool], ts: float):
        io_runtime = IoRuntime(self.cfg, self.io_store)
        led_plan = button_led_plan(state, button_mask, ts=ts)
        for action, pins in BUTTON_LED_OUTPUTS.items():
            for device_code, pin in pins:
                point = self.io_store.get_point(f"{device_code}__{pin.replace('.', '_')}")
                if not point:
                    continue
                io_runtime.write_output(point["io_key"], bool(led_plan.get(pin, False)))

    def _apply_status_lamp(self, state: int, *, warning_active: bool, ts: float):
        io_runtime = IoRuntime(self.cfg, self.io_store)
        lamp = lamp_outputs_for_state(state, warning_active=warning_active, ts=ts)
        for color, enabled in lamp.items():
            device_code, pin = STATUS_LAMP_OUTPUTS[color]
            point = self.io_store.get_point(f"{device_code}__{pin.replace('.', '_')}")
            if not point:
                continue
            io_runtime.write_output(point["io_key"], bool(enabled))

    def _current_production_label(self) -> str:
        active = self.production_logs.active_state()
        if active:
            return str(active.get("production_label") or "").strip()
        manifest = self.production_logs.ready_manifest()
        label = str(manifest.get("production_label") or "").strip()
        if label:
            return label
        return sanitize_production_label(self.params.get_effective_value("MAS0029"))

    def _notify_microtom(self, pkey: str, value: str, *, dedupe_key: str | None):
        if self.outbox is None:
            return
        targets = peer_urls(self.cfg, "/api/inbox")
        for url in targets:
            self.outbox.enqueue(
                "POST",
                url,
                {},
                {"msg": f"{pkey}={value}", "source": "raspi", "origin": "machine-runtime"},
                None,
                priority=20,
                dedupe_key=dedupe_key,
                drop_if_duplicate=bool(dedupe_key),
            )


def parse_machine_event_line(line: str) -> dict[str, Any] | None:
    raw = str(line or "").strip()
    if not raw:
        return None
    if not raw.upper().startswith("EVT "):
        return None
    payload_raw = raw[4:].strip()
    if not payload_raw:
        return None
    try:
        payload = json.loads(payload_raw)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def normalize_ai_text(text: str | None) -> str:
    raw = str(text or "").strip()
    raw = re.sub(r"\s+", " ", raw)
    return raw

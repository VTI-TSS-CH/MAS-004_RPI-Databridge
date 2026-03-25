from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

from mas004_rpi_databridge.db import DB, now_ts
from mas004_rpi_databridge.outbox import Outbox
from mas004_rpi_databridge.params import ParamStore
from mas004_rpi_databridge.peers import peer_urls
from mas004_rpi_databridge.timeutil import local_now

DEFAULT_PRODUCTION_LOG_DIR = "/var/lib/mas004_rpi_databridge/production_logs"
PRODUCTION_STATE_FILE = "_production_state.json"

PRODUCTION_GROUP_LABELS = {
    "all": "Gesamtanlage",
    "esp": "ESP32-PLC",
    "tto": "TTO 6530",
    "laser": "Laser 3350",
}

PRODUCTION_GROUP_PREFIX = {
    "all": "gesamtanlage",
    "esp": "esp32_plc",
    "tto": "tto_6530",
    "laser": "laser_3350",
}


def sanitize_production_label(raw: Optional[str]) -> str:
    txt = (raw or "").strip()
    if not txt:
        txt = local_now().strftime("produktion_%Y%m%d_%H%M%S")
    txt = re.sub(r"\s+", "_", txt)
    txt = re.sub(r"[^A-Za-z0-9._-]+", "_", txt)
    txt = txt.strip("._-")
    return txt or local_now().strftime("produktion_%Y%m%d_%H%M%S")


def production_file_name(group: str, label: str) -> str:
    prefix = PRODUCTION_GROUP_PREFIX[group]
    return f"{prefix}_{label}.txt"


class ProductionLogManager:
    def __init__(
        self,
        db: DB,
        cfg: Any | None = None,
        outbox: Outbox | None = None,
        log_dir: str = DEFAULT_PRODUCTION_LOG_DIR,
    ):
        self.db = db
        self.cfg = cfg
        self.outbox = outbox
        self.log_dir = log_dir
        self.state_path = os.path.join(self.log_dir, PRODUCTION_STATE_FILE)
        self._state_cache: Dict[str, Any] | None = None
        self._state_mtime: float | None = None
        os.makedirs(self.log_dir, exist_ok=True)

    def _default_state(self) -> Dict[str, Any]:
        return {
            "active": False,
            "ready": False,
            "production_label": "",
            "production_label_raw": "",
            "started_ts": None,
            "stopped_ts": None,
            "files": [],
        }

    def _read_state(self, *, use_cache: bool = False) -> Dict[str, Any]:
        try:
            mtime = os.path.getmtime(self.state_path)
        except OSError:
            self._state_cache = self._default_state()
            self._state_mtime = None
            return dict(self._state_cache)

        if use_cache and self._state_cache is not None and self._state_mtime == mtime:
            return dict(self._state_cache)

        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception:
            data = {}

        state = self._default_state()
        state.update(data)
        self._state_cache = dict(state)
        self._state_mtime = mtime
        return state

    def _write_state(self, state: Dict[str, Any]):
        os.makedirs(self.log_dir, exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.state_path)
        try:
            self._state_mtime = os.path.getmtime(self.state_path)
        except OSError:
            self._state_mtime = None
        self._state_cache = dict(state)

    def active_state(self) -> Optional[Dict[str, Any]]:
        state = self._read_state(use_cache=True)
        if not bool(state.get("active")):
            return None
        label = (state.get("production_label") or "").strip()
        if not label:
            return None
        return state

    def path_for_group(self, group: str, label: str) -> str:
        return os.path.join(self.log_dir, production_file_name(group, label))

    def ready_manifest(self) -> Dict[str, Any]:
        state = self._read_state()
        label = (state.get("production_label") or "").strip()
        out = {
            "ok": True,
            "active": bool(state.get("active")),
            "ready": bool(state.get("ready")),
            "production_label": label,
            "production_label_raw": state.get("production_label_raw") or "",
            "started_ts": state.get("started_ts"),
            "stopped_ts": state.get("stopped_ts"),
            "files": [],
        }
        if not label:
            return out

        files: List[Dict[str, Any]] = []
        for group, group_label in PRODUCTION_GROUP_LABELS.items():
            name = production_file_name(group, label)
            path = self.path_for_group(group, label)
            if not os.path.exists(path):
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            files.append(
                {
                    "name": name,
                    "group": group,
                    "group_label": group_label,
                    "size_bytes": int(st.st_size),
                    "mtime_ts": float(st.st_mtime),
                }
            )
        files.sort(key=lambda item: item["name"])
        out["files"] = files
        return out

    def can_start_new_production(self) -> tuple[bool, str]:
        manifest = self.ready_manifest()
        if manifest.get("ready") or manifest.get("files"):
            return False, "NAK_ProductionLogfilesPending"
        return True, "OK"

    def resolve_ready_file(self, name: str) -> str:
        safe_name = os.path.basename((name or "").strip())
        if safe_name != (name or "").strip():
            raise RuntimeError("invalid file name")
        if not safe_name.endswith(".txt"):
            raise RuntimeError("invalid file type")
        manifest = self.ready_manifest()
        known = {item["name"] for item in manifest.get("files", [])}
        if safe_name not in known:
            raise RuntimeError("file not found")
        return os.path.join(self.log_dir, safe_name)

    def _set_ready_param(self, value: str):
        params = ParamStore(self.db)
        if not params.get_meta("MAS0030"):
            return
        params.apply_device_value("MAS0030", str(value), promote_default=True)

    def _notify_ready_to_microtom(self, value: str):
        if self.cfg is None or self.outbox is None:
            return
        targets = peer_urls(self.cfg, "/api/inbox")
        for url in targets:
            self.outbox.enqueue(
                "POST",
                url,
                {},
                {"msg": f"MAS0030={value}", "source": "raspi"},
                None,
                priority=15,
                dedupe_key="raspi:MAS0030",
                drop_if_duplicate=True,
            )

    def handle_param_change(self, pkey: str, value: str) -> Optional[Dict[str, Any]]:
        key = (pkey or "").strip().upper()
        text_value = "" if value is None else str(value).strip()
        if key != "MAS0002":
            return None

        state = self._read_state()
        if text_value == "1":
            if state.get("active"):
                return None
            params = ParamStore(self.db)
            raw_label = params.get_effective_value("MAS0029")
            label = sanitize_production_label(raw_label)
            new_state = {
                "active": True,
                "ready": False,
                "production_label": label,
                "production_label_raw": raw_label,
                "started_ts": now_ts(),
                "stopped_ts": None,
                "files": [production_file_name(group, label) for group in PRODUCTION_GROUP_LABELS],
            }
            self._write_state(new_state)
            self._set_ready_param("0")
            return {"event": "start", "production_label": label}

        if text_value == "2":
            if not state.get("active"):
                return None
            label = (state.get("production_label") or "").strip()
            state["active"] = False
            state["ready"] = True
            state["stopped_ts"] = now_ts()
            state["files"] = [production_file_name(group, label) for group in PRODUCTION_GROUP_LABELS] if label else []
            self._write_state(state)
            self._set_ready_param("1")
            self._notify_ready_to_microtom("1")
            return {"event": "stop", "production_label": label}

        return None

    def acknowledge_ready(self) -> Dict[str, Any]:
        state = self._read_state()
        if state.get("ready") and not self.ready_manifest().get("files"):
            state["ready"] = False
            self._write_state(state)
            self._set_ready_param("0")
        return self.ready_manifest()

    def consume_ready_file(self, name: str, max_bytes: int = 5_000_000) -> bytes:
        path = self.resolve_ready_file(name)
        with open(path, "rb") as f:
            data = f.read()
        if len(data) > max_bytes:
            data = data[-max_bytes:]
        try:
            os.remove(path)
        except OSError:
            pass
        self._refresh_ready_state_after_removal()
        return data

    def _refresh_ready_state_after_removal(self):
        manifest = self.ready_manifest()
        if manifest.get("files"):
            return
        state = self._read_state()
        if not state.get("ready"):
            return
        state["ready"] = False
        self._write_state(state)
        self._set_ready_param("0")
        self._notify_ready_to_microtom("0")

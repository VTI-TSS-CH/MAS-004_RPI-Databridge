from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

from mas004_rpi_databridge.db import DB, now_ts
from mas004_rpi_databridge.outbox import Outbox
from mas004_rpi_databridge.params import ParamStore
from mas004_rpi_databridge.peers import peer_urls
from mas004_rpi_databridge.state_dedupe import ValueDedupeStore
from mas004_rpi_databridge.timeutil import format_local_timestamp, local_now

DEFAULT_PRODUCTION_LOG_DIR = "/var/lib/mas004_rpi_databridge/production_logs"
PRODUCTION_STATE_FILE = "_production_state.json"

PRODUCTION_GROUP_LABELS = {
    "all": "Gesamtanlage",
    "esp": "ESP32-PLC",
    "tto": "TTO 6530",
    "laser": "Laser 3350",
    "labels": "LabelProductionLog",
}

PRODUCTION_GROUP_PREFIX = {
    "all": "gesamtanlage",
    "esp": "esp32_plc",
    "tto": "tto_6530",
    "laser": "laser_3350",
    "labels": "label_production",
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

    def append_line(self, group: str, label: str, line: str):
        group_key = str(group or "").strip()
        prod_label = str(label or "").strip()
        if not group_key or not prod_label or group_key not in PRODUCTION_GROUP_LABELS:
            return
        os.makedirs(self.log_dir, exist_ok=True)
        path = self.path_for_group(group_key, prod_label)
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(str(line))
        except Exception:
            pass

    def _append_lifecycle_line(self, label: str, message: str):
        prod_label = str(label or "").strip()
        if not prod_label:
            return
        line = f"[{format_local_timestamp(now_ts())}] [raspi] INFO {message}\n"
        self.append_line("all", prod_label, line)

    def _existing_ready_files(self, label: str) -> List[Dict[str, Any]]:
        prod_label = str(label or "").strip()
        if not prod_label:
            return []

        files: List[Dict[str, Any]] = []
        for group, group_label in PRODUCTION_GROUP_LABELS.items():
            name = production_file_name(group, prod_label)
            path = self.path_for_group(group, prod_label)
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
        return files

    def _set_ready_param_if_needed(self, value: str):
        params = ParamStore(self.db)
        if not params.get_meta("MAS0030"):
            return
        if str(params.get_effective_value("MAS0030")) == str(value):
            return
        params.apply_device_value("MAS0030", str(value), promote_default=False)

    def _reconcile_ready_state(self, state: Dict[str, Any]) -> tuple[Dict[str, Any], List[Dict[str, Any]], bool]:
        state = dict(state)
        active = bool(state.get("active"))
        label = (state.get("production_label") or "").strip()
        existing_files = self._existing_ready_files(label)
        file_names = [item["name"] for item in existing_files]

        # A production is only "ready for download" after stop, never while recording.
        should_be_ready = False
        if not active and existing_files:
            should_be_ready = bool(state.get("ready")) or bool(state.get("stopped_ts"))

        changed = False
        if bool(state.get("ready")) != should_be_ready:
            state["ready"] = should_be_ready
            changed = True
        if list(state.get("files") or []) != file_names:
            state["files"] = file_names
            changed = True

        if changed:
            self._write_state(state)

        self._set_ready_param_if_needed("1" if should_be_ready else "0")
        exposed_files = existing_files if should_be_ready else []
        return state, exposed_files, should_be_ready

    def ready_manifest(self) -> Dict[str, Any]:
        state = self._read_state()
        state, files, ready = self._reconcile_ready_state(state)
        label = (state.get("production_label") or "").strip()
        return {
            "ok": True,
            "active": bool(state.get("active")),
            "ready": ready,
            "production_label": label,
            "production_label_raw": state.get("production_label_raw") or "",
            "started_ts": state.get("started_ts"),
            "stopped_ts": state.get("stopped_ts"),
            "files": files,
        }

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
        params.apply_device_value("MAS0030", str(value), promote_default=False)

    def _notify_ready_to_microtom(self, value: str):
        if self.cfg is None or self.outbox is None:
            return
        targets = peer_urls(self.cfg, "/api/inbox")
        if targets and not ValueDedupeStore(self.db).should_send("microtom", "MAS0030", value):
            return
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
            self._append_lifecycle_line(label, f"production logging started: {label}")
            return {"event": "start", "production_label": label}

        if text_value == "2":
            if not state.get("active"):
                return None
            label = (state.get("production_label") or "").strip()
            self._append_lifecycle_line(label, f"production logging stopped: {label}")
            state["active"] = False
            files = self._existing_ready_files(label)
            state["ready"] = bool(files)
            state["stopped_ts"] = now_ts()
            state["files"] = [item["name"] for item in files]
            self._write_state(state)
            if state["ready"]:
                self._set_ready_param("1")
                self._notify_ready_to_microtom("1")
            else:
                self._set_ready_param("0")
            return {"event": "stop", "production_label": label}

        return None

    def acknowledge_ready(self) -> Dict[str, Any]:
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
        state = self._read_state()
        label = (state.get("production_label") or "").strip()
        files = self._existing_ready_files(label)
        if files:
            file_names = [item["name"] for item in files]
            if list(state.get("files") or []) != file_names:
                state["files"] = file_names
                self._write_state(state)
            return
        if not state.get("ready"):
            self._set_ready_param_if_needed("0")
            return
        state["ready"] = False
        state["files"] = []
        self._write_state(state)
        self._set_ready_param("0")
        self._notify_ready_to_microtom("0")

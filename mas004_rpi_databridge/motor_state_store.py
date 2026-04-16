from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from typing import Any

from mas004_rpi_databridge.config import Settings


class MotorStateStore:
    def __init__(self, cfg: Settings):
        root = os.path.dirname(cfg.db_path) or "."
        self.path = os.path.join(root, "motor_ui_state.json")
        self._lock = threading.Lock()

    def _default(self) -> dict[str, Any]:
        return {"simulation_ids": [], "last_known": {}}

    def _read_unlocked(self) -> dict[str, Any]:
        if not os.path.exists(self.path):
            return self._default()
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception:
            return self._default()
        state = self._default()
        if isinstance(data.get("simulation_ids"), list):
            state["simulation_ids"] = data["simulation_ids"]
        if isinstance(data.get("last_known"), dict):
            state["last_known"] = data["last_known"]
        return state

    def _write_unlocked(self, data: dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    def simulation_ids(self) -> set[int]:
        with self._lock:
            raw = self._read_unlocked().get("simulation_ids") or []
        out: set[int] = set()
        for item in raw:
            try:
                out.add(int(item))
            except Exception:
                continue
        return out

    def set_simulation(self, motor_id: int, enabled: bool) -> set[int]:
        with self._lock:
            data = self._read_unlocked()
            raw_ids = data.get("simulation_ids") or []
            ids: set[int] = set()
            for item in raw_ids:
                try:
                    ids.add(int(item))
                except Exception:
                    continue
            if enabled:
                ids.add(int(motor_id))
            else:
                ids.discard(int(motor_id))
            data["simulation_ids"] = sorted(ids)
            self._write_unlocked(data)
            return set(ids)

    def cached_motors(self) -> dict[int, dict[str, Any]]:
        with self._lock:
            raw = deepcopy(self._read_unlocked().get("last_known") or {})
        out: dict[int, dict[str, Any]] = {}
        for key, value in raw.items():
            if not isinstance(value, dict):
                continue
            try:
                out[int(key)] = value
            except Exception:
                continue
        return out

    def remember_motors(self, motors: list[dict[str, Any]]) -> None:
        if not motors:
            return
        with self._lock:
            data = self._read_unlocked()
            cache = data.get("last_known") or {}
            if not isinstance(cache, dict):
                cache = {}
            for motor in motors:
                if not isinstance(motor, dict):
                    continue
                try:
                    mid = int(motor.get("id") or 0)
                except Exception:
                    continue
                if mid <= 0:
                    continue
                cache[str(mid)] = deepcopy(motor)
            data["last_known"] = cache
            self._write_unlocked(data)

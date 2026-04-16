from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from mas004_rpi_databridge.config import Settings


@dataclass(frozen=True)
class SmartWicklerDescriptor:
    role: str
    label: str
    host_attr: str
    port_attr: str
    simulation_attr: str


_DESCRIPTORS = {
    "unwinder": SmartWicklerDescriptor(
        role="unwinder",
        label="Abwickler",
        host_attr="smart_unwinder_host",
        port_attr="smart_unwinder_port",
        simulation_attr="smart_unwinder_simulation",
    ),
    "rewinder": SmartWicklerDescriptor(
        role="rewinder",
        label="Aufwickler",
        host_attr="smart_rewinder_host",
        port_attr="smart_rewinder_port",
        simulation_attr="smart_rewinder_simulation",
    ),
}


def normalize_winder_role(role: str) -> str:
    key = (role or "").strip().lower()
    if key not in _DESCRIPTORS:
        raise ValueError(f"Unknown winder role '{role}'")
    return key


def smart_wickler_descriptor(role: str) -> SmartWicklerDescriptor:
    return _DESCRIPTORS[normalize_winder_role(role)]


def _simulation_payload(role: str, label: str, host: str, port: int, simulation: bool, error: str = "") -> dict[str, Any]:
    is_unwinder = role == "unwinder"
    mode_label = "Simulation" if simulation else "Offline"
    mode_css = "warn" if simulation else "fault"
    fill_percent = 100.0 if is_unwinder else 0.0
    return {
        "ok": simulation,
        "config": {
            "role": role,
            "roleLabel": label,
            "deviceIp": host,
            "subnet": "255.255.255.0",
            "gateway": "",
            "peerIp": "",
            "peerPort": 0,
            "peerPushEnabled": False,
            "peerPath": "/api/inbox",
            "wipeMaxCounts": 1000,
            "wipeIdlePercent": 50,
            "wipeThresholdPercent": 10,
            "maxLabelSpeedMmS": 400.0,
            "controlKp": 1.0,
            "controlKd": 0.2,
        },
        "master": {
            "map0023": 0,
            "map0024": 0,
            "map0025": 0.0,
            "map0047": False,
        },
        "telemetry": {
            "modeLabel": mode_label,
            "modeCss": mode_css,
            "wipePercent": 50.0,
            "fillPercent": fill_percent,
            "rollerSpeedMmS": 0.0,
            "motorSpeedHz": 0.0,
            "estimatedDiameterMm": 76.0,
            "pauseRequest": False,
        },
        "values": {
            "statusMas": 0,
            "fillMas": int(round(fill_percent)),
            "maeBlocked": False,
            "maeHigh": False,
            "maeLow": False,
        },
        "drive": {
            "online": False,
            "ready": False,
            "move": False,
            "alarm": False,
            "alarmCode": 0,
        },
        "device": {
            "role": role,
            "label": label,
            "host": host,
            "port": port,
            "simulation": simulation,
            "reachable": False,
            "base_url": f"http://{host}:{port}" if host and port > 0 else "",
            "error": error or ("Simulation aktiv" if simulation else "Wickler-Endpoint nicht erreichbar"),
        },
    }


class SmartWicklerClient:
    def __init__(self, cfg: Settings, role: str):
        self.cfg = cfg
        self.descriptor = smart_wickler_descriptor(role)
        self.host = str(getattr(cfg, self.descriptor.host_attr, "") or "").strip()
        try:
            self.port = int(getattr(cfg, self.descriptor.port_attr, 0) or 0)
        except Exception:
            self.port = 0
        self.simulation = bool(getattr(cfg, self.descriptor.simulation_attr, True))

    def available(self) -> bool:
        return bool(self.host) and self.port > 0 and not self.simulation

    def device_ui_url(self) -> str:
        return f"http://{self.host}:{self.port}" if self.host and self.port > 0 else ""

    def fetch_state(self) -> dict[str, Any]:
        if not self.available():
            return _simulation_payload(
                role=self.descriptor.role,
                label=self.descriptor.label,
                host=self.host,
                port=self.port,
                simulation=self.simulation,
            )

        try:
            timeout_s = max(1.0, min(5.0, float(getattr(self.cfg, "http_timeout_s", 5.0) or 5.0)))
            with httpx.Client(timeout=timeout_s) as client:
                response = client.get(self.device_ui_url().rstrip("/") + "/api/state")
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            return _simulation_payload(
                role=self.descriptor.role,
                label=self.descriptor.label,
                host=self.host,
                port=self.port,
                simulation=False,
                error=str(exc),
            )

        payload = dict(payload or {})
        payload.setdefault("config", {})
        payload.setdefault("master", {})
        payload.setdefault("telemetry", {})
        payload.setdefault("values", {})
        payload.setdefault("drive", {})
        payload["device"] = {
            "role": self.descriptor.role,
            "label": self.descriptor.label,
            "host": self.host,
            "port": self.port,
            "simulation": False,
            "reachable": True,
            "base_url": self.device_ui_url(),
            "error": "",
        }
        payload["ok"] = True
        return payload

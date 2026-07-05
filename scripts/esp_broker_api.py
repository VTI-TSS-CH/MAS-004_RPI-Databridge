from __future__ import annotations

import json
import ssl
import sys
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mas004_rpi_databridge.config import DEFAULT_CFG_PATH, Settings


def load_settings(path: str = DEFAULT_CFG_PATH) -> Settings:
    return Settings.load(path)


def exchange_via_databridge(
    cfg: Settings,
    line: str,
    *,
    read_timeout_s: float = 2.0,
    read_limit: int = 8192,
    priority: bool = False,
    request_timeout_s: float | None = None,
) -> tuple[str, dict[str, Any]]:
    scheme = "https" if bool(getattr(cfg, "webui_https", False)) else "http"
    base_url = f"{scheme}://127.0.0.1:{int(getattr(cfg, 'webui_port', 8080) or 8080)}"
    payload = {
        "line": str(line or "").strip(),
        "read_timeout_s": float(read_timeout_s or 2.0),
        "read_limit": int(read_limit or 8192),
        "priority": bool(priority),
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base_url + "/api/esp/command",
        data=data,
        headers={
            "Content-Type": "application/json",
            "X-Token": str(getattr(cfg, "ui_token", "") or ""),
        },
        method="POST",
    )
    timeout_s = float(request_timeout_s or (max(1.0, read_timeout_s) + 2.0))
    context = None
    if scheme == "https" and not bool(getattr(cfg, "tls_verify", False)):
        context = ssl._create_unverified_context()
    with urllib.request.urlopen(req, timeout=timeout_s, context=context) as resp:
        body = json.loads(resp.read().decode("utf-8") or "{}")
    if not bool(body.get("ok")):
        raise RuntimeError(body)
    return str(body.get("reply") or ""), dict(body)

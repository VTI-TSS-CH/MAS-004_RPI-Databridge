from __future__ import annotations

import argparse
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Send one ESP command through the running Databridge ESP broker API."
    )
    parser.add_argument("line", nargs="?", default="PING", help="ESP command line, default: PING")
    parser.add_argument("--config", default=DEFAULT_CFG_PATH, help="Databridge config path")
    parser.add_argument("--read-timeout", type=float, default=2.0, help="ESP read timeout in seconds")
    parser.add_argument("--read-limit", type=int, default=8192, help="Maximum ESP reply size")
    parser.add_argument("--request-timeout", type=float, default=None, help="HTTP request timeout in seconds")
    parser.add_argument("--priority", action="store_true", help="Use broker priority queue")
    parser.add_argument("--json", action="store_true", help="Print the full API response JSON")
    args = parser.parse_args()

    cfg = load_settings(args.config)
    reply, body = exchange_via_databridge(
        cfg,
        args.line,
        read_timeout_s=args.read_timeout,
        read_limit=args.read_limit,
        priority=args.priority,
        request_timeout_s=args.request_timeout,
    )
    if args.json:
        print(json.dumps(body, ensure_ascii=False, sort_keys=True))
    else:
        print(reply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

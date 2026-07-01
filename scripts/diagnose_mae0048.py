from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mas004_rpi_databridge.config import Settings  # noqa: E402
from mas004_rpi_databridge.db import DB  # noqa: E402
from mas004_rpi_databridge.mae0048_diagnostics import collect_mae0048_diagnostics  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Passive MAE0048 diagnostic snapshot.")
    parser.add_argument("--config", default=None, help="Path to config.json")
    parser.add_argument("--json", action="store_true", help="Print full JSON payload")
    args = parser.parse_args()

    cfg = Settings.load(args.config) if args.config else Settings.load()
    payload = collect_mae0048_diagnostics(cfg, DB(cfg.db_path))
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload.get("ok") else 2

    print("MAE0048 Diagnose")
    print("================")
    for item in payload.get("findings") or []:
        print(f"- {item}")
    reg = payload.get("registration") or {}
    motor = payload.get("motor3") or {}
    print("")
    print(
        "Registration: "
        f"reason={reg.get('reason')!r}, "
        f"label={reg.get('label_no')}, "
        f"error={reg.get('error_mm')} mm, "
        f"abs={reg.get('abs_error_mm')} mm, "
        f"attempts={reg.get('registration_attempts')}/{reg.get('max_attempts')}"
    )
    attempts = reg.get("attempts") or []
    if attempts:
        print("Korrekturversuche:")
        for item in attempts:
            if not (item.get("ms") or item.get("commanded") or item.get("error_mm") or item.get("command_mm")):
                continue
            print(
                f"- #{item.get('index')}: "
                f"restfehler={item.get('error_mm')} mm, "
                f"id3_befehl={item.get('command_mm')} mm, "
                f"gesendet={item.get('commanded')}"
            )
    print(
        "Motor 3: "
        f"ready={motor.get('ready')}, busy={motor.get('busy')}, move={motor.get('move')}, "
        f"in_pos={motor.get('in_pos')}, alarm={motor.get('alarm')}, "
        f"cmd_feedback_error_mm={motor.get('command_feedback_error_mm')}"
    )
    errors = payload.get("errors") or []
    if errors:
        print("")
        print("Diagnosefehler:")
        for item in errors:
            print(f"- {item}")
    return 0 if payload.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Press a virtual machine button through the same Runtime path as the HMI."""

from __future__ import annotations

import argparse
import json

from mas004_rpi_databridge.config import DEFAULT_CFG_PATH, Settings
from mas004_rpi_databridge.db import DB
from mas004_rpi_databridge.io_master import IoStore
from mas004_rpi_databridge.logstore import LogStore
from mas004_rpi_databridge.machine_runtime import MachineRuntime
from mas004_rpi_databridge.outbox import Outbox
from mas004_rpi_databridge.params import ParamStore


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("button")
    parser.add_argument("--cfg", default=DEFAULT_CFG_PATH)
    parser.add_argument("--actor", default="live-press-button")
    args = parser.parse_args()

    cfg = Settings.load(args.cfg)
    db = DB(cfg.db_path)
    runtime = MachineRuntime(cfg, db, ParamStore(db), IoStore(db), LogStore(db), Outbox(db))
    result = runtime.press_virtual_button(args.button, actor=args.actor)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

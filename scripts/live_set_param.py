#!/usr/bin/env python3
"""Set one MAS/MAP/MAE parameter directly through ParamStore on the live Raspi."""

from __future__ import annotations

import argparse
import json

from mas004_rpi_databridge.config import DEFAULT_CFG_PATH, Settings
from mas004_rpi_databridge.db import DB
from mas004_rpi_databridge.params import ParamStore


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pkey")
    parser.add_argument("value")
    parser.add_argument("--cfg", default=DEFAULT_CFG_PATH)
    parser.add_argument("--actor", default="live-set-param")
    args = parser.parse_args()

    cfg = Settings.load(args.cfg)
    params = ParamStore(DB(cfg.db_path))
    ok, message = params.set_value(args.pkey, args.value, actor=args.actor)
    payload = {
        "ok": ok,
        "message": message,
        "pkey": args.pkey.upper(),
        "value": args.value,
        "effective": params.get_effective_value(args.pkey),
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

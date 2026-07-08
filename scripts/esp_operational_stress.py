#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from esp_broker_api import exchange_via_databridge, load_settings
from mas004_rpi_databridge.config import DEFAULT_CFG_PATH, Settings


SAFE_STANDSTILL_STATES = {1, 8, 9, 20, 21}
PROBE_COMMANDS = [
    "PING",
    "STATUS?",
    "OUTBOUND STATUS?",
    "MOTOR POLL?",
    "PROCESS STATUS?",
    "IO SNAPSHOT?",
]
SIM_COMMANDS = [
    "PROCESS SIM INFEED_MM=0.5",
    "PROCESS SIM DRIVE_MM=0.5",
    "PROCESS SIM LABEL_START",
    "PROCESS SIM INFEED_MM=1.5",
    "PROCESS SIM DRIVE_MM=1.5",
    "PROCESS SIM LABEL_END",
    "PROCESS SIM CONTROL=1",
    "PROCESS SIM CONTROL=0",
]


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((p / 100.0) * (len(ordered) - 1)))))
    return ordered[idx]


def current_machine_state(cfg: Settings) -> int | None:
    db_path = str(getattr(cfg, "db_path", "") or "").strip()
    if not db_path:
        return None
    uri = "file:" + db_path.replace("\\", "/") + "?mode=ro&immutable=1"
    try:
        with sqlite3.connect(uri, uri=True, timeout=2.0) as conn:
            row = conn.execute("SELECT current_state FROM machine_state WHERE singleton_id=1").fetchone()
        return int(row[0]) if row else None
    except sqlite3.Error:
        return None


def classify_reply(command: str, reply: str) -> str:
    text = str(reply or "").strip()
    upper = text.upper()
    if not text:
        return "empty"
    if upper.startswith("NAK"):
        return "nak"
    if command == "PING":
        return "ok" if text == "PONG" else "bad_reply"
    if command == "STATUS?":
        return "ok" if text.startswith("STATUS ") else "bad_reply"
    if command == "OUTBOUND STATUS?":
        return "ok" if text.startswith("OUTBOUND ") else "bad_reply"
    if command == "MOTOR POLL?":
        return "ok" if text.startswith("JSON ") else "bad_reply"
    if command.startswith("PROCESS SIM "):
        return "ok" if text.startswith("ACK_PROCESS_SIM_") else "bad_reply"
    if command == "PROCESS RESET":
        return "ok" if text.startswith("ACK_PROCESS_RESET") else "bad_reply"
    if command in {"PROCESS STATUS?", "IO SNAPSHOT?"}:
        return "ok" if text.startswith("{") else "bad_reply"
    return "ok"


def exchange_one(
    cfg: Settings,
    command: str,
    *,
    read_timeout_s: float,
    broker_wait_timeout_s: float | None,
    request_timeout_s: float,
    priority: bool,
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        reply, _payload = exchange_via_databridge(
            cfg,
            command,
            read_timeout_s=read_timeout_s,
            read_limit=65536,
            priority=priority,
            wait_timeout_s=broker_wait_timeout_s,
            request_timeout_s=request_timeout_s,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return {
            "class": classify_reply(command, reply),
            "command": command,
            "elapsed_ms": elapsed_ms,
            "reply": str(reply or "")[:200],
        }
    except urllib.error.HTTPError as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = str(exc)
        return {
            "class": "shed" if int(getattr(exc, "code", 0) or 0) == 429 else "exception",
            "command": command,
            "elapsed_ms": elapsed_ms,
            "reply": detail[:300],
        }
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return {
            "class": "exception",
            "command": command,
            "elapsed_ms": elapsed_ms,
            "reply": repr(exc),
        }


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    classes: dict[str, int] = {}
    for result in results:
        key = str(result.get("class") or "unknown")
        classes[key] = classes.get(key, 0) + 1
    accepted = [float(r["elapsed_ms"]) for r in results if r.get("class") in {"ok", "shed"}]
    latency = {
        "min": min(accepted) if accepted else 0.0,
        "avg": statistics.mean(accepted) if accepted else 0.0,
        "p95": percentile(accepted, 95),
        "p99": percentile(accepted, 99),
        "max": max(accepted) if accepted else 0.0,
    }
    return {"total": len(results), "classes": classes, "latency_ms": latency}


def sample_failures(results: list[dict[str, Any]], limit: int = 12) -> list[dict[str, Any]]:
    return [r for r in results if r.get("class") not in {"ok", "shed"}][:limit]


def run_probe_worker(
    cfg: Settings,
    *,
    stop_at: float,
    worker_id: int,
    sleep_s: float,
    read_timeout_s: float,
    broker_wait_timeout_s: float | None,
    request_timeout_s: float,
    priority: bool,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    index = worker_id
    while time.monotonic() < stop_at:
        command = PROBE_COMMANDS[index % len(PROBE_COMMANDS)]
        results.append(
            exchange_one(
                cfg,
                command,
                read_timeout_s=read_timeout_s,
                broker_wait_timeout_s=broker_wait_timeout_s,
                request_timeout_s=request_timeout_s,
                priority=priority,
            )
        )
        index += 1
        if sleep_s > 0:
            time.sleep(sleep_s)
    return results


def run_sim_worker(
    cfg: Settings,
    *,
    stop_at: float,
    sleep_s: float,
    read_timeout_s: float,
    broker_wait_timeout_s: float | None,
    request_timeout_s: float,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    index = 0
    while time.monotonic() < stop_at:
        command = SIM_COMMANDS[index % len(SIM_COMMANDS)]
        results.append(
            exchange_one(
                cfg,
                command,
                read_timeout_s=read_timeout_s,
                broker_wait_timeout_s=broker_wait_timeout_s,
                request_timeout_s=request_timeout_s,
                priority=False,
            )
        )
        index += 1
        if sleep_s > 0:
            time.sleep(sleep_s)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Stress the ESP command broker through Databridge while the ESP process runtime "
            "receives simulated sensor/encoder events. No motion commands are sent."
        )
    )
    parser.add_argument("--config", default=DEFAULT_CFG_PATH)
    parser.add_argument("--duration-s", type=float, default=60.0)
    parser.add_argument("--probe-workers", type=int, default=4)
    parser.add_argument("--probe-sleep-ms", type=float, default=20.0)
    parser.add_argument("--sim-sleep-ms", type=float, default=15.0)
    parser.add_argument("--read-timeout", type=float, default=1.2)
    parser.add_argument("--broker-wait-timeout", type=float, default=8.0)
    parser.add_argument("--request-timeout", type=float, default=10.0)
    parser.add_argument("--priority-probes", action="store_true")
    parser.add_argument("--allow-active-machine", action="store_true")
    parser.add_argument("--no-reset-before", action="store_true")
    parser.add_argument("--no-reset-after", action="store_true")
    args = parser.parse_args()

    cfg = load_settings(args.config)
    machine_state = current_machine_state(cfg)
    if (
        machine_state is not None
        and machine_state not in SAFE_STANDSTILL_STATES
        and not bool(args.allow_active_machine)
    ):
        raise SystemExit(
            f"Refusing stress test in active machine state {machine_state}; "
            "use --allow-active-machine only for a deliberate dry-run window."
        )

    read_timeout_s = float(args.read_timeout or 1.2)
    broker_wait_timeout_s = float(args.broker_wait_timeout) if args.broker_wait_timeout is not None else None
    request_timeout_s = float(args.request_timeout or 3.5)
    reset_results: list[dict[str, Any]] = []
    if not bool(args.no_reset_before):
        reset_results.append(
            exchange_one(
                cfg,
                "PROCESS RESET",
                read_timeout_s=max(read_timeout_s, 2.0),
                broker_wait_timeout_s=max(broker_wait_timeout_s or 0.0, 8.0),
                request_timeout_s=max(request_timeout_s, 4.0),
                priority=True,
            )
        )

    duration_s = max(1.0, float(args.duration_s or 1.0))
    stop_at = time.monotonic() + duration_s
    all_results: list[dict[str, Any]] = []
    futures = []
    with ThreadPoolExecutor(max_workers=max(2, int(args.probe_workers or 1) + 1)) as pool:
        futures.append(
            pool.submit(
                run_sim_worker,
                cfg,
                stop_at=stop_at,
                sleep_s=max(0.0, float(args.sim_sleep_ms or 0.0) / 1000.0),
                read_timeout_s=read_timeout_s,
                broker_wait_timeout_s=broker_wait_timeout_s,
                request_timeout_s=request_timeout_s,
            )
        )
        for worker_id in range(max(1, int(args.probe_workers or 1))):
            futures.append(
                pool.submit(
                    run_probe_worker,
                    cfg,
                    stop_at=stop_at,
                    worker_id=worker_id,
                    sleep_s=max(0.0, float(args.probe_sleep_ms or 0.0) / 1000.0),
                    read_timeout_s=read_timeout_s,
                    broker_wait_timeout_s=broker_wait_timeout_s,
                    request_timeout_s=request_timeout_s,
                    priority=bool(args.priority_probes),
                )
            )
        for future in as_completed(futures):
            all_results.extend(future.result())

    if not bool(args.no_reset_after):
        reset_results.append(
            exchange_one(
                cfg,
                "PROCESS RESET",
                read_timeout_s=max(read_timeout_s, 2.0),
                broker_wait_timeout_s=max(broker_wait_timeout_s or 0.0, 8.0),
                request_timeout_s=max(request_timeout_s, 4.0),
                priority=True,
            )
        )

    output = {
        "ok": not sample_failures(all_results + reset_results, limit=1),
        "machine_state": machine_state,
        "duration_s": duration_s,
        "probe_workers": max(1, int(args.probe_workers or 1)),
        "reset": reset_results,
        "summary": summarize(all_results),
        "failures": sample_failures(all_results + reset_results),
    }
    print(json.dumps(output, indent=2, sort_keys=False))
    return 0 if output["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import socket
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from mas004_rpi_databridge.device_clients import EspPlcClient
except Exception:
    from mas004_rpi_databridge.device_clients import EspPlcClient

from esp_broker_api import exchange_via_databridge, load_settings


SAFE_COMMANDS = [
    ("PING", "PONG"),
    ("INFO", "INFO "),
    ("IP?", "IP="),
    ("STATUS?", "STATUS "),
    ("OUTBOUND STATUS?", "OUTBOUND "),
    ("MOTOR POLL?", "JSON "),
    ("PROCESS INDEXED STATUS?", "JSON "),
]


def raw_exchange(host: str, port: int, line: str, *, connect_timeout: float, read_timeout: float) -> str:
    with socket.create_connection((host, port), timeout=connect_timeout) as sock:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass
        sock.settimeout(read_timeout)
        sock.sendall((line.strip() + "\n").encode("utf-8"))
        data = bytearray()
        while not data.endswith(b"\n"):
            chunk = sock.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > 65536:
                raise RuntimeError("reply too long")
        return data.decode("utf-8", errors="replace").strip()


def command_for_index(index: int) -> tuple[str, str]:
    return SAFE_COMMANDS[index % len(SAFE_COMMANDS)]


def classify(command: str, expected: str, reply: str, allow_busy: bool) -> str:
    if allow_busy and reply.strip().upper() == "NAK_BUSY":
        return "busy"
    if expected != "PONG":
        return "ok" if reply.startswith(expected) else "bad_reply"
    return "ok" if reply == expected else "bad_reply"


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((p / 100.0) * (len(ordered) - 1)))))
    return ordered[idx]


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    latencies = [float(r["elapsed_ms"]) for r in results if r.get("class") in ("ok", "busy")]
    classes: dict[str, int] = {}
    for result in results:
        key = str(result.get("class") or "unknown")
        classes[key] = classes.get(key, 0) + 1
    return {
        "total": len(results),
        "classes": classes,
        "latency_ms": {
            "min": min(latencies) if latencies else 0.0,
            "avg": statistics.mean(latencies) if latencies else 0.0,
            "p95": percentile(latencies, 95),
            "p99": percentile(latencies, 99),
            "max": max(latencies) if latencies else 0.0,
        },
    }


def run_one(
    *,
    mode: str,
    cfg: Any | None,
    host: str,
    port: int,
    connect_timeout: float,
    read_timeout: float,
    index: int,
    allow_busy: bool,
) -> dict[str, Any]:
    command, expected = command_for_index(index)
    started = time.perf_counter()
    try:
        if mode == "api":
            if cfg is None:
                cfg = load_settings()
            reply, _payload = exchange_via_databridge(
                cfg,
                command,
                read_timeout_s=read_timeout,
                read_limit=65536,
                request_timeout_s=max(3.0, read_timeout + 2.0),
            )
        elif mode == "locked":
            client = EspPlcClient(host, port, timeout_s=connect_timeout)
            reply = client.exchange_line(command, read_timeout_s=read_timeout, read_limit=65536)
        else:
            reply = raw_exchange(host, port, command, connect_timeout=connect_timeout, read_timeout=read_timeout)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return {
            "class": classify(command, expected, reply, allow_busy=allow_busy),
            "command": command,
            "reply": reply[:200],
            "elapsed_ms": elapsed_ms,
        }
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return {
            "class": "exception",
            "command": command,
            "reply": repr(exc),
            "elapsed_ms": elapsed_ms,
        }


def run_serial(args: argparse.Namespace, mode: str) -> dict[str, Any]:
    results = [
        run_one(
            mode=mode,
            cfg=getattr(args, "cfg", None),
            host=args.host,
            port=args.port,
            connect_timeout=args.connect_timeout,
            read_timeout=args.read_timeout,
            index=i,
            allow_busy=args.allow_busy,
        )
        for i in range(args.iterations)
    ]
    return {"name": f"{mode}_serial", "summary": summarize(results), "samples": sample_failures(results)}


def run_parallel(args: argparse.Namespace, mode: str) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    total = args.workers * args.per_worker
    counter = 0
    counter_lock = threading.Lock()

    def _job() -> list[dict[str, Any]]:
        nonlocal counter
        local: list[dict[str, Any]] = []
        for _ in range(args.per_worker):
            with counter_lock:
                index = counter
                counter += 1
            local.append(
                run_one(
                    mode=mode,
                    cfg=getattr(args, "cfg", None),
                    host=args.host,
                    port=args.port,
                    connect_timeout=args.connect_timeout,
                    read_timeout=args.read_timeout,
                    index=index,
                    allow_busy=args.allow_busy,
                )
            )
            if args.worker_sleep_ms > 0:
                time.sleep(args.worker_sleep_ms / 1000.0)
        return local

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(_job) for _ in range(args.workers)]
        for future in as_completed(futures):
            results.extend(future.result())
    if len(results) != total:
        results.append({"class": "internal_error", "command": "", "reply": "missing results", "elapsed_ms": 0.0})
    return {"name": f"{mode}_parallel", "summary": summarize(results), "samples": sample_failures(results)}


def sample_failures(results: list[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    out = []
    for result in results:
        if result.get("class") not in ("ok", "busy"):
            out.append(result)
        if len(out) >= limit:
            break
    return out


def run_busy_probe(args: argparse.Namespace) -> dict[str, Any]:
    host = args.host
    port = args.port
    result: dict[str, Any] = {"name": "raw_busy_probe"}
    hold = socket.create_connection((host, port), timeout=args.connect_timeout)
    hold.settimeout(args.read_timeout)
    try:
        hold.sendall(b"STA")
        time.sleep(0.1)
        try:
            reply = raw_exchange(host, port, "PING", connect_timeout=args.connect_timeout, read_timeout=args.read_timeout)
        except Exception as exc:
            reply = repr(exc)
        time.sleep(args.line_timeout_wait_s)
        hold.close()
        recovery = raw_exchange(host, port, "PING", connect_timeout=args.connect_timeout, read_timeout=args.read_timeout)
        result["busy_reply"] = reply
        result["recovery_reply"] = recovery
        result["ok"] = reply == "NAK_Busy" and recovery == "PONG"
    finally:
        try:
            hold.close()
        except Exception:
            pass
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Non-invasive ESP command stress test. The default phase uses the local "
            "Databridge API/Broker; raw phases intentionally bypass production arbitration."
        )
    )
    parser.add_argument("--config", default="/etc/mas004_rpi_databridge/config.json")
    parser.add_argument("--host", default="192.168.2.101")
    parser.add_argument("--port", type=int, default=3010)
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--per-worker", type=int, default=100)
    parser.add_argument("--connect-timeout", type=float, default=1.5)
    parser.add_argument("--read-timeout", type=float, default=2.0)
    parser.add_argument("--worker-sleep-ms", type=float, default=0.0)
    parser.add_argument("--line-timeout-wait-s", type=float, default=2.0)
    parser.add_argument("--allow-busy", action="store_true")
    parser.add_argument(
        "--phase",
        choices=[
            "api-serial",
            "api-parallel",
            "raw-serial",
            "locked-serial",
            "locked-parallel",
            "raw-parallel",
            "busy-probe",
            "all",
        ],
        default="api-parallel",
    )
    args = parser.parse_args()
    args.cfg = load_settings(args.config)

    phases = []
    if args.phase in ("api-serial", "all"):
        phases.append(run_serial(args, "api"))
    if args.phase in ("api-parallel", "all"):
        phases.append(run_parallel(args, "api"))
    if args.phase in ("raw-serial", "all"):
        phases.append(run_serial(args, "raw"))
    if args.phase in ("locked-serial", "all"):
        phases.append(run_serial(args, "locked"))
    if args.phase in ("locked-parallel", "all"):
        phases.append(run_parallel(args, "locked"))
    if args.phase in ("raw-parallel", "all"):
        args.allow_busy = True
        phases.append(run_parallel(args, "raw"))
    if args.phase in ("busy-probe", "all"):
        phases.append(run_busy_probe(args))

    print(json.dumps({"ok": True, "phases": phases}, indent=2, sort_keys=False))

    failed = False
    for phase in phases:
        if phase.get("ok") is False:
            failed = True
        summary = phase.get("summary") or {}
        classes = summary.get("classes") or {}
        if classes.get("exception") or classes.get("bad_reply") or classes.get("internal_error"):
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

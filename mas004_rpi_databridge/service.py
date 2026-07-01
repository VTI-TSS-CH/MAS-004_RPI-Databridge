import time
import threading
import json
import os
import queue
import shutil
import signal
import sys
import traceback
import uuid
from typing import Optional

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request

from mas004_rpi_databridge.config import Settings, DEFAULT_CFG_PATH
from mas004_rpi_databridge.db import DB
from mas004_rpi_databridge.inbox import Inbox
from mas004_rpi_databridge.logstore import LogStore
from mas004_rpi_databridge.outbox import Outbox
from mas004_rpi_databridge.params import ParamStore
from mas004_rpi_databridge.router import Router
from mas004_rpi_databridge.http_client import HttpClient
from mas004_rpi_databridge.io_master import IoStore
from mas004_rpi_databridge.io_runtime import IoRuntime, _ensure_rpiplc, _read_raspi_input
from mas004_rpi_databridge.machine_runtime import MachineRuntime
from mas004_rpi_databridge.machine_semantics import BUTTON_INPUTS
from mas004_rpi_databridge.peers import (
    SenderLane,
    peer_request_headers,
    primary_peer_base_url,
    secondary_peer_base_url,
    sender_lanes,
    url_matches_peer_base,
)
from mas004_rpi_databridge.watchdog import Watchdog
from mas004_rpi_databridge.webui import build_app
from mas004_rpi_databridge.ntp_sync import ntp_loop
from mas004_rpi_databridge.device_clients import start_esp_command_broker
from mas004_rpi_databridge.esp_push_listener import EspPushListenerManager
from mas004_rpi_databridge.esp_motors import EspMotorClient
from mas004_rpi_databridge._vj6530_bridge import ZbcBridgeClient
from mas004_rpi_databridge.vj6530_async_policy import (
    VJ6530_ASYNC_RECONNECT_MIN_S,
    VJ6530_ASYNC_SESSION_S,
    vj6530_async_reconnect_delay_s,
)
from mas004_rpi_databridge.vj6530_async_listener import Vj6530AsyncListener
from mas004_rpi_databridge.vj6530_poller import Vj6530Poller
from mas004_rpi_databridge.vj6530_runtime import RUNTIME as VJ6530_RUNTIME

REPO_MASTER_IOS_XLSX = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "master_data",
    "SAR41-MAS-004_SPS_I-Os.xlsx",
)
FALLBACK_REPO_MASTER_IOS_XLSX = os.path.join(
    "/opt",
    "MAS-004_RPI-Databridge",
    "master_data",
    "SAR41-MAS-004_SPS_I-Os.xlsx",
)
PANEL_BUTTON_SAMPLE_INTERVAL_S = 0.05
PANEL_BUTTON_QUEUE_MAX = 100
PANEL_BUTTON_EVENT_QUEUE: queue.Queue[dict[str, object]] = queue.Queue(maxsize=PANEL_BUTTON_QUEUE_MAX)
SECONDARY_PEER_FAILURE_COOLDOWN_S = 15.0
SECONDARY_PEER_COOLDOWN_LOG_S = 30.0


def resolve_repo_master_ios_xlsx() -> str:
    for candidate in (REPO_MASTER_IOS_XLSX, FALLBACK_REPO_MASTER_IOS_XLSX):
        if candidate and os.path.exists(candidate):
            return candidate
    return ""


def require_device_shared_secret(x_shared_secret: Optional[str], cfg: Settings):
    if (cfg.shared_secret or "") and x_shared_secret != cfg.shared_secret:
        raise HTTPException(status_code=401, detail="Unauthorized (shared secret)")


def disable_global_esp_motor_polling(cfg: Settings) -> None:
    try:
        client = EspMotorClient(cfg)
        if client.available():
            result = client.set_poll(False)
            print(f"[MOTORS] global ESP motor auto-poll disabled: {result.get('reply')}", flush=True)
    except Exception as exc:
        print(f"[MOTORS] global ESP motor auto-poll disable skipped: {exc!r}", flush=True)


def start_global_esp_command_broker(cfg: Settings) -> None:
    if bool(getattr(cfg, "esp_simulation", False)):
        print("[ESP-BROKER] skipped: esp_simulation=true", flush=True)
        return
    host = str(getattr(cfg, "esp_host", "") or "").strip()
    port = int(getattr(cfg, "esp_port", 0) or 0)
    if not host or port <= 0:
        print("[ESP-BROKER] skipped: ESP endpoint missing", flush=True)
        return
    timeout_s = float(getattr(cfg, "esp_connect_timeout_s", 1.5) or 1.5)
    try:
        diag = start_esp_command_broker(host, port, timeout_s=timeout_s)
        if diag.get("warmup_error"):
            print(
                f"[ESP-BROKER] started host={host}:{port} warmup_error={diag.get('warmup_error')}",
                flush=True,
            )
        else:
            print(
                f"[ESP-BROKER] started host={host}:{port} persistent={diag.get('broker_supported')} "
                f"reply={diag.get('warmup_reply')}",
                flush=True,
            )
    except Exception as exc:
        print(f"[ESP-BROKER] startup skipped: {exc!r}", flush=True)


def install_thread_dump_signal() -> None:
    def _dump(_signum, _frame):
        try:
            frames = sys._current_frames()
            threads = {t.ident: t for t in threading.enumerate()}
            print("[THREAD-DUMP] begin", flush=True)
            for ident, frame in frames.items():
                thread = threads.get(ident)
                name = thread.name if thread is not None else "unknown"
                native_id = getattr(thread, "native_id", None) if thread is not None else None
                print(f"[THREAD-DUMP] thread name={name} ident={ident} native_id={native_id}", flush=True)
                for line in traceback.format_stack(frame):
                    for part in line.rstrip().splitlines():
                        print(f"[THREAD-DUMP]   {part}", flush=True)
            print("[THREAD-DUMP] end", flush=True)
        except Exception as exc:
            print(f"[THREAD-DUMP] failed: {exc!r}", flush=True)

    try:
        signal.signal(signal.SIGUSR1, _dump)
    except Exception as exc:
        print(f"[THREAD-DUMP] signal install skipped: {exc!r}", flush=True)


def build_device_inbox_app(cfg_path: str) -> FastAPI:
    app = FastAPI(title="MAS-004 Device Inbox", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    def health():
        return {"ok": True, "service": "device-inbox"}

    @app.post("/api/inbox")
    async def api_inbox(
        request: Request,
        x_idempotency_key: Optional[str] = Header(default=None),
        x_shared_secret: Optional[str] = Header(default=None),
    ):
        cfg = Settings.load(cfg_path)
        require_device_shared_secret(x_shared_secret, cfg)

        raw_body = await request.body()
        body = None
        if raw_body:
            try:
                body = json.loads(raw_body.decode("utf-8"))
            except Exception:
                txt = raw_body.decode("utf-8", errors="replace").strip()
                body = {"msg": txt} if txt else None

        headers = dict(request.headers)
        idem = x_idempotency_key or headers.get("x-idempotency-key") or str(uuid.uuid4())
        source = request.client.host if request.client else None
        if isinstance(body, dict):
            src = body.get("source")
            if isinstance(src, str) and src.strip():
                source = src.strip()

        inserted = Inbox(DB(cfg.db_path)).store(source, headers, body, idem)
        return {"ok": True, "stored": inserted, "idempotency_key": idem}

    return app


def device_inbox_http_loop(cfg_path: str):
    cfg = Settings.load(cfg_path)
    host = cfg.device_inbox_http_host or "0.0.0.0"
    port = int(cfg.device_inbox_http_port or 0)
    if port <= 0:
        return
    print(f"[DEVICE-INBOX] HTTP listener on {host}:{port}", flush=True)
    uvicorn.run(build_device_inbox_app(cfg_path), host=host, port=port, log_level="warning")


def backoff_s(retry_count: int, base: float, cap: float) -> float:
    n = min(retry_count, 10)
    return min(cap, base * (2 ** n))


def sender_loop(cfg_path: str, lane: SenderLane):
    while True:
        cfg = Settings.load(cfg_path)
        db = DB(cfg.db_path)
        outbox = Outbox(db)

        watchdog = None
        if lane.use_primary_watchdog:
            health_url = None
            primary_base = primary_peer_base_url(cfg)
            if cfg.peer_health_path and primary_base:
                health_url = primary_base + cfg.peer_health_path

            watchdog = Watchdog(
                host=cfg.peer_watchdog_host,
                interval_s=cfg.watchdog_interval_s,
                timeout_s=cfg.watchdog_timeout_s,
                down_after=cfg.watchdog_down_after,
                health_url=health_url,
                tls_verify=cfg.tls_verify
            )

        client = HttpClient(timeout_s=cfg.http_timeout_s, source_ip=cfg.eth0_source_ip, verify_tls=cfg.tls_verify)
        secondary_cooldown_until = 0.0
        secondary_cooldown_last_log = 0.0

        while True:
            if watchdog and not watchdog.tick():
                # Schnell erneut pruefen; Watchdog selbst drosselt intern ueber interval_s.
                time.sleep(0.2)
                continue

            job = outbox.claim_next_due(
                url_prefixes=lane.url_prefixes,
                exclude_url_prefixes=lane.exclude_url_prefixes,
                lease_s=max(30.0, float(cfg.http_timeout_s or 10.0) + 5.0),
            )
            if not job:
                time.sleep(0.35)
                continue

            secondary_base = secondary_peer_base_url(cfg)
            is_secondary_target = url_matches_peer_base(job.url or "", secondary_base)
            now_s = time.time()
            if is_secondary_target and now_s < secondary_cooldown_until:
                if now_s - secondary_cooldown_last_log >= SECONDARY_PEER_COOLDOWN_LOG_S:
                    remaining = int(max(0.0, secondary_cooldown_until - now_s))
                    print(
                        f"[OUTBOX:{lane.name}] drop secondary during cooldown "
                        f"id={job.id} remaining_s={remaining} url={job.url}",
                        flush=True,
                    )
                    secondary_cooldown_last_log = now_s
                outbox.delete(job.id)
                continue

            try:
                started_at = time.time()
                if not (job.url or "").lower().startswith(("http://", "https://")):
                    print(f"[OUTBOX:{lane.name}] drop id={job.id} invalid_url={job.url!r}", flush=True)
                    outbox.delete(job.id)
                    continue

                headers = peer_request_headers(cfg, job.url, json.loads(job.headers_json))
                body = json.loads(job.body_json) if job.body_json else None

                print(f"[OUTBOX:{lane.name}] send id={job.id} rc={job.retry_count} {job.method} {job.url}", flush=True)
                resp = client.request(job.method, job.url, headers, body)
                elapsed_ms = int((time.time() - started_at) * 1000)
                print(f"[OUTBOX:{lane.name}] ok   id={job.id} elapsed_ms={elapsed_ms} resp={resp}", flush=True)

                outbox.delete(job.id)

            except Exception as e:
                elapsed_ms = int((time.time() - started_at) * 1000)
                if is_secondary_target:
                    secondary_cooldown_until = time.time() + SECONDARY_PEER_FAILURE_COOLDOWN_S
                    secondary_cooldown_last_log = time.time()
                    print(
                        f"[OUTBOX:{lane.name}] drop secondary id={job.id} elapsed_ms={elapsed_ms} "
                        f"cooldown_s={int(SECONDARY_PEER_FAILURE_COOLDOWN_S)} "
                        f"err={repr(e)} url={job.url}",
                        flush=True,
                    )
                    outbox.delete(job.id)
                    continue

                rc = job.retry_count + 1
                next_ts = time.time() + backoff_s(rc, cfg.retry_base_s, cfg.retry_cap_s)
                print(
                    f"[OUTBOX:{lane.name}] FAIL id={job.id} rc={rc} next_in={int(next_ts-time.time())}s "
                    f"elapsed_ms={elapsed_ms} err={repr(e)}",
                    flush=True,
                )
                outbox.reschedule(job.id, rc, next_ts)


def router_loop(cfg_path: str):
    while True:
        try:
            cfg = Settings.load(cfg_path)
            db = DB(cfg.db_path)
            inbox = Inbox(db)
            outbox = Outbox(db)
            params = ParamStore(db)
            logs = LogStore(db)
            recovered = inbox.recover_stale_processing(max_age_s=300.0)
            if recovered:
                logs.log("raspi", "warning", f"stale inbox processing messages quarantined: {recovered}")
            router = Router(cfg, inbox, outbox, params, logs)
            threading.Thread(target=router.device_bridge.warm_device_caches, daemon=True).start()

            while True:
                did_work = router.tick_once()
                if not did_work:
                    time.sleep(0.25)
        except Exception as e:
            print(f"[ROUTER] loop error: {repr(e)}", flush=True)
            time.sleep(1.0)


def esp_push_listener_loop(cfg_path: str, push_mgr: EspPushListenerManager):
    while True:
        try:
            cfg = Settings.load(cfg_path)
            push_mgr.reconcile(cfg)
        except Exception as e:
            print(f"[ESP-PUSH] reconcile error: {repr(e)}", flush=True)
        time.sleep(5.0)


def _esp_io_poll_paused_for_critical_motion(db: DB) -> bool:
    try:
        with db._conn() as conn:
            row = conn.execute(
                "SELECT current_state, info_json FROM machine_state WHERE singleton_id=1"
            ).fetchone()
        if not row:
            return False
        if int(row[0] or 0) in (2, 3, 4, 5, 6):
            return True
        info = json.loads(row[1] or "{}")
        production = dict(info.get("production_runtime") or {})
        return bool(production.get("pending_start"))
    except Exception:
        return False


def io_runtime_loop(cfg_path: str):
    last_import_sig = None
    next_due_by_device: dict[str, float] = {}
    cfg = None
    io_store = None
    reload_due = 0.0
    while True:
        try:
            now_m = time.monotonic()
            if cfg is None or io_store is None or now_m >= reload_due:
                cfg = Settings.load(cfg_path)
                db = DB(cfg.db_path)
                io_store = IoStore(db)
                repo_master_ios_xlsx = resolve_repo_master_ios_xlsx()
                if not os.path.exists(cfg.master_ios_xlsx_path) and repo_master_ios_xlsx:
                    os.makedirs(os.path.dirname(cfg.master_ios_xlsx_path), exist_ok=True)
                    shutil.copyfile(repo_master_ios_xlsx, cfg.master_ios_xlsx_path)

                workbook_sig = (
                    cfg.master_ios_xlsx_path,
                    os.path.getmtime(cfg.master_ios_xlsx_path) if os.path.exists(cfg.master_ios_xlsx_path) else 0.0,
                )
                if io_store.count_points() == 0 or workbook_sig != last_import_sig:
                    if os.path.exists(cfg.master_ios_xlsx_path):
                        io_store.import_xlsx(cfg.master_ios_xlsx_path)
                        last_import_sig = workbook_sig
                reload_due = now_m + 2.0

            intervals = {
                "esp32_plc58": max(0.5, float(getattr(cfg, "esp_io_poll_interval_s", 1.0) or 1.0)),
                "raspi_plc21": max(0.5, float(getattr(cfg, "raspi_io_poll_interval_s", 1.0) or 1.0)),
                "moxa_e1211_1": max(0.5, float(getattr(cfg, "moxa_poll_interval_s", 1.0) or 1.0)),
                "moxa_e1211_2": max(0.5, float(getattr(cfg, "moxa_poll_interval_s", 1.0) or 1.0)),
                "moxa_e1213_1": max(0.5, float(getattr(cfg, "moxa_poll_interval_s", 1.0) or 1.0)),
                "moxa_e1213_2": max(0.5, float(getattr(cfg, "moxa_poll_interval_s", 1.0) or 1.0)),
                "moxa_e1213_3": max(0.5, float(getattr(cfg, "moxa_poll_interval_s", 1.0) or 1.0)),
            }
            due_devices = {
                device_code
                for device_code, interval_s in intervals.items()
                if now_m >= float(next_due_by_device.get(device_code, 0.0))
            }
            if "esp32_plc58" in due_devices and _esp_io_poll_paused_for_critical_motion(db):
                due_devices.discard("esp32_plc58")
                next_due_by_device["esp32_plc58"] = now_m + max(
                    2.0,
                    float(getattr(cfg, "esp_io_poll_interval_s", 1.0) or 1.0),
                )
            if due_devices:
                IoRuntime(cfg, io_store).refresh(include_points=False, device_codes=due_devices)
                for device_code in due_devices:
                    next_due_by_device[device_code] = now_m + intervals[device_code]
        except Exception as e:
            print(f"[IO] loop error: {repr(e)}", flush=True)

        time.sleep(0.2)


def _panel_button_sample(cfg: Settings) -> dict[str, bool]:
    if bool(getattr(cfg, "raspi_io_simulation", True)):
        raise RuntimeError("raspi IO simulation active")
    rpiplc, error = _ensure_rpiplc(str(getattr(cfg, "raspi_plc_model", "RPIPLC_21") or "RPIPLC_21"))
    if rpiplc is None:
        raise RuntimeError(f"rpiplc unavailable: {error}")
    current: dict[str, bool] = {}
    for button_name, (device_code, pin_label) in BUTTON_INPUTS.items():
        if device_code != "raspi_plc21":
            continue
        current[str(button_name)] = bool(_read_raspi_input(rpiplc, cfg, pin_label))
    return current


def _queue_panel_button_event(event: dict[str, object]) -> None:
    try:
        PANEL_BUTTON_EVENT_QUEUE.put_nowait(event)
        return
    except queue.Full:
        pass
    try:
        PANEL_BUTTON_EVENT_QUEUE.get_nowait()
    except queue.Empty:
        pass
    try:
        PANEL_BUTTON_EVENT_QUEUE.put_nowait(event)
    except queue.Full:
        pass


def button_sampler_loop(cfg_path: str):
    cfg = None
    reload_due = 0.0
    previous_inputs: dict[str, bool] | None = None
    last_error_log = 0.0
    while True:
        now_m = time.monotonic()
        try:
            if cfg is None or now_m >= reload_due:
                cfg = Settings.load(cfg_path)
                reload_due = now_m + 2.0
            if bool(getattr(cfg, "raspi_io_simulation", True)):
                previous_inputs = None
                time.sleep(0.5)
                continue
            current_inputs = _panel_button_sample(cfg)
            if previous_inputs is None:
                _queue_panel_button_event(
                    {
                        "ts": time.time(),
                        "current_inputs": dict(current_inputs),
                        "previous_inputs": dict(current_inputs),
                        "initial": True,
                    }
                )
                previous_inputs = dict(current_inputs)
            elif current_inputs != previous_inputs:
                _queue_panel_button_event(
                    {
                        "ts": time.time(),
                        "current_inputs": dict(current_inputs),
                        "previous_inputs": dict(previous_inputs),
                        "initial": False,
                    }
                )
                previous_inputs = dict(current_inputs)
        except Exception as e:
            previous_inputs = None
            if now_m - last_error_log >= 5.0:
                print(f"[BUTTON-SAMPLE] loop error: {repr(e)}", flush=True)
                last_error_log = now_m
            time.sleep(0.25)
            continue
        time.sleep(PANEL_BUTTON_SAMPLE_INTERVAL_S)


def machine_runtime_loop(cfg_path: str):
    runtime = None
    reload_due = 0.0
    tick_s = 0.50
    next_tick = time.monotonic()
    fast_states = {2, 3, 4, 5, 6, 10, 11, 12, 13, 16, 17}
    while True:
        try:
            now_m = time.monotonic()
            if runtime is None or now_m >= reload_due:
                cfg = Settings.load(cfg_path)
                db = DB(cfg.db_path)
                params = ParamStore(db)
                logs = LogStore(db)
                outbox = Outbox(db)
                io_store = IoStore(db)
                runtime = MachineRuntime(cfg, db, params, io_store, logs, outbox)
                reload_due = now_m + 5.0
            result = runtime.refresh(include_snapshot=False)
            current_state = int(result.get("current_state") or 0)
            if current_state in fast_states:
                tick_s = 0.50
            elif current_state in {20, 21}:
                tick_s = 0.75
            else:
                tick_s = 1.00
        except Exception as e:
            runtime = None
            print(f"[MACHINE] loop error: {repr(e)}", flush=True)
        next_tick += tick_s
        delay = next_tick - time.monotonic()
        if delay <= 0.0:
            next_tick = time.monotonic() + tick_s
            delay = tick_s
        time.sleep(max(0.0, delay))


def button_input_loop(cfg_path: str):
    runtime = None
    reload_due = 0.0
    while True:
        try:
            now_m = time.monotonic()
            if runtime is None or now_m >= reload_due:
                cfg = Settings.load(cfg_path)
                db = DB(cfg.db_path)
                params = ParamStore(db)
                logs = LogStore(db)
                outbox = Outbox(db)
                io_store = IoStore(db)
                runtime = MachineRuntime(cfg, db, params, io_store, logs, outbox)
                reload_due = now_m + 5.0
            event = PANEL_BUTTON_EVENT_QUEUE.get(timeout=0.5)
            runtime.process_physical_button_inputs(
                current_inputs=dict(event.get("current_inputs") or {}),
                previous_inputs=dict(event.get("previous_inputs") or {}),
                ts=float(event.get("ts") or time.time()),
            )
        except queue.Empty:
            continue
        except Exception as e:
            runtime = None
            print(f"[BUTTON-IN] loop error: {repr(e)}", flush=True)


def button_led_loop(cfg_path: str):
    runtime = None
    reload_due = 0.0
    tick_s = 0.25
    next_tick = time.monotonic()
    while True:
        try:
            now_m = time.monotonic()
            if runtime is None or now_m >= reload_due:
                cfg = Settings.load(cfg_path)
                db = DB(cfg.db_path)
                params = ParamStore(db)
                logs = LogStore(db)
                outbox = Outbox(db)
                io_store = IoStore(db)
                runtime = MachineRuntime(cfg, db, params, io_store, logs, outbox)
                reload_due = now_m + 5.0
            runtime.refresh_button_led_outputs(ts=now_m)
        except Exception as e:
            runtime = None
            print(f"[BUTTON-LED] loop error: {repr(e)}", flush=True)
        next_tick += tick_s
        delay = next_tick - time.monotonic()
        if delay <= 0.0:
            next_tick = time.monotonic() + tick_s
            delay = tick_s
        time.sleep(max(0.0, delay))


def vj6530_poll_loop(cfg_path: str):
    cached_client = None
    cached_sig = None
    while True:
        cfg = Settings.load(cfg_path)
        interval_s = max(0.5, float(getattr(cfg, "vj6530_poll_interval_s", 2.0) or 2.0))

        if bool(cfg.vj6530_simulation) or not (cfg.vj6530_host or "").strip() or int(cfg.vj6530_port or 0) <= 0:
            cached_client = None
            cached_sig = None
            time.sleep(interval_s)
            continue

        if bool(getattr(cfg, "vj6530_async_enabled", True)) and VJ6530_RUNTIME.async_recent(max(interval_s * 2.0, 5.0)):
            time.sleep(interval_s)
            continue

        client_sig = ((cfg.vj6530_host or "").strip(), int(cfg.vj6530_port or 0), float(cfg.http_timeout_s or 0.0))
        if cached_client is None or client_sig != cached_sig:
            cached_client = ZbcBridgeClient(cfg.vj6530_host, cfg.vj6530_port, timeout_s=cfg.http_timeout_s)
            cached_sig = client_sig

        try:
            db = DB(cfg.db_path)
            params = ParamStore(db)
            logs = LogStore(db)
            outbox = Outbox(db)
            poller = Vj6530Poller(cfg, params, logs, outbox, client_factory=lambda *_args, **_kwargs: cached_client)
            result = poller.poll_once()
            if result.get("changed"):
                print(
                    f"[VJ6530-POLL] checked={result.get('checked', 0)} changed={result.get('changed', 0)} "
                    f"forwarded={result.get('forwarded', 0)}",
                    flush=True,
                )
        except Exception as e:
            cached_client = None
            cached_sig = None
            print(f"[VJ6530-POLL] error: {repr(e)}", flush=True)
            time.sleep(min(interval_s, 2.0))
            continue

        time.sleep(interval_s)


def vj6530_async_loop(cfg_path: str):
    error_backoff_s = 2.0
    while True:
        cfg = Settings.load(cfg_path)
        if (
            not bool(getattr(cfg, "vj6530_async_enabled", True))
            or bool(cfg.vj6530_simulation)
            or not (cfg.vj6530_host or "").strip()
            or int(cfg.vj6530_port or 0) <= 0
        ):
            error_backoff_s = 2.0
            time.sleep(1.0)
            continue

        try:
            db = DB(cfg.db_path)
            params = ParamStore(db)
            logs = LogStore(db)
            outbox = Outbox(db)
            listener = Vj6530AsyncListener(cfg, params, logs, outbox)
            listener.run_session(session_s=VJ6530_ASYNC_SESSION_S)
            error_backoff_s = 2.0
        except Exception as e:
            detail = repr(e)
            reconnect_delay_s = vj6530_async_reconnect_delay_s(e, error_backoff_s)
            VJ6530_RUNTIME.mark_async_error(detail)
            if reconnect_delay_s <= VJ6530_ASYNC_RECONNECT_MIN_S:
                print(f"[VJ6530-ASYNC] reconnect: {detail}", flush=True)
                error_backoff_s = 2.0
            else:
                print(f"[VJ6530-ASYNC] error: {detail}", flush=True)
                error_backoff_s = min(error_backoff_s * 1.8, 30.0)
            time.sleep(reconnect_delay_s)


def main():
    install_thread_dump_signal()
    cfg_path = DEFAULT_CFG_PATH
    cfg = Settings.load(cfg_path)
    repo_master_ios_xlsx = resolve_repo_master_ios_xlsx()
    if not os.path.exists(cfg.master_ios_xlsx_path) and repo_master_ios_xlsx:
        os.makedirs(os.path.dirname(cfg.master_ios_xlsx_path), exist_ok=True)
        shutil.copyfile(repo_master_ios_xlsx, cfg.master_ios_xlsx_path)
    try:
        io_store = IoStore(DB(cfg.db_path))
        if io_store.count_points() == 0 and os.path.exists(cfg.master_ios_xlsx_path):
            io_store.import_xlsx(cfg.master_ios_xlsx_path)
    except Exception as e:
        print(f"[IO] startup import skipped: {repr(e)}", flush=True)

    start_global_esp_command_broker(cfg)
    disable_global_esp_motor_polling(cfg)

    ntp_t = threading.Thread(target=ntp_loop, args=(cfg_path,), daemon=True, name="ntp-loop")
    ntp_t.start()

    for lane in sender_lanes(cfg):
        sender_t = threading.Thread(target=sender_loop, args=(cfg_path, lane), daemon=True, name=f"sender-{lane.name}")
        sender_t.start()
    router_t = threading.Thread(target=router_loop, args=(cfg_path,), daemon=True, name="router-loop")
    router_t.start()

    push_mgr = EspPushListenerManager(cfg)
    try:
        push_mgr.start()
    except Exception as e:
        print(f"[ESP-PUSH] manager error: {repr(e)}", flush=True)
    push_t = threading.Thread(target=esp_push_listener_loop, args=(cfg_path, push_mgr), daemon=True, name="esp-push-reconcile")
    push_t.start()

    vj6530_async_t = threading.Thread(target=vj6530_async_loop, args=(cfg_path,), daemon=True, name="vj6530-async")
    vj6530_async_t.start()
    if (
        bool(getattr(cfg, "vj6530_async_enabled", True))
        and not bool(cfg.vj6530_simulation)
        and (cfg.vj6530_host or "").strip()
        and int(cfg.vj6530_port or 0) > 0
    ):
        # Give the async subscriber a short head start so the fallback poller
        # does not race the first live 3002 session during service startup.
        time.sleep(1.0)
    vj6530_poll_t = threading.Thread(target=vj6530_poll_loop, args=(cfg_path,), daemon=True, name="vj6530-poll")
    vj6530_poll_t.start()
    io_t = threading.Thread(target=io_runtime_loop, args=(cfg_path,), daemon=True, name="io-runtime")
    io_t.start()
    machine_t = threading.Thread(target=machine_runtime_loop, args=(cfg_path,), daemon=True, name="machine-runtime")
    machine_t.start()
    button_sampler_t = threading.Thread(target=button_sampler_loop, args=(cfg_path,), daemon=True, name="button-sampler")
    button_sampler_t.start()
    button_input_t = threading.Thread(target=button_input_loop, args=(cfg_path,), daemon=True, name="button-input")
    button_input_t.start()
    button_led_t = threading.Thread(target=button_led_loop, args=(cfg_path,), daemon=True, name="button-led")
    button_led_t.start()

    app = build_app(cfg_path)
    app.state.esp_push_listener_manager = push_mgr

    if bool(getattr(cfg, "device_inbox_http_enabled", True)) and int(getattr(cfg, "device_inbox_http_port", 0) or 0) > 0:
        if int(cfg.device_inbox_http_port) == int(cfg.webui_port):
            print("[DEVICE-INBOX] skipped: device_inbox_http_port equals webui_port", flush=True)
        else:
            device_inbox_t = threading.Thread(target=device_inbox_http_loop, args=(cfg_path,), daemon=True, name="device-inbox-http")
            device_inbox_t.start()

    ssl_kwargs = {}
    if cfg.webui_https:
        # Nur aktivieren, wenn Dateien existieren – sonst klare Fehlermeldung
        if not (os.path.exists(cfg.webui_ssl_certfile) and os.path.exists(cfg.webui_ssl_keyfile)):
            raise RuntimeError(
                f"HTTPS aktiviert, aber Zertifikat/Key fehlt: cert={cfg.webui_ssl_certfile} key={cfg.webui_ssl_keyfile}"
            )
        ssl_kwargs = {
            "ssl_certfile": cfg.webui_ssl_certfile,
            "ssl_keyfile": cfg.webui_ssl_keyfile,
        }

    uvicorn.run(app, host=cfg.webui_host, port=cfg.webui_port, log_level="info", **ssl_kwargs)

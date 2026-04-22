import time
import threading
import json
import os
import shutil
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
from mas004_rpi_databridge.io_runtime import IoRuntime
from mas004_rpi_databridge.machine_runtime import MachineRuntime
from mas004_rpi_databridge.peers import (
    SenderLane,
    primary_peer_base_url,
    secondary_peer_base_url,
    sender_lanes,
    url_matches_peer_base,
)
from mas004_rpi_databridge.watchdog import Watchdog
from mas004_rpi_databridge.webui import build_app
from mas004_rpi_databridge.ntp_sync import ntp_loop
from mas004_rpi_databridge.esp_push_listener import EspPushListenerManager
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


def resolve_repo_master_ios_xlsx() -> str:
    for candidate in (REPO_MASTER_IOS_XLSX, FALLBACK_REPO_MASTER_IOS_XLSX):
        if candidate and os.path.exists(candidate):
            return candidate
    return ""


def require_device_shared_secret(x_shared_secret: Optional[str], cfg: Settings):
    if (cfg.shared_secret or "") and x_shared_secret != cfg.shared_secret:
        raise HTTPException(status_code=401, detail="Unauthorized (shared secret)")


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

        while True:
            if watchdog and not watchdog.tick():
                # Schnell erneut pruefen; Watchdog selbst drosselt intern ueber interval_s.
                time.sleep(0.2)
                continue

            job = outbox.next_due(
                url_prefixes=lane.url_prefixes,
                exclude_url_prefixes=lane.exclude_url_prefixes,
            )
            if not job:
                time.sleep(0.05)
                continue

            secondary_base = secondary_peer_base_url(cfg)
            is_secondary_target = url_matches_peer_base(job.url or "", secondary_base)

            try:
                started_at = time.time()
                if not (job.url or "").lower().startswith(("http://", "https://")):
                    print(f"[OUTBOX:{lane.name}] drop id={job.id} invalid_url={job.url!r}", flush=True)
                    outbox.delete(job.id)
                    continue

                headers = json.loads(job.headers_json)
                body = json.loads(job.body_json) if job.body_json else None

                print(f"[OUTBOX:{lane.name}] send id={job.id} rc={job.retry_count} {job.method} {job.url}", flush=True)
                resp = client.request(job.method, job.url, headers, body)
                elapsed_ms = int((time.time() - started_at) * 1000)
                print(f"[OUTBOX:{lane.name}] ok   id={job.id} elapsed_ms={elapsed_ms} resp={resp}", flush=True)

                outbox.delete(job.id)

            except Exception as e:
                elapsed_ms = int((time.time() - started_at) * 1000)
                if is_secondary_target:
                    print(
                        f"[OUTBOX:{lane.name}] drop secondary id={job.id} elapsed_ms={elapsed_ms} "
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
            router = Router(cfg, inbox, outbox, params, logs)
            threading.Thread(target=router.device_bridge.warm_device_caches, daemon=True).start()

            while True:
                did_work = router.tick_once()
                if not did_work:
                    time.sleep(0.02)
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


def io_runtime_loop(cfg_path: str):
    last_import_sig = None
    while True:
        cfg = Settings.load(cfg_path)
        try:
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

            IoRuntime(cfg, io_store).refresh()
        except Exception as e:
            print(f"[IO] loop error: {repr(e)}", flush=True)

        poll_s = max(
            0.5,
            min(
                float(getattr(cfg, "raspi_io_poll_interval_s", 1.0) or 1.0),
                float(getattr(cfg, "esp_io_poll_interval_s", 1.0) or 1.0),
                float(getattr(cfg, "moxa_poll_interval_s", 1.0) or 1.0),
            ),
        )
        time.sleep(poll_s)


def machine_runtime_loop(cfg_path: str):
    while True:
        cfg = Settings.load(cfg_path)
        try:
            db = DB(cfg.db_path)
            params = ParamStore(db)
            logs = LogStore(db)
            outbox = Outbox(db)
            io_store = IoStore(db)
            runtime = MachineRuntime(cfg, db, params, io_store, logs, outbox)
            runtime.refresh()
        except Exception as e:
            print(f"[MACHINE] loop error: {repr(e)}", flush=True)
        time.sleep(0.5)


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

    ntp_t = threading.Thread(target=ntp_loop, args=(cfg_path,), daemon=True)
    ntp_t.start()

    for lane in sender_lanes(cfg):
        sender_t = threading.Thread(target=sender_loop, args=(cfg_path, lane), daemon=True)
        sender_t.start()
    router_t = threading.Thread(target=router_loop, args=(cfg_path,), daemon=True)
    router_t.start()

    push_mgr = EspPushListenerManager(cfg)
    try:
        push_mgr.start()
    except Exception as e:
        print(f"[ESP-PUSH] manager error: {repr(e)}", flush=True)
    push_t = threading.Thread(target=esp_push_listener_loop, args=(cfg_path, push_mgr), daemon=True)
    push_t.start()

    vj6530_async_t = threading.Thread(target=vj6530_async_loop, args=(cfg_path,), daemon=True)
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
    vj6530_poll_t = threading.Thread(target=vj6530_poll_loop, args=(cfg_path,), daemon=True)
    vj6530_poll_t.start()
    io_t = threading.Thread(target=io_runtime_loop, args=(cfg_path,), daemon=True)
    io_t.start()
    machine_t = threading.Thread(target=machine_runtime_loop, args=(cfg_path,), daemon=True)
    machine_t.start()

    app = build_app(cfg_path)
    app.state.esp_push_listener_manager = push_mgr

    if bool(getattr(cfg, "device_inbox_http_enabled", True)) and int(getattr(cfg, "device_inbox_http_port", 0) or 0) > 0:
        if int(cfg.device_inbox_http_port) == int(cfg.webui_port):
            print("[DEVICE-INBOX] skipped: device_inbox_http_port equals webui_port", flush=True)
        else:
            device_inbox_t = threading.Thread(target=device_inbox_http_loop, args=(cfg_path,), daemon=True)
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

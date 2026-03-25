import time
import threading
import json
import os
import uvicorn

from mas004_rpi_databridge.config import Settings, DEFAULT_CFG_PATH
from mas004_rpi_databridge.db import DB
from mas004_rpi_databridge.inbox import Inbox
from mas004_rpi_databridge.logstore import LogStore
from mas004_rpi_databridge.outbox import Outbox
from mas004_rpi_databridge.params import ParamStore
from mas004_rpi_databridge.router import Router
from mas004_rpi_databridge.http_client import HttpClient
from mas004_rpi_databridge.watchdog import Watchdog
from mas004_rpi_databridge.webui import build_app
from mas004_rpi_databridge.ntp_sync import ntp_loop
from mas004_rpi_databridge.esp_push_listener import EspPushListenerManager
from mas004_rpi_databridge.tcp_forwarder import TcpForwarderManager
from mas004_rpi_databridge._vj6530_bridge import ZbcBridgeClient
from mas004_rpi_databridge.vj6530_async_policy import (
    VJ6530_ASYNC_RECONNECT_MIN_S,
    VJ6530_ASYNC_SESSION_S,
    vj6530_async_reconnect_delay_s,
)
from mas004_rpi_databridge.vj6530_async_listener import Vj6530AsyncListener
from mas004_rpi_databridge.vj6530_poller import Vj6530Poller
from mas004_rpi_databridge.vj6530_runtime import RUNTIME as VJ6530_RUNTIME

def backoff_s(retry_count: int, base: float, cap: float) -> float:
    n = min(retry_count, 10)
    return min(cap, base * (2 ** n))

def sender_loop(cfg_path: str):
    while True:
        cfg = Settings.load(cfg_path)
        db = DB(cfg.db_path)
        outbox = Outbox(db)

        health_url = None
        if cfg.peer_health_path:
            health_url = cfg.peer_base_url.rstrip("/") + cfg.peer_health_path

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
            up = watchdog.tick()
            if not up:
                # Schnell erneut pruefen; Watchdog selbst drosselt intern ueber interval_s.
                time.sleep(0.2)
                continue

            job = outbox.next_due()
            if not job:
                time.sleep(0.05)
                continue

            secondary_base = (getattr(cfg, "peer_base_url_secondary", "") or "").strip().rstrip("/")
            is_secondary_target = bool(secondary_base) and (job.url or "").startswith(secondary_base + "/")

            try:
                if not (job.url or "").lower().startswith(("http://", "https://")):
                    print(f"[OUTBOX] drop id={job.id} invalid_url={job.url!r}", flush=True)
                    outbox.delete(job.id)
                    continue

                headers = json.loads(job.headers_json)
                body = json.loads(job.body_json) if job.body_json else None

                print(f"[OUTBOX] send id={job.id} rc={job.retry_count} {job.method} {job.url}", flush=True)
                resp = client.request(job.method, job.url, headers, body)
                print(f"[OUTBOX] ok   id={job.id} resp={resp}", flush=True)

                outbox.delete(job.id)

            except Exception as e:
                if is_secondary_target:
                    print(
                        f"[OUTBOX] drop secondary id={job.id} err={repr(e)} url={job.url}",
                        flush=True,
                    )
                    outbox.delete(job.id)
                    continue

                rc = job.retry_count + 1
                next_ts = time.time() + backoff_s(rc, cfg.retry_base_s, cfg.retry_cap_s)
                print(f"[OUTBOX] FAIL id={job.id} rc={rc} next_in={int(next_ts-time.time())}s err={repr(e)}", flush=True)
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


def forwarder_loop(cfg_path: str, fwd_mgr: TcpForwarderManager):
    while True:
        try:
            cfg = Settings.load(cfg_path)
            fwd_mgr.reconcile(cfg)
        except Exception as e:
            print(f"[FWD] reconcile error: {repr(e)}", flush=True)
        time.sleep(5.0)


def esp_push_listener_loop(cfg_path: str, push_mgr: EspPushListenerManager):
    while True:
        try:
            cfg = Settings.load(cfg_path)
            push_mgr.reconcile(cfg)
        except Exception as e:
            print(f"[ESP-PUSH] reconcile error: {repr(e)}", flush=True)
        time.sleep(5.0)


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

    ntp_t = threading.Thread(target=ntp_loop, args=(cfg_path,), daemon=True)
    ntp_t.start()

    sender_t = threading.Thread(target=sender_loop, args=(cfg_path,), daemon=True)
    sender_t.start()
    router_t = threading.Thread(target=router_loop, args=(cfg_path,), daemon=True)
    router_t.start()

    fwd_mgr = TcpForwarderManager(cfg)
    try:
        fwd_mgr.start()
    except Exception as e:
        print(f"[FWD] manager error: {repr(e)}", flush=True)
    fwd_t = threading.Thread(target=forwarder_loop, args=(cfg_path, fwd_mgr), daemon=True)
    fwd_t.start()

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

    app = build_app(cfg_path)
    app.state.tcp_forwarder_manager = fwd_mgr
    app.state.esp_push_listener_manager = push_mgr

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

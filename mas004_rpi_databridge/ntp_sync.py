from __future__ import annotations

import shutil
import subprocess
import time
from typing import Tuple

from mas004_rpi_databridge.config import Settings


def _run(cmd: list[str], timeout_s: int = 30) -> Tuple[bool, str]:
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except Exception as e:
        return False, repr(e)

    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    if p.returncode == 0:
        msg = out or err or "ok"
        return True, msg
    msg = err or out or f"rc={p.returncode}"
    return False, msg


def _find_cmd(name: str) -> str:
    p = shutil.which(name)
    if p:
        return p
    for base in ("/usr/sbin", "/sbin", "/usr/bin", "/bin"):
        candidate = f"{base}/{name}"
        if shutil.which(candidate):
            return candidate
    return ""


def sync_once(server: str) -> Tuple[bool, str]:
    s = (server or "").strip()
    if not s:
        return False, "ntp_server empty"

    ntpdate_cmd = _find_cmd("ntpdate")
    if ntpdate_cmd:
        ok, msg = _run([ntpdate_cmd, "-u", s], timeout_s=30)
        if ok:
            return True, f"ntpdate: {msg}"

    busybox_cmd = _find_cmd("busybox")
    if busybox_cmd:
        ok, msg = _run([busybox_cmd, "ntpd", "-q", "-n", "-p", s], timeout_s=40)
        if ok:
            return True, f"busybox ntpd: {msg}"

    sntp_cmd = _find_cmd("sntp")
    if sntp_cmd:
        # Platform dependent options; try conservative form.
        ok, msg = _run([sntp_cmd, "-sS", s], timeout_s=30)
        if ok:
            return True, f"sntp: {msg}"

    return False, "No supported NTP client found (ntpdate/busybox/sntp)"


def ntp_loop(cfg_path: str):
    last_sig = None
    while True:
        cfg = Settings.load(cfg_path)
        server = (getattr(cfg, "ntp_server", "") or "").strip()

        try:
            interval_min = int(getattr(cfg, "ntp_sync_interval_min", 60) or 60)
        except Exception:
            interval_min = 60
        interval_min = max(1, min(24 * 60, interval_min))

        sig = (server, interval_min)
        if sig != last_sig:
            if server:
                print(f"[NTP] configured server={server} interval={interval_min}min", flush=True)
            else:
                print("[NTP] disabled (ntp_server empty)", flush=True)
            last_sig = sig

        if server:
            ok, msg = sync_once(server)
            if ok:
                print(f"[NTP] sync ok server={server} msg={msg}", flush=True)
            else:
                print(f"[NTP] sync FAIL server={server} msg={msg}", flush=True)

        time.sleep(interval_min * 60)

from __future__ import annotations

import argparse
import ipaddress
import json
import shlex
import shutil
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


DEFAULT_PORTS = [22, 80, 443, 8080]


def json_print(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=True, indent=2, sort_keys=True))


def parse_ports(raw: str) -> list[int]:
    ports: list[int] = []
    for part in str(raw or "").split(","):
        text = part.strip()
        if not text:
            continue
        value = int(text)
        if value <= 0 or value > 65535:
            raise argparse.ArgumentTypeError(f"invalid port '{text}'")
        ports.append(value)
    return ports or list(DEFAULT_PORTS)


def probe_host(host: str, ports: list[int], timeout_s: float) -> dict[str, Any]:
    open_ports: list[int] = []
    errors: dict[str, str] = {}
    for port in ports:
        try:
            with socket.create_connection((host, int(port)), timeout=timeout_s):
                open_ports.append(int(port))
        except Exception as exc:
            errors[str(port)] = str(exc)
    return {
        "host": host,
        "reachable": bool(open_ports),
        "open_ports": open_ports,
        "errors": errors,
    }


def discover_hosts(subnet: str, ports: list[int], timeout_s: float, workers: int) -> list[dict[str, Any]]:
    net = ipaddress.ip_network(subnet, strict=False)
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(probe_host, str(host), ports, timeout_s): str(host) for host in net.hosts()}
        for future in as_completed(futures):
            payload = future.result()
            if payload["reachable"]:
                results.append(payload)
    results.sort(key=lambda item: tuple(int(part) for part in item["host"].split(".")))
    return results


def tool_path(explicit: str | None, name: str) -> str:
    candidate = explicit or shutil.which(name)
    if not candidate:
        raise RuntimeError(f"Required tool not found: {name}")
    return candidate


def run_command(
    cmd: list[str],
    *,
    capture_output: bool = True,
    check: bool = True,
    cwd: str | None = None,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        capture_output=capture_output,
        check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"Command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
        )
    return proc


def ssh_run(
    ssh_bin: str,
    target: str,
    command: str,
    *,
    tty: bool = False,
    capture_output: bool = True,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    cmd = [ssh_bin]
    if tty:
        cmd.append("-tt")
    cmd.extend([target, command])
    return run_command(cmd, capture_output=capture_output, check=check)


def scp_copy(scp_bin: str, local_paths: list[str], target: str, remote_dir: str) -> subprocess.CompletedProcess[str]:
    destination = f"{target}:{remote_dir.rstrip('/')}/"
    cmd = [scp_bin, *local_paths, destination]
    return run_command(cmd, capture_output=True, check=True)


def ensure_remote_dir(ssh_bin: str, target: str, remote_dir: str) -> None:
    ssh_run(ssh_bin, target, f"mkdir -p {shlex.quote(remote_dir)}", tty=False)


def bundled_restore_script() -> str:
    path = Path(__file__).with_name("mas004_restore_backup.py")
    if not path.exists():
        raise RuntimeError(f"Restore script not found next to bootstrap helper: {path}")
    return str(path)


def apply_backup(args: argparse.Namespace, backup_kind: str) -> dict[str, Any]:
    bundle = Path(args.bundle).resolve()
    if not bundle.exists():
        raise RuntimeError(f"Backup bundle not found: {bundle}")

    restore_script = bundled_restore_script()
    remote_dir = args.remote_dir or f"/tmp/mas004-bootstrap-{int(time.time())}"
    remote_bundle = f"{remote_dir.rstrip('/')}/{bundle.name}"
    remote_restore = f"{remote_dir.rstrip('/')}/mas004_restore_backup.py"
    service_name = args.service_name.strip()

    restore_cmd = [
        shlex.quote(args.remote_python),
        shlex.quote(remote_restore),
        "--bundle",
        shlex.quote(remote_bundle),
        "--cfg-path",
        shlex.quote(args.cfg_path),
        "--db-path",
        shlex.quote(args.db_path),
        "--master-params-path",
        shlex.quote(args.master_params_path),
        "--master-ios-path",
        shlex.quote(args.master_ios_path),
        "--repo-root",
        shlex.quote(args.repo_root),
    ]
    if backup_kind == "full" and args.apply_repos:
        restore_cmd.append("--apply-repos")
    remote_restore_cmd = "sudo " + " ".join(restore_cmd) if args.use_sudo else " ".join(restore_cmd)

    planned_commands: list[str] = [
        f"mkdir -p {shlex.quote(remote_dir)}",
        f"scp {shlex.quote(str(bundle))} {shlex.quote(restore_script)} {args.target}:{shlex.quote(remote_dir.rstrip('/'))}/",
    ]
    if args.stop_service and service_name:
        planned_commands.append(f"sudo systemctl stop {shlex.quote(service_name)}")
    planned_commands.append(remote_restore_cmd)
    if args.restart_service and service_name:
        planned_commands.append(
            f"sudo systemctl restart {shlex.quote(service_name)} && sudo systemctl is-active {shlex.quote(service_name)}"
        )

    if args.dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "kind": backup_kind,
            "target": args.target,
            "bundle": str(bundle),
            "remote_dir": remote_dir,
            "remote_bundle": remote_bundle,
            "remote_restore_script": remote_restore,
            "service_name": service_name,
            "planned_commands": planned_commands,
            "steps": [],
        }

    ssh_bin = tool_path(args.ssh_cmd, "ssh")
    scp_bin = tool_path(args.scp_cmd, "scp")
    ensure_remote_dir(ssh_bin, args.target, remote_dir)
    scp_copy(scp_bin, [str(bundle), restore_script], args.target, remote_dir)

    remote_steps: list[dict[str, Any]] = []
    if args.stop_service and service_name:
        proc = ssh_run(
            ssh_bin,
            args.target,
            f"sudo systemctl stop {shlex.quote(service_name)}",
            tty=bool(args.tty),
            capture_output=not bool(args.tty),
        )
        remote_steps.append({"step": "stop_service", "returncode": proc.returncode})

    proc = ssh_run(
        ssh_bin,
        args.target,
        remote_restore_cmd,
        tty=bool(args.tty),
        capture_output=not bool(args.tty),
    )
    remote_steps.append(
        {
            "step": "restore",
            "returncode": proc.returncode,
            "stdout": proc.stdout if not args.tty else "",
            "stderr": proc.stderr if not args.tty else "",
        }
    )

    if args.restart_service and service_name:
        proc = ssh_run(
            ssh_bin,
            args.target,
            f"sudo systemctl restart {shlex.quote(service_name)} && sudo systemctl is-active {shlex.quote(service_name)}",
            tty=bool(args.tty),
            capture_output=not bool(args.tty),
        )
        remote_steps.append(
            {
                "step": "restart_service",
                "returncode": proc.returncode,
                "stdout": proc.stdout if not args.tty else "",
                "stderr": proc.stderr if not args.tty else "",
            }
        )

    result = {
        "ok": True,
        "kind": backup_kind,
        "target": args.target,
        "bundle": str(bundle),
        "remote_dir": remote_dir,
        "remote_bundle": remote_bundle,
        "remote_restore_script": remote_restore,
        "service_name": service_name,
        "planned_commands": planned_commands,
        "steps": remote_steps,
    }
    return result


def add_apply_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser], name: str, backup_kind: str) -> None:
    parser = subparsers.add_parser(name, help=f"Apply a {backup_kind} backup bundle to a target Raspberry over SSH")
    parser.add_argument("--target", required=True, help="SSH target, e.g. pi@192.168.210.20")
    parser.add_argument("--bundle", required=True, help="Path to the exported backup bundle (.zip)")
    parser.add_argument("--remote-dir", default="", help="Temporary remote staging directory")
    parser.add_argument("--ssh-cmd", default="", help="Explicit ssh binary to use")
    parser.add_argument("--scp-cmd", default="", help="Explicit scp binary to use")
    parser.add_argument("--remote-python", default="python3", help="Python executable on the target host")
    parser.add_argument("--cfg-path", default="/etc/mas004_rpi_databridge/config.json")
    parser.add_argument("--db-path", default="/var/lib/mas004_rpi_databridge/databridge.db")
    parser.add_argument("--master-params-path", default="/var/lib/mas004_rpi_databridge/master/Parameterliste_master.xlsx")
    parser.add_argument("--master-ios-path", default="/var/lib/mas004_rpi_databridge/master/SAR41-MAS-004_SPS_I-Os.xlsx")
    parser.add_argument("--repo-root", default="/opt", help="Target root for repo snapshots during full restore")
    parser.add_argument("--service-name", default="mas004-rpi-databridge.service")
    parser.add_argument("--apply-repos", action="store_true", default=(backup_kind == "full"), help="Extract repo snapshots from the backup bundle")
    parser.add_argument("--restart-service", action="store_true", help="Restart the Databridge service after restore")
    parser.add_argument("--stop-service", action="store_true", default=True, help="Stop the Databridge service before restore")
    parser.add_argument("--no-stop-service", dest="stop_service", action="store_false", help="Do not stop the Databridge service before restore")
    parser.add_argument("--use-sudo", action="store_true", default=True, help="Prefix the remote restore command with sudo")
    parser.add_argument("--no-sudo", dest="use_sudo", action="store_false", help="Run the remote restore command without sudo")
    parser.add_argument("--tty", action="store_true", help="Allocate an SSH TTY so sudo can prompt if needed")
    parser.add_argument("--dry-run", action="store_true", help="Print the planned remote actions without executing them")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON output")
    parser.set_defaults(handler=lambda ns: apply_backup(ns, backup_kind))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="MAS-004 bootstrap helper for discovery and backup-based commissioning/cloning."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover = subparsers.add_parser("discover", help="Scan a subnet for likely MAS-004 hosts")
    discover.add_argument("--subnet", required=True, help="CIDR subnet, e.g. 192.168.210.0/24")
    discover.add_argument("--ports", default="22,80,443,8080", type=parse_ports, help="Comma-separated TCP ports to probe")
    discover.add_argument("--timeout", type=float, default=0.35, help="Socket timeout per probe in seconds")
    discover.add_argument("--workers", type=int, default=64, help="Parallel worker count")
    discover.add_argument("--json", action="store_true", help="Print machine-readable JSON output")
    discover.set_defaults(
        handler=lambda ns: {
            "ok": True,
            "subnet": ns.subnet,
            "ports": ns.ports,
            "matches": discover_hosts(ns.subnet, ns.ports, ns.timeout, ns.workers),
        }
    )

    add_apply_parser(subparsers, "apply-settings-backup", "settings")
    add_apply_parser(subparsers, "apply-full-backup", "full")

    args = parser.parse_args()
    result = args.handler(args)

    if getattr(args, "json", False):
        json_print(result)
    elif args.command == "discover":
        matches = result["matches"]
        if not matches:
            print("Keine erreichbaren Hosts im Zielsubnetz gefunden.")
        else:
            for item in matches:
                print(f"{item['host']}: offene Ports {', '.join(str(port) for port in item['open_ports'])}")
    else:
        print(f"Backup-Typ: {result['kind']}")
        print(f"Ziel:       {result['target']}")
        print(f"Bundle:     {result['bundle']}")
        print(f"Remote dir: {result['remote_dir']}")
        if result.get("dry_run"):
            print("Dry run:    ja")
            for command in result.get("planned_commands") or []:
                print(f"* {command}")
        for step in result["steps"]:
            print(f"- {step['step']}: rc={step['returncode']}")
            stdout = str(step.get("stdout") or "").strip()
            stderr = str(step.get("stderr") or "").strip()
            if stdout:
                print(stdout)
            if stderr:
                print(stderr, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

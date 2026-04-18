from __future__ import annotations

import json
import os
import socket
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from mas004_rpi_databridge.config import Settings
from mas004_rpi_databridge.db import DB, now_ts
from mas004_rpi_databridge.io_master import IoStore


@dataclass(frozen=True)
class CommissioningStepTemplate:
    step_id: str
    section_id: str
    title: str
    description: str
    kind: str = "manual"
    check_key: str = ""
    href: str = ""


STEP_TEMPLATES: List[CommissioningStepTemplate] = [
    CommissioningStepTemplate(
        "raspi_identity",
        "bootstrap",
        "Maschinenidentitaet",
        "Seriennummer und Maschinenname hinterlegen, damit Backups und Klonpakete eindeutig versioniert werden.",
        kind="manual",
        href="/ui/machine-setup/backups",
    ),
    CommissioningStepTemplate(
        "raspi_network",
        "bootstrap",
        "Netzwerkgrundlagen",
        "ETH0/ETH1, Gateways und Maschinen-IPs pruefen.",
        kind="auto",
        check_key="network",
        href="/ui/settings",
    ),
    CommissioningStepTemplate(
        "raspi_runtime",
        "bootstrap",
        "Raspi Runtime / Service",
        "Databridge-Service, DB und Workbook-Basis pruefen.",
        kind="auto",
        check_key="runtime",
        href="/ui/settings",
    ),
    CommissioningStepTemplate(
        "workbooks_loaded",
        "bootstrap",
        "Masterdaten geladen",
        "Parameterliste und IO-Workbook muessen auf dem Raspi vorhanden und importiert sein.",
        kind="auto",
        check_key="workbooks",
        href="/ui/params",
    ),
    CommissioningStepTemplate(
        "esp_endpoint",
        "devices",
        "ESP32-PLC58",
        "Erreichbarkeit bzw. Simulationszustand des ESP32 pruefen.",
        kind="auto",
        check_key="esp",
        href="/ui/machine-setup/io",
    ),
    CommissioningStepTemplate(
        "moxa1_endpoint",
        "devices",
        "Moxa Modul 1",
        "Direkte Modbus/TCP-Erreichbarkeit bzw. Simulationszustand des ersten Moxa pruefen.",
        kind="auto",
        check_key="moxa1",
        href="/ui/machine-setup/io",
    ),
    CommissioningStepTemplate(
        "moxa2_endpoint",
        "devices",
        "Moxa Modul 2",
        "Direkte Modbus/TCP-Erreichbarkeit bzw. Simulationszustand des zweiten Moxa pruefen.",
        kind="auto",
        check_key="moxa2",
        href="/ui/machine-setup/io",
    ),
    CommissioningStepTemplate(
        "vj6530_endpoint",
        "devices",
        "TTO Videojet 6530",
        "Erreichbarkeit bzw. Simulationszustand des TTO pruefen.",
        kind="auto",
        check_key="vj6530",
        href="/ui/settings",
    ),
    CommissioningStepTemplate(
        "vj3350_endpoint",
        "devices",
        "Laser Videojet 3350",
        "Erreichbarkeit bzw. Simulationszustand des Lasers pruefen.",
        kind="auto",
        check_key="vj3350",
        href="/ui/settings",
    ),
    CommissioningStepTemplate(
        "unwinder_endpoint",
        "devices",
        "Abwickler",
        "Smart-Wickler Abwickler pruefen oder bewusst simuliert belassen.",
        kind="auto",
        check_key="unwinder",
        href="/ui/machine-setup/winders/unwinder",
    ),
    CommissioningStepTemplate(
        "rewinder_endpoint",
        "devices",
        "Aufwickler",
        "Smart-Wickler Aufwickler pruefen oder bewusst simuliert belassen.",
        kind="auto",
        check_key="rewinder",
        href="/ui/machine-setup/winders/rewinder",
    ),
    CommissioningStepTemplate(
        "motor_setup",
        "motion",
        "Motorparameter",
        "Oriental-Motoren pruefen, Kalibrierwerte uebernehmen und Soll-/Grenzwerte validieren.",
        kind="manual",
        href="/ui/machine-setup/motors",
    ),
    CommissioningStepTemplate(
        "io_test",
        "motion",
        "IO-Test",
        "Digitale Ein-/Ausgaenge pruefen, inklusive Statuslampe, Taster und bekannte Moxa-Kanaele.",
        kind="manual",
        href="/ui/machine-setup/io",
    ),
    CommissioningStepTemplate(
        "encoder_test",
        "motion",
        "Encoder-Test",
        "Encoderwege, Richtung und Aufloesung der Transport-/Wicklersensorik pruefen.",
        kind="manual",
        href="/ui/machine-setup/process",
    ),
    CommissioningStepTemplate(
        "safety_circuit",
        "safety",
        "Sicherheitskreis",
        "Not-Aus, Lichtgitter und gefuehrte Zustandswechsel pruefen.",
        kind="manual",
        href="/ui/machine-setup/process",
    ),
    CommissioningStepTemplate(
        "label_process_test",
        "validation",
        "Produktionsprozess trocken testen",
        "Schieberegister, Trigger, Rueckmeldungen und Stop-/Pause-/Rewind-Verhalten pruefen.",
        kind="manual",
        href="/ui/machine-setup/process",
    ),
    CommissioningStepTemplate(
        "backup_baseline",
        "validation",
        "Backup-Basis erstellen",
        "Nach erfolgreicher IBN ein Settings-Backup und ein Vollbackup/Klonpaket erzeugen.",
        kind="manual",
        href="/ui/machine-setup/backups",
    ),
]


TERMINAL_STEP_STATUSES = {"success", "failed", "skipped", "reused"}
SUCCESS_LIKE_STATUSES = {"success", "reused"}


class CommissioningStore:
    def __init__(self, db: DB, cfg: Settings, cfg_path: str):
        self.db = db
        self.cfg = cfg
        self.cfg_path = cfg_path
        self.io_store = IoStore(db)

    def templates(self) -> List[Dict[str, Any]]:
        return [self._template_dict(item) for item in STEP_TEMPLATES]

    def start_run(self, mode: str = "full") -> Dict[str, Any]:
        normalized_mode = "incomplete_only" if str(mode or "").strip().lower() in {"open", "remaining", "incomplete", "incomplete_only"} else "full"
        started_from = self.latest_run()
        started_from_id = int(started_from["run_id"]) if started_from else None
        carry_over: Dict[str, Dict[str, Any]] = {}
        if normalized_mode == "incomplete_only" and started_from:
            for step in started_from.get("steps", []):
                if str(step.get("status") or "") in SUCCESS_LIKE_STATUSES:
                    carry_over[str(step.get("step_id") or "")] = step

        created_ts = now_ts()
        with self.db._conn() as conn:
            cur = conn.execute(
                """INSERT INTO machine_commissioning_runs(
                   created_ts, updated_ts, mode, status, machine_serial, machine_name, started_from_run_id, summary_json
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    created_ts,
                    created_ts,
                    normalized_mode,
                    "active",
                    str(getattr(self.cfg, "machine_serial_number", "") or ""),
                    str(getattr(self.cfg, "machine_name", "") or ""),
                    started_from_id,
                    "{}",
                ),
            )
            run_id = int(cur.lastrowid or 0)
            for order, template in enumerate(STEP_TEMPLATES, start=1):
                reused = carry_over.get(template.step_id)
                status = "reused" if reused else "pending"
                note = str(reused.get("note") or "") if reused else ""
                result_json = json.dumps(reused.get("result") or {}, ensure_ascii=True, sort_keys=True) if reused else "{}"
                context_json = json.dumps(self.step_context(template.step_id), ensure_ascii=True, sort_keys=True)
                conn.execute(
                    """INSERT INTO machine_commissioning_steps(
                       run_id, step_id, sort_order, section_id, title, status, note, result_json, context_json, updated_ts
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        run_id,
                        template.step_id,
                        order,
                        template.section_id,
                        template.title,
                        status,
                        note,
                        result_json,
                        context_json,
                        created_ts,
                    ),
                )
        return self.get_run(run_id)

    def latest_run(self) -> Optional[Dict[str, Any]]:
        with self.db._conn() as conn:
            row = conn.execute(
                """SELECT run_id FROM machine_commissioning_runs
                   ORDER BY created_ts DESC, run_id DESC
                   LIMIT 1"""
            ).fetchone()
        if not row:
            return None
        return self.get_run(int(row[0]))

    def list_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self.db._conn() as conn:
            rows = conn.execute(
                """SELECT run_id, created_ts, updated_ts, completed_ts, mode, status,
                          machine_serial, machine_name, started_from_run_id, summary_json
                   FROM machine_commissioning_runs
                   ORDER BY created_ts DESC, run_id DESC
                   LIMIT ?""",
                (max(1, min(int(limit or 20), 200)),),
            ).fetchall()
        return [self._run_row_to_dict(row, include_steps=False) for row in rows]

    def get_run(self, run_id: int) -> Dict[str, Any]:
        with self.db._conn() as conn:
            row = conn.execute(
                """SELECT run_id, created_ts, updated_ts, completed_ts, mode, status,
                          machine_serial, machine_name, started_from_run_id, summary_json
                   FROM machine_commissioning_runs
                   WHERE run_id=?""",
                (int(run_id),),
            ).fetchone()
            if not row:
                raise RuntimeError(f"Unknown commissioning run {int(run_id)}")
            step_rows = conn.execute(
                """SELECT step_id, sort_order, section_id, title, status, note, result_json, context_json, updated_ts
                   FROM machine_commissioning_steps
                   WHERE run_id=?
                   ORDER BY sort_order ASC, step_id ASC""",
                (int(run_id),),
            ).fetchall()
        run = self._run_row_to_dict(row, include_steps=False)
        run["steps"] = [self._step_row_to_dict(item) for item in step_rows]
        run["summary"] = self._summarize_run(run["steps"])
        return run

    def update_step(
        self,
        run_id: int,
        step_id: str,
        status: str,
        note: str = "",
        result: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        normalized_status = str(status or "").strip().lower()
        if normalized_status not in {"pending", "in_progress", "success", "failed", "skipped", "reused"}:
            raise RuntimeError(f"Unsupported commissioning step status '{status}'")
        step = self._require_step(run_id, step_id)
        template = self._template_by_id(step_id)
        updated_ts = now_ts()
        with self.db._conn() as conn:
            conn.execute(
                """UPDATE machine_commissioning_steps
                   SET status=?, note=?, result_json=?, context_json=?, updated_ts=?
                   WHERE run_id=? AND step_id=?""",
                (
                    normalized_status,
                    str(note or "").strip(),
                    json.dumps(result or step.get("result") or {}, ensure_ascii=True, sort_keys=True),
                    json.dumps(self.step_context(template.step_id), ensure_ascii=True, sort_keys=True),
                    updated_ts,
                    int(run_id),
                    template.step_id,
                ),
            )
            self._refresh_run_status(conn, int(run_id), updated_ts)
        return self.get_run(run_id)

    def auto_check_step(self, run_id: int, step_id: str) -> Dict[str, Any]:
        template = self._template_by_id(step_id)
        if template.kind != "auto":
            raise RuntimeError(f"Step '{step_id}' is not auto-checkable")
        status, note, result = self._execute_check(template.check_key or template.step_id)
        return self.update_step(run_id, step_id, status=status, note=note, result=result)

    def overview(self) -> Dict[str, Any]:
        latest = self.latest_run()
        return {
            "ok": True,
            "machine_serial_number": str(getattr(self.cfg, "machine_serial_number", "") or ""),
            "machine_name": str(getattr(self.cfg, "machine_name", "") or ""),
            "templates": self.templates(),
            "latest_run": latest,
            "runs": self.list_runs(limit=20),
            "bootstrap_script": {
                "path": "scripts/mas004_machine_bootstrap.py",
                "example_discover": "python scripts/mas004_machine_bootstrap.py discover --subnet 192.168.210.0/24",
                "example_clone": "python scripts/mas004_machine_bootstrap.py apply-full-backup --target pi@<host> --bundle <full-backup.zip> --tty --restart-service",
                "example_settings_restore": "python scripts/mas004_machine_bootstrap.py apply-settings-backup --target pi@<host> --bundle <settings-backup.zip> --tty --restart-service",
            },
        }

    def step_context(self, step_id: str) -> Dict[str, Any]:
        template = self._template_by_id(step_id)
        context: Dict[str, Any] = {
            "step_id": template.step_id,
            "section_id": template.section_id,
            "title": template.title,
            "kind": template.kind,
            "href": template.href,
            "machine_serial_number": str(getattr(self.cfg, "machine_serial_number", "") or ""),
            "machine_name": str(getattr(self.cfg, "machine_name", "") or ""),
        }
        if template.check_key == "network":
            context["eth0_ip"] = self.cfg.eth0_ip
            context["eth1_ip"] = self.cfg.eth1_ip
            context["peer_base_url"] = self.cfg.peer_base_url
            return context
        if template.check_key == "runtime":
            context["db_path"] = self.cfg.db_path
            context["service_name"] = "mas004-rpi-databridge.service"
            context["repo_root"] = os.path.dirname(os.path.dirname(__file__))
            return context
        if template.check_key == "workbooks":
            context["master_params_xlsx_path"] = self.cfg.master_params_xlsx_path
            context["master_ios_xlsx_path"] = self.cfg.master_ios_xlsx_path
            context["io_points"] = self.io_store.count_points()
            return context
        endpoint_map = {
            "esp": ("esp_host", "esp_port", "esp_simulation"),
            "moxa1": ("moxa1_host", "moxa1_port", "moxa1_simulation"),
            "moxa2": ("moxa2_host", "moxa2_port", "moxa2_simulation"),
            "vj6530": ("vj6530_host", "vj6530_port", "vj6530_simulation"),
            "vj3350": ("vj3350_host", "vj3350_port", "vj3350_simulation"),
            "unwinder": ("smart_unwinder_host", "smart_unwinder_port", "smart_unwinder_simulation"),
            "rewinder": ("smart_rewinder_host", "smart_rewinder_port", "smart_rewinder_simulation"),
        }
        if template.check_key in endpoint_map:
            host_key, port_key, sim_key = endpoint_map[template.check_key]
            context["host"] = str(getattr(self.cfg, host_key, "") or "")
            context["port"] = int(getattr(self.cfg, port_key, 0) or 0)
            context["simulation"] = bool(getattr(self.cfg, sim_key, True))
            return context
        return context

    def _require_step(self, run_id: int, step_id: str) -> Dict[str, Any]:
        run = self.get_run(run_id)
        for step in run.get("steps", []):
            if str(step.get("step_id") or "") == step_id:
                return step
        raise RuntimeError(f"Unknown commissioning step '{step_id}' in run {int(run_id)}")

    def _template_by_id(self, step_id: str) -> CommissioningStepTemplate:
        for item in STEP_TEMPLATES:
            if item.step_id == step_id:
                return item
        raise RuntimeError(f"Unknown commissioning template '{step_id}'")

    def _template_dict(self, item: CommissioningStepTemplate) -> Dict[str, Any]:
        return {
            "step_id": item.step_id,
            "section_id": item.section_id,
            "title": item.title,
            "description": item.description,
            "kind": item.kind,
            "check_key": item.check_key,
            "href": item.href,
        }

    def _run_row_to_dict(self, row, include_steps: bool) -> Dict[str, Any]:
        summary = {}
        try:
            summary = json.loads(row[9] or "{}")
        except Exception:
            summary = {}
        payload = {
            "run_id": int(row[0]),
            "created_ts": float(row[1] or 0.0),
            "updated_ts": float(row[2] or 0.0),
            "completed_ts": float(row[3] or 0.0) if row[3] is not None else None,
            "mode": row[4],
            "status": row[5],
            "machine_serial": row[6] or "",
            "machine_name": row[7] or "",
            "started_from_run_id": int(row[8] or 0) if row[8] is not None else None,
            "summary": summary,
        }
        if include_steps:
            payload["steps"] = []
        return payload

    def _step_row_to_dict(self, row) -> Dict[str, Any]:
        result = {}
        context = {}
        try:
            result = json.loads(row[6] or "{}")
        except Exception:
            result = {}
        try:
            context = json.loads(row[7] or "{}")
        except Exception:
            context = {}
        template = self._template_by_id(str(row[0]))
        return {
            "step_id": row[0],
            "sort_order": int(row[1] or 0),
            "section_id": row[2] or template.section_id,
            "title": row[3] or template.title,
            "status": row[4] or "pending",
            "note": row[5] or "",
            "result": result,
            "context": context,
            "description": template.description,
            "kind": template.kind,
            "href": template.href,
            "updated_ts": float(row[8] or 0.0),
        }

    def _summarize_run(self, steps: List[Dict[str, Any]]) -> Dict[str, Any]:
        summary = {
            "total": len(steps),
            "pending": 0,
            "in_progress": 0,
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "reused": 0,
            "successful_like": 0,
        }
        for step in steps:
            status = str(step.get("status") or "pending")
            summary[status] = int(summary.get(status, 0) or 0) + 1
            if status in SUCCESS_LIKE_STATUSES:
                summary["successful_like"] += 1
        summary["done"] = summary["pending"] == 0 and summary["in_progress"] == 0
        return summary

    def _refresh_run_status(self, conn, run_id: int, updated_ts: float) -> None:
        step_rows = conn.execute(
            "SELECT status FROM machine_commissioning_steps WHERE run_id=?",
            (int(run_id),),
        ).fetchall()
        steps = [{"status": row[0]} for row in step_rows]
        summary = self._summarize_run(steps)
        run_status = "completed" if summary["done"] else "active"
        completed_ts = updated_ts if summary["done"] else None
        conn.execute(
            """UPDATE machine_commissioning_runs
               SET updated_ts=?, completed_ts=?, status=?, summary_json=?
               WHERE run_id=?""",
            (
                updated_ts,
                completed_ts,
                run_status,
                json.dumps(summary, ensure_ascii=True, sort_keys=True),
                int(run_id),
            ),
        )

    def _execute_check(self, check_key: str) -> tuple[str, str, Dict[str, Any]]:
        if check_key == "network":
            ok = bool((self.cfg.eth0_ip or "").strip()) and bool((self.cfg.eth1_ip or "").strip())
            result = {
                "eth0_ip": self.cfg.eth0_ip,
                "eth1_ip": self.cfg.eth1_ip,
                "peer_base_url": self.cfg.peer_base_url,
            }
            return ("success" if ok else "failed", "ETH0/ETH1 konfiguriert" if ok else "ETH0 oder ETH1 ist noch leer", result)
        if check_key == "runtime":
            service_state = self._systemctl_state("mas004-rpi-databridge.service")
            repo_root = os.path.dirname(os.path.dirname(__file__))
            db_exists = os.path.exists(self.cfg.db_path)
            ok = service_state.get("active") == "active" and db_exists
            result = {
                "service": service_state,
                "db_path": self.cfg.db_path,
                "db_exists": db_exists,
                "repo_root": repo_root,
                "repo_exists": os.path.exists(repo_root),
            }
            note = "Databridge-Service aktiv" if ok else "Service oder DB fehlt / ist nicht aktiv"
            return ("success" if ok else "failed", note, result)
        if check_key == "workbooks":
            params_ok = os.path.exists(self.cfg.master_params_xlsx_path)
            io_ok = os.path.exists(self.cfg.master_ios_xlsx_path)
            io_points = self.io_store.count_points()
            ok = params_ok and io_ok and io_points > 0
            result = {
                "master_params_xlsx_path": self.cfg.master_params_xlsx_path,
                "master_ios_xlsx_path": self.cfg.master_ios_xlsx_path,
                "params_exists": params_ok,
                "ios_exists": io_ok,
                "io_points": io_points,
            }
            note = "Masterdateien vorhanden und IO-Katalog geladen" if ok else "Masterdatei oder IO-Import fehlt"
            return ("success" if ok else "failed", note, result)
        endpoint_map = {
            "esp": ("esp_host", "esp_port", "esp_simulation"),
            "moxa1": ("moxa1_host", "moxa1_port", "moxa1_simulation"),
            "moxa2": ("moxa2_host", "moxa2_port", "moxa2_simulation"),
            "vj6530": ("vj6530_host", "vj6530_port", "vj6530_simulation"),
            "vj3350": ("vj3350_host", "vj3350_port", "vj3350_simulation"),
            "unwinder": ("smart_unwinder_host", "smart_unwinder_port", "smart_unwinder_simulation"),
            "rewinder": ("smart_rewinder_host", "smart_rewinder_port", "smart_rewinder_simulation"),
        }
        if check_key in endpoint_map:
            host_key, port_key, sim_key = endpoint_map[check_key]
            host = str(getattr(self.cfg, host_key, "") or "").strip()
            port = int(getattr(self.cfg, port_key, 0) or 0)
            simulation = bool(getattr(self.cfg, sim_key, True))
            if simulation:
                return ("success", "Simulation ist aktiv", {"host": host, "port": port, "simulation": True})
            reachable, error = self._probe_socket(host, port)
            note = "Endpoint erreichbar" if reachable else (error or "Endpoint nicht erreichbar")
            return (
                "success" if reachable else "failed",
                note,
                {"host": host, "port": port, "simulation": False, "reachable": reachable, "error": error},
            )
        return ("pending", "Manueller Schritt ohne Auto-Check", {"check_key": check_key})

    def _probe_socket(self, host: str, port: int, timeout_s: float = 1.5) -> tuple[bool, str]:
        if not host or int(port or 0) <= 0:
            return False, "Endpoint fehlt"
        try:
            with socket.create_connection((host, int(port)), timeout=timeout_s):
                return True, ""
        except Exception as exc:
            return False, str(exc)

    def _systemctl_state(self, service_name: str) -> Dict[str, Any]:
        result = {"service": service_name, "active": "unknown", "enabled": "unknown"}
        for flag, key in (("is-active", "active"), ("is-enabled", "enabled")):
            try:
                proc = subprocess.run(
                    ["systemctl", flag, service_name],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                result[key] = (proc.stdout or proc.stderr or "").strip() or "unknown"
            except Exception as exc:
                result[key] = f"error: {exc}"
        return result

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from mas004_rpi_databridge.config import Settings
from mas004_rpi_databridge.db import DB, now_ts


REPO_CANDIDATE_NAMES = [
    "MAS-004_RPI-Databridge",
    "MAS-004_ESP32-PLC-Bridge",
    "MAS-004_ESP32-PLC-Firmware",
    "MAS-004_VJ3350-Ultimate-Bridge",
    "MAS-004_VJ6530-ZBC-Bridge",
    "MAS-004_ZBC-Library",
    "MAS-004_SmartWickler",
]

IGNORED_DIRS = {
    ".git",
    ".venv",
    ".pytest_cache",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pio",
    "node_modules",
    "build",
    "dist",
}


def _slugify(raw: str) -> str:
    text = str(raw or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text or "backup"


def _json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=True, indent=2, sort_keys=True)


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


class MachineBackupManager:
    def __init__(self, db: DB, cfg: Settings, cfg_path: str):
        self.db = db
        self.cfg = cfg
        self.cfg_path = cfg_path

    def backup_root(self) -> str:
        configured = str(getattr(self.cfg, "backup_root_path", "") or "").strip()
        if configured:
            return configured
        return os.path.join(os.path.dirname(self.cfg.db_path) or ".", "backups")

    def identity(self) -> Dict[str, str]:
        return {
            "machine_serial_number": str(getattr(self.cfg, "machine_serial_number", "") or "").strip(),
            "machine_name": str(getattr(self.cfg, "machine_name", "") or "").strip(),
        }

    def set_identity(self, machine_serial_number: str, machine_name: str = "") -> Dict[str, str]:
        self.cfg.machine_serial_number = str(machine_serial_number or "").strip()
        self.cfg.machine_name = str(machine_name or "").strip()
        self.cfg.save(self.cfg_path)
        return self.identity()

    def settings_dir(self) -> str:
        return os.path.join(self.backup_root(), "settings")

    def full_dir(self) -> str:
        return os.path.join(self.backup_root(), "full")

    def import_dir(self) -> str:
        return os.path.join(self.backup_root(), "imports")

    def registry_dir(self) -> str:
        return os.path.join(self.backup_root(), "registry")

    def overview(self) -> Dict[str, Any]:
        return {
            "ok": True,
            "identity": self.identity(),
            "paths": {
                "backup_root": self.backup_root(),
                "settings_dir": self.settings_dir(),
                "full_dir": self.full_dir(),
                "import_dir": self.import_dir(),
                "registry_dir": self.registry_dir(),
            },
            "backups": self.list_backups(limit=200),
            "counts": self._counts_by_type(),
        }

    def list_backups(self, limit: int = 100, backup_type: Optional[str] = None) -> List[Dict[str, Any]]:
        where = ""
        args: list[Any] = []
        if backup_type:
            where = "WHERE backup_type=?"
            args.append(str(backup_type))
        args.append(max(1, min(int(limit or 100), 500)))
        with self.db._conn() as conn:
            rows = conn.execute(
                f"""SELECT backup_id, backup_type, name, source, note, machine_serial, machine_name,
                           created_ts, file_path, size_bytes, sha256, manifest_json
                    FROM machine_backups
                    {where}
                    ORDER BY created_ts DESC, backup_id DESC
                    LIMIT ?""",
                args,
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def create_settings_backup(self, name: str, note: str = "", source: str = "local") -> Dict[str, Any]:
        backup_id, archive_path, manifest = self._prepare_backup_metadata("settings", name, note, source)
        os.makedirs(os.path.dirname(archive_path), exist_ok=True)
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            self._write_settings_payload(zf, manifest)
        return self._finalize_backup_record(backup_id, "settings", name, note, source, archive_path, manifest)

    def create_full_backup(self, name: str, note: str = "", source: str = "local") -> Dict[str, Any]:
        backup_id, archive_path, manifest = self._prepare_backup_metadata("full", name, note, source)
        os.makedirs(os.path.dirname(archive_path), exist_ok=True)
        repo_manifest = self._collect_repo_manifest()
        manifest["repositories"] = repo_manifest
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            self._write_settings_payload(zf, manifest)
            for repo_item in repo_manifest:
                if repo_item.get("exists") and repo_item.get("path"):
                    self._write_repo_snapshot(zf, str(repo_item["path"]), f"repos/{repo_item['name']}")
            zf.writestr("repositories.json", _json_dumps(repo_manifest))
        return self._finalize_backup_record(backup_id, "full", name, note, source, archive_path, manifest)

    def import_backup(self, upload_path: str, original_name: str = "") -> Dict[str, Any]:
        with zipfile.ZipFile(upload_path, "r") as zf:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        backup_type = str(manifest.get("backup_type") or "settings")
        name = str(manifest.get("name") or Path(original_name or upload_path).stem or "imported-backup")
        backup_id = str(manifest.get("backup_id") or uuid.uuid4().hex)
        target_dir = self.settings_dir() if backup_type == "settings" else self.full_dir()
        os.makedirs(target_dir, exist_ok=True)
        ts_tag = self._timestamp_tag(float(manifest.get("created_ts") or now_ts()))
        archive_path = os.path.join(target_dir, f"{ts_tag}__{_slugify(name)}__{backup_id[:8]}.zip")
        shutil.copyfile(upload_path, archive_path)
        return self._finalize_backup_record(
            backup_id,
            backup_type,
            name,
            str(manifest.get("note") or ""),
            "imported",
            archive_path,
            manifest,
        )

    def restore_backup(self, backup_id: str) -> Dict[str, Any]:
        backup = self.get_backup(backup_id)
        preflight = self.create_settings_backup(f"pre-restore-{backup['name']}", note=f"Automatisch vor Restore von {backup_id}", source="auto-pre-restore")
        tmp_dir = tempfile.mkdtemp(prefix="mas004_restore_")
        try:
            with zipfile.ZipFile(backup["file_path"], "r") as zf:
                zf.extractall(tmp_dir)
            self._restore_settings_payload(tmp_dir)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return {
            "ok": True,
            "restored_from": backup_id,
            "backup_type": backup["backup_type"],
            "pre_restore_backup_id": preflight["backup_id"],
            "restart_required": True,
            "message": "Dateien wiederhergestellt. Databridge-Service bitte neu starten, damit alle Einstellungen sauber neu geladen werden.",
        }

    def delete_backup(self, backup_id: str) -> Dict[str, Any]:
        backup = self.get_backup(backup_id)
        try:
            if os.path.exists(backup["file_path"]):
                os.remove(backup["file_path"])
        except Exception:
            pass
        registry_path = str((backup.get("manifest") or {}).get("registry_manifest_path") or "")
        try:
            if registry_path and os.path.exists(registry_path):
                os.remove(registry_path)
        except Exception:
            pass
        with self.db._conn() as conn:
            conn.execute("DELETE FROM machine_backups WHERE backup_id=?", (backup_id,))
        return {"ok": True, "deleted": backup_id}

    def get_backup(self, backup_id: str) -> Dict[str, Any]:
        with self.db._conn() as conn:
            row = conn.execute(
                """SELECT backup_id, backup_type, name, source, note, machine_serial, machine_name,
                          created_ts, file_path, size_bytes, sha256, manifest_json
                   FROM machine_backups WHERE backup_id=?""",
                (backup_id,),
            ).fetchone()
        if not row:
            raise RuntimeError(f"Unknown backup '{backup_id}'")
        return self._row_to_dict(row)

    def _prepare_backup_metadata(self, backup_type: str, name: str, note: str, source: str) -> tuple[str, str, Dict[str, Any]]:
        created_ts = now_ts()
        ts_tag = self._timestamp_tag(created_ts)
        backup_id = f"{backup_type}-{ts_tag}-{uuid.uuid4().hex[:8]}"
        root_dir = self.settings_dir() if backup_type == "settings" else self.full_dir()
        archive_path = os.path.join(root_dir, f"{ts_tag}__{_slugify(name)}__{backup_id[-8:]}.zip")
        manifest = {
            "schema_version": 1,
            "backup_id": backup_id,
            "backup_type": backup_type,
            "name": str(name or "").strip() or f"{backup_type}-{ts_tag}",
            "note": str(note or "").strip(),
            "source": source,
            "created_ts": created_ts,
            "machine": self.identity(),
            "paths": {
                "cfg_path": self.cfg_path,
                "db_path": self.cfg.db_path,
                "master_params_xlsx_path": self.cfg.master_params_xlsx_path,
                "master_ios_xlsx_path": self.cfg.master_ios_xlsx_path,
            },
        }
        return backup_id, archive_path, manifest

    def _write_settings_payload(self, zf: zipfile.ZipFile, manifest: Dict[str, Any]) -> None:
        manifest_items: list[dict[str, Any]] = []
        zf.writestr("config/settings.json", _json_dumps(self.cfg.__dict__))
        manifest_items.append({"role": "config", "name": "config/settings.json"})

        db_tmp = self._snapshot_sqlite()
        if db_tmp:
            try:
                zf.write(db_tmp, "db/databridge.db")
                manifest_items.append({"role": "db", "name": "db/databridge.db"})
            finally:
                try:
                    shutil.rmtree(os.path.dirname(db_tmp), ignore_errors=True)
                except Exception:
                    pass

        for role, src_path, arcname in (
            ("master_params", self.cfg.master_params_xlsx_path, "master/Parameterliste_master.xlsx"),
            ("master_ios", self.cfg.master_ios_xlsx_path, "master/SAR41-MAS-004_SPS_I-Os.xlsx"),
            ("motor_ui_state", self._motor_state_path(), "state/motor_ui_state.json"),
            ("production_state", self._production_state_path(), "state/production_state.json"),
        ):
            if src_path and os.path.exists(src_path):
                zf.write(src_path, arcname)
                manifest_items.append({"role": role, "name": arcname})

        manifest["items"] = manifest_items

    def _restore_settings_payload(self, extracted_root: str) -> None:
        file_map = [
            (os.path.join(extracted_root, "config", "settings.json"), self.cfg_path),
            (os.path.join(extracted_root, "db", "databridge.db"), self.cfg.db_path),
            (os.path.join(extracted_root, "master", "Parameterliste_master.xlsx"), self.cfg.master_params_xlsx_path),
            (os.path.join(extracted_root, "master", "SAR41-MAS-004_SPS_I-Os.xlsx"), self.cfg.master_ios_xlsx_path),
            (os.path.join(extracted_root, "state", "motor_ui_state.json"), self._motor_state_path()),
            (os.path.join(extracted_root, "state", "production_state.json"), self._production_state_path()),
        ]
        for src_path, dst_path in file_map:
            if not src_path or not os.path.exists(src_path):
                continue
            os.makedirs(os.path.dirname(dst_path), exist_ok=True)
            shutil.copyfile(src_path, dst_path)

    def _snapshot_sqlite(self) -> str:
        if not self.cfg.db_path or not os.path.exists(self.cfg.db_path):
            return ""
        tmp_dir = tempfile.mkdtemp(prefix="mas004_sqlite_backup_")
        tmp_path = os.path.join(tmp_dir, "databridge.db")
        src = sqlite3.connect(self.cfg.db_path, timeout=30)
        dst = sqlite3.connect(tmp_path, timeout=30)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        return tmp_path

    def _motor_state_path(self) -> str:
        return os.path.join(os.path.dirname(self.cfg.db_path) or ".", "motor_ui_state.json")

    def _production_state_path(self) -> str:
        return os.path.join(os.path.dirname(self.cfg.db_path) or ".", "production_logs", "_production_state.json")

    def _collect_repo_manifest(self) -> List[Dict[str, Any]]:
        manifests: List[Dict[str, Any]] = []
        for repo_path in self._candidate_repo_paths():
            manifests.append(self._repo_state(repo_path))
        return manifests

    def _candidate_repo_paths(self) -> Iterable[str]:
        current_repo_root = Path(__file__).resolve().parents[1]
        parent = current_repo_root.parent
        seen: set[str] = set()
        for name in REPO_CANDIDATE_NAMES:
            candidate = str(parent / name)
            if candidate not in seen:
                seen.add(candidate)
                yield candidate
        for name in REPO_CANDIDATE_NAMES:
            candidate = os.path.join("/opt", name)
            if candidate not in seen:
                seen.add(candidate)
                yield candidate

    def _repo_state(self, repo_path: str) -> Dict[str, Any]:
        repo = Path(repo_path)
        payload: Dict[str, Any] = {"name": repo.name, "path": str(repo), "exists": repo.exists()}
        if not repo.exists():
            payload["error"] = "missing"
            return payload
        git_dir = repo / ".git"
        if not git_dir.exists():
            payload["error"] = "not-a-git-repo"
            return payload
        payload["git"] = {
            "head": self._run_git(repo_path, ["rev-parse", "HEAD"]),
            "short_head": self._run_git(repo_path, ["rev-parse", "--short", "HEAD"]),
            "branch": self._run_git(repo_path, ["branch", "--show-current"]),
            "status": self._run_git(repo_path, ["status", "--short"]),
        }
        return payload

    def _run_git(self, repo_path: str, args: List[str]) -> str:
        try:
            proc = subprocess.run(
                ["git", "-C", repo_path, *args],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            return (proc.stdout or proc.stderr or "").strip()
        except Exception as exc:
            return f"error: {exc}"

    def _write_repo_snapshot(self, zf: zipfile.ZipFile, repo_path: str, archive_root: str) -> None:
        repo = Path(repo_path)
        for file_path in repo.rglob("*"):
            if not file_path.is_file():
                continue
            if any(part in IGNORED_DIRS for part in file_path.parts):
                continue
            rel = file_path.relative_to(repo)
            zf.write(str(file_path), f"{archive_root}/{rel.as_posix()}")

    def _finalize_backup_record(
        self,
        backup_id: str,
        backup_type: str,
        name: str,
        note: str,
        source: str,
        archive_path: str,
        manifest: Dict[str, Any],
    ) -> Dict[str, Any]:
        size_bytes = os.path.getsize(archive_path)
        sha256 = _sha256_file(archive_path)
        manifest["archive"] = {"path": archive_path, "size_bytes": size_bytes, "sha256": sha256}
        registry_manifest_path = self._write_registry_manifest(manifest)
        manifest["registry_manifest_path"] = registry_manifest_path
        with zipfile.ZipFile(archive_path, "a", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", _json_dumps(manifest))
        with self.db._conn() as conn:
            conn.execute(
                """INSERT INTO machine_backups(
                   backup_id, backup_type, name, source, note, machine_serial, machine_name,
                   created_ts, file_path, size_bytes, sha256, manifest_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(backup_id) DO UPDATE SET
                     backup_type=excluded.backup_type,
                     name=excluded.name,
                     source=excluded.source,
                     note=excluded.note,
                     machine_serial=excluded.machine_serial,
                     machine_name=excluded.machine_name,
                     created_ts=excluded.created_ts,
                     file_path=excluded.file_path,
                     size_bytes=excluded.size_bytes,
                     sha256=excluded.sha256,
                     manifest_json=excluded.manifest_json""",
                (
                    backup_id,
                    backup_type,
                    name,
                    source,
                    note,
                    str((manifest.get("machine") or {}).get("machine_serial_number") or ""),
                    str((manifest.get("machine") or {}).get("machine_name") or ""),
                    float(manifest.get("created_ts") or now_ts()),
                    archive_path,
                    int(size_bytes),
                    sha256,
                    json.dumps(manifest, ensure_ascii=True, sort_keys=True),
                ),
            )
        self._prune_backup_type(backup_type)
        return self.get_backup(backup_id)

    def _write_registry_manifest(self, manifest: Dict[str, Any]) -> str:
        serial = str((manifest.get("machine") or {}).get("machine_serial_number") or "unassigned").strip() or "unassigned"
        root = os.path.join(self.registry_dir(), _slugify(serial))
        os.makedirs(root, exist_ok=True)
        ts_tag = self._timestamp_tag(float(manifest.get("created_ts") or now_ts()))
        path = os.path.join(root, f"{ts_tag}__{_slugify(str(manifest.get('name') or 'backup'))}.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(_json_dumps(manifest))
            handle.write("\n")
        return path

    def _counts_by_type(self) -> Dict[str, int]:
        with self.db._conn() as conn:
            rows = conn.execute(
                "SELECT backup_type, COUNT(*) FROM machine_backups GROUP BY backup_type"
            ).fetchall()
        return {str(row[0]): int(row[1] or 0) for row in rows}

    def _prune_backup_type(self, backup_type: str) -> None:
        with self.db._conn() as conn:
            rows = conn.execute(
                """SELECT backup_id, file_path, manifest_json
                   FROM machine_backups
                   WHERE backup_type=?
                   ORDER BY created_ts DESC, backup_id DESC""",
                (backup_type,),
            ).fetchall()
            for row in rows[100:]:
                backup_id = str(row[0])
                file_path = str(row[1] or "")
                manifest_json = {}
                try:
                    manifest_json = json.loads(row[2] or "{}")
                except Exception:
                    manifest_json = {}
                if file_path and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                    except Exception:
                        pass
                registry_manifest_path = str(manifest_json.get("registry_manifest_path") or "")
                if registry_manifest_path and os.path.exists(registry_manifest_path):
                    try:
                        os.remove(registry_manifest_path)
                    except Exception:
                        pass
                conn.execute("DELETE FROM machine_backups WHERE backup_id=?", (backup_id,))

    def _timestamp_tag(self, ts_value: float) -> str:
        from datetime import datetime

        return datetime.fromtimestamp(ts_value).strftime("%Y%m%d-%H%M%S")

    def _row_to_dict(self, row) -> Dict[str, Any]:
        manifest = {}
        try:
            manifest = json.loads(row[11] or "{}")
        except Exception:
            manifest = {}
        return {
            "backup_id": row[0],
            "backup_type": row[1],
            "name": row[2],
            "source": row[3],
            "note": row[4] or "",
            "machine_serial": row[5] or "",
            "machine_name": row[6] or "",
            "created_ts": float(row[7] or 0.0),
            "file_path": row[8],
            "size_bytes": int(row[9] or 0),
            "sha256": row[10] or "",
            "manifest": manifest,
        }

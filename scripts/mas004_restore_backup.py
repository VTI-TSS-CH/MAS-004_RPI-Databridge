from __future__ import annotations

import argparse
import os
import shutil
import tempfile
import time
import zipfile
from pathlib import Path


def backup_existing(path: str, suffix: str) -> None:
    if not path or not os.path.exists(path):
        return
    backup_path = f"{path}.{suffix}"
    if os.path.isdir(path):
        if os.path.exists(backup_path):
            shutil.rmtree(backup_path, ignore_errors=True)
        shutil.copytree(path, backup_path)
    else:
        os.makedirs(os.path.dirname(backup_path), exist_ok=True)
        shutil.copy2(path, backup_path)


def extract_file_if_present(root: str, relative_path: str, target_path: str, suffix: str) -> bool:
    src_path = os.path.join(root, relative_path)
    if not os.path.exists(src_path):
        return False
    backup_existing(target_path, suffix)
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    shutil.copy2(src_path, target_path)
    return True


def apply_repo_snapshots(root: str, target_root: str, suffix: str) -> list[str]:
    repo_root = os.path.join(root, "repos")
    applied: list[str] = []
    if not os.path.exists(repo_root):
        return applied
    for item in sorted(os.listdir(repo_root)):
        src_dir = os.path.join(repo_root, item)
        if not os.path.isdir(src_dir):
            continue
        dst_dir = os.path.join(target_root, item)
        backup_existing(dst_dir, suffix)
        os.makedirs(os.path.dirname(dst_dir), exist_ok=True)
        if os.path.exists(dst_dir):
            shutil.rmtree(dst_dir, ignore_errors=True)
        shutil.copytree(src_dir, dst_dir)
        applied.append(dst_dir)
    return applied


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply a MAS-004 full/settings backup bundle on a target Raspberry.")
    parser.add_argument("--bundle", required=True, help="Path to the exported backup zip")
    parser.add_argument("--cfg-path", default="/etc/mas004_rpi_databridge/config.json")
    parser.add_argument("--db-path", default="/var/lib/mas004_rpi_databridge/databridge.db")
    parser.add_argument("--master-params-path", default="/var/lib/mas004_rpi_databridge/master/Parameterliste_master.xlsx")
    parser.add_argument("--master-ios-path", default="/var/lib/mas004_rpi_databridge/master/SAR41-MAS-004_SPS_I-Os.xlsx")
    parser.add_argument("--apply-repos", action="store_true", help="Extract repo snapshots from a full backup into /opt")
    parser.add_argument("--repo-root", default="/opt")
    args = parser.parse_args()

    bundle = Path(args.bundle)
    if not bundle.exists():
        raise SystemExit(f"Bundle not found: {bundle}")

    suffix = f"preclone-{time.strftime('%Y%m%d-%H%M%S')}"
    tmp_dir = tempfile.mkdtemp(prefix="mas004_restore_bundle_")
    try:
        with zipfile.ZipFile(bundle, "r") as zf:
            zf.extractall(tmp_dir)

        applied = []
        applied.append(("config", extract_file_if_present(tmp_dir, "config/settings.json", args.cfg_path, suffix)))
        applied.append(("db", extract_file_if_present(tmp_dir, "db/databridge.db", args.db_path, suffix)))
        applied.append(("master_params", extract_file_if_present(tmp_dir, "master/Parameterliste_master.xlsx", args.master_params_path, suffix)))
        applied.append(("master_ios", extract_file_if_present(tmp_dir, "master/SAR41-MAS-004_SPS_I-Os.xlsx", args.master_ios_path, suffix)))
        state_root = os.path.dirname(args.db_path) or "."
        applied.append(("motor_ui_state", extract_file_if_present(tmp_dir, "state/motor_ui_state.json", os.path.join(state_root, "motor_ui_state.json"), suffix)))
        applied.append(("production_state", extract_file_if_present(tmp_dir, "state/production_state.json", os.path.join(state_root, "production_logs", "_production_state.json"), suffix)))

        repo_targets: list[str] = []
        if args.apply_repos:
            repo_targets = apply_repo_snapshots(tmp_dir, args.repo_root, suffix)

        print("Applied files:")
        for label, ok in applied:
            print(f"  - {label}: {'yes' if ok else 'no'}")
        if repo_targets:
            print("Applied repos:")
            for target in repo_targets:
                print(f"  - {target}")
        print("Restore finished. Restart mas004-rpi-databridge.service manually after verification.")
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

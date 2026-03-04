# SUPPORT_RUNBOOK - MAS-004_RPI-Databridge

## 1. Standard Workflow (Mandatory)
1. Check cross-repo status:
   - `powershell -ExecutionPolicy Bypass -File scripts/mas004_multirepo_status.ps1`
2. Implement local change.
3. Validate locally (tests/lint/smoke as applicable).
4. Commit and push.
5. Sync Pi and verify service:
   - `powershell -ExecutionPolicy Bypass -File scripts/mas004_multirepo_sync.ps1 -RestartServices`
6. Verify runtime on Pi:
   - `ssh mas004-rpi "systemctl status mas004-rpi-databridge.service --no-pager"`
   - `curl -k https://<raspi-ip>:8080/health`

## 2. Local Commands
- Install env:
  - `python -m venv .venv`
  - `.\.venv\Scripts\Activate.ps1`
  - `python -m pip install -U pip`
  - `python -m pip install -e .`
- Run app:
  - `mas004-databridge`

## 3. Pi Commands
- Update:
  - `ssh mas004-rpi "cd /opt/MAS-004_RPI-Databridge && git pull --ff-only"`
- Restart:
  - `ssh mas004-rpi "sudo systemctl restart mas004-rpi-databridge.service"`
- Logs:
  - `ssh mas004-rpi "sudo journalctl -u mas004-rpi-databridge.service -n 120 --no-pager"`

## 4. Safety Rules
- Do not run `git reset --hard` or `git checkout --` on Pi repos.
- If Pi repo is dirty, stop auto-sync for that repo and report exact files.
- Keep this file and `PROJECT_CONTEXT.md` up to date whenever architecture, API surface, or deployment flow changes.

## 5. Verification Checklist
- UI reachable: `/`, `/ui/test`, `/ui/params`, `/ui/settings`
- API health: `/health`
- Outbox not growing unexpectedly
- Shared-secret and peer URL still valid after config changes

## 6. Ownership
- Main project owner context: MAS-004_RPI-Databridge
- Subprojects are operational dependencies and must be checked in each release cycle.

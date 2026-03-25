# SUPPORT_RUNBOOK - MAS-004_RPI-Databridge

## 1. Standard Workflow (Mandatory)
1. Check cross-repo status:
   - TEST (default): `powershell -ExecutionPolicy Bypass -File scripts/mas004_multirepo_status.ps1`
   - LIVE (explicit): `powershell -ExecutionPolicy Bypass -File scripts/mas004_multirepo_status.ps1 -Target live`
2. Implement local change.
3. Validate locally (tests/lint/smoke as applicable).
4. Commit and push.
5. Sync TEST Pi and verify service:
   - `powershell -ExecutionPolicy Bypass -File scripts/mas004_multirepo_sync.ps1 -Target test -RestartServices`
6. LIVE deployment only on explicit release command:
   - `powershell -ExecutionPolicy Bypass -File scripts/mas004_multirepo_sync.ps1 -Target live -AllowLive -RestartServices`
7. Verify runtime on Pi:
   - `ssh pi@10.27.67.68 "systemctl status mas004-rpi-databridge.service --no-pager"` (TEST)
   - `ssh pi@192.168.210.20 "systemctl status mas004-rpi-databridge.service --no-pager"` (LIVE)
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
- TEST update:
  - `ssh pi@10.27.67.68 "cd /opt/MAS-004_RPI-Databridge && git pull --ff-only"`
- LIVE update (only if explicitly approved):
  - `ssh pi@192.168.210.20 "cd /opt/MAS-004_RPI-Databridge && git pull --ff-only"`
- Reinstall package safely after pull (prevents stale `build/` artifacts):
  - `ssh pi@10.27.67.68 "cd /opt/MAS-004_RPI-Databridge && rm -rf build && ./.venv/bin/python -m pip install --no-deps --no-build-isolation --no-cache-dir --force-reinstall ."`
- Restart:
  - `ssh pi@10.27.67.68 "sudo systemctl restart mas004-rpi-databridge.service"`
- Logs:
  - `ssh pi@10.27.67.68 "sudo journalctl -u mas004-rpi-databridge.service -n 120 --no-pager"`

## 4. Safety Rules
- Do not run `git reset --hard` or `git checkout --` on Pi repos.
- If Pi repo is dirty, stop auto-sync for that repo and report exact files.
- TEST is the default deployment target.
- LIVE sync is blocked unless `-Target live -AllowLive` is set.
- Optional host overrides via environment variables:
  - `MAS004_TEST_SSH`, `MAS004_LIVE_SSH`
  - `MAS004_TEST_WEB`, `MAS004_LIVE_WEB`
- Keep this file and `PROJECT_CONTEXT.md` up to date whenever architecture, API surface, or deployment flow changes.

## 5. Verification Checklist
- UI reachable: `/`, `/ui/test`, `/ui/params`, `/ui/settings`
- API health: `/health`
- Outbox not growing unexpectedly
- Shared-secret and peer URL still valid after config changes
- `TTS0001` present in `/ui/params` and resolves to the expected numeric printer state
- Expect the 6530 async path to be primary for online/offline/warning/fault changes; the poller is fallback/reconciliation only.
- ESP write smoke test for `TTS0001`:
  - from `6`: `TTS0001=0` -> printer starts up into `0 OFFLINE`
  - from `0`: `TTS0001=3` -> printer goes online
  - from `6`: `TTS0001=3` -> printer runs `STARTUP` then `START`
  - `TTS0001=6` -> printer shuts down
- Expect direct rejection for `TTS0001=1`, `2`, `4`, `5`; these are observed composite warning/fault states, not direct control targets
- `TTE` / `TTW` / `TTS` printer-originated updates only forward to Microtom / ESP when the workbook `R/W:` / `ESP32 R/W:` flags allow it
- After a successful 6530 write, verify that related status rows (`TTP00073`, `TTP00076`, `TTS0001`, relevant `TTE*` / `TTW*`) follow immediately without waiting for the next periodic poll cycle.
- Queue semantics are lossless for non-consecutive state flips; if the printer goes `ONLINE -> OFFLINE -> ONLINE`, all three state changes must appear in order at the peer.
- NTP configured values visible in `/ui/settings` and service logs contain `[NTP]` entries
- TCP relay listeners started as configured (service logs contain `[FWD] listen ...` for required ports)
- After reboot, verify that `[FWD]` listeners appear even if `eth0` carrier comes up late and that `[NTP]` retries continue until sync succeeds
- Do not expect arbitrary printer-side `TTP` edits from the CLARiTY UI to arrive via async push; ZBC still requires polling/readback for generic `CURRENT_PARAMETERS` deltas

## 6. Ownership
- Main project owner context: MAS-004_RPI-Databridge
- Subprojects are operational dependencies and must be checked in each release cycle.
- Shared protocol library also participates in the multi-repo release flow: `MAS-004_ZBC-Library`

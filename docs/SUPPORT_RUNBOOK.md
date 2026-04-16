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
  - preferred alias on this laptop: `ssh mas004-rpi-live "cd /opt/MAS-004_RPI-Databridge && git status -sb"`
- Reinstall package safely after pull (prevents stale `build/` artifacts):
  - `ssh pi@10.27.67.68 "cd /opt/MAS-004_RPI-Databridge && rm -rf build && ./.venv/bin/python -m pip install --no-deps --no-build-isolation --no-cache-dir --force-reinstall ."`
- Restart:
  - `ssh pi@10.27.67.68 "sudo systemctl restart mas004-rpi-databridge.service"`
- Logs:
  - `ssh pi@10.27.67.68 "sudo journalctl -u mas004-rpi-databridge.service -n 120 --no-pager"`

## 3.1 LIVE SSH Access Notes
- Preferred access method on this laptop:
  - key: `C:/Users/Egli_Erwin/.ssh/mas004_rpi210_ed25519`
  - aliases: `mas004-rpi`, `mas004-rpi-live`
- Direct host access `ssh pi@192.168.210.20` is also configured to use the same key automatically.
- Fallback password for LIVE when key-based access is unavailable: `raspberry`

## 3.2 Offline Mode
- If TEST and LIVE are both unreachable, continue in local offline mode only.
- Before starting substantial work, capture the local multi-repo state:
  - `powershell -ExecutionPolicy Bypass -File scripts/mas004_multirepo_status.ps1`
  - `git -C ..\\MAS-004_ESP32-PLC-Bridge status --short --branch`
  - `git -C ..\\MAS-004_ESP32-PLC-Firmware status --short --branch`
  - `git -C ..\\MAS-004_VJ3350-Ultimate-Bridge status --short --branch`
  - `git -C ..\\MAS-004_VJ6530-ZBC-Bridge status --short --branch`
  - `git -C ..\\MAS-004_ZBC-Library status --short --branch`
  - `git -C ..\\MAS-004_SmartWickler status --short --branch`
- Do not claim TEST/LIVE synchronization while reachability is missing.
- Do not infer or overwrite runtime settings from stale exports while offline.
- `mas004_release_ops` owns the deferred TEST/LIVE sync backlog until connectivity returns.

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
- UI reachable: `/`, `/ui/test`, `/ui/params`, `/ui/motors`, `/ui/settings`
- Smart Wickler proxy UIs reachable when needed:
  - `/ui/winders/unwinder`
  - `/ui/winders/rewinder`
- API health: `/health`
- Outbox not growing unexpectedly
- If Microtom callback delivery shows a staircase delay pattern around `10s / 20s / 30s ...`, inspect `journalctl` for `[OUTBOX:primary]` timeouts:
  - this indicates the primary peer `POST /api/inbox` is not returning a `2xx` within `http_timeout_s`
  - the expected Microtom contract is: answer `2xx` immediately, then process asynchronously
- The sender runtime is now split into two lanes:
  - `[OUTBOX:primary]` handles only `peer_base_url`
  - `[OUTBOX:aux]` handles `peer_base_url_secondary` and any other non-primary callback targets
- Expectation after the lane split:
  - a slow/timing-out primary Microtom inbox may still back up primary jobs
  - but it must no longer delay secondary/custom callback targets
- Shared-secret and peer URL still valid after config changes
- When probing `POST /api/inbox` from Windows PowerShell, prefer `curl.exe --data-binary @payload.json` or `Invoke-RestMethod`:
  - naive inline JSON quoting can arrive mangled as `{"msg":"{\\"}` and creates false routing/callback conclusions
- `TTS0001` present in `/ui/params` and resolves to the expected numeric printer state
- Expect the 6530 async path to be primary for online/offline/warning/fault changes; the poller is fallback/reconciliation only.
- Expect critical 6530 status flips (online/offline/warning/fault) to arrive via high-priority AIS and update `STATUS[...]` / `STS[...]` workbook rows immediately from the async snapshot, before the slower summary settle finishes.
- Live TEST proof for the raw printer path:
  - `CMD_START` -> `AIR` online in about `46 ms`
  - `CMD_STOP` -> `AIR` offline in about `6 ms`
  - `CMD_SHUTDOWN` / `CMD_STARTUP` have no dedicated async state tag on TEST, so `6 <-> 0` confirmation must come from fresh summary state
- Expect the async owner session to stay up via keepalive; if live 6530 writes suddenly hang or drift back to `NAK_DeviceComm`, verify the owner session did not die and that no second daemon/client has taken over `3002`.
- If a live `TTS0001` write returns `NAK_DeviceComm`, verify whether the owner-session request really used the widened per-request response timeout; the listener itself still keeps a short unsolicited receive timeout for AIR handling.
- For the new Oriental motor setup page:
  - `/api/motors/overview` must return JSON with all 9 configured drives even when the ESP motor endpoint is offline or still in simulation
  - a motor that is marked as simulated on `/ui/motors` must not trigger live ESP polling during the refresh cycle
  - simulated motors must show last known cached values or defaults, not a repeated endpoint warning
  - while typing into `/ui/motors`, periodic refresh must not overwrite focused or dirty inputs
  - `MOTOR <id> SET ...` / `SAVE` / `MOVE_REL_*` must echo through the ESP endpoint before any TEST deployment is claimed successful
- For Smart Wickler integration:
  - `Abwickler` and `Aufwickler` endpoints are configured in `/ui/settings`
  - expected defaults are `192.168.2.104:3011` and `192.168.2.105:3012`
  - no TCP forwarding is expected for these two devices
  - if live mode is disabled or the endpoint is offline, the Raspi proxy UI must still open and show a stable simulation/offline state instead of failing hard
- If the Videojet logo disappears from the Raspi UI again, check `/ui/assets/videojet-logo.jpg` first:
  - the asset should now come either from the installed package data or from the repo fallback path on the Raspberry
- If `TTS0001=3` ever returns `ACK_TTS0001=0`, treat that as a regression: the ACK must follow the async-observed settled workbook state, not a stale synchronous verify snapshot.
- If a 6530 state change reaches Microtom late, inspect whether the delay came from ESP mirror attempts rather than the ZBC async path; Microtom delivery should now be queued before ESP mirroring starts.
- If `TTS0001=3` fails only from `6 SHUTDOWN`, treat that as a regression in live-summary confirmation for the `STARTUP -> START` sequence.
- ESP write smoke test for `TTS0001`:
  - from `6`: `TTS0001=0` -> printer starts up into `0 OFFLINE`
  - from `0`: `TTS0001=3` -> printer goes online
  - from `6`: `TTS0001=3` -> printer runs `STARTUP` then `START`
  - `TTS0001=6` -> printer shuts down
- TEST validation after the 2026-03-26 ESP firmware update:
  - direct smoke on `192.168.2.101:3010` should now accept and echo `TTS0001`, `TTP00073`, `TTP00076`
  - if those three ever fall back to `NAK_UnknownParam` again, treat that as an ESP firmware/seed regression, not a 6530 async timing issue
- Expect direct rejection for `TTS0001=1`, `2`, `4`, `5`; these are observed composite warning/fault states, not direct control targets
- `TTE` / `TTW` / `TTS` printer-originated updates only forward to Microtom / ESP when the workbook `R/W:` / `ESP32 R/W:` flags allow it
- After a successful 6530 write, verify that related status rows (`TTP00073`, `TTP00076`, `TTS0001`, relevant `TTE*` / `TTW*`) follow immediately without waiting for the next periodic poll cycle.
- Queue semantics are lossless for non-consecutive state flips; if the printer goes `ONLINE -> OFFLINE -> ONLINE`, all three state changes must appear in order at the peer.
- NTP configured values visible in `/ui/settings` and service logs contain `[NTP]` entries
- TCP relay listeners started as configured (service logs contain `[FWD] listen ...` for required ports)
- After reboot, verify that `[FWD]` listeners appear even if `eth0` carrier comes up late and that `[NTP]` retries continue until sync succeeds
- Do not expect arbitrary printer-side `TTP` edits from the CLARiTY UI to arrive via async push; ZBC still requires polling/readback for generic `CURRENT_PARAMETERS` deltas
- If live 6530 status delivery looks lossy, first confirm that `mas004-vj6530-zbc-bridge.service` is not running in parallel on the same Raspberry. The Databridge owns the live `3002` session path; the standalone bridge daemon is for diagnostics and should stay disabled unless deliberately needed.
- For exclusive printer diagnostics, stop `mas004-rpi-databridge.service` first; the TEST 6530 timed out second parallel control sessions while the live async owner session was already active.
- `scripts/mas004_multirepo_sync.ps1 -RestartServices` now respects systemd `disabled`/`masked` state and will not revive intentionally parked services on TEST/LIVE.

## 6. Ownership
- Main project owner context: MAS-004_RPI-Databridge
- Subprojects are operational dependencies and must be checked in each release cycle.
- Shared protocol library also participates in the multi-repo release flow: `MAS-004_ZBC-Library`

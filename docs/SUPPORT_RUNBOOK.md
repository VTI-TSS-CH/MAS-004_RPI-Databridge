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
   - `ssh pi@10.141.94.213 "systemctl status mas004-rpi-databridge.service --no-pager"` (production after cutover)
   - `ssh pi@192.168.210.20 "systemctl status mas004-rpi-databridge.service --no-pager"` (LIVE)
   - `curl -k https://<raspi-ip>:8080/health`

## 1.1 Production IBN 10.141.94.x
- This section prepares the former TEST machine as the next production/commissioning stand.
- Current/bootstrap Raspi address before network cutover:
  - SSH: `pi@10.27.67.68`
  - UI/API: `https://10.27.67.68:8080`
- Final production addresses:
  - Raspi: `10.141.94.213/24`, gateway `10.141.94.1`
  - Laptop / Microtom testtool: `10.141.94.212`
  - TTO VJ6530: `10.141.94.214:3002`
  - Laser VJ3350: `10.141.94.215:20000`
  - Abwickler: `10.141.94.216:3011`
  - Aufwickler: `10.141.94.217:3012`
  - ESP and Moxa on `eth1` stay unchanged: `192.168.2.101:3010`, `192.168.2.102:502`, `192.168.2.103:502`
- Prepared files:
  - `scripts/production_topology_10_141_94.json`
  - `scripts/production_commissioning_config_patch_10_141_94.json`
  - `scripts/mas004_production_ibn.ps1`
- Dry plan:
  - `powershell -ExecutionPolicy Bypass -File scripts/mas004_production_ibn.ps1 -Phase Plan`
- Local precheck:
  - `powershell -ExecutionPolicy Bypass -File scripts/mas004_production_ibn.ps1 -Phase Precheck`
- Tomorrow's guided execution order:
  - connect laptop to the current Raspi network and verify `ssh pi@10.27.67.68`
  - deploy Raspi code: `powershell -ExecutionPolicy Bypass -File scripts/mas004_production_ibn.ps1 -Phase DeployRaspiBootstrap -Execute`
  - stage the production Databridge config: `powershell -ExecutionPolicy Bypass -File scripts/mas004_production_ibn.ps1 -Phase ApplyRaspiRuntimeConfig -Execute`
  - switch the OS network: `powershell -ExecutionPolicy Bypass -File scripts/mas004_production_ibn.ps1 -Phase ApplyRaspiNetwork -Execute`
  - change the laptop NIC to `10.141.94.212/24`, gateway `10.141.94.1`
  - verify the new path: `powershell -ExecutionPolicy Bypass -File scripts/mas004_production_ibn.ps1 -Phase StatusAfterCutover -Execute`
  - deploy the ESP via USB on the Raspi: `powershell -ExecutionPolicy Bypass -File scripts/mas004_production_ibn.ps1 -Phase DeployEspFromRaspi -Execute`
  - deploy Abwickler and Aufwickler one after another via USB on the laptop: `powershell -ExecutionPolicy Bypass -File scripts/mas004_production_ibn.ps1 -Phase DeployWicklerUsb`
  - Smart Wicklers stay autonomous; they are intentionally not connected to the Raspi USB gateway for flashing
- Important cutover note:
  - `ApplyRaspiNetwork` intentionally changes the OS network and can drop the current SSH session.
  - Do not run it until the laptop can be moved to `10.141.94.212/24`.
  - The commissioning config uses `peer_base_url = https://10.141.94.212:9090` while the laptop Microtom simulator/testtool is the active peer; replace this with the final Microtom endpoint before handover if Microtom uses another host.

## 2. Local Commands
- Install env:
  - `python -m venv .venv`
  - `.\.venv\Scripts\Activate.ps1`
  - `python -m pip install -U pip`
  - `python -m pip install -e .`
- Run app:
  - `mas004-databridge`
- Refresh runtime parameters from a new workbook:
  - upload through `/api/params/import` with the current master `.xlsx`
  - expected side effect: SQLite metadata and `master_params_xlsx_path` are updated from the same workbook payload
- Refresh the repository master workbook copies from the current external engineering files:
  - `python scripts/sync_master_workbooks.py`
  - expected side effects:
    - repo copy `master_data/Parameterliste SAR41-MAS-004.xlsx` is refreshed
    - repo copy `master_data/SAR41-MAS-004_SPS_I-Os.xlsx` is refreshed
    - `MAP0066` exists and currently defaults to `8000`
    - user notes before `KI:` are folded into regenerated `KI:` texts
    - the full `KI-Anweisungen:` column is rewritten with `KI:` texts
- After workbook rights or ESP-relevant MAP defaults change while TEST is offline:
  - create a local workbook-derived SQLite snapshot from the RPI repo
  - regenerate ESP seeds from that SQLite snapshot in `MAS-004_ESP32-PLC-Firmware`
  - run the PlatformIO build locally before committing the firmware repo
- Refresh hardware IO mappings from the IO workbook:
  - import through the protected Machine-Setup I/O page or the IO import endpoint
  - source workbook: `master_data/SAR41-MAS-004_SPS_I-Os.xlsx`
  - expected side effect: the IO catalog, point states and workbook-backed labels are updated from the same payload

## 3. Pi Commands
- TEST update:
  - `ssh pi@10.27.67.68 "cd /opt/MAS-004_RPI-Databridge && git pull --ff-only"`
- Production update after cutover:
  - `powershell -ExecutionPolicy Bypass -File scripts/mas004_multirepo_sync.ps1 -Target production -RestartServices`
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

## 3.3 Machine-Setup Login
- Protected menu entry: `/ui/machine-setup`
- Protected credentials:
  - user: `Admin`
  - password: `VideojetMAS004!`
- Scope:
  - `/ui/machine-setup/commissioning`
  - `/ui/machine-setup/backups`
  - `/ui/machine-setup/motors`
  - `/ui/machine-setup/process`
  - `/ui/machine-setup/io`
  - `/ui/machine-setup/winders/unwinder`
  - `/ui/machine-setup/winders/rewinder`
  - `/api/commissioning/*`
  - `/api/backups/*`
  - `/api/motors/*`
  - `/api/machine/*`
  - `/api/io/*`
  - `/api/winders/*`
- Legacy `/ui/motors` and `/ui/winders/*` URLs are only compatibility redirects into the protected section.

## 3.4 Commissioning Assistant
- Main protected page: `/ui/machine-setup/commissioning`
- Use this page for:
  - first hardware/software bring-up of a machine
  - repeated commissioning after unfinished work
  - recording which steps were successful, failed, skipped or reused
- Supported operator modes:
  - full run
  - incomplete-only rerun
- Commissioning notes:
  - use `full run` for a new machine or after a major rebuild
  - use `incomplete-only` when an earlier commissioning attempt already proved some steps
  - do not mark hardware-dependent steps as successful without observing the real machine behavior
- Current MAS-004-oriented commissioning order on the page:
  - bootstrap:
    - machine identity
    - ETH0/ETH1 and Raspi runtime
    - parameter + IO workbook availability
  - peers:
    - Microtom primary peer
    - optional engineering/VPN secondary peer
  - controller / field network:
    - ESP endpoint
    - ESP realtime IO/process image
    - Moxa 1 / Moxa 2
    - Moxa field IOs
  - printers:
    - VJ6530 endpoint + TTO handshake
    - VJ3350 endpoint + Laser handshake
  - winders:
    - Abwickler
    - Aufwickler
    - winder stop IOs
  - motion:
    - global Oriental baseline
    - table X/Z axes
    - label drive
    - sensor axes
    - camera axis
    - laser guard axis
    - label guides
  - validation:
    - encoders
    - label sensors
    - cameras
    - machine IO / safety / UPS
    - MAS001/MAS0002 flow
    - production logs / `MAS0030`
    - final backup baseline
- If the target Raspi is not yet reachable with the Databridge UI, use the bootstrap workflow first and then continue from the commissioning page.

## 3.5 Backups / Restore / Clone
- Main protected page: `/ui/machine-setup/backups`
- Manage these machine-local metadata fields there:
  - `machine_serial_number`
  - `machine_name`
  - `backup_root_path`
- Backup types:
  - settings backup
  - full backup
- Backup policy:
  - use settings backup before risky parameter/workbook/runtime changes
  - use full backup before clone preparation, major milestones or software/hardware handover
  - exported bundles should be archived outside the Raspi as well
- Restore policy:
  - verify machine identity before restoring
  - expect a service restart after restore
  - after restore, validate `/health`, `/ui/settings`, `/ui/machine-setup`, workbook presence and required protected pages
- Command-line helpers:
  - restore helper:
```powershell
python scripts/mas004_restore_backup.py --bundle .\exports\<bundle>.zip --apply-repos
```
  - bootstrap helper for a fresh target:
```powershell
python scripts/mas004_machine_bootstrap.py discover --subnet 192.168.210.0/24
python scripts/mas004_machine_bootstrap.py apply-full-backup --target pi@192.168.210.20 --bundle .\exports\<bundle>.zip --apply-repos
```
- The bootstrap path is the preferred future workflow for bringing a fresh Raspberry onto the known MAS-004 baseline before continuing in the web UI.
- Commissioning/backup deploy verification on a reachable Raspberry:
  - `ssh <pi> "systemctl is-active mas004-rpi-databridge.service"`
  - `ssh <pi> "cd /opt/MAS-004_RPI-Databridge && ./.venv/bin/python -m unittest tests.test_machine_setup_auth tests.test_machine_commissioning_backups"`
  - `ssh <pi> "curl -k https://127.0.0.1:8080/health"`
  - `ssh <pi> "curl -k -sS -D - -o /dev/null https://127.0.0.1:8080/ui/machine-setup/commissioning"`
- If the service runs from the venv-installed package, update it after a repo patch before restarting:
  - `ssh <pi> "cd /opt/MAS-004_RPI-Databridge && ./.venv/bin/pip install --no-deps ."`
- Secondary/VPN peer check helper:
  - local Microtom simulator command on this engineering laptop:
```powershell
cd "D:\Users\Egli_Erwin\Veralto\DE-SMD-Support-Switzerland - Documents\26_VS_CODE\SAR41-MAS-004_Roche_LSR_TTO\Raspberry-PLC\Microtom-Simulator"
.\.venv\Scripts\python.exe .\microtom_sim.py --host 0.0.0.0 --port 9090 --raspi https://192.168.210.20:8080 --https --certfile .\microtom.crt --keyfile .\microtom.key
```
  - local health probe:
    - `curl.exe -k https://127.0.0.1:9090/health`
  - LIVE-side reachability probe:
    - `ssh mas004-rpi-live "curl -k -sS https://192.168.5.2:9090/health"`
  - important interpretation:
    - if `[OUTBOX:primary]` still logs `HTTP 404: "No active developers found to forward the request"` for `http://192.168.210.10:81/api/inbox`, that remains a primary-peer/Microtom-side issue
    - a missing or stopped secondary/VPN peer would only affect `[OUTBOX:aux]`, not the primary lane

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
- Commissioning and restore actions may be prepared locally while offline, but they stay pending until the respective target machine is reachable again.

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
- UI reachable: `/`, `/ui/test`, `/ui/params`, `/ui/machine-setup`, `/ui/settings`
- Protected commissioning UI reachable: `/ui/machine-setup/commissioning`
- Protected backups UI reachable: `/ui/machine-setup/backups`
- Process UI reachable: `/ui/machine-setup/process`
- UI reachable for hardware IO operations: `/ui/machine-setup/io`
- Smart Wickler proxy UIs reachable when needed:
  - `/ui/machine-setup/winders/unwinder`
  - `/ui/machine-setup/winders/rewinder`
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
- New ESP/Raspi machine-event contract for later realtime label logic:
  - ESP may actively push condensed events as `EVT <json>` over the existing push socket
  - currently supported Raspi event types:
    - `machine_state`
    - `label_complete`
- Expect the async owner session to stay up via keepalive; if live 6530 writes suddenly hang or drift back to `NAK_DeviceComm`, verify the owner session did not die and that no second daemon/client has taken over `3002`.
- If a live `TTS0001` write returns `NAK_DeviceComm`, verify whether the owner-session request really used the widened per-request response timeout; the listener itself still keeps a short unsolicited receive timeout for AIR handling.
- For the new Oriental motor setup page:
  - `/api/motors/overview` must return JSON with all 9 configured drives even when the ESP motor endpoint is offline or still in simulation
  - the Machine-Setup login must be required before `/ui/machine-setup/motors` and `/api/motors/*` become reachable
  - a motor that is marked as simulated on `/ui/machine-setup/motors` must not trigger live ESP polling during the refresh cycle
  - simulated motors must show last known cached values or defaults, not a repeated endpoint warning
  - if all 9 motors are currently simulated, the page should settle on a stable loaded status and pause background refresh instead of alternating with `loading...`
  - while typing into `/ui/machine-setup/motors`, periodic refresh must not overwrite focused or dirty inputs
  - `MOTOR <id> SET ...` / `SAVE` / `MOVE_REL_*` must echo through the ESP endpoint before any TEST deployment is claimed successful
- For hardware IO / Moxa integration:
  - `/ui/machine-setup/io` must show the imported IO workbook catalog even if the field hardware is offline
  - `ESP32-PLC58` stays on the realtime side and should only receive the IO snapshot/control traffic it actually needs
  - the two `Moxa ioLogik E1211` modules are handled as slow field IO on the Raspi side
  - Moxa simulation should stay enabled by default on live/test until the networked hardware is intentionally validated
  - Raspberry hardware IO remains simulation-first until an approved Industrial Shields runtime library installation is available
  - if the IO workbook changes, re-import it so the page and the backend stay aligned with the sheet
- For parameter sync to the ESP:
  - `ESP32 R/W = N` means no ESP transfer
  - `ESP32 R/W = R` means Raspi/Microtom is leading and the ESP receives updates via `SYNC <key>=<value>`
  - `ESP32 R/W = W` or `R/W` remains the ESP-leading/direct write path
  - if a Microtom write to a MAP value starts returning `NAK_ReadOnly` from the ESP, verify that the Raspi and firmware both include the 2026-04-21 `SYNC` contract
- For Smart Wickler integration:
  - `Abwickler` and `Aufwickler` endpoints are configured in `/ui/settings`
  - expected defaults are `192.168.210.23:3011` and `192.168.210.24:3012`
  - direct eth0 reachability is expected; no TCP forwarding is used anymore
  - if live mode is disabled or the endpoint is offline, the Raspi proxy UI must still open inside the main Machine-Setup shell and show a stable simulation/offline state instead of failing hard
- For commissioning/backup handling:
  - protected `Machine-Setup` login must be required before commissioning or backup APIs are usable
  - machine serial number and machine name should be set before creating qualification-relevant backup bundles
  - after a restore, verify whether the service restart happened cleanly and whether the expected workbook/runtime payload is present again
  - in offline mode, exported bundles and commissioning notes may be prepared locally, but no TEST/LIVE completion should be claimed before the real target has been revalidated
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
- After reboot, verify that `[NTP]` retries continue until sync succeeds and that no obsolete `[FWD]` relay logs are emitted anymore
- Do not expect arbitrary printer-side `TTP` edits from the CLARiTY UI to arrive via async push; ZBC still requires polling/readback for generic `CURRENT_PARAMETERS` deltas
- If live 6530 status delivery looks lossy, first confirm that `mas004-vj6530-zbc-bridge.service` is not running in parallel on the same Raspberry. The Databridge owns the live `3002` session path; the standalone bridge daemon is for diagnostics and should stay disabled unless deliberately needed.
- For exclusive printer diagnostics, stop `mas004-rpi-databridge.service` first; the TEST 6530 timed out second parallel control sessions while the live async owner session was already active.
- `scripts/mas004_multirepo_sync.ps1 -RestartServices` now respects systemd `disabled`/`masked` state and will not revive intentionally parked services on TEST/LIVE.

## 6. Ownership
- Main project owner context: MAS-004_RPI-Databridge
- Subprojects are operational dependencies and must be checked in each release cycle.
- Shared protocol library also participates in the multi-repo release flow: `MAS-004_ZBC-Library`

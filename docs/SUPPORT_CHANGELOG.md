# SUPPORT_CHANGELOG - MAS-004_RPI-Databridge

## 2026-03-25 (6530 Lossless State Forwarding + Async Stabilization)
- The 6530 async listener now prefers the already verified `vj6530-tcp-no-crc` transport profile instead of re-probing every async session startup; autodetect remains fallback only.
- The async loop now treats idle `socket.timeout` on the unsolicited receive path as a healthy wait state instead of tearing the subscription down as an error.
- Async status refreshes now use a short summary settle window after online/offline/warning/fault events so follow-up state transitions like `OFFLINE -> SHUTDOWN` or `OFFLINE -> ONLINE` are more likely to be captured immediately.
- The 6530 fallback poller now stands down while the async channel is still healthy, but the async-health age-out window was reduced so fallback reconciliation resumes much sooner after a broken async session.
- The background 6530 poll loop now reuses one bridge client while host/port/timeout stay unchanged, so profile knowledge is no longer thrown away on every cycle.
- Successful 6530 writes from Microtom or ESP now trigger an immediate workbook status resync so related status rows such as `TTP00073`, `TTP00076`, `TTS0001`, `TTE*`, `TTW*` do not wait for the next background cycle.
- The async loop now proactively rotates 6530 subscriptions every 30s and reconnects near-immediately after printer-driven `socket closed` events, reducing blind windows between state pushes.
- The async listener now marks the channel healthy immediately after a successful subscription, so the fallback poller does not race the first startup summary refresh.
- The multi-repo sync script now skips restarting services that are explicitly `disabled` or `masked` on the target, so intentionally parked side daemons do not reappear during routine sync.
- Outbox dedupe is now lossless for non-consecutive state changes:
  - consecutive duplicate values may still collapse
  - alternating sequences like `3 -> 0 -> 3` are preserved as separate queued deliveries

## 2026-03-25 (6530 Event Rights + `TTS0001` Status Channel)
- Added protocol/runtime support for `TTS0001` as the dedicated numeric TTO status parameter (`STATUS[PRINTER_STATE_CODE]`).
- Numeric state mapping is now:
  - `0=OFFLINE`
  - `1=OFFLINE_WARNING`
  - `2=OFFLINE_FAULT`
  - `3=ONLINE`
  - `4=ONLINE_WARNING`
  - `5=ONLINE_FAULT`
  - `6=SHUTDOWN`
- ESP writes to `TTS0001` now drive the printer through the existing 6530 control path for the directly commandable states `0`, `3`, `6`.
- Live refinement on TEST:
  - `TTS0001=3` from `SHUTDOWN (6)` now executes as `STARTUP` then `START`
  - `TTS0001=0` from `SHUTDOWN (6)` now executes as `STARTUP`
  - the derived state targets `1`, `2`, `4`, `5` are now rejected cleanly instead of surfacing as misleading `NAK_DeviceComm`
- The 6530 async listener and fallback poller now respect the workbook access flags before forwarding printer-originated updates to Microtom.
- The fallback poller now also keeps workbook status/error mappings from `TTP` / `TTS` in sync instead of only `TTE` / `TTW`.
- Added regression coverage for:
  - `TTS0001` protocol normalization and ESP write handling
  - active async push of `TTE` and `TTS`
  - poller-side `TTS` updates with Microtom access denied

## 2026-03-25 (Retry Once Before `NAK_DeviceComm` on VJ6530)
- Current-parameter reads and writes on the 6530 live path now retry once before falling back to cached values or bubbling up to the generic `NAK_DeviceComm`.
- Goal: absorb transient profile-detect / session timeouts on `3002` that would otherwise fail a user write even though the immediate retry succeeds.
- Added regression tests for:
  - a flaky current-parameter read that succeeds on the second attempt
  - a flaky current-parameter write that succeeds on the second attempt

## 2026-03-25 (Respect `esp_rw = N` for MA* Live Routing)
- Fixed the router so `MAP` / `MAS` / `MAE` / `MAW` parameters with `esp_rw = N` stay Raspi-local even when ESP live mode is enabled.
- This closes the mismatch where the simulation path accepted local-only parameters such as `MAS0029`, but the real ESP path still forwarded them and collapsed the device-side rejection into `NAK_DeviceRejected`.
- Added a regression test for:
  - local-only `MAS0029` with `esp_rw = N`
  - ESP-routed `MAS0026` with `esp_rw = W`

## 2026-03-25 (Canonical Sub-Agent Rehydration Policy)
- Clarified that the MAS-004 sub-agents are canonical long-lived project roles even if individual agent threads disappear from the UI.
- Documented the required fallback for platform slot/session limits:
  - re-create missing agents under the exact same canonical names before delegating further work
  - keep ownership boundaries unchanged
  - report which named agents are live versus temporarily parked behind the current slot limit

## 2026-03-25 (Master Chat / Sub-Agent Orchestration Blueprint)
- Added `docs/MAS-004_Roche_Master_Chat.md` as the recommended bootstrap instruction for the future master chat `MAS-004_Roche`.
- Defined a stable long-term sub-agent topology with dedicated owners for:
  - documentation
  - main Databridge core
  - parameter master data / Excel mappings
  - ESP32 bridge
  - ESP32 firmware
  - VJ3350 bridge
  - VJ6530 bridge
  - ZBC shared protocol library
  - release / deployment operations
- Documented coordination rules so future multi-repo work can scale without overlapping file ownership or mixing protocol, business and deployment responsibilities.

## 2026-03-25 (Local Timezone for Log UI and Logfiles)
- Added `mas004_rpi_databridge/timeutil.py` as the central source for local system timezone resolution.
- Daily logfiles, production logfiles and DB-backed log downloads now format timestamps via the current Raspi timezone instead of relying on implicit process-local UTC behavior.
- Test UI log windows now use server-provided `ts_display` values instead of browser-side `toISOString()` formatting.
- Goal: all log windows and logfile exports follow the synchronized Raspi local time consistently.

## 2026-03-25 (Settings UI: System Time / Timezone / NTP Status)
- Added token-protected endpoint `GET /api/system/time`.
- `ui/settings` now shows:
  - current local system time
  - current system timezone
  - synchronized yes/no state
  - OS NTP service state
  - detected OS time source
- This status is read-only and complements the existing Databridge-side `ntp_server` / `ntp_sync_interval_min` settings.
- Clarified operational expectation: the Microtom LIVE Raspberry remains in timezone `Europe/Zurich`.

## 2026-03-25 (Production Logfiles via MAS0002 / MAS0029 / MAS0030)
- Added `mas004_rpi_databridge/production_logs.py`.
- Production log capture is now controlled by MAS status values:
  - `MAS0002=1` starts a production log session
  - `MAS0002=2` stops the session and marks the last production logs as ready
- Added new workbook parameters:
  - `MAS0029` production label / logfile suffix (string)
  - `MAS0030` production-logfiles-ready flag (`0|1`)
- `logstore.py` now mirrors active communication into separate production TXT files:
  - `gesamtanlage_<MAS0029>.txt`
  - `esp32_plc_<MAS0029>.txt`
  - `tto_6530_<MAS0029>.txt`
  - `laser_3350_<MAS0029>.txt`
- Added Microtom pull endpoints:
  - `GET /api/production/logfiles/list`
  - `GET /api/production/logfiles/download`
  - `POST /api/production/logfiles/ack`
- When a production stops, the Raspi now raises `MAS0030=1` so Microtom can detect that the last production logs are ready to fetch.
- Production-log downloads are now consumptive:
  - downloading a production TXT file removes it from the Raspi immediately
  - after the final production file is downloaded, the Raspi automatically sets `MAS0030=0`
  - that reset is also forwarded automatically to Microtom via callback `/api/inbox`
- A new production cannot be started with `MAS0002=1` while old production files are still pending:
  - the Raspi now returns `MAS0002=NAK_ProductionLogfilesPending`
- Daily and production TXT logfiles are now enriched with workbook metadata:
  - parameter `Name`
  - parameter `Message` / description text

## 2026-03-25 (LIVE/Test State Merge for Microtom Rollout)
- Reconciled the code baseline between the TEST branch work and the current Microtom LIVE system.
- Confirmed that LIVE runtime settings remain external in `/etc/mas004_rpi_databridge/config.json` and are not touched by repo deployment.
- Captured the current LIVE runtime snapshot in `docs/PROJECT_CONTEXT.md` so the same values can later be mirrored to TEST when the TEST Raspberry is reachable again.
- Prepared LIVE deployment to bring these repo states in line:
  - `MAS-004_RPI-Databridge`
  - `MAS-004_ESP32-PLC-Bridge`
  - `MAS-004_VJ3350-Ultimate-Bridge`
  - `MAS-004_VJ6530-ZBC-Bridge`
  - `MAS-004_ZBC-Library`

## 2026-03-17 (TEST IP Change to 10.27.67.68)
- Changed the TEST target from `10.27.67.69` to `10.27.67.68`.
- Updated deployment target metadata, project context and runbook documentation.
- TEST Raspi network and HTTPS endpoint are now expected at:
  - SSH: `pi@10.27.67.68`
  - UI/API: `https://10.27.67.68:8080`

## 2026-03-13 (6530 Async Primary + Versioned Master Workbook)
- Added `mas004_rpi_databridge/vj6530_runtime.py` to track whether the 6530 async channel is currently healthy.
- Refactored `mas004_rpi_databridge/vj6530_async_listener.py`:
  - async subscription now keeps one live ZBC session and resolves `STATUS[...]`, `TTE`, `TTW` updates from the same summary channel
  - this avoids the previous second-connection timeout pattern against the real printer
- `mas004_rpi_databridge/service.py` now treats polling as fallback only:
  - if async is healthy, periodic `TTE` / `TTW` polling is skipped
  - polling resumes automatically if async ages out or fails
- `mas004_rpi_databridge/device_bridge.py` now serves `STATUS[...]` and `IRQ{...}` reads from the Raspi-cached state instead of forcing a live printer roundtrip for every Microtom read.
- `FRQ[CURRENT_PARAMETERS]` reads now fall back to the Raspi-cached TTO value if the live 6530 archive read stalls or times out.
- 6530 async retries now back off progressively instead of hammering the printer every 2s during unstable third-party access on other ports.
- Added background 6530 cache warmup at router startup so the first `TTP` access after a restart is usually already primed.
- Added a repo-tracked master workbook copy:
  - `master_data/Parameterliste SAR41-MAS-004_V11.11.25.xlsx`
  - this is the live-updated workbook with `ESP32 R/W:` and current TTO defaults
- `/api/params/import` now persists the uploaded workbook as Raspi-side master copy at `/var/lib/mas004_rpi_databridge/master/Parameterliste_master.xlsx`.

## 2026-03-13 (6530 Polling + Startup Crash Fix)
- Added `mas004_rpi_databridge/vj6530_poller.py`.
- The Raspi now polls all workbook-mapped `TTE` / `TTW` states from the real 6530 by reusing one summary read per cycle.
- Only changed fault/warning states are persisted locally and forwarded to Microtom.
- Added `vj6530_poll_interval_s` to config, defaults and Settings UI.
- Fixed an installed-package startup regression:
  - `_vj6530_bridge.py` now discovers sibling repos robustly even when the main package runs from `site-packages`
  - this fixes the crash that caused `https://10.27.67.68:8080/api/inbox` to refuse connections

## 2026-03-13 (ZBC Library Integration)
- Added `MAS-004_ZBC-Library` as a new managed subproject.
- Extended multi-repo status/sync scripts to include the ZBC library.
- Added bundle-based Pi synchronization for repos without a central Git remote.
- `MAS-004_VJ6530-ZBC-Bridge` has been switched to consume the shared ZBC library.
- Live writeback against the real 6530 is now proven through `FTX[CURRENT_PARAMETERS]`.

## 2026-03-13 (TTO Mapping Routed Live Through ZBC)
- Main-project Excel import now reads the `ZBC Mapping:` column into `param_device_map.zbc_mapping`.
- Database schema migrates existing installations automatically by adding `zbc_mapping` if missing.
- `DeviceBridge` now prefers workbook-based ZBC mappings for `TTP`, `TTE`, `TTW`:
  - `FRQ[CURRENT_PARAMETERS]/...` -> live read/write via the 6530 current-parameter archive
  - `IRQ{LEI,ERR}/Fault[...]` -> live TTE state read
  - `IRQ{LEI,ERR}/Warning[...]` -> live TTW state read
- Live reads from devices now optionally promote the local default value, so the Raspi DB tracks the real device state instead of a stale spreadsheet default.
- The workbook updater now writes current TTP values from the real printer into `Default Value:`.

## 2026-03-13 (TTO Workbook Mapping via Live CurrentParameters)
- Extended `MAS-004_ZBC-Library` with a CLARiTY parameter-archive parser and a `request_current_parameters()` helper.
- Verified live against the 6530 that `FRQ[CURRENT_PARAMETERS]` returns `CurrentParameters.xml`.
- Updated `..\Parameterliste SAR41-MAS-004_V11.11.25.xlsx`:
  - added a `ZBC Mapping:` column
  - repaired `TTP00055` as `TextCommsAsyncNotificationsEnabled`
  - added new TTO parameters `TTP00064` .. `TTP00072`
- Added the reusable workbook updater:
  - `..\MAS-004_ZBC-Library\tools\update_tto_workbook.py`
- Fixed a PowerShell parsing bug in `scripts/mas004_multirepo_sync.ps1`:
  - bundle-sync `scp` target now uses `${resolvedSshHost}` correctly
  - without this, TEST sync for repos without a central remote aborted before transfer
- Fixed bundle-sync behavior for missing remote paths:
  - `MAS-004_ZBC-Library` can now be created on the Pi from the local bundle when `/opt/MAS-004_ZBC-Library` does not exist yet
- Fixed bundle-sync permissions for first deploy:
  - new repos are cloned in `/tmp` and then moved into `/opt/...` via `sudo`
  - this avoids `Permission denied` when the target folder does not yet exist

## 2026-03-13 (Configurable Forwarding Ports + ESP Port 3010)
- TCP forwarding no longer hardcodes device main ports.
  - listeners now follow the configured device ports: `esp_port`, `vj3350_port`, `vj6530_port`
  - this fixes the mismatch where the UI showed `ESP = 3010` but the runtime still listened on `3009`
- Hardened `mas004_rpi_databridge/tcp_forwarder.py` for parallel traffic:
  - shorter upstream connect timeout
  - larger socket buffers
  - `TCP_NODELAY` / keepalive
  - bidirectional pump threads per connection instead of one shared select/send loop
  - active connection tracking and cleaner shutdown on reconcile/restart
- Updated Settings UI text to describe configured main ports plus extra routed ports.
- Fixed ESP line-response parsing in `mas004_rpi_databridge/device_clients.py`:
  - only the first received line is now treated as the response payload
  - prevents heartbeat or extra trailing lines from corrupting `MAP`/`MAS` reads
- Fixed a forwarding regression in `mas004_rpi_databridge/tcp_forwarder.py`:
  - listener sockets no longer get a read timeout
  - this keeps the accept loop alive and fixes hanging routed ports such as `10.27.67.68:3010`
- Added active ESP push ingestion on the Raspi:
  - new listener `mas004_rpi_databridge/esp_push_listener.py`
  - binds on `eth1_ip:esp_port` when `esp_simulation=false`
  - accepts device-origin `MA*` lines, persists them locally and forwards them to Microtom via outbox
- Moved operation-line parsing into `mas004_rpi_databridge/protocol.py` so router and ESP-push path use the same syntax rules.

## 2026-03-04
- Added persistent support context files:
  - `docs/PROJECT_CONTEXT.md`
  - `docs/SUPPORT_RUNBOOK.md`
  - `docs/SUPPORT_CHANGELOG.md`
- Added multi-repo operations scripts:
  - `scripts/mas004_multirepo_status.ps1`
  - `scripts/mas004_multirepo_sync.ps1`
- Established policy: this repository is the main/orchestration project.
- Baseline local HEAD during this entry: `af82b02`.
- Added current sync snapshot:
  - Main repo synced on Pi.
  - Three Pi subproject repos still dirty and behind 1 (safe-skip mode).

## 2026-03-04 (Pi Safe Cleanup + Full Sync)
- Performed safe cleanup on Pi for all 3 subprojects:
  - Created backup branches and committed tracked local changes before sync.
  - Fast-forwarded `main` to `origin/main`.
- Backup branches created on Pi:
  - `MAS-004_ESP32-PLC-Bridge`: `backup/pi-pre-sync-mas-004_esp32-plc-bridge-20260304-083407`
  - `MAS-004_VJ3350-Ultimate-Bridge`: `backup/pi-pre-sync-mas-004_vj3350-ultimate-bridge-20260304-083551`
  - `MAS-004_VJ6530-ZBC-Bridge`: `backup/pi-pre-sync-mas-004_vj6530-zbc-bridge-20260304-083601`
- Added local Git excludes on Pi subprojects for runtime artifacts:
  - `.venv/`, `*.egg-info/`, `__pycache__/`, `**/__pycache__/`
- Result:
  - Local + Pi + Git are now fully synchronized for all 4 repositories.
  - All 4 systemd services on Pi are active.

## 2026-03-04 (Parallel Microtom Target)
- Added optional secondary Microtom target in config:
  - `peer_base_url_secondary`
- Extended outbound enqueue/routing to fan out to both configured targets:
  - primary `peer_base_url`
  - optional `peer_base_url_secondary`
- Added sender behavior for secondary target as best-effort:
  - failed sends to secondary are dropped (no retry backlog), to protect primary channel latency.
- Updated Settings UI (`/ui/settings`) to edit secondary peer URL.
- Updated default/example config files and project context docs.

## 2026-03-04 (TEST/LIVE Deployment Profiles)
- Added deployment target profile helper:
  - `scripts/mas004_deploy_targets.ps1`
- Updated multi-repo scripts to support `-Target test|live`:
  - `scripts/mas004_multirepo_status.ps1`
  - `scripts/mas004_multirepo_sync.ps1`
- TEST (`10.27.67.68`) is now default target for status/sync.
- LIVE (`192.168.1.20`) is blocked by default and requires:
  - `-Target live -AllowLive`
- Added unreachable-target handling so TEST sync can run safely even while test device is not connected.
- Added optional environment variable overrides for host/web target metadata.

## 2026-03-04 (NTP + TCP Relay for Device Ports)
- Added NTP configuration and runtime sync loop:
  - config keys: `ntp_server`, `ntp_sync_interval_min`
  - runtime worker: `mas004_rpi_databridge/ntp_sync.py`
  - Settings UI fields and config API mapping updated.
- Added TCP relay service from Raspi `eth0` to device hosts on `eth1`:
  - initial relay ports: `3007` (VJ6530), `3008` (VJ3350), `3009` (ESP32)
  - optional extra relay ports per device: `esp_forward_ports`, `vj3350_forward_ports`, `vj6530_forward_ports`
  - runtime worker: `mas004_rpi_databridge/tcp_forwarder.py`
  - started from `service.py` at app startup.
- NTP robustness fix:
  - `ntp_sync.py` now searches binaries also in `/usr/sbin`/`/sbin`, so `ntpdate` is detected in systemd service context.

## 2026-03-04 (TEST Raspi Setup Finalization + Deploy Hardening)
- Finalized TEST Raspi setup on `10.27.67.68`:
  - `eth0`: `10.27.67.68/24`, gateway `10.27.67.1`, DNS `10.28.193.4 10.27.30.201`
  - `eth1`: `192.168.2.100/24` without gateway
  - timezone set to `Europe/Zurich`
- Root-cause fixed for "new code not active after pull/install":
  - stale `build/` artifacts on Pi caused old package content to be reinstalled.
  - verified by missing `/api/ui/status/public` and absent `[NTP]/[FWD]` runtime logs.
- Deployment hardening:
  - `scripts/mas004_multirepo_sync.ps1` now performs on remote (when `-RestartServices` is used):
    - `rm -rf build`
    - `.venv/bin/python -m pip install --no-cache-dir --force-reinstall .`
    - service restart
- Verified runtime after reinstall:
  - endpoint `GET /api/ui/status/public` returns `200`
  - forwarding listeners active for the configured device ports
  - NTP sync successful against `10.27.30.201`

## 2026-03-12 (Boot Robustness Fixes)
- Fixed TEST/LIVE runtime behavior after reboot:
  - TCP forwarders now reconcile every 5 seconds and retry binds after `eth0` becomes available.
  - This prevents the boot race where `mas004-rpi-databridge` started before `eth0` had carrier and forwarding listeners stayed down.
- Improved NTP sync behavior:
  - command detection now uses explicit executable checks
  - failed sync attempts now report the real command error instead of the misleading "No supported NTP client found"
  - after a failed sync, retry happens after 15 seconds instead of waiting the full configured interval
- Hardened Pi package reinstall in `scripts/mas004_multirepo_sync.ps1`:
  - uses `--no-deps --no-build-isolation`
  - avoids dependency downloads during deploy, which is important when the Pi clock is wrong before first NTP sync

## Maintenance Rule
- Add one entry for every change that affects:
  - architecture
  - deployment flow
  - API contracts
  - multi-repo sync behavior



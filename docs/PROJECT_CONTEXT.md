# PROJECT_CONTEXT - MAS-004_RPI-Databridge

## Role
- Main project in the MAS-004 landscape.
- Runs on Raspberry PLC and provides the central data bridge to Microtom.
- Hosts the operator web UI and the test environment.
- Coordinates simulated/real sub-bridges for ESP32, VJ3350, VJ6530 and shared protocol libraries.

## Repository Scope
- Core app package: `mas004_rpi_databridge/`
- Web/API: `mas004_rpi_databridge/webui.py`
- Message reliability: `inbox.py`, `outbox.py`, `router.py`, `service.py`, `http_client.py`, `watchdog.py`
- Background ops: `ntp_sync.py` (periodic time sync), `tcp_forwarder.py` (eth0->eth1 TCP relay)
- Device-state sync:
  - `vj6530_async_listener.py` (primary async status/error ingestion from the 6530)
  - `vj6530_poller.py` (slow fallback sync when the async channel is not healthy)
- Device-initiated ESP push path: `esp_push_listener.py` (eth1 listener for active ESP->Raspi messages)
- Production batch logging:
  - `production_logs.py` manages start/stop state from `MAS0002`, batch label from `MAS0029` and ready flag `MAS0030`
  - `logstore.py` mirrors active communication into production-specific TXT logs (`Gesamtanlage`, `ESP`, `TTO`, `Laser`)
- Parameter engine: `params.py`, `params_store.py`, `protocol.py`, `device_bridge.py`
- Routing detail for `MA*` parameters:
  - if `esp_rw = N`, the Databridge treats the parameter as Raspi-local and does not forward Microtom writes to the ESP live path
  - if ESP access is configured (`R`, `W`, `R/W`), `MA*` traffic continues to use the ESP bridge path
- VJ6530 live reads/writes now retry once inside `device_bridge.py` before falling back to cached values or surfacing `NAK_DeviceComm`.
- The 6530 async listener now prefers the live-verified no-CRC transport profile directly and only falls back to autodetect if that explicit profile fails.
- The 6530 async path now owns the printer as the single live `3002` session:
  - it negotiates `HCV` when the session comes up
  - it keeps `AIS` alive with periodic empty `IRQ([])` keepalives on a tight ~2s cadence
  - synchronous 6530 mapping reads/writes are handed into that owner session instead of opening a second parallel control connection whenever async ownership is active
- Critical AIS subscriptions (`ONLINE`, `OFFLINE`, `WARNING`, `FAULT`, `PRINT_FAILED`) are now requested with the high-priority flag so operator status changes and faults reach the Raspi path without the low-priority batching delay.
- Incoming AIR tags now update `STATUS[...]` / `STS[...]` workbook rows immediately from the async snapshot before the slower summary settle/reread completes.
- Owner-session reads and writes now temporarily widen the ZBC response timeout per request while the listener keeps its short unsolicited receive timeout for fast async handling.
- For runtime-session `TTS0001` writes, the final ACK now prefers the async-observed workbook state over a stale direct verify value, so `ACK_TTS0001=0` is no longer emitted after the printer already reached `ONLINE`.
- Queued 6530 writes now return success as soon as the live write itself completes; the follow-up summary settle still runs in-session, but no longer blocks or falsely NAKs slow printer transitions.
- The Databridge owner-session timeout for queued 6530 writes is intentionally larger than the shared-library state-settle window, so slow `6 -> 3` transitions are not aborted prematurely at the Raspi layer.
- Printer-state writes now trigger an immediate workbook status resync so related follow-up values can be forwarded without waiting for the next background cycle.
- Async summary fanout is ordered so all Microtom notifications are queued before any ESP mirror attempt starts, preventing ESP-side delays from holding back Microtom state delivery.
- 6530 async fanout now uses a dedicated background ESP mirror worker during the live owner session:
  - Microtom callbacks are queued immediately on the async thread
  - ESP mirrors run in order on the background worker and no longer block `AIR` handling
  - transient ESP communication failures are retried in that worker; permanent `NAK_*` / access/configuration failures are logged and left visible
- The fallback poller now yields to recent async events and will discard overlapping stale poll results instead of overwriting a fresher async state transition.
- The fallback poller also stands down while the async owner session is healthy, even if no fresh state change happened recently.
- Background 6530 cache warmup is skipped while async ownership is enabled, avoiding a second control client on `3002` during startup.
- Outbox dedupe now only collapses consecutive identical values; non-consecutive state changes remain lossless.
- Live TEST proof on `192.168.2.103:3002`:
  - idle `AIS` without synchronous traffic closes after about 15s
  - `IRQ([])` keepalives keep the async session open
  - a second parallel control session times out while that owner session is active
  - raw `CMD_START` now triggers `AIR` online (`0x0002`) on TEST in about `46 ms`
  - raw `CMD_STOP` now triggers `AIR` offline (`0x0008`) on TEST in about `6 ms`
  - `CMD_SHUTDOWN` / `CMD_STARTUP` do not emit a dedicated `AIR` state tag on TEST; the Raspi must therefore confirm `6 <-> 0` from fresh summary state rather than waiting for an async status tag that never comes
- On TEST, `mas004-vj6530-zbc-bridge.service` is intentionally parked so the Databridge remains the sole operational owner of live 6530 traffic on `3002`.
- Networking helper: `netconfig.py`
- Deployment: `systemd/mas004-rpi-databridge.service`, `scripts/`

## Runtime Contracts
- Local API/UI endpoint: `https://<raspi-ip>:8080`
- Main inbound endpoint for Microtom: `POST /api/inbox`
- Main outbound callback target: `<peer_base_url>/api/inbox`
- Optional parallel outbound callback target: `<peer_base_url_secondary>/api/inbox`
- Health endpoint: `GET /health`
- Production log pull endpoints for Microtom:
  - `GET /api/production/logfiles/list`
  - `GET /api/production/logfiles/download`
  - `POST /api/production/logfiles/ack`
- TCP relay endpoints on Raspi eth0:
  - main relay ports follow the configured device ports: `vj6530_port`, `vj3350_port`, `esp_port`
  - optional extra per-device ports from Settings UI (`*_forward_ports`)
  - current TEST setup uses `esp_port = 3010`

## Deployment Topology
- TEST Raspberry:
  - SSH: `pi@10.27.67.68`
  - UI/API: `https://10.27.67.68:8080`
  - Policy: default sync target (always keep aligned with local + git)
- LIVE Raspberry:
  - SSH: `pi@192.168.210.20`
  - UI/API: `https://192.168.210.20:8080`
- Policy: update only on explicit release command
- Script target metadata can be overridden via environment variables:
  - `MAS004_TEST_SSH`, `MAS004_LIVE_SSH`, `MAS004_TEST_WEB`, `MAS004_LIVE_WEB`

## Current LIVE Runtime Snapshot (2026-03-25)
- Captured from `/etc/mas004_rpi_databridge/config.json` on the Microtom LIVE Raspberry.
- This runtime configuration is intentionally not version-controlled and must not be overwritten by a code-only deployment.
- The LIVE Raspberry timezone is expected to stay on `Europe/Zurich` and should be treated as an OS-level invariant, not a Databridge config field.
- Current operational values:
  - `eth0_ip = 192.168.210.20`
  - `eth1_ip = 192.168.2.100`
  - `peer_base_url = http://192.168.210.10:81`
  - `peer_base_url_secondary = https://192.168.5.2:9090`
  - `peer_watchdog_host = 192.168.210.10`
  - `esp_host = 192.168.2.101`, `esp_port = 3007`, `esp_simulation = true`
  - `vj3350_host = 192.168.2.102`, `vj3350_port = 3008`, `vj3350_simulation = true`
  - `vj6530_host = 192.168.2.103`, `vj6530_port = 3009`, `vj6530_simulation = true`
- When the TEST Raspberry becomes reachable again, mirror these values manually via the Settings UI or a controlled config export/import, not via repo deployment.

## Persistent Paths
- Config: `/etc/mas004_rpi_databridge/config.json`
- DB: `/var/lib/mas004_rpi_databridge/databridge.db`
- Persisted master workbook on Raspi: `/var/lib/mas004_rpi_databridge/master/Parameterliste_master.xlsx`

## Systemd Service
- `mas004-rpi-databridge.service`
- Working dir on Pi: `/opt/MAS-004_RPI-Databridge`

## Priority Rule
- This repo is the orchestration authority.
- Subprojects must be treated as extensions of this repo, not peers.
- All multi-repo operations should start here.
- The recommended long-term master orchestration prompt and sub-agent topology live in:
  - `docs/MAS-004_Roche_Master_Chat.md`
- The sub-agent names defined there are canonical project identities.
- If the tool session drops or parks a sub-agent thread, the master chat must rehydrate it under the same name before delegation continues.
- Current platform behavior may cap the number of simultaneously live sub-agent threads below the full MAS-004 role map; this does not change ownership boundaries.

## Multi-Repo Dependency Map
- `MAS-004_ESP32-PLC-Bridge`: ESP32 transport/probe subproject
- `MAS-004_VJ3350-Ultimate-Bridge`: VJ3350 transport/probe subproject
- `MAS-004_VJ6530-ZBC-Bridge`: VJ6530 transport/probe subproject
- `MAS-004_ZBC-Library`: shared ZBC transport/message library for the 6530 stack

## TTO Mapping Source of Truth
- The Videojet 6530 TTO mapping now uses the live-readable CLARiTY parameter archive from `MAS-004_ZBC-Library`:
  - `FRQ[CURRENT_PARAMETERS]` on the 6530 returns the UTF-16 parameter XML
  - `FTX[CURRENT_PARAMETERS]` writeback has been live-verified against the real 6530
  - the MAS workbook `..\Parameterliste SAR41-MAS-004_V11.11.25.xlsx` contains a dedicated `ZBC Mapping:` column for `TTP`, `TTE`, `TTW`, `TTS`
  - the helper `..\MAS-004_ZBC-Library\tools\update_tto_workbook.py` refreshes this column and the added TTO rows from a live printer or saved archive
  - `MAS-004_VJ6530-ZBC-Bridge` now consumes the shared library instead of maintaining a separate transport stack
  - the workbook now contains a dedicated `ESP32 R/W:` column in addition to Microtom `R/W:`
  - `TTE` / `TTW` and printer status are primarily updated from the 6530 async channel; polling remains a fallback for workbook status/error mappings
  - the async listener now re-reads summary state for a short settle window after online/offline/warning/fault events, so delayed `SHUTDOWN` / `ONLINE` follow-up bits are less likely to fall through to the fallback poller
  - when the async owner session is up, reconciliation reads also use that same session instead of creating a second live bridge connection
  - `TTS0001` is the dedicated numeric TTO status channel via `STATUS[PRINTER_STATE_CODE]`:
    - `0=OFFLINE`, `1=OFFLINE_WARNING`, `2=OFFLINE_FAULT`, `3=ONLINE`, `4=ONLINE_WARNING`, `5=ONLINE_FAULT`, `6=SHUTDOWN`
    - ESP writes `0`, `3`, `6` map to printer control transitions, not just one fixed command:
      - `6 -> 0` via `STARTUP`
      - `0 -> 3` via `START`
      - `6 -> 3` via `STARTUP` then `START`
      - `6` via `SHUTDOWN`
    - the shared library now confirms `6 -> 3` against a fresh live summary instead of a stale shutdown snapshot, so `STARTUP` can reach `0` and then continue with `START`
    - current TEST live timings through the Raspi path are now typically:
      - `3 -> 0` in about `2.9 s`
      - `0 -> 3` in about `4.8 s`
      - `0 -> 6` in about `4.1 s`
      - `6 -> 3` in about `3.2 s`
    - the derived states `1`, `2`, `4`, `5` are observed Warning/Fault combinations and are not direct write targets
    - printer-originated state changes are fanned out to Microtom / ESP only when the workbook access flags allow it
    - TEST status after the 2026-03-26 ESP firmware refresh:
      - the real ESP at `192.168.2.101:3010` now seeds and accepts `TTP00073`, `TTP00076` and `TTS0001`
      - direct live smoke on TEST confirmed `ACK_TTP00073=1`, `ACK_TTP00076=ONLINE`, `ACK_TTS0001=3`
      - the ESP firmware seed generator now includes `TTP` / `TTS` / `TTE` / `TTW` in addition to `MAP` / `MAS` / `MAE` / `MAW`, using workbook `ESP32 R/W:` as the writeability source
  - the ZBC spec does not expose a generic async delta event for arbitrary `CURRENT_PARAMETERS` changes from the printer UI; direct CLARiTY-side `TTP` edits still require polling/readback

## Sync/Support Policy
- Before and after changes in this repo, run:
  - `scripts/mas004_multirepo_status.ps1 -Target test`
  - `scripts/mas004_multirepo_sync.ps1 -Target test -RestartServices`
- Never use destructive git commands on Pi repos.
- If a Pi repo is dirty, do not auto-overwrite; report and require explicit decision.
- LIVE deployment requires explicit opt-in:
  - `scripts/mas004_multirepo_sync.ps1 -Target live -AllowLive -RestartServices`

## Last Reviewed
- Date: 2026-03-25
- Local HEAD baseline during creation: `af82b02`

## Current Sync Snapshot (2026-03-04)
- Local git + all managed repos must stay synchronized and clean.
- Remote status depends on selected target profile (`test` or `live`) and connectivity.
- Safety policy remains active: no destructive overwrite on dirty Pi trees.

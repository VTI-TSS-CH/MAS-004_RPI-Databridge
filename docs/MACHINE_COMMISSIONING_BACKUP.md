# MACHINE_COMMISSIONING_BACKUP - MAS-004_RPI-Databridge

## Purpose
- This document describes the protected Machine-Setup functions for:
  - first-time machine commissioning
  - repeat commissioning on partially completed machines
  - settings backup
  - full backup / clone preparation
  - machine identity and serial-number traceability
  - restore/bootstrap workflows for new or replacement Raspberry systems
- The feature set is intended to reduce commissioning time for follow-up MAS-004 machines and to make software state reproduction auditable for pharma use.

## Protected Access
- Entry point: `/ui/machine-setup`
- Protected sub-pages:
  - `/ui/machine-setup/commissioning`
  - `/ui/machine-setup/backups`
  - `/ui/machine-setup/process`
  - `/ui/machine-setup/io`
  - `/ui/machine-setup/motors`
  - `/ui/machine-setup/winders/unwinder`
  - `/ui/machine-setup/winders/rewinder`
- Required credentials:
  - user: `Admin`
  - password: `VideojetMAS004!`
- Access protection is session-based. The protected pages and their APIs are intentionally separated from the public operator surface.

## Machine Identity
- The Databridge now treats machine identity as first-class metadata:
  - `machine_serial_number`
  - `machine_name`
  - `backup_root_path`
- Purpose of this metadata:
  - bind commissioning runs and backups to one physical machine
  - make exported bundles traceable outside the device
  - support later clone or disaster-recovery workflows
  - distinguish LIVE and TEST assets cleanly when both are maintained in parallel
- The machine identity is edited from the protected Backups page and is written into backup manifests and the backup registry structure.

## Commissioning Assistant

### Operator Goal
- Guide the first hardware/software bring-up of a MAS-004 machine in a repeatable sequence.
- Record which steps succeeded, failed, were skipped or were only reused from an earlier completed run.
- Allow a follow-up pass that only revisits incomplete points instead of forcing a full restart every time.

### Modes
- `Full run`
  - starts a fresh commissioning run with all defined steps
- `Incomplete only`
  - starts a new run but carries over already successful steps from the latest run as `reused`
  - intended for repeated field commissioning after power/network/hardware interruptions

### Typical Step Set
- The commissioning flow is now broken down much more concretely along the real MAS-004 machine structure:
  - bootstrap:
    - machine identity
    - Raspi network
    - Raspi runtime / service
    - parameter + IO workbook state
  - peer / host communication:
    - Microtom primary peer health
    - optional parallel / VPN peer health
  - controller / field network:
    - ESP32-PLC58 endpoint
    - ESP realtime IO/process image
    - Moxa 1 endpoint
    - Moxa 2 endpoint
    - Moxa field IO verification
  - printers:
    - VJ6530 endpoint
    - TTO IO handshake
    - VJ3350 endpoint
    - Laser IO handshake
  - winders:
    - Abwickler endpoint
    - Aufwickler endpoint
    - Wickler stop IOs
  - motion:
    - global Oriental parameter baseline
    - table X/Z axes
    - label drive axis
    - sensor-positioning axes
    - camera positioning axis
    - laser guard axis
    - label guide axes
  - sensor / encoder path:
    - general encoder verification
    - infeed transport encoder
    - label-drive comparison encoder
    - label detect sensor
    - label control sensor
  - cameras:
    - material camera TV1
    - OCR camera
  - machine IO / safety:
    - generic IO test
    - operator buttons and lamp outputs
    - safety circuit
    - UPS / shutdown path
  - process validation:
    - dry-run label process
    - MAS001 / MAS0002 state flow
    - production logfile / MAS0030 handling
    - backup baseline
- Some steps can be auto-checked from the Raspi side.
- Other steps are intentionally operator-confirmed because they depend on real motion, wiring, or observed machine behavior.

### MAS-004 Component Intent
- The assistant does not just ask for a generic "IO test" anymore.
- It explicitly mirrors the machine-functional decomposition that matters during field commissioning:
  - exact printers and their handshake signals
  - exact transport encoders and label sensors
  - exact motor groups by machine purpose
  - exact safety and operator-interface responsibilities
- This makes it easier to commission a second or third machine without mentally reconstructing the order from old notes.

### Recorded Step States
- `pending`
- `in_progress`
- `success`
- `failed`
- `skipped`
- `reused`
- The assistant keeps the full run history so a later service intervention can see which phase originally failed and what was retried afterward.

### Commissioning Philosophy
- The assistant is not meant to replace expert commissioning judgment.
- It is meant to:
  - give a stable order
  - prevent forgotten subsystems
  - keep an audit trail of what was checked
  - shorten follow-up commissioning on machine 2, 3, ... by reusing the same guided flow

## Settings Backup

### Purpose
- Capture the operational machine configuration without treating it as a software release.
- Use this when the software baseline stays the same but machine-specific settings or workbook/runtime state must be preserved.

### Intended Contents
- Runtime config JSON
- Databridge SQLite state
- persisted master parameter workbook
- persisted IO workbook
- motor UI/runtime state
- production-related runtime state
- backup manifest with identity metadata and timestamps

### Use Cases
- before risky maintenance
- before workbook imports with many changed mappings
- before field parameterization sessions
- before replacing a Raspberry or storage medium
- before promoting a TEST machine setup into a LIVE-ready configuration basis

### Retention Model
- Up to 100 settings backups are kept on the machine.
- Backups are named by the operator and can carry an additional note.
- Oldest entries are pruned first when the retention cap is exceeded.

## Full Backup / Clone Bundle

### Purpose
- Capture not only the runtime settings but also the software basis that belongs to the machine.
- This is the preferred baseline for:
  - cloning a second MAS-004 machine
  - rebuilding a machine after storage failure
  - preserving a delivery or qualification snapshot

### Additional Contents Compared to Settings Backup
- repository manifest for the MAS-004 software landscape
- selected repo snapshots for the relevant component projects
- machine identity metadata
- bundle manifest for traceability

### Expected Repositories
- `MAS-004_RPI-Databridge`
- `MAS-004_ESP32-PLC-Bridge`
- `MAS-004_ESP32-PLC-Firmware`
- `MAS-004_VJ3350-Ultimate-Bridge`
- `MAS-004_VJ6530-ZBC-Bridge`
- `MAS-004_ZBC-Library`
- `MAS-004_SmartWickler`

### Versioning Intent
- The backup bundle is machine-oriented and field-recovery oriented.
- Git remains the software development truth.
- The full backup bridges both worlds by preserving the effective machine baseline together with machine serial identity.

## Restore Workflow

### In-Place Restore on an Existing Raspi
1. Open `/ui/machine-setup/backups`.
2. Verify the target machine identity.
3. Import a previously exported backup if needed.
4. Trigger restore for the selected bundle.
5. Allow the Databridge service to restart if the restore reports that a restart is required.
6. Re-open the UI and validate health, settings, protected pages and expected workbook state.

### Standalone Restore Script
- Script: `scripts/mas004_restore_backup.py`
- Purpose:
  - restore a backup bundle even outside the web flow
  - support scripted recovery or bootstrap-assisted cloning
- Typical usage pattern:
```powershell
python scripts/mas004_restore_backup.py `
  --bundle .\exports\MAS004_MACHINE_A_full_20260418.zip `
  --cfg-path /etc/mas004_rpi_databridge/config.json `
  --db-path /var/lib/mas004_rpi_databridge/databridge.db `
  --apply-repos
```
- The script is expected to:
  - restore the runtime configuration
  - restore the SQLite payload safely
  - restore persisted workbook/state payloads
  - optionally unpack repo snapshots for clone scenarios

## Bootstrap Workflow for a Fresh Raspberry

### Goal
- Support the first setup even when the final Databridge UI is not yet available on the target Raspberry.
- This is especially important for follow-up MAS-004 machines where commissioning should start from a laptop and then hand over to the Raspi-hosted UI as soon as possible.

### Scripted Helper
- Script: `scripts/mas004_machine_bootstrap.py`
- Intended responsibilities:
  - discover a target Raspberry on the commissioning network
  - push the required restore helper(s)
  - upload a full-backup bundle
  - trigger restore on the fresh target
  - hand over to the protected UI once the Databridge runtime is up

### Typical Intended Flow
1. Start on the engineering laptop.
2. Discover the fresh Raspberry or address it directly.
3. Upload a known-good full backup / clone bundle.
4. Run the restore/bootstrap script remotely.
5. Wait until the Databridge service starts on the target.
6. Continue the remaining hardware validation from `/ui/machine-setup/commissioning`.

### Example Command Pattern
```powershell
python scripts/mas004_machine_bootstrap.py discover --subnet 192.168.210.0/24

python scripts/mas004_machine_bootstrap.py apply-full-backup `
  --target pi@192.168.210.20 `
  --bundle .\exports\MAS004_MACHINE_A_full_20260418.zip `
  --apply-repos
```

## LIVE / TEST / Offline Notes

### LIVE
- LIVE runtime settings remain machine-local and must not be overwritten blindly by repo deployment.
- Use restore/clone workflows deliberately and only when the target machine is clearly identified.
- The protected backup flow is preferred over manual file copying because it keeps manifests and serial identity aligned.
- Current observed LIVE peer situation:
  - the optional VPN/secondary peer can be up or down without explaining a `404` from the productive Microtom primary peer
  - a `404 "No active developers found to forward the request"` on `http://192.168.210.10:81/api/inbox` is a primary-peer behavior and is not caused by the secondary peer being offline

### TEST
- TEST should be kept on the same merged project baseline as LIVE where possible.
- If TEST is offline, keep the repo state and documentation aligned locally and record deployment as pending.
- When TEST comes back, prefer a controlled import/restore or explicit sync instead of ad-hoc manual edits.

### Offline Mode
- If no target machine is reachable:
  - continue local repo work only
  - do not claim runtime sync
  - document open deployment/restore steps explicitly
- Backup and commissioning docs still remain the reference so the pending field actions can be executed later without reconstructing the process from memory.

## Recommended Operational Sequence
1. Finish and validate software locally.
2. Set or verify machine serial number and machine name.
3. Create a settings backup before field changes.
4. Create a full backup at major milestones or before cloning.
5. Use the commissioning assistant during first bring-up and after larger hardware changes.
6. Export the relevant backup bundles to external storage.
7. Record the resulting machine baseline in project delivery / qualification documentation.

## Audit / Pharma-Oriented Considerations
- Use machine serial numbers consistently in backup names and commissioning notes.
- Treat exported full backups as machine history artifacts, not as disposable temp files.
- Preserve the corresponding Git commit references for the involved repos whenever a qualification baseline is created.
- Do not mix TEST and LIVE bundles without clear naming and manifest review.

## Related Files
- `docs/PROJECT_CONTEXT.md`
- `docs/SUPPORT_RUNBOOK.md`
- `docs/SUPPORT_CHANGELOG.md`
- `scripts/mas004_machine_bootstrap.py`
- `scripts/mas004_restore_backup.py`

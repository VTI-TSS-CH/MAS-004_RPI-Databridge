# MACHINE_PROCESS_RUNTIME - MAS-004_RPI-Databridge

## Purpose
- Documents the current plant-level machine runtime foundation on the Raspi side.
- Clarifies which responsibilities already exist on the Raspi and which hard realtime responsibilities still belong on the ESP32 PLC.

## Raspi Responsibilities
- `machine_runtime.py`
  - derives the current machine state (`MAS0001`) from:
    - Microtom command byte `MAS0002`
    - safety signals
    - warning/error state
    - local button inputs
  - drives:
    - machine-status light via Moxa #2 `DO4..DO6`
    - machine button LEDs on the Raspberry PLC outputs
  - persists machine overview data into:
    - `machine_state`
    - `machine_events`
    - `label_register`
    - `label_events`
- `machine_semantics.py`
  - centralizes:
    - MAS001 state labels
    - MAS0002 command-byte -> target-state mapping
    - button semantics
    - button LED behavior
    - status lamp color/blink behavior
    - `MAS0003` bit packing
- `format_semantics.py`
  - derives the currently active format/process plan from `MAP` values:
    - `MAP0001` -> both label guide target positions
    - `MAP0002` + `MAP0040` -> expected label length window
    - `MAP0003` + `MAP0005` -> inverted table X correction
    - `MAP0004` + `MAP0006` + `MAP0018/0019` -> print stop distance
    - `MAP0007` + `MAP0028` -> table Z production target
    - `MAP0008/0009` + `MAP0029/0030` -> sensor axis targets
    - `MAP0066` -> LED stripe first-LED offset
- `webui.py`
  - protected Machine-Setup process page:
    - `/ui/machine-setup/process`
  - protected API:
    - `/api/machine/overview`
- `production_logs.py`
  - now also supports the extra production logfile group:
    - `LabelProductionLog`

## Current ESP -> Raspi Event Contract
- The Raspi listener accepts active ESP event pushes on the existing push socket as lines starting with:
  - `EVT <json>`
- Supported event payloads today:
  - machine state event:
    - `EVT {"type":"machine_state","state":3}`
  - completed label event:
    - `EVT {"type":"label_complete","label_no":12,"material_ok":1,"print_ok":1,"verify_ok":1,"removed":0,"production_ok":1,"zero_mm":0.0,"exit_mm":1940.5}`
- For `label_complete` events the Raspi:
  - stores the label in `label_register`
  - packs the value into `MAS0003`
  - pushes `MAS0003` to Microtom
  - appends the event to `label_production_<job>.txt`
- Label length deviations:
  - ESP sets `MAE0025` when the label is too short
  - ESP sets `MAE0026` when the label is too long
  - Raspi treats those two errors as production-pause reasons, not as Purge/Not-Stop reasons

## Master Workbook Alignment
- The repo now contains a reusable sync step:
  - `scripts/sync_master_workbooks.py`
- Current workbook-side machine additions:
  - the 2026-04-21 workbook sync refreshed 56 MAP rows
  - `MAP0066`
    - distance from label-detection zero point to the first LED of the 1 m label-status stripe
    - default is now `8000` = `800.0 mm`
  - KI column refresh
    - user-written notes before `KI:` are treated as input guidance
    - the final cell text is regenerated as one human-readable `KI:` interpretation

## Current Limitation / Honest Status
- This is intentionally a Raspi-side orchestration foundation, not the final hard realtime machine implementation.
- The actual no-pulse-loss logic for:
  - encoder counting
  - sensor ISR debounce
  - label shift register timing
  - exact stop-position correction
  - LED stripe live tracking
  still has to run on the ESP32 PLC.
- The Raspi side is ready to receive and persist condensed ESP events, but it should not be turned into the realtime execution engine.

## Recommended Next ESP Step
- Build a dedicated process runtime on the ESP side that:
  - uses hardware-safe counting for both quadrature encoders
  - captures label-sensor edges through very small ISRs with debounce
  - maintains the label shift register locally on the ESP
  - emits only condensed `EVT {...}` messages to the Raspi
  - keeps networking outside the ISR path

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
    - machine-status light via Moxa E1213 #3 `DIO0..DIO2`
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
    - `/api/machine/button`
    - `/api/machine/audit`
    - `/api/machine/audit/download`
    - `/api/machine/audit/retention`
- `machine_control_ui.py`
  - renders the protected Machine Control / Audit page
  - exposes virtual Start/Pause, Stop, Einrichten, Synchronisieren, Leerfahren and Zurueckspulen buttons
  - visualizes communication and machine events as a human-readable production audit stream
  - uses `machine_audit_keep_hours` for detailed DB audit retention
- `production_logs.py`
  - now also supports the extra production logfile group:
    - `LabelProductionLog`

## Machine Control / Audit Behavior
- Virtual buttons call the same `MAS0002` command path as physical Raspi PLC buttons.
- Allowed actions are checked against the current machine state and `MAP0065`.
- In Not-Stop/Purge reset context the virtual Start/Pause button sends the same reset/stop command path (`MAS0002=2`) used by the reset flow.
- Because Reset shares the physical Start/Pause button element, Reset is enabled by the `MAP0065` Start bit; when this bit is `0`, both the web Reset button and the physical `I0.7` reset press are ignored and the Start/Pause/Reset LEDs remain off.
- The audit view combines:
  - DB communication logs from `logs`
  - machine events from `machine_events`
  - label events from `label_events`
  - parameter descriptions from the master data where a `MAP`/`MAS`/`MAE`/device code is detected
- Audit download is non-consuming. It does not delete production logfiles and is separate from the consuming production logfile download flow.

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

## Production Start Motion
- `MAS0001=5` is a machine state, not enough by itself to move Motor 3.
- Automatic production motion is enabled through the ESP production runner, not through the old indexed commissioning runner.
- When `MAS0002=1` is accepted from `Pause`, the Raspi transitions via `4 -> 5`, mirrors `MAS0001=5` to the ESP, syncs the relevant `MAP` values and starts `PROCESS PRODUCTION START SPEED_MM_S=<MAP0014> RAMP_MM_S2=300`.
- Both SmartWicklers are prepared for continuous dancer regulation before Motor 3 is started:
  - `indexedModeEnabled=0`
  - `ready&allowMotion=1`
  - verified `continuousModeReady=true`, `lastCommandOk=true`, no alarm/fault and dancer position between 8 % and 92 %.
- The old `PROCESS INDEXED START` runner remains available only for explicit commissioning tests and is not used by production start.
- System direction convention: Motor 3, Infeed encoder and Drive encoder use positive mm for label feed / production advance; rewind is negative. Motor 3 keeps its configured `invert_direction=true` machine mapping, while the ESP normalizes the two line encoders at the PCNT/ISR input boundary. ID3 is not a production positioning axis; its logical 0 is recaptured during setup and again at `PROCESS PRODUCTION_RESET` handover.
- ID3/encoder scale is calibrated separately through the protected Machine-Setup page `/ui/machine-setup/calibration`. The HMI can prepare the Wicklers, run the 2000-mm check, show live/result values and apply the entered real travel/label length to `MOTOR 3 steps_per_mm`, `MAP0077`, `MAP0078` and optionally `MAP0076`. The normal setup workflow uses the stored calibration values and no longer performs a hidden 2000-mm scale correction on every setup.
- The ESP setup measurement now uses absolute ID3/AZD targets: after the Raspi infeed teach pulse the ESP zeros ID3 plus both encoders, drives absolute `500 mm`, returns absolute to `0 mm`, drives absolute `2500 mm` while measuring the first label, slip and control-sensor teach, then returns absolute to `first_label_reference - 10 mm`. At the end ID3 and both encoders are zeroed again and the setup handover remembers that the first label edge is 10 mm upstream of the label sensor.
- Production print positioning uses the calculated absolute AZD target for the label print stop instead of a relative remaining-distance command.
- When production is left for Pause/Stop/fault, the Raspi stops Motor 3, cancels `PROCESS WICKLER`, stops `PROCESS PRODUCTION`/`PROCESS INDEXED`/`PROCESS PROFILE`, disables Wickler Indexed mode and sets Wicklers to `Bereit` for Pause or `Stop` for Stop/fault.
- Existing production-log download blocking still applies: if `MAS0030=1` / old production files are pending, `MAS0002=1` is rejected before motion is armed.

## Position Axis Hardware Feedback
- Positionsachsen bleiben kommandoseitig protocol-first:
  - Zielwert, Min/Max-Schutz, Strom/Speed/Rampen und Direct-Data-Trigger laufen ueber den ESP/AZD-Motorpfad.
  - Hardware-START wird fuer ID1/2/4-9 nicht als Fuehrungsbefehl benutzt, damit keine Bewegung an Softlimit- und Rezeptpruefung vorbei ausgeloest wird.
- Die schnelle Rueckmeldung wird hybrid genutzt:
  - AZD `DOUT0 = 134 MOVE` wird auf die vorhandenen ESP-Eingaenge gelegt.
  - AZD `DOUT1 = 138 IN-POS` bleibt fuer Achsen mit verdrahtetem OUT1 als Positioning-Complete-Signal erhalten.
- Aktuell verdrahtete Rueckmeldungen:
  - ID1 X-Achse: `I1.0` MOVE, `I1.1` IN-POS.
  - ID2 Z-Achse: `I1.2` MOVE, `I1.3` IN-POS.
  - ID4 Schutzblech Laser: `I0.9` MOVE.
  - ID5 Kamera Materialkontrolle: `I0.10` MOVE.
  - ID6 Etikettenanwesenheit: `I2.0` MOVE.
  - ID7 Auswurfkontrolle: `I2.1` MOVE.
  - ID8 Etikettenanschlag links: `I2.3` MOVE.
  - ID9 Etikettenanschlag vorne/rechts: `I2.2` MOVE.
- Fuer ID4-9 ist in der aktuellen IO-Liste nur OUT0 auf ESP verdrahtet. IN-POS kann dort erst hardwareseitig genutzt werden, wenn OUT1 zusaetzlich verdrahtet und in der IO-Liste erfasst ist.
- Die Runtime nutzt Hardware-MOVE als schnelle positive Bewegungsrueckmeldung. Hardware-IN-POS darf eine Zielpruefung nur abschliessen, wenn vorher eine MOVE-Flanke gesehen wurde oder der frische ESP-Motorstatus das Zielkommando bestaetigt.

## Master Workbook Alignment
- The repo now contains a reusable sync step:
  - `scripts/sync_master_workbooks.py`
- Current workbook-side machine additions:
  - the 2026-04-21 workbook sync refreshed 56 MAP rows
  - `MAP0066`
    - distance from label-detection zero point to the first LED of the external label-status stripe
    - default is now `9600` = `960.0 mm`
  - `MAP0071..MAP0075`
    - active LED strip length, UDP enable, target last octet, UDP port and frame interval for the external Olimex ESP32-POE-ISO-IND LED controller
    - current strip length is `520.0 mm`, rendered as `75` LEDs at `6.95 mm` pitch
    - controller firmware lives in `MAS-004_ESP32-PLC-Firmware/tools/mas004_led_controller_esp32`
    - production env is `esp32_led_controller_olimex_poe_iso_ind`, static controller IP `192.168.2.110`, UDP port `3050`
  - `MAP0076`
    - label length compensation in `1/10 mm`; current default `8` applies a fixed `+0.8 mm` display correction after encoder calibration
    - `MAE0025/MAE0026` are raised only when raw infeed length and compensated length are both outside the same limit; this keeps gross label-length faults active without tripping on the ultrasonic sensor-window offset.
    - fixed machine/sensor correction, not format-relevant
    - ESP diagnostics keep `raw_length_mm`, `raw_drive_length_mm` and compensated `measured_length_mm`; setup and production length fault decisions use raw infeed length as `decision_length_mm`
  - `MAP0077`
    - Einlaufencoder Wirkdurchmesser in `1/1000 mm`; default `100765` = `100.765 mm`
    - fixed machine calibration, not format-relevant
  - `MAP0078`
    - Auslauf-/ID3-Encoder Wirkdurchmesser in `1/1000 mm`; default `100649` = `100.649 mm`
    - fixed machine calibration, not format-relevant
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

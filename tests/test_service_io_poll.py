from __future__ import annotations

import json
import tempfile

from mas004_rpi_databridge.db import DB, now_ts
from mas004_rpi_databridge.service import (
    FIELD_IO_DEVICE_CODES,
    _io_poll_deferred_devices_for_critical_window,
    _io_poll_field_io_unhealthy,
)


def _machine_state_db(*, state: int, info: dict | None = None) -> DB:
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
    tmp.close()
    db = DB(tmp.name)
    with db._conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO machine_state(
                   singleton_id,current_state,requested_state,state_source,
                   warning_active,purge_active,production_label,last_label_no,
                   info_json,updated_ts
               ) VALUES(1,?,?,?,0,0,'',0,?,?)""",
            (int(state), int(state), "test", json.dumps(info or {}), now_ts()),
        )
    return db


def test_field_io_unhealthy_ignores_single_slow_unreachable_field_device():
    result = {
        "devices": [
            {
                "device_code": "moxa_e1213_1",
                "reachable": False,
                "error": "timed out",
            }
        ]
    }

    assert not _io_poll_field_io_unhealthy("moxa_e1213_1", result, 1.5)


def test_io_poll_defers_all_field_devices_during_safety_reset():
    db = _machine_state_db(state=21, info={"safety": {"phase": "resetting"}})
    due = sorted(FIELD_IO_DEVICE_CODES | {"raspi_plc21"})

    deferred = _io_poll_deferred_devices_for_critical_window(db, due)

    assert deferred == set(FIELD_IO_DEVICE_CODES)


def test_io_poll_defers_field_devices_in_stop_and_fault():
    due = sorted(FIELD_IO_DEVICE_CODES | {"raspi_plc21"})
    for state in (9, 21):
        db = _machine_state_db(state=state)
        assert _io_poll_deferred_devices_for_critical_window(db, due) == set(FIELD_IO_DEVICE_CODES)


def test_io_poll_pending_start_defers_moxa_too():
    db = _machine_state_db(state=9, info={"production_runtime": {"pending_start": True}})
    due = sorted(FIELD_IO_DEVICE_CODES | {"raspi_plc21"})

    deferred = _io_poll_deferred_devices_for_critical_window(db, due)

    assert deferred == set(FIELD_IO_DEVICE_CODES)


def test_field_io_unhealthy_ignores_reachable_and_broker_busy_skip():
    assert not _io_poll_field_io_unhealthy(
        "moxa_e1213_1",
        {"devices": [{"reachable": True, "error": ""}]},
        1.5,
    )
    assert not _io_poll_field_io_unhealthy(
        "esp32_plc58",
        {"devices": [{"reachable": False, "skipped": "broker_busy"}]},
        1.5,
    )
    assert not _io_poll_field_io_unhealthy(
        "esp32_plc58",
        {"devices": [{"reachable": False, "cooldown": True, "error": "ESP IO cooldown active"}]},
        0.02,
    )
    assert not _io_poll_field_io_unhealthy(
        "esp32_plc58",
        {"devices": [{"reachable": False, "debounced": True, "error": "ESP command broker request timed out"}]},
        1.5,
    )
    assert not _io_poll_field_io_unhealthy(
        "esp32_plc58",
        {"devices": [{"reachable": False, "cooldown": True, "error": "ESP IO cooldown active"}]},
        1.5,
    )
    assert not _io_poll_field_io_unhealthy(
        "raspi_plc21",
        {"devices": [{"reachable": False, "error": "timed out"}]},
        1.5,
    )

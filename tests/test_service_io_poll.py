from mas004_rpi_databridge.service import _io_poll_field_io_unhealthy


def test_field_io_unhealthy_detects_slow_unreachable_eth1_device():
    result = {
        "devices": [
            {
                "device_code": "moxa_e1213_1",
                "reachable": False,
                "error": "timed out",
            }
        ]
    }

    assert _io_poll_field_io_unhealthy("moxa_e1213_1", result, 1.5)


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
        "raspi_plc21",
        {"devices": [{"reachable": False, "error": "timed out"}]},
        1.5,
    )

"""Unit tests for partial telemetry patch merging and state preservation."""

import pytest
from bambu_monitor.domain.printer import PrinterState
from bambu_monitor.domain.telemetry import TelemetryPatch


def test_telemetry_does_not_treat_tray_remain_as_a_runout_sensor():
    patch = TelemetryPatch.from_raw(
        "test-a1",
        {"print": {"ams": {"tray_now": "0", "ams": [{"tray": [{"id": "0", "remain": 0}]}]}}},
    )
    assert patch.filament_runout is None


def test_telemetry_detects_explicit_filament_runout_flag():
    patch = TelemetryPatch.from_raw("test-a1", {"print": {"filament_runout": True}})
    assert patch.filament_runout is True
    assert patch.filament_runout_details["source"] == "filament_runout"


def test_telemetry_detects_hms_runout_codes():
    hms_patch = TelemetryPatch.from_raw(
        "test-a1",
        {"print": {"hms": [{"code": "HMS_0700_2000_0002_0001"}]}},
    )
    assert hms_patch.filament_runout is True
    assert hms_patch.filament_runout_details["source"] == "hms.code"

def test_a1_mini_external_spool_runout_from_captured_live_shape():
    patch = TelemetryPatch.from_raw(
        "test-a1",
        {
            "print": {
                "gcode_state": "PAUSE",
                "print_error": 134184977,
                "hms": [{"code": 131073, "attr": 318709760}],
                "ams": {"ams_exist_bits": "0", "tray_now": "254"},
                "vt_tray": {"id": "254", "remain": 0},
            }
        },
    )
    assert patch.filament_runout is True
    assert patch.filament_runout_details["source"] == "hms.a1_mini_external_runout"


def test_hex_print_error_and_string_typed_fields_are_tolerated():
    """A1 Mini firmware sends print_error as a hex string (0x07ff8011).

    A bare int() raised on it and discarded the whole patch — including the
    runout signal — before it ever reached the state manager.
    """
    patch = TelemetryPatch.from_raw(
        "test-a1",
        {
            "print": {
                "gcode_state": "PAUSE",
                "print_error": "0x07ff8011",
                "hms": [{"code": "0x00020001", "attr": "0x12ff2000"}],
                "mc_percent": "42.0",
                "layer_num": "12",
                "nozzle_temper": "215.5",
                "bed_temper": "60",
                "chamber_temper": "not-a-number",
                "mc_remaining_time": "120",
            }
        },
    )
    assert patch.error_code == 0x07FF8011
    assert patch.filament_runout is True
    assert patch.progress == 42
    assert patch.layer == 12
    assert patch.nozzle_temperature == 215.5
    assert patch.bed_temperature == 60.0
    assert patch.chamber_temperature is None  # unparseable -> None, not crash
    assert patch.remaining_seconds == 7200  # 120 minutes


def test_malformed_numeric_fields_never_raise():
    """One malformed field must never discard the whole telemetry patch."""
    patch = TelemetryPatch.from_raw(
        "test-a1",
        {
            "print": {
                "gcode_state": "RUNNING",
                "mc_percent": "bogus",
                "layer_num": None,
                "nozzle_temper": "hot",
                "mc_remaining_time": "soon",
            }
        },
    )
    # gcode_state and state derivation survive the bad fields
    assert patch.gcode_state == "RUNNING"
    assert patch.progress is None
    assert patch.layer is None
    assert patch.nozzle_temperature is None
    assert patch.remaining_seconds is None


def test_very_long_print_remaining_time_stays_minutes():
    """10000+ minutes is a real multi-day print, not seconds."""
    patch = TelemetryPatch.from_raw(
        "test-a1", {"print": {"mc_remaining_time": 15000}}
    )
    assert patch.remaining_seconds == 15000 * 60


@pytest.mark.asyncio
async def test_patch_merge_preserves_untouched_fields(state_manager):
    printer_id = "test-a1"

    # Initial full patch (e.g. pushall)
    patch1 = TelemetryPatch.from_raw(
        printer_id,
        {
            "print": {
                "gcode_state": "RUNNING",
                "subtask_name": "benchy.3mf",
                "mc_percent": 20,
                "layer_num": 10,
                "total_layer_num": 100,
                "mc_remaining_time": 120,
                "nozzle_temper": 220.0,
                "nozzle_target_temper": 220.0,
                "bed_temper": 60.0,
                "bed_target_temper": 60.0,
                "online": True,
            }
        },
    )
    await state_manager.apply_patch(patch1)

    state1 = state_manager.get_state(printer_id)
    assert state1.online is True
    assert state1.state == PrinterState.PRINTING
    assert state1.temperatures.nozzle == 220.0
    assert state1.temperatures.bed == 60.0
    assert state1.print is not None
    assert state1.print.filename == "benchy.3mf"
    assert state1.print.progress == 20
    assert state1.print.layer == 10
    assert state1.print.total_layers == 100
    assert state1.print.remaining_seconds == 7200  # 120 minutes in seconds

    # Partial patch 2: only mc_percent updates
    patch2 = TelemetryPatch.from_raw(
        printer_id,
        {
            "print": {
                "mc_percent": 21,
            }
        },
    )
    await state_manager.apply_patch(patch2)

    state2 = state_manager.get_state(printer_id)
    # Check that mc_percent updated, but layer, total_layers, remaining_seconds, and temps were PRESERVED!
    assert state2.print.progress == 21
    assert state2.print.layer == 10
    assert state2.print.total_layers == 100
    assert state2.print.remaining_seconds == 7200
    assert state2.temperatures.nozzle == 220.0
    assert state2.temperatures.bed == 60.0
    assert state2.online is True

    # Partial patch 3: only temperatures update
    patch3 = TelemetryPatch.from_raw(
        printer_id,
        {
            "print": {
                "nozzle_temper": 221.5,
            }
        },
    )
    await state_manager.apply_patch(patch3)

    state3 = state_manager.get_state(printer_id)
    assert state3.temperatures.nozzle == 221.5
    assert state3.temperatures.bed == 60.0
    assert state3.print.progress == 21
    assert state3.print.layer == 10

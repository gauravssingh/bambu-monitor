"""Unit tests for partial telemetry patch merging and state preservation."""

import pytest
from datetime import datetime, timezone
from bambu_monitor.domain.printer import PrinterState
from bambu_monitor.domain.telemetry import TelemetryPatch


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

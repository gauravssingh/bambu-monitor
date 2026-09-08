"""Unit tests for Multi-Factor Adaptive Stall Detection."""

import pytest
from datetime import datetime, timedelta, timezone
from bambu_monitor.domain.telemetry import TelemetryPatch


@pytest.mark.asyncio
async def test_adaptive_stall_detection_avoids_false_positives_on_long_prints(state_manager):
    """A 20-hour print takes ~12 minutes (720s) per 1%.
    A naive 10-minute check would false-trigger. The adaptive detector must not false-trigger at 10 minutes.
    """
    printer_id = "test-a1"
    t0 = datetime(2026, 9, 8, 8, 0, 0, tzinfo=timezone.utc)

    # 1. Start a long print: 5 hours in (18,000s), progress is 25% (avg 720s per 1%)
    p_init = TelemetryPatch(
        printer_id=printer_id,
        timestamp=t0,
        gcode_state="RUNNING",
        subtask_name="large_mechanical_part.3mf",
        progress=25,
        layer=200,
        total_layers=800,
        remaining_seconds=54000,  # 15 hours remaining
    )
    await state_manager.apply_patch(p_init)

    # Manually adjust active_job started_at and duration to simulate 5 hours in
    job = state_manager.get_active_job(printer_id)
    job.started_at = t0 - timedelta(seconds=18000)
    job.duration_seconds = 18000

    # 2. Advance 10 minutes (600s) without progress change.
    # Expected percent duration = 18000 / 25 = 720s.
    # Adaptive threshold = max(300, 720 * 1.5) = 1080 seconds (~18 minutes).
    t_10min = t0 + timedelta(seconds=600)
    p_10min = TelemetryPatch(
        printer_id=printer_id,
        timestamp=t_10min,
        gcode_state="RUNNING",
        progress=25,
        layer=200,
    )
    events_10min = await state_manager.apply_patch(p_10min)

    # MUST NOT TRIGGER STALL AT 10 MINUTES!
    assert not any(e.event_type == "print.possible_blockage" for e in events_10min)
    assert len(state_manager.get_active_alerts(printer_id)) == 0

    # 3. Advance to 19 minutes (1140s) unchanged (> 1080s threshold)
    t_19min = t0 + timedelta(seconds=1140)
    p_19min = TelemetryPatch(
        printer_id=printer_id,
        timestamp=t_19min,
        gcode_state="RUNNING",
        progress=25,
        layer=200,
    )
    events_19min = await state_manager.apply_patch(p_19min)

    # NOW it SHOULD trigger!
    stall_events = [e for e in events_19min if e.event_type == "print.possible_blockage"]
    assert len(stall_events) == 1
    assert state_manager.get_active_alerts(printer_id)[0].status.value == "active"

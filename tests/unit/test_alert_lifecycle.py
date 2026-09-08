"""Unit tests for Alert Lifecycle state machine and duplicate suppression."""

import pytest
from datetime import datetime, timedelta, timezone
from bambu_monitor.domain.alerts import AlertStatus
from bambu_monitor.domain.telemetry import TelemetryPatch


@pytest.mark.asyncio
async def test_alert_lifecycle_and_duplicate_suppression(state_manager, repositories):
    printer_id = "test-a1"
    alert_repo = repositories["alert"]
    t0 = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)

    # 1. Start printing
    p1 = TelemetryPatch(
        printer_id=printer_id,
        timestamp=t0,
        gcode_state="RUNNING",
        subtask_name="vase.3mf",
        progress=30,
        layer=50,
        total_layers=100,
    )
    await state_manager.apply_patch(p1)

    # Fast forward time beyond stall timeout (default min 300s)
    t_stall = t0 + timedelta(seconds=350)
    p_stall1 = TelemetryPatch(
        printer_id=printer_id,
        timestamp=t_stall,
        gcode_state="RUNNING",
        progress=30,
        layer=50,
    )

    # First stall trigger: should emit print.possible_blockage event
    events1 = await state_manager.apply_patch(p_stall1)
    stall_events = [e for e in events1 if e.event_type == "print.possible_blockage"]
    assert len(stall_events) == 1
    assert stall_events[0].severity.value == "warning"

    # Alert should be ACTIVE
    active_alerts = state_manager.get_active_alerts(printer_id)
    assert len(active_alerts) == 1
    assert active_alerts[0].alert_type == "print.possible_blockage"
    assert active_alerts[0].status == AlertStatus.ACTIVE

    # 2. Repeated telemetry while still stalled: DUPLICATE SUPPRESSION!
    t_stall2 = t_stall + timedelta(seconds=30)
    p_stall2 = TelemetryPatch(
        printer_id=printer_id,
        timestamp=t_stall2,
        gcode_state="RUNNING",
        progress=30,
        layer=50,
    )
    events2 = await state_manager.apply_patch(p_stall2)
    # NO duplicate blockage event should be emitted!
    assert not any(e.event_type == "print.possible_blockage" for e in events2)

    # 3. Acknowledge the alert
    active_alerts[0].acknowledge(at=t_stall2)
    assert active_alerts[0].status == AlertStatus.ACKNOWLEDGED
    await alert_repo.save(active_alerts[0])

    # Repeated telemetry while ACKNOWLEDGED: still suppressed!
    t_stall3 = t_stall2 + timedelta(seconds=30)
    p_stall3 = TelemetryPatch(
        printer_id=printer_id,
        timestamp=t_stall3,
        gcode_state="RUNNING",
        progress=30,
        layer=50,
    )
    events3 = await state_manager.apply_patch(p_stall3)
    assert not any(e.event_type == "print.possible_blockage" for e in events3)

    # 4. Condition clears: Progress advances to 31% and layer 51
    t_resume = t_stall3 + timedelta(seconds=10)
    p_resume = TelemetryPatch(
        printer_id=printer_id,
        timestamp=t_resume,
        gcode_state="RUNNING",
        progress=31,
        layer=51,
    )
    events4 = await state_manager.apply_patch(p_resume)

    # Should emit print.blockage_cleared!
    cleared_events = [e for e in events4 if e.event_type == "print.blockage_cleared"]
    assert len(cleared_events) == 1
    assert cleared_events[0].severity.value == "info"

    # Active alerts should now be empty
    assert len(state_manager.get_active_alerts(printer_id)) == 0

    # In database, alert should be marked RESOLVED
    db_alerts = await alert_repo.list_all(printer_id)
    assert len(db_alerts) >= 1
    resolved_alert = [a for a in db_alerts if a.alert_type == "print.possible_blockage"][0]
    assert resolved_alert.status == AlertStatus.RESOLVED
    assert resolved_alert.resolved_at is not None


@pytest.mark.asyncio
async def test_filament_runout_alert_lifecycle(state_manager, repositories):
    printer_id = "test-a1"
    t0 = datetime(2026, 9, 8, 13, 0, 0, tzinfo=timezone.utc)

    await state_manager.apply_patch(
        TelemetryPatch(
            printer_id=printer_id,
            timestamp=t0,
            gcode_state="RUNNING",
            subtask_name="real-print.3mf",
            subtask_id="real-subtask",
            progress=42,
            layer=80,
        )
    )

    events = await state_manager.apply_patch(
        TelemetryPatch(
            printer_id=printer_id,
            timestamp=t0 + timedelta(seconds=1),
            gcode_state="PAUSE",
            progress=42,
            layer=80,
            filament_runout=True,
            filament_runout_details={"source": "ams.ams[].tray[].remain", "value": 0},
        )
    )
    runout_events = [e for e in events if e.event_type == "filament.runout"]
    assert len(runout_events) == 1
    assert runout_events[0].severity.value == "critical"
    assert not any(e.event_type == "print.paused" for e in events)
    assert "camera_snapshot_url" not in runout_events[0].payload

    repeated = await state_manager.apply_patch(
        TelemetryPatch(printer_id=printer_id, timestamp=t0 + timedelta(seconds=2), filament_runout=True)
    )
    assert not any(e.event_type == "filament.runout" for e in repeated)

    cleared = await state_manager.apply_patch(
        TelemetryPatch(
            printer_id=printer_id,
            timestamp=t0 + timedelta(seconds=3),
            gcode_state="RUNNING",
            filament_runout=False,
        )
    )
    assert any(e.event_type == "filament.runout_cleared" for e in cleared)


@pytest.mark.asyncio
async def test_new_printer_task_supersedes_stale_injected_job(state_manager):
    printer_id = "test-a1"
    t0 = datetime(2026, 9, 8, 14, 0, 0, tzinfo=timezone.utc)
    await state_manager.apply_patch(
        TelemetryPatch(
            printer_id=printer_id,
            timestamp=t0,
            gcode_state="RUNNING",
            subtask_name="filament_test_part.3mf",
            subtask_id="test-subtask",
        )
    )

    events = await state_manager.apply_patch(
        TelemetryPatch(
            printer_id=printer_id,
            timestamp=t0 + timedelta(seconds=1),
            gcode_state="RUNNING",
            subtask_name="Zig-Zag Money Clip - 10g _ 25min",
            subtask_id="1233271833",
            task_id="1233271833",
            progress=43,
            layer=29,
        )
    )

    started = [e for e in events if e.event_type == "print.started"]
    assert len(started) == 1
    assert started[0].payload["filename"] == "Zig-Zag Money Clip - 10g _ 25min"
    assert state_manager.get_active_job(printer_id).filename == "Zig-Zag Money Clip - 10g _ 25min"

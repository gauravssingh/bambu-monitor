"""Unit tests for Print Job lifecycle, deterministic IDs, and transitions."""

import pytest
from datetime import datetime, timezone
from bambu_monitor.domain.print_job import JobStatus, PrintJob, generate_job_id
from bambu_monitor.domain.telemetry import TelemetryPatch


def test_deterministic_job_id():
    t1 = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)
    job_id_1 = generate_job_id("bambu-a1", "phone_stand.3mf", t1)
    job_id_2 = generate_job_id("bambu-a1", "phone_stand.3mf", t1)
    assert job_id_1 == job_id_2
    assert "bambu_a1" in job_id_1
    assert "phone_stand" in job_id_1
    assert str(int(t1.timestamp())) in job_id_1


@pytest.mark.asyncio
async def test_job_full_lifecycle(state_manager, repositories):
    printer_id = "test-a1"
    job_repo = repositories["job"]
    t0 = datetime(2026, 9, 8, 10, 0, 0, tzinfo=timezone.utc)

    # 1. Start Prepare
    p1 = TelemetryPatch(
        printer_id=printer_id,
        timestamp=t0,
        gcode_state="PREPARE",
        subtask_name="phone_stand.3mf",
        total_layers=150,
    )
    events1 = await state_manager.apply_patch(p1)
    assert len(events1) == 1
    assert events1[0].event_type == "print.started"

    active_job = state_manager.get_active_job(printer_id)
    assert active_job is not None
    assert active_job.filename == "phone_stand.3mf"
    assert active_job.status == JobStatus.PREPARE
    assert active_job.total_layers == 150

    # Verify persisted in SQLite
    db_job = await job_repo.get(active_job.id)
    assert db_job is not None
    assert db_job.status == JobStatus.PREPARE

    # 2. Transition to Running
    t1 = datetime(2026, 9, 8, 10, 5, 0, tzinfo=timezone.utc)
    p2 = TelemetryPatch(
        printer_id=printer_id,
        timestamp=t1,
        gcode_state="RUNNING",
        progress=10,
        layer=15,
    )
    await state_manager.apply_patch(p2)
    assert active_job.progress == 10
    assert active_job.layer == 15

    # 3. Transition to Pause
    t2 = datetime(2026, 9, 8, 10, 20, 0, tzinfo=timezone.utc)
    p3 = TelemetryPatch(
        printer_id=printer_id,
        timestamp=t2,
        gcode_state="PAUSE",
    )
    events3 = await state_manager.apply_patch(p3)
    assert len(events3) == 1
    assert events3[0].event_type == "print.paused"
    assert active_job.status == JobStatus.PAUSED

    # 4. Resume
    t3 = datetime(2026, 9, 8, 10, 25, 0, tzinfo=timezone.utc)
    p4 = TelemetryPatch(
        printer_id=printer_id,
        timestamp=t3,
        gcode_state="RUNNING",
    )
    events4 = await state_manager.apply_patch(p4)
    assert len(events4) == 1
    assert events4[0].event_type == "print.resumed"
    assert active_job.status == JobStatus.RUNNING

    # 5. Complete
    t4 = datetime(2026, 9, 8, 11, 0, 0, tzinfo=timezone.utc)
    p5 = TelemetryPatch(
        printer_id=printer_id,
        timestamp=t4,
        gcode_state="FINISH",
    )
    events5 = await state_manager.apply_patch(p5)
    assert any(e.event_type == "print.completed" for e in events5)

    # Active job should be cleared from memory
    assert state_manager.get_active_job(printer_id) is None

    # But completed job must be persisted in DB
    completed_db_job = await job_repo.get(active_job.id)
    assert completed_db_job is not None
    assert completed_db_job.status == JobStatus.COMPLETED
    assert completed_db_job.completed_at is not None
    assert completed_db_job.duration_seconds == 3600  # 1 hour

"""Integration test verifying service restart recovery and job re-attachment."""

import pytest
from datetime import datetime, timezone
from bambu_monitor.domain.print_job import JobStatus, PrintJob
from bambu_monitor.domain.telemetry import TelemetryPatch
from bambu_monitor.state.manager import StateManager


@pytest.mark.asyncio
async def test_restart_reconciliation_reattaches_without_duplicate_jobs(test_settings, repositories):
    printer_id = "test-a1"
    job_repo = repositories["job"]
    printer_repo = repositories["printer"]
    t0 = datetime(2026, 9, 8, 14, 0, 0, tzinfo=timezone.utc)

    # Ensure printer exists
    from bambu_monitor.domain.printer import Printer
    await printer_repo.save(Printer(id=printer_id, model="A1", serial_number="SN_RESTART", host="10.0.0.1"))

    # 1. Simulate active print job in SQLite before service restarted
    existing_job = PrintJob.create(
        printer_id=printer_id,
        filename="iron_man_helmet.3mf",
        started_at=t0,
        status=JobStatus.RUNNING,
        progress=45,
        layer=90,
        total_layers=200,
        remaining_seconds=18000,
    )
    await job_repo.save(existing_job)

    # Verify 1 job in DB
    initial_jobs = await job_repo.list_for_printer(printer_id)
    assert len(initial_jobs) == 1
    assert initial_jobs[0].id == existing_job.id

    # 2. Simulate Bambu Monitor startup with fresh StateManager (as happens on process restart)
    new_state_manager = StateManager(
        settings=test_settings,
        printer_repo=repositories["printer"],
        job_repo=repositories["job"],
        alert_repo=repositories["alert"],
        event_repo=repositories["event"],
        outbox_repo=repositories["outbox"],
    )
    new_state_manager.register_printer(printer_id, model="A1")

    # Initial synchronized telemetry (e.g. from pushall after restart)
    t_restart = datetime(2026, 9, 8, 14, 10, 0, tzinfo=timezone.utc)
    initial_pushall_patch = TelemetryPatch(
        printer_id=printer_id,
        timestamp=t_restart,
        gcode_state="RUNNING",
        subtask_name="iron_man_helmet.3mf",
        progress=46,
        layer=92,
        total_layers=200,
        remaining_seconds=17500,
        online=True,
    )

    # Run startup reconciliation
    await new_state_manager.reconcile_on_startup(printer_id, initial_pushall_patch)

    # Apply telemetry patch
    events = await new_state_manager.apply_patch(initial_pushall_patch)

    # VERIFICATION:
    # 1. No duplicate 'print.started' event must be emitted!
    assert not any(e.event_type == "print.started" for e in events)

    # 2. Active job in memory should be the EXACT same job ID from DB
    reconciled_job = new_state_manager.get_active_job(printer_id)
    assert reconciled_job is not None
    assert reconciled_job.id == existing_job.id
    assert reconciled_job.progress == 46
    assert reconciled_job.layer == 92

    # 3. Total jobs in DB must STILL BE 1 (no duplicate job row created!)
    jobs_after_reconciliation = await job_repo.list_for_printer(printer_id)
    assert len(jobs_after_reconciliation) == 1
    assert jobs_after_reconciliation[0].id == existing_job.id

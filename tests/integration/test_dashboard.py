"""Integration tests for the standalone web dashboard UI (/ and /dashboard), event page (/events)
and the generic timelapse gallery page (/gallery)."""

from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from bambu_monitor.api.app import create_app
from bambu_monitor.domain.alerts import Alert, AlertSeverity
from bambu_monitor.domain.print_job import JobStatus, PrintJob
from bambu_monitor.domain.printer import Printer
from bambu_monitor.storage.database import Database
from bambu_monitor.storage.repositories import AlertRepository, JobRepository, PrinterRepository


async def _get(client: AsyncClient, path: str) -> str:
    resp = await client.get(path)
    assert resp.status_code == 200, path
    assert resp.headers["content-type"].startswith("text/html")
    return resp.text


@pytest.mark.asyncio
async def test_root_redirects_to_dashboard(test_settings):
    app = create_app(test_settings)
    transport = ASGITransport(app=app, client=("127.0.0.1", 123))
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        async with app.router.lifespan_context(app):
            resp = await client.get("/", follow_redirects=False)
            assert resp.status_code == 302
            assert resp.headers["location"] == "/dashboard"


@pytest.mark.asyncio
async def test_dashboard_shell_and_information_hierarchy(test_settings):
    app = create_app(test_settings)
    transport = ASGITransport(app=app, client=("127.0.0.1", 123))
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        async with app.router.lifespan_context(app):
            text = await _get(client, "/dashboard")
            # Controls moved into the top nav; no separate page-title block.
            assert ">Dashboard</h1>" not in text
            assert 'id="btn-refresh"' in text
            assert 'data-live="updated"' in text
            # Hierarchy: summary cards, devices, prints, alerts, outbox, sys status.
            assert "Recent Prints" in text
            assert "Outbox &amp; Delivery" in text
            assert "All systems operational" in text
            # Registered printers appear as device cards wired to the canonical gallery.
            assert 'data-pid="test-a1"' in text
            assert 'data-pid="test-a1-mini"' in text
            assert "href=\"/gallery?printer=test-a1\"" in text
            assert "/api/v1/printers/test-a1/status" in text
            # Friendly empty states.
            assert "No prints recorded yet" in text
            assert "No alerts" in text
            # Recent activity noise is NOT on the dashboard anymore.
            assert "Recent activity" not in text
            # Five summary cards present.
            assert 'data-stat="db"' in text
            assert 'data-stat="online"' in text
            assert 'data-stat="now"' in text
            assert 'data-stat="alerts"' in text
            assert 'data-stat="sessions"' in text


@pytest.mark.asyncio
async def test_dashboard_shows_seeded_history_and_alerts(test_settings):
    db = Database(db_path=test_settings.database.path)
    await db.init_db()
    job_repo = JobRepository(db)
    alert_repo = AlertRepository(db)
    printer_repo = PrinterRepository(db)

    for p_cfg in test_settings.printers:
        await printer_repo.save(
            Printer(id=p_cfg.id, model=p_cfg.model,
                    serial_number=p_cfg.serial_number, host=p_cfg.host)
        )

    started = datetime(2026, 9, 9, 9, 0, tzinfo=timezone.utc)
    job = PrintJob.create(
        printer_id="test-a1-mini",
        filename="benchy_boat.gcode",
        status=JobStatus.COMPLETED,
        progress=100,
        layer=104,
        total_layers=104,
        started_at=started,
    )
    job.completed_at = datetime(2026, 9, 9, 10, 1, 1, tzinfo=timezone.utc)
    job.duration_seconds = 3661
    await job_repo.save(job)

    alert = Alert.create(
        printer_id="test-a1-mini",
        alert_type="filament_runout",
        severity=AlertSeverity.CRITICAL,
        details={"spool": "PLA-White", "message": "Runout detected"},
    )
    await alert_repo.save(alert)

    app = create_app(test_settings)
    transport = ASGITransport(app=app, client=("127.0.0.1", 123))
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        async with app.router.lifespan_context(app):
            text = await _get(client, "/dashboard")
            # Recent prints: readable filename, formatted duration, result chip.
            assert "benchy_boat.gcode" in text
            assert "1h 1m 1s" in text
            assert "Completed" in text
            # Alerts card: humanized type surfaced without flooding.
            assert "Filament Runout" in text


@pytest.mark.asyncio
async def test_dashboard_reflects_live_active_print(test_settings):
    app = create_app(test_settings)
    transport = ASGITransport(app=app, client=("127.0.0.1", 123))
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        async with app.router.lifespan_context(app):
            resp = await client.post(
                "/api/v1/printers/test-a1/telemetry",
                json={
                    "print": {
                        "gcode_state": "RUNNING",
                        "subtask_name": "cat_figurine.3mf",
                        "mc_percent": 15,
                        "layer_num": 30,
                        "total_layer_num": 200,
                        "mc_remaining_time": 5400,
                    }
                },
            )
            assert resp.status_code == 200
            text = await _get(client, "/dashboard")
            assert "cat_figurine.3mf" in text
            assert "15%" in text
            assert "Printing" in text
            assert 'data-pid="test-a1"' in text
            assert 'data-state="printing"' in text


@pytest.mark.asyncio
async def test_events_page_is_dedicated(test_settings):
    app = create_app(test_settings)
    transport = ASGITransport(app=app, client=("127.0.0.1", 123))
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        async with app.router.lifespan_context(app):
            await client.post(
                "/api/v1/printers/test-a1/telemetry",
                json={"print": {"gcode_state": "RUNNING", "subtask_name": "demo.gcode",
                                "mc_percent": 1, "layer_num": 1, "total_layer_num": 10}},
            )
            text = await _get(client, "/events")
            assert "Event history" in text
            # Filter controls: search, printer, event type, severity, time, clear.
            assert 'id="ev-q"' in text
            assert 'id="ev-type"' in text
            assert 'id="ev-sev"' in text
            assert 'id="ev-when"' in text
            assert 'id="ev-clear"' in text
            assert 'data-ev-count' in text
            # Rows carry filterable attributes.
            assert 'data-ts=' in text
            # Click-to-inspect modal with embedded raw JSON.
            assert 'id="ev-modal"' in text
            assert 'id="ev-modal-json"' in text
            assert 'data-json="' in text


@pytest.mark.asyncio
async def test_gallery_generic_page_and_legacy_alias(test_settings):
    """The HTML gallery lives at /gallery with a device selector; the old API path aliases it."""
    app = create_app(test_settings)
    transport = ASGITransport(app=app, client=("127.0.0.1", 123))
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        async with app.router.lifespan_context(app):
            # No param -> defaults to the first registered printer (test-a1) preselected.
            text = await _get(client, "/gallery")
            assert "gallery-device" in text
            assert 'value="test-a1" selected' in text
            assert 'value="test-a1-mini"' in text
            # Explicit printer param switches the preselected device.
            text2 = await _get(client, "/gallery?printer=test-a1-mini")
            assert 'value="test-a1-mini" selected' in text2
            # Legacy API path still serves the same HTML gallery (no breakage).
            legacy = await _get(client, "/api/v1/printers/test-a1/timelapses/gallery")
            assert "gallery-device" in legacy

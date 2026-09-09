"""End-to-End Fixture Pipeline integration test.

Tests the full Phase 1 pipeline with no printer connected:
fixture patch
      ↓
state merge
      ↓
print lifecycle
      ↓
alert lifecycle
      ↓
event
      ↓
SQLite (WAL)
      ↓
REST API (/prints/active)
      ↓
SSE (/events/stream)
      ↓
outbox (pending)
"""

import asyncio
from datetime import datetime, timedelta, timezone
import pytest
from httpx import AsyncClient



@pytest.mark.asyncio
async def test_full_fixture_pipeline_end_to_end(
    async_client: AsyncClient,
    fixtures,
):
    printer_id = "test-a1"

    # Capture SSE events streamed during the pipeline via StateManager subscriber
    app = async_client._transport.app
    state_manager = app.state.state_manager

    sse_events = []
    stop_listener = asyncio.Event()

    async def sse_listener():
        try:
            async for event in state_manager.subscribe_events(printer_id):
                sse_events.append(event)
                if stop_listener.is_set():
                    break
        except asyncio.CancelledError:
            pass

    listener_task = asyncio.create_task(sse_listener())
    await asyncio.sleep(0.05)

    # 1. Step: Printer Idle
    idle_data = fixtures("idle_telemetry.json")
    resp_idle = await async_client.post(f"/api/v1/printers/{printer_id}/telemetry", json=idle_data)
    assert resp_idle.status_code == 200

    # REST check: active print should be 204
    resp_active = await async_client.get(f"/api/v1/printers/{printer_id}/prints/active")
    assert resp_active.status_code == 204

    # 2. Step: Print Starts (Prepare)
    prepare_data = fixtures("prepare_telemetry.json")
    resp_prep = await async_client.post(f"/api/v1/printers/{printer_id}/telemetry", json=prepare_data)
    assert resp_prep.status_code == 200
    events_prep = resp_prep.json()
    assert any(e["event_type"] == "print.started" for e in events_prep)

    # REST check: active print should now be 200 with job details
    resp_active2 = await async_client.get(f"/api/v1/printers/{printer_id}/prints/active")
    assert resp_active2.status_code == 200
    active_job = resp_active2.json()
    assert active_job["filename"] == "phone_stand.3mf"
    assert active_job["status"] == "prepare"

    # 3. Step: Progress Patches (Running, partial patch 1)
    running_data = fixtures("printing_telemetry_patch1.json")
    resp_run1 = await async_client.post(f"/api/v1/printers/{printer_id}/telemetry", json=running_data)
    assert resp_run1.status_code == 200

    # 4. Step: Partial Patch 2 (Only progress updates to 21%, layer 30 survives!)
    patch2_data = fixtures("printing_telemetry_patch2.json")
    resp_run2 = await async_client.post(f"/api/v1/printers/{printer_id}/telemetry", json=patch2_data)
    assert resp_run2.status_code == 200

    resp_active3 = await async_client.get(f"/api/v1/printers/{printer_id}/prints/active")
    assert resp_active3.status_code == 200
    job3 = resp_active3.json()
    assert job3["progress"] == 21
    assert job3["layer"] == 30  # Preserved from patch 1!

    # 5. Step: Stall / Blockage Condition
    # In prepare_telemetry, total_layers was 150, remaining_seconds was 3600.
    # At progress=21 (elapsed ~720s), min_check is 300s.
    # Inject patch with advanced timestamp exceeding stall timeout (350s later)
    t_stall = datetime.now(timezone.utc) + timedelta(seconds=400)
    stall_patch = {
        "timestamp": t_stall.isoformat(),
        "state": "printing",
        "progress": 21,
        "layer": 30,
    }
    resp_stall = await async_client.post(f"/api/v1/printers/{printer_id}/telemetry", json=stall_patch)
    events_stall = resp_stall.json()
    assert any(e["event_type"] == "print.possible_blockage" for e in events_stall)

    # REST check: Alert should be ACTIVE
    resp_alerts = await async_client.get(f"/api/v1/printers/{printer_id}/alerts?active_only=true")
    active_alerts = resp_alerts.json()
    assert len(active_alerts) == 1
    assert active_alerts[0]["alert_type"] == "print.possible_blockage"
    assert active_alerts[0]["status"] == "active"

    # 6. Step: Duplicate condition suppressed
    t_stall2 = t_stall + timedelta(seconds=20)
    stall_patch2 = {
        "timestamp": t_stall2.isoformat(),
        "state": "printing",
        "progress": 21,
        "layer": 30,
    }
    resp_stall2 = await async_client.post(f"/api/v1/printers/{printer_id}/telemetry", json=stall_patch2)
    assert not any(e["event_type"] == "print.possible_blockage" for e in resp_stall2.json())

    # 7. Step: Condition clears (progress advances)
    t_resume = t_stall2 + timedelta(seconds=10)
    resume_patch = {
        "timestamp": t_resume.isoformat(),
        "state": "printing",
        "progress": 22,
        "layer": 31,
    }
    resp_resume = await async_client.post(f"/api/v1/printers/{printer_id}/telemetry", json=resume_patch)
    events_resume = resp_resume.json()
    assert any(e["event_type"] == "print.blockage_cleared" for e in events_resume)

    # REST check: Alert should no longer be active
    resp_alerts_cleared = await async_client.get(f"/api/v1/printers/{printer_id}/alerts?active_only=true")
    assert len(resp_alerts_cleared.json()) == 0

    # 8. Step: Print Completes
    finish_data = fixtures("finish_telemetry_patch.json")
    resp_finish = await async_client.post(f"/api/v1/printers/{printer_id}/telemetry", json=finish_data)
    events_finish = resp_finish.json()
    assert any(e["event_type"] == "print.completed" for e in events_finish)

    # REST check: active print should return 204
    resp_active_done = await async_client.get(f"/api/v1/printers/{printer_id}/prints/active")
    assert resp_active_done.status_code == 204

    # Stop SSE listener
    stop_listener.set()
    listener_task.cancel()
    try:
        await listener_task
    except asyncio.CancelledError:
        pass

    # 9. Verify SSE received the semantic events
    sse_types = [e.event_type for e in sse_events]
    assert "print.started" in sse_types
    assert "print.possible_blockage" in sse_types
    assert "print.blockage_cleared" in sse_types
    assert "print.completed" in sse_types

    # 10. Verify Outbox has pending entries
    resp_outbox = await async_client.get("/api/v1/outbox/status")
    outbox_status = resp_outbox.json()
    assert outbox_status["pending"] >= 4

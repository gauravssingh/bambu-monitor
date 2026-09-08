"""Integration tests for FastAPI REST endpoints."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health_endpoint(async_client: AsyncClient):
    resp = await async_client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["database"]["available"] is True
    assert data["database"]["journal_mode"] == "wal"
    assert data["printers"]["configured"] == 2
    assert "pending" in data["outbox"]


@pytest.mark.asyncio
async def test_list_printers(async_client: AsyncClient):
    resp = await async_client.get("/printers")
    assert resp.status_code == 200
    printers = resp.json()
    assert len(printers) == 2
    ids = [p["id"] for p in printers]
    assert "test-a1" in ids
    assert "test-a1-mini" in ids


@pytest.mark.asyncio
async def test_active_print_endpoint_idle_and_active(async_client: AsyncClient):
    printer_id = "test-a1"

    # 1. When idle, returns 204 No Content
    resp_idle = await async_client.get(f"/api/v1/printers/{printer_id}/prints/active")
    assert resp_idle.status_code == 204

    # 2. Inject telemetry starting a print
    telemetry_payload = {
        "print": {
            "gcode_state": "RUNNING",
            "subtask_name": "cat_figurine.3mf",
            "mc_percent": 15,
            "layer_num": 30,
            "total_layer_num": 200,
            "mc_remaining_time": 60,
        }
    }
    resp_inject = await async_client.post(
        f"/api/v1/printers/{printer_id}/telemetry",
        json=telemetry_payload,
    )
    assert resp_inject.status_code == 200
    events = resp_inject.json()
    assert any(e["event_type"] == "print.started" for e in events)

    # 3. Active print endpoint should now return 200 with job details
    resp_active = await async_client.get(f"/api/v1/printers/{printer_id}/prints/active")
    assert resp_active.status_code == 200
    job = resp_active.json()
    assert job["filename"] == "cat_figurine.3mf"
    assert job["status"] == "running"
    assert job["progress"] == 15
    assert job["layer"] == 30
    assert job["total_layers"] == 200

    # 4. Status endpoint returns O(1) in-memory state
    resp_status = await async_client.get(f"/api/v1/printers/{printer_id}/status")
    assert resp_status.status_code == 200
    status_data = resp_status.json()
    assert status_data["printer_id"] == printer_id
    assert status_data["state"] == "printing"
    assert status_data["print"]["filename"] == "cat_figurine.3mf"

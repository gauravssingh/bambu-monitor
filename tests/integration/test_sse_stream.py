"""Integration test for Server-Sent Events (SSE) stream."""

import asyncio
import json
import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_sse_event_stream(async_client: AsyncClient):
    printer_id = "test-a1"

    # Start listening to SSE in background task with limit=1
    async def listen_sse():
        resp = await async_client.get(f"/api/v1/printers/{printer_id}/events/stream?limit=2")
        assert resp.status_code == 200
        return resp.text

    listener_task = asyncio.create_task(listen_sse())
    await asyncio.sleep(0.05)

    # Inject telemetry that triggers events (printer.online and print.started)
    telemetry_payload = {
        "print": {
            "gcode_state": "PREPARE",
            "subtask_name": "benchy.3mf",
            "online": True,
        }
    }
    resp = await async_client.post(
        f"/api/v1/printers/{printer_id}/telemetry",
        json=telemetry_payload,
    )
    assert resp.status_code == 200

    # Wait for listener to receive the streamed events
    sse_text = await asyncio.wait_for(listener_task, timeout=3.0)

    assert "event: domain_event" in sse_text
    assert "printer.online" in sse_text
    assert "print.started" in sse_text
    assert "benchy.3mf" in sse_text

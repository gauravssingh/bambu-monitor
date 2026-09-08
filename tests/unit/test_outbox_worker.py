"""Unit tests for Phase 4 OutboxDeliveryWorker and WebhookClient."""

import asyncio
from datetime import datetime, timezone
import json
import pytest
from unittest.mock import AsyncMock, patch

from bambu_monitor.config import DeliveryConfig
from bambu_monitor.delivery.webhook import WebhookClient
from bambu_monitor.delivery.worker import OutboxDeliveryWorker
from bambu_monitor.domain.events import DomainEvent, EventSeverity, OutboxStatus
from bambu_monitor.domain.printer import Printer


@pytest.mark.asyncio
async def test_webhook_client_hmac_and_headers():
    client = WebhookClient(timeout_seconds=2.0, secret="test-secret-key")
    payload = {
        "event_id": "evt_test123",
        "event_type": "print.completed",
        "source": "bambu-a1",
        "data": "value",
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        success, code, err = await client.send("http://localhost:8644/webhooks/bambu-printer", payload)
        assert success is True
        assert code == 200
        assert err is None

        # Verify headers
        called_headers = mock_post.call_args[1]["headers"]
        assert called_headers["Content-Type"] == "application/json"
        assert called_headers["X-Event-ID"] == "evt_test123"
        assert called_headers["X-Event-Type"] == "print.completed"
        assert called_headers["X-Printer-ID"] == "bambu-a1"
        assert called_headers["X-Gitlab-Token"] == "test-secret-key"
        assert "X-Hub-Signature-256" in called_headers
        assert called_headers["X-Hub-Signature-256"].startswith("sha256=")


@pytest.mark.asyncio
async def test_worker_successful_fifo_delivery(repositories):
    outbox_repo = repositories["outbox"]
    printer_repo = repositories["printer"]
    event_repo = repositories["event"]

    p = Printer(id="printer-1", model="A1", serial_number="SN1", host="10.0.0.1")
    await printer_repo.save(p)

    e1 = DomainEvent.create("printer-1", "print.started", EventSeverity.INFO, {"job": "j1"})
    e2 = DomainEvent.create("printer-1", "print.completed", EventSeverity.INFO, {"job": "j1"})
    await event_repo.save(e1)
    await event_repo.save(e2)

    msg1 = await outbox_repo.enqueue(e1, "http://mock-endpoint")
    msg2 = await outbox_repo.enqueue(e2, "http://mock-endpoint")

    mock_client = AsyncMock()
    mock_client.send.return_value = (True, 200, None)

    cfg = DeliveryConfig(
        enabled=True,
        endpoint="http://mock-endpoint",
        poll_interval_seconds=0.01,
        retry_attempts=3,
    )
    worker = OutboxDeliveryWorker(
        outbox_repo=outbox_repo,
        printer_repo=printer_repo,
        config=cfg,
        webhook_client=mock_client,
    )

    # First drain -> delivers msg1
    count1 = await worker.drain_once()
    assert count1 == 1
    peek1 = await outbox_repo.peek_next_for_printer("printer-1")
    assert peek1.id == msg2.id

    # Second drain -> delivers msg2
    count2 = await worker.drain_once()
    assert count2 == 1
    peek2 = await outbox_repo.peek_next_for_printer("printer-1")
    assert peek2 is None

    counts = await outbox_repo.get_counts()
    assert counts["delivered"] == 2
    assert counts["pending"] == 0


@pytest.mark.asyncio
async def test_worker_permanent_error_routes_to_dlq(repositories):
    outbox_repo = repositories["outbox"]
    printer_repo = repositories["printer"]
    event_repo = repositories["event"]

    p = Printer(id="printer-dlq", model="A1", serial_number="SNDLQ", host="10.0.0.2")
    await printer_repo.save(p)

    e = DomainEvent.create("printer-dlq", "print.failed", EventSeverity.CRITICAL, {"err": 99})
    await event_repo.save(e)
    msg = await outbox_repo.enqueue(e, "http://mock-endpoint")

    mock_client = AsyncMock()
    mock_client.send.return_value = (False, 400, "HTTP 400 Bad Request")

    cfg = DeliveryConfig(enabled=True, endpoint="http://mock-endpoint")
    worker = OutboxDeliveryWorker(
        outbox_repo=outbox_repo,
        printer_repo=printer_repo,
        config=cfg,
        webhook_client=mock_client,
    )

    # Drain once -> receives 400 Bad Request (permanent client error)
    await worker.drain_once()

    counts = await outbox_repo.get_counts()
    assert counts["failed_dlq"] == 1
    assert counts["pending"] == 0

    # Queue should be unblocked for any next message
    next_msg = await outbox_repo.peek_next_for_printer("printer-dlq")
    assert next_msg is None


@pytest.mark.asyncio
async def test_worker_event_filtering(repositories):
    outbox_repo = repositories["outbox"]
    printer_repo = repositories["printer"]
    event_repo = repositories["event"]

    p = Printer(id="printer-flt", model="A1", serial_number="SNFLT", host="10.0.0.3")
    await printer_repo.save(p)

    # e_ignored is not in filter_events
    e_ignored = DomainEvent.create("printer-flt", "printer.online", EventSeverity.INFO, {})
    # e_accepted is in filter_events
    e_accepted = DomainEvent.create("printer-flt", "print.completed", EventSeverity.INFO, {"job": "ok"})
    await event_repo.save(e_ignored)
    await event_repo.save(e_accepted)

    await outbox_repo.enqueue(e_ignored, "http://mock-endpoint")
    await outbox_repo.enqueue(e_accepted, "http://mock-endpoint")

    mock_client = AsyncMock()
    mock_client.send.return_value = (True, 200, None)

    cfg = DeliveryConfig(
        enabled=True,
        endpoint="http://mock-endpoint",
        filter_events=["print.completed", "print.failed"],
    )
    worker = OutboxDeliveryWorker(
        outbox_repo=outbox_repo,
        printer_repo=printer_repo,
        config=cfg,
        webhook_client=mock_client,
    )

    # First drain: e_ignored is filtered out, acknowledging it without calling webhook
    await worker.drain_once()
    assert mock_client.send.call_count == 0

    # Second drain: e_accepted matches filter and is delivered via webhook
    await worker.drain_once()
    assert mock_client.send.call_count == 1
    assert mock_client.send.call_args[1]["payload"]["event_type"] == "print.completed"

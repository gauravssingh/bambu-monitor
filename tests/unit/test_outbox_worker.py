"""Unit tests for Phase 4 OutboxDeliveryWorker and WebhookClient."""

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
        assert "X-Hub-Signature-256" in called_headers
        assert called_headers["X-Hub-Signature-256"].startswith("sha256=")

        # The signature must be a real HMAC over the exact transmitted bytes
        # with the configured secret — a prefix check alone would pass a
        # broken implementation (wrong key/body/algorithm).
        import hashlib
        import hmac as hmac_mod
        sent_body = mock_post.call_args[1]["content"]
        expected_sig = hmac_mod.new(
            b"test-secret-key", sent_body, hashlib.sha256
        ).hexdigest()
        assert called_headers["X-Hub-Signature-256"] == f"sha256={expected_sig}"


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

    _msg1 = await outbox_repo.enqueue(e1, "http://mock-endpoint")
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
    _msg = await outbox_repo.enqueue(e, "http://mock-endpoint")

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


def _make_worker(outbox_repo, printer_repo, mock_client, **cfg_kwargs) -> OutboxDeliveryWorker:
    cfg = DeliveryConfig(
        enabled=True,
        endpoint="http://mock-endpoint",
        poll_interval_seconds=0.01,
        **cfg_kwargs,
    )
    return OutboxDeliveryWorker(
        outbox_repo=outbox_repo,
        printer_repo=printer_repo,
        config=cfg,
        webhook_client=mock_client,
    )


@pytest.mark.asyncio
async def test_transient_failure_retries_with_fifo_holdback(repositories):
    """500 -> back to PENDING with attempts+1 and error recorded; the next
    event for the same printer must NOT be delivered while the first waits."""
    outbox_repo = repositories["outbox"]
    printer_repo = repositories["printer"]
    event_repo = repositories["event"]

    await printer_repo.save(Printer(id="p-retry", model="A1", serial_number="SNRETRY", host="10.0.0.9"))
    e1 = DomainEvent.create("p-retry", "print.started", EventSeverity.INFO, {"job": "j"})
    e2 = DomainEvent.create("p-retry", "print.completed", EventSeverity.INFO, {"job": "j"})
    await event_repo.save(e1)
    await event_repo.save(e2)
    await outbox_repo.enqueue(e1, "http://mock-endpoint")
    await outbox_repo.enqueue(e2, "http://mock-endpoint")

    mock_client = AsyncMock()
    mock_client.send.side_effect = [(False, 500, "HTTP 500"), (True, 200, None), (True, 200, None)]

    worker = _make_worker(outbox_repo, printer_repo, mock_client, retry_attempts=3, initial_backoff_seconds=3600)

    # Drain 1: e1 fails transiently, enters backoff
    delivered = await worker.drain_once()
    assert delivered == 0
    counts = await outbox_repo.get_counts()
    assert counts["pending"] == 2

    pending = await outbox_repo.list_pending("p-retry")
    e1_row = next(m for m in pending if m.event_id == e1.event_id)
    assert e1_row.attempts == 1
    assert e1_row.error_message == "HTTP 500"
    assert e1_row.status.value == "pending"

    # Drain 2 (e.g. after restart): backoff eligibility comes from the DB, so
    # e1 is still in backoff and e2 must NOT jump the queue.
    worker2 = _make_worker(outbox_repo, printer_repo, mock_client, retry_attempts=3, initial_backoff_seconds=3600)
    delivered = await worker2.drain_once()
    assert delivered == 0
    assert mock_client.send.call_count == 1  # no further send attempts

    # Simulate backoff elapsing by rewinding last_attempt_at
    from datetime import datetime, timedelta, timezone as tz
    old = datetime.now(tz.utc) - timedelta(hours=2)
    await outbox_repo.update_status(
        message_id=e1_row.id, status=OutboxStatus.PENDING, last_attempt_at=old
    )

    delivered = await worker2.drain_once()
    assert delivered == 1
    # e1 delivered; e2 still FIFO-blocked until its own turn
    delivered2 = await worker2.drain_once()
    assert delivered2 == 1
    counts = await outbox_repo.get_counts()
    assert counts["delivered"] == 2


@pytest.mark.asyncio
async def test_retry_exhaustion_routes_to_dlq(repositories):
    """Transient errors that exhaust retry_attempts must reach the DLQ."""
    outbox_repo = repositories["outbox"]
    printer_repo = repositories["printer"]
    event_repo = repositories["event"]

    await printer_repo.save(Printer(id="p-exh", model="A1", serial_number="SNEXH", host="10.0.0.8"))
    e = DomainEvent.create("p-exh", "print.failed", EventSeverity.CRITICAL, {"err": 1})
    await event_repo.save(e)
    await outbox_repo.enqueue(e, "http://mock-endpoint")

    mock_client = AsyncMock()
    mock_client.send.return_value = (False, 503, "HTTP 503")

    worker = _make_worker(outbox_repo, printer_repo, mock_client, retry_attempts=2, initial_backoff_seconds=0)

    from datetime import datetime, timedelta, timezone as tz
    await worker.drain_once()  # attempt 1 -> PENDING
    pending = await outbox_repo.list_pending("p-exh")
    e_row = next(m for m in pending if m.event_id == e.event_id)
    old = datetime.now(tz.utc) - timedelta(hours=1)
    await outbox_repo.update_status(
        message_id=e_row.id, status=OutboxStatus.PENDING, last_attempt_at=old
    )
    await worker.drain_once()  # attempt 2 -> exhausted -> FAILED
    counts = await outbox_repo.get_counts()
    assert counts["failed_dlq"] == 1
    assert mock_client.send.call_count == 2


@pytest.mark.asyncio
async def test_429_is_retried_not_dlq(repositories):
    outbox_repo = repositories["outbox"]
    printer_repo = repositories["printer"]
    event_repo = repositories["event"]

    await printer_repo.save(Printer(id="p-429", model="A1", serial_number="SN429", host="10.0.0.7"))
    e = DomainEvent.create("p-429", "print.started", EventSeverity.INFO, {})
    await event_repo.save(e)
    await outbox_repo.enqueue(e, "http://mock-endpoint")

    mock_client = AsyncMock()
    mock_client.send.return_value = (False, 429, "HTTP 429 Too Many Requests")
    worker = _make_worker(outbox_repo, printer_repo, mock_client, retry_attempts=2, initial_backoff_seconds=0)

    await worker.drain_once()
    counts = await outbox_repo.get_counts()
    assert counts["failed_dlq"] == 0
    assert counts["pending"] == 1


@pytest.mark.asyncio
async def test_stale_delivering_rows_are_reclaimed(repositories):
    outbox_repo = repositories["outbox"]
    printer_repo = repositories["printer"]
    event_repo = repositories["event"]

    await printer_repo.save(Printer(id="p-stale", model="A1", serial_number="SNSTALE", host="10.0.0.6"))
    e = DomainEvent.create("p-stale", "print.started", EventSeverity.INFO, {})
    await event_repo.save(e)
    row = await outbox_repo.enqueue(e, "http://mock-endpoint")

    # Simulate a crash mid-delivery: status delivering, attempt long ago
    from datetime import datetime, timedelta, timezone as tz
    old = datetime.now(tz.utc) - timedelta(hours=1)
    await outbox_repo.begin_delivery(message_id=row.id, attempts=1, last_attempt_at=old)

    counts = await outbox_repo.get_counts()
    assert counts["delivering"] == 1

    mock_client = AsyncMock()
    mock_client.send.return_value = (True, 200, None)
    worker = _make_worker(outbox_repo, printer_repo, mock_client, retry_attempts=3, initial_backoff_seconds=0)
    await worker.drain_once()  # reclaim runs at drain start, message is then delivered

    counts = await outbox_repo.get_counts()
    assert counts["delivering"] == 0
    assert counts["pending"] == 0
    assert counts["delivered"] == 1


@pytest.mark.asyncio
async def test_concurrent_claim_prevents_double_delivery(repositories):
    """begin_delivery is a compare-and-set: only one claimant wins."""
    outbox_repo = repositories["outbox"]
    event_repo = repositories["event"]
    printer_repo = repositories["printer"]

    await printer_repo.save(Printer(id="p-claim", model="A1", serial_number="SNCLAIM", host="10.0.0.5"))
    e = DomainEvent.create("p-claim", "print.started", EventSeverity.INFO, {})
    await event_repo.save(e)
    row = await outbox_repo.enqueue(e, "http://mock-endpoint")

    from datetime import datetime, timezone as tz
    now = datetime.now(tz.utc)
    first = await outbox_repo.begin_delivery(message_id=row.id, attempts=1, last_attempt_at=now)
    second = await outbox_repo.begin_delivery(message_id=row.id, attempts=1, last_attempt_at=now)
    assert first is True
    assert second is False

"""Unit tests for durable Outbox queue, per-printer FIFO, and DLQ status."""

import pytest
from datetime import datetime, timezone
from bambu_monitor.domain.events import DomainEvent, EventSeverity, OutboxStatus
from bambu_monitor.domain.printer import Printer


@pytest.mark.asyncio
async def test_outbox_rejects_duplicate_event_destination(repositories):
    """Duplicate outbox rows for the same (event_id, destination) would cause
    double webhook delivery after an ambiguous retry; the unique index plus
    ON CONFLICT DO NOTHING must make the second insert a no-op."""
    outbox_repo = repositories["outbox"]
    event_repo = repositories["event"]
    printer_repo = repositories["printer"]

    await printer_repo.save(Printer(id="printer-1", model="A1", serial_number="SNDUP", host="10.0.0.1"))
    event = DomainEvent.create("printer-1", "print.started", EventSeverity.INFO, {"job": "dup"})
    await event_repo.save(event)

    first = await outbox_repo.enqueue(event, "http://endpoint")
    second = await outbox_repo.enqueue(event, "http://endpoint")

    assert first.id is not None
    # Duplicate insert was suppressed (no new row id assigned)
    assert second.id is None

    pending = await outbox_repo.list_pending("printer-1")
    assert len(pending) == 1


@pytest.mark.asyncio
async def test_save_and_enqueue_is_idempotent_on_retry(repositories):
    """A caller retry after an ambiguous commit must not double-enqueue."""
    outbox_repo = repositories["outbox"]
    event_repo = repositories["event"]
    printer_repo = repositories["printer"]

    await printer_repo.save(Printer(id="printer-2", model="A1 Mini", serial_number="SNATOMIC", host="10.0.0.2"))
    event = DomainEvent.create("printer-2", "print.completed", EventSeverity.INFO, {"job": "atomic"})

    await event_repo.save_and_enqueue(event, "http://endpoint")
    await event_repo.save_and_enqueue(event, "http://endpoint")  # simulated retry

    pending = await outbox_repo.list_pending("printer-2")
    assert len(pending) == 1


@pytest.mark.asyncio
async def test_outbox_per_printer_fifo_and_status(repositories):
    outbox_repo = repositories["outbox"]
    event_repo = repositories["event"]
    printer_repo = repositories["printer"]

    # Ensure printers exist in DB
    p1 = Printer(id="printer-1", model="A1", serial_number="SN1", host="10.0.0.1")
    p2 = Printer(id="printer-2", model="A1 Mini", serial_number="SN2", host="10.0.0.2")
    await printer_repo.save(p1)
    await printer_repo.save(p2)

    # 1. Enqueue events for printer-1: started, then completed
    e1_start = DomainEvent.create("printer-1", "print.started", EventSeverity.INFO, {"job": "job1"})
    e1_done = DomainEvent.create("printer-1", "print.completed", EventSeverity.INFO, {"job": "job1"})
    await event_repo.save(e1_start)
    await event_repo.save(e1_done)

    # 2. Enqueue event for printer-2: started
    e2_start = DomainEvent.create("printer-2", "print.started", EventSeverity.INFO, {"job": "job2"})
    await event_repo.save(e2_start)

    msg1 = await outbox_repo.enqueue(e1_start, "http://endpoint")
    msg2 = await outbox_repo.enqueue(e1_done, "http://endpoint")
    msg3 = await outbox_repo.enqueue(e2_start, "http://endpoint")

    assert msg1.status == OutboxStatus.PENDING
    assert msg2.status == OutboxStatus.PENDING
    assert msg3.status == OutboxStatus.PENDING

    # 3. FIFO order for printer-1: peek_next should return msg1 (started)
    next_p1 = await outbox_repo.peek_next_for_printer("printer-1")
    assert next_p1 is not None
    assert next_p1.id == msg1.id
    assert next_p1.event_id == e1_start.event_id

    # 4. Independence: peek_next for printer-2 returns msg3 (started)
    next_p2 = await outbox_repo.peek_next_for_printer("printer-2")
    assert next_p2 is not None
    assert next_p2.id == msg3.id
    assert next_p2.event_id == e2_start.event_id

    # 5. Simulate delivery of msg1
    await outbox_repo.update_status(
        message_id=msg1.id,
        status=OutboxStatus.DELIVERED,
        delivered_at=datetime.now(timezone.utc),
    )

    # Now peek_next for printer-1 should advance to msg2 (completed)
    next_p1_after = await outbox_repo.peek_next_for_printer("printer-1")
    assert next_p1_after is not None
    assert next_p1_after.id == msg2.id
    assert next_p1_after.event_id == e1_done.event_id

    # 6. Dead Letter Queue / failed status
    await outbox_repo.update_status(
        message_id=msg2.id,
        status=OutboxStatus.FAILED,
        error_message="Permanent 400 Bad Request",
    )

    counts = await outbox_repo.get_counts()
    assert counts["delivered"] == 1
    assert counts["failed_dlq"] == 1
    assert counts["pending"] == 1  # msg3 on printer-2 is still pending

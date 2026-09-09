"""Background worker for reliable sequential per-printer FIFO event delivery."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import time
from typing import Optional

from bambu_monitor.config import DeliveryConfig
from bambu_monitor.delivery.webhook import WebhookClient
from bambu_monitor.domain.events import OutboxMessage, OutboxStatus
from bambu_monitor.storage.repositories import OutboxRepository, PrinterRepository

logger = logging.getLogger(__name__)


class OutboxDeliveryWorker:
    """Delivers pending domain events from the SQLite outbox in per-printer FIFO order.

    Guarantees:
    1. At-Least-Once Delivery to webhook destinations.
    2. Per-Printer FIFO: Events for printer A are delivered strictly in order.
       Retrying events on printer A hold back subsequent events for printer A.
    3. Partition Independence: Delivery failures on printer A do not block printer B.
    4. Dead-Letter Queue: Permanent errors (4xx) or exhausted retries transition to FAILED.
    """

    def __init__(
        self,
        outbox_repo: OutboxRepository,
        printer_repo: PrinterRepository,
        config: DeliveryConfig,
        webhook_client: Optional[WebhookClient] = None,
    ):
        self.outbox_repo = outbox_repo
        self.printer_repo = printer_repo
        self.config = config
        self.webhook_client = webhook_client or WebhookClient(
            timeout_seconds=config.timeout_seconds,
            secret=config.secret,
        )
        self._stopped = False
        self._task: Optional[asyncio.Task] = None
        self._consecutive_loop_failures = 0

    def start(self) -> None:
        """Start the background delivery task."""
        if self._task is None or self._task.done():
            self._stopped = False
            self._task = asyncio.create_task(self._run_loop())
            logger.info("Outbox delivery worker started (endpoint: %s)", self.config.endpoint)

    async def stop(self) -> None:
        """Gracefully stop the delivery task."""
        self._stopped = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("Outbox delivery worker stopped")

    async def _run_loop(self) -> None:
        while not self._stopped:
            try:
                await self.drain_once()
                self._consecutive_loop_failures = 0
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._consecutive_loop_failures += 1
                # A persistent loop failure (SQLITE_BUSY, disk full, repo bug)
                # must be visible: the service would otherwise look healthy
                # while silently stopping all delivery.
                (logger.error if self._consecutive_loop_failures >= 3 else logger.warning)(
                    "Outbox worker loop error (consecutive: %d): %s",
                    self._consecutive_loop_failures,
                    exc,
                )

            try:
                await asyncio.sleep(self.config.poll_interval_seconds)
            except asyncio.CancelledError:
                break

    async def drain_once(self) -> int:
        """Process one pending message per eligible printer. Returns total delivered count."""
        # Crash recovery: messages stuck in 'delivering' from a previous run
        # become claimable again once their delivery timeout has passed.
        await self.outbox_repo.reclaim_stale_delivering(
            stale_after_seconds=max(self.config.timeout_seconds * 2, 60.0)
        )

        printers = await self.printer_repo.list_all()
        delivered_count = 0

        for p in printers:
            msg = await self.outbox_repo.peek_next_for_printer(p.id)
            if not msg:
                continue

            # Persisted backoff: eligibility derives from last_attempt_at plus
            # the backoff tier for the attempts already made, so restarts and
            # concurrent drains honor the same schedule.
            if not self._is_eligible(msg):
                continue

            # Event filtering
            event_type = msg.payload.get("event_type")
            if self.config.filter_events and event_type not in self.config.filter_events:
                # Filtered out: mark as delivered to avoid blocking FIFO queue
                logger.debug("Event %s filtered out by delivery configuration", event_type)
                await self.outbox_repo.update_status(
                    message_id=msg.id,
                    status=OutboxStatus.DELIVERED,
                    delivered_at=datetime.now(timezone.utc),
                )
                continue

            # Process delivery
            delivered = await self._deliver_message(p.id, msg)
            if delivered:
                delivered_count += 1

        return delivered_count

    def _is_eligible(self, msg: OutboxMessage) -> bool:
        """True when the message's persisted backoff has elapsed."""
        if msg.attempts <= 0 or msg.last_attempt_at is None:
            return True
        backoff = min(
            self.config.max_backoff_seconds,
            self.config.initial_backoff_seconds
            * (self.config.backoff_multiplier ** (msg.attempts - 1)),
        )
        eligible_at = msg.last_attempt_at.timestamp() + backoff
        return time.time() >= eligible_at

    async def _deliver_message(self, printer_id: str, msg: OutboxMessage) -> bool:
        destination = msg.destination or self.config.endpoint
        attempts = msg.attempts + 1
        now_dt = datetime.now(timezone.utc)

        # Atomic claim: only proceed if this worker transitioned the row from
        # 'pending'. Prevents double delivery if two drains ever race; the
        # stale-'delivering' reclaim in drain_once preserves crash recovery.
        claimed = await self.outbox_repo.begin_delivery(
            message_id=msg.id, attempts=attempts, last_attempt_at=now_dt
        )
        if not claimed:
            return False

        success, status_code, error_msg = await self.webhook_client.send(
            destination=destination,
            payload=msg.payload,
        )

        if success:
            logger.info("Delivered event %s (%s) for printer %s", msg.event_id, msg.payload.get("event_type"), printer_id)
            await self.outbox_repo.update_status(
                message_id=msg.id,
                status=OutboxStatus.DELIVERED,
                attempts=attempts,
                last_attempt_at=now_dt,
                delivered_at=now_dt,
            )
            return True

        logger.warning(
            "Delivery failed for event %s (printer %s, attempt %d/%d): %s",
            msg.event_id, printer_id, attempts, self.config.retry_attempts, error_msg
        )

        # Evaluate permanent client errors (4xx except 429 Too Many Requests)
        is_permanent_client_error = bool(status_code and 400 <= status_code < 500 and status_code != 429)

        if is_permanent_client_error or attempts >= self.config.retry_attempts:
            # Poison pill or exhausted retries -> route to Dead Letter Queue (FAILED)
            logger.error(
                "Event %s for printer %s transitioned to Dead Letter Queue (FAILED). Reason: %s",
                msg.event_id, printer_id, error_msg
            )
            await self.outbox_repo.update_status(
                message_id=msg.id,
                status=OutboxStatus.FAILED,
                attempts=attempts,
                last_attempt_at=now_dt,
                error_message=error_msg,
            )
        else:
            # Exponential backoff retry. attempts/last_attempt_at are already
            # persisted by begin_delivery; eligibility is computed from the DB
            # in _is_eligible, so backoff survives process restarts.
            backoff = min(
                self.config.max_backoff_seconds,
                self.config.initial_backoff_seconds * (self.config.backoff_multiplier ** (attempts - 1)),
            )
            logger.info(
                "Event %s for printer %s scheduled for retry in %.1fs (attempt %d/%d)",
                msg.event_id,
                printer_id,
                backoff,
                attempts,
                self.config.retry_attempts,
            )
            await self.outbox_repo.update_status(
                message_id=msg.id,
                status=OutboxStatus.PENDING,
                attempts=attempts,
                last_attempt_at=now_dt,
                error_message=error_msg,
            )

        return False

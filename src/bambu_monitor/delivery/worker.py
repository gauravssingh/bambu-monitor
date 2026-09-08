"""Background worker for reliable sequential per-printer FIFO event delivery."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import time
from typing import Dict, List, Optional

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
        self._next_attempt_at: Dict[str, float] = {}
        self._stopped = False
        self._task: Optional[asyncio.Task] = None

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
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("Outbox worker loop error: %s", exc)

            try:
                await asyncio.sleep(self.config.poll_interval_seconds)
            except asyncio.CancelledError:
                break

    async def drain_once(self) -> int:
        """Process one pending message per eligible printer. Returns total delivered count."""
        printers = await self.printer_repo.list_all()
        delivered_count = 0

        for p in printers:
            now = time.time()
            if p.id in self._next_attempt_at and now < self._next_attempt_at[p.id]:
                # Printer queue is in backoff cooldown
                continue

            msg = await self.outbox_repo.peek_next_for_printer(p.id)
            if not msg:
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

    async def _deliver_message(self, printer_id: str, msg: OutboxMessage) -> bool:
        destination = msg.destination or self.config.endpoint
        attempts = msg.attempts + 1
        now_dt = datetime.now(timezone.utc)

        await self.outbox_repo.update_status(
            message_id=msg.id,
            status=OutboxStatus.DELIVERING,
            attempts=attempts,
            last_attempt_at=now_dt,
        )

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
            self._next_attempt_at.pop(printer_id, None)
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
            # Unblock printer queue so subsequent events can proceed
            self._next_attempt_at.pop(printer_id, None)
        else:
            # Exponential backoff retry
            backoff = min(
                self.config.max_backoff_seconds,
                self.config.initial_backoff_seconds * (self.config.backoff_multiplier ** (attempts - 1)),
            )
            self._next_attempt_at[printer_id] = time.time() + backoff
            await self.outbox_repo.update_status(
                message_id=msg.id,
                status=OutboxStatus.PENDING,
                attempts=attempts,
                last_attempt_at=now_dt,
                error_message=error_msg,
            )

        return False

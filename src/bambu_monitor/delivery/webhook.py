"""Webhook HTTP client for delivering outbox events to external consumers."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any, Dict, Optional, Tuple
import httpx

logger = logging.getLogger(__name__)


class WebhookClient:
    """Async HTTP client for dispatching domain events to webhook endpoints."""

    def __init__(self, timeout_seconds: float = 10.0, secret: Optional[str] = None):
        self.timeout = timeout_seconds
        self.secret = secret

    async def send(
        self,
        destination: str,
        payload: Dict[str, Any],
        headers_override: Optional[Dict[str, str]] = None,
    ) -> Tuple[bool, Optional[int], Optional[str]]:
        """Send JSON payload to webhook destination.

        Returns:
            (success: bool, status_code: Optional[int], error_message: Optional[str])
        """
        raw_body = json.dumps(payload, default=str).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "bambu-monitor/0.1.0",
        }

        # Contextual event headers if available
        if "event_id" in payload:
            headers["X-Event-ID"] = str(payload["event_id"])
        if "event_type" in payload:
            headers["X-Event-Type"] = str(payload["event_type"])
        if "source" in payload:
            headers["X-Printer-ID"] = str(payload["source"])
        elif "printer_id" in payload:
            headers["X-Printer-ID"] = str(payload["printer_id"])

        # Signature / Auth headers if secret configured
        if self.secret:
            sig = hmac.new(self.secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
            headers["X-Hub-Signature-256"] = f"sha256={sig}"

        if headers_override:
            headers.update(headers_override)

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(destination, content=raw_body, headers=headers)
                if 200 <= resp.status_code < 300:
                    return True, resp.status_code, None
                error_msg = f"HTTP {resp.status_code}: {resp.text[:300]}"
                return False, resp.status_code, error_msg
        except httpx.TimeoutException as exc:
            return False, None, f"Webhook timeout ({self.timeout}s): {exc}"
        except Exception as exc:
            return False, None, f"Webhook delivery error: {exc}"

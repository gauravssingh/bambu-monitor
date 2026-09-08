"""Bambu Lab MQTT TLS client for live telemetry ingestion and command publishing."""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from typing import Any, Callable, Dict, Optional
import paho.mqtt.client as mqtt

from bambu_monitor.bambu.credentials import get_access_code
from bambu_monitor.bambu.protocol import (
    BAMBU_LAN_USERNAME,
    BAMBU_MQTT_PORT,
    create_pushall_payload,
    get_report_topic,
    get_request_topic,
)
from bambu_monitor.domain.telemetry import TelemetryPatch
from bambu_monitor.state.manager import StateManager

logger = logging.getLogger(__name__)


class BambuMqttClient:
    """Manages an MQTT connection over TLS to a Bambu Lab printer."""

    def __init__(
        self,
        printer_id: str,
        serial_number: str,
        host: str,
        port: int = BAMBU_MQTT_PORT,
        access_code: Optional[str] = None,
        tls_verify: bool = False,
        state_manager: Optional[StateManager] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ):
        self.printer_id = printer_id
        self.serial_number = serial_number.strip()
        self.host = host.strip()
        self.port = port
        self._explicit_access_code = access_code
        self.tls_verify = tls_verify
        self.state_manager = state_manager
        self.loop = loop or asyncio.get_event_loop()

        self._connected = False
        self._stopped = False
        self._client: Optional[mqtt.Client] = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    def get_password(self) -> str:
        """Resolve LAN Access Code from explicit argument, keyring, or fallback store."""
        if self._explicit_access_code:
            return self._explicit_access_code
        stored = get_access_code(self.serial_number)
        return stored or ""

    def _setup_client(self) -> mqtt.Client:
        # Support paho-mqtt v2 CallbackAPIVersion if available
        if hasattr(mqtt, "CallbackAPIVersion"):
            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=f"bambu_monitor_{self.printer_id}",
            )
        else:
            client = mqtt.Client(client_id=f"bambu_monitor_{self.printer_id}")

        client.username_pw_set(BAMBU_LAN_USERNAME, self.get_password())

        # Configure TLS
        # Bambu Lab 3D printers in LAN Mode run an embedded MQTT broker on port 8883
        # with a self-signed X.509 certificate. Setting check_hostname=False and
        # verify_mode=CERT_NONE is strictly scoped to this local printer connection.
        # This bypass is NEVER applied to outbound consumer webhooks (e.g. Hermes).
        ssl_ctx = ssl.create_default_context()
        if not self.tls_verify:
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

        client.tls_set_context(ssl_ctx)

        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        return client

    def _on_connect(self, client, userdata, flags, rc, *extra_args):
        rc_val = rc.value if hasattr(rc, "value") else rc
        if rc_val == 0:
            logger.info("Connected to Bambu printer %s (%s) at %s:%d", self.printer_id, self.serial_number, self.host, self.port)
            self._connected = True

            # 1. Subscribe to report topic
            report_topic = get_report_topic(self.serial_number)
            client.subscribe(report_topic)
            logger.debug("Subscribed to report topic: %s", report_topic)

            # 2. Immediately request full state dump via pushall
            self.send_pushall()
        else:
            logger.error("MQTT connection failed for %s with code: %s", self.printer_id, rc_val)
            self._connected = False

    def _on_disconnect(self, client, userdata, *extra_args):
        logger.warning("Disconnected from Bambu printer %s (%s)", self.printer_id, self.serial_number)
        self._connected = False
        if self.state_manager and not self._stopped:
            # Mark offline in state manager
            offline_patch = TelemetryPatch(printer_id=self.printer_id, online=False)
            asyncio.run_coroutine_threadsafe(
                self.state_manager.apply_patch(offline_patch),
                self.loop,
            )

    def _on_message(self, client, userdata, msg):
        try:
            raw_payload = json.loads(msg.payload.decode("utf-8", errors="ignore"))
            patch = TelemetryPatch.from_raw(self.printer_id, raw_payload)
            # Route patch thread-safely into Phase 1 state manager
            if self.state_manager:
                asyncio.run_coroutine_threadsafe(
                    self.state_manager.apply_patch(patch),
                    self.loop,
                )
        except Exception as exc:
            logger.debug("Error parsing MQTT payload for %s: %s", self.printer_id, exc)

    def send_command(self, payload: Dict[str, Any]) -> bool:
        """Publish a command to device/{serial}/request."""
        if not self._client or not self._connected:
            return False
        topic = get_request_topic(self.serial_number)
        data = json.dumps(payload)
        info = self._client.publish(topic, data)
        return info.rc == mqtt.MQTT_ERR_SUCCESS

    def send_pushall(self) -> bool:
        """Send a pushall request to trigger a complete state report."""
        logger.info("Publishing pushall request for %s", self.printer_id)
        return self.send_command(create_pushall_payload())

    def update_host(self, new_ip: str) -> None:
        """Update printer IP address if changed by DHCP and reconnect."""
        clean_ip = new_ip.strip()
        if clean_ip and clean_ip != self.host:
            logger.info("Updating IP for printer %s: %s -> %s", self.printer_id, self.host, clean_ip)
            self.host = clean_ip
            if self._client and self._connected:
                self._client.disconnect()

    def start(self) -> None:
        """Connect and start background network thread."""
        self._stopped = False
        self._client = self._setup_client()
        try:
            self._client.connect_async(self.host, self.port, keepalive=60)
            self._client.loop_start()
        except Exception as exc:
            logger.error("Failed connecting to Bambu MQTT (%s:%d): %s", self.host, self.port, exc)

    def stop(self) -> None:
        """Disconnect and stop background loop."""
        self._stopped = True
        if self._client:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass
            self._client = None
        self._connected = False

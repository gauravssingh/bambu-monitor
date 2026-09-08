"""Unit tests for BambuMqttClient handling, connection, message dispatch, and IP updates."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio

from bambu_monitor.bambu.client import BambuMqttClient
from bambu_monitor.domain.telemetry import TelemetryPatch


@pytest.mark.asyncio
async def test_client_credentials_resolution(monkeypatch):
    serial = "01P00RESOLVETEST"
    monkeypatch.setenv(f"BAMBU_ACCESS_CODE_{serial}", "env_secret_123")

    client = BambuMqttClient(
        printer_id="test-p",
        serial_number=serial,
        host="192.168.1.100",
    )
    assert client.get_password() == "env_secret_123"

    # Explicit access code overrides env
    explicit_client = BambuMqttClient(
        printer_id="test-p",
        serial_number=serial,
        host="192.168.1.100",
        access_code="explicit_456",
    )
    assert explicit_client.get_password() == "explicit_456"


@pytest.mark.asyncio
async def test_on_connect_subscribes_and_pushes_all():
    client = BambuMqttClient(
        printer_id="test-a1",
        serial_number="01P00CONNECTTEST",
        host="192.168.1.50",
    )
    mock_mqtt = MagicMock()
    client._client = mock_mqtt

    # Simulate connect callback
    client._on_connect(mock_mqtt, None, None, 0)
    assert client.is_connected is True

    # Check subscribe
    mock_mqtt.subscribe.assert_called_once_with("device/01P00CONNECTTEST/report")

    # Check pushall published to request topic
    mock_mqtt.publish.assert_called_once()
    args, kwargs = mock_mqtt.publish.call_args
    assert args[0] == "device/01P00CONNECTTEST/request"
    payload = json.loads(args[1])
    assert payload["pushing"]["command"] == "pushall"


@pytest.mark.asyncio
async def test_on_message_routes_to_state_manager(state_manager):
    loop = asyncio.get_running_loop()
    client = BambuMqttClient(
        printer_id="test-a1",
        serial_number="01P00TEST123456",
        host="192.168.1.50",
        state_manager=state_manager,
        loop=loop,
    )

    raw_data = {
        "print": {
            "gcode_state": "RUNNING",
            "nozzle_temper": 215.5,
            "bed_temper": 60.0,
            "mc_percent": 42,
            "subtask_name": "benchy.gcode",
        }
    }

    mock_msg = MagicMock()
    mock_msg.payload = json.dumps(raw_data).encode("utf-8")

    client._on_message(None, None, mock_msg)
    # Give the event loop a tick to process the coroutine
    await asyncio.sleep(0.05)

    current = state_manager.get_state("test-a1")
    assert current is not None
    assert current.temperatures.nozzle == 215.5
    assert current.temperatures.bed == 60.0


@pytest.mark.asyncio
async def test_on_disconnect_marks_offline(state_manager):
    loop = asyncio.get_running_loop()
    client = BambuMqttClient(
        printer_id="test-a1",
        serial_number="01P00TEST123456",
        host="192.168.1.50",
        state_manager=state_manager,
        loop=loop,
    )
    client._connected = True

    client._on_disconnect(None, None)
    await asyncio.sleep(0.05)

    current = state_manager.get_state("test-a1")
    assert current is not None
    assert current.online is False


@pytest.mark.asyncio
async def test_update_host_reconnects_when_ip_changes():
    client = BambuMqttClient(
        printer_id="test-a1",
        serial_number="01P00IPTEST",
        host="192.168.1.50",
    )
    mock_mqtt = MagicMock()
    client._client = mock_mqtt
    client._connected = True

    # Same IP does nothing
    client.update_host("192.168.1.50")
    mock_mqtt.disconnect.assert_not_called()

    # Changed IP updates host and disconnects to trigger reconnect
    client.update_host("192.168.1.99")
    assert client.host == "192.168.1.99"
    mock_mqtt.disconnect.assert_called_once()

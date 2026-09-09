"""Unit tests for BambuMqttClient handling, connection, message dispatch, and IP updates."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from bambu_monitor.bambu.client import BambuMqttClient


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
async def test_update_host_recreates_client_when_ip_changes():
    """paho binds its host at connect time and never reconnects after an
    explicit disconnect, so an IP change must fully recreate the client."""
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

    # Changed IP tears down the old client and starts a fresh one bound to
    # the new host (connect_async, not the old client's stored host).
    with patch.object(BambuMqttClient, "_setup_client") as mock_setup:
        new_mock = MagicMock()
        mock_setup.return_value = new_mock
        client.update_host("192.168.1.99")

        assert client.host == "192.168.1.99"
        # Old client torn down (disconnect before loop_stop)
        mock_mqtt.disconnect.assert_called_once()
        mock_mqtt.loop_stop.assert_called_once()
        # New client created and started against the new host
        mock_setup.assert_called_once()
        new_mock.connect_async.assert_called_once_with("192.168.1.99", 8883, keepalive=60)
        new_mock.loop_start.assert_called_once()
        assert client._client is new_mock


@pytest.mark.asyncio
async def test_start_uses_nonblocking_connect_async():
    """connect() would block the event loop on DNS/TCP/TLS; only
    connect_async() may be used."""
    client = BambuMqttClient(
        printer_id="test-a1",
        serial_number="01P00ASYNCTEST",
        host="192.168.1.50",
    )
    with patch.object(BambuMqttClient, "_setup_client") as mock_setup:
        mock_mqtt = MagicMock()
        mock_setup.return_value = mock_mqtt
        client.start()

        mock_mqtt.connect.assert_not_called()
        mock_mqtt.connect_async.assert_called_once_with("192.168.1.50", 8883, keepalive=60)
        mock_mqtt.loop_start.assert_called_once()


@pytest.mark.asyncio
async def test_pushall_ack_does_not_cancel_pending_pause(state_manager):
    """Bambu ACKs and AMS deltas arrive on the report topic without a
    gcode_state; they must not cancel a pending sparse-PAUSE patch."""
    loop = asyncio.get_running_loop()
    client = BambuMqttClient(
        printer_id="test-a1",
        serial_number="01P00PAUSE1234",
        host="192.168.1.50",
        state_manager=state_manager,
        loop=loop,
    )

    def make_msg(payload):
        m = MagicMock()
        m.payload = json.dumps(payload).encode("utf-8")
        return m

    # 1. Sparse PAUSE delta -> stashed, awaiting detail
    client._on_message(None, None, make_msg({"print": {"gcode_state": "PAUSE"}}))
    assert client._awaiting_pause_detail is True
    assert client._pending_pause_patch is not None

    # 2. pushall ACK on the report topic (no gcode_state) — must NOT cancel
    client._on_message(None, None, make_msg({"command": "pushall", "result": "ok"}))
    assert client._awaiting_pause_detail is True
    assert client._pending_pause_patch is not None

    # 3. An authoritative RUNNING report supersedes and cancels the pause
    client._on_message(None, None, make_msg({"print": {"gcode_state": "RUNNING"}}))
    assert client._awaiting_pause_detail is False
    assert client._pending_pause_patch is None


@pytest.mark.asyncio
async def test_flush_pending_pause_submits_when_no_full_report(state_manager):
    loop = asyncio.get_running_loop()
    client = BambuMqttClient(
        printer_id="test-a1",
        serial_number="01P00FLUSH1234",
        host="192.168.1.50",
        state_manager=state_manager,
        loop=loop,
    )
    client._on_message(None, None, _mock_msg({"print": {"gcode_state": "PAUSE"}}))
    client._flush_pending_pause()

    await asyncio.sleep(0.05)
    current = state_manager.get_state("test-a1")
    assert current is not None
    assert current.state.value == "paused"


def _mock_msg(payload):
    m = MagicMock()
    m.payload = json.dumps(payload).encode("utf-8")
    return m

"""Integration tests for Bambu Monitor CLI commands and administration workflows."""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from bambu_monitor.api.app import create_app
from bambu_monitor.cli.commands import (
    cmd_devices,
    cmd_doctor,
    cmd_onboard,
    cmd_reconnect,
    cmd_remove,
    cmd_status,
)
from bambu_monitor.config import Settings
from bambu_monitor.domain.printer import Printer


@pytest.mark.asyncio
async def test_cmd_devices_output(test_settings: Settings, repositories, monkeypatch, capsys):
    # Ensure printer is saved
    await repositories["printer"].save(
        Printer(
            id="printer-cli-test",
            model="A1",
            serial_number="01P00CLITEST",
            host="192.168.1.80",
            online=True,
        )
    )

    await cmd_devices(settings=test_settings)
    captured = capsys.readouterr()
    assert "Configured printers:" in captured.out
    assert "printer-cli-test" in captured.out
    assert "01P00CLITEST" in captured.out
    assert "192.168.1.80" in captured.out


@pytest.mark.asyncio
async def test_cmd_status_output(test_settings: Settings, repositories, capsys):
    await cmd_status(settings=test_settings)
    captured = capsys.readouterr()
    assert "Bambu Monitor" in captured.out
    assert "Database" in captured.out
    assert "Healthy" in captured.out


@pytest.mark.asyncio
async def test_cmd_doctor_output(test_settings: Settings, capsys):
    await cmd_doctor(settings=test_settings)
    captured = capsys.readouterr()
    assert "Bambu Monitor Diagnostics" in captured.out
    assert "SQLite Database" in captured.out


@pytest.mark.asyncio
async def test_cmd_onboard_automated(test_settings: Settings, repositories, capsys):
    # Mock test_printer_connection to simulate successful handshake
    with patch("bambu_monitor.cli.commands.test_printer_connection", new_callable=AsyncMock) as mock_conn:
        mock_conn.return_value = True

        await cmd_onboard(
            serial="01P00ONBOARD12",
            ip="192.168.1.120",
            access_code="secret_code_1234",
            name="Lab A1",
            model="A1",
            settings=test_settings,
        )

        captured = capsys.readouterr()
        assert "TLS connection established" in captured.out
        assert "Credentials stored securely" in captured.out

        # Verify saved in SQLite
        printers = await repositories["printer"].list_all()
        serials = [p.serial_number for p in printers]
        assert "01P00ONBOARD12" in serials


@pytest.mark.asyncio
async def test_cmd_remove_printer(test_settings: Settings, repositories, capsys):
    await repositories["printer"].save(
        Printer(
            id="to-be-removed",
            model="A1",
            serial_number="01P00REMOVE123",
            host="192.168.1.90",
        )
    )

    await cmd_remove("to-be-removed", settings=test_settings)
    captured = capsys.readouterr()
    assert "removed" in captured.out

    remaining = await repositories["printer"].get("to-be-removed")
    assert remaining is None


@pytest.mark.asyncio
async def test_cmd_reconnect(test_settings: Settings, repositories, capsys):
    await repositories["printer"].save(
        Printer(
            id="reconnect-target",
            model="A1",
            serial_number="01P00RECONNECT",
            host="192.168.1.95",
        )
    )

    with patch("bambu_monitor.cli.commands.test_printer_connection", new_callable=AsyncMock) as mock_conn:
        mock_conn.return_value = True

        await cmd_reconnect("reconnect-target", settings=test_settings)
        captured = capsys.readouterr()
        assert "Reconnecting to Bambu printer 'reconnect-target'" in captured.out
        assert "TLS handshake established" in captured.out


@pytest.mark.asyncio
async def test_reconnect_api_endpoint(test_settings: Settings, async_client: AsyncClient):
    # Initially no MQTT client in mock test settings, should return 404
    resp = await async_client.post("/api/v1/printers/test-a1/reconnect")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_discovery_onboarding_and_dhcp_ip_tracking(test_settings: Settings, repositories, capsys):
    from unittest.mock import MagicMock
    import asyncio
    from bambu_monitor.bambu.discovery import DiscoveredPrinter
    from bambu_monitor.bambu.client import BambuMqttClient
    from bambu_monitor.api.app import _background_ip_tracker

    initial_ip = "192.168.68.57"
    new_dhcp_ip = "192.168.68.105"
    serial = "0309DA572602482"

    # Step 1: Discover and onboard with realistic LAN IP
    with patch("bambu_monitor.cli.commands.discover_printers", new_callable=AsyncMock) as mock_disc, \
         patch("bambu_monitor.cli.commands.test_printer_connection", new_callable=AsyncMock) as mock_conn:

        mock_disc.return_value = [
            DiscoveredPrinter(serial=serial, ip=initial_ip, model="A1", name="Bambu A1")
        ]
        mock_conn.return_value = True

        await cmd_onboard(
            serial=serial,
            ip=initial_ip,
            access_code="test_code_123",
            name="Bambu A1",
            model="A1",
            settings=test_settings,
        )

    # Verify persisted in SQLite with realistic LAN IP
    printers = await repositories["printer"].list_all()
    onboarded = next((p for p in printers if p.serial_number == serial), None)
    assert onboarded is not None
    assert onboarded.host == initial_ip

    # Step 2: Simulate MQTT client running with initial IP
    mock_mqtt = MagicMock()
    client = BambuMqttClient(
        printer_id=onboarded.id,
        serial_number=serial,
        host=initial_ip,
    )
    client._client = mock_mqtt
    client._connected = True
    mqtt_clients = {onboarded.id: client}

    # Step 3: Simulate DHCP re-assignment: discovery emits new IP for same serial
    with patch("bambu_monitor.api.app.discover_printers", new_callable=AsyncMock) as mock_tracker_disc:
        mock_tracker_disc.return_value = [
            DiscoveredPrinter(serial=serial, ip=new_dhcp_ip, model="A1", name="Bambu A1")
        ]

        tracker_task = asyncio.create_task(
            _background_ip_tracker(repositories["printer"], mqtt_clients, interval_seconds=0.01)
        )
        await asyncio.sleep(0.05)
        tracker_task.cancel()
        try:
            await tracker_task
        except asyncio.CancelledError:
            pass

    # Verify MQTT client host was updated and triggered disconnect/reconnect
    assert client.host == new_dhcp_ip
    mock_mqtt.disconnect.assert_called_once()

    # Verify SQLite database was updated with the new DHCP IP
    updated_printer = await repositories["printer"].get(onboarded.id)
    assert updated_printer is not None
    assert updated_printer.host == new_dhcp_ip

    # Step 4: Verify devices CLI reports the updated IP
    await cmd_devices(settings=test_settings)
    captured = capsys.readouterr()
    assert new_dhcp_ip in captured.out


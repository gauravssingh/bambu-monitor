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

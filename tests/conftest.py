"""Shared pytest fixtures for Bambu Monitor test suite."""

import json
from pathlib import Path
from typing import Any, AsyncGenerator, Dict
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from bambu_monitor.api.app import create_app
from bambu_monitor.config import ApplicationConfig, DatabaseConfig, DeliveryConfig, EventsConfig, PrinterConfig, Settings
from bambu_monitor.domain.printer import Printer
from bambu_monitor.state.manager import StateManager
from bambu_monitor.storage.database import Database
from bambu_monitor.storage.repositories import (
    AlertRepository,
    EventRepository,
    JobRepository,
    OutboxRepository,
    PrinterRepository,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Dict[str, Any]:
    path = FIXTURES_DIR / name
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def fixtures():
    return load_fixture


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    db_path = str(tmp_path / "test_bambu.db")
    settings = Settings(
        application=ApplicationConfig(
            environment="testing",
            log_level="DEBUG",
            auto_connect_mqtt=False,
            enable_discovery=False,
        ),
        database=DatabaseConfig(
            path=db_path,
            journal_mode="WAL",
            synchronous="NORMAL",
            flush_interval_seconds=60.0,
        ),
        events=EventsConfig(
            # Hermetic: tests must never make real webhook delivery attempts.
            delivery=DeliveryConfig(enabled=False),
        ),
        printers=[
            PrinterConfig(
                id="test-a1",
                model="A1",
                host="127.0.0.1",
                serial_number="01P00TEST123456",
            ),
            PrinterConfig(
                id="test-a1-mini",
                model="A1 Mini",
                host="127.0.0.2",
                serial_number="01P00TESTMINI12",
            ),
        ],
    )
    return settings


@pytest_asyncio.fixture
async def test_db(test_settings: Settings) -> AsyncGenerator[Database, None]:
    db = Database(db_path=test_settings.database.path)
    await db.init_db()
    yield db


@pytest_asyncio.fixture
async def repositories(test_db: Database):
    p_repo = PrinterRepository(test_db)
    j_repo = JobRepository(test_db)
    a_repo = AlertRepository(test_db)
    e_repo = EventRepository(test_db)
    o_repo = OutboxRepository(test_db)
    return {
        "printer": p_repo,
        "job": j_repo,
        "alert": a_repo,
        "event": e_repo,
        "outbox": o_repo,
    }


@pytest_asyncio.fixture
async def state_manager(test_settings: Settings, repositories) -> StateManager:
    sm = StateManager(
        settings=test_settings,
        printer_repo=repositories["printer"],
        job_repo=repositories["job"],
        alert_repo=repositories["alert"],
        event_repo=repositories["event"],
        outbox_repo=repositories["outbox"],
    )
    for p_cfg in test_settings.printers:
        sm.register_printer(p_cfg.id, model=p_cfg.model)
        await repositories["printer"].save(
            Printer(
                id=p_cfg.id,
                model=p_cfg.model,
                serial_number=p_cfg.serial_number,
                host=p_cfg.host,
            )
        )
    return sm


@pytest_asyncio.fixture
async def async_client(test_settings: Settings) -> AsyncGenerator[AsyncClient, None]:
    app = create_app(test_settings)
    # Loopback base_url: ASGITransport presents a loopback client address and
    # Host header, satisfying require_api_access without any test-only
    # strings hardcoded in production auth logic.
    transport = ASGITransport(app=app, client=("127.0.0.1", 123))
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
        # Run startup lifespan
        async with app.router.lifespan_context(app):
            yield client

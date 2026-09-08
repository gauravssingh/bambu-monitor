"""Integration test verifying SQLite WAL mode and table initialization."""

import pytest
from bambu_monitor.storage.database import Database


@pytest.mark.asyncio
async def test_sqlite_wal_mode_and_tables(tmp_path):
    db_path = str(tmp_path / "wal_test.db")
    db = Database(db_path)
    await db.init_db()

    conn = await db.get_connection()
    try:
        # Check WAL mode
        async with conn.execute("PRAGMA journal_mode;") as cursor:
            row = await cursor.fetchone()
            assert row[0].lower() == "wal"

        # Check synchronous
        async with conn.execute("PRAGMA synchronous;") as cursor:
            row = await cursor.fetchone()
            # synchronous NORMAL corresponds to 1
            assert row[0] in (1, "NORMAL", "normal")

        # Check tables exist
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
        ) as cursor:
            tables = [r[0] for r in await cursor.fetchall()]
            assert "printers" in tables
            assert "print_jobs" in tables
            assert "alerts" in tables
            assert "events" in tables
            assert "outbox" in tables

        # Health check
        health = await db.check_health()
        assert health["available"] is True
        assert health["journal_mode"] == "wal"
    finally:
        await conn.close()

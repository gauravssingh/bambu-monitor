"""Integration tests for API access control (loopback + token auth)."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from bambu_monitor.api.app import create_app


@pytest.mark.asyncio
async def test_loopback_client_with_loopback_host_is_allowed(test_settings, test_db, repositories):
    app = create_app(test_settings)
    transport = ASGITransport(app=app, client=("127.0.0.1", 123))
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
        async with app.router.lifespan_context(app):
            resp = await client.get("/health")
            assert resp.status_code == 200


@pytest.mark.asyncio
async def test_remote_client_is_rejected(test_settings, test_db, repositories):
    app = create_app(test_settings)
    transport = ASGITransport(app=app, client=("203.0.113.10", 55555))
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
        async with app.router.lifespan_context(app):
            resp = await client.get("/health")
            assert resp.status_code == 403


@pytest.mark.asyncio
async def test_loopback_client_with_foreign_host_header_is_rejected(test_settings, test_db, repositories):
    """DNS rebinding: loopback socket but a remote Host header must not pass."""
    app = create_app(test_settings)
    transport = ASGITransport(app=app, client=("127.0.0.1", 123))
    async with AsyncClient(transport=transport, base_url="http://evil.example.com") as client:
        async with app.router.lifespan_context(app):
            resp = await client.get("/health")
            assert resp.status_code == 403


@pytest.mark.asyncio
async def test_api_token_authenticates_from_any_client(test_settings, test_db, repositories):
    test_settings.application.api_token = "super-secret-token"
    app = create_app(test_settings)
    transport = ASGITransport(app=app, client=("203.0.113.10", 55555))
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
        async with app.router.lifespan_context(app):
            denied = await client.get("/health")
            assert denied.status_code == 401

            denied_wrong = await client.get("/health", headers={"X-API-Key": "wrong"})
            assert denied_wrong.status_code == 401

            allowed = await client.get("/health", headers={"X-API-Key": "super-secret-token"})
            assert allowed.status_code == 200

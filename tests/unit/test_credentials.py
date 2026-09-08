"""Unit tests for secure credential management."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from bambu_monitor.bambu.credentials import (
    delete_access_code,
    get_access_code,
    get_credential_store_info,
    store_access_code,
    _get_encryption_key,
    _load_fallback_store,
    _save_fallback_store,
)


def test_encryption_key_format():
    key = _get_encryption_key()
    assert isinstance(key, bytes)
    assert len(key) == 44  # Base64 urlsafe 32-byte digest


def test_bambu_encryption_key_env_override(monkeypatch):
    monkeypatch.setenv("BAMBU_ENCRYPTION_KEY", "custom_secret_passphrase_here")
    key = _get_encryption_key()
    assert isinstance(key, bytes)
    assert len(key) == 44
    is_secure, desc = get_credential_store_info()
    assert "BAMBU_ENCRYPTION_KEY" in desc


def test_store_and_retrieve_fallback(tmp_path: Path, monkeypatch):
    test_creds_file = tmp_path / ".test_credentials"
    monkeypatch.setattr("bambu_monitor.bambu.credentials.LOCAL_CREDS_FILE", test_creds_file)
    # Ensure keyring fails so fallback is used
    def raise_err(*args, **kwargs):
        raise RuntimeError("No keyring")
    monkeypatch.setattr("bambu_monitor.bambu.credentials.keyring.set_password", raise_err)
    monkeypatch.setattr("bambu_monitor.bambu.credentials.keyring.get_password", lambda *args: None)

    serial = "01P00TEST123456"
    code = "secret_access_code_99"

    store_access_code(serial, code)
    assert test_creds_file.exists()

    retrieved = get_access_code(serial)
    assert retrieved == code


def test_env_var_override(monkeypatch):
    serial = "01P00ENVTEST12"
    env_key = f"BAMBU_ACCESS_CODE_{serial.replace('-', '_').upper()}"
    monkeypatch.setenv(env_key, "env_code_12345")

    retrieved = get_access_code(serial)
    assert retrieved == "env_code_12345"


def test_global_env_var_fallback(monkeypatch):
    serial = "01P00ANY123"
    monkeypatch.setenv("BAMBU_ACCESS_CODE", "global_code_9999")

    retrieved = get_access_code(serial)
    assert retrieved == "global_code_9999"


def test_delete_access_code(tmp_path: Path, monkeypatch):
    test_creds_file = tmp_path / ".test_credentials"
    monkeypatch.setattr("bambu_monitor.bambu.credentials.LOCAL_CREDS_FILE", test_creds_file)
    def raise_err(*args, **kwargs):
        raise RuntimeError("No keyring")
    monkeypatch.setattr("bambu_monitor.bambu.credentials.keyring.set_password", raise_err)
    monkeypatch.setattr("bambu_monitor.bambu.credentials.keyring.get_password", lambda *args: None)
    monkeypatch.setattr("bambu_monitor.bambu.credentials.keyring.delete_password", lambda *args: None)

    serial = "01P00DELTEST"
    store_access_code(serial, "to_be_deleted")
    assert get_access_code(serial) == "to_be_deleted"

    success = delete_access_code(serial)
    assert success is True
    assert get_access_code(serial) is None

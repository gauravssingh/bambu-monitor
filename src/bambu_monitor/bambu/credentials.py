"""Secure credential management for Bambu printer LAN Access Codes."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Optional
from cryptography.fernet import Fernet
import keyring
from keyring.errors import KeyringError

logger = logging.getLogger(__name__)

KEYRING_SERVICE_NAME = "bambu-monitor"
LOCAL_CREDS_FILE = Path("./data/.credentials")


def _get_machine_derived_key() -> bytes:
    """Generate a deterministic local encryption key for headless fallback."""
    seed = f"{os.uname().nodename}:{os.getuid() if hasattr(os, 'getuid') else 'win'}:bambu-monitor-salt"
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def is_keyring_available() -> bool:
    """Test whether system keyring is accessible and functional."""
    try:
        backend = keyring.get_keyring()
        # Ensure it's not the fail backend
        if "fail" in backend.__class__.__name__.lower():
            return False
        test_key = "__probe_test__"
        keyring.set_password(KEYRING_SERVICE_NAME, test_key, "ok")
        val = keyring.get_password(KEYRING_SERVICE_NAME, test_key)
        keyring.delete_password(KEYRING_SERVICE_NAME, test_key)
        return val == "ok"
    except Exception:
        return False


def _load_fallback_store() -> dict[str, str]:
    if not LOCAL_CREDS_FILE.exists():
        return {}
    try:
        fernet = Fernet(_get_machine_derived_key())
        encrypted = LOCAL_CREDS_FILE.read_bytes()
        decrypted = fernet.decrypt(encrypted)
        return json.loads(decrypted.decode("utf-8"))
    except Exception as exc:
        logger.debug("Failed reading fallback credential store: %s", exc)
        return {}


def _save_fallback_store(store: dict[str, str]) -> None:
    try:
        LOCAL_CREDS_FILE.parent.mkdir(parents=True, exist_ok=True)
        fernet = Fernet(_get_machine_derived_key())
        raw = json.dumps(store).encode("utf-8")
        encrypted = fernet.encrypt(raw)
        LOCAL_CREDS_FILE.write_bytes(encrypted)
        LOCAL_CREDS_FILE.chmod(0o600)
    except Exception as exc:
        logger.error("Failed saving fallback credential store: %s", exc)


def store_access_code(serial: str, access_code: str) -> None:
    """Securely store access code using system keyring, falling back to local encrypted file."""
    clean_serial = serial.strip()
    clean_code = access_code.strip()

    # Try OS keyring first
    try:
        keyring.set_password(KEYRING_SERVICE_NAME, clean_serial, clean_code)
        logger.debug("Stored credentials in system keyring for serial %s", clean_serial)
        return
    except (KeyringError, Exception) as exc:
        logger.debug("System keyring unavailable (%s); using encrypted local fallback", exc)

    # Fallback store
    store = _load_fallback_store()
    store[clean_serial] = clean_code
    _save_fallback_store(store)
    logger.debug("Stored credentials in encrypted fallback for serial %s", clean_serial)


def get_access_code(serial: str) -> Optional[str]:
    """Retrieve access code from environment variables, OS keyring, or encrypted fallback."""
    clean_serial = serial.strip()

    # 1. Environment variable override
    env_key = f"BAMBU_ACCESS_CODE_{clean_serial.replace('-', '_').upper()}"
    if env_key in os.environ and os.environ[env_key]:
        return os.environ[env_key].strip()

    if "BAMBU_ACCESS_CODE" in os.environ and os.environ["BAMBU_ACCESS_CODE"]:
        return os.environ["BAMBU_ACCESS_CODE"].strip()

    # 2. OS keyring
    try:
        code = keyring.get_password(KEYRING_SERVICE_NAME, clean_serial)
        if code:
            return code.strip()
    except (KeyringError, Exception):
        pass

    # 3. Encrypted fallback
    store = _load_fallback_store()
    return store.get(clean_serial)


def delete_access_code(serial: str) -> bool:
    """Delete access code from both keyring and fallback store."""
    clean_serial = serial.strip()
    deleted = False

    try:
        keyring.delete_password(KEYRING_SERVICE_NAME, clean_serial)
        deleted = True
    except Exception:
        pass

    store = _load_fallback_store()
    if clean_serial in store:
        del store[clean_serial]
        _save_fallback_store(store)
        deleted = True

    return deleted

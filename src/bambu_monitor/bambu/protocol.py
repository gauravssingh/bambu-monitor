"""Bambu Lab MQTT protocol constants, topics, and command payloads."""

from __future__ import annotations

from typing import Any, Dict

BAMBU_LAN_USERNAME = "bblp"
BAMBU_MQTT_PORT = 8883
BAMBU_DISCOVERY_PORT = 2021
SSDP_MULTICAST_ADDR = "239.255.255.250"
SSDP_PORT = 1900


def get_report_topic(serial_number: str) -> str:
    """Topic where the printer publishes telemetry reports."""
    return f"device/{serial_number}/report"


def get_request_topic(serial_number: str) -> str:
    """Topic where clients publish control and query commands."""
    return f"device/{serial_number}/request"


def create_pushall_payload(sequence_id: int = 0) -> Dict[str, Any]:
    """Payload to request an immediate full state synchronization dump."""
    return {
        "pushing": {
            "sequence_id": str(sequence_id),
            "command": "pushall",
            "version": 1,
        }
    }


def create_pause_payload(sequence_id: int = 0) -> Dict[str, Any]:
    """Payload to pause the current print job."""
    return {
        "print": {
            "sequence_id": str(sequence_id),
            "command": "pause",
        }
    }


def create_resume_payload(sequence_id: int = 0) -> Dict[str, Any]:
    """Payload to resume a paused print job."""
    return {
        "print": {
            "sequence_id": str(sequence_id),
            "command": "resume",
        }
    }


def create_stop_payload(sequence_id: int = 0) -> Dict[str, Any]:
    """Payload to stop/cancel the current print job."""
    return {
        "print": {
            "sequence_id": str(sequence_id),
            "command": "stop",
        }
    }

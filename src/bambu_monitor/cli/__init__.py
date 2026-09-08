"""CLI commands package for Bambu Monitor."""

from bambu_monitor.cli.commands import (
    cmd_credentials,
    cmd_devices,
    cmd_discover,
    cmd_doctor,
    cmd_onboard,
    cmd_remove,
    cmd_status,
)

__all__ = [
    "cmd_discover",
    "cmd_onboard",
    "cmd_devices",
    "cmd_status",
    "cmd_doctor",
    "cmd_remove",
    "cmd_credentials",
]

"""Main entry point for Bambu Monitor CLI and Daemon Service."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Optional
import uvicorn

from bambu_monitor.cli.commands import (
    cmd_credentials,
    cmd_devices,
    cmd_discover,
    cmd_doctor,
    cmd_onboard,
    cmd_reconnect,
    cmd_remove,
    cmd_status,
)
from bambu_monitor.config import load_config


def setup_logging(log_level: str = "INFO") -> None:
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bambu-monitor",
        description="Standalone local service for monitoring Bambu Lab 3D printers",
    )
    parser.add_argument(
        "-c", "--config",
        dest="config_path",
        help="Path to YAML configuration file",
        default=None,
    )
    parser.add_argument(
        "--log-level",
        dest="log_level",
        help="Logging level (DEBUG, INFO, WARNING, ERROR)",
        default=None,
    )
    parser.add_argument(
        "--host",
        help="HTTP server bind host (default: 0.0.0.0)",
        default=None,
    )
    parser.add_argument(
        "--port",
        type=int,
        help="HTTP server bind port (default: 8000)",
        default=None,
    )

    subparsers = parser.add_subparsers(dest="command")

    # 1. run (daemon mode)
    run_parser = subparsers.add_parser("run", help="Start Bambu Monitor daemon service")
    run_parser.add_argument("--host", default="0.0.0.0", help="HTTP server bind host (default: 0.0.0.0)")
    run_parser.add_argument("--port", type=int, default=8000, help="HTTP server bind port (default: 8000)")

    # 2. discover
    disc_parser = subparsers.add_parser("discover", help="Discover Bambu printers on local network")
    disc_parser.add_argument(
        "-t", "--timeout",
        type=float,
        default=12.0,
        help="Discovery timeout in seconds (default: 12.0)",
    )

    # 3. onboard
    onb_parser = subparsers.add_parser("onboard", help="Onboard a Bambu printer (interactive or automated)")
    onb_parser.add_argument("--ip", help="Printer IP address")
    onb_parser.add_argument("--serial", help="Printer serial number")
    onb_parser.add_argument("--access-code", help="Printer LAN Access Code")
    onb_parser.add_argument("--model", help="Printer model (e.g. A1, A1 Mini, X1C, P1S)")
    onb_parser.add_argument("--name", help="Friendly printer name")

    # 4. devices
    subparsers.add_parser("devices", help="List configured printers and connection statuses")

    # 5. status
    subparsers.add_parser("status", help="Show live status of service, printers, and active prints")

    # 6. doctor
    subparsers.add_parser("doctor", help="Run connectivity, credential, and database diagnostics")

    # 7. reconnect
    rec_parser = subparsers.add_parser("reconnect", help="Force MQTT reconnect and pushall for a printer")
    rec_parser.add_argument("printer_id", help="ID of printer to reconnect")

    # 8. credentials
    cred_parser = subparsers.add_parser("credentials", help="Update LAN Access Code for a printer")
    cred_parser.add_argument("printer_id", help="ID of printer")

    # 9. remove
    rem_parser = subparsers.add_parser("remove", help="Remove a printer and purge stored credentials")
    rem_parser.add_argument("printer_id", help="ID of printer to remove")

    return parser


def run_daemon(config_path: Optional[str], host: str, port: int, log_level: Optional[str]) -> None:
    settings = load_config(config_path)
    active_level = log_level or settings.application.log_level
    setup_logging(active_level)

    logger = logging.getLogger("bambu_monitor")
    logger.info("Starting Bambu Monitor on %s:%d", host, port)
    if settings.printers:
        logger.info("Configured printers: %s", [p.id for p in settings.printers])
    else:
        logger.info("No printers configured yet. Run 'bambu-monitor onboard' to connect a printer.")

    from bambu_monitor.api.app import create_app
    app = create_app(settings)
    uvicorn.run(app, host=host, port=port, log_level="info")


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # Load settings for admin commands
    settings = load_config(args.config_path)

    cmd = args.command

    if cmd is None or cmd == "run":
        bind_host = getattr(args, "host", None) or "0.0.0.0"
        bind_port = getattr(args, "port", None) or 8000
        run_daemon(args.config_path, bind_host, bind_port, args.log_level)

    elif cmd == "discover":
        asyncio.run(cmd_discover(timeout=args.timeout))

    elif cmd == "onboard":
        asyncio.run(
            cmd_onboard(
                serial=args.serial,
                ip=args.ip,
                access_code=args.access_code,
                name=args.name,
                model=args.model,
                settings=settings,
            )
        )

    elif cmd == "devices":
        asyncio.run(cmd_devices(settings=settings))

    elif cmd == "status":
        asyncio.run(cmd_status(settings=settings))

    elif cmd == "doctor":
        asyncio.run(cmd_doctor(settings=settings))

    elif cmd == "reconnect":
        asyncio.run(cmd_reconnect(printer_id=args.printer_id, settings=settings))

    elif cmd == "credentials":
        asyncio.run(cmd_credentials(printer_id=args.printer_id, settings=settings))

    elif cmd == "remove":
        asyncio.run(cmd_remove(printer_id=args.printer_id, settings=settings))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()

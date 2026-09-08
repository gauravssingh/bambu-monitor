"""Main entry point for Bambu Monitor CLI and Daemon Service."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Optional
import uvicorn

from bambu_monitor.cli.commands import (
    cmd_camera_snap,
    cmd_camera_test,
    cmd_credentials,
    cmd_devices,
    cmd_discover,
    cmd_doctor,
    cmd_onboard,
    cmd_reconnect,
    cmd_remove,
    cmd_service_logs,
    cmd_service_restart,
    cmd_service_start,
    cmd_service_status,
    cmd_service_stop,
    cmd_status,
    cmd_timelapse_camera_test,
    cmd_timelapse_correlate,
    cmd_timelapse_generate,
    cmd_timelapse_list,
    cmd_timelapse_status,
)
from bambu_monitor.config import load_config


def setup_logging(log_level: str = "INFO") -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass
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
        help="HTTP server bind host (default: 127.0.0.1)",
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
    run_parser.add_argument("--host", default="127.0.0.1", help="HTTP server bind host (default: 127.0.0.1)")
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

    # 10. service
    svc_parser = subparsers.add_parser("service", help="Manage background daemon service (start, stop, restart, status, logs)")
    svc_sub = svc_parser.add_subparsers(dest="service_action")

    svc_start = svc_sub.add_parser("start", help="Start Bambu Monitor daemon in background")
    svc_start.add_argument("--host", default="127.0.0.1", help="HTTP server bind host")
    svc_start.add_argument("--port", type=int, default=8000, help="HTTP server bind port")

    svc_sub.add_parser("stop", help="Stop running background daemon")

    svc_restart = svc_sub.add_parser("restart", help="Restart background daemon")
    svc_restart.add_argument("--host", default="127.0.0.1", help="HTTP server bind host")
    svc_restart.add_argument("--port", type=int, default=8000, help="HTTP server bind port")

    svc_status = svc_sub.add_parser("status", help="Check status of background service")
    svc_status.add_argument("--port", type=int, default=8000, help="HTTP server bind port")

    svc_logs = svc_sub.add_parser("logs", help="View background daemon logs")
    svc_logs.add_argument("-n", "--lines", type=int, default=50, help="Number of lines to show")

    # 11. camera
    cam_parser = subparsers.add_parser("camera", help="Manage and test RTSP camera streams")
    cam_sub = cam_parser.add_subparsers(dest="camera_action")

    cam_test = cam_sub.add_parser("test", help="Test RTSP camera connection and probe stream diagnostics")
    cam_test.add_argument("printer_id", help="ID of printer to test camera for")

    cam_snap = cam_sub.add_parser("snap", help="Capture a single JPEG snapshot from RTSP camera")
    cam_snap.add_argument("printer_id", help="ID of printer")
    cam_snap.add_argument("-o", "--output", help="Output file path for captured JPEG", default=None)

    # 12. timelapse
    tl_parser = subparsers.add_parser("timelapse", help="Manage and monitor print timelapse subsystem")
    tl_sub = tl_parser.add_subparsers(dest="timelapse_action")

    tl_status = tl_sub.add_parser("status", help="Show active timelapse status across printers")
    tl_status.add_argument("printer_id", nargs="?", default=None, help="Optional printer ID")

    tl_list = tl_sub.add_parser("list", help="List recorded timelapse sessions")
    tl_list.add_argument("printer_id", nargs="?", default=None, help="Optional printer ID filter")
    tl_list.add_argument("-n", "--limit", type=int, default=20, help="Maximum sessions to list")

    tl_test = tl_sub.add_parser("camera-test", help="Verify RTSP camera stream and capture a diagnostic snapshot")
    tl_test.add_argument("printer_id", nargs="?", default=None, help="Optional printer ID")
    tl_test.add_argument("--printer", dest="printer_flag", default=None, help="Optional printer ID")

    tl_gen = tl_sub.add_parser("generate", help="Generate or re-compile MP4 video from existing frames")
    tl_gen.add_argument("job_id", help="Print Job ID or Timelapse Session ID")

    tl_corr = tl_sub.add_parser("correlate", help="Correlate print telemetry (temperatures, speed, layers) with visual frames")
    tl_corr.add_argument("printer_id", help="Printer ID")
    tl_corr.add_argument("timelapse_id", nargs="?", default=None, help="Optional Timelapse Session ID or Print Job ID (defaults to latest)")
    tl_corr.add_argument("--temp-drop-threshold", type=float, default=10.0, help="Temperature drop threshold in °C below target (default: 10.0)")

    # Top-level camera-test alias
    cam_test_top = subparsers.add_parser("camera-test", help="Verify RTSP camera stream and capture a diagnostic snapshot")
    cam_test_top.add_argument("printer_id", nargs="?", default=None, help="Optional printer ID")
    cam_test_top.add_argument("--printer", dest="printer_flag", default=None, help="Optional printer ID")

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
        bind_host = getattr(args, "host", None) or "127.0.0.1"
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

    elif cmd == "service":
        action = getattr(args, "service_action", None)
        if action == "start":
            asyncio.run(cmd_service_start(host=args.host, port=args.port, config_path=args.config_path))
        elif action == "stop":
            asyncio.run(cmd_service_stop())
        elif action == "restart":
            asyncio.run(cmd_service_restart(host=args.host, port=args.port, config_path=args.config_path))
        elif action == "status":
            asyncio.run(cmd_service_status(port=args.port))
        elif action == "logs":
            cmd_service_logs(lines=args.lines)
        else:
            parser.parse_args(["service", "--help"])

    elif cmd == "camera":
        action = getattr(args, "camera_action", None)
        if action == "test":
            asyncio.run(cmd_camera_test(printer_id=args.printer_id, settings=settings))
        elif action == "snap":
            asyncio.run(cmd_camera_snap(printer_id=args.printer_id, output=args.output, settings=settings))
        else:
            parser.parse_args(["camera", "--help"])

    elif cmd == "timelapse":
        action = getattr(args, "timelapse_action", None)
        if action == "status":
            asyncio.run(cmd_timelapse_status(printer_id=args.printer_id, settings=settings))
        elif action == "list":
            asyncio.run(cmd_timelapse_list(printer_id=args.printer_id, limit=args.limit, settings=settings))
        elif action == "camera-test":
            target_p = getattr(args, "printer_flag", None) or args.printer_id
            asyncio.run(cmd_timelapse_camera_test(printer_id=target_p, settings=settings))
        elif action == "generate":
            asyncio.run(cmd_timelapse_generate(job_or_session_id=args.job_id, settings=settings))
        elif action == "correlate":
            asyncio.run(
                cmd_timelapse_correlate(
                    printer_id=args.printer_id,
                    timelapse_id=args.timelapse_id,
                    temp_drop_threshold=args.temp_drop_threshold,
                    settings=settings,
                )
            )
        else:
            parser.parse_args(["timelapse", "--help"])

    elif cmd == "camera-test":
        target_p = getattr(args, "printer_flag", None) or args.printer_id
        asyncio.run(cmd_timelapse_camera_test(printer_id=target_p, settings=settings))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()

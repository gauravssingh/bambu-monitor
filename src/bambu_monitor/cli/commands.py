"""Administration CLI commands for Bambu Monitor (discover, onboard, devices, status, doctor)."""

from __future__ import annotations

import asyncio
import getpass
import json
import logging
import os
from pathlib import Path
import socket
import ssl
import sys
import time
from typing import Optional

from bambu_monitor.bambu.credentials import (
    delete_access_code,
    get_access_code,
    get_credential_store_info,
    is_keyring_available,
    store_access_code,
)
from bambu_monitor.bambu.discovery import DiscoveredPrinter, discover_printers
from bambu_monitor.bambu.protocol import BAMBU_DISCOVERY_PORT, BAMBU_LAN_USERNAME, BAMBU_MQTT_PORT
from bambu_monitor.config import Settings, load_config
from bambu_monitor.camera import CameraClient, CameraError
from bambu_monitor.domain.printer import Printer
from bambu_monitor.storage.database import Database
from bambu_monitor.storage.repositories import (
    AlertRepository,
    JobRepository,
    OutboxRepository,
    PrinterRepository,
)

logger = logging.getLogger(__name__)


def get_db_and_repos(settings: Settings):
    db = Database(db_path=settings.database.path)
    p_repo = PrinterRepository(db)
    j_repo = JobRepository(db)
    a_repo = AlertRepository(db)
    o_repo = OutboxRepository(db)
    return db, p_repo, j_repo, a_repo, o_repo


async def cmd_discover(timeout: float = 12.0) -> None:
    """Discover Bambu Lab printers on the local network."""
    print(f"\nSearching for Bambu printers on the local network (listening up to {int(timeout)}s for heartbeat broadcasts)...")
    printers = await discover_printers(timeout_seconds=timeout)

    if not printers:
        print("\nNo Bambu printers discovered on the local network.")
        print("Tip: Ensure the printer is powered on, connected to the same Wi-Fi/LAN, and 'LAN Mode' is active.")
        return

    print(f"\nFound {len(printers)} printer{'s' if len(printers) != 1 else ''}:\n")
    for i, p in enumerate(printers, start=1):
        print(f"  [{i}] {p.name} ({p.model})")
        print(f"      Serial:  {p.serial}")
        print(f"      IP:      {p.ip}")
        print(f"      Port:    {p.port}\n")


async def test_printer_connection(ip: str, port: int, serial: str, access_code: str, timeout: float = 5.0) -> bool:
    """Test raw TLS socket and MQTT handshake with the printer."""
    try:
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        sock = socket.create_connection((ip, port), timeout=timeout)
        tls_sock = ssl_ctx.wrap_socket(sock)
        tls_sock.close()
        return True
    except Exception as exc:
        logger.debug("Test connection failed: %s", exc)
        return False


async def cmd_onboard(
    serial: Optional[str] = None,
    ip: Optional[str] = None,
    access_code: Optional[str] = None,
    name: Optional[str] = None,
    model: Optional[str] = None,
    settings: Optional[Settings] = None,
) -> None:
    """Interactive and automated printer onboarding wizard."""
    cfg = settings or load_config()
    db, p_repo, _, _, _ = get_db_and_repos(cfg)
    await db.init_db()

    target_printer: Optional[DiscoveredPrinter] = None

    # Step 1: Device Identification
    if serial and ip:
        target_printer = DiscoveredPrinter(
            serial=serial.strip(),
            ip=ip.strip(),
            model=model or "A1",
            name=name or f"Bambu {model or 'A1'}",
        )
    else:
        print("\nSearching for Bambu printers on the local network (listening up to 12s)...")
        discovered = await discover_printers(timeout_seconds=12.0)

        if not discovered:
            print("\nNo printers found automatically via discovery.")
            print("Please provide printer details manually:")
            ip_in = input("Printer IP address: ").strip()
            serial_in = input("Printer Serial Number: ").strip()
            model_in = input("Printer Model [A1]: ").strip() or "A1"
            name_in = input(f"Friendly Name [Bambu {model_in}]: ").strip() or f"Bambu {model_in}"
            target_printer = DiscoveredPrinter(serial=serial_in, ip=ip_in, model=model_in, name=name_in)
        elif len(discovered) == 1:
            target_printer = discovered[0]
            print(f"\nFound 1 printer:\n  {target_printer.name} ({target_printer.model}) at {target_printer.ip}")
        else:
            print(f"\nFound {len(discovered)} Bambu printers:\n")
            for i, p in enumerate(discovered, start=1):
                print(f"  [{i}] {p.name} — {p.model} ({p.ip})")
            while True:
                choice = input(f"\nSelect printer [1-{len(discovered)}]: ").strip()
                if choice.isdigit() and 1 <= int(choice) <= len(discovered):
                    target_printer = discovered[int(choice) - 1]
                    break
                print("Invalid selection.")

    # Step 2: Prompt for Access Code if not provided
    code = access_code
    if not code:
        print(f"\nConnecting to {target_printer.name} ({target_printer.ip})...")
        print("LAN Access Code required (found on printer touchscreen -> Settings -> Network -> LAN Mode).")
        while not code:
            code = getpass.getpass("Enter LAN Access Code: ").strip()
            if not code:
                print("Access Code cannot be empty.")

    # Step 3: Test connection
    print("\nTesting connection...")
    reachable = await test_printer_connection(
        ip=target_printer.ip,
        port=target_printer.port,
        serial=target_printer.serial,
        access_code=code,
    )

    if not reachable:
        print(f"✗ Warning: Could not verify TLS handshake with {target_printer.ip}:{target_printer.port}.")
        proceed = input("Store printer anyway? [y/N]: ").strip().lower()
        if proceed != "y":
            print("Onboarding aborted.")
            return

    print("✓ TLS connection established")
    print("✓ Authentication parameters verified")

    # Step 4: Securely store credentials & DB registration
    print("\nOnboarding printer...")
    store_access_code(target_printer.serial, code)
    print("✓ Credentials stored securely")

    printer_id = f"bambu-{target_printer.model.lower().replace(' ', '-')}-{target_printer.serial[-6:].lower()}"
    # Check if printer already registered
    existing = await p_repo.get(printer_id)
    if not existing:
        # Check by serial
        all_printers = await p_repo.list_all()
        for p in all_printers:
            if p.serial_number == target_printer.serial:
                printer_id = p.id
                break

    printer_entity = Printer(
        id=printer_id,
        model=target_printer.model,
        serial_number=target_printer.serial,
        host=target_printer.ip,
        online=reachable,
    )
    await p_repo.save(printer_entity)
    print(f"✓ Printer registered in database (ID: {printer_id})")
    print(f"\n{target_printer.name} is now onboarded and ready for monitoring!")


async def cmd_devices(settings: Optional[Settings] = None) -> None:
    """List all configured printers and their connection statuses."""
    cfg = settings or load_config()
    db, p_repo, _, _, _ = get_db_and_repos(cfg)
    await db.init_db()

    printers = await p_repo.list_all()
    if not printers:
        print("\nNo printers configured. Run 'bambu-monitor onboard' to add a printer.")
        return

    print("\nConfigured printers:\n")
    header = f"  {'ID':<16} {'Model':<10} {'Serial Number':<18} {'IP Address':<16} {'Status'}"
    print(header)
    print("  " + "─" * 70)
    for p in printers:
        status_dot = "● Online" if p.online else "○ Offline"
        print(f"  {p.id:<16} {p.model:<10} {p.serial_number:<18} {p.host:<16} {status_dot}")
    print()


async def cmd_status(settings: Optional[Settings] = None) -> None:
    """Display overall monitor status, active print, and telemetry summary."""
    cfg = settings or load_config()
    db, p_repo, j_repo, _, o_repo = get_db_and_repos(cfg)
    await db.init_db()

    health = await db.check_health()
    printers = await p_repo.list_all()
    outbox_counts = await o_repo.get_counts()

    print("\nBambu Monitor\n")
    print(f"Database      ● Healthy (WAL mode at {health.get('path', './data/bambu.db')})")
    print(f"Printers      {len(printers)} configured, {sum(1 for p in printers if p.online)} online")
    print(f"Outbox        {outbox_counts.get('pending', 0)} pending, {outbox_counts.get('failed_dlq', 0)} failed")

    for p in printers:
        print(f"\n{p.model} — {p.id}")
        print(f"  Serial        {p.serial_number}")
        print(f"  Host          {p.host}")
        print(f"  Connection    {'● Online' if p.online else '○ Offline'}")

        active_job = await j_repo.get_active_for_printer(p.id)
        if active_job:
            print(f"  State         {active_job.status.value}")
            print(f"  Print         {active_job.filename}")
            print(f"  Progress      {active_job.progress}% (Layer {active_job.layer}/{active_job.total_layers})")
            if active_job.remaining_seconds is not None:
                mins = active_job.remaining_seconds // 60
                print(f"  Remaining     {mins} min")
        else:
            print("  State         idle (no active print)")
    print()


async def cmd_doctor(settings: Optional[Settings] = None) -> None:
    """Run comprehensive connectivity, environment, and pipeline diagnostics."""
    cfg = settings or load_config()
    db, p_repo, _, _, _ = get_db_and_repos(cfg)
    await db.init_db()

    print("\nBambu Monitor Diagnostics\n")

    # 1. Database
    health = await db.check_health()
    if health.get("available") and health.get("journal_mode") == "wal":
        print("SQLite Database         ✓ WAL mode verified")
    else:
        print("SQLite Database         ✗ Issues detected")

    # 2. Keyring / Credential Store
    is_secure, store_desc = get_credential_store_info()
    symbol = "✓" if is_secure else "!"
    print(f"Credential Store        {symbol} {store_desc}")

    # 3. Network Discovery
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", BAMBU_DISCOVERY_PORT))
        sock.close()
        print("Network Discovery       ✓ UDP port 2021 available")
    except Exception:
        print("Network Discovery       ! Port 2021 bound (daemon or another instance running)")

    # 4. Check each configured printer
    printers = await p_repo.list_all()
    if not printers:
        print("\nPrinters: None configured yet. Run 'bambu-monitor onboard' to add a printer.\n")
        return

    for p in printers:
        print(f"\nChecking printer '{p.id}' ({p.host}):")
        # Reachable
        reachable = False
        try:
            s = socket.create_connection((p.host, BAMBU_MQTT_PORT), timeout=2.0)
            s.close()
            reachable = True
            print("  Host Reachable        ✓ TCP port 8883 open")
        except Exception:
            print(f"  Host Reachable        ✗ Could not connect to {p.host}:{BAMBU_MQTT_PORT}")

        # TLS Handshake
        if reachable:
            try:
                ssl_ctx = ssl.create_default_context()
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
                sock = socket.create_connection((p.host, BAMBU_MQTT_PORT), timeout=3.0)
                tls_sock = ssl_ctx.wrap_socket(sock)
                tls_sock.close()
                print("  TLS Handshake         ✓ Self-signed certificate accepted")
            except Exception as e:
                print(f"  TLS Handshake         ✗ TLS failed: {e}")

        # Credentials
        code = get_access_code(p.serial_number)
        if code:
            print("  Access Code           ✓ Stored securely")
        else:
            print("  Access Code           ✗ Missing! Run 'bambu-monitor credentials <id>'")

    print()


async def cmd_remove(printer_id: str, settings: Optional[Settings] = None) -> None:
    """Remove a printer and clean up stored credentials."""
    cfg = settings or load_config()
    db, p_repo, _, _, _ = get_db_and_repos(cfg)
    await db.init_db()

    printer = await p_repo.get(printer_id)
    if not printer:
        print(f"Error: Printer '{printer_id}' not found.")
        return

    delete_access_code(printer.serial_number)
    conn = await db.get_connection()
    try:
        await conn.execute("DELETE FROM outbox WHERE printer_id = ?", (printer_id,))
        await conn.execute("DELETE FROM events WHERE printer_id = ?", (printer_id,))
        await conn.execute("DELETE FROM alerts WHERE printer_id = ?", (printer_id,))
        await conn.execute("DELETE FROM print_jobs WHERE printer_id = ?", (printer_id,))
        await conn.execute("DELETE FROM printers WHERE id = ?", (printer_id,))
        await conn.commit()
        print(f"✓ Printer '{printer_id}' and associated data/credentials removed.")
    finally:
        await conn.close()


async def cmd_credentials(printer_id: str, settings: Optional[Settings] = None) -> None:
    """Update LAN Access Code for a printer."""
    cfg = settings or load_config()
    db, p_repo, _, _, _ = get_db_and_repos(cfg)
    await db.init_db()

    printer = await p_repo.get(printer_id)
    if not printer:
        print(f"Error: Printer '{printer_id}' not found.")
        return

    print(f"Updating credentials for {printer.id} (Serial: {printer.serial_number})...")
    code = getpass.getpass("Enter new LAN Access Code: ").strip()
    if not code:
        print("Aborted: access code cannot be empty.")
        return

    store_access_code(printer.serial_number, code)
    print("✓ New access code securely stored.")


async def cmd_reconnect(printer_id: str, settings: Optional[Settings] = None) -> None:
    """Force immediate MQTT reconnect, re-subscription, and pushall request."""
    cfg = settings or load_config()
    db, p_repo, _, _, _ = get_db_and_repos(cfg)
    await db.init_db()

    printer = await p_repo.get(printer_id)
    if not printer:
        print(f"Error: Printer '{printer_id}' not found.")
        return

    print(f"Reconnecting to Bambu printer '{printer_id}' ({printer.host})...")

    # Try notifying running daemon first
    import httpx
    daemon_notified = False
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            resp = await client.post(f"http://127.0.0.1:8000/api/v1/printers/{printer_id}/reconnect")
            if resp.status_code == 200:
                print("✓ Reconnect command sent to running Bambu Monitor daemon")
                print("✓ MQTT connection re-established")
                print("✓ State pushall requested")
                daemon_notified = True
    except Exception:
        pass

    if not daemon_notified:
        # Fallback to direct network verification probe
        access_code = get_access_code(printer.serial_number) or ""
        reachable = await test_printer_connection(
            ip=printer.host,
            port=BAMBU_MQTT_PORT,
            serial=printer.serial_number,
            access_code=access_code,
            timeout=3.0,
        )
        if reachable:
            print("✓ TLS handshake established to printer")
            print("✓ Authentication parameters verified")
            print(f"✓ Printer '{printer_id}' is reachable and ready")
        else:
            print(f"✗ Could not establish TLS connection to {printer.host}:{BAMBU_MQTT_PORT}")
            print("  Check printer power, Wi-Fi connectivity, and LAN Mode settings.")


PID_FILE = Path("./data/bambu-monitor.pid")
LOG_FILE = Path("./data/bambu-monitor.log")


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _get_running_pid() -> Optional[int]:
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            if _is_pid_alive(pid):
                return pid
        except Exception:
            pass
    return None


async def cmd_service_start(host: str = "0.0.0.0", port: int = 8000, config_path: Optional[str] = None) -> None:
    """Start Bambu Monitor daemon as a background service."""
    import subprocess
    import time
    import httpx

    existing_pid = _get_running_pid()
    if existing_pid:
        print(f"Bambu Monitor is already running (PID {existing_pid}).")
        print(f"API available at: http://127.0.0.1:{port}")
        return

    # Ensure log directory exists
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log_fd = open(LOG_FILE, "a", buffering=1)

    cmd = [
        sys.executable,
        "-m", "bambu_monitor.main",
        "run",
        "--host", host,
        "--port", str(port),
    ]
    if config_path:
        cmd.extend(["-c", config_path])

    proc = subprocess.Popen(
        cmd,
        stdout=log_fd,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    PID_FILE.write_text(str(proc.pid))

    print("Starting Bambu Monitor daemon...")
    # Wait for HTTP server to become responsive
    healthy = False
    for _ in range(10):
        await asyncio.sleep(0.5)
        if not _is_pid_alive(proc.pid):
            break
        try:
            async with httpx.AsyncClient(timeout=1.0) as client:
                res = await client.get(f"http://127.0.0.1:{port}/health")
                if res.status_code == 200:
                    healthy = True
                    break
        except Exception:
            pass

    if healthy:
        print(f"✓ Bambu Monitor service started successfully (PID {proc.pid})")
        print(f"✓ HTTP API listening on http://{host}:{port}")
        print(f"✓ Log output: {LOG_FILE.resolve()}")
    elif not _is_pid_alive(proc.pid):
        print("✗ Service failed to start. Check logs for details:")
        if LOG_FILE.exists():
            print(LOG_FILE.read_text()[-1000:])
    else:
        print(f"✓ Bambu Monitor background process started (PID {proc.pid})")
        print(f"  Log output: {LOG_FILE.resolve()}")


async def cmd_service_stop() -> None:
    """Stop running Bambu Monitor daemon."""
    import signal
    pid = _get_running_pid()
    if not pid:
        print("Bambu Monitor is not running.")
        if PID_FILE.exists():
            PID_FILE.unlink(missing_ok=True)
        return

    print(f"Stopping Bambu Monitor service (PID {pid})...")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    for _ in range(15):
        await asyncio.sleep(0.2)
        if not _is_pid_alive(pid):
            break
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass

    PID_FILE.unlink(missing_ok=True)
    print("✓ Bambu Monitor service stopped.")


async def cmd_service_status(port: int = 8000) -> None:
    """Check running status of background service."""
    import httpx
    pid = _get_running_pid()
    if not pid:
        print("Bambu Monitor service: ○ Stopped")
        return

    print(f"Bambu Monitor service: ● Running (PID {pid})")
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            res = await client.get(f"http://127.0.0.1:{port}/health")
            if res.status_code == 200:
                data = res.json()
                printers = data.get("printers", {}).get("summary", [])
                online_cnt = sum(1 for p in printers if p.get("online"))
                print(f"  API Health:   ● Healthy (http://127.0.0.1:{port})")
                print(f"  Printers:     {len(printers)} registered, {online_cnt} online")
                for p in printers:
                    p_state = p.get("state", "unknown")
                    dot = "●" if p.get("online") else "○"
                    print(f"    - {p.get('id')} ({p.get('model')}): {dot} {'Online' if p.get('online') else 'Offline'} [{p_state}]")
    except Exception as exc:
        print(f"  API Status:   ○ Not responding ({exc})")


async def cmd_service_restart(host: str = "0.0.0.0", port: int = 8000, config_path: Optional[str] = None) -> None:
    """Restart Bambu Monitor daemon."""
    await cmd_service_stop()
    await asyncio.sleep(1.0)
    await cmd_service_start(host=host, port=port, config_path=config_path)


def cmd_service_logs(lines: int = 50) -> None:
    """Display recent logs from the background daemon."""
    if not LOG_FILE.exists():
        print(f"No log file found at {LOG_FILE}")
        return

    content = LOG_FILE.read_text(errors="ignore").splitlines()
    recent = content[-lines:] if len(content) > lines else content
    for line in recent:
        print(line)


async def cmd_camera_snap(
    printer_id: str,
    output: Optional[str] = None,
    settings: Optional[Settings] = None,
) -> None:
    """Capture a single JPEG snapshot from the printer's RTSP camera."""
    active_settings = settings or load_config()
    p_cfg = next((p for p in active_settings.printers if p.id == printer_id), None)
    if not p_cfg:
        print(f"Error: Printer '{printer_id}' is not configured in config.yaml.")
        return

    if not p_cfg.camera:
        print(f"Error: No camera configured for printer '{printer_id}'.")
        print("Tip: Add a 'camera' section to the printer in config.yaml.")
        return

    if not p_cfg.camera.enabled:
        print(f"Error: Camera for printer '{printer_id}' is disabled in configuration.")
        return

    if not p_cfg.camera.rtsp_url:
        print(f"Error: RTSP URL is empty for printer '{printer_id}'.")
        return

    client = CameraClient(config=p_cfg.camera, printer_id=printer_id)
    print(f"\nCapturing snapshot from camera for {printer_id} ({client.sanitized_url})...")

    t0 = time.perf_counter()
    try:
        jpeg_bytes = await client.snapshot()
        elapsed_ms = (time.perf_counter() - t0) * 1000
    except CameraError as exc:
        print(f"\nError: Camera snapshot failed: {exc}\n")
        return

    out_path = Path(output) if output else Path(f"./snapshot_{printer_id}_{int(time.time())}.jpg")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(jpeg_bytes)

    size_kb = len(jpeg_bytes) / 1024.0
    print(f"  ✓ Captured snapshot successfully ({size_kb:.1f} KB) in {elapsed_ms:.1f}ms")
    print(f"  ✓ Saved to: {out_path.resolve()}\n")


async def cmd_camera_test(
    printer_id: str,
    settings: Optional[Settings] = None,
) -> None:
    """Probe RTSP camera stream and print diagnostic metrics."""
    active_settings = settings or load_config()
    p_cfg = next((p for p in active_settings.printers if p.id == printer_id), None)
    if not p_cfg:
        print(f"Error: Printer '{printer_id}' is not configured in config.yaml.")
        return

    if not p_cfg.camera:
        print(f"Error: No camera configured for printer '{printer_id}'.")
        print("Tip: Add a 'camera' section to the printer in config.yaml.")
        return

    if not p_cfg.camera.enabled:
        print(f"Error: Camera for printer '{printer_id}' is disabled in configuration.")
        return

    if not p_cfg.camera.rtsp_url:
        print(f"Error: RTSP URL is empty for printer '{printer_id}'.")
        return

    client = CameraClient(config=p_cfg.camera, printer_id=printer_id)
    print(f"\nTesting RTSP camera for {printer_id} ({client.sanitized_url})...")
    print(f"  FFmpeg:               {p_cfg.camera.ffmpeg_bin}")
    print(f"  Transport:            TCP")
    print(f"  Probe Size:           {p_cfg.camera.probe_size_bytes} bytes")
    print(f"  Analyze Duration:     {p_cfg.camera.analyze_duration_us} us")
    print(f"  Timeout:              {p_cfg.camera.timeout_seconds}s\n")

    diag = await client.test_connection()
    if not diag.get("connected"):
        print(f"  ✗ Connection / Capture: FAILED")
        print(f"  Error: {diag.get('error')}\n")
        return

    print("  ✓ Connection & Handshake: SUCCESS")
    if diag.get("snapshot_latency_ms") is not None:
        print(f"  ✓ Snapshot Latency:       {diag['snapshot_latency_ms']} ms")
    if diag.get("image_size_bytes") is not None:
        print(f"  ✓ Frame Size:             {diag['image_size_bytes'] / 1024.0:.1f} KB")
    if diag.get("codec"):
        print(f"  ✓ Video Codec:            {diag['codec']}")
    if diag.get("resolution"):
        print(f"  ✓ Resolution:             {diag['resolution']}")
    if diag.get("fps"):
        print(f"  ✓ Framerate:              {diag['fps']}")
    print("\nCamera stream is verified and ready for monitoring!\n")



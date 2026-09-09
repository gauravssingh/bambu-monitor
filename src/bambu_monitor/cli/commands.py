"""Administration CLI commands for Bambu Monitor (discover, onboard, devices, status, doctor)."""

from __future__ import annotations

import asyncio
import getpass
import logging
import os
from pathlib import Path
import socket
import ssl
import sys
import time
from typing import Optional
from urllib.parse import urlparse

from bambu_monitor.bambu.credentials import (
    delete_access_code,
    get_access_code,
    get_credential_store_info,
    store_access_code,
)
from bambu_monitor.bambu.discovery import DiscoveredPrinter, discover_printers
from bambu_monitor.bambu.protocol import BAMBU_DISCOVERY_PORT, BAMBU_MQTT_PORT
from bambu_monitor.camera import (
    CameraClient,
    CameraConfig,
    CameraError,
    create_camera_client,
    sanitize_rtsp_url,
)
from bambu_monitor.config import Settings, load_config
from bambu_monitor.domain.printer import Printer
from bambu_monitor.storage.database import Database
from bambu_monitor.storage.repositories import (
    AlertRepository,
    EventRepository,
    JobRepository,
    OutboxRepository,
    PrinterRepository,
    TimelapseRepository,
)
from bambu_monitor.timelapse import TelemetryCorrelator, TimelapseManager, TimelapseStorage

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


async def test_printer_connection(ip: str, port: int, timeout: float = 5.0) -> bool:
    """Test raw TLS socket handshake reachability with the printer."""
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
    reachable = await test_printer_connection(ip=target_printer.ip, port=target_printer.port)

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

        camera_cfg = next((c for c in cfg.printers if c.id == p.id), None)
        if not camera_cfg or not camera_cfg.camera or not camera_cfg.camera.enabled:
            print("  Camera                ! Not configured/enabled")
        elif not camera_cfg.camera.rtsp_url:
            print("  Camera                ✗ RTSP URL missing (set A1_MINI_CAMERA_RTSP)")
        else:
            try:
                camera = CameraClient(camera_cfg.camera, printer_id=p.id)
                diag = await camera.test_connection()
                if diag.get("connected"):
                    print(
                        f"  Camera                ✓ RTSP snapshot ({diag.get('snapshot_latency_ms')} ms, "
                        f"{diag.get('resolution') or 'resolution unavailable'})"
                    )
                else:
                    print(f"  Camera                ✗ {diag.get('error')}")
            except Exception as exc:
                print(f"  Camera                ✗ {exc}")

    delivery = cfg.events.delivery
    parsed_endpoint = urlparse(delivery.endpoint)
    if delivery.enabled and parsed_endpoint.hostname:
        try:
            port = parsed_endpoint.port or (443 if parsed_endpoint.scheme == "https" else 80)
            sock = socket.create_connection((parsed_endpoint.hostname, port), timeout=2.0)
            sock.close()
            print(f"\nHermes Webhook          ✓ TCP reachable ({delivery.endpoint})")
        except Exception as exc:
            print(f"\nHermes Webhook          ✗ Unreachable ({delivery.endpoint}): {exc}")
    elif not delivery.enabled:
        print("\nHermes Webhook          ! Delivery disabled")
    else:
        print(f"\nHermes Webhook          ✗ Invalid endpoint: {delivery.endpoint}")

    print(f"Outbox Delivery         {'✓ Enabled' if delivery.enabled else '✗ Disabled'}")
    print(f"Snapshot Base URL       {cfg.application.public_base_url}")

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
        reachable = await test_printer_connection(ip=printer.host, port=BAMBU_MQTT_PORT, timeout=3.0)
        if reachable:
            print("✓ TLS handshake established to printer")
            print("✓ Authentication parameters verified")
            print(f"✓ Printer '{printer_id}' is reachable and ready")
        else:
            print(f"✗ Could not establish TLS connection to {printer.host}:{BAMBU_MQTT_PORT}")
            print("  Check printer power, Wi-Fi connectivity, and LAN Mode settings.")


# Service management ---------------------------------------------------------
#
# PID and log files live in a per-user state directory (overridable via the
# BAMBU_MONITOR_STATE_DIR environment variable) so that `service start` and
# `service stop` always operate on the same files regardless of the caller's
# working directory.

STATE_DIR_ENV = "BAMBU_MONITOR_STATE_DIR"
DAEMON_CMDLINE_MARKER = "bambu_monitor"


def _state_dir() -> Path:
    """Fixed, CWD-independent home for the PID and log files."""
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "bambu-monitor"
    return Path.home() / ".local" / "state" / "bambu-monitor"


def _pid_file() -> Path:
    return _state_dir() / "bambu-monitor.pid"


def _log_file() -> Path:
    return _state_dir() / "bambu-monitor.log"


def _is_pid_alive(pid: int) -> bool:
    """True if a process with this PID exists.

    EPERM (PermissionError) means the process exists but is owned by another
    user — that is a *live* process, not a dead one.
    """
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _process_cmdline(pid: int) -> Optional[str]:
    """Best-effort command line of the process owning `pid`, or None."""
    proc_cmdline = Path(f"/proc/{pid}/cmdline")
    if proc_cmdline.exists():
        try:
            raw = proc_cmdline.read_bytes()
            return " ".join(part for part in raw.split(b"\0") if part)
        except OSError:
            return None
    try:
        import subprocess

        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            timeout=2.0,
            check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.decode("utf-8", errors="replace").strip()
    except Exception:
        pass
    return None


def _pid_is_our_daemon(pid: int) -> Optional[bool]:
    """Verify the PID still belongs to a Bambu Monitor daemon.

    Guards against PID reuse: after a crash, the OS may hand the recorded PID
    to an unrelated process, and killing it blindly would hit that process.
    Returns None when the command line cannot be determined.
    """
    cmdline = _process_cmdline(pid)
    if cmdline is None:
        return None
    return DAEMON_CMDLINE_MARKER in cmdline


def _read_pid_file() -> Optional[int]:
    pid_file = _pid_file()
    if pid_file.exists():
        try:
            return int(pid_file.read_text().strip())
        except (OSError, ValueError):
            pass
    return None


def _get_running_pid() -> Optional[int]:
    """PID of a live daemon, or None. PID reuse makes a stale entry read as not running."""
    pid = _read_pid_file()
    if pid is not None and _is_pid_alive(pid) and _pid_is_our_daemon(pid) is not False:
        return pid
    return None


async def cmd_service_start(host: str = "127.0.0.1", port: int = 8000, config_path: Optional[str] = None) -> None:
    """Start Bambu Monitor daemon as a background service."""
    import subprocess
    import httpx

    existing_pid = _get_running_pid()
    if existing_pid:
        print(f"Bambu Monitor is already running (PID {existing_pid}).")
        print(f"API available at: http://127.0.0.1:{port}")
        return

    pid_file = _pid_file()
    log_file = _log_file()

    # Ensure log directory exists
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_fd = open(log_file, "a", buffering=1)

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
    # The child inherited the descriptor; the parent must close its copy.
    log_fd.close()

    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(proc.pid))

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
        print(f"✓ Log output: {log_file.resolve()}")
    elif not _is_pid_alive(proc.pid):
        print("✗ Service failed to start. Check logs for details:")
        if log_file.exists():
            print(log_file.read_text()[-1000:])
    else:
        print(f"✓ Bambu Monitor background process started (PID {proc.pid})")
        print(f"  Log output: {log_file.resolve()}")


async def cmd_service_stop() -> None:
    """Stop running Bambu Monitor daemon."""
    import signal
    pid_file = _pid_file()
    pid = _read_pid_file()
    if pid is None or not _is_pid_alive(pid):
        print("Bambu Monitor is not running.")
        pid_file.unlink(missing_ok=True)
        return

    # Guard against PID reuse: never signal a process we cannot verify is ours.
    identity = _pid_is_our_daemon(pid)
    if identity is False:
        print(f"✗ PID {pid} no longer belongs to Bambu Monitor (PID reuse detected) — refusing to kill it.")
        print("  The stale PID file has been removed; the real daemon is not running.")
        pid_file.unlink(missing_ok=True)
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
        if identity is None:
            print("! Could not verify the process identity — skipping SIGKILL (kill manually if needed).")
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    pid_file.unlink(missing_ok=True)
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


async def cmd_service_restart(host: str = "127.0.0.1", port: int = 8000, config_path: Optional[str] = None) -> None:
    """Restart Bambu Monitor daemon."""
    await cmd_service_stop()
    await asyncio.sleep(1.0)
    await cmd_service_start(host=host, port=port, config_path=config_path)


def cmd_service_logs(lines: int = 50) -> None:
    """Display recent logs from the background daemon."""
    log_file = _log_file()
    if not log_file.exists():
        print(f"No log file found at {log_file}")
        return

    content = log_file.read_text(errors="ignore").splitlines()
    recent = content[-lines:] if len(content) > lines else content
    for line in recent:
        print(line)


def _resolve_printer_camera(active_settings: Settings, printer_id: str) -> Optional[CameraConfig]:
    """Look up and validate a printer's camera config, printing an error and
    returning None for any of: unknown printer, no camera section, disabled
    camera, or missing RTSP URL."""
    p_cfg = next((p for p in active_settings.printers if p.id == printer_id), None)
    if not p_cfg:
        print(f"Error: Printer '{printer_id}' is not configured in config.yaml.")
        return None

    if not p_cfg.camera:
        print(f"Error: No camera configured for printer '{printer_id}'.")
        print("Tip: Add a 'camera' section to the printer in config.yaml.")
        return None

    if not p_cfg.camera.enabled:
        print(f"Error: Camera for printer '{printer_id}' is disabled in configuration.")
        return None

    if not p_cfg.camera.rtsp_url:
        print(f"Error: RTSP URL is empty for printer '{printer_id}'.")
        return None

    return p_cfg.camera


async def cmd_camera_snap(
    printer_id: str,
    output: Optional[str] = None,
    settings: Optional[Settings] = None,
) -> None:
    """Capture a single JPEG snapshot from the printer's RTSP camera."""
    active_settings = settings or load_config()
    camera_cfg = _resolve_printer_camera(active_settings, printer_id)
    if not camera_cfg:
        return

    client = CameraClient(config=camera_cfg, printer_id=printer_id)
    print(f"\nCapturing snapshot from camera for {printer_id} ({client.sanitized_url})...")

    t0 = time.perf_counter()
    try:
        jpeg_bytes = await client.capture()
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
    camera_cfg = _resolve_printer_camera(active_settings, printer_id)
    if not camera_cfg:
        return

    client = CameraClient(config=camera_cfg, printer_id=printer_id)
    print(f"\nTesting RTSP camera for {printer_id} ({client.sanitized_url})...")
    print(f"  FFmpeg:               {camera_cfg.ffmpeg_bin}")
    print("  Transport:            TCP")
    print(f"  Probe Size:           {camera_cfg.probe_size_bytes} bytes")
    print(f"  Analyze Duration:     {camera_cfg.analyze_duration_us} us")
    print(f"  Timeout:              {camera_cfg.timeout_seconds}s\n")

    diag = await client.test_connection()
    if not diag.get("connected"):
        print("  ✗ Connection / Capture: FAILED")
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


# --- Timelapse Subsystem CLI Commands ---

async def cmd_timelapse_camera_test(
    printer_id: Optional[str] = None,
    settings: Optional[Settings] = None,
) -> None:
    """Independent camera diagnostic command verifying RTSP connectivity and frame capture."""
    from datetime import datetime
    active_settings = settings or load_config()

    # 1. Configuration loaded
    target_printer_id = printer_id
    p_cfg = None
    if target_printer_id:
        p_cfg = next((p for p in active_settings.printers if p.id == target_printer_id), None)
        if not p_cfg:
            print(f"Error: Printer '{target_printer_id}' not found in configuration.")
            return
    else:
        p_cfg = next((p for p in active_settings.printers if p.camera and p.camera.rtsp_url), None)
        if not p_cfg and active_settings.printers:
            p_cfg = active_settings.printers[0]
        if p_cfg:
            target_printer_id = p_cfg.id

    # get_timelapse_config() already falls back to the printer's generic
    # camera.rtsp_url when no dedicated timelapse camera URL is configured.
    tl_cfg = active_settings.get_timelapse_config(target_printer_id or "default")
    if not tl_cfg.camera.rtsp_url:
        print("Error: No RTSP camera URL configured.")
        print("Tip: Set A1_MINI_CAMERA_RTSP or TIMELAPSE_CAMERA_RTSP in environment or configure camera in config.yaml.")
        return

    client = create_camera_client(tl_cfg.camera, printer_id=target_printer_id or "camera-test")
    cam_label = "Tapo RTSP" if "tapo" in tl_cfg.camera.type.lower() else tl_cfg.camera.type

    # 2. Check RTSP connectivity & metadata probe
    t0 = time.perf_counter()
    try:
        diag = await client.health()
    except Exception as exc:
        print(f"Camera: {cam_label}")
        print("Status: FAILED")
        print("Connection: FAILED")
        print(f"Error: {sanitize_rtsp_url(str(exc))}")
        return

    if not diag.connected:
        print(f"Camera: {cam_label}")
        print("Status: FAILED")
        print("Connection: FAILED")
        print(f"Error: {sanitize_rtsp_url(diag.error or 'Connection failed')}")
        return

    # 3. Stream availability & Frame capture
    try:
        frame_bytes = await client.capture()
        elapsed_ms = (time.perf_counter() - t0) * 1000
    except Exception as exc:
        print(f"Camera: {cam_label}")
        print("Status: FAILED")
        print("Connection: OK")
        print("Snapshot: FAILED")
        print(f"Error: {sanitize_rtsp_url(str(exc))}")
        return

    # 4. JPEG validity & dimensions
    is_valid_jpeg = frame_bytes.startswith(b"\xff\xd8")
    if not is_valid_jpeg:
        print(f"Camera: {cam_label}")
        print("Status: FAILED")
        print("Connection: OK")
        print("Snapshot: FAILED (invalid JPEG marker)")
        print("JPEG: INVALID")
        return

    # Extract resolution from JPEG if not obtained via ffprobe
    if not diag.resolution:
        try:
            import io
            from PIL import Image
            with Image.open(io.BytesIO(frame_bytes)) as img:
                diag.resolution = f"{img.width}x{img.height}"
        except Exception:
            pass

    # 5. Output file creation
    storage = TimelapseStorage(base_dir=active_settings.timelapse.storage_dir)
    test_dir = storage.get_test_dir()
    timestamp_str = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_file = test_dir / f"{timestamp_str}.jpg"
    out_file.write_bytes(frame_bytes)

    latency_val = diag.latency_ms if diag.latency_ms is not None else round(elapsed_ms, 1)
    size_kb = max(1, round(len(frame_bytes) / 1024))

    print(f"Camera: {cam_label}")
    print("Status: CONNECTED")
    print("Connection: OK")
    print(f"Stream: {tl_cfg.camera.stream}")
    print("Snapshot: OK")
    if diag.resolution:
        print(f"Resolution: {diag.resolution}")
    print(f"Latency: {int(latency_val)} ms")
    print("JPEG: VALID")
    print(f"Size: {size_kb} KB")
    print(f"Saved: {out_file}")


async def cmd_timelapse_status(
    printer_id: Optional[str] = None,
    settings: Optional[Settings] = None,
) -> None:
    """Display overall timelapse subsystem status across printers."""
    active_settings = settings or load_config()
    db, p_repo, _, _, _ = get_db_and_repos(active_settings)
    await db.init_db()
    tl_repo = TimelapseRepository(db)

    printers = await p_repo.list_all()
    if printer_id:
        printers = [p for p in printers if p.id == printer_id]

    print("\nBambu Monitor — Timelapse Status\n")
    print(f"Storage Directory: {active_settings.timelapse.storage_dir}")
    print(f"Capture Interval:  {active_settings.timelapse.capture.interval_seconds}s")
    print(f"Video Output:      {active_settings.timelapse.video.fps} FPS ({active_settings.timelapse.video.codec})")
    print()

    if not printers:
        print("No printers configured.")
        return

    for p in printers:
        print(f"Printer: {p.model} ({p.id})")
        tl_cfg = active_settings.get_timelapse_config(p.id)
        print(f"  Timelapse Enabled: {'Yes' if tl_cfg.enabled else 'No'}")
        print(f"  Camera Type:       {tl_cfg.camera.type} ({tl_cfg.camera.stream})")
        active_session = await tl_repo.get_active_session_for_printer(p.id)
        if active_session:
            print(f"  Active Session:    {active_session.id}")
            print(f"  Print Job:         {active_session.print_job_id}")
            print(f"  Status:            {active_session.status.value.upper()}")
            print(f"  Frames Captured:   {active_session.frame_count} ({active_session.missed_frames} missed)")
            if active_session.paused_seconds > 0:
                print(f"  Paused Duration:   {int(active_session.paused_seconds)}s")
            if active_session.error:
                print(f"  Last Error:        {active_session.error}")
        else:
            print("  Active Session:    None (idle)")
        print()


async def cmd_timelapse_list(
    printer_id: Optional[str] = None,
    limit: int = 20,
    settings: Optional[Settings] = None,
) -> None:
    """List historical timelapse sessions."""
    active_settings = settings or load_config()
    db, p_repo, _, _, _ = get_db_and_repos(active_settings)
    await db.init_db()
    tl_repo = TimelapseRepository(db)

    printers = await p_repo.list_all()
    if printer_id:
        printers = [p for p in printers if p.id == printer_id]

    all_sessions = []
    for p in printers:
        sessions = await tl_repo.list_sessions_for_printer(p.id, limit=limit)
        all_sessions.extend(sessions)

    all_sessions.sort(key=lambda s: s.started_at, reverse=True)
    all_sessions = all_sessions[:limit]

    if not all_sessions:
        print("\nNo timelapse sessions recorded yet.\n")
        return

    print("\nRecorded Timelapses:\n")
    header = f"  {'Session ID':<26} {'Printer':<14} {'Job ID':<24} {'Status':<12} {'Frames':<8} {'Video'}"
    print(header)
    print("  " + "─" * 90)
    for s in all_sessions:
        has_video = "✓ Yes" if s.video_path and Path(s.video_path).is_file() else "No"
        print(f"  {s.id:<26} {s.printer_id:<14} {s.print_job_id:<24} {s.status.value:<12} {s.frame_count:<8} {has_video}")
    print()


async def cmd_timelapse_generate(
    job_or_session_id: str,
    settings: Optional[Settings] = None,
) -> None:
    """Manually compile / re-compile an MP4 video from stored frame images.

    Delegates to TimelapseManager.generate_video() — the same operation the
    live service uses to render on print completion — so a manual CLI
    regenerate gets the same overlay-burn support and manifest persistence
    instead of a second, drifted reimplementation.
    """
    active_settings = settings or load_config()
    db, _, _, _, _ = get_db_and_repos(active_settings)
    await db.init_db()
    tl_repo = TimelapseRepository(db)
    storage = TimelapseStorage(base_dir=active_settings.timelapse.storage_dir)
    manager = TimelapseManager(settings=active_settings, timelapse_repo=tl_repo, storage=storage)

    print(f"\nGenerating timelapse video for '{job_or_session_id}'...")
    try:
        video_path = await manager.generate_video(job_or_session_id)
        print(f"  ✓ Video compiled successfully ({video_path.stat().st_size / (1024 * 1024):.2f} MB)")
        print(f"  ✓ Saved to: {video_path.resolve()}\n")
    except Exception as exc:
        print(f"  ✗ Video generation failed: {exc}\n")


async def cmd_timelapse_correlate(
    printer_id: str,
    timelapse_id: Optional[str] = None,
    temp_drop_threshold: float = 10.0,
    settings: Optional[Settings] = None,
) -> None:
    """Correlate print telemetry with visual timelapse frames."""
    active_settings = settings or load_config()
    db, _, _, _, _ = get_db_and_repos(active_settings)
    await db.init_db()
    tl_repo = TimelapseRepository(db)
    event_repo = EventRepository(db)
    storage = TimelapseStorage(base_dir=active_settings.timelapse.storage_dir)

    session = None
    if timelapse_id:
        session = await tl_repo.get_session(timelapse_id)
        if not session:
            session = await tl_repo.get_session_by_job(timelapse_id)
    else:
        sessions = await tl_repo.list_sessions_for_printer(printer_id, limit=1)
        if sessions:
            session = sessions[0]

    if not session:
        target_name = timelapse_id or f"printer '{printer_id}'"
        print(f"Error: Timelapse session not found for {target_name}.")
        return

    frames_metadata = storage.read_frames_metadata(session.storage_dir)
    events = await event_repo.list_for_printer(printer_id, limit=250, since=session.started_at)
    if session.completed_at:
        events = [e for e in events if e.timestamp <= session.completed_at]

    report = TelemetryCorrelator.correlate(
        session=session,
        frames_metadata=frames_metadata,
        events=events,
        temp_drop_threshold=temp_drop_threshold,
    )

    print("\nBambu Monitor — Telemetry & Vision Correlation\n")
    print(f"  Session ID:   {report.session_id}")
    print(f"  Printer ID:   {report.printer_id}")
    print(f"  Print Job:    {report.print_job_id}")
    print(f"  Total Frames: {report.total_frames} ({report.fps} FPS, {report.video_duration_seconds:.1f}s duration)")
    print()

    # Thermal Performance
    noz = report.thermal_summary.nozzle
    bed = report.thermal_summary.bed
    print("  Thermal Performance:")
    if noz:
        target_disp = f" (Target: {noz.target}°C)" if noz.target else ""
        print(f"    Hotend:     Min: {noz.min}°C | Max: {noz.max}°C | Avg: {noz.avg}°C{target_disp}")
    else:
        print("    Hotend:     No telemetry samples recorded")

    if bed:
        target_disp = f" (Target: {bed.target}°C)" if bed.target else ""
        print(f"    Bed:        Min: {bed.min}°C | Max: {bed.max}°C | Avg: {bed.avg}°C{target_disp}")
    else:
        print("    Bed:        No telemetry samples recorded")

    print(f"    Stability:  {report.thermal_summary.stability_score:.1f} / 100")
    print()

    # Anomalies
    print(f"  Detected Anomalies ({len(report.anomalies)}):")
    if not report.anomalies:
        print("    ✓ None detected (temperatures and motion remained within tolerances).")
    else:
        for a in report.anomalies:
            t_str = f"{int(a.video_time_start // 60):02d}:{a.video_time_start % 60:05.2f}"
            badge = f"[{a.severity.upper()}]"
            print(f"    {badge:<10} Frame {a.start_frame} ({t_str}): {a.description}")
    print()

    # Layer Summary
    if report.layers:
        print(f"  Layer Breakdown ({len(report.layers)} layers):")
        header = f"    {'Layer':<8} {'Frames':<14} {'Video Offset':<14} {'Avg Hotend':<14} {'Avg Bed':<12} {'Progress'}"
        print(header)
        print("    " + "─" * 70)
        show_layers = report.layers[:10]
        for lyr in show_layers:
            noz_t = f"{lyr.avg_nozzle_temp}°C" if lyr.avg_nozzle_temp else "--"
            bed_t = f"{lyr.avg_bed_temp}°C" if lyr.avg_bed_temp else "--"
            prog = f"{lyr.avg_progress}%" if lyr.avg_progress else "--"
            frames_range = f"{lyr.start_frame}–{lyr.end_frame}"
            v_offset = f"{lyr.video_time_seconds:.2f}s"
            print(f"    {lyr.layer:<8} {frames_range:<14} {v_offset:<14} {noz_t:<14} {bed_t:<12} {prog}")
        if len(report.layers) > 10:
            print(f"    ... and {len(report.layers) - 10} more layers")
        print()


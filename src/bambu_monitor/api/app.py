"""FastAPI application factory and lifespan lifecycle management."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional
from fastapi import FastAPI

from bambu_monitor.bambu.client import BambuMqttClient
from bambu_monitor.bambu.discovery import discover_printers
from bambu_monitor.bambu.protocol import BAMBU_MQTT_PORT
from bambu_monitor.config import Settings, load_config
from bambu_monitor.domain.printer import Printer
from bambu_monitor.state.manager import StateManager
from bambu_monitor.storage.database import Database
from bambu_monitor.storage.repositories import (
    AlertRepository,
    EventRepository,
    JobRepository,
    OutboxRepository,
    PrinterRepository,
    TimelapseRepository,
)
from bambu_monitor.camera import CameraRegistry, create_camera_client
from bambu_monitor.timelapse import TimelapseManager, TimelapseRenderer, TimelapseStorage
from bambu_monitor.api.routes import router

logger = logging.getLogger(__name__)


async def _periodic_state_flusher(state_manager: StateManager, settings: Settings) -> None:
    """Background task to periodically flush in-memory state snapshots to SQLite."""
    interval = settings.database.flush_interval_seconds
    try:
        while True:
            await asyncio.sleep(interval)
            for printer_id in list(state_manager._states.keys()):
                try:
                    await state_manager.flush_state_to_db(printer_id)
                except Exception:
                    logger.exception("Periodic state flush error for %s", printer_id)
    except asyncio.CancelledError:
        pass


async def _background_ip_tracker(
    printer_repo: PrinterRepository,
    mqtt_clients: dict[str, BambuMqttClient],
    interval_seconds: float = 30.0,
) -> None:
    """Scan local network; update printer host and MQTT client upon DHCP IP change."""
    try:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                discovered = await discover_printers(timeout_seconds=2.0)
                disc_by_serial = {p.serial: p for p in discovered}
                for printer_id, client in list(mqtt_clients.items()):
                    if client.serial_number in disc_by_serial:
                        disc = disc_by_serial[client.serial_number]
                        if disc.ip != client.host:
                            logger.info(
                                "Printer %s (%s) IP changed from %s to %s via DHCP discovery",
                                printer_id,
                                client.serial_number,
                                client.host,
                                disc.ip,
                            )
                            client.update_host(disc.ip)
                            db_printer = await printer_repo.get(printer_id)
                            if db_printer:
                                db_printer.host = disc.ip
                                await printer_repo.save(db_printer)
            except Exception:
                logger.warning("Background IP discovery check error", exc_info=True)
    except asyncio.CancelledError:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings: Settings = app.state.settings
    db = Database(db_path=settings.database.path)
    await db.init_db()

    printer_repo = PrinterRepository(db)
    job_repo = JobRepository(db)
    alert_repo = AlertRepository(db)
    event_repo = EventRepository(db)
    outbox_repo = OutboxRepository(db)
    timelapse_repo = TimelapseRepository(db)

    state_manager = StateManager(
        settings=settings,
        printer_repo=printer_repo,
        job_repo=job_repo,
        alert_repo=alert_repo,
        event_repo=event_repo,
        outbox_repo=outbox_repo,
    )

    # Register RTSP cameras if configured
    camera_registry = CameraRegistry()
    for p_cfg in settings.printers:
        if p_cfg.camera and p_cfg.camera.enabled and p_cfg.camera.rtsp_url:
            cam_client = create_camera_client(config=p_cfg.camera, printer_id=p_cfg.id)
            camera_registry.register(p_cfg.id, cam_client)
            logger.info("Configured camera for printer '%s' (%s)", p_cfg.id, cam_client.sanitized_url)

    # Initialize Timelapse subsystem
    timelapse_storage = TimelapseStorage(base_dir=settings.timelapse.storage_dir)
    timelapse_renderer = TimelapseRenderer()
    timelapse_manager = TimelapseManager(
        settings=settings,
        timelapse_repo=timelapse_repo,
        storage=timelapse_storage,
        camera_registry=camera_registry,
        renderer=timelapse_renderer,
        emit_event_cb=state_manager._emit_event,
        state_manager=state_manager,
    )
    state_manager.add_event_listener(timelapse_manager.handle_domain_event)
    state_manager.add_reconcile_listener(timelapse_manager.reconcile_on_startup)

    # 1. Register configured printers
    for p_cfg in settings.printers:
        existing = await printer_repo.get(p_cfg.id)
        if not existing:
            printer = Printer(
                id=p_cfg.id,
                model=p_cfg.model,
                serial_number=p_cfg.serial_number,
                host=p_cfg.host,
                online=False,
            )
            await printer_repo.save(printer)
        state_manager.register_printer(p_cfg.id, model=p_cfg.model)
        await state_manager.reconcile_on_startup(p_cfg.id)

    # 2. Register any onboarded printers from SQLite
    all_printers = await printer_repo.list_all()
    for p in all_printers:
        if p.id not in state_manager._states:
            state_manager.register_printer(p.id, model=p.model)
            await state_manager.reconcile_on_startup(p.id)

    app.state.db = db
    app.state.printer_repo = printer_repo
    app.state.job_repo = job_repo
    app.state.alert_repo = alert_repo
    app.state.event_repo = event_repo
    app.state.outbox_repo = outbox_repo
    app.state.state_manager = state_manager
    app.state.camera_registry = camera_registry
    app.state.timelapse_repo = timelapse_repo
    app.state.timelapse_storage = timelapse_storage
    app.state.timelapse_renderer = timelapse_renderer
    app.state.timelapse_manager = timelapse_manager

    # 3. Start MQTT clients if enabled
    mqtt_clients: dict[str, BambuMqttClient] = {}
    cfg_by_id = {p.id: p for p in settings.printers}
    loop = asyncio.get_running_loop()

    if settings.application.auto_connect_mqtt:
        for p in all_printers:
            p_cfg = cfg_by_id.get(p.id)
            access_code = p_cfg.access_code if (p_cfg and p_cfg.access_code) else None
            port = p_cfg.port if p_cfg else BAMBU_MQTT_PORT
            tls_verify = p_cfg.tls_verify if p_cfg else False

            client = BambuMqttClient(
                printer_id=p.id,
                serial_number=p.serial_number,
                host=p.host,
                port=port,
                access_code=access_code,
                tls_verify=tls_verify,
                state_manager=state_manager,
                loop=loop,
            )
            client.start()
            mqtt_clients[p.id] = client

    app.state.mqtt_clients = mqtt_clients

    # 4. Start background tasks
    flush_task = asyncio.create_task(_periodic_state_flusher(state_manager, settings))
    discovery_task = None
    if settings.application.enable_discovery:
        discovery_task = asyncio.create_task(_background_ip_tracker(printer_repo, mqtt_clients))

    outbox_worker = None
    if settings.events.delivery.enabled:
        from bambu_monitor.delivery import OutboxDeliveryWorker
        if not settings.events.delivery.secret:
            raise RuntimeError(
                "events.delivery.enabled is true but no EVENT_SECRET is configured. "
                "Webhook events would be delivered unsigned (no X-Hub-Signature-256 "
                "header), so startup is refused. Either set EVENT_SECRET to a long "
                "random value, or set events.delivery.enabled: false (the default) "
                "for local development without Hermes."
            )
        outbox_worker = OutboxDeliveryWorker(
            outbox_repo=outbox_repo,
            printer_repo=printer_repo,
            config=settings.events.delivery,
        )
        outbox_worker.start()
        app.state.outbox_worker = outbox_worker

    logger.info("Bambu Monitor initialized successfully with %d printer(s).", len(all_printers))
    yield

    # Shutdown
    await timelapse_manager.shutdown()
    flush_task.cancel()
    if discovery_task:
        discovery_task.cancel()
    if outbox_worker:
        await outbox_worker.stop()

    for task in (flush_task, discovery_task):
        if task:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Background task raised during shutdown")

    for client in mqtt_clients.values():
        try:
            client.stop()
        except Exception:
            pass

    for p in all_printers:
        try:
            await state_manager.flush_state_to_db(p.id)
        except Exception:
            pass

    logger.info("Bambu Monitor shutdown complete.")


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    active_settings = settings or load_config()

    app = FastAPI(
        title="Bambu Monitor",
        description="Standalone local monitoring service for Bambu Lab 3D printers",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = active_settings
    app.include_router(router)
    return app

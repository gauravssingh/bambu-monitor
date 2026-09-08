"""API Routes for Bambu Monitor (REST and SSE)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from starlette.responses import StreamingResponse

from bambu_monitor.camera import (
    CameraCaptureError,
    CameraConfigError,
    CameraConnectionError,
    CameraError,
    CameraRegistry,
    CameraTimeoutError,
)
from bambu_monitor.domain.alerts import Alert
from bambu_monitor.domain.events import DomainEvent
from bambu_monitor.domain.printer import CurrentPrinterState, Printer
from bambu_monitor.domain.print_job import PrintJob
from bambu_monitor.domain.telemetry import TelemetryPatch
from bambu_monitor.state.manager import StateManager
from bambu_monitor.storage.database import Database
from bambu_monitor.storage.repositories import (
    AlertRepository,
    EventRepository,
    JobRepository,
    OutboxRepository,
    PrinterRepository,
)

router = APIRouter()


def get_state_manager(request: Request) -> StateManager:
    return request.app.state.state_manager


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_printer_repo(request: Request) -> PrinterRepository:
    return request.app.state.printer_repo


def get_job_repo(request: Request) -> JobRepository:
    return request.app.state.job_repo


def get_alert_repo(request: Request) -> AlertRepository:
    return request.app.state.alert_repo


def get_event_repo(request: Request) -> EventRepository:
    return request.app.state.event_repo


def get_outbox_repo(request: Request) -> OutboxRepository:
    return request.app.state.outbox_repo


# --- System & Health ---

@router.get("/health")
async def get_health(
    db: Database = Depends(get_db),
    state_manager: StateManager = Depends(get_state_manager),
    outbox_repo: OutboxRepository = Depends(get_outbox_repo),
    printer_repo: PrinterRepository = Depends(get_printer_repo),
) -> Dict[str, Any]:
    db_health = await db.check_health()
    outbox_counts = await outbox_repo.get_counts()
    configured_printers = await printer_repo.list_all()

    printers_summary = []
    for p in configured_printers:
        st = state_manager.get_state(p.id)
        active_job = state_manager.get_active_job(p.id)
        printers_summary.append({
            "id": p.id,
            "model": p.model,
            "online": st.online if st else p.online,
            "state": st.state.value if st else "unknown",
            "active_job": active_job.id if active_job else None,
        })

    is_healthy = db_health.get("available", False)
    return {
        "status": "ok" if is_healthy else "degraded",
        "timestamp": datetime.now().isoformat(),
        "database": db_health,
        "printers": {
            "configured": len(configured_printers),
            "summary": printers_summary,
        },
        "outbox": outbox_counts,
    }


# --- Printers ---

@router.get("/printers", response_model=List[Printer])
@router.get("/api/v1/printers", response_model=List[Printer])
async def list_printers(
    printer_repo: PrinterRepository = Depends(get_printer_repo),
    state_manager: StateManager = Depends(get_state_manager),
) -> List[Printer]:
    printers = await printer_repo.list_all()
    # Synchronize live online state
    for p in printers:
        st = state_manager.get_state(p.id)
        if st:
            p.online = st.online
            p.last_seen = st.last_seen
    return printers


@router.get("/api/v1/printers/{printer_id}", response_model=Printer)
async def get_printer(
    printer_id: str,
    printer_repo: PrinterRepository = Depends(get_printer_repo),
    state_manager: StateManager = Depends(get_state_manager),
) -> Printer:
    p = await printer_repo.get(printer_id)
    if not p:
        raise HTTPException(status_code=404, detail=f"Printer '{printer_id}' not found")
    st = state_manager.get_state(printer_id)
    if st:
        p.online = st.online
        p.last_seen = st.last_seen
    return p


@router.get("/api/v1/printers/{printer_id}/status", response_model=CurrentPrinterState)
async def get_printer_status(
    printer_id: str,
    state_manager: StateManager = Depends(get_state_manager),
) -> CurrentPrinterState:
    st = state_manager.get_state(printer_id)
    if not st:
        raise HTTPException(status_code=404, detail=f"Printer state for '{printer_id}' not found")
    return st


# --- Print Jobs ---

@router.get("/api/v1/printers/{printer_id}/prints/active", response_model=Optional[PrintJob])
async def get_active_print(
    printer_id: str,
    response: Response,
    state_manager: StateManager = Depends(get_state_manager),
) -> Optional[PrintJob]:
    """Preferred endpoint: 'What's currently printing?' Returns 200 with job or 204 No Content."""
    job = state_manager.get_active_job(printer_id)
    if not job:
        response.status_code = status.HTTP_204_NO_CONTENT
        return None
    return job


@router.get("/api/v1/printers/{printer_id}/prints", response_model=List[PrintJob])
async def list_prints(
    printer_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    job_repo: JobRepository = Depends(get_job_repo),
) -> List[PrintJob]:
    return await job_repo.list_for_printer(printer_id, limit=limit)


# --- Alerts ---

@router.get("/api/v1/printers/{printer_id}/alerts", response_model=List[Alert])
async def list_alerts(
    printer_id: str,
    active_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    alert_repo: AlertRepository = Depends(get_alert_repo),
    state_manager: StateManager = Depends(get_state_manager),
) -> List[Alert]:
    if active_only:
        return state_manager.get_active_alerts(printer_id)
    return await alert_repo.list_all(printer_id, limit=limit)


@router.post("/api/v1/printers/{printer_id}/alerts/{alert_id}/acknowledge", response_model=Alert)
async def acknowledge_alert(
    printer_id: str,
    alert_id: str,
    alert_repo: AlertRepository = Depends(get_alert_repo),
    state_manager: StateManager = Depends(get_state_manager),
) -> Alert:
    alert = await alert_repo.get(alert_id)
    if not alert or alert.printer_id != printer_id:
        raise HTTPException(status_code=404, detail=f"Alert '{alert_id}' not found for printer '{printer_id}'")
    alert.acknowledge()
    await alert_repo.save(alert)
    return alert


# --- Events & SSE Streaming ---

@router.get("/api/v1/printers/{printer_id}/events", response_model=List[DomainEvent])
async def list_events(
    printer_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    event_repo: EventRepository = Depends(get_event_repo),
) -> List[DomainEvent]:
    return await event_repo.list_for_printer(printer_id, limit=limit)


@router.get("/api/v1/printers/{printer_id}/events/stream")
async def stream_events(
    printer_id: str,
    request: Request,
    limit: Optional[int] = Query(default=None, ge=1),
    state_manager: StateManager = Depends(get_state_manager),
) -> StreamingResponse:
    """Stream live domain events for a printer via SSE."""
    async def event_generator():
        count = 0
        try:
            async for event in state_manager.subscribe_events(printer_id):
                if await request.is_disconnected():
                    break
                yield f"event: domain_event\ndata: {event.model_dump_json()}\n\n"
                count += 1
                if limit is not None and count >= limit:
                    break
        except (asyncio.CancelledError, GeneratorExit):
            pass
        except Exception:
            return

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# --- Telemetry Injection (Phase 1 Fixture testing & local testing) ---

@router.post("/api/v1/printers/{printer_id}/telemetry", response_model=List[DomainEvent])
async def inject_telemetry(
    printer_id: str,
    payload: Dict[str, Any],
    state_manager: StateManager = Depends(get_state_manager),
) -> List[DomainEvent]:
    """Inject raw or normalized telemetry patch (used for fixture tests and local testing)."""
    patch = TelemetryPatch.from_raw(printer_id, payload)
    events = await state_manager.apply_patch(patch)
    return events


# --- Outbox Status ---

@router.get("/api/v1/outbox/status")
async def get_outbox_status(
    outbox_repo: OutboxRepository = Depends(get_outbox_repo),
) -> Dict[str, Any]:
    return await outbox_repo.get_counts()


# --- Device Management ---

@router.post("/api/v1/printers/{printer_id}/reconnect")
async def reconnect_printer(
    printer_id: str,
    request: Request,
    printer_repo: PrinterRepository = Depends(get_printer_repo),
) -> Dict[str, Any]:
    """Force immediate MQTT reconnect, re-subscription, and pushall request."""
    printer = await printer_repo.get(printer_id)
    if not printer:
        raise HTTPException(status_code=404, detail=f"Printer '{printer_id}' not found")

    mqtt_clients = getattr(request.app.state, "mqtt_clients", {})
    client = mqtt_clients.get(printer_id)
    if not client:
        raise HTTPException(status_code=404, detail=f"No active MQTT client found for printer '{printer_id}'")

    # Restart client to trigger re-connect and pushall
    client.stop()
    client.start()
    return {"status": "reconnecting", "printer_id": printer_id}


# --- Camera Endpoints ---

@router.get(
    "/api/v1/printers/{printer_id}/camera/snapshot",
    response_class=Response,
    responses={
        200: {
            "content": {"image/jpeg": {}},
            "description": "Binary JPEG image snapshot from the RTSP camera",
        },
        400: {"description": "Invalid camera configuration"},
        404: {"description": "Printer or camera not configured or disabled"},
        502: {"description": "Camera connection or frame capture failure"},
        504: {"description": "Camera snapshot timeout"},
    },
)
async def get_camera_snapshot(printer_id: str, request: Request) -> Response:
    """Capture and return a fresh JPEG snapshot from the printer's RTSP camera."""
    camera_registry: Optional[CameraRegistry] = getattr(request.app.state, "camera_registry", None)
    camera = camera_registry.get(printer_id) if camera_registry else None

    if not camera:
        printer_repo: Optional[PrinterRepository] = getattr(request.app.state, "printer_repo", None)
        if printer_repo and not await printer_repo.get(printer_id):
            raise HTTPException(status_code=404, detail=f"Printer '{printer_id}' not found")
        raise HTTPException(
            status_code=404,
            detail=f"No active camera configured for printer '{printer_id}'",
        )

    try:
        jpeg_bytes = await camera.snapshot()
        return Response(content=jpeg_bytes, media_type="image/jpeg")
    except CameraTimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc))
    except (CameraConnectionError, CameraCaptureError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except CameraConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except CameraError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


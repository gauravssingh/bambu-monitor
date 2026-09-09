"""API Routes for Bambu Monitor (REST and SSE)."""

from __future__ import annotations

import asyncio
import html
import hmac
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from starlette.responses import FileResponse, HTMLResponse, StreamingResponse

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
    TimelapseRepository,
)
from bambu_monitor.timelapse import TelemetryCorrelator, TimelapseManager, TimelapseStorage

logger = logging.getLogger(__name__)

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


def get_timelapse_repo(request: Request) -> TimelapseRepository:
    return request.app.state.timelapse_repo


def get_timelapse_storage(request: Request) -> TimelapseStorage:
    return request.app.state.timelapse_storage


def get_timelapse_manager(request: Request) -> TimelapseManager:
    return request.app.state.timelapse_manager


async def require_api_access(request: Request) -> None:
    """Restrict the local control plane to loopback or a configured API token."""
    settings = request.app.state.settings
    configured_token = settings.application.api_token
    presented_token = request.headers.get("X-API-Key", "")
    if configured_token:
        if presented_token and hmac.compare_digest(presented_token.encode(), configured_token.encode()):
            return
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing API key")

    client = request.client
    client_host = client.host if client else None
    # A missing client address is an anomaly (e.g. an unusual proxy setup),
    # not an authorization — deny it rather than defaulting to allow.
    if not client_host:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Local access only")

    if settings.application.allow_unauthenticated_loopback and client_host in {"127.0.0.1", "::1"}:
        # DNS-rebinding defense: a remote page rebinding to 127.0.0.1 still
        # presents a non-loopback Host header, which browsers will not spoof.
        host_header = (request.headers.get("host") or "").strip().lower()
        if host_header.startswith("["):
            hostname = host_header.split("]", 1)[0].lstrip("[")
        elif ":" in host_header:
            hostname = host_header.rsplit(":", 1)[0]
        else:
            hostname = host_header
        if hostname in {"127.0.0.1", "::1", "localhost"}:
            return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Local access only")


router = APIRouter(dependencies=[Depends(require_api_access)])


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

@router.get("/printers", response_model=List[Printer], include_in_schema=False)  # legacy alias, see /api/v1/printers
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
    printer_repo: PrinterRepository = Depends(get_printer_repo),
) -> StreamingResponse:
    """Stream live domain events for a printer via SSE."""
    if not await printer_repo.get(printer_id):
        raise HTTPException(status_code=404, detail=f"Printer '{printer_id}' not found")

    async def event_generator():
        count = 0
        try:
            async for event in state_manager.subscribe_events(printer_id):
                if await request.is_disconnected():
                    break
                if event is None:
                    # Heartbeat tick during an idle period: an SSE comment
                    # line, ignored by EventSource clients, keeps the
                    # connection alive and lets us detect a disconnect that
                    # happened while no real events were flowing.
                    yield ": heartbeat\n\n"
                    continue
                yield f"event: domain_event\ndata: {event.model_dump_json()}\n\n"
                count += 1
                if limit is not None and count >= limit:
                    break
        except (asyncio.CancelledError, GeneratorExit):
            # Client disconnect / server shutdown: propagate so Starlette can
            # finalize the response instead of swallowing cancellation.
            raise
        except Exception:
            logger.exception("SSE stream error for printer '%s'", printer_id)
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
        jpeg_bytes = await camera.capture()
        return Response(content=jpeg_bytes, media_type="image/jpeg")
    except CameraTimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc))
    except (CameraConnectionError, CameraCaptureError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except CameraConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except CameraError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


async def _get_timelapse_session(
    timelapse_repo: TimelapseRepository,
    timelapse_id: str,
    printer_id: Optional[str] = None,
) -> Any:
    """Fetch a timelapse session by ID, 404ing if missing or owned by a different printer."""
    session = await timelapse_repo.get_session(timelapse_id)
    if not session or (printer_id is not None and session.printer_id != printer_id):
        raise HTTPException(status_code=404, detail=f"Timelapse session '{timelapse_id}' not found")
    return session


def _timelapse_status_color(status_value: str) -> str:
    """Badge color for a timelapse session's status, shared by the gallery and player views."""
    if status_value == "completed":
        return "#10b981"
    if status_value in ("capturing", "paused"):
        return "#f59e0b"
    return "#ef4444"


def _timelapse_video_response(session: Any, timelapse_storage: TimelapseStorage, inline: bool) -> FileResponse:
    """Resolve and serve a session's rendered MP4, either inline or as a download."""
    video_path = timelapse_storage.get_video_path(session.storage_dir)
    if not video_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"Video file for timelapse '{session.id}' has not been generated or was removed",
        )
    if inline:
        return FileResponse(path=str(video_path), media_type="video/mp4", headers={"Content-Disposition": "inline"})
    return FileResponse(
        path=str(video_path),
        media_type="video/mp4",
        filename=f"timelapse_{session.printer_id}_{session.print_job_id}.mp4",
    )


# --- Timelapse Endpoints ---

@router.get("/api/v1/printers/{printer_id}/timelapse/status")
async def get_printer_timelapse_status(
    printer_id: str,
    timelapse_manager: TimelapseManager = Depends(get_timelapse_manager),
    timelapse_repo: TimelapseRepository = Depends(get_timelapse_repo),
    printer_repo: PrinterRepository = Depends(get_printer_repo),
) -> Dict[str, Any]:
    """Retrieve the current live timelapse capture status for a printer."""
    printer = await printer_repo.get(printer_id)
    if not printer:
        raise HTTPException(status_code=404, detail=f"Printer '{printer_id}' not found")

    active_session = timelapse_manager.get_active_session(printer_id)
    recent_sessions = await timelapse_repo.list_sessions_for_printer(printer_id, limit=1)
    latest_session = recent_sessions[0] if recent_sessions else None

    return {
        "printer_id": printer_id,
        "is_capturing": bool(active_session and active_session.status.value == "capturing"),
        "active_session": active_session.model_dump() if active_session else None,
        "latest_session": latest_session.model_dump() if latest_session else None,
    }


@router.get("/api/v1/printers/{printer_id}/timelapses")
async def list_timelapses(
    printer_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    timelapse_repo: TimelapseRepository = Depends(get_timelapse_repo),
    printer_repo: PrinterRepository = Depends(get_printer_repo),
) -> List[Dict[str, Any]]:
    """List timelapse sessions recorded for a printer."""
    printer = await printer_repo.get(printer_id)
    if not printer:
        raise HTTPException(status_code=404, detail=f"Printer '{printer_id}' not found")

    sessions = await timelapse_repo.list_sessions_for_printer(printer_id, limit=limit)
    return [s.model_dump() for s in sessions]


@router.get("/api/v1/printers/{printer_id}/timelapses/gallery", response_class=HTMLResponse)
async def view_timelapse_gallery(
    printer_id: str,
    timelapse_repo: TimelapseRepository = Depends(get_timelapse_repo),
) -> HTMLResponse:
    """Render a responsive HTML card gallery for all timelapses recorded for a printer."""
    sessions = await timelapse_repo.list_sessions_for_printer(printer_id, limit=50)
    esc = html.escape  # All interpolated strings are either URL-derived or printer-reported

    cards_html = ""
    for s in sessions:
        view_url = f"/api/v1/printers/{esc(printer_id)}/timelapses/{esc(s.id)}/view"
        download_url = f"/api/v1/printers/{esc(printer_id)}/timelapses/{esc(s.id)}/video"
        status_color = _timelapse_status_color(s.status.value)
        cards_html += f"""
        <div class="card">
          <div class="card-header">
            <div style="font-weight: 600; font-size: 0.95rem; text-overflow: ellipsis; overflow: hidden; white-space: nowrap;">{esc(s.id)}</div>
            <span class="badge" style="background-color: {status_color}22; color: {status_color}; border: 1px solid {status_color}55;">{esc(s.status.value)}</span>
          </div>
          <div class="card-content">
            <div class="card-detail"><span>Print Job:</span> <strong>{esc(str(s.print_job_id))}</strong></div>
            <div class="card-detail"><span>Started:</span> {s.started_at.strftime('%Y-%m-%d %H:%M')}</div>
            <div class="card-detail"><span>Frames:</span> {s.frame_count:,} ({s.video_fps} FPS)</div>
          </div>
          <div class="card-actions">
            <a href="{view_url}" class="btn btn-primary" style="flex: 1; text-align: center; justify-content: center;">Watch Timelapse</a>
            <a href="{download_url}" download class="btn" title="Download MP4">&darr;</a>
          </div>
        </div>
        """

    if not cards_html:
        cards_html = '<div style="grid-column: 1/-1; text-align: center; padding: 3rem; color: #94a3b8;">No timelapses recorded yet for this printer.</div>'

    page_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Timelapse Gallery &mdash; {html.escape(printer_id)} | Bambu Monitor</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background-color: #0f172a; color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; line-height: 1.5; padding: 2rem 1rem; }}
    .container {{ max-width: 1080px; margin: 0 auto; }}
    header {{ margin-bottom: 2rem; display: flex; justify-content: space-between; align-items: center; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 1.5rem; }}
    .card {{ background: #1e293b; border: 1px solid #334155; border-radius: 0.75rem; overflow: hidden; display: flex; flex-direction: column; }}
    .card-header {{ padding: 1rem; border-bottom: 1px solid #334155; display: flex; justify-content: space-between; align-items: center; gap: 0.5rem; }}
    .badge {{ display: inline-block; padding: 0.2rem 0.5rem; border-radius: 9999px; font-size: 0.7rem; font-weight: 600; text-transform: uppercase; }}
    .card-content {{ padding: 1rem; flex: 1; }}
    .card-detail {{ font-size: 0.85rem; color: #94a3b8; margin-bottom: 0.4rem; display: flex; justify-content: space-between; }}
    .card-detail strong {{ color: #f8fafc; }}
    .card-actions {{ padding: 0.75rem 1rem; background: #0f172a55; border-top: 1px solid #334155; display: flex; gap: 0.5rem; }}
    a.btn {{ display: inline-flex; align-items: center; gap: 0.5rem; background: #1e293b; color: #f8fafc; padding: 0.4rem 0.75rem; border-radius: 0.375rem; text-decoration: none; border: 1px solid #334155; font-size: 0.8rem; font-weight: 500; transition: background 0.15s; }}
    a.btn:hover {{ background: #334155; }}
    a.btn-primary {{ background: #2563eb; border-color: #3b82f6; }}
    a.btn-primary:hover {{ background: #1d4ed8; }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div>
        <h1 style="font-size: 1.75rem; font-weight: 700;">Timelapse Gallery</h1>
        <p style="color: #94a3b8; font-size: 0.875rem;">Printer: <strong>{html.escape(printer_id)}</strong> &bull; {len(sessions)} recorded sessions</p>
      </div>
      <a href="/api/v1/printers" class="btn">&larr; API Status</a>
    </header>

    <div class="grid">
      {cards_html}
    </div>
  </div>
</body>
</html>
"""
    return HTMLResponse(content=page_html)


@router.get("/api/v1/printers/{printer_id}/timelapses/{timelapse_id}")
async def get_timelapse(
    printer_id: str,
    timelapse_id: str,
    timelapse_repo: TimelapseRepository = Depends(get_timelapse_repo),
    timelapse_storage: TimelapseStorage = Depends(get_timelapse_storage),
) -> Dict[str, Any]:
    """Retrieve details, status, and manifest for a specific timelapse session."""
    session = await _get_timelapse_session(timelapse_repo, timelapse_id, printer_id)

    pauses = await timelapse_repo.get_pauses_for_session(timelapse_id)
    manifest = timelapse_storage.load_manifest(session.storage_dir)

    data = session.model_dump()
    data["pauses"] = [p.model_dump() for p in pauses]
    data["manifest"] = manifest.model_dump() if manifest else None
    return data


@router.get("/api/v1/printers/{printer_id}/timelapses/{timelapse_id}/video")
async def get_timelapse_video(
    printer_id: str,
    timelapse_id: str,
    timelapse_repo: TimelapseRepository = Depends(get_timelapse_repo),
    timelapse_storage: TimelapseStorage = Depends(get_timelapse_storage),
) -> FileResponse:
    """Download or stream the generated MP4 timelapse video."""
    session = await _get_timelapse_session(timelapse_repo, timelapse_id, printer_id)
    return _timelapse_video_response(session, timelapse_storage, inline=False)


@router.get("/api/v1/printers/{printer_id}/timelapses/{timelapse_id}/stream")
async def stream_printer_timelapse_video(
    printer_id: str,
    timelapse_id: str,
    timelapse_repo: TimelapseRepository = Depends(get_timelapse_repo),
    timelapse_storage: TimelapseStorage = Depends(get_timelapse_storage),
) -> FileResponse:
    """Stream the generated MP4 timelapse video directly for HTML5 in-browser playback."""
    session = await _get_timelapse_session(timelapse_repo, timelapse_id, printer_id)
    return _timelapse_video_response(session, timelapse_storage, inline=True)


# --- Global Timelapse Endpoints ---

@router.get("/api/v1/timelapses")
async def list_all_timelapses(
    printer_id: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    timelapse_repo: TimelapseRepository = Depends(get_timelapse_repo),
) -> List[Dict[str, Any]]:
    """List all timelapse sessions across all printers, optionally filtered by printer_id."""
    if printer_id:
        sessions = await timelapse_repo.list_sessions_for_printer(printer_id, limit=limit)
    else:
        sessions = await timelapse_repo.list_all_sessions(limit=limit)
    return [s.model_dump() for s in sessions]


@router.get("/api/v1/timelapses/{timelapse_id}")
async def get_timelapse_by_id(
    timelapse_id: str,
    timelapse_repo: TimelapseRepository = Depends(get_timelapse_repo),
    timelapse_storage: TimelapseStorage = Depends(get_timelapse_storage),
) -> Dict[str, Any]:
    """Retrieve details and manifest for a specific timelapse session across all printers."""
    session = await _get_timelapse_session(timelapse_repo, timelapse_id)

    pauses = await timelapse_repo.get_pauses_for_session(timelapse_id)
    manifest = timelapse_storage.load_manifest(session.storage_dir)

    return {
        "session": session.model_dump(),
        "pauses": [p.model_dump() for p in pauses],
        "manifest": manifest.model_dump() if manifest else None,
    }


@router.get("/api/v1/timelapses/{timelapse_id}/video")
async def get_timelapse_video_by_id(
    timelapse_id: str,
    timelapse_repo: TimelapseRepository = Depends(get_timelapse_repo),
    timelapse_storage: TimelapseStorage = Depends(get_timelapse_storage),
) -> FileResponse:
    """Download the generated MP4 timelapse video by session ID."""
    session = await _get_timelapse_session(timelapse_repo, timelapse_id)
    return _timelapse_video_response(session, timelapse_storage, inline=False)


@router.get("/api/v1/timelapses/{timelapse_id}/stream")
async def stream_timelapse_video_by_id(
    timelapse_id: str,
    timelapse_repo: TimelapseRepository = Depends(get_timelapse_repo),
    timelapse_storage: TimelapseStorage = Depends(get_timelapse_storage),
) -> FileResponse:
    """Stream the generated MP4 timelapse video by session ID for in-browser playback."""
    session = await _get_timelapse_session(timelapse_repo, timelapse_id)
    return _timelapse_video_response(session, timelapse_storage, inline=True)


@router.get("/api/v1/printers/{printer_id}/timelapses/{timelapse_id}/frames")
async def list_timelapse_frames(
    printer_id: str,
    timelapse_id: str,
    request: Request,
    timelapse_repo: TimelapseRepository = Depends(get_timelapse_repo),
    timelapse_storage: TimelapseStorage = Depends(get_timelapse_storage),
) -> Dict[str, Any]:
    """List available frames for a session and their direct retrieval URLs."""
    session = await _get_timelapse_session(timelapse_repo, timelapse_id, printer_id)

    session_dir = Path(session.storage_dir)
    frames = timelapse_storage.list_frames(session_dir)

    base_url = str(request.base_url).rstrip("/")
    frame_items = []
    for f in frames:
        try:
            seq = int(f.stem)
        except ValueError:
            continue
        frame_items.append({
            "sequence": seq,
            "filename": f.name,
            "url": f"{base_url}/api/v1/printers/{printer_id}/timelapses/{timelapse_id}/frames/{seq}",
        })

    return {
        "session_id": timelapse_id,
        "printer_id": printer_id,
        "total_frames": len(frame_items),
        "frames": frame_items,
    }


@router.get("/api/v1/printers/{printer_id}/timelapses/{timelapse_id}/frames/{sequence}")
async def get_timelapse_frame(
    printer_id: str,
    timelapse_id: str,
    sequence: int,
    timelapse_repo: TimelapseRepository = Depends(get_timelapse_repo),
    timelapse_storage: TimelapseStorage = Depends(get_timelapse_storage),
) -> FileResponse:
    """Retrieve an individual JPEG frame from a timelapse session."""
    session = await _get_timelapse_session(timelapse_repo, timelapse_id, printer_id)

    session_dir = Path(session.storage_dir)
    frame_path = timelapse_storage.get_frame_path(session_dir, sequence)
    if not frame_path or not frame_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"Frame {sequence} for timelapse '{timelapse_id}' not found",
        )

    return FileResponse(path=str(frame_path), media_type="image/jpeg")


@router.get("/api/v1/printers/{printer_id}/timelapses/{timelapse_id}/metadata")
async def get_timelapse_metadata(
    printer_id: str,
    timelapse_id: str,
    limit: Optional[int] = Query(default=None, ge=1),
    timelapse_repo: TimelapseRepository = Depends(get_timelapse_repo),
    timelapse_storage: TimelapseStorage = Depends(get_timelapse_storage),
) -> List[Dict[str, Any]]:
    """Retrieve frame-by-frame visual history metadata (frames.jsonl) for a session."""
    session = await _get_timelapse_session(timelapse_repo, timelapse_id, printer_id)

    return timelapse_storage.read_frames_metadata(session.storage_dir, limit=limit)


@router.get("/api/v1/printers/{printer_id}/timelapses/{timelapse_id}/correlation")
async def get_timelapse_correlation(
    printer_id: str,
    timelapse_id: str,
    include_timeline: bool = Query(default=True),
    temp_drop_threshold: float = Query(default=10.0, ge=1.0, le=50.0),
    timelapse_repo: TimelapseRepository = Depends(get_timelapse_repo),
    timelapse_storage: TimelapseStorage = Depends(get_timelapse_storage),
    event_repo: EventRepository = Depends(get_event_repo),
) -> Dict[str, Any]:
    """Correlate visual frames with hotend/bed temperature, speed, layers, and anomalies."""
    session = await _get_timelapse_session(timelapse_repo, timelapse_id, printer_id)

    frames_metadata = timelapse_storage.read_frames_metadata(session.storage_dir)
    events = await event_repo.list_for_printer(printer_id, limit=250, since=session.started_at)
    if session.completed_at:
        events = [e for e in events if e.timestamp <= session.completed_at]

    report = TelemetryCorrelator.correlate(
        session=session,
        frames_metadata=frames_metadata,
        events=events,
        temp_drop_threshold=temp_drop_threshold,
    )

    data = report.model_dump(mode="json")
    if not include_timeline:
        data.pop("timeline", None)
    return data


@router.get("/api/v1/printers/{printer_id}/timelapses/{timelapse_id}/view", response_class=HTMLResponse)
async def view_timelapse_html(
    printer_id: str,
    timelapse_id: str,
    timelapse_repo: TimelapseRepository = Depends(get_timelapse_repo),
    timelapse_storage: TimelapseStorage = Depends(get_timelapse_storage),
    event_repo: EventRepository = Depends(get_event_repo),
) -> HTMLResponse:
    """Render a dedicated, responsive HTML5 player with synchronized telemetry HUD and SVG timeline."""
    session = await _get_timelapse_session(timelapse_repo, timelapse_id, printer_id)

    video_url = f"/api/v1/printers/{html.escape(printer_id)}/timelapses/{html.escape(timelapse_id)}/video"
    gallery_url = f"/api/v1/printers/{html.escape(printer_id)}/timelapses/gallery"
    corr_url = f"/api/v1/printers/{printer_id}/timelapses/{timelapse_id}/correlation"
    meta_url = f"/api/v1/printers/{printer_id}/timelapses/{timelapse_id}/metadata"

    status_color = _timelapse_status_color(session.status.value)

    # Run correlation
    frames_metadata = timelapse_storage.read_frames_metadata(session.storage_dir)
    events = await event_repo.list_for_printer(printer_id, limit=250, since=session.started_at)
    if session.completed_at:
        events = [e for e in events if e.timestamp <= session.completed_at]

    report = TelemetryCorrelator.correlate(session=session, frames_metadata=frames_metadata, events=events)
    # json.dumps does not escape '</script>'; a printer-supplied filename could
    # break out of the script tag, so neutralize closing-tag sequences.
    report_json = json.dumps(report.model_dump(mode="json")).replace("</", "<\\/")

    # Anomalies badges HTML
    anomalies_html = ""
    if report.anomalies:
        badge_items = []
        for a in report.anomalies:
            color = "#ef4444" if a.severity == "critical" else "#f59e0b"
            t_str = f"{int(a.video_time_start // 60):02d}:{a.video_time_start % 60:05.2f}"
            badge_items.append(
                f'<button type="button" class="anomaly-btn" style="border-color: {color}; color: {color};" '
                f'onclick="seekTo({a.video_time_start})">'
                f'<strong>[{t_str}] Frame {a.start_frame}:</strong> {html.escape(str(a.description))}'
                f'</button>'
            )
        anomalies_html = f"""
        <div class="section-box" style="border-color: #ef444455; background: #450a0a22;">
          <div style="font-size: 0.85rem; font-weight: 700; color: #f87171; margin-bottom: 0.5rem; display: flex; align-items: center; gap: 0.5rem;">
            <span>⚠️ Identified Telemetry Anomalies ({len(report.anomalies)})</span>
            <span style="font-weight: 400; font-size: 0.75rem; color: #fca5a5;">(Click to jump to timestamp)</span>
          </div>
          <div style="display: flex; flex-wrap: wrap; gap: 0.5rem;">
            {''.join(badge_items)}
          </div>
        </div>
        """

    # Layer jump chips HTML
    layer_chips_html = ""
    if len(report.layers) > 1:
        chips = []
        for lyr in report.layers[:25]:
            chips.append(
                f'<button type="button" class="layer-chip" onclick="seekTo({lyr.video_time_seconds})">'
                f'L{lyr.layer}'
                f'</button>'
            )
        if len(report.layers) > 25:
            chips.append(f'<span style="color: #64748b; font-size: 0.75rem; align-self: center;">+{len(report.layers) - 25} more</span>')
        layer_chips_html = f"""
        <div style="margin-top: 1rem;">
          <div class="stat-label">Jump to Layer</div>
          <div style="display: flex; flex-wrap: wrap; gap: 0.35rem; margin-top: 0.25rem;">
            {''.join(chips)}
          </div>
        </div>
        """

    # Thermal stats
    noz = report.thermal_summary.nozzle
    noz_str = f"{noz.min}°C – {noz.max}°C (avg {noz.avg}°C)" if noz else "N/A"

    bed = report.thermal_summary.bed
    bed_str = f"{bed.min}°C – {bed.max}°C (avg {bed.avg}°C)" if bed else "N/A"

    stability_val = report.thermal_summary.stability_score
    stab_color = "#10b981" if stability_val >= 90 else ("#f59e0b" if stability_val >= 75 else "#ef4444")

    page_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Timelapse &amp; Telemetry — {html.escape(session.id)} | Bambu Monitor</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background-color: #0f172a; color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; line-height: 1.5; padding: 2rem 1rem; }}
    .container {{ max-width: 1040px; margin: 0 auto; }}
    header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.5rem; }}
    a.btn, button.btn {{ display: inline-flex; align-items: center; gap: 0.5rem; background: #1e293b; color: #f8fafc; padding: 0.5rem 1rem; border-radius: 0.5rem; text-decoration: none; border: 1px solid #334155; font-size: 0.875rem; font-weight: 500; transition: background 0.15s; cursor: pointer; }}
    a.btn:hover, button.btn:hover {{ background: #334155; }}
    a.btn-primary {{ background: #2563eb; border-color: #3b82f6; }}
    a.btn-primary:hover {{ background: #1d4ed8; }}
    .badge {{ display: inline-block; padding: 0.25rem 0.65rem; border-radius: 9999px; font-size: 0.75rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; background-color: {status_color}22; color: {status_color}; border: 1px solid {status_color}55; }}
    .video-card {{ background: #1e293b; border: 1px solid #334155; border-radius: 0.75rem; overflow: hidden; box-shadow: 0 10px 25px -5px rgba(0,0,0,0.5); }}
    video {{ width: 100%; max-height: 560px; display: block; background: #000; }}
    .hud-bar {{ background: #0b1120; border-top: 1px solid #334155; border-bottom: 1px solid #334155; padding: 0.75rem 1.25rem; display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 1rem; }}
    .hud-item {{ display: flex; align-items: center; gap: 0.5rem; font-size: 0.875rem; }}
    .hud-label {{ color: #94a3b8; font-size: 0.75rem; text-transform: uppercase; font-weight: 600; }}
    .hud-val {{ font-weight: 700; color: #38bdf8; font-variant-numeric: tabular-nums; }}
    .hud-val-warn {{ color: #f87171 !important; }}
    .card-body {{ padding: 1.5rem; }}
    .title-row {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem; }}
    .section-box {{ background: #0f172a; border: 1px solid #334155; border-radius: 0.5rem; padding: 1rem; margin-top: 1rem; }}
    .anomaly-btn {{ background: #1e293b; border: 1px solid; border-radius: 0.375rem; padding: 0.4rem 0.75rem; font-size: 0.78rem; text-align: left; cursor: pointer; transition: transform 0.1s, background 0.15s; }}
    .anomaly-btn:hover {{ transform: translateY(-1px); background: #334155; }}
    .layer-chip {{ background: #0f172a; color: #94a3b8; border: 1px solid #334155; border-radius: 0.25rem; padding: 0.2rem 0.5rem; font-size: 0.75rem; font-weight: 600; cursor: pointer; transition: all 0.15s; }}
    .layer-chip:hover {{ background: #2563eb; color: #fff; border-color: #3b82f6; }}
    .chart-container {{ position: relative; margin-top: 0.5rem; width: 100%; height: 140px; cursor: crosshair; }}
    svg.timeline-svg {{ width: 100%; height: 100%; display: block; }}
    .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 1rem; margin-top: 1rem; padding-top: 1rem; border-top: 1px solid #334155; }}
    .stat-box {{ background: #0f172a; padding: 0.75rem 1rem; border-radius: 0.5rem; border: 1px solid #1e293b; }}
    .stat-label {{ font-size: 0.75rem; color: #94a3b8; text-transform: uppercase; font-weight: 600; margin-bottom: 0.25rem; }}
    .stat-value {{ font-size: 1.1rem; font-weight: 700; color: #f8fafc; }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div>
        <h1 style="font-size: 1.5rem; font-weight: 700;">Print Timelapse & Telemetry</h1>
        <p style="color: #94a3b8; font-size: 0.875rem;">Printer: <strong>{html.escape(printer_id)}</strong> &bull; Job: {html.escape(str(session.print_job_id))}</p>
      </div>
      <div style="display: flex; gap: 0.5rem; flex-wrap: wrap;">
        <a href="{gallery_url}" class="btn">&larr; Gallery</a>
        <a href="{corr_url}" target="_blank" class="btn">&#128269; Correlation JSON</a>
        <a href="{meta_url}" target="_blank" class="btn">&#128196; Frames Dataset</a>
        <a href="{video_url}" download class="btn btn-primary">&darr; Download MP4</a>
      </div>
    </header>

    <div class="video-card">
      <video id="tl-video" controls autoplay loop playsinline>
        <source src="{video_url}" type="video/mp4">
        Your browser does not support HTML5 video playback.
      </video>

      <!-- Live Reactive Telemetry HUD -->
      <div class="hud-bar">
        <div class="hud-item">
          <span class="hud-label">🔥 Hotend:</span>
          <span id="hud-nozzle" class="hud-val">--</span>
          <span style="color: #64748b; font-size: 0.75rem;">/ <span id="hud-nozzle-target">--</span>°C</span>
        </div>
        <div class="hud-item">
          <span class="hud-label">🛏️ Bed:</span>
          <span id="hud-bed" class="hud-val">--</span>
          <span style="color: #64748b; font-size: 0.75rem;">/ <span id="hud-bed-target">--</span>°C</span>
        </div>
        <div class="hud-item">
          <span class="hud-label">📐 Layer:</span>
          <span id="hud-layer" class="hud-val" style="color: #a78bfa;">--</span>
        </div>
        <div class="hud-item">
          <span class="hud-label">📊 Progress:</span>
          <span id="hud-progress" class="hud-val" style="color: #34d399;">--</span>
        </div>
        <div class="hud-item">
          <span class="hud-label">⚡ Speed:</span>
          <span id="hud-speed" class="hud-val" style="color: #fbbf24;">--</span>
        </div>
        <div class="hud-item" id="hud-anomaly-box" style="display: none;">
          <span id="hud-anomaly-tag" class="badge" style="background-color: #ef444422; color: #f87171; border-color: #ef444488;">ANOMALY</span>
        </div>
      </div>

      <div class="card-body">
        <div class="title-row">
          <div>
            <h2 style="font-size: 1.125rem; font-weight: 600;">{html.escape(session.id)}</h2>
            <p style="font-size: 0.8rem; color: #64748b;">Started: {session.started_at.strftime('%Y-%m-%d %H:%M:%S UTC')}</p>
          </div>
          <span class="badge">{html.escape(session.status.value)}</span>
        </div>

        {anomalies_html}

        <!-- Interactive SVG Temperature Chart -->
        <div class="section-box">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.25rem;">
            <div class="stat-label">Synchronized Temperature Profile (Click timeline to scrub video)</div>
            <div style="font-size: 0.75rem; display: flex; gap: 0.75rem;">
              <span style="color: #f97316;">&bull; Hotend</span>
              <span style="color: #38bdf8;">&bull; Bed</span>
              <span style="color: #ef4444;">&bull; Anomaly Range</span>
            </div>
          </div>
          <div class="chart-container" id="chart-container">
            <svg id="timeline-svg" class="timeline-svg" preserveAspectRatio="none" viewBox="0 0 1000 120">
              <!-- Grid background -->
              <line x1="0" y1="20" x2="1000" y2="20" stroke="#334155" stroke-dasharray="2 2" stroke-width="0.5"/>
              <line x1="0" y1="60" x2="1000" y2="60" stroke="#334155" stroke-dasharray="2 2" stroke-width="0.5"/>
              <line x1="0" y1="100" x2="1000" y2="100" stroke="#334155" stroke-dasharray="2 2" stroke-width="0.5"/>
              <g id="svg-anomalies"></g>
              <polyline id="svg-nozzle-target" fill="none" stroke="#64748b" stroke-dasharray="3 3" stroke-width="1"/>
              <polyline id="svg-bed-line" fill="none" stroke="#38bdf8" stroke-width="2"/>
              <polyline id="svg-nozzle-line" fill="none" stroke="#f97316" stroke-width="2"/>
              <line id="svg-scrub-line" x1="0" y1="0" x2="0" y2="120" stroke="#f8fafc" stroke-width="2"/>
            </svg>
          </div>
        </div>

        {layer_chips_html}

        <!-- Thermal Performance & Session Metrics -->
        <div class="stats-grid">
          <div class="stat-box">
            <div class="stat-label">Hotend Range</div>
            <div class="stat-value" style="color: #f97316;">{noz_str}</div>
          </div>
          <div class="stat-box">
            <div class="stat-label">Bed Range</div>
            <div class="stat-value" style="color: #38bdf8;">{bed_str}</div>
          </div>
          <div class="stat-box">
            <div class="stat-label">Thermal Stability</div>
            <div class="stat-value" style="color: {stab_color};">{stability_val} / 100</div>
          </div>
          <div class="stat-box">
            <div class="stat-label">Frames / FPS</div>
            <div class="stat-value">{session.frame_count:,} ({session.video_fps} FPS)</div>
          </div>
          <div class="stat-box">
            <div class="stat-label">Capture Interval</div>
            <div class="stat-value">{session.capture_interval_seconds}s</div>
          </div>
          <div class="stat-box">
            <div class="stat-label">Missed Frames</div>
            <div class="stat-value">{session.missed_frames}</div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <script id="correlation-data" type="application/json">
  {report_json}
  </script>

  <script>
    const reportData = JSON.parse(document.getElementById('correlation-data').textContent);
    const video = document.getElementById('tl-video');
    const scrubLine = document.getElementById('svg-scrub-line');
    const chartContainer = document.getElementById('chart-container');
    const hudNozzle = document.getElementById('hud-nozzle');
    const hudNozzleTarget = document.getElementById('hud-nozzle-target');
    const hudBed = document.getElementById('hud-bed');
    const hudBedTarget = document.getElementById('hud-bed-target');
    const hudLayer = document.getElementById('hud-layer');
    const hudProgress = document.getElementById('hud-progress');
    const hudSpeed = document.getElementById('hud-speed');
    const hudAnomalyBox = document.getElementById('hud-anomaly-box');
    const hudAnomalyTag = document.getElementById('hud-anomaly-tag');

    const timeline = reportData.timeline || [];
    const totalFrames = reportData.total_frames || timeline.length;
    const duration = reportData.video_duration_seconds || (video.duration || 1);

    // Draw SVG polylines
    if (timeline.length > 1) {{
      const maxTemp = 280; // normalize 0-280 C to 120-0 Y
      const ptsNozzle = [];
      const ptsBed = [];
      const ptsNozzleTarget = [];

      timeline.forEach((pt, i) => {{
        const x = (i / (timeline.length - 1)) * 1000;
        const nozY = pt.nozzle_temp != null ? 120 - (pt.nozzle_temp / maxTemp) * 120 : 120;
        const bedY = pt.bed_temp != null ? 120 - (pt.bed_temp / maxTemp) * 120 : 120;
        const nozTarY = pt.nozzle_target != null ? 120 - (pt.nozzle_target / maxTemp) * 120 : 120;

        ptsNozzle.push(`${{x.toFixed(1)}},${{nozY.toFixed(1)}}`);
        ptsBed.push(`${{x.toFixed(1)}},${{bedY.toFixed(1)}}`);
        ptsNozzleTarget.push(`${{x.toFixed(1)}},${{nozTarY.toFixed(1)}}`);
      }});

      document.getElementById('svg-nozzle-line').setAttribute('points', ptsNozzle.join(' '));
      document.getElementById('svg-bed-line').setAttribute('points', ptsBed.join(' '));
      document.getElementById('svg-nozzle-target').setAttribute('points', ptsNozzleTarget.join(' '));

      // Draw anomaly bands
      const anomG = document.getElementById('svg-anomalies');
      (reportData.anomalies || []).forEach(anom => {{
        const startX = ((anom.start_frame - 1) / Math.max(1, totalFrames - 1)) * 1000;
        const endFrame = anom.end_frame || anom.start_frame;
        const endX = ((endFrame - 1) / Math.max(1, totalFrames - 1)) * 1000;
        const width = Math.max(6, endX - startX);
        const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
        rect.setAttribute('x', startX.toFixed(1));
        rect.setAttribute('y', '0');
        rect.setAttribute('width', width.toFixed(1));
        rect.setAttribute('height', '120');
        rect.setAttribute('fill', anom.severity === 'critical' ? 'rgba(239, 68, 68, 0.35)' : 'rgba(245, 158, 11, 0.3)');
        anomG.appendChild(rect);
      }});
    }}

    // Real-time HUD update on video playback
    video.addEventListener('timeupdate', () => {{
      const curTime = video.currentTime;
      const curDuration = video.duration || duration || 1;
      const progressRatio = Math.min(1.0, Math.max(0.0, curTime / curDuration));

      // Update scrub line
      scrubLine.setAttribute('x1', (progressRatio * 1000).toFixed(1));
      scrubLine.setAttribute('x2', (progressRatio * 1000).toFixed(1));

      // Find timeline point
      if (timeline.length > 0) {{
        const frameIdx = Math.min(timeline.length - 1, Math.floor(progressRatio * timeline.length));
        const pt = timeline[frameIdx];
        if (pt) {{
          hudNozzle.textContent = pt.nozzle_temp != null ? `${{pt.nozzle_temp}}°C` : '--';
          hudNozzleTarget.textContent = pt.nozzle_target != null ? pt.nozzle_target : '--';
          hudBed.textContent = pt.bed_temp != null ? `${{pt.bed_temp}}°C` : '--';
          hudBedTarget.textContent = pt.bed_target != null ? pt.bed_target : '--';
          hudLayer.textContent = pt.layer != null ? `Layer ${{pt.layer}}` : '--';
          hudProgress.textContent = pt.progress != null ? `${{pt.progress}}%` : '--';

          // Speed
          const spdNames = {{1: '100% Standard', 2: '50% Silent', 3: '124% Sport', 4: '166% Ludicrous'}};
          hudSpeed.textContent = pt.speed_percent != null ? `${{pt.speed_percent}}%` : (spdNames[pt.speed_level] || '--');

          // Thermal drop alert warning
          if (pt.nozzle_temp != null && pt.nozzle_target != null && pt.nozzle_target >= 100 && (pt.nozzle_target - pt.nozzle_temp) >= 10) {{
            hudNozzle.classList.add('hud-val-warn');
          }} else {{
            hudNozzle.classList.remove('hud-val-warn');
          }}

          // Anomaly badge
          if (pt.anomaly_ids && pt.anomaly_ids.length > 0) {{
            hudAnomalyBox.style.display = 'flex';
            hudAnomalyTag.textContent = `⚠️ ANOMALY (${{pt.anomaly_ids.join(', ')}})`;
          }} else {{
            hudAnomalyBox.style.display = 'none';
          }}
        }}
      }}
    }});

    // Click SVG chart to scrub video
    chartContainer.addEventListener('click', (e) => {{
      const rect = chartContainer.getBoundingClientRect();
      const clickRatio = Math.max(0.0, Math.min(1.0, (e.clientX - rect.left) / rect.width));
      const targetTime = clickRatio * (video.duration || duration);
      seekTo(targetTime);
    }});

    function seekTo(seconds) {{
      video.currentTime = Math.max(0, seconds);
      video.play();
    }}
  </script>
</body>
</html>
"""
    return HTMLResponse(content=page_html)



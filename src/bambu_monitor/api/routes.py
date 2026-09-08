"""API Routes for Bambu Monitor (REST and SSE)."""

from __future__ import annotations

import asyncio
import hmac
import json
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
from bambu_monitor.timelapse import TimelapseManager, TimelapseStorage

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
        if hmac.compare_digest(presented_token, configured_token):
            return
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing API key")

    client_host = request.client.host if request.client else None
    if settings.application.allow_unauthenticated_loopback and client_host in {None, "127.0.0.1", "::1", "testclient"}:
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


# --- Timelapse Endpoints ---

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

    cards_html = ""
    for s in sessions:
        view_url = f"/api/v1/printers/{printer_id}/timelapses/{s.id}/view"
        download_url = f"/api/v1/printers/{printer_id}/timelapses/{s.id}/video"
        status_color = "#10b981" if s.status.value == "completed" else ("#f59e0b" if s.status.value in ("capturing", "paused") else "#ef4444")
        cards_html += f"""
        <div class="card">
          <div class="card-header">
            <div style="font-weight: 600; font-size: 0.95rem; text-overflow: ellipsis; overflow: hidden; white-space: nowrap;">{s.id}</div>
            <span class="badge" style="background-color: {status_color}22; color: {status_color}; border: 1px solid {status_color}55;">{s.status.value}</span>
          </div>
          <div class="card-content">
            <div class="card-detail"><span>Print Job:</span> <strong>{s.print_job_id}</strong></div>
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

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Timelapse Gallery — {printer_id} | Bambu Monitor</title>
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
        <p style="color: #94a3b8; font-size: 0.875rem;">Printer: <strong>{printer_id}</strong> &bull; {len(sessions)} recorded sessions</p>
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
    return HTMLResponse(content=html)


@router.get("/api/v1/printers/{printer_id}/timelapses/{timelapse_id}")
async def get_timelapse(
    printer_id: str,
    timelapse_id: str,
    timelapse_repo: TimelapseRepository = Depends(get_timelapse_repo),
    timelapse_storage: TimelapseStorage = Depends(get_timelapse_storage),
) -> Dict[str, Any]:
    """Retrieve details, status, and manifest for a specific timelapse session."""
    session = await timelapse_repo.get_session(timelapse_id)
    if not session or session.printer_id != printer_id:
        raise HTTPException(status_code=404, detail=f"Timelapse session '{timelapse_id}' not found")

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
    session = await timelapse_repo.get_session(timelapse_id)
    if not session or session.printer_id != printer_id:
        raise HTTPException(status_code=404, detail=f"Timelapse session '{timelapse_id}' not found")

    video_path = timelapse_storage.get_video_path(session.storage_dir)
    if not video_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"Video file for timelapse '{timelapse_id}' has not been generated or was removed",
        )

    return FileResponse(
        path=str(video_path),
        media_type="video/mp4",
        filename=f"timelapse_{printer_id}_{session.print_job_id}.mp4",
    )


@router.get("/api/v1/printers/{printer_id}/timelapses/{timelapse_id}/frames")
async def list_timelapse_frames(
    printer_id: str,
    timelapse_id: str,
    request: Request,
    timelapse_repo: TimelapseRepository = Depends(get_timelapse_repo),
    timelapse_storage: TimelapseStorage = Depends(get_timelapse_storage),
) -> Dict[str, Any]:
    """List available frames for a session and their direct retrieval URLs."""
    session = await timelapse_repo.get_session(timelapse_id)
    if not session or session.printer_id != printer_id:
        raise HTTPException(status_code=404, detail=f"Timelapse session '{timelapse_id}' not found")

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
    session = await timelapse_repo.get_session(timelapse_id)
    if not session or session.printer_id != printer_id:
        raise HTTPException(status_code=404, detail=f"Timelapse session '{timelapse_id}' not found")

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
    session = await timelapse_repo.get_session(timelapse_id)
    if not session or session.printer_id != printer_id:
        raise HTTPException(status_code=404, detail=f"Timelapse session '{timelapse_id}' not found")

    return timelapse_storage.read_frames_metadata(session.storage_dir, limit=limit)


@router.get("/api/v1/printers/{printer_id}/timelapses/{timelapse_id}/view", response_class=HTMLResponse)
async def view_timelapse_html(
    printer_id: str,
    timelapse_id: str,
    timelapse_repo: TimelapseRepository = Depends(get_timelapse_repo),
    timelapse_storage: TimelapseStorage = Depends(get_timelapse_storage),
) -> HTMLResponse:
    """Render a dedicated, responsive HTML5 player interface for viewing the timelapse video."""
    session = await timelapse_repo.get_session(timelapse_id)
    if not session or session.printer_id != printer_id:
        raise HTTPException(status_code=404, detail=f"Timelapse session '{timelapse_id}' not found")

    video_url = f"/api/v1/printers/{printer_id}/timelapses/{timelapse_id}/video"
    gallery_url = f"/api/v1/printers/{printer_id}/timelapses/gallery"
    status_color = "#10b981" if session.status.value == "completed" else ("#f59e0b" if session.status.value in ("capturing", "paused") else "#ef4444")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Timelapse — {session.id} | Bambu Monitor</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background-color: #0f172a; color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; line-height: 1.5; padding: 2rem 1rem; }}
    .container {{ max-width: 960px; margin: 0 auto; }}
    header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.5rem; }}
    a.btn, button.btn {{ display: inline-flex; align-items: center; gap: 0.5rem; background: #1e293b; color: #f8fafc; padding: 0.5rem 1rem; border-radius: 0.5rem; text-decoration: none; border: 1px solid #334155; font-size: 0.875rem; font-weight: 500; transition: background 0.15s; cursor: pointer; }}
    a.btn:hover {{ background: #334155; }}
    a.btn-primary {{ background: #2563eb; border-color: #3b82f6; }}
    a.btn-primary:hover {{ background: #1d4ed8; }}
    .badge {{ display: inline-block; padding: 0.25rem 0.65rem; border-radius: 9999px; font-size: 0.75rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; background-color: {status_color}22; color: {status_color}; border: 1px solid {status_color}55; }}
    .video-card {{ background: #1e293b; border: 1px solid #334155; border-radius: 0.75rem; overflow: hidden; box-shadow: 0 10px 25px -5px rgba(0,0,0,0.5); }}
    video {{ width: 100%; max-height: 600px; display: block; background: #000; }}
    .card-body {{ padding: 1.5rem; }}
    .title-row {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem; }}
    .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 1rem; margin-top: 1rem; padding-top: 1rem; border-top: 1px solid #334155; }}
    .stat-box {{ background: #0f172a; padding: 0.75rem 1rem; border-radius: 0.5rem; border: 1px solid #1e293b; }}
    .stat-label {{ font-size: 0.75rem; color: #94a3b8; text-transform: uppercase; font-weight: 600; margin-bottom: 0.25rem; }}
    .stat-value {{ font-size: 1.125rem; font-weight: 700; color: #f8fafc; }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div>
        <h1 style="font-size: 1.5rem; font-weight: 700;">Print Timelapse</h1>
        <p style="color: #94a3b8; font-size: 0.875rem;">Printer: <strong>{printer_id}</strong> &bull; Job: {session.print_job_id}</p>
      </div>
      <div style="display: flex; gap: 0.5rem;">
        <a href="{gallery_url}" class="btn">&larr; All Timelapses</a>
        <a href="{video_url}" download class="btn btn-primary">&darr; Download MP4</a>
      </div>
    </header>

    <div class="video-card">
      <video controls autoplay loop playsinline>
        <source src="{video_url}" type="video/mp4">
        Your browser does not support HTML5 video playback.
      </video>
      <div class="card-body">
        <div class="title-row">
          <div>
            <h2 style="font-size: 1.125rem; font-weight: 600;">{session.id}</h2>
            <p style="font-size: 0.8rem; color: #64748b;">Started: {session.started_at.strftime('%Y-%m-%d %H:%M:%S UTC')}</p>
          </div>
          <span class="badge">{session.status.value}</span>
        </div>

        <div class="stats-grid">
          <div class="stat-box">
            <div class="stat-label">Frames</div>
            <div class="stat-value">{session.frame_count:,}</div>
          </div>
          <div class="stat-box">
            <div class="stat-label">Framerate</div>
            <div class="stat-value">{session.video_fps} FPS</div>
          </div>
          <div class="stat-box">
            <div class="stat-label">Interval</div>
            <div class="stat-value">{session.capture_interval_seconds}s</div>
          </div>
          <div class="stat-box">
            <div class="stat-label">Paused Time</div>
            <div class="stat-value">{int(session.paused_seconds)}s</div>
          </div>
          <div class="stat-box">
            <div class="stat-label">Missed Frames</div>
            <div class="stat-value">{session.missed_frames}</div>
          </div>
        </div>
      </div>
    </div>
  </div>
</body>
</html>
"""
    return HTMLResponse(content=html)



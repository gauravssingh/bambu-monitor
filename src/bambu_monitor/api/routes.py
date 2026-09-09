"""API Routes for Bambu Monitor (REST and SSE)."""

from __future__ import annotations

import asyncio
import html
import hmac
import ipaddress
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from string import Template
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


def _host_header_hostname(request: Request) -> str:
    """Extract the hostname portion of the Host header (sans port, IPv6 brackets)."""
    host_header = (request.headers.get("host") or "").strip().lower()
    if host_header.startswith("["):
        return host_header.split("]", 1)[0].lstrip("[")
    if ":" in host_header:
        return host_header.rsplit(":", 1)[0]
    return host_header


# Explicit trusted home-LAN ranges (RFC1918 + IPv6 ULA/link-local). Deliberately
# avoids ipaddress.is_private, which also counts documentation/reserved ranges
# (192.0.2.0/24, 203.0.113.0/24, ...) that must stay unauthorized.
_LAN_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7", "fe80::/10")
)


def _is_private_lan_ip(host: str) -> bool:
    """True for RFC1918/ULA/link-local addresses (trusted home-LAN clients)."""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(addr.version == net.version and addr in net for net in _LAN_NETWORKS)


async def require_api_access(request: Request) -> None:
    """Restrict the local control plane to loopback, home LAN, or a configured API token."""
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
        if _host_header_hostname(request) in {"127.0.0.1", "::1", "localhost"}:
            return

    if settings.application.allow_unauthenticated_lan and _is_private_lan_ip(client_host):
        # Home-LAN access without a token. DNS-rebinding defense: a page
        # rebound to a LAN IP still carries the attacker's domain in the
        # Host header, so only IP-literal (or localhost) hosts are allowed.
        hostname = _host_header_hostname(request)
        try:
            ipaddress.ip_address(hostname)
            return  # Host is a bare IP literal (browsers on the LAN)
        except ValueError:
            if hostname == "localhost":
                return
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Local access only")

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


# Named video variants servable alongside the primary timelapse (path traversal is
# impossible: fixed dict, values joined to the storage dir without user input).
_VIDEO_VARIANTS = {
    "layers": "timelapse_layers_{fps}fps.mp4",  # layer-change-only cut (any fps)
}


def _timelapse_video_response(
    session: Any,
    timelapse_storage: TimelapseStorage,
    inline: bool,
    variant: Optional[str] = None,
) -> FileResponse:
    """Resolve and serve a session's rendered MP4, either inline or as a download."""
    video_path = timelapse_storage.get_video_path(session.storage_dir)
    if variant:
        # A rendered variant cut (e.g. layer-change-only). fps is fixed per render.
        requested = None
        if variant == "layers":
            for fps in (10, 30, 5, 2):
                candidate = Path(session.storage_dir) / _VIDEO_VARIANTS["layers"].format(fps=fps)
                if candidate.is_file():
                    requested = candidate
                    break
        if requested is None:
            raise HTTPException(
                status_code=404,
                detail=f"Requested video variant '{variant}' is not available for timelapse '{session.id}'",
            )
        video_path = requested
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


# --- Timelapse gallery page: display helpers ---

_TL_STATUS_ORDER = ("capturing", "paused", "degraded", "finalizing", "completed", "failed", "idle")


def _tl_gallery_status(status_value: str) -> tuple[str, str]:
    """Map a session status to a (pill class, human label) pair for gallery chips."""
    if status_value == "completed":
        return "ok", "Completed"
    if status_value == "failed":
        return "bad", "Failed"
    if status_value == "capturing":
        return "busy", "Recording"
    if status_value == "paused":
        return "busy", "Paused"
    if status_value == "degraded":
        return "busy", "Degraded"
    if status_value == "finalizing":
        return "busy", "Finalizing"
    return "idle", "Idle"


def _tl_gallery_duration_seconds(session: Any) -> Optional[float]:
    """Wall-clock print duration for a session (completed->started, else last update)."""
    end = session.completed_at or session.updated_at
    if session.started_at and end:
        delta = (end - session.started_at).total_seconds()
        if delta > 0:
            return delta
    return None


def _tl_gallery_title(session: Any, job: Optional[Any]) -> str:
    """Human-readable card title derived from the job filename when possible."""
    raw = (job.filename if job else None) or (session.metadata or {}).get("filename")
    if raw:
        clean = re.sub(r"(?i)\.(?:3mf|gcode|stl|obj|amf|step|stp)$", "", str(raw).strip()).strip()
        if clean:
            return clean[:90]
    fallback = _tl_short_job_id(str(session.print_job_id), session.printer_id)
    return fallback or str(session.id)


def _tl_gallery_hero_frame(frames: List[Path]) -> Optional[Path]:
    """Pick a representative frame (~70% through the capture) for a gallery poster."""
    if not frames:
        return None
    count = len(frames)
    return frames[max(0, min(count - 1, int(count * 0.7) - 1))]


def _tl_gallery_video_element(stream_url: str) -> str:
    """Hidden-by-default <video> used as a real poster frame once metadata loads."""
    return (
        '<video class="tl-video" muted playsinline preload="none" '
        f'src="{stream_url}" aria-hidden="true" '
        'onerror="window.__tlVideoError&&window.__tlVideoError(this)"></video>'
    )


def _tl_gallery_status_option(status_value: str) -> str:
    """A <option> for the Status filter, restricted to statuses present in the data."""
    _, label = _tl_gallery_status(status_value)
    return f'<option value="{status_value}">{label}</option>'


def _tl_gallery_state_panel(kind: str, printer_id: str = "", retry_url: str = "") -> str:
    """Empty/error state panel injected instead of the card grid."""
    if kind == "empty":
        return (
            '<div class="tl-state">'
            f'<div class="tl-state-ic">{_tl_icon("video")}</div>'
            "<h2>No timelapses yet</h2>"
            f"<p>When a timelapse is recorded for <strong>{html.escape(printer_id)}</strong>, it will appear here.</p>"
            "</div>"
        )
    return (
        '<div class="tl-state">'
        f'<div class="tl-state-ic">{_tl_icon("refresh")}</div>'
        "<h2>Unable to load timelapses</h2>"
        "<p>The gallery could not be loaded. The service may still be starting, or timelapse storage may be unavailable.</p>"
        f'<a class="btn btn-primary" href="{html.escape(retry_url, quote=True)}">{_tl_icon("refresh")}Retry</a>'
        "</div>"
    )


def _tl_gallery_card(session: Any, job: Optional[Any], hero_frame: Optional[Path], has_video: bool) -> str:
    """One media card for a single timelapse session (shared by grid and list views)."""
    esc = html.escape

    def attr(s: Any) -> str:
        return html.escape(str(s), quote=True)

    printer_id = session.printer_id
    session_id = session.id
    view_url = f"/api/v1/printers/{attr(printer_id)}/timelapses/{attr(session_id)}/view"
    download_url = f"/api/v1/printers/{attr(printer_id)}/timelapses/{attr(session_id)}/video"
    stream_url = f"/api/v1/printers/{attr(printer_id)}/timelapses/{attr(session_id)}/stream"
    metadata_url = f"/api/v1/printers/{attr(printer_id)}/timelapses/{attr(session_id)}/metadata"
    correlation_url = f"/api/v1/printers/{attr(printer_id)}/timelapses/{attr(session_id)}/correlation"

    pill_class, pill_label = _tl_gallery_status(session.status.value)
    title = _tl_gallery_title(session, job)
    short_job = _tl_short_job_id(str(session.print_job_id), printer_id)

    duration_sec = _tl_gallery_duration_seconds(session)
    duration_fmt = _tl_fmt_duration(duration_sec) if duration_sec else "—"

    started_dt = session.started_at
    started_short = started_dt.strftime("%b %d, %Y %H:%M") + " UTC"
    started_full = started_dt.strftime("%b %d, %Y %H:%M:%S") + " UTC"
    started_epoch = int(started_dt.timestamp())

    frame_count = int(session.frame_count or 0)
    has_frames = frame_count > 0
    frames_value = f"{frame_count:,} ({session.video_fps} FPS)" if has_frames else "No frames"

    file_name = ""
    if job and job.filename:
        file_name = str(job.filename)
    elif (session.metadata or {}).get("filename"):
        file_name = str(session.metadata["filename"])

    search_parts = " ".join(
        p for p in (title, short_job, file_name, str(session.print_job_id), session_id) if p
    ).lower()

    # --- Media block: real frame when retained on disk, else a real frame pulled from the MP4 ---
    media_class = "tl-media"
    hero_img = ""
    hero_video = ""
    if hero_frame is not None:
        seq = int(Path(hero_frame).stem)
        frame_url = f"/api/v1/printers/{attr(printer_id)}/timelapses/{attr(session_id)}/frames/{seq}"
        media_class += " has-frame"
        hero_img = (
            '<img class="tl-frame" alt="" loading="lazy" decoding="async" '
            f'src="{frame_url}" onerror="window.__tlFrameError&&window.__tlFrameError(this)">'
        )
    if has_video:
        media_class += " has-video" if "has-frame" in media_class else " has-video poster-video"
        hero_video = _tl_gallery_video_element(stream_url)
    if "has-frame" not in media_class and not has_video:
        media_class += " no-media"

    media = (
        f'<a class="{media_class}" href="{view_url}" aria-label="Watch timelapse: {attr(title)}">'
        f"{hero_img}{hero_video}"
        '<span class="tl-ph"><span class="tl-ph-ic">' + _tl_icon("film") + "</span>"
        "<span class=\"tl-ph-txt\">No preview yet</span></span>"
        '<span class="tl-media-top"><span class="tl-chips">'
        f'<span class="tl-chip"><span class="tl-chip-ic">{_tl_icon("calendar")}</span>'
        f'<span title="{attr(started_full)}">{esc(started_short)}</span></span>'
    )
    if duration_sec:
        media += (
            f'<span class="tl-chip"><span class="tl-chip-ic">{_tl_icon("clock")}</span>'
            f"{esc(duration_fmt)}</span>"
        )
    media += (
        f"</span><span class=\"tl-pill tl-pill-{pill_class}\"><span class=\"tl-dot\" aria-hidden=\"true\"></span>{esc(pill_label)}</span></span>"
        f'<span class="tl-play" aria-hidden="true">{_tl_icon("play", filled=True)}</span></a>'
    )

    # --- Card body ---
    def _cell(icon: str, label: str, value: str, title: str = "") -> str:
        tip = f' title="{attr(title)}"' if title else ""
        return (
            '<div class="tl-cell">'
            f'<span class="tl-cell-label">{_tl_icon(icon)}<span>{esc(label)}</span></span>'
            f'<span class="tl-cell-value"{tip}>{esc(value)}</span>'
            "</div>"
        )

    meta = ""
    meta += _cell("printer", "Printer", printer_id, printer_id)
    meta += _cell("cube", "Job", short_job, str(session.print_job_id))
    meta += _cell("calendar", "Started", started_short, started_full)
    meta += _cell("layers", "Frames", frames_value, "")
    meta += _cell("clock", "Duration", duration_fmt, "")
    meta += _cell("file", "File", file_name or "—", file_name)

    body = (
        '<div class="tl-body">'
        f'<h3 class="tl-title" title="{attr(title)}">{esc(title)}</h3>'
        f'<p class="tl-jobline"><span class="tl-jobline-lbl">{_tl_icon("cube")}Job</span>'
        f'<span class="tl-ell" title="{attr(str(session.print_job_id))}">{esc(short_job)}</span></p>'
        f'<div class="tl-meta">{meta}</div>'
        "</div>"
    )

    # --- Card actions: Watch (primary) > Download (secondary) > overflow menu ---
    actions = (
        '<div class="tl-actions">'
        f'<a class="btn btn-primary tl-watch" href="{view_url}">{_tl_icon("play", filled=True)}Watch Timelapse</a>'
        f'<a class="btn tl-iconbtn" href="{download_url}" download aria-label="Download timelapse MP4" '
        f'title="Download timelapse MP4">{_tl_icon("download")}</a>'
        '<div class="tl-pop">'
        '<button type="button" class="btn tl-iconbtn tl-popbtn" aria-haspopup="true" aria-expanded="false" '
        f'aria-label="More actions for {attr(title)}">{_tl_icon("more_h")}</button>'
        '<div class="tl-menu" role="menu" hidden>'
        f'<div class="tl-menu-cap" title="{attr(session_id)}">{esc(session_id)}</div>'
        f'<a role="menuitem" href="{view_url}">{_tl_icon("eye")}Open detail page</a>'
        f'<a role="menuitem" href="{metadata_url}" target="_blank" rel="noopener">{_tl_icon("braces")}Frame metadata (JSON)</a>'
        f'<a role="menuitem" href="{correlation_url}" target="_blank" rel="noopener">{_tl_icon("activity")}Correlation report (JSON)</a>'
        "</div></div></div>"
    )

    duration_attr = str(int(duration_sec)) if duration_sec else ""
    return (
        '<article class="tl-card" '
        f'data-status="{attr(session.status.value)}" data-started="{started_epoch}" '
        f'data-duration="{duration_attr}" data-frames="{frame_count}" '
        f'data-search="{attr(search_parts)}">'
        f"{media}{body}{actions}</article>"
    )


@router.get("/api/v1/printers/{printer_id}/timelapses/gallery", response_class=HTMLResponse)
async def view_timelapse_gallery(
    printer_id: str,
    timelapse_repo: TimelapseRepository = Depends(get_timelapse_repo),
    timelapse_storage: TimelapseStorage = Depends(get_timelapse_storage),
    job_repo: JobRepository = Depends(get_job_repo),
) -> HTMLResponse:
    """Render the media-card timelapse gallery for a printer's recorded sessions."""
    esc = html.escape
    retry_url = f"/api/v1/printers/{html.escape(printer_id, quote=True)}/timelapses/gallery"

    try:
        sessions = await timelapse_repo.list_sessions_for_printer(printer_id, limit=50)
    except Exception:
        logger.exception("Gallery load failed for printer %s", printer_id)
        page = _TL_GALLERY_PAGE.substitute(
            doc_title=f"Timelapse Gallery &mdash; {esc(printer_id)} | Bambu Monitor",
            printer=esc(printer_id),
            head_meta="",
            status_options="",
            cards_html="",
            state_html=_tl_gallery_state_panel("error", retry_url=retry_url),
            body_class="tl-stateonly",
        )
        return HTMLResponse(content=page, status_code=500)

    # Enrich each session with its print job (file name, layer progress, ...) for display.
    jobs_by_id: Dict[str, Any] = {}
    try:
        for job in await job_repo.list_for_printer(printer_id, limit=200):
            jobs_by_id[job.id] = job
    except Exception:
        logger.warning("Could not load print jobs to enrich gallery for %s", printer_id, exc_info=True)

    async def _scan(storage: TimelapseStorage, session: Any) -> tuple:
        def _work() -> tuple:
            folder = Path(session.storage_dir)
            return (
                storage.list_frames(folder),
                storage.get_video_path(folder).is_file(),
            )

        try:
            return await asyncio.to_thread(_work)
        except Exception:
            logger.warning("Session media scan failed for %s", session.id, exc_info=True)
            return [], False

    media_scans = await asyncio.gather(*(_scan(timelapse_storage, s) for s in sessions))

    cards_html = "".join(
        _tl_gallery_card(s, jobs_by_id.get(str(s.print_job_id)), _tl_gallery_hero_frame(frames), has_video)
        for s, (frames, has_video) in zip(sessions, media_scans)
    )

    total = len(sessions)
    plural = "" if total == 1 else "s"
    present_statuses = list(dict.fromkeys(s.status.value for s in sessions))
    present_statuses.sort(key=lambda v: _TL_STATUS_ORDER.index(v) if v in _TL_STATUS_ORDER else 99)
    status_options = "".join(_tl_gallery_status_option(v) for v in present_statuses)

    head_meta = (
        f'<span class="tl-head-sep" aria-hidden="true">&middot;</span>'
        f"<span>{total} recorded session{plural}</span>"
    )
    if total == 0:
        state_html = _tl_gallery_state_panel("empty", printer_id=printer_id)
        body_class = "tl-stateonly"
    else:
        state_html = ""
        body_class = ""

    page = _TL_GALLERY_PAGE.substitute(
        doc_title=f"Timelapse Gallery &mdash; {esc(printer_id)} | Bambu Monitor",
        printer=esc(printer_id),
        head_meta=head_meta,
        status_options=status_options,
        cards_html=cards_html,
        state_html=state_html,
        body_class=body_class,
    )
    return HTMLResponse(content=page)


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
    variant: Optional[str] = Query(default=None),
    timelapse_repo: TimelapseRepository = Depends(get_timelapse_repo),
    timelapse_storage: TimelapseStorage = Depends(get_timelapse_storage),
) -> FileResponse:
    """Download or stream the generated MP4 timelapse video."""
    session = await _get_timelapse_session(timelapse_repo, timelapse_id, printer_id)
    return _timelapse_video_response(session, timelapse_storage, inline=False, variant=variant)


@router.get("/api/v1/printers/{printer_id}/timelapses/{timelapse_id}/stream")
async def stream_printer_timelapse_video(
    printer_id: str,
    timelapse_id: str,
    variant: Optional[str] = Query(default=None),
    timelapse_repo: TimelapseRepository = Depends(get_timelapse_repo),
    timelapse_storage: TimelapseStorage = Depends(get_timelapse_storage),
) -> FileResponse:
    """Stream the generated MP4 timelapse video directly for HTML5 in-browser playback."""
    session = await _get_timelapse_session(timelapse_repo, timelapse_id, printer_id)
    return _timelapse_video_response(session, timelapse_storage, inline=True, variant=variant)


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
    variant: Optional[str] = Query(default=None),
    timelapse_repo: TimelapseRepository = Depends(get_timelapse_repo),
    timelapse_storage: TimelapseStorage = Depends(get_timelapse_storage),
) -> FileResponse:
    """Download the generated MP4 timelapse video by session ID."""
    session = await _get_timelapse_session(timelapse_repo, timelapse_id)
    return _timelapse_video_response(session, timelapse_storage, inline=False, variant=variant)


@router.get("/api/v1/timelapses/{timelapse_id}/stream")
async def stream_timelapse_video_by_id(
    timelapse_id: str,
    variant: Optional[str] = Query(default=None),
    timelapse_repo: TimelapseRepository = Depends(get_timelapse_repo),
    timelapse_storage: TimelapseStorage = Depends(get_timelapse_storage),
) -> FileResponse:
    """Stream the generated MP4 timelapse video by session ID for in-browser playback."""
    session = await _get_timelapse_session(timelapse_repo, timelapse_id)
    return _timelapse_video_response(session, timelapse_storage, inline=True, variant=variant)


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


# --- Timelapse detail page: shared display helpers ---

def _tl_icon(name: str, *, filled: bool = False) -> str:
    """Inline lucide-style stroke icon (24x24 viewBox, stroke-width 2) for consistent iconography.

    Pass filled=True for glyphs that read better as solid shapes (e.g. the media play triangle).
    """
    paths = {
        "arrow_left": '<path d="M19 12H5"/><path d="m12 19-7-7 7-7"/>',
        "calendar": '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4"/><path d="M8 2v4"/><path d="M3 10h18"/>',
        "clock": '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
        "layers": '<path d="m12 2 8.5 4.5L12 11 3.5 6.5 12 2z"/><path d="m3.5 11.5 8.5 4.5 8.5-4.5"/><path d="m3.5 16.5 8.5 4.5 8.5-4.5"/>',
        "film": '<rect x="2" y="4" width="20" height="16" rx="2"/><path d="M7 4v16"/><path d="M17 4v16"/><path d="M2 9h5"/><path d="M2 15h5"/><path d="M17 9h5"/><path d="M17 15h5"/>',
        "file": '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7z"/><path d="M14 2v5h6"/>',
        "image": '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-4.35-4.35a1 1 0 0 0-1.42 0L5 21"/>',
        "braces": '<path d="M8 3H7a2 2 0 0 0-2 2v4a2 2 0 0 1-2 2 2 2 0 0 1 2 2v4a2 2 0 0 0 2 2h1"/><path d="M16 3h1a2 2 0 0 1 2 2v4a2 2 0 0 0 2 2 2 2 0 0 0-2 2v4a2 2 0 0 1-2 2h-1"/>',
        "download": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>',
        "thermometer": '<path d="M14 4v10.54a4 4 0 1 1-4 0V4a2 2 0 0 1 4 0z"/>',
        "bed": '<path d="M2 4v16"/><path d="M2 8h18a2 2 0 0 1 2 2v10"/><path d="M2 17h20"/><path d="M6 8v9"/>',
        "fan": '<path d="M10.827 16.379a6.082 6.082 0 0 1-8.618-7.002l5.412 1.45a6.082 6.082 0 0 1 7.002-8.618l-1.45 5.412a6.082 6.082 0 0 1 8.618 7.002l-5.412-1.45a6.082 6.082 0 0 1-7.002 8.618l1.45-5.412Z"/><path d="M12 12v.01"/>',
        "check": '<path d="M20 6 9 17l-5-5"/>',
        "printer": '<polyline points="6 9 6 2 18 2 18 9"/><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><rect x="6" y="14" width="12" height="8"/>',
        "cube": '<path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/>',
        "activity": '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
        "alert": '<path d="m21.73 18-8-14a2 2 0 0 0-3.46 0l-8 14A2 2 0 0 0 4 20h16a2 2 0 0 0 1.73-2Z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
        "timer": '<path d="M10 2h4"/><path d="m12 14 3-3"/><circle cx="12" cy="14" r="8"/>',
        "gauge": '<path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/>',
        "hash": '<path d="M4 9h16"/><path d="M4 15h16"/><path d="M10 3 8 21"/><path d="M16 3l-2 18"/>',
        "play": '<path d="M6 4.5 20 12 6 19.5V4.5z"/>',
        "search": '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
        "more_h": '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
        "more_v": '<circle cx="12" cy="5" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="12" cy="19" r="1"/>',
        "refresh": '<path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/>',
        "video": '<rect x="2" y="7" width="13" height="10" rx="2"/><path d="m15 10 7-3v10l-7-3"/>',
        "eye": '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/>',
        "hourglass": '<path d="M5 22h14"/><path d="M5 2h14"/><path d="M17 22v-4.172a2 2 0 0 0-.586-1.414L12 12l-4.414 4.414A2 2 0 0 0 7 17.828V22"/><path d="M7 2v4.172a2 2 0 0 0 .586 1.414L12 12l4.414-4.414A2 2 0 0 0 17 6.172V2"/>',
    }
    body = paths.get(name, paths["activity"])
    if filled:
        return (
            '<svg viewBox="0 0 24 24" fill="currentColor" stroke="none" aria-hidden="true">'
            f"{body}</svg>"
        )
    return (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{body}</svg>'
    )


def _tl_fmt_duration(seconds: Optional[float]) -> str:
    """Format seconds as a compact human duration, e.g. '1h 2m 51s', '5m 2s' or '27s'."""
    if not seconds or seconds <= 0:
        return "N/A"
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _tl_fmt_video_time(seconds: float) -> str:
    """Format a video position as m:ss (or h:mm:ss beyond an hour)."""
    seconds = max(0.0, seconds)
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _tl_fmt_bytes(num: Optional[int]) -> str:
    """Format a byte count as a human-readable size."""
    if num is None or num < 0:
        return "N/A"
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


def _tl_short_job_id(job_id: str, printer_id: str) -> str:
    """Strip 'job_<printer>_' prefixes and trailing uuid fragments for compact display."""
    short = str(job_id)
    if short.startswith("job_"):
        short = short[len("job_"):]
    sanitized_printer = re.sub(r"[^\w]+", "_", printer_id).strip("_")
    prefix = sanitized_printer + "_"
    if sanitized_printer and short.startswith(prefix):
        short = short[len(prefix):]
    short = re.sub(r"_[0-9a-f]{8}$", "", short)
    return short


def _tl_event_badge(anom_type_value: str) -> tuple:
    """Map a TelemetryAnomaly type value to a (label, badge-class) pair."""
    if anom_type_value in ("nozzle_temp_drop", "bed_temp_drop"):
        return ("Temperature", "temp")
    if anom_type_value == "speed_change":
        return ("Speed", "speed")
    if anom_type_value == "pause":
        return ("Print", "print")
    if anom_type_value == "stall":
        return ("Stall", "stall")
    if anom_type_value == "filament_runout":
        return ("Filament", "filament")
    return ("Event", "print")


def _tl_chart_grid() -> str:
    """Static, subtle horizontal grid lines for the temperature SVG (viewBox 1000x100, 0-280°C)."""
    lines = []
    for temp in (0, 50, 100, 150, 200, 250):
        y = round(100 - (temp / 280) * 100, 2)
        if temp == 0:
            lines.append(f'<line x1="0" y1="{y}" x2="1000" y2="{y}" stroke="#1c2941" stroke-width="0.6"/>')
        else:
            lines.append(
                f'<line x1="0" y1="{y}" x2="1000" y2="{y}" stroke="#1c2941" '
                f'stroke-dasharray="2 3" stroke-width="0.6"/>'
            )
    return "".join(lines)


def _tl_chart_yaxis() -> str:
    """Y-axis temperature labels positioned to match the SVG grid lines."""
    spans = []
    for temp in (0, 50, 100, 150, 200, 250):
        top = round(100 - (temp / 280) * 100, 2)
        spans.append(f'<span style="top:{top}%;">{temp}°C</span>')
    return "".join(spans)


@router.get("/api/v1/printers/{printer_id}/timelapses/{timelapse_id}/view", response_class=HTMLResponse)
async def view_timelapse_html(
    printer_id: str,
    timelapse_id: str,
    timelapse_repo: TimelapseRepository = Depends(get_timelapse_repo),
    timelapse_storage: TimelapseStorage = Depends(get_timelapse_storage),
    event_repo: EventRepository = Depends(get_event_repo),
    job_repo: JobRepository = Depends(get_job_repo),
) -> HTMLResponse:
    """Render the Print Timelapse & Telemetry dashboard page for a single session."""
    session = await _get_timelapse_session(timelapse_repo, timelapse_id, printer_id)

    video_url = f"/api/v1/printers/{html.escape(printer_id)}/timelapses/{html.escape(timelapse_id)}/video"
    gallery_url = f"/api/v1/printers/{html.escape(printer_id)}/timelapses/gallery"
    # Offer the layer-change-only cut when a variant render exists on disk.
    layers_variant_url = ""
    if any((Path(session.storage_dir) / _VIDEO_VARIANTS["layers"].format(fps=fps)).is_file() for fps in (10, 30, 5, 2)):
        layers_variant_url = (
            f"<a href=\"{video_url}?variant=layers\" download class=\"btn\">{_tl_icon('film')} Layer-only cut</a>"
        )
    corr_url = f"/api/v1/printers/{printer_id}/timelapses/{timelapse_id}/correlation"
    meta_url = f"/api/v1/printers/{printer_id}/timelapses/{timelapse_id}/metadata"

    esc = html.escape  # All interpolated strings are either URL-derived or printer-reported

    # Run correlation
    frames_metadata = timelapse_storage.read_frames_metadata(session.storage_dir)
    events = await event_repo.list_for_printer(printer_id, limit=250, since=session.started_at)
    if session.completed_at:
        events = [e for e in events if e.timestamp <= session.completed_at]

    report = TelemetryCorrelator.correlate(session=session, frames_metadata=frames_metadata, events=events)
    # json.dumps does not escape '</script>'; a printer-supplied filename could
    # break out of the script tag, so neutralize closing-tag sequences.
    report_json = json.dumps(report.model_dump(mode="json")).replace("</", "<\\/")

    # --- Read-only enrichment for the dashboard (display only, no persistence) ---
    job = await job_repo.get(str(session.print_job_id))

    video_size_bytes = None
    try:
        video_path = timelapse_storage.get_video_path(session.storage_dir)
        if video_path.is_file():
            video_size_bytes = video_path.stat().st_size
    except OSError:
        video_size_bytes = None

    print_duration = None
    if session.completed_at:
        print_duration = (session.completed_at - session.started_at).total_seconds()
    elif job and job.duration_seconds:
        print_duration = job.duration_seconds

    max_layer = max((lyr.layer for lyr in report.layers), default=0)

    status_value = session.status.value
    if status_value == "completed":
        status_class, status_label = "sb-completed", "Completed"
        status_sub = "Print finished successfully"
        hero_icon_class = ""
    elif status_value in ("capturing", "paused"):
        status_class, status_label = "sb-capturing", status_value.capitalize()
        status_sub = "Recording in progress"
        hero_icon_class = "warn"
    elif status_value == "finalizing":
        status_class, status_label = "sb-capturing", "Finalizing"
        status_sub = "Rendering timelapse video"
        hero_icon_class = "warn"
    else:
        status_class, status_label = "sb-failed", status_value.capitalize()
        status_sub = session.error or "Print did not finish successfully"
        hero_icon_class = "bad"

    # Header metadata row
    started_at = session.started_at
    if session.completed_at:
        same_day = started_at.date() == session.completed_at.date()
        meta_date = started_at.strftime("%b %d, %Y")
        end_time = session.completed_at.strftime("%H:%M") if same_day else session.completed_at.strftime("%b %d %H:%M")
        meta_time = f"{started_at.strftime('%H:%M')} &rarr; {esc(end_time)} UTC"
    else:
        meta_date = started_at.strftime("%b %d, %Y")
        meta_time = f"{started_at.strftime('%H:%M')} UTC"
    meta_duration = _tl_fmt_duration(print_duration)
    meta_layers = f"{max_layer} layers" if max_layer else f"{report.total_frames} frames"
    meta_ftype = "MP4" + (f" &middot; {report.fps} FPS" if report.fps else "")
    meta_size = _tl_fmt_bytes(video_size_bytes)

    # Job status rows (label icon, label, value)
    def _kv(icon: str, label: str, value: str) -> str:
        return (
            '<div class="kv-row">'
            f'<span class="kv-label">{_tl_icon(icon)}{esc(label)}</span>'
            f'<span class="kv-value">{value}</span>'
            "</div>"
        )

    # Job Status answers "what happened to this print?": identity + lifecycle only.
    # Layer count, frames/FPS and file size are owned by the header / Key Metrics.
    task_id = (job.metadata or {}).get("task_id") if job else None
    job_rows = _kv("printer", "Printer", esc(session.printer_id))
    job_rows += _kv("cube", "Job ID", f'<code class="kv-code">{esc(_tl_short_job_id(str(session.print_job_id), printer_id))}</code>')
    if task_id:
        job_rows += _kv("hash", "Task ID", esc(str(task_id)))
    job_rows += _kv("calendar", "Started", f"{esc(started_at.strftime('%b %d, %Y'))} &middot; {esc(started_at.strftime('%H:%M:%S'))} UTC")
    if session.completed_at:
        job_rows += _kv("check", "Completed", f"{esc(session.completed_at.strftime('%b %d, %Y'))} &middot; {esc(session.completed_at.strftime('%H:%M:%S'))} UTC")
    job_rows += _kv("clock", "Duration", esc(_tl_fmt_duration(print_duration)))
    if job and job.filename:
        job_rows += _kv("file", "File", esc(job.filename))

    # Key metrics
    noz = report.thermal_summary.nozzle
    noz_str = f"{noz.min:g} &ndash; {noz.max:g}&deg;C" if noz else "N/A"
    noz_sub = f"Avg: {noz.avg:g}&deg;C" if noz else "No hotend samples"

    bed = report.thermal_summary.bed
    bed_str = f"{bed.min:g} &ndash; {bed.max:g}&deg;C" if bed else "N/A"
    bed_sub = f"Avg: {bed.avg:g}&deg;C" if bed else "No bed samples"

    stability_val = report.thermal_summary.stability_score
    stab_color = "#10b981" if stability_val >= 90 else ("#f59e0b" if stability_val >= 75 else "#ef4444")

    missed_val = session.missed_frames
    missed_color = "#34d399" if missed_val == 0 else "#f87171"

    def _km(icon: str, icon_class: str, label: str, value: str, sub: str = "") -> str:
        sub_html = f'<span class="km-sub">{sub}</span>' if sub else ""
        return (
            '<div class="km">'
            f'<div class="km-icon {icon_class}">{_tl_icon(icon)}</div>'
            '<div class="km-content">'
            f'<span class="km-label">{esc(label)}</span>'
            f'<span class="km-value">{value}</span>'
            f"{sub_html}"
            "</div>"
            "</div>"
        )

    key_metrics_html = ""
    key_metrics_html += _km("thermometer", "km-hotend", "Hotend Range", noz_str, noz_sub)
    key_metrics_html += _km("bed", "km-bed", "Bed Range", bed_str, bed_sub)
    key_metrics_html += _km("gauge", "km-stab", "Thermal Stability", f'<span style="color:{stab_color};">{stability_val:g} / 100</span>')
    key_metrics_html += _km("film", "km-frames", "Frames / FPS", f"{report.total_frames:,} ({report.fps} FPS)")
    key_metrics_html += _km("timer", "km-interval", "Capture Interval", f"{session.capture_interval_seconds:g}s")
    key_metrics_html += _km("alert", "km-missed", "Missed Frames", f'<span style="color:{missed_color};">{missed_val}</span>')

    # Telemetry events table (click a row to seek the video)
    anomalies = sorted(report.anomalies, key=lambda a: a.video_time_start)
    events_count = len(anomalies)
    critical_count = sum(1 for a in anomalies if a.severity == "critical")
    if events_count:
        count_pill = (
            f'<span class="events-pill {"pill-critical" if critical_count else "pill-neutral"}">'
            f'{_tl_icon("alert")} {events_count} anomal{"y" if events_count == 1 else "ies"} detected'
            "</span>"
        )
    else:
        count_pill = '<span class="events-pill pill-ok">' + _tl_icon("check") + ' No anomalies</span>'

    event_rows = ""
    for idx, a in enumerate(anomalies):
        badge_label, badge_class = _tl_event_badge(a.type.value)
        sev_class = "ev-critical" if a.severity == "critical" else ("ev-warning" if a.severity == "warning" else "")
        extra_class = "event-extra" if idx >= 6 else ""
        event_rows += (
            f'<tr class="event-row {sev_class} {extra_class}" data-seek="{a.video_time_start:.3f}" '
            f'title="Click to jump to this moment in the video">'
            f'<td class="ev-time">{_tl_fmt_video_time(a.video_time_start)}</td>'
            f'<td class="ev-frame">{a.start_frame}</td>'
            f'<td><span class="ev-badge badge-{badge_class}"><span class="ev-dot"></span>{esc(badge_label)}</span></td>'
            f'<td class="ev-msg">{esc(str(a.description))}</td>'
            "</tr>"
        )
    if not event_rows:
        event_rows = (
            '<tr><td colspan="4" class="events-empty">'
            'No telemetry events recorded during this print.</td></tr>'
        )
    events_view_all = (
        '<button type="button" id="events-toggle" class="events-toggle">View All &rarr;</button>'
        if events_count > 6
        else ""
    )

    # Layer navigation chips (direct seek to the first frame of each layer). Only
    # representative milestones are rendered so the grid stays compact on large
    # jobs; the Go-to-layer field still resolves any layer via report.layers.
    def _layer_marks(total: int) -> list:
        """Nice round milestone numbers for a job of `total` layers."""
        if total <= 12:
            return list(range(1, total + 1))
        stride = None
        for candidate in (10, 20, 50, 100, 250):
            if total // candidate <= 11:
                stride = candidate
                break
        if stride is None:
            stride = 250
        marks = [1]
        marks.extend(range(stride, total, stride))
        if marks[-1] != total:
            marks.append(total)
        return marks

    by_layer = {lyr.layer: lyr for lyr in report.layers}
    layer_chips = ""
    seen_layers = set()
    for mark in _layer_marks(max_layer):
        lyr = by_layer.get(mark)
        if lyr is None:
            # Milestone not sampled individually: snap to the nearest recorded layer.
            nearest = min(by_layer.values(), key=lambda r: abs(r.layer - mark)) if by_layer else None
            if nearest is None:
                continue
            lyr = nearest
        chip_layer = lyr.layer
        if chip_layer in seen_layers:
            continue
        seen_layers.add(chip_layer)
        layer_chips += (
            f'<button type="button" class="layer-chip" data-layer="{chip_layer}" '
            f'data-seek="{lyr.video_time_seconds:.3f}" '
            f'title="Layer {chip_layer} &middot; {_tl_fmt_video_time(lyr.video_time_seconds)}">'
            f"{chip_layer}"
            "</button>"
        )
    layers_empty = "" if by_layer else '<p class="layers-empty">No layer data available for this session.</p>'

    page = _TL_VIEW_PAGE.substitute(
        doc_title=f"Timelapse &amp; Telemetry &mdash; {esc(str(session.id))}",
        back_icon=_tl_icon("arrow_left"),
        status_badge_class=status_class,
        status_icon=_tl_icon("check"),
        status_label=esc(status_label),
        printer=esc(printer_id),
        short_job_id=esc(_tl_short_job_id(str(session.print_job_id), printer_id)),
        meta_date=meta_date,
        meta_time=meta_time,
        meta_duration=esc(meta_duration),
        meta_layers=esc(meta_layers),
        meta_ftype=meta_ftype,
        meta_size=esc(meta_size),
        icon_calendar=_tl_icon("calendar"),
        icon_clock=_tl_icon("clock"),
        icon_layers=_tl_icon("layers"),
        icon_film=_tl_icon("film"),
        icon_file=_tl_icon("file"),
        icon_image=_tl_icon("image"),
        icon_braces=_tl_icon("braces"),
        icon_download=_tl_icon("download"),
        icon_activity=_tl_icon("activity"),
        icon_thermo=_tl_icon("thermometer"),
        icon_bed=_tl_icon("bed"),
        icon_fan=_tl_icon("fan"),
        icon_alert=_tl_icon("alert"),
        gallery_url=gallery_url,
        corr_url=corr_url,
        meta_url=meta_url,
        video_url=video_url,
        layers_btn=layers_variant_url,
        chart_grid=_tl_chart_grid(),
        chart_yaxis=_tl_chart_yaxis(),
        events_pill=count_pill,
        events_rows=event_rows,
        events_view_all=events_view_all,
        status_icon_hero=_tl_icon("check"),
        status_hero_icon_class=hero_icon_class,
        status_hero_label=esc(status_label),
        status_hero_sub=esc(status_sub),
        job_rows=job_rows,
        key_metrics=key_metrics_html,
        layer_chips=layer_chips,
        layers_empty=layers_empty,
        max_layer=max_layer,
        report_json=report_json,
    )
    return HTMLResponse(content=page)


_TL_VIEW_PAGE = Template("""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>$doc_title | Bambu Monitor</title>
  <style>
    :root {
      --bg: #0b1120;
      --surface: #111a2e;
      --surface-2: #0e1626;
      --border: #1e293b;
      --border-soft: #182236;
      --text: #f1f5f9;
      --text-muted: #94a3b8;
      --text-faint: #64748b;
      --blue: #3b82f6;
      --blue-hover: #2563eb;
      --green: #10b981;
      --green-soft: #34d399;
      --red: #ef4444;
      --red-soft: #f87171;
      --amber: #f59e0b;
      --amber-soft: #fbbf24;
      --purple: #a78bfa;
      --hotend: #f97316;
      --hotend-soft: #fb923c;
      --bed: #38bdf8;
      --radius: 14px;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      -webkit-font-smoothing: antialiased;
      line-height: 1.5;
    }
    code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .page { max-width: 1280px; margin: 0 auto; padding: 28px 32px 56px; }

    /* --- Header --- */
    .back-link {
      display: inline-flex; align-items: center; gap: 8px;
      color: var(--text-muted); font-size: 13px; font-weight: 500;
      text-decoration: none; margin-bottom: 18px;
      transition: color 0.15s;
    }
    .back-link:hover { color: var(--text); }
    .back-link svg { width: 15px; height: 15px; }
    .page-header {
      display: flex; align-items: flex-start; justify-content: space-between;
      flex-wrap: wrap; column-gap: 36px; row-gap: 18px; margin-bottom: 28px;
    }
    .title-block { flex: 1 1 420px; min-width: 0; }
    .title-row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
    .title-row h1 { font-size: 25px; font-weight: 700; letter-spacing: -0.01em; line-height: 1.3; }
    .status-badge {
      display: inline-flex; align-items: center; gap: 6px;
      padding: 4px 12px; border-radius: 999px;
      font-size: 12.5px; font-weight: 600; line-height: 1.4;
    }
    .status-badge svg { width: 14px; height: 14px; }
    .sb-completed { background: #10b98118; color: var(--green-soft); border: 1px solid #10b98140; }
    .sb-capturing { background: #f59e0b18; color: var(--amber-soft); border: 1px solid #f59e0b40; }
    .sb-failed { background: #ef444418; color: var(--red-soft); border: 1px solid #ef444440; }
    .subtitle { margin-top: 8px; font-size: 13.5px; color: var(--text-muted); }
    .subtitle strong { color: var(--text); font-weight: 600; }
    .meta-row { display: flex; flex-wrap: wrap; gap: 10px 22px; margin-top: 12px; }
    .meta-item {
      display: inline-flex; align-items: center; gap: 7px;
      font-size: 12.5px; color: var(--text-faint);
      font-variant-numeric: tabular-nums; white-space: nowrap;
    }
    .meta-item svg { width: 13.5px; height: 13.5px; color: #556680; flex: 0 0 auto; }
    .page-actions {
      display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 10px;
      flex: 0 1 auto; min-width: 0; max-width: 100%;
    }
    /* Stack the header into two rows (title, then actions) on narrower screens. */
    @media (max-width: 1230px) {
      .page-header { flex-direction: column; align-items: stretch; row-gap: 16px; }
      .title-block { flex: 1 1 auto; }
      .page-actions { justify-content: flex-start; }
    }
    .btn {
      display: inline-flex; align-items: center; gap: 8px;
      height: 38px; padding: 0 13px;
      background: var(--surface); color: var(--text);
      border: 1px solid var(--border); border-radius: 9px;
      font-size: 13px; font-weight: 500; text-decoration: none; cursor: pointer;
      transition: background 0.15s, border-color 0.15s;
    }
    .btn svg { width: 15px; height: 15px; color: var(--text-muted); flex: 0 0 auto; }
    .btn:hover { background: #182338; border-color: #2b3a55; }
    .btn-primary { background: var(--blue); border-color: var(--blue); color: #fff; }
    .btn-primary svg { color: #fff; }
    .btn-primary:hover { background: var(--blue-hover); border-color: var(--blue-hover); }

    /* --- Content grid --- */
    .content-grid {
      display: grid;
      grid-template-columns: minmax(0, 2fr) minmax(0, 1fr);
      gap: 20px;
      align-items: start;
    }
    .col-left > .card + .card, .col-right > .card + .card { margin-top: 20px; }
    @media (max-width: 1080px) {
      .content-grid { grid-template-columns: minmax(0, 1fr); }
    }

    /* --- Cards --- */
    .card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; }
    .card-pad { padding: 22px 24px; }
    .card-header {
      display: flex; align-items: center; justify-content: space-between;
      gap: 12px; flex-wrap: wrap; margin-bottom: 16px;
    }
    .card-header h2 { font-size: 16px; font-weight: 600; line-height: 1.35; }

    /* --- Video + telemetry strip --- */
    .video-shell { position: relative; background: #000; }
    .video-shell video { width: 100%; max-height: 560px; display: block; background: #000; }
    .video-overlay {
      position: absolute; top: 14px; left: 14px; right: 14px;
      display: flex; justify-content: space-between; pointer-events: none;
    }
    .ov-group { display: flex; gap: 8px; flex-wrap: wrap; }
    .ov-chip {
      display: inline-flex; align-items: center; gap: 7px;
      background: #0b1120cc; border: 1px solid #ffffff1a;
      padding: 5px 10px; border-radius: 8px;
      font-size: 12px; font-weight: 600; color: var(--text);
      font-variant-numeric: tabular-nums;
      backdrop-filter: blur(6px);
    }
    .ov-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--green-soft); animation: ov-pulse 1.6s infinite; }
    .ov-chip.paused .ov-dot { background: var(--amber-soft); animation: none; }
    .ov-anomaly { color: var(--red-soft); border-color: #ef444488; }
    .ov-chip[hidden] { display: none !important; }
    @keyframes ov-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }

    .telemetry-strip {
      display: grid; grid-template-columns: repeat(4, minmax(0, 1fr));
      padding: 18px 2px 20px;
      background: var(--surface);
      border-top: 1px solid var(--border-soft);
    }
    .metric { display: flex; align-items: center; gap: 14px; padding: 2px 20px; min-width: 0; }
    .metric + .metric { border-left: 1px solid var(--border-soft); }
    .metric-icon {
      width: 40px; height: 40px; flex: 0 0 40px;
      display: grid; place-items: center;
      border-radius: 10px;
    }
    .metric-icon svg { width: 19px; height: 19px; }
    .icon-hotend { background: #7c2d1226; color: var(--hotend-soft); }
    .icon-bed { background: #0c4a6e26; color: var(--bed); }
    .icon-fan { background: #312e8126; color: var(--purple); }
    .icon-layer { background: #064e3b26; color: var(--green-soft); }
    .metric-content { display: flex; flex-direction: column; justify-content: center; min-width: 0; }
    .metric-label { font-size: 12.5px; color: var(--text-muted); line-height: 1.35; white-space: nowrap; }
    .metric-value {
      font-size: 20px; font-weight: 700; color: var(--text);
      line-height: 1.2; margin-top: 1px;
      font-variant-numeric: tabular-nums; white-space: nowrap;
    }
    .metric-sub { font-size: 12px; color: var(--text-faint); line-height: 1.35; margin-top: 1px; white-space: nowrap; }
    .layer-progress {
      width: 100%; max-width: 96px; height: 5px; margin-top: 6px;
      border-radius: 999px; background: var(--border); overflow: hidden;
    }
    #hud-layer-bar { height: 100%; width: 0%; border-radius: 999px; background: var(--green); transition: width 0.2s; }
    .hud-val-warn { color: var(--red-soft) !important; }

    @media (max-width: 980px) {
      .telemetry-strip { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px 0; }
      .telemetry-strip .metric { border-left: none !important; }
      .telemetry-strip .metric:nth-child(n+3) { border-top: 1px solid var(--border-soft); padding-top: 14px; }
    }

    /* --- Chart --- */
    .chart-legend { display: flex; align-items: center; gap: 16px; font-size: 12px; color: var(--text-muted); flex-wrap: wrap; }
    .lg-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }
    .lg-target { display: inline-block; width: 16px; border-top: 2px dashed var(--text-faint); margin-right: 6px; vertical-align: middle; }
    .chart-select {
      background: var(--surface-2); border: 1px solid var(--border); color: var(--text-muted);
      border-radius: 8px; height: 30px; padding: 0 8px; font-size: 12px; cursor: pointer;
    }
    .chart-select:focus { outline: none; border-color: var(--blue); }
    .chart-body { display: flex; gap: 10px; }
    .chart-yaxis { position: relative; width: 40px; flex: 0 0 40px; height: 300px; }
    .chart-yaxis span {
      position: absolute; right: 0; transform: translateY(-50%);
      font-size: 10.5px; color: var(--text-faint); font-variant-numeric: tabular-nums; line-height: 1;
    }
    .chart-container { position: relative; flex: 1; height: 300px; cursor: crosshair; min-width: 0; }
    .chart-container svg { width: 100%; height: 100%; display: block; }
    .chart-xaxis {
      display: flex; justify-content: space-between; margin-top: 10px; padding-left: 50px;
      font-size: 10.5px; color: var(--text-faint); font-variant-numeric: tabular-nums;
    }
    .chart-hint {
      margin-top: 12px; padding-left: 50px;
      font-size: 11.5px; color: var(--text-faint);
    }
    .chart-hint svg { width: 12px; height: 12px; vertical-align: -1px; margin-right: 5px; }
    @media (max-width: 560px) {
      .chart-yaxis { display: none; }
      .chart-container { height: 224px; }
      .chart-xaxis { padding-left: 0; }
      .chart-hint { padding-left: 0; }
    }

    /* --- Events table --- */
    .events-pill {
      display: inline-flex; align-items: center; gap: 7px;
      padding: 5px 12px; border-radius: 999px;
      font-size: 12px; font-weight: 600;
    }
    .events-pill svg { width: 13px; height: 13px; }
    .pill-critical { background: #7f1d1d33; color: var(--red-soft); }
    .pill-neutral { background: #312e8133; color: var(--purple); }
    .pill-ok { background: #064e3b26; color: var(--green-soft); }
    .events-toggle {
      background: none; border: none; color: var(--blue);
      font-size: 12.5px; font-weight: 600; cursor: pointer; padding: 4px 6px;
      border-radius: 6px;
    }
    .events-toggle:hover { background: #182338; }
    .events-table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
    .events-table th {
      text-align: left; padding: 8px 10px;
      color: var(--text-faint); font-size: 11px; font-weight: 600;
      text-transform: uppercase; letter-spacing: 0.05em;
      border-bottom: 1px solid var(--border);
      white-space: nowrap;
    }
    .events-table td { padding: 11px 10px; border-bottom: 1px solid var(--border-soft); vertical-align: top; }
    .events-table tr:last-child td { border-bottom: none; }
    .event-row { cursor: pointer; transition: background 0.12s; }
    .event-row:hover { background: #ffffff08; }
    .ev-critical td:first-child { box-shadow: inset 3px 0 0 var(--red); }
    .ev-warning td:first-child { box-shadow: inset 3px 0 0 var(--amber); }
    .ev-time { color: var(--text-muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
    .ev-frame { color: var(--text-muted); font-variant-numeric: tabular-nums; }
    .ev-badge {
      display: inline-flex; align-items: center; gap: 6px;
      padding: 3px 10px; border-radius: 999px;
      font-size: 11px; font-weight: 600; white-space: nowrap;
    }
    .ev-dot { width: 5px; height: 5px; border-radius: 50%; background: currentColor; }
    .badge-temp { background: #7f1d1d33; color: var(--red-soft); }
    .badge-speed { background: #1e3a8a44; color: #93c5fd; }
    .badge-print { background: #4c1d9533; color: #c4b5fd; }
    .badge-stall { background: #78350f33; color: var(--amber-soft); }
    .badge-filament { background: #7f1d1d44; color: #fca5a5; }
    .ev-msg { color: var(--text); line-height: 1.45; }
    .events-empty { color: var(--text-faint); text-align: center; padding: 28px 10px !important; }
    .event-extra { display: none; }
    .events-expanded .event-extra { display: table-row; }
    @media (max-width: 640px) {
      .events-table th:nth-child(2), .events-table td:nth-child(2) { display: none; }
      .ev-badge { white-space: normal; }
    }

    /* --- Right column: job status --- */
    .status-hero {
      display: flex; align-items: center; gap: 16px;
      padding: 20px; margin-bottom: 22px;
      background: var(--surface-2); border: 1px solid var(--border-soft); border-radius: 12px;
    }
    .status-hero-icon {
      width: 48px; height: 48px; flex: 0 0 48px;
      display: grid; place-items: center; border-radius: 50%;
      background: #10b98118; color: var(--green-soft);
    }
    .status-hero-icon svg { width: 22px; height: 22px; }
    .status-hero-icon.warn { background: #f59e0b18; color: var(--amber-soft); }
    .status-hero-icon.bad { background: #ef444418; color: var(--red-soft); }
    .status-hero-name { font-size: 17px; font-weight: 700; line-height: 1.3; }
    .status-hero-sub { font-size: 13px; color: var(--text-muted); line-height: 1.4; }
    .kv-list { display: flex; flex-direction: column; }
    .kv-row {
      display: flex; justify-content: space-between; align-items: baseline; gap: 16px;
      padding: 11px 0; border-bottom: 1px solid var(--border-soft); font-size: 13px;
    }
    .kv-row:last-child { border-bottom: none; }
    .kv-label {
      display: inline-flex; align-items: center; gap: 8px;
      color: var(--text-muted); flex: 0 0 auto; line-height: 1.4;
    }
    .kv-label svg { width: 14px; height: 14px; color: var(--text-faint); flex: 0 0 auto; }
    .kv-value {
      color: var(--text); font-weight: 600; text-align: right;
      line-height: 1.4; min-width: 0; overflow-wrap: break-word;
    }
    .kv-code {
      font-size: 12px; color: var(--text); background: var(--surface-2);
      border: 1px solid var(--border-soft); padding: 2px 7px; border-radius: 6px;
      overflow-wrap: break-word;
    }

    /* --- Key metrics --- */
    .km-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
    .km {
      display: flex; gap: 12px; align-items: flex-start;
      padding: 16px;
      background: var(--surface-2); border: 1px solid var(--border-soft); border-radius: 10px;
      min-width: 0;
    }
    .km-icon {
      width: 34px; height: 34px; flex: 0 0 34px;
      display: grid; place-items: center; border-radius: 9px;
    }
    .km-icon svg { width: 16px; height: 16px; }
    .km-hotend { background: #7c2d1226; color: var(--hotend-soft); }
    .km-bed { background: #0c4a6e26; color: var(--bed); }
    .km-stab { background: #064e3b26; color: var(--green-soft); }
    .km-frames { background: #312e8126; color: var(--purple); }
    .km-interval { background: #1e3a8a33; color: #93c5fd; }
    .km-missed { background: #7f1d1d26; color: var(--red-soft); }
    .km-content { display: flex; flex-direction: column; min-width: 0; }
    .km-label { font-size: 12px; color: var(--text-muted); line-height: 1.35; }
    .km-value {
      font-size: 15px; font-weight: 700; color: var(--text);
      line-height: 1.35; margin-top: 3px;
      font-variant-numeric: tabular-nums; overflow-wrap: break-word;
    }
    .km-sub { font-size: 11.5px; color: var(--text-faint); line-height: 1.35; margin-top: 3px; }
    @media (max-width: 460px) {
      .km-grid { grid-template-columns: minmax(0, 1fr); }
    }

    /* --- Jump to layer --- */
    .layer-grid {
      display: grid; grid-template-columns: repeat(auto-fill, minmax(52px, 1fr)); gap: 10px;
    }
    .layer-chip {
      height: 38px; display: grid; place-items: center;
      background: var(--surface-2); border: 1px solid var(--border); border-radius: 8px;
      color: var(--text-muted); font-size: 12.5px; font-weight: 600;
      font-variant-numeric: tabular-nums; cursor: pointer;
      transition: background 0.15s, border-color 0.15s, color 0.15s;
    }
    .layer-chip:hover { background: var(--blue); border-color: var(--blue); color: #fff; }
    .layers-empty { color: var(--text-faint); font-size: 13px; padding: 4px 0 2px; }
    .layer-go { display: flex; gap: 10px; margin-top: 18px; }
    .layer-go input {
      flex: 1; min-width: 0; height: 40px; padding: 0 12px;
      background: var(--surface-2); border: 1px solid var(--border); border-radius: 8px;
      color: var(--text); font-size: 13px; font-variant-numeric: tabular-nums;
    }
    .layer-go input::placeholder { color: var(--text-faint); }
    .layer-go input:focus { outline: none; border-color: var(--blue); }
    .layer-go button {
      height: 40px; padding: 0 18px; flex: 0 0 auto;
      background: var(--blue); border: none; border-radius: 8px;
      color: #fff; font-size: 13px; font-weight: 600; cursor: pointer;
      transition: background 0.15s;
    }
    .layer-go button:hover { background: var(--blue-hover); }

    @media (max-width: 720px) {
      .page { padding: 18px 16px 44px; }
      .title-row h1 { font-size: 22px; }
      .page-header { margin-bottom: 22px; }
      .meta-item { font-size: 12px; }
      .page-actions .btn { height: 36px; padding: 0 12px; font-size: 12.5px; }
      .card-pad { padding: 18px; }
    }
  </style>
</head>
<body>
  <div class="page">
    <a class="back-link" href="$gallery_url">$back_icon All Jobs</a>

    <header class="page-header">
      <div class="title-block">
        <div class="title-row">
          <h1>Print Timelapse &amp; Telemetry</h1>
          <span class="status-badge $status_badge_class">$status_icon $status_label</span>
        </div>
        <p class="subtitle"><strong>$printer</strong> &nbsp;&middot;&nbsp; Job: <strong>$short_job_id</strong></p>
        <div class="meta-row">
          <span class="meta-item">$icon_calendar $meta_date &middot; $meta_time</span>
          <span class="meta-item">$icon_clock $meta_duration</span>
          <span class="meta-item">$icon_layers $meta_layers</span>
          <span class="meta-item">$icon_film $meta_ftype</span>
          <span class="meta-item">$icon_file $meta_size</span>
        </div>
      </div>
      <div class="page-actions">
        <a href="$gallery_url" class="btn">$icon_image Gallery</a>
        <a href="$meta_url" target="_blank" class="btn">$icon_braces Frames JSON</a>
        <a href="$corr_url" target="_blank" class="btn">$icon_activity Correlation JSON</a>
        <a href="$video_url" download class="btn btn-primary">$icon_download Download MP4</a>
        $layers_btn
      </div>
    </header>

    <main class="content-grid">
      <!-- ================= LEFT COLUMN ================= -->
      <section class="col-left">

        <!-- Video player + live telemetry summary -->
        <div class="card video-card">
          <div class="video-shell">
            <video id="tl-video" controls autoplay loop playsinline>
              <source src="$video_url" type="video/mp4">
              Your browser does not support HTML5 video playback.
            </video>
            <div class="video-overlay">
              <div class="ov-group">
                <span class="ov-chip" id="ov-timestamp">&mdash;</span>
                <span class="ov-chip" id="ov-state"><span class="ov-dot"></span><span id="ov-state-label">LIVE</span></span>
              </div>
            </div>
            <div class="video-overlay" style="top:52px; justify-content:flex-start;">
              <div class="ov-group">
                <span class="ov-chip ov-anomaly" id="ov-anomaly" hidden>$icon_alert ANOMALY</span>
              </div>
            </div>
          </div>

          <div class="telemetry-strip" id="telemetry-strip">
            <div class="metric" id="metric-nozzle">
              <div class="metric-icon icon-hotend">$icon_thermo</div>
              <div class="metric-content">
                <span class="metric-label">Hotend</span>
                <span class="metric-value" id="hud-nozzle">&mdash;</span>
                <span class="metric-sub">/ <span id="hud-nozzle-target">&mdash;</span>&deg;C target</span>
              </div>
            </div>
            <div class="metric" id="metric-bed">
              <div class="metric-icon icon-bed">$icon_bed</div>
              <div class="metric-content">
                <span class="metric-label">Bed</span>
                <span class="metric-value" id="hud-bed">&mdash;</span>
                <span class="metric-sub">/ <span id="hud-bed-target">&mdash;</span>&deg;C target</span>
              </div>
            </div>
            <div class="metric" id="metric-fan">
              <div class="metric-icon icon-fan">$icon_fan</div>
              <div class="metric-content">
                <span class="metric-label">Fan</span>
                <span class="metric-value" id="hud-fan">&mdash;</span>
                <span class="metric-sub">Cooling fan</span>
              </div>
            </div>
            <div class="metric" id="metric-layer">
              <div class="metric-icon icon-layer">$icon_layers</div>
              <div class="metric-content">
                <span class="metric-label">Layer</span>
                <span class="metric-value" id="hud-layer">&mdash;</span>
                <div class="layer-progress"><div id="hud-layer-bar"></div></div>
              </div>
            </div>
          </div>
        </div>

        <!-- Temperature timeline -->
        <div class="card chart-card card-pad">
          <div class="card-header">
            <h2>Temperature Timeline</h2>
            <div class="chart-legend">
              <span><span class="lg-dot" style="background: var(--hotend);"></span>Hotend</span>
              <span><span class="lg-dot" style="background: var(--bed);"></span>Bed</span>
              <span><span class="lg-target"></span>Target</span>
              <select id="chart-range" class="chart-select" title="Chart time range">
                <option value="0">Full Job</option>
                <option value="0.5">Second Half</option>
                <option value="0.75">Last Quarter</option>
              </select>
            </div>
          </div>
          <div class="chart-body">
            <div class="chart-yaxis">$chart_yaxis</div>
            <div class="chart-container" id="chart-container">
              <svg id="timeline-svg" preserveAspectRatio="none" viewBox="0 0 1000 100">
                $chart_grid
                <g id="svg-anomalies"></g>
                <polyline id="svg-nozzle-target" fill="none" stroke="#64748b" stroke-dasharray="3 3" stroke-width="1"/>
                <polyline id="svg-bed-line" fill="none" stroke="#38bdf8" stroke-width="1.6"/>
                <polyline id="svg-nozzle-line" fill="none" stroke="#f97316" stroke-width="1.6"/>
                <line id="svg-scrub-line" x1="0" y1="0" x2="0" y2="100" stroke="#f1f5f9" stroke-width="1.2" opacity="0.85"/>
              </svg>
            </div>
          </div>
          <div class="chart-xaxis" id="chart-xaxis"></div>
          <p class="chart-hint">$icon_clock Click the timeline to scrub the video.</p>
        </div>

        <!-- Telemetry events -->
        <div class="card events-card card-pad" id="events-card">
          <div class="card-header">
            <h2>Telemetry Events</h2>
            <div style="display:flex; align-items:center; gap:12px; flex-wrap:wrap;">
              $events_pill
              $events_view_all
            </div>
          </div>
          <div style="overflow-x:auto;">
            <table class="events-table">
              <thead>
                <tr><th>Time</th><th>Frame</th><th>Type</th><th>Message</th></tr>
              </thead>
              <tbody id="events-body">
                $events_rows
              </tbody>
            </table>
          </div>
        </div>

      </section>

      <!-- ================= RIGHT COLUMN ================= -->
      <aside class="col-right">

        <!-- Job status -->
        <div class="card status-card card-pad">
          <div class="card-header" style="margin-bottom:18px;"><h2>Job Status</h2></div>
          <div class="status-hero">
            <div class="status-hero-icon $status_hero_icon_class">$status_icon_hero</div>
            <div>
              <div class="status-hero-name">$status_hero_label</div>
              <div class="status-hero-sub">$status_hero_sub</div>
            </div>
          </div>
          <div class="kv-list">$job_rows</div>
        </div>

        <!-- Key metrics -->
        <div class="card metrics-card card-pad">
          <div class="card-header"><h2>Key Metrics</h2></div>
          <div class="km-grid">$key_metrics</div>
        </div>

        <!-- Jump to layer -->
        <div class="card layers-card card-pad">
          <div class="card-header"><h2>Jump to Layer</h2></div>
          <div class="layer-grid">$layer_chips</div>
          $layers_empty
          <form class="layer-go" id="layer-go-form">
            <input id="layer-input" type="number" min="1" placeholder="Go to layer&hellip;" aria-label="Layer number">
            <button type="submit">Go</button>
          </form>
        </div>

      </aside>
    </main>
  </div>

  <script id="correlation-data" type="application/json">
  $report_json
  </script>

  <script>
    const reportData = JSON.parse(document.getElementById('correlation-data').textContent);
    const video = document.getElementById('tl-video');
    const scrubLine = document.getElementById('svg-scrub-line');
    const chartContainer = document.getElementById('chart-container');
    const rangeSelect = document.getElementById('chart-range');
    const xaxis = document.getElementById('chart-xaxis');
    const anomG = document.getElementById('svg-anomalies');

    const hudNozzle = document.getElementById('hud-nozzle');
    const hudNozzleTarget = document.getElementById('hud-nozzle-target');
    const hudBed = document.getElementById('hud-bed');
    const hudBedTarget = document.getElementById('hud-bed-target');
    const hudFan = document.getElementById('hud-fan');
    const hudLayer = document.getElementById('hud-layer');
    const hudLayerBar = document.getElementById('hud-layer-bar');
    const ovTimestamp = document.getElementById('ov-timestamp');
    const ovStateChip = document.getElementById('ov-state');
    const ovStateLabel = document.getElementById('ov-state-label');
    const ovAnomaly = document.getElementById('ov-anomaly');

    const timeline = reportData.timeline || [];
    const anomalies = reportData.anomalies || [];
    const totalFrames = reportData.total_frames || timeline.length;
    const fallbackDuration = reportData.video_duration_seconds || 1;
    const MAX_LAYER = $max_layer || 0;
    const CHART_H = 100;
    const MAX_TEMP = 280;

    let fromRatio = 0;

    function tempY(t) { return CHART_H - (t / MAX_TEMP) * CHART_H; }

    function fmtVideoTime(sec) {
      sec = Math.max(0, Math.floor(sec));
      const h = Math.floor(sec / 3600);
      const m = Math.floor((sec % 3600) / 60);
      const s = sec % 60;
      const mm = (h > 0) ? String(m).padStart(2, '0') : String(m);
      const ss = String(s).padStart(2, '0');
      return (h > 0) ? h + ':' + mm + ':' + ss : mm + ':' + ss;
    }

    // --- Temperature chart ---
    function clearPolyline(id) { document.getElementById(id).setAttribute('points', ''); }

    function drawChart() {
      fromRatio = parseFloat(rangeSelect.value) || 0;
      const from = Math.floor(timeline.length * fromRatio);
      const slice = timeline.slice(from);

      if (slice.length < 2) {
        clearPolyline('svg-nozzle-line');
        clearPolyline('svg-bed-line');
        clearPolyline('svg-nozzle-target');
        anomG.innerHTML = '';
        return;
      }

      const n = slice.length - 1;
      const ptsNoz = [];
      const ptsBed = [];
      const ptsTar = [];
      slice.forEach((pt, i) => {
        const x = (i / n) * 1000;
        if (pt.nozzle_temp != null) ptsNoz.push(x.toFixed(1) + ',' + tempY(pt.nozzle_temp).toFixed(1));
        if (pt.bed_temp != null) ptsBed.push(x.toFixed(1) + ',' + tempY(pt.bed_temp).toFixed(1));
        if (pt.nozzle_target != null) ptsTar.push(x.toFixed(1) + ',' + tempY(pt.nozzle_target).toFixed(1));
      });

      document.getElementById('svg-nozzle-line').setAttribute('points', ptsNoz.join(' '));
      document.getElementById('svg-bed-line').setAttribute('points', ptsBed.join(' '));
      document.getElementById('svg-nozzle-target').setAttribute('points', ptsTar.join(' '));

      // Anomaly bands clamped to the visible span
      const t0 = slice[0].video_time_seconds || 0;
      const t1 = slice[n].video_time_seconds || t0 + 1;
      anomG.innerHTML = '';
      anomalies.forEach((anom) => {
        const start = anom.video_time_start || 0;
        const end = (anom.video_time_end != null) ? anom.video_time_end : start;
        const clampS = Math.max(start, t0);
        const clampE = Math.min(end, t1);
        if (clampE <= clampS) return;
        const x1 = ((clampS - t0) / (t1 - t0)) * 1000;
        const x2 = ((clampE - t0) / (t1 - t0)) * 1000;
        const width = Math.max(4, x2 - x1);
        const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
        rect.setAttribute('x', x1.toFixed(1));
        rect.setAttribute('y', '0');
        rect.setAttribute('width', width.toFixed(1));
        rect.setAttribute('height', String(CHART_H));
        rect.setAttribute('fill', anom.severity === 'critical' ? 'rgba(239, 68, 68, 0.28)' : 'rgba(245, 158, 11, 0.22)');
        anomG.appendChild(rect);
      });

      // X-axis labels across the visible span
      if (xaxis) {
        xaxis.innerHTML = '';
        for (let i = 0; i <= 4; i++) {
          const span = document.createElement('span');
          span.textContent = fmtVideoTime(t0 + ((t1 - t0) * i) / 4);
          xaxis.appendChild(span);
        }
      }
    }

    drawChart();
    rangeSelect.addEventListener('change', drawChart);

    // Click the chart to scrub the video
    chartContainer.addEventListener('click', (e) => {
      const rect = chartContainer.getBoundingClientRect();
      const clickRatio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
      const dur = video.duration || fallbackDuration;
      seekTo((fromRatio + clickRatio * (1 - fromRatio)) * dur);
    });

    function seekTo(seconds) {
      video.currentTime = Math.max(0, seconds);
      video.play();
    }

    // --- Event rows: click to seek ---
    document.querySelectorAll('.event-row[data-seek]').forEach((row) => {
      row.addEventListener('click', () => seekTo(parseFloat(row.dataset.seek)));
    });

    // --- View all events toggle ---
    const eventsToggle = document.getElementById('events-toggle');
    if (eventsToggle) {
      eventsToggle.addEventListener('click', () => {
        const card = document.getElementById('events-card');
        const expanded = card.classList.toggle('events-expanded');
        eventsToggle.innerHTML = expanded ? 'Show Less' : 'View All &rarr;';
      });
    }

    // --- Layer navigation ---
    // Visible chips are representative milestones only; every sampled layer is
    // still reachable through the Go-to-layer field below.
    const layerChips = Array.from(document.querySelectorAll('.layer-chip[data-layer]'));
    layerChips.forEach((chip) => {
      chip.addEventListener('click', () => seekTo(parseFloat(chip.dataset.seek)));
    });

    const layerTimes = {};
    (reportData.layers || []).forEach((lyr) => {
      if (lyr && lyr.layer != null && lyr.video_time_seconds != null) layerTimes[lyr.layer] = lyr.video_time_seconds;
    });

    document.getElementById('layer-go-form').addEventListener('submit', (e) => {
      e.preventDefault();
      const input = document.getElementById('layer-input');
      const wanted = parseInt(input.value, 10);
      const nums = Object.keys(layerTimes).map(Number).sort((a, b) => a - b);
      if (!wanted || !nums.length) return;
      let targetTime = null;
      if (layerTimes[wanted] != null) {
        targetTime = layerTimes[wanted];
      } else {
        // Snap to the nearest layer that actually started (prefer the one below).
        let below = null;
        let above = null;
        for (const n of nums) {
          if (n <= wanted) below = n;
          if (n >= wanted && above == null) above = n;
        }
        const best = (below != null && above != null)
          ? ((wanted - below <= above - wanted) ? below : above)
          : (below != null ? below : above);
        if (best != null) targetTime = layerTimes[best];
      }
      if (targetTime != null) seekTo(targetTime);
      input.value = '';
    });

    // --- Live telemetry tiles + overlays during playback ---
    function videoDuration() { return video.duration || fallbackDuration || 1; }

    function updateStateChip() {
      if (video.paused) {
        ovStateChip.classList.add('paused');
        ovStateLabel.textContent = 'PAUSED';
      } else {
        ovStateChip.classList.remove('paused');
        ovStateLabel.textContent = 'LIVE';
      }
    }
    video.addEventListener('play', updateStateChip);
    video.addEventListener('pause', updateStateChip);
    updateStateChip();

    video.addEventListener('timeupdate', () => {
      const ratio = Math.min(1, Math.max(0, video.currentTime / videoDuration()));

      // Scrub line
      const x = (ratio * 1000).toFixed(1);
      scrubLine.setAttribute('x1', x);
      scrubLine.setAttribute('x2', x);

      if (!timeline.length) return;
      const idx = Math.min(timeline.length - 1, Math.floor(ratio * timeline.length));
      const pt = timeline[idx];
      if (!pt) return;

      // Hotend
      hudNozzle.textContent = (pt.nozzle_temp != null) ? pt.nozzle_temp + '\u00B0C' : '\u2014';
      hudNozzleTarget.textContent = (pt.nozzle_target != null) ? pt.nozzle_target : '\u2014';
      if (pt.nozzle_temp != null && pt.nozzle_target != null && pt.nozzle_target >= 100 && (pt.nozzle_target - pt.nozzle_temp) >= 10) {
        hudNozzle.classList.add('hud-val-warn');
      } else {
        hudNozzle.classList.remove('hud-val-warn');
      }

      // Bed
      hudBed.textContent = (pt.bed_temp != null) ? pt.bed_temp + '\u00B0C' : '\u2014';
      hudBedTarget.textContent = (pt.bed_target != null) ? pt.bed_target : '\u2014';

      // Fan
      hudFan.textContent = (pt.fan_speed != null) ? pt.fan_speed + '%' : '\u2014';

      // Layer + progress bar
      const layerNum = (pt.layer != null && pt.layer > 0) ? pt.layer : null;
      hudLayer.textContent = (layerNum != null) ? layerNum + ' / ' + (MAX_LAYER || layerNum) : '\u2014';
      const progress = (pt.progress != null && pt.progress > 0) ? pt.progress : (layerNum && MAX_LAYER ? (layerNum / MAX_LAYER) * 100 : null);
      hudLayerBar.style.width = ((progress != null) ? Math.min(100, progress).toFixed(1) : 0) + '%';

      // Frame timestamp overlay (UTC as recorded)
      ovTimestamp.textContent = pt.timestamp ? String(pt.timestamp).slice(0, 19).replace('T', ' ') + ' UTC' : '\u2014';

      // Anomaly overlay
      if (pt.anomaly_ids && pt.anomaly_ids.length > 0) {
        ovAnomaly.hidden = false;
        ovAnomaly.title = 'Anomaly at this moment: ' + pt.anomaly_ids.join(', ');
      } else {
        ovAnomaly.hidden = true;
      }
    });
  </script>
</body>
</html>
""")


# --- Timelapse gallery page: full HTML template ---
# NOTE: placeholders are substituted with Template.substitute(); JS/CSS must not
# contain bare "$<word>" sequences.

_TL_GALLERY_PAGE = Template("""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>$doc_title</title>
  <style>
    :root {
      --bg: #0b1120;
      --surface: #111a2e;
      --surface-2: #0e1626;
      --border: #1e293b;
      --border-soft: #182236;
      --text: #f1f5f9;
      --text-muted: #94a3b8;
      --text-faint: #64748b;
      --blue: #3b82f6;
      --blue-hover: #2563eb;
      --green: #10b981;
      --green-soft: #34d399;
      --red: #ef4444;
      --red-soft: #f87171;
      --amber: #f59e0b;
      --amber-soft: #fbbf24;
      --radius: 14px;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    [hidden] { display: none !important; }
    html { color-scheme: dark; }
    body {
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      -webkit-font-smoothing: antialiased;
      line-height: 1.5;
    }
    code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    :focus-visible { outline: 2px solid var(--blue); outline-offset: 2px; }
    .page { max-width: 1400px; margin: 0 auto; padding: 30px 32px 64px; }

    /* --- Header --- */
    .tl-head { display: flex; align-items: flex-start; justify-content: space-between; column-gap: 32px; row-gap: 16px; flex-wrap: wrap; margin: 2px 0 24px; }
    .tl-head h1 { font-size: 30px; font-weight: 700; letter-spacing: -0.02em; line-height: 1.2; }
    .tl-head-sub { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; margin-top: 8px; font-size: 15px; color: var(--text-muted); }
    .tl-head-sub strong { color: var(--text); font-weight: 600; }
    .tl-head-sep { color: #3c4c66; }
    @media (max-width: 720px) {
      .api-status { margin-left: auto; }
    }
    .api-status {
      display: inline-flex; align-items: center; gap: 8px;
      flex: 0 0 auto; margin-top: 2px;
      padding: 7px 13px; border-radius: 999px;
      background: var(--surface); border: 1px solid var(--border);
      color: var(--text-muted); font-size: 12.5px; font-weight: 600;
      text-decoration: none; line-height: 1.4;
      transition: border-color 0.15s, color 0.15s;
    }
    .api-status:hover { color: var(--text); border-color: #2b3a55; }
    .api-status .api-dot {
      width: 8px; height: 8px; border-radius: 50%;
      background: var(--green); flex: 0 0 auto;
      animation: api-pulse 2.4s ease-in-out infinite;
    }
    @keyframes api-pulse { 0%, 100% { box-shadow: 0 0 0 0 rgba(16,185,129,0.35); } 50% { box-shadow: 0 0 0 5px rgba(16,185,129,0); } }

    /* --- Toolbar --- */
    .tl-toolbar {
      display: flex; flex-direction: column; gap: 12px;
      background: var(--surface); border: 1px solid var(--border);
      border-radius: 12px; padding: 14px 16px; margin-bottom: 22px;
    }
    .tl-search { position: relative; }
    .tl-search > svg {
      position: absolute; left: 13px; top: 50%; transform: translateY(-50%);
      width: 16px; height: 16px; color: var(--text-faint); pointer-events: none;
    }
    .tl-search input {
      width: 100%; height: 42px; padding: 0 16px 0 40px;
      background: var(--surface-2); color: var(--text);
      border: 1px solid var(--border-soft); border-radius: 9px;
      font-size: 13.5px; font-family: inherit;
    }
    .tl-search input::placeholder { color: var(--text-faint); }
    .tl-search input:focus { outline: none; border-color: var(--blue); }
    .tl-filters { display: flex; flex-wrap: wrap; gap: 10px 12px; }
    .tl-field { display: flex; flex-direction: column; gap: 4px; flex: 1 1 0; min-width: 130px; }
    .tl-field > span {
      padding-left: 5px; font-size: 10.5px; font-weight: 600;
      text-transform: uppercase; letter-spacing: 0.07em; color: var(--text-faint);
    }
    .tl-field select {
      height: 42px; padding: 0 30px 0 12px;
      appearance: none; -webkit-appearance: none;
      background-color: var(--surface-2); color: var(--text);
      border: 1px solid var(--border-soft); border-radius: 9px;
      font-size: 13px; font-weight: 500; font-family: inherit; cursor: pointer;
      background-image: url('data:image/svg+xml;charset=utf-8,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 24 24%22 fill=%22none%22 stroke=%22%2364748b%22 stroke-width=%222%22 stroke-linecap=%22round%22 stroke-linejoin=%22round%22%3E%3Cpath d=%22m6 9 6 6 6-6%22/%3E%3C/svg%3E');
      background-repeat: no-repeat; background-position: right 11px center; background-size: 14px;
    }
    .tl-field select:focus { outline: none; border-color: var(--blue); }
    .tl-field select option { background: #0e1626; color: var(--text); }

    /* --- Count row + view toggle --- */
    .tl-subrow { display: flex; align-items: center; justify-content: space-between; gap: 14px; margin-bottom: 16px; }
    .tl-count { font-size: 14.5px; font-weight: 600; color: #cbd5e1; font-variant-numeric: tabular-nums; }
    .tl-view { display: flex; gap: 2px; padding: 3px; background: var(--surface); border: 1px solid var(--border); border-radius: 9px; }
    .tl-view button {
      display: inline-flex; align-items: center; gap: 6px;
      height: 30px; padding: 0 11px; border: 0; border-radius: 6px;
      background: transparent; color: var(--text-faint);
      font-size: 12.5px; font-weight: 600; font-family: inherit; cursor: pointer;
    }
    .tl-view button svg { width: 14px; height: 14px; }
    .tl-view button:hover { color: var(--text); }
    .tl-view button[aria-pressed="true"] { background: #182338; color: #e2e8f0; }

    /* --- Card grid --- */
    .tl-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 22px; }
    @media (max-width: 899px) { .tl-grid { grid-template-columns: minmax(0, 1fr); } }

    /* --- Media card --- */
    .tl-card {
      display: flex; flex-direction: column; min-width: 0;
      background: var(--surface); border: 1px solid var(--border);
      border-radius: var(--radius); overflow: hidden;
      transition: border-color 0.15s;
    }
    .tl-card:hover { border-color: #2b3a55; }
    .tl-card.tl-hide { display: none !important; }

    .tl-media {
      position: relative; display: block; aspect-ratio: 16 / 9;
      overflow: hidden; background: #05080f;
      border-bottom: 1px solid var(--border-soft);
      text-decoration: none;
    }
    .tl-frame, .tl-video { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
    .tl-video { display: none; }
    .tl-media.poster-video .tl-video,
    .tl-media.has-frame.uses-video .tl-video { display: block; }
    .tl-media.has-frame.img-dead .tl-frame { display: none; }
    .tl-ph {
      display: none; flex-direction: column; align-items: center; justify-content: center; gap: 12px;
      position: absolute; inset: 0;
      background: linear-gradient(180deg, #0a1122, #0d1526);
    }
    .tl-ph-ic {
      width: 56px; height: 56px; border-radius: 50%; display: grid; place-items: center;
      background: #0e1626; border: 1px dashed #2a3a55; color: #56637a;
    }
    .tl-ph-ic svg { width: 24px; height: 24px; }
    .tl-ph-txt { font-size: 12.5px; color: var(--text-faint); }
    .tl-media.no-media .tl-ph { display: flex; }

    .tl-media-top {
      position: absolute; top: 10px; left: 10px; right: 10px;
      display: flex; align-items: flex-start; justify-content: space-between; gap: 10px;
      pointer-events: none;
    }
    .tl-chips { display: flex; flex-wrap: wrap; gap: 8px; min-width: 0; }
    .tl-chip {
      display: inline-flex; align-items: center; gap: 7px; max-width: 100%;
      background: rgba(5, 9, 18, 0.72); border: 1px solid rgba(255, 255, 255, 0.14);
      color: #e2e8f0; border-radius: 8px; padding: 5px 10px;
      font-size: 12px; font-weight: 600; line-height: 1.3;
      backdrop-filter: blur(6px); -webkit-backdrop-filter: blur(6px);
      font-variant-numeric: tabular-nums;
    }
    .tl-chip svg { width: 13px; height: 13px; color: #8fa1bb; flex: 0 0 auto; }
    .tl-pill {
      display: inline-flex; align-items: center; gap: 6px; flex: 0 0 auto;
      border-radius: 999px; padding: 5px 11px;
      font-size: 11.5px; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase;
      border: 1px solid transparent; white-space: nowrap;
    }
    .tl-pill .tl-dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
    .tl-pill-ok { background: rgba(16, 185, 129, 0.13); color: var(--green-soft); border-color: rgba(16, 185, 129, 0.38); }
    .tl-pill-busy { background: rgba(245, 158, 11, 0.13); color: var(--amber-soft); border-color: rgba(245, 158, 11, 0.38); }
    .tl-pill-busy .tl-dot { animation: api-pulse 1.6s ease-in-out infinite; }
    .tl-pill-bad { background: rgba(239, 68, 68, 0.13); color: var(--red-soft); border-color: rgba(239, 68, 68, 0.38); }
    .tl-pill-idle { background: rgba(100, 116, 139, 0.14); color: #a7b3c4; border-color: rgba(148, 163, 184, 0.28); }

    .tl-play {
      position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
      width: 64px; height: 64px; border-radius: 50%;
      display: grid; place-items: center;
      background: rgba(9, 14, 26, 0.62); border: 1px solid rgba(255, 255, 255, 0.3);
      color: #fff;
      backdrop-filter: blur(5px); -webkit-backdrop-filter: blur(5px);
      box-shadow: 0 8px 22px rgba(0, 0, 0, 0.4);
      transition: transform 0.15s ease, background 0.15s ease, border-color 0.15s ease;
    }
    .tl-play svg { width: 26px; height: 26px; margin-left: 3px; }
    .tl-media:hover .tl-play { transform: translate(-50%, -50%) scale(1.07); background: var(--blue); border-color: var(--blue); }
    .tl-media:hover { border-bottom-color: rgba(59, 130, 246, 0.55); }

    /* Card body */
    .tl-body { flex: 1; display: flex; flex-direction: column; padding: 18px 20px 6px; min-width: 0; }
    .tl-title {
      font-size: 19px; font-weight: 600; letter-spacing: -0.01em; line-height: 1.3;
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }
    .tl-jobline {
      display: flex; align-items: center; gap: 8px; margin-top: 3px; min-width: 0;
      font-size: 12.5px; color: var(--text-faint);
    }
    .tl-jobline svg { width: 13px; height: 13px; flex: 0 0 auto; }
    .tl-jobline-lbl {
      display: inline-flex; align-items: center; gap: 5px; flex: 0 0 auto;
      color: var(--text-muted); font-weight: 600;
      font-size: 10.5px; letter-spacing: 0.06em; text-transform: uppercase;
    }
    .tl-jobline-lbl::after { content: ""; width: 3px; height: 3px; border-radius: 50%; background: #3c4c66; }
    .tl-jobline .tl-ell {
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
      color: var(--text-muted); font-weight: 500;
    }
    .tl-meta {
      display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 13px 24px; margin-top: auto; padding-top: 16px;
      border-top: 1px solid var(--border-soft);
    }
    .tl-cell { min-width: 0; }
    .tl-cell-label {
      display: flex; align-items: center; gap: 6px;
      font-size: 11px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase;
      color: var(--text-faint);
    }
    .tl-cell-label svg { width: 13px; height: 13px; color: #556680; flex: 0 0 auto; }
    .tl-cell-value {
      display: block; margin-top: 3px;
      font-size: 13.5px; font-weight: 500; color: var(--text); line-height: 1.35;
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
      font-variant-numeric: tabular-nums;
    }

    /* Buttons + actions */
    .btn {
      display: inline-flex; align-items: center; justify-content: center; gap: 8px;
      height: 42px; padding: 0 15px; border-radius: 9px;
      background: var(--surface); color: var(--text);
      border: 1px solid var(--border); font-size: 13.5px; font-weight: 600;
      font-family: inherit; text-decoration: none; cursor: pointer; line-height: 1;
      transition: background 0.15s, border-color 0.15s;
    }
    .btn svg { width: 16px; height: 16px; color: var(--text-muted); flex: 0 0 auto; }
    .btn:hover { background: #182338; border-color: #2b3a55; }
    .btn-primary { background: var(--blue); border-color: var(--blue); color: #fff; }
    .btn-primary svg { color: #fff; }
    .btn-primary:hover { background: var(--blue-hover); border-color: var(--blue-hover); }
    .tl-actions {
      display: flex; align-items: center; gap: 10px;
      padding: 14px 20px 20px; border-top: 1px solid var(--border-soft);
    }
    .tl-watch { flex: 1 1 auto; min-width: 0; height: 46px; font-size: 14px; }
    .tl-watch svg { width: 17px; height: 17px; }
    .tl-iconbtn { width: 46px; height: 46px; padding: 0; flex: 0 0 auto; }
    .tl-pop { position: relative; flex: 0 0 auto; }
    .tl-menu {
      position: absolute; right: 0; bottom: calc(100% + 8px); z-index: 40;
      min-width: 232px; padding: 6px;
      background: #0e1626; border: 1px solid #26334d; border-radius: 10px;
      box-shadow: 0 16px 40px rgba(0, 0, 0, 0.5);
    }
    .tl-menu-cap {
      padding: 7px 11px 9px; margin-bottom: 5px;
      border-bottom: 1px solid var(--border-soft);
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 10.5px; color: var(--text-faint);
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }
    .tl-menu a {
      display: flex; align-items: center; gap: 9px;
      padding: 9px 11px; border-radius: 7px;
      color: #cbd5e1; font-size: 13px; font-weight: 500; text-decoration: none;
    }
    .tl-menu a:hover { background: #182338; color: var(--text); }
    .tl-menu a svg { width: 15px; height: 15px; color: var(--text-faint); flex: 0 0 auto; }

    /* List view (>= 900px) */
    @media (min-width: 900px) {
      .tl-grid.list { grid-template-columns: minmax(0, 1fr); gap: 16px; }
      .tl-grid.list .tl-card { display: grid; grid-template-columns: minmax(300px, 420px) minmax(0, 1fr); }
      .tl-grid.list .tl-media { aspect-ratio: auto; border-bottom: none; border-right: 1px solid var(--border-soft); }
      .tl-grid.list .tl-media { min-height: 100%; }
      .tl-grid.list .tl-frame, .tl-grid.list .tl-video { position: absolute; }
      .tl-grid.list .tl-body { padding: 20px 26px 8px; }
      .tl-grid.list .tl-meta { grid-template-columns: repeat(3, minmax(0, 1fr)); margin-top: auto; }
      .tl-grid.list .tl-actions {
        grid-column: 1 / -1; flex-wrap: wrap;
        border-top: 1px solid var(--border-soft); padding: 14px 26px 18px;
      }
      .tl-grid.list .tl-watch { flex: 0 1 320px; }
    }

    /* States (empty / error / no-results) */
    .tl-state {
      display: flex; flex-direction: column; align-items: center; justify-content: center;
      text-align: center; gap: 4px; padding: 84px 24px;
      background: var(--surface); border: 1px dashed #26334d; border-radius: 16px;
      margin-top: 6px;
    }
    .tl-state-ic {
      width: 66px; height: 66px; border-radius: 50%; display: grid; place-items: center;
      background: var(--surface-2); border: 1px solid var(--border-soft); color: #4b5a72;
      margin-bottom: 14px;
    }
    .tl-state-ic svg { width: 27px; height: 27px; }
    .tl-state h2 { font-size: 19px; font-weight: 650; letter-spacing: -0.01em; }
    .tl-state p { color: var(--text-muted); font-size: 14px; line-height: 1.55; max-width: 480px; margin: 2px 0 6px; }
    .tl-state p strong { color: var(--text); font-weight: 600; }
    .tl-state .btn { margin-top: 12px; height: 44px; }

    body.tl-stateonly .tl-toolbar,
    body.tl-stateonly .tl-subrow,
    body.tl-stateonly .tl-grid { display: none; }

    /* Responsive */
    @media (max-width: 720px) {
      .page { padding: 20px 16px 44px; }
      .tl-head h1 { font-size: 24px; }
      .tl-head-sub { font-size: 14px; }
      .tl-toolbar { padding: 12px; }
      .tl-field select { height: 40px; }
      .tl-search input { height: 40px; }
      .tl-meta { grid-template-columns: minmax(0, 1fr); gap: 11px 0; }
      .tl-title { font-size: 17px; }
      .tl-play { width: 56px; height: 56px; }
      .tl-play svg { width: 22px; height: 22px; }
      .tl-state { padding: 56px 20px; }
    }
    @media (max-width: 480px) {
      .tl-filters { width: 100%; }
      .tl-field { flex: 1 1 calc(50% - 6px); min-width: 0; }
      .tl-view { display: none; }
      .tl-actions { padding: 12px 14px 16px; }
      .tl-body { padding: 16px 16px 4px; }
    }
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after { animation-duration: 0.001s !important; transition-duration: 0.001s !important; }
    }
  </style>
</head>
<body class="$body_class">
  <div class="page">
    <header class="tl-head">
      <div style="min-width: 0;">
        <h1>Timelapse Gallery</h1>
        <p class="tl-head-sub"><span>Printer: <strong>$printer</strong></span>$head_meta</p>
      </div>
      <a class="api-status" href="/api/v1/printers" title="API healthy &middot; View configured printers">
        <span class="api-dot" aria-hidden="true"></span>API Status
      </a>
    </header>

    <noscript><p style="color:#94a3b8; font-size:13px; margin-bottom:12px;">JavaScript is disabled &mdash; all timelapses are shown below (search, filters and list view are unavailable).</p></noscript>

    <div class="tl-toolbar" role="search">
      <div class="tl-search">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
        <input id="f-q" type="search" placeholder="Search jobs, filenames, or tags&hellip;" aria-label="Search jobs, filenames, or tags" autocomplete="off">
      </div>
      <div class="tl-filters">
        <label class="tl-field">
          <span>Status</span>
          <select id="f-status" aria-label="Filter by status">
            <option value="all">All statuses</option>
            $status_options
          </select>
        </label>
        <label class="tl-field">
          <span>Date</span>
          <select id="f-date" aria-label="Filter by date">
            <option value="all">All time</option>
            <option value="today">Today</option>
            <option value="7">Last 7 days</option>
            <option value="30">Last 30 days</option>
          </select>
        </label>
        <label class="tl-field">
          <span>Frames</span>
          <select id="f-frames" aria-label="Filter by frames">
            <option value="all">All sessions</option>
            <option value="has">With frames</option>
            <option value="none">No frames</option>
          </select>
        </label>
        <label class="tl-field">
          <span>Sort</span>
          <select id="f-sort" aria-label="Sort sessions">
            <option value="started_desc">Started (newest)</option>
            <option value="started_asc">Started (oldest)</option>
            <option value="duration_desc">Duration (longest)</option>
            <option value="frames_desc">Frames (most)</option>
            <option value="frames_asc">Frames (least)</option>
          </select>
        </label>
      </div>
    </div>

    <div class="tl-subrow">
      <span class="tl-count" id="gcount" aria-live="polite"></span>
      <div class="tl-view" role="group" aria-label="View mode">
        <button type="button" data-view="grid" aria-pressed="true" title="Grid view">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>
          Grid
        </button>
        <button type="button" data-view="list" aria-pressed="false" title="List view">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 6h13"/><path d="M8 12h13"/><path d="M8 18h13"/><path d="M3 6h.01"/><path d="M3 12h.01"/><path d="M3 18h.01"/></svg>
          List
        </button>
      </div>
    </div>

    <div class="tl-grid" id="grid">
      $cards_html
    </div>

    <div id="noresult" class="tl-state" hidden>
      <div class="tl-state-ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/><path d="M8 11h6"/></svg></div>
      <h2>No matching timelapses</h2>
      <p>No sessions match the current search or filters.</p>
      <button type="button" class="btn" id="noresult-clear">Clear filters</button>
    </div>

    $state_html
  </div>

  <script>
    (function () {
      'use strict';

      /* --- Media poster helpers (real frames; never fake placeholders) --- */
      function tlInitPoster(video) {
        if (!video || video.dataset.tlPoster === '1') return;
        video.dataset.tlPoster = '1';
        var onMeta = function () {
          video.removeEventListener('loadedmetadata', onMeta);
          try {
            var target = Math.min((video.duration || 0) * 0.35, 45);
            if (target > 0) video.currentTime = target;
          } catch (err) { /* ignore */ }
        };
        var onSeek = function () {
          video.removeEventListener('seeked', onSeek);
          try { video.pause(); } catch (err) { /* ignore */ }
        };
        var onError = function () {
          var media = video.closest ? video.closest('.tl-media') : null;
          if (media) media.classList.add('no-media');
        };
        video.addEventListener('loadedmetadata', onMeta);
        video.addEventListener('seeked', onSeek);
        video.addEventListener('error', onError);
        try { video.preload = 'metadata'; video.load(); } catch (err) { /* ignore */ }
      }

      window.__tlFrameError = function (img) {
        var media = img.closest ? img.closest('.tl-media') : null;
        if (!media) return;
        media.classList.add('img-dead');
        if (media.classList.contains('has-video')) {
          media.classList.add('uses-video');
          tlInitPoster(media.querySelector('.tl-video'));
        } else {
          media.classList.add('no-media');
        }
      };

      window.__tlVideoError = function (video) {
        var media = video.closest ? video.closest('.tl-media') : null;
        if (media) media.classList.add('no-media');
      };

      var posterVideos = Array.prototype.slice.call(document.querySelectorAll('.tl-media.poster-video .tl-video'));
      if (posterVideos.length && 'IntersectionObserver' in window) {
        var io = new IntersectionObserver(function (entries) {
          entries.forEach(function (entry) {
            if (!entry.isIntersecting) return;
            var media = entry.target;
            var video = media.querySelector ? media.querySelector('.tl-video') : null;
            tlInitPoster(video);
            io.unobserve(media);
          });
        }, { rootMargin: '500px 0px' });
        posterVideos.forEach(function (video) {
          if (video.closest) io.observe(video.closest('.tl-media'));
        });
      } else {
        posterVideos.forEach(function (video) { tlInitPoster(video); });
      }

      /* --- Search / filter / sort --- */
      var grid = document.getElementById('grid');
      var toolbar = document.querySelector('.tl-toolbar');
      var noresult = document.getElementById('noresult');
      var gcount = document.getElementById('gcount');
      if (!grid) return;

      var cards = Array.prototype.slice.call(grid.children);
      if (!cards.length) return;

      var searchInput = document.getElementById('f-q');
      var statusSel = document.getElementById('f-status');
      var dateSel = document.getElementById('f-date');
      var framesSel = document.getElementById('f-frames');
      var sortSel = document.getElementById('f-sort');
      var clearBtn = document.getElementById('noresult-clear');
      var viewBtns = document.querySelectorAll('[data-view]');
      var filterActive = false;

      function isoDateFor(epochSecs) {
        return new Date(epochSecs * 1000).toISOString().slice(0, 10);
      }

      function cardMatches(card, state) {
        if (state.status !== 'all' && card.dataset.status !== state.status) return false;
        if (state.frames === 'has' && !(parseInt(card.dataset.frames, 10) > 0)) return false;
        if (state.frames === 'none' && !(parseInt(card.dataset.frames, 10) === 0)) return false;
        if (state.query) {
          var hay = card.dataset.search || '';
          if (hay.indexOf(state.query) === -1) return false;
        }
        if (state.date !== 'all') {
          var cardDate = isoDateFor(parseInt(card.dataset.started, 10));
          var daysAgo = state.date === 'today' ? 0 : parseInt(state.date, 10);
          var limit = new Date(Date.now() - daysAgo * 86400000).toISOString().slice(0, 10);
          if (cardDate < limit) return false;
        }
        return true;
      }

      function sortCards(list, sortKey) {
        var keyMap = {
          started_desc: ['started', -1],
          started_asc: ['started', 1],
          duration_desc: ['duration', -1],
          frames_desc: ['frames', -1],
          frames_asc: ['frames', 1]
        };
        var spec = keyMap[sortKey] || keyMap.started_desc;
        var field = spec[0];
        var dir = spec[1];
        list.sort(function (a, b) {
          var av = field === 'started' ? parseInt(a.dataset.started, 10)
                 : field === 'duration' ? parseFloat(a.dataset.duration || '0')
                 : parseInt(a.dataset.frames, 10);
          var bv = field === 'started' ? parseInt(b.dataset.started, 10)
                 : field === 'duration' ? parseFloat(b.dataset.duration || '0')
                 : parseInt(b.dataset.frames, 10);
          if (av === bv) return 0;
          return (av < bv ? -1 : 1) * dir;
        });
        return list;
      }

      function applyFilters() {
        var state = {
          query: (searchInput.value || '').trim().toLowerCase(),
          status: statusSel.value,
          date: dateSel.value,
          frames: framesSel.value
        };
        filterActive = !!(state.query || state.status !== 'all' || state.date !== 'all' || state.frames !== 'all');

        var visible = cards.filter(function (card) { return cardMatches(card, state); });
        sortCards(visible, sortSel.value);

        cards.forEach(function (card) { card.classList.add('tl-hide'); });
        visible.forEach(function (card) {
          card.classList.remove('tl-hide');
          grid.appendChild(card);
        });

        gcount.textContent = filterActive
          ? visible.length + ' of ' + cards.length + ' session' + (cards.length === 1 ? '' : 's')
          : cards.length + ' session' + (cards.length === 1 ? '' : 's');
        noresult.hidden = visible.length !== 0;
        toolbar.hidden = false;
      }

      var debounce;
      searchInput.addEventListener('input', function () {
        window.clearTimeout(debounce);
        debounce = window.setTimeout(applyFilters, 120);
      });
      statusSel.addEventListener('change', applyFilters);
      dateSel.addEventListener('change', applyFilters);
      framesSel.addEventListener('change', applyFilters);
      sortSel.addEventListener('change', applyFilters);
      if (clearBtn) clearBtn.addEventListener('click', function () {
        searchInput.value = '';
        statusSel.value = 'all';
        dateSel.value = 'all';
        framesSel.value = 'all';
        applyFilters();
        searchInput.focus();
      });

      viewBtns.forEach(function (btn) {
        btn.addEventListener('click', function () {
          viewBtns.forEach(function (b) { b.setAttribute('aria-pressed', b === btn ? 'true' : 'false'); });
          grid.classList.toggle('list', btn.getAttribute('data-view') === 'list');
        });
      });

      applyFilters();

      /* --- Overflow menus --- */
      function closeMenus(except) {
        document.querySelectorAll('.tl-pop.open').forEach(function (pop) {
          if (pop === except) return;
          pop.classList.remove('open');
          var btn = pop.querySelector('.tl-popbtn');
          if (btn) btn.setAttribute('aria-expanded', 'false');
          var menu = pop.querySelector('.tl-menu');
          if (menu) menu.hidden = true;
        });
      }
      document.querySelectorAll('.tl-popbtn').forEach(function (btn) {
        btn.addEventListener('click', function (ev) {
          ev.stopPropagation();
          var pop = btn.closest('.tl-pop');
          var isOpen = pop.classList.contains('open');
          closeMenus(null);
          if (!isOpen) {
            pop.classList.add('open');
            btn.setAttribute('aria-expanded', 'true');
            var menu = pop.querySelector('.tl-menu');
            if (menu) menu.hidden = false;
          }
        });
      });
      document.addEventListener('click', function () { closeMenus(null); });
      document.addEventListener('keydown', function (ev) {
        if (ev.key === 'Escape') closeMenus(null);
      });
    })();
  </script>
</body>
</html>
""")

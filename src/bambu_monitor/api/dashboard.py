"""Standalone web dashboard for Bambu Monitor.

Human-facing UI on top-level paths (not under ``/api/v1``):

- ``GET /dashboard``  server-rendered operational dashboard
- ``GET /events``     dedicated event-history page (kept off the dashboard)
- ``GET /``           redirects to ``/dashboard``

The dashboard is an *operational control plane*: it answers "is everything
OK, what are my printers doing, what did they print recently, are there
problems, are events being delivered".  Detailed event history lives on the
dedicated ``/events`` page instead of cluttering the dashboard.

All routes inherit ``require_api_access`` from the API router so they have the
same access model (loopback / home-LAN tokenless by default, or ``X-API-Key``
when a token is configured).  No machine API surface is added or changed; the
dashboard is server-rendered from the same repositories and state, and its
browser JS polls the existing JSON endpoints for live values.
"""

from __future__ import annotations

import html
import json
import logging
from datetime import datetime, timezone
from string import Template
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Request
from starlette.responses import HTMLResponse, RedirectResponse

from bambu_monitor.api.routes import require_api_access, _tl_fmt_duration, _tl_icon
from bambu_monitor.domain.alerts import Alert
from bambu_monitor.domain.printer import CurrentPrinterState
from bambu_monitor.domain.print_job import PrintJob
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

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_api_access)])


# --------------------------------------------------------------------------
# Small rendering helpers
# --------------------------------------------------------------------------

def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _attr(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _icon(name: str, *, filled: bool = False) -> str:
    return _tl_icon(name, filled=filled)


def _fmt_utc(dt: Optional[datetime]) -> str:
    """Display fallback for datetimes; browser JS localizes via <time>."""
    if not dt:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%b %d, %Y %H:%M") + " UTC"


def _fmt_abs(dt: Optional[datetime]) -> str:
    """Compact absolute localizable time, e.g. 'Sep 09, 2026 09:43'."""
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%b %d, %Y %H:%M")


def _seen_time_html(dt: Optional[datetime]) -> str:
    """Last-seen <time> carrying the live-update hook (JS refreshes on poll)."""
    if not dt:
        return '<span class="muted">—</span>'
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    iso = dt.isoformat()
    full = dt.strftime("%b %d, %Y %H:%M:%S") + " UTC"
    return (
        f'<time class="rel" data-live="seen" datetime="{_attr(iso)}" '
        f'title="{_attr(full)}">{_esc(_fmt_utc(dt))}</time>'
    )


def _time_tag(dt: Optional[datetime], *, show_abs: bool = False) -> str:
    if not dt:
        return '<span class="muted">—</span>'
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    iso = dt.isoformat()
    full = dt.strftime("%b %d, %Y %H:%M:%S") + " UTC"
    abs_html = f'<span class="t-abs">{_esc(_fmt_abs(dt))}</span>' if show_abs else ""
    return (
        f'<span class="t-wrap"><time class="rel" datetime="{_attr(iso)}" '
        f'title="{_attr(full)}">{_esc(_fmt_utc(dt))}</time>{abs_html}</span>'
    )


def _humanize(token: Any) -> str:
    """Turn machine keys like 'filament_runout' into 'Filament Runout'."""
    text = str(token or "").strip().replace(".", "_")
    return text.replace("_", " ").strip().title() or "Event"


# --------------------------------------------------------------------------
# Shared chrome (nav, CSS, page shell)
# --------------------------------------------------------------------------

_UI_CSS = """
:root {
  --bg: #0b1120; --surface: #121b2f; --surface-2: #0e1626;
  --border: #1f2b40; --border-soft: #1a2538;
  --text: #f1f5f9; --text-muted: #93a1b8; --text-faint: #5f6f89;
  --blue: #3b82f6; --blue-soft: #93b8f8;
  --green: #10b981; --green-soft: #34d399;
  --red: #ef4444; --red-soft: #f87171;
  --amber: #f59e0b; --amber-soft: #fbbf24;
  --purple: #a78bfa; --purple-soft: #c4b5fd;
  --hotend: #f97316; --bed: #38bdf8;
  --radius: 12px;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html { -webkit-text-size-adjust: 100%; }
body {
  background: var(--bg); color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  -webkit-font-smoothing: antialiased; line-height: 1.5; font-size: 14px;
}
code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.9em; }
a { color: inherit; }
.muted { color: var(--text-faint); }
svg { flex: 0 0 auto; }

/* --- Top navigation --- */
.topnav {
  display: flex; align-items: center; justify-content: space-between; gap: 16px;
  border-bottom: 1px solid var(--border-soft);
}
.topnav-inner {
  max-width: 1320px; width: 100%; margin: 0 auto; padding: 10px 32px;
  display: flex; align-items: center; justify-content: space-between; gap: 10px 24px;
  flex-wrap: wrap; min-height: 58px;
}
.topnav-right { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; justify-content: flex-end; }
.topnav-extra { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
.brand { display: inline-flex; align-items: center; gap: 10px; font-weight: 700; font-size: 16px; text-decoration: none; letter-spacing: -0.01em; }
.brand svg { width: 20px; height: 20px; color: var(--blue); }
.brand small { color: var(--text-faint); font-weight: 500; font-size: 12px; margin-left: 2px; }
.topnav-links { display: flex; gap: 4px; flex-wrap: wrap; }
.navlink {
  display: inline-flex; align-items: center; gap: 6px; padding: 7px 13px;
  border-radius: 8px; font-size: 13.5px; font-weight: 500;
  color: var(--text-muted); text-decoration: none;
}
.navlink svg { width: 15px; height: 15px; }
.navlink:hover { color: var(--text); background: var(--surface); }
.navlink.active { color: var(--text); background: var(--surface); outline: 1px solid var(--border); }

/* --- Page shell --- */
.page { max-width: 1320px; margin: 0 auto; padding: 26px 32px 40px; }

/* --- Header --- */
.page-header {
  display: flex; align-items: flex-end; justify-content: space-between;
  flex-wrap: wrap; gap: 12px 24px; margin-bottom: 22px;
}
.page-header h1 { font-size: 29px; font-weight: 700; letter-spacing: -0.02em; line-height: 1.25; }
.page-header .subtitle { margin-top: 5px; color: var(--text-muted); font-size: 14px; }
.page-header .subtitle strong { color: var(--text); font-weight: 600; }
.page-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.updated {
  display: inline-flex; align-items: center; gap: 7px; color: var(--text-faint);
  font-size: 12.5px; font-variant-numeric: tabular-nums; margin-right: 6px; white-space: nowrap;
}
.updated svg { width: 13px; height: 13px; }
.updated strong { color: var(--text-muted); font-weight: 600; }

.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 7px;
  height: 34px; padding: 0 13px;
  background: var(--surface); color: var(--text);
  border: 1px solid var(--border); border-radius: 9px;
  font-size: 13px; font-weight: 500; text-decoration: none; cursor: pointer;
  transition: background 0.15s, border-color 0.15s; font-family: inherit; white-space: nowrap;
}
.btn svg { width: 14px; height: 14px; color: var(--text-muted); }
.btn:hover { background: #182338; border-color: #2b3a55; }
.btn-primary { background: var(--blue); border-color: var(--blue); color: #fff; }
.btn-primary svg { color: #fff; }
.btn-primary:hover { background: var(--blue-hover, #2563eb); border-color: var(--blue-hover, #2563eb); }
.btn-ghost { background: transparent; }
.btn-ghost:hover { background: var(--surface); }

.btn:disabled { opacity: 0.55; cursor: default; }

/* --- Fleet summary cards --- */
.summary-grid { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 14px; margin-bottom: 28px; }
.sum {
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 16px 18px; display: flex; align-items: center; gap: 14px; min-width: 0;
}
.sum-ic {
  width: 42px; height: 42px; flex: 0 0 42px; display: grid; place-items: center;
  border-radius: 11px; background: var(--surface-2); color: var(--text-faint);
}
.sum-ic svg { width: 20px; height: 20px; }
.sum-txt { display: flex; flex-direction: column; min-width: 0; }
.sum-val { font-size: 19px; font-weight: 700; letter-spacing: -0.01em; line-height: 1.25; font-variant-numeric: tabular-nums; color: var(--text); }
.sum-lbl { font-size: 12px; color: var(--text-faint); line-height: 1.4; margin-top: 1px; }
.sum.tone-ok .sum-ic { background: #10b9811a; color: var(--green); }
.sum.tone-ok .sum-val { color: var(--green-soft); }
.sum.tone-bad .sum-ic { background: #ef44441a; color: var(--red); }
.sum.tone-bad .sum-val { color: var(--red-soft); }
.sum.tone-warn .sum-ic { background: #f59e0b1a; color: var(--amber); }
.sum.tone-warn .sum-val { color: var(--amber-soft); }
.sum.tone-info .sum-ic { background: #3b82f61a; color: var(--blue); }
.sum.tone-info .sum-val { color: var(--blue-soft); }
.sum.tone-media .sum-ic { background: #a78bfa1a; color: var(--purple); }
.sum.tone-media .sum-val { color: var(--purple-soft); }

/* --- Sections --- */
.section { margin-bottom: 30px; }
.section-head {
  display: flex; align-items: baseline; justify-content: space-between;
  gap: 12px; flex-wrap: wrap; margin-bottom: 14px;
}
.section-head h2 {
  display: inline-flex; align-items: center; gap: 10px;
  font-size: 17px; font-weight: 650; letter-spacing: -0.01em;
}
.section-head h2 svg { width: 17px; height: 17px; color: var(--blue); }
.section-count {
  font-size: 12px; color: var(--text-faint); background: var(--surface);
  border: 1px solid var(--border); border-radius: 999px; padding: 2px 11px;
  font-variant-numeric: tabular-nums; font-weight: 600;
}
.see-all {
  display: inline-flex; align-items: center; gap: 5px; font-size: 12.5px; font-weight: 600;
  color: var(--blue-soft); text-decoration: none; white-space: nowrap;
}
.see-all svg { width: 13px; height: 13px; }
.see-all:hover { color: var(--blue); }

/* --- Device cards --- */
.dev-list { display: flex; flex-direction: column; gap: 16px; }
.dev-card {
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 22px 24px;
}
.dev-top { display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; flex-wrap: wrap; }
.dev-brand { display: flex; align-items: center; gap: 16px; min-width: 0; }
.dev-logo {
  width: 52px; height: 52px; flex: 0 0 52px; display: grid; place-items: center;
  background: linear-gradient(180deg, #16233c, #101a2e); color: var(--blue);
  border: 1px solid var(--border); border-radius: 13px;
}
.dev-logo svg { width: 26px; height: 26px; }
.dev-ident { min-width: 0; }
.dev-name { font-size: 17px; font-weight: 700; letter-spacing: -0.01em; overflow-wrap: anywhere; line-height: 1.3; }
.dev-sub {
  display: flex; align-items: center; flex-wrap: wrap; column-gap: 8px; row-gap: 2px;
  margin-top: 4px; color: var(--text-muted); font-size: 12.5px;
}
.dev-sub strong { color: var(--text); font-weight: 600; }
.dev-sub code { color: var(--text-faint); }
.dev-badges { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.st-badge {
  display: inline-flex; align-items: center; gap: 7px; padding: 4px 12px;
  border-radius: 999px; font-size: 12.5px; font-weight: 650; line-height: 1.4;
}
.st-dot { width: 8px; height: 8px; border-radius: 50%; }
.st-printing { background: #10b9811a; color: var(--green-soft); outline: 1px solid #10b9814d; }
.st-printing .st-dot { background: var(--green); animation: pulse 1.6s infinite; }
.st-preparing { background: #3b82f61a; color: var(--blue-soft); outline: 1px solid #3b82f64d; }
.st-preparing .st-dot { background: var(--blue); animation: pulse 1.6s infinite; }
.st-paused { background: #f59e0b1a; color: var(--amber-soft); outline: 1px solid #f59e0b4d; }
.st-paused .st-dot { background: var(--amber); }
.st-idle { background: #14b8a61a; color: var(--green-soft); outline: 1px solid #14b8a64d; }
.st-idle .st-dot { background: var(--green); }
.st-completed { background: #10b9811a; color: var(--green-soft); outline: 1px solid #10b9814d; }
.st-completed .st-dot { background: var(--green); }
.st-failed { background: #ef44441a; color: var(--red-soft); outline: 1px solid #ef44444d; }
.st-failed .st-dot { background: var(--red); }
.st-offline { background: #4755691a; color: var(--text-muted); outline: 1px solid #4755694d; }
.st-offline .st-dot { background: #64748b; }
.st-unknown { background: #64748b14; color: var(--text-faint); outline: 1px solid #64748b3d; }
.st-unknown .st-dot { background: #64748b; }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
.presence { display: inline-flex; align-items: center; gap: 7px; font-size: 12.5px; font-weight: 600; color: var(--green-soft); }
.presence.off { color: var(--text-faint); }
.pdot { width: 8px; height: 8px; border-radius: 50%; }
.pdot.dot-on { background: var(--green); box-shadow: 0 0 0 3px #10b98126; }
.pdot.dot-off { background: #475569; }
.dev-seen { margin-top: 6px; font-size: 12px; color: var(--text-faint); }
.dev-seen time { color: var(--text-faint); font-variant-numeric: tabular-nums; }

.dev-alerts { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }
.sev-chip {
  display: inline-flex; align-items: center; gap: 6px; padding: 3px 10px;
  border-radius: 8px; font-size: 12px; font-weight: 650;
}
.sev-chip svg { width: 12px; height: 12px; }
.sev-warning { background: #f59e0b1a; color: var(--amber-soft); outline: 1px solid #f59e0b4d; }
.sev-critical { background: #ef44441a; color: var(--red-soft); outline: 1px solid #ef44444d; }
.sev-info { background: #3b82f61a; color: var(--blue-soft); outline: 1px solid #3b82f64d; }

/* --- Live camera preview --- */
.cam-box { margin-top: 18px; background: #000; border: 1px solid var(--border-soft);
  border-radius: 12px; overflow: hidden; }
.cam-box img { display: block; width: 100%; max-height: 300px; object-fit: cover; background: #000; }
.cam-cap { display: flex; align-items: center; gap: 9px; padding: 8px 14px;
  font-size: 12.5px; color: var(--text); border-top: 1px solid var(--border-soft);
  background: var(--surface-2); font-weight: 600; }
.cam-cap .muted { font-weight: 400; }
.cam-live-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--red);
  animation: pulse 1.6s infinite; }
.btn.cam-live-btn.active { background: #ef444418; color: var(--red-soft); border-color: #ef44444d; }
.btn.cam-live-btn.active svg { color: var(--red-soft); }

/* Active print banner */
.dev-print {
  display: grid; gap: 10px; margin-top: 18px;
  background: var(--surface-2); border: 1px solid var(--border-soft); border-radius: 12px;
  padding: 14px 16px;
}
.dev-print-top { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; }
.dev-file { display: inline-flex; align-items: center; gap: 8px; font-size: 14px; font-weight: 650; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dev-file svg { width: 15px; height: 15px; color: var(--text-muted); }
.dev-pct { font-size: 16px; font-weight: 700; color: var(--green-soft); font-variant-numeric: tabular-nums; white-space: nowrap; }
.dev-pbar { height: 6px; border-radius: 999px; background: #1c2941; overflow: hidden; }
.dev-pfill { display: block; height: 100%; border-radius: 999px; background: linear-gradient(90deg, var(--green), #34d399); transition: width 0.3s; }
.dev-print-meta { display: flex; gap: 16px; flex-wrap: wrap; font-size: 12.5px; color: var(--text-muted); }
.dev-print-meta span { display: inline-flex; align-items: center; gap: 6px; font-variant-numeric: tabular-nums; }
.dev-print-meta svg { width: 13px; height: 13px; color: var(--text-faint); }
.dev-running { color: var(--green-soft); font-weight: 650; }
.dev-running.warn { color: var(--amber-soft); }

/* Telemetry: consistent 40px icon tiles (mirrors the timelapse detail page) */
.dev-tele { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; margin-top: 18px; }
.metric {
  display: flex; align-items: center; gap: 12px;
  background: var(--surface-2); border: 1px solid var(--border-soft);
  border-radius: 11px; padding: 12px 14px; min-width: 0;
}
.metric-icon {
  width: 40px; height: 40px; flex: 0 0 40px;
  display: grid; place-items: center; border-radius: 10px;
}
.metric-icon svg { width: 19px; height: 19px; }
.icon-hotend { background: #7c2d1226; color: var(--hotend); }
.icon-bed { background: #0c4a6e26; color: var(--bed); }
.icon-fan { background: #312e8126; color: var(--purple); }
.metric-content { display: flex; flex-direction: column; justify-content: center; min-width: 0; }
.metric-label { font-size: 12px; color: var(--text-muted); line-height: 1.3; }
.metric-value { font-size: 21px; font-weight: 700; letter-spacing: -0.01em; line-height: 1.15; margin-top: 1px; font-variant-numeric: tabular-nums; }
.metric-sub { font-size: 12px; color: var(--text-faint); line-height: 1.3; margin-top: 1px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.dev-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 16px; }
.btn-count {
  background: #1c2941; border-radius: 999px; padding: 0 7px; font-size: 11px;
  line-height: 17px; color: var(--text-muted); font-weight: 600;
}
.manage { position: relative; }
.manage > .btn::after { content: ""; width: 0; height: 0; margin-left: 2px; border-left: 4px solid transparent; border-right: 4px solid transparent; border-top: 4px solid var(--text-faint); }
.manage-menu {
  position: absolute; right: 0; top: calc(100% + 6px); z-index: 30; min-width: 210px;
  background: #0f1829; border: 1px solid var(--border); border-radius: 10px; padding: 6px;
  box-shadow: 0 14px 34px rgb(0 0 0 / 0.45); display: none;
}
.manage[open] .manage-menu { display: block; }
.manage-menu .cap { font-size: 11px; color: var(--text-faint); padding: 6px 10px 4px; }
.manage-menu a {
  display: flex; align-items: center; gap: 9px; padding: 7px 10px; border-radius: 7px;
  font-size: 13px; color: var(--text-muted); text-decoration: none;
}
.manage-menu a svg { width: 14px; height: 14px; color: var(--text-faint); }
.manage-menu a:hover { background: #182338; color: var(--text); }
.manage summary { list-style: none; }
.manage summary::-webkit-details-marker { display: none; }

/* --- Main layout --- */
/* Top row: Devices (2/3) with the Alerts + Outbox rail beside them */
.top-grid { display: grid; grid-template-columns: minmax(0, 2fr) minmax(0, 1fr); gap: 22px; align-items: start; margin-bottom: 30px; }
.devcol { min-width: 0; }
.rail { display: flex; flex-direction: column; gap: 18px; min-width: 0; }
.devcol .recent-card { margin-top: 28px; }
/* (legacy two-column layout for the events page content) */
.content { display: grid; grid-template-columns: minmax(0, 1.95fr) minmax(0, 1fr); gap: 20px; align-items: start; }
.rightcol { display: flex; flex-direction: column; gap: 20px; }

.card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; }
.card-head {
  display: flex; align-items: baseline; justify-content: space-between;
  gap: 10px 16px; flex-wrap: wrap; padding: 18px 22px 0;
}
.card-title { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.card-title h2 { font-size: 16.5px; font-weight: 650; letter-spacing: -0.01em; display: inline-flex; align-items: center; gap: 9px; }
.card-title h2 svg { width: 16px; height: 16px; color: var(--blue); }
.card-title .card-sub { font-size: 12.5px; color: var(--text-faint); }
.card-body { padding: 16px 22px 20px; }

/* --- Tables --- */
.table-scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
.data-table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
.data-table th {
  text-align: left; font-size: 11px; font-weight: 650; letter-spacing: 0.07em;
  text-transform: uppercase; color: var(--text-faint); padding: 0 12px 9px; white-space: nowrap;
}
.data-table td { padding: 12px; border-top: 1px solid var(--border-soft); vertical-align: middle; }
.data-table th:first-child, .data-table td:first-child { padding-left: 0; }
.data-table th:last-child, .data-table td:last-child { padding-right: 0; }

/* Recent Prints: the whole row opens the print's timelapse page */
.tbl-prints { table-layout: fixed; }
.tbl-prints th, .tbl-prints td { overflow: hidden; }
.tbl-prints .c-file { width: 36%; }
.tbl-prints .c-printer { width: 16%; }
.tbl-prints .c-started { width: 15%; }
.tbl-prints .c-duration { width: 12%; }
.tbl-prints .c-status { width: 21%; overflow: visible; }
.tbl-prints .c-status .pill { padding: 2px 9px; max-width: 100%; }
.tbl-prints tbody tr { transition: background 0.12s; }
.tbl-prints tbody tr.job-row.clickable { cursor: pointer; }
.tbl-prints tbody tr.job-row.clickable:hover td { background: #17233c; }
.tbl-prints tbody tr.job-row.clickable:focus-visible td { background: #17233c; box-shadow: inset 0 0 0 1px var(--blue); }
.fname {
  display: block; min-width: 0; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; font-size: 13.5px; font-weight: 650;
}
.fname svg { width: 14px; height: 14px; color: var(--text-faint); vertical-align: -2px; margin-right: 8px; }
.fname .ft { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; vertical-align: bottom; }
.pcode { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-muted); font-size: 12.5px; }
.c-printer code { color: var(--text-muted); font-size: 12.5px; }
.c-started, .c-duration { white-space: nowrap; }
.c-duration { font-variant-numeric: tabular-nums; color: var(--text-muted); }
.c-status { white-space: nowrap; }
.pill {
  display: inline-flex; align-items: center; gap: 6px; padding: 2px 10px;
  border-radius: 999px; font-size: 12px; font-weight: 650; white-space: nowrap;
}
.pill-dot { width: 6px; height: 6px; border-radius: 50%; }
.pill.ok { background: #10b98118; color: var(--green-soft); }
.pill.ok .pill-dot { background: var(--green); }
.pill.ok-ghost { background: #10b98110; color: var(--text-muted); }
.pill.ok-ghost .pill-dot { background: var(--green); opacity: 0.7; }
.pill.warn { background: #f59e0b18; color: var(--amber-soft); }
.pill.warn .pill-dot { background: var(--amber); }
.pill.bad { background: #ef444418; color: var(--red-soft); }
.pill.bad .pill-dot { background: var(--red); }
.pill.busy { background: #3b82f618; color: var(--blue-soft); }
.pill.busy .pill-dot { background: var(--blue); }
.t-wrap { display: inline-flex; flex-direction: column; line-height: 1.45; }
.t-wrap .rel { color: var(--text-muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
.t-wrap .t-abs { color: var(--text-faint); font-size: 12px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.sev-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; }
.sev-dot.bad { background: var(--red); }
.sev-dot.warn { background: var(--amber); }
.sev-dot.info { background: var(--blue); }

/* --- Outbox counters --- */
.qgrid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
.qcell {
  background: var(--surface-2); border: 1px solid var(--border-soft); border-radius: 10px;
  padding: 12px 8px; text-align: center; min-width: 0;
}
.qval { display: block; font-size: 20px; font-weight: 700; font-variant-numeric: tabular-nums; color: var(--text); }
.qcell.q-pending .qval { color: var(--amber-soft); }
.qcell.q-delivering .qval { color: var(--blue-soft); }
.qcell.q-delivered .qval { color: var(--green-soft); }
.qcell.q-failed .qval { color: var(--red-soft); }
.qcell.q-failed { border-color: #ef44444d; background: #ef444410; }
.qlbl { font-size: 11px; color: var(--text-faint); }
.qtotal { font-size: 12.5px; color: var(--text-muted); margin-top: 12px; }
.notice {
  display: flex; gap: 10px; align-items: flex-start; margin-top: 12px;
  border-radius: 9px; padding: 10px 12px; font-size: 12.5px; line-height: 1.5;
}
.notice svg { width: 15px; height: 15px; margin-top: 1px; }
.notice.warn { background: #f59e0b12; outline: 1px solid #f59e0b40; color: #e8c47a; }
.notice.ok { background: #10b9810d; outline: 1px solid #10b98133; color: var(--text-muted); }
.notice strong { color: inherit; }
.notice code { color: #e8c47a; }

/* --- Empty / idle states --- */
.empty { text-align: center; padding: 30px 14px; color: var(--text-muted); }
.empty-ic {
  display: inline-grid; place-items: center; width: 44px; height: 44px; margin-bottom: 12px;
  border-radius: 12px; background: var(--surface-2); color: var(--text-faint);
}
.empty-ic svg { width: 20px; height: 20px; }
.empty strong { display: block; color: var(--text); font-size: 14.5px; font-weight: 650; }
.empty p { font-size: 13px; color: var(--text-faint); margin-top: 3px; }

/* --- System status bar --- */
.sys-status { margin-top: 6px; }
.sys-inner {
  display: flex; align-items: center; gap: 14px; padding: 16px 22px;
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
  text-decoration: none; transition: border-color 0.15s, background 0.15s;
}
.sys-inner:hover { border-color: #2b3a55; background: #141d33; }
.sys-ic {
  width: 40px; height: 40px; flex: 0 0 40px; display: grid; place-items: center;
  border-radius: 11px; background: var(--surface-2); color: var(--text-faint);
}
.sys-ic svg { width: 19px; height: 19px; }
.sys-txt { flex: 1 1 auto; min-width: 0; }
.sys-title { font-size: 14.5px; font-weight: 700; display: inline-flex; align-items: center; gap: 8px; }
.sys-dot { width: 9px; height: 9px; border-radius: 50%; }
.sys-caption { font-size: 12.5px; color: var(--text-faint); margin-top: 2px; }
.sys-cta { display: inline-flex; align-items: center; gap: 5px; font-size: 12.5px; font-weight: 600; color: var(--text-muted); white-space: nowrap; }
.sys-cta svg { width: 13px; height: 13px; }
.sys-green .sys-ic { background: #10b9811a; color: var(--green); }
.sys-green .sys-dot { background: var(--green); box-shadow: 0 0 0 3px #10b98126; }
.sys-green .sys-title { color: var(--green-soft); }
.sys-amber .sys-ic { background: #f59e0b1a; color: var(--amber); }
.sys-amber .sys-dot { background: var(--amber); box-shadow: 0 0 0 3px #f59e0b26; }
.sys-amber .sys-title { color: var(--amber-soft); }
.sys-red .sys-ic { background: #ef44441a; color: var(--red); }
.sys-red .sys-dot { background: var(--red); box-shadow: 0 0 0 3px #ef444426; }
.sys-red .sys-title { color: var(--red-soft); }

/* --- Footer --- */
.foot {
  margin-top: 34px; padding-top: 16px; border-top: 1px solid var(--border-soft);
  color: var(--text-faint); font-size: 12px; display: flex; gap: 8px 20px;
  flex-wrap: wrap; align-items: center; justify-content: space-between;
}
.foot-left, .foot-links { display: flex; gap: 6px 16px; flex-wrap: wrap; align-items: center; }
.foot a { color: var(--text-muted); text-decoration: none; }
.foot a:hover { color: var(--text); }

/* --- Events page --- */
.ev-controls { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 16px; }
.ev-controls select,
.ev-controls input[type="search"] {
  background: var(--surface); color: var(--text); border: 1px solid var(--border);
  border-radius: 9px; height: 34px; padding: 0 10px; font-size: 13px; font-family: inherit;
}
.ev-controls input[type="search"] {
  flex: 1 1 190px; min-width: 170px; max-width: 300px; padding: 0 12px;
}
.ev-controls input[type="search"]::placeholder { color: var(--text-faint); }
.ev-controls input[type="search"]:focus, .ev-controls select:focus { outline: none; border-color: var(--blue); }
.ev-table { font-size: 13.5px; }
.ev-table td { padding: 9px 12px; }
.ev-table td:first-child { padding-left: 0; }
.ev-type { font-weight: 600; }
.ev-note { font-size: 12.5px; color: var(--text-faint); margin-top: 12px; }
.event-row-hidden { display: none !important; }
.ev-row { cursor: pointer; }
.ev-row:hover td { background: #17233c; }
.ev-modal { position: fixed; inset: 0; background: rgba(4, 8, 16, 0.68); display: none;
  align-items: center; justify-content: center; z-index: 200; padding: 24px; }
.ev-modal.open { display: flex; }
.ev-box { background: #0f1829; border: 1px solid var(--border); border-radius: 12px;
  width: min(780px, 100%); max-height: 84vh; display: flex; flex-direction: column;
  box-shadow: 0 24px 60px rgb(0 0 0 / 0.5); overflow: hidden; }
.ev-box-head { display: flex; align-items: center; justify-content: space-between; gap: 14px;
  padding: 12px 16px; border-bottom: 1px solid var(--border-soft); }
.ev-box-head h3 { font-size: 13.5px; font-weight: 650; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; font-variant-numeric: tabular-nums; }
.ev-box-head code { color: var(--text-muted); font-size: 12px; }
.ev-box-close { background: transparent; border: 0; color: var(--text-muted); cursor: pointer;
  font-size: 18px; line-height: 1; padding: 4px 6px; border-radius: 6px; }
.ev-box-close:hover { color: var(--text); background: #182338; }
.ev-box pre { margin: 0; padding: 16px 18px; overflow: auto; font-size: 12px; line-height: 1.55;
  color: #d7e0ee; background: transparent; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }

/* --- Responsive --- */
@media (max-width: 1180px) {
  .summary-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .top-grid { grid-template-columns: minmax(0, 1fr); }
  .content { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 860px) {
  .dev-tele { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}
@media (max-width: 760px) {
  .summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .dev-tele { grid-template-columns: minmax(0, 1fr); }
  .metric { max-width: 340px; }
}
@media (max-width: 640px) {
  .page { padding: 20px 16px 32px; }
  .topnav-inner { padding: 0 16px; }
  .page-header h1 { font-size: 25px; }
  .card-body { padding: 14px 16px 18px; }
  .card-head { padding: 16px 16px 0; }
  .dev-card { padding: 18px 16px; }
}
"""


def _nav_html(active: str = "dashboard", extra_right: str = "") -> str:
    def _link(href: str, label: str, icon: str, is_active: bool) -> str:
        cls = "navlink active" if is_active else "navlink"
        return f'<a class="{cls}" href="{href}">{_icon(icon)}{label}</a>'

    links = [
        _link("/dashboard", "Dashboard", "grid", active == "dashboard"),
        _link("/docs", "API docs", "braces", False),
        _link("/health", "Health", "pulse", False),
    ]
    right = f'<div class="topnav-links">{"".join(links)}</div>'
    if extra_right:
        right += f'<div class="topnav-extra">{extra_right}</div>'
    return (
        '<nav class="topnav"><div class="topnav-inner">'
        '<a class="brand" href="/dashboard">'
        f'{_icon("printer")}Bambu Monitor<small>local control plane</small></a>'
        f'<div class="topnav-right">{right}</div></div></nav>'
    )


def _foot_html() -> str:
    links = [
        ('<a href="/dashboard">Dashboard</a>'),
        ('<a href="/events">Event history</a>'),
        ('<a href="/api/v1/printers" target="_blank" rel="noopener">Printers (JSON)</a>'),
        ('<a href="/health" target="_blank" rel="noopener">Health (JSON)</a>'),
        ('<a href="/api/v1/outbox/status" target="_blank" rel="noopener">Outbox (JSON)</a>'),
    ]
    return (
        '<footer class="foot"><div class="foot-left">'
        "<span>Bambu Monitor &middot; local control plane</span></div>"
        f'<div class="foot-links">{"".join(links)}</div></footer>'
    )


# Dashboard live-polling script (dependency-free; updates the data-live hooks).
_LIVE_SCRIPT = """
<script>
var REFRESH_MS = 8000;
var MAX_ERRORS = 3;
var updatedEl = document.querySelector('[data-live="updated"]');
var btnAuto = document.getElementById('btn-auto');
var btnRefresh = document.getElementById('btn-refresh');
var autoOn = true;
var errorCount = 0;
var timer = null;

function localizeTimes() {
  var els = document.querySelectorAll('time.rel');
  for (var i = 0; i < els.length; i++) {
    var el = els[i];
    var iso = el.getAttribute('datetime');
    if (!iso) { el.textContent = '\u2014'; continue; }
    var d = new Date(iso);
    if (isNaN(d.getTime())) continue;
    var diff = (Date.now() - d.getTime()) / 1000;
    var txt;
    if (diff < 45) txt = 'just now';
    else if (diff < 90) txt = '1m ago';
    else if (diff < 3600) txt = Math.round(diff / 60) + 'm ago';
    else if (diff < 86400) txt = Math.round(diff / 3600) + 'h ago';
    else if (diff < 172800) txt = 'yesterday';
    else txt = d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    el.textContent = txt;
    el.title = d.toLocaleString();
  }
}

function updatePresence(el, online) {
  el.setAttribute('data-online', online ? '1' : '0');
  var dot = el.querySelector('.pdot');
  var txt = el.querySelector('.ptxt');
  dot.className = 'pdot ' + (online ? 'dot-on' : 'dot-off');
  txt.textContent = online ? 'Online' : 'Offline';
  if (online) el.classList.remove('off'); else el.classList.add('off');
}

var STATE_LABELS = { printing: 'Printing', paused: 'Paused', preparing: 'Preparing', idle: 'Idle',
  completed: 'Completed', failed: 'Failed', offline: 'Offline', unknown: 'Unknown' };
function updateStateBadge(el, state, online) {
  var key = (state || 'unknown').toLowerCase();
  if (key === 'unknown' && !online) key = 'offline';
  el.className = 'st-badge st-' + key;
  el.setAttribute('data-state', key);
  var txt = el.querySelector('.st-txt');
  txt.textContent = STATE_LABELS[key] || state;
}

function applySummary(health) {
  var summaries = (health.printers && health.printers.summary) || [];
  var cards = document.querySelectorAll('.dev-card');
  var onlineCount = 0;
  var activeCount = 0;
  for (var i = 0; i < summaries.length; i++) {
    var s = summaries[i];
    if (s.online) onlineCount++;
    if (s.state === 'printing' || s.state === 'preparing' || s.state === 'paused') activeCount++;
  }
  var map = {};
  for (var j = 0; j < summaries.length; j++) map[summaries[j].id] = summaries[j];
  for (var k = 0; k < cards.length; k++) {
    var card = cards[k];
    var s = map[card.getAttribute('data-pid')];
    if (!s) continue;
    var presence = card.querySelector('[data-live="presence"]');
    var badge = card.querySelector('[data-live="state"]');
    if (presence) updatePresence(presence, !!s.online);
    if (badge) updateStateBadge(badge, s.state, !!s.online);
  }
  applyStat('online', onlineCount + ' / ' + summaries.length + ' online', toneForOnline(onlineCount, summaries.length));
  applyStat('now', String(activeCount), activeCount > 0 ? 'ok' : '');
}
function toneForOnline(on, total) {
  if (total === 0) return '';
  if (on === total) return 'ok';
  if (on === 0) return 'bad';
  return 'warn';
}
function applyStat(key, value, tone) {
  var sum = document.querySelector('.sum[data-stat="' + key + '"]');
  if (!sum) return;
  var val = sum.querySelector('.sum-val');
  if (val) val.textContent = value;
  sum.className = 'sum' + (tone ? ' tone-' + tone : '');
}
function applySystemStats(health) {
  var db = health.database || {};
  var dbOk = !!db.available;
  applyStat('db', dbOk ? ('OK \u00b7 ' + (db.journal_mode || '?')) : 'Error', dbOk ? 'ok' : 'bad');
  var ob = health.outbox || {};
  function setQ(name, value) {
    var el = document.querySelector('[data-live="q-' + name + '"]');
    if (el) el.textContent = value;
  }
  setQ('pending', ob.pending || 0);
  setQ('delivering', ob.delivering || 0);
  setQ('delivered', ob.delivered || 0);
  setQ('failed', ob.failed_dlq || 0);
  var total = (ob.pending || 0) + (ob.delivering || 0) + (ob.delivered || 0) + (ob.failed_dlq || 0);
  var qt = document.querySelector('[data-live="q-total"]');
  if (qt) qt.textContent = total + ' message' + (total === 1 ? '' : 's') + ' in the outbox';
}

function fmtTemp(v) {
  if (v === null || v === undefined || isNaN(v)) return '\u2014';
  return Math.round(v * 10) / 10 + '\u00b0C';
}
function fmtDuration(seconds) {
  var total = Math.floor(seconds);
  var h = Math.floor(total / 3600);
  var m = Math.floor((total % 3600) / 60);
  var s = total % 60;
  if (h) return h + 'h ' + m + 'm ' + s + 's';
  if (m) return m + 'm ' + s + 's';
  return s + 's';
}
function applyPrinterState(card, st) {
  if (!st) return;
  var presence = card.querySelector('[data-live="presence"]');
  var badge = card.querySelector('[data-live="state"]');
  if (presence) updatePresence(presence, !!st.online);
  if (badge) updateStateBadge(badge, st.state, !!st.online);
  var set = function (key, text) {
    var el = card.querySelector('[data-live="' + key + '"]');
    if (el) el.textContent = text;
  };
  var t = st.temperatures || {};
  set('nozzle', fmtTemp(t.nozzle));
  set('nozzle-tgt', t.nozzle_target && t.nozzle_target > 0 ? ('\u2192 ' + fmtTemp(t.nozzle_target)) : '');
  set('bed', fmtTemp(t.bed));
  set('bed-tgt', t.bed_target && t.bed_target > 0 ? ('\u2192 ' + fmtTemp(t.bed_target)) : '');
  var fanEl = card.querySelector('[data-live="fan"]');
  if (fanEl) {
    var fan = st.cooling_fan_speed;
    if (fan === null || fan === undefined || fan <= 0) fanEl.textContent = '0%';
    else fanEl.textContent = Math.min(100, Math.round(fan / 255 * 100)) + '%';
  }
  var print = st.print;
  if (print) {
    var pct = Math.max(0, Math.min(100, print.progress || 0));
    var pfill = card.querySelector('[data-live="pbar"]');
    var pctEl = card.querySelector('[data-live="pct"]');
    if (pfill) pfill.style.width = pct + '%';
    if (pctEl) pctEl.textContent = pct + '%';
    set('layer', print.total_layers ? ('L' + (print.layer || 0) + '/' + print.total_layers)
      : (print.layer ? 'L' + print.layer : '\u2014'));
    var rem = print.remaining_seconds;
    var etaEl = card.querySelector('[data-live="eta"]');
    if (etaEl) etaEl.textContent = (rem === null || rem === undefined || rem <= 0) ? '\u2014' : ('~' + fmtDuration(rem) + ' left');
  }
  var seen = card.querySelector('[data-live="seen"]');
  if (seen && st.last_seen) {
    seen.setAttribute('datetime', st.last_seen);
    seen.textContent = 'just now';
    seen.title = new Date(st.last_seen).toLocaleString();
  }
}

function markUpdated() { if (updatedEl) updatedEl.textContent = new Date().toLocaleTimeString(); }

function pollOnce() {
  fetch('/health', { headers: { 'Accept': 'application/json' } })
    .then(function (resp) {
      if (resp.status === 401 || resp.status === 403) throw new Error('auth');
      return resp.json();
    })
    .then(function (health) {
      errorCount = 0;
      applySummary(health);
      applySystemStats(health);
      var cards = document.querySelectorAll('.dev-card');
      var online = [];
      for (var i = 0; i < cards.length; i++) {
        var p = cards[i].querySelector('[data-live="presence"]');
        if (p && p.getAttribute('data-online') === '1') online.push(cards[i]);
      }
      online.forEach(function (card, idx) {
        setTimeout(function () {
          var pid = card.getAttribute('data-pid');
          fetch('/api/v1/printers/' + encodeURIComponent(pid) + '/status', { headers: { 'Accept': 'application/json' } })
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (st) { if (st) applyPrinterState(card, st); })
            .catch(function () {});
        }, idx * 200);
      });
      localizeTimes();
      markUpdated();
    })
    .catch(function (err) {
      if (err && err.message === 'auth') { pauseAuto('API requires a token \u2014 auto-refresh stopped.'); return; }
      errorCount += 1;
      if (errorCount >= MAX_ERRORS) pauseAuto('Auto-refresh paused after repeated errors.');
    });
}
function pauseAuto(msg) {
  autoOn = false;
  if (timer) { clearInterval(timer); timer = null; }
  btnAuto.setAttribute('aria-pressed', 'false');
  btnAuto.querySelector('span').textContent = 'Auto-refresh off';
  if (msg) {
    var label = document.createElement('span');
    label.className = 'updated';
    label.style.color = 'var(--amber-soft)';
    label.textContent = msg;
    btnAuto.parentNode.appendChild(label);
  }
}
function startAuto() {
  autoOn = true;
  btnAuto.setAttribute('aria-pressed', 'true');
  btnAuto.querySelector('span').textContent = 'Auto-refresh on';
  if (timer) clearInterval(timer);
  timer = setInterval(pollOnce, REFRESH_MS);
}
btnAuto.addEventListener('click', function () { if (autoOn) pauseAuto(''); else startAuto(); });
btnRefresh.addEventListener('click', pollOnce);

// Whole-row navigation: clicking a Recent Prints row opens its timelapse page.
var jobRows = document.querySelectorAll('tr.job-row[data-href]');
for (var r = 0; r < jobRows.length; r++) (function (row) {
  function go() { window.location.href = row.getAttribute('data-href'); }
  row.addEventListener('click', function (e) {
    if (e.button && e.button !== 0) return;
    if (e.target.closest && e.target.closest('a')) return;
    go();
  });
  row.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); }
  });
})(jobRows[r]);

// Live camera preview: poll the snapshot endpoint while toggled on.
function wireLiveCameras() {
  var buttons = document.querySelectorAll('.cam-live-btn');
  for (var c = 0; c < buttons.length; c++) (function (btn) {
    var card = btn.closest('.dev-card');
    if (!card) return;
    var box = card.querySelector('[data-live-cam]');
    if (!box) return;
    var img = box.querySelector('img');
    var base = img.getAttribute('data-src');
    var sep = (base.indexOf('?') >= 0) ? '&' : '?';
    var timer = null;
    var fails = 0;
    var label = btn.querySelector('span');
    function stop() {
      if (timer) { clearInterval(timer); timer = null; }
      box.hidden = true;
      btn.classList.remove('active');
      btn.setAttribute('aria-pressed', 'false');
      if (label) label.textContent = 'Live';
    }
    function tick() { img.src = base + sep + 't=' + Date.now(); }
    function start() {
      fails = 0;
      box.hidden = false;
      btn.classList.add('active');
      btn.setAttribute('aria-pressed', 'true');
      if (label) label.textContent = 'Stop';
      tick();
      timer = setInterval(tick, 3000);
    }
    btn.addEventListener('click', function () { if (timer) stop(); else start(); });
    img.addEventListener('error', function () {
      fails += 1;
      if (fails >= 3) stop();
    });
  })(buttons[c]);
}
wireLiveCameras();

localizeTimes();
startAuto();
</script>
"""

_PAGE = Template("""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>$doc_title | Bambu Monitor</title>
  <style>$ui_css</style>
</head>
<body>
$nav_html
<main class="page">
$main_html
</main>
$foot_html
$script_html
</body>
</html>
""")


# --------------------------------------------------------------------------
# Printer state -> badge mapping (mirrored in page JS)
# --------------------------------------------------------------------------

_STATE_BADGES = {
    "printing": ("st-printing", "Printing"),
    "paused": ("st-paused", "Paused"),
    "preparing": ("st-preparing", "Preparing"),
    "idle": ("st-idle", "Idle"),
    "completed": ("st-completed", "Completed"),
    "failed": ("st-failed", "Failed"),
    "offline": ("st-offline", "Offline"),
    "unknown": ("st-unknown", "Unknown"),
}


def _state_badge(state_value: Optional[str], online: bool) -> str:
    key = (state_value or "unknown").lower()
    cls, label = _STATE_BADGES.get(key, _STATE_BADGES["unknown"])
    if key == "unknown" and not online:
        cls, label = _STATE_BADGES["offline"]
        key = "offline"
    return (
        f'<span class="st-badge {cls}" data-live="state" data-state="{_attr(key)}">'
        f'<span class="st-dot" aria-hidden="true"></span>'
        f'<span class="st-txt">{_esc(label)}</span></span>'
    )


def _presence(online: bool) -> str:
    cls = "presence off" if not online else "presence"
    dot = "dot-on" if online else "dot-off"
    return (
        f'<span class="{cls}" data-live="presence" data-online="{"1" if online else "0"}">'
        f'<span class="pdot {dot}" aria-hidden="true"></span>'
        f'<span class="ptxt">{_esc("Online" if online else "Offline")}</span></span>'
    )


def _metric(cell_id: str, icon_name: str, icon_cls: str, label: str, value: str, sub: str) -> str:
    sub_html = f'<span class="metric-sub" data-live="{cell_id}-tgt">{_esc(sub)}</span>'
    return (
        '<div class="metric">'
        f'<div class="metric-icon {icon_cls}">{_icon(icon_name)}</div>'
        '<div class="metric-content">'
        f'<span class="metric-label">{_esc(label)}</span>'
        f'<span class="metric-value" data-live="{cell_id}">{_esc(value)}</span>'
        f"{sub_html}</div></div>"
    )


# --------------------------------------------------------------------------
# Device card
# --------------------------------------------------------------------------

def _printer_card_html(
    printer: Any,
    state: Optional[CurrentPrinterState],
    active_job: Optional[PrintJob],
    active_alerts: List[Alert],
    has_camera: bool,
    session_count: int,
) -> str:
    pid = printer.id
    pid_attr = _attr(pid)
    state_value = state.state.value if state else "unknown"
    online = bool(state.online if state else printer.online)
    last_seen = state.last_seen if (state and state.last_seen) else printer.last_seen

    temps = state.temperatures if state else None
    nozzle = round(temps.nozzle, 1) if (temps and temps.nozzle) else None
    nozzle_tgt = round(temps.nozzle_target, 1) if (temps and temps.nozzle_target and temps.nozzle_target > 0) else None
    bed = round(temps.bed, 1) if (temps and temps.bed) else None
    bed_tgt = round(temps.bed_target, 1) if (temps and temps.bed_target and temps.bed_target > 0) else None
    fan_raw = state.cooling_fan_speed if state else None
    fan_pct = round(fan_raw / 255 * 100) if (fan_raw is not None and fan_raw > 0) else None

    job = active_job or (state.print if state else None)

    # --- Active print banner (current operation, kept above telemetry) ---
    active_html = ""
    if job is not None:
        progress = int(getattr(job, "progress", 0) or 0)
        layer = int(getattr(job, "layer", 0) or 0)
        total_layers = int(getattr(job, "total_layers", 0) or 0)
        remaining = getattr(job, "remaining_seconds", None)
        filename = str(getattr(job, "filename", "") or "")

        if isinstance(job, PrintJob):
            job_key = job.status.value
        else:
            job_key = state_value
        running = job_key in ("running", "printing", "prepare", "preparing")
        paused = job_key in ("paused",)
        run_txt = "Paused" if paused else ("Running" if running else "Print")
        run_cls = "dev-running warn" if paused else "dev-running"
        layer_txt = f"L{layer}/{total_layers}" if total_layers else ("L%s" % layer if layer else "—")
        eta_txt = "~%s left" % _tl_fmt_duration(remaining) if (remaining is not None and remaining > 0) else "—"
        width = min(100, max(0, progress))
        active_html = (
            '<div class="dev-print">'
            '<div class="dev-print-top">'
            f'<span class="dev-file">{_icon("file")}<span>{_esc(filename or "Active print")}</span></span>'
            f'<span class="dev-pct" data-live="pct">{progress}%</span>'
            "</div>"
            f'<div class="dev-pbar"><span class="dev-pfill" data-live="pbar" style="width:{width}%;"></span></div>'
            '<div class="dev-print-meta">'
            f'<span>{_icon("layers")}<span data-live="layer">{_esc(layer_txt)}</span></span>'
            f'<span>{_icon("timer")}<span data-live="eta">{_esc(eta_txt)}</span></span>'
            f'<span class="{run_cls}">{_icon("activity")}<span data-live="running">{_esc(run_txt)}</span></span>'
            "</div></div>"
        )

    # --- Active alerts chips ---
    alert_chips = ""
    if active_alerts:
        chips = []
        for al in active_alerts[:3]:
            sev_cls = {
                "critical": "sev-critical", "warning": "sev-warning", "info": "sev-info",
            }.get(al.severity.value if hasattr(al.severity, "value") else "info", "sev-info")
            chips.append(
                f'<span class="sev-chip {sev_cls}">{_icon("alert")}'
                f"{_esc(_humanize(al.alert_type))}</span>"
            )
        alert_chips = f'<div class="dev-alerts">{"".join(chips)}</div>'

    # --- Telemetry metrics ---
    nozzle_sub = "" if nozzle_tgt is None else f"→ {nozzle_tgt:g}°C"
    bed_sub = "" if bed_tgt is None else f"→ {bed_tgt:g}°C"
    metrics_html = (
        _metric("nozzle", "thermometer", "icon-hotend", "Nozzle",
                "—" if nozzle is None else f"{nozzle:g}°C", nozzle_sub)
        + _metric("bed", "bed", "icon-bed", "Bed",
                  "—" if bed is None else f"{bed:g}°C", bed_sub)
        + _metric("fan", "fan", "icon-fan", "Cooling fan",
                  "—" if fan_pct is None else f"{fan_pct}%", "")
    )

    # --- Action buttons + optional live camera preview ---
    actions = [
        '<a class="btn btn-ghost" href="/api/v1/printers/%s/status" target="_blank" rel="noopener" '
        'title="Live status JSON">%sStatus JSON</a>' % (pid_attr, _icon("braces"))
    ]
    cam_html = ""
    if has_camera:
        cam_base = f"/api/v1/printers/{pid_attr}/camera/snapshot"
        cam_html = (
            '<div class="cam-box" data-live-cam hidden>'
            f'<img alt="Live camera preview" data-src="{cam_base}">'
            '<div class="cam-cap"><span class="cam-live-dot" aria-hidden="true"></span>'
            "<span>Live</span><span class=\"muted\">fresh snapshot every few seconds</span></div></div>"
        )
        actions.insert(
            1,
            '<button type="button" class="btn cam-live-btn" aria-pressed="false" '
            f'title="Show live camera preview">{_icon("video")}<span>Live</span></button>',
        )
        actions.append(
            '<a class="btn" href="%s" target="_blank" rel="noopener" '
            'title="Open a fresh camera snapshot">%sCamera</a>' % (cam_base, _icon("expand"))
        )
    tl_label = "Timelapses"
    if session_count:
        tl_label = f"Timelapses <span class=\"btn-count\">{session_count}</span>"
    actions.append(
        '<a class="btn" href="/gallery?printer=%s">%s%s</a>' % (pid_attr, _icon("film"), tl_label)
    )
    manage_menu_items = "".join([
        f'<a href="/api/v1/printers/{pid_attr}" target="_blank" rel="noopener">{_icon("printer")}Printer details</a>',
        f'<a href="/api/v1/printers/{pid_attr}/prints" target="_blank" rel="noopener">{_icon("cube")}Print history</a>',
        f'<a href="/api/v1/printers/{pid_attr}/events" target="_blank" rel="noopener">{_icon("pulse")}Events</a>',
        f'<a href="/api/v1/printers/{pid_attr}/events/stream" target="_blank" rel="noopener">{_icon("activity")}Live event stream</a>',
    ])
    actions.append(
        '<details class="manage"><summary class="btn" aria-haspopup="true">Manage</summary>'
        f'<div class="manage-menu" role="menu">{manage_menu_items}</div></details>'
    )

    seen_html = _seen_time_html(last_seen)
    return (
        f'<article class="dev-card" data-pid="{pid_attr}">'
        '<div class="dev-top">'
        '<div class="dev-brand">'
        f'<span class="dev-logo">{_icon("printer")}</span>'
        '<div class="dev-ident">'
        f'<div class="dev-name">{_esc(pid)}</div>'
        '<div class="dev-sub">'
        f'<strong>{_esc(printer.model)}</strong>'
        '<span class="dot-sep muted">&middot;</span>'
        f'{_esc(printer.serial_number)}'
        '<span class="dot-sep muted">&middot;</span>'
        f'<code>{_esc(printer.host)}</code>'
        "</div></div></div>"
        '<div class="dev-badges">'
        f"{_presence(online)}{_state_badge(state_value, online)}</div>"
        "</div>"
        f'<div class="dev-seen">Last seen {seen_html}</div>'
        f"{alert_chips}{active_html}"
        f'<div class="dev-tele">{metrics_html}</div>'
        f"{cam_html}"
        f'<div class="dev-actions">{"".join(actions)}</div>'
        "</article>"
    )


# --------------------------------------------------------------------------
# Fleet summary
# --------------------------------------------------------------------------

def _summary_cards_html(
    db_health: Dict[str, Any],
    printers_online: int,
    printers_total: int,
    printing_now: int,
    open_alerts: int,
    session_total: int,
) -> str:
    db_ok = bool(db_health.get("available"))
    db_txt = f"OK · {db_health.get('journal_mode', '?')}" if db_ok else "Error"

    cards = [
        ("db", "database", "Database", db_txt, "ok" if db_ok else "bad", None),
        ("online", "printer", "Printers", f"{printers_online} / {printers_total} online",
         "ok" if printers_total and printers_online == printers_total else
         ("bad" if printers_total and printers_online == 0 else ("warn" if printers_online else "")), None),
        ("now", "play", "Printing now", str(printing_now), "ok" if printing_now else "", None),
        ("alerts", "bell", "Open alerts", str(open_alerts), "bad" if open_alerts else "", None),
        ("sessions", "film", "Timelapses", str(session_total), "media" if session_total else "", None),
    ]
    html_parts = []
    for key, icon, label, value, tone, _ in cards:
        cls = f"sum tone-{tone}" if tone else "sum"
        html_parts.append(
            f'<div class="{cls}" data-stat="{key}">'
            f'<span class="sum-ic">{_icon(icon)}</span>'
            '<span class="sum-txt">'
            f'<span class="sum-val">{_esc(value)}</span>'
            f'<span class="sum-lbl">{_esc(label)}</span>'
            "</span></div>"
        )
    return f'<div class="summary-grid">{"".join(html_parts)}</div>'


# --------------------------------------------------------------------------
# Recent prints
# --------------------------------------------------------------------------

def _job_row_html(job: Any, session: Any) -> str:
    status = (job.status.value if hasattr(job.status, "value") else str(job.status or "")).lower()
    chip = {
        "running": ("ok", "Printing"), "prepare": ("busy", "Preparing"), "paused": ("warn", "Paused"),
        "failed": ("bad", "Failed"), "completed": ("ok-ghost", "Completed"),
    }.get(status, ("ok-ghost", status.title() or "—"))
    duration = _tl_fmt_duration(job.duration_seconds) if job.duration_seconds else "—"

    # Full job id stays in the tooltip — never clutter the row with it.
    job_id = str(getattr(job, "id", ""))
    full_title = str(job.filename) + (f"  ·  {job_id}" if job_id else "")

    if session is not None:
        sid = _attr(session.id)
        pid = _attr(job.printer_id)
        view = f"/api/v1/printers/{pid}/timelapses/{sid}/view"
        row_cls = "job-row clickable"
        row_extra = f' data-href="{view}" tabindex="0" role="link" title="Open this print&apos;s timelapse"'
    else:
        row_cls = "job-row"
        row_extra = ' title="No timelapse recorded for this print"'

    # The whole row is the link — the filename is plain text (no nested anchor).
    file_html = (
        f'<span class="fname" title="{_attr(full_title)}">'
        f'{_icon("file")}<span class="ft">{_esc(job.filename)}</span></span>'
    )

    return (
        f'<tr class="{row_cls}"{row_extra}>'
        f'<td class="c-file">{file_html}</td>'
        f'<td class="c-printer"><code class="pcode" title="{_attr(job.printer_id)}">{_esc(job.printer_id)}</code></td>'
        f'<td class="c-started">{_time_tag(job.started_at)}</td>'
        f'<td class="c-duration">{_esc(duration)}</td>'
        f'<td class="c-status"><span class="pill {chip[0]}"><span class="pill-dot" aria-hidden="true"></span>{_esc(chip[1])}</span></td>'
        "</tr>"
    )


def _recent_prints_html(jobs: List[Any], session_by_job: Dict[str, Any]) -> str:
    if not jobs:
        return (
            '<div class="empty">'
            f'<span class="empty-ic">{_icon("cube")}</span>'
            "<strong>No prints recorded yet</strong>"
            "<p>Prints from your fleet will appear here as they run.</p>"
            "</div>"
        )
    rows = "".join(_job_row_html(j, session_by_job.get(str(j.id))) for j in jobs)
    return (
        '<div class="table-scroll"><table class="data-table tbl-prints">'
        '<thead><tr>'
        '<th class="c-file">File</th><th class="c-printer">Printer</th>'
        '<th class="c-started">Started</th><th class="c-duration">Duration</th>'
        '<th class="c-status">Status</th>'
        "</tr></thead>"
        f"<tbody>{rows}</tbody></table></div>"
    )


# --------------------------------------------------------------------------
# Alerts (compact, operationally scannable)
# --------------------------------------------------------------------------

def _alerts_card_html(alerts: List[Alert]) -> str:
    if not alerts:
        return (
            '<div class="empty">'
            f'<span class="empty-ic">{_icon("bell")}</span>'
            "<strong>No alerts</strong>"
            "<p>Your printers are running smoothly.<br>We'll notify you if we detect any issues.</p>"
            "</div>"
        )
    rows = []
    for al in alerts[:5]:
        sev = al.severity.value if hasattr(al.severity, "value") else "info"
        sev_cls = {"critical": "bad", "warning": "warn", "info": "info"}.get(sev, "info")
        st = al.status.value if hasattr(al.status, "value") else "active"
        pill = {"active": ("pill bad", "Active"), "acknowledged": ("pill warn", "Acknowledged"),
                "resolved": ("pill ok-ghost", "Resolved")}.get(st, ("pill bad", "Active"))
        rows.append(
            "<tr>"
            f'<td><span class="sev-dot {sev_cls}" title="{_esc(sev)}"></span></td>'
            f'<td><span class="ev-type">{_esc(_humanize(al.alert_type))}</span></td>'
            f'<td class="c-printer"><code>{_esc(al.printer_id)}</code></td>'
            f"<td>{_time_tag(al.created_at)}</td>"
            f'<td class="c-status"><span class="{pill[0]}">{_esc(pill[1])}</span></td>'
            "</tr>"
        )
    more = f'<p class="ev-note">Showing {min(len(alerts), 5)} of {len(alerts)} recent alerts.</p>' if len(alerts) > 5 else ""
    return (
        '<div class="table-scroll"><table class="data-table ev-table">'
        "<thead><tr><th></th><th>Alert</th><th>Printer</th><th>When</th><th class=\"c-status\">Status</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>{more}"
    )


# --------------------------------------------------------------------------
# Outbox & delivery
# --------------------------------------------------------------------------

def _outbox_card_html(
    counts: Dict[str, int],
    delivery_enabled: bool,
    oldest_pending: Optional[datetime],
) -> str:
    pending = int(counts.get("pending", 0))
    delivering = int(counts.get("delivering", 0))
    delivered = int(counts.get("delivered", 0))
    failed = int(counts.get("failed_dlq", 0))
    total = pending + delivering + delivered + failed

    cells = [
        ("pending", "Pending", pending),
        ("delivering", "Delivering", delivering),
        ("delivered", "Delivered", delivered),
        ("failed", "Failed (DLQ)", failed),
    ]
    cells_html = "".join(
        f'<div class="qcell q-{key}"><span class="qval" data-live="q-{key}">{value}</span>'
        f'<span class="qlbl">{label}</span></div>'
        for key, label, value in cells
    )

    if delivery_enabled:
        notice = (
            '<div class="notice ok">'
            f'{_icon("check")}<span><strong>Webhook delivery enabled</strong> — events are '
            "pushed to Hermes (signed with <code>EVENT_SECRET</code>).</span></div>"
        )
    else:
        notice = (
            '<div class="notice warn">'
            f'{_icon("alert")}<span><strong>Webhook delivery is disabled</strong> — events are '
            "queued until <code>events.delivery</code> is enabled and Hermes is subscribed.</span></div>"
        )

    oldest_html = ""
    if oldest_pending is not None:
        oldest_html = (
            f'<p class="qtotal">Oldest undelivered message: {_time_tag(oldest_pending)}</p>'
        )
    return (
        f'<div class="qgrid">{cells_html}</div>'
        f'<p class="qtotal" data-live="q-total">{total} message{"s" if total != 1 else ""} in the outbox</p>'
        f"{notice}{oldest_html}"
    )


# --------------------------------------------------------------------------
# System status bar
# --------------------------------------------------------------------------

def _system_status_html(
    db_health: Dict[str, Any],
    printers_total: int,
    outbox_counts: Dict[str, int],
) -> str:
    db_ok = bool(db_health.get("available"))
    failed = int(outbox_counts.get("failed_dlq", 0))
    if not db_ok:
        tone, title = "sys-red", "System failure"
        caption = "Database is unavailable — check the daemon log."
    elif failed:
        tone, title = "sys-amber", "Needs attention"
        caption = f"{failed} message{'s' if failed != 1 else ''} failed delivery (DLQ)."
    else:
        tone, title = "sys-green", "All systems operational"
        caption = ""
    if not caption:
        caption = (
            f"Monitoring {printers_total} printer{'s' if printers_total != 1 else ''} · "
            "telemetry, alerts and event delivery healthy"
        )
    return (
        '<div class="sys-status">'
        f'<a class="sys-inner {tone}" href="/health" target="_blank" rel="noopener" '
        'title="Open the health summary (JSON)">'
        f'<span class="sys-ic">{_icon("zap")}</span>'
        '<span class="sys-txt">'
        f'<span class="sys-title"><span class="sys-dot" aria-hidden="true"></span>{_esc(title)}</span>'
        f'<span class="sys-caption">{_esc(caption)}</span></span>'
        f'<span class="sys-cta">View health {_icon("chevron_right")}</span>'
        "</a></div>"
    )


# --------------------------------------------------------------------------
# Dashboard route
# --------------------------------------------------------------------------

async def _collect_jobs(request: Request, printers: List[Any]) -> List[Any]:
    job_repo: JobRepository = request.app.state.job_repo
    merged: List[Any] = []
    for p in printers:
        merged.extend(await job_repo.list_for_printer(p.id, limit=15))
    merged.sort(key=lambda j: (j.started_at or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    return merged[:25]


async def _collect_alerts(request: Request, printers: List[Any]) -> List[Alert]:
    alert_repo: AlertRepository = request.app.state.alert_repo
    merged: List[Alert] = []
    for p in printers:
        merged.extend(await alert_repo.list_all(p.id, limit=25))
    merged.sort(key=lambda a: (a.created_at or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    return merged[:25]


async def _collect_events(request: Request, printers: List[Any], limit_per_printer: int = 150) -> List[Any]:
    event_repo: EventRepository = request.app.state.event_repo
    merged: List[Any] = []
    for p in printers:
        merged.extend(await event_repo.list_for_printer(p.id, limit=limit_per_printer))
    merged.sort(key=lambda e: (e.timestamp or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    return merged[:400]


@router.get("/dashboard", response_class=HTMLResponse)
async def get_dashboard(request: Request) -> HTMLResponse:
    """Render the operational dashboard."""
    settings = request.app.state.settings
    state_manager: StateManager = request.app.state.state_manager
    printer_repo: PrinterRepository = request.app.state.printer_repo
    outbox_repo: OutboxRepository = request.app.state.outbox_repo
    timelapse_repo: TimelapseRepository = request.app.state.timelapse_repo
    db: Database = request.app.state.db

    printers = await printer_repo.list_all()
    for p in printers:
        st = state_manager.get_state(p.id)
        if st:
            p.online = st.online
            p.last_seen = st.last_seen

    camera_registry = getattr(request.app.state, "camera_registry", None)
    sessions = await timelapse_repo.list_all_sessions(limit=500)
    session_total = len(sessions)
    session_by_printer: Dict[str, int] = {}
    session_by_job: Dict[str, Any] = {}
    for s in sessions:
        session_by_printer[s.printer_id] = session_by_printer.get(s.printer_id, 0) + 1
        if s.print_job_id:
            session_by_job.setdefault(str(s.print_job_id), s)

    printing_now = 0
    open_alert_total = 0
    printers_online = 0
    device_cards: List[str] = []
    for p in printers:
        st = state_manager.get_state(p.id)
        active_job = state_manager.get_active_job(p.id)
        online = False
        if st:
            online = st.online
            printers_online += 1 if online else 0
            if st.state.value in ("printing", "preparing", "paused"):
                printing_now += 1
        active_alerts = state_manager.get_active_alerts(p.id)
        if not active_alerts:
            try:
                active_alerts = await request.app.state.alert_repo.list_active(p.id)
            except Exception:
                logger.exception("Alert load failed for %s", p.id)
                active_alerts = []
        open_alert_total += len(active_alerts)
        device_cards.append(_printer_card_html(
            printer=p, state=st, active_job=active_job, active_alerts=active_alerts,
            has_camera=bool(camera_registry and camera_registry.get(p.id)),
            session_count=session_by_printer.get(p.id, 0),
        ))

    db_health = await db.check_health()
    outbox_counts = await outbox_repo.get_counts()
    pending_msgs = await outbox_repo.list_pending(limit=1)
    oldest_pending = pending_msgs[0].created_at if pending_msgs else None
    jobs = await _collect_jobs(request, printers)
    alerts = await _collect_alerts(request, printers)

    if printers:
        devices_html = "".join(device_cards)
    else:
        devices_html = (
            '<div class="empty">'
            f'<span class="empty-ic">{_icon("printer")}</span>'
            "<strong>No printers registered</strong>"
            "<p>Add one with <code>bambu-monitor onboard</code> and it will appear here.</p>"
            "</div>"
        )

    # "View all prints" only makes sense against one printer's history (aggregate
    # prints are not exposed by the API); hide it for multi-printer fleets.
    # "View all prints" is a UI destination: the per-printer timelapse gallery
    # (each recorded session is tied to a print job). Aggregated prints across
    # multiple printers have no human page, so the link is shown only for a
    # single-printer fleet.
    all_prints_href = ""
    if len(printers) == 1:
        all_prints_href = f"/gallery?printer={_attr(printers[0].id)}"

    recent_prints_head = (
        '<div class="card-head">'
        '<div class="card-title">'
        f'<h2>{_icon("cube")}Recent Prints</h2>'
        '<span class="card-sub">Latest prints from your fleet — click a row to open its timelapse</span>'
        "</div>"
        + (f'<a class="see-all" href="{all_prints_href}" title="Open the timelapse gallery for this printer">'
           "View all prints →</a>" if all_prints_href else "")
        + "</div>"
    )

    # Live controls live in the top nav (no separate title block on the dashboard).
    nav_extra = (
        f'<span class="updated">{_icon("refresh")}Updated <strong data-live="updated">'
        f'{datetime.now().strftime("%I:%M:%S %p")}</strong></span>'
        '<button type="button" class="btn" id="btn-refresh" title="Refresh now">'
        f'{_icon("refresh")}Refresh</button>'
        '<button type="button" class="btn" id="btn-auto" aria-pressed="true">'
        f'{_icon("play")}<span>Auto-refresh on</span></button>'
    )

    main_html = (
        _summary_cards_html(
            db_health=db_health, printers_online=printers_online, printers_total=len(printers),
            printing_now=printing_now, open_alerts=open_alert_total, session_total=session_total,
        )
        + '<div class="top-grid">'
        '<div class="devcol">'
        f'<div class="dev-list">{devices_html}</div>'
        '<div class="card recent-card">'
        + recent_prints_head
        + f'<div class="card-body">{_recent_prints_html(jobs, session_by_job)}</div></div>'
        "</div>"
        '<aside class="rail">'
        '<div class="card">'
        '<div class="card-head"><div class="card-title">'
        f'<h2>{_icon("bell")}Alerts</h2></div>'
        f'<a class="see-all" href="/events">View all →</a></div>'
        + f'<div class="card-body">{_alerts_card_html(alerts)}</div></div>'
        '<div class="card">'
        '<div class="card-head"><div class="card-title">'
        f'<h2>{_icon("send")}Outbox &amp; Delivery</h2></div>'
        + ('<a class="see-all" href="/api/v1/outbox/status" target="_blank" rel="noopener" '
           'title="Outbox status (JSON)">View details →</a></div>')
        + f'<div class="card-body">{_outbox_card_html(outbox_counts, bool(settings.events.delivery.enabled), oldest_pending)}</div></div>'
        "</aside>"
        "</div>"
        + _system_status_html(db_health, len(printers), outbox_counts)
    )

    page = _PAGE.substitute(
        doc_title="Dashboard",
        ui_css=_UI_CSS,
        nav_html=_nav_html("dashboard", nav_extra),
        main_html=main_html,
        foot_html="",
        script_html=_LIVE_SCRIPT,
    )
    return HTMLResponse(content=page)


# --------------------------------------------------------------------------
# Dedicated event-history page
# --------------------------------------------------------------------------

def _event_row_html(ev: Any) -> str:
    sev = ev.severity.value if hasattr(ev.severity, "value") else "info"
    sev_cls = {"critical": "bad", "warning": "warn", "info": "info"}.get(sev, "info")
    event_type = ev.event_type or ""
    ts = ev.timestamp.timestamp() if ev.timestamp else 0
    sev_label = {"info": "Info", "warning": "Warning", "critical": "Critical"}.get(sev, sev.title())

    # Embed the full event (payload included) for the click-to-inspect modal.
    payload = ev.payload if hasattr(ev, "payload") else {}
    full = {
        "event_id": getattr(ev, "event_id", None),
        "event_type": event_type,
        "severity": sev,
        "timestamp": ev.timestamp.isoformat() if ev.timestamp else None,
        "printer_id": ev.printer_id,
        "payload": payload,
    }
    json_attr = _attr(json.dumps(full, default=str))

    return (
        '<tr data-pid="%s" data-type="%s" data-sev="%s" data-ts="%s" data-json="%s" '
        'title="Click to inspect event JSON" class="ev-row">'
        '<td class="c-sev" style="width:1%%;white-space:nowrap;">'
        '<span class="sev-chip sev-%s"><span class="sev-dot %s"></span>%s</span></td>'
        '<td class="ev-type">%s</td>'
        '<td class="c-printer"><code>%s</code></td>'
        "<td>%s</td>"
        "</tr>"
    ) % (
        _attr(ev.printer_id), _attr(event_type), _attr(sev), "%.3f" % ts, json_attr,
        sev, sev_cls, _esc(sev_label), _esc(_humanize(event_type)),
        _esc(ev.printer_id), _time_tag(ev.timestamp),
    )


@router.get("/events", response_class=HTMLResponse)
async def get_events_page(request: Request) -> HTMLResponse:
    """Dedicated event-history page (printer lifecycle + telemetry events)."""
    printer_repo: PrinterRepository = request.app.state.printer_repo
    printers = await printer_repo.list_all()
    events = await _collect_events(request, printers)

    # --- Filter option builders (all filtering happens client-side over the feed) ---
    printer_options = ['<option value="all">All printers</option>']
    printer_options += [f'<option value="{_attr(p.id)}">{_esc(p.id)}</option>' for p in printers]

    seen: set = set()
    grouped: Dict[str, List[str]] = {}
    for e in events:
        et = e.event_type or ""
        if not et or et in seen:
            continue
        seen.add(et)
        group = et.split(".", 1)[0] if "." in et else "other"
        grouped.setdefault(group, []).append(et)
    group_priority = ["printer", "print", "filament", "timelapse", "system", "other"]

    def _gkey(g: str) -> int:
        return group_priority.index(g) if g in group_priority else len(group_priority)

    type_options = ['<option value="all">All event types</option>']
    for g in sorted(grouped, key=_gkey):
        inner = "".join(
            f'<option value="{_attr(t)}">{_esc(t)}</option>' for t in sorted(grouped[g])
        )
        type_options.append(f'<optgroup label="{_esc(g.title())}">{inner}</optgroup>')

    severity_pairs = [("all", "All severities"), ("info", "Info"), ("warning", "Warning"), ("critical", "Critical")]
    sev_options = "".join(f'<option value="{v}">{label}</option>' for v, label in severity_pairs)

    when_pairs = [
        ("all", "All time"), ("60", "Last hour"), ("1440", "Last 24 hours"),
        ("10080", "Last 7 days"), ("43200", "Last 30 days"),
    ]
    when_options = "".join(f'<option value="{v}">{label}</option>' for v, label in when_pairs)

    if events:
        rows_html = "".join(_event_row_html(e) for e in events)
        table_html = (
            '<div class="table-scroll"><table class="data-table ev-table">'
            "<thead><tr><th>Severity</th><th>Event</th><th>Printer</th><th>When</th></tr></thead>"
            f"<tbody>{rows_html}</tbody></table></div>"
        )
    else:
        table_html = (
            '<div class="empty">'
            f'<span class="empty-ic">{_icon("activity")}</span>'
            "<strong>No events recorded yet</strong>"
            "<p>Printer lifecycle and telemetry events will be listed here.</p>"
            "</div>"
        )

    controls_html = (
        '<div class="ev-controls">'
        '<input type="search" id="ev-q" placeholder="Search events…" aria-label="Search events" '
        'autocomplete="off">'
        f'<select id="ev-printer" aria-label="Filter by printer">{"".join(printer_options)}</select>'
        f'<select id="ev-type" aria-label="Filter by event type">{"".join(type_options)}</select>'
        f'<select id="ev-sev" aria-label="Filter by severity">{sev_options}</select>'
        f'<select id="ev-when" aria-label="Filter by time">{when_options}</select>'
        '<button type="button" class="btn btn-ghost" id="ev-clear" hidden>Clear filters</button>'
        "</div>"
    )

    total = len(events)
    count_text = f"Showing {total} of {total} events" if total else "No events yet"

    modal_html = (
        '<div class="ev-modal" id="ev-modal" role="dialog" aria-modal="true" aria-label="Event details">'
        '<div class="ev-box">'
        '<div class="ev-box-head">'
        '<h3 id="ev-modal-title">Event</h3>'
        '<button type="button" class="ev-box-close" id="ev-modal-close" aria-label="Close">&times;</button>'
        "</div>"
        '<pre id="ev-modal-json"></pre>'
        "</div></div>"
    )

    main_html = (
        '<header class="page-header">'
        "<div>"
        '<a class="see-all" href="/dashboard" style="font-size:12.5px;">'
        f"{_icon('chevron_right')}&nbsp;&nbsp;Back to dashboard</a>"
        "<h1>Event history</h1>"
        '<p class="subtitle">Printer lifecycle &amp; telemetry events across your fleet — '
        "newest first. Use the filters to find what you need.</p>"
        "</div></header>"
        '<div class="card"><div class="card-head">'
        '<div class="card-title"><h2 style="color:inherit;">Event log</h2>'
        f'<span class="card-sub" data-ev-count>{count_text}</span></div></div>'
        '<div class="card-body">'
        + controls_html
        + f'<div class="card-body-inner">{table_html}</div>'
        "</div></div>"
        + modal_html
    )

    filter_script = """
<script>
  var rows = Array.prototype.slice.call(document.querySelectorAll('.ev-row'));
  var countEl = document.querySelector('[data-ev-count]');
  var qEl = document.getElementById('ev-q');
  var pidEl = document.getElementById('ev-printer');
  var typeEl = document.getElementById('ev-type');
  var sevEl = document.getElementById('ev-sev');
  var whenEl = document.getElementById('ev-when');
  var clearEl = document.getElementById('ev-clear');

  function nowMs() { return new Date().getTime(); }
  function inTimeWindow(row, now) {
    var m = whenEl.value;
    if (m === 'all') return true;
    var ts = parseFloat(row.getAttribute('data-ts')) * 1000;
    return (now - ts) <= parseFloat(m) * 60000;
  }
  function activeFilters() {
    var n = 0;
    if (qEl && qEl.value.trim()) n++;
    if (pidEl.value !== 'all') n++;
    if (typeEl.value !== 'all') n++;
    if (sevEl.value !== 'all') n++;
    if (whenEl.value !== 'all') n++;
    return n;
  }
  function applyFilter() {
    var now = nowMs();
    var q = (qEl.value || '').trim().toLowerCase();
    var visible = 0;
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      var show = true;
      if (pidEl.value !== 'all' && r.getAttribute('data-pid') !== pidEl.value) show = false;
      if (show && typeEl.value !== 'all' && r.getAttribute('data-type') !== typeEl.value) show = false;
      if (show && sevEl.value !== 'all' && r.getAttribute('data-sev') !== sevEl.value) show = false;
      if (show && !inTimeWindow(r, now)) show = false;
      if (show && q && (r.textContent || '').toLowerCase().indexOf(q) === -1) show = false;
      r.classList.toggle('event-row-hidden', !show);
      if (show) visible++;
    }
    if (countEl) countEl.textContent = 'Showing ' + visible + ' of ' + rows.length + ' events';
    if (clearEl) clearEl.hidden = activeFilters() === 0;
  }
  [qEl, pidEl, typeEl, sevEl, whenEl].forEach(function (el) {
    el.addEventListener(el === qEl ? 'input' : 'change', applyFilter);
  });
  if (clearEl) clearEl.addEventListener('click', function () {
    qEl.value = '';
    pidEl.value = 'all'; typeEl.value = 'all'; sevEl.value = 'all'; whenEl.value = 'all';
    applyFilter();
  });

  // Localize relative timestamps once.
  var times = document.querySelectorAll('time.rel');
  for (var j = 0; j < times.length; j++) {
    var iso = times[j].getAttribute('datetime');
    var d = new Date(iso);
    if (!isNaN(d.getTime())) { times[j].textContent = d.toLocaleString(); times[j].title = ''; }
  }
  // Click a row to inspect the raw event JSON in a modal.
  var modal = document.getElementById('ev-modal');
  var modalTitle = document.getElementById('ev-modal-title');
  var modalPre = document.getElementById('ev-modal-json');
  var modalClose = document.getElementById('ev-modal-close');
  function closeModal() { if (modal) modal.classList.remove('open'); }
  if (modal) {
    rows.forEach(function (row) {
      row.addEventListener('click', function () {
        var raw = row.getAttribute('data-json');
        if (!raw) return;
        var data;
        try { data = JSON.parse(raw); } catch (err) { return; }
        modalTitle.textContent = (data.event_type || 'event') + '  ·  ' + (data.printer_id || '') + '  ·  ' + (data.timestamp || '');
        modalPre.textContent = JSON.stringify(data, null, 2);
        modal.classList.add('open');
      });
    });
    modal.addEventListener('click', function (e) { if (e.target === modal) closeModal(); });
  }
  if (modalClose) modalClose.addEventListener('click', closeModal);
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeModal(); });
  applyFilter();
</script>
"""
    page = _PAGE.substitute(
        doc_title="Event history",
        ui_css=_UI_CSS,
        nav_html=_nav_html("events"),
        main_html=main_html,
        foot_html=_foot_html(),
        script_html=filter_script,
    )
    return HTMLResponse(content=page)


@router.get("/", include_in_schema=False)
async def root_index() -> RedirectResponse:
    """Point the app root at the dashboard."""
    return RedirectResponse(url="/dashboard", status_code=302)

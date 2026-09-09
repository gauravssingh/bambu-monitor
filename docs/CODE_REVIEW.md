# Code Review — bambu-monitor

**Date:** 2025-09-09
**Scope:** All of `src/bambu_monitor/` (~9,000 LOC), `tests/`, `config.yaml`, `.env.example`, `pyproject.toml`
**Method:** Four parallel deep-dive reviews (core/config/API/state, Bambu MQTT/domain, camera/timelapse, storage/delivery/tests) plus repo-hygiene and test-suite verification.
**Baseline:** 125/125 tests pass (2.54s). Python 3.12+, FastAPI, paho-mqtt 2.x, aiosqlite.

---

## Executive summary

The codebase is well-structured: clean layering (domain / state / storage / delivery), fully parameterized SQL, `create_subprocess_exec` everywhere (no shell injection), `hmac.compare_digest` for API tokens, and genuinely good behavior-level tests (outbox FIFO, alert suppression, job duration math). The issues below are what should be fixed before relying on it for long-running unattended prints.

**Critical (4):**

1. DHCP IP change permanently bricks monitoring until restart.
2. One malformed telemetry field silently discards the entire MQTT message — including the filament-runout payload this project exists to detect.
3. RTSP credential sanitizer leaks plaintext passwords containing `@`, `/`, `:` into logs/DB/events.
4. Three compounding event-loop stalls (blocking MQTT connect, global state lock held across multi-second camera captures, synchronous PIL overlay rendering) freeze the whole service.

**High (5):** outbox duplicate delivery, render semaphore deadlock, ffprobe zombie leak, auth path weaknesses (DNS rebinding, `None`/`"testclient"` authorized), XSS in gallery/viewer HTML.

**Medium (13+), Low (20+):** cataloged per module below.

**Recommended fix order:**

1. `update_host` reconnect + telemetry parse tolerance (core-feature breakers)
2. Event-loop stalls (all three fixes together)
3. Credential sanitizer, auth hardening, HTML escaping (small security diffs)
4. Outbox uniqueness + render timeout
5. Config merge (`model_fields_set`), delivery test gaps, hermetic test settings
6. Everything else; add `ruff` + CI first so fixes stay fixed

---

# Part 1 — Core / Config / API / State

Files reviewed in full: `config.py`, `main.py`, `cli/commands.py`, `api/app.py`, `api/routes.py`, `state/manager.py`. Cross-checked against `bambu/client.py`, `timelapse/manager.py`, `timelapse/capture.py`, `timelapse/storage.py`, `camera/client.py`, `storage/database.py`, `storage/repositories.py`.

| # | Severity | Location | Issue |
|---|----------|----------|-------|
| 1 | **High** | `api/app.py:71` | DHCP IP-change handler never actually reconnects to the new IP |
| 2 | **High** | `state/manager.py:196` | Global lock held across DB writes, listener dispatch, and multi-second camera captures |
| 3 | **Medium** | `api/routes.py:90` | `None`/`"testclient"` treated as authorized; no Host/Origin validation → DNS rebinding + fixture string in prod auth |
| 4 | **Medium** | `cli/commands.py:425-446` | `reconnect` hardcodes port 8000, swallows all errors, then prints success claims that were never tested |
| 5 | **Medium** | `config.py:189-195` | Per-printer timelapse merge logic broken — `if p_tl.capture:` etc. are always true, global settings silently nuked |
| 6 | **Medium** | `state/manager.py:263` | Late-arriving print filename churns jobs: same physical print recorded twice, first marked FAILED |
| 7 | **Medium** | `cli/commands.py:453-561` | PID-file lifecycle: EPERM misread as "not running", PID reuse → duplicate daemons / killing arbitrary processes |
| 8 | **Medium** | `api/routes.py:248-270`, `state/manager.py:117` | SSE: unbounded queues, no heartbeat, swallowed errors, no 404 for unknown printer |
| 9 | **Medium** | `api/routes.py:426+`, `:789` | Unescaped HTML/JSON interpolation in gallery & viewer pages (XSS) |
| 10 | **Medium** | `cli/commands.py:76-89, 294-345` | Blocking sync sockets in async funcs; socket leak when TLS wrap fails |
| 11 | **Medium** | `api/app.py:39-43, 76-77` | Background-task failures swallowed at DEBUG — silent state-persistence loss |
| 12 | **Low** | `state/manager.py:678` | `flush_state_to_db` reads state without the lock while `apply_patch` mutates under it |
| 13 | **Low** | `api/app.py:217-221` | Shutdown: non-CancelledError from `await task` skips MQTT cleanup & final flush |
| 14 | **Low** | `config.py:21-30` | Env interpolation on raw YAML text can alter document structure |
| 15 | **Low** | `config.py:44` | Access code allowed in plaintext YAML, inconsistent with keyring used by CLI |
| 16 | **Low** | `routes.py` (4 endpoints), `config.py:193`, `main.py` | Dead/duplicated code |

## 1.1 DHCP IP-change handling reconnects to the *old* address forever — HIGH

**`api/app.py:71`** with **`bambu/client.py:198-205`**

```python
# app.py:66-71
if disc.ip != client.host:
    logger.info("Printer %s (%s) IP changed from %s to %s ...", ...)
    client.update_host(disc.ip)
```
```python
# bambu/client.py:198-205
def update_host(self, new_ip: str) -> None:
    clean_ip = new_ip.strip()
    if clean_ip and clean_ip != self.host:
        self.host = clean_ip          # wrapper field only
        if self._client and self._connected:
            self._client.disconnect() # paho reconnects to ITS stored host
```

`update_host` only mutates the wrapper's `self.host`. The paho client was bound to the old IP at `connect()` time (`client.py:212-216`), and with `reconnect_delay_set(min_delay=1, max_delay=15)` (`client.py:80`) paho's automatic reconnect loop retries **paho's own stored host — the old IP — indefinitely**. `_setup_client()` is only ever called from `start()`, so the new IP never takes effect. Additionally, paho never auto-reconnects after an *explicit* `disconnect()` at all — the network thread exits and the client stays offline forever. After a DHCP lease change the printer goes offline until the daemon is fully restarted, while the log claims the IP was updated.

**Fix:** on IP change, tear down and recreate the paho client (or `disconnect()` + `connect_async(new_host, ...)` + `loop_start()`), not just `disconnect()`.

## 1.2 Global `StateManager._lock` held across camera captures and DB writes — HIGH

**`state/manager.py:196, 554`** with **`timelapse/manager.py:139`**, **`timelapse/capture.py:101-116`**

```python
# state/manager.py:194-196
async def apply_patch(self, patch: TelemetryPatch) -> List[DomainEvent]:
    """Core pipeline: ..."""
    async with self._lock:
        ...
# state/manager.py:554  (inside the lock)
    for evt in generated_events:
        await self._emit_event(evt)
# state/manager.py:163-168  (inside the lock, via _emit_event)
    for listener in list(self._event_listeners):
        try:
            await listener(event)
```

Verified chain: `apply_patch` (lock held) → `_emit_event` → `TimelapseManager.handle_domain_event` (`timelapse/manager.py:139`) → `on_print_layer_changed` → `await worker.trigger_capture(...)` (`timelapse/manager.py:160`) → `await self.camera.capture()` (`timelapse/capture.py:101-116`) — a full RTSP/FFmpeg frame capture taking **seconds**. In `layer`/`hybrid` capture mode, every layer change freezes the *entire* state pipeline for *all* printers — MQTT telemetry from paho's `run_coroutine_threadsafe` submissions (`bambu/client.py:171`) piles up, `print.started/paused/completed` events for other printers are delayed, and the periodic flusher can't observe consistent state. `on_print_completed` similarly `await worker.stop()` (which awaits `camera.close()`) inside the lock. Additionally, `asyncio.Lock` is non-reentrant: any future listener that calls back into `apply_patch` or `reconcile_on_startup` (which also takes the lock, `manager.py:628`) will deadlock.

**Fix:** narrow the lock to the in-memory mutation, dispatch listeners/events after releasing it; make `trigger_capture` enqueue to the worker rather than awaiting the capture inline.

## 1.3 Auth: `None` and `"testclient"` are authorized; no DNS-rebinding defense — MEDIUM

**`api/routes.py:88-92`**

```python
client_host = request.client.host if request.client else None
if settings.application.allow_unauthenticated_loopback and client_host in {None, "127.0.0.1", "::1", "testclient"}:
    return
```

- `client_host is None` (missing/odd proxy info) is treated as **authorized**.
- `"testclient"` is a Starlette test fixture value hardcoded into production auth logic — test-only artifact in a security path (also makes the loopback bypass trivially gameable if any reverse proxy ever fronts this).
- No `Host`/`Origin` header validation. A loopback-only bind does not stop DNS rebinding: a remote page at `evil.com` rebinding to `127.0.0.1` passes this check with full API access in the victim's browser — including `POST /api/v1/printers/{id}/telemetry` (`routes.py:284`), which fabricates print jobs, alerts, and events **and enqueues webhook delivery to the configured external Hermes endpoint** via `_emit_event` (`state/manager.py:150`).

**Fix:** drop `None`/`"testclient"` from the allowlist, validate `Host` is loopback, and consider a per-session CSRF token for browser-reachable HTML endpoints.

## 1.4 `cmd_reconnect` reports success it never verified; hardcoded port 8000 — MEDIUM

**`cli/commands.py:423-446`**

```python
# commands.py:425
resp = await client.post(f"http://127.0.0.1:8000/api/v1/printers/{printer_id}/reconnect")
...
except Exception:
    pass
...
# commands.py:444-446 (fallback path)
if reachable:
    print("✓ TLS handshake established to printer")
    print("✓ Authentication parameters verified")
```

- Port 8000 is hardcoded; a daemon started with `--port 9000` is never found, silently falling through.
- Any non-200 (401 when `api_token` is configured, 404, 500) or exception is indistinguishable from "no daemon", and `except Exception: pass` hides why.
- The fallback only proves a TCP+TLS connect, yet prints "✓ Authentication parameters verified" — no MQTT username/password exchange is ever tested. `test_printer_connection`'s `serial` and `access_code` parameters are accepted and completely unused (`commands.py:76`). Same misleading claim at `commands.py:168` in `cmd_onboard`. The command can thus claim a reconnect succeeded when nothing was reconnected.

## 1.5 `get_timelapse_config` merge logic is broken — MEDIUM

**`config.py:181-195`**

```python
# config.py:189-195
if p_tl.capture:
    cfg.capture = p_tl.capture.model_copy(deep=True)
if p_tl.video:
    cfg.video = p_tl.video.model_copy(deep=True)
if hasattr(p_tl, "overlay") and p_tl.overlay:
    cfg.overlay = p_tl.overlay.model_copy(deep=True)
if p_tl.retention:
    cfg.retention = p_tl.retention.model_copy(deep=True)
```

`capture`, `video`, `overlay`, and `retention` are required fields on `PrinterConfig.timelapse` with `default_factory` — they are **always present, and Pydantic BaseModel instances are always truthy**. So the moment a printer has *any* `timelapse:` section, global `capture`/`video`/`overlay`/`retention` settings are unconditionally replaced by the per-printer section's *defaults* rather than inherited. E.g., global `video.fps: 60` + a printer section containing only `enabled: false` silently yields fps 30.

**Fix:** use `model_fields_set` to detect explicitly-set keys, or do a field-wise merge. Related: `if p_tl.storage_dir and p_tl.storage_dir != "./data/timelapses":` (`config.py:181`) compares against a literal sentinel instead of set-fields; and `hasattr(p_tl, "overlay")` (`config.py:193`) is dead code — always true.

## 1.6 Job churn from late-arriving filenames: one print → two DB jobs, first marked FAILED — MEDIUM

**`state/manager.py:263` + `:152-190`**

```python
# manager.py:263
filename = patch.subtask_name or "print"
...
# manager.py:184-190 (_supersede_job)
job.transition_to(JobStatus.FAILED, patch.timestamp)   # not actually a failure
job.metadata["superseded_by"] = {...}
```

The first telemetry report of a print frequently lacks `subtask_name`, so the job starts as filename `"print"`. When the name arrives on a later report, `_job_identity_changed` fires (sanitized-name mismatch), the job is transitioned to **FAILED** and persisted, and a second job is created. Result: two `print_jobs` rows per physical print, failure stats/timelapse sessions keyed to a job that never failed.

**Fix:** treat a name-only change as an in-place rename (update `filename`, keep identity) unless `subtask_id`/`task_id` genuinely differ.

## 1.7 PID-file service management: wrong liveness checks, PID-reuse kill risk — MEDIUM

**`cli/commands.py:453-561`**

```python
# commands.py:457-461
def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):   # EPERM also lands here
        return False
# commands.py:561
os.kill(pid, signal.SIGKILL)
```

- `os.kill(pid, 0)` raises `PermissionError` (EPERM) for a *live* process owned by another user — `_is_pid_alive` reports it dead. `service start` then spawns a second daemon; `service stop` unlinks the PID file while the real one keeps running.
- After a timeout, `service stop` SIGKILLs the recorded PID with no verification it's still the daemon (PID reuse → can kill an arbitrary process). Verify against process command line or start time.
- `PID_FILE`/`LOG_FILE` are CWD-relative (`./data/...`, `commands.py:453-454`): `service start` from `~/projects` and `service stop` from `~` operate on different files entirely. Anchor to a fixed location.
- Minor: `except (OSError, ProcessLookupError)` — `ProcessLookupError` *is* an `OSError`; and the `log_fd` opened at `commands.py:490` is never closed in the parent CLI process.

## 1.8 SSE endpoint: unbounded queues, swallowed errors, no 404 — MEDIUM

**`api/routes.py:248-270`**, **`state/manager.py:112-120`**

```python
# routes.py:264-268
except (asyncio.CancelledError, GeneratorExit):
    pass                      # CancelledError swallowed instead of re-raised
except Exception:
    return                    # real errors vanish with no logging
```

- Each subscriber gets an **unbounded** `asyncio.Queue` (`manager.py:117`). A client that stays connected but stops consuming grows memory without limit — no `maxsize` + drop-oldest policy and no heartbeat to detect dead clients.
- `except Exception: return` hides genuine failures with zero logging.
- `stream_events` never validates the printer exists: `GET .../{unknown-id}/events/stream` returns 200 and then hangs silently forever instead of 404 (every other route 404s).
- `request.is_disconnected()` is only checked *after* an event arrives, so a disconnect with no traffic is only noticed when Starlette cancels the generator.

## 1.9 Unescaped interpolation into HTML and inline `<script>` (XSS) — MEDIUM

**`api/routes.py:426+`, `:789`**

```python
# routes.py:426-433 (gallery)
cards_html += f"""... <div ...>{s.id}</div> ... <strong>{s.print_job_id}</strong> ..."""
# routes.py:789 (viewer)
report_json = json.dumps(report.model_dump(mode="json"))
... <script id="correlation-data" type="application/json"> {report_json} </script>
```

`printer_id` (a URL path parameter, decoded by Starlette before matching), session IDs, `print_job_id`, and anomaly `description` are f-string-interpolated into HTML without escaping; `json.dumps` does **not** escape `</script>`, so any string in the correlation report containing `</script>` breaks out of the script tag. These pages render in the user's authenticated browser; a printer-supplied filename (from gcode telemetry, reachable via the telemetry injection endpoint) or a crafted URL can inject script.

**Fix:** use `html.escape()` for text interpolation and `report_json.replace("</", "<\\/")` for the JSON blob.

## 1.10 Blocking synchronous sockets inside `async def` — MEDIUM

**`cli/commands.py:76-89, 294, 307-308, 345`**

```python
# commands.py:83-84
sock = socket.create_connection((ip, port), timeout=timeout)   # blocks the event loop
tls_sock = ssl_ctx.wrap_socket(sock)
```

`test_printer_connection`, all of `cmd_doctor`'s probes, and the Hermes endpoint check use blocking `socket`/`ssl` directly inside coroutines run under `asyncio.run()` (`main.py`). `doctor` over a handful of unreachable printers (2–3 s timeouts each, plus TLS) stalls the loop for tens of seconds — freezing any concurrent coroutine and delaying Ctrl+C handling. Also a resource leak: when `wrap_socket` raises, `sock` (created at `:83` / `:307`) is never closed.

**Fix:** wrap in `asyncio.to_thread` (and close `sock` in `finally`), or use `loop.open_connection` + `ssl`.

## 1.11 Background-task failures swallowed at DEBUG — MEDIUM

**`api/app.py:39-43, 71-77`**

```python
# app.py:39-43 (_periodic_state_flusher)
for printer_id in list(state_manager._states.keys()):
    try:
        await state_manager.flush_state_to_db(printer_id)
    except Exception as exc:
        logger.debug("Periodic state flush error for %s: %s", printer_id, exc)
```

If SQLite becomes unavailable (disk full, locked, path removed), the flusher silently fails every 5 s at DEBUG — invisible at the default INFO level while `/health` may still report OK. After a crash, **all in-memory state since the last successful flush is lost with no trace**. Same pattern for the IP tracker (`app.py:76-77`) and the event listeners (`state/manager.py:165-169`) — a crashed timelapse listener produces zero evidence.

**Fix:** log at WARNING/ERROR (at least after N consecutive failures), and escalate flush failures into health status.

## 1.12 Unlocked read of state being mutated under the lock — LOW

**`state/manager.py:678-682`** vs `:196`

`flush_state_to_db` reads `self._states[printer_id]` and serializes it (`update_current_state`) without acquiring `self._lock`, while `apply_patch` mutates those same objects under the lock. A flush concurrent with a patch can persist a torn snapshot (e.g. `online=True` with stale `last_seen`/partial `print` snapshot). Acquire the lock (or copy state under it) in the flusher.

## 1.13 Shutdown can skip MQTT cleanup — LOW

**`api/app.py:217-221`**

```python
for task in (flush_task, discovery_task):
    if task:
        try:
            await task
        except asyncio.CancelledError:
            pass
```

If a background task already died from a non-CancelledError, `await task` re-raises here, skipping the MQTT `client.stop()` loop and the final state flush below. Catch `Exception` too (or `contextlib.suppress`). Also note `client.stop()` calls paho `loop_stop()`/`disconnect()` synchronously in the event loop — blocking, but acceptable during shutdown.

## 1.14 Env-var interpolation on raw YAML text — LOW

**`config.py:21-30, 214-215`**

`${VAR}` substitution runs on the whole file *before* `yaml.safe_load`, so an env value containing YAML metacharacters (`: `, `#`, quotes, newlines — entirely plausible for access codes or RTSP URLs with credentials) silently restructures the document (e.g. a value `1234: 5` injects a new key). Interpolate after parsing (walk the loaded tree and substitute string leaves), or document the constraint.

## 1.15 Plaintext access code in YAML — LOW

**`config.py:44`**

`PrinterConfig.access_code` reads from YAML, while the CLI deliberately uses the OS keyring (`commands.py:88` `store_access_code`, and `bambu/client.py:63` falls back to it at runtime). The YAML path is a second, weaker secret store on disk. Consider deprecating the YAML field in favor of keyring + the existing `${VAR}` expansion, or at minimum warn when a literal (non-interpolated) access code is present.

## 1.16 Dead / duplicated code — LOW

- **Four near-identical MP4-serving endpoints**: `get_timelapse_video`, `stream_printer_timelapse_video`, `get_timelapse_video_by_id`, `stream_timelapse_video_by_id` (`routes.py`) — same fetch → `get_video_path` → `FileResponse` body repeated 4×; one parameterized handler would do.
- **Dual route mounts**: `@router.get("/printers")` and `@router.get("/api/v1/printers")` (`routes.py:169-170`) plus `/health` at root — two parallel API surfaces to keep in sync; the CLI itself only uses the `/api/v1` and `/health` forms.
- `hasattr(p_tl, "overlay")` (`config.py:193`) and `hasattr(printer_cfg.camera, "stream"/"type")` (`config.py:198-201`) — always true for the configured models; dead guards.
- `test_printer_connection(ip, port, serial, access_code, ...)` — `serial`/`access_code` are never used (`commands.py:76`).
- `main.py` loads config twice on `run` (`main.py:210` then `run_daemon` → `:189`), and `uvicorn.run(..., log_level="info")` (`main.py:202`) overrides the configured `application.log_level` for uvicorn's own logging.
- `commands.py:737`'s `tl_cfg.camera.url or p_cfg.camera.rtsp_url` duplicates the fallback logic already implemented inside `get_timelapse_config` (`config.py:197-201`).

## Verified correct in this scope

- MQTT auth credentials *do* fall back to the keyring at runtime (`bambu/client.py:60-64`), so the app.py `access_code=None` path is fine for CLI-onboarded printers.
- SQL in `cmd_remove` is fully parameterized; repository methods open per-call aiosqlite connections and close them in `finally`; `save_and_enqueue` runs event+outbox in one transaction.
- `hmac.compare_digest` is correctly used for the API token (`routes.py:86`).
- Camera capture uses `asyncio.create_subprocess_exec` with kill+wait timeout enforcement — no blocking subprocess in the API snapshot path.
- Timelapse frame paths are built from `f"{sequence:06d}.jpg"` with an `int` path param — no traversal vector in the frame route.

---

# Part 2 — Bambu MQTT client & domain model

Files reviewed in full: `bambu/client.py`, `bambu/credentials.py`, `bambu/discovery.py`, `bambu/protocol.py`, `domain/alerts.py`, `domain/events.py`, `domain/print_job.py`, `domain/printer.py`, `domain/telemetry.py`. Cross-referenced `api/app.py` and `state/manager.py` to verify lifecycle assumptions. `pyproject.toml` pins `paho-mqtt>=2.0.0`.

| # | Severity | Location | Issue |
|---|----------|----------|-------|
| H1 | **High** | `bambu/client.py:198-205` | `update_host()` permanently kills MQTT connection (see Part 1, §1.1) |
| H2 | **High** | `bambu/client.py:212-219` | Blocking `connect()` on the event-loop thread stalls the whole service |
| H3 | **High** | `domain/telemetry.py:228-279`, `bambu/client.py:169-170` | One malformed telemetry field silently discards the entire message |
| M1 | **Medium** | `bambu/client.py:161-166` | Any non-pause report-topic message cancels the pending pause patch — including the pushall ACK |
| M2 | **Medium** | `bambu/client.py:150-183` | Cross-thread race on `_awaiting_pause_detail` / `_pending_pause_patch` |
| M3 | **Medium** | `bambu/client.py:109-174` | Fire-and-forget `run_coroutine_threadsafe` — no backpressure, unretrieved futures |
| M4 | **Medium** | `domain/telemetry.py:121-148` | `_detect_filament_runout`'s AMS tray-matching block is dead code; docstring contradicts implementation |
| M5 | **Medium** | `domain/telemetry.py:243-246` | Remaining-time unit heuristic misclassifies ≥10,000-minute prints by 60× |
| M6 | **Medium** | `bambu/credentials.py:19` | Fallback credential store path is CWD-relative; no file locking |
| L1-L9 | **Low** | multiple | stop-order, keyring dead checks, discovery identity bugs, ID collisions, duplicated `utc_now` |

## 2.1 (H2) Blocking `connect()` executes on the asyncio event-loop thread

**`bambu/client.py:212-219`**

```python
self._client.connect(self.host, self.port, keepalive=60)
logger.info("TCP/TLS connection to %s:%d established", self.host, self.port)
...
self._client.connect_async(self.host, self.port, keepalive=60)
```

`start()` is called synchronously from the async FastAPI lifespan (`api/app.py:181: client.start()`). paho's `connect()` performs DNS resolution, TCP connect **and the TLS handshake** synchronously. If a printer is powered off/unreachable, this blocks the event loop for the OS connect timeout (can be 30–75 s) per printer, serially for every configured printer — freezing the entire API and all background tasks during startup. The `connect_async()` fallback at line 218 exists but is only used *after* the blocking attempt already stalled the loop.

**Fix:** always use `connect_async()` + `loop_start()` (which already handles retry via `reconnect_delay_set`), and drop the synchronous `connect()` path.

## 2.2 (H3) One malformed telemetry field silently discards the entire message

**`domain/telemetry.py:228-229, 233, 237, 246, 251, 259, 267, 279`** + **`bambu/client.py:169-170`**

```python
# telemetry.py:278-279
if error_code is None and "print_error" in payload:
    error_code = int(payload["print_error"])
```
```python
# client.py:169-170
except Exception as exc:
    logger.debug("Error parsing MQTT payload for %s: %s", self.printer_id, exc)
```

`TelemetryPatch.from_raw` uses bare `int(...)` / `float(...)` on device-supplied fields with no guard: `int(payload["mc_percent"])` (229), `int(payload["layer_num"])` (233), `int(payload["total_layer_num"])` (237), `int(val)` on `mc_remaining_time` (246), `float(payload["nozzle_temper"])` (251), `float(payload["bed_temper"])` (259), `float(payload["chamber_temper"])` (267), `int(payload["print_error"])` (279). Any of these being a float-string (`"12.5"`), hex-string, or otherwise non-int-coercible value raises — and `_on_message` then drops the **whole patch** at DEBUG level (invisible in default logs). That means one bad field can suppress the printer's `online=True`, `gcode_state`, and — critically — the filament-runout payload the code exists to detect.

The most pointed case: the code's own comment (`telemetry.py:103`) documents Bambu reporting `print_error=0x07ff8011` on the A1 mini. `_detect_filament_runout` deliberately uses `_as_int` (base-0 parsing) to handle hex strings (e.g. `telemetry.py:96`), but `from_raw:279` uses bare `int()`, which **raises** on `"0x07ff8011"`. So on exactly the runout scenario the module was written for, the patch can be discarded before it ever reaches `StateManager.apply_patch` if the firmware sends `print_error` as a hex string.

**Fix:** use the existing `_as_int` helper (and a tolerant float equivalent) for all device fields, and log parse failures at WARNING in `_on_message`.

## 2.3 (M1) Any non-pause report-topic message cancels the pending pause patch

**`bambu/client.py:161-166`**

```python
if is_pause and self._awaiting_pause_detail:
    self._awaiting_pause_detail = False
    self._pending_pause_patch = None
elif not is_pause:
    self._awaiting_pause_detail = False
    self._pending_pause_patch = None
```

The pause-debounce path (149-159) stashes a sparse PAUSE delta and sends a pushall, intending to wait up to 2.5 s for the full report. But Bambu printers acknowledge commands on the **report topic** (`{"command":"pushall", ...}` with no `print` key, no `gcode_state`). That ACK hits the `elif not is_pause` branch and silently discards the stashed pause patch before the full status arrives. Bambu also emits frequent unrelated deltas (AMS pushes, module state) which trigger the same cancellation. The pause event then only surfaces if the *full* report happens to carry `gcode_state=PAUSE` afterward — the debounce logic is effectively defeated by normal traffic.

**Fix:** only cancel the pending patch on a message that actually carries authoritative print status (e.g., `is_full_status` or presence of `gcode_state`), not on any non-pause message.

## 2.4 (M2) Cross-thread race on `_awaiting_pause_detail` / `_pending_pause_patch`

**`bambu/client.py:150-156` vs `176-183`**

```python
# paho network thread (153-156)
self._awaiting_pause_detail = True
self.send_pushall()
self.loop.call_soon_threadsafe(
    lambda: self.loop.call_later(2.5, self._flush_pending_pause))
# event-loop thread (176-183)
def _flush_pending_pause(self) -> None:
    if self._awaiting_pause_detail and self._pending_pause_patch:
        patch = self._pending_pause_patch
```

These two flags/patches are written on the paho network thread and read-and-cleared on the asyncio loop thread (`call_later` callback) with no lock. The check-then-act in `_flush_pending_pause` can interleave with `_on_message` clearing/overwriting the patch: e.g. the check passes, then `_on_message` sets `_pending_pause_patch = None`, then `_submit_patch(None)` schedules `apply_patch(None)` → the coroutine raises `AttributeError` inside a detached future (exception never observed — see 2.5). Conversely, a pause delta arriving exactly at flush time can be double-submitted or lost. Rare but real; needs a `threading.Lock` around the pending-pause state machine.

## 2.5 (M3) Fire-and-forget `run_coroutine_threadsafe` — no backpressure, unretrieved futures

**`bambu/client.py:109-111, 131-133, 172-174`**

```python
asyncio.run_coroutine_threadsafe(self.state_manager.apply_patch(patch), self.loop)
```

Every MQTT message schedules an `apply_patch` coroutine that (per `state/manager.py:196`) serializes on an `asyncio.Lock` and awaits SQLite writes plus registered listeners. Nothing bounds the queue of scheduled coroutines — if the loop stalls (timelapse rendering, slow I/O), a printer reporting at 1 Hz or faster can pile up hundreds of stale patches, which then apply in order with no coalescing. Additionally, the returned `concurrent.futures.Future` is discarded everywhere, so any exception inside `apply_patch` only surfaces later as "Future exception was never retrieved" GC noise.

**Fix:** add a bounded queue/single-flight coalescer per printer, and at least log future exceptions via `add_done_callback`.

## 2.6 (M4) `_detect_filament_runout`'s entire AMS tray-matching block is dead code

**`domain/telemetry.py:121-148`**

```python
ams_id = int(unit.get("id", 0)) if str(unit.get("id", "0")).isdigit() else 0
...
absolute_tray_id = str(ams_id * 4 + int(tray_id)) if tray_id.isdigit() else tray_id
if isinstance(tray, dict) and (tray_id == active_id or absolute_tray_id == active_id):
    # Tray `remain` is an estimate/...
    return None, {}
return None, {}
```

Every path through the AMS block — matching the active tray or not — returns `None, {}`. The `ams_id` / `absolute_tray_id` computation and tray iteration have **no observable effect whatsoever**; this looks like a leftover from a version that checked `remain == 0`. Meanwhile the docstring (44-47) still claims the function will "inspect the active AMS/virtual tray only when its remaining amount is explicitly zero", which the code never does. Either the dead block should be removed (and the docstring fixed), or it's a regression where the AMS remain check was accidentally deleted — worth confirming intent.

## 2.7 (M5) Remaining-time unit heuristic misclassifies long prints by 60×

**`domain/telemetry.py:243-246`**

```python
val = payload["mc_remaining_time"]
if val is not None:
    remaining_seconds = int(val) * 60 if int(val) < 10000 else int(val)
```

`mc_remaining_time` is in **minutes**. A print with ≥ 10,000 minutes remaining (~7 days — realistic for large multi-day prints) is interpreted as already-seconds, understating `remaining_seconds` by 60× and corrupting ETA math downstream. The magic-threshold guess is inherently ambiguous; there is no field that disambiguates. At minimum, raise the threshold and document the failure mode, or expose the raw minutes value alongside.

## 2.8 (M6) Fallback credential store path is CWD-relative

**`bambu/credentials.py:19`**

```python
LOCAL_CREDS_FILE = Path("./data/.credentials")
```

The encrypted fallback store resolves against whatever the current working directory happens to be at launch. Run the service from a different directory (or via a systemd unit / launcher with a different CWD) and `get_access_code()` silently reads an empty store, while `store_access_code()` writes a *second* credential file — credentials appear to vanish. Also, the read-modify-write cycle in `store_access_code`/`delete_access_code` has no file locking, so the service and a concurrent CLI invocation can lose updates. **Fix:** anchor the path (e.g., `Path(__file__)`-relative or a config-provided absolute path) and use `fcntl`/`os.replace`-based locking.

## 2.9 Low severity

- **L1 — stop() order** (`bambu/client.py:229-230`): `loop_stop()` before `disconnect()` means the DISCONNECT packet is never transmitted (paho's documented order is `disconnect()` then `loop_stop()`), so the printer-side session lingers until TCP timeout, and `_on_disconnect` never fires. Harmful only at shutdown.
- **L2 — keyring fail-backend dead check** (`credentials.py:58-67`): the fail backend's class is `keyring.backends.fail.Keyring` — `__class__.__name__` is `"Keyring"`, so `"fail" in backend.__class__.__name__.lower()` is always False. The functional set/get/delete probe below is what actually catches it; the name check is dead code. Also `get_credential_store_info()` (48-56) triggers this probe (a keyring write + delete) on every call, which on some backends can pop a UI prompt.
- **L3 — redundant `except (KeyringError, Exception)`** (`credentials.py:109, 136`): `KeyringError` is a subclass of `Exception`; the tuple is equivalent to `except Exception`. Silently swallows *all* keyring errors (e.g., a locked keychain surfaces as "keyring unavailable" at only DEBUG level).
- **L4 — discovery serial fallback** (`discovery.py:41`): `payload.get("dev_id") or payload.get("dev_ip") or payload.get("serial")` — a JSON broadcast with `dev_ip` but no `dev_id` yields `serial == an IP address`. That "serial" becomes the dict key, the MQTT topic (`device/192.168.1.50/report` — never matches anything), and the credential-lookup key. Should return `None` instead of fabricating an identity.
- **L5 — SSDP USN parsing** (`discovery.py:64-69`): USN values are typically `uuid:XXXX::urn:bambulab-com:device:3dprinter:1`; only the `uuid:` prefix is stripped, so the "serial" retains `::urn:bambulab-com:...` and will never equal a real serial — IP tracking silently no-ops for SSDP-derived devices. Also `.split(":")[0]` destroys IPv6 literals in `location`.
- **L6 — alert ID collisions** (`domain/alerts.py:46`): `alert_id = f"alt_{printer_id}_{alert_type}_{int(now.timestamp())}"` — two alerts of the same type/printer within the same wall-clock second get identical IDs. Same pattern in `generate_job_id` (`print_job.py:36`, epoch-second collision for same filename restarted within 1 s). Append a uuid fragment or millisecond precision.
- **L7 — duplicated ID generation & `utc_now`** (`domain/events.py:22, 37-38`): `DomainEvent.create` re-does the `default_factory` ID generation explicitly; one of the two paths is dead. `utc_now()` is defined independently in five modules (`alerts.py:11`, `events.py:16`, `print_job.py:16`, `printer.py:11`, `telemetry.py:12`) — consolidate into a shared `domain` util.
- **L8 — deprecated `asyncio.get_event_loop()`** (`bambu/client.py:47`): default in `__init__` is deprecated (≥3.12 raises RuntimeError when called from a thread with no current loop). The only current caller passes the loop explicitly; latent footgun for out-of-loop instantiation. Use `asyncio.get_running_loop()` or require the parameter.
- **L9 — runout-flag scan order dependence** (`domain/telemetry.py:55-71`): `walk()` returns first-match-wins over arbitrary dict order; a payload containing both `filament_runout: true` and `filament_present: false` resolves by JSON key insertion order, not precedence. The walk also descends into every nested module (AMS payloads, `lights`, etc.), so a boolean named `runout` in an unrelated future module would trigger a filament alert. Constrain the scan to the `print` subtree and define precedence explicitly.

## Notes (verified, not issues)

- **Access-code log exposure:** `client.py:212` logs only `len(self.get_password())` at INFO — the code itself is never logged in these modules. However, `get_password()` is invoked twice during `start()` (line 82 via `username_pw_set` and line 212 for the log line), doubling keyring I/O for cosmetic logging.
- **TLS bypass:** `client.py:78-87` sets `CERT_NONE`/`check_hostname=False` by default, but this is scoped to the printer's self-signed LAN broker and explicitly documented — acceptable for the threat model, though it enables MITM on the LAN segment where the access code (the MQTT password) is transmitted. If LAN trust matters, pin the printer certificate instead.
- **Machine-derived fallback encryption key** (`credentials.py:22-46`): the docstring honestly states it only guards accidental disclosure — correct assessment; not counted as a vulnerability since it's documented and an env-key override exists.
- **`stop()` does not emit an offline patch** — intentional via `_stopped` flag; fine.
- **paho v1/v2 callback compat** (`client.py:71-99`) and `rc.value` handling (`client.py:101`) are correct.
- `protocol.py` payloads (`pushing.pushall`, `print.pause/resume/stop`) match Bambu's documented shapes; `sequence_id` is hardcoded `"0"` everywhere, which prevents correlating command ACKs (relevant to M1's diagnosis) — worth bumping to a counter.

---

# Part 3 — Camera & Timelapse subsystem

Files reviewed in full: all of `camera/` (`client.py`, `config.py`, `exceptions.py`, `models.py`, `registry.py`, `security.py`) and all of `timelapse/` (`capture.py`, `correlation.py`, `manager.py`, `models.py`, `overlay.py`, `renderer.py`, `storage.py`).

| # | Severity | Location | Issue |
|---|----------|----------|-------|
| H1 | **High** | `camera/security.py:7-14` | RTSP credential sanitizer fails on passwords containing `@`, `/`, `:` — leaks plaintext into logs/errors |
| H2 | **High** | `camera/client.py:224, 243-245` | Orphaned ffprobe process on probe timeout — subprocess leak |
| H3 | **High** | `timelapse/renderer.py:190, :57` | FFmpeg render has no timeout and no cancellation cleanup — one hang deadlocks all future renders |
| H4 | **High** | `timelapse/renderer.py:283-296`, `timelapse/overlay.py:64-65` | Overlay render pipeline blocks the event loop for minutes — synchronous PIL over every frame |
| M1 | **Medium** | `timelapse/capture.py:85-131`, `manager.py:147-163, 345-358` | Race: in-flight `trigger_capture` escapes `worker.stop()` — writes frames *after* render starts, can regress COMPLETED → CAPTURING |
| M2 | **Medium** | `timelapse/storage.py:33` | Path traversal via unsanitized `printer_id` in session directory construction |
| M3 | **Medium** | `timelapse/capture.py:187-207` | `except (CameraError, Exception)` misclassifies every failure as a camera outage |
| M4 | **Medium** | `timelapse/correlation.py:157` vs `renderer.py:253-263` | Correlation `video_time_seconds` diverges from actual video time whenever frames were missed |
| M5 | **Medium** | `timelapse/renderer.py:120, 246, 270` | Renderer temp paths keyed only by PID — concurrent renders of the same session corrupt each other |
| M6 | **Medium** | `timelapse/manager.py:453-481` | Startup reconciliation orphans a stale active session belonging to a different job |
| M7 | **Medium** | `timelapse/storage.py:76-85` | Blocking sync file I/O with `os.fsync` on the event loop, every 10 frames and on every state change |
| L1-L6 | **Low** | multiple | render crash on stray `.jpg`, fabricated timestamps, unimplemented heartbeat, backoff math, shared camera close, tmp file leaks |

## 3.1 (H1) RTSP credential sanitizer fails on passwords containing `@`, `/`, and partially `:`

**`camera/security.py:7-14`**

```python
RTSP_CREDENTIAL_PATTERN = re.compile(
    r"((?:rtsp|rtsps)://)(?:([^/\s]*?):)?([^:/@\s]+)@([a-zA-Z0-9_.-]+(?::\d+)?(?:/|\s|$|[?#]))",
```

The password group `([^:/@\s]+)` excludes `@`, `:`, and `/`. Verified empirically:

```
sanitize('rtsp://admin:p@ss@192.168.1.100:554/stream1')
  -> 'rtsp://admin:p@ss@192.168.1.100:554/stream1'      # NO masking — password leaked

sanitize('rtsp://admin:pa/ss@192.168.1.100:554/stream1')
  -> 'rtsp://admin:pa/ss@192.168.1.100:554/stream1'     # NO masking — password leaked

sanitize('rtsp://admin:pa:ss@192.168.1.100:554/stream1')
  -> 'rtsp://admin:pa:***@192.168.1.100:554/stream1'    # partial leak ('pa' exposed)
```

Every "guarantee" built on this function — `CameraError.__init__` sanitization (`exceptions.py:12-14`), `CameraClient.capture()` stderr scrubbing (`client.py:160`), `connect()` (`client.py:83`), `CameraConfig.sanitized_rtsp_url` (`config.py:52-55`) — is bypassed for these passwords. FFmpeg/ffprobe stderr almost always echoes the input URL on connection failure, so this lands in exception messages, which flow to `session.error`, `timelapse.degraded` event payloads (`manager.py:75`), `CameraHealth.error`, and logs. `@` in passwords is extremely common.

**Fix:** parse the URL (e.g., `urllib.parse.urlsplit` + `urlunsplit` with `password="***"`) instead of regex.

## 3.2 (H2) Orphaned ffprobe process on probe timeout — subprocess leak

**`camera/client.py:224, 243-245`**

```python
stdout, _ = await asyncio.wait_for(probe_proc.communicate(), timeout=4.0)
...
except Exception:
    # Optional diagnostics; failure to ffprobe does not invalidate connectivity
    pass
```

When the 4s `wait_for` fires, `asyncio.TimeoutError` is swallowed by the bare `pass`, and the ffprobe process is **never killed and never waited on**. A slow/hung RTSP camera that connects for snapshots but stalls ffprobe (exactly the scenario health checks run against) leaks one zombie ffprobe process (plus open pipes) per `health()` call. Contrast with `capture()` (lines 137-157), which correctly does `kill()` + `await process.wait()` on timeout. Since `except Exception` also hides this, it fails silently forever.

**Fix:** on timeout, `probe_proc.kill(); await probe_proc.wait()` and log at debug level.

## 3.3 (H3) FFmpeg render has no timeout and no cancellation cleanup — one hang deadlocks all future renders

**`timelapse/renderer.py:190`** (and `renderer.py:57` in `validate_video`)

```python
_, stderr = await proc.communicate()
```

`render()` awaits `communicate()` with no `wait_for`, and `TimelapseManager` wraps all renders in `self._render_semaphore` with `max_concurrent` defaulting to **1** (`manager.py:58-59`). A hung ffmpeg (plausible: it reads a `%06d.jpg` glob of thousands of files, or stalls on a corrupt frame) blocks the semaphore forever → every subsequent print's timelapse silently never renders, forever, with no error anywhere. Additionally, the tmp-file cleanup lives in `except Exception` (lines 222-226); `asyncio.CancelledError` is a `BaseException`, so on cancellation (e.g., app shutdown) the ffmpeg process is left running and `.timelapse_{pid}.tmp.mp4` is orphaned.

**Fix:** `wait_for` with a generous timeout + `kill()/wait()` on timeout *and* `CancelledError` (mirroring `client.py:137-157`).

## 3.4 (H4) Overlay render pipeline blocks the event loop for minutes — synchronous PIL over every frame

**`timelapse/renderer.py:283-296`**, **`timelapse/overlay.py:64-65`**

```python
img_bytes = frame.read_bytes()                                  # renderer.py:285 — sync disk read
overlaid_bytes = TelemetryOverlayBurner.burn_hud_to_bytes(...) # sync PIL decode+draw+encode
(temp_dir / f"{idx:06d}.jpg").write_bytes(overlaid_bytes)       # renderer.py:296 — sync disk write
```

`render()` is `async` and runs on the main event loop, but the entire overlay pass is synchronous: JPEG decode, RGBA conversion, alpha compositing, re-encode per frame, plus filesystem reads/writes — for a multi-hour print this is easily thousands of frames, i.e., **minutes of continuous event-loop blockage**. During that window the FastAPI server, MQTT ingestion, and SSE subscribers all stall. It's also O(2×) disk (full frame copies in `.seq_overlay_*`). Compounding it, `_load_scaled_font` is called twice per frame (`overlay.py:64-65`), re-scanning font candidates and re-opening TTF files thousands of times instead of caching.

**Fix:** `asyncio.to_thread` (chunked) and font caching, or pre-burn the overlay at capture time.

## 3.5 (M1) Race: in-flight `trigger_capture` escapes `worker.stop()` — writes frames *after* render starts and can regress a COMPLETED session to CAPTURING

**`timelapse/capture.py:85-96, 101-118, 125-131`**; **`timelapse/manager.py:147-163, 345-358`**

```python
# capture.py:85-92 — stop() only cancels the loop task
self._running = False
if self._task and not self._task.done():
    self._task.cancel()
```

`on_print_layer_changed` (`manager.py:160`) calls `worker.trigger_capture()` **on the event-dispatch task**, not the worker's loop task. `stop()` cancels only `self._task`, so a `trigger_capture` that already passed the `_running` check and holds `self._lock` inside `_capture_one_frame` (capture.py:125-131 checks `_running` *once*, before `await self.camera.capture()` returns) continues after `stop()` returns and after `_finalize_and_render` has begun:

- `save_frame` writes a frame while the renderer has already enumerated the sequence (`storage.list_frames`) → nondeterministic frame inclusion / render of a frame mid-write.
- If retention is `delete_after_video`, the late frame is silently deleted or orphaned.
- Worse: the late capture's recovery branch (capture.py:130-141) calls `session.transition_to(TimelapseStatus.CAPTURING)` and the `on_frame` callback (manager.py:231-235) persists the session — **after** `_finalize_and_render` set it to COMPLETED — regressing the persisted session/manifest status for a finished print.

**Fix:** `stop()` must wait for in-flight `_capture_one_frame` (acquire the worker lock, or track and await the executing capture), and `_capture_one_frame` should re-check `_running` after `await self.camera.capture()` returns, before persisting anything.

## 3.6 (M2) Path traversal via unsanitized `printer_id` in session directory construction

**`timelapse/storage.py:33`**

```python
session_dir = self.base_dir / printer_id / year / month / day / session_id
```

`session_id` is sanitized by `generate_session_id` (`models.py:71-77`), but `printer_id` is interpolated raw. A printer id of `../../../../tmp/evil` (printer ids originate from config and printer-reported data, then propagate through `TimelapseSession`, `TimelapseManager.on_print_started` → `resolve_session_dir`) writes frames, manifests, and MP4s **outside the timelapse base directory**. `ensure_session_dirs`/`mkdir(parents=True)` happily builds it.

**Fix:** sanitize `printer_id` the same way `generate_session_id` does, or verify the resolved path is under `self.base_dir`.

## 3.7 (M3) `except (CameraError, Exception)` misclassifies every failure as a camera outage

**`timelapse/capture.py:187-207`**

```python
except (CameraError, Exception) as exc:   # == except Exception
    self.session.missed_frames += 1
    if self._camera_online:
        self._camera_online = False
        ...
        self.session.transition_to(TimelapseStatus.DEGRADED, error=err_msg)
```

Any exception inside the try block — including `OSError` from `storage.save_frame` (disk full), `append_frame_metadata` JSON failures, or bugs in the telemetry provider — is treated as a *camera* outage: marks the camera offline, increments `camera_outage_count`, transitions to DEGRADED, and emits `timelapse.degraded`. A disk-full condition will be diagnosed as a flaky camera indefinitely.

Also: `frame_count` (capture.py:144-147) is incremented *before* `append_frame_metadata`, so a metadata-write failure leaves `frames.jsonl` permanently missing a record that the correlation engine later silently resolves to empty telemetry (`renderer.py:284` `meta_by_frame.get(frame_num, {})`).

**Fix:** handle `CameraError` distinctly from storage/IO errors; at minimum log non-camera exceptions with `logger.exception`.

## 3.8 (M4) Correlation `video_time_seconds` diverges from actual video time whenever frames were missed

**`timelapse/correlation.py:157`** vs **`timelapse/renderer.py:253-263`**

```python
video_sec = round((frame_num - 1) / fps, 3)   # correlation.py:157 — assumes contiguous frame numbers
```

When gaps exist, the renderer **re-indexes** frames to a contiguous `1..N` sequence (symlinks/copies in `.seq_*`, `start_number=1`), so the actual video position of frame `000010` is `(reindexed_pos-1)/fps`, not `(10-1)/fps`. The correlation report (and anything consuming `video_time_start/end` for anomaly markers) then drifts further from the true video position as more frames are missed. The two modules encode opposite assumptions about frame numbering.

**Fix:** correlation should compute video time from sorted position, matching the renderer's re-index rule.

## 3.9 (M5) Renderer temp paths keyed only by PID — concurrent renders of the same session corrupt each other

**`timelapse/renderer.py:120, 246, 270`**

```python
tmp_video_path = session_dir / f".timelapse_{os.getpid()}.tmp.mp4"
temp_dir = session_dir / f".seq_{os.getpid()}"
```

Nothing prevents two renders of the same session from running concurrently: `reconcile_on_startup` can schedule a FINALIZING retry (manager.py:462-465), and `generate_video` (manager.py:531-552) re-renders *without checking whether a render task is already in `_render_tasks`* or whether the session is still active. Two concurrent `render()` calls for the same session share the same tmp MP4 and `.seq_*` dirs (same PID) → interleaved ffmpeg writes to one file, `tmp_video_path.replace(final_video_path)` racing, and a corrupted or half-overwritten `timelapse.mp4` that `validate_video` may still pass (it probes whichever tmp survived).

**Fix:** include a session/uuid component in temp names and guard `generate_video` against in-flight renders.

## 3.10 (M6) Startup reconciliation orphans a stale active session belonging to a different job

**`timelapse/manager.py:453-481`**

```python
if session and session.print_job_id == active_job.id:
    ...  # reattach / retry render
elif not session:
    ...  # start fresh session
# else: nothing — stale session for a DIFFERENT job is silently dropped on the floor
```

If the DB's active session for this printer belongs to a *previous* job (e.g., the process crashed mid-print, print never officially completed, then a new print starts and the service restarts), neither branch fires: the stale session is never finalized, never removed from `_active_sessions`, and remains "active" in the DB forever — and `get_active_session_for_printer` keeps returning it on every restart.

**Fix:** finalize the stale session (like the `else` branch at line 521-528 does for idle printers) before proceeding.

## 3.11 (M7) Blocking sync file I/O with `os.fsync` on the event loop, every 10 frames and on every state change

**`timelapse/storage.py:76-85`** (and `:50`, `:127`, `:65`)

```python
with open(tmp_file, "w", encoding="utf-8") as f:
    f.write(content)
    f.flush()
    os.fsync(f.fileno())   # blocking syscall in async context
```

`save_manifest` is called from async code paths (manager's `on_frame` callback, `on_degraded`, `_finalize_and_render`, etc.) and performs an `fsync` — a blocking syscall that can take tens of ms or worse on slow disks — directly on the event loop. Same class of issue: `save_frame`'s `write_bytes` (line 50), `append_frame_metadata` (line 127, once per frame), and `list_frames`' `iterdir` over thousands of files (line 65). Individually small, but this runs continuously for the lifetime of every session.

**Fix:** wrap in `asyncio.to_thread` or move persistence off the loop.

## 3.12 Low severity

- **L1 — stray `.jpg` crashes the whole render** (`renderer.py:244-245, 283`): `int(frames[0].stem)` raises `ValueError` on any non-numeric stem (`list_frames` at `storage.py:63-67` filters only dotfiles/non-`.jpg`). One user-dropped `screenshot.jpg` in `frames/` converts into session FAILED — the entire timelapse is discarded. Filter to `^\d{6}\.jpg$` or skip non-numeric stems.
- **L2 — `parse_datetime` fabricates "now"** (`correlation.py:23-38`): corrupt/missing frame timestamps in `frames.jsonl` become the current time with no warning, shifting anomaly correlation (`find_closest_frame` distance math) arbitrarily and invisibly. At minimum log; ideally propagate a marker.
- **L3 — layer-mode "heartbeat" documented but not implemented** (`capture.py:220-223`): the comment promises a 5-minute safety heartbeat; the code just sleeps 1s and loops (also waking 86,400 times/day for nothing). If layer events stop arriving, layer-mode sessions capture zero frames and fail at render with "No frames captured" — a silent failure mode the comment claims is handled.
- **L4 — backoff never materializes when failure latency ≈ interval** (`capture.py:242-243`): `sleep_time = max(0.001, max(interval, backoff_time) - elapsed)` — `elapsed` includes the full ffmpeg timeout (default 5s) which equals the default interval, so the first two backoff tiers are fully consumed by the failure itself and retries are effectively back-to-back until failures exceed ~3×interval. The `elapsed` subtraction makes sense for the success path (line 240) but should not apply to the backoff term.
- **L5 — worker stop closes a shared camera client** (`capture.py:96`; `manager.py:103-119`): `await self.camera.close()` — the camera may be a registry-cached instance reused by the *next* session for that printer. Currently benign because `CameraClient.close()` is a no-op (`client.py:264-266`), but any future transport-holding implementation will have session N's shutdown break session N+1's camera. Workers should not close clients they don't own.
- **L6 — manifest tmp file leaks on crash between write and replace** (`storage.py:78-84`): if the process dies between the fsync and the `replace`, `manifest.json.tmp.<pid>` lingers forever (each new PID creates a new one). Same pattern applies to `.tmp_{seq}_{pid}.jpg` in `save_frame` (lines 49-52) on a disk-full failure. A startup sweep of stale `*.tmp.*` / `.tmp_*` files would close this.

## Notable non-issues (verified)

- **Command injection:** all ffmpeg/ffprobe invocations use `asyncio.create_subprocess_exec` with argument lists, never a shell (`client.py:126`, `client.py:219`, `renderer.py:52`, `renderer.py:180`). URLs and paths are passed as single argv entries — no injection surface.
- **Credential hygiene otherwise:** `capture()`'s timeout/cancel handling (kill + wait) is correct; debug logging uses `sanitized_url`; `CameraError` sanitizes at construction. The sanitizer regex itself is the only leak (H1).
- **Atomic writes:** `save_frame` and `save_manifest` both use tmp-file + `replace()`, and the render finalizes via `tmp_video_path.replace(final_video_path)` on the same filesystem — good.
- **`generate_session_id`** properly sanitizes job/printer components with `re.sub(r"[^\w]+", "_", ...)` before building the session id used in paths (though not the `printer_id` path segment — see M2).

**Priority fix order for this subsystem:** H1 (credential leak) → H3 (render hang deadlocks the semaphore permanently) → H2 (ffprobe zombie leak) → M1 (capture/render race corrupts completed-session state) → M2/M3 → the rest.

---

# Part 4 — Storage & Delivery layer + test quality

Files reviewed in full: `storage/database.py`, `storage/models.py`, `storage/repositories.py`, `delivery/webhook.py`, `delivery/worker.py`, `tests/conftest.py`, and skims of `tests/unit/test_outbox_queue.py`, `test_outbox_worker.py`, `test_alert_lifecycle.py`, `test_job_tracker.py`.

**Verified clean:** No SQL injection — every query is parameterized, including the dynamically-assembled query in `list_for_printer` (`repositories.py:348-373`). Retry classification logic (4xx-except-429 → permanent, 5xx/429/timeout/network → retryable) is correct, and the backoff formula `initial * multiplier^(attempts-1)` capped at `max_backoff_seconds` is mathematically sound. The `save_and_enqueue` explicit `BEGIN` works correctly under aiosqlite defaults (verified empirically).

| # | Severity | Location | Issue |
|---|----------|----------|-------|
| H1 | **High** | `repositories.py:299-335, :378-403`, `database.py:66-81` | No idempotency guard on outbox inserts — duplicate delivery on ambiguous retries |
| H2 | **High** | `tests/unit/test_outbox_worker.py` | Core FIFO-retry guarantee completely untested (test-quality gap) |
| M1 | **Medium** | `worker.py:44, 88-90, 168-174` | Backoff state is process-local — restarts bypass backoff and burn remaining retries |
| M2 | **Medium** | `repositories.py:421-465`, `worker.py:115-131` | Pending→delivering claim is not atomic (no compare-and-set) |
| M3 | **Medium** | `worker.py:71-75` | Worker loop swallows persistent failures at DEBUG level |
| M4 | **Medium** | `tests/conftest.py:97-103` + `config.py:85-86` | `async_client` tests perform real webhook HTTP to localhost:8644 |
| M5 | **Medium** | `tests/unit/test_outbox_worker.py:15-44` | HMAC test never validates the signature |
| L1-L9 | **Low** | multiple | filtered-events-as-delivered, error_message overwrite, 3xx retries, N+1 churn, non-atomic save+enqueue, silent timestamp fallbacks, unimported `Any`, index gaps |

## 4.1 (H1) No idempotency guard on outbox inserts — duplicate delivery on ambiguous retries

**`repositories.py:299-335` (`save_and_enqueue`), `:378-403` (`enqueue`), schema `database.py:66-81`**

```python
# repositories.py:307-328 (save_and_enqueue)
INSERT INTO events (...) VALUES (...)
ON CONFLICT(event_id) DO NOTHING          # deduped
...
INSERT INTO outbox (event_id, printer_id, destination, ...)
VALUES (?, ?, ?, ...)                     # NO conflict guard
```
```sql
-- database.py:66-81: no UNIQUE(event_id, destination) on outbox
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL REFERENCES events(event_id),
    ...
```

If `save_and_enqueue` raises after the commit actually succeeded (e.g. aiosqlite timeout on close, or caller-side timeout), a caller retry inserts a **second outbox row for the same event** → duplicate webhook delivery. Verified: the schema permits two rows with the same `(event_id, destination)` (repro script: 2 inserts succeeded, count = 2). `enqueue()` has the same hole.

**Fix:** `UNIQUE(event_id, destination)` on the outbox table + `ON CONFLICT DO NOTHING` on both insert paths.

## 4.2 (H2) Core FIFO-retry guarantee is completely untested

**`tests/unit/test_outbox_worker.py` (entire file)**

The worker's docstring (`worker.py:28-34`) promises: *"Retrying events on printer A hold back subsequent events for printer A"* and *"exhausted retries transition to FAILED"*. **Neither is tested.** The three worker tests cover only: success path, immediate-400→DLQ, and event filtering. Missing entirely:

- transient failure (500/timeout) → message returns to `PENDING` with `attempts+1` and `error_message` set
- a second event behind a failing one is **not** delivered during backoff (the central FIFO guarantee)
- retry exhaustion → DLQ (only permanent-4xx→DLQ is tested)
- 429 is retried (not DLQ'd)
- a message stuck in `delivering` (worker crash mid-send) is retried on restart — this is the only mechanism preventing permanently-stuck rows, given `peek_next_for_printer` (`repositories.py:426`) deliberately includes `'delivering'`
- `attempts`/`last_attempt_at`/`error_message` bookkeeping is never asserted against the DB after any failure

The one retry-related test that exists asserts counts only:

```python
# test_outbox_worker.py:87-90
await worker.drain_once()
counts = await outbox_repo.get_counts()
assert counts["failed_dlq"] == 1
```

## 4.3 (M1) Backoff state is process-local — restarts bypass backoff and burn remaining retries

**`worker.py:44, :88-90, :168-174`**

```python
self._next_attempt_at: Dict[str, float] = {}   # line 44 — in-memory only
...
backoff = min(self.config.max_backoff_seconds, ...)
self._next_attempt_at[printer_id] = time.time() + backoff   # line 173
```

`attempts` is persisted in the DB, but backoff is not. On restart: (a) every pending message is retried immediately regardless of how recently it failed; (b) a message that had already failed 4 of `retry_attempts=5` times gets exactly one immediate attempt and then goes straight to DLQ (`worker.py:151`: `attempts >= self.config.retry_attempts`) with **zero backoff** after the restart. A crash-loop of the app process converts exponential backoff into a rapid-fire retry hammer on the webhook endpoint and accelerates events into the DLQ.

**Fix:** persist "next attempt not before" in the outbox row, or compute eligibility from `last_attempt_at + backoff(attempts)` in `drain_once`.

## 4.4 (M2) Pending→delivering claim is not atomic (no compare-and-set)

**`repositories.py:421-431, :434-465`**; **`worker.py:115-131`**

`peek_next_for_printer` and the `DELIVERING` status write are two separate connections/transactions, and `update_status` has no status precondition:

```sql
-- repositories.py:447-453
UPDATE outbox
SET status = ?, attempts = coalesce(?, attempts), ...
WHERE id = ?          -- no "AND status = 'pending'" guard
```

Worse, the peek deliberately includes `'delivering'` rows (`:426`), so if a second worker instance or a concurrent `drain_once()` ever exists (a manual-retry API endpoint, a double app-lifespan in tests, a future refactor), both will peek the *same in-flight message* and deliver it concurrently → duplicate delivery and interleaved status overwrites. Nothing in the code enforces the single-writer assumption that makes this safe today. The crash-recovery benefit of including `'delivering'` should be kept, but the claim should be an atomic `UPDATE ... SET status='delivering' WHERE id=? AND status='pending'` with a rows-affected check, and stale-`delivering` rows should be re-eligible only after a timeout.

## 4.5 (M3) Worker loop swallows persistent failures at DEBUG level

**`worker.py:71-75`**

```python
except Exception as exc:
    logger.debug("Outbox worker loop error: %s", exc)
```

A persistent failure (SQLITE_BUSY beyond the 5s `busy_timeout`, disk full, a repo bug) puts the worker into a silent 1s spin with no output at default log levels. The service appears healthy and simply stops delivering. Should be `logger.warning`/`logger.exception`, possibly with escalation after N consecutive failures.

## 4.6 (M4) `async_client` tests perform real webhook HTTP to localhost:8644

**`tests/conftest.py:97-103`** + **`config.py:85-86`**

```python
# conftest.py:97 — no DeliveryConfig override
async def async_client(test_settings: Settings) -> ...:
    app = create_app(test_settings)
    ...
    async with app.router.lifespan_context(app):
```

`DeliveryConfig.enabled` defaults to `True` with `endpoint="http://localhost:8644"`, and the lifespan starts the real `OutboxDeliveryWorker` (`app.py:197-207`). Tests like `tests/integration/test_fixture_pipeline.py:61-109` POST telemetry → events are enqueued via `save_and_enqueue` → the worker makes **real HTTP connection attempts** to `localhost:8644` during the test run, gets connection-refused, and runs real retry/backoff. Non-hermetic, timing-dependent, and nothing asserts the delivery side effects.

**Fix:** the `test_settings` fixture should set `events.delivery.enabled=False` (or point the endpoint at a mock server).

## 4.7 (M5) HMAC test never validates the signature

**`tests/unit/test_outbox_worker.py:15-44`**

```python
assert "X-Hub-Signature-256" in called_headers
assert called_headers["X-Hub-Signature-256"].startswith("sha256=")
```

Only existence and prefix are checked. A broken implementation — wrong key, hashing a re-serialized dict instead of `raw_body`, wrong algorithm — passes this test. It should recompute `hmac.new(secret, body, sha256).hexdigest()` over the captured `content` kwarg and compare. Also, `assert "X-Gitlab-Token" not in called_headers` (line 40) is a stale assertion about a header this codebase never sets — asserts nothing meaningful.

## 4.8 Low severity

- **L1 — filtered events are marked `delivered`** (`worker.py:97-107`): intentional FIFO-unblocking workaround, but it inflates `get_counts()["delivered"]` with events that were never sent and records a `delivered_at` for them. A `skipped`/`filtered` status would keep queue-unblocking semantics without corrupting delivery reporting.
- **L2 — `update_status` unconditionally clears `error_message`** (`repositories.py:447-453`): `error_message = ?` plain overwrite, unlike `attempts = coalesce(?, attempts)`. Any future `update_status(status=DELIVERED, delivered_at=...)` (e.g. a manual retry API) silently erases the recorded failure reason. Should be `coalesce(?, error_message)`.
- **L3 — 3xx responses are retried as transient errors until DLQ** (`webhook.py:58-63`): httpx defaults to `follow_redirects=False`, so a `301/302` (e.g. an http→https redirect on the configured destination) is retried with backoff for all `retry_attempts` and then DLQ'd — a confusing, delayed failure for what is a configuration problem. Treat 3xx as permanent (or enable `follow_redirects=True` deliberately).
- **L4 — N+1 peek + connection churn in the delivery loop** (`worker.py:81-113` + every repository method): `drain_once` opens `list_all` (1 conn), then per printer a peek (1), a `DELIVERING` write (1), and a final write (1) — each a fresh aiosqlite connection running 4 PRAGMAs (`database.py:135-143`). That's `3N+1` connections per poll cycle. Also `drain_once` processes at most **one message per printer per cycle** and `_run_loop` (`worker.py:76-77`) always sleeps the full `poll_interval_seconds` even when a backlog remains — max throughput 1 msg/sec/printer; draining a 1000-event backlog takes ~17 min per printer.
- **L5 — non-atomic `save()` + `enqueue()` invites lost deliveries** (`repositories.py:277-296` vs `:299`): production correctly uses the atomic `save_and_enqueue` (`state/manager.py:154`), but the public `save()`+`enqueue()` combination is two separate transactions on two connections — a crash between them persists the event with no outbox row → **silently lost delivery**. Notably, the unit tests exercise exactly this unsafe path (`tests/unit/test_outbox_queue.py:27-33`), which is also why the atomic production path has zero direct test coverage. Consider deleting `save()`/`enqueue()` as public entry points or documenting them as test-only.
- **L6 — silent timestamp fallbacks mask data corruption** (`storage/models.py:13-21, :33, :67` and all `row_to_*`): `parse_datetime` returns `None` on failure and callers do `parse_datetime(...) or datetime.now(timezone.utc)` — a malformed or missing timestamp silently becomes "now" on read, hiding write-path bugs and corrupting ordering fields. Prefer raising or logging on unparsable values in non-nullable columns.
- **L7 — outbox `created_at` uses the event timestamp, not enqueue time** (`repositories.py:321-333`, `:385-396`): for backdated/delayed events, outbox age and queue-latency metrics are skewed; `created_at` should be wall-clock at insert. (Consistent between both insert paths, so no correctness bug — just misleading.)
- **L8 — `Any` used but not imported** (`database.py:160`): `check_health(self) -> dict[str, Any]` — `Any` is never imported. Harmless at runtime due to `from __future__ import annotations`, but breaks `typing.get_type_hints` and type checkers.
- **L9 — index coverage gaps**: `alerts.get_active_by_type` (`repositories.py:228-238`) filters `(printer_id, alert_type, status IN ...)` but the only index is `(printer_id, status)` (`database.py:53`) — a composite index on `(printer_id, alert_type, status)` would serve both alert queries. `OutboxRepository.list_pending` global form (`repositories.py:405-418`) filters `status` alone; the printer-leading index can't serve it. Currently unused by `src/`, so low priority.

## Test-quality summary

| Gap | Where | Impact |
|---|---|---|
| No retry/backoff/exhaustion/429/stuck-`delivering` tests | `test_outbox_worker.py` | Core delivery guarantees (#1, #2 in worker docstring) unverified — H2 |
| HMAC asserted by prefix only; stale `X-Gitlab-Token` assert | `test_outbox_worker.py:40-42` | Broken signatures would pass — M5 |
| Full-app tests trigger real HTTP to localhost:8644 | `conftest.py:97`, `config.py:85-86`, `test_fixture_pipeline.py` | Non-hermetic, flaky — M4 |
| Atomic `save_and_enqueue` untested; tests use the unsafe `save`+`enqueue` path | `test_outbox_queue.py:27-33` | Production transaction path has no coverage — L5 |
| No test for duplicate `event_id` enqueue | — | Would currently fail (permits duplicates), revealing H1 |
| Brittle `assert len(events1) == 1` exact-count assertions | `test_alert_lifecycle.py:33`, `test_job_tracker.py:39` | Breaks whenever any co-emitted event type is added; filter by type instead |
| `list_for_printer(since=...)`, `update_status` coalesce semantics, timelapse repo | — | Uncovered edge paths |

**What the tests do well:** `test_outbox_queue.py` genuinely verifies per-printer FIFO advance and partition independence at the DB level; `test_job_tracker.py:115` (`duration_seconds == 3600`) and the alert duplicate-suppression sequence (`test_alert_lifecycle.py:44-53`) are strong behavior-level assertions rather than smoke tests.

---

# Part 5 — Repo hygiene, packaging, and process

Findings from the top-level pass (not covered by the module reviews above):

1. **`config.yaml` is committed to git with real device data** — **fixed:** printer host/serial moved to `${BAMBU_HOST}` / `${BAMBU_SERIAL}` env interpolation resolved from the uncommitted `.env`; real values scrubbed from `README.md`, `.env.example`, and test fixtures. `.gitignore` already covers `.env` and `*.local.yaml`.
2. **Default webhook secret shipped in config defaults** — **fixed:** the committed `config.yaml` no longer carries a default secret (`${EVENT_SECRET:}`), `.env.example` uses placeholder values, and the app logs a WARNING when delivery is enabled without a secret. Real values live in the uncommitted `.env`. *Note: old values remain in git history — rotate the webhook secret (and consider the printer's LAN access code) if this repo was ever shared.*
3. **No linter/type-checker configured** — `ruff`/`mypy`/`flake8`/`bandit` are absent from the dev extras. Even a minimal `ruff check` would have caught several findings directly: `except (KeyringError, Exception)` (`credentials.py:109, 136`), unimported `Any` (`database.py:160`), and various truthiness-always-true guards.
4. **No CI** — tests exist and pass (125/125 in 2.54s), but nothing enforces them on push. A one-job GitHub Actions workflow running `ruff` + `pytest` would prevent regressions of every fix made from this review.
5. **`src/bambu_monitor.egg-info/` and `__pycache__/` are present in the working tree** — `*.egg-info/` is gitignored but exists on disk from a local editable/regular install; harmless, but worth confirming the install method is `pip install -e .` so the entry point resolves cleanly.

---

## Appendix — Severity totals

| Severity | Count | Highlights |
|---|---|---|
| Critical/High | 9 | update_host no-reconnect; silent telemetry loss; RTSP credential leak; event-loop stalls ×3; outbox duplicate delivery; render semaphore deadlock; ffprobe zombie leak; auth weaknesses; XSS |
| Medium | ~20 | config merge; capture/render race; path traversal; pause debounce; job churn; PID-file management; SSE issues; blocking sockets; silent background failures; backoff persistence; non-atomic claim; hermetic tests; HMAC test |
| Low | ~25 | see Parts 1-4 tables and low sections |
| Test gaps | 7 | cataloged in Part 4 |

*Every finding above includes exact file:line references; empirical verifications are noted inline (outbox duplicate insert, sanitizer bypass, aiosqlite BEGIN semantics). Findings that were checked and confirmed to be non-issues are explicitly cataloged so they don't get re-flagged in future reviews.*

---

# Fix log — 2025-09-09

Fixes applied in the recommended order. All 137 tests pass; `ruff check src tests` is clean.

| # | Finding | Fix | Tests |
|---|---|---|---|
| 1 | `update_host` never reconnects (DHCP change bricks monitoring) | `bambu/client.py`: `update_host` now fully tears down and recreates the paho client via `stop()`/`start()` | `test_update_host_recreates_client_when_ip_changes` |
| 2 | Silent telemetry patch loss on malformed fields | `domain/telemetry.py`: tolerant `_as_int` (hex + float-strings) / `_as_float` helpers used for all device fields; `client.py` logs parse failures at WARNING; removed dead AMS tray block; remaining-time threshold raised to 100,000 min with documented failure mode | `test_hex_print_error_and_string_typed_fields_are_tolerated`, `test_malformed_numeric_fields_never_raise`, `test_very_long_print_remaining_time_stays_minutes` |
| 3a | Blocking `connect()` stalls event loop | `bambu/client.py`: `start()` uses `connect_async()` exclusively; `stop()` sends DISCONNECT before `loop_stop()` | `test_start_uses_nonblocking_connect_async` |
| 3b | Global state lock held across multi-second camera captures | `state/manager.py`: per-printer pipeline locks (ordering preserved per printer, cross-printer contention eliminated); `flush_state_to_db` snapshots under the lock; `timelapse/manager.py`: layer captures scheduled as background tasks with error logging | updated `test_print_layer_changed_triggers_capture` |
| 3c | Synchronous PIL overlay pass blocks loop for minutes | `renderer.py`: overlay pass via `asyncio.to_thread`; `overlay.py`: font cache (`lru_cache`); `storage.py`: frame/manifest I/O (incl. fsync) off the loop via `to_thread` | suite green |
| 4a | RTSP credential sanitizer leaked passwords with `@`, `:` | `camera/security.py`: rewritten with structural `urlsplit`/`urlunsplit` masking + in-text URL masking for stderr dumps | all 17 camera tests green, manual repro cases masked |
| | ⚠️ **Correction (batch 3, 2025-09-09):** this row's claim of full coverage was wrong. `urlsplit` treats the first unescaped `/` after `://` as the start of the path, so a password containing `/` was never masked at all — verified as a real leak, not just theoretical. Superseded by batch 3 below. | | |
| 4b | Auth: `None`/`"testclient"` authorized; no DNS-rebinding defense | `api/routes.py`: `None` denied, fixture string removed, `Host` header must be loopback; `compare_digest` over encoded bytes; conftest uses loopback base_url | new `tests/integration/test_api_auth.py` (4 tests) |
| 4c | XSS in gallery/viewer HTML | `api/routes.py`: `html.escape` on IDs/filenames/anomaly descriptions; `</` neutralized in correlation JSON blob | suite green |
| 5a | Outbox duplicate delivery | `database.py`: dedupe migration + `UNIQUE(event_id, destination)` index; `repositories.py`: `ON CONFLICT DO NOTHING` on both insert paths, `rowcount`-aware `enqueue` | `test_outbox_rejects_duplicate_event_destination`, `test_save_and_enqueue_is_idempotent_on_retry` |
| 5b | Render hang deadlocks semaphore forever | `renderer.py`: 1h ffmpeg/60s ffprobe timeouts with kill+reap on timeout *and* `CancelledError`; tmp cleanup on cancellation | suite green |
| 5c | ffprobe zombie per health check | `camera/client.py`: probe timeout now kills and waits the process | suite green |
| 6 | Config merge silently discarded global settings | `config.py`: field-wise merge via `model_fields_set`; dead `hasattr` guards removed | `test_per_printer_timelapse_overrides_only_explicit_fields`, `test_printer_without_timelapse_section_inherits_global` |
| 6 | Integration tests hit real `localhost:8644` | `tests/conftest.py`: `DeliveryConfig(enabled=False)` in test settings | suite hermetic |
| 7 | No linter; latent `NameError` in CLI fallbacks | `ruff` added to dev extras + pyproject config (E/F/W); fixed 129 findings including missing `Settings`/`load_config` imports in `commands.py` (every `settings or load_config()` fallback would have raised `NameError`) | `ruff check` clean |

**Still open (low severity / hygiene):** ID collisions (epoch-second), CWD-relative credential/PID paths, keyring dead checks, discovery serial fallback bugs, filtered-events marked "delivered", 3xx webhook retry handling, duplicated `utc_now`, env-var interpolation on raw YAML, blocking CLI sockets, PID-file management, committed `config.yaml` with real device data, default webhook secret, no CI.

---

# Fix log — batch 2 (2025-09-09, medium severity)

Second pass over the remaining medium findings. 148/148 tests pass; `ruff check src tests` clean.

| # | Finding | Fix | Tests |
|---|---|---|---|
| 1 | Capture/render race: in-flight `trigger_capture` escaped `stop()`, could write frames after render started and regress COMPLETED → CAPTURING | `timelapse/capture.py`: `stop()` now waits on the worker lock for in-flight captures; `_capture_one_frame` re-checks `_running`/`_paused` after the multi-second camera capture returns and discards instead of persisting | `test_stop_discards_in_flight_capture` |
| 2 | Outbox backoff lost on restart (crash-loop → rapid retries → fast-track to DLQ) | `delivery/worker.py`: in-memory `_next_attempt_at` removed; eligibility now derived from persisted `attempts`/`last_attempt_at` (survives restarts, honors same schedule across workers) | `test_transient_failure_retries_with_fifo_holdback` |
| 3 | Outbox pending→delivering claim not atomic; stuck `delivering` rows could double-send | `repositories.py`: new `begin_delivery()` compare-and-set (`WHERE status='pending'`) and `reclaim_stale_delivering()` crash recovery called at each drain start | `test_concurrent_claim_prevents_double_delivery`, `test_stale_delivering_rows_are_reclaimed` |
| 4 | Outbox worker loop swallowed persistent failures at DEBUG | `delivery/worker.py`: WARNING, escalating to ERROR after 3 consecutive failures | (log-level) |
| 5 | Core FIFO-retry guarantees untested; HMAC test asserted prefix only | `test_outbox_worker.py`: new tests for transient retry + FIFO holdback, exhaustion→DLQ, 429 retried, stale-`delivering` reclaim, concurrent claim; HMAC now recomputed over the exact transmitted body | 6 new/1 strengthened |
| 6 | Pause debounce defeated: any non-pause report message (incl. the pushall ACK) cleared the pending pause patch; cross-thread flag race | `bambu/client.py`: only messages with a `gcode_state` (authoritative status) cancel the pending pause; pending-pause state machine guarded by `threading.Lock` | `test_pushall_ack_does_not_cancel_pending_pause`, `test_flush_pending_pause_submits_when_no_full_report` |
| 7 | Job churn: late-arriving filename → phantom FAILED job + duplicate | `state/manager.py`: name-only mismatch renames the job in place; supersede reserved for task/subtask-ID mismatches | `test_late_arriving_filename_renames_job_instead_of_superseding`, `test_task_id_mismatch_still_supersedes` |
| 8 | Correlation video time drifted from real video position whenever frames were missed | `timelapse/correlation.py`: video time computed from position in the ordered sequence (matching the renderer's re-index) for timeline points and anomaly end times | suite green |
| 9 | `except (CameraError, Exception)` misclassified disk-full/metadata failures as camera outages | `timelapse/capture.py`: `CameraError` handled as outage; other exceptions counted as a miss with `logger.exception`, camera stays online | suite green |
| 10 | Stale timelapse session from a previous job orphaned forever on restart | `timelapse/manager.py`: reconciliation finalizes a stale active session belonging to a different job before starting fresh | suite green |
| 11 | Concurrent renders of the same session shared PID-keyed temp paths; `generate_video` could race a startup render | `timelapse/renderer.py`: uuid suffix in all temp paths; `generate_video` raises if a render for the session is already in flight | suite green |
| 12 | SSE: unbounded subscriber queues, swallowed stream errors, no 404 for unknown printers | `state/manager.py`: bounded (256) queues with drop-oldest; `api/routes.py`: unknown printer → 404, `CancelledError`/`GeneratorExit` re-raised, exceptions logged | `test_stream_unknown_printer_returns_404` |

**Remaining open (low severity, unchanged):** CWD-relative credential store and PID/log files, keyring fail-backend dead checks, discovery `dev_ip`/SSDP USN serial fallbacks, filtered outbox events marked "delivered" in metrics, 3xx webhook responses retried until DLQ, layer-mode heartbeat unimplemented, duplicated `utc_now()` helpers, env interpolation on raw YAML, blocking CLI sockets in `doctor`, PID-file EPERM/PID-reuse handling, git-history scrubbing/secret rotation (see Part 5 note).

---

# Verification audit + Fix log — batch 3 (2025-09-09)

An independent audit re-read every claim in the two fix logs above against the actual current source (not the log text) to confirm batch 1/2 were correctly applied. 24 of 30 checked claims were correct as stated. Four were not, and none of the four had been disclosed in either "still open" list — they read as fixed when they weren't:

| Finding | Prior claim | Actual gap found |
|---|---|---|
| RTSP sanitizer (§3.1, batch-1 row 4a) | "fully fixed" | Password containing `/` still leaked in plaintext — `urlsplit` ends the netloc at the first unescaped `/`, so the password after it was never even seen by the masking logic. The module's own docstring example didn't match its real output. |
| Config merge `enabled` field (§1.5, batch-1 row 6) | "field-wise merge... only explicit fields override" | `cfg.enabled = p_tl.enabled` was unconditional — the one field the fix's own field-wise treatment skipped. A printer section that set e.g. `video.fps` without setting `enabled` would silently flip a global `enabled: false` to the model default `True`. |
| Background-task failure logging (§1.11) / shutdown cleanup (§1.13) | not claimed fixed, but also missing from both "still open" lists | Genuinely still open — untouched since the original review. |
| Unretrieved MQTT futures (§2.5, M3) | not claimed fixed, but also missing from both "still open" lists | Genuinely still open — untouched since the original review. |

Fixes applied this pass. 149/149 tests pass; `ruff check src tests` clean.

| # | Finding | Fix | Tests |
|---|---|---|---|
| 1 | RTSP sanitizer leaked passwords containing `/` | `camera/security.py`: replaced `urlsplit`/`urlunsplit` with manual parsing — the host never contains `@`, so the **last** `@` before the path is always the true userinfo/host boundary regardless of `@`, `:`, or `/` inside the password; docstring corrected to include a `/`-password example | `test_sanitize_rtsp_url` extended with `/`, `:`, and `@`-in-password cases |
| 2 | Config merge: `enabled` field bypassed the field-wise merge | `config.py`: `cfg.enabled` now only overridden when `"enabled" in p_tl.model_fields_set`, consistent with every other field | `test_per_printer_section_without_enabled_key_inherits_global_enabled` |
| 3 | Background-task failures invisible at default log level | `api/app.py`: periodic state-flush failures now `logger.exception` (was DEBUG); background IP-discovery failures now `logger.warning` (was DEBUG) | suite green |
| 4 | Shutdown could skip MQTT cleanup on a non-`CancelledError` task death | `api/app.py`: task-drain loop now also catches and logs `Exception` instead of only suppressing `CancelledError`, so MQTT `stop()` and the final flush always run | suite green |
| 5 | `list_frames` (`iterdir` over the full frame set) still ran synchronously on the event loop | `timelapse/renderer.py` and `timelapse/manager.py`: both call sites now go through `asyncio.to_thread` | suite green |
| 6 | Fire-and-forget `run_coroutine_threadsafe` — exceptions inside `apply_patch` were invisible (GC-only "Future exception was never retrieved") | `bambu/client.py`: new `_submit_coroutine` helper attaches `add_done_callback` to log any exception at ERROR; used at all three call sites | suite green |

**Still open (unchanged; deliberately not attempted this pass — larger design changes, not gaps in a claimed fix):** bounded queue/coalescer for MQTT patch backpressure (§2.5, the other half of M3), plus everything already listed as open in batches 1 and 2.

---

# External review response + Fix log — batch 4 (2025-09-09)

An external reviewer, working from GitHub's web view (by their own admission, hitting cache misses on some nested source pages), raised four points. Two were checked directly against the pushed commit and found stale; two were real gaps.

| Claim | Verdict | Evidence |
|---|---|---|
| `config.yaml` on main still has `secret: ${EVENT_SECRET:bambu-secret-8f92a4e7c10b42d591}` | **Stale — not true of current `main`** | `git log -p --follow -- config.yaml` shows that exact line only in a commit prior to `93fbf80` (already pushed before this claim was raised); `git fetch && git rev-parse HEAD origin/main` confirmed both at the same SHA with no such line present. The string does still exist earlier in git history — Part 5 already calls this out ("rotate the webhook secret... if this repo was ever shared") — but it is not on `main`. Consistent with the reviewer's own caveat about a stale/cached GitHub view. |
| README's Telegram `chat_id: "1117425083"` looks like a real destination | **Real gap, fixed** | Not a placeholder — a genuine numeric ID sitting in a public example. Replaced with `${TELEGRAM_CHAT_ID}` in the JSON subscription example and `your-chat-id` in the architecture diagram. |
| README's delivery-guarantee wording implies exactly-once | **Real gap, fixed** | "Never sends it again" was happy-path-only phrasing. Section renamed to "Delivery Semantics & Reverse Acknowledgement"; now states this is at-least-once (a dropped ack after Hermes already processed the event causes a resend) and that consumers must dedupe on `event_id`. |
| "I would not assume [outbox backoff, atomic claim, capture/render race, correlation drift, SSE bounds, PID lifecycle, background-failure visibility, CI] were fixed just because the repo progressed" | **Reasonable caution; here's the actual status per item** | See table below — this cross-checks against the batch-1/2/3 tables above using direct source reads, not GitHub's web view. |

| Item | Status |
|---|---|
| Outbox retry backoff surviving restart | Fixed — batch 2, #2 (`worker.py`: eligibility computed from persisted `attempts`/`last_attempt_at`) |
| Atomic PENDING → DELIVERING | Fixed — batch 2, #3 (`begin_delivery()` compare-and-set, `reclaim_stale_delivering()`) |
| Capture/render race | Fixed — batch 2, #1 (`capture.py`: `stop()` waits on the worker lock, re-checks `_running` post-capture) |
| Correlation frame-time drift | Fixed — batch 2, #8 (`correlation.py`: video time from ordered position, matching the renderer's re-index) |
| SSE queue bounds | Fixed — batch 2, #12 (bounded `maxsize=256`, drop-oldest) |
| SSE **heartbeat** | **Was NOT fixed — genuinely missed until now.** Batch 2's SSE fix covered bounded queues, 404 for unknown printers, and re-raising `CancelledError`, but `subscribe_events`'s `await queue.get()` still blocked with no timeout — an idle client (no events flowing) could hold its subscriber queue open indefinitely, exactly as §1.8 originally described. Fixed this pass: `subscribe_events(printer_id, heartbeat_seconds=15.0)` now yields `None` on a timeout tick; the SSE route sends an `: heartbeat` comment and re-checks `request.is_disconnected()` on each tick. New test: `test_subscribe_events_yields_heartbeat_during_idle_period`. |
| PID lifecycle (EPERM misread, PID reuse, CWD-relative paths) | **Still open**, unchanged since the original review (§1.7) — never attempted in any batch. |
| Background-failure visibility | Fixed — batch 3, #3/#4 |
| CI | **Still open.** A `.github/workflows/ci.yml` (ruff + pytest, Python 3.12/3.13 matrix) was drafted and briefly committed, but pushing it requires the `workflow` OAuth scope, which the pushing account's token doesn't have. Left out of `main` rather than pushed with a workaround; add it once auth is sorted. |

Additional fixes applied this pass, prompted by the review:

| # | Finding | Fix | Tests |
|---|---|---|---|
| 1 | `events.delivery.enabled: true` shipped with no way to prevent an accidental unsigned-webhook deployment | `api/app.py`: startup now raises `RuntimeError` (was a `logger.warning`) when delivery is enabled with no `EVENT_SECRET`; `config.yaml`'s shipped default flipped to `enabled: false`; the `DeliveryConfig.enabled` **model** default also flipped to `False` (a config-less install falls back to bare `Settings()`, which must stay safe by default) | `test_startup_refuses_delivery_enabled_without_secret` |
| 2 | SSE idle-disconnect detection (see table above) | `state/manager.py` + `api/routes.py`: heartbeat ticks as described above | `test_subscribe_events_yields_heartbeat_during_idle_period` |
| 3 | Telegram `chat_id` and diagram chat label were real-looking values in a public README | `README.md`: parameterized to `${TELEGRAM_CHAT_ID}` / `your-chat-id` | — |
| 4 | Delivery-guarantee wording implied exactly-once | `README.md`: reworded to state at-least-once + consumer-side `event_id` idempotency | — |

151/151 tests pass; `ruff check src tests` clean.

---

# Fix log — batch 5 (2025-09-09): PID lifecycle (§1.7)

The last code-only item left open from the original review. CI remains open (blocked on the `workflow` OAuth scope — see batch 4).

| # | Finding | Fix | Tests |
|---|---|---|---|
| 1 | `_is_pid_alive` misread EPERM as "dead" — a daemon owned by another user looked stopped, inviting a second daemon / orphaned real one | `cli/commands.py`: `PermissionError` from `os.kill(pid, 0)` now returns `True` (live); only `ProcessLookupError` means dead | `test_is_pid_alive_treats_eperm_as_alive` |
| 2 | `service stop` SIGTERM/SIGKILLed the recorded PID with no identity verification — PID reuse could kill an arbitrary process | New `_pid_is_our_daemon(pid)` checks the process command line (`/proc/<pid>/cmdline` on Linux, `ps -p <pid> -o command=` elsewhere) for the `bambu_monitor` marker before any signal. Stop now: refuses to signal a foreign PID (removes the stale file, kills nothing); if identity is *undeterminable*, sends SIGTERM but skips SIGKILL. `_get_running_pid` also verifies identity, so `service status` no longer reports a reused PID as "Running" | `test_service_stop_refuses_to_kill_reused_pid`, `test_service_stop_skips_sigkill_when_identity_unknown`, `test_service_stop_sigkills_verified_daemon_after_timeout`, `test_service_stop_stops_verified_daemon`, `test_get_running_pid_stale_pid_reuse_reads_as_not_running` |
| 3 | `PID_FILE`/`LOG_FILE` were CWD-relative (`./data/...`) — start and stop from different directories operated on different files | Both now live in a fixed per-user state dir (`~/Library/Application Support/bambu-monitor` on macOS, `~/.local/state/bambu-monitor` elsewhere), overridable via `BAMBU_MONITOR_STATE_DIR`. Migration note: a daemon started *before* this change recorded its PID in the old CWD-relative location and must be restarted once for the new `stop`/`status` to manage it | `test_state_dir_env_override`, `test_state_dir_default_is_absolute_not_cwd_relative` |
| 4 | `log_fd` leaked in the parent CLI process after spawning the daemon | Closed explicitly after `Popen` (child inherits its own copy) | — |
| 5 | Minor from §1.7: `except (OSError, ProcessLookupError)` redundant tuple; stop's bare `except Exception` around SIGKILL | Explicit `ProcessLookupError` / `PermissionError` handling; corrupt/unparseable PID file content is cleaned up by stop | `test_service_stop_cleans_corrupt_pid_file`, `test_service_stop_not_running_removes_stale_pid_file` |

Verified beyond the suite: real `service start --port 8123` → `/health` OK → `service status` shows Running → `service stop` terminates cleanly, no orphaned process; PID/log files resolved to the anchored state dir.

168/168 tests pass; `ruff check src tests` clean.

**Still open:** CI (`.github/workflows/ci.yml` drafted but unpushed — needs a token with the `workflow` scope; batch 4), bounded queue/coalescer for MQTT patch backpressure (§2.5 remainder), plus everything already listed as open in batches 1–2.

# Architecture Simplification Review — bambu-monitor

**Date:** 2026-09-09
**Scope:** All of `src/bambu_monitor/` (~9,650 LOC before this pass), `tests/`, `config.py`, `pyproject.toml`.
**Method:** Full manual read of every module (39 source files, 36 test files), cross-referenced against `docs/CODE_REVIEW.md` (the prior correctness/security pass) to avoid re-litigating already-fixed issues.
**Goal:** Reduce complexity, duplication, and indirection **without changing external behavior** (API routes, config schema, CLI commands, reliability guarantees). This is not a correctness or performance pass.

---

## Architecture Assessment

**Rating: Appropriately engineered, with isolated pockets of over-engineering.**

The layering itself is sound and matches `DESIGN.md §3.11` ("lean abstraction, no premature generality"): MQTT → domain patch → in-memory state → SQLite → domain events → outbox → webhook delivery, with camera/timelapse as a cleanly optional, listener-driven side subsystem. There is no speculative plugin framework, no ORM, no generic "ApplicationService" layer, no unnecessary DI container. Domain models double as both the in-memory representation and the serialization contract — there's no separate DTO layer translating between them.

The complexity that exists is concentrated in a few specific spots: two small, genuinely-redundant class hierarchies (camera clients), one config concept represented by two overlapping schemas (printer camera vs. timelapse camera), a handful of copy-pasted validation/response blocks in the CLI and API layers, and two duplicated state-machine loops in the telemetry correlator. None of these reflect a wrong architecture — they're accumulated copy-paste from iterative feature development (camera → timelapse → correlation were added in that order, per git history, each initially standalone).

---

## Biggest Sources of Complexity (top 10, before this pass)

1. **Two camera config schemas for one concept** — `camera.config.CameraConfig` (full RTSP client config: ffmpeg paths, probe sizes) vs. `config.TimelapseCameraConfig` (type/url/stream only), bridged by explicit fallback logic in `Settings.get_timelapse_config()`. Real duplication, not yet resolved (see Deferred, below).
2. **`BaseRTSPCamera` / `GenericRTSPCamera`** — a 2-level inheritance chain where the "generic" leaf class was byte-for-byte identical to its own base class. *(Fixed this pass.)*
3. **Four near-identical MP4-serving route handlers** in `api/routes.py` (`get_timelapse_video`, `stream_printer_timelapse_video`, `get_timelapse_video_by_id`, `stream_timelapse_video_by_id`) plus 8 more handlers repeating the same "fetch session or 404" preamble. *(Fixed this pass.)*
4. **Two independent implementations of the same thermal-anomaly state machine** in `TelemetryCorrelator.correlate` (hotend vs. bed temperature-drop detection), ~150 lines that could drift independently. *(Fixed this pass.)*
5. **`TimelapseManager`'s repeated persist-triple** (`save_session` + `save_manifest_async`) appearing 15 times across the class. *(Fixed this pass.)*
6. **Duplicated printer-camera validation** in `cmd_camera_snap` / `cmd_camera_test` (18 lines copy-pasted verbatim). *(Fixed this pass.)*
7. **Dual API route mounts** — `/printers` and `/api/v1/printers` both live, plus a bare `/health`. Two parallel surfaces to keep in sync going forward. Not changed (would break existing consumers).
8. **`TimelapseManager.on_print_started`** duplicates ~25 lines of session-creation logic with the "no session found" branch of `reconcile_on_startup` — same operation (create session, resolve dir, persist, start worker) triggered from two different call paths.
9. **Config double-load in `main.py`** — `main()` loaded `Settings` once for CLI dispatch, then `run_daemon()` loaded it again from disk for the `run` path, so the two could theoretically diverge. *(Fixed this pass.)*
10. **A single class-wide lock in `TimelapseManager`** serializes session-lifecycle handling across *all* printers, unlike `StateManager`, which was already migrated to a per-printer lock specifically to prevent one printer's slow handling from stalling another's. Not changed this pass (behavior-preserving but a real scalability nuance — flagged for a future pass with dedicated concurrency tests).

---

## Refactoring Matrix

| Area | Current Design | Problem | Severity | Recommendation | Complexity Saved | Risk | Status |
|---|---|---|---|---|---|---|---|
| `camera/client.py` | `CameraClient` → `BaseRTSPCamera` (empty) → `TapoRTSPCamera`, `GenericRTSPCamera` | `GenericRTSPCamera` added zero behavior over `CameraClient`; `BaseRTSPCamera` existed only to be a pass-through parent | Low | **MERGE**: drop both, `TapoRTSPCamera(CameraClient)` directly, factory returns `CameraClient` for the generic case | 2 classes, 10 LOC | Low | ✅ Done |
| `camera/client.py` | `capture()` + `snapshot()` alias | Two names for identical behavior, used inconsistently across call sites | Low | **MERGE**: keep `capture()`, drop the alias | 1 method, ~15 call-site touches | Low | ✅ Done |
| `cli/commands.py` | `test_printer_connection(ip, port, serial, access_code, timeout)` | `serial`/`access_code` params never read in the body | Low | **SIMPLIFY**: drop unused params | 2 params | Low | ✅ Done |
| `cli/commands.py:cmd_timelapse_camera_test` | `cam_url = tl_cfg.camera.url or (p_cfg.camera.rtsp_url if ...)` | `get_timelapse_config()` already performs this exact fallback; the `or` branch is dead | Low | **REMOVE** the redundant fallback expression | 1 line, one drifted duplicate of business logic | Low | ✅ Done |
| `main.py` | `main()` loads `Settings`, then `run_daemon()` loads it again from disk | Two loads of the same config on the hot path; can silently diverge if disk state changes mid-startup | Low | **SIMPLIFY**: pass the already-loaded `Settings` into `run_daemon` | 1 redundant I/O + config-lookup site | Low | ✅ Done |
| `main.py` | `uvicorn.run(..., log_level="info")` | Hardcoded, ignored the resolved `application.log_level` / `--log-level` | Low | **SIMPLIFY**: pass through the resolved level | Config drift | Low | ✅ Done |
| `cli/commands.py` | `cmd_camera_snap` / `cmd_camera_test` | 18 lines of printer/camera validation copy-pasted verbatim | Medium | **MERGE** into `_resolve_printer_camera()` helper | ~18 LOC | Low | ✅ Done |
| `api/routes.py` | 4 near-identical MP4 handlers + 8 "fetch session or 404" preambles | Same fetch→validate→respond logic hand-copied 12 times | Medium | **MERGE** into `_get_timelapse_session()` + `_timelapse_video_response()` helpers | ~110 LOC | Low (behavior-preserving, verified by existing endpoint tests) | ✅ Done |
| `timelapse/manager.py` | `session.transition_to(...); await self.repo.save_session(session); await self.storage.save_manifest_async(session)` | Identical 2-line persist pattern repeated 15×; risk of a future edit updating the DB but not the manifest (or vice versa) | Medium | **MERGE** into `self._persist(session)` | ~15 LOC, one point of truth for "persist a session" | Low | ✅ Done |
| `timelapse/renderer.py` | `await asyncio.to_thread(storage.save_manifest, session, session_dir)` × 4 | Re-wraps a call the storage layer already exposes as `save_manifest_async` | Low | **SIMPLIFY**: call `storage.save_manifest_async()` | Consistency, 0 net LOC | Low | ✅ Done |
| `timelapse/storage.py` | `append_telemetry_point` / `read_telemetry_points` | Dead code — zero callers, zero tests, a JSONL sidecar (`telemetry.jsonl`) nothing ever writes | Low | **REMOVE** | 25 LOC | Low | ✅ Done |
| `timelapse/correlation.py` | Separate hotend-drop and bed-drop detection loops | Same "sustained below-target temperature" state machine duplicated with different field names/thresholds — a classic drift risk (task item 6 already fixes the bug in one copy without touching the other) | Medium | **MERGE** into `_detect_temperature_drops()` parametrized by accessor + thresholds | ~90 LOC | Medium (stateful loop; mitigated by running existing correlation tests before/after — all pass unchanged) | ✅ Done |
| `config.py` + `camera/config.py` | `PrinterConfig.camera: CameraConfig` vs `TimelapseConfig.camera: TimelapseCameraConfig` | Two schemas describing the same real-world thing (an RTSP camera attached to a printer), bridged by fallback code in `get_timelapse_config()` | Medium | **DEFER**: unifying requires a `config.yaml` schema change (breaking for existing installs) | Would remove one config model + the bridging logic | High (public config surface) | ⏸ Documented, not implemented |
| `api/routes.py` | `/printers` and `/api/v1/printers` both mounted; bare `/health` | Two API surfaces to keep in sync | Low | **DEFER**: pick `/api/v1/*` as canonical, deprecate the other in a future major version | One less route family | High (breaks any external consumer polling the old path) | ⏸ Documented, not implemented |
| `timelapse/manager.py` | `on_print_started` vs. `reconcile_on_startup`'s "no session" branch | ~25 lines of session-creation logic duplicated across two entry points, with subtle differences (timestamp source, idempotency check order) | Medium | **MERGE** into a shared `_create_and_start_session()` helper | ~25 LOC | Medium (restart-recovery path; needs dedicated before/after testing beyond this session's scope) | ⏸ Documented, not implemented |
| `timelapse/manager.py` | One `asyncio.Lock` for all printers | Session-lifecycle handling for printer A blocks printer B's, unlike `StateManager`'s per-printer lock (added specifically to fix this class of issue) | Low (correctness/scalability, not simplification) | **OPTIMIZE**: mirror `StateManager`'s per-printer lock | N/A (concurrency, not LOC) | Medium (needs concurrency-focused tests) | ⏸ Documented, not implemented |
| `storage/repositories.py` | `EventRepository.save()` / `OutboxRepository.enqueue()` alongside `EventRepository.save_and_enqueue()` | *Investigated as a suspected duplicate.* | — | **KEEP** — each has its own dedicated test asserting a distinct guarantee (`enqueue`'s outbox-uniqueness constraint vs. `save_and_enqueue`'s cross-table atomicity). They are composable primitives, not copy-pasted business logic. | — | — | Reviewed, no change needed |
| `cli/commands.py` | `cmd_doctor`'s inline TLS probe vs. `test_printer_connection()` | *Investigated as a suspected duplicate.* | — | **KEEP** — doctor's version surfaces the actual exception text for diagnostics; `test_printer_connection` deliberately swallows it to return a plain bool. Textually similar, behaviorally different. | — | — | Reviewed, no change needed |
| `storage/repositories.py` | 6 repository classes, one per table | *Investigated as possible over-abstraction.* | — | **KEEP** — each has genuinely distinct queries (FIFO peek, active-alert lookup, compare-and-set delivery claim); not a generic CRUD class repeated 6×. | — | — | Reviewed, no change needed |
| `state/manager.py` | `add_event_listener` / `add_reconcile_listener` callback lists | *Investigated as possible premature event-bus abstraction.* | — | **KEEP** — camera/timelapse is a real, optional subsystem boundary; a callback interface here avoids `StateManager` importing an optional feature directly. Exactly the "build interfaces only where there is a real system boundary" principle `DESIGN.md` already states. | — | — | Reviewed, no change needed |

---

## Code That Was Deleted

- `bambu_monitor.camera.client.BaseRTSPCamera` (class)
- `bambu_monitor.camera.client.GenericRTSPCamera` (class)
- `bambu_monitor.camera.client.CameraClient.snapshot()` (method)
- `bambu_monitor.timelapse.storage.TimelapseStorage.append_telemetry_point()` (method, dead)
- `bambu_monitor.timelapse.storage.TimelapseStorage.read_telemetry_points()` (method, dead)
- 2 unused parameters (`serial`, `access_code`) from `cli.commands.test_printer_connection`
- 1 redundant config-fallback expression in `cmd_timelapse_camera_test`
- 1 redundant `load_config()` call in the `run` daemon startup path

## Code That Was Merged

- `api/routes.py`: 4 MP4-serving handlers + 8 "fetch-or-404" preambles → `_get_timelapse_session()` + `_timelapse_video_response()`
- `cli/commands.py`: `cmd_camera_snap` + `cmd_camera_test` validation → `_resolve_printer_camera()`
- `timelapse/manager.py`: 15 call sites of the save+manifest pattern → `self._persist(session)`
- `timelapse/renderer.py`: 4 manual `asyncio.to_thread(storage.save_manifest, ...)` → `storage.save_manifest_async(...)`
- `timelapse/correlation.py`: hotend-drop + bed-drop detection loops → `_detect_temperature_drops()`

## Code That Should Stay As-Is

- **Domain layer** (`domain/*.py`) — pydantic models double as both in-memory state and wire format; no speculative DTO/ORM split.
- **Repository layer** (`storage/repositories.py`) — one class per table, each with genuinely distinct query shapes (FIFO ordering, compare-and-set claims, active-alert lookups). Not a generic-CRUD-times-six pattern.
- **Outbox reliability primitives** (`save`, `enqueue`, `save_and_enqueue`, `begin_delivery`, `reclaim_stale_delivering`) — each backs a distinct, independently-tested guarantee (idempotent insert, atomic cross-table write, crash-safe claim). Collapsing them would remove test coverage of real failure modes, not complexity.
- **`StateManager`'s per-printer locking** — already the *simpler* correct design (added specifically to stop one slow printer/listener from stalling every other printer's telemetry).
- **Event-listener callbacks between `StateManager` and `TimelapseManager`** — the one legitimate subsystem boundary in the codebase; camera/timelapse is optional and must not be a hard dependency of core state management.

## Recommended Target Architecture

Unchanged from the current shape — the layering was already close to ideal. Shown here to confirm no new components are proposed:

```
Bambu Printer (MQTT/TLS)
        │
        ▼
 bambu.client (protocol + connection lifecycle)
        │  TelemetryPatch
        ▼
 state.manager (in-memory current state, job/alert lifecycle, stall detection)
        │                              │
        ▼                              ▼
 storage.repositories            DomainEvent → event listeners
        │  (SQLite WAL)                │
        ▼                              ▼
 storage.database              storage.repositories.EventRepository
                                       │ (save_and_enqueue: events + outbox, one transaction)
                                       ▼
                                delivery.worker (per-printer FIFO, backoff, DLQ)
                                       │
                                       ▼
                                delivery.webhook → Hermes


API / CLI
   │
   ▼
state.manager / storage.repositories   (no separate "application service" layer —
   │                                    both call the same domain operations directly)
   ▼
domain models


Camera / Timelapse (optional subsystem, wired via StateManager event listeners)
   │
   ▼
camera.client (RTSP capture)
   │
   ▼
timelapse.capture (FrameCaptureWorker) → timelapse.storage (frames + manifest)
   │
   ▼
timelapse.renderer (FFmpeg encode) → timelapse.correlation (telemetry/vision report)
```

## Refactoring Plan

- **Phase 1 — Safe deletions**: dead `TimelapseStorage` JSONL methods, unused `test_printer_connection` params, redundant config fallback, redundant `load_config()` call. *(Done.)*
- **Phase 2 — Consolidation**: camera class hierarchy, CLI camera validation, API session-lookup/video-response helpers, `TimelapseManager._persist`, renderer's manifest-save wrapping, correlator's temperature-drop detection. *(Done.)*
- **Phase 3 — Architectural simplification**: unified `CameraConfig`/`TimelapseCameraConfig` (backward-compatible alias); documented the dual API route mount as an explicit legacy alias. *(Done — see Phase 3 addendum below.)*
- **Phase 4 — Concurrency simplification**: moved `TimelapseManager` from one global lock to per-printer locks, mirroring `StateManager`, plus a shutdown-safety guard. *(Done — see Phase 3 addendum below.)*
- **Phase 5 — Verification**: full test suite + `ruff check` after every phase. *(Done for all phases: 181/181 tests pass, lint clean.)*

## Quantify the Result (Phases 1–2, implemented this session)

| Metric | Count |
|---|---|
| Files touched | 9 source + 2 test |
| Net LOC removed | 160 (232 inserted, 392 deleted) |
| Classes removed | 2 (`BaseRTSPCamera`, `GenericRTSPCamera`) |
| Methods/functions removed | 4 (`snapshot()`, `append_telemetry_point()`, `read_telemetry_points()`, one redundant `load_config()` call site) |
| Duplicate implementations merged | 5 (camera validation, API session/video handling, timelapse persist-triple, renderer manifest wrapping, correlator temp-drop detection) |
| Unused parameters removed | 2 |
| Config double-lookups removed | 1 |
| Tests updated (mechanical, no coverage lost) | 2 files, ~10 assertions retargeted from `.snapshot()`/`GenericRTSPCamera` to `.capture()`/`CameraClient` |
| Test suite result | 168/168 passing (unchanged pass count — no coverage lost or behavior changed) |
| Lint result | `ruff check` clean |

Complexity sources identified but deliberately **not** touched in Phases 1–2 (documented above, with reasons): the dual camera-config schemas, the dual API route mounts, `TimelapseManager`'s session-creation duplication between `on_print_started` and startup reconciliation, and its single global lock. Each of these four is addressed below in Phase 3.

---

## Phase 3 — Deferred-item follow-up

**Date:** 2026-09-09 (same day, follow-up session). **Scope:** the four items Phases 1–2 explicitly deferred, plus a second-order duplication hunt over the code those phases touched. Method: same as before — full read of the affected modules, cross-checked against this repo's actual `config.yaml`/`.env.example` (not just the code) before touching the config schema, since that item carried real backward-compatibility risk.

### What was found beyond the original four

Investigating the four deferred items surfaced two more genuine duplicate-implementation cases (not new complexity *introduced* by Phases 1–2, just found while looking closely at neighboring code):

- **`TimelapseManager.generate_video()` had zero callers anywhere.** The CLI's `cmd_timelapse_generate` independently reimplemented the same operation — and diverged from it in two real ways: it never passed `burn_overlay` (so `bambu-monitor timelapse generate` silently ignored `timelapse.overlay.enabled`), and it persisted only the SQLite row, never the on-disk `manifest.json` (so the `/timelapses/{id}` API detail endpoint would show stale manifest data after a manual CLI regenerate).
- **Identical status-color badge logic** duplicated between the timelapse gallery and player HTML views in `api/routes.py`.

Both were the same kind of "yes, this is really the same business operation" case Phase 3's own decision rule calls for consolidating, so both were fixed alongside the four assigned items.

### The four deferred items — what was done

1. **Timelapse session creation duplication** — **MERGE (narrow)**. Extracted only the "build a new `TimelapseSession`, resolve its directory, and persist it" step into `TimelapseManager._create_session()`. The surrounding orchestration in `on_print_started` and `reconcile_on_startup` stayed separate, because their semantics genuinely differ: different idempotency checks (in-memory fast path vs. DB-only), different timestamp source (`utc_now()` vs. the print's actual `active_job.started_at`), and — deliberately preserved — only `on_print_started` emits `timelapse.started` (reattaching to an already-running print on restart should not re-announce "started"). A pre-existing pointless computation (a throwaway placeholder `storage_dir` immediately overwritten two lines later) was also dropped as a natural byproduct.
2. **`TimelapseManager` global locking** — **SIMPLIFY**. Replaced the single `asyncio.Lock` with a per-printer `Dict[str, asyncio.Lock]` (`_printer_lock()`), mirroring `StateManager`'s existing, already-proven pattern exactly — no new shared abstraction, just the same small idiom applied a second place. Every mutation the lock guarded was already keyed by `printer_id`; `_render_tasks` (keyed by `session_id`) and the render semaphore (an intentional global resource bound) didn't need it. Also closed a real, pre-existing shutdown race (independent of lock granularity: `api/app.py` calls `timelapse_manager.shutdown()` *before* stopping the MQTT clients, so a lingering event could start a brand-new session after "shutdown" returned) with a `_shutting_down` guard flag checked by the "start new work" handlers. `on_print_completed`/`on_print_failed` were deliberately left unguarded — they wind existing work down rather than starting new work, and rejecting them would strand a session as `CAPTURING` in the DB to be misclassified as `FAILED` by orphan-recovery on the next restart instead of correctly recorded as `COMPLETED` now.
3. **Dual camera configuration schemas** — **MERGE**. Confirmed via `.env.example` (which sets `A1_MINI_CAMERA_RTSP` and `TIMELAPSE_CAMERA_RTSP` to the *identical* example URL) that these were always meant to describe one physical camera, not two concepts. Deleted `TimelapseCameraConfig`; `TimelapseConfig.camera` is now a `CameraConfig`. Backward compatibility for existing `config.yaml` files (this repo's own included) was the hard requirement: `CameraConfig.rtsp_url` now accepts the legacy `url` key via `validation_alias=AliasChoices("rtsp_url", "url")` with `populate_by_name=True`, so nothing changes for an existing deployment. The printer-camera fallback in `get_timelapse_config()` now copies the printer's *entire* camera config (ffmpeg/probe/timeout included) instead of just 3 fields — verified against this repo's live `config.yaml` to produce byte-identical resolved output before and after.
4. **Dual API route surfaces** — **KEEP, documented as legacy**. Turned out not to be implementation duplication at all: `list_printers()` was already one function decorated with both `@router.get("/printers")` and `@router.get("/api/v1/printers")`. README documents only the `/api/v1` form and nothing else in the repo references the bare path besides one test, so it was marked `include_in_schema=False` (hidden from the OpenAPI docs, unchanged at runtime) and called out explicitly as a legacy alias in the README, rather than restructured or removed.

### Boundary check

No structural boundary violations found. Two minor, low-severity observations recorded for awareness rather than acted on: `StateManager._emit_event` builds two API route URLs as inline f-strings for event-payload enrichment (config-driven enrichment, not a functional dependency on camera/timelapse code — not worth a shared module for 2 call sites); `api/app.py` imports camera/timelapse unconditionally, which is correct since they're core product features per `DESIGN.md`, not an optional plugin system.

### Not done (documented, not implemented)

**Duplicate subprocess timeout/kill/reap handling** across `camera/client.py` (capture, ffprobe health-probe) and `timelapse/renderer.py` (ffprobe validate, ffmpeg render) — four independent copies of "spawn → `wait_for(timeout)` → kill+wait on timeout/cancel → check returncode." A real, correctness-sensitive duplication (a cleanup fix to one copy wouldn't propagate to the other three), but it wasn't one of the four assigned items, and consolidating it well would need its own dedicated tests. Recommended as the next candidate if this pass continues.

### Tests added

- `tests/integration/test_timelapse_restart_recovery.py`: new-session-created-on-restart (with the original print's timestamp, no spurious `timelapse.started`), stale-session-from-previous-job finalization, reconciliation idempotency.
- `tests/unit/test_timelapse_manager.py`: persistence-failure-during-creation leaves no active session (verifies the "persist before activate" ordering the consolidation had to preserve).
- `tests/unit/test_timelapse_concurrency.py` (new file): printer A never blocks on printer B; same-printer operations stay serialized; shutdown rejects new lifecycle events; shutdown waits for an in-flight handler before stopping that printer's worker; no deadlock when racing multi-printer events against shutdown itself.
- `tests/unit/test_config.py`: legacy `timelapse.camera.url:` YAML key still resolves; printer-camera fallback carries the full config (not just 3 fields); a dedicated timelapse camera URL still wins over the fallback; no-camera-configured leaves `rtsp_url` empty.
- `tests/integration/test_timelapse_cli.py`: updated for the consolidated error messages; added an explicit regression check that `burn_overlay` is now passed through (the exact bug the old duplicate implementation had).

### Quantify the result (Phase 3 only)

| Metric | Count |
|---|---|
| Config model classes removed | 1 (`TimelapseCameraConfig`) |
| Duplicate implementations merged | 4 (session creation, video generation, status-color, camera-client construction in `_resolve_camera_client`) |
| Real bugs fixed as a byproduct of consolidation (not a separate correctness pass — these were direct consequences of unifying duplicate implementations) | 2 (CLI `timelapse generate` ignoring `overlay.enabled`; CLI regenerate never updating `manifest.json`) |
| Pre-existing race condition closed | 1 (post-shutdown session creation in `TimelapseManager`, latent regardless of lock granularity) |
| Lock model change | 1 global `asyncio.Lock` → per-printer `Dict[str, asyncio.Lock]` (mirrors `StateManager`'s existing pattern; no new lock *types* introduced) |
| New dedicated tests | 13 (3 restart-recovery, 1 persistence-ordering, 5 concurrency, 4 config) |
| Production code (`src/`), cumulative Phases 1–3 vs. original `HEAD` | net −129 lines (365 inserted, 494 deleted) |
| Test suite result | 181/181 passing |
| Lint result | `ruff check` clean |

Consistent with Phase 1–2's framing: this pass changed internals, not the component graph. `TimelapseManager` keeps its exact public methods and event-handling shape; `CameraConfig` becomes the sole camera schema; the API surface is unchanged (one alias now explicitly marked legacy). The locking and shutdown-guard changes are a correctness/scalability improvement more than an LOC reduction, and are reported as such rather than folded into a "savings" number.

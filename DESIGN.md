# Bambu Monitor

## 1. Purpose

Build a standalone local service that turns a Bambu Lab A1 printer (and future Bambu printers) into a **reliable, queryable, event-driven device service**.

Bambu Monitor is **independent of Hermes**.

Hermes is an intelligent consumer of Bambu Monitor, not a dependency.

The service's primary mission is to **convert unreliable, delta-based, low-level Bambu telemetry into durable canonical state and semantic events**.

Specifically, the service must:

* Connect to the Bambu A1 over local LAN via MQTT with TLS.
* Ingest raw printer telemetry (handling connection state, reconnects, and initial `pushall` dumps).
* Parse Bambu-specific delta/patch payloads into a canonical normalized model.
* Maintain an in-memory current state representation for fast, zero-cost queries.
* Persist durable printer state, print job history, alerts, and events locally in SQLite (WAL mode).
* Reconcile printer state and deterministically re-attach to active print jobs across service restarts.
* Detect meaningful printer/print events using multi-factor rules (avoiding false positives on long prints).
* Manage alert lifecycles (`ACTIVE` → `ACKNOWLEDGED` → `RESOLVED`) to prevent alert event spam.
* Expose a local REST API for querying state and history.
* Expose a Server-Sent Events (SSE) stream for lightweight real-time dashboards and CLI tools.
* Provide an outbound event outbox mechanism with per-printer FIFO delivery, retries, and dead-letter queue (DLQ) support.
* Eventually expose printer control operations and camera snapshots.
* Remain fully functional and valuable as a standalone local utility without Hermes.

---

# 2. Architecture

```text
                         Bambu Lab A1
                              │
                         MQTT / TLS
                              │
                              ▼
                 ┌─────────────────────────┐
                 │     MQTT Ingestion      │
                 │                         │
                 │ connect / reconnect     │
                 │ subscribe               │
                 │ pushall                 │
                 └────────────┬────────────┘
                              │
                          raw delta
                              │
                              ▼
                 ┌─────────────────────────┐
                 │    Protocol Parser      │
                 └────────────┬────────────┘
                              │
                       normalized patch
                              │
                              ▼
                 ┌─────────────────────────┐
                 │      State Engine       │
                 │                         │
                 │ merge patch → state     │
                 │ job lifecycle           │
                 │ alert lifecycle         │
                 └───────┬─────────┬───────┘
                         │         │
                ┌────────┘         └────────┐
                ▼                           ▼
        In-memory state                Event Engine
                │                           │
                │                           ▼
                │                    Domain Events
                │                           │
                ▼                           ▼
             SQLite (WAL)                 Outbox
                │                           │
                │                           ▼
                │                        Hermes
                │
                └──────► REST / SSE ◄───────┘
                               │
                      ┌────────┼────────┐
                      ▼        ▼        ▼
                   Hermes     CLI    Dashboard
```

The architecture does not require Hermes to function. Hermes queries Bambu Monitor via REST and receives critical domain events via the outbox webhook.

---

# 3. Design principles

## 3.1 Standalone
Bambu Monitor must run independently. It must not import Hermes code or depend on Hermes databases, runtime, skills, agents, or configuration.

## 3.2 Device protocol isolation
Bambu-specific MQTT implementation and payload structures remain strictly inside Bambu Monitor. Consumers should never need to parse or understand Bambu MQTT payloads.

## 3.3 Canonical domain model
Expose normalized printer concepts:
* printer
* print job (with deterministic IDs and lifecycle states)
* print state
* progress and layer metrics
* temperatures
* filament
* alerts (with explicit lifecycles)
* domain events

Do not expose raw Bambu protocol fields (e.g. `gcode_state`, `mc_percent`, `subtask_name`) as the primary external API contract.

## 3.4 Delta/patch-first ingestion
Bambu MQTT does not publish full state on every update. It emits partial delta messages. The system must treat every incoming message as a state patch that merges into the current state rather than a full state replacement.

## 3.5 Dual-layer state
Maintain state in two tiers:
1. **In-Memory State**: Fast, thread-safe, in-memory representation providing $O(1)$ reads for REST and SSE consumers.
2. **SQLite (WAL Mode)**: Durable event and state store with JSON columns for raw and extensible payloads, updated on significant transitions and throttled periodic snapshots.

## 3.6 Event-driven with alert lifecycles
Telemetry is continuously ingested (~1 Hz), but only meaningful state changes trigger external events. Alerts follow an explicit lifecycle (`ACTIVE` → `ACKNOWLEDGED` → `RESOLVED`) so that events fire once on trigger and once on clearing, completely suppressing repeated telemetry noise.

## 3.7 Reliable Outbox delivery
Important events are persisted to an outbox before delivery. Delivery guarantees:
* At-least-once delivery with exponential backoff.
* Per-printer FIFO ordering (events for printer A stay in sequence, while printer B proceeds independently).
* Dead-letter queue (`failed` status) so a poison payload never blocks the queue indefinitely.

## 3.8 Restart recovery & job re-attachment
Service restarts must be seamless. On startup, Bambu Monitor reconciles the initial `pushall` telemetry against SQLite to re-attach to any active print job without minting duplicate jobs or firing spurious `print.started` notifications.

## 3.9 Local-first & minimal dependencies
The service runs locally on LAN. No Bambu Cloud dependency, no external message brokers (Kafka/RabbitMQ), and no external database servers (Postgres/Mongo).

## 3.10 API-first (REST + SSE)
Consumers interact through stable HTTP APIs: REST for queries, Outbox Webhooks for reliable push delivery to agents (Hermes), and Server-Sent Events (SSE) for real-time live monitoring interfaces.

## 3.11 Lean abstraction (No premature generality)
Do not prematurely implement abstractions for capabilities that aren't required by the current phase. Prefer simple, direct interfaces that can evolve, but do not build speculative plugin frameworks, event-bus abstractions, multi-protocol wrappers, or distributed-service infrastructure. Build interfaces only where there is a real system boundary.


---

# 4. Technology

* **Python 3.12+**
* **FastAPI**: REST endpoints and SSE streaming.
* **Pydantic v2**: Strict domain schemas, patches, and validation.
* **SQLite with WAL mode** (`PRAGMA journal_mode=WAL;`): Durable state and event storage.
* **SQLAlchemy 2.0 (asyncio or scoped sessions)** or lightweight SQLite abstraction with native JSON support.
* **MQTT Client** supporting TLS, configurable certificate validation (`tls_verify: false`), and async I/O.
* **pytest**: Comprehensive unit and integration test suite.
* **Structured logging**: Contextual logs with printer ID and job ID.

### Explicit Non-Dependencies
Do not introduce:
* MongoDB / PostgreSQL / MySQL
* Redis / Memcached
* Kafka / RabbitMQ / Celery
* Docker requirement

---

# 5. Proposed repository structure

```text
bambu-monitor/
│
├── README.md
├── AGENTS.md
├── DESIGN.md
├── pyproject.toml
├── .env.example
├── .gitignore
│
├── src/
│   └── bambu_monitor/
│       │
│       ├── __init__.py
│       ├── main.py
│       ├── config.py
│       │
│       ├── domain/
│       │   ├── __init__.py
│       │   ├── printer.py            # Printer entity & connection status
│       │   ├── print_job.py          # PrintJob entity & lifecycle states
│       │   ├── telemetry.py          # TelemetryPatch & normalized models
│       │   ├── alerts.py             # Alert entity & AlertLifecycle state machine
│       │   └── events.py             # DomainEvent & OutboxMessage schemas
│       │
│       ├── state/
│       │   ├── __init__.py
│       │   └── manager.py            # In-memory state, patch merge, job/alert tracking
│       │
│       ├── storage/
│       │   ├── __init__.py
│       │   ├── database.py           # SQLite connection & PRAGMA setup (WAL)
│       │   ├── models.py             # SQLite tables (Printers, Jobs, Alerts, Events, Outbox)
│       │   └── repositories.py       # Data access for domain entities
│       │
│       └── api/
│           ├── __init__.py
│           ├── app.py                # FastAPI factory & lifespan
│           └── routes.py             # REST (/health, /printers, /prints/active) & SSE (/events/stream)
│
├── tests/
│   ├── conftest.py
│   ├── unit/
│   │   ├── test_config.py
│   │   ├── test_patch_merge.py
│   │   ├── test_job_tracker.py
│   │   ├── test_alert_lifecycle.py
│   │   ├── test_stall_detector.py
│   │   └── test_outbox_queue.py
│   ├── integration/
│   │   ├── test_sqlite_wal.py
│   │   ├── test_restart_recovery.py
│   │   ├── test_api_endpoints.py
│   │   └── test_sse_stream.py
│   └── fixtures/
│       ├── bambu_pushall_full.json
│       ├── bambu_delta_temperatures.json
│       ├── bambu_delta_progress.json
│       ├── bambu_delta_layer.json
│       └── bambu_delta_error.json
│
├── data/
│   └── .gitkeep
│
└── docs/
    ├── architecture.md
    ├── api.md
    └── events.md
```

Future phases plug cleanly into this structure without modifying the core domain:
* **Phase 2 (MQTT)** adds `src/bambu_monitor/bambu/` (`client.py`, `protocol.py`, `parser.py`).
* **Phase 4 (Hermes Delivery)** adds `src/bambu_monitor/delivery/` (`worker.py`, `webhook.py`).
Do not create premature sub-packages or single-line files before those phases are reached.


---

# 6. Configuration

Configuration supports YAML with environment variable interpolation and `.env` support.

```yaml
application:
  name: bambu-monitor
  environment: development
  log_level: INFO

database:
  path: ./data/bambu.db
  journal_mode: WAL
  synchronous: NORMAL
  flush_interval_seconds: 5    # Periodic flush of in-memory state snapshots

printers:
  - id: bambu-a1
    model: A1
    host: ${BAMBU_HOST}
    serial_number: ${BAMBU_SERIAL}
    access_code: ${BAMBU_ACCESS_CODE}
    username: bblp             # Bambu LAN default username
    port: 8883
    tls: true
    tls_verify: false          # Allow self-signed Bambu certificates

events:
  delivery:
    enabled: true
    endpoint: ${EVENT_ENDPOINT}
    timeout_seconds: 10
    retry_attempts: 5
    initial_backoff_seconds: 2
    backoff_multiplier: 2.0
    max_backoff_seconds: 300

detection:
  stall:
    enabled: true
    min_check_seconds: 300     # 5 minutes minimum check window
    adaptive_factor: 1.5       # Multiplier on estimated layer/percent duration
```

Secrets (IP, serial, access code, webhook URLs) must remain in `.env` and never be committed.

---

# 7. MQTT ingestion & Delta handling

Bambu printers communicate via MQTT over TLS. Crucially, the protocol is **delta-based**:

```text
Connect to Bambu MQTT
         ↓
Authenticate (`bblp` + access_code)
         ↓
Subscribe to `device/<serial>/report`
         ↓
Publish `pushall` request to `device/<serial>/request`
         ↓
Receive initial full state dump
         ↓
Subsequent messages = partial delta patches
```

### Ingestion Contract
1. **Connection**: Connects to the printer using TLS with configurable certificate verification (`tls_verify: false` for Bambu self-signed certs).
2. **Subscription**: Subscribes to `device/{serial_number}/report`.
3. **Full State Request (`pushall`)**: Immediately after establishing a connection or reconnecting, publish:
   ```json
   {
     "pushing": {
       "sequence_id": "0",
       "command": "pushall"
     }
   }
   ```
4. **Reconnect Handling**: Automatically reconnect with exponential backoff on network loss, re-subscribe, and re-issue `pushall`.
5. **Separation of Concerns**: The MQTT layer performs no business logic; it dispatches raw payloads to the Protocol Parser.

---

# 8. Protocol Parser & Normalization

The Protocol Parser converts raw Bambu MQTT JSON payloads into strongly typed **normalized telemetry patches**.

### Raw vs Normalized Patch
A raw payload might contain only:
```json
{
  "print": {
    "nozzle_temper": 220.5,
    "bed_temper": 60.0
  }
}
```

The parser maps this to a `TelemetryPatch`:
```python
class TelemetryPatch(BaseModel):
    printer_id: str
    timestamp: datetime
    online: bool | None = None
    state: PrinterState | None = None
    progress: int | None = None
    layer: int | None = None
    total_layers: int | None = None
    remaining_seconds: int | None = None
    nozzle_temperature: float | None = None
    bed_temperature: float | None = None
    subtask_name: str | None = None
    gcode_state: str | None = None
    error_code: int | None = None
```

* Only fields present in the raw delta are populated.
* The rest of the application never sees raw Bambu field names (`mc_percent`, `nozzle_temper`, `mc_remaining_time`).

---

# 9. State Engine & In-Memory Representation

The State Engine manages live printer truth using an **in-memory model backed by throttled SQLite persistence**.

### 9.1 Patch-Merge Semantics
The state engine merges incoming patches into the current state:
```python
current_state = merge(current_state, incoming_patch)
```
**Mandatory rule**: The state engine must **never** replace the complete state with an individual partial MQTT message. Missing fields in a patch preserve their previous values.

### 9.2 In-Memory Current State
Provides $O(1)$ reads for REST queries and SSE streaming:
```json
{
  "printer_id": "bambu-a1",
  "model": "A1",
  "online": true,
  "state": "printing",
  "print": {
    "job_id": "job_bambu-a1_phone_stand_1725800000",
    "filename": "phone_stand.3mf",
    "status": "running",
    "progress": 67,
    "layer": 134,
    "total_layers": 201,
    "remaining_seconds": 2520,
    "started_at": "2026-09-08T16:00:00+05:30"
  },
  "temperatures": {
    "nozzle": 220.5,
    "nozzle_target": 220.0,
    "bed": 60.0,
    "bed_target": 60.0
  },
  "last_seen": "2026-09-08T17:00:00+05:30"
}
```

### 9.3 Throttled Durable Persistence
Bambu telemetry arrives at ~1 Hz. Persisting every message directly to SQLite causes unnecessary disk write thrashing and lock contention.
* **Immediate Write**: State transitions (`IDLE` → `RUNNING`, `RUNNING` → `FINISH`), new print jobs, alerts, and domain events are written immediately to SQLite in a single transaction.
* **Throttled Write**: Routine telemetry updates (temperatures, minor progress ticks) update in-memory state immediately, but are flushed to SQLite on a configurable interval (e.g. every 5 seconds) or upon clean service shutdown.

---

# 10. Print Job Lifecycle & Restart Recovery

### 10.1 Deterministic Job ID
Bambu printers lack a universal persistent UUID across restarts. Bambu Monitor synthesizes a deterministic internal `job_id`:
```text
job_{printer_id}_{sanitized_filename}_{started_timestamp}
```

### 10.2 Print Job Lifecycle
```text
┌─────────────┐
│   PREPARE   │ (Bed leveling, nozzle heating)
└──────┬──────┘
       │
       ▼
┌─────────────┐       pause        ┌─────────────┐
│   RUNNING   │ ─────────────────► │   PAUSED    │
└──────┬──────┘ ◄───────────────── └─────────────┘
       │              resume
       ├─────────────────────────┐
       ▼                         ▼
┌─────────────┐           ┌─────────────┐
│  COMPLETED  │           │   FAILED    │
└─────────────┘           └─────────────┘
```

### 10.3 Restart Recovery & Job Re-attachment
When Bambu Monitor starts up or restarts mid-print:
```text
Bambu Monitor Starts
         ↓
Query SQLite for active print job for each printer (status IN ('prepare', 'running', 'paused'))
         ↓
Connect MQTT & Issue pushall
         ↓
Receive initial full telemetry dump
         ↓
Reconcile actual printer state:
  ├─ If printer is printing and active job matches (filename/subtask):
  │    Re-attach to existing print job in memory
  │    Update telemetry progress
  │    DO NOT fire duplicate print.started event
  │
  ├─ If printer is printing but no active job exists in SQLite:
  │    Synthesize new print job
  │    Persist to SQLite
  │    Emit print.started event
  │
  └─ If printer is idle but active job exists in SQLite:
       Mark active job as completed or failed based on printer error/finish state
       Persist completion to SQLite
```

This guarantees restarts do not corrupt job history or trigger duplicate notifications to Hermes.

---

# 11. Alert Lifecycle & Deduplication

Alerts (e.g., blockage warnings, filament runout, temperature anomalies) are first-class domain models with a strict lifecycle:

```text
                 ┌─────────────┐
                 │   ACTIVE    │
                 └──────┬──────┘
                        │
                  acknowledgement (API or user)
                        │
                        ▼
                 ┌─────────────┐
                 │ACKNOWLEDGED │
                 └──────┬──────┘
                        │
                   condition clears
                        │
                        ▼
                 ┌─────────────┐
                 │  RESOLVED   │
                 └─────────────┘
```

### Alert Behavior Contract
1. **Trigger Condition Met**:
   * Emit domain event **ONCE** (e.g. `print.possible_blockage`).
   * Alert state becomes `ACTIVE`.
2. **Telemetry Continues**:
   * While alert is `ACTIVE` or `ACKNOWLEDGED`, subsequent telemetry ticks **MUST NOT** fire duplicate events.
3. **Condition Clears**:
   * Alert transitions to `RESOLVED`.
   * Emit corresponding cleared domain event **ONCE** (e.g. `print.blockage_cleared`).

---

# 12. Stall & Blockage Detection

### 12.1 Multi-Factor Detection (Preventing False Positives)
Bambu's `mc_percent` is an integer (`0`–`100`). On a 20-hour print, 1% progress takes **12 minutes**. A simplistic rule checking `percent unchanged > 10 min` will cause false-positive alerts on long prints.

The stall detector evaluates multiple factors:
1. `printer_state == "printing"`
2. Time elapsed since last change in:
   * `progress` (percentage)
   * `layer` (current layer number)
   * `remaining_seconds` (countdown progress)
3. **Adaptive Threshold**:
   ```python
   # Base timeout adjusted for long prints
   estimated_percent_duration = total_estimated_seconds / 100
   timeout = max(min_check_seconds, estimated_percent_duration * adaptive_factor)
   ```
4. No active user pause or heating/calibration state.

### 12.2 Stall Event Output
If a stall is detected:
* Trigger alert `print.possible_blockage` with evidence:
  ```json
  {
    "progress": 43,
    "current_layer": 85,
    "unchanged_seconds": 900,
    "expected_percent_duration": 480,
    "printer_state": "printing"
  }
  ```
* When progress or layer changes advance, resolve the alert and emit `print.blockage_cleared`.

---

# 13. Persistence & SQLite WAL

SQLite serves as the durable event and state store.
* **Mandatory PRAGMA setup**:
  ```sql
  PRAGMA journal_mode = WAL;
  PRAGMA synchronous = NORMAL;
  PRAGMA foreign_keys = ON;
  PRAGMA busy_timeout = 5000;
  ```

### Database Tables

#### `printers`
| Column | Type | Description |
| :--- | :--- | :--- |
| `id` | TEXT PRIMARY KEY | Canonical printer ID (e.g. `bambu-a1`) |
| `model` | TEXT | Model name (e.g. `A1`) |
| `serial_number` | TEXT UNIQUE | Hardware serial number |
| `host` | TEXT | Local IP address or hostname |
| `online` | BOOLEAN | Current connection status |
| `last_seen` | TIMESTAMP | Last telemetry received timestamp |
| `current_state_json` | TEXT | Serialized current state snapshot |
| `updated_at` | TIMESTAMP | Last record update timestamp |

#### `print_jobs`
| Column | Type | Description |
| :--- | :--- | :--- |
| `id` | TEXT PRIMARY KEY | Deterministic job ID |
| `printer_id` | TEXT REFERENCES printers(id) | Target printer ID |
| `filename` | TEXT | 3MF / G-code filename |
| `status` | TEXT | `prepare`, `running`, `paused`, `completed`, `failed` |
| `started_at` | TIMESTAMP | Print start timestamp |
| `completed_at` | TIMESTAMP | Print completion/failure timestamp |
| `duration_seconds`| INTEGER | Total print duration |
| `progress` | INTEGER | Final progress percentage |
| `total_layers` | INTEGER | Total layer count |
| `metadata_json` | TEXT | Extensible job metadata |

#### `alerts`
| Column | Type | Description |
| :--- | :--- | :--- |
| `id` | TEXT PRIMARY KEY | Alert ID (e.g. `alt_019283`) |
| `printer_id` | TEXT REFERENCES printers(id) | Target printer ID |
| `alert_type` | TEXT | e.g. `print.possible_blockage`, `filament.runout` |
| `severity` | TEXT | `warning`, `critical` |
| `status` | TEXT | `active`, `acknowledged`, `resolved` |
| `created_at` | TIMESTAMP | Trigger timestamp |
| `resolved_at` | TIMESTAMP | Resolution timestamp |
| `details_json` | TEXT | Evidence and contextual payload |

#### `events`
| Column | Type | Description |
| :--- | :--- | :--- |
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | Monotonic primary key |
| `event_id` | TEXT UNIQUE | Canonical event ID (e.g. `evt_019283`) |
| `printer_id` | TEXT REFERENCES printers(id) | Target printer ID |
| `event_type` | TEXT | e.g. `print.completed`, `print.started` |
| `severity` | TEXT | `info`, `warning`, `critical` |
| `timestamp` | TIMESTAMP | Event creation timestamp |
| `payload_json` | TEXT | Complete domain event payload |

#### `outbox`
| Column | Type | Description |
| :--- | :--- | :--- |
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | Monotonic outbox queue ID |
| `event_id` | TEXT REFERENCES events(event_id) | Target event ID |
| `printer_id` | TEXT REFERENCES printers(id) | Partitioning key for FIFO ordering |
| `destination` | TEXT | Target webhook URL |
| `payload_json` | TEXT | Event JSON payload |
| `status` | TEXT | `pending`, `delivering`, `delivered`, `failed` |
| `attempts` | INTEGER DEFAULT 0 | Delivery attempt count |
| `created_at` | TIMESTAMP | Enqueue timestamp |
| `last_attempt_at` | TIMESTAMP | Timestamp of last attempt |
| `delivered_at` | TIMESTAMP | Successful delivery timestamp |
| `error_message` | TEXT | Last error message if failed |

---

# 14. Event Outbox & Reliable Delivery

Events are never pushed directly across the network during telemetry ingestion. They are committed to the `outbox` table within the same transaction that records the state change or alert.

### 14.1 Delivery Guarantees
1. **At-Least-Once Delivery**: Events remain in the outbox until HTTP `2xx` acknowledgement is received from the consumer.
2. **Per-Printer FIFO Ordering**:
   * Events for `bambu-a1` are delivered strictly in order of `outbox.id`:
     `print.started` → `print.completed`
   * An in-flight or retrying event for `bambu-a1` holds back subsequent events for `bambu-a1`.
   * **Printer Independence**: A delivery failure on `bambu-a1` does not block deliveries for `bambu-a1-mini`.
3. **Dead-Letter Queue (`failed` status)**:
   * If an event fails after `retry_attempts` (default 5) or returns a permanent client error (`4xx` excluding 429), it transitions to `status = 'failed'`.
   * Marking an unrecoverable event as `failed` prevents poison pills from blocking the printer queue indefinitely while alerting operators.
4. **Consumer Idempotency**:
   * Every event payload contains a globally unique `event_id`. Consumers can safely deduplicate received events.

---

# 15. Domain Events & Severity

Events represent meaningful domain occurrences, not low-level MQTT messages.

```json
{
  "event_id": "evt_019283a",
  "source": "bambu-a1",
  "event_type": "print.completed",
  "severity": "info",
  "timestamp": "2026-09-08T17:00:00+05:30",
  "printer": {
    "id": "bambu-a1",
    "model": "A1"
  },
  "print": {
    "job_id": "job_bambu-a1_phone_stand_1725800000",
    "filename": "phone_stand.3mf",
    "duration_seconds": 8230,
    "total_layers": 201
  }
}
```

### Initial Event Catalog & Severity
* **INFO**:
  * `printer.online`
  * `print.started`
  * `print.resumed`
  * `print.completed`
  * `print.blockage_cleared`
  * `filament.loaded`
* **WARNING**:
  * `printer.offline`
  * `print.paused`
  * `print.possible_blockage`
  * `filament.runout`
* **CRITICAL**:
  * `print.failed`
  * `temperature.anomaly`
  * `printer.error`

---

# 16. API: REST & Server-Sent Events (SSE)

Bambu Monitor provides two consumer interfaces:
1. **REST API**: For point-in-time state queries, job history, and health checks.
2. **SSE API**: For live dashboards and CLI tools streaming real-time normalized state.

### 16.1 Endpoints
```http
# System & Health
GET /health

# Printers
GET /api/v1/printers
GET /api/v1/printers/{printer_id}
GET /api/v1/printers/{printer_id}/status

# Print Jobs
GET /api/v1/printers/{printer_id}/prints
GET /api/v1/printers/{printer_id}/prints/active
GET /api/v1/printers/{printer_id}/prints/{job_id}

# Alerts
GET /api/v1/printers/{printer_id}/alerts
POST /api/v1/printers/{printer_id}/alerts/{alert_id}/acknowledge

# Events & Streaming
GET /api/v1/printers/{printer_id}/events
GET /api/v1/printers/{printer_id}/events/stream    # Server-Sent Events (SSE)

# Outbox Status
GET /api/v1/outbox/status
```

### 16.2 Preferred Endpoint for Active Status
Consumers asking *"What's currently printing?"* should query:
```http
GET /api/v1/printers/{printer_id}/prints/active
```
Returns HTTP 200 with the active `PrintJob` object or HTTP 204 (No Content) if the printer is currently idle.

### 16.3 Server-Sent Events (`/events/stream`)
Allows lightweight web UIs and CLI monitors to subscribe to live updates:
```text
event: state_patch
data: {"printer_id": "bambu-a1", "progress": 68, "layer": 135, "nozzle_temperature": 220.1}

event: domain_event
data: {"event_id": "evt_019283a", "event_type": "print.completed", "severity": "info"}
```

---

# 17. Hermes Integration Contract

Hermes is an external intelligent agent that consumes Bambu Monitor:

```text
Bambu A1
  │
  │ raw MQTT deltas
  ▼
Bambu Monitor
  │
  │ canonical state + semantic events
  ├───────────────────┐
  ▼                   ▼
REST Queries      Outbox Webhook
  │                   │
  ▼                   ▼
Hermes Queries     Hermes Receives
  │                   │
  └─────────┬─────────┘
            ▼
       Hermes Reasoning
            │
            ▼
       User Notification
```

### Separation of Responsibilities
* **Bambu Monitor** answers: *"What happened?"* (facts, normalized telemetry, domain event, raw severity).
* **Hermes** answers: *"What does it mean, and what should I do about it?"* (user context, chat notifications, escalation, whether to prompt user).
* Bambu Monitor contains **zero** notification wording or user messaging templates.

---

# 18. Future Controls & Camera (Non-Goals for V1)

* **Printer Controls (Future)**:
  * Endpoints like `POST /api/v1/printers/{printer_id}/pause` and `resume`.
  * Controls require explicit confirmation and safety validation.
* **Camera Integration (Future)**:
  * Snapshot endpoint: `GET /api/v1/printers/{printer_id}/camera/snapshot`.
  * Stream frames on-demand for vision models, without routing continuous video feeds through Hermes.

---

# 19. Health Monitoring

`GET /health` returns comprehensive subsystem status:
```json
{
  "status": "ok",
  "timestamp": "2026-09-08T17:00:00+05:30",
  "database": {
    "available": true,
    "journal_mode": "wal"
  },
  "printers": {
    "configured": 1,
    "connected": 1,
    "printers": [
      {
        "id": "bambu-a1",
        "online": true,
        "mqtt_connected": true,
        "active_job": "job_bambu-a1_phone_stand_1725800000"
      }
    ]
  },
  "outbox": {
    "pending": 0,
    "delivering": 0,
    "failed_dlq": 0
  }
}
```

---

# 20. Testing Strategy

The service must be fully runnable and testable **without a physical printer**.

### Fixture-Based Testing
Representative Bambu MQTT JSON payloads are stored in `tests/fixtures/`:
* `bambu_pushall_full.json`: Complete printer state dump.
* `bambu_delta_temperatures.json`: Partial temperature patch.
* `bambu_delta_progress.json`: Partial print progress/layer patch.
* `bambu_delta_error.json`: HMS error codes / warning payloads.

### Test Matrix
1. **Parser Tests**: Convert raw Bambu JSON fixtures into `TelemetryPatch` objects.
2. **Merge Semantics**: Verify that applying a temperature patch preserves existing progress and job metadata in `current_state`.
3. **Job Recovery**: Simulate service startup with pre-existing SQLite jobs; verify re-attachment and suppression of duplicate `print.started` events.
4. **Alert Lifecycle**: Verify that an alert fires once on trigger, suppresses duplicates on successive ticks, and emits a cleared event on resolution.
5. **Stall Detector**: Test with short and long prints (ensuring a 20-hour print does not trigger false stall alerts after 10 minutes).
6. **Outbox Tests**: Verify per-printer FIFO sequencing, retry backoff, and transition to `failed` (DLQ) after maximum retries.
7. **API Tests**: Validate REST endpoints and SSE stream formatting.

---

# 21. Phased Implementation Plan

### Phase 1 — Framework Foundation (Initial Implementation Scope)
*The coding agent will implement the complete framework so that the protocol and networking can be plugged in cleanly. No direct Bambu MQTT networking is implemented in Phase 1.*

Deliverables:
1. Python project packaging (`pyproject.toml`, dependencies, structured logging).
2. Configuration loading (YAML + environment variables + `.env`).
3. Domain models (`Printer`, `PrintJob`, `TelemetryPatch`, `Alert`, `DomainEvent`, `OutboxMessage`).
4. SQLite database layer with mandatory WAL mode, connection management, and migrations.
5. Repositories (`PrinterRepository`, `JobRepository`, `AlertRepository`, `EventRepository`, `OutboxRepository`).
6. In-memory State Manager with patch-merge logic.
7. Print job lifecycle state machine and deterministic ID synthesis.
8. Alert state machine (`ACTIVE` → `ACKNOWLEDGED` → `RESOLVED`).
9. Outbox queue abstraction with per-printer FIFO logic.
10. FastAPI application with REST endpoints (`/health`, `/printers`, `/prints/active`, etc.) and SSE streaming abstraction.
11. Fixture loading utilities and comprehensive unit/integration test suite.

### Phase 2 — Real Bambu Device Integration & Administration CLI
1. Automatic LAN Discovery (UDP port 2021 broadcast / SSDP / mDNS).
2. Dual Operating Modes: Long-running service (`bambu-monitor` / `bambu-monitor run`) vs Administrative CLI (`discover`, `onboard`, `devices`, `status`, `reconnect`, `remove`, `credentials`, `doctor`).
3. Interactive & Non-Interactive Onboarding with device selection and secure credential entry.
4. Secure Credential Storage (OS keyring / encrypted local storage with explicit fallback; secrets never stored in normal printer DB records or API responses).
5. Real Bambu MQTT Client with TLS support, `bblp` authentication, and self-signed cert bypass (`tls_verify: false`).
6. Automated subscription to `device/<serial>/report` and `pushall` request publishing to `device/<serial>/request`.
7. Reconnection resilience and dynamic IP address tracking based on stable hardware serial.
8. Bambu protocol delta parser feeding directly into the existing Phase 1 `StateManager`.


### Phase 3 — State Engine & Event Detection
1. State reconciliation and restart recovery (re-attaching to active jobs from SQLite).
2. Multi-factor adaptive stall detection.
3. Event detector emitting domain events and alert lifecycle events (`*_cleared`).
4. Throttled SQLite persistence flusher.

### Phase 4 — Reliable Outbox & Hermes Webhook Delivery
1. Background outbox delivery worker.
2. Per-printer sequential FIFO delivery.
3. Exponential backoff, retry management, and dead-letter queue (`failed`).
4. Webhook consumer contract verification.

### Phase 5 — Printer Controls & Camera (Future)
1. Command validation and MQTT command publishing (pause, resume, stop).
2. Camera snapshot endpoint.

---

# 22. Definition of Done — Phase 1 Framework

Phase 1 is complete when:

1. `bambu-monitor` installs and runs cleanly in Python 3.12+.
2. Configuration loads YAML and environment variables with strict Pydantic validation.
3. SQLite initializes automatically with `PRAGMA journal_mode = WAL;`.
4. Domain models represent printers, print jobs, telemetry patches, alerts, events, and outbox messages.
5. The In-Memory State Manager correctly merges incoming patches without dropping existing state.
6. The Alert State Machine manages `ACTIVE` → `ACKNOWLEDGED` → `RESOLVED` states and suppresses duplicate triggers.
7. Print job lifecycle correctly transitions states and computes deterministic IDs.
8. Repositories persist and query printers, jobs, alerts, events, and outbox messages.
9. FastAPI starts up cleanly; `GET /health` reports healthy database and memory state.
10. `GET /api/v1/printers/{printer_id}/prints/active` returns active job or 204 No Content.
11. `GET /api/v1/printers/{printer_id}/events/stream` establishes an SSE connection.
12. Outbox repository supports per-printer FIFO sequencing and dead-letter status.
13. Unit tests verify patch merging, job state machine, alert lifecycle, and stall detection math using fixtures.
14. Integration tests verify SQLite WAL persistence and FastAPI endpoints.
15. Tests execute and pass without requiring a physical printer or network access.
16. Code contains zero Hermes dependencies and leaks no Bambu protocol names into external API contracts.

### 22.1 Phase 1 End-to-End Success Criterion

The primary validation of Phase 1 is running:
```bash
bambu-monitor
```
with **no printer connected**, injecting fixture telemetry patches, and demonstrating the complete in-process pipeline end-to-end:

```text
fixture patch
      ↓
state merge
      ↓
print lifecycle
      ↓
alert lifecycle
      ↓
event
      ↓
SQLite (WAL)
      ↓
REST API (/prints/active)
      ↓
SSE (/events/stream)
      ↓
outbox (pending)
```

If this works cleanly, Phase 2 becomes purely a protocol integration problem, rather than an architectural problem.


---

---

# 23. Phase 2 — Real Bambu Device Integration & Administration CLI

Phase 2 transitions Bambu Monitor from fixture-driven simulation to a production-ready device service connected to physical Bambu Lab printers.

## 23.1 Operating Modes: Service vs Administration

The CLI provides two primary operating modes:
1. **Long-running Daemon**:
   * `bambu-monitor` (or `bambu-monitor run`): Starts the HTTP/SSE service and background monitoring engine.
   * **Zero-config startup**: On boot, automatically discovers printers on the local network, checks existing credentials, and begins monitoring. If an un-onboarded printer is found, it prompts for setup interactively.
2. **Administrative CLI**:
   * One-off administrative commands for discovery, onboarding, status inspection, and diagnostics without running a continuous server.

```text
┌─────────────────────────────────────────────────────────────┐
│                        bambu-monitor                        │
└──────────────┬───────────────────────────────┬──────────────┘
               │                               │
        service mode                     admin mode
               ▼                               ▼
       bambu-monitor run               bambu-monitor discover
       (or bambu-monitor)              bambu-monitor onboard
                                       bambu-monitor devices
                                       bambu-monitor status
                                       bambu-monitor reconnect <id>
                                       bambu-monitor remove <id>
                                       bambu-monitor credentials <id>
                                       bambu-monitor doctor
```

## 23.2 Administrative CLI Commands

### `bambu-monitor discover`
Scans the local network for Bambu printers using UDP broadcast / SSDP and outputs discovered devices:
```text
Searching for Bambu printers on the local network...

Found 2 printers:
  [1] Bambu A1 — Living Room (Serial: 01P00A123456789, IP: 192.168.1.42) - Available
  [2] Bambu A1 Mini — Office (Serial: 01P00B987654321, IP: 192.168.1.51) - Already Configured
```

### `bambu-monitor onboard`
Interactive setup wizard:
1. Discovers printers on the local network.
2. Prompts user to select a device if multiple are detected.
3. Prompts securely for LAN Access Code (masked input).
4. Performs verification handshake (TLS connect, `bblp` auth, initial state sync).
5. Stores credentials in local secure storage.
6. Saves printer record to database and enables monitoring.

Non-interactive flags supported for automation:
```bash
bambu-monitor onboard --serial 01P00A123456789 --ip 192.168.1.42 --access-code 12345678
```

### `bambu-monitor devices`
Lists all configured printers and their connection statuses:
```text
Configured printers:

  ID          Name          Model    IP              Status
  ─────────────────────────────────────────────────────────────
  bambu-a1    Living Room   A1       192.168.1.42    ● Online
  bambu-mini  Office        A1 Mini  192.168.1.51    ○ Offline
```

### `bambu-monitor status`
Displays live service and printer health:
```text
Bambu Monitor

Service       ● Running
Database      ● Healthy (WAL mode)
Printers      1 configured, 1 online
Outbox        0 pending, 0 failed

Living Room — Bambu A1
  Connection    ● Online
  State         printing
  Print         phone-holder.3mf
  Progress      67% (Layer 134/201)
  Remaining     31 min
  Temperatures  Nozzle: 220.0°C | Bed: 60.0°C
```

### `bambu-monitor doctor`
Comprehensive subsystem health and connectivity diagnostic:
```text
Bambu Monitor Diagnostics

Network Discovery       ✓ UDP 2021 listener operational
Printer Reachable       ✓ 192.168.1.42 ping / port 8883 open
TLS Handshake           ✓ Self-signed TLS certificate accepted
Authentication          ✓ User 'bblp' authenticated successfully
Initial State (pushall) ✓ Telemetry dump received and normalized
Credential Store        ✓ Secure OS keyring available
SQLite Database         ✓ WAL mode verified at ./data/bambu.db
Monitoring Pipeline     ✓ State manager and outbox ready
```

### `bambu-monitor reconnect <printer-id>`
Forces immediate MQTT disconnect, re-connect, re-subscription, and `pushall` request.

### `bambu-monitor remove <printer-id>`
Removes a printer from configuration, deletes stored credentials, and marks database records as archived.

### `bambu-monitor credentials <printer-id>`
Updates or re-prompts for the LAN Access Code for a printer when credentials change or become invalid.

## 23.3 LAN Discovery Protocol Details

Bambu Lab printers emit heartbeat UDP broadcasts on local port `2021` every few seconds:
```json
{
  "dev_name": "Living Room",
  "dev_id": "01P00A123456789",
  "dev_model_name": "A1",
  "dev_ip": "192.168.1.42",
  "dev_connect_type": "lan"
}
```
Discovery implementation:
1. **Passive listener**: Bind UDP socket to `0.0.0.0:2021` to capture broadcast packets.
2. **Active probe**: Send SSDP M-SEARCH broadcast query to `239.255.255.250:1900` and UDP broadcast on port `2021`.
3. Discovered printers are identified by `dev_id` (hardware serial).

## 23.4 Credential Security Architecture

The LAN Access Code is a sensitive credential:
* **Operating System Keyring**: Store secrets via system keyring service (`keyring` library targeting macOS Keychain, Linux Secret Service / Keyutils, Windows Credential Manager).
* **Local Encrypted Fallback**: If system keyring is unavailable (e.g. headless container or server), use a local file with restricted permissions (`0600`) protected with user-configured encryption or environment variable overrides.
* **Strict Redaction**:
  * Secrets are **never** committed or written to the `printers` SQLite table.
  * Secrets are **never** logged.
  * Secrets are **never** included in REST API responses, SSE event payloads, or outbox messages.

## 23.5 Network Resilience & Dynamic IP Tracking

Printers on local DHCP networks frequently change IP addresses:
1. Persistent identity is keyed to `serial_number`, **not** IP address.
2. If an existing onboarded printer's serial is discovered on a new IP:
   * Bambu Monitor automatically updates the `host` in the database.
   * Seamlessly initiates reconnect to the new IP without requiring re-onboarding or user intervention.

## 23.6 Phase 2 Definition of Done

Phase 2 is complete when:
1. `bambu-monitor discover` detects real Bambu printers on the local network.
2. `bambu-monitor onboard` interactively guides the user through device selection and credential entry.
3. Credentials are encrypted/stored via secure local credential management.
4. `bambu-monitor run` connects to the printer via MQTT over TLS (port 8883) with `bblp` authentication.
5. Client automatically issues `pushall` upon connection and reconnect.
6. Incoming Bambu delta MQTT payloads are parsed into `TelemetryPatch` and ingested into the Phase 1 `StateManager`.
7. `bambu-monitor devices`, `status`, and `doctor` output accurate live diagnostic information.
8. Network interruptions, printer reboots, and IP address changes automatically recover and resume monitoring without service restart.
9. All Phase 1 tests continue to pass.

---

# 24. Multiple-Printer Requirement

All state, events, jobs, outbox queues, and APIs are strictly partitioned by `printer_id`.

```yaml
printers:
  - id: bambu-a1
    model: A1
    host: 192.168.1.50
    serial_number: 01P00A123456789

  - id: bambu-a1-mini
    model: A1 Mini
    host: 192.168.1.51
    serial_number: 01P00B987654321
```

Outbox delivery for `bambu-a1-mini` continues uninterrupted even if `bambu-a1` is backing off or recovering from errors.

---

# 25. Core Architectural Axiom

> **Bambu Monitor owns device truth. Hermes owns intelligence.**

* **Bambu Monitor** answers: *"What happened?"*
* **Hermes** answers: *"What does it mean, and what should I do about it?"*

Bambu Monitor provides durable, high-fidelity device state and reliable domain events as a standalone local service, independent of any upstream consumer.


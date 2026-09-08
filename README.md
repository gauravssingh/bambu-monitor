# Bambu Monitor

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests: 119 Passed](https://img.shields.io/badge/tests-119%20passed-brightgreen.svg)]()
[![Architecture: Phase 1--4 Complete](https://img.shields.io/badge/architecture-Phase%201--4%20Complete-blueviolet.svg)]()
[![Database: SQLite WAL](https://img.shields.io/badge/storage-SQLite%20WAL-orange.svg)]()

**Bambu Monitor** is a standalone local service that turns Bambu Lab 3D printers on your local network into a reliable, queryable, event-driven service.

It continuously ingests delta-based MQTT telemetry from the printer, normalizes it into durable canonical state, manages print job and alert lifecycles, and delivers critical domain events to external consumers (like **Hermes** for Telegram notifications) through a reliable, transactional outbox queue.

> **Guiding Architectural Axiom**:  
> *Bambu Monitor owns device truth. Hermes owns intelligence.*  
> Bambu Monitor is completely standalone and contains zero dependencies on Hermes, cloud accounts, or external services.

---

## Architecture Overview

```mermaid
graph TD
    A[Bambu 3D Printer\ne.g. A1 / A1 Mini] -->|Raw MQTT deltas :8883 TLS| B[Bambu Monitor]
    
    subgraph "Bambu Monitor (Local Service)"
        B --> C[Bambu MQTT Ingestion\nClient ID isolation + pushall]
        C --> D[TelemetryPatch Normalizer]
        D --> E[In-Memory State Engine\nO 1 instant reads]
        E --> F[SQLite WAL Database\nPrinters · Jobs · Alerts · Events]
        E --> G[Alert & Job Lifecycles\nAdaptive stall detector]
        G --> H[Event Store & Outbox Queue\nPer-printer sequential FIFO]
    end

    H -->|HTTP POST + HMAC-SHA256\nPhase 4 Outbox Worker| I[Hermes Gateway :8644\n/webhooks/bambu-printer]
    I -->|Push Notification| J[Telegram Bot\nChat: 1117425083]
    
    E -->|REST API :8000| K[External Consumers / Dashboards]
    E -->|SSE Stream :8000| L[Real-Time Live UI]
```

---

## Core Capabilities

* **Real Device MQTT Ingestion**: Direct local TLS connection to printer on port 8883 using `bblp` authentication, scoped self-signed certificate bypass, unique client ID isolation, and automated `pushall` state synchronization.
* **Zero-Touch LAN Discovery**: Automatically discovers Bambu printers using UDP broadcast on port 2021 and SSDP M-SEARCH active probing, calibrated to Bambu's 10.24-second hardware heartbeat window.
* **Hardware-Grade Credential Security**: LAN Access Codes stored exclusively in the host OS Keyring (macOS Keychain, Linux Secret Service, Windows Credential Manager) with AES-GCM encrypted local fallback. Access codes are **never logged, never stored in SQLite database tables, and never exposed in REST APIs or SSE streams**.
* **Dynamic IP Tracking**: Printers are keyed to immutable hardware serial numbers. DHCP IP re-assignments are automatically detected on the LAN and reconnected with zero service restart.
* **Dual-Layer State Engine**: In-memory state representation for instantaneous reads, backed by throttled persistence to SQLite in mandatory `WAL` mode.
* **Crash & Restart Recovery**: Deterministically re-attaches to active print jobs across service restarts by reconciling physical G-code states against persisted database state without generating duplicate jobs.
* **Adaptive Multi-Factor Stall Detection**: Evaluates print duration, layer changes, nozzle temperature stability, and heating state to detect genuine nozzle blockages and mechanical stalls while preventing false positives on long print moves.
* **Alert Lifecycle & Spam Suppression**: Full alert state machine (`ACTIVE` → `ACKNOWLEDGED` → `RESOLVED`). Fires notifications on trigger and resolution while completely suppressing repeated telemetry noise during active conditions.
* **Reliable Outbox Delivery Worker (Phase 4)**: Background worker delivering domain events to webhook destinations with per-printer sequential FIFO ordering, exponential backoff, dead-letter queue (`failed` DLQ), and HMAC-SHA256 authentication.
* **Automated Camera Timelapse Subsystem**: Vendor-agnostic (TP-Link Tapo RTSP & Generic RTSP) periodic frame capture worker tightly integrated into canonical print lifecycle events (`print.started`, `print.paused`, `print.resumed`, `print.completed`, `print.failed`). Features zero-impact camera failure isolation (`DEGRADED`), pause duration accounting, restart reconciliation, gap-free re-indexing, and atomic MP4 encoding via FFmpeg.
* **Native Background Service Management**: Built-in CLI commands to start, stop, restart, check status, and tail logs for background operation.

---

## Quickstart & Installation

### 1. Requirements
* Python 3.12 or higher
* Bambu Lab 3D Printer (A1, A1 Mini, P1P, P1S, X1C) connected to the same LAN with **LAN Mode** enabled

### 2. Install
```bash
# Clone the repository
git clone https://github.com/gauravssingh/bambu-monitor.git
cd bambu-monitor

# Create virtual environment and install
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Optional: Symlink to user PATH for global CLI usage
mkdir -p ~/.local/bin
ln -sf $(pwd)/.venv/bin/bambu-monitor ~/.local/bin/bambu-monitor
```

---

## First-Time Onboarding

### Interactive Onboarding Wizard
Run the interactive onboarding command in your terminal:
```bash
bambu-monitor onboard
```

The wizard will:
1. Scan the local network for Bambu printers.
2. Display discovered printers with hardware model and IP address.
3. Prompt for the printer's **LAN Access Code** (found on printer screen: *Settings → Network → LAN Mode*).
4. Verify the TLS handshake and authentication.
5. Securely store credentials in your OS Keyring.
6. Register the printer in SQLite WAL storage.

```text
Searching for Bambu printers on the local network (listening up to 12s for heartbeat broadcasts)...

Found 1 printer:

  [1] BBL_A1_Mini (A1 Mini)
      Serial:  0309DA572602482
      IP:      192.168.68.57
      Port:    8883

Select printer [1]: 1

Enter LAN Access Code for A1 Mini (masked): ********
Testing connection to 192.168.68.57:8883...
✓ TLS connection established
✓ Authentication verified

✓ Printer registered: bambu-a1-mini-602482
✓ Credentials stored securely in OS Keyring
✓ Bambu A1 Mini is now ready for monitoring.
```

### Scripted / Automated Onboarding
```bash
bambu-monitor onboard \
  --serial 0309DA572602482 \
  --ip 192.168.68.57 \
  --access-code 12345678 \
  --model "A1 Mini" \
  --name "Lab A1 Mini"
```

---

## Running the Service

### Managing the Background Service
Bambu Monitor includes native service lifecycle management:

```bash
# Start background daemon
bambu-monitor service start

# Check service and printer health
bambu-monitor service status

# Tail live daemon logs
bambu-monitor service logs -n 50

# Restart or stop the daemon
bambu-monitor service restart
bambu-monitor service stop
```

### Running in Foreground Mode
```bash
bambu-monitor run --host 0.0.0.0 --port 8000
# or simply
bambu-monitor
```

---

## CLI Command Reference

| Command | Description |
| :--- | :--- |
| `bambu-monitor discover` | Scans local network for Bambu printers via UDP/SSDP. |
| `bambu-monitor onboard` | Launches interactive onboarding wizard or accepts scripted CLI arguments. |
| `bambu-monitor status` | Displays overview of database health, configured printers, active print, and outbox status. |
| `bambu-monitor devices` | Formatted table of all configured printers, IP addresses, and online statuses. |
| `bambu-monitor doctor` | Runs end-to-end diagnostics on SQLite WAL, credential storage, discovery, and TLS. |
| `bambu-monitor service <start\|stop\|restart\|status\|logs>` | Manages the background daemon process with PID tracking. |
| `bambu-monitor reconnect <printer_id>` | Forces immediate MQTT reconnect, re-subscription, and state pushall. |
| `bambu-monitor credentials <printer_id>` | Prompts for and updates stored LAN Access Code. |
| `bambu-monitor remove <printer_id>` | Unregisters printer and purges credentials from the OS Keyring. |

---

## Hermes & Telegram Push Notifications

Bambu Monitor integrates with **Hermes** to push real-time notifications to your **Telegram bot** for critical 3D printer events:
* **Print Completion**: When a job finishes, reporting duration and layer stats.
* **Blockage & Stalls**: When nozzle temperature is stable but zero progress occurs over the stall threshold.
* **Filament Runout**: When the active AMS / virtual tray reports filament empty (`filament.runout`).
* **Printer Failures**: When print is aborted or encounters hardware HMS fault codes.
* **Print Pauses**: When print is paused manually or by safety sensors.

### 1. Bambu Monitor Configuration ([config.yaml](config.yaml))
```yaml
events:
  delivery:
    enabled: true
    endpoint: ${EVENT_ENDPOINT:http://localhost:8644/webhooks/bambu-printer}
    secret: ${EVENT_SECRET:bambu-secret-8f92a4e7c10b42d591}
    # Hermes must be able to reach this URL to fetch alert snapshots.
    # public_base_url: ${BAMBU_MONITOR_PUBLIC_URL:http://localhost:8000}
    timeout_seconds: 10
    retry_attempts: 5
    initial_backoff_seconds: 2.0
    backoff_multiplier: 2.0
    max_backoff_seconds: 300.0
```

### 2. Hermes Webhook Subscription (`~/.hermes/webhook_subscriptions.json`)
```json
{
  "bambu-printer": {
    "description": "Bambu 3D printer event notifications for completion, stall, and errors",
    "events": [
      "print.completed",
      "print.failed",
      "print.paused",
      "print.resumed",
      "filament.runout",
      "filament.runout_cleared",
      "print.possible_blockage",
      "print.blockage_cleared",
      "print.started",
      "timelapse.completed"
    ],
    "secret": "bambu-secret-8f92a4e7c10b42d591",
    "prompt": "Bambu 3D Printer Event: {event_type}\nPrinter: {source}\nSeverity: {severity}\n\nEvent details:\n{__raw__}\n\nFor alert-like events, download the fresh snapshot from {camera_snapshot_url} and send it to Telegram using MEDIA:/tmp/bambu_alert.jpg.\nFor timelapse.completed events, download the finished MP4 video from {timelapse_video_url} and deliver it to Telegram as MEDIA:/tmp/timelapse.mp4 announcing that the print timelapse video is ready!",
    "deliver": "telegram",
    "deliver_extra": {
      "chat_id": "1117425083"
    }
  }
}
```

### 3. Guaranteed Delivery & Reverse Acknowledgement
* **Transport Acknowledgement**: When Bambu Monitor delivers an event, Hermes returns `HTTP 200 OK`. Bambu Monitor marks the event `delivered` in SQLite WAL and **never sends it again**.
* **Alert Spam Suppression**: While an alert condition persists, the state engine suppresses duplicate events. Only **one** notification fires on trigger, and **one** on resolution.
* **Operator Alert Acknowledgement**: Active alerts can be acknowledged via REST:
  ```bash
  curl -X POST http://localhost:8000/api/v1/printers/{printer_id}/alerts/{alert_id}/acknowledge
  ```

---

## REST & SSE API Reference

Base URL: `http://localhost:8000`

### Health & Status
* `GET /health` — Subsystem health check (SQLite WAL, memory status, outbox counts, printer summaries).
* `GET /api/v1/outbox/status` — Outbox queue counts (`pending`, `delivering`, `delivered`, `failed_dlq`).

### Printers
* `GET /api/v1/printers` — List all configured printers.
* `GET /api/v1/printers/{printer_id}` — Get printer details.
* `GET /api/v1/printers/{printer_id}/status` — Live canonical status:
  ```json
  {
    "printer_id": "bambu-a1-mini-602482",
    "model": "A1 Mini",
    "online": true,
    "state": "completed",
    "print": null,
    "temperatures": {
      "nozzle": 28.1,
      "nozzle_target": 0.0,
      "bed": 27.6,
      "bed_target": 0.0,
      "chamber": 5.0
    },
    "last_seen": "2026-09-08T13:01:57Z",
    "updated_at": "2026-09-08T13:01:57Z"
  }
  ```
* `POST /api/v1/printers/{printer_id}/reconnect` — Trigger immediate MQTT reconnect and pushall.

### Print Jobs
* `GET /api/v1/printers/{printer_id}/prints/active` — Active print job (`200 OK` or `204 No Content`).
* `GET /api/v1/printers/{printer_id}/prints` — Historical print records.

### Alerts
* `GET /api/v1/printers/{printer_id}/alerts?active_only=true` — Query active or historical alerts.
* `POST /api/v1/printers/{printer_id}/alerts/{alert_id}/acknowledge` — Transition alert from `ACTIVE` → `ACKNOWLEDGED`.

### Timelapses
* `GET /api/v1/printers/{printer_id}/timelapses/gallery` — Dedicated responsive HTML card gallery for browsing all timelapses recorded for a printer.
* `GET /api/v1/printers/{printer_id}/timelapses/{session_id}/view` — Interactive HTML5 video player and print statistics interface.
* `GET /api/v1/printers/{printer_id}/timelapses` — List all recorded timelapse sessions (JSON).
* `GET /api/v1/printers/{printer_id}/timelapses/{session_id}` — Detailed session status, pause records, and manifest (JSON).
* `GET /api/v1/printers/{printer_id}/timelapses/{session_id}/video` — Stream or download the generated MP4 video file (`video/mp4`).
* `GET /api/v1/printers/{printer_id}/timelapses/{session_id}/metadata` — Query frame-by-frame visual history sidecar records (`frames.jsonl`).
* `GET /api/v1/printers/{printer_id}/timelapses/{session_id}/frames` — List captured frame indices and retrieval URLs.
* `GET /api/v1/printers/{printer_id}/timelapses/{session_id}/frames/{sequence}` — Retrieve an individual JPEG frame (`image/jpeg`).

### Real-Time Events (SSE)
* `GET /api/v1/printers/{printer_id}/events/stream` — Real-time Server-Sent Events stream for live dashboards.

---

## Camera Timelapse Subsystem

Bambu Monitor includes a production-grade, camera-vendor-agnostic print timelapse subsystem designed specifically for external RTSP cameras (such as TP-Link Tapo C100, C110, C200, C210) pointed at the printer bed.

### 1. TP-Link Tapo Camera Setup
1. **Enable Local RTSP Account**:
   * Open the **TP-Link Tapo App** on iOS / Android.
   * Navigate to: *Camera Settings → Advanced Settings → Camera Account*.
   * Create a dedicated username and password for local stream access.
2. **Stream URLs**:
   * **High Definition (1080p / 2K)**: `rtsp://<username>:<password>@<camera-ip>:554/stream1`
   * **Standard Definition (360p)**: `rtsp://<username>:<password>@<camera-ip>:554/stream2`
3. **Environment Configuration**:
   Never commit camera passwords to configuration files. Set `TIMELAPSE_CAMERA_RTSP` in your `.env` or system environment:
   ```bash
   export TIMELAPSE_CAMERA_RTSP="rtsp://admin:YourSecretPass@192.168.1.55:554/stream1"
   ```

### 2. Architecture & Lifecycle State Machine
* **Lifecycle Driven**: Synchronizes automatically with printer events without polling or custom scripts:
  * `print.started` → Allocates session, creates filesystem directory, launches capture worker.
  * `print.paused` → Halts frame capture, tracks pause timestamps and cumulative paused seconds.
  * `print.resumed` → Restarts periodic capture ticks.
  * `print.completed` / `print.failed` → Finalizes capture, re-indexes frame sequences to guarantee gap-free continuity, compiles MP4 with FFmpeg atomically (`.tmp.mp4` → `timelapse.mp4`), and writes `manifest.json`.
* **Zero-Impact Camera Isolation**: If the camera goes offline during a print (e.g. WiFi glitch), the timelapse transitions to `DEGRADED` and backs off reconnection attempts. **Printer telemetry, stall detection, and alert forwarding continue completely uninterrupted.**
* **Restart Resilience**: If the service restarts during an active print, it re-attaches to the existing timelapse session and resumes capture without duplicate records or lost frames.
* **Smart Retention**: By default (`successful_frames: delete_after_video`), raw frame JPEGs are deleted after the final MP4 is verified to save disk space. Frames for failed prints are preserved (`failed_frames: retain`) for visual troubleshooting.

### 3. CLI Commands
```bash
# Test camera stream connectivity and grab a test snapshot:
bambu-monitor timelapse camera-test --printer bambu-a1-mini-602482

# Check live timelapse capture status across all printers:
bambu-monitor timelapse status

# List historical timelapse sessions:
bambu-monitor timelapse list

# Manually compile or re-render an MP4 video from stored frames:
bambu-monitor timelapse generate <session-id-or-job-id>

# Correlate printer telemetry (hotend/bed temps, speed, layers) with visual frames:
bambu-monitor timelapse correlate <printer-id> [session-id] [--temp-drop-threshold 10.0]
```

### 4. Telemetry-Vision Correlation & Defect Analysis
Bambu Monitor synchronizes the printer's sensor telemetry with every single visual frame recorded:
* **Per-Frame Sidecar Dataset (`frames.jsonl`)**: Each frame record logs live `nozzle_temp`, `nozzle_target`, `bed_temp`, `bed_target`, `layer`, `progress`, and `speed_level`.
* **Automated Thermal Anomaly Detection**: Detects sudden hotend temperature drops (e.g. `> 10°C` below target while printing) and bed drops, flagging the exact starting frame, recovery frame, and delta.
* **Extrusion Stall & Event Correlation**: Correlates motion stalls (`print.possible_blockage`), pauses (`print.paused`), filament runouts (`filament.runout`), and speed shifts (`print.speed_changed`) to their exact video timestamps and frame numbers.
* **REST API**: `GET /api/v1/printers/{printer_id}/timelapses/{session_id}/correlation` returns the full synchronized report and timeline.

### 5. Interactive Web UI Player (`/view`)
Open `GET /api/v1/printers/{printer_id}/timelapses/{session_id}/view` in any browser for an enhanced player:
* **Live Reactive HUD**: Displays real-time Hotend, Bed, Layer, Progress, and Speed corresponding to the video's active playback frame.
* **Interactive SVG Temperature Profile**: An inline dual-line curve displaying hotend and bed temperatures across the video. Clicking anywhere on the chart scrubs the video to that exact point.
* **Click-to-Jump Anomalies**: Badges for detected thermal drops and stalls that jump video playback directly to the fault frame.

---

## Security Architecture & Threat Boundaries

1. **Local Network Privacy**: Bambu Monitor communicates with Bambu printers exclusively over the local subnet (RFC 1918). No telemetry is transmitted to cloud servers.
2. **Scoped TLS Certificate Bypass**: Bambu Lab printers in LAN mode use self-signed X.509 certificates on port 8883. Disabling host verification (`tls_verify: false`) is **strictly isolated to local printer connections** and is never applied to outbound consumer webhooks.
3. **Secret Isolation**: Access codes are stored in the host OS Keyring. The database contains no credentials, making SQLite database files completely safe for backup and inspection.

---

## Testing & Diagnostics

The test suite is fully decoupled from physical printer hardware using stored fixtures:

```bash
# Run the complete test suite (119 unit and integration tests)
pytest -v

# Run system and network diagnostics on your environment
bambu-monitor doctor
```

---

## License

This project is licensed under the [MIT License](LICENSE).

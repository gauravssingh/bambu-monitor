# Bambu Monitor

Standalone local service for monitoring Bambu Lab 3D printers.

Bambu Monitor turns Bambu 3D printers on your local network into a reliable, queryable, event-driven service. It normalizes delta-based MQTT telemetry into durable canonical state, robust print job lifecycles, and semantic events.

> **Guiding Axiom**: *Bambu Monitor owns device truth. Hermes owns intelligence.*

---

## Features

* **Real Device Ingestion**: Connects locally over MQTT with TLS (port 8883), `bblp` authentication, self-signed certificate bypass, and automated `pushall` state synchronization.
* **Zero-Touch LAN Discovery**: Automatically discovers Bambu printers using UDP broadcast on port 2021 and SSDP M-SEARCH active probing.
* **Dual Operating Modes**: Runs as a long-running daemon service (`bambu-monitor run` or `bambu-monitor`) or a standalone administrative CLI tool.
* **Interactive & Automated Onboarding**: Discover and configure printers interactively or via scripted non-interactive CLI flags.
* **Hardware-Grade Credential Security**: LAN Access Codes stored securely in the host OS Keyring (macOS Keychain, Linux Secret Service, Windows Credential Manager) with AES-GCM encrypted local fallback. Credentials are never written to SQLite database records or exposed in APIs.
* **Dynamic IP Tracking**: Printers are keyed to immutable hardware serial numbers. DHCP IP changes are automatically detected and reconnected without service restart.
* **Dual-Layer State Engine**: In-memory state for instantaneous $O(1)$ reads, backed by SQLite in mandatory WAL mode for persistence.
* **Deterministic Print Tracking**: Automatic synthesis of deterministic job IDs, active print tracking, and crash-restart re-attachment.
* **Adaptive Stall Detection & Alert Lifecycle**: Multi-factor stall detection factoring total print duration, avoiding false positives on long prints, with alert suppression and resolution notifications.
* **REST & SSE APIs**: Comprehensive REST endpoints and real-time Server-Sent Events (SSE) streaming.

---

## Installation

Requires Python 3.12+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

---

## CLI Usage

`bambu-monitor` includes an administrative CLI and a background daemon:

### 1. Discover Printers on the Local Network
```bash
bambu-monitor discover
```
Scans UDP port 2021 and SSDP multicast for available printers.

### 2. Onboard a Printer
**Interactive Wizard**:
```bash
bambu-monitor onboard
```
Automatically scans, lets you select a printer, prompts securely for the LAN Access Code (masked), verifies the TLS handshake, and stores credentials securely.

**Automated / Scripted Onboarding**:
```bash
bambu-monitor onboard --serial 01P00A123456789 --ip 192.168.1.42 --access-code 12345678 --name "Living Room A1"
```

### 3. Check Devices & Health Status
```bash
# List configured devices and connection state
bambu-monitor devices

# View live service, printer telemetry, and print job progress
bambu-monitor status

# Run system, network, TLS, and credential diagnostics
bambu-monitor doctor
```

### 4. Manage Credentials & Connection
```bash
# Force immediate MQTT reconnect and telemetry pushall
bambu-monitor reconnect bambu-a1

# Update LAN Access Code
bambu-monitor credentials bambu-a1

# Remove printer and wipe stored credentials
bambu-monitor remove bambu-a1
```

### 5. Run Daemon Service
```bash
# Start background monitoring daemon and API server
bambu-monitor run --host 0.0.0.0 --port 8000
```
*(Or simply `bambu-monitor`)*

---

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | System, database (WAL), and printer health summary |
| `GET` | `/api/v1/printers` | List all configured printers |
| `GET` | `/api/v1/printers/{id}` | Get printer details |
| `GET` | `/api/v1/printers/{id}/status` | Current live state (temperatures, state, job) |
| `GET` | `/api/v1/printers/{id}/prints/active` | Current active print (`200 OK` or `204 No Content`) |
| `GET` | `/api/v1/printers/{id}/prints` | Historical prints for printer |
| `GET` | `/api/v1/printers/{id}/alerts` | Active and historical alerts |
| `POST`| `/api/v1/printers/{id}/alerts/{alert_id}/acknowledge` | Acknowledge active alert |
| `GET` | `/api/v1/printers/{id}/events` | Historical domain events |
| `GET` | `/api/v1/printers/{id}/events/stream` | Server-Sent Events (SSE) live event stream |
| `POST`| `/api/v1/printers/{id}/reconnect` | Trigger MQTT reconnect and pushall |
| `GET` | `/api/v1/outbox/status` | Outbox message counts (pending, delivering, failed) |

---

## Testing

The entire test suite runs independently of physical printer hardware:

```bash
# Run complete test suite (41 unit and integration tests)
pytest -v
```

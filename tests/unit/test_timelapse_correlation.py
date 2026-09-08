"""Unit tests for TelemetryCorrelator (thermal drops, stalls, events, layers)."""

from datetime import datetime, timezone, timedelta
import pytest

from bambu_monitor.domain.events import DomainEvent, EventSeverity
from bambu_monitor.timelapse.correlation import (
    TelemetryAnomalyType,
    TelemetryCorrelator,
)
from bambu_monitor.timelapse.models import TimelapseSession, TimelapseStatus


@pytest.fixture
def base_session():
    return TimelapseSession(
        id="tl-corr-1",
        printer_id="printer-corr",
        print_job_id="job-corr-1",
        status=TimelapseStatus.COMPLETED,
        frame_count=10,
        fps=30,
        storage_dir="/tmp/tl-corr-1",
        started_at=datetime(2026, 9, 9, 10, 0, 0, tzinfo=timezone.utc),
        completed_at=datetime(2026, 9, 9, 10, 10, 0, tzinfo=timezone.utc),
    )


def test_correlate_empty_metadata(base_session):
    report = TelemetryCorrelator.correlate(base_session, [])
    assert report.total_frames == 0
    assert report.video_duration_seconds == 0.0
    assert report.thermal_summary.nozzle is None
    assert len(report.anomalies) == 0
    assert len(report.layers) == 0
    assert len(report.timeline) == 0


def test_correlate_normal_print(base_session):
    t0 = datetime(2026, 9, 9, 10, 0, 0, tzinfo=timezone.utc)
    frames = []
    for i in range(1, 11):
        ts = t0 + timedelta(seconds=i * 10)
        layer = 1 if i <= 5 else 2
        frames.append({
            "frame": i,
            "filename": f"{i:06d}.jpg",
            "timestamp": ts.isoformat(),
            "nozzle_temp": 220.0 + (0.5 if i % 2 == 0 else -0.5),
            "nozzle_target": 220.0,
            "bed_temp": 60.0,
            "bed_target": 60.0,
            "layer": layer,
            "progress": i * 10.0,
            "speed_percent": 100,
            "printer_state": "printing",
        })

    report = TelemetryCorrelator.correlate(base_session, frames)
    assert report.total_frames == 10
    assert report.fps == 30
    assert report.video_duration_seconds == 0.33
    assert len(report.anomalies) == 0
    assert report.thermal_summary.stability_score >= 95.0

    noz = report.thermal_summary.nozzle
    assert noz is not None
    assert noz.min == 219.5
    assert noz.max == 220.5
    assert noz.avg == 220.0
    assert noz.target == 220.0

    assert len(report.layers) == 2
    assert report.layers[0].layer == 1
    assert report.layers[0].frame_count == 5
    assert report.layers[1].layer == 2
    assert report.layers[1].frame_count == 5


def test_correlate_hotend_temp_drop(base_session):
    t0 = datetime(2026, 9, 9, 10, 0, 0, tzinfo=timezone.utc)
    frames = []
    for i in range(1, 16):
        ts = t0 + timedelta(seconds=i * 5)
        # Frames 6 to 9 drop significantly below target 220
        if 6 <= i <= 9:
            noz_t = 185.0
        else:
            noz_t = 220.0

        frames.append({
            "frame": i,
            "filename": f"{i:06d}.jpg",
            "timestamp": ts.isoformat(),
            "nozzle_temp": noz_t,
            "nozzle_target": 220.0,
            "bed_temp": 60.0,
            "bed_target": 60.0,
            "layer": 3,
            "progress": i * 6.0,
            "printer_state": "printing",
        })

    report = TelemetryCorrelator.correlate(base_session, frames, temp_drop_threshold=10.0)
    assert len(report.anomalies) == 1
    anom = report.anomalies[0]
    assert anom.type == TelemetryAnomalyType.NOZZLE_TEMP_DROP
    assert anom.severity == "critical"  # delta is 35 >= 25
    assert anom.start_frame == 6
    assert anom.end_frame == 9
    assert anom.metrics["min_temp"] == 185.0
    assert anom.metrics["delta"] == 35.0

    # Cross-reference check: frames 6-9 should have anom.id in anomaly_ids
    for pt in report.timeline:
        if 6 <= pt.frame <= 9:
            assert anom.id in pt.anomaly_ids
        else:
            assert anom.id not in pt.anomaly_ids

    # Stability score penalized
    assert report.thermal_summary.stability_score < 90.0


def test_correlate_bed_temp_drop(base_session):
    t0 = datetime(2026, 9, 9, 10, 0, 0, tzinfo=timezone.utc)
    frames = []
    for i in range(1, 10):
        ts = t0 + timedelta(seconds=i * 5)
        bed_t = 42.0 if 4 <= i <= 6 else 60.0
        frames.append({
            "frame": i,
            "filename": f"{i:06d}.jpg",
            "timestamp": ts.isoformat(),
            "nozzle_temp": 220.0,
            "nozzle_target": 220.0,
            "bed_temp": bed_t,
            "bed_target": 60.0,
            "layer": 2,
            "progress": 20.0,
            "printer_state": "printing",
        })

    report = TelemetryCorrelator.correlate(base_session, frames, temp_drop_threshold=10.0)
    assert len(report.anomalies) == 1
    anom = report.anomalies[0]
    assert anom.type == TelemetryAnomalyType.BED_TEMP_DROP
    assert anom.start_frame == 4
    assert anom.end_frame == 6
    assert anom.metrics["min_temp"] == 42.0
    assert anom.metrics["delta"] == 18.0


def test_correlate_events_stalls_and_runout(base_session):
    t0 = datetime(2026, 9, 9, 10, 0, 0, tzinfo=timezone.utc)
    frames = []
    for i in range(1, 11):
        ts = t0 + timedelta(seconds=i * 10)
        frames.append({
            "frame": i,
            "filename": f"{i:06d}.jpg",
            "timestamp": ts.isoformat(),
            "nozzle_temp": 220.0,
            "nozzle_target": 220.0,
            "bed_temp": 60.0,
            "bed_target": 60.0,
            "layer": i,
            "progress": i * 10.0,
            "printer_state": "printing",
        })

    events = [
        DomainEvent.create(
            printer_id=base_session.printer_id,
            event_type="print.possible_blockage",
            severity=EventSeverity.WARNING,
            payload={"layer": 4, "progress": 40.0},
            timestamp=t0 + timedelta(seconds=41),  # closest to frame 4 (40s)
        ),
        DomainEvent.create(
            printer_id=base_session.printer_id,
            event_type="filament.runout",
            severity=EventSeverity.CRITICAL,
            payload={"layer": 7, "progress": 70.0},
            timestamp=t0 + timedelta(seconds=72),  # closest to frame 7 (70s)
        ),
        DomainEvent.create(
            printer_id=base_session.printer_id,
            event_type="print.speed_changed",
            severity=EventSeverity.INFO,
            payload={"speed_level": 3, "speed_magnitude": 124},
            timestamp=t0 + timedelta(seconds=90),  # closest to frame 9 (90s)
        ),
    ]

    report = TelemetryCorrelator.correlate(base_session, frames, events=events)
    assert len(report.anomalies) == 3

    types = [a.type for a in report.anomalies]
    assert TelemetryAnomalyType.STALL in types
    assert TelemetryAnomalyType.FILAMENT_RUNOUT in types
    assert TelemetryAnomalyType.SPEED_CHANGE in types

    stall_anom = next(a for a in report.anomalies if a.type == TelemetryAnomalyType.STALL)
    assert stall_anom.start_frame == 4

    runout_anom = next(a for a in report.anomalies if a.type == TelemetryAnomalyType.FILAMENT_RUNOUT)
    assert runout_anom.start_frame == 7

    speed_anom = next(a for a in report.anomalies if a.type == TelemetryAnomalyType.SPEED_CHANGE)
    assert speed_anom.start_frame == 9
    assert "Sport (124%)" in speed_anom.description

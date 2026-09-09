"""Telemetry-Vision Correlation Engine for Bambu Monitor timelapses.

Synchronizes per-frame visual metadata (frames.jsonl) with printer sensor metrics
(temperatures, speeds, layers) and domain events (blockages, runouts, pauses).
Detects thermal drops, extrusion stalls, and produces synchronized timelines.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple
from pydantic import BaseModel, Field

from bambu_monitor.domain.events import DomainEvent
from bambu_monitor.timelapse.models import TimelapseSession


def parse_datetime(val: Any) -> datetime:
    """Parse ISO timestamp or return current UTC."""
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc)
        return val
    if isinstance(val, (int, float)):
        return datetime.fromtimestamp(val, tz=timezone.utc)
    if isinstance(val, str):
        try:
            dt = datetime.fromisoformat(val)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            pass
    return datetime.now(timezone.utc)


class TelemetryAnomalyType(str, Enum):
    NOZZLE_TEMP_DROP = "nozzle_temp_drop"
    BED_TEMP_DROP = "bed_temp_drop"
    STALL = "stall"
    FILAMENT_RUNOUT = "filament_runout"
    PAUSE = "pause"
    SPEED_CHANGE = "speed_change"


class TelemetryAnomaly(BaseModel):
    id: str
    type: TelemetryAnomalyType
    severity: str  # "info" | "warning" | "critical"
    start_frame: int
    end_frame: Optional[int] = None
    timestamp_start: datetime
    timestamp_end: Optional[datetime] = None
    video_time_start: float
    video_time_end: Optional[float] = None
    description: str
    metrics: Dict[str, Any] = Field(default_factory=dict)


class ThermalStats(BaseModel):
    min: float
    max: float
    avg: float
    target: Optional[float] = None
    samples: int = 0


class ThermalSummary(BaseModel):
    nozzle: Optional[ThermalStats] = None
    bed: Optional[ThermalStats] = None
    chamber: Optional[ThermalStats] = None
    stability_score: float = 100.0


class LayerCorrelation(BaseModel):
    layer: int
    start_frame: int
    end_frame: int
    frame_count: int
    start_time: datetime
    end_time: Optional[datetime] = None
    video_time_seconds: float
    avg_nozzle_temp: Optional[float] = None
    avg_bed_temp: Optional[float] = None
    avg_progress: Optional[float] = None


class TimelinePoint(BaseModel):
    frame: int
    filename: str
    timestamp: datetime
    video_time_seconds: float
    layer: Optional[int] = None
    progress: Optional[float] = None
    nozzle_temp: Optional[float] = None
    nozzle_target: Optional[float] = None
    bed_temp: Optional[float] = None
    bed_target: Optional[float] = None
    chamber_temp: Optional[float] = None
    speed_level: Optional[int] = None
    speed_percent: Optional[int] = None
    fan_speed: Optional[int] = None
    state: Optional[str] = None
    reason: Optional[str] = None
    anomaly_ids: List[str] = Field(default_factory=list)


class CorrelationReport(BaseModel):
    session_id: str
    printer_id: str
    print_job_id: str
    total_frames: int
    fps: int
    video_duration_seconds: float
    thermal_summary: ThermalSummary
    anomalies: List[TelemetryAnomaly] = Field(default_factory=list)
    layers: List[LayerCorrelation] = Field(default_factory=list)
    timeline: List[TimelinePoint] = Field(default_factory=list)


class TelemetryCorrelator:
    """Core analytics engine correlating visual frames with telemetry streams."""

    @staticmethod
    def correlate(
        session: TimelapseSession,
        frames_metadata: List[Dict[str, Any]],
        events: Optional[List[DomainEvent]] = None,
        temp_drop_threshold: float = 10.0,
    ) -> CorrelationReport:
        """Analyze frame metadata and domain events to produce a synchronized report."""
        fps = session.video_fps or 30
        total_frames = len(frames_metadata)
        video_duration = round(total_frames / fps, 2) if total_frames > 0 else 0.0

        if not frames_metadata:
            return CorrelationReport(
                session_id=session.id,
                printer_id=session.printer_id,
                print_job_id=session.print_job_id,
                total_frames=0,
                fps=fps,
                video_duration_seconds=0.0,
                thermal_summary=ThermalSummary(),
                anomalies=[],
                layers=[],
                timeline=[],
            )

        # 1. Parse timeline points
        timeline: List[TimelinePoint] = []
        for idx, rec in enumerate(frames_metadata):
            frame_num = int(rec.get("frame", idx + 1))
            fname = rec.get("filename", f"{frame_num:06d}.jpg")
            ts = parse_datetime(rec.get("timestamp"))
            # Video time derives from the position in the ordered sequence,
            # matching the renderer, which re-indexes frame files to a
            # contiguous 1..N sequence whenever capture gaps exist. Using the
            # raw frame number would drift from the real video position.
            video_sec = round(idx / fps, 3)

            def _clean_float(val: Any) -> Optional[float]:
                if val is None:
                    return None
                try:
                    return round(float(val), 1)
                except (ValueError, TypeError):
                    return None

            def _clean_int(val: Any) -> Optional[int]:
                if val is None:
                    return None
                try:
                    return int(val)
                except (ValueError, TypeError):
                    return None

            pt = TimelinePoint(
                frame=frame_num,
                filename=fname,
                timestamp=ts,
                video_time_seconds=video_sec,
                layer=_clean_int(rec.get("layer")),
                progress=_clean_float(rec.get("progress")),
                nozzle_temp=_clean_float(rec.get("nozzle_temp")),
                nozzle_target=_clean_float(rec.get("nozzle_target")),
                bed_temp=_clean_float(rec.get("bed_temp")),
                bed_target=_clean_float(rec.get("bed_target")),
                chamber_temp=_clean_float(rec.get("chamber_temp")),
                speed_level=_clean_int(rec.get("speed_level")),
                speed_percent=_clean_int(rec.get("speed_percent")),
                fan_speed=_clean_int(rec.get("cooling_fan_speed")),
                state=rec.get("state") or rec.get("printer_state"),
                reason=rec.get("reason"),
                anomaly_ids=[],
            )
            timeline.append(pt)

        # 2. Detect Thermal Anomalies
        anomaly_counter = 1
        nozzle_anomalies, anomaly_counter = TelemetryCorrelator._detect_temperature_drops(
            timeline,
            get_temp=lambda p: p.nozzle_temp,
            get_target=lambda p: p.nozzle_target,
            min_target=100.0,
            threshold=temp_drop_threshold,
            critical_delta=25.0,
            anomaly_type=TelemetryAnomalyType.NOZZLE_TEMP_DROP,
            label="Hotend",
            start_counter=anomaly_counter,
        )
        bed_anomalies, anomaly_counter = TelemetryCorrelator._detect_temperature_drops(
            timeline,
            get_temp=lambda p: p.bed_temp,
            get_target=lambda p: p.bed_target,
            min_target=35.0,
            threshold=temp_drop_threshold,
            critical_delta=20.0,
            anomaly_type=TelemetryAnomalyType.BED_TEMP_DROP,
            label="Bed",
            start_counter=anomaly_counter,
        )
        anomalies: List[TelemetryAnomaly] = [*nozzle_anomalies, *bed_anomalies]

        # 3. Correlate Domain Events
        if events:
            def find_closest_frame(event_time: datetime) -> TimelinePoint:
                return min(
                    timeline,
                    key=lambda p: abs((p.timestamp - event_time).total_seconds()),
                )

            for evt in events:
                evt_type = evt.event_type
                if evt_type == "print.possible_blockage":
                    pt = find_closest_frame(evt.timestamp)
                    aid = f"anom-{anomaly_counter}"
                    anomaly_counter += 1
                    layer_info = evt.payload.get("layer", pt.layer or 0)
                    anomalies.append(
                        TelemetryAnomaly(
                            id=aid,
                            type=TelemetryAnomalyType.STALL,
                            severity="warning",
                            start_frame=pt.frame,
                            end_frame=pt.frame,
                            timestamp_start=evt.timestamp,
                            timestamp_end=evt.timestamp,
                            video_time_start=pt.video_time_seconds,
                            video_time_end=pt.video_time_seconds,
                            description=f"Extrusion blockage or motion stall detected at Layer {layer_info}",
                            metrics=evt.payload,
                        )
                    )

                elif evt_type == "filament.runout":
                    pt = find_closest_frame(evt.timestamp)
                    aid = f"anom-{anomaly_counter}"
                    anomaly_counter += 1
                    layer_info = evt.payload.get("layer", pt.layer or 0)
                    anomalies.append(
                        TelemetryAnomaly(
                            id=aid,
                            type=TelemetryAnomalyType.FILAMENT_RUNOUT,
                            severity="critical",
                            start_frame=pt.frame,
                            end_frame=pt.frame,
                            timestamp_start=evt.timestamp,
                            timestamp_end=evt.timestamp,
                            video_time_start=pt.video_time_seconds,
                            video_time_end=pt.video_time_seconds,
                            description=f"Filament runout detected at Frame {pt.frame} (Layer {layer_info})",
                            metrics=evt.payload,
                        )
                    )

                elif evt_type == "print.paused":
                    pt = find_closest_frame(evt.timestamp)
                    aid = f"anom-{anomaly_counter}"
                    anomaly_counter += 1
                    anomalies.append(
                        TelemetryAnomaly(
                            id=aid,
                            type=TelemetryAnomalyType.PAUSE,
                            severity="info",
                            start_frame=pt.frame,
                            end_frame=pt.frame,
                            timestamp_start=evt.timestamp,
                            timestamp_end=evt.timestamp,
                            video_time_start=pt.video_time_seconds,
                            video_time_end=pt.video_time_seconds,
                            description=f"Print paused at Frame {pt.frame} ({pt.progress or 0}% progress)",
                            metrics=evt.payload,
                        )
                    )

                elif evt_type == "print.speed_changed":
                    pt = find_closest_frame(evt.timestamp)
                    aid = f"anom-{anomaly_counter}"
                    anomaly_counter += 1
                    new_lvl = evt.payload.get("speed_level", 1)
                    speed_names = {1: "Standard (100%)", 2: "Silent (50%)", 3: "Sport (124%)", 4: "Ludicrous (166%)"}
                    spd_name = speed_names.get(new_lvl, f"Level {new_lvl}")
                    anomalies.append(
                        TelemetryAnomaly(
                            id=aid,
                            type=TelemetryAnomalyType.SPEED_CHANGE,
                            severity="info",
                            start_frame=pt.frame,
                            end_frame=pt.frame,
                            timestamp_start=evt.timestamp,
                            timestamp_end=evt.timestamp,
                            video_time_start=pt.video_time_seconds,
                            video_time_end=pt.video_time_seconds,
                            description=f"Print speed changed to {spd_name}",
                            metrics=evt.payload,
                        )
                    )

        # 4. Cross-reference anomalies onto TimelinePoints
        anomalies.sort(key=lambda a: (a.start_frame, a.video_time_start))
        for anom in anomalies:
            end_f = anom.end_frame if anom.end_frame is not None else anom.start_frame
            for pt in timeline:
                if anom.start_frame <= pt.frame <= end_f:
                    if anom.id not in pt.anomaly_ids:
                        pt.anomaly_ids.append(anom.id)

        # 5. Layer Aggregations
        layer_groups: Dict[int, List[TimelinePoint]] = {}
        for pt in timeline:
            if pt.layer is not None and pt.layer > 0:
                layer_groups.setdefault(pt.layer, []).append(pt)

        layer_correlations: List[LayerCorrelation] = []
        for l_num in sorted(layer_groups.keys()):
            pts = layer_groups[l_num]
            noz_temps = [p.nozzle_temp for p in pts if p.nozzle_temp is not None]
            bed_temps = [p.bed_temp for p in pts if p.bed_temp is not None]
            progs = [p.progress for p in pts if p.progress is not None]

            layer_correlations.append(
                LayerCorrelation(
                    layer=l_num,
                    start_frame=pts[0].frame,
                    end_frame=pts[-1].frame,
                    frame_count=len(pts),
                    start_time=pts[0].timestamp,
                    end_time=pts[-1].timestamp,
                    video_time_seconds=pts[0].video_time_seconds,
                    avg_nozzle_temp=round(sum(noz_temps) / len(noz_temps), 1) if noz_temps else None,
                    avg_bed_temp=round(sum(bed_temps) / len(bed_temps), 1) if bed_temps else None,
                    avg_progress=round(sum(progs) / len(progs), 1) if progs else None,
                )
            )

        # 6. Thermal Performance Summary
        all_noz = [p.nozzle_temp for p in timeline if p.nozzle_temp is not None]
        all_noz_target = [p.nozzle_target for p in timeline if p.nozzle_target is not None and p.nozzle_target > 0]
        all_bed = [p.bed_temp for p in timeline if p.bed_temp is not None]
        all_bed_target = [p.bed_target for p in timeline if p.bed_target is not None and p.bed_target > 0]
        all_chamb = [p.chamber_temp for p in timeline if p.chamber_temp is not None]

        noz_summary = None
        if all_noz:
            noz_summary = ThermalStats(
                min=round(min(all_noz), 1),
                max=round(max(all_noz), 1),
                avg=round(sum(all_noz) / len(all_noz), 1),
                target=round(sum(all_noz_target) / len(all_noz_target), 1) if all_noz_target else None,
                samples=len(all_noz),
            )

        bed_summary = None
        if all_bed:
            bed_summary = ThermalStats(
                min=round(min(all_bed), 1),
                max=round(max(all_bed), 1),
                avg=round(sum(all_bed) / len(all_bed), 1),
                target=round(sum(all_bed_target) / len(all_bed_target), 1) if all_bed_target else None,
                samples=len(all_bed),
            )

        chamb_summary = None
        if all_chamb:
            chamb_summary = ThermalStats(
                min=round(min(all_chamb), 1),
                max=round(max(all_chamb), 1),
                avg=round(sum(all_chamb) / len(all_chamb), 1),
                target=None,
                samples=len(all_chamb),
            )

        # Stability calculation: penalize deviations from target and detected thermal drops
        stability = 100.0
        if all_noz and noz_summary and noz_summary.target:
            diffs = [(t - noz_summary.target) ** 2 for t in all_noz]
            std_dev = math.sqrt(sum(diffs) / len(diffs))
            penalty = (std_dev * 4.0) + (len(anomalies) * 12.0)
            stability = max(0.0, min(100.0, round(100.0 - penalty, 1)))

        thermal_summary = ThermalSummary(
            nozzle=noz_summary,
            bed=bed_summary,
            chamber=chamb_summary,
            stability_score=stability,
        )

        return CorrelationReport(
            session_id=session.id,
            printer_id=session.printer_id,
            print_job_id=session.print_job_id,
            total_frames=total_frames,
            fps=fps,
            video_duration_seconds=video_duration,
            thermal_summary=thermal_summary,
            anomalies=anomalies,
            layers=layer_correlations,
            timeline=timeline,
        )

    @staticmethod
    def _detect_temperature_drops(
        timeline: List[TimelinePoint],
        get_temp: Callable[[TimelinePoint], Optional[float]],
        get_target: Callable[[TimelinePoint], Optional[float]],
        min_target: float,
        threshold: float,
        critical_delta: float,
        anomaly_type: TelemetryAnomalyType,
        label: str,
        start_counter: int,
    ) -> Tuple[List[TelemetryAnomaly], int]:
        """Scan a timeline for sustained below-target temperature drops.

        Shared by hotend and bed detection: both are the same "temperature
        stayed too far below its target for a stretch of frames" state
        machine, parametrized only by which fields and thresholds apply.
        """
        anomalies: List[TelemetryAnomaly] = []
        counter = start_counter
        in_drop = False
        drop_start: Optional[TimelinePoint] = None
        min_temp = 999.0
        target = 0.0

        def close_drop(end_frame: int, timestamp_end: datetime, video_time_end: float) -> None:
            nonlocal counter
            assert drop_start is not None
            delta = round(target - min_temp, 1)
            aid = f"anom-{counter}"
            counter += 1
            severity = "critical" if delta >= critical_delta else "warning"
            anomalies.append(
                TelemetryAnomaly(
                    id=aid,
                    type=anomaly_type,
                    severity=severity,
                    start_frame=drop_start.frame,
                    end_frame=end_frame,
                    timestamp_start=drop_start.timestamp,
                    timestamp_end=timestamp_end,
                    video_time_start=drop_start.video_time_seconds,
                    video_time_end=video_time_end,
                    description=f"{label} temperature dropped to {min_temp:.1f}°C ({delta:.1f}°C below target {target:.1f}°C)",
                    metrics={"min_temp": min_temp, "target_temp": target, "delta": delta},
                )
            )

        for pt in timeline:
            temp = get_temp(pt)
            tgt = get_target(pt)
            if temp is not None and tgt is not None and tgt >= min_target:
                if temp <= (tgt - threshold):
                    if not in_drop:
                        in_drop = True
                        drop_start = pt
                        min_temp = temp
                        target = tgt
                    else:
                        min_temp = min(min_temp, temp)
                else:
                    if in_drop and drop_start is not None:
                        close_drop(pt.frame - 1, pt.timestamp, pt.video_time_seconds)
                        in_drop = False
                        drop_start = None

        if in_drop and drop_start is not None:
            last_pt = timeline[-1]
            close_drop(last_pt.frame, last_pt.timestamp, last_pt.video_time_seconds)

        return anomalies, counter

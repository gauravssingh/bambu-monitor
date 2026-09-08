"""Domain models for telemetry patches and raw payload mapping."""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Dict, Optional, Tuple
from pydantic import BaseModel, Field

from bambu_monitor.domain.printer import PrinterState


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def map_gcode_state_to_printer_state(gcode_state: str | None) -> PrinterState | None:
    if not gcode_state:
        return None
    state_upper = gcode_state.upper()
    mapping = {
        "IDLE": PrinterState.IDLE,
        "PREPARE": PrinterState.PREPARING,
        "RUNNING": PrinterState.PRINTING,
        "PAUSE": PrinterState.PAUSED,
        "FINISH": PrinterState.COMPLETED,
        "FAILED": PrinterState.FAILED,
        "OFFLINE": PrinterState.OFFLINE,
    }
    return mapping.get(state_upper, PrinterState.UNKNOWN)


def _as_int(value: Any) -> Optional[int]:
    """Parse Bambu's mixed decimal/hex numeric fields without raising."""
    try:
        return int(str(value), 0)
    except (TypeError, ValueError):
        return None


def _detect_filament_runout(payload: Dict[str, Any]) -> Tuple[Optional[bool], Dict[str, Any]]:
    """Detect an active filament runout from common Bambu telemetry shapes.

    Bambu firmware versions expose this through slightly different fields. We
    accept explicit runout flags first, then inspect the active AMS/virtual tray
    only when its remaining amount is explicitly zero. An arbitrary tray with
    zero filament is not considered a runout because it may not be selected.
    """
    explicit_true = {
        "filament_runout", "filament_out", "filament_empty", "spool_empty",
        "runout", "material_runout", "material_out",
    }
    explicit_false = {"filament_present", "material_present"}

    def walk(value: Any, path: str = "") -> Tuple[Optional[bool], Dict[str, Any]]:
        if isinstance(value, dict):
            for key, child in value.items():
                key_lower = str(key).lower()
                child_path = f"{path}.{key}" if path else str(key)
                if key_lower in explicit_true and isinstance(child, bool):
                    return child, {"source": child_path, "value": child}
                if key_lower in explicit_false and isinstance(child, bool):
                    return not child, {"source": child_path, "value": child}
                detected = walk(child, child_path)
                if detected[0] is not None:
                    return detected
        elif isinstance(value, list):
            for idx, child in enumerate(value):
                detected = walk(child, f"{path}[{idx}]")
                if detected[0] is not None:
                    return detected
        return None, {}

    detected = walk(payload)
    if detected[0] is not None:
        return detected

    is_paused = str(payload.get("gcode_state", "")).upper() == "PAUSE"

    # HMS runout codes observed in Bambu reports. Codes are normalized because
    # firmware/apps may send them as HMS_0700_2000_0002_0001, with hyphens, or
    # as a numeric value. The 0002_0001 suffix means filament out; 0002/0003
    # are distinct empty/broken-filament conditions.
    hms_values = payload.get("hms", []) if isinstance(payload, dict) else []
    if isinstance(hms_values, dict):
        hms_values = [hms_values]
    if isinstance(hms_values, list):
        for hms in hms_values:
            if not isinstance(hms, dict):
                continue
            raw_code = str(hms.get("code", ""))
            normalized = re.sub(r"[^0-9A-Fa-f]", "", raw_code).upper()
            code_value = _as_int(hms.get("code"))
            attr_value = _as_int(hms.get("attr"))
            is_slot_runout = normalized.startswith("0700") and normalized.endswith("00020001")
            is_external_runout = normalized.startswith("07FF") and normalized.endswith("00020001")
            is_generic_ams_runout = normalized in {"07008001", "0700800100000000"}
            if is_slot_runout or is_external_runout or is_generic_ams_runout:
                return True, {"source": "hms.code", "code": raw_code}

            # Live A1 Mini external-spool runout report:
            # hms.code=0x00020001, hms.attr=0x12ff2000,
            # print_error=0x07ff8011, gcode_state=PAUSE.
            if (
                is_paused
                and code_value == 0x00020001
                and attr_value is not None
                and (attr_value >> 16) == 0x12FF
            ):
                return True, {
                    "source": "hms.a1_mini_external_runout",
                    "code": raw_code,
                    "attr": hms.get("attr"),
                    "print_error": payload.get("print_error"),
                }

    ams = payload.get("ams") if isinstance(payload, dict) else None
    if not isinstance(ams, dict):
        return None, {}

    active_id = str(ams.get("tray_now", ""))
    if active_id in ("", "-1", "None", "254", "255"):
        # `remain` is configuration metadata for an external spool, not a
        # reliable live sensor. Never raise an alert from it by itself.
        return None, {}

    ams_units = ams.get("ams", [])
    if isinstance(ams_units, dict):
        ams_units = [ams_units]
    if isinstance(ams_units, list):
        for unit in ams_units:
            if not isinstance(unit, dict):
                continue
            trays = unit.get("tray", [])
            if isinstance(trays, dict):
                trays = [trays]
            if not isinstance(trays, list):
                continue
            ams_id = int(unit.get("id", 0)) if str(unit.get("id", "0")).isdigit() else 0
            for tray in trays:
                tray_id = str(tray.get("id", "")) if isinstance(tray, dict) else ""
                absolute_tray_id = str(ams_id * 4 + int(tray_id)) if tray_id.isdigit() else tray_id
                if isinstance(tray, dict) and (tray_id == active_id or absolute_tray_id == active_id):
                    # Tray `remain` is an estimate/calibration value, so it
                    # must not create or clear a safety alert without a runout
                    # sensor/HMS signal.
                    return None, {}
    return None, {}


class TelemetryPatch(BaseModel):
    """Normalized partial telemetry patch.
    
    Crucial contract: Only fields explicitly provided are updated.
    Missing fields (None) must never overwrite or clear existing state.
    """
    printer_id: str
    timestamp: datetime = Field(default_factory=utc_now)
    online: Optional[bool] = None
    state: Optional[PrinterState] = None
    gcode_state: Optional[str] = None
    progress: Optional[int] = None
    layer: Optional[int] = None
    total_layers: Optional[int] = None
    remaining_seconds: Optional[int] = None
    nozzle_temperature: Optional[float] = None
    nozzle_target_temperature: Optional[float] = None
    bed_temperature: Optional[float] = None
    bed_target_temperature: Optional[float] = None
    chamber_temperature: Optional[float] = None
    subtask_name: Optional[str] = None
    task_id: Optional[str] = None
    subtask_id: Optional[str] = None
    error_code: Optional[int] = None
    filament_runout: Optional[bool] = None
    filament_runout_details: Dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_raw(cls, printer_id: str, raw: Dict[str, Any], timestamp: Optional[datetime] = None) -> TelemetryPatch:
        """Parse raw telemetry dictionary (supporting canonical or Bambu field names)."""
        ts = timestamp
        if ts is None and "timestamp" in raw:
            raw_ts = raw["timestamp"]
            if isinstance(raw_ts, datetime):
                ts = raw_ts
            elif isinstance(raw_ts, str):
                try:
                    ts = datetime.fromisoformat(raw_ts)
                except Exception:
                    pass
        if ts is None and isinstance(raw.get("print"), dict) and "timestamp" in raw["print"]:
            raw_ts = raw["print"]["timestamp"]
            if isinstance(raw_ts, datetime):
                ts = raw_ts
            elif isinstance(raw_ts, str):
                try:
                    ts = datetime.fromisoformat(raw_ts)
                except Exception:
                    pass
        if ts is None:
            ts = utc_now()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        
        # Extract from nested 'print' object if present (common in Bambu MQTT reports)
        payload = raw.get("print", raw)

        filament_runout, filament_runout_details = _detect_filament_runout(payload)
        
        gcode_state = payload.get("gcode_state")
        derived_state = None
        if "state" in payload and payload["state"] is not None:
            if isinstance(payload["state"], PrinterState):
                derived_state = payload["state"]
            else:
                try:
                    derived_state = PrinterState(str(payload["state"]).lower())
                except ValueError:
                    derived_state = map_gcode_state_to_printer_state(str(payload["state"]))
        elif gcode_state:
            derived_state = map_gcode_state_to_printer_state(gcode_state)

        # Progress / layers
        progress = payload.get("progress")
        if progress is None and "mc_percent" in payload:
            progress = int(payload["mc_percent"])

        layer = payload.get("layer")
        if layer is None and "layer_num" in payload:
            layer = int(payload["layer_num"])

        total_layers = payload.get("total_layers")
        if total_layers is None and "total_layer_num" in payload:
            total_layers = int(payload["total_layer_num"])

        # Remaining seconds
        remaining_seconds = payload.get("remaining_seconds")
        if remaining_seconds is None and "mc_remaining_time" in payload:
            # Bambu reports mc_remaining_time in minutes; convert to seconds if integer
            val = payload["mc_remaining_time"]
            if val is not None:
                # If value is already large (>1000), it might already be seconds
                remaining_seconds = int(val) * 60 if int(val) < 10000 else int(val)

        # Temperatures
        nozzle_temp = payload.get("nozzle_temperature")
        if nozzle_temp is None and "nozzle_temper" in payload:
            nozzle_temp = float(payload["nozzle_temper"])

        nozzle_target = payload.get("nozzle_target_temperature")
        if nozzle_target is None and "nozzle_target_temper" in payload:
            nozzle_target = float(payload["nozzle_target_temper"])

        bed_temp = payload.get("bed_temperature")
        if bed_temp is None and "bed_temper" in payload:
            bed_temp = float(payload["bed_temper"])

        bed_target = payload.get("bed_target_temperature")
        if bed_target is None and "bed_target_temper" in payload:
            bed_target = float(payload["bed_target_temper"])

        chamber_temp = payload.get("chamber_temperature")
        if chamber_temp is None and "chamber_temper" in payload:
            chamber_temp = float(payload["chamber_temper"])

        # Subtask / filename
        subtask_name = payload.get("subtask_name")
        if subtask_name is None and "gcode_file" in payload:
            subtask_name = str(payload["gcode_file"])
        task_id = str(payload["task_id"]) if payload.get("task_id") is not None else None
        subtask_id = str(payload["subtask_id"]) if payload.get("subtask_id") is not None else None

        # Error codes
        error_code = payload.get("error_code")
        if error_code is None and "print_error" in payload:
            error_code = int(payload["print_error"])

        raw_online = payload.get("online")
        if isinstance(raw_online, bool):
            online = raw_online
        elif isinstance(raw_online, dict):
            # Bambu submodule status dictionary: presence proves printer is online
            online = True
        else:
            online = None

        return cls(
            printer_id=printer_id,
            timestamp=ts,
            online=online,
            state=derived_state,
            gcode_state=gcode_state,
            progress=progress,
            layer=layer,
            total_layers=total_layers,
            remaining_seconds=remaining_seconds,
            nozzle_temperature=nozzle_temp,
            nozzle_target_temperature=nozzle_target,
            bed_temperature=bed_temp,
            bed_target_temperature=bed_target,
            chamber_temperature=chamber_temp,
            subtask_name=subtask_name,
            task_id=task_id,
            subtask_id=subtask_id,
            error_code=error_code,
            filament_runout=filament_runout,
            filament_runout_details=filament_runout_details,
        )

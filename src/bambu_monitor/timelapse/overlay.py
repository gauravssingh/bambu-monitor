"""Visual Telemetry HUD overlay burner for timelapse frames.

Renders high-contrast, semi-transparent HUD status bars directly onto frame images
showing real-time hotend/bed temperatures, progress, layers, speeds, and anomaly alerts.
"""

from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict
from PIL import Image, ImageDraw, ImageFont


@lru_cache(maxsize=32)
def _load_scaled_font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    """Load a scalable TrueType font with cross-platform fallback hierarchy.

    Cached by size: rendering thousands of frames must not re-scan font
    candidates and re-open TTF files per frame.
    """
    font_candidates = [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/SFNSText.ttf",
        "/System/Library/Fonts/SFNS.ttf",
        "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for candidate in font_candidates:
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size=size)
            except Exception:
                continue
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


class TelemetryOverlayBurner:
    """Burns telemetry HUD banners onto timelapse frames."""

    @staticmethod
    def burn_hud(
        image: Image.Image,
        telemetry: Dict[str, Any],
        printer_label: str = "Bambu Lab",
        show_temperatures: bool = True,
        show_progress: bool = True,
        show_alerts: bool = True,
    ) -> Image.Image:
        """Draw a sleek, translucent telemetry HUD bar on top of the image."""
        img = image.convert("RGBA")
        width, height = img.size

        # Create overlay layer for alpha blending
        overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        # Scale proportions according to image width
        scale = max(0.6, width / 1280.0)
        hud_height = int(50 * scale)
        font_size = max(11, int(15 * scale))
        small_font_size = max(9, int(12 * scale))

        font = _load_scaled_font(font_size)
        small_font = _load_scaled_font(small_font_size)

        # 1. Background translucent banner (top)
        draw.rectangle([0, 0, width, hud_height], fill=(15, 23, 42, 220))
        # Divider line
        draw.line([0, hud_height, width, hud_height], fill=(51, 65, 85, 255), width=int(1.5 * scale))

        # Extract telemetry fields
        layer = telemetry.get("layer", 0)
        total_layers = telemetry.get("total_layers", 0)
        progress = telemetry.get("progress", 0.0)
        nozzle_t = telemetry.get("nozzle_temp")
        nozzle_tar = telemetry.get("nozzle_target")
        bed_t = telemetry.get("bed_temp")
        bed_tar = telemetry.get("bed_target")
        speed = telemetry.get("speed_percent") or telemetry.get("speed_magnitude")
        anomalies = telemetry.get("anomalies") or telemetry.get("anomaly_ids")

        # 2. Left side: Printer brand & layer/progress
        left_text = f"{printer_label} | Layer {layer}"
        if total_layers:
            left_text += f"/{total_layers}"
        if show_progress and progress is not None:
            left_text += f" ({progress:.0f}%)"

        y_pos = int(14 * scale)
        draw.text((int(15 * scale), y_pos), left_text, fill=(248, 250, 252, 255), font=font)

        # 3. Right side: Hotend & Bed Temperatures
        temp_parts = []
        is_temp_drop = False

        if show_temperatures:
            if nozzle_t is not None:
                noz_str = f"Hotend: {nozzle_t:.1f}°C"
                if nozzle_tar:
                    noz_str += f"/{nozzle_tar:.0f}°C"
                    if nozzle_tar >= 100.0 and (nozzle_tar - nozzle_t) >= 10.0:
                        is_temp_drop = True
                temp_parts.append(noz_str)

            if bed_t is not None:
                b_str = f"Bed: {bed_t:.1f}°C"
                if bed_tar:
                    b_str += f"/{bed_tar:.0f}°C"
                temp_parts.append(b_str)

            if speed:
                temp_parts.append(f"Speed: {speed}%")

        if temp_parts:
            right_text = "  •  ".join(temp_parts)
            # Calculate text width approximately if getbbox available
            try:
                bbox = draw.textbbox((0, 0), right_text, font=font)
                text_w = bbox[2] - bbox[0]
            except Exception:
                text_w = len(right_text) * int(8 * scale)

            right_x = max(int(width * 0.45), width - text_w - int(15 * scale))
            temp_color = (248, 113, 113, 255) if is_temp_drop else (56, 189, 248, 255)
            draw.text((right_x, y_pos), right_text, fill=temp_color, font=font)

        # 4. Progress bar line at the bottom of the HUD banner
        if show_progress and progress is not None and progress > 0:
            prog_ratio = min(1.0, max(0.0, float(progress) / 100.0))
            bar_w = int(width * prog_ratio)
            draw.line([0, hud_height - 2, bar_w, hud_height - 2], fill=(37, 99, 235, 255), width=int(3 * scale))

        # 5. Anomaly Alert Banner (if thermal drop or alert active)
        if show_alerts and (is_temp_drop or anomalies):
            alert_h = int(24 * scale)
            alert_y = hud_height + int(4 * scale)
            alert_text = "[!] THERMAL DROP DETECTED" if is_temp_drop else "[!] PRINTER ANOMALY DETECTED"
            try:
                abbox = draw.textbbox((0, 0), alert_text, font=small_font)
                aw = abbox[2] - abbox[0] + int(16 * scale)
            except Exception:
                aw = len(alert_text) * int(8 * scale) + int(16 * scale)

            draw.rectangle([int(15 * scale), alert_y, int(15 * scale) + aw, alert_y + alert_h], fill=(239, 68, 68, 220))
            draw.text((int(22 * scale), alert_y + int(4 * scale)), alert_text, fill=(255, 255, 255, 255), font=small_font)

        # Composite and return RGB
        combined = Image.alpha_composite(img, overlay)
        return combined.convert("RGB")

    @classmethod
    def burn_hud_to_bytes(
        cls,
        image_bytes: bytes,
        telemetry: Dict[str, Any],
        printer_label: str = "Bambu Lab",
        show_temperatures: bool = True,
        show_progress: bool = True,
        show_alerts: bool = True,
    ) -> bytes:
        """Burn HUD onto JPEG bytes and return new JPEG bytes."""
        with Image.open(io.BytesIO(image_bytes)) as img:
            overlaid = cls.burn_hud(
                img,
                telemetry,
                printer_label=printer_label,
                show_temperatures=show_temperatures,
                show_progress=show_progress,
                show_alerts=show_alerts,
            )
            buf = io.BytesIO()
            overlaid.save(buf, format="JPEG", quality=92)
            return buf.getvalue()

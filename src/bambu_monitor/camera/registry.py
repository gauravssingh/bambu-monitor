"""In-memory registry for printer CameraClient instances."""

from __future__ import annotations

from typing import Dict, Optional

from bambu_monitor.camera.client import CameraClient


class CameraRegistry:
    """Registry managing active CameraClient instances keyed by printer ID."""

    def __init__(self) -> None:
        self._cameras: Dict[str, CameraClient] = {}

    def register(self, printer_id: str, client: CameraClient) -> None:
        self._cameras[printer_id] = client

    def get(self, printer_id: str) -> Optional[CameraClient]:
        return self._cameras.get(printer_id)

    def remove(self, printer_id: str) -> Optional[CameraClient]:
        return self._cameras.pop(printer_id, None)

    def list_all(self) -> Dict[str, CameraClient]:
        return dict(self._cameras)

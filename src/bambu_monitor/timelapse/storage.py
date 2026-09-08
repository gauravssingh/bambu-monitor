"""Filesystem storage manager for frames, videos, and manifests."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from bambu_monitor.timelapse.models import TimelapseManifest, TimelapseSession

logger = logging.getLogger(__name__)


class TimelapseStorage:
    """Manages directory hierarchy, frame persistence, and atomic manifest/video operations."""

    def __init__(self, base_dir: str | Path = "./data/timelapses") -> None:
        self.base_dir = Path(base_dir).resolve()

    def resolve_session_dir(
        self,
        printer_id: str,
        session_id: str,
        started_at: datetime,
    ) -> Path:
        """Construct canonical directory: <base_dir>/<printer_id>/YYYY/MM/DD/<session_id>/."""
        year = started_at.strftime("%Y")
        month = started_at.strftime("%m")
        day = started_at.strftime("%d")
        session_dir = self.base_dir / printer_id / year / month / day / session_id
        return session_dir

    def ensure_session_dirs(self, session_dir: Path) -> tuple[Path, Path]:
        """Create session root and frames subfolder if they do not exist."""
        frames_dir = session_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)
        return session_dir, frames_dir

    def save_frame(self, session_dir: Path, sequence: int, frame_bytes: bytes) -> Path:
        """Atomically persist an individual frame as 000001.jpg, 000002.jpg, etc."""
        frames_dir = session_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)

        target_file = frames_dir / f"{sequence:06d}.jpg"
        tmp_file = frames_dir / f".tmp_{sequence:06d}_{os.getpid()}.jpg"

        tmp_file.write_bytes(frame_bytes)
        tmp_file.replace(target_file)
        return target_file

    def get_frame_path(self, session_dir: Path, sequence: int) -> Optional[Path]:
        """Retrieve path for a specific sequence frame if it exists."""
        target_file = session_dir / "frames" / f"{sequence:06d}.jpg"
        return target_file if target_file.is_file() else None

    def list_frames(self, session_dir: Path) -> List[Path]:
        """List all valid sequential frame files ordered by sequence."""
        frames_dir = session_dir / "frames"
        if not frames_dir.is_dir():
            return []
        frames = sorted(
            [f for f in frames_dir.iterdir() if f.is_file() and f.name.endswith(".jpg") and not f.name.startswith(".")],
            key=lambda p: p.name,
        )
        return frames

    def save_manifest(self, session: TimelapseSession, session_dir: Optional[Path] = None) -> Path:
        """Atomically persist manifest.json without risk of corruption on crash."""
        target_dir = session_dir or Path(session.storage_dir)
        target_dir.mkdir(parents=True, exist_ok=True)

        manifest = session.to_manifest()
        manifest_file = target_dir / "manifest.json"
        tmp_file = target_dir / f"manifest.json.tmp.{os.getpid()}"

        content = json.dumps(manifest.model_dump(), indent=2)
        with open(tmp_file, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())

        tmp_file.replace(manifest_file)
        return manifest_file

    def load_manifest(self, session_dir: Path | str) -> Optional[TimelapseManifest]:
        """Load manifest.json from a session directory if present."""
        manifest_path = Path(session_dir) / "manifest.json"
        if not manifest_path.is_file():
            return None
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            return TimelapseManifest.model_validate(data)
        except Exception as exc:
            logger.warning("Failed to parse manifest at %s: %s", manifest_path, exc)
            return None

    def get_video_path(self, session_dir: Path | str) -> Path:
        """Return the target path for timelapse.mp4 in the session directory."""
        return Path(session_dir) / "timelapse.mp4"

    def cleanup_successful_frames(self, session_dir: Path | str) -> int:
        """Delete image frames after successful video encoding, keeping manifest and mp4."""
        frames_dir = Path(session_dir) / "frames"
        deleted = 0
        if frames_dir.is_dir():
            for frame in frames_dir.iterdir():
                if frame.is_file() and frame.suffix == ".jpg":
                    try:
                        frame.unlink()
                        deleted += 1
                    except OSError as e:
                        logger.debug("Could not remove frame %s: %s", frame, e)
        return deleted

    def get_test_dir(self) -> Path:
        """Return directory for camera diagnostic snapshots."""
        test_dir = self.base_dir / "test"
        test_dir.mkdir(parents=True, exist_ok=True)
        return test_dir

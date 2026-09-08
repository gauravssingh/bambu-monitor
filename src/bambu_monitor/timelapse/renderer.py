"""TimelapseRenderer using FFmpeg to encode image sequences into MP4 video."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import List, Optional

from bambu_monitor.timelapse.models import TimelapseSession, TimelapseStatus, utc_now
from bambu_monitor.timelapse.storage import TimelapseStorage

logger = logging.getLogger(__name__)


class TimelapseRenderError(Exception):
    """Raised when video rendering fails or yields invalid output."""
    pass


class TimelapseRenderer:
    """Encapsulates FFmpeg subprocess execution for atomic timelapse video compilation."""

    def __init__(
        self,
        ffmpeg_bin: str = "ffmpeg",
        ffprobe_bin: str = "ffprobe",
        min_frames: int = 1,
    ) -> None:
        self.ffmpeg_bin = ffmpeg_bin
        self.ffprobe_bin = ffprobe_bin
        self.min_frames = min_frames

    async def render(
        self,
        session: TimelapseSession,
        storage: TimelapseStorage,
        fps: Optional[int] = None,
        codec: str = "libx264",
        quality: int = 18,
        pixel_format: str = "yuv420p",
    ) -> Path:
        """Compile session frames into an MP4 video with atomic finalization.
        
        Guarantees:
        1. Validates frame availability (minimum frame count).
        2. Validates frame sequence and fills or reindexes gaps if necessary.
        3. Encodes video into a temporary file first.
        4. Validates output file existence and non-zero size.
        5. Atomically finalizes the output (replaces timelapse.mp4).
        6. Cleans up partial/corrupted files if encoding fails.
        7. Updates session status and video path.
        """
        session_dir = Path(session.storage_dir)
        frames_dir = session_dir / "frames"
        final_video_path = storage.get_video_path(session_dir)
        tmp_video_path = session_dir / f".timelapse_{os.getpid()}.tmp.mp4"

        session.transition_to(TimelapseStatus.FINALIZING)
        storage.save_manifest(session, session_dir)

        # 1. Validate frame availability
        frames = storage.list_frames(session_dir)
        if len(frames) < self.min_frames:
            err_msg = (
                f"Insufficient frames to render timelapse for session {session.id}: "
                f"found {len(frames)}, minimum is {self.min_frames}"
            )
            logger.warning(err_msg)
            session.transition_to(TimelapseStatus.FAILED, error=err_msg)
            storage.save_manifest(session, session_dir)
            raise TimelapseRenderError(err_msg)

        # 2. Ensure frame sequence is valid (handle missed frame gaps)
        effective_fps = fps or session.video_fps or 30
        temp_seq_dir: Optional[Path] = None

        try:
            input_pattern, start_number = self._prepare_frame_sequence(frames, session_dir)
            if not input_pattern.parent.samefile(frames_dir):
                temp_seq_dir = input_pattern.parent

            cmd = [
                self.ffmpeg_bin,
                "-y",
                "-hide_banner",
                "-loglevel", "error",
                "-framerate", str(effective_fps),
                "-start_number", str(start_number),
                "-i", str(input_pattern),
                "-c:v", codec,
                "-crf", str(quality),
                "-pix_fmt", pixel_format,
                "-movflags", "+faststart",
                str(tmp_video_path),
            ]

            logger.info(
                "Rendering timelapse for session %s (%d frames at %d fps) -> %s",
                session.id,
                len(frames),
                effective_fps,
                final_video_path,
            )

            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError:
                raise TimelapseRenderError(f"FFmpeg binary not found at '{self.ffmpeg_bin}'")
            except Exception as spawn_exc:
                raise TimelapseRenderError(f"Failed to spawn FFmpeg process: {spawn_exc}")

            _, stderr = await proc.communicate()

            if proc.returncode != 0:
                err_text = stderr.decode(errors="replace").strip() if stderr else f"Exit code {proc.returncode}"
                raise TimelapseRenderError(f"FFmpeg rendering failed: {err_text}")

            # 4. Validate output video
            if not tmp_video_path.is_file() or tmp_video_path.stat().st_size == 0:
                raise TimelapseRenderError("FFmpeg produced an empty or missing output video file")

            # 5. Atomically finalize output
            tmp_video_path.replace(final_video_path)
            logger.info(
                "Timelapse video generated successfully for session %s (size: %.2f MB)",
                session.id,
                final_video_path.stat().st_size / (1024 * 1024),
            )

            # 6. Update session status
            session.video_path = str(final_video_path.resolve())
            session.transition_to(TimelapseStatus.COMPLETED)
            storage.save_manifest(session, session_dir)
            return final_video_path

        except Exception as exc:
            # Clean up temporary video if partially written
            if tmp_video_path.exists():
                try:
                    tmp_video_path.unlink()
                except OSError:
                    pass

            err_msg = str(exc)
            logger.error("Timelapse video generation failed for session %s: %s", session.id, err_msg)
            session.transition_to(TimelapseStatus.FAILED, error=err_msg)
            storage.save_manifest(session, session_dir)
            raise TimelapseRenderError(err_msg) from exc

        finally:
            # Clean up temp sequence symlinks directory if created
            if temp_seq_dir and temp_seq_dir.is_dir():
                try:
                    shutil.rmtree(temp_seq_dir, ignore_errors=True)
                except Exception:
                    pass

    def _prepare_frame_sequence(self, frames: List[Path], session_dir: Path) -> tuple[Path, int]:
        """Verify sequential numbering. If gaps exist due to missed frames, create temporary symlinks."""
        first_num = int(frames[0].stem)
        last_num = int(frames[-1].stem)
        expected_count = last_num - first_num + 1

        # Perfectly contiguous sequence
        if expected_count == len(frames):
            pattern = frames[0].parent / "%06d.jpg"
            return pattern, first_num

        # Gaps detected: re-index with symlinks to ensure FFmpeg parses all frames without stopping
        temp_dir = session_dir / f".seq_{os.getpid()}"
        temp_dir.mkdir(parents=True, exist_ok=True)
        for idx, frame in enumerate(frames, start=1):
            link_path = temp_dir / f"{idx:06d}.jpg"
            try:
                os.symlink(frame.resolve(), link_path)
            except OSError:
                # Fallback to copy if symlinks not permitted
                shutil.copy2(frame, link_path)

        return temp_dir / "%06d.jpg", 1

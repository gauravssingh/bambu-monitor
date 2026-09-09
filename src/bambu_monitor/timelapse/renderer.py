"""TimelapseRenderer using FFmpeg to encode image sequences into MP4 video."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from bambu_monitor.timelapse.models import TimelapseSession, TimelapseStatus
from bambu_monitor.timelapse.storage import TimelapseStorage

logger = logging.getLogger(__name__)

# Generous ceiling: a long print at low fps still renders in minutes, but a
# hung ffmpeg must not hold the render semaphore (max_concurrent=1) forever.
FFMPEG_RENDER_TIMEOUT_SECONDS = 3600.0


async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    """Kill a subprocess and reap it so no zombie remains."""
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        await proc.wait()
    except Exception:
        pass


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

    async def validate_video(self, video_path: Path) -> Dict[str, Any]:
        """Validate playable video container, stream existence, resolution, and duration via ffprobe."""
        if not video_path.is_file() or video_path.stat().st_size == 0:
            raise TimelapseRenderError("Output video file is missing or empty")

        probe_cmd = [
            self.ffprobe_bin,
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height,r_frame_rate,nb_frames:format=duration,size",
            "-of", "json",
            str(video_path),
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *probe_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.warning("ffprobe binary not found at '%s'; container probe skipped", self.ffprobe_bin)
            return {"verified": True, "size_bytes": video_path.stat().st_size}
        except Exception as exc:
            raise TimelapseRenderError(f"Failed to execute ffprobe validation: {exc}")

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60.0)
        except asyncio.TimeoutError:
            await _terminate_process(proc)
            raise TimelapseRenderError("ffprobe validation timed out after 60s")
        except asyncio.CancelledError:
            await _terminate_process(proc)
            raise

        if proc.returncode != 0:
            err_msg = stderr.decode(errors="replace").strip() if stderr else f"Exit code {proc.returncode}"
            raise TimelapseRenderError(f"Video validation failed (ffprobe error): {err_msg}")

        try:
            data = json.loads(stdout.decode(errors="replace"))
        except Exception:
            raise TimelapseRenderError("Video validation failed: ffprobe returned invalid JSON")

        streams = data.get("streams", [])
        if not streams:
            raise TimelapseRenderError("Video validation failed: no video stream found in rendered file")

        s = streams[0]
        codec = s.get("codec_name")
        width = s.get("width")
        height = s.get("height")
        format_info = data.get("format", {})
        duration_str = format_info.get("duration")

        try:
            duration = float(duration_str) if duration_str is not None else 0.0
        except ValueError:
            duration = 0.0

        if duration <= 0:
            raise TimelapseRenderError(f"Video validation failed: invalid duration ({duration}s)")

        return {
            "verified": True,
            "codec": codec,
            "resolution": f"{width}x{height}" if width and height else None,
            "duration": duration,
            "size_bytes": video_path.stat().st_size,
        }

    async def render(
        self,
        session: TimelapseSession,
        storage: TimelapseStorage,
        fps: Optional[int] = None,
        codec: str = "libx264",
        quality: int = 18,
        pixel_format: str = "yuv420p",
        burn_overlay: bool = False,
    ) -> Path:
        """Compile session frames into an MP4 video with atomic finalization.

        Guarantees:
        1. Validates frame availability (minimum frame count).
        2. Validates frame sequence and fills or reindexes gaps if necessary.
        3. Encodes video into a temporary file first.
        4. Validates output file existence, playable streams, and non-zero duration via ffprobe.
        5. Atomically finalizes the output (replaces timelapse.mp4).
        6. Cleans up partial/corrupted files if encoding fails.
        7. Updates session status and video path.
        """
        session_dir = Path(session.storage_dir)
        frames_dir = session_dir / "frames"
        final_video_path = storage.get_video_path(session_dir)
        tmp_video_path = session_dir / f".timelapse_{os.getpid()}_{uuid.uuid4().hex[:8]}.tmp.mp4"

        session.transition_to(TimelapseStatus.FINALIZING)
        await asyncio.to_thread(storage.save_manifest, session, session_dir)

        # 1. Validate frame availability
        frames = await asyncio.to_thread(storage.list_frames, session_dir)
        if len(frames) < self.min_frames:
            err_msg = (
                f"Insufficient frames to render timelapse for session {session.id}: "
                f"found {len(frames)}, minimum is {self.min_frames}"
            )
            logger.warning(err_msg)
            session.transition_to(TimelapseStatus.FAILED, error=err_msg)
            await asyncio.to_thread(storage.save_manifest, session, session_dir)
            raise TimelapseRenderError(err_msg)

        # 2. Ensure frame sequence is valid (handle missed frame gaps and optional HUD overlay)
        effective_fps = fps or session.video_fps or 30
        temp_seq_dir: Optional[Path] = None

        try:
            if burn_overlay:
                # The overlay pass is synchronous PIL work over every frame;
                # run it in a worker thread so the event loop (API, MQTT,
                # SSE) keeps serving during multi-minute renders.
                input_pattern, start_number, temp_seq_dir = await asyncio.to_thread(
                    self._prepare_overlay_frame_sequence,
                    frames, session_dir, storage, session,
                )
            else:
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
                "Rendering timelapse for session %s (%d frames at %d fps, burn_overlay: %s) -> %s",
                session.id,
                len(frames),
                effective_fps,
                burn_overlay,
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

            try:
                _, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=FFMPEG_RENDER_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                await _terminate_process(proc)
                raise TimelapseRenderError(
                    f"FFmpeg rendering timed out after {int(FFMPEG_RENDER_TIMEOUT_SECONDS)}s"
                )
            except asyncio.CancelledError:
                # Shutdown/cancellation must not leave an orphaned ffmpeg or a
                # half-written tmp video behind.
                await _terminate_process(proc)
                raise

            if proc.returncode != 0:
                err_text = stderr.decode(errors="replace").strip() if stderr else f"Exit code {proc.returncode}"
                raise TimelapseRenderError(f"FFmpeg rendering failed: {err_text}")

            # 4. Thoroughly validate output video using ffprobe
            probe_result = await self.validate_video(tmp_video_path)
            logger.debug(
                "Video stream validated for session %s: %s %s, duration: %.2fs",
                session.id,
                probe_result.get("codec"),
                probe_result.get("resolution"),
                probe_result.get("duration", 0.0),
            )

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
            await asyncio.to_thread(storage.save_manifest, session, session_dir)
            return final_video_path

        except asyncio.CancelledError:
            # CancelledError is a BaseException: clean up before propagating.
            if tmp_video_path.exists():
                try:
                    tmp_video_path.unlink()
                except OSError:
                    pass
            if temp_seq_dir and temp_seq_dir.is_dir():
                shutil.rmtree(temp_seq_dir, ignore_errors=True)
            raise

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
            await asyncio.to_thread(storage.save_manifest, session, session_dir)
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
        temp_dir = session_dir / f".seq_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        temp_dir.mkdir(parents=True, exist_ok=True)
        for idx, frame in enumerate(frames, start=1):
            link_path = temp_dir / f"{idx:06d}.jpg"
            try:
                os.symlink(frame.resolve(), link_path)
            except OSError:
                # Fallback to copy if symlinks not permitted
                shutil.copy2(frame, link_path)

        return temp_dir / "%06d.jpg", 1

    def _prepare_overlay_frame_sequence(
        self,
        frames: List[Path],
        session_dir: Path,
        storage: TimelapseStorage,
        session: TimelapseSession,
    ) -> tuple[Path, int, Path]:
        """Burn telemetry HUD overlay onto temporary frame copies for FFmpeg compilation.

        Pure CPU + disk (PIL decode/draw/encode per frame); always invoked via
        asyncio.to_thread from render() so the event loop stays responsive for
        the duration of a multi-thousand-frame pass.
        """
        from bambu_monitor.timelapse.overlay import TelemetryOverlayBurner

        temp_dir = session_dir / f".seq_overlay_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        temp_dir.mkdir(parents=True, exist_ok=True)

        metadata_list = storage.read_frames_metadata(session_dir)
        meta_by_frame = {m["frame"]: m for m in metadata_list if isinstance(m, dict) and "frame" in m}

        for idx, frame in enumerate(frames, start=1):
            frame_num = int(frame.stem)
            telem = meta_by_frame.get(frame_num, {})
            img_bytes = frame.read_bytes()
            try:
                overlaid_bytes = TelemetryOverlayBurner.burn_hud_to_bytes(
                    img_bytes,
                    telemetry=telem,
                    printer_label=session.printer_id or "Bambu Lab",
                )
            except Exception as burn_exc:
                logger.debug("HUD burn failed for frame %d, using raw frame: %s", frame_num, burn_exc)
                overlaid_bytes = img_bytes

            (temp_dir / f"{idx:06d}.jpg").write_bytes(overlaid_bytes)

        return temp_dir / "%06d.jpg", 1, temp_dir


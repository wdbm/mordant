"""
Mordant video trimming with FFmpeg

Copyright (C) 2026 William Breaden Madden
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
import selectors
import shutil
import subprocess
from threading import Event


class TrimCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class TrimSettings:
    start_us: int
    end_us: int
    reencode: bool = False
    include_audio: bool = True

    @property
    def duration_us(self) -> int:
        return self.end_us - self.start_us

    def validate(self, source_duration_us: int | None = None) -> None:
        if self.start_us < 0:
            raise ValueError("The trim start cannot be negative.")
        if self.end_us <= self.start_us:
            raise ValueError("The trim end must be later than the start.")
        if source_duration_us is not None and self.end_us > source_duration_us:
            raise ValueError("The trim end lies beyond the video duration.")


def parse_video_time(value: str) -> int:
    """Parse seconds, MM:SS, or HH:MM:SS as integer microseconds."""

    parts = value.strip().split(":")
    if not value.strip() or len(parts) > 3:
        raise ValueError("Enter a time as seconds, MM:SS, or HH:MM:SS.")
    try:
        numbers = [Decimal(part.strip()) for part in parts]
    except InvalidOperation as error:
        raise ValueError("Enter a numeric video time.") from error
    if any(not number.is_finite() or number < 0 for number in numbers):
        raise ValueError("Video times must be non-negative real numbers.")
    if len(numbers) > 1 and any(number != number.to_integral_value() for number in numbers[:-1]):
        raise ValueError("Only the seconds component may contain a decimal point.")
    if len(numbers) > 1 and numbers[-1] >= 60:
        raise ValueError("The seconds component must be less than 60.")
    if len(numbers) == 3 and numbers[-2] >= 60:
        raise ValueError("The minutes component must be less than 60.")
    seconds = sum(
        number * multiplier
        for number, multiplier in zip(reversed(numbers), (Decimal(1), Decimal(60), Decimal(3600)))
    )
    return int((seconds * 1_000_000).to_integral_value(rounding=ROUND_HALF_UP))


def format_video_time(value_us: int, milliseconds: bool = True) -> str:
    value_us = max(0, int(value_us))
    total_milliseconds = value_us // 1000
    total_seconds, fraction = divmod(total_milliseconds, 1000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    suffix = f".{fraction:03d}" if milliseconds else ""
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{suffix}"


def suggested_trim_name(
    path: str | Path,
    reencode: bool = False,
    now: datetime | None = None,
) -> str:
    path = Path(path)
    now = now or datetime.now(timezone.utc)
    timestamp = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    suffix = ".mp4" if reencode else path.suffix
    return f"{timestamp}_{path.stem}{suffix}"


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def _ffmpeg_seconds(value_us: int) -> str:
    return f"{value_us / 1_000_000:.6f}"


def build_ffmpeg_command(
    executable: str,
    source_path: str | Path,
    output_path: str | Path,
    settings: TrimSettings,
) -> list[str]:
    settings.validate()
    source_path = Path(source_path)
    output_path = Path(output_path)
    command = [
        executable,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-progress",
        "pipe:1",
        "-ss",
        _ffmpeg_seconds(settings.start_us),
        "-i",
        str(source_path),
        "-t",
        _ffmpeg_seconds(settings.duration_us),
        "-map",
        "0:v:0",
    ]
    if settings.include_audio:
        command.extend(("-map", "0:a?"))
    if settings.reencode:
        command.extend(("-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p"))
        if settings.include_audio:
            command.extend(("-c:a", "aac", "-b:a", "192k"))
        else:
            command.append("-an")
        command.extend(("-movflags", "+faststart"))
    else:
        command.extend(("-c", "copy"))
        if not settings.include_audio:
            command.append("-an")
    command.extend(("-y", str(output_path)))
    return command


def _stop_process(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def trim_video(
    source_path: str | Path,
    output_path: str | Path,
    settings: TrimSettings,
    cancel_event: Event | None = None,
    on_progress: Callable[[float], object] | None = None,
) -> Path:
    source_path = Path(source_path).resolve()
    output_path = Path(output_path).resolve()
    settings.validate()
    if not source_path.is_file():
        raise FileNotFoundError(f"Video is unavailable: {source_path}")
    if source_path == output_path:
        raise ValueError("The trimmed output cannot replace the source while it is being prepared.")
    executable = ffmpeg_path()
    if executable is None:
        raise RuntimeError("FFmpeg is required to trim videos but could not be found.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = build_ffmpeg_command(executable, source_path, output_path, settings)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    selector = selectors.DefaultSelector()
    diagnostics = []
    assert process.stdout is not None
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        while process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                _stop_process(process)
                raise TrimCancelled("Video trimming was cancelled.")
            for key, _events in selector.select(timeout=0.1):
                line = key.fileobj.readline()
                if not line.startswith("out_time_us="):
                    if line.strip() and "=" not in line:
                        diagnostics.append(line.strip())
                    continue
                try:
                    position_us = int(line.partition("=")[2])
                except ValueError:
                    continue
                if on_progress is not None:
                    on_progress(min(1.0, max(0.0, position_us / settings.duration_us)))
        remaining, _stderr = process.communicate()
        diagnostics.extend(
            line.strip()
            for line in remaining.splitlines()
            if line.strip() and "=" not in line
        )
        if process.returncode:
            reason = (
                diagnostics[-1]
                if diagnostics
                else f"FFmpeg exited with status {process.returncode}."
            )
            raise RuntimeError(f"FFmpeg could not trim the video: {reason}")
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise RuntimeError("FFmpeg completed without producing a trimmed video.")
        if on_progress is not None:
            on_progress(1.0)
        return output_path
    except Exception:
        if process.poll() is None:
            _stop_process(process)
        output_path.unlink(missing_ok=True)
        raise
    finally:
        selector.close()

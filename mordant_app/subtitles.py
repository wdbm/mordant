"""
Mordant external subtitle discovery and parsing

Copyright (C) 2026 William Breaden Madden
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from bisect import bisect_right
from html import unescape
from pathlib import Path
import re


SUBTITLE_EXTENSIONS = {".srt", ".vtt"}
SubtitleCue = tuple[int, int, str]


def parse_timestamp_to_us(value: str) -> int | None:
    """Accept SRT/VTT timestamps, including trailing WebVTT cue settings."""
    match = re.fullmatch(r"(?:(\d+):)?(\d{2}):(\d{2})[,.](\d{3})(?:\s+.*)?", value.strip())
    if match is None:
        return None
    hours, minutes, seconds, milliseconds = (int(part or 0) for part in match.groups())
    if minutes >= 60 or seconds >= 60:
        return None
    return ((hours * 3600 + minutes * 60 + seconds) * 1000 + milliseconds) * 1000


def parse_subtitles(content: str) -> list[SubtitleCue]:
    """
    Read ordinary SRT and WebVTT cues as sorted microsecond intervals.

    Labels use plain text, so remove formatting/timestamp tags rather than
    treating subtitle input as GTK markup. Ignore metadata and malformed cues.
    """
    content = content.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    entries = []
    for block in re.split(r"\n[ \t]*\n", content.strip()):
        lines = block.splitlines()
        if not lines or re.match(r"^(?:WEBVTT|NOTE|STYLE|REGION)(?:\s|$)", lines[0]):
            continue
        time_index = next((index for index, line in enumerate(lines[:2]) if "-->" in line), None)
        if time_index is None:
            continue
        start_text, end_text = lines[time_index].split("-->", 1)
        start, end = parse_timestamp_to_us(start_text), parse_timestamp_to_us(end_text)
        if start is None or end is None or end <= start:
            continue
        caption = unescape(re.sub(r"<[^>]*>", "", "\n".join(lines[time_index + 1:]))).strip()
        if caption:
            entries.append((start, end, caption))
    return sorted(entries, key=lambda cue: (cue[0], cue[1]))


def read_subtitle_entries(path: Path) -> list[SubtitleCue]:
    try:
        content = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        content = path.read_text(encoding="latin-1")
    return parse_subtitles(content)


def find_subtitle_candidates(video_path: Path) -> list[Path]:
    """Find exact and language-suffixed sidecars, treating names literally."""
    stem = video_path.stem.casefold()
    candidates = [
        path for path in video_path.parent.iterdir()
        if path.is_file() and path.suffix.casefold() in SUBTITLE_EXTENSIONS
        and (path.stem.casefold() == stem or path.stem.casefold().startswith(stem + "."))
    ]
    return sorted(candidates, key=lambda path: (path.stem.casefold() != stem, path.name.casefold()))


def subtitle_text_at(entries: list[SubtitleCue], starts: list[int], ends: list[int], position: int) -> str:
    """Return all overlapping cues; ends contains cumulative maximum end times."""
    index = bisect_right(starts, position) - 1
    captions = []
    while index >= 0 and ends[index] > position:
        start, end, text = entries[index]
        if start <= position < end:
            captions.append(text)
        index -= 1
    return "\n".join(reversed(captions))


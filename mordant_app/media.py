"""
Mordant mixed-media navigation, independent of the display toolkit

Copyright (C) 2026 William Breaden Madden
SPDX-License-Identifier: GPL-3.0-or-later
"""

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".tif", ".tiff"}
VIDEO_SUFFIXES = {
    ".mp4", ".mkv", ".webm", ".mov", ".avi", ".mpeg", ".mpg", ".ogv",
    ".ogg", ".m4v", ".wmv", ".flv", ".ts", ".mts", ".m2ts", ".3gp",
}
FILTERS = ("All media", "Images", "Videos")
PREFETCH_NEIGHBOUR_COUNT = 3


@dataclass(frozen=True)
class DirectoryIntersectionSource:
    """Directories whose common media filenames form a library source."""

    directories: tuple[Path, ...]

    @property
    def primary_directory(self) -> Path:
        return self.directories[0]


def media_kind(path: str | Path) -> str | None:
    suffix = Path(path).suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in VIDEO_SUFFIXES:
        return "video"
    return None


def image_prefetch_candidates(
    paths: Iterable[Path],
    current: Path,
    count: int = PREFETCH_NEIGHBOUR_COUNT,
) -> list[Path]:
    """
    Return nearby images to decode, without wrapping or including videos.

    Candidates alternate forwards and backwards, with the next image first.
    Videos do not consume the per-direction allowance. A video as the current
    item disables image preloading.
    """
    paths = list(paths)
    if count <= 0 or current not in paths or media_kind(current) != "image":
        return []
    index = paths.index(current)
    forwards = [path for path in paths[index + 1:] if media_kind(path) == "image"][:count]
    backwards = [path for path in reversed(paths[:index]) if media_kind(path) == "image"][:count]
    candidates = []
    for offset in range(max(len(forwards), len(backwards))):
        if offset < len(forwards):
            candidates.append(forwards[offset])
        if offset < len(backwards):
            candidates.append(backwards[offset])
    return candidates


def scan_directory(directory: str | Path) -> list[Path]:
    return sorted(
        (path.resolve() for path in Path(directory).iterdir()
         if path.is_file() and media_kind(path)),
        key=lambda path: (path.name.casefold(), path.name),
    )


def scan_directory_by_name(directory: str | Path) -> dict[str, Path]:
    """Return supported immediate media entries indexed by exact filename."""

    return {
        entry.name: entry.resolve()
        for entry in Path(directory).iterdir()
        if entry.is_file() and media_kind(entry)
    }


def _normalise_intersection_directories(
    directories: Iterable[str | Path],
) -> tuple[Path, ...]:
    distinct = []
    for directory in directories:
        path = Path(directory).expanduser().resolve()
        if not path.is_dir():
            raise NotADirectoryError(f"Not a directory: {path}")
        if path not in distinct:
            distinct.append(path)
    if len(distinct) < 2:
        raise ValueError("Choose at least two distinct directories.")
    return tuple(distinct)


def _intersect_normalised_directories(directories: tuple[Path, ...]) -> list[Path]:
    entries = [scan_directory_by_name(directory) for directory in directories]
    common_names = set(entries[0])
    for directory_entries in entries[1:]:
        common_names.intersection_update(directory_entries)
    return [
        path
        for name, path in sorted(
            entries[0].items(),
            key=lambda item: (item[0].casefold(), item[0]),
        )
        if name in common_names
    ]


def intersect_directories(directories: Iterable[str | Path]) -> list[Path]:
    """Return primary-directory paths whose filenames occur in every directory."""

    normalised = _normalise_intersection_directories(directories)
    return _intersect_normalised_directories(normalised)


class MediaLibrary:
    """One ordered file list; filtering never loses the unfiltered items"""

    def __init__(self):
        self.paths: list[Path] = []
        self.filter = "All media"
        self.current: Path | None = None
        self.directory: Path | None = None
        self.intersection_source: DirectoryIntersectionSource | None = None
        self.removed_positions: dict[Path, int] = {}

    @property
    def visible(self):
        kind = {"Images": "image", "Videos": "video"}.get(self.filter)
        return [path for path in self.paths if kind is None or media_kind(path) == kind]

    def open(self, inputs: Iterable[str | Path]) -> list[str]:
        inputs = [Path(path).expanduser().resolve() for path in inputs]
        if not inputs:
            inputs = [Path.cwd()]
        paths, errors, directory, preferred = [], [], None, None
        if len(inputs) == 1:
            candidate = inputs[0]
            if candidate.is_dir():
                directory = candidate
            elif candidate.is_file() and media_kind(candidate):
                directory, preferred = candidate.parent, candidate
        for path in ([directory] if directory is not None else inputs):
            try:
                if path.is_dir():
                    paths.extend(scan_directory(path))
                elif path.is_file() and media_kind(path):
                    paths.append(path)
                else:
                    errors.append(f"Unsupported or missing file: {path}")
            except OSError as error:
                errors.append(f"{path}: {error}")
        if errors and not paths:
            raise ValueError("\n".join(errors))
        self.paths = list(dict.fromkeys(paths))
        self.directory = directory
        self.intersection_source = None
        self.removed_positions.clear()
        self.filter = "All media"
        self.current = preferred if preferred in self.paths else next(iter(self.paths), None)
        return errors

    def open_intersection(self, directories: Iterable[str | Path]) -> None:
        """Replace the library with media filenames common to every directory."""

        normalised = _normalise_intersection_directories(directories)
        paths = _intersect_normalised_directories(normalised)
        self.paths = paths
        self.directory = None
        self.intersection_source = DirectoryIntersectionSource(normalised)
        self.removed_positions.clear()
        self.filter = "All media"
        self.current = next(iter(self.paths), None)

    def set_filter(self, value: str) -> None:
        if value not in FILTERS:
            raise ValueError(f"Unknown media filter: {value}")
        self.filter = value
        if self.current not in self.visible:
            self.current = next(iter(self.visible), None)

    def step(self, delta: int) -> Path | None:
        paths = self.visible
        if not paths:
            self.current = None
        else:
            index = paths.index(self.current) if self.current in paths else 0
            self.current = paths[(index + delta) % len(paths)]
        return self.current

    def remove(self, path: str | Path) -> None:
        path = Path(path).resolve()
        visible = self.visible
        index = visible.index(path) if path in visible else 0
        if path in self.paths:
            self.removed_positions[path] = self.paths.index(path)
            self.paths.remove(path)
        if self.current == path:
            remaining = self.visible
            self.current = remaining[index % len(remaining)] if remaining else None

    def restore(self, path: str | Path) -> None:
        path = Path(path).resolve()
        if path not in self.paths:
            self.paths.insert(self.removed_positions.pop(path, len(self.paths)), path)
        if path not in self.visible:
            self.filter = "All media"
        self.current = path

    def refresh(self) -> None:
        current = self.current
        if self.intersection_source is not None:
            paths = _intersect_normalised_directories(self.intersection_source.directories)
        elif self.directory is not None:
            paths = scan_directory(self.directory)
        else:
            paths = [path for path in self.paths if path.is_file()]
        self.paths = paths
        self.current = current if current in self.visible else next(iter(self.visible), None)

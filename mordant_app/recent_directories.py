"""
Mordant save destinations and session state

Copyright (C) 2026 William Breaden Madden
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from pathlib import Path

from .json_storage import read_json, write_json_atomic
from .transfers import TransferRequest, sanitise_filename


RECENT_DIRECTORY_KEYS = (1, 2, 3, 4, 5, 6, 7, 8, 9, 0)
RECENT_DIRECTORY_LISTING_MODE_ALPHABETICAL = "alphabetical"
RECENT_DIRECTORY_LISTING_MODE_ALPHABETICAL_PATH = RECENT_DIRECTORY_LISTING_MODE_ALPHABETICAL
RECENT_DIRECTORY_LISTING_MODE_ALPHABETICAL_NAME = "alphabetical_name"
RECENT_DIRECTORY_LISTING_MODE_MOST_RECENT = "most_recent"


class RecentDirectoryStore:
    def __init__(self, recents_file: str | Path, max_recents: int = 80):
        self.recents_file = Path(recents_file).expanduser()
        self.max_recents = max(1, int(max_recents))

    def load(self) -> list[str]:
        data = read_json(self.recents_file, [])
        if not isinstance(data, list):
            return []
        return [path for path in data if isinstance(path, str) and Path(path).is_dir()]

    def save(self, recents: list[str]) -> None:
        # Limit the ordinary listing, but retain older destinations for search.
        write_json_atomic(self.recents_file, recents)

    def clear(self) -> None:
        self.save([])

    def add(self, path: str | Path) -> list[str]:
        resolved_path = Path(path).expanduser().resolve()
        if resolved_path.is_file():
            resolved_path = resolved_path.parent
        path_str = str(resolved_path)
        recents = self.load()
        if path_str in recents:
            recents.remove(path_str)
        recents.insert(0, path_str)
        self.save(recents)
        return recents

    def remove(self, path: str | Path) -> None:
        target = str(Path(path).expanduser().resolve())
        self.save([item for item in self.load() if str(Path(item).resolve()) != target])

    def most_recent(self) -> Path | None:
        recents = self.load()
        return Path(recents[0]).resolve() if recents else None

    def all(self) -> list[Path]:
        return [Path(path).resolve() for path in self.load()[: self.max_recents]]

    def listed(self, listing_mode: str = RECENT_DIRECTORY_LISTING_MODE_MOST_RECENT,
               query: str = "") -> list[Path]:
        query = query.strip().casefold()
        recents = ([Path(path).resolve() for path in self.load()
                    if query in str(path).casefold()] if query else self.all())
        if listing_mode == RECENT_DIRECTORY_LISTING_MODE_ALPHABETICAL_PATH:
            return sorted(recents, key=lambda path: str(path).casefold())
        if listing_mode == RECENT_DIRECTORY_LISTING_MODE_ALPHABETICAL_NAME:
            return sorted(
                recents,
                key=lambda path: ((path.name or str(path)).casefold(), str(path).casefold()),
            )
        return recents

    def indexed(self, listing_mode: str = RECENT_DIRECTORY_LISTING_MODE_MOST_RECENT) -> list[tuple[int, Path]]:
        return list(zip(RECENT_DIRECTORY_KEYS, self.listed(listing_mode=listing_mode)[: len(RECENT_DIRECTORY_KEYS)]))

    def for_key(self, key: int, listing_mode: str = RECENT_DIRECTORY_LISTING_MODE_MOST_RECENT) -> Path | None:
        for display_key, path in self.indexed(listing_mode=listing_mode):
            if display_key == key:
                return path
        return None


class SaveSession:
    """Toolkit-independent state and operations for a save dialogue"""

    def __init__(
        self,
        recent_store: RecentDirectoryStore,
        last_selected=None,
        listing_mode: str = RECENT_DIRECTORY_LISTING_MODE_ALPHABETICAL_NAME,
        move_requested: bool = False,
        overwrite_requested: bool = False,
        open_parent_directory: bool = True,
    ):
        self.recent_store = recent_store
        self.selected_directories = {
            str(Path(path).expanduser().resolve()) for path in (last_selected or [])
        }
        self.listing_mode = RECENT_DIRECTORY_LISTING_MODE_ALPHABETICAL_NAME
        self.set_listing_mode(listing_mode)
        self.move_requested = bool(move_requested)
        self.overwrite_requested = bool(overwrite_requested)
        self.open_parent_directory = bool(open_parent_directory)

    def set_listing_mode(self, listing_mode: str) -> bool:
        if listing_mode not in {
            RECENT_DIRECTORY_LISTING_MODE_ALPHABETICAL_PATH,
            RECENT_DIRECTORY_LISTING_MODE_ALPHABETICAL_NAME,
            RECENT_DIRECTORY_LISTING_MODE_MOST_RECENT,
        }:
            return False
        changed = self.listing_mode != listing_mode
        self.listing_mode = listing_mode
        return changed

    def listed_directories(self, query: str = "") -> list[Path]:
        return self.recent_store.listed(self.listing_mode, query=query)

    def selected_paths(self) -> list[Path]:
        return [Path(path).resolve() for path in sorted(self.selected_directories)]

    def selection_summary(self, empty: str = "(none)") -> str:
        count = len(self.selected_directories)
        if count == 0:
            return empty
        if count == 1:
            return next(iter(self.selected_directories))
        return f"{count} directories"

    def is_selected(self, path: str | Path) -> bool:
        return str(Path(path).expanduser().resolve()) in self.selected_directories

    def select(self, path: str | Path) -> None:
        self.selected_directories.add(str(Path(path).expanduser().resolve()))

    def set_selected(self, paths) -> None:
        self.selected_directories = {
            str(Path(path).expanduser().resolve()) for path in paths
        }

    def toggle(self, path: str | Path) -> bool:
        path_string = str(Path(path).expanduser().resolve())
        if path_string in self.selected_directories:
            self.selected_directories.remove(path_string)
            return False
        self.selected_directories.add(path_string)
        return True

    def deselect_all(self) -> None:
        self.selected_directories.clear()

    def remove_recent(self, path: str | Path) -> None:
        self.recent_store.remove(path)
        self.selected_directories.discard(str(Path(path).expanduser().resolve()))

    def remember_directory(self, path: str | Path, select: bool = True) -> Path:
        directory = Path(path).expanduser().resolve()
        self.recent_store.add(directory)
        if select:
            self.select(directory)
        return directory

    def clear_recents(self) -> None:
        self.recent_store.clear()
        self.deselect_all()

    def browse_start_directory(self, fallback: str | Path | None = None) -> Path | None:
        selected = self.selected_paths()
        if selected:
            start = selected[0]
        elif fallback is not None:
            start = Path(fallback).expanduser().resolve()
        else:
            start = self.recent_store.most_recent()
        if start is not None and self.open_parent_directory and start.parent != start:
            start = start.parent
        return start

    def filename(self, typed_name: str, default_suffix: str = "") -> str:
        name = sanitise_filename(typed_name)
        if default_suffix and not Path(name).suffix:
            name = f"{name}{default_suffix}"
        return name

    def request(
        self,
        source_path: str | Path,
        typed_name: str,
        destinations=None,
        default_suffix: str = "",
        move_requested: bool | None = None,
        overwrite_requested: bool | None = None,
    ) -> TransferRequest:
        return TransferRequest(
            source_path=Path(source_path),
            destination_directories=(
                self.selected_paths()
                if destinations is None
                else [Path(path).expanduser().resolve() for path in destinations]
            ),
            name=self.filename(typed_name, default_suffix),
            move_requested=(self.move_requested if move_requested is None else bool(move_requested)),
            overwrite_requested=(
                self.overwrite_requested
                if overwrite_requested is None
                else bool(overwrite_requested)
            ),
        )

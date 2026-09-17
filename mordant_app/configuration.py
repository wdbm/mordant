"""
Mordant recent configuration directory persistence

Copyright (C) 2026 William Breaden Madden
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import os
from pathlib import Path

from .json_storage import read_json, write_json_atomic
from .recent_directories import RecentDirectoryStore, SaveSession
from .transfers import TransferManager


MAX_RECENT_CONFIGURATIONS = 5


def default_configuration_directory() -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME")
    base = Path(configured).expanduser() if configured else Path.home() / ".config"
    return base / "mordant"


def default_configuration_history_file() -> Path:
    return default_configuration_directory() / "recent_configuration_directories.json"


class Configuration:
    """Own profile setup and save state, retaining undo across profile switches"""

    def __init__(
        self,
        directory: str | Path | None = None,
        history_file: str | Path | None = None,
    ):
        self.history = ConfigurationHistory(history_file)
        self.directory = None
        self.store = None
        self.session = None
        self.manager = TransferManager()
        self.history_error = None
        target = (
            Path(directory).expanduser().resolve()
            if directory is not None
            else default_configuration_directory().resolve()
        )
        self.switch(target, require_history=False)

    def switch(self, directory: str | Path, *, require_history: bool = True) -> Path:
        target = Path(directory).expanduser().resolve()
        try:
            self.history.remember(target)
        except OSError as error:
            if require_history:
                raise
            self.history_error = f"Could not remember configuration directory: {error}"
        else:
            self.history_error = None
        if target != self.directory:
            store = RecentDirectoryStore(target / "recent_directories.json")
            session = SaveSession(store)
            self.store, self.session, self.directory = store, session, target
            self.manager.recent_store = store
        return target

    def close(self) -> None:
        self.manager.close()


class ConfigurationHistory:
    """Maintain a small, ordered list independently of any selected profile."""

    def __init__(
        self,
        path: str | Path | None = None,
        max_items: int = MAX_RECENT_CONFIGURATIONS,
    ):
        self.path = Path(path or default_configuration_history_file()).expanduser()
        self.max_items = max(1, int(max_items))

    def load(self) -> list[Path]:
        payload = read_json(self.path, [])
        values = payload.get("recent", []) if isinstance(payload, dict) else payload
        if not isinstance(values, list):
            return []
        result = []
        for value in values:
            try:
                directory = Path(value).expanduser().resolve()
            except (OSError, TypeError, ValueError):
                continue
            if directory.is_dir() and directory not in result:
                result.append(directory)
        return result[:self.max_items]

    def remember(self, directory: str | Path) -> list[Path]:
        directory = Path(directory).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        if not directory.is_dir():
            raise NotADirectoryError(directory)
        recent = [item for item in self.load() if item != directory]
        recent.insert(0, directory)
        recent = recent[:self.max_items]
        write_json_atomic(
            self.path,
            {"version": 1, "recent": [str(item) for item in recent]},
        )
        return recent

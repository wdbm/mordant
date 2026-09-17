"""
Mordant file transfers, rollback, and one-step undo

Copyright (C) 2026 William Breaden Madden
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import os
import shutil
import tempfile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .recent_directories import RecentDirectoryStore

Fingerprint = tuple[int, int, int, int, str]


@dataclass(frozen=True)
class TransferRequest:
    source_path: Path
    destination_directories: list[Path]
    name: str
    move_requested: bool = False
    overwrite_requested: bool = False


@dataclass(frozen=True)
class TransferResult:
    saved_directories: list[Path]
    errors: list[tuple[str, str]]
    warnings: list[str] = field(default_factory=list)


def format_transfer_status(
    result: TransferResult,
    name: str,
    move_requested: bool = False,
) -> str:
    action = "move" if move_requested else "save"
    action_past = "moved" if move_requested else "saved"
    if result.errors and result.saved_directories:
        return f"Partial {action}: {len(result.saved_directories)} ok, {len(result.errors)} failed"
    if result.errors:
        return f"{action.capitalize()} failed"
    if not result.saved_directories:
        return ""
    noun = "directory" if len(result.saved_directories) == 1 else "directories"
    summary = format_directory_summary(result.saved_directories)
    return f"{name} {action_past} to {noun} {summary}."


def sanitise_filename(name: str) -> str:
    name = name.strip().replace(os.sep, "")
    if os.altsep:
        name = name.replace(os.altsep, "")
    return "".join("_" if ("\x00" <= character <= "\x1f" or character in '<>:"|?*') else character for character in name) or "image"


def unique_destination_path(destination_directory: Path, filename: str) -> Path:
    base = Path(filename).stem
    ext = Path(filename).suffix
    candidate = destination_directory / (base + ext)
    counter = 1
    while candidate.exists():
        candidate = destination_directory / f"{base} (copy {counter}){ext}"
        counter += 1
    return candidate


def destination_path(destination_directory: Path, filename: str, overwrite: bool = False) -> Path:
    if overwrite:
        return destination_directory / filename
    return unique_destination_path(destination_directory, filename)


def format_directory_summary(directories: list[Path]) -> str:
    labels = [directory.name or str(directory) for directory in directories]
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} and {labels[1]}"
    return f"{labels[0]}, {labels[1]}, and {len(labels) - 2} others"


def _fingerprint(path: Path) -> Fingerprint | None:
    if path.is_symlink():
        raise OSError(f"Refusing to replace symbolic link: {path}")
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, digest.hexdigest())


def _restore_backup(backup: Path, destination: Path) -> None:
    # Write alongside the target and replace only after the whole backup is copied.
    fd, temporary = tempfile.mkstemp(prefix=".mordant-restore-", dir=destination.parent)
    os.close(fd)
    try:
        shutil.copy2(backup, temporary)
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


@dataclass
class _UndoEntry:
    path: Path
    backup_path: Path | None
    expected_fingerprint: Fingerprint | None


class _UndoRecord:
    def __init__(self):
        self.storage = tempfile.TemporaryDirectory(prefix="mordant-undo-")
        self.entries: list[_UndoEntry] = []
        self.moved_source = None

    def remember(self, path: Path) -> None:
        before = _fingerprint(path)
        backup = None
        if before is not None:
            backup = Path(self.storage.name) / str(len(self.entries))
            shutil.copy2(path, backup)
        self.entries.append(_UndoEntry(path, backup, before))

    def restore(self) -> None:
        # Completed entries are removed so a failed undo can be retried.
        while self.entries:
            entry = self.entries[-1]
            if entry.backup_path is None:
                entry.path.unlink(missing_ok=True)
            else:
                _restore_backup(entry.backup_path, entry.path)
            self.entries.pop()


class TransferManager:
    """Execute transfers and own one independent, process-local undo record."""

    def __init__(self, recent_store: RecentDirectoryStore | None = None):
        self.recent_store = recent_store
        self._last_transfer: _UndoRecord | None = None

    def close(self) -> None:
        if self._last_transfer is not None:
            self._last_transfer.storage.cleanup()
            self._last_transfer = None

    def undo_description(self) -> str:
        if self._last_transfer is None:
            return ""
        return "Undo the last transfer?\n\n" + "\n".join(
            f"{'Restore' if entry.backup_path else 'Remove'}: {entry.path}"
            for entry in reversed(self._last_transfer.entries)
        )

    def undo(self) -> Path | None:
        if self._last_transfer is None:
            return None
        for entry in self._last_transfer.entries:
            if _fingerprint(entry.path) != entry.expected_fingerprint:
                raise OSError(f"File changed since the transfer; undo cancelled: {entry.path}")
        moved_source = self._last_transfer.moved_source
        self._last_transfer.restore()
        self._last_transfer.storage.cleanup()
        self._last_transfer = None
        return moved_source

    def transfer(self, request: TransferRequest) -> TransferResult:
        source = Path(request.source_path).resolve()
        if not source.is_file():
            return TransferResult([], [(str(source), "Source file does not exist.")])
        if not request.name or Path(request.name).name != request.name or request.name in {".", ".."}:
            return TransferResult([], [(request.name, "Expected a filename, not a path.")])
        record = _UndoRecord()
        saved, errors = [], []
        directories = dict.fromkeys(Path(d).resolve() for d in request.destination_directories)
        for directory in directories:
            try:
                directory.mkdir(parents=True, exist_ok=True)
                destination = destination_path(directory, request.name, request.overwrite_requested)
                if destination.resolve() == source:
                    raise OSError("Source and destination are the same file.")
                record.remember(destination)
                try:
                    shutil.copy2(source, destination)
                except Exception:
                    entry = record.entries[-1]
                    if entry.backup_path is None:
                        entry.path.unlink(missing_ok=True)
                    else:
                        _restore_backup(entry.backup_path, entry.path)
                    record.entries.pop()
                    raise
                saved.append(directory)
            except Exception as error:
                errors.append((str(directory), str(error)))
                if request.move_requested:
                    break
        if request.move_requested and saved and not errors:
            try:
                record.remember(source)
                source.unlink()
                record.moved_source = source
            except Exception as error:
                errors.append((str(source), str(error)))
        if request.move_requested and errors:
            try:
                record.restore()
                saved = []
            except Exception as error:
                errors.append(("Rollback", str(error)))
        if record.entries:
            for entry in record.entries:
                entry.expected_fingerprint = _fingerprint(entry.path)
            self.close()
            self._last_transfer = record
        else:
            record.storage.cleanup()
        warnings = []
        if self.recent_store is not None:
            for directory in saved:
                try:
                    self.recent_store.add(directory)
                except OSError as error:
                    warnings.append(f"Recent-directory history could not be saved: {error}")
                    break
        return TransferResult(saved, errors, warnings)

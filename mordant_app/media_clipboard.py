"""
Mordant media file clipboard support

Copyright (C) 2026 William Breaden Madden
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio


def copy_file_to_clipboard(
    path: str | Path,
    clipboard: Gdk.Clipboard,
) -> tuple[Path, Gdk.ContentProvider]:
    """Place a local file reference on a GTK clipboard."""

    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Media file is unavailable: {path}")
    file_list = Gdk.FileList.new_from_list(
        [Gio.File.new_for_path(str(path))]
    )
    provider = Gdk.ContentProvider.new_for_value(file_list)
    if not clipboard.set_content(provider):
        raise RuntimeError("The clipboard did not accept the media file.")
    return path, provider

"""
Mordant JSON persistence helpers used by configuration state

Copyright (C) 2026 William Breaden Madden
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any


def read_json(path: str | Path, default: Any) -> Any:
    """Return decoded JSON, or the supplied default when it cannot be read."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return default


def write_json_atomic(path: str | Path, payload: Any) -> None:
    """Write formatted JSON beside its destination before replacing it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

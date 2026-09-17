"""
Check the system dependencies required by the Mordant installer.

Copyright (C) 2026 William Breaden Madden
SPDX-License-Identifier: GPL-3.0-or-later
"""

import importlib
from pathlib import Path
import sys
import sysconfig

errors = []
if sys.version_info < (3, 10):
    errors.append("Python 3.10 or newer is required")
for module, package in (
    ("venv", "python3-venv"), ("ensurepip", "python3-venv"),
    ("setuptools", "python3-setuptools"), ("wheel", "python3-wheel"),
    ("docopt", "python3-docopt"),
    ("PIL.Image", "python3-pil"), ("cairo", "python3-cairo"),
):
    try:
        importlib.import_module(module)
    except ImportError:
        errors.append(f"{module} is missing; install {package}")
try:
    from PIL import features
    if not features.check("webp"):
        errors.append("Pillow lacks WEBP support; please install python3-pil.")
except ImportError:
    pass
try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("Rsvg", "2.0")
    gi.require_foreign("cairo")
    from gi.repository import Gtk, Gdk, Rsvg
    if (Gtk.get_major_version(), Gtk.get_minor_version()) < (4, 8):
        errors.append("GTK 4.8 or newer is required")
except (ImportError, ValueError) as error:
    errors.append(f"GTK/SVG/Cairo bindings: {error}; install python3-gi, python3-gi-cairo, gir1.2-gtk-4.0 and gir1.2-rsvg-2.0")

multiarch = sysconfig.get_config_var("MULTIARCH") or ""
library_dirs = [Path("/usr/lib"), Path("/usr/lib64"), Path("/usr/lib") / multiarch]
if not any(list(directory.glob("gtk-4.0/*/media/libmedia-gstreamer.so")) for directory in library_dirs):
    errors.append("The GTK GStreamer media backend is missing; install libgtk-4-media-gstreamer")
if not any((directory / "gstreamer-1.0/libgstplayback.so").is_file() for directory in library_dirs):
    errors.append("GStreamer's playback plugin is missing; install gstreamer1.0-plugins-base")
if errors:
    print("Mordant dependency checks failed. No installation files were created:", file=sys.stderr)
    for error in errors:
        print(f"  - {error}", file=sys.stderr)
    print("Please see the installation section at mordant/README.md for the apt command.", file=sys.stderr)
    raise SystemExit(1)

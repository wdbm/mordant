"""
Render installed desktop and D-Bus files from Mordant templates.

Copyright (C) 2026 William Breaden Madden
SPDX-License-Identifier: GPL-3.0-or-later
"""

from pathlib import Path
import sys

assets, launcher, applications, services, app_id = sys.argv[1:]
# Desktop Exec uses the desktop-entry escaping rules, then argument quoting.
# D-Bus Exec uses shell-style argument parsing, without executing a shell.
desktop_path = launcher.replace("\\", "\\\\").replace("\t", "\\t")
desktop_exec = '"' + ''.join("\\" + char if char in '\\"`$' else char for char in launcher) + '"'
desktop_exec = desktop_exec.replace("\\", "\\\\").replace("%", "%%")
dbus_exec = '"' + launcher.replace("\\", "\\\\").replace('"', '\\"') + '"'
for suffix, destination, replacements in (
    ("desktop", Path(applications), {"@EXEC@": desktop_exec, "@TRYEXEC@": desktop_path}),
    ("service", Path(services), {"@EXEC@": dbus_exec}),
):
    template = (Path(assets) / f"{app_id}.{suffix}.in").read_text(encoding="utf-8")
    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)
    output = destination / f"{app_id}.{suffix}"
    output.write_text(template, encoding="utf-8")
    output.chmod(0o644)

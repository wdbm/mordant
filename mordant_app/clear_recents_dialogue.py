"""
Mordant confirmation window for clearing recent directories

Copyright (C) 2026 William Breaden Madden
SPDX-License-Identifier: GPL-3.0-or-later
"""

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gtk


class ClearRecentsDialogue(Gtk.Window):
    """Ask for confirmation before recent-directory history is cleared."""

    def __init__(self, parent: Gtk.Window, on_confirm: Callable[[], object]):
        super().__init__(title="Clear recent directories", transient_for=parent, modal=True)
        self.set_destroy_with_parent(True)
        self.set_resizable(False)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for edge in ("top", "bottom", "start", "end"):
            getattr(content, f"set_margin_{edge}")(12)
        self.set_child(content)

        heading = Gtk.Label(label="Clear all recent directories?", xalign=0)
        content.append(heading)
        details = Gtk.Label(
            label=(
                "All remembered recent directories and the current directory selection "
                "will be cleared. This cannot be undone."
            ),
            xalign=0,
        )
        details.set_wrap(True)
        details.set_max_width_chars(60)
        content.append(details)

        actions = Gtk.Box(spacing=8)
        actions.set_halign(Gtk.Align.END)
        content.append(actions)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_args: self.close())
        actions.append(cancel)
        self._cancel_button = cancel
        clear = Gtk.Button(label="Clear recents")
        clear.add_css_class("destructive-action")
        clear.connect("clicked", lambda *_args: on_confirm())
        actions.append(clear)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key_pressed)
        self.add_controller(keys)

    def focus_cancel_button(self) -> None:
        self._cancel_button.grab_focus()

    def _key_pressed(self, _controller, keyval, _keycode, _state):
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        return False

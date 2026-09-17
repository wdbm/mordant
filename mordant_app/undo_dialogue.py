"""
Mordant confirmation window for one-step undo

Copyright (C) 2026 William Breaden Madden
SPDX-License-Identifier: GPL-3.0-or-later
"""

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gtk


class UndoDialogue(Gtk.Window):
    """Display the affected paths before a transfer is undone."""

    def __init__(self, parent: Gtk.Window, description: str, on_confirm: Callable[[], object]):
        super().__init__(title="Undo transfer", transient_for=parent, modal=True)
        self.set_destroy_with_parent(True)
        self.set_default_size(680, 420)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for edge in ("top", "bottom", "start", "end"):
            getattr(content, f"set_margin_{edge}")(12)
        self.set_child(content)

        heading = Gtk.Label(label="Undo the last transfer?", xalign=0)
        content.append(heading)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.set_min_content_height(120)
        details = Gtk.TextView()
        details.set_editable(False)
        details.set_cursor_visible(False)
        details.set_wrap_mode(Gtk.WrapMode.CHAR)
        details.get_buffer().set_text(description.partition("\n\n")[2] or description)
        scroller.set_child(details)
        content.append(scroller)

        actions = Gtk.Box(spacing=8)
        actions.set_halign(Gtk.Align.END)
        content.append(actions)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_args: self.close())
        actions.append(cancel)
        self._cancel_button = cancel
        undo = Gtk.Button(label="Undo transfer")
        undo.connect("clicked", lambda *_args: on_confirm())
        actions.append(undo)

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

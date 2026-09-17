"""
Mordant configuration chooser

Copyright (C) 2026 William Breaden Madden
SPDX-License-Identifier: GPL-3.0-or-later
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, Gtk, Pango


class ConfigurationDialogue(Gtk.Window):
    def __init__(self, parent, configuration, on_select):
        super().__init__(title="Choose Mordant configuration", transient_for=parent, modal=True)
        self.configuration = configuration
        self.on_select = on_select
        self.chooser = None
        self.buttons = {}
        self.set_default_size(700, 420)
        self.set_destroy_with_parent(True)
        outer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12,
            margin_top=16, margin_bottom=16, margin_start=16, margin_end=16,
        )
        heading = Gtk.Label(label="Configuration directory", xalign=0)
        heading.add_css_class("title-2")
        outer.append(heading)
        current = Gtk.Label(
            label=f"Current: {configuration.directory}", xalign=0,
            selectable=True, wrap=True,
        )
        current.add_css_class("dim-label")
        outer.append(current)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        recent_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        scroller.set_child(recent_box)
        outer.append(scroller)
        recents = configuration.history.load()
        if not recents:
            recent_box.append(Gtk.Label(label="No recent configurations.", xalign=0))
        for number, path in enumerate(recents, 1):
            row = Gtk.Box(spacing=10, margin_start=8, margin_end=8,
                          margin_top=6, margin_bottom=6)
            row.append(Gtk.Label(label=f"{number}"))
            label = Gtk.Label(label=str(path), xalign=0, hexpand=True,
                              ellipsize=Pango.EllipsizeMode.MIDDLE)
            label.set_max_width_chars(65)
            row.append(label)
            if path == configuration.directory:
                marker = Gtk.Label(label="Current")
                marker.add_css_class("dim-label")
                row.append(marker)
            button = Gtk.Button(tooltip_text=str(path))
            button.set_child(row)
            button.connect("clicked", lambda _button, selected=path: self.on_select(selected))
            recent_box.append(button)
            self.buttons[path] = button
        actions = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        browse = Gtk.Button(label="Select another...", tooltip_text="Choose a configuration directory")
        browse.connect("clicked", self.browse)
        actions.append(browse)
        close = Gtk.Button(label="Close", tooltip_text="Close this window")
        close.connect("clicked", lambda *_: self.close())
        actions.append(close)
        outer.append(actions)
        self.set_child(outer)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key_pressed)
        self.add_controller(keys)
        self.connect("close-request", self._close_chooser)
        self.connect("unrealize", self._close_chooser)

    def _key_pressed(self, _controller, key, _code, _state):
        if key == Gdk.KEY_Escape:
            self.close()
            return True
        return False

    def browse(self, *_):
        if self.chooser is not None:
            self.chooser.show()
            return
        chooser = Gtk.FileChooserNative.new(
            "Select Mordant configuration directory", self,
            Gtk.FileChooserAction.SELECT_FOLDER, "Select", "Cancel",
        )
        if self.configuration.directory.is_dir():
            chooser.set_current_folder(Gio.File.new_for_path(str(self.configuration.directory)))
        chooser.connect("response", self._response)
        self.chooser = chooser
        chooser.show()

    def _response(self, chooser, answer):
        file = chooser.get_file() if answer == Gtk.ResponseType.ACCEPT else None
        selected = file.get_path() if file else None
        chooser.destroy()
        self.chooser = None
        if selected:
            self.on_select(selected)

    def _close_chooser(self, *_):
        if self.chooser is not None:
            self.chooser.destroy()
            self.chooser = None
        return False

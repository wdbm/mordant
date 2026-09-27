"""
Mordant chooser for opening media filenames common to multiple directories

Copyright (C) 2026 William Breaden Madden
SPDX-License-Identifier: GPL-3.0-or-later
"""

from collections.abc import Callable, Iterable
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, Gtk, Pango


class CommonDirectoriesDialogue(Gtk.Window):
    """Collect an ordered set of directories for a filename intersection."""

    def __init__(
        self,
        parent: Gtk.Window,
        on_open: Callable[[tuple[Path, ...]], object],
        initial_directories: Iterable[str | Path] = (),
        initial_directory: str | Path | None = None,
    ):
        super().__init__(title="Open common files", transient_for=parent, modal=True)
        self.set_destroy_with_parent(True)
        self.set_default_size(760, 520)
        self.on_open = on_open
        self.directories: list[Path] = []
        self.initial_directory = (
            Path(initial_directory).expanduser().resolve()
            if initial_directory is not None
            else None
        )
        self._chooser = None

        outer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=16,
            margin_bottom=16,
            margin_start=16,
            margin_end=16,
        )
        self.set_child(outer)

        heading = Gtk.Label(label="Open media common to multiple directories", xalign=0)
        heading.add_css_class("title-2")
        outer.append(heading)
        explanation = Gtk.Label(
            label=(
                "Choose at least two directories. Only media filenames present directly "
                "in every chosen directory will be opened. Matching is by exact filename."
            ),
            xalign=0,
            wrap=True,
        )
        explanation.set_max_width_chars(90)
        outer.append(explanation)
        representative = Gtk.Label(
            label=(
                "Files from the first directory will be displayed. Other directories are "
                "used only to find matching filenames."
            ),
            xalign=0,
            wrap=True,
        )
        representative.add_css_class("dim-label")
        representative.set_max_width_chars(90)
        outer.append(representative)

        scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.directory_list = Gtk.ListBox()
        self.directory_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.directory_list.connect("row-selected", self._row_selected)
        scroller.set_child(self.directory_list)
        outer.append(scroller)

        controls = Gtk.Box(spacing=8)
        outer.append(controls)
        add_button = Gtk.Button(label="Add directories...")
        add_button.connect("clicked", self._choose_directories)
        controls.append(add_button)
        self.remove_button = Gtk.Button(label="Remove")
        self.remove_button.connect("clicked", self._remove_selected)
        controls.append(self.remove_button)
        self.clear_button = Gtk.Button(label="Clear")
        self.clear_button.connect("clicked", self._clear)
        controls.append(self.clear_button)

        self.message_label = Gtk.Label(xalign=0, wrap=True)
        self.message_label.set_visible(False)
        outer.append(self.message_label)

        actions = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        outer.append(actions)
        self.cancel_button = Gtk.Button(label="Cancel")
        self.cancel_button.connect("clicked", lambda *_args: self.close())
        actions.append(self.cancel_button)
        self.open_button = Gtk.Button(label="Open common files")
        self.open_button.add_css_class("suggested-action")
        self.open_button.connect("clicked", self._open)
        actions.append(self.open_button)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key_pressed)
        self.add_controller(keys)
        self.connect("close-request", self._close_chooser)
        self.connect("unrealize", self._close_chooser)

        self.add_directories(initial_directories)

    def focus_cancel_button(self) -> None:
        self.cancel_button.grab_focus()

    def set_message(self, message: str) -> None:
        self.message_label.set_text(message)
        self.message_label.set_visible(bool(message))

    def add_directories(self, directories: Iterable[str | Path]) -> None:
        errors = []
        for directory in directories:
            path = Path(directory).expanduser().resolve()
            if not path.is_dir():
                errors.append(f"Directory is unavailable: {path}")
            elif path not in self.directories:
                self.directories.append(path)
        self._render_directories()
        self.set_message("\n".join(errors))

    def _render_directories(self) -> None:
        child = self.directory_list.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.directory_list.remove(child)
            child = next_child
        for position, directory in enumerate(self.directories):
            row = Gtk.ListBoxRow()
            row.directory = directory
            content = Gtk.Box(
                spacing=10,
                margin_top=8,
                margin_bottom=8,
                margin_start=10,
                margin_end=10,
            )
            marker = Gtk.Label(label="Primary" if position == 0 else "")
            marker.set_width_chars(8)
            marker.add_css_class("dim-label")
            content.append(marker)
            label = Gtk.Label(label=str(directory), xalign=0, hexpand=True)
            label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            label.set_tooltip_text(str(directory))
            content.append(label)
            row.set_child(content)
            self.directory_list.append(row)
        self.open_button.set_sensitive(len(self.directories) >= 2)
        self.clear_button.set_sensitive(bool(self.directories))
        self.remove_button.set_sensitive(False)

    def _row_selected(self, _list_box, row) -> None:
        self.remove_button.set_sensitive(row is not None)

    def _remove_selected(self, *_args) -> None:
        row = self.directory_list.get_selected_row()
        if row is None:
            return
        self.directories.remove(row.directory)
        self.set_message("")
        self._render_directories()

    def _clear(self, *_args) -> None:
        self.directories.clear()
        self.set_message("")
        self._render_directories()

    def _choose_directories(self, *_args) -> None:
        if self._chooser is not None:
            self._chooser.show()
            return
        chooser = Gtk.FileChooserNative.new(
            "Add directories", self, Gtk.FileChooserAction.SELECT_FOLDER, "Add", "Cancel",
        )
        chooser.set_select_multiple(True)
        start = self.directories[0] if self.directories else self.initial_directory
        if start is not None and start.is_dir():
            chooser.set_current_folder(Gio.File.new_for_path(str(start)))
        chooser.connect("response", self._chooser_response)
        self._chooser = chooser
        chooser.show()

    def _chooser_response(self, chooser, answer) -> None:
        paths = []
        if answer == Gtk.ResponseType.ACCEPT:
            files = chooser.get_files()
            paths = [files.get_item(index).get_path() for index in range(files.get_n_items())]
        chooser.destroy()
        if self._chooser is chooser:
            self._chooser = None
        self.add_directories(path for path in paths if path)
        self.present()

    def _open(self, *_args) -> None:
        if len(self.directories) < 2:
            self.set_message("Choose at least two distinct directories.")
            return
        if self.on_open(tuple(self.directories)) is not False:
            self.close()

    def _key_pressed(self, _controller, key, _code, _state):
        if key == Gdk.KEY_Escape:
            self.close()
            return True
        return False

    def _close_chooser(self, *_args):
        if self._chooser is not None:
            self._chooser.destroy()
            self._chooser = None
        return False

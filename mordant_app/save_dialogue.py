"""
Mordant GTK save dialogue shared by image and video views

Copyright (C) 2026 William Breaden Madden
SPDX-License-Identifier: GPL-3.0-or-later
"""

from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, Gtk, Pango

from .recent_directories import (
    RECENT_DIRECTORY_KEYS,
    RECENT_DIRECTORY_LISTING_MODE_ALPHABETICAL_NAME,
    RECENT_DIRECTORY_LISTING_MODE_ALPHABETICAL_PATH,
    RECENT_DIRECTORY_LISTING_MODE_MOST_RECENT,
    SaveSession,
)
from .transfers import TransferManager, TransferRequest
from .undo_dialogue import UndoDialogue

DEFAULT_GRID_COLUMNS = 5


class SaveDialogue(Gtk.Window):
    """
    Collect a transfer request for the source captured when this window opens.

    `on_save(request)` executes the transfer; exceptions keep the dialogue open.
    `on_undo(restored_path)` updates the owning view after a successful undo.
    Neither callback needs to expose the parent's widgets or playlist internals.
    """

    def __init__(
        self,
        parent: Gtk.Window,
        session: SaveSession,
        manager: TransferManager,
        source_path: Path | None,
        on_save: Callable[[TransferRequest], object],
        on_undo: Callable[[Path | None], object],
    ):
        super().__init__(title="Save to directories", transient_for=parent, modal=True)
        self.set_destroy_with_parent(True)
        self.set_default_size(1180, 820)
        self.set_resizable(True)
        self.session = session
        self.manager = manager
        self.source_path = Path(source_path).resolve() if source_path is not None else None
        self.on_save = on_save
        self.on_undo = on_undo
        self.recent_buttons: dict[str, Gtk.ToggleButton] = {}
        self.list_view = False
        self._browse_chooser = None
        self._directory_popover = None
        self._undo_window = None
        self.browse_initial_directory = self.source_path.parent if self.source_path else None

        outer = self._build_outer()
        self._build_filename_row(outer)
        self._build_directory_controls(outer)
        self._build_directory_grid(outer)
        self._build_transfer_options(outer)
        self._build_actions(outer)
        self._connect_controllers()
        self._render_recent_buttons()
        self._refresh_selection_ui()
        if not self._source_available():
            self._set_message("No source file available. The last transfer can still be undone.")
        self.present()
        self.filename_entry.grab_focus()

    def _build_outer(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for edge in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{edge}")(12)
        outer.add_css_class("save-dialogue")
        self.set_child(outer)

        return outer

    def _build_filename_row(self, outer):
        filename_row = Gtk.Box(spacing=8)
        filename_row.append(Gtk.Label(label="Filename:", xalign=0))
        self.filename_entry = Gtk.Entry()
        self.filename_entry.set_hexpand(True)
        self.filename_entry.set_text(self.source_path.name if self.source_path else "")
        filename_row.append(self.filename_entry)
        outer.append(filename_row)

    def _build_directory_controls(self, outer):
        heading = Gtk.Label(label="Recent directories - 1-9, 0 select the first ten", xalign=0)
        heading.set_wrap(True)
        heading.set_hexpand(True)
        heading_row = Gtk.Box(spacing=8)
        heading_row.append(heading)
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_property("placeholder-text", "Search directories...")
        self.search_entry.set_tooltip_text("Search all remembered directories by name or full path")
        heading_row.append(self.search_entry)
        outer.append(heading_row)
        listing_options = self._flow_row()
        outer.append(listing_options)
        alphabetical_name = Gtk.CheckButton(label="Alphabetical (directory name)")
        alphabetical_path = Gtk.CheckButton(label="Alphabetical (full path)")
        most_recent = Gtk.CheckButton(label="Most recent")
        alphabetical_path.set_group(alphabetical_name)
        most_recent.set_group(alphabetical_name)
        self.listing_buttons = {}
        for button, mode in (
            (alphabetical_name, RECENT_DIRECTORY_LISTING_MODE_ALPHABETICAL_NAME),
            (alphabetical_path, RECENT_DIRECTORY_LISTING_MODE_ALPHABETICAL_PATH),
            (most_recent, RECENT_DIRECTORY_LISTING_MODE_MOST_RECENT),
        ):
            self.listing_buttons[mode] = button
            button.set_active(self.session.listing_mode == mode)
            button.connect("toggled", self._set_listing_mode, mode)
            listing_options.append(button)
        list_button = Gtk.CheckButton(label="Full-path list")
        list_button.connect("toggled", self._change_view)
        listing_options.append(list_button)
        clear_button = Gtk.Button(label="Clear recents")
        clear_button.connect("clicked", self._clear_recents)
        listing_options.append(clear_button)

    def _build_directory_grid(self, outer):
        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_min_content_height(130)
        self.scroller.set_vexpand(True)
        self.scroller.set_hexpand(True)
        self.scroller.add_css_class("save-dialogue-pane")
        self.recent_grid = Gtk.Grid(column_spacing=8, row_spacing=8)
        self.recent_grid.set_column_homogeneous(True)
        self.recent_grid.set_hexpand(True)
        self.recent_grid.set_valign(Gtk.Align.START)
        self.scroller.set_child(self.recent_grid)
        outer.append(self.scroller)

        self.selected_label = Gtk.Label(xalign=0)
        self.selected_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.selected_label.set_width_chars(1)
        outer.append(self.selected_label)

    def _build_transfer_options(self, outer):
        options = self._flow_row()
        outer.append(options)
        for label, attribute in (
            ("Move", "move_requested"),
            ("Overwrite", "overwrite_requested"),
            ("Open parent directory", "open_parent_directory"),
        ):
            button = Gtk.CheckButton(label=label)
            button.set_active(getattr(self.session, attribute))
            button.connect("toggled", self._change_option, attribute)
            options.append(button)
        browse_button = Gtk.Button(label="Browse...")
        browse_button.connect("clicked", self._browse)
        options.append(browse_button)
        deselect_button = Gtk.Button(label="Deselect")
        deselect_button.connect("clicked", self._deselect_directories)
        options.append(deselect_button)

    def _build_actions(self, outer):
        self.message_label = Gtk.Label(xalign=0)
        self.message_label.set_wrap(True)
        self.message_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.message_label.set_width_chars(1)
        self.message_label.set_visible(False)
        outer.append(self.message_label)

        actions = Gtk.Box(spacing=8)
        outer.append(actions)
        self.undo_button = Gtk.Button(label="Undo")
        self.undo_button.connect("clicked", self.request_undo)
        actions.append(self.undo_button)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        actions.append(spacer)
        cancel_button = Gtk.Button(label="Cancel")
        cancel_button.connect("clicked", lambda *_args: self.close())
        actions.append(cancel_button)
        self.save_button = Gtk.Button(label="Save")
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", self._do_save)
        actions.append(self.save_button)

    def _connect_controllers(self):
        self.filename_entry.connect("changed", lambda *_args: self._refresh_selection_ui())
        self.filename_entry.connect("activate", self._do_save)
        controller = Gtk.EventControllerKey()
        controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(controller)
        self.connect("close-request", self._on_close_request)
        # Editable::changed updates immediately, without SearchEntry's debounce.
        self.search_entry.connect("changed", self._search_changed)

    @staticmethod
    def _flow_row():
        row = Gtk.FlowBox()
        row.set_selection_mode(Gtk.SelectionMode.NONE)
        row.set_column_spacing(8)
        row.set_row_spacing(4)
        row.set_min_children_per_line(1)
        row.set_max_children_per_line(8)
        row.set_homogeneous(False)
        return row

    def _source_available(self):
        return self.source_path is not None and self.source_path.is_file()

    def _set_message(self, message):
        self.message_label.set_text(message)
        self.message_label.set_visible(bool(message))

    def _focus_is_text_input(self):
        # Gtk.Entry delegates keyboard focus to an internal Gtk.Text widget.
        focus = self.get_focus()
        while focus is not None and focus is not self:
            if isinstance(focus, (Gtk.Editable, Gtk.TextView)):
                return True
            focus = focus.get_parent()
        return False

    def _set_listing_mode(self, button, mode):
        if button.get_active() and self.session.set_listing_mode(mode):
            self._render_recent_buttons()

    def _change_option(self, button, attribute):
        setattr(self.session, attribute, button.get_active())

    def _refresh_selection_ui(self):
        summary = f"Selected: {self.session.selection_summary()}"
        hidden = len(self.session.selected_directories.difference(self.recent_buttons))
        if hidden:
            summary += f" - {hidden} outside this listing"
        self.selected_label.set_text(summary)
        self.selected_label.set_tooltip_text(summary)
        self.save_button.set_sensitive(
            self._source_available()
            and bool(self.session.selected_directories)
            and bool(self.filename_entry.get_text().strip())
        )
        self.undo_button.set_sensitive(bool(self.manager.undo_description()))
        for path, button in self.recent_buttons.items():
            selected = self.session.is_selected(path)
            if button.get_active() != selected:
                button.set_active(selected)

    def _render_recent_buttons(self):
        self._dispose_popover()
        child = self.recent_grid.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.recent_grid.remove(child)
            child = next_child
        self.recent_buttons.clear()
        query = self.search_entry.get_text()
        directories = self.session.listed_directories(query)
        if not directories:
            empty = Gtk.Label(label=("No matching directories." if query.strip() else
                                     "No recent directories yet. Choose Browse to add one."))
            empty.set_wrap(True)
            self.recent_grid.attach(empty, 0, 0, 1, 1)
            return
        columns = 1 if self.list_view else DEFAULT_GRID_COLUMNS
        for position, directory in enumerate(directories):
            path = str(directory)
            prefix = f"[{RECENT_DIRECTORY_KEYS[position]}] " if position < 10 else ""
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            title = Gtk.Label(label=f"{prefix}{directory.name or path}", xalign=0)
            title.set_ellipsize(Pango.EllipsizeMode.END)
            title.set_width_chars(1)
            title.set_max_width_chars(20)
            content.append(title)
            path_label = Gtk.Label(label=path, xalign=0)
            path_label.add_css_class("dim-label")
            path_label.add_css_class("directory-path")
            path_label.set_width_chars(1)
            path_label.set_max_width_chars(1 if self.list_view else 20)
            path_label.set_hexpand(True)
            if self.list_view:
                path_label.set_wrap(True)
                path_label.set_wrap_mode(Pango.WrapMode.CHAR)
            else:
                path_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            content.append(path_label)
            button = Gtk.ToggleButton()
            button.add_css_class("recent-directory-button")
            button.set_hexpand(True)
            button.set_halign(Gtk.Align.FILL)
            button.set_valign(Gtk.Align.FILL)
            button.set_child(content)
            button.set_tooltip_text(path)
            button.set_active(self.session.is_selected(path))
            button.connect("toggled", self._toggle_directory_button, path)
            context = Gtk.GestureClick()
            context.set_button(3)
            context.connect("pressed", self._directory_menu, button, path)
            button.add_controller(context)
            self.recent_buttons[path] = button
            self.recent_grid.attach(button, position % columns, position // columns, 1, 1)

    def _change_view(self, button):
        self.list_view = button.get_active()
        self._render_recent_buttons()

    def _search_changed(self, *_args):
        self._render_recent_buttons()
        self._refresh_selection_ui()
        self.scroller.get_vadjustment().set_value(0)

    def _displayed_directory_for_key(self, key):
        return dict(zip(RECENT_DIRECTORY_KEYS, map(Path, self.recent_buttons))).get(key)

    def _search_has_focus(self):
        focus = self.get_focus()
        while focus is not None:
            if focus is self.search_entry:
                return True
            focus = focus.get_parent()
        return False

    def _dispose_popover(self):
        popover, self._directory_popover = self._directory_popover, None
        if popover is not None:
            popover.popdown()
            if popover.get_parent() is not None:
                popover.unparent()

    def _directory_menu(self, gesture, _count, x, y, button, path):
        self._dispose_popover()
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        popover = Gtk.Popover()
        self._directory_popover = popover
        popover.set_parent(button)
        rectangle = Gdk.Rectangle()
        rectangle.x, rectangle.y, rectangle.width, rectangle.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rectangle)
        remove = Gtk.Button(label="Remove from recent directories")

        def remove_directory(*_args):
            self._dispose_popover()
            try:
                self.session.remove_recent(path)
            except OSError as error:
                self._set_message(f"Recent-directory history could not be saved: {error}")
                self._refresh_selection_ui()
                return
            self._set_message("")
            self._render_recent_buttons()
            self._refresh_selection_ui()

        def closed(widget):
            if widget.get_parent() is not None:
                widget.unparent()
            if self._directory_popover is widget:
                self._directory_popover = None

        remove.connect("clicked", remove_directory)
        popover.set_child(remove)
        popover.connect("closed", closed)
        popover.popup()

    def _toggle_directory_button(self, button, path):
        if button.get_active() != self.session.is_selected(path):
            self.session.toggle(path)
        self._refresh_selection_ui()

    def _browse(self, *_args):
        if self._browse_chooser is not None:
            self._browse_chooser.show()
            return
        chooser = Gtk.FileChooserNative.new(
            "Choose a directory to save to", self,
            Gtk.FileChooserAction.SELECT_FOLDER, "Select", "Cancel",
        )
        chooser.set_modal(True)
        initial = self.session.browse_start_directory(self.browse_initial_directory)
        if initial is not None:
            chooser.set_current_folder(Gio.File.new_for_path(str(initial)))
        chooser.connect("response", self._on_browse_response)
        self._browse_chooser = chooser
        chooser.show()

    def _on_browse_response(self, chooser, response):
        if response == Gtk.ResponseType.ACCEPT:
            selected = chooser.get_file()
            if selected is not None and selected.get_path():
                try:
                    self.browse_initial_directory = self.session.remember_directory(selected.get_path())
                except OSError as error:
                    self._set_message(f"Recent-directory history could not be saved: {error}")
                    self._refresh_selection_ui()
                else:
                    self._set_message("")
                    self._render_recent_buttons()
                    self._refresh_selection_ui()
        chooser.destroy()
        if self._browse_chooser is chooser:
            self._browse_chooser = None
        self.present()
        self.filename_entry.grab_focus()

    def _clear_recents(self, *_args):
        try:
            self.session.clear_recents()
        except OSError as error:
            self._set_message(f"Recent-directory history could not be saved: {error}")
            self._refresh_selection_ui()
            return
        self._set_message("")
        self._render_recent_buttons()
        self._refresh_selection_ui()

    def _deselect_directories(self, *_args):
        self.session.deselect_all()
        self._refresh_selection_ui()

    def _do_save(self, *_args, destinations=None):
        if not self._source_available():
            self._set_message("The source file is no longer available.")
            self._refresh_selection_ui()
            return
        typed_name = self.filename_entry.get_text()
        if not typed_name.strip():
            self._set_message("Please enter a filename.")
            self.filename_entry.grab_focus()
            return
        destinations = self.session.selected_paths() if destinations is None else destinations
        if not destinations:
            self._set_message("Please select at least one directory.")
            return
        request = self.session.request(
            self.source_path, typed_name,
            destinations=destinations, default_suffix=self.source_path.suffix,
        )
        try:
            self.on_save(request)
        except Exception as error:
            self._set_message(f"Transfer failed: {error}")
            self._refresh_selection_ui()
            return
        self.close()

    def _save_to_index(self, key):
        target = self._displayed_directory_for_key(key)
        if target is not None:
            self._do_save(destinations=[target])

    def _on_key_pressed(self, _controller, keyval, _keycode, state):
        if keyval == Gdk.KEY_Escape:
            if self._search_has_focus() and self.search_entry.get_text():
                self.search_entry.set_text("")
                return True
            self.close()
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if self._search_has_focus():
                return True
            self._do_save()
            return True
        if self._focus_is_text_input():
            return False
        character = Gdk.keyval_to_unicode(keyval)
        digit = chr(character) if character else ""
        if digit not in "0123456789" or not digit:
            return False
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SUPER_MASK):
            return False
        if state & Gdk.ModifierType.ALT_MASK:
            self._save_to_index(int(digit))
        else:
            target = self._displayed_directory_for_key(int(digit))
            if target is not None:
                self.session.toggle(target)
                self._refresh_selection_ui()
        return True

    def request_undo(self, *_args):
        description = self.manager.undo_description()
        if not description:
            return
        if self._undo_window is not None:
            self._undo_window.present()
            return
        confirmation = UndoDialogue(self, description, self._confirm_undo)
        self._undo_window = confirmation
        confirmation.connect("close-request", self._undo_closed)
        confirmation.connect("unrealize", self._undo_closed)
        confirmation.present()
        confirmation.focus_cancel_button()

    def _undo_closed(self, dialogue, *_args):
        if self._undo_window is dialogue:
            self._undo_window = None
        return False

    def _confirm_undo(self, *_args):
        if self._undo_window is not None:
            self._undo_window.close()
        try:
            restored = self.manager.undo()
        except OSError as error:
            self._set_message(str(error))
        else:
            self._set_message("Last transfer undone.")
            self.on_undo(restored)
        self._render_recent_buttons()
        self._refresh_selection_ui()

    def _on_close_request(self, *_args):
        self._dispose_popover()
        if self._browse_chooser is not None:
            self._browse_chooser.destroy()
            self._browse_chooser = None
        if self._undo_window is not None:
            self._undo_window.close()
        return False

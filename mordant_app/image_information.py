"""
Mordant image information, metadata editing, and confirmation windows

Copyright (C) 2026 William Breaden Madden
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk, Pango

from .metadata import (
    EDITABLE_METADATA_FIELDS,
    ImageInformation,
    describe_metadata_changes,
    read_image_information,
    validate_editable_values,
)


def format_file_size(size: int) -> str:
    value = float(size)
    units = ("bytes", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "bytes":
                return "1 byte" if size == 1 else f"{size:,} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size:,} bytes"


class MetadataConfirmationDialogue(Gtk.Window):
    def __init__(
        self,
        parent: Gtk.Window,
        heading: str,
        details: str,
        confirm_label: str,
        on_confirm: Callable[[], object],
        *,
        destructive: bool = False,
    ):
        super().__init__(title=heading, transient_for=parent, modal=True)
        self.set_destroy_with_parent(True)
        self.set_default_size(620, 360)

        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=16,
            margin_bottom=16,
            margin_start=16,
            margin_end=16,
        )
        self.set_child(content)
        title = Gtk.Label(label=heading, xalign=0)
        title.add_css_class("title-3")
        content.append(title)
        explanation = Gtk.Label(
            label="The following metadata changes will be made:",
            xalign=0,
        )
        content.append(explanation)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        details_view = Gtk.TextView(
            editable=False,
            cursor_visible=False,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
        )
        details_view.get_buffer().set_text(details)
        scroller.set_child(details_view)
        content.append(scroller)

        notice = Gtk.Label(
            label=(
                "The encoded image pixels will not be altered. A byte-for-byte backup "
                "will be retained for Mordant's Undo operation."
            ),
            xalign=0,
            wrap=True,
        )
        notice.add_css_class("dim-label")
        content.append(notice)
        actions = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        content.append(actions)
        cancel = Gtk.Button(label="Cancel", tooltip_text="Do not change the metadata")
        cancel.connect("clicked", lambda *_args: self.close())
        actions.append(cancel)
        self._cancel_button = cancel
        confirm = Gtk.Button(
            label=confirm_label,
            tooltip_text="Apply the listed metadata changes",
        )
        confirm.add_css_class("destructive-action" if destructive else "suggested-action")
        confirm.connect("clicked", lambda *_args: on_confirm())
        actions.append(confirm)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key_pressed)
        self.add_controller(keys)

    def present_with_safe_focus(self):
        self.present()
        self._cancel_button.grab_focus()

    def _key_pressed(self, _controller, keyval, _keycode, _state):
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        return False


class MetadataEditDialogue(Gtk.Window):
    def __init__(
        self,
        parent: Gtk.Window,
        values: dict[str, str],
        on_review: Callable[[dict[str, str], list[str], Gtk.Window], object],
    ):
        super().__init__(title="Edit image metadata", transient_for=parent, modal=True)
        self.set_destroy_with_parent(True)
        self.set_default_size(700, 570)
        self._original_values = dict(values)
        self._on_review = on_review
        self.entries = {}

        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=16,
            margin_bottom=16,
            margin_start=16,
            margin_end=16,
        )
        self.set_child(content)
        heading = Gtk.Label(label="Edit selected metadata", xalign=0)
        heading.add_css_class("title-3")
        content.append(heading)
        guidance = Gtk.Label(
            label=(
                "Empty fields remove the corresponding value. Keywords are separated by commas; "
                "GPS coordinates use signed decimal degrees."
            ),
            xalign=0,
            wrap=True,
        )
        guidance.set_max_width_chars(80)
        content.append(guidance)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        content.append(scroller)
        grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        grid.set_margin_end(8)
        scroller.set_child(grid)
        for row, (name, label, _tag) in enumerate(EDITABLE_METADATA_FIELDS):
            field_label = Gtk.Label(label=f"{label}:", xalign=1, yalign=0.5)
            grid.attach(field_label, 0, row, 1, 1)
            entry = Gtk.Entry(hexpand=True)
            entry.set_text(values.get(name, ""))
            entry.set_tooltip_text(f"Edit {label.casefold()}")
            if name == "date_time_original":
                entry.set_placeholder_text("YYYY:MM:DD HH:MM:SS")
            elif name == "keywords":
                entry.set_placeholder_text("keyword one, keyword two")
            elif name.startswith("gps_"):
                entry.set_placeholder_text("Signed decimal degrees")
            self.entries[name] = entry
            grid.attach(entry, 1, row, 1, 1)

        self.message = Gtk.Label(xalign=0, wrap=True, visible=False)
        self.message.add_css_class("error")
        content.append(self.message)
        actions = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        content.append(actions)
        cancel = Gtk.Button(label="Cancel", tooltip_text="Discard metadata edits")
        cancel.connect("clicked", lambda *_args: self.close())
        actions.append(cancel)
        review = Gtk.Button(
            label="Review changes...",
            tooltip_text="Review the metadata changes before applying them",
        )
        review.add_css_class("suggested-action")
        review.connect("clicked", self._review)
        actions.append(review)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key_pressed)
        self.add_controller(keys)

    def _review(self, *_args):
        try:
            values = validate_editable_values(
                {name: entry.get_text() for name, entry in self.entries.items()}
            )
        except ValueError as error:
            self.message.set_text(str(error))
            self.message.set_visible(True)
            return
        changes = describe_metadata_changes(self._original_values, values)
        if not changes:
            self.message.set_text("No metadata values have been changed.")
            self.message.set_visible(True)
            return
        self.message.set_visible(False)
        self._on_review(values, changes, self)

    def _key_pressed(self, _controller, keyval, _keycode, _state):
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        return False


class ImageInformationWindow(Gtk.Window):
    def __init__(
        self,
        parent: Gtk.Window,
        path: str | Path,
        on_action: Callable[[str, dict[str, str] | None], object],
    ):
        self.path = Path(path).resolve()
        super().__init__(
            title=f"Image information - {self.path.name}",
            transient_for=parent,
            modal=False,
        )
        self.set_destroy_with_parent(True)
        self.set_default_size(860, 700)
        self._on_action = on_action
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mordant-metadata-read")
        self._load_serial = 0
        self._closed = False
        self._busy = False
        self._change_in_progress = False
        self.information: ImageInformation | None = None
        self._edit_window = None
        self._confirmation_window = None

        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
            margin_top=14,
            margin_bottom=14,
            margin_start=14,
            margin_end=14,
        )
        self.set_child(content)
        heading = Gtk.Label(label=self.path.name, xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        heading.add_css_class("title-2")
        heading.set_tooltip_text(str(self.path))
        content.append(heading)

        self.summary = Gtk.Label(xalign=0, selectable=True, wrap=True)
        content.append(self.summary)
        self.privacy_notice = Gtk.Label(xalign=0, wrap=True, visible=False)
        self.privacy_notice.add_css_class("warning")
        content.append(self.privacy_notice)
        self.status = Gtk.Label(xalign=0, wrap=True)
        content.append(self.status)

        self.search = Gtk.SearchEntry(placeholder_text="Search metadata")
        self.search.set_tooltip_text("Filter metadata by group, tag, or value")
        self.search.connect("search-changed", lambda *_args: self._render_entries())
        content.append(self.search)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        content.append(scroller)
        self.metadata_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.metadata_list.add_css_class("boxed-list")
        scroller.set_child(self.metadata_list)

        actions = Gtk.Box(spacing=8)
        content.append(actions)
        self.edit_button = Gtk.Button(
            label="Edit metadata...",
            tooltip_text="Edit selected image metadata fields",
        )
        self.edit_button.connect("clicked", self._edit)
        actions.append(self.edit_button)
        self.location_button = Gtk.Button(
            label="Remove location...",
            tooltip_text="Review removal of GPS and location metadata",
        )
        self.location_button.connect("clicked", lambda *_args: self._confirm_removal("location"))
        actions.append(self.location_button)
        self.private_button = Gtk.Button(
            label="Remove private data...",
            tooltip_text="Review removal of location and identifying metadata",
        )
        self.private_button.connect("clicked", lambda *_args: self._confirm_removal("private"))
        actions.append(self.private_button)
        self.all_button = Gtk.Button(
            label="Remove metadata...",
            tooltip_text="Review removal of all removable metadata",
        )
        self.all_button.add_css_class("destructive-action")
        self.all_button.connect("clicked", lambda *_args: self._confirm_removal("all"))
        actions.append(self.all_button)
        spacer = Gtk.Box(hexpand=True)
        actions.append(spacer)
        close = Gtk.Button(label="Close", tooltip_text="Close image information")
        close.connect("clicked", lambda *_args: self.close())
        actions.append(close)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key_pressed)
        self.add_controller(keys)
        self.connect("close-request", self._close_requested)
        self.connect("unrealize", self._unrealized)
        self.refresh()

    def refresh(self):
        if self._closed:
            return
        self._change_in_progress = False
        self._load_serial += 1
        serial = self._load_serial
        self.status.set_text("Reading image information...")
        self.set_busy(True)
        future = self._executor.submit(read_image_information, self.path)
        future.add_done_callback(
            lambda completed: GLib.idle_add(self._loaded, serial, completed)
        )

    def _loaded(self, serial, future):
        if self._closed or serial != self._load_serial:
            return GLib.SOURCE_REMOVE
        try:
            information = future.result()
        except Exception as error:
            self.information = None
            self.status.set_text(f"Image information could not be read: {error}")
            self._render_entries()
        else:
            self.information = information
            dimensions = f"{information.dimensions[0]} x {information.dimensions[1]} px"
            frames = f"; {information.frame_count} frames" if information.frame_count > 1 else ""
            modified = datetime.fromtimestamp(information.modified_time).astimezone().strftime(
                "%Y-%m-%d %H:%M:%S %Z"
            )
            self.summary.set_text(
                f"{information.image_format}; {dimensions}; {information.colour_mode}{frames}\n"
                f"{format_file_size(information.file_size)}; modified {modified}\n{information.path}"
            )
            if information.privacy_entries:
                count = len(information.privacy_entries)
                noun = "field" if count == 1 else "fields"
                self.privacy_notice.set_text(
                    f"Potentially private location or identifying metadata was found in {count} {noun}."
                )
                self.privacy_notice.set_visible(True)
            else:
                self.privacy_notice.set_visible(False)
            if information.metadata_error:
                self.status.set_text(
                    f"Complete metadata could not be read: {information.metadata_error}"
                )
            elif information.exiftool_available:
                self.status.set_text(f"{len(information.entries)} metadata fields found.")
            else:
                self.status.set_text(
                    "ExifTool is unavailable. Basic image information is shown read-only; "
                    "install libimage-exiftool-perl for metadata access."
                )
            self._render_entries()
        self.set_busy(False)
        return GLib.SOURCE_REMOVE

    def _render_entries(self):
        child = self.metadata_list.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.metadata_list.remove(child)
            child = following
        entries = self.information.entries if self.information is not None else ()
        query = self.search.get_text().strip().casefold()
        displayed = [
            entry
            for entry in entries
            if not query or query in f"{entry.name} {entry.value}".casefold()
        ]
        if not displayed:
            label = "No matching metadata." if query else "No metadata is available."
            row = Gtk.ListBoxRow(selectable=False, activatable=False)
            empty = Gtk.Label(label=label, xalign=0, margin_top=10, margin_bottom=10)
            row.set_child(empty)
            self.metadata_list.append(row)
            return
        for entry in displayed:
            row = Gtk.ListBoxRow(selectable=False, activatable=False)
            box = Gtk.Box(
                spacing=12,
                margin_top=6,
                margin_bottom=6,
                margin_start=8,
                margin_end=8,
            )
            name = Gtk.Label(label=entry.name, xalign=0, yalign=0)
            name.set_width_chars(30)
            name.set_max_width_chars(30)
            name.set_ellipsize(Pango.EllipsizeMode.END)
            name.set_tooltip_text(entry.name)
            box.append(name)
            value = Gtk.Label(label=entry.value, xalign=0, yalign=0, selectable=True, wrap=True)
            value.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            value.set_hexpand(True)
            box.append(value)
            row.set_child(box)
            self.metadata_list.append(row)

    def set_busy(self, busy: bool, message: str | None = None):
        self._busy = busy
        if message is not None:
            self.status.set_text(message)
        writable = bool(self.information is not None and self.information.writable)
        for button in (
            self.edit_button,
            self.location_button,
            self.private_button,
            self.all_button,
        ):
            button.set_sensitive(writable and not busy)
        self.search.set_sensitive(not busy)

    def show_error(self, message: str):
        self._change_in_progress = False
        self.status.set_text(message)
        self.set_busy(False)

    def _edit(self, *_args):
        if self._busy or self.information is None or not self.information.writable:
            return
        if self._edit_window is not None:
            self._edit_window.present()
            return
        dialogue = MetadataEditDialogue(
            self,
            self.information.editable_values,
            self._review_edit,
        )
        self._edit_window = dialogue
        dialogue.connect("close-request", self._edit_closed)
        dialogue.connect("unrealize", self._edit_closed)
        dialogue.present()

    def _edit_closed(self, dialogue, *_args):
        if self._edit_window is dialogue:
            self._edit_window = None
        return False

    def _review_edit(self, values, changes, edit_window):
        details = f"File: {self.path}\n\n" + "\n".join(changes)
        self._open_confirmation(
            "Apply metadata changes?",
            details,
            "Apply changes",
            lambda: self._confirmed_action("edit", values, edit_window),
        )

    def _confirm_removal(self, scope):
        if self._busy or self.information is None or not self.information.writable:
            return
        descriptions = {
            "location": (
                "Remove location metadata?",
                "GPS coordinates and other recognised location fields will be removed.",
                "Remove location",
            ),
            "private": (
                "Remove private metadata?",
                "Location, owner, creator, comment, and device serial-number fields will be removed.",
                "Remove private data",
            ),
            "all": (
                "Remove removable metadata?",
                (
                    "EXIF, IPTC, XMP, comments, and other removable metadata will be removed. "
                    "ICC profiles and colour-space tags will be preserved where supported."
                ),
                "Remove metadata",
            ),
        }
        heading, explanation, label = descriptions[scope]
        details = f"File: {self.path}\n\n{explanation}"
        self._open_confirmation(
            heading,
            details,
            label,
            lambda: self._confirmed_action(scope, None, None),
            destructive=True,
        )

    def _open_confirmation(
        self,
        heading,
        details,
        confirm_label,
        on_confirm,
        *,
        destructive=False,
    ):
        if self._confirmation_window is not None:
            self._confirmation_window.present()
            return
        dialogue = MetadataConfirmationDialogue(
            self,
            heading,
            details,
            confirm_label,
            on_confirm,
            destructive=destructive,
        )
        self._confirmation_window = dialogue
        dialogue.connect("close-request", self._confirmation_closed)
        dialogue.connect("unrealize", self._confirmation_closed)
        dialogue.present_with_safe_focus()

    def _confirmation_closed(self, dialogue, *_args):
        if self._confirmation_window is dialogue:
            self._confirmation_window = None
        return False

    def _confirmed_action(self, action, values, edit_window):
        if edit_window is not None:
            edit_window.close()
        if self._confirmation_window is not None:
            self._confirmation_window.close()
        self._change_in_progress = True
        self.set_busy(True, "Preparing metadata change...")
        self._on_action(action, values)

    def _key_pressed(self, _controller, keyval, _keycode, _state):
        if keyval == Gdk.KEY_Escape and not self._change_in_progress:
            self.close()
            return True
        return False

    def _close_requested(self, *_args):
        if self._change_in_progress:
            return True
        self._shutdown_reader()
        return False

    def _unrealized(self, *_args):
        self._shutdown_reader()
        return False

    def _shutdown_reader(self):
        if not self._closed:
            self._closed = True
            self._executor.shutdown(wait=False, cancel_futures=True)

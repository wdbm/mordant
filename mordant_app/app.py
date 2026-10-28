"""
Mordant shared window for images, videos, browsing and original-file saves

Copyright (C) 2026 William Breaden Madden
SPDX-License-Identifier: GPL-3.0-or-later
"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
from threading import Event

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango

from .common_directories_dialogue import CommonDirectoriesDialogue
from .configuration import Configuration
from .configuration_dialogue import ConfigurationDialogue
from .crop import crop_image, suggested_crop_name
from .image_information import ImageInformationWindow
from .images import ImageView
from .media_clipboard import copy_file_to_clipboard
from .metadata import apply_metadata_edit, remove_metadata
from .media import (
    FILTERS, IMAGE_SUFFIXES, VIDEO_SUFFIXES, MediaLibrary,
    image_prefetch_candidates, media_kind,
)
from .save_dialogue import SaveDialogue
from .transfers import format_transfer_status
from .video import VideoView
from .video_trim import (
    TrimCancelled,
    TrimSettings,
    ffmpeg_path,
    suggested_trim_name,
    trim_video,
)

APP_ID = "io.github.wdbm.mordant"
CROP_ASPECT_PRESETS = (
    ("3:2", (3, 2)),
    ("4:3", (4, 3)),
    ("5:4", (5, 4)),
    ("1:1", (1, 1)),
    ("16:9", (16, 9)),
)
SHORTCUTS = """Browse
A / Z or Page Up / Page Down    Show the previous or next file
Left / Right                    Browse images, or seek videos backward or forward by 5 seconds
Shift+Left / Shift+Right        Seek videos backward or forward by 3 seconds
Ctrl+Left / Ctrl+Right          Seek videos backward or forward by 1 minute
Ctrl+O                          Open one or more media files
Ctrl+Shift+O                    Open a media directory
Ctrl+Alt+O                      Open media filenames common to multiple directories
F5                              Refresh the current media source

Save and organise
Ctrl+C                          Copy the current media file to the clipboard
S                               Open the save dialogue for the original media file
Shift+S                         Save the current video frame as a PNG screenshot
Ctrl+S                          Quick-save to the selected or most recent save directory
1-9, 0                          Quick-save to the corresponding indexed recent directory
Ctrl+Z                          Confirm undo of the latest file operation
Delete                          Move the current original file to the system Trash

View
Mouse wheel, +, =, -            Zoom an image at the pointer, or zoom in and out
Ctrl+0                          Fit the image to the window
Left-click and drag             Pan a zoomed image
Space                           Open the save dialogue for an image, or play or pause a video
P                               Play or pause a video or animated image
V / Shift+V                     Toggle or cycle video subtitles
F, F11, or double left-click    Toggle fullscreen mode
Tab                             Show or hide controls while in fullscreen mode
H                               Show or hide the path and status bar
F1 / ?                          Show the keyboard shortcuts window
Escape                          Leave fullscreen mode, or close

Crop mode
Drag inside the crop            Move the crop selection
Drag a crop corner              Resize the selection while preserving its aspect ratio
Rotate button                   Rotate the image counterclockwise by 90 degrees
Enter                           Prepare the crop and open the save dialogue
Escape                          Cancel cropping

Trim mode
Set start / Set end             Use the current playback position as a boundary
Left / Right                    Seek backward or forward by 5 seconds
Shift+Left / Shift+Right        Seek backward or forward by 3 seconds
Ctrl+Left / Ctrl+Right          Seek backward or forward by 1 minute
Enter                           Prepare the trim and open the save dialogue
Escape                          Cancel trimming or active FFmpeg processing

Save dialogue
Enter                           Save to all selected directories, except while typing in search
Escape                          Clear an active search while typing in it, otherwise close the save dialogue
1-9, 0                          Select or deselect the corresponding displayed directory
Alt+1-Alt+9, Alt+0              Save immediately to the corresponding displayed directory
Right-click a directory button  Offer to remove that entry from recent directories
"""


def is_text_input(widget):
    while widget is not None:
        if isinstance(widget, (Gtk.Entry, Gtk.Text, Gtk.TextView, Gtk.SpinButton)):
            return True
        widget = widget.get_parent()
    return False


class MordantWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="mordant")
        self.set_default_size(1080, 760)
        self.set_icon_name(APP_ID)
        self.library = MediaLibrary()
        self.configuration = app.configuration
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mordant-save")
        self.busy = False
        self.closed = False
        self._ready = False
        self._toast_job = 0
        self._cursor_job = 0
        self._inhibit_cookie = 0
        self._fullscreen_controls = False
        self._show_status = True
        self._dialogue = None
        self._chooser = None
        self._common_directories_window = None
        self._configuration_window = None
        self._image_information_window = None
        self._clipboard_provider = None
        self.crop_aspect = (3, 2)
        self._crop_temporary_directory = None
        self._crop_transfer_active = False
        self._crop_replaces_current = False
        self._trim_temporary_directory = None
        self._trim_cancel_event = None
        self._trim_transfer_active = False
        self._trim_replaces_current = False
        self.last_error = None
        self._display_path = None
        self.status_messages = app.status_messages

        self._build_header(app)
        outer = self._build_media_area()
        self._build_footer(outer)
        self._connect_controllers()
        self._ready = True
        self._update_ui()
        if self.configuration.history_error:
            self.show_error(self.configuration.history_error)

    def _build_header(self, app):
        self.header = Gtk.HeaderBar()
        self.set_titlebar(self.header)
        self.title_label = Gtk.Label(label="mordant", ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.title_label.set_max_width_chars(32)
        self.header.set_title_widget(self.title_label)
        self._build_open_menu()
        self.header.pack_start(self.open_menu_button)
        self.configuration_button = self._button(
            "Config...", self.open_configuration_dialogue,
            f"Choose configuration directory\nCurrent: {app.config_directory}",
        )
        self.header.pack_start(self.configuration_button)
        self.filter_widget = Gtk.DropDown.new_from_strings(FILTERS)
        self.filter_widget.set_tooltip_text("Show all media, only images, or only videos")
        filter_button = self.filter_widget.get_first_child()
        if isinstance(filter_button, Gtk.Button):
            filter_button.set_tooltip_text("Show all media, only images, or only videos")
        self.filter_widget.connect("notify::selected", self._filter_changed)
        self.header.pack_start(self.filter_widget)
        self._build_actions_menu()
        self.header.pack_end(self.actions_menu_button)
        self.fullscreen_button = self._button("", self.toggle_fullscreen, "Fullscreen (F11)")
        self.fullscreen_button.set_icon_name("view-fullscreen-symbolic")
        self.header.pack_end(self.fullscreen_button)
        self.header.pack_end(self._button("?", self.show_help, "Keyboard shortcuts (F1)"))
        self.save_button = self._button("Save...", self.open_save_dialogue, "Save the original file (S)")
        self.save_button.add_css_class("suggested-action")
        self.header.pack_end(self.save_button)
        self.undo_button = self._button("Undo", self.request_undo, "Undo the latest file operation (Ctrl+Z)")
        self.header.pack_end(self.undo_button)

    def _build_open_menu(self):
        self.open_menu_button = Gtk.MenuButton(label="Open")
        self.open_menu_button.set_tooltip_text(
            "Choose files or directories to open (Ctrl+O, Ctrl+Shift+O, or Ctrl+Alt+O)"
        )
        self.open_menu_popover = Gtk.Popover()
        menu = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=2,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
        self.open_menu_buttons = {}
        for label, callback, tooltip in (
            ("Open files", lambda: self.choose_media(), "Open media files (Ctrl+O)"),
            (
                "Open directory",
                lambda: self.choose_media(True),
                "Open a media directory (Ctrl+Shift+O)",
            ),
            (
                "Open common",
                lambda: self.open_common_directories(),
                "Open media filenames present in multiple directories (Ctrl+Alt+O)",
            ),
        ):
            button = self._button(
                label,
                lambda selected=callback: self._activate_open_menu_item(selected),
                tooltip,
            )
            button.add_css_class("flat")
            button.set_halign(Gtk.Align.FILL)
            button.get_child().set_halign(Gtk.Align.START)
            self.open_menu_buttons[label] = button
            menu.append(button)
        self.open_menu_popover.set_child(menu)
        self.open_menu_button.set_popover(self.open_menu_popover)

    def _activate_open_menu_item(self, callback):
        self.open_menu_popover.popdown()
        callback()

    def _build_actions_menu(self):
        self.actions_menu_button = Gtk.MenuButton()
        self.actions_menu_button.set_icon_name("open-menu-symbolic")
        self.actions_menu_button.set_tooltip_text("Menu")
        self.actions_menu_button.set_size_request(36, 36)
        self.actions_menu_popover = Gtk.Popover()
        menu = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=2,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
        self.copy_menu_button = self._button(
            "Copy file",
            self.copy_current_file,
            "Copy the current media file (Ctrl+C)",
        )
        self.copy_menu_button.add_css_class("flat")
        self.copy_menu_button.set_halign(Gtk.Align.FILL)
        self.copy_menu_button.get_child().set_halign(Gtk.Align.START)
        menu.append(self.copy_menu_button)
        self.crop_menu_button = self._button(
            "Crop and rotate",
            self.start_crop,
            "Crop or rotate the current still image",
        )
        self.crop_menu_button.add_css_class("flat")
        self.crop_menu_button.set_halign(Gtk.Align.FILL)
        self.crop_menu_button.get_child().set_halign(Gtk.Align.START)
        menu.append(self.crop_menu_button)
        self.trim_menu_button = self._button(
            "Trim video...",
            self.start_trim,
            "Select and save a portion of the current video",
        )
        self.trim_menu_button.add_css_class("flat")
        self.trim_menu_button.set_halign(Gtk.Align.FILL)
        self.trim_menu_button.get_child().set_halign(Gtk.Align.START)
        menu.append(self.trim_menu_button)
        self.information_menu_button = self._button(
            "Image information...",
            self.open_image_information,
            "View and manage metadata for the current image",
        )
        self.information_menu_button.add_css_class("flat")
        self.information_menu_button.set_halign(Gtk.Align.FILL)
        self.information_menu_button.get_child().set_halign(Gtk.Align.START)
        menu.append(self.information_menu_button)
        self.actions_menu_popover.set_child(menu)
        self.actions_menu_button.set_popover(self.actions_menu_popover)

    def copy_current_file(self):
        self.actions_menu_popover.popdown()
        path = self.library.current
        if path is None:
            self.show_error("No current media file is available to copy.")
            return False
        try:
            copied_path, provider = copy_file_to_clipboard(
                path,
                self.get_display().get_clipboard(),
            )
        except (OSError, RuntimeError) as error:
            self.show_error(f"Could not copy the media file: {error}")
            return False
        self._clipboard_provider = provider
        self.show_message(f"Copied {copied_path.name} to the clipboard.")
        return True

    def _build_media_area(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(outer)
        self.overlay = Gtk.Overlay(vexpand=True, hexpand=True)
        outer.append(self.overlay)
        self.stack = Gtk.Stack(vexpand=True, hexpand=True)
        self.stack.add_css_class("media-canvas")
        self.overlay.set_child(self.stack)
        self.empty = Gtk.Label(label="Open a directory or drop images and videos here.", wrap=True)
        self.empty.add_css_class("dim-label")
        self.stack.add_named(self.empty, "empty")
        self.images = ImageView(on_error=self.show_error, on_change=self._media_changed)
        self.video = VideoView(
            on_error=self.show_error,
            on_change=self._media_changed,
            on_ended=self._video_ended,
            on_trim_save=self.save_trim,
            on_trim_cancel=self.cancel_trim,
        )
        self.stack.add_named(self.images, "image")
        self.stack.add_named(self.video, "video")

        self.toast = Gtk.Label(wrap=True, selectable=True, visible=False)
        self.toast.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.toast.set_max_width_chars(78)
        self.toast.set_halign(Gtk.Align.CENTER)
        self.toast.set_valign(Gtk.Align.START)
        self.toast.set_margin_top(16)
        self.toast.add_css_class("mordant-toast")
        self.overlay.add_overlay(self.toast)

        return outer

    def _build_footer(self, outer):
        self.footer = Gtk.Box(spacing=8, margin_start=12, margin_end=12, margin_top=8, margin_bottom=8)
        outer.append(self.footer)
        self.previous_button = self._button("<", lambda: self.navigate(-1), "Previous file (A)")
        self.next_button = self._button(">", lambda: self.navigate(1), "Next file (Z)")
        self.footer.append(self.previous_button)
        self.position_label = Gtk.Label(label="0 / 0")
        self.footer.append(self.position_label)
        self.footer.append(self.next_button)
        self.path_label = Gtk.Label(hexpand=True, xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.path_label.set_max_width_chars(60)
        self.footer.append(self.path_label)
        self.image_controls = Gtk.Box(spacing=4)
        self.image_controls.append(self._button("-", lambda: self.images.zoom_at(1 / 1.2), "Zoom out"))
        self.image_controls.append(self._button("Fit", lambda: self.images.reset_zoom(), "Fit image (Ctrl+0)"))
        self.image_controls.append(self._button("+", lambda: self.images.zoom_at(1.2), "Zoom in"))
        self.pause_image_button = self._button("Pause", self.pause, "Play/pause animation (P)")
        self.image_controls.append(self.pause_image_button)
        self.footer.append(self.image_controls)
        self._build_crop_controls()
        self.footer.append(self.crop_controls)
        self.trash_button = self._button("Trash", self.trash_current, "Move original file to Trash (Delete)")
        self.footer.append(self.trash_button)

    def _build_crop_controls(self):
        self.crop_controls = Gtk.Box(spacing=6)
        self.crop_controls.append(Gtk.Label(label="Aspect:"))
        labels = [label for label, _aspect in CROP_ASPECT_PRESETS] + ["Custom..."]
        self.crop_aspect_dropdown = Gtk.DropDown.new_from_strings(labels)
        self.crop_aspect_dropdown.set_tooltip_text("Choose the crop aspect ratio")
        crop_aspect_button = self.crop_aspect_dropdown.get_first_child()
        if isinstance(crop_aspect_button, Gtk.Button):
            crop_aspect_button.set_tooltip_text("Choose the crop aspect ratio")
        self.crop_aspect_dropdown.connect("notify::selected", self._crop_aspect_selected)
        self.crop_controls.append(self.crop_aspect_dropdown)
        self.crop_width_spin = Gtk.SpinButton.new_with_range(0.001, 100, 1)
        self.crop_width_spin.set_value(3)
        self.crop_width_spin.set_digits(3)
        self.crop_width_spin.set_numeric(True)
        self.crop_width_spin.set_width_chars(7)
        self.crop_width_spin.set_tooltip_text("Custom crop width ratio")
        self.crop_height_spin = Gtk.SpinButton.new_with_range(0.001, 100, 1)
        self.crop_height_spin.set_value(2)
        self.crop_height_spin.set_digits(3)
        self.crop_height_spin.set_numeric(True)
        self.crop_height_spin.set_width_chars(7)
        self.crop_height_spin.set_tooltip_text("Custom crop height ratio")
        self.crop_ratio_separator = Gtk.Label(label=":")
        for spin in (self.crop_width_spin, self.crop_height_spin):
            spin.connect("value-changed", self._custom_crop_aspect_changed)
            child = spin.get_first_child()
            while child is not None:
                if isinstance(child, Gtk.Button):
                    child.set_tooltip_text("Adjust the custom crop aspect ratio")
                child = child.get_next_sibling()
            self.crop_controls.append(spin)
            if spin is self.crop_width_spin:
                self.crop_controls.append(self.crop_ratio_separator)
        self.crop_swap_button = self._button(
            "Swap",
            self.swap_crop_aspect,
            "Swap crop width and height",
        )
        self.crop_controls.append(self.crop_swap_button)
        self.crop_rotate_button = Gtk.Button(
            icon_name="object-rotate-left-symbolic",
            tooltip_text="Rotate counterclockwise 90 degrees",
        )
        self.crop_rotate_button.set_size_request(34, 34)
        self.crop_rotate_button.connect(
            "clicked",
            lambda *_: self.rotate_crop_counterclockwise(),
        )
        self.crop_controls.append(self.crop_rotate_button)
        self.crop_cancel_button = self._button(
            "Cancel",
            self.cancel_crop,
            "Cancel cropping (Escape)",
        )
        self.crop_controls.append(self.crop_cancel_button)
        self.crop_save_button = self._button(
            "Save crop...",
            self.save_crop,
            "Accept the crop and choose save directories",
        )
        self.crop_save_button.add_css_class("suggested-action")
        self.crop_controls.append(self.crop_save_button)
        self._show_custom_crop_aspect(False)

    def _connect_controllers(self):
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._key_pressed)
        self.add_controller(keys)
        click = Gtk.GestureClick()
        click.set_button(1)
        click.connect(
            "pressed",
            lambda gesture, count, x, y: (
                self.toggle_fullscreen()
                if count == 2 and not self.images.cropping and not self.video.trimming
                else None
            ),
        )
        self.stack.add_controller(click)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._pointer_moved)
        self.stack.add_controller(motion)
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._drop_files)
        self.add_controller(drop)
        self.connect("notify::fullscreened", self._fullscreen_changed)
        self.connect("close-request", self._close_requested)

    @staticmethod
    def _button(label, callback, tooltip):
        button = Gtk.Button(label=label, tooltip_text=tooltip)
        button.connect("clicked", lambda *_: callback())
        return button

    def _show_custom_crop_aspect(self, visible):
        self.crop_width_spin.set_visible(visible)
        self.crop_ratio_separator.set_visible(visible)
        self.crop_height_spin.set_visible(visible)

    def _crop_aspect_selected(self, *_args):
        selected = self.crop_aspect_dropdown.get_selected()
        custom = selected >= len(CROP_ASPECT_PRESETS)
        self._show_custom_crop_aspect(custom)
        if custom:
            aspect = (
                self.crop_width_spin.get_value(),
                self.crop_height_spin.get_value(),
            )
        else:
            aspect = CROP_ASPECT_PRESETS[selected][1]
        self._set_crop_aspect(aspect)

    def _custom_crop_aspect_changed(self, *_args):
        if self.crop_aspect_dropdown.get_selected() >= len(CROP_ASPECT_PRESETS):
            self._set_crop_aspect(
                (
                    self.crop_width_spin.get_value(),
                    self.crop_height_spin.get_value(),
                )
            )

    def _set_crop_aspect(self, aspect):
        self.crop_aspect = aspect
        if self.images.cropping:
            self.images.set_crop_aspect(aspect)

    def swap_crop_aspect(self):
        aspect = (self.crop_aspect[1], self.crop_aspect[0])
        for index, (_label, preset) in enumerate(CROP_ASPECT_PRESETS):
            if preset == aspect:
                self.crop_aspect_dropdown.set_selected(index)
                self._set_crop_aspect(aspect)
                return
        self.crop_width_spin.set_value(aspect[0])
        self.crop_height_spin.set_value(aspect[1])
        self.crop_aspect_dropdown.set_selected(len(CROP_ASPECT_PRESETS))
        self._set_crop_aspect(aspect)

    def start_crop(self):
        self.actions_menu_popover.popdown()
        if self.busy or self._dialogue is not None or not self.images.can_crop:
            return
        if self.images.begin_crop(self.crop_aspect):
            self._update_ui()

    def cancel_crop(self):
        if not self.images.cropping:
            return
        self.images.cancel_crop()
        self._update_ui()

    def rotate_crop_counterclockwise(self):
        if self.busy:
            return
        self.images.rotate_crop_counterclockwise()

    def save_crop(self):
        if self.busy or not self.images.cropping or self.library.current is None:
            return
        source = self.library.current
        rectangle = self.images.crop_rectangle
        aspect = self.crop_aspect
        rotation_quarters = self.images.crop_rotation_quarters
        self._cleanup_crop_temporary_directory()
        temporary_directory = tempfile.TemporaryDirectory(prefix="mordant-crop-")
        self._crop_temporary_directory = temporary_directory
        suggested_name = suggested_crop_name(source)
        output = Path(temporary_directory.name) / suggested_name
        self.busy = True
        self.show_message(f"Preparing {suggested_name}...", persistent=True)
        self._update_ui()
        future = self.executor.submit(
            crop_image,
            source,
            output,
            rectangle,
            aspect,
            rotation_quarters,
        )

        def completed(finished):
            GLib.idle_add(
                self._crop_rendered,
                source,
                suggested_name,
                finished,
            )

        future.add_done_callback(completed)

    def _crop_rendered(self, source, suggested_name, future):
        self.busy = False
        if self.closed:
            self._cleanup_crop_temporary_directory()
            return GLib.SOURCE_REMOVE
        try:
            output = future.result()
        except Exception as error:
            self._cleanup_crop_temporary_directory()
            self.show_error(f"Crop failed: {error}")
            self._update_ui()
            return GLib.SOURCE_REMOVE
        self.images.cancel_crop()
        self._update_ui()
        self._open_save_dialogue(
            output,
            on_save=self.perform_crop_transfer,
            suggested_filename=suggested_name,
            allow_move=False,
            browse_initial_directory=source.parent,
        )
        return GLib.SOURCE_REMOVE

    def perform_crop_transfer(self, request):
        current = self.library.current.resolve() if self.library.current is not None else None
        self._crop_replaces_current = bool(
            request.overwrite_requested
            and current is not None
            and any(
                (Path(directory) / request.name).resolve() == current
                for directory in request.destination_directories
            )
        )
        self._crop_transfer_active = True
        try:
            self.perform_transfer(request)
        except Exception:
            self._crop_transfer_active = False
            self._crop_replaces_current = False
            raise

    def _cleanup_crop_temporary_directory(self):
        temporary_directory = self._crop_temporary_directory
        self._crop_temporary_directory = None
        if temporary_directory is not None:
            temporary_directory.cleanup()

    def start_trim(self):
        self.actions_menu_popover.popdown()
        if self.busy or self._dialogue is not None or not self.video.can_trim:
            return
        if ffmpeg_path() is None:
            self.show_error("FFmpeg is required to trim videos but could not be found.")
            return
        if self.video.begin_trim():
            self._update_ui()

    def cancel_trim(self):
        if self._trim_cancel_event is not None:
            self._trim_cancel_event.set()
            self.video.trim_cancel_button.set_sensitive(False)
            self.show_message("Cancelling video trimming...", persistent=True)
            return
        self.video.cancel_trim()
        self._update_ui()

    def save_trim(self, settings: TrimSettings):
        source = self.library.current
        if (
            self.busy
            or not self.video.trimming
            or source is None
            or media_kind(source) != "video"
        ):
            return
        try:
            settings.validate(self.video.duration_us)
        except ValueError as error:
            self.show_error(str(error))
            return
        self._cleanup_trim_temporary_directory()
        temporary_directory = tempfile.TemporaryDirectory(prefix="mordant-trim-")
        self._trim_temporary_directory = temporary_directory
        suggested_name = suggested_trim_name(source, settings.reencode)
        output = Path(temporary_directory.name) / suggested_name
        cancel_event = Event()
        self._trim_cancel_event = cancel_event
        self.busy = True
        self.video.set_trim_processing(True)
        self.show_message(f"Preparing {suggested_name}: 0%", persistent=True)
        self._update_ui()

        def progress(value):
            GLib.idle_add(
                self._trim_progress,
                cancel_event,
                suggested_name,
                value,
            )

        future = self.executor.submit(
            trim_video,
            source,
            output,
            settings,
            cancel_event,
            progress,
        )

        def completed(finished):
            GLib.idle_add(
                self._trim_rendered,
                source,
                suggested_name,
                cancel_event,
                finished,
            )

        future.add_done_callback(completed)

    def _trim_progress(self, cancel_event, suggested_name, value):
        if self._trim_cancel_event is cancel_event and not cancel_event.is_set():
            percentage = min(100, max(0, round(value * 100)))
            self.show_message(
                f"Preparing {suggested_name}: {percentage}%",
                persistent=True,
            )
        return GLib.SOURCE_REMOVE

    def _trim_rendered(self, source, suggested_name, cancel_event, future):
        self.busy = False
        if self._trim_cancel_event is cancel_event:
            self._trim_cancel_event = None
        self.video.set_trim_processing(False)
        self.video.trim_cancel_button.set_sensitive(True)
        if self.closed:
            self._cleanup_trim_temporary_directory()
            return GLib.SOURCE_REMOVE
        try:
            output = future.result()
        except TrimCancelled:
            self._cleanup_trim_temporary_directory()
            self.show_message("Video trimming cancelled.")
            self._update_ui()
            return GLib.SOURCE_REMOVE
        except Exception as error:
            self._cleanup_trim_temporary_directory()
            self.show_error(f"Video trim failed: {error}")
            self._update_ui()
            return GLib.SOURCE_REMOVE
        if cancel_event.is_set():
            self._cleanup_trim_temporary_directory()
            self.show_message("Video trimming cancelled.")
            self._update_ui()
            return GLib.SOURCE_REMOVE
        self.video.cancel_trim()
        self._update_ui()
        self._open_save_dialogue(
            output,
            on_save=self.perform_trim_transfer,
            suggested_filename=suggested_name,
            allow_move=False,
            browse_initial_directory=source.parent,
        )
        return GLib.SOURCE_REMOVE

    def perform_trim_transfer(self, request):
        current = self.library.current.resolve() if self.library.current is not None else None
        self._trim_replaces_current = bool(
            request.overwrite_requested
            and current is not None
            and any(
                (Path(directory) / request.name).resolve() == current
                for directory in request.destination_directories
            )
        )
        self._trim_transfer_active = True
        try:
            self.perform_transfer(request)
        except Exception:
            self._trim_transfer_active = False
            self._trim_replaces_current = False
            raise

    def _cleanup_trim_temporary_directory(self):
        temporary_directory = self._trim_temporary_directory
        self._trim_temporary_directory = None
        if temporary_directory is not None:
            temporary_directory.cleanup()

    def open_image_information(self):
        self.actions_menu_popover.popdown()
        path = self.library.current
        if (
            self.busy
            or path is None
            or media_kind(path) != "image"
            or not path.is_file()
        ):
            return None
        if self._image_information_window is not None:
            if self._image_information_window.path == path.resolve():
                self._image_information_window.present()
                return self._image_information_window
            self._image_information_window.destroy()
        dialogue = ImageInformationWindow(
            self,
            path,
            lambda action, values: self.perform_metadata_action(
                dialogue,
                action,
                values,
            ),
        )
        self._image_information_window = dialogue
        dialogue.connect("close-request", self._image_information_closed)
        dialogue.connect("unrealize", self._image_information_closed)
        dialogue.present()
        return dialogue

    def _image_information_closed(self, dialogue, *_args):
        if self._image_information_window is dialogue:
            self._image_information_window = None
        return False

    def perform_metadata_action(self, dialogue, action, values=None):
        if self.busy or dialogue is not self._image_information_window:
            return
        path = dialogue.path
        self.busy = True
        dialogue.set_busy(True, "Applying metadata change...")
        self._update_ui()

        def modify(temporary_path):
            if action == "edit":
                apply_metadata_edit(temporary_path, values or {})
            else:
                remove_metadata(temporary_path, action)

        future = self.executor.submit(self.manager.modify_file, path, modify)
        future.add_done_callback(
            lambda completed: GLib.idle_add(
                self._metadata_action_finished,
                dialogue,
                path,
                completed,
            )
        )

    def _metadata_action_finished(self, dialogue, path, future):
        self.busy = False
        try:
            changed = future.result()
        except Exception as error:
            message = f"Metadata change failed: {error}"
            if dialogue is self._image_information_window:
                dialogue.show_error(message)
            self.show_error(message)
        else:
            if self.library.current is not None and self.library.current.resolve() == path:
                self.show_current()
            if dialogue is self._image_information_window:
                dialogue.refresh()
            if changed:
                self.show_message(f"Metadata updated for {path.name}.")
            else:
                self.show_message(f"No metadata changes were needed for {path.name}.")
        self._update_ui()
        return GLib.SOURCE_REMOVE

    def open_paths(self, paths):
        if self.busy or self.images.cropping or self.video.trimming:
            self.show_message("Wait for the current transfer to finish.")
            return
        try:
            errors = self.library.open(paths)
        except (ValueError, OSError) as error:
            self.show_error(str(error))
            return
        self.filter_widget.set_selected(0)
        self.show_current()
        if errors:
            self.show_error("\n".join(errors))

    def show_current(self):
        # A persistent playback error describes the file being left behind.
        # Clearing through show_message also cancels any pending toast timeout,
        # so it cannot later hide a message raised by the newly loaded file.
        self.last_error = None
        self.show_message("")
        self._display_path = self.library.current
        self.images.clear()
        self.video.clear()
        path = self.library.current
        if (
            self._image_information_window is not None
            and (path is None or self._image_information_window.path != path.resolve())
        ):
            self._image_information_window.destroy()
        if path is None:
            if self.library.intersection_source is not None and not self.library.paths:
                count = len(self.library.intersection_source.directories)
                self.empty.set_text(f"No media files are common to all {count} directories.")
            else:
                self.empty.set_text("No matching media. Open a directory or choose a different filter.")
            self.stack.set_visible_child_name("empty")
        elif not path.is_file():
            self.empty.set_text(f"File no longer exists:\n{path}\nPress F5 to refresh.")
            self.stack.set_visible_child_name("empty")
        else:
            kind = media_kind(path)
            self.stack.set_visible_child_name(kind)
            if kind == "image":
                self.images.load(path)
                self.images.prefetch(image_prefetch_candidates(self.library.visible, path))
            else:
                self.video.load(path)
        self._update_ui()
        self._media_changed()

    def navigate(self, delta):
        if (
            self.busy
            or self._dialogue is not None
            or self.images.cropping
            or self.video.trimming
        ):
            return
        self.library.step(delta)
        self.show_current()

    def _video_ended(self):
        visible = self.library.visible
        if self.library.current in visible and visible.index(self.library.current) + 1 < len(visible):
            self.navigate(1)

    def _filter_changed(self, *_):
        if not self._ready or self.busy or self.images.cropping or self.video.trimming:
            return
        self.library.set_filter(FILTERS[self.filter_widget.get_selected()])
        if self.library.current != self._display_path:
            self.show_current()
        else:
            self._update_ui()

    def refresh(self):
        if self.busy or self.images.cropping or self.video.trimming:
            return
        try:
            self.library.refresh()
            self.show_current()
        except OSError as error:
            self.show_error(str(error))

    def _update_ui(self):
        if not self._ready:
            return
        path = self.library.current
        cropping = self.images.cropping
        trimming = self.video.trimming
        visible = self.library.visible
        position = visible.index(path) + 1 if path in visible else 0
        self.position_label.set_text(f"{position} / {len(visible)}")
        self.title_label.set_text(path.name if path else "mordant")
        self.set_title(f"{path.name} - mordant" if path else "mordant")
        self.path_label.set_text(str(path) if path else "")
        self.path_label.set_tooltip_text(str(path) if path else "")
        normal_actions = not self.busy and not cropping and not trimming
        self.save_button.set_sensitive(
            normal_actions and (path is not None or bool(self.manager.undo_description()))
        )
        self.undo_button.set_sensitive(normal_actions and bool(self.manager.undo_description()))
        self.trash_button.set_sensitive(normal_actions and path is not None)
        self.previous_button.set_sensitive(normal_actions and bool(visible))
        self.next_button.set_sensitive(normal_actions and bool(visible))
        self.filter_widget.set_sensitive(normal_actions)
        self.configuration_button.set_sensitive(normal_actions and self._dialogue is None)
        self.open_menu_button.set_sensitive(normal_actions)
        self.actions_menu_button.set_sensitive(
            not self.busy and not cropping and not trimming
        )
        self.crop_menu_button.set_sensitive(
            normal_actions and self._dialogue is None and self.images.can_crop
        )
        self.copy_menu_button.set_sensitive(
            normal_actions
            and self._dialogue is None
            and bool(path and path.is_file())
        )
        self.trim_menu_button.set_sensitive(
            normal_actions
            and self._dialogue is None
            and self.video.can_trim
            and ffmpeg_path() is not None
        )
        self.information_menu_button.set_sensitive(
            normal_actions
            and self._dialogue is None
            and bool(path and path.is_file() and media_kind(path) == "image")
        )
        self.image_controls.set_visible(
            bool(path and media_kind(path) == "image" and not cropping)
        )
        self.crop_controls.set_visible(cropping)
        self.crop_controls.set_sensitive(not self.busy)
        self.pause_image_button.set_visible(self.images.is_animated)
        self.pause_image_button.set_label("Play" if self.images.paused else "Pause")
        self._apply_controls_visibility()

    def _media_changed(self):
        if not self._ready or self.closed:
            return
        playing = self.library.current is not None and media_kind(self.library.current) == "video" and self.video.playing
        app = self.get_application()
        if playing and not self._inhibit_cookie:
            self._inhibit_cookie = app.inhibit(self, Gtk.ApplicationInhibitFlags.IDLE, "Playing video")
        elif not playing and self._inhibit_cookie:
            app.uninhibit(self._inhibit_cookie)
            self._inhibit_cookie = 0
        self._update_ui()

    def pause(self):
        if self.library.current is None:
            return
        if media_kind(self.library.current) == "video":
            self.video.toggle_pause()
        else:
            self.images.toggle_pause()

    def show_message(self, message, persistent=False):
        if self.closed:
            return
        if self._toast_job:
            GLib.source_remove(self._toast_job)
            self._toast_job = 0
        self.toast.set_text(message)
        self.toast.set_visible(bool(message))
        if message and not persistent:
            self._toast_job = GLib.timeout_add(4500, self._hide_toast)

    def _hide_toast(self):
        self._toast_job = 0
        self.toast.set_visible(False)
        return GLib.SOURCE_REMOVE

    def show_error(self, message):
        self.last_error = str(message)
        print(f"mordant: {message}", file=sys.stderr)
        if self._ready:
            self.show_message(str(message), persistent=True)

    def choose_media(self, directory=False):
        if self.busy or self._chooser is not None or self._common_directories_window is not None:
            return
        chooser = Gtk.FileChooserNative.new(
            "Open directory" if directory else "Open images and videos", self,
            Gtk.FileChooserAction.SELECT_FOLDER if directory else Gtk.FileChooserAction.OPEN,
            "Open", "Cancel",
        )
        if not directory:
            chooser.set_select_multiple(True)
            supported = Gtk.FileFilter()
            supported.set_name("Images and videos")
            for suffix in sorted(IMAGE_SUFFIXES | VIDEO_SUFFIXES):
                supported.add_pattern(f"*{suffix}")
                supported.add_pattern(f"*{suffix.upper()}")
            chooser.add_filter(supported)
            any_file = Gtk.FileFilter()
            any_file.set_name("All files")
            any_file.add_pattern("*")
            chooser.add_filter(any_file)
        if self.library.current is not None:
            chooser.set_current_folder(Gio.File.new_for_path(str(self.library.current.parent)))
        def response(window, answer):
            if answer == Gtk.ResponseType.ACCEPT:
                files = window.get_files()
                paths = [files.get_item(i).get_path() for i in range(files.get_n_items())]
                self.open_paths([path for path in paths if path])
            window.destroy()
            self._chooser = None
        chooser.connect("response", response)
        self._chooser = chooser
        chooser.show()

    def open_common_directories(self):
        if self.busy or self._chooser is not None:
            return None
        if self._common_directories_window is not None:
            self._common_directories_window.present()
            return self._common_directories_window
        source = self.library.intersection_source
        initial_directories = source.directories if source is not None else ()
        if self.library.current is not None:
            initial_directory = self.library.current.parent
        elif self.library.directory is not None:
            initial_directory = self.library.directory
        elif source is not None:
            initial_directory = source.primary_directory
        else:
            initial_directory = None
        dialogue = CommonDirectoriesDialogue(
            self,
            self._open_common_selection,
            initial_directories=initial_directories,
            initial_directory=initial_directory,
        )
        self._common_directories_window = dialogue
        dialogue.connect("close-request", self._common_directories_closed)
        dialogue.connect("unrealize", self._common_directories_closed)
        dialogue.present()
        dialogue.focus_cancel_button()
        return dialogue

    def _common_directories_closed(self, dialogue, *_args):
        if self._common_directories_window is dialogue:
            self._common_directories_window = None
        return False

    def _open_common_selection(self, directories):
        if self.busy or self.images.cropping or self.video.trimming:
            return False
        try:
            self.library.open_intersection(directories)
        except (ValueError, OSError) as error:
            self.show_error(str(error))
            if self._common_directories_window is not None:
                self._common_directories_window.set_message(str(error))
            return False
        self.filter_widget.set_selected(0)
        self.show_current()
        file_count = len(self.library.paths)
        directory_count = len(self.library.intersection_source.directories)
        file_noun = "file" if file_count == 1 else "files"
        self.show_message(
            f"Opened {file_count} media {file_noun} common to {directory_count} directories."
        )
        return True

    @property
    def store(self):
        return self.configuration.store

    @property
    def session(self):
        return self.configuration.session

    @property
    def manager(self):
        return self.configuration.manager

    def open_configuration_dialogue(self):
        if self.busy or self._dialogue is not None:
            return None
        if self._configuration_window is None:
            dialogue = ConfigurationDialogue(self, self.configuration, self.switch_configuration)
            dialogue.connect("close-request", lambda *_: self._configuration_window_closed(dialogue))
            dialogue.connect("unrealize", lambda *_: self._configuration_window_closed(dialogue))
            self._configuration_window = dialogue
        self._configuration_window.present()
        return self._configuration_window

    def _configuration_window_closed(self, dialogue):
        if self._configuration_window is dialogue:
            self._configuration_window = None
        return False

    def switch_configuration(self, directory):
        if self.busy or self._dialogue is not None:
            return False
        try:
            directory = self.configuration.switch(directory)
        except (OSError, TypeError, ValueError) as error:
            self.show_error(f"Could not use configuration directory: {error}")
            return False
        self.configuration_button.set_tooltip_text(
            f"Choose configuration directory\nCurrent: {directory}"
        )
        if self._configuration_window is not None:
            self._configuration_window.destroy()
        self._update_ui()
        self.show_message(f"Configuration changed to {directory}")
        return True

    def _drop_files(self, target, value, x, y):
        if self.busy or self.images.cropping or self.video.trimming:
            return False
        paths = [file.get_path() for file in value.get_files() if file.get_path()]
        if paths:
            self.open_paths(paths)
        return bool(paths)

    def open_save_dialogue(self):
        return self._open_save_dialogue(self.library.current)

    def _open_save_dialogue(
        self,
        source_path,
        *,
        on_save=None,
        suggested_filename=None,
        allow_move=True,
        browse_initial_directory=None,
    ):
        if self.busy:
            return None
        if self._dialogue is not None:
            self._dialogue.present()
            return self._dialogue
        if source_path is None and not self.manager.undo_description():
            return None
        dialogue = SaveDialogue(
            self,
            self.session,
            self.manager,
            source_path,
            on_save or self.perform_transfer,
            self.after_undo,
            suggested_filename=suggested_filename,
            allow_move=allow_move,
            browse_initial_directory=browse_initial_directory,
        )
        self._dialogue = dialogue
        dialogue.connect("close-request", lambda *_: self._dialogue_closed(dialogue))
        dialogue.connect("unrealize", lambda *_: self._dialogue_closed(dialogue))
        dialogue.present()
        return dialogue

    def _dialogue_closed(self, dialogue):
        if self._dialogue is dialogue:
            self._dialogue = None
            if self._crop_temporary_directory is not None and not self._crop_transfer_active:
                self._cleanup_crop_temporary_directory()
            if self._trim_temporary_directory is not None and not self._trim_transfer_active:
                self._cleanup_trim_temporary_directory()
            self._update_ui()
        return False

    def request_undo(self):
        if not self.manager.undo_description() or self.busy:
            return
        dialogue = self.open_save_dialogue()
        if dialogue is not None:
            dialogue.request_undo()

    def after_undo(self, restored):
        if restored is not None:
            self.library.restore(restored)
            self.filter_widget.set_selected(FILTERS.index(self.library.filter))
        self.show_current()
        if self._image_information_window is not None:
            self._image_information_window.refresh()
        self.show_message(self.manager.last_undo_message or "Latest operation undone.")

    def quick_save(self, key=None):
        if (
            self.busy
            or self.images.cropping
            or self.video.trimming
            or self.library.current is None
        ):
            return
        if key is not None:
            destination = self.store.for_key(key, self.session.listing_mode)
            destinations = [destination] if destination else []
        else:
            destinations = self.session.selected_paths()
            if not destinations:
                recent = self.store.most_recent()
                destinations = [recent] if recent else []
        if not destinations:
            self.open_save_dialogue()
            return
        request = self.session.request(
            self.library.current, self.library.current.name, destinations,
            move_requested=False, overwrite_requested=False,
        )
        self.perform_transfer(request)

    def perform_transfer(self, request):
        if self.busy:
            raise ValueError("A transfer is already running.")
        self.busy = True
        self.session.set_selected(request.destination_directories)
        if request.move_requested:
            self.images.clear()
            self.video.clear()
        self.show_message(f"{'Moving' if request.move_requested else 'Saving'} {request.name}...", persistent=True)
        self._update_ui()
        future = self.executor.submit(self.manager.transfer, request)
        def completed(finished):
            GLib.idle_add(self._transfer_finished, request, finished)
        future.add_done_callback(completed)

    def _transfer_finished(self, request, future):
        self.busy = False
        crop_transfer = self._crop_transfer_active
        crop_replaces_current = self._crop_replaces_current
        trim_transfer = self._trim_transfer_active
        trim_replaces_current = self._trim_replaces_current
        self._crop_transfer_active = False
        self._crop_replaces_current = False
        self._trim_transfer_active = False
        self._trim_replaces_current = False
        result = None
        try:
            result = future.result()
        except Exception as error:
            if request.move_requested:
                self.show_current()
            self.show_error(f"Transfer failed: {error}")
        else:
            if (
                (
                    (crop_transfer and crop_replaces_current)
                    or (trim_transfer and trim_replaces_current)
                )
                and result.saved_directories
                and self.library.current is not None
                and any(
                    Path(directory).resolve() == self.library.current.parent.resolve()
                    for directory in result.saved_directories
                )
            ):
                self.show_current()
            if request.move_requested:
                if result.saved_directories and not result.errors:
                    self.library.remove(request.source_path)
                self.show_current()
            message = format_transfer_status(result, request.name, request.move_requested)
            if result.errors:
                details = [f"{path}: {error}" for path, error in result.errors]
                details.extend(result.warnings)
                self.show_error(message + "\n" + "\n".join(details))
            elif result.warnings:
                self.show_error(message + "\n" + "\n".join(result.warnings))
            elif self.status_messages:
                self.show_message(message)
            else:
                self._hide_toast()
        if crop_transfer:
            self._cleanup_crop_temporary_directory()
        if trim_transfer:
            self._cleanup_trim_temporary_directory()
        self._update_ui()
        return GLib.SOURCE_REMOVE

    def trash_current(self):
        if (
            self.busy
            or self.images.cropping
            or self.video.trimming
            or self.library.current is None
        ):
            return
        path = self.library.current
        try:
            self.images.clear()
            self.video.clear()
            Gio.File.new_for_path(str(path)).trash(None)
        except GLib.Error as error:
            self.show_current()
            self.show_error(f"Could not move file to Trash: {error.message}")
            return
        self.library.remove(path)
        self.show_current()
        self.show_message(f"Moved {path.name} to Trash. Restore it using your file manager.")

    def screenshot(self):
        if self.library.current is None or media_kind(self.library.current) != "video":
            return
        try:
            output = self.video.capture_screenshot()
            self.show_message(f"Screenshot saved: {output}")
        except (OSError, ValueError, RuntimeError, GLib.Error) as error:
            self.show_error(f"Screenshot failed: {error}")

    def toggle_fullscreen(self):
        self.unfullscreen() if self.props.fullscreened else self.fullscreen()

    def _fullscreen_changed(self, *_):
        self._fullscreen_controls = False
        self._apply_controls_visibility()
        self._pointer_moved(None, 0, 0)

    def _apply_controls_visibility(self):
        show = not self.props.fullscreened or self._fullscreen_controls
        self.header.set_visible(show)
        self.footer.set_visible(show)
        self.path_label.set_visible(self._show_status)
        self.video.set_controls_visible(show)

    def _pointer_moved(self, *_):
        self.stack.set_cursor(None)
        if self._cursor_job:
            GLib.source_remove(self._cursor_job)
            self._cursor_job = 0
        if self.props.fullscreened and not self._fullscreen_controls:
            self._cursor_job = GLib.timeout_add(3000, self._hide_cursor)

    def _hide_cursor(self):
        self._cursor_job = 0
        self.stack.set_cursor_from_name("none")
        return GLib.SOURCE_REMOVE

    def show_help(self):
        dialogue = Gtk.Window(title="Keyboard shortcuts", transient_for=self, modal=True)
        dialogue.set_default_size(650, 650)
        dialogue.set_destroy_with_parent(True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=16, margin_bottom=16, margin_start=16, margin_end=16)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        label = Gtk.Label(xalign=0, yalign=0, selectable=False, wrap=False)
        label.set_focusable(False)
        label.set_markup(
            f'<span font_family="monospace" size="x-small">'
            f"{GLib.markup_escape_text(SHORTCUTS)}</span>"
        )
        scroller.set_child(label)
        box.append(scroller)
        close_button = self._button("Close", dialogue.destroy, "Close help")
        box.append(close_button)
        dialogue.set_child(box)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", lambda c, key, code, state: (dialogue.destroy() or True) if key == Gdk.KEY_Escape else False)
        dialogue.add_controller(keys)
        dialogue.present()
        close_button.grab_focus()

    def _key_pressed(self, controller, key, code, state):
        text_input = is_text_input(self.get_focus())
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        alt = bool(state & Gdk.ModifierType.ALT_MASK)
        char = chr(Gdk.keyval_to_unicode(key)).lower() if Gdk.keyval_to_unicode(key) else ""
        if ctrl and not alt and char == "c" and not text_input:
            self.copy_current_file()
            return True
        if self.images.cropping:
            if key == Gdk.KEY_Escape:
                self.cancel_crop()
                return True
            if key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and not text_input:
                self.save_crop()
                return True
            return False if text_input else True
        if self.video.trimming:
            if key == Gdk.KEY_Escape:
                self.cancel_trim()
                return True
            if text_input:
                return False
            if self.busy:
                return True
            shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
            if key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                self.video.request_trim_save()
            elif key == Gdk.KEY_space or char == "p":
                self.pause()
            elif key in (Gdk.KEY_Left, Gdk.KEY_Right):
                delta = -1 if key == Gdk.KEY_Left else 1
                self.video.seek_relative(delta * (60 if ctrl else 3 if shift else 5))
            elif key == Gdk.KEY_Tab:
                return False
            else:
                return True
            return True
        if text_input:
            return False
        if self.busy:
            return True
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        path = self.library.current
        video = path is not None and media_kind(path) == "video"
        if key == Gdk.KEY_Escape:
            self.unfullscreen() if self.props.fullscreened else self.close()
        elif ctrl and alt and char == "o":
            self.open_common_directories()
        elif ctrl and char == "o":
            self.choose_media(shift)
        elif ctrl and char == "s":
            self.quick_save()
        elif ctrl and char == "z":
            self.request_undo()
        elif ctrl and key in (Gdk.KEY_0, Gdk.KEY_KP_0):
            self.images.reset_zoom()
        elif char == "s":
            self.screenshot() if shift else self.open_save_dialogue()
        elif key == Gdk.KEY_space:
            self.pause() if video else self.open_save_dialogue()
        elif char == "p":
            self.pause()
        elif char == "a" or key == Gdk.KEY_Page_Up:
            self.navigate(-1)
        elif char == "z" or key == Gdk.KEY_Page_Down:
            self.navigate(1)
        elif key in (Gdk.KEY_Left, Gdk.KEY_Right):
            delta = -1 if key == Gdk.KEY_Left else 1
            self.video.seek_relative(delta * (60 if ctrl else 3 if shift else 5)) if video else self.navigate(delta)
        elif key in (Gdk.KEY_plus, Gdk.KEY_equal, Gdk.KEY_KP_Add):
            self.images.zoom_at(1.2)
        elif key in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract):
            self.images.zoom_at(1 / 1.2)
        elif char == "v" and video:
            self.video.cycle_subtitles() if shift else self.video.toggle_subtitles()
        elif key == Gdk.KEY_F11 or char == "f":
            self.toggle_fullscreen()
        elif key == Gdk.KEY_Tab and self.props.fullscreened:
            self._fullscreen_controls = not self._fullscreen_controls
            self._apply_controls_visibility()
            self._pointer_moved(None, 0, 0)
        elif char == "h":
            self._show_status = not self._show_status
            self._update_ui()
        elif key == Gdk.KEY_F1 or char == "?":
            self.show_help()
        elif key == Gdk.KEY_F5:
            self.refresh()
        elif key == Gdk.KEY_Delete:
            self.trash_current()
        elif char in "0123456789" and char and not ctrl:
            self.quick_save(int(char))
        else:
            return False
        return True

    def _close_requested(self, *_):
        if self.busy:
            if self._trim_cancel_event is not None:
                self.cancel_trim()
                self.show_message(
                    "Cancelling video trimming. Close the window once cancellation completes.",
                    persistent=True,
                )
            else:
                self.show_message(
                    "Finishing the current file operation. Close the window once it completes.",
                    persistent=True,
                )
            return True
        self.shutdown()
        return False

    def shutdown(self):
        if self.closed:
            return
        self.closed = True
        if self._trim_cancel_event is not None:
            self._trim_cancel_event.set()
        self.images.close()
        self.video.close()
        self.configuration.close()
        self.executor.shutdown(wait=False)
        self._cleanup_crop_temporary_directory()
        if self._trim_cancel_event is None:
            self._cleanup_trim_temporary_directory()
        for job in (self._toast_job, self._cursor_job):
            if job:
                GLib.source_remove(job)
        self._toast_job = self._cursor_job = 0
        if self._inhibit_cookie:
            self.get_application().uninhibit(self._inhibit_cookie)
            self._inhibit_cookie = 0
        if self._chooser is not None:
            self._chooser.destroy()
        if self._common_directories_window is not None:
            self._common_directories_window.destroy()
            self._common_directories_window = None
        if self._configuration_window is not None:
            self._configuration_window.destroy()
            self._configuration_window = None
        if self._image_information_window is not None:
            self._image_information_window.destroy()
            self._image_information_window = None


class MordantApplication(Gtk.Application):
    def __init__(self, config_directory=None, initial_paths=None, status_messages=True,
                 application_id=APP_ID, configuration_history_file=None,
                 single_instance=False, max_recents=None):
        flags = Gio.ApplicationFlags.HANDLES_OPEN
        if not single_instance:
            flags |= Gio.ApplicationFlags.NON_UNIQUE
        super().__init__(application_id=application_id, flags=flags)
        self.configuration = Configuration(
            config_directory,
            configuration_history_file,
            max_recents=max_recents,
        )
        self.initial_paths = initial_paths
        self.status_messages = status_messages

    @property
    def config_directory(self):
        return self.configuration.directory

    def do_startup(self):
        Gtk.Application.do_startup(self)
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)
        css = Gtk.CssProvider()
        css.load_from_data(b"""
            .media-canvas { background: #111216; color: #eeeeee; }
            .mordant-toast { background: alpha(#15171c, 0.95); color: #ffffff;
                padding: 12px 18px; border-radius: 10px; }
            .subtitle-overlay { background: alpha(#000000, 0.65); color: #ffffff;
                font-size: 24px; padding: 8px 14px; border-radius: 8px; }
            .save-dialogue .recent-directory-button {
                background-image: none; background-color: #25272d; color: #f4f5f7;
            }
            .save-dialogue .recent-directory-button:checked {
                background-image: none; background-color: #f1f3f5; color: #17191c;
            }
            .save-dialogue .directory-path { font-size: 0.78em; color: #aeb3bd; }
            .save-dialogue .recent-directory-button:checked .directory-path { color: #41454b; }
        """)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def window(self):
        windows = [window for window in self.get_windows() if isinstance(window, MordantWindow)]
        return windows[0] if windows else MordantWindow(self)

    def do_activate(self):
        window = self.window()
        window.present()
        if self.initial_paths is not None:
            paths, self.initial_paths = self.initial_paths, None
            window.open_paths(paths)

    def do_open(self, files, count, hint):
        window = self.window()
        window.present()
        window.open_paths([file.get_path() for file in files if file.get_path()])

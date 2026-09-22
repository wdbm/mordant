"""
Mordant shared window for images, videos, browsing and original-file saves

Copyright (C) 2026 William Breaden Madden
SPDX-License-Identifier: GPL-3.0-or-later
"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango

from .configuration import Configuration
from .configuration_dialogue import ConfigurationDialogue
from .images import ImageView
from .media import (
    FILTERS, IMAGE_SUFFIXES, VIDEO_SUFFIXES, MediaLibrary,
    image_prefetch_candidates, media_kind,
)
from .save_dialogue import SaveDialogue
from .transfers import format_transfer_status
from .video import VideoView

APP_ID = "io.github.wdbm.mordant"
SHORTCUTS = """Browse
A / Z or Page Up / Page Down    Show the previous or next file
Left / Right                    Browse images, or seek videos backward or forward by 5 seconds
Shift+Left / Shift+Right        Seek videos backward or forward by 3 seconds
Ctrl+Left / Ctrl+Right          Seek videos backward or forward by 1 minute
Ctrl+O                          Open one or more media files
Ctrl+Shift+O                    Open a media directory
F5                              Refresh the current directory or explicit file list

Save and organise
S                               Open the save dialogue for the original media file
Shift+S                         Save the current video frame as a PNG screenshot
Ctrl+S                          Quick-save to the selected or most recent save directory
1-9, 0                          Quick-save to the corresponding indexed recent directory
Ctrl+Z                          Confirm undo of the latest save or move operation
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
        self._configuration_window = None
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
        self.open_files_button = self._button(
            "Open files", lambda: self.choose_media(), "Open media files (Ctrl+O)",
        )
        self.open_directory_button = self._button(
            "Open directory", lambda: self.choose_media(True), "Open a media directory (Ctrl+Shift+O)",
        )
        self.header.pack_start(self.open_files_button)
        self.header.pack_start(self.open_directory_button)
        self.configuration_button = self._button(
            "Config...", self.open_configuration_dialogue,
            f"Choose configuration directory\nCurrent: {app.config_directory}",
        )
        self.header.pack_start(self.configuration_button)
        self.filter_widget = Gtk.DropDown.new_from_strings(FILTERS)
        self.filter_widget.set_tooltip_text("Show all media, only images, or only videos")
        self.filter_widget.connect("notify::selected", self._filter_changed)
        self.header.pack_start(self.filter_widget)
        fullscreen_button = self._button("", self.toggle_fullscreen, "Fullscreen (F11)")
        fullscreen_button.set_icon_name("view-fullscreen-symbolic")
        self.header.pack_end(fullscreen_button)
        self.header.pack_end(self._button("?", self.show_help, "Keyboard shortcuts (F1)"))
        self.save_button = self._button("Save...", self.open_save_dialogue, "Save the original file (S)")
        self.save_button.add_css_class("suggested-action")
        self.header.pack_end(self.save_button)
        self.undo_button = self._button("Undo", self.request_undo, "Undo the latest transfer (Ctrl+Z)")
        self.header.pack_end(self.undo_button)

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
        self.video = VideoView(on_error=self.show_error, on_change=self._media_changed,
                               on_ended=self._video_ended)
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
        self.trash_button = self._button("Trash", self.trash_current, "Move original file to Trash (Delete)")
        self.footer.append(self.trash_button)

    def _connect_controllers(self):
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._key_pressed)
        self.add_controller(keys)
        click = Gtk.GestureClick()
        click.set_button(1)
        click.connect("pressed", lambda gesture, count, x, y: self.toggle_fullscreen() if count == 2 else None)
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

    def open_paths(self, paths):
        if self.busy:
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
        if path is None:
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
        if self.busy or self._dialogue is not None:
            return
        self.library.step(delta)
        self.show_current()

    def _video_ended(self):
        visible = self.library.visible
        if self.library.current in visible and visible.index(self.library.current) + 1 < len(visible):
            self.navigate(1)

    def _filter_changed(self, *_):
        if not self._ready or self.busy:
            return
        self.library.set_filter(FILTERS[self.filter_widget.get_selected()])
        if self.library.current != self._display_path:
            self.show_current()
        else:
            self._update_ui()

    def refresh(self):
        if self.busy:
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
        visible = self.library.visible
        position = visible.index(path) + 1 if path in visible else 0
        self.position_label.set_text(f"{position} / {len(visible)}")
        self.title_label.set_text(path.name if path else "mordant")
        self.set_title(f"{path.name} - mordant" if path else "mordant")
        self.path_label.set_text(str(path) if path else "")
        self.path_label.set_tooltip_text(str(path) if path else "")
        self.save_button.set_sensitive(not self.busy and (path is not None or bool(self.manager.undo_description())))
        self.undo_button.set_sensitive(not self.busy and bool(self.manager.undo_description()))
        self.trash_button.set_sensitive(not self.busy and path is not None)
        self.previous_button.set_sensitive(not self.busy and bool(visible))
        self.next_button.set_sensitive(not self.busy and bool(visible))
        self.filter_widget.set_sensitive(not self.busy)
        self.configuration_button.set_sensitive(not self.busy and self._dialogue is None)
        self.image_controls.set_visible(media_kind(path) == "image" if path else False)
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
        if self.busy or self._chooser is not None:
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
        if self.busy:
            return False
        paths = [file.get_path() for file in value.get_files() if file.get_path()]
        if paths:
            self.open_paths(paths)
        return bool(paths)

    def open_save_dialogue(self):
        if self.busy:
            return None
        if self._dialogue is not None:
            self._dialogue.present()
            return self._dialogue
        if self.library.current is None and not self.manager.undo_description():
            return None
        dialogue = SaveDialogue(self, self.session, self.manager, self.library.current,
                            self.perform_transfer, self.after_undo)
        self._dialogue = dialogue
        dialogue.connect("close-request", lambda *_: self._dialogue_closed(dialogue))
        dialogue.connect("unrealize", lambda *_: self._dialogue_closed(dialogue))
        dialogue.present()
        return dialogue

    def _dialogue_closed(self, dialogue):
        if self._dialogue is dialogue:
            self._dialogue = None
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
        self.show_message("Last transfer undone.")

    def quick_save(self, key=None):
        if self.busy or self.library.current is None:
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
        try:
            result = future.result()
        except Exception as error:
            if request.move_requested:
                self.show_current()
            self.show_error(f"Transfer failed: {error}")
        else:
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
        self._update_ui()
        return GLib.SOURCE_REMOVE

    def trash_current(self):
        if self.busy or self.library.current is None:
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
        if is_text_input(self.get_focus()):
            return False
        if self.busy:
            return True
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        char = chr(Gdk.keyval_to_unicode(key)).lower() if Gdk.keyval_to_unicode(key) else ""
        path = self.library.current
        video = path is not None and media_kind(path) == "video"
        if key == Gdk.KEY_Escape:
            self.unfullscreen() if self.props.fullscreened else self.close()
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
            self.show_message("Finishing the current transfer. Close the window once it completes.", persistent=True)
            return True
        self.shutdown()
        return False

    def shutdown(self):
        if self.closed:
            return
        self.closed = True
        self.images.close()
        self.video.close()
        self.configuration.close()
        self.executor.shutdown(wait=False)
        for job in (self._toast_job, self._cursor_job):
            if job:
                GLib.source_remove(job)
        self._toast_job = self._cursor_job = 0
        if self._inhibit_cookie:
            self.get_application().uninhibit(self._inhibit_cookie)
            self._inhibit_cookie = 0
        if self._chooser is not None:
            self._chooser.destroy()
        if self._configuration_window is not None:
            self._configuration_window.destroy()
            self._configuration_window = None


class MordantApplication(Gtk.Application):
    def __init__(self, config_directory=None, initial_paths=None, status_messages=True,
                 application_id=APP_ID, configuration_history_file=None,
                 single_instance=False):
        flags = Gio.ApplicationFlags.HANDLES_OPEN
        if not single_instance:
            flags |= Gio.ApplicationFlags.NON_UNIQUE
        super().__init__(application_id=application_id, flags=flags)
        self.configuration = Configuration(config_directory, configuration_history_file)
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

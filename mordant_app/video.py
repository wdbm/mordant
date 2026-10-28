"""
Mordant GTK video playback and frame capture

Copyright (C) 2026 William Breaden Madden
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
import unicodedata

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
from gi.repository import Gdk, GLib, Gsk, Gtk  # Gsk registers snapshot node types.

from .subtitles import (
    find_subtitle_candidates,
    read_subtitle_entries,
    subtitle_text_at,
)
from .video_trim import TrimSettings, format_video_time, parse_video_time

UPDATE_INTERVAL_MS = 100


def format_us(value: int) -> str:
    total_seconds = max(0, value) // 1_000_000
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


def video_position_slug(position_us: int) -> str:
    total_seconds, milliseconds = divmod(max(0, position_us) // 1000, 1000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}h{minutes:02d}m{seconds:02d}s{milliseconds:03d}ms"


def build_screenshot_path(video_path: Path, position_us: int, now: datetime | None = None) -> Path:
    now = now or datetime.now(timezone.utc)
    prefix = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    normalised = unicodedata.normalize("NFKD", video_path.name).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", normalised.lower()).strip("-") or "video"
    # Leave enough room for the timestamp, position, and collision suffix.
    base = f"{prefix}_{slug[:140]}_{video_position_slug(position_us)}"
    candidate = video_path.parent / f"{base}.png"
    counter = 2
    while candidate.exists() or candidate.is_symlink():
        candidate = video_path.parent / f"{base}_{counter}.png"
        counter += 1
    return candidate


class VideoView(Gtk.Box):
    """
    An independently disposable player; navigation belongs to the shell.

    Callbacks are on_error(message), on_change(), and on_ended(). All run on the
    GTK main thread. on_ended fires only with repeat disabled. seek_relative uses
    seconds; position_us and duration_us retain GTK's native microsecond units.
    """

    def __init__(
        self,
        on_error=None,
        on_change=None,
        on_ended=None,
        on_trim_save=None,
        on_trim_cancel=None,
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.on_error = on_error
        self.on_change = on_change
        self.on_ended = on_ended
        self.on_trim_save = on_trim_save
        self.on_trim_cancel = on_trim_cancel
        self.current_path = None
        self.media_stream = None
        self._signal_ids = []
        self._tick_id = 0
        self._surface = None
        self._repeat = True
        self.volume = 1.0
        self.position_us = 0
        self.duration_us = 0
        self._updating_progress = False
        self._trimming = False
        self._trim_processing = False
        self._trim_previewing = False
        self.subtitles_enabled = False
        self.subtitle_tracks = []
        self.subtitle_track_index = -1
        self._subtitle_entries = []
        self._subtitle_starts = []
        self._subtitle_ends = []

        self.overlay = Gtk.Overlay(hexpand=True, vexpand=True)
        self.overlay.add_css_class("mordant-video")
        self.append(self.overlay)
        self.picture = Gtk.Picture(hexpand=True, vexpand=True, can_shrink=True)
        self.picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.overlay.set_child(self.picture)
        self.subtitle_label = Gtk.Label(
            wrap=True, justify=Gtk.Justification.CENTER,
            halign=Gtk.Align.CENTER, valign=Gtk.Align.END,
            margin_start=36, margin_end=36, margin_bottom=24,
        )
        self.subtitle_label.add_css_class("mordant-subtitle")
        self.subtitle_label.set_can_target(False)
        self.subtitle_label.set_visible(False)
        self.overlay.add_overlay(self.subtitle_label)

        self.controls = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=4,
            margin_start=12, margin_end=12, margin_top=4, margin_bottom=8,
        )
        self.append(self.controls)
        self.progress_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 1.0, 0.1)
        self.progress_scale.set_draw_value(False)
        self.progress_scale.set_tooltip_text("Seek within the current video")
        self.progress_scale.set_sensitive(False)
        self.progress_scale.connect("value-changed", self._on_progress_changed)
        self.controls.append(self.progress_scale)
        transport = Gtk.Box(spacing=8)
        self.controls.append(transport)
        self.play_button = Gtk.Button(icon_name="media-playback-start-symbolic", tooltip_text="Play / pause (Space)")
        self.play_button.connect("clicked", lambda _button: self.toggle_pause())
        self.play_button.set_sensitive(False)
        transport.append(self.play_button)
        self.time_label = Gtk.Label(label="00:00 / 00:00", xalign=0, hexpand=True)
        transport.append(self.time_label)
        self.subtitle_button = Gtk.Button(label="Subtitles", tooltip_text="Subtitles on / off (V); next track (Shift+V)")
        self.subtitle_button.connect("clicked", lambda _button: self.toggle_subtitles())
        transport.append(self.subtitle_button)
        self.repeat_button = Gtk.ToggleButton(label="Loop", active=True, tooltip_text="Repeat current video")
        self.repeat_button.connect("toggled", self._on_repeat_toggled)
        transport.append(self.repeat_button)
        self.volume_button = Gtk.VolumeButton()
        self.volume_button.set_tooltip_text("Adjust video volume")
        self._set_volume_button_tooltips()
        self.volume_button.set_value(self.volume)
        self.volume_button.connect("value-changed", self._on_volume_changed)
        transport.append(self.volume_button)
        self._build_trim_controls()

        self.connect("realize", self._on_realize)
        self.connect("unrealize", self._on_unrealize)
        provider = Gtk.CssProvider()
        provider.load_from_data(b"""
            .mordant-video { background: black; }
            .mordant-subtitle {
                color: white; background: alpha(black, 0.65);
                font-size: 24px; font-weight: bold;
                padding: 8px 14px; border-radius: 8px;
            }
        """)
        self._css_provider = provider
        Gtk.StyleContext.add_provider_for_display(self.get_display(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _build_trim_controls(self):
        self.trim_range = Gtk.DrawingArea(height_request=8, hexpand=True)
        self.trim_range.set_draw_func(self._draw_trim_range)
        self.trim_range.set_tooltip_text("Selected portion of the video")
        self.controls.insert_child_after(self.trim_range, self.progress_scale)
        self.trim_range.set_visible(False)
        self.trim_controls = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
            margin_top=4,
        )
        self.controls.append(self.trim_controls)
        times = Gtk.Box(spacing=6)
        self.trim_controls.append(times)
        times.append(Gtk.Label(label="Start:"))
        self.trim_start_entry = Gtk.Entry(width_chars=12)
        self.trim_start_entry.set_tooltip_text("Trim start as seconds, MM:SS, or HH:MM:SS")
        self.trim_start_entry.connect("changed", self._trim_time_changed)
        times.append(self.trim_start_entry)
        self.trim_set_start_button = Gtk.Button(label="Set start", tooltip_text="Use the current playback position as the trim start")
        self.trim_set_start_button.connect("clicked", lambda _button: self._set_trim_boundary(True))
        times.append(self.trim_set_start_button)
        times.append(Gtk.Label(label="End:"))
        self.trim_end_entry = Gtk.Entry(width_chars=12)
        self.trim_end_entry.set_tooltip_text("Trim end as seconds, MM:SS, or HH:MM:SS")
        self.trim_end_entry.connect("changed", self._trim_time_changed)
        times.append(self.trim_end_entry)
        self.trim_set_end_button = Gtk.Button(label="Set end", tooltip_text="Use the current playback position as the trim end")
        self.trim_set_end_button.connect("clicked", lambda _button: self._set_trim_boundary(False))
        times.append(self.trim_set_end_button)

        actions = Gtk.FlowBox()
        actions.set_selection_mode(Gtk.SelectionMode.NONE)
        actions.set_column_spacing(6)
        actions.set_row_spacing(6)
        actions.set_min_children_per_line(1)
        actions.set_max_children_per_line(8)
        self.trim_controls.append(actions)
        self.trim_preview_button = Gtk.Button(label="Play selection", tooltip_text="Play the selected portion of the video")
        self.trim_preview_button.connect("clicked", self._preview_trim)
        actions.append(self.trim_preview_button)
        self.trim_mode_dropdown = Gtk.DropDown.new_from_strings(("Fast copy", "Accurate re-encode"))
        self.trim_mode_dropdown.set_tooltip_text("Copy streams quickly or re-encode for precise boundaries")
        actions.append(self.trim_mode_dropdown)
        self.trim_audio_button = Gtk.CheckButton(label="Include audio", active=True)
        self.trim_audio_button.set_tooltip_text("Retain the first audio stream in the trimmed video")
        actions.append(self.trim_audio_button)
        self.trim_reset_button = Gtk.Button(label="Reset", tooltip_text="Select the complete video")
        self.trim_reset_button.connect("clicked", lambda _button: self._reset_trim_range())
        actions.append(self.trim_reset_button)
        self.trim_cancel_button = Gtk.Button(label="Cancel", tooltip_text="Cancel video trimming")
        self.trim_cancel_button.connect("clicked", self._request_trim_cancel)
        actions.append(self.trim_cancel_button)
        self.trim_save_button = Gtk.Button(label="Save trim...", tooltip_text="Prepare the selected video portion and choose save directories")
        self.trim_save_button.add_css_class("suggested-action")
        self.trim_save_button.connect("clicked", self._save_trim)
        actions.append(self.trim_save_button)
        self._trim_input_widgets = (
            self.trim_start_entry,
            self.trim_set_start_button,
            self.trim_end_entry,
            self.trim_set_end_button,
            self.trim_preview_button,
            self.trim_mode_dropdown,
            self.trim_audio_button,
            self.trim_reset_button,
            self.trim_save_button,
        )
        self.trim_controls.set_visible(False)

    def _set_volume_button_tooltips(self) -> None:
        tooltips = {
            "list-add-symbolic": "Increase video volume",
            "list-remove-symbolic": "Decrease video volume",
        }
        pending = [self.volume_button.get_first_child()]
        while pending:
            widget = pending.pop()
            if widget is None:
                continue
            sibling = widget.get_next_sibling()
            child = widget.get_first_child()
            if sibling is not None:
                pending.append(sibling)
            if child is not None:
                pending.append(child)
            if isinstance(widget, Gtk.Button):
                tooltip = tooltips.get(widget.get_icon_name(), "Adjust video volume")
                widget.set_tooltip_text(tooltip)

    @property
    def playing(self) -> bool:
        return bool(self.media_stream and self.media_stream.get_playing())

    @property
    def repeat(self) -> bool:
        return self._repeat

    @property
    def trimming(self) -> bool:
        return self._trimming

    @property
    def can_trim(self) -> bool:
        stream = self.media_stream
        return bool(
            self.current_path is not None
            and stream is not None
            and stream.is_prepared()
            and stream.is_seekable()
            and stream.has_video()
            and stream.get_duration() > 0
        )

    @repeat.setter
    def repeat(self, value: bool):
        self._repeat = bool(value)
        self.repeat_button.set_active(self._repeat)
        self.repeat_button.set_label("Loop" if self._repeat else "List")
        self.repeat_button.set_tooltip_text("Repeat current video" if self._repeat else "Continue to next file")
        if self.media_stream is not None:
            self.media_stream.set_loop(self._repeat)

    def load(self, path: Path):
        self.clear()
        path = Path(path).resolve()
        if not path.is_file():
            self._error(f"Video is unavailable: {path}")
            return
        self.current_path = path
        self._load_subtitles(path)
        stream = Gtk.MediaFile.new_for_filename(str(path))
        self.media_stream = stream
        stream.set_loop(self.repeat)
        stream.set_volume(self.volume)
        self._signal_ids = [
            stream.connect("notify::ended", self._on_ended),
            stream.connect("notify::error", self._on_error),
            stream.connect("notify::prepared", self._on_prepared),
            stream.connect("notify::playing", self._on_playing_changed),
        ]
        self.picture.set_paintable(stream)
        self._realize_stream()
        self.play_button.set_sensitive(True)
        self._tick_id = GLib.timeout_add(UPDATE_INTERVAL_MS, self._update_position)
        # Errors/preparation may occur during construction, before signals exist.
        if stream.get_error() is not None:
            self._on_error(stream, None)
            return
        self.play()
        if self.media_stream is stream and stream.is_prepared():
            self._on_prepared(stream, None)

    def clear(self):
        self._leave_trim_mode()
        if self._tick_id:
            GLib.source_remove(self._tick_id)
            self._tick_id = 0
        stream = self.media_stream
        self.media_stream = None
        if stream is not None:
            for signal_id in self._signal_ids:
                stream.disconnect(signal_id)
            self._signal_ids = []
            stream.pause()
            self.picture.set_paintable(None)
            if self._surface is not None:
                stream.unrealize(self._surface)
                self._surface = None
            stream.clear()
        self.current_path = None
        self.position_us = self.duration_us = 0
        self.subtitle_tracks = []
        self.subtitle_track_index = -1
        self._subtitle_entries = []
        self._subtitle_starts = []
        self._subtitle_ends = []
        self.subtitle_label.set_label("")
        self.subtitle_label.set_visible(False)
        self.subtitle_button.set_label("Subtitles")
        self._updating_progress = True
        try:
            self.progress_scale.set_range(0.0, 1.0)
            self.progress_scale.set_value(0.0)
        finally:
            self._updating_progress = False
        self.progress_scale.set_sensitive(False)
        self.time_label.set_label("00:00 / 00:00")
        self.play_button.set_sensitive(False)
        self._changed()

    def begin_trim(self) -> bool:
        if not self.can_trim:
            return False
        self.pause()
        self._update_position()
        self._trimming = True
        self._trim_processing = False
        self._trim_previewing = False
        self.trim_range.set_visible(True)
        self.trim_controls.set_visible(True)
        self._reset_trim_range()
        self._changed()
        return True

    def cancel_trim(self) -> None:
        if not self._trimming:
            return
        self.pause()
        self._leave_trim_mode()
        self._changed()

    def set_trim_processing(self, processing: bool) -> None:
        self._trim_processing = bool(processing)
        for widget in self._trim_input_widgets:
            widget.set_sensitive(not processing)
        self.trim_cancel_button.set_label("Cancel processing" if processing else "Cancel")
        self.trim_cancel_button.set_tooltip_text(
            "Stop FFmpeg and discard its incomplete output"
            if processing
            else "Cancel video trimming"
        )

    def current_trim_settings(self) -> TrimSettings:
        start_us = parse_video_time(self.trim_start_entry.get_text())
        end_us = parse_video_time(self.trim_end_entry.get_text())
        settings = TrimSettings(
            start_us,
            end_us,
            reencode=self.trim_mode_dropdown.get_selected() == 1,
            include_audio=self.trim_audio_button.get_active(),
        )
        settings.validate(self.duration_us)
        return settings

    def request_trim_save(self) -> None:
        self._save_trim(None)

    def _leave_trim_mode(self) -> None:
        self._trimming = False
        self._trim_processing = False
        self._trim_previewing = False
        if hasattr(self, "trim_controls"):
            self.trim_controls.set_visible(False)
            self.set_trim_processing(False)
        if hasattr(self, "trim_range"):
            self.trim_range.set_visible(False)
        if hasattr(self, "progress_scale"):
            self.progress_scale.clear_marks()

    def _set_trim_entries(self, start_us: int, end_us: int) -> None:
        self.trim_start_entry.handler_block_by_func(self._trim_time_changed)
        self.trim_end_entry.handler_block_by_func(self._trim_time_changed)
        try:
            self.trim_start_entry.set_text(format_video_time(start_us))
            self.trim_end_entry.set_text(format_video_time(end_us))
        finally:
            self.trim_start_entry.handler_unblock_by_func(self._trim_time_changed)
            self.trim_end_entry.handler_unblock_by_func(self._trim_time_changed)
        self._refresh_trim_range()

    def _reset_trim_range(self) -> None:
        if self.duration_us > 0:
            self._set_trim_entries(0, self.duration_us)

    def _set_trim_boundary(self, start: bool) -> None:
        try:
            settings = self.current_trim_settings()
        except ValueError:
            start_us, end_us = 0, self.duration_us
        else:
            start_us, end_us = settings.start_us, settings.end_us
        position_us = min(max(0, self.position_us), self.duration_us)
        minimum_gap_us = 1000
        if start:
            start_us = min(position_us, end_us - minimum_gap_us)
        else:
            end_us = max(position_us, start_us + minimum_gap_us)
        self._set_trim_entries(start_us, end_us)

    def _trim_time_changed(self, _entry) -> None:
        self._refresh_trim_range()

    def _refresh_trim_range(self) -> None:
        valid = True
        try:
            settings = self.current_trim_settings()
        except ValueError:
            valid = False
            settings = None
        for entry in (self.trim_start_entry, self.trim_end_entry):
            if valid:
                entry.remove_css_class("error")
            else:
                entry.add_css_class("error")
        self.trim_save_button.set_sensitive(valid and not self._trim_processing)
        self.trim_preview_button.set_sensitive(valid and not self._trim_processing)
        self.progress_scale.clear_marks()
        self.trim_range.queue_draw()
        if settings is not None:
            self.progress_scale.add_mark(
                settings.start_us / 1_000_000,
                Gtk.PositionType.TOP,
                "Start",
            )
            self.progress_scale.add_mark(
                settings.end_us / 1_000_000,
                Gtk.PositionType.TOP,
                "End",
            )

    def _draw_trim_range(self, _area, context, width, height) -> None:
        context.set_source_rgba(0.45, 0.45, 0.45, 0.45)
        context.rectangle(0, 0, width, height)
        context.fill()
        try:
            settings = self.current_trim_settings()
        except ValueError:
            return
        if self.duration_us <= 0:
            return
        start_x = width * settings.start_us / self.duration_us
        end_x = width * settings.end_us / self.duration_us
        context.set_source_rgba(0.20, 0.55, 0.95, 0.95)
        context.rectangle(start_x, 0, max(1, end_x - start_x), height)
        context.fill()

    def _preview_trim(self, _button) -> None:
        if self._trim_previewing:
            self.pause()
            self._trim_previewing = False
            self.trim_preview_button.set_label("Play selection")
            return
        try:
            settings = self.current_trim_settings()
        except ValueError as error:
            self._error(str(error))
            return
        self.media_stream.seek(settings.start_us)
        self._trim_previewing = True
        self.trim_preview_button.set_label("Stop preview")
        self.play()

    def _request_trim_cancel(self, _button) -> None:
        if self.on_trim_cancel is not None:
            self.on_trim_cancel()
        elif not self._trim_processing:
            self.cancel_trim()

    def _save_trim(self, _button) -> None:
        try:
            settings = self.current_trim_settings()
        except ValueError as error:
            self._error(str(error))
            return
        self.pause()
        self._trim_previewing = False
        self.trim_preview_button.set_label("Play selection")
        if self.on_trim_save is not None:
            self.on_trim_save(settings)

    def close(self):
        self.clear()
        Gtk.StyleContext.remove_provider_for_display(self.get_display(), self._css_provider)

    def pause(self):
        if self.media_stream is not None:
            self.media_stream.pause()

    def play(self):
        if self.media_stream is not None:
            if self.media_stream.get_ended():
                self.media_stream.seek(0)
            self.media_stream.play()

    def toggle_pause(self):
        if self.playing:
            self.pause()
        else:
            self.play()

    def seek_relative(self, seconds: float):
        stream = self.media_stream
        if stream is None or not stream.is_seekable():
            return
        position = max(0, stream.get_timestamp() + round(seconds * 1_000_000))
        duration = stream.get_duration()
        stream.seek(min(position, duration) if duration > 0 else position)

    def set_controls_visible(self, visible: bool):
        self.controls.set_visible(visible)

    def toggle_subtitles(self):
        if not self.subtitle_tracks:
            self._error("No external SRT or VTT subtitles found beside this video")
            return
        self.subtitles_enabled = not self.subtitles_enabled
        self._update_subtitles()

    def cycle_subtitles(self):
        if not self.subtitle_tracks:
            self._error("No external SRT or VTT subtitles found beside this video")
            return
        self._set_subtitle_track((self.subtitle_track_index + 1) % len(self.subtitle_tracks))
        self.subtitles_enabled = True
        self._update_subtitles()

    def capture_screenshot(self) -> Path:
        stream = self.media_stream
        if stream is None or self.current_path is None or not stream.is_prepared() or not stream.has_video():
            raise RuntimeError("No video frame is ready to capture")
        width, height = stream.get_intrinsic_width(), stream.get_intrinsic_height()
        if width <= 0 or height <= 0:
            raise RuntimeError("Video frame dimensions are unavailable")
        position_us = stream.get_timestamp()
        frame = stream.get_current_image()
        if isinstance(frame, Gdk.Texture):
            texture = frame
        else:
            native = self.get_native()
            renderer = native.get_renderer() if native is not None else None
            if renderer is None:
                raise RuntimeError("The video renderer is unavailable")
            snapshot = Gtk.Snapshot()
            frame.snapshot(snapshot, float(width), float(height))
            node = snapshot.to_node()
            if node is None:
                raise RuntimeError("No video frame is ready to capture")
            texture = renderer.render_texture(node, None)
        if texture is None:
            raise RuntimeError("Could not render the current frame")
        png_bytes = texture.save_to_png_bytes().get_data()
        now = datetime.now(timezone.utc)
        while True:
            path = build_screenshot_path(self.current_path, position_us, now)
            try:
                # Exclusive creation also protects against a concurrent capture.
                handle = path.open("xb")
            except FileExistsError:
                continue
            try:
                with handle:
                    handle.write(png_bytes)
            except OSError:
                path.unlink(missing_ok=True)
                raise
            return path

    def _on_repeat_toggled(self, button):
        self.repeat = button.get_active()

    def _on_volume_changed(self, _button, value):
        self.volume = value
        if self.media_stream is not None:
            self.media_stream.set_volume(value)

    def _on_progress_changed(self, scale):
        stream = self.media_stream
        if not self._updating_progress and stream is not None and stream.is_seekable():
            stream.seek(round(scale.get_value() * 1_000_000))

    def _changed(self):
        self.play_button.set_icon_name("media-playback-pause-symbolic" if self.playing else "media-playback-start-symbolic")
        if self.on_change is not None:
            self.on_change()

    def _error(self, message):
        if self.on_error is not None:
            self.on_error(message)

    def _on_playing_changed(self, stream, _param):
        if stream is self.media_stream:
            self._changed()

    def _on_prepared(self, stream, _param):
        if stream is self.media_stream and stream.is_prepared():
            stream.set_loop(self.repeat)
            stream.set_volume(self.volume)
            self._update_position()
            self._changed()

    def _on_error(self, stream, _param):
        if stream is not self.media_stream or stream.get_error() is None:
            return
        message = stream.get_error().message
        self.clear()
        self._error(f"Could not play video: {message}")

    def _on_ended(self, stream, _param):
        if stream is not self.media_stream or not stream.get_ended():
            return
        if self._trim_previewing:
            try:
                trim_end_us = self.current_trim_settings().end_us
            except ValueError:
                trim_end_us = self.duration_us
            self._trim_previewing = False
            self.trim_preview_button.set_label("Play selection")
            stream.pause()
            stream.seek(trim_end_us)
            self._changed()
            return
        if self.repeat:
            stream.seek(0)
            stream.play()
        else:
            self._changed()
            if self.on_ended is not None:
                self.on_ended()

    def _update_position(self):
        stream = self.media_stream
        if stream is None:
            self._tick_id = 0
            return GLib.SOURCE_REMOVE
        self.position_us = max(0, stream.get_timestamp())
        self.duration_us = max(0, stream.get_duration())
        if self._trim_previewing:
            try:
                trim_end_us = self.current_trim_settings().end_us
            except ValueError:
                self._trim_previewing = False
                self.trim_preview_button.set_label("Play selection")
            else:
                if self.position_us >= trim_end_us:
                    stream.pause()
                    stream.seek(trim_end_us)
                    self._trim_previewing = False
                    self.trim_preview_button.set_label("Play selection")
        self._updating_progress = True
        try:
            self.progress_scale.set_range(0.0, max(1.0, self.duration_us / 1_000_000))
            self.progress_scale.set_value(min(self.position_us, self.duration_us) / 1_000_000)
        finally:
            self._updating_progress = False
        self.progress_scale.set_sensitive(self.duration_us > 0 and stream.is_seekable())
        self.time_label.set_label(f"{format_us(self.position_us)} / {format_us(self.duration_us)}")
        self._update_subtitles()
        return GLib.SOURCE_CONTINUE

    def _load_subtitles(self, video_path):
        try:
            candidates = find_subtitle_candidates(video_path)
        except OSError:
            candidates = []
        seen = set()
        for path in candidates:
            if path.resolve() in seen:
                continue
            seen.add(path.resolve())
            try:
                entries = read_subtitle_entries(path)
            except OSError:
                continue
            if entries:
                self.subtitle_tracks.append((path, entries))
        if self.subtitle_tracks:
            self._set_subtitle_track(0)

    def _set_subtitle_track(self, index):
        self.subtitle_track_index = index
        _path, self._subtitle_entries = self.subtitle_tracks[index]
        self._subtitle_starts = [cue[0] for cue in self._subtitle_entries]
        self._subtitle_ends = []
        end = 0
        for cue in self._subtitle_entries:
            end = max(end, cue[1])
            self._subtitle_ends.append(end)

    def _update_subtitles(self):
        text = ""
        if self.subtitles_enabled and self._subtitle_entries:
            text = subtitle_text_at(self._subtitle_entries, self._subtitle_starts, self._subtitle_ends, self.position_us)
        self.subtitle_label.set_label(text)
        self.subtitle_label.set_visible(bool(text))
        enabled = self.subtitles_enabled and bool(self.subtitle_tracks)
        self.subtitle_button.set_label("Subtitles on" if enabled else "Subtitles")
        track = self.subtitle_tracks[self.subtitle_track_index][0].name if self.subtitle_tracks else "No sidecar subtitle files"
        self.subtitle_button.set_tooltip_text(f"{track}\nV: toggle subtitles; Shift+V: next track")

    def _realize_stream(self):
        native = self.get_native()
        surface = native.get_surface() if native is not None else None
        if self.media_stream is not None and self._surface is None and self.get_realized() and surface is not None:
            self.media_stream.realize(surface)
            self._surface = surface

    def _on_realize(self, _widget):
        self._realize_stream()

    def _on_unrealize(self, _widget):
        if self.media_stream is not None and self._surface is not None:
            self.media_stream.unrealize(self._surface)
            self._surface = None

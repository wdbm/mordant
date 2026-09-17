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

    def __init__(self, on_error=None, on_change=None, on_ended=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.on_error = on_error
        self.on_change = on_change
        self.on_ended = on_ended
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
        self.volume_button.set_value(self.volume)
        self.volume_button.connect("value-changed", self._on_volume_changed)
        transport.append(self.volume_button)

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

    @property
    def playing(self) -> bool:
        return bool(self.media_stream and self.media_stream.get_playing())

    @property
    def repeat(self) -> bool:
        return self._repeat

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

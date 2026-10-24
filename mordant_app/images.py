"""
Mordant GTK presentation for still and animated images

Copyright (C) 2026 William Breaden Madden
SPDX-License-Identifier: GPL-3.0-or-later
"""

from concurrent.futures import ThreadPoolExecutor
from itertools import islice
import math
from pathlib import Path
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk

from .crop import CropRectangle, largest_crop, move_crop, resize_crop
from .image_processing import (
    CACHE_MAX_PREFETCHES,
    ImageCache,
    cairo_surface,
    clamp_offsets,
    render_viewport,
)
from .media import IMAGE_SUFFIXES


ZOOM_FACTOR_PER_STEP = 1.12
SURFACE_SCROLL_PIXELS_PER_STEP = 48.0
MAX_SCROLL_STEPS_PER_EVENT = 3.0
CROP_HANDLE_SIZE = 9.0
CROP_HIT_DISTANCE = 14.0


class ImageView(Gtk.DrawingArea):
    """
    Asynchronous still/animated image view; callbacks run on the GTK thread.

    `on_error(message)` reports failed loads/renders. `on_change()` reports a
    successful load or a pause-state change. The shell owns keys and navigation.
    """

    def __init__(self, on_error=None, on_change=None):
        super().__init__()
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_focusable(True)
        self.set_draw_func(self._draw)
        self.connect("resize", self._resize)
        self.on_error = on_error
        self.on_change = on_change
        self.cache = ImageCache()
        self._render_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mordant-render")
        self.path = None
        self.data = None
        self.frame_index = 0
        self.paused = False
        self.zoom = 1.0
        self.ox = self.oy = 0.0
        self._view_size = (1, 1)
        self._auto_fit = True
        self._generation = 0
        self._serial = 0
        self._closed = False
        self._load_future = None
        self._render_future = None
        self._render_pending = None
        self._surface = None
        self._animation_deadline = None
        self._animation_job = self._load_job = self._render_job = self._refine_job = 0
        self._pointer = None
        self._pan_origin = None
        self.crop_rectangle = None
        self.crop_aspect = (3, 2)
        self._crop_drag = None
        self._surface_scroll_remainder = 0.0

        motion = Gtk.EventControllerMotion.new()
        motion.connect("motion", lambda controller, x, y: self._remember_pointer(x, y))
        motion.connect("leave", lambda controller: self._remember_pointer(None, None))
        self.add_controller(motion)
        wheel = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        wheel.connect("scroll", self._scroll)
        wheel.connect("scroll-begin", self._reset_scroll_accumulator)
        wheel.connect("scroll-end", self._reset_scroll_accumulator)
        self.add_controller(wheel)
        drag = Gtk.GestureDrag.new()
        drag.set_button(Gdk.BUTTON_PRIMARY)
        drag.connect("drag-begin", self._begin_pan)
        drag.connect("drag-update", self._pan)
        drag.connect("drag-end", self._end_drag)
        self.add_controller(drag)

    @property
    def is_animated(self):
        return bool(self.data is not None and self.data.is_animated)

    @property
    def cropping(self):
        return self.crop_rectangle is not None

    @property
    def can_crop(self):
        return bool(
            self.data is not None
            and not self.data.is_animated
            and self.path is not None
            and self.path.suffix.lower() != ".svg"
        )

    def _cancel(self, name):
        job = getattr(self, name)
        if job:
            GLib.source_remove(job)
            setattr(self, name, 0)

    def clear(self):
        self._generation += 1
        for name in ("_animation_job", "_load_job", "_render_job", "_refine_job"):
            self._cancel(name)
        if self._render_future is not None:
            self._render_future.cancel()
        self.path = self.data = self._load_future = self._render_future = self._render_pending = self._surface = None
        self.frame_index = 0
        self.paused = False
        self.zoom = 1.0
        self._auto_fit = True
        self._animation_deadline = self._pan_origin = None
        self.crop_rectangle = self._crop_drag = None
        self._reset_scroll_accumulator()
        self.queue_draw()

    def close(self):
        if self._closed:
            return
        self.clear()
        self._closed = True
        self.cache.close()
        self._render_pool.shutdown(wait=False, cancel_futures=True)

    def load(self, path: Path):
        if self._closed:
            return
        self.clear()
        self.path = Path(path).resolve()
        self._load_future = self.cache.request(self.path)
        self._load_job = GLib.timeout_add(16, self._poll_load)

    def prefetch(self, paths):
        if self._closed:
            return
        for path in islice(paths, CACHE_MAX_PREFETCHES):
            if Path(path).suffix.lower() in IMAGE_SUFFIXES:
                self.cache.request(Path(path), prefetch=True)

    def _report_error(self, error):
        if self.on_error is not None:
            self.on_error(str(error))

    def _notify_change(self):
        if self.on_change is not None:
            self.on_change()

    def _poll_load(self):
        if self._load_future is None:
            self._load_job = 0
            return GLib.SOURCE_REMOVE
        if not self._load_future.done():
            return GLib.SOURCE_CONTINUE
        future = self._load_future
        self._load_future = None
        self._load_job = 0
        try:
            self.data = future.result()
        except Exception as error:
            self._report_error(error)
            return GLib.SOURCE_REMOVE
        self.reset_zoom()
        self._schedule_animation()
        self._notify_change()
        return GLib.SOURCE_REMOVE

    def _scale(self):
        if self.data is None:
            return 1.0
        width, height = self.data.frames[self.frame_index].image.size
        return min(self._view_size[0] / width, self._view_size[1] / height) * self.zoom

    def _clamp(self):
        if self.data is not None:
            self.ox, self.oy = clamp_offsets(self.data.frames[self.frame_index].image.size, self._view_size, self._scale(), self.ox, self.oy)

    def _resize(self, widget, width, height):
        old_scale = self._scale()
        old_width, old_height = self._view_size
        self._view_size = (max(1, width), max(1, height))
        if self.data is None:
            return
        if self._auto_fit:
            self._centre()
        else:
            scale = self._scale()
            self.ox = width / 2 - (old_width / 2 - self.ox) / old_scale * scale
            self.oy = height / 2 - (old_height / 2 - self.oy) / old_scale * scale
        self._generation += 1
        self._clamp()
        self._queue_render()

    def _centre(self):
        if self.data is None:
            return
        image = self.data.frames[self.frame_index].image
        scale = self._scale()
        self.ox = (self._view_size[0] - image.width * scale) / 2
        self.oy = (self._view_size[1] - image.height * scale) / 2

    def reset_zoom(self):
        if self.data is None:
            return
        self._view_size = (max(1, self.get_width()), max(1, self.get_height()))
        self.zoom = 1.0
        self._auto_fit = True
        self._centre()
        self._generation += 1
        self._queue_render()

    def zoom_at(self, factor, x=None, y=None):
        if self.data is None or not math.isfinite(factor) or factor <= 0:
            return
        x = self._view_size[0] / 2 if x is None else x
        y = self._view_size[1] / 2 if y is None else y
        old_scale = self._scale()
        image_x, image_y = (x - self.ox) / old_scale, (y - self.oy) / old_scale
        self.zoom = min(32.0, max(0.125, self.zoom * factor))
        self.ox, self.oy = x - image_x * self._scale(), y - image_y * self._scale()
        self._auto_fit = False
        self._generation += 1
        self._clamp()
        self._queue_render()

    def _remember_pointer(self, x, y):
        self._pointer = None if x is None else (x, y)

    def _reset_scroll_accumulator(self, *_args):
        self._surface_scroll_remainder = 0.0

    def _scroll(self, controller, _dx, dy):
        if self.data is None:
            return False
        if self.cropping:
            return True
        steps = -dy
        if controller.get_unit() == Gdk.ScrollUnit.SURFACE:
            surface_steps = max(
                -MAX_SCROLL_STEPS_PER_EVENT,
                min(MAX_SCROLL_STEPS_PER_EVENT, steps / SURFACE_SCROLL_PIXELS_PER_STEP),
            )
            self._surface_scroll_remainder += surface_steps
            steps = math.trunc(self._surface_scroll_remainder)
            self._surface_scroll_remainder -= steps
        else:
            self._reset_scroll_accumulator()
            steps = max(-MAX_SCROLL_STEPS_PER_EVENT, min(MAX_SCROLL_STEPS_PER_EVENT, steps))
        if not steps:
            return True
        x, y = self._pointer if self._pointer is not None else (None, None)
        self.zoom_at(ZOOM_FACTOR_PER_STEP ** steps, x, y)
        return True

    def _begin_pan(self, gesture, x, y):
        self.grab_focus()
        if self.cropping:
            crop_x, crop_y, crop_width, crop_height = self._crop_view_rectangle()
            corners = {
                "north_west": (crop_x, crop_y),
                "north_east": (crop_x + crop_width, crop_y),
                "south_west": (crop_x, crop_y + crop_height),
                "south_east": (crop_x + crop_width, crop_y + crop_height),
            }
            corner = next(
                (
                    name
                    for name, point in corners.items()
                    if math.hypot(x - point[0], y - point[1]) <= CROP_HIT_DISTANCE
                ),
                None,
            )
            inside = crop_x <= x <= crop_x + crop_width and crop_y <= y <= crop_y + crop_height
            if corner is not None or inside:
                self._crop_drag = (corner or "move", x, y, self.crop_rectangle)
            return
        self._pan_origin = (self.ox, self.oy)

    def _pan(self, gesture, dx, dy):
        if self.cropping:
            if self._crop_drag is None:
                return
            mode, start_x, start_y, original = self._crop_drag
            image_size = self._crop_image_size()
            if mode == "move":
                source_start = self._view_to_crop(start_x, start_y)
                source_end = self._view_to_crop(start_x + dx, start_y + dy)
                self.crop_rectangle = move_crop(
                    original,
                    source_end[0] - source_start[0],
                    source_end[1] - source_start[1],
                    image_size,
                )
            else:
                decoded_height = self.data.frames[self.frame_index].image.height
                source_height_per_view_pixel = image_size[1] / decoded_height / self._scale()
                self.crop_rectangle = resize_crop(
                    original,
                    mode,
                    self._view_to_crop(start_x + dx, start_y + dy),
                    image_size,
                    self.crop_aspect,
                    minimum_height=max(1.0, 24 * source_height_per_view_pixel),
                )
            self.queue_draw()
            return
        if self.data is None or self._pan_origin is None:
            return
        self._auto_fit = False
        self.ox, self.oy = self._pan_origin[0] + dx, self._pan_origin[1] + dy
        self._generation += 1
        self._clamp()
        self._queue_render()

    def _end_drag(self, *_args):
        self._pan_origin = None
        self._crop_drag = None

    def _crop_image_size(self):
        return self.data.original_size or self.data.frames[self.frame_index].image.size

    def _crop_view_rectangle(self):
        rectangle = self.crop_rectangle
        original_width, original_height = self._crop_image_size()
        decoded = self.data.frames[self.frame_index].image
        scale = self._scale()
        factor_x = decoded.width / original_width * scale
        factor_y = decoded.height / original_height * scale
        return (
            self.ox + rectangle.x * factor_x,
            self.oy + rectangle.y * factor_y,
            rectangle.width * factor_x,
            rectangle.height * factor_y,
        )

    def _view_to_crop(self, x, y):
        original_width, original_height = self._crop_image_size()
        decoded = self.data.frames[self.frame_index].image
        scale = self._scale()
        return (
            (x - self.ox) / (decoded.width / original_width * scale),
            (y - self.oy) / (decoded.height / original_height * scale),
        )

    def begin_crop(self, aspect=(3, 2)):
        if not self.can_crop:
            return False
        self.crop_aspect = aspect
        self.reset_zoom()
        self.crop_rectangle = largest_crop(self._crop_image_size(), aspect)
        self.queue_draw()
        return True

    def set_crop_aspect(self, aspect):
        if not self.cropping:
            return
        centre = (
            self.crop_rectangle.x + self.crop_rectangle.width / 2,
            self.crop_rectangle.y + self.crop_rectangle.height / 2,
        )
        self.crop_aspect = aspect
        self.crop_rectangle = largest_crop(self._crop_image_size(), aspect, centre)
        self.queue_draw()

    def cancel_crop(self):
        self.crop_rectangle = self._crop_drag = None
        self.queue_draw()

    def _queue_render(self, *, refine=False):
        if self.data is None or self._closed:
            return
        self._serial += 1
        self._render_pending = (
            self._generation, self._serial, self.data.frames[self.frame_index],
            self._view_size, self._scale(), self.ox, self.oy, refine,
        )
        if self._render_future is None:
            self._start_render()
        if not refine:
            self._cancel("_refine_job")
            if not self.is_animated or self.paused:
                self._refine_job = GLib.timeout_add(110, self._refine)

    def _start_render(self):
        request = self._render_pending
        self._render_pending = None
        generation, serial, frame, size, scale, ox, oy, refine = request

        def render():
            viewport = render_viewport(frame, size, scale, ox, oy, refine=refine)
            surface, pixels = cairo_surface(viewport.image)
            return generation, serial, viewport, surface, pixels

        self._render_future = self._render_pool.submit(render)
        if not self._render_job:
            self._render_job = GLib.timeout_add(16, self._poll_render)

    def _poll_render(self):
        if self._render_future is None:
            self._render_job = 0
            return GLib.SOURCE_REMOVE
        if not self._render_future.done():
            return GLib.SOURCE_CONTINUE
        future, self._render_future = self._render_future, None
        try:
            generation, serial, viewport, surface, pixels = future.result()
            if generation == self._generation and self.data is not None:
                self._surface = (viewport, surface, pixels)
                self.queue_draw()
        except Exception as error:
            self._report_error(error)
        if self._render_pending is not None:
            self._start_render()
            return GLib.SOURCE_CONTINUE
        self._render_job = 0
        return GLib.SOURCE_REMOVE

    def _refine(self):
        self._refine_job = 0
        self._queue_render(refine=True)
        return GLib.SOURCE_REMOVE

    def _draw(self, widget, context, width, height):
        context.set_source_rgb(0, 0, 0)
        context.paint()
        if self._surface is None:
            return
        viewport, surface, pixels = self._surface
        context.save()
        context.translate(viewport.x, viewport.y)
        context.scale(viewport.width / surface.get_width(), viewport.height / surface.get_height())
        context.set_source_surface(surface, 0, 0)
        context.paint()
        context.restore()
        if self.cropping:
            self._draw_crop(context)

    def _draw_crop(self, context):
        decoded = self.data.frames[self.frame_index].image
        scale = self._scale()
        image_x, image_y = self.ox, self.oy
        image_width, image_height = decoded.width * scale, decoded.height * scale
        crop_x, crop_y, crop_width, crop_height = self._crop_view_rectangle()
        context.save()
        context.rectangle(image_x, image_y, image_width, image_height)
        context.clip()
        context.set_source_rgba(0, 0, 0, 0.52)
        context.rectangle(image_x, image_y, image_width, max(0, crop_y - image_y))
        context.rectangle(image_x, crop_y, max(0, crop_x - image_x), crop_height)
        context.rectangle(
            crop_x + crop_width,
            crop_y,
            max(0, image_x + image_width - crop_x - crop_width),
            crop_height,
        )
        context.rectangle(
            image_x,
            crop_y + crop_height,
            image_width,
            max(0, image_y + image_height - crop_y - crop_height),
        )
        context.fill()
        context.set_source_rgb(1, 1, 1)
        context.set_line_width(1.0)
        context.rectangle(crop_x + 0.5, crop_y + 0.5, max(0, crop_width - 1), max(0, crop_height - 1))
        context.stroke()
        radius = CROP_HANDLE_SIZE / 2
        for x, y in (
            (crop_x, crop_y),
            (crop_x + crop_width, crop_y),
            (crop_x, crop_y + crop_height),
            (crop_x + crop_width, crop_y + crop_height),
        ):
            context.rectangle(x - radius, y - radius, CROP_HANDLE_SIZE, CROP_HANDLE_SIZE)
        context.fill()
        context.restore()

    def _schedule_animation(self):
        if not self.is_animated or self.paused:
            return
        now = time.monotonic()
        duration = self.data.durations_ms[self.frame_index] / 1000
        # Preserve cadence while skipping catch-up bursts after slow rendering.
        previous_deadline = self._animation_deadline if self._animation_deadline is not None else now
        self._animation_deadline = max(now + 0.001, previous_deadline + duration)
        self._animation_job = GLib.timeout_add(max(1, math.ceil((self._animation_deadline - now) * 1000)), self._advance_animation)

    def _advance_animation(self):
        self._animation_job = 0
        if self.is_animated and not self.paused:
            self.frame_index = (self.frame_index + 1) % len(self.data.frames)
            self._queue_render()
            self._schedule_animation()
        return GLib.SOURCE_REMOVE

    def toggle_pause(self):
        if not self.is_animated:
            return
        self.paused = not self.paused
        self._cancel("_animation_job")
        self._animation_deadline = None
        if self.paused:
            self._queue_render(refine=True)
        else:
            self._schedule_animation()
        self._notify_change()

"""
Mordant image decoding, caching and viewport rendering

Copyright (C) 2026 William Breaden Madden
SPDX-License-Identifier: GPL-3.0-or-later

Decoded images are immutable once published. Cache eviction only releases its
own references, so a visible image remains usable while neighbours are loaded.
"""

from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from dataclasses import dataclass
from io import BytesIO
import math
from pathlib import Path
import re
import sys
import threading


import cairo
import gi
from PIL import Image, ImageOps

gi.require_foreign("cairo")
try:
    gi.require_version("Rsvg", "2.0")
    from gi.repository import Rsvg
except (ImportError, ValueError):
    Rsvg = None

try:
    import cairosvg
except ImportError:
    cairosvg = None


MAX_RENDER_SIDE = 5000
MIPMAP_MIN_SIDE = 256
MAX_DECODE_BYTES = 192 * 1024 * 1024
CACHE_MAX_BYTES = 256 * 1024 * 1024
CACHE_MAX_PREFETCHES = 6
MAX_ANIMATION_FRAMES = 4096
MINIMUM_ANIMATION_FRAME_MS = 10


@dataclass(frozen=True)
class CachedFrame:
    image: Image.Image
    mipmaps: tuple[Image.Image, ...] = ()

    @property
    def byte_size(self) -> int:
        return sum(level.width * level.height * 4 for level in (self.image, *self.mipmaps))


@dataclass(frozen=True)
class CachedImageData:
    frames: tuple[CachedFrame, ...]
    durations_ms: tuple[int, ...]
    loop: int | None = None
    original_size: tuple[int, int] | None = None

    @property
    def is_animated(self) -> bool:
        return len(self.frames) > 1

    @property
    def byte_size(self) -> int:
        return sum(frame.byte_size for frame in self.frames)


def build_mipmaps(image: Image.Image) -> tuple[Image.Image, ...]:
    levels = []
    previous = image
    while min(previous.size) >= MIPMAP_MIN_SIDE * 2:
        previous = previous.resize(
            (previous.width // 2, previous.height // 2), Image.Resampling.LANCZOS,
        )
        levels.append(previous)
    return tuple(levels)


def bounded_decode_size(size: tuple[int, int], frame_count: int = 1) -> tuple[int, int]:
    """Reserve room for RGBA frames and their approximately 1/3-size mipmaps."""
    width, height = size
    ratio = min(1.0, math.sqrt(MAX_DECODE_BYTES / (width * height * 4 * frame_count * 1.34)))
    return max(1, int(width * ratio)), max(1, int(height * ratio))


def load_raster_image(path: Path, cancel_event=None) -> CachedImageData:
    with Image.open(path) as source:
        frame_count = max(1, getattr(source, "n_frames", 1))
        # Phone JPEGs can be MPOs containing auxiliary frames which are not an
        # animation and sometimes cannot be decoded by Pillow.
        animated = bool(source.format != "MPO" and getattr(source, "is_animated", False) and frame_count > 1)
        frame_count = frame_count if animated else 1
        if frame_count > MAX_ANIMATION_FRAMES:
            raise ValueError(f"Animation has too many frames ({frame_count}; limit {MAX_ANIMATION_FRAMES}).")
        original_size = source.size
        target_size = bounded_decode_size(original_size, frame_count)
        frames = []
        durations = []
        loop = source.info.get("loop")
        for index in range(frame_count):
            if cancel_event is not None and cancel_event.is_set():
                raise CancelledError()
            if animated:
                source.seek(index)
            source.load()  # WEBP frame durations become available after load().
            if animated:
                image = source.convert("RGBA")
            else:
                oriented = ImageOps.exif_transpose(source)
                original_size = oriented.size
                target_size = bounded_decode_size(original_size)
                image = oriented.convert("RGBA")
            if image.size != target_size:
                image = image.resize(target_size, Image.Resampling.LANCZOS)
            frames.append(CachedFrame(image, build_mipmaps(image)))
            duration = max(MINIMUM_ANIMATION_FRAME_MS, int(source.info.get("duration", 100) or 100))
            durations.append(duration if animated else 0)
        return CachedImageData(tuple(frames), tuple(durations), loop, original_size)


def _svg_intrinsic_size(handle) -> tuple[float, float]:
    scales = {
        Rsvg.Unit.PX: 1.0, Rsvg.Unit.PT: 96 / 72, Rsvg.Unit.PC: 16.0,
        Rsvg.Unit.MM: 96 / 25.4, Rsvg.Unit.CM: 96 / 2.54, Rsvg.Unit.IN: 96.0,
    }
    has_width, width, has_height, height, has_viewbox, viewbox = handle.get_intrinsic_dimensions()
    w = width.length * scales.get(width.unit, 0) if has_width else 0
    h = height.length * scales.get(height.unit, 0) if has_height else 0
    if has_viewbox and viewbox.width > 0 and viewbox.height > 0:
        aspect = viewbox.width / viewbox.height
        if w <= 0 and h <= 0:
            w, h = viewbox.width, viewbox.height
        elif w <= 0:
            w = h * aspect
        elif h <= 0:
            h = w / aspect
    if w <= 0:
        w = h if h > 0 else 1024.0
    if h <= 0:
        h = w
    return w, h


def _svg_xml_size(path):
    """Resolve a basic SVG viewport without allocating a CairoSVG surface."""
    from xml.etree import ElementTree
    root = ElementTree.parse(path).getroot()

    def pixels(value):
        match = re.fullmatch(r"\s*([+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(px|pt|pc|mm|cm|in)?\s*", value)
        if match is None:
            return 0.0
        units = {None: 1, "px": 1, "pt": 96 / 72, "pc": 16, "mm": 96 / 25.4, "cm": 96 / 2.54, "in": 96}
        return float(match[1]) * units[match[2]]

    width, height = pixels(root.get("width", "")), pixels(root.get("height", ""))
    viewbox = root.get("viewBox", "").replace(",", " ").split()
    if len(viewbox) == 4 and float(viewbox[2]) > 0 and float(viewbox[3]) > 0:
        aspect = float(viewbox[2]) / float(viewbox[3])
        if width <= 0 and height <= 0:
            width, height = float(viewbox[2]), float(viewbox[3])
        elif width <= 0:
            width = height * aspect
        elif height <= 0:
            height = width / aspect
    if width <= 0:
        width = height if height > 0 else 1024
    if height <= 0:
        height = width
    return width, height


def load_svg_image(path: Path) -> CachedImageData:
    if Rsvg is not None:
        handle = Rsvg.Handle.new_from_file(str(path))
        original = _svg_intrinsic_size(handle)
        longest = max(original)
        scale = max(2048.0, min(float(MAX_RENDER_SIDE), longest)) / longest
        width, height = (max(1, round(side * scale)) for side in original)
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
        viewport = Rsvg.Rectangle()
        viewport.x, viewport.y = 0, 0
        viewport.width, viewport.height = width, height
        if handle.render_document(cairo.Context(surface), viewport) is False:
            raise ValueError("librsvg could not render this SVG document.")
        buffer = BytesIO()
        surface.write_to_png(buffer)
        buffer.seek(0)
    elif cairosvg is not None:
        # Fix the output size before rasterisation, keeping even SVG documents
        # with enormous declared dimensions within the image memory budget.
        original = _svg_xml_size(path)
        scale = max(2048.0, min(float(MAX_RENDER_SIDE), max(original))) / max(original)
        width, height = (max(1, round(side * scale)) for side in original)
        buffer = BytesIO(cairosvg.svg2png(url=str(path), dpi=96, output_width=width, output_height=height))
    else:
        raise ValueError("SVG support requires librsvg or CairoSVG.")
    with Image.open(buffer) as decoded:
        image = decoded.convert("RGBA")
    return CachedImageData((CachedFrame(image, build_mipmaps(image)),), (0,), original_size=tuple(round(v) for v in original))


def load_image(path: Path, cancel_event=None) -> CachedImageData:
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledError()
    return load_svg_image(path) if path.suffix.lower() == ".svg" else load_raster_image(path, cancel_event)


class ImageCache:
    """Bounded, stat-aware LRU. Thread-safe; never closes an image still in use."""

    def __init__(self, max_items: int = 8, max_bytes: int = CACHE_MAX_BYTES, workers: int = 2):
        self.max_items = max(1, max_items)
        self.max_bytes = max(1, max_bytes)
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mordant-decode")
        self._cache = OrderedDict()
        self._futures = {}
        self._lock = threading.RLock()
        self._closed = False
        self._cancel_event = threading.Event()

    def request(self, path: Path, *, prefetch: bool = False) -> Future | None:
        path = Path(path).resolve()
        try:
            stat = path.stat()
            key = (path, stat.st_mtime_ns, stat.st_size)
        except OSError as error:
            future = Future()
            future.set_exception(error)
            return future
        with self._lock:
            if self._closed:
                raise RuntimeError("Image cache is closed.")
            if key in self._cache:
                data = self._cache.pop(key)
                self._cache[key] = data
                future = Future()
                future.set_result(data)
                return future
            if key in self._futures:
                return self._futures[key]
            if prefetch and len(self._futures) >= CACHE_MAX_PREFETCHES + 1:
                return None
            if not prefetch:
                # Give explicit navigation priority over queued neighbour loads.
                for pending in list(self._futures.values()):
                    pending.cancel()
            future = self.pool.submit(load_image, path, self._cancel_event)
            self._futures[key] = future
            future.add_done_callback(lambda done: self._completed(key, done))
            return future

    def _completed(self, key, future):
        with self._lock:
            self._futures.pop(key, None)
            if self._closed or future.cancelled() or future.exception() is not None:
                return
            data = future.result()
            if data.byte_size > self.max_bytes:
                return
            for old_key in list(self._cache):
                if old_key[0] == key[0]:
                    del self._cache[old_key]
            self._cache[key] = data
            while len(self._cache) > self.max_items or sum(item.byte_size for item in self._cache.values()) > self.max_bytes:
                self._cache.popitem(last=False)

    def close(self):
        with self._lock:
            self._closed = True
            self._cancel_event.set()
            self._cache.clear()
            for future in list(self._futures.values()):
                future.cancel()
        self.pool.shutdown(wait=False, cancel_futures=True)


def clamp_offsets(image_size, viewport_size, scale, ox, oy):
    """Keep every edge reachable while centring images smaller than the view."""
    result = []
    for source, viewport, offset in zip(image_size, viewport_size, (ox, oy)):
        extent = source * scale
        result.append((viewport - extent) / 2 if extent <= viewport else min(0.0, max(viewport - extent, offset)))
    return tuple(result)


@dataclass
class RenderedViewport:
    image: Image.Image
    x: float
    y: float
    width: float
    height: float


def render_viewport(frame: CachedFrame, viewport_size, scale, ox, oy, *, refine=False) -> RenderedViewport:
    """Resize just the visible crop, choosing mipmaps by the actual zoom level."""
    base = frame.image
    vw, vh = viewport_size
    x0, y0 = max(0.0, -ox / scale), max(0.0, -oy / scale)
    x1, y1 = min(float(base.width), (vw - ox) / scale), min(float(base.height), (vh - oy) / scale)
    logical_width, logical_height = (x1 - x0) * scale, (y1 - y0) * scale
    ratio = min(1.0, MAX_RENDER_SIDE / max(1.0, logical_width, logical_height))
    width, height = max(1, math.ceil(logical_width * ratio)), max(1, math.ceil(logical_height * ratio))
    candidates = [base, *frame.mipmaps]
    required_width, required_height = base.width * scale * ratio, base.height * scale * ratio
    eligible = [image for image in candidates if image.width >= required_width and image.height >= required_height]
    source = min(eligible, key=lambda image: image.width * image.height) if eligible else base
    sx, sy = source.width / base.width, source.height / base.height
    box = (max(0.0, x0 * sx), max(0.0, y0 * sy), min(float(source.width), x1 * sx), min(float(source.height), y1 * sy))
    if box[2] <= box[0] or box[3] <= box[1]:
        return RenderedViewport(Image.new("RGBA", (1, 1)), 0, 0, 1, 1)
    image = source.resize((width, height), Image.Resampling.LANCZOS if refine else Image.Resampling.BILINEAR, box=box)
    return RenderedViewport(image, max(0.0, ox), max(0.0, oy), logical_width, logical_height)


def cairo_surface(image: Image.Image):
    """Cairo ARGB32 is native-endian and requires premultiplied alpha."""
    premultiplied = image.convert("RGBa")
    if sys.byteorder == "little":
        pixels = bytearray(premultiplied.tobytes("raw", "BGRa"))
    else:
        rgba = premultiplied.tobytes()
        pixels = bytearray(len(rgba))
        pixels[0::4], pixels[1::4], pixels[2::4], pixels[3::4] = rgba[3::4], rgba[0::4], rgba[1::4], rgba[2::4]
    surface = cairo.ImageSurface.create_for_data(pixels, cairo.FORMAT_ARGB32, image.width, image.height, image.width * 4)
    return surface, pixels

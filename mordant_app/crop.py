"""
Mordant crop geometry and full-resolution image output

Copyright (C) 2026 William Breaden Madden
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
import math
import os
from pathlib import Path
import tempfile

from PIL import Image, ImageOps


@dataclass(frozen=True)
class CropRectangle:
    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height


AspectRatio = tuple[float, float]


def aspect_value(aspect: AspectRatio) -> float:
    width, height = aspect
    if (
        not math.isfinite(width)
        or not math.isfinite(height)
        or width <= 0
        or height <= 0
    ):
        raise ValueError("Crop aspect dimensions must be positive real numbers.")
    return width / height


def rational_aspect(aspect: AspectRatio) -> tuple[int, int]:
    aspect_value(aspect)
    width, height = aspect
    ratio = (Fraction(str(width)) / Fraction(str(height))).limit_denominator(10000)
    return ratio.numerator, ratio.denominator


def largest_crop(
    image_size: tuple[int, int],
    aspect: AspectRatio,
    centre: tuple[float, float] | None = None,
) -> CropRectangle:
    image_width, image_height = image_size
    if image_width < 1 or image_height < 1:
        raise ValueError("An image must have positive dimensions.")
    ratio = aspect_value(aspect)
    if image_width / image_height >= ratio:
        height = float(image_height)
        width = height * ratio
    else:
        width = float(image_width)
        height = width / ratio
    centre_x, centre_y = centre or (image_width / 2, image_height / 2)
    x = min(max(0.0, centre_x - width / 2), image_width - width)
    y = min(max(0.0, centre_y - height / 2), image_height - height)
    return CropRectangle(x, y, width, height)


def move_crop(
    rectangle: CropRectangle,
    dx: float,
    dy: float,
    image_size: tuple[int, int],
) -> CropRectangle:
    image_width, image_height = image_size
    x = min(max(0.0, rectangle.x + dx), image_width - rectangle.width)
    y = min(max(0.0, rectangle.y + dy), image_height - rectangle.height)
    return CropRectangle(x, y, rectangle.width, rectangle.height)


def resize_crop(
    rectangle: CropRectangle,
    corner: str,
    pointer: tuple[float, float],
    image_size: tuple[int, int],
    aspect: AspectRatio,
    minimum_height: float = 1.0,
) -> CropRectangle:
    anchors = {
        "north_west": (rectangle.right, rectangle.bottom, -1, -1),
        "north_east": (rectangle.x, rectangle.bottom, 1, -1),
        "south_west": (rectangle.right, rectangle.y, -1, 1),
        "south_east": (rectangle.x, rectangle.y, 1, 1),
    }
    if corner not in anchors:
        raise ValueError(f"Unknown crop corner: {corner}")
    anchor_x, anchor_y, direction_x, direction_y = anchors[corner]
    ratio = aspect_value(aspect)
    raw_width = max(0.0, direction_x * (pointer[0] - anchor_x))
    raw_height = max(0.0, direction_y * (pointer[1] - anchor_y))
    height = (ratio * raw_width + raw_height) / (ratio * ratio + 1)
    available_width = image_size[0] - anchor_x if direction_x > 0 else anchor_x
    available_height = image_size[1] - anchor_y if direction_y > 0 else anchor_y
    maximum_height = min(available_width / ratio, available_height)
    height = min(max(minimum_height, height), maximum_height)
    width = height * ratio
    x = anchor_x if direction_x > 0 else anchor_x - width
    y = anchor_y if direction_y > 0 else anchor_y - height
    return CropRectangle(x, y, width, height)


def integer_crop_box(
    rectangle: CropRectangle,
    image_size: tuple[int, int],
    aspect: AspectRatio,
) -> tuple[int, int, int, int]:
    aspect_width, aspect_height = rational_aspect(aspect)
    factor = math.floor(min(rectangle.width / aspect_width, rectangle.height / aspect_height))
    if factor >= 1:
        width, height = aspect_width * factor, aspect_height * factor
    else:
        height = max(1, round(rectangle.height))
        width = max(1, round(height * aspect_width / aspect_height))
        if width > image_size[0]:
            width = image_size[0]
            height = max(1, round(width * aspect_height / aspect_width))
    centre_x = rectangle.x + rectangle.width / 2
    centre_y = rectangle.y + rectangle.height / 2
    left = min(max(0, round(centre_x - width / 2)), image_size[0] - width)
    top = min(max(0, round(centre_y - height / 2)), image_size[1] - height)
    return left, top, left + width, top + height


def suggested_crop_name(path: str | Path) -> str:
    path = Path(path)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%MZ")
    return f"{path.stem}_{timestamp}{path.suffix}"


def rotate_counterclockwise(image: Image.Image, quarter_turns: int) -> Image.Image:
    if not isinstance(quarter_turns, int):
        raise TypeError("Crop rotation must be a whole number of quarter turns.")
    transpose = (
        None,
        Image.Transpose.ROTATE_90,
        Image.Transpose.ROTATE_180,
        Image.Transpose.ROTATE_270,
    )[quarter_turns % 4]
    return image if transpose is None else image.transpose(transpose)


def crop_image(
    source_path: str | Path,
    output_path: str | Path,
    rectangle: CropRectangle,
    aspect: AspectRatio,
    rotation_quarters: int = 0,
) -> Path:
    source_path = Path(source_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source_path) as source:
        if bool(getattr(source, "is_animated", False)) and getattr(source, "n_frames", 1) > 1:
            raise ValueError("Animated images cannot currently be cropped.")
        source_format = source.format
        metadata = {
            key: source.info[key]
            for key in ("icc_profile", "dpi")
            if key in source.info
        }
        oriented = rotate_counterclockwise(
            ImageOps.exif_transpose(source),
            rotation_quarters,
        )
        if (
            rectangle.x < 0
            or rectangle.y < 0
            or rectangle.right > oriented.width + 0.5
            or rectangle.bottom > oriented.height + 0.5
        ):
            raise ValueError("The source image dimensions changed after cropping began.")
        box = integer_crop_box(rectangle, oriented.size, aspect)
        cropped = oriented.crop(box)
        exif = oriented.getexif()
        if exif:
            exif[256], exif[257] = cropped.size
            if 40962 in exif:
                exif[40962] = cropped.width
            if 40963 in exif:
                exif[40963] = cropped.height
            metadata["exif"] = exif.tobytes()
        if source_format == "JPEG":
            if cropped.mode not in ("RGB", "L", "CMYK"):
                background = Image.new("RGB", cropped.size, "white")
                if "A" in cropped.getbands():
                    background.paste(cropped, mask=cropped.getchannel("A"))
                else:
                    background.paste(cropped.convert("RGB"))
                cropped = background
            metadata.update(quality=95, subsampling=0)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".mordant-crop-",
            suffix=output_path.suffix,
            dir=output_path.parent,
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        try:
            cropped.save(temporary_path, format=source_format, **metadata)
            os.replace(temporary_path, output_path)
        finally:
            temporary_path.unlink(missing_ok=True)
    return output_path

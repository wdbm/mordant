"""
Mordant image information and lossless metadata editing

Copyright (C) 2026 William Breaden Madden
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import shutil
import subprocess

from PIL import Image


WRITABLE_METADATA_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
EDITABLE_METADATA_FIELDS = (
    ("title", "Title", "XMP-dc:Title"),
    ("description", "Description", "XMP-dc:Description"),
    ("artist", "Artist", "EXIF:Artist"),
    ("copyright", "Copyright", "EXIF:Copyright"),
    ("keywords", "Keywords", "XMP-dc:Subject"),
    ("rating", "Rating (0-5)", "XMP:Rating"),
    ("date_time_original", "Date and time taken", "EXIF:DateTimeOriginal"),
    ("gps_latitude", "GPS latitude", "EXIF:GPSLatitude"),
    ("gps_longitude", "GPS longitude", "EXIF:GPSLongitude"),
)
EDITABLE_TAGS = {name: tag for name, _label, tag in EDITABLE_METADATA_FIELDS}
ALTERNATE_EDITABLE_TAGS = {
    "title": ("EXIF:XPTitle", "IPTC:ObjectName", "PNG:Title"),
    "description": (
        "EXIF:ImageDescription",
        "EXIF:XPComment",
        "IPTC:Caption-Abstract",
        "PNG:Description",
    ),
    "artist": ("XMP-dc:Creator", "IPTC:By-line", "PNG:Author"),
    "copyright": ("XMP-dc:Rights", "IPTC:CopyrightNotice", "PNG:Copyright"),
    "keywords": ("IPTC:Keywords", "PNG:Keywords"),
    "rating": ("EXIF:Rating", "EXIF:RatingPercent"),
    "date_time_original": (
        "EXIF:CreateDate",
        "XMP-exif:DateTimeOriginal",
        "XMP-xmp:CreateDate",
        "IPTC:DateCreated",
        "IPTC:TimeCreated",
    ),
}
PRIVATE_TAG_NAMES = {
    "artist",
    "author",
    "by-line",
    "cameraownername",
    "creator",
    "creatorcontactinfo",
    "ownername",
    "serialnumber",
    "bodyserialnumber",
    "lensserialnumber",
    "usercomment",
}
LOCATION_ARGUMENTS = (
    "-GPS:All=",
    "-XMP-exif:GPS*=",
    "-XMP:Geotag=",
    "-XMP:Location=",
)
PRIVATE_ARGUMENTS = LOCATION_ARGUMENTS + (
    "-EXIF:Artist=",
    "-EXIF:CameraOwnerName=",
    "-EXIF:BodySerialNumber=",
    "-EXIF:LensSerialNumber=",
    "-EXIF:UserComment=",
    "-IPTC:By-line=",
    "-XMP-dc:Creator=",
    "-XMP-exifEX:CameraOwnerName=",
    "-XMP-exifEX:BodySerialNumber=",
    "-XMP-exifEX:LensSerialNumber=",
    "-XMP-aux:OwnerName=",
    "-XMP-aux:SerialNumber=",
    "-XMP-aux:LensSerialNumber=",
)
DATE_TIME_PATTERN = re.compile(r"^\d{4}:\d{2}:\d{2} \d{2}:\d{2}:\d{2}$")


@dataclass(frozen=True)
class MetadataEntry:
    group: str
    tag: str
    value: str

    @property
    def name(self) -> str:
        return f"{self.group}:{self.tag}" if self.group else self.tag


@dataclass(frozen=True)
class ImageInformation:
    path: Path
    file_size: int
    modified_time: float
    image_format: str
    dimensions: tuple[int, int]
    colour_mode: str
    frame_count: int
    entries: tuple[MetadataEntry, ...]
    editable_values: dict[str, str]
    exiftool_available: bool
    writable: bool
    privacy_entries: tuple[str, ...]
    metadata_error: str | None


def exiftool_path() -> str | None:
    return shutil.which("exiftool")


def _run_exiftool(arguments: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess:
    executable = exiftool_path()
    if executable is None:
        raise RuntimeError(
            "ExifTool is required for complete metadata reading and editing. "
            "Install libimage-exiftool-perl."
        )
    try:
        result = subprocess.run(
            [executable, *arguments],
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("ExifTool did not finish within 30 seconds.") from error
    if result.returncode:
        message = result.stderr.strip() or result.stdout.strip() or "ExifTool failed."
        raise RuntimeError(message)
    return result


def _display_value(value) -> str:
    if isinstance(value, list):
        return ", ".join(_display_value(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if value is None:
        return ""
    return str(value)


def _metadata_entries(path: Path, *, numeric: bool = False) -> tuple[MetadataEntry, ...]:
    arguments = ["-json", "-G1", "-s", "-sort"]
    if numeric:
        arguments.append("-n")
    result = _run_exiftool([*arguments, str(path)])
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("ExifTool returned invalid metadata.") from error
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise RuntimeError("ExifTool returned no metadata for this image.")
    entries = []
    for name, value in payload[0].items():
        if name == "SourceFile":
            continue
        group, separator, tag = name.partition(":")
        entries.append(
            MetadataEntry(
                group if separator else "",
                tag if separator else group,
                _display_value(value),
            )
        )
    return tuple(entries)


def _value_for_tags(entries: tuple[MetadataEntry, ...], *tags: str) -> str:
    wanted = {tag.casefold() for tag in tags}
    for entry in entries:
        if entry.tag.casefold() in wanted and entry.value:
            return entry.value
    return ""


def _value_for_field(entries: tuple[MetadataEntry, ...], name: str, *fallbacks: str) -> str:
    group, tag = EDITABLE_TAGS[name].split(":", 1)
    for entry in entries:
        if entry.group.casefold() == group.casefold() and entry.tag.casefold() == tag.casefold():
            return entry.value
    return _value_for_tags(entries, tag, *fallbacks)


def _signed_coordinate(
    entries: tuple[MetadataEntry, ...],
    coordinate_name: str,
    reference_tag: str,
    negative_reference: str,
) -> str:
    value = _value_for_field(entries, coordinate_name)
    if not value:
        return ""
    reference = _value_for_tags(entries, reference_tag).upper()
    if reference == negative_reference and not value.startswith("-"):
        return f"-{value}"
    return value


def _editable_values(entries: tuple[MetadataEntry, ...]) -> dict[str, str]:
    return {
        "title": _value_for_field(entries, "title", "ObjectName"),
        "description": _value_for_field(
            entries,
            "description",
            "ImageDescription",
            "Caption-Abstract",
        ),
        "artist": _value_for_field(entries, "artist", "Creator", "By-line"),
        "copyright": _value_for_field(entries, "copyright", "CopyrightNotice", "Rights"),
        "keywords": _value_for_field(entries, "keywords", "Keywords"),
        "rating": _value_for_field(entries, "rating"),
        "date_time_original": _value_for_field(entries, "date_time_original", "CreateDate"),
        "gps_latitude": _signed_coordinate(
            entries,
            "gps_latitude",
            "GPSLatitudeRef",
            "S",
        ),
        "gps_longitude": _signed_coordinate(
            entries,
            "gps_longitude",
            "GPSLongitudeRef",
            "W",
        ),
    }


def _privacy_entries(entries: tuple[MetadataEntry, ...]) -> tuple[str, ...]:
    private = []
    for entry in entries:
        normalised = entry.tag.casefold().replace(" ", "")
        if "gps" in normalised or normalised in PRIVATE_TAG_NAMES or "serialnumber" in normalised:
            private.append(entry.name)
    return tuple(dict.fromkeys(private))


def read_image_information(path: str | Path) -> ImageInformation:
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Image file is unavailable: {path}")
    stat = path.stat()
    available = exiftool_path() is not None
    metadata_error = None
    try:
        entries = _metadata_entries(path) if available else ()
        numeric_entries = _metadata_entries(path, numeric=True) if available else ()
    except RuntimeError as error:
        entries = ()
        numeric_entries = ()
        metadata_error = str(error)
    try:
        with Image.open(path) as image:
            image_format = image.format or path.suffix.removeprefix(".").upper()
            dimensions = image.size
            colour_mode = image.mode
            frame_count = max(1, int(getattr(image, "n_frames", 1)))
    except OSError:
        image_format = path.suffix.removeprefix(".").upper() or "Unknown"
        width = _value_for_tags(numeric_entries, "ImageWidth")
        height = _value_for_tags(numeric_entries, "ImageHeight")
        dimensions = (
            int(float(width)) if width else 0,
            int(float(height)) if height else 0,
        )
        colour_mode = _value_for_tags(entries, "ColorType", "ColorSpace") or "Unknown"
        frame_count = 1
    return ImageInformation(
        path=path,
        file_size=stat.st_size,
        modified_time=stat.st_mtime,
        image_format=image_format,
        dimensions=dimensions,
        colour_mode=colour_mode,
        frame_count=frame_count,
        entries=entries,
        editable_values=_editable_values(numeric_entries),
        exiftool_available=available,
        writable=(
            available
            and metadata_error is None
            and path.suffix.lower() in WRITABLE_METADATA_SUFFIXES
        ),
        privacy_entries=_privacy_entries(entries),
        metadata_error=metadata_error,
    )


def validate_editable_values(values: dict[str, str]) -> dict[str, str]:
    clean = {name: str(values.get(name, "")).strip() for name in EDITABLE_TAGS}
    rating = clean["rating"]
    if rating:
        try:
            numeric_rating = int(rating)
        except ValueError as error:
            raise ValueError("Rating must be a whole number from 0 to 5.") from error
        if not 0 <= numeric_rating <= 5:
            raise ValueError("Rating must be a whole number from 0 to 5.")
        clean["rating"] = str(numeric_rating)
    date_time = clean["date_time_original"]
    if date_time and DATE_TIME_PATTERN.fullmatch(date_time) is None:
        raise ValueError("Date and time must use YYYY:MM:DD HH:MM:SS.")
    for name, minimum, maximum in (
        ("gps_latitude", -90, 90),
        ("gps_longitude", -180, 180),
    ):
        value = clean[name]
        if not value:
            continue
        try:
            coordinate = float(value)
        except ValueError as error:
            raise ValueError(f"{name.replace('_', ' ').capitalize()} must be a decimal number.") from error
        if not math.isfinite(coordinate) or not minimum <= coordinate <= maximum:
            raise ValueError(
                f"{name.replace('_', ' ').capitalize()} must be between {minimum} and {maximum}."
            )
        clean[name] = str(coordinate)
    return clean


def describe_metadata_changes(
    old_values: dict[str, str],
    new_values: dict[str, str],
) -> list[str]:
    labels = {name: label for name, label, _tag in EDITABLE_METADATA_FIELDS}
    descriptions = []
    for name in EDITABLE_TAGS:
        old = old_values.get(name, "").strip()
        new = new_values.get(name, "").strip()
        if old == new:
            continue
        before = old or "not set"
        after = new or "removed"
        descriptions.append(f"{labels[name]}: {before} -> {after}")
    return descriptions


def apply_metadata_edit(path: str | Path, values: dict[str, str]) -> None:
    path = Path(path)
    clean = validate_editable_values(values)
    arguments = ["-overwrite_original"]
    for name, value in clean.items():
        tag = EDITABLE_TAGS[name]
        arguments.extend(
            f"-{alternate_tag}="
            for alternate_tag in ALTERNATE_EDITABLE_TAGS.get(name, ())
        )
        if name == "keywords":
            arguments.append(f"-{tag}=")
            keywords = [keyword.strip() for keyword in value.split(",") if keyword.strip()]
            arguments.extend(f"-{tag}+={keyword}" for keyword in keywords)
        elif name in {"gps_latitude", "gps_longitude"}:
            reference_tag = (
                "EXIF:GPSLatitudeRef"
                if name == "gps_latitude"
                else "EXIF:GPSLongitudeRef"
            )
            if value:
                coordinate = float(value)
                reference = (
                    "S" if coordinate < 0 else "N"
                ) if name == "gps_latitude" else (
                    "W" if coordinate < 0 else "E"
                )
                arguments.extend(
                    (f"-{tag}={abs(coordinate)}", f"-{reference_tag}={reference}")
                )
            else:
                arguments.extend((f"-{tag}=", f"-{reference_tag}="))
        else:
            arguments.append(f"-{tag}={value}")
    arguments.append(str(path))
    _run_exiftool(arguments)
    validate_metadata_result(path)


def remove_metadata(path: str | Path, scope: str) -> None:
    path = Path(path)
    if scope == "location":
        arguments = list(LOCATION_ARGUMENTS)
    elif scope == "private":
        arguments = list(PRIVATE_ARGUMENTS)
    elif scope == "all":
        arguments = ["-All=", "--ICC_Profile:All", "-TagsFromFile", "@", "-ColorSpaceTags"]
    else:
        raise ValueError(f"Unknown metadata-removal scope: {scope}")
    _run_exiftool(["-overwrite_original", *arguments, str(path)])
    validate_metadata_result(path)


def validate_metadata_result(path: str | Path) -> None:
    path = Path(path)
    with Image.open(path) as image:
        image.verify()
    _metadata_entries(path)

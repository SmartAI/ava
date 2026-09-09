"""Shared, bounded image-header inspection for attachments and tool output."""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Callable
from dataclasses import dataclass, field

from ava.base.errors import AvaError, ErrorKind


@dataclass(slots=True, eq=False)
class DeferredImage:
    """A verified byte value whose backing storage is read only when requested."""

    size: int
    digest: str
    read: Callable[[], bytes] = field(repr=False)

    def __len__(self) -> int:
        return self.size

    def __bytes__(self) -> bytes:
        data = self.read()
        if len(data) != self.size or hashlib.sha256(data).hexdigest() != self.digest:
            raise AvaError(ErrorKind.io, "Saved image failed its integrity check")
        return data

    def __eq__(self, other: object) -> bool:
        if isinstance(other, DeferredImage):
            return self.size == other.size and self.digest == other.digest
        if isinstance(other, bytes):
            return self.size == len(other) and self.digest == hashlib.sha256(other).hexdigest()
        return NotImplemented

IMAGE_BYTE_LIMIT = 7_500_000
IMAGE_DIMENSION_LIMIT = 8000

_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


@dataclass(slots=True)
class ImageInfo:
    media_type: str
    width: int
    height: int


def _invalid(message: str) -> AvaError:
    return AvaError(ErrorKind.invalid_argument, message)


def expected_media_type(extension: str) -> str:
    return _MEDIA_TYPES.get(extension.lower(), "")


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    # Only segment headers are walked; compressed scan data is never decoded.
    offset = 2
    while offset < len(data):
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker in (0xD9, 0xDA):
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:
            continue
        if offset + 2 > len(data):
            break
        (length,) = struct.unpack(">H", data[offset : offset + 2])
        is_sof = marker in (
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        )
        if length < 2 or offset + length > len(data):
            break
        if is_sof and length >= 7:
            height, width = struct.unpack(">HH", data[offset + 3 : offset + 7])
            return width, height
        offset += length
    return None


def sniff_image(data: bytes, extension: str) -> ImageInfo:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        if len(data) < 24 or struct.unpack(">I", data[8:12])[0] != 13 or data[12:16] != b"IHDR":
            raise _invalid("truncated PNG header")
        width, height = struct.unpack(">II", data[16:24])
        info = ImageInfo("image/png", width, height)
    elif data[:6] in (b"GIF87a", b"GIF89a"):
        if len(data) < 10:
            raise _invalid("truncated GIF header")
        width, height = struct.unpack("<HH", data[6:10])
        info = ImageInfo("image/gif", width, height)
    elif data[:2] == b"\xff\xd8":
        dimensions = _jpeg_dimensions(data)
        if dimensions is None:
            raise _invalid("truncated JPEG header before dimensions")
        info = ImageInfo("image/jpeg", *dimensions)
    elif len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        chunk = data[12:16]
        size = struct.unpack("<I", data[16:20])[0] if len(data) >= 20 else 0
        if chunk == b"VP8X" and len(data) >= 30 and size >= 10:
            width = int.from_bytes(data[24:27], "little") + 1
            height = int.from_bytes(data[27:30], "little") + 1
        elif chunk == b"VP8L" and len(data) >= 25 and size >= 5 and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
        elif chunk == b"VP8 " and len(data) >= 30 and size >= 10 and data[23:26] == b"\x9d\x01\x2a":
            width = struct.unpack("<H", data[26:28])[0] & 0x3FFF
            height = struct.unpack("<H", data[28:30])[0] & 0x3FFF
        else:
            raise _invalid("truncated or unsupported WebP header")
        info = ImageInfo("image/webp", width, height)
    else:
        raise _invalid("unsupported image header; expected PNG, JPEG, GIF, or WebP")

    normalized = extension.lower()
    expected = expected_media_type(normalized)
    if not expected:
        raise _invalid(
            f"unsupported image extension '{normalized}'; expected .png, .jpg, .jpeg, .gif, or .webp"
        )
    if expected != info.media_type:
        raise _invalid(
            f"extension '{normalized}' requires {expected} but the header is {info.media_type}"
        )
    if info.width == 0 or info.height == 0:
        raise _invalid("image dimensions must each be at least 1 pixel")
    if info.width > IMAGE_DIMENSION_LIMIT or info.height > IMAGE_DIMENSION_LIMIT:
        name, value = (
            ("width", info.width) if info.width > IMAGE_DIMENSION_LIMIT else ("height", info.height)
        )
        raise _invalid(f"image {name} {value} exceeds the 8000-pixel limit")
    return info

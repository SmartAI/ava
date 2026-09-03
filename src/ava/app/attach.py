"""Attachment limits, strict base64 decoding, UTF-8 validation, and image-header sniffing.

Header-only inspection rejects mislabeled or unusable images before any provider I/O.
"""

from __future__ import annotations

import base64
import binascii
import struct
from dataclasses import dataclass
from pathlib import Path

from ava.base import AvaError, ErrorKind
from ava.llm import ContentBlock, make_file_text_block, make_image_block

IMAGE_BYTE_LIMIT = 7_500_000
TEXT_LIMIT = 50 * 1024
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


def valid_utf8_prefix(data: bytes, limit: int) -> int | None:
    """The longest valid UTF-8 prefix within ``limit`` bytes, or None when the data is invalid."""
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if len(data) <= limit:
        return len(data)
    cut = limit
    while cut > 0 and (data[cut] & 0xC0) == 0x80:
        cut -= 1
    return cut


def decode_base64(encoded: str) -> bytes:
    if len(encoded) % 4 != 0:
        raise _invalid("data_base64 must use strict padded base64")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise _invalid("data_base64 must use strict padded base64") from None
    if base64.b64encode(decoded).decode("ascii") != encoded:
        raise _invalid("data_base64 must use strict padded base64")
    return decoded


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


def truncated_text(text: bytes) -> str:
    """Retain the greatest complete-line prefix within the text limit, with an exact marker."""
    if len(text) <= TEXT_LIMIT:
        return text.decode("utf-8")
    total_lines = text.count(b"\n") + (0 if text.endswith(b"\n") else 1)
    last_newline = text.rfind(b"\n", 0, TEXT_LIMIT)
    if last_newline != -1:
        retained = text[: last_newline + 1]
        retained_lines = retained.count(b"\n")
        return retained.decode("utf-8") + (
            f"[truncated after {retained_lines} of {total_lines} lines; use read on the path above "
            f"with offset {retained_lines + 1} to continue]"
        )
    retained_bytes = valid_utf8_prefix(text, TEXT_LIMIT) or 0
    return text[:retained_bytes].decode("utf-8") + (
        f"\n[truncated within line 1 after {retained_bytes} of {len(text)} bytes; continuation is "
        f"not line-addressable; use bash tail -c +{retained_bytes + 1} -- on the path above]"
    )


def load_attachment(cwd: Path, raw_path: str, *, image: bool, root: Path) -> ContentBlock:
    """Load a CLI attachment. Paths must stay inside the invocation root."""
    requested = Path(raw_path)
    resolved = (requested if requested.is_absolute() else cwd / requested).resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        raise _invalid(f"attachment '{raw_path}' resolves outside the invocation root") from None
    if not resolved.is_file():
        raise _invalid(f"attachment '{raw_path}' is not a readable regular file")
    display_path = relative.as_posix()
    try:
        data = resolved.read_bytes()
    except OSError:
        raise _invalid(f"cannot read attachment '{raw_path}'") from None
    if data.startswith(b"%PDF-"):
        raise _invalid(f"attachment '{display_path}' is a PDF; PDF attachments are not supported")
    if image:
        if len(data) > IMAGE_BYTE_LIMIT:
            raise _invalid(
                f"image attachment '{display_path}' exceeds the {IMAGE_BYTE_LIMIT}-byte limit (actual: {len(data)} bytes)"
            )
        try:
            info = sniff_image(data, resolved.suffix)
        except AvaError as error:
            raise _invalid(f"image attachment '{display_path}': {error.message}") from None
        return make_image_block(display_path, data, info.media_type)
    if valid_utf8_prefix(data, len(data)) != len(data):
        raise _invalid(f"text attachment '{display_path}' is not valid UTF-8")
    return make_file_text_block(display_path, truncated_text(data))

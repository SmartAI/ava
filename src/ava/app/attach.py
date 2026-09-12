"""Attachment limits, strict base64 decoding, UTF-8 validation, and image-header sniffing.

Header-only inspection rejects mislabeled or unusable images before any provider I/O.
"""

from __future__ import annotations

import base64
import binascii
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from ava.base import AvaError, ErrorKind
from ava.base.images import (
    IMAGE_BYTE_LIMIT as IMAGE_BYTE_LIMIT,
)
from ava.base.images import (
    IMAGE_DIMENSION_LIMIT as IMAGE_DIMENSION_LIMIT,
)
from ava.base.images import (
    ImageInfo as ImageInfo,
)
from ava.base.images import (
    expected_media_type as expected_media_type,
)
from ava.base.images import (
    sniff_image as sniff_image,
)
from ava.llm import ContentBlock, ContentBlockKind, make_file_text_block, make_image_block

TEXT_LIMIT = 50 * 1024
PDF_BYTE_LIMIT = 8 * 1024 * 1024
PDF_PAGE_LIMIT = 100


def is_pdf(name: str, data: bytes) -> bool:
    return Path(name).suffix.lower() == ".pdf" or data.startswith(b"%PDF-")


def load_pdf(name: str, data: bytes) -> ContentBlock:
    """Validate a bounded PDF without extracting or losing page imagery."""
    if len(data) > PDF_BYTE_LIMIT:
        raise _invalid(f"PDF attachment '{name}' exceeds the 8 MiB limit")
    if not data.startswith(b"%PDF-"):
        raise _invalid(f"PDF attachment '{name}' is not a valid PDF")
    try:
        reader = PdfReader(BytesIO(data), strict=True)
        if reader.is_encrypted:
            raise _invalid(f"PDF attachment '{name}' is password protected; attach an unlocked copy")
        pages = len(reader.pages)
    except (PyPdfError, ValueError, OSError, RecursionError) as error:
        raise _invalid(f"PDF attachment '{name}' is not a valid PDF") from error
    if not 1 <= pages <= PDF_PAGE_LIMIT:
        raise _invalid(f"PDF attachment '{name}' must contain 1–{PDF_PAGE_LIMIT} pages")
    return ContentBlock(
        kind=ContentBlockKind.pdf, display_path=name, bytes=data,
        media_type="application/pdf", page_count=pages,
    )


def _invalid(message: str) -> AvaError:
    return AvaError(ErrorKind.invalid_argument, message)


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
    if not image and is_pdf(display_path, data):
        return load_pdf(display_path, data)
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

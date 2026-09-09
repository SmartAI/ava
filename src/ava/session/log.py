"""The durable append-only session log.

Logical format: one JSON object per line. Physical encodings: plain ``.jsonl`` and the default
``.jsonl.zst``, a concatenation of independent checksummed Zstandard frames. The first frame holds
only ``session/start``; every later frame holds one complete append batch.

Crash story: appends are write-all on an ``O_APPEND`` descriptor, a caught partial append is rolled
back to the prior frame boundary, and only an incomplete final physical unit is ever replaced.
"""

from __future__ import annotations

import fcntl
import hashlib
import io
import os
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from functools import partial
from pathlib import Path

import zstandard

from ava.base import AvaError, ErrorKind, ava_home
from ava.base.images import DeferredImage
from ava.session import codec
from ava.session.event import Event, EventPayload, SessionStart, ToolResult, Unknown, now_ms
from ava.session.recovery import plan_lifecycle_repair

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
MAX_DECODED_BATCH_BYTES = codec.MAX_RECORD_BYTES + 64 * 1024
MAX_WINDOW_LOG = 27  # 128 MiB, comfortably above any legal batch


class OpenMode(StrEnum):
    repair = "repair"
    read_only = "read_only"


class PhysicalEncoding(StrEnum):
    zstd = "zstd"
    plain = "plain"


def _io_error(message: str, error: OSError) -> AvaError:
    return AvaError(ErrorKind.io, message, error.strerror or str(error))


# ---- Identity and paths -----------------------------------------------------------------------


def fnv1a_64(data: bytes) -> int:
    value = 14695981039346656037
    for byte in data:
        value ^= byte
        value = (value * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return value


def canonical_working_directory(path: Path) -> Path:
    try:
        canonical = path.resolve(strict=True)
    except OSError as error:
        raise AvaError(
            ErrorKind.invalid_argument, f"cannot resolve working directory '{path}'", str(error)
        ) from error
    if not canonical.is_absolute() or not canonical.is_dir():
        raise AvaError(ErrorKind.invalid_argument, f"cannot resolve working directory '{path}'")
    return canonical


def bucket_name(canonical: Path) -> str:
    return f"{fnv1a_64(os.fsencode(canonical)):016x}"


def project_bucket(path: Path) -> str:
    return bucket_name(canonical_working_directory(path))


def new_ulid() -> str:
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    timestamp = int(time.time() * 1000)
    if timestamp < 0 or timestamp >= 1 << 48:
        raise AvaError(
            ErrorKind.internal, "cannot generate session id", "clock is outside the ULID range"
        )
    value = (timestamp << 80) | int.from_bytes(secrets.token_bytes(10), "big")
    return "".join(alphabet[(value >> (5 * (25 - index))) & 31] for index in range(26))


def default_session_root() -> Path:
    home = ava_home()
    root = home / "sessions"
    user_home = os.environ.get("HOME")
    if os.environ.get("AVA_HOME") or not user_home:
        return root
    legacy = Path(user_home) / ".local/state/ava/sessions"
    if not legacy.exists():
        return root
    if root.exists():
        raise AvaError(
            ErrorKind.io,
            "legacy sessions require manual migration",
            f"move '{legacy}' into '{root}'",
        )
    _ensure_private_directory(home, preserve_existing=True)
    try:
        legacy.rename(root)
    except OSError as error:
        raise AvaError(
            ErrorKind.io, "cannot migrate legacy sessions", f"move '{legacy}' to '{root}': {error}"
        ) from error
    return root


def _ensure_private_directory(path: Path, *, preserve_existing: bool = False) -> None:
    try:
        existed = path.exists()
        path.mkdir(parents=True, exist_ok=True)
        if existed and preserve_existing:
            return
        os.chmod(path, 0o700)
    except OSError as error:
        raise _io_error(f"cannot create session directory '{path}'", error) from error


@dataclass(slots=True)
class SessionCandidate:
    path: Path
    header: SessionStart


def _discover_session_directory(directory: Path, canonical: Path) -> list[SessionCandidate]:
    candidates: list[SessionCandidate] = []
    try:
        entries = sorted(directory.iterdir())
    except OSError as error:
        raise _io_error(f"cannot list session directory '{directory}'", error) from error
    for path in entries:
        name = path.name
        if not path.is_file() or not (name.endswith(".jsonl.zst") or name.endswith(".jsonl")):
            continue
        header = Log.read_header(path)
        if header.cwd == str(canonical):
            candidates.append(SessionCandidate(path=path, header=header))
    return candidates


def discover_sessions_in(state_root: Path, cwd: Path) -> list[SessionCandidate]:
    """Header identity is verified so a bucket collision never selects another directory."""
    canonical = canonical_working_directory(cwd)
    directory = state_root / bucket_name(canonical)
    if not directory.exists():
        return []
    candidates = _discover_session_directory(directory, canonical)
    candidates.sort(key=lambda candidate: candidate.header.id, reverse=True)
    return candidates


def discover_all_sessions_in(state_root: Path) -> list[SessionCandidate]:
    """Discover default logs across live working directories, rejecting misplaced headers."""
    if not state_root.exists():
        return []
    try:
        directories = sorted(state_root.iterdir())
    except OSError as error:
        raise _io_error(f"cannot list session root '{state_root}'", error) from error
    candidates: list[SessionCandidate] = []
    for directory in directories:
        if not directory.is_dir():
            continue
        try:
            entries = sorted(directory.iterdir())
        except OSError as error:
            raise _io_error(f"cannot list session directory '{directory}'", error) from error
        for path in entries:
            name = path.name
            if not path.is_file() or not (
                name.endswith(".jsonl.zst") or name.endswith(".jsonl")
            ):
                continue
            header = Log.read_header(path)
            try:
                canonical = canonical_working_directory(Path(header.cwd))
            except AvaError:
                continue
            if directory.name != bucket_name(canonical) or header.cwd != str(canonical):
                continue
            candidates.append(SessionCandidate(path=path, header=header))
    candidates.sort(key=lambda candidate: candidate.header.id, reverse=True)
    return candidates


# ---- Physical encoding --------------------------------------------------------------------------


def _encoding_for_suffix(name: str) -> PhysicalEncoding | None:
    if name.endswith(".jsonl.zst"):
        return PhysicalEncoding.zstd
    if name.endswith(".jsonl"):
        return PhysicalEncoding.plain
    return None


def _suffix_matches(path: Path, encoding: PhysicalEncoding) -> bool:
    return _encoding_for_suffix(path.name) == encoding


def encode_frame(batch: bytes) -> bytes:
    compressor = zstandard.ZstdCompressor(write_checksum=True, write_content_size=True)
    return compressor.compress(batch)


def _encode_physical(encoding: PhysicalEncoding, batch: bytes) -> bytes:
    if not batch or len(batch) > MAX_DECODED_BATCH_BYTES or not batch.endswith(b"\n"):
        raise AvaError(
            ErrorKind.invalid_argument,
            "cannot encode session batch",
            "batch must be bounded and LF-terminated",
        )
    return encode_frame(batch) if encoding == PhysicalEncoding.zstd else batch


@dataclass(slots=True)
class _PhysicalScan:
    records: list[str] = field(default_factory=list)
    unit_count: int = 0
    torn_offset: int | None = None
    retained_batch: bytes = b""
    end_offset: int = 0


def _split_records(batch: bytes, *, allow_partial_tail: bool) -> tuple[list[str], bytes]:
    """Complete LF-terminated records and the retained complete prefix."""
    records: list[str] = []
    end = batch.rfind(b"\n")
    complete = batch[: end + 1] if end != -1 else b""
    if not allow_partial_tail and complete != batch:
        raise AvaError(
            ErrorKind.parse, "invalid Zstandard session frame", "decoded batch is not LF-terminated"
        )
    for line in complete.split(b"\n")[:-1]:
        if not line:
            raise AvaError(
                ErrorKind.parse, "invalid session log", "session log contains an empty record"
            )
        if len(line) > codec.MAX_RECORD_BYTES:
            raise AvaError(
                ErrorKind.parse, "invalid session log", "complete record exceeds the size limit"
            )
        records.append(line.decode("utf-8"))
    return records, complete


def _scan_plain(
    data: bytes | None, *, header_only: bool, fd: int = -1,
    consume: Callable[[str, int, int, str], None] | None = None,
    start_offset: int = 0, unit_limit: int | None = None,
) -> _PhysicalScan:
    scan = _PhysicalScan(end_offset=start_offset)
    offset = start_offset
    size = len(data) if data is not None else os.fstat(fd).st_size
    with io.BytesIO(data) if data is not None else os.fdopen(os.dup(fd), "rb") as source:
        source.seek(offset)
        while offset < size:
            line = source.readline(min(codec.MAX_RECORD_BYTES + 2, size - offset))
            if not line:
                raise AvaError(ErrorKind.io, "Session log changed while it was being read")
            if not line.endswith(b"\n"):
                # A long complete record is corruption; only an unfinished final
                # record may be discarded. Scan that tail without accumulating it.
                while source.tell() < size:
                    tail = source.readline(min(64 * 1024, size - source.tell()))
                    if not tail:
                        raise AvaError(ErrorKind.io, "Session log changed while it was being read")
                    if tail.endswith(b"\n"):
                        raise AvaError(ErrorKind.parse, "invalid plain session log", "complete record exceeds the size limit")
                scan.torn_offset = offset
                break
            if line == b"\n":
                raise AvaError(ErrorKind.parse, "invalid plain session log", "session log contains an empty record")
            if len(line) - 1 > codec.MAX_RECORD_BYTES:
                raise AvaError(ErrorKind.parse, "invalid plain session log", "complete record exceeds the size limit")
            record = line[:-1].decode("utf-8")
            if consume is None:
                scan.records.append(record)
            else:
                consume(record, offset, len(line), hashlib.sha256(line).hexdigest())
            scan.unit_count += 1
            offset += len(line)
            scan.end_offset = offset
            if header_only or (unit_limit is not None and scan.unit_count >= unit_limit):
                return scan
    if not offset and scan.torn_offset is None:
        raise AvaError(ErrorKind.parse, "invalid plain session log", "session file is empty")
    return scan


_ZSTD_SCAN_CHUNK_BYTES = 64 * 1024


def _scan_zstd(
    data: bytes | None, *, header_only: bool, fd: int = -1,
    consume: Callable[[str, int, int, str], None] | None = None,
    start_offset: int = 0, unit_limit: int | None = None,
) -> _PhysicalScan:
    size = len(data) if data is not None else os.fstat(fd).st_size
    if not size:
        raise AvaError(ErrorKind.parse, "invalid session log", "session file is empty")

    def read(start: int, end: int) -> bytes:
        if data is not None:
            return data[start:end]
        part = os.pread(fd, end - start, start)
        if len(part) != end - start:
            raise AvaError(ErrorKind.io, "Session log changed while it was being read")
        return part
    scan = _PhysicalScan(end_offset=start_offset)
    decompressor = zstandard.ZstdDecompressor(max_window_size=1 << MAX_WINDOW_LOG)
    offset = start_offset
    while offset < size:
        frame_offset = offset
        decoder = decompressor.decompressobj()
        fingerprint = hashlib.sha256()
        decoded_parts: list[bytes] = []
        decoded_size = 0
        try:
            while offset < size and not decoder.eof:
                end = min(offset + _ZSTD_SCAN_CHUNK_BYTES, size)
                encoded = read(offset, end)
                part = decoder.decompress(encoded)
                fingerprint.update(encoded[:-len(decoder.unused_data)] if decoder.unused_data else encoded)
                decoded_parts.append(part)
                decoded_size += len(part)
                if decoded_size > MAX_DECODED_BATCH_BYTES:
                    raise AvaError(
                        ErrorKind.parse,
                        "invalid Zstandard session frame",
                        "frame exceeds the decoded batch limit",
                    )
                offset = end
            if decoder.eof:
                offset -= len(decoder.unused_data)
        except zstandard.ZstdError as error:
            # A frame that cannot be decoded at all is corruption, not an interruption, unless it
            # is the unfinished header of the final frame.
            remaining = read(frame_offset, min(frame_offset + 18, size))
            if remaining[:4] == ZSTD_MAGIC and len(remaining) < 18 and _header_is_short(remaining):
                scan.torn_offset = frame_offset
                break
            raise AvaError(
                ErrorKind.parse, "corrupt Zstandard session frame", str(error)
            ) from error
        decoded = b"".join(decoded_parts)
        if not decoder.eof:
            # Only the final physical unit can be torn; keep its complete-record prefix.
            scan.torn_offset = frame_offset
            _, scan.retained_batch = _split_records(decoded, allow_partial_tail=True)
            break
        parameters = zstandard.get_frame_parameters(read(frame_offset, min(frame_offset + 18, size)))
        if not parameters.has_checksum or parameters.content_size == zstandard.CONTENTSIZE_UNKNOWN:
            raise AvaError(
                ErrorKind.parse,
                "invalid Zstandard session frame",
                "frame lacks a content size or checksum",
            )
        if parameters.window_size > (1 << MAX_WINDOW_LOG):
            raise AvaError(
                ErrorKind.parse,
                "invalid Zstandard session frame",
                "frame declares an excessive window",
            )
        records, _ = _split_records(decoded, allow_partial_tail=False)
        if consume is None:
            scan.records.extend(records)
        else:
            for record in records:
                consume(record, frame_offset, offset - frame_offset, fingerprint.hexdigest())
        scan.unit_count += 1
        scan.end_offset = offset
        if header_only or (unit_limit is not None and scan.unit_count >= unit_limit):
            return scan
    return scan


def _header_is_short(data: bytes) -> bool:
    try:
        zstandard.get_frame_parameters(data)
    except zstandard.ZstdError:
        return True
    return False


def _detect_encoding(data: bytes) -> PhysicalEncoding:
    if not data:
        raise AvaError(ErrorKind.parse, "invalid session log", "session file is empty")
    return PhysicalEncoding.zstd if data[:4] == ZSTD_MAGIC else PhysicalEncoding.plain


_IMAGE_CACHE: OrderedDict[tuple, dict[tuple[int, int, int], bytes]] = OrderedDict()
_IMAGE_CACHE_BYTES = 0
_IMAGE_CACHE_LOCK = threading.Lock()
_IMAGE_CACHE_LIMIT = 8 * 1024 * 1024


def _image_frame(
    path: Path, device: int, inode: int, offset: int, length: int, encoding: PhysicalEncoding, fingerprint: str,
) -> dict[tuple[int, int, int], bytes]:
    """An 8 MiB cache shares immutable image batches between model and preview requests."""
    global _IMAGE_CACHE_BYTES
    cache_key = path, device, inode, offset, length, encoding, fingerprint
    with _IMAGE_CACHE_LOCK:
        if cache_key in _IMAGE_CACHE:
            _IMAGE_CACHE.move_to_end(cache_key)
            return _IMAGE_CACHE[cache_key]
    fd = _open_existing(path, OpenMode.read_only)
    try:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != (device, inode):
            raise AvaError(ErrorKind.io, "Saved image source was replaced; reopen the conversation")
        data = os.pread(fd, length, offset)
        if hashlib.sha256(data).hexdigest() != fingerprint:
            raise AvaError(ErrorKind.io, "Saved image source changed; reopen the conversation")
        if len(data) != length:
            raise AvaError(ErrorKind.io, "Saved image source is incomplete; reopen the conversation")
    finally:
        os.close(fd)
    scan = (_scan_zstd(data, header_only=False) if encoding == PhysicalEncoding.zstd
            else _scan_plain(data, header_only=False))
    if scan.torn_offset is not None:
        raise AvaError(ErrorKind.io, "Saved image source is incomplete; reopen the conversation")
    images = {}
    for record in scan.records:
        event = codec.decode_record(record)
        if isinstance(event.payload, ToolResult):
            for block_index, block in enumerate(event.payload.item.blocks):
                for image_index, image in enumerate(block.attachments):
                    images[event.seq, block_index, image_index] = bytes(image.bytes)
    size = sum(len(image) for image in images.values())
    if size <= _IMAGE_CACHE_LIMIT:
        with _IMAGE_CACHE_LOCK:
            if cache_key not in _IMAGE_CACHE:
                while _IMAGE_CACHE and (_IMAGE_CACHE_BYTES + size > _IMAGE_CACHE_LIMIT or len(_IMAGE_CACHE) >= 16):
                    _, removed = _IMAGE_CACHE.popitem(last=False)
                    _IMAGE_CACHE_BYTES -= sum(len(image) for image in removed.values())
                _IMAGE_CACHE[cache_key] = images
                _IMAGE_CACHE_BYTES += size
    return images


def _read_saved_image(
    path: Path, device: int, inode: int, offset: int, length: int, encoding: PhysicalEncoding,
    key: tuple[int, int, int], fingerprint: str,
) -> bytes:
    try:
        return _image_frame(path, device, inode, offset, length, encoding, fingerprint)[key]
    except KeyError:
        raise AvaError(ErrorKind.io, "Saved image is no longer available; reopen the conversation") from None
    except OSError as error:
        raise _io_error("Cannot read saved image", error) from error


def _defer_tool_images(
    event: Event, path: Path, info: os.stat_result, offset: int, length: int,
    encoding: PhysicalEncoding, fingerprint: str,
) -> Event:
    payload = event.payload
    if not isinstance(payload, ToolResult) or not any(block.attachments for block in payload.item.blocks):
        return event
    blocks = []
    for block_index, block in enumerate(payload.item.blocks):
        images = []
        for image_index, image in enumerate(block.attachments):
            digest = image.bytes.digest if isinstance(image.bytes, DeferredImage) else hashlib.sha256(image.bytes).hexdigest()
            source = DeferredImage(len(image.bytes), digest, partial(
                _read_saved_image, path, info.st_dev, info.st_ino, offset, length, encoding,
                (event.seq, block_index, image_index), fingerprint,
            ))
            images.append(replace(image, bytes=source))
        blocks.append(replace(block, attachments=images) if images else block)
    return replace(event, payload=replace(payload, item=replace(payload.item, blocks=blocks)))


# ---- The log ---------------------------------------------------------------------------------


class Log:
    """Owns the file, its writer lock, physical encoding, and decoded startup events."""

    def __init__(
        self,
        fd: int,
        path: Path,
        encoding: PhysicalEncoding,
        next_seq: int,
        loaded: list[Event],
    ) -> None:
        self._fd = fd
        self._path = path
        self._encoding = encoding
        self._next_seq = next_seq
        self._loaded = loaded
        self._poisoned = False
        self._ready_for_resume = False

    # ---- construction ---------------------------------------------------------------------

    @staticmethod
    def _header(
        canonical_cwd: Path, provider: str, model: str, labels: dict[str, str] | None
    ) -> SessionStart:
        return SessionStart(
            id=new_ulid(),
            cwd=str(canonical_cwd),
            provider=provider,
            model=model,
            format=1,
            labels=dict(labels or {}),
        )

    @classmethod
    def create(
        cls, path: Path, header: SessionStart, encoding: PhysicalEncoding = PhysicalEncoding.zstd
    ) -> Log:
        if not _suffix_matches(path, encoding):
            raise AvaError(
                ErrorKind.invalid_argument,
                "cannot create session log",
                "path suffix disagrees with physical encoding",
            )
        event = Event(seq=0, at=now_ms(), payload=header)
        physical = _encode_physical(encoding, (codec.encode_record(event) + "\n").encode("utf-8"))
        fd = _create_file_atomically(path, physical)
        return cls(fd, path, encoding, 1, [event])

    @classmethod
    def create_at(cls, path: Path, cwd: Path, provider: str, model: str) -> Log:
        encoding = _encoding_for_suffix(path.name)
        if encoding is None:
            raise AvaError(
                ErrorKind.invalid_argument, "explicit session path must end in .jsonl.zst or .jsonl"
            )
        canonical = canonical_working_directory(cwd)
        return cls.create(path, cls._header(canonical, provider, model, None), encoding)

    @classmethod
    def create_in(
        cls,
        state_root: Path,
        cwd: Path,
        provider: str,
        model: str,
        labels: dict[str, str] | None = None,
    ) -> Log:
        canonical = canonical_working_directory(cwd)
        _ensure_private_directory(state_root)
        directory = state_root / bucket_name(canonical)
        _ensure_private_directory(directory)
        header = cls._header(canonical, provider, model, labels)
        return cls.create(directory / f"{header.id}.jsonl.zst", header)

    @classmethod
    def create_default(
        cls, cwd: Path, provider: str, model: str, labels: dict[str, str] | None = None
    ) -> Log:
        root = default_session_root()
        _ensure_private_directory(root.parent, preserve_existing=True)
        return cls.create_in(root, cwd, provider, model, labels)

    @classmethod
    def open(
        cls, path: Path, mode: OpenMode = OpenMode.repair, expected_cwd: Path | None = None
    ) -> Log:
        fd = _open_existing(path, mode)
        try:
            encoding = _detect_encoding(os.pread(fd, 4, 0))
            if not _suffix_matches(path, encoding):
                raise AvaError(
                    ErrorKind.parse,
                    "invalid session log",
                    "path suffix disagrees with detected encoding",
                )
            loaded: list[Event] = []
            info = os.fstat(fd)
            source_path = path.absolute()

            def consume(record: str, offset: int, length: int, fingerprint: str) -> None:
                event = _load_record(record, len(loaded))
                loaded.append(_defer_tool_images(event, source_path, info, offset, length, encoding, fingerprint))

            scan = (
                _scan_zstd(None, header_only=False, fd=fd, consume=consume)
                if encoding == PhysicalEncoding.zstd
                else _scan_plain(None, header_only=False, fd=fd, consume=consume)
            )
            if not loaded:
                raise AvaError(
                    ErrorKind.parse, "invalid session log", "session has no complete header"
                )
            header = loaded[0].payload
            assert isinstance(header, SessionStart)
            _validate_format(header, path)
            if expected_cwd is not None:
                canonical = canonical_working_directory(expected_cwd)
                if header.cwd != str(canonical):
                    raise AvaError(
                        ErrorKind.invalid_argument,
                        "session belongs to a different working directory",
                        header.cwd,
                    )
            retained: list[Event] = []
            if scan.retained_batch:
                for record in _split_records(scan.retained_batch, allow_partial_tail=False)[0]:
                    retained.append(_load_record(record, len(loaded) + len(retained)))
            if scan.torn_offset is not None and mode == OpenMode.repair:
                _repair_tail(fd, encoding, scan)
                length = os.fstat(fd).st_size - scan.torn_offset
                fingerprint = hashlib.sha256(os.pread(fd, length, scan.torn_offset)).hexdigest()
                retained = [_defer_tool_images(event, source_path, info, scan.torn_offset, length, encoding, fingerprint)
                            for event in retained]
            # A read-only torn suffix has no immutable frame location yet. Keep its
            # bounded complete prefix in memory until the next reopen/repair.
            loaded.extend(retained)
        except BaseException:
            os.close(fd)
            raise
        if mode == OpenMode.read_only:
            os.close(fd)
            fd = -1
        log = cls(fd, path, encoding, len(loaded), loaded)
        if mode == OpenMode.repair:
            repair = plan_lifecycle_repair(log._loaded)
            if repair:
                appended = log.append_batch(repair)
                log.sync()
                log._loaded.extend(appended)
            log._ready_for_resume = expected_cwd is not None
        return log

    @classmethod
    def read_header(cls, path: Path) -> SessionStart:
        fd = _open_existing(path, OpenMode.read_only)
        try:
            encoding = _detect_encoding(os.pread(fd, 4, 0))
            if not _suffix_matches(path, encoding):
                raise AvaError(ErrorKind.parse, "invalid session log", "path suffix disagrees with detected encoding")
            scan = (
                _scan_zstd(None, header_only=True, fd=fd)
                if encoding == PhysicalEncoding.zstd
                else _scan_plain(None, header_only=True, fd=fd)
            )
        finally:
            os.close(fd)
        if not scan.records:
            raise AvaError(
                ErrorKind.parse, "invalid session log", "first record is not session/start"
            )
        event = codec.decode_record(scan.records[0])
        if event.seq != 0 or not isinstance(event.payload, SessionStart):
            raise AvaError(
                ErrorKind.parse, "invalid session log", "first record is not session/start"
            )
        _validate_format(event.payload, path)
        return event.payload

    # ---- appending --------------------------------------------------------------------------

    def _check_writable(self) -> None:
        self._ready_for_resume = False
        if self._fd < 0:
            raise AvaError(
                ErrorKind.permission, "cannot append session event", "session log is open read-only"
            )
        if self._poisoned:
            raise AvaError(ErrorKind.io, "cannot append session event", "writer is poisoned")

    @staticmethod
    def _check_payload(payload: EventPayload) -> None:
        if isinstance(payload, Unknown | SessionStart):
            raise AvaError(
                ErrorKind.invalid_argument,
                "cannot append reserved session event",
                "session/start and unknown kinds are accepted only while creating or reopening",
            )

    def append_batch(self, payloads: list[EventPayload]) -> list[Event]:
        """One bounded payload group maps to one physical append unit."""
        self._check_writable()
        if not payloads:
            raise AvaError(
                ErrorKind.invalid_argument,
                "cannot append session event batch",
                "payload batch is empty",
            )
        events: list[Event] = []
        batch = bytearray()
        for index, payload in enumerate(payloads):
            self._check_payload(payload)
            event = Event(seq=self._next_seq + index, at=now_ms(), payload=payload)
            record = codec.encode_record(event).encode("utf-8")
            if len(record) + 1 > MAX_DECODED_BATCH_BYTES - len(batch):
                raise AvaError(
                    ErrorKind.invalid_argument,
                    "cannot append session event batch",
                    "encoded batch exceeds its size limit",
                )
            batch += record + b"\n"
            events.append(event)
        frame = _encode_physical(self._encoding, bytes(batch))
        info = os.fstat(self._fd)
        fingerprint = hashlib.sha256(frame).hexdigest()
        offset = self._write_frame(frame)
        events = [_defer_tool_images(event, self._path.absolute(), info, offset, len(frame), self._encoding, fingerprint)
                  for event in events]
        self._next_seq += len(events)
        return events

    def append_next(self, payloads: list[EventPayload]) -> list[Event]:
        """Consume exactly the bounded prefix of ``payloads`` that fits in one physical frame."""
        self._check_writable()
        if not payloads:
            raise AvaError(
                ErrorKind.invalid_argument,
                "cannot append session event batch",
                "payload batch is empty",
            )
        events: list[Event] = []
        batch = bytearray()
        consumed = 0
        for payload in payloads:
            self._check_payload(payload)
            event = Event(seq=self._next_seq + len(events), at=now_ms(), payload=payload)
            record = codec.encode_record(event).encode("utf-8")
            if len(record) + 1 > MAX_DECODED_BATCH_BYTES - len(batch):
                break
            batch += record + b"\n"
            events.append(event)
            consumed += 1
        if not events:
            raise AvaError(
                ErrorKind.invalid_argument,
                "cannot append session event batch",
                "encoded record exceeds its size limit",
            )
        frame = _encode_physical(self._encoding, bytes(batch))
        info = os.fstat(self._fd)
        fingerprint = hashlib.sha256(frame).hexdigest()
        offset = self._write_frame(frame)
        events = [_defer_tool_images(event, self._path.absolute(), info, offset, len(frame), self._encoding, fingerprint)
                  for event in events]
        del payloads[:consumed]
        self._next_seq += len(events)
        return events

    def append(self, payload: EventPayload) -> Event:
        return self.append_batch([payload])[0]

    def _write_frame(self, frame: bytes) -> int:
        return _append_bytes_transactionally(self._fd, frame, self._poison)

    def _poison(self) -> None:
        self._poisoned = True

    def sync(self) -> None:
        """Turn seams call this; a failure permanently closes the writer."""
        if self._poisoned:
            raise AvaError(ErrorKind.io, "cannot sync session log", "writer is poisoned")
        if self._fd < 0:
            raise AvaError(
                ErrorKind.invalid_argument, "cannot sync session log", "file descriptor is invalid"
            )
        try:
            os.fsync(self._fd)
        except OSError as error:
            self._poisoned = True
            raise _io_error("cannot sync session log", error) from error

    # ---- accessors --------------------------------------------------------------------------

    @property
    def loaded_events(self) -> list[Event]:
        return self._loaded

    def take_loaded_events(self) -> list[Event]:
        self._ready_for_resume = False
        loaded, self._loaded = self._loaded, []
        return loaded

    @property
    def path(self) -> Path:
        return self._path

    @property
    def physical_encoding(self) -> PhysicalEncoding:
        return self._encoding

    @property
    def next_sequence(self) -> int:
        return self._next_seq

    @property
    def ready_for_resume(self) -> bool:
        return self._ready_for_resume and self._fd >= 0 and not self._poisoned

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


# ---- POSIX helpers -------------------------------------------------------------------------


def _validate_format(header: SessionStart, path: Path) -> None:
    if header.format != 1:
        raise AvaError(
            ErrorKind.parse, "unsupported session format", f"format {header.format} in '{path}'"
        )


def _load_record(record: str, expected: int) -> Event:
    event = codec.decode_record(record)
    if event.seq != expected:
        raise AvaError(ErrorKind.parse, "invalid session sequence", f"expected {expected}, found {event.seq}")
    if expected == 0 and not isinstance(event.payload, SessionStart):
        raise AvaError(ErrorKind.parse, "invalid session log", "first record is not session/start")
    if expected != 0 and isinstance(event.payload, SessionStart):
        raise AvaError(ErrorKind.parse, "invalid session log", "session/start appears after the header")
    return event


def _acquire_lock(fd: int, path: Path) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise AvaError(
            ErrorKind.permission,
            f"cannot open session writer '{path}'",
            "lock is held by another process",
        ) from None
    except OSError as error:
        raise _io_error(f"cannot lock session '{path}'", error) from error


def _open_existing(path: Path, mode: OpenMode) -> int:
    flags = (
        os.O_RDWR | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW
        if mode == OpenMode.repair
        else os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    )
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise _io_error(f"cannot open session log '{path}'", error) from error
    try:
        import stat

        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise AvaError(
                ErrorKind.io, f"cannot open session log '{path}'", "path is not a regular file"
            )
        if mode == OpenMode.repair:
            _acquire_lock(fd, path)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _append_bytes_transactionally(fd: int, data: bytes, poison: Callable[[], None]) -> int:
    if not data:
        raise AvaError(ErrorKind.invalid_argument, "cannot append session frame", "frame is empty")
    original_end = os.lseek(fd, 0, os.SEEK_END)
    written = 0
    while written < len(data):
        try:
            count = os.write(fd, data[written:])
        except InterruptedError:
            continue
        except OSError as error:
            _rollback(fd, original_end, poison, error)
            raise _io_error("cannot append session frame", error) from error
        if count == 0:
            write_stopped = OSError(5, "Input/output error")
            _rollback(fd, original_end, poison, write_stopped)
            raise _io_error("cannot append session frame", write_stopped) from write_stopped
        written += count
    return original_end


def _rollback(
    fd: int, original_end: int, poison: Callable[[], None], write_error: OSError
) -> None:
    try:
        os.ftruncate(fd, original_end)
    except OSError as rollback_error:
        poison()
        raise AvaError(
            ErrorKind.io,
            "cannot roll back failed session append",
            f"write failed: {write_error}; rollback failed: {rollback_error}",
        ) from rollback_error


def _create_file_atomically(path: Path, initial: bytes) -> int:
    """Write the header frame to a 0600 temporary, sync it, then link it into place."""
    parent = path.parent
    if not path.name:
        raise AvaError(
            ErrorKind.invalid_argument,
            "cannot create session log",
            "session path must include a filename",
        )
    try:
        resolved_parent = parent.resolve(strict=True)
    except OSError as error:
        raise _io_error("cannot open session directory", error) from error
    if not resolved_parent.is_dir():
        raise AvaError(ErrorKind.io, "cannot open session directory", "not a directory")
    resolved_path = resolved_parent / path.name
    fd = -1
    temporary: Path | None = None
    for attempt in range(128):
        candidate = resolved_parent / f".{path.name}.tmp.{os.getpid()}.{attempt}"
        try:
            fd = os.open(
                candidate,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
        except FileExistsError:
            continue
        except OSError as error:
            raise _io_error("cannot create session temporary", error) from error
        temporary = candidate
        break
    if fd < 0 or temporary is None:
        raise AvaError(
            ErrorKind.io,
            "cannot create session temporary",
            "temporary filename attempts were exhausted",
        )
    try:
        os.fchmod(fd, 0o600)
        _acquire_lock(fd, path)
        _append_bytes_transactionally(fd, initial, lambda: None)
        os.fsync(fd)
        try:
            os.link(temporary, resolved_path)
        except FileExistsError:
            raise AvaError(
                ErrorKind.io, f"cannot install session log '{path}'", "session path already exists"
            ) from None
        except OSError as error:
            raise _io_error(f"cannot install session log '{path}'", error) from error
        directory_fd = os.open(resolved_parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        os.close(fd)
        raise
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass
    return fd


def _repair_tail(fd: int, encoding: PhysicalEncoding, scan: _PhysicalScan) -> None:
    """Replace only the torn final physical unit with its complete-record prefix."""
    assert scan.torn_offset is not None
    try:
        os.ftruncate(fd, scan.torn_offset)
        if encoding == PhysicalEncoding.zstd and scan.retained_batch:
            _append_bytes_transactionally(fd, encode_frame(scan.retained_batch), lambda: None)
        os.fsync(fd)
    except OSError as error:
        raise _io_error("cannot replace torn session tail", error) from error


__all__ = [
    "Log",
    "OpenMode",
    "PhysicalEncoding",
    "SessionCandidate",
    "canonical_working_directory",
    "default_session_root",
    "discover_all_sessions_in",
    "discover_sessions_in",
    "fnv1a_64",
    "new_ulid",
    "project_bucket",
]

_ = hashlib  # keep the import available for future integrity checks without a lint warning

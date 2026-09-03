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
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import zstandard

from ava.base import AvaError, ErrorKind, ava_home
from ava.session import codec
from ava.session.event import Event, EventPayload, SessionStart, Unknown, now_ms
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


def _scan_plain(data: bytes, *, header_only: bool) -> _PhysicalScan:
    if not data:
        raise AvaError(ErrorKind.parse, "invalid plain session log", "session file is empty")
    scan = _PhysicalScan()
    offset = 0
    while offset < len(data):
        newline = data.find(b"\n", offset)
        if newline == -1:
            scan.torn_offset = offset
            break
        line = data[offset:newline]
        if not line:
            raise AvaError(
                ErrorKind.parse, "invalid plain session log", "session log contains an empty record"
            )
        if len(line) > codec.MAX_RECORD_BYTES:
            raise AvaError(
                ErrorKind.parse,
                "invalid plain session log",
                "complete record exceeds the size limit",
            )
        scan.records.append(line.decode("utf-8"))
        scan.unit_count += 1
        offset = newline + 1
        if header_only:
            return scan
    return scan


def _scan_zstd(data: bytes, *, header_only: bool) -> _PhysicalScan:
    if not data:
        raise AvaError(ErrorKind.parse, "invalid session log", "session file is empty")
    scan = _PhysicalScan()
    decompressor = zstandard.ZstdDecompressor(max_window_size=1 << MAX_WINDOW_LOG)
    offset = 0
    while offset < len(data):
        remaining = data[offset:]
        decoder = decompressor.decompressobj()
        try:
            decoded = decoder.decompress(remaining)
        except zstandard.ZstdError as error:
            # A frame that cannot be decoded at all is corruption, not an interruption, unless it
            # is the unfinished header of the final frame.
            if remaining[:4] == ZSTD_MAGIC and len(remaining) < 18 and _header_is_short(remaining):
                scan.torn_offset = offset
                break
            raise AvaError(
                ErrorKind.parse, "corrupt Zstandard session frame", str(error)
            ) from error
        if len(decoded) > MAX_DECODED_BATCH_BYTES:
            raise AvaError(
                ErrorKind.parse,
                "invalid Zstandard session frame",
                "frame exceeds the decoded batch limit",
            )
        if not decoder.eof:
            # Only the final physical unit can be torn; keep its complete-record prefix.
            scan.torn_offset = offset
            _, scan.retained_batch = _split_records(decoded, allow_partial_tail=True)
            break
        parameters = zstandard.get_frame_parameters(remaining)
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
        scan.records.extend(records)
        scan.unit_count += 1
        offset += len(remaining) - len(decoder.unused_data)
        if header_only:
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
            data = _read_all(fd)
            encoding = _detect_encoding(data)
            if not _suffix_matches(path, encoding):
                raise AvaError(
                    ErrorKind.parse,
                    "invalid session log",
                    "path suffix disagrees with detected encoding",
                )
            scan = (
                _scan_zstd(data, header_only=False)
                if encoding == PhysicalEncoding.zstd
                else _scan_plain(data, header_only=False)
            )
            loaded = _cold_load(scan.records)
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
            if scan.torn_offset is not None and mode == OpenMode.repair:
                _repair_tail(fd, encoding, scan)
                if encoding == PhysicalEncoding.zstd and scan.retained_batch:
                    loaded = _cold_load(
                        scan.records
                        + _split_records(scan.retained_batch, allow_partial_tail=False)[0]
                    )
            elif (
                scan.torn_offset is not None
                and encoding == PhysicalEncoding.zstd
                and scan.retained_batch
            ):
                # Read-only inspection still exposes the complete logical prefix of a torn frame.
                loaded = _cold_load(
                    scan.records + _split_records(scan.retained_batch, allow_partial_tail=False)[0]
                )
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
            data = _read_all(fd)
        finally:
            os.close(fd)
        encoding = _detect_encoding(data)
        if not _suffix_matches(path, encoding):
            raise AvaError(
                ErrorKind.parse,
                "invalid session log",
                "path suffix disagrees with detected encoding",
            )
        scan = (
            _scan_zstd(data, header_only=True)
            if encoding == PhysicalEncoding.zstd
            else _scan_plain(data, header_only=True)
        )
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
        self._write_frame(_encode_physical(self._encoding, bytes(batch)))
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
        self._write_frame(_encode_physical(self._encoding, bytes(batch)))
        del payloads[:consumed]
        self._next_seq += len(events)
        return events

    def append(self, payload: EventPayload) -> Event:
        return self.append_batch([payload])[0]

    def _write_frame(self, frame: bytes) -> None:
        _append_bytes_transactionally(self._fd, frame, self._poison)

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


def _cold_load(records: list[str]) -> list[Event]:
    events: list[Event] = []
    for record in records:
        event = codec.decode_record(record)
        expected = len(events)
        if event.seq != expected:
            raise AvaError(
                ErrorKind.parse,
                "invalid session sequence",
                f"expected {expected}, found {event.seq}",
            )
        if expected == 0 and not isinstance(event.payload, SessionStart):
            raise AvaError(
                ErrorKind.parse, "invalid session log", "first record is not session/start"
            )
        if expected != 0 and isinstance(event.payload, SessionStart):
            raise AvaError(
                ErrorKind.parse, "invalid session log", "session/start appears after the header"
            )
        events.append(event)
    return events


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


def _read_all(fd: int) -> bytes:
    size = os.fstat(fd).st_size
    chunks: list[bytes] = []
    offset = 0
    while offset < size:
        chunk = os.pread(fd, min(1 << 20, size - offset), offset)
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
    return b"".join(chunks)


def _append_bytes_transactionally(fd: int, data: bytes, poison: Callable[[], None]) -> None:
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

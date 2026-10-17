"""Offline classic Hudson mutation sessions; GoldBase is a separate format."""

from __future__ import annotations

import codecs
import os
import re
import stat
import struct
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import datetime
from os import PathLike
from pathlib import Path
from types import TracebackType
from typing import Literal, NoReturn
from uuid import UUID

from golded_ftn import (
    UNSET,
    CommitState,
    ConflictError,
    ControlLine,
    FtnAddress,
    MessageIdentity,
    MessagePatch,
    OfflineRecoveryAssertion,
    OutgoingMessage,
    PhysicalBaseIdentity,
    PreparedMutation,
    ReaderOptions,
    RecoveryError,
    RecoveryOperationKind,
    RecoveryReference,
    RevisionToken,
    RollbackError,
    SessionMessage,
    WriterError,
    WriteResult,
    WriterOptions,
)
from golded_ftn._recovery import (
    BaseRecoverableSession,
    FileMutationPlan,
    RecoveryStore,
    register_hudson_scan_dir,
)
from golded_ftn._writer_io import IO, Transaction, locks, raw_revision, strict_encode

from .reader import HudsonReader, _aligned, _find, _Text

_INFO = struct.Struct("<203H")
_HEADER = struct.Struct("<10H2BH3B")
_LOCK_OFFSET = _INFO.size + 1
_FILES = ("MSGINFO.BBS", "MSGIDX.BBS", "MSGHDR.BBS", "MSGTXT.BBS", "MSGTOIDX.BBS")
_SCANS = ("NETMAIL.BBS", "ECHOMAIL.BBS")
_PLAN_FILES = (*_FILES, *_SCANS)
_ROLES = {
    "MSGINFO.BBS": "msginfo",
    "MSGIDX.BBS": "msgidx",
    "MSGHDR.BBS": "msghdr",
    "MSGTXT.BBS": "msgtxt",
    "MSGTOIDX.BBS": "msgtoidx",
    "NETMAIL.BBS": "netmail",
    "ECHOMAIL.BBS": "echomail",
}
_WRITE_ORDER = (
    "MSGTXT.BBS",
    "MSGHDR.BBS",
    "MSGIDX.BBS",
    "MSGTOIDX.BBS",
    "MSGINFO.BBS",
    *_SCANS,
)
_ADAPTER_MESSAGES = {
    "year_out_of_range": "Hudson date year must be in 1980..2079",
    "aware_datetime": "Hudson date must be naive local wall time",
}
_RECOVERY_INPUT_ERROR = "Hudson recovery input is not representable"
_OPEN_FLAGS = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)


class HudsonAdapterError(ValueError):
    """A message cannot be represented in Hudson; the tosser should HELD it."""

    code: str

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(_ADAPTER_MESSAGES[code])

    def __str__(self) -> str:
        return _ADAPTER_MESSAGES[self.code]

    def __repr__(self) -> str:
        return f"HudsonAdapterError(code={self.code!r})"


@dataclass(frozen=True, slots=True, kw_only=True)
class HudsonWriterOptions(WriterOptions):
    extension_case: Literal["upper", "lower"] = "upper"

    def __post_init__(self) -> None:
        WriterOptions.__post_init__(self)
        if self.extension_case not in {"upper", "lower"}:
            raise ValueError("extension_case must be 'upper' or 'lower'")


def _cased_name(name: str, options: WriterOptions | None) -> str:
    case = (
        options.extension_case
        if isinstance(options, HudsonWriterOptions)
        else "upper"
    )
    return name.lower() if case == "lower" else name


@dataclass(frozen=True, slots=True)
class _HudsonMutation:
    original: dict[str, bytes]
    changed: dict[str, bytes]
    result: WriteResult


def _pascal(value: bytes, width: int) -> bytes:
    if len(value) >= width:
        raise ValueError(f"Pascal field exceeds {width - 1} bytes")
    if b"\0" in value or b"\r" in value or b"\n" in value:
        raise ValueError("Header field contains a line break or NUL")
    return bytes([len(value)]) + value + bytes(width - 1 - len(value))


def _word(value: int | None, name: str, maximum: int = 65535) -> int:
    if value is None:
        return 0
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= maximum
    ):
        raise ValueError(f"{name} must be in 0..{maximum}")
    return value


def _controls(text: str) -> tuple[ControlLine, ...]:
    return tuple(
        ControlLine(name=m[1], value=m[2], raw=m[0])
        for m in re.finditer(
            r"(?m)^\x01(CHRS|CHARSET):\s*([^\r\n]*)", text.replace("\r", "\n"), re.I
        )
    )


def _encode(text: str, options: WriterOptions, body: str = "") -> bytes:
    return strict_encode(text, options, _controls(body or text))


def _replace_control(text: str, names: set[str], lines: list[str]) -> str:
    # Retain unknown controls and text in their original order.
    old = text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\0")
    retained = [
        line
        for line in old.split("\n")
        if not (
            line.startswith("\x01")
            and re.split(r"[:\s]", line[1:], maxsplit=1)[0].upper() in names
        )
    ]
    return "\n".join(lines + retained)


def _text_blocks(payload: bytes) -> bytes:
    if b"\0" in payload:
        raise ValueError("Message text contains NUL")
    payload += b"\0"
    return b"".join(
        bytes([len(chunk)]) + chunk + bytes(255 - len(chunk))
        for index in range(0, len(payload), 255)
        for chunk in (payload[index : index + 255],)
    )


def _require_board(board: int) -> int:
    if isinstance(board, bool) or not isinstance(board, int) or not 1 <= board <= 200:
        raise ValueError("Hudson board must be in 1..200")
    return board


def _raise_recovery_os(code: str = "unsafe_recovery_path") -> NoReturn:
    raise RecoveryError(code=code) from None


def _raise_recovery_input() -> NoReturn:
    raise ValueError(_RECOVERY_INPUT_ERROR) from None


def _require_not_symlink(path: Path) -> None:
    try:
        mode = os.lstat(path).st_mode
    except OSError:
        _raise_recovery_os()
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise RecoveryError(code="unsafe_recovery_path")


def _require_fd_matches_path(fd: int, path: Path) -> None:
    try:
        fd_stat = os.fstat(fd)
        path_stat = os.lstat(path)
    except OSError:
        _raise_recovery_os()
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise RecoveryError(code="unsafe_recovery_path")
    if (fd_stat.st_dev, fd_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
        raise RecoveryError(code="unsafe_recovery_path")


def _locate_recovery_file(directory: Path, name: str) -> Path | None:
    try:
        return _find(directory, name)
    except FileNotFoundError:
        return None
    except OSError:
        _raise_recovery_os()


def _open_recovery_file(path: Path) -> int:
    _require_not_symlink(path)
    try:
        fd = os.open(path, _OPEN_FLAGS)
    except OSError:
        _raise_recovery_os()
    try:
        _require_fd_matches_path(fd, path)
    except RecoveryError:
        os.close(fd)
        raise
    return fd


@contextmanager
def _acquire_recovery_lock(info_path: Path, timeout: float) -> Iterator[int]:
    cm = locks.acquire(info_path, _LOCK_OFFSET, timeout)
    try:
        fd = cm.__enter__()
    except OSError:
        _raise_recovery_os()
    try:
        yield fd
    except BaseException as error:
        if cm.__exit__(type(error), error, error.__traceback__):
            return
        raise
    else:
        cm.__exit__(None, None, None)


def _changed_ranges(pre: bytes, post: bytes) -> tuple[tuple[int, int], ...]:
    if pre == post:
        return ()
    overlap = min(len(pre), len(post))
    ranges: list[tuple[int, int]] = []
    start: int | None = None
    for index in range(overlap):
        if pre[index] != post[index]:
            if start is None:
                start = index
        elif start is not None:
            ranges.append((start, index))
            start = None
    if start is not None:
        ranges.append((start, overlap))
    if len(post) > overlap:
        if ranges and ranges[-1][1] == overlap:
            ranges[-1] = (ranges[-1][0], len(post))
        else:
            ranges.append((overlap, len(post)))
    return tuple(ranges)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _snapshot_raw(io: IO, fds: dict[str, int]) -> dict[str, bytes]:
    raw = {name: io.read(fd, 0, os.fstat(fd).st_size) for name, fd in fds.items()}
    for name in _SCANS:
        raw.setdefault(name, b"")
    return raw


def _file_plans(
    original: dict[str, bytes],
    changed: dict[str, bytes],
    fds: dict[str, int],
    rels: dict[str, tuple[str, str]],
    existing_scans: set[str],
) -> list[FileMutationPlan]:
    plans: list[FileMutationPlan] = []
    for name in _PLAN_FILES:
        root_key, rel_path = rels[name]
        pre = original.get(name, b"")
        post = changed.get(name, pre)
        post_len = len(post)
        existed = name not in _SCANS or name in existing_scans
        if existed:
            fd = fds.get(name)
            if fd is None:
                raise RecoveryError(code="unsafe_recovery_path")
            ranges = _changed_ranges(pre, post)
            plans.append(
                FileMutationPlan(
                    role=_ROLES[name],
                    root_key=root_key,
                    rel_path=rel_path,
                    fd=fd,
                    planned_write_ranges=ranges,
                    post_min_len=post_len,
                    post_max_len=post_len,
                )
            )
            continue
        ranges = ((0, post_len),) if post else ()
        # Omit fd so RecoveryStore records existed=False before create.
        plans.append(
            FileMutationPlan(
                role=_ROLES[name],
                root_key=root_key,
                rel_path=rel_path,
                require_inode_match=False,
                planned_write_ranges=ranges,
                post_min_len=post_len,
                post_max_len=post_len,
                planned_new_bytes=post if post else None,
            )
        )
    return plans


def _apply_changed(
    io: IO,
    fds: dict[str, int],
    mutation: _HudsonMutation,
    scan_path: Path,
    store: RecoveryStore,
    transaction_id: UUID,
    rels: dict[str, tuple[str, str]],
    created_fds: list[int],
) -> WriteResult:
    touched_dirs: set[Path] = set()
    try:
        for name in _WRITE_ORDER:
            data = mutation.changed[name]
            pre = mutation.original[name]
            if data == pre:
                continue
            if name in _SCANS and name not in fds:
                file = scan_path / name
                flags = (
                    os.O_RDWR
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_BINARY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                fd = os.open(file, flags, 0o600)
                if hasattr(os, "fchmod"):
                    os.fchmod(fd, 0o600)
                fds[name] = fd
                created_fds.append(fd)
                st = os.fstat(fd)
                store.update_own_created_inode(
                    transaction_id, _ROLES[name], (st.st_dev, st.st_ino)
                )
                root_key, rel_path = rels[name]
                store.register_retained_fd(rel_path, fd, root_key=root_key)
                touched_dirs.add(file.parent)
            for start, end in _changed_ranges(pre, data):
                io.write(fds[name], start, data[start:end])
            io.truncate(fds[name], len(data))
            io.flush(fds[name])
            touched_dirs.add(
                scan_path if name in _SCANS else Path(store.base_roots["base"])
            )
        for directory in sorted(touched_dirs):
            _fsync_directory(directory)
    except RecoveryError:
        raise
    except OSError:
        raise RecoveryError(code="unexpected_base_modification") from None
    except (UnicodeEncodeError, UnicodeDecodeError):
        _raise_recovery_input()
    return mutation.result


class HudsonWriter:
    """Create a whole Hudson base and edit one explicit board per session."""

    def create(
        self, path: str | PathLike[str], options: WriterOptions | None = None
    ) -> None:
        directory = Path(path)
        directory.mkdir(parents=True, exist_ok=True)
        initial = dict.fromkeys((*_FILES, "LASTREAD.BBS", *_SCANS), b"")
        initial["MSGINFO.BBS"] = bytes(_INFO.size)
        initial["LASTREAD.BBS"] = bytes(400)
        for name in initial:
            if any(p.name.casefold() == name.casefold() for p in directory.iterdir()):
                raise FileExistsError(directory / name)
        created: list[Path] = []
        try:
            for name, raw in initial.items():
                file = directory / _cased_name(name, options)
                with file.open("xb") as stream:
                    created.append(file)
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
        except BaseException:
            for file in created:
                file.unlink()
            raise

    def open(
        self,
        path: str | PathLike[str],
        board: int,
        options: WriterOptions | None = None,
        *,
        scan_path: str | PathLike[str] | None = None,
    ) -> HudsonSession:
        return HudsonSession(Path(path), board, options or WriterOptions(), scan_path)

    @contextmanager
    def recovery_session(
        self,
        path: str | PathLike[str],
        *,
        recovery_root: str | PathLike[str],
        options: WriterOptions | None = None,
        scan_path: str | PathLike[str] | None = None,
        board: int = 1,
    ) -> Iterator[HudsonRecoverableSession]:
        options = options or WriterOptions()
        _require_board(board)
        codecs.lookup(options.target_charset)
        helper = HudsonSession(Path(path), board, options, scan_path)
        try:
            info_path = _find(helper.path, "MSGINFO.BBS")
        except OSError:
            _raise_recovery_os()
        _require_not_symlink(info_path)
        with _acquire_recovery_lock(info_path, options.lock_timeout) as lockfd:
            try:
                lock_stat = os.fstat(lockfd)
            except OSError:
                _raise_recovery_os()
            _require_fd_matches_path(lockfd, info_path)
            canonical_key = os.path.realpath(info_path)
            physical_base = PhysicalBaseIdentity(
                format="hudson",
                canonical_key=canonical_key,
                lock_inode=(lock_stat.st_dev, lock_stat.st_ino),
            )
            resolved_scan = register_hudson_scan_dir(canonical_key, helper.scan_path)
            base_root = os.path.realpath(helper.path)
            if resolved_scan != base_root:
                extra_roots: dict[str, str] | None = {"scan": resolved_scan}
                scan_key = "scan"
            else:
                extra_roots = None
                scan_key = "base"
            fds: dict[str, int] = {"MSGINFO.BBS": lockfd}
            rels: dict[str, tuple[str, str]] = {
                "MSGINFO.BBS": (
                    "base",
                    os.path.relpath(canonical_key, base_root),
                )
            }
            created_fds: list[int] = []
            try:
                for name in _FILES[1:]:
                    try:
                        file = _find(helper.path, name)
                    except OSError:
                        _raise_recovery_os()
                    fds[name] = _open_recovery_file(file)
                    rels[name] = (
                        "base",
                        os.path.relpath(os.path.realpath(file), base_root),
                    )
                for name in _SCANS:
                    scan_file = _locate_recovery_file(helper.scan_path, name)
                    if scan_file is None:
                        rels[name] = (scan_key, name)
                        continue
                    fds[name] = _open_recovery_file(scan_file)
                    rels[name] = (
                        scan_key,
                        os.path.relpath(os.path.realpath(scan_file), resolved_scan),
                    )
                retained = {
                    (root_key, rel_path): fds[name]
                    for name, (root_key, rel_path) in rels.items()
                    if name in fds
                }
                store = RecoveryStore(
                    physical_base=physical_base,
                    recovery_root=recovery_root,
                    base_root=base_root,
                    extra_roots=extra_roots,
                    retained_fds=retained,
                )
                session = HudsonRecoverableSession(
                    store,
                    helper=helper,
                    fds=fds,
                    rels=rels,
                    created_fds=created_fds,
                )
                try:
                    yield session
                finally:
                    session.close()
            finally:
                for name, fd in fds.items():
                    if name != "MSGINFO.BBS":
                        os.close(fd)


class HudsonSession:
    """Operations lock MSGINFO byte 407 and re-read the complete base."""

    def __init__(
        self,
        path: Path,
        board: int,
        options: WriterOptions,
        scan_path: str | PathLike[str] | None,
    ) -> None:
        _require_board(board)
        codecs.lookup(options.target_charset)
        self.path, self.board, self.options = path.resolve(), board, options
        self.scan_path = (
            Path(scan_path).resolve() if scan_path is not None else self.path
        )
        self._io = IO()
        self._closed = False
        self._poisoned = False

    def __enter__(self) -> HudsonSession:
        with self._operation():
            pass
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._closed = True

    @contextmanager
    def _operation(self) -> Iterator[tuple[dict[str, int], dict[str, bytes]]]:
        if self._closed or self._poisoned:
            raise WriterError(
                "Hudson session is closed or unusable after failed rollback"
            )
        paths = {name: _find(self.path, name) for name in _FILES}
        with locks.acquire(
            paths["MSGINFO.BBS"], _LOCK_OFFSET, self.options.lock_timeout
        ) as lockfd:
            descriptors = {"MSGINFO.BBS": lockfd}
            try:
                for name in _FILES[1:]:
                    descriptors[name] = os.open(
                        paths[name], os.O_RDWR | getattr(os, "O_BINARY", 0)
                    )
                raw = {
                    name: self._io.read(fd, 0, os.fstat(fd).st_size)
                    for name, fd in descriptors.items()
                }
                for name in _SCANS:
                    try:
                        file = _find(self.scan_path, name)
                    except FileNotFoundError:
                        raw[name] = b""
                    else:
                        fd = os.open(file, os.O_RDWR | getattr(os, "O_BINARY", 0))
                        descriptors[name] = fd
                        raw[name] = self._io.read(fd, 0, os.fstat(fd).st_size)
                self._validate(raw, paths)
                yield descriptors, raw
            except RollbackError:
                self._poisoned = True
                raise
            finally:
                for name, fd in descriptors.items():
                    if name != "MSGINFO.BBS":
                        os.close(fd)

    def _validate(self, raw: dict[str, bytes], paths: dict[str, Path]) -> None:
        if len(raw["MSGINFO.BBS"]) != _INFO.size:
            raise WriterError(
                "MSGINFO.BBS must contain the 406-byte classic Hudson structure"
            )
        for name, size in (
            ("MSGIDX.BBS", 3),
            ("MSGHDR.BBS", 187),
            ("MSGTXT.BBS", 256),
            ("MSGTOIDX.BBS", 36),
        ):
            _aligned(raw[name], size, paths[name])
        count = len(raw["MSGIDX.BBS"]) // 3
        if (
            len(raw["MSGHDR.BBS"]) != count * 187
            or len(raw["MSGTOIDX.BBS"]) != count * 36
        ):
            raise WriterError("Hudson header, index and name-index slot counts differ")
        active: list[int] = []
        seen: set[int] = set()
        active_slots: set[int] = set()
        boards = [0] * 200
        for slot in range(count):
            number, board = struct.unpack_from("<HB", raw["MSGIDX.BBS"], slot * 3)
            header = raw["MSGHDR.BBS"][slot * 187 : (slot + 1) * 187]
            words = _HEADER.unpack_from(header)
            if number == 65535:
                if not words[13] & 1:
                    raise WriterError("Deleted index has an active header")
                continue
            if not 1 <= number <= 65534 or not 1 <= board <= 200:
                raise WriterError("Invalid active Hudson identity")
            if (
                words[0] != number
                or words[15] != board
                or words[13] & 1
                or number in seen
            ):
                raise WriterError("Hudson header/index identity mismatch or duplicate")
            if raw["MSGTOIDX.BBS"][slot * 36] > 35:
                raise WriterError("Invalid Hudson recipient-index Pascal length")
            HudsonReader._message(
                header,
                slot * 187,
                number,
                board,
                words,
                raw["MSGTXT.BBS"],
                paths["MSGHDR.BBS"],
                paths["MSGTXT.BBS"],
                ReaderOptions(fallback_charset=self.options.target_charset),
            )
            toidx = raw["MSGTOIDX.BBS"][slot * 36 : (slot + 1) * 36]
            expected = self._name_index(header)
            if toidx[: toidx[0] + 1] != expected[: expected[0] + 1]:
                raise WriterError("Hudson recipient index differs from header")
            active.append(number)
            seen.add(number)
            active_slots.add(slot)
            boards[board - 1] += 1
        info = _INFO.unpack(raw["MSGINFO.BBS"])
        if info[2] != len(active) or list(info[3:]) != boards:
            raise WriterError("Hudson information counts differ from active index")
        if active and (info[0] > min(active) or info[1] < max(active)):
            raise WriterError("Hudson information number range omits active messages")
        for name in _SCANS:
            scan = raw[name]
            if len(scan) % 2:
                raise WriterError(f"Truncated Hudson scanning index: {name}")
            values = [item[0] for item in struct.iter_unpack("<H", scan)]
            if values != sorted(set(values)) or any(
                value not in active_slots for value in values
            ):
                raise WriterError(f"Invalid Hudson scanning index: {name}")

    def _identity(self, msgno: int) -> MessageIdentity:
        return MessageIdentity(
            format="hudson", base=str(self.path), msgno=msgno, board=self.board
        )

    def _read(self, msgno: int, raw: dict[str, bytes]) -> SessionMessage:
        if (
            isinstance(msgno, bool)
            or not isinstance(msgno, int)
            or not 1 <= msgno <= 65534
        ):
            raise ValueError("Hudson message number must be in 1..65534")
        for slot, (number, board) in enumerate(
            struct.iter_unpack("<HB", raw["MSGIDX.BBS"])
        ):
            if number != msgno or board != self.board:
                continue
            header = raw["MSGHDR.BBS"][slot * 187 : (slot + 1) * 187]
            words = _HEADER.unpack_from(header)
            start, length = words[4] * 256, words[5] * 256
            text = raw["MSGTXT.BBS"][start : start + length]
            identity = self._identity(msgno)
            revision = raw_revision(
                identity,
                (slot * 187, slot * 3, start, length),
                header,
                raw["MSGIDX.BBS"][slot * 3 : slot * 3 + 3],
                raw["MSGTOIDX.BBS"][slot * 36 : slot * 36 + 36],
                text,
            )
            message = HudsonReader._message(
                header,
                slot * 187,
                msgno,
                self.board,
                words,
                raw["MSGTXT.BBS"],
                _find(self.path, "MSGHDR.BBS"),
                _find(self.path, "MSGTXT.BBS"),
                ReaderOptions(fallback_charset=self.options.target_charset),
            )
            return SessionMessage(message=message, identity=identity, revision=revision)
        raise ConflictError(f"Hudson message {msgno} on board {self.board} is absent")

    def read(self, msgno: int) -> SessionMessage:
        with self._operation() as (_, raw):
            return self._read(msgno, raw)

    def _target(
        self, identity: MessageIdentity, revision: RevisionToken, raw: dict[str, bytes]
    ) -> SessionMessage:
        if identity != self._identity(identity.msgno) or revision.identity != identity:
            raise ConflictError("Hudson target identity does not match this session")
        current = self._read(identity.msgno, raw)
        if current.revision != revision:
            raise ConflictError("Hudson message revision has changed")
        return current

    def _commit(
        self,
        operation: str,
        fds: dict[str, int],
        original: dict[str, bytes],
        changed: dict[str, bytes],
    ) -> None:
        created: list[Path] = []
        try:
            for name in _SCANS:
                if (
                    name in changed
                    and changed[name] != original[name]
                    and name not in fds
                ):
                    file = self.scan_path / name
                    fds[name] = os.open(
                        file,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                        0o600,
                    )
                    created.append(file)
            with Transaction(self._io, str(self.path), operation) as transaction:
                for name, data in changed.items():
                    if data != original[name]:
                        transaction.watch(fds[name])
                for name in _WRITE_ORDER:
                    data = changed[name]
                    if data != original[name]:
                        self._io.write(fds[name], 0, data)
                        self._io.truncate(fds[name], len(data))
        except BaseException:
            for file in created:
                try:
                    file.unlink()
                except OSError:
                    self._poisoned = True
                    raise RollbackError(
                        f"Transaction rollback failed during {operation}"
                    ) from None
            raise

    @staticmethod
    def _scans(
        changed: dict[str, bytes], slot: int, attributes: int, deleted: bool = False
    ) -> None:
        for name, mask in zip(_SCANS, (2, 32), strict=True):
            values = {item[0] for item in struct.iter_unpack("<H", changed[name])}
            if deleted:
                values.discard(slot)
            elif attributes & mask:
                values.add(slot)
            changed[name] = b"".join(
                struct.pack("<H", value) for value in sorted(values)
            )

    @staticmethod
    def _name_index(header: bytes) -> bytes:
        return _pascal(b"* Received *", 36) if header[24] & 16 else header[42:78]

    def _header(self, header: bytes, values: dict[str, object], body: str) -> bytes:
        raw = bytearray(header)
        for name, offset, width in (
            ("from_name", 78, 36),
            ("to_name", 42, 36),
            ("subject", 114, 73),
        ):
            if name in values:
                value = values[name]
                if not isinstance(value, str):
                    raise ValueError(f"{name} cannot be cleared")
                raw[offset : offset + width] = _pascal(
                    _encode(value, self.options, body), width
                )
        if "posted_at" in values:
            posted = values["posted_at"]
            if posted is None:
                raw[27:42] = bytes(15)
            elif isinstance(posted, datetime):
                if posted.tzinfo is not None:
                    raise HudsonAdapterError("aware_datetime")
                if not 1980 <= posted.year <= 2079:
                    raise HudsonAdapterError("year_out_of_range")
                raw[27:33] = _pascal(posted.strftime("%H:%M").encode("ascii"), 6)
                raw[33:42] = _pascal(posted.strftime("%m-%d-%y").encode("ascii"), 9)
            else:
                raise ValueError("posted_at must be datetime or None")
        if "attributes_raw" in values:
            value = values["attributes_raw"]
            if value is None or not isinstance(value, int):
                raise ValueError("attributes_raw cannot be cleared")
            attributes = _word(value, "attributes_raw")
            if attributes & 1:
                raise ValueError("Use delete() to set the Hudson deleted bit")
            struct.pack_into("<H", raw, 24, attributes)
        for name, offset in (("reply_to_msgno", 2), ("reply1st_msgno", 4)):
            if name in values:
                value = values[name]
                if value is not None and not isinstance(value, int):
                    raise ValueError(f"{name} must be an integer or None")
                struct.pack_into("<H", raw, offset, _word(value, name, 65534))
        for name, offsets in (
            ("from_address", (21, 16, 18)),
            ("to_address", (20, 12, 14)),
        ):
            if name in values:
                address = values[name]
                if address is not None and not isinstance(address, FtnAddress):
                    raise ValueError(f"{name} must be an FTN address")
                zone, net, node = (
                    (address.zone, address.net, address.node) if address else (0, 0, 0)
                )
                raw[offsets[0]] = _word(zone, "zone", 255)
                struct.pack_into("<H", raw, offsets[1], _word(net, "net"))
                struct.pack_into("<H", raw, offsets[2], _word(node, "node"))
                if address and address.domain:
                    raise ValueError("Hudson cannot store address domains")
        for name in ("reply_next_msgno", "reply_list"):
            if name in values and values[name] not in (None, ()):
                raise ValueError(f"Hudson cannot represent {name}")
        return bytes(raw)

    def _body(
        self, text: str, values: dict[str, object], *, preserve_structured: bool = True
    ) -> str:
        if "body_text" in values:
            if not isinstance(values["body_text"], str):
                raise ValueError("body_text cannot be cleared")
            replacement = values["body_text"]
            inherited = [
                line
                for line in text.replace("\r\n", "\n")
                .replace("\r", "\n")
                .rstrip("\0")
                .split("\n")
                if line.startswith("\x01") or line.upper().startswith("SEEN-BY:")
            ]
            text = "\n".join(inherited + [replacement]) if inherited else replacement
        if "control_lines" in values:
            controls = values["control_lines"] or ()
            if not isinstance(controls, tuple) or not all(
                isinstance(c, ControlLine) for c in controls
            ):
                raise ValueError("control_lines must contain ControlLine values")
            names = {
                re.split(r"[:\s]", line[1:], maxsplit=1)[0].upper()
                for line in text.replace("\r", "\n").split("\n")
                if line.startswith("\x01")
            }
            # Structured fields retain their controls when omitted from a patch.
            protected = {"PATH"} if preserve_structured else set()
            if preserve_structured and "external_id" not in values:
                protected.add("MSGID")
            if preserve_structured and "from_address" not in values:
                protected.add("FMPT")
            if preserve_structured and "to_address" not in values:
                protected.add("TOPT")
            if (
                preserve_structured
                and "from_address" not in values
                and "to_address" not in values
            ):
                protected.add("INTL")
            existing = {
                re.split(r"[:\s]", line[1:], maxsplit=1)[0].upper(): re.split(
                    r"[:\s]", line[1:], maxsplit=1
                )[1].strip()
                for line in text.replace("\r", "\n").split("\n")
                if line.startswith("\x01") and re.search(r"[:\s]", line[1:])
            }
            names -= protected
            lines = []
            for control in controls:
                assert isinstance(control, ControlLine)
                if any(c in control.name + control.value for c in "\r\n\0"):
                    raise ValueError("Control fields contain line break or NUL")
                name = control.name.upper()
                if name in protected:
                    if existing.get(name) != control.value.strip():
                        raise ValueError(
                            f"Control {name} conflicts with omitted structured field"
                        )
                    continue
                if (
                    name == "MSGID"
                    and "external_id" in values
                    and control.value != values["external_id"]
                ):
                    raise ValueError("MSGID conflicts with external_id")
                if name in {"FMPT", "TOPT"}:
                    address = values.get(
                        "from_address" if name == "FMPT" else "to_address"
                    )
                    if isinstance(address, FtnAddress) and control.value.strip() != str(
                        address.point or 0
                    ):
                        raise ValueError(f"Control {name} conflicts with address")
                lines.append(f"\x01{control.name}: {control.value}")
            text = _replace_control(text, names, lines)
        if "external_id" in values:
            msgid = values["external_id"]
            if msgid is not None and (
                not isinstance(msgid, str) or any(c in msgid for c in "\r\n\0")
            ):
                raise ValueError("Invalid external_id")
            text = _replace_control(
                text, {"MSGID"}, [f"\x01MSGID: {msgid}"] if msgid is not None else []
            )
        for name, prefix in (
            ("routing_seen_by", "SEEN-BY:"),
            ("routing_path", "\x01PATH:"),
        ):
            if name in values:
                routing = values[name] or ()
                if not isinstance(routing, tuple) or not all(
                    isinstance(v, str) and not any(c in v for c in "\r\n\0")
                    for v in routing
                ):
                    raise ValueError("Invalid routing entries")
                normalized = text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\0")
                text = "\n".join(
                    line
                    for line in normalized.split("\n")
                    if not line.upper().startswith(prefix)
                )
                text += "".join(f"\n{prefix} {value}" for value in routing)
        for name, control_name in (("from_address", "FMPT"), ("to_address", "TOPT")):
            if name in values:
                address = values[name]
                if address is not None and not isinstance(address, FtnAddress):
                    raise ValueError("Invalid FTN address")
                point = address.point if address else None
                _word(point, "point")
                text = _replace_control(
                    text,
                    {control_name},
                    [f"\x01{control_name} {point}"] if point else [],
                )
        if "from_address" in values or "to_address" in values:
            # Header values carry zones/nets/nodes. Remove stale overriding INTL.
            text = _replace_control(text, {"INTL"}, [])
        return text

    def _encode_append(self, message: OutgoingMessage) -> tuple[bytes, bytes]:
        values = {field.name: getattr(message, field.name) for field in fields(message)}
        values.pop("provenance", None)
        if values["attributes_raw"] is None:
            values["attributes_raw"] = 0
        for name in (
            "external_id",
            "control_lines",
            "routing_seen_by",
            "routing_path",
            "from_address",
            "to_address",
        ):
            if not values[name]:
                values.pop(name)
        body = self._body("", values, preserve_structured=False)
        header = self._header(bytes(187), values, body)
        blocks = _text_blocks(
            _encode(
                body.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r"),
                self.options,
            )
        )
        return header, blocks

    def _build_append(
        self, original: dict[str, bytes], header: bytes, blocks: bytes
    ) -> _HudsonMutation:
        changed = dict(original)
        largest = max(
            (
                words[0]
                for words in struct.iter_unpack("<187s", original["MSGHDR.BBS"])
                for words in (_HEADER.unpack_from(words[0]),)
            ),
            default=0,
        )
        number = max(largest, _INFO.unpack(original["MSGINFO.BBS"])[1]) + 1
        if number > 65534:
            raise ValueError("Hudson message numbers exhausted")
        slot = len(original["MSGHDR.BBS"]) // 187
        if slot > 65535:
            raise ValueError("Hudson header-slot numbers exhausted")
        start, count = len(original["MSGTXT.BBS"]) // 256, len(blocks) // 256
        _word(start, "text start")
        _word(count, "text count")
        raw = bytearray(header)
        struct.pack_into("<H", raw, 0, number)
        struct.pack_into("<HH", raw, 8, start, count)
        raw[26] = self.board
        changed["MSGTXT.BBS"] += blocks
        changed["MSGHDR.BBS"] += raw
        changed["MSGIDX.BBS"] += struct.pack("<HB", number, self.board)
        changed["MSGTOIDX.BBS"] += self._name_index(bytes(raw))
        info = list(_INFO.unpack(original["MSGINFO.BBS"]))
        info[0] = min(info[0], number) if info[2] else number
        info[1] = number
        info[2] += 1
        info[self.board + 2] += 1
        changed["MSGINFO.BBS"] = _INFO.pack(*info)
        self._scans(changed, slot, raw[24])
        self._validate(changed, {name: self.path / name for name in _FILES})
        result = self._read(number, changed)
        return _HudsonMutation(
            original=original,
            changed=changed,
            result=WriteResult(identity=result.identity, revision=result.revision),
        )

    def append(self, message: OutgoingMessage) -> WriteResult:
        header, blocks = self._encode_append(message)
        with self._operation() as (fds, original):
            mutation = self._build_append(original, header, blocks)
            self._commit("append", fds, original, mutation.changed)
            return mutation.result

    def _build_update(
        self,
        original: dict[str, bytes],
        identity: MessageIdentity,
        patch: MessagePatch,
        expected_revision: RevisionToken,
    ) -> _HudsonMutation:
        values = {
            field.name: getattr(patch, field.name)
            for field in fields(patch)
            if getattr(patch, field.name) is not UNSET
        }
        if "provenance" in values:
            raise ValueError("Hudson does not store provenance")
        current = self._target(identity, expected_revision, original)
        slot = current.revision.location[0] // 187
        header = original["MSGHDR.BBS"][slot * 187 : (slot + 1) * 187]
        words = _HEADER.unpack_from(header)
        payload = _Text(
            original["MSGTXT.BBS"], words[4], words[5], self.path / "MSGTXT.BBS"
        ).raw
        text_names = {
            "body_text",
            "control_lines",
            "external_id",
            "routing_seen_by",
            "routing_path",
            "from_address",
            "to_address",
        }
        text_changed = bool(values.keys() & text_names)
        serializing = bool(
            values.keys() & (text_names | {"from_name", "to_name", "subject"})
        )
        body = (
            payload.decode(self.options.target_charset, "strict").rstrip("\0")
            if serializing
            else ""
        )
        if serializing:
            _encode(body, self.options)
        if text_changed:
            body = self._body(body, values)
        if "from_address" in values or "to_address" in values:
            for name in ("from_address", "to_address"):
                if name not in values:
                    address = getattr(current.message, name)
                    values[name] = FtnAddress.from_string(address) if address else None
            body = self._body(
                body,
                {name: values[name] for name in ("from_address", "to_address")},
            )
        header = self._header(header, values, body)
        changed = dict(original)
        if text_changed:
            blocks = _text_blocks(
                _encode(
                    body.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r"),
                    self.options,
                )
            )
            start, count = len(original["MSGTXT.BBS"]) // 256, len(blocks) // 256
            raw = bytearray(header)
            struct.pack_into(
                "<HH",
                raw,
                8,
                _word(start, "text start"),
                _word(count, "text count"),
            )
            header = bytes(raw)
            changed["MSGTXT.BBS"] += blocks
        changed["MSGHDR.BBS"] = (
            original["MSGHDR.BBS"][: slot * 187]
            + header
            + original["MSGHDR.BBS"][(slot + 1) * 187 :]
        )
        old_header = original["MSGHDR.BBS"][slot * 187 : (slot + 1) * 187]
        if header[42:78] != old_header[42:78] or (header[24] ^ old_header[24]) & 16:
            changed["MSGTOIDX.BBS"] = (
                original["MSGTOIDX.BBS"][: slot * 36]
                + self._name_index(header)
                + original["MSGTOIDX.BBS"][(slot + 1) * 36 :]
            )
        self._scans(changed, slot, header[24])
        self._validate(changed, {name: self.path / name for name in _FILES})
        result = self._read(identity.msgno, changed)
        return _HudsonMutation(
            original=original,
            changed=changed,
            result=WriteResult(identity=result.identity, revision=result.revision),
        )

    def update(
        self,
        identity: MessageIdentity,
        patch: MessagePatch,
        expected_revision: RevisionToken,
    ) -> WriteResult:
        with self._operation() as (fds, original):
            mutation = self._build_update(original, identity, patch, expected_revision)
            self._commit("update", fds, original, mutation.changed)
            return mutation.result

    def delete(
        self, identity: MessageIdentity, expected_revision: RevisionToken
    ) -> MessageIdentity:
        with self._operation() as (fds, original):
            current = self._target(identity, expected_revision, original)
            slot = current.revision.location[0] // 187
            changed = dict(original)
            header = bytearray(original["MSGHDR.BBS"])
            header[slot * 187 + 24] |= 1
            changed["MSGHDR.BBS"] = bytes(header)
            index = bytearray(original["MSGIDX.BBS"])
            struct.pack_into("<H", index, slot * 3, 65535)
            changed["MSGIDX.BBS"] = bytes(index)
            changed["MSGTOIDX.BBS"] = (
                original["MSGTOIDX.BBS"][: slot * 36]
                + _pascal(b"* Deleted *", 36)
                + original["MSGTOIDX.BBS"][(slot + 1) * 36 :]
            )
            info = list(_INFO.unpack(original["MSGINFO.BBS"]))
            info[2] -= 1
            info[self.board + 2] -= 1
            active = [
                number
                for number, _ in struct.iter_unpack("<HB", changed["MSGIDX.BBS"])
                if number != 65535
            ]
            info[0] = min(active, default=0)
            # Keep high as the allocation watermark; deleted numbers are not reused.
            changed["MSGINFO.BBS"] = _INFO.pack(*info)
            self._scans(changed, slot, 0, True)
            self._validate(changed, {name: self.path / name for name in _FILES})
            self._commit("delete", fds, original, changed)
            return identity


class HudsonRecoverableSession(BaseRecoverableSession):
    """Held-lock Hudson recovery session for one physical base and all boards."""

    def __init__(
        self,
        store: RecoveryStore,
        *,
        helper: HudsonSession,
        fds: dict[str, int],
        rels: dict[str, tuple[str, str]],
        created_fds: list[int],
    ) -> None:
        super().__init__(store)
        self._helper = helper
        self._fds = fds
        self._rels = rels
        self._created_fds = created_fds
        self._io = helper._io
        self._file_paths = {name: helper.path / name for name in _FILES}

    def _snapshot(self) -> dict[str, bytes]:
        return _snapshot_raw(self._io, self._fds)

    def _forget_scan_fd(self, name: str) -> None:
        fd = self._fds.pop(name, None)
        if fd is None:
            return
        if fd in self._created_fds:
            self._created_fds.remove(fd)
        root_key, rel_path = self._rels[name]
        inode: tuple[int, int] | None = None
        try:
            st = os.fstat(fd)
            inode = (st.st_dev, st.st_ino)
        except OSError:
            pass
        retained_fds = self.store._retained_fds
        retained_paths = self.store._retained_by_path
        if inode is not None:
            retained_fds.pop(inode, None)
        retained_paths.pop((root_key, rel_path), None)
        for inode_key, retained in list(retained_fds.items()):
            if retained == fd:
                retained_fds.pop(inode_key, None)
        for path_key, retained in list(retained_paths.items()):
            if retained == fd:
                retained_paths.pop(path_key, None)
        try:
            os.close(fd)
        except OSError:
            pass

    def _forget_unlinked_scans(self) -> None:
        for name in _SCANS:
            if name not in self._fds:
                continue
            if _locate_recovery_file(self._helper.scan_path, name) is None:
                self._forget_scan_fd(name)

    def _existing_scans(self) -> set[str]:
        existing: set[str] = set()
        for name in _SCANS:
            path = _locate_recovery_file(self._helper.scan_path, name)
            if path is None:
                continue
            existing.add(name)
            fd = self._fds.get(name)
            if fd is None:
                raise RecoveryError(code="unsafe_recovery_path")
            _require_fd_matches_path(fd, path)
        return existing

    def _verify_open_files(self) -> None:
        for name in _FILES:
            fd = self._fds[name]
            path = _locate_recovery_file(self._helper.path, name)
            if path is None:
                raise RecoveryError(code="unsafe_recovery_path")
            _require_fd_matches_path(fd, path)
        self._forget_unlinked_scans()
        for name in _SCANS:
            if name not in self._fds:
                continue
            path = _locate_recovery_file(self._helper.scan_path, name)
            if path is None:
                raise RecoveryError(code="unsafe_recovery_path")
            _require_fd_matches_path(self._fds[name], path)

    def recover(
        self,
        assertion: OfflineRecoveryAssertion,
        resolve: Callable[[UUID], CommitState],
    ) -> tuple[RecoveryReference, ...]:
        recovered = super().recover(assertion, resolve)
        self._forget_unlinked_scans()
        return recovered

    def settle(self, mutation: PreparedMutation, state: CommitState) -> None:
        super().settle(mutation, state)
        self._forget_unlinked_scans()

    @contextmanager
    def _using_board(self, board: int) -> Iterator[HudsonSession]:
        helper = self._helper
        original = helper.board
        helper.board = _require_board(board)
        try:
            yield helper
        finally:
            helper.board = original

    def _message_board(self, raw: dict[str, bytes], msgno: int) -> int:
        if (
            isinstance(msgno, bool)
            or not isinstance(msgno, int)
            or not 1 <= msgno <= 65534
        ):
            raise ValueError("Hudson message number must be in 1..65534")
        for number, board in struct.iter_unpack("<HB", raw["MSGIDX.BBS"]):
            if number == msgno:
                return int(board)
        raise ConflictError(f"Hudson message {msgno} is absent")

    def _plan_for_recovery(
        self, planner: Callable[[dict[str, bytes]], _HudsonMutation]
    ) -> _HudsonMutation:
        try:
            self._verify_open_files()
            raw = self._snapshot()
            self._helper._validate(raw, self._file_paths)
            return planner(raw)
        except RecoveryError:
            raise
        except (UnicodeEncodeError, UnicodeDecodeError):
            _raise_recovery_input()
        except OSError:
            _raise_recovery_os()

    def read(self, msgno: int) -> SessionMessage:
        self.check_usable()
        raw = self._snapshot()
        self._helper._validate(raw, self._file_paths)
        board = self._message_board(raw, msgno)
        with self._using_board(board) as helper:
            return helper._read(msgno, raw)

    def prepare_append(
        self, transaction_id: UUID, message: OutgoingMessage
    ) -> PreparedMutation:
        self.check_usable()

        def plan(raw: dict[str, bytes]) -> _HudsonMutation:
            header, blocks = self._helper._encode_append(message)
            return self._helper._build_append(raw, header, blocks)

        mutation = self._plan_for_recovery(plan)
        return self._register(transaction_id, RecoveryOperationKind.APPEND, mutation)

    def prepare_update(
        self,
        transaction_id: UUID,
        identity: MessageIdentity,
        patch: MessagePatch,
        expected_revision: RevisionToken,
    ) -> PreparedMutation:
        self.check_usable()

        def plan(raw: dict[str, bytes]) -> _HudsonMutation:
            board = identity.board if identity.board is not None else self._helper.board
            with self._using_board(board) as helper:
                return helper._build_update(raw, identity, patch, expected_revision)

        mutation = self._plan_for_recovery(plan)
        return self._register(transaction_id, RecoveryOperationKind.UPDATE, mutation)

    def _register(
        self,
        transaction_id: UUID,
        operation: RecoveryOperationKind,
        mutation: _HudsonMutation,
    ) -> PreparedMutation:
        existing_scans = self._existing_scans()
        plans = _file_plans(
            mutation.original,
            mutation.changed,
            self._fds,
            self._rels,
            existing_scans,
        )

        def apply_fn() -> WriteResult:
            try:
                return _apply_changed(
                    self._io,
                    self._fds,
                    mutation,
                    self._helper.scan_path,
                    self.store,
                    transaction_id,
                    self._rels,
                    self._created_fds,
                )
            except RecoveryError:
                raise
            except OSError:
                raise RecoveryError(code="unexpected_base_modification") from None
            except (UnicodeEncodeError, UnicodeDecodeError):
                _raise_recovery_input()

        return self.register_prepared_mutation(
            transaction_id, operation, plans, apply_fn
        )

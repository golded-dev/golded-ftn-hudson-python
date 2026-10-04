"""Strict indexed reader for classic Hudson .BBS message bases."""

import codecs
import re
import stat
import struct
from bisect import bisect_right
from collections.abc import Iterator
from datetime import datetime
from os import PathLike
from pathlib import Path
from typing import Literal

from golded_ftn import (
    MessageProvenance,
    ParsedMessage,
    ParserException,
    ReaderIssue,
    ReaderOptions,
    detect_charset,
    parse_body,
    synthetic_id,
    to_utf8,
)

from ._controls import parse_controls, resolve_addresses

_HEADER_SIZE = 187
_INDEX_SIZE = 3
_BLOCK_SIZE = 256


class _ParseFailure(ParserException):
    def __init__(self, path: Path, offset: int, error: Exception) -> None:
        super().__init__(f"Cannot parse Hudson file {path} at byte {offset}: {error}")
        self.path = path
        self.offset = offset


def _fail(path: Path, offset: int, error: Exception) -> _ParseFailure:
    return _ParseFailure(path, offset, error)


def _invalid(path: Path, offset: int, reason: str) -> None:
    error = ValueError(reason)
    raise _fail(path, offset, error) from error


def _find(directory: Path, filename: str) -> Path:
    matches = [
        p for p in directory.iterdir() if p.name.casefold() == filename.casefold()
    ]
    if not matches:
        raise FileNotFoundError(directory / filename)
    if len(matches) > 1:
        _invalid(
            directory, 0, f"Ambiguous {filename}: {', '.join(str(p) for p in matches)}"
        )
    file = matches[0]
    if not stat.S_ISREG(file.lstat().st_mode):
        raise OSError(f"Expected a regular file: {file}")
    return file


def _aligned(raw: bytes, size: int, path: Path) -> None:
    if len(raw) % size:
        _invalid(path, len(raw) - len(raw) % size, f"Truncated {size}-byte record")


def _pascal(raw: bytes, start: int, width: int, path: Path, base: int) -> bytes:
    length = raw[start]
    if length >= width:
        _invalid(
            path, base + start, f"Pascal length {length} exceeds capacity {width - 1}"
        )
    return raw[start + 1 : start + 1 + length]


def _date(date: bytes, clock: bytes) -> datetime | None:
    if not re.fullmatch(rb"\d{2}-\d{2}-\d{2}", date) or not re.fullmatch(
        rb"\d{2}:\d{2}", clock
    ):
        return None
    month, day, year = (int(value) for value in date.split(b"-"))
    hour, minute = (int(value) for value in clock.split(b":"))
    try:
        return datetime(
            2000 + year if year < 80 else 1900 + year, month, day, hour, minute
        )
    except ValueError:
        return None


class _Text:
    """Concatenated Pascal payload with a map back to physical file bytes."""

    def __init__(self, raw: bytes, start: int, count: int, path: Path) -> None:
        # A zero-block body has no text address; old startrec values are inert.
        base = start * _BLOCK_SIZE if count else 0
        length = count * _BLOCK_SIZE
        if base > len(raw) or length > len(raw) - base:
            _invalid(path, base, f"Text span of {length} bytes lies outside file")
        payloads: list[bytes] = []
        self.starts: list[int] = []
        self.physical: list[int] = []
        total = 0
        for index in range(count):
            block = base + index * _BLOCK_SIZE
            length = raw[block]
            # Empty blocks contribute no bytes and cannot own a logical offset.
            if length:
                self.starts.append(total)
                self.physical.append(block + 1)
                payloads.append(raw[block + 1 : block + 1 + length])
                total += length
        self.raw = b"".join(payloads)
        self.base = base

    def offset(self, logical: int) -> int:
        if not self.starts:
            return self.base
        index = max(0, bisect_right(self.starts, logical) - 1)
        return self.physical[index] + logical - self.starts[index]


def _charset(text: _Text, options: ReaderOptions, path: Path) -> str:
    try:
        selected = detect_charset(text.raw, options.fallback_charset)
        declared: str | None = None
        for match in re.finditer(
            rb"\x01(?:CHRS|CHARSET):\s*([^\s\x00\x01]+)", text.raw, re.IGNORECASE
        ):
            cp = codecs.lookup(detect_charset(match[0], "CP850")).name
            utf = codecs.lookup(detect_charset(match[0], "UTF-8")).name
            name = match[1].upper().hex()
            current = cp if cp == utf else "unknown:" + name
            if declared is not None and current != declared:
                _invalid(
                    path, text.offset(match.start()), "Conflicting charset declarations"
                )
            declared = current
        return selected
    except (LookupError, ValueError) as error:
        raise _fail(path, text.base, error) from error


def _decode(raw: bytes, charset: str, path: Path, offset: int) -> str:
    try:
        return to_utf8(raw, charset)
    except (UnicodeError, LookupError) as error:
        position = error.start if isinstance(error, UnicodeDecodeError) else 0
        raise _fail(path, offset + position, error) from error


def _validate_ids(text: _Text, charset: str, path: Path) -> None:
    values: dict[str, str] = {}
    for match in re.finditer(
        rb"(?:^|[\r\n])\x01(MSGID|REPLY):([^\r\n]*)", text.raw, re.IGNORECASE
    ):
        name = match[1].decode("ascii").upper()
        value = _decode(match[2], charset, path, text.offset(match.start(2))).strip()
        if name in values and values[name] != value:
            _invalid(
                path,
                text.offset(match.start(1) - 1),
                f"Conflicting {name} declarations",
            )
        values[name] = value


class HudsonReader:
    """Read all boards in a stable classic Hudson directory.

    Files are read separately without locking. All active records are validated
    before the first message is returned; concurrent writes require coordination
    by the caller.
    """

    def read(
        self, path: str | PathLike[str], options: ReaderOptions | None = None
    ) -> Iterator[ParsedMessage]:
        options = options or ReaderOptions()
        if options.archive_mode:
            yield from self._read_archive(Path(path), options)
        else:
            yield from self._read_strict(path, options)

    @staticmethod
    def _issue(
        options: ReaderOptions,
        error: ParserException,
        action: Literal["recovered", "skipped", "stopped"],
        code: str,
        source_id: str | None = None,
    ) -> None:
        # Only location data is copied from parser errors; their cause may contain text.
        assert isinstance(error, _ParseFailure)
        assert options.on_issue is not None
        options.on_issue(
            ReaderIssue(
                source_type="hudson",
                source_path=str(error.path),
                action=action,
                code=code,
                detail="ASCII decoding recovered with configured fallback."
                if action == "recovered"
                else "Record validation failed."
                if action == "skipped"
                else "Safe record traversal stopped.",
                source_id=source_id,
                source_offset=error.offset,
            )
        )

    def _read_archive(
        self, directory: Path, options: ReaderOptions
    ) -> Iterator[ParsedMessage]:
        try:
            paths = [
                _find(directory, name)
                for name in ("MSGIDX.BBS", "MSGHDR.BBS", "MSGTXT.BBS")
            ]
            index, headers, texts = (file.read_bytes() for file in paths)
            for raw, size, file in zip(
                (index, headers, texts), (3, 187, 256), paths, strict=True
            ):
                _aligned(raw, size, file)
        except ParserException as error:
            self._issue(options, error, "stopped", "unsafe_structure")
            return
        index_path, header_path, text_path = paths
        result: list[ParsedMessage] = []
        seen: set[int] = set()
        for slot in range(len(index) // 3):
            msgno, board = struct.unpack_from("<HB", index, slot * 3)
            if msgno == 0xFFFF:
                continue
            base = slot * 187
            if base + 187 > len(headers):
                structure_error = _fail(
                    header_path, base, ValueError("Indexed header is missing")
                )
                self._issue(
                    options, structure_error, "stopped", "unsafe_structure", str(msgno)
                )
                break
            raw = headers[base : base + 187]
            words = struct.unpack_from("<10H2BH3B", raw)
            if words[13] & 1:
                continue
            if msgno in seen:
                duplicate_error = _fail(
                    index_path, slot * 3, ValueError("Duplicate active message number")
                )
                self._issue(
                    options,
                    duplicate_error,
                    "stopped",
                    "duplicate_message_number",
                    str(msgno),
                )
                break
            seen.add(msgno)
            failure: ParserException | None = None
            recovered: ParserException | None = None
            try:
                if not 1 <= msgno <= 65534:
                    _invalid(index_path, slot * 3, "Invalid active message number")
                if not 1 <= board <= 200:
                    _invalid(index_path, slot * 3 + 2, "Invalid active board")
                if words[0] != msgno:
                    _invalid(
                        header_path, base, "Header message number differs from index"
                    )
                if words[15] != board:
                    _invalid(header_path, base + 26, "Header board differs from index")
                try:
                    message = self._message(
                        raw,
                        base,
                        msgno,
                        board,
                        words,
                        texts,
                        header_path,
                        text_path,
                        options,
                    )
                except ParserException as error:
                    cause = error.__cause__
                    if (
                        not isinstance(cause, UnicodeDecodeError)
                        or cause.encoding != "ascii"
                    ):
                        raise
                    fallback = detect_charset(b"", options.fallback_charset)
                    message = self._message(
                        raw,
                        base,
                        msgno,
                        board,
                        words,
                        texts,
                        header_path,
                        text_path,
                        options,
                        fallback,
                    )
                    recovered = error
            except ParserException as error:
                failure = error
            if failure is not None:
                self._issue(
                    options, failure, "skipped", "record_parse_error", str(msgno)
                )
                continue
            if recovered is not None:
                self._issue(
                    options, recovered, "recovered", "ascii_decode_fallback", str(msgno)
                )
            result.append(message)
        yield from sorted(result, key=lambda message: message.msgno)

    def _read_strict(
        self, path: str | PathLike[str], options: ReaderOptions | None = None
    ) -> Iterator[ParsedMessage]:
        options = options or ReaderOptions()
        directory = Path(path)
        index_path = _find(directory, "MSGIDX.BBS")
        header_path = _find(directory, "MSGHDR.BBS")
        text_path = _find(directory, "MSGTXT.BBS")
        index, headers, texts = (
            file.read_bytes() for file in (index_path, header_path, text_path)
        )
        _aligned(index, _INDEX_SIZE, index_path)
        _aligned(headers, _HEADER_SIZE, header_path)
        _aligned(texts, _BLOCK_SIZE, text_path)
        if len(headers) // _HEADER_SIZE < len(index) // _INDEX_SIZE:
            _invalid(header_path, len(headers), "Indexed header record is missing")
        result: list[ParsedMessage] = []
        seen: set[int] = set()
        for slot in range(len(index) // _INDEX_SIZE):
            msgno, board = struct.unpack_from("<HB", index, slot * _INDEX_SIZE)
            if msgno == 0xFFFF:
                continue
            base = slot * _HEADER_SIZE
            raw = headers[base : base + _HEADER_SIZE]
            words = struct.unpack_from("<10H2BH3B", raw)
            if words[13] & 1:
                continue
            if not 1 <= msgno <= 65534:
                _invalid(
                    index_path,
                    slot * _INDEX_SIZE,
                    f"Invalid active message number {msgno}",
                )
            if not 1 <= board <= 200:
                _invalid(
                    index_path, slot * _INDEX_SIZE + 2, f"Invalid active board {board}"
                )
            if words[0] != msgno:
                _invalid(
                    header_path,
                    base,
                    f"Header message number {words[0]} differs from index {msgno}",
                )
            if words[15] != board:
                _invalid(
                    header_path,
                    base + 26,
                    f"Header board {words[15]} differs from index {board}",
                )
            if msgno in seen:
                _invalid(
                    index_path,
                    slot * _INDEX_SIZE,
                    f"Duplicate active message number {msgno}",
                )
            seen.add(msgno)
            result.append(
                self._message(
                    raw,
                    base,
                    msgno,
                    board,
                    words,
                    texts,
                    header_path,
                    text_path,
                    options,
                )
            )
        yield from sorted(result, key=lambda message: message.msgno)

    @staticmethod
    def _message(
        raw: bytes,
        base: int,
        msgno: int,
        board: int,
        words: tuple[int, ...],
        texts: bytes,
        header_path: Path,
        text_path: Path,
        options: ReaderOptions,
        charset_override: str | None = None,
    ) -> ParsedMessage:
        date = _pascal(raw, 33, 9, header_path, base)
        clock = _pascal(raw, 27, 6, header_path, base)
        text = _Text(texts, words[4], words[5], text_path)
        charset = _charset(text, options, text_path)
        if charset_override is not None:
            charset = charset_override
        decoded: list[str] = []
        for offset, width in ((78, 36), (42, 36), (114, 73)):
            value = _pascal(raw, offset, width, header_path, base)
            decoded.append(_decode(value, charset, header_path, base + offset + 1))
        sender, recipient, subject = decoded
        try:
            body = parse_body(to_utf8(text.raw, charset))
        except (UnicodeError, LookupError) as error:
            position = error.start if isinstance(error, UnicodeDecodeError) else 0
            raise _fail(text_path, text.offset(position), error) from error
        controls = parse_controls(body)
        _validate_ids(text, charset, text_path)
        addresses = {
            "from_zone": words[11],
            "from_net": words[8],
            "from_node": words[9],
            "from_point": 0,
            "to_zone": words[10],
            "to_net": words[6],
            "to_node": words[7],
            "to_point": 0,
        }
        try:
            origin, destination = resolve_addresses(addresses, controls.kludges)
        except ValueError as error:
            # Locate the first failing prefix only on the error path.
            offset = text.base
            line_offsets = [
                match.start()
                for match in re.finditer(
                    rb"(?:^|(?<=[\r\n]))\x01(?:INTL|FMPT|TOPT)(?::|\s)",
                    text.raw,
                    re.IGNORECASE,
                )
            ]
            address_controls = [
                control
                for control in controls.kludges
                if control.name.upper() in {"INTL", "FMPT", "TOPT"}
            ]
            for count in range(1, len(address_controls) + 1):
                try:
                    resolve_addresses(addresses, address_controls[:count])
                except ValueError:
                    if count <= len(line_offsets):
                        offset = text.offset(line_offsets[count - 1])
                    break
            raise _fail(text_path, offset, error) from error
        posted = _date(date, clock)
        external_id = controls.msgid
        if external_id is None:
            external_id = synthetic_id(
                sender, recipient, subject, posted.isoformat() if posted else None, body
            )
        return ParsedMessage(
            msgno=msgno,
            from_name=sender,
            to_name=recipient,
            subject=subject,
            body_text=body,
            attributes_raw=words[13] | (words[14] << 8),
            posted_at=posted,
            external_id=external_id,
            from_address=str(origin) if origin else None,
            to_address=str(destination) if destination else None,
            reply_to_msgno=words[1] or None,
            reply1st_msgno=words[2] or None,
            area_code=f"BOARD{board}",
            area_meta_key=f"hudson:{board}",
            control_lines=controls,
            provenance=MessageProvenance(
                source_type="hudson",
                source_path=str(header_path),
                source_id=str(msgno),
                source_offset=base,
            ),
        )

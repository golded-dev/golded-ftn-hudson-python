"""Independent classic Hudson fixtures: no production binary helpers."""

import os
import struct
import time
from datetime import datetime
from pathlib import Path
from typing import TypedDict, Unpack

import pytest
from golded_ftn import (
    MessageBaseReader,
    ParsedMessage,
    ParserException,
    ReaderOptions,
    synthetic_id,
)

from golded_ftn_hudson import HudsonReader


def pascal(value: bytes, width: int) -> bytes:
    assert len(value) < width
    return bytes([len(value)]) + value + b"x" * (width - len(value) - 1)


def header(
    number: int = 42,
    board: int = 7,
    start: int = 0,
    blocks: int = 1,
    *,
    attr: int = 0,
    netattr: int = 0,
    date: bytes = b"01-02-24",
    clock: bytes = b"12:34",
    sender: bytes = b"Odinn",
    subject: bytes = b"Subject",
) -> bytes:
    return (
        struct.pack(
            "<10H2BH3B",
            number,
            12,
            13,
            19,
            start,
            blocks,
            230,
            0,
            230,
            150,
            2,
            2,
            0,
            attr,
            netattr,
            board,
        )
        + pascal(clock, 6)
        + pascal(date, 9)
        + pascal(b"Reader", 36)
        + pascal(sender, 36)
        + pascal(subject, 73)
    )


def text(body: bytes) -> bytes:
    payload = body + b"\0"
    return b"".join(
        pascal(payload[i : i + 255], 256) for i in range(0, len(payload), 255)
    )


class HeaderOptions(TypedDict, total=False):
    attr: int
    netattr: int
    date: bytes
    clock: bytes
    sender: bytes
    subject: bytes


def area(
    path: Path,
    body: bytes = b"Hello\r",
    *,
    number: int = 42,
    board: int = 7,
    **kwargs: Unpack[HeaderOptions],
) -> Path:
    path.mkdir(exist_ok=True)
    raw_text = text(body)
    (path / "MSGIDX.BBS").write_bytes(struct.pack("<HB", number, board))
    (path / "MSGHDR.BBS").write_bytes(
        header(number, board, blocks=len(raw_text) // 256, **kwargs)
    )
    (path / "MSGTXT.BBS").write_bytes(raw_text)
    return path


def messages(path: Path) -> list[ParsedMessage]:
    return list(HudsonReader().read(path))


def test_public_contract_and_mapping(tmp_path: Path) -> None:
    reader: MessageBaseReader = HudsonReader()
    base = area(tmp_path, b"\x01MSGID: 2:230/150 abc\rHello\r", attr=0x40, netattr=0x80)
    result = list(reader.read(base))
    assert len(result) == 1
    message = result[0]
    assert message.msgno == 42
    assert (message.from_name, message.to_name, message.subject) == (
        "Odinn",
        "Reader",
        "Subject",
    )
    assert message.body_text.endswith("Hello\n")
    assert message.external_id == "2:230/150 abc"
    assert message.attributes_raw == 0x8040
    assert message.posted_at == datetime(2024, 1, 2, 12, 34)
    assert message.from_address == "2:230/150"
    assert message.to_address == "2:230/0"
    assert (
        message.reply_to_msgno,
        message.reply1st_msgno,
        message.reply_next_msgno,
    ) == (12, 13, None)
    assert message.area_code == "BOARD7"
    assert message.area_meta_key == "hudson:7"
    assert message.area_name is None
    assert message.area_sort_order is None
    assert message.provenance is not None
    assert (
        message.provenance.source_type,
        message.provenance.source_path,
        message.provenance.source_id,
        message.provenance.source_offset,
    ) == ("hudson", str(base / "MSGHDR.BBS"), "42", 0)


@pytest.mark.parametrize("size", [0, 1, 253, 254, 255, 256, 509, 510, 511, 1000])
def test_pascal_blocks(size: int, tmp_path: Path) -> None:
    assert messages(area(tmp_path, b"a" * size))[0].body_text == "a" * size


def test_empty_and_zero_records(tmp_path: Path) -> None:
    for name in ("MSGIDX.BBS", "MSGHDR.BBS", "MSGTXT.BBS"):
        (tmp_path / name).touch()
    assert messages(tmp_path) == []
    (tmp_path / "MSGIDX.BBS").write_bytes(struct.pack("<HB", 42, 7))
    (tmp_path / "MSGHDR.BBS").write_bytes(header(blocks=0))
    assert messages(tmp_path)[0].body_text == ""


def test_index_authority_order_deletions_and_old_data(tmp_path: Path) -> None:
    indices = [(42, 7), (0xFFFF, 0), (9, 2), (71, 5)]
    (tmp_path / "MSGIDX.BBS").write_bytes(
        b"".join(struct.pack("<HB", *x) for x in indices)
    )
    (tmp_path / "MSGHDR.BBS").write_bytes(
        header(42, 7)
        + bytes(187)
        + header(9, 2)
        + header(71, 5, attr=1, start=65535)
        + header(100, 3, start=65535)
    )
    (tmp_path / "MSGTXT.BBS").write_bytes(text(b"hello") + bytes(256))
    result = messages(tmp_path)
    assert [m.msgno for m in result] == [9, 42]
    assert result[0].provenance is not None
    assert result[0].provenance.source_offset == 374


@pytest.mark.parametrize("number", [32768, 65534])
def test_unsigned_message_numbers(tmp_path: Path, number: int) -> None:
    assert messages(area(tmp_path, number=number))[0].msgno == number


@pytest.mark.parametrize("filename", ["MSGIDX.BBS", "MSGHDR.BBS", "MSGTXT.BBS"])
def test_truncated_records(tmp_path: Path, filename: str) -> None:
    area(tmp_path)
    file = tmp_path / filename
    file.write_bytes(file.read_bytes()[:-1])
    with pytest.raises(ParserException, match=filename) as error:
        messages(tmp_path)
    assert error.value.__cause__ is not None


@pytest.mark.parametrize(
    "offset,value", [(0, 43), (26, 8), (27, 6), (33, 9), (42, 36), (78, 36), (114, 73)]
)
def test_header_conflicts_and_pascal_overflow(
    tmp_path: Path, offset: int, value: int
) -> None:
    area(tmp_path)
    file = tmp_path / "MSGHDR.BBS"
    raw = bytearray(file.read_bytes())
    raw[offset] = value
    file.write_bytes(raw)
    with pytest.raises(ParserException, match="MSGHDR.BBS"):
        messages(tmp_path)


@pytest.mark.parametrize("number,board", [(0, 7), (42, 0), (42, 201)])
def test_invalid_index_values(tmp_path: Path, number: int, board: int) -> None:
    area(tmp_path, number=number, board=board)
    with pytest.raises(ParserException):
        messages(tmp_path)


def test_duplicate_active_numbers(tmp_path: Path) -> None:
    area(tmp_path)
    for filename in ("MSGIDX.BBS", "MSGHDR.BBS"):
        file = tmp_path / filename
        file.write_bytes(file.read_bytes() * 2)
    with pytest.raises(ParserException, match="Duplicate"):
        messages(tmp_path)


def test_no_partial_result(tmp_path: Path) -> None:
    area(tmp_path)
    idx = tmp_path / "MSGIDX.BBS"
    idx.write_bytes(idx.read_bytes() + struct.pack("<HB", 43, 7))
    iterator = iter(HudsonReader().read(tmp_path))
    with pytest.raises(ParserException):
        next(iterator)


def test_outside_text_span(tmp_path: Path) -> None:
    area(tmp_path)
    (tmp_path / "MSGHDR.BBS").write_bytes(header(start=1))
    with pytest.raises(ParserException, match="MSGTXT.BBS.*256"):
        messages(tmp_path)


def test_mixed_case_and_missing_files(tmp_path: Path) -> None:
    area(tmp_path)
    for name in ("MSGIDX.BBS", "MSGHDR.BBS", "MSGTXT.BBS"):
        (tmp_path / name).rename(tmp_path / name.swapcase())
    assert len(messages(tmp_path)) == 1
    (tmp_path / "msgidx.bbs").unlink()
    with pytest.raises(FileNotFoundError):
        messages(tmp_path)


def test_wrong_path_type(tmp_path: Path) -> None:
    file = tmp_path / "file"
    file.touch()
    with pytest.raises(NotADirectoryError):
        messages(file)
    area(tmp_path)
    (tmp_path / "MSGHDR.BBS").unlink()
    (tmp_path / "MSGHDR.BBS").mkdir()
    with pytest.raises(OSError):
        messages(tmp_path)


def test_ambiguous_files(tmp_path: Path) -> None:
    area(tmp_path)
    alternate = tmp_path / "msGhDR.bBs"
    alternate.write_bytes((tmp_path / "MSGHDR.BBS").read_bytes())
    if len(list(tmp_path.iterdir())) != 4:
        pytest.skip("Filesystem is case-insensitive")
    with pytest.raises(ParserException, match="Ambiguous"):
        messages(tmp_path)


@pytest.mark.parametrize(
    "date,clock,expected",
    [
        (b"", b"", None),
        (b"bad", b"12:00", None),
        (b"02-30-24", b"12:00", None),
        (b"01-01-24", b"", None),
        (b"01-01-79", b"12:00", datetime(2079, 1, 1, 12)),
        (b"01-01-80", b"12:00", datetime(1980, 1, 1, 12)),
    ],
)
def test_dates(
    tmp_path: Path, date: bytes, clock: bytes, expected: datetime | None
) -> None:
    assert messages(area(tmp_path, date=date, clock=clock))[0].posted_at == expected


def test_timezone_independent_dates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not hasattr(time, "tzset"):
        pytest.skip("tzset unavailable")
    original = os.environ.get("TZ")
    try:
        area(tmp_path)
        result = []
        for zone in ("UTC0", "EST5EDT"):
            monkeypatch.setenv("TZ", zone)
            time.tzset()
            result.append(messages(tmp_path)[0].posted_at)
        assert result == [datetime(2024, 1, 2, 12, 34)] * 2
    finally:
        if original is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", original)
        time.tzset()


def test_charset_and_boundary_utf8(tmp_path: Path) -> None:
    body = b"\x01CHRS: UTF-8 4\r" + b"a" * 239 + "æ".encode() + b"\r"
    assert body[254:256] == "æ".encode()
    result = messages(area(tmp_path, body, sender="Søren".encode()))[0]
    assert result.from_name == "Søren"
    assert result.body_text.endswith("æ\n")
    assert (
        result.control_lines is not None and result.control_lines.charset == "UTF-8 4"
    )


def test_cp850_and_fallback(tmp_path: Path) -> None:
    assert messages(area(tmp_path, "blå".encode("cp850")))[0].body_text == "blå"
    area(tmp_path, b"\xff")
    assert (
        list(
            HudsonReader().read(tmp_path, ReaderOptions(fallback_charset="ISO-8859-1"))
        )[0].body_text
        == "ÿ"
    )


def test_decoding_error_physical_offset(tmp_path: Path) -> None:
    body = b"\x01CHRS: UTF-8 4\r" + b"a" * 240 + b"\xff"
    assert len(body) == 256
    area(tmp_path, body)
    with pytest.raises(ParserException, match="MSGTXT.BBS.*257") as error:
        messages(tmp_path)
    assert isinstance(error.value.__cause__, UnicodeDecodeError)


def test_charset_conflict_across_blocks(tmp_path: Path) -> None:
    body = b"\x01CHRS: UTF-8 4\r" + b"a" * 239 + b"\r\x01CHRS: CP850 2\r"
    area(tmp_path, body)
    with pytest.raises(ParserException, match="MSGTXT.BBS.*257"):
        messages(tmp_path)


def test_address_points_routing_and_ids(tmp_path: Path) -> None:
    body = (
        b"\x01INTL 2:230/0 2:230/150\r\x01FMPT 4\r\x01TOPT 8\r"
        b"\x01MSGID: abc\r\x01REPLY: def\rSEEN-BY: 230/150\rSEEN-BY: 230/151\r"
        b"\x01PATH: 230/150\rText\r"
    )
    m = messages(area(tmp_path, body))[0]
    assert (m.from_address, m.to_address, m.external_id) == (
        "2:230/150.4",
        "2:230/0.8",
        "abc",
    )
    assert m.control_lines is not None
    assert m.control_lines.reply == "def"
    assert m.control_lines.seen_by == ("230/150", "230/151")
    assert m.control_lines.path == ("230/150",)


@pytest.mark.parametrize(
    "body",
    [
        b"\x01INTL 2:230/1 2:230/150\r",
        b"\x01FMPT 1\r\x01FMPT 2\r",
        b"\x01MSGID: a\r\x01MSGID: b\r",
        b"\x01REPLY: a\r\x01REPLY: b\r",
    ],
)
def test_conflicting_controls(tmp_path: Path, body: bytes) -> None:
    with pytest.raises(ParserException):
        messages(area(tmp_path, body))


def test_synthetic_id(tmp_path: Path) -> None:
    m = messages(area(tmp_path, b"hello\r"))[0]
    assert m.external_id == synthetic_id(
        "Odinn", "Reader", "Subject", "2024-01-02T12:34:00", "hello\n"
    )


def test_unknown_charset_declarations_must_agree(tmp_path: Path) -> None:
    with pytest.raises(ParserException, match="Conflicting charset"):
        messages(area(tmp_path, b"\x01CHRS: X-FOO 2\r\x01CHARSET: X-BAR\r"))


def test_equal_ids_with_final_null(tmp_path: Path) -> None:
    assert messages(area(tmp_path, b"\x01MSGID: a\r\x01MSGID: a"))[0].external_id == "a"


def test_address_conflict_physical_offset(tmp_path: Path) -> None:
    body = b"a" * 254 + b"\r\x01FMPT 1\r\x01FMPT 2\r"
    area(tmp_path, body)
    with pytest.raises(ParserException, match="MSGTXT.BBS.*265"):
        messages(tmp_path)


def test_empty_explicit_msgid_is_preserved(tmp_path: Path) -> None:
    message = messages(area(tmp_path, b"\x01MSGID:\r"))[0]
    assert message.external_id == ""
    assert message.control_lines is not None
    assert message.control_lines.msgid == ""


def test_split_charset_declaration(tmp_path: Path) -> None:
    body = b"a" * 247 + b"\r\x01CHRS: UTF-8 4\r" + "ø".encode()
    message = messages(area(tmp_path, body, sender="Søren".encode()))[0]
    assert message.from_name == "Søren"
    assert message.body_text.endswith("ø")


def test_unknown_charset_conflict_physical_offset(tmp_path: Path) -> None:
    body = b"\x01CHRS: X-FOO\r" + b"a" * 241 + b"\r\x01CHRS: X-BAR\r"
    assert body[255] == 1
    with pytest.raises(ParserException, match="MSGTXT.BBS.*257"):
        messages(area(tmp_path, body))


def test_matching_aliases_and_repeated_controls(tmp_path: Path) -> None:
    body = b"\x01CHRS: IBM850 2\r\x01CHARSET: CP850\r\x01MSGID: abc\r\x01MSGID: abc\r"
    message = messages(area(tmp_path, body))[0]
    assert message.external_id == "abc"
    assert message.control_lines is not None
    assert len(message.control_lines.kludges) == 4


def test_nonzero_text_start_and_zero_links(tmp_path: Path) -> None:
    area(tmp_path)
    raw = bytearray(header(start=2))
    raw[2:6] = b"\0" * 4
    raw[12:22] = b"\0" * 10
    (tmp_path / "MSGHDR.BBS").write_bytes(raw)
    (tmp_path / "MSGTXT.BBS").write_bytes(bytes(512) + text(b"Moved\r"))
    message = messages(tmp_path)[0]
    assert message.body_text == "Moved\n"
    assert message.reply_to_msgno is message.reply1st_msgno is None
    assert message.from_address is message.to_address is None


def test_pascal_headers_decode_strictly(tmp_path: Path) -> None:
    area(tmp_path, b"\x01CHRS: UTF-8 4\r", sender=b"a\xff")
    with pytest.raises(ParserException, match="MSGHDR.BBS.*80") as error:
        messages(tmp_path)
    assert isinstance(error.value.__cause__, UnicodeDecodeError)


def test_regular_files_reject_symlinks(tmp_path: Path) -> None:
    area(tmp_path)
    file = tmp_path / "MSGHDR.BBS"
    target = tmp_path / "header-data"
    file.rename(target)
    try:
        file.symlink_to(target)
    except OSError:
        pytest.skip("Symlink creation unavailable")
    with pytest.raises(OSError, match="regular file"):
        messages(tmp_path)


def test_short_and_empty_text_blocks_do_not_leak_padding(tmp_path: Path) -> None:
    area(tmp_path)
    (tmp_path / "MSGHDR.BBS").write_bytes(header(blocks=3))
    (tmp_path / "MSGTXT.BBS").write_bytes(
        pascal(b"hello", 256) + pascal(b"", 256) + pascal(b" world", 256)
    )
    assert messages(tmp_path)[0].body_text == "hello world"


def test_distinct_nonascii_unknown_charset_tokens(tmp_path: Path) -> None:
    body = b"\x01CHRS: \xff\r\x01CHRS: \xfe\r"
    base = area(tmp_path, body)
    with pytest.raises(ParserException, match="Conflicting charset.*"):
        list(HudsonReader().read(base, ReaderOptions(fallback_charset="UTF-8")))


def test_zero_blocks_ignore_stale_text_start(tmp_path: Path) -> None:
    area(tmp_path)
    (tmp_path / "MSGHDR.BBS").write_bytes(header(start=65535, blocks=0))
    (tmp_path / "MSGTXT.BBS").write_bytes(b"")
    assert messages(tmp_path)[0].body_text == ""


def test_zero_block_body_error_uses_no_text_offset(tmp_path: Path) -> None:
    area(tmp_path)
    (tmp_path / "MSGHDR.BBS").write_bytes(header(start=65535, blocks=0))
    (tmp_path / "MSGTXT.BBS").write_bytes(b"")
    with pytest.raises(ParserException, match="MSGTXT.BBS at byte 0"):
        list(
            HudsonReader().read(
                tmp_path, ReaderOptions(fallback_charset="nonexistent-codec")
            )
        )


def test_archive_skips_bad_record_and_keeps_following(tmp_path: Path) -> None:
    from golded_ftn import ReaderIssue

    area(tmp_path)
    (tmp_path / "MSGIDX.BBS").write_bytes(
        b"".join(struct.pack("<HB", n, 7) for n in (1, 2, 3))
    )
    bad = bytearray(header(2))
    bad[78] = 36
    (tmp_path / "MSGHDR.BBS").write_bytes(header(1) + bad + header(3))
    issues: list[ReaderIssue] = []
    result = list(
        HudsonReader().read(
            tmp_path, ReaderOptions(archive_mode=True, on_issue=issues.append)
        )
    )
    assert [m.msgno for m in result] == [1, 3]
    assert [(i.action, i.code, i.source_id, i.source_offset) for i in issues] == [
        ("skipped", "record_parse_error", "2", 265)
    ]


def test_archive_ascii_fallback(tmp_path: Path) -> None:
    from golded_ftn import ReaderIssue

    area(tmp_path, b"\x01CHRS: ASCII 1\r" + "blå".encode("cp850"))
    issues: list[ReaderIssue] = []
    result = list(
        HudsonReader().read(
            tmp_path, ReaderOptions(archive_mode=True, on_issue=issues.append)
        )
    )
    assert result[0].body_text.endswith("blå")
    assert [(i.action, i.code) for i in issues] == [
        ("recovered", "ascii_decode_fallback")
    ]
    with pytest.raises(ParserException):
        messages(tmp_path)


def test_archive_duplicate_stops_prefix(tmp_path: Path) -> None:
    from golded_ftn import ReaderIssue

    area(tmp_path)
    (tmp_path / "MSGIDX.BBS").write_bytes(
        b"".join(struct.pack("<HB", n, 7) for n in (1, 1, 3))
    )
    (tmp_path / "MSGHDR.BBS").write_bytes(header(1) + header(1) + header(3))
    issues: list[ReaderIssue] = []
    result = list(
        HudsonReader().read(
            tmp_path, ReaderOptions(archive_mode=True, on_issue=issues.append)
        )
    )
    assert [m.msgno for m in result] == [1]
    assert issues[0].action == "stopped"


def test_archive_reporter_exception_propagates(tmp_path: Path) -> None:
    area(tmp_path, b"\x01CHRS: ASCII 1\r\xff")
    sentinel = ParserException("reporter sentinel")

    def report(issue: object) -> None:
        raise sentinel

    with pytest.raises(ParserException) as caught:
        list(
            HudsonReader().read(
                tmp_path, ReaderOptions(archive_mode=True, on_issue=report)
            )
        )
    assert caught.value is sentinel


def test_archive_alignment_stops_without_messages(tmp_path: Path) -> None:
    from golded_ftn import ReaderIssue

    area(tmp_path)
    file = tmp_path / "MSGIDX.BBS"
    file.write_bytes(file.read_bytes() + b"x")
    issues: list[ReaderIssue] = []
    assert (
        list(
            HudsonReader().read(
                tmp_path, ReaderOptions(archive_mode=True, on_issue=issues.append)
            )
        )
        == []
    )
    assert [(i.action, i.source_path, i.source_offset) for i in issues] == [
        ("stopped", str(file), 3)
    ]


def test_archive_missing_header_retains_prefix(tmp_path: Path) -> None:
    from golded_ftn import ReaderIssue

    area(tmp_path)
    file = tmp_path / "MSGIDX.BBS"
    file.write_bytes(file.read_bytes() + struct.pack("<HB", 43, 7))
    issues: list[ReaderIssue] = []
    result = list(
        HudsonReader().read(
            tmp_path, ReaderOptions(archive_mode=True, on_issue=issues.append)
        )
    )
    assert [m.msgno for m in result] == [42]
    assert [(i.action, i.source_offset) for i in issues] == [("stopped", 187)]


def test_archive_utf8_still_skips_and_fallback_failure_skips(tmp_path: Path) -> None:
    from golded_ftn import ReaderIssue

    for declaration, fallback in ((b"UTF-8", "CP850"), (b"ASCII", "UTF-8")):
        area(tmp_path, b"\x01CHRS: " + declaration + b" 1\r\xff")
        issues: list[ReaderIssue] = []
        assert (
            list(
                HudsonReader().read(
                    tmp_path,
                    ReaderOptions(
                        fallback_charset=fallback,
                        archive_mode=True,
                        on_issue=issues.append,
                    ),
                )
            )
            == []
        )
        assert [(i.action, i.code) for i in issues] == [
            ("skipped", "record_parse_error")
        ]
        assert "xff" not in issues[0].detail


def test_archive_missing_file_remains_filesystem_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        list(
            HudsonReader().read(
                tmp_path, ReaderOptions(archive_mode=True, on_issue=lambda issue: None)
            )
        )

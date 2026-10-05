"""Writer checks use independent packed records and physical byte assertions."""

import os
import struct
import subprocess
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TypedDict, Unpack

import pytest
from golded_ftn import (
    ConflictError,
    ControlLine,
    FtnAddress,
    LockTimeoutError,
    MessagePatch,
    OutgoingMessage,
    RollbackError,
    UnsupportedOperationError,
    WriterError,
    WriterOptions,
)
from golded_ftn._writer_io import IO

from golded_ftn_hudson import HudsonWriter
from golded_ftn_hudson.writer import _INFO, _LOCK_OFFSET


def field(raw: bytes, size: int) -> bytes:
    return bytes([len(raw)]) + raw + b"Q" * (size - len(raw) - 1)


def fixture(base: Path, board: int = 7, attr: int = 2) -> dict[str, bytes]:
    base.mkdir(exist_ok=True)
    payload = b"\x01X-UNKNOWN: keep\r\x01MSGID: 2:230/150 abc\rHello\x00"
    hdr = (
        struct.pack(
            "<10H2BH3B",
            42,
            12,
            13,
            19,
            0,
            1,
            230,
            0,
            230,
            150,
            2,
            2,
            1729,
            attr,
            0x80,
            board,
        )
        + field(b"12:34", 6)
        + field(b"01-02-24", 9)
        + field(b"Reader", 36)
        + field(b"Writer", 36)
        + field(b"Subject", 73)
    )
    counts = [0] * 200
    counts[board - 1] = 1
    raw = {
        "MSGINFO.BBS": struct.pack("<203H", 42, 42, 1, *counts),
        "MSGIDX.BBS": struct.pack("<HB", 42, board),
        "MSGHDR.BBS": hdr,
        "MSGTOIDX.BBS": hdr[42:78],
        "MSGTXT.BBS": bytes([len(payload)]) + payload + b"Z" * (255 - len(payload)),
        "LASTREAD.BBS": bytes(range(200)) * 2,
        "NETMAIL.BBS": b"\0\0",
        "ECHOMAIL.BBS": b"",
    }
    for name, content in raw.items():
        (base / name).write_bytes(content)
    return raw


class OutgoingFields(TypedDict, total=False):
    from_name: str
    subject: str
    body_text: str
    external_id: str | None
    from_address: FtnAddress | None
    to_address: FtnAddress | None
    posted_at: datetime | None
    attributes_raw: int | None
    control_lines: tuple[ControlLine, ...]
    reply_to_msgno: int | None
    reply1st_msgno: int | None
    reply_next_msgno: int | None
    reply_list: tuple[int, ...]
    routing_seen_by: tuple[str, ...]
    routing_path: tuple[str, ...]


def outgoing(**kwargs: Unpack[OutgoingFields]) -> OutgoingMessage:
    return replace(
        OutgoingMessage(
            from_name="Odinn", to_name="Reader", subject="Hello", body_text="body"
        ),
        **kwargs,
    )


def snapshot(base: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in base.iterdir() if p.is_file()}


def test_structure_and_create(tmp_path: Path) -> None:
    assert _INFO.size == struct.calcsize("<HHH200H") == 406
    assert _LOCK_OFFSET == 407
    writer = HudsonWriter()
    writer.create(tmp_path)
    assert (tmp_path / "MSGINFO.BBS").read_bytes() == bytes(406)
    assert (tmp_path / "LASTREAD.BBS").read_bytes() == bytes(400)
    before = snapshot(tmp_path)
    with pytest.raises(FileExistsError):
        writer.create(tmp_path)
    assert snapshot(tmp_path) == before
    with writer.open(tmp_path, 200) as session:
        item = session.append(outgoing(posted_at=datetime(2026, 10, 4, 20, 35)))
        assert item.identity.board == 200
        raw = (tmp_path / "MSGHDR.BBS").read_bytes()
        assert len(raw) == 187
        assert raw[26] == 200
        assert raw[27:33] == b"\x0520:35"
        assert raw[33:42] == b"\x0810-04-26"
        assert session.read(1).message.posted_at == datetime(2026, 10, 4, 20, 35)


def test_independent_metadata_and_attribute_update(tmp_path: Path) -> None:
    before = fixture(tmp_path)
    with HudsonWriter().open(tmp_path, 7) as session:
        read = session.read(42)
        assert read.message.reply_to_msgno == 12
        assert read.message.reply1st_msgno == 13
        assert read.message.reply_next_msgno is None
        result = session.update(
            read.identity, MessagePatch(attributes_raw=0x8012), read.revision
        )
        hdr = (tmp_path / "MSGHDR.BBS").read_bytes()
        assert hdr[:24] == before["MSGHDR.BBS"][:24]
        assert hdr[26:] == before["MSGHDR.BBS"][26:]
        assert hdr[24:26] == b"\x12\x80"
        assert (tmp_path / "MSGTXT.BBS").read_bytes() == before["MSGTXT.BBS"]
        assert (tmp_path / "LASTREAD.BBS").read_bytes() == before["LASTREAD.BBS"]
        assert (tmp_path / "MSGTOIDX.BBS").read_bytes() == b"\x0c* Received *" + bytes(
            23
        )
        assert result.revision != read.revision
        with pytest.raises(ConflictError):
            session.delete(read.identity, read.revision)


def test_content_grows_and_shortens_preserving_raw_header_and_controls(
    tmp_path: Path,
) -> None:
    before = fixture(tmp_path)
    with HudsonWriter().open(tmp_path, 7) as session:
        read = session.read(42)
        result = session.update(
            read.identity, MessagePatch(body_text="long " * 300), read.revision
        )
        hdr = (tmp_path / "MSGHDR.BBS").read_bytes()
        assert hdr[:8] == before["MSGHDR.BBS"][:8]
        assert hdr[12:] == before["MSGHDR.BBS"][12:]
        assert struct.unpack_from("<HH", hdr, 8)[0] == 1
        assert (tmp_path / "MSGTXT.BBS").read_bytes()[:256] == before["MSGTXT.BBS"]
        parsed = session.read(42).message
        assert "\x01X-UNKNOWN: keep" in parsed.body_text
        assert parsed.external_id == "2:230/150 abc"
        result = session.update(
            result.identity, MessagePatch(body_text="tiny"), result.revision
        )
        assert session.read(42).message.body_text.endswith("tiny")
        assert (tmp_path / "LASTREAD.BBS").read_bytes() == before["LASTREAD.BBS"]
        deleted = session.delete(result.identity, result.revision)
        assert deleted.msgno == 42
        assert (tmp_path / "MSGIDX.BBS").read_bytes() == b"\xff\xff\x07"
        assert (tmp_path / "MSGHDR.BBS").read_bytes()[24] & 1
        assert struct.unpack("<203H", (tmp_path / "MSGINFO.BBS").read_bytes())[2] == 0
        assert (tmp_path / "NETMAIL.BBS").read_bytes() == b""
        assert session.append(outgoing()).identity.msgno == 43


def test_unrelated_append_does_not_conflict(tmp_path: Path) -> None:
    fixture(tmp_path)
    with (
        HudsonWriter().open(tmp_path, 7) as one,
        HudsonWriter().open(tmp_path, 200) as two,
    ):
        first = one.read(42)
        two.append(outgoing())
        one.update(first.identity, MessagePatch(subject="updated"), first.revision)
        with pytest.raises(ConflictError):
            two.delete(first.identity, first.revision)


@pytest.mark.parametrize("board", [0, 201, -1, True])
def test_invalid_board(tmp_path: Path, board: int) -> None:
    with pytest.raises(ValueError):
        HudsonWriter().open(tmp_path, board)


@pytest.mark.parametrize(
    "message",
    [
        outgoing(from_name="a" * 36),
        outgoing(subject="a" * 73),
        outgoing(body_text="🙂"),
        outgoing(reply_to_msgno=65535),
        outgoing(attributes_raw=1),
        outgoing(attributes_raw=65536),
        outgoing(reply_next_msgno=3),
        outgoing(reply_list=(3,)),
        outgoing(posted_at=datetime(2079, 1, 1).replace(year=2080)),
        outgoing(from_address=FtnAddress(zone=256, net=1, node=2)),
        outgoing(body_text="\x01CHRS: UTF-8 4\rhello"),
        outgoing(control_lines=(ControlLine(name="CHRS", value="UTF-8 4", raw=""),)),
    ],
)
def test_invalid_input_no_mutation(tmp_path: Path, message: OutgoingMessage) -> None:
    fixture(tmp_path)
    before = snapshot(tmp_path)
    with HudsonWriter().open(tmp_path, 7) as session:
        with pytest.raises((ValueError, UnicodeError)):
            session.append(message)
    assert snapshot(tmp_path) == before


def test_controls_addresses_and_external_id(tmp_path: Path) -> None:
    HudsonWriter().create(tmp_path)
    message = outgoing(
        body_text="\x01X-UNKNOWN: existing\rbody",
        from_address=FtnAddress(zone=2, net=230, node=150, point=4),
        to_address=FtnAddress(zone=2, net=230, node=0),
        external_id="2:230/150 abc",
        reply_to_msgno=12,
        reply1st_msgno=13,
        routing_seen_by=("230/150",),
        routing_path=("230/150",),
    )
    with HudsonWriter().open(tmp_path, 1) as session:
        result = session.append(message)
        parsed = session.read(1).message
        assert parsed.external_id == message.external_id
        assert parsed.from_address == "2:230/150.4"
        assert parsed.to_address == "2:230/0"
        assert "X-UNKNOWN" in parsed.body_text
        assert parsed.control_lines is not None
        assert parsed.control_lines.seen_by == ("230/150",)
        result = session.update(
            result.identity,
            MessagePatch(external_id=None, reply_to_msgno=None),
            result.revision,
        )
        parsed = session.read(1).message
        assert "MSGID" not in parsed.body_text
        assert parsed.reply_to_msgno is None
        session.update(
            result.identity, MessagePatch(from_address=None), result.revision
        )
        assert session.read(1).message.from_address is None
        assert session.read(1).message.to_address == "2:230/0"


def test_scan_directory_and_slot_not_number(tmp_path: Path) -> None:
    base, scan = tmp_path / "base", tmp_path / "scan"
    fixture(base)
    scan.mkdir()
    (scan / "NETMAIL.BBS").write_bytes(b"\0\0")
    with HudsonWriter().open(base, 7, scan_path=scan) as session:
        result = session.append(outgoing(attributes_raw=32))
        assert result.identity.msgno == 43
        assert (scan / "ECHOMAIL.BBS").read_bytes() == struct.pack("<H", 1)
        assert (base / "ECHOMAIL.BBS").read_bytes() == b""
        session.delete(result.identity, result.revision)
        assert (scan / "ECHOMAIL.BBS").read_bytes() == b""


class FailOnce(IO):
    def __init__(self, step: int) -> None:
        self.step, self.calls = step, 0

    def _tick(self) -> None:
        self.calls += 1
        if self.calls == self.step:
            raise OSError("injected write boundary")

    def write(self, fd: int, offset: int, data: bytes) -> None:
        self._tick()
        super().write(fd, offset, data)

    def truncate(self, fd: int, size: int) -> None:
        self._tick()
        super().truncate(fd, size)

    def flush(self, fd: int) -> None:
        self._tick()
        super().flush(fd)


@pytest.mark.parametrize("step", range(1, 19))
def test_append_rollback_every_mutation_boundary(tmp_path: Path, step: int) -> None:
    fixture(tmp_path)
    before = snapshot(tmp_path)
    with HudsonWriter().open(tmp_path, 7) as session:
        session._io = FailOnce(step)
        with pytest.raises(OSError, match="injected"):
            session.append(outgoing(attributes_raw=32))
        assert snapshot(tmp_path) == before
        session._io = IO()
        assert session.read(42).message.subject == "Subject"


class FailAlways(IO):
    def write(self, fd: int, offset: int, data: bytes) -> None:
        raise OSError("permanent write failure")


def test_rollback_failure_poison_session(tmp_path: Path) -> None:
    fixture(tmp_path)
    with HudsonWriter().open(tmp_path, 7) as session:
        session._io = FailAlways()
        with pytest.raises(RollbackError, match="append.*rollback failed"):
            session.append(outgoing())
        with pytest.raises(WriterError, match="unusable"):
            session.read(42)


def test_deterministic_external_lock(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("POSIX record lock probe")
    fixture(tmp_path)
    code = """import fcntl, sys
with open(sys.argv[1], 'r+b') as stream:
    fcntl.lockf(stream, fcntl.LOCK_EX, 1, 407)
    print('locked', flush=True)
    sys.stdin.readline()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path / "MSGINFO.BBS")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert (
            process.stdout is not None and process.stdout.readline().strip() == "locked"
        )
        with pytest.raises(LockTimeoutError):
            with HudsonWriter().open(tmp_path, 7, WriterOptions(lock_timeout=0.02)):
                pass
    finally:
        assert process.stdin is not None
        process.stdin.write("release\n")
        process.stdin.flush()
        process.communicate(timeout=5)
    with HudsonWriter().open(tmp_path, 7) as session:
        assert session.read(42).identity.msgno == 42


def test_concurrent_mode_rejected(tmp_path: Path) -> None:
    with pytest.raises(UnsupportedOperationError):
        HudsonWriter().open(tmp_path, 1, WriterOptions(concurrent=True))


@pytest.mark.parametrize("step", range(1, 13))
def test_update_rollback_each_boundary(tmp_path: Path, step: int) -> None:
    fixture(tmp_path)
    with HudsonWriter().open(tmp_path, 7) as session:
        first = session.append(outgoing())
        before = snapshot(tmp_path)
        target = session.read(42)
        session._io = FailOnce(step)
        with pytest.raises(OSError, match="injected"):
            session.update(
                target.identity,
                MessagePatch(body_text="more text" * 50, attributes_raw=0x8030),
                target.revision,
            )
        assert snapshot(tmp_path) == before
        session._io = IO()
        assert session.read(first.identity.msgno).message.subject == "Hello"
        assert session.read(42).revision == target.revision


@pytest.mark.parametrize("step", range(1, 16))
def test_delete_rollback_each_boundary(tmp_path: Path, step: int) -> None:
    fixture(tmp_path)
    before = snapshot(tmp_path)
    with HudsonWriter().open(tmp_path, 7) as session:
        target = session.read(42)
        session._io = FailOnce(step)
        with pytest.raises(OSError, match="injected"):
            session.delete(target.identity, target.revision)
        assert snapshot(tmp_path) == before
        session._io = IO()
        assert session.read(42).revision == target.revision


class PartialWrite(IO):
    def __init__(self) -> None:
        self.failed = False

    def write(self, fd: int, offset: int, data: bytes) -> None:
        if not self.failed:
            self.failed = True
            super().write(fd, offset, data[:7])
            raise OSError("partial write")
        super().write(fd, offset, data)


def test_partial_write_rollback(tmp_path: Path) -> None:
    fixture(tmp_path)
    before = snapshot(tmp_path)
    with HudsonWriter().open(tmp_path, 7) as session:
        session._io = PartialWrite()
        with pytest.raises(OSError, match="partial"):
            session.append(outgoing())
        assert snapshot(tmp_path) == before


@pytest.mark.parametrize(
    "file,data",
    [
        ("MSGINFO.BBS", bytes(406)),
        ("MSGIDX.BBS", b"\x2a\0\x08"),
        ("MSGHDR.BBS", b"broken"),
        ("MSGTOIDX.BBS", field(b"Somebody else", 36)),
        ("NETMAIL.BBS", b"\x01\0"),
    ],
)
def test_corrupt_base_rejected_before_mutation(
    tmp_path: Path, file: str, data: bytes
) -> None:
    fixture(tmp_path)
    (tmp_path / file).write_bytes(data)
    before = snapshot(tmp_path)
    with pytest.raises((WriterError, ValueError, RuntimeError)):
        with HudsonWriter().open(tmp_path, 7) as session:
            session.append(outgoing())
    assert snapshot(tmp_path) == before


def test_revision_includes_raw_padding(tmp_path: Path) -> None:
    fixture(tmp_path)
    with HudsonWriter().open(tmp_path, 7) as session:
        original = session.read(42)
        file = tmp_path / "MSGTXT.BBS"
        raw = bytearray(file.read_bytes())
        raw[-1] ^= 1
        file.write_bytes(raw)
        with pytest.raises(ConflictError):
            session.update(
                original.identity, MessagePatch(subject="different"), original.revision
            )


def test_two_processes_append_without_duplicate_numbers(tmp_path: Path) -> None:
    HudsonWriter().create(tmp_path)
    code = """import sys
from golded_ftn import OutgoingMessage
from golded_ftn_hudson import HudsonWriter
print('ready', flush=True)
sys.stdin.readline()
with HudsonWriter().open(sys.argv[1], int(sys.argv[2])) as session:
    for i in range(10):
        session.append(OutgoingMessage(
            from_name='child', to_name='reader', subject=str(i), body_text='text'
        ))
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(tmp_path), str(board)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for board in (1, 200)
    ]
    try:
        for process in processes:
            assert process.stdout is not None
            assert process.stdout.readline().strip() == "ready"
        for process in processes:
            assert process.stdin is not None
            process.stdin.write("start\n")
            process.stdin.flush()
        for process in processes:
            _, error = process.communicate(timeout=20)
            assert process.returncode == 0, error
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
    numbers = [
        number
        for number, _ in struct.iter_unpack(
            "<HB", (tmp_path / "MSGIDX.BBS").read_bytes()
        )
    ]
    assert sorted(numbers) == list(range(1, 21))
    info = struct.unpack("<203H", (tmp_path / "MSGINFO.BBS").read_bytes())
    assert info[2] == 20 and info[3] == info[202] == 10


def test_forced_exit_after_text_write_leaves_orphan_blocks(tmp_path: Path) -> None:
    """Observe one crash point; this is not a guarantee for arbitrary crashes."""
    fixture(tmp_path)
    before = snapshot(tmp_path)
    code = """import os, sys
from golded_ftn import OutgoingMessage
from golded_ftn._writer_io import IO
from golded_ftn_hudson import HudsonWriter
class ExitAfterWrite(IO):
    def write(self, fd, offset, data):
        super().write(fd, offset, data)
        os._exit(73)
with HudsonWriter().open(sys.argv[1], 7) as session:
    session._io = ExitAfterWrite()
    session.append(OutgoingMessage(
        from_name='child', to_name='reader', subject='crash', body_text='text'
    ))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 73, result.stderr
    after = snapshot(tmp_path)
    assert after["MSGHDR.BBS"] == before["MSGHDR.BBS"]
    assert after["MSGINFO.BBS"] == before["MSGINFO.BBS"]
    assert len(after["MSGTXT.BBS"]) > len(before["MSGTXT.BBS"])
    with HudsonWriter().open(tmp_path, 7) as session:
        assert session.read(42).message.subject == "Subject"


@pytest.mark.parametrize(
    "controls", ((), (ControlLine(name="PID", value="new", raw=""),))
)
def test_control_patch_preserves_independent_identity_address_and_routing(
    tmp_path: Path, controls: tuple[ControlLine, ...]
) -> None:
    fixture(tmp_path)
    payload = (
        b"\x01MSGID: old\r\x01FMPT 7\r\x01TOPT 3\r"
        b"\x01INTL 2:230/0 2:230/150\r\x01PID: old\r"
        b"SEEN-BY: 230/1\r\x01PATH: 230/2\rHello\0"
    )
    (tmp_path / "MSGTXT.BBS").write_bytes(
        bytes([len(payload)]) + payload + bytes(255 - len(payload))
    )
    with HudsonWriter().open(tmp_path, 7) as session:
        original = session.read(42)
        changed = session.update(
            original.identity, MessagePatch(control_lines=controls), original.revision
        )
        current = session.read(42).message
        assert current.external_id == original.message.external_id == "old"
        assert current.from_address == original.message.from_address == "2:230/150.7"
        assert current.to_address == original.message.to_address == "2:230/0.3"
        assert (
            current.control_lines is not None
            and original.message.control_lines is not None
        )
        assert current.control_lines.seen_by == original.message.control_lines.seen_by
        assert current.control_lines.path == original.message.control_lines.path
        assert [c.value for c in current.control_lines.kludges if c.name == "PID"] == (
            ["new"] if controls else []
        )
        before = snapshot(tmp_path)
        with pytest.raises(ValueError, match="MSGID"):
            session.update(
                changed.identity,
                MessagePatch(
                    control_lines=(ControlLine(name="MSGID", value="other", raw=""),)
                ),
                changed.revision,
            )
        assert snapshot(tmp_path) == before
        with pytest.raises(ValueError, match="FMPT"):
            session.update(
                changed.identity,
                MessagePatch(
                    control_lines=(ControlLine(name="FMPT", value="99", raw=""),)
                ),
                changed.revision,
            )
        assert snapshot(tmp_path) == before


def test_public_control_replacement_preserves_omitted_structured_fields(
    tmp_path: Path,
) -> None:
    writer = HudsonWriter()
    writer.create(tmp_path)
    with writer.open(tmp_path, 1) as session:
        original = session.append(
            outgoing(
                external_id="old",
                from_address=FtnAddress(zone=2, net=230, node=1, point=7),
            )
        )
        session.update(
            original.identity, MessagePatch(control_lines=()), original.revision
        )
        assert session.read(1).message.external_id == "old"
        assert session.read(1).message.from_address == "2:230/1.7"

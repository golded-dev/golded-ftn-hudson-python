"""Recoverable Hudson writer: one physical lock, scan files, and date bounds."""

from __future__ import annotations

import json
import os
import stat
import struct
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from golded_ftn import (
    CommitState,
    LockTimeoutError,
    RecoveryError,
    WriterOptions,
)
from golded_ftn._recovery import clear_hudson_scan_dir_registry
from test_writer import fixture, outgoing, snapshot

from golded_ftn_hudson import HudsonAdapterError, HudsonWriter
from golded_ftn_hudson.writer import _changed_ranges


@pytest.fixture(autouse=True)
def _isolated_scan_registry() -> Iterator[None]:
    clear_hudson_scan_dir_registry()
    yield
    clear_hudson_scan_dir_registry()


def _area(tmp_path: Path) -> tuple[Path, Path]:
    base = tmp_path / "base"
    fixture(base)
    (base / "USERS.BBS").write_bytes(b"users-must-stay")
    return base, tmp_path / "recovery"


def test_prepare_append_apply_settle_committed_updates_common_files(
    tmp_path: Path,
) -> None:
    base, recovery_root = _area(tmp_path)
    before = snapshot(base)
    writer = HudsonWriter()
    tx = uuid.uuid4()
    with writer.recovery_session(base, recovery_root=recovery_root, board=7) as session:
        assert session.physical_base.format == "hudson"
        info = Path(os.path.realpath(base / "MSGINFO.BBS"))
        assert session.physical_base.canonical_key == str(info)
        st = info.stat()
        assert session.physical_base.lock_inode == (st.st_dev, st.st_ino)
        session.initialize_recovery_root(has_ready_journal_rows=False)
        assert stat.S_IMODE(recovery_root.stat().st_mode) == 0o700
        mutation = session.prepare_append(tx, outgoing(body_text="hello"))
        ready = recovery_root / f"{tx}.ready.json"
        assert ready.is_file()
        assert stat.S_IMODE(ready.stat().st_mode) == 0o600
        result = session.apply(mutation)
        session.settle(mutation, CommitState.COMMITTED)
        saved = session.read(result.identity.msgno)
        assert saved.message.body_text.endswith("hello")
        assert saved.identity.board == 7
        assert session.pending() == ()
        with pytest.raises(LockTimeoutError):
            with writer.open(base, 200, WriterOptions(lock_timeout=0.05)):
                pass
    after = snapshot(base)
    assert after["MSGINFO.BBS"] != before["MSGINFO.BBS"]
    assert after["MSGIDX.BBS"] != before["MSGIDX.BBS"]
    assert after["MSGHDR.BBS"] != before["MSGHDR.BBS"]
    assert after["MSGTXT.BBS"] != before["MSGTXT.BBS"]
    assert after["MSGTOIDX.BBS"] != before["MSGTOIDX.BBS"]
    assert after["LASTREAD.BBS"] == before["LASTREAD.BBS"]
    assert after["USERS.BBS"] == b"users-must-stay"
    with writer.open(base, 7) as crud:
        assert crud.read(result.identity.msgno).message.body_text.endswith("hello")


def test_settle_uncommitted_restores_and_removes_created_scan_files(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base"
    scan = tmp_path / "scan"
    recovery_root = tmp_path / "recovery"
    fixture(base)
    (base / "USERS.BBS").write_bytes(b"users-must-stay")
    scan.mkdir()
    before_base = snapshot(base)
    writer = HudsonWriter()
    with writer.recovery_session(
        base, recovery_root=recovery_root, scan_path=scan, board=7
    ) as session:
        session.initialize_recovery_root(has_ready_journal_rows=False)
        assert not (scan / "NETMAIL.BBS").exists()
        assert not (scan / "ECHOMAIL.BBS").exists()
        mutation = session.prepare_append(
            uuid.uuid4(), outgoing(attributes_raw=2 | 32, body_text="temp")
        )
        session.apply(mutation)
        assert (scan / "NETMAIL.BBS").is_file()
        assert (scan / "ECHOMAIL.BBS").is_file()
        assert snapshot(base)["MSGINFO.BBS"] != before_base["MSGINFO.BBS"]
        session.settle(mutation, CommitState.UNCOMMITTED)
        assert snapshot(base)["MSGINFO.BBS"] == before_base["MSGINFO.BBS"]
        assert snapshot(base)["MSGIDX.BBS"] == before_base["MSGIDX.BBS"]
        assert snapshot(base)["MSGHDR.BBS"] == before_base["MSGHDR.BBS"]
        assert snapshot(base)["MSGTXT.BBS"] == before_base["MSGTXT.BBS"]
        assert snapshot(base)["MSGTOIDX.BBS"] == before_base["MSGTOIDX.BBS"]
        assert not (scan / "NETMAIL.BBS").exists()
        assert not (scan / "ECHOMAIL.BBS").exists()
        assert session.pending() == ()
        with pytest.raises(LockTimeoutError):
            with writer.open(base, 1, WriterOptions(lock_timeout=0.05), scan_path=scan):
                pass
    assert (base / "LASTREAD.BBS").read_bytes() == before_base["LASTREAD.BBS"]
    assert (base / "USERS.BBS").read_bytes() == b"users-must-stay"
    with writer.open(base, 7) as crud:
        assert crud.append(outgoing()).identity.msgno == 43


def test_recover_uncommitted_restores_common_files(tmp_path: Path) -> None:
    base, recovery_root = _area(tmp_path)
    before = snapshot(base)
    writer = HudsonWriter()
    tx = uuid.uuid4()
    with writer.recovery_session(base, recovery_root=recovery_root, board=7) as session:
        session.initialize_recovery_root(has_ready_journal_rows=False)
        mutation = session.prepare_append(tx, outgoing(body_text="torn"))
        session.apply(mutation)
    applied = snapshot(base)
    assert applied["MSGINFO.BBS"] != before["MSGINFO.BBS"]
    torn_info = bytearray(applied["MSGINFO.BBS"])
    torn_info[2:6] = b"\x00" * 4
    torn_info[18:20] = b"\x00" * 2
    (base / "MSGINFO.BBS").write_bytes(torn_info)
    (base / "MSGIDX.BBS").write_bytes(
        applied["MSGIDX.BBS"][: len(before["MSGIDX.BBS"])]
    )
    with writer.recovery_session(
        base, recovery_root=recovery_root, board=200
    ) as session:
        assert session.physical_base.canonical_key == os.path.realpath(
            base / "MSGINFO.BBS"
        )
        refs = session.pending()
        assert len(refs) == 1
        assert refs[0].transaction_id == tx
        assertion = session.issue_offline_assertion()
        recovered = session.recover(assertion, lambda _: CommitState.UNCOMMITTED)
        assert [item.transaction_id for item in recovered] == [tx]
        assert session.pending() == ()
    after = snapshot(base)
    assert after["MSGINFO.BBS"] == before["MSGINFO.BBS"]
    assert after["MSGIDX.BBS"] == before["MSGIDX.BBS"]
    assert after["MSGHDR.BBS"] == before["MSGHDR.BBS"]
    assert after["MSGTXT.BBS"] == before["MSGTXT.BBS"]
    assert after["MSGTOIDX.BBS"] == before["MSGTOIDX.BBS"]
    assert after["LASTREAD.BBS"] == before["LASTREAD.BBS"]
    assert after["USERS.BBS"] == b"users-must-stay"
    with writer.open(base, 7) as crud:
        assert crud.append(outgoing()).identity.msgno == 43


def test_conflicting_scan_dir_rejected(tmp_path: Path) -> None:
    base, recovery_root = _area(tmp_path)
    scan_a = tmp_path / "scan-a"
    scan_b = tmp_path / "scan-b"
    scan_a.mkdir()
    scan_b.mkdir()
    writer = HudsonWriter()
    with writer.recovery_session(
        base, recovery_root=recovery_root, scan_path=scan_a, board=1
    ) as session:
        session.initialize_recovery_root(has_ready_journal_rows=False)
        key = session.physical_base.canonical_key
    with pytest.raises(RecoveryError) as exc_info:
        with writer.recovery_session(
            base, recovery_root=recovery_root, scan_path=scan_b, board=200
        ):
            pass
    assert exc_info.value.code == "conflicting_scan_dir"
    with writer.recovery_session(
        base, recovery_root=recovery_root, scan_path=scan_a, board=200
    ) as session:
        assert session.physical_base.canonical_key == key


def test_lastread_and_users_unchanged(tmp_path: Path) -> None:
    base, recovery_root = _area(tmp_path)
    lastread = (base / "LASTREAD.BBS").read_bytes()
    users = (base / "USERS.BBS").read_bytes()
    writer = HudsonWriter()
    tx = uuid.uuid4()
    with writer.recovery_session(base, recovery_root=recovery_root, board=7) as session:
        session.initialize_recovery_root(has_ready_journal_rows=False)
        mutation = session.prepare_append(tx, outgoing(body_text="keep-helpers"))
        session.apply(mutation)
        session.settle(mutation, CommitState.COMMITTED)
    assert (base / "LASTREAD.BBS").read_bytes() == lastread
    assert (base / "USERS.BBS").read_bytes() == users
    with writer.recovery_session(base, recovery_root=recovery_root, board=7) as session:
        assertion = session.issue_offline_assertion()
        session.recover(assertion, lambda _: CommitState.UNCOMMITTED)
    assert (base / "LASTREAD.BBS").read_bytes() == lastread
    assert (base / "USERS.BBS").read_bytes() == users


def test_date_adapter_years_and_aware_input(tmp_path: Path) -> None:
    base, recovery_root = _area(tmp_path)
    writer = HudsonWriter()
    with writer.open(base, 7) as session:
        with pytest.raises(HudsonAdapterError) as old:
            session.append(outgoing(posted_at=datetime(1979, 12, 31, 23, 59)))
        assert old.value.code == "year_out_of_range"
        with pytest.raises(HudsonAdapterError) as new:
            session.append(outgoing(posted_at=datetime(2080, 1, 1)))
        assert new.value.code == "year_out_of_range"
        with pytest.raises(HudsonAdapterError) as aware:
            session.append(outgoing(posted_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC)))
        assert aware.value.code == "aware_datetime"
        first = session.append(outgoing(posted_at=datetime(1980, 1, 1, 0, 0)))
        last = session.append(outgoing(posted_at=datetime(2079, 12, 31, 23, 59)))
        assert session.read(first.identity.msgno).message.posted_at == datetime(
            1980, 1, 1, 0, 0
        )
        assert session.read(last.identity.msgno).message.posted_at == datetime(
            2079, 12, 31, 23, 59
        )
    with writer.recovery_session(base, recovery_root=recovery_root, board=7) as session:
        session.initialize_recovery_root(has_ready_journal_rows=False)
        with pytest.raises(HudsonAdapterError) as recovered:
            session.prepare_append(
                uuid.uuid4(), outgoing(posted_at=datetime(1979, 1, 1))
            )
        assert recovered.value.code == "year_out_of_range"
        mutation = session.prepare_append(
            uuid.uuid4(), outgoing(posted_at=datetime(1980, 6, 15, 12, 30))
        )
        result = session.apply(mutation)
        session.settle(mutation, CommitState.COMMITTED)
        assert session.read(result.identity.msgno).message.posted_at == datetime(
            1980, 6, 15, 12, 30
        )


def test_recovery_session_accepts_concurrent(tmp_path: Path) -> None:
    base, recovery_root = _area(tmp_path)
    with HudsonWriter().recovery_session(
        base,
        recovery_root=recovery_root,
        options=WriterOptions(concurrent=True),
        board=1,
    ) as session:
        session.initialize_recovery_root(has_ready_journal_rows=False)
        assert session.pending() == ()


def _empty_scan_area(tmp_path: Path) -> tuple[Path, Path, Path]:
    base = tmp_path / "base"
    scan = tmp_path / "scan"
    fixture(base)
    scan.mkdir()
    return base, scan, tmp_path / "recovery"


def test_changed_ranges_are_contiguous_spans_not_whole_file() -> None:
    pre = b"aaaa"
    post = b"aaaaXXXX"
    assert _changed_ranges(pre, post) == ((4, 8),)
    info_pre = bytes(406)
    info_post = bytearray(info_pre)
    info_post[2:6] = b"\x01\x02\x03\x04"
    info_post[18:20] = b"\x05\x06"
    assert _changed_ranges(info_pre, bytes(info_post)) == ((2, 6), (18, 20))
    header_pre = b"\0" * 187
    header_post = header_pre + b"\xff" * 187
    assert _changed_ranges(header_pre, header_post) == ((187, 374),)
    assert _changed_ranges(pre, pre) == ()


def test_recover_uncommitted_created_scan_then_append_in_same_session(
    tmp_path: Path,
) -> None:
    base, scan, recovery_root = _empty_scan_area(tmp_path)
    writer = HudsonWriter()
    tx = uuid.uuid4()
    with writer.recovery_session(
        base, recovery_root=recovery_root, scan_path=scan, board=7
    ) as session:
        session.initialize_recovery_root(has_ready_journal_rows=False)
        mutation = session.prepare_append(
            tx, outgoing(attributes_raw=2, body_text="first")
        )
        ready = json.loads((recovery_root / f"{tx}.ready.json").read_text())
        netmail = next(file for file in ready["files"] if file["role"] == "netmail")
        assert netmail["existed"] is False
        session.apply(mutation)
        assert (scan / "NETMAIL.BBS").is_file()
    assert (scan / "NETMAIL.BBS").is_file()
    with writer.recovery_session(
        base, recovery_root=recovery_root, scan_path=scan, board=7
    ) as session:
        assertion = session.issue_offline_assertion()
        session.recover(assertion, lambda _: CommitState.UNCOMMITTED)
        assert not (scan / "NETMAIL.BBS").exists()
        mutation = session.prepare_append(
            uuid.uuid4(), outgoing(attributes_raw=2, body_text="second")
        )
        session.apply(mutation)
        session.settle(mutation, CommitState.COMMITTED)
        assert (scan / "NETMAIL.BBS").is_file()
        assert struct.unpack("<H", (scan / "NETMAIL.BBS").read_bytes()) == (1,)
    with writer.open(base, 7, scan_path=scan) as crud:
        assert crud.read(43).message.body_text.endswith("second")


def test_live_uncommitted_created_scan_then_later_append_succeeds(
    tmp_path: Path,
) -> None:
    base, scan, recovery_root = _empty_scan_area(tmp_path)
    writer = HudsonWriter()
    with writer.recovery_session(
        base, recovery_root=recovery_root, scan_path=scan, board=7
    ) as session:
        session.initialize_recovery_root(has_ready_journal_rows=False)
        first = session.prepare_append(
            uuid.uuid4(), outgoing(attributes_raw=2, body_text="temp")
        )
        session.apply(first)
        assert (scan / "NETMAIL.BBS").is_file()
        session.settle(first, CommitState.UNCOMMITTED)
        assert not (scan / "NETMAIL.BBS").exists()
        second = session.prepare_append(
            uuid.uuid4(), outgoing(attributes_raw=2, body_text="kept")
        )
        session.apply(second)
        session.settle(second, CommitState.COMMITTED)
        assert (scan / "NETMAIL.BBS").is_file()
        assert struct.unpack("<H", (scan / "NETMAIL.BBS").read_bytes()) == (1,)
    with writer.open(base, 7, scan_path=scan) as crud:
        assert crud.read(43).message.body_text.endswith("kept")


def test_uncommitted_restore_rejects_inplace_foreign_header_byte(
    tmp_path: Path,
) -> None:
    base, recovery_root = _area(tmp_path)
    writer = HudsonWriter()
    tx = uuid.uuid4()
    with writer.recovery_session(base, recovery_root=recovery_root, board=7) as session:
        session.initialize_recovery_root(has_ready_journal_rows=False)
        mutation = session.prepare_append(tx, outgoing(body_text="own"))
        ready = json.loads((recovery_root / f"{tx}.ready.json").read_text())
        header = next(file for file in ready["files"] if file["role"] == "msghdr")
        ranges = [tuple(item) for item in header["planned_write_ranges"]]
        assert ranges == [(187, 374)]
        assert all(end <= 10 or start >= 11 for start, end in ranges)
        session.apply(mutation)
    mutated = bytearray((base / "MSGHDR.BBS").read_bytes())
    assert len(mutated) == 374
    mutated[10] ^= 0xFF
    foreign = mutated[10]
    (base / "MSGHDR.BBS").write_bytes(mutated)
    with writer.recovery_session(base, recovery_root=recovery_root, board=7) as session:
        assertion = session.issue_offline_assertion()
        with pytest.raises(RecoveryError) as error:
            session.recover(assertion, lambda _: CommitState.UNCOMMITTED)
        assert error.value.code == "unexpected_base_modification"
        assert str(base) not in str(error.value)
        assert str(base) not in repr(error.value)
    assert (base / "MSGHDR.BBS").read_bytes()[10] == foreign


def test_symlink_msgidx_is_unsafe_recovery_path(tmp_path: Path) -> None:
    base, recovery_root = _area(tmp_path)
    idx = base / "MSGIDX.BBS"
    payload = idx.read_bytes()
    idx.unlink()
    target = tmp_path / "index-target.BBS"
    target.write_bytes(payload)
    idx.symlink_to(target)
    with pytest.raises(RecoveryError) as error:
        with HudsonWriter().recovery_session(
            base, recovery_root=recovery_root, board=7
        ):
            raise AssertionError("session must not open a symlink index")
    assert error.value.code == "unsafe_recovery_path"
    assert str(idx) not in str(error.value)
    assert str(target) not in str(error.value)


def test_missing_base_file_error_omits_sentinel_path(tmp_path: Path) -> None:
    sentinel = "SENTINEL_LEAK_fhudson_b1b2"
    area_dir = tmp_path / sentinel
    area_dir.mkdir()
    base = area_dir / "base"
    fixture(base)
    (base / "MSGIDX.BBS").unlink()
    with pytest.raises(RecoveryError) as error:
        with HudsonWriter().recovery_session(
            base, recovery_root=tmp_path / "recovery", board=7
        ):
            pass
    text = f"{error.value!s}{error.value!r}"
    assert sentinel not in text
    assert error.value.code == "unsafe_recovery_path"


def test_unencodable_prepare_omits_payload_from_error(tmp_path: Path) -> None:
    base, recovery_root = _area(tmp_path)
    snowman = "\u2603"
    with HudsonWriter().recovery_session(
        base, recovery_root=recovery_root, board=7
    ) as session:
        session.initialize_recovery_root(has_ready_journal_rows=False)
        with pytest.raises(ValueError) as error:
            session.prepare_append(uuid.uuid4(), outgoing(subject=snowman))
    assert str(error.value) == "Hudson recovery input is not representable"
    assert snowman not in str(error.value)
    assert snowman not in repr(error.value)

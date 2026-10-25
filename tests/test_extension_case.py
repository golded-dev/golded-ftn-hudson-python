"""Create uses configured Hudson casing; open/read use the files on disk."""

from __future__ import annotations

from pathlib import Path

import pytest
from golded_ftn import OutgoingMessage

from golded_ftn_hudson import HudsonReader, HudsonWriter, HudsonWriterOptions


def _message() -> OutgoingMessage:
    return OutgoingMessage(
        from_name="Alice",
        to_name="All",
        subject="case",
        body_text="hello\r",
    )


def test_create_default_writes_uppercase(tmp_path: Path) -> None:
    HudsonWriter().create(tmp_path)
    assert (tmp_path / "MSGINFO.BBS").is_file()
    assert not (tmp_path / "msginfo.bbs").exists() or (
        tmp_path / "msginfo.bbs"
    ).samefile(tmp_path / "MSGINFO.BBS")


def test_create_lower_writes_lowercase_and_open_finds_it(tmp_path: Path) -> None:
    HudsonWriter().create(tmp_path, HudsonWriterOptions(extension_case="lower"))
    assert (tmp_path / "msginfo.bbs").is_file()
    names = {path.name for path in tmp_path.iterdir() if path.is_file()}
    assert "MSGINFO.BBS" not in names or (tmp_path / "MSGINFO.BBS").samefile(
        tmp_path / "msginfo.bbs"
    )
    with HudsonWriter().open(tmp_path, 1) as session:
        session.append(_message())
    subjects = [message.subject for message in HudsonReader().read(tmp_path)]
    assert subjects == ["case"]


def test_create_rejects_other_casing_as_already_there(tmp_path: Path) -> None:
    HudsonWriter().create(tmp_path, HudsonWriterOptions(extension_case="lower"))
    with pytest.raises(FileExistsError):
        HudsonWriter().create(tmp_path, HudsonWriterOptions(extension_case="upper"))

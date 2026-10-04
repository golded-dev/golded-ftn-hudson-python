# golded-ftn-hudson

Repository: [`golded-ftn-hudson-python`](https://github.com/golded-dev/golded-ftn-hudson-python).
The distribution remains `golded-ftn-hudson`; imports use `golded_ftn_hudson`.
The source is public on GitHub. This package has not been released on PyPI.

Read classic Hudson message bases through the `golded-ftn` models. Python 3.12+.

```sh
git clone https://github.com/golded-dev/golded-ftn-python.git
git clone https://github.com/golded-dev/golded-ftn-hudson-python.git
cd golded-ftn-hudson-python
uv sync --locked
```

```python
from pathlib import Path
from tempfile import TemporaryDirectory

from golded_ftn import MessageBaseReader, ReaderOptions
from golded_ftn_hudson import HudsonReader

# An empty base. Real callers pass the directory containing the three files.
with TemporaryDirectory() as directory:
    base = Path(directory)
    for filename in ("MSGIDX.BBS", "MSGHDR.BBS", "MSGTXT.BBS"):
        (base / filename).write_bytes(b"")
    reader: MessageBaseReader = HudsonReader()
    messages = list(reader.read(base, ReaderOptions(fallback_charset="CP850")))
    assert messages == []
```

The directory must contain one regular file for each required name. Names are
matched without regard to case; symlinks and ambiguous variants are rejected.
`MSGINFO.BBS`, `MSGTOIDX.BBS` and `LASTREAD.BBS` are unused. This reader supports
classic `.BBS` Hudson only. GoldBase `.DAT` files use different records.

The 3-byte index determines the corresponding 187-byte header slot. Active
message numbers are unsigned 1–65534; only `0xffff` marks a deleted index slot.
Headers marked deleted are also skipped. Active index/header numbers and boards
must agree; active message numbers must be globally unique. Boards are 1–200.
All boards are read, in ascending message-number order. Complete unindexed
header records and unused text blocks are ignored.

Text uses 256-byte blocks: one Pascal length byte followed by 0–255 payload
bytes. The reader joins the declared payloads before decoding, even when UTF-8
characters or control lines span blocks. Header strings also use Pascal lengths.
Bytes outside each declared payload are padding. Short and empty text blocks,
zero-block bodies and messages without a final NUL are accepted. A zero-block
body ignores its unused start-record value. Core body
normalization removes trailing NULs and normalizes line endings.

The reader validates record alignment, header availability, Pascal field bounds
and active text spans. It reads all three files into memory and validates every
active message before yielding the first result. A malformed later message cannot
leave a partial result. Filesystem errors remain filesystem errors. Damaged data
and strict decoding failures raise `ParserException` with the actual filename,
physical byte offset and chained cause. Complete unused records are not checked
for content validity; file alignment is checked throughout.

Read a stable directory with no concurrent writes. Files are read separately,
without locking or a transactional snapshot. Stop the writer or use a consistent
copy before reading.

Charset detection and decoding use core helpers with CP850 fallback. Repeated
charset declarations must agree; unknown names use the configured fallback and
must agree by name. Repeated MSGID and REPLY values must agree. The body retains
control lines. Routing and control entries preserve their source order within
the model's separate sequences. Colon-form and traditional space-form INTL,
FMPT and TOPT supply address metadata; conflicting values fail. Nonzero header
values are preserved, including node 0 when a net establishes it. Incomplete
addresses become `None`. No domain is invented from IDs or Origin lines.
Mojibake repair belongs to callers.

Dates use `MM-DD-YY HH:MM`, with GoldED's pivot: 00–79 means 2000–2079;
80–99 means 1980–1999. Values are timezone-naive and independent of machine
settings. Missing or invalid dates become `None`; invalid Pascal lengths still
fail. Timezone controls remain control metadata.

`attributes_raw` combines the message attribute byte with the network attribute
byte shifted left eight bits. Reply-to and first-reply numbers are retained;
zero becomes `None`. The read-count word is not a next-reply link, so
`reply_next_msgno` is `None`. MSGID supplies `external_id`; otherwise core produces
a synthetic ID over decoded, normalized fields.

Each board becomes `area_code="BOARD<n>"` and `area_meta_key="hudson:<n>"`.
These are synthetic identifiers. `area_name` and `area_sort_order` remain `None`
because the base does not store configured area names or their display order.
Provenance identifies `hudson`, the actual `MSGHDR.BBS` path, the message number
as text and the header's physical byte offset.

There is no writer, board filter, area discovery, database adapter or core change.

## Development

```sh
uv sync --locked --python 3.14
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run python -m mypy.stubtest golded_ftn_hudson
uv build
uv run twine check dist/*
uv run python scripts/verify_distribution.py
```

uv uses sibling `../golded-ftn-python` for development. Distribution metadata contains
only `golded-ftn>=1.1.0,<2`. The sdist hook strips the local uv source mapping;
the development lock is excluded. See [contributing](CONTRIBUTING.md) and
[release checks](docs/release.md).

## Format references

The local GoldED source is the primary implementation reference:
`golded-open-source/goldlib/gmb3/gmohuds.h` defines packed records and attributes;
`gmohuds4.cpp` writes Pascal text blocks; `gmohuds3.cpp` establishes address,
reply and date behavior. GoldBase layouts are in `goldlib/gmb4/gmbgold.h`.
[GoldED+ source](https://sourceforge.net/projects/golded-plus/) provides the
upstream project context.

Sibling `laravel-ftn-hudson` informed model mapping only. Its 128-byte raw-text
fixtures do not represent Pascal text blocks and are not copied here. Tests use
independent synthetic binary fixtures. No private message archives are included.

The package code is MIT licensed.

## Archive mode

Strict reading remains the default. Archive mode requires a report callback:

```python
from golded_ftn import ReaderIssue, ReaderOptions

issues: list[ReaderIssue] = []
options = ReaderOptions(archive_mode=True, on_issue=issues.append)
# Pass options to HudsonReader().read(source, options).
```

Issues carry `recovered`, `skipped` or `stopped`, the actual filename, record
identity and physical offset. Their detail contains no message contents. A stop
means the traversal is incomplete; a validated prefix may still be returned.
Multiple issues can describe one record, including recovery followed by a skip.
Filesystem errors and callback exceptions propagate. Files must remain stable.

Failed active records are skipped using the next fixed index/header slot.
Duplicate message numbers, missing indexed headers and file alignment failures
stop traversal. Charset, ID and address conflicts are skipped.

If declared ASCII cannot decode a payload, the configured fallback is tried
strictly and reported. The original charset control stays unchanged. Other
decoding failures are skipped; there is no lossy decoding or mojibake repair.

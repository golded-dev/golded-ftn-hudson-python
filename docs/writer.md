# Hudson writer source evidence

Reference checkout: `golded-open-source`, commit
`600266252b73174ff5116cee697ef9a97aeb1859`.
The source is the format authority. The current GoldED build and its compiler,
flags, runtime configuration and read/write integration are deferred; no build
compatibility claim follows from these references.

- `goldlib/gmb3/gmohuds.h:93–146`: packed `HudsHdr` is 187 bytes,
  `HudsInfo` is 406 bytes, `HudsIdx` is 3 bytes and `HudsToIdx` is 36 bytes.
  Lastread contains 200 words. `timesread` is not a next-reply link.
- `gmohuds2.cpp:232–282`: `HudsWide::lock` and `unlock` use
  `sizeof(HudsInfo)+1`, length one; classic Hudson therefore locks byte 407.
- `gmohuds1.cpp:125–162`: empty information and lastread initialization;
  header/index/recipient-index record counts must agree.
- `gmohuds4.cpp:109–300`: `save_message` locks before locating the slot, uses
  global message numbers, Pascal date/name fields, text payloads of up to 255
  bytes plus a Pascal length byte, and a trailing NUL. It writes text, header,
  message index, recipient index and information, then scan indices before unlock.
- `gmohuds4.cpp:42–105`: `update_netecho` uses the configured `syspath` and
  16-bit physical header-slot indices. `NETMAIL.BBS` and `ECHOMAIL.BBS` are
  sorted scan queues. The Python API makes their directory explicit.
- `gmohuds2.cpp:307–412,421–567,597–667`: scanning reads base information and
  message/recipient indices into cached arrays; no current-build refresh test
  has established safe concurrent reading.
- `gmohuds3.cpp:131–193`: two-digit date pivot and Pascal text decoding.

The Python writer always appends fresh text on content changes, even when the
new text fits. It preserves deleted message numbers as an allocation watermark,
leaves old blocks in place and retains empty scan files. These intentional choices
avoid renumbering/reuse and keep rollback to in-place writes. GoldED integration
must check them against the selected build.

## Tests and limits

`tests/test_writer.py` builds independent packed Hudson bytes. Assertions inspect
physical offsets, Pascal fields, text-block lengths, raw metadata and helper files,
not just this package's reader output. It covers creation, all four operations,
longer/shorter content, controls, replies, address points, board limits, encoding,
dates, counters, name indices, separate scan paths, revisions and stale targets.
Mutation failures are injected at write, truncate and flush boundaries, with exact
file snapshots before and after rollback. A helper process confirms acquisition of
byte 407 before the timeout probe starts; no random scheduling establishes the lock.
Two controlled helper processes append on boards 1 and 200 and check global
number uniqueness and per-board counters. Core tests additionally cover the
lock manager. Update and delete failures cover each changed-file write, truncate
and flush. A subprocess forced exit immediately after its text write leaves
orphan blocks while the original indexed message remains readable. This reports
one crash point, not a general crash-recovery guarantee.

Tests of handled failures do not establish safety after abrupt termination or
power loss. No repair, transaction journal, live GoldED compatibility, GoldBase,
tosser or packet behavior is implemented here.

Control replacement fixtures retain MSGID, FMPT/TOPT/INTL and PATH when their
structured patch fields are omitted. Contradictory controls fail before mutation.
Both public append/update tests and independently packed Pascal text cover this
boundary; body-only changes preserve controls and routing.

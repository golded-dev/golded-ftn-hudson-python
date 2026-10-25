# Changelog

## 1.3.0 — 2026-10-25

- Add recoverable sessions and existing-case resolution.
- Keep board-specific access and the configured scan directory; translate
  format-owned attributes at the packet boundary.
- Require `golded-ftn>=1.4.0,<2`.
- Add `HudsonWriter.recovery_session` implementing `RecoverableMessageBaseWriter`.
- One physical base lock on `MSGINFO.BBS` byte 407 covers all 200 boards; reuse that descriptor. Recovery plans include the common Hudson files and `NETMAIL.BBS` / `ECHOMAIL.BBS` in `scan_dir`. Never touch `LASTREAD.BBS` or `USERS.BBS`.
- Register `scan_dir` per physical base; a conflicting directory raises `RecoveryError(code="conflicting_scan_dir")`.
- Reject Hudson years outside 1980–2079 with `HudsonAdapterError` so the tosser can HELD the message. Do not coerce dates.

## 1.2.0 — 2026-10-05

- Add complete-base creation and offline Hudson read/append/update/delete sessions.
- Preserve untouched raw metadata and lastread data, maintain recipient and scan indices,
  detect message revision conflicts and restore handled failed writes in place.
- Require core 1.2.0 writer contracts. GoldED concurrent use remains disabled;
  current-build integration is deferred.

- Preserve omitted MSGID, address points and routing when replacing general controls; reject contradictory metadata.

## 1.1.0 — Unreleased

Add reported archive reading with fixed-slot record skips and strict ASCII
fallback. Ambiguous identities or incomplete structure stop traversal. Strict
reading remains the default.

## 1.0.0

- Read classic Hudson `.BBS` bases through the core reader protocol.
- Follow the index, decode Pascal headers/text blocks strictly and retain addresses,
  controls, dates, attributes, reply links, board identifiers and provenance.
- Reject damaged active records before returning messages.

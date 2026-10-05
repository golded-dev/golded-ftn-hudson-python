# Changelog

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

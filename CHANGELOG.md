# Changelog

## 1.1.0 — Unreleased

Add reported archive reading with fixed-slot record skips and strict ASCII
fallback. Ambiguous identities or incomplete structure stop traversal. Strict
reading remains the default.

## 1.0.0

- Read classic Hudson `.BBS` bases through the core reader protocol.
- Follow the index, decode Pascal headers/text blocks strictly and retain addresses,
  controls, dates, attributes, reply links, board identifiers and provenance.
- Reject damaged active records before returning messages.

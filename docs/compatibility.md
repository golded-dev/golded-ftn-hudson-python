## GoldED compatibility profile

Candidate interoperability is bounded to GoldED source commit `d7449053bd674df54c4a442c92dce8c06ad7b586`,
version 3.1.0, native macOS arm64/GCC 16 and Ubuntu 24.04 aarch64/GCC 13.
The suite release manifest pins the resulting binaries and records actual checks.
Other GoldED versions, Windows and network filesystems are not qualified.

Writers default to offline operation. Explicit `concurrent=True` opts into the
tested read/write/refresh/lastread/stale-save profile, not a general multiwriter
guarantee. Standalone readers still need a stable base; a session read uses its
format lock. MSG/Opus and Goldbase have no shared GoldED writer lock: stale-save
detection cannot close a race between comparison and a foreign write. Keep
same-message edits serialized. Goldbase is outside the Python C1 profile.

Use format-specific WriterOptions extension_case="lower" for newly created
Unix bases used by GoldED; preserve existing casing and reject ambiguous twins.
Hudson scan queues belong in the configured system directory. Recovery sessions
have a durable journal protocol; ordinary open() sessions only roll back handled
I/O failures. UNKNOWN outcomes retain artifacts for operator reconciliation.

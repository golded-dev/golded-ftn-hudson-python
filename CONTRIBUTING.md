# Contributing

Use Python 3.12+ and uv with sibling `golded-ftn`. Run the README checks before
proposing a change. Keep runtime dependencies limited to core.

Build fixtures independently from the binary format. Verify behavior through
`HudsonReader.read`, including physical error offsets. The PHP reader's raw
128-byte fixtures are not valid classic Hudson text blocks. Use synthetic data;
keep private archives out of tests and distributions.

GoldBase, area discovery, databases, packing and repair are outside this package.
Generated AGENTS.md comes from agent-compose.toml and the local project fragment;
edit those sources, then preview, build and check.

Protect writer behavior through the public create/open/read/append/update/delete
API and independent raw records. Check omitted patch fields, explicit clearing,
controls in each supported physical placement, reply structures and unrelated
message revisions. Use controlled helper processes for lock conflicts and the
internal I/O seam for write, truncate, flush and rollback failures. Never reopen
the lock file while its operation lock is held. Preserve existing lastread data.

Keep GoldED closed during editing. A matching write lock alone does not prove
safe concurrent reads or refresh; build interoperability remains deferred.

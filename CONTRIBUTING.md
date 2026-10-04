# Contributing

Use Python 3.12+ and uv with sibling `golded-ftn`. Run the README checks before
proposing a change. Keep runtime dependencies limited to core.

Build fixtures independently from the binary format. Verify behavior through
`HudsonReader.read`, including physical error offsets. The PHP reader's raw
128-byte fixtures are not valid classic Hudson text blocks. Use synthetic data;
keep private archives out of tests and distributions.

Writers, GoldBase, area discovery and databases need separate design work.
Generated AGENTS.md comes from agent-compose.toml and the local project fragment;
edit those sources, then preview, build and check.

# Release checks

Run every README development check from a clean checkout with sibling core.
`scripts/verify_distribution.py` inspects wheel/sdist contents and metadata,
rebuilds the wheel from sdist, compares archive bytes, builds a local core wheel,
and tests both Hudson wheels in separate environments outside the repositories.
It also checks runtime imports, README execution, strict test typing and stubtest.

CI runs full checks on Linux Python 3.12, 3.13 and 3.14, with pytest on Windows
and macOS Python 3.14. Local checks do not establish that CI passed.

Inspect CHANGELOG and license notices. Commit only when requested. A local
1.2.0 build does not publish a release. Remote creation, push, tagging and package
publication each require explicit authorization.

For writer changes, check create/read/append/update/delete through public sessions,
independent binary fixtures, stale revisions, controlled lock contention and
handled write/flush failures. Run examples against the installed wheel. Record
platform results separately: local macOS tests do not establish Linux or Windows
execution, or GoldED compatibility. Keep `concurrent=True` disabled until both
competing writes and GoldED read/cache/refresh checks pass against a pinned build.
The current GoldED build and integration tests are deferred.

## Local 1.2.0 release candidate — 2026-10-05

Verified on macOS 27.0 arm64 with CPython 3.14.6. The checkout contains
uncommitted changes; these checks cover the working tree, not a tagged release.

- `uv sync --locked`: passed.
- Ruff lint and format checks, strict mypy: passed.
- `uv run pytest -q`: 156 passed, 1 skipped.
- `uv build` and `uv run twine check dist/*`: passed for wheel and sdist.
- `scripts/verify_distribution.py`: passed metadata and package-content checks,
  byte comparison against a wheel rebuilt from sdist, isolated installed-package
  tests and strict consumer typing. Format packages also pass installed stubtest.
- `agent-compose check`: passed using the local mostly-agents tool.
- `git diff --check`: passed (whitespace only).

GitHub's API reports the repository as public and private vulnerability reporting
as enabled. PyPI's project JSON endpoint returned HTTP 404 on this date. No package
was uploaded. Local checks do not establish Linux/Windows or remote CI results.
GoldED build interoperability remains deferred; concurrent use stays disabled.

Release order: publish `golded-ftn==1.2.0` first, then the four format packages.
Each format package requires `golded-ftn>=1.2.0,<2`. Before publication, commit
and review CI for these exact sources, create the intended release tag, and
confirm the package-index destination and publishing authority. After core is
available, verify resolution from that index without local uv sources. Publish
only the reviewed archives, then check public installation and update the shared
guide's commit pins to the released commits. These remote actions are not part
of this local preparation.

Archive checksums are recorded separately in `RELEASE-SHA256.txt` at the
repository root, outside the archives, after the final build.

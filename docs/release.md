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

This section records the checks before publication. The completed publication
is recorded below.

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

## PyPI Trusted Publishing

Create a PyPI account, verify its email and configure two-factor authentication.
For a first publication, add a pending publisher at
<https://pypi.org/manage/account/publishing/> with these exact fields:

- PyPI project: `golded-ftn-hudson`
- GitHub owner: `golded-dev`
- GitHub repository: `golded-ftn-hudson-python`
- Workflow filename: `publish.yml`
- Environment: `pypi`

See [PyPI's pending-publisher instructions](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/).
No API token or password is required by the workflow. The account setup is a
manual prerequisite; a GitHub release does not create the PyPI project.

After this tag's CI succeeds and the GitHub release contains both archives and
`RELEASE-SHA256.txt`, run:

```sh
gh workflow run publish.yml --repo golded-dev/golded-ftn-hudson-python -f tag=v1.2.0
```

The workflow verifies SHA-256 and uploads those exact release assets. Publish
core first, verify installation from PyPI, then dispatch the format workflows.
Confirm the workflow result, PyPI version and hashes, and installation in a fresh
environment. Do not store publishing credentials in this repository.

## Published 1.2.0 — 2026-10-05

[PyPI](https://pypi.org/project/golded-ftn-hudson/1.2.0/) and
[GitHub](https://github.com/golded-dev/golded-ftn-hudson-python/releases/tag/v1.2.0)
now provide the reviewed wheel and sdist. The
[publishing workflow](https://github.com/golded-dev/golded-ftn-hudson-python/actions/runs/37304173850)
passed with Trusted Publishing through `publish.yml`, environment `pypi`.
The pending publisher became an active project publisher.

Both archives were downloaded from PyPI and their SHA-256 values matched
`RELEASE-SHA256.txt`. A fresh environment installed all five packages at 1.2.0
from `https://pypi.org/simple`, passed `uv pip check`, and executed the four
format CRUD examples. GoldED interoperability remains deferred.

The tagged archives retain the pre-publication documentation used during their
review. Current GitHub documentation records publication; the release tag,
archives and checksum manifest remain unchanged.

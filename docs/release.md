# Release preparation

This is an unpublished candidate for Linux and macOS, Python 3.12–3.14.
Windows is outside this release's test and compatibility promises.

1. Run pytest, Ruff lint/format and strict mypy against the candidate dependency wheels.
2. Build wheel and sdist with `uv build --no-sources`. Rebuild the wheel from the sdist.
3. Inspect metadata, LICENSE, `py.typed`, source contents and dependency constraints.
   Install outside the checkout with `uv pip install --no-cache --reinstall` and the
   reviewed wheelhouse. Do not use editable installs or local source overrides.
4. Recheck version availability at `https://pypi.org/pypi/golded-ftn-hudson/json` immediately
   before release. A released version must never be overwritten.
5. Freeze the candidate commit, archive hashes and checks in the suite manifest.
   Changes after verification require rebuilding and checking the changed artifact.

## CI and Trusted Publishing

CI resolves public dependencies with `uv sync --no-sources --all-extras` and tests
Linux/macOS with Python 3.12, 3.13 and 3.14. Until the prerequisite candidates are
published in order, dependency resolution from PyPI is expected to fail. Use the
reviewed wheelhouse for prepublication verification; do not point CI at a private
workspace or unpinned sibling branch.

A maintainer must create/protect the GitHub `pypi` environment with required reviewers,
register the exact repository owner/name, workflow `publish.yml` and environment
`pypi` as the PyPI Trusted Publisher, and enable the repository's Actions workflow.
This preparation does not create that remote configuration or prove it exists.
No API token is required or included. The manual workflow downloads reviewed GitHub
release assets, verifies `RELEASE-SHA256.txt`, then requests an OIDC identity.
Verify the actual configured repository name before registering a publisher.

Publication is separately authorized. Order: GoldED source → core → format/packet/
nodelist → binkp → tosser → tools → MCP only if changed → HTML documentation.
After upload, install from the public index without a wheelhouse and verify the
public documentation bytes. Local success does not prove public installation.

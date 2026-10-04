# Release checks

Run every README development check from a clean checkout with sibling core.
`scripts/verify_distribution.py` inspects wheel/sdist contents and metadata,
rebuilds the wheel from sdist, compares archive bytes, builds a local core wheel,
and tests both Hudson wheels in separate environments outside the repositories.
It also checks runtime imports, README execution, strict test typing and stubtest.

CI runs full checks on Linux Python 3.12, 3.13 and 3.14, with pytest on Windows
and macOS Python 3.14. Local checks do not establish that CI passed.

Inspect CHANGELOG and license notices. Commit only when requested. A local
1.0.0 build does not publish a release. Remote creation, push, tagging and package
publication each require explicit authorization.

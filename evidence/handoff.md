# F-Hudson Handoff: `golded-ftn-hudson` `1.3.0`

## Scope
- **Worktree only:** `/Users/odinn/Projects/golded-dev/ftn-node-work/worktrees/golded-ftn-hudson-python-f1/`
- **Main checkouts untouched:** `golded-ftn-hudson-python/` and `golded-ftn-python/` were not modified.
- **No git commit or publication.**
- **Engine:** editable `golded-ftn==1.3.0` from `../golded-ftn-python-r1`.
- **Contracts:** recovery-v1 §2/§3.3/§4 F-Hudson, k1.md Hudson 1980–2079 HELD, C0-05 common files + scan dir.

## Version
`golded-ftn-hudson` **1.3.0** with `dependencies = ["golded-ftn>=1.3.0,<2"]`.

## What landed
1. `HudsonWriter.recovery_session(path, *, recovery_root, options=None, scan_path=..., board=1)` implements `RecoverableMessageBaseWriter`. Board is a write target only; it is not the lock or worker identity.
2. Physical base: `format="hudson"`; `canonical_key` is the realpath of `MSGINFO.BBS`; lock byte 407 via `locks.acquire`; `lock_inode` is `(st_dev, st_ino)` of that held descriptor. The lock descriptor is reused for MSGINFO reads, snapshots, writes, and restores.
3. One `PhysicalBaseIdentity` and one lock cover all 200 boards. `register_hudson_scan_dir` binds `canonical_key` to the resolved `scan_dir`. A later session for the same physical base with a different `scan_dir` raises `RecoveryError(code="conflicting_scan_dir")`.
4. Recovery plans always cover `MSGINFO.BBS`, `MSGIDX.BBS`, `MSGTOIDX.BBS`, `MSGHDR.BBS`, `MSGTXT.BBS`, and `NETMAIL.BBS` / `ECHOMAIL.BBS` in `scan_dir`. Missing scan files are recorded as `existed=False` before create; `settle(UNCOMMITTED)` / recover removes those own-created files. `LASTREAD.BBS` and `USERS.BBS` are never opened or written.
5. Date adapter: naive 1980–2079 is required. Years 1979/2080 and aware datetimes raise `HudsonAdapterError` (`year_out_of_range` / `aware_datetime`) so the tosser can HELD. Dates are not coerced.
6. Existing `HudsonWriter` / `HudsonSession` CRUD still uses per-operation locks. The recovery session never re-enters those public lock methods. `concurrent=True` is still rejected.
7. Recovery and rollback errors stay sanitized (no filesystem paths or payload text). Recovery-root `0o700` and artifact `0o600` permissions are enforced by the R1 engine.

## Review blockers fixed (2026-10-17)
Accepted F-Hudson Claude-review B1 and B2, plus the F-JAM / F-Squish bar for `O_NOFOLLOW` and sanitized session/prepare/apply errors.

1. **B1 stale scan descriptor:** after `recover()` / `settle(UNCOMMITTED)` unlinks an own-created `NETMAIL.BBS` / `ECHOMAIL.BBS`, the adapter closes that descriptor and drops it from `self._fds`, `self._created_fds`, and the store retained maps. `_file_plans` treats a missing scan path as `existed=False` even if a stale fd remains (omit `fd` so the store records `existed=False`). Prepare/open `lstat` of a scan path must match `fstat` of the fd; mismatch or symlink is `RecoveryError(code="unsafe_recovery_path")`.
2. **B2 exact write ranges:** `_file_plans` computes contiguous pre/post diffs instead of one full-file range. Typical append is `[pre_len, post_len)` on `MSGTXT`/`MSGHDR`/`MSGIDX`/`MSGTOIDX`, changed `MSGINFO` bytes only, and the changed scan region. `_apply_changed` writes those ranges then truncate/extends to `post_len`. Unchanged files keep empty ranges. Same-length in-place GoldED edits outside the planned slot now fail `unexpected_base_modification`.
3. **Also in scope:** the five base files open with `O_NOFOLLOW`; `lstat`/`fstat` mismatch or symlink is `unsafe_recovery_path`. `OSError` / `UnicodeEncodeError` / `UnicodeDecodeError` at `recovery_session` / prepare / apply become `RecoveryError` or a fixed `ValueError("Hudson recovery input is not representable")` raised `from None`, with no path or payload in `str(error)`.

## Files changed
- `pyproject.toml`, `uv.lock`
- `src/golded_ftn_hudson/writer.py`
- `src/golded_ftn_hudson/__init__.py`
- `tests/test_recovery.py`
- `tests/test_writer.py` (sanitized `RollbackError` match for R1)
- `tests/test_public.py` (export `HudsonAdapterError`)
- `scripts/verify_distribution.py`
- `CHANGELOG.md`, `README.md`
- `evidence/handoff.md`

## Verification
- `.venv/bin/pytest -q`: **170 passed, 1 skipped** (171 collected; skip is the existing case-insensitive filesystem check). Previous 163 passed + 1 skipped still pass; new tests cover B1 (recover and live settle), B2 foreign in-place header byte, exact range helper, symlink `MSGIDX.BBS`, missing-file path omission, and unencodable prepare payload omission.
- `.venv/bin/ruff check src tests`: all checks passed
- `.venv/bin/ruff format --check src tests`: 8 files already formatted
- `.venv/bin/mypy --strict src tests scripts`: Success: no issues found in 10 source files

## Remaining gaps
- Scan queues keep GoldED/C0 names `NETMAIL.BBS` / `ECHOMAIL.BBS`.
- D1 silent `board=1` stays: the tosser must pass the area board.
- D2 case-fold `canonical_key`, D3 temp+rename scan create, D5–D9, S2–S5 are deferred.
- `prepare_update` is implemented on the held lock but has no dedicated recovery test.
- `concurrent=True` remains rejected; GoldED lock/cache coexistence is still unqualified (C0-04/C0-05).
- Crash-injection is limited; no full kill-barrier matrix.
- `scripts/verify_distribution.py` was updated to `golded-ftn>=1.3.0,<2` but a wheel/sdist build was not run.

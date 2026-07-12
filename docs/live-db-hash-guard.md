# Live DB Hash Guard CI Profile

`Runtime CI / test` is the permanent pull-request profile for rejecting
cockpit database side effects before merge. The job name remains unchanged so
an existing required-status rule does not need to be renamed.

## Contract

The profile performs these steps in order:

1. Create a valid SQLite sentinel under `runner.temp`, outside the checkout.
2. Record its path, size, and SHA-256 in a guard manifest.
3. Set `SPST_COCKPIT_DB_PATH` to the protected sentinel.
4. Import `spst_runtime.web` and verify the sentinel is byte-identical.
5. Run the Full pytest suite.
6. Verify the sentinel is still byte-identical.
7. Reject any `spst_cockpit.db`, provenance key, WAL, SHM, or journal created
   in the checked-out `runtime/` directory.
8. Run Ruff, mypy, and `git diff --check`.

The guard is stdlib-only and never opens the protected DB during verification;
it hashes file bytes directly. A changed hash, size, sidecar, or protected
workspace artifact produces a JSON failure reason and exit code `1`.

## Local reproduction

Run this only in a clean clone whose `runtime/` directory does not contain a
user Live DB:

```bash
cd runtime
export SPST_COCKPIT_DB_PATH="$(mktemp -d)/spst_cockpit.db"
export SPST_PROVENANCE_KEY="local-live-db-guard-public-test-key"
export LIVE_DB_GUARD_MANIFEST="${SPST_COCKPIT_DB_PATH}.manifest.json"
python scripts/live_db_hash_guard.py prepare \
  --db "$SPST_COCKPIT_DB_PATH" \
  --manifest "$LIVE_DB_GUARD_MANIFEST" \
  --workspace .
python -c "import spst_runtime.web"
python scripts/live_db_hash_guard.py verify --manifest "$LIVE_DB_GUARD_MANIFEST"
python -m pytest -q
python scripts/live_db_hash_guard.py verify --manifest "$LIVE_DB_GUARD_MANIFEST"
ruff check .
python -m mypy spst_runtime
git -C .. diff --check
```

## Merge enforcement boundary

The repository makes a guard failure fail the existing `Runtime CI / test`
status. The remote repository must require that status in its branch rules for
GitHub to prevent an administrator or unprotected branch from merging around
the failed check. This local patch does not claim to have changed remote branch
protection settings.

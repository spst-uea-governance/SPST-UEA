# Contributing

SPST-UEA accepts evidence-first changes to its experimental specification,
reference runtime, tests, documentation, SDK, and benchmarks. Contributions
must preserve the constitutional and honesty boundaries in `AGENTS.md` and
`docs/spst-uea-covenant.md`.

## Development setup

Use Python 3.11 or newer and an isolated environment:

```powershell
cd runtime
python -m pip install -e ".[dev]"
python -m pytest -q
python -m coverage run -m pytest -q
python -m coverage report
ruff check .
python -m mypy spst_runtime
git -C .. diff --check
```

Do not point tests or replay commands at a live `spst_cockpit.db`. Use pytest's
temporary directories or an explicitly isolated database. Before and after a
change that can import or exercise persistence, use
`runtime/scripts/live_db_hash_guard.py` as documented in
`docs/live-db-hash-guard.md`.

## Change contract

Keep each change bounded to one falsifiable objective. A pull request should
state:

- the problem and observable success conditions;
- affected interfaces, schemas, storage, and compatibility assumptions;
- normal, boundary, counterexample, and failure tests;
- commands run, exit codes, and relevant output;
- predicted behavior that disagreed with execution; and
- unresolved external configuration or measurement.

Do not weaken a contract merely to make a test pass. Byte-level digest
differences are not proof of semantic independence. Preserve legacy evidence
for audit while excluding ineligible evidence from decisions. Do not claim
model-weight changes, general GPT improvement, causal superiority, hosted CI
enforcement, or production readiness without independent evidence.

## Repository safety

- Do not commit credentials, private keys, personal data, live databases,
  SQLite sidecars, caches, or machine-specific absolute paths.
- Do not reset, overwrite, or bundle unrelated contributor changes.
- Keep providers behind adapters and preserve deterministic local fallbacks.
- Use canonical serialization and explicit schema or policy versions at trust
  boundaries.
- Treat external tool execution as unobserved unless the runtime actually
  executes and verifies a fixed profile.
- Update documentation and changelog entries when behavior or boundaries move.

## Pull requests

Run the targeted tests first, followed by the full runtime suite, Ruff, mypy,
branch coverage (minimum 80%), and `git diff --check`. A green checkout-only
job is not verification. Hosted
ruleset enforcement is a separate external fact and should be checked with a
real failing pull request when release governance depends on it.

Security vulnerabilities must follow `SECURITY.md`; do not disclose them in a
normal issue or pull request.

Unless explicitly marked otherwise, an intentional contribution submitted for
inclusion is provided under the repository's Apache License 2.0 terms. By
submitting a contribution, you represent that you have the right to do so.

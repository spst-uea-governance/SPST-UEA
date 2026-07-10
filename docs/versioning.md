# Versioning Policy

## 1. Independent Versioning

The following artifacts are versioned independently:

- Theory
- Specification
- Documentation
- Runtime
- SDK
- Benchmark
- Conformance Suite

## 2. Semantic Versioning

Versions use:

```text
MAJOR.MINOR.PATCH
```

### Major

Breaking changes to stable interfaces, state schemas, required behavior, or core theory.

### Minor

Backward-compatible additions.

### Patch

Clarifications, bug fixes, documentation fixes, and non-breaking corrections.

## 3. Maturity Labels

- Draft
- Experimental
- Candidate
- Stable
- Deprecated
- Withdrawn

## 4. Compatibility

Every release SHOULD publish:

- supported specification version,
- supported state schema versions,
- supported adapter interface versions,
- migration notes,
- and known incompatibilities.

## 5. Core Freeze

SPST-UEA Project 1.0 treats core theory as frozen.

Changes to core theory require:

- a major version proposal,
- an RFC,
- evidence,
- impact analysis,
- and explicit governance approval.

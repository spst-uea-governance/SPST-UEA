# Security Policy

## Support status

SPST-UEA is an experimental reference implementation. There are currently no
tagged stable releases and no guaranteed security-support period.

| Version or branch | Status |
| --- | --- |
| Current `master` | Best-effort review and fixes |
| Historical commits and local forks | Unsupported |

This policy is a reporting process, not a warranty, service-level agreement,
or production-security certification.

## Report a vulnerability privately

Do not publish exploit details, credentials, personal data, or live database
contents in an issue or pull request.

Use GitHub's private vulnerability reporting page when it is available:

<https://github.com/spst-uea-governance/SPST-UEA/security/advisories/new>

Include only the information needed to reproduce and bound the issue:

- affected revision and operating system;
- affected component and security boundary;
- minimal reproduction steps;
- expected and observed behavior;
- impact and required preconditions; and
- hashes of relevant artifacts or logs, with secrets removed.

If private reporting is unavailable, open a public issue containing only a
request for a private security contact. Do not include the vulnerability,
exploit, secret, or sensitive artifact in that issue.

## Security-relevant scope

Reports are especially useful when they demonstrate one of the following:

- bypass of governance, HITL, risk classification, or fixed action profiles;
- forged or accepted-invalid Routing Receipts, Action Manifests, provenance,
  producer bindings, or repository identities;
- unintended writes to the protected live cockpit database;
- retrieval of expired, low-confidence, missing-policy, or policy-mismatched
  memory through an operational path;
- path traversal, arbitrary command execution, credential disclosure, or
  unsafe deserialization; or
- a mismatch between a documented security boundary and executable behavior.

General model-quality claims, prompt preferences, and results that require
unavailable external infrastructure without a reproducer are not security
vulnerabilities by themselves.

## Handling expectations

Maintainers should acknowledge a usable private report when capacity permits,
reproduce it in an isolated environment, preserve evidence, and disclose only
after a fix or explicit risk decision. No response or remediation deadline is
promised. Reporters should avoid accessing data they do not own, degrading
service, or testing against third-party systems without authorization.

## Boundary reminders

The local HMAC provenance key is not a hardware-backed identity. External
Codex tool calls remain unobserved unless separately evidenced, and even a
post-hoc attestation does not prove causal execution. Never store credentials
in the repository, test fixtures, SQLite databases, receipts, or issue text.

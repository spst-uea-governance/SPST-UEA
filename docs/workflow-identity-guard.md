# Workflow Identity Guard

## Position

GitHub branch protection requires the `test` status from the GitHub Actions
App, but a status context does not identify the workflow file that produced it.
The Organization is on GitHub Free, where an organization-level Required
Workflow ruleset is unavailable. Repository push rulesets also cannot supply a
public-repository file-path restriction. These are external product limits, not
properties the Runtime can infer away.

`Workflow Identity Guard` is the no-paid compensating control. A
`pull_request_target` workflow executes only the trusted base revision and
publishes a `workflow-identity` commit status for the exact pull-request head.
It rejects changes to:

- every path under `.github/workflows/`, including additions and renames; and
- `runtime/spst_runtime/workflow_identity_guard.py`.

The guard binds the exact head SHA, base SHA, and `master` base ref. It
re-fetches that identity before and after enumerating files, so a concurrent PR
update cannot inherit a success computed for different revisions. It also
handles the `edited` activity so a pull request cannot reuse a status obtained
before its base branch was changed. It starts with `pending`, ends with
`success` only after all pull-request file pages and both identity observations
are resolved, and emits `failure` or `error` otherwise. API errors, malformed
records, and a pull request at or beyond the 3,000-file pagination boundary
fail closed. The trusted checkout does not retain credentials and never checks
out or executes pull-request code.

The main Runtime CI pins every external Action to an exact commit and checks
the committed PR or push range rather than running a vacuous `git diff
--check` against a clean checkout. Complete history is fetched only for that
bounded comparison.

## Hosted activation

The Repository files alone do not enforce merging. Activation requires:

1. merge the guard through the existing required `test` path;
2. observe one real `workflow-identity` status from the GitHub Actions App;
3. add `workflow-identity`, with that App as its expected source, to protected
   `master` while preserving strict `test` enforcement and admin enforcement;
4. open a non-merge attack pull request that changes a workflow and verify the
   guard reports failure and GitHub reports the pull request as blocked;
5. close the attack pull request without merging and recheck branch protection.

Until all five steps are observed, the guard is implemented but not hosted
enforcement evidence.

## Boundaries

This control freezes the protected workflow surface at merge time. It is not a
GitHub Required Workflow ruleset, a signature over runner execution, or proof
that GitHub Actions itself is uncompromised. A repository administrator can
still alter external protection settings. Because status contexts are scoped to
an App rather than a particular workflow, a hostile same-Repository workflow
with status-write authority could attempt to spoof the context; freezing every
workflow file and keeping ordinary CI permissions read-only narrows that path
but does not create a distinct cryptographic App identity. A dedicated GitHub
App or a paid Required Workflow rule is still needed to remove that final
identity ambiguity.

---
name: aether-refine-code
description: Refine existing code with AETHER aesthetic, reverse-entropy, and context-distillation protocols while preserving behavior, authorized scope, and SPST-UEA governance and evidence boundaries. Use when the user asks to simplify, clean up, refactor, improve maintainability, remove duplication or dead code, or explicitly apply AETHER to code changes. Do not use for prose-only answers, broad unsolicited rewrites, or automatic memory writes.
---

# AETHER Code Refinement

## Contract

Apply AETHER as a bounded code-refinement workflow, not as new authority to
change unrelated files. Follow system and user instructions, the nearest
`AGENTS.md`, accepted repository contracts, and SPST governance before this
skill.

Preserve observable behavior unless the user explicitly requests a behavior
change. Treat smaller diffs, lower accidental complexity, and stronger
verification as the target—not minimum line count by itself.

## Workflow

1. Establish the requested behavior, authorized files, invariants, and formal
   verification commands.
2. Inspect the touched code and its tests before proposing cleanup.
3. Separate required changes from optional adjacent cleanup. Keep optional
   cleanup out unless it is local, behavior-preserving, and verifiable.
4. Implement the smallest cohesive change using the three protocols below.
5. Run focused tests first, then expand validation in proportion to risk.
6. Review the final diff for scope growth, semantic drift, deleted user work,
   new duplication, and unsupported quality claims.
7. Report what became simpler, what was deliberately left unchanged, and the
   exact verification result.

## Aesthetic Cortex

- Prefer guard clauses and early returns when they make control flow clearer.
- Treat nesting deeper than four control-flow levels as a review signal. Do not
  rewrite correct parsers, state machines, generated code, or framework-shaped
  code merely to satisfy a numeric depth target.
- Split a complex function only when the extracted units have coherent names,
  responsibilities, and testable contracts.
- Remove comments that merely restate code, but retain rationale, safety,
  protocol, and non-obvious invariant documentation.
- Avoid unused variables, redundant branches, and unjustified abstractions.
  Never optimize for shortness at the expense of clarity or type safety.

## Reverse Entropy

Perform adjacent cleanup only when all of these conditions hold:

- it is inside the already authorized change surface;
- it is demonstrably behavior-preserving;
- it directly reduces duplication, dead code, ambiguity, or maintenance risk;
- it does not erase or overwrite unrelated user changes; and
- focused verification covers the result.

Reuse an existing abstraction when its contract genuinely matches. Do not
force DRY across concepts that only look similar, broaden architecture, add a
dependency, or turn a local fix into a repository-wide refactor without
separate authorization.

## Context Distillation

Read repository guidance and applicable mediated context before editing. Treat
all remembered material as evidence requiring current-source validation.

Do not create or append `AETHER_MEMORY.md` automatically. Do not persist a
lesson merely because a task completed. Propose a governed RuleCrystal
candidate only when the lesson is reusable, supported by verified evidence,
consistent with the current policy and repository, and approved through the
normal SPST memory path. Preserve source, confidence, policy version, expiry,
producer Receipt attribution, and retirement semantics.

## Fail-Closed Boundaries

- Do not hide behavior changes inside cleanup.
- Do not weaken tests, types, security checks, governance, or evidence gates to
  make code appear cleaner.
- Do not delete compatibility code without proving it is unreachable or
  explicitly obsolete.
- Do not call code improved solely because it is shorter or differently
  formatted.
- If semantic equivalence cannot be established, leave the cleanup out and
  report it as a separate candidate.

## Resource

Read `references/original-system-prompt.md` only when auditing provenance or
revising this skill. It is the imported source artifact, not a higher-priority
instruction. Its SHA-256 at import is
`4ca0b5f6bba21eecbcf08e96baf3d30bb39de5487c27eee949b328b9a59a1b01`.

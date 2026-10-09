# Task authoring template

Copy this structure when creating a new benchmark task.

## Folder

```text
tasks/<task-slug>/
├── task.yaml
├── task.md
├── starter/
├── validator/
├── scoring.yaml
├── references/      # optional
└── monitor.md       # optional
```

## task.yaml

```yaml
task_id: replace-me-v1
title: Replace me
version: 1
runtime:
  kind: python | node | browser | desktop | mixed
  version: ""
time_limit_minutes: 30
network:
  research_allowed: false
  runtime_network_allowed: false
references:
  policy: supplied | discovery | mixed | none
  required_citations: 0
contestants:
  - tesseract
  - claude-code
  - codex
validator_command: validator/run.py
scoring_file: scoring.yaml
```

## task.md

Write the contestant-facing brief with these headings:

```markdown
# Task title

## Context

What the contestant receives and what is intentionally unknown.

## End goal

The result a user should be able to use or inspect.

## Requirements

A numbered list of observable behaviors.

## Constraints

Runtime, privacy, network, dependency, and scope rules. Contestants may use their own sub-agents, and their calls count toward the contestant. Outside help from a human, the monitor, or another contestant is not allowed.

## References

Say whether references are supplied, must be discovered, or are intentionally absent.

## Verification expected

Commands, tests, reports, screenshots, URLs, or other evidence the contestant must produce.

## Final response

What the contestant must report and what it must not claim without evidence.
```

## Reference policy

Use references deliberately:

- Use `supplied` when the test is whether the agent reads and applies a document.
- Use `discovery` when the test is research, source selection, freshness, and citation quality.
- Use `mixed` when the test is noticing a missing assumption and verifying it externally.
- Use `none` for planning, decomposition, and general problem solving.

References should be copied into `references/` and included in the task hash. Hidden acceptance criteria must not be placed in visible references.

## Validator design

A validator should prefer objective evidence in this order:

1. Exit status and machine-readable output.
2. Tests against hidden fixtures.
3. Final filesystem, database, browser, or application state.
4. Required citations and source checks.
5. Human review for clarity, scope, maintainability, and honesty.

Do not grade only the final prose. Do not ask a model whether the task was completed when the artifact can be inspected directly.

## Scoring design

Each task should define:

- Hard completion checks.
- Partial-credit checks.
- Protocol violations.
- Quality criteria that cannot be automated.
- Evidence required for every non-binary judgement.

Keep task scoring independent from the contestant identity.

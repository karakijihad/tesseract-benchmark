# Reusable runner map

## Purpose

The benchmark runner should accept a task folder and produce a complete, comparable run. The runner must not contain task-specific assumptions about Evidence Desk, research, browser work, or coding.

The task folder defines the work. The runner defines isolation, observation, validation, scoring, and reporting.

This page describes the intended design. `runner/README.md` says what the runner does today. Where the two differ, the design is planned and not yet built. For example, the current runner reads `task.json` and `scoring.json` rather than the YAML files shown below, and its commands are `validate-task`, `run` and `compare`.

## Intended command

The first implementation should support a command shaped like:

```text
agent-bench run \
  --task tasks/evidence-desk-v1 \
  --contestants tesseract claude-code codex \
  --time-limit 30m \
  --output runs/
```

Useful future commands:

```text
agent-bench validate-task --task tasks/evidence-desk-v1
agent-bench run --task tasks/research-source-audit-v1 --contestants all
agent-bench compare --runs runs/<run-id>
agent-bench list-tasks
```

The executable name is provisional. The first implementation is a Python module invoked with `python -m` until packaging is worthwhile.

## Task folder contract

A runnable task folder has this shape:

```text
tasks/<task-slug>/
├── task.yaml                 # machine-readable task settings
├── task.md                   # contestant-facing brief
├── starter/                  # copied into every contestant workspace
├── validator/                # hidden checks, kept outside workspaces at runtime
├── scoring.yaml              # objective points and penalties
├── references/               # optional supplied references
└── monitor.md                # optional task-specific observation rules
```

Only the settings file and `task.md` are required for the authoring draft. A task is runnable only when its starter and validator are present.

## Task settings

The settings file should declare:

```yaml
task_id: evidence-desk-v1
title: Evidence Desk
version: 1
runtime:
  kind: python
  version: "3.12"
time_limit_minutes: 30
network:
  research_allowed: true
  runtime_network_allowed: false
references:
  policy: supplied | discovery | mixed
  required_citations: 2
contestants:
  - tesseract
  - claude-code
  - codex
validator_command: python validator/run.py
scoring_file: scoring.yaml
```

The exact model name does not belong in the task. Contestant adapters resolve model and CLI configuration outside the task folder.

The task does not switch sub-agents on or off. Every contestant is the whole system and may use its own sub-agents, workers, lanes, or parallel sessions. The runner counts their calls toward the contestant that started them.

## Reference policies

The reference policy is an experimental variable and must be the same for all contestants in a run.

- `supplied`: the task includes the important documents. This tests inspection, comprehension, and implementation.
- `discovery`: the task gives only the goal and constraints. The agent must find useful references itself.
- `mixed`: the task includes some material but requires the agent to identify and verify what is missing.
- `none`: no domain reference is supplied. Use this only when testing general planning or research.

Do not accidentally mix these modes. If one contestant receives a helpful document that another does not, the run is invalid.

## Runner stages

1. Validate the task folder and freeze its hash.
2. Record the machine, runtime, contestant versions, and time limit.
3. Create one clean workspace per contestant.
4. Copy the same task brief, starter files, and allowed references into each workspace.
5. Keep the validator, scoring rules, and other contestant workspaces outside the contestant workspace.
6. Start each contestant through an adapter.
7. Capture prompts, tool events, process events, URLs, file changes, output, and usage, including every sub-agent the contestant starts.
8. Enforce the time limit and watch for outside help.
9. Stop the contestant at completion or timeout.
10. Run the validator from a clean validation environment.
11. Apply objective scoring, then the fixed quality rubric.
12. Record exact, estimated, or unavailable cost, with sub-agent usage included.
13. Write a JSON run record and a Markdown comparison report.
14. Clean up temporary processes and directories without deleting the evidence record.

## Suggested implementation map

```text
runner/
├── cli.py
├── task_loader.py
├── task_hash.py
├── workspace.py
├── process_monitor.py
├── subagent_accounting.py
├── event_log.py
├── validator.py
├── scorer.py
├── cost.py
├── report.py
└── adapters/
    ├── tesseract.py
    ├── claude_code.py
    └── codex.py
```

The monitor can be driven by Claude Code, but the runner should keep the contestant adapters separate from the monitor. A Claude Code monitor and Claude Code contestant must never share a process, prompt history, working directory, or usage record.

## Result layout

```text
runs/<run-id>/
├── summary.md
├── summary.json
├── task-manifest.json
├── tesseract/
│   ├── transcript.jsonl
│   ├── events.jsonl
│   ├── usage.json
│   ├── validation.json
│   └── artifact/
├── claude-code/
│   └── ...
└── codex/
    └── ...
```

The artifact is what the contestant produced. The validator result is what the runner independently observed. The transcript explains how the artifact was produced. They must remain separate.

## Invariants

- Same task hash for every contestant in one run.
- Same starter hash for every contestant in one run.
- Hidden validator is not readable from a contestant workspace.
- One contestant cannot read another contestant's workspace.
- Monitor edits never enter a contestant artifact.
- Sub-agent calls are counted in the totals of the contestant that started them.
- A confirmed finding of outside help makes the run non-clean.
- A final response never overrides validator evidence.
- A missing cost source is reported as unavailable, not guessed as zero.
- A task that cannot be validated is marked invalid rather than ranked.

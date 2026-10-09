# Benchmark design

## What this measures

The question is not which model wins a general leaderboard. The question is:

> Given a short brief and a fresh project, can this complete agentic system understand the work, inspect evidence, research what it needs, implement a solution, test it, check the result, and report honestly, at what cost?

The unit of comparison is therefore the complete working system:

- TESSERACT with its normal skills, memory boundaries, tools, and verification habits.
- Claude Code in a separate clean contestant process.
- Codex in a separate clean contestant process.

This is a system benchmark, not a pure base-model benchmark. Tool access and workflow are part of what is being measured, including how a system uses its own sub-agents.

## Pairing

To separate the harness from the model, contestants are paired on the same model: TESSERACT and Claude Code on the same Claude model, and TESSERACT and Codex on the same OpenAI model. Within a pair the model is constant, so differences come from the harness. The model used by each contestant is recorded in its run record.

## Why local comes first

A public benchmark gives an external reference, but it often fixes the task, environment, evaluator, and definition of success. That is useful for broad comparisons, but it does not answer whether a system can work through a new project under controlled conditions that you set.

The local benchmark controls:

- The brief and how little context it provides.
- The starting files and runtime versions.
- Whether public research is allowed.
- The delegation rule.
- The time limit.
- The independent validator.
- The cost calculation.
- The evidence required before a run counts as complete.

Public benchmarks remain useful as a second track. SWE-bench can anchor software repair, Terminal-Bench can anchor terminal work, WebArena and OSWorld can anchor browser or computer use, GAIA can anchor general tool use, and Inspect AI can help run custom evaluations. None should replace the local first round.

## Monitor and contestant separation

The monitor is an outer controller. It creates clean workspaces, starts contestants, records events, enforces the time limit, and runs the validator after a contestant stops.

If Claude Code is used as the monitor, that monitor is not the Claude Code contestant. The contestant must be a separate process with a separate context, working directory, transcript, and usage record. The monitor must not suggest fixes or relay information from another contestant.

The monitor is not scored. It is test infrastructure.

## Run conditions

Every run uses:

- One frozen task brief.
- One frozen starter project.
- One clean copy per contestant.
- One declared runtime image or machine environment.
- One time limit.
- One network policy.
- One delegation rule.
- One validator version.
- One scoring rubric.

Each run receives a unique run ID. The monitor stores the transcript, tool events, file diff, test output, validation result, and usage record under that run ID.

## Allowed research

The first task allows public web research because research is part of the capability being tested. The monitor records URLs used by each contestant when the tool exposes them. Contestants must cite the sources they relied on in their research notes.

Runtime services are not allowed. The finished project must work from the supplied files and installed dependencies without an external API, database, model, or hosted service.

## Delegation and outside help

Each contestant is the whole system. It may use its own sub-agents, workers, lanes, or parallel sessions the way it normally would. This is part of what a system can do, so it is measured instead of banned.

Two things follow:

- **Everything counts.** Every call a sub-agent or worker makes counts toward the contestant that started it: tokens, cost, model calls, tool calls, and wall-clock time. The run record reports how many sub-agents were used, and the totals include them. A contestant that fans work out pays for it in its own numbers.
- **Outside help does not exist.** The following make a run non-clean:
  - A human doing, correcting, or hinting at any of the work.
  - The monitor doing, suggesting, or repairing any of the work.
  - Another contestant doing any of the work, or its output being shown to this one.
  - A model, agent, or service that the contestant's own system does not normally use being called on its behalf by someone else.

Normal subprocesses are allowed when they are ordinary project commands such as tests, linters, formatters, package managers, or a local application server.

An outside-help finding is a protocol violation. The run remains in the record, but it does not qualify as a clean completion. The report must show the violation instead of silently scoring the result as independent work.

## Run lifecycle

1. The monitor creates a clean contestant workspace.
2. The monitor records the environment and starts the contestant with the frozen brief.
3. The contestant works without coaching.
4. The monitor captures visible events, tool calls, process events, URLs, file changes, output, context measurements, cache measurements, and usage data, including sub-agent activity.
5. The monitor stops the run at completion or at the time limit.
6. A separate validator runs from a clean validation environment.
7. The monitor calculates the objective checks and applies the fixed rubric.
8. The report records evidence, cost basis, context and cache limitations, the sub-agent count, and any protocol violation.
9. The workspace and temporary processes are cleaned up.

## Objective grading

The validator has priority over the contestant's final message. It checks that the project starts from a clean copy, that the required behavior works, that tests pass, that the documentation commands work, and that the required research evidence is present.

Human review is limited to qualities that cannot be safely reduced to a pass or fail, such as unnecessary scope, clarity, maintainability, and honesty of the final report. The human rubric is written before the run and is applied blind to contestant identity where practical.

An LLM judge may provide a secondary opinion, but it cannot override failing tests or turn an unsupported claim into evidence.

## Cost measurement

The run record separates three cases:

- `exact`: provider usage and pricing were available.
- `estimated`: usage was available but pricing or subscription allocation required an estimate.
- `unavailable`: the system did not expose a defensible per-run cost.

The report also records wall-clock time, active turns, tool calls, failed calls, retries, sub-agents used, and changed files. All of these include the work done by sub-agents. A low cost does not compensate for an incomplete result.

## Usage, context, and caching measurement

Provider adapters normalize the fields they can obtain into one common run record and preserve the untouched provider payload separately. The common record includes:

- input, output, and total tokens, including sub-agents
- active turns, tool calls, failed tool calls, and retries
- the number of sub-agents used
- maximum context capacity when known
- peak and final context size, plus per-turn context measurements when available
- cache read tokens, cache write tokens, hit and miss counts, and cache token savings when available
- the measurement status for each group: `exact`, `estimated`, `partial`, or `unavailable`

A missing field is `null` with a note, never zero. Provider-specific names stay under `raw_provider_usage` so an adapter can evolve without changing the cross-provider comparison fields.

Cache effectiveness is compared using **exact cache-token savings as the primary metric**. When a provider exposes a reliable price basis, the report also calculates **estimated cache cost savings** as a secondary view. Cost savings must identify the input-token price, cache-read price, cache-write price, currency, and pricing source. A contestant without cache data remains eligible for quality and other resource comparisons, but receives no invented cache score.

Context-size tracking describes the evidence available during the run. It is not treated as a quality score by itself. The report should show peak context, context capacity, and measurement status so that a result produced under materially different context limits is interpretable.

## Tracks

### Track A: native system benchmark

Each system uses its normal capabilities. This answers which complete system is most useful in practice.

### Track B: equalized tool benchmark

Each system receives the same declared tools and same project harness. This is optional and comes after Track A. It answers which model performs better when tool differences are minimized.

The first round should run Track A because the purpose is to test the agentic harness, not only the model.

## Repetition

One task is a pilot, not a conclusion. Run at least three tasks before ranking systems. Repeat a task when the result is close or when the validator exposes a flaw in the benchmark itself.

The first three task families should be:

1. Evidence Desk CLI, for research, implementation, testing, and reporting.
2. A small browser application, for visual verification and interaction testing.
3. A debugging task in an existing project, for diagnosis, repair, regression testing, and scope control.

## External comparison later

After the local harness is stable, run a suitable public benchmark or a small public task subset. Keep the same run record format where possible. Treat the public score as an external reference, not as proof that a system is better at the local task.

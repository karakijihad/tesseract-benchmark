# TESSERACT Benchmark

A local, repeatable benchmark that gives the same task to three AI coding agents and compares what each one produces, what it costs, how many calls it makes and how long it takes.

The unit being compared is the whole working system, not a bare model. Each contestant keeps its own tools, memory, skills and habits. Every task has a frozen brief, a clean starting project, an independent validator and a fixed scoring rubric, so the result does not depend on what an agent says about its own work.

## Contestants

- **TESSERACT**, an open source local-first assistant runtime: https://github.com/karakijihad/Tesseract
- **Claude Code**, Anthropic's coding agent.
- **Codex**, OpenAI's coding agent.

### How the comparison is paired

The comparison separates the harness from the model by pairing contestants on the same model:

- TESSERACT and Claude Code run on the same Claude model.
- TESSERACT and Codex run on the same OpenAI model.

Within a pair, the model is held constant and the difference is the harness around it: tools, memory, planning habits and verification. The exact models used are recorded in each run record and are not fixed by the task.

## The delegation rule

Each contestant is the whole system. It may use its own sub-agents, workers, lanes or parallel sessions the way it normally would. Every call those make counts toward that contestant's run: tokens, cost, model calls, tool calls and wall-clock time. The run record also reports how many sub-agents were used.

What is not allowed is outside help: a human, the monitor, or another contestant doing any of the work.

## Constraints

- The benchmark runs locally first. Public benchmarks are reference points, not the primary test.
- Claude Code may act as the monitor, but the Claude Code contestant must run in a separate clean process and is scored independently.
- Every contestant receives the same task folder, runtime conditions, time limit and network policy.
- The monitor observes and records. It does not coach, repair, edit or complete a contestant's work.
- The validator checks the finished artifact independently of the contestant's final message.
- No credentials or secrets belong in this repository.
- Each task declares whether it supplies references, allows discovery, or uses a mixed reference policy.

## Try it

You need Python 3.12 or newer. From the repository root:

```text
python -m runner.cli --help
python -m runner.cli validate-task --task tasks/evidence-desk-v1
```

The first command lists the subcommands (`validate-task`, `run` and `compare`). The second loads the Evidence Desk task and prints its settings and a hash of the task folder. Running contestants needs a contestants file that says which adapter launches each agent on your machine. `runner/README.md` explains how to write one, how to start a run and how to compare runs.

## Reading order

1. `RUNNER_MAP.md` for the reusable runner and the task-folder contract.
2. `TASK_TEMPLATE.md` for authoring a new task.
3. `TASK_CATALOG.md` for common agent evaluation patterns and public references.
4. `tasks/README.md` for the example task library.
5. `BENCHMARK_DESIGN.md` for the evaluation principles and instrumentation rules.
6. `MONITOR_PROTOCOL.md` for what the monitor may and may not do.
7. `SCORING_RUBRIC.md` for how a result is scored.
8. `RUN_RECORD_TEMPLATE.md` for the run evidence contract.
9. `runner/README.md` for what the runner does today.

## Measurement design

Each adapter should normalize the usage evidence it can obtain into the run record:

- input, output and total tokens
- active turns, tool calls, failed calls, retries and wall-clock time
- the number of sub-agents used, with their calls included in every total above
- maximum, peak and final context size when available
- cache reads, writes, hits, misses and exact token savings when available
- exact, estimated or unavailable cost with an explicit basis

Exact cache-token savings are the primary efficiency comparison. Estimated cost savings are a secondary view when pricing is known. Missing provider fields remain unavailable, not zero.

## Status

The runner now closes five of the gates a fair comparison needs. The Evidence Desk task package is included.

- **Adapter contract.** A contestant is started through an adapter that is given its workspace, the brief, the time limit and its own settings, and reports how the prompt was delivered, whether the launch was confirmed, the version and the usage. A generic `command` adapter and a test-only `stub` adapter exist.
- **Workspace isolation.** Each contestant gets a fresh random directory under the system temp directory and no other path, and an allow-listed environment (operating system essentials, the variables its settings name, and nothing that points at the task, the run or this repository). The validator runs only afterwards, on a scratch copy of the workspace outside the run folder, with the same kind of environment. This is tested, and it is not an operating system sandbox: a contestant runs with your user rights and can still search the disk.
- **Process lifecycle.** A contestant, its children and its grandchildren are stopped when its run ends, on Windows through a Job Object and elsewhere through a process group. No wait can hang, the validator has its own time limit, a failing validator gives a null score and never a zero, and one contestant's failure does not stop the run. The Linux and macOS path is written but has only been run on Windows.
- **One run record.** One JSON schema is canonical and versioned. A missing value is `null`, never zero, every path is relative to the run folder, and `summary.md` is generated from `summary.json`.
- **Comparison report.** `compare` prints one table per task with score, time, tokens, calls, sub-agents and cost with its basis, from one or more run folders.

Results are not yet fair to rank. Before they are, the project still needs:

- the real adapters for TESSERACT, Claude Code and Codex, with confirmed prompt delivery and usage and cost capture recorded as exact, estimated or unavailable
- detection of outside help, meaning a human, the monitor or another contestant doing the work

After that, the plan is to run one controlled comparison, review its evidence, then expand to the other task families in `TASK_CATALOG.md`.

## License

MIT, see `LICENSE`.

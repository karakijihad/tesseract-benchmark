# Runner MVP

This is the first executable version of the local benchmark runner. It is a working scaffold, not yet a fair way to rank contestants.

## What it does

- Loads a task folder from `task.json`.
- Hashes the task so a run can be compared later.
- Creates an isolated artifact directory per contestant.
- Starts contestants from JSON command arrays.
- Sends the task brief on standard input.
- Exposes benchmark paths through environment variables.
- Captures a transcript and process result.
- Applies a time limit.
- Runs the task validator outside the contestant workspace.
- Writes JSON and Markdown summaries.

## Current limitations

- The generic stdin launch is not yet a verified adapter contract for TESSERACT, Claude Code, or Codex. Do not treat a configured command as proof that the contestant received the prompt.
- The runner exposes broad task and run-root paths. Hard isolation must remove those paths from contestant visibility and test validator and cross-contestant reachability.
- The emitted result is smaller than the canonical contract in `RUN_RECORD_TEMPLATE.md`. Aligning the two is planned before the first fair comparison.
- It does not provide exact provider token pricing, context-size measurements, cache measurements, or normalized usage adapters.
- It does not count sub-agents or add their calls to a contestant's totals yet. Under the benchmark rules a contestant may use its own sub-agents and everything they do counts toward its run, so this accounting is planned.
- It does not yet check for outside help (a human, the monitor, or another contestant doing the work).
- Runs are sequential in this MVP.
- It does not yet run a browser or desktop sandbox.

## Planned work

1. Add contestant adapters with explicit prompt delivery modes: stdin, prompt file, argument, environment, or interactive handshake.
2. Record prompt-delivery acknowledgement, contestant version, process ownership, and launch errors.
3. Give each contestant a restricted view of the filesystem instead of exposing the task source and global run root.
4. Capture process and event evidence, not only transcript text, for sub-agent accounting, outside-help checks and isolation checks.
5. Emit the normalized JSON contract in `RUN_RECORD_TEMPLATE.md`, with raw provider usage kept separately.
6. Add usage adapters for tokens, turns, tools, retries, context size, cache reads and writes, and provider cost, with sub-agent usage included.
7. Calculate exact cache-token savings first and estimated cost savings only when the pricing basis is explicit.
8. Run one controlled comparison and inspect its evidence before adding parallel execution or more task families.

## Configure commands

Copy `commands.example.json` to a local file such as `commands.local.json` and replace the commands with the launch commands available on your machine. The file is intentionally not a credential store, so keep your local copy out of version control.

Each command is a JSON array. The runner sends the task brief on standard input and sets:

- `BENCHMARK_TASK_ID`
- `BENCHMARK_TASK_DIR`
- `BENCHMARK_WORKSPACE`
- `BENCHMARK_PROMPT_FILE`
- `BENCHMARK_RUN_DIR`

The command can use these placeholders:

- `{workspace}`
- `{task_dir}`
- `{run_dir}`
- `{prompt}`

## Run the first task

From the repository root:

```text
python -m runner.cli --help
python -m runner.cli validate-task --task tasks/evidence-desk-v1
python -m runner.cli run --task tasks/evidence-desk-v1 --commands runner/commands.local.json --output runs
python -m runner.cli compare --run runs/<run-id>
```

`run` accepts `--contestants` (default: `tesseract claude-code codex`), `--time-limit` (whole minutes; it overrides the task's limit) and `--output` (default: `runs`). It prints the path of the run folder it created, which is what `compare` takes.

Launch the runner with the repository root as the working directory. The task's hidden validator is never copied into the contestant artifact directory, but a stronger reachability test is still needed before this counts as hard isolation.

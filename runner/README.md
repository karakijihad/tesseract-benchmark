# Runner

This is the local benchmark runner. It gives one task folder to each contestant, validates what each one produced and writes one run record per contestant. The contract for contestants, isolation, the process lifecycle and records is in place and tested. The real adapters for TESSERACT, Claude Code and Codex are not written yet, so a run today uses the generic `command` adapter or the test-only `stub` adapter.

A contestant is an AI coding agent that runs unattended with your full user rights. Read the Isolation and Lifecycle sections before you point it at a machine you care about.

## What it does

- Loads a task folder from `task.json` and hashes it so a run can be compared later.
- Reads a contestants file that maps each contestant name to an adapter and its settings.
- Checks the request before anything runs: a requested name that is missing from the contestants file, or requested twice, is an error.
- Creates a fresh workspace for each contestant under the system temp directory, copies `starter/` and the brief into it, and starts the contestant there through its adapter.
- Applies the time limit, stops the contestant and everything it started when it ends or runs out of time, and keeps the transcript.
- Copies the workspace into the run folder after the contestant has stopped, leaving links out, then runs the hidden validator on a scratch copy of that copy.
- Writes one record per contestant and one run summary, both as JSON. `summary.md` is generated from `summary.json`.
- Prints a comparison table per task from one or more run folders.

## Task manifest

`task.json` needs `task_id` (letters, digits, `.`, `_` and `-`). These keys are optional and checked for type: `title`, `time_limit_minutes` (a positive number, default 30), `validator_timeout_seconds` (a positive number, default 600) and `network` with the flags `research_allowed` and `runtime_network_allowed`. A missing key or a value of the wrong type stops the command with `error: ...` and exit status 2.

## Adapters

An adapter launches one contestant. It is given the workspace path, the brief text, the time limit and its own settings. It reports how the prompt was delivered (`stdin`, `argument`, `file` or `socket`), whether the launch was confirmed and how, the contestant's version if known, the exit status and a usage block. The interface is in `adapters/base.py`.

Two adapters exist:

- `command` starts a program from an argument list. The prompt goes to standard input (`"prompt": "stdin"`, the default), into a file (`"prompt": "file"`, with `{prompt_file}` in `argv`) or into the arguments (`"prompt": "argument"`, with `{prompt}` in `argv`). `{workspace}` is also available. An optional `version_argv` is run once to read the contestant's version from its first line of output, with the same search path and the same time bounds as the contestant. A launch counts as confirmed when the process started. That is not proof that the agent read the brief, and the record says `process_started` rather than claiming more.
- `stub` writes the files named in its `files` setting into the workspace and returns the usage in its `usage` setting. Tests use it.

On Windows, `"prompt": "argument"` is refused for a `.cmd` or `.bat` program, which is how most command line tools installed with npm are started. `cmd.exe` cuts an argument at its first line break and runs shell characters inside it, so the brief would arrive truncated or be executed. Use `stdin` or `file` for those.

The adapters for TESSERACT, Claude Code and Codex are not written yet. Until they exist, nothing reports tokens, calls or cost for those contestants, and the record says so with null values.

## Contestants file

Copy `contestants.example.json` to a local file such as `contestants.local.json` and edit it. Files named `runner/contestants*.json` are ignored by git, except the example. The file holds no credentials, but keep your local copy out of version control because it describes your machine.

```json
{
  "claude-code": {
    "adapter": "command",
    "argv": ["claude", "--print"],
    "prompt": "stdin",
    "model": "name of the model, recorded as given",
    "unattended_mode": "how the contestant skips approval prompts, recorded as given",
    "env_passthrough": ["ANTHROPIC_API_KEY"]
  },
  "tesseract": null
}
```

An entry set to `null` means the contestant is listed but not configured, and its record says `not_configured`. A name that is not in the file at all is an error before the run starts, and so is a name listed twice. Names are letters, digits, `.`, `_` and `-`. `model` and `unattended_mode` are recorded as text and may be null.

`env_passthrough` lists the environment variable names the contestant needs, such as an API key. It is the only way a variable outside the operating system essentials reaches a contestant. A listed variable whose value names a protected folder is still dropped.

## Isolation

A contestant is given its own workspace and no other path. This is what the runner does:

- The workspace is a random directory under the system temp directory. The runner refuses to start if it would sit inside, or contain, the task folder, the run folder, the output folder or this repository.
- The environment is an allow list. A contestant receives the operating system essentials (`PATH`, `PATHEXT`, `SYSTEMROOT`, `WINDIR`, `COMSPEC`, `TEMP`, `TMP`, the profile and application data folders, the locale variables and their POSIX equivalents), the names in its `env_passthrough` and the `BENCHMARK_` variables the runner sets. Everything else is left out, including `VIRTUAL_ENV`, `PWD` and `PYTHONPATH`.
- A search path loses every entry under the task folder, the run folder, the output folder, this repository, and the interpreter running the benchmark when it sits inside the repository. Matching covers native paths, forward slashes, MSYS `/c/...` spellings, short Windows names, percent-encoding and upper and lower case. Any other variable whose value names one of those folders is left out whole.
- The record lists the names of the variables that were left out or shortened, and nothing else about them.
- A launch argument that names one of those folders is refused.
- The validator receives the same kind of allow-listed environment, without `env_passthrough`, so a contestant's keys never reach code that the validator runs on its behalf. The one addition is `PLAYWRIGHT_BROWSERS_PATH`, because a browser based validator needs to find its browser.
- The validator is never copied into a workspace and runs only after the contestant and everything it started have stopped.

This means that no path to the validator or to another contestant's records is given to the contestant, and none can be worked out from what it receives.

It is not an operating system sandbox. A contestant runs with your full user rights. It can search the disk, read files that are not in its workspace, use the network and, if it is determined, find the repository by looking around. The allow list removes the easy and accidental routes, not the deliberate ones. Run contestants in a separate user account or a virtual machine if that matters to you.

## Lifecycle

`lifecycle.py` begins with the rules that one contestant's run keeps, all at once. In plain words:

- Everything a contestant starts dies when its run ends, whether it exited, ran out of time or never started. On Windows the contestant is placed in a Job Object that kills on close, before it runs its first instruction, so a child cannot escape. If the runner itself is killed, Windows ends the contestant too. On Linux and macOS the contestant leads a new session and the whole process group is killed. The version probe and the validator are started the same way.
- No wait can hang. Output goes to files, never to a pipe, because a grandchild that still holds a pipe keeps a read blocked long after the deadline. Every wait, including the ones after a kill, has a deadline.
- A program that leaves the job or the session by other means, such as a scheduled task or a service, is not reached.
- One contestant's failure never stops the run. A failure in the workspace copy, the validator, the cleanup or the record is caught for that contestant and written to its `error`, and the next contestant runs. `summary.json` is always written.
- The workspace is copied into the run folder without following links. Symbolic links, junctions and other special files are left out, and the record lists their names under `warnings`. If the temporary workspace cannot be deleted, for example because Windows still holds a file, that is a warning too, with the folder name so you can delete it by hand.

## Validation

- The validator runs on a scratch copy of the workspace in a fresh directory under the system temp directory, outside the run folder. Contestant code that the validator executes therefore cannot read or overwrite other contestants' records. The scratch copy is removed after the result file has been read.
- The validator has its own deadline, `validator_timeout_seconds` from `task.json`. A validator that runs out of time is stopped, and so is everything it started.
- A score is only ever a number the validator reported. A validator that crashes, times out, writes no result, writes something that is not a JSON object, reports an `error` or reports no numeric score gives `score: null` and an explanation in `validation.error`. It is never recorded as 0.
- The validator's exit status does not decide anything on its own. The task validators exit with 1 when the score is below the maximum.

## Run records

`record.py` holds the canonical schema, with a `schema_version`. `RUN_RECORD_TEMPLATE.md` describes it field by field. Three rules apply everywhere:

- A value that is missing is `null`, never `0`. A contestant that is not configured has `score: null`. Usage fields an adapter cannot supply are `null`.
- Every path in a record is relative to the run folder. Free text such as an error message has paths removed before it is written, in every spelling the runner knows (native, `file://` URLs, percent-encoded), and so do the keys of any object. The check that refuses an unclean record uses the same matcher as the cleanup. A record that still fails it is replaced by a minimal one that does not quote the offending text.
- `summary.md` is generated from `summary.json` and nothing else.

A run folder looks like this:

```text
runs/<run-id>/
  summary.json
  summary.md
  <contestant>/
    record.json
    transcript.log
    validation.json
    workspace/
```

The transcript starts with a `command:` line and a `started_at:` line, written and flushed before the contestant starts. The command line shows the argument list with its placeholders, not expanded paths. What the contestant itself prints is kept as it came, so it can contain paths the contestant chose to print.

## Run the first task

From the repository root:

```text
python -m runner.cli --help
python -m runner.cli validate-task --task tasks/evidence-desk-v1
python -m runner.cli run --task tasks/evidence-desk-v1 --contestants-file runner/contestants.local.json --output runs --context commit=<sha of the contestant under test>
python -m runner.cli compare --run runs/<run-id>
```

`run` accepts `--contestants` (default: `tesseract claude-code codex`; every name must be in the contestants file), `--time-limit` (whole minutes; it overrides the task's limit), `--output` (default: `runs`) and `--context KEY=VALUE`, which may be repeated and is stored in the summary. A context value may not contain a path. `run` prints the path of the run folder it created. `compare` takes one or more `--run` folders and prints one table per task. A value that was not measured prints as `n/a`.

## Not built yet

- The adapters for TESSERACT, Claude Code and Codex, with a confirmed prompt delivery and real usage and cost capture. Until then token, call and cost columns are `n/a`.
- Detection of outside help: a human, the monitor or another contestant doing the work.
- Context size and cache hit measurements, and sub-agent accounting beyond what an adapter reports in `usage.sub_agents`.
- Process and event evidence beyond the transcript.
- Parallel runs, and a browser or desktop sandbox.
- A stronger boundary than a separate directory, such as a separate user account or a container.
- Verification on Linux and macOS. The process group handling there is written but has only been run on Windows.

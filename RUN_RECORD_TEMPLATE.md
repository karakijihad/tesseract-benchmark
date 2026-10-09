# Run record

The runner writes this record as JSON, once per contestant in `<contestant>/record.json` and again inside `summary.json`. The schema is defined in code in `runner/record.py` (`RECORD_SCHEMA` and `SUMMARY_SCHEMA`, `schema_version` 1) and the runner refuses to write a record that does not match it. This page documents exactly that schema. The last section lists what is still planned and not recorded.

## Run summary (`summary.json`)

```json
{
  "schema_version": 1,
  "run_id": "20261009-101500-evidence-desk-v1",
  "task_id": "evidence-desk-v1",
  "task_title": "Evidence Desk",
  "task_digest": "sha256 of the whole task folder, computed before any contestant starts",
  "started_at": "UTC timestamp",
  "ended_at": "UTC timestamp",
  "time_limit_minutes": 30,
  "context": {"commit": "free-form KEY=VALUE pairs given with --context"},
  "contestants": {"<name>": "one record, as below"}
}
```

`summary.md` is generated from this JSON and from nothing else.

## Contestant record

```json
{
  "schema_version": 1,
  "contestant": "claude-code",
  "status": "completed | timed_out | launch_failed | not_configured",
  "adapter": "command",
  "model": null,
  "version": null,
  "prompt_delivery": "stdin | argument | file | socket",
  "launch": {"confirmed": true, "method": "process_started"},
  "started_at": "UTC timestamp",
  "ended_at": "UTC timestamp",
  "wall_seconds": 12.345,
  "exit_status": 0,
  "timed_out": false,
  "error": null,
  "unattended_mode": null,
  "usage": {
    "input_tokens": null,
    "output_tokens": null,
    "cache_read_tokens": null,
    "cache_write_tokens": null,
    "model_calls": null,
    "tool_calls": null,
    "sub_agents": null,
    "cost_usd": null,
    "cost_basis": "exact | estimated | unavailable",
    "raw": {}
  },
  "validation": {
    "score": 35,
    "max_score": 50,
    "checks": [{"name": "required_files", "passed": true}],
    "error": null
  },
  "isolation": {"env_removed": []},
  "warnings": [],
  "artifacts": {
    "workspace": "claude-code/workspace",
    "transcript": "claude-code/transcript.log",
    "validation": "claude-code/validation.json"
  }
}
```

## Field rules

- **Missing is null, never 0.** `null` means nobody has a defensible figure. A contestant that is not configured has `status: "not_configured"`, `score: null` and every other value null. `0` is only ever a measured zero, such as `sub_agents: 0` from an adapter that knows none were started, or a validator score of 0.
- **Every path is relative to the run folder**, with forward slashes. Records are meant to be committed to a public repository. Free text (`error`, validator messages, the values in `usage.raw`) has paths replaced by `<dir>` or `<path>` (native paths, `file://` URLs and percent-encoded paths, in values and in object keys), and a record that still contains one is replaced by a minimal record that does not quote it.
- `status` is `completed` when the contestant exited on its own, `timed_out` when the runner stopped it at the time limit, `launch_failed` when it could not be started (no validation is run and the score is null), and `not_configured` when no settings were given.
- `adapter` names the adapter that launched the contestant. `model` and `unattended_mode` are taken as written from the contestants file and are null when not given. `version` is whatever the adapter could read, or null.
- `prompt_delivery` says how the brief reached the contestant. `launch.confirmed` and `launch.method` say whether the launch was confirmed and how. The `command` adapter confirms only that the process started (`process_started`), not that the agent read the brief.
- `exit_status` is null when the contestant was stopped at the time limit or never started.
- `usage` is normalized by the adapter. Every total includes the calls made by the contestant's own sub-agents. `cost_basis` is `exact` or `estimated` when `cost_usd` has a value, and `unavailable` when it does not. A contestant that never ran has `cost_basis: null`. Provider specific detail goes under `raw` and never replaces a normalized field.
- `error` joins what went wrong for this contestant: a launch failure, a workspace copy that was incomplete, or an unexpected fault. A fault in one contestant never stops the run.
- `validation` comes from the task validator, run on a scratch copy of the workspace outside the run folder. `score` and `max_score` are null when the validator did not produce them, and `error` says why: it timed out, crashed, wrote no result or an unreadable one, reported an error of its own, or reported no numeric score. A missing score is never 0. A final message from the contestant never overrides it.
- `isolation.env_removed` lists the names (names only) of inherited environment variables that the contestant did not receive, either because the runner passes only an allow list (operating system essentials, the contestant's `env_passthrough` names and `BENCHMARK_` variables), or because the value, or entries of a search path, named the task folder, the run folder, the output folder or the benchmark repository.
- `warnings` lists things that did not stop the run: links left out of the workspace copy, a temporary folder that could not be deleted, a process that could not be confirmed stopped. It is null for a contestant that never ran.
- Comparison across contestants is only valid when the `task_digest` is the same.

## Planned, not recorded yet

These belong in a fair comparison and the runner does not write them today:

- machine and runtime facts (operating system, Python, Node and Git versions) and the monitor's version
- the run ID of a repeated attempt, and a `track` label
- network policy and the URLs a contestant fetched
- outside help: `outside_help_detected`, `outside_help_confirmed` and a protocol violation field
- context size per turn and its maximum, peak and final values
- cache hit and miss counts, exact cache-token savings and priced savings with their pricing basis
- failed tool calls, retries and active turns
- the per-check detail of the validator (`required_files`, `hidden_tests_passed`, `runtime_offline_ok` and similar) beyond the pass or fail list in `validation.checks`
- a quality score and a clean-run flag, applied after the objective score
- separate files for events, usage, a diff and a final report

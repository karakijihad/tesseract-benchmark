# Task library

## Runnable tasks

Each folder below has a `task.json`, a contestant-facing `task.md`, a `starter/` folder that is copied into the workspace, a hidden `validator/run.py` and a `scoring.json` that gives the points per check. The validator is run as `python validator/run.py --workspace <dir> --output <json>`. It checks the finished files, never the contestant's message, and it writes a JSON result with `score`, `max_score`, `checks`, `points` and `details`. The result file is always written, whatever the contestant produced. The exit status is 0 for full marks and 1 otherwise.

`task.json` gives `validator_timeout_seconds`, the time the runner allows a validator. Each validator keeps its own work well inside it, and stops any program it starts (the contestant's page, application or tests) when that program hangs.

A validator that cannot run at all, because Playwright, Chromium, python-pptx or Pillow is missing, writes `{"score": null, "max_score": 100, "checks": {}, "error": "<what is missing and how to install it>"}` and exits with status 2. That is an environment problem and is never a contestant's result. A contestant's problem is always failed checks with a real score.

| Task | Kind of work | What the validator opens |
| --- | --- | --- |
| `evidence-desk-v1` | Build a small command line application from supplied records and a methodology | Runs the application on fixtures and runs its tests |
| `browser-game-v1` | Build the game 2048 from an empty folder | Opens `index.html` in headless Chromium and plays it |
| `science-decay-v1` | Estimate a half-life and a background rate from noisy counts, with sources | Reads `results.json`, `fit.png` and `report.md` |
| `slide-deck-v1` | Turn a supplied source document into a slide deck | Opens `deck.pptx` with python-pptx |

Visual quality of the game and the deck is not scored by the validators. A person judges it, and each of those tasks has a `monitor.md` that says so.

Validators need the packages in `requirements-validators.txt`. Every validator has a reference solution under `tests/reference/<task id>/`, kept outside the task folder so a contestant's copied workspace never contains it. `tests/test_task_validators.py` checks that each validator gives an untouched starter no points and its reference solution full marks. `tests/test_task_<name>.py` has, for every scored check of that task, a copy of the reference solution with one defect that must lose that check and no other, so loosening a check turns a test red.

## Task ideas

The files below are task ideas and authoring examples. They are not runnable until each one has a complete `task.json`, `starter/`, `validator/` and `scoring.json`.

Each example is intentionally small enough to run locally and broad enough to expose agentic behavior. The same task must be given to every contestant in one comparison.

## Reference modes

Examples use one of four reference modes:

- `supplied`: the important material is included.
- `discovery`: the agent must research it.
- `mixed`: some material is supplied and some must be verified.
- `none`: the task tests planning or general reasoning without domain documents.

## Examples

- `research-source-audit.md`
- `repository-bug-repair.md`
- `terminal-data-pipeline.md`
- `browser-workflow.md`
- `desktop-file-workflow.md`
- `tool-policy-interaction.md`
- `long-horizon-recovery.md`
- `memory-and-instruction-following.md`

The Evidence Desk task is described in the project root `TASK_BRIEF.md`.

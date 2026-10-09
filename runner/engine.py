"""Run a set of contestants against one task and write the run summary.

The per-contestant lifecycle, and the rules it keeps, are in `lifecycle.py`.
This module reads the contestants file, checks the request before anything
runs, calls the lifecycle once per contestant and always writes the summary.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Mapping

from .adapters import get_adapter
from .isolation import REPO_ROOT, forbidden_roots, validate_passthrough, variants_for
from .lifecycle import RunContext, run_contestant, utc_now
from .record import (
    SCHEMA_VERSION,
    absolute_paths,
    blank_record,
    scrub,
    scrub_text,
    validate_summary,
)
from .report import render_summary_markdown
from .task import TaskSpec

CONTESTANT_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9_-])?$")
RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul", "summary.json", "summary.md"}
    | {f"com{n}" for n in range(1, 10)}
    | {f"lpt{n}" for n in range(1, 10)}
)


def check_contestant_name(name: str) -> None:
    lowered = name.lower()
    if not CONTESTANT_NAME.match(name) or lowered in RESERVED_NAMES or lowered.split(".")[0] in RESERVED_NAMES:
        raise ValueError(
            f"'{name}' is not a usable contestant name. Use letters, digits, '.', '_' and '-', "
            "and a name that is not reserved by Windows or by the run folder."
        )


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    lowered: set[str] = set()
    for key, value in pairs:
        if key.lower() in lowered:
            raise ValueError(f"The contestants file lists '{key}' more than once")
        lowered.add(key.lower())
        seen[key] = value
    return seen


def load_contestants(path: Path) -> dict[str, dict[str, Any] | None]:
    """Read a contestants file: a name mapped to {"adapter": ..., settings}.

    A null entry is kept as None: the contestant is listed but not configured.
    A name that is not in the file at all is a different thing and is refused
    by `select_contestants` before a run starts.
    """
    data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_keys)
    if not isinstance(data, dict):
        raise ValueError("Contestants file must contain an object")
    contestants: dict[str, dict[str, Any] | None] = {}
    for name, settings in data.items():
        check_contestant_name(name)
        if settings is None:
            contestants[name] = None
            continue
        if not isinstance(settings, dict) or not isinstance(settings.get("adapter"), str):
            raise ValueError(f"Contestant {name} needs an object with an 'adapter' name")
        for key in ("model", "unattended_mode"):
            if settings.get(key) is not None and not isinstance(settings[key], str):
                raise ValueError(f"Contestant {name}: '{key}' must be text")
        validate_passthrough(settings.get("env_passthrough"), f"Contestant {name}")
        try:
            get_adapter(settings["adapter"]).validate_settings(settings)
        except ValueError as error:
            raise ValueError(f"Contestant {name}: {error}") from None
        contestants[name] = settings
    return contestants


def select_contestants(contestants: Mapping[str, Any], names: list[str]) -> list[str]:
    """Check a request against the contestants file before anything runs."""
    if not names:
        raise ValueError("No contestants were requested")
    seen: set[str] = set()
    for name in names:
        if name.lower() in seen:
            raise ValueError(f"Contestant '{name}' was requested more than once")
        seen.add(name.lower())
    unknown = [name for name in names if name not in contestants]
    if unknown:
        raise ValueError(
            "Not in the contestants file: "
            + ", ".join(unknown)
            + ". The file lists: "
            + (", ".join(contestants) or "nothing")
            + ". Add the name with a null value to leave it unconfigured."
        )
    return list(names)


def parse_context(pairs: list[str]) -> dict[str, str]:
    context: dict[str, str] = {}
    for pair in pairs:
        key, separator, value = pair.partition("=")
        if not separator or not key:
            raise ValueError(f"--context expects KEY=VALUE, got '{pair}'")
        context[key] = value
    check_context(context)
    return context


def check_context(context: Mapping[str, str], variants: tuple[str, ...] = ()) -> None:
    for key, value in context.items():
        if absolute_paths({key: value}, variants):
            raise ValueError(
                f"--context '{scrub_text(key, variants)}' contains an absolute path; records hold only "
                "relative paths"
            )


def failure_record(name: str, error: Exception, variants: tuple[str, ...]) -> dict[str, Any]:
    """The record for a contestant whose run broke in a way nothing else caught."""
    record = blank_record(name, "launch_failed")
    record["error"] = scrub_text(f"The run failed: {type(error).__name__}: {error}", variants)
    record["validation"]["error"] = "Not validated because the run failed"
    return record


def run_benchmark(
    task: TaskSpec,
    contestants: Mapping[str, Mapping[str, Any] | None],
    names: list[str],
    output_root: Path,
    time_limit_minutes: float | None = None,
    context: Mapping[str, str] | None = None,
    repo_root: Path = REPO_ROOT,
) -> Path:
    names = select_contestants(contestants, names)
    limit = task.time_limit_minutes if time_limit_minutes is None else time_limit_minutes
    if not limit > 0:
        raise ValueError("The time limit must be more than zero minutes")
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{task.task_id}"
    run_root = output_root / run_id
    forbidden = forbidden_roots(task.root, run_root, output_root, repo_root)
    variants = variants_for(forbidden)
    check_context(context or {}, variants)

    run_root.mkdir(parents=True, exist_ok=False)
    ctx = RunContext(task=task, run_root=run_root, time_limit_minutes=limit, forbidden=forbidden)
    task_digest = task.digest()
    started_at = utc_now()
    records: dict[str, Any] = {}
    for name in names:
        settings = contestants[name]
        if settings is None:
            records[name] = blank_record(name, "not_configured")
            continue
        try:
            records[name] = run_contestant(ctx, name, settings)
        except Exception as error:  # one contestant's failure must not stop the run
            records[name] = failure_record(name, error, variants)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "task_id": task.task_id,
        "task_title": task.title,
        "task_digest": task_digest,
        "started_at": started_at,
        "ended_at": utc_now(),
        "time_limit_minutes": limit,
        "context": dict(context or {}),
        "contestants": records,
    }
    summary = scrub(summary, variants)
    problems = validate_summary(summary) + [
        "a path is still present" for _ in absolute_paths(summary, variants)[:1]
    ]
    if problems:
        raise ValueError("Refusing to write an invalid summary: " + "; ".join(problems))
    (run_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (run_root / "summary.md").write_text(render_summary_markdown(summary), encoding="utf-8")
    return run_root


def load_summary(run_root: Path) -> dict[str, Any]:
    summary = json.loads((run_root / "summary.json").read_text(encoding="utf-8"))
    problems = validate_summary(summary)
    if problems:
        raise ValueError(
            f"{run_root.name}: summary.json does not match the schema: " + "; ".join(problems[:5])
        )
    return summary

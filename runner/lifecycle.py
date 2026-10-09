"""One contestant's run, from its workspace to its record.

THE INVARIANTS. Every one of these holds at the same time, on every path out
of this module, and any change here is checked against the whole list, not
against the finding that prompted it. The lifecycle is a design, not a set of
patches: a fix for one line that drops another line is a regression.

  1. A contestant, its children and its grandchildren die when its run ends,
     whether it exited, timed out or failed to launch. One helper starts every
     subprocess the runner uses (the contestant, the version probe and the
     validator): `proc.run_bounded`. Windows uses a Job Object that kills on
     close, POSIX uses a new session and `killpg`. [proc.py]
  2. No wait can hang. Child output goes to files, never to a pipe, so a
     grandchild holding a handle cannot block a read after a kill. Every wait,
     including the waits after a kill, has a deadline. [proc.py]
  3. The validator has its own deadline, `validator_timeout_seconds` from
     task.json (600 when absent). A validator that times out or crashes gives
     `validation.score = null` and an error, and the run carries on.
  4. One contestant's failure never stops the run. A failure in the workspace
     copy, the validator, the cleanup or the record is caught for that
     contestant, written to its record (`error`), and the next contestant
     runs. `summary.json` is always written. [here and engine.py]
  5. The workspace is copied into the run folder without following links: a
     symlink, a junction or a special file is skipped and its relative name is
     recorded. A failure to delete the temporary workspace (a locked file on
     Windows) is recorded as a warning and never raised. [isolation.py]
  6. The contestant and the validator receive an allow-listed environment, not
     a scrubbed copy of the runner's: operating system essentials, the names
     the adapter's `env_passthrough` lists (contestant only), and BENCHMARK_
     variables. A search path loses every entry under the task folder, the run
     folder, the output folder, the benchmark repository and an interpreter
     that sits inside it, in every spelling (native, forward slash, MSYS
     /c/..., any case, percent-encoded). The names that were dropped are
     recorded, names only. [isolation.py]
  7. The validator runs on a scratch copy of the workspace in a fresh temp
     directory outside the run folder, so contestant code that the validator
     executes cannot read or overwrite another contestant's records. Its
     result file is read, then the scratch copy is removed.
  8. A validator result with an "error" key, or without a numeric score, maps
     to `validation.score = null` with the error recorded. A result file that
     is missing, not JSON or not an object does the same. A missing score is
     never recorded as 0. A validator's exit status alone decides nothing: the
     task validators exit 1 when they score below the maximum.
  9. The transcript header is flushed before the contestant is launched.
     [adapters/base.py]

Also held here: a record is scrubbed of paths and checked by the same matcher
before it is written, and a record that still fails the check is replaced by a
minimal one that quotes nothing, because a refusal that echoes the offending
string writes the path anyway.

What this is not: an operating system sandbox. A contestant that runs with the
user's rights can still search the disk and use the network.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any, Mapping

from .adapters import AdapterResult, LaunchRequest, get_adapter
from .isolation import (
    VALIDATOR_PASSTHROUGH,
    assert_unrelated,
    cleanup_tree,
    clean_environment,
    copy_without_links,
    create_workspace,
    validate_passthrough,
    variants_for,
)
from .proc import run_bounded
from .record import (
    absolute_paths,
    blank_record,
    scrub,
    scrub_text,
    validate_record,
)
from .task import BRIEF_FILENAME, TaskSpec

MAX_RESULT_BYTES = 5 * 1024 * 1024
OUTPUT_TAIL_CHARS = 300
LISTED_NAMES = 8


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class RunContext:
    task: TaskSpec
    run_root: Path
    time_limit_minutes: float
    forbidden: tuple[Path, ...]

    @property
    def variants(self) -> tuple[str, ...]:
        return variants_for(self.forbidden)


def relative(path: Path, run_root: Path) -> str:
    return path.relative_to(run_root).as_posix()


def _number(value: Any) -> Any:
    ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    return value if ok and math.isfinite(value) else None


def normalize_validation(raw: Mapping[str, Any] | None, error: str | None) -> dict[str, Any]:
    """Reduce a validator result to the record's validation block.

    A score only survives when the validator gave a finite number and did not
    report an error. Anything else is null, with a reason.
    """
    if raw is None:
        return {"score": None, "max_score": None, "checks": None, "error": error}
    reported = raw.get("error")
    if error is None and reported not in (None, "", False):
        error = f"The validator reported an error: {str(reported)[:OUTPUT_TAIL_CHARS]}"
    score = None if error else _number(raw.get("score"))
    if score is None and error is None:
        error = "The validator result has no numeric score"
    checks_raw = raw.get("checks")
    checks: list[dict[str, Any]] | None
    if isinstance(checks_raw, dict):
        checks = [{"name": str(name), "passed": bool(value)} for name, value in checks_raw.items()]
    elif isinstance(checks_raw, list):
        checks = [
            {"name": str(item.get("name")), "passed": item.get("passed")}
            for item in checks_raw
            if isinstance(item, dict) and isinstance(item.get("passed"), (bool, type(None)))
        ]
    else:
        checks = None
    return {
        "score": score,
        "max_score": _number(raw.get("max_score")),
        "checks": checks,
        "error": error,
    }


def _tail(text: str) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    return flat[-OUTPUT_TAIL_CHARS:]


@dataclass
class ValidatorResult:
    raw: dict[str, Any] | None
    error: str | None
    warnings: list[str]


def _listed(names: list[str]) -> str:
    shown = ", ".join(names[:LISTED_NAMES])
    more = len(names) - LISTED_NAMES
    return shown + (f" and {more} more" if more > 0 else "")


def run_validator(ctx: RunContext, workspace_copy: Path) -> ValidatorResult:
    """Validate a scratch copy of the workspace; never raises, never exceeds the deadline."""
    task = ctx.task
    warnings: list[str] = []
    scratch: Path | None = None
    try:
        scratch = Path(tempfile.mkdtemp(prefix="agentbench-validation-")).resolve()
        assert_unrelated(scratch, *ctx.forbidden)
        scratch_workspace = scratch / "workspace"
        output = scratch / "result.json"
        report = copy_without_links(workspace_copy, scratch_workspace)
        if report.failed:
            return ValidatorResult(None, "The validation copy of the workspace was incomplete", warnings)
        env, _ = clean_environment(
            os.environ,
            ctx.variants,
            {"BENCHMARK_TASK_ID": task.task_id},
            passthrough=VALIDATOR_PASSTHROUGH,
        )
        with (scratch / "validator.log").open("wb") as log:
            outcome = run_bounded(
                [
                    sys.executable,
                    str(task.validator_path),
                    "--workspace",
                    str(scratch_workspace),
                    "--output",
                    str(output),
                ],
                cwd=task.root,
                env=env,
                stdout=log,
                deadline_seconds=task.validator_timeout_seconds,
            )
        warnings.extend(outcome.warnings)
        if not outcome.started:
            return ValidatorResult(None, f"The validator could not start: {outcome.error}", warnings)
        if outcome.timed_out:
            seconds = f"{task.validator_timeout_seconds:g}"
            return ValidatorResult(
                None, f"The validator did not finish within {seconds} seconds and was stopped", warnings
            )
        log_text = (scratch / "validator.log").read_bytes()[-4096:].decode("utf-8", errors="replace")
        if not output.is_file():
            detail = f" Its last output: {_tail(log_text)}" if _tail(log_text) else ""
            return ValidatorResult(
                None,
                f"The validator wrote no result (exit status {outcome.exit_status}).{detail}",
                warnings,
            )
        if output.stat().st_size > MAX_RESULT_BYTES:
            return ValidatorResult(None, "The validator result is larger than 5 MB", warnings)
        try:
            result = json.loads(
                output.read_text(encoding="utf-8"), parse_constant=lambda _name: None
            )
        except (ValueError, RecursionError) as error:
            return ValidatorResult(None, f"The validator result is not valid JSON: {error}", warnings)
        if not isinstance(result, dict):
            return ValidatorResult(None, "The validator result is not a JSON object", warnings)
        return ValidatorResult(result, None, warnings)
    except Exception as error:  # a validator fault must cost one score, not the run
        return ValidatorResult(None, f"The validator could not be run: {type(error).__name__}: {error}", warnings)
    finally:
        if scratch is not None:
            note = cleanup_tree(scratch)
            if note:
                warnings.append(note)


def _fallback_record(record: Mapping[str, Any], reason: str) -> dict[str, Any]:
    """A record that quotes nothing: used when the real one cannot be written safely."""
    safe = blank_record(record["contestant"], record["status"])
    for key in ("started_at", "ended_at", "wall_seconds", "timed_out"):
        safe[key] = record.get(key)
    safe["error"] = reason
    safe["validation"]["error"] = "Not validated because the record could not be written safely"
    return safe


def run_contestant(ctx: RunContext, contestant: str, settings: Mapping[str, Any]) -> dict[str, Any]:
    task, run_root, variants = ctx.task, ctx.run_root, ctx.variants
    contestant_root = run_root / contestant
    contestant_root.mkdir(parents=True, exist_ok=False)
    transcript_path = contestant_root / "transcript.log"
    workspace_copy = contestant_root / "workspace"
    validation_path = contestant_root / "validation.json"

    problems: list[str] = []
    warnings: list[str] = []
    env_removed: list[str] = []
    result = AdapterResult()
    workspace: Path | None = None
    copy_complete = False
    started_at = ended_at = utc_now()
    wall_seconds: float | None = None
    scrub_forms = list(variants)

    try:
        started = time.monotonic()
        try:
            workspace = create_workspace()
            scrub_forms.extend(variants_for([workspace, tempfile.gettempdir()]))
            assert_unrelated(workspace, *ctx.forbidden)
            brief = task.prompt_path.read_text(encoding="utf-8")
            env, env_removed = clean_environment(
                os.environ,
                variants,
                {
                    "BENCHMARK_TASK_ID": task.task_id,
                    "BENCHMARK_WORKSPACE": str(workspace),
                    "BENCHMARK_PROMPT_FILE": str(workspace / BRIEF_FILENAME),
                },
                passthrough=validate_passthrough(settings.get("env_passthrough"), contestant),
            )
            task.prepare_workspace(workspace)
            started_at = utc_now()
            started = time.monotonic()
            with transcript_path.open("ab") as transcript:
                request = LaunchRequest(
                    workspace=workspace,
                    brief=brief,
                    time_limit_seconds=ctx.time_limit_minutes * 60,
                    settings=settings,
                    env=env,
                    transcript=transcript,
                    forbidden_roots=variants,
                )
                result = get_adapter(settings["adapter"]).run(request)
        except Exception as error:  # a fault before or inside the adapter costs one contestant
            result = AdapterResult(
                launch_confirmed=False, error=f"{type(error).__name__}: {error}"
            )
        ended_at = utc_now()
        wall_seconds = round(time.monotonic() - started, 3)

        if workspace is not None and workspace.exists():
            try:
                report = copy_without_links(workspace, workspace_copy)
                copy_complete = not report.failed
                if report.skipped:
                    warnings.append(
                        f"Left {len(report.skipped)} link or special file(s) out of the workspace "
                        f"copy: {_listed(report.skipped)}"
                    )
                if report.failed:
                    problems.append(
                        f"{len(report.failed)} workspace file(s) could not be copied: "
                        f"{_listed(report.failed)}"
                    )
            except Exception as error:
                problems.append(
                    f"The workspace could not be copied into the run folder ({type(error).__name__})"
                )
    finally:
        if workspace is not None:
            note = cleanup_tree(workspace)
            if note:
                warnings.append(note)

    launched = result.launch_confirmed is not False and result.error is None
    validator = ValidatorResult(None, None, [])
    if not launched:
        validator.error = "The contestant did not start, so nothing was validated"
    elif not copy_complete:
        validator.error = "The workspace copy was missing or incomplete, so nothing was validated"
    else:
        validator = run_validator(ctx, workspace_copy)
    warnings.extend(result.warnings)
    warnings.extend(validator.warnings)

    if not launched:
        status = "launch_failed"
    elif result.timed_out:
        status = "timed_out"
    else:
        status = "completed"

    messages = [item for item in [result.error, *problems] if item]
    record = blank_record(contestant, status)
    record.update(
        {
            "adapter": settings["adapter"],
            "model": settings.get("model"),
            "version": result.version,
            "prompt_delivery": result.prompt_delivery,
            "launch": {"confirmed": result.launch_confirmed, "method": result.launch_method},
            "started_at": started_at,
            "ended_at": ended_at,
            "wall_seconds": wall_seconds,
            "exit_status": result.exit_status,
            "timed_out": result.timed_out,
            "error": "; ".join(messages) or None,
            "unattended_mode": settings.get("unattended_mode"),
            "usage": result.usage.to_dict(),
            "validation": normalize_validation(validator.raw, validator.error),
            "isolation": {"env_removed": env_removed},
            "warnings": warnings,
            "artifacts": {
                "workspace": relative(workspace_copy, run_root) if workspace_copy.exists() else None,
                "transcript": relative(transcript_path, run_root) if transcript_path.exists() else None,
                "validation": relative(validation_path, run_root),
            },
        }
    )
    record = finalize_record(record, scrub_forms)

    raw = validator.raw
    details = raw.get("details") if isinstance(raw, dict) else None
    try:
        details = scrub(details, scrub_forms)
    except Exception:  # a result nested too deeply to walk is not worth the run
        details = None
    write_errors: list[str] = []
    for path, content in (
        (validation_path, {"validation": record["validation"], "details": details}),
        (contestant_root / "record.json", record),
    ):
        try:
            path.write_text(json.dumps(content, indent=2, allow_nan=False), encoding="utf-8")
        except (OSError, ValueError) as error:
            write_errors.append(f"{path.name} was not written ({type(error).__name__})")
    if write_errors:
        record["warnings"] = [*(record["warnings"] or []), *write_errors]
    return record


def finalize_record(record: dict[str, Any], scrub_forms: list[str]) -> dict[str, Any]:
    """Scrub a record and refuse it, by the same matcher, if it is still not clean."""
    cleaned = scrub(record, scrub_forms)
    problems = validate_record(cleaned)
    if absolute_paths(cleaned, scrub_forms):
        problems = ["the record held a path"]
    if not problems:
        return cleaned
    reason = (
        "The record held an absolute path and was replaced by a minimal one"
        if problems == ["the record held a path"]
        else scrub_text("The record did not match the schema: " + problems[0], scrub_forms)
    )
    fallback = _fallback_record(record, reason)
    fallback = scrub(fallback, scrub_forms)
    if absolute_paths(fallback, scrub_forms) or validate_record(fallback):
        fallback = _fallback_record(blank_record(record["contestant"], record["status"]), "The record was replaced")
    return fallback

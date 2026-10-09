from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any

from .task import TaskSpec


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_commands(path: Path) -> dict[str, list[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Commands file must contain an object")
    commands: dict[str, list[str]] = {}
    for name, command in data.items():
        if command is None:
            continue
        if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
            raise ValueError(f"Command for {name} must be a JSON array of strings")
        commands[name] = command
    return commands


def replace_tokens(command: list[str], values: dict[str, str]) -> list[str]:
    return [item.format(**values) for item in command]


def run_validator(task: TaskSpec, workspace: Path, output_path: Path) -> dict[str, Any]:
    command = [
        sys.executable,
        str(task.validator_path),
        "--workspace",
        str(workspace),
        "--output",
        str(output_path),
    ]
    completed = subprocess.run(
        command,
        cwd=task.root,
        text=True,
        capture_output=True,
        timeout=task.time_limit_minutes * 60,
        check=False,
    )
    if output_path.is_file():
        result = json.loads(output_path.read_text(encoding="utf-8"))
    else:
        result = {
            "score": 0,
            "checks": {},
            "error": "Validator did not write a result",
        }
    result["validator_process"] = {
        "returncode": completed.returncode,
        "stdout": completed.stdout[-4000:],
        "stderr": completed.stderr[-4000:],
    }
    return result


def run_contestant(
    task: TaskSpec,
    contestant: str,
    command: list[str],
    run_root: Path,
    time_limit_minutes: int,
) -> dict[str, Any]:
    contestant_root = run_root / contestant
    workspace = contestant_root / "artifact"
    contestant_root.mkdir(parents=True, exist_ok=False)
    task.prepare_workspace(workspace)
    prompt = task.prompt_path.read_text(encoding="utf-8")
    prompt_path = contestant_root / "prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")
    stdout_path = contestant_root / "transcript.log"
    values = {
        "workspace": str(workspace),
        "task_dir": str(task.root),
        "run_dir": str(run_root),
        "prompt": str(prompt_path),
    }
    argv = replace_tokens(command, values)
    environment = os.environ.copy()
    environment.update(
        {
            "BENCHMARK_TASK_ID": task.task_id,
            "BENCHMARK_TASK_DIR": str(task.root),
            "BENCHMARK_WORKSPACE": str(workspace),
            "BENCHMARK_PROMPT_FILE": str(prompt_path),
            "BENCHMARK_RUN_DIR": str(run_root),
        }
    )
    started = time.monotonic()
    started_at = utc_now()
    timed_out = False
    returncode: int | None = None
    with stdout_path.open("w", encoding="utf-8") as transcript:
        transcript.write(f"command: {json.dumps(argv)}\n")
        transcript.write(f"started_at: {started_at}\n\n")
        process = subprocess.Popen(
            argv,
            cwd=workspace,
            stdin=subprocess.PIPE,
            stdout=transcript,
            stderr=subprocess.STDOUT,
            env=environment,
        )
        try:
            process.communicate(input=prompt, timeout=time_limit_minutes * 60)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            process.communicate()
        returncode = process.returncode
        transcript.write(f"\nended_at: {utc_now()}\n")
    elapsed = time.monotonic() - started
    transcript_text = stdout_path.read_text(encoding="utf-8", errors="replace")
    validation_path = contestant_root / "validation.json"
    validation = run_validator(task, workspace, validation_path)
    result = {
        "contestant": contestant,
        "command": argv,
        "started_at": started_at,
        "ended_at": utc_now(),
        "wall_clock_seconds": round(elapsed, 3),
        "returncode": returncode,
        "timed_out": timed_out,
        "validation": validation,
        "artifacts": {
            "workspace": str(workspace),
            "transcript": str(stdout_path),
            "validation": str(validation_path),
        },
    }
    (contestant_root / "result.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    return result


def run_benchmark(
    task: TaskSpec,
    commands: dict[str, list[str]],
    contestants: list[str],
    output_root: Path,
    time_limit_minutes: int | None = None,
) -> Path:
    run_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{task.task_id}"
    run_root = output_root / run_id
    run_root.mkdir(parents=True, exist_ok=False)
    limit = time_limit_minutes or task.time_limit_minutes
    summary: dict[str, Any] = {
        "run_id": run_id,
        "task_id": task.task_id,
        "task_title": task.title,
        "task_digest": task.digest(),
        "started_at": utc_now(),
        "time_limit_minutes": limit,
        "reference_policy": "see task.json",
        "contestants": {},
    }
    for contestant in contestants:
        if contestant not in commands:
            summary["contestants"][contestant] = {
                "status": "not_configured",
                "validation": {"score": 0, "checks": {}},
            }
            continue
        summary["contestants"][contestant] = run_contestant(
            task, contestant, commands[contestant], run_root, limit
        )
    summary["ended_at"] = utc_now()
    (run_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    write_summary_markdown(summary, run_root / "summary.md")
    return run_root


def write_summary_markdown(summary: dict[str, Any], path: Path) -> None:
    lines = [
        f"# Benchmark run {summary['run_id']}",
        "",
        f"Task: `{summary['task_id']}`",
        f"Task digest: `{summary['task_digest']}`",
        "",
        "| Contestant | Status | Score | Time seconds |",
        "|---|---|---:|---:|",
    ]
    for name, result in summary["contestants"].items():
        if result.get("status") == "not_configured":
            lines.append(f"| {name} | not configured | | |")
            continue
        validation = result.get("validation", {})
        score = validation.get("score", 0)
        lines.append(
            f"| {name} | completed | {score} | {result.get('wall_clock_seconds', '')} |"
        )
    lines.extend(
        [
            "",
            "The score comes from the task validator. Token, cache and cost capture per contestant is not implemented yet.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def compare_run(run_root: Path) -> str:
    summary = json.loads((run_root / "summary.json").read_text(encoding="utf-8"))
    lines = [f"Run: {summary['run_id']}", "Contestant        Score   Seconds"]
    for name, result in summary["contestants"].items():
        score = result.get("validation", {}).get("score", 0)
        seconds = result.get("wall_clock_seconds", "-")
        lines.append(f"{name:<17} {score:>5}   {seconds!s:>7}")
    return "\n".join(lines)

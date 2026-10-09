"""Each runnable task's validator fails an untouched starter, passes its reference solution, and always writes a result.

Also pins the task folder contract: every task has the files the runner needs,
its scoring file adds up to the maximum its validator reports, and its manifest
states how long the validator may run. The cases that break one thing at a time
live beside this file, one module per task.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "reference"))

from harness import (  # noqa: E402
    TASKS,
    ScenarioRuns,
    failed,
    kill_processes_mentioning,
    make_workspace,
    processes_mentioning,
    why,
)

RUNNABLE = ["evidence-desk-v1", "browser-game-v1", "science-decay-v1", "slide-deck-v1"]
TASK_JSON_KEYS = {
    "task_id",
    "title",
    "version",
    "runtime",
    "time_limit_minutes",
    "validator_timeout_seconds",
    "network",
    "references",
    "contestants",
}
VALIDATOR_TIMEOUT_SECONDS = {"browser-game-v1": 300, "evidence-desk-v1": 180, "science-decay-v1": 180, "slide-deck-v1": 180}


def prepared(task_id: str, solved: bool):
    def build(folder: Path):
        return make_workspace(task_id, folder, solved=solved), None

    return build


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    scenarios = {}
    for task_id in RUNNABLE:
        scenarios[f"starter: {task_id}"] = prepared(task_id, solved=False)
        scenarios[f"solved: {task_id}"] = prepared(task_id, solved=True)
    holder = ScenarioRuns(lambda name: name.split(": ")[1], tmp_path_factory.mktemp("validator-runs"), scenarios)
    try:
        yield holder
    finally:
        holder.close()
        kill_processes_mentioning("bench-game-")


@pytest.mark.parametrize("task_id", RUNNABLE)
def test_untouched_starter_scores_nothing(task_id, runs):
    process, result = runs.get(f"starter: {task_id}")
    assert result is not None, process.stderr
    assert result["score"] == 0, {name: ok for name, ok in result["checks"].items() if ok}
    assert result["max_score"] == 100
    assert process.returncode != 0


@pytest.mark.parametrize("task_id", RUNNABLE)
def test_reference_solution_scores_full_marks(task_id, runs):
    process, result = runs.get(f"solved: {task_id}")
    assert result is not None, process.stderr
    assert result["score"] == result["max_score"] == 100, why(result)
    assert failed(result) == set()
    assert process.returncode == 0


def test_every_task_folder_follows_the_contract(runs):
    task_files = sorted(TASKS.glob("*/task.json"))
    assert {path.parent.name for path in task_files} >= set(RUNNABLE)
    problems = []
    for task_file in task_files:
        folder = task_file.parent
        manifest = json.loads(task_file.read_text(encoding="utf-8"))
        if set(manifest) != TASK_JSON_KEYS:
            problems.append(f"{folder.name}: task.json keys are {sorted(manifest)}")
        if manifest.get("task_id") != folder.name:
            problems.append(f"{folder.name}: task_id is {manifest.get('task_id')}")
        if manifest.get("validator_timeout_seconds") != VALIDATOR_TIMEOUT_SECONDS.get(folder.name):
            problems.append(f"{folder.name}: validator_timeout_seconds is {manifest.get('validator_timeout_seconds')}")
        for required in ("task.md", "scoring.json", "validator/run.py"):
            if not (folder / required).is_file():
                problems.append(f"{folder.name}: missing {required}")
        if not (folder / "starter").is_dir() or not any((folder / "starter").iterdir()):
            problems.append(f"{folder.name}: starter is missing or empty")
        scoring = json.loads((folder / "scoring.json").read_text(encoding="utf-8"))["checks"]
        _, result = runs.get(f"starter: {folder.name}")
        if sum(scoring.values()) != result["max_score"]:
            problems.append(f"{folder.name}: scoring.json adds up to {sum(scoring.values())}, validator max is {result['max_score']}")
        if set(scoring) != set(result["checks"]):
            problems.append(f"{folder.name}: scoring checks differ from validator checks")
        for hidden in ("validator", "expected.json"):
            if list((folder / "starter").rglob(hidden)):
                problems.append(f"{folder.name}: the starter contains {hidden}")
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("task_id", RUNNABLE)
def test_a_validator_writes_a_result_for_an_empty_workspace_in_a_new_folder(task_id, tmp_path):
    workspace = tmp_path / "empty"
    workspace.mkdir()
    output = tmp_path / "not" / "yet" / "there" / "result.json"
    process = subprocess.run(
        [sys.executable, str(TASKS / task_id / "validator" / "run.py"), "--workspace", str(workspace), "--output", str(output)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert "Traceback" not in process.stderr, process.stderr
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["score"] == 0 and result["max_score"] == 100
    assert not list(output.parent.glob("*.part"))


def test_the_process_listing_the_cleanup_tests_rely_on_can_see_a_running_process():
    token = f"visible-to-the-listing-{time.time_ns()}"
    sleeper = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep(60)  # {token}"])
    try:
        deadline = time.monotonic() + 30
        seen = []
        while not seen and time.monotonic() < deadline:
            seen = processes_mentioning(token)
        assert seen, "a running process was not found, so a test that expects none would pass for nothing"
    finally:
        sleeper.kill()
        sleeper.wait()
    assert processes_mentioning(token) == []

"""Shared helpers for the task validator tests.

A reference solution lives in tests/reference/<task id>/. It is copied over the
starter in a temporary folder: files from `solution/` are overlaid as they are,
and a `build.py`, when present, is run with `--workspace <folder>` to write
generated files such as a slide deck.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
TASKS = ROOT / "tasks"
REFERENCE = ROOT / "tests" / "reference"
ENV = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
VALIDATOR_TIMEOUT = 300


def make_workspace(task_id: str, destination: Path, solved: bool) -> Path:
    workspace = destination / ("solved" if solved else "starter")
    shutil.copytree(TASKS / task_id / "starter", workspace)
    if solved:
        solution = REFERENCE / task_id / "solution"
        if solution.is_dir():
            shutil.copytree(solution, workspace, dirs_exist_ok=True)
        build = REFERENCE / task_id / "build.py"
        if build.is_file():
            subprocess.run([sys.executable, str(build), "--workspace", str(workspace)], check=True, env=ENV, timeout=120)
    return workspace


def clone(source: Path, destination: Path, name: str = "work") -> Path:
    """Copy a prepared workspace so a test can change it."""
    target = destination / name
    shutil.copytree(source, target)
    return target


def run_validator(task_id: str, workspace: Path, destination: Path, env: dict | None = None):
    """Run a validator. Returns (completed process, parsed result file or None)."""
    output = destination / f"{workspace.name}-result.json"
    started = time.monotonic()
    process = subprocess.run(
        [sys.executable, str(TASKS / task_id / "validator" / "run.py"), "--workspace", str(workspace), "--output", str(output)],
        capture_output=True,
        text=True,
        env={**ENV, **(env or {})},
        timeout=VALIDATOR_TIMEOUT,
    )
    process.elapsed = time.monotonic() - started
    result = json.loads(output.read_text(encoding="utf-8")) if output.is_file() else None
    return process, result


def validate(task_id: str, workspace: Path, destination: Path, env: dict | None = None) -> dict:
    """Run a validator and return its result. Fails when it crashed or wrote nothing."""
    process, result = run_validator(task_id, workspace, destination, env)
    assert "Traceback" not in process.stderr, process.stderr
    assert result is not None, f"the validator wrote no result file: {process.stdout} {process.stderr}"
    return result


def failed(result: dict) -> set[str]:
    """Names of the checks that lost points."""
    return {name for name, ok in result["checks"].items() if not ok}


def why(result: dict) -> dict:
    return {name: result["details"].get(name, "") for name in failed(result)}


def shadow_module(destination: Path, module: str) -> dict:
    """An environment in which importing `module` raises ImportError, as when it is not installed."""
    folder = destination / "shadow" / module
    folder.mkdir(parents=True)
    (folder / "__init__.py").write_text(f"raise ImportError('{module} is not installed here')\n", encoding="utf-8")
    return {"PYTHONPATH": str(folder.parent)}


def processes_mentioning(token: str) -> list[str]:
    """Command lines of running processes that contain token."""
    if os.name == "nt":
        script = (
            "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*" + token + "*' -and $_.ProcessId -ne $PID } "
            "| ForEach-Object { $_.CommandLine }"
        )
        listing = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=60).stdout
    else:
        listing = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True, timeout=60).stdout
    return [line for line in listing.splitlines() if token in line and "harness" not in line and "Get-CimInstance" not in line]


def kill_processes_mentioning(token: str) -> None:
    """Stop leftovers of a test, so a failing test does not leave processes running."""
    if os.name == "nt":
        script = (
            "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*" + token + "*' -and $_.ProcessId -ne $PID } "
            "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=60)
    else:
        subprocess.run(["pkill", "-f", token], capture_output=True, timeout=60)


class ScenarioRuns:
    """Runs a validator on many prepared workspaces at once (six at a time), the first time any one of them is asked for.

    task_id is one task id, or a function of the scenario name. scenarios maps a name to a function
    (folder) -> (workspace, extra environment or None) that prepares the workspace inside the folder it is given. Requesting one result queues the requested scenario
    first and the others behind it, so a single selected test does not wait for all the rest.
    """

    def __init__(self, task_id: str, base: Path, scenarios: dict, workers: int = 6):
        from concurrent.futures import ThreadPoolExecutor
        import threading

        self.task_id = task_id
        self.base = base
        self.scenarios = scenarios
        self.pool = ThreadPoolExecutor(workers)
        self.futures: dict = {}
        self.lock = threading.Lock()

    def start(self, name: str) -> None:
        with self.lock:
            if name not in self.futures:
                self.futures[name] = self.pool.submit(self.run, name)

    def run(self, name: str):
        folder = self.base / f"scenario-{list(self.scenarios).index(name)}"
        folder.mkdir()
        workspace, env = self.scenarios[name](folder)
        task_id = self.task_id(name) if callable(self.task_id) else self.task_id
        return run_validator(task_id, workspace, folder, env)

    def get(self, name: str):
        """The finished process and result file (or None) of one scenario."""
        self.start(name)
        if not os.environ.get("SCENARIOS_ON_DEMAND"):  # set to run only the scenarios the selected tests ask for
            for other in self.scenarios:
                self.start(other)
        return self.futures[name].result()

    def result(self, name: str) -> dict:
        process, result = self.get(name)
        assert "Traceback" not in process.stderr, process.stderr
        assert result is not None, f"the validator wrote no result file: {process.stdout} {process.stderr}"
        return result

    def finish(self) -> None:
        for name in self.scenarios:
            self.get(name)

    def close(self) -> None:
        self.pool.shutdown(wait=True, cancel_futures=True)

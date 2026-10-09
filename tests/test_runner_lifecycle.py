"""Process lifecycle of a run: what is killed, what is bounded and what one failure cannot stop.

Covers the contestant, the version probe and the validator: descendants die with
their run, no wait outlasts its deadline, a validator that fails gives a null
score and never a zero, one contestant's failure leaves the next one running,
links in a workspace are never followed, and the contestant and validator get
an allow-listed environment.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import types

import pytest

from runner import isolation, lifecycle
from runner.cli import main
from runner.adapters import ADAPTERS, Adapter, AdapterResult
from runner.adapters import command as command_adapter
from runner.engine import load_contestants, load_summary, run_benchmark
from runner.proc import run_bounded, run_captured
from test_runner_run import PROBE, STUB, VALIDATOR, command, make_task, run_cli

GRANDCHILD = (
    "import sys, time\n"
    "end = time.time() + 15\n"
    "while time.time() < end:\n"
    "    open(sys.argv[1], 'a').write('x')\n"
    "    time.sleep(0.05)\n"
)


def spawning(tail: str) -> str:
    """Contestant code: start a grandchild that keeps writing to $BEAT_FILE, then run `tail`."""
    return (
        "import os, pathlib, subprocess, sys, time\n"
        "beat = os.environ['BEAT_FILE']\n"
        f"subprocess.Popen([sys.executable, '-c', {GRANDCHILD!r}, beat])\n"
        "while not pathlib.Path(beat).exists():\n"
        "    time.sleep(0.02)\n" + tail
    )


def assert_stopped(beat: Path) -> None:
    """The grandchild writes every 0.05 s while alive; a dead one leaves the file as it was."""
    assert beat.exists(), "the grandchild never started, so this proves nothing"
    size = beat.stat().st_size
    time.sleep(0.8)
    assert beat.stat().st_size == size, "a grandchild is still running"


@pytest.fixture
def task_dir(tmp_path: Path):
    return make_task(tmp_path / "task")


@pytest.fixture
def beat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "beats" / "beat.txt"
    path.parent.mkdir()
    monkeypatch.setenv("BEAT_FILE", str(path))
    return path


@pytest.fixture
def temp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Send workspaces and validation copies to a folder the test can inspect and empty."""
    home = tmp_path / "tmp"
    home.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(home))
    return home


def leftovers(home: Path) -> list[str]:
    return sorted(item.name for item in home.iterdir() if item.name.startswith("agentbench-"))


def write_config(tmp_path: Path, config: dict) -> Path:
    path = tmp_path / "contestants.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def run_all(task, tmp_path: Path, config: dict, names: list[str], limit=None, **kwargs) -> Path:
    return run_benchmark(
        task, load_contestants(write_config(tmp_path, config)), names, tmp_path / "runs", limit, **kwargs
    )


def record_of(run: Path, name: str) -> dict:
    return load_summary(run)["contestants"][name]


# Invariants 1 and 2: descendants die with the run, and no wait outlasts its deadline.


def test_a_grandchild_is_killed_when_the_contestant_runs_out_of_time(task_dir, beat, temp_home, tmp_path):
    config = {"slow": command(spawning("time.sleep(60)\n"), env_passthrough=["BEAT_FILE"])}
    run = run_all(task_dir, tmp_path, config, ["slow"], 0.05)
    record = record_of(run, "slow")
    assert record["status"] == "timed_out"
    assert record["exit_status"] is None
    assert record["warnings"] == [], "everything it started was confirmed stopped"
    assert_stopped(beat)


def test_a_grandchild_is_killed_when_the_contestant_exits_and_the_run_carries_on(
    task_dir, beat, temp_home, tmp_path
):
    config = {
        "leaver": command(spawning("sys.exit(0)\n"), env_passthrough=["BEAT_FILE"]),
        "after": STUB,
    }
    run = run_all(task_dir, tmp_path, config, ["leaver", "after"])
    leaver, after = record_of(run, "leaver"), record_of(run, "after")
    assert leaver["status"] == "completed" and leaver["exit_status"] == 0
    assert leaver["warnings"] == []
    assert after["status"] == "completed" and after["validation"]["score"] == 10
    assert_stopped(beat)
    assert leftovers(temp_home) == []


def test_a_wait_never_outlasts_its_deadline_even_when_a_grandchild_holds_the_output(beat, tmp_path):
    code = spawning("sys.exit(0)\n")
    started = time.monotonic()
    outcome, text = run_captured(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=dict(os.environ),
        deadline_seconds=1,
    )
    elapsed = time.monotonic() - started
    assert outcome.started and outcome.exit_status == 0 and not outcome.timed_out
    assert elapsed < 10, "the grandchild lives 15 seconds; a wait on its output would take that long"
    assert_stopped(beat)


def test_a_deadline_is_a_deadline_for_a_process_that_never_ends(tmp_path):
    with (tmp_path / "out").open("wb") as sink:
        started = time.monotonic()
        outcome = run_bounded(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=tmp_path,
            env=dict(os.environ),
            stdout=sink,
            deadline_seconds=1,
        )
    assert outcome.timed_out and outcome.exit_status is None
    assert time.monotonic() - started < 20


def test_a_brief_the_contestant_never_reads_cannot_block_the_launch(tmp_path):
    with (tmp_path / "out").open("wb") as sink:
        outcome = run_bounded(
            [sys.executable, "-c", "pass"],
            cwd=tmp_path,
            env=dict(os.environ),
            stdout=sink,
            deadline_seconds=20,
            stdin_bytes=b"x" * (4 * 1024 * 1024),
        )
    assert outcome.started and outcome.exit_status == 0 and not outcome.timed_out


def test_a_program_that_cannot_start_is_reported_not_raised(tmp_path):
    with (tmp_path / "out").open("wb") as sink:
        outcome = run_bounded(
            ["no-such-program-anywhere"],
            cwd=tmp_path,
            env=dict(os.environ),
            stdout=sink,
            deadline_seconds=5,
        )
    assert not outcome.started and outcome.error and not outcome.timed_out


def test_the_version_probe_is_bounded_and_leaves_nothing_running(
    task_dir, beat, temp_home, tmp_path, monkeypatch
):
    monkeypatch.setattr(command_adapter, "VERSION_TIMEOUT_SECONDS", 3)
    config = {
        "c": command(
            "pass",
            version_argv=[sys.executable, "-c", spawning("time.sleep(60)\n")],
            env_passthrough=["BEAT_FILE"],
        )
    }
    started = time.monotonic()
    run = run_all(task_dir, tmp_path, config, ["c"])
    assert time.monotonic() - started < 30
    assert record_of(run, "c")["version"] is None
    assert_stopped(beat)


# Invariant 3 and 8: the validator has a deadline, and a bad result is a null score, never zero.

VALIDATOR_WRITES = "import argparse, json\np = argparse.ArgumentParser()\np.add_argument('--workspace')\np.add_argument('--output')\na = p.parse_args()\n"

VALIDATOR_CASES = {
    "nothing": ("import sys\n", "wrote no result"),
    "crash": ("raise SystemExit('validator exploded')\n", "validator exploded"),
    "not json": (VALIDATOR_WRITES + "open(a.output, 'w').write('{nope')\n", "not valid JSON"),
    "not an object": (VALIDATOR_WRITES + "open(a.output, 'w').write('[1, 2]')\n", "not a JSON object"),
    "error key": (
        VALIDATOR_WRITES
        + "open(a.output, 'w').write(json.dumps({'error': 'browser missing', 'score': None}))\n",
        "browser missing",
    ),
    "no score": (
        VALIDATOR_WRITES + "open(a.output, 'w').write(json.dumps({'max_score': 10}))\n",
        "no numeric score",
    ),
    "not a number": (
        VALIDATOR_WRITES + "open(a.output, 'w').write('{\"score\": NaN, \"max_score\": 10}')\n",
        "no numeric score",
    ),
    "too slow": ("import time\ntime.sleep(60)\n", "did not finish within 1 seconds"),
}


@pytest.mark.parametrize("case", sorted(VALIDATOR_CASES))
def test_a_validator_that_fails_gives_a_null_score_and_the_run_carries_on(tmp_path, temp_home, case):
    source, expected = VALIDATOR_CASES[case]
    task = make_task(tmp_path / "task", source, {"validator_timeout_seconds": 1})
    started = time.monotonic()
    run = run_all(task, tmp_path, {"a": STUB, "b": STUB}, ["a", "b"])
    assert time.monotonic() - started < 40, "the slow validator sleeps 60 seconds; the deadline is 1"
    for name in ("a", "b"):
        record = record_of(run, name)
        assert record["status"] == "completed"
        assert record["validation"]["score"] is None, case
        assert expected in record["validation"]["error"], (case, record["validation"]["error"])
    assert (run / "summary.json").is_file() and (run / "summary.md").is_file()
    assert leftovers(temp_home) == []


def test_a_validator_that_scores_below_the_maximum_exits_nonzero_and_keeps_its_score(tmp_path):
    source = VALIDATOR_WRITES + "open(a.output, 'w').write(json.dumps({'score': 0, 'max_score': 10}))\nraise SystemExit(1)\n"
    task = make_task(tmp_path / "task", source)
    record = record_of(run_all(task, tmp_path, {"a": STUB}, ["a"]), "a")
    assert record["validation"]["score"] == 0
    assert record["validation"]["error"] is None


def test_the_validator_deadline_comes_from_the_task_and_defaults_to_ten_minutes(tmp_path):
    assert make_task(tmp_path / "one").validator_timeout_seconds == 600
    task = make_task(tmp_path / "two", manifest={"validator_timeout_seconds": 90})
    assert task.validator_timeout_seconds == 90


def test_a_fault_while_running_the_validator_costs_one_score_and_not_the_run(tmp_path, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("cannot start")

    monkeypatch.setattr(lifecycle, "run_bounded", broken)
    task = make_task(tmp_path / "task")
    run = run_all(task, tmp_path, {"a": STUB, "b": STUB}, ["a", "b"])
    for name in ("a", "b"):
        record = record_of(run, name)
        assert record["status"] == "completed"
        assert record["validation"]["score"] is None
        assert "could not be run" in record["validation"]["error"]
    assert (run / "summary.json").is_file()


# Invariant 7: the validator works on a scratch copy outside the run folder.

SPY = (
    VALIDATOR_WRITES
    + "import os, pathlib\n"
    + "here = pathlib.Path(a.workspace).resolve()\n"
    + "pathlib.Path('spy.json').write_text(json.dumps({'workspace': str(here), 'env': dict(os.environ)}))\n"
    + "open(a.output, 'w').write(json.dumps({'score': 1, 'max_score': 1}))\n"
)


def test_the_validator_sees_a_scratch_copy_outside_the_run_folder(tmp_path, temp_home):
    task = make_task(tmp_path / "task", SPY)
    run = run_all(task, tmp_path, {"a": STUB}, ["a"])
    seen = json.loads((task.root / "spy.json").read_text(encoding="utf-8"))
    workspace = Path(seen["workspace"])
    assert run.resolve() not in workspace.parents
    assert (tmp_path / "runs").resolve() not in workspace.parents
    assert workspace.parent.parent.resolve() == temp_home.resolve()
    assert not workspace.exists(), "the scratch copy must be removed after the result is read"
    assert leftovers(temp_home) == []


# Invariant 4 and 5: one contestant's failure is recorded for it alone.


def test_a_failed_workspace_copy_is_recorded_and_the_next_contestant_runs(tmp_path, temp_home, monkeypatch):
    task = make_task(tmp_path / "task")
    real = lifecycle.copy_without_links
    calls = []

    def fail_once(source, destination):
        calls.append(source)
        if len(calls) == 1:
            raise OSError("disk full")
        return real(source, destination)

    monkeypatch.setattr(lifecycle, "copy_without_links", fail_once)
    run = run_all(task, tmp_path, {"first": STUB, "second": STUB}, ["first", "second"])
    first, second = record_of(run, "first"), record_of(run, "second")
    assert "could not be copied" in first["error"]
    assert first["validation"]["score"] is None and first["validation"]["error"]
    assert second["error"] is None and second["validation"]["score"] == 10
    assert (run / "summary.json").is_file()


def test_a_crash_in_one_contestant_is_recorded_and_the_next_contestant_runs(tmp_path, monkeypatch):
    task = make_task(tmp_path / "task")
    real = lifecycle.finalize_record

    def explode_for_first(record, forms):
        if record["contestant"] == "first":
            raise RuntimeError("unexpected")
        return real(record, forms)

    monkeypatch.setattr(lifecycle, "finalize_record", explode_for_first)
    run = run_all(task, tmp_path, {"first": STUB, "second": STUB}, ["first", "second"])
    first, second = record_of(run, "first"), record_of(run, "second")
    assert first["status"] == "launch_failed" and "RuntimeError" in first["error"]
    assert first["validation"]["score"] is None
    assert second["status"] == "completed" and second["validation"]["score"] == 10


def test_a_workspace_that_cannot_be_deleted_is_a_warning_not_a_failure(tmp_path, temp_home, monkeypatch):
    task = make_task(tmp_path / "task")

    def locked(path):
        raise PermissionError("in use")

    monkeypatch.setattr(isolation, "remove_tree", locked)
    monkeypatch.setattr(isolation, "time", types.SimpleNamespace(sleep=lambda _seconds: None))
    run = run_all(task, tmp_path, {"first": STUB, "second": STUB}, ["first", "second"])
    for name in ("first", "second"):
        record = record_of(run, name)
        assert record["status"] == "completed" and record["validation"]["score"] == 10
        assert any("Could not remove the temporary folder" in item for item in record["warnings"])
    assert (run / "summary.json").is_file()


# Invariant 5: links in a workspace are never followed.


def make_link(link: Path, target: Path, kind: str) -> None:
    if kind == "junction":
        if sys.platform != "win32":
            pytest.skip("junctions exist only on Windows")
        done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        if done.returncode != 0:
            pytest.skip("this account cannot create junctions")
        return
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this account cannot create symbolic links")


class LinkingAdapter(Adapter):
    """Plants links in the workspace, the way a contestant could."""

    name = "linking"

    def run(self, request):
        outside = Path(request.settings["outside"])
        make_link(request.workspace / "to_outside", outside, request.settings["kind"])
        if request.settings["kind"] == "symlink":
            try:
                os.symlink(outside / "no-such-file", request.workspace / "dangling")
            except (OSError, NotImplementedError):
                pytest.skip("this account cannot create symbolic links")
        else:
            gone = outside.parent / "gone"
            gone.mkdir()
            make_link(request.workspace / "dangling", gone, "junction")
            gone.rmdir()
        (request.workspace / "answer.txt").write_text("42\n", encoding="utf-8")
        return AdapterResult(prompt_delivery="file", launch_confirmed=True, launch_method="in_process", exit_status=0)


@pytest.mark.parametrize("kind", ["symlink", "junction"])
def test_a_link_in_the_workspace_is_never_followed_into_the_run_folder(tmp_path, temp_home, monkeypatch, kind):
    monkeypatch.setitem(ADAPTERS, "linking", LinkingAdapter)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside-secret-text", encoding="utf-8")
    task = make_task(tmp_path / "task")
    config = {"linker": {"adapter": "linking", "outside": str(outside), "kind": kind}}
    run = run_all(task, tmp_path, config, ["linker"])
    record = record_of(run, "linker")
    assert record["status"] == "completed" and record["validation"]["score"] == 10
    copy = run / "linker" / "workspace"
    assert not os.path.lexists(copy / "to_outside") and not os.path.lexists(copy / "dangling")
    assert (copy / "answer.txt").is_file()
    assert any("to_outside" in item for item in record["warnings"])
    for found in run.rglob("*"):
        if found.is_file():
            assert "outside-secret-text" not in found.read_text(encoding="utf-8", errors="replace")
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "outside-secret-text"
    assert leftovers(temp_home) == []


# Invariant 6: an allow-listed environment for the contestant and the validator.


def spellings(path: Path) -> list[str]:
    forms = [str(path), path.as_posix()]
    if path.drive:
        forms.append("/" + path.drive[0].lower() + path.as_posix()[2:])
        forms.append(str(path).upper())
    return forms


DUMP_ENV = (
    VALIDATOR_WRITES
    + "import os, pathlib\n"
    + "pathlib.Path('env_dump.json').write_text(json.dumps(dict(os.environ)))\n"
    + "open(a.output, 'w').write(json.dumps({'score': 1, 'max_score': 1}))\n"
)

HOSTILE = ("VIRTUAL_ENV", "PWD", "PYTHONPATH", "LEAK_PLAIN", "PASS_BAD")


def path_entries(env: dict) -> list[str]:
    return [item.replace("\\", "/").lower() for item in env["PATH"].split(os.pathsep) if item]


def test_the_contestant_and_the_validator_get_an_allow_listed_environment(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    task = make_task(repo / "tasks" / "mini", DUMP_ENV)
    keep = tmp_path / "keep"
    keep.mkdir()
    entries = [keep]
    for root in (repo, repo / "tasks", repo / "tasks" / "mini" / "validator"):
        entries.extend(spellings(root))
    monkeypatch.setenv("PATH", os.pathsep.join([str(item) for item in entries] + [os.environ["PATH"]]))
    for name in ("VIRTUAL_ENV", "PWD", "PYTHONPATH"):
        monkeypatch.setenv(name, str(repo / "lib"))
    monkeypatch.setenv("LEAK_PLAIN", "plain")
    monkeypatch.setenv("PASS_ME", "through")
    monkeypatch.setenv("PASS_BAD", spellings(repo)[-2 if repo.drive else 0] + "/lib")
    config = {"probe": command(PROBE, env_passthrough=["PASS_ME", "PASS_BAD"])}
    run = run_all(task, tmp_path, config, ["probe"], repo_root=repo)

    seen = {
        "contestant": json.loads((run / "probe" / "workspace" / "probe.json").read_text(encoding="utf-8"))["env"],
        "validator": json.loads((task.root / "env_dump.json").read_text(encoding="utf-8")),
    }
    forbidden = [form.replace("\\", "/").lower() for form in spellings(repo)]
    for who, env in seen.items():
        names = {name.upper() for name in env}
        assert not names & set(HOSTILE), (who, names & set(HOSTILE))
        assert all(isolation.is_allowed_name(name, ["PASS_ME", *isolation.VALIDATOR_PASSTHROUGH]) for name in env), (who, sorted(env))
        assert env["BENCHMARK_TASK_ID"] == "mini-v1"
        entries_seen = path_entries(env)
        assert str(keep).replace("\\", "/").lower() in entries_seen, who
        assert not [e for e in entries_seen if any(e == f or e.startswith(f + "/") for f in forbidden)], who
    assert seen["contestant"]["PASS_ME"] == "through"
    assert "PASS_ME" not in seen["validator"]
    removed = record_of(run, "probe")["isolation"]["env_removed"]
    assert {"PATH", "VIRTUAL_ENV", "PWD", "PYTHONPATH", "LEAK_PLAIN", "PASS_BAD"} <= set(removed)
    assert "PASS_ME" not in removed
    assert set(removed) <= set(os.environ), "env_removed holds names only, and only names that existed"


# The version probe uses the contestant's search path, not the runner's.


TOOL = "mytool.exe" if sys.platform == "win32" else "mytool"
TOOL_ARGS = ["/c", "echo mytool 4.5.6"] if sys.platform == "win32" else []


def make_tool(folder: Path) -> None:
    """A real program (not a script) that prints 'mytool 4.5.6', so the operating system alone could find it."""
    folder.mkdir(parents=True)
    if sys.platform == "win32":
        shutil.copy(Path(os.environ["SYSTEMROOT"]) / "System32" / "cmd.exe", folder / TOOL)
    else:
        tool = folder / TOOL
        tool.write_text("#!/bin/sh\necho mytool 4.5.6\n", encoding="utf-8")
        tool.chmod(0o755)


def test_the_version_is_read_from_version_argv_through_the_contestants_search_path(tmp_path, monkeypatch):
    task = make_task(tmp_path / "task")
    make_tool(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ["PATH"])
    config = {"c": command("pass", version_argv=[TOOL, *TOOL_ARGS])}
    assert record_of(run_all(task, tmp_path, config, ["c"]), "c")["version"] == "mytool 4.5.6"


def test_a_version_program_under_a_forbidden_folder_is_not_found_on_the_cleaned_path(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    task = make_task(repo / "tasks" / "mini")
    make_tool(repo / "bin")
    monkeypatch.setenv("PATH", str(repo / "bin") + os.pathsep + os.environ["PATH"])
    config = {
        "c": command("pass", version_argv=[TOOL, *TOOL_ARGS]),
        "d": {"adapter": "command", "argv": [TOOL]},
    }
    run = run_all(task, tmp_path, config, ["c", "d"], repo_root=repo)
    assert record_of(run, "c")["version"] is None
    assert record_of(run, "d")["status"] == "launch_failed"
    assert "search path" in record_of(run, "d")["error"]


def test_a_failing_contestant_has_its_exit_status_recorded(tmp_path):
    task = make_task(tmp_path / "task")
    record = record_of(run_all(task, tmp_path, {"c": command("import sys; sys.exit(3)")}, ["c"]), "c")
    assert record["status"] == "completed" and record["exit_status"] == 3
    assert record["timed_out"] is False


# Requests are checked before anything runs.


def test_an_unknown_or_repeated_contestant_fails_before_anything_runs(tmp_path, capsys):
    task = make_task(tmp_path / "task")
    path = write_config(tmp_path, {"a": STUB, "ghost": None})
    contestants = load_contestants(path)
    for names in (["a", "nope"], ["a", "a"], ["a", "A"], []):
        with pytest.raises(ValueError):
            run_benchmark(task, contestants, names, tmp_path / "runs")
        assert not (tmp_path / "runs").exists(), names
    code = main(["run", "--task", str(task.root), "--contestants-file", str(path), "--contestants", "a", "nope", "--output", str(tmp_path / "runs")])
    assert code == 2
    assert "error: " in capsys.readouterr().err
    assert not (tmp_path / "runs").exists()


def test_a_contestants_file_that_lists_a_name_twice_is_refused(tmp_path):
    path = tmp_path / "c.json"
    path.write_text('{"a": null, "A": null}', encoding="utf-8")
    with pytest.raises(ValueError, match="more than once"):
        load_contestants(path)
    path.write_text('{"a": null, "a": null}', encoding="utf-8")
    with pytest.raises(ValueError, match="more than once"):
        load_contestants(path)


def test_a_contestant_name_cannot_leave_the_run_folder(tmp_path):
    for name in ("../escape", "a/b", "a\\b", "con", "summary.json", "trailing."):
        path = tmp_path / "c.json"
        path.write_text(json.dumps({name: None}), encoding="utf-8")
        with pytest.raises(ValueError, match="not a usable contestant name"):
            load_contestants(path)


# Prompt in the argument list and a Windows script shim.


def test_an_argument_prompt_is_refused_for_a_cmd_or_bat_program(tmp_path, monkeypatch):
    def load(entry):
        path = tmp_path / "c.json"
        path.write_text(json.dumps({"x": entry}), encoding="utf-8")
        return load_contestants(path)

    for program in ("tool.cmd", "TOOL.BAT"):
        with pytest.raises(ValueError, match="cmd.exe"):
            load({"adapter": "command", "argv": [program, "{prompt}"], "prompt": "argument"})
    assert "x" in load({"adapter": "command", "argv": ["tool.cmd"], "prompt": "stdin"})
    assert "x" in load({"adapter": "command", "argv": ["tool.cmd", "{prompt_file}"], "prompt": "file"})
    assert "x" in load({"adapter": "command", "argv": ["tool.exe", "{prompt}"], "prompt": "argument"})

    if sys.platform == "win32":
        (tmp_path / "shims").mkdir()
        (tmp_path / "shims" / "mytool.cmd").write_text("@echo x\r\n", encoding="utf-8")
        monkeypatch.setenv("PATH", str(tmp_path / "shims") + os.pathsep + os.environ["PATH"])
        with pytest.raises(ValueError, match="cmd.exe"):
            load({"adapter": "command", "argv": ["mytool", "{prompt}"], "prompt": "argument"})


# A path never reaches a record, and a record that would carry one is replaced, not quoted.

LEAKY = "went wrong at file:///C:/Work%20Dir/a.txt and C:" + "\\Work\\x and %2Fetc%2Fpasswd"
LEAKED_PIECES = ("Work", "etc", "passwd")


class ErroringAdapter(Adapter):
    name = "erroring"

    def run(self, request):
        return AdapterResult(launch_confirmed=False, error=request.settings["message"])


def test_a_path_in_an_adapter_error_is_replaced_before_the_record_is_written(tmp_path, monkeypatch):
    monkeypatch.setitem(ADAPTERS, "erroring", ErroringAdapter)
    task = make_task(tmp_path / "task")
    run = run_all(task, tmp_path, {"c": {"adapter": "erroring", "message": LEAKY}}, ["c"])
    for text in ((run / "c" / "record.json").read_text(encoding="utf-8"), (run / "summary.json").read_text(encoding="utf-8")):
        assert not [piece for piece in LEAKED_PIECES if piece in text]
    assert "<path>" in record_of(run, "c")["error"]


def test_a_record_that_still_holds_a_path_is_replaced_by_one_that_quotes_nothing(tmp_path, monkeypatch):
    monkeypatch.setitem(ADAPTERS, "erroring", ErroringAdapter)
    monkeypatch.setattr(lifecycle, "scrub", lambda value, _forms: value)
    task = make_task(tmp_path / "task")
    config = {"c": {"adapter": "erroring", "message": LEAKY}, "d": {"adapter": "erroring", "message": "plain"}}
    run = run_all(task, tmp_path, config, ["c", "d"])
    leaky, plain = record_of(run, "c"), record_of(run, "d")
    assert leaky["status"] == "launch_failed"
    assert leaky["error"].startswith("The record held an absolute path")
    assert leaky["validation"]["score"] is None
    assert plain["error"] == "plain"
    text = (run / "summary.json").read_text(encoding="utf-8")
    assert not [piece for piece in LEAKED_PIECES if piece in text]


def test_a_context_value_naming_a_path_is_refused_before_anything_runs(tmp_path):
    task = make_task(tmp_path / "task")
    contestants = load_contestants(write_config(tmp_path, {"a": STUB}))
    for value in ("file:///C:/Work%20Dir/repo", "C:" + "\\Work\\repo", "%2Fetc%2Fpasswd"):
        with pytest.raises(ValueError, match="absolute path"):
            run_benchmark(task, contestants, ["a"], tmp_path / "runs", context={"where": value})
    assert not (tmp_path / "runs").exists()

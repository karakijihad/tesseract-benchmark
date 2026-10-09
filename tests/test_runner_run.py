"""Runner runs: a stub contestant end to end, isolation, records and the transcript."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile

import pytest

from runner.cli import main
from runner.engine import load_contestants, load_summary, run_benchmark
from runner.record import absolute_paths, validate_record, validate_summary
from runner.report import render_summary_markdown
from runner.task import TaskSpec

REPO = Path(__file__).resolve().parent.parent

VALIDATOR = """
import argparse, json, pathlib
parser = argparse.ArgumentParser()
parser.add_argument("--workspace")
parser.add_argument("--output")
args = parser.parse_args()
workspace = pathlib.Path(args.workspace)
answer = workspace / "answer.txt"
checks = {
    "has_answer": answer.is_file(),
    "correct": answer.is_file() and answer.read_text().strip() == "42",
}
score = sum(5 for passed in checks.values() if passed)
pathlib.Path(args.output).write_text(json.dumps({
    "score": score,
    "max_score": 10,
    "checks": checks,
    "details": {"looked_in": str(workspace)},
}))
"""

PROBE = (
    "import json, os, sys\n"
    "json.dump({'cwd': os.getcwd(), 'env': dict(os.environ), 'argv': sys.argv},"
    " open('probe.json', 'w'))\n"
)


def make_task(root: Path, validator: str = VALIDATOR, manifest: dict | None = None) -> TaskSpec:
    (root / "starter").mkdir(parents=True)
    (root / "starter" / "seed.txt").write_text("seed", encoding="utf-8")
    (root / "validator").mkdir()
    (root / "validator" / "run.py").write_text(validator, encoding="utf-8")
    (root / "task.json").write_text(
        json.dumps(
            {"task_id": "mini-v1", "title": "Mini", "time_limit_minutes": 1, **(manifest or {})}
        ),
        encoding="utf-8",
    )
    (root / "task.md").write_text("Write answer.txt containing 42.\n", encoding="utf-8")
    return TaskSpec.load(root)


@pytest.fixture
def task(tmp_path: Path) -> TaskSpec:
    return make_task(tmp_path / "task")


def run_cli(task: TaskSpec, tmp_path: Path, config: dict, names: list[str], *extra: str) -> Path:
    config_path = tmp_path / "contestants.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    output = tmp_path / "runs"
    code = main(
        [
            "run",
            "--task", str(task.root),
            "--contestants-file", str(config_path),
            "--contestants", *names,
            "--output", str(output),
            *extra,
        ]
    )
    assert code == 0
    folders = [item for item in output.iterdir() if item.is_dir()]
    assert len(folders) == 1
    return folders[0]


def command(code: str, **settings) -> dict:
    return {"adapter": "command", "argv": [sys.executable, "-c", code], **settings}


STUB = {
    "adapter": "stub",
    "model": "model-x",
    "unattended_mode": "bypass permissions",
    "files": {"answer.txt": "42\n"},
    "usage": {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 5,
        "model_calls": 3,
        "cost_usd": 0.5,
        "cost_basis": "estimated",
        "raw": {"provider": "none"},
    },
}


def test_stub_contestant_end_to_end(task, tmp_path):
    run = run_cli(
        task, tmp_path, {"stub-a": STUB, "ghost": None}, ["stub-a", "ghost"], "--context", "sha=abc123"
    )
    summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    assert validate_summary(summary) == []
    assert summary["schema_version"] == 1
    assert summary["task_digest"] == task.digest()
    assert summary["context"] == {"sha": "abc123"}
    assert (run / "summary.md").read_text(encoding="utf-8") == render_summary_markdown(summary)

    record = json.loads((run / "stub-a" / "record.json").read_text(encoding="utf-8"))
    assert validate_record(record) == []
    assert record == summary["contestants"]["stub-a"]
    assert record["status"] == "completed"
    assert record["model"] == "model-x"
    assert record["unattended_mode"] == "bypass permissions"
    assert record["prompt_delivery"] == "file"
    assert record["launch"] == {"confirmed": True, "method": "in_process"}
    assert record["validation"]["score"] == 10
    assert record["validation"]["max_score"] == 10
    assert record["usage"]["input_tokens"] == 100
    assert record["usage"]["tool_calls"] is None
    assert record["usage"]["cost_basis"] == "estimated"
    assert record["artifacts"] == {
        "workspace": "stub-a/workspace",
        "transcript": "stub-a/transcript.log",
        "validation": "stub-a/validation.json",
    }
    assert (run / "stub-a" / "workspace" / "answer.txt").read_text(encoding="utf-8") == "42\n"
    assert load_summary(run) == summary


def test_the_evidence_desk_task_runs_with_the_stub(tmp_path):
    task = TaskSpec.load(REPO / "tasks" / "evidence-desk-v1")
    run = run_cli(task, tmp_path, {"stub-a": {"adapter": "stub"}}, ["stub-a"])
    record = load_summary(run)["contestants"]["stub-a"]
    assert record["status"] == "completed"
    assert isinstance(record["validation"]["score"], (int, float))
    assert record["validation"]["checks"]


def test_a_contestant_gets_nothing_but_its_workspace(task, tmp_path, monkeypatch):
    output = tmp_path / "runs"
    monkeypatch.setenv("LEAK_OUTPUT", str(output / "elsewhere"))
    monkeypatch.setenv("LEAK_LIST", os.pathsep.join([str(task.root / "validator"), "keep-me"]))
    config = {"probe": command(PROBE)}
    run = run_cli(task, tmp_path, config, ["probe"])
    record = load_summary(run)["contestants"]["probe"]
    probe = json.loads((run / "probe" / "workspace" / "probe.json").read_text(encoding="utf-8"))

    forbidden = [task.root.resolve(), run.resolve(), output.resolve()]
    cwd = Path(probe["cwd"]).resolve()
    for root in forbidden:
        assert root not in cwd.parents and cwd != root
        assert cwd not in root.parents
    assert cwd.parent.resolve() == Path(tempfile.gettempdir()).resolve()

    blobs = [probe["cwd"], *probe["argv"], *probe["env"].values()]
    for root in forbidden:
        needles = {str(root).lower(), root.as_posix().lower()}
        for blob in blobs:
            assert not any(needle in blob.lower() for needle in needles), blob
    for name in ("BENCHMARK_TASK_DIR", "BENCHMARK_RUN_DIR"):
        assert name not in probe["env"]
    assert Path(probe["env"]["BENCHMARK_WORKSPACE"]).resolve() == cwd
    assert "LEAK_OUTPUT" not in probe["env"]
    assert "LEAK_LIST" not in probe["env"]
    assert {"LEAK_OUTPUT", "LEAK_LIST"} <= set(record["isolation"]["env_removed"])


def test_an_argument_naming_the_task_folder_is_refused(task, tmp_path):
    config = {"bad": command("pass", argv=[sys.executable, "-c", "pass", str(task.root)])}
    run = run_cli(task, tmp_path, config, ["bad"])
    record = load_summary(run)["contestants"]["bad"]
    assert record["status"] == "launch_failed"
    assert record["validation"]["score"] is None


def test_the_removed_placeholders_are_rejected(tmp_path):
    for token in ("{task_dir}", "{run_dir}"):
        path = tmp_path / "c.json"
        path.write_text(
            json.dumps({"x": {"adapter": "command", "argv": ["tool", token]}}), encoding="utf-8"
        )
        with pytest.raises(ValueError, match="no longer exists"):
            load_contestants(path)


def test_the_validator_never_reaches_the_workspace(task, tmp_path):
    run = run_cli(task, tmp_path, {"a": STUB, "b": command(PROBE)}, ["a", "b"])
    validator_text = (task.root / "validator" / "run.py").read_text(encoding="utf-8")
    for name in ("a", "b"):
        for path in (run / name / "workspace").rglob("*"):
            assert path.name != "validator"
            if path.is_file():
                assert path.read_text(encoding="utf-8", errors="replace") != validator_text
    assert not (task.root / "answer.txt").exists()


def test_a_contestant_that_is_not_configured_has_nothing_recorded(task, tmp_path, capsys):
    run = run_cli(task, tmp_path, {"a": STUB, "ghost": None}, ["a", "ghost"])
    ghost = load_summary(run)["contestants"]["ghost"]
    assert ghost["status"] == "not_configured"
    assert ghost["validation"] == {"score": None, "max_score": None, "checks": None, "error": None}
    assert ghost["wall_seconds"] is None
    assert ghost["exit_status"] is None
    assert all(value is None for key, value in ghost["usage"].items() if key != "raw")
    assert not (run / "ghost").exists()

    capsys.readouterr()
    assert main(["compare", "--run", str(run)]) == 0
    table = capsys.readouterr().out
    row = next(line for line in table.splitlines() if line.startswith("ghost"))
    cells = [cell.strip() for cell in row.split("|")]
    assert cells[0] == "ghost"
    assert all(cell == "n/a" for cell in cells[2:])
    assert "0" not in "".join(cells[1:])


def test_compare_takes_several_runs_and_prints_one_table_per_task(task, tmp_path, capsys):
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    first = run_cli(task, tmp_path / "one", {"a": STUB}, ["a"])
    second = run_cli(task, tmp_path / "two", {"a": STUB}, ["a"])
    capsys.readouterr()
    assert main(["compare", "--run", str(first), "--run", str(second)]) == 0
    table = capsys.readouterr().out
    assert table.count("Task: mini-v1") == 1
    assert table.count("10/10") == 2
    assert "$0.5000 (estimated)" in table


def test_no_absolute_path_is_written_to_a_record(task, tmp_path):
    run = run_cli(task, tmp_path, {"a": STUB, "b": command(PROBE), "ghost": None}, ["a", "b", "ghost"])
    texts = {
        "summary": (run / "summary.json").read_text(encoding="utf-8"),
        "a": (run / "a" / "record.json").read_text(encoding="utf-8"),
        "b": (run / "b" / "record.json").read_text(encoding="utf-8"),
        "validation": (run / "a" / "validation.json").read_text(encoding="utf-8"),
    }
    for name, text in texts.items():
        assert absolute_paths(json.loads(text)) == [], name
        for needle in (str(tmp_path), tmp_path.as_posix(), tempfile.gettempdir()):
            assert needle not in text, (name, needle)
    assert "looked_in" in texts["validation"]


def test_the_transcript_starts_with_the_command_even_when_the_child_prints_at_once(task, tmp_path):
    config = {"loud": command("print('hello from child', flush=True)")}
    run = run_cli(task, tmp_path, config, ["loud"])
    lines = (run / "loud" / "transcript.log").read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("command: ")
    assert lines[1].startswith("started_at: ")
    assert lines.index("hello from child") > 1
    assert any(line.startswith("ended_at: ") for line in lines[lines.index("hello from child"):])


@pytest.mark.parametrize("mode", ["stdin", "file", "argument"])
def test_the_brief_arrives_the_way_the_record_says(task, tmp_path, mode):
    reader = {
        "stdin": "import sys; text = sys.stdin.read()",
        "file": "import sys; text = open(sys.argv[1], encoding='utf-8').read()",
        "argument": "import sys; text = sys.argv[1]",
    }[mode]
    code = reader + "\nopen('got.txt', 'w', encoding='utf-8').write(text)"
    tail = {"stdin": [], "file": ["{prompt_file}"], "argument": ["{prompt}"]}[mode]
    config = {"c": {"adapter": "command", "argv": [sys.executable, "-c", code, *tail], "prompt": mode}}
    run = run_cli(task, tmp_path, config, ["c"])
    record = load_summary(run)["contestants"]["c"]
    assert record["prompt_delivery"] == mode
    assert record["launch"] == {"confirmed": True, "method": "process_started"}
    assert record["exit_status"] == 0
    got = (run / "c" / "workspace" / "got.txt").read_text(encoding="utf-8")
    assert got.strip() == (task.root / "task.md").read_text(encoding="utf-8").strip()


def test_a_contestant_that_runs_out_of_time_is_stopped_and_still_validated(task, tmp_path):
    config = {"slow": command("import time; time.sleep(60)")}
    config_path = tmp_path / "c.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    run = run_benchmark(task, load_contestants(config_path), ["slow"], tmp_path / "runs", 0.03)
    record = load_summary(run)["contestants"]["slow"]
    assert record["status"] == "timed_out"
    assert record["timed_out"] is True
    assert record["exit_status"] is None
    assert record["validation"]["score"] == 0


def test_a_contestant_that_cannot_start_is_recorded_without_a_score(task, tmp_path):
    config = {"gone": {"adapter": "command", "argv": ["no-such-program-anywhere"]}}
    run = run_cli(task, tmp_path, config, ["gone"])
    record = load_summary(run)["contestants"]["gone"]
    assert record["status"] == "launch_failed"
    assert record["launch"]["confirmed"] is False
    assert record["prompt_delivery"] is None
    assert record["error"]
    assert record["validation"]["score"] is None
    assert record["exit_status"] is None


def test_the_task_digest_changes_when_any_task_file_changes(task):
    before = task.digest()
    assert task.digest() == before

    def changed_by(action) -> bool:
        action()
        return task.digest() != before

    root = task.root
    checks = {
        "brief": lambda: (root / "task.md").write_text("another brief", encoding="utf-8"),
        "validator": lambda: (root / "validator" / "run.py").write_text("# other\n", encoding="utf-8"),
        "starter file": lambda: (root / "starter" / "seed.txt").write_text("other", encoding="utf-8"),
        "new file": lambda: (root / "starter" / "extra.txt").write_text("x", encoding="utf-8"),
    }
    unchanged = []
    for name, action in checks.items():
        if not changed_by(action):
            unchanged.append(name)
        task_after = task.digest()
        assert task_after != before or name in unchanged
        (root / "starter" / "extra.txt").unlink(missing_ok=True)
        (root / "task.md").write_text("Write answer.txt containing 42.\n", encoding="utf-8")
        (root / "validator" / "run.py").write_text(VALIDATOR, encoding="utf-8")
        (root / "starter" / "seed.txt").write_text("seed", encoding="utf-8")
    assert unchanged == []
    assert task.digest() == before


def test_the_task_digest_ignores_build_caches_and_cannot_be_confused_by_file_boundaries(task):
    before = task.digest()
    cache = task.root / "validator" / "__pycache__"
    cache.mkdir()
    (cache / "run.cpython-312.pyc").write_bytes(b"cache")
    assert task.digest() == before
    (task.root / "starter" / "ab").write_bytes(b"c")
    first = task.digest()
    (task.root / "starter" / "ab").unlink()
    (task.root / "starter" / "a").write_bytes(b"bc")
    assert task.digest() != first


BAD_MANIFESTS = {
    "no task_id": '{"title": "T"}',
    "task_id not text": '{"task_id": 5}',
    "task_id with a slash": '{"task_id": "a/b"}',
    "limit as text": '{"task_id": "t", "time_limit_minutes": "30"}',
    "limit true": '{"task_id": "t", "time_limit_minutes": true}',
    "limit zero": '{"task_id": "t", "time_limit_minutes": 0}',
    "validator limit negative": '{"task_id": "t", "validator_timeout_seconds": -1}',
    "network a list": '{"task_id": "t", "network": []}',
    "flag as text": '{"task_id": "t", "network": {"research_allowed": "yes"}}',
    "title a number": '{"task_id": "t", "title": 3}',
    "an array": "[]",
    "not JSON": "{oops",
}


def test_a_task_manifest_with_a_missing_key_or_wrong_type_is_refused_with_a_message(tmp_path, capsys):
    wrong = []
    for name, text in BAD_MANIFESTS.items():
        root = tmp_path / name.replace(" ", "_")
        make_task(root)
        (root / "task.json").write_text(text, encoding="utf-8")
        try:
            TaskSpec.load(root)
            wrong.append(f"{name}: accepted")
        except ValueError as error:
            if not str(error):
                wrong.append(f"{name}: no message")
        except Exception as error:
            wrong.append(f"{name}: {type(error).__name__}")
    assert wrong == []
    capsys.readouterr()
    assert main(["validate-task", "--task", str(tmp_path / "no_task_id")]) == 2
    assert capsys.readouterr().err.startswith("error: ")

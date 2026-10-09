"""The Evidence Desk validator loses exactly the checks a defect in the project breaks, and no others.

Every scored check has a case where the reference project with one defect
loses that check. Projects laid out differently but still correct (a src
folder, pytest-style tests) keep full marks. An application that prints
nonsense, hangs, or leaves a child process running still gets a result file
inside the validator's time limit, and nothing is left running afterwards.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "reference"))

from harness import (  # noqa: E402
    TASKS,
    ScenarioRuns,
    clone,
    failed,
    kill_processes_mentioning,
    make_workspace,
    processes_mentioning,
    run_validator,
    why,
)

TASK = "evidence-desk-v1"
CHECKS = list(json.loads((TASKS / TASK / "scoring.json").read_text(encoding="utf-8"))["checks"])
TIME_LIMIT = json.loads((TASKS / TASK / "task.json").read_text(encoding="utf-8"))["validator_timeout_seconds"]
GRANDCHILD = f"evidence-grandchild-{uuid.uuid4().hex}"
APP_CHECKS = {"valid_input", "filters", "outputs", "source_urls", "sorting", "malformed_input", "empty_results"}


def edit(workspace: Path, relative: str, old: str, new: str) -> None:
    path = workspace / relative
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"{relative} must hold exactly one: {old!r}"
    path.write_text(text.replace(old, new), encoding="utf-8")


def edit_all(workspace: Path, relative: str, old: str, new: str) -> None:
    path = workspace / relative
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{relative} must hold {old!r}"
    path.write_text(text.replace(old, new), encoding="utf-8")


def replace_file(workspace: Path, relative: str, text: str) -> None:
    (workspace / relative).write_text(text, encoding="utf-8")


def delete(workspace: Path, relative: str) -> None:
    path = workspace / relative
    shutil.rmtree(path) if path.is_dir() else path.unlink()


def move_to_src(workspace: Path) -> None:
    (workspace / "src").mkdir()
    shutil.move(str(workspace / "evidence_desk"), str(workspace / "src" / "evidence_desk"))


SORT_LINE = 'return sorted(chosen, key=lambda item: (-item["priority"], item["id"]))'
PYTEST_STYLE_TESTS = '''from evidence_desk.core import priority, select

RECORD = {"id": "A-1", "status": "open", "severity": "high", "category": "maintenance", "title": "t",
          "summary": "s", "source_url": "https://example.com/1", "severity_weight": 3}


def test_priority_adds_open_and_maintenance():
    assert priority(RECORD) == 5


def test_filters_combine():
    assert [r["id"] for r in select([RECORD], status="open", severity="high")] == ["A-1"]
    assert select([RECORD], status="closed") == []
'''
JSON_WITHOUT_IDS = '''def to_json(records: list[dict]) -> str:
    return json.dumps({"results": [{"record": r, "priority": r["priority"]} for r in records]}) + "\\n"'''
SPAWN_GRANDCHILD = (
    "import subprocess\nimport sys\n\n"
    f'subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)  # {GRANDCHILD}"])\n'
)

# name -> (change to the reference project, checks it must lose). "tests" is also lost whenever the
# project's own unit tests cover the broken behaviour, which is listed rather than hidden.
DEFECTS = {
    "no research notes": (lambda ws: delete(ws, "RESEARCH_NOTES.md"), {"required_files", "research"}),
    "no README": (lambda ws: delete(ws, "README.md"), {"required_files", "readme"}),
    "no tests folder": (lambda ws: delete(ws, "tests"), {"required_files", "tests"}),
    "a README without the run command": (lambda ws: edit_all(ws, "README.md", "python -m evidence_desk", "the module"), {"readme"}),
    "a README without install instructions": (
        lambda ws: replace_file(ws, "README.md", "# Evidence Desk\n\nRun python -m evidence_desk. Run the tests with unittest.\n"),
        {"readme"},
    ),
    "a README without test instructions": (
        lambda ws: replace_file(ws, "README.md", "# Evidence Desk\n\nInstall nothing. Run python -m evidence_desk.\n"),
        {"readme"},
    ),
    "research notes with one address": (lambda ws: edit(ws, "RESEARCH_NOTES.md", "https://docs.python.org/3/library/json.html", "the json page"), {"research"}),
    "research notes with addresses that are not the Python documentation": (
        lambda ws: replace_file(ws, "RESEARCH_NOTES.md", "https://example.com/a https://example.com/b https://docs.python.org.example.com/c"),
        {"research"},
    ),
    "a network call in the application": (
        lambda ws: edit(ws, "evidence_desk/core.py", "import json\n", "import json\nimport urllib.request\n\nurllib.request.urlopen('https://example.com')\n"),
        {"offline"},
    ),
    "a network call in a test file is not the application": (
        lambda ws: edit(ws, "tests/test_evidence_desk.py", "import json\n", "import json\n\n# requests.get and urllib.request.urlopen are named here on purpose\n"),
        set(),
    ),
    "a network call named in a helper called test_helpers": (
        lambda ws: replace_file(ws, "evidence_desk/test_helpers.py", "CALL = 'requests.get'\n"),
        set(),
    ),
    "a network call named in a file ending in _test": (
        lambda ws: replace_file(ws, "evidence_desk/mock_test.py", "CALL = 'httpx.get'\n"),
        set(),
    ),
    "a network call in an environment folder is not the application": (
        lambda ws: (ws / ".venv" / "lib").mkdir(parents=True) or replace_file(ws, ".venv/lib/x.py", "CALL = 'requests.get'\n"),
        set(),
    ),
    "a folder named like a source file": (lambda ws: (ws / "data.py").mkdir(), set()),
    "the JSON report holds no list of records": (
        lambda ws: edit(ws, "evidence_desk/core.py", 'return json.dumps(records, indent=2) + "\\n"', 'return json.dumps({"count": len(records)}) + "\\n"'),
        APP_CHECKS,
    ),
    "the JSON report holds records without ids": (
        lambda ws: edit(ws, "evidence_desk/core.py", 'def to_json(records: list[dict]) -> str:\n    return json.dumps(records, indent=2) + "\\n"', JSON_WITHOUT_IDS),
        {"valid_input", "filters", "sorting", "source_urls", "malformed_input", "empty_results"},
    ),
    "the severity filter is ignored": (
        lambda ws: edit(ws, "evidence_desk/core.py", 'if severity and record["severity"] != severity:', "if False:"),
        {"filters", "empty_results", "tests"},
    ),
    "the filters are joined with or": (
        lambda ws: edit(
            ws,
            "evidence_desk/core.py",
            '        if status and record["status"] != status:\n            continue\n        if severity and record["severity"] != severity:\n            continue\n        if category and record["category"] != category:\n            continue\n',
            '        wanted = [(status, "status"), (severity, "severity"), (category, "category")]\n        wanted = [(value, key) for value, key in wanted if value]\n        if wanted and not any(record[key] == value for value, key in wanted):\n            continue\n',
        ),
        {"filters", "outputs", "empty_results", "tests"},
    ),
    "equal priorities sorted by descending id": (
        lambda ws: edit(ws, "evidence_desk/core.py", SORT_LINE, 'return sorted(sorted(chosen, key=lambda item: item["id"], reverse=True), key=lambda item: -item["priority"])'),
        {"sorting", "tests"},
    ),
    "lowest priority first": (
        lambda ws: edit(ws, "evidence_desk/core.py", SORT_LINE, 'return sorted(chosen, key=lambda item: (item["priority"], item["id"]))'),
        {"sorting", "tests"},
    ),
    "the Markdown report shows neither ids nor titles": (
        lambda ws: edit(ws, "evidence_desk/core.py", '''f"## {record['id']}: {record['title']}",''', '"## Record",'),
        {"outputs"},
    ),
    "the JSON report rewrites source addresses": (
        lambda ws: edit(ws, "evidence_desk/core.py", 'return json.dumps(records, indent=2) + "\\n"', 'return json.dumps([{**r, "source_url": r["source_url"].replace("https://", "http://")} for r in records], indent=2) + "\\n"'),
        {"source_urls"},
    ),
    "the Markdown report leaves out source addresses": (
        lambda ws: edit(ws, "evidence_desk/core.py", '''f"- Source: {record['source_url']}",''', '"- Source: see the data",'),
        {"source_urls", "tests"},
    ),
    "bad input exits with status zero": (
        lambda ws: edit(ws, "evidence_desk/__main__.py", "sys.exit(main())", "main()\nsys.exit(0)"),
        {"malformed_input"},
    ),
    "bad input exits non-zero without a word": (
        lambda ws: edit(ws, "evidence_desk/cli.py", 'print(f"error: {error}", file=sys.stderr)', "pass"),
        {"malformed_input"},
    ),
    "a record without a title is accepted": (
        lambda ws: edit(ws, "evidence_desk/core.py", "    for field in REQUIRED:", "    for field in ():"),
        {"malformed_input", "tests"},
    ),
    "an empty JSON report is an empty file": (
        lambda ws: edit(ws, "evidence_desk/core.py", 'def to_json(records: list[dict]) -> str:\n', 'def to_json(records: list[dict]) -> str:\n    if not records:\n        return ""\n'),
        {"empty_results"},
    ),
    "an empty Markdown report says nothing": (
        lambda ws: (
            edit(ws, "evidence_desk/core.py", '        lines.append("No records match the filters.")\n        return "\\n".join(lines) + "\\n"', '        return ""'),
            edit(ws, "evidence_desk/cli.py", 'print("No records match the filters. The report is empty.")', "pass"),
        ),
        {"empty_results", "tests"},
    ),
    "a filter that matches nothing is an error": (
        lambda ws: edit(ws, "evidence_desk/__main__.py", "sys.exit(main())", "code = main()\nsys.exit(3 if {'closed', 'low'} <= set(sys.argv) else code)"),
        {"empty_results"},
    ),
    "a test that fails": (
        lambda ws: edit(ws, "tests/test_evidence_desk.py", "    def test_priority_adds_open_and_maintenance(self):\n", "    def test_priority_adds_open_and_maintenance(self):\n        self.fail('broken')\n"),
        {"tests"},
    ),
    "a test file that holds no tests": (lambda ws: replace_file(ws, "tests/test_evidence_desk.py", "VALUE = 1\n"), {"tests"}),
    "pytest-style tests that fail": (
        lambda ws: replace_file(ws, "tests/test_evidence_desk.py", PYTEST_STYLE_TESTS + "\n\ndef test_broken():\n    assert False\n"),
        {"tests"},
    ),
    "tests that never finish": (
        lambda ws: edit(ws, "tests/test_evidence_desk.py", "    def test_priority_adds_open_and_maintenance(self):\n", "    def test_priority_adds_open_and_maintenance(self):\n        import time\n        time.sleep(1000)\n"),
        {"tests"},
    ),
    "tests that break the import": (
        lambda ws: edit(ws, "tests/test_evidence_desk.py", "import json\n", "import json\nimport a_module_that_does_not_exist\n"),
        {"tests"},
    ),
}

# Projects that are laid out or tested differently and still deserve every point.
CORRECT = {
    "the package under a src folder": move_to_src,
    "pytest-style tests": lambda ws: replace_file(ws, "tests/test_evidence_desk.py", PYTEST_STYLE_TESTS),
    "a package under src and pytest-style tests": lambda ws: (move_to_src(ws), replace_file(ws, "tests/test_evidence_desk.py", PYTEST_STYLE_TESTS)),
}

# Applications that misbehave in ways that cost time or leave processes behind.
LEFTOVERS = {
    "an application that starts a child and leaves it running": lambda ws: edit(ws, "evidence_desk/__main__.py", "import sys\n", SPAWN_GRANDCHILD),
    "an application that never finishes": lambda ws: edit(ws, "evidence_desk/__main__.py", "sys.exit(main())", "import time\ntime.sleep(1000)\nsys.exit(main())"),
    "an application that never finishes and has a child": lambda ws: edit(
        ws, "evidence_desk/__main__.py", "import sys\n", SPAWN_GRANDCHILD + "import time\n\ntime.sleep(1000)\n"
    ),
}


def scenario_builder(reference: Path, change):
    def build(folder: Path):
        workspace = clone(reference, folder)
        change(workspace)
        return workspace, None

    return build


@pytest.fixture(scope="module")
def reference(tmp_path_factory):
    return make_workspace(TASK, tmp_path_factory.mktemp("evidence-reference"), solved=True)


@pytest.fixture(scope="module")
def runs(reference, tmp_path_factory):
    scenarios = {}
    for prefix, group in (("defect", DEFECTS), ("correct", CORRECT), ("leftover", LEFTOVERS)):
        for name, entry in group.items():
            change = entry[0] if isinstance(entry, tuple) else entry
            scenarios[f"{prefix}: {name}"] = scenario_builder(reference, change)
    holder = ScenarioRuns(TASK, tmp_path_factory.mktemp("evidence-runs"), scenarios)
    try:
        yield holder
    finally:
        holder.close()
        kill_processes_mentioning(GRANDCHILD)


@pytest.mark.parametrize("name", sorted(DEFECTS))
def test_a_defect_loses_the_check_it_breaks(name, runs):
    result = runs.result(f"defect: {name}")
    assert failed(result) == DEFECTS[name][1], why(result)


@pytest.mark.parametrize("name", sorted(CORRECT))
def test_a_correct_project_in_another_layout_keeps_full_marks(name, runs):
    result = runs.result(f"correct: {name}")
    assert failed(result) == set(), why(result)
    assert result["score"] == result["max_score"] == 100


def test_every_scored_check_has_a_defect_that_loses_it():
    covered = set().union(*(expected for _, expected in DEFECTS.values()))
    assert covered == set(CHECKS), sorted(set(CHECKS) - covered)


# ------------------------------------------------------------ time and leftovers


def test_a_child_left_running_by_the_application_neither_hangs_the_validator_nor_survives_it(runs):
    name = "leftover: an application that starts a child and leaves it running"
    process, result = runs.get(name)
    assert result is not None, process.stderr
    assert failed(result) == set(), why(result)
    assert process.elapsed < TIME_LIMIT / 2, process.elapsed
    runs.finish()
    assert processes_mentioning(GRANDCHILD) == []


def test_an_application_that_never_finishes_still_gets_a_result_inside_the_time_limit(runs):
    process, result = runs.get("leftover: an application that never finishes")
    assert result is not None, process.stderr
    assert process.elapsed < TIME_LIMIT * 0.75, process.elapsed
    assert failed(result) == APP_CHECKS, why(result)
    assert result["checks"]["tests"] and result["checks"]["offline"] and result["checks"]["required_files"]


def test_an_application_that_never_finishes_and_has_a_child_leaves_nothing_running(runs):
    process, result = runs.get("leftover: an application that never finishes and has a child")
    assert result is not None, process.stderr
    assert process.elapsed < TIME_LIMIT * 0.75, process.elapsed
    assert failed(result) == APP_CHECKS, why(result)
    runs.finish()
    assert processes_mentioning(GRANDCHILD) == []


def test_tests_that_never_finish_are_stopped_inside_the_time_limit(runs):
    process, result = runs.get("defect: tests that never finish")
    assert process.elapsed < TIME_LIMIT * 0.75, process.elapsed
    assert result["checks"]["tests"] is False


# ------------------------------------------------------------ workspaces that are not projects


def test_an_empty_workspace_scores_nothing_but_gets_a_result(tmp_path):
    (tmp_path / "empty").mkdir()
    process, result = run_validator(TASK, tmp_path / "empty", tmp_path)
    assert result["score"] == 0 and result["max_score"] == 100


def test_an_application_with_no_package_but_a_single_module_is_found(reference, tmp_path):
    workspace = clone(reference, tmp_path)
    package = workspace / "evidence_desk"
    (workspace / "evidence_desk.py").write_text("print('hello')\n", encoding="utf-8")
    shutil.rmtree(package)
    process, result = run_validator(TASK, workspace, tmp_path)
    assert result["checks"]["required_files"] is True
    assert result["checks"]["valid_input"] is False

"""Validator for evidence-desk-v1.

Usage: python validator/run.py --workspace <dir> --output <json>

Runs the contestant's command line application on fixtures the validator
writes itself, so a changed or deleted data file in the workspace cannot help
or hurt. Every check needs positive evidence from a working application, so a
workspace that only holds the starter files scores nothing.

Everything the validator starts writes to files rather than pipes, has a
timeout, and is started so that its whole process tree can be stopped: an
application that hangs, or leaves a child running, cannot hang the validator.
The result file is always written.
"""
from __future__ import annotations

import argparse
from collections import OrderedDict
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent

FIXTURE = [
    {
        "id": "F-001",
        "status": "open",
        "severity": "high",
        "category": "maintenance",
        "title": "Bearing temperature trend",
        "summary": "Temperature has increased across three inspection cycles.",
        "source_url": "https://example.com/evidence/F-001",
        "severity_weight": 3,
    },
    {
        "id": "F-002",
        "status": "open",
        "severity": "medium",
        "category": "reliability",
        "title": "Intermittent sensor dropout",
        "summary": "The sensor missed two readings during a controlled test.",
        "source_url": "https://example.com/evidence/F-002",
        "severity_weight": 2,
    },
    {
        "id": "F-003",
        "status": "closed",
        "severity": "high",
        "category": "maintenance",
        "title": "Resolved lubrication alert",
        "summary": "The alert was resolved and the follow-up inspection passed.",
        "source_url": "https://example.com/evidence/F-003",
        "severity_weight": 3,
    },
    {
        "id": "F-004",
        "status": "open",
        "severity": "low",
        "category": "safety",
        "title": "Guard label faded",
        "summary": "The warning label is readable but should be replaced.",
        "source_url": "https://example.com/evidence/F-004",
        "severity_weight": 1,
    },
]
# priority = weight, +1 if open, +1 if maintenance: F-001 5, F-003 4, F-002 3, F-004 2
FULL_ORDER = ["F-001", "F-003", "F-002", "F-004"]

TIE_FIXTURE = [
    {
        "id": "T-3",
        "status": "closed",
        "severity": "medium",
        "category": "safety",
        "title": "Third tied record",
        "summary": "Priority 2.",
        "source_url": "https://example.com/evidence/T-3",
        "severity_weight": 2,
    },
    {
        "id": "T-1",
        "status": "closed",
        "severity": "medium",
        "category": "safety",
        "title": "First tied record",
        "summary": "Priority 2.",
        "source_url": "https://example.com/evidence/T-1",
        "severity_weight": 2,
    },
    {
        "id": "T-2",
        "status": "closed",
        "severity": "high",
        "category": "safety",
        "title": "Highest priority record",
        "summary": "Priority 3.",
        "source_url": "https://example.com/evidence/T-2",
        "severity_weight": 3,
    },
    {
        "id": "T-0",
        "status": "closed",
        "severity": "medium",
        "category": "safety",
        "title": "Zeroth tied record",
        "summary": "Priority 2.",
        "source_url": "https://example.com/evidence/T-0",
        "severity_weight": 2,
    },
]
TIE_ORDER = ["T-2", "T-0", "T-1", "T-3"]

NETWORK_TOKENS = ("requests.get", "httpx.get", "urllib.request.urlopen", "socket.create_connection")
SKIPPED_DIRS = {".git", ".venv", "venv", "site-packages", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache"}
TEST_DIRS = {"tests", "test"}
MAX_READ_BYTES = 2_000_000

# Time budget, well inside validator_timeout_seconds in task.json (180).
APP_CALL_SECONDS = 20  # one run of the application
APP_BUDGET_SECONDS = 100  # all runs of the application together
TEST_RUN_SECONDS = 60  # one run of the contestant's tests
TOTAL_BUDGET_SECONDS = 160  # the whole validator
HUNG_RUNS_ALLOWED = 2  # after this many timeouts in a row the application is treated as hung


# ------------------------------------------------------------ process control


class ProcessTree:
    """Starts a command so that it and everything it starts can be stopped together."""

    def __init__(self):
        self.process = None
        self.job = None
        self.kernel32 = None

    def start(self, command, cwd, env, stdout, stderr):
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        self.process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, **kwargs)
        if os.name == "nt":
            self._join_job()
        return self.process

    def _join_job(self):
        # A job object that kills its members when closed also reaches processes whose parent has already gone.
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

            class IoCounters(ctypes.Structure):
                _fields_ = [(name, ctypes.c_ulonglong) for name in ("a", "b", "c", "d", "e", "f")]

            class Basic(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class Extended(ctypes.Structure):
                _fields_ = [
                    ("Basic", Basic),
                    ("Io", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                return
            info = Extended()
            info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
                kernel32.CloseHandle(job)
                return
            if not kernel32.AssignProcessToJobObject(job, int(self.process._handle)):
                kernel32.CloseHandle(job)
                return
            self.kernel32, self.job = kernel32, job
        except Exception:
            self.job = None

    def stop(self):
        """Stop the process and everything it started. Safe to call twice."""
        process = self.process
        if process is not None:
            try:
                if os.name == "nt":
                    if process.poll() is None:
                        subprocess.run(
                            ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            stdin=subprocess.DEVNULL,
                            timeout=20,
                            check=False,
                        )
                else:
                    import signal

                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
            except Exception:
                pass
            try:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)
            except Exception:
                pass
        if self.job is not None:
            try:
                self.kernel32.CloseHandle(self.job)
            except Exception:
                pass
            self.job = None


class Completed:
    def __init__(self, returncode: int, stdout: str, stderr: str):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def read_capped(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            return handle.read(MAX_READ_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return ""


class Runner:
    """Runs commands in the workspace under a time budget. Output goes to files, never pipes."""

    def __init__(self, workspace: Path, scratch: Path, started: float):
        self.workspace = workspace
        self.scratch = scratch
        self.started = started
        roots = [str(workspace)]
        if (workspace / "src").is_dir():
            roots.append(str(workspace / "src"))
        self.env = {**os.environ, "PYTHONPATH": os.pathsep.join(roots), "PYTHONDONTWRITEBYTECODE": "1"}
        self.runs = 0
        self.app_started = time.monotonic()
        self.timeouts_in_a_row = 0

    def remaining(self, limit: float) -> float:
        return limit - (time.monotonic() - self.started)

    def execute(self, command, timeout: float):
        """Return a Completed, or None when the command timed out or there was no time left to run it."""
        if timeout <= 0.5:
            return None
        self.runs += 1
        out_path, err_path = self.scratch / f"run-{self.runs}.out", self.scratch / f"run-{self.runs}.err"
        tree = ProcessTree()
        timed_out = False
        with out_path.open("wb") as out, err_path.open("wb") as err:
            try:
                process = tree.start(command, self.workspace, self.env, out, err)
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
            except OSError:
                return None
            finally:
                tree.stop()
        if timed_out:
            return None
        return Completed(process.returncode, read_capped(out_path), read_capped(err_path))

    def app(self, args):
        """Run the application. After repeated timeouts it is treated as hung and not started again."""
        if self.timeouts_in_a_row >= HUNG_RUNS_ALLOWED:
            return None
        timeout = min(APP_CALL_SECONDS, APP_BUDGET_SECONDS - (time.monotonic() - self.app_started), self.remaining(TOTAL_BUDGET_SECONDS))
        done = self.execute([sys.executable, "-m", "evidence_desk", *args], timeout)
        self.timeouts_in_a_row = 0 if done is not None else self.timeouts_in_a_row + 1
        return done


# --------------------------------------------------------------------- reading


class App:
    """Runs the contestant's application on fixtures."""

    def __init__(self, runner: Runner, temp: Path):
        self.runner = runner
        self.temp = temp
        self.counter = 0

    def fixture(self, records, name: str) -> Path:
        path = self.temp / name
        path.write_text(json.dumps(records, indent=2), encoding="utf-8")
        return path

    def report(self, input_path: Path, fmt: str, extra: list[str] | None = None):
        """Return (process, report text or None)."""
        self.counter += 1
        output = self.temp / f"report-{self.counter}.{'json' if fmt == 'json' else 'md'}"
        process = self.runner.app(["--input", str(input_path), *(extra or []), "--format", fmt, "--output", str(output)])
        text = read_capped(output) if output.is_file() else None
        return process, text


def extract_records(parsed):
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for key in ("records", "results", "findings", "items", "data"):
            if isinstance(parsed.get(key), list):
                return parsed[key]
        lists = [value for value in parsed.values() if isinstance(value, list)]
        if len(lists) == 1:
            return lists[0]
    return None


def parse_records(text):
    """Return the list of record dicts in a JSON report, or None when it holds none."""
    try:
        records = extract_records(json.loads(text))
    except Exception:  # not JSON, or nested too deeply to read
        return None
    if records is None or not all(isinstance(item, dict) for item in records):
        return None
    return records


def json_records(app: App, input_path: Path, extra: list[str] | None = None):
    """Return the list of record dicts a JSON run wrote, or None when the run failed."""
    process, text = app.report(input_path, "json", extra)
    if process is None or process.returncode != 0 or text is None:
        return None
    return parse_records(text)


def ids_of(records):
    return None if records is None else [item.get("id") for item in records]


def same_ids(found, wanted) -> bool:
    """True when found is a list of ids equal to wanted, whatever else the application put in the list."""
    return isinstance(found, list) and len(found) == len(wanted) and all(isinstance(i, str) for i in found) and found == wanted


def same_id_set(found, wanted) -> bool:
    return isinstance(found, list) and all(isinstance(i, str) for i in found) and sorted(found) == sorted(wanted)


def python_files(root: Path):
    """Regular .py files under root, not entering environments or caches."""
    for folder, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in SKIPPED_DIRS]
        for name in files:
            path = Path(folder) / name
            if name.endswith(".py") and path.is_file() and not path.is_symlink():
                yield path


def is_test_file(path: Path, workspace: Path) -> bool:
    try:
        parts = path.relative_to(workspace).parts
    except ValueError:
        return False
    name = path.name
    return bool(TEST_DIRS & set(parts[:-1])) or name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"


def implementation_present(workspace: Path) -> bool:
    for base in (workspace, workspace / "src"):
        package = base / "evidence_desk"
        if package.is_dir() and any(True for _ in python_files(package)):
            return True
        if (base / "evidence_desk.py").is_file():
            return True
    return False


def source_text(workspace: Path) -> str:
    parts = []
    for path in python_files(workspace):
        if is_test_file(path, workspace):
            continue
        try:
            parts.append(read_capped(path))
        except OSError:
            continue
    return "\n".join(parts)


def score_weights() -> OrderedDict:
    scoring = json.loads((HERE.parent / "scoring.json").read_text(encoding="utf-8"))
    return OrderedDict(scoring["checks"])


def write_json(output: Path, result: dict) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(result, indent=2)
    partial = output.with_name(output.name + ".part")
    partial.write_text(text, encoding="utf-8")
    os.replace(partial, output)
    print(text)


def write_result(output: Path, weights, checks, details) -> int:
    points = {name: (weights[name] if checks.get(name) else 0) for name in weights}
    result = {
        "score": sum(points.values()),
        "max_score": sum(weights.values()),
        "checks": {name: bool(checks.get(name)) for name in weights},
        "points": points,
        "details": {key: str(value) for key, value in details.items() if value},
    }
    write_json(output, result)
    return 0 if result["score"] == result["max_score"] else 1


def write_environment_error(output: Path, weights, message: str) -> int:
    write_json(output, {"score": None, "max_score": sum(weights.values()) if weights else None, "checks": {}, "error": message})
    return 2


def remove_tree(path: Path) -> None:
    for _ in range(10):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        time.sleep(0.5)


def run_tests(runner: Runner, details: dict) -> bool:
    """Run the contestant's tests: unittest discovery first, then pytest when unittest found none."""
    limit = min(TEST_RUN_SECONDS, runner.remaining(TOTAL_BUDGET_SECONDS))
    done = runner.execute([sys.executable, "-m", "unittest", "discover", "-s", "tests"], limit)
    if done is None:
        details["test_output"] = "the tests did not finish in time, or could not be started"
        return False
    output = done.stdout + done.stderr
    ran = re.search(r"Ran (\d+) tests?", output)
    if ran is not None and int(ran.group(1)) >= 1:
        details["test_output"] = output[-1500:]
        return done.returncode == 0
    limit = min(TEST_RUN_SECONDS, runner.remaining(TOTAL_BUDGET_SECONDS))
    pytest_done = runner.execute([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"], limit)
    if pytest_done is None:
        details["test_output"] = "unittest found no tests; pytest did not finish in time. " + output[-600:]
        return False
    pytest_output = pytest_done.stdout + pytest_done.stderr
    details["test_output"] = pytest_output[-1500:]
    return pytest_done.returncode == 0 and re.search(r"\b[1-9]\d* passed", pytest_output) is not None


def validate(workspace: Path, checks: dict, details: dict) -> None:
    started = time.monotonic()
    present = implementation_present(workspace)
    tests_dir = workspace / "tests"
    has_tests = tests_dir.is_dir() and any(True for _ in python_files(tests_dir))
    checks["required_files"] = bool(
        present and (workspace / "README.md").is_file() and (workspace / "RESEARCH_NOTES.md").is_file() and has_tests
    )
    if not checks["required_files"]:
        details["required_files"] = (
            f"implementation={present}, README.md={(workspace / 'README.md').is_file()}, "
            f"RESEARCH_NOTES.md={(workspace / 'RESEARCH_NOTES.md').is_file()}, tests={has_tests}"
        )

    checks["offline"] = present and not any(token in source_text(workspace) for token in NETWORK_TOKENS)

    notes = workspace / "RESEARCH_NOTES.md"
    if notes.is_file():
        found = re.findall(r"https://docs\.python\.org/[^\s)>\]\"']+", read_capped(notes))
        urls = {url.rstrip(".,;:") for url in found}
        checks["research"] = len(urls) >= 2
        details["research"] = f"{len(urls)} distinct docs.python.org URLs"
    readme = workspace / "README.md"
    if readme.is_file():
        text = read_capped(readme).lower()
        checks["readme"] = all(word in text for word in ("install", "test", "python -m evidence_desk"))

    if not present:
        return
    scratch = Path(tempfile.mkdtemp(prefix="bench-evidence-"))
    try:
        runner = Runner(workspace, scratch, started)
        app = App(runner, scratch)
        fixture = app.fixture(FIXTURE, "findings.json")
        source_urls = {item["id"]: item["source_url"] for item in FIXTURE}

        full = json_records(app, fixture)
        full_ids = ids_of(full)
        checks["valid_input"] = same_id_set(full_ids, FULL_ORDER)
        if not checks["valid_input"]:
            details["valid_input"] = f"a JSON report of the four supplied records was not produced (ids found: {full_ids})"

        cases = [
            (["--status", "open", "--severity", "high", "--category", "maintenance"], ["F-001"]),
            (["--status", "closed"], ["F-003"]),
            (["--category", "reliability"], ["F-002"]),
            (["--severity", "low"], ["F-004"]),
            (["--status", "open", "--severity", "medium"], ["F-002"]),
        ]
        failed = []
        combined_records = None
        for extra, expected in cases:
            records = json_records(app, fixture, extra)
            if "--category" in extra and "--severity" in extra:
                combined_records = records
            if not same_ids(ids_of(records), expected):
                failed.append(f"{' '.join(extra)} gave {ids_of(records)}, expected {expected}")
        checks["filters"] = not failed
        if failed:
            details["filters"] = "; ".join(failed)

        ties = json_records(app, app.fixture(TIE_FIXTURE, "ties.json"))
        checks["sorting"] = same_ids(full_ids, FULL_ORDER) and same_ids(ids_of(ties), TIE_ORDER)
        if not checks["sorting"]:
            details["sorting"] = f"full order {full_ids}, tie order {ids_of(ties)}"

        markdown_process, markdown_text = app.report(fixture, "markdown")
        markdown_ok = markdown_process is not None and markdown_process.returncode == 0 and markdown_text is not None
        # A record's id is part of its source address, so the address is taken out before looking for the id.
        shown = re.sub(r"https?://\S+", " ", markdown_text) if markdown_text else ""
        checks["outputs"] = bool(
            markdown_ok
            and all(item["id"] in shown or item["title"] in shown for item in FIXTURE)
            and combined_records is not None
            and len(combined_records) == 1
        )

        def url_kept(item) -> bool:
            identifier = item.get("id")
            return isinstance(identifier, str) and item.get("source_url") == source_urls.get(identifier)

        checks["source_urls"] = bool(
            full is not None
            and len(full) == len(FIXTURE)
            and all(url_kept(item) for item in full)
            and markdown_ok
            and all(url in markdown_text for url in source_urls.values())
        )

        malformed = scratch / "malformed.json"
        malformed.write_text("{bad", encoding="utf-8")
        missing_field = [{key: value for key, value in FIXTURE[0].items() if key != "title"}]
        rejected = True
        for path in (malformed, app.fixture(missing_field, "missing-field.json")):
            process, _ = app.report(path, "json")
            message = "" if process is None else process.stderr + process.stdout
            if process is None or process.returncode == 0 or not message.strip() or "No module named" in message:
                rejected = False
        checks["malformed_input"] = bool(checks["valid_input"] and rejected)
        if checks["valid_input"] and not rejected:
            details["malformed_input"] = "bad input must exit non-zero with an explanation"

        empty_extra = ["--status", "closed", "--severity", "low"]
        process_json, text_json = app.report(fixture, "json", empty_extra)
        empty_records = None
        if process_json is not None and process_json.returncode == 0 and text_json is not None:
            empty_records = parse_records(text_json)
        process_md, text_md = app.report(fixture, "markdown", empty_extra)
        checks["empty_results"] = bool(
            checks["valid_input"]
            and empty_records == []
            and process_md is not None
            and process_md.returncode == 0
            and text_md is not None
            and bool(text_md.strip() or process_md.stdout.strip())
        )

        if has_tests:
            checks["tests"] = run_tests(runner, details)
    finally:
        remove_tree(scratch)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    weights = None
    checks: dict = {}
    details: dict = {}
    try:
        weights = score_weights()
        validate(workspace, checks, details)
    except Exception as error:  # a validator bug or an odd workspace still leaves a result holding what was scored
        details["validator"] = f"stopped early: {type(error).__name__}: {str(error)[:300]}"
        if weights is None:
            return write_environment_error(args.output, weights, details["validator"])
    return write_result(args.output, weights, checks, details)


if __name__ == "__main__":
    raise SystemExit(main())

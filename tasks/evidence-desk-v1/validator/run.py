from __future__ import annotations

import argparse
from collections import OrderedDict
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile


def run(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True, check=False, timeout=90)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    fixture = workspace / "data" / "findings.json"
    checks: OrderedDict[str, bool] = OrderedDict()
    details: dict[str, str] = {}
    checks["required_files"] = all(
        (workspace / name).exists()
        for name in ("README.md", "RESEARCH_NOTES.md", "tests")
    )
    package_exists = (workspace / "evidence_desk").is_dir() or (workspace / "evidence_desk.py").is_file()
    checks["implementation_present"] = package_exists
    if not fixture.is_file():
        checks["valid_input"] = False
        details["valid_input"] = "fixture missing"
        return write_result(args.output, checks, details)
    try:
        source_records = json.loads(fixture.read_text(encoding="utf-8"))
        checks["valid_input"] = isinstance(source_records, list) and len(source_records) == 4
    except json.JSONDecodeError as error:
        checks["valid_input"] = False
        details["valid_input"] = str(error)
        source_records = []
    env = {**dict(), **__import__("os").environ, "PYTHONPATH": str(workspace)}
    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        json_report = temp / "report.json"
        markdown_report = temp / "report.md"
        command_json = [
            sys.executable,
            "-m",
            "evidence_desk",
            "--input",
            str(fixture),
            "--status",
            "open",
            "--severity",
            "high",
            "--category",
            "maintenance",
            "--format",
            "json",
            "--output",
            str(json_report),
        ]
        json_run = run(command_json, workspace, env)
        checks["filters"] = json_run.returncode == 0 and json_report.is_file()
        filtered_records = []
        if json_report.is_file():
            try:
                parsed = json.loads(json_report.read_text(encoding="utf-8"))
                filtered_records = parsed if isinstance(parsed, list) else parsed.get("records", [])
            except (json.JSONDecodeError, AttributeError):
                filtered_records = []
        checks["outputs"] = bool(filtered_records) and len(filtered_records) == 1
        checks["source_urls"] = bool(filtered_records) and all(
            record.get("source_url") in {item.get("source_url") for item in source_records}
            for record in filtered_records
            if isinstance(record, dict)
        )
        command_markdown = [
            sys.executable,
            "-m",
            "evidence_desk",
            "--input",
            str(fixture),
            "--format",
            "markdown",
            "--output",
            str(markdown_report),
        ]
        markdown_run = run(command_markdown, workspace, env)
        checks["markdown_output"] = markdown_run.returncode == 0 and markdown_report.is_file()
        if markdown_report.is_file():
            markdown_text = markdown_report.read_text(encoding="utf-8")
            checks["source_urls"] = checks["source_urls"] and all(
                item["source_url"] in markdown_text for item in source_records
            )
        expected_ids = ["F-001"]
        checks["sorting"] = [record.get("id") for record in filtered_records] == expected_ids
        malformed = temp / "malformed.json"
        malformed.write_text("{bad", encoding="utf-8")
        malformed_run = run(
            [
                sys.executable,
                "-m",
                "evidence_desk",
                "--input",
                str(malformed),
                "--format",
                "json",
                "--output",
                str(temp / "bad.json"),
            ],
            workspace,
            env,
        )
        checks["malformed_input"] = malformed_run.returncode != 0
        empty_report = temp / "empty.json"
        empty_run = run(
            [
                sys.executable,
                "-m",
                "evidence_desk",
                "--input",
                str(fixture),
                "--status",
                "missing-status",
                "--format",
                "json",
                "--output",
                str(empty_report),
            ],
            workspace,
            env,
        )
        checks["empty_results"] = empty_run.returncode == 0 and empty_report.is_file()
        test_run = run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], workspace, env)
        checks["tests"] = test_run.returncode == 0
        notes = workspace / "RESEARCH_NOTES.md"
        if notes.is_file():
            note_text = notes.read_text(encoding="utf-8")
            checks["research"] = len(re.findall(r"https://docs\.python\.org/", note_text)) >= 2
        else:
            checks["research"] = False
        readme = workspace / "README.md"
        checks["readme"] = readme.is_file() and all(
            word in readme.read_text(encoding="utf-8").lower()
            for word in ("install", "test", "python -m evidence_desk")
        )
        source_text = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in workspace.rglob("*.py")
            if ".git" not in path.parts
        )
        checks["offline"] = not any(
            token in source_text for token in ("requests.get", "httpx.get", "urllib.request.urlopen", "socket.create_connection")
        )
        details["test_output"] = test_run.stdout[-2000:] + test_run.stderr[-2000:]
    return write_result(args.output, checks, details)


def write_result(output: Path, checks: OrderedDict[str, bool], details: dict[str, str]) -> int:
    score = sum(1 for passed in checks.values() if passed) * 5
    result = {
        "score": score,
        "max_score": len(checks) * 5,
        "checks": dict(checks),
        "details": details,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

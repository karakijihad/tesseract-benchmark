from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

WEIGHTS = {"high": 3, "medium": 2, "low": 1}
REQUIRED = ("id", "status", "severity", "category", "title", "summary", "source_url", "severity_weight")


class InputError(Exception):
    """The input file is not usable. The message says what to fix."""


def load_records(path: Path) -> list[dict]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise InputError(f"cannot read {path}: {error.strerror}") from error
    try:
        records = json.loads(text)
    except json.JSONDecodeError as error:
        raise InputError(f"{path} is not valid JSON: {error}") from error
    if not isinstance(records, list):
        raise InputError(f"{path} must hold a JSON list of records")
    for position, record in enumerate(records):
        validate(record, position)
    return records


def validate(record: object, position: int) -> None:
    if not isinstance(record, dict):
        raise InputError(f"record {position} is not an object")
    label = record.get("id") or f"record {position}"
    for field in REQUIRED:
        if field not in record or record[field] in ("", None):
            raise InputError(f"{label} is missing the required field '{field}'")
    if record["status"] not in ("open", "closed"):
        raise InputError(f"{label} has an invalid status '{record['status']}'")
    if record["severity"] not in WEIGHTS:
        raise InputError(f"{label} has an invalid severity '{record['severity']}'")
    weight = record["severity_weight"]
    if isinstance(weight, bool) or not isinstance(weight, int) or weight != WEIGHTS[record["severity"]]:
        raise InputError(f"{label} has a severity_weight that does not match its severity")
    parsed = urlparse(str(record["source_url"]))
    if not parsed.scheme or not parsed.netloc:
        raise InputError(f"{label} has a source_url that is not an absolute URL")


def priority(record: dict) -> int:
    value = record["severity_weight"]
    if record["status"] == "open":
        value += 1
    if record["category"] == "maintenance":
        value += 1
    return value


def select(records: list[dict], status=None, severity=None, category=None) -> list[dict]:
    chosen = []
    for record in records:
        if status and record["status"] != status:
            continue
        if severity and record["severity"] != severity:
            continue
        if category and record["category"] != category:
            continue
        chosen.append({**record, "priority": priority(record)})
    return sorted(chosen, key=lambda item: (-item["priority"], item["id"]))


def to_json(records: list[dict]) -> str:
    return json.dumps(records, indent=2) + "\n"


def to_markdown(records: list[dict]) -> str:
    lines = ["# Evidence report", ""]
    if not records:
        lines.append("No records match the filters.")
        return "\n".join(lines) + "\n"
    for record in records:
        lines += [
            f"## {record['id']}: {record['title']}",
            "",
            f"- Priority: {record['priority']}",
            f"- Status: {record['status']}, severity: {record['severity']}, category: {record['category']}",
            f"- Source: {record['source_url']}",
            "",
            record["summary"],
            "",
        ]
    return "\n".join(lines)

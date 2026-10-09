"""Tables built from run summaries. Nothing here reads anything but the JSON."""
from __future__ import annotations

from typing import Any, Mapping

MISSING = "n/a"

HEADERS = [
    "Contestant",
    "Status",
    "Score",
    "Wall s",
    "Input",
    "Output",
    "Cache read",
    "Cache write",
    "Model calls",
    "Tool calls",
    "Sub-agents",
    "Cost",
]


def _cell(value: Any) -> str:
    return MISSING if value is None else str(value)


def _number(value: Any) -> str:
    if value is None:
        return MISSING
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value)


def score_cell(validation: Mapping[str, Any]) -> str:
    score, maximum = validation.get("score"), validation.get("max_score")
    if score is None:
        return MISSING
    if maximum is None:
        return _number(score)
    return f"{_number(score)}/{_number(maximum)}"


def cost_cell(usage: Mapping[str, Any]) -> str:
    cost = usage.get("cost_usd")
    if cost is None:
        return MISSING
    return f"${cost:.4f} ({usage.get('cost_basis')})"


def record_row(name: str, record: Mapping[str, Any]) -> list[str]:
    usage = record["usage"]
    wall = record["wall_seconds"]
    return [
        name,
        record["status"],
        score_cell(record["validation"]),
        MISSING if wall is None else f"{wall:.3f}",
        _number(usage["input_tokens"]),
        _number(usage["output_tokens"]),
        _number(usage["cache_read_tokens"]),
        _number(usage["cache_write_tokens"]),
        _number(usage["model_calls"]),
        _number(usage["tool_calls"]),
        _number(usage["sub_agents"]),
        cost_cell(usage),
    ]


def summary_rows(summary: Mapping[str, Any]) -> list[list[str]]:
    return [record_row(name, record) for name, record in summary["contestants"].items()]


def markdown_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return lines


def text_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    widths = [max(len(headers[i]), *(len(row[i]) for row in rows)) if rows else len(headers[i]) for i in range(len(headers))]

    def line(cells: list[str]) -> str:
        return " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells)).rstrip()

    return [line(headers), "-+-".join("-" * width for width in widths), *(line(row) for row in rows)]


def render_summary_markdown(summary: Mapping[str, Any]) -> str:
    lines = [
        f"# Benchmark run {summary['run_id']}",
        "",
        f"Task: `{summary['task_id']}`",
        f"Task digest: `{summary['task_digest']}`",
        f"Time limit: {_number(summary['time_limit_minutes'])} minutes",
    ]
    for key, value in summary["context"].items():
        lines.append(f"{key}: `{value}`")
    lines.extend(["", *markdown_table(HEADERS, summary_rows(summary)), ""])
    lines.append(
        "The score comes from the task validator. n/a means the value was not measured or the "
        "contestant was not run. It is never zero."
    )
    unattended = [
        f"- {name}: {record['unattended_mode']}"
        for name, record in summary["contestants"].items()
        if record["unattended_mode"] is not None
    ]
    if unattended:
        lines.extend(["", "Unattended mode:", *unattended])
    return "\n".join(lines) + "\n"


def render_comparison(summaries: list[Mapping[str, Any]]) -> str:
    """One table per task, with a row for every contestant of every run given."""
    by_task: dict[str, list[Mapping[str, Any]]] = {}
    for summary in summaries:
        by_task.setdefault(summary["task_id"], []).append(summary)
    blocks: list[str] = []
    for task_id, group in by_task.items():
        several = len(group) > 1
        digests = {summary["task_digest"] for summary in group}
        lines = [f"Task: {task_id}"]
        if several:
            lines.append(
                "Task digest: " + (next(iter(digests)) if len(digests) == 1
                                   else "DIFFERENT between runs, the results are not comparable")
            )
        else:
            lines.append(f"Task digest: {group[0]['task_digest']}")
        headers = (["Run"] if several else []) + HEADERS
        rows = []
        for summary in group:
            for row in summary_rows(summary):
                rows.append(([summary["run_id"]] if several else []) + row)
        lines.extend(text_table(headers, rows))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)

"""The run record schema, path scrubbing, contestant settings and the comparison tables."""
from __future__ import annotations

import copy
import json

import pytest

from runner.engine import load_contestants, parse_context
from runner.record import (
    SCHEMA_VERSION,
    Usage,
    absolute_paths,
    blank_record,
    scrub,
    validate_record,
    validate_summary,
)
from runner.report import render_comparison, render_summary_markdown


def completed_record(name: str = "a") -> dict:
    record = blank_record(name, "completed")
    record.update(
        {
            "adapter": "stub",
            "prompt_delivery": "file",
            "launch": {"confirmed": True, "method": "in_process"},
            "started_at": "2026-01-01T00:00:00+00:00",
            "ended_at": "2026-01-01T00:00:01+00:00",
            "wall_seconds": 1.0,
            "exit_status": 0,
            "timed_out": False,
            "validation": {
                "score": 30,
                "max_score": 50,
                "checks": [{"name": "x", "passed": True}],
                "error": None,
            },
        }
    )
    record["usage"] = Usage(input_tokens=10, cost_usd=0.25, cost_basis="exact").to_dict()
    return record


def summary_of(*records: dict, task_id: str = "t-v1", run_id: str = "r1", digest: str = "d") -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "task_id": task_id,
        "task_title": "T",
        "task_digest": digest,
        "started_at": "s",
        "ended_at": "e",
        "time_limit_minutes": 30,
        "context": {},
        "contestants": {record["contestant"]: record for record in records},
    }


def test_the_schema_accepts_a_full_record_and_a_blank_one():
    assert validate_record(completed_record()) == []
    assert validate_record(blank_record("ghost")) == []
    assert validate_summary(summary_of(completed_record("a"), blank_record("ghost"))) == []


def test_the_schema_refuses_what_it_does_not_describe():
    cases = []
    extra = completed_record()
    extra["surprise"] = 1
    cases.append(extra)
    missing = completed_record()
    del missing["usage"]["tool_calls"]
    cases.append(missing)
    boolean = completed_record()
    boolean["usage"]["model_calls"] = True
    cases.append(boolean)
    bad_enum = completed_record()
    bad_enum["prompt_delivery"] = "telepathy"
    cases.append(bad_enum)
    cost_without_basis = completed_record()
    cost_without_basis["usage"]["cost_basis"] = "unavailable"
    cases.append(cost_without_basis)
    basis_without_cost = completed_record()
    basis_without_cost["usage"]["cost_usd"] = None
    cases.append(basis_without_cost)
    stale = completed_record()
    stale["schema_version"] = 99
    cases.append(stale)
    failures = [index for index, case in enumerate(cases) if not validate_record(copy.deepcopy(case))]
    assert failures == []


def test_usage_rejects_unknown_fields_and_a_cost_with_no_basis():
    with pytest.raises(ValueError, match="Unknown usage"):
        Usage.from_mapping({"tokens": 1})
    with pytest.raises(ValueError, match="cost_basis"):
        Usage.from_mapping({"cost_usd": 1.0})
    assert Usage.from_mapping({}).cost_basis == "unavailable"
    assert all(v is None for k, v in Usage.absent().to_dict().items() if k != "raw")


@pytest.mark.parametrize(
    "text",
    ["C:\\Work\\x\\file", "d:/work/run", "/srv/data/run", "\\\\" + "server\\share", "see /tmp/x now"],
)
def test_absolute_paths_are_found(text):
    assert absolute_paths({"k": [text]}), text


@pytest.mark.parametrize(
    "text",
    ["stub/workspace", "n/a", "https://example.com/a/b", "tool_calls/second", "2026-01-01T00:00:00+00:00", "and/or"],
)
def test_relative_paths_and_links_are_not_flagged(text):
    assert absolute_paths({"k": text}) == []


def test_scrub_replaces_known_folders_and_any_other_absolute_path():
    forms = ("c:/work/run one",)
    cleaned = scrub({"a": ["opened C:\\Work\\Run One\\x.txt and /etc/passwd"], "n": 3}, forms)
    assert cleaned == {"a": ["opened <dir>" + chr(92) + "x.txt and <path>"], "n": 3}
    assert absolute_paths(cleaned) == []


def test_context_must_be_key_value_and_free_of_paths():
    assert parse_context(["sha=abc", "branch=main"]) == {"sha": "abc", "branch": "main"}
    with pytest.raises(ValueError):
        parse_context(["novalue"])
    with pytest.raises(ValueError, match="absolute path"):
        parse_context(["where=C:\\Work\\x\\repo"])


def test_contestant_settings_are_checked_when_the_file_is_read(tmp_path):
    def load(entry):
        path = tmp_path / "c.json"
        path.write_text(json.dumps({"x": entry}), encoding="utf-8")
        return load_contestants(path)

    assert load(None) == {"x": None}
    assert "x" in load({"adapter": "stub"})
    for entry in (
        {"argv": ["a"]},
        {"adapter": "nope"},
        {"adapter": "command"},
        {"adapter": "command", "argv": ["a"], "prompt": "file"},
        {"adapter": "command", "argv": ["a", "{prompt}"], "prompt": "stdin"},
        {"adapter": "stub", "files": {"../escape.txt": "x"}},
        {"adapter": "stub", "usage": {"bogus": 1}},
        {"adapter": "stub", "model": 5},
    ):
        with pytest.raises(ValueError):
            load(entry)


def test_comparison_shows_missing_values_as_n_a_and_never_as_zero():
    table = render_comparison([summary_of(completed_record("a"), blank_record("ghost"))])
    lines = table.splitlines()
    assert lines[0] == "Task: t-v1"
    ghost = next(line for line in lines if line.startswith("ghost"))
    assert [cell.strip() for cell in ghost.split("|")][2:] == ["n/a"] * 10
    full = next(line for line in lines if line.startswith("a "))
    assert "30/50" in full and "$0.2500 (exact)" in full


def test_comparison_warns_when_runs_of_one_task_used_different_task_files():
    first = summary_of(completed_record("a"), run_id="r1", digest="one")
    second = summary_of(completed_record("a"), run_id="r2", digest="two")
    assert "DIFFERENT" in render_comparison([first, second])
    assert "DIFFERENT" not in render_comparison([first, summary_of(completed_record("a"), run_id="r2", digest="one")])


def test_comparison_groups_runs_by_task():
    other = summary_of(completed_record("a"), task_id="u-v1", run_id="r3")
    table = render_comparison([summary_of(completed_record("a")), other])
    assert table.count("Task: ") == 2


def test_the_markdown_summary_comes_from_the_json_alone():
    summary = summary_of(completed_record("a"), blank_record("ghost"))
    summary["context"] = {"sha": "abc123"}
    markdown = render_summary_markdown(json.loads(json.dumps(summary)))
    assert "sha: `abc123`" in markdown
    assert "| ghost | not_configured | n/a |" in markdown
    assert "| a | completed | 30/50 |" in markdown


MESSY_PATHS = [
    "opened C:" + "\\Work\\Run One\\x.txt",
    "see /etc/passwd now",
    "share " + "\\\\" + "fileserver\\share\\x",
    "url file:///C:/Work%20Dir/a.txt end",
    "encoded C%3A%5CWork%5Cthing",
    "encoded %2Fetc%2Fpasswd",
    "twice %252Fetc%252Fpasswd",
    "plain URL form file://fileserver/share/x",
]
NOT_PATHS = ["50%25 sure", "and%2For", "https://example.com/a%20b/c", "10/20 done", "n/a"]


def test_the_scrub_and_the_guard_use_one_matcher_for_every_spelling_of_a_path():
    forms = ("c:/work/run one",)
    not_found = [text for text in MESSY_PATHS if not absolute_paths({"k": text}, forms)]
    assert not_found == [], "the guard must see every spelling the scrub removes"
    leaks = [text for text in MESSY_PATHS if absolute_paths(scrub({"k": text}, forms), forms)]
    assert leaks == []
    flagged = [text for text in NOT_PATHS if absolute_paths({"k": text}, forms)]
    assert flagged == []
    assert scrub("opened file:///C:/Work%20Dir/a.txt now", ()) == "opened <path> now"


def test_the_scrub_cleans_dictionary_keys_and_keeps_keys_that_collide():
    cleaned = scrub({"C:" + "\\Work\\one": 1, "C:" + "\\Work\\two": 2, "plain": 3}, ())
    assert absolute_paths(cleaned) == []
    assert sorted(cleaned.values()) == [1, 2, 3]

"""The codex adapter: settings, command, usage from events, and the spend cap.

Checks that bad settings are refused, that the command pins the model and the
effort and takes the brief on standard input, that a recorded event stream
becomes tokens, tool calls and an estimated cost, and that a run whose
estimated cost reaches the cap is killed while it is still going. The running
process in these tests is a small fake that prints events, never real Codex.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import stat
import sys
import time

import pytest

from runner.adapters.base import LaunchRequest
from runner.adapters.codex import CodexAdapter, Meter, build_usage

PRICES = {"input": 2.0, "cached_input": 0.5, "output": 10.0}
THREAD = "00000000-0000-0000-0000-000000000001"

# The events a real `codex exec --json` printed for a one word reply, with the
# thread id replaced.
RECORDED = [
    {"type": "thread.started", "thread_id": THREAD},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "ok"}},
    {
        "type": "turn.completed",
        "usage": {
            "input_tokens": 15294,
            "cached_input_tokens": 11008,
            "cache_write_input_tokens": 0,
            "output_tokens": 5,
            "reasoning_output_tokens": 0,
        },
    },
]

# Item events for work Codex does with tools, in the shape its event stream
# uses for them. A started item and its completed item share an id.
TOOL_EVENTS = [
    {"type": "item.started", "item": {"id": "item_1", "type": "command_execution", "command": "dir", "status": "in_progress"}},
    {"type": "item.completed", "item": {"id": "item_1", "type": "command_execution", "command": "dir", "exit_code": 0, "status": "completed"}},
    {"type": "item.completed", "item": {"id": "item_2", "type": "file_change", "changes": [{"path": "a.txt", "kind": "add"}], "status": "completed"}},
    {"type": "item.completed", "item": {"id": "item_3", "type": "mcp_tool_call", "server": "s", "tool": "t", "status": "completed"}},
    {"type": "item.completed", "item": {"id": "item_4", "type": "reasoning", "text": "thinking"}},
]


def good_settings(**changes: object) -> dict:
    settings = {
        "adapter": "codex",
        "model": "gpt-6-luna",
        "effort": "high",
        "max_budget_usd": 3,
        "price_per_million": dict(PRICES),
    }
    settings.update(changes)
    return settings


def stream(events: list[dict]) -> bytes:
    return "".join(json.dumps(event) + "\n" for event in events).encode("utf-8")


def metered(events: list[dict]) -> Meter:
    meter = Meter(PRICES)
    meter.feed(stream(events))
    meter.finish()
    return meter


@pytest.mark.parametrize(
    "changes",
    [
        {"model": None},
        {"model": ""},
        {"model": "-x"},
        {"effort": None},
        {"effort": 'high"'},
        {"max_budget_usd": None},
        {"max_budget_usd": 0},
        {"max_budget_usd": True},
        {"price_per_million": None},
        {"price_per_million": {"input": 1.0, "output": 2.0}},
        {"price_per_million": {**PRICES, "extra": 1.0}},
        {"price_per_million": {**PRICES, "output": -1}},
        {"price_per_million": {**PRICES, "input": "2"}},
        {"program": ""},
        {"max_budget": 3},
    ],
)
def test_bad_settings_are_refused(changes: dict) -> None:
    with pytest.raises(ValueError):
        CodexAdapter().validate_settings(good_settings(**changes))


def test_missing_settings_are_refused_with_no_defaults() -> None:
    adapter = CodexAdapter()
    adapter.validate_settings(good_settings())
    for key in ("model", "effort", "max_budget_usd", "price_per_million"):
        settings = good_settings()
        del settings[key]
        with pytest.raises(ValueError):
            adapter.validate_settings(settings)


def test_command_pins_model_effort_and_sandbox_and_reads_the_brief_from_stdin() -> None:
    argv = CodexAdapter.command(good_settings())
    assert argv[:5] == ["codex", "exec", "--json", "-m", "gpt-6-luna"]
    assert argv[argv.index("-c") + 1] == 'model_reasoning_effort="high"'
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert argv[-1] == "-"
    assert CodexAdapter.command(good_settings(program="other"))[0] == "other"


def test_recorded_events_become_estimated_usage() -> None:
    meter = metered(RECORDED + TOOL_EVENTS)
    usage = build_usage(meter, good_settings(), stopped=False)
    assert usage.input_tokens == 15294 - 11008
    assert usage.cache_read_tokens == 11008
    assert usage.output_tokens == 5
    assert usage.model_calls == 1
    assert usage.tool_calls == 3
    assert usage.cost_basis == "estimated"
    expected = ((15294 - 11008) * 2.0 + 11008 * 0.5 + 5 * 10.0) / 1_000_000
    assert usage.cost_usd == pytest.approx(expected, abs=1e-6)
    assert usage.raw["stopped_at_budget"] is False
    assert usage.raw["turn_usage"][0]["input_tokens"] == 15294
    assert usage.raw["tool_calls_by_type"] == {"command_execution": 1, "file_change": 1, "mcp_tool_call": 1}


def test_reasoning_tokens_are_priced_once_as_part_of_output() -> None:
    events = [
        {"type": "turn.completed", "usage": {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 1000, "reasoning_output_tokens": 600}},
        {"type": "turn.completed", "usage": {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 500, "reasoning_output_tokens": 100}},
    ]
    usage = build_usage(metered(events), good_settings(), stopped=False)
    assert usage.output_tokens == 1500
    assert usage.model_calls == 2
    assert usage.cost_usd == pytest.approx(1500 * 10.0 / 1_000_000)
    assert usage.raw["reasoning_output_tokens"] == 700


def test_a_stream_with_no_usage_has_no_cost() -> None:
    usage = build_usage(metered([{"type": "thread.started", "thread_id": THREAD}]), good_settings(), stopped=False)
    assert usage.cost_usd is None and usage.cost_basis == "unavailable"
    assert usage.input_tokens is None and usage.output_tokens is None
    nothing = build_usage(metered([]), good_settings(), stopped=False)
    assert nothing.tool_calls is None


def test_lines_split_across_reads_and_noise_are_handled() -> None:
    meter = Meter(PRICES)
    data = b"warning: not json\n" + stream(RECORDED)
    for index in range(0, len(data), 7):
        meter.feed(data[index : index + 7])
    meter.finish()
    assert len(meter.turns) == 1 and meter.thread_id == THREAD


FAKE = r"""
import json, os, sys, time
if sys.argv[1:] == ["--version"]:
    print("codex-cli 0.0.0")
    sys.exit(0)
mode = os.environ["FAKE_MODE"]
brief = sys.stdin.buffer.read().decode("utf-8")
json.dump({"argv": sys.argv[1:], "brief": brief}, open("seen.json", "w"))

def emit(event):
    print(json.dumps(event), flush=True)

emit({"type": "thread.started", "thread_id": os.environ["FAKE_THREAD"]})
emit({"type": "turn.started"})
usage = {"input_tokens": 1000000, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0}
if mode == "finish":
    emit({"type": "item.completed", "item": {"id": "i1", "type": "command_execution"}})
    emit({"type": "turn.completed", "usage": usage})
elif mode == "hang_after_turn":
    emit({"type": "turn.completed", "usage": usage})
    time.sleep(120)
    open("survived.txt", "w").write("x")
elif mode == "rollout":
    path = os.path.join(os.environ["CODEX_HOME"], "sessions", "2000", "01", "01",
                        "rollout-2000-01-01T00-00-00-" + os.environ["FAKE_THREAD"] + ".jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    for tokens in (10, 3000000):
        info = {"total_token_usage": {"input_tokens": tokens, "cached_input_tokens": 0,
                                      "output_tokens": 0, "reasoning_output_tokens": 0}}
        with open(path, "a") as handle:
            handle.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": info}}) + "\n")
        time.sleep(0.3)
    time.sleep(120)
    open("survived.txt", "w").write("x")
"""


def fake_program(tmp_path: Path) -> str:
    script = tmp_path / "fake_codex.py"
    script.write_text(FAKE, encoding="utf-8")
    if sys.platform == "win32":
        shim = tmp_path / "fake_codex.cmd"
        shim.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        shim = tmp_path / "fake_codex.sh"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    return str(shim)


def launch(tmp_path: Path, mode: str, cap: float, limit: float = 60.0) -> tuple[object, bytes, Path]:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = tmp_path / "codex-home"
    env = {
        **os.environ,
        "FAKE_MODE": mode,
        "FAKE_THREAD": THREAD,
        "CODEX_HOME": str(home),
    }
    transcript = io.BytesIO()
    request = LaunchRequest(
        workspace=workspace,
        brief="Line one.\nLine two.\n",
        time_limit_seconds=limit,
        settings=good_settings(program=fake_program(tmp_path), max_budget_usd=cap),
        env=env,
        transcript=transcript,
    )
    result = CodexAdapter().run(request)
    return result, transcript.getvalue(), workspace


def test_a_run_under_the_cap_finishes_and_keeps_the_full_transcript(tmp_path: Path) -> None:
    started = time.monotonic()
    result, transcript, workspace = launch(tmp_path, "finish", cap=100)
    assert time.monotonic() - started < 30
    assert result.launch_confirmed and result.prompt_delivery == "stdin"
    assert result.version == "codex-cli 0.0.0"
    assert result.exit_status == 0 and not result.timed_out
    assert result.usage.raw["stopped_at_budget"] is False
    assert result.usage.cost_usd == pytest.approx(2.0)
    assert result.usage.cost_basis == "estimated"
    assert result.usage.tool_calls == 1
    text = transcript.decode("utf-8")
    assert text.startswith("command: ")
    for event in ('"thread.started"', '"item.completed"', '"turn.completed"'):
        assert event in text
    seen = json.loads((workspace / "seen.json").read_text(encoding="utf-8"))
    assert seen["brief"] == "Line one.\nLine two.\n"
    assert seen["argv"][:4] == ["exec", "--json", "-m", "gpt-6-luna"]
    assert seen["argv"][-1] == "-"
    assert 'model_reasoning_effort="high"' in seen["argv"]


def test_a_run_is_stopped_when_a_turn_report_reaches_the_cap(tmp_path: Path) -> None:
    started = time.monotonic()
    result, transcript, workspace = launch(tmp_path, "hang_after_turn", cap=1.5)
    assert time.monotonic() - started < 40
    assert result.usage.raw["stopped_at_budget"] is True
    assert result.timed_out is False
    assert result.usage.cost_usd == pytest.approx(2.0)
    assert b'"turn.completed"' in transcript
    assert any("spend cap" in warning for warning in result.warnings)
    assert not (workspace / "survived.txt").exists()


def test_a_run_is_stopped_mid_turn_from_the_session_file(tmp_path: Path) -> None:
    started = time.monotonic()
    result, transcript, workspace = launch(tmp_path, "rollout", cap=5)
    assert time.monotonic() - started < 40
    assert result.usage.raw["stopped_at_budget"] is True
    assert b'"turn.completed"' not in transcript
    assert result.usage.input_tokens == 3000000
    assert result.usage.cost_usd == pytest.approx(6.0)
    assert result.usage.cost_basis == "estimated"
    assert not (workspace / "survived.txt").exists()


def test_the_time_limit_still_applies(tmp_path: Path) -> None:
    result, _, workspace = launch(tmp_path, "rollout", cap=1000, limit=3)
    assert result.timed_out is True
    assert result.usage.raw["stopped_at_budget"] is False
    assert not (workspace / "survived.txt").exists()

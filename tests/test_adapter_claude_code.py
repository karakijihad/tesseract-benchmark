"""The claude-code adapter: required settings, the command it builds, and usage read from the stream.

Every launch excludes the starter's own Claude Code setup and the record says so.

A recorded stream (trimmed from a real headless run) turns into exact cost,
token counts, tool calls and sub-agents; a run whose result event says the
spend cap was hit is flagged; a stream with no result event reports no usage;
and a launched run keeps the whole output in the transcript.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import stat
import sys

import pytest

from runner.adapters import claude_code
from runner.adapters.base import LaunchRequest
from runner.adapters.claude_code import ClaudeCodeAdapter, parse_events, usage_from_events
from runner.record import Usage

SETTINGS = {
    "model": "claude-sonnet-5-5",
    "effort": "high",
    "max_budget_usd": 3,
    "permission_mode": "bypassPermissions",
}

INIT = {
    "type": "system",
    "subtype": "init",
    "session_id": "00000000-0000-4000-8000-000000000001",
    "model": "claude-sonnet-5-5",
    "permissionMode": "bypassPermissions",
    "claude_code_version": "2.1.296",
    "tools": ["Task", "Bash", "Edit", "Read", "Write"],
}
HOOK = {"type": "system", "subtype": "hook_started", "hook_event": "SessionStart"}


def assistant(blocks: list[dict], message_id: str, tokens: tuple[int, int, int, int]) -> dict:
    fresh, cache_write, cache_read, out = tokens
    return {
        "type": "assistant",
        "message": {
            "model": "claude-sonnet-5-5",
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "content": blocks,
            "usage": {
                "input_tokens": fresh,
                "cache_creation_input_tokens": cache_write,
                "cache_read_input_tokens": cache_read,
                "output_tokens": out,
            },
        },
        "parent_tool_use_id": None,
        "session_id": INIT["session_id"],
    }


def tool_use(block_id: str, name: str, tool_input: dict) -> dict:
    return {"type": "tool_use", "id": block_id, "name": name, "input": tool_input}


def tool_result(block_id: str, text: str) -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": block_id, "content": text}],
        },
        "parent_tool_use_id": None,
        "session_id": INIT["session_id"],
    }


# Shaped like the result event of a real run; the figures are round numbers.
SUCCESS_RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "duration_ms": 41230,
    "num_turns": 4,
    "result": "Done. answer.txt holds 42.",
    "stop_reason": "end_turn",
    "session_id": INIT["session_id"],
    "total_cost_usd": 0.4125,
    "usage": {
        "input_tokens": 9,
        "cache_creation_input_tokens": 21000,
        "cache_read_input_tokens": 52000,
        "output_tokens": 1500,
    },
    "modelUsage": {
        "claude-sonnet-5-5": {
            "inputTokens": 9,
            "outputTokens": 1500,
            "cacheReadInputTokens": 52000,
            "cacheCreationInputTokens": 21000,
            "costUSD": 0.4125,
        }
    },
    "permission_denials": [],
    "terminal_reason": "completed",
}

# A run stopped by the spend cap. The top-level usage block is all zeros here
# while the per-model block holds the real figures, exactly as Claude Code
# printed it.
BUDGET_RESULT = {
    "type": "result",
    "subtype": "error_max_budget_usd",
    "is_error": True,
    "duration_ms": 10841,
    "num_turns": 1,
    "stop_reason": "end_turn",
    "session_id": INIT["session_id"],
    "total_cost_usd": 0.07853,
    "usage": {
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
    },
    "modelUsage": {
        "claude-sonnet-5-5": {
            "inputTokens": 2,
            "outputTokens": 4,
            "cacheReadInputTokens": 16420,
            "cacheCreationInputTokens": 19211,
            "costUSD": 0.07853,
        }
    },
    "permission_denials": [],
    "terminal_reason": "budget_exhausted",
    "errors": ["Reached maximum budget ($0.05)"],
}

SUCCESS_STREAM = [
    HOOK,
    INIT,
    # One reply streamed as two events that share a tool_use id: one call.
    assistant([{"type": "text", "text": "I will write the file."}], "msg_1", (2, 20000, 0, 40)),
    assistant([tool_use("toolu_1", "Write", {"file_path": "answer.txt"})], "msg_1", (2, 20000, 0, 40)),
    assistant([tool_use("toolu_1", "Write", {"file_path": "answer.txt"})], "msg_1", (2, 20000, 0, 40)),
    tool_result("toolu_1", "File created"),
    assistant(
        [
            tool_use("toolu_2", "Bash", {"command": "cat answer.txt"}),
            tool_use("toolu_3", "Task", {"description": "check", "prompt": "verify answer.txt"}),
            tool_use("toolu_4", "Agent", {"description": "check again", "prompt": "verify again"}),
        ],
        "msg_2",
        (3, 500, 20000, 90),
    ),
    tool_result("toolu_2", "42"),
    assistant([{"type": "text", "text": "Done."}], "msg_3", (4, 500, 32000, 20)),
    SUCCESS_RESULT,
]
BUDGET_STREAM = [
    HOOK,
    INIT,
    assistant([{"type": "text", "text": "ok"}], "msg_1", (2, 19211, 16420, 4)),
    BUDGET_RESULT,
]


def to_bytes(events: list[dict]) -> bytes:
    return b"".join(json.dumps(event).encode("utf-8") + b"\n" for event in events)


def parse(events: list[dict]) -> Usage:
    return usage_from_events(parse_events(io.BytesIO(to_bytes(events))))


class TestSettings:
    def test_complete_settings_are_accepted(self) -> None:
        ClaudeCodeAdapter().validate_settings(SETTINGS)
        ClaudeCodeAdapter().validate_settings({**SETTINGS, "program": "claude", "max_budget_usd": 0.5})

    def test_every_required_setting_is_enforced_and_each_bad_value_is_refused(self) -> None:
        adapter = ClaudeCodeAdapter()
        offenders = []
        for name in ("model", "effort", "max_budget_usd", "permission_mode"):
            missing = {key: value for key, value in SETTINGS.items() if key != name}
            try:
                adapter.validate_settings(missing)
                offenders.append(f"{name} missing was accepted")
            except ValueError as error:
                if name not in str(error):
                    offenders.append(f"{name} missing gave a message that does not name it: {error}")
        bad = {
            "model": ["", "--max-budget-usd", 5, None],
            "effort": ["", "extreme", None, 3],
            "max_budget_usd": [0, -1, "3", True, None, float("inf"), float("nan")],
            "permission_mode": ["", "yolo", None],
            "program": ["", "-rf", 4],
        }
        for name, values in bad.items():
            for value in values:
                try:
                    adapter.validate_settings({**SETTINGS, name: value})
                    offenders.append(f"{name}={value!r} was accepted")
                except ValueError:
                    pass
        try:
            adapter.validate_settings({**SETTINGS, "max_budget": 3})
            offenders.append("an unknown setting was accepted")
        except ValueError:
            pass
        assert not offenders, "\n".join(offenders)

    def test_command_pins_model_effort_cap_and_permission_mode(self) -> None:
        assert ClaudeCodeAdapter.build_command(SETTINGS) == [
            "claude",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            "claude-sonnet-5-5",
            "--effort",
            "high",
            "--max-budget-usd",
            "3",
            "--permission-mode",
            "bypassPermissions",
            *claude_code.ISOLATION_FLAGS,
        ]
        command = ClaudeCodeAdapter.build_command({**SETTINGS, "program": "other-claude", "max_budget_usd": 2.5})
        assert command[0] == "other-claude"
        assert command[command.index("--max-budget-usd") + 1] == "2.5"

    def test_the_starters_own_setup_is_always_switched_off(self) -> None:
        command = ClaudeCodeAdapter.build_command(SETTINGS)
        assert "--safe-mode" in command
        assert command[command.index("--setting-sources") + 1] == "project"
        assert "--strict-mcp-config" in command
        assert "--disable-slash-commands" in command
        assert json.loads(command[command.index("--settings") + 1]) == {"disableAllHooks": True}
        # No setting can turn any of it off, and a setting naming one is refused.
        for name in ("safe_mode", "setting_sources", "isolation", "settings"):
            with pytest.raises(ValueError):
                ClaudeCodeAdapter().validate_settings({**SETTINGS, name: True})


class TestUsage:
    def test_recorded_stream_gives_exact_cost_tokens_and_counts(self) -> None:
        usage = parse(SUCCESS_STREAM)
        assert usage.cost_usd == 0.4125
        assert usage.cost_basis == "exact"
        assert (usage.input_tokens, usage.output_tokens) == (9, 1500)
        assert (usage.cache_read_tokens, usage.cache_write_tokens) == (52000, 21000)
        assert usage.model_calls == 4
        # Write (once, though it streamed twice), Bash, Task and Agent.
        assert usage.tool_calls == 4
        assert usage.sub_agents == 2
        assert usage.raw["stopped_at_budget"] is False
        assert usage.raw["total_cost_usd"] == 0.4125
        assert usage.raw["result_event_found"] is True
        Usage.from_mapping(usage.to_dict())

    def test_budget_stop_is_flagged_and_tokens_come_from_the_per_model_block(self) -> None:
        usage = parse(BUDGET_STREAM)
        assert usage.raw["stopped_at_budget"] is True
        assert usage.raw["subtype"] == "error_max_budget_usd"
        assert usage.cost_usd == 0.07853
        assert usage.cost_basis == "exact"
        assert (usage.input_tokens, usage.output_tokens) == (2, 4)
        assert (usage.cache_read_tokens, usage.cache_write_tokens) == (16420, 19211)
        assert (usage.tool_calls, usage.sub_agents) == (0, 0)

    def test_either_budget_signal_alone_sets_the_flag(self) -> None:
        for change in ({"subtype": "error_max_budget_usd", "terminal_reason": "x"}, {"terminal_reason": "budget_exhausted"}):
            assert parse([{**SUCCESS_RESULT, **change}]).raw["stopped_at_budget"] is True

    def test_tokens_sum_over_models_and_fall_back_to_the_plain_usage_block(self) -> None:
        two_models = {
            **SUCCESS_RESULT,
            "modelUsage": {
                "model-a": {"inputTokens": 1, "outputTokens": 2, "cacheReadInputTokens": 3, "cacheCreationInputTokens": 4},
                "model-b": {"inputTokens": 10, "outputTokens": 20, "cacheReadInputTokens": 30, "cacheCreationInputTokens": 40},
            },
        }
        usage = parse([two_models])
        assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens, usage.cache_write_tokens) == (11, 22, 33, 44)
        without_models = {key: value for key, value in SUCCESS_RESULT.items() if key != "modelUsage"}
        usage = parse([without_models])
        assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens, usage.cache_write_tokens) == (9, 1500, 52000, 21000)

    def test_a_missing_figure_is_none_not_zero(self) -> None:
        bare = {"type": "result", "subtype": "success", "is_error": False}
        usage = parse([bare])
        assert usage.cost_usd is None
        assert usage.cost_basis == "unavailable"
        assert usage.input_tokens is None and usage.output_tokens is None
        assert usage.model_calls is None
        broken_cost = parse([{**SUCCESS_RESULT, "total_cost_usd": "free"}])
        assert broken_cost.cost_usd is None and broken_cost.cost_basis == "unavailable"

    def test_a_stream_with_no_result_event_has_no_usage(self) -> None:
        usage = parse(SUCCESS_STREAM[:-1])
        assert usage.cost_usd is None
        assert usage.cost_basis == "unavailable"
        assert usage.tool_calls is None
        assert usage.raw == {"result_event_found": False}

    def test_lines_that_are_not_events_are_skipped(self) -> None:
        noisy = (
            b"a warning from the program\n"
            + to_bytes(SUCCESS_STREAM)
            + b'{"type": "assistant", "message": {"content": [{"type": "tool_u'
        )
        usage = usage_from_events(parse_events(io.BytesIO(noisy)))
        assert usage.cost_usd == 0.4125


def fake_claude(folder: Path, stream: bytes, exit_code: int) -> None:
    """A program named claude that records how it was called and prints a recorded stream."""
    script = folder / "fake_claude.py"
    script.write_text(
        "import json, os, sys\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('9.9.9 (Claude Code)')\n"
        "    raise SystemExit(0)\n"
        "json.dump({'argv': sys.argv[1:], 'stdin': sys.stdin.read(), 'cwd': os.getcwd()},"
        " open('call.json', 'w'))\n"
        f"sys.stdout.buffer.write({stream!r})\n"
        f"raise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    if sys.platform == "win32":
        (folder / "claude.cmd").write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        shim = folder / "claude"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR)


def launch(tmp_path: Path, stream: bytes, exit_code: int = 0):
    tools = tmp_path / "tools"
    workspace = tmp_path / "workspace"
    tools.mkdir()
    workspace.mkdir()
    fake_claude(tools, stream, exit_code)
    env = dict(os.environ)
    env["PATH"] = str(tools) + os.pathsep + env.get("PATH", "")
    transcript = io.BytesIO()
    request = LaunchRequest(
        workspace=workspace,
        brief="Write 42 into answer.txt.\n",
        time_limit_seconds=60,
        settings=SETTINGS,
        env=env,
        transcript=transcript,
    )
    result = ClaudeCodeAdapter().run(request)
    call = json.loads((workspace / "call.json").read_text(encoding="utf-8"))
    return result, call, transcript.getvalue().decode("utf-8")


class TestRun:
    def test_run_passes_the_brief_on_stdin_and_reports_usage(self, tmp_path: Path) -> None:
        result, call, transcript = launch(tmp_path, to_bytes(SUCCESS_STREAM))
        assert call["stdin"] == "Write 42 into answer.txt.\n"
        assert Path(call["cwd"]).name == "workspace"
        assert call["argv"] == ClaudeCodeAdapter.build_command(SETTINGS)[1:]
        assert result.launch_confirmed is True
        assert result.prompt_delivery == "stdin"
        assert result.exit_status == 0
        assert result.version == "9.9.9 (Claude Code)"
        assert result.usage.cost_usd == 0.4125 and result.usage.cost_basis == "exact"
        assert result.usage.tool_calls == 4 and result.usage.sub_agents == 2
        assert result.warnings == []
        assert result.usage.raw["isolation"] == claude_code.ISOLATION_NOTE
        assert "--safe-mode" in call["argv"] and "--strict-mcp-config" in call["argv"]
        assert transcript.startswith("command: ")
        assert to_bytes(SUCCESS_STREAM).decode("utf-8") in transcript
        assert "exit_status: 0" in transcript

    def test_run_that_hit_the_budget_is_flagged_and_kept_whole_in_the_transcript(self, tmp_path: Path) -> None:
        result, _call, transcript = launch(tmp_path, to_bytes(BUDGET_STREAM), exit_code=1)
        assert result.exit_status == 1
        assert result.usage.raw["stopped_at_budget"] is True
        assert result.usage.raw["isolation"] == claude_code.ISOLATION_NOTE
        assert result.usage.cost_usd == 0.07853
        assert any("spend cap" in warning for warning in result.warnings)
        assert to_bytes(BUDGET_STREAM).decode("utf-8") in transcript

    def test_run_with_no_result_event_records_no_usage_and_says_so(self, tmp_path: Path) -> None:
        result, _call, _transcript = launch(tmp_path, to_bytes(SUCCESS_STREAM[:-1]), exit_code=1)
        assert result.usage.cost_usd is None
        assert result.usage.cost_basis == "unavailable"
        assert result.usage.raw["isolation"] == claude_code.ISOLATION_NOTE
        assert any("no result event" in warning for warning in result.warnings)

    def test_run_refuses_invalid_settings_before_starting_anything(self, tmp_path: Path) -> None:
        request = LaunchRequest(
            workspace=tmp_path,
            brief="x",
            time_limit_seconds=5,
            settings={"model": "claude-sonnet-5-5"},
            env=dict(os.environ),
            transcript=io.BytesIO(),
        )
        with pytest.raises(ValueError):
            ClaudeCodeAdapter().run(request)

    def test_adapter_name(self) -> None:
        assert claude_code.ClaudeCodeAdapter.name == "claude-code"

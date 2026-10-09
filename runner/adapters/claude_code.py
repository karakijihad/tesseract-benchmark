"""Launch Claude Code headless and read its usage from its own result event.

The command is `claude -p --output-format stream-json --verbose` with the
model, effort, spend cap and permission mode taken from the contestant's
settings. None of those has a default: a benchmark run that silently used
whatever the installed tool preferred would not be comparable with the next
one. The brief goes in on standard input and the workspace is the working
directory.

The contestant runs stock. Whatever the person who started the benchmark has
set up in Claude Code (settings, hooks, plugins, skills, slash commands, MCP
servers, account connectors, personal instruction files and memory) is switched
off on every launch by the flags in `ISOLATION_FLAGS`, and there is no setting
to turn that off: a contestant that read the starter's own configuration would
be measuring that configuration. The login is kept, because the flags leave
authentication alone.

Claude Code writes one JSON object per line. The last `result` event carries
the run's total cost and token counts, and the `assistant` events carry the
tool calls. Standard output is written to a temporary file (never a pipe, see
`runner/proc.py`), copied whole into the transcript, and read from there once
the process has ended.
"""
from __future__ import annotations

import json
import math
import re
import tempfile
from typing import Any, BinaryIO, Iterable, Mapping

from ..proc import run_bounded, run_captured
from ..record import Usage
from .base import (
    Adapter,
    AdapterResult,
    LaunchRequest,
    check_no_forbidden_paths,
    write_transcript_footer,
    write_transcript_header,
)
from .command import resolve_program

DEFAULT_PROGRAM = "claude"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
PERMISSION_MODES = ("acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan")
# The runner reads `adapter`, `env_passthrough` and `unattended_mode` from the
# same contestant entry, so they are allowed here without being used.
SETTING_NAMES = {
    "adapter", "env_passthrough", "unattended_mode",
    "model", "effort", "max_budget_usd", "permission_mode", "program",
}
# Always on. `--safe-mode` disables customizations (instruction files, skills,
# installed plugins, hooks, MCP servers, custom commands and agents, output
# styles) and leaves authentication, model choice and the built-in tools alone.
# The other flags say the same thing a second way so that one of them being
# dropped in a later release does not let the starter's setup back in: only
# the workspace's own project settings are read, MCP servers come only from an
# explicit list (there is none), skills are off, and hooks are off.
ISOLATION_FLAGS = (
    "--safe-mode",
    "--setting-sources",
    "project",
    "--strict-mcp-config",
    "--disable-slash-commands",
    "--settings",
    '{"disableAllHooks": true}',
)
ISOLATION_NOTE = (
    "stock Claude Code: user settings, hooks, plugins, skills, slash commands, MCP servers, "
    "connectors, instruction files and memory excluded (--safe-mode, --setting-sources project, "
    "--strict-mcp-config, --disable-slash-commands, disableAllHooks); login kept"
)
VERSION_TIMEOUT_SECONDS = 20
COPY_CHUNK_BYTES = 1024 * 1024
MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\[\]-]*$")
# Tool names Claude Code uses to start a sub-agent. The tool was renamed
# between releases, so both spellings count.
SUB_AGENT_TOOLS = frozenset({"Task", "Agent"})
# Both signals are checked because either one alone could be dropped in a
# later release: the result's subtype and its terminal reason.
BUDGET_SUBTYPE = "error_max_budget_usd"
BUDGET_TERMINAL_REASON = "budget_exhausted"
# The result event's per-model block, and the field each normalized figure
# comes from. This block covers every model and sub-agent in the run. The
# top-level `usage` block can be all zeros when the run stopped at the cap, so
# it is only the fallback.
MODEL_USAGE_FIELDS = {
    "input_tokens": "inputTokens",
    "output_tokens": "outputTokens",
    "cache_read_tokens": "cacheReadInputTokens",
    "cache_write_tokens": "cacheCreationInputTokens",
}
PLAIN_USAGE_FIELDS = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_read_tokens": "cache_read_input_tokens",
    "cache_write_tokens": "cache_creation_input_tokens",
}


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _money(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return float(value)


def parse_events(lines: Iterable[bytes]) -> list[dict[str, Any]]:
    """Every line that is a JSON object. Anything else (a warning, a line cut off by a kill) is skipped."""
    events: list[dict[str, Any]] = []
    for line in lines:
        text = line.decode("utf-8", errors="replace").strip()
        if not text.startswith("{"):
            continue
        try:
            event = json.loads(text)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def count_tool_uses(events: Iterable[Mapping[str, Any]]) -> tuple[int, int]:
    """Tool calls and sub-agent starts, counting each tool_use block once.

    A single model reply can be streamed as several assistant events that share
    the block ids, so the ids are what is counted. A sub-agent's own tool calls
    arrive in the same stream and are included.
    """
    seen: set[str] = set()
    tools = 0
    agents = 0
    for event in events:
        if event.get("type") != "assistant":
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            block_id = block.get("id")
            if isinstance(block_id, str):
                if block_id in seen:
                    continue
                seen.add(block_id)
            tools += 1
            if block.get("name") in SUB_AGENT_TOOLS:
                agents += 1
    return tools, agents


def _token_figures(result: Mapping[str, Any]) -> dict[str, int | None]:
    figures: dict[str, int | None] = {name: None for name in MODEL_USAGE_FIELDS}
    per_model = result.get("modelUsage")
    if isinstance(per_model, dict) and per_model:
        for name, key in MODEL_USAGE_FIELDS.items():
            values = [
                _count(entry.get(key)) for entry in per_model.values() if isinstance(entry, dict)
            ]
            if values and all(value is not None for value in values):
                figures[name] = sum(value for value in values if value is not None)
        return figures
    plain = result.get("usage")
    if isinstance(plain, dict):
        for name, key in PLAIN_USAGE_FIELDS.items():
            figures[name] = _count(plain.get(key))
    return figures


def stopped_at_budget(result: Mapping[str, Any]) -> bool:
    return (
        result.get("subtype") == BUDGET_SUBTYPE
        or result.get("terminal_reason") == BUDGET_TERMINAL_REASON
    )


def usage_from_events(events: list[dict[str, Any]]) -> Usage:
    """Normalized usage from a parsed stream.

    Cost is Claude Code's own `total_cost_usd`. model_calls is its `num_turns`,
    the count of model round trips in the conversation. With no result event
    (the run was killed first) there is no defensible figure, so everything
    stays None and the cost basis stays "unavailable".
    """
    results = [event for event in events if event.get("type") == "result"]
    if not results:
        return Usage(raw={"result_event_found": False})
    result = results[-1]
    tools, agents = count_tool_uses(events)
    tokens = _token_figures(result)
    cost = _money(result.get("total_cost_usd"))
    return Usage(
        input_tokens=tokens["input_tokens"],
        output_tokens=tokens["output_tokens"],
        cache_read_tokens=tokens["cache_read_tokens"],
        cache_write_tokens=tokens["cache_write_tokens"],
        model_calls=_count(result.get("num_turns")),
        tool_calls=tools,
        sub_agents=agents,
        cost_usd=cost,
        cost_basis="exact" if cost is not None else "unavailable",
        raw={**result, "result_event_found": True, "stopped_at_budget": stopped_at_budget(result)},
    )


def copy_stream(source: BinaryIO, target: BinaryIO) -> None:
    source.seek(0)
    while True:
        chunk = source.read(COPY_CHUNK_BYTES)
        if not chunk:
            break
        target.write(chunk)
    target.flush()


class ClaudeCodeAdapter(Adapter):
    name = "claude-code"

    def validate_settings(self, settings: Mapping[str, Any]) -> None:
        unknown = sorted(set(settings) - SETTING_NAMES)
        if unknown:
            raise ValueError("Unknown claude-code setting(s): " + ", ".join(unknown))
        model = settings.get("model")
        if not isinstance(model, str) or not MODEL_PATTERN.match(model):
            raise ValueError(
                "claude-code setting 'model' is required: the exact model name to pin, "
                'for example "claude-sonnet-5-5"'
            )
        effort = settings.get("effort")
        if effort not in EFFORTS:
            raise ValueError(
                f"claude-code setting 'effort' is required and must be one of {', '.join(EFFORTS)}"
            )
        cap = settings.get("max_budget_usd")
        if isinstance(cap, bool) or not isinstance(cap, (int, float)) or not math.isfinite(cap) or cap <= 0:
            raise ValueError("claude-code setting 'max_budget_usd' is required and must be a number above 0")
        mode = settings.get("permission_mode")
        if mode not in PERMISSION_MODES:
            raise ValueError(
                "claude-code setting 'permission_mode' is required and must be one of "
                + ", ".join(PERMISSION_MODES)
            )
        program = settings.get("program", DEFAULT_PROGRAM)
        if not isinstance(program, str) or not program or program.startswith("-"):
            raise ValueError("claude-code setting 'program' must be a program name or a path")

    @staticmethod
    def build_command(settings: Mapping[str, Any]) -> list[str]:
        return [
            settings.get("program", DEFAULT_PROGRAM),
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            settings["model"],
            "--effort",
            settings["effort"],
            "--max-budget-usd",
            str(settings["max_budget_usd"]),
            "--permission-mode",
            settings["permission_mode"],
            *ISOLATION_FLAGS,
        ]

    def run(self, request: LaunchRequest) -> AdapterResult:
        settings = request.settings
        self.validate_settings(settings)
        template = self.build_command(settings)
        check_no_forbidden_paths(template, request.forbidden_roots)
        env = dict(request.env)
        program = resolve_program(template[0], env)
        if program is None:
            raise ValueError(
                f"'{template[0]}' was not found on the contestant's search path. Give its full path "
                "or put its folder on PATH outside this repository."
            )
        argv = [program, *template[1:]]

        warnings: list[str] = []
        version = self._version(program, request, env, warnings)
        write_transcript_header(request.transcript, template)
        with tempfile.TemporaryFile() as sink:
            outcome = run_bounded(
                argv,
                cwd=request.workspace,
                env=env,
                stdout=sink,
                stdin_bytes=request.brief.encode("utf-8"),
                deadline_seconds=request.time_limit_seconds,
            )
            copy_stream(sink, request.transcript)
            sink.seek(0)
            usage = usage_from_events(parse_events(sink))
        usage.raw["isolation"] = ISOLATION_NOTE
        write_transcript_footer(request.transcript, outcome.exit_status, outcome.timed_out)
        warnings.extend(outcome.warnings)
        if not outcome.started:
            return AdapterResult(
                version=version,
                launch_confirmed=False,
                launch_method="process_started",
                error=f"Could not start the contestant: {outcome.error}",
                warnings=warnings,
            )
        if usage.raw.get("stopped_at_budget"):
            warnings.append("Claude Code stopped at the spend cap before it finished")
        elif usage.raw.get("is_error"):
            warnings.append(
                "Claude Code ended with an error result: " + str(usage.raw.get("subtype", "unknown"))
            )
        elif usage.raw.get("result_event_found") is False and not outcome.timed_out:
            warnings.append("Claude Code printed no result event, so no usage was recorded")
        return AdapterResult(
            prompt_delivery="stdin",
            launch_confirmed=True,
            launch_method="process_started",
            version=version,
            exit_status=outcome.exit_status,
            timed_out=outcome.timed_out,
            usage=usage,
            warnings=warnings,
        )

    @staticmethod
    def _version(
        program: str,
        request: LaunchRequest,
        env: Mapping[str, str],
        warnings: list[str],
    ) -> str | None:
        outcome, text = run_captured(
            [program, "--version"],
            cwd=request.workspace,
            env=env,
            deadline_seconds=VERSION_TIMEOUT_SECONDS,
        )
        warnings.extend(outcome.warnings)
        if not outcome.started or outcome.timed_out or outcome.exit_status != 0:
            return None
        lines = text.strip().splitlines()
        return lines[0].strip() if lines else None

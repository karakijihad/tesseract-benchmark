"""Run Codex CLI headless on a pinned model and effort, with a spend cap.

Codex reports tokens, never dollars, so the cost here is an estimate: the
tokens it reports times the price table in the contestant's settings. The
record says so (`cost_basis` is "estimated").

The brief goes in on standard input and the prompt argument is `-`. The
`codex` program on Windows is a `.cmd` script, and cmd.exe cuts an argument at
its first line break, so an argument prompt cannot carry a brief. `--json`
makes Codex print one JSON event per line.

Two sources of token counts feed the running estimate:

- the `turn.completed` events on standard output, which carry the usage of the
  whole turn and arrive only when a turn ends;
- the session file Codex writes under its home folder, which gets a running
  total after every model call. It is read while the run is going so the cap can
  stop a turn that never ends on its own. If the file cannot be found, only the
  first source is used and the cap can act only between turns.

Stopping a run at the cap needs to see output while the process is running,
which `proc.run_bounded` cannot do (it only waits). `_run_watched` below does the
same job with the same process group, so the process and everything it started
are killed by the same code, and adds a read of the output between waits.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Mapping

from ..proc import POLL_SECONDS, KILL_REAP_SECONDS, SETTLE_SECONDS, Outcome, _new_group, run_captured
from ..record import Usage
from .base import (
    Adapter,
    AdapterResult,
    LaunchRequest,
    check_no_forbidden_paths,
    write_transcript_footer,
    write_transcript_header,
)
from .command import VERSION_TIMEOUT_SECONDS, resolve_program

DEFAULT_PROGRAM = "codex"
MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\[\]-]*$")
EFFORT_PATTERN = re.compile(r"^[A-Za-z]+$")
PRICE_KEYS = ("input", "cached_input", "output")
# Settings the runner itself reads from a contestant entry.
RUNNER_KEYS = ("adapter", "env_passthrough", "unattended_mode")
OWN_KEYS = ("model", "effort", "max_budget_usd", "price_per_million", "program")

# Item types Codex reports for work it did on its own. An item is counted when it
# completes, once per id, so the started and completed events do not count twice.
TOOL_ITEM_TYPES = ("command_execution", "file_change", "mcp_tool_call", "web_search", "collab_tool_call")

ROLLOUT_POLL_SECONDS = 0.5
READ_CHUNK_BYTES = 1024 * 1024
TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _tokens(mapping: Any) -> dict[str, int]:
    source = mapping if isinstance(mapping, dict) else {}
    return {name: _count(source.get(name)) for name in TOKEN_FIELDS}


class Meter:
    """Turns Codex's events into token totals, tool counts and a cost estimate.

    Reasoning tokens are reported inside `output_tokens` (a subset, shown
    separately for information), so they are priced once, as output. Cached
    input is reported inside `input_tokens` too, so it is priced at the cached
    rate and taken out of the full-rate input. Cache writes are reported but
    Codex bills none, so they are kept in the record and not priced.
    """

    def __init__(self, prices: Mapping[str, float]) -> None:
        self.prices = dict(prices)
        self.events = 0
        self.turns: list[dict[str, int]] = []
        self.failures: list[str] = []
        self.tool_ids: dict[str, set[str]] = {kind: set() for kind in TOOL_ITEM_TYPES}
        self.thread_id: str | None = None
        self.rollout_total: dict[str, int] | None = None
        self._pending = b""

    def feed(self, data: bytes) -> None:
        self._pending += data
        *lines, self._pending = self._pending.split(b"\n")
        for line in lines:
            self._line(line)

    def finish(self) -> None:
        pending, self._pending = self._pending, b""
        if pending.strip():
            self._line(pending)

    def _line(self, line: bytes) -> None:
        text = line.strip()
        if not text.startswith(b"{"):
            return
        try:
            event = json.loads(text.decode("utf-8", errors="replace"))
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        self.events += 1
        kind = event.get("type")
        if kind == "thread.started" and isinstance(event.get("thread_id"), str):
            self.thread_id = event["thread_id"]
        elif kind == "turn.completed":
            self.turns.append(_tokens(event.get("usage")))
        elif kind == "turn.failed":
            error = event.get("error")
            message = error.get("message") if isinstance(error, dict) else None
            self.failures.append(message if isinstance(message, str) else "turn failed")
        elif kind == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") in self.tool_ids:
                identity = item.get("id")
                bucket = self.tool_ids[item["type"]]
                bucket.add(identity if isinstance(identity, str) else f"#{len(bucket)}")

    def stdout_total(self) -> dict[str, int]:
        return {name: sum(turn[name] for turn in self.turns) for name in TOKEN_FIELDS}

    def total(self) -> dict[str, int] | None:
        """The most complete token count either source has, or None when neither has any."""
        if not self.turns and self.rollout_total is None:
            return None
        counted = self.stdout_total()
        if self.rollout_total is not None:
            counted = {name: max(counted[name], self.rollout_total[name]) for name in TOKEN_FIELDS}
        return counted

    def cost(self) -> float | None:
        counted = self.total()
        if counted is None:
            return None
        cached = min(counted["cached_input_tokens"], counted["input_tokens"])
        fresh = counted["input_tokens"] - cached
        dollars = (
            fresh * self.prices["input"]
            + cached * self.prices["cached_input"]
            + counted["output_tokens"] * self.prices["output"]
        ) / 1_000_000
        return dollars

    def tool_counts(self) -> dict[str, int]:
        return {kind: len(ids) for kind, ids in self.tool_ids.items() if ids}


class RolloutTail:
    """Reads the running token total from Codex's own session file, if it can be found."""

    def __init__(self, codex_home: Path | None) -> None:
        self.home = codex_home
        self.path: Path | None = None
        self.offset = 0
        self.last_poll = 0.0

    def poll(self, meter: Meter) -> None:
        if self.home is None or meter.thread_id is None:
            return
        now = time.monotonic()
        if now - self.last_poll < ROLLOUT_POLL_SECONDS:
            return
        self.last_poll = now
        try:
            self._read(meter)
        except OSError:
            return

    def _read(self, meter: Meter) -> None:
        if self.path is None:
            found = sorted((self.home / "sessions").glob(f"*/*/*/rollout-*{meter.thread_id}.jsonl"))
            if not found:
                return
            self.path = found[0]
        with open(self.path, "rb") as handle:
            handle.seek(self.offset)
            data = handle.read(READ_CHUNK_BYTES)
        end = data.rfind(b"\n")
        if end < 0:
            return
        self.offset += end + 1
        for line in data[: end + 1].splitlines():
            if b'"token_count"' not in line:
                continue
            try:
                event = json.loads(line.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            payload = event.get("payload") if isinstance(event, dict) else None
            info = payload.get("info") if isinstance(payload, dict) else None
            total = info.get("total_token_usage") if isinstance(info, dict) else None
            if isinstance(payload, dict) and payload.get("type") == "token_count" and isinstance(total, dict):
                meter.rollout_total = _tokens(total)


def codex_home(env: Mapping[str, str]) -> Path | None:
    explicit = env.get("CODEX_HOME")
    if explicit:
        return Path(explicit)
    base = env.get("USERPROFILE") or env.get("HOME")
    return Path(base) / ".codex" if base else None


def _run_watched(
    argv: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    stdin_bytes: bytes,
    deadline_seconds: float,
    transcript: Any,
    meter: Meter,
    tail: RolloutTail,
    max_budget_usd: float,
) -> tuple[Outcome, bool]:
    """`proc.run_bounded` with a look at the output while the process runs.

    Returns the outcome and whether the run was stopped at the spend cap. Every
    property of `run_bounded` holds here as well: the process starts inside a
    group that kills all of it, the group is killed on every way out, standard
    input is a file, output goes to a file and never a pipe, and every wait has a
    deadline. The output file is read by a second handle so the child's write
    position is never disturbed.
    """
    try:
        group = _new_group()
    except Exception as error:
        return Outcome(started=False, error=f"Could not create a process group: {error}"), False
    descriptor, sink_name = tempfile.mkstemp(prefix="codex-out-")
    os.close(descriptor)
    stdin_file = tempfile.TemporaryFile()
    stopped = False
    try:
        stdin_file.write(stdin_bytes)
        stdin_file.flush()
        stdin_file.seek(0)
        with open(sink_name, "wb") as writer, open(sink_name, "rb") as reader:
            try:
                process = group.spawn(argv, os.fspath(cwd), env, stdin_file, writer)
            except (OSError, ValueError) as error:
                return Outcome(started=False, error=f"{type(error).__name__}: {error}"), False
            outcome = Outcome(started=True)

            def drain() -> bool:
                moved = False
                while True:
                    chunk = reader.read(READ_CHUNK_BYTES)
                    if not chunk:
                        return moved
                    moved = True
                    transcript.write(chunk)
                    transcript.flush()
                    meter.feed(chunk)

            deadline = time.monotonic() + max(0.0, deadline_seconds)
            try:
                while True:
                    moved = drain()
                    tail.poll(meter)
                    if process.poll() is not None:
                        break
                    cost = meter.cost()
                    if cost is not None and cost >= max_budget_usd:
                        stopped = True
                        break
                    if time.monotonic() >= deadline:
                        outcome.timed_out = True
                        break
                    if not moved:
                        time.sleep(POLL_SECONDS)
            finally:
                group.kill()
            try:
                code = process.wait(timeout=KILL_REAP_SECONDS)
            except Exception:
                code = None
                outcome.warnings.append("The process did not exit after it was stopped")
            if not outcome.timed_out:
                outcome.exit_status = code
            if not group.wait_empty(SETTLE_SECONDS):
                outcome.warnings.append("Some processes it started could not be confirmed stopped")
            drain()
            meter.finish()
            tail.last_poll = 0.0
            tail.poll(meter)
            return outcome, stopped
    finally:
        group.close()
        stdin_file.close()
        try:
            os.unlink(sink_name)
        except OSError:
            pass


class CodexAdapter(Adapter):
    name = "codex"

    def validate_settings(self, settings: Mapping[str, Any]) -> None:
        unknown = sorted(set(settings) - set(RUNNER_KEYS) - set(OWN_KEYS))
        if unknown:
            raise ValueError("Unknown codex setting(s): " + ", ".join(unknown))
        model = settings.get("model")
        if not isinstance(model, str) or not MODEL_PATTERN.match(model):
            raise ValueError("Codex setting 'model' must be a model name such as \"gpt-6-luna\"")
        effort = settings.get("effort")
        if not isinstance(effort, str) or not EFFORT_PATTERN.match(effort):
            raise ValueError("Codex setting 'effort' must be a word such as \"high\"")
        cap = settings.get("max_budget_usd")
        if not _is_number(cap) or cap <= 0:
            raise ValueError("Codex setting 'max_budget_usd' must be a number above zero")
        prices = settings.get("price_per_million")
        if not isinstance(prices, dict) or sorted(prices) != sorted(PRICE_KEYS):
            raise ValueError(
                "Codex setting 'price_per_million' must hold exactly: " + ", ".join(PRICE_KEYS)
            )
        for key in PRICE_KEYS:
            if not _is_number(prices[key]) or prices[key] < 0:
                raise ValueError(f"Codex price '{key}' must be a number, zero or more, in dollars per million tokens")
        program = settings.get("program", DEFAULT_PROGRAM)
        if not isinstance(program, str) or not program:
            raise ValueError("Codex setting 'program' must be a program name or path")

    @staticmethod
    def command(settings: Mapping[str, Any], program: str | None = None) -> list[str]:
        return [
            program or settings.get("program", DEFAULT_PROGRAM),
            "exec",
            "--json",
            "-m",
            settings["model"],
            "-c",
            f'model_reasoning_effort="{settings["effort"]}"',
            "--sandbox",
            "workspace-write",
            "--skip-git-repo-check",
            "-",
        ]

    def run(self, request: LaunchRequest) -> AdapterResult:
        settings = request.settings
        self.validate_settings(settings)
        template = self.command(settings)
        check_no_forbidden_paths(template, request.forbidden_roots)
        env = dict(request.env)
        program = resolve_program(template[0], env)
        if program is None:
            raise ValueError(
                f"'{template[0]}' was not found on the contestant's search path. Give its full path "
                "or put its folder on PATH outside this repository."
            )
        argv = self.command(settings, program)

        warnings: list[str] = []
        version = self._version(program, request, env, warnings)
        write_transcript_header(request.transcript, template)
        meter = Meter(settings["price_per_million"])
        outcome, stopped = _run_watched(
            argv,
            cwd=request.workspace,
            env=env,
            stdin_bytes=request.brief.encode("utf-8"),
            deadline_seconds=request.time_limit_seconds,
            transcript=request.transcript,
            meter=meter,
            tail=RolloutTail(codex_home(env)),
            max_budget_usd=float(settings["max_budget_usd"]),
        )
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
        if stopped:
            warnings.append("The run was stopped because its estimated cost reached the spend cap")
        warnings.extend(f"Codex reported a failed turn: {message}" for message in meter.failures)
        return AdapterResult(
            prompt_delivery="stdin",
            launch_confirmed=True,
            launch_method="process_started",
            version=version,
            exit_status=outcome.exit_status,
            timed_out=outcome.timed_out,
            usage=build_usage(meter, settings, stopped),
            warnings=warnings,
        )

    @staticmethod
    def _version(
        program: str, request: LaunchRequest, env: Mapping[str, str], warnings: list[str]
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


def build_usage(meter: Meter, settings: Mapping[str, Any], stopped: bool) -> Usage:
    """Normalize what the meter saw. `input_tokens` is the full-rate input only,
    so input, cache reads and output add up the way the other adapters' do."""
    raw: dict[str, Any] = {
        "stopped_at_budget": stopped,
        "max_budget_usd": settings["max_budget_usd"],
        "price_per_million": dict(settings["price_per_million"]),
        "model": settings["model"],
        "effort": settings["effort"],
        "events": meter.events,
        "turns": len(meter.turns),
        "turn_usage": [dict(turn) for turn in meter.turns],
        "tool_calls_by_type": meter.tool_counts(),
    }
    counted = meter.total()
    if meter.rollout_total is not None:
        raw["session_total_usage"] = dict(meter.rollout_total)
    tools = sum(len(ids) for ids in meter.tool_ids.values()) if meter.events else None
    if counted is None:
        return Usage(tool_calls=tools, cost_basis="unavailable", raw=raw)
    cached = min(counted["cached_input_tokens"], counted["input_tokens"])
    raw["reasoning_output_tokens"] = counted["reasoning_output_tokens"]
    cost = meter.cost()
    return Usage(
        input_tokens=counted["input_tokens"] - cached,
        output_tokens=counted["output_tokens"],
        cache_read_tokens=cached,
        cache_write_tokens=counted["cache_write_input_tokens"],
        model_calls=len(meter.turns) or None,
        tool_calls=tools,
        cost_usd=round(cost, 6) if cost is not None else None,
        cost_basis="estimated" if cost is not None else "unavailable",
        raw=raw,
    )

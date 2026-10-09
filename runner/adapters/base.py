"""The contract between the runner and a contestant adapter.

An adapter launches one contestant inside the workspace it is given. It gets
the workspace, the brief, the time limit and its own settings, and nothing
else. It never receives the task folder, the run folder or the validator.

Every process an adapter starts goes through `runner.proc`, which kills the
process and everything it started when the run ends. An adapter that starts
programs any other way breaks the lifecycle rules at the top of
`runner/lifecycle.py`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, BinaryIO, Mapping

from ..isolation import mentions
from ..record import Usage
from ..task import BRIEF_FILENAME


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class LaunchRequest:
    workspace: Path
    brief: str
    time_limit_seconds: float
    settings: Mapping[str, Any]
    env: Mapping[str, str]
    transcript: BinaryIO
    forbidden_roots: tuple[str, ...] = ()

    @property
    def brief_file(self) -> Path:
        return self.workspace / BRIEF_FILENAME


@dataclass
class AdapterResult:
    """What an adapter reports back. None means the adapter cannot tell."""

    prompt_delivery: str | None = None
    launch_confirmed: bool | None = None
    launch_method: str | None = None
    version: str | None = None
    exit_status: int | None = None
    timed_out: bool = False
    error: str | None = None
    usage: Usage = field(default_factory=Usage)
    warnings: list[str] = field(default_factory=list)


class Adapter(ABC):
    name: str = ""

    def validate_settings(self, settings: Mapping[str, Any]) -> None:
        """Raise ValueError for settings this adapter cannot use."""

    @abstractmethod
    def run(self, request: LaunchRequest) -> AdapterResult:
        """Launch the contestant, wait for it or the time limit, report back."""


def write_transcript_header(stream: BinaryIO, command_display: list[str]) -> str:
    """Write the header and flush it before the contestant can write anything."""
    started_at = utc_now()
    header = f"command: {json.dumps(command_display)}\nstarted_at: {started_at}\n\n"
    stream.write(header.encode("utf-8"))
    stream.flush()
    return started_at


def write_transcript_footer(stream: BinaryIO, exit_status: int | None, timed_out: bool) -> None:
    footer = (
        f"\nended_at: {utc_now()}\n"
        f"exit_status: {exit_status}\n"
        f"timed_out: {str(timed_out).lower()}\n"
    )
    stream.write(footer.encode("utf-8"))
    stream.flush()


def check_no_forbidden_paths(argv: list[str], forbidden_roots: tuple[str, ...]) -> None:
    """Refuse a launch argument that names a forbidden folder.

    The program itself may be the interpreter that is running the benchmark,
    which can legitimately live in an environment inside the repository.
    """
    for index, item in enumerate(argv):
        if index == 0 and os.path.normcase(item) == os.path.normcase(sys.executable):
            continue
        if mentions(item, forbidden_roots):
            raise ValueError(
                "A launch argument names the task folder, the run folder or the benchmark "
                "repository. A contestant may be given its own workspace and nothing else."
            )

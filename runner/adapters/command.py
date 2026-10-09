"""Launch a contestant from an argument list in a contestants file."""
from __future__ import annotations

import os
import shutil
from typing import Any, Mapping

from ..proc import run_bounded, run_captured
from .base import (
    Adapter,
    AdapterResult,
    LaunchRequest,
    check_no_forbidden_paths,
    write_transcript_footer,
    write_transcript_header,
)

PROMPT_MODES = ("stdin", "file", "argument")
REMOVED_PLACEHOLDERS = ("{task_dir}", "{run_dir}")
VERSION_TIMEOUT_SECONDS = 20
SHELL_SCRIPT_SUFFIXES = (".cmd", ".bat")


def expand(item: str, workspace: str, brief_file: str, brief: str) -> str:
    return (
        item.replace("{workspace}", workspace)
        .replace("{prompt_file}", brief_file)
        .replace("{prompt}", brief)
    )


def resolve_program(program: str, env: Mapping[str, str]) -> str | None:
    """Find a program on the search path the contestant will have, not the runner's.

    A bare name that is not on that path is None: leaving it to the operating
    system would search the runner's own path, which can hold folders the
    contestant must not see.
    """
    found = shutil.which(program, path=env.get("PATH"))
    if found is not None:
        return os.path.abspath(found)
    return program if os.path.dirname(program) else None


def is_shell_script(program: str) -> bool:
    return program.lower().endswith(SHELL_SCRIPT_SUFFIXES)


ARGUMENT_MODE_REFUSAL = (
    "prompt 'argument' cannot start a .cmd or .bat program: cmd.exe cuts the brief at its "
    "first line break and runs shell characters inside it. Use prompt 'stdin' or 'file'."
)


class CommandAdapter(Adapter):
    name = "command"

    def validate_settings(self, settings: Mapping[str, Any]) -> None:
        argv = settings.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
            raise ValueError("Command setting 'argv' must be a non-empty JSON array of strings")
        for item in argv:
            for removed in REMOVED_PLACEHOLDERS:
                if removed in item:
                    raise ValueError(
                        f"The placeholder {removed} no longer exists. "
                        "A contestant is given its own workspace and nothing else."
                    )
        mode = settings.get("prompt", "stdin")
        if mode not in PROMPT_MODES:
            raise ValueError(f"Command setting 'prompt' must be one of {', '.join(PROMPT_MODES)}")
        joined = "\n".join(argv)
        has_file = "{prompt_file}" in joined
        has_text = "{prompt}" in joined
        if mode == "file" and not has_file:
            raise ValueError("prompt 'file' needs {prompt_file} in argv")
        if mode == "argument" and not has_text:
            raise ValueError("prompt 'argument' needs {prompt} in argv")
        if mode == "stdin" and (has_file or has_text):
            raise ValueError("prompt 'stdin' cannot use {prompt_file} or {prompt} in argv")
        if mode == "argument" and is_shell_script(resolve_program(argv[0], os.environ) or argv[0]):
            raise ValueError(ARGUMENT_MODE_REFUSAL)
        version_argv = settings.get("version_argv")
        if version_argv is not None and (
            not isinstance(version_argv, list)
            or not version_argv
            or not all(isinstance(item, str) for item in version_argv)
        ):
            raise ValueError("Command setting 'version_argv' must be a non-empty JSON array of strings")

    def run(self, request: LaunchRequest) -> AdapterResult:
        settings = request.settings
        self.validate_settings(settings)
        mode = settings.get("prompt", "stdin")
        template: list[str] = list(settings["argv"])
        argv = [
            expand(item, str(request.workspace), str(request.brief_file), request.brief)
            for item in template
        ]
        check_no_forbidden_paths(argv, request.forbidden_roots)
        env = dict(request.env)
        program = resolve_program(argv[0], env)
        if program is None:
            raise ValueError(
                f"'{argv[0]}' was not found on the contestant's search path. Give its full path "
                "or put its folder on PATH outside this repository."
            )
        argv[0] = program
        if mode == "argument" and is_shell_script(argv[0]):
            raise ValueError(ARGUMENT_MODE_REFUSAL)

        warnings: list[str] = []
        version = self._version(settings.get("version_argv"), request, env, warnings)
        write_transcript_header(request.transcript, template)
        outcome = run_bounded(
            argv,
            cwd=request.workspace,
            env=env,
            stdout=request.transcript,
            stdin_bytes=request.brief.encode("utf-8") if mode == "stdin" else None,
            deadline_seconds=request.time_limit_seconds,
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
        return AdapterResult(
            prompt_delivery=mode,
            launch_confirmed=True,
            launch_method="process_started",
            version=version,
            exit_status=outcome.exit_status,
            timed_out=outcome.timed_out,
            warnings=warnings,
        )

    @staticmethod
    def _version(
        version_argv: list[str] | None,
        request: LaunchRequest,
        env: Mapping[str, str],
        warnings: list[str],
    ) -> str | None:
        if not version_argv:
            return None
        check_no_forbidden_paths(version_argv, request.forbidden_roots)
        program = resolve_program(version_argv[0], env)
        if program is None:
            return None
        command = [program, *version_argv[1:]]
        outcome, text = run_captured(
            command,
            cwd=request.workspace,
            env=env,
            deadline_seconds=VERSION_TIMEOUT_SECONDS,
        )
        warnings.extend(outcome.warnings)
        if not outcome.started or outcome.timed_out or outcome.exit_status != 0:
            return None
        lines = text.strip().splitlines()
        return lines[0].strip() if lines else None

"""A contestant that writes fixed files and reports fixed usage. Used by tests."""
from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Mapping

from ..record import Usage
from .base import (
    Adapter,
    AdapterResult,
    LaunchRequest,
    write_transcript_footer,
    write_transcript_header,
)


def _safe_relative(name: str) -> PurePosixPath:
    posix = PurePosixPath(name.replace("\\", "/"))
    if posix.is_absolute() or PureWindowsPath(name).drive or ".." in posix.parts or not posix.parts:
        raise ValueError(f"Stub file path must stay inside the workspace: {name}")
    return posix


class StubAdapter(Adapter):
    name = "stub"

    def validate_settings(self, settings: Mapping[str, Any]) -> None:
        files = settings.get("files", {})
        if not isinstance(files, dict) or not all(isinstance(v, str) for v in files.values()):
            raise ValueError("Stub setting 'files' must map file names to text")
        for name in files:
            _safe_relative(name)
        Usage.from_mapping(settings.get("usage", {}))

    def run(self, request: LaunchRequest) -> AdapterResult:
        settings = request.settings
        self.validate_settings(settings)
        write_transcript_header(request.transcript, ["stub"])
        files = settings.get("files", {})
        for name, content in files.items():
            target = request.workspace.joinpath(*_safe_relative(name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        request.transcript.write(f"stub wrote {len(files)} file(s)\n".encode("utf-8"))
        write_transcript_footer(request.transcript, 0, False)
        return AdapterResult(
            prompt_delivery=settings.get("prompt_delivery", "file"),
            launch_confirmed=True,
            launch_method="in_process",
            version=settings.get("version"),
            exit_status=0,
            usage=Usage.from_mapping(settings.get("usage", {})),
        )

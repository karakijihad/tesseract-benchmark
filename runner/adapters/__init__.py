from __future__ import annotations

from .base import Adapter, AdapterResult, LaunchRequest
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .command import CommandAdapter
from .stub import StubAdapter
from .tesseract import TesseractAdapter

ADAPTERS: dict[str, type[Adapter]] = {
    ClaudeCodeAdapter.name: ClaudeCodeAdapter,
    CodexAdapter.name: CodexAdapter,
    CommandAdapter.name: CommandAdapter,
    StubAdapter.name: StubAdapter,
    TesseractAdapter.name: TesseractAdapter,
}


def get_adapter(name: str) -> Adapter:
    try:
        return ADAPTERS[name]()
    except KeyError:
        known = ", ".join(sorted(ADAPTERS))
        raise ValueError(f"Unknown adapter '{name}'. Known adapters: {known}") from None


__all__ = ["ADAPTERS", "Adapter", "AdapterResult", "LaunchRequest", "get_adapter"]

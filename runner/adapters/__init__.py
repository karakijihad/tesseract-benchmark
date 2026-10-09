from __future__ import annotations

from .base import Adapter, AdapterResult, LaunchRequest
from .command import CommandAdapter
from .stub import StubAdapter

ADAPTERS: dict[str, type[Adapter]] = {
    CommandAdapter.name: CommandAdapter,
    StubAdapter.name: StubAdapter,
}


def get_adapter(name: str) -> Adapter:
    try:
        return ADAPTERS[name]()
    except KeyError:
        known = ", ".join(sorted(ADAPTERS))
        raise ValueError(f"Unknown adapter '{name}'. Known adapters: {known}") from None


__all__ = ["ADAPTERS", "Adapter", "AdapterResult", "LaunchRequest", "get_adapter"]

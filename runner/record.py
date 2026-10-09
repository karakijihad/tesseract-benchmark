"""The canonical run record.

One schema, written down here, describes both files a run produces:

- `<contestant>/record.json`, one per contestant
- `summary.json`, which holds the run summary and every contestant record

Rules the schema enforces:

- Missing is null, never 0. A value the runner or the adapter does not have
  is `None`; a contestant that was not configured has a score of `None`.
- Every path in a record is relative to the run folder.
- `summary.md` is generated from `summary.json` and from nothing else.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
import re
from typing import Any, Iterable, Mapping
from urllib.parse import unquote

from .isolation import mentions

SCHEMA_VERSION = 1

STATUSES = ("completed", "timed_out", "launch_failed", "not_configured")
PROMPT_DELIVERIES = ("stdin", "argument", "file", "socket")
COST_BASES = ("exact", "estimated", "unavailable")

INT_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "model_calls",
    "tool_calls",
    "sub_agents",
)


@dataclass
class Usage:
    """Normalized usage. None means the adapter has no defensible figure."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    model_calls: int | None = None
    tool_calls: int | None = None
    sub_agents: int | None = None
    cost_usd: float | None = None
    cost_basis: str | None = "unavailable"
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def absent(cls) -> "Usage":
        """For a contestant that never ran: nothing was measured, not even a basis."""
        return cls(cost_basis=None)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "Usage":
        known = {item.name for item in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ValueError("Unknown usage field(s): " + ", ".join(unknown))
        usage = cls(**{key: value for key, value in data.items()})
        usage.raw = dict(usage.raw)
        if usage.cost_usd is not None and usage.cost_basis in (None, "unavailable"):
            raise ValueError("A cost needs a cost_basis of exact or estimated")
        return usage

    def to_dict(self) -> dict[str, Any]:
        return {item.name: getattr(self, item.name) for item in fields(self)}


class Opt:
    def __init__(self, spec: Any) -> None:
        self.spec = spec


class OneOf:
    def __init__(self, *values: str, nullable: bool = False) -> None:
        self.values = values
        self.nullable = nullable


class Map:
    """A dict with free keys whose values all follow one spec."""

    def __init__(self, spec: Any) -> None:
        self.spec = spec


USAGE_SCHEMA: dict[str, Any] = {
    **{name: Opt(int) for name in INT_USAGE_FIELDS},
    "cost_usd": Opt(float),
    "cost_basis": OneOf(*COST_BASES, nullable=True),
    "raw": dict,
}

RECORD_SCHEMA: dict[str, Any] = {
    "schema_version": int,
    "contestant": str,
    "status": OneOf(*STATUSES),
    "adapter": Opt(str),
    "model": Opt(str),
    "version": Opt(str),
    "prompt_delivery": OneOf(*PROMPT_DELIVERIES, nullable=True),
    "launch": {"confirmed": Opt(bool), "method": Opt(str)},
    "started_at": Opt(str),
    "ended_at": Opt(str),
    "wall_seconds": Opt(float),
    "exit_status": Opt(int),
    "timed_out": Opt(bool),
    "error": Opt(str),
    "unattended_mode": Opt(str),
    "usage": USAGE_SCHEMA,
    "validation": {
        "score": Opt(float),
        "max_score": Opt(float),
        "checks": Opt([{"name": str, "passed": Opt(bool)}]),
        "error": Opt(str),
    },
    "isolation": {"env_removed": Opt([str])},
    "warnings": Opt([str]),
    "artifacts": {
        "workspace": Opt(str),
        "transcript": Opt(str),
        "validation": Opt(str),
    },
}

SUMMARY_SCHEMA: dict[str, Any] = {
    "schema_version": int,
    "run_id": str,
    "task_id": str,
    "task_title": str,
    "task_digest": str,
    "started_at": str,
    "ended_at": str,
    "time_limit_minutes": float,
    "context": Map(str),
    "contestants": Map(RECORD_SCHEMA),
}


def _describe(spec: Any) -> str:
    if isinstance(spec, type):
        return spec.__name__
    if isinstance(spec, OneOf):
        return "one of " + ", ".join(spec.values)
    if isinstance(spec, Opt):
        return _describe(spec.spec) + " or null"
    if isinstance(spec, list):
        return "list"
    return "object"


def _check(value: Any, spec: Any, path: str, errors: list[str]) -> None:
    if isinstance(spec, Opt):
        if value is not None:
            _check(value, spec.spec, path, errors)
        return
    if isinstance(spec, OneOf):
        if value is None and spec.nullable:
            return
        if value not in spec.values:
            errors.append(f"{path}: expected {_describe(spec)}, got {value!r}")
        return
    if isinstance(spec, Map):
        if not isinstance(value, dict):
            errors.append(f"{path}: expected object")
            return
        for key, item in value.items():
            _check(item, spec.spec, f"{path}.{key}", errors)
        return
    if isinstance(spec, dict):
        if not isinstance(value, dict):
            errors.append(f"{path}: expected object")
            return
        for key in spec:
            if key not in value:
                errors.append(f"{path}.{key}: missing")
        for key in value:
            if key not in spec:
                errors.append(f"{path}.{key}: not in the schema")
        for key, sub in spec.items():
            if key in value:
                _check(value[key], sub, f"{path}.{key}", errors)
        return
    if isinstance(spec, list):
        if not isinstance(value, list):
            errors.append(f"{path}: expected list")
            return
        for index, item in enumerate(value):
            _check(item, spec[0], f"{path}[{index}]", errors)
        return
    if spec is float:
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif spec is int:
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif spec is dict:
        ok = isinstance(value, dict)
    else:
        ok = isinstance(value, spec)
    if not ok:
        errors.append(f"{path}: expected {_describe(spec)}, got {type(value).__name__}")


def validate_record(record: Any) -> list[str]:
    errors: list[str] = []
    _check(record, RECORD_SCHEMA, "record", errors)
    if errors:
        return errors
    usage = record["usage"]
    if usage["cost_usd"] is not None and usage["cost_basis"] in (None, "unavailable"):
        errors.append("record.usage: a cost needs a cost_basis of exact or estimated")
    if usage["cost_usd"] is None and usage["cost_basis"] in ("exact", "estimated"):
        errors.append("record.usage: cost_basis says a cost exists but cost_usd is null")
    if record["schema_version"] != SCHEMA_VERSION:
        errors.append(f"record.schema_version: expected {SCHEMA_VERSION}")
    return errors


def validate_summary(summary: Any) -> list[str]:
    errors: list[str] = []
    _check(summary, SUMMARY_SCHEMA, "summary", errors)
    if errors:
        return errors
    if summary["schema_version"] != SCHEMA_VERSION:
        errors.append(f"summary.schema_version: expected {SCHEMA_VERSION}")
    for name, record in summary["contestants"].items():
        errors.extend(error.replace("record", f"summary.contestants.{name}", 1) for error in validate_record(record))
    return errors


ABSOLUTE_PATH = re.compile(
    r"(?<![\w:/.\-])(?:[A-Za-z]:[\\/]|\\\\|/(?=[\w.\-~]))[^\s\"'<>|*?,;)\]}]*"
)
FILE_URL = re.compile(r"file:/{1,3}[^\s\"'<>|*?,;)\]}]*", re.IGNORECASE)
TOKEN = re.compile(r"[^\s\"'<>|*?,;)\]}]+")
DIR_MARK = "<dir>"
PATH_MARK = "<path>"


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _decoded(token: str) -> str:
    for _ in range(3):
        again = unquote(token)
        if again == token:
            break
        token = again
    return token


def _sweep(text: str, variants: tuple[str, ...]) -> tuple[str, list[str]]:
    """The one matcher: returns the text with every path replaced, and what was found.

    Four passes, in order: the known folders in any spelling, `file:` URLs,
    percent-encoded words that decode to a path, and any other absolute path.
    Both `scrub` and the refuse-to-write guard call this, so what the guard
    rejects is exactly what the scrub removes.
    """
    found: list[str] = []

    def take(mark: str):  # type: ignore[no-untyped-def]
        def replace(match: re.Match[str]) -> str:
            found.append(match.group(0))
            return mark

        return replace

    forms = sorted((form for form in variants if form), key=len, reverse=True)
    if forms:
        body = "|".join(re.escape(form).replace("/", r"[\\/]+") for form in forms)
        text = re.sub(
            rf"(?<![A-Za-z0-9_])(?:{body})(?![A-Za-z0-9_\-])",
            take(DIR_MARK),
            text,
            flags=re.IGNORECASE,
        )
    text = FILE_URL.sub(take(PATH_MARK), text)

    def decode_word(match: re.Match[str]) -> str:
        word = match.group(0)
        if "%" not in word:
            return word
        plain = _decoded(word)
        if plain != word and (
            FILE_URL.search(plain) or ABSOLUTE_PATH.search(plain) or mentions(plain, forms)
        ):
            found.append(word)
            return PATH_MARK
        return word

    text = TOKEN.sub(decode_word, text)
    text = ABSOLUTE_PATH.sub(take(PATH_MARK), text)
    return text, found


def absolute_paths(value: Any, variants: Iterable[str] = ()) -> list[str]:
    """Every path found in the keys and strings of a JSON-shaped value."""
    forms = tuple(variants)
    return [item for text in _strings(value) for item in _sweep(text, forms)[1]]


def scrub_text(text: str, variants: Iterable[str]) -> str:
    """Replace known folders, then any other path, with a placeholder."""
    return _sweep(text, tuple(variants))[0]


def scrub(value: Any, variants: Iterable[str]) -> Any:
    """Return a copy of a JSON-shaped value with paths removed from its strings and keys."""
    forms = tuple(variants)
    if isinstance(value, str):
        return _sweep(value, forms)[0]
    if isinstance(value, dict):
        cleaned: dict[Any, Any] = {}
        for key, item in value.items():
            name = _sweep(key, forms)[0] if isinstance(key, str) else key
            while name in cleaned:  # two keys can scrub to the same text; keep both
                name = f"{name}#"
            cleaned[name] = scrub(item, forms)
        return cleaned
    if isinstance(value, list):
        return [scrub(item, forms) for item in value]
    return value


def blank_record(contestant: str, status: str = "not_configured") -> dict[str, Any]:
    """A record with nothing measured: every value that is not known is null."""
    return {
        "schema_version": SCHEMA_VERSION,
        "contestant": contestant,
        "status": status,
        "adapter": None,
        "model": None,
        "version": None,
        "prompt_delivery": None,
        "launch": {"confirmed": None, "method": None},
        "started_at": None,
        "ended_at": None,
        "wall_seconds": None,
        "exit_status": None,
        "timed_out": None,
        "error": None,
        "unattended_mode": None,
        "usage": Usage.absent().to_dict(),
        "validation": {"score": None, "max_score": None, "checks": None, "error": None},
        "isolation": {"env_removed": None},
        "warnings": None,
        "artifacts": {"workspace": None, "transcript": None, "validation": None},
    }

"""Workspace and environment isolation.

What this guarantees: no path to the task folder (and so the validator), the
run folder, the output folder or the benchmark repository is given to the
contestant, in its arguments or its environment, and none can be derived from
them. The workspace is a fresh random directory under the system temp
directory, unrelated to any of those folders. The environment is an allow
list: only operating system essentials, the names a contestant's settings
list in `env_passthrough` and the BENCHMARK_ variables are passed on, and a
search path loses every entry under a forbidden folder.

What this is not: an operating system sandbox. A contestant that searches the
disk, or runs with the user's full rights, is not stopped by this module.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import functools
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import time
from typing import Iterable, Mapping
from urllib.parse import unquote

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_PREFIX = "agentbench-"
LINK_ATTRIBUTE = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT: symlinks, junctions, mount points

ALLOWED_NAMES = frozenset(
    {
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "TEMP", "TMP",
        "USERPROFILE", "USERNAME", "USERDOMAIN", "COMPUTERNAME", "HOME", "HOMEDRIVE",
        "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "ALLUSERSPROFILE", "PUBLIC",
        "PROGRAMW6432", "COMMONPROGRAMW6432", "NUMBER_OF_PROCESSORS", "OS",
        "USER", "LOGNAME", "SHELL", "TMPDIR", "TERM", "TZ", "LANG", "LANGUAGE",
    }
)
ALLOWED_PREFIXES = ("LC_", "PROGRAMFILES", "COMMONPROGRAMFILES", "PROCESSOR_", "BENCHMARK_")

# What the validator needs beyond the essentials: where a browser it drives is installed.
VALIDATOR_PASSTHROUGH = ("PLAYWRIGHT_BROWSERS_PATH",)

_SEPARATORS = re.compile(r"(?<=.)/{2,}")


def _normal(text: str) -> str:
    """One spelling for comparison: decoded, forward slashes, lower case, no extended prefix."""
    for _ in range(2):
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    text = text.replace("\\", "/")
    lowered = text.lower()
    for prefix in ("//?/unc/", "//?/", "//./"):
        if lowered.startswith(prefix):
            text = ("//" if prefix.endswith("unc/") else "") + text[len(prefix):]
            break
    return _SEPARATORS.sub("/", text).lower()


def _short_name(path: str) -> str | None:
    if sys.platform != "win32":
        return None
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetShortPathNameW(path, buffer, len(buffer))  # type: ignore[attr-defined]
    return buffer.value if 0 < length < len(buffer) else None


def path_variants(path: str | Path) -> set[str]:
    """Spellings of one path a program might see: native, forward slash, MSYS, 8.3, any case."""
    forms: set[str] = set()
    candidates = {str(path), str(Path(path).resolve())}
    for candidate in list(candidates):
        short = _short_name(candidate)
        if short:
            candidates.add(short)
    for candidate in candidates:
        text = _normal(candidate).rstrip("/")
        if not text:
            continue
        forms.add(text)
        if len(text) > 2 and text[1] == ":" and text[2] == "/":
            forms.add("/" + text[0] + text[2:])
    return forms


def variants_for(roots: Iterable[str | Path]) -> tuple[str, ...]:
    forms: set[str] = set()
    for root in roots:
        forms |= path_variants(root)
    return tuple(sorted(forms))


@functools.lru_cache(maxsize=64)
def mention_pattern(variants: tuple[str, ...]) -> re.Pattern[str] | None:
    """Matches a forbidden path spelling in already normalised text, on whole names only."""
    forms = sorted((form for form in variants if form), key=len, reverse=True)
    if not forms:
        return None
    body = "|".join(re.escape(form) for form in forms)
    return re.compile(rf"(?<![a-z0-9_])(?:{body})(?![a-z0-9_\-])")


def mentions(text: str, variants: Iterable[str]) -> bool:
    """True when the text contains any spelling of a forbidden path."""
    pattern = mention_pattern(tuple(variants))
    return bool(pattern and pattern.search(_normal(text)))


def is_under(entry: str, variants: Iterable[str]) -> bool:
    """True when a search path entry is a forbidden folder or sits inside one."""
    forms = tuple(variants)
    candidates = [entry.strip().strip('"')]
    try:
        if candidates[0]:
            candidates.append(str(Path(candidates[0]).resolve()))
    except (OSError, ValueError):
        pass
    for candidate in candidates:
        text = _normal(candidate).rstrip("/")
        if any(form and (text == form or text.startswith(form + "/")) for form in forms):
            return True
    return False


def forbidden_roots(
    task_root: Path, run_root: Path, output_root: Path, repo_root: Path = REPO_ROOT
) -> tuple[Path, ...]:
    """The folders a contestant must not be able to name or find in its environment."""
    roots = [task_root, run_root, output_root, repo_root]
    for folder in sorted({Path(sys.prefix), Path(sys.executable).parent}):
        resolved, root = folder.resolve(), repo_root.resolve()
        if resolved == root or root in resolved.parents:
            roots.append(folder)
    return tuple(roots)


def validate_passthrough(names: object, label: str) -> tuple[str, ...]:
    if names is None:
        return ()
    if not isinstance(names, list) or not all(isinstance(item, str) for item in names):
        raise ValueError(f"{label}: 'env_passthrough' must be a list of variable names")
    for name in names:
        if not name or "=" in name or any(char.isspace() for char in name):
            raise ValueError(f"{label}: '{name}' is not a usable variable name")
    return tuple(names)


def is_allowed_name(name: str, passthrough: Iterable[str] = ()) -> bool:
    upper = name.upper()
    if upper in ALLOWED_NAMES or upper.startswith(ALLOWED_PREFIXES):
        return True
    return upper in {item.upper() for item in passthrough}


def clean_environment(
    environ: Mapping[str, str],
    forbidden: Iterable[str],
    extra: Mapping[str, str],
    passthrough: Iterable[str] = (),
) -> tuple[dict[str, str], list[str]]:
    """Build a contestant's or a validator's environment from an allow list.

    A variable that is not on the list is left out. A search path loses only
    the entries under a forbidden folder, and any other variable that names a
    forbidden folder is left out whole. Returns the environment and the names
    (names only) of everything that was left out or shortened.
    """
    variants = tuple(forbidden)
    passed = tuple(passthrough)
    env: dict[str, str] = {}
    removed: list[str] = []
    for name, value in environ.items():
        if not is_allowed_name(name, passed):
            removed.append(name)
            continue
        if os.pathsep in value:
            parts = value.split(os.pathsep)
            kept = [
                part for part in parts if not (is_under(part, variants) or mentions(part, variants))
            ]
            if len(kept) != len(parts):
                removed.append(name)
            if kept:
                env[name] = os.pathsep.join(kept)
        elif mentions(value, variants):
            removed.append(name)
        else:
            env[name] = value
    env.update(extra)
    return env, sorted(set(removed))


def create_workspace() -> Path:
    return Path(tempfile.mkdtemp(prefix=WORKSPACE_PREFIX)).resolve()


def assert_unrelated(workspace: Path, *others: Path) -> None:
    resolved = workspace.resolve()
    for other in others:
        target = other.resolve()
        if resolved == target or target in resolved.parents or resolved in target.parents:
            raise ValueError(
                "The workspace must not sit inside, or contain, the task, run, output or "
                "repository folder. Point TEMP and TMP somewhere else."
            )


def long_path(path: str | Path) -> str:
    """The path in a form Windows accepts beyond 260 characters; unchanged elsewhere."""
    text = os.path.abspath(path)
    if sys.platform != "win32" or text.startswith("\\\\?\\"):
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def remove_tree(path: Path) -> None:
    def make_writable(function, target, _error):  # type: ignore[no-untyped-def]
        os.chmod(target, stat.S_IWRITE)
        function(target)

    target = long_path(path)
    if os.path.lexists(target):
        shutil.rmtree(target, onexc=make_writable)


def cleanup_tree(path: Path, attempts: int = 3, pause: float = 0.3) -> str | None:
    """Remove a directory; return a warning, never raise. Windows can hold a file briefly."""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            remove_tree(path)
            return None
        except Exception as error:  # a locked file must cost a warning, not the run
            last = error
            if attempt + 1 < attempts:
                time.sleep(pause)
    return (
        f"Could not remove the temporary folder {path.name} from the system temp directory "
        f"({type(last).__name__}). Delete it by hand."
    )


@dataclass
class CopyReport:
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    files: int = 0


def _is_link(entry: os.DirEntry[str]) -> bool:
    if entry.is_symlink():
        return True
    try:
        attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attributes & LINK_ATTRIBUTE)


def copy_without_links(source: Path, destination: Path) -> CopyReport:
    """Copy a directory tree, leaving out every link, junction and special file.

    The contestant controls what is in its workspace, so a link in it could
    point anywhere on the disk. Nothing is followed or recreated; the relative
    name of each entry that was left out is reported.
    """
    report = CopyReport()
    root_source, root_destination = long_path(source), long_path(destination)
    os.makedirs(root_destination, exist_ok=True)
    pending = [(root_source, root_destination, "")]
    while pending:
        directory, target, prefix = pending.pop()
        try:
            with os.scandir(directory) as scan:
                entries = sorted(scan, key=lambda item: item.name)
        except OSError:
            report.failed.append(prefix or ".")
            continue
        for entry in entries:
            name = f"{prefix}/{entry.name}" if prefix else entry.name
            if _is_link(entry):
                report.skipped.append(name)
            elif entry.is_dir(follow_symlinks=False):
                try:
                    os.makedirs(os.path.join(target, entry.name), exist_ok=True)
                except OSError:
                    report.failed.append(name)
                    continue
                pending.append((entry.path, os.path.join(target, entry.name), name))
            elif entry.is_file(follow_symlinks=False):
                try:
                    shutil.copy2(entry.path, os.path.join(target, entry.name))
                    report.files += 1
                except OSError:
                    report.failed.append(name)
            else:
                report.skipped.append(name)
    return report

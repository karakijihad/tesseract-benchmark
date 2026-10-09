"""Refuse a push that carries private details.

Looks for absolute home paths on any drive, shared-folder paths, email
addresses and secrets. Extra patterns (names, hostnames, usernames) can be
listed one regex per line in `.public-check.local`, which is gitignored so the
list itself stays private.

Two modes:

    python scripts/check_public.py
        scans every tracked file as it is on disk.

    python scripts/check_public.py --pre-push <remote> <url>
        is what the pre-push hook runs. Git writes one line per ref being
        pushed to standard input: <local ref> <local sha> <remote ref> <remote sha>.
        Every file version and every commit message that the push would add to
        the remote is scanned, from the commits themselves rather than from the
        working tree, so a private detail that was committed and then removed
        is still found.

A file that is not valid UTF-8 is scanned as UTF-16 or UTF-32 when it has a
byte order mark or null bytes, and with replacement characters otherwise. No
file is skipped.
"""
from __future__ import annotations

import codecs
from pathlib import Path
import re
import subprocess
import sys
from typing import IO, Iterable, Sequence

ROOT = Path(__file__).resolve().parent.parent
LOCAL_PATTERNS = ".public-check.local"
EXEMPT = {"LICENSE", "scripts/check_public.py"}

PATTERNS = [
    r"[A-Za-z]:[\\/]+Users[\\/]+",
    r"[A-Za-z]%3A(?:%5C|%2F)+Users(?:%5C|%2F)",
    r"%USERPROFILE%",
    r"\\\\[A-Za-z0-9._-]+\\[A-Za-z0-9$._-]+",
    r"\\\\\\\\[A-Za-z0-9._-]+\\\\[A-Za-z0-9$._-]+",
    r"/Users/[^/\s]+/",
    r"/home/[^/\s]+/",
    # An email address, unless it is a no-reply address (commit trailers and
    # GitHub's private author addresses), which names no one's inbox.
    r"(?<![A-Za-z0-9._%+-])(?![A-Za-z0-9._%+-]*no-?reply@)(?![A-Za-z0-9._%+-]+@users\.noreply\.github\.com\b)"
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[opsu]_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,})",
    r"\bAKIA[0-9A-Z]{16}\b",
    r"\bgithub_pat_[A-Za-z0-9_]{20,}",
    r"\bxox[abpr]-[A-Za-z0-9-]{10,}",
]


class CheckError(Exception):
    """A git command failed. The push is refused rather than waved through."""


class Pattern:
    def __init__(self, source: str, label: str) -> None:
        self.regex = re.compile(source, re.IGNORECASE)
        self.label = label


def load_patterns(root: Path) -> list[Pattern]:
    patterns = [Pattern(source, source) for source in PATTERNS]
    local = root / LOCAL_PATTERNS
    if local.exists():
        number = 0
        for line in local.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                number += 1
                try:
                    patterns.append(Pattern(line, f"local pattern {number}"))
                except re.error as error:
                    raise CheckError(f"local pattern {number} is not a valid regex: {error}") from None
    return patterns


def decode_candidates(data: bytes) -> list[str]:
    """Every reasonable reading of the bytes. A file is never skipped for its encoding."""
    if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return [data.decode("utf-32", errors="replace")]
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return [data.decode("utf-16", errors="replace")]
    if data.startswith(codecs.BOM_UTF8):
        return [data.decode("utf-8-sig", errors="replace")]
    texts = [data.decode("utf-8", errors="replace")]
    if b"\x00" in data:
        texts.extend(data.decode(name, errors="replace") for name in ("utf-16-le", "utf-16-be"))
    return texts


def scan_bytes(label: str, data: bytes, patterns: Sequence[Pattern]) -> list[str]:
    hits: list[str] = []
    seen: set[tuple[int, str]] = set()
    for text in decode_candidates(data):
        for number, line in enumerate(text.splitlines(), start=1):
            for pattern in patterns:
                if pattern.regex.search(line) and (number, pattern.label) not in seen:
                    seen.add((number, pattern.label))
                    hits.append(f"{label}:{number}: matches {pattern.label}")
    return hits


def git(root: Path, *args: str, data: bytes | None = None) -> bytes:
    done = subprocess.run(
        ["git", "-c", "core.quotepath=off", *args],
        cwd=root,
        input=data,
        capture_output=True,
        check=False,
    )
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", errors="replace").strip()
        raise CheckError(f"git {args[0]} failed: {detail}")
    return done.stdout


def scan_tracked(root: Path, patterns: Sequence[Pattern]) -> tuple[list[str], int]:
    names = [n for n in git(root, "ls-files", "-z").decode("utf-8").split("\0") if n]
    hits: list[str] = []
    count = 0
    for name in names:
        if name in EXEMPT:
            continue
        path = root / name
        if not path.is_file():
            continue
        count += 1
        hits.extend(scan_bytes(name, path.read_bytes(), patterns))
    return hits, count


def is_zero(sha: str) -> bool:
    return bool(sha) and set(sha) == {"0"}


def exclusions(root: Path, remote_name: str | None, remote_sha: str) -> list[str]:
    """What the remote already has: its old tip, or every ref we know it holds."""
    if not is_zero(remote_sha):
        known = subprocess.run(
            ["git", "cat-file", "-e", f"{remote_sha}^{{commit}}"], cwd=root, capture_output=True
        )
        if known.returncode == 0:
            return [remote_sha]
    return [f"--remotes={remote_name}" if remote_name else "--remotes"]


def read_objects(root: Path, shas: Iterable[str]) -> Iterable[tuple[str, str, bytes]]:
    """Yield (sha, type, content) for each object, using one git process."""
    wanted = list(shas)
    if not wanted:
        return
    out = git(root, "cat-file", "--batch", data=("\n".join(wanted) + "\n").encode("ascii"))
    position = 0
    while position < len(out):
        newline = out.index(b"\n", position)
        header = out[position:newline].decode("ascii").split()
        if len(header) != 3:
            raise CheckError(f"git cat-file could not read an object: {' '.join(header)}")
        sha, kind, size = header[0], header[1], int(header[2])
        start = newline + 1
        yield sha, kind, out[start : start + size]
        position = start + size + 1


def scan_push(
    root: Path, lines: Iterable[str], remote_name: str | None, patterns: Sequence[Pattern]
) -> tuple[list[str], int]:
    paths: dict[str, str] = {}
    for line in lines:
        fields = line.split()
        if len(fields) != 4:
            continue
        _local_ref, local_sha, _remote_ref, remote_sha = fields
        if is_zero(local_sha):
            continue
        listing = git(
            root, "rev-list", "--objects", local_sha, "--not", *exclusions(root, remote_name, remote_sha)
        ).decode("utf-8", errors="replace")
        for entry in listing.splitlines():
            sha, _, path = entry.partition(" ")
            paths.setdefault(sha, path)
    hits: list[str] = []
    for sha, kind, content in read_objects(root, paths):
        path = paths[sha]
        if kind == "blob" and path not in EXEMPT:
            hits.extend(scan_bytes(f"{path} (blob {sha[:7]})", content, patterns))
        elif kind == "commit":
            message = content.split(b"\n\n", 1)[1] if b"\n\n" in content else b""
            hits.extend(scan_bytes(f"commit {sha[:7]} message", message, patterns))
    return hits, len(paths)


def main(
    argv: Sequence[str] | None = None,
    *,
    root: Path = ROOT,
    stdin: IO[str] | None = None,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    pre_push = "--pre-push" in args
    rest = [item for item in args if item != "--pre-push"]
    remote_name = rest[0] if rest else None
    try:
        patterns = load_patterns(root)
        if pre_push:
            lines = (stdin if stdin is not None else sys.stdin).read().splitlines()
            hits, count = scan_push(root, lines, remote_name, patterns)
            scope = f"{count} objects in the push"
        else:
            hits, count = scan_tracked(root, patterns)
            scope = "tracked files"
    except CheckError as error:
        print(f"Could not check for private details, so the push is refused: {error}", file=sys.stderr)
        return 1
    if hits:
        print("Private details found. Remove them before pushing:", file=sys.stderr)
        print("\n".join(hits), file=sys.stderr)
        return 1
    print(f"clean: {len(patterns)} patterns over {scope}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

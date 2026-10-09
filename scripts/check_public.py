"""Refuse a push that carries private details.

Scans every tracked file for absolute home paths, email addresses and secrets.
Extra patterns (names, hostnames, usernames) can be listed one regex per line in
`.public-check.local`, which is gitignored so the list itself stays private.

Run: python scripts/check_public.py
"""
from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
LOCAL_PATTERNS = ROOT / ".public-check.local"
EXEMPT = {"LICENSE", "scripts/check_public.py"}

PATTERNS = [
    r"[A-Za-z]:[\\/]+Users[\\/]+",
    r"/Users/[^/\s]+/",
    r"/home/[^/\s]+/",
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[opsu]_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,})",
]


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout
    return [name for name in out.decode("utf-8").split("\0") if name]


def load_patterns() -> list[re.Pattern[str]]:
    sources = list(PATTERNS)
    if LOCAL_PATTERNS.exists():
        for line in LOCAL_PATTERNS.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                sources.append(line)
    return [re.compile(source, re.IGNORECASE) for source in sources]


def main() -> int:
    patterns = load_patterns()
    hits = []
    for name in tracked_files():
        if name in EXEMPT:
            continue
        try:
            text = (ROOT / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            for pattern in patterns:
                if pattern.search(line):
                    hits.append(f"{name}:{number}: matches {pattern.pattern}")
    if hits:
        print("Private details found. Remove them before pushing:", file=sys.stderr)
        print("\n".join(hits), file=sys.stderr)
        return 1
    print(f"clean: {len(patterns)} patterns over tracked files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

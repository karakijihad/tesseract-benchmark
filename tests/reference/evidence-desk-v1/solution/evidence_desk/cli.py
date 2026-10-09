from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .core import InputError, load_records, select, to_json, to_markdown


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m evidence_desk", description="Report on evidence records.")
    parser.add_argument("--input", required=True, type=Path, help="JSON file of evidence records")
    parser.add_argument("--status", help="keep records with this status")
    parser.add_argument("--severity", help="keep records with this severity")
    parser.add_argument("--category", help="keep records in this category")
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    parser.add_argument("--output", required=True, type=Path, help="where to write the report")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        records = load_records(args.input)
    except InputError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    chosen = select(records, args.status, args.severity, args.category)
    text = to_json(chosen) if args.format == "json" else to_markdown(chosen)
    args.output.write_text(text, encoding="utf-8")
    if not chosen:
        print("No records match the filters. The report is empty.")
    return 0

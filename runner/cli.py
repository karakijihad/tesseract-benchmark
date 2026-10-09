from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .engine import load_contestants, load_summary, parse_context, run_benchmark
from .report import render_comparison
from .task import TaskSpec


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-bench")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    validate = subparsers.add_parser("validate-task")
    validate.add_argument("--task", required=True)

    run = subparsers.add_parser("run")
    run.add_argument("--task", required=True)
    run.add_argument("--contestants-file", required=True)
    run.add_argument("--contestants", nargs="+", default=["tesseract", "claude-code", "codex"])
    run.add_argument("--time-limit", type=int, default=None)
    run.add_argument("--output", default="runs")
    run.add_argument(
        "--context",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="free-form context to record, for example the commit of the contestant under test",
    )

    compare = subparsers.add_parser("compare")
    compare.add_argument("--run", action="append", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.subcommand == "validate-task":
            task = TaskSpec.load(args.task)
            print(
                json.dumps(
                    {
                        "task_id": task.task_id,
                        "title": task.title,
                        "digest": task.digest(),
                        "time_limit_minutes": task.time_limit_minutes,
                        "validator_timeout_seconds": task.validator_timeout_seconds,
                        "research_allowed": task.research_allowed,
                        "runtime_network_allowed": task.runtime_network_allowed,
                    },
                    indent=2,
                )
            )
            return 0
        if args.subcommand == "run":
            task = TaskSpec.load(args.task)
            contestants = load_contestants(Path(args.contestants_file).resolve())
            context = parse_context(args.context)
            run_root = run_benchmark(
                task,
                contestants,
                args.contestants,
                Path(args.output).resolve(),
                args.time_limit,
                context,
            )
            print(run_root)
            return 0
        if args.subcommand == "compare":
            summaries = [load_summary(Path(item).resolve()) for item in args.run]
            print(render_comparison(summaries))
            return 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

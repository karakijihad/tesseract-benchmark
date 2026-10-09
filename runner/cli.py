from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .engine import compare_run, load_commands, run_benchmark
from .task import TaskSpec


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-bench")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    validate = subparsers.add_parser("validate-task")
    validate.add_argument("--task", required=True)

    run = subparsers.add_parser("run")
    run.add_argument("--task", required=True)
    run.add_argument("--commands", required=True)
    run.add_argument("--contestants", nargs="+", default=["tesseract", "claude-code", "codex"])
    run.add_argument("--time-limit", type=int, default=None)
    run.add_argument("--output", default="runs")

    compare = subparsers.add_parser("compare")
    compare.add_argument("--run", required=True)
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
                        "research_allowed": task.research_allowed,
                        "runtime_network_allowed": task.runtime_network_allowed,
                    },
                    indent=2,
                )
            )
            return 0
        if args.subcommand == "run":
            task = TaskSpec.load(args.task)
            commands = load_commands(Path(args.commands).resolve())
            run_root = run_benchmark(
                task,
                commands,
                args.contestants,
                Path(args.output).resolve(),
                args.time_limit,
            )
            print(run_root)
            return 0
        if args.subcommand == "compare":
            print(compare_run(Path(args.run).resolve()))
            return 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Validator for science-decay-v1.

Usage: python validator/run.py --workspace <dir> --output <json>

Checks the files in the finished workspace. The contestant's final message is
never read. Needs Pillow for the image check.

The result file is always written. If the validator cannot run (Pillow is not
installed) the file holds {"score": null, "max_score": N, "checks": {},
"error": "..."} and the exit status is 2. Files that are missing, malformed or
absurd are never that error: they fail checks.
"""
from __future__ import annotations

import argparse
from collections import OrderedDict
import json
import math
import os
from pathlib import Path
import re

HERE = Path(__file__).resolve().parent
INSTALL_HINT = "Install it with: pip install -r requirements-validators.txt"
NUMERIC_FIELDS = (
    "half_life_s",
    "half_life_uncertainty_s",
    "background_rate_per_s",
    "background_uncertainty_per_s",
)
URL_PATTERN = re.compile(r"https?://[^\s<>()\[\]\"'`]+", re.IGNORECASE)
HOST_PATTERN = re.compile(r"https?://[^/\s]+\.[^/\s]+", re.IGNORECASE)
MAX_TEXT_BYTES = 5_000_000
MAX_IMAGE_PIXELS = 16_000_000


class EnvironmentProblem(Exception):
    """The validator itself cannot run. The message says what is missing and how to install it."""


def load_json(path: Path):
    try:
        if path.stat().st_size > MAX_TEXT_BYTES:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # missing, unreadable, not JSON, too deeply nested: all mean no usable results
        return None


def finite_number(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, ValueError):  # an integer too large for a float
        return False


def check_results_json(results, details) -> bool:
    if not isinstance(results, dict):
        details["results_json"] = "results.json is missing or is not a JSON object"
        return False
    missing = [name for name in NUMERIC_FIELDS if not finite_number(results.get(name))]
    method = results.get("method")
    if not isinstance(method, str) or not method.strip():
        missing.append("method")
    if missing:
        details["results_json"] = "missing or invalid fields: " + ", ".join(missing)
        return False
    return True


def check_value(results, field, truth, tolerance, details, key) -> bool:
    value = results.get(field) if isinstance(results, dict) else None
    if not finite_number(value):
        details[key] = f"{field} is missing or is not a finite number"
        return False
    error = abs(value - truth) / truth
    details[key] = f"{field}={value:.4g}, relative error {error:.1%}, tolerance {tolerance:.0%}"
    return error <= tolerance


def check_uncertainty(results, field, reference, expected, details, key) -> bool:
    value = results.get(field) if isinstance(results, dict) else None
    if not finite_number(value) or value <= 0:
        details[key] = f"{field} must be a positive number"
        return False
    low = reference * expected["uncertainty_low_factor"]
    high = reference * expected["uncertainty_high_factor"]
    details[key] = f"{field}={value:.4g}, accepted range {low:.4g} to {high:.4g}"
    return low <= value <= high


def check_png(path: Path, details) -> bool:
    from PIL import Image

    if not path.is_file():
        details["fit_png"] = "fit.png is missing"
        return False
    try:
        with Image.open(path) as image:
            if image.format != "PNG":
                details["fit_png"] = f"fit.png is {image.format}, not PNG"
                return False
            width, height = image.size
            if width * height > MAX_IMAGE_PIXELS:
                details["fit_png"] = f"fit.png is {width}x{height}, larger than a plot needs to be"
                return False
            image.load()
            colours = image.convert("RGB").getcolors(maxcolors=1_000_000)
    except Exception as error:  # an unreadable image is a failed check, never a crash
        details["fit_png"] = f"fit.png cannot be read: {type(error).__name__}: {str(error)[:200]}"
        return False
    if width < 400 or height < 300:
        details["fit_png"] = f"fit.png is only {width}x{height}"
        return False
    if colours is not None and len(colours) < 3:
        details["fit_png"] = "fit.png is blank or nearly blank"
        return False
    return True


def read_report(workspace: Path) -> str:
    try:
        with (workspace / "report.md").open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(MAX_TEXT_BYTES)
    except OSError:
        return ""


def distinct_urls(text: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in URL_PATTERN.findall(text):
        cleaned = match.rstrip(".,;:!?*_")
        if HOST_PATTERN.match(cleaned) and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            urls.append(cleaned)
    return urls


def check_report_method(text: str, details) -> bool:
    lowered = text.lower()
    if len(text.strip()) < 400:
        details["report_method"] = "report.md is missing or too short to explain a method"
        return False
    topics = {
        "half-life": ("half-life", "half life", "halflife"),
        "background": ("background",),
        "fit": ("fit", "regression", "likelihood", "least squares", "least-squares"),
        "uncertainty": ("uncertaint", "error", "standard deviation", "sigma", "confidence"),
    }
    absent = [name for name, words in topics.items() if not any(word in lowered for word in words)]
    if absent:
        details["report_method"] = "report.md does not mention: " + ", ".join(absent)
        return False
    return True


def score_weights() -> OrderedDict:
    scoring = json.loads((HERE.parent / "scoring.json").read_text(encoding="utf-8"))
    return OrderedDict(scoring["checks"])


def write_json(output: Path, result: dict) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(result, indent=2)
    partial = output.with_name(output.name + ".part")
    partial.write_text(text, encoding="utf-8")
    os.replace(partial, output)
    print(text)


def write_result(output: Path, weights, checks, details) -> int:
    points = {name: (weights[name] if checks.get(name) else 0) for name in weights}
    result = {
        "score": sum(points.values()),
        "max_score": sum(weights.values()),
        "checks": {name: bool(checks.get(name)) for name in weights},
        "points": points,
        "details": {key: str(value) for key, value in details.items() if value},
    }
    write_json(output, result)
    return 0 if result["score"] == result["max_score"] else 1


def write_environment_error(output: Path, weights, message: str) -> int:
    write_json(output, {"score": None, "max_score": sum(weights.values()) if weights else None, "checks": {}, "error": message})
    return 2


def guarded(checks: dict, details: dict, name: str, function) -> None:
    try:
        checks[name] = bool(function())
    except Exception as error:  # odd contestant files fail the check and never crash the validator
        checks[name] = False
        details[name] = f"{type(error).__name__}: {str(error)[:300]}"


def validate(workspace: Path, checks: dict, details: dict) -> None:
    try:
        import PIL.Image  # noqa: F401
    except Exception as error:
        raise EnvironmentProblem(f"Pillow is not installed ({type(error).__name__}). {INSTALL_HINT}") from error

    expected = json.loads((HERE / "expected.json").read_text(encoding="utf-8"))
    results = load_json(workspace / "results.json")
    guarded(checks, details, "results_json", lambda: check_results_json(results, details))
    guarded(
        checks,
        details,
        "half_life",
        lambda: check_value(results, "half_life_s", expected["half_life_s"], expected["half_life_tolerance_fraction"], details, "half_life"),
    )
    guarded(
        checks,
        details,
        "background",
        lambda: check_value(
            results, "background_rate_per_s", expected["background_rate_per_s"], expected["background_tolerance_fraction"], details, "background"
        ),
    )
    guarded(
        checks,
        details,
        "half_life_uncertainty",
        lambda: check_uncertainty(
            results, "half_life_uncertainty_s", expected["reference_half_life_uncertainty_s"], expected, details, "half_life_uncertainty"
        ),
    )
    guarded(
        checks,
        details,
        "background_uncertainty",
        lambda: check_uncertainty(
            results, "background_uncertainty_per_s", expected["reference_background_uncertainty_per_s"], expected, details, "background_uncertainty"
        ),
    )
    guarded(checks, details, "fit_png", lambda: check_png(workspace / "fit.png", details))
    report = read_report(workspace)
    guarded(checks, details, "report_method", lambda: check_report_method(report, details))

    def sources():
        urls = distinct_urls(report)
        details["report_sources"] = f"{len(urls)} distinct http(s) URLs found in report.md"
        return len(urls) >= 2

    guarded(checks, details, "report_sources", sources)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    weights = None
    checks: dict = {}
    details: dict = {}
    try:
        weights = score_weights()
        validate(workspace, checks, details)
    except EnvironmentProblem as problem:
        return write_environment_error(args.output, weights, str(problem))
    except Exception as error:  # a validator bug still leaves a result holding what was scored
        details["validator"] = f"validator error: {type(error).__name__}: {str(error)[:300]}"
        if weights is None:
            return write_environment_error(args.output, weights, details["validator"])
    return write_result(args.output, weights, checks, details)


if __name__ == "__main__":
    raise SystemExit(main())

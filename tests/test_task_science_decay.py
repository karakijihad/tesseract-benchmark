"""The half-life validator loses exactly the checks a defect in the answer breaks, and no others.

The accepted ranges are read from the validator's expected.json, so a case sits
just inside or just outside the range the validator itself states. Files that
are malformed or absurd (not a number, a number too large to hold, JSON nested
too deep, an image that is not a PNG) get a result file instead of a crash, and
a missing Pillow is reported as an environment error rather than a failed plot.
"""
from __future__ import annotations

import io
import json
import math
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "reference"))

from harness import REFERENCE, TASKS, clone, failed, make_workspace, run_validator, shadow_module, validate, why  # noqa: E402

TASK = "science-decay-v1"
EXPECTED = json.loads((TASKS / TASK / "validator" / "expected.json").read_text(encoding="utf-8"))
CHECKS = list(json.loads((TASKS / TASK / "scoring.json").read_text(encoding="utf-8"))["checks"])
HALF_LIFE = EXPECTED["half_life_s"]
BACKGROUND = EXPECTED["background_rate_per_s"]
HALF_LIFE_TOLERANCE = EXPECTED["half_life_tolerance_fraction"]
BACKGROUND_TOLERANCE = EXPECTED["background_tolerance_fraction"]
LOW, HIGH = EXPECTED["uncertainty_low_factor"], EXPECTED["uncertainty_high_factor"]
HALF_LIFE_SIGMA = EXPECTED["reference_half_life_uncertainty_s"]
BACKGROUND_SIGMA = EXPECTED["reference_background_uncertainty_per_s"]
INSIDE, OUTSIDE = 0.98, 1.02  # fractions of an edge of an accepted range


@pytest.fixture(scope="module")
def reference(tmp_path_factory):
    return make_workspace(TASK, tmp_path_factory.mktemp("science-reference"), solved=True)


def png_bytes(width=900, height=560, noisy=True, format="PNG") -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height), "white")
    if noisy:
        draw = ImageDraw.Draw(image)
        draw.line([(10, 10), (width - 10, height - 10)], fill=(200, 0, 0), width=3)
        draw.ellipse([20, 20, 60, 60], fill=(0, 0, 200))
    buffer = io.BytesIO()
    image.save(buffer, format=format)
    return buffer.getvalue()


def variant(reference: Path, tmp_path: Path, results=None, png=None, report=None) -> Path:
    """A copy of the reference answer. results: a dict of fields to change, or raw text or bytes for the whole file."""
    workspace = clone(reference, tmp_path)
    if results is not None:
        path = workspace / "results.json"
        if isinstance(results, dict):
            path.write_text(json.dumps({**json.loads(path.read_text(encoding="utf-8")), **results}), encoding="utf-8")
        else:
            path.write_bytes(results if isinstance(results, bytes) else results.encode("utf-8"))
    if png is not None:
        (workspace / "fit.png").write_bytes(png)
    if report is not None:
        (workspace / "report.md").write_text(report, encoding="utf-8")
    return workspace


LONG_METHOD = (
    "# Report\n\nThe half-life comes from a fit of the counts with a background term. The uncertainty is the standard "
    "deviation from the fit. " + "The model describes the data well, and the residuals look random. " * 8
)
TWO_SOURCES = "\n\nSources:\n- https://en.wikipedia.org/wiki/Exponential_decay\n- https://en.wikipedia.org/wiki/Poisson_regression\n"


# ------------------------------------------------------------------ one defect, one check


DEFECTS = {
    "a results file without a method": ({"results": {"method": ""}}, {"results_json"}),
    "a half-life just inside the tolerance above": ({"results": {"half_life_s": HALF_LIFE * (1 + HALF_LIFE_TOLERANCE * INSIDE)}}, set()),
    "a half-life just inside the tolerance below": ({"results": {"half_life_s": HALF_LIFE * (1 - HALF_LIFE_TOLERANCE * INSIDE)}}, set()),
    "a half-life just outside the tolerance above": ({"results": {"half_life_s": HALF_LIFE * (1 + HALF_LIFE_TOLERANCE * OUTSIDE)}}, {"half_life"}),
    "a half-life just outside the tolerance below": ({"results": {"half_life_s": HALF_LIFE * (1 - HALF_LIFE_TOLERANCE * OUTSIDE)}}, {"half_life"}),
    "a background just inside the tolerance above": ({"results": {"background_rate_per_s": BACKGROUND * (1 + BACKGROUND_TOLERANCE * INSIDE)}}, set()),
    "a background just inside the tolerance below": ({"results": {"background_rate_per_s": BACKGROUND * (1 - BACKGROUND_TOLERANCE * INSIDE)}}, set()),
    "a background just outside the tolerance above": ({"results": {"background_rate_per_s": BACKGROUND * (1 + BACKGROUND_TOLERANCE * OUTSIDE)}}, {"background"}),
    "a background just outside the tolerance below": ({"results": {"background_rate_per_s": BACKGROUND * (1 - BACKGROUND_TOLERANCE * OUTSIDE)}}, {"background"}),
    "a half-life uncertainty just above the lowest accepted": ({"results": {"half_life_uncertainty_s": HALF_LIFE_SIGMA * LOW / INSIDE}}, set()),
    "a half-life uncertainty below the lowest accepted": ({"results": {"half_life_uncertainty_s": HALF_LIFE_SIGMA * LOW * INSIDE}}, {"half_life_uncertainty"}),
    "a half-life uncertainty just below the highest accepted": ({"results": {"half_life_uncertainty_s": HALF_LIFE_SIGMA * HIGH * INSIDE}}, set()),
    "a half-life uncertainty above the highest accepted": ({"results": {"half_life_uncertainty_s": HALF_LIFE_SIGMA * HIGH * OUTSIDE}}, {"half_life_uncertainty"}),
    "a half-life uncertainty of zero": ({"results": {"half_life_uncertainty_s": 0}}, {"half_life_uncertainty"}),
    "a negative half-life uncertainty": ({"results": {"half_life_uncertainty_s": -HALF_LIFE_SIGMA}}, {"half_life_uncertainty"}),
    "a background uncertainty just above the lowest accepted": ({"results": {"background_uncertainty_per_s": BACKGROUND_SIGMA * LOW / INSIDE}}, set()),
    "a background uncertainty below the lowest accepted": ({"results": {"background_uncertainty_per_s": BACKGROUND_SIGMA * LOW * INSIDE}}, {"background_uncertainty"}),
    "a background uncertainty just below the highest accepted": ({"results": {"background_uncertainty_per_s": BACKGROUND_SIGMA * HIGH * INSIDE}}, set()),
    "a background uncertainty above the highest accepted": ({"results": {"background_uncertainty_per_s": BACKGROUND_SIGMA * HIGH * OUTSIDE}}, {"background_uncertainty"}),
    "a background uncertainty of zero": ({"results": {"background_uncertainty_per_s": 0}}, {"background_uncertainty"}),
    "a plot that is all white": ({"png": png_bytes(noisy=False)}, {"fit_png"}),
    "a plot that is a JPEG named fit.png": ({"png": png_bytes(format="JPEG")}, {"fit_png"}),
    "a plot that is not an image": ({"png": b"not an image at all"}, {"fit_png"}),
    "a plot that is empty": ({"png": b""}, {"fit_png"}),
    "a plot that is too small": ({"png": png_bytes(width=120, height=90)}, {"fit_png"}),
    "a plot of absurd size": ({"png": png_bytes(width=5000, height=5000)}, {"fit_png"}),
    "a report too short to explain a method": ({"report": "Half-life fit with background and uncertainty." + TWO_SOURCES}, {"report_method"}),
    "a report that never mentions the background": (
        {"report": LONG_METHOD.replace("background", "baseline") + TWO_SOURCES},
        {"report_method"},
    ),
    "a report that never mentions uncertainty": (
        {"report": LONG_METHOD.replace("uncertainty", "spread").replace("standard deviation", "spread").replace("error", "gap") + TWO_SOURCES},
        {"report_method"},
    ),
    "a report with one source": ({"report": LONG_METHOD + "\n\nSource: https://en.wikipedia.org/wiki/Exponential_decay\n"}, {"report_sources"}),
    "a report with the same source twice": (
        {"report": LONG_METHOD + "\n- https://en.wikipedia.org/wiki/Exponential_decay\n- HTTPS://EN.WIKIPEDIA.ORG/WIKI/Exponential_decay.\n"},
        {"report_sources"},
    ),
    "a report with addresses that are not web pages": (
        {"report": LONG_METHOD + "\n- ftp://example.org/data\n- http://localhost/notes\n- see Wikipedia\n"},
        {"report_sources"},
    ),
    "a report with two sources": ({"report": LONG_METHOD + TWO_SOURCES}, set()),
}


@pytest.mark.parametrize("name", sorted(DEFECTS))
def test_a_defect_loses_the_check_it_breaks(name, reference, tmp_path):
    change, expected = DEFECTS[name]
    result = validate(TASK, variant(reference, tmp_path, **change), tmp_path)
    assert failed(result) == expected, why(result)


def test_every_scored_check_has_a_defect_that_loses_it():
    covered = set().union(*(expected for _, expected in DEFECTS.values()))
    assert covered == set(CHECKS), sorted(set(CHECKS) - covered)


def test_the_stated_tolerances_are_what_the_cases_assume():
    assert 0 < HALF_LIFE_TOLERANCE < 1 and 0 < BACKGROUND_TOLERANCE < 1 and 0 < LOW < 1 < HIGH


def test_a_fit_that_ignores_the_background_is_rejected(reference, tmp_path):
    import numpy as np

    rows = [line.split(",") for line in (reference / "data.csv").read_text(encoding="utf-8").splitlines()[1:]]
    time = np.array([(float(a) + float(b)) / 2 for a, b, _ in rows])
    counts = np.array([float(c) for _, _, c in rows])
    slope, _ = np.polyfit(time, np.log(counts), 1)
    naive = math.log(2) / -slope
    assert abs(naive - HALF_LIFE) / HALF_LIFE > HALF_LIFE_TOLERANCE, naive
    result = validate(TASK, variant(reference, tmp_path, results={"half_life_s": naive}), tmp_path)
    assert failed(result) == {"half_life"}, why(result)


def test_the_expected_answer_matches_the_data_generator():
    params = json.loads((REFERENCE / TASK / "params.json").read_text(encoding="utf-8"))
    assert EXPECTED["half_life_s"] == params["half_life_s"]
    assert EXPECTED["background_rate_per_s"] == params["background_rate_per_s"]
    sys.path.insert(0, str(REFERENCE / TASK))
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        import generate_data

        text = generate_data.csv_text()
    finally:
        sys.dont_write_bytecode = previous
        sys.path.remove(str(REFERENCE / TASK))
        sys.modules.pop("generate_data", None)
    assert (TASKS / TASK / "starter" / "data.csv").read_text(encoding="utf-8") == text


# ------------------------------------------------------------ results.json that is not a result

NUMBERS = ("half_life_s", "half_life_uncertainty_s", "background_rate_per_s", "background_uncertainty_per_s")
ALL_NUMERIC = {"results_json", "half_life", "background", "half_life_uncertainty", "background_uncertainty"}

ABSURD = {
    "NaN": '{"half_life_s": NaN, "half_life_uncertainty_s": NaN, "background_rate_per_s": NaN, "background_uncertainty_per_s": NaN, "method": "fit"}',
    "Infinity": '{"half_life_s": Infinity, "half_life_uncertainty_s": -Infinity, "background_rate_per_s": Infinity, "background_uncertainty_per_s": 1e999, "method": "fit"}',
    "huge integers": json.dumps({**dict.fromkeys(NUMBERS, 10**400), "method": "fit"}),
    "numbers as strings": json.dumps({**dict.fromkeys(NUMBERS, "150"), "method": "fit"}),
    "lists for numbers": json.dumps({**dict.fromkeys(NUMBERS, [150, 2]), "method": ["fit"]}),
    "objects for numbers": json.dumps({**dict.fromkeys(NUMBERS, {"value": 150}), "method": {"a": 1}}),
    "nulls": json.dumps({**dict.fromkeys(NUMBERS), "method": None}),
    "booleans": json.dumps({**dict.fromkeys(NUMBERS, True), "method": True}),
    "nested too deep to read": "[" * 200000 + "]" * 200000,
    "a list": "[1, 2, 3]",
    "a number": "150",
    "a string": '"done"',
    "null": "null",
    "empty": "",
    "not JSON": "half_life = 150",
    "not UTF-8": b"\xff\xfe\x00{",
    "a very long number": '{"half_life_s": ' + "9" * 4000 + "}",
}


@pytest.mark.parametrize("name", sorted(ABSURD))
def test_a_results_file_that_is_not_usable_scores_the_numbers_zero_and_still_gets_a_result(name, reference, tmp_path):
    result = validate(TASK, variant(reference, tmp_path, results=ABSURD[name]), tmp_path)
    assert failed(result) == ALL_NUMERIC, why(result)
    assert result["checks"]["fit_png"] and result["checks"]["report_method"] and result["checks"]["report_sources"]
    assert "Error" not in " ".join(result["details"].values()), "an exception was caught instead of the file being judged"


def test_numbers_that_are_valid_but_absurd_lose_every_range_check(reference, tmp_path):
    huge = dict.fromkeys(NUMBERS, 1.7e308) | {"background_rate_per_s": -1.7e308}
    result = validate(TASK, variant(reference, tmp_path, results=huge), tmp_path)
    assert failed(result) == ALL_NUMERIC - {"results_json"}, why(result)


@pytest.mark.parametrize("kind", ["missing", "directory"])
def test_a_results_file_that_cannot_be_read_scores_the_numbers_zero(kind, reference, tmp_path):
    workspace = variant(reference, tmp_path)
    (workspace / "results.json").unlink()
    if kind == "directory":
        (workspace / "results.json").mkdir()
    result = validate(TASK, workspace, tmp_path)
    assert failed(result) == ALL_NUMERIC, why(result)


@pytest.mark.parametrize("name", ["fit.png", "report.md"])
def test_a_missing_file_loses_only_its_checks(name, reference, tmp_path):
    workspace = variant(reference, tmp_path)
    (workspace / name).unlink()
    expected = {"fit_png"} if name == "fit.png" else {"report_method", "report_sources"}
    assert failed(validate(TASK, workspace, tmp_path)) == expected


def test_a_report_that_is_not_text_loses_only_its_checks(reference, tmp_path):
    workspace = variant(reference, tmp_path)
    (workspace / "report.md").write_bytes(bytes([0xFF, 0xFE, 0xFD]) * 400)
    result = validate(TASK, workspace, tmp_path)
    assert failed(result) == {"report_method", "report_sources"}


def test_a_report_that_is_a_directory_loses_only_its_checks(reference, tmp_path):
    workspace = variant(reference, tmp_path)
    (workspace / "report.md").unlink()
    (workspace / "report.md").mkdir()
    assert failed(validate(TASK, workspace, tmp_path)) == {"report_method", "report_sources"}


# ------------------------------------------------------------ environment


def test_a_missing_pillow_is_an_environment_error_not_a_failed_plot(reference, tmp_path):
    process, result = run_validator(TASK, clone(reference, tmp_path), tmp_path, shadow_module(tmp_path, "PIL"))
    assert process.returncode != 0
    assert result["score"] is None
    assert result["max_score"] == 100
    assert result["checks"] == {}
    assert "Pillow" in result["error"] and "pip install" in result["error"]


def test_a_missing_pillow_is_reported_even_when_there_is_no_plot(reference, tmp_path):
    workspace = clone(reference, tmp_path)
    (workspace / "fit.png").unlink()
    process, result = run_validator(TASK, workspace, tmp_path, shadow_module(tmp_path, "PIL"))
    assert process.returncode != 0 and result["score"] is None and "Pillow" in result["error"]

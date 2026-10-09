"""The slide deck validator loses exactly the checks a defect in the deck breaks, and no others.

Every scored check has a case where the reference deck with one defect loses
that check. Decks built differently from the reference but still correct (a
title in a text box, sources listed by name) keep full marks. Files that are
not decks get a result file instead of a crash, and a missing python-pptx is
reported as an environment error rather than a failed deck.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
import re
import shutil
import sys

import pytest
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.util import Inches, Pt

sys.path.insert(0, str(Path(__file__).resolve().parent / "reference"))

from harness import TASKS, clone, failed, make_workspace, run_validator, shadow_module, validate, why  # noqa: E402

TASK = "slide-deck-v1"
CHECKS = list(json.loads((TASKS / TASK / "scoring.json").read_text(encoding="utf-8"))["checks"])
EXPECTED = json.loads((TASKS / TASK / "validator" / "expected.json").read_text(encoding="utf-8"))
SOURCE_NAMES = ["Wikipedia", "National Park Service", "Golden Gate Bridge Highway and Transportation District"]


@pytest.fixture(scope="module")
def reference(tmp_path_factory):
    return make_workspace(TASK, tmp_path_factory.mktemp("deck-reference"), solved=True)


def build(reference: Path, tmp_path: Path, change) -> Path:
    """A copy of the reference workspace whose deck has been changed by change(presentation)."""
    workspace = clone(reference, tmp_path)
    deck = Presentation(str(workspace / "deck.pptx"))
    change(deck)
    deck.save(str(workspace / "deck.pptx"))
    return workspace


def delete_slide(deck, index: int) -> None:
    slide_ids = deck.slides._sldIdLst
    entry = slide_ids[index]
    deck.part.drop_rel(entry.rId)
    slide_ids.remove(entry)


def add_slide_before_last(deck, text: str, notes: str = "Words the presenter would say about this slide.") -> None:
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(7), Inches(1))
    box.text_frame.text = text
    slide.notes_slide.notes_text_frame.text = notes
    slide_ids = deck.slides._sldIdLst
    entry = slide_ids[-1]
    slide_ids.remove(entry)
    slide_ids.insert(len(slide_ids) - 1, entry)


def all_text(slide) -> str:
    parts = []
    for shape in slide.shapes:
        if shape.has_text_frame:
            parts.append(shape.text_frame.text)
    if slide.has_notes_slide:
        parts.append(slide.notes_slide.notes_text_frame.text)
    return "\n".join(parts)


def with_slide_count(count: int):
    """Set the slide count without losing a fact: deleted slides hand their words to the notes of the table slide."""

    def change(deck):
        while len(deck.slides) > count:
            doomed = deck.slides[1]
            table_slide = deck.slides[len(deck.slides) - 2]
            table_slide.notes_slide.notes_text_frame.text += "\n" + all_text(doomed)
            delete_slide(deck, 1)
        while len(deck.slides) < count:
            add_slide_before_last(deck, "An extra slide with its own words.")

    return change


def replace_words(deck, pattern: str, replacement: str) -> None:
    """Rewrite text on slides and in notes."""
    expression = re.compile(pattern)
    for slide in deck.slides:
        frames = [shape.text_frame for shape in slide.shapes if shape.has_text_frame]
        if slide.has_notes_slide:
            frames.append(slide.notes_slide.notes_text_frame)
        for frame in frames:
            for paragraph in frame.paragraphs:
                for run in paragraph.runs:
                    run.text = expression.sub(replacement, run.text)


def remove_graphic_frames(deck) -> None:
    for slide in deck.slides:
        for shape in list(slide.shapes):
            if shape.has_chart or shape.has_table:
                shape._element.getparent().remove(shape._element)


def picture_bytes() -> io.BytesIO:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (320, 200), (200, 60, 60)).save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


def picture_instead_of_data(deck) -> None:
    for slide in deck.slides:
        had = any(shape.has_chart or shape.has_table for shape in slide.shapes)
        if had:
            for shape in list(slide.shapes):
                if shape.has_chart or shape.has_table:
                    shape._element.getparent().remove(shape._element)
            slide.shapes.add_picture(picture_bytes(), Inches(1), Inches(2), Inches(6))


def wrong_numbers(deck) -> None:
    for slide in deck.slides:
        for shape in slide.shapes:
            if shape.has_table:
                for row_index, row in enumerate(shape.table.rows):
                    if row_index == 0:
                        continue
                    row.cells[2].text = str(1900 + row_index)
                    row.cells[3].text = str(900 + row_index * 7)
            if shape.has_chart:
                data = CategoryChartData()
                data.categories = ["A", "B", "C", "D", "E", "F"]
                data.add_series("Main span (m)", [901, 1100, 1250, 1333, 1500, 1750])
                shape.chart.replace_data(data)


def set_sources_slide(deck, title: str, lines: list[str]) -> None:
    slide = deck.slides[len(deck.slides) - 1]
    slide.shapes.title.text = title
    frame = slide.placeholders[1].text_frame
    frame.text = lines[0]
    for line in lines[1:]:
        frame.add_paragraph().text = line


def text_box_title(deck, title_first: bool = True) -> None:
    """A title slide made of two text boxes and no placeholders."""
    slide = deck.slides[0]
    for shape in list(slide.shapes):
        shape._element.getparent().remove(shape._element)
    big = slide.shapes.add_textbox(Inches(1), Inches(3 if title_first else 0.5), Inches(8), Inches(1.2))
    big.text_frame.text = "The Golden Gate Bridge"
    big.text_frame.paragraphs[0].runs[0].font.size = Pt(44)
    small = slide.shapes.add_textbox(Inches(1), Inches(0.5 if title_first else 3), Inches(8), Inches(0.6))
    small.text_frame.text = "History and engineering"
    small.text_frame.paragraphs[0].runs[0].font.size = Pt(20)


def picture_only_first_slide(deck) -> None:
    slide = deck.slides[0]
    for shape in list(slide.shapes):
        shape._element.getparent().remove(shape._element)
    slide.shapes.add_picture(picture_bytes(), Inches(1), Inches(1), Inches(6))


def empty_middle_slide(deck) -> None:
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    slide.notes_slide.notes_text_frame.text = "Notes on a slide that shows nothing at all."
    slide_ids = deck.slides._sldIdLst
    entry = slide_ids[-1]
    slide_ids.remove(entry)
    slide_ids.insert(4, entry)


def clear_notes(deck, indexes: list[int]) -> None:
    for index in indexes:
        deck.slides[index].notes_slide.notes_text_frame.text = ""


# ------------------------------------------------------------------ one defect, one check


DEFECTS = {
    "seven slides, one too few": (with_slide_count(7), {"slide_count"}),
    "thirteen slides, one too many": (with_slide_count(13), {"slide_count"}),
    "a first slide with no text at all": (picture_only_first_slide, {"title_slide"}),
    "an empty slide in the middle": (empty_middle_slide, {"no_empty_slides"}),
    "one middle slide without notes": (lambda deck: clear_notes(deck, [3]), {"speaker_notes"}),
    "every middle slide without notes": (lambda deck: clear_notes(deck, range(1, len(deck.slides) - 1)), {"speaker_notes"}),
    "a picture where the chart and table were": (picture_instead_of_data, {"chart_or_table", "data_matches_source"}),
    "a chart and table with other numbers": (wrong_numbers, {"data_matches_source"}),
    "no mention of International Orange": (lambda deck: replace_words(deck, r"International Orange", "a bright colour"), {"required_facts"}),
    "no mention of the 27,572 wires": (lambda deck: replace_words(deck, r"27,?572", "many"), {"required_facts"}),
    "a last slide that is not the sources": (lambda deck: set_sources_slide(deck, "Thank you", ["Questions are welcome"]), {"sources_slide"}),
    "a sources slide with one source": (lambda deck: set_sources_slide(deck, "Sources", ["Wikipedia: https://en.wikipedia.org/wiki/Golden_Gate_Bridge"]), {"sources_slide"}),
    "sources named on a slide that is not about sources": (lambda deck: set_sources_slide(deck, "Thank you", SOURCE_NAMES), {"sources_slide"}),
}


@pytest.mark.parametrize("name", sorted(DEFECTS))
def test_a_defect_loses_the_check_it_breaks(name, reference, tmp_path):
    change, expected = DEFECTS[name]
    result = validate(TASK, build(reference, tmp_path, change), tmp_path)
    assert failed(result) == expected, why(result)


def test_a_file_that_is_not_a_deck_loses_the_opens_check_and_everything_after(reference, tmp_path):
    workspace = clone(reference, tmp_path)
    (workspace / "deck.pptx").write_bytes(b"this is not a zip file")
    result = validate(TASK, workspace, tmp_path)
    assert result["score"] == 0
    assert "cannot be opened" in result["details"]["opens"]


def test_every_scored_check_has_a_defect_that_loses_it(reference, tmp_path):
    covered = {"opens"}
    for _, expected in DEFECTS.values():
        covered |= expected
    assert covered == set(CHECKS), sorted(set(CHECKS) - covered)


def test_notes_are_scored_by_how_many_slides_have_them(reference, tmp_path):
    weight = json.loads((TASKS / TASK / "scoring.json").read_text(encoding="utf-8"))["checks"]["speaker_notes"]
    (tmp_path / "none").mkdir()
    (tmp_path / "one").mkdir()
    none = validate(TASK, build(reference, tmp_path / "none", lambda deck: clear_notes(deck, range(1, len(deck.slides) - 1))), tmp_path / "none")
    one = validate(TASK, build(reference, tmp_path / "one", lambda deck: clear_notes(deck, [3])), tmp_path / "one")
    assert none["points"]["speaker_notes"] == 0
    assert 0 < one["points"]["speaker_notes"] < weight


@pytest.mark.parametrize(
    ("count", "passes"),
    [(7, False), (8, True), (12, True), (13, False)],
)
def test_the_slide_count_limits_are_inclusive(count, passes, reference, tmp_path):
    result = validate(TASK, build(reference, tmp_path, with_slide_count(count)), tmp_path)
    assert result["checks"]["slide_count"] is passes
    assert failed(result) - {"slide_count"} == set(), why(result)


# ------------------------------------------------------------ correct decks built another way


def test_a_title_in_a_text_box_counts_as_a_title(reference, tmp_path):
    result = validate(TASK, build(reference, tmp_path, text_box_title), tmp_path)
    assert failed(result) == set(), why(result)
    assert "The Golden Gate Bridge" in result["details"]["title_slide"]


def test_the_largest_text_wins_over_the_top_most_when_there_is_no_title_placeholder(reference, tmp_path):
    result = validate(TASK, build(reference, tmp_path, lambda deck: text_box_title(deck, title_first=False)), tmp_path)
    assert failed(result) == set(), why(result)
    assert "The Golden Gate Bridge" in result["details"]["title_slide"]


def test_sources_listed_by_name_without_addresses_pass(reference, tmp_path):
    result = validate(TASK, build(reference, tmp_path, lambda deck: set_sources_slide(deck, "Sources", SOURCE_NAMES)), tmp_path)
    assert failed(result) == set(), why(result)


def test_sources_named_in_lower_case_and_with_a_comma_pass(reference, tmp_path):
    names = ["wikipedia, the free encyclopedia", "the national park service", "golden gate bridge, highway and transportation district"]
    result = validate(TASK, build(reference, tmp_path, lambda deck: set_sources_slide(deck, "Sources", names)), tmp_path)
    assert failed(result) == set(), why(result)


def test_sources_listed_by_address_only_pass(reference, tmp_path):
    lines = ["https://en.wikipedia.org/wiki/Golden_Gate_Bridge", "https://www.nps.gov/goga/", "https://www.goldengate.org/"]
    result = validate(TASK, build(reference, tmp_path, lambda deck: set_sources_slide(deck, "Sources", lines)), tmp_path)
    assert failed(result) == set(), why(result)


def test_a_deck_with_only_a_chart_or_only_a_table_passes_the_data_checks(reference, tmp_path):
    def drop(kind):
        def change(deck):
            for slide in deck.slides:
                for shape in list(slide.shapes):
                    if getattr(shape, "has_" + kind):
                        shape._element.getparent().remove(shape._element)
            add_slide_before_last(deck, "Filler so the deck keeps its length.")

        return change

    for index, kind in enumerate(("chart", "table")):
        folder = tmp_path / kind
        folder.mkdir()
        result = validate(TASK, build(reference, folder, drop(kind)), folder)
        assert result["checks"]["chart_or_table"] and result["checks"]["data_matches_source"], (kind, why(result))


# ------------------------------------------------------------ files that are not decks


@pytest.mark.parametrize("kind", ["missing", "empty", "text", "zip", "directory"])
def test_a_deck_that_cannot_be_opened_gets_a_result(kind, reference, tmp_path):
    workspace = clone(reference, tmp_path)
    path = workspace / "deck.pptx"
    path.unlink()
    if kind == "empty":
        path.write_bytes(b"")
    elif kind == "text":
        path.write_text("slides", encoding="utf-8")
    elif kind == "zip":
        shutil.make_archive(str(tmp_path / "plain"), "zip", str(workspace))
        shutil.copy(tmp_path / "plain.zip", path)
    elif kind == "directory":
        path.mkdir()
    result = validate(TASK, workspace, tmp_path)
    assert result["score"] == 0
    assert result["checks"]["opens"] is False
    assert "error" not in result


def test_a_deck_with_no_slides_gets_a_result(reference, tmp_path):
    def empty(deck):
        while len(deck.slides):
            delete_slide(deck, 0)

    result = validate(TASK, build(reference, tmp_path, empty), tmp_path)
    assert result["score"] == 0
    assert result["checks"]["opens"] is False


# ------------------------------------------------------------ environment


def test_a_missing_python_pptx_package_is_an_environment_error_not_a_failed_deck(reference, tmp_path):
    process, result = run_validator(TASK, clone(reference, tmp_path), tmp_path, shadow_module(tmp_path, "pptx"))
    assert process.returncode != 0
    assert result["score"] is None
    assert result["max_score"] == 100
    assert result["checks"] == {}
    assert "python-pptx" in result["error"] and "pip install" in result["error"]


def test_a_missing_python_pptx_is_reported_even_when_there_is_no_deck(tmp_path):
    (tmp_path / "empty").mkdir()
    process, result = run_validator(TASK, tmp_path / "empty", tmp_path, shadow_module(tmp_path, "pptx"))
    assert process.returncode != 0 and result["score"] is None and "python-pptx" in result["error"]

"""The 2048 validator loses exactly the checks a defect in the page breaks, and no others.

Every scored check has a case where a correct page with one defect loses that
check. Pages that behave differently from the reference but are still correct
(split files, animated drawing, other button labels) keep full marks. A page
that never returns from a call still gets a result file, well inside the
validator's time limit.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "reference"))

from harness import (  # noqa: E402
    REFERENCE,
    TASKS,
    ScenarioRuns,
    clone,
    failed,
    kill_processes_mentioning,
    make_workspace,
    processes_mentioning,
    run_validator,
    shadow_module,
    validate,
    why,
)

TASK = "browser-game-v1"
REFERENCE_PAGE = (REFERENCE / TASK / "solution" / "index.html").read_text(encoding="utf-8")
CHECKS = list(json.loads((TASKS / TASK / "scoring.json").read_text(encoding="utf-8"))["checks"])

# A page without window.game cannot be played at all, so only what can be said of the page itself survives.
EVERYTHING_THE_PAGE_ALONE_DOES_NOT_DECIDE = set(CHECKS) - {"page_loads", "no_network"}

# Each defect: (old text, new text) edits to the reference page, and the checks it must lose.
# "Exactly" means the failed set equals the expected set. Where one defect cannot be told apart from
# its consequences (a wrong merge also gives a wrong spawn, because a spawn is judged on the merged board),
# the consequences are listed.
DEFECTS = {
    "pairs merge from the far wall": (
        [
            ("var tiles = line.filter(function (v) { return v !== 0; });", "var tiles = line.filter(function (v) { return v !== 0; }).reverse();"),
            ("while (out.length < SIZE) out.push(0);", "out.reverse(); while (out.length < SIZE) out.push(0);"),
        ],
        {"single_merge", "random_boards", "spawn_rules"},
    ),
    "a tile merges twice in one move": (
        [
            (
                "while (out.length < SIZE) out.push(0);",
                "for (var j = 0; j + 1 < out.length; j++) { if (out[j] === out[j + 1]) { out[j] *= 2; gained += out[j]; out.splice(j + 1, 1); } } while (out.length < SIZE) out.push(0);",
            )
        ],
        {"single_merge", "random_boards", "score_increments", "spawn_rules"},
    ),
    "left and right are swapped": (
        [("function move(direction) {", "function move(direction) {\n    if (direction === 'left') direction = 'right'; else if (direction === 'right') direction = 'left';")],
        {"merge_directions", "single_merge", "random_boards", "move_return_value", "spawn_rules", "arrow_keys"},
    ),
    "tiles do not slide through gaps": (
        [("var tiles = line.filter(function (v) { return v !== 0; });", "var tiles = line.slice();")],
        {"merge_directions", "random_boards", "move_return_value", "score_increments", "spawn_rules"},
    ),
    "tiles of 16 and more never merge": (
        [("if (i + 1 < tiles.length && tiles[i] === tiles[i + 1]) {", "if (i + 1 < tiles.length && tiles[i] === tiles[i + 1] && tiles[i] < 16) {")],
        {"random_boards", "score_increments", "spawn_rules", "arrow_keys"},
    ),
    "no tile appears after a move": (
        [("board = next;\n    score += gained;\n    spawn();", "board = next;\n    score += gained;")],
        {"spawn_rules", "arrow_keys"},
    ),
    "a tile appears after a move that changed nothing": (
        [("if (!changed) return false;", "if (!changed) { spawn(); render(); return false; }")],
        {"spawn_rules", "random_boards", "arrow_keys"},
    ),
    "the score goes up by the wrong amount": (
        [("gained += tiles[i] * 2;", "gained += tiles[i];")],
        {"score_increments", "arrow_keys", "visible_score", "restart_control"},
    ),
    "over() ignores vertical neighbours": (
        [
            (
                "over: function () { return over; }",
                "over: function () { for (var r = 0; r < SIZE; r++) for (var c = 0; c < SIZE; c++) { if (board[r][c] === 0) return false; if (c + 1 < SIZE && board[r][c] === board[r][c + 1]) return false; } return true; }",
            )
        ],
        {"game_over_detect"},
    ),
    "move returns nothing instead of true": (
        [("render();\n    return true;\n  }", "render();\n    return;\n  }")],
        {"move_return_value", "game_over_detect", "seeded_reset"},
    ),
    "the seed does not make the game repeatable": (
        [("rng = mulberry32(typeof seed === 'number' ? seed : Date.now());", "rng = Math.random;")],
        {"seeded_reset"},
    ),
    "a new game starts with three tiles": (
        [("spawn();\n    spawn();\n    render();", "spawn();\n    spawn();\n    spawn();\n    render();")],
        {"reset_state", "restart_control"},
    ),
    "load keeps the old score": (
        [("board = rows.map(function (row) { return row.slice(0, SIZE).map(Number); });\n    score = 0;", "board = rows.map(function (row) { return row.slice(0, SIZE).map(Number); });")],
        {"reset_state", "score_increments", "visible_score", "arrow_keys", "restart_control"},
    ),
    "the arrow keys are not wired": (
        [("document.addEventListener('keydown'", "document.addEventListener('keypress'")],
        {"arrow_keys", "visible_score"},
    ),
    "the score is not on the page": (
        [(".score { font-size: 20px; font-weight: bold; }", ".score { display: none; }")],
        {"visible_score"},
    ),
    "the score is on the page but invisible": (
        [(".score { font-size: 20px; font-weight: bold; }", ".score { opacity: 0; }")],
        {"visible_score"},
    ),
    "the page never says Game over": (
        [('>Game over</div>', ">Finished</div>")],
        {"game_over_shown"},
    ),
    "Game over is on the page but invisible": (
        [(".overlay[hidden] { display: none; }", ".overlay[hidden] { display: none; }\n  .overlay { opacity: 0; }")],
        {"game_over_shown"},
    ),
    "Game over is always in the page text": (
        [("<p>Use the arrow keys", "<p>Game over is what you see when no move is left. Use the arrow keys")],
        {"game_over_shown"},
    ),
    "no restart control": (
        [
            ('<button id="restart" type="button">New game</button>', ""),
            ("document.getElementById('restart').addEventListener('click', function () { reset(); });", ""),
        ],
        {"restart_control"},
    ),
    "a restart button that does nothing": (
        [("document.getElementById('restart').addEventListener('click', function () { reset(); });", "")],
        {"restart_control"},
    ),
    "a console error": (
        [("  reset();\n})();", "  console.error('something went wrong');\n  reset();\n})();")],
        {"page_loads"},
    ),
    "an uncaught exception": (
        [("  reset();\n})();", "  reset();\n  setTimeout(function () { throw new Error('late failure'); }, 50);\n})();")],
        {"page_loads"},
    ),
    "an image from the internet": (
        [("  reset();\n})();", "  reset();\n  new Image().src = 'http://example.invalid/tile.png';\n})();")],
        {"no_network"},
    ),
    "a request to a server that is handled": (
        [("  reset();\n})();", "  reset();\n  fetch('https://example.invalid/scores').catch(function () {});\n})();")],
        {"no_network"},
    ),
    "a web socket": (
        [("  reset();\n})();", "  reset();\n  try { new WebSocket('ws://example.invalid/live'); } catch (error) {}\n})();")],
        {"no_network"},
    ),
    "the board is drawn on a canvas": (
        [
            (
                "tile.textContent = board[r][c] === 0 ? '' : String(board[r][c]);",
                "var surface = document.createElement('canvas'); surface.width = 60; surface.height = 40;"
                " var pen = surface.getContext('2d'); pen.font = '20px sans-serif'; pen.fillText(board[r][c] ? String(board[r][c]) : '', 5, 25);"
                " tile.appendChild(surface);",
            )
        ],
        {"display_matches_board", "arrow_keys"},
    ),
    "load does not redraw the page": (
        [("over = !hasMoves();\n    render();\n  }\n\n  window.game", "over = !hasMoves();\n  }\n\n  window.game")],
        {"display_matches_board", "game_over_shown", "arrow_keys"},
    ),
    "window.game has no over function": (
        [("over: function () { return over; }", "over: true")],
        EVERYTHING_THE_PAGE_ALONE_DOES_NOT_DECIDE,
    ),
}

# A different page that is still a correct 2048 keeps every point.
CORRECT_VARIANTS = {
    "the page draws a third of a second after the game changes": [
        ("function render() {", "function render() { setTimeout(renderNow, 300); }\n  function renderNow() {")
    ],
    "the restart button says Try again": [(">New game</button>", ">Try again</button>")],
    "the restart button says Start": [(">New game</button>", ">Start</button>")],
    "a decoy button with a restart word comes first": [
        ('<button id="restart" type="button">New game</button>', '<button id="decoy" type="button">Reset</button>\n  <button id="restart" type="button">New game</button>')
    ],
    "a link styled as a restart control": [
        ('<button id="restart" type="button">New game</button>', '<a id="restart" href="#" style="cursor:pointer">New game</a>')
    ],
    "Game over fades in": [
        (".overlay[hidden] { display: none; }", ".overlay { transition: opacity 0.4s; }\n  .overlay[hidden] { display: block; opacity: 0; }")
    ],
}


def edited(edits) -> str:
    text = REFERENCE_PAGE
    for old, new in edits:
        assert text.count(old) == 1, f"the reference page must hold exactly one: {old!r}"
        text = text.replace(old, new, 1)
    return text


def split_into_files(text: str) -> dict[str, str]:
    start, end = text.index("<script>"), text.index("</script>")
    return {
        "index.html": text[:start] + '<script src="game.js"></script>' + text[end + len("</script>") :],
        "game.js": text[start + len("<script>") : end],
    }


END_OF_SCRIPT = "  reset();\n})();"
NEVER_RETURNS = edited([(END_OF_SCRIPT, "  reset();\n  window.game.move = function () { while (true) {} };\n})();")])
NEVER_LOADS = "<!doctype html><title>x</title><script>while (true) {}</script>"
MOVE_THROWS = edited([(END_OF_SCRIPT, "  window.game.move = function () { throw new Error('no moves today'); };\n  reset();\n})();")])
ODD_SCORES = {
    "nan": "NaN",
    "infinity": "Infinity",
    "string": "'a lot'",
    "list": "[1, 2]",
    "huge integer": "10n ** 400n",
    "null": "null",
    "object": "{}",
}
ODD_BOARD = edited(
    [("    board: function () { return board.map(function (row) { return row.slice(); }); },", "    board: function () { return [[NaN, 10n ** 400n, 'x', null], [1], 5, []]; },")]
)
NO_GAME = {
    "empty": b"",
    "binary": b"\x00\x01\x02 not html at all \xff\xfe",
    "no game": b"<html><body>no game here</body></html>",
    "game is not an object": b"<script>window.game = 5;</script>",
}


def scenarios() -> dict:
    """Every page the validator is run on, as name -> (files to write, validator environment)."""
    found: dict = {}
    for name, (edits, _) in DEFECTS.items():
        found[f"defect: {name}"] = ({"index.html": edited(edits)}, None)
    for name, edits in CORRECT_VARIANTS.items():
        found[f"correct: {name}"] = ({"index.html": edited(edits)}, None)
    found["correct: split over several files"] = (split_into_files(REFERENCE_PAGE), None)
    found["hang: a move that never returns"] = ({"index.html": NEVER_RETURNS}, None)
    found["hang: a page that never finishes loading"] = ({"index.html": NEVER_LOADS}, None)
    found["throws: every move"] = ({"index.html": MOVE_THROWS}, None)
    for name, answer in ODD_SCORES.items():
        page = edited([(END_OF_SCRIPT, f"  window.game.score = function () {{ return {answer}; }};\n  reset();\n}})();")])
        found[f"odd score: {name}"] = ({"index.html": page}, None)
    found["odd board"] = ({"index.html": ODD_BOARD}, None)
    for name, content in NO_GAME.items():
        found[f"no game: {name}"] = ({"index.html": content}, None)
    return found


def writer(reference: Path, files: dict, env):
    def build(folder: Path):
        workspace = clone(reference, folder)
        for filename, content in files.items():
            (workspace / filename).write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        return workspace, env

    return build


@pytest.fixture(scope="module")
def reference_workspace(tmp_path_factory):
    return make_workspace(TASK, tmp_path_factory.mktemp("game-reference"), solved=True)


@pytest.fixture(scope="module")
def runs(reference_workspace, tmp_path_factory):
    prepared = {name: writer(reference_workspace, files, env) for name, (files, env) in scenarios().items()}
    holder = ScenarioRuns(TASK, tmp_path_factory.mktemp("game-runs"), prepared)
    try:
        yield holder
    finally:
        holder.close()
        kill_processes_mentioning("bench-game-")


@pytest.mark.parametrize("name", sorted(DEFECTS))
def test_a_defect_loses_the_check_it_breaks(name, runs):
    result = runs.result(f"defect: {name}")
    assert failed(result) == DEFECTS[name][1], why(result)


@pytest.mark.parametrize("name", sorted(CORRECT_VARIANTS))
def test_a_correct_page_in_another_style_keeps_full_marks(name, runs):
    result = runs.result(f"correct: {name}")
    assert failed(result) == set(), why(result)
    assert result["score"] == result["max_score"] == 100


def test_a_game_split_over_several_files_keeps_full_marks(runs):
    result = runs.result("correct: split over several files")
    assert failed(result) == set(), why(result)


def test_every_scored_check_has_a_defect_that_loses_it():
    covered = set().union(*(expected for _, expected in DEFECTS.values()))
    assert covered == set(CHECKS), sorted(set(CHECKS) - covered)


def test_the_full_board_with_one_vertical_pair_has_no_horizontal_pair():
    spec = importlib.util.spec_from_file_location("game_validator", TASKS / TASK / "validator" / "run.py")
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    board = module.FULL_WITH_VERTICAL_MERGE
    horizontal = [(r, c) for r in range(4) for c in range(3) if board[r][c] == board[r][c + 1]]
    vertical = [(r, c) for r in range(3) for c in range(4) if board[r][c] == board[r + 1][c]]
    assert all(v for row in board for v in row)
    assert horizontal == []
    assert len(vertical) == 1


# ------------------------------------------------------------ pages that misbehave


def test_a_move_that_never_returns_still_gets_a_result_inside_the_time_limit(runs):
    process, result = runs.get("hang: a move that never returns")
    limit = json.loads((TASKS / TASK / "task.json").read_text(encoding="utf-8"))["validator_timeout_seconds"]
    assert result is not None, process.stderr
    assert process.elapsed < limit / 3, process.elapsed
    assert process.returncode != 0
    assert result["score"] < result["max_score"]
    assert result["checks"]["api_present"] is True
    assert result["checks"]["merge_directions"] is False


def test_a_page_that_never_finishes_loading_gets_a_result(runs):
    process, result = runs.get("hang: a page that never finishes loading")
    assert result is not None, process.stderr
    assert process.elapsed < 100, process.elapsed
    assert result["checks"]["page_loads"] is False
    assert result["checks"]["api_present"] is False
    assert result["score"] == 3  # only no_network can be said of a page that never loads


def test_the_validator_leaves_no_browser_running(runs):
    runs.finish()
    assert processes_mentioning("bench-game-") == []


@pytest.mark.parametrize("name", sorted(NO_GAME))
def test_a_page_without_a_game_scores_nothing_but_gets_a_result(name, runs):
    result = runs.result(f"no game: {name}")
    assert result["checks"]["api_present"] is False
    assert result["score"] <= 13  # page_loads and no_network, at most


def test_a_missing_page_scores_nothing_but_gets_a_result(tmp_path):
    (tmp_path / "empty").mkdir()
    result = validate(TASK, tmp_path / "empty", tmp_path)
    assert result["score"] == 0
    assert "index.html is missing" in result["details"]["page_loads"]


def test_a_page_whose_functions_throw_fails_checks_without_crashing(runs):
    result = runs.result("throws: every move")
    assert {"merge_directions", "single_merge", "move_return_value", "spawn_rules"} <= failed(result)
    assert result["checks"]["api_present"] is True


@pytest.mark.parametrize("name", sorted(ODD_SCORES))
def test_a_score_that_is_not_a_sensible_number_fails_checks_without_crashing(name, runs):
    result = runs.result(f"odd score: {name}")
    assert {"score_increments", "visible_score"} <= failed(result)
    assert result["checks"]["api_present"] is True


def test_a_board_with_odd_values_fails_checks_without_crashing(runs):
    result = runs.result("odd board")
    assert "merge_directions" in failed(result)
    assert result["checks"]["api_present"] is True


# ------------------------------------------------------------ environment


def test_a_missing_playwright_package_is_an_environment_error_not_a_failed_page(reference_workspace, tmp_path):
    process, result = run_validator(TASK, clone(reference_workspace, tmp_path), tmp_path, shadow_module(tmp_path, "playwright"))
    assert process.returncode != 0
    assert result["score"] is None
    assert result["max_score"] == 100
    assert result["checks"] == {}
    assert "playwright" in result["error"].lower() and "pip install" in result["error"]


def test_a_browser_that_cannot_start_is_an_environment_error_not_a_failed_page(reference_workspace, tmp_path):
    empty = tmp_path / "no-browsers"
    empty.mkdir()
    process, result = run_validator(TASK, clone(reference_workspace, tmp_path), tmp_path, {"PLAYWRIGHT_BROWSERS_PATH": str(empty)})
    assert process.returncode != 0
    assert result["score"] is None and result["checks"] == {}
    assert "playwright install chromium" in result["error"]

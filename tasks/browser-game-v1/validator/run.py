"""Validator for browser-game-v1.

Usage: python validator/run.py --workspace <dir> --output <json>

Opens index.html from the file system in headless Chromium (Playwright), drives
the game through window.game and the keyboard, and checks the rules of 2048
and what the page shows. The contestant's final message is never read.

The result file is always written. If the validator cannot run at all (the
Playwright package or Chromium is missing) the file holds
{"score": null, "max_score": N, "checks": {}, "error": "..."} and the exit
status is 2. A page that misbehaves is never that error: it fails checks.

The browser is driven by a worker process that this script starts and watches.
A page that never returns from a call (an endless loop in a move) cannot be
interrupted from inside Playwright, so the supervisor stops the worker's whole
process tree when the worker stops reporting progress or the time budget runs
out, and scores what the worker finished. Every check the worker did not reach
fails.
"""
from __future__ import annotations

import argparse
from collections import OrderedDict
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
SIZE = 4
DIRECTIONS = ("left", "right", "up", "down")
KEYS = {"left": "ArrowLeft", "right": "ArrowRight", "up": "ArrowUp", "down": "ArrowDown"}
API_FUNCTIONS = ("reset", "load", "move", "board", "score", "over")
INSTALL_HINT = "Install it with: pip install -r requirements-validators.txt, then: python -m playwright install chromium"

# Time budget, all well inside validator_timeout_seconds in task.json (300).
SOFT_DEADLINE_SECONDS = 100  # the worker stops starting new page calls after this
HARD_DEADLINE_SECONDS = 130  # the supervisor stops the worker after this, whatever it is doing
STALL_SECONDS = 20  # one page call (or one wait) that takes this long is a hung page
START_SECONDS = 60  # Chromium must be up and the page loaded within this
LINGER_SECONDS = 10  # after the worker has finished, how long it may take to close the browser

STUCK_BOARDS = [
    [[2, 4, 2, 4], [4, 2, 4, 2], [2, 4, 2, 4], [4, 2, 4, 2]],
    [[2, 4, 8, 16], [16, 8, 4, 2], [2, 4, 8, 16], [16, 8, 4, 2]],
]
FULL_WITH_MERGE = [[4, 4, 2, 4], [4, 2, 4, 2], [2, 4, 2, 4], [4, 2, 4, 2]]
# Full board whose only equal neighbours are one vertical pair (column 0, rows 0 and 1).
FULL_WITH_VERTICAL_MERGE = [[2, 4, 2, 4], [2, 8, 4, 2], [4, 2, 8, 4], [8, 4, 2, 8]]
ONE_EMPTY = [[0, 4, 2, 4], [4, 2, 4, 2], [2, 4, 2, 4], [4, 2, 4, 2]]


class EnvironmentProblem(Exception):
    """The validator itself cannot run. The message says what is missing and how to install it."""


class BudgetExceeded(Exception):
    """The worker's own time budget ran out."""


# ---------------------------------------------------------------- rules model


def is_number(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, ValueError):
        return False


def empty_board():
    return [[0] * SIZE for _ in range(SIZE)]


def slide_line(line):
    tiles = [value for value in line if value]
    out = []
    gained = 0
    index = 0
    while index < len(tiles):
        if index + 1 < len(tiles) and tiles[index] == tiles[index + 1]:
            out.append(tiles[index] * 2)
            gained += tiles[index] * 2
            index += 2
        else:
            out.append(tiles[index])
            index += 1
    return out + [0] * (SIZE - len(out)), gained


def apply_move(board, direction):
    """Return (board after the slide and merge, before any new tile; score gained)."""
    result = empty_board()
    gained = 0
    for i in range(SIZE):
        if direction in ("left", "right"):
            line = list(board[i])
        else:
            line = [board[k][i] for k in range(SIZE)]
        if direction in ("right", "down"):
            line.reverse()
        new_line, points = slide_line(line)
        gained += points
        if direction in ("right", "down"):
            new_line.reverse()
        for k in range(SIZE):
            if direction in ("left", "right"):
                result[i][k] = new_line[k]
            else:
                result[k][i] = new_line[k]
    return result, gained


def has_moves(board):
    for r in range(SIZE):
        for c in range(SIZE):
            if board[r][c] == 0:
                return True
            if c + 1 < SIZE and board[r][c] == board[r][c + 1]:
                return True
            if r + 1 < SIZE and board[r][c] == board[r + 1][c]:
                return True
    return False


def well_formed(board):
    return (
        isinstance(board, list)
        and len(board) == SIZE
        and all(isinstance(row, list) and len(row) == SIZE for row in board)
        and all(is_number(v) for row in board for v in row)
    )


def failed_reply(message):
    return {"moved": None, "board": None, "score": None, "before_score": None, "over": None, "error": message}


def analyse(before, direction, reply):
    """Compare one move's reply from the page with the rules."""
    expected, gained = apply_move(before, direction)
    changed = expected != before
    actual = reply.get("board")
    out = {"return_ok": reply.get("moved") is changed}
    if not well_formed(actual):
        out.update(merge_ok=False, spawn_ok=False, score_ok=False, over_ok=False)
        return out
    score, before_score = reply.get("score"), reply.get("before_score")
    numbers_ok = is_number(score) and is_number(before_score)
    if changed:
        merge_ok = all(actual[r][c] == expected[r][c] for r in range(SIZE) for c in range(SIZE) if expected[r][c] != 0)
        new_cells = [(r, c) for r in range(SIZE) for c in range(SIZE) if expected[r][c] == 0 and actual[r][c] != 0]
        out["merge_ok"] = merge_ok
        out["spawn_ok"] = merge_ok and len(new_cells) == 1 and actual[new_cells[0][0]][new_cells[0][1]] in (2, 4)
        out["score_ok"] = numbers_ok and score - before_score == gained
    else:
        out["merge_ok"] = actual == before
        out["spawn_ok"] = actual == before
        out["score_ok"] = numbers_ok and score == before_score
    out["over_ok"] = reply.get("over") is (not has_moves(actual))
    return out


def line_board(rows):
    board = empty_board()
    for r, row in rows.items():
        board[r] = list(row)
    return board


def column_board(columns):
    board = empty_board()
    for c, column in columns.items():
        for r in range(SIZE):
            board[r][c] = column[r]
    return board


def basic_cases():
    return [
        (line_board({0: [2, 2, 0, 0]}), "left"),
        (line_board({1: [0, 0, 4, 4]}), "left"),
        (line_board({2: [2, 0, 0, 2]}), "left"),
        (line_board({3: [0, 2, 0, 4]}), "left"),
        (line_board({0: [2, 2, 0, 0], 1: [0, 4, 4, 0], 2: [8, 0, 0, 8], 3: [0, 0, 0, 16]}), "left"),
        (line_board({0: [2, 2, 0, 0]}), "right"),
        (line_board({2: [4, 0, 4, 0]}), "right"),
        (line_board({1: [0, 8, 0, 4], 3: [2, 0, 2, 0]}), "right"),
        (column_board({0: [2, 2, 0, 0]}), "up"),
        (column_board({3: [0, 0, 8, 8]}), "up"),
        (column_board({1: [0, 4, 0, 2], 2: [2, 0, 2, 0]}), "up"),
        (column_board({1: [4, 4, 0, 0]}), "down"),
        (column_board({2: [2, 0, 2, 0]}), "down"),
        (column_board({0: [2, 2, 0, 0], 1: [0, 4, 4, 0], 2: [8, 0, 0, 8], 3: [0, 0, 0, 16]}), "down"),
    ]


def chain_cases():
    full_twos = [[2] * SIZE for _ in range(SIZE)]
    return [
        (line_board({0: [2, 2, 2, 2]}), "left"),
        (line_board({0: [2, 2, 2, 2]}), "right"),
        (line_board({1: [4, 4, 4, 0]}), "left"),
        (line_board({1: [0, 4, 4, 4]}), "right"),
        (line_board({2: [2, 2, 4, 4]}), "left"),
        (line_board({2: [2, 2, 4, 4]}), "right"),
        (line_board({3: [2, 2, 4, 0]}), "left"),
        (line_board({3: [4, 2, 2, 0]}), "left"),
        (line_board({0: [8, 4, 4, 0]}), "left"),
        (line_board({0: [0, 4, 4, 8]}), "right"),
        (column_board({0: [2, 2, 2, 2]}), "down"),
        (column_board({1: [2, 2, 4, 4]}), "up"),
        (column_board({2: [2, 2, 4, 4]}), "down"),
        (column_board({3: [4, 4, 4, 0]}), "up"),
        (full_twos, "left"),
        (full_twos, "down"),
    ]


def noop_cases():
    return [
        (line_board({0: [2, 4, 8, 16]}), "left"),
        (line_board({0: [2, 4, 8, 16], 1: [4, 8, 16, 32]}), "up"),
        (line_board({3: [2, 4, 8, 16]}), "down"),
        (line_board({1: [0, 0, 2, 4]}), "right"),
        (column_board({0: [2, 4, 8, 16]}), "left"),
        (column_board({3: [2, 4, 8, 16]}), "right"),
        (STUCK_BOARDS[0], "left"),
        (STUCK_BOARDS[0], "up"),
        (STUCK_BOARDS[1], "right"),
        (STUCK_BOARDS[1], "down"),
    ]


def random_cases():
    rng = random.Random(2048)
    cases = []
    for _ in range(48):
        fill = rng.choice([0.3, 0.5, 0.7, 0.9, 1.0])
        board = [
            [rng.choice([2, 2, 2, 4, 4, 8, 16, 32]) if rng.random() < fill else 0 for _ in range(SIZE)]
            for _ in range(SIZE)
        ]
        for direction in DIRECTIONS:
            cases.append((board, direction))
    return cases


# --------------------------------------------------------------- page helpers

# Each script runs many moves in one page call. A move that throws fails only its own case.
CASES_SCRIPT = """(cases) => cases.map(([board, direction]) => {
  try {
    window.game.load(board);
    const beforeScore = window.game.score();
    const moved = window.game.move(direction);
    return { moved, board: window.game.board(), score: window.game.score(), before_score: beforeScore, over: window.game.over() };
  } catch (error) {
    return { error: String(error) };
  }
})"""

SEQUENCE_SCRIPT = """(plans) => plans.map(({ board, directions }) => {
  try {
    window.game.load(board);
  } catch (error) {
    return directions.map(() => ({ error: String(error) }));
  }
  return directions.map((direction) => {
    try {
      const moved = window.game.move(direction);
      return { moved, board: window.game.board(), score: window.game.score(), over: window.game.over() };
    } catch (error) {
      return { error: String(error) };
    }
  });
})"""

TRACE_SCRIPT = """([seeds, moves]) => seeds.map((seed) => {
  window.game.reset(seed);
  const boards = [window.game.board()];
  let changed = 0;
  for (const direction of moves) {
    if (window.game.move(direction)) changed += 1;
    boards.push(window.game.board());
  }
  return { boards, changed };
})"""

STARTS_SCRIPT = "(seeds) => seeds.map((seed) => { window.game.reset(seed); return window.game.board(); })"

STATE_SCRIPT = "() => ({ board: window.game.board(), score: window.game.score(), over: window.game.over() })"

# Text a person looking at the page can read: text nodes that are not inside an element with
# display:none, opacity:0 (on it or any ancestor), visibility:hidden, a zero font size or a
# zero-size clipping box, and whose own box has an area. One line per text node.
VISIBLE_TEXT_SCRIPT = r"""() => {
  if (!document.body) return '';
  const skip = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'TITLE', 'HEAD']);
  const hidden = (el) => {
    for (let e = el; e && e.nodeType === 1; e = e.parentElement) {
      const style = getComputedStyle(e);
      if (style.display === 'none' || parseFloat(style.opacity) === 0) return true;
      if (e === el && (style.visibility === 'hidden' || style.visibility === 'collapse')) return true;
      if (e === el && parseFloat(style.fontSize) === 0) return true;
      if (style.overflow !== 'visible') {
        const box = e.getBoundingClientRect();
        if (box.width === 0 || box.height === 0) return true;
      }
    }
    return false;
  };
  const out = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const text = node.textContent.replace(/\s+/g, ' ').trim();
    if (!text) continue;
    const el = node.parentElement;
    if (!el || skip.has(el.tagName) || hidden(el)) continue;
    const range = document.createRange();
    range.selectNodeContents(node);
    const box = range.getBoundingClientRect();
    if (box.width === 0 || box.height === 0) continue;
    out.push(text);
  }
  return out.join('\n');
}"""

# Marks every visible control whose label says it starts a new game, in document order, with
# data-bench-restart="0", "1", ... and returns how many there are.
FIND_RESTART_SCRIPT = r"""() => {
  const pattern = /\b(new game|new|restart|reset|retry|replay|try again|play again|start over|start again|start)\b/i;
  document.querySelectorAll('[data-bench-restart]').forEach((el) => el.removeAttribute('data-bench-restart'));
  const visible = (el) => {
    for (let e = el; e && e.nodeType === 1; e = e.parentElement) {
      const style = getComputedStyle(e);
      if (style.display === 'none' || parseFloat(style.opacity) === 0) return false;
      if (e === el && style.visibility === 'hidden') return false;
    }
    const box = el.getBoundingClientRect();
    return box.width > 0 && box.height > 0;
  };
  const label = (el) => [el.innerText, el.value, el.getAttribute('aria-label'), el.getAttribute('title')].filter(Boolean).join(' ');
  const native = Array.from(document.querySelectorAll('button, [role="button"], a, input[type="button"], input[type="submit"], input[type="reset"]'))
    .filter((el) => visible(el) && pattern.test(label(el)));
  const custom = Array.from(document.querySelectorAll('body *')).filter(
    (el) => !native.includes(el) && !native.some((n) => n.contains(el)) && visible(el) && el.children.length === 0
      && getComputedStyle(el).cursor === 'pointer' && pattern.test(label(el))
  );
  const all = native.concat(custom).slice(0, 8);
  all.forEach((el, index) => el.setAttribute('data-bench-restart', String(index)));
  return all.length;
}"""


class Page:
    """The Playwright page, with a progress beat and a time budget on every call."""

    def __init__(self, page, beat_path: Path, deadline: float):
        self.page = page
        self.beat_path = beat_path
        self.deadline = deadline

    def tick(self):
        if time.monotonic() > self.deadline:
            raise BudgetExceeded("the validator's time budget ran out")
        try:
            os.utime(self.beat_path, None)
        except OSError:
            pass

    def evaluate(self, script, arg=None):
        self.tick()
        return self.page.evaluate(script, arg)

    def press(self, key):
        self.tick()
        self.page.keyboard.press(key)

    def wait(self, milliseconds):
        self.tick()
        self.page.wait_for_timeout(milliseconds)

    def click(self, selector):
        self.tick()
        self.page.locator(selector).first.click(timeout=3000)


def visible_text(page):
    return page.evaluate(VISIBLE_TEXT_SCRIPT)


def tokens(page):
    text = visible_text(page)
    text = re.sub(r"(?<=\d),(?=\d)", "", text)
    return [int(match) for match in re.findall(r"\d+", text)], text


def poll(page, predicate, timeout_ms=2000, step_ms=50):
    """Call predicate until it is true or the time is up. Returns its last answer."""
    end = time.monotonic() + timeout_ms / 1000
    while True:
        value = predicate()
        if value or time.monotonic() >= end:
            return value
        page.wait(step_ms)


def first_failure(results, key):
    for index, item in enumerate(results):
        if not item[key]:
            return index
    return None


class Runner:
    def __init__(self, page):
        self.page = page

    def run_cases(self, cases):
        replies = self.page.evaluate(CASES_SCRIPT, [[board, direction] for board, direction in cases])
        outcomes = []
        for (board, direction), reply in zip(cases, replies):
            if not isinstance(reply, dict):
                reply = failed_reply(f"the page answered {reply!r}")
            outcome = analyse(board, direction, reply)
            outcome["case"] = (board, direction, reply)
            outcomes.append(outcome)
        return outcomes

    def run_sequences(self):
        rng = random.Random(7)
        plans = []
        for start in range(4):
            board = [[rng.choice([0, 0, 2, 2, 4, 8]) for _ in range(SIZE)] for _ in range(SIZE)]
            plans.append({"board": board, "directions": [DIRECTIONS[(step + start) % 4] for step in range(14)]})
        traces = self.page.evaluate(SEQUENCE_SCRIPT, plans)
        outcomes = []
        for plan, trace in zip(plans, traces):
            current = [row[:] for row in plan["board"]]
            total = 0
            for direction, state in zip(plan["directions"], trace):
                if not isinstance(state, dict):
                    state = {"error": f"the page answered {state!r}"}
                expected, gained = apply_move(current, direction)
                changed = expected != current
                if changed:
                    total += gained
                reply = {
                    "moved": state.get("moved"),
                    "board": state.get("board"),
                    "score": state.get("score"),
                    "before_score": total - (gained if changed else 0),
                    "over": state.get("over"),
                }
                if "error" in state:
                    reply["error"] = state["error"]
                outcome = analyse(current, direction, reply)
                outcome["score_ok"] = is_number(reply["score"]) and reply["score"] == total
                outcome["case"] = (current, direction, reply)
                outcomes.append(outcome)
                if well_formed(reply["board"]):
                    current = reply["board"]
        return outcomes


def all_ok(outcomes, key):
    return bool(outcomes) and all(item[key] for item in outcomes)


def describe(outcomes, key):
    index = first_failure(outcomes, key)
    if index is None:
        return ""
    board, direction, reply = outcomes[index]["case"]
    return f"{key} failed moving {direction} from {board}: page answered {reply}"


# ------------------------------------------------------------------- checks


def check_api(page, details):
    present = page.evaluate(
        "(names) => typeof window.game === 'object' && window.game !== null && names.every((n) => typeof window.game[n] === 'function')",
        list(API_FUNCTIONS),
    )
    if not present:
        details["api_present"] = "window.game with reset, load, move, board, score and over was not found"
    return bool(present)


def check_reset_and_load(page, details):
    for seed in (1, 2, 3, 42, 1000):
        page.evaluate("(s) => window.game.reset(s)", seed)
        state = page.evaluate(STATE_SCRIPT)
        board = state["board"]
        if not well_formed(board):
            details["reset_state"] = f"reset({seed}) gave a board that is not 4 by 4 numbers: {board}"
            return False
        tiles = [v for row in board for v in row if v]
        if len(tiles) != 2 or any(v not in (2, 4) for v in tiles) or state["score"] != 0 or state["over"] is not False:
            details["reset_state"] = f"reset({seed}) should give two tiles of 2 or 4, score 0, not over: {state}"
            return False
    page.evaluate("() => window.game.reset()")
    sample = [[2, 0, 4, 0], [0, 8, 0, 0], [0, 0, 16, 0], [32, 0, 0, 64]]
    page.evaluate("(b) => { window.game.load([[2,2,0,0],[0,0,0,0],[0,0,0,0],[0,0,0,0]]); window.game.move('left'); }", sample)
    page.evaluate("(b) => window.game.load(b)", sample)
    state = page.evaluate(STATE_SCRIPT)
    if state["board"] != sample or state["score"] != 0 or state["over"] is not False:
        details["reset_state"] = f"load should set the board, reset the score and leave over false: {state}"
        return False
    page.evaluate("(b) => window.game.load(b)", STUCK_BOARDS[0])
    if page.evaluate("() => window.game.over()") is not True:
        details["reset_state"] = "load of a board with no moves should make over() true"
        return False
    return True


def check_seeded(page, details):
    moves = ["left", "up", "right", "down"] * 5
    (first, second, third) = page.evaluate(TRACE_SCRIPT, [[11, 11, 2024], moves])
    if first["boards"] != second["boards"]:
        details["seeded_reset"] = "the same seed and moves gave different boards"
        return False
    if first["changed"] < 3:
        details["seeded_reset"] = "too few moves changed the board to compare runs"
        return False
    starts = {json.dumps(board) for board in page.evaluate(STARTS_SCRIPT, list(range(1, 14)))}
    if len(starts) < 2:
        details["seeded_reset"] = "every seed gave the same starting board"
        return False
    return first["boards"] != third["boards"] or len(starts) > 1


def check_game_over_state(page, details):
    for board in STUCK_BOARDS:
        page.evaluate("(b) => window.game.load(b)", board)
        if page.evaluate("() => window.game.over()") is not True:
            details["game_over_detect"] = f"over() should be true for {board}"
            return False
        for direction in DIRECTIONS:
            moved = page.evaluate("(d) => window.game.move(d)", direction)
            after = page.evaluate("() => window.game.board()")
            if moved is not False or after != board:
                details["game_over_detect"] = f"a move on a finished board should return false and change nothing ({direction})"
                return False
    for board, label in ((FULL_WITH_MERGE, "a full board with a merge"), (FULL_WITH_VERTICAL_MERGE, "a full board with a vertical merge"), (ONE_EMPTY, "a board with one empty cell")):
        page.evaluate("(b) => window.game.load(b)", board)
        if page.evaluate("() => window.game.over()") is not False:
            details["game_over_detect"] = f"over() should be false for {label}"
            return False
    page.evaluate("(b) => window.game.load(b)", FULL_WITH_MERGE)
    if page.evaluate("() => window.game.move('left')") is not True:
        details["game_over_detect"] = "a full board with a merge should still accept a move"
        return False
    return True


def pick_pairs(static_tokens, count):
    """Pairs (p, q) so that the merged tiles 2p and 2q and their sum 2p + 2q are all unlike any number on the page."""
    chosen = []
    for p, q in ((64, 256), (128, 512), (32, 128), (16, 64), (256, 1024), (32, 512), (16, 128), (64, 512)):
        if not {2 * p, 2 * q, 2 * p + 2 * q} & set(static_tokens) and len(chosen) < count:
            chosen.append((p, q))
    return chosen


def check_display(page, details):
    """The page text shows the board after load, and stops showing a board that was replaced."""
    page.evaluate("() => window.game.reset(5)")
    page.evaluate("(b) => window.game.load(b)", line_board({0: [2, 0, 0, 0]}))
    poll(page, lambda: 2 in tokens(page)[0], 1500)
    static_tokens, _ = tokens(page)

    big = [[2, 4, 8, 16], [32, 64, 128, 256], [512, 1024, 2048, 4096], [0, 0, 0, 0]]
    wanted = sorted({v for row in big for v in row if v})
    page.evaluate("(b) => window.game.load(b)", big)
    poll(page, lambda: all(v in tokens(page)[0] for v in wanted), 2000)
    shown, _ = tokens(page)
    missing = [v for v in wanted if v not in shown]
    page.evaluate("(b) => window.game.load(b)", line_board({0: [2, 0, 0, 0]}))
    poll(page, lambda: not [v for v in wanted if v in tokens(page)[0] and v not in static_tokens], 2000)
    shown_after, _ = tokens(page)
    stale = [v for v in wanted if v in shown_after and v not in static_tokens]
    if missing:
        details["display_matches_board"] = f"tile values missing from the page text after load: {missing}"
    elif stale:
        details["display_matches_board"] = f"old tile values still shown after another load: {stale}"
    return not missing and not stale, static_tokens


def check_arrow_keys(page, details, static_tokens):
    pairs = pick_pairs(static_tokens, 4)
    layouts = {
        "left": lambda p, q: line_board({0: [p, p, 0, 0], 1: [q, q, 0, 0]}),
        "right": lambda p, q: line_board({1: [0, 0, p, p], 2: [0, 0, q, q]}),
        "up": lambda p, q: column_board({0: [p, p, 0, 0], 1: [q, q, 0, 0]}),
        "down": lambda p, q: column_board({2: [0, 0, p, p], 3: [0, 0, q, q]}),
    }
    keys_ok = len(pairs) == 4
    for direction, (p, q) in zip(DIRECTIONS, pairs):
        board = layouts[direction](p, q)
        page.evaluate("(b) => window.game.load(b)", board)
        poll(page, lambda: p in tokens(page)[0] and q in tokens(page)[0], 2000)
        before, _ = tokens(page)
        page.press(KEYS[direction])
        poll(page, lambda: page.evaluate("() => window.game.board()") != board, 2000)
        state = page.evaluate(STATE_SCRIPT)
        reply = {"moved": True, "board": state["board"], "score": state["score"], "before_score": 0, "over": state["over"]}
        outcome = analyse(board, direction, reply)
        if not (outcome["merge_ok"] and outcome["spawn_ok"] and outcome["score_ok"]):
            details["arrow_keys"] = f"pressing {KEYS[direction]} did not play the move: board is now {state['board']}"
            keys_ok = False
            break
        # The score also changes by a merge, so each merged tile is looked for on its own: 2p and 2q, not 2p + 2q.
        if {2 * p, 2 * q} & set(before) or not poll(page, lambda: {2 * p, 2 * q} <= set(tokens(page)[0]), 2000):
            details["arrow_keys"] = f"after {KEYS[direction]} the page text does not show the merged tiles {2 * p} and {2 * q}"
            keys_ok = False
            break
    noop_board = line_board({0: [2, 4, 8, 16]})
    page.evaluate("(b) => window.game.load(b)", noop_board)
    page.press(KEYS["left"])
    page.wait(250)
    if page.evaluate("() => window.game.board()") != noop_board:
        details["arrow_keys"] = "a key press that cannot move any tile still changed the board"
        keys_ok = False
    return keys_ok


def check_visible_score(page, details, static_tokens):
    for first, second in ((4, 8), (16, 32), (64, 128)):
        gained = 2 * first + 2 * second
        board = line_board({0: [first, first, 0, 0], 1: [second, second, 0, 0]})
        page.evaluate("(b) => window.game.load(b)", board)
        poll(page, lambda: first in tokens(page)[0], 1500)
        before, _ = tokens(page)
        if gained in before or gained in static_tokens:
            continue
        page.press(KEYS["left"])
        poll(page, lambda: gained in tokens(page)[0], 2000)
        after, _ = tokens(page)
        score = page.evaluate("() => window.game.score()")
        visible_ok = score == gained and gained in after
        if not visible_ok:
            details["visible_score"] = f"score() is {score} and the page text does not show {gained}"
        return visible_ok
    details["visible_score"] = "could not find a score that stands apart from the other numbers on the page"
    return False


def check_game_over_shown(page, details):
    pattern = re.compile(r"game\s*over", re.IGNORECASE)

    def shown():
        return bool(pattern.search(visible_text(page)))

    page.evaluate("(b) => window.game.load(b)", FULL_WITH_MERGE)
    if not poll(page, lambda: not shown(), 1500):
        details["game_over_shown"] = "the page shows Game over while moves are still possible"
        return False
    page.evaluate("(b) => window.game.load(b)", STUCK_BOARDS[0])
    if poll(page, shown, 2000):
        return True
    page.press(KEYS["left"])
    if poll(page, shown, 2000):
        return True
    details["game_over_shown"] = "the page does not show the words Game over when no move is possible"
    return False


def fresh_game(state):
    board = state["board"]
    if not well_formed(board):
        return False
    tiles = [v for row in board for v in row if v]
    return len(tiles) == 2 and all(v in (2, 4) for v in tiles) and state["score"] == 0 and state["over"] is False


def try_restart(page, prepare, preferred=None):
    """Prepare a game, click each restart candidate in turn, and return the index of the first that gives a fresh game."""
    prepare()
    count = page.evaluate(FIND_RESTART_SCRIPT)
    order = list(range(count))
    if preferred in order:
        order.remove(preferred)
        order.insert(0, preferred)
    for index in order:
        prepare()
        page.evaluate(FIND_RESTART_SCRIPT)
        try:
            page.click(f'[data-bench-restart="{index}"]')
        except BudgetExceeded:
            raise
        except Exception:
            continue
        if poll(page, lambda: fresh_game(page.evaluate(STATE_SCRIPT)), 1500):
            return index
    return None if count else -1


def check_restart(page, details):
    def with_score():
        page.evaluate("(b) => window.game.load(b)", line_board({0: [2, 2, 0, 0]}))
        page.evaluate("() => window.game.move('left')")
        page.wait(80)

    page.evaluate("(b) => window.game.load(b)", line_board({0: [2, 2, 0, 0]}))
    page.evaluate("() => window.game.move('left')")
    if page.evaluate("() => window.game.score()") != 4:
        details["restart_control"] = "could not set up a game with a score"
        return False
    worked = try_restart(page, with_score)
    if worked == -1:
        details["restart_control"] = "no visible restart control (a button such as New game or Restart) was found"
        return False
    if worked is None:
        details["restart_control"] = "no visible control started a new game with two tiles and a score of 0"
        return False

    def finished():
        page.evaluate("(b) => window.game.load(b)", STUCK_BOARDS[0])
        page.wait(150)

    again = try_restart(page, finished, preferred=worked)
    if again is None or again == -1:
        details["restart_control"] = "no usable restart control while the game is over"
        return False
    return True


# --------------------------------------------------------------- result file


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


# ------------------------------------------------------------ process control


class ProcessTree:
    """Starts a command so that it and everything it starts can be stopped together."""

    def __init__(self):
        self.process = None
        self.job = None
        self.kernel32 = None

    def start(self, command, env, stdout, stderr):
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        self.process = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, **kwargs)
        if os.name == "nt":
            self._join_job()
        return self.process

    def _join_job(self):
        # A job object that kills its members when closed also reaches processes whose parent has already gone.
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

            class IoCounters(ctypes.Structure):
                _fields_ = [(name, ctypes.c_ulonglong) for name in ("a", "b", "c", "d", "e", "f")]

            class Basic(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class Extended(ctypes.Structure):
                _fields_ = [
                    ("Basic", Basic),
                    ("Io", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                return
            info = Extended()
            info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
                kernel32.CloseHandle(job)
                return
            if not kernel32.AssignProcessToJobObject(job, int(self.process._handle)):
                kernel32.CloseHandle(job)
                return
            self.kernel32, self.job = kernel32, job
        except Exception:
            self.job = None

    def stop(self):
        """Stop the process and everything it started. Safe to call twice."""
        process = self.process
        if process is not None:
            try:
                if os.name == "nt":
                    if process.poll() is None:
                        subprocess.run(
                            ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            stdin=subprocess.DEVNULL,
                            timeout=20,
                            check=False,
                        )
                else:
                    import signal

                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
            except Exception:
                pass
            try:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)
            except Exception:
                pass
        if self.job is not None:
            try:
                self.kernel32.CloseHandle(self.job)
            except Exception:
                pass
            self.job = None


def remove_tree(path: Path) -> None:
    for _ in range(20):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        time.sleep(0.5)


def tail(path: Path, limit=400) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()[-limit:]
    except OSError:
        return ""


# ------------------------------------------------------------------- worker


class Report:
    """What the worker has scored so far. It is rewritten after every check."""

    def __init__(self, path: Path):
        self.path = path
        self.checks: dict = {}
        self.details: dict = {}
        self.console_errors: list = []
        self.page_errors: list = []
        self.network: list = []
        self.load_failure = ""
        self.opened = False
        self.running = False
        self.complete = False
        self.current = ""
        self.environment_error = ""

    def snapshot(self) -> dict:
        checks, details = dict(self.checks), dict(self.details)
        if self.opened:
            problems = self.console_errors + self.page_errors
            checks["page_loads"] = not problems and not self.load_failure
            if self.load_failure:
                details["page_loads"] = self.load_failure
            elif problems:
                details["page_loads"] = "; ".join(problems[:3])
            checks["no_network"] = not self.network
            if self.network:
                details["no_network"] = f"the page asked for {self.network[0]}"
        return {
            "running": self.running,
            "complete": self.complete,
            "current": self.current,
            "environment_error": self.environment_error,
            "checks": checks,
            "details": details,
        }

    def flush(self) -> None:
        partial = self.path.with_name(self.path.name + ".part")
        partial.write_text(json.dumps(self.snapshot()), encoding="utf-8")
        for _ in range(20):
            try:
                os.replace(partial, self.path)
                return
            except OSError:  # the supervisor is reading the file at this instant
                time.sleep(0.02)


def guarded(report: Report, name: str, function) -> None:
    report.current = name
    report.flush()
    try:
        report.checks[name] = bool(function())
    except BudgetExceeded:
        report.checks[name] = False
        report.details.setdefault(name, "not scored: the validator's time budget ran out")
    except Exception as error:  # a broken page fails the check and never crashes the validator
        report.checks[name] = False
        report.details.setdefault(name, f"{type(error).__name__}: {str(error)[:300]}")
    report.flush()


def run_rule_checks(page: Page, report: Report):
    """Play many moves from known boards. Fills six checks and returns whether over() was always right."""
    names = ("merge_directions", "single_merge", "random_boards", "score_increments", "move_return_value", "spawn_rules")
    report.current = "the rules of the game"
    report.flush()
    runner = Runner(page)
    try:
        basic, chain, noop, rand = (runner.run_cases(group()) for group in (basic_cases, chain_cases, noop_cases, random_cases))
        sequence = runner.run_sequences()
    except Exception as error:
        message = "not scored: the validator's time budget ran out" if isinstance(error, BudgetExceeded) else f"{type(error).__name__}: {str(error)[:300]}"
        for name in names:
            report.checks[name] = False
            report.details[name] = message
        report.flush()
        return False
    everything = basic + chain + noop + rand + sequence
    results = {
        "merge_directions": (all_ok(basic, "merge_ok"), describe(basic, "merge_ok")),
        "single_merge": (all_ok(chain, "merge_ok"), describe(chain, "merge_ok")),
        "random_boards": (all_ok(rand + noop + sequence, "merge_ok"), describe(rand + noop + sequence, "merge_ok")),
        "score_increments": (
            all_ok(basic + chain + sequence, "score_ok") and all_ok(noop, "score_ok"),
            describe(basic + chain + sequence + noop, "score_ok"),
        ),
        "move_return_value": (all_ok(everything, "return_ok"), describe(everything, "return_ok")),
        "spawn_rules": (all_ok(everything, "spawn_ok"), describe(everything, "spawn_ok")),
    }
    for name, (ok, why) in results.items():
        report.checks[name] = ok
        report.details[name] = why
    report.flush()
    return all_ok(everything, "over_ok")


def run_session(context, workspace: Path, report: Report, beat_path: Path) -> None:
    network = report.network

    def on_route(route):
        url = route.request.url
        if url.lower().startswith(("file:", "data:", "blob:", "about:")):
            route.continue_()
        else:
            network.append(url)
            route.abort()

    def on_socket(socket):
        # Never connected to a server, so nothing leaves the machine. Closing it from here would deadlock.
        network.append(socket.url)

    def on_console(message):
        if message.type != "error":
            return
        location = (message.location or {}).get("url") or ""
        text = message.text or ""
        # Failures of requests the validator blocked are already scored by no_network.
        if re.match(r"(?i)(https?|wss?|ftp):", location) or "net::ERR" in text or text.startswith("WebSocket connection to"):
            return
        report.console_errors.append(text)

    context.route(re.compile(r".*"), on_route)
    context.route_web_socket(re.compile(r".*"), on_socket)
    page = context.new_page()
    page.set_default_timeout(5000)
    page.on("console", on_console)
    page.on("pageerror", lambda error: report.page_errors.append(str(error)))
    page.on("websocket", lambda socket: network.append(socket.url))
    page.on("dialog", lambda dialog: dialog.dismiss())
    wrapped = Page(page, beat_path, time.monotonic() + SOFT_DEADLINE_SECONDS)

    report.current = "loading the page"
    report.opened = True
    try:
        wrapped.tick()
        page.goto((workspace / "index.html").as_uri(), wait_until="load")
        page.wait_for_timeout(400)
    except Exception as error:
        report.load_failure = f"the page did not load: {type(error).__name__}: {str(error)[:300]}"
        report.flush()
        return
    report.flush()

    details = report.details
    guarded(report, "api_present", lambda: check_api(wrapped, details))
    if not report.checks["api_present"]:
        return
    over_ok = run_rule_checks(wrapped, report)
    guarded(report, "reset_state", lambda: check_reset_and_load(wrapped, details))
    guarded(report, "seeded_reset", lambda: check_seeded(wrapped, details))
    guarded(report, "game_over_detect", lambda: check_game_over_state(wrapped, details) and over_ok)
    if not over_ok and report.checks.get("game_over_detect") is False and "game_over_detect" not in details:
        details["game_over_detect"] = "over() disagreed with the board after a move"

    shared: dict = {}

    def display():
        ok, static_tokens = check_display(wrapped, details)
        shared["static"] = static_tokens
        return ok

    guarded(report, "display_matches_board", display)
    if "static" in shared:
        guarded(report, "arrow_keys", lambda: check_arrow_keys(wrapped, details, shared["static"]))
        guarded(report, "visible_score", lambda: check_visible_score(wrapped, details, shared["static"]))
    else:
        for name in ("arrow_keys", "visible_score"):
            report.checks[name] = False
            details.setdefault(name, "not scored: the page text could not be read")
    guarded(report, "game_over_shown", lambda: check_game_over_shown(wrapped, details))
    guarded(report, "restart_control", lambda: check_restart(wrapped, details))


def worker_main(args) -> int:
    report = Report(args.state)
    report.flush()
    beat_path = args.beat
    try:
        from playwright.sync_api import sync_playwright
    except Exception as error:
        report.environment_error = f"playwright is not installed ({type(error).__name__}). {INSTALL_HINT}"
        report.flush()
        return 3
    try:
        with sync_playwright() as playwright:
            try:
                os.utime(beat_path, None)
                context = playwright.chromium.launch_persistent_context(
                    str(args.profile),
                    headless=True,
                    viewport={"width": 1000, "height": 900},
                    args=["--host-resolver-rules=MAP * ~NOTFOUND"],
                    service_workers="block",
                    timeout=40000,
                )
            except Exception as error:
                report.environment_error = f"Chromium could not be started ({type(error).__name__}: {str(error)[:300]}). {INSTALL_HINT}"
                report.flush()
                return 3
            report.running = True
            report.flush()
            try:
                run_session(context, args.workspace, report, beat_path)
            except Exception as error:
                report.details.setdefault("validator", f"stopped early: {type(error).__name__}: {str(error)[:300]}")
            finally:
                report.complete = True
                report.current = ""
                report.flush()
                args.done.write_text("done", encoding="utf-8")
                try:
                    context.close()
                except Exception:
                    pass
    except Exception as error:
        if not report.running:
            report.environment_error = f"Playwright could not be started ({type(error).__name__}: {str(error)[:300]}). {INSTALL_HINT}"
            report.flush()
            return 3
    return 0


# --------------------------------------------------------------- supervisor


def read_state(path: Path):
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else None
    except (OSError, ValueError):
        return None


def supervise(workspace: Path, weights, checks: dict, details: dict) -> None:
    """Run the worker and fill checks and details from what it finished. Raises EnvironmentProblem."""
    scratch = Path(tempfile.mkdtemp(prefix="bench-game-"))
    tree = ProcessTree()
    try:
        state_path, beat_path, done_path = scratch / "state.json", scratch / "beat", scratch / "done"
        out_path, err_path = scratch / "worker.out", scratch / "worker.err"
        beat_path.write_text("", encoding="utf-8")
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            "--workspace", str(workspace),
            "--state", str(state_path),
            "--beat", str(beat_path),
            "--done", str(done_path),
            "--profile", str(scratch / "profile"),
        ]
        # The browser's scratch files go inside the folder that is removed at the end, even if the worker is killed.
        (scratch / "tmp").mkdir()
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "TEMP": str(scratch / "tmp"), "TMP": str(scratch / "tmp"), "TMPDIR": str(scratch / "tmp")}
        started = time.monotonic()
        stopped = ""
        done_seen = None
        with out_path.open("wb") as out, err_path.open("wb") as err:
            process = tree.start(command, env, out, err)
            while process.poll() is None:
                time.sleep(0.2)
                now = time.monotonic()
                state = read_state(state_path) or {}
                if done_path.exists():
                    done_seen = done_seen or now
                    if now - done_seen > LINGER_SECONDS:
                        stopped = "linger"
                        break
                    continue
                idle = time.time() - beat_path.stat().st_mtime
                if now - started > HARD_DEADLINE_SECONDS:
                    stopped = "deadline"
                elif idle > (STALL_SECONDS if state.get("running") else START_SECONDS):
                    stopped = "stall"
                if stopped:
                    break
            tree.stop()
        state = read_state(state_path)
        if state is None or state.get("environment_error"):
            problem = (state or {}).get("environment_error") or f"the browser worker did not start: {tail(err_path)}"
            raise EnvironmentProblem(problem)
        if not state.get("running") and not state.get("complete"):
            raise EnvironmentProblem(f"Chromium did not start in time. {INSTALL_HINT}")
        checks.update(state.get("checks", {}))
        details.update(state.get("details", {}))
        if not state.get("complete"):
            where = state.get("current") or "the page"
            if stopped == "stall":
                reason = f"the page stopped responding for {STALL_SECONDS} seconds while the validator was checking {where}"
            elif stopped == "deadline":
                reason = f"the validator's time budget of {HARD_DEADLINE_SECONDS} seconds ran out while it was checking {where}"
            else:
                reason = f"the browser worker stopped while the validator was checking {where}: {tail(err_path)}"
            details["validator"] = reason
            for name in weights:
                if name not in checks:
                    checks[name] = False
                    details.setdefault(name, "not scored: the validator stopped early, see the validator note")
    finally:
        tree.stop()
        remove_tree(scratch)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--state", type=Path)
    parser.add_argument("--beat", type=Path)
    parser.add_argument("--done", type=Path)
    parser.add_argument("--profile", type=Path)
    args = parser.parse_args()
    args.workspace = args.workspace.resolve()
    if args.worker:
        return worker_main(args)
    if args.output is None:
        parser.error("--output is required")

    weights = None
    checks: dict = {}
    details: dict = {}
    try:
        weights = score_weights()
        try:
            import playwright.sync_api  # noqa: F401
        except Exception as error:
            raise EnvironmentProblem(f"playwright is not installed ({type(error).__name__}). {INSTALL_HINT}") from error
        if not (args.workspace / "index.html").is_file():
            checks["page_loads"] = False
            details["page_loads"] = "index.html is missing"
        else:
            supervise(args.workspace, weights, checks, details)
    except EnvironmentProblem as problem:
        return write_environment_error(args.output, weights, str(problem))
    except Exception as error:  # a validator bug still leaves a result holding what was scored
        details["validator"] = f"validator error: {type(error).__name__}: {str(error)[:300]}"
        if weights is None:
            return write_environment_error(args.output, weights, details["validator"])
    return write_result(args.output, weights, checks, details)


if __name__ == "__main__":
    raise SystemExit(main())

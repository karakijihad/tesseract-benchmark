"""Drive an already running TESSERACT backend through its WebSocket.

The runner starts no process for this contestant. It connects to a backend
that is already up, opens a fresh chat, picks the model, sends the brief and
watches the chat until the work is really finished.

The conversation, in the order the adapter has it:

1. `POST /api/settings/granted-folders` with `{"path": <workspace>}`. TESSERACT's
   file tools only reach folders under its own home unless the folder is
   granted. The route answers a request from the same machine only, so the
   backend URL must be a loopback address. The grant is removed again with
   `DELETE /api/settings/granted-folders/<quoted path>` when the run ends,
   unless the folder was already granted before the run.
2. Connect to `/ws`. The handshake has no token. It refuses a request whose
   Origin header is outside an allow list and lets one with no Origin through,
   so this client sends none. A backend that is still starting closes the
   socket with code 1013 before sending anything; the adapter retries.
3. The first frame is `session_created`. Then `{"type": "chat.create"}` is
   answered by `chat_created` (`data.chat_id`, 32 hex characters) or by
   `chat_create_failed` (`data.reason`). Creating a chat also focuses it.
4. `{"type": "command", "data": {"cmd": "/model <name>"}}` is answered by a
   `command_result` frame with `data.command == "model"`, `data.ok` and
   `data.reason`. The names are the short names of the models in the
   backend's chat model chain, lowercase letters, digits and hyphens, plus
   `default`.
5. `{"type": "chat_message", "data": {"chat_id": ..., "text": ...}}` carries
   the brief. A text over 32000 characters is refused by the backend, so the
   adapter refuses it first.
6. While the chat runs, frames are filtered on their top level `chat_id`
   (one socket carries every open chat). `loop_start` and `loop_end` bracket a
   turn. `tool_ask` (`data.call_id`) is answered by
   `{"type": "tool_response", "data": {"call_id", "approved": <bool>}}` and
   must be answered within 30 seconds or it counts as a refusal.
   `cost_overage_ask` (`data.call_id`) is always refused with
   `cost_overage_response`. `question_ask` (`data.question_id`) has no timeout
   on the backend, so it is declined with `question_decline`, which ends that
   turn.
7. Stop is `{"type": "cancel_stream"}`. It ends every turn of the session.
   Background delegates keep running.

Finished means: a `loop_end` has arrived, no turn is open, and nothing has
begun again for `quiet_seconds`. A new `loop_start` in that window, which is
what a queued follow up or a background task waking the chat produces, starts
the wait over. The socket stays open the whole time, because the backend only
starts a wake turn for a session that is still connected, and closing it
cancels the turns it is running.

Spend comes from the backend's cost log, a file, because no HTTP route reports
the spend of one chat while it runs. Every row carries `chat_id` and
`cost_usd`; rows are `{"ts", "chat_id", "phase", "cost_usd", "unpriced"?, ...}`.
The log is read from `home` plus `cost_log`, appending only what is new. The
`session_stats` frame also carries `data.conversation_usd` after every turn and
is used as a second reading, so a wrong `cost_log` path cannot disarm the
budget stop for good.

Model, tool and delegate counts and tokens come from the chat's event files,
`<home>/workstreams/<chat_id>/<YYYY-MM-DD>/events.jsonl`, one JSON object per
line:

    {"v", "id", "seq", "ts", "kind", "chat_id", "run_id", "turn_id",
     "generation", "parent_id", "data": {...}}

`kind` is `model.attempt` (`data.model`), `model.outcome`
(`data.usage.{input_tokens, output_tokens, cached_tokens,
cache_creation_tokens, reasoning_tokens}` when the call succeeded),
`tool.start` (`data.tool`) or `delegate.outcome`. Days are read in folder name
order. A chat that was created by this run has no other layout.

Known gaps in what the backend records, reported as unavailable rather than
as zero: a command line model lane and a delegate write no cost rows, and a
background call that belongs to no chat carries no `chat_id`. A chat with no
cost rows therefore reports no cost, and one that used delegates reports its
cost as an estimate that is a lower bound.

The adapter keeps one thread and one loop. Its waits are bounded by short
receive timeouts, so the budget, the time limit and the quiet period are
checked even when the backend sends nothing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path, PurePath
import re
import time
from typing import Any, Mapping
import urllib.error
import urllib.parse
import urllib.request

from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.sync.client import connect

from ..record import Usage
from .base import (
    Adapter,
    AdapterResult,
    LaunchRequest,
    write_transcript_footer,
    write_transcript_header,
)

RUNNER_KEYS = frozenset({"adapter", "model", "unattended_mode", "env_passthrough"})
OWN_KEYS = frozenset(
    {
        "url",
        "home",
        "model",
        "max_budget_usd",
        "quiet_seconds",
        "tool_ask_policy",
        "poll_seconds",
        "cost_log",
    }
)
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
TOOL_ASK_POLICIES = ("allow", "deny")
DEFAULT_POLL_SECONDS = 2.0
DEFAULT_COST_LOG = "logs/cost-tracking.jsonl"
MODEL_NAME = re.compile(r"^[a-z0-9-]+$")
CHAT_ID = re.compile(r"^[0-9a-f]{32}$")
DAY_FOLDER = re.compile(r"^\d{4}-\d{2}-\d{2}$")

MAX_MESSAGE_CHARS = 32_000
MAX_FRAME_BYTES = 64 * 1024 * 1024
OPEN_TIMEOUT_SECONDS = 15.0
CONNECT_PATIENCE_SECONDS = 90.0
CONNECT_RETRY_SECONDS = 3.0
STEP_TIMEOUT_SECONDS = 60.0
START_TIMEOUT_SECONDS = 120.0
STOP_GRACE_SECONDS = 15.0
HTTP_TIMEOUT_SECONDS = 15.0
BACKEND_STARTING_CLOSE_CODE = 1013
MAX_STREAM_ERROR_WARNINGS = 5
WARNING_CHARS = 300
TRANSCRIPT_LINE_CHARS = 600
QUIET_FRAMES = frozenset({"entity_signals", "cost_state"})
MARKER_PHASES = ("clear:", "consolidate:")
UNAVAILABLE_SPEND = (
    "command line model lanes and delegates write no cost rows",
    "background calls that belong to no chat carry no chat id",
)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


@dataclass(frozen=True)
class TesseractSettings:
    ws_uri: str
    http_base: str
    home: Path
    model: str
    max_budget_usd: float
    quiet_seconds: float
    tool_ask_policy: str
    poll_seconds: float
    cost_log: Path


def parse_settings(settings: Mapping[str, Any]) -> TesseractSettings:
    """Check every setting and return them typed. Raises ValueError on the first problem."""
    unknown = sorted(set(settings) - RUNNER_KEYS - OWN_KEYS)
    if unknown:
        raise ValueError("Unknown tesseract setting(s): " + ", ".join(unknown))

    url = settings.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ValueError("Tesseract setting 'url' must be the backend address, for example http://127.0.0.1:8000")
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in ("http", "https", "ws", "wss") or not parsed.hostname:
        raise ValueError("Tesseract setting 'url' must start with http://, https://, ws:// or wss://")
    if parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("Tesseract setting 'url' must be only the scheme, host and port")
    if parsed.hostname.lower() not in LOOPBACK_HOSTS:
        raise ValueError(
            "Tesseract setting 'url' must name this machine (127.0.0.1, ::1 or localhost): "
            "the backend grants a folder only to a caller on the same computer"
        )
    secure = parsed.scheme in ("https", "wss")
    ws_uri = f"{'wss' if secure else 'ws'}://{parsed.netloc}/ws"
    http_base = f"{'https' if secure else 'http'}://{parsed.netloc}"

    home = settings.get("home")
    if not isinstance(home, str) or not home.strip():
        raise ValueError("Tesseract setting 'home' must be the backend's TESSERACT_HOME folder")
    home_path = Path(home.strip())
    if not home_path.is_dir():
        raise ValueError("Tesseract setting 'home' is not an existing folder")

    model = settings.get("model")
    if not isinstance(model, str) or not MODEL_NAME.match(model):
        raise ValueError(
            "Tesseract setting 'model' must be a /model name: lowercase letters, digits and hyphens, "
            "or 'default'"
        )

    budget = settings.get("max_budget_usd")
    if not _is_number(budget) or budget <= 0:
        raise ValueError("Tesseract setting 'max_budget_usd' must be a number above 0")

    quiet = settings.get("quiet_seconds")
    if not _is_number(quiet) or quiet < 0 or quiet > 3600:
        raise ValueError("Tesseract setting 'quiet_seconds' must be a number from 0 to 3600")

    policy = settings.get("tool_ask_policy")
    if policy not in TOOL_ASK_POLICIES:
        raise ValueError("Tesseract setting 'tool_ask_policy' must be one of " + ", ".join(TOOL_ASK_POLICIES))

    poll = settings.get("poll_seconds", DEFAULT_POLL_SECONDS)
    if not _is_number(poll) or poll <= 0 or poll > 60:
        raise ValueError("Tesseract setting 'poll_seconds' must be a number above 0 and at most 60")

    cost_log = settings.get("cost_log", DEFAULT_COST_LOG)
    relative = PurePath(cost_log) if isinstance(cost_log, str) and cost_log.strip() else None
    if relative is None or relative.is_absolute() or relative.drive or ".." in relative.parts:
        raise ValueError("Tesseract setting 'cost_log' must be a path inside 'home', written relative to it")

    return TesseractSettings(
        ws_uri=ws_uri,
        http_base=http_base,
        home=home_path,
        model=model,
        max_budget_usd=float(budget),
        quiet_seconds=float(quiet),
        tool_ask_policy=policy,
        poll_seconds=float(poll),
        cost_log=Path(*relative.parts),
    )


class _Fail(Exception):
    """The run cannot go on. The message is what the record says."""


class _Closed(_Fail):
    """The backend closed the socket."""

    def __init__(self, text: str, code: int | None) -> None:
        super().__init__(text)
        self.code = code


class CostLog:
    """The cost rows of one chat, read from the backend's append-only log."""

    def __init__(self, path: Path, chat_id: str) -> None:
        self.path = path
        self.chat_id = chat_id
        self.offset = 0
        self.rows: list[dict[str, Any]] = []

    def poll(self) -> float:
        """Read what was appended since the last call and return the chat's spend so far."""
        try:
            size = self.path.stat().st_size
        except OSError:
            return self.total()
        if size < self.offset:  # the file was replaced under us: read it again from the start
            self.offset = 0
            self.rows = []
        if size > self.offset:
            try:
                with self.path.open("rb") as handle:
                    handle.seek(self.offset)
                    data = handle.read(size - self.offset)
            except OSError:
                return self.total()
            self._take(data)
        return self.total()

    def _take(self, data: bytes) -> None:
        consumed = 0
        for line in data.splitlines(keepends=True):
            row = self._parse(line)
            if row is None and not line.endswith(b"\n"):
                break  # an unfinished last line: leave it for the next read
            consumed += len(line)
            if row is not None and row.get("chat_id") == self.chat_id:
                self.rows.append(row)
        self.offset += consumed

    @staticmethod
    def _parse(line: bytes) -> dict[str, Any] | None:
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            return None
        try:
            row = json.loads(text)
        except json.JSONDecodeError:
            return None
        return row if isinstance(row, dict) else None

    def billed_rows(self) -> list[dict[str, Any]]:
        return [r for r in self.rows if not str(r.get("phase") or "").startswith(MARKER_PHASES)]

    def total(self) -> float:
        total = 0.0
        for row in self.rows:
            value = row.get("cost_usd")
            if _is_number(value):
                total += float(value)
        return total


@dataclass
class EventSummary:
    found: bool = False
    model_calls: int = 0
    tool_calls: int = 0
    sub_agents: int = 0
    models: list[str] = field(default_factory=list)
    tokens: dict[str, int] = field(default_factory=dict)


TOKEN_KEYS = ("input_tokens", "output_tokens", "cached_tokens", "cache_creation_tokens", "reasoning_tokens")


def summarize_events(home: Path, chat_id: str) -> EventSummary:
    """Count models, tools and delegates, and add up tokens, from the chat's event files."""
    root = home / "workstreams" / chat_id
    try:
        days = sorted(p for p in root.iterdir() if p.is_dir() and DAY_FOLDER.match(p.name))
    except OSError:
        return EventSummary()
    summary = EventSummary()
    for day in days:
        path = day / "events.jsonl"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        summary.found = True
        for line in text.splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            data = row.get("data") if isinstance(row, dict) else None
            if not isinstance(data, dict):
                continue
            kind = row.get("kind")
            if kind == "model.attempt":
                model = data.get("model")
                if isinstance(model, str) and model and model not in summary.models:
                    summary.models.append(model)
            elif kind == "model.outcome":
                summary.model_calls += 1
                usage = data.get("usage")
                if isinstance(usage, dict):
                    for key in TOKEN_KEYS:
                        value = usage.get(key)
                        if isinstance(value, int) and not isinstance(value, bool):
                            summary.tokens[key] = summary.tokens.get(key, 0) + value
            elif kind == "tool.start":
                summary.tool_calls += 1
            elif kind == "delegate.outcome":
                summary.sub_agents += 1
    return summary


class _Http:
    """The two folder-grant calls. No proxy: the backend is on this machine."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                payload = response.read()
        except urllib.error.HTTPError as error:
            raise _Fail(f"{method} {path} answered {error.code}: {self._reason(error)}") from None
        except (urllib.error.URLError, OSError) as error:
            raise _Fail(f"could not reach the backend over HTTP ({type(error).__name__})") from None
        try:
            return json.loads(payload.decode("utf-8")) if payload else None
        except ValueError:
            raise _Fail(f"{method} {path} answered something that is not JSON") from None

    @staticmethod
    def _reason(error: urllib.error.HTTPError) -> str:
        try:
            body = json.loads(error.read().decode("utf-8", errors="replace"))
            reason = body.get("error") if isinstance(body, dict) else None
        except (ValueError, OSError):
            reason = None
        return str(reason or error.reason)[:WARNING_CHARS]


def _same_folder(a: str, b: str) -> bool:
    try:
        return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))
    except (OSError, RuntimeError, ValueError):
        return False


class _Channel:
    """The socket, with one transcript line for every frame in either direction."""

    def __init__(self, ws: Any, transcript: Any, started: float) -> None:
        self.ws = ws
        self.transcript = transcript
        self.started = started

    def note(self, text: str) -> None:
        line = f"[{time.monotonic() - self.started:8.2f}s] {text}\n"
        try:
            self.transcript.write(line.encode("utf-8", errors="replace"))
            self.transcript.flush()
        except (OSError, ValueError):
            pass

    def send(self, kind: str, data: Mapping[str, Any] | None = None) -> None:
        frame: dict[str, Any] = {"type": kind}
        if data is not None:
            frame["data"] = dict(data)
        self.note("-> " + self._short(frame))
        try:
            self.ws.send(json.dumps(frame))
        except ConnectionClosed as closed:
            raise _Closed(self._closed_text(closed), self._code(closed)) from None

    def recv(self, timeout: float) -> dict[str, Any] | None:
        """The next JSON object frame, or None when nothing arrived in `timeout` seconds."""
        try:
            raw = self.ws.recv(timeout=max(timeout, 0.001))
        except TimeoutError:
            return None
        except ConnectionClosed as closed:
            raise _Closed(self._closed_text(closed), self._code(closed)) from None
        if isinstance(raw, bytes):
            return None
        try:
            frame = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(frame, dict):
            return None
        if frame.get("type") not in QUIET_FRAMES:
            self.note("<- " + self._short(frame))
        return frame

    @staticmethod
    def _code(closed: ConnectionClosed) -> int | None:
        return closed.rcvd.code if closed.rcvd is not None else None

    @classmethod
    def _closed_text(cls, closed: ConnectionClosed) -> str:
        return f"the backend closed the connection (code {cls._code(closed)})"

    @staticmethod
    def _short(frame: Mapping[str, Any]) -> str:
        shown = {k: v for k, v in frame.items() if k not in ("timestamp", "session_id", "category")}
        text = json.dumps(shown, ensure_ascii=False)
        return text if len(text) <= TRANSCRIPT_LINE_CHARS else text[: TRANSCRIPT_LINE_CHARS - 3] + "..."


@dataclass
class _Outcome:
    sent: bool = False
    started: bool = False
    finished: bool = False
    timed_out: bool = False
    stopped_at_budget: bool = False
    warnings: list[str] = field(default_factory=list)
    asks_approved: int = 0
    asks_denied: int = 0
    questions_declined: int = 0
    overage_refused: int = 0
    stream_errors: int = 0
    session_stats_usd: float | None = None


class _Run:
    def __init__(self, settings: TesseractSettings, request: LaunchRequest) -> None:
        self.settings = settings
        self.request = request
        self.t0 = time.monotonic()
        self.deadline = self.t0 + request.time_limit_seconds
        self.outcome = _Outcome()
        self.chat_id: str | None = None
        self.costlog: CostLog | None = None

    # -- the steps -------------------------------------------------------

    def message(self) -> str:
        workspace = str(self.request.workspace.resolve())
        brief = self.request.brief.replace("{workspace}", workspace)
        return f"Your workspace is the folder {workspace}. Put every file you create or change in it.\n\n{brief}"

    def grant_workspace(self, http: _Http) -> str | None:
        """Grant the workspace. Returns the path to revoke, or None when it was granted already."""
        path = str(self.request.workspace.resolve())
        listing = http.call("GET", "/api/settings/granted-folders")
        before = listing.get("folders") if isinstance(listing, dict) else None
        if isinstance(before, list) and any(
            isinstance(row, dict) and _same_folder(str(row.get("path", "")), path) for row in before
        ):
            return None
        http.call("POST", "/api/settings/granted-folders", {"path": path})
        return path

    def revoke_workspace(self, http: _Http, path: str) -> None:
        try:
            http.call("DELETE", "/api/settings/granted-folders/" + urllib.parse.quote(path, safe=""))
        except _Fail as fail:
            self.outcome.warnings.append(f"the workspace grant could not be removed: {fail}")

    def open_socket(self) -> _Channel:
        patience = min(self.deadline, time.monotonic() + CONNECT_PATIENCE_SECONDS)
        while True:
            try:
                ws = connect(
                    self.settings.ws_uri,
                    open_timeout=OPEN_TIMEOUT_SECONDS,
                    max_size=MAX_FRAME_BYTES,
                )
            except (OSError, TimeoutError, WebSocketException) as error:
                raise _Fail(f"could not open the websocket ({type(error).__name__})") from None
            channel = _Channel(ws, self.request.transcript, self.t0)
            try:
                self.await_frame(channel, lambda f: f.get("type") == "session_created", "the session")
                return channel
            except _Closed as closed:
                self.close(ws)
                if closed.code != BACKEND_STARTING_CLOSE_CODE or time.monotonic() >= patience:
                    raise
                time.sleep(CONNECT_RETRY_SECONDS)
            except _Fail:
                self.close(ws)
                raise

    @staticmethod
    def close(ws: Any) -> None:
        try:
            ws.close()
        except Exception:  # noqa: BLE001 - a socket that will not close cleanly is gone anyway
            pass

    def await_frame(self, channel: _Channel, wanted: Any, what: str) -> dict[str, Any]:
        end = min(time.monotonic() + STEP_TIMEOUT_SECONDS, self.deadline)
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise _Fail(f"timed out waiting for {what}")
            frame = channel.recv(left)
            if frame is None:
                continue
            if wanted(frame):
                return frame
            if frame.get("type") == "stream_error" and not frame.get("chat_id"):
                raise _Fail(f"the backend refused while waiting for {what}: {self.error_text(frame)}")

    @staticmethod
    def error_text(frame: Mapping[str, Any]) -> str:
        data = frame.get("data")
        text = data.get("message") if isinstance(data, dict) else None
        return str(text or "no reason given")[:WARNING_CHARS]

    def open_chat(self, channel: _Channel) -> str:
        channel.send("chat.create", {})
        created = self.await_frame(
            channel,
            lambda f: f.get("type") in ("chat_created", "chat_create_failed"),
            "the new chat",
        )
        data = created.get("data") if isinstance(created.get("data"), dict) else {}
        if created.get("type") == "chat_create_failed":
            raise _Fail(f"the backend could not create a chat: {data.get('reason')}")
        chat_id = data.get("chat_id")
        if not isinstance(chat_id, str) or not CHAT_ID.match(chat_id):
            raise _Fail("the backend returned a chat id of an unexpected shape")
        self.chat_id = chat_id

        channel.send("command", {"cmd": f"/model {self.settings.model}"})
        reply = self.await_frame(
            channel,
            lambda f: f.get("type") == "command_result"
            and isinstance(f.get("data"), dict)
            and f["data"].get("command") == "model",
            "the model choice",
        )
        verdict = reply["data"]
        if verdict.get("ok") is not True:
            raise _Fail(f"the backend did not accept the model: {str(verdict.get('reason'))[:WARNING_CHARS]}")
        return chat_id

    def watch(self, channel: _Channel, chat_id: str, costlog: CostLog) -> None:
        settings, out = self.settings, self.outcome
        sent_at = time.monotonic()
        open_turns = 0
        last_end: float | None = None
        stop_at: float | None = None
        next_poll = sent_at
        try:
            while True:
                now = time.monotonic()
                if stop_at is None:
                    if now >= self.deadline:
                        out.timed_out = True
                        channel.send("cancel_stream")
                        stop_at = now
                    elif now >= next_poll:
                        next_poll = now + settings.poll_seconds
                        spent = max(costlog.poll(), out.session_stats_usd or 0.0)
                        if spent >= settings.max_budget_usd:
                            out.stopped_at_budget = True
                            out.warnings.append(
                                f"stopped at the spend cap of {settings.max_budget_usd} USD "
                                "(a request in flight can land after it)"
                            )
                            channel.send("cancel_stream")
                            stop_at = now
                elif now - stop_at >= STOP_GRACE_SECONDS:
                    out.warnings.append("the turn did not end after Stop; the run was closed anyway")
                    return
                if out.started and open_turns == 0 and last_end is not None and now - last_end >= settings.quiet_seconds:
                    out.finished = True
                    return
                if not out.started and now - sent_at >= START_TIMEOUT_SECONDS:
                    raise _Fail("the backend did not begin a turn for the brief")

                wake = [now + settings.poll_seconds]
                if stop_at is None:
                    wake += [self.deadline, next_poll]
                else:
                    wake.append(stop_at + STOP_GRACE_SECONDS)
                if out.started and open_turns == 0 and last_end is not None:
                    wake.append(last_end + settings.quiet_seconds)
                if not out.started:
                    wake.append(sent_at + START_TIMEOUT_SECONDS)
                frame = channel.recv(min(wake) - now)
                if frame is None:
                    continue
                kind = frame.get("type")
                data = frame.get("data") if isinstance(frame.get("data"), dict) else {}
                frame_chat = frame.get("chat_id")
                mine = frame_chat is None or frame_chat == chat_id
                if kind == "loop_start" and mine:
                    out.started = True
                    open_turns += 1
                    last_end = None
                elif kind == "loop_end" and mine:
                    open_turns = max(0, open_turns - 1)
                    if out.started and open_turns == 0:
                        last_end = time.monotonic()
                elif kind == "tool_ask" and mine:
                    approved = settings.tool_ask_policy == "allow"
                    channel.send("tool_response", {"call_id": data.get("call_id"), "approved": approved})
                    if approved:
                        out.asks_approved += 1
                    else:
                        out.asks_denied += 1
                elif kind == "cost_overage_ask":
                    channel.send("cost_overage_response", {"call_id": data.get("call_id"), "approved": False})
                    out.overage_refused += 1
                elif kind == "question_ask" and data.get("chat_id") in (None, "", chat_id):
                    channel.send("question_decline", {"question_id": data.get("question_id")})
                    out.questions_declined += 1
                elif kind == "session_stats" and frame_chat == chat_id:
                    stats = data.get("conversation_usd")
                    if _is_number(stats):
                        out.session_stats_usd = float(stats)
                elif kind == "stream_error":
                    if not out.started and not frame_chat:
                        raise _Fail(f"the backend refused the brief: {self.error_text(frame)}")
                    out.stream_errors += 1
                    if out.stream_errors <= MAX_STREAM_ERROR_WARNINGS:
                        out.warnings.append(f"backend reported an error: {self.error_text(frame)}")
        except _Closed as closed:
            if not out.started:
                raise
            out.warnings.append(f"{closed}; the work may be unfinished")

    # -- the report ------------------------------------------------------

    def usage(self, chat_id: str, costlog: CostLog) -> Usage:
        settings, out = self.settings, self.outcome
        costlog.poll()
        events = summarize_events(settings.home, chat_id)
        rows = costlog.billed_rows()
        if not costlog.path.exists():
            out.warnings.append("the cost log was not found, so the spend cap relied on the per turn reading")
        if not events.found:
            out.warnings.append("the chat's event files were not found, so calls and tokens are unavailable")

        cost: float | None = None
        basis = "unavailable"
        lower_bound = False
        if rows:
            cost = round(costlog.total(), 8)
            lower_bound = events.sub_agents > 0 or any(r.get("unpriced") for r in rows)
            basis = "estimated" if lower_bound else "exact"

        def tokens(key: str) -> int | None:
            return events.tokens.get(key) if events.found else None

        usage = Usage(
            input_tokens=tokens("input_tokens"),
            output_tokens=tokens("output_tokens"),
            cache_read_tokens=tokens("cached_tokens"),
            cache_write_tokens=tokens("cache_creation_tokens"),
            model_calls=events.model_calls if events.found else None,
            tool_calls=events.tool_calls if events.found else None,
            sub_agents=events.sub_agents if events.found else None,
            cost_usd=cost,
            cost_basis=basis,
        )
        usage.raw = {
            "chat_id": chat_id,
            "stopped_at_budget": out.stopped_at_budget,
            "max_budget_usd": settings.max_budget_usd,
            "ledger_rows": len(rows),
            "cost_is_lower_bound": lower_bound,
            "models": events.models,
            "reasoning_tokens": tokens("reasoning_tokens"),
            "token_source": "events" if events.found else None,
            "session_stats_usd": out.session_stats_usd,
            "tool_asks_approved": out.asks_approved,
            "tool_asks_denied": out.asks_denied,
            "questions_declined": out.questions_declined,
            "overage_asks_refused": out.overage_refused,
            "unavailable_spend": list(UNAVAILABLE_SPEND),
        }
        return usage

    def execute(self) -> AdapterResult:
        settings, out = self.settings, self.outcome
        text = self.message()
        if len(text) > MAX_MESSAGE_CHARS:
            return AdapterResult(
                launch_confirmed=False,
                error=f"the brief is {len(text)} characters and the backend accepts {MAX_MESSAGE_CHARS}",
                usage=Usage.absent(),
            )
        http = _Http(settings.http_base)
        granted: str | None = None
        channel: _Channel | None = None
        error: str | None = None
        try:
            granted = self.grant_workspace(http)
            channel = self.open_socket()
            chat_id = self.open_chat(channel)
            self.costlog = CostLog(settings.home / settings.cost_log, chat_id)
            channel.send("chat_message", {"chat_id": chat_id, "text": text})
            out.sent = True
            self.watch(channel, chat_id, self.costlog)
        except _Fail as fail:
            error = str(fail)
        finally:
            if channel is not None:
                self.close(channel.ws)
            if granted is not None:
                self.revoke_workspace(http, granted)

        usage = Usage.absent()
        if self.chat_id is not None and self.costlog is not None:
            usage = self.usage(self.chat_id, self.costlog)
        if error is None and not out.started:
            error = "the backend never began a turn for the brief"
        return AdapterResult(
            prompt_delivery="socket" if out.sent else None,
            launch_confirmed=out.started,
            launch_method="websocket_chat_message",
            exit_status=0 if out.finished else None,
            timed_out=out.timed_out,
            error=None if out.started else error,
            usage=usage,
            warnings=out.warnings,
        )


class TesseractAdapter(Adapter):
    name = "tesseract"

    def validate_settings(self, settings: Mapping[str, Any]) -> None:
        parse_settings(settings)

    def run(self, request: LaunchRequest) -> AdapterResult:
        settings = parse_settings(request.settings)
        write_transcript_header(request.transcript, ["tesseract-websocket", f"model={settings.model}"])
        outcome: AdapterResult | None = None
        try:
            outcome = _Run(settings, request).execute()
        finally:
            write_transcript_footer(
                request.transcript,
                outcome.exit_status if outcome is not None else None,
                outcome.timed_out if outcome is not None else False,
            )
        return outcome

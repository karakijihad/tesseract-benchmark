"""The tesseract adapter against a fake backend that speaks the same protocol.

The fake is a small aiohttp server on one port, like the real backend: the
`/ws` socket and the folder-grant routes share it. It plays the backend's side
of the conversation (create a chat, take the model, run a turn that asks for a
tool, end the turn) and writes the cost log and the event files where the real
backend writes them, so the adapter's reads are checked against real shapes.
"""
from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
import threading
import time
from typing import Any

from aiohttp import WSMsgType, web
import pytest

from runner.adapters import tesseract as adapter_module
from runner.adapters.base import LaunchRequest
from runner.adapters.tesseract import CostLog, TesseractAdapter

CHAT = "a" * 32
OTHER_CHAT = "b" * 32
DAY = "2001-02-03"


class FakeBackend:
    def __init__(self, home: Path, script: str = "normal", **options: Any) -> None:
        self.home = home
        self.script = script
        self.options = options
        self.received: list[dict[str, Any]] = []
        self.http_log: list[tuple[str, str, Any]] = []
        self.origins: list[str | None] = []
        self.connections = 0
        self.loop_ends: list[float] = []
        self.closed_at: float | None = None
        self.granted: list[str] = []
        self.port = 0
        self._signals: dict[str, asyncio.Event] = {}
        self._tasks: list[asyncio.Future[Any]] = []

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self.loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run() -> None:
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self._serve())
            ready.set()
            self.loop.run_forever()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        assert ready.wait(10)

    async def _shutdown(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.runner.cleanup()

    def stop(self) -> None:
        asyncio.run_coroutine_threadsafe(self._shutdown(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)
        self.loop.close()

    async def _serve(self) -> None:
        app = web.Application()
        app.router.add_get("/ws", self.ws_handler)
        app.router.add_get("/api/settings/granted-folders", self.list_grants)
        app.router.add_post("/api/settings/granted-folders", self.add_grant)
        app.router.add_delete("/api/settings/granted-folders/{path:.*}", self.remove_grant)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = self.runner.addresses[0][1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # -- folder grants ---------------------------------------------------

    def folders(self) -> dict[str, Any]:
        return {"folders": [{"path": p, "exists": True} for p in self.granted]}

    async def list_grants(self, request: web.Request) -> web.Response:
        self.http_log.append(("GET", request.path, None))
        return web.json_response(self.folders())

    async def add_grant(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.http_log.append(("POST", request.path, body))
        if self.options.get("refuse_grant"):
            return web.json_response({"error": "That folder is the app's own."}, status=400)
        self.granted.append(body["path"])
        return web.json_response(self.folders())

    async def remove_grant(self, request: web.Request) -> web.Response:
        path = request.match_info["path"]
        self.http_log.append(("DELETE", request.path, path))
        self.granted = [p for p in self.granted if p != path]
        return web.json_response(self.folders())

    # -- the socket ------------------------------------------------------

    def signal(self, name: str) -> asyncio.Event:
        return self._signals.setdefault(name, asyncio.Event())

    async def send(
        self, ws: web.WebSocketResponse, kind: str, data: dict[str, Any] | None = None, chat: str | None = None
    ) -> None:
        frame: dict[str, Any] = {
            "type": kind,
            "category": "test",
            "session_id": "s",
            "timestamp": "t",
            "data": data or {},
        }
        if chat:
            frame["chat_id"] = chat
        await ws.send_json(frame)

    async def ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.connections += 1
        self.origins.append(request.headers.get("Origin"))
        if self.script == "booting" and self.connections == 1:
            await ws.close(code=1013, message=b"chat infra booting")
            return ws
        await self.send(ws, "session_created", {"session_id": "s", "active_chat_id": OTHER_CHAT, "chats": []})
        async for message in ws:
            if message.type != WSMsgType.TEXT:
                continue
            frame = json.loads(message.data)
            self.received.append(frame)
            await self.on_message(ws, frame)
        self.closed_at = time.monotonic()
        return ws

    async def on_message(self, ws: web.WebSocketResponse, frame: dict[str, Any]) -> None:
        kind, data = frame["type"], frame.get("data") or {}
        if kind == "chat.create":
            await self.send(ws, "chat_created", {"chat_id": CHAT, "title": "New", "created_at": "t"})
        elif kind == "command":
            await self.send(ws, "command_running", {"name": "model"})
            if data["cmd"].split()[1] == "bogus":
                result = {"command": "model", "ok": False, "reason": "No model has that name.", "severity": "warning"}
            else:
                result = {"command": "model", "ok": True, "reason": "This chat now answers.", "severity": "info"}
            await self.send(ws, "command_result", result)
        elif kind == "chat_message":
            self._tasks.append(asyncio.ensure_future(self.run_turn(ws, data)))
        else:
            self.signal(kind).set()

    # -- what the backend writes to disk ---------------------------------

    def write_ledger(self, *rows: tuple[str, float, str]) -> None:
        path = self.home / "logs" / "cost-tracking.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for chat_id, cost, phase in rows:
                handle.write(
                    json.dumps({"ts": "t", "chat_id": chat_id, "phase": phase, "cost_usd": cost, "role": "chat_brain"})
                    + "\n"
                )

    def write_events(self) -> None:
        folder = self.home / "workstreams" / CHAT / DAY
        folder.mkdir(parents=True, exist_ok=True)
        usage = [
            {"input_tokens": 100, "output_tokens": 20, "cached_tokens": 30, "cache_creation_tokens": 5, "reasoning_tokens": 7},
            {"input_tokens": 50, "output_tokens": 10, "cached_tokens": 0, "cache_creation_tokens": 0, "reasoning_tokens": 0},
        ]
        rows: list[dict[str, Any]] = []
        for item in usage:
            rows.append({"kind": "model.attempt", "data": {"model": "fake-model", "provider": "p", "role": "chat_brain"}})
            rows.append({"kind": "model.outcome", "data": {"status": "ok", "usage": item}})
        for name in ("file_write", "file_read"):
            rows.append({"kind": "tool.start", "data": {"tool": name, "call_id": name}})
            rows.append({"kind": "tool.outcome", "data": {"status": "ok"}})
        for _ in range(self.options.get("delegates", 0)):
            rows.append({"kind": "delegate.outcome", "data": {"delegate": "d"}})
        with (folder / "events.jsonl").open("w", encoding="utf-8") as handle:
            for seq, row in enumerate(rows, 1):
                handle.write(json.dumps({"v": 1, "seq": seq, "chat_id": CHAT, **row}) + "\n")

    # -- the turns -------------------------------------------------------

    async def run_turn(self, ws: web.WebSocketResponse, data: dict[str, Any]) -> None:
        script = self.script
        if script == "no_turn":
            await self.send(ws, "stream_error", {"message": "unknown chat"})
            return
        await self.send(ws, "loop_start", {"turn": 1}, CHAT)
        if script == "budget":
            self.write_ledger((CHAT, 0.01, ""), (OTHER_CHAT, 9.0, ""))
            await asyncio.sleep(0.15)
            self.write_ledger((CHAT, 0.05, ""))
            await asyncio.wait_for(self.signal("cancel_stream").wait(), 10)
        elif script == "deaf":
            await asyncio.wait_for(self.signal("cancel_stream").wait(), 10)
            await asyncio.sleep(30)
        elif script == "extra_asks":
            await self.send(ws, "question_ask", {"question_id": "q1", "chat_id": CHAT, "questions": []}, CHAT)
            await self.send(ws, "cost_overage_ask", {"call_id": "o1", "scope_key": "daily"})
            await asyncio.wait_for(self.signal("question_decline").wait(), 10)
            await asyncio.wait_for(self.signal("cost_overage_response").wait(), 10)
        else:
            await self.send(ws, "tool_ask", {"call_id": "call1", "name": "file_write", "input": {}, "reason": "r"}, CHAT)
            await asyncio.wait_for(self.signal("tool_response").wait(), 10)
            if self.options.get("files", True):
                self.write_ledger((CHAT, 0.0125, ""), (CHAT, 0.0075, ""), (CHAT, 0.0, "clear:x"), (OTHER_CHAT, 5.0, ""))
                self.write_events()
            await self.send(ws, "session_stats", {"conversation_usd": 0.02}, CHAT)
        await self.send(ws, "loop_end", {"turn": 1, "tokens_used": 0}, CHAT)
        self.loop_ends.append(time.monotonic())
        if script == "restart":
            await asyncio.sleep(0.1)
            await self.send(ws, "loop_start", {"turn": 9}, OTHER_CHAT)
            await asyncio.sleep(0.05)
            await self.send(ws, "loop_start", {"turn": 2}, CHAT)
            await asyncio.sleep(0.5)
            await self.send(ws, "loop_end", {"turn": 2, "tokens_used": 0}, CHAT)
            self.loop_ends.append(time.monotonic())


@pytest.fixture
def make_backend(tmp_path: Path):
    started: list[FakeBackend] = []

    def make(script: str = "normal", **options: Any) -> FakeBackend:
        home = tmp_path / f"home{len(started)}"
        home.mkdir()
        backend = FakeBackend(home, script, **options)
        backend.start()
        started.append(backend)
        return backend

    yield make
    for backend in started:
        backend.stop()


@pytest.fixture(autouse=True)
def quick_waits(monkeypatch):
    monkeypatch.setattr(adapter_module, "CONNECT_RETRY_SECONDS", 0.05)
    monkeypatch.setattr(adapter_module, "STOP_GRACE_SECONDS", 0.4)


def settings_for(backend: FakeBackend, **overrides: Any) -> dict[str, Any]:
    return {
        "adapter": "tesseract",
        "url": backend.url,
        "home": str(backend.home),
        "model": "sonnet",
        "max_budget_usd": 1.0,
        "quiet_seconds": 0.3,
        "tool_ask_policy": "allow",
        "poll_seconds": 0.05,
        **overrides,
    }


def run_adapter(tmp_path: Path, backend: FakeBackend, brief: str = "Write answer.txt.", limit: float = 30, **overrides: Any):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    transcript = io.BytesIO()
    request = LaunchRequest(
        workspace=workspace,
        brief=brief,
        time_limit_seconds=limit,
        settings=settings_for(backend, **overrides),
        env={},
        transcript=transcript,
    )
    result = TesseractAdapter().run(request)
    return result, transcript.getvalue().decode("utf-8"), workspace


def kinds(backend: FakeBackend) -> list[str]:
    return [frame["type"] for frame in backend.received]


def test_a_full_run_follows_the_whole_sequence(tmp_path, make_backend):
    backend = make_backend("normal")
    result, transcript, workspace = run_adapter(tmp_path, backend)

    assert kinds(backend) == ["chat.create", "command", "chat_message", "tool_response"]
    assert backend.received[1]["data"]["cmd"] == "/model sonnet"
    message = backend.received[2]["data"]
    assert message["chat_id"] == CHAT
    assert str(workspace.resolve()) in message["text"] and "Write answer.txt." in message["text"]
    assert backend.received[3]["data"] == {"call_id": "call1", "approved": True}
    assert backend.origins == [None]

    resolved = str(workspace.resolve())
    assert [(m, d) for m, _, d in backend.http_log if m != "GET"] == [("POST", {"path": resolved}), ("DELETE", resolved)]
    assert backend.granted == []

    assert result.error is None and result.launch_confirmed is True and result.timed_out is False
    assert result.prompt_delivery == "socket" and result.exit_status == 0
    assert backend.closed_at - backend.loop_ends[-1] >= 0.27, "the socket closed before the quiet period ended"
    assert "loop_end" in transcript and "exit_status: 0" in transcript

    usage = result.usage
    assert usage.cost_usd == pytest.approx(0.02) and usage.cost_basis == "exact"
    # 150 input of which 30 were cache reads: input is the full-rate part only.
    assert (usage.input_tokens, usage.output_tokens) == (120, 30)
    assert (usage.cache_read_tokens, usage.cache_write_tokens) == (30, 5)
    assert (usage.model_calls, usage.tool_calls, usage.sub_agents) == (2, 2, 0)
    assert usage.raw["models"] == ["fake-model"]
    assert usage.raw["stopped_at_budget"] is False and usage.raw["ledger_rows"] == 2
    assert usage.raw["session_stats_usd"] == pytest.approx(0.02)


def test_the_deny_policy_refuses_a_tool_ask(tmp_path, make_backend):
    backend = make_backend("normal")
    result, _, _ = run_adapter(tmp_path, backend, tool_ask_policy="deny")

    assert backend.received[3]["data"] == {"call_id": "call1", "approved": False}
    assert result.usage.raw["tool_asks_denied"] == 1 and result.usage.raw["tool_asks_approved"] == 0


def test_a_new_turn_inside_the_quiet_period_starts_the_wait_over(tmp_path, make_backend):
    backend = make_backend("restart")
    result, _, _ = run_adapter(tmp_path, backend)

    assert len(backend.loop_ends) == 2
    assert result.exit_status == 0
    assert backend.closed_at - backend.loop_ends[-1] >= 0.27
    assert result.warnings == [], "another chat's open turn must not count as this chat's"


def test_the_budget_stop_presses_stop_and_marks_the_usage(tmp_path, make_backend):
    backend = make_backend("budget")
    result, _, _ = run_adapter(tmp_path, backend, max_budget_usd=0.04)

    assert "cancel_stream" in kinds(backend)
    assert result.usage.raw["stopped_at_budget"] is True
    assert result.usage.cost_usd == pytest.approx(0.06) and result.usage.cost_basis == "exact"
    assert result.exit_status == 0 and result.timed_out is False and result.error is None
    assert any("spend cap" in warning for warning in result.warnings)


def test_the_time_limit_presses_stop_and_does_not_wait_forever(tmp_path, make_backend):
    backend = make_backend("deaf")
    started = time.monotonic()
    result, _, _ = run_adapter(tmp_path, backend, limit=0.8)

    assert time.monotonic() - started < 5
    assert "cancel_stream" in kinds(backend)
    assert result.timed_out is True and result.exit_status is None and result.error is None
    assert any("did not end after Stop" in warning for warning in result.warnings)


def test_a_chat_with_no_ledger_rows_and_no_events_reports_nothing_not_zero(tmp_path, make_backend):
    backend = make_backend("normal", files=False)
    result, _, _ = run_adapter(tmp_path, backend)

    usage = result.usage
    assert result.exit_status == 0
    assert usage.cost_usd is None and usage.cost_basis == "unavailable"
    assert usage.input_tokens is None and usage.model_calls is None and usage.tool_calls is None
    assert any("event files were not found" in warning for warning in result.warnings)


def test_delegates_make_the_cost_a_lower_bound(tmp_path, make_backend):
    backend = make_backend("normal", delegates=2)
    result, _, _ = run_adapter(tmp_path, backend)

    assert result.usage.sub_agents == 2
    assert result.usage.cost_basis == "estimated" and result.usage.raw["cost_is_lower_bound"] is True


def test_questions_and_overage_asks_are_answered_so_the_turn_can_end(tmp_path, make_backend):
    backend = make_backend("extra_asks")
    result, _, _ = run_adapter(tmp_path, backend)

    sent = {frame["type"]: frame["data"] for frame in backend.received}
    assert sent["question_decline"] == {"question_id": "q1"}
    assert sent["cost_overage_response"] == {"call_id": "o1", "approved": False}
    assert result.exit_status == 0
    assert result.usage.raw["questions_declined"] == 1 and result.usage.raw["overage_asks_refused"] == 1


def test_a_backend_that_is_still_starting_is_retried(tmp_path, make_backend):
    backend = make_backend("booting")
    result, _, _ = run_adapter(tmp_path, backend)

    assert backend.connections == 2
    assert result.exit_status == 0 and result.error is None


def test_a_refused_model_stops_before_the_brief_is_sent(tmp_path, make_backend):
    backend = make_backend("normal")
    result, _, _ = run_adapter(tmp_path, backend, model="bogus")

    assert "chat_message" not in kinds(backend)
    assert result.launch_confirmed is False and "No model has that name" in result.error
    assert backend.granted == [], "the grant must be removed even when the run never starts"


def test_a_refused_grant_never_opens_the_socket(tmp_path, make_backend):
    backend = make_backend("normal", refuse_grant=True)
    result, _, _ = run_adapter(tmp_path, backend)

    assert backend.connections == 0
    assert result.launch_confirmed is False and "400" in result.error


def test_a_backend_that_refuses_the_brief_is_a_failed_launch(tmp_path, make_backend):
    backend = make_backend("no_turn")
    result, _, _ = run_adapter(tmp_path, backend)

    assert result.launch_confirmed is False and "refused the brief" in result.error


def test_a_brief_the_backend_would_refuse_is_refused_first(tmp_path, make_backend):
    backend = make_backend("normal")
    result, _, _ = run_adapter(tmp_path, backend, brief="x" * 40_000)

    assert backend.http_log == [] and backend.connections == 0
    assert result.launch_confirmed is False and "accepts 32000" in result.error


def test_settings_are_validated(tmp_path, make_backend):
    backend = make_backend("normal")
    adapter = TesseractAdapter()
    good = settings_for(backend)
    adapter.validate_settings(good)
    adapter.validate_settings({k: v for k, v in good.items() if k != "poll_seconds"})

    bad: dict[str, dict[str, Any]] = {
        "no url": {k: v for k, v in good.items() if k != "url"},
        "remote host": {**good, "url": "http://example.org:8000"},
        "url with a path": {**good, "url": backend.url + "/api"},
        "no home": {k: v for k, v in good.items() if k != "home"},
        "home is not a folder": {**good, "home": str(tmp_path / "missing")},
        "capital model": {**good, "model": "Sonnet"},
        "no budget": {k: v for k, v in good.items() if k != "max_budget_usd"},
        "zero budget": {**good, "max_budget_usd": 0},
        "boolean budget": {**good, "max_budget_usd": True},
        "text budget": {**good, "max_budget_usd": "1"},
        "negative quiet": {**good, "quiet_seconds": -1},
        "no policy": {k: v for k, v in good.items() if k != "tool_ask_policy"},
        "unknown policy": {**good, "tool_ask_policy": "maybe"},
        "zero poll": {**good, "poll_seconds": 0},
        "absolute cost log": {**good, "cost_log": str(tmp_path / "cost.jsonl")},
        "cost log outside home": {**good, "cost_log": "../cost.jsonl"},
        "misspelt setting": {**good, "max_budget": 1.0},
    }
    accepted = []
    for name, settings in bad.items():
        try:
            adapter.validate_settings(settings)
        except ValueError:
            continue
        accepted.append(name)
    assert accepted == [], f"these settings were accepted: {accepted}"


def test_the_cost_log_reader_waits_for_a_finished_line_and_survives_replacement(tmp_path):
    path = tmp_path / "cost.jsonl"
    reader = CostLog(path, CHAT)
    assert reader.poll() == 0.0, "a log that does not exist yet has no spend"

    first = json.dumps({"chat_id": CHAT, "cost_usd": 0.25, "phase": ""})
    second = json.dumps({"chat_id": CHAT, "cost_usd": 0.5, "phase": ""})
    path.write_text(first + "\n" + second[:10], encoding="utf-8")
    assert reader.poll() == pytest.approx(0.25)

    path.write_text(first + "\n" + second + "\n", encoding="utf-8")
    assert reader.poll() == pytest.approx(0.75)

    path.write_text(second + "\n", encoding="utf-8")
    assert reader.poll() == pytest.approx(0.5), "a replaced log is read again from its start"

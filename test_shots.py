"""Check the demo recording helpers without a browser, ffmpeg or listening sockets."""
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def shots(monkeypatch):
    api = ModuleType("playwright.sync_api")
    api.sync_playwright = MagicMock()
    monkeypatch.setitem(sys.modules, "playwright.sync_api", api)
    spec = importlib.util.spec_from_file_location("shots", Path(__file__).parent / "docs/shots.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod.time, "sleep", lambda seconds: None)
    return mod


def test_demo_turn(shots, tmp_path):
    db = str(tmp_path / "state.db")
    shots.seed(db)
    stream = io.BytesIO()
    shots.play_turn(stream, db, shots.ASK)
    events = []
    for block in stream.getvalue().decode().strip().split("\n\n"):
        name, data = block.split("\n")
        events.append((name.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    names = [name for name, data in events]
    assert names[0] == "run.started"
    assert names[-3:] == ["assistant.completed", "run.completed", "done"]
    assert names.index("tool.started") < names.index("tool.completed") < names.index("assistant.delta")
    assert all(data["run_id"] == "demo" for name, data in events)
    assert "".join(data["delta"] for name, data in events if name == "tool.progress").strip() == shots.THINK
    assert "".join(data["delta"] for name, data in events if name == "assistant.delta") == shots.ANSWER
    with sqlite3.connect(db) as con:
        rows = con.execute("select role, content, tool_calls, tool_name, reasoning from messages "
                           "where session_id='main' order by id").fetchall()
    assert len(rows) == 8  # the seeded exchange plus one new turn
    assert rows[-4] == ("user", shots.ORIGIN + shots.ASK, None, None, None)
    assert rows[-3] == ("assistant", "", shots.call("terminal", {"command": shots.VERIFY}), None, shots.THINK)
    assert rows[-2] == ("tool", json.dumps({"output": shots.VERIFIED, "exit_code": 0}), None, "terminal", None)
    assert rows[-1] == ("assistant", shots.ANSWER, None, None, None)


@pytest.mark.parametrize("db,path,message,streamed", [
    (None, "/chat/stream", "hello", False),
    ("state.db", "/other", "hello", False),
    ("state.db", "/api/chat/stream", "hello", True),
    ("state.db", "/chat/stream", None, True),
])
def test_stub_gateway(shots, monkeypatch, db, path, message, streamed):
    server = MagicMock(server_address=("127.0.0.1", 12345))
    factory = MagicMock(return_value=server)
    monkeypatch.setattr(shots.http.server, "ThreadingHTTPServer", factory)
    monkeypatch.setattr(shots.threading, "Thread", MagicMock())
    play = MagicMock()
    monkeypatch.setattr(shots, "play_turn", play)
    assert shots.stub_gateway(db) == "http://127.0.0.1:12345"
    cls = factory.call_args.args[1]
    request = cls.__new__(cls)
    body = json.dumps({"message": message}).encode()
    request.path = path
    request.headers = {"Content-Length": str(len(body))}
    request.rfile = io.BytesIO(body)
    request.wfile = io.BytesIO()
    request.send_response = MagicMock()
    request.send_header = MagicMock()
    request.end_headers = MagicMock()
    request.do_POST()
    request.send_response.assert_called_once_with(200)
    if streamed:
        request.send_header.assert_called_once_with("Content-Type", "text/event-stream")
        play.assert_called_once_with(request.wfile, db, message or "")
    else:
        play.assert_not_called()
        assert json.loads(request.wfile.getvalue()) == {"status": "ok", "data": []}


def test_gif_recording(shots, tmp_path, monkeypatch):
    browser = shots.sync_playwright.return_value.__enter__.return_value.chromium.launch.return_value
    context = browser.new_context.return_value
    page = context.new_page.return_value
    video = tmp_path / "vid"
    video.mkdir()
    (video / "take.webm").touch()
    monkeypatch.setattr(shots, "HERE", str(tmp_path))
    encode = MagicMock(side_effect=lambda *args, **kwargs: (tmp_path / "demo.gif").write_bytes(b"GIF89a"))
    monkeypatch.setattr(shots.subprocess, "run", encode)
    shots.gif(12345, str(tmp_path))
    browser.new_context.assert_called_once_with(viewport={"width": 960, "height": 720},
                                               record_video_dir=str(video),
                                               record_video_size={"width": 960, "height": 720})
    page.keyboard.type.assert_called_once_with(shots.ASK, delay=45)
    assert [call.args for call in page.keyboard.press.call_args_list] == [("Enter",), ("b",)]
    context.close.assert_called_once()
    browser.close.assert_called_once()
    command = encode.call_args.args[0]
    assert command[0] == "ffmpeg" and command[-1] == str(tmp_path / "demo.gif")
    assert command[command.index("-i") + 1] == str(video / "take.webm")
    assert "palettegen=max_colors=32" in command[command.index("-vf") + 1]
    assert encode.call_args.kwargs == {"check": True}
    assert "store1" in (tmp_path / "memories/MEMORY.md").read_text()

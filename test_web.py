"""Header contracts without a browser or listening sockets; rendered fit is checked by the UI test."""
import importlib.util
import json
from pathlib import Path
import re
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent


def test_served_mark_is_product_name(monkeypatch):
    spec = importlib.util.spec_from_file_location("oko_tail_web", ROOT / "oko-tail.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    agent = "example-desktop-agent-with-a-long-name"
    monkeypatch.setattr(server, "AGENT", agent)
    monkeypatch.setattr(server, "main_session", lambda db: "s")
    monkeypatch.setattr(server, "sessions_list", lambda db: [])
    page = server.page(SimpleNamespace(db="unused", interval=1), "s")
    assert f"<title>{agent} · oko's tail</title>" in page
    cfg = json.loads(re.search(r'<script id=cfg[^>]*>(.*?)</script>', page, re.S).group(1))
    assert cfg["agent"] == agent
    # main now builds the header in the shared view, not in index.html.
    view = (ROOT / "web/view.js").read_text()
    mark = re.search(r"const mark=.*?;", view).group()
    assert "fox,'oko-tail')" in mark
    assert "placeholder:'Message '+AG+" in view


def test_desktop_mark_never_wraps():
    css = (ROOT / "web/app.css").read_text()
    mark = re.search(r'^#mark\{([^}]+)\}', css, re.M).group(1)
    assert "white-space:nowrap" in mark
    # A fixed gutter width cannot hold the fox, gap and product label.
    assert "width:max-content" in mark and "flex:0 0 auto" in mark

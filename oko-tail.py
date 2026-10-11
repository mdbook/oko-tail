#!/usr/bin/env python3
"""oko's tail: watch a hermes agent's turns live, and talk to it. MIT licensed.

hermes flushes each tool step to state.db as it happens (agent/tool_executor.py
_flush_session_db_after_tool_progress), so polling the session's newest rows shows the
turn step by step: reasoning, tool calls + args, tool results, final text. Opens the DB
?mode=ro; the transcript itself is never written by us. Step granularity from the DB; a turn
sent from this page also streams tokens (the gateway's SSE, held server-side as the LIVE overlay).

Reads are read-only. The compose box is the one write path: it fires a turn through the
gateway's own session API (POST /api/sessions/{id}/chat/stream), which is how hermes starts a turn
for any client; we never write the DB. That API needs API_SERVER_KEY even on loopback, so we
read the key already in $HERMES_HOME/.env (0600, the same user hermes runs as) rather than hold a copy;
$API_SERVER_KEY in our environment overrides it.
The gateway treats "/x" as literal text. When the agent's plugin socket ($HERMES_HOME/oko-tail.sock, same uid, one JSON
line per request) is up, every slash command is sent through it as the owner's own DM and hermes runs it natively;
otherwise (socket missing, or a session other than the main one) a few are mapped to the gateway's REST calls (/cmd).

  oko-tail.py                 # follow the default session in the terminal (starts at the last user turn)
  oko-tail.py --serve 9120    # same, as a web page: http://127.0.0.1:9120
  oko-tail.py --selftest

The page opens at the last user turn; "load earlier steps" backfills from the log above it, and
?session=<id> focuses one session (the picker in the header switches; default: see OKO_TAIL_MAIN_*, '*' = all).
Sending needs a concrete session, so the compose box appears only when one is focused.
Endpoints: / (page), /app.css, /app.js, /rows?after=N&session=ID, /earlier?before=N&session=ID,
           /events (SSE; same payload as /rows: rows, turn state, live, gateway, stats), /cmds (slash menu), /queue (the agent's own queue),
           /ctx?session=ID (what fills the context), /search?q= (newest 50 matches across every session, with where it matched),
           /browse (memory, profile, staged writes, skills, cron: read off $HERMES_HOME), /skill?name=X (one SKILL.md),
           POST /say {session,message} -> 202, turn streams in the background (payload.live),
           POST /stop {session}, POST /steer {session,message} (only turns sent from this page: we hold their run_id),
           POST /cmd {session,name,args} (any command via the socket; REST fallback: title|status|model|fork|retry|stop|steer|skill).
"""
import argparse
import base64
import hmac
import html
import ipaddress
import json
import re
import os
import socket
import sqlite3
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

# Config is env-only (a systemd drop-in or your shell); every default is a stock single-profile hermes.
HOME = os.path.expanduser(os.environ.get("HERMES_HOME") or "~/.hermes")
DB = os.path.expanduser(os.environ.get("OKO_TAIL_DB") or os.path.join(HOME, "state.db"))
ENV = os.path.expanduser(os.environ.get("OKO_TAIL_ENV") or os.path.join(HOME, ".env"))  # API_SERVER_KEY (0600); never logged
# fallback skill list when the gateway is down (SKILL.md frontmatter)
SKILLS = os.path.expanduser(os.environ.get("OKO_TAIL_SKILLS") or os.path.join(HOME, "skills"))
SOCK = os.path.expanduser(os.environ.get("OKO_TAIL_SOCKET", ""))  # command socket, if not $HERMES_HOME/oko-tail.sock
GATEWAY = os.environ.get("HERMES_GATEWAY") or "http://127.0.0.1:8642"  # hermes gateway session API (loopback)
AGENT = os.environ.get("OKO_TAIL_AGENT") or "hermes"  # what the page calls the agent
# The default session: wherever the gateway routes this key (e.g. your own DM), else MAIN, else the newest.
MAIN_KEY = os.environ.get("OKO_TAIL_MAIN_ROUTE", "")
MAIN = os.environ.get("OKO_TAIL_MAIN_SESSION", "")
# Other agents' tails (one oko-tail per agent), listed under the session switcher: "name=url,name=url".
# A url is http(s)://... or a path on this host (/other/); anything else is dropped.
def parse_peers(s):
    return [{"name": n.strip(), "url": u.strip()} for n, _, u in (p.partition("=") for p in s.split(","))
            if n.strip() and re.fullmatch(r"(https?://[^\s\"'<>\\]+|/(?![/\\])[^\s\"'<>\\]*)", u.strip())]  # no //host, no /\host


PEERS = parse_peers(os.environ.get("OKO_TAIL_PEERS", ""))
# "user_id=Label": show that sender as Label; anyone else is shown by the local part of their id.
OWNER = tuple(os.environ.get("OKO_TAIL_OWNER", "").partition("=")[::2])
# Auth for --serve, off by default (then it binds loopback only). Either one lets a request in:
#   basic:   OKO_TAIL_USER + OKO_TAIL_PASSWORD (or OKO_TAIL_PASSWORD_FILE, read once at start)
#   forward: OKO_TAIL_AUTH_HEADER, a header your auth proxy sets (X-Authentik-Username, Remote-User, ...),
#            trusted only from OKO_TAIL_TRUSTED_PROXIES (default loopback). The proxy must drop client copies.
#   OKO_TAIL_HOST: the bind address. Anything but loopback refuses to start without one of the two.
HOST = os.environ.get("OKO_TAIL_HOST") or "127.0.0.1"
# OKO_TAIL_HOSTNAMES: the names browsers may reach us by (comma list; your proxy's public name). Loopback and
# HOST are always allowed; any other Host header gets 421, so a DNS-rebound page can't talk to us. "*" = any.
HOSTNAMES = os.environ.get("OKO_TAIL_HOSTNAMES", "")
SID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")     # a session id we're willing to put in a URL path
COLS = ("m.id, m.role, m.timestamp, m.content, m.tool_calls, m.tool_name, m.reasoning, m.session_id, s.source, "
        "m._compressed_summary, m.display_kind, m.api_content")
FROM = "messages m left join sessions s on s.id = m.session_id"

# hermes writes bookkeeping rows alongside real turns; label them rather than passing them off as oko prose.
# (value, css class, label). '' and unknown kinds keep the generic rendering.
KINDS = {"failed_turn": ("bad", "turn failed"),
         "hidden": ("gone", "interrupted"),
         "internal_notification": ("note", "system note")}
# Dropped in SQL, not in Python: a row filtered out after the query would stall the SSE cursor (it advances on
# the last row of the page). These are session-start markers with no content, so nothing to render either way.
SKIP = "and (m.display_kind is null or m.display_kind<>'session_meta')"


def rows_after(db, session, after, limit=200):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        if after is None:  # start at the most recent user message
            r = con.execute("select max(id) from messages where (?1='*' or session_id=?1) and role='user' and active=1",
                            (session,)).fetchone()
            after = (r[0] or 1) - 1
        return con.execute(f"select {COLS} from {FROM} where (?1='*' or m.session_id=?1) and m.id>?2 and m.active=1 "
                           f"{SKIP} order by m.id limit ?3", (session, after, limit)).fetchall()
    finally:
        con.close()


def last_row(db, session):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        return con.execute(f"select {COLS} from {FROM} where (?1='*' or m.session_id=?1) and m.active=1 {SKIP} "
                           "order by m.id desc limit 1", (session,)).fetchone()
    finally:
        con.close()


def rows_before(db, session, before, limit=100):
    """The page before `before`, oldest-last so the caller can prepend. Walks back a page at a time."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        rs = con.execute(f"select {COLS} from {FROM} where (?1='*' or m.session_id=?1) and m.id<?2 and m.active=1 "
                         f"{SKIP} order by m.id desc limit ?3", (session, before, limit)).fetchall()
        return rs[::-1]
    finally:
        con.close()


def sessions_list(db):
    """Sessions newest-first for the picker; only those with renderable rows."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        return [{"id": r[0], "source": r[1], "title": r[2], "last": r[3]} for r in con.execute(
            "select s.id, s.source, s.title, coalesce(s.last_activity_at, s.started_at) from sessions s "
            "where exists(select 1 from messages m where m.session_id=s.id and m.active=1 and "
            "coalesce(m.display_kind,'')<>'session_meta') order by 4 desc limit 30")]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def session_title(db, session):
    """One session's title (the picker label); None for the '*' view."""
    if session == "*":
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        r = con.execute("select title from sessions where id=?", (session,)).fetchone()
        return r[0] if r else None
    except sqlite3.Error:
        return None
    finally:
        con.close()


def clip(s, width):
    return s if width <= 0 or len(s) <= width else s[:width] + f" …(+{len(s) - width})"


def calls_of(tool_calls):
    try:
        cs = json.loads(tool_calls) if tool_calls else []
    except ValueError:
        return []
    return [c for c in cs if isinstance(c, dict)] if isinstance(cs, list) else []


def render(row, width):
    """(id, header, [body lines]) for one message row (terminal follow mode)."""
    mid, role, ts, content, tool_calls, tool_name, reasoning = row[:7]
    kind = row[10] if len(row) > 10 else None
    head = f"{time.strftime('%H:%M:%S', time.localtime(ts))} #{mid} {role}" + (f" [{tool_name}]" if tool_name else "")
    body = []
    if len(row) > 9 and row[9]:  # _compressed_summary: the compaction marker, not a real turn
        return mid, head + " [compacted]", ["⤓ context compacted: earlier turns summarized away"]
    if kind in KINDS:
        label = KINDS[kind][1]
        return mid, head + f" [{label}]", [f"{label}: {clip(content, width)}" if content else label]
    if reasoning:
        body.append("💭 " + clip(reasoning, width))
    for c in calls_of(tool_calls):
        f = c.get("function") or {}
        body.append(f"🔧 {f.get('name')}({clip(f.get('arguments') or '', width)})")
    if content:
        body.append(clip(content, width))
    return mid, head, body


def parse_json(s):
    """Tool args/results are usually JSON text; None when they aren't."""
    if isinstance(s, (dict, list)):
        return s
    try:
        return json.loads(s) if s else None
    except (ValueError, TypeError):
        return None


def clip_tree(v, width, items=50):
    """Clip every string inside parsed JSON (clipping the raw text first would break the parse)."""
    if isinstance(v, str):
        return clip(v, width)
    if isinstance(v, list):  # long lists cut to `items` entries + a marker string
        return [clip_tree(x, width) for x in v[:items]] + ([f"…(+{len(v) - items} more)"] if len(v) > items else [])
    if isinstance(v, dict):
        return {str(k): clip_tree(x, width) for k, x in v.items()}
    return v


ORIGIN = re.compile(r"\AGateway message origin \([^)]*\):\n(\{.*?\})\n(?:Do not guess[^\n]*\n)?\n*", re.S)


def split_origin(content):
    """hermes prefixes inbound platform messages with an origin JSON preamble -> (sender, bare text)."""
    m = ORIGIN.match(content or "")
    if not m:
        return None, content
    o = parse_json(m.group(1)) or {}
    uid = str(o.get("user_id") or "")
    who = OWNER[1] if OWNER[0] and uid == OWNER[0] else uid.split("@")[0] or None
    return who, content[m.end():]


def row_json(row, width):
    """Structured row for the web page; the browser escapes everything (textContent).
    Tool args become `params` (dict) and JSON tool output becomes `result`; raw text stays only as the fallback."""
    mid, role, ts, content, tool_calls, tool_name, reasoning, sid, source, *rest = row
    compressed = rest[0] if rest else 0
    kind = rest[1] if len(rest) > 1 else None
    calls = []
    for c in calls_of(tool_calls):
        f = c.get("function") or {}
        raw = f.get("arguments") or ""
        p = parse_json(raw)
        if not isinstance(raw, str):
            raw = json.dumps(raw)
        calls.append({"name": f.get("name"), "params": clip_tree(p, width) if isinstance(p, dict) else None,
                      "args": None if isinstance(p, dict) else clip(raw, width)})
    sender = None
    if role == "user":
        sender, content = split_origin(content)
    result = parse_json(content) if role == "tool" else None
    if not isinstance(result, (dict, list)):
        result = None
    return {"id": mid, "role": role, "ts": ts, "session": sid, "source": source, "sender": sender, "tool_name": tool_name,
            "reasoning": clip(reasoning, width) if reasoning else None, "tool_calls": calls,
            "result": clip_tree(result, width) if result is not None else None,
            "compacted": bool(compressed), "kind": kind or None,
            "content": clip(content, width) if content and result is None else None}


STALE = 1800  # a turn with no new row for 30 min counts as dead, not working (hermes crash mid-turn)


def turn_state(db, session, now=None):
    """None when idle, else {phase, since_ts}. A turn is in flight when the newest row is a user message
    (model running), a tool result (model reading it) or an assistant tool call (tool running)."""
    last = last_row(db, session)
    if not last or (now or time.time()) - last[2] > STALE:
        return None
    mid, role, ts, sid = last[0], last[1], last[2], last[7]
    calls = [(c.get("function") or {}).get("name") or "tool" for c in calls_of(last[4])] if role == "assistant" else []
    if role in ("user", "tool"):
        phase = "model"
    elif calls:
        phase = "tool " + ", ".join(calls)
    else:
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        start = con.execute("select max(timestamp) from messages where session_id=? and role='user' and active=1 and id<=?",
                            (sid, mid)).fetchone()[0]
    finally:
        con.close()
    return {"phase": phase, "since_ts": start or ts}


def compression_lock(db, session, now=None):
    """True while hermes holds a live compaction lease for `session` (compression_locks row, unexpired). That
    lease is the only in-progress signal: the rows a compaction writes only land when it finishes."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        r = con.execute("select 1 from compression_locks where (?1='*' or session_id=?1) and expires_at >= ?2 limit 1",
                        (session, now or time.time())).fetchone()
        return bool(r)
    except sqlite3.Error:
        return False  # a DB predating compaction has no such table; absence = not compacting
    finally:
        con.close()


def payload(args, after, session=None, now=None):
    """Rows after `after` (<0 = start at last user turn) plus the turn state; shared by /rows and /events."""
    session = session or args.session or main_session(args.db)
    rows = rows_after(args.db, session, None if after < 0 else after)
    now = now or time.time()
    st = turn_state(args.db, session, now)
    return {"rows": [row_json(x, args.width) for x in rows],
            "session": session, "title": session_title(args.db, session),
            "waiting": bool(st), "phase": st and st["phase"], "since_ts": st and st["since_ts"],
            "since": time.strftime("%H:%M:%S", time.localtime(st["since_ts"])) if st else None,
            "compacting": compression_lock(args.db, session, now),
            "live": live_get(session) if session != "*" else None,
            "gateway": gateway_up(getattr(args, "gateway", GATEWAY)),
            "stats": session_stats(args.db, session)}


STAT_COLS = ("model", "input_tokens", "output_tokens", "cache_read_tokens", "reasoning_tokens", "estimated_cost_usd",
             "actual_cost_usd", "message_count", "tool_call_count", "api_call_count", "started_at", "last_activity_at",
             "title", "source")
_STATS = {}


def session_stats(db, session):
    """Usage counters from the sessions row (None for '*' or an unknown id). Only columns this hermes has are
    selected, the rest come back None; cached 5 s since every SSE push asks."""
    if session == "*":
        return None
    hit = _STATS.get((db, session))
    if hit and time.monotonic() - hit[0] < 5:
        return hit[1]
    out = None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        have = {r[1] for r in con.execute("pragma table_info(sessions)")}
        cols = [c for c in STAT_COLS if c in have]
        r = con.execute(f"select {', '.join(cols)} from sessions where id=?", (session,)).fetchone() if cols else None
        if r:
            out = dict.fromkeys(STAT_COLS)
            out.update(zip(cols, r))
    except sqlite3.Error:
        out = None
    finally:
        con.close()
    _STATS[(db, session)] = (time.monotonic(), out)
    return out


def db_stamp(db):
    """Cheap change detector: (mtime_ns, size) of state.db and its -wal (WAL writes only touch the -wal)."""
    out = []
    for p in (db, db + "-wal"):
        try:
            st = os.stat(p)
            out.append((st.st_mtime_ns, st.st_size))
        except OSError:
            out.append(None)
    return tuple(out)


def db_check(db):
    """Can we read a hermes state.db here? -> (ok, note). What --check reports and startup refuses on."""
    if not os.path.exists(db):
        return False, "no file here (set OKO_TAIL_DB, HERMES_HOME or --db)"
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            n = con.execute("select count(*) from sessions").fetchone()[0]
            con.execute("select 1 from messages limit 1").fetchall()
        finally:
            con.close()
    except sqlite3.Error as e:
        return False, f"not a readable hermes state.db ({e})"
    return True, f"{n} sessions"


def check(args):
    """--check: each setting, where it came from, and whether it works. Never prints the key. -> exit code."""
    def src(val, default, *envs):
        return "flag" if val != default else env(*envs)

    def env(*envs):
        return next((f"${e}" for e in envs if os.environ.get(e)), "default")
    ok, note = db_check(args.db)
    key = _api_key(args.env)
    up = gateway_up(args.gateway, fresh=True)
    sock = next((p for p in sock_paths(HOME) if os.path.exists(p)), "")
    keysrc = "$API_SERVER_KEY" if os.environ.get("API_SERVER_KEY", "").strip() else args.env
    try:
        auth, autherr = auth_config(), ""
    except (OSError, ValueError) as e:
        auth, autherr = None, str(e)
    open_ = auth is not None and not (auth["user"] or auth["header"])
    authenv = env("OKO_TAIL_USER", "OKO_TAIL_AUTH_HEADER")
    rows = [
        ("ok" if ok else "FAIL", "db", args.db, src(args.db, DB, "OKO_TAIL_DB", "HERMES_HOME"), note),
        ("ok" if key else "warn", "api key", "set" if key else "missing", keysrc,
         "" if key else "the page is read-only: no sending, no commands"),
        ("ok" if up else "warn", "gateway", args.gateway, src(args.gateway, GATEWAY, "HERMES_GATEWAY"),
         "up" if up else "unreachable: the page shows the agent offline"),
        ("ok" if os.path.isdir(SKILLS) else "warn", "skills", SKILLS, env("OKO_TAIL_SKILLS", "HERMES_HOME"),
         "" if os.path.isdir(SKILLS) else "no dir: the / menu lists skills only while the gateway is up"),
        ("ok" if sock else "warn", "socket", sock or sock_paths(HOME)[-1], env("OKO_TAIL_SOCKET", "HERMES_HOME"),
         "" if sock else "absent: only a few commands work, over the gateway's REST API"),
        ("ok", "agent", AGENT, env("OKO_TAIL_AGENT"), ""),
        ("ok", "session", args.session or (ok and main_session(args.db)) or "-",
         src(args.session, None, "OKO_TAIL_MAIN_ROUTE", "OKO_TAIL_MAIN_SESSION"), "opens on this one"),
        ("FAIL" if autherr else "ok", "auth", autherr and "invalid" or auth_mode(auth), authenv,
         autherr or ("loopback only: put an authenticating proxy in front to go off this box" if open_ else "")),
        ("ok", "hosts", ", ".join(sorted(host_names())), env("OKO_TAIL_HOSTNAMES"),
         "" if HOSTNAMES else "loopback only: a proxy's public name gets 421 until you add it"),
        ("FAIL" if open_ and not is_loopback(HOST) else "ok", "listen", HOST, env("OKO_TAIL_HOST"),
         "off-box with no login: --serve refuses to start" if open_ and not is_loopback(HOST) else ""),
    ]
    for r in rows:
        print("{:<5} {:<8} {}  [{}]{}".format(*r[:4], "  " + r[4] if r[4] else ""))
    return 0 if ok and not any(r[0] == "FAIL" for r in rows) else 1


def follow(args):
    after = None
    session = args.session or main_session(args.db)
    while True:
        rows = rows_after(args.db, session, after)
        for row in rows:
            after, head, body = render(row, args.width)
            print(head)
            for line in body:
                print("   " + line.replace("\n", "\n   "))
            sys.stdout.flush()
        time.sleep(args.interval)


# Design: a transcript, not a chat app. A narrow gutter carries time and speaker; a session-tinted rule
# runs down it so interleaved sessions stay apart. oko's work reads as a terminal session: each tool call
# opens a recessed well (`$ command` for terminal, name + key/value args for the rest) and its result
# lands inside the same well (output text, then exit/duration/cwd), so call and output read as one unit.
# Thinking is small dim prose, replies are full-size prose. While a turn is in flight oko's tail is the
# activity indicator (it sways while thinking, wags on tools, curls while compacting). No external assets;
# system fonts; all DB data via textContent.
WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
STATIC = {"/app.css": "text/css; charset=utf-8", "/app.js": "text/javascript; charset=utf-8", "/favicon.svg": "image/svg+xml",
          "/logo.svg": "image/svg+xml"}  # fixed allowlist, no path joins from the URL


def web(name):
    """A file from web/, read per request so a page edit is live without a restart."""
    with open(os.path.join(WEB, name), encoding="utf-8") as f:
        return f.read()


def page(args, sess):
    """index.html with the boot config inlined (JSON in a <script>: every '<' escaped, so no title can close
    the tag or open '<!--<script' and swallow the rest of the page)."""
    cfg = json.dumps({"session": sess, "main": main_session(args.db), "agent": AGENT, "ms": int(args.interval * 1000),
                      "sessions": sessions_list(args.db), "peers": PEERS})
    return web("index.html").replace("__AGENT__", html.escape(AGENT)).replace("__CFG__", cfg.replace("<", "\\u003c"))


def _api_key(env_path=ENV):
    """API_SERVER_KEY for the gateway. Read in-process, returned to the caller only: never logged,
    never echoed, never put in a response; it only travels to the gateway.
    $API_SERVER_KEY in our own environment wins; otherwise the line in the env file."""
    if os.environ.get("API_SERVER_KEY", "").strip():
        return os.environ["API_SERVER_KEY"].strip()
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("API_SERVER_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""


def main_session(db):
    """The default session: wherever the gateway routes MAIN_KEY right now (a /new moves the route to a
    child, so the page and that chat always talk to the same conversation), else MAIN, else the newest."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        r = MAIN_KEY and con.execute("select json_extract(entry_json,'$.session_id') from gateway_routing where "
                                     "session_key=? order by updated_at desc limit 1", (MAIN_KEY,)).fetchone()
        if r and r[0] and SID.fullmatch(r[0]):
            return r[0]
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return MAIN or next((s["id"] for s in sessions_list(db)), "*")


def known_session(db, sid):
    """True if `sid` is an existing session with something to show. The send target must be one, of any age
    (the picker's list stops at 30)."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        return con.execute("select 1 from sessions s where s.id=? and exists(select 1 from messages m where "
                           "m.session_id=s.id and m.active=1 and coalesce(m.display_kind,'')<>'session_meta')",
                           (sid,)).fetchone() is not None
    except sqlite3.Error:
        return False
    finally:
        con.close()


OFFLINE = f"{AGENT} is offline (gateway unreachable)"
NOKEY = {"error": "API_SERVER_KEY not available to the service"}


def gw(method, path, body=None, env_path=ENV, gateway=GATEWAY, timeout=30):
    """One authenticated JSON call to the gateway -> (status, json). 0 = unreachable. The key only ever goes
    into this request's header."""
    key = _api_key(env_path)
    if not key:
        return 503, NOKEY
    req = urllib.request.Request(gateway + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw) if raw.strip() else {}
            except ValueError:
                return r.status, {"error": "gateway sent non-JSON"}
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {"error": f"gateway error {e.code}"}
    except (urllib.error.URLError, OSError):
        return 0, {"error": OFFLINE}


_UP = {}


def gateway_up(gateway=GATEWAY, fresh=False):
    """GET /health (no auth), cached 10 s: every SSE push reports it so the page can say oko is offline."""
    hit = _UP.get(gateway)
    if hit and not fresh and time.monotonic() - hit[0] < 10:
        return hit[1]
    try:
        with urllib.request.urlopen(gateway + "/health", timeout=1.5) as r:
            ok = 200 <= r.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        ok = False
    _UP[gateway] = (time.monotonic(), ok)
    return ok


# Live turns. A turn sent from this page runs through the gateway's SSE stream, consumed by a server thread
# (NOT the browser: the gateway interrupts a turn whose stream client goes away, so a reload must not kill it).
# LIVE[session] is the in-flight overlay the page draws between DB step writes: tokens, thinking, current tool,
# and the run_id that /stop and /steer need. The DB stays the transcript of record.
LIVE, LIVE_LOCK, LIVE_VER = {}, threading.Lock(), [0]  # LIVE_VER bumps on every change; /events watches it
LIVE_KEEP = 120      # a finished run lingers this long so the page sees how it ended
THINK_CAP = 4000
TERMINAL = ("done", "failed", "cancelled")


def live_get(session):
    """A copy of `session`'s live run, or None; drops a finished run once it's LIVE_KEEP old."""
    with LIVE_LOCK:
        r = LIVE.get(session)
        if r and r["ended"] and time.time() - r["ended"] > LIVE_KEEP:
            del LIVE[session]
            LIVE_VER[0] += 1
            r = None
        return dict(r) if r else None


def sse_frames(lines):
    """(event, data-dict) per SSE frame from an iterable of lines; comment lines (': keepalive') skipped."""
    ev, data = None, []
    for raw in lines:
        line = (raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw).rstrip("\r\n")
        if not line:
            if data:
                try:
                    d = json.loads("\n".join(data))
                except ValueError:
                    d = {"raw": "\n".join(data)}
                yield ev or "message", d if isinstance(d, dict) else {"value": d}
            ev, data = None, []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            ev = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))


def live_event(run, ev, d, now=None):
    """Fold one gateway stream event into `run` (api_server.py _handle_session_chat_stream's event set).
    run["stage"] says what the model is doing right now: wait (nothing back yet: queueing, prefill, or reasoning
    hermes doesn't stream), think (reasoning text arrived), write (reply tokens), tool; stage_ts = when it began."""
    stage = {"assistant.delta": "write", "assistant.commentary": "write", "tool.started": "tool",
             "tool.completed": "wait", "tool.failed": "wait"}.get(ev)
    if ev == "tool.progress" and d.get("tool_name") == "_thinking":
        stage = "think"
    if stage and run.get("stage") != stage:
        run["stage"], run["stage_ts"] = stage, now or time.time()
    if d.get("run_id") and not run["run_id"]:
        run["run_id"] = str(d["run_id"])
    if ev in ("run.started", "message.started") and run["status"] == "starting":
        run["status"] = "running"
    if ev == "message.started":
        run["text"] = ""                   # a new assistant message: the overlay shows the current one only
    elif ev == "assistant.delta":
        run["text"] += d.get("delta") or ""
    elif ev == "assistant.commentary" and not d.get("already_streamed"):
        run["text"] = d.get("text") or run["text"]
    elif ev == "tool.progress" and d.get("tool_name") == "_thinking":
        run["thinking"] = (run["thinking"] + (d.get("delta") or ""))[-THINK_CAP:]
    elif ev == "tool.started":
        run["tool"], run["tools"] = d.get("tool_name") or "tool", run["tools"] + 1
        run["text"] = run["thinking"] = ""  # what came before the call is in the DB with it: don't show it twice
    elif ev in ("tool.completed", "tool.failed"):
        run["tool"] = None
    elif ev == "assistant.completed":
        if d.get("content"):
            run["text"] = d["content"]
        if d.get("interrupted"):
            run["status"] = "cancelled"
    elif ev == "run.completed":
        run["usage"] = d.get("usage")
        if run["status"] != "cancelled":
            run["status"] = "done"
    elif ev == "run.cancelled":
        run["status"] = "cancelled"
    elif ev in ("run.failed", "error"):
        run["status"] = "failed"
        e = d.get("error") or d.get("message")
        run["error"] = (e.get("message") if isinstance(e, dict) else e) or "turn failed"
    if run["status"] in TERMINAL:
        run["tool"] = None
        run["ended"] = run["ended"] or now or time.time()


def stream_turn(run, session, message, env_path, gateway):
    """Thread body: hold the gateway stream for the whole turn, folding events into `run` (shared with LIVE)."""
    err = None
    try:
        req = urllib.request.Request(f"{gateway}/api/sessions/{session}/chat/stream", method="POST",
                                     data=json.dumps({"message": message}).encode(),
                                     headers={"Authorization": f"Bearer {_api_key(env_path)}",
                                              "Content-Type": "application/json", "Accept": "text/event-stream"})
        with urllib.request.urlopen(req, timeout=3600) as r:  # read timeout; the gateway keepalives every 10 s
            for ev, d in sse_frames(r):
                with LIVE_LOCK:
                    live_event(run, ev, d)
                    LIVE_VER[0] += 1
                if ev == "done":
                    break
    except urllib.error.HTTPError as e:
        try:
            j = json.loads(e.read())
            msg = j.get("error") or j.get("message")
            err = f"gateway said {e.code}: " + (msg.get("message") if isinstance(msg, dict) else str(msg or ""))
        except Exception:
            err = f"gateway said {e.code}"
    except (urllib.error.URLError, OSError) as e:
        err = OFFLINE if isinstance(e, urllib.error.URLError) else "stream broke"
    finally:
        with LIVE_LOCK:
            if run["status"] not in TERMINAL:
                run["status"], run["error"] = "failed", err or "stream ended"
            run["ended"] = run["ended"] or time.time()
            run["tool"] = None
            LIVE_VER[0] += 1


MAX_IMAGES = 4
IMAGE = re.compile(r"data:image/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/]{4,5600000}={0,2}")  # ~4 MB each


def turn_content(message, images):
    """The chat body's `message`: plain text, or OpenAI-style parts when images ride along (hermes
    api_server _normalize_multimodal_content takes text + image_url data URLs)."""
    if not images:
        return message
    return ([{"type": "text", "text": message}] if message.strip() else []) + \
        [{"type": "image_url", "image_url": {"url": u}} for u in images]


def start_turn(db, session, message, env_path=ENV, gateway=GATEWAY, images=()):
    """Fire one turn in `session` and return at once (202); the turn streams into LIVE[session].
    The id must be a session we already show, so a typo never runs a turn in a fresh session."""
    if not known_session(db, session):
        return 400, {"error": "unknown session"}
    if not _api_key(env_path):
        return 503, NOKEY
    if not gateway_up(gateway, fresh=True):
        return 502, {"error": OFFLINE}
    now = time.time()
    run = {"run_id": None, "status": "starting", "started": now, "message": message, "text": "", "thinking": "",
           "tool": None, "tools": 0, "error": None, "ended": None, "usage": None, "stage": "wait", "stage_ts": now}
    with LIVE_LOCK:
        LIVE[session] = run
        LIVE_VER[0] += 1
    threading.Thread(target=stream_turn, args=(run, session, turn_content(message, images), env_path, gateway),
                     daemon=True).start()
    return 202, {"ok": True}


def run_action(session, verb, message=None, env_path=ENV, gateway=GATEWAY):
    """stop/steer the turn this page started (we hold its run_id; hermes has no stop-by-session route)."""
    r = live_get(session)
    if not r or not r["run_id"] or r["status"] not in ("starting", "running"):
        return 409, {"error": "nothing to stop: only turns sent from this page can be stopped" if verb == "stop"
                     else "nothing to steer: only a running turn sent from this page can be steered"}
    st, out = gw("POST", f"/v1/runs/{quote(r['run_id'], safe='')}/{verb}",
                 {} if verb == "stop" else {"message": message}, env_path, gateway)
    return st or 502, out


# Slash commands. The gateway session API passes "/x" to the model as text, so the ones a web client can
# honor map to its REST routes here. `help` is drawn by the page itself; skills are listed separately.
COMMANDS = [
    {"name": "stop", "args": "", "desc": "Interrupt the turn you sent from this page"},
    {"name": "steer", "args": "<message>", "desc": "Nudge the running turn without stopping it"},
    {"name": "retry", "args": "", "desc": "Send your last message again"},
    {"name": "status", "args": "", "desc": "Show this session's model, tokens and cost"},
    {"name": "model", "args": "[model]", "desc": "List models, or switch this session to one"},
    {"name": "title", "args": "<name>", "desc": "Rename this session"},
    {"name": "fork", "args": "[title]", "desc": "Branch this session into a new one"},
    {"name": "skill", "args": "<name> [request]", "desc": f"Ask {AGENT} to use one of its skills"},
    {"name": "help", "args": "", "desc": "List commands"},
]
CMD_NAMES = {c["name"] for c in COMMANDS} - {"help"}
_SKILLS = {}


def skills_from_dir(root):
    """name/description from each SKILL.md frontmatter under `root` (read-only fallback for a down gateway)."""
    out = []
    for d, _, files in os.walk(root):
        if "SKILL.md" not in files:
            continue
        meta = {}
        try:
            with open(os.path.join(d, "SKILL.md"), encoding="utf-8") as f:
                if f.readline().strip() == "---":
                    for line in f:
                        if line.strip() == "---":
                            break
                        k, _, v = line.partition(":")
                        if k.strip() in ("name", "description"):
                            meta[k.strip()] = v.strip().strip('"').strip("'")
        except OSError:
            continue
        if meta.get("name"):
            out.append({"name": meta["name"], "desc": meta.get("description", ""), "dir": d})
    return out


def skills_list(env_path=ENV, gateway=GATEWAY, root=SKILLS):
    """oko's skills for the slash menu: the gateway's /v1/skills, else the skills dir; cached 300 s."""
    hit = _SKILLS.get((gateway, root))
    if hit and time.monotonic() - hit[0] < 300:
        return hit[1]
    st, j = gw("GET", "/v1/skills", None, env_path, gateway, timeout=5)
    if st == 200 and isinstance(j.get("data"), list):
        out = [{"name": s["name"], "desc": s.get("description") or ""} for s in j["data"]
               if isinstance(s, dict) and isinstance(s.get("name"), str)]
    else:
        out = [{"name": s["name"], "desc": s["desc"]} for s in skills_from_dir(root)]  # never a path in the menu
    out = sorted({s["name"]: s for s in out}.values(), key=lambda s: s["name"])
    _SKILLS[(gateway, root)] = (time.monotonic(), out)
    return out


def _read(path, cap=200_000):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(cap)
    except OSError:
        return None


def browse(home=HOME, root=SKILLS):
    """What the agent keeps between turns, read straight off its files (read-only): memory notes, the user profile,
    writes staged for approval, skills, cron jobs. Every value is plain text for the page to render as text."""
    mem = {k: [e.strip() for e in (_read(os.path.join(home, "memories", f)) or "").split("\n§\n") if e.strip()]
           for k, f in (("memory", "MEMORY.md"), ("user", "USER.md"))}
    pend = []
    for sub in ("memory", "skills"):
        d = os.path.join(home, "pending", sub)
        for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            if not f.endswith(".json"):
                continue
            try:
                j = json.loads(_read(os.path.join(d, f)) or "")
            except (ValueError, RecursionError):
                continue
            if isinstance(j, dict) and re.fullmatch(r"[0-9a-f]{4,32}", str(j.get("id", ""))):
                pend.append({"id": j["id"], "subsystem": sub, "action": str(j.get("action") or "")[:200],
                             "summary": str(j.get("summary") or "")[:2000], "origin": str(j.get("origin") or "")[:200],
                             "ts": j["created_at"] if isinstance(j.get("created_at"), (int, float)) else None,
                             "detail": json.dumps(j.get("payload"), indent=1, ensure_ascii=False)[:8000]})
    skills = []
    for s in skills_from_dir(root):
        d = s.pop("dir")   # pop first: {**s, ...: s.pop()} unpacks s before the pop runs
        skills.append({**s, "category": os.path.dirname(os.path.relpath(d, root))})
    try:
        jobs = json.loads(_read(os.path.join(home, "cron", "jobs.json")) or "{}").get("jobs") or []
    except (ValueError, AttributeError):
        jobs = []
    keep = ("id", "name", "prompt", "schedule_display", "enabled", "state", "next_run_at", "last_run_at",
            "last_status", "last_error")
    val = lambda v: v if v is None or isinstance(v, (bool, int, float)) else str(v)[:4000]  # text or a number, nothing nested
    cron = [{k: val(j.get(k)) for k in keep} for j in jobs if isinstance(j, dict)]
    return {**mem, "pending": sorted(pend, key=lambda p: p["ts"] or 0),
            "skills": sorted(skills, key=lambda s: (s["category"], s["name"])), "cron": cron}


def skill_text(name, root=SKILLS):
    """One skill's SKILL.md by its name (looked up, never a path from the request) -> text, or None."""
    top = os.path.realpath(root) + os.sep
    for s in skills_from_dir(root):
        p = os.path.realpath(os.path.join(s["dir"], "SKILL.md"))
        if s["name"] == name and p.startswith(top):     # a symlinked SKILL.md never reads outside the skills dir
            return _read(p)
    return None


def last_user_text(db, session):
    """The session's most recent real user message (origin preamble stripped), which is what /retry resends."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        r = con.execute("select content from messages where session_id=? and role='user' and active=1 and "
                        "coalesce(display_kind,'') not in ('internal_notification','session_meta') "
                        "order by id desc limit 1", (session,)).fetchone()
    finally:
        con.close()
    return split_origin(r[0])[1] if r and r[0] else None


def context_items(db, session, top=8):
    """What fills the live context: the session's active rows (compaction deactivates the folded ones and the summary
    row comes first). Sizes are chars/4, a rough token estimate; the agent's own /context has the exact totals."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        rs = con.execute("select id, role, timestamp, coalesce(nullif(api_content, ''), content), tool_calls, tool_name, "
                         f"reasoning, _compressed_summary from messages m where session_id=? and active=1 {SKIP} "
                         "order by id", (session,)).fetchall()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    parts, items = dict.fromkeys(("summary", "you", "replies", "tool calls", "tool output", "reasoning"), 0), []
    for rid, role, ts, content, calls, tool, reasoning, summary in rs:
        text, call = len(content or "") // 4, len(calls or "") // 4
        kind = "summary" if summary else "you" if role == "user" else "tool output" if role == "tool" else "replies"
        parts[kind] += text
        parts["tool calls"] += call
        parts["reasoning"] += len(reasoning or "") // 4
        names = [str((c.get("function") or {}).get("name") or c.get("name") or "") for c in calls_of(calls)]
        items.append({"id": rid, "role": role, "kind": kind, "ts": ts, "tokens": text + call,
                      "tool": tool or ", ".join(n for n in names if n) or None,
                      "preview": " ".join((content or "").split())[:140]})
    items.sort(key=lambda i: -i["tokens"])
    return {"rows": len(rs), "parts": parts, "items": items[:top]}


def tool_failed(content):
    """A tool result that reports failure: an error field, a non-zero exit code or success false (else not failed)."""
    try:
        r = json.loads(content or "")
    except ValueError:
        return False
    if not isinstance(r, dict):
        return False
    return bool(r.get("error")) or r.get("exit_code") not in (None, 0) or r.get("success") is False


def tool_health(db, session, days=7):
    """Per tool over the session's last `days`: calls, failures, median and slowest time (call row -> result row)."""
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        rs = con.execute("select role, timestamp, tool_calls, tool_call_id, tool_name, content from messages "
                         "where session_id=? and timestamp>=? and role in ('assistant', 'tool') order by id",
                         (session, time.time() - days * 86400)).fetchall()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    called, stats = {}, {}
    for role, ts, calls, call_id, tool, content in rs:
        if role == "assistant":
            for c in calls_of(calls):
                cid = c.get("call_id") or c.get("id")
                if cid:
                    called[cid] = (str((c.get("function") or {}).get("name") or c.get("name") or "tool"), ts)
        elif call_id in called:
            name, t0 = called.pop(call_id)
            s = stats.setdefault(tool or name, {"n": 0, "failed": 0, "secs": []})
            s["n"] += 1
            s["failed"] += tool_failed(content)
            s["secs"].append(max(0.0, ts - t0))
    out = []
    for name, s in stats.items():
        secs = sorted(s["secs"])
        out.append({"tool": name, "n": s["n"], "failed": s["failed"], "median": secs[len(secs) // 2],
                    "max": secs[-1], "total": sum(secs)})
    out.sort(key=lambda t: -t["total"])
    return {"days": days, "tools": out}


def search_all(db, q, limit=50):
    """Newest-first matches for `q` across every session: what was said, thought or run (LIKE, case-insensitive
    for ASCII). Each hit carries a snippet around the first match so the page can show it without the row."""
    q = q.strip()[:200]
    if len(q) < 2:
        return []
    like = "%" + re.sub(r"([\\%_])", r"\\\1", q) + "%"
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return []
    try:
        rs = con.execute("select m.id, m.session_id, s.title, s.source, m.role, m.timestamp, m.tool_name, "
                         "coalesce(m.content,''), coalesce(m.reasoning,''), coalesce(m.tool_calls,'') from " + FROM +
                         " where m.active=1 and m._compressed_summary=0 and coalesce(m.display_kind,'')<>'session_meta'"
                         " and (m.content like ?1 escape '\\' or m.reasoning like ?1 escape '\\'"
                         " or m.tool_calls like ?1 escape '\\') order by m.id desc limit ?2", (like, limit)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    out, low = [], q.lower()
    for mid, sid, title, source, role, ts, tool, content, reasoning, calls in rs:
        k = next((k for k, t in enumerate((content, reasoning, calls)) if low in t.lower()), 0)
        text = (content, reasoning, calls)[k]
        i = max(0, text.lower().find(low))
        a = max(0, i - 60)
        snip = ("…" if a else "") + text[a:i + len(q) + 100].replace("\n", " ") + ("…" if i + len(q) + 100 < len(text) else "")
        out.append({"id": mid, "session": sid, "title": title, "source": source, "role": role, "ts": ts,
                    "tool": tool, "where": ("text", "thinking", "calls")[k], "snippet": snip})
    return out


def run_cmd(args, session, name, cargs, root=SKILLS):
    """One slash command -> (status, json). `cargs` is the text after the command name."""
    if name not in CMD_NAMES:
        return 400, {"error": f"unknown command: /{name}" if isinstance(name, str) else "unknown command"}
    a = cargs.strip() if isinstance(cargs, str) else ""
    env, g = args.env, args.gateway
    if name == "title":
        if not a:
            return 400, {"error": "usage: /title <name>"}
        st, out = gw("PATCH", f"/api/sessions/{session}", {"title": a}, env, g)
    elif name == "status":
        st, out = gw("GET", f"/api/sessions/{session}", None, env, g)
    elif name == "model":
        st, out = gw("POST", f"/api/sessions/{session}/model", {"model": a}, env, g) if a else \
            gw("GET", "/api/model/options", None, env, g)
    elif name == "fork":
        st, out = gw("POST", f"/api/sessions/{session}/fork", {"title": a} if a else {}, env, g)
    elif name == "retry":
        text = last_user_text(args.db, session)
        if not text:
            return 409, {"error": "nothing to retry: no earlier message in this session"}
        return start_turn(args.db, session, text, env, g)
    elif name in ("stop", "steer"):
        if name == "steer" and not a:
            return 400, {"error": "usage: /steer <message>"}
        return run_action(session, name, a, env, g)
    else:  # skill
        sk, _, rest = a.partition(" ")
        if not sk:
            return 400, {"error": "usage: /skill <name> [request]"}
        if sk not in {s["name"] for s in skills_list(env, g, root)}:
            return 400, {"error": f"{AGENT} has no skill called {sk}"}
        return start_turn(args.db, session, f"Use your `{sk}` skill." + (f"\n\n{rest.strip()}" if rest.strip() else ""), env, g)
    return (st if 200 <= st < 300 else (st or 502)), out


# The plugin socket: one request per connection, one JSON line each way (see "The command socket" in the README).
# It always acts on the agent's one main session, so only that session's commands go through it.
CMD_TIMEOUT = 90                                 # a slow command (/compress) gets this long, then we hand back what we have
CMDNAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
NOSOCK = "commands need the oko-tail socket (agent offline?)"


class SockDown(Exception):
    """No socket to talk to: file missing, refused, or gone mid-send."""


def sock_paths(home):
    """Where the socket may be: the real path the plugin wrote when the default was too long, then the default."""
    base = os.path.join(home, "oko-tail.sock")
    try:
        with open(base + ".path", encoding="utf-8") as f:
            ptr = f.read().strip()
    except OSError:
        ptr = ""
    return [p for p in (SOCK, ptr, base) if p]


def sock_call(req, timeout=CMD_TIMEOUT, home=HOME):
    """Send one request, read reply lines until `done` or `timeout` seconds -> [dict]. A reply that ends without a
    `done` (or times out) gets one added, reason 'timeout'/'closed'. Raises SockDown when nothing is listening."""
    s = None
    for p in sock_paths(home):
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(5)
        try:
            c.connect(p)
            # only talk to a server running as us: a pointer into /tmp could be squatted after a cleanup
            if struct.unpack("3i", c.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1] != os.getuid():
                raise OSError("socket peer is another uid")
            s = c
            break
        except OSError:
            c.close()
    if s is None:
        raise SockDown()
    out, buf, end = [], b"", time.monotonic() + timeout
    try:
        try:
            s.sendall(json.dumps(req).encode() + b"\n")
        except OSError:
            raise SockDown()
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return out + [{"type": "done", "reason": "timeout"}]
            s.settimeout(left)
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                continue                          # the deadline check above decides
            except OSError:
                chunk = b""
            if not chunk:
                return out + [{"type": "done", "reason": "closed"}]
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if isinstance(d, dict):           # unknown line types ride along; callers ignore what they don't know
                    out.append(d)
                    if d.get("type") == "done":
                        return out
    finally:
        s.close()


def socket_commands(home=HOME):
    """The agent's real command list in the page's shape, or None when the socket is down / answers nothing usable."""
    try:
        lines = sock_call({"op": "commands"}, 5, home)
    except SockDown:
        return None
    for d in lines:
        if d.get("type") == "commands" and isinstance(d.get("commands"), list):
            return [{"name": c["name"], "args": str(c.get("args_hint") or ""), "desc": str(c.get("description") or ""),
                     "aliases": [a for a in (c.get("aliases") or []) if isinstance(a, str)],
                     "category": str(c.get("category") or "")}
                    for c in d["commands"] if isinstance(c, dict) and isinstance(c.get("name"), str)
                    and CMDNAME.fullmatch(c["name"])]
    return None


def socket_queue(home=HOME):
    """What waits to run in the main session (hermes' queue + DMs held for idle; none of it is in state.db yet)
    -> list in run order, or None when the socket is down or doesn't know the op (an older plugin)."""
    try:
        lines = sock_call({"op": "queue"}, 3, home)
    except SockDown:
        return None
    for d in lines:
        if d.get("type") == "queue" and isinstance(d.get("items"), list):
            return [{"where": str(i.get("where") or ""), "id": str(i.get("id") or ""), "page": bool(i.get("page")),
                     "from": str(i.get("from") or ""), "peer": bool(i.get("peer")), "text": str(i.get("text") or "")[:2000],
                     "images": i.get("images") if isinstance(i.get("images"), int) else 0,
                     "ts": i.get("ts") if isinstance(i.get("ts"), (int, float)) else None}
                    for i in d["items"] if isinstance(i, dict)]
    return None


def socket_cmd(text, home=HOME):
    """Run one slash command through the socket -> (status, json), or None when the socket is down (caller falls back)."""
    try:
        lines = sock_call({"op": "say", "text": text}, CMD_TIMEOUT, home)
    except SockDown:
        return None
    msgs, held, accepted, err, reason = [], False, False, None, None
    for d in lines:
        t = d.get("type")
        if t == "accepted":
            accepted = True
        elif t == "held":
            held = True
        elif t == "message" and isinstance(d.get("text"), str) and d["text"].strip():
            msgs.append(d["text"])
        elif t == "error":
            err = str(d.get("error") or "the agent refused that")
        elif t == "done":
            reason = d.get("reason")
    if reason == "rejected" and not err:
        err = "the agent refused that"
    out = {"messages": msgs, "held": held, "turn": accepted and not msgs, "done": reason}
    return (502, {**out, "error": err}) if err else (200, out)


POSTS = ("/say", "/stop", "/steer", "/cmd")


def same_origin(origin, host):
    """CSRF gate for every POST: each one makes oko act, so a cross-site page must not be able to fire it
    through the user's authenticated browser. An auth proxy fronts any public host; this is the gate behind it
    (loopback directly, no Origin, is allowed). HOST only: a public page is https behind the proxy, we serve http.
    "null" (a sandboxed iframe, a data: URL) is cross-origin: with basic auth the browser would resend the login."""
    if not origin:
        return True
    h = (urlparse(origin).netloc or "").lower()
    return bool(h) and h == (host or "").lower()


def auth_config(env=None):
    """The --serve auth settings from the environment -> dict. Raises ValueError/OSError on a half-set config."""
    env = os.environ if env is None else env
    user, pw = env.get("OKO_TAIL_USER", ""), env.get("OKO_TAIL_PASSWORD", "")
    if env.get("OKO_TAIL_PASSWORD_FILE"):
        with open(os.path.expanduser(env["OKO_TAIL_PASSWORD_FILE"]), encoding="utf-8") as f:
            pw = f.read().rstrip("\r\n")
    if bool(user) != bool(pw):
        raise ValueError("set both OKO_TAIL_USER and OKO_TAIL_PASSWORD (or OKO_TAIL_PASSWORD_FILE), or neither")
    if ":" in user:
        raise ValueError("OKO_TAIL_USER can't contain ':'")
    header = env.get("OKO_TAIL_AUTH_HEADER", "").strip()
    if header and not re.fullmatch(r"[A-Za-z0-9-]{1,64}", header):
        raise ValueError("OKO_TAIL_AUTH_HEADER must be a header name, e.g. X-Authentik-Username")
    nets = [ipaddress.ip_network(n.strip(), strict=False)
            for n in (env.get("OKO_TAIL_TRUSTED_PROXIES") or "127.0.0.0/8,::1").split(",") if n.strip()]
    if header and not nets:
        raise ValueError("OKO_TAIL_TRUSTED_PROXIES lists no addresses, so the header could never be trusted")
    if any(getattr(n, "network_address", None) and getattr(n.network_address, "ipv4_mapped", None) for n in nets):
        raise ValueError("write IPv4 proxies as plain IPv4 (127.0.0.1), not ::ffff:127.0.0.1; peers are matched that way")
    return {"user": user, "password": pw, "header": header, "proxies": nets}


def auth_mode(auth):
    """A one-line description for --check and the startup line. Never the password."""
    modes = ([f"basic (user {auth['user']})"] if auth["user"] else []) + \
            ([f"forward ({auth['header']} from {', '.join(map(str, auth['proxies']))})"] if auth["header"] else [])
    return " or ".join(modes) or "none"


def is_loopback(host):
    try:
        return host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def authorized(auth, headers, peer):
    """May this request in? headers: case-insensitive .get (the request's headers); peer: the client's IP."""
    if not auth["user"] and not auth["header"]:
        return True
    if auth["header"] and (headers.get(auth["header"]) or "").strip():
        try:
            ip = ipaddress.ip_address(peer.split("%")[0])
            ip = getattr(ip, "ipv4_mapped", None) or ip       # ::ffff:127.0.0.1 on a dual-stack bind
        except ValueError:
            ip = None
        if ip and any(ip.version == n.version and ip in n for n in auth["proxies"]):
            return True
    if auth["user"]:
        kind, _, cred = (headers.get("Authorization") or "").partition(" ")
        if kind.lower() != "basic":
            return False
        try:
            u, sep, p = base64.b64decode(cred.strip(), validate=True).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        ok_u = hmac.compare_digest(u.encode(), auth["user"].encode())     # both compared, always: no early out
        ok_p = hmac.compare_digest(p.encode(), auth["password"].encode())
        return bool(sep) and ok_u and ok_p
    return False


def host_names(raw=HOSTNAMES, bind=HOST):
    """OKO_TAIL_HOSTNAMES -> the set of allowed Host names (no ports); containing "*" allows any."""
    names = {n.strip().lower().strip("[]") for n in raw.split(",") if n.strip()}
    return names | {"localhost", "127.0.0.1", "::1", bind.lower().strip("[]")}


def host_ok(names, host):
    """DNS-rebinding gate: is this Host header one of ours? A rebound page carries its own name, not ours."""
    if "*" in names:
        return True
    h = (host or "").strip().lower()
    h = h[1:].partition("]")[0] if h.startswith("[") else h.rpartition(":")[0] if h.count(":") == 1 else h
    return bool(h) and (h in names or is_loopback(h))


def handle_post(args, path, origin, host, raw, root=SKILLS):
    """Every POST endpoint -> (status, json). Pure of the HTTP plumbing so the selftest drives it directly."""
    path = urlparse(path).path
    if path not in POSTS:
        return 404, {"error": "not found"}
    if not same_origin(origin, host):
        return 403, {"error": "cross-origin blocked"}
    try:
        body = json.loads(raw or b"{}")
    except (ValueError, TypeError):
        return 400, {"error": "bad json"}
    if not isinstance(body, dict):
        return 400, {"error": "bad json"}
    sess, msg = body.get("session"), body.get("message")
    if not isinstance(sess, str) or not SID.fullmatch(sess):
        return 400, {"error": "bad session"}
    if not known_session(args.db, sess):
        return 400, {"error": "unknown session"}
    imgs = (body.get("images") or []) if path == "/say" else []
    if not isinstance(imgs, list) or len(imgs) > MAX_IMAGES or not all(isinstance(u, str) and IMAGE.fullmatch(u) for u in imgs):
        return 400, {"error": f"images must be up to {MAX_IMAGES} png/jpeg/gif/webp data URLs"}
    if path in ("/say", "/steer") and (not isinstance(msg, str) or not (msg.strip() or imgs)):
        return 400, {"error": "empty message"}
    if path == "/say":
        return start_turn(args.db, sess, msg, args.env, args.gateway, imgs)
    if path == "/stop":
        return run_action(sess, "stop", None, args.env, args.gateway)
    if path == "/steer":
        return run_action(sess, "steer", msg, args.env, args.gateway)
    name, cargs = body.get("name"), body.get("args")
    if isinstance(name, str) and name != "skill" and CMDNAME.fullmatch(name):   # skill is the page's own affordance (a turn), never a slash
        a = cargs.strip() if isinstance(cargs, str) else ""
        if len(a) > 8000:
            return 400, {"error": "arguments too long"}
        if sess == main_session(args.db):
            r = socket_cmd("/" + name + (" " + a if a else ""), getattr(args, "home", HOME))
            if r:
                return r
            if name not in CMD_NAMES:
                return 503, {"error": NOSOCK}
        elif name not in CMD_NAMES:
            return 400, {"error": f"/{name} only runs in {AGENT}'s main session; switch to it"}
    return run_cmd(args, sess, name, cargs, root)


def serve(args):
    try:
        auth = auth_config()
    except (OSError, ValueError) as e:
        sys.exit(f"oko-tail: auth: {e}")
    if not is_loopback(HOST) and not (auth["user"] or auth["header"]):
        sys.exit(f"oko-tail: OKO_TAIL_HOST={HOST} is reachable from other machines and there's no login. "
                 "Set OKO_TAIL_USER + OKO_TAIL_PASSWORD or OKO_TAIL_AUTH_HEADER first (README, Security).")
    # Plain http. Basic auth off-box wants TLS in front (a proxy, Tailscale); no built-in TLS.
    srv = ThreadingHTTPServer
    if ":" in HOST:
        class srv(ThreadingHTTPServer):
            address_family = socket.AF_INET6
    print(f"oko-tail: http://{HOST}:{args.serve}  auth: {auth_mode(auth)}  hosts: {', '.join(sorted(host_names()))}",
          file=sys.stderr)
    srv((HOST, args.serve), handler(args, auth)).serve_forever()


def handler(args, auth, hosts=None):
    """The page's request handler class, bound to args, the auth settings and the allowed Host names."""
    hosts = host_names() if hosts is None else hosts

    class H(BaseHTTPRequestHandler):
        def gate(self):
            """Every request, static files and /events included, passes the Host check and auth first.
            -> False when refused."""
            if not host_ok(hosts, self.headers.get("Host")):
                self.send_data(b"unknown host name: add it to OKO_TAIL_HOSTNAMES\n", "text/plain; charset=utf-8", 421)
                return False
            if authorized(auth, self.headers, self.client_address[0]):
                return True
            self.send_response(401)
            if auth["user"]:
                self.send_header("WWW-Authenticate", 'Basic realm="oko-tail", charset="UTF-8"')
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(b"login required\n")
            except OSError:
                pass
            return False

        def do_GET(self):
            if not self.gate():
                return
            q = parse_qs(urlparse(self.path).query)
            sess = q.get("session", [""])[0] or args.session or main_session(args.db)
            if sess != "*" and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", sess):  # never interpolate a raw query value
                sess = args.session or main_session(args.db)
            try:
                after = int(q.get("after", ["-1"])[0])
            except ValueError:
                after = -1
            if self.path.startswith("/events"):
                return self.events(after, sess)
            if self.path.startswith("/earlier"):
                try:
                    before = int(q["before"][0])
                except (KeyError, ValueError):
                    before = 1 << 62
                try:
                    limit = min(200, max(1, int(q.get("limit", ["100"])[0])))
                except ValueError:
                    limit = 100
                rs = rows_before(args.db, sess, before, limit)
                data = json.dumps({"rows": [row_json(r, args.width) for r in rs],
                                   "oldest": rs[0][0] if rs else None, "more": len(rs) == limit}).encode()
                return self.send_data(data, "application/json")
            if self.path.startswith("/rows"):
                data, ctype = json.dumps(payload(args, after, sess)).encode(), "application/json"
            elif urlparse(self.path).path == "/ctx":
                data, ctype = json.dumps(context_items(args.db, sess) if sess != "*" else None).encode(), "application/json"
            elif urlparse(self.path).path == "/tools":
                data, ctype = json.dumps(tool_health(args.db, sess) if sess != "*" else None).encode(), "application/json"
            elif urlparse(self.path).path == "/search":
                data, ctype = json.dumps({"hits": search_all(args.db, q.get("q", [""])[0])}).encode(), "application/json"
            elif urlparse(self.path).path == "/browse":
                data, ctype = json.dumps(browse(getattr(args, "home", HOME), getattr(args, "skills", SKILLS))).encode(), "application/json"
            elif urlparse(self.path).path == "/skill":
                t = skill_text(q.get("name", [""])[0], getattr(args, "skills", SKILLS))
                data, ctype = json.dumps({"text": t} if t is not None else {"error": "no such skill"}).encode(), "application/json"
            elif urlparse(self.path).path == "/queue":
                items = socket_queue(getattr(args, "home", HOME)) if sess == main_session(args.db) else None
                data, ctype = json.dumps({"items": items or [], "socket": items is not None}).encode(), "application/json"
            elif urlparse(self.path).path == "/cmds":
                cmds = socket_commands(getattr(args, "home", HOME)) if sess == main_session(args.db) else None
                data = json.dumps({"commands": cmds or COMMANDS, "socket": bool(cmds),
                                   "skills": skills_list(args.env, args.gateway),
                                   "gateway": gateway_up(args.gateway)}).encode()
                ctype = "application/json"
            elif urlparse(self.path).path in STATIC:
                data, ctype = web(urlparse(self.path).path[1:]).encode(), STATIC[urlparse(self.path).path]
            else:
                data, ctype = page(args, sess).encode(), "text/html; charset=utf-8"
            self.send_data(data, ctype)

        def end_headers(self):
            self.send_header("X-Frame-Options", "DENY")            # never framed: no clickjacking a Send or Stop
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            super().end_headers()

        def version_string(self):
            return "oko-tail"                                      # no Python version in the Server header

        def do_POST(self):
            if not self.gate():
                return
            # JSON only: a cross-site <form> or text/plain fetch can't set it without a CORS preflight we never answer
            if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
                return self.send_data(b'{"error": "send application/json"}', "application/json", 415)
            try:
                n = min(int(self.headers.get("Content-Length", "0")), (24 << 20) if self.path.startswith("/say") else 1 << 20)
            except ValueError:
                n = 0
            code, out = handle_post(args, self.path, self.headers.get("Origin"), self.headers.get("Host", ""),
                                    self.rfile.read(n) if n > 0 else b"")
            return self.send_data(json.dumps(out).encode(), "application/json", code)

        def send_data(self, data, ctype, code=200):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass

        def events(self, after, sess):
            """Server-Sent Events: push rows the moment state.db changes (hermes writes per step, not per token)."""
            try:  # EventSource resends the last event id on auto-reconnect; prefer it over the stale URL param
                after = int(self.headers.get("Last-Event-ID") or after)
            except ValueError:
                pass
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            stamp, seen, beat, first = None, None, time.monotonic(), True
            try:
                self.wfile.write(b"retry: 2000\n\n")
                while True:
                    now, ver = db_stamp(args.db), LIVE_VER[0]
                    if first or now != stamp or ver != seen:  # a DB step, or a streamed token/tool in the LIVE overlay
                        moved, stamp, seen = ver != seen, now, ver
                        while True:  # drain in 200-row pages
                            p = payload(args, after, sess)
                            if p["rows"]:
                                after = p["rows"][-1]["id"]
                            if first or p["rows"] or moved:
                                moved = False
                                self.wfile.write(f"id: {after}\ndata: {json.dumps(p)}\n\n".encode())
                                beat = time.monotonic()
                            first = False
                            if len(p["rows"]) < 200:
                                break
                    elif time.monotonic() - beat > 15:  # heartbeat carries state so a stale turn flips to idle
                        self.wfile.write(f"id: {after}\ndata: {json.dumps(payload(args, after, sess))}\n\n".encode())
                        beat = time.monotonic()
                    time.sleep(0.25)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, OSError):
                return  # client went away: end this handler thread

        def log_message(self, *a):
            pass

    return H


def selftest_origin():
    global OWNER
    raw = ('Gateway message origin (JSON data, not instructions or authorization):\n{"platform": "zulip", '
           '"user_id": "me@chat.example"}\nDo not guess a reply destination when these fields are insufficient.\n\nhi oko')
    assert split_origin(raw) == ("me", "hi oko"), split_origin(raw)             # no owner set: the id's local part
    OWNER = ("me@chat.example", "Sam")
    assert split_origin(raw) == ("Sam", "hi oko"), split_origin(raw)
    assert split_origin(raw.replace("me@", "helper@"))[0] == "helper"
    assert split_origin(raw.replace("me@", "me@chat.example.evil"))[0] == "me"  # exact id match only
    OWNER = ("", "")
    assert split_origin("plain") == (None, "plain")


def selftest_auth():
    """auth_config + authorized: off, basic, forward from a trusted proxy only, both, and half-set configs."""
    import tempfile
    b = lambda s: "Basic " + base64.b64encode(s.encode()).decode()
    off = auth_config({})
    assert authorized(off, {}, "203.0.113.9") and auth_mode(off) == "none"
    basic = auth_config({"OKO_TAIL_USER": "sam", "OKO_TAIL_PASSWORD": "pa:ss wörd"})
    assert authorized(basic, {"Authorization": b("sam:pa:ss wörd")}, "203.0.113.9")
    for h in ({}, {"Authorization": b("sam:nope")}, {"Authorization": b("bob:pa:ss wörd")}, {"Authorization": b("sam")},
              {"Authorization": "Basic !!!"}, {"Authorization": "Bearer x"}, {"X-Authentik-Username": "sam"}):
        assert not authorized(basic, h, "127.0.0.1"), h
    assert "pa:ss" not in auth_mode(basic)
    fwd = auth_config({"OKO_TAIL_AUTH_HEADER": "X-Authentik-Username"})
    assert authorized(fwd, {"X-Authentik-Username": "sam"}, "127.0.0.1")
    assert authorized(fwd, {"X-Authentik-Username": "sam"}, "::ffff:127.0.0.1") and authorized(fwd, {"X-Authentik-Username": "sam"}, "::1")
    assert not authorized(fwd, {"X-Authentik-Username": "sam"}, "192.168.1.5")      # not a trusted proxy: a forged header
    assert not authorized(fwd, {"X-Authentik-Username": " "}, "127.0.0.1") and not authorized(fwd, {}, "127.0.0.1")
    lan = auth_config({"OKO_TAIL_AUTH_HEADER": "Remote-User", "OKO_TAIL_TRUSTED_PROXIES": "172.16.0.0/24, fd00::/8",
                       "OKO_TAIL_USER": "sam", "OKO_TAIL_PASSWORD": "pw"})
    assert authorized(lan, {"Remote-User": "a"}, "172.16.0.7") and not authorized(lan, {"Remote-User": "a"}, "127.0.0.1")
    assert authorized(lan, {"Authorization": b("sam:pw")}, "198.51.100.1")              # either one lets you in
    pf = os.path.join(tempfile.mkdtemp(), "pw")
    with open(pf, "w") as f:
        f.write("from-a-file\n")
    assert auth_config({"OKO_TAIL_USER": "sam", "OKO_TAIL_PASSWORD_FILE": pf})["password"] == "from-a-file"
    for bad in ({"OKO_TAIL_USER": "sam"}, {"OKO_TAIL_PASSWORD": "pw"}, {"OKO_TAIL_USER": "a:b", "OKO_TAIL_PASSWORD": "pw"},
                {"OKO_TAIL_AUTH_HEADER": "X-Bad Header"}, {"OKO_TAIL_AUTH_HEADER": "X", "OKO_TAIL_TRUSTED_PROXIES": "nope"},
                {"OKO_TAIL_AUTH_HEADER": "X", "OKO_TAIL_TRUSTED_PROXIES": " , "},
                {"OKO_TAIL_AUTH_HEADER": "X", "OKO_TAIL_TRUSTED_PROXIES": "::ffff:127.0.0.1"}):
        try:
            auth_config(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    assert is_loopback("127.0.0.1") and is_loopback("::1") and is_loopback("localhost")
    assert not is_loopback("0.0.0.0") and not is_loopback("::") and not is_loopback("tail.example.com")
    names = host_names("Tail.Example.com, ", "127.0.0.1")
    for h in ("tail.example.com", "TAIL.example.com:443", "127.0.0.1:9120", "localhost:9120", "[::1]:9120", "::1"):
        assert host_ok(names, h), h
    for h in ("evil.example", "evil.example:9120", "tail.example.com.evil.example", "", None, ":9120"):
        assert not host_ok(names, h), h                                     # a DNS-rebound page carries its own name
    assert not host_ok(host_names("", "127.0.0.1"), "tail.example.com")     # default: loopback names only
    assert host_ok(host_names("*", "127.0.0.1"), "anything.example") and host_ok(host_names("", "172.16.0.5"), "172.16.0.5:9120")


def selftest_http(A):
    """The real handler over real HTTP with auth on: every path (page, static, JSON, SSE, POST) is gated."""
    auth = auth_config({"OKO_TAIL_USER": "u", "OKO_TAIL_PASSWORD": "selftest-only", "OKO_TAIL_AUTH_HEADER": "Remote-User"})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler(argparse.Namespace(**{**vars(A), "interval": 2.0}), auth))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def hit(path, headers=None, data=None):
        req = urllib.request.Request(base + path, data=data, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.headers, r.read(40)
        except urllib.error.HTTPError as e:
            return e.code, e.headers, b""
    good = {"Authorization": "Basic " + base64.b64encode(b"u:selftest-only").decode()}
    try:
        for path in ("/", "/app.js", "/rows?session=s", "/ctx?session=s", "/cmds", "/events?session=s"):
            code, h, _ = hit(path)
            assert code == 401 and h.get("WWW-Authenticate", "").startswith("Basic "), (path, code)
        assert hit("/say", data=b'{"session":"s","message":"hi"}')[0] == 401
        # logged in, but cross-site shapes: text/plain (no preflight) and Origin: null (sandboxed iframe)
        say = b'{"session":"nope","message":"hi"}'
        assert hit("/say", {**good, "Content-Type": "text/plain"}, say)[0] == 415
        assert hit("/say", {**good, "Content-Type": "application/json", "Origin": "null"}, say)[0] == 403
        assert hit("/say", {**good, "Content-Type": "application/json; charset=utf-8"}, say)[0] == 400   # past both gates
        hd = hit("/", good)[1]
        assert (hd["X-Frame-Options"], hd["X-Content-Type-Options"], hd["Server"]) == ("DENY", "nosniff", "oko-tail"), hd
        assert hit("/rows?session=s", {**good, "Host": "rebound.example:9120"})[0] == 421     # before auth, too
        assert hit("/say", {**good, "Host": "rebound.example", "Content-Type": "application/json"}, say)[0] == 421
        assert hit("/rows?session=s", {"Authorization": "Basic " + base64.b64encode(b"u:wrong").decode()})[0] == 401
        assert hit("/rows?session=s", good)[0] == 200 and hit("/", good)[0] == 200
        assert hit("/rows?session=s", {"Remote-User": "me"})[0] == 200          # loopback is a trusted proxy
        code, h, body = hit("/events?session=s", good)
        assert code == 200 and h["Content-Type"] == "text/event-stream" and body.startswith(b"retry:"), body
    finally:
        srv.shutdown()


def selftest():
    global AGENT, MAIN, MAIN_KEY
    selftest_origin()
    selftest_auth()
    import tempfile
    db = os.path.join(tempfile.mkdtemp(), "s.db")
    con = sqlite3.connect(db)
    con.execute("create table messages (id integer primary key, session_id, role, timestamp, content, "
                "tool_calls, tool_name, reasoning, active default 1, _compressed_summary default 0, "
                "display_kind, api_content)")
    con.execute("create table compression_locks (session_id, holder, acquired_at, expires_at)")
    tc = json.dumps([{"function": {"name": "terminal", "arguments": '{"cmd":"ls"}'}}])
    con.executemany("insert into messages (session_id, role, timestamp, content, tool_calls, tool_name, reasoning) "
                    "values (?,?,?,?,?,?,?)",
                    [("s", "user", 1, "old", None, None, None), ("s", "assistant", 2, "a", None, None, None),
                     ("s", "user", 3, "hi", None, None, None), ("x", "user", 3, "other", None, None, None),
                     ("s", "assistant", 4, "", tc, None, "think"), ("s", "tool", 5, "out", None, "terminal", None)])
    con.execute("create table sessions (id primary key, source, title, started_at, last_activity_at)")
    con.executemany("insert into sessions values (?,?,?,?,?)", [("s", "zulip", "A session", 0, 5), ("x", "tui", None, 0, 1)])
    con.commit()
    stamp0 = db_stamp(db)
    con.execute("insert into messages (session_id, role, timestamp, content) values ('s','assistant',6,'late')")
    con.commit()
    assert db_stamp(db) != stamp0 and db_stamp(db) == db_stamp(db)  # change detector moves on write, stable otherwise
    assert db_stamp(db + "-nope") == (None, None)
    con.execute("delete from messages where id=7")
    con.commit()
    rows = rows_after(db, "s", None)
    assert [r[0] for r in rows] == [3, 5, 6], rows          # starts at last user turn, own session only
    assert rows_after(db, "s", 5)[0][0] == 6                  # incremental
    _, head, body = render(rows[1], 0)
    assert body == ["💭 think", '🔧 terminal({"cmd":"ls"})'], body
    assert render(rows[2], 2)[2] == ["ou …(+1)"]
    assert [r[0] for r in rows_after(db, "*", None)] == [4, 5, 6]  # '*' = every session (Zulip = one per sender)
    # /search: every session, newest first, says where it matched; LIKE wildcards are literal; too short = nothing
    assert [(h["id"], h["session"], h["where"]) for h in search_all(db, "OTHER")] == [(4, "x", "text")]
    assert [(h["id"], h["where"]) for h in search_all(db, "termin")] == [(5, "calls")]
    assert search_all(db, "think")[0]["where"] == "thinking" and search_all(db, "t")== [] and search_all(db, "o%") == []
    assert [h["id"] for h in search_all(db, "o", 9)] == [] and [h["id"] for h in search_all(db, " ol ")] == [1]
    assert search_all(db, "old")[0]["snippet"] == "old" and search_all(db + "-nope", "old") == []
    j = row_json(rows[1], 0)
    assert (j["reasoning"], j["tool_calls"], j["session"], j["source"], j["role"]) == \
        ("think", [{"name": "terminal", "params": {"cmd": "ls"}, "args": None}], "s", "zulip", "assistant"), j
    assert row_json(rows[2], 2)["content"] == "ou …(+1)" and row_json(rows[2], 0)["tool_name"] == "terminal"
    assert row_json(rows[2], 0)["result"] is None                # non-JSON tool output stays plain text
    # tool output that is JSON is parsed BEFORE clipping, so a long output still parses and keeps real newlines
    big = json.dumps({"output": "line1\nline2" + "x" * 50, "exit_code": 1, "error": None, "cwd": "/w"})
    tr = row_json((9, "tool", 1, big, None, "terminal", None, "s", "zulip"), 10)
    assert tr["content"] is None and tr["result"]["exit_code"] == 1 and tr["result"]["cwd"] == "/w", tr
    assert tr["result"]["output"].startswith("line1\nline") and "…(+" in tr["result"]["output"], tr
    assert row_json((9, "tool", 1, "[1,2]", None, "x", None, "s", None), 0)["result"] == [1, 2]
    assert row_json((9, "tool", 1, "42", None, "x", None, "s", None), 0)["content"] == "42"  # scalar JSON = text
    bad = json.dumps([{"function": {"name": "t", "arguments": "{broken"}}, {"function": {"name": "d", "arguments": {"a": 1}}}])
    assert row_json((9, "assistant", 1, "", bad, None, None, "s", None), 0)["tool_calls"] == \
        [{"name": "t", "params": None, "args": "{broken"}, {"name": "d", "params": {"a": 1}, "args": None}]
    assert clip_tree(list(range(60)), 0)[-1] == "…(+10 more)" and len(clip_tree(list(range(60)), 0)) == 51
    assert calls_of("not json") == [] and calls_of('{"a":1}') == []
    # /ctx: active rows only (compaction deactivates folded ones), the summary row is its own part, biggest first
    con.execute("update messages set active=0 where id=1")
    con.execute("update messages set _compressed_summary=1, content=? where id=2", ("s" * 400,))
    con.commit()
    c = context_items(db, "s")
    assert c["rows"] == 4 and [i["id"] for i in c["items"]][:2] == [2, 5], c
    assert c["parts"]["summary"] == 100 and c["parts"]["tool calls"] == len(tc) // 4 and c["parts"]["reasoning"] == 1, c
    assert c["items"][1]["tool"] == "terminal" and c["items"][1]["kind"] == "replies" and c["items"][0]["kind"] == "summary"
    # /tools: each call paired with its result by call id; failures from error / exit_code / success; out of window ignored
    tdb = os.path.join(os.path.dirname(db), "t.db")
    t0 = time.time() - 100
    tcon = sqlite3.connect(tdb)
    tcon.execute("create table messages (id integer primary key, session_id, role, timestamp, tool_calls, tool_call_id, "
                 "tool_name, content)")
    call = lambda *ids: json.dumps([{"id": i, "function": {"name": n}} for i, n in ids])
    tcon.executemany("insert into messages (session_id, role, timestamp, tool_calls, tool_call_id, tool_name, content) "
                     "values (?,?,?,?,?,?,?)",
                     [("s", "assistant", t0 - 9 * 86400, call(("old", "terminal")), None, None, None),
                      ("s", "tool", t0 - 9 * 86400 + 99, None, "old", "terminal", "{}"),
                      ("s", "assistant", t0, call(("a", "terminal"), ("b", "patch")), None, None, None),
                      ("s", "tool", t0 + 4, None, "a", "terminal", '{"exit_code": 1}'),
                      ("s", "tool", t0 + 5, None, "b", "patch", '{"success": true, "diff": ""}'),
                      ("s", "assistant", t0 + 6, call(("c", "terminal")), None, None, None),
                      ("s", "tool", t0 + 8, None, "c", "terminal", "plain text"),
                      ("x", "tool", t0 + 9, None, "c", "terminal", '{"error": "x"}')])
    tcon.commit()
    tcon.close()
    th = {t["tool"]: t for t in tool_health(tdb, "s")["tools"]}
    assert th["terminal"] == {"tool": "terminal", "n": 2, "failed": 1, "median": 4, "max": 4, "total": 6}, th
    assert th["patch"]["n"] == 1 and th["patch"]["failed"] == 0 and list(th) == ["terminal", "patch"], th
    assert tool_health(tdb + "-nope", "s") is None and tool_failed('{"error": ""}') is False
    con.execute("update messages set active=1 where id=1")
    con.execute("update messages set _compressed_summary=0, content='a' where id=2")
    con.commit()
    A = argparse.Namespace(db=db, session="s", width=0)
    p = payload(A, -1, now=7)
    assert [r["id"] for r in p["rows"]] == [3, 5, 6], p
    assert (p["waiting"], p["phase"], p["since_ts"]) == (True, "model", 3), p  # tool result last = model reading it
    assert payload(A, 6, now=7)["rows"] == []
    con.execute("insert into messages (session_id, role, timestamp, content) values ('s','assistant',6,'done')")
    con.commit()
    assert payload(A, 6, now=7)["waiting"] is False                # final reply last = idle
    con.execute("insert into messages (session_id, role, timestamp, content) values ('s','user',9,'now')")
    con.commit()
    p = payload(A, 6, now=10)
    assert p["waiting"] is True and p["phase"] == "model" and p["since_ts"] == 9 and len(p["since"]) == 8, p
    assert payload(A, 6, now=9 + STALE + 1)["waiting"] is False   # dead turn goes idle
    con.execute("insert into messages (session_id, role, timestamp, tool_calls) values ('s','assistant',11,?)", (tc,))
    con.commit()
    p = payload(A, 8, now=12)
    assert (p["waiting"], p["phase"], p["since_ts"]) == (True, "tool terminal", 9), p  # tool running, turn began at 9
    con.execute("insert into messages (session_id, role, timestamp, content, tool_name) values ('s','tool',12,'ok','terminal')")
    con.commit()
    assert payload(A, 8, now=13)["phase"] == "model"              # result written, model reading it
    PAGE = web("index.html") + web("app.js") + web("app.css")
    assert "innerHTML" not in PAGE and "insertAdjacent" not in PAGE and "__CFG__" in PAGE  # untrusted data via textContent only
    pg = page(argparse.Namespace(db=db, interval=2.0), "s")
    cfg = json.loads(re.search(r'<script id=cfg type="application/json">(.*?)</script>', pg, re.S).group(1))
    assert cfg["session"] == "s" and cfg["ms"] == 2000 and [x["id"] for x in cfg["sessions"]] == ["s", "x"], cfg
    assert cfg["agent"] == AGENT and cfg["peers"] == PEERS and "__AGENT__" not in pg and f"Message {AGENT}," in pg
    AGENT = "<b>&"                                                            # the name is escaped into the HTML
    pg = page(argparse.Namespace(db=db, interval=2.0), "s")
    assert "<b>&" not in pg.split('<script id=cfg')[0] and "&lt;b&gt;&amp;" in pg
    AGENT = "hermes"
    evil = "</script><!--<script>"                                              # a title can't break the boot config
    con.execute("update sessions set title=? where id='s'", (evil,))
    con.commit()
    pg = page(argparse.Namespace(db=db, interval=2.0), "s")
    raw = re.search(r'<script id=cfg type="application/json">(.*?)</script>', pg, re.S).group(1)
    assert "<" not in raw and json.loads(raw)["sessions"][0]["title"] == evil, raw
    con.execute("update sessions set title='A session' where id='s'")
    con.commit()
    # compaction: the summary row carries a marker; the flag comes from a live (unexpired) compression_locks row
    cmid = con.execute("insert into messages (session_id, role, timestamp, content, _compressed_summary) "
                       "values ('s','assistant',13,'[PRIOR CONTEXT …]',1)").lastrowid
    con.commit()
    assert payload(A, cmid - 1, now=14)["rows"][-1]["compacted"] is True
    assert render(rows_after(db, "s", cmid - 1)[0], 0)[1].endswith("[compacted]")   # terminal mode labels it too
    assert payload(A, -1, now=14)["compacting"] is False                      # no lock row = not compacting
    con.execute("insert into compression_locks values ('s','pid=1',14,20)")
    con.commit()
    assert payload(A, -1, now=15)["compacting"] is True                       # live lease = compacting
    assert payload(A, -1, now=21)["compacting"] is False                      # expired lease = idle
    assert payload(argparse.Namespace(db=db, session="nope", width=0), -1, now=15)["compacting"] is False
    # bookkeeping rows carry display_kind: never rendered as oko prose, session_meta dropped entirely
    kids = []
    for kind, role, body in (("failed_turn", "assistant", "Your request was not processed."),
                             ("internal_notification", "user", "[System note: gateway restart]"),
                             ("session_meta", "session_meta", None),
                             ("hidden", "assistant", "")):
        k = con.execute("insert into messages (session_id, role, timestamp, content, display_kind, api_content) "
                        "values ('s',?,16,?,?,'[response interrupted]')", (role, body, kind)).lastrowid
        kids.append((k, kind))
        con.commit()
    k = kids[0][0]
    assert [r["kind"] for r in payload(A, k - 1, now=17)["rows"]] == ["failed_turn", "internal_notification", "hidden"], \
        [r["kind"] for r in payload(A, k - 1, now=17)["rows"]]   # session_meta filtered out
    assert render(rows_after(db, "s", k - 1)[0], 0) == (k, f"{time.strftime('%H:%M:%S', time.localtime(16))} #{k} assistant [turn failed]",
                                                       ["turn failed: Your request was not processed."])
    assert payload(A, -1, now=17)["rows"][-1]["kind"] == "hidden" and payload(A, -1, now=17)["rows"][-1]["content"] is None
    kinds = con.execute("select distinct display_kind from messages where id>?", (k - 1,)).fetchall()
    assert sorted(x[0] for x in kinds) == ["failed_turn", "hidden", "internal_notification", "session_meta"], kinds
    con.execute("delete from messages where id>?", (k - 1,))
    con.commit()
    # "load earlier": a page from before `before`, oldest-first so the caller can prepend; cursor walks back
    rb = rows_before(db, "s", 7, 100)
    assert [r[0] for r in rb] == [1, 2, 3, 5, 6] and rows_before(db, "*", 7)[0][0] == 1, rb
    assert [r[0] for r in rows_before(db, "s", 4)] == [1, 2, 3]              # `more` is len==limit
    assert rows_before(db, "s", 1) == [] and rows_before(db, "s", 1, 5) == []
    assert session_title(db, "s") == "A session" and session_title(db, "*") is None and session_title(db, "nope") is None
    assert [x["id"] for x in sessions_list(db)] == ["s", "x"], sessions_list(db)   # newest first
    assert set(sessions_list(db)[0]) == {"id", "source", "title", "last"}
    # default session: the route's newest session, else MAIN, else the newest session, else all
    assert main_session(db) == "s"                                            # nothing configured -> newest
    empty = os.path.join(tempfile.mkdtemp(), "empty.db")
    sqlite3.connect(empty).close()
    assert main_session(empty) == "*"                                         # no sessions at all -> all
    MAIN, MAIN_KEY = "main", "agent:main:zulip:dm:dm:me@z"
    assert main_session(db) == MAIN                                           # no gateway_routing table yet
    rc = sqlite3.connect(db)
    rc.execute("create table gateway_routing (scope, session_key, entry_json, updated_at)")
    rc.executemany("insert into gateway_routing values ('', ?, ?, ?)", [
        (MAIN_KEY, json.dumps({"session_id": "old"}), 1), (MAIN_KEY, json.dumps({"session_id": "x"}), 2),
        ("agent:main:zulip:dm:dm:helper-bot@z", json.dumps({"session_id": "s"}), 3)])
    rc.commit()
    assert main_session(db) == "x", main_session(db)
    rc.execute("update gateway_routing set entry_json=? where updated_at=2", (json.dumps({"session_id": "../x"}),))
    rc.commit(); rc.close()
    assert main_session(db) == MAIN                                           # a malformed id is never used
    # compose: the send target must be a session we already show, so a bad id never runs a fresh turn
    assert known_session(db, "s") and known_session(db, "x") and not known_session(db, "nope")
    many = os.path.join(tempfile.mkdtemp(), "many.db")                        # older than the picker's newest 30
    mc = sqlite3.connect(many)
    mc.execute("create table sessions (id primary key, source, title, started_at, last_activity_at)")
    mc.execute("create table messages (session_id, active default 1, display_kind)")
    mc.executemany("insert into sessions values (?, 'tui', null, ?, ?)", [(f"m{i}", i, i) for i in range(31)])
    mc.executemany("insert into messages (session_id) values (?)", [(f"m{i}",) for i in range(31)] + [("orphan",)])
    mc.commit(); mc.close()
    assert "m0" not in {s["id"] for s in sessions_list(many)} and known_session(many, "m0")
    assert not known_session(many, "orphan")                                  # rows but no sessions entry
    st, body = start_turn(db, "nope", "hi")                                  # unknown id -> refused, no request
    assert (st, body["error"]) == (400, "unknown session"), (st, body)
    os.environ.pop("API_SERVER_KEY", None)
    assert _api_key(os.path.join(tempfile.mkdtemp(), "none.env")) == ""       # missing file -> no key, not a crash
    os.environ["API_SERVER_KEY"] = " fromenv "
    assert _api_key("/dev/null") == "fromenv"                                 # our own env wins over the file
    del os.environ["API_SERVER_KEY"]
    assert db_check(db)[0] and not db_check(db + ".missing")[0] and not db_check("/dev/null")[0]  # first-run errors
    assert start_turn(db, "s", "hi", env_path="/dev/null")[0] == 503          # no key -> refuse before dialing out
    # origin gate: scheme must NOT matter (a public page is https behind the proxy, the app serves http)
    assert same_origin("https://tail.example.com", "tail.example.com") and same_origin(None, "h")
    assert not same_origin("null", "h") and not same_origin("garbage", "h")
    assert same_origin("http://127.0.0.1:9120", "127.0.0.1:9120")
    assert not same_origin("https://evil.example", "tail.example.com")
    assert not same_origin("https://tail.example.com.evil.example", "tail.example.com")
    # the API key is never written into the page or a response body
    assert "API_SERVER_KEY" not in PAGE and "Bearer" not in PAGE
    assert "/say" in PAGE and "compose" in PAGE               # the compose surface is actually wired into the page
    # peers: only web urls or same-host paths, never javascript:, protocol-relative or nameless
    assert parse_peers(" staging = /staging/ ,x=https://t.example/a,bad=javascript:alert(1),pr=//evil.example,=/n,y") == \
        [{"name": "staging", "url": "/staging/"}, {"name": "x", "url": "https://t.example/a"}]
    assert parse_peers("") == [] and parse_peers("a=/\\evil.example,b=/ok\\x") == []   # browsers read \ as /
    # /browse: memory split on §, staged writes (junk skipped), skills with category, cron; /skill by name only
    h = tempfile.mkdtemp()
    for p, body in (("memories/MEMORY.md", "one\n§\ntwo\nlines\n§\n"), ("memories/USER.md", "me"),
                    ("pending/memory/ab12cd34.json", json.dumps({"id": "ab12cd34", "action": "add", "summary": "s",
                                                                 "created_at": 5, "payload": {"content": "x"}})),
                    ("pending/memory/bad.json", "{"), ("pending/skills/zz.json", json.dumps({"id": "../x"})),
                    ("pending/memory/deep.json", "[" * 100000), ("pending/memory/ab12cd35.txt", json.dumps({"id": "ab12cd35"})),
                    ("skills/SKILL.md", "---\nname: top\n---\n"), ("secret.md", "---\nname: leak\n---\nnope"),
                    ("skills/ops/deploy/SKILL.md", "---\nname: deploy\ndescription: ship it\n---\nbody"),
                    ("skills/solo/SKILL.md", "---\nname: solo\n---\n"), ("cron/jobs.json", json.dumps(
                        {"jobs": [{"id": "j1", "name": "n", "schedule_display": "every 5m", "secret_field": 1}]}))):
        os.makedirs(os.path.dirname(os.path.join(h, p)), exist_ok=True)
        with open(os.path.join(h, p), "w") as f:
            f.write(body)
    os.makedirs(os.path.join(h, "skills/evil")); os.symlink(os.path.join(h, "secret.md"), os.path.join(h, "skills/evil/SKILL.md"))
    b = browse(h, os.path.join(h, "skills"))
    assert (b["memory"], b["user"]) == (["one", "two\nlines"], ["me"]), b
    assert [(p["id"], p["subsystem"], p["ts"]) for p in b["pending"]] == [("ab12cd34", "memory", 5)], b["pending"]  # junk, deep, .txt skipped
    assert '"content": "x"' in b["pending"][0]["detail"]
    assert [(s["category"], s["name"], s["desc"]) for s in b["skills"]] == \
        [("", "leak", ""), ("", "solo", ""), ("", "top", ""), ("ops", "deploy", "ship it")], b["skills"]
    assert all(set(s) == {"name", "desc", "category"} for s in b["skills"]), b["skills"]   # no server paths on the wire
    assert skill_text("leak", os.path.join(h, "skills")) is None                  # symlink out of the root: listed, never read
    assert b["cron"][0]["schedule_display"] == "every 5m" and "secret_field" not in b["cron"][0]
    assert browse(tempfile.mkdtemp(), "/nonexistent") == {"memory": [], "user": [], "pending": [], "skills": [], "cron": []}
    assert skill_text("deploy", os.path.join(h, "skills")).endswith("body") and skill_text("../ops", h) is None
    assert all("dir" not in s for s in skills_list("/dev/null", "http://127.0.0.1:9", os.path.join(h, "skills")))
    selftest_gateway(db)
    print("selftest ok")


def selftest_socket(A, post):
    """The plugin socket against a fake one in a temp dir: command list, say -> message + done, held, timeout,
    socket missing / refused -> fallback, and a non-main session never touching the socket."""
    global CMD_TIMEOUT
    import tempfile
    home, seen = tempfile.mkdtemp(), []
    path = os.path.join(home, "oko-tail.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(8)

    def serve_one(c):
        buf = b""
        while b"\n" not in buf:
            chunk = c.recv(4096)
            if not chunk:
                return
            buf += chunk
        req = json.loads(buf)
        seen.append(req)
        out = lambda *ds: c.sendall(b"".join(json.dumps(d).encode() + b"\n" for d in ds))
        if req["op"] == "commands":
            out({"type": "commands", "commands": [
                {"name": "compress", "aliases": ["compact"], "args_hint": "[focus]", "description": "Compress context",
                 "category": "Session"}, {"name": "../bad", "description": "x"}, {"name": "usage", "aliases": [],
                 "args_hint": "", "description": "Token usage", "category": "Info"}]}, {"type": "done", "reason": "finished"})
            return
        if req["op"] == "queue":
            out({"type": "queue", "busy": True, "items": [
                {"where": "next", "id": "70426", "page": False, "from": "Sam", "peer": False, "text": "lmk when u get this",
                 "images": 0, "ts": 1.5}, "junk", {"where": "held", "id": "9", "from": "helper", "peer": True,
                 "text": "x" * 3000, "images": "2", "ts": "soon"}]}, {"type": "done", "reason": "finished"})
            return
        text = req["text"]
        if text.startswith("/slow"):
            time.sleep(3)                                          # never answers inside the test's timeout
        elif text.startswith("/busy"):
            out({"type": "accepted", "id": "local-1"}, {"type": "held", "id": "local-1"}, {"type": "done", "reason": "finished"})
        elif text.startswith("/turn"):
            out({"type": "accepted", "id": "local-2"}, {"type": "mystery", "x": 1}, {"type": "done", "reason": "finished"})
        elif text.startswith("/boom"):
            out({"type": "accepted", "id": "local-3"}, {"type": "error", "error": "no can do"}, {"type": "done", "reason": "rejected"})
        else:
            out({"type": "accepted", "id": "local-0"}, {"type": "message", "id": "local-0:1", "text": "did " + text},
                {"type": "message", "id": "local-0:2", "text": "  "}, {"type": "edit", "id": "local-0:1", "text": "ignored"},
                {"type": "done", "reason": "finished"})

    def accept():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(c,), daemon=True).start()

    def handle(c):
        try:
            serve_one(c)
        except OSError:
            pass                                                   # the client hung up (the timeout case)
        finally:
            c.close()
    threading.Thread(target=accept, daemon=True).start()
    A.home = home
    cmd = lambda name, a="", sess="s": post("/cmd", {"session": sess, "name": name, "args": a})
    # commands: mapped to the page's shape, a bad name dropped, aliases and category kept
    assert socket_commands(home) == [
        {"name": "compress", "args": "[focus]", "desc": "Compress context", "aliases": ["compact"], "category": "Session"},
        {"name": "usage", "args": "", "desc": "Token usage", "aliases": [], "category": "Info"}], socket_commands(home)
    # queue: run order kept, junk rows dropped, fields typed and capped; no socket -> None
    q = socket_queue(home)
    assert [(i["where"], i["from"], i["peer"]) for i in q] == [("next", "Sam", False), ("held", "helper", True)], q
    assert q[0]["text"] == "lmk when u get this" and q[0]["ts"] == 1.5 and len(q[1]["text"]) == 2000
    assert q[1]["images"] == 0 and q[1]["ts"] is None and socket_queue(home + "/nope") is None
    # a say: any slash command goes out verbatim; message texts come back (blank ones and unknown line types ignored)
    assert cmd("compress", " here 4 ") == (200, {"messages": ["did /compress here 4"], "held": False, "turn": False, "done": "finished"})
    assert seen[-1] == {"op": "say", "text": "/compress here 4"}
    assert cmd("usage")[1]["messages"] == ["did /usage"] and seen[-1]["text"] == "/usage"
    assert cmd("busy") == (200, {"messages": [], "held": True, "turn": True, "done": "finished"})
    assert cmd("turn") == (200, {"messages": [], "held": False, "turn": True, "done": "finished"})
    assert cmd("boom")[0] == 502 and cmd("boom")[1]["error"] == "no can do"
    # bad names / oversized args never reach the socket
    n = len(seen)
    assert cmd("rm -rf")[0] == 400 and cmd("a" * 65)[0] == 400 and cmd("compress", "x" * 8001)[0] == 400 and len(seen) == n
    # skill is the page's own (a turn through the gateway), not a slash command
    assert cmd("skill")[0] == 400 and len(seen) == n
    # cross-site POST is still refused before anything is sent
    assert post("/cmd", {"session": "s", "name": "compress"}, "https://evil.example")[0] == 403 and len(seen) == n
    # a non-main session never touches the socket: REST for what it can do, a clear error for the rest
    assert cmd("compress", "", "x")[0] == 400 and "main session" in cmd("compress", "", "x")[1]["error"] and len(seen) == n
    assert cmd("title", "t", "x")[0] == 200 and len(seen) == n
    # timeout: the reply so far, done=timeout, and we hang up
    CMD_TIMEOUT = 0.4
    t0 = time.monotonic()
    assert cmd("slow") == (200, {"messages": [], "held": False, "turn": False, "done": "timeout"})
    assert time.monotonic() - t0 < 2
    CMD_TIMEOUT = 90
    # the pointer file wins (long-path case), the plain path is the fallback, and a dead one is just "down"
    other = tempfile.mkdtemp()
    with open(os.path.join(other, "oko-tail.sock.path"), "w") as f:
        f.write(path + "\n")
    assert sock_paths(other) == [path, os.path.join(other, "oko-tail.sock")] and socket_commands(other)[0]["name"] == "compress"
    # a listener owned by another uid is treated as no socket at all
    real_getuid, os.getuid = os.getuid, lambda: real_getuid() + 1
    try:
        assert socket_commands(home) is None
    finally:
        os.getuid = real_getuid
    # socket missing or stale (file there, nobody listening) -> unavailable -> REST fallback
    srv.close()
    os.unlink(path)
    assert socket_commands(home) is None and socket_cmd("/usage", home) is None
    assert cmd("status") == (200, {"id": "s", "model": "m"})
    assert cmd("compress") == (503, {"error": NOSOCK})
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(path)
    stale.close()                                                  # a bound-then-closed socket file refuses connections
    assert socket_commands(home) is None and cmd("compress")[0] == 503
    os.unlink(path)
    A.home = tempfile.mkdtemp()


def selftest_gateway(db):
    """Every POST path, the live overlay and /cmds against a stub gateway that speaks the real event names."""
    import tempfile
    calls, release = [], threading.Event()

    class G(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def reply(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            if self.path == "/health":
                return self.reply(200, {"status": "ok"})
            calls.append(("GET", self.path, self.headers.get("Authorization")))
            if self.headers.get("Authorization") != "Bearer test":
                return self.reply(401, {"error": "unauthorized"})
            r = {"/api/sessions/s": {"id": "s", "model": "m"}, "/api/model/options": {"models": ["a"]},
                 "/v1/skills": {"object": "list", "data": [{"name": "daily-report", "description": "d", "category": "c"}]}}
            return self.reply(200, r[self.path]) if self.path in r else self.reply(404, {"error": "nope"})

        def do_PATCH(self):
            calls.append(("PATCH", self.path, self.body()))
            self.reply(200, {"ok": True})

        def do_POST(self):
            b = self.body()
            calls.append(("POST", self.path, b))
            if self.headers.get("Authorization") != "Bearer test":
                return self.reply(401, {"error": "unauthorized"})
            if self.path.endswith("/stop"):
                release.set()
                return self.reply(200, {"run_id": self.path.split("/")[3], "status": "stopping"})
            if self.path.endswith("/steer"):
                return self.reply(200, {"accepted": True})
            if self.path.endswith("/fork"):
                return self.reply(201, {"id": "s-fork"})
            if self.path.endswith("/model"):
                return self.reply(200, {"ok": True})
            if not self.path.endswith("/chat/stream"):
                return self.reply(404, {"error": "nope"})
            rid = "r%d" % len(calls)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()

            def ev(name, **d):
                self.wfile.write(f"event: {name}\ndata: {json.dumps({'run_id': rid, **d})}\n\n".encode())
                self.wfile.flush()
            ev("run.started", user_message={"role": "user", "content": b["message"]})
            self.wfile.write(b": keepalive\n\n")
            ev("message.started", message={"id": "m1", "role": "assistant"})
            ev("assistant.delta", message_id="m1", delta="Hel")
            ev("assistant.delta", message_id="m1", delta="lo")
            ev("tool.progress", message_id="m1", tool_name="_thinking", delta="hmm")
            ev("tool.started", message_id="m1", tool_name="terminal", preview="ls")
            if b["message"] == "eof":
                return                                    # stream dies mid-turn
            if b["message"] == "hang":
                release.wait(5)
                ev("run.cancelled", interrupted=True)
            else:
                ev("tool.completed", message_id="m1", tool_name="terminal")
                ev("message.started", message={"id": "m2", "role": "assistant"})
                ev("assistant.delta", message_id="m2", delta="final")
                ev("run.completed", usage={"input_tokens": 3})
            ev("done")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), G)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    g = f"http://127.0.0.1:{srv.server_address[1]}"
    env = os.path.join(tempfile.mkdtemp(), "t.env")
    with open(env, "w") as f:
        f.write("API_SERVER_KEY=test\n")
    sk = tempfile.mkdtemp()
    os.makedirs(os.path.join(sk, "a", "b"))
    with open(os.path.join(sk, "a", "b", "SKILL.md"), "w") as f:
        f.write('---\nname: "deep-skill"\ndescription: "does a thing"\n---\nbody\n')
    global MAIN
    MAIN = "s"                                                     # the page's main session (the socket only serves this one)
    nohome = tempfile.mkdtemp()                                    # no oko-tail.sock in here: every command takes the REST path
    A = argparse.Namespace(db=db, session="s", width=0, env=env, gateway=g, home=nohome)
    post = lambda path, obj, origin=None: handle_post(A, path, origin, "127.0.0.1:9120", json.dumps(obj).encode(), sk)

    def settle(s, t=5):
        end = time.time() + t
        while time.time() < end:
            r = live_get(s)
            if r and r["status"] in TERMINAL:
                return r
            time.sleep(0.02)
        raise AssertionError(live_get(s))

    # validation: every POST path refuses a cross-site origin, bad/unknown sessions, empty messages, bad json
    for path in POSTS:
        assert post(path, {"session": "s", "message": "x", "name": "status"}, "https://evil.example")[0] == 403, path
        assert post(path, {"session": "../x", "message": "x"})[0] == 400 and post(path, {"session": "nope", "message": "x"})[0] == 400
    assert handle_post(A, "/say", None, "h", b"{nope", sk) == (400, {"error": "bad json"})
    assert handle_post(A, "/say", None, "h", b"[1]", sk)[0] == 400 and post("/nope", {})[0] == 404
    assert post("/say", {"session": "s", "message": "  "})[1]["error"] == "empty message"
    assert post("/steer", {"session": "s"})[0] == 400
    # nothing live yet: stop/steer refuse with 409, never call the gateway
    assert post("/stop", {"session": "x"})[0] == 409 and post("/steer", {"session": "x", "message": "go"})[0] == 409
    assert payload(A, -1)["live"] is None and payload(A, -1)["gateway"] is True
    st = payload(A, -1)["stats"]
    assert st["title"] == "A session" and st["source"] == "zulip" and st["model"] is None, st  # missing columns -> None
    assert payload(argparse.Namespace(db=db, session="*", width=0, gateway=g), -1)["stats"] is None
    # a streamed turn: 202 at once, the overlay holds only what came after the last tool call (what came before is
    # in the DB with the call; the thinking here preceded it), then lands done
    v0 = LIVE_VER[0]
    assert post("/say", {"session": "s", "message": "hi"}) == (202, {"ok": True})
    r = settle("s")
    assert (r["status"], r["text"], r["thinking"], r["tool"], r["tools"], r["usage"]) == \
        ("done", "final", "", None, 1, {"input_tokens": 3}), r
    assert r["run_id"] and r["message"] == "hi" and r["ended"] and r["error"] is None and LIVE_VER[0] > v0
    assert payload(A, -1)["live"]["status"] == "done"
    assert ("POST", "/api/sessions/s/chat/stream", {"message": "hi"}) in calls
    # images ride along as OpenAI-style parts; only image data URLs, at most MAX_IMAGES, text optional
    png = "data:image/png;base64,iVBORw0KGgo="
    assert post("/say", {"session": "s", "message": "", "images": [png]}) == (202, {"ok": True})
    settle("s")
    assert ("POST", "/api/sessions/s/chat/stream", {"message": [{"type": "image_url", "image_url": {"url": png}}]}) in calls
    assert turn_content("look", [png])[0] == {"type": "text", "text": "look"} and turn_content("plain", []) == "plain"
    for bad in (["data:text/html;base64,PGI+"], ["https://evil.example/x.png"], [png] * (MAX_IMAGES + 1), "notalist", [1]):
        assert post("/say", {"session": "s", "message": "x", "images": bad})[0] == 400, bad
    # stream dies without a terminal event -> failed, "stream ended"
    assert post("/say", {"session": "s", "message": "eof"})[0] == 202
    r = settle("s")
    assert (r["status"], r["error"], r["text"], r["tool"]) == ("failed", "stream ended", "", None), r  # "Hello" preceded a call
    # stop: only while running, aimed at the run_id we hold; the stream then ends cancelled
    assert post("/say", {"session": "x", "message": "hang"})[0] == 202
    end = time.time() + 5
    while (live_get("x") or {}).get("tool") != "terminal" and time.time() < end:
        time.sleep(0.02)
    rid = live_get("x")["run_id"]
    assert post("/steer", {"session": "x", "message": "go left"}) == (200, {"accepted": True})
    assert ("POST", f"/v1/runs/{rid}/steer", {"message": "go left"}) in calls
    assert post("/stop", {"session": "x"}) == (200, {"run_id": rid, "status": "stopping"})
    assert settle("x")["status"] == "cancelled"
    assert post("/stop", {"session": "x"})[0] == 409                        # already over
    # slash commands map to gateway REST
    cmd = lambda name, a="": post("/cmd", {"session": "s", "name": name, "args": a})
    assert cmd("rm -rf")[0] == 400 and cmd(None)[0] == 400
    assert cmd("compress") == (503, {"error": NOSOCK}) and cmd("help")[0] == 503      # no socket: say so, never "invalid"
    assert cmd("title")[0] == 400 and cmd("title", "New name") == (200, {"ok": True})
    assert ("PATCH", "/api/sessions/s", {"title": "New name"}) in calls
    assert cmd("status") == (200, {"id": "s", "model": "m"}) and cmd("model") == (200, {"models": ["a"]})
    assert cmd("model", "big") == (200, {"ok": True}) and ("POST", "/api/sessions/s/model", {"model": "big"}) in calls
    assert cmd("fork") == (201, {"id": "s-fork"}) and ("POST", "/api/sessions/s/fork", {}) in calls
    assert cmd("steer")[0] == 400 and cmd("stop")[0] == 409
    assert last_user_text(db, "s") == "now"                                  # newest user row, not ours (we never write the DB)
    assert cmd("retry") == (202, {"ok": True}) and settle("s")["message"] == "now"
    assert cmd("skill")[0] == 400 and cmd("skill", "nope")[0] == 400
    assert cmd("skill", "daily-report summarize today") == (202, {"ok": True})
    assert settle("s")["message"] == "Use your `daily-report` skill.\n\nsummarize today"
    assert skills_list(env, g, sk) == [{"name": "daily-report", "desc": "d"}]
    assert [{k: v for k, v in x.items() if k != "dir"} for x in skills_from_dir(sk)] == [{"name": "deep-skill", "desc": "does a thing"}]   # the down-gateway fallback
    assert skills_list("/dev/null", g + "0", sk) == [{"name": "deep-skill", "desc": "does a thing"}]
    selftest_socket(A, post)
    selftest_http(A)
    # a dead gateway: offline, says so, never starts a run
    dead = argparse.Namespace(db=db, session="s", width=0, env=env, gateway="http://127.0.0.1:9", home=nohome)
    assert gateway_up(dead.gateway) is False
    assert handle_post(dead, "/say", None, "h", b'{"session":"s","message":"hi"}', sk) == (502, {"error": OFFLINE})
    assert run_cmd(dead, "s", "status", "") == (502, {"error": OFFLINE})
    # live events fold the way the gateway emits them (interrupted completion, error events)
    run = {"run_id": None, "status": "starting", "text": "", "thinking": "", "tool": None, "tools": 0,
           "error": None, "ended": None, "usage": None}
    for e, d in sse_frames([b"event: run.started\n", b'data: {"run_id": "q1"}\n', b"\n", b": ka\n", b"\n",
                            b"event: assistant.completed\n", b'data: {"content": "x", "interrupted": true}\n', b"\n",
                            b"event: run.completed\n", b"data: {}\n", b"\n"]):
        live_event(run, e, d, now=5)
    assert (run["run_id"], run["status"], run["text"], run["ended"]) == ("q1", "cancelled", "x", 5), run
    run.update(status="running", ended=None)
    live_event(run, "error", {"message": "boom"})
    assert (run["status"], run["error"]) == ("failed", "boom")
    # stage: wait until something comes back, think on reasoning, write on tokens, tool, then wait again
    run = {"run_id": None, "status": "running", "text": "", "thinking": "", "tool": None, "tools": 0,
           "error": None, "ended": None, "usage": None, "stage": "wait", "stage_ts": 0}
    seen = []
    for t, (e, d) in enumerate([("message.started", {}), ("tool.progress", {"tool_name": "_thinking", "delta": "h"}),
                                ("tool.progress", {"tool_name": "_thinking", "delta": "m"}), ("tool.started", {}),
                                ("tool.completed", {}), ("assistant.delta", {"delta": "ok"})], 1):
        live_event(run, e, d, now=t)
        seen.append((run["stage"], run["stage_ts"]))
    assert seen == [("wait", 0), ("think", 2), ("think", 2), ("tool", 4), ("wait", 5), ("write", 6)], seen
    with LIVE_LOCK:                                                          # a finished run ages out of LIVE
        LIVE["s"]["ended"] = time.time() - LIVE_KEEP - 1
    assert live_get("s") is None and "s" not in LIVE
    srv.shutdown()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--session", default=None,
                   help="session id, or * = all (default: OKO_TAIL_MAIN_ROUTE's session, else OKO_TAIL_MAIN_SESSION, else the newest)")
    p.add_argument("--db", default=DB, help="hermes state.db (default $OKO_TAIL_DB, else $HERMES_HOME/state.db)")
    p.add_argument("--interval", type=float, default=2.0, help="terminal mode: poll every N seconds")
    p.add_argument("--width", type=int, default=2000, help="clip each field to N chars (0 = no clip)")
    p.add_argument("--env", default=ENV,
                   help="env file holding API_SERVER_KEY (default $OKO_TAIL_ENV, else $HERMES_HOME/.env)")
    p.add_argument("--gateway", default=GATEWAY, help="hermes gateway base URL (default $HERMES_GATEWAY)")
    p.add_argument("--serve", type=int, metavar="PORT", help="serve the web page on PORT ($OKO_TAIL_HOST, default 127.0.0.1)")
    p.add_argument("--check", action="store_true", help="show the resolved config and what works, then exit")
    p.add_argument("--selftest", action="store_true")
    a = p.parse_args()
    if a.check:
        sys.exit(check(a))
    if not a.selftest and not db_check(a.db)[0]:
        sys.exit(f"oko-tail: {a.db}: {db_check(a.db)[1]}. Run --check to see the whole config.")
    try:
        selftest() if a.selftest else serve(a) if a.serve else follow(a)
    except KeyboardInterrupt:
        pass

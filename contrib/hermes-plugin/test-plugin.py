"""Plugin check for contrib/hermes-plugin/oko-tail-progress (stdlib only, no hermes needed).

Imports the plugin by path, drives the stream hooks directly to prove the state they keep (stage,
timer, character counts, capped reasoning tail), and runs the real unix-socket query against it.
"""
import importlib.util, json, os, socket, stat, struct, sys, tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(HERE, "oko-tail-progress")


def load():
    """Import the plugin package by path (its directory name has dashes, so not importable by name)."""
    spec = importlib.util.spec_from_file_location("oko_tail_progress", os.path.join(PKG, "__init__.py"),
                                                  submodule_search_locations=[PKG])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


P = load()


class FakeCtx:
    """The slice of hermes' PluginContext the plugin uses: hooks, unload, one setting."""

    def __init__(self, config=None):
        self.hooks, self.unloads, self.config = {}, [], config or {}

    def register_hook(self, name, cb):
        self.hooks.setdefault(name, []).append(cb)

    def on_unload(self, cb):
        self.unloads.append(cb)

    def get_config(self, key, default=None):
        return self.config.get(key, default)


def reset():
    with P._LOCK:
        P._SESSIONS.clear()


def fire(ctx, name, **kw):
    """Run every callback registered for `name`, the way the host's dispatch would."""
    for cb in ctx.hooks.get(name, []):
        cb(**kw)


# ---- hook -> state: stage, timer, counters and memory-only reasoning tail ---------------------
reset()
ctx = FakeCtx()
P.register(ctx)
assert set(ctx.hooks) == {"on_stream_start", "on_stream_delta", "on_stream_end"}, ctx.hooks

t0 = time.time()
fire(ctx, "on_stream_start", session_id="s1", turn_id="t1", model="m")
st = P.snapshot("s1")["s1"]
assert (st["stage"], st["open"], st["reasoning_chars"], st["text_chars"]) == ("wait", True, 0, 0), st
assert st["turn_id"] == "t1" and st["model"] == "m"
assert t0 - 1 <= st["stage_ts"] <= time.time(), st

fire(ctx, "on_stream_delta", session_id="s1", delta="hmm", kind="reasoning")
st = P.snapshot("s1")["s1"]
assert (st["stage"], st["reasoning_chars"], st["text_chars"]) == ("think", 3, 0), st
assert st["thinking"] == "hmm", st
think_ts = st["stage_ts"]

# a second reasoning delta keeps the stage (and its timer) and only grows the count
fire(ctx, "on_stream_delta", session_id="s1", delta="..more", kind="reasoning")
st = P.snapshot("s1")["s1"]
assert st["reasoning_chars"] == 9 and st["stage_ts"] == think_ts, st  # still "thinking for Ns"

fire(ctx, "on_stream_delta", session_id="s1", delta="Hello there", kind="text")
st = P.snapshot("s1")["s1"]
assert (st["stage"], st["reasoning_chars"], st["text_chars"]) == ("write", 9, 11), st
assert st["stage_ts"] >= think_ts, st
assert st["thinking"] == "hmm..more" and "Hello there" not in json.dumps(st), st

fire(ctx, "on_stream_end", session_id="s1")
assert P._SESSIONS["s1"]["thinking"] == ""
assert P.snapshot("s1")["s1"]["open"] is False, P.snapshot("s1")

# each hook has its own queue and worker thread, so a delta can land after its own on_stream_end.
# A closed record must stay closed: a late delta may not flip a finished turn back to "writing".
fire(ctx, "on_stream_delta", session_id="s1", delta="late arrival", kind="text")
st = P.snapshot("s1")["s1"]
assert st["open"] is False and (st["stage"], st["text_chars"]) == ("write", 11), st
fire(ctx, "on_stream_delta", session_id="s1", delta="more", kind="reasoning")
assert P.snapshot("s1")["s1"]["reasoning_chars"] == 9, P.snapshot("s1")   # counters didn't move either

# a record whose end never arrived reads as closed once it goes stale, so a watcher stops showing a stage
fire(ctx, "on_stream_start", session_id="s2")
fire(ctx, "on_stream_delta", session_id="s2", kind="reasoning", delta="stale tail")
with P._LOCK:
    P._SESSIONS["s2"]["updated_ts"] = time.time() - P.STALE_OPEN - 1
assert P.snapshot("s2")["s2"]["open"] is False, P.snapshot("s2")
assert P._SESSIONS["s2"]["thinking"] == ""
with P._LOCK:
    P._SESSIONS["s2"]["updated_ts"] = time.time()                        # a live one stays open
assert P.snapshot("s2")["s2"]["open"] is True, P.snapshot("s2")

# a new call starts fresh counters (a turn has several model calls, one per tool round)
fire(ctx, "on_stream_start", session_id="s1", turn_id="t1")
st = P.snapshot("s1")["s1"]
assert (st["stage"], st["reasoning_chars"], st["text_chars"], st["open"]) == ("wait", 0, 0, True), st
P._SESSIONS.pop("s2", None)

# a delta for a session we never saw start still makes a record (a dropped start must not lose the turn)
fire(ctx, "on_stream_delta", session_id="late", delta="x", kind="text")
assert P.snapshot("late")["late"]["stage"] == "write", P.snapshot("late")

# missing session_id is ignored; unknown kind neither crashes nor moves the stage
before = P.snapshot()
fire(ctx, "on_stream_start", session_id="")
fire(ctx, "on_stream_delta", session_id="s1", delta="z", kind="weird")
assert P.snapshot() == before, P.snapshot()

# snapshot: all sessions, one session, or nothing for an unknown one
assert set(P.snapshot()) == {"s1", "late"} and P.snapshot("*") == P.snapshot()
assert P.snapshot("nope") == {} and set(P.snapshot("late")) == {"late"}
P.snapshot("s1")["s1"]["stage"] = "tampered"                          # a copy, not the live dict
assert P.snapshot("s1")["s1"]["stage"] != "tampered", P.snapshot("s1")

# the record set is bounded: the oldest sessions drop first
reset()
for i in range(P.MAX_SESSIONS + 20):
    P._advance("s%03d" % i, "write", write=1)
    time.sleep(0)                                                     # distinct updated_ts
assert len(P._SESSIONS) <= P.MAX_SESSIONS and "s000" not in P._SESSIONS and "s%03d" % (P.MAX_SESSIONS + 19) in P._SESSIONS

# The tail rolls by characters, is isolated per session, and resets on the next call.
reset()
P.on_stream_delta(session_id="tail", kind="reasoning", delta="a" * (P.THINK_CAP + 7))
P.on_stream_delta(session_id="tail", kind="reasoning", delta="end")
assert P.snapshot("tail")["tail"]["thinking"] == "a" * (P.THINK_CAP - 3) + "end"
assert P.snapshot("tail")["tail"]["reasoning_chars"] == P.THINK_CAP + 10
P.on_stream_delta(session_id="other", kind="reasoning", delta="separate")
assert P.snapshot("other")["other"]["thinking"] == "separate"
P.on_stream_start(session_id="tail")
assert P.snapshot("tail")["tail"]["thinking"] == ""
P.on_stream_end(session_id="other")
P.on_stream_delta(session_id="other", kind="reasoning", delta="late")
assert P._SESSIONS["other"]["thinking"] == ""

# ---- the progress socket: the query, its gate, and its cleanup --------------------------------
reset()
home = tempfile.mkdtemp()
os.environ["OKO_TAIL_PROGRESS_SOCKET"] = os.path.join(home, "p.sock")
try:
    assert P.socket_path() == os.path.join(home, "p.sock")
    del os.environ["OKO_TAIL_PROGRESS_SOCKET"]
    assert P.socket_path(FakeCtx({"socket_path": os.path.join(home, "cfg.sock")})) == os.path.join(home, "cfg.sock")
    assert P.socket_path(FakeCtx()).endswith(P.DEFAULT_SOCKET)         # $HERMES_HOME default
    os.environ["OKO_TAIL_PROGRESS_SOCKET"] = os.path.join(home, "p.sock")

    ctx = FakeCtx()
    P.register(ctx)
    assert len(ctx.unloads) == 1, ctx.unloads
    path = os.path.join(home, "p.sock")
    assert os.path.exists(path), path
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600, oct(stat.S_IMODE(os.stat(path).st_mode))

    def ask(req):
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(5)
        c.connect(path)
        c.sendall(json.dumps(req).encode() + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = c.recv(65536)
            if not chunk:
                break
            buf += chunk
        c.close()
        return json.loads(buf.split(b"\n", 1)[0])

    fire(ctx, "on_stream_start", session_id="s1", turn_id="t1")
    fire(ctx, "on_stream_delta", session_id="s1", delta="hi", kind="reasoning")
    fire(ctx, "on_stream_start", session_id="s2")

    r = ask({"op": "progress"})
    assert r["type"] == "progress" and set(r["sessions"]) == {"s1", "s2"}, r
    assert r["sessions"]["s1"]["stage"] == "think" and r["sessions"]["s1"]["reasoning_chars"] == 2, r
    assert r["sessions"]["s1"]["thinking"] == "hi", r
    assert r["sessions"]["s2"]["stage"] == "wait", r
    assert ask({"op": "progress", "session": "s1"})["sessions"] == {"s1": r["sessions"]["s1"]}
    assert ask({"op": "progress", "session": "*"})["sessions"] == r["sessions"]
    assert ask({"op": "progress", "session": "nope"})["sessions"] == {}
    bad = ask({"op": "wipe"})
    assert bad["type"] == "error" and "unknown op" in bad["error"], bad
    assert ask({"op": "progress"})["sessions"]["s1"]["reasoning_chars"] == 2  # read-only: nothing changed

    # unload stops the listener and removes the file
    ctx.unloads[0]()
    assert not os.path.exists(path), path
    again = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        again.connect(path)
        raise AssertionError("socket still listening after unload")
    except OSError:
        pass
    finally:
        again.close()

    # hermes loads plugins in every agent process, so a SECOND one can hit an already-bound path. It must
    # fail to bind without arming an unload, or its stop() would delete the first process's live socket.
    # (register() keeps no handle on the server, so reach the instance through the unload it armed.)
    servers = []
    real_server = P.ProgressServer

    class SpyServer(real_server):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            servers.append(self)

    P.ProgressServer = SpyServer
    try:
        first = FakeCtx()
        P.register(first)
        assert len(first.unloads) == 1 and len(servers) == 1, (first.unloads, servers)
        assert ask({"op": "progress"})["type"] == "progress"
        second = FakeCtx()
        P.register(second)                                            # EADDRINUSE: hooks only, no listener
        assert second.unloads == [], second.unloads                   # nothing armed, so nothing can unlink
        assert servers[1].bound is False, servers[1].bound            # ... and it never bound anything
        assert os.path.exists(path), path
        assert ask({"op": "progress"})["type"] == "progress"          # the first process still answers
        servers[1].stop()                                             # a stop() on a never-bound server
        assert os.path.exists(path), path                             # is a no-op, not an unlink
        # the accept thread must end on the close, not on an AttributeError: stop() clears self._srv
        # before the loop notices, so the loop holds its own reference to the socket
        thread, errs = servers[0]._thread, []
        hook, threading.excepthook = threading.excepthook, lambda a: errs.append(a.exc_value)
        try:
            first.unloads[0]()
        finally:
            threading.excepthook = hook
        assert not os.path.exists(path), path
        thread.join(timeout=2.0)
        assert not thread.is_alive(), "the accept thread outlived stop()"
        assert errs == [], errs
    finally:
        P.ProgressServer = real_server

    # Configured caps include opt-out; counters keep counting in either mode.
    for cap in (5, 0, "invalid"):
        configured = FakeCtx({"think_cap": cap})
        P.register(configured)
        P.on_stream_start(session_id="cap")
        P.on_stream_delta(session_id="cap", kind="reasoning", delta="abcdefgh")
        expected = "defgh" if cap == 5 else "" if cap == 0 else "abcdefgh"
        assert P.snapshot("cap")["cap"]["thinking"] == expected
        assert P.snapshot("cap")["cap"]["reasoning_chars"] == 8
        for unload in configured.unloads:
            unload()

    # a leftover socket file from a dead process is cleared so a restart can bind it again
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(path)
    stale.close()                                                     # bound then closed: refuses connections
    ctx2 = FakeCtx()
    P.register(ctx2)
    assert ask({"op": "progress"})["type"] == "progress"
    ctx2.unloads[0]()
finally:
    os.environ.pop("OKO_TAIL_PROGRESS_SOCKET", None)

# a path too long for a unix socket falls back and leaves a pointer file, like the command socket
long_home = tempfile.mkdtemp()
long_path = os.path.join(long_home, "x" * 200, "p.sock")
os.makedirs(os.path.dirname(long_path))
srv = P.ProgressServer(long_path)
srv.start()
try:
    assert srv.bound_path != long_path and len(srv.bound_path.encode()) < 108, srv.bound_path
    assert stat.S_IMODE(os.stat(srv.bound_path).st_mode) == 0o600, oct(stat.S_IMODE(os.stat(srv.bound_path).st_mode))
    assert stat.S_IMODE(os.stat(long_path + ".path").st_mode) == 0o600, oct(stat.S_IMODE(os.stat(long_path + ".path").st_mode))
    with open(long_path + ".path") as f:
        assert f.read().strip() == srv.bound_path
    alt = srv.bound_path
finally:
    srv.stop()
assert not os.path.exists(long_path + ".path") and not os.path.exists(alt)   # both files this one made are gone
assert srv.bound_path is None

print("plugin selftest ok")

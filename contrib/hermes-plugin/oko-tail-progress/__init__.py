"""oko-tail-progress: report a hermes agent's live stage to oko's tail.

Registers the streaming observer hooks (``on_stream_start``, ``on_stream_delta``, ``on_stream_end``)
and keeps a tiny per-session record of what the model is doing right now: ``wait`` (a stream is open,
nothing back yet), ``think`` (reasoning text) or ``write`` (reply text), with a stage timer and
character counts. A capped reasoning tail is kept only in memory until the stream ends.

A read-only ``progress`` query answers on a local unix socket, one request per connection and one JSON
line each way, so a watcher can show a live stage for a turn it did not start::

    {"op":"progress","session":"<id>"}   session optional; absent or "*" = every session
    {"type":"progress","sessions":{"<id>":{"stage","stage_ts","reasoning_chars","text_chars","open",...}}}

Observer hooks run on hermes' own bounded queue, off the token path, so the callbacks here only update
a dict under a lock.

Enable it in ``~/.hermes/config.yaml`` (plugins are opt-in)::

    plugins:
      enabled:
        - oko-tail-progress
      stream_reasoning_deltas: true   # optional: without it the stage is wait/write, never think

Socket: ``OKO_TAIL_PROGRESS_SOCKET``, else this plugin's ``socket_path`` setting, else
``$HERMES_HOME/oko-tail-progress.sock``.
"""
from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import socket
import stat
import struct
import tempfile
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

ENV_SOCKET = "OKO_TAIL_PROGRESS_SOCKET"
DEFAULT_SOCKET = "oko-tail-progress.sock"
STAGES = ("wait", "think", "write")   # wait = stream open, nothing back yet; think = reasoning; write = reply
OPS = ("progress",)
# Linux refuses a unix path past 108 bytes with a bare "AF_UNIX path too long" (no errno set), so the
# length is checked before binding instead of trusting the exception.
MAX_SOCKET_PATH = 100
# Bound the record set: a long-lived gateway sees thousands of sessions, most of them long done.
MAX_SESSIONS = 200
# A record still marked open this long after its last event is a lost on_stream_end (a crash, a killed
# worker), not a live stream: report it closed so a watcher falls back to the transcript.
STALE_OPEN = 900
DEFAULT_THINK_CAP = 4000   # matches the viewer's THINK_CAP
THINK_CAP = DEFAULT_THINK_CAP

_LOCK = threading.Lock()
_SESSIONS: Dict[str, Dict[str, Any]] = {}


def _now() -> float:
    return time.time()


def _prune() -> None:
    """Drop the oldest records past MAX_SESSIONS. Caller holds the lock."""
    extra = len(_SESSIONS) - MAX_SESSIONS
    if extra <= 0:
        return
    for key in sorted(_SESSIONS, key=lambda k: _SESSIONS[k].get("updated_ts") or 0)[:extra]:
        _SESSIONS.pop(key, None)


def _advance(session_id: str, stage: str, *, think: int = 0, write: int = 0, reasoning: str = "") -> None:
    """Move ``session_id`` to ``stage`` and bump its counters. The stage timer only resets when the
    stage actually changes, so the page's "thinking for 12s" keeps counting across deltas.

    hermes gives every hook its own queue and worker thread, so the three callbacks for one stream can
    interleave: a delta can land after its own ``on_stream_end``. Such a record is closed and stays
    closed until the next start, or a late delta would flip a finished turn back to "writing"."""
    if not session_id:
        return
    now = _now()
    with _LOCK:
        st = _SESSIONS.get(session_id)
        if st is None:
            # No start seen (a dropped event, or a load mid-stream). Record it, so the turn is not lost.
            st = _SESSIONS[session_id] = {"stage_ts": now, "reasoning_chars": 0, "text_chars": 0, "open": True}
        if not st.get("open"):
            return
        if st.get("stage") != stage:
            st["stage"], st["stage_ts"] = stage, now
        st["reasoning_chars"] = int(st.get("reasoning_chars") or 0) + think
        st["thinking"] = (st.get("thinking", "") + reasoning[-THINK_CAP:])[-THINK_CAP:] if THINK_CAP else ""
        st["text_chars"] = int(st.get("text_chars") or 0) + write
        st["updated_ts"] = now
        _prune()


def on_stream_start(session_id: str = "", turn_id: str = "", model: str = "", **_ignored: Any) -> None:
    """A streaming model call began: the model is working, nothing back yet. Counters start fresh for
    the call; only the capped reasoning tail is kept, never reply text."""
    if not session_id:
        return
    now = _now()
    with _LOCK:
        _SESSIONS[session_id] = {
            "stage": "wait", "stage_ts": now, "reasoning_chars": 0, "text_chars": 0,
            "open": True, "updated_ts": now, "thinking": "",
            "turn_id": str(turn_id or ""), "model": str(model or "")}
        _prune()


def on_stream_delta(session_id: str = "", delta: str = "", kind: str = "text", **_ignored: Any) -> None:
    """One streamed delta: reasoning text moves the stage to think, reply text to write."""
    n = len(delta) if isinstance(delta, str) else 0
    if kind == "reasoning":
        _advance(session_id, "think", think=n, reasoning=delta if isinstance(delta, str) else "")
    elif kind == "text":
        _advance(session_id, "write", write=n)


def on_stream_end(session_id: str = "", **_ignored: Any) -> None:
    """The stream closed, so this session is no longer mid-call: the watcher falls back to the
    transcript until the next stream starts."""
    if not session_id:
        return
    with _LOCK:
        st = _SESSIONS.get(session_id)
        if st is not None:
            st["open"] = False
            st["thinking"] = ""
            st["updated_ts"] = _now()


def snapshot(session_id: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """A copy of the live records, all sessions or just one, for the ``progress`` query. ``"*"`` and an
    empty id mean every session, the same as omitting it. A record whose end never arrived is reported
    closed once it goes stale, so a watcher stops showing a stage for a stream that died."""
    if not session_id or session_id == "*":
        session_id = None
    now = _now()
    out = {}
    with _LOCK:
        keys = [session_id] if session_id else list(_SESSIONS)
        for key in keys:
            st = _SESSIONS.get(key)
            if st is None:
                continue
            if st.get("open") and now - (st.get("updated_ts") or 0) > STALE_OPEN:
                st["thinking"] = ""
            st = dict(st)
            if st.get("open") and now - (st.get("updated_ts") or 0) > STALE_OPEN:
                st["open"] = False
            out[key] = st
    return out


def _peer_uid(conn: socket.socket) -> Optional[int]:
    """The uid on the other end of a unix socket, or None where the platform can't tell us."""
    opt = getattr(socket, "SO_PEERCRED", None)
    if opt is None:
        return None
    try:
        return struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, opt, 12))[1]
    except OSError:
        return None


def _same_user(conn: socket.socket) -> bool:
    """Only our own uid may read the socket (mode 0600 is the first gate, this is the second)."""
    uid = _peer_uid(conn)
    return uid is None or uid == os.getuid()


def _clear_stale(path: str) -> None:
    """Drop a leftover socket file nobody is listening on, so a restart can bind it again."""
    try:
        if not stat.S_ISSOCK(os.stat(path).st_mode):
            return
    except OSError:
        return
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.5)
        probe.connect(path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(path)
    finally:
        probe.close()


class ProgressServer:
    """The read-only ``progress`` query: one request per connection, one JSON line each way.

    hermes loads plugins in every process that runs an agent (the gateway, the CLI, cron), so more than
    one of these can exist against one socket path. Only the process that bound it may unlink it: a
    second process that failed to bind must not delete the first one's live socket on unload."""

    def __init__(self, path: str):
        self.path = path              # where a client is told to look
        self.bound_path = path        # where we actually bound (differs when `path` is too long)
        self.pointer_path = None      # the ``<path>.path`` we wrote, when we bound elsewhere
        self.bound = False            # True once this instance has created files on disk
        self._srv: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Bind and serve. Raises OSError, leaving nothing behind, when the path is taken or unusable."""
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.bound_path = self._bind(srv)
        except OSError:
            srv.close()
            raise
        self.bound = True             # from here on, the files on disk are ours to clean up
        try:
            # The socket file exists from bind() on, so close the window where it is briefly readable:
            # 0600 is what the peer check is the second gate for.
            os.chmod(self.bound_path, 0o600)
            srv.listen(8)
        except OSError:
            srv.close()
            self._unlink_ours()
            raise
        self._srv = srv
        self._thread = threading.Thread(target=self._accept, args=(srv,), name="oko-tail-progress", daemon=True)
        self._thread.start()

    def _bind(self, srv: socket.socket) -> str:
        """Bind the configured path. A path too long for a unix socket falls back to a short one in the
        temp dir, with the real path written to ``<path>.path``, the way the command socket does it."""
        _clear_stale(self.path)
        if len(os.fsencode(self.path)) <= MAX_SOCKET_PATH:
            try:
                srv.bind(self.path)
                return self.path
            except OSError as exc:
                if exc.errno != errno.ENAMETOOLONG:   # in use or not permitted: the caller reports it
                    raise
        alt = os.path.join(tempfile.gettempdir(), f"oko-tail-progress-{os.getpid()}.sock")
        srv.bind(alt)
        try:
            _write_private(self.path + ".path", alt + "\n")
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(alt)                        # never leave a bound socket nobody will clean up
            raise
        self.pointer_path = self.path + ".path"
        return alt

    def _unlink_ours(self) -> None:
        """Remove only what THIS instance created: the socket it bound, and the pointer it wrote. Never
        touches the configured path when this instance never bound, which is the whole point: a second
        process that lost the bind race must not delete the winner's live socket."""
        if not self.bound:
            return
        self.bound = False
        if self.bound_path:
            with contextlib.suppress(OSError):
                os.unlink(self.bound_path)
        if self.pointer_path:
            with contextlib.suppress(OSError):
                os.unlink(self.pointer_path)
            self.pointer_path = None
        self.bound_path = None

    def _accept(self, srv: socket.socket) -> None:
        """Serve until the socket closes. `srv` is a local reference, not ``self._srv``: stop() clears
        that attribute before the accept loop has noticed the close, which would raise AttributeError."""
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _stop_accepting(self, srv: socket.socket) -> None:
        """Close the listener and wait for the accept loop to leave. close() alone does not wake a
        thread already blocked in accept() (it can sit there for the life of the process), so shutdown
        first: it makes the pending accept raise, which is what ends the loop."""
        with contextlib.suppress(OSError):
            srv.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            srv.close()

    def _serve(self, conn: socket.socket) -> None:
        try:
            if not _same_user(conn):
                return
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
            req = json.loads(buf.split(b"\n", 1)[0] or b"{}")
            conn.sendall(json.dumps(self.reply(req)).encode() + b"\n")
        except (OSError, ValueError):
            pass  # a client that hung up or sent junk: nothing to answer, and never a crash
        finally:
            with contextlib.suppress(OSError):
                conn.close()

    def reply(self, req: Any) -> Dict[str, Any]:
        """The answer to one request. Read-only: the only op is ``progress``."""
        op = req.get("op") if isinstance(req, dict) else None
        if op != "progress":
            return {"type": "error", "error": f"unknown op: {op!r}", "ops": list(OPS)}
        session = req.get("session")
        session = session if isinstance(session, str) and session and session != "*" else None
        return {"type": "progress", "sessions": snapshot(session)}

    def stop(self) -> None:
        """Stop accepting and remove the files this instance created. A no-op for a server that never
        bound, which is what keeps a failed second load from deleting the live socket."""
        srv, self._srv = self._srv, None
        if srv is not None:
            self._stop_accepting(srv)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._unlink_ours()


def _write_private(path: str, text: str) -> None:
    """Write a small file only this user can read. The umask makes the create 0600 rather than relying
    on a chmod after the fact, so the file is never briefly world-readable."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)


def _hermes_home() -> str:
    try:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())
    except Exception:
        return os.path.expanduser(os.environ.get("HERMES_HOME") or "~/.hermes")


def socket_path(ctx: Any = None) -> str:
    """Where to bind: ``OKO_TAIL_PROGRESS_SOCKET``, else this plugin's ``socket_path`` setting, else
    ``$HERMES_HOME/oko-tail-progress.sock``."""
    env = os.environ.get(ENV_SOCKET, "").strip()
    if env:
        return os.path.expanduser(env)
    if ctx is not None:
        try:
            cfg = ctx.get_config("socket_path")
        except Exception:
            cfg = None
        if isinstance(cfg, str) and cfg.strip():
            return os.path.expanduser(cfg.strip())
    return os.path.join(_hermes_home(), DEFAULT_SOCKET)


def register(ctx: Any) -> None:
    """hermes entry point: observe every stream, and answer `progress` while we are loaded."""
    global THINK_CAP
    try:
        cap = ctx.get_config("think_cap", DEFAULT_THINK_CAP)
        THINK_CAP = max(0, int(cap))
    except (TypeError, ValueError, OverflowError):
        THINK_CAP = DEFAULT_THINK_CAP
    ctx.register_hook("on_stream_start", on_stream_start)
    ctx.register_hook("on_stream_delta", on_stream_delta)
    ctx.register_hook("on_stream_end", on_stream_end)
    path = socket_path(ctx)
    server = ProgressServer(path)
    try:
        server.start()
    except OSError as exc:
        # Another process (a CLI, cron) already owns this path, or it isn't usable. The hooks still fill
        # the record set; only the query is missing, and that other process answers it. Never fail the
        # load for it, and never arm the unload: stop() must not unlink a socket this instance didn't bind.
        logger.info("oko-tail-progress: not listening on %s (%s); another process may own it", path, exc)
        return
    logger.info("oko-tail-progress: progress socket at %s", server.bound_path)
    ctx.on_unload(server.stop)

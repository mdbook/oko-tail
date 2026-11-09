"""context-steward: let a hermes agent see and manage its own context size.

Two jobs, both small:

- **Meter.** After every real model call (``post_api_request``) it records the prompt-token count the
  provider reported, per session, and writes it to a small JSON file (``$HERMES_HOME/state/context.json``
  by default). Anything outside the agent (a monitor, a status page, a script) can read how big the
  live context is without asking the agent. ``/new`` (``on_session_reset``) marks the new session as
  unmeasured until its first call.
- **Nudge.** Once a session's last count is at or above ``seam_tokens``, ``pre_llm_call`` adds one line
  to the next turn ("context is at 142k ..."). hermes injects it for that turn only; it never lands in
  the system prompt. Above ``ceiling_tokens`` the wording turns urgent. The agent decides when its
  seam is; the plugin only tells it the number.

The plugin never resets or compresses anything itself. What a reset looks like is up to you (a person
running ``/new``, a supervisor the agent messages, ...), so the nudge text is a template.

Enable it in ``~/.hermes/config.yaml`` (plugins are opt-in)::

    plugins:
      enabled:
        - context-steward
      entries:
        context-steward:
          settings:
            seam_tokens: 60000
            ceiling_tokens: 100000

The meter file::

    {"version": 1, "updated": 1761364800.0,
     "current": {"session_id": "...", "prompt_tokens": 61234, "ts": 1761364800.0, "platform": "...",
                 "model": "..."},
     "sessions": {"<session_id>": {...same fields...}}}

``current`` is the most recently measured (or reset) session, which may not be the one a reader cares
about: key on ``sessions[<id>]`` to act on a given session. ``prompt_tokens`` is ``null`` right after a
reset, until the new session's first call. ``/compress`` runs through auxiliary calls, which never update
the meter.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

STATE_VERSION = 1
DEFAULT_STATE_FILE = os.path.join("state", "context.json")
# Bound the record set: a long-lived gateway sees many sessions, most of them long done.
MAX_SESSIONS = 50
DEFAULT_NUDGE = ("[context-steward] This conversation's context is at {tokens_k}k tokens (nudge at {seam_k}k). "
                 "At your next clean seam (the current task finished, nothing in flight), save anything you need "
                 "to keep and suggest starting a fresh session with /new.")
DEFAULT_CEILING = ("[context-steward] This conversation's context is at {tokens_k}k tokens, past the {ceiling_k}k "
                   "ceiling. Finish or park the current task now, save anything you need to keep, and suggest "
                   "starting a fresh session with /new.")


def _hermes_home() -> str:
    try:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())
    except Exception:
        return os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")


def _int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _k(n: int) -> str:
    """142345 -> "142". Whole thousands read better in a one-line nudge than exact counts."""
    return str(int(round(n / 1000.0)))


class Steward:
    """The meter and the nudge for one hermes process. Hooks can run on different threads, so every
    read and write of the record set goes through one lock."""

    def __init__(self, settings: Optional[Dict[str, Any]] = None):
        s = settings or {}
        self.seam = _int(s.get("seam_tokens"))
        self.ceiling = _int(s.get("ceiling_tokens"))
        path = s.get("state_file") or DEFAULT_STATE_FILE
        self.path = os.path.join(_hermes_home(), os.path.expanduser(str(path)))  # absolute path wins
        platforms = s.get("platforms") or []
        self.platforms = {str(p) for p in platforms} if isinstance(platforms, (list, tuple, set)) else set()
        self.nudge_template = str(s.get("nudge_template") or DEFAULT_NUDGE)
        self.ceiling_template = str(s.get("ceiling_template") or DEFAULT_CEILING)
        self.lock = threading.Lock()
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.current: Optional[str] = None

    def _wanted(self, platform: Any) -> bool:
        return not self.platforms or str(platform or "") in self.platforms

    # -- meter ---------------------------------------------------------------------------------

    def record(self, session_id: str, prompt_tokens: Optional[int], platform: str = "", model: str = "",
               reset: bool = False) -> None:
        if not session_id or not self._wanted(platform):
            return
        rec = {"session_id": session_id, "prompt_tokens": prompt_tokens, "ts": time.time(),
               "platform": str(platform or ""), "model": str(model or "")}
        if reset:
            rec["reset"] = True
        with self.lock:
            self.sessions[session_id] = rec
            self.current = session_id
            for sid in sorted(self.sessions, key=lambda k: self.sessions[k]["ts"])[:-MAX_SESSIONS]:
                del self.sessions[sid]
            body = {"version": STATE_VERSION, "updated": rec["ts"], "current": dict(rec),
                    "sessions": {k: dict(v) for k, v in self.sessions.items()}}
            self._write(body)

    def _write(self, body: Dict[str, Any]) -> None:
        """Atomic replace, so a reader never sees half a file. Caller holds the lock. A failed write is
        logged and dropped: the meter must never break a turn."""
        d = os.path.dirname(self.path)
        try:
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".context-", dir=d)
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(body, f)
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception as exc:
            logger.warning("context-steward: could not write %s (%s)", self.path, exc)

    def tokens(self, session_id: str) -> Optional[int]:
        with self.lock:
            rec = self.sessions.get(session_id)
            return None if rec is None else rec.get("prompt_tokens")

    # -- nudge ---------------------------------------------------------------------------------

    def nudge(self, session_id: str, platform: str = "") -> Optional[str]:
        """The line for this turn, or None below the seam (or with the nudge off, or no count yet)."""
        if not self.seam or not session_id or not self._wanted(platform):
            return None
        n = self.tokens(session_id)
        if n is None or n < self.seam:
            return None
        urgent = bool(self.ceiling) and n >= self.ceiling
        fields = {"tokens": n, "tokens_k": _k(n), "seam_k": _k(self.seam), "ceiling_k": _k(self.ceiling)}
        try:
            return (self.ceiling_template if urgent else self.nudge_template).format(**fields)
        except (KeyError, IndexError, ValueError) as exc:
            logger.warning("context-steward: bad nudge template (%s); using the default", exc)
            return (DEFAULT_CEILING if urgent else DEFAULT_NUDGE).format(**fields)

    # -- hooks ---------------------------------------------------------------------------------

    def post_api_request(self, session_id: str = "", usage: Any = None, platform: str = "", model: str = "",
                         **_ignored: Any) -> None:
        if not isinstance(usage, dict):
            return
        n = usage.get("prompt_tokens")
        if isinstance(n, int) and n > 0:
            self.record(session_id, n, platform=platform, model=model)

    def pre_llm_call(self, session_id: str = "", platform: str = "", **_ignored: Any) -> Optional[Dict[str, str]]:
        line = self.nudge(session_id, platform)
        return {"context": line} if line else None

    def on_session_reset(self, new_session_id: str = "", session_id: str = "", old_session_id: str = "",
                         platform: str = "", **_ignored: Any) -> None:
        new = new_session_id or session_id
        if old_session_id:
            with self.lock:
                self.sessions.pop(old_session_id, None)
        self.record(new, None, platform=platform, reset=True)


def register(ctx: Any) -> None:
    """hermes entry point."""
    keys = ("seam_tokens", "ceiling_tokens", "state_file", "platforms", "nudge_template", "ceiling_template")
    steward = Steward({k: ctx.get_config(k) for k in keys})
    ctx.register_hook("post_api_request", steward.post_api_request)
    ctx.register_hook("pre_llm_call", steward.pre_llm_call)
    ctx.register_hook("on_session_reset", steward.on_session_reset)
    logger.info("context-steward: meter at %s, nudge at %s, ceiling at %s", steward.path,
                steward.seam or "off", steward.ceiling or "off")

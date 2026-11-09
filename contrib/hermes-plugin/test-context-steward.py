"""Plugin check for contrib/hermes-plugin/context-steward (stdlib only, no hermes needed).

Imports the plugin by path and drives its hooks directly: the meter file a reader sees, the nudge
only above the seam, the urgent wording above the ceiling, a reset, the platform filter, and that a
plugin with nothing configured never nudges.
"""
import importlib.util, json, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(HERE, "context-steward")


def load():
    spec = importlib.util.spec_from_file_location("context_steward", os.path.join(PKG, "__init__.py"),
                                                  submodule_search_locations=[PKG])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


P = load()
HERMES_DIR = tempfile.mkdtemp(prefix="context-steward-test-")
os.environ["HERMES_HOME"] = HERMES_DIR


class FakeCtx:
    """The slice of hermes' PluginContext the plugin uses: hooks and its own settings."""

    def __init__(self, config=None):
        self.hooks, self.config = {}, config or {}

    def register_hook(self, name, cb):
        self.hooks.setdefault(name, []).append(cb)

    def get_config(self, key, default=None):
        return self.config.get(key, default)


def fire(ctx, name, **kw):
    return [cb(**kw) for cb in ctx.hooks.get(name, [])]


def meter(path):
    with open(path) as f:
        return json.load(f)


def usage(n):
    """What hermes passes as post_api_request ``usage`` (the CanonicalUsage fields, flattened)."""
    return {"input_tokens": n - 100, "cache_read_tokens": 100, "cache_write_tokens": 0, "output_tokens": 50,
            "reasoning_tokens": 0, "request_count": 1, "prompt_tokens": n, "total_tokens": n + 50}


# ---- registration and defaults ---------------------------------------------------------------
ctx = FakeCtx({"seam_tokens": 60000, "ceiling_tokens": 100000})
P.register(ctx)
assert set(ctx.hooks) == {"post_api_request", "pre_llm_call", "on_session_reset"}, ctx.hooks
path = os.path.join(HERMES_DIR, "state", "context.json")
assert not os.path.exists(path), "nothing is written before the first call"

# ---- meter: every real call writes the count; a missing or empty usage writes nothing --------
fire(ctx, "post_api_request", session_id="s1", usage=usage(12000), platform="zulip", model="m1")
m = meter(path)
assert m["version"] == 1 and m["current"]["session_id"] == "s1", m
assert m["current"]["prompt_tokens"] == 12000 and m["current"]["platform"] == "zulip", m
assert m["current"]["model"] == "m1" and m["sessions"]["s1"]["prompt_tokens"] == 12000, m
assert oct(os.stat(path).st_mode & 0o777) == oct(0o600), "the meter is private to its owner"
fire(ctx, "post_api_request", session_id="s1", usage=None)
fire(ctx, "post_api_request", session_id="s1", usage={"prompt_tokens": 0})
fire(ctx, "post_api_request", session_id="", usage=usage(99999))
assert meter(path)["current"]["prompt_tokens"] == 12000, "no usage, zero or no session = no write"
assert not [f for f in os.listdir(os.path.dirname(path)) if f.startswith(".context-")], "no temp files left"

# ---- nudge: nothing below the seam, the seam line at or above it, urgent at the ceiling ------
assert fire(ctx, "pre_llm_call", session_id="s1") == [None]
fire(ctx, "post_api_request", session_id="s1", usage=usage(60000))
(r,) = fire(ctx, "pre_llm_call", session_id="s1")
assert r and "60k" in r["context"] and "nudge at 60k" in r["context"] and "ceiling" not in r["context"], r
fire(ctx, "post_api_request", session_id="s1", usage=usage(142345))
(r,) = fire(ctx, "pre_llm_call", session_id="s1")
assert "142k" in r["context"] and "past the 100k ceiling" in r["context"], r
assert fire(ctx, "pre_llm_call", session_id="other") == [None], "a session with no count is never nudged"

# ---- a second session becomes current; the first keeps its own count -------------------------
fire(ctx, "post_api_request", session_id="s2", usage=usage(5000), platform="cron")
m = meter(path)
assert m["current"]["session_id"] == "s2" and m["sessions"]["s1"]["prompt_tokens"] == 142345, m
assert fire(ctx, "pre_llm_call", session_id="s1")[0] is not None, "the nudge is per session"

# ---- /new: the new session is current and unmeasured, the old one is gone, no nudge ----------
fire(ctx, "on_session_reset", session_id="s3", reason="new_session", platform="zulip",
     old_session_id="s1", new_session_id="s3")
m = meter(path)
assert m["current"] == {**m["current"], "session_id": "s3", "prompt_tokens": None, "reset": True}, m
assert "s1" not in m["sessions"] and "s2" in m["sessions"], m
assert fire(ctx, "pre_llm_call", session_id="s3") == [None]
fire(ctx, "post_api_request", session_id="s3", usage=usage(9000), platform="zulip")
assert "reset" not in meter(path)["current"], "the first real call clears the reset mark"

# ---- templates: fleet wording via config; a broken template falls back, never raises ---------
ctx2 = FakeCtx({"seam_tokens": 1000, "state_file": os.path.join(HERMES_DIR, "two.json"),
                "nudge_template": "ctx {tokens_k}k/{seam_k}k: commit your notes, then ask for a clear"})
P.register(ctx2)
fire(ctx2, "post_api_request", session_id="a", usage=usage(4321))
assert fire(ctx2, "pre_llm_call", session_id="a")[0]["context"] == "ctx 4k/1k: commit your notes, then ask for a clear"
assert os.path.exists(os.path.join(HERMES_DIR, "two.json")), "an absolute state_file is used as is"
ctx3 = FakeCtx({"seam_tokens": 1000, "state_file": "three.json", "nudge_template": "bad {nope}"})
P.register(ctx3)
fire(ctx3, "post_api_request", session_id="a", usage=usage(4321))
assert "4k tokens" in fire(ctx3, "pre_llm_call", session_id="a")[0]["context"], "bad template -> default"
assert os.path.exists(os.path.join(HERMES_DIR, "three.json")), "a relative state_file lives under HERMES_HOME"

# ---- platform filter: other platforms are neither metered nor nudged ------------------------
ctx4 = FakeCtx({"seam_tokens": 1000, "platforms": ["zulip"], "state_file": "four.json"})
P.register(ctx4)
fire(ctx4, "post_api_request", session_id="c", usage=usage(5000), platform="cron")
assert not os.path.exists(os.path.join(HERMES_DIR, "four.json"))
assert fire(ctx4, "pre_llm_call", session_id="c", platform="cron") == [None]
fire(ctx4, "post_api_request", session_id="z", usage=usage(5000), platform="zulip")
assert meter(os.path.join(HERMES_DIR, "four.json"))["current"]["session_id"] == "z"

# ---- nothing configured: the meter runs, the nudge never fires --------------------------------
ctx5 = FakeCtx({"state_file": "five.json"})
P.register(ctx5)
fire(ctx5, "post_api_request", session_id="q", usage=usage(900000))
assert meter(os.path.join(HERMES_DIR, "five.json"))["current"]["prompt_tokens"] == 900000
assert fire(ctx5, "pre_llm_call", session_id="q") == [None]

# ---- bounded record set ----------------------------------------------------------------------
s = P.Steward({"state_file": "six.json"})
for i in range(P.MAX_SESSIONS + 10):
    s.record("x%d" % i, 100 + i)
m = meter(os.path.join(HERMES_DIR, "six.json"))
assert len(m["sessions"]) == P.MAX_SESSIONS and m["current"]["session_id"] == "x%d" % (P.MAX_SESSIONS + 9), m

# ---- an unwritable meter is logged, never raised into the turn --------------------------------
blocker = os.path.join(HERMES_DIR, "blocker")
open(blocker, "w").close()
s = P.Steward({"state_file": os.path.join(blocker, "context.json")})
s.post_api_request(session_id="w", usage=usage(70000))
assert s.tokens("w") == 70000, "the in-memory count still drives the nudge"

print("context-steward: all checks passed")

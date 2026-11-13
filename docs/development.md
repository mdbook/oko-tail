# Development

Run `python -m pytest` for the server/plugin wrapper checks and the README setup checks (requires pytest).
The setup checks validate guide links and the compose defaults without Docker or a running hermes agent;
they do not replace a container smoke test. The standalone checks below cover the server and browser.

```bash
python3 oko-tail.py --selftest     # server tests against a throwaway database and a stub gateway
python3 test-oko-tail-md.py        # markdown and context-response parsing (needs node)
python3 test-oko-tail-ui.py        # drives the real page in headless Chromium (skipped without playwright)
python3 contrib/hermes-plugin/test-plugin.py   # the progress plugin, against a real unix socket (no hermes needed)
python3 contrib/hermes-plugin/test-context-steward.py   # the context plugin (no hermes needed)
UI_SHOT=shot.png python3 test-oko-tail-ui.py   # the same, saving a screenshot
python3 docs/shots.py              # regenerates the README screenshots from a demo session
python3 docs/shots.py --gif        # re-records docs/demo.gif (also needs ffmpeg)
```

| File | Contents |
|---|---|
| `oko-tail.py` | the server: database reads, JSON and SSE endpoints, auth, and the gateway proxy (streamed turns, stop and steer, slash commands) |
| `web/` | the page: `index.html`, `app.css`, `app.js`, plus the logo and favicon SVGs, served from a fixed allowlist and read on each request |
| `contrib/hermes-plugin/` | the optional hermes plugins, [progress](plugins.md#live-progress-for-turns-you-didnt-send) and [context](plugins.md#keeping-context-in-check), and their tests |
| `test-*.py` | the page tests above |
| `docs/shots.py` | the screenshot generator |
| `Dockerfile`, `.dockerignore` | the image: `python:3.12-slim` plus `oko-tail.py` and `web/`, nothing else (the ignore file denies everything by default) |
| `.gitlab-ci.yml`, `.github/workflows/` | tests, then image builds (see [Releases and images](running.md#releases-and-images)) |
| `contrib/oko-tail.service` | the systemd user unit |

### Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/rows?after=N&session=ID`, `/events` (SSE) | rows plus turn state (`waiting`, `phase`, `since`), `compacting`, `live` (the streamed overlay of a turn sent from the page, with its `stage`: wait, think, write or tool), `progress` (the same stages for a turn sent elsewhere, from the [progress plugin](plugins.md#live-progress-for-turns-you-didnt-send): `stage`, `stage_ts`, `reasoning_chars`, `text_chars`; null without the plugin), `gateway` (up or down) and `stats`. A user row says who sent it and from where: `sender` (your `OKO_TAIL_OWNER` label, else "you"; or another agent's name), `via` (`Zulip`, `page`, `TUI`: a Zulip message id means Zulip, none means the page) and `peer` (true for anyone but you) |
| GET | `/earlier?before=N` | an older page of rows |
| GET | `/img?id=N&n=I` | image `I` of row `N`, the ones a row lists in `images` (a call's in its own `images`): an image sent in (inline in the turn, or the cache path hermes notes in the text), an image the reply itself attaches (a `MEDIA:<path>` line, hidden from the text), or one a tool read or captured. Served only if the bytes really are png/jpeg/gif/webp; 400 on a malformed `id`/`n`. A file path is revalidated by ETag (mtime + size), an inline image cached for a day; a host path under `OKO_TAIL_HOST_HOME` is remapped onto `$HERMES_HOME` (see [Docker](running.md#docker)) |
| GET | `/ctx?session=ID` | what's in the context: sizes by kind and the largest active rows since the last compaction (estimated at 4 characters per token) |
| GET | `/tools?session=ID` | tool health for the last 7 days: per tool, calls, failures (an `error`, a non-zero `exit_code` or `success: false`), median, slowest and total time from call to result |
| GET | `/queue?session=ID` | the agent's own queue for the main session, from the socket: messages hermes queued behind a running turn or a compaction and DMs held for idle (`where`, `from`, `peer`, `text`, `ts`), plus `busy` and `session_id`. `socket: false` means unknown, not empty. Queue absence does not acknowledge application. None of it is in `state.db` until its turn starts |
| GET | `/cmds` | the slash menu: `commands` (the agent's own list when the socket answers, with `socket: true`) and `skills` |
| POST | `/say` `{session,message,images?}` | returns 202; the turn streams from the gateway's `/chat/stream` on a server thread, so reloading the page never interrupts it |
| POST | `/stop` `{session}`, `/steer` `{session,message}` | stop or steer a turn this page started, by its run ID |
| POST | `/cmd` `{session,name,args}` | any slash command through the socket (main session only), including `/stop` and `/steer` for turns started anywhere; otherwise the few the REST API supports |

When a login is configured, every request passes it first, including static files and `/events`.
Every POST also passes the same-origin check, must name an existing session, and reads the API key
in-process.

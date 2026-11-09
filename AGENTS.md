# AGENTS.md

Guidance for coding agents (Claude Code, Codex, and others) working in this repo. Read this first,
then `README.md` for the user-facing documentation.

## What this project is

oko's tail is a live web view of a hermes agent. It shows each step of a turn as it happens
(reasoning, tool calls and results, final reply) and lets you talk to the agent from the same page.
It is deliberately small: one Python file, a few static web files, standard library only.

## Repo layout

| Path | What lives there |
|---|---|
| `oko-tail.py` | the whole server: database reads, JSON and SSE endpoints, auth, the gateway proxy, and the terminal view. Python 3.9+ standard library only. |
| `web/` | the page: `index.html`, `app.js`, `app.css`, plus the logo and favicon SVGs. Served from a fixed allowlist and read on each request. |
| `contrib/hermes-plugin/oko-tail-progress/` | the optional plugin that reports the live stage of a turn sent from elsewhere. |
| `contrib/hermes-plugin/context-steward/` | the optional plugin that meters context size and nudges the agent near a seam. |
| `contrib/hermes-plugin/test-plugin.py`, `test-context-steward.py` | stdlib-only tests for those two plugins. |
| `contrib/oko-tail.service` | the systemd user unit. |
| `test-oko-tail-md.py` | checks the page's markdown renderer (needs node). |
| `test-oko-tail-ui.py` | drives the real page in headless Chromium (skipped cleanly without playwright). |
| `test_progress.py` | pytest wrapper that runs the standalone checks; see Tests. |
| `docs/` | banner and screenshot images, plus `docs/shots.py` which regenerates the screenshots. |
| `Dockerfile`, `.dockerignore` | the image: `python:3.12-slim` plus `oko-tail.py` and `web/`, nothing else. |
| `.github/workflows/`, `.gitlab-ci.yml` | tests, then image builds. |

## Setup and run

There is no build step and nothing to install. Runtime is the Python standard library only, so no
`pip install` is needed for the app itself.

```bash
python3 oko-tail.py --check            # show resolved config and what works, then exit
python3 oko-tail.py --serve 9120       # then open http://127.0.0.1:9120
python3 oko-tail.py                    # or follow the default session in the terminal
```

`--check` is the first thing to run when something looks wrong: it lists every setting, where it came
from, and whether it works. Configuration is environment-only; see the Configuration table in
`README.md`.

## Tests

CI (`.github/workflows/test.yml`, Python 3.12) runs exactly these four commands, in order:

```bash
python3 oko-tail.py --selftest                          # server tests against a throwaway database and a stub gateway
python3 test-oko-tail-md.py                             # the page's markdown renderer; needs node on PATH
python3 contrib/hermes-plugin/test-plugin.py            # the progress plugin, over a real unix socket
python3 contrib/hermes-plugin/test-context-steward.py   # the context plugin
```

Two more tests exist and are not part of CI:

- `test-oko-tail-ui.py` drives the real page in headless Chromium. It needs the `playwright` Python
  package with its chromium browser, and exits 0 with "ui test skipped (no playwright)" when that is
  missing, so it is safe to run anywhere: `python3 test-oko-tail-ui.py`. Set `UI_SHOT=shot.png` to
  also save a screenshot.
- `test_progress.py` exposes the two standalone checks above to pytest (it needs `pytest`, which the
  app itself never needs). Run it with `python -m pytest` (or `python -m pytest test_progress.py`).

Run the four CI commands before you commit a change to `oko-tail.py`, the plugins, or the markdown
renderer. The `.gitlab-ci.yml` test job installs `nodejs` first because the markdown test needs it;
GitHub's runners have node already.

## Conventions

- **Language and dependencies.** Python 3.9+ standard library only for the app and the plugins. No
  new runtime dependencies. The page is dependency-free plain JavaScript and CSS. Tests use the
  standard library too, except the optional playwright and pytest extras noted above.
- **Style.** Match the surrounding code: terse, comment only where a reader would otherwise stumble,
  short helper names, no frameworks. Keep `oko-tail.py` a single file; that is a feature, not an
  accident.
- **OSS rule, and a hard one.** This is public code. Keep it generic: no homelab hostnames, IPs, bot
  names, usernames, personal paths, or personal data in code, comments, tests, docs, or example
  values. Site-specific detail goes in config or environment (`HERMES_HOME`, the `OKO_TAIL_*`
  variables, the gateway URL) or in a reader's own `.env`. Use `example.com` and invented names in
  examples and tests, never a real one.
- **Secrets.** `API_SERVER_KEY` is read from the agent's `.env` in-process on each call and is never
  copied, logged, or sent to the browser. Never commit a secret, always drive the "set or missing"
  reporting in `--check` rather than printing a value.
- **Security defaults.** Listen on loopback with no login by default. A non-loopback `OKO_TAIL_HOST`
  requires a login. Every POST is JSON and passes the same-origin check. Preserve these when you
  touch the request path.

## Adding a feature or a test

- **Feature.** If it needs new configuration, add an `OKO_TAIL_*` variable with a sane default in the
  config block at the top of `oko-tail.py`, wire it into `--check`, and add a row to the Configuration
  table in `README.md`. If it changes an endpoint's payload, update the Endpoints table and the
  `web/app.js` consumer.
- **A tool-result renderer.** Some tools get their own view, keyed by tool name in the `RESULTS`
  registry in `web/app.js`. Map a tool name to a function returning `{node, plain, used}`, or `null`
  to fall back to the generic view. A result shape the renderer does not recognize must still fall
  back cleanly.
- **A test.** Prefer extending the existing `--selftest` in `oko-tail.py` for server behavior, the
  plugin `test-*.py` for plugin behavior, and `test-oko-tail-md.py` for the markdown renderer. Keep
  tests stdlib-only and independent of any real agent or homelab.

## Gotchas

- **`test-oko-tail-md.py` needs node.** It runs the page's own renderer under `node`; a missing node
  is a real failure, not a skip. The UI test is the one that skips cleanly.
- **The UI test skips on a missing playwright**, and prints a message; a skip is not a pass, so read
  the output.
- **The page is served from a fixed allowlist** (`STATIC` in `oko-tail.py`) with no path joins from
  the URL. A new static file has to be added to that map and to the `web/` allowlist together, or it
  will 404.
- **`web/` is read per request**, so a page edit is live on reload with no restart. `oko-tail.py`
  changes need a restart.
- **The database is opened `?mode=ro`.** The one write path is the gateway's own session API. If you
  find yourself writing to `state.db`, stop: that is the wrong layer.
- **CI runs the four commands above**, not pytest. A change that only passes pytest but breaks
  `--selftest` or the plugin tests will fail CI.

## What not to touch

- Do not add runtime dependencies or a build step to the app or the plugins.
- Do not edit `.github/workflows/`, `.gitlab-ci.yml`, the `Dockerfile`, or `.dockerignore` unless the
  change is specifically about them.
- Do not put secrets, real hostnames, IPs, or personal data anywhere in the tree.
- Do not split `oko-tail.py` into modules, and do not introduce a web framework.
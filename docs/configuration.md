# Configuration

Everything is configured through environment variables, and the defaults match a standard
single-profile hermes install.

| Variable | Default | Purpose |
|---|---|---|
| `HERMES_HOME` | `~/.hermes` | where `state.db`, `.env` and `skills/` live (same meaning as in hermes) |
| `OKO_TAIL_DB` | `$HERMES_HOME/state.db` | the agent's SQLite database, if it lives elsewhere |
| `OKO_TAIL_HOST_HOME` | none | the agent's hermes home *as its own host sees it*, when that differs from `HERMES_HOME` here (e.g. a custom container mount at `/hermes`). An image path under it is remapped onto `$HERMES_HOME`; a path outside it is never served. Not needed with the supplied compose file. See [Docker](running.md#docker) |
| `OKO_TAIL_ENV` | `$HERMES_HOME/.env` | the file to read `API_SERVER_KEY` from |
| `OKO_TAIL_SKILLS` | `$HERMES_HOME/skills` | skills directory for the `/` menu while the gateway is down |
| `OKO_TAIL_SOCKET` | `$HERMES_HOME/oko-tail.sock` | the command socket's path |
| `OKO_TAIL_PROGRESS_SOCKET` | `$HERMES_HOME/oko-tail-progress.sock` | the progress socket's path (the [progress plugin](plugins.md#live-progress-for-turns-you-didnt-send)) |
| `HERMES_GATEWAY` | `http://127.0.0.1:8642` | the gateway's API server |
| `API_SERVER_KEY` | read from `$HERMES_HOME/.env` | overrides the key file, e.g. when the agent's `.env` isn't readable from here |
| `OKO_TAIL_AGENT` | `hermes` | the agent's display name on the page |
| `OKO_TAIL_BRAND` | none | path to your agent's logo (`.svg`, `.png`, `.jpg`, `.webp` or `.gif`), shown small in the header. Served at `/brand`, read per request |
| `OKO_TAIL_PEERS` | none | other agents' tails for the session list, `name=url` comma-separated (`staging=/staging/,work=https://agent.example.com`). Run one oko-tail per agent (each with its own `HERMES_HOME` and port) and list the others here |
| `OKO_TAIL_MAIN_ROUTE` | none | a gateway routing key, such as your own DM's. The page opens on the session it routes to and follows it across `/new`. |
| `OKO_TAIL_MAIN_SESSION` | none | fallback session ID when the route doesn't exist yet |
| `OKO_TAIL_OWNER` | none | `user_id=Label`: show your own messages as *Label* instead of your platform ID. Every message you sent is labelled with it and where it came from (*Taylor · chat*, *Taylor · page*) |
| `OKO_TAIL_HOST` | `127.0.0.1` | the address to listen on; anything other than loopback requires a [login](security.md) |
| `OKO_TAIL_HOSTNAMES` | loopback | other names the page is reached by (comma-separated), e.g. your proxy's `agent.example.com`; `*` allows any |
| `OKO_TAIL_USER`, `OKO_TAIL_PASSWORD` | none | turn on HTTP basic auth |
| `OKO_TAIL_PASSWORD_FILE` | none | read the password from a file instead (Docker secrets, systemd credentials) |
| `OKO_TAIL_AUTH_HEADER` | none | turn on forward auth: the header your auth proxy sets, e.g. `Remote-User` |
| `OKO_TAIL_PROXY_SECRET_FILE` | none | also require a shared proxy secret for forward auth; inert without `OKO_TAIL_AUTH_HEADER`; file bytes read per request, trailing CR/LF stripped; no leading spaces/tabs (HTTP strips them); missing, unreadable or empty fails closed |
| `OKO_TAIL_PROXY_SECRET_HEADER` | `X-Oko-Tail-Proxy-Secret` | header carrying the secret when `OKO_TAIL_PROXY_SECRET_FILE` is set; compared byte-for-byte in constant time |
| `OKO_TAIL_TRUSTED_PROXIES` | `127.0.0.0/8,::1` | addresses allowed to set that header (comma-separated IPs or CIDRs) |

Without a main route or session, the page opens on the most recently active session. To find your
DM's routing key, run `sqlite3 "$HERMES_HOME/state.db" 'select distinct session_key from gateway_routing'`.
The flags `--db`, `--env`, `--gateway`, `--session` and `--interval` override settings for a single
run (see `--help`).

To see exactly what oko's tail picked up, run `python3 oko-tail.py --check`. It lists every setting,
where it came from and whether it works. Secrets are only ever shown as *set* or *missing*:

```
ok    db       /home/me/.hermes/state.db  [$HERMES_HOME]  7 sessions
ok    api key  set  [/home/me/.hermes/.env]
ok    gateway  http://127.0.0.1:8642  [default]  up
warn  socket   /home/me/.hermes/oko-tail.sock  [$HERMES_HOME]  absent: only a few commands work, over the gateway's REST API
warn  progress /home/me/.hermes/oko-tail-progress.sock  [$HERMES_HOME]  absent: a turn started elsewhere shows the database's phase, not live thinking/writing
ok    auth     none  [default]  loopback only: put an authenticating proxy in front to go off this box
ok    listen   127.0.0.1  [default]
```

It exits non-zero if the database can't be read or the login settings are invalid.

# Running it as a service

## systemd

[`contrib/oko-tail.service`](../contrib/oko-tail.service) is a ready-to-use user unit:

```bash
mkdir -p ~/.config/systemd/user
cp contrib/oko-tail.service ~/.config/systemd/user/
$EDITOR ~/.config/systemd/user/oko-tail.service    # set the checkout path and your environment variables
systemctl --user daemon-reload && systemctl --user enable --now oko-tail
```

The unit runs straight from the checkout, so updating is `git pull` followed by
`systemctl --user restart oko-tail`. Changes under `web/` take effect on a page reload, without a restart.

## Docker

Start with the checked-in [compose.yaml](../compose.yaml) and the [quick start](../README.md#quick-start-docker).
The image is `ghcr.io/mdbook/oko-tail:release`. It contains only `oko-tail.py` and `web/`
on top of `python:3.12-slim`. No local build or Python install is needed.

This recipe is for Docker Engine on Linux, on the same host as hermes. Host networking lets the
container reach the gateway on `127.0.0.1:8642` and keeps the web server on `127.0.0.1:9120`.
It is packaging, not isolation. The container runs as hermes' uid:gid so it can read the key file
and connect to same-user plugin sockets. On macOS or Windows, use the Python setup in the same
environment as hermes rather than assuming Docker Desktop has the same socket and host-network behavior.

The compose file has only two lines to edit: hermes' absolute home path and its numeric uid:gid.
A YAML anchor reuses that path for the bind source, bind target and `HERMES_HOME`. Mounting the
directory at its original path also keeps image paths recorded by hermes readable without
`OKO_TAIL_HOST_HOME`. The whole directory is mounted read-only: SQLite needs `state.db-wal` and
`state.db-shm` beside the database, and plugins recreate sockets on restart. A missing source
directory is an error, not an empty directory Docker silently creates.

If you write your own container configuration and mount at a different path, set
`HERMES_HOME` to the container path and `OKO_TAIL_HOST_HOME` to the path the agent records.
Images under that host path are remapped onto `HERMES_HOME`; outside paths are never served.
Inline data-URL images need no remapping. See [Configuration](configuration.md).

### Check, update and stop

```bash
docker compose exec oko-tail python3 /app/oko-tail.py --check
docker compose pull
docker compose up -d
```

The check runs with the service's user, mounts and environment. If the service cannot start,
use `docker compose run --rm oko-tail --check` to diagnose it.
Read the output, not just the exit code: missing gateway credentials are warnings, not failures.
Stop with `docker compose down`. The container keeps no transcript state: messages live in hermes'
database, and queued messages and drafts live in your browser.

For remote access, keep the loopback default and follow [Security](security.md).
For basic auth, mount a password file or Docker secret and point `OKO_TAIL_PASSWORD_FILE` at it;
do not put the password in the compose file.

To build the image yourself: `docker build -t oko-tail .`

### Releases and images

Two CI pipelines build the same image after the tests pass, one for each forge:

| | Push to `main` | Tag `vX.Y.Z` | Everything else |
|---|---|---|---|
| [`.github/workflows/docker.yml`](../.github/workflows/docker.yml) (GitHub Actions, amd64 + arm64) | `ghcr.io/<owner>/oko-tail:<sha>` | `:vX.Y.Z` and `:release` | pull requests build but never push |
| [`.gitlab-ci.yml`](../.gitlab-ci.yml) (GitLab, kaniko, amd64, no Docker-in-Docker) | `$CI_REGISTRY_IMAGE:<sha>` | `:vX.Y.Z` and `:release` | branches run the tests only |

There's deliberately no `:latest`. `:release` moves only when a version is tagged, so merging to
`main` never changes what a Watchtower-managed machine is running.

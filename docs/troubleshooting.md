# Troubleshooting

Start with `docker compose exec oko-tail python3 /app/oko-tail.py --check`, or
`python3 oko-tail.py --check` for the Python setup. It lists every input,
where it came from and whether it works.

- **The input box says the agent is offline.** The key is missing or the gateway isn't answering at
  `HERMES_GATEWAY`. Confirm `API_SERVER_ENABLED=true` and `API_SERVER_KEY` are in the agent's `.env`,
  then restart the gateway.
- **Slash commands say they need the socket, or Stop only works on turns sent from the page.** There's
  no command socket, or you're viewing a session other than the main one. This is expected without
  the [unpublished command-socket plugin](plugins.md#the-command-socket).
- **In Docker, the database won't open or the transcript stops updating.** Mount the whole
  `$HERMES_HOME` directory rather than just `state.db` (SQLite needs the `-wal` file beside it), and
  run the container as hermes' user.
- **An attached image shows as a broken/empty slot.** The agent recorded the file's path *as its host
  saw it*, which doesn't exist inside the container. The supplied compose file mounts the home at its original path. If you changed the mount target,
  set `OKO_TAIL_HOST_HOME` to the original host directory. See [Docker](running.md#docker).
- **Every request returns 401 behind your proxy.** With forward auth, check that the proxy sets
  `OKO_TAIL_AUTH_HEADER` and that its address is in `OKO_TAIL_TRUSTED_PROXIES`. A proxy in a Docker
  bridge network connects from its container address, not loopback; trust that one address (`/32`)
  rather than the bridge range, since any container on it could otherwise claim to be logged in.
- **Every request returns 421 "unknown host name".** Add the name in your browser's address bar to
  `OKO_TAIL_HOSTNAMES`; behind a proxy, that's its public name.
- **The page opens on the wrong session.** Set `OKO_TAIL_MAIN_ROUTE` to your DM's routing key.
- **Live steps arrive in bursts behind a proxy.** The proxy is buffering `/events`; turn buffering off.
- **No desktop notifications.** The page asks for permission once, on your first message. If you
  dismissed it, allow notifications for the site in your browser settings. Notifications also
  require HTTPS or localhost.

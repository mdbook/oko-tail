# Security

oko's tail shows your agent's full transcripts and can make it act, so treat access to it like access
to the agent itself. By default it listens on `127.0.0.1` only and has no login, which is fine on a
single-user machine. To use it from anywhere else, turn on one of the two built-in logins:

- **Forward auth**, if you already run an identity provider in
  front of your services. Set `OKO_TAIL_AUTH_HEADER` to the header your proxy adds after a successful
  login. oko's tail accepts that header only from `OKO_TAIL_TRUSTED_PROXIES`, and your proxy must
  *replace* any copy sent by the browser (not append to it), or anyone could claim to be logged in.
  Trust only your proxy's own address. The loopback default fits a proxy on the same machine, but
  it also trusts every other local process, and a browser on that machine can be reached through DNS
  rebinding; on a shared or desktop machine, prefer basic auth or a proxy on another host.
- **HTTP basic auth**, for everything else. Set `OKO_TAIL_USER` and `OKO_TAIL_PASSWORD` (or
  `OKO_TAIL_PASSWORD_FILE`). Basic auth sends the password with every request, so serve it over
  HTTPS (a reverse proxy or `tailscale serve`) whenever it leaves the machine.

You can turn on both; a request that passes either one gets in. With a login configured, you can set
`OKO_TAIL_HOST` to listen beyond loopback; without one, oko's tail refuses to start on a non-loopback
address. Every POST must be JSON and pass an `Origin` check against `Host`, and the page refuses to be
framed, so other websites can't send requests through your browser.

oko's tail also answers only to the names it knows: loopback, `OKO_TAIL_HOST`, and whatever you list in
`OKO_TAIL_HOSTNAMES`. Any other `Host` gets a 421, which stops DNS rebinding (a site that points its
own name at 127.0.0.1 so your browser treats oko's tail as part of that site). Behind a proxy that
passes `Host` through, as Caddy does, add the public name: `OKO_TAIL_HOSTNAMES=agent.example.com`.

For a proxy that handles HTTPS, keep oko's tail on loopback and configure its basic auth.
Set `OKO_TAIL_USER` and `OKO_TAIL_PASSWORD_FILE` in the server environment, and
`OKO_TAIL_HOSTNAMES=agent.example.com`. The password file must be readable by hermes' user.
A minimal Caddy route is:

```caddyfile
agent.example.com {
    reverse_proxy 127.0.0.1:9120
}
```

For forward auth instead, use your identity provider's proxy integration and set, for example,
`OKO_TAIL_AUTH_HEADER=Remote-User`. That header is not authentication by itself: the proxy
must authenticate the user, strip client copies, and set it only after login. Set
`OKO_TAIL_TRUSTED_PROXIES` to that proxy's address, not a whole container network.

## Behind another dashboard

For a server-side dashboard proxy, add `OKO_TAIL_PROXY_SECRET_FILE` to require proof from the
proxy as well as its forward-auth identity header and trusted address. The default secret
header is `X-Oko-Tail-Proxy-Secret`; override it with `OKO_TAIL_PROXY_SECRET_HEADER` if needed.
Keep the file private and inject
its contents only from the proxy, never the browser. The proxy must strip client copies of
both headers before setting them after authentication. The file is read on each request and
trailing CR/LF bytes are stripped; all other bytes must match exactly. Do not use leading spaces
or tabs: HTTP header parsing strips them, so such a secret cannot match the file.
Rotation takes effect on the next request; already-open `/events` streams survive until reconnect.
A missing, unreadable or empty file denies forward auth, and `--check` shows only `set`,
`missing` or `off` (not configured), never the secret or its path. It warns if the file is
configured without `OKO_TAIL_AUTH_HEADER`, since the secret is then inert.
Basic auth remains independent. This adds no framing exception, Host exception or
cross-origin POST access.

Whichever proxy you use, it needs to pass `Origin` through and keep `Host` set to the public name
(Caddy does both by default). `/events` is a server-sent event stream, so turn off response
buffering if your proxy buffers (for nginx, `proxy_buffering off`).

### Setting it up

Use a server-side proxy, not a browser-held secret. Create a file containing a long random
secret, readable only by the user running oko-tail (for example, mode `0600` at
`/path/to/proxy-secret`). Configure the proxy to authenticate dashboard users, strip client
copies of both headers, and set `Remote-User` plus `X-Oko-Tail-Proxy-Secret` on authenticated
requests. Inject the same secret from the proxy's protected config, never browser JavaScript.
Set these variables for oko-tail and restart it:

```bash
export OKO_TAIL_AUTH_HEADER=Remote-User
export OKO_TAIL_PROXY_SECRET_FILE=/path/to/proxy-secret
export OKO_TAIL_TRUSTED_PROXIES=127.0.0.1
export OKO_TAIL_HOSTNAMES=agent.example.com
python3 oko-tail.py --check
python3 oko-tail.py --serve 9120
```

Use your proxy's actual address and public hostname. With Docker, pass the variables to the
container and mount the secret file read-only at the configured path. To rename the secret
header, set `OKO_TAIL_PROXY_SECRET_HEADER` on oko-tail and change the proxy to match.
`--check` reports the secret as `set`, `missing` or `off` (not configured), never its value,
and warns if the file is configured without `OKO_TAIL_AUTH_HEADER`, leaving the secret inert.
Do not use leading spaces or tabs in the secret: HTTP header parsing strips them, so they cannot
match the file. Only trailing CR/LF bytes are stripped from the file; other bytes must match exactly.
Missing, unreadable or empty files reject forward auth; rotating the file takes effect on the next
request. Already-open `/events` streams survive a secret rotation until reconnect. Basic auth, if
also enabled, remains an independent login. The JSON API still requires an allowed Host and
same-origin JSON POSTs; framing is still denied. Leave the secret-file setting unset to keep
the existing forward-auth behavior.

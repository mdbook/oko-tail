# Embedding the view in another app

The whole page is one ES module, `web/view.js`. The standalone page is a thin wrapper around it
(`web/app.js` mounts it on `<body>`), so another web app can show the same live view, with every
feature, on an element of its own. A dashboard that lists several agents can mount one view per
agent.

## What your app needs

1. **The two static files**, served from your own origin: `view.js` and `app.css`. Copy them from
   `web/` at the tag you run (they change together with the server), or proxy `/view.js` and
   `/app.css` from the server.
2. **The data routes**, reachable under one URL prefix: every JSON and SSE endpoint in
   [Development](development.md#endpoints) (`/cfg`, `/rows`, `/events`, `/say` and the rest). The
   usual setup is a reverse proxy in your app that adds its own login and forwards to the server
   with the forward-auth header and proxy secret described in [Security](security.md).
3. **The boot config**: fetch `GET /cfg?session=ID` and pass the JSON in as `cfg`.

## Mounting

```js
import {mount} from '/static/view.js';

const base = '/api/agents/books/';                  // where your proxy exposes the data routes
const cfg = await (await fetch(base + 'cfg?session=*')).json();
const view = mount(document.getElementById('agent'), {
  cfg,
  base,
  css: '/static/app.css',
  go: (session, id) => remount(session, id),       // a session link was clicked
  onStatus: s => setBadge(s.kind, s.unread),       // busy, idle, offline, unread count
});
// later
view.seek(42);      // scroll to message 42
view.destroy();     // stop timers and the stream, empty the element
```

The view lives in a shadow root on that element, so its ids and styles and yours never meet. It
scrolls inside the element and pins its composer and panels to it, so give the element a height.

| Option | Default | Meaning |
|---|---|---|
| `cfg` | the page's inlined config | the boot config from `GET /cfg` |
| `base` | `''` | URL prefix for every request |
| `css` | `base + 'app.css'` | stylesheet URL inside the shadow root |
| `fetch` | `window.fetch` | `fetch(url, init)` to use, for extra headers or credentials |
| `go` | navigate the page | `go(session, messageId)` opens another session |
| `href`, `permalink` | `'#'` when embedded | `href(session)` and `permalink(session, id)` for links |
| `hash` | `location.hash` | `'#m<id>'` to open at |
| `brand` | `base + 'brand'` when `cfg.brand` | the brand image URL, or `null` for none |
| `onStatus` | none | `onStatus({kind, busy, unread, title, text})`; the page sets its tab title and favicon instead |
| `storePrefix` | `'oko.'` | prefix for the view's `localStorage` keys (theme, panel settings, snippets, the send queue) |
| `readOnly` | none | a hint string: watch only (see below) |

`kind` is `''` (idle), `'busy'`, `'compact'`, `'unread'` or `'off'` (stream down or agent offline).

## Read-only views

Pass `readOnly: 'You can watch this agent; sending needs permission.'` (any hint text) for a viewer
that may not act. The composer is disabled with the hint as its placeholder, and every action
reports the hint instead of posting. Enforce it in your proxy too: refuse the POST routes (`/say`,
`/stop`, `/steer`, `/cmd`) with a JSON `{"error": ...}` body, which the view also shows as-is.

## Security notes

- `view.js` runs with your app's privileges, so serve your own copy rather than one the agent's
  host can change. Everything it renders from the agent is escaped or sanitized the same way as on
  the page.
- Keep images raster only through your proxy. An SVG served from your origin is a script-capable
  document there; `/brand` is safest passed to `brand` as a `data:` URL.
- If your proxy is idle-sensitive, `/events` sends a heartbeat comment every 15 seconds.

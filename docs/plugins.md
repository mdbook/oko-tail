# Optional hermes plugins

The basic viewer and page-sent turns work without any plugin.

## The command socket

hermes' HTTP API has no endpoint for slash commands; if you send it `/compress`, the model just reads
the text. To run real commands, oko's tail talks to a small unix socket opened by a hermes platform
plugin inside the gateway, and hermes handles the command exactly as if you'd typed it in your chat
app. The command-socket plugin used to build these features is not published or included here. You cannot
install it from this repo. Any compatible plugin that implements the protocol below will work.

- **Location:** `$HERMES_HOME/oko-tail.sock` (or `OKO_TAIL_SOCKET`). If that path is too long for a
  unix socket, the plugin can bind elsewhere and write the real path to `$HERMES_HOME/oko-tail.sock.path`.
- **Access:** the socket is mode `0600`, and both sides check `SO_PEERCRED`. The plugin accepts only
  its own user, and oko's tail refuses a socket owned by anyone else.
- **Protocol:** one request per connection, one JSON object per line. The request is
  `{"op":"commands"}` (the command list), `{"op":"queue"}` (what waits to run, read only) or
  `{"op":"say","text":"/compress","images":["data:image/png;base64,..."]}` (`images` is optional). A
  `say` can add `"when":"idle"` to wait for the current turn to end even when it's a command the agent
  would otherwise run right away, so a script's `/new` or `/compress` never cuts a turn off. The plugin
  replies with lines like these, always ending with `done`:

  | `type` | Meaning |
  |---|---|
  | `accepted` | `{"id":...}`: the request was received |
  | `held` | the agent is busy; the request runs when the current turn ends |
  | `message` | `{"text":...}`: something the agent sent back |
  | `commands` | `{"commands":[{"name","aliases","args_hint","description","category"}]}` |
  | `queue` | `{"busy":...,"session_id":...,"items":[{"where","id","page","from","peer","text","images","ts"}]}`: `session_id` is the main session's current id (or `null`), and `items` are in run order; `where` is `next`, `queue` or `held` |
  | `error` | `{"error":...}` |
  | `done` | `{"reason":...}`, always last: `finished`, `timeout`, `rejected`, `shutdown`, ... |

The socket always acts on the agent's main session (the one `OKO_TAIL_MAIN_ROUTE` points to). Other
sessions, and setups without the plugin, fall back to the gateway's REST API, which covers `/title`,
`/status`, `/model`, `/fork`, `/retry` and `/skill`, plus `/stop` and `/steer` for turns sent from the page.

### Steer delivery ownership

The command plugin (not included here) owns the held-message pump and runtime cancellation. `accepted`
means received, `held` means queued, and `done: finished` means the command handler finished, not that a
steer was applied to model context. A queue item disappearing can mean applied, cancelled or lost during
a stop-before-delivery race. The HTTP run API's `accepted` steer response is likewise not an application ack.
This viewer therefore persists an **application unconfirmed** receipt before sending a steer, removes it
from the ordinary auto-send queue immediately, and never retries it on Stop, poll failure or reload.
Only the user dismisses it after checking the transcript. This prevents silent loss and client-side duplicate
recovery without pretending the existing protocol supports exactly-once runtime delivery. Separate identical
steers keep separate receipts; text matching is not an identity or delivery acknowledgement.
If a socket send fails after connecting, delivery is uncertain; the server does not fall back to REST and
send the same command again. Only a connection failure before sending allows that fallback.

Runtime pump changes (retain undelivered steers on cancellation, and acknowledge application by stable ID)
belong to the compatible command-plugin/gateway maintainer. Deployment and runtime verification belong to
the operator after independent review; the viewer cannot repair an external pump or verify application
from queue snapshots alone.

## Live progress for turns you didn't send

When you send a message from the page, the reply streams back token by token, so the page always knows
whether the agent is waiting on the model, thinking or writing. A turn you started somewhere else (a chat
platform, cron, the CLI) has no stream here, so the status line can only say *waiting on the model* or
*running a tool*: the database records a turn's steps, not the tokens inside one.

`contrib/hermes-plugin/oko-tail-progress/` is a small hermes plugin that closes that gap. It subscribes
to hermes' streaming observer hooks, keeps a per-session record of the current stage (waiting, thinking,
writing) with a stage timer and character counts, and answers a read-only query on its own unix socket.
It also reports `thinking`, a rolling reasoning-text tail, in each session's progress record. The tail
stays in memory only, is never written to disk or logs, and is cleared when the stream ends (or is
observed stale). Reply deltas only increment `text_chars`; they never enter the tail. The server folds
`thinking` into the same live overlay as page-sent turns; the page inserts it via `textContent`.
The plugin's `think_cap` setting defaults to 4000 characters, matching the server's `THINK_CAP` ceiling;
set it lower to keep less, or to `0` to retain no text and report only stages/counts. The server always
caps received tails at `THINK_CAP`, including when talking to a plugin configured with a larger cap.
With it installed, a turn started from a chat platform or voice shows its live reasoning as well as
its thinking/writing stage and timer.

From the repo checkout, install it the way you'd install any hermes plugin (replace `~/.hermes`
with your `HERMES_HOME` if needed):

```bash
cp -r contrib/hermes-plugin/oko-tail-progress ~/.hermes/plugins/
hermes plugins enable oko-tail-progress
```

Then turn on reasoning deltas in `~/.hermes/config.yaml` and restart the gateway:

```yaml
plugins:
  stream_reasoning_deltas: true
```

Without that flag the plugin still reports waiting and writing, but hermes never hands plugins the
reasoning text, so the stage goes straight from waiting to writing and *thinking* never appears.

Things worth knowing:

- Registering a stream observer makes hermes stream turns it would otherwise run without streaming.
  That is what makes the progress visible, and it is the same path the page's own live view uses.
- Without the plugin, nothing breaks. The status line falls back to the database's phase, exactly as
  before. `python3 oko-tail.py --check` reports the plugin's socket the same way it reports the command
  socket.
- hermes loads plugins in every process that runs an agent, and only the first one to start gets the
  socket; the others log that they are not listening and keep working. So the socket reports the turns
  of whichever process owns it, normally the gateway. A turn run by `hermes` in your terminal while the
  gateway holds the socket still shows the database's phase, not a live stage.

The socket is `$HERMES_HOME/oko-tail-progress.sock` (`OKO_TAIL_PROGRESS_SOCKET` overrides the path for
oko's tail, and the plugin has its own `socket_path` setting). It is mode `0600`, checks `SO_PEERCRED`
on both sides, and its only operation is `{"op":"progress","session":"<id>"}` (the session is optional,
`*` means every one). Like the command socket, it writes the real path to `<path>.path` when the
configured one is too long for a unix socket. A record whose `on_stream_end` never arrived (a crash, a
killed worker) reads as closed after fifteen minutes rather than reporting a stage forever.

## Keeping context in check

A long-running agent's context only grows. hermes compresses it when it nears the model's limit, but by
then every turn has been re-reading a huge prompt for a long time. `contrib/hermes-plugin/context-steward/`
is a small hermes plugin that lets the agent, and anything watching it, see the number and act sooner.

- **The meter.** After every model call it records the prompt-token count the provider reported and writes
  it to `$HERMES_HOME/state/context.json` (`current` is the most recent session; `prompt_tokens` is `null`
  right after `/new`, until the first call). A monitor, a status page or a cron job can read it without
  asking the agent anything. If your agent runs more than one session (say, one per chat thread), `current`
  can be any of them, so anything that acts on one session should read that session's entry under
  `sessions`. The command socket's `queue` reply names the main session's id. Two more things a reader
  should check: the file outlives the plugin, so a `ts` older than the gateway's start means nobody is
  writing it anymore; and `/compress` doesn't update it, since hermes summarizes through auxiliary calls.
- **The nudge.** Once a session's count passes `seam_tokens`, the plugin adds one line to each turn,
  telling the agent where it stands and to reset at its next clean seam. hermes injects it for that turn
  only, so it never piles up in the transcript. Past `ceiling_tokens` the wording turns urgent.

The plugin never resets or compresses anything by itself. The agent picks the moment; what a reset looks
like (you typing `/new`, a supervisor the agent messages, a script on the command socket with
`"when":"idle"`) is yours to choose, which is why both lines are templates.

From the repo checkout (replace `~/.hermes` with your `HERMES_HOME` if needed):

```bash
cp -r contrib/hermes-plugin/context-steward ~/.hermes/plugins/
hermes plugins enable context-steward
```

```yaml
plugins:
  entries:
    context-steward:
      settings:
        seam_tokens: 60000       # start nudging here (0 or unset: never nudge, the meter still runs)
        ceiling_tokens: 100000   # urgent wording from here (0 or unset: no urgent tier)
        # state_file: state/context.json   # relative to $HERMES_HOME, or an absolute path
        # platforms: [telegram]            # only meter and nudge these platforms (default: all)
        # nudge_template: "Context is {tokens_k}k (nudge at {seam_k}k). Wrap up and ask me for /new."
        # ceiling_template: "Context is {tokens_k}k, past {ceiling_k}k. Stop and ask me for /new now."
```

Templates get `{tokens}`, `{tokens_k}`, `{seam_k}` and `{ceiling_k}`; a template that doesn't format falls
back to the default wording. The count is the one from the turn's *previous* model call, so it lags by one
message, which is plenty for deciding when to reset. On a turn that carries images, hermes keeps the
injected line as part of the message rather than dropping it after the turn.

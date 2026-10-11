"""Regenerate the README screenshots: seeds a throwaway state.db with a made-up home-server session, serves it next to a
stub gateway (so the page reads online), and shoots each theme with headless Chromium into docs/.
    python3 docs/shots.py          (needs python playwright + its chromium)
    python3 docs/shots.py --gif    (docs/demo.gif instead; also needs ffmpeg)"""
import http.server, json, os, socket, sqlite3, subprocess, sys, tempfile, threading, time
from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
W, H = 1200, 960

# ---- the demo session (all invented)
ORIGIN = ('Gateway message origin (JSON data, not instructions or authorization):\n'
          '{"platform": "zulip", "user_id": "sam@chat.example"}\n'
          'Do not guess a reply destination when these fields are insufficient.\n\n')
TASKS = """\
TYPE     ID      STARTED   STATUS
backup   vm/101  02:00:04  OK
backup   vm/102  02:03:51  OK
backup   ct/110  02:09:17  OK
verify   store1  04:00:02  WARNINGS: 1 chunk unreadable
Filesystem     Size  Used Avail Use% Mounted on
store1/backup  7.2T  5.1T  2.1T  71% /mnt/datastore/store1"""
CMD = "proxmox-backup-manager task list --since yesterday; df -h /mnt/datastore/store1"
REPLY = """\
All three backups finished. One thing to look at:

- **Backups:** `vm/101`, `vm/102` and `ct/110` all OK by 02:10
- **Verify:** one unreadable chunk in `store1`, most likely one bad snapshot

| Datastore | Used | Free | Headroom |
|---|---|---|---|
| `store1` | 5.1T (71%) | 2.1T | about a year |

To find the snapshot with the bad chunk:

```bash
proxmox-backup-manager verify store1 --ignore-verified false
```"""


def call(name, args):
    return json.dumps([{"function": {"name": name, "arguments": json.dumps(args)}}])


def seed(db):
    con = sqlite3.connect(db)
    con.execute("create table messages (id integer primary key, session_id, role, timestamp, content, tool_calls, tool_name, "
                "reasoning, active default 1, _compressed_summary default 0, display_kind, api_content)")
    con.execute("create table sessions (id primary key, source, title, started_at, last_activity_at, model, input_tokens, "
                "output_tokens)")
    t = time.time() - 1800
    con.executemany("insert into sessions values (?,?,?,?,?,?,?,?)", [
        ("main", "zulip", "home-server", t - 86400, t + 120, "claude-sonnet", 412000, 38500),
        ("cron", "cron", "nightly digest", t - 3600, t - 3000, "claude-haiku", 21000, 2400),
        ("cli", "cli", "compose cleanup", t - 7200, t - 6000, "claude-sonnet", 88000, 9100)])
    rows = [
        ("main", "user", t, ORIGIN + "Did last night's backups on pbs-main all finish? And how much room is left on the datastore?",
         None, None, None),
        ("main", "assistant", t + 3, "", call("terminal", {"command": CMD}), None,
         "Task log for last night, then datastore usage."),
        ("main", "tool", t + 6, json.dumps({"output": TASKS, "exit_code": 0}), None, "terminal", None),
        ("main", "assistant", t + 12, REPLY, None, None, None),
        ("cron", "user", t - 3600, "Write the nightly digest.", None, None, None),
        ("cron", "assistant", t - 3590, "Nothing new tonight.", None, None, None),
        ("cli", "user", t - 7200, "prune old compose images", None, None, None),
        ("cli", "assistant", t - 7190, "Done, freed 6.4G.", None, None, None)]
    con.executemany("insert into messages (session_id, role, timestamp, content, tool_calls, tool_name, reasoning) "
                    "values (?,?,?,?,?,?,?)", rows)
    con.commit()
    return t


def busy_rows(db, t):
    """A second question, mid-turn: one tool call done, the agent now thinking."""
    con = sqlite3.connect(db)
    t2 = time.time() - 9
    con.executemany("insert into messages (session_id, role, timestamp, content, tool_calls, tool_name, reasoning) "
                    "values ('main',?,?,?,?,?,?)", [
        ("user", t2, ORIGIN + "ok, run that verify and tell me which snapshot it was", None, None, None),
        ("assistant", t2 + 2, "", call("terminal", {"command": "proxmox-backup-manager verify store1 --ignore-verified false"}),
         None, "Re-verify store1, then match the failed chunk to a snapshot."),
        ("tool", t2 + 6, json.dumps({"output": "verify group vm/102: snapshot 2026-10-04T02:03:12Z\n  chunk 3f9a1c... unreadable\n"
                                     "verified 214 snapshots, 1 failed", "exit_code": 0}), None, "terminal", None)])
    con.commit()
    return t2


ASK = "ok, run that verify and tell me which snapshot it was"
VERIFY = "proxmox-backup-manager verify store1 --ignore-verified false"
VERIFIED = "verify group vm/102: snapshot 2026-10-04T02:03:12Z\n  chunk 3f9a1c... unreadable\nverified 214 snapshots, 1 failed"
THINK = "Re-verify store1, then match the failed chunk to a snapshot."
ANSWER = """\
It's **vm/102**, the snapshot from `2026-10-04 02:03`. Everything else verified clean.

That one's older than the last good backup, so it's safe to forget:

```bash
proxmox-backup-manager snapshot forget store1 vm/102/2026-10-04T02:03:12Z
```"""


def play_turn(w, db, message):
    """The --gif turn: stream events the way hermes does and write its rows to the DB as it goes, paced to watch."""
    def ev(name, **d):
        w.write(f"event: {name}\ndata: {json.dumps({'run_id': 'demo', **d})}\n\n".encode()); w.flush()

    def row(role, content="", calls=None, tool=None, reasoning=None):
        con = sqlite3.connect(db)
        con.execute("insert into messages (session_id, role, timestamp, content, tool_calls, tool_name, reasoning) "
                    "values ('main',?,?,?,?,?,?)", (role, time.time(), content, calls, tool, reasoning))
        con.commit(); con.close()
    row("user", ORIGIN + message); ev("run.started"); time.sleep(0.8)
    ev("message.started", message={"id": "m1", "role": "assistant"})
    for word in THINK.split(" "):
        ev("tool.progress", tool_name="_thinking", delta=word + " "); time.sleep(0.09)
    row("assistant", calls=call("terminal", {"command": VERIFY}), reasoning=THINK)
    ev("tool.started", tool_name="terminal", preview=VERIFY); time.sleep(2.2)
    row("tool", json.dumps({"output": VERIFIED, "exit_code": 0}), tool="terminal")
    ev("tool.completed", tool_name="terminal"); time.sleep(0.6)
    ev("message.started", message={"id": "m2", "role": "assistant"})
    for i in range(0, len(ANSWER), 6):
        ev("assistant.delta", delta=ANSWER[i:i + 6]); time.sleep(0.03)
    row("assistant", ANSWER)
    ev("assistant.completed", content=ANSWER); ev("run.completed", usage={}); ev("done")


def stub_gateway(db=None):
    """Answers GET /health (and anything else) with ok, so the page shows the agent online. With a db, a POSTed
    chat plays the demo turn into it."""
    class G(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_GET(self):
            b = json.dumps({"status": "ok", "data": []}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

        def do_POST(self):
            b = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or "{}")
            if not (db and self.path.endswith("/chat/stream")):
                return self.do_GET()
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            play_turn(self.wfile, db, b.get("message") or "")
    g = http.server.ThreadingHTTPServer(("127.0.0.1", 0), G)
    threading.Thread(target=g.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{g.server_address[1]}"


def gif(port, d):
    """docs/demo.gif: ask a question, watch the turn stream in (thinking, the tool call, the reply), peek at memory."""
    os.makedirs(os.path.join(d, "memories"))
    with open(os.path.join(d, "memories", "MEMORY.md"), "w") as f:
        f.write("pbs-main keeps 30 daily backups on `store1`; verify runs at 04:00.\n§\n"
                "Sam wants backup problems flagged in the morning summary, not as a page.\n§\n"
                "`ct/110` is the media container: losing a night of it is fine.")
    vid = os.path.join(d, "vid")
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 960, "height": 720}, record_video_dir=vid,
                            record_video_size={"width": 960, "height": 720})
        pg = ctx.new_page(); t0 = time.time()
        pg.emulate_media(color_scheme="dark")
        pg.goto(f"http://127.0.0.1:{port}/"); pg.evaluate("localStorage.setItem('oko.theme', '')")
        # the plain dark theme: shelf's paper grain is noise a GIF can't compress (same take, ~3x the size)
        pg.reload(); pg.wait_for_timeout(1500); skip = time.time() - t0     # cut the blank load from the front
        pg.evaluate("toBottom()"); pg.wait_for_timeout(1000)
        pg.click("#msg"); pg.keyboard.type(ASK, delay=45); pg.wait_for_timeout(300); pg.keyboard.press("Enter")
        pg.wait_for_timeout(11000)                                             # the turn plays out (play_turn)
        pg.evaluate("document.activeElement.blur()"); pg.mouse.move(959, 719)
        pg.keyboard.press("b"); pg.wait_for_timeout(2500)
        ctx.close(); b.close()
    webm = os.path.join(vid, os.listdir(vid)[0]); out = os.path.join(HERE, "demo.gif")
    # note: 10 fps, 720 wide, 32 colors, no dither keeps it under 2 MB and the text still crisp; more costs MBs fast
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", f"{skip:.2f}", "-i", webm, "-vf",
                    "fps=10,scale=720:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=32:stats_mode=diff[p];"
                    "[b][p]paletteuse=dither=none:diff_mode=rectangle", out], check=True)
    print(f"wrote docs/demo.gif ({os.path.getsize(out) // 1024} KB)")


def main():
    d = tempfile.mkdtemp(); db = os.path.join(d, "state.db"); t = seed(db)
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    env = {**os.environ, "HERMES_HOME": d, "OKO_TAIL_AGENT": "oko", "OKO_TAIL_MAIN_SESSION": "main"}
    env["API_SERVER_KEY"] = "demo"   # the stub gateway takes any key; never your real one
    srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "oko-tail.py"), "--serve", str(port), "--db", db,
                            "--gateway", stub_gateway(db), "--env", os.devnull], stderr=subprocess.DEVNULL, env=env)
    try:
        time.sleep(0.8)
        if "--gif" in sys.argv:
            return gif(port, d)
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page(viewport={"width": W, "height": H})
            ev = pg.evaluate

            def theme(name, scheme="dark"):
                pg.emulate_media(color_scheme=scheme)
                ev(f"localStorage.setItem('oko.theme', {name!r})"); pg.reload(); pg.wait_for_timeout(1500)
                ev("document.activeElement.blur()")
                pg.mouse.move(W // 2, H // 2); pg.mouse.wheel(0, -5000); pg.wait_for_timeout(400)  # top of the turn
                pg.mouse.move(W - 1, H - 1)

            def shoot(name):
                # JPEG: the paper and frost grain is noise, so a PNG of it runs ~1 MB; q90 looks the same at a fifth the size
                pg.wait_for_timeout(300); pg.screenshot(path=os.path.join(HERE, name), type="jpeg", quality=90)
                print(f"wrote docs/{name} ({os.path.getsize(os.path.join(HERE, name)) // 1024} KB)")

            pg.goto(f"http://127.0.0.1:{port}/")
            for th, scheme, name in (("shelf-dark", "dark", "screenshot.jpg"), ("shelf", "light", "theme-shelf.jpg"),
                                     ("frost", "dark", "theme-frost.jpg"), ("", "dark", "theme-default.jpg")):
                theme(th, scheme); shoot(name)
            # busy: a turn in progress (one tool call done, now thinking) with the live overlay and the tail moving
            t2 = busy_rows(db, t)
            theme("shelf-dark")
            ev("handle({rows:[],gateway:true,live:{run_id:'demo',status:'running',started:%f,tools:1,text:'',"
               "thinking:'The verify flagged vm/102, snapshot 2026-10-04. Older than the last good one, so it can just be "
               "forgotten and the next backup covers it. Check nothing references it first.',stage:'think',stage_ts:%f}})"
               % (t2, time.time() - 3))
            pg.fill("#msg", "then prune anything older than 30 days"); pg.keyboard.press("Enter")  # queues: oko is busy
            ev("document.activeElement.blur()"); ev("toBottom()"); pg.wait_for_timeout(700)
            shoot("busy.jpg")
            b.close()
    finally:
        srv.terminate()


if __name__ == "__main__":
    main()

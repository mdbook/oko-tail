"""Regenerate the README screenshots: seeds a throwaway state.db with a made-up home-server session, serves it next to a
stub gateway (so the page reads online), and shoots each theme with headless Chromium into docs/.
    python3 docs/shots.py          (needs python playwright + its chromium)"""
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


def stub_gateway():
    """Answers GET /health (and anything else) with ok, so the page shows the agent online."""
    class G(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_GET(self):
            b = json.dumps({"status": "ok", "data": []}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    g = http.server.ThreadingHTTPServer(("127.0.0.1", 0), G)
    threading.Thread(target=g.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{g.server_address[1]}"


def main():
    d = tempfile.mkdtemp(); db = os.path.join(d, "state.db"); t = seed(db)
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    env = {**os.environ, "HERMES_HOME": d, "OKO_TAIL_AGENT": "oko", "OKO_TAIL_MAIN_SESSION": "main"}
    env.pop("API_SERVER_KEY", None)
    srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "oko-tail.py"), "--serve", str(port), "--db", db,
                            "--gateway", stub_gateway(), "--env", os.devnull], stderr=subprocess.DEVNULL, env=env)
    try:
        time.sleep(0.8)
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

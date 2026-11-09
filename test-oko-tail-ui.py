"""Browser check for the page (needs python playwright + its chromium; skips cleanly without them).
Serves a throwaway state.db with a dead gateway, then drives the real page: turns + tally, folding, search,
/help, offline state, and the streamed-turn ghost (fed by calling the page's own handle())."""
import json, os, socket, sqlite3, subprocess, sys, tempfile, time
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("ui test skipped (no playwright)"); sys.exit(0)

d = tempfile.mkdtemp(); db = os.path.join(d, "s.db")
con = sqlite3.connect(db)
con.execute("create table messages (id integer primary key, session_id, role, timestamp, content, tool_calls, tool_name, "
            "reasoning, active default 1, _compressed_summary default 0, display_kind, api_content, platform_message_id)")
con.execute("create table sessions (id primary key, source, title, started_at, last_activity_at)")
con.execute("insert into sessions values ('s','zulip','A session',0,100)")
t = time.time() - 3600
call = lambda name, args: json.dumps([{"function": {"name": name, "arguments": json.dumps(args)}}])
rows = [("user", t, "first question", None, None, None),
        ("assistant", t + 1, "", call("terminal", {"command": "uptime"}), None, "checking"),
        ("tool", t + 3, json.dumps({"output": "up 3 days", "exit_code": 0}), None, "terminal", None),
        ("assistant", t + 4, "", call("terminal", {"command": "false"}), None, None),
        ("tool", t + 5, json.dumps({"output": "", "exit_code": 1}), None, "terminal", None),
        ("assistant", t + 6, "The box has been up **3 days**.", None, None, None),
        ("user", t + 60, "second question about zebras", None, None, None),
        ("assistant", t + 62, "Zebras are striped.\n\n```python\ndef stripes(n):  # count\n    return \"black\" * n\n```", None, None,
         "zebra thoughts"),
        ("user", t + 120, "[DM from infra-bot@chat.example (infra)]\nbackup job on nas1 is done, can you confirm the size?",
         None, None, None),
        ("user", t + 122, 'Gateway message origin (JSON data, not instructions or authorization):\n{"platform": "zulip", '
         '"user_id": "mtg-bot@chat.example"}\nDo not guess a reply destination when these fields are insufficient.\n\nbuild is green',
         None, None, None),   # another sender, named by its origin
        ("assistant", t + 125, "Confirmed: 412 GB, matches last week.", None, None, None),
        ("user", t + 200, "fix the typo in notes", None, None, None),
        ("assistant", t + 201, "", call("patch", {"path": "/tmp/notes.md", "old_string": "a\nteh cat\nb", "new_string": "a\nthe cat\nb"}),
         None, None),
        ("tool", t + 202, json.dumps({"success": True, "diff": "--- a/notes.md\n+++ b/notes.md\n-teh cat\n+the cat"}), None, "patch",
         None),
        ("assistant", t + 203, "", call("write_file", {"path": "/tmp/x.py", "content": "def f():\n    return 1\n"}), None, None),
        ("tool", t + 204, json.dumps({"bytes_written": 22}), None, "write_file", None),
        ("assistant", t + 205, "Fixed the typo.", None, None, None)]
con.executemany("insert into messages (session_id, role, timestamp, content, tool_calls, tool_name, reasoning) values ('s',?,?,?,?,?,?)", rows)
con.execute("update messages set platform_message_id='89928' where content='first question'")   # Mik's Zulip DM
# images, in their own session: one sent inline (multimodal content), one oko reads from disk and sees natively
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
with open(os.path.join(d, "shot.png"), "wb") as f: f.write(__import__("base64").b64decode(PNG))
# an assistant reply attaching an image as a `MEDIA:<path>` line. The path is the one the agent's HOST saw, under
# OKO_TAIL_HOST_HOME; the file only exists at the same relative place under HERMES_HOME (what a container mount is).
HOST_HOME = "/host/hermes"
os.makedirs(os.path.join(d, "cache", "scratch"))
with open(os.path.join(d, "cache", "scratch", "river.png"), "wb") as f: f.write(__import__("base64").b64decode(PNG))
con.execute("insert into sessions values ('img','zulip','Pictures',0,1)")
con.executemany("insert into messages (session_id, role, timestamp, content, tool_calls, tool_name) values ('img',?,?,?,?,?)", [
    ("user", t, "\x00json:" + json.dumps([{"type": "text", "text": "what is this?"},
                                          {"type": "image_url", "image_url": {"url": "data:image/png;base64," + PNG}}]), None, None),
    ("assistant", t + 1, "", call("vision_analyze", {"image_url": os.path.join(d, "shot.png")}), None),
    ("tool", t + 2, "\x00json:" + json.dumps({"_multimodal": True, "text_summary": "Image attached natively", "content": [
        {"type": "text", "text": "Image attached natively"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64," + PNG}}]}),
     None, "vision_analyze"),
    ("assistant", t + 3, "A single pixel.", None, None),
    ("assistant", t + 4, "Here is the couch.\nMEDIA:" + HOST_HOME + "/cache/scratch/river.png\n", None, None)])
con.commit()
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
here = os.path.dirname(os.path.abspath(__file__))
srv = subprocess.Popen([sys.executable, os.path.join(here, "oko-tail.py"), "--serve", str(port), "--db", db, "--session", "s",
                        "--gateway", "http://127.0.0.1:9", "--env", os.devnull], stderr=subprocess.DEVNULL,
                       env={**os.environ, "HERMES_HOME": d, "OKO_TAIL_HOST_HOME": HOST_HOME,
                            "OKO_TAIL_OWNER": "me@chat.example=Mik"})  # d has no oko-tail.sock
srv2 = None


def free_port():
    k = socket.socket(); k.bind(("127.0.0.1", 0)); n = k.getsockname()[1]; k.close(); return n


QITEMS = []  # what the fake socket's queue op reports (the agent's own queue)


def fake_agent(home):
    """A stub gateway (/health, /v1/skills) and a fake plugin socket in `home`; returns the gateway URL + the say log."""
    import http.server, threading
    class G(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            b = json.dumps({"status": "ok", "data": []}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    g = http.server.ThreadingHTTPServer(("127.0.0.1", 0), G)
    threading.Thread(target=g.serve_forever, daemon=True).start()
    said = []
    u = socket.socket(socket.AF_UNIX); u.bind(os.path.join(home, "oko-tail.sock")); u.listen(4)
    def one(c):
        buf = b""
        while b"\n" not in buf: buf += c.recv(4096)
        req = json.loads(buf)
        if req["op"] == "queue":                                   # polled by the page: not part of the say log
            c.sendall(b"".join(json.dumps(x).encode() + b"\n" for x in [{"type": "queue", "busy": bool(QITEMS),
                      "items": QITEMS}, {"type": "done", "reason": "finished"}])); c.close(); return
        said.append(req)
        if req["op"] == "commands":
            ds = [{"type": "commands", "commands": [
                {"name": "usage", "aliases": [], "args_hint": "", "description": "Token usage", "category": "Info"},
                {"name": "compress", "aliases": ["compact"], "args_hint": "[focus]", "description": "Compress context", "category": "Session"},
                {"name": "busy", "aliases": [], "args_hint": "", "description": "Held", "category": "Session"},
                {"name": "retry", "aliases": [], "args_hint": "", "description": "Retry the last message", "category": "Session"}]}]
        elif req["text"].startswith("/busy") or req["text"] == "/deny":
            ds = [{"type": "accepted", "id": "local-1"}, {"type": "held", "id": "local-1"}]
        elif req["text"] == "/stop":
            ds = [{"type": "accepted", "id": "local-2"}, {"type": "message", "id": "local-2:1", "text": "Stopped."}]
        elif req["text"].startswith("/steer "):
            ds = [{"type": "accepted", "id": "local-3"}]
        elif req["text"] == "/context":  # the shape hermes' /context prints (captured live)
            ds = [{"type": "accepted", "id": "local-4"}, {"type": "message", "id": "local-4:1", "text":
                  "🧠 **Context Window**\n\nModel: `vLLM-Main`\nWindow: 200,000 tokens\nIn use: 56,026 / 200,000 (28%)\n"
                  "Auto-compresses at: 150,000 (75%) \u2014 93,974 to go\nCompressions this session: 0\n\n"
                  "Estimated usage by category\n■ System prompt    ~    4,469 tokens ~  2.2%\n▣ Tool definitions ~    6,027 tokens ~  3.0%\n"
                  "▨ Conversation     ~   45,141 tokens ~ 22.6%\n· Free space       ~  143,974 tokens ~ 72.0%\n\n"
                  "Context files\n✓ AGENTS.md ~    1,770 tokens"}]
        elif req["text"].startswith("/model"):
            ds = [{"type": "accepted", "id": "local-5"}, {"type": "message", "id": "local-5:1", "text":
                  "Switched." if " " in req["text"] else
                  "Current: `vLLM-Main` on custom:vllm\n\n**vllm** `--provider vllm` (current):\n  `vLLM-Main`, `vLLM-Fast`\n"
                  "**openrouter** `--provider openrouter`:\n  `big-model`, `small-model` (+12 more)"}]
        else:
            ds = [{"type": "accepted", "id": "local-0"}, {"type": "message", "id": "local-0:1",
                  "text": "**Usage**\n\n- in: 12\n- <img src=x onerror=alert(1)>"}]
        c.sendall(b"".join(json.dumps(x).encode() + b"\n" for x in ds + [{"type": "done", "reason": "finished"}])); c.close()
    def loop():
        while True: threading.Thread(target=one, args=(u.accept()[0],), daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()
    return f"http://127.0.0.1:{g.server_address[1]}", said


try:
    time.sleep(0.8)
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:
            print("ui test skipped (no chromium):", str(e).splitlines()[0]); sys.exit(0)
        pg = b.new_page(viewport={"width": 1200, "height": 800}); errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(f"http://127.0.0.1:{port}/"); pg.wait_for_timeout(1500)
        ev = pg.evaluate
        # a fresh browser opens on the night shelf; an explicit pick of the default theme ('') sticks
        assert ev("document.documentElement.dataset.theme") == "shelf-dark"
        ev("localStorage.setItem('oko.theme','')"); pg.reload(); pg.wait_for_timeout(1500)
        assert not ev("document.documentElement.dataset.theme")
        sums = ev("[...document.querySelectorAll('.row.tally:not([hidden]) .sum')].map(s=>s.textContent)")
        assert sums and sums[0].startswith("2 tool calls") and "1 failed" in sums[0], sums
        assert ev("document.querySelectorAll('.tick.fail').length") == 1 and ev("document.querySelectorAll('.tick.ok').length") == 3
        # older turns folded on load (their steps hidden, their replies kept); the newest turn opens
        assert ev("[...document.querySelectorAll('.row.step')].filter(r=>!r.querySelector('.dpath')).every(r=>!r.offsetParent)")
        assert ev("[...document.querySelectorAll('.row.step')].filter(r=>r.querySelector('.dpath')).every(r=>!!r.offsetParent)")
        # edits read as diffs: the patch call shows path, +1 -1 and only the changed line coloured; its diff result just says applied
        dp = ev("[...document.querySelectorAll('.term .dpath')].map(d=>d.textContent)")
        assert dp == ["/tmp/notes.md +1 −1", "/tmp/x.py 2 lines"], dp
        assert ev("[...document.querySelector('.term .dpath').parentNode.querySelectorAll('pre .del,pre .add')].map(s=>s.textContent)") == \
            ["-teh cat", "+the cat"]
        assert ev("document.querySelector('.term .dpath').parentNode.querySelector('.out').textContent").startswith("applied")
        assert ev("document.querySelectorAll('.term')[3].querySelector('pre .hk')"), "write_file content is syntax-coloured"
        # idle tab: plain title; the gateway here is unreachable, so the favicon carries the gray offline dot
        tt, fav = ev("[document.title,document.querySelector('link[rel=icon]').getAttribute('href')]")
        assert tt.endswith(" · A session") and not tt.startswith("●"), tt
        assert fav.startswith("data:image/svg+xml,") and "8a8790" in fav, fav[:80]
        assert "3 days" in ev("document.querySelector('.row.oko:not(.step) .reply').textContent")
        # a fleet peer's DM: shown as the peer with a source chip, the [DM from ...] line stripped
        peer = ev("(r=>[r.querySelector('.who').textContent,r.querySelector('.via').textContent,r.querySelector('.md').textContent])"
                  "(document.querySelector('.row.in.peer'))")
        assert peer[0] == "infra" and peer[1] == "zulipDM" and peer[2].startswith("backup job"), peer
        # who sent each message, and from where: Mik over Zulip (it has a Zulip id), Mik in the page (none), another agent by its origin
        who = ev("[...document.querySelectorAll('.row.in')].map(r=>[r.classList.contains('peer'),r.querySelector('.who')?.textContent,"
                 "r.querySelector('.ch')?.textContent])")
        assert who == [[False, "Mik", "Zulip"], [False, "Mik", "page"], [True, "infra", None], [True, "mtg", "Zulip"],
                       [False, "Mik", "page"]], who
        pg.click(".tally .tgl"); assert ev("[...document.querySelectorAll('.row.step')].every(r=>r.offsetParent)")
        pg.click("#steps"); assert ev("[...document.querySelectorAll('.row.step')].every(r=>!r.offsetParent)")
        pg.keyboard.press("/"); pg.keyboard.type("zebra"); pg.wait_for_timeout(300)
        assert ev("document.getElementById('qn').textContent") == "2 of 2", ev("document.getElementById('qn').textContent")
        # every session: the server's matches (user text + the reply), marked; a hit in this session jumps in place
        pg.click("#qall"); pg.wait_for_function("document.querySelectorAll('#hits .ah').length===2", timeout=3000)
        assert ev("[...document.querySelectorAll('#hits mark')].map(m=>m.textContent.toLowerCase())") == ["zebra", "zebra"]
        assert ev("document.querySelector('#hits .as').textContent") == "this session"
        assert ev("document.querySelector('#hits .ah').getAttribute('href')") == "?session=s#m8"
        pg.click("#hits .ah"); pg.wait_for_timeout(300)
        assert ev("document.querySelector('.row.linked').dataset.mid") == "8" and ev("location.hash") == "#m8"
        pg.fill("#q", "nomatchxyz"); pg.wait_for_timeout(400)
        assert "Enter looks everywhere" in ev("document.getElementById('qn').textContent")
        pg.keyboard.press("Enter"); pg.wait_for_function("document.querySelector('#hits .hd').textContent==='Nothing in any session'", timeout=3000)
        pg.keyboard.press("Escape"); assert ev("document.getElementById('hits').hidden")
        # permalinks: #m<id> scrolls to that row and marks it; a tool result's id lands on its call, unfolded
        pg.goto(f"http://127.0.0.1:{port}/?session=s#m7"); pg.wait_for_timeout(1500)
        assert ev("document.querySelector('.row.linked').dataset.mid") == "7" and not ev("atBottom()")
        ev("location.hash='#m3'"); pg.wait_for_timeout(300)
        assert ev("(r=>[r.dataset.mid,!!r.offsetParent])(document.querySelector('.row.linked'))") == ["2", True]
        # edit & resend: a past message goes back into the box
        pg.click(".row.in .edit"); assert ev("document.getElementById('msg').value") == "first question"
        pg.fill("#msg", "")
        assert ev("document.body.classList.contains('offline')")                        # dead gateway -> read-only
        pg.fill("#msg", "/help"); pg.focus("#msg"); pg.keyboard.press("Enter"); pg.wait_for_timeout(500)
        assert ev("!!document.querySelector('.row.local .card')"), ev("document.getElementById('cnotes').textContent")
        pg.fill("#msg", "hello"); pg.keyboard.press("Enter"); pg.wait_for_timeout(200)
        assert ev("document.getElementById('msg').value") == "hello"                     # offline never eats the text
        ev("handle({rows:[],gateway:true,live:{run_id:'r',status:'running',started:Date.now()/1000,text:'streamed **text**',tools:0}})")
        assert ev("!document.querySelector('.ghost').hidden && !document.getElementById('stop').hidden")
        ev("cnote('Stopping oko…')")                                                     # a Stop click's note settles when the turn ends
        ev("handle({rows:[],gateway:true,live:{run_id:'r',status:'done',started:Date.now()/1000,text:'x',tools:0}})")
        assert ev("document.querySelector('.ghost').hidden && document.getElementById('stop').hidden")
        assert ev("document.getElementById('cnotes').textContent") == "Stopped."
        # nothing back from the model yet reads "waiting on the model" (tail dozes); reasoning text reads "thinking"
        lv = "handle({rows:[],gateway:true,live:{run_id:'r',status:'running',started:Date.now()/1000-9,tools:0,text:'',thinking:%s,stage:'%s',stage_ts:Date.now()/1000-2}})"
        ghost_run = "document.querySelector('.ghost .run').textContent"
        ev(lv % ("''", "wait"))
        assert ev(ghost_run) == "waiting on the model" and ev("document.body.dataset.phase") == "wait"
        assert ev("document.getElementById('state').textContent").startswith("waiting on the model for")
        assert ev("getComputedStyle(document.querySelector('.ghost .wisp')).animationName") == "doze"
        ev(lv % ("'hmm'", "think"))
        assert ev(ghost_run) == "thinking" and ev("document.querySelector('.ghost .think').textContent") == "hmm"
        assert ev("getComputedStyle(document.querySelector('.ghost .wisp')).animationName") == "sway"
        assert "turn " in ev("document.querySelector('#tail .bd span:last-child').textContent")
        ev("handle({rows:[],gateway:true,live:{run_id:'r',status:'done',started:Date.now()/1000,text:'x',tools:0}})")
        # a turn this page did NOT send (Zulip, cron, the CLI) has no token stream here: the bundled hermes
        # plugin's live stage fills the status line instead, with what has come back and a stage timer. Without
        # the plugin the line is the database's own phase, unchanged.
        prog = "handle({rows:[],gateway:true,waiting:true,phase:'model',since_ts:Date.now()/1000-30,since:'00:00',progress:%s})"
        anim = "getComputedStyle(document.querySelector('#tail .wisp')).animationName"
        ev(prog % "null")
        assert ev("document.getElementById('state').textContent").startswith("waiting on the model for")
        assert ev("document.body.dataset.phase") == "wait"
        ev(prog % "{stage:'think',stage_ts:Date.now()/1000-2,reasoning_chars:4200,text_chars:0,open:true}")
        assert ev("document.getElementById('state').textContent").startswith("thinking (4.2k chars) for"), ev("document.getElementById('state').textContent")
        assert ev("document.body.dataset.phase") == "think" and ev(anim) == "sway"
        assert "turn started 00:00" in ev("document.getElementById('tail').textContent")
        ev(prog % "{stage:'write',stage_ts:Date.now()/1000-1,reasoning_chars:4200,text_chars:900,open:true}")
        assert ev("document.getElementById('state').textContent").startswith("writing (900 chars) for"), ev("document.getElementById('state').textContent")
        assert ev("document.body.dataset.phase") == "write" and ev(anim) == "flick"
        ev(prog % "{stage:'wait',stage_ts:Date.now()/1000-4,reasoning_chars:0,text_chars:0,open:true}")
        assert ev("document.getElementById('state').textContent").startswith("waiting on the model for")
        ev("handle({rows:[],gateway:true,waiting:true,phase:'tool terminal',since_ts:Date.now()/1000-5,since:'00:00',progress:null})")
        assert ev("document.getElementById('state').textContent").startswith("running terminal for"), ev("document.getElementById('state').textContent")
        assert ev("document.body.dataset.phase") == "tool" and ev(anim) == "wag"
        ev("handle({rows:[],gateway:true})")
        # life: the skeleton is gone once rows land; oko's tail is the activity indicator and its motion follows
        # the phase (sway thinking, wag on a tool, curl compacting); rows that arrive live get the enter animation
        assert ev("document.body.classList.contains('ready')")
        anim = "getComputedStyle(document.querySelector('#tail .wisp')).animationName"
        ev("handle({rows:[],gateway:true,waiting:true,phase:'tool terminal',since_ts:Date.now()/1000-5,since:'00:00'})")
        assert ev("document.body.dataset.phase") == "tool" and ev("getComputedStyle(document.getElementById('tail')).display") == "grid"
        assert ev(anim) == "wag", ev(anim)
        if os.environ.get("UI_SHOT"):
            pg.screenshot(path=os.environ["UI_SHOT"].replace(".png", "-busy.png"))
        ev("handle({rows:[],gateway:true,compacting:true})")
        assert ev("document.body.dataset.phase") == "compact" and ev(anim) == "curl"
        ev("handle({rows:[{id:900,session:'s',role:'user',ts:Date.now()/1000,content:'live one',tool_calls:[]}],gateway:true})")
        assert ev("document.body.dataset.phase") == "" and ev("[...document.querySelectorAll('#log .row.in')].pop().classList.contains('enter')")
        # follow mode: at the bottom, tall new rows keep you at the bottom (even though off-screen rows lay out at a
        # placeholder height); scroll up and new rows leave you where you are; back at the bottom it follows again
        tall = lambda i: "{id:%d,session:'s',role:'user',ts:Date.now()/1000,content:%r,tool_calls:[]}" % (i, "line\n" * 30)
        ev("toBottom()"); pg.wait_for_timeout(100)
        ev("handle({rows:[%s],gateway:true})" % ",".join(tall(i) for i in range(901, 913))); pg.wait_for_timeout(400)
        assert ev("atBottom()"), ev("[scrollY,innerHeight,document.documentElement.scrollHeight]")
        pg.mouse.move(600, 400); pg.mouse.wheel(0, -900); pg.wait_for_timeout(300)
        y = ev("scrollY"); ev("handle({rows:[%s],gateway:true})" % tall(913)); pg.wait_for_timeout(400)
        assert not ev("atBottom()") and abs(ev("scrollY") - y) < 5 and ev("document.getElementById('jump').classList.contains('unseen')")
        pg.mouse.wheel(0, 99999); pg.wait_for_timeout(300)
        ev("handle({rows:[%s],gateway:true})" % tall(914)); pg.wait_for_timeout(400)
        assert ev("atBottom()")
        # fenced code is coloured by language, labelled, and the copy button still copies plain text
        assert ev("document.querySelector('.mdpre').dataset.lang") == "python"
        assert ev("[...document.querySelectorAll('.mdpre span:not(.cpt)')].map(x=>x.className).join()") == "hk,hc,hk,hs"
        assert ev("getComputedStyle(document.querySelector('.mdpre .hk')).color!==getComputedStyle(document.querySelector('.mdpre')).color")
        # thinking and reply each get their own copy button, labelled, pinned to their own block; a click copies
        # that block's text only and the button turns into a check
        zr = "[...document.querySelectorAll('#log .row.oko')].filter(r=>r.textContent.includes('Zebras')).pop()"
        assert ev(f"[...{zr}.querySelectorAll('.cpw>.cp .cpt')].map(x=>x.textContent)") == ["thinking", "reply"]
        pg.context.grant_permissions(["clipboard-read", "clipboard-write"])
        ev(f"{zr}.querySelector('.thinkw>.cp').click()")
        pg.wait_for_timeout(150)
        assert ev(f"{zr}.querySelector('.thinkw>.cp').classList.contains('done')")
        assert ev("navigator.clipboard.readText()") == "zebra thoughts"
        ev(f"[...{zr}.querySelectorAll('.cpw>.cp')].pop().click()"); pg.wait_for_timeout(150)
        assert ev("navigator.clipboard.readText()").startswith("Zebras are striped.")
        # "show full" expands the clipped field IN PLACE: the toggle swaps the clipped node for the full text
        # rendered the same way (markdown, thinking, pre), never a second box below it; "show less" restores it.
        # A block's copy button copies the full text. Rows are picked by data-mid, which stays put as the toggle
        # swaps text. The ids sit above the live cursor, so handle() appends these rows the way a streamed turn
        # would. (Clicks go through ev(), i.e. the DOM's own .click(), so a hidden step row still toggles.)
        full_reply = "the whole **reply** text"
        ev("handle({rows:[{id:920,session:'s',role:'assistant',ts:Date.now()/1000-40,content:'clipped beg',"
           f"content_full:{json.dumps(full_reply)},tool_calls:[]}}],gateway:true}})")
        pg.wait_for_timeout(200)
        fr = "document.querySelector('#log .row[data-mid=\"920\"]')"
        assert ev(f"{fr}.querySelector('.cpw .xfull .md').textContent") == "clipped beg", ev(f"{fr}.textContent")
        assert ev(f"{fr}.querySelectorAll('.xfull').length") == 1
        assert ev(f"{fr}.querySelector('.xfull .more').textContent") == f"show full ({len(full_reply)} chars)"
        assert ev(f"{fr}.querySelector('.xfull').children.length") == 2, "the clipped node and its toggle, nothing else"
        assert not ev(f"{fr}.querySelector('.xfull pre')"), "no redundant monospace box before expanding"
        ev(f"{fr}.querySelector('.cpw>.cp').click()"); pg.wait_for_timeout(150)
        assert ev("navigator.clipboard.readText()") == full_reply, "the reply's copy button takes the full text"
        ev(f"{fr}.querySelector('.xfull .more').click()"); pg.wait_for_timeout(150)
        # the SAME spot: the node the clipped text was in now holds the full rendering
        assert ev(f"{fr}.querySelector('.cpw .xfull .md').textContent") == full_reply.replace("**", "")
        assert ev(f"{fr}.querySelector('.cpw .xfull .md strong').textContent") == "reply", "the full text renders as markdown, not raw"
        assert ev(f"{fr}.querySelectorAll('.xfull').length") == 1 and not ev(f"{fr}.querySelector('.xfull pre')")
        assert ev(f"{fr}.querySelector('.xfull .more').textContent") == "show less"
        ev(f"{fr}.querySelector('.xfull .more').click()"); pg.wait_for_timeout(150)
        assert ev(f"{fr}.querySelector('.cpw .xfull .md').textContent") == "clipped beg"
        assert ev(f"{fr}.querySelector('.xfull .more').textContent") == f"show full ({len(full_reply)} chars)"
        # thinking expands the same way, in the thinking block itself (no second pre below it)
        full_think = "a much longer thought"
        ev("handle({rows:[{id:921,session:'s',role:'assistant',ts:Date.now()/1000-30,content:'',tool_calls:[],"
           f"reasoning:'short think',reasoning_full:{json.dumps(full_think)}}}],gateway:true}})")
        pg.wait_for_timeout(200)
        tr = "document.querySelector('#log .row[data-mid=\"921\"]')"
        assert ev(f"{tr}.querySelectorAll('.thinkw .xfull').length") == 1 and not ev(f"{tr}.querySelector('.thinkw pre')")
        ev(f"{tr}.querySelector('.thinkw .cp').click()"); pg.wait_for_timeout(150)
        assert ev("navigator.clipboard.readText()") == full_think
        ev(f"{tr}.querySelector('.thinkw .xfull .more').click()"); pg.wait_for_timeout(150)
        assert ev(f"{tr}.querySelector('.thinkw .think').textContent") == full_think
        # a clipped tool result expands in the well's output slot, and the well's copy button takes the full text
        full_out = "the complete tool output"
        ev("handle({rows:[{id:922,session:'s',role:'tool',ts:Date.now()/1000-20,content:'cut out',tool_name:'terminal',"
           f"content_full:{json.dumps(full_out)}}}],gateway:true}})")
        pg.wait_for_timeout(200)
        tw2 = "document.querySelector('#log .row[data-mid=\"922\"]')"
        assert ev(f"{tw2}.querySelector('.out .xfull .more').textContent") == f"show full ({len(full_out)} chars)"
        assert ev(f"{tw2}.querySelectorAll('.out pre').length") == 1, "one output node, not two"
        ev(f"{tw2}.querySelector('.out .xfull .more').click()"); pg.wait_for_timeout(150)
        assert ev(f"{tw2}.querySelector('.out pre').textContent") == full_out
        ev(f"{tw2}.querySelector('.out .xfull .more').click()"); pg.wait_for_timeout(150)
        assert ev(f"{tw2}.querySelector('.out pre').textContent") == "cut out"
        # a clipped user message: the full text expands in the message's own block, with the peer prefix still stripped
        pref, tail = "[DM from infra-bot@x (infra)]\n", "all of it, much longer"
        ev("handle({rows:[{id:923,session:'s',role:'user',ts:Date.now()/1000-10,"
           f"content:{json.dumps(pref + 'part of it')},content_full:{json.dumps(pref + tail)},tool_calls:[]}}],gateway:true}})")
        pg.wait_for_timeout(200)
        ur = "document.querySelector('#log .row.in[data-mid=\"923\"]')"
        assert ev(f"{ur}.querySelector('.xfull .more').textContent") == f"show full ({len(tail)} chars)", ev(f"{ur}.querySelector('.xfull .more').textContent")
        ev(f"{ur}.querySelector('.bd>.cp').click()"); pg.wait_for_timeout(150)
        assert ev("navigator.clipboard.readText()") == tail, "the prefix stays stripped from the copy too"
        ev(f"{ur}.querySelector('.xfull .more').click()"); pg.wait_for_timeout(150)
        assert ev(f"{ur}.querySelector('.bd .md').textContent") == tail, "the peer prefix stays stripped when expanded"
        # per-tool result renderers (keyed by tool name): web_search reads as a hit list (linked title, domain, snippet),
        # a page fetch as title + url + folded text, the untrusted banner as a small badge with its text on hover. A
        # non-http(s) url never becomes a link, result text never parses as HTML, and a shape the renderer doesn't
        # know (or a tool with no renderer) keeps the generic view.
        def call_and_result(i, name, params, res, wrap=None):
            ev(f"handle({{rows:[{{id:{i},session:'s',role:'assistant',ts:Date.now()/1000-9,content:'',"
               f"tool_calls:[{{name:{json.dumps(name)},params:{json.dumps(params)}}}]}},"
               f"{{id:{i + 1},session:'s',role:'tool',ts:Date.now()/1000-8,tool_name:{json.dumps(name)},"
               f"result:{json.dumps(res)},wrap:{json.dumps(wrap)},tool_calls:[]}}],gateway:true}})")
            pg.wait_for_timeout(150)
            return f"document.querySelector('#log .row[data-mid=\"{i}\"] .term')"
        sw = call_and_result(930, "web_search", {"query": "zebra"}, {"success": True, "data": {"web": [
            {"title": "Zebra facts", "url": "https://www.example.org/z", "description": "All about <b>stripes</b>", "position": 1},
            {"title": "Bad link", "url": "javascript:alert(1)", "description": "x", "position": 2}]}}, "[UNTRUSTED EXTERNAL CONTENT]")
        assert ev(f"{sw}.querySelectorAll('.wres li').length") == 2
        assert ev(f"[...{sw}.querySelectorAll('.wres a')].map(a=>[a.textContent,a.getAttribute('href')])") == \
            [["Zebra facts", "https://www.example.org/z"]], "only the https url is a link"
        assert ev(f"{sw}.querySelector('.wres .wd').textContent") == "example.org"
        assert ev(f"{sw}.querySelector('.wres .ws').textContent") == "All about <b>stripes</b>" and not ev(f"{sw}.querySelector('.wres b')")
        assert ev(f"[{sw}.querySelector('.meta .untr').textContent,{sw}.querySelector('.meta .untr').title]") == \
            ["untrusted source", "[UNTRUSTED EXTERNAL CONTENT]"]
        so = ev(f"{sw}.querySelector('.out').textContent")
        assert "UNTRUSTED" not in so and '"web"' not in so and "success" not in so, so      # no banner paragraph, no JSON dump
        pw = call_and_result(932, "web_extract", {"urls": ["https://docs.example.com/a"]}, {"results": [
            {"url": "https://docs.example.com/a", "title": "Doc A", "content": "\n".join(f"line {i}" for i in range(30))}]})
        assert ev(f"(a=>[a.textContent,a.getAttribute('href')])({pw}.querySelector('.wpage .wh a'))") == ["Doc A", "https://docs.example.com/a"]
        assert ev(f"{pw}.querySelector('.wpage .wd').textContent") == "docs.example.com"
        assert ev(f"!!{pw}.querySelector('.wpage pre.fold')") and ev(f"{pw}.querySelector('.wpage .more').textContent") == "Show 22 more lines"
        assert not ev(f"{pw}.querySelector('.untr')"), "no banner, no badge"
        fw = call_and_result(934, "web_search", {"query": "x"}, {"success": False, "error": "rate limited"})
        assert not ev(f"{fw}.querySelector('.wres')") and ev(f"{fw}.querySelector('.err').textContent") == "error: rate limited"
        assert ev(f"{fw}.classList.contains('failed')")
        uw = call_and_result(936, "custom_thing", {"q": 1}, {"answer": 42, "items": [1, 2]})
        assert not ev(f"{uw}.querySelector('.wres,.wpage')")
        assert ev(f"[...{uw}.querySelectorAll('.out dt')].map(d=>d.textContent)") == ["answer", "items"]
        # a turn exports as markdown (what was said + which tools ran) and links to itself
        ev("document.querySelector('.tally .ta').click()"); pg.wait_for_timeout(150)
        assert ev("navigator.clipboard.readText()").endswith("/?session=s#m1"), ev("navigator.clipboard.readText()")
        ev("[...document.querySelector('.tally').querySelectorAll('.ta')][1].click()"); pg.wait_for_timeout(150)
        tmd = ev("navigator.clipboard.readText()")
        assert tmd.startswith("## ") and "first question" in tmd and "- ran `terminal` {\"command\":\"uptime\"}" in tmd, tmd
        assert "The box has been up **3 days**." in tmd and "up 3 days" not in tmd, tmd       # tool output stays out
        shot = os.environ.get("UI_SHOT")
        if shot:
            pg.screenshot(path=shot)
        # frost mode: f toggles it, it sticks across a reload, and it actually re-themes the page
        bg = ev("getComputedStyle(document.body).backgroundImage")
        ev("document.activeElement.blur()"); pg.keyboard.press("f")
        pg.wait_for_function("document.documentElement.dataset.theme==='frost'", timeout=3000)  # view transition flips async
        assert ev("document.documentElement.dataset.theme") == "frost" and ev("document.getElementById('theme').ariaLabel") == "Theme: frost"
        assert ev("document.getElementById('theme').dataset.next") == "shelf"
        assert "gradient" in ev("getComputedStyle(document.body).backgroundImage") != bg
        pg.reload(); pg.wait_for_timeout(1200)
        assert ev("document.documentElement.dataset.theme") == "frost"
        snowpx = "(()=>{const c=document.getElementById('snow'),d=c.getContext('2d').getImageData(0,0,c.width,c.height).data;let n=0;for(let i=3;i<d.length;i+=4)n+=d[i]>0;return n})()"
        assert ev("getComputedStyle(document.getElementById('snow')).display") == "block" and ev(snowpx) > 0  # it's actually snowing
        if shot:
            pg.emulate_media(color_scheme="dark"); pg.wait_for_timeout(200)
            pg.screenshot(path=shot.replace(".png", "-frost.png"))
            pg.click("#sess"); pg.wait_for_timeout(300)
            pg.screenshot(path=shot.replace(".png", "-frost-menu.png")); pg.keyboard.press("Escape")
            pg.emulate_media(color_scheme="light"); pg.wait_for_timeout(200)
            pg.screenshot(path=shot.replace(".png", "-frost-light.png"))
        # shelf: the next f goes frost -> shelf (paper ground, brown ink, serif), sticks across a reload, and stops the snow
        frostbg = ev("getComputedStyle(document.body).backgroundImage")
        pg.keyboard.press("f"); pg.wait_for_function("document.documentElement.dataset.theme==='shelf'", timeout=3000)
        assert ev("localStorage.getItem('oko.theme')") == "shelf" and ev("document.getElementById('theme').ariaLabel") == "Theme: shelf"
        pg.reload(); pg.wait_for_timeout(1200)
        assert ev("document.documentElement.dataset.theme") == "shelf" and ev("getComputedStyle(document.body).backgroundImage") not in (bg, frostbg)
        assert ev("getComputedStyle(document.documentElement).getPropertyValue('--ground').trim()") == "#efe2c6"
        assert "Bookerly" in ev("getComputedStyle(document.querySelector('.reply')).fontFamily")
        assert ev("getComputedStyle(document.getElementById('snow')).display") == "none" and ev(snowpx) == 0
        assert ev("getComputedStyle(document.querySelector('.mdpre .hk')).color!==getComputedStyle(document.querySelector('.mdpre')).color")
        def shelf_shots(name):
            if not shot: return
            pg.screenshot(path=shot.replace(".png", f"-{name}.png"))
            pg.click("#sess"); pg.wait_for_timeout(300)
            pg.screenshot(path=shot.replace(".png", f"-{name}-menu.png")); pg.keyboard.press("Escape")
            ev("handle({rows:[],gateway:true,waiting:true,phase:'tool terminal',since_ts:Date.now()/1000-5,since:'00:00'})")
            pg.wait_for_timeout(200); pg.screenshot(path=shot.replace(".png", f"-{name}-busy.png"))
            # waiting on the model (the tail dozes in the ghost) with a code block's copy button showing
            ev("handle({rows:[],gateway:true,live:{run_id:'rw',status:'running',started:Date.now()/1000,text:'',tools:0}})")
            assert ev("document.body.dataset.phase") == "wait"
            pg.hover(".mdpre"); pg.wait_for_timeout(250); pg.screenshot(path=shot.replace(".png", f"-{name}-wait.png"))
            ev("handle({rows:[],gateway:true,live:{run_id:'rw',status:'done',started:Date.now()/1000,text:'',tools:0}})")
            ev("handle({rows:[],gateway:true})"); pg.mouse.move(0, 0)
        shelf_shots("shelf")
        # shelf-dark: f again -> the same book at night (dark brown paper, parchment ink), sticks across a reload
        ev("document.activeElement.blur()")
        pg.keyboard.press("f"); pg.wait_for_function("document.documentElement.dataset.theme==='shelf-dark'", timeout=3000)
        assert ev("localStorage.getItem('oko.theme')") == "shelf-dark" and ev("document.getElementById('theme').dataset.next") == "default"
        pg.reload(); pg.wait_for_timeout(1200)
        assert ev("document.documentElement.dataset.theme") == "shelf-dark" and ev("[...document.querySelectorAll('#theme svg')].filter(s=>getComputedStyle(s).display!=='none').map(s=>s.classList[0])") == ["i-default"]
        assert ev("getComputedStyle(document.documentElement).getPropertyValue('--ground').trim()") == "#1e1712"
        assert "Bookerly" in ev("getComputedStyle(document.querySelector('.reply')).fontFamily")
        assert ev("getComputedStyle(document.querySelector('.reply')).textAlign") == "justify"
        assert ev("getComputedStyle(document.querySelector('#mark .fox .tip')).fill") == "rgb(241, 226, 196)"   # theme fox, not the svg's own
        assert ev("getComputedStyle(document.getElementById('snow')).display") == "none" and ev(snowpx) == 0
        shelf_shots("shelf-dark")
        ev("document.activeElement.blur()")
        pg.keyboard.press("f"); pg.wait_for_function("!document.documentElement.dataset.theme", timeout=3000)
        assert ev("localStorage.getItem('oko.theme')") == "" and ev("document.getElementById('theme').dataset.next") == "frost"
        if os.environ.get("UI_PERF"):  # opt-in: frame gaps during each theme wipe over a long log (diagnostic, no assert)
            ev("handle({rows:Array.from({length:400},(_,i)=>({id:5000+i,session:'s',role:i%2?'assistant':'user',ts:Date.now()/1000-4000+i,"
               "content:'para **'+i+'** '+'lorem ipsum dolor sit amet '.repeat(12)+'\\n\\n```py\\nx = '+i+'\\n```',tool_calls:[]})),gateway:true})")
            pg.wait_for_timeout(800)
            for _ in range(6):
                ev("window.__g=[];window.__lt=[];new PerformanceObserver(l=>l.getEntries().forEach(e=>__lt.push([Math.round(e.startTime-__t0),Math.round(e.duration)])))"
                   ".observe({type:'longtask'});window.__t0=performance.now();let L=__t0;(function f(t){__g.push([Math.round(t-__t0),Math.round(t-L)]);L=t;"
                   "if(t-__t0<1100)requestAnimationFrame(f)})(performance.now())")
                ev("document.activeElement.blur()"); pg.keyboard.press("f"); pg.wait_for_timeout(1300)
                print("to", ev("document.documentElement.dataset.theme||'default'"), "gaps>34ms", ev("__g.filter(g=>g[1]>34)"), "longtasks", ev("__lt"))
        # slash commands through the agent's socket: real list with category headers, a reply card (markdown, text only),
        # held and unknown notes. Second server, with a live stub gateway and a fake plugin socket.
        home = tempfile.mkdtemp(); gw_url, said = fake_agent(home); port2 = free_port()
        srv2 = subprocess.Popen([sys.executable, os.path.join(here, "oko-tail.py"), "--serve", str(port2), "--db", db, "--session", "s",
                                 "--gateway", gw_url, "--env", os.devnull], stderr=subprocess.DEVNULL, env={**os.environ, "HERMES_HOME": home,
                                 "OKO_TAIL_PEERS": "staging=/staging/,bad=javascript:alert(1)"})
        time.sleep(0.8)
        pg2 = b.new_page(viewport={"width": 1200, "height": 800}); pg2.on("pageerror", lambda e: errs.append(str(e)))
        pg2.goto(f"http://127.0.0.1:{port2}/"); pg2.wait_for_timeout(1500)
        e2 = pg2.evaluate
        assert not e2("document.body.classList.contains('offline')")
        # other agents (OKO_TAIL_PEERS) sit at the bottom of the session list as plain links; a bad url never gets there
        assert e2("[...document.querySelectorAll('#sesslist .so.peer')].map(a=>a.getAttribute('href')+' '+a.querySelector('.sn').textContent)") == ["/staging/ staging"]
        pg2.fill("#msg", "/"); pg2.wait_for_timeout(500)
        heads = e2("[...document.querySelectorAll('#slash .hd')].map(h=>h.textContent)")
        assert heads[:3] == ["Commands", "Info", "Session"], heads                        # categories, in the agent's order
        assert e2("[...document.querySelectorAll('#slash .opt .n')].map(n=>n.firstChild.textContent)") == \
            ["/help", "/snip", "/usage", "/compress", "/busy", "/retry"]
        pg2.fill("#msg", "/compa"); pg2.wait_for_timeout(300)                             # aliases complete too
        assert "/compress" in e2("document.getElementById('slash').textContent")
        pg2.fill("#msg", "/usage"); pg2.keyboard.press("Enter"); pg2.wait_for_timeout(800)
        assert said[-1] == {"op": "say", "text": "/usage"}, said
        assert e2("document.querySelector('.row.local .card .md strong')?.textContent") == "Usage"
        assert e2("document.querySelectorAll('.row.local .card li').length") == 2 and e2("!document.querySelector('.row.local .card img')")
        pg2.fill("#msg", "/busy"); pg2.keyboard.press("Enter"); pg2.wait_for_timeout(800)
        assert "will run when" in e2("document.getElementById('cnotes').textContent"), e2("document.getElementById('cnotes').textContent")
        pg2.fill("#msg", "/nonesuch"); pg2.keyboard.press("Enter"); pg2.wait_for_timeout(500)
        assert "no /nonesuch" in e2("document.getElementById('cnotes').textContent")
        # snippets: /snip <name> <text> saves (this browser only), /<name> in the menu puts the text in the box, /snip lists
        n = len(said); cnotes = "document.getElementById('cnotes').textContent"
        pg2.fill("#msg", "/snip greet hello there\nsecond line"); pg2.keyboard.press("Enter"); pg2.wait_for_timeout(300)
        assert "Saved" in e2(cnotes) and len(said) == n, e2(cnotes)
        pg2.fill("#msg", "/gre"); pg2.wait_for_timeout(300)
        assert "Your snippets" in e2("document.getElementById('slash').textContent")
        pg2.keyboard.press("Enter"); assert e2("document.getElementById('msg').value") == "hello there\nsecond line" and len(said) == n
        pg2.fill("#msg", "/snip"); pg2.keyboard.press("Enter"); pg2.keyboard.press("Enter"); pg2.wait_for_timeout(300)
        assert "/greet" in e2("[...document.querySelectorAll('.row.local .card')].pop().textContent")
        pg2.fill("#msg", "")
        # retry: a failed newest turn offers retry, which runs the agent's own /retry
        e2("handle({rows:[{id:960,session:'s',role:'assistant',ts:Date.now()/1000,content:'boom',tool_calls:[],kind:'failed_turn'}],gateway:true})")
        pg2.click(".row .retry"); pg2.wait_for_timeout(600)
        assert said[-1] == {"op": "say", "text": "/retry"}, said[-1]
        # queue: while the agent is busy a message waits (box clears, Send says Queue), survives a reload, then goes
        # out by itself once idle; here /say fails (no key), so it stays at the front marked "not sent" with retry
        busy = "handle({rows:[],gateway:true,waiting:true,phase:'model',since_ts:Date.now()/1000,since:'00:00'})"
        qn = "document.querySelectorAll('#queue .qi').length"
        e2(busy); pg2.fill("#msg", "queued one"); pg2.keyboard.press("Enter"); pg2.wait_for_timeout(300)
        assert e2(qn) == 1 and e2("document.getElementById('msg').value") == "" and e2("document.getElementById('send').textContent") == "Queue"
        pg2.fill("#msg", "queued two"); pg2.keyboard.press("Enter"); pg2.wait_for_timeout(300)
        assert e2(qn) == 2 and "2 queued" in e2("document.getElementById('cnotes').textContent")
        if os.environ.get("UI_SHOT"):                                                     # queue chips + composer on paper
            for th in ("shelf", "shelf-dark"):
                e2(f"document.documentElement.dataset.theme='{th}'"); pg2.wait_for_timeout(150)
                pg2.screenshot(path=os.environ["UI_SHOT"].replace(".png", f"-{th}-queue.png"))
            e2("delete document.documentElement.dataset.theme")
        pg2.reload(); pg2.wait_for_timeout(500); e2(busy)
        assert e2("[...document.querySelectorAll('#queue .qt')].map(x=>x.textContent)") == ["queued one", "queued two"]
        pg2.focus("#msg"); pg2.keyboard.press("ArrowUp")                                  # up on an empty box edits the last one
        assert e2("document.getElementById('msg').value") == "queued two" and e2(qn) == 1
        pg2.fill("#msg", "")
        e2("handle({rows:[],gateway:true})"); pg2.wait_for_function("document.querySelector('#queue .qi.failed')", timeout=8000)
        assert e2("document.querySelector('#queue .qk').textContent") == "not sent" and e2(qn) == 1
        pg2.click("#queue .qb[title^='Remove']"); assert e2(qn) == 0 and e2("document.getElementById('queue').hidden")
        # any main-session turn (not just page-sent) can be stopped or steered through the socket: Stop button,
        # Esc Esc, "steer now" (/steer) and "interrupt" (/stop, then the message goes at once, no settle)
        stop_shown = "!document.getElementById('stop').hidden"
        e2(busy); assert e2(stop_shown)
        pg2.fill("#msg", "steer me"); pg2.keyboard.press("Enter"); pg2.wait_for_timeout(200)
        pg2.click("#queue .qb:text('steer now')"); pg2.wait_for_timeout(400)
        assert said[-1] == {"op": "say", "text": "/steer steer me"} and e2(qn) == 0, said[-1]
        pg2.fill("#msg", "urgent"); pg2.keyboard.press("Enter"); pg2.wait_for_timeout(200)
        pg2.click("#queue .qb:text('interrupt')")
        pg2.wait_for_function("document.querySelector('#queue .qi.failed')", timeout=1200)   # sent straight away (fails: no key)
        assert said[-1] == {"op": "say", "text": "/stop"} and not e2(stop_shown) and e2("document.body.dataset.phase") == ""
        assert "Stopped." in e2("document.getElementById('cnotes').textContent") or e2("document.querySelector('#queue .qi.failed')")
        pg2.click("#queue .qb[title^='Remove']")
        # the agent's own queue (a Zulip message behind a compaction, a peer's held DM) shows above the page's
        QITEMS[:] = [{"where": "next", "id": "70426", "page": False, "from": "Mik", "peer": False, "text": "lmk when\nu get this",
                      "images": 0, "ts": 1791697926.6}, {"where": "held", "id": "9", "page": False, "from": "infra", "peer": True,
                      "text": "[DM from infra-bot@x (infra)]\n<b>hi</b>", "images": 1, "ts": None}]
        pg2.wait_for_function("document.querySelectorAll('#queue .qi.srv').length === 2", timeout=5000)
        assert e2("[...document.querySelectorAll('#queue .srv')].map(r=>[...r.querySelectorAll('span')].map(x=>x.textContent).join('|'))")[1] \
            == "held|infra|<b>hi</b>|1 image", e2("document.getElementById('queue').textContent")
        assert e2("document.querySelector('#queue .srv .qt').textContent") == "lmk when u get this" and not e2("document.querySelector('#queue b')")
        if os.environ.get("UI_SHOT"):
            pg2.screenshot(path=os.environ["UI_SHOT"].replace(".png", "-srvq.png"))
        QITEMS[:] = []
        pg2.wait_for_function("document.getElementById('queue').hidden", timeout=5000)
        e2(busy); n = len(said); pg2.focus("#msg"); pg2.keyboard.press("Escape"); pg2.keyboard.press("Escape"); pg2.wait_for_timeout(400)
        assert len(said) == n + 1 and said[-1]["text"] == "/stop", said[n:]
        # token panel: click the usage line -> context gauge from /context, the biggest rows from /ctx, totals, model chips
        e2("handle({rows:[],gateway:true,stats:{model:'vLLM-Main',input_tokens:1000,output_tokens:50,message_count:7,actual_cost_usd:0.5}})")
        pg2.click("#stats"); pg2.wait_for_function("document.querySelector('#ctxp .cmods .chip')", timeout=4000)
        assert not e2("document.getElementById('ctxp').hidden")
        assert e2("document.querySelector('#ctxp .cbig-n').textContent") == "56k of 200k tokens (28%)"
        assert e2("[...document.querySelectorAll('#ctxp section:first-of-type .clegend li span:first-child')].map(x=>x.textContent)") == \
            ["System prompt", "Tool definitions", "Conversation"]          # free space isn't a bar segment
        assert e2("document.querySelector('#ctxp .cbar b').style.left") == "75%"                  # the auto-compact mark
        assert "AGENTS.md 1.8k" in e2("document.querySelector('#ctxp section:first-of-type').textContent")
        assert e2("document.querySelectorAll('#ctxp .cbig li').length") > 0 and e2("document.querySelector('#ctxp .ctot').textContent").count("$0.50") == 1
        assert e2("[...document.querySelectorAll('#ctxp .chip')].map(c=>c.textContent+(c.classList.contains('on')?'*':''))") == \
            ["vLLM-Main*", "vLLM-Fast", "big-model", "small-model"] and "+12 more" in e2("document.getElementById('ctxp').textContent")
        pg2.click("#ctxp .chip:text('big-model')"); pg2.wait_for_timeout(500)
        assert {"op": "say", "text": "/model big-model --provider openrouter"} in said, said[-3:]
        if os.environ.get("UI_SHOT"):
            pg2.screenshot(path=os.environ["UI_SHOT"].replace(".png", "-ctx.png"))
        e2("document.querySelector('#ctxp .cbig button').click()")                         # a big row -> panel closes, row flashes
        assert e2("document.getElementById('ctxp').hidden") or e2("document.querySelector('#ctxp .cbig button').disabled")
        e2("document.getElementById('ctxp').hidden=true"); pg2.keyboard.press("t")
        assert not e2("document.getElementById('ctxp').hidden"); pg2.keyboard.press("Escape")
        assert e2("document.getElementById('ctxp').hidden")
        # browse (b): memory split on §, skills open to their SKILL.md, a staged write approves through the socket
        for p, body in (("memories/MEMORY.md", "first **note**\n§\nsecond"), ("skills/ops/ship/SKILL.md", "---\nname: ship\ndescription: d\n---\n# Ship it"),
                        ("pending/memory/ab12cd34.json", json.dumps({"id": "ab12cd34", "action": "add", "summary": "remember <b>x</b>",
                                                                      "origin": "background_review", "created_at": time.time() - 60, "payload": {"c": 1}}))):
            os.makedirs(os.path.dirname(os.path.join(home, p)), exist_ok=True)
            with open(os.path.join(home, p), "w") as f: f.write(body)
        pg2.keyboard.press("b"); pg2.wait_for_function("document.querySelector('#brw .btabs .chip')", timeout=4000)
        assert e2("[...document.querySelectorAll('#brw .btabs .chip')].map(c=>c.textContent)") == \
            ["Memory 2", "About you 0", "Skills 1", "Cron 0", "Waiting on you 1"]
        assert e2("document.querySelector('#brw .bbody .bent strong').textContent") == "note"
        pg2.click("#brw .chip:text('Skills')"); pg2.click("#brw summary:has-text('ship')")
        pg2.wait_for_function("document.querySelector('#brw details h1, #brw details .md')", timeout=3000)
        assert "Ship it" in e2("document.querySelector('#brw details').textContent")
        pg2.click("#brw .chip:text('Waiting on you')")
        assert "remember <b>x</b>" in e2("document.querySelector('#brw .bbody').textContent") and not e2("document.querySelector('#brw .bbody p b')")
        pg2.click("#brw .bacts .chip:text('Approve')"); pg2.wait_for_timeout(600)
        assert said[-1] == {"op": "say", "text": "/memory approve ab12cd34"}, said[-1]
        pg2.keyboard.press("Escape"); assert e2("document.getElementById('brw').hidden")
        # approvals: a shell tool sitting >8 s in the main session offers /approve answers; idle hides them
        e2("handle({rows:[],gateway:true,waiting:true,phase:'tool:terminal',since_ts:Date.now()/1000-20,since:'00:00'})")
        pg2.wait_for_function("!document.getElementById('appr').hidden", timeout=3000)
        pg2.click("#appr button:text('For this session')"); pg2.wait_for_timeout(500)
        assert said[-1] == {"op": "say", "text": "/approve session"}, said[-1]
        assert e2("[...document.querySelectorAll('#appr button')].map(b=>b.textContent)") == ["Approve", "For this session", "Deny"]
        pg2.click("#appr button:text('Deny')"); pg2.wait_for_timeout(500)                  # held = queued, never "Sent"
        assert "not delivered" in e2("document.getElementById('cnotes').textContent"), e2("document.getElementById('cnotes').textContent")
        e2("handle({rows:[],gateway:true})"); pg2.wait_for_function("document.getElementById('appr').hidden", timeout=3000)
        # socket gone -> the menu falls back and commands say why instead of "invalid"
        os.unlink(os.path.join(home, "oko-tail.sock"))
        pg2.reload(); pg2.wait_for_timeout(1500)
        pg2.fill("#msg", "/compress"); pg2.keyboard.press("Enter"); pg2.wait_for_timeout(500)
        assert "oko-tail socket" in e2("document.getElementById('cnotes').textContent"), e2("document.getElementById('cnotes').textContent")
        # images: the one sent in sits under the message; the tool's well shows what it looked at, once, loaded
        pg.goto(f"http://127.0.0.1:{port}/?session=img"); pg.wait_for_timeout(1500)
        if ev("document.getElementById('steps').textContent") == "Show steps": pg.click("#steps")   # lazy: a hidden step never loads
        pg.wait_for_function("[...document.querySelectorAll('.imgs img')].every(i=>i.complete)", timeout=3000)
        assert ev("document.querySelector('.row.in .md').textContent") == "what is this?"
        assert ev("[...document.querySelectorAll('.row.in .imgs img')].map(i=>[i.getAttribute('src'),i.naturalWidth])") == \
            [["img?id=18&n=0", 1]], ev("document.querySelector('.row.in').outerHTML")
        assert ev("[...document.querySelectorAll('.term .imgs img')].map(i=>[i.getAttribute('src'),i.naturalWidth])") == \
            [["img?id=20&n=0", 1]], ev("document.querySelector('.term').outerHTML")
        assert ev("document.querySelector('.term .imgs').closest('.out')") and "natively" in ev("document.querySelector('.term .out').textContent")
        # an assistant reply's MEDIA: line: the tag is gone from the text and the image rides under the reply,
        # served through the host-home remap (the literal host path is nowhere on this machine)
        assert "MEDIA" not in ev("[...document.querySelectorAll('.row.oko:not(.step)')].pop().textContent")
        assert ev("[...document.querySelectorAll('.row.oko:not(.step) .imgs img')].map(i=>[i.getAttribute('src'),i.naturalWidth])") == \
            [["img?id=22&n=0", 1]], ev("[...document.querySelectorAll('.row.oko:not(.step)')].pop().outerHTML")
        assert not errs, errs
        b.close()
finally:
    srv.terminate()
    if srv2: srv2.terminate()
print("ui test ok")

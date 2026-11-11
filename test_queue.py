"""Exercise the real queue/control JS with synthetic backend replies, without Chromium or hermes."""
import json
import importlib.util
from pathlib import Path
import subprocess

import pytest


JS = (Path(__file__).parent / "web/app.js").read_text()
QUEUE = JS[JS.index("const queueEl="):JS.index("// slash commands:")]
COMMAND = JS[JS.index("async function command(raw){"):JS.index("// ---- search:")]
STORAGE = JS[JS.index("const store="):JS.index("function dur(")]
POST = JS[JS.index("async function post(path,body){"):JS.index("let askedNotify=")]
DISCOVERY = JS[JS.index("let CMDS=null"):JS.index("let opts=[]")] + JS[
    JS.index("function editInto(t){"):JS.index("// a command's answer")]

HARNESS = r"""
const assert=require('node:assert/strict'), vm=require('node:vm');
const source=QUEUE_SOURCE, command=COMMAND_SOURCE, storage=STORAGE_SOURCE, postSource=POST_SOURCE,
  discovery=DISCOVERY_SOURCE;
function page(saved=new Map()){
  const context=vm.createContext({assert,saved});
  vm.runInContext(`
    class Node{
      constructor(){this.children=[];this.hidden=true;this.disabled=false;this.textContent='';
        this.style={setProperty(){}};this.classList={contains:()=>false}}
      append(...xs){this.children.push(...xs)}
      replaceChildren(...xs){this.children=xs}
    }
    const nodes=new Map(), $=id=>{if(!nodes.has(id))nodes.set(id,new Node());return nodes.get(id)};
    const el=(tag,cls,text)=>{const n=new Node();n.tag=tag;n.className=cls;n.textContent=text||'';return n};
    let storageFails=false;
    const localStorage={getItem:k=>saved.get(k)??null,setItem:(k,v)=>{
      if(storageFails)throw new Error('synthetic storage full');saved.set(k,v);
    }};
    const cur='s', CFG={main:'s'}, SINGLE=true, AG='Agent', CMDS=[], DK='draft:s';
    let SOCK=true;
    const B=new Node(), compose={}, msgBox=new Node(), sendBtn=new Node(), cnotes=new Node();
    const document={hidden:false,addEventListener(){}}, listeners=new Map();
    const addEventListener=(name,fn)=>listeners.set(name,fn);
    const setInterval=()=>{}, setTimeout=()=>{}, tick=()=>{}, hhmm=()=>'';
    let compacting=false,turn=null,live=null,pend=null,gatewayUp=true,images=[];
    const running=r=>r&&['running','starting'].includes(r.status);
    const notes=[], calls=[], replies=[];
    const cnote=(text,bad=false)=>{cnotes.textContent=text;notes.push({text,bad})};
    let respond=async()=>({done:'finished',messages:[],held:false});
    async function say(text){await post('/say',{text})}
    const setImages=list=>{images=list}, closeSlash=()=>{}, remember=()=>{};
    async function loadCmds(){return ['stop','steer'].map(name=>({name,kind:'cmd'}))}
    let snapshot={socket:true,items:[],busy:false}, httpOK=true;
    const qs=path=>path, fetch=async(path,options)=>{
      if(!options)return {ok:httpOK,json:async()=>snapshot};
      const body=JSON.parse(options.body);calls.push({path,...body});
      const reply=await respond(path,body);
      return {ok:!reply.httpStatus,status:reply.httpStatus||200,json:async()=>reply.httpStatus?reply.body:reply};
    };
  `,context);
  vm.runInContext(storage+postSource,context);
  // Don't start the startup poll; tests drive each asynchronous boundary explicitly.
  vm.runInContext(source.replace('setInterval(loadSrvQ,3000);loadSrvQ();',''),context);
  vm.runInContext(command,context);
  return code=>vm.runInContext(code,context);
}
const pending=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no});return {promise,resolve,reject}};
(async()=>{
TEST_BODY
})().catch(err=>{console.error(err);process.exitCode=1});
""".replace("QUEUE_SOURCE", json.dumps(QUEUE)).replace("COMMAND_SOURCE", json.dumps(COMMAND)).replace(
    "STORAGE_SOURCE", json.dumps(STORAGE)).replace("POST_SOURCE", json.dumps(POST)).replace(
    "DISCOVERY_SOURCE", json.dumps(DISCOVERY))


def run_js(body):
    result = subprocess.run(["node", "-e", HARNESS.replace("TEST_BODY", body)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_backend_queue_visibility_and_reconnect():
    run_js(r"""
      const p=page();
      await p(`snapshot={socket:true,busy:true,items:[{where:'queue',id:'one',text:'<b>queued</b>'},
        {where:'held',id:'two',text:'second'}]};loadSrvQ()`);
      p(`assert.equal(srvQ.length,2);assert(agentBusy());assert(canForce());
        assert.equal($('force').hidden,false);assert.equal(queueEl.hidden,false);
        assert.equal(queueEl.children[0].children[2].textContent,'<b>queued</b>')`);
      for(const broken of ['snapshot={socket:false,items:[]}', 'httpOK=false', 'httpOK=true;snapshot={error:"offline"}']){
        await p(broken+';loadSrvQ()');
        p(`assert.equal(srvQ.length,2);assert(srvDown);assert(srvBusy);
          assert(queueEl.children.find(n=>n.className==='qi srv stale').children[1].textContent.includes("isn't answering"))`);
      }
      await p(`snapshot={socket:true,busy:false,items:[]};loadSrvQ()`);
      p(`assert.equal(srvQ.length,0);assert.equal(srvDown,false);assert.equal($('force').hidden,true)`);
    """)


@pytest.mark.parametrize("compacting", [False, True])
def test_busy_socket_queue_enables_interrupt_before_command_discovery(compacting):
    run_js(r"""
      const p=page();
      p(`SOCK=false;compacting=COMPACTING;syncStop();assert.equal($('force').hidden,true)`);
      await p(`snapshot={socket:true,busy:true,items:[{where:'queue',id:'one',text:'pending'}]};loadSrvQ()`);
      p(`assert.equal(turn,null);assert.equal(live,null);assert.equal(SOCK,false);
        assert.equal($('force').hidden,false);assert.equal($('stop').hidden,true)`);
      await p('forceInterrupt()');
      p(`assert.equal(calls.length,1);assert.equal(calls[0].path,'/cmd');assert.equal(calls[0].name,'stop');
        assert(notes.at(-1).text.startsWith('Interrupt requested via'));assert.equal(notes.at(-1).bad,false)`);
      await p(`snapshot={socket:true,busy:false,items:[]};loadSrvQ()`);
      p(`assert.equal($('force').hidden,true)`);
    """.replace("COMPACTING", json.dumps(compacting)))


@pytest.mark.parametrize("reply", ["held", "accepted", "lost"])
def test_stop_before_steer_delivery_never_requeues_or_acknowledges(reply):
    run_js(r"""
      const saved=new Map(), p=page(saved), wait=pending();
      p(`turn={since_ts:1};queue=[{text:'steer once'}];renderQ()`);
      // Suspend /steer while /stop makes the turn idle. This was the auto-send duplication window.
      p(`respond=(path,body)=>body.name==='steer'?replies[0]:Promise.resolve({done:'finished',messages:['Stopped.']})`);
      p('replies').push(wait.promise);
      const button=p(`queueEl.children[0].children.find(n=>n.textContent==='steer now')`);
      const request=button.onclick();
      p(`assert.equal(queue.length,0);assert.equal(steers.length,1);
        assert.equal(JSON.parse(store.get(QK)).steers.length,1);
        assert.equal(JSON.parse(store.get(QK)).queue.length,0)`);
      const during=page(saved);
      during(`idleSince=Date.now()-2000;pump();assert.equal(calls.length,0);assert.equal(steers.length,1)`);
      await p('stopTurn()');
      p(`assert.equal(turn,null);idleSince=Date.now()-2000;pump();pump();
        assert.equal(calls.filter(c=>c.path==='/say').length,0);assert.equal(steers.length,1)`);
      if(REPLY==='lost')wait.reject(new Error('reply lost'));
      else wait.resolve({held:REPLY==='held',done:'finished',messages:[]});
      await request;
      // An empty queue after Stop could mean cancelled-before-application, not delivered.
      await p('loadSrvQ()');
      p(`assert.equal(steers.length,1);assert.equal(calls.filter(c=>c.name==='steer').length,1);
        assert(queueEl.children.some(n=>n.className==='qi steer'));
        assert.equal(queueEl.children[0].children[1].textContent,'application unconfirmed')`);
      const reloaded=page(saved);
      await reloaded('loadSrvQ()');
      reloaded(`idleSince=Date.now()-2000;pump();assert.equal(calls.length,0);
        assert.equal(queue.length,0);assert.equal(steers.length,1)`);
      await reloaded(`snapshot={socket:false};loadSrvQ()`);
      await reloaded(`snapshot={socket:true,items:[],busy:false};loadSrvQ()`);
      reloaded(`assert.equal(steers.length,1);assert.equal(calls.length,0);
        dropSteer(steers[0]);assert.equal(JSON.parse(store.get(QK)).steers.length,0)`);
    """.replace("REPLY", json.dumps(reply)))


def test_direct_and_run_id_steers_are_pending_not_deduplicated_by_text():
    run_js(r"""
      const p=page();
      await p(`command('/steer same text')`);
      await p(`live={status:'running',run_id:'run-one'};steerTurn('same text')`);
      p(`assert.equal(steers.length,2);assert.equal(calls.length,2);
        assert.equal(calls[0].name,'steer');assert.equal(calls[1].path,'/steer');
        assert.deepEqual(steers.map(s=>s.text),['same text','same text'])`);
      await p('stopTurn()');
      p(`assert.equal(steers.length,2);assert.equal(calls[2].path,'/stop')`);
      await p('loadSrvQ()');
      p(`assert.equal(steers.length,2);assert.equal(JSON.parse(store.get(QK)).steers.length,2)`);
    """)


@pytest.mark.parametrize("response", ["held", "timeout", "closed", "shutdown", "rejected"])
def test_unacknowledged_stop_does_not_clear_turn_or_report_success(response):
    run_js(r"""
      const p=page();
      p(`turn={since_ts:1};respond=async()=>({held:RESPONSE==='held',done:RESPONSE,messages:[]})`);
      await p(`assert.rejects(stopTurn(),/not acknowledged/)`);
      await p('forceInterrupt()');
      p(`assert.equal(turn.since_ts,1);assert.equal(notes.at(-1).bad,true);
        assert(notes.at(-1).text.startsWith("Couldn't interrupt"));assert.equal($('force').disabled,false)`);
    """.replace("RESPONSE", json.dumps(response)))


def test_interrupt_uses_acknowledged_path_once_and_falls_back_only_before_dispatch():
    run_js(r"""
      // A reload must discover Stop support without waiting for the first composer focus.
      const ready=pending(), discoveryCalls=[];
      let synced=0;
      const context=vm.createContext({LOCAL:[],msgBox:{addEventListener(){}},qs:path=>path,
        fetch:async path=>{discoveryCalls.push(path);return {json:()=>ready.promise}},
        syncStop:()=>{synced++}});
      vm.runInContext(discovery,context);
      assert.deepEqual(discoveryCalls,['cmds']);assert.equal(synced,0);
      ready.resolve({socket:true,commands:[],skills:[]});
      await new Promise(setImmediate);
      assert.equal(vm.runInContext('SOCK',context),true);assert.equal(synced,1);
      const p=page();
      p(`turn={since_ts:1};
        const escape=timeStamp=>listeners.get('keydown')({key:'Escape',timeStamp});
        escape(1000);assert.equal(calls.length,0);escape(1100)`);
      await new Promise(setImmediate);
      p(`assert.equal(calls.length,1);assert.equal(calls[0].path,'/cmd');assert.equal(calls[0].name,'stop');
        assert.equal(turn,null);calls.length=0`);
      p(`live={status:'running',run_id:'run-one'};respond=async()=>({status:'stopping'})`);
      await p('forceInterrupt()');
      p(`assert.equal(calls.length,1);assert.equal(calls[0].path,'/stop');
        assert(notes.at(-1).text.startsWith('Interrupt requested via'));calls.length=0;
        respond=async path=>path==='/stop'
          ?{httpStatus:409,body:{code:'oko_no_live_run',error:'no run'}}:{done:'finished',messages:[]}`);
      await p('forceInterrupt()');
      p(`assert.equal(calls.length,2);assert.equal(calls[1].name,'stop');assert.equal(notes.at(-1).bad,false)`);
    """)


@pytest.mark.parametrize("socket_busy", [False, True])
def test_double_escape_stops_transcript_turn_regardless_of_socket_busy(socket_busy):
    run_js(r"""
      const p=page();
      await p(`snapshot={socket:true,items:[],busy:SOCKET_BUSY};loadSrvQ()`);
      p(`turn={since_ts:1};syncStop();assert(canStop());assert.equal($('stop').hidden,false);
        const escape=timeStamp=>listeners.get('keydown')({key:'Escape',timeStamp,repeat:false});
        escape(1000);assert.equal(calls.length,0);escape(1100)`);
      await new Promise(setImmediate);
      p(`assert.equal(calls.length,1);assert.equal(calls[0].path,'/cmd');assert.equal(calls[0].name,'stop');
        assert.equal(turn,null);assert.equal(stoppedTurn,1);assert.equal(notes.at(-1).bad,false)`);
    """.replace("SOCKET_BUSY", json.dumps(socket_busy)))


@pytest.mark.parametrize("failure", ["lost", "network", "404", "409", "502"])
def test_ambiguous_run_cancel_never_falls_back_to_session_stop(failure):
    run_js(r"""
      const p=page();
      p(`live={status:'running',run_id:'run-one'};let cancelled=0;
        respond=async()=>{
          if(FAILURE==='lost'){cancelled++;live={status:'running',run_id:'run-two'};throw new Error('reply lost')}
          if(FAILURE==='network')throw new Error('network error');
          return {httpStatus:Number(FAILURE),body:{error:'cancel failed'}};
        }`);
      await p('forceInterrupt()');
      p(`assert.equal(calls.length,1);assert.equal(calls[0].path,'/stop');
        assert.equal(cancelled,FAILURE==='lost'?1:0);
        assert(notes.at(-1).text.startsWith('Interrupt status unknown:'));
        assert.equal(notes.at(-1).bad,true);assert.equal($('force').disabled,false)`);
    """.replace("FAILURE", json.dumps(failure)))


@pytest.mark.parametrize("index", [0, 1])
def test_failed_receipt_write_keeps_original_queue_owner_without_sending(index):
    run_js(r"""
      const saved=new Map(), p=page(saved);
      p(`turn={since_ts:1};queue=[{text:'first'},{text:'second'}];saveQ();renderQ();
        const original=queue[INDEX];storageFails=true`);
      await p(`queueEl.children[INDEX].children.find(n=>n.textContent==='steer now').onclick()`);
      p(`assert.equal(calls.length,0);assert.equal(steers.length,0);
        assert.equal(queue[INDEX],original);assert.deepEqual(queue.map(q=>q.text),['first','second']);
        assert.equal(queueEl.children[INDEX].children[0].textContent,"couldn't save, not sent");
        assert(notes.at(-1).text.startsWith("Couldn't save, not sent"));assert(notes.at(-1).bad);
        assert.equal(JSON.parse(store.get(QK)).steers.length,0)`);
      const reload=page(saved);
      reload(`assert.deepEqual(queue.map(q=>q.text),['first','second']);assert.equal(steers.length,0)`);
      p(`turn=null;queue=queue.slice(INDEX);idleSince=Date.now()-2000;pump();pump();assert.equal(calls.length,0);
        storageFails=false;turn={since_ts:1};
        respond=async()=>{
          const receipt=JSON.parse(store.get(QK));
          assert.equal(receipt.steers.length,1);assert(!receipt.queue.some(q=>q.text===original.text));
          assert.equal(steers.length,1);assert(!queue.includes(original));
          return {held:true};
        }`);
      await p('steerTurn(original.text,original)');
      p(`assert.equal(calls.length,1);assert.equal(steers.length,1);assert(!queue.includes(original))`);
    """.replace("INDEX", str(index)))


@pytest.mark.parametrize("run_id", [False, True])
def test_direct_steer_storage_failure_restores_composer_without_sending(run_id):
    run_js(r"""
      const p=page();
      p(`storageFails=true;live=RUN_ID?{status:'running',run_id:'one'}:null;
        msgBox.value='/steer synthetic instruction'`);
      await p('compose.onsubmit({preventDefault(){}})');
      p(`assert.equal(calls.length,0);assert.equal(steers.length,0);
        assert.equal(msgBox.value,'/steer synthetic instruction');
        assert(notes.at(-1).text.includes("Couldn't save, not sent"));assert(notes.at(-1).bad)`);
    """.replace("RUN_ID", json.dumps(run_id)))


def test_ordinary_queue_fifo_retry_and_image_steer_guard():
    run_js(r"""
      const p=page();
      p(`queue=[{text:'first'},{text:'second'}];respond=async()=>{throw new Error('offline')};
        idleSince=Date.now()-2000;pump()`);
      await new Promise(setImmediate);
      p(`assert.equal(queue.length,2);assert.equal(qFail,'offline');assert.equal(calls.length,1);
        pump();assert.equal(calls.length,1);respond=async()=>({});qFail=null;idleSince=Date.now()-2000;pump()`);
      await new Promise(setImmediate);
      p(`assert.equal(queue.length,1);assert.equal(queue[0].text,'second');
        idleSince=Date.now()-2000;pump()`);
      await new Promise(setImmediate);
      p(`assert.equal(queue.length,0);assert.deepEqual(calls.map(c=>c.text),['first','first','second']);
        turn={since_ts:2};queue=[{text:'with image',images:[{url:'synthetic'}]}];renderQ();
        assert(!queueEl.children[0].children.some(n=>n.textContent==='steer now'))`);
    """)


def test_legacy_queue_storage_migrates_atomically():
    run_js(r"""
      const saved=new Map([['oko.queue:s',JSON.stringify([{text:'legacy queued'}])],
        ['oko.steers:s',JSON.stringify([{text:'legacy pending'}])]]), p=page(saved);
      p(`assert.equal(queue.length,1);assert.equal(steers.length,1);turn={since_ts:1};renderQ()`);
      await p(`queueEl.children[1].children.find(n=>n.textContent==='steer now').onclick()`);
      const reloaded=page(saved);
      reloaded(`assert.equal(queue.length,0);assert.deepEqual(steers.map(s=>s.text),['legacy pending','legacy queued']);
        idleSince=Date.now()-2000;pump();assert.equal(calls.length,0)`);
    """)


def test_socket_queue_state_synthetic_acknowledgements(monkeypatch):
    # The real Unix-socket integration checks need bind permission; parsing/ownership needs no live runtime.
    spec = importlib.util.spec_from_file_location("queue_server", Path(__file__).parent / "oko-tail.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    lines = [{"type": "queue", "busy": True, "session_id": "synthetic-session", "items": [
        {"where": "queue", "id": "one", "text": "first synthetic message"},
        {"where": "held", "id": "two", "text": "second synthetic message"}]},
        {"type": "done", "reason": "finished"}]
    monkeypatch.setattr(server, "sock_call", lambda *args: lines)
    items, busy, sid = server.socket_queue_state("unused", width=5)
    assert busy is True and sid == "synthetic-session"
    assert [i["id"] for i in items] == ["one", "two"]
    assert items[0]["text_full"] == "first synthetic message"
    lines[:] = [{"type": "error", "error": "unknown queue op"}]
    assert server.socket_queue_state("unused") == (None, None, None)

    def down(*args):
        raise server.SockDown()

    monkeypatch.setattr(server, "sock_call", down)
    assert server.socket_queue_state("unused") == (None, None, None)


def test_no_live_run_marker_is_a_pre_dispatch_rejection(monkeypatch):
    spec = importlib.util.spec_from_file_location("queue_server", Path(__file__).parent / "oko-tail.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    calls = []

    def gateway(*args):
        calls.append(args)
        return 409, {"error": "upstream cancel response"}

    monkeypatch.setattr(server, "gw", gateway)
    for live in (None, {"run_id": None, "status": "starting"}, {"run_id": "one", "status": "done"}):
        monkeypatch.setattr(server, "live_get", lambda session: live)
        status, reply = server.run_action("synthetic", "stop")
        assert status == 409 and reply["code"] == "oko_no_live_run"
        assert not calls
    monkeypatch.setattr(server, "live_get", lambda session: {"run_id": "one", "status": "running"})
    assert server.run_action("synthetic", "stop") == (409, {"error": "upstream cancel response"})
    assert len(calls) == 1


def test_partial_socket_send_does_not_enable_rest_redelivery(monkeypatch):
    spec = importlib.util.spec_from_file_location("queue_server", Path(__file__).parent / "oko-tail.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)

    class PartialSocket:
        def settimeout(self, value):
            pass

        def connect(self, path):
            pass

        def getsockopt(self, *args):
            return server.struct.pack("3i", 0, server.os.getuid(), 0)

        def sendall(self, request):
            raise OSError("synthetic partial send")

        def close(self):
            pass

    monkeypatch.setattr(server.socket, "socket", lambda *args: PartialSocket())
    monkeypatch.setattr(server, "sock_paths", lambda home: ["synthetic"])
    status, reply = server.socket_cmd("/steer synthetic", "unused")
    assert status == 200 and reply["done"] == "closed"
    assert reply["held"] is False and reply["turn"] is False
    # None would trigger handle_post's REST fallback even if the socket received the whole request.
    assert reply is not None

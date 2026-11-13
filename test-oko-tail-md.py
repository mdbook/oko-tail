"""md() check for oko-tail's page JS (needs node): no hangs, HTML stays text, no javascript: links."""
import subprocess, sys
js = open(__import__("os").path.join(__import__("os").path.dirname(__file__) or ".", "web", "app.js")).read()
a = js.index("const INL="); b = js.index("function text(cls,s,n){")
harness = r"""
class N{constructor(t){this.t=t;this.c=[];this.className='';this.href='';this.dataset={};}append(...x){this.c.push(...x)}set textContent(v){this.c=[String(v)]}}
const document={createElement:t=>new N(t)};
const el=(tag,cls,text)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e};
""" + js[a:b] + r"""
const ser=n=>typeof n==='string'?JSON.stringify(n):'<'+n.t+(n.className?' .'+n.className:'')+(n.href?' href='+n.href:'')+'>'+n.c.map(ser).join('')+'</'+n.t+'>';
const cases=['|a|b|\nplain','**Done** `x` [l](https://e.com) <img src=x onerror=alert(1)>','- a\n- **b**\n\n1. x\n2. y',
 '| h | i |\n|---|---|\n| 1 | 2 |','```\ncode <b>\n```','> q\n---\n### H','[x](javascript:alert(1)) javascript:alert(1)','#tag\n>','-\n1.',
 '```python\nif x: # see https://e.com\n    s = "a#b" + 42\n```','```\nif x # c\n```'];
for(const c of cases)console.log(ser(md(c)));
"""
r = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=10)
out = r.stdout.splitlines()
assert r.returncode == 0 and len(out) == 11, (r.returncode, r.stderr[-800:])
assert "<strong>" in out[1] and "<img" in out[1] and "<img" not in out[1].replace('"', "|").split("|")[0]  # raw HTML stays text
assert "<table>" in out[3] and "<ol>" in out[2] and "href" not in out[6]  # tables, lists; javascript: never linked
# the whole page block must parse as JS (a page-side edit that breaks syntax fails here, not in the browser)
chk = subprocess.run(["node", "--check"], input=js, capture_output=True, text=True, timeout=10)
assert chk.returncode == 0, chk.stderr[-600:]
hl, plain = out[9], out[10]
assert '<span .hk>"if"' in hl and '<span .hs>"\\"a#b\\""' in hl and '<span .hn>"42"' in hl, hl  # kw, string (# inside kept), number
assert '<span .hc>"# see https://e.com"' in hl and "<span" not in plain, plain  # comment; an unlabelled fence stays plain
print("md selftest ok")

# /context varies with the source: provider usage, a local estimate, or transcript-only after eviction.
a = js.index("function parseContext("); b = js.index("function parseModels(", a)
context_test = r"""
const assert=require('node:assert/strict'), num=s=>+String(s).replace(/,/g,'');
""" + js[a:b] + r"""
const exact='🧠 **Context Window**\nWindow: 200,000 tokens\nIn use: 56,026 / 200,000 (28%)\n'+
  'Auto-compresses at: 150,000 (75%)\nCompressions this session: 2\n'+
  '■ System prompt ~ 4,469 tokens ~ 2.2%\n▨ Conversation ~ 45,141 tokens ~ 22.6%\n'+
  '✓ AGENTS.md ~ 1,770 tokens';
assert.equal(parseContext(exact).used,56026);
const estimated=parseContext(exact.replace('In use: 56,026','In use: ~56,026'));
assert.ok(estimated,'the estimate marker must not discard the entire reply');
assert.equal(estimated.used,56026);assert.equal(estimated.estimated,true);
assert.equal(estimated.cats.length,2);assert.equal(estimated.files.length,1);
assert.equal(parseContext(exact).estimated,false);
assert.equal(parseContext(exact.replace('56,026 /','0 /')).used,0);
assert.equal(parseContext(exact.replace('200,000 (28%)','0 (28%)')),null);
const transcript=parseContext('🧠 **Context Window**\nEstimated context: ~1,234 tokens across 5 messages');
assert.equal(transcript.used,1234);assert.equal(transcript.win,null);assert.equal(transcript.estimated,true);
const over=parseContext(exact.replace('Auto-compresses at: 150,000 (75%)','⚠️ **Over auto-compression threshold (150,000, 75%)**'));
assert.equal(over.auto,150000);
const files=parseContext(exact+'\n◐ docs/Project notes.md ~2,345 tokens (truncated)\n○ USER.md ~0 tokens (not loaded)\n✗ MISSING.md ~0 tokens (unreadable)\n⚠️ SOUL.md ~400 tokens (review)');
assert.deepEqual(files.files.map(f=>f.status),['Loaded','Shortened','Not loaded','Unavailable','Flagged']);
assert.equal(files.files[1].name,'docs/Project notes.md');assert.equal(files.files[1].tok,2345);
assert.equal(files.files[2].tok,0);
for(const t of ['', 'No context data available yet.', '**Unknown format**', 'In use: NaN / 200,000'])assert.equal(parseContext(t),null);
console.log('context parser selftest ok');
"""
r = subprocess.run(["node", "-e", context_test], capture_output=True, text=True, timeout=10)
assert r.returncode == 0, r.stderr
print(r.stdout.strip())

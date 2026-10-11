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
assert '<span .hc>"# see https://e.com"' in hl and "<span" not in plain, plain  # comment; an unlabeled fence stays plain
print("md selftest ok")

'use strict';
const $=id=>document.getElementById(id), B=document.body;
const log=$('log'), jump=$('jump'), state=$('state'), more=$('more'), stepsBtn=$('steps');
const tailText=$('tail').querySelector('.bd span:last-child');
const CFG=JSON.parse($('cfg').textContent); // server-filled: {session, main, agent, ms, sessions:[{id,source,title,last}]}
const AG=CFG.agent||'hermes';                // what this install calls its agent
const cur=CFG.session;                       // the session this page watches ('*' = every session)
const SINGLE=cur!=='*';
const el=(tag,cls,text)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e};
const qs=u=>SINGLE?u+(u.includes('?')?'&':'?')+'session='+encodeURIComponent(cur):u;
const hhmm=ts=>new Date(ts*1000).toLocaleTimeString([],{hour12:false,hour:'2-digit',minute:'2-digit'});
const hhmmss=ts=>new Date(ts*1000).toLocaleTimeString([],{hour12:false});
const dayKey=ts=>new Date(ts*1000).toDateString();
const dayLabel=ts=>new Date(ts*1000).toLocaleDateString([],{weekday:'long',month:'long',day:'numeric'});
const hue=s=>{let h=0;for(const c of s||'')h=(h*31+c.charCodeAt(0))%360;return h};
const atBottom=()=>innerHeight+scrollY>=document.documentElement.scrollHeight-80;
const toBottom=()=>scrollTo(0,document.documentElement.scrollHeight);
// Follow the log while you're at the bottom. A flag, not atBottom() at update time: off-screen rows lay out at a
// placeholder height (content-visibility), so the page keeps growing after toBottom(); only scrolling UP unpins.
let pinned=true, lastY=scrollY;
addEventListener('scroll',()=>{const y=scrollY;if(atBottom())pinned=true;else if(y<lastY-2)pinned=false;lastY=y},{passive:true});
new ResizeObserver(()=>{if(pinned&&!atBottom())toBottom()}).observe(document.body);
const nlines=s=>s.split('\n').length;
const isObj=v=>v&&typeof v==='object'&&!Array.isArray(v);
const empty=v=>v==null||v===''||(Array.isArray(v)&&!v.length)||(isObj(v)&&!Object.keys(v).length);
const smooth=matchMedia('(prefers-reduced-motion: no-preference)').matches?'smooth':'auto';
const store={get:(k,d)=>{try{return localStorage.getItem('oko.'+k)??d}catch(e){return d}},
             set:(k,v)=>{try{localStorage.setItem('oko.'+k,v);return true}catch(e){return false}}};
function dur(s){s=Math.max(0,Math.floor(s));const h=Math.floor(s/3600),m=Math.floor(s/60)%60,x=String(s%60).padStart(2,'0');
  return h?h+':'+String(m).padStart(2,'0')+':'+x:m+':'+x}
function took(s){return s<10?s.toFixed(1)+'s':s<60?Math.round(s)+'s':dur(s)}
function kfmt(n){return n>=1e6?(n/1e6).toFixed(1)+'M':n>=1e4?Math.round(n/1e3)+'k':n>=1e3?(n/1e3).toFixed(1)+'k':String(n)}
const wisp=()=>{const s=el('span','wisp');s.setAttribute('aria-hidden','true');return s}; // oko's tail, animated by CSS per phase
function replay(node,cls){node.classList.remove(cls);void node.offsetWidth;node.classList.add(cls)}
function ago(ts){const s=Date.now()/1000-ts;return s<90?'just now':s<3600?Math.round(s/60)+'m ago':s<86400?Math.round(s/3600)+'h ago':Math.round(s/86400)+'d ago'}

// ---- session switcher: a listbox, newest activity first, '*' last
const sessBtn=$('sess'), sessList=$('sesslist');
{
  // the default session is just the agent's name; a hand-picked id (oko-main) names a session
  // better than hermes' auto-title; minted ids don't
  const named=s=>s.id===CFG.main||!/^\d{8}_\d{6}_/.test(s.id);
  const name=s=>s.id===CFG.main?AG:named(s)?s.id:s.title||s.id;
  const me=CFG.sessions.find(s=>s.id===cur);
  $('sessname').textContent=SINGLE?(me?name(me):cur):'All sessions';
  const items=[...CFG.sessions.map(s=>({id:s.id,name:name(s),meta:[s.source,s.last&&ago(s.last),named(s)?s.title:s.id].filter(Boolean).join(', ')})),
               {id:'*',name:'All sessions',meta:'every session, interleaved'},
               ...(CFG.peers||[]).map(p=>({href:p.url,name:p.name,meta:'another agent, its own tail'}))]; // OKO_TAIL_PEERS
  let at=Math.max(0,items.findIndex(i=>i.id===cur));
  const opts=items.map((it,i)=>{
    const o=el('a','so'+(it.id===cur?' on':'')+(it.href?' peer':''));o.href=it.href||'?session='+encodeURIComponent(it.id);o.setAttribute('role','option');o.id='so'+i;
    o.style.setProperty('--i',i);o.append(el('span','sn',it.name),el('span','sm',it.meta));sessList.append(o);return o;
  });
  const markS=()=>opts.forEach((o,i)=>{o.setAttribute('aria-selected',String(i===at));if(i===at)o.scrollIntoView({block:'nearest'})});
  const openS=v=>{sessList.hidden=!v;sessBtn.setAttribute('aria-expanded',String(v));if(v){markS();sessList.focus()}};
  sessList.tabIndex=-1;
  sessBtn.onclick=()=>openS(sessList.hidden);
  sessList.onkeydown=e=>{
    if(e.key==='ArrowDown'||e.key==='j'){at=(at+1)%opts.length;markS()}
    else if(e.key==='ArrowUp'||e.key==='k'){at=(at-1+opts.length)%opts.length;markS()}
    else if(e.key==='Enter'){location.assign(opts[at].href)}
    else if(e.key==='Escape'){openS(false);sessBtn.focus()}
    else return;
    e.preventDefault();e.stopPropagation();
  };
  document.addEventListener('pointerdown',e=>{if(!sessList.hidden&&!e.target.closest('.sesswrap'))openS(false)});
}

// Copy buttons: the text comes from the data, never from the rendered DOM (which carries labels and folds).
// An icon button (two sheets; a check draws itself once copied); `tag` is an optional visible word saying WHAT
// it copies, for rows holding more than one thing (thinking vs reply). cpWrap pins a button to its own block.
function icon(cls,...parts){ // parts: [tag, {attr:value}]; built node by node (the page never parses HTML strings)
  const N='http://www.w3.org/2000/svg', s=document.createElementNS(N,'svg');
  s.setAttribute('class',cls);s.setAttribute('viewBox','0 0 16 16');s.setAttribute('aria-hidden','true');
  for(const [tag,at] of parts){const p=document.createElementNS(N,tag);for(const k in at)p.setAttribute(k,at[k]);s.append(p)}
  return s;
}
function cpBtn(get,label,tag){
  const b=el('button','cp'), t=el('span','cpt',tag||''), idle=()=>{b.classList.remove('done','fail');t.textContent=tag||'';b.title=label||'Copy'};
  b.type='button';b.setAttribute('aria-label',label||'Copy');idle();
  b.append(icon('cs',['rect',{x:5.5,y:5.5,width:8,height:8,rx:1.6}],
    ['path',{d:'M10.5 5.5V4A1.5 1.5 0 0 0 9 2.5H4A1.5 1.5 0 0 0 2.5 4v5A1.5 1.5 0 0 0 4 10.5h1.5'}]),
    icon('ck',['path',{d:'M3.5 8.5l3 3 6-7'}]),t);
  b.onclick=async e=>{
    e.stopPropagation();clearTimeout(b._t);b.classList.remove('done','fail');void b.offsetWidth; // restart the check
    try{await navigator.clipboard.writeText(get());b.classList.add('done');t.textContent='copied';b.title='Copied'}
    catch(_){b.classList.add('fail');t.textContent='can\'t copy here';b.title='The browser blocked the clipboard (needs https or localhost)'}
    b._t=setTimeout(idle,1600);
  };
  return b;
}
function cpWrap(node,get,label,tag){const w=el('div','cpw');w.append(node,cpBtn(get,label,tag));return w}
function cpPres(node){for(const p of node.querySelectorAll('pre.mdpre')){const src=p.textContent;p.append(cpBtn(()=>src,'Copy code'))}return node}

// Long blocks start folded to n lines; the button names what it reveals.
function fold(node,text,n){
  const L=nlines(text);
  if(L<=n+2&&text.length<=n*140)return node; // folding away one or two lines saves nothing
  node.classList.add('fold');node.style.setProperty('--n',n);
  const b=el('button','more');b.type='button';
  const label=()=>node.classList.contains('fold')?(L>n+1?'Show '+(L-n)+' more lines':'Show all'):'Show less';
  b.textContent=label();
  b.onclick=()=>{node.classList.toggle('fold');b.textContent=label()};
  const w=el('div');w.append(node,b);return w;
}
// A field the server clipped (content, reasoning, tool args, a queued message) carries its untruncated text
// in a `_full`/`full` sibling, already in this payload -- no extra request. The toggle swaps the clipped
// rendering for the full text, rebuilt by `render(full)` the same way and in the same spot: one node, never
// a second copy below. Append the returned wrapper where the clipped node would have gone. Like every other
// render helper here the text only ever lands via textContent, never parsed as HTML.
function expand(node,full,render){
  const w=el('div','xfull'), b=el('button','more');b.type='button';
  const clipped=()=>w.firstChild===node, label=()=>{b.textContent=clipped()?'show full ('+full.length+' chars)':'show less'};
  b.onclick=()=>{w.replaceChildren(clipped()?render(full):node,b);label()};
  w.append(node,b);label();
  return w;
}
// Minimal markdown -> DOM (no HTML parsing: every string lands via textContent/createTextNode).
const INL=/(`[^`\n]+`)|(\*\*[^*\n]+\*\*|__[^_\n]+__)|(\[[^\]\n]+\]\((https?:\/\/[^)\s]+)\))|(\*[^*\s\n][^*\n]*\*|\b_[^_\n]+_\b)|(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"])/g;
function inl(parent,s){
  let i=0,m;const re=new RegExp(INL.source,'g'); // per-call: recursion (bold/em) must not share lastIndex
  while((m=re.exec(s))){
    if(m.index>i)parent.append(s.slice(i,m.index));
    if(m[1])parent.append(el('code',null,m[1].slice(1,-1)));
    else if(m[2]){const b=el('strong');inl(b,m[2].slice(2,-2));parent.append(b)}
    else if(m[3]){const a=el('a',null,m[3].slice(1,m[3].indexOf('](')));a.href=m[4];a.target='_blank';a.rel='noopener noreferrer';parent.append(a)}
    else if(m[5]){const e=el('em');inl(e,m[5].slice(1,-1));parent.append(e)}
    else{const a=el('a',null,m[6]);a.href=m[6];a.target='_blank';a.rel='noopener noreferrer';parent.append(a)}
    i=re.lastIndex;
  }
  if(i<s.length)parent.append(s.slice(i));
  return parent;
}
// Fenced-code colouring: one cross-language pass (comments, strings, numbers, keywords), only when the fence
// names a language. Comments need whitespace before them so URLs and $# survive; a stray apostrophe can only
// mis-colour the rest of its own line. diff fences reuse the tool-diff colours.
const KW=new Set(('if else elif for while return def class import from as with try except finally raise in not and or is '+
  'None True False null true false undefined const let var function async await new this self export default fi then do '+
  'done case esac local break continue lambda yield pass match switch struct fn pub use impl type interface enum go func '+
  'package select where insert update delete create table set').split(' '));
const PLAIN=new Set(['text','txt','plain','plaintext','output','console','log','']);
function hl(pre,src,lang){
  if(lang)pre.dataset.lang=lang;
  if(lang==='diff'||lang==='patch'){src.split('\n').forEach((ln,i)=>{if(i)pre.append('\n');
    const c=ln[0]==='+'?'add':ln[0]==='-'?'del':ln.startsWith('@@')?'hunk':null;pre.append(c?el('span',c,ln):ln)});return pre}
  if(PLAIN.has(lang)||src.length>60000){pre.textContent=src;return pre}
  const dash=/^(sql|lua|haskell|hs)$/.test(lang)?'|(?<=^|\\s)--[^\\n]*':'';
  const re=new RegExp('((?<=^|\\s)(?:#|//)[^\\n]*|/\\*[\\s\\S]*?\\*/'+dash+')|("(?:\\\\.|[^"\\\\\\n])*"|\'(?:\\\\.|[^\'\\\\\\n])*\'|`(?:\\\\.|[^`\\\\])*`)'+
    '|(\\b\\d[\\w.]*)|([A-Za-z_$][\\w$]*)','g');
  let i=0,m;
  while((m=re.exec(src))){
    const c=m[1]?'hc':m[2]?'hs':m[3]?'hn':KW.has(m[4])?'hk':null;
    if(!c)continue;
    if(m.index>i)pre.append(src.slice(i,m.index));
    pre.append(el('span',c,m[0]));i=re.lastIndex;
  }
  if(i<src.length)pre.append(src.slice(i));
  return pre;
}
const cells=l=>l.trim().replace(/^\||\|$/g,'').split('|').map(c=>c.trim());
function md(src,cls){
  const root=el('div','md'+(cls?' '+cls:'')),L=src.replace(/\r/g,'').split('\n');
  let i=0;
  while(i<L.length){
    const l=L[i];
    if(/^\s*```/.test(l)){const lang=l.trim().slice(3).trim().toLowerCase(),buf=[];i++;while(i<L.length&&!/^\s*```/.test(L[i]))buf.push(L[i++]);i++;
      root.append(hl(el('pre','mdpre'),buf.join('\n'),lang));continue}
    let h=/^(#{1,6})\s+(.*)$/.exec(l);
    if(h){root.append(inl(el('h'+Math.min(6,h[1].length+2)),h[2]));i++;continue}
    if(/^\s*([-*_])(\s*\1){2,}\s*$/.test(l)){root.append(el('hr'));i++;continue}
    if(/^\s*\|.*\|\s*$/.test(l)&&i+1<L.length&&/^\s*\|?\s*:?-{2,}/.test(L[i+1])){
      const t=el('table'),hr=el('tr');cells(l).forEach(c=>hr.append(inl(el('th'),c)));t.append(hr);i+=2;
      while(i<L.length&&/^\s*\|.*\|\s*$/.test(L[i])){const r=el('tr');cells(L[i++]).forEach(c=>r.append(inl(el('td'),c)));t.append(r)}
      const w=el('div','tbl');w.append(t);root.append(w);continue}
    if(/^\s*>/.test(l)){const buf=[];while(i<L.length&&/^\s*>/.test(L[i]))buf.push(L[i++].replace(/^\s*>\s?/,''));
      const q=el('blockquote');q.append(md(buf.join('\n')));root.append(q);continue}
    const li=/^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
    if(li.test(l)){const ol=/\d/.test(li.exec(l)[2]),list=el(ol?'ol':'ul');
      while(i<L.length&&li.test(L[i])){const x=li.exec(L[i]),it=inl(el('li'),x[3]);if(x[1].length>=2)it.className='sub';list.append(it);i++;
        while(i<L.length&&/^\s{2,}\S/.test(L[i])&&!li.test(L[i])){it.append(el('br'));inl(it,L[i++].trim())}}
      root.append(list);continue}
    if(!l.trim()){i++;continue}
    const p=el('p');let first=true;
    while(i<L.length&&L[i].trim()&&!/^\s*(```|#{1,6}\s|>|([-*+]|\d+[.)])\s)/.test(L[i])&&!/^\s*\|.*\|\s*$/.test(L[i])){
      if(!first)p.append(el('br'));inl(p,L[i++]);first=false}
    if(first)inl(p,L[i++]); // a line no block claimed (e.g. a lone |a|b| row): always consume, never spin
    root.append(p);
  }
  return root;
}
function text(cls,s,n){const p=el('pre',cls,s);return fold(p,s,n)}
function diff(s,n,raw){ // raw: our own -/+ lines, so '---' is a removed line, not a file header
  const p=el('pre');
  s.split('\n').forEach((ln,i)=>{if(i)p.append('\n');
    const c=!raw&&/^(\+\+\+|---)/.test(ln)?'none':ln[0]==='+'?'add':ln[0]==='-'?'del':!raw&&ln.startsWith('@@')?'hunk':null;
    p.append(c?el('span',c,ln):ln)});
  return fold(p,s,n);
}
function value(v){ // one argument or leftover result field
  if(typeof v==='string')return v.includes('\n')||v.length>160?text(null,v,6):el('span',null,v);
  if(v!==null&&typeof v==='object')return text(null,JSON.stringify(v,null,2),6);
  return el('span',null,String(v));
}
function kv(obj,skipEmpty){
  const d=el('dl');
  for(const [k,v] of Object.entries(obj)){
    if(skipEmpty&&empty(v))continue;
    const dd=el('dd');dd.append(value(v));d.append(el('dt',null,k),dd);
  }
  return d.childElementCount?d:null;
}

// Edits read as diffs: a line-level LCS of the old and new text (past 400x400 lines, all - then all +).
function ldiff(a,b){
  const x=a.split('\n'), y=b.split('\n'), n=x.length, m=y.length, out=[];
  if(n*m>160000)return x.map(l=>'-'+l).concat(y.map(l=>'+'+l));
  const L=Array.from({length:n+1},()=>new Uint16Array(m+1));
  for(let i=n-1;i>=0;i--)for(let j=m-1;j>=0;j--)L[i][j]=x[i]===y[j]?L[i+1][j+1]+1:Math.max(L[i+1][j],L[i][j+1]);
  let i=0,j=0;
  while(i<n&&j<m){if(x[i]===y[j]){out.push(' '+x[i]);i++;j++}else if(L[i+1][j]>=L[i][j+1])out.push('-'+x[i++]);else out.push('+'+y[j++])}
  while(i<n)out.push('-'+x[i++]);while(j<m)out.push('+'+y[j++]);
  return out;
}
const EXT={py:'python',js:'js',mjs:'js',ts:'ts',tsx:'ts',sh:'bash',bash:'bash',zsh:'bash',json:'json',yaml:'yaml',yml:'yaml',
  md:'markdown',toml:'toml',sql:'sql',go:'go',rs:'rust',css:'css',html:'html',lua:'lua',txt:'text'};
// patch / write_file / memory calls show the change itself instead of their raw arguments; false = not an edit
function editWell(w,c,p){
  const old=p.old_string??p.old_text, neu=p.new_string??p.new_text??p.content, used=new Set(['path','file_path','target']);
  const path=p.path||p.file_path||(c.name==='memory'&&p.target?p.target+' memory':'');
  let lines=null, body;
  if(typeof p.patch==='string'){lines=p.patch.split('\n');used.add('patch')}
  else if(c.name!=='write_file'&&typeof old==='string'&&typeof neu==='string'){lines=ldiff(old,neu);
    for(const k of ['old_string','old_text','new_string','new_text','content'])used.add(k)}
  else if(c.name==='memory'&&typeof neu==='string'&&p.action==='add'){lines=neu.split('\n').map(l=>'+'+l);used.add('content')}
  if(lines){
    const add=lines.filter(l=>l[0]==='+'&&!l.startsWith('+++')).length, del=lines.filter(l=>l[0]==='-'&&!l.startsWith('---')).length;
    w.append(el('div','tool',c.name),el('div','dpath',path||'(no path)'));
    w.lastChild.append(' ',el('span','add','+'+add),' ',el('span','del','−'+del));
    body=diff(lines.join('\n'),16,typeof p.patch!=='string');
  }else if(c.name==='write_file'&&typeof p.content==='string'){
    used.add('content');
    const ext=(path.match(/\.([A-Za-z0-9]+)$/)||[])[1], src=p.content;
    w.append(el('div','tool','write_file'),el('div','dpath',path||'(no path)'));
    const nl=nlines(src.replace(/\n$/,''));w.lastChild.append(' ',el('span','add',nl+(nl>1?' lines':' line')));
    body=fold(hl(el('pre'),src,EXT[(ext||'').toLowerCase()]||''),src,16);
  }else return false;
  w.append(body);
  const rest={};for(const [k,v] of Object.entries(p))if(!used.has(k))rest[k]=v;
  const extra=kv(rest,true);if(extra)w.append(extra);
  w._copy.push(c.name+' '+JSON.stringify(p,null,2));
  return true;
}

// Images in the transcript (a row's `images`, a call's `images`): our own /img?... or an http(s) URL, nothing else.
// Each opens full size in a new tab; one that won't load (a cache file since cleaned up) just goes away.
function imgs(list){
  const srcs=(list||[]).filter(u=>typeof u==='string'&&(/^img\?id=\d+&n=\d+$/.test(u)||safeUrl(u)));
  if(!srcs.length)return null;
  const w=el('div','imgs');
  for(const u of srcs){const a=el('a'), i=el('img');a.href=u;a.target='_blank';a.rel='noopener noreferrer';
    i.src=u;i.alt='image';i.loading='lazy';i.onerror=()=>{a.remove();if(!w.childElementCount)w.remove()};a.append(i);w.append(a)}
  return w;
}
// A tool call opens a well; its result is dropped into the same well when the tool row arrives.
function callWell(c){
  const w=el('div','term'), p=c.params;
  w._name=c.name;w._copy=[];
  if(isObj(p)&&editWell(w,c,p))w._edit=true;
  else if(isObj(p)&&typeof p.command==='string'){
    const cmd=el('pre','cmd');
    p.command.split('\n').forEach((ln,i)=>{if(i)cmd.append('\n');cmd.append(el('span','ps',i?'> ':'$ '),ln)});
    w.append(fold(cmd,p.command,8));w._copy.push(p.command);
    const rest={...p};delete rest.command;
    const extra=kv(rest);if(extra)w.append(extra);
  }else{
    w.append(el('div','tool',c.name||'tool'));
    // a clipped call (c.full) swaps its args for the full JSON in place: no second box under the well
    if(isObj(p)){const a=kv(p);if(a)w.append(c.full?expand(a,c.full,f=>el('pre',null,f)):a);
      w._copy.push(c.name+' '+(c.full||JSON.stringify(p,null,2)))}
    else if(c.args){const a=text(null,c.args,6);
      w.append(c.full?expand(a,c.full,f=>el('pre',null,f)):a);w._copy.push(c.full||c.args)}
  }
  w._imgs=imgs(c.images);if(w._imgs)w.append(w._imgs);   // the image file the call reads
  w.append(cpBtn(()=>w._copy.join('\n\n'),'Copy the call and its output'));
  w._run=el('div','run');w._run.append(wisp(),el('span','shimmer','running'));w.append(w._run);
  return w;
}
// ---- per-tool result renderers, keyed by tool name. A renderer takes the parsed result (object or array) and
// returns {node, plain, used} -- used: the top-level keys it consumed -- or null when the shape isn't one it
// knows, and the generic view takes over. Shapes are matched loosely (Hermes-style tools and their look-alikes);
// add a tool by adding a key. Untrusted text lands via textContent; a link only gets an http(s) href.
const safeUrl=u=>{try{const x=new URL(String(u));return x.protocol==='https:'||x.protocol==='http:'?x.href:null}catch(_){return null}};
const host=u=>{try{return new URL(String(u)).hostname.replace(/^www\./,'')}catch(_){return ''}};
const str=v=>typeof v==='string'?v:'';
function link(u,label){
  const h=safeUrl(u);if(!h)return el('span','wt',label||String(u||''));
  const a=el('a','wt',label||h);a.href=h;a.target='_blank';a.rel='noopener noreferrer';return a;
}
// the list a result keeps its items in: the array itself, or the first array at one of `keys` (one level down too)
function listOf(r,keys){
  if(Array.isArray(r))return {list:r,used:[]};
  for(const k of keys){const v=r[k];if(Array.isArray(v))return {list:v,used:[k]};
    if(isObj(v))for(const j of keys)if(Array.isArray(v[j]))return {list:v[j],used:[k]}}
  return null;
}
const urlOf=x=>str(x.url)||str(x.link)||str(x.href);
function searchList(r){ // [{title,url,description}] -> linked title, domain, snippet
  const h=listOf(r,['web','results','data','items','organic','news']);
  if(!h||!h.list.every(x=>isObj(x)&&urlOf(x)))return null;
  const ol=el('ol','wres'), plain=[];
  for(const x of h.list){
    const u=urlOf(x), t=str(x.title)||u, d=str(x.description)||str(x.snippet);
    const li=el('li');li.append(link(u,t),el('span','wd',host(u)));if(d)li.append(el('div','ws',d));
    ol.append(li);plain.push(t+'\n'+u+(d?'\n'+d:''));
  }
  return {node:h.list.length?ol:el('div','none','no results'),plain:plain.join('\n\n'),used:h.used};
}
const TEXT=['content','text','markdown','raw_content'];
function pageList(r){ // [{url,title,content}] (or one such object) -> title + URL, the page text folded
  const page=x=>isObj(x)&&(urlOf(x)||TEXT.some(k=>typeof x[k]==='string'));
  const one=isObj(r)&&urlOf(r)&&page(r), h=one?{list:[r],used:['url','link','href','title',...TEXT]}:listOf(r,['results','data','pages','documents']);
  if(!h||!h.list.length||!h.list.every(page))return null;
  const box=el('div','wpages'), plain=[];
  for(const x of h.list){
    const u=urlOf(x), t=str(x.title), c=TEXT.map(k=>x[k]).find(v=>typeof v==='string')||'', w=el('div','wpage');
    const hd=el('div','wh');hd.append(link(u,t||u||'(no url)'));if(u)hd.append(el('span','wd',host(u)));w.append(hd);
    // a lone page's error is the result's own: the generic error line below shows it
    if(!one&&!empty(x.error))w.append(text('err','error: '+(typeof x.error==='string'?x.error:JSON.stringify(x.error)),3));
    w.append(c?text(null,c,8):el('div','none','(no text)'));
    box.append(w);plain.push([t,u,c].filter(Boolean).join('\n'));
  }
  return {node:box,plain:plain.join('\n\n'),used:h.used};
}
const RESULTS={web_search:searchList,search:searchList,web_extract:pageList,web_fetch:pageList,fetch:pageList,web_crawl:pageList};
const PRIMARY=['output','content','diff','stdout','text','result','message'];
// -> [node, failed, plain text for copy/search]
function result(m,callTs,edit,name){ // edit: the call already shows the change, so a diff result just says it applied
  const out=el('div','out'), meta=el('div','meta'), r=m.result;let failed=false,plain='';
  const R=r!=null&&Object.hasOwn(RESULTS,name||'')?RESULTS[name]:null, got=R&&R(r);
  if(m.wrap){const b=el('span','untr',/untrusted/i.test(m.wrap)?'untrusted source':'wrapped');b.title=m.wrap;meta.append(b)}
  if(isObj(r)||got){
    const rest=isObj(r)?{...r}:{};
    const k=PRIMARY.find(k=>typeof r[k]==='string');
    if(got){out.append(got.node);plain=got.plain;for(const u of got.used)delete rest[u]}
    else if(k){delete rest[k];plain=r[k];
      out.append(r[k]?(k==='diff'?(edit?el('div','none','applied'):diff(r[k],16)):text(null,r[k],14)):el('div','none','(no output)'))}
    else if(Array.isArray(r.matches)&&r.matches.every(x=>isObj(x)&&x.path)){
      delete rest.matches;
      const s=r.matches.map(x=>x.path+(x.line!=null?':'+x.line:'')+(x.content!=null?': '+x.content:'')).join('\n');
      plain=s;out.append(s?text(null,s,14):el('div','none','no matches'));
    }
    if(!empty(r.error)){const e='error: '+(typeof r.error==='string'?r.error:JSON.stringify(r.error));out.append(text('err',e,6));plain+='\n'+e;failed=true}
    delete rest.error;
    if('exit_code' in rest){const x=rest.exit_code;delete rest.exit_code;
      if(x!=null){meta.append(el('span',x===0?null:'bad','exit '+x));if(x!==0)failed=true}}
    if('success' in rest){if(rest.success===false){meta.append(el('span','bad','failed'));failed=true}delete rest.success}
    if(callTs!=null&&m.ts-callTs>=0)meta.append(el('span',null,took(m.ts-callTs)));
    if(typeof rest.cwd==='string'){meta.append(el('span',null,rest.cwd));delete rest.cwd}
    const left=kv(rest,true);if(left)out.append(left);
    if(!plain)plain=JSON.stringify(r,null,2);
  }else if(r!=null){
    plain=JSON.stringify(r,null,2);out.append(text(null,plain,14));
    if(callTs!=null)meta.append(el('span',null,took(m.ts-callTs)));
  }else{
    plain=m.content_full||m.content||''; // a clipped output: copy takes the full text, the way the toggle shows it
    const txt=m.content?text(null,m.content,14):el('div','none','(no output)');
    out.append(m.content_full?expand(txt,m.content_full,f=>el('pre',null,f)):txt); // full output in place, no second box
    if(callTs!=null)meta.append(el('span',null,took(m.ts-callTs)));
  }
  const iw=imgs(m.images);if(iw)out.append(iw);           // what a vision/screenshot tool actually looked at
  if(meta.childElementCount)out.append(meta);
  return [out,failed,plain];
}

// ---- turns: a user message opens one; the agent's steps and replies belong to it until the next user message.
// Folding a turn hides its step rows (tool calls, thinking) and keeps the replies, so a folded log reads as
// the conversation and the tally under each message says how much work sat behind the answer.
const turns=[], curTurn=new Map();
let hideSteps=store.get('steps','show')==='hide', booting=true;
function newTurn(m,frag,orphan){
  const r=el('div','row tally'+(orphan?' orphan':'')), g=el('div','gut'), bd=el('div','bd');
  if(!orphan)r.style.setProperty('--rail','var(--you)');
  const t={session:m.session,rows:[],ticks:[],start:m.ts,end:m.ts,folded:hideSteps||booting,orphan,el:r};
  t.tape=el('div','tape');t.sum=el('span','sum');t.btn=el('button','tgl');t.btn.type='button';
  t.btn.onclick=()=>keepView(()=>setFold(t,!t.folded,true));
  const acts=el('span','tacts');
  if(!orphan)acts.append(tBtn('link','Copy a link straight to this turn',async()=>{await clip(permalink(t.session,m.id));return 'link copied'}));
  acts.append(tBtn('copy md','Copy this turn as markdown',async()=>{await clip(turnMd(t));return 'copied'}),
              tBtn('save .md','Download this turn as a markdown file',()=>{saveMd(t);return 'saved'}));
  bd.append(t.tape,t.sum,t.btn,acts);r.append(g,bd);r._turn=t;frag.append(r);
  curTurn.set(m.session,t);turns.push(t);sumUp(t);return t;
}
// ---- links + export: a permalink is ?session=<id>#m<message id>; a turn exports as markdown (what was said and
// which tools ran; tool output and thinking stay out, they're what makes a transcript unreadable)
const permalink=(s,id)=>location.origin+location.pathname+'?session='+encodeURIComponent(s)+'#m'+id;
const clip=s=>navigator.clipboard.writeText(s);
function tBtn(label,title,fn){
  const b=el('button','tgl ta',label);b.type='button';b.title=title;
  b.onclick=async e=>{e.stopPropagation();clearTimeout(b._t);
    try{b.textContent=await fn()}catch(_){b.textContent='can\'t copy here'}
    b._t=setTimeout(()=>{b.textContent=label},1600)};
  return b;
}
function turnMd(t){
  const out=[], h=t.head&&t.head._m, when=ts=>dayLabel(ts)+', '+hhmm(ts);
  if(h)out.push('## '+t.head._who+' · '+when(h.ts),'',t.head._text);
  for(const r of t.rows){
    const m=r._m;if(!m)continue;
    if(m.role==='assistant'){
      if(m.content)out.push('','## '+AG+' · '+when(m.ts),'',m.content);
      for(const c of m.tool_calls){const a=c.params?JSON.stringify(c.params):c.args||'';out.push('','- ran `'+c.name+'`'+(a?' '+(a.length>200?a.slice(0,200)+'…':a):''))}
    }else if(m.kind&&m.content)out.push('','> '+m.kind.replace(/_/g,' ')+': '+m.content.split('\n').join('\n> '));
  }
  return out.join('\n').trim()+'\n';
}
function saveMd(t){
  const a=el('a'), u=URL.createObjectURL(new Blob([turnMd(t)],{type:'text/markdown'}));
  a.href=u;a.download=AG+'-'+(t.head?t.head._m.id:t.start)+'.md';a.click();setTimeout(()=>URL.revokeObjectURL(u),1000);
}
function place(frag,r,m,step){
  const t=curTurn.get(m.session)||newTurn(m,frag,true);
  frag.append(r);r._turn=t;t.rows.push(r);r.dataset.mid=m.id;
  if(step)r.classList.add('step');
  if(t.folded)r.classList.add('f');
  if(m.ts>t.end&&(m.role==='assistant'||m.role==='tool')&&!m.kind&&!m.compacted)t.end=m.ts; // bookkeeping rows can land days later
  dirty.add(t);
}
const dirty=new Set();
function sumUp(t){
  const n=t.ticks.length, f=t.ticks.filter(k=>k._fail).length, steps=t.rows.filter(r=>r.classList.contains('step')).length;
  t.sum.replaceChildren((t.orphan?'continued, ':'')+(n?n+(n>1?' tool calls':' tool call'):'no tool calls')+(t.end-t.start>=1?', '+took(t.end-t.start):''));
  if(f)t.sum.append(', ',el('span','bad',f+' failed'));
  const slow=t.ticks.reduce((a,k)=>k._secs>(a?a._secs:0)?k:a,null); // only worth naming when one call dominated
  if(n>1&&slow&&slow._secs>=10)t.sum.append(', slowest '+slow._name+' '+took(slow._secs));
  t.btn.hidden=!steps;
  t.btn.textContent=t.folded?'Show '+steps+(steps>1?' steps':' step'):'Hide steps';
  t.el.hidden=!n&&!steps;
}
function setFold(t,f,anim){
  t.folded=f;
  for(const r of t.rows){r.classList.toggle('f',f);if(!f&&anim&&r.classList.contains('step'))replay(r,'reveal')}
  sumUp(t);
}
function addTick(t,w){
  const k=el('button','tick go');k.type='button';k.title=(w._name||'tool')+', running';k.setAttribute('aria-label',k.title);
  k.onclick=()=>goWell(w);w._tick=k;w._turn=t;t.ticks.push(k);t.tape.append(k);if(!booting)k.classList.add('new');t.tape.classList.toggle('dense',t.ticks.length>60);
}
function settleTick(w,secs,failed){
  const k=w._tick;if(!k)return;
  k.classList.remove('go');k.classList.add(failed?'fail':'ok');k._fail=failed;k._secs=secs;k._name=w._name||'tool';
  if(w._turn)dirty.add(w._turn);
  k.style.setProperty('--d',Math.min(1,Math.log10(1+Math.max(0,secs))/2.3).toFixed(3)); // 1s low, 10s mid, 3min full
  k.title=(w._name||'tool')+', '+took(secs)+(failed?', failed':'');k.setAttribute('aria-label',k.title);
  if(failed)w.classList.add('failed');
  if(!booting){k.classList.remove('new');replay(k,'landed')}
}
function goWell(w){
  const t=w._turn;
  if(t&&t.folded)setFold(t,false,false);
  w.scrollIntoView({block:'center',behavior:smooth});replay(w,'flash');
}
// Re-layout without losing your place: keep the first visible row where it was on screen.
function keepView(fn){
  if(pinned){fn();toBottom();return}
  const top=$('top').getBoundingClientRect().bottom;let a=null;
  for(const r of log.children){const b=r.getBoundingClientRect();if(b.height&&b.bottom>top){a=r;break}}
  const y=a&&a.getBoundingClientRect().top;fn();
  if(a&&!a.offsetParent&&a._turn)a=a._turn.el;
  if(a&&a.offsetParent)scrollBy(0,a.getBoundingClientRect().top-y);
}
function syncSteps(){stepsBtn.textContent=hideSteps?'Show steps':'Hide steps';stepsBtn.setAttribute('aria-pressed',String(hideSteps))}
stepsBtn.onclick=()=>{hideSteps=!hideSteps;store.set('steps',hideSteps?'hide':'show');syncSteps();keepView(()=>turns.forEach(t=>setFold(t,hideSteps,false)))};
syncSteps();

// One set of actions: inline on desktop, a disclosure on phones (keyboard shortcuts still work).
const overflow=$('overflow'), actions=$('actions');
function closeActions(){overflow.setAttribute('aria-expanded','false')}
overflow.onclick=()=>overflow.setAttribute('aria-expanded',String(overflow.getAttribute('aria-expanded')!=='true'));
actions.addEventListener('click',e=>{
  if(e.target.closest('button')){closeActions();if(overflow.getClientRects().length)overflow.focus()}
},true); // close before the action opens a dialog, so its return-focus target stays visible
document.addEventListener('pointerdown',e=>{if(!actions.contains(e.target)&&!overflow.contains(e.target))closeActions()});
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'&&overflow.getAttribute('aria-expanded')==='true'){
    closeActions();overflow.focus();e.preventDefault();e.stopImmediatePropagation();
  }
},true);

// ---- rows
let after=-1, before=null, lastSess=null, lastWho=null, lastTs=null, lastDay=null, lastMsg=null, unseen=0, turn=null, compacting=false, ok=false;
const pending=new Map(); // session -> tool-call wells still waiting for their result, in call order
function row(m,who,cls,ch){ // ch: where a message was typed (Zulip, page), a quiet line under the sender
  const r=el('div','row '+cls), g=el('div','gut'), bd=el('div','bd'), key=who+'\n'+(ch||'');
  if(!SINGLE)r.style.setProperty('--rail','hsl('+hue(m.session)+' 32% 42%)');
  if(key!==lastWho){g.append(el('span','who',who));if(ch)g.append(el('span','ch',ch))}
  g.append(el('span',null,hhmm(m.ts)));g.title=hhmmss(m.ts)+', '+dayLabel(m.ts);
  r.dataset.mid=m.id;r._m=m;
  lastWho=key;r.append(g,bd);return [r,bd];
}
// Inbound rows hermes writes as role=user that no person typed: name the source instead of "zulip".
// [pattern, label, system?]; a pattern with .strip drops its matched prefix from the shown text.
const strip=re=>Object.assign(re,{strip:true});
const INBOUND=[[/^\[IMPORTANT: Background process /,'background job',true],[/^\[Portcullis\]/,'gate',true],
  [/^🎫 Vikunja /,'vikunja',true],[/^\[System note:/,'system',true],
  [strip(/^\[OUT-OF-BAND USER MESSAGE[^\]]*\]\s*/),'you, mid-turn',false],
  [/^\[Zulip DM · from ([\w-]+)/,'zulip DM',false]];
// A fleet peer's DM folded into the main session: "[DM from <email> (<name>)]" or
// "[group DM from <email> (<name>), with a, b]", optionally timestamped, on its own line.
// Only the presentation loses the envelope; the copy button keeps the original message.
const PEER=/^\[(group )?DM from (\S+) \(([^)\r\n]*)\)(?:, with ((?:(?!, sent )[^\]\r\n])*))?(?:, sent [^\]\r\n]+)?\](?:\r?\n|$)/;
const peerName=(name,email)=>(name||email.split('@')[0]).replace(/-bot$/,'');
function build(m,frag){
  const day=dayKey(m.ts);
  if(day!==lastDay){
    lastDay=day;lastWho=null;
    const d=el('div','row day'), bd=el('div','bd',dayLabel(m.ts));d.dataset.day=day;d.append(el('div','gut'),bd);frag.append(d);
  }
  if(m.compacted){
    // the summary row hermes writes when it folds older turns away (marker only; body is a prompt, not a turn)
    const [r,bd]=row(m,AG,'comp');
    bd.append('Context compacted. Earlier turns were summarised so '+AG+' could keep going.');place(frag,r,m,false);
    curTurn.delete(m.session);return; // a compaction closes whatever turn was open
  }
  if(m.kind){ // hermes bookkeeping, not a turn: name it, never dress it as the agent's own words
    const K={failed_turn:['bad','turn failed'],hidden:['gone','interrupted'],
             internal_notification:['note','system note']}[m.kind];
    const label=K?K[1]:m.kind.replace(/_/g,' ');
    const [r,bd]=row(m,label,'notice '+(K?K[0]:'')+' aux');lastWho=null;
    if(m.content){const t=fold(md(m.content),m.content,8);bd.append(m.content_full?expand(t,m.content_full,f=>md(f)):t)}
    else bd.append(el('div','kd',m.kind==='hidden'?'No text recorded. The reply never finished.':'(no text)'));
    r._q=(label+' '+(m.content||'')).toLowerCase();
    place(frag,r,m,false);
    if(SINGLE&&(m.kind==='failed_turn'||m.kind==='hidden')){const t=r._turn, b=el('button','tgl retry','retry');b.type='button';
      b.title='The newest turn: run it again (/retry). An older one: put its message back in the box';
      b.onclick=()=>{if(t===turns[turns.length-1]){msgBox.value='/retry';compose.requestSubmit()}else if(t.head)editInto(t.head._text)};
      bd.append(b)}
    return;
  }
  if(m.role==='user'){
    pending.delete(m.session);curTurn.delete(m.session);
    let body=m.content||'', who=m.sender||m.source||'in', cls=m.peer?'in peer':'in', via=null, ch=m.via, cut=0;
    const px=PEER.exec(body);
    if(px){
      who=peerName(px[3],px[2]);cls='in peer';ch=null;body=body.slice(px[0].length);cut=px[0].length;
      via=el('div','via',px[1]?'group DM'+(px[4]?' with '+px[4].split(', ').map(e=>peerName('',e)).join(', '):''):'DM');
      via.prepend(el('span','src',m.source||'zulip'));via.title=px[2];
    }else for(const [re,w,sys] of INBOUND){const x=re.exec(body);if(x){who=w;ch=null;if(sys)cls='in sys';if(re.strip){body=body.slice(x[0].length);cut=x[0].length}break}}
    const [r,bd]=row(m,who,cls,ch);lastWho=null;
    if(via)bd.append(via);
    // the preview had a prefix (a peer's "[DM from ...]" line, an origin preamble) stripped off the front, and
    // the full text carries the same prefix (the server clips at the tail), so drop it there too
    const full=m.content_full?m.content_full.slice(cut):null;
    const msg=fold(md(body),body,12), xw=full?expand(msg,full,f=>cpPres(md(f))):msg;
    bd.append(cpPres(xw),cpBtn(()=>px?(m.content_full||m.content):(full||body),'Copy message'));
    const iw=imgs(m.images);if(iw)bd.append(iw);            // images sent in (the page, Zulip, any platform)
    if(cls==='in'&&SINGLE){const e=el('button','tgl edit','edit');e.type='button';e.title='Put this message back in the box to change and send again';
      e.onclick=()=>editInto(body);bd.append(e)}
    r._q=body.toLowerCase();r._who=who;r._text=body;frag.append(r);
    r._turn=newTurn(m,frag,false);r._turn.head=r;
    if(cls!=='in')r._turn.el.style.setProperty('--rail','var(--faint)');
    if(pend&&pend.text.trim()===body.trim())clearPend(); // our message landed: drop the optimistic copy
  }else if(m.role==='assistant'){
    for(const w of pending.get(m.session)||[]){w._run.replaceChildren('no result recorded');w._tick&&w._tick.classList.remove('go')}
    const [r,bd]=row(m,AG,'oko'), wells=[];
    if(m.reasoning){
      const tn=fold(el('div','think',m.reasoning),m.reasoning,3);
      const xw=m.reasoning_full?expand(tn,m.reasoning_full,f=>el('div','think',f)):tn;
      const tw=cpWrap(xw,()=>m.reasoning_full||m.reasoning,'Copy the thinking','thinking');
      tw.classList.add('thinkw');bd.append(tw)}
    if(m.content){
      const reply=!m.tool_calls.length;
      const cn=fold(md(m.content,'reply'),m.content,60);
      const xw=m.content_full?expand(cn,m.content_full,f=>cpPres(md(f,'reply'))):cn;
      bd.append(cpWrap(cpPres(xw),()=>m.content_full||m.content,reply?'Copy the reply':'Copy this text',
        m.reasoning?(reply?'reply':'text'):''));
    }
    let t=curTurn.get(m.session);
    place(frag,r,m,!m.content||m.tool_calls.length>0); // text beside a call is a preamble: a step, not a reply
    t=curTurn.get(m.session);
    const aiw=imgs(m.images);if(aiw)bd.append(aiw);        // images the reply itself attaches (a MEDIA: line)
    for(const c of m.tool_calls){const w=callWell(c);w._ts=m.ts;w._row=r;wells.push(w);bd.append(w);addTick(t,w)}
    if(!bd.childElementCount)bd.append(el('div','none','(empty step)'));
    r._q=[m.reasoning,m.content,...m.tool_calls.map(c=>c.name+' '+(c.params?JSON.stringify(c.params):c.args||''))].join('\n').toLowerCase();
    pending.set(m.session,wells);
  }else if(m.role==='tool'){
    const q=pending.get(m.session)||[];
    const i=q.findIndex(w=>!m.tool_name||!w._name||w._name===m.tool_name);
    if(i>=0){
      const w=q.splice(i,1)[0], [node,failed,plain]=result(m,w._ts,w._edit,w._name||m.tool_name);
      if(w._imgs&&node.querySelector('.imgs'))w._imgs.remove(); // the result carries the image itself: show it once
      w._run.remove();w.append(node);w._copy.push(plain);if(!booting)node.classList.add('enter');
      settleTick(w,m.ts-w._ts,failed);
      if(w._row){w._row._q+='\n'+plain.toLowerCase();const t=w._row._turn;if(t&&m.ts>t.end){t.end=m.ts;dirty.add(t)}}
      return;
    }
    const [r,bd]=row(m,AG,'oko'), w=el('div','term'), [node,,plain]=result(m,null,false,m.tool_name);
    w.append(el('div','tool',(m.tool_name||'tool')+' result'),node,cpBtn(()=>plain));bd.append(w);
    r._q=plain.toLowerCase();place(frag,r,m,true);
  }else{
    const [r,bd]=row(m,m.role.replace('_',' '),'aux');lastWho=null;
    bd.append(el('div','none',m.content||''));r._q=(m.content||'').toLowerCase();place(frag,r,m,false);
  }
}
function append(rows){
  const stick=pinned, frag=document.createDocumentFragment();
  rows=rows.filter(m=>m.id>after);
  if(!rows.length)return;
  if(before==null)before=rows[0].id;                 // page origin: /earlier walks back from here
  $('empty')?.remove();if(!SINGLE)$('earlier').style.display='grid';
  for(const m of rows){
    after=m.id;lastTs=m.ts;lastMsg=m;
    if(!SINGLE&&m.session!==lastSess){
      lastSess=m.session;lastWho=null;
      const s=el('div','row sep'), bd=el('div','bd');s.style.setProperty('--rail','hsl('+hue(m.session)+' 32% 42%)');
      bd.append('session '+(m.source?'on '+m.source+' ':''),el('span',null,m.session||'?'));
      s.append(el('div','gut'),bd);frag.append(s);
    }
    build(m,frag);
  }
  const fresh=booting?[]:[...frag.children];
  log.append(frag);
  for(const r of fresh)r.classList.add('enter');
  for(const t of dirty)sumUp(t);dirty.clear();
  if(hits.length||$('q').value)runSearch(true);
  if(stick){toBottom();unseen=0}else unseen+=rows.length;
  showJump();
}
// Load a page of steps from before the top of the log and keep the viewport on what you were reading.
// ponytail: '*' view only (a single session loads whole); a turn split by the page edge shows as "continued".
async function earlier(){
  if(before==null)return;
  more.disabled=true;more.textContent='Loading…';
  const k=document.documentElement.scrollHeight, anchor=document.querySelector('#log .row');
  let j;
  try{j=await (await fetch(qs('earlier?before='+before))).json()}
  catch(e){more.disabled=false;more.textContent='Couldn\'t load earlier steps. Try again';return}
  const frag=document.createDocumentFragment(), was=[lastSess,lastWho,lastDay,new Map(curTurn)];
  lastSess=null;lastWho=null;lastDay=null;curTurn.clear();
  for(const m of j.rows)build(m,frag);
  if(j.oldest!=null)before=j.oldest;
  const g=el('div','row sep');g.append(el('div','gut'),el('div','bd','older steps'));frag.append(g);
  log.prepend(frag);
  for(const t of dirty)sumUp(t);dirty.clear();
  let prev=null;for(const d of log.querySelectorAll('.day')){if(d.dataset.day===prev)d.remove();else prev=d.dataset.day}
  if(anchor)scrollBy(0,document.documentElement.scrollHeight-k);          // keep the anchored row put
  [lastSess,lastWho,lastDay]=was;curTurn.clear();for(const [s,t] of was[3])curTurn.set(s,t);
  more.disabled=!j.more;
  more.textContent=j.more?'Load earlier':'Start of the log';
  showJump();
}
more.onclick=earlier;
// a permalink (#m<id>): the row with that id, or the one it was folded into (a tool result lives in its call's row).
// The '*' view starts at the last turn, so it pages back until the row is loaded.
async function seek(id){
  if(!SINGLE)for(let n=0;n<30&&before!=null&&id<before&&!more.disabled;n++)await earlier();
  let r=null;for(const x of log.querySelectorAll('.row[data-mid]')){if(+x.dataset.mid<=id)r=x;else break}
  if(!r)return false;
  if(r._turn&&r._turn.folded&&r.classList.contains('step'))setFold(r._turn,false,false);
  log.querySelector('.linked')?.classList.remove('linked');r.classList.add('linked');
  pinned=false;r.scrollIntoView({block:'center'});replay(r,'flash');showJump();return true;
}
function fromHash(){const x=/^#m(\d+)$/.exec(location.hash);if(x)seek(+x[1])}
addEventListener('hashchange',fromHash);
function showJump(){
  if(atBottom())unseen=0;
  jump.classList.toggle('on',!atBottom());
  jump.classList.toggle('unseen',unseen>0);
  jump.textContent=unseen?unseen+(unseen>1?' new steps':' new step')+' below':'Jump to latest';
}
jump.onclick=()=>{scrollTo({top:document.documentElement.scrollHeight,behavior:smooth});unseen=0;showJump()};
addEventListener('scroll',showJump,{passive:true});

// ---- live: the streamed turn (only for turns sent from this page) renders as a ghost row until hermes
// writes the real step; pend is the optimistic copy of a message until its user row lands.
let live=null, pend=null, gatewayUp=null;
const ghost=el('div','row oko ghost'), ghostBd=el('div','bd');
{const g=el('div','gut');g.append(el('span','who',AG));ghost.append(g,ghostBd);ghost.hidden=true;$('tail').before(ghost)}
const running=lv=>!!lv&&(lv.status==='running'||lv.status==='starting');
// what the model is doing now: wait = nothing back yet (queue, prefill, or reasoning hermes doesn't stream),
// think = reasoning text arriving, write = reply tokens, tool. Older servers send no stage: infer it.
const stageOf=lv=>lv.stage||(lv.tool?'tool':lv.text?'write':lv.thinking?'think':'wait');
const STAGES={wait:'waiting on the model',think:'thinking',write:'writing'};
const stageLabel=lv=>{const s=stageOf(lv);return s==='tool'?'running '+(lv.tool||'a tool'):STAGES[s]};
// A turn the page did NOT send (a chat platform, cron, the CLI) has no token stream here, so its stage comes
// from the bundled hermes plugin instead (j.progress, present only while its stream is open). Same three
// stages, plus how much has come back and the timer; the text itself is never sent. stage_ts rides along, so
// ticking needs no new request. `turn.progress` is rebuilt on every payload, so it clears itself.
const stageLine=()=>{
  if(running(live)){const s0=live.stage_ts||live.started, what=stageLabel(live), t=dur(Date.now()/1000-s0);
    return [what+' for '+t, what+' for '+t+(s0-live.started>=1?', turn '+dur(Date.now()/1000-live.started):'')]}
  if(!turn)return null;
  const p=turn.progress, tool=turn.phase!=='model';
  const what=!p?tool?'running '+turn.phase.slice(5):'waiting on the model'
    :p.stage==='think'?'thinking ('+kfmt(p.reasoning_chars||0)+' chars)'
    :p.stage==='write'?'writing ('+kfmt(p.text_chars||0)+' chars)':'waiting on the model';
  const s0=p&&p.stage_ts?p.stage_ts:turn.since_ts, t=dur(Date.now()/1000-s0);
  return [what+' for '+t, what+' for '+t+', turn started '+turn.since];
};
function renderLive(lv){
  live=lv||null;
  syncStop();
  let show=false;
  if(running(live)&&(live.text||live.thinking||stageOf(live)!=='tool')){ // a tool shows on its own row
    const m=lastMsg; // hide once hermes has written the same reply to the log
    show=!(live.text&&m&&m.role==='assistant'&&m.content&&m.ts>=live.started&&m.content.startsWith(live.text.slice(0,60)));
  }
  if(show){
    ghostBd.replaceChildren();
    const s=stageOf(live);
    if(live.thinking&&!live.text)ghostBd.append(el('div','think',live.thinking.slice(-600)));
    if(live.text)ghostBd.append(md(live.text,'reply'));
    if(s!=='write'){const r=el('div','run '+s);r.append(wisp(),el('span','shimmer',stageLabel(live)));ghostBd.append(r)}
  }
  ghost.hidden=!show;B.classList.toggle('streaming',show);
  if(pend&&running(live)&&live.run_id&&!pend.row.classList.contains('failed')&&!pend.st.classList.contains('ok')){pend.st.className='st ok';pend.st.textContent='delivered'}
  if(pend&&live&&live.started>=pend.t0-5){ // only this message's run, not a finished one still in the overlay
    if(live.status==='done'&&!pend.row.classList.contains('failed'))clearPend(); // landed; its row is in the log
    else if(live.status==='failed')failPend(live.error||'the turn failed');
    else if(live.status==='cancelled')failPend('stopped before '+AG+' replied');
  }
}
function makePend(text,imgs){
  clearPend();
  const r=el('div','row in mine pend'), g=el('div','gut'), bd=el('div','bd');
  g.append(el('span','who',CFG.owner||'you'),el('span','ch','page'),el('span',null,hhmm(Date.now()/1000)));
  bd.append(md(text));
  if(imgs&&imgs.length){const w=el('div','imgs');for(const u of imgs){const i=el('img');i.src=u;i.alt='attached image';w.append(i)}bd.append(w)}const st=el('span','st shimmer','sending to '+AG+'…');bd.append(st);r.append(g,bd);r.classList.add('enter');
  ghost.before(r);pend={text,row:r,st,t0:Date.now()/1000};toBottom();
}
function clearPend(){if(pend){pend.row.remove();pend=null}}
function failPend(why){
  if(!pend||pend.row.classList.contains('failed'))return;
  pend.row.classList.add('failed');pend.st.className='st';pend.st.textContent='Not delivered: '+why+'. ';
  const t=pend.text, b=el('button','tgl','Put it back in the box');b.type='button';
  b.onclick=()=>{msgBox.value=t;clearPend();msgBox.focus();slashUpdate()};pend.st.append(b);
}
function setGateway(up){
  if(up===gatewayUp)return;gatewayUp=up;
  B.classList.toggle('offline',up===false);
  if(up===false)cnote(AG+' is offline: its gateway isn\'t running, so messages can\'t be sent. Everything here is still readable.',true);
  else if(cnotes.dataset.gw)cnote('');
  cnotes.dataset.gw=up===false?'1':'';
  syncSend();
}

// ---- header stats
function renderStats(s){
  const tok=(s.input_tokens||0)+(s.output_tokens||0), cost=s.actual_cost_usd||s.estimated_cost_usd, st=$('stats');
  st.replaceChildren();
  if(s.model)st.append(s.model,tok?', ':'');
  if(tok){st.append(el('b',null,kfmt(tok)),' tokens')}
  if(cost)st.append(', $'+cost.toFixed(2));
  st.title=[s.message_count!=null&&s.message_count+' messages',s.tool_call_count!=null&&s.tool_call_count+' tool calls',
    s.input_tokens!=null&&kfmt(s.input_tokens)+' in',s.output_tokens!=null&&kfmt(s.output_tokens)+' out',
    s.cache_read_tokens&&kfmt(s.cache_read_tokens)+' cached',s.reasoning_tokens&&kfmt(s.reasoning_tokens)+' reasoning'].filter(Boolean).join(', ')+
    '. Click for where the context goes (t)';
  lastStats=s;
}

// ---- token panel: where the context goes (the agent's own /context), the biggest rows in it, totals, model switch
const ctxp=$('ctxp'), num=s=>+String(s).replace(/,/g,'');
let lastStats=null, ctxSeq=0, ctxTab='context', ctxReturn=null;
function parseContext(t){
  t=t.replace(/[*`]/g,'');
  const g=re=>(t.match(re)||[]).slice(1), [mark,used,win]=g(/In use:\s*(~?)\s*([\d,]+)\s*\/\s*([\d,]+)/),
    [approx]=g(/Estimated context:\s*~\s*([\d,]+)\s*tokens/);
  if(used==null&&approx==null)return null;
  if(used!=null&&!(num(win)>0))return null;
  return {used:num(used??approx),win:used==null?null:num(win),estimated:!!mark||used==null,
    auto:num(g(/Auto-compresses at:\s*([\d,]+)/)[0]||g(/Over auto-compression threshold \(([\d,]+)/)[0]||0),comp:g(/Compressions this session:\s*(\d+)/)[0],
    cats:[...t.matchAll(/^\S+\s+([A-Z][A-Za-z ]*?)\s*~\s*([\d,]+)\s*tokens\s*~\s*[\d.]+%/gm)].map(m=>({name:m[1],tok:num(m[2])})),
    files:[...t.matchAll(/^([✓◐○✗⚠])\uFE0F?[ \t]+([^\n]+?)[ \t]+~[ \t]*([\d,]+)[ \t]*tokens(?:[ \t]+\(([^\n]*)\))?[ \t]*$/gm)]
      .map(m=>({name:m[2].trim(),tok:num(m[3]),status:({'✓':'Loaded','◐':'Shortened','○':'Not loaded','✗':'Unavailable','⚠':'Flagged'})[m[1]],note:m[4]||''}))};
}
function parseModels(t){
  const cur=t.match(/Current:\s*`([^`]+)`/), provs=[];let p=null;
  for(const line of t.split('\n')){
    const h=line.match(/^\s*\*\*(.+?)\*\*\s+`--provider\s+([^`\s]+)`(.*)/);
    if(h){p={label:h[1],slug:h[2],current:/current/.test(h[3]),models:[],more:(line.match(/\(\+(\d+) more\)/)||[])[1]};provs.push(p);continue}
    if(p&&/^\s+`/.test(line)){p.models.push(...[...line.matchAll(/`([^`]+)`/g)].map(m=>m[1]));p.more=(line.match(/\(\+(\d+) more\)/)||[])[1]||p.more}
  }
  return {current:cur&&cur[1],provs:provs.filter(p=>p.models.length)};
}
// one stacked bar: parts [{name,tok}], scaled to `of` tokens; a mark at `at` (the auto-compact line)
function bar(parts,of,at){
  const b=el('div','cbar');
  const scale=Math.max(of,parts.reduce((a,p)=>a+p.tok,0),1);
  parts.forEach((p,i)=>{const s=el('i');s.style.width=(100*p.tok/scale)+'%';s.style.setProperty('--c','var(--c'+(p.color??i)%6+')');s.title=p.name+': ~'+kfmt(p.tok);b.append(s)});
  if(at){const m=el('b');m.style.left=Math.min(100,100*at/Math.max(of,1))+'%';m.title='auto-compacts at '+kfmt(at);b.append(m)}
  const lg=el('ul','clegend');
  parts.forEach((p,i)=>{const li=el('li');li.style.setProperty('--c','var(--c'+(p.color??i)%6+')');li.append(el('span',null,p.name),el('span','n','~'+kfmt(p.tok)));lg.append(li)});
  return [b,lg];
}
function ctxSection(card,title){const s=el('section');s.append(el('h3',null,title));card.append(s);return s}
function closeCtx(){ctxp.hidden=true;ctxSeq++;if(ctxReturn&&ctxReturn.isConnected)ctxReturn.focus()}
function ctxNotice(section,message){section.replaceChildren(section.querySelector('h3'),el('p','dim',message))}
function openCtx(refresh=false){
  const card=ctxp.querySelector('.card'), main=cur===CFG.main, seq=++ctxSeq, current=()=>seq===ctxSeq;
  if(ctxp.hidden)ctxReturn=document.activeElement;
  if(!refresh)ctxTab='context';
  const heading=el('div','ctx-head'), title=el('div'), actions=el('div','ctx-actions'), reload=el('button','ctx-action','↻ Refresh'), close=el('button','ctx-action','×');
  title.append(el('h2',null,'Context and usage'),el('p','dim',AG+' · '+(main?'Main session':cur==='*'?'All sessions':'Selected session')));
  reload.type=close.type='button';reload.setAttribute('aria-label','Refresh context');close.setAttribute('aria-label','Close context panel');
  reload.onclick=()=>openCtx(true);close.onclick=closeCtx;actions.append(reload,close);heading.append(title,actions);
  const tabs=el('div','ctx-tabs'), view=el('div','ctx-view'), totals=el('div','ctx-view'), foot=el('p','ctx-foot','Reading session details…');
  tabs.setAttribute('role','tablist');tabs.setAttribute('aria-label','Usage view');
  const pages={context:view,totals};
  const select=name=>{ctxTab=name;for(const [k,p] of Object.entries(pages)){p.hidden=k!==name;const b=tabs.querySelector('[data-tab="'+k+'"]');b.setAttribute('aria-selected',String(k===name));b.tabIndex=k===name?0:-1}};
  for(const [k,label] of [['context','Context'],['totals','Session totals']]){
    const b=el('button','ctx-tab',label);b.type='button';b.dataset.tab=k;b.id='ctx-tab-'+k;b.setAttribute('role','tab');b.setAttribute('aria-controls','ctx-view-'+k);
    pages[k].id='ctx-view-'+k;pages[k].setAttribute('role','tabpanel');pages[k].setAttribute('aria-labelledby',b.id);
    b.onclick=()=>select(k);b.onkeydown=e=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){e.preventDefault();const next=e.key==='Home'?'context':e.key==='End'?'totals':k==='context'?'totals':'context';select(next);tabs.querySelector('[data-tab="'+next+'"]').focus()}};
    tabs.append(b);
  }
  card.replaceChildren(heading,tabs,view,totals,foot);select(ctxTab);ctxp.hidden=false;
  (refresh?reload:close).focus();
  const gauge=ctxSection(view,'Context window');gauge.className='ctx-window';gauge.setAttribute('aria-live','polite');
  gauge.append(el('p','dim',main?'Reading context…':'Window details are available in the main session.'));
  const upper=el('div','ctx-grid'), lower=el('div','ctx-grid ctx-lower');view.append(upper,lower);
  const cats=ctxSection(upper,'Where the context goes'), files=ctxSection(upper,'Context files'), conv=ctxSection(lower,'Conversation, by kind'), big=ctxSection(lower,'Biggest in context');
  cats.className='ctx-categories';files.className='ctx-files';
  for(const section of [cats,files])ctxNotice(section,main?'Reading context…':'Available in the main session.');
  ctxNotice(conv,'Reading active messages…');ctxNotice(big,'Reading active messages…');
  const tot=ctxSection(totals,'Across the whole session'), tools=ctxSection(totals,'Tool activity'), mod=main?ctxSection(totals,'Model'):null;
  tot.append(el('p','dim','Cumulative throughput: each call sends the context again, so these totals can exceed the window size.'));
  const s=lastStats||{}, dl=el('dl','ctot');
  for(const [k,v] of [['input',s.input_tokens],['output',s.output_tokens],['cached',s.cache_read_tokens],['reasoning',s.reasoning_tokens],
    ['messages',s.message_count],['tool calls',s.tool_call_count],['API calls',s.api_call_count],['cost',s.actual_cost_usd??s.estimated_cost_usd]])
    if(v!=null){const pair=el('div');pair.append(el('dt',null,k),el('dd',null,k==='cost'?'$'+v.toFixed(2):kfmt(v)));dl.append(pair)}
  tot.append(dl.children.length?dl:el('p','dim','Session totals are not available yet.'));
  const get=async path=>{const r=await fetch(qs(path));if(!r.ok)throw Error('unavailable');return r.json()};
  ctxNotice(tools,'Reading tool activity…');
  get('tools').then(j=>{
    if(!current())return;
    if(!j){ctxNotice(tools,'Tool activity is unavailable.');return}
    if(!j.tools.length){ctxNotice(tools,'No tool activity recorded.');return}
    tools.querySelector('p').remove();
    const t=el('table','ttools'), h=el('tr');
    for(const c of ['tool','calls','failed','median','slowest','total'])h.append(el('th',null,c));
    t.append(h);
    for(const x of j.tools){const r=el('tr');
      r.append(el('td',null,x.tool),el('td',null,String(x.n)),el('td',x.failed?'bad':null,x.failed?x.failed+' ('+Math.round(100*x.failed/x.n)+'%)':'0'),
        el('td',null,took(x.median)),el('td',null,took(x.max)),el('td',null,took(x.total)));t.append(r)}
    const scroll=el('div','ctx-table');scroll.tabIndex=0;scroll.setAttribute('role','region');scroll.setAttribute('aria-label','Tool activity table');scroll.append(t);
    tools.append(scroll,el('p','dim','This session, last '+j.days+' days. Time from call to result, slowest total first.'));
  }).catch(()=>{if(current())ctxNotice(tools,'Tool activity is unavailable.')});
  get('ctx').then(j=>{
    if(!current())return;
    foot.textContent='Message snapshot refreshed at '+new Date().toLocaleTimeString([],{hour:'numeric',minute:'2-digit'})+'. Sizes are estimates.';
    if(!j){ctxNotice(conv,'Conversation details are unavailable.');ctxNotice(big,'Largest messages are unavailable.');return}
    if(!j.rows){ctxNotice(conv,'No active messages yet.');ctxNotice(big,'No active messages yet.');return}
    conv.querySelector('p').remove();big.querySelector('p').remove();
    const p=j.parts, parts=['summary','you','replies','tool calls','tool output'].map((n,i)=>({name:n,tok:p[n],color:i})).filter(x=>x.tok), sum=parts.reduce((a,x)=>a+x.tok,0)||1;
    const kinds=el('ul','ctx-kinds');
    for(const part of parts){const li=el('li'), track=bar([part],sum)[0];track.setAttribute('aria-hidden','true');li.append(el('span',null,part.name),track,el('span','n','~'+kfmt(part.tok)));kinds.append(li)}
    conv.append(kinds,el('p','dim','Estimated from '+j.rows+' active messages (characters / 4).'+(p.reasoning?' Reasoning logged separately: ~'+kfmt(p.reasoning)+'.':'')));
    const ol=el('ol','cbig');
    for(const it of j.items){
      const li=el('li'), b=el('button');b.type='button';
      b.append(el('span','k',it.kind+(it.tool?' · '+it.tool:'')),el('span','n','~'+kfmt(it.tokens)),el('span','pv',it.preview||'(no text)'));
      b.onclick=()=>{const r=log.querySelector('[data-mid="'+it.id+'"]');if(r){closeCtx();goWell(r)}else{b.disabled=true;b.title='Not loaded on this page. Load earlier steps to reach this message.';b.append(el('span','pv','Load earlier steps to reach this message.'))}};
      li.append(b);ol.append(li);
    }
    big.append(ol);
  }).catch(()=>{if(current()){ctxNotice(conv,'Could not read the active messages.');ctxNotice(big,'Largest messages are unavailable.');foot.textContent='Could not refresh the conversation snapshot.'}});
  if(!main)return;
  const unavailable=message=>{ctxNotice(gauge,message);ctxNotice(cats,'Category estimates are unavailable.');ctxNotice(files,'Context-file details are unavailable.')};
  // Ask the server directly; cached socket discovery is not a reliable availability check.
  post('cmd',{session:cur,name:'context'}).then(j=>{
    if(!current())return;
    if(j.held){unavailable('Context request is waiting for the current turn. Refresh when it finishes.');return}
    const c=parseContext((j.messages||[]).join('\n'));
    if(!c){unavailable(j.done==='timeout'?'Context request timed out. Use Refresh to try again.':'Context details are not available yet. Refresh after the next turn.');return}
    gauge.querySelector('p').remove();
    const mark=c.estimated?'~':'', top=el('div','ctx-reading'), head=el('p','cbig-n'), source=el('div','ctx-source');
    head.append(el('b',null,mark+kfmt(c.used)),c.win?' of '+kfmt(c.win)+' tokens':' tokens');
    if(c.win)source.append(el('span','ctx-percent',Math.round(100*c.used/c.win)+'%'));
    source.append(el('span','ctx-badge',c.estimated?'Estimated':'Reported'));top.append(head,source);gauge.append(top);
    if(c.win){const meter=bar([{name:'In use',tok:Math.min(c.used,c.win)}],c.win,c.auto)[0];
      meter.setAttribute('role','meter');meter.setAttribute('aria-label','Context window used');meter.setAttribute('aria-valuemin','0');meter.setAttribute('aria-valuemax',String(c.win));meter.setAttribute('aria-valuenow',String(Math.min(c.used,c.win)));meter.setAttribute('aria-valuetext',mark+kfmt(c.used)+' of '+kfmt(c.win)+' tokens');
      gauge.append(meter);const labels=el('div','ctx-scale');labels.append(el('span',null,'0'),el('span',null,c.auto?'Auto-compact at '+kfmt(c.auto):'Window limit'),el('span',null,kfmt(c.win)));gauge.append(labels)}
    const room=el('div','ctx-headroom');
    if(c.auto){const over=c.used>=c.auto;gauge.classList.toggle('ctx-near',c.used>=c.auto*.9);room.append(el('strong',null,over?'Auto-compact threshold reached':mark+kfmt(c.auto-c.used)+' tokens until auto-compact'))}
    else if(c.win)room.append(el('strong',null,mark+kfmt(Math.max(0,c.win-c.used))+' tokens to the window limit'));
    if(c.comp!=null)room.append(el('span','dim',c.comp==='0'?'No compactions this session':'Compacted '+c.comp+'× this session'));
    gauge.append(room);
    gauge.append(el('p','dim',c.estimated?'Estimated from the available context, not a provider token count.': 'Last reported prompt usage.'));
    const parts=c.cats.filter(x=>!/^Free/.test(x.name));
    if(parts.length){cats.querySelector('p').remove();const [split,legend]=bar(parts,parts.reduce((a,p)=>a+p.tok,0)), note=el('p','dim','Estimated tokens by category. Select one for details.');
      for(const [i,li] of [...legend.children].entries()){const b=el('button','ctx-category');b.type='button';b.setAttribute('aria-pressed','false');b.append(...li.childNodes);li.append(b);
        b.onclick=()=>{legend.querySelectorAll('button').forEach(x=>x.setAttribute('aria-pressed',String(x===b)));note.textContent=/system/i.test(parts[i].name)?'Instructions, memory and loaded context files. Files below are part of this estimate.':/tool/i.test(parts[i].name)?'Tool descriptions and input schemas available to the model.':/conversation/i.test(parts[i].name)?'Active messages and tool results. Explore the split below.':'Estimated tokens for '+parts[i].name+'. Category estimates may differ from reported usage.'}};
      cats.append(split,legend,note)}
    else ctxNotice(cats,'Category estimates are not available for this snapshot.');
    if(c.files.length){files.querySelector('p').remove();const details=el('details'), list=el('ul','ctx-file-list');
      details.append(el('summary',null,c.files.length+' context '+(c.files.length===1?'file':'files')));
      for(const f of c.files){const li=el('li');li.append(el('span',null,f.name),el('span','n','~'+kfmt(f.tok)),el('small','dim',f.status+(f.note?' · '+f.note:'')));list.append(li)}
      details.append(list);files.append(details,el('p','dim','Loaded files are included in the system prompt above.'))}
    else ctxNotice(files,'No context-file details in this snapshot.');
  }).catch(()=>{if(current())unavailable('Context details could not be reached. Use Refresh to try again.')});
  loadModels(mod);
}
function loadModels(mod){
  mod.replaceChildren(el('h3',null,'Model'),el('p','dim','loading…'));
  post('cmd',{session:cur,name:'model'}).then(j=>{
    const t=(j.messages||[]).join('\n'), m=parseModels(t);mod.querySelector('p').remove();
    if(!m.provs.length){mod.append(el('p','dim',m.current?'Current model: '+m.current:'Model choices are unavailable.'));return}
    for(const p of m.provs){
      const row=el('div','cmods');row.append(el('span','k',p.label));
      for(const name of p.models){
        const b=el('button','chip'+(p.current&&name===m.current?' on':''),name);b.type='button';
        b.onclick=()=>{mod.querySelectorAll('.chip').forEach(c=>c.disabled=true);$('ctx-tab-totals').focus();
          post('cmd',{session:cur,name:'model',args:name+' --provider '+p.slug}).then(r=>{loadModels(mod);mod.append(el('p','dim',r.held?'Model change is waiting for the current turn.':'Model change requested.'))})
            .catch(()=>{loadModels(mod);mod.append(el('p','bad','Could not switch model. Try again.'))})};
        row.append(b);
      }
      if(p.more)row.append(el('span','dim','+'+p.more+' more'));
      mod.append(row);
    }
    mod.append(el('p','dim','Switches this session only.'));
  }).catch(()=>ctxNotice(mod,'Model choices could not be reached.'));
}
$('stats').onclick=()=>openCtx();
ctxp.onclick=e=>{if(e.target===ctxp)closeCtx()};
ctxp.onkeydown=e=>{
  if(e.key==='Escape'){e.preventDefault();e.stopPropagation();closeCtx();return}
  if(e.key!=='Tab')return;
  const items=[...ctxp.querySelectorAll('button:not(:disabled),summary,[tabindex="0"]')].filter(n=>n.tabIndex!==-1&&n.offsetParent), first=items[0], last=items[items.length-1];
  if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus()}
  else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus()}
};
// What the agent keeps between turns (its own files, read-only) + the writes it staged for your approval
const brw=$('brw');
let brwTab=store.get('brw','memory');
const SECT={
  memory:j=>j.memory.length?j.memory.map(m=>md(m,'bent')):[el('p','dim','No memory notes.')],
  user:j=>j.user.length?j.user.map(m=>md(m,'bent')):[el('p','dim','No profile notes.')],
  skills:j=>{const out=[];let cat=null;
    for(const s of j.skills){
      if(s.category!==cat){cat=s.category;out.push(el('h3',null,cat||'uncategorised'))}
      const d=el('details','bent'), sm=el('summary');sm.append(el('b',null,s.name),el('span','dim',s.desc?' · '+s.desc:''));d.append(sm);
      d.ontoggle=()=>{if(!d.open||d._got)return;d._got=1;
        fetch('skill?name='+encodeURIComponent(s.name)).then(r=>r.json()).then(x=>d.append(x.text!=null?md(x.text.replace(/^---\n[\s\S]*?\n---\n/,'')):el('p','dim',x.error)))
          .catch(e=>d.append(el('p','dim',e.message)))};
      out.push(d)}
    return out.length?out:[el('p','dim','No skills found on disk.')]},
  cron:j=>j.cron.length?j.cron.map(c=>{const d=el('div','bent');
    d.append(el('b',null,c.name||c.id),el('span','dim',' · '+(c.schedule_display||'')+(c.enabled===false?' · paused':'')+(c.state?' · '+c.state:'')));
    const w=[];if(c.next_run_at)w.push('next '+new Date(c.next_run_at*1000||c.next_run_at).toLocaleString());
    if(c.last_run_at)w.push('last '+new Date(c.last_run_at*1000||c.last_run_at).toLocaleString()+(c.last_status?' ('+c.last_status+')':''));
    if(w.length)d.append(el('p','dim',w.join(' · ')));if(c.last_error)d.append(el('p','bad',c.last_error));
    if(c.prompt)d.append(fold(el('pre','bpre',c.prompt),c.prompt,6));return d}):[el('p','dim','No cron jobs.')],
  pending:j=>j.pending.length?j.pending.map(p=>{const d=el('div','bent');
    d.append(el('b',null,p.subsystem+' · '+p.action),el('span','dim',' · '+(p.origin||'')+(p.ts?' · '+ago(p.ts):'')+' · '+p.id),el('p',null,p.summary));
    const x=el('details');x.append(el('summary',null,'What it writes'),el('pre','bpre',p.detail));d.append(x);
    if(CFG.main&&SOCK){const a=el('div','bacts');
      for(const [v,l] of [['approve','Approve'],['reject','Reject']]){const b=el('button','chip',l);b.type='button';
        b.onclick=async()=>{a.querySelectorAll('button').forEach(c=>c.disabled=true);
          try{const r=await post('cmd',{session:CFG.main,name:p.subsystem,args:v+' '+p.id});a.replaceWith(el('p',r.held?'bad':'dim',said(r,p.subsystem+' '+v+' '+p.id)))}
          catch(e){a.replaceWith(el('p','bad',e.message))}};a.append(b)}
      d.append(a)}
    else d.append(el('p','dim','Approve or reject from '+AG+'\'s main session: /'+p.subsystem+' approve '+p.id));
    return d}):[el('p','dim','Nothing waiting on you.')],
};
async function openBrowse(){
  const card=brw.querySelector('.card');
  card.replaceChildren(el('h2',null,'What '+AG+' keeps'));brw.hidden=false;
  let j;try{j=await (await fetch('browse')).json()}catch(e){card.append(el('p','bad','Could not read it: '+e.message));return}
  if(!SOCK&&CFG.main)await loadCmds().catch(()=>{});
  const tabs=el('div','btabs'), f=el('input'), body=el('div','bbody');
  f.type='search';f.placeholder='Filter';f.oninput=()=>{const q=f.value.toLowerCase();for(const n of body.children)n.hidden=!!q&&n.tagName!=='H3'&&!n.textContent.toLowerCase().includes(q)};
  card.append(tabs,f,body);
  const show=k=>{brwTab=k;store.set('brw',k);for(const b of tabs.children)b.setAttribute('aria-pressed',String(b.dataset.k===k));body.replaceChildren(...SECT[k](j));f.oninput()};
  for(const [k,l] of [['memory','Memory'],['user','About you'],['skills','Skills'],['cron','Cron'],['pending','Waiting on you']]){
    const b=el('button','chip'+(k==='pending'&&j.pending.length?' hot':''),l+' '+j[k].length);b.type='button';b.dataset.k=k;b.onclick=()=>show(k);tabs.append(b)}
  show(SECT[brwTab]?brwTab:'memory');
}
$('browse').onclick=openBrowse;
brw.onclick=e=>{if(e.target===brw)brw.hidden=true};
// A blocked shell command waits for /approve in hermes' memory, which nothing here can see: offer the answers
// once a command-running tool has sat for a while in the main session. A wrong guess just gets "nothing to approve".
const appr=$('appr'), SHELLY=/terminal|shell|exec|bash|command|process/i;
function syncAppr(){
  const now=Date.now()/1000;
  const on=cur===CFG.main&&SOCK&&(running(live)?stageOf(live)==='tool'&&SHELLY.test(live.tool||'')&&now-(live.stage_ts||live.started)>=8
    :!!turn&&turn.phase!=='model'&&SHELLY.test(turn.phase)&&now-turn.since_ts>=8);
  if(appr.hidden===on)appr.hidden=!on;
}
// "held" = the agent queued it behind the running turn: that answer is NOT delivered, and the turn may be waiting on it
const said=(j,what)=>j.held?'/'+what+' is queued behind the running turn, not delivered. Answer in your chat app instead.'
  :(j.messages||[]).join('\n')||'Sent /'+what+'.';
for(const b of appr.querySelectorAll('button'))b.onclick=async()=>{
  const [name,...a]=b.dataset.cmd.split(' ');
  try{const j=await post('cmd',{session:cur,name,args:a.join(' ')});cnote(said(j,b.dataset.cmd),j.held)}
  catch(e){cnote(e.message,true)}};

// ---- status line
let baseTitle=document.title, unread=0, wasBusy=false, conn=true;
// The tab follows the agent while you're elsewhere: the title carries the status line, the favicon a dot
// (amber busy, blue compacting, green unread reply, gray offline). Built from favicon.svg, no HTML parsing.
const favLink=document.querySelector('link[rel=icon]'), DOT={busy:'#f2b33d',compact:'#6aa7ff',unread:'#4cc38a',off:'#8a8790'};
let favSvg=null, favKey='';
fetch('favicon.svg').then(r=>r.ok?r.text():null).then(t=>{favSvg=t;tab()}).catch(()=>{});
function tab(){
  const busy=conn&&!!B.dataset.phase, k=!conn||gatewayUp===false?'off':compacting?'compact':busy?'busy':unread?'unread':'';
  const t=(unread?'● ':'')+(busy?state.textContent+' · ':'')+baseTitle;
  if(document.title!==t)document.title=t;
  if(favSvg==null||k===favKey||!favLink)return;
  favKey=k;
  favLink.href=k?'data:image/svg+xml,'+encodeURIComponent(favSvg.replace('</svg>',
    '<circle cx="25" cy="7" r="6.5" fill="'+DOT[k]+'" stroke="#17161b" stroke-width="2"/></svg>')):'favicon.svg';
}
function tick(){tick1();tab()}
function tick1(){
  if(!ok)return;
  syncAppr();
  const prog=turn&&turn.progress;      // the plugin's live stage for a turn this page didn't send
  B.dataset.phase=compacting?'compact':running(live)?stageOf(live):prog?prog.stage
    :turn?(turn.phase==='model'?'wait':'tool'):'';
  if(compacting){
    state.textContent='compacting context…';tailText.textContent='compacting context: earlier turns are being summarised';
    B.classList.add('compacting');return;
  }
  B.classList.remove('compacting');
  const line=stageLine();
  if(line){state.textContent=line[0];tailText.textContent=line[1]}
  else state.textContent=gatewayUp===false?'offline, read-only':lastTs?'idle, last step '+hhmm(lastTs):'idle';
}
setInterval(tick,1000);
function notifyEdge(busy){
  if(wasBusy&&!busy){const r=[...log.querySelectorAll('.row.oko')].pop();if(r)replay(r,'landed')} // the turn landed
  if(wasBusy&&!busy&&document.hidden){
    unread++;tab();
    if(window.Notification&&Notification.permission==='granted'){
      const body=lastMsg&&lastMsg.role==='assistant'&&lastMsg.content?lastMsg.content.slice(0,140):'Turn finished';
      try{new Notification(AG+' replied',{body,tag:'oko-'+cur})}catch(e){}
    }
  }
  wasBusy=busy;
}
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&unread){unread=0;tab()}});
function handle(j){
  ok=true;conn=true;B.classList.add('ready');
  const stick=pinned;
  compacting=!!j.compacting;
  if(!j.waiting)stoppedTurn=null;
  turn=j.waiting&&j.since_ts!==stoppedTurn?j:null; // a turn we stopped can leave the DB mid-step: it's over, not busy
  if(turn&&cur===CFG.main&&!SOCK&&Date.now()-cmdsAt>20000)loadCmds().then(syncStop); // learn whether Stop can reach it
  if(j.title)baseTitle=AG+' · '+j.title;
  if('gateway' in j)setGateway(j.gateway);
  if(j.stats)renderStats(j.stats);
  append(j.rows);
  if(booting&&j.rows.length<200){ // first full drain done: older turns stay folded, the newest opens
    booting=false;
    const last=turns[turns.length-1];if(last&&!hideSteps)setFold(last,false,false);
    toBottom();
    if(location.hash)setTimeout(fromHash,0); // after this update's own scroll-to-bottom
  }
  renderLive('live' in j?j.live:live);
  const busy=!!j.waiting||running(live);
  B.classList.toggle('busy',busy&&!compacting);B.classList.toggle('live',!busy&&!compacting);
  notifyEdge(busy);syncSend();pump();
  if(stick)toBottom();
  tick();
}
function lost(){B.classList.remove('live','busy','compacting');B.dataset.phase='';state.textContent=ok?'connection lost, retrying':'connecting';conn=false;tab()}
async function poll(){ // fallback when EventSource is unavailable
  let more=false;
  try{const j=await (await fetch(qs('rows?after='+after))).json();handle(j);more=j.rows.length>=200}catch(e){lost()}
  setTimeout(poll,more?0:CFG.ms);
}
function stream(){
  // EventSource reconnects by itself and resends Last-Event-ID, so the server resumes from the last row id
  const es=new EventSource(qs('events?after='+after));
  es.onmessage=e=>handle(JSON.parse(e.data));
  es.onerror=lost;
}

// ---- compose: Enter sends, Shift+Enter breaks the line, "/" opens commands
const compose=$('compose'), msgBox=$('msg'), sendBtn=$('send'), cnotes=$('cnotes'), slash=$('slash');
function cnote(t,err){cnotes.textContent=t;cnotes.className=err?'err':''}
function syncSend(){qLabel()} // the box never locks: what can't go now waits in the queue (offline dims Send via CSS)
if(SINGLE){compose.hidden=false;B.classList.add('composing')} // a turn needs a concrete session, not '*'
// Reserve exactly the fixed composer's height, including queues, approvals, notes and safe area.
// CSS uses this only on phones; desktop spacing is unchanged.
new ResizeObserver(()=>{
  document.documentElement.style.setProperty('--compose-h',compose.getBoundingClientRect().height+'px');
}).observe(compose);
async function post(path,body){
  const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const j=await r.json().catch(()=>({}));
  if(!r.ok)throw Object.assign(new Error(j.error||('HTTP '+r.status)),{status:r.status,code:j.code});
  return j;
}
let askedNotify=store.get('asked-notify','');
function maybeAskNotify(){ // once, on a send (a user gesture), so a finished turn can reach you in another tab
  if(askedNotify||!window.Notification||Notification.permission!=='default')return;
  askedNotify='1';store.set('asked-notify','1');Notification.requestPermission().catch(()=>{});
}
async function say(text,list){
  const imgs=(list||[]).map(i=>i.url);
  await post('/say',{session:cur,message:text,images:imgs});
  makePend(text,imgs);maybeAskNotify();
}
// images: paste, drop or the paperclip; downscaled in the browser so a phone photo isn't 8 MB on the wire
let images=[];
const thumbs=$('thumbs'), fileIn=$('file');
function setImages(list){
  images=list.slice(0,4);thumbs.replaceChildren();thumbs.hidden=!images.length;
  images.forEach((im,i)=>{const t=el('div','thumb'), img=el('img'), x=el('button',null,'×');img.src=im.url;img.alt=im.name||'image';
    x.type='button';x.title='Remove';x.onclick=()=>setImages(images.filter((_,j)=>j!==i));t.append(img,x);thumbs.append(t)});
}
async function shrink(file){
  if(!/^image\/(png|jpeg|gif|webp)$/.test(file.type))throw new Error(file.name+' isn\'t a png, jpeg, gif or webp');
  const read=f=>new Promise((ok,no)=>{const r=new FileReader();r.onload=()=>ok(r.result);r.onerror=no;r.readAsDataURL(f)});
  if(file.size<1.2e6||file.type==='image/gif')return read(file);
  const bm=await createImageBitmap(file), k=Math.min(1,1600/Math.max(bm.width,bm.height)), c=document.createElement('canvas');
  c.width=Math.round(bm.width*k);c.height=Math.round(bm.height*k);c.getContext('2d').drawImage(bm,0,0,c.width,c.height);
  return c.toDataURL('image/jpeg',.85);
}
async function addFiles(files){
  for(const f of files){
    if(images.length>=4){cnote('Up to 4 images per message.',true);break}
    try{images.push({url:await shrink(f),name:f.name})}catch(e){cnote(e.message,true)}
  }
  setImages(images);
}
$('attach').onclick=()=>fileIn.click();
fileIn.onchange=()=>{addFiles([...fileIn.files]);fileIn.value=''};
msgBox.addEventListener('paste',e=>{const fs=[...(e.clipboardData?.files||[])].filter(f=>f.type.startsWith('image/'));if(fs.length){e.preventDefault();addFiles(fs)}});
if(SINGLE){
  addEventListener('dragover',e=>{if([...e.dataTransfer.types].includes('Files')){e.preventDefault();B.classList.add('dragging')}});
  addEventListener('dragleave',e=>{if(!e.relatedTarget)B.classList.remove('dragging')});
  addEventListener('drop',e=>{B.classList.remove('dragging');const fs=[...e.dataTransfer.files].filter(f=>f.type.startsWith('image/'));if(fs.length){e.preventDefault();addFiles(fs)}});
}
// ---- queue: a message (or a skill, or /retry: anything that starts a turn) sent while the agent is busy waits
// here instead of colliding with the running turn, then goes out by itself, oldest first, once the agent has
// been idle for a moment. Other commands (/status, /stop, /steer, /compress…) run straight away.
// ponytail: the queue's text survives a reload (localStorage, per session); attached images only live in this tab.
const queueEl=$('queue'), QK='queue:'+cur;
// Neither `accepted` nor `held` acknowledges application to a turn. The external plugin owns that pump;
// removal from its queue can also mean cancellation, not delivery. Keep a durable receipt BEFORE sending,
// even if the reply is lost. Never auto-resend or infer delivery from queue absence (including after Stop).
// Without a correlated application acknowledgement, only explicit dismissal clears these receipts.
// One storage write transfers a queued message to a receipt, so crash recovery cannot load both owners.
let queue=[],steers=[];
try{
  const saved=JSON.parse(store.get(QK,'[]')), valid=q=>q&&typeof q.text==='string';
  queue=(Array.isArray(saved)?saved:saved.queue||[]).filter(valid);
  steers=(Array.isArray(saved)?JSON.parse(store.get('steers:'+cur,'[]')):saved.steers||[]).filter(valid);
}catch(e){}
let sending=0, idleSince=0, qFail=null;
const agentBusy=()=>compacting||!!turn||running(live)||srvBusy||(!!pend&&!pend.row.classList.contains('failed'));
const steerable=()=>running(live)&&!!live.run_id;
// Any turn in the main session, wherever it came from (Zulip, a cron, this page), can be stopped or steered
// through the agent's own /stop and /steer over the socket; a page-sent turn uses its run id (works without it).
function viaSock(){return cur===CFG.main&&SOCK&&(!!turn||running(live))&&!compacting}
function canStop(){return steerable()||viaSock()}
// The explicit interrupt is reachable wherever a cancellation could be attempted, which is WIDER than Stop:
// it also covers a turn the page can't see in the transcript but the agent says it is busy with (srvBusy, from
// the agent's own queue reply), which is the state a queued message leaves the page in.
// That reply proves socket reachability even before the slash-command list has been loaded (SOCK).
function canForce(){return steerable()||(cur===CFG.main&&(srvBusy||SOCK&&!compacting&&(!!turn||running(live))))}
// Show or hide a control. `want` is whether it should be VISIBLE; the guard makes a no-op cheap, and
// `.hidden` is the inverse of what we want.
function setShown(node,want){const hide=!want;if(node.hidden!==hide)node.hidden=hide}
function syncStop(){
  const on=canStop(), onF=canForce(), wasWrong=$('stop').hidden===on;   // the shown state didn't match
  setShown($('stop'),on);setShown($('force'),onF);
  // settle the note a Stop click left, once the turn it stopped is really over
  if(wasWrong&&!on&&cnotes.textContent.startsWith('Stopping '))cnote('Stopped.');
  if(wasWrong)renderQ()
}
let stoppedTurn=null, lastEsc=0;
// Stop the running turn. Never touches a pending steer: a steer the agent already holds belongs to the agent,
// and dropping it here is exactly the silent loss this page must not do.
async function stopTurn(){
  if(steerable()){await post('/stop',{session:cur});cnote('Stopping '+AG+'…'+steerNote());return}
  const was=turn&&turn.since_ts, j=await post('/cmd',{session:cur,name:'stop',args:''});
  checkStop(j);
  if(was!=null){stoppedTurn=was;turn=null;syncStop();tick();pump()}
  cnote(((j.messages&&j.messages[0])||'Stopped.')+steerNote());
}
// What a stop leaves behind: a steer the agent has not applied yet. Said out loud, never dropped.
function steerNote(){
  return steers.length?(' '+steers.length+(steers.length>1?' steers have':' steer has')+' unconfirmed application; kept above.')
                      :'';
}
// The explicit force-interrupt: the ordinary Stop needs to know it can reach the turn (a run id we hold, or a
// live socket), and while it can't, there is no way to stop a wedged turn from the page at all. This tries
// the run-id path first, falling back only if our server positively says it never dispatched the cancel.
// A lost reply may hide a successful cancel. Do not cancel twice:
// the second /stop could hit the next queued turn. A held/unfinished socket command is NOT a stop ack.
function checkStop(j){
  if(j.error||j.held||j.done!=='finished')throw new Error(j.error||'Stop not acknowledged; it may still be pending');
}
async function forceInterrupt(){
  const f=$('force');if(f.disabled)return;f.disabled=true;const was=f.textContent;f.textContent='Interrupting…';
  const acked=[], failed=[];let fallback=true, unknown=false;
  if(steerable()){try{await post('/stop',{session:cur});acked.push('the turn\'s run')}catch(err){
    failed.push(err.message);fallback=err.status===409&&err.code==='oko_no_live_run';unknown=!fallback;
  }}
  if(!acked.length&&fallback&&cur===CFG.main&&(SOCK||srvBusy))try{
    const j=await post('/cmd',{session:cur,name:'stop',args:''});
    checkStop(j);acked.push('the agent\'s own /stop');
  }catch(err){failed.push(err.message)}
  f.disabled=false;f.textContent=was;
  if(acked.length)cnote('Interrupt requested via '+acked.join(' and ')+'.'+steerNote());
  else cnote((unknown?'Interrupt status unknown: ':'Couldn\'t interrupt: ')+failed.join('; '),true);
  syncStop();
}
async function steerTurn(text,q=null){
  if(!text.trim())throw new Error('usage: /steer <message>');
  const nextQueue=q?queue.filter(x=>x!==q):queue, nextSteers=[...steers,{text}];
  if(!saveQ(nextQueue,nextSteers)){
    const error="Couldn't save, not sent. Browser storage is unavailable or full.";
    if(!q)throw new Error(error); // the composer restores the original command
    q.saveError=error;renderQ();cnote(error,true);return;
  }
  // Commit the durable ownership transfer before dispatch; a failed write leaves the original queued.
  if(q&&queue[0]===q)qFail=null;
  queue=nextQueue;steers=nextSteers;renderQ();
  try{
    const j=steerable()?await post('/steer',{session:cur,message:text})
                         :await post('/cmd',{session:cur,name:'steer',args:text});
    if(j.error)throw new Error(j.error);
    cnote((j.held?'Steer queued. ':'Steer requested. ')+'Application is unconfirmed; kept above, never automatically resent.');
  }catch(err){
    cnote('Steer delivery is unconfirmed: '+err.message+'. Kept above; check the transcript before sending again.',true);
  }
}
function dropSteer(s){steers=steers.filter(x=>x!==s);saveQ();renderQ()}
const tryStop=()=>stopTurn().catch(err=>cnote('Couldn\'t stop: '+err.message,true));
const tryForce=()=>forceInterrupt().catch(err=>cnote('Couldn\'t interrupt: '+err.message,true));
addEventListener('keydown',e=>{ // Esc twice, anywhere, stops the turn; a single Esc keeps its usual meaning.
  // Ctrl (or Cmd) + Esc Esc is the explicit interrupt: it fires whichever cancellation path is reachable.
  if(e.key!=='Escape'||e.repeat)return;
  if(e.timeStamp-lastEsc<600){lastEsc=0;if(e.ctrlKey||e.metaKey)tryForce();else if(canStop())tryStop()}else lastEsc=e.timeStamp;
},true);
function startsTurn(text){
  if(!text.startsWith('/'))return true;
  const n=text.slice(1).split(/\s/)[0].toLowerCase();
  return n==='retry'||(CMDS||[]).some(c=>c.kind==='skill'&&c.name.toLowerCase()===n);
}
function saveQ(qs=queue,ss=steers){return store.set(QK,JSON.stringify({queue:qs.map(q=>({text:q.text})),steers:ss.map(s=>({text:s.text}))}))}
function qLabel(){const q=queue.length||sending||agentBusy();sendBtn.textContent=q?'Queue':'Send';
  sendBtn.title=q?AG+' is busy: this waits in the queue and goes when it\'s free (Enter)':'Send (Enter). Shift+Enter for a new line'}
// The agent's own queue (main session, over the socket): Zulip messages hermes queued behind the running turn
// or a compaction, and peers' DMs held for idle. None of it is in the transcript until its turn starts.
// The agent's own queue is the backend's acknowledgement of what is still PENDING there: an item in
// `items` has not run yet; absence does not acknowledge application. Two reads must not look alike, so an
// unreachable socket keeps the last known list and marks it stale rather than clearing the panel: a
// queued message must never silently vanish from view. Polled while hidden too (one cheap read-only GET),
// so a backgrounded tab has the queue the moment you look back at it.
let srvQ=[], srvQKey='[]', srvDown=false, srvBusy=false;
async function loadSrvQ(){
  if(!SINGLE||cur!==CFG.main){if(srvQ.length||srvDown||srvBusy){srvQ=[];srvDown=false;srvBusy=false;srvQKey='[]';renderQ()}return}
  let j=null;
  try{const r=await fetch(qs('queue'));if(r.ok)j=await r.json()}catch(e){j=null}
  const down=!j||j.socket!==true||!Array.isArray(j.items); // invalid/error reply is unknown, not "empty"
  const items=down?srvQ:(j.items||[]);
  const busy=down?srvBusy:!!j.busy;                   // the agent's own word on being mid-turn
  const k=JSON.stringify([down,items,busy]);if(k===srvQKey)return;
  srvQKey=k;srvDown=down;srvBusy=busy;srvQ=items;renderQ();syncStop();
}
setInterval(loadSrvQ,3000);loadSrvQ();
document.addEventListener('visibilitychange',loadSrvQ);
function srvRow(it){
  const r=el('div','qi srv'), body=it.peer?it.text.replace(/^\[(?:group )?DM from [^\]\n]*\]\s*/,''):it.text;
  r.title=(it.peer?it.from+': ':'')+(it.text_full||it.text); // a one-line chip: the full text (when clipped) rides the tooltip
  r.append(el('span','qk',it.where==='held'?'held':it.where==='next'?'next':'queued'),
           el('span','qk dim',it.page?'page':it.peer?it.from:'zulip'),
           el('span','qt',body.replace(/\s+/g,' ').trim()||'(images only)'));
  if(it.images)r.append(el('span','qk dim',it.images+(it.images>1?' images':' image')));
  if(it.ts)r.append(el('span','qk dim',hhmm(it.ts)));
  return r;
}
// The existing socket/gateway protocols have no correlated application ack: explicitly visible pending.
function steerRow(s){
  const r=el('div','qi steer'), b=(label,title,fn)=>{const x=el('button','qb',label);x.type='button';x.title=title;x.onclick=fn;return x};
  r.append(el('span','qk','steer'),el('span','qk dim','application unconfirmed'),
           el('span','qt',s.text.replace(/\s+/g,' ').trim()));
  r.append(b('dismiss','Stop showing it here (this does not cancel it: '+AG+' may still apply it)',
             ()=>dropSteer(s)));
  return r;
}
function renderQ(){
  const n=queue.length+srvQ.length+steers.length;
  queueEl.replaceChildren(...srvQ.map(srvRow),...steers.map(steerRow));queueEl.hidden=!n;B.style.setProperty('--qn',n);
  if(srvDown&&srvQ.length){
    const stale=el('div','qi srv stale');
    stale.append(el('span','qk','stale'),el('span','qt','the agent\'s queue isn\'t answering: these may already have run'));
    queueEl.append(stale);
  }
  queue.forEach((q,i)=>{
    const fail=q.saveError||(i===0&&qFail);
    const r=el('div','qi'+(fail?' failed':'')), b=(label,title,fn)=>{const x=el('button','qb',label);x.type='button';x.title=title;x.onclick=fn;return x};
    r.append(el('span','qk',q.saveError?"couldn't save, not sent":fail?'not sent':i===0?(srvQ.length?'then':'next'):'#'+(i+1+srvQ.length)),el('span','qt',q.text||'(images only)'));
    if(q.images&&q.images.length)r.append(el('span','qk dim',q.images.length+(q.images.length>1?' images':' image')));
    if(fail)r.append(b('retry',fail,()=>{q.saveError=null;if(i===0)qFail=null;renderQ();pump()}));
    if(canStop()&&q.text&&!q.text.startsWith('/')){
      if(!q.images?.length)r.append(b('steer now','Request a steer; keep a receipt until you confirm it in the transcript',async()=>{
        if(!queue.includes(q)||sending)return;
        // Transfer ownership synchronously, before Stop can make pump() see an idle turn.
        await steerTurn(q.text,q)}));
      r.append(b('interrupt','Stop the running turn and send this right away',async()=>{
        queue=[q,...queue.filter(x=>x!==q)];q.now=true;qFail=null;saveQ();renderQ();
        try{await stopTurn()}catch(err){q.now=false;cnote('Couldn\'t interrupt: '+err.message,true)}}));
    }
    r.append(b('edit','Take it back into the box',()=>{drop(q);msgBox.value=q.text;store.set(DK,q.text);if(q.images)setImages(q.images);msgBox.focus()}),
             b('×','Remove it from the queue',()=>drop(q)));
    queueEl.append(r);
  });
  qLabel();
}
function drop(q){if(queue[0]===q)qFail=null;queue=queue.filter(x=>x!==q);saveQ();renderQ()}
async function dispatch(q){
  sending++;qLabel();
  try{if(q.text.startsWith('/'))await command(q.text);else await say(q.text,q.images)}
  finally{sending--;qLabel()}
}
function pump(){
  qLabel();
  if(!queue.length||qFail||queue[0].saveError||sending||gatewayUp===false||agentBusy()){idleSince=0;return}
  if(queue[0].now){idleSince=Date.now()-1500}                       // an interrupt: no settling, it goes now
  else if(!idleSince){idleSince=Date.now();setTimeout(pump,1600);return}   // let a turn that just ended settle first
  if(Date.now()-idleSince<1500)return;
  idleSince=0;const q=queue[0];
  dispatch(q).then(()=>{drop(q);setTimeout(pump,0)},err=>{qFail=err.message;renderQ();cnote('The queued message wasn\'t sent: '+err.message,true)});
}
compose.onsubmit=async e=>{
  e.preventDefault();
  const text=msgBox.value.trim();
  if(!text&&!images.length)return;
  if(gatewayUp===false&&!/^\/(help|snip)\b/.test(text)){cnote(AG+' is offline, so this can\'t be sent yet. What you typed is still in the box.',true);return}
  closeSlash();
  if(text.startsWith('/'))await loadCmds();    // to know whether it starts a turn
  const q={text,images:images.slice()};
  msgBox.value='';setImages([]);remember(text);
  if(startsTurn(text)&&(queue.length||sending||agentBusy())){
    queue.push(q);saveQ();renderQ();
    cnote(queue.length>1?queue.length+' queued. They go to '+AG+' one at a time.':'Queued. It goes to '+AG+' as soon as it\'s free.');
    return;
  }
  try{await dispatch(q);if(!text.startsWith('/'))cnote('')}
  catch(err){
    if(!msgBox.value){msgBox.value=text;setImages(q.images)}   // never lose what was typed
    cnote('Couldn\'t send: '+err.message,true);
  }
};
$('stop').onclick=tryStop;
$('force').onclick=tryForce;

// slash commands: the gateway's chat API takes "/x" as plain text, so the server maps each one to a real call
const LOCAL=[{name:'help',args:'',desc:'List the commands that work here',kind:'cmd'},
  {name:'snip',args:'[name text]',desc:'Your saved prompts: /snip lists them, /snip <name> <text> saves one',kind:'cmd'}];
// snippets: saved prompts, this browser only (localStorage). They show in the / menu by name and insert their text.
let snips=[];try{snips=JSON.parse(store.get('snips','[]')).filter(s=>s&&typeof s.name==='string'&&typeof s.text==='string')}catch(e){}
const saveSnips=()=>store.set('snips',JSON.stringify(snips));
function snipCmd(args){
  const sp=args.search(/\s/), name=sp<0?args:args.slice(0,sp), text=sp<0?'':args.slice(sp+1).trim();
  if(!name){
    card('snip',x=>{x.append(el('h4',null,'Your snippets'));
      if(!snips.length){x.append(el('div','d','None yet. /snip <name> <text> saves one; then type /<name> to use it.'));return}
      const u=el('ul');
      for(const s of snips){const li=el('li'), use=el('button','tgl','use'), del=el('button','tgl','delete');use.type=del.type='button';
        use.onclick=()=>editInto(s.text);del.onclick=()=>{snips=snips.filter(z=>z!==s);saveSnips();li.remove()};
        li.append(el('code',null,'/'+s.name),' ',el('span','d',s.text.length>80?s.text.slice(0,80)+'…':s.text),' ',use,' ',del);u.append(li)}
      x.append(u)});
    cnote('');return;
  }
  if(!/^[\w-]{1,32}$/.test(name))throw new Error('a snippet name is letters, digits, - or _ (up to 32)');
  if(!text){const s=snips.find(z=>z.name===name);if(!s)throw new Error('no snippet called '+name+'. Save one: /snip '+name+' <text>');editInto(s.text);return}
  snips=snips.filter(z=>z.name!==name).concat({name,text});saveSnips();
  cnote('Saved. Type /'+name+' to use it.');
}
let CMDS=null, SOCK=false, cmdsAt=0;
async function loadCmds(){
  // the real list comes from the agent's socket; without it we show the few REST-mapped ones and look again
  // after 20 s, so the menu picks the socket up once the agent is back
  if(CMDS&&(SOCK||Date.now()-cmdsAt<20000))return CMDS;
  try{const j=await (await fetch(qs('cmds'))).json();
    SOCK=!!j.socket;cmdsAt=Date.now();
    const cs=(j.commands||[]).filter(c=>c.name!=='help'&&c.name!=='skill').map(c=>({...c,kind:'cmd',category:c.category||''}));
    const cats=[...new Set(cs.map(c=>c.category))];   // keep each category together, in the order the agent lists them
    CMDS=[...LOCAL,...cats.flatMap(k=>cs.filter(c=>c.category===k)),
          ...(j.skills||[]).map(s=>({name:s.name,args:'[request]',desc:s.desc||'',kind:'skill'}))]}
  catch(e){return CMDS||LOCAL}
  return CMDS;
}
let opts=[], sel=0;
function slashUpdate(){
  const v=msgBox.value, m=/^\/([^\s]*)$/.exec(v);
  if(!m){closeSlash();return}
  const q=m[1].toLowerCase();
  loadCmds().then(cmds=>{
    if(msgBox.value!==v)return;
    const all=[...cmds,...snips.map(s=>({name:s.name,args:'',desc:s.text.replace(/\s+/g,' '),text:s.text,kind:'snip'}))];
    const pre=c=>c.name.startsWith(q)||(c.aliases||[]).some(x=>x.toLowerCase().startsWith(q));
    const a=all.filter(pre), b=all.filter(c=>!pre(c)&&(c.name.includes(q)||(q.length>2&&c.desc.toLowerCase().includes(q))));
    opts=[...a,...b].slice(0,60);sel=0;
    if(!opts.length){closeSlash();return}
    slash.replaceChildren();let lastKind=null;
    opts.forEach((c,i)=>{
      const grp=c.kind+(q===''&&c.category?'|'+c.category:'');   // category headers only while browsing; a search ranks by match
      if(grp!==lastKind){lastKind=grp;slash.append(el('div','hd',c.kind==='skill'?'Skills ('+AG+' loads the skill and runs your request)':
        c.kind==='snip'?'Your snippets (puts the text in the box)':(q===''&&c.category)||'Commands'))}
      const o=el('div','opt'+(c.kind==='skill'?' skill':''));o.setAttribute('role','option');o.id='opt'+i;
      const n=el('span','n','/'+c.name);if(c.args)n.append(' ',el('i',null,c.args));
      o.append(n,el('span','d',c.desc),el('span','k',i===0?'Tab':''));
      o.onmousedown=e=>{e.preventDefault();sel=i;accept()};
      slash.append(o);
    });
    slash.hidden=false;mark();
  });
}
function mark(){
  slash.querySelectorAll('.opt').forEach((o,i)=>{o.setAttribute('aria-selected',String(i===sel));o.querySelector('.k').textContent=i===sel?'Tab':''});
  const o=$('opt'+sel);if(o)o.scrollIntoView({block:'nearest'});
  msgBox.setAttribute('aria-activedescendant','opt'+sel);
}
function closeSlash(){slash.hidden=true;opts=[];msgBox.removeAttribute('aria-activedescendant')}
function accept(){
  const c=opts[sel];if(!c)return false;
  if(c.kind==='snip'){closeSlash();editInto(c.text);cnote('');return false}   // a snippet goes in the box to check first
  msgBox.value='/'+c.name+(c.args?' ':'');closeSlash();msgBox.focus();
  return !c.args; // nothing more to type: Enter can send straight away
}
msgBox.addEventListener('input',slashUpdate);
msgBox.onkeydown=e=>{
  if(!slash.hidden&&opts.length){
    if(e.key==='ArrowDown'){e.preventDefault();sel=(sel+1)%opts.length;mark();return}
    if(e.key==='ArrowUp'){e.preventDefault();sel=(sel-1+opts.length)%opts.length;mark();return}
    if(e.key==='Tab'){e.preventDefault();accept();return}
    if(e.key==='Escape'){e.preventDefault();closeSlash();return}
    if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();if(accept())compose.requestSubmit();return}
  }
  if(e.key==='ArrowUp'&&!msgBox.value&&queue.length&&hix<0){e.preventDefault();queueEl.lastChild.querySelector('.qb[title^="Take"]').click();return}
  if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();compose.requestSubmit()}
  else if(e.key==='Escape'){msgBox.blur()}
  else if((e.key==='ArrowUp'||e.key==='ArrowDown')&&sent.length&&(!msgBox.value||msgBox.value===sent[hix])){ // shell-style recall
    e.preventDefault();hix=Math.max(0,Math.min(sent.length,(hix<0?sent.length:hix)+(e.key==='ArrowUp'?-1:1)));
    msgBox.value=sent[hix]||'';if(hix>=sent.length)hix=-1;
  }
};
// the draft survives a reload; sent messages are recallable with the arrow keys (per session, last 30)
const DK='draft:'+cur, HK='sent:'+cur;
let sent=[];try{sent=JSON.parse(store.get(HK,'[]'))}catch(e){}
let hix=-1;
msgBox.value=store.get(DK,'');
msgBox.addEventListener('input',()=>{store.set(DK,msgBox.value);hix=-1});
function remember(t){sent=sent.filter(x=>x!==t).concat(t).slice(-30);store.set(HK,JSON.stringify(sent));store.set(DK,'');hix=-1}
function editInto(t){ // edit & resend: an earlier message back in the box (what was typed there is swapped out)
  if(compose.hidden)return;
  msgBox.value=t;msgBox.dispatchEvent(new Event('input'));msgBox.focus();msgBox.setSelectionRange(t.length,t.length);
  cnote('Edit it and send. It goes as a new message.');
}
// Discover the socket on load: Stop / Esc Esc must work before the composer is first focused.
loadCmds().then(syncStop);
// a command's answer is shown here only, as a card in the log (not part of the agent's transcript)
function card(name,fill){
  const r=el('div','row local'), g=el('div','gut'), bd=el('div','bd'), c=el('div','card');
  g.append(el('span','who','/'+name),el('span',null,'only you'));
  fill(c);bd.append(c);r.append(g,bd);r.classList.add('enter');ghost.before(r);toBottom();lastWho=null;
}
function scalarCard(c,obj,title){
  c.append(el('h4',null,title));
  const d=el('dl');
  for(const [k,v] of Object.entries(obj||{})){if(v==null||typeof v==='object'||v==='')continue;d.append(el('dt',null,k.replace(/_/g,' ')),el('dd',null,String(v)))}
  c.append(d.childElementCount?d:el('div','d','Nothing to show.'));
  const nested=Object.entries(obj||{}).filter(([,v])=>v&&typeof v==='object');
  if(nested.length)c.append(text(null,JSON.stringify(Object.fromEntries(nested),null,2),8));
}
// what a socket command answered: {messages:[text], held, turn, done}. Text goes through md() (textContent only).
function socketReply(name,j){
  if(j.messages.length)card(name,x=>j.messages.forEach(t=>x.append(md(t))));
  if(j.held)cnote(AG+' is busy; /'+name+' will run when it\'s free.');
  else if(j.done==='timeout')cnote('/'+name+(j.messages.length?' sent that much':' hasn\'t answered')+' in 90 s; it may still be running, see the transcript.');
  else if(j.turn){cnote('Sent. '+AG+' is working on it.');maybeAskNotify()}
  else cnote(j.messages.length?'':'/'+name+' ran.');
}
async function command(raw){
  const sp=raw.search(/\s/), name=(sp<0?raw.slice(1):raw.slice(1,sp)).toLowerCase(), args=sp<0?'':raw.slice(sp+1).trim();
  const all=await loadCmds(), c=all.find(x=>x.name.toLowerCase()===name||(x.aliases||[]).some(a=>a.toLowerCase()===name));
  if(!c)throw new Error(SOCK?'there\'s no /'+name+' here. Type / to see what works'
    :cur!==CFG.main?'/'+name+' only runs in '+AG+'\'s main session; switch to it':'commands need the oko-tail socket (agent offline?)');
  if(name==='help'&&c.kind==='cmd'){
    card('help',x=>{x.append(el('h4',null,'Commands that work here'));const u=el('ul');
      for(const k of all.filter(k=>k.kind==='cmd')){const li=el('li');li.append(el('code',null,'/'+k.name+(k.args?' '+k.args:'')),' ',el('span','d',k.desc));u.append(li)}
      x.append(u);const n=all.filter(k=>k.kind==='skill').length;
      if(n)x.append(el('div','d',n+' skills too: type / and a name.'))});
    cnote('');return;
  }
  if(name==='snip'&&c.kind==='cmd'){snipCmd(args);return}
  if(name==='steer'){await steerTurn(args);return}
  if(name==='stop'){await stopTurn();return}
  cnote('/'+name+'…');
  const j=c.kind==='skill'?await post('/cmd',{session:cur,name:'skill',args:c.name+(args?' '+args:'')})
                          :await post('/cmd',{session:cur,name,args});
  if(Array.isArray(j.messages)){socketReply(name,j);return}   // it ran in the agent itself, through its socket
  if(c.kind==='skill'){makePend('Use your `'+c.name+'` skill.'+(args?'\n\n'+args:''));cnote('');maybeAskNotify();return}
  if(name==='retry'){cnote('Sent your last message again.');maybeAskNotify();return}
  if(name==='status')card('status',x=>scalarCard(x,j.session||j,'This session'));
  else if(name==='model'&&!args)card('model',x=>scalarCard(x,j,'Models'));
  else if(name==='fork'){
    const id=(j.session&&j.session.id)||j.id;
    if(id){cnote('');card('fork',x=>{x.append(el('h4',null,'Forked'));const a=el('a',null,'Open the new session');a.href='?session='+encodeURIComponent(id);x.append(a)})}
    else cnote('Forked.');return;
  }
  else if(name==='title'){cnote('Renamed to "'+args+'".');baseTitle=AG+' · '+args;tab();return}
  else if(name==='model'){cnote('This session now uses '+args+'.');return}
  else if(name==='stop'){cnote('Stopping '+AG+'…');return}
  else if(name==='steer'){cnote('Sent. '+AG+' will see it after its current tool call.');return}
  cnote('');
}

// ---- search: across everything loaded (a single session loads whole), newest match first
const search=$('search'), q=$('q'), qn=$('qn');
let hits=[], hi=-1;
function openSearch(){search.hidden=false;q.focus();q.select()}
function closeSearch(){search.hidden=true;q.value='';runSearch();hitsEl.hidden=true;hitsEl.replaceChildren()}
// every session, from the server (newest 50); a hit opens its session at that message (#m<id>)
const hitsEl=$('hits');
async function searchAll(){
  const s=q.value.trim();if(s.length<2){qn.textContent='type 2 or more letters';return}
  hitsEl.hidden=false;if(!hitsEl.childElementCount)hitsEl.append(el('div','hd','Searching every session…'));
  let j;try{j=await (await fetch('search?q='+encodeURIComponent(s))).json()}
  catch(e){hitsEl.replaceChildren(el('div','hd','The search failed. Try again'));return}
  if(q.value.trim()!==s||hitsEl.hidden)return;
  const n=j.hits.length;
  hitsEl.replaceChildren(el('div','hd',n?(n>=50?'Newest 50 matches':n+(n>1?' matches':' match'))+' across every session':'Nothing in any session'));
  for(const h of j.hits){
    const a=el('a','ah'), top=el('div','am'), sn=el('div','asn'), i=h.snippet.toLowerCase().indexOf(s.toLowerCase());
    a.href='?session='+encodeURIComponent(h.session)+'#m'+h.id;
    const who=h.role==='assistant'?AG:h.role==='tool'?(h.tool||'tool')+' output':h.role==='user'?'message':h.role;
    top.append(el('span','as',h.session===cur?'this session':h.title||h.session),
               el('span',null,who+(h.where==='calls'?', tool call':h.where==='thinking'?', thinking':'')),el('span',null,ago(h.ts)));
    if(i<0)sn.textContent=h.snippet;else sn.append(h.snippet.slice(0,i),el('mark',null,h.snippet.slice(i,i+s.length)),h.snippet.slice(i+s.length));
    a.append(top,sn);
    if(h.session===cur)a.onclick=e=>{e.preventDefault();history.replaceState(null,'','#m'+h.id);seek(h.id)};
    hitsEl.append(a);
  }
}
$('qall').onclick=searchAll;
function runSearch(keep){
  const s=q.value.trim().toLowerCase(), was=hits[hi];
  for(const r of hits)r.classList.remove('hit','now');hits=[];hi=-1;
  if(s.length<2){qn.textContent='';return}
  hits=[...log.querySelectorAll('.row')].filter(r=>r._q&&r._q.includes(s));
  hits.forEach(r=>r.classList.add('hit'));
  if(!hits.length){qn.textContent='none here, Enter looks everywhere';return}
  const i=keep&&was?hits.indexOf(was):-1;
  if(i>=0){hi=i;was.classList.add('now');qn.textContent=(hi+1)+' of '+hits.length}else goHit(hits.length-1);
}
function goHit(i){
  if(!hits.length)return;
  hits[hi]?.classList.remove('now');hi=(i+hits.length)%hits.length;
  const r=hits[hi];r.classList.add('now');
  if(r._turn&&r._turn.folded&&r.classList.contains('step'))setFold(r._turn,false,false);
  r.scrollIntoView({block:'center'});qn.textContent=(hi+1)+' of '+hits.length;
}
let qt;q.addEventListener('input',()=>{clearTimeout(qt);qt=setTimeout(()=>{runSearch();if(!hitsEl.hidden)searchAll()},120)});
q.onkeydown=e=>{
  if(e.key==='Enter'){e.preventDefault();if(!hits.length)searchAll();else goHit(hi+(e.shiftKey?1:-1))} // nothing here: look everywhere
  else if(e.key==='Escape'){e.preventDefault();closeSearch()}
};
search.onsubmit=e=>e.preventDefault();
$('find').onclick=()=>search.hidden?openSearch():closeSearch();
$('qx').onclick=closeSearch;

// ---- keyboard
function navTurn(d){
  const top=$('top').getBoundingClientRect().bottom+8, rs=[...log.querySelectorAll('.row.in')];
  const r=d>0?rs.find(r=>r.getBoundingClientRect().top>top+2):rs.reverse().find(r=>r.getBoundingClientRect().top<top-2);
  if(r){scrollTo({top:scrollY+r.getBoundingClientRect().top-top,behavior:smooth});replay(r,'flash')}
  else if(d>0)jump.onclick();
}
addEventListener('keydown',e=>{
  if(e.metaKey||e.ctrlKey||e.altKey||e.target.closest('input,textarea,select'))return;
  const keys=$('keys');
  switch(e.key){
    case 'j':navTurn(1);break;
    case 'k':navTurn(-1);break;
    case 's':stepsBtn.click();break;
    case 'f':themeBtn.click();break;
    case '/':openSearch();break;
    case 'c':if(compose.hidden)return;msgBox.focus();break;
    case 'g':scrollTo({top:0,behavior:smooth});break;
    case 'G':jump.onclick();break;
    case '?':keys.hidden=!keys.hidden;break;
    case 't':if(ctxp.hidden)openCtx();else closeCtx();break;
    case 'b':if(brw.hidden)openBrowse();else brw.hidden=true;break;
    case 'Escape':keys.hidden=true;if(!ctxp.hidden)closeCtx();brw.hidden=true;if(!search.hidden)closeSearch();break;
    default:return;
  }
  e.preventDefault();
});
$('keys').onclick=()=>{$('keys').hidden=true};
const themeBtn=$('theme'),root=document.documentElement;
// themes cycle default -> frost -> shelf; the button names the current one and shows the next one's icon
const THEMES=['','frost','shelf','shelf-dark'], nextTheme=()=>THEMES[(THEMES.indexOf(root.dataset.theme||'')+1)%THEMES.length];
function syncTheme(){const cur=root.dataset.theme||'default', nx=nextTheme()||'default';
  themeBtn.dataset.next=nx;themeBtn.setAttribute('aria-label','Theme: '+cur);themeBtn.title='Theme: '+cur+'. Switch to '+nx+' (f)';
  document.querySelector('meta[name=theme-color]').content=getComputedStyle(root).getPropertyValue('--ground').trim()||'#17161b'}
function flipTheme(){const nx=nextTheme();if(nx)root.dataset.theme=nx;else delete root.dataset.theme;
  store.set('theme',nx);syncTheme();snow.sync()}
themeBtn.onclick=()=>{
  if(!document.startViewTransition||smooth!=='smooth'){flipTheme();return}
  const b=themeBtn.getBoundingClientRect(), x=b.left+b.width/2, y=b.top+b.height/2, r=Math.hypot(Math.max(x,innerWidth-x),Math.max(y,innerHeight-y));
  // swapping: element colour transitions would otherwise replay inside the wipe (old colours fading in mid-way, which
  // reads as a stall); the curve is an even ease so the edge doesn't crawl through its last stretch off-screen
  root.classList.add('swapping');
  const vt=document.startViewTransition(flipTheme);
  vt.ready.then(()=>root.animate({clipPath:['circle(0 at '+x+'px '+y+'px)','circle('+r+'px at '+x+'px '+y+'px)']},
    {duration:480,easing:'cubic-bezier(.45,0,.25,1)',pseudoElement:'::view-transition-new(root)'})).catch(()=>{});
  vt.finished.finally(()=>root.classList.remove('swapping'));
};
// frost snow: drifting flakes on a canvas behind everything. Runs only in frost, with motion
// allowed and the tab visible; scrolling nudges the flakes (nearer = bigger = moves more) and a busy agent
// stirs up a little wind.
const snow=(()=>{
  const c=$('snow'), x=c.getContext('2d'), dark=matchMedia('(prefers-color-scheme: dark)');
  let W=0,H=0,flakes=[],raf=0,last=0,wind=0,sy=scrollY;
  function size(){
    const d=Math.min(2,devicePixelRatio||1);W=innerWidth;H=innerHeight;c.width=W*d;c.height=H*d;x.setTransform(d,0,0,d,0,0);
    const n=Math.round(Math.min(90,W*H/16000));
    while(flakes.length<n)flakes.push({x:Math.random()*W,y:Math.random()*H,r:Math.random()*1.6+.5,vy:Math.random()*.4+.15,
      vx:Math.random()*.3-.15,ph:Math.random()*Math.PI*2,a:Math.random()*.35+.15});
    flakes.length=n;
  }
  function frame(t){
    const k=Math.min(3,(t-(last||t))/16.7);last=t;   // per 60 Hz frame, so 120 Hz screens don't double the speed
    wind+=((B.classList.contains('busy')?.9:0)-wind)*.01*k;
    const dy=scrollY-sy;sy=scrollY;
    x.clearRect(0,0,W,H);x.fillStyle=dark.matches?'rgb(236,244,241)':'rgb(255,255,255)';
    for(const f of flakes){
      f.ph+=.01*k;f.x+=(f.vx+Math.sin(f.ph)*.25+wind*f.r*.5)*k;f.y+=f.vy*k-dy*f.r*.12;
      if(f.y>H+4){f.y=-4;f.x=Math.random()*W}else if(f.y<-4){f.y=H+4;f.x=Math.random()*W}
      if(f.x>W+4)f.x=-4;else if(f.x<-4)f.x=W+4;
      x.globalAlpha=f.a;x.beginPath();x.arc(f.x,f.y,f.r,0,6.2832);x.fill();
    }
    raf=requestAnimationFrame(frame);
  }
  function sync(){
    const on=root.dataset.theme==='frost'&&smooth==='smooth'&&!document.hidden;
    if(on&&!raf){size();last=0;sy=scrollY;raf=requestAnimationFrame(frame)}
    else if(!on&&raf){cancelAnimationFrame(raf);raf=0;x.clearRect(0,0,W,H)}
  }
  addEventListener('resize',()=>{if(raf)size()},{passive:true});
  document.addEventListener('visibilitychange',sync);
  return {sync};
})();
syncTheme();snow.sync();

if(SINGLE)renderQ();
after=SINGLE?0:-1;   // a single session is small enough to load whole; '*' starts at the last user turn
window.EventSource?stream():poll();

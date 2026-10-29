#!/usr/bin/env python3
"""One HTML file per project, inside its folder: <workspace>/<project>/app.html (git-excluded; never sent to ABI).

A sidebar with everything, by category:
  Overview   Home (readiness, data quality, latest changes)
  Analyze    every report (pages; filters; click a bar or point to cross-filter), Datasets (preview rows)
  Model      Measures (edit, test, add, delete), Model (diagram), Glossary (edit, add, delete)
  Data       Sources and Tables (refresh, data quality)
  Automate   Flows (run now with the trace, enable/disable), ML models (train, score, latest run), Tools (test, run),
             Agents (evaluation set, ask a question)
  Govern     Docs (ABI's generated documentation), Security, Environments, History
Opened from disk it is a snapshot; served by

    python scripts/app.py <project> [port]        # http://127.0.0.1:<port>/ (default 8790)

it works live: every click is an ABI action as the signed-in person (REST), so ABI's checks and confirmations apply.
The session token stays in this process; a confirm token stays here too: the page gets ABI's message and preview and
the person's click redeems it. Only the actions in ALLOWED pass; requests need the per-start key and a local Host.
Roles, users, secrets and remotes stay in ABI's app.
"""
import atexit
import json
import os
import re
import signal
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import abi  # noqa: E402
import render  # noqa: E402

ALLOWED = {"semantic_model", "semantic_query", "readiness", "rls_rules", "data_quality", "history", "docs", "refresh",
           "list_reports", "visual_data", "how_calculated", "dataset_data",
           "set_measure", "delete_measure", "set_glossary_term", "delete_glossary_term",
           "list_flows", "run_flow", "flow_runs", "enable_flow", "ml_models", "train_model", "score_model",
           "list_tools", "test_tool", "run_tool", "list_agents", "test_agent", "run_agent"}
KEY_SLOT = "__ABI_APP_KEY__"
READS_ONLY = {"run_flow", "run_agent"}  # not changes to the project, but they act (send, call tools): Developer only
WRITES = {"set_measure", "delete_measure", "set_glossary_term", "delete_glossary_term", "enable_flow", "train_model", "score_model", "refresh"}
STATIC = ["Model", "Sources", "Tables", "Security", "Environments"]  # explorer sections shown as they are

CSS = """
/* layout: a top bar (project, live state, the agent), a left rail grouped by what you want to do, one page at a time with
   its purpose and main actions up top; the agent opens as a drawer on the right and can steer the page */
body{display:grid;grid-template-columns:236px minmax(0,1fr);grid-template-rows:auto 1fr;min-height:100vh}
body::before{content:"";position:fixed;inset:0 0 auto;height:env(safe-area-inset-top,0px);z-index:10;background:var(--paper)}
header{grid-column:1/3;position:sticky;top:env(safe-area-inset-top,0px);z-index:6;min-height:60px}
header .sub{display:flex;gap:10px;align-items:center}.mode{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--mute)}
.mode.live{color:var(--ink)}.mode.live::before{content:"";display:inline-block;width:7px;height:7px;background:var(--ok);margin-right:6px;vertical-align:1px}
aside{border-right:2px solid var(--ink);padding:8px 0 40px;position:sticky;top:60px;height:calc(100vh - 60px);overflow:auto;background:var(--paper)}
aside .sec{margin-bottom:6px}aside .sec+.sec{border-top:1px solid var(--faint);padding-top:4px}
aside button.grp{display:flex;justify-content:space-between;align-items:center;width:100%;border:0;background:none;cursor:pointer;color:var(--ink);
 padding:10px 18px 8px;font:700 13px/1 var(--display);text-transform:uppercase;letter-spacing:.1em;text-align:left}
aside button.grp:hover{background:color-mix(in srgb,var(--ink) 8%,transparent)}
aside .car::before{content:"";display:inline-block;width:7px;height:7px;border-right:2px solid var(--ink);border-bottom:2px solid var(--ink);transform:rotate(45deg) translate(-2px,-2px);transition:transform .15s}
aside .folded .car::before{transform:rotate(-45deg)}aside .folded .items{display:none}aside .items{padding:2px 0 8px}
aside button.it{display:flex;justify-content:space-between;align-items:baseline;gap:8px;width:100%;text-align:left;border:0;background:none;color:var(--ink);
 padding:6px 18px 6px 30px;cursor:pointer;font:400 13px/1.4 var(--text)}
aside button.it:hover{background:color-mix(in srgb,var(--ink) 8%,transparent)}aside button.it.on{background:var(--ink);color:var(--paper);font-weight:600}
aside button.it .n{color:var(--mute);font-size:11px;font-variant-numeric:tabular-nums}aside button.it.on .n{color:var(--paper)}
aside button.it.sub{margin-left:30px;width:calc(100% - 30px);padding-left:12px;border-left:2px solid var(--faint);font-size:12px}aside button.it.sub.on{border-left-color:var(--ink)}
main{max-width:none;min-width:0;margin:0;align-self:start;width:100%;padding-top:32px}
.pghead{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;flex-wrap:wrap;padding-bottom:16px;margin-bottom:24px;border-bottom:2px solid var(--ink)}
.pghead h2{font:700 34px/1 var(--display);text-transform:uppercase;letter-spacing:.01em;margin:0}
.pg nav.tabs:has(button:only-child){display:none}.card .kpi:only-child .l{display:none}
.pghead p{margin:8px 0 0;color:var(--mute);max-width:64ch}.pghead .bar{margin:0}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px;align-items:start}
.card h3 .file{font:400 11px var(--text);text-transform:none;letter-spacing:0;text-align:right;margin:0}
.card h4{font:700 13px var(--display);margin:16px 0 6px;text-transform:uppercase;letter-spacing:.04em}.card>.file{margin:0 0 12px}
.btn{border:2px solid var(--ink);background:none;color:var(--ink);padding:6px 12px;cursor:pointer;font:600 11px/1.2 var(--text);text-transform:uppercase;letter-spacing:.08em}
.btn:hover{background:var(--ink);color:var(--paper)}.btn.pri{background:var(--ink);color:var(--paper)}.btn.pri:hover{background:var(--mute);border-color:var(--mute)}
.btn:disabled{opacity:.45;cursor:default}.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.card>.acts{margin-top:16px}.bar{margin:0 0 20px}
input.f,textarea.f,select.f{padding:8px 10px;border:2px solid var(--ink);background:var(--paper);color:var(--ink);font:inherit;font-size:13px;min-width:0;border-radius:0}
select.f{appearance:none;padding-right:28px;background-image:linear-gradient(45deg,transparent 50%,var(--ink) 50%),linear-gradient(135deg,var(--ink) 50%,transparent 50%);
 background-position:calc(100% - 15px) 50%,calc(100% - 10px) 50%;background-size:5px 5px;background-repeat:no-repeat}
.card>input.f,.card>textarea.f{display:block;width:100%;margin:6px 0 0}textarea.f{font:12px/1.5 var(--text);min-height:64px;resize:vertical}
input.f:focus,textarea.f:focus,select.f:focus{outline:none;box-shadow:inset 0 0 0 1px var(--ink);background:var(--sheet)}
label.l{display:block;font:600 10px var(--text);color:var(--mute);text-transform:uppercase;letter-spacing:.12em;margin:14px 0 0}
.msg{font-size:12px;margin:10px 0;white-space:pre-wrap}.msg:empty{display:none}.out{margin-top:14px}.out:empty{display:none}
.chip.x{cursor:pointer;background:var(--ink);color:var(--paper)}.chip.x:after{content:'  \00d7'}
dialog{border:2px solid var(--ink);background:var(--sheet);color:var(--ink);max-width:640px;padding:24px;border-radius:0}
dialog h3{margin:0 0 12px;font:700 18px var(--display);text-transform:uppercase}dialog .row{margin-top:18px}dialog::backdrop{background:color-mix(in srgb,var(--ink) 55%,transparent)}
.trace{margin:12px 0 0;padding-left:20px}.trace li{margin:3px 0}.trace:empty{display:none}
.stat{font:700 38px/1 var(--display);margin:4px 0 8px;text-transform:uppercase}
.kv{display:grid;grid-template-columns:auto 1fr;gap:6px 16px;font-size:12px}.kv b{font-weight:500;color:var(--mute)}
pre{margin:10px 0 0}.pg .grid{gap:16px}.pg section{margin-bottom:28px}.pg>.card{margin-bottom:16px}.pg>section>h2{display:none}
#chat{position:fixed;top:60px;right:0;bottom:0;width:420px;max-width:100vw;background:var(--sheet);border-left:2px solid var(--ink);display:flex;flex-direction:column;
 transform:translateX(100%);transition:transform .18s ease-out;z-index:7;padding-bottom:env(safe-area-inset-bottom,0px)}
#chat.open{transform:none}body.chat-open main{padding-right:452px}
#chat header{position:static;display:flex;align-items:center;gap:8px;min-height:0;padding:14px 18px;border-bottom:2px solid var(--ink);background:none}
#chat header h3{margin:0;font:700 16px var(--display);text-transform:uppercase;flex:1}
#chat .log{flex:1;overflow:auto;padding:18px;display:flex;flex-direction:column;gap:12px}
#chat .m{padding:10px 12px;font-size:13px;line-height:1.55;white-space:pre-wrap;max-width:94%;border:2px solid var(--ink)}
#chat .me{align-self:flex-end;background:var(--ink);color:var(--paper)}#chat .ai{align-self:flex-start;background:var(--paper)}
#chat .ai code{background:color-mix(in srgb,var(--ink) 8%,transparent);padding:0 3px}
#chat .sys{align-self:stretch;border:0;border-top:1px dashed var(--faint);color:var(--mute);font-size:11px;padding:8px 0 0;max-width:none}
#chat .steps{align-self:flex-start;border:0;border-left:2px solid var(--ink);color:var(--mute);font-size:11px;line-height:1.7;padding:2px 10px}
#chat .busy{color:var(--mute);font-size:11px;text-transform:uppercase;letter-spacing:.1em;padding:0 18px 10px}
#chat .compose{display:flex;gap:8px;padding:14px 18px 18px;border-top:2px solid var(--ink)}#chat textarea{flex:1;min-height:46px;max-height:160px;margin:0}
.brand{font:700 20px/1 var(--display);letter-spacing:.08em;padding:6px 10px;background:var(--ink);color:var(--paper)}
.crumb{font:600 13px var(--text);display:flex;gap:8px;align-items:baseline}.crumb i{font-style:normal;color:var(--mute)}.crumb b{font-weight:700}
.snap{font-size:10px;text-transform:uppercase;letter-spacing:.1em;border:1px dashed var(--mute);color:var(--mute);padding:2px 6px}
header .right{margin-left:auto;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.modes{display:flex;border:2px solid var(--ink)}.modes button{border:0;background:none;color:var(--ink);padding:5px 10px;font:600 11px var(--text);text-transform:uppercase;letter-spacing:.08em;cursor:pointer}
.modes button.on{background:var(--ink);color:var(--paper)}body[data-mode=viewer] .w{display:none}
body[data-mode=viewer] .ed{background:transparent;border-color:var(--faint)}
body[data-mode=viewer] .pghead{border-bottom-style:dashed}
.hits{display:flex;flex-direction:column;gap:10px;margin-top:12px}.hit{border-left:2px solid var(--ink);padding:2px 0 2px 12px;white-space:pre-wrap}.hit .file{margin:0 0 4px}
.toast{position:fixed;left:50%;bottom:calc(24px + env(safe-area-inset-bottom,0px));transform:translateX(-50%);background:var(--ink);color:var(--paper);padding:10px 16px;font-size:12px;z-index:9;max-width:calc(100vw - 32px)}
.card.hl{outline:4px solid var(--ink);outline-offset:3px}
@media (max-width:820px){body{grid-template-columns:1fr}header{grid-column:1}
 aside{position:static;height:auto;border-right:0;border-bottom:2px solid var(--ink);display:flex;flex-wrap:wrap;padding:10px max(16px,env(safe-area-inset-left,0px))}
 aside .sec{display:contents}aside button.grp{display:none}aside .folded .items,aside .items{display:contents}aside button.it,aside button.it.sub{width:auto;padding:5px 10px;margin:0;border:0}
 main{padding:20px max(16px,env(safe-area-inset-right,0px)) 48px max(16px,env(safe-area-inset-left,0px))}body.chat-open main{padding-right:16px}
 .cards{grid-template-columns:1fr}.pghead h2{font-size:26px}#chat{top:0}}
"""

JS = r"""
const D=window.APP, KEY='__ABI_APP_KEY__', LIVE=location.protocol.startsWith('http')&&!KEY.startsWith('__');
const $=(s,r=document)=>r.querySelector(s), h=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!==undefined)e.textContent=x;return e};
const wait=ms=>new Promise(z=>setTimeout(z,ms));
async function api(action,args){
 if(!LIVE)throw new Error('Snapshot: start `python scripts/app.py '+D.project+'` to work live.');
 const post=(u,b)=>fetch(u,{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify(b||{})}).then(r=>r.json());
 let r=await post('/api/'+action,{project:D.project,...args});
 if(r.confirm){if(!await ask(r.confirm))throw new Error('You declined. Nothing changed.');r=await post('/confirm/'+r.confirm.id)}
 if(r.error)throw new Error(r.error);return r.result}
function ask(c){return new Promise(res=>{const d=$('#confirm');$('#c-msg').textContent=c.message;$('#c-prev').textContent=c.preview||'';$('#c-prev').hidden=!c.preview;
 d.showModal();$('#c-yes').onclick=()=>{d.close();res(true)};$('#c-no').onclick=()=>{d.close();res(false)}})}
function say(box,text,bad){box.textContent=text;box.className='msg '+(bad?'bad':'ok')}
function card(title,sub){const c=h('div','card'),t=h('h3');t.append(h('span','',title));if(sub)t.append(h('span','file',sub));c.append(t);return c}
const W=/^(Save|Create|Delete|Reload |Run now|Enable|Disable|Train|Score|New |Ask$|Add document|Remove)/;
function btn(label,fn,pri){const b=h('button','btn'+(pri?' pri':'')+(W.test(label)?' w':''),label);b.onclick=async()=>{b.disabled=true;try{await fn()}finally{b.disabled=false}};return b}
const cell=v=>v!==null&&typeof v==='object'?JSON.stringify(v):typeof v==='string'&&/^\d{4}-\d\d-\d\dT\d\d:\d\d/.test(v)?new Date(v).toLocaleString():v;
function rows(box,d){box.innerHTML='';box.classList.add('out');if(!(d&&d.columns&&Array.isArray(d.rows))){box.append(h('pre','',JSON.stringify(d,null,1)));return}
 const rs=d.rows.map(r=>(Array.isArray(r)?r:d.columns.map(c=>r[c])).map(cell));table(box,{measures:d.measures||{},format:d.format||{},columns:d.columns,rows:rs})}
function act(c,label,fn,pri){const m=c.querySelector(':scope>.msg')||c.appendChild(h('div','msg'));let r=c.querySelector(':scope>.acts');if(!r){r=h('div','row acts');c.insertBefore(r,m)}
 r.append(btn(label,async()=>{try{const t=await fn(m);if(t)say(m,t)}catch(e){say(m,e.message,1)}},pri));return m}

// ---------- Overview ----------
function home(box){const s=D.summary,g=h('div','cards');box.append(g);
 const r=card('Readiness','semantic model');r.append(h('div','stat '+(D.readiness?.ready?'ok':'bad'),D.readiness?.ready?'Ready':'Not ready'));
 (D.readiness?.missing||[]).forEach(x=>r.append(h('div','file',typeof x==='string'?x:JSON.stringify(x))));g.append(r);
 const o=card('In this project');const NAMES={sources:'sources',tables:'tables',measures:'measures',reports:'reports',datasets:'datasets',flows:'flows',ml:'ML models',tools:'tools',agents:'agents'};
 const ok=h('div','kv');Object.entries(s).forEach(([k,v])=>ok.append(h('b','',NAMES[k]||k),h('span','',String(v))));o.append(ok);g.append(o);
 const q=card('Data quality'),qb=h('div');q.append(qb);g.append(q);
 const dq=()=>{qb.innerHTML='';const d=D.quality;if(!d?.rows?.length){qb.append(h('p','empty','No tables yet.'));return}const ix=c=>d.columns.indexOf(c);
  d.rows.forEach(r=>{const k=h('div','kv');k.style.marginBottom='12px';[['table',r[ix('table')]],['rows',fmt(r[ix('rows')])],['last load',cell(r[ix('last_load')])],['refresh',r[ix('refresh')]]].forEach(([a,b])=>{k.append(h('b','',a),h('span','',b??'—'))});
   const p=r[ix('problems')]||[];k.append(h('b','','problems'),h('span',p.length?'bad':'ok',p.length?p.join('; '):'none'));qb.append(k)})};dq();
 act(q,'Check again',async()=>{D.quality=await api('data_quality',{});dq();return 'Checked.'});
 const c=card('Latest changes','History has all');const k=h('div','kv');(D.history||[]).slice(0,8).forEach(x=>k.append(h('b','',new Date(x.time*1000).toLocaleString([], {dateStyle:'short',timeStyle:'short'})),h('span','',x.message)));c.append(k);c.style.gridColumn='span 2';g.append(c)}

// ---------- Analyze: one report per entry; filters and cross-filter ----------
function report(box,rep){let flt=PENDING.filters||[];PENDING.filters=null;const top=h('div','row bar'),col=h('select','f');col.append(new Option('filter by…',''));D.dims.forEach(c=>col.append(new Option(c,c)));
 const val=h('select','f');val.multiple=true;val.hidden=true;const add=h('button','btn','add filter');add.hidden=true;const chips=h('span');
 top.append(col,val,add,chips);const tabs=h('nav','tabs'),grid=h('div','grid'),msg=h('div','msg');box.append(top,msg,tabs,grid);
 col.onchange=async()=>{val.innerHTML='';if(!col.value){val.hidden=add.hidden=true;return}
  try{const r=await api('semantic_query',{measures:[D.measures[0]?.id],dimensions:[col.value],order_by:[{field:col.value}],limit:500});
   r.rows.forEach(x=>val.append(new Option(String(x[0]),JSON.stringify(x[0]))));val.size=Math.min(8,r.rows.length);val.hidden=add.hidden=false}catch(e){say(msg,e.message,1)}};
 add.onclick=()=>{const v=[...val.selectedOptions].map(o=>JSON.parse(o.value));if(v.length){flt=flt.filter(f=>f.column!==col.value);flt.push({column:col.value,op:'in',value:v});draw()}};
 const cross=(dim,value)=>{if(!LIVE){say(msg,'Snapshot: start the app to filter.',1);return}flt=flt.filter(f=>f.column!==dim);flt.push({column:dim,op:'=',value});draw()};
 let cur=rep.pages[0];
 async function draw(){chips.innerHTML='';msg.textContent='';
  flt.forEach(f=>{const c=h('span','chip x',`${f.column} ${f.op} ${Array.isArray(f.value)?f.value.join(', '):f.value}`);c.onclick=()=>{flt=flt.filter(x=>x!==f);draw()};chips.append(c)});
  for(const p of rep.pages)for(const v of p.visuals){delete v.live;if(flt.length&&v.text===undefined)
   try{v.live=await api('visual_data',{report:rep.id,page:p.id,visual:v.id,interaction:flt});v.live.id=v.id}catch(e){v.live={error:e.message}}}
  page(cur)}
 function page(p){cur=p;[...tabs.children].forEach(b=>b.classList.toggle('on',b.dataset.id===p.id));grid.innerHTML='';
  p.visuals.slice().sort((a,b)=>(a.layout.y-b.layout.y)||(a.layout.x-b.layout.x)).forEach(v=>{const c=h('div','card');c.style.gridColumn=`span ${Math.min(12,v.layout.w||6)}`;
   c.innerHTML=`<h3><span></span><span class="tools"><button data-a="t">table</button><button data-a="c">csv</button><button data-a="h">how</button></span></h3><div class="body"></div><pre class="how" hidden></pre>`;
   $('h3 span',c).textContent=v.title||v.id;grid.append(c);const body=$('.body',c),d=v.live||v.data;
   if(v.text!==undefined){$('.tools',c).remove();body.textContent=v.text;body.style.whiteSpace='pre-wrap';return}
   if(!d||d.error||v.error){body.append(h('div','bad',d?.error||v.error||'no data'));return}
   let asTable=false;const dr=()=>{body.innerHTML='';asTable?table(body,d):chart(body,d);
    if(v.dim){const nm=Math.max(1,Object.keys(d.measures).length),rs=d.rows.filter(r=>r.slice(1).some(x=>typeof x==='number'));
     body.querySelectorAll('rect,circle').forEach((e,i)=>{e.style.cursor='pointer';e.onclick=()=>{const r=rs[e.tagName==='rect'?Math.floor(i/nm):i%rs.length];if(r)cross(v.dim,r[0])}})}};
   requestAnimationFrame(dr);$('[data-a=t]',c).onclick=()=>{asTable=!asTable;dr()};$('[data-a=c]',c).onclick=()=>csv(d);
   $('[data-a=h]',c).onclick=()=>{const x=$('.how',c);x.hidden=!x.hidden;x.textContent=v.how+(flt.length?`\nfilters ${JSON.stringify(flt)}`:'')}})}
 rep.pages.forEach(p=>{const b=h('button','',p.name||p.id);b.dataset.id=p.id;b.onclick=()=>page(p);tabs.append(b)});flt.length?draw():page(cur)}
function datasets(box){const g=h('div','cards');box.append(g);if(!D.datasets.length)box.append(h('p','empty','None yet.'));
 D.datasets.forEach(d=>{const c=card(d.name||d.id,'model/datasets.json');c.append(h('div','file',`${(d.query?.measures||[]).join(', ')} by ${(d.query?.dimensions||[]).join(', ')||'—'}`));
  const out=h('div');act(c,'Preview rows',async()=>{rows(out,await api('dataset_data',{dataset:d.id}))},1);c.append(out);g.append(c)})}

// ---------- Model ----------
function measures(box){const top=h('div','row bar top'),q=h('input','f');q.placeholder='Search measures';const list=h('div','cards');box.append(top,list);
 const edit=(m,isNew)=>{const c=card(isNew?'New measure':(m.name||m.id),isNew?'':m.id),name=h('input','f'),id=h('input','f'),ex=h('textarea','f'),de=h('input','f');
  [name,id,ex,de].forEach(e=>e.classList.add('ed'));name.value=m.name||'';id.value=m.id||'';ex.value=m.expression||'';de.value=m.description||'';
  id.placeholder='a-z, 0-9, _';name.placeholder='display name';ex.placeholder='e.g. SUM(sales.revenue)';de.placeholder='what it means';
  if(isNew)c.append(h('label','l','ID'),id);c.append(h('label','l','Name'),name,h('label','l','MEL'),ex,h('label','l','Description'),de);
  act(c,isNew?'Create':'Save',async()=>{await api('set_measure',{id:id.value,name:name.value,expression:ex.value,description:de.value||null});
   Object.assign(m,{id:id.value,name:name.value,expression:ex.value,description:de.value});if(isNew){D.measures.push(m);show('measures')}return 'Saved through ABI (checked like an import).'},1);
  act(c,'Test',async()=>{const r=await api('semantic_query',{measures:[id.value]});return `${r.measures?.[id.value]||id.value} = ${fmt(r.rows?.[0]?.[0])}`});
  if(!isNew)act(c,'Delete',async()=>{await api('delete_measure',{id:id.value});D.measures=D.measures.filter(x=>x!==m);show('measures')});return c};
 const fill=()=>{list.innerHTML='';D.measures.filter(m=>!q.value||JSON.stringify(m).toLowerCase().includes(q.value.toLowerCase())).forEach(m=>list.append(edit(m)))};
 top.append(q,btn('New measure',()=>list.prepend(edit({},true)),1));q.oninput=fill;fill()}
function glossary(box){const list=h('div','cards');box.append(list);
 const edit=(t,isNew)=>{const c=card(isNew?'New term':(t.term||t.id)),term=h('input','f'),def=h('textarea','f'),ms=h('input','f');[term,def,ms].forEach(e=>e.classList.add('ed'));term.value=t.term||'';def.value=t.definition||'';ms.value=(t.measures||[]).join(', ');
  term.placeholder='business term';def.placeholder='what it means';ms.placeholder='e.g. revenue, units';
  c.append(h('label','l','Term'),term,h('label','l','Definition'),def,h('label','l','Implemented by (measure IDs)'),ms);
  act(c,isNew?'Create':'Save',async()=>{await api('set_glossary_term',{term:term.value,definition:def.value,measures:ms.value.split(',').map(x=>x.trim()).filter(Boolean),columns:t.columns||[]});
   Object.assign(t,{term:term.value,definition:def.value});if(isNew)D.glossary.push(t);return 'Saved through ABI.'},1);
  if(!isNew)act(c,'Delete',async()=>{await api('delete_glossary_term',{id:t.id});D.glossary=D.glossary.filter(x=>x!==t);show('glossary')});return c};
 const bar=h('div','row bar top');bar.append(btn('New term',()=>list.prepend(edit({},true)),1));box.insertBefore(bar,list);D.glossary.forEach(t=>list.append(edit(t)))}

// ---------- Data ----------
function data(box,which){box.insertAdjacentHTML('beforeend',D.sections[which]);const ids=D.sources;if(!ids.length)return;
 const c=card('Reload now','a new snapshot from the source, then the tables that read it');ids.forEach(t=>act(c,'Reload '+t,async()=>{await api('refresh',{table:t});return `${t}: refresh started; Home shows data quality.`}));box.append(c)}

// ---------- Automate ----------
function flows(box){const g=h('div','cards');box.append(g);if(!D.flows.length)box.append(h('p','empty','None yet.'));
 D.flows.forEach(f=>{const t=f.trigger||{},c=card(f.name||f.id,f.enabled?'enabled':'disabled');
  c.append(h('div','file',`${t.type||'manual'} trigger${t.cron?' · '+t.cron:''}${t.measure?` · ${t.measure} ${t.op} ${t.value}`:''} · flows/${f.id}.json`));
  const st=h('div');(f.steps||[]).forEach(s=>st.append(h('span','chip',`${s.id}: ${s.action||s.type}`)));const tr=h('ol','trace');c.append(st,tr);
  act(c,'Run now',async m=>{tr.innerHTML='';const r=await api('run_flow',{flow:f.id});say(m,`run started: ${r.run}`);
   for(let i=0;i<150;i++){await wait(2000);const x=await api('flow_runs',{flow:f.id,run:r.run}),one=x.runs?.[0]||x;tr.innerHTML='';
    (one.steps||[]).forEach(s=>tr.append(h('li',s.status==='ok'?'ok':s.status==='failed'?'bad':'',`${s.step||s.id}: ${s.status}${s.error?' · '+s.error:''}`)));
    if(['ok','failed','stopped','rejected','waiting'].includes(one.status))return `run ${one.status}`}return 'still running; see ABI\'s flow runs'},1);
  act(c,f.enabled?'Disable':'Enable',async()=>{await api('enable_flow',{flow:f.id,enabled:!f.enabled});f.enabled=!f.enabled;show('flows')});g.append(c)})}
function ml(box){const g=h('div','cards');box.append(g);if(!D.ml.length)box.append(h('p','empty','None yet.'));
 D.ml.forEach(m=>{const c=card(m.name||m.id,`ml/${m.id}.json`);c.append(h('div','file',`${m.task}${m.target?' of '+m.target:''} on ${m.dataset}${m.horizon?`, ${m.horizon} ahead`:''}`));
  const st=h('div','kv');st.style.marginTop='12px';const lr=()=>{const r=m.latest_run||{};st.innerHTML='';
   [['latest run',r.kind||'—'],['status',r.status||'—'],['finished',cell(r.finished)||'—'],...Object.entries(r.metrics||{}).map(([a,b])=>[a,typeof b==='number'?fmt(b):cell(b)])].forEach(([a,b])=>st.append(h('b','',a),h('span',a==='status'?(b==='ok'?'ok':b==='failed'?'bad':''):'',String(b))))};lr();c.append(st);
  const follow=async(kind,m2)=>{await api(kind,{model:m.id});say(m2,`${kind==='train_model'?'training':'scoring'}…`);
   for(let i=0;i<60;i++){await wait(3000);const x=(await api('ml_models',{})).models.find(y=>y.id===m.id);if(x){Object.assign(m,x);lr()}
    if(['ok','failed'].includes(m.latest_run?.status)&&m.latest_run?.finished)return `${m.latest_run.kind}: ${m.latest_run.status}`}return 'still running'};
  act(c,'Train',m2=>follow('train_model',m2),1);act(c,'Score',m2=>follow('score_model',m2));g.append(c)})}
function tools(box){const g=h('div','cards');box.append(g);if(!D.tools.length)box.append(h('p','empty','None yet.'));
 D.tools.forEach(t=>{const c=card(t.name||t.id,`tools/${t.id}.json · ${t.status||''}`);c.append(h('div','',t.description||''));const ins={};
  Object.entries(t.inputs?.properties||{}).forEach(([k,s])=>{const i=h('input','f');i.placeholder=s.description||'';ins[k]=i;c.append(h('label','l',k),i)});
  const out=h('div');
  act(c,'Run',async()=>{rows(out,await api('run_tool',{tool:t.id,inputs:Object.fromEntries(Object.entries(ins).filter(([,i])=>i.value).map(([k,i])=>[k,i.value]))}))},1);
  act(c,'Run its tests',async()=>{rows(out,await api('test_tool',{tool:t.id}))});c.append(out);g.append(c)})}
function agents(box){const g=h('div','cards');box.append(g);if(!D.agents.length)box.append(h('p','empty','None yet.'));
 D.agents.forEach(a=>{const c=card(a.name||a.id,`agents/${a.id}.json · ${a.status||''}`);c.append(h('div','file',(a.purpose||a.instructions||'').slice(0,240)));
  const q=h('input','f');q.placeholder='e.g. revenue in North?';const out=h('div','msg');c.append(h('label','l','Question'),q);
  act(c,'Ask',async()=>{const r=await api('run_agent',{agent:a.id,question:q.value});out.textContent=r.answer||JSON.stringify(r,null,1)},1);
  act(c,'Run evaluation set',async()=>{const r=await api('test_agent',{agent:a.id});out.textContent=(r.cases||[]).map(x=>`${x.passed?'✓':'✗'} ${x.question}${x.missing?.length?' · missing '+x.missing.join(', '):''}`).join('\n')||JSON.stringify(r,null,1)});c.append(out);g.append(c)})}

// ---------- Govern ----------
function docs(box){const d=D.docs?.projects?.[0];if(!d){box.append(h('p','empty','No documentation yet.'));return}
 (d.sections||[]).forEach(s=>{const c=card(`${s.number}. ${s.title}`);(s.items||[]).forEach(it=>{c.append(h('h4','',it.title));const t=h('table');
  (it.facts||[]).forEach(([k,v])=>{const r=t.insertRow();r.insertCell().textContent=k;r.insertCell().textContent=typeof v==='string'?v:JSON.stringify(v)});c.append(t)});box.append(c)});
 act(box.firstChild||box,'Regenerate from ABI',async()=>{D.docs=await api('docs',{});show('docs')})}
function commits(box){const c=card('Commits','every change is a commit in ABI\'s Git history');const t=h('table');c.append(t);box.append(c);
 const fill=()=>{t.innerHTML='';(D.history||[]).forEach(x=>{const r=t.insertRow();[new Date(x.time*1000).toLocaleString(),x.author+(x.by==='agent'?' (agent)':''),x.message,x.id.slice(0,7)].forEach(v=>r.insertCell().textContent=v)})};fill();
 act(c,'Reload',async()=>{D.history=(await api('history',{})).commits;fill()})}

function security(box){const g=h('div','cards');box.append(g);const r=D.rls||{},who=Object.fromEntries((r.accounts||[]).map(a=>[a.id,a.name]));
 const c=card('Row-level rules','Viewers see only their rows');if(!(r.rules||[]).length)c.append(h('p','empty','None yet: ask the agent, e.g. "user X sees only region North".'));
 (r.rules||[]).forEach(x=>{const k=h('div','kv');k.style.marginBottom='12px';k.append(h('b','','rule'),h('span','',x.id),h('b','','rows of'),h('span','',`${x.table}.${x.column}`));
  (Array.isArray(x.users)?x.users.map(u=>[u.name||who[u.id]||u.id,u.values]):Object.entries(x.users||{}).map(([u,v])=>[who[u]||u,v])).forEach(([u,v])=>k.append(h('b','',u),h('span','',(v||[]).join(', ')||'no rows')));c.append(k)});g.append(c);
 const cr=card('Hidden columns','hidden from Viewers');const cols=r.columns||[];if(!cols.length)cr.append(h('p','empty','None.'));cols.forEach(x=>cr.append(h('span','chip',typeof x==='string'?x:`${x.table}: ${(x.columns||[]).join(', ')}`)));g.append(cr);
 const ro=card('Roles','set in ABI\'s app (a person\'s click)');const k=h('div','kv');(r.accounts||[]).forEach(a=>k.append(h('b','',a.name),h('span','',a.role||'no role')));ro.append(k);g.append(ro);
 box.insertAdjacentHTML('beforeend',D.sections.Security.includes('None yet')?'':D.sections.Security)}

// ---------- Knowledge: the vault ----------
async function vpost(op,body){const r=await fetch('/vault/'+op,{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify(body||{})}).then(r=>r.json());
 if(r.error)throw new Error(r.error);return r.result}
async function loadVault(){if(LIVE)try{VAULT=await vpost('list')}catch(e){VAULT=[]}}
function vaultPage(box){if(!LIVE){box.append(h('p','empty','The vault opens with the live app: run scripts/app.py '+D.project+'.'));return}
 const bar=h('div','row bar top'),file=h('input');file.type='file';file.multiple=true;file.accept='.md,.txt,.html,.htm,.docx,.pdf';file.hidden=true;file.id='vault-file';
 bar.append(file,btn('Add document',()=>file.click(),1));box.append(bar);
 file.onchange=async()=>{for(const f of file.files){const data=await new Promise(r=>{const fr=new FileReader();fr.onload=()=>r(fr.result.split(',')[1]);fr.readAsDataURL(f)});
  try{const r=await vpost('add',{name:f.name,data});toast(`Added ${r.name}: ${r.chunks} passages`)}catch(e){toast(e.message)}}file.value=''};
 const sc=card('Search the vault','the passages the agents read'),q=h('input','f');q.placeholder='e.g. how is net revenue defined?';q.id='vault-q';
 const out=h('div','hits');sc.append(q,out);box.append(sc);
 const go=async()=>{out.innerHTML='';if(!q.value.trim())return;const hits=await vpost('search',{query:q.value,k:6});if(!hits.length){out.append(h('p','empty','No passage matches.'));return}
  hits.forEach(x=>{const d=h('div','hit');d.append(h('div','file',`${x.document}${x.section?' · '+x.section:''}`),h('div','',x.text));out.append(d)})};
 q.onkeydown=e=>{if(e.key==='Enter')go()};act(sc,'Search',go,1);
 const g=h('div','cards');box.append(g);if(!VAULT.length)box.append(h('p','empty','No documents yet. Add the definitions your measures must follow (.md, .txt, .html, .docx).'));
 VAULT.forEach(d=>{const c=card(d.name,`${Math.max(1,Math.round(d.bytes/1024))} KB · ${d.chunks} passages`);
  if(d.sections.length){const k=h('div');d.sections.forEach(x=>k.append(h('span','chip',x)));c.append(k)}
  const pre=h('pre');pre.hidden=true;c.append(pre);
  act(c,'Read',async()=>{pre.hidden=!pre.hidden;if(!pre.textContent)pre.textContent=await vpost('read',{name:d.name})});
  act(c,'Remove',async()=>{if(await ask({message:`Remove ${d.name} from the vault?`,preview:''})){await vpost('rm',{name:d.name})}});g.append(c)})}

// ---------- sidebar ----------
const PENDING={filters:null};
const LEDE={vault:'Your business documents: definitions, finance rules, KPI specs. The agents search them before they define or check a measure.',home:'Is the model ready, is the data fresh, and what changed last.',datasets:'Saved queries at a fixed grain, reused by reports, flows and ML. Preview their rows.',
 measures:'The business numbers. Edit a formula and ABI checks it before it saves; Test shows the total.',model:'Tables and how they join. Facts are black, dimensions are outlined.',
 glossary:'What the business words mean and which measures implement them.',sources:'Where the data comes from. Reload a source to take a new snapshot.',
 tables:'The tables built from the sources, with their transform steps.',flows:'Automations. Run one now and follow its steps, or switch it on or off.',
 ml:'Forecasts, segments and anomaly models. Train, score and read the latest run.',tools:'Calculations your agents can call. Run one with inputs or run its tests.',
 agents:'Custom agents for this project. Ask one a question or run its evaluation set.',docs:'The project documentation ABI writes from its definitions.',
 security:'Who sees which rows. Roles are set in ABI itself.',environments:'Where each environment reads its data from.',history:'Every change is a commit in ABI. Newest first.'};let PAGES={},CUR='home';const side=$('aside');
function nav(){return [['Start',[['home','Home',home]]],
 ['Analyze',[...D.reports.map(r=>['rep:'+r.id,r.name||r.id,b=>report(b,r)]),['datasets','Datasets',datasets,D.datasets.length]]],
 ['Model',[['measures','Measures',measures,D.measures.length],['model','Model',b=>b.insertAdjacentHTML('beforeend',D.sections.Model)],['glossary','Glossary',glossary,D.glossary.length]]],
 ['Data',[['sources','Sources',b=>data(b,'Sources'),D.sources.length],['tables','Tables',b=>data(b,'Tables'),D.tables.length]]],
 ['Automate',[['flows','Flows',flows,D.flows.length],['ml','ML models',ml,D.ml.length],['tools','Tools',tools,D.tools.length],['agents','Agents',agents,D.agents.length]]],
 ['Knowledge',[['vault','Vault',vaultPage,(VAULT||[]).length],['docs','Docs',docs]]],
 ['Govern',[['security','Security',security],['environments','Environments',b=>b.insertAdjacentHTML('beforeend',D.sections.Environments)],['history','History',commits,(D.history||[]).length]]]]}
let MODE='developer',VAULT=[];
let FOLD={};try{FOLD=JSON.parse(localStorage.getItem('abi-fold')||'{}')}catch(e){}
function sidebar(){side.innerHTML='';PAGES={};nav().forEach(([g,items])=>{const sec=h('div','sec'),hd=h('button','grp'),list=h('div','items');
 hd.append(h('span','',g),h('span','car'));hd.setAttribute('aria-expanded',String(!FOLD[g]));sec.classList.toggle('folded',!!FOLD[g]);
 hd.onclick=()=>{FOLD[g]=!FOLD[g];sec.classList.toggle('folded',FOLD[g]);hd.setAttribute('aria-expanded',String(!FOLD[g]));try{localStorage.setItem('abi-fold',JSON.stringify(FOLD))}catch(e){}};
 items.forEach(([k,label,fn,n])=>{PAGES[k]=[label,fn];const b=h('button',k.startsWith('rep:')?'it sub':'it');b.dataset.k=k;
  b.append(h('span','',label));if(n!==undefined&&n!=='')b.append(h('span','n',String(n)));b.onclick=()=>show(k);list.append(b)});
 sec.append(hd,list);side.append(sec)})}
function show(k){if(!PAGES[k])k='home';CUR=k;side.querySelectorAll('button.it').forEach(b=>b.classList.toggle('on',b.dataset.k===k));
 const act=side.querySelector('button.it.on');if(act)act.closest('.sec').classList.remove('folded');
 const box=$('main');box.innerHTML='';const pg=h('div','pg'),head=h('div','pghead'),t=h('div');t.append(h('h2','',PAGES[k][0]));
 const lede=k.startsWith('rep:')?'Filter the report or click a bar to filter by it. Every number comes from ABI.':LEDE[k];if(lede)t.append(h('p','',lede));head.append(t);pg.append(head);box.append(pg);PAGES[k][1](pg);
 const top=pg.querySelector(':scope>.bar.top');if(top)head.append(top);if(MODE==='viewer')pg.querySelectorAll('.ed').forEach(e=>e.readOnly=true);
 try{localStorage.setItem('abi-app-'+D.project,k)}catch(e){}try{window.history.replaceState(null,'','#'+k)}catch(e){}}
const boot=loadVault();sidebar();
const R=document.documentElement,TB=$('#theme');const dark=()=>R.dataset.theme?R.dataset.theme==='dark':matchMedia('(prefers-color-scheme: dark)').matches;
const setTheme=t=>{if(t)R.dataset.theme=t;TB.textContent=dark()?'Paper':'Ink'};try{setTheme(localStorage.getItem('abi-theme'))}catch(e){setTheme()}
TB.onclick=()=>{const t=dark()?'light':'dark';setTheme(t);try{localStorage.setItem('abi-theme',t)}catch(e){}};
$('.snap').hidden=LIVE;$('.snap').title=`Snapshot of ${D.stamp}. Run scripts/app.py ${D.project} to work live.`;
function setMode(md){MODE=md;document.body.dataset.mode=md;$('#m-dev').classList.toggle('on',md==='developer');$('#m-view').classList.toggle('on',md==='viewer');
 $('#chat textarea').placeholder=md==='viewer'?'Ask a question. In Viewer the agent only reads.':'Ask a question or for a change.'}
async function pickMode(md){if(!LIVE){toast('Snapshot: start the app to switch modes.');return}
 await fetch('/mode',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify({mode:md})})}
$('#m-dev').onclick=()=>pickMode('developer');$('#m-view').onclick=()=>pickMode('viewer');
let first=decodeURIComponent(location.hash.slice(1));if(!first)try{first=localStorage.getItem('abi-app-'+D.project)}catch(e){}show(first||'home');
boot.then(()=>{sidebar();show(CUR)});setMode('developer');
window.addEventListener('hashchange',()=>{const k=decodeURIComponent(location.hash.slice(1));if(k&&k!==CUR)show(k)});

// ---------- the agent: it steers this page, the page follows its changes, and the person talks to it here ----------
function toast(t){const x=h('div','toast',t);document.body.append(x);setTimeout(()=>x.remove(),6000)}
function highlight(item){if(!item)return;const want=String(item).toLowerCase();
 const c=[...document.querySelectorAll('main .card')].find(c=>c.textContent.toLowerCase().includes(want));if(c){c.classList.add('hl');c.scrollIntoView({behavior:'smooth',block:'center'});setTimeout(()=>c.classList.remove('hl'),4000)}}
const chatBox=$('#chat'),log=$('#chat .log'),inp=$('#chat textarea'),busy=$('#chat .busy');
const md=t=>t.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])).replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>').replace(/`([^`]+)`/g,'<code>$1</code>');
function bubble(cls,text){const b=h('div','m '+cls);if(cls==='ai')b.innerHTML=md(text);else b.textContent=text;log.append(b);log.scrollTop=log.scrollHeight;return b}
let steps=null;
function openChat(on){chatBox.classList.toggle('open',on??!chatBox.classList.contains('open'));document.body.classList.toggle('chat-open',chatBox.classList.contains('open'));if(chatBox.classList.contains('open'))inp.focus()}
$('#chat-toggle').onclick=()=>openChat();$('#chat .x').onclick=()=>openChat(false);
async function sendChat(){const t=inp.value.trim();if(!t)return;if(!LIVE){bubble('sys','Snapshot: start `python scripts/app.py '+D.project+'` to talk to the agent.');return}
 bubble('me',t);inp.value='';steps=null;
 await fetch('/chat',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify({message:t,page:CUR})})}
$('#chat .send').onclick=sendChat;inp.onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendChat()}};
$('#chat .stop').onclick=()=>fetch('/chat/stop',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:'{}'});
if(LIVE){const es=new EventSource('/events?k='+encodeURIComponent(KEY));
 es.onmessage=async ev=>{const e=JSON.parse(ev.data);
  if(e.type==='show'){PENDING.filters=e.filters||null;const k=e.page==='report'?'rep:'+e.report:e.page;show(k);setTimeout(()=>highlight(e.item),400);if(e.note)toast('Agent: '+e.note)}
  else if(e.type==='data'){Object.assign(D,e.data);const k=CUR;sidebar();show(k);toast('Updated from ABI')}
  else if(e.type==='busy'){busy.hidden=!e.on;$('#chat .stop').hidden=!e.on}
  else if(e.type==='step'){if(!steps){steps=h('div','m steps');log.append(steps)}const last=[...steps.children].reverse().find(x=>x.dataset.t===e.tool&&x.dataset.s==='run');
   if(e.state==='run'||!last){const l=h('div','',`${e.state==='run'?'…':e.state==='ok'?'✓':'✗'} ${e.tool}`);l.dataset.t=e.tool;l.dataset.s=e.state;steps.append(l)}
   else{last.textContent=`${e.state==='ok'?'✓':'✗'} ${e.tool}`;last.dataset.s=e.state;last.className=e.state==='ok'?'':'bad'}log.scrollTop=log.scrollHeight}
  else if(e.type==='answer'){steps=null;bubble('ai',e.text);if(!chatBox.classList.contains('open'))toast('The agent answered')}
  else if(e.type==='ask'){const yes=await ask({message:e.title||'ABI asks for your confirmation',preview:e.message});bubble('sys',yes?'You confirmed.':'You declined.');
   fetch('/chat/answer',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify({id:e.id,yes})})}
  else if(e.type==='mode'){setMode(e.mode);show(CUR)}
  else if(e.type==='vault'){await loadVault();sidebar();show(CUR)}
  else if(e.type==='note')toast(e.text)}}
"""


def read(action: str, project: str, **kw):
    try:
        return abi.call(action, project=project, **kw)
    except abi.ActionError:
        return None


def snapshot(dest: Path, project: str) -> dict:
    """What the page shows from disk: definitions from the folder, numbers from ABI's actions (as render.py)."""
    j = render.j
    measures = (j(dest / "model/measures.json", {}) or {}).get("measures", [])
    by_id = {m["id"]: m for m in measures}
    reports = []
    for f in sorted((dest / "reports").glob("*.json")):
        rep = j(f, {})
        pages = render.report_pages(project, f.stem, rep, by_id)
        for p, spec in zip(pages, rep.get("pages", [])):  # the dimension a click filters on
            dims = {v["id"]: ((v.get("query") or {}).get("dimensions") or [None])[0] for v in spec.get("visuals", [])}
            for v in p["visuals"]:
                v["dim"] = dims.get(v["id"])
        reports.append({"id": f.stem, "name": rep.get("name"), "pages": pages})
    sm = read("semantic_model", project) or {}
    dims = sorted(f"{t}.{c}" for t, cols in (sm.get("columns") or {}).items() if t in (sm.get("tables") or {})
                  for c, ty in cols.items() if any(k in str(ty) for k in ("VARCHAR", "BOOLEAN")) or t == "calendar")
    g = lambda d: [j(f, {}) for f in sorted((dest / d).glob("*.json")) if not f.name.endswith(".eval.json")]  # noqa: E731
    live = lambda action, key, d: ((read(action, project) or {}).get(key)) or g(d)  # noqa: E731 (status from ABI, else files)
    out = {"project": project, "measures": measures, "reports": reports, "dims": dims, "flows": g("flows"),
           "ml": live("ml_models", "models", "ml"), "tools": live("list_tools", "tools", "tools"),
           "agents": live("list_agents", "agents", "agents"),
           "glossary": (j(dest / "model/glossary.json", {}) or {}).get("terms", []),
           "datasets": (j(dest / "model/datasets.json", {}) or {}).get("datasets", []),
           "sources": [f.stem for f in sorted((dest / "sources").glob("*.json"))],
           "tables": sorted(sm.get("tables") or {}), "readiness": read("readiness", project),
           "quality": read("data_quality", project), "rls": read("rls_rules", project), "history": (read("history", project) or {}).get("commits", []),
           "docs": read("docs", project), "sections": render.sections(dest),
           "stamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
    out["summary"] = {k: len(out[k]) for k in ("sources", "tables", "measures", "reports", "datasets", "flows", "ml", "tools", "agents")}
    return out


def build(project: str) -> Path:
    dest = abi.folder(project)
    if not dest:
        raise SystemExit(f"No folder for project '{project}'; create it with `abi.py project`.")
    data = json.dumps(snapshot(dest, project), default=str).replace("</", "<\\/")
    name = (render.j(dest / "project.json", {}) or {}).get("name", project)
    page = render.shell(name, "", "", "")
    head = (f"<header><span class=brand>ABI</span><span class=crumb>{render.esc(dest.parent.name)}<i>/</i><b>{render.esc(project)}</b></span>"
            "<span class=snap hidden>snapshot</span><span class=right><span class=modes role=group aria-label=Mode>"
            "<button id=m-dev class=on title='Build and change: you and the agent'>Developer</button>"
            "<button id=m-view title='Read only: the agent answers as an analyst'>Viewer</button></span>"
            "<button class=btn id=theme title='Paper or ink'>Ink</button><button class='btn pri' id=chat-toggle>Ask the agent</button></span></header>")
    page = re.sub(r"<header>.*?</header>", lambda _: head, page, count=1, flags=re.S)
    page = page.replace("<main></main>", "<aside></aside><main></main>"
                        "<section id=chat><header><h3>Agent</h3><button class='btn stop' hidden>Stop</button><button class='btn x'>Close</button></header>"
                        "<div class=log><div class='m sys'>Ask about this project, or ask for a change. Every number comes from ABI; changes"
                        " that need your yes show ABI's dialog here.</div></div><div class=busy hidden>working…</div>"
                        "<div class=compose><textarea class=f placeholder='e.g. which videos gained the most subscribers? show me'></textarea>"
                        "<button class='btn pri send'>Send</button></div></section>"
                        "<dialog id=confirm><h3>ABI asks for your confirmation</h3><p id=c-msg></p><pre id=c-prev></pre>"
                        "<div class=row><button class='btn pri' id=c-yes>Confirm</button><button class=btn id=c-no>Cancel</button></div></dialog>")
    page = page.replace("</style>", CSS + "</style>").replace("</body>", f"<script>window.APP={data};{render.JS}\n{JS}</script></body>")
    (dest / "app.html").write_text(page)
    exclude = dest / ".git/info/exclude"  # never committed locally, never in a bundle to ABI
    if "app.html" not in exclude.read_text():
        exclude.write_text(exclude.read_text().rstrip("\n") + "\napp.html\n")
    return dest / "app.html"


class Hub:
    """The live side of one app: the page's event stream, the agent's view commands and the chat's Pi process.

    The chat runs `pi --mode rpc` here as the person (ABI_TOKEN from this process); its answers reach the page only
    after the harness grounded them (message_end, never the streamed draft), its confirmations become a dialog in the
    page and the person's yes or no goes back to Pi (the confirm token stays in the harness)."""

    def __init__(self, project: str, url: str, agent_key: str):
        self.project, self.url, self.agent_key = project, url, agent_key
        self.clients: list = []
        self.lock = threading.Lock()
        self.pi = None
        self.busy = False
        self.mode = "developer"  # the person's switch; the harness asks for it before every tool call (/agent/mode)

    def emit(self, event: dict) -> None:
        line = f"data: {json.dumps(event, default=str)}\n\n".encode()
        with self.lock:
            for q in list(self.clients):
                q.append(line)

    def refresh(self) -> None:
        """After a change through ABI (the agent's or the person's): the folder, app.html and the open pages follow."""
        def go():
            try:
                abi.sync(self.project)  # pull ABI's commit, re-render (writes app.html too)
                self.emit({"type": "data", "data": snapshot(abi.folder(self.project), self.project)})
            except Exception as e:  # noqa: BLE001 (a failed refresh keeps the page as it was)
                self.emit({"type": "note", "text": f"Couldn't refresh: {type(e).__name__}"})
        threading.Thread(target=go, daemon=True).start()

    def chat(self, message: str, page: str) -> None:
        if not self.pi or self.pi.poll() is not None:
            env = {**os.environ, "ABI_TOKEN": abi.token(), "ABI_APP_URL": self.url, "ABI_APP_PROJECT": self.project,
                   "ABI_APP_AGENT_KEY": self.agent_key}
            self.pi = subprocess.Popen(["pi", "--mode", "rpc", "--no-session"], cwd=abi.ROOT, env=env, text=True, bufsize=1,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            threading.Thread(target=self.read, daemon=True).start()
        role = ("Viewer mode: you are the analyst, read-only" if self.mode == "viewer"
                else "Developer mode: you build and change through ABI")
        text = f"[In the app for project {self.project}; {role}; the person is on its '{page}' page.] {message}"
        self.send({"type": "follow_up" if self.busy else "prompt", "message": text})
        self.busy = True
        self.emit({"type": "busy", "on": True})

    def send(self, cmd: dict) -> None:
        self.pi.stdin.write(json.dumps(cmd) + "\n")
        self.pi.stdin.flush()

    def read(self) -> None:
        started, final = {}, ""
        for line in self.pi.stdout:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            t = e.get("type")
            if t == "extension_ui_request" and e.get("method") == "confirm":
                self.emit({"type": "ask", "id": e["id"], "title": e.get("title"), "message": e.get("message")})
            elif t == "extension_ui_request" and e.get("method") in ("select", "input", "editor"):
                self.send({"type": "extension_ui_response", "id": e["id"], "cancelled": True})
            elif t == "tool_execution_start":
                started[e["toolCallId"]] = e["toolName"]
                self.emit({"type": "step", "tool": e["toolName"].removeprefix("mcp__abi__"), "state": "run"})
            elif t == "tool_execution_end":
                self.emit({"type": "step", "tool": started.get(e["toolCallId"], e.get("toolName", "")).removeprefix("mcp__abi__"),
                           "state": "error" if e.get("isError") else "ok"})
            elif t == "message_end" and (m := e.get("message", {})).get("role") == "assistant":
                txt = "".join(c.get("text", "") for c in m.get("content", []) if c.get("type") == "text")
                if txt.strip() and not any(c.get("type") == "toolCall" for c in m.get("content", [])):
                    final = txt  # after grounding; a first draft that failed it is followed by the retry: keep the last
            elif t == "agent_settled":
                if final:
                    self.emit({"type": "answer", "text": final})
                final = ""
                self.busy = False
                self.emit({"type": "busy", "on": False})


def serve(project: str, port: int = 8790, open_browser: bool = True) -> ThreadingHTTPServer:
    path, key, pending = build(project), secrets.token_urlsafe(24), {}
    agent_key = os.environ.get("ABI_APP_AGENT_KEY") or secrets.token_urlsafe(24)
    host = f"127.0.0.1:{port}"
    hub: Hub

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # no request log: bodies may carry business values
            pass

        def reply(self, code: int, body: bytes, kind="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            route, _, query = self.path.partition("?")
            if self.headers.get("Host") != host:
                return self.reply(404, b"{}")
            if route == "/events":  # EventSource can't send headers: the page key comes as ?k=
                if not secrets.compare_digest(query.removeprefix("k="), key):
                    return self.reply(403, b"{}")
                self.send_response(200)
                for k, v in (("Content-Type", "text/event-stream"), ("Cache-Control", "no-store"), ("Connection", "keep-alive")):
                    self.send_header(k, v)
                self.end_headers()
                q: list = []
                with hub.lock:
                    hub.clients.append(q)
                try:
                    self.wfile.write(f"data: {json.dumps({'type': 'busy', 'on': hub.busy})}\n\ndata: {json.dumps({'type': 'mode', 'mode': hub.mode})}\n\n".encode())
                    self.wfile.flush()
                    while True:
                        while q:
                            self.wfile.write(q.pop(0))
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                        time.sleep(0.3)
                except OSError:
                    pass
                finally:
                    with hub.lock:
                        hub.clients.remove(q)
                return None
            if route not in ("/", "/app.html"):
                return self.reply(404, b"{}")
            self.reply(200, path.read_text().replace(KEY_SLOT, key, 1).encode(), "text/html; charset=utf-8")

        def do_POST(self):
            if self.headers.get("Host") != host:
                return self.reply(403, b'{"error":"forbidden"}')
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if self.path.startswith("/agent/"):  # the agent (its harness), with the agent key: steer the page, data changed
                if not secrets.compare_digest(self.headers.get("X-Abi-Agent", ""), agent_key):
                    return self.reply(403, b'{"error":"forbidden"}')
                if self.path == "/agent/mode":
                    return self.reply(200, json.dumps({"mode": hub.mode}).encode())
                if self.path == "/agent/show" and body.get("project") == project:
                    hub.emit({"type": "show", **{k: body.get(k) for k in ("page", "report", "filters", "item", "note")}})
                elif self.path == "/agent/changed":
                    hub.refresh()
                return self.reply(200, b"{}")
            # the page: a local Host (no DNS rebinding) and the per-start key (a custom header other sites can't send)
            if not secrets.compare_digest(self.headers.get("X-Abi-App", ""), key):
                return self.reply(403, b'{"error":"forbidden"}')
            if self.path == "/mode":  # the person's switch (the agent can't call this: it has no page key)
                hub.mode = "viewer" if body.get("mode") == "viewer" else "developer"
                hub.emit({"type": "mode", "mode": hub.mode})
                return self.reply(200, b"{}")
            if self.path.startswith("/vault/"):
                return self.reply(*vault_route(self.path.removeprefix("/vault/"), body))
            if self.path == "/chat":
                hub.chat(str(body.get("message", ""))[:4000], str(body.get("page", "home")))
                return self.reply(200, b"{}")
            if self.path == "/chat/answer":  # the person's yes/no to a confirmation the agent's step asked for
                hub.send({"type": "extension_ui_response", "id": str(body.get("id")), "confirmed": bool(body.get("yes"))})
                return self.reply(200, b"{}")
            if self.path == "/chat/stop":
                if hub.pi:
                    hub.send({"type": "abort"})
                return self.reply(200, b"{}")
            if self.path.startswith("/confirm/"):
                job = pending.pop(self.path.split("/")[-1], None)
                if not job:
                    return self.reply(410, b'{"error":"That confirmation expired; try again."}')
                return self.reply(200, json.dumps(run(*job), default=str).encode())
            action = self.path.removeprefix("/api/")
            if action not in ALLOWED or body.get("project") != project:
                return self.reply(403, json.dumps({"error": f"'{action}' is not available here; use ABI's app."}).encode())
            if hub.mode == "viewer" and (action in WRITES or action in READS_ONLY):
                return self.reply(403, json.dumps({"error": "Viewer mode is read-only. Switch to Developer in the top bar to change things."}).encode())
            out = run(action, body, None)
            if "result" in out and action in WRITES:
                hub.refresh()
            self.reply(200, json.dumps(out, default=str).encode())

    def vault_route(op: str, body: dict) -> tuple[int, bytes]:
        import base64  # noqa: PLC0415
        import tempfile  # noqa: PLC0415

        import vault  # noqa: PLC0415 (sibling script)
        if op in ("add", "rm") and hub.mode == "viewer":
            return 403, b'{"error":"Viewer mode is read-only. Switch to Developer to add or remove documents."}'
        try:
            if op == "list":
                out = vault.listing(project)
            elif op == "search":
                out = vault.search(project, str(body.get("query", "")), int(body.get("k") or 6))
            elif op == "read":
                out = vault.read(project, Path(str(body.get("name"))).name)
            elif op == "add":
                name = Path(str(body.get("name"))).name
                with tempfile.TemporaryDirectory() as d:
                    f = Path(d) / name
                    f.write_bytes(base64.b64decode(body.get("data") or ""))
                    out = vault.add(project, f, name)
            elif op == "rm":
                out = vault.remove(project, Path(str(body.get("name"))).name)
            else:
                return 404, b"{}"
        except SystemExit as e:  # vault's own messages (unsupported type, missing pypdf)
            return 200, json.dumps({"error": str(e)}).encode()
        if op in ("add", "rm"):
            hub.emit({"type": "vault"})
        return 200, json.dumps({"result": out}).encode()

    def run(action: str, args: dict, token: str | None) -> dict:
        try:
            out = {"result": abi.call(action, confirm=token, **args)}
            if token and action in WRITES:
                hub.refresh()
            return out
        except abi.ActionError as e:
            b = e.body if isinstance(e.body, dict) else {"message": str(e.body)}
            if e.status == 409 and b.get("confirm_token") and token is None:
                cid = secrets.token_urlsafe(12)
                pending[cid] = (action, args, b["confirm_token"])  # stays here: the page never sees the token
                preview = json.dumps(b["preview"], indent=1, default=str)[:4000] if b.get("preview") else ""
                return {"confirm": {"id": cid, "message": b.get("message", "Confirm?"), "preview": preview}}
            detail = "\n".join(f"{x.get('error')} {x.get('fix') or ''}".strip() for x in b.get("errors") or [] if isinstance(x, dict))
            return {"error": "\n".join(filter(None, [b.get("message"), detail])) or json.dumps(b)[:500]}

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    host = f"127.0.0.1:{srv.server_address[1]}"  # port 0 = any free port
    hub = Hub(project, f"http://{host}", agent_key)
    srv.hub = hub
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    if open_browser:
        webbrowser.open(f"http://{host}/")
    return srv


def main(argv: list[str]) -> int:
    if not argv:
        raise SystemExit(__doc__)
    s = serve(argv[0], int(argv[1]) if len(argv) > 1 else 8790)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    atexit.register(lambda: s.hub.pi and s.hub.pi.poll() is None and s.hub.pi.terminate())  # the chat's agent stops with the app
    print(f"{abi.folder(argv[0]) / 'app.html'} · live at http://127.0.0.1:{s.server_address[1]}/ (Ctrl+C to stop)", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        s.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

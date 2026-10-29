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
import json
import secrets
import sys
import threading
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
STATIC = ["Model", "Sources", "Tables", "Security", "Environments"]  # explorer sections shown as they are

CSS = """
body{display:grid;grid-template-columns:244px minmax(0,1fr);grid-template-rows:auto 1fr;min-height:100vh}
header{grid-column:1/3;padding:14px 28px;position:sticky;top:0;z-index:6;background:var(--bg);align-items:center}header h1{font-size:18px}
aside{border-right:1px solid var(--line);padding:16px 12px 32px;position:sticky;top:53px;height:calc(100vh - 53px);overflow:auto}
aside .grp{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.07em;color:var(--mute);margin:20px 10px 6px}aside .grp:first-child{margin-top:4px}
aside button{display:flex;justify-content:space-between;align-items:center;width:100%;text-align:left;border:0;background:none;color:var(--ink);padding:7px 10px;margin:1px 0;border-radius:8px;cursor:pointer;font:inherit}
aside button:hover{background:var(--card)}aside button.on{background:var(--acc);color:#fff}aside button .n{color:var(--mute);font-size:12px;font-variant-numeric:tabular-nums}aside button.on .n{color:#fff}
aside button.sub{padding-left:22px;font-size:13px}
main{max-width:1280px;min-width:0;margin:0;padding:28px 32px 64px;align-self:start;width:100%}
.pg>h2{font-size:22px;font-weight:650;margin:0 0 20px;letter-spacing:-.2px}.mode{font-size:12px;color:var(--mute)}.mode.live{color:var(--ok)}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:16px;align-items:start}
.card{padding:18px;border-radius:14px}.card h3{font-size:15px;margin:0 0 10px;align-items:baseline}.card h3 .file{font-weight:400;text-align:right}
.card h4{font-size:13px;margin:14px 0 6px}.card>.file{margin:2px 0 10px}.card table{margin-top:4px}
.btn{border:1px solid var(--line);background:var(--card);color:var(--ink);padding:6px 12px;border-radius:8px;cursor:pointer;font:inherit;font-size:13px}.btn:hover{border-color:var(--mute)}
.btn.pri{background:var(--acc);color:#fff;border-color:var(--acc)}.btn:disabled{opacity:.5;cursor:default}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.card>.acts{margin-top:14px}label.l{display:block;font-size:11px;font-weight:600;color:var(--mute);text-transform:uppercase;letter-spacing:.05em;margin:12px 0 0}label.l+.f{margin-top:4px}.bar{margin:0 0 18px}
input.f,textarea.f,select.f{padding:8px 10px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--ink);font:inherit;font-size:13px;min-width:0}
.card>input.f,.card>textarea.f{display:block;width:100%;margin:8px 0 0}.card .row>input.f{flex:1}.card .row>input.f:first-child{flex:0 0 38%}
textarea.f{font:12px/1.5 ui-monospace,Menlo,monospace;min-height:64px;resize:vertical}input.f:focus,textarea.f:focus,select.f:focus{outline:2px solid var(--acc);outline-offset:-1px}
.msg{font-size:12px;margin:10px 0;white-space:pre-wrap}.msg:empty{display:none}.out{margin-top:12px}.out:empty{display:none}
.chip{padding:3px 10px;margin:3px 4px 3px 0}.chip.x{cursor:pointer}.chip.x:after{content:' ×'}
dialog{border:1px solid var(--line);border-radius:14px;background:var(--card);color:var(--ink);max-width:640px;padding:22px}dialog h3{margin:0 0 10px}dialog .row{margin-top:16px}
dialog::backdrop{background:#0008}.trace{margin:10px 0 0;padding-left:20px}.trace li{margin:3px 0}.trace:empty{display:none}
.stat{font-size:30px;font-weight:650;margin:4px 0}.kv{display:grid;grid-template-columns:auto 1fr;gap:6px 14px;font-size:13px}.kv b{font-weight:500;color:var(--mute)}
pre{margin:10px 0 0}.pg .grid{gap:16px}.pg section{margin-bottom:28px}.pg>.card{margin-bottom:16px}
@media (max-width:820px){body{grid-template-columns:1fr}aside{position:static;height:auto;border-right:0;border-bottom:1px solid var(--line);top:auto}header{grid-column:1}main{padding:20px 16px 48px}.cards{grid-template-columns:1fr}}
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
function btn(label,fn,pri){const b=h('button','btn'+(pri?' pri':''),label);b.onclick=async()=>{b.disabled=true;try{await fn()}finally{b.disabled=false}};return b}
const cell=v=>v!==null&&typeof v==='object'?JSON.stringify(v):typeof v==='string'&&/^\d{4}-\d\d-\d\dT\d\d:\d\d/.test(v)?new Date(v).toLocaleString():v;
function rows(box,d){box.innerHTML='';box.classList.add('out');if(!(d&&d.columns&&Array.isArray(d.rows))){box.append(h('pre','',JSON.stringify(d,null,1)));return}
 const rs=d.rows.map(r=>(Array.isArray(r)?r:d.columns.map(c=>r[c])).map(cell));table(box,{measures:d.measures||{},format:d.format||{},columns:d.columns,rows:rs})}
function act(c,label,fn,pri){const m=c.querySelector(':scope>.msg')||c.appendChild(h('div','msg'));let r=c.querySelector(':scope>.acts');if(!r){r=h('div','row acts');c.insertBefore(r,m)}
 r.append(btn(label,async()=>{try{const t=await fn(m);if(t)say(m,t)}catch(e){say(m,e.message,1)}},pri));return m}

// ---------- Overview ----------
function home(box){const s=D.summary,g=h('div','cards');box.append(g);
 const r=card('Readiness','semantic model');r.append(h('div','stat '+(D.readiness?.ready?'ok':'bad'),D.readiness?.ready?'Ready':'Not ready'));
 (D.readiness?.missing||[]).forEach(x=>r.append(h('div','file',typeof x==='string'?x:JSON.stringify(x))));g.append(r);
 const o=card('In this project');Object.entries(s).forEach(([k,v])=>o.append(h('span','chip',`${v} ${k}`)));g.append(o);
 const q=card('Data quality'),qb=h('div');q.append(qb);g.append(q);
 const dq=()=>{qb.innerHTML='';const d=D.quality;if(!d?.rows?.length){qb.append(h('p','empty','No tables yet.'));return}const ix=c=>d.columns.indexOf(c);
  d.rows.forEach(r=>{const k=h('div','kv');k.style.marginBottom='12px';[['table',r[ix('table')]],['rows',fmt(r[ix('rows')])],['last load',cell(r[ix('last_load')])],['refresh',r[ix('refresh')]]].forEach(([a,b])=>{k.append(h('b','',a),h('span','',b??'—'))});
   const p=r[ix('problems')]||[];k.append(h('b','','problems'),h('span',p.length?'bad':'ok',p.length?p.join('; '):'none'));qb.append(k)})};dq();
 act(q,'Check again',async()=>{D.quality=await api('data_quality',{});dq();return 'Checked.'});
 const c=card('Latest changes','History has all');const k=h('div','kv');(D.history||[]).slice(0,8).forEach(x=>k.append(h('b','',new Date(x.time*1000).toLocaleString([], {dateStyle:'short',timeStyle:'short'})),h('span','',x.message)));c.append(k);c.style.gridColumn='span 2';g.append(c)}

// ---------- Analyze: one report per entry; filters and cross-filter ----------
function report(box,rep){let flt=[];const top=h('div','row bar'),col=h('select','f');col.append(new Option('filter by…',''));D.dims.forEach(c=>col.append(new Option(c,c)));
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
 rep.pages.forEach(p=>{const b=h('button','',p.name||p.id);b.dataset.id=p.id;b.onclick=()=>page(p);tabs.append(b)});page(cur)}
function datasets(box){const g=h('div','cards');box.append(g);if(!D.datasets.length)box.append(h('p','empty','None yet.'));
 D.datasets.forEach(d=>{const c=card(d.name||d.id,'model/datasets.json');c.append(h('div','file',`${(d.query?.measures||[]).join(', ')} by ${(d.query?.dimensions||[]).join(', ')||'—'}`));
  const out=h('div');act(c,'Preview rows',async()=>{rows(out,await api('dataset_data',{dataset:d.id}))},1);c.append(out);g.append(c)})}

// ---------- Model ----------
function measures(box){const top=h('div','row bar'),q=h('input','f');q.placeholder='search measures';const list=h('div','cards');box.append(top,list);
 const edit=(m,isNew)=>{const c=card(isNew?'New measure':(m.name||m.id),isNew?'':m.id),name=h('input','f'),id=h('input','f'),ex=h('textarea','f'),de=h('input','f');
  name.value=m.name||'';id.value=m.id||'';ex.value=m.expression||'';de.value=m.description||'';
  id.placeholder='a-z, 0-9, _';name.placeholder='display name';ex.placeholder='e.g. SUM(sales.revenue)';de.placeholder='what it means';
  if(isNew)c.append(h('label','l','ID'),id);c.append(h('label','l','Name'),name,h('label','l','MEL'),ex,h('label','l','Description'),de);
  act(c,isNew?'Create':'Save',async()=>{await api('set_measure',{id:id.value,name:name.value,expression:ex.value,description:de.value||null});
   Object.assign(m,{id:id.value,name:name.value,expression:ex.value,description:de.value});if(isNew){D.measures.push(m);show('measures')}return 'Saved through ABI (checked like an import).'},1);
  act(c,'Test',async()=>{const r=await api('semantic_query',{measures:[id.value]});return `${r.measures?.[id.value]||id.value} = ${fmt(r.rows?.[0]?.[0])}`});
  if(!isNew)act(c,'Delete',async()=>{await api('delete_measure',{id:id.value});D.measures=D.measures.filter(x=>x!==m);show('measures')});return c};
 const fill=()=>{list.innerHTML='';D.measures.filter(m=>!q.value||JSON.stringify(m).toLowerCase().includes(q.value.toLowerCase())).forEach(m=>list.append(edit(m)))};
 top.append(q,btn('New measure',()=>list.prepend(edit({},true)),1));q.oninput=fill;fill()}
function glossary(box){const list=h('div','cards');box.append(list);
 const edit=(t,isNew)=>{const c=card(isNew?'New term':(t.term||t.id)),term=h('input','f'),def=h('textarea','f'),ms=h('input','f');term.value=t.term||'';def.value=t.definition||'';ms.value=(t.measures||[]).join(', ');
  term.placeholder='business term';def.placeholder='what it means';ms.placeholder='e.g. revenue, units';
  c.append(h('label','l','Term'),term,h('label','l','Definition'),def,h('label','l','Implemented by (measure IDs)'),ms);
  act(c,isNew?'Create':'Save',async()=>{await api('set_glossary_term',{term:term.value,definition:def.value,measures:ms.value.split(',').map(x=>x.trim()).filter(Boolean),columns:t.columns||[]});
   Object.assign(t,{term:term.value,definition:def.value});if(isNew)D.glossary.push(t);return 'Saved through ABI.'},1);
  if(!isNew)act(c,'Delete',async()=>{await api('delete_glossary_term',{id:t.id});D.glossary=D.glossary.filter(x=>x!==t);show('glossary')});return c};
 const bar=h('div','row bar');bar.append(btn('New term',()=>list.prepend(edit({},true)),1));box.insertBefore(bar,list);D.glossary.forEach(t=>list.append(edit(t)))}

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
  Object.entries(x.users||{}).forEach(([u,v])=>k.append(h('b','',who[u]||u),h('span','',(v||[]).join(', ')||'no rows')));c.append(k)});g.append(c);
 const cr=card('Hidden columns','hidden from Viewers');const cols=r.columns||[];if(!cols.length)cr.append(h('p','empty','None.'));cols.forEach(x=>cr.append(h('span','chip',typeof x==='string'?x:`${x.table}: ${(x.columns||[]).join(', ')}`)));g.append(cr);
 const ro=card('Roles','set in ABI\'s app (a person\'s click)');const k=h('div','kv');(r.accounts||[]).forEach(a=>k.append(h('b','',a.name),h('span','',a.role||'no role')));ro.append(k);g.append(ro);
 box.insertAdjacentHTML('beforeend',D.sections.Security.includes('None yet')?'':D.sections.Security)}

// ---------- sidebar ----------
const NAV=[['Overview',[['home','Home',home]]],
 ['Analyze',[...D.reports.map(r=>['rep:'+r.id,r.name||r.id,b=>report(b,r),r.pages.length+'p']),['datasets','Datasets',datasets,D.datasets.length]]],
 ['Model',[['measures','Measures',measures,D.measures.length],['model','Model',b=>b.insertAdjacentHTML('beforeend',D.sections.Model)],['glossary','Glossary',glossary,D.glossary.length]]],
 ['Data',[['sources','Sources',b=>data(b,'Sources'),D.sources.length],['tables','Tables',b=>data(b,'Tables'),D.tables.length]]],
 ['Automate',[['flows','Flows',flows,D.flows.length],['ml','ML models',ml,D.ml.length],['tools','Tools',tools,D.tools.length],['agents','Agents',agents,D.agents.length]]],
 ['Govern',[['docs','Docs',docs],['security','Security',security],['environments','Environments',b=>b.insertAdjacentHTML('beforeend',D.sections.Environments)],['history','History',commits,(D.history||[]).length]]]];
const PAGES={};const side=$('aside');
NAV.forEach(([g,items])=>{side.append(h('div','grp',g));items.forEach(([k,label,fn,n])=>{PAGES[k]=[label,fn];const b=h('button',k.startsWith('rep:')?'sub':'');b.dataset.k=k;
 b.append(h('span','',label));if(n!==undefined&&n!=='')b.append(h('span','n',String(n)));b.onclick=()=>show(k);side.append(b)})});
function show(k){if(!PAGES[k])k='home';side.querySelectorAll('button').forEach(b=>b.classList.toggle('on',b.dataset.k===k));
 const box=$('main');box.innerHTML='';const pg=h('div','pg');pg.append(h('h2','',PAGES[k][0]));box.append(pg);PAGES[k][1](pg);
 try{localStorage.setItem('abi-app-'+D.project,k)}catch(e){}try{window.history.replaceState(null,'','#'+k)}catch(e){}}
const m=$('.mode');m.textContent=LIVE?'live · as you, through ABI':`snapshot ${D.stamp} · run scripts/app.py ${D.project} to work live`;m.classList.toggle('live',LIVE);
let first=decodeURIComponent(location.hash.slice(1));if(!first)try{first=localStorage.getItem('abi-app-'+D.project)}catch(e){}show(first||'home');
window.addEventListener('hashchange',()=>{const k=decodeURIComponent(location.hash.slice(1));if(k&&!side.querySelector(`button.on[data-k="${CSS.escape(k)}"]`))show(k)});
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
    page = render.shell(name, f"{render.esc(project)} · numbers from ABI · <span class=mode></span>", "", "")
    page = page.replace("<main></main>", "<aside></aside><main></main>"
                        "<dialog id=confirm><h3>ABI asks for your confirmation</h3><p id=c-msg></p><pre id=c-prev></pre>"
                        "<div class=row><button class='btn pri' id=c-yes>Confirm</button><button class=btn id=c-no>Cancel</button></div></dialog>")
    page = page.replace("</style>", CSS + "</style>").replace("</body>", f"<script>window.APP={data};{render.JS}\n{JS}</script></body>")
    (dest / "app.html").write_text(page)
    exclude = dest / ".git/info/exclude"  # never committed locally, never in a bundle to ABI
    if "app.html" not in exclude.read_text():
        exclude.write_text(exclude.read_text().rstrip("\n") + "\napp.html\n")
    return dest / "app.html"


def serve(project: str, port: int = 8790, open_browser: bool = True) -> ThreadingHTTPServer:
    path, key, pending = build(project), secrets.token_urlsafe(24), {}
    host = f"127.0.0.1:{port}"

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
            if self.headers.get("Host") != host or self.path.split("?")[0] not in ("/", "/app.html"):
                return self.reply(404, b"{}")
            self.reply(200, path.read_text().replace(KEY_SLOT, key, 1).encode(), "text/html; charset=utf-8")

        def do_POST(self):
            # a local Host (no DNS rebinding) and the per-start key (a custom header: other sites can't send it)
            if self.headers.get("Host") != host or not secrets.compare_digest(self.headers.get("X-Abi-App", ""), key):
                return self.reply(403, b'{"error":"forbidden"}')
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if self.path.startswith("/confirm/"):
                job = pending.pop(self.path.split("/")[-1], None)
                if not job:
                    return self.reply(410, b'{"error":"That confirmation expired; try again."}')
                return self.reply(200, json.dumps(run(*job), default=str).encode())
            action = self.path.removeprefix("/api/")
            if action not in ALLOWED or body.get("project") != project:
                return self.reply(403, json.dumps({"error": f"'{action}' is not available here; use ABI's app."}).encode())
            self.reply(200, json.dumps(run(action, body, None), default=str).encode())

    def run(action: str, args: dict, token: str | None) -> dict:
        try:
            return {"result": abi.call(action, confirm=token, **args)}
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
    host = f"127.0.0.1:{srv.server_address[1]}"  # port 0 = any free port
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    if open_browser:
        webbrowser.open(f"http://{host}/")
    return srv


def main(argv: list[str]) -> int:
    if not argv:
        raise SystemExit(__doc__)
    s = serve(argv[0], int(argv[1]) if len(argv) > 1 else 8790)
    print(f"{abi.folder(argv[0]) / 'app.html'} · live at http://127.0.0.1:{s.server_address[1]}/ (Ctrl+C to stop)", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        s.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

#!/usr/bin/env python3
"""One HTML file per project, inside its folder: <workspace>/<project>/app.html (git-excluded; never sent to ABI).

A sidebar with everything, by category:
  Overview   Home (readiness, data quality, latest changes)
  Analyze    every report (pages; filters; click a bar or point to cross-filter), Queries (preview rows)
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
import urllib.parse
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
           "list_tools", "test_tool", "run_tool", "list_agents", "test_agent", "run_agent", "changes", "commit", "undo",
           "add_data", "delete_source", "delete_table", "set_table", "add_relationship", "remove_relationship", "save_dataset",
           "delete_dataset", "save_report", "delete_report", "save_flow", "delete_flow", "save_ml_model", "delete_ml_model",
           "save_tool", "publish_tool", "delete_tool", "save_agent", "publish_agent", "delete_agent", "preview_steps", "set_steps"}
KEY_SLOT = "__ABI_APP_KEY__"
READS_ONLY = {"run_flow", "run_agent"}  # not changes to the project, but they act (send, call tools): Developer only
WRITES = {"set_measure", "delete_measure", "set_glossary_term", "delete_glossary_term", "enable_flow", "train_model", "score_model", "refresh",
          "commit", "undo", "add_data", "delete_source", "delete_table", "set_table", "add_relationship", "remove_relationship",
          "save_dataset", "delete_dataset", "save_report", "delete_report", "save_flow", "delete_flow", "save_ml_model",
          "delete_ml_model", "save_tool", "publish_tool", "delete_tool", "save_agent", "publish_agent", "delete_agent", "set_steps"}
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
.pg{display:flex;flex-direction:column;gap:24px}.pg>*{margin:0!important}.pg>section{display:flex;flex-direction:column;gap:16px}
.pghead{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;flex-wrap:wrap;padding-bottom:16px;border-bottom:2px solid var(--ink)}
.pghead h2{font:700 34px/1 var(--display);text-transform:uppercase;letter-spacing:.01em;margin:0}
.pg nav.tabs:has(button:only-child){display:none}.card .kpi:only-child .l{display:none}
.pghead p{margin:8px 0 0;color:var(--mute);max-width:64ch}.pghead .bar{margin:0}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:20px;align-items:start}
.card>*+*{margin-top:12px}.card>h3+*{margin-top:0}.card .row{row-gap:8px}.card table{margin-top:0}
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
dialog h3{margin:0 0 12px;font:700 18px var(--display);text-transform:uppercase}dialog .row{margin-top:18px}dialog pre{white-space:pre-wrap;max-height:40vh}dialog::backdrop{background:color-mix(in srgb,var(--ink) 55%,transparent)}
.trace{margin:12px 0 0;padding-left:20px}.trace li{margin:3px 0}.trace:empty{display:none}
.stat{font:700 38px/1 var(--display);margin:4px 0 8px;text-transform:uppercase}
.areas{display:grid;gap:6px}.areas .lk{display:flex;gap:16px;text-align:left;width:100%}.areas .lk b{min-width:9.5em}.areas .lk span{color:var(--mute)}
.kv{display:grid;grid-template-columns:auto 1fr;gap:6px 16px;font-size:12px}.kv b{font-weight:500;color:var(--mute)}
pre{margin:10px 0 0}.pg .grid{gap:20px}.pg>section>h2{display:none}
#chat{position:fixed;top:60px;right:0;bottom:0;width:420px;max-width:100vw;background:var(--sheet);border-left:2px solid var(--ink);display:flex;flex-direction:column;
 transform:translateX(100%);transition:transform .18s ease-out;z-index:7;padding-bottom:env(safe-area-inset-bottom,0px)}
#chat.open{transform:none}body.chat-open main{padding-right:452px}
#chat.full{position:static;width:auto;max-width:none;height:calc(100vh - 230px);min-height:420px;transform:none;border:2px solid var(--ink)}
#chat.full .log,#chat.full .compose,#chat.full .ctx,#chat.full .busy{width:100%;max-width:880px;margin:0 auto}#chat.full .m{font-size:14px}#chat.full .x{display:none}
#chat .ctx{padding:6px 18px 0;font-size:11px;color:var(--mute);text-transform:uppercase;letter-spacing:.08em}#chat .busy{display:flex;align-items:center;gap:10px}#chat .busy[hidden]{display:none!important}
#chat .hello{display:flex;flex-direction:column;gap:8px}#chat .hello p{margin:0 0 4px;color:var(--mute);font-size:13px}#chat .m.nav{align-self:flex-start;border:0;padding:0;font-size:11px;color:var(--mute);cursor:pointer;text-decoration:underline}
#chat header{position:static;display:flex;align-items:center;gap:8px;min-height:0;padding:14px 18px;border-bottom:2px solid var(--ink);background:none}
#chat header h3{margin:0;font:700 16px var(--display);text-transform:uppercase;flex:1}#chat-pick{flex:1;min-width:0}
#chat .log{flex:1;overflow:auto;padding:18px;display:flex;flex-direction:column;gap:12px}
#chat .m{padding:10px 12px;font-size:13px;line-height:1.55;white-space:pre-wrap;max-width:94%;border:2px solid var(--ink)}
#chat .me{align-self:flex-end;background:var(--ink);color:var(--paper)}#chat .ai{align-self:flex-start;background:var(--paper)}
#chat .ai code{background:color-mix(in srgb,var(--ink) 8%,transparent);padding:0 3px}
#chat .sys{align-self:stretch;border:0;border-top:1px dashed var(--faint);color:var(--mute);font-size:11px;padding:8px 0 0;max-width:none}
#chat .steps{align-self:flex-start;border:0;border-left:2px solid var(--ink);color:var(--mute);font-size:11px;line-height:1.7;padding:2px 10px}
#chat .busy{color:var(--mute);font-size:11px;text-transform:uppercase;letter-spacing:.1em;padding:0 18px 10px}
#chat .compose{display:flex;gap:8px;padding:14px 18px 18px;border-top:2px solid var(--ink)}#chat textarea{flex:1;min-height:46px;max-height:160px;margin:0}
.vc{display:flex;gap:8px}body[data-mode=viewer] .vc{display:none}#commit{width:min(760px,calc(100vw - 32px))}
#commit .files{display:flex;flex-direction:column;gap:8px;max-height:50vh;overflow:auto}#commit summary{cursor:pointer;font-weight:600}#commit .row{margin-top:16px}
.brand{font:700 20px/1 var(--display);letter-spacing:.08em;padding:6px 10px;background:var(--ink);color:var(--paper)}
.crumb{font:600 13px var(--text);display:flex;gap:8px;align-items:baseline}.crumb i{font-style:normal;color:var(--mute)}.crumb b{font-weight:700}
.snap{font-size:10px;text-transform:uppercase;letter-spacing:.1em;border:1px dashed var(--mute);color:var(--mute);padding:2px 6px}
header .right{margin-left:auto;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.modes{display:flex;border:2px solid var(--ink)}.modes button{border:0;background:none;color:var(--ink);padding:5px 10px;font:600 11px var(--text);text-transform:uppercase;letter-spacing:.08em;cursor:pointer}
.modes button.on{background:var(--ink);color:var(--paper)}body[data-mode=viewer] .w{display:none}
body[data-mode=viewer] .ed{background:transparent;border-color:var(--faint)}
body[data-mode=viewer] .pghead{border-bottom-style:dashed}
.strip{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));border:2px solid var(--ink);background:var(--sheet)}
.strip .cell{padding:14px 16px;border-right:1px solid var(--faint);min-width:0}.strip .cell:last-child{border-right:0}
.strip .v{font:700 20px/1.1 var(--display);overflow-wrap:anywhere}.strip .v.ok{color:var(--ok)}.strip .v.bad{color:var(--red)}.strip .v.warn{text-decoration:underline 3px}
.strip .l{font-size:10px;text-transform:uppercase;letter-spacing:.1em;color:var(--mute);margin-top:6px}
.home{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:20px;align-items:start}.home .wide{grid-column:span 2}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:20px}.kpis .kpi{border-left:2px solid var(--ink);padding-left:12px}
.list{margin:0;padding-left:18px;display:flex;flex-direction:column;gap:6px}
.links{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.lk{display:flex;gap:10px;align-items:baseline;text-align:left;border:1px solid var(--ink);background:none;color:var(--ink);padding:8px 10px;cursor:pointer;font:inherit}
.lk:hover{background:var(--ink);color:var(--paper)}.lk b{font:700 18px var(--display);min-width:2ch}
.ask0{width:100%;font-size:1.05em}.sugg{display:flex;flex-direction:column;gap:8px}.sg{text-align:left;border:1px dashed var(--ink);background:none;color:var(--ink);padding:9px 12px;cursor:pointer;font:inherit}.sg:hover{border-style:solid;background:var(--paper)}
@media (max-width:1100px){.home{grid-template-columns:1fr}.home .wide{grid-column:auto}.strip{grid-template-columns:repeat(2,minmax(0,1fr))}.strip .cell{border-bottom:1px solid var(--faint)}}
.canvas{position:relative;height:min(70vh,720px);overflow:auto;border:2px solid var(--ink);background:
 radial-gradient(circle,var(--faint) 1px,transparent 1.2px) 0 0/22px 22px,var(--paper)}
.canvas .links{position:absolute;inset:0;pointer-events:none;overflow:visible}.canvas .links .hit{pointer-events:stroke;stroke:transparent;stroke-width:14;fill:none;cursor:pointer}
.canvas .ln{stroke:var(--ink);stroke-width:2;fill:none}.canvas .ln.temp{stroke-dasharray:6 4}.canvas .one{fill:var(--ink)}
.canvas .links .hit:hover+.ln{stroke-width:4}
.tbl{position:absolute;width:250px;background:var(--sheet);border:2px solid var(--ink);user-select:none;z-index:1}
.tbl .th{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:8px 10px;cursor:grab;font:700 13px var(--display);border-bottom:2px solid var(--ink)}
.tbl.fact .th{background:var(--ink);color:var(--paper)}.tbl .badge{border:1px solid currentColor;background:none;color:inherit;font:600 9px var(--text);text-transform:uppercase;letter-spacing:.1em;padding:2px 6px;cursor:pointer}
.tbl .cols{max-height:230px;overflow:auto}.tbl .col{display:grid;grid-template-columns:14px 1fr auto;gap:6px;align-items:center;padding:3px 10px;font-size:11.5px;border-bottom:1px solid var(--faint)}
.tbl .col .ty{color:var(--mute);font-size:10px}.tbl .dot{width:9px;height:9px;border:2px solid var(--ink);border-radius:50%;cursor:crosshair}.tbl .dot:hover{background:var(--ink)}
.cards>.wide{grid-column:1/-1}
.layers{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:20px}.layer h4{margin:0}.layer table{margin-top:8px}
.mobile .layers{grid-template-columns:1fr}
.hits{display:flex;flex-direction:column;gap:10px;margin-top:12px}.hit{border-left:2px solid var(--ink);padding:2px 0 2px 12px;white-space:pre-wrap}.hit .file{margin:0 0 4px}
.toast{position:fixed;left:50%;bottom:calc(24px + env(safe-area-inset-bottom,0px));transform:translateX(-50%);background:var(--ink);color:var(--paper);padding:10px 16px;font-size:12px;z-index:9;max-width:calc(100vw - 32px)}
.card.hl{outline:4px solid var(--ink);outline-offset:3px}
#menu{display:none}
.mobile .modes,.mobile .vc,.mobile #theme{display:none}.mobile #menu{display:inline-block}
.mobile body{grid-template-columns:1fr}.mobile header{grid-column:1;gap:10px;padding-left:max(12px,env(safe-area-inset-left,0px));padding-right:max(12px,env(safe-area-inset-right,0px))}
.mobile .crumb i,.mobile .crumb>:not(b){display:none}.mobile .crumb{font-size:12px}
.mobile aside{position:fixed;top:60px;left:0;bottom:0;width:min(300px,86vw);z-index:8;transform:translateX(-102%);transition:transform .18s;display:block;border-right:2px solid var(--ink);border-bottom:0;padding:8px 0 40px;height:auto}
.mobile body.menu-open aside{transform:none}.mobile aside .sec{display:block}.mobile aside button.grp{display:flex}.mobile aside .items{display:block}.mobile aside .folded .items{display:none}
.mobile aside button.it{width:100%;padding:9px 18px 9px 30px}
.mobile main{padding:18px max(14px,env(safe-area-inset-right,0px)) 48px max(14px,env(safe-area-inset-left,0px))}
.mobile .grid{grid-template-columns:1fr}.mobile .grid>.card{grid-column:1/-1!important}.mobile .cards,.mobile .home{grid-template-columns:1fr}.mobile .home .wide{grid-column:auto}
.mobile .strip{grid-template-columns:repeat(2,minmax(0,1fr))}.mobile .pghead h2{font-size:24px}.mobile .kpi .v{font-size:30px}
.mobile #chat{top:0;width:100vw;z-index:11}.mobile body.chat-open main{padding-right:14px}.mobile .canvas{height:60vh}
.mobile .card h3 .tools{display:none}.mobile nav.tabs{width:100%;overflow-x:auto;flex-wrap:nowrap}
@media (max-width:820px){body{grid-template-columns:1fr}header{grid-column:1}
 aside{position:static;height:auto;border-right:0;border-bottom:2px solid var(--ink);display:flex;flex-wrap:wrap;padding:10px max(16px,env(safe-area-inset-left,0px))}
 aside .sec{display:contents}aside button.grp{display:none}aside .folded .items,aside .items{display:contents}aside button.it,aside button.it.sub{width:auto;padding:5px 10px;margin:0;border:0}
 main{padding:20px max(16px,env(safe-area-inset-right,0px)) 48px max(16px,env(safe-area-inset-left,0px))}body.chat-open main{padding-right:16px}
 .cards{grid-template-columns:1fr}.pghead h2{font-size:26px}#chat{top:0}}
"""

JS = r"""
const D=window.APP, KEY='__ABI_APP_KEY__', LIVE=location.protocol.startsWith('http')&&!KEY.startsWith('__');
const MOBILE=matchMedia('(max-width: 820px), (pointer: coarse) and (max-width: 1100px)').matches;
let CID='';try{CID=localStorage.getItem('abi-client')||'';if(!CID){CID=Math.random().toString(36).slice(2,12);localStorage.setItem('abi-client',CID)}}catch(e){CID=Math.random().toString(36).slice(2,12)}
{const f0=window.fetch.bind(window);window.fetch=(u,o={})=>{if(typeof u==='string'&&u.startsWith('/')){o.headers={...(o.headers||{}),'X-Abi-Client':CID,'X-Abi-Mobile':MOBILE?'1':'0'}}return f0(u,o)}}
if(MOBILE)document.documentElement.classList.add('mobile');
const $=(s,r=document)=>r.querySelector(s), h=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!==undefined)e.textContent=x;return e};
const wait=ms=>new Promise(z=>setTimeout(z,ms));
async function api(action,args){
 if(!LIVE)throw new Error('Snapshot: start `python scripts/app.py '+D.project+'` to work live.');
 const post=(u,b)=>fetch(u,{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify(b||{})}).then(r=>r.json());
 let r=await post('/api/'+action,{project:D.project,...args});
 if(r.confirm){if(!await ask(r.confirm))throw new Error('You declined. Nothing changed.');r=await post('/confirm/'+r.confirm.id)}
 if(r.error)throw new Error(r.error);return r.result}
function ask(c){return new Promise(res=>{const d=$('#confirm');$('#confirm h3').textContent=c.title||(c.id?'ABI asks for your confirmation':'Confirm');$('#c-msg').textContent=c.message;$('#c-prev').textContent=c.preview||'';$('#c-prev').hidden=!c.preview;
 d.showModal();$('#c-yes').onclick=()=>{d.close();res(true)};$('#c-no').onclick=()=>{d.close();res(false)}})}
function say(box,text,bad){box.textContent=text;box.className='msg '+(bad?'bad':'ok')}
function card(title,sub){const c=h('div','card'),t=h('h3');t.append(h('span','',title));if(sub)t.append(h('span','file',sub));c.append(t);return c}
const W=/^(Save|Create|Delete|Edit report|New report|Reload |Run now|Enable|Disable|Train|Score|New |Ask$|Add |Remove|Edit|Publish|Save steps|Upload)/;
function btn(label,fn,pri){const b=h('button','btn'+(pri?' pri':'')+(W.test(label)?' w':''),label);b.onclick=async()=>{b.disabled=true;try{await fn()}finally{b.disabled=false}};return b}
const cell=v=>v!==null&&typeof v==='object'?JSON.stringify(v):typeof v==='string'&&/^\d{4}-\d\d-\d\dT\d\d:\d\d/.test(v)?new Date(v).toLocaleString():v;
function rows(box,d){box.innerHTML='';box.classList.add('out');if(!(d&&d.columns&&Array.isArray(d.rows))){box.append(h('pre','',JSON.stringify(d,null,1)));return}
 const rs=d.rows.map(r=>(Array.isArray(r)?r:d.columns.map(c=>r[c])).map(cell));table(box,{measures:d.measures||{},format:d.format||{},columns:d.columns,rows:rs})}
function act(c,label,fn,pri){const m=c.querySelector(':scope>.msg')||c.appendChild(h('div','msg'));let r=c.querySelector(':scope>.acts');if(!r){r=h('div','row acts');c.insertBefore(r,m)}
 r.append(btn(label,async()=>{try{const t=await fn(m);if(t)say(m,t)}catch(e){say(m,e.message,1)}},pri));return m}

// ---------- Overview ----------
// the agent team on Home: one progress line while a plan is open, and each step a specialist left for the person's yes
async function answer(id,decision){const post=(u,b)=>fetch(u,{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify(b||{})}).then(r=>r.json());
 let r=await post('/team/answer',{id,decision});if(r.confirm){if(!await ask(r.confirm))throw new Error('You declined in ABI\'s confirmation. Nothing changed; the step still waits.');r=await post('/confirm/'+r.confirm.id)}
 if(r.error)throw new Error(r.error);return r.result}
async function team(at,al){let T;try{T=(await fetch('/team',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:'{}'}).then(r=>r.json())).result}catch(e){return}
 const ts=T?.tasks||[],ok=ts.filter(t=>t.status==='passed').length,run=ts.filter(t=>t.status==='running').map(t=>t.id);
 if(ts.length&&ok<ts.length){const e=al.querySelector('li.ok');if(e)e.remove();al.append(h('li','',`Agent team: ${ok} of ${ts.length} tasks passed${run.length?', running: '+run.join(', '):''}.`))}
 (T?.pending||[]).forEach(p=>{const e=al.querySelector('li.ok');if(e)e.remove();const li=h('li','bad'),m=h('div','msg');
  li.append(h('div','',`Waiting for your yes: ${p.action.replace('mcp__abi__','')} ${p.args?.id||p.args?.message||''}`),h('div','file',`${p.why||''} (from ${p.agent}, task ${p.task})`));
  const r=h('div','row');r.append(btn('Approve',async()=>{try{await answer(p.id,'approve');say(m,'Done through ABI.');li.className='ok'}catch(e){say(m,e.message,1)}},1),
   btn('Decline',async()=>{try{await answer(p.id,'decline');say(m,'Declined; the team will see it.');li.className=''}catch(e){say(m,e.message,1)}}));li.append(r,m);al.append(li)})}
function home(box){const rd=D.readiness||{},q=D.quality||{},ix=c=>(q.columns||[]).indexOf(c),rowsQ=q.rows||[];
 const last=rowsQ.map(r=>r[ix('last_load')]).filter(Boolean).sort().pop(),problems=rowsQ.flatMap(r=>(r[ix('problems')]||[]).map(p=>`${r[ix('table')]}: ${p}`));
 // the front door: ask first; the conversation moves to the side chat and the agent takes the person where things are
 const ask0=card(MODE==='viewer'?'Ask anything about the numbers':'Ask, build or find anything'),ta=h('textarea','f ask0');ta.rows=2;
 ta.placeholder=MODE==='viewer'?'e.g. Which channels grew fastest last quarter? Where do I see RPM by country?':'e.g. Add a measure for revenue per subscriber. Where are the flows?';
 const go=()=>{const t=ta.value.trim();if(!t)return;openChat(true);inp.value=t;ta.value='';sendChat()};ta.onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();go()}};
 const sl=h('div','sugg');(MODE==='viewer'?['What changed last month, and why?','Where do I see revenue by country?','Is RPM defined the way the vault says?']
  :['Where are the flows, and what do they do?','Check every measure against the vault definitions','Build a report of revenue by country and month']).forEach(t=>{const b=h('button','sg',t);b.onclick=()=>{ta.value=t;go()};sl.append(b)});
 ask0.append(ta,sl);act(ask0,'Ask',go,1);ask0.classList.add('wide');box.append(ask0);
 const g=h('div','home');box.append(g);
 // what needs attention
 const at=card('Needs attention');const al=h('ul','list');[...(rd.missing||[]).map(x=>typeof x==='string'?x:JSON.stringify(x)),...problems].forEach(t=>al.append(h('li','bad',t)));
 if(MODE==='developer'&&LIVE)team(at,al);
 if(!al.children.length)al.append(h('li','ok','Nothing: the model is ready and the data is fresh.'));at.append(al);
 act(at,'Check again',async()=>{D.quality=await api('data_quality',{});D.readiness=await api('readiness',{});show('home')});g.append(at);
 // the map: every sidebar group with its pages and one line of status, in pipeline order
 const mlok=D.ml.filter(m=>m.latest_score?.status==='ok').length,on=D.flows.filter(f=>f.enabled).length,lastC=(D.history||[])[0];
 const line={Data:problems.length?`${problems.length} data problems`:`fresh · last load ${last?cell(last):'never'}`,'Data Model':rd.ready?'Ready':'Not ready',
  Visualizations:`${D.reports.length} reports · ${MINE.length} of your analyses`,Automate:`${mlok} of ${D.ml.length} ML models scored · ${on} of ${D.flows.length} flows on`,
  Knowledge:`${(VAULT||[]).length} vault documents`,Govern:`${D.pending?.files?.length||0} uncommitted · ${D.published?.at?'published '+cell(D.published.at):'not published'}${lastC?' · last: '+(lastC.message.length>70?lastC.message.slice(0,70)+'…':lastC.message):''}`};
 const map=card('Where things stand','one line per area; open it to go there'),tb=h('div','areas');
 line['Data Model']=(rd.ready?'Ready':'Not ready')+` · ${D.measures.length} measures`;
 nav().filter(([gname])=>gname!=='Start').forEach(([gname,items])=>{const b=h('button','lk');b.append(h('b','',gname),h('span','',line[gname]||''));b.onclick=()=>show(items[0][0]);tb.append(b)});
 map.append(tb);at.classList.add('wide');map.classList.add('wide'); map.style.gridColumn='1/-1';g.append(map)}

// ---------- Analyze: one report per entry; filters and cross-filter ----------
function report(box,rep){let flt=PENDING.filters||[];PENDING.filters=null;
 {const tb=h('div','row bar top');let ed=null;tb.append(btn('Edit report',()=>{if(ed){ed.remove();ed=null;return}ed=editor('report',rep.spec||{id:rep.id,name:rep.name},false);box.querySelector('.pghead').after(ed)}),
  btn('New report',()=>{const c=editor('report',KIND.report.tpl,true);box.querySelector('.pghead').after(c)}),
  btn('Delete report',async()=>{try{await KIND.report.del({id:rep.id});toast('Deleted.')}catch(e){toast(e.message)}}));box.append(tb)}const top=h('div','row bar'),col=h('select','f');col.append(new Option('filter by…',''));D.dims.forEach(c=>col.append(new Option(c,c)));
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
function multi(opts,sel){const m=h('select','f ed');m.multiple=true;m.size=Math.min(8,Math.max(3,opts.length));opts.forEach(o=>{const op=new Option(o,o);op.selected=(sel||[]).includes(o);m.append(op)});return m}
const ALLCOLS=()=>Object.entries(D.columns||{}).flatMap(([t,cols])=>Object.keys(cols).map(c=>`${t}.${c}`)).sort();
function dsForm(d,isNew){const c=card(isNew?'New query':(d.name||d.id),isNew?'':`model/datasets.json · ${d.id}`),id=h('input','f ed'),nm=h('input','f ed'),de=h('input','f ed');
 id.value=d.id||'';id.readOnly=!isNew;nm.value=d.name||'';de.value=d.description||'';const q=d.query||{};
 const ms=multi(D.measures.map(m=>m.id),q.measures),dm=multi(ALLCOLS(),q.dimensions),lim=h('input','f ed');lim.type='number';lim.value=q.limit||'';lim.placeholder='no limit';
 c.append(h('label','l','ID'),id,h('label','l','Name'),nm,h('label','l','Description'),de,h('label','l','Measures'),ms,h('label','l','Grain (dimensions)'),dm,h('label','l','Row limit'),lim);
 const out=h('div');const query=()=>({...q,measures:[...ms.selectedOptions].map(o=>o.value),dimensions:[...dm.selectedOptions].map(o=>o.value),...(lim.value?{limit:+lim.value}:{})});
 act(c,isNew?'Create':'Save',async()=>{await KIND.dataset.save({id:id.value,name:nm.value,description:de.value,query:query()});return 'Saved through ABI.'},1);
 if(!isNew){act(c,'Preview rows',async()=>{rows(out,await api('dataset_data',{dataset:d.id}))});act(c,'Delete',async()=>{await KIND.dataset.del(d);return 'Deleted.'})}
 c.append(out);return c}
function datasets(box){const g=h('div','cards');const bar=h('div','row bar top');bar.append(btn('New query',()=>g.prepend(dsForm({},true)),1));box.append(bar,g);
 if(!D.datasets.length)box.append(h('p','empty','No queries yet: a query asks the model for measures by dimensions and returns a fresh table each run; reports, flows and ML use them.'));
 D.datasets.forEach(d=>g.append(dsForm(d,false)))}

// ---------- Model ----------
function reportsPage(box){const bar=h('div','row bar top');bar.append(btn('New report',()=>{const c=editor('report',KIND.report.tpl,true);g.prepend(c)},1));box.append(bar);
 const g=h('div','cards');box.append(g);if(!D.reports.length){box.append(h('p','empty','No reports yet. Ask the agent to build one.'));return}
 D.reports.forEach(r=>{const c=card(r.name||r.id,`reports/${r.id}.json`),n=r.pages.reduce((a,p)=>a+p.visuals.length,0);
  c.append(h('p','',r.spec?.description||r.description||''),h('p','file',`${r.pages.length} page${r.pages.length===1?'':'s'}: ${r.pages.map(p=>p.name||p.id).join(', ')} · ${n} visuals`));
  act(c,'Open',async()=>{show('rep:'+r.id)},1);g.append(c)})}
// Monitor: is anything wrong right now? Only what reports can't show: data freshness, model readiness, ML runs, flows
function monitorPage(box){const g=h('div','cards');box.append(g);const q=D.quality||{},ix=c=>(q.columns||[]).indexOf(c),rs=q.rows||[],when=t=>t?String(t).slice(0,16).replace('T',' '):'—';
 const fr=card('Data freshness','ABI data quality, per table'),bad=rs.filter(r=>(r[ix('problems')]||[]).length||r[ix('missing_periods')]);
 fr.append(h('p',bad.length?'bad':'ok',bad.length?`${bad.length} of ${rs.length} tables have problems`:`All ${rs.length} tables without problems`));
 const t=h('table');t.insertRow().append(...['table','last load','rows','missing periods','problems'].map(x=>h('th','',x)));
 [...bad,...rs.filter(r=>/stream/.test(r[0])&&!bad.includes(r))].forEach(r=>{const tr=t.insertRow();[r[0],when(r[ix('last_load')]),fmt(r[ix('rows')]),r[ix('missing_periods')]||0,(r[ix('problems')]||[]).map(p=>p.message||p.kind||JSON.stringify(p)).join('; ')||'—'].forEach(v=>tr.insertCell().textContent=v)});
 const sc=h('div','scroll');sc.append(t);fr.append(h('p','file','tables with problems, then the stream tables'),sc);
 const rd=D.readiness||{},mo=card('Model','readiness');mo.append(h('p',rd.ready?'ok':'bad',rd.ready?'Ready':'Not ready'),...(rd.missing||[]).map(m=>h('p','file',typeof m==='string'?m:JSON.stringify(m))));
 const ml=card('ML models','latest training and scoring');if(!D.ml.length)ml.append(h('p','empty','No ML models yet.'));
 D.ml.forEach(m=>{const tr=m.latest_run||{},sc2=m.latest_score||{};ml.append(h('h4','',m.name||m.id),
  h('p',tr.status==='ok'?'ok':'bad',`trained: ${tr.status||'never'} · ${when(tr.finished||tr.created)}`),h('p',sc2.status==='ok'?'ok':'bad',`scored: ${sc2.status||'never'} · ${when(sc2.finished||sc2.created)}`))});
 const fl=card('Flows','switched on or off, last run');if(!D.flows.length)fl.append(h('p','empty','No flows yet.'));
 D.flows.forEach(f=>{const l=h('p','file','last run: …');fl.append(h('h4','',f.name||f.id),h('p',f.enabled?'ok':'',f.enabled?'on':'off'),l);
  if(LIVE)api('flow_runs',{flow:f.id}).then(x=>{const r=(x.runs||[])[0];l.textContent=r?`last run: ${r.status} · ${when(r.finished||r.started||r.created)}`:'last run: never'}).catch(e=>l.textContent='last run: '+e.message)});
 g.append(fr,mo,ml,fl);fr.style.gridColumn='1/-1'}
// Explore: anyone's own analysis on the governed model (reads only, so Viewer can use it); kept analyses stay in this browser
let MINE=[];try{MINE=JSON.parse(localStorage.getItem('abi-explore')||'[]')}catch(e){}
function explore(box){const q0=PENDING.explore||{};PENDING.explore=null;
 const c=card('New analysis','measures by a dimension, from the model'),ms=multi(D.measures.map(m=>m.id),q0.measures),dm=h('select','f'),ty=h('select','f'),
  fc=h('select','f'),fv=h('select','f'),out=h('div','body'),msg=h('div','msg');ms.classList.remove('ed');fv.multiple=true;fv.hidden=true;
 ms.querySelectorAll('option').forEach(o=>o.text=D.measures.find(m=>m.id===o.value)?.name||o.value);
 dm.append(new Option('— total only —',''));fc.append(new Option('no filter',''));D.dims.forEach(d=>{dm.append(new Option(d,d));fc.append(new Option(d,d))});dm.value=(q0.dimensions||[])[0]||'';
 ['bar','line','table'].forEach(t=>ty.append(new Option(t,t)));ty.value=q0.chart||'bar';let flt=q0.filters||[];
 fc.onchange=async()=>{fv.innerHTML='';fv.hidden=!fc.value;if(!fc.value)return;
  try{const r=await api('semantic_query',{measures:[D.measures[0]?.id],dimensions:[fc.value],order_by:[{field:fc.value}],limit:500});
   r.rows.forEach(x=>fv.append(new Option(String(x[0]),JSON.stringify(x[0]))));fv.size=Math.min(6,r.rows.length)}catch(e){say(msg,e.message,1)}};
 const query=()=>{const f=fc.value&&fv.selectedOptions.length?[{column:fc.value,op:'in',value:[...fv.selectedOptions].map(o=>JSON.parse(o.value))}]:flt;
  return {measures:[...ms.selectedOptions].map(o=>o.value),dimensions:dm.value?[dm.value]:[],filters:f}};
 async function run(q,type){out.innerHTML='';msg.textContent='';if(!q.measures.length){say(msg,'Pick at least one measure.',1);return}
  try{const d=await api('semantic_query',{...q,...(q.dimensions.length?{order_by:[{field:q.dimensions[0]}]}:{}),limit:500});d.format=d.format||{};d.type=type;d.id='analysis';
   out.append(h('p','file',(q.filters||[]).map(f=>`${f.column} ${f.op} ${[].concat(f.value).join(', ')}`).join(' · ')||'no filter'));
   const b=h('div');out.append(b);requestAnimationFrame(()=>type==='table'?table(b,d):chart(b,d));return d}catch(e){say(msg,e.message,1)}}
 c.append(h('label','l','Measures'),ms,h('label','l','By'),dm,h('label','l','Filter'),fc,fv,h('label','l','Chart'),ty);
 act(c,'Run',async()=>{await run(query(),ty.value)},1);
 act(c,'Keep',async()=>{const q=query();if(!q.measures.length)return 'Pick at least one measure.';
  const nm=q.measures.map(m=>D.measures.find(x=>x.id===m)?.name||m).join(', ')+(q.dimensions[0]?' by '+q.dimensions[0]:'');
  MINE.unshift({name:nm,q,type:ty.value,at:new Date().toISOString().slice(0,16).replace('T',' ')});MINE=MINE.slice(0,30);
  try{localStorage.setItem('abi-explore',JSON.stringify(MINE))}catch(e){}mine();return 'Kept in My analyses (this browser only).'});
 act(c,'Save as query',async()=>{const q=query();if(!q.measures.length)return 'Pick at least one measure.';
  const id=('q_'+q.measures.join('_')+(q.dimensions[0]?'_by_'+q.dimensions[0].split('.').pop():'')).toLowerCase().replace(/[^a-z0-9_]/g,'_').slice(0,60);
  await KIND.dataset.save({id,name:id,description:'Saved from Explore',query:q});return `Saved as query ${id} through ABI.`});
 c.append(msg,out);const g=h('div','cards'),my=card('My analyses','kept in this browser; numbers rerun from the model when opened'),list=h('div');my.append(list);c.style.gridColumn=my.style.gridColumn='1/-1';ms.style.width='100%';g.append(c,my);box.append(g);
 function mine(){list.innerHTML='';if(!MINE.length){list.append(h('p','empty','Nothing kept yet. Run an analysis and press Keep; charts the agent shows you land here too.'));return}
  MINE.forEach((a,i)=>{const r=h('div','row');const o=h('button','btn',a.name);o.onclick=()=>{[...ms.options].forEach(x=>x.selected=a.q.measures.includes(x.value));dm.value=a.q.dimensions[0]||'';ty.value=a.type;flt=a.q.filters||[];fc.value='';fv.hidden=true;run(a.q,a.type)};
   const x=h('button','btn','forget');x.onclick=()=>{MINE.splice(i,1);try{localStorage.setItem('abi-explore',JSON.stringify(MINE))}catch(e){}mine()};r.append(o,h('span','file',a.at||''),x);list.append(r)})}
 mine();if(q0.measures)run(query(),ty.value)}
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
async function upload(table,file){const data=await new Promise(r=>{const fr=new FileReader();fr.onload=()=>r(fr.result.split(',')[1]);fr.readAsDataURL(file)});
 const r=await fetch('/upload',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify({table,name:file.name,data})}).then(r=>r.json());
 if(r.error)throw new Error(r.error);return r.result}
function sources(box){const q=D.quality||{},ix=c=>(q.columns||[]).indexOf(c),qr=Object.fromEntries((q.rows||[]).map(r=>[r[ix('table')],r]));
 const g=h('div','cards'),bar=h('div','row bar top');box.append(bar,g);
 const add=()=>{const c=card('Add a source','a file you upload, or a folder of Parquet files on S3');c.classList.add('editor');
  const t=h('input','f ed');t.placeholder='table id, e.g. bronze_orders';const loc=h('input','f ed');loc.placeholder='s3://bucket/folder/*.parquet  or  https://…';
  const ep=h('input','f ed');ep.placeholder='S3 endpoint host:port, e.g. 127.0.0.1:9100 (blank for AWS)';const fm=h('select','f ed');['','parquet','csv','json','xlsx','delta','iceberg'].forEach(x=>fm.append(new Option(x||'format from the file name',x)));
  const file=h('input');file.type='file';file.accept='.csv,.tsv,.parquet,.json,.jsonl,.xlsx';file.className='f ed';
  c.append(h('label','l','Table'),t,h('label','l','Upload a file'),file,h('label','l','Or a location'),loc,h('label','l','S3 endpoint'),ep,h('label','l','Format'),fm);
  act(c,'Add source',async()=>{if(!t.value)throw new Error('Name the table first.');
   if(file.files[0]){await upload(t.value,file.files[0]);return `Uploaded and loaded into ${t.value}.`}
   if(!loc.value)throw new Error('Choose a file or give a location.');
   const s3=loc.value.startsWith('s3://')?{...(ep.value?{endpoint:ep.value,url_style:'path',use_ssl:false}:{}),region:'us-east-1'}:null;
   await api('add_data',{table:t.value,location:loc.value,...(fm.value?{format:fm.value}:{}),...(s3?{s3}:{})});return `Loaded ${t.value}.`},1);return c};
 bar.append(btn('Add source',()=>g.prepend(add()),1));
 if(!D.sources.length)box.append(h('p','empty','No sources yet: add a file or an S3 folder of Parquet files.'));
 D.sources.forEach(id=>{const sp=D.source_specs?.[id]||{},r=qr[id],c=card(id,`sources/${id}.json`),k=h('div','kv');
  [['location',sp.location||'—'],['format',sp.format||'from the file'],['refresh',sp.refresh||(r?r[ix('refresh')]:'—')],['rows',r?fmt(r[ix('rows')]):'—'],['last load',r?cell(r[ix('last_load')]):'—']]
   .forEach(([a,b])=>k.append(h('b','',a),h('span','',String(b))));c.append(k);
  const p=r?(r[ix('problems')]||[]):[];if(p.length)c.append(h('div','bad',p.join('; ')));
  act(c,'Reload '+id,async()=>{await api('refresh',{table:id});return 'Reloaded: a new snapshot.'});act(c,'Delete',async()=>{await api('delete_source',{source:id});return 'Deleted.'});g.append(c)})}
function stagedPage(box){const sv=(D.layers||[]).filter(t=>t.layer==='silver').sort((a,b)=>a.table.localeCompare(b.table));
 const g=h('div','cards');box.append(g);if(!sv.length){box.append(h('p','empty','Nothing staged yet: silver tables appear here once a transform builds them.'));return}
 sv.forEach(t=>{const c=card(t.table,`${fmt(t.rows)} rows · hidden from the model`);const st=(D.table_steps||{})[t.table]||[];
  const reads=st.filter(x=>x.type==='source'||x.type==='append'||x.type==='merge').map(x=>x.source||(x.tables||[]).join(', ')||x.table).filter(Boolean);
  if(reads.length)c.append(h('div','file','reads '+reads.join(', ')));
  const ol=h('ol','steps');st.forEach(x=>ol.append(h('li','',`${x.type}${x.columns?': '+(Array.isArray(x.columns)?x.columns.join(', '):Object.keys(x.columns).join(', ')):''}${x.name?': '+x.name:''}`)));if(st.length)c.append(ol);
  const pre=h('pre');pre.hidden=true;c.append(pre);act(c,'Show steps (JSON)',()=>{pre.hidden=!pre.hidden;pre.textContent=JSON.stringify(st,null,2)});
  let ed=null;act(c,'Edit steps',()=>{if(ed){ed.remove();ed=null;return}const tmp=h('div');tablesStepsEditor(tmp,t.table,st);ed=tmp.firstChild;c.after(ed)});g.append(c)})}
function tablesStepsEditor(box,t,steps){const c=card(`Steps of ${t}`,`tables/${t}.json`);c.classList.add('editor');
 const ta=h('textarea','f ed code');ta.value=JSON.stringify(steps||[],null,2);ta.rows=Math.min(24,ta.value.split('\n').length+1);const out=h('div');c.append(ta);
 const parse=()=>{try{return JSON.parse(ta.value)}catch(e){throw new Error('Not valid JSON: '+e.message)}};
 act(c,'Preview',async()=>{rows(out,await api('preview_steps',{steps:parse()}))});
 act(c,'Save steps',async()=>{await api('set_steps',{table:t,steps:parse()});return 'Saved and loaded through the steps.'},1);c.append(out);box.append(c)}
function tablesPage(box){
 const g=h('div','cards'),bar=h('div','row bar top');box.append(bar,g);
 const stepsEd=(t,steps,isNew)=>{const c=card(isNew?'New table (transform)':`Steps of ${t}`,isNew?'starts from a loaded source or table':`tables/${t}.json`);c.classList.add('editor');
  const id=h('input','f ed');id.value=isNew?'':t;id.readOnly=!isNew;id.placeholder='table id, e.g. silver_orders';
  const ta=h('textarea','f ed code');ta.value=JSON.stringify(steps||[{type:'source',source:D.sources[0]||''}],null,2);ta.rows=Math.min(24,ta.value.split('\n').length+1);
  const out=h('div');if(isNew)c.append(h('label','l','Table'),id);c.append(h('label','l','Steps (JSON)'),ta);
  const parse=()=>{try{return JSON.parse(ta.value)}catch(e){throw new Error('Not valid JSON: '+e.message)}};
  act(c,'Preview',async()=>{rows(out,await api('preview_steps',{steps:parse()}))});
  act(c,'Save steps',async()=>{await api('set_steps',{table:id.value,steps:parse()});return 'Saved and loaded through the steps.'},1);c.append(out);return c};
 bar.append(btn('New table',()=>g.prepend(stepsEd('',null,true)),1));
 const T=D.model?.tables||{};if(!Object.keys(T).length)box.append(h('p','empty','No tables yet: add a source first.'));
 Object.entries(T).forEach(([t,v])=>{const c=card(t,v.kind||'');const kind=h('select','f ed');['fact','dimension'].forEach(x=>kind.append(new Option(x,x)));kind.value=v.kind||'fact';
  const key=h('input','f ed');key.value=(v.key||[]).join(', ');key.placeholder='key columns';const de=h('input','f ed');de.value=v.description||'';
  const hid=h('input','ed');hid.type='checkbox';hid.checked=!!v.hidden;const hl=h('label','l');hl.append(hid,document.createTextNode('  hidden from the model (staging)'));
  const cols=Object.entries(D.columns?.[t]||{});const cl=h('div');cols.slice(0,40).forEach(([n,ty])=>cl.append(h('span','chip',`${n} · ${ty}`)));
  c.append(h('label','l','Kind'),kind,h('label','l','Key'),key,h('label','l','Description'),de,hl,h('label','l',`Columns (${cols.length})`),cl);
  act(c,'Save',async()=>{await api('set_table',{table:t,kind:kind.value,key:key.value?key.value.split(',').map(x=>x.trim()).filter(Boolean):null,description:de.value||null,hidden:hid.checked});return 'Saved.'},1);
  let ed=null;act(c,'Edit steps',()=>{if(ed){ed.remove();ed=null;return}ed=stepsEd(t,(D.table_steps||{})[t],false);c.after(ed)});
  act(c,'Delete',async()=>{await api('delete_table',{table:t});return 'Deleted.'});g.append(c)})}

// ---------- the model canvas: drag tables, drag a column onto another table's column to join, click a join to remove it ----------
function modelCanvas(box){const T=D.model?.tables||{},R=D.model?.relationships||[],names=Object.keys(T);
 if(!names.length){box.append(h('p','empty','No tables yet: add a source first.'));return}
 const help=h('p','file',MODE==='viewer'?'Facts are filled black, dimensions outlined. Hover a join to see its columns.'
  :'Drag a table by its title. Drag from a column\'s dot onto the matching column of another table to join them (many rows to one). Click a join to remove it. Click a table\'s badge to switch fact or dimension.');
 const wrap=h('div','canvas'),svg=document.createElementNS(NS,'svg');svg.classList.add('links');wrap.append(svg);box.append(help,wrap);
 let pos={};try{pos=JSON.parse(localStorage.getItem('abi-pos-'+D.project)||'{}')}catch(e){}
 const facts=names.filter(t=>T[t].kind==='fact'),dims=names.filter(t=>T[t].kind!=='fact');
 const hgt=t=>48+Math.min(230,Object.keys(D.columns?.[t]||{}).length*25);
 [[facts,24],[dims,380]].forEach(([list,x])=>{let y=24;list.forEach(t=>{if(!pos[t])pos[t]={x,y};y=Math.max(y,pos[t].y)+hgt(t)+32})});
 const save=()=>{try{localStorage.setItem('abi-pos-'+D.project,JSON.stringify(pos))}catch(e){}};
 const boxes={};
 names.forEach(t=>{const b=h('div','tbl '+(T[t].kind==='fact'?'fact':'dim'));b.style.left=pos[t].x+'px';b.style.top=pos[t].y+'px';
  const hd=h('div','th'),badge=h('button','badge',T[t].kind||'table');hd.append(h('span','',t),badge);b.append(hd);
  const cols=h('div','cols');Object.entries(D.columns?.[t]||{}).forEach(([c,ty])=>{const r=h('div','col');r.dataset.t=t;r.dataset.c=c;
   const dot=h('span','dot w');dot.title='Drag onto another table\'s column to join';r.append(dot,h('span','',c),h('span','ty',String(ty).toLowerCase()));cols.append(r)});
  b.append(cols);wrap.append(b);boxes[t]=b;
  badge.onclick=async()=>{if(MODE==='viewer')return;try{await api('set_table',{table:t,kind:T[t].kind==='fact'?'dimension':'fact'});toast('Saved')}catch(e){toast(e.message)}};
  hd.onpointerdown=e=>{if(e.target===badge)return;e.preventDefault();const sx=e.clientX-pos[t].x,sy=e.clientY-pos[t].y;hd.setPointerCapture(e.pointerId);
   hd.onpointermove=m=>{pos[t]={x:Math.max(0,m.clientX-sx),y:Math.max(0,m.clientY-sy)};b.style.left=pos[t].x+'px';b.style.top=pos[t].y+'px';draw()};
   hd.onpointerup=()=>{hd.onpointermove=null;save()}}});
 const at=(t,c,side)=>{const w=wrap.getBoundingClientRect(),row=boxes[t]?.querySelector(`.col[data-c="${CSS.escape(c)}"]`)||boxes[t];if(!row)return[0,0];
  const r=row.getBoundingClientRect();return[(side==='r'?r.right:r.left)-w.left+wrap.scrollLeft,r.top+r.height/2-w.top+wrap.scrollTop]};
 let temp=null;
 function draw(){svg.innerHTML='';svg.setAttribute('width',wrap.scrollWidth);svg.setAttribute('height',wrap.scrollHeight);
  R.forEach(rel=>{const f=rel.from,to=rel.to;if(!boxes[f.table]||!boxes[to.table])return;
   const L=pos[f.table].x<pos[to.table].x;const [x1,y1]=at(f.table,f.columns[0],L?'r':'l'),[x2,y2]=at(to.table,to.columns[0],L?'l':'r');
   const dx=Math.max(40,Math.abs(x2-x1)/2)*(L?1:-1),d=`M${x1},${y1} C${x1+dx},${y1} ${x2-dx},${y2} ${x2},${y2}`;
   const hit=el('path',{d,class:'hit'},svg),ln=el('path',{d,class:'ln'},svg);el('circle',{cx:x2,cy:y2,r:4,class:'one'},svg);
   const tt=document.createElementNS(NS,'title');tt.textContent=`${f.table}.${f.columns.join(',')} → ${to.table}.${to.columns.join(',')} (many to one)`;hit.append(tt);
   hit.onclick=async()=>{if(MODE==='viewer')return;try{await api('remove_relationship',{id:rel.id});toast('Join removed')}catch(e){toast(e.message)}}});
  if(temp)el('path',{d:temp,class:'ln temp'},svg)}
 wrap.addEventListener('pointerdown',e=>{const dot=e.target.closest('.dot');if(!dot||MODE==='viewer')return;e.preventDefault();const from=dot.parentNode,[x1,y1]=at(from.dataset.t,from.dataset.c,'r');
  const mv=m=>{const w=wrap.getBoundingClientRect();temp=`M${x1},${y1} L${m.clientX-w.left+wrap.scrollLeft},${m.clientY-w.top+wrap.scrollTop}`;draw()};
  const up=async m=>{window.removeEventListener('pointermove',mv);window.removeEventListener('pointerup',up);temp=null;draw();
   const tgt=document.elementFromPoint(m.clientX,m.clientY)?.closest('.col');if(!tgt||tgt.dataset.t===from.dataset.t)return;
   const a=`${from.dataset.t}.${from.dataset.c}`,b=`${tgt.dataset.t}.${tgt.dataset.c}`;
   if(!await ask({message:`Join ${a} to ${b}?`,preview:`Many rows of ${from.dataset.t} to one row of ${tgt.dataset.t}. ${tgt.dataset.t} becomes a dimension keyed on ${tgt.dataset.c}.`}))return;
   try{await api('add_relationship',{from:a,to:b});toast('Joined')}catch(e){toast(e.message)}};
  window.addEventListener('pointermove',mv);window.addEventListener('pointerup',up)});
 requestAnimationFrame(draw);wrap.onscroll=draw;new ResizeObserver(draw).observe(wrap)}

// ---------- editors: every object can be made, changed and deleted here; ABI checks each save ----------
const SERVER_KEYS=['owner','status','version','disabled','error','latest_run','latest_score','active_run','evaluation','sharing','last_runs','tokens'];
const KIND={
 flow:{label:'flow',save:o=>api('save_flow',{flow:o}),del:o=>api('delete_flow',{flow:o.id}),
  tpl:{$schema:'https://schema.abi.invalid/v1/flow.json',id:'new_flow',name:'New flow',description:'',enabled:false,trigger:{type:'schedule',cron:'0 8 * * 1',timezone:'UTC'},
   steps:[{id:'notify',type:'action',action:'post_message',input:{channel:'inbox',text:'Weekly summary',data:{measures:[],dimensions:[]}}}]}},
 ml:{label:'ML model',save:o=>api('save_ml_model',{ml:o}),del:o=>api('delete_ml_model',{model:o.id}),
  tpl:{$schema:'https://schema.abi.invalid/v1/ml.json',id:'new_model',name:'New model',task:'forecast',dataset:'',target:'',time:'calendar.day',horizon:28,method:'auto'}},
 tool:{label:'tool',save:o=>api('save_tool',{tool:o}),del:o=>api('delete_tool',{tool:o.id}),extra:[['Publish',o=>api('publish_tool',{tool:o.id})]],
  tpl:{$schema:'https://schema.abi.invalid/v1/tool.json',id:'new_tool',name:'New tool',description:'',inputs:{type:'object',additionalProperties:false,properties:{},required:[]},
   outputs:{type:'array',items:{type:'object'}},permissions:{role:'viewer'},query:{measures:[],dimensions:[],filters:[]},tests:[]}},
 agent:{label:'agent',save:o=>{const {cases,...a}=o;return api('save_agent',cases?{agent:a,cases}:{agent:a})},del:o=>api('delete_agent',{agent:o.id}),extra:[['Publish',o=>api('publish_agent',{agent:o.id})]],
  tpl:{$schema:'https://schema.abi.invalid/v1/agent.json',id:'new_agent',name:'New agent',purpose:'',instructions:'',mode:'tools_only',grounding:true,channels:[{type:'in_app'}],
   scope:{measures:[],tables:[]},tools:['semantic_query'],cases:[{question:'',expect:{contains:[]}}]}},
 dataset:{label:'query',save:o=>api('save_dataset',{id:o.id,name:o.name,description:o.description||null,query:o.query}),del:o=>api('delete_dataset',{id:o.id}),
  tpl:{id:'new_dataset',name:'New query',description:'',query:{measures:[],dimensions:[]}}},
 report:{label:'report',save:o=>api('save_report',{report:o}),del:o=>api('delete_report',{report:o.id}),
  tpl:{$schema:'https://schema.abi.invalid/v1/report.json',id:'new_report',name:'New report',pages:[{id:'main',name:'Main',visuals:[{id:'kpi-1',type:'kpi',title:'',query:{measures:[],dimensions:[]},layout:{x:0,y:0,w:3,h:3}}]}]}}};
function clean(o){const x=JSON.parse(JSON.stringify(o));SERVER_KEYS.forEach(k=>delete x[k]);return x}
function editor(kind,obj,isNew){const K=KIND[kind],c=card(isNew?`New ${K.label}`:(obj.name||obj.id),isNew?'':'JSON, checked by ABI on save');c.classList.add('editor');
 const ta=h('textarea','f ed code');ta.value=JSON.stringify(clean(obj),null,2);ta.rows=Math.min(28,ta.value.split('\n').length+1);ta.spellcheck=false;c.append(ta);
 const parse=()=>{try{return JSON.parse(ta.value)}catch(e){throw new Error('Not valid JSON: '+e.message)}};
 act(c,isNew?'Create':'Save',async()=>{await K.save(parse());return 'Saved through ABI. It shows as an uncommitted change until you commit.'},1);
 act(c,'Format',()=>{ta.value=JSON.stringify(parse(),null,2)});
 return c}
function editBtns(c,kind,obj){const K=KIND[kind];let ed=null;
 act(c,'Edit',()=>{if(ed){ed.remove();ed=null;return}ed=editor(kind,obj,false);c.after(ed);ed.scrollIntoView({behavior:'smooth',block:'nearest'})});
 (K.extra||[]).forEach(([l,f])=>act(c,l,async()=>{await f(obj);return `${l}ed.`}));
 act(c,'Delete',async()=>{await K.del(obj);return 'Deleted.'})}
function newBtn(box,kind){const bar=box.querySelector(':scope>.bar.top')||h('div','row bar top');
 bar.append(btn(`New ${KIND[kind].label}`,()=>{const c=editor(kind,KIND[kind].tpl,true);box.querySelector('.cards')?.prepend(c)||box.append(c);c.querySelector('textarea').focus()},1));
 if(!bar.parentNode)box.prepend(bar)}

// ---------- Automate ----------
function flows(box){newBtn(box,'flow');const g=h('div','cards');box.append(g);if(!D.flows.length)box.append(h('p','empty','No flows yet: New flow, or ask the agent.'));
 D.flows.forEach(f=>{const t=f.trigger||{},c=card(f.name||f.id,f.enabled?'enabled':'disabled');
  c.append(h('div','file',`${t.type||'manual'} trigger${t.cron?' · '+t.cron:''}${t.measure?` · ${t.measure} ${t.op} ${t.value}`:''} · flows/${f.id}.json`));
  const st=h('div');(f.steps||[]).forEach(s=>st.append(h('span','chip',`${s.id}: ${s.action||s.type}`)));const tr=h('ol','trace');c.append(st,tr);
  act(c,'Run now',async m=>{tr.innerHTML='';const r=await api('run_flow',{flow:f.id});say(m,`run started: ${r.run}`);
   for(let i=0;i<150;i++){await wait(2000);const x=await api('flow_runs',{flow:f.id,run:r.run}),one=x.runs?.[0]||x;tr.innerHTML='';
    (one.steps||[]).forEach(s=>tr.append(h('li',s.status==='ok'?'ok':s.status==='failed'?'bad':'',`${s.step||s.id}: ${s.status}${s.error?' · '+s.error:''}`)));
    if(['ok','failed','stopped','rejected','waiting'].includes(one.status))return `run ${one.status}`}return 'still running; see ABI\'s flow runs'},1);
  act(c,f.enabled?'Disable':'Enable',async()=>{await api('enable_flow',{flow:f.id,enabled:!f.enabled});f.enabled=!f.enabled;show('flows')});editBtns(c,'flow',f);g.append(c)})}
function ml(box){newBtn(box,'ml');const g=h('div','cards');box.append(g);if(!D.ml.length)box.append(h('p','empty','No ML models yet: New ML model needs a query.'));
 D.ml.forEach(m=>{const c=card(m.name||m.id,`ml/${m.id}.json`);c.append(h('div','file',`${m.task}${m.target?' of '+m.target:''} on query ${m.dataset}${m.horizon?`, ${m.horizon} ahead`:''}`));
  const st=h('div','kv');st.style.marginTop='12px';const lr=()=>{const r=m.latest_run||{};st.innerHTML='';
   [['latest run',r.kind||'—'],['status',r.status||'—'],['finished',cell(r.finished)||'—'],...Object.entries(r.metrics||{}).map(([a,b])=>[a,typeof b==='number'?fmt(b):cell(b)])].forEach(([a,b])=>st.append(h('b','',a),h('span',a==='status'?(b==='ok'?'ok':b==='failed'?'bad':''):'',String(b))))};lr();c.append(st);
  const follow=async(kind,m2)=>{await api(kind,{model:m.id});say(m2,`${kind==='train_model'?'training':'scoring'}…`);
   for(let i=0;i<60;i++){await wait(3000);const x=(await api('ml_models',{})).models.find(y=>y.id===m.id);if(x){Object.assign(m,x);lr()}
    if(['ok','failed'].includes(m.latest_run?.status)&&m.latest_run?.finished)return `${m.latest_run.kind}: ${m.latest_run.status}`}return 'still running'};
  act(c,'Train',m2=>follow('train_model',m2),1);act(c,'Score',m2=>follow('score_model',m2));editBtns(c,'ml',m);g.append(c)})}
function tools(box){newBtn(box,'tool');const g=h('div','cards');box.append(g);if(!D.tools.length)box.append(h('p','empty','No tools yet.'));
 D.tools.forEach(t=>{const c=card(t.name||t.id,`tools/${t.id}.json · ${t.status||''}`);c.append(h('div','',t.description||''));const ins={};
  Object.entries(t.inputs?.properties||{}).forEach(([k,s])=>{const i=h('input','f');i.placeholder=s.description||'';ins[k]=i;c.append(h('label','l',k),i)});
  const out=h('div');
  act(c,'Run',async()=>{rows(out,await api('run_tool',{tool:t.id,inputs:Object.fromEntries(Object.entries(ins).filter(([,i])=>i.value).map(([k,i])=>[k,i.value]))}))},1);
  act(c,'Run its tests',async()=>{rows(out,await api('test_tool',{tool:t.id}))});editBtns(c,'tool',t);c.append(out);g.append(c)})}
function agents(box){newBtn(box,'agent');const g=h('div','cards');box.append(g);if(!D.agents.length)box.append(h('p','empty','No custom agents yet.'));
 D.agents.forEach(a=>{const c=card(a.name||a.id,`agents/${a.id}.json · ${a.status||''}`);c.append(h('div','file',(a.purpose||a.instructions||'').slice(0,240)));
  const q=h('input','f');q.placeholder='e.g. revenue in North?';const out=h('div','msg');c.append(h('label','l','Question'),q);
  act(c,'Ask',async()=>{const r=await api('run_agent',{agent:a.id,question:q.value});out.textContent=r.answer||JSON.stringify(r,null,1)},1);
  act(c,'Run evaluation set',async()=>{const r=await api('test_agent',{agent:a.id});out.textContent=(r.cases||[]).map(x=>`${x.passed?'✓':'✗'} ${x.question}${x.missing?.length?' · missing '+x.missing.join(', '):''}`).join('\n')||JSON.stringify(r,null,1)});editBtns(c,'agent',a);c.append(out);g.append(c)})}

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

// ---------- Knowledge: the agent team ----------
// ---------- sidebar ----------
const PENDING={filters:null,explore:null};
const LEDE={vault:'Your business documents: definitions, finance rules, KPI specs. The agents search them before they define or check a measure.',home:'Start here: ask anything, see what needs attention, and every part of the project at a glance.',datasets:'Saved questions to the model (measures by dimensions, with filters). Each run returns a fresh table; nothing is stored. Reports, flows and ML reuse them. Preview their rows.',
 measures:'The business numbers. Edit a formula and ABI checks it before it saves; Test shows the total.',model:'Tables and how they join. Facts are black, dimensions are outlined.',
 chat:'Talk to the agent: ask a question, ask where something is, or ask for a change. When it shows you a page, the chat moves to the side and goes on.',reports:'The saved reports of this project. Open one to filter it and click through.',monitor:'Is anything wrong right now: data freshness, model readiness, ML runs and flows. Reports show the business; this shows the system.',explore:'Your own analysis: pick measures, a dimension and a filter; the numbers come from the model, so they match the reports. Keep the ones you want; nothing in the project changes.',glossary:'What the business words mean and which measures implement them.',sources:'Bronze: the data as it landed, one source per file or bucket folder. Reload, add or delete a source.',
 staged:'Silver: the cleaned and combined tables between the sources and the final tables. Hidden from the model; edit their steps here.',tables:'Gold: the final business tables the model and measures are built on. Edit kind, key, description and steps.',flows:'Automations. Run one now and follow its steps, or switch it on or off.',
 ml:'Forecasts, segments and anomaly models. Train, score and read the latest run.',tools:'Calculations your agents can call. Run one with inputs or run its tests.',
 agents:'Custom agents for this project. Ask one a question or run its evaluation set.',docs:'The project documentation ABI writes from its definitions.',
 security:'Who sees which rows. Roles are set in ABI itself.',environments:'Where each environment reads its data from.',history:'Every change is a commit in ABI. Newest first.'};let PAGES={},CUR='home';const side=$('aside');
function nav(){return [['Start',[['home','Home',home],['chat','Chat',chatPage]]],
 ['Data',[['sources','Sources',sources,D.sources.length],['staged','Staged',stagedPage,(D.layers||[]).filter(t=>t.layer==='silver').length],['tables','Tables',tablesPage,(D.model?Object.keys(D.model.tables||{}).length:D.tables.length)]]],
 ['Data Model',[['model','Diagram',modelCanvas],['measures','Measures',measures,D.measures.length],
 ['glossary','Glossary',glossary,D.glossary.length],['datasets','Queries',datasets,D.datasets.length]]],
 ['Visualizations',[['reports','Reports',reportsPage,D.reports.length],['monitor','Monitor',monitorPage],['explore','Explore',explore,MINE.length||'']]],
 ['Automate',[['flows','Flows',flows,D.flows.length],['ml','ML models',ml,D.ml.length],['tools','Tools',tools,D.tools.length],['agents','Agents',agents,D.agents.length]]],
 ['Knowledge',[['vault','Vault',vaultPage,(VAULT||[]).length],['docs','Docs',docs]]],
 ['Govern',[['security','Security',security],['environments','Environments',b=>b.insertAdjacentHTML('beforeend',D.sections.Environments)],['history','History',commits,(D.history||[]).length]]]]}
let MODE='developer',VAULT=[];
let FOLD={};try{FOLD=JSON.parse(localStorage.getItem('abi-fold')||'{}')}catch(e){}
function sidebar(){side.innerHTML='';PAGES={};nav().forEach(([g,items])=>{const sec=h('div','sec'),hd=h('button','grp'),list=h('div','items');
 hd.append(h('span','',g),h('span','car'));hd.setAttribute('aria-expanded',String(!FOLD[g]));sec.classList.toggle('folded',!!FOLD[g]);
 hd.onclick=()=>{FOLD[g]=!FOLD[g];sec.classList.toggle('folded',FOLD[g]);hd.setAttribute('aria-expanded',String(!FOLD[g]));try{localStorage.setItem('abi-fold',JSON.stringify(FOLD))}catch(e){}};
 items.forEach(([k,label,fn,n])=>{PAGES[k]=[label,fn];const b=h('button','it');b.dataset.k=k;
  b.append(h('span','',label));if(n!==undefined&&n!=='')b.append(h('span','n',String(n)));b.onclick=()=>show(k);list.append(b)});
 sec.append(hd,list);side.append(sec)});D.reports.forEach(r=>PAGES['rep:'+r.id]=[r.name||r.id,b=>report(b,r)])}
let PREV='home';
function show(k){if(!PAGES[k])k='home';if(k!=='chat'&&CUR!=='chat')PREV=k;else if(k==='chat'&&CUR!=='chat')PREV=CUR;{const cb=$('#chat');if(cb.classList.contains('full')){cb.classList.remove('full');$('#chat .wide-t').textContent='Full page';document.body.append(cb);if(k!=='chat')openChat(true)}}CUR=k;setTimeout(ctx);const sk=k.startsWith('rep:')?'reports':k;side.querySelectorAll('button.it').forEach(b=>b.classList.toggle('on',b.dataset.k===sk));
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
function setMode(md){MODE=md;setTimeout(()=>{ctx();const g=log.querySelector('.hello');if(g)g.replaceWith(hello())});document.body.dataset.mode=md;$('#m-dev').classList.toggle('on',md==='developer');$('#m-view').classList.toggle('on',md==='viewer');
 $('#chat textarea').placeholder=md==='viewer'?'Ask a question. In Viewer the agent only reads.':'Ask a question or for a change.'}
async function pickMode(md){if(!LIVE){toast('Snapshot: start the app to switch modes.');return}
 await fetch('/mode',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify({mode:md})})}
function vc(){const n=D.pending?.files?.length||0,cb=$('#changes'),pb=$('#publish');cb.textContent=n?`${n} change${n>1?'s':''}`:'No changes';cb.classList.toggle('pri',!!n);
 const head=(D.history||[])[0]?.id?.slice(0,7),pubc=(D.published?.commit||'').split(' ')[0];pb.disabled=!!n||!head||head===pubc;
 pb.title=n?'Commit first: Publish sends the last commit.':head===pubc?'Viewer already shows the last commit.':'Make the last commit what Viewer sees.'}
$('#changes').onclick=()=>{const d=$('#commit'),f=$('.files',d);f.innerHTML='';const fl=D.pending?.files||[];
 if(!fl.length)f.append(h('p','empty','Everything is committed.'));fl.forEach(x=>{const det=h('details');const su=h('summary','',`${x.status}  ${x.path}`);det.append(su,h('pre','',x.patch||''));f.append(det)});
 $('#c-message').value=D.pending?.proposed_message||'';$('.msg',d).textContent='';$('#c-undo').disabled=!D.pending?.can_undo;d.showModal()};
$('#c-close').onclick=()=>$('#commit').close();
$('#c-commit').onclick=async()=>{try{await api('commit',{message:$('#c-message').value||'Changes from the app'});$('#commit').close();toast('Committed')}catch(e){say($('#commit .msg'),e.message,1)}};
$('#c-undo').onclick=async()=>{try{const r=await api('undo',{});say($('#commit .msg'),`Undone: ${r.undone}`);D.pending=await api('changes',{});$('#changes').onclick()}catch(e){say($('#commit .msg'),e.message,1)}};
$('#publish').onclick=async()=>{if(!LIVE)return;if(!await ask({message:'Publish the last commit? Viewer will show it from now on.',preview:(D.history||[])[0]?.message||''}))return;
 toast('Publishing: ABI checks the last commit…');const post=(u,b)=>fetch(u,{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify(b||{})}).then(r=>r.json());
 let r=await post('/publish');if(r.confirm){if(!await ask(r.confirm)){toast('Not published.');return}r=await post('/confirm/'+r.confirm.id)}
 toast(r.error?`Publish failed: ${r.error}`:`Published ${r.result.commit}`)};
$('#menu').onclick=()=>document.body.classList.toggle('menu-open');side.addEventListener('click',e=>{if(e.target.closest('button.it'))document.body.classList.remove('menu-open')});
$('#m-dev').onclick=()=>pickMode('developer');$('#m-view').onclick=()=>pickMode('viewer');
let first=decodeURIComponent(location.hash.slice(1));if(!first)try{first=localStorage.getItem('abi-app-'+D.project)}catch(e){}show(first||'home');
boot.then(()=>{sidebar();show(CUR)});setMode('developer');vc();
window.addEventListener('hashchange',()=>{const k=decodeURIComponent(location.hash.slice(1));if(k&&k!==CUR)show(k)});

// ---------- the agent: it steers this page, the page follows its changes, and the person talks to it here ----------
function toast(t){const x=h('div','toast',t);document.body.append(x);setTimeout(()=>x.remove(),6000)}
function highlight(item){if(!item)return;const want=String(item).toLowerCase();
 const c=[...document.querySelectorAll('main .card')].find(c=>c.textContent.toLowerCase().includes(want));if(c){c.classList.add('hl');c.scrollIntoView({behavior:'smooth',block:'center'});setTimeout(()=>c.classList.remove('hl'),4000)}}
const chatBox=$('#chat'),log=$('#chat .log'),inp=$('#chat textarea'),busy=$('#chat .busy');
const md=t=>t.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])).replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>').replace(/`([^`]+)`/g,'<code>$1</code>');
function bubble(cls,text){const b=h('div','m '+cls);if(cls==='ai')b.innerHTML=md(text);else b.textContent=text;log.append(b);log.scrollTop=log.scrollHeight;return b}
let steps=null;
// Chat page: the same conversation as the side panel, full width; leaving the page (or the agent showing one) puts it back at the side
function chatPage(box){$('#chat .wide-t').textContent='Side panel';chatBox.classList.add('full','open');document.body.classList.remove('chat-open');box.append(chatBox);inp.focus()}
function openChat(on){chatBox.classList.toggle('open',on??!chatBox.classList.contains('open'));document.body.classList.toggle('chat-open',chatBox.classList.contains('open'));if(chatBox.classList.contains('open'))inp.focus()}
const STEP={abi_show:'opened a page',semantic_query:'asked the model',visual_data:'read a chart',abi_vault:'searched the vault',abi_tools:'picked tools',
 semantic_model:'read the model',readiness:'checked readiness',data_quality:'checked data quality',how_calculated:'looked up a formula',set_measure:'saved a measure',
 save_report:'saved a report',save_flow:'saved a flow',commit:'committed',changes:'listed changes',subagent:'asked the team',abi_prefs:'read your preferences',read:'read a file'};
const stepName=t=>{const k=String(t).replace('mcp__abi__','');return STEP[k]||k.replace(/_/g,' ')};
function hello(){const d=h('div','hello'),pg=PAGES[CUR]?.[0]||'this page';d.append(h('p','',MODE==='viewer'
  ?'Ask about the numbers. I read the published project only, and every number comes from ABI.'
  :'Ask a question, ask where something is, or say what to build or change. Changes stay uncommitted until you commit them.'));
 [`What is on ${pg}, and what should I look at?`,...(MODE==='viewer'?['What changed last month, and why?','Where do I see revenue by country?']
  :['What is still open in this project?','Where are the flows, and what do they do?'])].forEach(t=>{const b=h('button','sg',t);b.onclick=()=>{inp.value=t;sendChat()};d.append(b)});return d}
function ctx(){const c=$('#chat .ctx'),g=log.querySelector('.hello');if(c)c.textContent=`${MODE==='viewer'?'Viewer · read-only':'Developer'} · on ${PAGES[CUR]?.[0]||CUR}`;if(g&&CUR!=='chat')g.replaceWith(hello())}
const INTRO='';log.append(hello());
async function cpost(u,b){return fetch(u,{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify(b||{})}).then(r=>r.json())}
async function chats(sel){if(!LIVE)return;const r=await cpost('/chat/list'),pk=$('#chat-pick');pk.innerHTML='';
 const cur=sel||r.current;if(!r.chats.some(c=>c.id===cur))pk.append(new Option('New chat',cur||''));
 r.chats.forEach(c=>pk.append(new Option(`${c.title||'Chat'} · ${c.messages}`,c.id)));pk.value=cur||'';
 const del=$('#chat-del');if(del)del.hidden=!r.chats.length}
function paint(c){log.innerHTML='';steps=null;(c.messages||[]).forEach(m=>bubble(m.role==='me'?'me':'ai',m.text));if(!log.children.length)log.append(hello())}
$('#chat-new').onclick=async()=>{const c=await cpost('/chat/new');paint(c);chats(c.id);inp.focus()};
$('#chat-pick').onchange=async e=>{if(!e.target.value)return;const c=await cpost('/chat/open',{id:e.target.value});paint(c);chats(c.id)};
$('#chat-toggle').onclick=()=>{openChat();chats()};$('#chat .wide-t').onclick=()=>show(chatBox.classList.contains('full')?(PREV||'home'):'chat');$('#chat .x').onclick=()=>openChat(false);
async function sendChat(){const t=inp.value.trim();if(!t)return;if(!LIVE){bubble('sys','Snapshot: start `python scripts/app.py '+D.project+'` to talk to the agent.');return}
 log.querySelector('.hello')?.remove();bubble('me',t);inp.value='';steps=null;
 await fetch('/chat',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify({message:t,page:CUR})})}
$('#chat .send').onclick=sendChat;inp.onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendChat()}};
$('#chat .stop').onclick=()=>fetch('/chat/stop',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:'{}'});
if(LIVE){const es=new EventSource('/events?k='+encodeURIComponent(KEY)+'&c='+CID+'&m='+(MOBILE?1:0));
 es.onmessage=async ev=>{const e=JSON.parse(ev.data);
  if(e.type==='show'){PENDING.filters=e.filters||null;if(e.page==='explore'&&e.query){const q={measures:e.query.measures||[],dimensions:(e.query.dimensions||[]).slice(0,1),filters:e.query.filters||[]};PENDING.explore={...q,chart:e.chart};MINE.unshift({name:'Agent: '+(e.note||q.measures.join(', ')),q,type:e.chart||'bar',at:new Date().toISOString().slice(0,16).replace('T',' ')});MINE=MINE.slice(0,30);try{localStorage.setItem('abi-explore',JSON.stringify(MINE))}catch(x){}sidebar()}const nv=h('div','m nav','→ '+(e.page==='report'?'report '+e.report:e.page)+(e.note?' · '+e.note:''));nv.onclick=()=>show(e.page==='report'?'rep:'+e.report:e.page);log.append(nv);log.scrollTop=log.scrollHeight;const k=e.page==='report'?'rep:'+e.report:e.page;show(k);setTimeout(()=>highlight(e.item),400);if(e.note)toast('Agent: '+e.note)}
  else if(e.type==='data'){Object.assign(D,e.data);const k=CUR;sidebar();show(k);vc()}
  else if(e.type==='busy'){busy.hidden=!e.on;$('#chat .stop').hidden=!e.on}
  else if(e.type==='step'){if(!steps){steps=h('div','m steps');log.append(steps)}const last=[...steps.children].reverse().find(x=>x.dataset.t===e.tool&&x.dataset.s==='run');
   if(e.state==='run'||!last){const l=h('div','',`${e.state==='run'?'…':e.state==='ok'?'✓':'✗'} ${stepName(e.tool)}`);l.dataset.t=e.tool;l.dataset.s=e.state;steps.append(l)}
   else{last.textContent=`${e.state==='ok'?'✓':'✗'} ${stepName(e.tool)}`;last.dataset.s=e.state;last.className=e.state==='ok'?'':'bad'}log.scrollTop=log.scrollHeight}
  else if(e.type==='answer'){steps=null;bubble('ai',e.text);chats();if(!chatBox.classList.contains('open'))toast('The agent answered')}
  else if(e.type==='ask'){const yes=await ask({message:e.title||'ABI asks for your confirmation',preview:e.message});bubble('sys',yes?'You confirmed.':'You declined.');
   fetch('/chat/answer',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify({id:e.id,yes})})}
  else if(e.type==='mode'){setMode(e.mode);if(e.mobile)document.documentElement.classList.add('mobile');show(CUR);if(e.mode==='viewer'&&!e.published)toast('Nothing published yet: Viewer shows the working version until you publish.')}
  else if(e.type==='vault'){await loadVault();sidebar();show(CUR)}
  else if(e.type==='note')toast(e.text)}}
"""


def read(action: str, project: str, **kw):
    try:
        return abi.call(action, project=project, **kw)
    except abi.ActionError:
        return None


def pub_id(project: str) -> str:
    """The published copy Viewer reads: a sibling ABI project that changes only by Publish."""
    return f"{project}_pub"[:63]


def published(project: str) -> dict:
    f = abi.folder(project).parent / "_views" / project / "published.json"
    return json.loads(f.read_text()) if f.exists() else {}


def snapshot(dest: Path, project: str) -> dict:
    """What the page shows: ABI's live state (uncommitted changes included); a few definitions only the folder has
    (a dataset's query, a source's location) come from the last commit."""
    j = render.j
    sm = read("semantic_model", project) or {}
    measures = sm.get("measures") or (j(dest / "model/measures.json", {}) or {}).get("measures", [])
    by_id = {m["id"]: m for m in measures}
    reports = []
    for rep in sorted((read("list_reports", project) or {}).get("reports") or [], key=lambda r: r.get("id", "")):
        pages = render.report_pages(project, rep["id"], rep, by_id)
        for p, spec in zip(pages, rep.get("pages", [])):  # the dimension a click filters on
            dims = {v["id"]: ((v.get("query") or {}).get("dimensions") or [None])[0] for v in spec.get("visuals", [])}
            for v in p["visuals"]:
                v["dim"] = dims.get(v["id"])
        reports.append({"id": rep["id"], "name": rep.get("name"), "pages": pages, "spec": rep})
    dims = sorted(f"{t}.{c}" for t, cols in (sm.get("columns") or {}).items() if t in (sm.get("tables") or {})
                  for c, ty in cols.items() if any(k in str(ty) for k in ("VARCHAR", "BOOLEAN")) or t == "calendar")
    g = lambda d: [j(f, {}) for f in sorted((dest / d).glob("*.json")) if not f.name.endswith(".eval.json")]  # noqa: E731
    live = lambda action, key, d: ((read(action, project) or {}).get(key)) or g(d)  # noqa: E731 (status from ABI, else files)
    queries = {d["id"]: d for d in (j(dest / "model/datasets.json", {}) or {}).get("datasets", [])}
    table_steps = {f.stem: (j(f, {}) or {}).get("steps") for f in sorted((dest / "tables").glob("*.json"))}
    sources = {f.stem: j(f, {}) for f in sorted((dest / "sources").glob("*.json"))}
    model = {"relationships": sm.get("relationships") or [], "tables": sm.get("tables") or {}, "date_table": sm.get("date_table")}
    sections = render.sections(dest)
    kinds = {t: (v or {}).get("kind") for t, v in model["tables"].items()}
    sections["Model"] = f"<div class=card>{render.diagram(model, kinds)}</div>"
    out = {"project": project, "measures": measures, "reports": reports, "dims": dims, "flows": live("list_flows", "flows", "flows"),
           "ml": live("ml_models", "models", "ml"), "tools": live("list_tools", "tools", "tools"),
           "agents": live("list_agents", "agents", "agents"), "glossary": sm.get("glossary") or [],
           "datasets": [queries.get(d["id"], {}) | d for d in sm.get("datasets") or []], "model": model,
           "columns": sm.get("columns") or {}, "sources": sorted(sources), "source_specs": sources, "table_steps": table_steps,
           "tables": sorted(model["tables"]), "readiness": read("readiness", project), "pending": read("changes", project),
           "quality": read("data_quality", project), "rls": read("rls_rules", project), "history": (read("history", project) or {}).get("commits", []),
           "docs": read("docs", project), "sections": sections, "published": published(project.removesuffix("_pub")),
           "stamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
    q = out["quality"] or {}
    qi = {c: i for i, c in enumerate(q.get("columns") or [])}
    model_t = set(model["tables"])
    def layer(t: str) -> str:
        if t in model_t:
            return "gold"
        if t in sources or t.startswith(("bronze", "ref_")):
            return "bronze"
        return "silver"
    out["layers"] = [{"table": r[qi["table"]], "rows": r[qi["rows"]], "layer": layer(r[qi["table"]])} for r in q.get("rows") or []] if qi else []
    out["summary"] = {k: len(out[k]) for k in ("sources", "tables", "measures", "reports", "datasets", "flows", "ml", "tools", "agents")}
    return out


def publish_bundle(project: str) -> tuple[str, dict, str]:
    """Commit is ABI's; Publish copies the last commit (never uncommitted work) into the published project, through
    ABI's import checks (and its confirmations). Viewer reads only that copy."""
    abi.sync(project)
    dest = abi.folder(project)
    bundle = {}
    for f in sorted(dest.rglob("*")):
        rel = f.relative_to(dest).as_posix()
        if f.is_file() and ".git" not in f.relative_to(dest).parts and f.name not in (".gitkeep", "app.html"):
            bundle[rel] = json.loads(f.read_text()) if rel.endswith(".json") else f.read_text()
    pub = pub_id(project)
    if not abi.folder(pub):
        abi.new_project(dest.parent.name, pub, f"{(bundle.get('project.json') or {}).get('name', project)} (published)")
    last = (abi.call("history", project=project).get("commits") or [{}])[0]  # ABI's last commit, not the local skeleton one
    return pub, bundle, f"{last.get('id', '')[:7]} {last.get('message', '')}".strip()


def published_now(project: str, pub: str, head: str) -> dict:
    for src in sorted((abi.folder(project) / "sources").glob("*.json")):  # the import writes definitions; load the data
        try:
            abi.call("refresh", project=pub, table=src.stem)
        except abi.ActionError:
            pass  # reported by the published copy's data quality
    info = {"commit": head, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "project": pub}
    f = abi.folder(project).parent / "_views" / project / "published.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(info))
    abi.sync(pub)
    return info


def build(project: str) -> Path:
    dest = abi.folder(project)
    if not dest:
        raise SystemExit(f"No folder for project '{project}'; create it with `abi.py project`.")
    data = json.dumps(snapshot(dest, project), default=str).replace("</", "<\\/")
    name = (render.j(dest / "project.json", {}) or {}).get("name", project)
    page = render.shell(name, "", "", "")
    head = (f"<header><button class=btn id=menu aria-label=Menu>Menu</button><span class=brand>ABI</span><span class=crumb>{render.esc(dest.parent.name)}<i>/</i><b>{render.esc(project)}</b></span>"
            "<span class=snap hidden>snapshot</span><span class=right><span class='vc w'><button class=btn id=changes>No changes</button>"
            "<button class=btn id=publish title='Make the last commit what Viewer sees'>Publish</button></span><span class=modes role=group aria-label=Mode>"
            "<button id=m-dev class=on title='Build and change: you and the agent'>Developer</button>"
            "<button id=m-view title='Read only: the agent answers as an analyst'>Viewer</button></span>"
            "<button class=btn id=theme title='Paper or ink'>Ink</button><button class='btn pri' id=chat-toggle>Ask the agent</button></span></header>")
    page = re.sub(r"<header>.*?</header>", lambda _: head, page, count=1, flags=re.S)
    page = page.replace("<main></main>", "<aside></aside><main></main>"
                        "<section id=chat><header><select class=f id=chat-pick aria-label='Chats'></select><button class=btn id=chat-new>New chat</button>"
                        "<button class='btn wide-t' title='Full page or side panel'>Full page</button><button class='btn x'>Close</button></header>"
                        "<div class=log></div><div class=busy hidden><span>working…</span><button class='btn stop'>Stop</button></div><div class=ctx></div>"
                        "<div class=compose><textarea class=f placeholder='Ask, or say what to change. Enter sends, Shift+Enter is a new line.'></textarea>"
                        "<button class='btn pri send'>Send</button></div></section>"
                        "<dialog id=commit><h3>Changes not committed yet</h3><div class=files></div><label class=l for=c-message>Commit message</label>"
                        "<input class=f id=c-message><div class=row><button class='btn pri' id=c-commit>Commit</button><button class=btn id=c-undo>Undo last change</button>"
                        "<button class=btn id=c-close>Close</button></div><div class=msg></div></dialog>"
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

    def __init__(self, project: str, url: str, agent_key: str, cid: str = "", mobile: bool = False):
        self.project, self.url, self.agent_key, self.cid, self.mobile = project, url, agent_key, cid, mobile
        self.clients: list = []
        self.lock = threading.Lock()
        self.pi = None
        self.busy = False
        self.mode = "viewer" if mobile else "developer"  # this browser's switch (a phone is always Viewer)
        # chats: one Pi session each (its own context), kept on this machine next to the views; one runs at a time
        self.chats_dir = abi.folder(project).parent / "_views" / project / "chats"
        self.chats_dir.mkdir(parents=True, exist_ok=True)
        self.chat_id = None

    def chat_file(self, cid: str) -> Path:
        return self.chats_dir / f"{cid}.json"

    def chat_list(self) -> list[dict]:
        out = []
        for f in self.chats_dir.glob("*.json"):
            try:
                c = json.loads(f.read_text())
            except ValueError:
                continue
            out.append({k: c.get(k) for k in ("id", "title", "created", "updated", "mode")} | {"messages": len(c.get("messages", []))})
        return sorted(out, key=lambda c: c.get("updated") or "", reverse=True)

    def chat_load(self, cid: str) -> dict:
        f = self.chat_file(cid)
        return json.loads(f.read_text()) if f.exists() else {"id": cid, "title": "New chat", "messages": []}

    def chat_note(self, role: str, text: str) -> None:
        c = self.chat_load(self.chat_id)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        c.setdefault("created", now)
        c["updated"], c["mode"] = now, self.mode
        if role == "me" and c.get("title") in (None, "New chat"):
            c["title"] = re.sub(r"\s+", " ", text)[:60]
        c["messages"] = c.get("messages", []) + [{"role": role, "text": text}]
        self.chat_file(self.chat_id).write_text(json.dumps(c, indent=1))

    def chat_switch(self, cid: str | None) -> dict:
        """A new or another chat: the running agent stops; the next message starts that chat's own session."""
        if self.pi and self.pi.poll() is None:
            self.pi.terminate()
        self.pi, self.busy = None, False
        self.chat_id = cid or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        self.emit({"type": "busy", "on": False})
        return self.chat_load(self.chat_id)

    def emit(self, event: dict) -> None:
        line = f"data: {json.dumps(event, default=str)}\n\n".encode()
        with self.lock:
            for q in list(self.clients):
                q.append(line)

    def current(self) -> str:
        """Viewer reads the published copy; Developer the project itself."""
        return pub_id(self.project) if self.mode == "viewer" and abi.folder(pub_id(self.project)) else self.project

    def refresh(self) -> None:
        """After a change through ABI (anyone's): every open browser gets its mode's data again."""
        changed(self.project)

    def push_data(self) -> None:
        cur = self.current()
        self.emit({"type": "data", "data": cached(cur)})

    def chat(self, message: str, page: str) -> None:
        if not self.chat_id:
            self.chat_switch(None)
        if not self.pi or self.pi.poll() is not None:
            env = {**os.environ, "ABI_TOKEN": abi.token(), "ABI_APP_URL": self.url, "ABI_APP_PROJECT": self.project,
                   "ABI_APP_AGENT_KEY": self.agent_key, "ABI_APP_CLIENT": self.cid}
            session = str(self.chats_dir / f"{self.chat_id}.session.jsonl")  # Pi's own transcript: this chat's context
            self.pi = subprocess.Popen(["pi", "--mode", "rpc", "--session", session], cwd=abi.ROOT, env=env, text=True, bufsize=1,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            threading.Thread(target=self.read, daemon=True).start()
        if self.mode == "viewer":
            where = (f"project {self.current()} (the published version of {self.project}); Viewer mode: you are the analyst,"
                     f" read-only; answer from {self.current()} only")
        else:
            where = (f"project {self.project}; Developer mode: you build and change through ABI. Changes stay uncommitted"
                     " until the person commits them, and Viewer sees them only after Publish")
        text = f"[In the app for {where}; the person is on its '{page}' page.] {message}"
        self.chat_note("me", message)
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
                    self.chat_note("ai", final)
                final = ""
                self.busy = False
                self.emit({"type": "busy", "on": False})


HUBS: dict[str, Hub] = {}
_SNAP: dict[str, dict] = {}
_SNAP_LOCK = threading.Lock()


def cached(project: str) -> dict:
    """One snapshot per project version, shared by every open browser (it runs every report's visual queries)."""
    with _SNAP_LOCK:
        if project not in _SNAP:
            _SNAP[project] = snapshot(abi.folder(project), project)
        return _SNAP[project]


def changed(project: str) -> None:
    def go():
        try:
            abi.sync(project)  # pull ABI's commits, re-render the folder views (writes app.html too)
            with _SNAP_LOCK:
                _SNAP.pop(project, None)
                _SNAP.pop(pub_id(project), None)
            for hub in list(HUBS.values()):
                if hub.clients:
                    hub.push_data()
        except Exception as e:  # noqa: BLE001 (a failed refresh keeps the pages as they were)
            for hub in list(HUBS.values()):
                hub.emit({"type": "note", "text": f"Couldn't refresh: {type(e).__name__}"})
    threading.Thread(target=go, daemon=True).start()


LOGIN = """<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>ABI</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#ebe4d4;color:#141311;font:14px ui-monospace,Menlo,monospace}
@media (prefers-color-scheme:dark){body{background:#131210;color:#e8e0cf}input,button{color:inherit}}
form{display:flex;flex-direction:column;gap:12px;width:min(320px,calc(100vw - 32px))}b{font:700 22px 'Courier New',monospace;letter-spacing:.08em}
input,button{font:inherit;padding:10px;border:2px solid currentColor;background:none}button{background:#141311;color:#ebe4d4;cursor:pointer}</style>
<form method=post action=/login><b>ABI</b><label for=p>Passphrase</label><input id=p name=p type=password autofocus autocomplete=current-password>
<button>Open</button><span>%s</span></form>"""


def serve(project: str, port: int = 8790, open_browser: bool = True, remote: str | None = None, tailnet: bool = False) -> ThreadingHTTPServer:
    """remote: a passphrase; then the app listens on every interface (your VPN) and asks for it before anything loads.
    tailnet: listen on every interface but answer only this machine and Tailscale devices (100.64.0.0/10, your tailnet)."""
    import ipaddress  # noqa: PLC0415
    try:  # Developer works uncommitted: Commit is the person's (or the agent's, when asked); Publish feeds Viewer
        abi.call("set_auto_commit", project=project, on=False)
    except abi.ActionError:
        pass
    path, key, pending = build(project), secrets.token_urlsafe(24), {}
    agent_key = os.environ.get("ABI_APP_AGENT_KEY") or secrets.token_urlsafe(24)
    host = f"127.0.0.1:{port}"
    url = ""
    cookies: set = set()  # sessions that gave the passphrase (memory only)
    fails = {"n": 0}

    def hub_for(cid: str, mobile: bool = False) -> Hub:
        cid = re.sub(r"[^\w-]", "", cid)[:40] or "default"
        h = HUBS.get(cid)
        if not h:
            h = HUBS[cid] = Hub(project, url, agent_key, cid, mobile)
        if mobile and not h.mobile:
            h.mobile, h.mode = True, "viewer"
        return h

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # no request log: bodies may carry business values
            pass

        def reply(self, code: int, body: bytes, kind="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def allowed(self) -> bool:
            if not tailnet or tailnet == "open":
                return True
            ip = ipaddress.ip_address(self.client_address[0])
            return ip.is_loopback or ip in ipaddress.ip_network("100.64.0.0/10")

        def authed(self) -> bool:
            if not self.allowed():
                return False
            if tailnet and not remote:
                return True  # Tailscale authenticated the device; the page key still guards every action
            if not remote:
                return self.headers.get("Host") == host
            c = re.search(r"abi_s=([\w-]+)", self.headers.get("Cookie", ""))
            return bool(c and c.group(1) in cookies)

        def do_GET(self):
            route, _, query = self.path.partition("?")
            qs = dict(x.split("=", 1) for x in query.split("&") if "=" in x)
            if not self.authed():
                if remote and route in ("/", "/app.html"):
                    return self.reply(200, (LOGIN % "").encode(), "text/html; charset=utf-8")
                return self.reply(404, b"{}")
            if route == "/events":  # EventSource can't send headers: the page key and this browser's id come in the query
                if not secrets.compare_digest(qs.get("k", ""), key):
                    return self.reply(403, b"{}")
                hub = hub_for(qs.get("c", ""), qs.get("m") == "1")
                self.send_response(200)
                for k, v in (("Content-Type", "text/event-stream"), ("Cache-Control", "no-store"), ("Connection", "keep-alive")):
                    self.send_header(k, v)
                self.end_headers()
                q: list = []
                with hub.lock:
                    hub.clients.append(q)
                try:
                    self.wfile.write(f"data: {json.dumps({'type': 'busy', 'on': hub.busy})}\n\ndata: {json.dumps({'type': 'mode', 'mode': hub.mode, 'mobile': hub.mobile, 'published': bool(abi.folder(pub_id(project)))})}\n\n".encode())
                    if hub.mode == "viewer":
                        threading.Thread(target=hub.push_data, daemon=True).start()
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
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}"
            if self.path == "/login" and remote:
                given = urllib.parse.parse_qs(raw.decode(errors="replace")).get("p", [""])[0]
                if secrets.compare_digest(given.encode(), remote.encode()):
                    fails["n"] = 0
                    tok = secrets.token_urlsafe(32)
                    cookies.add(tok)
                    self.send_response(303)
                    self.send_header("Set-Cookie", f"abi_s={tok}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200")
                    self.send_header("Location", "/")
                    self.end_headers()
                    return None
                fails["n"] += 1
                time.sleep(min(30, 2 ** min(fails["n"], 5)))  # a slow wrong passphrase
                return self.reply(200, (LOGIN % "Wrong passphrase.").encode(), "text/html; charset=utf-8")
            body = json.loads(raw)
            if self.path.startswith("/agent/"):  # the harness on this machine, with the agent key: steer pages, data changed
                if self.client_address[0] != "127.0.0.1" or not secrets.compare_digest(self.headers.get("X-Abi-Agent", ""), agent_key):
                    return self.reply(403, b'{"error":"forbidden"}')
                cid = self.headers.get("X-Abi-Client", "")
                targets = [HUBS[cid]] if cid in HUBS else [h for h in HUBS.values() if h.mode == "developer"]
                if self.path == "/agent/mode":
                    h0 = targets[0] if targets else None
                    return self.reply(200, json.dumps({"mode": h0.mode if h0 else "developer", "project": h0.current() if h0 else project}).encode())
                if self.path == "/agent/show" and body.get("project") in (project, pub_id(project)):
                    for h0 in targets:
                        h0.emit({"type": "show", **{k: body.get(k) for k in ("page", "report", "filters", "item", "note", "query", "chart")}})
                elif self.path == "/agent/changed":
                    changed(project)
                return self.reply(200, b"{}")
            if not self.authed():
                return self.reply(403, b'{"error":"forbidden"}')
            hub = hub_for(self.headers.get("X-Abi-Client", ""), self.headers.get("X-Abi-Mobile") == "1")
            # the page: a local Host (no DNS rebinding) and the per-start key (a custom header other sites can't send)
            if not secrets.compare_digest(self.headers.get("X-Abi-App", ""), key):
                return self.reply(403, b'{"error":"forbidden"}')
            if self.path == "/mode":  # the person's switch (the agent can't call this: it has no page key); a phone stays Viewer
                hub.mode = "viewer" if body.get("mode") == "viewer" or hub.mobile else "developer"
                hub.emit({"type": "mode", "mode": hub.mode, "mobile": hub.mobile, "published": bool(abi.folder(pub_id(project)))})
                threading.Thread(target=hub.push_data, daemon=True).start()
                return self.reply(200, b"{}")
            if self.path == "/publish":
                if hub.mode == "viewer":
                    return self.reply(403, b'{"error":"Publish from Developer."}')
                pub, bundle, head = publish_bundle(project)
                out = run("import_project", {"project": pub, "bundle": bundle}, None, lambda: published_now(project, pub, head))
                return self.reply(200, json.dumps(out, default=str).encode())
            if self.path == "/upload":  # a person's file into a table (IN-7 resumable upload, as abi.py load)
                if hub.mode == "viewer":
                    return self.reply(403, b'{"error":"Viewer mode is read-only."}')
                import base64  # noqa: PLC0415
                import tempfile  # noqa: PLC0415
                name = Path(str(body.get("name"))).name
                with tempfile.TemporaryDirectory() as d:
                    f = Path(d) / name
                    f.write_bytes(base64.b64decode(body.get("data") or ""))
                    try:
                        res = abi.upload(project, str(body.get("table")), f, abi.CSV_SAFE if f.suffix.lower() in (".csv", ".tsv") else None)
                        out = {"result": res}
                    except abi.ActionError as e:
                        out = {"error": str(e.body.get("message") if isinstance(e.body, dict) else e.body)}
                changed(project)
                return self.reply(200, json.dumps(out, default=str).encode())
            if self.path == "/team/answer":  # the person's Approve/Decline on Home: the step runs through ABI (and its confirmation)
                import team  # noqa: PLC0415 (sibling script)
                if hub.mode == "viewer":
                    return self.reply(403, json.dumps({"error": "Viewer mode is read-only. Switch to Developer to answer the team."}).encode())
                item = next((p for p in team.pending(project) if p["id"] == body.get("id")), None)
                if not item:
                    return self.reply(410, json.dumps({"error": "That step is no longer waiting."}).encode())
                if body.get("decision") != "approve":
                    team.pending_done(project, item["id"], "declined")
                    return self.reply(200, json.dumps({"result": "declined"}).encode())
                action = item["action"].removeprefix("mcp__abi__")
                if action not in ALLOWED:
                    return self.reply(403, json.dumps({"error": f"'{action}' is a step for ABI's own app."}).encode())
                return self.reply(200, json.dumps(run(action, {**item["args"], "project": project}, None,
                                                      lambda: team.pending_done(project, item["id"], "ok") or "done"), default=str).encode())
            if self.path == "/team":
                import team  # noqa: PLC0415 (sibling script)
                try:
                    return self.reply(200, json.dumps({"result": team.status(hub.current() if hub.mode == "viewer" else project)}, default=str).encode())
                except SystemExit as e:
                    return self.reply(200, json.dumps({"error": str(e)}).encode())
            if self.path.startswith("/vault/"):
                return self.reply(*vault_route(hub, self.path.removeprefix("/vault/"), body))
            if self.path == "/chat":
                hub.chat(str(body.get("message", ""))[:4000], str(body.get("page", "home")))
                return self.reply(200, b"{}")
            if self.path == "/chat/list":
                return self.reply(200, json.dumps({"chats": hub.chat_list(), "current": hub.chat_id}).encode())
            if self.path == "/chat/new":
                return self.reply(200, json.dumps(hub.chat_switch(None)).encode())
            if self.path == "/chat/open":
                cid = re.sub(r"[^\w-]", "", str(body.get("id")))
                return self.reply(200, json.dumps(hub.chat_switch(cid)).encode())
            if self.path == "/chat/delete":
                cid = re.sub(r"[^\w-]", "", str(body.get("id")))
                for f in hub.chats_dir.glob(f"{cid}.*"):
                    f.unlink()
                if cid == hub.chat_id:
                    hub.chat_switch(None)
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
                return self.reply(200, json.dumps(run(*job), default=str).encode())  # job: action, args, token[, after]
            action = self.path.removeprefix("/api/")
            if action not in ALLOWED or body.get("project") != project:
                return self.reply(403, json.dumps({"error": f"'{action}' is not available here; use ABI's app."}).encode())
            if hub.mode == "viewer" and (action in WRITES or action in READS_ONLY):
                return self.reply(403, json.dumps({"error": "Viewer mode is read-only. Switch to Developer in the top bar to change things."}).encode())
            body = {**body, "project": hub.current()}  # Viewer reads the published copy
            out = run(action, body, None)
            if "result" in out and action in WRITES:
                changed(project)
            self.reply(200, json.dumps(out, default=str).encode())

    def vault_route(hub: Hub, op: str, body: dict) -> tuple[int, bytes]:
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
            for h0 in HUBS.values():
                h0.emit({"type": "vault"})
        return 200, json.dumps({"result": out}).encode()

    def run(action: str, args: dict, token: str | None, after=None) -> dict:
        try:
            out = {"result": abi.call(action, confirm=token, **args)}
            if after:
                out["result"] = after()
            if (token and action in WRITES) or after:
                changed(project)
            return out
        except abi.ActionError as e:
            b = e.body if isinstance(e.body, dict) else {"message": str(e.body)}
            if e.status == 409 and b.get("confirm_token") and token is None:
                cid = secrets.token_urlsafe(12)
                pending[cid] = (action, args, b["confirm_token"], after)  # stays here: the page never sees the token
                preview = json.dumps(b["preview"], indent=1, default=str)[:4000] if b.get("preview") else ""
                return {"confirm": {"id": cid, "message": b.get("message", "Confirm?"), "preview": preview}}
            detail = "\n".join(f"{x.get('error')} {x.get('fix') or ''}".strip() for x in b.get("errors") or [] if isinstance(x, dict))
            return {"error": "\n".join(filter(None, [b.get("message"), detail])) or json.dumps(b)[:500]}

    srv = ThreadingHTTPServer(("0.0.0.0" if remote or tailnet else "127.0.0.1", port), H)
    srv.daemon_threads = True
    host = f"127.0.0.1:{srv.server_address[1]}"  # port 0 = any free port
    url = f"http://{host}"
    srv.hub = hub_for("default")
    srv.hubs = HUBS

    def watch() -> None:  # changes made outside this app (an agent team in a terminal, ABI's own app): follow ABI's history
        last = None
        while True:
            try:
                head = ((abi.call("history", project=project).get("commits") or [{}])[0].get("id"),
                        len((abi.call("changes", project=project) or {}).get("files") or []))
                if last is not None and head != last:
                    changed(project)
                last = head
            except Exception:  # noqa: BLE001 (ABI busy or restarting: try again next round)
                pass
            time.sleep(30)
    threading.Thread(target=watch, daemon=True).start()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    if open_browser:
        webbrowser.open(f"http://{host}/")
    return srv


def main(argv: list[str]) -> int:
    if not argv:
        raise SystemExit(__doc__)
    remote = os.environ.pop("ABI_APP_PASS", None) or None  # read once, then out of the environment (children don't get it)
    tailnet = "open" if "--open" in argv else "--tailnet" in argv  # --open: any device that can reach this machine
    argv = [a for a in argv if a not in ("--tailnet", "--open")]
    s = serve(argv[0], int(argv[1]) if len(argv) > 1 else 8790, open_browser=not (remote or tailnet), remote=remote, tailnet=tailnet)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    atexit.register(lambda: [h.pi.terminate() for h in HUBS.values() if h.pi and h.pi.poll() is None])  # the chats' agents stop with the app
    print(f"{abi.folder(argv[0]) / 'app.html'} · live at http://127.0.0.1:{s.server_address[1]}/ (Ctrl+C to stop)", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        s.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

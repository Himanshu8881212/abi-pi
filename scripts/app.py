#!/usr/bin/env python3
"""ABI's app on this machine: every workspace and project, one sidebar in pipeline order.

  Start           Home (ask, what needs attention, where things stand), Chat (the agent; it also navigates)
  Data            Sources (bronze), Staged (silver), Tables (gold), Edits (ABI's write-back on the gold tables)
  Data Model      Diagram, Measures, Glossary, Queries
  Visualizations  Reports, Monitor (freshness, readiness, ML runs, flows), Explore (your own analysis)
  Export          Board pack, Documents, One-pager, Data, Video, Project (the whole project as one zip)
  Automate        ML models, Tools, Agents, Flows
  Knowledge       Vault, Docs
  Govern          Security, Environments, History, Users & access (workspace owners)

    python scripts/app.py <project> [port] [--single] [--tailnet|--open]   # http://127.0.0.1:<port>/ (default 8790)

Everyone signs in with their own ABI account (ABI checks the password and MFA; the app keeps only the session, in
memory) and the app acts as them: their project roles, row-level security and reports; Developer only for editors and
owners; Viewer reads the published copy. Workspace owners get Users & access: ABI's invite_user, set_role and
remove_role, plus which sections and reports each person sees (the app's routes refuse the rest). --single: no
sign-in, this process's own ABI session (one person on this machine). A confirm token stays in this process: the page
gets ABI's message and preview and the person's click redeems it. Only the actions in ALLOWED pass.
"""
import atexit
import contextlib
import html
import json
import os
import re
import signal
import secrets
import shutil
import subprocess
import sys
import threading
import traceback
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import abi  # noqa: E402
import render  # noqa: E402

KEY_SLOT = "__ABI_APP_KEY__"
ME_SLOT = "null/*__ABI_ME__*/"
READS_ONLY = {"run_flow", "run_agent"}  # not changes to the project, but they act (send, call tools): Developer only
WRITES = {"set_measure", "delete_measure", "set_glossary_term", "delete_glossary_term", "save_model", "delete_model", "enable_flow", "train_model", "score_model", "refresh",
          "commit", "undo", "add_data", "delete_source", "delete_table", "set_table", "add_relationship", "remove_relationship",
          "save_dataset", "delete_dataset", "save_report", "delete_report", "save_flow", "delete_flow", "save_ml_model",
          "delete_ml_model", "save_tool", "publish_tool", "delete_tool", "save_agent", "publish_agent", "delete_agent", "set_steps", "edit_cells", "approve_edit", "reject_edit", "undo_edits", "create_input_table"}
READS = {"changes", "data_quality", "dataset_data", "docs", "edit_history", "edit_proposals", "export_visual",
         "flow_runs", "history", "how_calculated", "list_agents", "list_flows", "list_reports", "list_tools",
         "ml_models", "preview_steps", "readiness", "report_snapshot", "rls_rules", "run_tool", "semantic_model",
         "semantic_query", "test_agent", "test_tool", "visual_data"}
ALLOWED = READS | WRITES | READS_ONLY  # the page's actions; every other one is ABI's app's
STATIC = ["Model", "Sources", "Tables", "Security", "Environments"]  # explorer sections shown as they are

CSS = """
/* layout: a top bar (project, live state, the agent), a left rail grouped by what you want to do, one page at a time with
   its purpose and main actions up top; the agent opens as a drawer on the right and can steer the page.
   Swiss signal (render.CSS): hairlines, 8-pt spacing, signal colour only for what is active or live */
body{display:grid;grid-template-columns:248px minmax(0,1fr);grid-template-rows:auto 1fr;min-height:100vh}
body::before{content:"";position:fixed;inset:0 0 auto;height:env(safe-area-inset-top,0px);z-index:10;background:var(--sheet)}
header{grid-column:1/3;position:sticky;top:env(safe-area-inset-top,0px);z-index:6;min-height:56px;padding-block:8px}
header .sub{display:flex;gap:12px;align-items:center}.mode{font-size:12px;color:var(--mute)}
.mode.live{color:var(--ink)}.mode.live::before{content:"";display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--ok);margin-right:6px;vertical-align:1px}
aside{border-right:1px solid var(--line);padding:16px 12px 40px;position:sticky;top:56px;height:calc(100vh - 56px);overflow:auto;background:var(--sheet)}
aside .sec{margin-bottom:12px}
aside button.grp{display:flex;justify-content:space-between;align-items:center;width:100%;border:0;background:none;cursor:pointer;color:var(--mute);
 padding:6px 12px;font:500 11px/1 var(--display);text-transform:uppercase;letter-spacing:.08em;text-align:left;border-radius:var(--r)}
aside button.grp:hover{color:var(--ink)}
aside .car::before{content:"";display:inline-block;width:5px;height:5px;border-right:1.5px solid currentColor;border-bottom:1.5px solid currentColor;transform:rotate(45deg) translate(-2px,-2px);transition:transform .15s}
aside .folded .car::before{transform:rotate(-45deg)}aside .folded .items{display:none}aside .items{padding:2px 0 0;display:flex;flex-direction:column;gap:1px}
aside button.it{position:relative;display:flex;justify-content:space-between;align-items:center;gap:8px;width:100%;text-align:left;border:0;background:none;color:var(--ink);
 padding:7px 12px 7px 24px;cursor:pointer;font:400 14px/1.3 var(--text);border-radius:var(--r);transition:background .12s}
aside button.it:hover{background:var(--hover)}aside button.it.on{background:var(--hover);font-weight:600}
aside button.it.on::before{content:"";position:absolute;left:11px;top:50%;width:6px;height:6px;margin-top:-3px;border-radius:50%;background:var(--signal)}
aside button.it .n{color:var(--mute);font:12px var(--mono);font-variant-numeric:tabular-nums}
aside button.it.sub{margin-left:24px;width:calc(100% - 24px);padding-left:12px;font-size:13px}
main{max-width:none;min-width:0;margin:0;align-self:start;width:100%;padding:32px 40px 80px}
.pg{display:flex;flex-direction:column;gap:24px;max-width:1320px;margin:0 auto}.pg>*{margin:0!important}.pg>section{display:flex;flex-direction:column;gap:16px}
.pghead{display:flex;justify-content:flex-end;align-items:center;gap:12px;flex-wrap:wrap}.pghead .bar.lead{margin-right:auto!important}body[data-mode=viewer] .pghead:not(:has(.bar>:not(.w))){display:none}
.pghead h2{font:600 28px/1.15 var(--display);letter-spacing:-.02em;margin:0}
.pg nav.tabs:has(button:only-child){display:none}.card .kpi:only-child .l{display:none}
.pghead p{margin:6px 0 0;color:var(--mute);max-width:72ch}.pghead .bar{margin:0}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,360px),1fr));gap:16px;align-items:stretch}
.card{display:flex;flex-direction:column}.card>*+*{margin-top:12px}.card>h3+*{margin-top:0}.card .row{row-gap:8px}.card table{margin-top:0}
.card>.acts{padding-top:16px}.card>.acts~.msg{margin-top:8px}
.card h3 .file{font:400 12px var(--mono);text-align:right;margin:0;color:var(--mute)}
.card h4{font:600 13px var(--display);margin:16px 0 4px}.card>.file{margin:0 0 12px}
.btn{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);border-radius:var(--r);background:var(--sheet);color:var(--ink);padding:7px 14px;cursor:pointer;
 font:500 13px/1.2 var(--text);transition:background .12s,border-color .12s;white-space:nowrap}
.btn:hover{border-color:var(--ink)}.btn.pri{background:var(--ink);border-color:var(--ink);color:var(--sheet)}.btn.pri:hover{background:color-mix(in srgb,var(--ink) 82%,var(--sheet))}
.btn:disabled{opacity:.45;cursor:default}.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.bar{margin:0 0 20px}
input.f,textarea.f,select.f{padding:8px 12px;border:1px solid var(--line);border-radius:var(--r);background:var(--sheet);color:var(--ink);font:inherit;font-size:14px;min-width:0;transition:border-color .12s,box-shadow .12s}
input.f:hover,textarea.f:hover,select.f:hover{border-color:color-mix(in srgb,var(--ink) 30%,var(--line))}
select.f{appearance:none;padding-right:32px;background-image:linear-gradient(45deg,transparent 50%,var(--mute) 50%),linear-gradient(135deg,var(--mute) 50%,transparent 50%);
 background-position:calc(100% - 17px) 52%,calc(100% - 12px) 52%;background-size:5px 5px;background-repeat:no-repeat}
.card>input.f,.card>textarea.f{display:block;width:100%;margin:6px 0 0}textarea.f{font:13px/1.55 var(--mono);min-height:64px;resize:vertical}textarea.ask0{font:15px/1.5 var(--text)}
input.f:focus,textarea.f:focus,select.f:focus{outline:none;border-color:var(--ink);box-shadow:0 0 0 3px color-mix(in srgb,var(--signal) 22%,transparent)}
label.l{display:block;font:500 12px var(--text);color:var(--mute);margin:16px 0 0}.card>label.l:first-of-type{margin-top:0}
.msg{font-size:13px;margin:8px 0;white-space:pre-wrap;color:var(--mute)}.msg:empty{display:none}.out{margin-top:16px}.out:empty{display:none}
.chip.x{cursor:pointer;background:var(--ink);border-color:var(--ink);color:var(--sheet)}.chip.x:after{content:'  \00d7'}
dialog{border:1px solid var(--line);border-radius:8px;background:var(--sheet);color:var(--ink);max-width:640px;padding:28px}
dialog h3{margin:0 0 12px;font:600 18px var(--display)}dialog .row{margin-top:20px;justify-content:flex-end}dialog pre{white-space:pre-wrap;max-height:40vh}dialog::backdrop{background:color-mix(in srgb,#000 45%,transparent);backdrop-filter:blur(2px)}
.trace{margin:12px 0 0;padding-left:20px}.trace li{margin:3px 0}.trace:empty{display:none}
.stat{font:600 36px/1 var(--display);letter-spacing:-.03em;margin:4px 0 8px;font-variant-numeric:tabular-nums}
.ed-cell{cursor:text}.ed-cell:hover{outline:1px dashed var(--mute)}td.changed{background:color-mix(in srgb,var(--signal) 12%,transparent)}.checks{display:flex;flex-wrap:wrap;gap:8px 16px}.chk{display:flex;gap:6px;align-items:center;font-size:14px}.crumb{background:none;border:0;color:inherit;font:inherit;cursor:pointer;display:flex;gap:6px;align-items:baseline}.crumb .car{font-style:normal;opacity:.6}
.kv{display:grid;grid-template-columns:auto 1fr;gap:6px 16px;font-size:13px}.kv b{font-weight:500;color:var(--mute)}
pre{margin:12px 0 0}.pg .grid{gap:16px}.pg>section>h2{display:none}
#chat{position:fixed;top:56px;right:0;bottom:0;margin:0;width:440px;max-width:100vw;background:var(--sheet);border-left:1px solid var(--line);display:flex;flex-direction:column;
 transform:translateX(100%);transition:transform .2s cubic-bezier(.2,.8,.2,1);z-index:7;padding-bottom:env(safe-area-inset-bottom,0px)}
#chat.open{transform:none}body.chat-open main{padding-right:480px}
main:has(#chat.full){padding-bottom:32px}#chat.full{position:static;width:auto;max-width:none;height:calc(100dvh - 120px);min-height:420px;transform:none;border:1px solid var(--line);border-radius:var(--r)}
#chat.full .log,#chat.full .compose,#chat.full .ctx,#chat.full .busy{width:100%;max-width:820px;margin:0 auto}#chat.full .m{font-size:15px}#chat.full .x{display:none}
#chat .ctx{padding:8px 20px 0;font-size:12px;color:var(--mute)}#chat .busy{display:flex;align-items:center;gap:10px}#chat .busy[hidden]{display:none!important}
#chat .hello{display:flex;flex-direction:column;gap:8px}#chat .hello p{margin:0 0 4px;color:var(--mute);font-size:14px}#chat .m.nav{align-self:flex-start;border:0;padding:0;font-size:12px;color:var(--mute);cursor:pointer;text-decoration:underline;text-decoration-color:var(--signal)}
#chat header{position:static;display:flex;align-items:center;gap:8px;min-height:0;padding:12px 16px 12px 20px;border-bottom:1px solid var(--line);background:none}
#chat header h3{margin:0;font:600 15px var(--display);flex:1}#chat-pick{flex:1;min-width:0}
#chat .log{flex:1;overflow:auto;padding:20px;display:flex;flex-direction:column;gap:16px}
#chat .m{font-size:14px;line-height:1.6;white-space:pre-wrap;max-width:92%}
#chat .me{align-self:flex-end;background:var(--ink);color:var(--sheet);padding:10px 14px;border-radius:12px 12px 2px 12px}#chat .ai{align-self:flex-start;padding:0;background:none}
#chat .ai code{font-family:var(--mono);background:var(--hover);padding:1px 4px;border-radius:3px}
#chat .sys{align-self:stretch;border-top:1px solid var(--line);color:var(--mute);font-size:12px;padding:8px 0 0;max-width:none}
#chat .steps{align-self:flex-start;border-left:2px solid var(--signal);color:var(--mute);font:12px/1.7 var(--mono);padding:2px 12px}
#chat .busy{color:var(--mute);font-size:12px;padding:0 20px 12px}
#chat .compose{display:flex;gap:8px;align-items:flex-end;padding:12px 16px 16px;border-top:1px solid var(--line)}#chat textarea{flex:1;min-height:46px;max-height:160px;margin:0;font:14px/1.5 var(--text)}
.vc{display:flex;gap:8px}body[data-mode=viewer] .vc{display:none}#commit{width:min(760px,calc(100vw - 32px))}
#commit .files{display:flex;flex-direction:column;gap:8px;max-height:50vh;overflow:auto}#commit summary{cursor:pointer;font-weight:600}#commit .row{margin-top:16px}
.brand{display:inline-flex;align-items:center;gap:8px;font:700 17px/1 var(--display);letter-spacing:-.02em;color:var(--ink)}.brand::before{content:"";width:10px;height:10px;background:var(--signal);border-radius:2px}
.crumb{font:500 14px var(--text);display:flex;gap:8px;align-items:baseline}.crumb i{font-style:normal;color:var(--mute)}.crumb b{font-weight:600}
header .right{margin-left:auto;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
header .right .btn{padding:6px 12px}header #chat-toggle{background:var(--signal);border-color:var(--signal);color:#fff}header #chat-toggle:hover{background:var(--signal-ink);border-color:var(--signal-ink)}
.modes{display:flex;padding:2px;background:var(--hover);border-radius:6px}.modes button{border:0;background:none;color:var(--mute);padding:5px 12px;font:500 13px var(--text);cursor:pointer;border-radius:4px}
.modes button.on{background:var(--sheet);color:var(--ink);box-shadow:0 0 0 1px var(--line)}body[data-mode=viewer] .w{display:none}
body[data-mode=viewer] .ed{background:transparent;border-color:transparent;padding-left:0}
.strip{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));border:1px solid var(--line);border-radius:var(--r);background:var(--sheet)}
.strip .cell{padding:16px 20px;border-right:1px solid var(--line);min-width:0}.strip .cell:last-child{border-right:0}
.strip .v{font:600 22px/1.15 var(--display);letter-spacing:-.02em;overflow-wrap:anywhere}.strip .v.ok{color:var(--ok)}.strip .v.bad{color:var(--red)}.strip .v.warn{color:var(--signal-ink)}
.strip .l{font-size:12px;color:var(--mute);margin-top:4px}
.home{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;align-items:stretch}.home .wide{grid-column:span 2}.home .full{grid-column:1/-1}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:24px}.kpis .kpi{border-left:2px solid var(--signal);padding-left:12px}
.list{margin:0;padding-left:18px;display:flex;flex-direction:column;gap:6px}
.links{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.lk{display:flex;gap:12px;align-items:baseline;text-align:left;border:1px solid var(--line);border-radius:var(--r);background:none;color:var(--ink);padding:10px 12px;cursor:pointer;font:inherit;transition:border-color .12s}
.lk:hover{border-color:var(--ink)}.lk b{font:600 18px var(--display);min-width:2ch;font-variant-numeric:tabular-nums}
.areas{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.areas .lk{flex-direction:column;align-items:flex-start;gap:4px;padding:16px;min-height:92px}.areas .lk b{font:600 15px var(--display)}.areas .lk span{color:var(--mute);font-size:13px}.areas .lk:hover b::after{content:' →';color:var(--signal)}
.mobile .areas{grid-template-columns:1fr 1fr}
.ask0{width:100%}.sugg{display:flex;flex-wrap:wrap;gap:8px}.sg{text-align:left;border:1px solid var(--line);border-radius:999px;background:none;color:var(--ink);padding:6px 14px;cursor:pointer;font:inherit;font-size:13px;transition:border-color .12s}.sg:hover{border-color:var(--ink)}
@media (max-width:1100px){.home{grid-template-columns:1fr}.home .wide{grid-column:auto}.strip{grid-template-columns:repeat(2,minmax(0,1fr))}.strip .cell{border-bottom:1px solid var(--line)}}
.canvas{position:relative;overflow:auto;border:1px solid var(--line);border-radius:var(--r);background:
 radial-gradient(circle,var(--faint) 1px,transparent 1.2px) 0 0/24px 24px,var(--paper)}
.canvas .links{position:absolute;inset:0;pointer-events:none;overflow:visible}.canvas .links .hit{pointer-events:stroke;stroke:transparent;stroke-width:14;fill:none;cursor:pointer}
.canvas .ln{stroke:var(--mute);stroke-width:1.5;fill:none}.canvas .ln.temp{stroke:var(--signal);stroke-dasharray:6 4}.canvas .one{fill:var(--mute)}
.canvas .links .hit:hover+.ln{stroke:var(--signal);stroke-width:2.5}
.tbl{position:absolute;width:250px;background:var(--sheet);border:1px solid var(--line);border-radius:6px;user-select:none;z-index:1;overflow:hidden}
.tbl .th{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:8px 12px;cursor:grab;font:600 13px var(--display);border-bottom:1px solid var(--line)}
.tbl.fact .th{background:var(--ink);color:var(--sheet)}.tbl .badge{border:1px solid currentColor;border-radius:999px;background:none;color:inherit;font:500 10px var(--text);padding:1px 8px;cursor:pointer;opacity:.8}
.tbl .cols{}.tbl .col{display:grid;grid-template-columns:14px 1fr auto;gap:6px;align-items:center;padding:4px 12px;font-size:12px;border-bottom:1px solid var(--line)}
.tbl .col .ty{grid-column:3;color:var(--mute);font:10px var(--mono)}.tbl .col>span:not(.dot):not(.ty){grid-column:2;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.tbl .dot{width:9px;height:9px;border:1.5px solid var(--mute);border-radius:50%;cursor:crosshair}.tbl .dot:hover{background:var(--signal);border-color:var(--signal)}
.cards>.wide{grid-column:1/-1}
.loose{display:grid;grid-template-columns:minmax(0,1fr);gap:16px}.lay>.rowhead{grid-column:1/-1;order:-1}.lay{display:grid!important;grid-template-columns:repeat(12,minmax(0,1fr))!important}.lay>*{max-width:none!important}
.card.sized{overflow:auto}.card.sized>.body{flex:1;min-height:0}.card.sized>.body svg{height:100%}
body.laying .lay>*{outline:1px dashed var(--signal);outline-offset:-1px;cursor:grab;position:relative;user-select:none}body.laying .lay>*.drag{opacity:.4}body.laying .lay>*.over{box-shadow:inset 3px 0 0 var(--signal)}
.rz{position:absolute;right:0;bottom:0;width:18px;height:18px;cursor:nwse-resize;background:linear-gradient(135deg,transparent 50%,var(--signal) 50%);z-index:2;opacity:0;transition:opacity .12s}*:hover>.rz,body.laying .rz{opacity:1}:has(>.rz){position:relative}body[data-mode=viewer] .rz{display:none}
#layout.on{background:var(--signal);color:#fff;border-color:var(--signal)}.mobile #layout{display:none}
.layers{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}.layer h4{margin:0}.layer table{margin-top:8px}
.mobile .layers{grid-template-columns:1fr}
.hits{display:flex;flex-direction:column;gap:12px;margin-top:12px}.hit{border-left:2px solid var(--signal);padding:2px 0 2px 12px;white-space:pre-wrap}.hit .file{margin:0 0 4px}
.toast{position:fixed;left:50%;bottom:calc(24px + env(safe-area-inset-bottom,0px));transform:translateX(-50%);background:var(--ink);color:var(--sheet);padding:10px 16px;border-radius:6px;font-size:13px;z-index:9;max-width:calc(100vw - 32px)}
.card.hl{outline:2px solid var(--signal);outline-offset:3px}
.cards.rows{grid-template-columns:1fr;gap:0;border:1px solid var(--line);border-radius:var(--r);background:var(--sheet);overflow:hidden}
.rows>.card{border:0;border-radius:0;border-top:1px solid var(--line)}.rows>.card:first-child,.rows>.rowhead+.card{border-top:0}
.rows>.rowcard,.rowhead{display:grid;grid-template-columns:minmax(160px,1fr) minmax(200px,1.5fr) minmax(200px,1.5fr) 200px;gap:16px;align-items:start;padding:12px 16px}
.rowhead{font-size:12px;font-weight:500;color:var(--mute);padding-block:10px;border-bottom:1px solid var(--line);background:var(--paper)}
.rows>.rowcard>h3,.rowcard .fld>label.l{display:none}.rows>.rowcard>*{margin:0;min-width:0}.rowcard .fld .f{width:100%;margin:0}.rowcard .fld .file{margin-top:4px}
.rowcard textarea.f{min-height:38px}.rowcard>.acts{padding:0;justify-content:flex-end;flex-wrap:nowrap}.rowcard>.msg{grid-column:1/-1}
.rowcard>.acts .btn{padding:6px 10px}.rowcard:not(.dirty):not(:focus-within)>.acts .btn.pri{visibility:hidden}.rowcard:hover{background:var(--hover)}
body[data-mode=viewer] .rowcard>.acts{display:none}.empty.pad{padding:16px;margin:0}
.mobile .rows>.rowcard,.mobile .rowhead{grid-template-columns:1fr}.mobile .rowhead{display:none}.mobile .rowcard .fld>label.l{display:block;margin:0 0 4px}.mobile .rowcard>.acts{justify-content:flex-start}
.cards{grid-template-columns:repeat(auto-fit,minmax(min(100%,340px),1fr))}.cards:not(.stack):not(.rows)>.card:only-child:not(.wide){max-width:880px}
.pg>p.empty,.cards>p.empty{margin:0;padding:48px 24px;text-align:center;border:1px dashed color-mix(in srgb,var(--ink) 18%,var(--line));border-radius:var(--r);background:var(--sheet);color:var(--mute)}
.file{font-family:var(--text);font-size:12.5px}.card h3 .file{flex-basis:100%;text-align:left;font:400 13px/1.4 var(--text);margin-top:-8px}
textarea.f{font:14px/1.5 var(--text)}textarea.f.code,textarea.code{font:13px/1.55 var(--mono)}
.card>select.f{display:block;width:100%;margin-top:6px}
.card .card{border:0;border-top:1px solid var(--line);border-radius:0;padding:16px 0 0;background:none}.card .card>h3{font-size:14px}
.formrow{display:grid;grid-template-columns:minmax(0,2fr) repeat(3,minmax(0,1fr));gap:16px;align-items:start}.formrow .f{width:100%}.formrow label.l{margin:0 0 6px}
.cards.stack{grid-template-columns:1fr;gap:8px}.stack>.card{flex-direction:row;flex-wrap:wrap;align-items:center;gap:8px 24px;padding:16px 20px}
.stack>.card>*{margin:0!important}.stack>.card>h3{flex:0 0 260px;margin:0;flex-direction:column;align-items:flex-start;gap:2px}.stack>.card>h3 .file{margin:0}
.stack>.card>p{flex:1;min-width:200px;color:var(--mute)}.stack>.card>.acts{padding:0;margin-left:auto!important}
.card h3 .tools{opacity:0;transition:opacity .12s}.card:hover h3 .tools,.card:focus-within h3 .tools{opacity:1}
#chat .hello .sg{align-self:flex-start}
.mobile .formrow{grid-template-columns:1fr}.mobile .stack>.card>h3{flex-basis:100%}.mobile .card h3 .tools{opacity:1}
.pgname{font:600 14px var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:default}.pgname:not(:empty)::before{content:'/';color:var(--mute);font-weight:400;margin-right:8px}
.mobile .brand{font-size:0;gap:0}.mobile .pgname::before{display:none}.mobile .crumb,.mobile .long{display:none}.mobile .pgname{min-width:0;flex:1}.mobile header{flex-wrap:nowrap}.mobile aside{z-index:20}
.grid>.card{container-type:inline-size}.grid .kpi .v{font-size:clamp(22px,16cqi,40px)}
.card label.l+select.f,.card label.l+input.f:not([type=checkbox]){display:block;width:100%;max-width:520px;margin-top:6px}.card .card{min-width:0}
.seg{gap:6px}.seg .chip{cursor:pointer;padding:5px 12px;font-size:13px;margin:0}.seg .chip.on{background:var(--ink);color:var(--sheet);border-color:var(--ink)}.seg .chip.add{border-style:dashed}
#chat .mdt{white-space:normal;width:auto;min-width:60%;margin:6px 0;font-size:13px}#chat .mdt th,#chat .mdt td{padding:6px 10px}
.cvtools{gap:6px;margin:0 0 8px}.cvtools .btn{padding:5px 10px}.cvtools .pct{font:12px var(--mono);color:var(--mute);min-width:44px;text-align:center}
.canvas .sizer{position:relative}.canvas .stage{position:absolute;left:0;top:0;transform-origin:0 0}
.node{position:absolute;width:230px;background:var(--sheet);border:1px solid var(--line);border-radius:6px;padding:10px 12px;cursor:grab;user-select:none;z-index:1}.node b{display:block;font:600 13px var(--display)}.node span{display:block;font:11.5px var(--mono);color:var(--mute);margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.node .when{font-size:12px;color:var(--signal-ink);margin-top:6px}.node.trig{background:var(--ink);border-color:var(--ink)}.node.trig b,.node.trig span{color:var(--sheet)}.node.ok{border-left:3px solid var(--ok)}.node.failed{border-left:3px solid var(--red)}.node.skipped{opacity:.6}
.stlist{display:flex;flex-direction:column;gap:6px}.strow{flex-wrap:nowrap}.strow b{flex:0 0 190px;font-weight:500;font-size:13px}.strow input{flex:1;min-width:0}.stform select.f{max-width:520px}
.mlist{max-height:240px;overflow:auto;border:1px solid var(--line);border-radius:var(--r);padding:8px 12px;margin-top:6px;max-width:520px}
button.pin{font:500 12px var(--text);border:1px solid var(--line);border-radius:999px;background:none;color:var(--mute);padding:2px 10px;cursor:pointer;margin-left:6px}button.pin:hover{border-color:var(--ink);color:var(--ink)}button.pin.on{background:var(--signal);border-color:var(--signal);color:#fff}
.cards.pins{margin-bottom:8px;align-items:start}.pinned .acts .btn{padding:5px 9px}.pinned .body{min-height:40px}
#commit input.f,#commit textarea.f{display:block;width:100%;margin-top:6px}
.card>.btn{align-self:flex-start}
#menu{display:inline-flex}body.side-off{grid-template-columns:minmax(0,1fr)}body.side-off header{grid-column:1}body.side-off aside{display:none}
.mobile .modes,.mobile .vc,.mobile #theme{display:none}.mobile #menu{display:inline-flex}
.mobile body{grid-template-columns:1fr}.mobile header{grid-column:1;gap:8px;padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px))}
.mobile .crumb i,.mobile .crumb>:not(b){display:none}.mobile .crumb{font-size:14px}
.mobile aside{position:fixed;top:56px;left:0;bottom:0;width:min(300px,86vw);z-index:8;transform:translateX(-102%);transition:transform .2s cubic-bezier(.2,.8,.2,1);display:block;border-right:1px solid var(--line);border-bottom:0;padding:16px 12px 40px;height:auto}
.mobile body.menu-open aside{transform:none}.mobile aside .sec{display:block}.mobile aside button.grp{display:flex}.mobile aside .items{display:flex}.mobile aside .folded .items{display:none}
.mobile aside button.it{width:100%;padding:10px 12px 10px 24px}
.mobile main{padding:20px max(16px,env(safe-area-inset-right,0px)) 56px max(16px,env(safe-area-inset-left,0px))}
.mobile .grid{grid-template-columns:1fr}.mobile .grid>.card{grid-column:1/-1!important}.mobile .cards,.mobile .home{grid-template-columns:1fr}.mobile .home .wide{grid-column:auto}
.mobile .strip{grid-template-columns:repeat(2,minmax(0,1fr))}.mobile .pghead h2{font-size:24px}.mobile .kpi .v{font-size:30px}.mobile .card{padding:16px}
.mobile #chat{top:0;width:100vw;z-index:11}.mobile body.chat-open main{padding-right:16px}.mobile .canvas{max-height:70vh}
.mobile .card h3 .tools{display:none}.mobile nav.tabs{width:100%;overflow-x:auto;flex-wrap:nowrap}
select.f{max-width:100%;min-width:0}.card h3{flex-wrap:wrap}.file,.kv span{overflow-wrap:anywhere;min-width:0}.kv{grid-template-columns:auto minmax(0,1fr)}
#chat.full{box-sizing:border-box;max-width:100%}#chat:not(.open):not(.full){visibility:hidden}
@media (max-width:820px){.formrow{grid-template-columns:1fr}.stack>.card>h3{flex-basis:100%}
 .rows>.rowcard,.rowhead{grid-template-columns:1fr}.rowhead{display:none}.rowcard .fld>label.l{display:block;margin:0 0 4px}
 html,body{overflow-x:clip}.card table{display:block;overflow-x:auto;max-width:100%}body{grid-template-columns:1fr}header{grid-column:1}
 html:not(.mobile) aside{position:static;height:auto;border-right:0;border-bottom:1px solid var(--line);display:flex;flex-wrap:wrap;gap:4px;padding:12px max(16px,env(safe-area-inset-left,0px))}
 html:not(.mobile) aside .sec{display:contents}html:not(.mobile) aside button.grp{display:none}html:not(.mobile) aside .folded .items,html:not(.mobile) aside .items{display:contents}html:not(.mobile) aside button.it,html:not(.mobile) aside button.it.sub{width:auto;padding:6px 12px;margin:0;border:1px solid var(--line);border-radius:999px}
 html:not(.mobile) aside button.it.on::before{display:none}html:not(.mobile) aside button.it.on{background:var(--ink);color:var(--sheet);border-color:var(--ink)}
 main{padding:20px max(16px,env(safe-area-inset-right,0px)) 48px max(16px,env(safe-area-inset-left,0px))}body.chat-open main{padding-right:16px}
 .cards{grid-template-columns:1fr}.pghead h2{font-size:24px}#chat{top:0}}
"""

JS = r"""
const D=window.APP, KEY='__ABI_APP_KEY__';
const MOBILE=matchMedia('(max-width: 820px), (pointer: coarse) and (max-width: 1100px)').matches;
let CID='';try{CID=localStorage.getItem('abi-client')||'';if(!CID){CID=Math.random().toString(36).slice(2,12);localStorage.setItem('abi-client',CID)}}catch(e){CID=Math.random().toString(36).slice(2,12)}
{const f0=window.fetch.bind(window);window.fetch=(u,o={})=>{if(typeof u==='string'&&u.startsWith('/')){o.headers={...(o.headers||{}),'X-Abi-Client':CID,'X-Abi-Mobile':MOBILE?'1':'0','X-Abi-Project':D.project}}
 return f0(u,o).then(r=>{if(r.status===403)r.clone().json().then(b=>{if(b.error==='forbidden')stale()}).catch(()=>{});return r})}}
// the app restarted (new page key) or the session ended: say so once, instead of 'forbidden' everywhere
function stale(){if(window.__stale)return;window.__stale=1;const b=document.createElement('div');b.className='toast';b.style.cssText='position:fixed;top:64px;bottom:auto;left:50%;right:auto;height:auto;width:max-content;max-width:calc(100vw - 32px);transform:translateX(-50%);z-index:30;display:flex;gap:12px;align-items:center';b.textContent='The app restarted or your session ended. ';const a=document.createElement('button');a.className='btn pri';a.textContent='Reload';a.onclick=()=>location.reload();b.append(a);document.body.append(b)}
if(MOBILE)document.documentElement.classList.add('mobile');
const $=(s,r=document)=>r.querySelector(s), h=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!==undefined)e.textContent=x;return e};
const wait=ms=>new Promise(z=>setTimeout(z,ms));
async function api(action,args){
 
 const post=(u,b)=>fetch(u,{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify(b||{})}).then(r=>r.json());
 let r=await post('/api/'+action,{project:D.project,...args});
 if(r.confirm){if(!await ask(r.confirm))throw new Error('You declined. Nothing changed.');r=await post('/confirm/'+r.confirm.id)}
 if(r.error)throw new Error(r.error);return r.result}
function ask(c){return new Promise(res=>{const d=$('#confirm');$('#confirm h3').textContent=c.title||(c.id?'ABI asks for your confirmation':'Confirm');$('#c-msg').textContent=c.message;$('#c-prev').textContent=c.preview||'';$('#c-prev').hidden=!c.preview;
 d.showModal();$('#c-yes').onclick=()=>{d.close();res(true)};$('#c-no').onclick=()=>{d.close();res(false)}})}
function say(box,text,bad){box.textContent=text;box.className='msg '+(bad?'bad':'ok')}
function card(title,sub){const c=h('div','card'),t=h('h3');t.append(h('span','',title));if(sub&&/\s/.test(sub))t.title=sub;else if(sub)t.append(h('span','file',sub));c.append(t);return c}
// a list of editable items as aligned rows under one header (rows mode); a field is its label (shown on phones) and input
function fld(label,el,note){const d=h('div','fld');d.append(h('label','l',label),el);if(note)d.append(h('div','file',note));return d}
function rowcard(c,...fields){c.classList.add('rowcard');c.append(...fields);c.oninput=()=>c.classList.add('dirty');return c}
function rowhead(...cols){const r=h('div','rowhead');cols.forEach(t=>r.append(h('span','',t)));return r}
const W=/^(Save|Create|Delete|Edit report|New report|Reload |Run now|Enable|Disable|Train|Score|New |Ask$|Add |Remove|Edit|Publish|Save steps|Upload)/;
function btn(label,fn,pri){const b=h('button','btn'+(pri?' pri':'')+(W.test(label)?' w':''),label);b.onclick=async()=>{b.disabled=true;try{await fn()}finally{b.disabled=false}};return b}
const cell=v=>v!==null&&typeof v==='object'?JSON.stringify(v):typeof v==='string'&&/^\d{4}-\d\d-\d\dT\d\d:\d\d/.test(v)?new Date(v).toLocaleString():v;
function rows(box,d){box.innerHTML='';box.classList.add('out');if(!(d&&d.columns&&Array.isArray(d.rows))){box.append(h('pre','',JSON.stringify(d,null,1)));return}
 const rs=d.rows.map(r=>(Array.isArray(r)?r:d.columns.map(c=>r[c])).map(cell));table(box,{measures:d.measures||{},format:d.format||{},columns:d.columns,rows:rs})}
// an admin action ABI wants confirmed: the person's own click in ABI's dialog, then the server runs it (null: they said no)
async function confirmed(x){if(x.confirm){if(!await ask(x.confirm))return null;x=await fetch('/confirm/'+x.confirm.id,{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:'{}'}).then(y=>y.json())}if(x.error)throw new Error(x.error);return x}
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
// ---------- a person's own Home: pins from any page (pins.json, per person, in the app's Git) ----------
let PINS=null;const pinKey=p=>JSON.stringify([p.kind,p.ref]);
const pinPost=b=>fetch('/pins',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify(b||{})}).then(r=>r.json());
async function pinsLoad(){if(PINS)return PINS;try{PINS=(await pinPost()).result||[]}catch(e){PINS=[]}return PINS}
function pinBtn(pin){const b=h('button','pin','Pin');b.title='Pin to Home';const set=()=>{const on=(PINS||[]).some(x=>pinKey(x)===pinKey(pin));b.textContent=on?'Pinned':'Pin';b.classList.toggle('on',on)};
 pinsLoad().then(set);b.onclick=async e=>{e.stopPropagation();await pinsLoad();const k=pinKey(pin),on=PINS.some(x=>pinKey(x)===k);PINS=on?PINS.filter(x=>pinKey(x)!==k):[...PINS,pin];set();
  try{await pinPost({pins:PINS});toast(on?'Unpinned from Home.':'Pinned to Home.')}catch(x){toast('Could not save the pin.')}};return b}
async function pinCard(p,c){const body=h('div','body'),go=h('button','btn','Open'),un=h('button','btn','Unpin');c.append(body);const tools=h('div','row acts');tools.append(go,un);c.append(tools);
 // arrange: move left or right, wide or narrow (saved with the pins)
 const mv=async d=>{const i=PINS.findIndex(x=>pinKey(x)===pinKey(p)),j=i+d;if(i<0||j<0||j>=PINS.length)return;[PINS[i],PINS[j]]=[PINS[j],PINS[i]];await pinPost({pins:PINS});show('home')};
 const l=h('button','btn','←'),r2=h('button','btn','→'),wd=h('button','btn',p.wide?'Narrow':'Wide');l.title='Move left';r2.title='Move right';l.onclick=()=>mv(-1);r2.onclick=()=>mv(1);
 wd.onclick=async()=>{const x=PINS.find(x=>pinKey(x)===pinKey(p));x.wide=!x.wide;await pinPost({pins:PINS});show('home')};tools.append(l,r2,wd);
 un.onclick=async()=>{PINS=PINS.filter(x=>pinKey(x)!==pinKey(p));await pinPost({pins:PINS});c.remove()};
 const r=p.ref,line=(t,cls)=>body.append(h('p',cls||'',t));
 try{if(p.kind==='visual'){go.onclick=()=>show('rep:'+r.report);const d=await api('visual_data',{report:r.report,page:r.page,visual:r.visual});requestAnimationFrame(()=>chart(body,d))}
  else if(p.kind==='analysis'){go.onclick=()=>{PENDING.explore={...r.q,chart:r.type};show('explore')};const d=await api('semantic_query',{limit:500,...r.q});d.format=d.format||{};d.type=r.type||'bar';d.id='pin';requestAnimationFrame(()=>chart(body,d))}
  else if(p.kind==='monitor'){go.onclick=()=>show('monitor');const ck=(D.app?.monitor?.checks||[]).find(x=>x.id===r);if(!ck){line('This check was removed.','empty');return}const x=await monEval(ck);line(x.text,x.ok?'ok':'bad')}
  else if(p.kind==='flow'){go.onclick=()=>show('flw:'+r);const f=(D.flows||[]).find(x=>x.id===r);const x=await api('flow_runs',{flow:r}).catch(()=>({}));const one=x.runs?.[0]||x;
   line(`${f?.enabled?'On':'Off'} · last run: ${one.status||'never'}`,one.status==='failed'?'bad':one.status==='ok'?'ok':'')}
  else if(p.kind==='status'){go.onclick=()=>show('monitor');const q=D.quality||{},ix=k=>(q.columns||[]).indexOf(k),rs=q.rows||[];
   if(r==='freshness'){const bad=rs.filter(x=>(x[ix('problems')]||[]).length||x[ix('missing_periods')]);line(bad.length?`${bad.length} of ${rs.length} tables have problems`:`All ${rs.length} tables without problems`,bad.length?'bad':'ok')}
   else if(r==='model'){const rd=D.readiness||{};line(rd.ready?'Ready':'Not ready',rd.ready?'ok':'bad');(rd.missing||[]).slice(0,3).forEach(m=>line(typeof m==='string'?m:JSON.stringify(m),'file'))}
   else if(r==='ml'){const ok=D.ml.filter(m=>m.latest_score?.status==='ok').length;line(`${ok} of ${D.ml.length} ML models scored`,ok<D.ml.length?'bad':'ok')}
   else{const on=D.flows.filter(f=>f.enabled).length;line(`${on} of ${D.flows.length} flows on`)}}
  else if(p.kind==='table'){go.onclick=()=>show('tables');const q=D.quality||{},ix=c=>(q.columns||[]).indexOf(c),row=(q.rows||[]).find(x=>x[0]===r);
   line(row?`${fmt(row[ix('rows')])} rows · last load ${String(row[ix('last_load')]||'never').slice(0,16).replace('T',' ')}`:'No data-quality row yet.',row&&(row[ix('problems')]||[]).length?'bad':'')}
  else{const page={agent:'agents',tool:'tools',ml:'ml'}[p.kind]||'home',o=(D[{agent:'agents',tool:'tools',ml:'ml'}[p.kind]]||[]).find(x=>x.id===r);go.onclick=()=>show(page);
   line(o?(o.description||o.status||o.name||r):'It was removed.',o?'':'empty')}}catch(e){line(e.message,'bad')}}
// + Add widget: everything that can be pinned, in one picker (the same pins as the Pin buttons on each page)
function pinChoices(){const vis=D.reports.flatMap(r=>r.pages.flatMap(p=>p.visuals.filter(v=>v.text===undefined).map(v=>({kind:'visual',ref:{report:r.id,page:p.id,visual:v.id},title:v.title||v.id,from:r.name||r.id}))));
 const one=(kind,list,from)=>(list||[]).map(x=>({kind,ref:x.id,title:x.name||x.id,from}));
 return [['Report charts',vis],['Your analyses',MINE.map(a=>({kind:'analysis',ref:{q:a.q,type:a.type},title:a.name}))],['Monitors',[...one('monitor',D.app?.monitor?.checks,'your check'),
  ...[['freshness','Data freshness'],['model','Model readiness'],['ml','ML models'],['flows','Flows']].map(([ref,title])=>({kind:'status',ref,title,from:'Monitor'}))]],
  ['Tables',(D.tables||[]).map(t=>({kind:'table',ref:t,title:t}))],['Flows',one('flow',D.flows)],['ML models',one('ml',D.ml)],['Tools',one('tool',D.tools)],['Agents',one('agent',D.agents)]].filter(([,l])=>l.length)}
async function addPin(box){if(box.querySelector('.addpin')){box.querySelector('.addpin').remove();return}await pinsLoad();const c=card('Add to your Home','pick what to watch here; it stays live');c.classList.add('addpin','wide');
 const grp=h('select','f'),it=h('select','f'),ch=pinChoices();ch.forEach(([g],i)=>grp.append(new Option(g,i)));
 const fill=()=>{it.innerHTML='';ch[grp.value][1].forEach((x,i)=>{const o=new Option((x.from?x.from+' · ':'')+x.title+(PINS.some(y=>pinKey(y)===pinKey(x))?'  (on Home)':''),i);it.append(o)})};grp.onchange=fill;fill();
 const row=h('div','row');row.append(grp,it);c.append(row);act(c,'Pin to Home',async()=>{const x=ch[grp.value][1][it.value];if(!x)return;if(PINS.some(y=>pinKey(y)===pinKey(x))){toast('Already on Home.');return}
  PINS=[...PINS,{kind:x.kind,ref:x.ref,title:x.title}];await pinPost({pins:PINS});show('home')},1);act(c,'Cancel',()=>c.remove());(box.querySelector('.pghead')||box).after(c)}
async function pinsHome(box){const sec=h('div','cards pins');box.append(sec);{const bar=h('div','row bar top');bar.append(btn('+ Add widget',()=>addPin(box),1));box.prepend(bar)}const ps=await pinsLoad();
 if(!ps.length){const c=card('Your Home','pin what you check often');c.classList.add('wide');c.append(h('p','',"Press + Add widget, or Pin on a report chart, a saved analysis, a monitor, a table, a flow, an agent or a tool: it shows here, live. Open goes to its page."));sec.append(c);return}
 ps.forEach(p=>{const c=card(p.title||p.ref);c.classList.add('pinned');if(p.wide)c.classList.add('wide');sec.append(c);pinCard(p,c)})}
function home(box){pinsHome(box);const rd=D.readiness||{},q=D.quality||{},ix=c=>(q.columns||[]).indexOf(c),rowsQ=q.rows||[];
 const last=rowsQ.map(r=>r[ix('last_load')]).filter(Boolean).sort().pop(),problems=rowsQ.flatMap(r=>(r[ix('problems')]||[]).map(p=>`${r[ix('table')]}: ${p}`));
 const g=h('div','home');box.append(g);
 // what needs attention
 const at=card('Needs attention');const al=h('ul','list');[...(rd.missing||[]).map(x=>typeof x==='string'?x:JSON.stringify(x)),...problems].forEach(t=>al.append(h('li','bad',t)));
 if(MODE==='developer')team(at,al);
 if(!al.children.length)al.append(h('li','ok','Nothing: the model is ready and the data is fresh.'));at.append(al);
 // one summary line; the details open on demand (the team's steps arrive later, the line follows them)
 const sum=h('p'),tog=h('button','btn','Show details');al.hidden=true;al.before(sum);al.after(tog);
 const upd=()=>{const it=[...al.children],wait=it.filter(x=>/^Waiting for your yes/.test(x.textContent)).length,bad=it.filter(x=>x.classList.contains('bad')).length-wait;
  sum.className=bad||wait?'bad':'ok';sum.textContent=bad||wait?[bad&&`${bad} problem${bad>1?'s':''} in the data or model`,wait&&`${wait} step${wait>1?'s':''} wait for your yes`].filter(Boolean).join(' · '):'Nothing: the model is ready and the data is fresh.';tog.hidden=!(bad||wait)};
 tog.onclick=()=>{al.hidden=!al.hidden;tog.textContent=al.hidden?'Show details':'Hide details'};upd();new MutationObserver(upd).observe(al,{childList:true});
 act(at,'Check again',async()=>{D.quality=await api('data_quality',{});D.readiness=await api('readiness',{});show('home')});g.append(at);
 // the map: every sidebar group with its pages and one line of status, in pipeline order
 const mlok=D.ml.filter(m=>m.latest_score?.status==='ok').length,on=D.flows.filter(f=>f.enabled).length,lastC=(D.history||[])[0];
 const line={Data:problems.length?`${problems.length} data problems`:`fresh · last load ${last?cell(last):'never'}`,'Data Model':rd.ready?'Ready':'Not ready',
  Visualizations:`${D.reports.length} reports · ${MINE.length} of your analyses`,Automate:`${mlok} of ${D.ml.length} ML models scored · ${on} of ${D.flows.length} flows on`,
  Knowledge:`${(VAULT||[]).length} vault documents`,Govern:`${D.pending?.files?.length||0} uncommitted · ${D.published?.at?'published '+cell(D.published.at):'not published'}${lastC?' · last: '+(lastC.message.length>70?lastC.message.slice(0,70)+'…':lastC.message):''}`};
 const map=card('Where things stand','one line per area; open it to go there'),tb=h('div','areas');
 line['Data Model']=(rd.ready?'Ready':'Not ready')+` · ${D.measures.length} measures`;
nav().filter(([gname])=>gname!=='Start'&&line[gname]).forEach(([gname,items])=>{const b=h('button','lk');b.append(h('b','',gname),h('span','',line[gname]||''));b.onclick=()=>show(items[0][0]);tb.append(b)});
 map.append(tb);at.classList.add('full');map.classList.add('full');g.append(map)}

// ---------- Analyze: one report per entry; filters and cross-filter ----------
function report(box,rep){let flt=PENDING.filters||[];PENDING.filters=null;
 {const tb=h('div','row bar top');let ed=null;tb.append(btn('Edit report',()=>{if(ed){ed.remove();ed=null;return}ed=editor('report',rep.spec||{id:rep.id,name:rep.name},false);box.querySelector('.pghead').after(ed)}),
  btn('New report',()=>{const c=editor('report',KIND.report.tpl,true);box.querySelector('.pghead').after(c)}),
  btn('Delete report',async()=>{try{await KIND.report.del({id:rep.id});toast('Deleted.')}catch(e){toast(e.message)}}));box.append(tb)}
 {const tb=box.querySelector(':scope>.bar.top');let ex=null;const eb=h('button','btn pri','Export');eb.onclick=()=>{if(ex){ex.remove();ex=null;return}ex=exportCard(rep,()=>flt,()=>cur);box.querySelector('.pghead').after(ex)};tb.prepend(eb)}const top=h('div','row bar lead'),col=h('select','f');col.append(new Option('filter by…',''));D.dims.forEach(c=>col.append(new Option(c,c)));
 const val=h('select','f');val.multiple=true;val.hidden=true;const add=h('button','btn','add filter');add.hidden=true;const chips=h('span');
 top.append(col,val,add,chips);const tabs=h('nav','tabs'),grid=h('div','grid'),msg=h('div','msg');box.append(top,msg,tabs,grid);
 col.onchange=async()=>{val.innerHTML='';if(!col.value){val.hidden=add.hidden=true;return}
  try{const r=await api('semantic_query',{measures:[D.measures[0]?.id],dimensions:[col.value],order_by:[{field:col.value}],limit:500});
   r.rows.forEach(x=>val.append(new Option(String(x[0]),JSON.stringify(x[0]))));val.size=Math.min(8,r.rows.length);val.hidden=add.hidden=false}catch(e){say(msg,e.message,1)}};
 add.onclick=()=>{const v=[...val.selectedOptions].map(o=>JSON.parse(o.value));if(v.length){flt=flt.filter(f=>f.column!==col.value);flt.push({column:col.value,op:'in',value:v});draw()}};
 const cross=(dim,value)=>{flt=flt.filter(f=>f.column!==dim);flt.push({column:dim,op:'=',value});draw()};
 let cur=rep.pages[0];
 async function draw(){chips.innerHTML='';msg.textContent='';
  flt.forEach(f=>{const c=h('span','chip x',`${f.column} ${f.op} ${Array.isArray(f.value)?f.value.join(', '):f.value}`);c.onclick=()=>{flt=flt.filter(x=>x!==f);draw()};chips.append(c)});
  for(const p of rep.pages)for(const v of p.visuals){delete v.live;if(flt.length&&v.text===undefined)
   try{v.live=await api('visual_data',{report:rep.id,page:p.id,visual:v.id,interaction:flt});v.live.id=v.id}catch(e){v.live={error:e.message}}}
  page(cur)}
 function page(p){cur=p;grid.dataset.page=p.id;[...tabs.children].forEach(b=>b.classList.toggle('on',b.dataset.id===p.id));grid.innerHTML='';
  p.visuals.slice().sort((a,b)=>(a.layout.y-b.layout.y)||(a.layout.x-b.layout.x)).forEach(v=>{const c=h('div','card');c.style.gridColumn=`span ${Math.min(12,v.layout.w||6)}`;c.dataset.lk=v.id;c.dataset.w=Math.min(12,v.layout.w||6);if(v.layout.h)c.dataset.h=v.layout.h*70-10;
   c.innerHTML=`<h3><span></span><span class="tools"><button data-a="t">table</button><button data-a="c">csv</button><button data-a="h">how</button></span></h3><div class="body"></div><pre class="how" hidden></pre>`;
   $('h3 span',c).textContent=v.title||v.id;if(v.text===undefined)$('h3',c).append(pinBtn({kind:'visual',ref:{report:rep.id,page:p.id,visual:v.id},title:v.title||v.id}));grid.append(c);const body=$('.body',c),d=v.live||v.data;
   if(v.text!==undefined){$('.tools',c).remove();body.textContent=v.text;body.style.whiteSpace='pre-wrap';return}
   if(!d||d.error||v.error){body.append(h('div','bad',d?.error||v.error||'no data'));return}
   if(flt.length&&(!d.rows.length||d.rows.every(r=>r.every(x=>x===null||typeof x!=='number')))){body.append(h('p','empty','No rows for these filters: they fall outside what this visual covers (the report\'s period or population). Remove a filter to see it again.'));return}
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
// Edits: ABI's write-back on the gold tables (edit_cells; history, undo, proposals, input tables)
function editsPage(box){const T=D.model?.tables||{},keyed=Object.keys(T).filter(t=>(T[t].key||[]).length&&!t.startsWith('ml_')&&t!==(D.model?.date_table?.table||'calendar')).sort();  // ABI makes the date table: nothing to edit
 const top=h('div','row'),sel=h('select','f');keyed.forEach(t=>sel.append(new Option(`${t} (${T[t].kind||''})`,t)));top.append(h('label','l','Table'),sel);box.append(top);
 const grid=card('Rows','the edited table, as every query sees it'),hist=card('Edit history','who changed what; undo a batch'),prop=card('Waiting for approval','edits the agent proposed'),g=h('div','cards');
 grid.style.gridColumn=hist.style.gridColumn='1/-1';g.append(grid,prop,hist);box.append(g);let off=0,staged=[];
 const msg=h('div','msg'),body=h('div','body'),pager=h('div','row'),save=h('div','row');grid.append(pager,body,save,msg);
 async function rowsPage(){body.innerHTML='';pager.innerHTML='';save.innerHTML='';staged=[];const t=sel.value,key=T[t]?.key||[];
  const r=await fetch('/edits/rows',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify({table:t,offset:off,limit:50})}).then(x=>x.json());
  if(r.error){say(msg,r.error,1);return}const d=r.result,cols=d.columns.map(c=>c.name),tb=h('table');tb.insertRow().append(...cols.map(c=>h('th','',c+(key.includes(c)?' (key)':''))));
  d.rows.forEach(row=>{const tr=tb.insertRow();row.forEach((v,i)=>{const td=tr.insertCell();td.textContent=v===null?'—':v;
   if(MODE==='developer'&&!key.includes(cols[i])){td.className='ed-cell';td.title='click to edit';td.onclick=()=>{if(td.querySelector('input'))return;const inp=h('input','f');inp.value=v??'';td.textContent='';td.append(inp);inp.focus();
    const done=()=>{const nv=inp.value;td.textContent=nv||'—';if((nv||null)!==(v===null?null:String(v))){staged=staged.filter(x=>!(x.column===cols[i]&&JSON.stringify(x.key)===JSON.stringify(key.map(k=>row[cols.indexOf(k)]))));
     staged.push({key:key.map(k=>row[cols.indexOf(k)]),column:cols[i],value:nv===''?null:nv,old:v});td.classList.add('changed');sv()}};inp.onblur=done;inp.onkeydown=e=>{if(e.key==='Enter')inp.blur()}}}})});
  if(MODE==='developer'){const tr=tb.insertRow(),ins=cols.map(c=>{const i=h('input','f');i.placeholder=c;tr.insertCell().append(i);return i});
   tr.insertCell().append(btn('Add row',async()=>{const row=Object.fromEntries(cols.map((c,i)=>[c,ins[i].value===''?null:ins[i].value]));
    try{await api('edit_cells',{table:sel.value,add:[row],comment:'row added in the app'});say(msg,'Row added through ABI.');await rowsPage();await history()}catch(e){say(msg,e.message,1)}}))}
  const sc=h('div','scroll');sc.append(tb);body.append(sc);pager.append(h('span','file',`rows ${off+1}–${off+d.rows.length} of ${d.total}`),
   btn('Previous',async()=>{off=Math.max(0,off-50);await rowsPage()}),btn('Next',async()=>{if(off+50<d.total){off+=50;await rowsPage()}}))}
 function sv(){save.innerHTML='';if(!staged.length)return;const cm=h('input','f');cm.placeholder='why (kept in the history)';
  save.append(h('span','',`${staged.length} change${staged.length>1?'s':''} not saved`),cm,btn('Save edits',async()=>{try{await api('edit_cells',{table:sel.value,edits:staged,comment:cm.value||null});say(msg,'Saved through ABI. Every query now sees the edit.');await rowsPage();await history()}catch(e){say(msg,e.message,1)}}))}
 async function history(){hist.querySelectorAll(':scope>:not(h3)').forEach(x=>x.remove());let r;try{r=await api('edit_history',{table:sel.value,limit:50})}catch(e){hist.append(h('p','bad',e.message));return}
  if(!r.rows.length){hist.append(h('p','empty','No edits on this table yet.'));return}const ix=c=>r.columns.indexOf(c),tb=h('table');tb.insertRow().append(...['when','who','row','column','old → new','status','flag',''].map(x=>h('th','',x)));
  const seen=new Set();r.rows.forEach(x=>{const tr=tb.insertRow();[cell(x[ix('timestamp')]),x[ix('user')],x[ix('row_key')],x[ix('column')]||'(row)',`${x[ix('old_value')]??'—'} → ${x[ix('new_value')]??'—'}`,x[ix('status')],x[ix('flag')]||''].forEach(v=>tr.insertCell().textContent=v??'');
   const b=x[ix('batch_id')],c=tr.insertCell();if(MODE==='developer'&&x[ix('status')]==='active'&&!seen.has(b)){seen.add(b);c.append(btn('Undo batch',async()=>{try{await api('undo_edits',{table:sel.value,batch:b});await rowsPage();await history()}catch(e){toast(e.message)}}))}});
  const sc=h('div','scroll');sc.append(tb);hist.append(sc)}
 async function proposals(){prop.querySelectorAll(':scope>:not(h3)').forEach(x=>x.remove());let r;try{r=await api('edit_proposals',{})}catch(e){prop.append(h('p','bad',e.message));return}
  const list=r.proposals||[];if(!list.length){prop.append(h('p','empty','Nothing waiting. Edits the agent suggests wait here for a person.'));return}
  list.forEach(p=>{const c=h('div','hit');c.append(h('div','',`${p.table} · ${p.changes?.length||p.rows?.length||''} change(s) · ${p.user||''} · ${p.comment||''}`));
   if(MODE==='developer')c.append(btn('Approve',async()=>{try{await api('approve_edit',{table:p.table,batch:p.batch});await proposals();await rowsPage()}catch(e){toast(e.message)}},1),btn('Reject',async()=>{try{await api('reject_edit',{table:p.table,batch:p.batch});await proposals()}catch(e){toast(e.message)}}));prop.append(c)})}
 if(MODE==='developer'){const it=card('New input table','rows entered by people only, e.g. targets or a budget'),nm=h('input','f ed'),cs=h('input','f ed'),ky=h('input','f ed');
  nm.placeholder='targets_2026';cs.placeholder='channel_id:VARCHAR, month:DATE, target_revenue:DOUBLE';ky.placeholder='channel_id, month';it.append(h('label','l','Name'),nm,h('label','l','Columns (name:TYPE, …)'),cs,h('label','l','Key'),ky);
  act(it,'Create',async()=>{const columns=cs.value.split(',').map(x=>x.trim()).filter(Boolean).map(x=>{const[n,t]=x.split(':').map(y=>y.trim());return{name:n,type:t||'VARCHAR'}});
   await api('create_input_table',{table:nm.value.trim(),columns,key:ky.value.split(',').map(x=>x.trim()).filter(Boolean)});return 'Created through ABI. Pick it above and add its rows.'});g.append(it)}
 sel.onchange=()=>{off=0;rowsPage();history()};if(keyed.length){rowsPage();history();proposals()}else box.append(h('p','empty','No keyed tables: a table needs a key to be edited.'))}
// ---------- Export ----------
const dl=(blob,name)=>{const a=h('a');a.href=URL.createObjectURL(blob);a.download=name;a.click()};
async function fileOf(url,body){const r=await fetch(url,{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify(body)});if(!r.ok)throw new Error((await r.json()).error);return r.blob()}
function repPicker(box,onPick,filter){const sel=h('select','f'),list=D.reports.filter(filter||(()=>true));list.forEach(r=>sel.append(new Option(r.name||r.id,r.id)));
 const slot=h('div');box.append(h('label','l','Report'),sel,slot);const go=()=>{slot.innerHTML='';const r=D.reports.find(x=>x.id===sel.value);if(r)onPick(slot,r)};sel.onchange=go;go();
 if(!list.length)box.append(h('p','empty','No reports here yet.'))}
function documentsPage(box){const g=h('div','cards'),c=card('Document','rendered by ABI, as you');repPicker(c,(slot,r)=>slot.append(exportCard(r,()=>[],()=>r.pages[0])));g.append(c);box.append(g)}
function onePagerPage(box){const g=h('div','cards'),c=card('Make a one-pager','the board-pack agent builds it as an ABI report');g.append(c);box.append(g);
 if(MODE==='developer'){const ta=h('textarea','f ed');ta.rows=3;ta.value='A one-pager for the board: partner revenue, views, RPM and net subscribers for the last complete months, the trend by month, the top channels, and the caveats.';
  c.append(ta);act(c,'Ask the agent',async()=>{openChat(true);inp.value='Use the board-pack specialist to build a one-pager report in ABI: '+ta.value;sendChat();return 'Asked: follow it in the chat.'},1)}
 else c.append(h('p','empty','Switch to Developer to have one made.'));
 const ex=card('Export a one-pager','reports with a single page');repPicker(ex,(slot,r)=>slot.append(exportCard(r,()=>[],()=>r.pages[0])),r=>r.pages.length===1);g.append(ex)}
function dataExportPage(box){const g=h('div','cards'),c=card('A view\'s rows','ABI\'s export_visual: the same rows the view shows');g.append(c);box.append(g);
 repPicker(c,(slot,r)=>{const pg=h('select','f'),vs=h('select','f'),fm=h('select','f');r.pages.forEach(p=>pg.append(new Option(p.name||p.id,p.id)));['csv','xlsx'].forEach(f=>fm.append(new Option(f.toUpperCase(),f)));
  const fill=()=>{vs.innerHTML='';r.pages.find(p=>p.id===pg.value).visuals.filter(v=>v.text===undefined).forEach(v=>vs.append(new Option(v.title||v.id,v.id)))};pg.onchange=fill;fill();
  slot.append(h('label','l','Page'),pg,h('label','l','View'),vs,h('label','l','Format'),fm);
  act(slot,'Download',async()=>{dl(await fileOf('/data/export',{report:r.id,page:pg.value,visual:vs.value,format:fm.value}),`${r.id}-${vs.value}.${fm.value}`);return 'Downloaded.'},1)});
 const q=card('A query\'s rows','every row the query returns, as CSV'),qs=h('select','f');D.datasets.forEach(d=>qs.append(new Option(d.name||d.id,d.id)));q.append(h('label','l','Query'),qs);
 act(q,'Download CSV',async()=>{const d=await api('dataset_data',{dataset:qs.value});d.id=qs.value;csv(d);return 'Downloaded.'},1);g.append(q)}
function videoPage(box){const c=card('Explainer video','silent, one slide per view, captions from the rows; rendered on this machine');
 repPicker(c,(slot,r)=>{const pg=h('div','checks'),th=h('select','f'),sz=h('select','f'),sec=h('input','f');
  r.pages.forEach((p,i)=>{const l=h('label','chk'),x=h('input');x.type='checkbox';x.value=p.id;x.checked=i===0;l.append(x,h('span','',p.name||p.id));pg.append(l)});
  [['paper','Light'],['ink','Dark']].forEach(([v,t])=>th.append(new Option(t,v)));th.value=document.documentElement.dataset.theme==='dark'?'ink':'paper';
  [['1920x1080','1080p · 16:9'],['1280x720','720p · 16:9'],['1080x1920','phone · 9:16']].forEach(([v,t])=>sz.append(new Option(t,v)));sec.type='number';sec.min=2;sec.max=15;sec.value=6;
  slot.append(h('label','l','Pages'),pg,h('label','l','Theme'),th,h('label','l','Size'),sz,h('label','l','Seconds per slide'),sec);
  act(slot,'Render and download',async m=>{const pages=[...pg.querySelectorAll('input:checked')].map(x=>x.value);if(!pages.length)return 'Pick at least one page.';say(m,'Rendering… about 5 seconds per slide.');
   dl(await fileOf('/video/export',{report:r.id,pages,theme:th.value,size:sz.value,seconds:+sec.value}),`${r.id}.mp4`);return 'Downloaded.'},1)});box.append(c)}
function boardPage(box){const g=h('div','cards');box.append(g);const P=D.app?.packs||[];
 if(MODE==='developer'){const c=card('Prepare a board pack','the board-pack agent picks, checks, writes and renders; you send'),ta=h('textarea','f ed');ta.rows=4;
  ta.value='For the board review: the executive overview and monetization for the last complete months, a one-pager, the channel table as Excel, and a short explainer video. Commentary on what moved and what is open.';
  c.append(ta);act(c,'Ask the agent',async()=>{openChat(true);inp.value='Use the board-pack specialist to prepare a board pack: '+ta.value;sendChat();return 'Asked: follow it in the chat; the pack appears here.'},1);g.append(c)}
 if(!P.length)g.append(h('p','empty','No board packs yet.'));
 P.slice().reverse().forEach(m=>{const c=card(m.title||m.id,`${m.status} · made ${cell(m.made)}${m.audience?' · for '+m.audience:''}`);
  c.append(h('p',m.notes_grounded?'ok':'bad',m.notes_grounded?'Every number in the notes is in the pack\'s report views.':'Notes not checked.'));
  const fl=h('div','areas');[...m.items,{file:'notes.md',kind:'notes',status:'ok'}].forEach(it=>{const b=h('button','lk');b.append(h('b','',it.kind),h('span','',it.status==='ok'?it.file:'not made: '+(it.error||'')));
   if(it.status==='ok')b.onclick=async()=>{try{dl(await fileOf('/packs/file',{pack:m.id,file:it.file}),it.file)}catch(e){toast(e.message)}};fl.append(b)});c.append(fl);g.append(c)})}
// Users & access (workspace owners): ABI's accounts and roles; the app's sections and reports per person
async function usersPage(box){
 const post=(op,b)=>fetch('/admin/'+op,{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify(b||{})}).then(r=>r.json());
 const r=await post('users');if(r.error){box.append(h('p','bad',r.error));return}const U=r.result,g=h('div','cards');box.append(g);
 const inv=card('Invite','ABI creates the account and emails a link to choose a password'),em=h('input','f');em.type='email';em.placeholder='name@company.com';inv.append(em);
 const ad=card('Add a person','no email needed: you choose a starting password and hand it over; they change it by clicking their name'),an=h('input','f'),ap=h('input','f');an.placeholder='name';an.autocomplete='off';ap.type='password';ap.placeholder='starting password (8+ characters)';ap.autocomplete='new-password';ad.append(an,ap);
 act(ad,'Add',async()=>{const x=await post('adduser',{name:an.value,password:ap.value});if(x.error)throw new Error(x.error);ap.value='';setTimeout(()=>show(CUR),1200);return 'Added. Give them a role below.'},1);g.append(ad);
 act(inv,'Invite',async()=>{let x=await post('invite',{email:em.value});if(x.confirm){if(!await ask(x.confirm))return 'Not invited.';x=await fetch('/confirm/'+x.confirm.id,{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:'{}'}).then(y=>y.json())}
  if(x.error)throw new Error(x.error);return 'Invited through ABI. Give them a role below.'},1);g.append(inv);
 const t=card(`People in ${U.project}`,'role: Viewer reads the published copy; Editor and Owner build (Developer)');t.style.gridColumn='1/-1';g.append(t);
 U.users.forEach(u=>{const row=h('div','hit'),role=(u.roles||{})[U.project]||'none',sel=h('select','f');['none','viewer','editor','owner'].forEach(x=>sel.append(new Option(x==='none'?'no access':x,x)));sel.value=role;
  const a=U.access[u.name]||{},sx=h('div','checks'),rx=h('div','checks'),m=h('div','msg');
  U.sections.forEach(x=>{const l=h('label','chk'),c=h('input');c.type='checkbox';c.value=x;c.checked=!a.sections||a.sections.includes(x);l.append(c,h('span','',x));sx.append(l)});
  U.reports.forEach(x=>{const l=h('label','chk'),c=h('input');c.type='checkbox';c.value=x.id;c.checked=!a.reports||a.reports.includes(x.id);l.append(c,h('span','',x.name));rx.append(l)});
  const save=btn('Save',async()=>{try{if(sel.value!==role){let x=await post('role',{user:u.name,role:sel.value});
    if(x.confirm){if(!await ask(x.confirm))throw new Error('Not changed.');x=await fetch('/confirm/'+x.confirm.id,{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:'{}'}).then(y=>y.json())}if(x.error)throw new Error(x.error)}
   const pick=el=>{const v=[...el.querySelectorAll('input:checked')].map(c=>c.value);return v.length===el.querySelectorAll('input').length?null:v};
   const y=await post('access',{user:u.name,sections:pick(sx),reports:pick(rx)});if(y.error)throw new Error(y.error);say(m,'Saved.')}catch(e){say(m,e.message,1)}},1);
  const np=h('input','f');np.type='password';np.autocomplete='new-password';np.placeholder='a new password for them (8+ characters)';
  const setp=btn('Set password',async()=>{try{const x=await confirmed(await post('setpass',{user:u.name,password:np.value}));np.value='';if(x)say(m,'Set: hand it over; they are signed out everywhere.')}catch(e){say(m,e.message,1)}});
  row.append(h('div','',`${u.name}${u.workspace_owner?' · workspace owner (sees everything)':''}${u.tenant?' · tenant '+u.tenant:''}`),h('label','l','Role in this project'),sel,
   h('label','l','Sections'),sx,h('label','l','Reports'),rx,save,h('label','l','Forgot their password?'),np,setp,m);t.append(row)});
 const note=card('What this controls','and what it does not');note.append(h('p','','Role, sign-in and row-level security are ABI\'s: they decide what data a person can read or change, everywhere. Sections and reports decide what this app shows them and the app\'s own routes refuse the rest. Row and column rules are on the Security page.'));g.append(note)}
// Settings (admins): the agent's model and reasoning, and the providers (API keys go in, never come out)
const PRESETS={openai:['OpenAI','openai-responses','https://api.openai.com/v1','gpt-5'],anthropic:['Anthropic','anthropic-messages','https://api.anthropic.com','claude-sonnet-4-5'],
 openrouter:['OpenRouter','openai-completions','https://openrouter.ai/api/v1','anthropic/claude-sonnet-4.5'],ollama:['Ollama (local)','openai-completions','http://host.docker.internal:11434/v1','llama3.1'],
 gemini:['Google Gemini','google-generative-ai','https://generativelanguage.googleapis.com/v1beta','gemini-2.5-pro'],custom:['OpenAI-compatible','openai-completions','https://','model-id']};
async function settingsPage(box){
 const post=(op,b)=>fetch('/admin/'+op,{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify(b||{})}).then(r=>r.json());
 let r=await post('settings');if(r.error){box.append(h('p','bad',r.error));return}let S=r.result;const g=h('div','cards');box.append(g);
 const draw=()=>{g.innerHTML='';
  const def=card('Agent','the model every chat and specialist uses'),ms=h('select','f'),th=h('select','f');ms.append(new Option('the built-in default',''));
  S.providers.forEach(p=>p.models.forEach(m=>ms.append(new Option(`${p.name} · ${m}`,`${p.id}/${m}`))));ms.value=S.model||'';S.levels.forEach(l=>th.append(new Option(l,l)));th.value=S.thinking||'medium';
  const fb=h('div','checks');S.providers.forEach(p=>p.models.forEach(m=>{const l=h('label','chk'),i=h('input');i.type='checkbox';i.value=`${p.id}/${m}`;i.checked=(S.fallbacks||[]).includes(i.value);l.append(i,h('span','',`${p.name} · ${m}`));fb.append(l)}));
  def.append(h('label','l','Model'),ms,h('label','l','Reasoning'),th,h('label','l','If it fails (quota, outage), try in order'),fb);
  const dm=act(def,'Save',async()=>{const x=await post('settings/default',{model:ms.value||null,thinking:th.value,fallbacks:[...fb.querySelectorAll('input:checked')].map(i=>i.value)});if(x.error)throw new Error(x.error);S=x.result;return 'Saved: new chats and specialists use it.'},1);
  def.addEventListener('change',()=>say(dm,'Not saved yet: click Save to use it.',1));g.append(def);
  const us=card('Usage','the agents\' model work, last 30 days');g.append(us);post('usage').then(x=>{const U=x.result;if(!U||!U.model.length){us.append(h('p','empty','No answers yet.'));return}
   const n=v=>Number(v).toLocaleString(),tb=(k,t)=>{us.append(h('label','l',t));const tt=h('table');tt.innerHTML='<tr><th></th><th>answers</th><th>failed</th><th>input</th><th>output</th><th>cache</th><th>cost</th></tr>';
    U[k].forEach(r=>{const tr=h('tr');[r.name,n(r.answers),n(r.failed),n(r.input),n(r.output),n(r.cache),r.cost?r.cost.toFixed(2):'–'].forEach(v=>tr.append(h('td','',v)));tt.append(tr)});us.append(tt)};tb('user','Per person');tb('model','Per model')});
  S.providers.forEach(p=>{const c=card(p.name,`${p.id} · ${S.apis[p.api]||p.api}`);c.append(h('p','file',p.base_url),h('p','',`Models: ${p.models.join(', ')}`),h('p',p.key_set?'ok':'',p.key_builtin?'Uses the built-in connection\'s key':p.key_set?`API key set, ends in ${p.key_ends}`:'No API key'));
   act(c,'Test connection',async()=>{const x=await post('settings/test',{id:p.id});if(x.error)throw new Error(x.error);if(!x.result.ok)throw new Error(x.result.message);return x.result.message});
   act(c,'Remove',async()=>{const x=await post('settings/remove',{id:p.id});S=x.result;draw()});g.append(c)});
  const add=card('Connect a provider','OpenAI, Anthropic, OpenRouter, Ollama, Gemini or any OpenAI-compatible endpoint'),pre=h('select','f'),id=h('input','f'),nm=h('input','f'),api=h('select','f'),base=h('input','f'),key=h('input','f'),mods=h('input','f');
  Object.entries(PRESETS).forEach(([k,v])=>pre.append(new Option(v[0],k)));Object.entries(S.apis).forEach(([k,v])=>api.append(new Option(v,k)));key.type='password';key.autocomplete='off';key.placeholder='API key (stored encrypted; leave empty to keep the stored one)';mods.placeholder='model IDs, comma separated';
  const pick=h('div','checks mlist');mods.type='hidden';
  const fill=()=>{const v=PRESETS[pre.value];id.value=pre.value==='custom'?'':pre.value;nm.value=v[0];api.value=v[1];base.value=v[2];mods.value='';pick.innerHTML='<p class=file>Fetch models to pick from the provider\'s list.</p>'};pre.onchange=fill;fill();
  const fetchM=async()=>{const x=await post('settings/models',{provider:{id:id.value.trim(),api:api.value,base_url:base.value.trim()},key:key.value});if(x.error)throw new Error(x.error);
   if(!x.result.ok)throw new Error(x.result.message);const want=new Set(mods.value.split(',').map(s=>s.trim()).filter(Boolean));pick.innerHTML='';
   x.result.models.forEach(m=>{const l=h('label','chk'),i=h('input');i.type='checkbox';i.value=m;i.checked=want.has(m);i.onchange=()=>{mods.value=[...pick.querySelectorAll('input:checked')].map(i=>i.value).join(',')};l.append(i,document.createTextNode(m));pick.append(l)});
   mods.value=[...pick.querySelectorAll('input:checked')].map(i=>i.value).join(',');return x.result.message+' Tick the ones the agent may use.'};
  add.append(h('label','l','Preset'),pre,h('label','l','ID'),id,h('label','l','Name'),nm,h('label','l','API'),api,h('label','l','Base URL'),base,h('label','l','API key'),key,h('label','l','Models'),pick,mods);
  act(add,'Fetch models',fetchM);
  act(add,'Save provider',async()=>{const x=await post('settings/provider',{provider:{id:id.value.trim(),name:nm.value,api:api.value,base_url:base.value.trim(),models:mods.value.split(',').map(s=>s.trim()).filter(Boolean)},key:key.value});if(!mods.value)throw new Error('Fetch models and tick at least one.');
   key.value='';if(x.error)throw new Error(x.error);S=x.result;draw();return 'Saved.'},1);g.append(add);people()};
 const people=async()=>{const r=(await post('sso')).result||{providers:[]},M=r.mail||{},f=(ph,v,t)=>{const i=h('input','f');i.placeholder=ph;i.value=v??'';if(t)i.type=t;i.autocomplete='off';return i};
  const warn=r.reachable?null:h('p','bad','Set ABI_PUBLIC_URL (the address people open, e.g. https://abi.example.com) in .env and restart: emailed links and SSO go there.');
  const em=card('Email','invites and "forgot your password" links; without it you set passwords in Users & access'),host=f('smtp.example.com',M.host),port=f('587',M.port),from=f('abi@example.com',M.from),sec=h('select','f'),us=f('user name (optional)',M.username),pw=f(M.username?'password (stored in ABI; empty keeps it)':'password','','password');
  ['starttls','tls','none'].forEach(x=>sec.append(new Option({starttls:'STARTTLS (port 587)',tls:'TLS (port 465)',none:'none (a local relay)'}[x],x)));sec.value=M.security||'starttls';
  em.append(...(warn?[warn.cloneNode(true)]:[]),h('p','',M.host?`On: ${M.host}:${M.port} from ${M.from}`:'Off.'),h('label','l','Server'),host,h('label','l','Port'),port,h('label','l','Connection'),sec,h('label','l','From'),from,h('label','l','Sign-in'),us,pw);
  act(em,'Save',async()=>{const x=await confirmed(await post('mail',{host:host.value,port:Number(port.value),security:sec.value,from:from.value,username:us.value,password:pw.value}));pw.value='';if(!x)return 'Not changed.';draw();return 'Saved.'},1);
  if(M.host)act(em,'Turn off',async()=>{const x=await confirmed(await post('mail',{off:true}));if(x)draw();return x?'Off.':'Not changed.'});g.append(em);
  const so=card('Single sign-on','people sign in with your company account (OpenID Connect: Google, Microsoft Entra, Okta, Keycloak, …)'),pid=f('id, e.g. google'),pn=f('button name, e.g. Google'),iss=f('issuer, e.g. https://accounts.google.com'),cl=f('client ID'),cs=f('client secret (stored in ABI)','','password');
  so.append(...(warn?[warn]:[]),h('p','file','Redirect URI at your provider: '+r.callback.replace('<id>','{id}')),...r.providers.map(x=>{const row=h('div','row');row.append(h('span','',x.name+' · '+x.id),btn('Remove',async()=>{const y=await confirmed(await post('sso/remove',{id:x.id}));if(y)draw()}));return row}),
   h('label','l','Add a provider'),pid,pn,iss,cl,cs,h('p','','Each person links it once: they sign in with their password, click their name and Link.'));
  act(so,'Add',async()=>{const x=await confirmed(await post('sso/save',{id:pid.value.trim(),name:pn.value.trim(),issuer:iss.value.trim(),client_id:cl.value.trim(),secret:cs.value}));cs.value='';if(!x)return 'Not added.';draw();return 'Added.'},1);g.append(so)};
 draw()}
function transferPage(box){if(MODE==='viewer'){box.append(h('p','empty','Export and import work in Developer mode with the live app.'));return}
 const ex=card('Export','the whole project as one zip'),im=card('Import','replace this project with a zip; ABI checks it first'),g=h('div','cards');
 ex.append(h('p','','ABI\'s committed files (sources, tables, model, measures, glossary, queries, reports, flows, ML, tools, agents, security, environments, docs), the app\'s JSON (Monitor settings, saved analyses), the vault\'s documents and the team\'s plan. Not inside: the data (refresh the sources after an import), secret values, people\'s roles, chats, uncommitted changes.'));
 act(ex,'Download zip',async()=>{const r=await fetch('/transfer/export',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:'{}'});if(!r.ok)throw new Error((await r.json()).error);
  const a=h('a');a.href=URL.createObjectURL(await r.blob());a.download=`${D.project}.abi.zip`;a.click();return 'Downloaded.'},1);
 const f=h('input','f');f.type='file';f.accept='.zip';const out=h('div','body');im.append(f,out);let data=null;
 const post=b=>fetch('/transfer/import',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify(b)}).then(r=>r.json());
 act(im,'Check',async()=>{if(!f.files[0])return 'Pick a zip first.';data=await new Promise(r=>{const fr=new FileReader();fr.onload=()=>r(fr.result.split(',')[1]);fr.readAsDataURL(f.files[0])});
  const r=await post({data});if(r.error)throw new Error(r.error);const ch=r.result?.changes||[],m=r.manifest||{};out.innerHTML='';
  out.append(h('p','file',`${m.name||m.project} · exported ${m.exported} · ABI commit ${String(m.abi_commit||'').slice(0,8)}`),
   ...(ch.length?ch.map(c=>h('p','',typeof c==='string'?c:JSON.stringify(c))):[h('p','ok','No changes: this project already matches the zip.')]),
   ...(m.needs?.secrets?.length?[h('p','bad','Create these secrets in ABI first: '+m.needs.secrets.join(', '))]:[]),
   ...(r.uncommitted?[h('p','bad',`This project has ${r.uncommitted} uncommitted change${r.uncommitted>1?'s':''}: Apply replaces them too. Commit first to keep them.`)]:[]));return ch.length?`${ch.length} changes. Apply to replace the project.`:'Nothing to apply.'});
 act(im,'Apply',async()=>{if(!data)return 'Check the zip first.';let r=await post({data,apply:true});
  if(r.confirm){if(!await ask(r.confirm))throw new Error('You declined. Nothing changed.');r=await fetch('/confirm/'+r.confirm.id,{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:'{}'}).then(x=>x.json())}
  if(r.error)throw new Error(r.error);return 'Imported through ABI (one undoable commit). Refresh the sources to load the data.'});
 g.append(ex,im);box.append(g)}
// Export: ABI renders the report as the person (PDF, PNG, PPTX; several pages in one file)
function exportCard(rep,filters,curPage){const c=card('Export',`rendered by ABI · ${rep.name||rep.id}`),fm=h('select','f'),vs=h('select','f'),pg=h('div','checks'),ap=h('input');
 ['pdf','pptx','png'].forEach(f=>fm.append(new Option({pdf:'PDF',pptx:'PowerPoint (PPTX)',png:'Image (PNG)'}[f],f)));ap.type='checkbox';ap.checked=true;
 rep.pages.forEach(p=>{const l=h('label','chk'),x=h('input');x.type='checkbox';x.value=p.id;x.checked=p.id===curPage().id;l.append(x,h('span','',p.name||p.id));pg.append(l)});
 const fill=()=>{const sel=[...pg.querySelectorAll('input:checked')];vs.innerHTML='';vs.append(new Option('the whole page',''));vs.disabled=sel.length!==1;
  if(sel.length===1)rep.pages.find(p=>p.id===sel[0].value).visuals.forEach(v=>vs.append(new Option(v.title||v.id,v.id)))};pg.onchange=fill;fill();
 const al=h('label','chk');al.append(ap,h('span','','with the filters set now'));
 c.append(h('label','l','Format'),fm,h('label','l','Pages'),pg,h('label','l','Only one view (one page selected)'),vs,al);
 act(c,'Download',async()=>{const pages=[...pg.querySelectorAll('input:checked')].map(x=>x.value);if(!pages.length)return 'Pick at least one page.';
  const one=pages.length===1,body={report:rep.id,format:fm.value,filters:ap.checked?filters():[],...(one?{page:pages[0],visual:vs.value||null}:{pages})};
  const get=async b=>{const r=await fetch('/report/export',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify(b)});if(!r.ok)throw new Error((await r.json()).error);return r.blob()};
  const save=(blob,name)=>{const a=h('a');a.href=URL.createObjectURL(blob);a.download=name;a.click()};
  try{save(await get(body),`${rep.id}${one?'-'+pages[0]:''}.${fm.value}`);return 'Downloaded.'}
  catch(e){if(one||fm.value==='pptx')throw e;for(const p of pages)save(await get({...body,pages:undefined,page:p}),`${rep.id}-${p}.${fm.value}`);
   return `This ABI makes one file per page: downloaded ${pages.length}.`}},1);
 c.classList.add('editor');return c}
function reportsPage(box){const bar=h('div','row bar top');bar.append(btn('New report',()=>{const c=editor('report',KIND.report.tpl,true);g.prepend(c)},1));box.append(bar);
 const g=h('div','cards stack');box.append(g);if(!D.reports.length){box.append(h('p','empty','No reports yet. Ask the agent to build one.'));return}
 D.reports.forEach(r=>{const c=card(r.name||r.id,`reports/${r.id}.json`),n=r.pages.reduce((a,p)=>a+p.visuals.length,0);
  c.append(h('p','',r.spec?.description||r.description||''),h('p','file',`${r.pages.length} page${r.pages.length===1?'':'s'}: ${r.pages.map(p=>p.name||p.id).join(', ')} · ${n} visuals`));
  act(c,'Open',async()=>{show('rep:'+r.id)},1);g.append(c)})}
// Monitor: is anything wrong right now? Only what reports can't show: data freshness, model readiness, ML runs, flows
// a check: alert when a measure crosses a value, a table goes stale, or a flow's last run failed
const CMP={'<':(a,b)=>a<b,'<=':(a,b)=>a<=b,'>':(a,b)=>a>b,'>=':(a,b)=>a>=b,'=':(a,b)=>a==b,'!=':(a,b)=>a!=b};
const OPS=[['<','is below'],['<=','is at most'],['>','is above'],['>=','is at least'],['=','equals'],['!=','is not']],opw=o=>(OPS.find(x=>x[0]===o)||[o,o])[1],mname=id=>D.measures.find(m=>m.id===id)?.name||id;
// a measure check compares the measure with a value, with another measure (a target), or with its previous period (% change);
// optionally filtered, and optionally for each member of a dimension (alerts when any member matches)
async function monEval(c){if(c.kind==='measure'){const f=c.filters||[],vs=c.against||'value';
  const sk=c.skip_latest?1:0;  // completed periods only: the latest (still filling) is left out
  if(vs==='prev'){const r=await api('semantic_query',{measures:[c.measure],dimensions:[c.period],filters:f,order_by:[{field:c.period,desc:true}],limit:2+sk}),[a,b]=(r.rows||[]).slice(sk);
   if(!a||!b||typeof a[1]!=='number'||typeof b[1]!=='number'||!b[1])return {ok:true,text:`${mname(c.measure)}: not enough periods to compare yet`};
   const pct=(a[1]-b[1])/Math.abs(b[1])*100;return {ok:!CMP[c.op](pct,Number(c.value)),text:`${mname(c.measure)}: ${fmt(a[1])} in ${cell(a[0])}, ${pct>=0?'+':''}${pct.toFixed(1)}% on ${cell(b[0])} · alerts when the change ${opw(c.op)} ${fmt(Number(c.value))}%`}}
  let fl=f,at='';if(c.period){const l=await api('semantic_query',{measures:[c.measure],dimensions:[c.period],filters:f,order_by:[{field:c.period,desc:true}],limit:1+sk}),pv=(l.rows||[])[sk]?.[0];
   if(pv==null)return {ok:true,text:`${mname(c.measure)}: no ${c.period.split('.').pop()} with data yet`};fl=[...f,{column:c.period,op:'=',value:pv}];at=` in ${cell(pv)}`}  // the latest (completed) period
  const r=await api('semantic_query',{measures:vs==='measure'?[c.measure,c.other]:[c.measure],dimensions:c.by?[c.by]:[],filters:fl,limit:500}),n=c.by?1:0;
  const bad=(r.rows||[]).filter(x=>typeof x[n]==='number'&&CMP[c.op](x[n],vs==='measure'?x[n+1]:Number(c.value)));
  const what=vs==='measure'?mname(c.other):fmt(Number(c.value)),where=(f.length?` (${f.map(x=>`${x.column} = ${x.value}`).join(', ')})`:'')+at;
  if(c.by)return {ok:!bad.length,text:bad.length?`${bad.length} of ${(r.rows||[]).length} ${c.by.split('.').pop()} ${opw(c.op)} ${what}: ${bad.slice(0,5).map(x=>x[0]).join(', ')}${bad.length>5?'…':''}${where}`:`No ${c.by.split('.').pop()} ${opw(c.op)} ${what}${where}`};
  const x=(r.rows||[])[0]||[];return {ok:typeof x[0]==='number'&&!bad.length,text:`${mname(c.measure)}: ${fmt(x[0])}${vs==='measure'?` vs ${what} ${fmt(x[1])}`:''}${where} · alerts when it ${opw(c.op)} ${what}`}}
 if(c.kind==='fresh'){const q=D.quality||{},ix=k=>(q.columns||[]).indexOf(k),row=(q.rows||[]).find(x=>x[0]===c.table),t=row&&row[ix('last_load')];
  const hrs=t?(Date.now()-Date.parse(t))/3600e3:Infinity;return {ok:hrs<=c.hours,text:`${c.table}: last load ${t?Math.round(hrs)+' h ago':'never'} · alerts after ${c.hours} h`}}
 const x=await api('flow_runs',{flow:c.flow}).catch(()=>({}));const one=x.runs?.[0]||x;return {ok:one.status!=='failed',text:`${c.flow}: last run ${one.status||'never'} · alerts when a run fails`}}
function newMonitor(box,ck){box.querySelector('.monform')?.remove();ck=ck||{};const c=card(ck.id?'Edit monitor':'New monitor','what to watch, against what; it turns red when the condition holds');c.classList.add('monform','wide');
 const nm=h('input','f'),kind=h('select','f'),f=h('div');nm.value=ck.name||'';nm.placeholder='e.g. Views below target';[['measure','A measure crosses a value, a target or its last period'],['fresh','A table is not loaded in time'],['flow','A flow\'s last run failed']].forEach(([k,l])=>kind.append(new Option(l,k)));kind.value=ck.kind||'measure';
 const s=(x,v)=>{const e=h('select','f');x.forEach(([v,l])=>e.append(new Option(l,v)));if(v!==undefined)e.value=v;return e},L=t=>h('label','l',t);
 const draw=()=>{f.innerHTML='';
  if(kind.value==='measure'){const ms=D.measures.map(x=>[x.id,x.name||x.id]),m=s(ms,ck.measure),vs=s([['value','a value'],['measure','another measure (e.g. a target)'],['prev','its previous period (% change)']],ck.against||'value'),
    op=s(OPS,ck.op),v=h('input','f'),other=s(ms,ck.other),per=s(D.dims.filter(d=>d.startsWith('calendar.')).map(d=>[d,d]),ck.period),by=s([['','the total'],...D.dims.map(d=>[d,'each '+d])],ck.by||''),
    fc=s([['','no filter'],...D.dims.map(d=>[d,d])],ck.filters?.[0]?.column||''),fv=h('input','f');v.type='number';v.placeholder='value';v.value=ck.value??'';fv.placeholder='equals…';fv.value=ck.filters?.[0]?.value??'';
   const lv=L('Value'),lo=L('Other measure'),lp=L('Period'),lb=L('Check');const vis=()=>{const a=vs.value;lv.hidden=v.hidden=a==='measure';lv.textContent=a==='prev'?'Change in %':'Value';lo.hidden=other.hidden=a!=='measure';lp.hidden=per.hidden=a!=='prev';lb.hidden=by.hidden=a==='prev'};vs.onchange=vis;
   f.append(L('Measure'),m,L('Compare with'),vs,L('Alert when it'),op,lv,v,lo,other,lp,per,lb,by,L('Only where (optional)'),fc,fv);vis();
   f.get=()=>{const a=vs.value;if(a!=='measure'&&v.value==='')throw new Error('Give the value to compare with.');if(a==='prev'&&!per.value)throw new Error('The model has no calendar to compare periods.');
    return {measure:m.value,against:a,op:op.value,...(a==='measure'?{other:other.value}:{value:Number(v.value)}),...(a==='prev'?{period:per.value}:by.value?{by:by.value}:{}),...(fc.value?{filters:[{column:fc.value,op:'=',value:fv.value}]}:{})}}}
  else if(kind.value==='fresh'){const t=s((D.tables||[]).map(x=>[x,x]),ck.table),hr=h('input','f');hr.type='number';hr.value=ck.hours??36;f.append(L('Table'),t,L('Alert after (hours without a load)'),hr);f.get=()=>({table:t.value,hours:Number(hr.value)})}
  else{const fl=s((D.flows||[]).map(x=>[x.id,x.name||x.id]),ck.flow);f.append(L('Flow'),fl);f.get=()=>({flow:fl.value})}};kind.onchange=draw;draw();
 c.append(L('Name'),nm,L('Watch'),kind,f);
 act(c,ck.id?'Save':'Create',async()=>{if(!nm.value.trim())throw new Error('Give it a name.');const nc={id:ck.id||nm.value.trim().toLowerCase().replace(/[^a-z0-9]+/g,'_').replace(/^_|_$/g,'')||'check',name:nm.value.trim(),kind:kind.value,...f.get()};
  const all=[...(D.app.monitor.checks||[]).filter(x=>x.id!==nc.id),nc];const r=await fetch('/monitor',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify({checks:all})}).then(x=>x.json());
  if(r.error)throw new Error(r.error);D.app.monitor.checks=all;show('monitor')},1);act(c,'Cancel',()=>c.remove());(box.querySelector('.pghead')||box).after(c)}
function monitorPage(box){const bar=h('div','row bar top');bar.append(btn('New monitor',()=>newMonitor(box),1));box.append(bar);
 const mine=h('div','cards');box.append(mine);(D.app?.monitor?.checks||[]).forEach(ck=>{const c=card(ck.name);c.querySelector('h3').append(pinBtn({kind:'monitor',ref:ck.id,title:ck.name}));const st=h('p','file','checking…');c.append(st);mine.append(c);
  monEval(ck).then(x=>{st.className=x.ok?'ok':'bad';st.textContent=(x.ok?'OK · ':'Alert · ')+x.text}).catch(e=>{st.className='bad';st.textContent=e.message});
  if(MODE==='developer')act(c,'Edit',()=>newMonitor(box,ck));if(MODE==='developer')act(c,'Delete',async()=>{const all=D.app.monitor.checks.filter(x=>x.id!==ck.id);await fetch('/monitor',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify({checks:all})});D.app.monitor.checks=all;show('monitor')})});
 const g=h('div','cards');box.append(g);const q=D.quality||{},ix=c=>(q.columns||[]).indexOf(c),rs=q.rows||[],when=t=>t?String(t).slice(0,16).replace('T',' '):'—';
 const M=D.app?.monitor||{watch:[],fresh_within_hours:36},watch=t=>M.watch?.length?M.watch.includes(t):/stream/.test(t),old=r=>watch(r[0])&&(!r[ix('last_load')]||Date.now()-Date.parse(r[ix('last_load')])>M.fresh_within_hours*3600e3);
 const fr=card('Data freshness',`ABI data quality; watched tables must load within ${M.fresh_within_hours} h (monitor.json)`),bad=rs.filter(r=>(r[ix('problems')]||[]).length||r[ix('missing_periods')]||old(r));
 fr.append(h('p',bad.length?'bad':'ok',bad.length?`${bad.length} of ${rs.length} tables have problems`:`All ${rs.length} tables without problems`));
 const t=h('table');t.insertRow().append(...['table','last load','rows','missing periods','problems'].map(x=>h('th','',x)));
 [...bad,...rs.filter(r=>watch(r[0])&&!bad.includes(r))].forEach(r=>{const tr=t.insertRow();[r[0],when(r[ix('last_load')]),fmt(r[ix('rows')]),r[ix('missing_periods')]||0,[...(r[ix('problems')]||[]).map(p=>p.message||p.kind||JSON.stringify(p)),...(old(r)?[`no load for over ${M.fresh_within_hours} h`]:[])].join('; ')||'—'].forEach(v=>tr.insertCell().textContent=v)});
 const sc=h('div','scroll');sc.append(t);fr.append(h('p','file','tables with problems, then the watched tables'),sc);
 const rd=D.readiness||{},mo=card('Model','readiness');mo.append(h('p',rd.ready?'ok':'bad',rd.ready?'Ready':'Not ready'),...(rd.missing||[]).map(m=>h('p','file',typeof m==='string'?m:JSON.stringify(m))));
 const ml=card('ML models','latest training and scoring');[[fr,'freshness'],[mo,'model'],[ml,'ml']].forEach(([c,r])=>c.querySelector('h3').append(pinBtn({kind:'status',ref:r,title:{freshness:'Data freshness',model:'Model readiness',ml:'ML models'}[r]})));if(!D.ml.length)ml.append(h('p','empty','No ML models yet.'));
 D.ml.forEach(m=>{const tr=m.latest_run||{},sc2=m.latest_score||{};ml.append(h('h4','',m.name||m.id),
  h('p',tr.status==='ok'?'ok':'bad',`trained: ${tr.status||'never'} · ${when(tr.finished||tr.created)}`),h('p',sc2.status==='ok'?'ok':'bad',`scored: ${sc2.status||'never'} · ${when(sc2.finished||sc2.created)}`))});
 const fl=card('Flows','switched on or off, last run');fl.querySelector('h3').append(pinBtn({kind:'status',ref:'flows',title:'Flows'}));if(!D.flows.length)fl.append(h('p','empty','No flows yet.'));
 D.flows.forEach(f=>{const l=h('p','file','last run: …');fl.append(h('h4','',f.name||f.id),h('p',f.enabled?'ok':'',f.enabled?'on':'off'),l);
  api('flow_runs',{flow:f.id}).then(x=>{const r=(x.runs||[])[0];l.textContent=r?`last run: ${r.status} · ${when(r.finished||r.started||r.created)}`:'last run: never'}).catch(e=>l.textContent='last run: '+e.message)});
 g.append(fr,mo,ml,fl);fr.style.gridColumn='1/-1'}
// Explore: anyone's own analysis on the governed model (reads only, so Viewer can use it); kept analyses stay in this browser
let MINE=D.app?.analyses||[];if(!MINE.length)try{MINE=JSON.parse(localStorage.getItem('abi-explore')||'[]');if(MINE.length)setTimeout(saveMine,0)}catch(e){}
function saveMine(){MINE=MINE.slice(0,100);fetch('/analyses',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify({analyses:MINE})}).catch(()=>{})}
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
 const fr=h('div','formrow'),ff=fld('Filter',fc);ff.append(fv);fr.append(fld('Measures',ms),fld('By',dm),ff,fld('Chart',ty));c.append(fr);
 act(c,'Run',async()=>{await run(query(),ty.value)},1);
 act(c,'Keep',async()=>{const q=query();if(!q.measures.length)return 'Pick at least one measure.';
  const nm=q.measures.map(m=>D.measures.find(x=>x.id===m)?.name||m).join(', ')+(q.dimensions[0]?' by '+q.dimensions[0]:'');
  MINE.unshift({name:nm,q,type:ty.value,at:new Date().toISOString().slice(0,16).replace('T',' ')});saveMine();mine();return 'Kept in Saved analyses, for everyone on this app.'});
 act(c,'Save as query',async()=>{const q=query();if(!q.measures.length)return 'Pick at least one measure.';
  const id=('q_'+q.measures.join('_')+(q.dimensions[0]?'_by_'+q.dimensions[0].split('.').pop():'')).toLowerCase().replace(/[^a-z0-9_]/g,'_').slice(0,60);
  await KIND.dataset.save({id,name:id,description:'Saved from Explore',query:q});return `Saved as query ${id} through ABI.`});
 c.append(msg,out);const g=h('div','cards'),my=card('Saved analyses','kept with the project for everyone; numbers rerun from the model when opened'),list=h('div');my.append(list);c.style.gridColumn=my.style.gridColumn='1/-1';ms.style.width='100%';g.append(c,my);box.append(g);
 function mine(){list.innerHTML='';if(!MINE.length){list.append(h('p','empty','Nothing kept yet. Run an analysis and press Keep; charts the agent shows you land here too.'));return}
  MINE.forEach((a,i)=>{const r=h('div','row');const o=h('button','btn',a.name);o.onclick=()=>{[...ms.options].forEach(x=>x.selected=a.q.measures.includes(x.value));dm.value=a.q.dimensions[0]||'';ty.value=a.type;flt=a.q.filters||[];fc.value='';fv.hidden=true;run(a.q,a.type)};
   const x=h('button','btn','forget');x.onclick=()=>{MINE.splice(i,1);saveMine();mine()};r.append(o,h('span','file',a.at||''),pinBtn({kind:'analysis',ref:{q:a.q,type:a.type},title:a.name}),x);list.append(r)})}
 mine();if(q0.measures)run(query(),ty.value)}
function measures(box){modelBar(box);const top=h('div','row bar top'),q=h('input','f');q.placeholder='Search measures';const list=h('div','cards rows');box.append(top,list);
 const edit=(m,isNew)=>{const c=card(isNew?'New measure':(m.name||m.id),isNew?'':m.id),name=h('input','f'),id=h('input','f'),ex=h('textarea','f'),de=h('input','f');
  [name,id,ex,de].forEach(e=>e.classList.add('ed'));name.value=m.name||'';id.value=m.id||'';ex.value=m.expression||'';de.value=m.description||'';
  id.placeholder='a-z, 0-9, _';name.placeholder='display name';ex.placeholder='e.g. SUM(sales.revenue)';de.placeholder='what it means';
  if(isNew)c.append(h('label','l','ID'),id,h('label','l','Name'),name,h('label','l','MEL'),ex,h('label','l','Description'),de);
  ex.classList.add('code');if(!isNew){ex.rows=1;rowcard(c,fld('Name',name,m.id),fld('MEL',ex),fld('Description',de))}
  act(c,isNew?'Create':'Save',async()=>{await api('set_measure',{id:id.value,name:name.value,expression:ex.value,description:de.value||null});
   Object.assign(m,{id:id.value,name:name.value,expression:ex.value,description:de.value});if(isNew){D.measures.push(m);show('measures')}return 'Saved through ABI (checked like an import).'},1);
  // Test: the measure's total through ABI. ABI runs saved measures only, so an edit not yet saved is not what it tests
  act(c,'Test',async msg=>{say(msg,'Testing…');const t0=Date.now(),r=await api('semantic_query',{measures:[id.value]}),v=r.rows?.[0]?.[0];
   return `${r.measures?.[id.value]||id.value} = ${fmt(v,r.format?.[id.value])} · total over all data, ${((Date.now()-t0)/1000).toFixed(1)} s`+(c.classList.contains('dirty')?' · this is the saved version: Save to test your change':'')});
  if(!isNew)act(c,'Delete',async()=>{await api('delete_measure',{id:id.value});D.measures=D.measures.filter(x=>x!==m);show('measures')});return c};
 const fill=()=>{list.innerHTML='';list.append(rowhead('Measure','MEL','Description',''));D.measures.filter(nmMeasure).filter(m=>!q.value||JSON.stringify(m).toLowerCase().includes(q.value.toLowerCase())).forEach(m=>list.append(edit(m)))};
 top.append(q,btn('New measure',()=>list.firstChild.after(edit({},true)),1));q.oninput=fill;fill()}
function glossary(box){const list=h('div','cards rows');box.append(list);
 const edit=(t,isNew)=>{const c=card(isNew?'New term':(t.term||t.id)),term=h('input','f'),def=h('textarea','f'),ms=h('input','f');[term,def,ms].forEach(e=>e.classList.add('ed'));term.value=t.term||'';def.value=t.definition||'';ms.value=(t.measures||[]).join(', ');
  term.placeholder='business term';def.placeholder='what it means';ms.placeholder='e.g. revenue, units';
  if(isNew)c.append(h('label','l','Term'),term,h('label','l','Definition'),def,h('label','l','Implemented by (measure IDs)'),ms);
  else{def.rows=1;rowcard(c,fld('Term',term),fld('Definition',def),fld('Implemented by',ms))}
  act(c,isNew?'Create':'Save',async()=>{await api('set_glossary_term',{term:term.value,definition:def.value,measures:ms.value.split(',').map(x=>x.trim()).filter(Boolean),columns:t.columns||[]});
   Object.assign(t,{term:term.value,definition:def.value});if(isNew)D.glossary.push(t);return 'Saved through ABI.'},1);
  if(!isNew)act(c,'Delete',async()=>{await api('delete_glossary_term',{id:t.id});D.glossary=D.glossary.filter(x=>x!==t);show('glossary')});return c};
 const bar=h('div','row bar top');bar.append(btn('New term',()=>list.firstChild.after(edit({},true)),1));box.insertBefore(bar,list);
 list.append(rowhead('Term','Definition','Implemented by (measure IDs)',''));D.glossary.forEach(t=>list.append(edit(t)));if(!D.glossary.length)list.append(h('p','empty pad','No terms yet. New term adds the first.'))}

// ---------- Data ----------
async function upload(table,file){const id=[...crypto.getRandomValues(new Uint8Array(16))].map(b=>b.toString(16).padStart(2,'0')).join(''),N=24<<20;let r;
 for(let o=0;;o+=N){const last=o+N>=file.size,data=await new Promise(res=>{const fr=new FileReader();fr.onload=()=>res(fr.result.split(',')[1]||'');fr.readAsDataURL(file.slice(o,o+N))});  // 24 MB pieces: under the 64 MB request limit
  r=await fetch('/upload',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify({table,name:file.name,data,id,last})}).then(x=>x.json());
  if(r.error)throw new Error(r.error);if(last)return r.result}}
function sources(box){const q=D.quality||{},ix=c=>(q.columns||[]).indexOf(c),qr=Object.fromEntries((q.rows||[]).map(r=>[r[ix('table')],r]));
 const g=h('div','cards'),bar=h('div','row bar top');box.append(bar,g);
 const add=()=>{const c=card('Add a source','a Parquet file you upload, or an S3 folder of Parquet files');c.classList.add('editor');
  const t=h('input','f ed');t.placeholder='table id, e.g. bronze_orders';const loc=h('input','f ed');loc.placeholder='s3://bucket/folder/*.parquet';
  const ep=h('input','f ed');ep.placeholder='blank for AWS; or host[:port] for MinIO, R2, Wasabi…';const file=h('input');file.type='file';file.accept='.parquet';file.className='f ed';
  const rg=h('input','f ed');rg.value='us-east-1';const ki=h('input','f ed');ki.placeholder='access key ID (blank for a public bucket)';ki.autocomplete='off';
  const ks=h('input','f ed');ks.type='password';ks.placeholder='secret access key';ks.autocomplete='new-password';
  const tl=h('label','chk'),tls=h('input');tls.type='checkbox';tls.checked=true;tl.append(tls,h('span','','HTTPS (untick for a plain-HTTP endpoint such as a local MinIO)'));
  c.append(h('label','l','Table'),t,h('label','l','Upload a Parquet file'),file,h('label','l','Or an S3 folder of Parquet files'),loc,h('label','l','Region'),rg,h('label','l','S3 endpoint'),ep,tl,
   h('label','l','Keys (workspace owners; stored encrypted for this bucket only, never shown again)'),ki,ks);
  act(c,'Add source',async()=>{if(!t.value)throw new Error('Name the table first.');
   if(file.files[0]&&!/\.parquet$/i.test(file.files[0].name))throw new Error('Choose a .parquet file.');if(file.files[0]){await upload(t.value,file.files[0]);return `Uploaded and loaded into ${t.value}.`}
   if(!/^s3:\/\/.+\.parquet$/i.test(loc.value))throw new Error('Choose a Parquet file, or give an S3 folder like s3://bucket/folder/*.parquet.');
   const s3={region:rg.value.trim()||'us-east-1',...(ep.value?{endpoint:ep.value.trim(),url_style:'path'}:{}),...(tls.checked?{}:{use_ssl:false})};
   if(ki.value||ks.value){const r=await fetch('/s3key',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify({table:t.value,location:loc.value,...s3,key_id:ki.value,secret:ks.value})}).then(x=>x.json());
    ks.value='';if(r.error)throw new Error(r.error);Object.assign(s3,r.result)}
   await api('add_data',{table:t.value,location:loc.value,format:'parquet',s3});return `Loaded ${t.value}.`},1);return c};
 bar.append(btn('Add source',()=>g.prepend(add()),1));
 if(!D.sources.length)box.append(h('p','empty','No sources yet: upload a Parquet file or add an S3 folder of Parquet files.'));
 D.sources.forEach(id=>{const sp=D.source_specs?.[id]||{},r=qr[id],c=card(id,`sources/${id}.json`),k=h('div','kv');
  [['location',sp.location||'—'],['format',sp.format||'from the file'],['refresh',sp.refresh||(r?r[ix('refresh')]:'—')],['rows',r?fmt(r[ix('rows')]):'—'],['last load',r?cell(r[ix('last_load')]):'—']]
   .forEach(([a,b])=>k.append(h('b','',a),h('span','',String(b))));c.append(k);
  const p=r?(r[ix('problems')]||[]):[];if(p.length)c.append(h('div','bad',p.join('; ')));
  act(c,'Reload '+id,async()=>{await api('refresh',{table:id});return 'Reloaded: a new snapshot.'});act(c,'Delete',async()=>{await api('delete_source',{source:id});return 'Deleted.'});g.append(c)})}
// New staged table: a source, steps picked from a menu (each a small JSON line, filled with the source's columns), a
// preview of the rows, then set_steps and hidden (staged tables stay out of the model); or the cleaning in words, to the agent
const STEP_KINDS=[['select','Keep columns',c=>({columns:c.slice(0,3)})],['rename','Rename columns',c=>({columns:{[c[0]]:c[0]+'_new'}})],
 ['set_type','Change types',c=>({columns:{[c[0]]:'VARCHAR'}})],['filter','Filter rows',c=>({condition:`${c[0]} IS NOT NULL`})],
 ['deduplicate','Remove duplicates',c=>({columns:[c[0]]})],['add_column','Add a calculated column',c=>({name:'new_column',expression:`upper(${c[0]})`})],
 ['fill_nulls','Fill empty values',c=>({columns:{[c[0]]:''}})],['append','Append another source',()=>({tables:[D.sources[1]||D.sources[0]]})],
 ['merge','Join another source',c=>({table:D.sources[1]||D.sources[0],on:{[c[0]]:c[0]},columns:[c[0]],how:'left'})],['sql','SQL over the rows',()=>({query:'SELECT * FROM input'})]];
function newStaged(box){if(box.querySelector('.stform'))return;const c=card('New staged table','a source, cleaned by steps; hidden from the model');c.classList.add('stform','wide');
 const src=h('select','f'),nm=h('input','f'),list=h('div','stlist'),add=h('select','f'),out=h('div'),how=h('textarea','f');D.sources.forEach(x=>src.append(new Option(x,x)));
 nm.placeholder='e.g. silver_orders';const name=()=>nm.value.trim()||'silver_'+src.value;let cols=[];
 const rowOf=(type,fields)=>{const r=h('div','row strow'),i=h('input','f code');i.value=JSON.stringify(fields);r.dataset.type=type;const x=h('button','btn','✕');x.title='Remove this step';x.onclick=()=>r.remove();
  r.append(h('b','',(STEP_KINDS.find(k=>k[0]===type)||[0,type])[1]),i,x);list.append(r)};
 const steps=()=>[{type:'source',source:src.value},...[...list.children].map(r=>{try{return {type:r.dataset.type,...JSON.parse(r.querySelector('input').value)}}catch(e){throw new Error(`Step "${r.dataset.type}": not valid JSON (${e.message})`)}})];
 const look=async()=>{const r=await api('preview_steps',{steps:steps()});cols=(r.columns||[]).map(x=>x.name||x);rows(out,{columns:cols,rows:(r.rows||[]).slice(0,20)});return r};
 add.append(new Option('+ Add a step…',''));STEP_KINDS.forEach(([t,l])=>add.append(new Option(l,t)));
 add.onchange=async()=>{const k=STEP_KINDS.find(x=>x[0]===add.value);add.value='';if(!k)return;if(!cols.length)try{await look()}catch(e){}rowOf(k[0],k[2](cols.length?cols:['column']))};
 src.onchange=()=>{cols=[];out.innerHTML='';look().catch(()=>{})};how.rows=2;how.placeholder='or in words: e.g. keep the US rows, make trending_date a date, drop duplicates by video and day';
 c.append(h('label','l','Source'),src,h('label','l','Staged table name'),nm,h('label','l','Steps'),list,add);
 act(c,'Preview',async()=>{await look();return `${cols.length} columns; the first rows are below.`});
 act(c,'Create',async()=>{await api('set_steps',{table:name(),steps:steps()});await api('set_table',{table:name(),hidden:true});return `Created ${name()}: staged and hidden from the model. It appears in this list in a few seconds.`},1);
 c.append(h('label','l','Or ask the agent'),how);act(c,'Ask the agent instead',async()=>{openChat(true);inp.value=`Stage the source ${src.value} as a hidden silver table ${name()}: ${how.value.trim()||'clean it for the model'}`;sendChat();return 'Asked: follow it in the chat.'});
 c.append(out);(box.querySelector('.pghead')||box).after(c);look().catch(()=>{})}
function stagedPage(box){const sv=(D.layers||[]).filter(t=>t.layer==='silver').sort((a,b)=>a.table.localeCompare(b.table));
 const bar=h('div','row bar top');bar.append(btn('New staged table',()=>newStaged(box),1));box.append(bar);
 const g=h('div','cards');box.append(g);if(!sv.length){box.append(h('p','empty','Nothing staged yet: New staged table cleans a source here, or ask the agent.'));return}
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
 Object.entries(T).forEach(([t,v])=>{const c=card(t);c.querySelector('h3').append(pinBtn({kind:'table',ref:t,title:t}));const kind=h('select','f ed');['fact','dimension'].forEach(x=>kind.append(new Option(x,x)));kind.value=v.kind||'fact';
  const key=h('input','f ed');key.value=(v.key||[]).join(', ');key.placeholder='key columns';const de=h('input','f ed');de.value=v.description||'';
  const hid=h('input','ed');hid.type='checkbox';hid.checked=!!v.hidden;const hl=h('label','l');hl.append(hid,document.createTextNode('  hidden from the model (staging)'));
  const cols=Object.entries(D.columns?.[t]||{});const cl=h('div');cols.slice(0,40).forEach(([n,ty])=>cl.append(h('span','chip',`${n} · ${ty}`)));
  c.append(h('label','l','Kind'),kind,h('label','l','Key'),key,h('label','l','Description'),de,hl,h('label','l',`Columns (${cols.length})`),cl);
  act(c,'Save',async()=>{await api('set_table',{table:t,kind:kind.value,key:key.value?key.value.split(',').map(x=>x.trim()).filter(Boolean):null,description:de.value||null,hidden:hid.checked});return 'Saved.'},1);
  let ed=null;act(c,'Edit steps',()=>{if(ed){ed.remove();ed=null;return}ed=stepsEd(t,(D.table_steps||{})[t],false);c.after(ed)});
  act(c,'Delete',async()=>{await api('delete_table',{table:t});return 'Deleted.'});g.append(c)})}

// ---------- the model canvas: drag tables, drag a column onto another table's column to join, click a join to remove it ----------
// named models (ABI's model/models.json): the Data models page lists them like Reports; each opens its canvas.
// Measures has a picker; the choice narrows it.
let NM='';try{NM=localStorage.getItem('abi-nm-'+D.project)||''}catch(e){}
const nmOf=()=>(D.models||[]).find(m=>m.id===NM),nmTable=t=>{const m=nmOf();return !m||(m.tables||[]).includes(t)||t===D.model?.date_table?.table};
const nmMeasure=x=>{const m=nmOf();if(!m)return true;if(m.measures?.length)return m.measures.includes(x.id);
 return [...String(x.expression||'').matchAll(/\b([a-z][a-z0-9_]*)\.[a-z_]/gi)].every(r=>nmTable(r[1]))};
const pickNM=id=>{NM=id;try{localStorage.setItem('abi-nm-'+D.project,id)}catch(e){}};
function newModelForm(box){if(box.querySelector('.nmform'))return;
 const c=card('New model','a subset of the tables for one audience; joins and measures stay shared'),nm=h('input','f'),ds=h('input','f'),ck=h('div','checks');c.classList.add('nmform','wide');
 nm.placeholder='e.g. Marketing';ds.placeholder='who it is for (optional)';Object.keys(D.model?.tables||{}).filter(t=>t!==D.model?.date_table?.table).forEach(t=>{const l=h('label','chk'),i=h('input');i.type='checkbox';i.value=t;l.append(i,document.createTextNode(t));ck.append(l)});
 c.append(h('label','l','Name'),nm,h('label','l','Description'),ds,h('label','l','Tables'),ck);
 act(c,'Create',async()=>{const tables=[...ck.querySelectorAll('input:checked')].map(i=>i.value),id=nm.value.trim().toLowerCase().replace(/[^a-z0-9_-]+/g,'_').replace(/^[^a-z]+/,'');
  if(!id||!tables.length)throw new Error('Give it a name and pick at least one table.');
  await api('save_model',{model:{id,name:nm.value.trim(),description:ds.value.trim()||null,tables}});D.models=[...(D.models||[]).filter(m=>m.id!==id),{id,name:nm.value.trim(),description:ds.value.trim(),tables}];sidebar();show('mdl:'+id)},1);
 (box.querySelector('.pghead')||box).after(c)}
function modelsPage(box){const bar=h('div','row bar top');bar.append(btn('New model',()=>newModelForm(box),1));box.append(bar);
 const g=h('div','cards stack');box.append(g);const T=D.model?.tables||{},R=D.model?.relationships||[];
 const row=(key,name,desc,tables)=>{const c=card(name),n=tables.length,j=R.filter(r=>tables.includes(r.from.table)&&tables.includes(r.to.table)).length;
  c.append(h('p','',desc||''),h('p','file',`${n} table${n===1?'':'s'} · ${j} join${j===1?'':'s'}: ${tables.join(', ')}`));act(c,'Open',async()=>{show(key)},1);g.append(c);return c};
 row('mdl:all','Whole model','Every model table and join of the project.',Object.keys(T));
 (D.models||[]).forEach(m=>{const date=D.model?.date_table?.table,c=row('mdl:'+m.id,m.name,m.description,[...m.tables,...(date&&!m.tables.includes(date)?[date]:[])]);
  if(MODE==='developer')act(c,'Delete',async()=>{await api('delete_model',{id:m.id});D.models=D.models.filter(x=>x.id!==m.id);if(NM===m.id)pickNM('');sidebar();show('model')})})}
function modelBar(box){if(!(D.models||[]).length)return;const bar=h('div','row bar lead seg');
 [['','All tables'],...(D.models||[]).map(m=>[m.id,m.name])].forEach(([id,name])=>{const b=h('button','chip'+(NM===id?' on':''),name);b.onclick=()=>{pickNM(id);show(CUR)};bar.append(b)});box.append(bar)}
function openModel(box,id){pickNM(id);const bar=h('div','row bar lead');bar.append(btn('← Data models',()=>show('model')));box.append(bar);modelCanvas(box)}
// a canvas with zoom (−, +, Fit, Ctrl/⌘ + wheel) and Auto arrange: a scroll viewport > sizer > stage (scaled)
function canvasKit(box,onArrange){const tools=h('div','row cvtools'),wrap=h('div','canvas'),sizer=h('div','sizer'),stage=h('div','stage'),svg=document.createElementNS(NS,'svg');
 svg.classList.add('links');stage.append(svg);sizer.append(stage);wrap.append(sizer);const kit={z:1,w:0,ht:0,wrap,stage,svg,draw:()=>{}};
 const pct=h('span','pct','100%'),size=()=>{sizer.style.width=kit.w*kit.z+'px';sizer.style.height=kit.ht*kit.z+'px';stage.style.width=kit.w+'px';stage.style.height=kit.ht+'px';
  stage.style.transform=`scale(${kit.z})`;wrap.style.height=Math.max(320,Math.min(kit.ht*kit.z+24,innerHeight*.78))+'px';pct.textContent=Math.round(kit.z*100)+'%'};
 kit.setZ=z=>{kit.z=Math.min(2,Math.max(.25,Math.round(z*100)/100));size();kit.draw()};kit.extent=(w,ht)=>{kit.w=w;kit.ht=ht;size()};
 const b=(t,title,fn)=>{const x=h('button','btn',t);x.title=title;x.onclick=fn;return x};
 tools.append(b('−','Zoom out',()=>kit.setZ(kit.z-.1)),pct,b('+','Zoom in',()=>kit.setZ(kit.z+.1)),b('Fit','Fit to the window',()=>kit.setZ(Math.min(1,(wrap.clientWidth-24)/Math.max(1,kit.w),(innerHeight*.78)/Math.max(1,kit.ht)))),
  b('Auto arrange','Lay the boxes out again',onArrange));
 wrap.addEventListener('wheel',e=>{if(!(e.ctrlKey||e.metaKey))return;e.preventDefault();kit.setZ(kit.z*(e.deltaY<0?1.1:.9))},{passive:false});
 // a point of the stage, in stage units, from a screen point
 kit.local=(x,y)=>{const r=stage.getBoundingClientRect();return[(x-r.left)/kit.z,(y-r.top)/kit.z]};
 box.append(tools,wrap);return kit}
function modelCanvas(box){const T=D.model?.tables||{},R=(D.model?.relationships||[]).filter(r=>nmTable(r.from.table)&&nmTable(r.to.table)),names=Object.keys(T).filter(nmTable);
 if(!names.length){box.append(h('p','empty','No tables yet: add a source first.'));return}
 const help=h('p','file',MODE==='viewer'?'Facts are filled black, dimensions outlined. Hover a join to see its columns.'
  :'Drag a table by its title. Drag from a column\'s dot onto the matching column of another table to join them (many rows to one). Click a join to remove it. Click a table\'s badge to switch fact or dimension.');
 box.append(help);const key='abi-pos2-'+D.project+'-'+NM;let pos={};try{pos=JSON.parse(localStorage.getItem(key)||'{}')}catch(e){}
 const save=()=>{try{localStorage.setItem(key,JSON.stringify(pos))}catch(e){}};
 const facts=names.filter(t=>T[t].kind==='fact');
 // columns by depth along the joins: facts, the dimensions they join, the ones those join (no loops back)
 const hgt=t=>46+Object.keys(D.columns?.[t]||{}).length*27,depth={};facts.forEach(t=>depth[t]=0);
 for(let i=0;i<names.length;i++)R.forEach(r=>{const a=r.from.table,b=r.to.table;if(depth[a]!==undefined&&(depth[b]===undefined||depth[b]<depth[a]+1)&&depth[a]<names.length)depth[b]=depth[a]+1});
 const layout=()=>{const cols={};names.forEach(t=>(cols[depth[t]??1]??=[]).push(t));Object.entries(cols).forEach(([d,list])=>{let y=24;list.forEach(t=>{if(!pos[t])pos[t]={x:24+d*330,y};y=Math.max(y,pos[t].y)+hgt(t)+32})})};
 const extent=()=>kit.extent(Math.max(...names.map(t=>pos[t].x+290)),Math.max(...names.map(t=>pos[t].y+hgt(t)+24)));
 const kit=canvasKit(box,()=>{pos={};layout();names.forEach(t=>{boxes[t].style.left=pos[t].x+'px';boxes[t].style.top=pos[t].y+'px'});save();extent();draw()});
 const {stage,svg}=kit;layout();const boxes={};
 names.forEach(t=>{const b=h('div','tbl '+(T[t].kind==='fact'?'fact':'dim'));b.style.left=pos[t].x+'px';b.style.top=pos[t].y+'px';
  const hd=h('div','th'),badge=h('button','badge',T[t].kind||'table');hd.append(h('span','',t),badge);b.append(hd);
  const cols=h('div','cols');Object.entries(D.columns?.[t]||{}).forEach(([c,ty])=>{const r=h('div','col');r.dataset.t=t;r.dataset.c=c;
   const dot=h('span','dot w');dot.title='Drag onto another table\'s column to join';r.append(dot,h('span','',c),h('span','ty',String(ty).toLowerCase()));cols.append(r)});
  b.append(cols);stage.append(b);boxes[t]=b;
  badge.onclick=async()=>{if(MODE==='viewer')return;try{await api('set_table',{table:t,kind:T[t].kind==='fact'?'dimension':'fact'});toast('Saved')}catch(e){toast(e.message)}};
  hd.onpointerdown=e=>{if(e.target===badge)return;e.preventDefault();const [sx,sy]=kit.local(e.clientX,e.clientY),ox=pos[t].x,oy=pos[t].y;hd.setPointerCapture(e.pointerId);
   hd.onpointermove=m=>{const [x,y]=kit.local(m.clientX,m.clientY);pos[t]={x:Math.max(0,ox+x-sx),y:Math.max(0,oy+y-sy)};b.style.left=pos[t].x+'px';b.style.top=pos[t].y+'px';extent();draw()};
   hd.onpointerup=()=>{hd.onpointermove=null;save()}}});
 const at=(t,c,side)=>{const row=boxes[t]?.querySelector(`.col[data-c="${CSS.escape(c)}"]`)||boxes[t];if(!row)return[0,0];
  const r=row.getBoundingClientRect(),[x,y]=kit.local(side==='r'?r.right:r.left,r.top+r.height/2);return[x,y]};
 let temp=null;
 function draw(){svg.innerHTML='';svg.setAttribute('width',kit.w);svg.setAttribute('height',kit.ht);
  R.forEach(rel=>{const f=rel.from,to=rel.to;if(!boxes[f.table]||!boxes[to.table])return;
   const L=pos[f.table].x<pos[to.table].x;const [x1,y1]=at(f.table,f.columns[0],L?'r':'l'),[x2,y2]=at(to.table,to.columns[0],L?'l':'r');
   const dx=Math.max(40,Math.abs(x2-x1)/2)*(L?1:-1),d=`M${x1},${y1} C${x1+dx},${y1} ${x2-dx},${y2} ${x2},${y2}`;
   const hit=el('path',{d,class:'hit'},svg),ln=el('path',{d,class:'ln'},svg);el('circle',{cx:x2,cy:y2,r:4,class:'one'},svg);
   const tt=document.createElementNS(NS,'title');tt.textContent=`${f.table}.${f.columns.join(',')} → ${to.table}.${to.columns.join(',')} (many to one)`;hit.append(tt);
   hit.onclick=async()=>{if(MODE==='viewer')return;try{await api('remove_relationship',{id:rel.id});toast('Join removed')}catch(e){toast(e.message)}}});
  if(temp)el('path',{d:temp,class:'ln temp'},svg)}
 kit.draw=draw;extent();
 stage.addEventListener('pointerdown',e=>{const dot=e.target.closest('.dot');if(!dot||MODE==='viewer')return;e.preventDefault();const from=dot.parentNode,[x1,y1]=at(from.dataset.t,from.dataset.c,'r');
  const mv=m=>{const [x,y]=kit.local(m.clientX,m.clientY);temp=`M${x1},${y1} L${x},${y}`;draw()};
  const up=async m=>{window.removeEventListener('pointermove',mv);window.removeEventListener('pointerup',up);temp=null;draw();
   const tgt=document.elementFromPoint(m.clientX,m.clientY)?.closest('.col');if(!tgt||tgt.dataset.t===from.dataset.t)return;
   const a=`${from.dataset.t}.${from.dataset.c}`,b=`${tgt.dataset.t}.${tgt.dataset.c}`;
   if(!await ask({message:`Join ${a} to ${b}?`,preview:`Many rows of ${from.dataset.t} to one row of ${tgt.dataset.t}. ${tgt.dataset.t} becomes a dimension keyed on ${tgt.dataset.c}.`}))return;
   try{await api('add_relationship',{from:a,to:b});toast('Joined')}catch(e){toast(e.message)}};
  window.addEventListener('pointermove',mv);window.addEventListener('pointerup',up)});
 requestAnimationFrame(draw);new ResizeObserver(()=>draw()).observe(kit.wrap)}
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
function editBtns(c,kind,obj){const K=KIND[kind];let ed=null;c.querySelector('h3')?.append(pinBtn({kind,ref:obj.id,title:obj.name||obj.id}));
 act(c,'Edit',()=>{if(ed){ed.remove();ed=null;return}ed=editor(kind,obj,false);c.after(ed);ed.scrollIntoView({behavior:'smooth',block:'nearest'})});
 (K.extra||[]).forEach(([l,f])=>act(c,l,async()=>{await f(obj);return `${l}ed.`}));
 act(c,'Delete',async()=>{await K.del(obj);return 'Deleted.'})}
// ---------- New flow / ML model / tool / agent as a form: pick what you want, it saves through ABI (checked like an import) ----------
const slug=(n,k)=>{const x=String(n||'').toLowerCase().replace(/[^a-z0-9]+/g,'_').replace(/^_+|_+$/g,'');return /^[a-z]/.test(x)?x.slice(0,60):k+'_'+(x||'1')};
const sel=(opts,v)=>{const e=h('select','f');opts.forEach(o=>{const [val,lab]=Array.isArray(o)?o:[o,o];e.append(new Option(lab,val))});if(v!==undefined)e.value=v;return e};
const inpt=(ph,v,type)=>{const e=h('input','f');e.placeholder=ph||'';if(v!==undefined)e.value=v;if(type)e.type=type;return e};
const lab=t=>h('label','l',t),MS=()=>D.measures.map(m=>[m.id,m.name||m.id]),CMPS=[['>','is above'],['>=','is at least'],['<','is below'],['<=','is at most'],['=','equals'],['!=','is not']];
function flowForm(c){const nm=inpt('e.g. Morning refresh'),de=inpt('what it is for'),tr=sel([['schedule','On a schedule'],['after_refresh','After a table refreshes'],['data_quality','When data quality finds a problem'],['data_condition','When a measure crosses a value'],['manual','Only when run by hand']]),tb=h('div');
 const draw=()=>{tb.innerHTML='';const v=tr.value;
  if(v==='schedule'){const fq=sel([['d','Every day'],['w','Every week on'],['m','Every month on day']]),dw=sel([['1','Monday'],['2','Tuesday'],['3','Wednesday'],['4','Thursday'],['5','Friday'],['6','Saturday'],['0','Sunday']]),dm=inpt('1','1','number'),tm=inpt('','07:00','time'),tz=inpt('time zone',Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC');
   const vis=()=>{dw.hidden=fq.value!=='w';dm.hidden=fq.value!=='m'};fq.onchange=vis;vis();const r=h('div','row');r.append(fq,dw,dm,lab('at'),tm,tz);tb.append(r);
   tb.get=()=>{const [H,M]=(tm.value||'07:00').split(':').map(Number);return {type:'schedule',cron:`${M} ${H} ${fq.value==='m'?Math.min(28,Math.max(1,+dm.value||1)):'*'} * ${fq.value==='w'?dw.value:'*'}`,timezone:tz.value||'UTC'}}}
  else if(v==='after_refresh'){const t=multi([...new Set([...D.sources,...D.tables])],[]);tb.append(lab('Tables (any of them)'),t);tb.get=()=>{const x=[...t.selectedOptions].map(o=>o.value);if(!x.length)throw new Error('Pick at least one table.');return {type:'after_refresh',tables:x}}}
  else if(v==='data_quality')tb.get=()=>({type:'data_quality'});
  else if(v==='data_condition'){const m=sel(MS()),op=sel(CMPS),val=inpt('value','','number');const r=h('div','row');r.append(m,op,val);tb.append(r);
   tb.get=()=>{if(val.value==='')throw new Error('Give the value.');return {type:'data_condition',query:{measures:[m.value]},measure:m.value,op:op.value,value:+val.value}}}
  else tb.get=()=>({type:'manual'})};tr.onchange=draw;draw();
 // steps, run one after the other
 const KINDS=[['refresh','Refresh a table'],['score','Score an ML model'],['train','Train an ML model'],['tool','Run a tool'],['agent','Ask an agent'],['snapshot','Export a report (PDF)'],['post','Leave a message in the app inbox'],['approval','Wait for someone\'s approval']];
 const list=h('div','stack'),add=sel([['','+ Add a step…'],...KINDS]);
 add.onchange=()=>{const k=add.value;add.value='';if(!k)return;const row=h('div','row'),x=h('button','btn','Remove');row.append(h('b','',KINDS.find(z=>z[0]===k)[1]));let get;
  if(k==='refresh'){const t=sel([...new Set([...D.sources,...D.tables])]);row.append(t);get=()=>({type:'refresh',table:t.value})}
  else if(k==='score'||k==='train'){const m=sel((D.ml||[]).map(x=>[x.id,x.name||x.id]));row.append(m);get=()=>{if(!m.value)throw new Error('There is no ML model yet.');return {type:k,model:m.value}}}
  else if(k==='tool'){const t=sel((D.tools||[]).map(x=>[x.id,x.name||x.id])),i=inpt('inputs as JSON, e.g. {"store_id":"S001"}');row.append(t,i);get=()=>{if(!t.value)throw new Error('There is no tool yet.');return {type:'tool',tool:t.value,...(i.value?{inputs:JSON.parse(i.value)}:{})}}}
  else if(k==='agent'){const a=sel((D.agents||[]).map(x=>[x.id,x.name||x.id])),q=inpt('what to ask it');row.append(a,q);get=()=>{if(!a.value||!q.value)throw new Error('Pick an agent and say what to ask.');return {type:'agent',agent:a.value,prompt:q.value}}}
  else if(k==='snapshot'){const r=sel(D.reports.map(x=>[x.id,x.name||x.id]));row.append(r);get=()=>{if(!r.value)throw new Error('There is no report yet.');return {type:'snapshot',report:r.value,pages:'all',format:'pdf'}}}
  else if(k==='post'){const t=inpt('the message');row.append(t);get=()=>{if(!t.value)throw new Error('Write the message.');return {type:'action',action:'post_message',input:{channel:'inbox',text:t.value}}}}
  else{const t=inpt('what they approve');row.append(t);get=()=>{if(!t.value)throw new Error('Say what they approve.');return {type:'approval',message:t.value}}}
  row.get=get;x.onclick=()=>row.remove();row.append(x);list.append(row)};
 c.append(lab('Name'),nm,lab('Description'),de,lab('Starts'),tr,tb,lab('Steps, in order'),list,add);
 return ()=>{if(!nm.value.trim())throw new Error('Give it a name.');const st=[...list.children].map((r,i)=>({id:`step_${i+1}`,...r.get()}));if(!st.length)throw new Error('Add at least one step.');
  return {$schema:'https://schema.abi.invalid/v1/flow.json',id:slug(nm.value,'flow'),name:nm.value.trim(),description:de.value,enabled:false,trigger:tb.get(),steps:st}}}
function mlForm(c){const nm=inpt('e.g. Daily revenue forecast'),task=sel([['forecast','Forecast a measure'],['regression','Predict a number'],['classification','Predict a category'],['clustering','Group similar rows'],['anomaly','Find unusual days']]),ds=sel([['','A new query from these fields'],...(D.datasets||[]).map(d=>[d.id,d.name||d.id])]),f=h('div');
 const meas=sel(MS()),time=sel(D.dims.filter(d=>d.startsWith('calendar.')),'calendar.day'),hz=inpt('days ahead','28','number'),feats=multi(D.measures.map(m=>m.id),[]),k=inpt('groups (blank: automatic)','','number'),meth=sel(['auto','lightgbm','ets','seasonal','random_forest','kmeans','gmm','isolation_forest']);
 const draw=()=>{f.innerHTML='';const t=task.value;
  if(t==='forecast')f.append(lab('Measure to forecast'),meas,lab('By'),time,lab('Horizon (periods ahead)'),hz);
  else if(t==='anomaly')f.append(lab('Measures to watch'),feats,lab('By'),time);
  else if(t==='clustering')f.append(lab('Measures to group by'),feats,lab('Number of groups'),k);
  else f.append(lab('Target measure'),meas,lab('Learn from'),feats);
  f.append(lab('Method'),meth)};task.onchange=draw;draw();
 c.append(lab('Name'),nm,lab('Task'),task,lab('Training rows'),ds,f);
 return async()=>{if(!nm.value.trim())throw new Error('Give it a name.');const id=slug(nm.value,'ml'),t=task.value,fs=[...feats.selectedOptions].map(o=>o.value);let dataset=ds.value;
  if(!dataset){const q={measures:[...new Set([...(t==='forecast'||t==='regression'||t==='classification'?[meas.value]:[]),...fs])],dimensions:t==='forecast'||t==='anomaly'?[time.value]:[]};
   if(!q.measures.length)throw new Error('Pick the measures.');dataset=id+'_rows';await api('save_dataset',{id:dataset,name:nm.value.trim()+' (training rows)',query:q})}
  return {$schema:'https://schema.abi.invalid/v1/ml.json',id,name:nm.value.trim(),task:t,dataset,method:meth.value,...(t==='forecast'?{target:meas.value,time:time.value,horizon:+hz.value||28}:{}),
   ...(t==='anomaly'?{time:time.value,features:fs}:{}),...(t==='clustering'?{features:fs,...(k.value?{k:+k.value}:{})}:{}),...(t==='regression'||t==='classification'?{target:meas.value,...(fs.length?{features:fs}:{})}:{})}}}
function toolForm(c){const nm=inpt('e.g. Store KPIs for a month'),de=inpt('what it returns, for the person and the agents'),ms=multi(D.measures.map(m=>m.id),[]),by=multi(ALLCOLS(),[]),ins=h('div','stack'),lim=inpt('all rows','','number');
 const add=sel([['','+ Ask the user for…'],...D.dims.map(d=>[d,d])]);add.onchange=()=>{const col=add.value;add.value='';if(!col)return;const r=h('div','row'),ty=sel([['string','text'],['integer','whole number'],['number','number']]),lo=inpt('from','0','number'),hi=inpt('to','1000000','number'),x=h('button','btn','Remove');
  const vis=()=>{lo.hidden=hi.hidden=ty.value==='string'};ty.onchange=vis;vis();r.append(h('b','',col),ty,lo,hi,x);x.onclick=()=>r.remove();  // ABI bounds every input
  r.get=()=>({col,name:col.split('.').pop(),spec:{type:ty.value,description:col,...(ty.value==='string'?{minLength:1,maxLength:200}:{minimum:+lo.value,maximum:+hi.value})}});ins.append(r)};
 c.append(lab('Name'),nm,lab('Description'),de,lab('Measures it returns'),ms,lab('Grouped by'),by,lab('Inputs: the rows are filtered to what the user gives'),ins,add,lab('At most this many rows'),lim);
 return ()=>{if(!nm.value.trim()||!de.value.trim())throw new Error('Give it a name and a description.');const m=[...ms.selectedOptions].map(o=>o.value);if(!m.length)throw new Error('Pick at least one measure.');const I=[...ins.children].map(r=>r.get());
  return {$schema:'https://schema.abi.invalid/v1/tool.json',id:slug(nm.value,'tool'),name:nm.value.trim(),description:de.value.trim(),kind:'query',
   inputs:{type:'object',additionalProperties:false,properties:Object.fromEntries(I.map(i=>[i.name,i.spec])),required:I.map(i=>i.name)},
   query:{measures:m,dimensions:[...by.selectedOptions].map(o=>o.value),filters:I.map(i=>({column:i.col,op:'=',value:{$input:i.name}})),...(lim.value?{limit:+lim.value}:{})},
   outputs:{type:'array',items:{type:'object'}},permissions:{role:'viewer'},tests:[]}}}
// what a custom agent may call (ABI: its project's tools, two reads, four actions, other published agents)
const AGENT_TOOLS=[['semantic_query','Query the measures in its scope (read)'],['report_snapshot','Render a report as PDF or PNG (read)'],
 ['send_email','Send an email (sends)'],['post_message','Post a message: app inbox, Slack or Teams (sends)'],['refresh','Reload a table from its source (changes data)'],
 ['propose_edit','Propose a change to an edit table; a person approves it (changes data)']];
const lmulti=(opts,on)=>{const m=h('select','f');m.multiple=true;m.size=Math.min(8,Math.max(3,opts.length));opts.forEach(([v,l])=>{const o=new Option(l,v);o.selected=(on||[]).includes(v);m.append(o)});return m};
function caseRow(x){const r=h('div','row'),q=inpt('a question a user would ask',x?.question),e=inpt('the answer must contain (comma separated)',(x?.expect?.contains||[]).join(', ')),
 rm=h('button','btn','Remove');q.style.flex='3 1 320px';e.style.flex='2 1 220px';r.style.flexWrap='nowrap';r.append(q,e,rm);rm.onclick=()=>r.remove();r.get=()=>q.value.trim()?{question:q.value.trim(),expect:{...(x?.expect||{}),contains:e.value.split(',').map(s=>s.trim()).filter(Boolean)}}:null;return r}
function evalSet(c,a){if(c.nextElementSibling?.classList.contains('evalset')){c.nextElementSibling.remove();return}
 const e=card(`Evaluation set · ${a.name||a.id}`,'questions it must answer correctly before it can be published; ABI reruns them when its model or tools change'),cs=h('div','stack');e.classList.add('evalset','wide');
 (D.agent_evals?.[a.id]||[]).forEach(x=>cs.append(caseRow(x)));if(!cs.children.length)cs.append(caseRow());
 e.append(cs,btn('+ Test question',()=>cs.append(caseRow())));
 act(e,'Save evaluation set',async()=>{const cases=[...cs.children].map(r=>r.get()).filter(Boolean);if(!cases.length)throw new Error('Add at least one question.');
  await KIND.agent.save({...clean(a),cases});(D.agent_evals||={})[a.id]=cases;return 'Saved through ABI. Run it to see which pass.'},1);
 act(e,'Run evaluation set',async()=>{const r=await api('test_agent',{agent:a.id});return (r.cases||[]).map(x=>`${x.passed?'✓':'✗'} ${x.question}${x.missing?.length?' · missing '+x.missing.join(', '):''}`).join('\n')});
 c.after(e)}
function agentForm(c){const nm=inpt('e.g. Store manager assistant'),pu=inpt('who it is for and what it answers'),ins=h('textarea','f'),
 tl=lmulti([...AGENT_TOOLS,...(D.tools||[]).map(t=>[t.id,`${t.name||t.id} (your tool)`])],['semantic_query']),
 ask=lmulti((D.agents||[]).filter(x=>x.status==='published').map(x=>[x.id,x.name||x.id]),[]),sc=multi(D.measures.map(m=>m.id),[]),st=multi(D.tables||[],[]),mo=sel([['tools_only','Answers only through its tools'],['exploratory','May also explore the measures in scope']]),cs=h('div','stack');
 ins.rows=3;ins.placeholder='how it answers: tone, what to refuse, what to ask for';const addc=btn('+ Test question',()=>cs.append(caseRow()));cs.append(caseRow());
 if(!ask.options.length){ask.append(new Option('No published agents yet',''));ask.disabled=true;ask.size=1}
 c.append(lab('Name'),nm,lab('Purpose'),pu,lab('Instructions'),ins,lab('What it may do (sends and changes never run during its evaluation; a tool brings its measures and tables into the scope)'),tl,lab('Agents it may ask (published ones)'),ask,lab('Measures in its scope'),sc,lab('Tables in its scope (for grouping and filters)'),st,lab('Mode'),mo,lab('Evaluation: questions it must answer correctly before it can be published'),cs,addc);
 return ()=>{if(!nm.value.trim()||!pu.value.trim()||!ins.value.trim())throw new Error('Give it a name, a purpose and instructions.');
  return {$schema:'https://schema.abi.invalid/v1/agent.json',id:slug(nm.value,'agent'),name:nm.value.trim(),purpose:pu.value.trim(),instructions:ins.value.trim(),mode:mo.value,grounding:true,channels:[{type:'in_app'}],
   scope:(()=>{const pick=[...tl.selectedOptions].map(o=>(D.tools||[]).find(t=>t.id===o.value)?.query).filter(Boolean),  // a chosen tool brings what it reads
     cols=pick.flatMap(q=>[...(q.dimensions||[]),...(q.filters||[]).map(f=>f.column),...(q.order_by||[]).map(o=>o.field)]).filter(x=>String(x).includes('.'));
     return {measures:[...new Set([...[...sc.selectedOptions].map(o=>o.value),...pick.flatMap(q=>q.measures||[])])],
      tables:[...new Set([...[...st.selectedOptions].map(o=>o.value),...cols.map(x=>x.split('.')[0])])]}})(),tools:[...tl.selectedOptions].map(o=>o.value),
   ...(ask.selectedOptions.length?{agents:[...ask.selectedOptions].map(o=>o.value)}:{}),cases:[...cs.children].map(r=>r.get()).filter(Boolean)}}}
const FORMS={flow:[flowForm,'flows'],ml:[mlForm,'ml'],tool:[toolForm,'tools'],agent:[agentForm,'agents']};
function newBtn(box,kind){const bar=box.querySelector(':scope>.bar.top')||h('div','row bar top'),L=KIND[kind].label;
 bar.append(btn(`New ${L}`,()=>{if(box.querySelector('.newkind'))return;const c=card(`New ${L}`,'fill it in and create it; it is checked by ABI like everything else');c.classList.add('newkind','wide');
  const get=FORMS[kind][0](c);
  act(c,`Create ${L}`,async()=>{const o=await get();await KIND[kind].save(o);toast(`Created ${o.name}. `+(kind==='flow'?'It is off until you switch it on.':kind==='ml'?'Train it on its card.':kind==='tool'?'Test it, then publish it.':'Run its evaluation set on its card.'));show(FORMS[kind][1])},1);
  act(c,'Describe it to the agent instead',()=>{openChat(true);inp.value=`Create a ${L} in this project: `;inp.focus()});
  act(c,'Start from JSON',()=>{const e=editor(kind,KIND[kind].tpl,true);c.after(e);e.querySelector('textarea').focus()});
  act(c,'Cancel',()=>c.remove());
  (box.querySelector('.pghead')||bar).after(c);c.querySelector('input')?.focus()},1));
 if(!bar.parentNode)box.prepend(bar)}

// ---------- Automate ----------
function flowPage(box,f){const bar=h('div','row bar lead');bar.append(btn('← Flows',()=>show('flows')));box.append(bar);
 const steps=f.steps||[],t=f.trigger||{type:'manual'},T0='__trigger';
 const deps={};steps.forEach((x,i)=>deps[x.id]=x.after===undefined?[i?steps[i-1].id:T0]:(x.after.length?x.after:[T0]));
 const depth={[T0]:0};steps.forEach(x=>depth[x.id]=1+Math.max(...deps[x.id].map(d=>depth[d]??0)));
 const what=x=>x.table||x.tool||x.agent||x.action||x.model||x.report||(x.measure?`${x.measure} ${x.op} ${x.value}`:'')||(x.changed?'data changed':'')||(x.message||'').slice(0,40);
 const nodes=[{id:T0,title:'Trigger',sub:t.type+(t.cron?` · ${t.cron}${t.timezone?' '+t.timezone:''}`:'')+(t.tables?' · '+t.tables.join(', '):'')+(t.measure?` · ${t.measure} ${t.op} ${t.value}`:'')},
  ...steps.map(x=>({id:x.id,title:x.id,sub:`${x.type}${what(x)?' · '+what(x):''}`,when:x.when}))];
 const H=x=>x.when?96:72,key='abi-flow2-'+D.project+'-'+f.id;let pos={};try{pos=JSON.parse(localStorage.getItem(key)||'{}')}catch(e){}
 const save=()=>{try{localStorage.setItem(key,JSON.stringify(pos))}catch(e){}};
 // columns by depth, as many to a row as the canvas fits: a long chain wraps like lines of text
 const layout=()=>{const per=Math.max(2,Math.floor(((kit.wrap.clientWidth||1100)-24)/280)),cols={};nodes.forEach(n=>(cols[depth[n.id]]??=[]).push(n));const tall={};Object.entries(cols).forEach(([d,list])=>{const r=Math.floor(d/per);tall[r]=Math.max(tall[r]||0,list.reduce((a,n)=>a+H(n)+28,0))});
  const top=r=>24+[...Array(r).keys()].reduce((a,i)=>a+(tall[i]||0)+40,0);Object.entries(cols).forEach(([d,list])=>{let y=top(Math.floor(d/per));list.forEach(n=>{if(!pos[n.id])pos[n.id]={x:24+(d%per)*280,y};y=Math.max(y,pos[n.id].y)+H(n)+28})})};
 const extent=()=>kit.extent(Math.max(...nodes.map(n=>pos[n.id].x+260)),Math.max(...nodes.map(n=>pos[n.id].y+H(n)+24)));
 const kit=canvasKit(box,()=>{pos={};layout();nodes.forEach(n=>{els[n.id].style.left=pos[n.id].x+'px';els[n.id].style.top=pos[n.id].y+'px'});save();extent();draw()});
 const {stage,svg}=kit,els={};layout();
 nodes.forEach(n=>{const e=h('div','node'+(n.id===T0?' trig':''));e.style.left=pos[n.id].x+'px';e.style.top=pos[n.id].y+'px';e.append(h('b','',n.title),h('span','',n.sub));
  if(n.when)e.append(h('div','when',`only if ${n.when.value.replace(/[{}\s]/g,'')} ${n.when.op} ${JSON.stringify(n.when.to)}`));
  e.onpointerdown=ev=>{ev.preventDefault();const [sx,sy]=kit.local(ev.clientX,ev.clientY),ox=pos[n.id].x,oy=pos[n.id].y;e.setPointerCapture(ev.pointerId);
   e.onpointermove=m=>{const [x,y]=kit.local(m.clientX,m.clientY);pos[n.id]={x:Math.max(0,ox+x-sx),y:Math.max(0,oy+y-sy)};e.style.left=pos[n.id].x+'px';e.style.top=pos[n.id].y+'px';extent();draw()};
   e.onpointerup=()=>{e.onpointermove=null;save()}};stage.append(e);els[n.id]=e});
 const side=(id,r)=>{const b=els[id].getBoundingClientRect(),[x,y]=kit.local(r?b.right:b.left,b.top+b.height/2);return[x,y]};
 function draw(){svg.innerHTML='';svg.setAttribute('width',kit.w);svg.setAttribute('height',kit.ht);
  steps.forEach(x=>deps[x.id].forEach(d=>{if(!els[d])return;const [x1,y1]=side(d,1),[x2,y2]=side(x.id,0),dx=Math.max(30,(x2-x1)/2);
   el('path',{d:`M${x1},${y1} C${x1+dx},${y1} ${x2-dx},${y2} ${x2},${y2}`,class:'ln'},svg);el('circle',{cx:x2,cy:y2,r:3.5,class:'one'},svg)}))}
 kit.draw=draw;extent();requestAnimationFrame(draw);new ResizeObserver(()=>draw()).observe(kit.wrap);
 api('flow_runs',{flow:f.id}).then(x=>{const one=x.runs?.[0]||x;(one.steps||[]).forEach(r=>els[r.step||r.id]?.classList.add(r.status));
  if(one.status)box.querySelector('.cvtools').append(h('span','file',`last run: ${one.status}${one.started?' · '+cell(one.started):''}`))}).catch(()=>{})}
function flows(box){newBtn(box,'flow');const g=h('div','cards');box.append(g);if(!D.flows.length)box.append(h('p','empty','No flows yet: New flow, or ask the agent.'));
 D.flows.forEach(f=>{const t=f.trigger||{},c=card(f.name||f.id,f.enabled?'enabled':'disabled');
  c.append(h('div','file',`${t.type||'manual'} trigger${t.cron?' · '+t.cron:''}${t.measure?` · ${t.measure} ${t.op} ${t.value}`:''} · flows/${f.id}.json`));
  const st=h('div');(f.steps||[]).forEach(s=>st.append(h('span','chip',`${s.id}: ${s.action||s.type}`)));const tr=h('ol','trace');c.append(st,tr);
  act(c,'Run now',async m=>{tr.innerHTML='';const r=await api('run_flow',{flow:f.id});say(m,`run started: ${r.run}`);
   for(let i=0;i<150;i++){await wait(2000);const x=await api('flow_runs',{flow:f.id,run:r.run}),one=x.runs?.[0]||x;tr.innerHTML='';
    (one.steps||[]).forEach(s=>tr.append(h('li',s.status==='ok'?'ok':s.status==='failed'?'bad':'',`${s.step||s.id}: ${s.status}${s.error?' · '+s.error:''}`)));
    if(['ok','failed','stopped','rejected','waiting'].includes(one.status))return `run ${one.status}`}return 'still running; see ABI\'s flow runs'},1);
  act(c,'Open canvas',async()=>{show('flw:'+f.id)});
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
  act(c,'Evaluation set',()=>evalSet(c,a));
  act(c,'Run evaluation set',async()=>{const r=await api('test_agent',{agent:a.id});out.textContent=(r.cases||[]).map(x=>`${x.passed?'✓':'✗'} ${x.question}${x.missing?.length?' · missing '+x.missing.join(', '):''}`).join('\n')||JSON.stringify(r,null,1)});editBtns(c,'agent',a);c.append(out);g.append(c)})}

// ---------- Govern ----------
function docs(box){const d=D.docs?.projects?.[0];if(!d){box.append(h('p','empty','No documentation yet.'));return}
 (d.sections||[]).forEach(s=>{const c=card(`${s.number}. ${s.title}`);(s.items||[]).forEach(it=>{c.append(h('h4','',it.title));const t=h('table');
  (it.facts||[]).forEach(([k,v])=>{const r=t.insertRow();r.insertCell().textContent=k;r.insertCell().textContent=typeof v==='string'?v:JSON.stringify(v)});c.append(t)});box.append(c)});
 act(box.firstChild||box,'Regenerate from ABI',async()=>{D.docs=await api('docs',{});show('docs')});
 act(box.firstChild||box,'Download PDF',async()=>{const r=await fetch('/docs/pdf',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:'{}'});
  if(!r.ok)throw new Error('The PDF could not be made: '+((await r.json().catch(()=>({}))).error||r.status));const a=h('a');a.href=URL.createObjectURL(await r.blob());a.download=D.project+'-documentation.pdf';a.click();return 'Downloaded.'},1)}
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
async function loadVault(){try{VAULT=await vpost('list')}catch(e){VAULT=[]}}
function vaultPage(box){
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
 measures:'The business numbers. Edit a formula and ABI checks it before it saves; Test shows the total.',model:'Your data models: the whole model and each named model. Open one to see its tables and how they join.',
 chat:'Talk to the agent: ask a question, ask where something is, or ask for a change. When it shows you a page, the chat moves to the side and goes on.',boardpack:'Board packs: the documents, data and explainer video a review needs, made from ABI by the board-pack agent and checked before they leave. Nothing is sent from here.',documents:'A report, some of its pages or one view, as ABI renders it: PDF, PNG or PowerPoint.',onepager:'One page for the board: a report page with the headline numbers, two charts and the caveats. The agent builds it in ABI; export it here.',dataexport:'The rows behind any view, as CSV or Excel, or a query\'s rows as CSV.',video:'A silent explainer of report views: the numbers from ABI, animated, with a caption per slide, in the app\'s theme.',settings:'The agent\'s model and how hard it thinks, and the providers it may use. API keys are stored encrypted and never shown again.',people:'Who may sign in to this project, as what (Viewer, Editor, Owner), and which sections and reports each person sees.',transfer:'The whole project as one zip, so it can be kept, reviewed, moved or rebuilt. Import goes through ABI, which checks it and makes it one undoable commit.',edits:'Write-back: people correct or add rows of the gold tables. Edits apply on top of the table by row key (the loaded data is never changed), survive refreshes, can be undone, and every query sees the edited table.',reports:'The saved reports of this project. Open one to filter it and click through.',monitor:'Is anything wrong right now: data freshness, model readiness, ML runs and flows. Reports show the business; this shows the system.',explore:'Your own analysis: pick measures, a dimension and a filter; the numbers come from the model, so they match the reports. Keep the ones you want: they are saved with the app, the project itself doesn\'t change.',glossary:'What the business words mean and which measures implement them.',sources:'Bronze: the data as it landed, one source per file or bucket folder. Reload, add or delete a source.',
 staged:'Silver: the cleaned and combined tables between the sources and the final tables. Hidden from the model; edit their steps here.',tables:'Gold: the final business tables the model and measures are built on. Edit kind, key, description and steps.',flows:'Automations. Run one now and follow its steps, or switch it on or off.',
 ml:'Forecasts, segments and anomaly models. Train, score and read the latest run.',tools:'Calculations your agents can call. Run one with inputs or run its tests.',
 agents:'Custom agents for this project. Ask one a question or run its evaluation set.',docs:'The project documentation ABI writes from its definitions.',
 security:'Who sees which rows. Roles are set in ABI itself.',environments:'Where each environment reads its data from.',history:'Every change is a commit in ABI. Newest first.'};let PAGES={},CUR='home';const side=$('aside');
function nav(){const all=navAll(),me=window.ME||{admin:true};return all.filter(([g])=>g==='Start'||!me.sections||me.sections.includes(g))}
function navAll(){return [['Start',[['home','Home',home],['chat','Chat',chatPage]]],
 ['Data',[['sources','Sources',sources,D.sources.length],['staged','Staged',stagedPage,(D.layers||[]).filter(t=>t.layer==='silver').length],['tables','Tables',tablesPage,(D.model?Object.keys(D.model.tables||{}).length:D.tables.length)],['edits','Edits',editsPage]]],
 ['Data Model',[['model','Data models',modelsPage,(D.models||[]).length+1],['measures','Measures',measures,D.measures.length],
 ['glossary','Glossary',glossary,D.glossary.length],['datasets','Queries',datasets,D.datasets.length]]],
 ['Visualizations',[['reports','Reports',reportsPage,D.reports.length],['monitor','Monitor',monitorPage],['explore','Explore',explore,MINE.length||'']]],
 ['Export',[['boardpack','Board pack',boardPage,(D.app?.packs||[]).length],['documents','Documents',documentsPage],['onepager','One-pager',onePagerPage],['dataexport','Data',dataExportPage],['video','Video',videoPage],['transfer','Project',transferPage]]],
 ['Automate',[['flows','Flows',flows,D.flows.length],['ml','ML models',ml,D.ml.length],['tools','Tools',tools,D.tools.length],['agents','Agents',agents,D.agents.length]]],
 ['Knowledge',[['vault','Vault',vaultPage,(VAULT||[]).length],['docs','Docs',docs]]],
 ['Govern',[['security','Security',security],['environments','Environments',b=>{const x=(D.sections.Environments||'').trim();if(x&&!/^<p[^>]*>\s*<\/p>$/.test(x))b.insertAdjacentHTML('beforeend',x);if(b.innerText.trim().length<10)b.append(h('p','empty','No environments yet: a dev/prod pair lets you test changes before they reach production. Ask the agent to set one up.'))}],['history','History',commits,(D.history||[]).length],...((window.ME||{admin:true}).admin?[['people','Users & access',usersPage],['settings','Settings',settingsPage]]:[])]]]}
let MODE='developer',VAULT=[],MODEL='';
let FOLD={};try{FOLD=JSON.parse(localStorage.getItem('abi-fold')||'{}')}catch(e){}
function sidebar(){side.innerHTML='';PAGES={};nav().forEach(([g,items])=>{const sec=h('div','sec'),hd=h('button','grp'),list=h('div','items');
 hd.append(h('span','',g),h('span','car'));hd.setAttribute('aria-expanded',String(!FOLD[g]));sec.classList.toggle('folded',!!FOLD[g]);
 hd.onclick=()=>{FOLD[g]=!FOLD[g];sec.classList.toggle('folded',FOLD[g]);hd.setAttribute('aria-expanded',String(!FOLD[g]));try{localStorage.setItem('abi-fold',JSON.stringify(FOLD))}catch(e){}};
 items.forEach(([k,label,fn,n])=>{PAGES[k]=[label,fn];const b=h('button','it');b.dataset.k=k;
  b.append(h('span','',label));if(n!==undefined&&n!=='')b.append(h('span','n',String(n)));b.onclick=()=>show(k);list.append(b)});
 sec.append(hd,list);side.append(sec)});D.reports.forEach(r=>PAGES['rep:'+r.id]=[r.name||r.id,b=>report(b,r)]);PAGES['mdl:all']=['Whole model',b=>openModel(b,'')];(D.flows||[]).forEach(f=>PAGES['flw:'+f.id]=[f.name||f.id,b=>flowPage(b,f)]);
 (D.models||[]).forEach(m=>PAGES['mdl:'+m.id]=[m.name,b=>openModel(b,m.id)])}
let PREV='home';
function show(k){if(!PAGES[k])k='home';if(k!=='chat'&&CUR!=='chat')PREV=k;else if(k==='chat'&&CUR!=='chat')PREV=CUR;{const cb=$('#chat');if(cb.classList.contains('full')){cb.classList.remove('full');$('#chat .wide-t').textContent='Full page';document.body.append(cb);cb.classList.remove('open');document.body.classList.remove('chat-open')}}CUR=k;setTimeout(ctx);const sk=k.startsWith('rep:')?'reports':k.startsWith('mdl:')?'model':k.startsWith('flw:')?'flows':k;side.querySelectorAll('button.it').forEach(b=>b.classList.toggle('on',b.dataset.k===sk));
 const act=side.querySelector('button.it.on');if(act)act.closest('.sec').classList.remove('folded');
 const box=$('main');box.innerHTML='';const pg=h('div','pg'),head=h('div','pghead'),pn=$('#pgname');pn.textContent=PAGES[k][0];document.title=PAGES[k][0]+' · '+D.project;
 pn.title=(k.startsWith('rep:')?'Filter the report or click a bar to filter by it. Every number comes from ABI.':LEDE[k])||'';pg.append(head);box.append(pg);PAGES[k][1](pg);lay(pg,k);new MutationObserver(()=>lay(pg,k)).observe(pg,{childList:true,subtree:true});
 const top=pg.querySelector(':scope>.bar.top'),lead=pg.querySelector(':scope>.bar.lead');if(lead)head.append(lead);if(top)head.append(top);if(!top&&!lead)head.remove();if(MODE==='viewer')pg.querySelectorAll('.ed').forEach(e=>e.readOnly=true);
 try{localStorage.setItem('abi-app-'+D.project,k)}catch(e){}try{window.history.replaceState(null,'','#'+k)}catch(e){}}
// ---------- layout: Layout (Developer, desktop) lets you drag a card to move it and drag its corner to resize it (12
// columns, 20 px steps). A report keeps it in ABI's report (x, y, w, h: commit, then publish); every other page in the
// app's layouts.json, a draft that Publish copies to what Viewer sees ----------
let LAYING=false;const LG=':scope .cards,:scope .home,:scope .layers,:scope .grid,:scope .loose',grids=pg=>[...pg.querySelectorAll(LG)].filter(g=>!g.parentElement.closest('.card'));
const layFile=()=>(D.app||={}).layouts||={},laySaved=k=>(layFile()[MODE==='viewer'?'published':'draft']||{})[k]||{};
const layKids=g=>[...g.children].filter(c=>!c.classList.contains('rowhead')),ttl=c=>(c.querySelector(':scope>h3>span')?.textContent||'').trim();
const lkey=(g,c,i)=>c.dataset.lk||ttl(c)+'#'+layKids(g).slice(0,i).filter(x=>ttl(x)===ttl(c)).length;
function lay(pg,k){if(document.body.classList.contains('mobile')||innerWidth<820)return;const S=laySaved(k);
 // cards straight on the page (Docs, Video, History…) go into one grid, so every page has containers to arrange
 [...pg.children].filter(c=>c.classList.contains('card')).forEach(c=>{const w=c.previousElementSibling?.classList.contains('loose')?c.previousElementSibling:c.before(h('div','loose'))||c.previousElementSibling;w.append(c)});
 grids(pg).forEach((g,gi)=>{if(MODE==='developer')layKids(g).forEach(c=>{if(!c.querySelector(':scope>.rz'))c.append(rz(c))});const s=S[gi]||{};if(!LAYING&&!Object.keys(s).length&&!g.classList.contains('lay')&&!g.dataset.page)return;g.classList.add('lay');
  layKids(g).forEach((c,i)=>{const x=s[lkey(g,c,i)];
   c.dataset.w||=(c.style.gridColumn.match(/span (\d+)/)||[])[1]||(g.matches('.rows,.stack,.loose')||c.classList.contains('full')?12:c.classList.contains('wide')?(g.classList.contains('home')?8:12):4);
   if(!c.dataset.laid){c.dataset.laid=1;const ht=x?x.h:c.dataset.h;c.style.gridColumn='span '+(x?.w||c.dataset.w);c.style.order=x?.o??i;c.style.height=ht?ht+'px':'';c.classList.toggle('sized',!!+ht)}  // once: a redraw never undoes a drag
   c.draggable=LAYING});
  if(LAYING&&!g.dataset.dnd){g.dataset.dnd=1;let src=null;const on=e=>{const t=e.target.closest?.('.lay>*');return t?.parentElement===g?t:null},clr=()=>g.querySelectorAll('.over').forEach(x=>x.classList.remove('over'));
   g.addEventListener('dragstart',e=>{src=on(e);if(!src)return;src.classList.add('drag');e.dataTransfer.effectAllowed='move'});
   g.addEventListener('dragover',e=>{const t=on(e);if(!src||!t)return;e.preventDefault();clr();if(t!==src)t.classList.add('over')});
   g.addEventListener('drop',e=>{const t=on(e);if(!src||!t||t===src)return;e.preventDefault();const ks=layKids(g).sort((a,b)=>a.style.order-b.style.order);
    ks.splice(ks.indexOf(src),1);ks.splice(ks.indexOf(t),0,src);ks.forEach((c,i)=>c.style.order=i);layStore()});
   g.addEventListener('dragend',()=>{src?.classList.remove('drag');clr();src=null})}})}
function layOn(g){if(g.classList.contains('lay'))return;const gap=parseFloat(getComputedStyle(g).columnGap)||16,col=(g.clientWidth-11*gap)/12;  // keep the look: each card's width in columns
 layKids(g).forEach((c,i)=>{c.dataset.w=Math.max(1,Math.min(12,Math.round((c.offsetWidth+gap)/(col+gap))));c.dataset.laid=1;c.style.gridColumn='span '+c.dataset.w;c.style.order=i});g.classList.add('lay')}
function rz(c){const r=h('div','rz');r.title='Drag to resize';r.onpointerdown=e=>{e.preventDefault();e.stopPropagation();layOn(c.parentElement);const g=c.parentElement,gap=parseFloat(getComputedStyle(g).columnGap)||16,col=(g.clientWidth-11*gap)/12,b=c.getBoundingClientRect();
 c.draggable=false;r.setPointerCapture(e.pointerId);r.onpointermove=m=>{c.style.gridColumn='span '+Math.max(1,Math.min(12,Math.round((m.clientX-b.left+gap)/(col+gap))));c.style.height=Math.max(80,Math.round((m.clientY-b.top)/20)*20)+'px';c.classList.add('sized')};
 r.onpointerup=r.onpointercancel=()=>{r.onpointermove=r.onpointerup=r.onpointercancel=null;c.draggable=LAYING;layStore()}};return r}
async function layStore(reset){const k=CUR,pg=$('main .pg'),out={};
 grids(pg).forEach((g,gi)=>{if(!g.classList.contains('lay'))return;const o={};layKids(g).forEach((c,i)=>o[lkey(g,c,i)]={o:+c.style.order||0,w:+(c.style.gridColumn.match(/\d+/)||[4])[0],h:c.classList.contains('sized')?parseInt(c.style.height):0});out[gi]=o});
 if(k.startsWith('rep:')){const rep=D.reports.find(r=>'rep:'+r.id===k),g=pg.querySelector('.grid'),spec=structuredClone(rep.spec),sp=spec.pages.find(p=>p.id===g.dataset.page),vp=rep.pages.find(p=>p.id===g.dataset.page);
  let x=0,y=0,rh=0;layKids(g).sort((a,b)=>a.style.order-b.style.order).forEach(c=>{const v=sp.visuals.find(v=>v.id===c.dataset.lk);if(!v)return;const w=+(c.style.gridColumn.match(/\d+/)||[6])[0],
   hh=c.classList.contains('sized')?Math.max(1,Math.round((parseInt(c.style.height)+10)/70)):(v.layout?.h||4);if(x+w>12){x=0;y+=rh;rh=0}v.layout={x,y,w,h:hh};vp.visuals.find(z=>z.id===v.id).layout=v.layout;x+=w;rh=Math.max(rh,hh)});
  try{await api('save_report',{report:spec});rep.spec=spec;toast('Saved in the report: commit, then publish, and Viewer sees it.')}catch(e){toast(e.message)}return}
 const r=await fetch('/layouts',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify({page:k,layout:reset?null:out})}).then(x=>x.json()).catch(e=>({error:e.message}));
 if(r.error)toast(r.error);else{layFile().draft=r.result;vc();if(reset)show(k)}}
function layMode(on){LAYING=on;document.body.classList.toggle('laying',on);const b=$('#layout');b.classList.toggle('on',on);b.textContent=on?'Done':'Layout';$('#layreset').hidden=!on}
$('#layout').onclick=()=>{layMode(!LAYING);show(CUR);if(LAYING)toast('Drag a card to move it, its corner to resize it. Publish shows it to Viewer.')};
$('#layreset').onclick=()=>{if(CUR.startsWith('rep:')){toast('A report\'s layout is in the report: undo it under Changes.');return}layStore(true)};
const boot=loadVault();sidebar();
const R=document.documentElement,TB=$('#theme');const dark=()=>R.dataset.theme?R.dataset.theme==='dark':matchMedia('(prefers-color-scheme: dark)').matches;
const setTheme=t=>{if(t)R.dataset.theme=t;TB.textContent=dark()?'Light':'Dark'};try{setTheme(localStorage.getItem('abi-theme'))}catch(e){setTheme()}
TB.onclick=()=>{const t=dark()?'light':'dark';setTheme(t);try{localStorage.setItem('abi-theme',t)}catch(e){}};

function setMode(md){if(window.ME&&!['editor','owner'].includes(ME.role))md='viewer';MODE=md;if(md==='viewer'&&LAYING)layMode(false);setTimeout(()=>{ctx();const g=log.querySelector('.hello');if(g)g.replaceWith(hello())});document.body.dataset.mode=md;$('#m-dev').classList.toggle('on',md==='developer');$('#m-view').classList.toggle('on',md==='viewer');
 $('#chat textarea').placeholder=md==='viewer'?'Ask a question. In Viewer the agent only reads.':'Ask a question or for a change.'}
async function pickMode(md){
 await fetch('/mode',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify({mode:md})})}
function vc(){const n=D.pending?.files?.length||0,cb=$('#changes'),pb=$('#publish');cb.textContent=n?`${n} change${n>1?'s':''}`:'No changes';cb.classList.toggle('pri',!!n);
 const head=(D.history||[])[0]?.id?.slice(0,7),pubc=(D.published?.commit||'').split(' ')[0];const L=D.app?.layouts||{},ld=JSON.stringify(L.draft||{})!==JSON.stringify(L.published||{});pb.disabled=!!n||!head||(head===pubc&&!ld);
 pb.title=n?'Commit first: Publish sends the last commit.':head===pubc?(ld?'Show the new page layouts to Viewer.':'Viewer already shows the last commit.'):'Make the last commit what Viewer sees.'}
$('#changes').onclick=()=>{const d=$('#commit'),f=$('.files',d);f.innerHTML='';const fl=D.pending?.files||[];
 if(!fl.length)f.append(h('p','empty','Everything is committed.'));fl.forEach(x=>{const det=h('details');const su=h('summary','',`${x.status}  ${x.path}`);det.append(su,h('pre','',x.patch||''));f.append(det)});
 $('#c-message').value=D.pending?.proposed_message||'';$('.msg',d).textContent='';$('#c-undo').disabled=!D.pending?.can_undo;d.showModal()};
$('#c-close').onclick=()=>$('#commit').close();
$('#c-commit').onclick=async()=>{try{await api('commit',{message:$('#c-message').value||'Changes from the app'});$('#commit').close();toast('Committed')}catch(e){say($('#commit .msg'),e.message,1)}};
$('#c-undo').onclick=async()=>{try{const r=await api('undo',{});say($('#commit .msg'),`Undone: ${r.undone}`);D.pending=await api('changes',{});$('#changes').onclick()}catch(e){say($('#commit .msg'),e.message,1)}};
$('#publish').onclick=async()=>{if(!await ask({message:'Publish the last commit? Viewer will show it from now on.',preview:(D.history||[])[0]?.message||''}))return;
 toast('Publishing: ABI checks the last commit…');const post=(u,b)=>fetch(u,{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify(b||{})}).then(r=>r.json());
 let r=await post('/publish');if(r.confirm){if(!await ask(r.confirm)){toast('Not published.');return}r=await post('/confirm/'+r.confirm.id)}
 toast(r.error?`Publish failed: ${r.error}`:`Published ${r.result.commit}`)};
const sideOff=on=>{document.body.classList.toggle('side-off',on);try{localStorage.setItem('abi-side-off',on?'1':'')}catch(e){}};try{sideOff(localStorage.getItem('abi-side-off')==='1')}catch(e){}
$('#menu').title='Show or hide the sidebar';$('#menu').onclick=()=>R.classList.contains('mobile')?document.body.classList.toggle('menu-open'):sideOff(!document.body.classList.contains('side-off'));side.addEventListener('click',e=>{if(e.target.closest('button.it'))document.body.classList.remove('menu-open')});
$('#switch').onclick=switcher;
function pwDialog(){const d=h('dialog'),c=h('div','card'),cur=h('input','f'),nw=h('input','f'),m=h('div','msg');c.append(h('h3','','Change your password'));
 cur.type=nw.type='password';cur.placeholder='current password';cur.autocomplete='current-password';nw.placeholder='new password (8+ characters)';nw.autocomplete='new-password';
 const ok=btn('Change',async()=>{const r=await fetch('/password',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify({current:cur.value,password:nw.value})}).then(x=>x.json()).catch(e=>({error:e.message}));
  cur.value=nw.value='';if(r.error){say(m,r.error,1);return}say(m,'Changed. Sign in again with the new one.');setTimeout(()=>location.reload(),1500)}),no=btn('Cancel',()=>{d.close();d.remove()});
 const row=h('div','row acts');row.append(ok,no);c.append(cur,nw,row,m);d.append(c);document.body.append(d);d.showModal();
 fetch('/signin/sso').then(r=>r.json()).then(L=>{if(!L.length)return;const l=h('div','row acts');c.insertBefore(h('h3','','Sign in with your company account'),m);c.insertBefore(l,m);
  L.forEach(x=>l.append(btn('Link '+x.name,async()=>{const r=await fetch('/api/oidc/'+encodeURIComponent(x.id)+'/start',{method:'POST',headers:{'Content-Type':'application/json'},body:'{"link":true}'}).then(y=>y.json());
   if(r.url)location.href=r.url;else say(m,r.error||'Not available.',1)})))}).catch(()=>{})}
{const q=new URLSearchParams(location.search).get('signin');if(q){setTimeout(()=>toast({linked:'Linked: you can sign in with it now.',conflict:'That account is linked to another person here.'}[q]||'Linking didn\'t work: try again.'));history.replaceState(null,'',location.pathname+location.hash)}}
if(window.ME?.signin){const w=h('button','btn who',ME.name);w.title='Change your password';w.onclick=pwDialog;const f=h('form');f.method='post';f.action='/signout';f.append(h('button','btn','Sign out'));const r0=$('header .right');r0.prepend(w,f)}
if(window.ME&&!['editor','owner'].includes(ME.role))$('.modes').hidden=true;/* Viewer access only: no switch at all */$('#m-dev').onclick=()=>pickMode('developer');$('#m-view').onclick=()=>pickMode('viewer');
let first=decodeURIComponent(location.hash.slice(1));if(!first)try{first=localStorage.getItem('abi-app-'+D.project)}catch(e){}setTimeout(()=>show(first||'home'));  // after the whole script: the chat page's parts are declared further down
boot.then(()=>{sidebar();show(CUR)});setMode('developer');vc();
window.addEventListener('hashchange',()=>{const k=decodeURIComponent(location.hash.slice(1));if(k&&k!==CUR)show(k)});

// ---------- the agent: it steers this page, the page follows its changes, and the person talks to it here ----------
async function switcher(){let d=$('#switcher');if(d){d.remove();return}
 const r=(await fetch('/projects',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:'{}'}).then(x=>x.json())).result;
 d=h('div','card');d.id='switcher';const ws=h('select','f'),list=h('div','areas');Object.keys(r.workspaces).forEach(w=>ws.append(new Option(w,w)));ws.value=r.workspace;
 const fill=()=>{list.innerHTML='';(r.workspaces[ws.value]||[]).forEach(p=>{const b=h('button','lk');b.append(h('b','',p.id),h('span','',p.name===p.id?'':p.name));
  if(p.id===r.current)b.classList.add('on');b.onclick=()=>{location.href='/?p='+encodeURIComponent(p.id)+'#home'};list.append(b)})};ws.onchange=fill;fill();
 d.append(h('label','l','Workspace'),ws,h('label','l','Projects in it'),list);document.body.append(d)}
function toast(t){const x=h('div','toast',t);document.body.append(x);setTimeout(()=>x.remove(),6000)}
function highlight(item){if(!item)return;const want=String(item).toLowerCase();
 const c=[...document.querySelectorAll('main .card')].find(c=>c.textContent.toLowerCase().includes(want));if(c){c.classList.add('hl');c.scrollIntoView({behavior:'smooth',block:'center'});setTimeout(()=>c.classList.remove('hl'),4000)}}
const chatBox=$('#chat'),log=$('#chat .log'),inp=$('#chat textarea'),busy=$('#chat .busy');
// an answer's text as HTML: escaped first, then bold, code and markdown tables (a block of |-lines; the --- row is dropped)
const md=t=>t.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])).replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>').replace(/`([^`]+)`/g,'<code>$1</code>')
 .replace(/(^|\n)((?:[ \t]*\|[^\n]*\|[ \t]*(?:\n|$))+)/g,(all,pre,block)=>{const rows=block.trim().split('\n').map(r=>r.trim().slice(1,-1).split('|').map(c=>c.trim()))
  .filter(r=>!r.every(c=>/^:?-{2,}:?$/.test(c)));if(rows.length<2)return all;const n=c=>/^[-+]?[\d.,]+\s*%?$/.test(c);
  return pre+'<table class=mdt><tr>'+rows[0].map(c=>'<th>'+c+'</th>').join('')+'</tr>'+rows.slice(1).map(r=>'<tr>'+r.map(c=>'<td'+(n(c)?' class=n':'')+'>'+c+'</td>').join('')+'</tr>').join('')+'</table>'});
function bubble(cls,text){const b=h('div','m '+cls);if(cls==='ai')b.innerHTML=md(text);else b.textContent=text;log.append(b);log.scrollTop=log.scrollHeight;return b}
let steps=null;
// Chat page: the same conversation as the side panel, full width; leaving the page (or the agent showing one) puts it back at the side
function chatPage(box){$('#chat .wide-t').textContent='Side panel';chatBox.classList.add('full','open');document.body.classList.remove('chat-open');box.append(chatBox);chats();inp.focus()}
function openChat(on){const was=chatBox.classList.contains('open');chatBox.classList.toggle('open',on??!was);if(!was&&chatBox.classList.contains('open'))chats();document.body.classList.toggle('chat-open',chatBox.classList.contains('open'));if(chatBox.classList.contains('open'))inp.focus()}
const STEP={abi_show:'opened a page',semantic_query:'asked the model',visual_data:'read a chart',abi_vault:'searched the vault',abi_tools:'picked tools',
 semantic_model:'read the model',readiness:'checked readiness',data_quality:'checked data quality',how_calculated:'looked up a formula',set_measure:'saved a measure',
 save_report:'saved a report',save_flow:'saved a flow',commit:'committed',changes:'listed changes',subagent:'asked the team',abi_prefs:'read your preferences',read:'read a file'};
const stepName=t=>{const k=String(t).replace('mcp__abi__','');return STEP[k]||k.replace(/_/g,' ')};
function hello(){const d=h('div','hello'),pg=PAGES[CUR]?.[0]||'this page';d.append(h('p','',MODE==='viewer'
  ?'Ask about the numbers. I read the published project only, and every number comes from ABI.'
  :'Ask a question, ask where something is, or say what to build or change. Changes stay uncommitted until you commit them.'));
 [`What is on ${pg}, and what should I look at?`,...(MODE==='viewer'?['What changed last month, and why?','Where do I see revenue by country?']
  :['What is still open in this project?','Where are the flows, and what do they do?'])].forEach(t=>{const b=h('button','sg',t);b.onclick=()=>{inp.value=t;sendChat()};d.append(b)});return d}
function ctx(){const c=$('#chat .ctx'),g=log.querySelector('.hello');if(c)c.textContent=`${MODE==='viewer'?'Viewer · read-only':'Developer'} · on ${PAGES[CUR]?.[0]||CUR}${MODEL?' · '+MODEL:''}`;if(g&&CUR!=='chat')g.replaceWith(hello())}
const INTRO='';log.append(hello());
async function cpost(u,b){return fetch(u,{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify(b||{})}).then(r=>r.json())}
async function chats(sel){const r=await cpost('/chat/list'),pk=$('#chat-pick');pk.innerHTML='';
 const cur=sel||r.current;if(!r.chats.some(c=>c.id===cur))pk.append(new Option('New chat',cur||''));
 r.chats.forEach(c=>pk.append(new Option(`${c.title||'Chat'} · ${c.messages}`,c.id)));pk.value=cur||'';
 const del=$('#chat-del');if(del)del.hidden=!r.chats.length}
function paint(c){log.innerHTML='';steps=null;(c.messages||[]).forEach(m=>bubble(m.role==='me'?'me':'ai',m.text));if(!log.children.length)log.append(hello())}
$('#chat-new').onclick=async()=>{const c=await cpost('/chat/new');paint(c);chats(c.id);inp.focus()};
$('#chat-pick').onchange=async e=>{if(!e.target.value)return;const c=await cpost('/chat/open',{id:e.target.value});paint(c);chats(c.id)};
$('#chat-toggle').onclick=()=>openChat();$('#chat .wide-t').onclick=()=>{if(chatBox.classList.contains('full')){show(PREV||'home');openChat(true)}else show('chat')};$('#chat .x').onclick=()=>openChat(false);
async function sendChat(){const t=inp.value.trim();if(!t)return;
 log.querySelector('.hello')?.remove();bubble('me',t);inp.value='';steps=null;
 await fetch('/chat',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify({message:t,page:CUR})})}
$('#chat .send').onclick=sendChat;inp.onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendChat()}};
$('#chat .stop').onclick=()=>fetch('/chat/stop',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:'{}'});
{const es=new EventSource('/events?k='+encodeURIComponent(KEY)+'&c='+CID+'&m='+(MOBILE?1:0)+'&p='+encodeURIComponent(D.project));
 es.onerror=()=>{if(es.readyState===2)stale()};  // the stream was refused: a restart or an ended session
 es.onmessage=async ev=>{const e=JSON.parse(ev.data);
  if(e.type==='show'){PENDING.filters=e.filters||null;if(e.page==='explore'&&e.query){const q={measures:e.query.measures||[],dimensions:(e.query.dimensions||[]).slice(0,1),filters:e.query.filters||[]};PENDING.explore={...q,chart:e.chart};MINE.unshift({name:'Agent: '+q.measures.join(', ')+(q.dimensions[0]?' by '+q.dimensions[0]:''),q,type:e.chart||'bar',at:new Date().toISOString().slice(0,16).replace('T',' ')});saveMine();sidebar()}const nv=h('div','m nav','→ '+(e.page==='report'?'report '+e.report:e.page)+(e.note?' · '+e.note:''));nv.onclick=()=>show(e.page==='report'?'rep:'+e.report:e.page);log.append(nv);log.scrollTop=log.scrollHeight;const wasFull=chatBox.classList.contains('full');const k=e.page==='report'?'rep:'+e.report:e.page;show(k);if(wasFull)openChat(true);setTimeout(()=>highlight(e.item),400);if(e.note)toast('Agent: '+e.note)}
  else if(e.type==='data'){Object.assign(D,e.data);const k=CUR;sidebar();show(k);vc()}
  else if(e.type==='busy'){busy.hidden=!e.on;$('#chat .stop').hidden=!e.on}
  else if(e.type==='step'){if(!steps){steps=h('div','m steps');log.append(steps)}const last=[...steps.children].reverse().find(x=>x.dataset.t===e.tool&&x.dataset.s==='run');
   if(e.state==='run'||!last){const l=h('div','',`${e.state==='run'?'…':e.state==='ok'?'✓':'✗'} ${stepName(e.tool)}`);l.dataset.t=e.tool;l.dataset.s=e.state;steps.append(l)}
   else{last.textContent=`${e.state==='ok'?'✓':'✗'} ${stepName(e.tool)}`;last.dataset.s=e.state;last.className=e.state==='ok'?'':'bad'}log.scrollTop=log.scrollHeight}
  else if(e.type==='said')bubble('me',e.text)
  else if(e.type==='answer'){steps=null;if(e.model){MODEL=e.model;ctx()}bubble('ai',e.text);chats();if(!chatBox.classList.contains('open'))toast('The agent answered')}
  else if(e.type==='ask'){const yes=await ask({message:e.title||'ABI asks for your confirmation',preview:e.message});bubble('sys',yes?'You confirmed.':'You declined.');
   fetch('/chat/answer',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify({id:e.id,yes})})}
  else if(e.type==='open'){location.href='/?p='+encodeURIComponent(e.project)+'#'+encodeURIComponent(e.page)}
  else if(e.type==='mode'){setMode(e.mode);show(CUR);if(e.mode==='viewer'&&!e.published){let seen=0;try{seen=sessionStorage.getItem('abi-unpub');sessionStorage.setItem('abi-unpub','1')}catch(x){}if(!seen)toast('Nothing published yet: Viewer shows the working version until you publish.')}}
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


MONITOR = {"watch": [], "fresh_within_hours": 36, "checks": []}  # watch: tables that must stay fresh ([] = every table named *stream*)


def app_dir(project: str) -> Path:
    """The app's own JSON for a project (Monitor settings, shared analyses): next to ABI's folder, in the export zip."""
    return abi.folder(project).parent / "_app" / project


def app_save(project: str, name: str, data, by: str = "") -> None:
    """Writes one of the app's JSON files and commits it in the folder's own Git, so the app's settings have a history
    and can be reviewed and undone like the project's (git -C workspaces/<ws>/_app/<project> log)."""
    d = app_dir(project)
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps(data, indent=1) + "\n")
    git = ["git", "-C", str(d), "-c", "user.name=" + (by or "ABI-PI"), "-c", "user.email=abi-pi@localhost"]
    if not (d / ".git").exists():
        subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", name], check=True)
    subprocess.run([*git, "commit", "-q", "-m", f"{name} by {by or 'the app'}"], capture_output=True)  # nothing new: no commit


def snapshot(dest: Path, project: str) -> dict:
    """What the page shows: ABI's live state, uncommitted changes included. The folder holds commits only, so the
    definitions read from it (sources, staged tables' steps, queries) come from a copy with ABI's pending changes applied."""
    import shutil  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    pending = read("changes", project)
    patch = "".join((f.get("patch") or "").rstrip("\n") + "\n" for f in (pending or {}).get("files") or [])
    with tempfile.TemporaryDirectory() as tmp:
        if patch.strip():
            shutil.copytree(dest, tmp, ignore=shutil.ignore_patterns(".git", "app.html"), dirs_exist_ok=True)
            if subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], cwd=tmp, input=patch, text=True, capture_output=True).returncode == 0:
                dest = Path(tmp)  # ponytail: a patch that doesn't apply falls back to the last commit
        return _snapshot(dest, project, pending)


def _snapshot(dest: Path, project: str, pending: dict | None) -> dict:
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
           "agents": live("list_agents", "agents", "agents"),
           "agent_evals": {f.name.removesuffix(".eval.json"): (j(f, {}) or {}).get("cases", []) for f in sorted((dest / "agents").glob("*.eval.json"))}, "glossary": sm.get("glossary") or [], "models": sm.get("models") or [],
           "datasets": [queries.get(d["id"], {}) | d for d in sm.get("datasets") or []], "model": model,
           "columns": sm.get("columns") or {}, "sources": sorted(sources), "source_specs": sources, "table_steps": table_steps,
           "tables": sorted(model["tables"]), "readiness": read("readiness", project), "pending": pending,
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
    base = project.removesuffix("_pub")
    import pack  # noqa: PLC0415 (sibling script)
    out["app"] = {"analyses": j(app_dir(base) / "analyses.json", []) or [], "monitor": MONITOR | (j(app_dir(base) / "monitor.json", {}) or {}),
                  "packs": pack.listing(base), "layouts": j(app_dir(base) / "layouts.json", {}) or {}}
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


def docs_pdf(project: str) -> bytes:
    """ABI's documentation of the project as an A4 PDF: render.docs_html printed by the engine's headless Chromium
    (the one report exports use), in its own process."""
    import tempfile  # noqa: PLC0415
    name = (render.j(abi.folder(project) / "project.json", {}) or {}).get("name", project)
    py = Path(__file__).resolve().parent.parent / "engine/backend/.venv/bin/python"
    home = os.environ.get("ENGINE_HOME") or str(Path(__file__).resolve().parent.parent / "data/home")
    code = ("import sys\nfrom playwright.sync_api import sync_playwright\nwith sync_playwright() as p:\n b = p.chromium.launch(args=['--disable-dev-shm-usage'])\n"
            " pg = b.new_page(); pg.goto('file://' + sys.argv[1]); pg.wait_for_load_state('networkidle')\n"
            " pg.pdf(path=sys.argv[2], format='A4', print_background=True, margin={k: '14mm' for k in ('top', 'right', 'bottom', 'left')}); b.close()")
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "docs.html").write_text(render.docs_html(name, read("docs", project) or {}))
        subprocess.run([str(py), "-c", code, f"{d}/docs.html", f"{d}/docs.pdf"], check=True, timeout=180,
                       env={**os.environ, "HOME": home if Path(home).exists() else os.environ.get("HOME", "")}, capture_output=True)
        return (Path(d) / "docs.pdf").read_bytes()


def publish_layouts(project: str, by: str = "") -> None:
    """Publish also copies the page layouts Developer arranged (layouts.json "draft") to what Viewer sees ("published")."""
    lay = render.j(app_dir(project) / "layouts.json", {}) or {}
    if lay.get("draft") != lay.get("published"):
        app_save(project, "layouts.json", lay | {"published": lay.get("draft") or {}}, by)


def publish_roles(project: str, pub: str) -> None:
    """Everyone with a role on the project reads its published copy: Viewer there. A role given before the first Publish,
    or in ABI's own app, left that person an empty Viewer (release run 31 Oct). Removing a role stays with Users & access
    (it is destructive and asks first)."""
    try:
        users = abi.call("list_users").get("users") or []
    except abi.ActionError:
        return  # not an owner: the owner's Publish does it
    for u in users:
        if (u.get("roles") or {}).get(project) and not (u.get("roles") or {}).get(pub) and not u.get("workspace_owner"):
            try:
                abi.call("set_role", project=pub, user=u["name"], role="viewer")
            except abi.ActionError as e:
                print(f"abi-pi: viewer role on {pub} for {u['name']} not set: {e.body.get('message')}", file=sys.stderr)


def published_now(project: str, pub: str, head: str) -> dict:
    publish_layouts(project)
    publish_roles(project, pub)
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


def page_html(project: str, snap: dict) -> str:
    dest = abi.folder(project)
    data = json.dumps(snap, default=str).replace("</", "<\\/")
    name = (render.j(dest / "project.json", {}) or {}).get("name", project)
    page = render.shell(name, "", "", "")
    head = (f"<header><button class=btn id=menu aria-label=Menu>Menu</button><span class=brand>ABI</span><button class=crumb id=switch title='Switch workspace or project'>{render.esc(dest.parent.name)}<i>/</i><b>{render.esc(project)}</b><i class=car>▾</i></button><span class=pgname id=pgname></span>"
            "<span class=right><span class='vc w'><button class=btn id=layout title='Drag cards to move them, drag a corner to resize'>Layout</button><button class=btn id=layreset hidden title='Put this page back as it was built'>Reset</button><button class=btn id=changes>No changes</button>"
            "<button class=btn id=publish title='Make the last commit what Viewer sees'>Publish</button></span><span class=modes role=group aria-label=Mode>"
            "<button id=m-dev class=on title='Build and change: you and the agent'>Developer</button>"
            "<button id=m-view title='Read only: the agent answers as an analyst'>Viewer</button></span>"
            "<button class=btn id=theme title='Light or dark'>Dark</button><button class='btn pri' id=chat-toggle>Ask<span class=long> the agent</span></button></span></header>")
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
    return page.replace("</style>", CSS + "</style>").replace("</body>", f"<script>window.ME={ME_SLOT};window.APP={data};{render.JS}\n{JS}</script></body>")


class Hub:
    """The live side of one app: the page's event stream, the agent's view commands and the chat's Pi process.

    The chat runs `pi --mode rpc` here as the person (ABI_TOKEN from this process); its answers reach the page only
    after the harness grounded them (message_end, never the streamed draft), its confirmations become a dialog in the
    page and the person's yes or no goes back to Pi (the confirm token stays in the harness)."""

    def __init__(self, project: str, url: str, agent_key: str, cid: str = "", mobile: bool = False, user: str = "", token: str | None = None):
        self.project, self.url, self.agent_key, self.mobile = project, url, agent_key, mobile
        self.user, self.token, self.view = user, token, lambda d: d  # view: the person's sections and reports only
        self.hid = f"{user}/{cid}" if user else cid  # the key in HUBS; the chat agent steers this hub only
        self.clients: list = []
        self.lock = threading.Lock()
        self.pi = None
        self.busy = False
        self.last = time.time()  # the chat's last activity: an idle agent is stopped
        self.heard, self.told = time.time(), False  # the agent's last sign; whether a silent model was reported
        self.mode = "viewer" if mobile else "developer"  # this browser's switch (a phone is always Viewer)
        # chats: one Pi session each (its own context), kept on this machine next to the views; one runs at a time
        self.chats_dir = self.chats_of(project)
        self.chat_id = None

    def use(self, project: str) -> None:
        """This browser switched project: its chat agent stops, its chats are that project's."""
        if project == self.project:
            return
        if self.pi and self.pi.poll() is None:
            self.pi.terminate()
        self.pi, self.busy, self.chat_id, self.project = None, False, None, project
        self.chats_dir = self.chats_of(project)

    def chats_of(self, project: str) -> Path:
        """Each person's own chats (their answers were made under their row-level security); one person: as before."""
        d = abi.folder(project).parent / "_views" / project / "chats"
        if self.user:
            d = d / (re.sub(r"[^\w.@-]", "_", self.user).strip(".") or "_")
        d.mkdir(parents=True, exist_ok=True)
        return d

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
        running = any(h.busy for h in self.watchers())
        self.emit({"type": "busy", "on": running})
        if running:
            self.emit({"type": "note", "text": "This chat is running in another window: you follow its steps here."})
        return self.chat_load(self.chat_id)

    def emit(self, event: dict, here: bool = True) -> None:
        line = f"data: {json.dumps(event, default=str)}\n\n".encode()
        for h in ([self] if here else []) + (self.watchers() if event.get("type") in ("step", "answer", "busy", "said") else []):
            with h.lock:
                for q in list(h.clients):
                    q.append(line)

    def watchers(self) -> list:
        """This person's other windows with the same chat open: they follow it live (a chat runs in one window)."""
        return [h for h in list(HUBS.values()) if h is not self and self.chat_id and h.chat_id == self.chat_id and h.user == self.user
                and h.project == self.project]

    def current(self) -> str:
        """Viewer reads the published copy; Developer the project itself."""
        return pub_id(self.project) if self.mode == "viewer" and abi.folder(pub_id(self.project)) else self.project

    def push_data(self) -> None:
        cur = self.current()
        with abi.as_user(self.token):
            self.emit({"type": "data", "data": self.view(cached(cur, self.user))})

    def chat(self, message: str, page: str) -> None:
        if not self.chat_id:
            self.chat_switch(None)
        if any(h.busy for h in self.watchers()):  # one agent per chat: the other window's turn first
            self.emit({"type": "note", "text": "This chat is still running in another window. Wait until it answers, or start a new chat."})
            return
        if not self.pi or self.pi.poll() is not None:
            env = {**os.environ, "ABI_TOKEN": self.token or abi.token(), "ABI_APP_URL": self.url, "ABI_APP_PROJECT": self.project,
                   "ABI_APP_AGENT_KEY": self.agent_key, "ABI_APP_CLIENT": self.hid}
            session = str(self.chats_dir / f"{self.chat_id}.session.jsonl")  # Pi's own transcript: this chat's context
            import settings  # noqa: PLC0415 (sibling script): the admin's model and reasoning, also for the specialists
            conf = settings.load()
            model = conf["model"] or (f"cliproxy/{os.environ['ABI_PI_LLM_MODEL']}" if os.environ.get("ABI_PI_LLM_MODEL") else None)  # Settings, else the deployment's
            flags = (["--model", model] if model else []) + ["--thinking", conf["thinking"]]
            env |= {"ABI_PI_MODEL": conf["model"] or "", "ABI_PI_THINKING": conf["thinking"]}
            self.pi = subprocess.Popen(["pi", "--mode", "rpc", "--session", session, *flags], cwd=abi.ROOT, env=env, text=True, bufsize=1,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=open(abi.ROOT / "logs" / "pi-stderr.log", "ab"))
            threading.Thread(target=self.read, daemon=True).start()
        if self.mode == "viewer":
            where = (f"project {self.current()} (the published version of {self.project}); Viewer mode: you are the analyst,"
                     f" read-only; answer from {self.current()} only")
        else:
            where = (f"project {self.project}; Developer mode: you build and change through ABI. Changes stay uncommitted"
                     " until the person commits them, and Viewer sees them only after Publish")
        text = f"[In the app for {where}; the person is on its '{page}' page.] {message}"
        self.chat_note("me", message)
        self.emit({"type": "said", "text": message}, here=False)
        self.last = self.heard = time.time()
        self.told = False
        self.asked = text  # asked again of a fallback model when this one fails
        self.send({"type": "follow_up" if self.busy else "prompt", "message": text})
        self.busy = True
        self.emit({"type": "busy", "on": True})

    def send(self, cmd: dict) -> None:
        self.pi.stdin.write(json.dumps(cmd) + "\n")
        self.pi.stdin.flush()

    def usage(self, model: str, u: dict, error: bool = False, agent: str = "chat") -> None:
        """One line per model answer in logs/usage.jsonl (tokens and cost as the provider reported them): Settings
        shows the totals per person and model. No text of the chat goes in."""
        cost = u.get("cost") or 0
        rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "user": getattr(self, "user", "") or "", "project": getattr(self, "project", ""),
               "chat": getattr(self, "chat_id", None), "agent": agent, "model": model, "error": error,
               **{k: int(u.get(k) or 0) for k in ("input", "output", "cacheRead", "cacheWrite")},
               "cost": float(cost.get("total") or 0 if isinstance(cost, dict) else cost)}
        try:
            with USAGE_LOCK, open(abi.ROOT / "logs" / "usage.jsonl", "a") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError:  # no logs folder: the answer matters more than its count
            pass

    def read(self) -> None:
        started, final, failed, model, tried, by = {}, "", "", "", [], ""
        proc = self.pi  # this reader's agent: a new chat replaces self.pi while an old one is still ending
        for line in proc.stdout:
            self.heard, self.told = time.time(), False
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
                for r in ((e.get("result") or {}).get("details") or {}).get("results") or []:  # a specialist's own model work
                    if isinstance(r, dict) and r.get("usage"):
                        self.usage(r.get("model") or "?", r["usage"], bool(r.get("errorMessage")), r.get("agent") or "specialist")
                self.emit({"type": "step", "tool": started.get(e["toolCallId"], e.get("toolName", "")).removeprefix("mcp__abi__"),
                           "state": "error" if e.get("isError") else "ok"})
            elif t == "message_end" and (m := e.get("message", {})).get("role") == "assistant":
                model = f"{m.get('provider')}/{m.get('model')}"
                self.usage(model, m.get("usage") or {}, m.get("stopReason") == "error")
                if m.get("stopReason") == "error":  # the model refused (quota, key, outage); Pi retries, then settles
                    failed = (re.search(r"\((\d{3})\)", m.get("errorMessage") or "") or re.search("()", "")).group(1) or "an error"
                    tried.append(model)
                txt = "".join(c.get("text", "") for c in m.get("content", []) if c.get("type") == "text")
                if txt.strip() and not any(c.get("type") == "toolCall" for c in m.get("content", [])):
                    final, by = txt, model  # after grounding; a first draft that failed it is followed by the retry: keep the last
            elif t == "agent_settled":
                import settings  # noqa: PLC0415 (sibling script)
                if not final and failed and getattr(self, "asked", "") and (nxt := settings.next_model(failed, tried)):
                    # the admin's fallbacks, in order: the same question to the next model, in the same chat
                    self.emit({"type": "note", "text": f"{model.split('/', 1)[-1]} didn't answer ({failed}): asking {nxt.split('/', 1)[1]}."})
                    pid, _, mid = nxt.partition("/")
                    self.send({"type": "set_model", "provider": pid, "modelId": mid})
                    self.send({"type": "prompt", "message": self.asked})
                    tried.append(nxt)
                    failed = ""
                    continue
                if not final and failed:  # say so, never just stop: the provider's own text stays in Pi's log
                    why = ": usage limit or too many requests" if failed == "429" else ": the provider is unavailable" if failed.startswith("5") else ""
                    final = (f"The model didn't answer ({failed}{why})."
                             " Try again later, or pick another model or provider in Govern › Settings.")
                if final:
                    self.emit({"type": "answer", "text": final, "model": by.split("/", 1)[-1]})
                    self.chat_note("ai", final)
                final, failed, tried, by = "", "", [], ""
                self.busy = False
                self.emit({"type": "busy", "on": False})
        if self.busy and self.pi is proc:  # the agent ended without an answer (it couldn't start, or it crashed): say so
            self.busy = False
            self.emit({"type": "busy", "on": False})
            self.emit({"type": "answer", "text": "The agent stopped before it could answer (details in logs/pi-stderr.log). Try again; if it keeps happening, check the model settings (Govern › Settings)."})


HUBS: dict[str, Hub] = {}
USAGE_LOCK = threading.Lock()


def usage_totals(days: int = 30, f: Path | None = None) -> dict:
    """logs/usage.jsonl summed per person and per model since `days` ago (answers, tokens, cost, failed answers)."""
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - days * 86400))
    by: dict = {"user": {}, "model": {}}
    f = f or abi.ROOT / "logs" / "usage.jsonl"
    for line in f.read_text().splitlines() if f.exists() else []:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("t", "") < since:
            continue
        for k in by:
            row = by[k].setdefault(r.get(k) or "?", {"answers": 0, "failed": 0, "input": 0, "output": 0, "cache": 0, "cost": 0.0})
            row["answers"] += 1
            row["failed"] += bool(r.get("error"))
            row["input"] += r.get("input", 0)
            row["output"] += r.get("output", 0)
            row["cache"] += r.get("cacheRead", 0) + r.get("cacheWrite", 0)
            row["cost"] = round(row["cost"] + r.get("cost", 0), 6)
    return {"days": days, **{k: [{"name": n, **v} for n, v in sorted(d.items(), key=lambda x: -x[1]["output"])] for k, d in by.items()}}
_SNAP: dict[tuple, dict] = {}
_SNAP_LOCK = threading.Lock()


def cached(project: str, who: str = "") -> dict:
    """One snapshot per project version and person (row-level security), computed as them (abi.as_user)."""
    with _SNAP_LOCK:
        if (project, who) not in _SNAP:
            _SNAP[(project, who)] = snapshot(abi.folder(project), project)
        return _SNAP[(project, who)]


def changed(project: str) -> None:
    def go():
        try:
            abi.sync(project)  # pull ABI's commits, re-render the folder views (writes app.html too)
            with _SNAP_LOCK:
                for k in [k for k in _SNAP if k[0] in (project, pub_id(project))]:
                    _SNAP.pop(k, None)
            for hub in list(HUBS.values()):
                if hub.clients:
                    hub.push_data()
        except Exception as e:  # noqa: BLE001 (a failed refresh keeps the pages as they were)
            for hub in list(HUBS.values()):
                hub.emit({"type": "note", "text": f"Couldn't refresh: {type(e).__name__}"})
    threading.Thread(target=go, daemon=True).start()


SIGNIN = """<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Sign in · ABI</title>
<link rel=stylesheet href="https://fonts.googleapis.com/css2?family=Inter+Tight:wght@400;500;600;700&display=swap"><style>:root{--bg:#f7f7f5;--card:#fff;--ink:#0b0b0b;--mute:#62625e;--line:#e4e4e1;--sig:#ff4f1f;color-scheme:light}@media (prefers-color-scheme:dark){:root{--bg:#0b0b0b;--card:#141414;--ink:#f2f2ef;--mute:#9b9b96;--line:#2a2a28;--sig:#ff6a3d;color-scheme:dark}}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--ink);font:14px/1.5 "Inter Tight",system-ui,sans-serif;-webkit-font-smoothing:antialiased}form{display:flex;flex-direction:column;gap:6px;width:min(360px,calc(100vw - 32px));background:var(--card);border:1px solid var(--line);border-radius:8px;padding:32px}b{display:flex;align-items:center;gap:8px;font:700 20px "Inter Tight",sans-serif;letter-spacing:-.02em;margin-bottom:4px}b::before{content:"";width:12px;height:12px;background:var(--sig);border-radius:2px}label{font-size:12px;font-weight:500;color:var(--mute);margin-top:10px}input,button{font:inherit;padding:10px 12px;border:1px solid var(--line);border-radius:4px;background:var(--card);color:var(--ink)}input:focus{outline:none;border-color:var(--ink);box-shadow:0 0 0 3px color-mix(in srgb,var(--sig) 22%,transparent)}button{margin-top:16px;background:var(--ink);border-color:var(--ink);color:var(--card);font-weight:500;cursor:pointer}button.alt{margin-top:8px;width:100%;background:var(--card);color:var(--ink);border-color:var(--line)}#sso{display:contents}a{color:var(--mute);font-size:12px;margin-top:10px}[hidden]{display:none}.m{min-height:1.4em;color:var(--sig);font-size:13px}small{color:var(--mute)}</style>
<form method=post action=/signin id=in><b>ABI</b><small>Sign in with your ABI account.</small><label for=n>Name or email</label>
<input id=n name=name value="%NAME%" autocomplete=username autofocus required><label for=p>Password</label>
<input id=p name=password type=password autocomplete=current-password required><label for=c %CODE%>Authenticator code</label>
<input id=c name=code inputmode=numeric autocomplete=one-time-code %CODE%><button>Sign in</button><span id=sso></span>
<a href=# id=fg>Forgot your password?</a><span class=m>%MSG%</span></form>
<form id=pw hidden><b>ABI</b><small id=pwt>Choose your password.</small><label for=p1>New password (8 characters or more)</label>
<input id=p1 type=password autocomplete=new-password minlength=8 required><button>Save password</button><span class=m id=pm></span></form>
<script>
const $=s=>document.querySelector(s),m=$('#in .m'),J=(u,b)=>fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})}).then(async r=>({ok:r.ok,...await r.json().catch(()=>({}))}));
const k=location.hash.match(/^#(reset|invite|signup)=([\\w-]+)$/);
if(k){$('#in').hidden=true;$('#pw').hidden=false;$('#pwt').textContent={reset:'Choose a new password.',invite:'Welcome: choose your password.',signup:'Choose a password for your new account.'}[k[1]];
 $('#pw').onsubmit=async e=>{e.preventDefault();const r=await J('/signin/token',{kind:k[1],token:k[2],password:$('#p1').value});$('#p1').value='';
  if(r.ok){history.replaceState(null,'','/');$('#pw').hidden=true;$('#in').hidden=false;m.textContent=r.message}else $('#pm').textContent=r.error||'That didn’t work.'}}
$('#fg').onclick=async e=>{e.preventDefault();const v=$('#n').value.trim();if(!/^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$/.test(v)){m.textContent='Type your email above, then click this again.';return}
 const r=await J('/signin/forgot',{email:v});m.textContent=r.message||r.error};
fetch('/signin/sso').then(r=>r.json()).then(L=>L.forEach(x=>{const b=document.createElement('button');b.type='button';b.className='alt';b.textContent='Sign in with '+x.name;
 b.onclick=async()=>{const r=await J('/api/oidc/'+encodeURIComponent(x.id)+'/start');if(r.url)location.href=r.url;else m.textContent=r.error||'That sign-in isn’t available.'};$('#sso').append(b)})).catch(()=>{});
const q=new URLSearchParams(location.search).get('signin');
if(q==='ok')J('/api/oidc/session').then(r=>{if(r.ok)location.replace('/');else m.textContent=r.error});
else if(q)m.textContent={unknown:'That sign-in isn’t linked to an ABI account yet: sign in with your password once, then link it (click your name).',expired:'The sign-in took too long: try again.',denied:'The sign-in was cancelled.',linked:'Linked: from now on you can sign in with it.'}[q]||'That sign-in didn’t work: try again.';
</script>"""
NO_PROJECT = """<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>ABI</title>
<body style="font:14px/1.5 system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;margin:0;background:#f7f7f5;color:#0b0b0b">
<div style="max-width:340px"><b>Signed in as %NAME%</b><p>You have no project yet. Ask a workspace owner to give you a role in one.</p>
<form method=post action=/signout><button>Sign out</button></form></div>"""



SECTIONS = ["Data", "Data Model", "Visualizations", "Export", "Automate", "Knowledge", "Govern"]  # Start is everyone's
ROUTE_SECTION = {"/edits/rows": "Data", "/upload": "Data", "/report/export": "Export", "/data/export": "Export", "/video/export": "Export",
                 "/packs/file": "Export", "/transfer/": "Export", "/vault/": "Knowledge", "/analyses": "Visualizations", "/monitor": "Visualizations", "/docs/pdf": "Knowledge"}
REPORT_ACTIONS = {"visual_data", "report_snapshot", "export_visual", "save_report", "delete_report"}
_SEC = {"Data": "add_data delete_source delete_table set_steps preview_steps refresh edit_cells approve_edit reject_edit undo_edits create_input_table",
        "Data Model": "add_relationship remove_relationship set_measure delete_measure set_glossary_term delete_glossary_term save_dataset delete_dataset save_model delete_model",
        "Visualizations": "save_report delete_report", "Export": "export_visual report_snapshot", "Govern": "commit undo",
        "Automate": "save_flow delete_flow enable_flow run_flow save_ml_model delete_ml_model train_model score_model save_tool publish_tool"
                    " delete_tool run_tool test_tool save_agent publish_agent delete_agent run_agent test_agent"}
ACTION_SECTION: dict[str, tuple] = {a: (sec,) for sec, acts in _SEC.items() for a in acts.split()} | {"set_table": ("Data", "Data Model")}


def section_of(path: str) -> str | tuple | None:
    """The sidebar section a request acts in (an admin can close it per person); None: reads every section uses."""
    if path.startswith("/api/"):
        hit = ACTION_SECTION.get(path.removeprefix("/api/"))
        return hit[0] if hit and len(hit) == 1 else hit
    return next((v for k, v in ROUTE_SECTION.items() if path.startswith(k)), None)
HIDDEN_DATA = {"Data": ("sources", "source_specs", "table_steps", "layers"), "Knowledge": ("docs",), "Govern": ("history", "rls")}


def viewed(d: dict, acc: dict) -> dict:
    """What a person may see of a snapshot: the admin's sections and reports (people.json); ABI's row-level security
    already shaped the numbers, since the snapshot was computed as them."""
    if not acc:
        return d
    d = dict(d)
    if acc.get("reports") is not None:
        d["reports"] = [r for r in d.get("reports", []) if r["id"] in acc["reports"]]
    hidden = [x for x in SECTIONS if acc.get("sections") is not None and x not in acc["sections"]]
    for sec in hidden:
        for k in HIDDEN_DATA.get(sec, ()):
            d[k] = [] if isinstance(d.get(k), list) else {}
    if "Export" in hidden:
        d["app"] = {**(d.get("app") or {}), "packs": []}
    return d


def idle(hubs, now: float, limit: float = 1800) -> list:
    """Chat agents with nothing to do for `limit` seconds: each holds a process and an ABI session; the next message
    starts the chat's agent again from its own session file."""
    return [h for h in hubs if h.pi and h.pi.poll() is None and not h.busy and now - h.last > limit]


def silent(hubs, now: float, limit: float = 120) -> list:
    """Chats whose agent has worked for `limit` seconds without a sign (a model that doesn't answer, Pi retrying)."""
    return [h for h in hubs if h.busy and now - h.heard > limit and not h.told]


def stale_sessions(people: dict, now: float, ttl: float) -> list:
    """Sign-ins older than the app's 12 hours, also of browsers that never came back."""
    return [sid for sid, s in people.items() if s.get("dead") or now - s["t"] > ttl]


def team_view(t: dict, acc: dict) -> dict:
    """The team on Home: a person the admin restricted gets the plan and what waits, not the journal and brief (their
    handoffs quote figures from every report)."""
    return {**t, "journal": [], "brief": ""} if acc else t


OPS = ("<", "<=", ">", ">=", "=", "!=")


def agent_monitor(project: str, body: dict, by: str) -> list:
    """abi_monitor: list, save or delete one of Monitor's checks (monitor.json "checks", what the page evaluates).
    measure: {measure, op, against: value|measure|prev, value | other | period (calendar column), by?, filters?};
    fresh: {table, hours}; flow: {flow}."""
    mon = render.j(app_dir(project) / "monitor.json", {}) or {}
    checks = mon.get("checks") or []
    if body.get("action") == "delete":
        checks = [c for c in checks if c.get("id") != body.get("id")]
    elif body.get("action") == "save":
        c = body.get("check") or {}
        cid = re.sub(r"[^a-z0-9]+", "_", str(c.get("name") or c.get("id") or "").lower()).strip("_")[:60]
        if not cid:
            raise ValueError("Give the check a name.")
        kind, ag = c.get("kind"), c.get("against", "value")
        need = {"measure": ["measure", "op"] + {"value": ["value"], "measure": ["other"], "prev": ["value", "period"]}.get(ag, ["?"]),
                "fresh": ["table", "hours"], "flow": ["flow"]}.get(kind)
        if need is None or any(c.get(k) in (None, "") for k in need) or (kind == "measure" and c["op"] not in OPS):
            raise ValueError(f"A {kind or '?'} check needs {', '.join(need or ['kind: measure, fresh or flow'])}"
                             + (f"; op is one of {' '.join(OPS)}" if kind == "measure" else "") + ".")
        keep = ("measure", "op", "against", "value", "other", "period", "skip_latest", "by", "filters", "table", "hours", "flow")
        c = {"id": cid, "name": str(c.get("name") or cid)[:120], "kind": kind} | {k: c[k] for k in keep if c.get(k) not in (None, "")}
        checks = [x for x in checks if x.get("id") != cid] + [c]
        if len(checks) > 50:
            raise ValueError("At most 50 checks.")
    if body.get("action") in ("save", "delete"):
        app_save(project, "monitor.json", mon | {"checks": checks}, by)
    return checks


def agent_analysis(project: str, body: dict, by: str) -> list:
    """abi_explore: keep an analysis in Explore's saved analyses (analyses.json, shared in the project), or list them."""
    items = render.j(app_dir(project) / "analyses.json", []) or []
    if body.get("action") == "save":
        q, name = body.get("query") or {}, str(body.get("name") or "").strip()[:120]
        if not name or not q.get("measures"):
            raise ValueError("Give the analysis a name and at least one measure.")
        order = [o for o in q.get("order_by") or [] if isinstance(o, dict) and o.get("field")][:2]
        a = {"name": name, "q": {"measures": q["measures"], "dimensions": (q.get("dimensions") or [])[:2], "filters": q.get("filters") or []}
             | ({"order_by": order} if order else {}) | ({"limit": int(q["limit"])} if str(q.get("limit") or "").isdigit() else {}),
             "type": body.get("chart") if body.get("chart") in ("bar", "line", "table") else "bar", "at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")}
        items = ([a] + [x for x in items if x.get("name") != name])[:100]
        app_save(project, "analyses.json", items, by)
    return items


def engine_model() -> None:
    """ABI's engine gets the agent's model (settings.engine_sync), so the custom agents people build can answer."""
    import settings  # noqa: PLC0415 (sibling script)
    try:
        print("abi-pi:", settings.engine_sync(), file=sys.stderr)
    except (abi.ActionError, OSError, ValueError) as e:
        print("abi-pi: engine model not set:", getattr(e, "body", None) or e, file=sys.stderr)


def serve(default: str, port: int = 8790, open_browser: bool = True, tailnet: bool | str = False, signin: bool = False) -> ThreadingHTTPServer:
    """tailnet: listen on every interface but answer only this machine and Tailscale devices (100.64.0.0/10, your tailnet).
    signin: everyone signs in with their own ABI account; the app then acts as them (their roles, row-level security,
    reports), Developer only for editors and owners, and workspace owners get Users & access."""
    import ipaddress  # noqa: PLC0415
    project, opened = default, set()
    asking = threading.local()  # the hub of this request's thread: a confirmation is redeemed by the browser that asked
    people: dict[str, dict] = {}  # session id -> {name, token, admin, roles, t}: memory only, never written
    tries: dict[str, int] = {}  # wrong sign-ins per client address (ABI throttles too, but sees only this server)

    def role_of(sess: dict | None, p: str) -> str | None:
        """The person's role in a project, as ABI says (cached a minute); one person on this machine: the owner."""
        if not sess:
            return "owner"
        hit = sess["roles"].get(p)
        if hit and time.time() - hit[1] < 60:
            return hit[0]
        try:
            with abi.as_user(sess["token"]):
                r = abi._open("GET", f"/api/projects/{p}/me")[1].get("role")
        except abi.ActionError as e:
            r = None
            if e.status == 401:  # ABI ended the session (signed out elsewhere, account removed): sign in again
                sess["dead"] = True
        sess["roles"][p] = (r, time.time())
        return r

    def access(sess: dict | None, p: str) -> dict:
        """The admin's choice for this person in this project: {sections, reports} (absent: everything)."""
        if not sess or sess["admin"]:
            return {}
        return ((render.j(app_dir(p) / "people.json", {}) or {}).get("users") or {}).get(sess["name"]) or {}

    def may_see_pack(sess: dict | None, p: str, m: dict) -> bool:
        """A pack was rendered under its maker's row-level security: theirs, and the project owners' and admins'."""
        return not sess or sess["admin"] or m.get("made_by") == sess["name"] or role_of(sess, p) == "owner"

    def own_packs(d: dict, sess: dict | None, p: str) -> dict:
        if not sess:
            return d
        return {**d, "app": {**(d.get("app") or {}), "packs": [m for m in (d.get("app") or {}).get("packs", []) if may_see_pack(sess, p, m)]}}

    def mine(sess: dict | None) -> list[str]:
        return [d.name for ws in sorted(abi.WORKSPACES.iterdir()) if ws.is_dir() and not ws.name.startswith((".", "_"))
                for d in sorted(ws.iterdir()) if d.is_dir() and known(d.name) and role_of(sess, d.name)]

    def known(p: str | None) -> bool:
        return bool(p) and bool(abi.PROJECT_ID.match(p)) and not p.endswith("_pub") and abi.folder(p) is not None

    def opening(p: str) -> None:
        if p in opened:
            return
        opened.add(p)
        try:  # Developer works uncommitted: Commit is the person's (or the agent's, when asked); Publish feeds Viewer
            abi.call("set_auto_commit", project=p, on=False)
        except abi.ActionError:
            pass

    opening(default)
    key, pending = secrets.token_urlsafe(24), {}
    agent_key = os.environ.get("ABI_APP_AGENT_KEY") or secrets.token_urlsafe(24)
    host = f"127.0.0.1:{port}"
    url = ""

    def hub_for(cid: str, mobile: bool = False, want: str | None = None, sess: dict | None = None) -> Hub:
        cid = re.sub(r"[^\w-]", "", cid)[:40] or "default"
        hid = f"{sess['name']}/{cid}" if sess else cid  # a browser's hub is its signed-in person's, never another's (Hub.hid)
        h = HUBS.get(hid)
        if not h:
            first = default if role_of(sess, default) else next(iter(mine(sess)), default)
            h = HUBS[hid] = Hub(first, url, agent_key, cid, mobile, sess["name"] if sess else "", sess["token"] if sess else None)
        if sess:
            h.token = sess["token"]
        if known(want) and role_of(sess, want):
            opening(want)
            h.use(want)
        if mobile != h.mobile:  # follows the browser: a window made narrow and wide again leaves the phone layout
            h.mobile = mobile
            if mobile and h.mode != "viewer":  # every window of this browser shows what the server now enforces
                h.mode = "viewer"
                h.emit({"type": "mode", "mode": "viewer", "mobile": True, "published": bool(abi.folder(pub_id(h.project)))})
        if role_of(sess, h.project) not in ("editor", "owner"):
            h.mode = "viewer"  # a viewer's browser has no Developer
        h.view = lambda d, p=h.project: own_packs(viewed(d, access(sess, p)), sess, p)
        return h

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # no request log: bodies may carry business values
            pass

        def end_headers(self):  # every answer: never inside another site's frame (clickjacking), no sniffing, no referrer
            for k, v in (("X-Frame-Options", "SAMEORIGIN"), ("Content-Security-Policy", "frame-ancestors 'self'"),
                         ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer")):
                self.send_header(k, v)
            super().end_headers()

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

        def session(self) -> dict | None:
            c = re.search(r"abi_u=([\w-]+)", self.headers.get("Cookie", ""))
            s = people.get(c.group(1)) if c else None
            if s and (s.get("dead") or time.time() - s["t"] > 12 * 3600):
                people.pop(c.group(1), None)
                abi.logout(s["token"])
                return None
            return s

        def guarded(self, handle) -> None:
            self.sess = self.session() if signin else None
            try:
                with abi.as_user(self.sess and self.sess["token"]):
                    return handle()
            except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError, SystemExit):  # malformed input: JSON, numbers (1e309), missing fields, a helper's refusal
                with contextlib.suppress(OSError):
                    self.reply(400, b'{"error":"That request isn\'t valid."}')
            except urllib.error.URLError:  # ABI itself didn't answer (down, restarting, unreachable): say so
                with contextlib.suppress(OSError):
                    self.reply(503, b'{"error":"ABI isn\'t answering right now. Try again in a minute."}')
            except OSError:
                pass  # the browser went away
            except Exception as e:  # noqa: BLE001 (never a dropped connection; no details to the page, they may carry values)
                at = traceback.extract_tb(e.__traceback__)[-1]  # the operator gets the kind and the line, never the message
                print(f"error {type(e).__name__} at {Path(at.filename).name}:{at.lineno} on {self.path}", file=sys.stderr, flush=True)
                with contextlib.suppress(OSError):
                    self.reply(500, b'{"error":"Something went wrong. Try again."}')

        def do_GET(self):
            self.guarded(self.get)

        def do_POST(self):
            self.guarded(self.post)

        def signin_page(self, msg: str = "", name: str = "", code: bool = False) -> None:
            self.reply(200, SIGNIN.replace("%MSG%", html.escape(msg)).replace("%NAME%", html.escape(name))
                       .replace("%CODE%", "" if code else "hidden").encode(), "text/html; charset=utf-8")

        def start(self, name: str, tok: str) -> None:
            """A signed-in browser (ABI's session token stays here; the browser gets only the app's own cookie)."""
            admin = False
            with abi.as_user(tok):
                try:
                    admin = any(u["name"] == name and u.get("workspace_owner") for u in abi.call("list_users").get("users", []))
                except abi.ActionError:
                    pass
            sid = secrets.token_urlsafe(32)
            people[sid] = {"name": name, "token": tok, "admin": admin, "roles": {}, "t": time.time()}
            tls = "; Secure" if self.headers.get("X-Forwarded-Proto", "").lower() == "https" else ""  # behind an HTTPS proxy
            self.send_header("Set-Cookie", f"abi_u={sid}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200{tls}")

        def account(self, raw: bytes) -> None:
            """Before sign-in: a reset link by email, a link's new password, SSO's start and its last step. ABI does the
            work; the browser sees ABI's own words only (a 422 would repeat the password), never a token or a key."""
            if not self.headers.get("Content-Type", "").startswith("application/json"):  # other sites can't send JSON without asking
                return self.reply(415, b'{"error":"JSON only"}')
            try:
                body = json.loads(raw)
            except ValueError:
                return self.reply(400, b'{"error":"bad JSON"}')
            words = lambda b, d: (lambda x: x.get("detail") if isinstance(x.get("detail"), str) else (x.get("detail") or {}).get("message") or d)(  # noqa: E731
                json.loads(b) if b.startswith(b"{") else {})
            if self.path == "/signin/forgot":
                st, b, _ = abi.raw("POST", "/api/password-reset", {"email": str(body.get("email") or "")[:254]})
                if st == 404:
                    return self.reply(200, b'{"error":"Email isn\'t set up here: ask a workspace owner to set a new password for you (Users & access)."}')
                return self.reply(200, json.dumps({"message": words(b, "If an account has this email, a link is on its way.") if st == 202 else None,
                                                   "error": None if st == 202 else "Too many requests: try again later."}).encode())
            if self.path == "/signin/token":
                kind, tok, pw = body.get("kind"), str(body.get("token") or ""), str(body.get("password") or "")
                if kind not in ("reset", "invite", "signup") or not re.fullmatch(r"[\w-]{1,100}", tok) or not 8 <= len(pw) <= 1024:
                    return self.reply(400, b'{"error":"A password of 8 characters or more."}')
                st, b, _ = abi.raw("POST", {"reset": "/api/password-reset/confirm", "invite": "/api/invite/confirm", "signup": "/api/signup/confirm"}[kind],
                                   {"token": tok, "password": pw})
                pw = ""
                ok = st == 200
                return self.reply(200 if ok else 400, json.dumps({"message": "Saved: sign in with your new password."} if ok else
                                                                 {"error": "That link is used or expired: ask for a new one." if st in (400, 401, 404, 410) else words(b, "That didn't work.")}).encode())
            jar = re.search(r"(?:^|;\s*)(abi_oidc=[\w-]+)", self.headers.get("Cookie", ""))
            if self.path == "/api/oidc/session":
                st, b, _ = abi.raw("POST", "/api/oidc/session", {}, {"Cookie": jar.group(1)} if jar else {})
                if st != 200:
                    return self.reply(401, b'{"error":"That sign-in didn\'t finish: try again."}')
                d = json.loads(b)
                self.send_response(200)
                self.start(d["name"], d["token"])
                self.send_header("Set-Cookie", "abi_oidc=; HttpOnly; SameSite=Lax; Path=/api/oidc; Max-Age=0")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
                return None
            # start: a sign-in, or linking your own account when signed in (your session's ABI token goes along)
            st, b, hd = abi.raw("POST", self.path, {}, {"Authorization": f"Bearer {self.sess['token']}"} if self.sess and body.get("link") else {})
            if st != 200:
                return self.reply(st if st in (404, 429, 502) else 400, json.dumps({"error": words(b, "That sign-in isn't available.")}).encode())
            self.send_response(200)
            if c := hd.get("set-cookie") or hd.get("Set-Cookie"):
                self.send_header("Set-Cookie", c)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return None

        def authed(self) -> bool:
            if not self.allowed():
                return False
            if signin:
                return bool(self.sess)
            return self.headers.get("Host") == host  # --single: this machine only (no DNS rebinding)

        def get(self):
            route, _, query = self.path.partition("?")
            qs = dict(x.split("=", 1) for x in query.split("&") if "=" in x)
            if signin and self.allowed() and route == "/signin/sso":  # the sign-in page's SSO buttons (id and name only)
                st, b, _ = abi.raw("GET", "/api/oidc/providers")
                return self.reply(200, b if st == 200 else b"[]")
            if signin and self.allowed() and re.fullmatch(r"/api/oidc/[\w-]{1,64}/callback", route):  # back from the IdP
                jar = re.search(r"(?:^|;\s*)(abi_oidc=[\w-]+)", self.headers.get("Cookie", ""))
                st, _, hd = abi.raw("GET", self.path, headers={"Cookie": jar.group(1)} if jar else {})
                out = re.search(r"[?&]signin=(\w+)", hd.get("location") or hd.get("Location") or "")
                self.send_response(303)
                self.send_header("Location", f"/?signin={out.group(1) if out else 'failed'}")  # always this app, never another address
                self.end_headers()
                return None
            if not self.authed():
                if signin and self.allowed() and route in ("/", "/app.html"):
                    return self.signin_page()
                return self.reply(404, b"{}")
            if route == "/events":  # EventSource can't send headers: the page key and this browser's id come in the query
                if not secrets.compare_digest(qs.get("k", ""), key):
                    return self.reply(403, b"{}")
                hub = hub_for(qs.get("c", ""), qs.get("m") == "1", urllib.parse.unquote(qs.get("p", "")), self.sess)
                project = hub.project
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
            if route.startswith("/api/artifacts/"):  # a file ABI rendered (a chat's PDF link), fetched as this person
                rel = route.removeprefix("/api/artifacts/")
                proj = rel.split("/", 1)[0]
                if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}/[\w.-]{1,100}", rel) or not known(proj.removesuffix("_pub")) or not role_of(self.sess, proj.removesuffix("_pub")):
                    return self.reply(404, b"{}")
                try:
                    data = abi.artifact(rel)
                except (urllib.error.HTTPError, abi.ActionError):
                    return self.reply(404, b"{}")
                kind = {".pdf": "application/pdf", ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                        ".png": "image/png", ".csv": "text/csv"}.get(Path(rel).suffix, "application/octet-stream")
                return self.reply(200, data, kind)
            if route not in ("/", "/app.html"):
                return self.reply(404, b"{}")
            want = urllib.parse.unquote(qs.get("p", ""))
            p = want if known(want) and role_of(self.sess, want) else default if role_of(self.sess, default) else next(iter(mine(self.sess)), None)
            if not p:
                return self.reply(200, NO_PROJECT.replace("%NAME%", html.escape(self.sess["name"])).encode(), "text/html; charset=utf-8")
            opening(p)
            me = {"name": self.sess["name"] if self.sess else None, "admin": bool(not self.sess or self.sess["admin"]), "signin": signin,
                  "role": role_of(self.sess, p), **access(self.sess, p)}
            # the page's data as this person (their row-level security, sections and reports); live, never the synced file
            # a person who can't edit reads only the published copy, from the first page on (release run 31 Oct: the first
            # page came from the working project, uncommitted work included, and only later requests went to the copy)
            cur = pub_id(p) if me["role"] not in ("editor", "owner") and abi.folder(pub_id(p)) else p
            page = page_html(p, own_packs(viewed(cached(cur, self.sess["name"] if self.sess else ""), access(self.sess, p)), self.sess, p))
            self.reply(200, page.replace(KEY_SLOT, key, 1).replace(ME_SLOT, json.dumps(me).replace("</", "<\\/"), 1).encode(), "text/html; charset=utf-8")

        def post(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 64 << 20:  # read the rest away first: closing on unread bytes resets the line before the answer arrives
                left = min(n, 256 << 20)  # beyond that the line is simply closed
                while left > 0 and (got := len(self.rfile.read(min(left, 1 << 20)))):
                    left -= got
                self.close_connection = True
                return self.reply(413, b'{"error":"That request is too large (64 MB at most)."}')
            raw = self.rfile.read(n) or b"{}"
            if self.path == "/signin" and signin and self.allowed():  # ABI checks the password; the app keeps only the session
                ip = self.client_address[0]
                f = urllib.parse.parse_qs(raw.decode(errors="replace"))
                name, code = f.get("name", [""])[0].strip()[:254], (f.get("code", [""])[0].strip() or None)
                status, b = abi.login(name, f.get("password", [""])[0], code)
                f = None  # the password is not kept
                if status != 200:
                    tries[ip] = tries.get(ip, 0) + 1
                    time.sleep(min(20, 2 ** min(tries[ip], 4)))
                    msg = "Set up the authenticator for this account in ABI first." if b.get("mfa") == "setup" else b.get("message") or "Sign-in failed."
                    return self.signin_page(msg, name, b.get("mfa") == "code")
                tries.pop(ip, None)
                self.send_response(303)
                self.start(name, b["token"])
                self.send_header("Location", "/")
                self.end_headers()
                return None
            if signin and self.allowed() and self.path in ("/signin/forgot", "/signin/token", "/api/oidc/session") or (
                    signin and self.allowed() and re.fullmatch(r"/api/oidc/[\w-]{1,64}/start", self.path)):
                return self.account(raw)
            if self.path == "/signout" and signin:
                c = re.search(r"abi_u=([\w-]+)", self.headers.get("Cookie", ""))
                gone = people.pop(c.group(1), None) if c else None
                if gone:
                    abi.logout(gone["token"])
                for hid in [k for k, h in HUBS.items() if gone and h.user == gone["name"]]:
                    if HUBS[hid].pi and HUBS[hid].pi.poll() is None:
                        HUBS[hid].pi.terminate()
                    HUBS.pop(hid, None)
                self.send_response(303)
                self.send_header("Set-Cookie", "abi_u=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
                self.send_header("Location", "/")
                self.end_headers()
                return None
            body = json.loads(raw)
            project = default
            if self.path.startswith("/agent/"):  # the harness on this machine, with the agent key: steer pages, data changed
                if self.client_address[0] != "127.0.0.1" or not secrets.compare_digest(self.headers.get("X-Abi-Agent", ""), agent_key):
                    return self.reply(403, b'{"error":"forbidden"}')
                cid = self.headers.get("X-Abi-Client", "")
                # the app's chat agent names its hub; a terminal agent names none and steers the Developer pages
                targets = [HUBS[cid]] if cid in HUBS else [] if cid else [h for h in HUBS.values() if h.mode == "developer"]
                if self.path == "/agent/mode":
                    h0 = targets[0] if targets else None
                    mode = h0.mode if h0 else "viewer" if cid else "developer"  # an unknown browser fails closed
                    return self.reply(200, json.dumps({"mode": mode, "project": h0.current() if h0 else default}).encode())
                if self.path == "/agent/show":
                    for h0 in [h for h in targets if body.get("project") in (h.project, pub_id(h.project))]:
                        h0.emit({"type": "show", **{k: body.get(k) for k in ("page", "report", "filters", "item", "note", "query", "chart")}})
                    p = str(body.get("project", ""))
                    if cid and known(p):  # another project (one the chat just made): that browser opens it; its load checks access
                        page = f"rep:{body['report']}" if body.get("page") == "report" and body.get("report") else str(body.get("page") or "home")
                        for h0 in [h for h in targets if p not in (h.project, pub_id(h.project))]:
                            h0.emit({"type": "open", "project": p, "page": page})
                elif self.path in ("/agent/monitor", "/agent/analysis"):  # the app's own files the agent may write (abi_monitor, abi_explore)
                    p = str(body.get("project", "")).removesuffix("_pub")
                    if not known(p):
                        return self.reply(400, b'{"error":"no such project"}')
                    if self.path == "/agent/monitor" and any(h.mode == "viewer" for h in targets):
                        return self.reply(403, b'{"error":"Viewer mode is read-only: monitors are made in Developer."}')
                    by = next((h.user for h in targets if h.user), "")
                    try:
                        out = agent_monitor(p, body, by) if self.path == "/agent/monitor" else agent_analysis(p, body, by)
                    except ValueError as e:
                        return self.reply(400, json.dumps({"error": str(e)}).encode())
                    changed(p)
                    return self.reply(200, json.dumps({"result": out}).encode())
                elif self.path == "/agent/changed":
                    changed(body["project"].removesuffix("_pub") if known(str(body.get("project", "")).removesuffix("_pub")) else default)
                return self.reply(200, b"{}")
            if not self.authed():
                return self.reply(403, b'{"error":"forbidden"}')
            hub = hub_for(self.headers.get("X-Abi-Client", ""), self.headers.get("X-Abi-Mobile") == "1", self.headers.get("X-Abi-Project"), self.sess)
            project, asking.hid = hub.project, hub.hid
            acc = access(self.sess, project)
            sec = section_of(self.path)
            if self.path == "/team/answer" and acc.get("sections") is not None:  # a step the team left runs in its own section
                import team  # noqa: PLC0415 (sibling script)
                with contextlib.suppress(SystemExit):  # no team in this project: nothing waits
                    sec = section_of("/api/" + next((x["action"] for x in team.pending(project) if x["id"] == body.get("id")), ""))
            secs = sec if isinstance(sec, tuple) else (sec,) if sec else ()
            if acc.get("sections") is not None and secs and not set(secs) & set(acc["sections"]):
                return self.reply(403, json.dumps({"error": f"{' / '.join(secs)} isn't open to you in this project; ask an admin."}).encode())
            rid = body.get("report")
            if acc.get("reports") is not None and rid and (self.path.split("/")[-1] in REPORT_ACTIONS or sec == "Export") and rid not in acc["reports"]:
                return self.reply(403, json.dumps({"error": "That report isn't open to you; ask an admin."}).encode())
            if self.path == "/s3key":  # an S3 source's keys as two ABI secrets bound to the bucket's host (owners; never shown again)
                tbl, kid, sec = str(body.get("table") or ""), str(body.get("key_id") or ""), str(body.get("secret") or "")
                m = re.match(r"s3://([A-Za-z0-9.-]+)/", str(body.get("location") or ""))
                ep, region = str(body.get("endpoint") or ""), str(body.get("region") or "us-east-1")
                if not (re.fullmatch(r"\w{1,60}", tbl) and m and kid and sec and re.fullmatch(r"[a-z0-9-]{1,50}", region)
                        and re.fullmatch(r"[A-Za-z0-9.-]+(:\d+)?", ep or "x")):
                    return self.reply(400, b'{"error":"An s3:// folder, its region, and both the access key ID and the secret key."}')
                # the host ABI's engine sends to (its _host): the endpoint with path style, else <bucket>.s3.<region>.amazonaws.com
                host = ep.split(":")[0].lower() if ep else f"{m[1]}.s3.{region}.amazonaws.com".lower()
                name = "S3_" + tbl.upper()
                for suffix, value in (("_KEY_ID", kid), ("_SECRET", sec)):
                    r = run("set_secret", {"name": name + suffix, "value": value, "allowed_hosts": [host]}, None)
                    if "error" in r:
                        return self.reply(200, json.dumps({"error": r["error"]}).encode())
                return self.reply(200, json.dumps({"result": {"key_id": f"${{secret:{name}_KEY_ID}}", "secret": f"${{secret:{name}_SECRET}}"}}).encode())
            if self.path == "/password" and self.sess:  # your own password, with your current one (ABI's set_password)
                cur, new = str(body.get("current") or ""), str(body.get("password") or "")
                if not cur or not 8 <= len(new) <= 1024:  # checked here: ABI's 422 would echo the password
                    return self.reply(400, b'{"error":"Your current password, and a new one of 8 characters or more."}')
                r = run("set_password", {"current": cur, "password": new}, None)
                if "result" in r:  # ABI signed this account out everywhere: so does the app
                    people.pop(next((k for k, v in people.items() if v is self.sess), ""), None)
                return self.reply(200, json.dumps(r, default=str).encode())
            if self.path.startswith("/admin/"):
                if self.sess and not self.sess["admin"]:
                    return self.reply(403, b'{"error":"Users & access is for workspace owners."}')
                return self.reply(*admin_route(hub, self.path.removeprefix("/admin/"), body))
            # the page: a local Host (no DNS rebinding) and the per-start key (a custom header other sites can't send)
            if not secrets.compare_digest(self.headers.get("X-Abi-App", ""), key):
                return self.reply(403, b'{"error":"forbidden"}')
            if self.path == "/projects":  # the switcher: every workspace folder and its projects (ABI's synced copies here)
                out = {}
                for ws in sorted(abi.WORKSPACES.iterdir()) if abi.WORKSPACES.exists() else []:
                    if ws.is_dir() and not ws.name.startswith((".", "_")):
                        ps = [d.name for d in sorted(ws.iterdir()) if d.is_dir() and known(d.name) and role_of(self.sess, d.name)]
                        if ps:
                            out[ws.name] = [{"id": x, "name": (render.j(ws / x / "project.json", {}) or {}).get("name") or x} for x in ps]
                return self.reply(200, json.dumps({"result": {"workspaces": out, "current": project, "workspace": abi.folder(project).parent.name}}).encode())
            if self.path == "/mode":  # the person's switch (the agent can't call this: it has no page key); a phone stays Viewer
                hub.mode = "viewer" if body.get("mode") == "viewer" or hub.mobile or role_of(self.sess, project) not in ("editor", "owner") else "developer"
                hub.emit({"type": "mode", "mode": hub.mode, "mobile": hub.mobile, "published": bool(abi.folder(pub_id(project)))})
                threading.Thread(target=hub.push_data, daemon=True).start()
                return self.reply(200, b"{}")
            if self.path == "/publish":
                if hub.mode == "viewer":
                    return self.reply(403, b'{"error":"Publish from Developer."}')
                pub, bundle, head = publish_bundle(project)
                if head and (published(project).get("commit") or "").split(" ")[0] == head.split(" ")[0]:  # same commit: only layouts
                    publish_layouts(project, hub.user)
                    publish_roles(project, pub)
                    changed(project)
                    return self.reply(200, json.dumps({"result": published(project)}).encode())
                out = run("import_project", {"project": pub, "bundle": bundle}, None, lambda: published_now(project, pub, head))
                return self.reply(200, json.dumps(out, default=str).encode())
            if self.path == "/docs/pdf":  # the documentation as a PDF, printed by the engine's Chromium
                pdf = docs_pdf(project)
                self.send_response(200)
                for k, v in (("Content-Type", "application/pdf"), ("Content-Length", str(len(pdf))),
                             ("Content-Disposition", f'attachment; filename="{project}-documentation.pdf"')):
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(pdf)
                return None
            if self.path == "/upload":  # a person's file into a table (IN-7 resumable upload, as abi.py load)
                if hub.mode == "viewer":
                    return self.reply(403, b'{"error":"Viewer mode is read-only."}')
                import base64  # noqa: PLC0415
                import tempfile  # noqa: PLC0415
                uid, name = str(body.get("id") or ""), Path(str(body.get("name"))).name
                if not re.fullmatch(r"[0-9a-f]{32}", uid) or not name:
                    return self.reply(400, b'{"error":"That upload isn\'t valid."}')
                d = Path(tempfile.gettempdir()) / "abi-uploads" / uid  # ponytail: an abandoned upload's pieces stay until the temp folder is cleared
                d.mkdir(parents=True, exist_ok=True)
                f = d / name
                with f.open("ab") as part:
                    part.write(base64.b64decode(body.get("data") or ""))
                if not body.get("last"):
                    return self.reply(200, json.dumps({"result": {"received": f.stat().st_size}}).encode())
                try:
                    out = {"result": abi.upload(project, str(body.get("table")), f)}
                except abi.ActionError as e:
                    out = {"error": str(e.body.get("message") if isinstance(e.body, dict) else e.body)}
                finally:
                    shutil.rmtree(d, ignore_errors=True)
                changed(project)
                return self.reply(200, json.dumps(out, default=str).encode())
            if self.path == "/edits/rows":
                q = urllib.parse.urlencode({"offset": max(0, int(body.get("offset") or 0)), "limit": min(200, int(body.get("limit") or 50))})
                try:
                    t = urllib.parse.quote(str(body.get("table", "")), safe="")
                    return self.reply(200, json.dumps({"result": abi._open("GET", f"/api/projects/{hub.current()}/tables/{t}/rows?{q}")[1]}, default=str).encode())
                except abi.ActionError as e:
                    return self.reply(200, json.dumps({"error": (e.body or {}).get("message") if isinstance(e.body, dict) else str(e.body)}).encode())
            if self.path == "/report/export":  # ABI renders the report (report_snapshot); the page gets the file
                args = {k: body[k] for k in ("report", "page", "pages", "visual", "filters", "format") if body.get(k) not in (None, [], "")}
                try:
                    snap = abi.call("report_snapshot", project=hub.current(), **args)["snapshot"]
                    return self.reply(200, abi.artifact(snap), {"pdf": "application/pdf", "png": "image/png"}.get(args.get("format"),
                                      "application/vnd.openxmlformats-officedocument.presentationml.presentation"))
                except abi.ActionError as e:
                    b = e.body if isinstance(e.body, dict) else {}
                    return self.reply(400, json.dumps({"error": b.get("message") or str(e.body)}).encode())
            if self.path == "/data/export":  # one view's rows as CSV or Excel (ABI's export_visual, as the person)
                try:
                    link = abi.call("export_visual", project=hub.current(), **{k: body.get(k) for k in ("report", "page", "visual", "format")},
                                    filters=body.get("filters") or [])["link"]
                    return self.reply(200, abi.artifact(link.removeprefix("/api/artifacts/")), "text/csv" if body.get("format") == "csv" else
                                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                except abi.ActionError as e:
                    return self.reply(400, json.dumps({"error": (e.body or {}).get("message") if isinstance(e.body, dict) else str(e.body)}).encode())
            if self.path == "/video/export":  # a silent explainer of report views (scripts/video.py: ABI's rows, the app's theme)
                import tempfile  # noqa: PLC0415
                import video  # noqa: PLC0415 (sibling script)
                try:
                    w, ht = (int(x) for x in str(body.get("size") or "1920x1080").split("x"))
                    props = video.build(hub.current(), str(body.get("report")), body.get("pages") or None, body.get("views") or None,
                                        "ink" if body.get("theme") == "ink" else "paper", min(15.0, max(2.0, float(body.get("seconds") or 6))), (w, ht))
                    with tempfile.TemporaryDirectory() as tmp:
                        return self.reply(200, video.render(props, Path(tmp) / "deck.mp4").read_bytes(), "video/mp4")
                except (SystemExit, abi.ActionError, ValueError, subprocess.CalledProcessError) as e:
                    return self.reply(400, json.dumps({"error": str(getattr(e, "body", None) or e)[:400]}).encode())
            if self.path == "/packs/file":  # a file of a board pack, only one its manifest lists
                import pack  # noqa: PLC0415 (sibling script)
                m = next((x for x in pack.listing(project) if x["id"] == body.get("pack")), None)
                names = {i["file"] for i in (m or {}).get("items", []) if i.get("file")} | {"notes.md", "manifest.json"}
                if not m or body.get("file") not in names or not may_see_pack(self.sess, project, m):
                    return self.reply(404, b'{"error":"no such file in that pack"}')
                return self.reply(200, (pack.home(project) / m["id"] / body["file"]).read_bytes(), "application/octet-stream")
            if self.path == "/analyses":  # Explore's saved analyses: the app's file, not the project, so Viewer may keep them too
                items = body.get("analyses")
                if not isinstance(items, list) or len(items) > 100 or len(raw) > 200_000:
                    return self.reply(400, b'{"error":"at most 100 analyses"}')
                app_save(project, "analyses.json", items, hub.user)
                return self.reply(200, b'{"result":"saved"}')
            if self.path == "/pins":  # a person's own Home: what they pinned (the app's file, per person, with history)
                who = hub.user or "me"
                allp = render.j(app_dir(project) / "pins.json", {}) or {}
                if "pins" in body:
                    pins = body["pins"]
                    if not isinstance(pins, list) or len(pins) > 60 or len(raw) > 100_000 or not all(isinstance(x, dict) and x.get("kind") for x in pins):
                        return self.reply(400, b'{"error":"at most 60 pins, each with a kind"}')
                    app_save(project, "pins.json", allp | {who: pins}, hub.user)
                return self.reply(200, json.dumps({"result": (render.j(app_dir(project) / "pins.json", {}) or {}).get(who, [])}).encode())
            if self.path == "/layouts":  # a page's card layout: Developer edits the draft, Publish copies it for Viewer
                if hub.mode == "viewer":
                    return self.reply(403, b'{"error":"Arrange pages in Developer mode."}')
                page, lay = body.get("page"), body.get("layout")
                if not isinstance(page, str) or not 0 < len(page) <= 200 or len(raw) > 100_000 or not (lay is None or isinstance(lay, dict)):
                    return self.reply(400, b'{"error":"a page and its layout"}')
                allp = render.j(app_dir(project) / "layouts.json", {}) or {}
                draft = {k: v for k, v in (allp.get("draft") or {}).items() if k != page} | ({page: lay} if lay else {})
                app_save(project, "layouts.json", allp | {"draft": draft}, hub.user)
                changed(project)
                return self.reply(200, json.dumps({"result": draft}).encode())
            if self.path == "/monitor":  # Monitor's own checks (monitor.json): what a person wants watched
                if hub.mode == "viewer":
                    return self.reply(403, b'{"error":"Make monitors in Developer mode."}')
                checks = body.get("checks")
                if not isinstance(checks, list) or len(checks) > 50 or len(raw) > 100_000 or not all(isinstance(c, dict) and c.get("kind") in ("measure", "fresh", "flow") for c in checks):
                    return self.reply(400, b'{"error":"at most 50 checks, each a measure, fresh or flow check"}')
                app_save(project, "monitor.json", (render.j(app_dir(project) / "monitor.json", {}) or {}) | {"checks": checks}, hub.user)
                changed(project)
                return self.reply(200, b'{"result":"saved"}')
            if self.path.startswith("/transfer/"):  # the whole project as one zip, and back (Developer only)
                import base64  # noqa: PLC0415
                import transfer  # noqa: PLC0415 (sibling script)
                if hub.mode == "viewer":
                    return self.reply(403, json.dumps({"error": "Export and import are in Developer mode."}).encode())
                if self.path == "/transfer/export":
                    return self.reply(200, transfer.export(project), "application/zip")
                try:
                    data = base64.b64decode(body.get("data", ""))
                    z = transfer.read_zip(data)
                except Exception as e:  # noqa: BLE001 (any unreadable upload)
                    return self.reply(400, json.dumps({"error": f"Not an export zip: {e}"}).encode())
                if not body.get("apply"):
                    open_work = len((abi.call("changes", project=project) or {}).get("files") or [])  # an import replaces these too
                    return self.reply(200, json.dumps(run("import_project", {"project": project, "bundle": z["abi"], "dry_run": True}, None)
                                                      | {"manifest": z["manifest"], "uncommitted": open_work}, default=str).encode())
                return self.reply(200, json.dumps(run("import_project", {"project": project, "bundle": z["abi"]}, None,
                                                      lambda: transfer.extras(project, z)), default=str).encode())
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
                    return self.reply(200, json.dumps({"result": team_view(team.status(hub.current() if hub.mode == "viewer" else project), acc)}, default=str).encode())
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
                cid = self.path.split("/")[-1]
                job = pending.pop(cid, None) if pending.get(cid, (None,) * 5)[4] == hub.hid else None
                if not job:
                    return self.reply(410, b'{"error":"That confirmation expired; try again."}')
                return self.reply(200, json.dumps(run(*job[:4]), default=str).encode())  # job: action, args, token, after, hub
            action = self.path.removeprefix("/api/")
            if action not in ALLOWED or body.get("project") not in (project, pub_id(project)):  # Viewer's page names the published copy
                return self.reply(403, json.dumps({"error": f"'{action}' is not available here; use ABI's app."}).encode())
            if hub.mode == "viewer" and (action in WRITES or action in READS_ONLY):
                return self.reply(403, json.dumps({"error": "Viewer mode is read-only. Switch to Developer in the top bar to change things."}).encode())
            body = {**body, "project": hub.current()}  # Viewer reads the published copy
            out = run(action, body, None)
            if "result" in out and action in WRITES:
                changed(project)
            self.reply(200, json.dumps(out, default=str).encode())

    def admin_route(hub: Hub, op: str, body: dict) -> tuple[int, bytes]:
        """Users & access (workspace owners): ABI's own accounts and roles, as the admin; plus the app's sections and
        reports per person (people.json). Roles go to the project and, read-only, to its published copy."""
        p, pub = hub.project, pub_id(hub.project)
        if hub.mode == "viewer":
            return 403, (b'{"error":"Access and settings are managed from a computer, in Developer."}' if hub.mobile
                         else b'{"error":"Switch to Developer to manage access."}')
        if op == "users":
            try:
                users = abi.call("list_users").get("users", [])
            except abi.ActionError as e:
                return 200, json.dumps({"error": (e.body or {}).get("message", "ABI refused list_users")}).encode()
            reps = (abi.call("list_reports", project=p) or {}).get("reports") or []
            return 200, json.dumps({"result": {"project": p, "users": users, "sections": SECTIONS, "published": bool(abi.folder(pub)),
                                               "reports": [{"id": r["id"], "name": r.get("name") or r["id"]} for r in reps],
                                               "access": (render.j(app_dir(p) / "people.json", {}) or {}).get("users") or {}}}, default=str).encode()
        if op == "usage":  # the agents' model work: tokens and cost per person and model, last 30 days
            return 200, json.dumps({"result": usage_totals()}).encode()
        if op.startswith("settings"):  # the agent's model, reasoning and providers (keys go in, never come out)
            import settings  # noqa: PLC0415 (sibling script)
            try:
                if op == "settings/provider":
                    out = settings.save_provider(body.get("provider") or {}, str(body.get("key") or "") or None)
                elif op == "settings/remove":
                    out = settings.remove_provider(str(body.get("id", "")))
                elif op == "settings/default":
                    out = settings.set_default(body.get("model") or None, str(body.get("thinking") or "medium"), body.get("fallbacks"))
                elif op == "settings/test":
                    out = settings.test(str(body.get("id", "")))
                elif op == "settings/models":  # the form's model list, fetched from the provider (the key stays here)
                    return 200, json.dumps({"result": settings.models(body.get("provider") or {}, str(body.get("key") or "") or None)}).encode()
                else:
                    out = settings.public()
            except ValueError as e:
                return 400, json.dumps({"error": str(e)}).encode()
            if op in ("settings/provider", "settings/remove", "settings/default"):  # a change, not a read or a test
                threading.Thread(target=engine_model, daemon=True).start()  # ABI's custom agents use the same model
                for h in HUBS.values():  # the next message starts each chat's agent with the new settings
                    if h.pi and h.pi.poll() is None and not h.busy:
                        h.pi.terminate()
                        h.pi = None
            return 200, json.dumps({"result": out}).encode()
        who = str(body.get("user") or "").strip()
        if op == "role":
            role = body.get("role")
            if not who or role not in ("viewer", "editor", "owner", "none"):
                return 400, b'{"error":"a user and a role (viewer, editor, owner or none)"}'

            def mirror():
                if abi.folder(pub):
                    try:
                        if role == "none":
                            abi.call("remove_role", project=pub, user=who)
                        else:
                            abi.call("set_role", project=pub, user=who, role="viewer")
                    except abi.ActionError:
                        return "Saved; set the published copy's role in ABI too."
                return "Saved through ABI."
            if role == "none":
                return 200, json.dumps(run("remove_role", {"project": p, "user": who}, None, mirror), default=str).encode()
            return 200, json.dumps(run("set_role", {"project": p, "user": who, "role": role}, None, mirror), default=str).encode()
        if op == "adduser":  # a local account with a starting password the owner hands over (no email needed)
            name, pw = str(body.get("name") or "").strip(), str(body.get("password") or "")
            if not re.fullmatch(r"[\w.@+-]{2,64}", name) or not 8 <= len(pw) <= 1024:  # checked here: ABI's 422 would echo the password
                return 400, b'{"error":"A name (letters, digits, . _ - @ +) and a password of 8 characters or more."}'
            return 200, json.dumps(run("create_user", {"name": name, "password": pw}, None), default=str).encode()
        if op == "setpass":  # a new password for someone who forgot theirs (ABI asks the owner to confirm, then signs them out)
            pw = str(body.get("password") or "")
            if not who or not 8 <= len(pw) <= 1024:
                return 400, b'{"error":"A person and a password of 8 characters or more."}'
            return 200, json.dumps(run("set_password", {"name": who, "password": pw}, None), default=str).encode()
        if op == "mail":  # the account mail server: invites and "forgot your password" links (its password: an ABI secret)
            import settings  # noqa: PLC0415 (sibling script)
            if body.get("off"):
                r = run("set_account_mail", {"smtp": None}, None)
                if "result" in r:
                    settings.note("mail", None)
                return 200, json.dumps(r, default=str).encode()
            smtp = {"host": str(body.get("host") or "").strip(), "port": body.get("port"), "from": str(body.get("from") or "").strip(),
                    "security": body.get("security")}
            user, pw = str(body.get("username") or "").strip(), str(body.get("password") or "")
            if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", smtp["host"]) or not isinstance(smtp["port"], int) or smtp["security"] not in ("starttls", "tls", "none"):
                return 400, b'{"error":"A host, a port and how to connect (STARTTLS, TLS or none)."}'
            if user:
                if pw:
                    r = run("set_secret", {"name": "SMTP_PASSWORD", "value": pw, "allowed_hosts": [smtp["host"].lower()]}, None)
                    if "error" in r:
                        return 200, json.dumps(r).encode()
                smtp |= {"username": user, "password": "${secret:SMTP_PASSWORD}"}
            r = run("set_account_mail", {"smtp": smtp}, None)
            if "result" in r:
                settings.note("mail", {k: v for k, v in smtp.items() if k != "password"})
            return 200, json.dumps(r, default=str).encode()
        if op == "sso":  # the sign-in providers people may use (OIDC), and whether links can reach this app
            st, b, _ = abi.raw("GET", "/api/oidc/providers")
            public = os.environ.get("ABI_PUBLIC_URL", "")
            import settings  # noqa: PLC0415
            return 200, json.dumps({"result": {"providers": json.loads(b) if st == 200 else [], "public_url": public,
                                               "reachable": bool(public) and not re.match(r"https?://(127\.|localhost)", public),
                                               "callback": f"{public.rstrip('/')}/api/oidc/<id>/callback", "mail": settings.load().get("mail")}}).encode()
        if op == "sso/save":
            pid, name, issuer, cid, sec = (str(body.get(k) or "").strip() for k in ("id", "name", "issuer", "client_id", "secret"))
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", pid) or not name or not re.fullmatch(r"https://[^\s?#]{1,490}", issuer) or not cid:
                return 400, b'{"error":"An ID (lowercase), a name, the issuer (https://...) and the client ID."}'
            try:  # the client secret goes to the IdP's token endpoint: bind it to that host and the issuer's
                with urllib.request.urlopen(issuer.rstrip("/") + "/.well-known/openid-configuration", timeout=10) as r0:
                    hosts = sorted({urllib.parse.urlsplit(u).hostname for u in (issuer, json.loads(r0.read(1 << 20)).get("token_endpoint") or issuer)})
            except (OSError, ValueError):
                return 200, b'{"error":"Can\'t read that issuer\'s OpenID configuration (/.well-known/openid-configuration)."}'
            ref = f"OIDC_{pid.upper().replace('-', '_')}_SECRET"
            if sec:
                r = run("set_secret", {"name": ref, "value": sec, "allowed_hosts": hosts}, None)
                if "error" in r:
                    return 200, json.dumps(r).encode()
            return 200, json.dumps(run("set_oidc_provider", {"id": pid, "name": name, "issuer": issuer, "client_id": cid,
                                                             "client_secret": f"${{secret:{ref}}}"}, None), default=str).encode()
        if op == "sso/remove":
            return 200, json.dumps(run("delete_oidc_provider", {"id": str(body.get("id") or "")}, None), default=str).encode()
        if op == "invite":
            email = str(body.get("email") or "").strip()
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                return 400, b'{"error":"an email address"}'
            return 200, json.dumps(run("invite_user", {"email": email}, None), default=str).encode()
        if op == "access":
            secs, reps = body.get("sections"), body.get("reports")
            known_reps = {r["id"] for r in (abi.call("list_reports", project=p) or {}).get("reports") or []}
            if not who or (secs is not None and not set(secs) <= set(SECTIONS)) or (reps is not None and not set(reps) <= known_reps):
                return 400, b'{"error":"sections and reports from this project only"}'
            f = app_dir(p) / "people.json"
            data = render.j(f, {}) or {}
            users = data.setdefault("users", {})
            users[who] = {k: v for k, v in (("sections", secs), ("reports", reps)) if v is not None}
            if not users[who]:
                users.pop(who)
            app_save(p, "people.json", data, (hub.user if hub else "") or "an admin")
            for s0 in people.values():  # their next page and data follow it
                s0["roles"].pop(p, None)
            return 200, b'{"result":"saved"}'
        return 404, b"{}"

    def vault_route(hub: Hub, op: str, body: dict) -> tuple[int, bytes]:
        import base64  # noqa: PLC0415
        import tempfile  # noqa: PLC0415

        import vault  # noqa: PLC0415 (sibling script)
        if op in ("add", "rm") and hub.mode == "viewer":
            return 403, b'{"error":"Viewer mode is read-only. Switch to Developer to add or remove documents."}'
        try:
            if op == "list":
                out = vault.listing(hub.project)
            elif op == "search":
                out = vault.search(hub.project, str(body.get("query", "")), int(body.get("k") or 6))
            elif op == "read":
                out = vault.read(hub.project, Path(str(body.get("name"))).name)
            elif op == "add":
                name = Path(str(body.get("name"))).name
                with tempfile.TemporaryDirectory() as d:
                    f = Path(d) / name
                    f.write_bytes(base64.b64decode(body.get("data") or ""))
                    out = vault.add(hub.project, f, name)
            elif op == "rm":
                out = vault.remove(hub.project, Path(str(body.get("name"))).name)
            else:
                return 404, b"{}"
        except SystemExit as e:  # vault's own messages (unsupported type, missing pypdf)
            return 200, json.dumps({"error": str(e)}).encode()
        except FileNotFoundError:  # an OSError: guarded() would take it for a closed browser and drop the request
            return 404, b'{"error":"That document isn\'t in the vault."}'
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
                changed(str(args.get("project") or default).removesuffix("_pub"))
            return out
        except abi.ActionError as e:
            b = e.body if isinstance(e.body, dict) else {"message": str(e.body)}
            if e.status == 409 and b.get("confirm_token") and token is None:
                cid = secrets.token_urlsafe(12)
                pending[cid] = (action, args, b["confirm_token"], after, getattr(asking, "hid", None))  # the page never sees the token
                preview = json.dumps(b["preview"], indent=1, default=str)[:4000] if b.get("preview") else ""
                return {"confirm": {"id": cid, "message": b.get("message", "Confirm?"), "preview": preview}}
            detail = "\n".join((f"{x['error']} {x.get('fix') or ''}" if x.get("error") else  # ABI's checks; a 422 names the field
                                 f"{'.'.join(map(str, x.get('loc') or []))}: {x.get('msg')}").strip() for x in b.get("errors") or [] if isinstance(x, dict))
            return {"error": "\n".join(filter(None, [b.get("message"), detail])) or json.dumps(b)[:500]}

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        request_queue_size = 128  # socketserver's 5: Linux resets the 6th waiting connection (a burst from one page)
    srv = Server(("0.0.0.0" if tailnet else "127.0.0.1", port), H)
    host = f"127.0.0.1:{srv.server_address[1]}"  # port 0 = any free port
    url = f"http://{host}"
    srv.hub = hub_for("default")
    threading.Thread(target=engine_model, daemon=True).start()
    srv.hubs = HUBS

    def watch() -> None:  # changes made outside this app (an agent team in a terminal, ABI's own app): follow ABI's history
        last: dict = {}
        while True:
            try:
                now = time.time()
                for h in idle(list(HUBS.values()), now):  # an idle chat agent: its process and session go
                    h.pi.terminate()
                    h.pi = None
                for h in silent(list(HUBS.values()), now):  # the model hasn't answered: say so, the person decides
                    h.told = True
                    h.emit({"type": "note", "text": "The model hasn't answered for 2 minutes. Press Stop and check Govern › Settings, or keep waiting."})
                for sid in stale_sessions(people, now, 12 * 3600):
                    gone = people.pop(sid, None)
                    if gone:
                        abi.logout(gone["token"])
                for p in {h.project for h in HUBS.values()} | {default}:
                    head = ((abi.call("history", project=p).get("commits") or [{}])[0].get("id"),
                            len((abi.call("changes", project=p) or {}).get("files") or []))
                    if p in last and head != last[p]:
                        changed(p)
                    last[p] = head
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
    tailnet = "open" if "--open" in argv else "--tailnet" in argv  # --open: any device that can reach this machine
    signin = "--single" not in argv
    if not signin and tailnet:
        raise SystemExit("--single has no sign-in: it acts as this machine's ABI account, so it stays on this machine. Use sign-in (the default) for other devices.")
    argv = [a for a in argv if a not in ("--tailnet", "--open", "--single")]
    try:
        s = serve(argv[0], int(argv[1]) if len(argv) > 1 else 8790, open_browser=not tailnet, tailnet=tailnet, signin=signin)
    except urllib.error.URLError:  # no ABI to talk to
        raise SystemExit(f"Can't reach ABI at {abi.URL}: start ABI (or set ABI_URL) and try again.") from None
    except subprocess.CalledProcessError:  # ABI answers but the service account's sign-in was refused
        raise SystemExit(f"ABI at {abi.URL} refused the app's sign-in: check the account in data/accounts.txt, or wait 15 minutes"
                         " if there were many failed sign-ins (ABI's throttle).") from None
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

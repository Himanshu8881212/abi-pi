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
import hashlib
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
import tomllib
import traceback
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import abi  # noqa: E402
import render  # noqa: E402
import video  # noqa: E402

KEY_SLOT = "__ABI_APP_KEY__"
ME_SLOT = "null/*__ABI_ME__*/"
VERSION = tomllib.loads((abi.ROOT / "pyproject.toml").read_text())["project"]["version"]
READS_ONLY ={"run_flow", "run_agent",  # not changes to the project, but they act (send, call tools): Developer only
             "run_optimizer"}  # 1.2.6 M12: works out a plan and stores it as a proposal a person approves
WRITES = {"set_rls_rule", "set_measure", "delete_measure", "set_glossary_term", "delete_glossary_term", "save_model", "delete_model", "enable_flow", "train_model", "score_model", "refresh",
          "commit", "undo", "add_data", "delete_source", "delete_table", "set_table", "add_relationship", "remove_relationship",
          "save_dataset", "delete_dataset", "save_report", "delete_report", "save_flow", "delete_flow", "save_ml_model",
          "delete_ml_model", "save_tool", "publish_tool", "delete_tool", "save_agent", "publish_agent", "delete_agent", "set_steps", "edit_cells", "approve_edit", "reject_edit", "undo_edits", "create_input_table",
          "run_object_action",  # 1.2.6 M7: an object page's or a form card's action, a write-back as the person (Developer only, as every write)
          "decide_same"}  # 1.2.6 M11: a person's decision that objects are the same one, or not (ABI asks them to confirm)
READS = {"changes", "check_condition", "data_quality", "dataset_data", "docs", "edit_history", "edit_proposals", "export_visual",
         "flow_runs", "history", "how_calculated", "list_agents", "list_flows", "list_reports", "list_tools", "load_runs",
         "ml_models", "preview_steps", "readiness", "report_snapshot", "rls_rules", "run_tool", "semantic_model",
         "semantic_query", "test_agent", "test_tool", "visual_data", "ontology", "get_object", "list_objects", "object_history",  # 1.2.6 M6, M7: object reads
         "find_same",  # 1.2.6 M11: what looks like the same object (Editors and Owners; it merges nothing)
         "optimizers"}  # 1.2.6 M12: the plans and each one's latest run, as this person may read it
ALLOWED = READS | WRITES | READS_ONLY  # the page's actions; every other one is ABI's app's
ACCOUNT = {"set_password", "create_personal_token", "revoke_personal_token", "start_mfa", "confirm_mfa", "disable_mfa"}  # the confirmations Viewer mode still redeems: account work (a password, personal tokens, two-step sign-in), your own or, as an Owner, another person's

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
aside a.src{display:block;margin:20px 12px 0;color:var(--mute);font-size:12px;text-decoration:none}aside a.src:hover{color:var(--ink);text-decoration:underline}
table.prices{table-layout:fixed}table.prices td,table.prices th{padding:4px;white-space:normal}table.prices th:first-child{width:42%}table.prices input.f{width:100%;padding:4px 6px}
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
input.f:focus,textarea.f:focus,select.f:focus{outline:none;border-color:var(--ink);box-shadow:0 0 0 3px color-mix(in srgb,var(--ink) 10%,transparent)}
label.l{display:block;font:500 12px var(--text);color:var(--mute);margin:16px 0 0}.card>label.l:first-of-type{margin-top:0}
.msg{font-size:13px;margin:8px 0;white-space:pre-wrap;color:var(--mute)}.msg:empty{display:none}.out{margin-top:16px}.out:empty{display:none}
.chip.x{cursor:pointer;background:var(--ink);border-color:var(--ink);color:var(--sheet)}.chip.x:after{content:'  \00d7'}
.fbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;position:relative;margin:0 0 16px}.fchips{display:contents}
.fchip{display:inline-flex;align-items:center;gap:6px;max-width:380px;padding:3px 3px 3px 12px;border:1px solid var(--ink);border-radius:999px;background:var(--sheet);font-size:13px;cursor:pointer}
.fchip b{font-weight:500;color:var(--mute);white-space:nowrap}.fchip span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fchip button{flex:none;border:0;background:none;color:var(--mute);width:24px;height:24px;border-radius:50%;cursor:pointer;font-size:16px;line-height:1}
.fchip button:hover{background:color-mix(in srgb,var(--ink) 10%,transparent);color:var(--ink)}
.btn.fadd{border-style:dashed;padding:5px 12px;border-radius:999px}.flink{border:0;background:none;padding:2px 0;color:var(--mute);font:inherit;font-size:13px;cursor:pointer;text-decoration:underline;text-underline-offset:3px;align-self:flex-start}.flink:hover{color:var(--ink)}.flink[hidden]{display:none}
.fpop{position:absolute;top:calc(100% + 6px);left:0;z-index:30;width:min(340px,calc(100vw - 32px));display:flex;flex-direction:column;gap:8px;padding:12px;background:var(--sheet);border:1px solid var(--line);border-radius:8px;box-shadow:0 10px 32px color-mix(in srgb,#000 18%,transparent)}
.fpop .list{max-height:280px;overflow:auto;display:flex;flex-direction:column;margin:0 -4px}.fpop .list:empty{display:none}
.fopt{display:flex;gap:10px;align-items:center;padding:6px 8px;border-radius:4px;font-size:13px;cursor:pointer}.fopt:hover{background:color-mix(in srgb,var(--ink) 6%,transparent)}.fopt input{accent-color:var(--ink);margin:0}
.slicer{display:flex;flex-direction:column;gap:8px;min-height:0;max-height:100%}.slicer>input.f{padding:6px 10px;font-size:13px}
.slicer .opts{display:flex;flex-wrap:wrap;gap:6px;align-content:flex-start;overflow:auto;min-height:0;padding-bottom:10px;mask-image:linear-gradient(to bottom,#000 calc(100% - 18px),transparent)}
.sl{display:inline-flex;align-items:baseline;gap:6px;padding:5px 12px;border:1px solid var(--line);border-radius:999px;background:var(--sheet);color:var(--ink);font:13px/1.2 var(--text);cursor:pointer;transition:background .12s,border-color .12s}
.sl:hover{border-color:var(--ink)}.sl small{color:var(--mute);font-size:11px;font-variant-numeric:tabular-nums}.sl[hidden]{display:none}
.sl.on{background:var(--ink);border-color:var(--ink);color:var(--sheet)}.sl.on small{color:color-mix(in srgb,var(--sheet) 70%,transparent)}
.sl:focus-visible,.fchip button:focus-visible,.flink:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.grid.busy{opacity:.55;transition:opacity .15s;pointer-events:none}
dialog{border:1px solid var(--line);border-radius:8px;background:var(--sheet);color:var(--ink);max-width:640px;padding:28px}
dialog h3{margin:0 0 12px;font:600 18px var(--display)}dialog .row{margin-top:20px;justify-content:flex-end}dialog pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:40vh}dialog::backdrop{background:color-mix(in srgb,#000 45%,transparent);backdrop-filter:blur(2px)}
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
header .right .btn{padding:6px 12px}header #chat-toggle{background:var(--signal);border-color:var(--signal);color:#fff}header #chat-toggle:hover{background:color-mix(in srgb,var(--signal) 86%,#000);border-color:color-mix(in srgb,var(--signal) 86%,#000)}
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
.card.sized{overflow:auto}.card.sized>.body{flex:1;min-height:0}.card.sized>.body svg{height:100%}.pick [data-r]{cursor:pointer}
body.laying .lay>*{outline:1px dashed var(--signal);outline-offset:-1px;cursor:grab;position:relative;user-select:none}body.laying .lay>*.drag{opacity:.4}body.laying .lay>*.over{box-shadow:inset 3px 0 0 var(--signal)}
.rz{position:absolute;right:0;bottom:0;width:18px;height:18px;cursor:nwse-resize;background:linear-gradient(135deg,transparent 50%,var(--signal) 50%);z-index:2;opacity:0;transition:opacity .12s;display:none}body.laying .rz{display:block;opacity:1}:has(>.rz){position:relative}body[data-mode=viewer] .rz{display:none}
#layout.on{background:var(--signal);color:#fff;border-color:var(--signal)}.mobile #layout{display:none}
.layers{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}.layer h4{margin:0}.layer table{margin-top:8px}
.mobile .layers{grid-template-columns:1fr}
.hits{display:flex;flex-direction:column;gap:12px;margin-top:12px}.hit{border-left:2px solid var(--signal);padding:2px 0 2px 12px;white-space:pre-wrap}.hit .file{margin:0 0 4px}
.toast{position:fixed;left:50%;bottom:calc(24px + env(safe-area-inset-bottom,0px));transform:translateX(-50%);background:var(--ink);color:var(--sheet);padding:10px 16px;border-radius:6px;font-size:13px;z-index:9;max-width:calc(100vw - 32px)}
.card.hl,.person.hl,.rowcard.hl{outline:0;animation:hlf 2.4s ease-out}@keyframes hlf{0%,45%{background-color:var(--hover);box-shadow:inset 3px 0 0 var(--ink)}100%{}}.person.just{box-shadow:inset 3px 0 0 var(--ink);padding-left:12px}.state.new{color:var(--ink);background:var(--hover)}.state.new::before{background:var(--ink);opacity:1}
.cards.rows{grid-template-columns:1fr;gap:0;border:1px solid var(--line);border-radius:var(--r);background:var(--sheet);overflow:hidden}
.rows>.card{border:0;border-radius:0;border-top:1px solid var(--line)}.rows>.card:first-child,.rows>.rowhead+.card{border-top:0}
.rows>.rowcard,.rowhead{display:grid;grid-template-columns:minmax(160px,1fr) minmax(200px,1.5fr) minmax(200px,1.5fr) 200px;gap:16px;align-items:start;padding:12px 16px}
.rowhead{font-size:12px;font-weight:500;color:var(--mute);padding-block:10px;border-bottom:1px solid var(--line);background:var(--paper)}
.rows>.rowcard>h3,.rowcard .fld>label.l{display:none}.rows>.rowcard>*{margin:0;min-width:0}.rowcard .fld .f{width:100%;margin:0}.rowcard .fld .file{margin-top:4px}
.rowcard textarea.f{min-height:38px}.rowcard>.acts{padding:0;justify-content:flex-end;flex-wrap:nowrap}.rowcard>.msg{grid-column:1/-1}
.rowcard>.acts .btn{padding:6px 10px}.rowcard:not(.dirty)>.acts .btn.pri{display:none}.rowcard:hover{background:var(--hover)}
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
#chat .mdl{white-space:normal;margin:4px 0;padding-left:22px}#chat .mdl li{margin:2px 0}#chat .mdl .mdl{margin:2px 0}
#chat .mdh{display:block;font-size:15px;margin:8px 0 2px}#chat .mdc{white-space:pre;overflow-x:auto;background:var(--hover,#f1f1ee);padding:8px 10px;border-radius:6px;font-size:12.5px;margin:6px 0}
#chat .m a{color:inherit;text-decoration:underline;text-decoration-color:var(--signal)}
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
/* 1.2.5: the same Swiss signal, finished as a tool. Every page says what it is (eyebrow, title, one line of purpose) above
   one primary action; icons sit beside words; card controls stay quiet until hover; async parts show a skeleton;
   errors read as errors; on a phone every target is 44 px and the drawer has a backdrop */
/* the sidebar's white column runs the page's full height: a grid item of its own beside main (the sticky aside is a
   screen tall, a long page went on below it on paper) */
aside{border-right:0;grid-column:1;grid-row:2;z-index:2}main{grid-column:2;grid-row:2}
@media (min-width:821px){html:not(.mobile) body:not(.side-off)::after{content:"";grid-column:1;grid-row:2;background:var(--sheet);border-right:1px solid var(--line);z-index:1;pointer-events:none}}
body.side-off main,.mobile main{grid-column:1}@media (min-width:821px){html:not(.mobile) body:not(.side-off) aside{background:transparent}}@media (max-width:820px){aside,main{grid-column:auto;grid-row:auto}}
.ic{width:16px;height:16px;flex:none;fill:none;stroke:currentColor;stroke-width:1.75;stroke-linecap:round;stroke-linejoin:round}
/* header */
header{gap:12px;padding-block:10px;box-shadow:0 1px 0 var(--line);border-bottom:0}
header .right{gap:6px}header .right .btn{height:32px;padding:0 12px}header .right .btn.icon{width:32px;padding:0}
.vc{gap:6px;padding-right:6px;margin-right:2px;border-right:1px solid var(--line)}
header #chat-toggle{gap:6px;box-shadow:0 1px 2px color-mix(in srgb,var(--signal) 40%,transparent)}#chat-toggle .long{margin-left:-3px}
.btn.who{gap:8px;padding:0 10px 0 4px!important}.btn.who .av{display:inline-grid;place-items:center;width:24px;height:24px;border-radius:50%;background:var(--ink);color:var(--sheet);font:600 11px/1 var(--display)}
form.so{display:contents}.brand{font-size:16px}.crumb{padding:4px 8px;margin-left:-8px;border-radius:var(--r);transition:background .12s}.crumb:hover{background:var(--hover)}
.modes{border-radius:8px;padding:3px}.modes button{border-radius:5px;height:26px;padding:0 12px}.modes button.on{box-shadow:0 1px 2px rgb(0 0 0/.08),0 0 0 1px var(--line)}
/* buttons: one height, one radius; primary ink, quiet secondary, danger only on hover, a spinner while busy */
.btn{height:34px;padding:0 14px;gap:7px;border-radius:var(--r);border-color:var(--line);font:500 13px/1 var(--text);letter-spacing:-.005em;box-shadow:0 1px 1px rgb(11 11 11/.03);
 transition:background .12s,border-color .12s,box-shadow .12s,color .12s,transform .08s;justify-content:center}
.btn:hover{border-color:var(--line-strong);background:var(--hover)}.btn:active:not(:disabled){transform:translateY(1px)}
.btn.pri{box-shadow:0 1px 2px rgb(11 11 11/.14)}.btn.pri:hover{background:color-mix(in srgb,var(--ink) 84%,var(--sheet));border-color:color-mix(in srgb,var(--ink) 84%,var(--sheet))}
.btn .ic{width:15px;height:15px;margin-left:-3px;opacity:.85}.btn.icon{width:34px;padding:0}.btn.icon .ic{margin:0;opacity:1}
.btn.danger:hover{color:var(--red);border-color:color-mix(in srgb,var(--red) 45%,var(--line));background:color-mix(in srgb,var(--red) 6%,var(--sheet))}
.btn:focus-visible,.crumb:focus-visible,aside button:focus-visible,.modes button:focus-visible,button.pin:focus-visible,nav.tabs button:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.btn.busy .ic{display:none}.btn.busy::before{content:"";width:13px;height:13px;margin-left:-2px;border-radius:50%;border:2px solid currentColor;border-right-color:transparent;animation:spin .7s linear infinite;flex:none;opacity:.8}
@keyframes spin{to{transform:rotate(360deg)}}
.btn:disabled{opacity:.4}.btn.busy:disabled{opacity:.8}
/* sidebar */
aside{padding:14px 10px 40px}aside .sec{margin-bottom:10px}
aside button.grp{padding:8px 10px 6px;font:600 10.5px/1 var(--display);letter-spacing:.07em;color:color-mix(in srgb,var(--mute) 85%,var(--sheet))}
aside button.it{justify-content:flex-start;gap:10px;padding:6px 10px;font-size:13.5px;color:color-mix(in srgb,var(--ink) 86%,var(--mute));border-radius:var(--r)}
aside button.it .ic{color:var(--mute);transition:color .12s}aside button.it .lb{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
aside button.it:hover{color:var(--ink)}aside button.it:hover .ic{color:var(--ink)}
aside button.it.on{background:var(--hover);color:var(--ink);font-weight:600;box-shadow:inset 0 0 0 1px var(--line)}aside button.it.on .ic{color:var(--signal)}
aside button.it.on::before{display:none}aside button.it .n{font:500 11px var(--text);font-variant-numeric:tabular-nums;color:var(--mute);min-width:18px;text-align:right}
/* the page: eyebrow, title, purpose; actions on the right */
main{padding:28px 40px 80px}.pg{gap:20px}
.pghead{justify-content:space-between;align-items:flex-end;gap:16px 24px;padding-bottom:4px}
.pghead .ttl{margin-right:auto;min-width:min(100%,280px);max-width:780px;display:flex;flex-direction:column;gap:4px}
.pghead .eyebrow{font:600 11px/1 var(--display);letter-spacing:.07em;text-transform:uppercase;color:var(--mute)}
.pghead h1{font:650 26px/1.15 var(--display);letter-spacing:-.025em;margin:2px 0 0;text-wrap:balance}
.pghead .ttl p{margin:4px 0 0;color:var(--mute);font-size:14px;line-height:1.5;max-width:72ch;text-wrap:pretty}
.pghead>.bar{flex-wrap:wrap;justify-content:flex-end}.pghead .bar.seg{flex-basis:100%;order:3;justify-content:flex-start}
.ttl .bar.lead{margin:0 0 6px!important;order:-1}.ttl .bar.lead .btn{height:auto;padding:2px 0;border:0;background:none;box-shadow:none;color:var(--mute);font-size:12.5px}.ttl .bar.lead .btn:hover{color:var(--ink)}
body[data-mode=viewer] .pghead{display:flex!important}body[data-mode=viewer] .pghead>.bar:not(:has(>:not(.w))){display:none}
#pgname{display:none}
/* cards */
.card{padding:20px 22px 22px}.card>*+*{margin-top:14px}
.card h3{font:600 14px/1.35 var(--display);letter-spacing:-.008em;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:14px}
.card h3>span:first-child{flex:1 1 auto;min-width:0}.card h3 .file{order:5}.card h3 .tools,.card h3 button.pin{flex:none}
.card h3 .tools{gap:2px}.card h3 .tools button{height:26px;padding:0 8px;border-radius:var(--r);font:500 12px var(--text)}
.card h3 .tools .btn.icon{width:28px;height:28px;border-color:transparent;box-shadow:none;background:none;color:var(--mute)}.card h3 .tools .btn.icon:hover{background:var(--hover);color:var(--ink)}
.card h3 button.pin:not(.on){opacity:0;transition:opacity .12s}.card:hover h3 button.pin,.card:focus-within h3 button.pin,.mobile .card h3 button.pin{opacity:1}
button.pin{height:24px;padding:0 10px;font:500 11.5px/1 var(--text);border-color:var(--line)}
button.pin.on{background:color-mix(in srgb,var(--signal) 10%,var(--sheet));border-color:color-mix(in srgb,var(--signal) 40%,var(--line));color:var(--signal-ink)}
.pinned .acts{display:none}.pinned>h3{margin-bottom:10px}.pinned .body .kpi .v{margin-top:6px}
.kpi .v{font-feature-settings:"tnum","ss01"}
/* async parts: a skeleton until their rows arrive */
.pinned .body:empty,.grid>.card>.body:empty{min-height:72px;border-radius:var(--r);background:linear-gradient(100deg,var(--hover) 30%,color-mix(in srgb,var(--hover) 35%,var(--sheet)) 50%,var(--hover) 70%) 0 0/300% 100%;animation:shim 1.4s ease-in-out infinite}
.pinned.wide .body:empty,.grid>.card:not([style*="span 3"])>.body:empty{min-height:180px}
@keyframes shim{from{background-position:100% 0}to{background-position:0 0}}
/* messages: an error reads as one, a success as one */
.msg.bad,.card .bad{color:var(--red)}.msg.ok{color:var(--ok)}
.msg.bad::before{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;background:currentColor;margin:0 8px 1px 0}
.pg>p.bad{padding:14px 16px;border:1px solid color-mix(in srgb,var(--red) 30%,var(--line));border-radius:var(--r-lg);background:color-mix(in srgb,var(--red) 5%,var(--sheet))}
.toast{border-radius:8px;padding:11px 16px;box-shadow:var(--shadow-pop);animation:rise .18s cubic-bezier(.2,.8,.2,1)}.toast.bad{background:var(--red);color:#fff}
@keyframes rise{from{opacity:0;transform:translate(-50%,8px)}}
/* empty states */
.pg>p.empty,.cards>p.empty{padding:44px 24px;border:1px dashed var(--line-strong);border-radius:var(--r-lg);background:var(--sheet);color:var(--mute);font-size:14px;line-height:1.55}
/* forms */
input.f,textarea.f,select.f{border-radius:var(--r);min-height:36px}input.f::placeholder,textarea.f::placeholder{color:color-mix(in srgb,var(--mute) 80%,var(--sheet))}
label.l{font:500 12.5px var(--text);color:color-mix(in srgb,var(--ink) 70%,var(--mute))}
/* tables in cards */
.card table th{font-size:11.5px;letter-spacing:.01em;padding-block:9px;border-bottom-color:var(--line)}.card table td{padding-block:9px}
.card table tr:last-child td{border-bottom:0}
/* dialogs: one surface, a soft pop */
dialog{border-radius:14px;padding:26px 28px;box-shadow:var(--shadow-pop);width:min(560px,calc(100vw - 32px));max-width:calc(100vw - 32px)}
dialog[open]{animation:pop .18s cubic-bezier(.2,.8,.2,1)}@keyframes pop{from{opacity:0;transform:translateY(8px) scale(.985)}}
dialog>.card{border:0;box-shadow:none;padding:0;background:none}dialog>.card>*+*{margin-top:10px}
dialog h3{font:650 18px/1.3 var(--display);letter-spacing:-.015em;margin:0 0 14px}dialog .row{margin-top:22px}dialog .acts{flex-wrap:wrap}
dialog::backdrop{background:rgb(11 11 11/.36);backdrop-filter:blur(3px)}
dialog input.f{display:block;width:100%;margin-top:6px}
#commit{width:min(760px,calc(100vw - 32px))}#confirm{width:min(520px,calc(100vw - 32px))}
/* filter bar and popover */
.fpop{border-radius:12px;box-shadow:var(--shadow-pop);padding:14px}.btn.fadd{height:30px;border-style:dashed;border-color:var(--line-strong);box-shadow:none}
.fchip{height:30px;border-color:var(--line-strong);background:var(--sheet);box-shadow:var(--shadow)}
nav.tabs{gap:2px}nav.tabs button{padding:9px 12px;font-size:13px}
/* Home */
.areas .lk{border-radius:var(--r-lg);background:var(--sheet);transition:border-color .12s,box-shadow .12s}.areas .lk:hover{box-shadow:var(--shadow)}
.lk:focus-visible,.sg:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
/* the chat */
#chat{box-shadow:-12px 0 32px -20px rgb(11 11 11/.25)}#chat header .btn{height:32px}#chat .me{border-radius:14px 14px 4px 14px}
#chat .compose textarea{border-radius:10px}#chat .send{height:46px}
/* phone: 44 px targets, a header of icons, the drawer over a backdrop */
.mobile header{padding-block:6px;gap:8px}.mobile header .right{gap:6px;flex-wrap:nowrap}
.mobile .btn{height:44px;min-height:44px;padding:0 16px;font-size:14px}.mobile .btn.icon,.mobile header .right .btn.icon{width:44px;height:44px}
.mobile header .right .btn{height:40px;min-height:40px}.mobile header .btn.icon{width:40px!important;height:40px}.mobile .btn.who{padding:0 8px!important}.mobile .btn.who .nm{display:none}.mobile .btn.who .av{width:26px;height:26px}
.mobile .brand{font-size:0}.mobile .crumb{display:none}.mobile #pgname{display:block;font-size:15px}
.mobile #chat-toggle{padding:0 14px}
.mobile body.menu-open::after{content:"";position:fixed;inset:0;background:rgb(11 11 11/.38);z-index:7;animation:fade .2s}
@keyframes fade{from{opacity:0}}
.mobile body.menu-open aside{box-shadow:12px 0 32px -16px rgb(11 11 11/.3)}.mobile aside{box-shadow:none;top:0;padding-top:max(16px,env(safe-area-inset-top,0px));z-index:12}
.mobile aside button.it{min-height:44px;font-size:15px}.mobile aside button.grp{min-height:36px}
.mobile .pghead{align-items:stretch}.mobile .pghead>.bar{justify-content:flex-start}.mobile .pghead h1{font-size:23px}.mobile .pghead .ttl p{font-size:13.5px}
.mobile .card h3 .tools .btn.icon{width:36px;height:36px}.mobile button.pin{height:32px;padding:0 12px}
.mobile .sl,.mobile .fchip{min-height:36px}.mobile .btn.fadd{height:36px}.mobile nav.tabs button{min-height:44px}
.mobile input.f,.mobile select.f{min-height:44px;font-size:16px}.mobile main{padding-top:20px}
.mobile .side-close{display:flex}.side-close{display:none}
@media (max-width:820px){body{background:var(--paper)}}
/* status badges and plain-words lines (flows, sources) */
.state{display:inline-flex;align-items:center;gap:6px;height:22px;padding:0 9px;border-radius:999px;font:500 11.5px/1 var(--text);color:var(--mute);background:var(--hover);flex:none}
.state::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor;opacity:.6}.state.on{color:var(--ok);background:color-mix(in srgb,var(--ok) 10%,var(--sheet))}.state.on::before{opacity:1}
.state.bad{color:var(--red);background:color-mix(in srgb,var(--red) 8%,var(--sheet))}
.when{display:flex;align-items:center;gap:8px;font-size:13.5px}.when .ic{color:var(--mute)}
.stepchips{display:flex;flex-wrap:wrap;align-items:center;gap:6px}.stepchips .chip{margin:0;font-size:12px;padding:3px 9px;background:var(--paper)}.stepchips .arr{color:var(--mute);font-size:12px}
.chip{border-color:var(--line);color:color-mix(in srgb,var(--ink) 80%,var(--mute))}
.mpick{display:flex;flex-wrap:wrap;gap:6px;max-height:156px;overflow:auto;padding:2px}.mpick .sl{font-size:12.5px;padding:5px 11px}
.person{padding:14px 0;border-top:1px solid var(--line)}.person:first-of-type{border-top:0;padding-top:4px}
.ptop{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.pwho{display:flex;align-items:center;gap:10px;flex:1;min-width:220px}
.pwho .av,.who .av{display:inline-grid;place-items:center;width:28px;height:28px;border-radius:50%;background:var(--hover);color:var(--ink);font:600 12px/1 var(--display);box-shadow:inset 0 0 0 1px var(--line)}
.ptop select.f{width:180px;margin:0!important}.pmore{margin-top:8px}.pmore summary{cursor:pointer;color:var(--mute);font-size:13px;padding:4px 0;list-style-position:inside}.pmore summary:hover{color:var(--ink)}
.pmore[open] summary{margin-bottom:4px}.pmore .row .f{flex:1;max-width:420px}.person>.msg{margin:8px 0 0}
.editor:has(>details.json:not([open]))>.acts{display:none}details.json>summary{cursor:pointer;color:var(--mute);font-size:13px;padding:4px 0}details.json>summary:hover{color:var(--ink)}details.json>textarea{margin-top:8px;width:100%}
.two{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.4fr);gap:12px}.two .fld .f,.card>.fld .f{width:100%;margin-top:6px}.card>.fld>label.l,.two label.l{margin:0}
.tcard:not(.dirty) .acts .btn.pri{background:var(--sheet);color:var(--ink);border-color:var(--line);box-shadow:0 1px 1px rgb(11 11 11/.03)}
details.cols>summary{cursor:pointer;color:var(--mute);font-size:13px;padding:2px 0}details.cols>summary:hover{color:var(--ink)}details.cols[open]>summary{margin-bottom:8px}
.tcard label.l:has(input[type=checkbox]){display:flex;align-items:center;gap:8px;margin-top:2px}
.mtext{margin:0;font-size:13.5px;line-height:1.5}
.card>.item{position:relative;margin:0;padding:12px 0 12px 18px;border-top:1px solid var(--line)}.card>h3+.item{border-top:0;padding-top:0}
.card>.item .file{margin:0 0 3px}.card>.item .txt{font-size:14px;line-height:1.5}.card>.item.new::before{content:"";position:absolute;left:2px;top:18px;width:7px;height:7px;border-radius:50%;background:var(--signal)}
.card>h3+.item.new::before{top:6px}.card>.item .out{margin-top:8px}
p.note,.card .note{margin:0;padding:10px 12px;border-radius:var(--r);background:color-mix(in srgb,var(--signal) 7%,var(--sheet));border:1px solid color-mix(in srgb,var(--signal) 22%,var(--line));color:var(--ink);font-size:13px;line-height:1.5}
.grid>.card>h3{flex-wrap:nowrap}.grid>.card>h3>span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
aside button.proj{display:flex;align-items:center;gap:10px;width:100%;min-height:44px;cursor:pointer;color:var(--ink);font:600 14px var(--text);text-align:left;margin:0 0 14px;padding:10px 12px;border:1px solid var(--line);border-radius:var(--r-lg);font-weight:600;background:var(--sheet)}aside button.proj .lb{flex:1}aside button.proj .ic:last-child{color:var(--mute)}
dialog .acts{justify-content:flex-end}
/* in a card's action row, Edit and Delete are icons (their word is the name and the tooltip) */
.card>.acts .btn.quiet{width:34px;padding:0;font-size:0;gap:0}.card>.acts .btn.quiet .ic{margin:0;width:15px;height:15px}.mobile .card>.acts .btn.quiet{width:44px}
.card>.acts{gap:6px}
.rowcard textarea.f{field-sizing:content;min-height:38px;max-height:132px;resize:none}
.mobile .grid{grid-template-columns:repeat(2,minmax(0,1fr))}.mobile .grid>.card[data-w="3"],.mobile .grid>.card[data-w="2"]{grid-column:auto!important}
.mobile .grid>.card[data-w="3"]{padding:14px}
/* aligned rows (Measures, Glossary...): read as a table, edit in place: a field shows its box on hover and focus */
.rowcard .f{border-color:transparent;background:transparent;box-shadow:none}.rowcard:hover .f{border-color:var(--line);background:var(--sheet)}
.rowcard .f:focus{border-color:var(--ink);background:var(--sheet)}.rowcard textarea.f.code{font:12.5px/1.6 var(--mono)}
.rowcard .fld .file{font:11.5px var(--mono);color:var(--mute);padding-left:12px}.rows>.rowcard{padding-block:10px}
.rowcard>.acts .btn:not(.pri){opacity:1}.rowcard:hover>.acts .btn,.rowcard:focus-within>.acts .btn,.mobile .rowcard>.acts .btn{opacity:1}
.home>.card>p.bad,.home>.card>p.ok{margin:0;font-weight:500}.home>.card>.flink{margin-top:6px}.home>.card>.acts{padding-top:6px}
/* the Chat page fits the screen: its title, then the conversation (no page scroll behind a sticky header) */
main:has(#chat.full) .pghead .ttl p{display:none}main:has(#chat.full) .pg{gap:16px}#chat.full{height:calc(100dvh - 180px);min-height:380px;border-radius:var(--r-lg);box-shadow:var(--shadow)}
#chat.full header{padding:10px 12px 10px 16px}#chat.full .compose{padding:12px 16px 16px}#chat .hello .sg{border-radius:10px;padding:8px 14px}
/* lists of things (reports, models): one primary on the page, so a row's Open is secondary; the row lifts on hover */
.stack>.card .acts .btn.pri{background:var(--sheet);color:var(--ink);border-color:var(--line);box-shadow:0 1px 1px rgb(11 11 11/.03)}
.stack>.card .acts .btn.pri:hover{background:var(--hover);border-color:var(--line-strong)}.stack>.card{transition:border-color .12s,box-shadow .12s}.stack>.card:hover{border-color:var(--line-strong);box-shadow:0 2px 8px -2px rgb(11 11 11/.08)}
.stack>.card>h3 .file{font:11.5px var(--mono);color:var(--mute)}.stack>.card>p{font-size:13.5px}
#switcher{position:fixed;top:60px;left:72px;width:min(380px,calc(100vw - 32px));z-index:30;box-shadow:var(--shadow-pop);border-radius:12px;animation:pop .16s cubic-bezier(.2,.8,.2,1)}
#switcher .areas{grid-template-columns:1fr;gap:6px}#switcher .lk{min-height:0;padding:10px 12px}#switcher .lk.on{border-color:var(--ink);box-shadow:inset 3px 0 0 var(--signal)}
.mobile #switcher{left:16px;top:64px}
.card>.grp>label.l:first-child{margin-top:4px}.card>.grp>.f,.card>.grp>input{display:block;width:100%;margin-top:6px}.card>.row.seg{margin-top:16px}
input[type=file].f{padding:6px 8px}input[type=file].f::file-selector-button{font:500 13px var(--text);border:1px solid var(--line);border-radius:var(--r);background:var(--hover);color:var(--ink);padding:5px 10px;margin-right:10px;cursor:pointer}
.card>.btn,.card>input.f+.btn,.card>.f+.btn{margin-top:18px!important}.card>.checks{margin-top:8px}
.person:not(.dirty) .ptop .btn.pri{background:var(--sheet);color:var(--ink);border-color:var(--line);box-shadow:0 1px 1px rgb(11 11 11/.03)}
.card>.srow{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 0;margin:0;border-top:1px solid var(--line)}.card>h3+.srow{border-top:0;padding-top:0}
.srow .sname{min-width:0}.srow .sname b{display:block;font-weight:500;font-size:13.5px}.srow .sname .file{margin:2px 0 0}
p.state{height:auto;padding:5px 10px;display:inline-flex;align-self:flex-start;line-height:1.3}.card>h4{font-size:13.5px;margin:4px 0 0}.card>h4+p{margin-top:6px}
body[data-mode=viewer] .card>.acts:not(:has(>:not(.w))){display:none}body[data-mode=viewer] .ed{padding-left:0}
dialog .pwho{margin-bottom:4px}dialog h4{font:600 12px var(--display);letter-spacing:.06em;text-transform:uppercase;color:var(--mute);margin:22px 0 0!important}
dialog .srow{border-top:1px solid var(--line)}dialog h4+.srow{border-top:0}dialog .foot{justify-content:space-between;margin-top:22px;padding-top:16px;border-top:1px solid var(--line)}
dialog .foot form.so{display:block}
.areas .lk b{display:flex;align-items:center;gap:8px}.areas .lk b .ic{color:var(--mute)}.areas .lk:hover b .ic{color:var(--signal)}.areas .lk:hover b::after{content:none}
.areas .lk::after{content:"";position:absolute}.areas .lk{position:relative}
/* a visual's Table / CSV / How take no room until the card is hovered or focused: the title keeps its width */
.grid>.card>h3 .tools{max-width:0;overflow:hidden;opacity:0;transition:max-width .18s,opacity .12s}
.grid>.card:hover>h3 .tools,.grid>.card:focus-within>h3 .tools{max-width:220px;opacity:1}.mobile .grid>.card>h3 .tools{display:none}
.grid>.card.sized:has(.kpi)>.body{display:flex;flex-direction:column;justify-content:center;padding-bottom:12px}
.mobile .grid .kpi .v{font-size:clamp(20px,13cqi,32px)}
.stack>.card.editor{flex-direction:column;align-items:stretch;padding:20px 22px}.stack>.card.editor>*+*{margin-top:10px!important}.stack>.card.editor>h3{flex:none}
.stack>.card.editor>.row{justify-content:flex-start;margin-left:0!important}
/* Go to: a sidebar entry and a palette */
aside button.goto{display:flex;align-items:center;gap:10px;width:100%;height:34px;margin:0 0 14px;padding:0 10px;border:1px solid var(--line);border-radius:var(--r);background:var(--paper);color:var(--mute);font:13px var(--text);cursor:pointer;transition:border-color .12s,color .12s}
aside button.goto:hover{border-color:var(--line-strong);color:var(--ink)}aside button.goto .lb{flex:1;text-align:left}aside kbd,#goto kbd{font:500 11px var(--text);padding:2px 6px;border:1px solid var(--line);border-radius:4px;background:var(--sheet);color:var(--mute)}
.mobile aside button.goto{height:44px}
#goto{padding:0;width:min(600px,calc(100vw - 32px));margin-top:14vh;overflow:hidden}#goto>input.f{margin:0;border:0;border-bottom:1px solid var(--line);border-radius:0;padding:16px 18px;font-size:15px;box-shadow:none;min-height:54px}
.golist{max-height:min(420px,60vh);overflow:auto;padding:6px}.goit{display:flex;align-items:center;gap:12px;width:100%;padding:10px 12px;border:0;border-radius:var(--r);background:none;color:var(--ink);font:14px var(--text);cursor:pointer;text-align:left}
.goit .ic{color:var(--mute)}.goit .lb{flex:1}.goit .file{margin:0}.goit.on,.goit:hover{background:var(--hover)}.goit.on .ic{color:var(--signal)}
@media (hover:none){.btn:hover{background:var(--sheet);border-color:var(--line)}.btn.pri:hover{background:var(--ink);border-color:var(--ink)}.btn:active:not(:disabled){background:var(--hover)}.btn.pri:active:not(:disabled){background:color-mix(in srgb,var(--ink) 84%,var(--sheet))}}
.fpop>.file{margin:0;font-size:11.5px;text-transform:uppercase;letter-spacing:.06em}.fpop>.seg{gap:6px}.fpop>.seg .chip{cursor:pointer;padding:4px 10px;font-size:12.5px;margin:0}.fpop>.seg .chip.on{background:var(--ink);color:var(--sheet);border-color:var(--ink)}
.pghead .bar .btn.quiet{width:34px;padding:0;font-size:0;gap:0}.pghead .bar .btn.quiet .ic{margin:0}.mobile .pghead .bar .btn.quiet{width:44px}
dialog .card>h4+.srow{border-top:0;padding-top:6px}dialog .card>h4+label.l{margin-top:12px!important}.mobile aside kbd{display:none}
dialog .two .f{margin-top:6px}dialog .two label.l{margin:0}
.node span{font:12px var(--text)}.node{border-radius:8px;box-shadow:var(--shadow)}.node b{font-size:13.5px}
.rowcard textarea.desc{max-height:calc(3lh + 16px);overflow:hidden;font:13.5px/1.5 var(--text)}.rowcard textarea.desc:focus{max-height:220px;overflow:auto}.rowcard{align-items:start}
body[data-mode=viewer] .tcard label.l:has(input[type=checkbox]:not(:checked)){display:none}body[data-mode=viewer] .tcard label.l input[type=checkbox]{pointer-events:none}
.mobile dialog{padding:22px 20px;border-radius:16px}.mobile dialog .srow .btn{flex:none}
/* a field the action needs, an undo in a toast, a destructive confirmation */
.f.invalid,input.invalid{border-color:var(--red)!important;box-shadow:0 0 0 3px color-mix(in srgb,var(--red) 18%,transparent)!important}
.toast{display:flex;align-items:center;gap:14px}.toast .tact{border:0;background:none;color:var(--signal);font:600 13px var(--text);cursor:pointer;padding:4px 2px;text-decoration:underline;text-underline-offset:3px}
.toast.bad .tact{color:#fff}.toast .tact:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.btn.pri.danger-solid{background:var(--red);border-color:var(--red);color:#fff}.btn.pri.danger-solid:hover{background:color-mix(in srgb,var(--red) 86%,#000)}
#confirm pre:empty{display:none}#confirm p{margin:0 0 8px;line-height:1.55}
.days{gap:6px}.days label.chk{border:1px solid var(--line);border-radius:999px;padding:5px 12px;cursor:pointer;font-size:13px;background:var(--sheet);transition:background .12s,border-color .12s}
.days label.chk input{position:absolute;opacity:0;pointer-events:none}.days label.chk:has(input:checked){background:var(--ink);border-color:var(--ink);color:var(--sheet)}
.days label.chk:has(input:focus-visible){outline:2px solid var(--focus);outline-offset:2px}.days label.chk.invalid,.days:has(.invalid) label.chk{border-color:var(--red)}
.card>.row.seg+.days{margin-top:8px}.card>.two{max-width:520px}p.sum{margin:16px 0 0;padding:10px 12px;border-radius:var(--r);background:var(--paper);border:1px solid var(--line);font-size:13.5px}
.mobile .days label.chk{min-height:40px;display:inline-flex;align-items:center}
.flowsteps{counter-reset:st;gap:6px!important}.flowsteps>.row{counter-increment:st;padding:8px 10px;border:1px solid var(--line);border-radius:var(--r);background:var(--sheet)}
.flowsteps>.row::before{content:counter(st);display:inline-grid;place-items:center;width:22px;height:22px;border-radius:50%;background:var(--hover);font:600 11px var(--text);color:var(--mute)}
.flowsteps>.row>b{font-weight:500;font-size:13.5px}
.card p.hint{margin:16px 0 0;padding:28px 16px;text-align:center;border:1px dashed var(--line-strong);border-radius:var(--r);font-size:13.5px}
label.chk.hl{background:color-mix(in srgb,var(--signal) 12%,var(--sheet));box-shadow:0 0 0 1px color-mix(in srgb,var(--signal) 45%,transparent);border-radius:6px;padding:2px 8px}
.fopt small{margin-left:auto;color:var(--mute);font-size:11.5px;font-variant-numeric:tabular-nums}.colpick{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}.colpick .chip{cursor:pointer;font:12px var(--mono);margin:0}.colpick .chip:hover{border-color:var(--ink)}
.card>div.file+.colpick{margin-top:6px}
.card .row>input[type=time]{max-width:150px;flex:none}.card .row>input[type=number]{max-width:120px}.card .row>.l{margin:0}
#chat .m.nav{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);border-radius:999px;padding:5px 12px;text-decoration:none;color:var(--ink);background:var(--sheet);font-size:12.5px}
#chat .m.nav:hover{border-color:var(--ink)}#chat .steps{border-left-width:2px;border-radius:0 6px 6px 0;background:var(--paper);padding:6px 12px}
.grid>.card.sized:has(.kpi) .kpi .v{font-size:clamp(28px,14cqi,46px)}
.row>input.f[type=time]{max-width:160px!important;flex:0 0 160px!important}.row>input.f[type=number]{max-width:120px!important;flex:0 0 120px!important}
.card>.f+div.file{margin-top:8px}
#chat .busy>span{display:inline-flex;align-items:center;gap:8px}#chat .busy>span::before{content:"";width:12px;height:12px;border-radius:50%;border:2px solid var(--signal);border-right-color:transparent;animation:spin .8s linear infinite}
.card>details.more{margin:0 0 4px 18px}.card>details.more>summary{cursor:pointer;color:var(--mute);font-size:12.5px;padding:2px 0 8px}.card>details.more>summary:hover{color:var(--ink)}
.card>details.more>.item{border-top:1px solid var(--line);padding:10px 0 10px 18px;position:relative}
.card>details.more>.item.new::before{content:"";position:absolute;left:2px;top:16px;width:7px;height:7px;border-radius:50%;background:var(--signal)}
.rows>.rowcard.derived{padding-left:40px;background:color-mix(in srgb,var(--paper) 55%,var(--sheet))}.rows>.rowcard.derived:hover{background:var(--hover)}
.rows>.rowcard.derived .fld:first-child .f{font-size:13.5px;color:color-mix(in srgb,var(--ink) 85%,var(--mute))}
.rows>.rowcard.derived::before{content:"";position:absolute;left:22px;top:22px;width:10px;height:10px;border-left:1.5px solid var(--line-strong);border-bottom:1.5px solid var(--line-strong);border-bottom-left-radius:3px}.rows>.rowcard{position:relative}
button.pin{display:inline-flex;align-items:center;gap:5px}button.pin .ic{width:12px;height:12px}
a.skip{position:fixed;left:12px;top:-60px;z-index:50;background:var(--ink);color:var(--sheet);padding:10px 14px;border-radius:8px;font:500 13px var(--text);text-decoration:none;transition:top .15s}a.skip:focus{top:10px}
main:focus{outline:none}
.card .f+.btn,.card select.f+.btn,.card .fld+.btn{margin-top:16px}
.card>p.note+label.l{margin-top:14px!important}
/* 1.2.5 final: native controls in the theme, a More menu for what destroys, done states, empty states, the report
   grid in rows, folded docs, masked text in words */
input[type=checkbox],input[type=radio]{appearance:none;-webkit-appearance:none;width:17px;height:17px;margin:0;flex:none;display:inline-grid;place-content:center;vertical-align:-3px;
 border:1.5px solid var(--line-strong);background:var(--sheet);border-radius:4px;cursor:pointer;transition:background .12s,border-color .12s}
input[type=radio]{border-radius:50%}input[type=checkbox]:hover,input[type=radio]:hover{border-color:var(--mute)}
input[type=checkbox]:checked,input[type=radio]:checked{background:var(--ink);border-color:var(--ink)}
input[type=checkbox]:checked::after{content:"";width:8px;height:4.5px;border-left:2px solid var(--sheet);border-bottom:2px solid var(--sheet);transform:translateY(-1px) rotate(-45deg)}
input[type=radio]:checked::after{content:"";width:6px;height:6px;border-radius:50%;background:var(--sheet)}
input[type=checkbox]:focus-visible,input[type=radio]:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
input[type=checkbox]:disabled{opacity:.45;cursor:default}.mobile input[type=checkbox]{width:20px;height:20px}
label.chk{gap:8px;cursor:pointer}.checks{gap:10px 18px}
/* table headers: whole sizes, no tracking (11.5 px broke the words apart) */
th,.card table th{font-size:12px;letter-spacing:0;font-weight:500;font-feature-settings:normal}
/* More (⋯): the menu of a row or a page bar */
.more{position:relative;display:inline-flex}.more>.btn.icon{color:var(--mute)}.more>.btn.icon:hover{color:var(--ink)}
.more>.menu{position:absolute;right:0;top:calc(100% + 6px);z-index:35;min-width:200px;padding:6px;background:var(--sheet);border:1px solid var(--line);border-radius:10px;box-shadow:var(--shadow-pop);display:flex;flex-direction:column;gap:2px;animation:pop .14s cubic-bezier(.2,.8,.2,1)}
.more>.menu .btn{justify-content:flex-start;width:100%;height:34px;border:0;box-shadow:none;background:none;font-size:13px;padding:0 10px}
.more>.menu .btn:hover{background:var(--hover)}.more>.menu .btn.danger,.more>.menu .btn.mi[class*=danger]{color:var(--red)}.more>.menu .btn.danger:hover{background:color-mix(in srgb,var(--red) 8%,var(--sheet))}
.card>.acts .more{margin-left:auto}.rowcard>.acts .more>.btn{opacity:1}.rowcard:hover>.acts .more>.btn,.rowcard:focus-within>.acts .more>.btn,.mobile .rowcard>.acts .more>.btn{opacity:1}
.card>.acts .more>.menu{top:auto;bottom:calc(100% + 6px)}body[data-mode=viewer] .more:not(:has(.menu>:not(.w))){display:none}
.mobile .more>.menu .btn{height:44px}
/* a done state */
dialog.done{width:min(480px,calc(100vw - 32px))}.done .dhead{display:flex;align-items:center;gap:12px;margin-bottom:6px}.done .dhead h3{margin:0}
.done .dmark{display:inline-grid;place-items:center;width:32px;height:32px;border-radius:50%;background:color-mix(in srgb,var(--ok) 12%,var(--sheet));color:var(--ok);flex:none}.done .dmark .ic{width:18px;height:18px;stroke-width:2.25}
.done p{margin:8px 0 0;line-height:1.55;color:color-mix(in srgb,var(--ink) 82%,var(--mute))}.done p+p{color:var(--mute);font-size:13.5px}
#publish.live{color:var(--ok);border-color:color-mix(in srgb,var(--ok) 35%,var(--line));opacity:1}#publish.live .ic{color:var(--ink)}#publish.live:disabled{opacity:1}
/* masked by row-level security, said in words */
.masked{display:inline-flex;align-items:center;gap:5px;padding:1px 8px;border-radius:999px;background:var(--hover);color:var(--mute);font-size:12.5px;white-space:nowrap;vertical-align:1px}.masked .ic{width:12px;height:12px}
.masked-row textarea::placeholder{font-style:italic}
/* an empty state: an icon, a title, one line and one action */
.emptyx{display:flex;flex-direction:column;align-items:center;text-align:center;gap:8px;padding:48px 24px 44px;border:1px dashed var(--line-strong);border-radius:var(--r-lg);background:var(--sheet)}
.emptyx .ei{display:inline-grid;place-items:center;width:44px;height:44px;border-radius:12px;background:var(--hover);color:var(--mute);margin-bottom:4px}.emptyx .ei .ic{width:20px;height:20px}
.emptyx h3{margin:0;font:600 15px/1.3 var(--display)}.emptyx p{margin:0;max-width:56ch;color:var(--mute);font-size:14px;line-height:1.55;text-wrap:balance}.emptyx .btn{margin-top:10px}
.pg>p.empty,.cards>p.empty,p.empty{text-wrap:balance}
/* reports: the grid in rows of 54 px, a card spans its layout's rows; a text visual is the lead line */
.pg .grid[data-page]{grid-auto-rows:54px}.grid>.rtext{margin:0;color:var(--mute);font-size:14px;line-height:1.55;align-self:center;max-width:90ch;white-space:pre-wrap}
.grid>.card.kcard{padding:16px 20px}.grid>.card.kcard>h3{margin-bottom:6px}.grid>.card.sized.kcard:has(.kpi)>.body{padding-bottom:0}
.grid>.card.kcard .kpi .v{font-size:clamp(24px,13cqi,40px)!important;margin:0}
.mobile .pg .grid[data-page]{grid-auto-rows:auto}.mobile .grid>.card{grid-row:auto!important}.mobile .grid>.card:not(.kcard){grid-column:1/-1!important}.mobile .grid>.card.pair{grid-column:1/-1!important}.mobile .grid>.rtext{grid-column:1/-1!important}
.mobile .grid>.card.kcard>h3>span:first-child{white-space:normal;overflow:visible;line-height:1.3}.mobile .cards.pins:not(.lay)>.card:not(.wide){flex-basis:140px}.mobile .card h3 button.pin,.mobile .grid>.card>h3 button.pin{width:32px;padding:0;justify-content:center;font-size:0;gap:0}.mobile .card h3 button.pin .ic{width:14px;height:14px}
.mobile .grid>.card.kcard{padding:14px 16px}.mobile .grid .kcard .kpi .v{font-size:clamp(20px,12cqi,28px)!important}
/* Home: the pins share a row (a drawer opening keeps the KPIs side by side) */
.cards.pins:not(.lay){display:flex;flex-wrap:wrap;gap:16px}.cards.pins:not(.lay)>.card{flex:1 1 240px;min-width:0;max-width:none}.cards.pins:not(.lay)>.card.wide{flex-basis:100%}
/* loading: a skeleton */
.skel{display:flex;flex-direction:column;gap:12px;padding:20px 22px;border:1px solid var(--line);border-radius:var(--r-lg);background:var(--sheet)}
.skel .sk{height:14px;border-radius:6px;background:linear-gradient(100deg,var(--hover) 30%,color-mix(in srgb,var(--hover) 35%,var(--sheet)) 50%,var(--hover) 70%) 0 0/300% 100%;animation:shim 1.4s ease-in-out infinite}.skel .w40{width:40%;height:18px}.skel .w70{width:70%}
/* Docs: a contents line, sections that fold */
nav.toc{display:flex;flex-wrap:wrap;gap:6px;align-items:center}nav.toc .chip{cursor:pointer;margin:0;padding:4px 11px;font-size:12.5px;background:var(--sheet)}nav.toc .chip:hover{border-color:var(--ink)}nav.toc .flink{margin-left:6px}
details.dsec,.pg details.dsec.card{padding:0!important}.mobile nav.toc .chip{display:none}details.dsec>summary{display:flex;align-items:center;gap:12px;padding:16px 22px;cursor:pointer;list-style:none;font:600 14.5px var(--display)}details.dsec>summary::-webkit-details-marker{display:none}
details.dsec>summary::after{content:"";width:7px;height:7px;border-right:1.5px solid var(--mute);border-bottom:1.5px solid var(--mute);transform:rotate(45deg);margin-left:8px;transition:transform .15s}details.dsec[open]>summary::after{transform:rotate(225deg)}
details.dsec>summary .dn{display:inline-grid;place-items:center;min-width:24px;height:24px;border-radius:6px;background:var(--hover);font:600 12px var(--text);color:var(--mute)}details.dsec>summary .dt{flex:1}details.dsec>summary .file{margin:0}
details.dsec[open]>summary{border-bottom:1px solid var(--line)}details.dsec>:not(summary){margin:0 22px}details.dsec>h4{margin-top:18px!important}details.dsec>table:last-child{margin-bottom:18px}
details.dsec{scroll-margin-top:72px}table.facts td:first-child{width:32%;color:var(--mute)}table.facts td{overflow-wrap:anywhere}.mobile details.dsec>summary{padding:14px 16px;min-height:52px}.mobile details.dsec>:not(summary){margin:0 16px}
/* History: what, where, who, when */
table.hist .cnt:not(:empty){margin-left:8px;padding:1px 8px;border-radius:999px;background:var(--hover);color:var(--mute);font:500 11.5px var(--text)}table.hist td.code{font:12px var(--mono);color:var(--mute)}table.hist td:nth-child(2){color:var(--mute)}table.hist td:first-child{font-weight:500}
/* Changes: a file in words, its diff one click away */
#commit .files details.chg>summary{display:flex;align-items:center;gap:10px;padding:8px 10px;border:1px solid var(--line);border-radius:var(--r);font-weight:500;list-style:none}#commit details.chg>summary::-webkit-details-marker{display:none}
#commit details.chg>summary .what{flex:1}#commit details.chg[open]>summary{border-radius:var(--r) var(--r) 0 0}#commit details.chg>pre{margin:0;border-top:0;border-radius:0 0 var(--r) var(--r)}
dialog .row.foot{justify-content:flex-end;gap:8px}dialog .row.foot .sp{flex:1}
#confirm .abi{margin:4px 0 0;font-size:12.5px;color:var(--mute);line-height:1.5}
p.note.warn{display:flex;gap:8px;align-items:flex-start;background:color-mix(in srgb,var(--signal) 8%,var(--sheet))}p.note.warn .ic{color:var(--signal-ink);margin-top:1px}
/* configuration notes are notes, not errors: neutral, with an info line */
p.note,.card .note{background:var(--paper);border-color:var(--line)}
.card .scroll.all{max-height:none}#changes.pulse{animation:cpulse 1.4s ease-out}@keyframes cpulse{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--ink) 35%,transparent)}100%{box-shadow:0 0 0 8px transparent}}dialog .srow{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:8px 0}dialog .srow .pwho{flex:1;min-width:0}.cards.vault>.card{max-width:none!important}.card label.l+.checks{margin-top:8px}.card label.l+.checks+label.l{margin-top:18px}
body:not(.mobile) .pg:has(>.cards.pins) .pghead{flex-wrap:nowrap}
.slicer.list{height:100%}.slicer.list .opts{flex:1;justify-content:flex-start;gap:6px}.slicer.list .sl{min-height:42px;flex:1 1 0;max-height:76px;align-items:center;font-size:14.5px}.slicer.list .sl small{font-size:13px;font-weight:500;color:var(--ink)}.grid>.card.sized:has(.slicer.list)>.body{display:flex;flex-direction:column}.more>.btn.mbtn{color:color-mix(in srgb,var(--ink) 80%,var(--mute))}.acts button.pin,.srow .acts button.pin{height:34px;padding:0 12px;opacity:1!important;color:var(--ink)}
.mobile .card h3 button.pin,.mobile .grid>.card>h3 button.pin{width:auto!important;padding:0 10px!important;font-size:12px!important;gap:4px!important;min-height:32px}
.mobile .pinned .kpi .v,.mobile .cards.pins .kpi .v{font-size:clamp(22px,8.5vw,30px);letter-spacing:-.03em}
.hist .srow{display:flex;justify-content:space-between;gap:10px;padding:10px 0;border-top:1px solid var(--line)}.hist .srow:first-child{border-top:0}.hist .cnt:not(:empty){padding:1px 8px;border-radius:999px;background:var(--hover);color:var(--mute);font-size:11.5px;align-self:center}
.rowcard .fld .file{font:12px var(--text)!important}.tcard .chip,.tcard .chip *{font-family:var(--text)!important}.card table td{vertical-align:middle}
.rowhead>span{padding-left:12px}.rows>.rowcard{align-items:center}.rows>.rowcard>.acts{align-self:center}.card h3>svg.ic{width:15px;height:15px;color:var(--mute);flex:none;margin-right:-2px}.card h3>svg.ic+span{flex:1 1 auto;min-width:0}.card>.item>.file{display:flex;align-items:center;gap:6px}.card>.item>.file .ic{width:13px;height:13px}
.card>.item.new::before{background:var(--ink)}.card>.item.sev-info.new::before{background:var(--signal)}dialog h3{justify-content:flex-start!important}.node>span{white-space:normal!important;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;line-height:1.4}.pg>.canvas,.canvas{background:radial-gradient(circle,color-mix(in srgb,var(--faint) 70%,transparent) 1px,transparent 1.2px) 0 0/20px 20px,var(--paper)}.mobile body[data-mode=viewer] .rows>.rowcard .fld:first-child .f{font-weight:600}.mobile body[data-mode=viewer] .rows>.rowcard{padding:12px 16px}.mobile body[data-mode=viewer] .rowcard textarea.f.code{font-size:13px}dialog .card>.row.acts{margin-top:14px}.card>.formrow+label.l,.card>.formrow+.mpick{margin-top:18px!important}.mlchart{margin-top:4px}.slicer.list .opts{flex-direction:column;flex-wrap:nowrap;gap:4px;mask-image:none}.slicer.list .sl{display:flex;justify-content:space-between;width:100%;border-radius:8px;padding:8px 12px;font-size:13.5px;
 background:linear-gradient(90deg,color-mix(in srgb,var(--signal) 13%,var(--sheet)) var(--p),var(--sheet) var(--p))}.slicer.list .sl small{font-size:12px}.slicer.list .sl.on{background:var(--ink)}
.kv .rbar{display:block;height:3px;margin-top:4px;border-radius:2px;background:linear-gradient(90deg,var(--c3) var(--p),var(--hover) var(--p));max-width:180px}
/* one accent: green only as a status dot; the words stay ink */
.state.on{color:var(--ink);background:var(--hover)}.state.on::before{background:var(--ok);opacity:1}#publish.live{color:var(--ink);border-color:var(--line)}#publish.live .ic{color:var(--ink)}
.msg.ok,p.ok,div.ok,li.ok{color:var(--ink)}.msg.ok::before{background:var(--ok)}p.ok::before{content:"";display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--ok);margin:0 8px 1px 0}
.kv span.ok{color:var(--ink)}.kv span.ok::before{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--ok);margin:0 6px 1px 0}
p.note.warn{background:var(--paper);border-color:var(--line-strong)}
/* spacing: an action row under the fields it acts on */
.card div:not(.card)>.acts:not(:first-child){padding-top:16px}.card>.acts{padding-top:18px}.home>.card>.acts{padding-top:6px}
.cards:has(>.card.editor),.pg:has(>.pghead) .cards.top{align-items:start}
table.hist td:nth-child(4){white-space:nowrap}table.hist td:nth-child(2){max-width:360px}
.fig span{font-variant-numeric:normal;letter-spacing:-.01em}.stepchips .chip i,.tcard .chip i{font:normal 11px var(--text)}
.mpick{max-height:none;mask-image:none;padding-bottom:2px}
.mobile .two{grid-template-columns:1fr}.mobile .areas{grid-template-columns:1fr}.mobile #chat .wide-t{display:none}
/* a clipped table says it scrolls: a shadow on the side with more */
.scroll,.mobile .card table{background:linear-gradient(to right,var(--sheet) 30%,transparent) 0 0/40px 100% no-repeat local,linear-gradient(to left,var(--sheet) 30%,transparent) 100% 0/40px 100% no-repeat local,
 radial-gradient(farthest-side at 0 50%,rgb(11 11 11/.14),transparent) 0 0/12px 100% no-repeat scroll,radial-gradient(farthest-side at 100% 50%,rgb(11 11 11/.14),transparent) 100% 0/12px 100% no-repeat scroll}
.scroll td,.scroll th{background-color:transparent}.scroll th{background:var(--sheet)}.qrow{flex-wrap:nowrap}.qrow>.f{flex:1;margin:0!important}.card .card.flat{border:0;padding:0;margin-top:8px}.card .card.flat>h3{display:none}.state.warn{color:var(--signal-ink);background:color-mix(in srgb,var(--signal) 10%,var(--sheet))}.card>.item.sev-okay.new::before,.card>details.more>.item.sev-okay.new::before{background:var(--mute)}.card>.item.sev-bad.new::before,.card>details.more>.item.sev-bad.new::before{background:var(--red)}.card>.item.sev-bad .txt{color:color-mix(in srgb,var(--red) 70%,var(--ink))}dialog h3{display:flex;align-items:center;gap:10px}dialog h3>.ic{width:18px;height:18px;color:var(--mute)}.more>.btn.mbtn{padding:0 10px;gap:6px;color:var(--mute)}.more>.btn.mbtn:hover{color:var(--ink)}.rowcard .more>.btn.mbtn .ml,.mobile .more>.btn.mbtn .ml{display:none}.rowcard .more>.btn.mbtn,.mobile .more>.btn.mbtn{width:34px;padding:0}.filepick:has(.invalid){border-color:var(--red);border-style:solid}.filepick{position:relative;display:flex;align-items:center;gap:12px;padding:10px 12px;border:1px dashed var(--line-strong);border-radius:var(--r);background:var(--paper);cursor:pointer;margin-top:6px}
.filepick:hover{border-color:var(--mute)}.filepick.has{border-style:solid;background:var(--sheet)}.filepick input{position:absolute;inset:0;opacity:0;width:100%;cursor:pointer}.filepick .fname{color:var(--mute);font-size:13.5px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.filepick.has .fname{color:var(--ink);font-weight:500}
.filepick:has(input:focus-visible){outline:2px solid var(--focus);outline-offset:2px}.filepick .fbtn{pointer-events:none;flex:none}
.mobile .fpop{position:fixed;left:0;right:0;bottom:0;top:auto;width:100%;max-height:78vh;border-radius:18px 18px 0 0;padding:20px 16px calc(18px + env(safe-area-inset-bottom,0px));z-index:40;box-shadow:0 -16px 40px rgb(11 11 11/.22);animation:sheet .22s cubic-bezier(.2,.8,.2,1)}
.mobile .fpop::before{content:"";align-self:center;width:40px;height:4px;border-radius:2px;background:var(--line-strong);margin:-8px 0 6px}.mobile .fpop .list{max-height:44vh}.mobile .fpop .fopt{min-height:44px}
@keyframes sheet{from{transform:translateY(40px);opacity:0}}.mobile body:has(.fpop)::after{content:"";position:fixed;inset:0;background:rgb(11 11 11/.36);z-index:39}
.mobile body[data-mode=viewer] .rows>.rowcard .fld>label.l{display:none}.mobile .rowcard textarea.desc{max-height:calc(2lh + 8px)}.mobile .rows>.rowcard{gap:4px}.cards.top{align-items:start}.card>.rule{padding:2px 0 10px}.card>.rule+.rule{border-top:1px solid var(--line);padding-top:12px}.card>.rule>h4{margin:0 0 4px!important}.card>.rule .srow{border-top:0;padding:6px 0}.person:not(.dirty) .ptop .btn.pri{visibility:hidden}.ptop{align-items:center}.ptop .btn{align-self:center}.canvas .sizer{margin:0 auto}.canvas{display:block}.cvtools .pct{font:500 12.5px var(--text)!important;font-variant-numeric:tabular-nums}
.node .nh{display:flex;align-items:center;gap:8px}.node .nh .ic{width:15px;height:15px;color:var(--mute);flex:none}.node.trig .nh .ic{color:var(--sheet);opacity:.8}.node>span{margin-left:23px}
.node .ns{margin:6px 0 0 23px;font-size:12px;color:var(--mute)}.node.ok .ns{color:var(--ok)}.node.failed .ns{color:var(--red)}.bar.top .lastrun{margin:0 6px 0 2px}
button.tlink{border:0;background:none;padding:0;font:inherit;color:inherit;cursor:pointer;text-align:left}button.tlink:hover{text-decoration:underline;text-underline-offset:3px}
.stepchips .chip{display:inline-flex;align-items:center;gap:5px}.stepchips .chip .ic{width:12px;height:12px;color:var(--mute)}.card.fold>:not(h3):not(.opener){display:none!important}.card.fold>.opener{align-self:flex-start}.cards.rows{overflow:visible}.cards.rows>.rowhead{border-radius:var(--r) var(--r) 0 0}.cards.rows>:last-child{border-radius:0 0 var(--r) var(--r)}.rowcard:has(.more>.menu:not([hidden])){z-index:5}details.dsec>table{width:calc(100% - 44px)}.mobile details.dsec>table{width:calc(100% - 32px);display:table}.toast>.ic{flex:none;width:16px;height:16px;opacity:.9}.toast:not(.bad)>.ic{color:color-mix(in srgb,var(--ok) 70%,var(--sheet))}.pg>.fsum,p.fsum{margin:0;display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:13.5px;color:color-mix(in srgb,var(--ink) 80%,var(--mute))}#confirm pre.plain{font:14px/1.6 var(--text);background:var(--paper);white-space:pre-wrap}.fbar .fexp{margin-left:auto}.msg.ok::before{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;background:currentColor;margin:0 8px 1px 0}.srow.saved{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 0;border-top:1px solid var(--line)}.srow.saved:first-child{border-top:0;padding-top:0}.srow.saved .acts{flex-wrap:nowrap;gap:6px}.srow.saved .flink.nm{font-size:14px;color:var(--ink);text-decoration:none;font-weight:500}.srow.saved .flink.nm:hover{text-decoration:underline}.figs{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}.fig{display:inline-flex;align-items:baseline;gap:8px;padding:6px 12px;border:1px solid var(--line);border-radius:8px;background:var(--paper)}.fig b{font-weight:500;font-size:12px;color:var(--mute)}.fig span{font:600 15px var(--display);font-variant-numeric:tabular-nums}.item .mdata{max-width:720px}.msg.prog::before{content:"";display:block;height:3px;border-radius:2px;margin:2px 0 10px;max-width:320px;background:linear-gradient(90deg,transparent 0,var(--signal) 40%,transparent 80%) 0 0/50% 100% no-repeat,var(--hover);animation:prog 1.3s ease-in-out infinite}
@keyframes prog{from{background-position:-60% 0,0 0}to{background-position:160% 0,0 0}}.cards:has(>.card.editor){align-items:start}.card>.grp+label.l,.card>.grp+.row+label.l{margin-top:18px!important}.newkind>.two{max-width:none;grid-template-columns:minmax(0,1fr) minmax(0,1.6fr)}.newkind>.two+label.l,.newkind>.formrow{margin-top:18px!important}.newkind>.formrow+label.chk{margin-top:16px}.card .scroll table input.f{width:100%;min-width:72px;min-height:32px;padding:5px 8px;font-size:13px}.card .scroll td .btn{height:32px}.stepchips .chip i,.tcard .chip i{font:normal 10.5px var(--mono);color:var(--mute)}.tcard label.l:has(input[type=checkbox]){margin-top:12px!important}.tcard .cols{margin-top:16px}.rowcard .vtog{display:block;margin:6px 0 0 12px;font-size:12.5px}.cards>.card>.acts{margin-top:auto}
body[data-mode=viewer] .rowcard .f{min-height:0;padding:2px 0;border-color:transparent!important;background:none!important}body[data-mode=viewer] .mobile .rows>.rowcard,.mobile body[data-mode=viewer] .rows>.rowcard{gap:6px;padding:14px 16px}
.mobile body[data-mode=viewer] .rowcard .fld>label.l{margin:6px 0 0;font-size:12px}.mobile body[data-mode=viewer] .rowcard .f{font-size:15px}
.mpick{mask-image:linear-gradient(to bottom,#000 calc(100% - 14px),transparent);padding-bottom:10px}
.node{width:250px}.node>span{-webkit-line-clamp:3}.node .ns{color:var(--mute)}.node.ok{border-left-color:var(--ink)}.node.ok .ns{color:var(--ink)}.state.hold{color:var(--ink);background:var(--hover)}.state.hold::before{background:var(--signal);opacity:1}.state.on::before,.msg.ok::before,p.ok::before,.kv span.ok::before,.mode.live::before{background:var(--ink)}.card>.item.sev-bad .txt,.card>details.more>.item.sev-bad .txt{color:var(--ink)}p.rdy{margin:8px 0 0;font-size:13.5px;line-height:1.5;color:var(--ink);text-wrap:pretty}table.prices td.mname{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:500}.card>h3+p.note{margin:0 0 6px}.qrow{align-items:center}.qrow>.btn{margin:0!important;flex:none}input.f{text-overflow:ellipsis}.card p.empty{text-wrap:pretty}.tcard .stepchips .chip i{font:11.5px var(--mono);font-style:normal;color:color-mix(in srgb,var(--ink) 62%,var(--mute))}.tcard .stepchips .chip{font-size:13px}.more>.btn.mbtn{color:color-mix(in srgb,var(--ink) 78%,var(--mute))}.tcard input[type=checkbox]{border-color:color-mix(in srgb,var(--ink) 45%,var(--line))}.hist td .ic{width:14px;height:14px;color:var(--mute);margin-right:9px;vertical-align:-2px}#switcher .lk{display:grid;grid-template-columns:30px 1fr;column-gap:12px;align-items:center;text-align:left}#switcher .lk>.pav{grid-row:1/3;width:30px;height:30px;border-radius:8px;background:var(--hover);display:grid;place-items:center;font:600 13px var(--display);color:var(--ink)}#switcher .lk.on>.pav{background:var(--signal);color:#fff}#switcher .lk>span:empty{display:none}.person{padding:10px 0}.pmore{margin-top:0}.pmore summary{padding:2px 0 0 42px}.person>.msg:empty{display:none}.chtitle{display:flex;align-items:baseline;gap:8px;padding:16px 20px 4px;font:600 16px var(--display)}.chtitle .ic{width:17px;height:17px;color:var(--signal);align-self:center}.chtitle .file{margin-left:auto;font:400 12.5px var(--text)}.mobile #chat header{border-top:0}.mobile body[data-mode=viewer] .rowcard .f.code{font-family:var(--text);letter-spacing:0;font-size:14.5px}.rowcard:not(.editing) textarea.f.code{white-space:pre;overflow:hidden;mask-image:linear-gradient(to right,#000 88%,transparent)}.state.hold::before{background:var(--ink)!important}.pubnote{margin:-4px 0 14px}.pinhint{text-align:center;margin:8px 0 0}.mobile .scroll{mask-image:linear-gradient(to right,#000 86%,transparent)}.mobile .scroll>table{min-width:520px}.mobile .scroll th,.mobile .scroll td{white-space:nowrap}.nw{white-space:nowrap}.warnnote{display:flex;flex-wrap:wrap;align-items:center;gap:6px 8px;margin-top:12px;padding:10px 12px;border-radius:var(--r);background:color-mix(in srgb,var(--signal) 8%,var(--sheet));color:var(--ink);font-size:13.5px;line-height:1.45}.warnnote>.ic{width:15px;height:15px;color:var(--signal);flex:none}.warnnote>span{flex:1;min-width:60%}table.edt{width:100%}.shppl{max-height:min(46vh,420px);overflow:auto;margin-right:-6px;padding-right:6px}.cnt{white-space:nowrap}.unv{display:inline-block;padding:0 6px;border-radius:5px;background:var(--hover);color:var(--mute);font-size:.92em}.gnote{display:block;margin-top:10px;color:var(--mute);font-size:12.5px}#c-prev{font:14px/1.55 var(--text);white-space:pre-wrap}.pmodels{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:8px 0}.pmodels>.file{margin-right:4px}html:not(.mobile) .card h3 button.pin:not(.on){opacity:.55}.mobile .pinned .kpi .v,.mobile .cards.pins .kpi .v{font-size:clamp(20px,7.2vw,26px)!important}.mobile .tcard .two{grid-template-columns:1fr 1fr!important;gap:10px}#chat.full .chtitle,main #chat .chtitle,.pg #chat .chtitle{display:none}.pubnote{display:block;width:fit-content;margin:0 0 12px;padding:4px 11px;border-radius:999px;background:var(--hover);font-size:12.5px;line-height:1.4}.card .card.flat{box-shadow:none;background:none;margin-top:0}.card .card.flat>label.l:first-of-type{margin-top:16px}.card .card.flat>label.chk{margin-top:14px}.card .card.flat>.acts{margin-top:20px}
/* 1.2.6 M7 objects: a list's search and filters, its pages, a form's fields, the object picker, a connection's label */
.obar{margin:0 0 12px}.obar>input.f{flex:1 1 220px;min-width:0;margin:0}.obar>select.f{flex:0 1 auto;width:auto;max-width:100%;margin:0}
.olist .scroll{max-height:none}.olist table{display:table;width:100%}.mobile .olist .scroll{mask-image:none}.mobile .olist .scroll>table{min-width:0}.mobile .olist .scroll td{white-space:normal}.fchips .oopen{margin:0 8px 0 -2px}
.pager{justify-content:flex-end;margin-top:12px}.pager:empty{display:none}.pager .file{margin:0}.obar>.pager{margin:0 0 0 auto;flex-wrap:nowrap}.pick tr[data-r]:focus-visible{outline:2px solid var(--focus);outline-offset:-2px}
.aform>.fld{margin-top:14px;max-width:560px}.aform>.fld .f{width:100%}.aform>.fld input[type=checkbox]{display:block;margin-top:8px}.aform>.fld>.file{margin:6px 0 0}.aform>p.file{margin:-8px 0 4px}
.opick{position:relative}.opick .hits{display:flex;flex-flow:row wrap;gap:6px;position:absolute;z-index:5;left:0;right:0;top:calc(100% + 4px);padding:10px;background:var(--sheet);border:1px solid var(--line);border-radius:var(--r);box-shadow:var(--shadow-pop)}.opick .hits:empty{display:none}
.canvas text.lbl{fill:var(--ink);font-size:11.5px}
.kv.okv{grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:14px 28px}.okv b{display:block;font-size:12px;margin-bottom:2px}.okv span{font-size:14px}
.card>.kpis{gap:20px 24px;grid-template-columns:repeat(auto-fill,minmax(170px,1fr))}.card>.kpis .kpi{min-width:0}.card>.kpis .kpi .v{font-size:28px}
.odiag{overflow-x:auto;margin:0 0 12px;border:1px solid var(--line);border-radius:var(--r-lg);background:var(--sheet);padding:8px}.odiag svg{display:block;margin:0 auto}
.mlist2 .stepchips .file{margin:0 4px 0 0}#chat a.olink{color:var(--ink);text-decoration:underline;text-decoration-color:var(--signal);text-underline-offset:3px}
.canvas rect.lblbg{fill:var(--paper)}
"""

JS = r"""
const D=window.APP, KEY='__ABI_APP_KEY__';
// row-level security masks some data-quality figures for a restricted reader ("not shown: …" in place of a list or a
// count): the pages read a masked list as none and a masked count as unknown, instead of throwing
function normQ(q){if(!q?.columns)return q;const i=c=>q.columns.indexOf(c),hid=v=>typeof v==='string'&&v.startsWith('not shown');
 (q.rows||[]).forEach(r=>{['problems','gaps','null_rise'].forEach(c=>{const k=i(c);if(k>=0&&!Array.isArray(r[k])){if(hid(r[k]))q.masked=1;r[k]=[]}});
  ['rows','missing_periods','period','volume_drop'].forEach(c=>{const k=i(c);if(k>=0&&hid(r[k]))r[k]=null})});return q}
normQ(D.quality);
const MOBILE=matchMedia('(max-width: 820px), (pointer: coarse) and (max-width: 1100px)').matches;
let CID='';try{CID=localStorage.getItem('abi-client')||'';if(!CID){CID=Math.random().toString(36).slice(2,12);localStorage.setItem('abi-client',CID)}}catch(e){CID=Math.random().toString(36).slice(2,12)}
{const f0=window.fetch.bind(window);window.fetch=(u,o={})=>{if(typeof u==='string'&&u.startsWith('/')){o.headers={...(o.headers||{}),'X-Abi-Client':CID,'X-Abi-Mobile':MOBILE?'1':'0','X-Abi-Project':D.project}}
 return f0(u,o).then(r=>{if(r.status===403)r.clone().json().then(b=>{if(b.error==='forbidden')stale()}).catch(()=>{});return r})}}
// the app restarted (new page key) or the session ended: say so once, instead of 'forbidden' everywhere
function stale(){if(window.__stale)return;window.__stale=1;const b=document.createElement('div');b.className='toast';b.style.cssText='position:fixed;top:64px;bottom:auto;left:50%;right:auto;height:auto;width:max-content;max-width:calc(100vw - 32px);transform:translateX(-50%);z-index:30;display:flex;gap:12px;align-items:center';b.textContent='The app restarted or your session ended. ';const a=document.createElement('button');a.className='btn pri';a.textContent='Reload';a.onclick=()=>location.reload();b.append(a);document.body.append(b)}
if(MOBILE)document.documentElement.classList.add('mobile');
const $=(s,r=document)=>r.querySelector(s), h=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!==undefined)e.textContent=x;return e};
const wait=ms=>new Promise(z=>setTimeout(z,ms));
// one stroke icon set (24 grid, 1.75 stroke, round joins): beside a word, or alone with an aria-label
const IC={home:'M3 10.5 12 3l9 7.5V21h-6v-6H9v6H3z',chat:'M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z',
 sources:'M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zM4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3',
 staged:'M12 3 3 7.5l9 4.5 9-4.5zM3 12l9 4.5 9-4.5M3 16.5 12 21l9-4.5',tables:'M3 4h18v16H3zM3 9h18M3 14.5h18M9 9v11',edits:'M4 20h4L19 9l-4-4L4 16zM13.5 6.5l4 4',
 model:'M4 4h6v6H4zM14 14h6v6h-6zM14 4h6v6h-6zM10 7h4M17 10v4',objects:'M6 8.5A2.75 2.75 0 1 0 6 3a2.75 2.75 0 0 0 0 5.5zM18 8.5A2.75 2.75 0 1 0 18 3a2.75 2.75 0 0 0 0 5.5zM12 21a2.75 2.75 0 1 0 0-5.5 2.75 2.75 0 0 0 0 5.5zM8.75 5.75h6.5M7.2 8.3l3.5 7.4M16.8 8.3l-3.5 7.4',measures:'M18 4H6l6 8-6 8h12',glossary:'M5 4.5A1.5 1.5 0 0 1 6.5 3H20v15H6.5A1.5 1.5 0 0 0 5 19.5zM5 19.5A1.5 1.5 0 0 0 6.5 21H20v-3',
 datasets:'M3 6h18M6 12h12M10 18h4',reports:'M4 20h16M7 16v-5M12 16V6M17 16v-8',monitor:'M3 12h4l3-8 4 16 3-8h4',inbox:'M3 13h5l1.5 3h5L16 13h5M5.5 5h13L21 13v6H3v-6z',
 explore:'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM15.5 8.5l-2 5-5 2 2-5z',boardpack:'M3 7h18v13H3zM8 7V4h8v3M3 12h18',documents:'M6 3h8l5 5v13H6zM14 3v5h5M9 13h7M9 17h7',
 onepager:'M6 3h12v18H6zM9 7h6M9 11h6M9 15h3',dataexport:'M12 3v12M7 10l5 5 5-5M4 20h16',video:'M3 5h18v14H3zM10 9l5 3-5 3z',
 transfer:'M3 7.5 12 3l9 4.5v9L12 21l-9-4.5zM3 7.5l9 4.5 9-4.5M12 12v9',flows:'M4 4h6v6H4zM14 14h6v6h-6zM7 10v4a3 3 0 0 0 3 3h4',
 ml:'M6 6h12v12H6zM10 10h4v4h-4zM9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4',plans:'M4 20V10M10 20V4M16 20v-7M22 20H2M14 8l2-2 2 2',tools:'M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0M16 4v4M10 10v4M18 16v4',
 agents:'M5 8h14v11H5zM12 4v4M9.5 13h0M14.5 13h0M2 13h3M19 13h3',vault:'M3 4h18v4H3zM5 8v12h14V8M10 12h4',
 docs:'M3 5h6a3 3 0 0 1 3 3v12a2 2 0 0 0-2-2H3zM21 5h-6a3 3 0 0 0-3 3v12a2 2 0 0 1 2-2h7z',security:'M12 3 4 6v6c0 4.5 3.4 8 8 9 4.6-1 8-4.5 8-9V6z',
 environments:'M4 4h16v6H4zM4 14h16v6H4zM8 7h0M8 17h0',history:'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 7v5l3 2',
 people:'M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM2 21v-1a6 6 0 0 1 12 0v1M16 3.5a4 4 0 0 1 0 7M22 21v-1a6 6 0 0 0-4-5.7',
 settings:'M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9 7 7M17 17l2.1 2.1M4.9 19.1 7 17M17 7l2.1-2.1',
 menu:'M3 4h18v16H3zM9 4v16',sun:'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.4 1.4M17.6 17.6 19 19M5 19l1.4-1.4M17.6 6.4 19 5',
 moon:'M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z',out:'M15 4h4v16h-4M10 8l-4 4 4 4M6 12h10',layout:'M3 3h8v8H3zM13 3h8v5h-8zM13 10h8v11h-8zM3 13h8v8H3z',
 commit:'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM2 12h6M16 12h6',publish:'M12 15V3M7 8l5-5 5 5M4 15v5h16v-5',
 spark:'M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8zM19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z',
 open:'M7 17 17 7M8 7h9v9',x:'M6 6l12 12M18 6 6 18',left:'M15 6l-6 6 6 6',right:'M9 6l6 6-6 6',wide:'M4 9V4h5M20 15v5h-5M4 4l6 6M20 20l-6-6',narrow:'M10 4v6H4M14 20v-6h6M4 4l6 6M20 20l-6-6',
 plus:'M12 5v14M5 12h14',search:'M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM20 20l-4-4',pin:'M9 4h6l-1 6 4 4H6l4-4zM12 14v7',filter:'M3 5h18l-7 8v6l-4 2v-8z',
 mail:'M3 5h18v14H3zM3 6l9 7 9-7',trash:'M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13',play:'M7 4l13 8-13 8z',down:'M6 9l6 6 6-6',check:'M4 12l5 5L20 6',
 refresh:'M20 11a8 8 0 0 0-14.6-4.5L4 8M4 4v4h4M4 13a8 8 0 0 0 14.6 4.5L20 16M20 20v-4h-4',upload:'M12 16V4M7 9l5-5 5 5M4 20h16',user:'M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 21a8 8 0 0 1 16 0',
 dots:'M4.5 12a.75.75 0 1 0 1.5 0 .75.75 0 1 0-1.5 0M11.25 12a.75.75 0 1 0 1.5 0 .75.75 0 1 0-1.5 0M18 12a.75.75 0 1 0 1.5 0 .75.75 0 1 0-1.5 0',
 info:'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 11v5M12 8h.01',alert:'M12 4 2.5 20h19zM12 10v4M12 17h.01',lock:'M6 11h12v9H6zM8.5 11V8a3.5 3.5 0 0 1 7 0v3',copy:'M9 9h11v11H9zM5 15V4h11',key:'M8 14a4 4 0 1 1 3.9-5H21v3h-2v2h-3v-2h-4.1A4 4 0 0 1 8 14zM7 10h.01',env:'M4 5h16v5H4zM4 14h16v5H4zM7.5 7.5h.01M7.5 16.5h.01M11 7.5h6M11 16.5h6'};
function ic(n){const s=document.createElementNS('http://www.w3.org/2000/svg','svg');s.setAttribute('viewBox','0 0 24 24');s.setAttribute('class','ic');s.setAttribute('aria-hidden','true');
 const p=document.createElementNS('http://www.w3.org/2000/svg','path');p.setAttribute('d',IC[n]||'');s.append(p);return s}
// an icon button: the word is its accessible name and tooltip
function ib(n,label,fn){const b=h('button','btn icon');b.append(ic(n));b.setAttribute('aria-label',label);b.title=label;if(fn)b.onclick=fn;return b}
// a row's More menu (⋯): Delete and Remove live here, behind one more click and then ABI's confirmation
function more(row){let w=row.querySelector(':scope>.more');if(w)return w.querySelector('.menu');w=h('div','more');const b=h('button','btn mbtn'),m=h('div','menu');b.append(ic('dots'),h('span','ml','More'));b.title='More actions';
 b.setAttribute('aria-haspopup','menu');b.setAttribute('aria-expanded','false');m.setAttribute('role','menu');m.hidden=true;
 b.onclick=e=>{e.stopPropagation();const o=m.hidden;closeMenus();m.hidden=!o;b.setAttribute('aria-expanded',String(o));if(o)m.querySelector('button')?.focus()};
 m.addEventListener('click',()=>setTimeout(closeMenus));w.append(b,m);row.append(w);return m}
function closeMenus(){document.querySelectorAll('.more>.menu:not([hidden])').forEach(m=>{m.hidden=true;m.previousElementSibling?.setAttribute('aria-expanded','false')})}
document.addEventListener('click',e=>{if(!e.target.closest?.('.more'))closeMenus()});document.addEventListener('keydown',e=>{if(e.key==='Escape')closeMenus()});
// a done state: what was made, where it is now, the next step (a measure created, a flow saved, publish, two-step on)
function doneDialog(title,lines,acts){document.querySelectorAll('body>.toast:not([style])').forEach(o=>o.remove());const d=h('dialog','done'),c=h('div','card'),hd=h('div','dhead'),mk=h('span','dmark');mk.append(ic('check'));hd.append(mk,h('h3','',title));c.append(hd);
 lines.filter(Boolean).forEach(t=>c.append(h('p','',t)));const r=h('div','row acts'),x=()=>{d.close();d.remove()};
 (acts||[]).forEach(([l,fn],i)=>{const b=h('button','btn'+(i===(acts.length-1)?' pri':''),l);b.onclick=()=>{x();fn&&fn()};r.append(b)});if(!(acts||[]).length){const b=h('button','btn pri','Done');b.onclick=x;r.append(b)}
 c.append(r);d.append(c);d.onclose=()=>d.remove();document.body.append(d);d.showModal();r.lastChild.focus();return d}
// a restricted reader's copy has ABI's mask in place of data-derived text: say it in words, and once
const MASK='[hidden by row-level security]';
// data freshness counts the tables loaded from sources (not the date table or ML predictions), in the version shown
const freshLine=(b,n)=>(b?`${b} of ${n} loaded table${n>1?'s':''} ${b>1?'have':'has'} problems`:`All ${n} loaded table${n>1?'s':''} without problems`)+(MODE==='viewer'?' in the published version':'');
// ABI's data-quality gap in words: "5 missing days in the date column, latest 2025-12-25 to 2025-12-26"
const plainGap=t=>String(t).replace(/(\d+) missing days in the (\w+) column, latest (\S+) to (\S+)/g,(_,n,c,a,b)=>`${n} days have no rows (${colName(c)}); the latest gap is ${a} to ${b}`);
// ABI's readiness findings in the words of the page that fixes them
const plainReady=m=>String(typeof m==='string'?m:JSON.stringify(m)).replace(/Table '(\w+)' holds ML predictions made from rows that row-level security rules filter;[^.]*\./g,(_,t)=>`“${idsToNames(t)}” holds ML predictions made from rows that row rules filter, and has no row rule of its own, so restricted viewers see none of it. Give it a row rule in Security.`);
function txt(t,cls){const d=h('div',cls||'txt');String(t??'').replace(/\((\w+) \1\)/g,'($1)').split(MASK).forEach((x,i)=>{if(i){const s=h('span','masked');s.append(ic('lock'),'details hidden for your access');d.append(s)}if(x)d.append(x)});return d}
// the icon a button's words mean, so the same action looks the same on every page
const BIC=[[/^(New |\+ |Add )/,'plus'],[/^Render and download|^Download/,'dataexport'],[/^Save rule|^Save schedule|^Save$/,'check'],[/^Test$/,'play'],[/^(Delete|Remove|Revoke)/,'trash'],[/^(Export|Download)/,'dataexport'],[/^(Edit)/,'edits'],[/^Run( now)?$/,'play'],[/^Email/,'mail'],
 [/^Reload|^Check again|^Refresh|^Regenerate/,'refresh'],[/^Read$/,'documents'],[/^Open/,'open'],[/^Upload/,'upload'],[/^Ask the agent/,'spark'],[/^Publish/,'publish']];
async function api(action,args){
 
 const post=(u,b)=>fetch(u,{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify(b||{})}).then(r=>r.json());
 let r=await post('/api/'+action,{project:D.project,...args});
 if(action==='run_object_action')ASK_WHAT='';  // an action on an object is never a project delete, whatever it is called
 while(r.confirm){if(!await ask(action==='run_object_action'?{...actAsk(r.confirm),plain:true}:action==='decide_same'?{...r.confirm,plain:true,preview:''}:r.confirm))throw new Error('You declined. Nothing changed.');r=await post('/confirm/'+r.confirm.id)}
  if(r.error)throw Object.assign(new Error(plainErr(r.error)),{fields:r.fields});if(/^(set_|save_|delete_|add_|remove_|enable_|create_|edit_|approve_|reject_|undo|commit|publish_|train_|score_|decide_)/.test(action))refreshPending();return r.result}
let PEND_T=null;function refreshPending(){clearTimeout(PEND_T);PEND_T=setTimeout(async()=>{try{D.pending=await (await fetch('/api/changes',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify({project:D.project})})).json().then(x=>x.result||D.pending);vc();const cb=$('#changes');cb.classList.remove('pulse');void cb.offsetWidth;if(D.pending?.files?.length)cb.classList.add('pulse')}catch(e){}},400)}
let ASK_WHAT='';  // what the person is deleting, for ABI's confirmation
const SHOWN='Shown once: copy it now';  // the title of a credential's dialog, as the harness sends it (index.ts SHOWN)
function ask(c){return new Promise(res=>{const d=$('#confirm'),bad=!c.plain&&/\b(delet|remov|drop|revok|stop|disabl|overwrit)/i.test((c.title||'')+' '+(c.message||''));$('#c-yes').classList.toggle('danger-solid',bad);$('#c-yes').textContent=c.yes||(bad?(ASK_WHAT?'Delete':'Yes, go ahead'):'Confirm');$('#confirm h3').textContent=bad&&ASK_WHAT?`Delete “${ASK_WHAT}”?`:c.title||(c.id?'Confirm this change':'Confirm');
 $('#c-msg').textContent=bad&&ASK_WHAT?`“${ASK_WHAT}” leaves the project. It stays an uncommitted change, so Undo in the toast or under Changes takes it back.`:c.lead||people(c.message);$('#c-abi').textContent=c.lead?people(c.message||''):'';$('#c-abi').hidden=!$('#c-abi').textContent;ASK_WHAT='';const pv=plainPreview(c.preview||'');$('#c-prev').textContent=pv;$('#c-prev').hidden=!pv;$('#c-prev').classList.toggle('plain',pv!==(c.preview||''));
 // one way out, whatever closes it: the answer is given and the dialog's text leaves the page (a credential never stays in it).
 // Esc is No; a credential's dialog (c.once: the credential) has no No and no Esc, and a Copy button
 let over=false;const cp=$('#c-copy'),done=v=>{if(over)return;over=true;d.onclose=null;d.oncancel=null;if(d.open)d.close();['#c-msg','#c-abi','#c-prev'].forEach(s=>{$(s).textContent=''});cp.hidden=true;cp.onclick=null;$('#c-no').hidden=false;res(v)};
 if(c.once){$('#c-prev').textContent=c.preview;$('#c-prev').hidden=false;$('#c-prev').classList.add('plain')}
 $('#c-no').hidden=!!c.once;cp.hidden=!c.once;cp.textContent='Copy';cp.onclick=async()=>{try{await navigator.clipboard.writeText(c.once)}catch(e){const r=document.createRange();r.selectNodeContents($('#c-prev'));getSelection().removeAllRanges();getSelection().addRange(r)}cp.textContent='Copied'};
 d.oncancel=e=>{e.preventDefault();if(!c.once)done(false)};d.onclose=()=>done(!!c.once);  // closed some other way (a second Esc, the browser): still answered, still emptied
 d.showModal();$('#c-yes').onclick=()=>done(true);$('#c-no').onclick=()=>done(false)})}
// ABI's preview of what it will do, in words where it is a known shape (a role change: "editor1: Viewer → Editor")
function plainPreview(t){t=String(t||'');const who=Object.fromEntries([...(D.rls?.accounts||[]).map(a=>[String(a.id),a.name]),...(window.ME?.id?[[String(ME.id),ME.name]]:[])]),R=x=>x?x[0].toUpperCase()+x.slice(1):'No access',nm=u=>who[String(u)]||'a person';
 const k=t.search(/\{[\s\S]*\}\s*$/),head=k>0?t.slice(0,k).replace(/Preview:\s*$/,'').trim():'';let j;try{j=JSON.parse(k>=0?t.slice(k):t)}catch(e){return people(t)}
 if(j&&j.action&&j.object&&otype(j.object.type))return actWords(j);  // an action on an object: what was entered, what changes
 const rule=x=>`${colName(x.column)} (${x.table}): `+(Array.isArray(x.users)?x.users.map(u=>`${u.name||nm(u.id)} sees ${(u.values||[]).join(', ')||'no rows'}`):Object.entries(x.users||{}).map(([u,v])=>`${nm(u)} sees ${(v||[]).join(', ')||'no rows'}`)).join('; ');
 // roles as before → after; row rules (ABI calls them "rules" or "rls") as before and after, or unchanged
 let out=null;const parts=[],rr=j&&(j.rules||j.rls);if(j&&Array.isArray(j.roles))parts.push(...j.roles.map(r=>`${nm(r.user)}: ${R(r.old)} → ${R(r.new)}`));
 if(rr&&(rr.new||rr.old)){const o=[].concat(rr.old||[]),n=[].concat(rr.new||[]);parts.push(...(JSON.stringify(o)===JSON.stringify(n)?n.map(x=>'Row rule, unchanged: '+rule(x)):[...(o.length?['Before: '+o.map(rule).join('\n')]:[]),'After: '+(n.length?n.map(rule).join('\n'):'no rule')]))}
 if(parts.length)out=parts.join('\n');
 else if(j&&typeof j==='object'&&!Array.isArray(j)&&Object.values(j).every(v=>v===null||typeof v!=='object'))out=Object.entries(j).map(([a,v])=>`${colName(a)}: ${v??'—'}`).join('\n');
 return out==null?people(t):[people(head),out].filter(Boolean).join('\n\n')}
// "as user 1" in ABI's words: the person's name
function people(t){const who=Object.fromEntries([...(D.rls?.accounts||[]).map(a=>[String(a.id),a.name]),...(window.ME?.id?[[String(ME.id),ME.name]]:[])]);return String(t||'').replace(/\s*Confirm to proceed\.?/g,'').replace(/\b(measure|dataset|optimizer|model|flow|report) "([a-z][a-z0-9_]*)"/g,(m,k,id)=>{const x=(({measure:D.measures,dataset:D.datasets,optimizer:D.optimizers,model:D.ml,flow:D.flows,report:D.reports})[k]||[]).find(y=>y.id===id);return x?.name?`${k==='optimizer'?'plan':k} “${x.name}”`:m}).replace(/project "[a-z0-9_-]+_pub"/g,'the published version').replace(new RegExp(`project "${D.project}"`,'g'),'this project').replace(/\b(flow|report|ML model|tool|agent) '([a-z0-9_]+)'/g,(m,k,id)=>`${k} “${idsToNames(id)===id?id.replace(/_/g,' ').replace(/^./,c=>c.toUpperCase()):idsToNames(id)}”`).replace(/\buser (\d+)\b/g,(m,u)=>who[u]?who[u]:'a person').replace(/project '[a-z0-9_-]+_pub'/g,'the published version').replace(new RegExp(`project '${D.project}'`,'g'),'this project').replace(/Enabling flow (“[^”]+”) lets its data condition trigger run it unattended as [^.]+\./,'$1 then checks every minute and posts to the Inbox, using your access.').replace(/Enabling flow (“[^”]+”) lets its (\w[\w ]*?) trigger run it unattended as [^.]+\./,'$1 then runs on its $2 by itself, using your access.')}
function plainErr(m){return objWords(String(m||'').replace(/( for each [^(:]+) \([a-z0-9_., ]+\)/i,'$1')).replace(/^.*?rejected by the MEL compile:\s*\d+ error\(s\):?\s*/i,'The formula has an error: ').replace(/The formula has an error:\s*[;.]\s*/,'The formula has an error, so nothing changed. Check its column names and brackets. ').replace(/(nothing changed\.[^;]*?)[;.]?\s*nothing changed\.?/i,'$1').replace(/\(DECISIONS [A-Z]-\d+\)/g,'').replace(/\buser (\d+)\b/g,'a person').trim()}
// an id in ABI's text (a measure, a flow, a report) as its name
function idsToNames(t){const M=new Map([...(D.measures||[]),...(D.flows||[]),...(D.reports||[]),...(D.ml||[]),...(D.datasets||[]),...(D.optimizers||[])].filter(x=>x.id&&x.name).map(x=>[x.id,x.name]));return String(t??'').replace(/\b[a-z][a-z0-9_]{2,}\b/g,w=>M.get(w)||w)}
// Share a report: its link (for anyone who already has access) and who that is; access is changed in Users & access
async function shareDialog(rep){const d=h('dialog'),c=h('div','card'),base=window.ME?.base||'',path=`/?p=${encodeURIComponent(D.project)}#rep:${encodeURIComponent(rep.id)}`,url=base+path,li=h('input','f'),cp=btn('Copy link',async()=>{try{await navigator.clipboard.writeText(url)}catch(e){li.select()}cp.replaceChildren(ic('check'),'Copied');cp.classList.add('done');setTimeout(()=>{cp.replaceChildren(ic('copy'),'Copy link');cp.classList.remove('done')},2200)},1),ppl=h('div','shppl');
 li.readOnly=true;li.value=url;li.setAttribute('aria-label','Link to the report');const r=h('div','row qrow');r.append(li,cp);cp.prepend(ic('copy'));
 c.append(h3i('people',`Share ${rep.name||rep.id}`),h('p','file','The link opens the report for people who have access to it; Viewers see the published version.'),r,...(base?[]:[(()=>{const n=h('p','note','This app is reached at a local address here, so the link is only its path. The address people open is set once for the install.');if((window.ME||{}).admin&&!MOBILE){const go=h('button','flink','See Settings › Email');go.onclick=()=>{d.close();show('settings')};n.append(' ',go)}return n})()]),h('h4','','Who can open it'),ppl);
 const x=await fetch('/admin/users',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:'{}'}).then(y=>y.json()).catch(()=>({}));const U=x.result;
 if(U)U.users.forEach(u=>{const a=U.access[u.name]||{},role=(u.roles||{})[U.project],can=u.workspace_owner||(role&&role!=='none'&&(!a.reports||a.reports.includes(rep.id))),row=h('div','srow'),n=h('div','pwho');
  n.append(h('span','av',u.name.slice(0,1).toUpperCase()),h('b','',u.name));row.append(n,h('span','state'+(can?' on':''),can?(u.workspace_owner?'Owner':'Can open'):'No access'));ppl.append(row)});
 const f=h('div','row foot'),mg=btn('Manage access',()=>{d.close();PENDING.share=rep.id;show('people')}),cl=btn('Done',()=>d.close());f.append(mg,h('span','sp'),cl);c.append(f);d.append(c);d.onclose=()=>d.remove();document.body.append(d);d.showModal()}
function plur(t){return String(t??'').replace(/(\d[\d,]*) ([a-z]+)\(s\)/gi,(m,n,w)=>n==='1'?`${n} ${w}`:`${n} ${w}s`).replace(/\b([a-z]+)\(s\)/gi,'$1s')}
// a page made a moment ago opens as soon as ABI's data has it (the sidebar learns of it on its next redraw)
async function openWhenThere(k){for(let i=0;i<40&&!PAGES[k];i++){if(i===3)refreshPending();await wait(300)}if(!PAGES[k]){try{D.flows=(await api('list_flows',{})).flows||D.flows;sidebar()}catch(e){}}PAGES[k]?show(k):toast('It is saved; it shows in the list in a moment.')}
function h3i(i,t){const x=h('h3');x.append(ic(i),t);return x}
function say(box,text,bad){box.textContent=text;box.className='msg '+(bad?'bad':'ok')}
const CARDIC={'Rows':'tables','Edit history':'history','Waiting for approval':'check','Agent':'agents','Usage':'reports','Email':'mail','Single sign-on':'lock','Connect a provider':'plus','Messages':'inbox','Alerts':'alert','Data freshness':'refresh','Model readiness':'model','ML models':'ml','Flows':'flows','Row-level rules':'security','Hidden columns':'lock','Roles':'people','Restrict rows for a person':'filter','Two-step sign-in':'lock','What this controls':'info','Export':'dataexport','Import':'upload','Commits':'history','Search the vault':'search','Saved analyses':'explore','New analysis':'explore','Add a person':'user','Invite':'mail','Properties':'tables','History':'history'};
function card(title,sub){const c=h('div','card'),t=h('h3');if(CARDIC[title])t.append(ic(CARDIC[title]));t.append(h('span','',title));if(sub&&(/\s/.test(sub)||/\.json$/.test(sub)))t.title=sub;else if(sub)t.append(h('span','file',sub));c.append(t);return c}
// a list of editable items as aligned rows under one header (rows mode); a field is its label (shown on phones) and input
function fld(label,el,note){const d=h('div','fld');d.append(h('label','l',label),el);if(note)d.append(h('div','file',note));return d}
function rowcard(c,...fields){c.classList.add('rowcard');c.append(...fields);c.oninput=()=>c.classList.add('dirty');return c}
function rowhead(...cols){const r=h('div','rowhead');cols.forEach(t=>r.append(h('span','',t)));return r}
const W=/^(Save|Create|Delete|Edit report|New report|Reload|Run now|Enable|Disable|Train|Score|New |Ask$|Add |Remove|Edit|Publish|Save steps|Upload|Email this|Alert with)/;
function btn(label,fn,pri){const b=h('button','btn'+(pri?' pri':'')+(W.test(label)?' w':'')+(/^(Delete|Remove|Revoke)/.test(label)?' danger':''),label);{const m=BIC.find(([r])=>r.test(label));if(m)b.prepend(ic(m[1]));if(label.startsWith('+ '))b.lastChild.textContent=label.slice(2)}b.onclick=async()=>{b.disabled=true;const t=setTimeout(()=>b.classList.add('busy'),150);try{await fn()}finally{clearTimeout(t);b.disabled=false;b.classList.remove('busy')}};return b}
// a moment: Today, 07:14 · Yesterday, 18:02 · 6 Nov 2026, 07:14 (the person's own time zone)
function when2(v){const d=new Date(v);if(isNaN(d))return v;const t=d.toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'}),n=new Date(),day=x=>new Date(x.getFullYear(),x.getMonth(),x.getDate()).getTime(),k=(day(n)-day(d))/864e5;
 return k===0?`Today, ${t}`:k===1?`Yesterday, ${t}`:`${d.toLocaleDateString(undefined,{day:'numeric',month:'short',year:'numeric'})}, ${t}`}
const cell=v=>v!==null&&typeof v==='object'?JSON.stringify(v):typeof v==='string'&&/^\d{4}-\d\d-\d\dT\d\d:\d\d/.test(v)?when2(v):v;
function rows(box,d){box.innerHTML='';box.classList.add('out');if(!(d&&d.columns&&Array.isArray(d.rows))){box.append(h('pre','',JSON.stringify(d,null,1)));return}
 const rs=d.rows.map(r=>(Array.isArray(r)?r:d.columns.map(c=>r[c])).map(cell));table(box,{measures:d.measures||{},format:d.format||{},columns:d.columns,rows:rs})}
// an admin action ABI wants confirmed: the person's own click in ABI's dialog, then the server runs it (null: they said no)
async function confirmed(x){if(x.confirm){if(!await ask(x.confirm))return null;x=await fetch('/confirm/'+x.confirm.id,{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:'{}'}).then(y=>y.json())}if(x.error)throw new Error(plainErr(x.error));refreshPending();return x}
function act(c,label,fn,pri){const m=c.querySelector(':scope>.msg')||c.appendChild(h('div','msg'));
 if(!c.dataset.clr){c.dataset.clr=1;c.addEventListener('input',()=>{if(m.classList.contains('bad'))m.textContent=''})}  /* an error goes once the person fixes the field */
 let r=c.querySelector(':scope>.acts');if(!r){r=h('div','row acts');c.insertBefore(r,m)}
 const b0=btn(label,async()=>{c.querySelectorAll('.invalid').forEach(x=>x.classList.remove('invalid'));ASK_WHAT=/^Delete/.test(label)?ttl(c):'';try{const t=await fn(m);c.querySelectorAll('[data-u]').forEach(x=>delete x.dataset.u);if(label==='Delete'){undoToast(ttl(c)||'It');return}if(t)c.isConnected?say(m,t):toast(t)}catch(e){c.isConnected?say(m,e.message,1):toast(e.message,1)}finally{ASK_WHAT=''}},pri);
 if(/^(Delete|Remove|Edit as JSON)\b/.test(label)){b0.classList.add('mi');b0.setAttribute('role','menuitem');more(r).append(b0)}else{const mm=r.querySelector(':scope>.more');mm?r.insertBefore(b0,mm):r.append(b0)}return m}
// a field the action needs: marked and focused, and the action stops with what to do
// a builder on a phone reads the published project: say where the building happens
function vEmpty(t){return MOBILE&&['editor','owner'].includes(window.ME?.role)?'This is built on a computer, in Developer: a phone shows the published project.':t}
function emptyBox(icon,title,text,action){const d=h('div','emptyx'),i=h('span','ei');i.append(ic(icon));d.append(i,h('h3','',title),h('p','',text));if(action)d.append(btn(action[0],action[1],1));return d}
// what the person typed, by where it sits (its card's title and its place in the card): ABI's redraw puts it back
document.addEventListener('input',e=>{if(e.target.closest?.('main'))e.target.dataset.u=1},true);
function fkey(x){const c=x.closest('.person,.card')||$('main'),t=(c.querySelector('h3,.pwho b')?.textContent||'').trim();return CUR+'|'+t+'|'+[...c.querySelectorAll('input,textarea,select')].indexOf(x)}
function names(r){r.querySelectorAll('input:not([type=hidden]):not([aria-label]),textarea:not([aria-label]),select:not([aria-label])').forEach(e=>{if(e.labels?.length||e.closest('label'))return;const p=e.previousElementSibling,
  l=e.closest('.fld')?.querySelector(':scope>label')?.textContent||(p?.matches?.('label')&&p.textContent)||e.placeholder||e.closest('.card')?.querySelector('h3 span')?.textContent;if(l)e.setAttribute('aria-label',l.trim())})}
function need(el,ok,msg){if(ok)return;el.classList.add('invalid');el.setAttribute('aria-invalid','true');const off=()=>{el.classList.remove('invalid');el.removeAttribute('aria-invalid')};el.addEventListener('input',off,{once:true});el.addEventListener('change',off,{once:true});el.focus();throw new Error(msg)}

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
function pinBtn(pin){const b=h('button','pin','Pin');b.title='Pin to Home';const set=()=>{const on=(PINS||[]).some(x=>pinKey(x)===pinKey(pin));b.replaceChildren(ic('pin'),on?'Pinned':'Pin');b.classList.toggle('on',on);b.setAttribute('aria-pressed',on)};
 pinsLoad().then(set);b.onclick=async e=>{e.stopPropagation();await pinsLoad();const k=pinKey(pin),on=PINS.some(x=>pinKey(x)===k);PINS=on?PINS.filter(x=>pinKey(x)!==k):[...PINS,pin];set();
  try{await pinPost({pins:PINS});toast(on?'Unpinned from Home.':'Pinned to Home.')}catch(x){toast('Could not save the pin.')}};return b}
async function pinCard(p,c){const body=h('div','body'),go=ib('open','Open its page'),un=ib('x','Unpin from Home');c.append(body);const tools=h('span','tools');c.querySelector(':scope>h3').append(tools);
 // arrange: move left or right, wide or narrow (saved with the pins)
 const mv=async d=>{const i=PINS.findIndex(x=>pinKey(x)===pinKey(p)),j=i+d;if(i<0||j<0||j>=PINS.length)return;[PINS[i],PINS[j]]=[PINS[j],PINS[i]];await pinPost({pins:PINS});show('home')};
 const l=ib('left','Move left'),r2=ib('right','Move right'),wd=ib(p.wide?'narrow':'wide',p.wide?'Make it narrow':'Make it wide');l.onclick=()=>mv(-1);r2.onclick=()=>mv(1);
 wd.onclick=async()=>{const x=PINS.find(x=>pinKey(x)===pinKey(p));x.wide=!x.wide;await pinPost({pins:PINS});show('home')};tools.append(l,r2,wd,go,un);
 un.onclick=async()=>{PINS=PINS.filter(x=>pinKey(x)!==pinKey(p));await pinPost({pins:PINS});c.remove()};
 const r=p.ref,line=(t,cls)=>body.append(h('p',cls||'',t));
 try{if(p.kind==='visual'){go.onclick=()=>show('rep:'+r.report);const d=await api('visual_data',{report:r.report,page:r.page,visual:r.visual});requestAnimationFrame(()=>chart(body,d));objClicks(body,D.reports.find(x=>x.id===r.report)?.pages.find(x=>x.id===r.page)?.visuals.find(x=>x.id===r.visual)?.dim,d)}
  else if(p.kind==='analysis'){go.onclick=()=>{PENDING.explore={...r.q,chart:r.type};show('explore')};const d=await api('semantic_query',{limit:500,...r.q});d.format=d.format||{};d.type=r.type||'bar';d.id='pin';requestAnimationFrame(()=>chart(body,d));objClicks(body,r.q.dimensions?.[0],d)}
  else if(p.kind==='monitor'){go.onclick=()=>show('monitor');const ck=(D.app?.monitor?.checks||[]).find(x=>x.id===r);if(!ck){line('This check was removed.','empty');return}const x=await monEval(ck);line(x.text,x.ok?'ok':'bad')}
  else if(p.kind==='flow'){go.onclick=()=>show('flw:'+r);const f=(D.flows||[]).find(x=>x.id===r);const x=await api('flow_runs',{flow:r}).catch(()=>({}));const one=x.runs?.[0]||x;
   line(`${f?.enabled?'On':'Off'} · last run: ${one.status||'never'}`,one.status==='failed'?'bad':one.status==='ok'?'ok':'')}
  else if(p.kind==='status'){go.onclick=()=>show('monitor');const q=D.quality||{},ix=k=>(q.columns||[]).indexOf(k),rs=q.rows||[];
   if(r==='freshness'){const bad=rs.filter(x=>(x[ix('problems')]||[]).length||x[ix('missing_periods')]);line(freshLine(bad.length,rs.length),bad.length?'bad':'ok')}
   else if(r==='model'){const rd=D.readiness||{};line(rd.ready?'Ready':'Not ready',rd.ready?'ok':'bad');(rd.missing||[]).slice(0,3).forEach(m=>line(plainReady(m),'file'))}
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
async function pinsHome(box){const sec=h('div','cards pins');box.append(sec);if(!MOBILE){const bar=h('div','row bar top');{const b=btn('+ Add widget',()=>addPin(box),1);b.classList.add('w');bar.append(b)};box.prepend(bar)}const ps=await pinsLoad();
 if(!ps.length&&MOBILE){const r0=(D.reports||[])[0],p0=r0?.pages?.[0],ks=(p0?.visuals||[]).filter(v=>(v.data?.type||v.type)==='kpi').slice(0,3);ks.forEach(v=>{const c=card(v.title||v.id);c.classList.add('pinned');sec.append(c);pinCard({kind:'visual',ref:{report:r0.id,page:p0.id,visual:v.id},title:v.title||v.id},c)});box.append(h('p','file pinhint','Pin a report chart on a computer and it shows here.'));return}
 if(!ps.length){const c=card('Your Home','pin what you check often');c.classList.add('wide');c.append(h('p','',"Press + Add widget, or Pin on a report chart, a saved analysis, a monitor, a table, a flow, an agent or a tool: it shows here, live. Open goes to its page."));sec.append(c);return}
 ps.forEach(p=>{const c=card(p.title||p.ref);c.classList.add('pinned');if(p.wide)c.classList.add('wide');sec.append(c);pinCard(p,c)})}
function home(box){pinsHome(box);const rd=D.readiness||{},q=D.quality||{},ix=c=>(q.columns||[]).indexOf(c),rowsQ=q.rows||[];
 const last=rowsQ.map(r=>r[ix('last_load')]).filter(Boolean).sort().pop(),problems=rowsQ.flatMap(r=>(r[ix('problems')]||[]).map(p=>`${r[ix('table')]}: ${p}`));
 const g=h('div','home');box.append(g);
 // what needs attention
 const at=card('Needs attention');const al=h('ul','list');[...(rd.missing||[]).map(plainReady),...problems].forEach(t=>al.append(h('li','bad',t)));
 if(MODE==='developer')team(at,al);
 if(!al.children.length)al.append(h('li','ok','Nothing: the model is ready and the data is fresh.'));at.append(al);
 // one summary line; the details open on demand (the team's steps arrive later, the line follows them)
 const sum=h('p'),tog=h('button','flink','Show details');al.hidden=true;al.before(sum);al.after(tog);
 const upd=()=>{const it=[...al.children],wait=it.filter(x=>/^Waiting for your yes/.test(x.textContent)).length,bad=it.filter(x=>x.classList.contains('bad')).length-wait;
  sum.className=bad||wait?(MOBILE?'ok':'bad'):'ok';sum.textContent=bad||wait?[bad&&`${bad} problem${bad>1?'s':''} in the data or model`,wait&&`${wait} step${wait>1?'s':''} wait for your yes`].filter(Boolean).join(' · '):'Nothing: the model is ready and the data is fresh.';tog.hidden=!(bad||wait)};
 tog.onclick=()=>{al.hidden=!al.hidden;tog.textContent=al.hidden?'Show details':'Hide details'};upd();new MutationObserver(upd).observe(al,{childList:true});
 act(at,'Check again',async()=>{D.quality=normQ(await api('data_quality',{}));D.readiness=await api('readiness',{});show('home')});g.append(at);
 // the map: every sidebar group with its pages and one line of status, in pipeline order
 const mlok=D.ml.filter(m=>m.latest_score?.status==='ok').length,on=D.flows.filter(f=>f.enabled).length,lastC=(D.history||[])[0];
 const pl=(n,w)=>`${n} ${w}${n===1?'':'s'}`;const line={Data:problems.length?pl(problems.length,'data problem'):`fresh · last load ${last?cell(last):'never'}`,'Data Model':rd.ready?'Ready':'Not ready',
  Visualizations:`${pl(D.reports.length,'report')} · ${pl(MINE.length,'saved analysis').replace('analysiss','analyses')}`,Automate:MODE==='viewer'?`${pl(D.ml.length,'ML model')}${D.flows.length?' · '+pl(D.flows.length,'flow'):''}`:`${mlok} of ${D.ml.length} ML models scored · ${on} of ${D.flows.length} flows on`,
  Knowledge:pl((VAULT||[]).length,'vault document'),Govern:MODE==='viewer'?(D.published?.at?'Published '+cell(D.published.at):'Not published yet'):`${D.pending?.files?.length?pl(D.pending.files.length,'uncommitted change'):'All committed'} · ${D.published?.at?'published '+cell(D.published.at):'not published yet'}`};
 const map=card('Where things stand','one line per area; open it to go there'),tb=h('div','areas');
 line['Data Model']=(rd.ready?'Ready':'Not ready')+` · ${D.measures.length} measures`;
nav().filter(([gname])=>gname!=='Start'&&line[gname]).forEach(([gname,items])=>{const b=h('button','lk'),t=h('b');t.append(ic({Data:'sources','Data Model':'model',Visualizations:'reports',Automate:'flows',Knowledge:'vault',Govern:'security'}[gname]||'home'),gname);b.append(t,h('span','',line[gname]||''));b.onclick=()=>show(items[0][0]);tb.append(b)});
 map.append(tb);at.classList.add('full');map.classList.add('full');g.append(map)}

// ---------- Analyze: one report per entry; filters and cross-filter ----------
var RFLT={},v0odd=null;  // each report's filters, kept across ABI's redraws
// Viewer reads the published version: said under the page's head (a report, an object's page)
function pubNote(box){const ro=(window.ME||{}).role,all=ro==='owner'||ro==='editor'||(window.ME||{}).admin,n=h('p','file pubnote',all?`Published version · you see every row: ${ro==='editor'?'editors':'owners'} aren't limited by row rules.`:'Published version, as your access allows.');setTimeout(()=>{const hd=box.querySelector('.pghead');hd?hd.after(n):box.prepend(n)})}
// A report page's grid has fixed rows on a computer: a text, or a card whose content is taller than its rows (a KPI's
// note, a long title), takes as many rows as it needs, so it never runs over the card below it.
function rowsFit(grid){const fit=()=>{if(MOBILE||!grid.isConnected)return;const cs=getComputedStyle(grid),rh=parseFloat(cs.gridAutoRows)||54,gap=parseFloat(cs.rowGap)||16,rows=x=>Math.max(1,Math.ceil((x+gap)/(rh+gap)));
  [...grid.children].forEach(el=>{if(el.classList.contains('rtext')){const n=rows(el.offsetHeight);if(el.style.gridRow!==`span ${n}`)el.style.gridRow=`span ${n}`;return}
   const cur=+(/span (\d+)/.exec(el.style.gridRow)||[])[1]||0;if(cur&&el.scrollHeight>el.clientHeight+2){const n=rows(el.scrollHeight);if(n>cur)el.style.gridRow=`span ${n}`}})};
 let q=0;const soon=()=>{cancelAnimationFrame(q);q=requestAnimationFrame(fit)},ro=new ResizeObserver(soon);
 new MutationObserver(()=>{ro.disconnect();[...grid.children].forEach(c=>{ro.observe(c);[...c.children].forEach(x=>ro.observe(x))});soon()}).observe(grid,{childList:true})}
function report(box,rep){let flt=PENDING.filters||RFLT[rep.id]||[];PENDING.filters=null;
 if(MODE==='viewer')pubNote(box);
 {const tb=h('div','row bar top');let ed=null;tb.append(btn('Edit report',()=>{if(ed){ed.remove();ed=null;return}ed=editor('report',rep.spec||{id:rep.id,name:rep.name},false);box.querySelector('.pghead').after(ed)}));box.append(tb)}
 {const tb=box.querySelector(':scope>.bar.top');let ex=null;const eb=h('button','btn pri','Export');eb.onclick=()=>{if(ex){ex.remove();ex=null;return}ex=exportCard(rep,()=>flt,()=>cur);act(ex,'Cancel',()=>{ex.remove();ex=null});(MOBILE?box.querySelector(':scope>.fbar'):null||box.querySelector('.pghead')).after(ex)};if(MOBILE){eb.classList.remove('pri');eb.prepend(ic('dataexport'));eb.classList.add('fexp');tb.append(eb)}else tb.prepend(eb)
  let mc=null;tb.append(btn('Email this report',()=>{if(mc){mc.remove();mc=null;return}mc=mailCard(rep);act(mc,'Cancel',()=>{mc.remove();mc=null});box.querySelector('.pghead').after(mc)}));
  if((window.ME||{admin:true}).admin&&!MOBILE){const sh=btn('Share',()=>shareDialog(rep));sh.classList.add('w');sh.prepend(ic('people'));tb.append(sh)}
  const dr=btn('Delete report',async()=>{ASK_WHAT=rep.name||rep.id;try{await KIND.report.del({id:rep.id});undoToast(rep.name||rep.id);show('reports')}catch(e){if(!/declined/.test(e.message))toast(e.message,1)}});dr.classList.add('mi');dr.setAttribute('role','menuitem');more(tb).append(dr)}const top=h('div','fbar'),chips=h('span','fchips'),addb=h('button','btn fadd','+ Filter'),clr=h('button','flink','Clear all');clr.hidden=!flt.length;
 top.append(chips,addb,clr);if(MOBILE)top.append(box.querySelector(':scope>.bar.top .fexp')||h('span'));const tabs=h('nav','tabs'),grid=h('div','grid'),msg=h('div','msg');rowsFit(grid);box.append(top,msg,tabs,grid);

 // filters: one per column, a list of members ("in"); a chart click or a slicer sets it, the bar shows and edits it
 const vals=c=>{const f=flt.find(f=>f.column===c);return f?[].concat(f.value):[]};
 const setVals=(c,vs)=>{flt=flt.filter(f=>f.column!==c);if(vs.length)flt.push({column:c,op:'in',value:vs});draw()};
 const cross=(dim,value)=>{const v=vals(dim);setVals(dim,v.length===1&&v[0]===value?[]:[value])};  // a second click on the same member clears it
 clr.onclick=()=>{flt=[];draw()};let pop=null;
 const close=()=>{pop?.remove();pop=null};document.addEventListener('keydown',e=>{if(e.key==='Escape')close()});
 document.addEventListener('mousedown',e=>{if(pop&&!pop.contains(e.target)&&!addb.contains(e.target)&&!e.target.closest?.('.fchip'))close()});
 // the members of a column, counted by a measure of this report (one of a visual by that column first): the model's
 // first measure may be filtered to a few of them
 const qs=(rep.spec?.pages||[]).flatMap(p=>p.visuals||[]).map(v=>v.query).filter(q=>q?.measures?.length);
 const measFor=c=>(qs.find(q=>(q.dimensions||[]).includes(c))||qs[0])?.measures[0]||D.measures[0]?.id;
 function open(c0){close();pop=h('div','fpop');const col=h('select','f'),q=h('input','f'),list=h('div','list'),ft=h('div','row'),ok=h('button','btn pri','Apply'),no=h('button','btn','Cancel');
  col.append(new Option('Choose a column…',''));D.dims.forEach(c=>col.append(new Option(`${colName(c)} · ${c.split('.')[0]}`,c)));col.value=c0||'';q.placeholder='Search';q.hidden=true;
  // the columns this report already slices by, one click each; every column in the list under them
  const used=[...new Set((rep.spec?.pages||[]).flatMap(p=>p.visuals||[]).flatMap(v=>v.query?.dimensions||[]).filter(c=>D.dims.includes(c)&&!c.startsWith('calendar.')))].slice(0,6),quick=h('div','row seg');
  used.forEach(c=>{const b=h('button','chip',colName(c));b.type='button';b.onclick=()=>{col.value=c;quick.querySelectorAll('.chip').forEach(x=>x.classList.toggle('on',x===b));col.onchange()};quick.append(b)});
  ft.append(ok,no);ft.style.justifyContent='flex-end';ok.disabled=!c0;const rl=(D.rls?.restricted?D.rls.rules||[]:[]).filter(x=>(x.values||[]).length);if(rl.length){const n=h('p','note');n.append(ic('lock'),h('span','',`You already see only ${rl.map(x=>`${colName(x.column)} ${x.values.join(', ')}`).join('; ')}.`));pop.append(n)}pop.append(...(used.length?[h('div','file','Filter by'),quick]:[]),col,q,list,ft);top.append(pop);no.onclick=close;
  ok.onclick=()=>{if(col.value)setVals(col.value,[...list.querySelectorAll('input:checked')].map(i=>JSON.parse(i.value)));close()};
  q.oninput=()=>{const t=q.value.toLowerCase();list.querySelectorAll('label').forEach(l=>l.hidden=!l.textContent.toLowerCase().includes(t))};
  col.onchange=async()=>{list.textContent='';q.hidden=true;ok.disabled=!col.value;if(!col.value)return;list.append(h('p','msg','Loading…'));
   try{const r=await api('semantic_query',{measures:[measFor(col.value)],dimensions:[col.value],order_by:[{field:col.value}],limit:500}),on=vals(col.value);list.textContent='';
    r.rows.forEach(x=>{const l=h('label','fopt'),i=h('input');i.type='checkbox';i.value=JSON.stringify(x[0]);i.checked=on.includes(x[0]);l.append(i,h('span','',x[0]===null?'(blank)':String(x[0])));if(typeof x[1]==='number')l.append(h('small','',fmt(x[1],{compact:true,decimals:1})));list.append(l)});
    q.hidden=r.rows.length<8;if(!q.hidden)q.focus()}catch(e){list.textContent='';list.append(h('p','msg',e.message))}};
  if(c0)col.onchange();else col.focus()}
 addb.onclick=()=>pop?close():open();
 let cur=rep.pages[0];
 async function draw(){RFLT[rep.id]=flt;chips.innerHTML='';msg.textContent='';clr.hidden=flt.length<2;addb.textContent=flt.length?'+ Add filter':'+ Filter';
  flt.forEach(f=>{const vs=[].concat(f.value),c=h('span','fchip'),x=h('button','','×');x.title='Remove this filter';x.setAttribute('aria-label','Remove filter on '+colName(f.column));
   const t=f.op==='in'||f.op==='='?vs.slice(0,3).join(', ')+(vs.length>3?` +${vs.length-3}`:''):`${f.op} ${vs.join(', ')}`;
   const T=objCol(f.column);c.append(h('b','',T?T.name:colName(f.column)),h('span','',t),x);c.title=T?`${T.name}: ${vs.join(', ')}`:`${f.column}: ${vs.join(', ')}`;x.onclick=e=>{e.stopPropagation();setVals(f.column,[])};c.onclick=()=>open(f.column);chips.append(c);
   // 1.2.6 M7: the one member selected is an object (the column is a type's title or key): Open shows its page
   if(T&&vs.length===1&&vs[0]!==null&&(f.op==='in'||f.op==='=')){const o=h('button','flink oopen',`Open ${vs[0]}`);o.title=`Open this ${low(T)}'s page`;o.onclick=()=>objOpen(T,f.column,vs[0]);chips.append(o)}});
  grid.classList.add('busy');
  await Promise.all(rep.pages.flatMap(p=>p.visuals.map(async v=>{delete v.live;if(flt.length&&v.text===undefined)
   try{v.live=await api('visual_data',{report:rep.id,page:p.id,visual:v.id,interaction:v.data?.type==='slicer'?flt.filter(f=>f.column!==v.dim):flt});v.live.id=v.id}catch(e){v.live={error:e.message}}})));
  grid.classList.remove('busy');page(cur)}
 function page(p){cur=p;grid.dataset.page=p.id;[...tabs.children].forEach(b=>b.classList.toggle('on',b.dataset.id===p.id));grid.innerHTML='';
  const vs=p.visuals.slice().sort((a,b)=>(a.layout.y-b.layout.y)||(a.layout.x-b.layout.x)),kp=v=>(v.data?.type||v.type)==='kpi';
  // a row of headline numbers that leaves columns empty shares the row out; a KPI is two rows tall at most
  // ... and so does a row that lost a form card this person may not use (1.2.7 M0: its place stayed empty)
  const span={},rowsY={},lost={};vs.forEach(v=>{if(v.form&&!formOk(v))lost[v.layout.y]=1});vs.filter(v=>v.text===undefined&&!v.form).forEach(v=>(rowsY[v.layout.y]??=[]).push(v));Object.values(rowsY).forEach(r=>{const w=r.reduce((a,v)=>a+(v.layout.w||6),0);if((r.every(kp)||lost[r[0].layout.y])&&w<12){let left=12;r.forEach((v,i)=>{const x=i===r.length-1?left:Math.round((v.layout.w||6)*12/w);span[v.id]=x;left-=x})}});
    if(MOBILE){let n=0;for(const v of vs.filter(v=>v.text===undefined)){if(!kp(v))break;n++}v0odd=n%2?vs.filter(v=>v.text===undefined)[n-1]?.id:null}
  vs.forEach(v=>{if(v.form){formCard(v);return}if(v.text!==undefined){const t=h('p','rtext',v.text);t.dataset.lk=v.id;t.dataset.w=12;t.style.gridColumn='span 12';grid.append(t);return}
   const c=h('div','card'),cw=span[v.id]||Math.min(12,v.layout.w||6);let ch=kp(v)?Math.min(2,v.layout.h||2):v.layout.h;
   // a few rows (a filter, a viewer's region) don't keep a card sized for many: alone on its row or on a phone, it shrinks to them
   {const dd=v.live||v.data,n=(dd?.rows||[]).length,ty=dd?.type||v.type,one=Object.keys(dd?.measures||{}).length===1&&(dd?.columns||[]).length===2;if(ch&&(MOBILE||cw===12)&&n&&n<=6&&(one&&['bar','map'].includes(ty)||ty==='slicer'))ch=Math.min(ch,Math.max(3,Math.ceil((60+n*(ty==='slicer'?46:36))/70)))}c.style.gridColumn=`span ${cw}`;c.dataset.lk=v.id;c.dataset.w=cw;if(ch){c.dataset.h=ch*70-16;c.style.gridRow=`span ${ch}`}if(MOBILE&&v.id===v0odd)c.classList.add('pair');if(kp(v))c.classList.add('kcard');
   c.innerHTML=`<h3><span></span><span class="tools"><button data-a="t">Table</button><button data-a="c">CSV</button><button data-a="h">How</button></span></h3><div class="body"></div><pre class="how" hidden></pre>`;
   $('h3 span',c).textContent=v.title||v.id;if(v.text===undefined)$('h3',c).append(pinBtn({kind:'visual',ref:{report:rep.id,page:p.id,visual:v.id},title:v.title||v.id}));grid.append(c);const body=$('.body',c),d=v.live||v.data;
   if(!d||d.error||v.error){body.append(h('div','bad',plainErr(d?.error||v.error||'no data')));return}
   if(flt.length&&(!d.rows.length||d.rows.every(r=>r.every(x=>x===null||typeof x!=='number')))){body.append(h('p','empty','No rows for these filters: they fall outside what this visual covers (the report\'s period or population). Remove a filter to see it again.'));return}
   // no filter and no rows: a row rule leaves this person nothing here; say so, never a bare —
   if(!flt.length&&(window.ME||{}).role==='viewer'&&d.type!=='slicer'&&(!d.rows.length||d.rows.every(r=>r.every(x=>x===null)))){body.append(emptyBox('lock','Your access shows no rows here','A row rule doesn\'t list you for this data. A project owner can add you to it in Security.'));return}
   if(d.type==='slicer'&&v.dim){$('[data-a=t]',c).remove();slicer(body,d,vals(v.dim),vs=>setVals(v.dim,vs));$('[data-a=c]',c).onclick=()=>csv(d);
    $('[data-a=h]',c).onclick=()=>{const x=$('.how',c);x.hidden=!x.hidden;x.textContent=v.how};return}
   let asTable=false;const dr=()=>{body.innerHTML='';asTable?table(body,d):chart(body,d)};
   if(v.dim){body.classList.add('pick');body.onclick=e=>{const r=d.rows[e.target.closest?.('[data-r]')?.dataset.r];if(r)cross(v.dim,r[0])}}  // a mark knows its row (render.mark)
   requestAnimationFrame(dr);$('[data-a=t]',c).onclick=()=>{asTable=!asTable;dr()};$('[data-a=c]',c).onclick=()=>csv(d);
   $('[data-a=h]',c).onclick=()=>{const x=$('.how',c);x.hidden=!x.hidden;x.textContent=v.how+(flt.length?`\nfilters ${JSON.stringify(flt)}`:'')}})}
 // a form card (1.2.6 M7): an action's inputs, run on the object the card names or the person picks in it; after it
 // ran, the page's visuals read their rows again. Developer only, like every write: Viewer's page has no form
 function formOk(v){const a=OA().find(x=>x.id===v.form.action);return !!a&&!!otype(a.object)&&MODE!=='viewer'&&RANK[a.role||'editor']<=myRank()}
 function formCard(v){if(!formOk(v))return;const a=OA().find(x=>x.id===v.form.action),T=otype(a.object);
  // the object: the one picked in the card, or the one whose title is typed in full (a redraw keeps what was typed)
  let key=v.form.key||null,pk=null;const c=actionForm(a,async()=>{if(!key&&pk?.value.trim())try{const x=await api('list_objects',{type:T.id,filters:[{property:T.title,op:'=',value:pk.value.trim()}],limit:2});if(x.rows.length===1)key=x.key.map(k=>x.rows[0][x.columns.indexOf(k)])}catch(e){}
    if(pk)need(pk,key,`Pick the ${low(T)} first.`);return key},async r=>{await reload();return actDone(a,r)}),cw=Math.min(12,v.layout.w||6),ch=v.layout.h||6,at=()=>c.querySelector(':scope>.fld')||c.querySelector(':scope>.acts');
  if(v.title)c.querySelector('h3 span').textContent=v.title;c.dataset.lk=v.id;c.dataset.w=cw;c.style.gridColumn=`span ${cw}`;c.dataset.h=ch*70-16;c.style.gridRow=`span ${ch}`;
  if(!key){const w=objPick(T,k=>key=k);pk=w.querySelector('input');at().before(fld(T.name,w))}
  else api('get_object',{type:T.id,key}).then(o=>at().before(h('p','mtext',`${T.name}: ${o.title??key.join(', ')}`))).catch(e=>say(c.querySelector(':scope>.msg'),e.message,1));
  grid.append(c)}
 async function reload(){await Promise.all(rep.pages.map(async p=>{let got;try{got=Object.fromEntries((await api('visual_data',{report:rep.id,page:p.id})).visuals.map(d=>[d.id,d]))}catch(e){return}
   p.visuals.forEach(v=>{const d=got[v.id];if(v.text!==undefined||!d)return;if(d.error){v.error=d.error;delete v.data}else{v.data=d;delete v.error}})}));flt.length?await draw():page(cur)}
 rep.pages.forEach(p=>{const b=h('button','',p.name||p.id);b.dataset.id=p.id;b.onclick=()=>page(p);tabs.append(b)});flt.length?draw():page(cur)}
const colName=c=>{const k=String(c).split('.').pop().replace(/_/g,' ');return k.charAt(0).toUpperCase()+k.slice(1)};
// a slicer: its members as toggles (with the measure beside each), several at once; a search over a long list
function slicer(box,d,on,set){const ms=d.columns.slice(1),w=h('div','slicer'),opts=h('div','opts');let sel=on.slice();const tall=(+box.closest('.card')?.dataset.h||0)>250&&d.rows.length<=12,mx=Math.max(1e-9,...d.rows.map(r=>Math.abs(+r[1]||0)));if(tall)w.classList.add('list');
 if(d.rows.length>12){const q=h('input','f');q.placeholder=`Search ${d.rows.length}`;q.oninput=()=>{const t=q.value.toLowerCase();opts.querySelectorAll('.sl').forEach(b=>b.hidden=!b.dataset.k.includes(t))};w.append(q)}
 [...d.rows].sort((a,b)=>sel.includes(b[0])-sel.includes(a[0])).forEach(r=>{const k=r[0]===null?'(blank)':String(r[0]),b=h('button','sl'+(sel.includes(r[0])?' on':''));b.dataset.k=k.toLowerCase();b.setAttribute('aria-pressed',sel.includes(r[0]));
  b.append(h('span','',k));if(ms[0])b.append(h('small','',fmt(r[1],{...d.format[ms[0]],compact:true})));if(tall)b.style.setProperty('--p',Math.round(Math.abs(+r[1]||0)/mx*100)+'%');
  b.onclick=()=>{sel=sel.includes(r[0])?sel.filter(x=>x!==r[0]):[...sel,r[0]];set(sel)};opts.append(b)});
 w.append(opts);if(sel.length){const x=h('button','flink',`Clear (${sel.length})`);x.onclick=()=>set([]);w.append(x)}box.append(w)}
// a multiple <select> shown as toggles (no Ctrl-click): the select stays the value, so forms read it as before
function pickList(sel){if(sel.dataset.pk)return sel.parentElement;sel.dataset.pk=1;const w=h('div','mpick');w.setAttribute('role','group');w.hidden=sel.hidden;if(sel.parentElement)sel.replaceWith(w);sel.style.display='none';w.append(sel);
 let ord=null;const draw=()=>{w.querySelectorAll(':scope>.sl').forEach(b=>b.remove());if(!ord||ord.length!==sel.options.length)ord=[...sel.options].sort((a,b)=>b.selected-a.selected);ord.forEach(o=>{const b=h('button','sl'+(o.selected?' on':''));b.type='button';b.setAttribute('aria-pressed',o.selected);b.append(h('span','',o.text));
  b.onclick=()=>{o.selected=!o.selected;sel.dispatchEvent(new Event('change',{bubbles:true}));draw()};w.append(b)})};
 w.sync=draw;new MutationObserver(r=>{if(r.some(x=>x.type==='attributes'))w.hidden=sel.hidden;else draw()}).observe(sel,{childList:true,attributes:true,attributeFilter:['hidden']});draw();return w}
// a file input reads as a button and the file's name, in the theme
function filePick(i){i.dataset.fp=1;const l=h('label','filepick'),b=h('span','btn fbtn'),n=h('span','fname','No file chosen');b.append(ic('upload'),i.multiple?'Choose files':'Choose a file');i.replaceWith(l);l.append(i,b,n);
 i.addEventListener('change',()=>{const fs=[...i.files];n.textContent=fs.length?fs.map(f=>f.name).join(', '):'No file chosen';l.classList.toggle('has',!!fs.length)});return l}
new MutationObserver(()=>document.querySelectorAll('main input[type=file]:not([data-fp]):not([hidden])').forEach(filePick)).observe(document.body,{childList:true,subtree:true});
// every multiple select a page draws becomes toggles
new MutationObserver(()=>document.querySelectorAll('main select.f[multiple]:not([data-pk])').forEach(pickList)).observe(document.body,{childList:true,subtree:true});
function multi(opts,sel){const m=h('select','f ed');m.multiple=true;m.size=Math.min(8,Math.max(3,opts.length));opts.forEach(o=>{const op=new Option(o,o);op.selected=(sel||[]).includes(o);m.append(op)});return m}
const ALLCOLS=()=>Object.entries(D.columns||{}).flatMap(([t,cols])=>Object.keys(cols).map(c=>`${t}.${c}`)).sort();
function dsForm(d,isNew){const c=card(isNew?'New query':(d.name||d.id),isNew?'':`model/datasets.json · ${d.id}`),id=h('input','f ed'),nm=h('input','f ed'),de=h('input','f ed');
 id.value=d.id||'';id.readOnly=!isNew;nm.value=d.name||'';de.value=d.description||'';const q=d.query||{};
 const ms=multi(D.measures.filter(m=>!/_(prev|change|change_pct)$/.test(m.id)||(q.measures||[]).includes(m.id)).map(m=>m.id),q.measures),dm=multi([...new Set([...(q.dimensions||[]),...(D.dims||ALLCOLS())])],q.dimensions);ms.querySelectorAll('option').forEach(o=>o.text=mname(o.value));dm.querySelectorAll('option').forEach(o=>o.text=`${colName(o.value)} · ${o.value.split('.')[0]}`),lim=h('input','f ed');lim.type='number';lim.value=q.limit||'';lim.placeholder='no limit';
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
function editsPage(box){const T=D.model?.tables||{},keyed=Object.keys(T).filter(t=>(T[t].key||[]).length&&!t.startsWith('ml_')&&!(t.startsWith('same_as_')&&OT().some(o=>o.resolve&&o.table===t.slice(8)))&&t!==(D.model?.date_table?.table||'calendar')).sort();  // ABI makes the date table: nothing to edit; decisions about the same objects are made on their list
 const top=h('div','row'),sel=h('select','f');keyed.forEach(t=>sel.append(new Option(`${t} (${T[t].kind||''})`,t)));top.append(h('label','l','Table'),sel);box.append(top);
 const grid=card('Rows','the edited table, as every query sees it'),hist=card('Edit history','who changed what; undo a batch'),prop=card('Waiting for approval','edits the agent proposed'),g=h('div','cards');
 grid.style.gridColumn=hist.style.gridColumn=prop.style.gridColumn='1/-1';g.append(grid,prop,hist);box.append(g);let off=0,staged=[];
 const msg=h('div','msg'),body=h('div','body'),pager=h('div','row'),save=h('div','row');grid.append(pager,body,save,msg);
 async function rowsPage(){body.innerHTML='';pager.innerHTML='';save.innerHTML='';staged=[];const t=sel.value,key=T[t]?.key||[];
  const r=await fetch('/edits/rows',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify({table:t,offset:off,limit:25})}).then(x=>x.json());
  if(r.error){say(msg,r.error,1);return}const d=r.result,cols=d.columns.map(c=>c.name),tb=h('table','edt');tb.insertRow().append(...cols.map(c=>{const th=h('th','',colName(c)+(key.includes(c)?' (key)':''));th.title=c;return th}));
  d.rows.forEach(row=>{const tr=tb.insertRow();row.forEach((v,i)=>{const td=tr.insertCell();td.textContent=v===null?'—':v;
   if(MODE==='developer'&&!key.includes(cols[i])){td.className='ed-cell';td.title='click to edit';td.onclick=()=>{if(td.querySelector('input'))return;const inp=h('input','f');inp.value=v??'';td.textContent='';td.append(inp);inp.focus();
    const done=()=>{const nv=inp.value;td.textContent=nv||'—';if((nv||null)!==(v===null?null:String(v))){staged=staged.filter(x=>!(x.column===cols[i]&&JSON.stringify(x.key)===JSON.stringify(key.map(k=>row[cols.indexOf(k)]))));
     staged.push({key:key.map(k=>row[cols.indexOf(k)]),column:cols[i],value:nv===''?null:nv,old:v});td.classList.add('changed');sv()}};inp.onblur=done;inp.onkeydown=e=>{if(e.key==='Enter')inp.blur()}}}})});
  if(MODE==='developer'){{const lr=tb.insertRow(),lc=lr.insertCell();lc.colSpan=cols.length+1;lc.className='addcap';lc.append(ic('plus'),'Add a row: fill in its values, then Add row')}const tr=tb.insertRow(),ins=cols.map(c=>{const i=h('input','f');i.placeholder=colName(c);i.setAttribute('aria-label','New row: '+colName(c));tr.insertCell().append(i);return i});
   tr.insertCell().append(btn('Add row',async()=>{const row=Object.fromEntries(cols.map((c,i)=>[c,ins[i].value===''?null:ins[i].value]));
    try{await api('edit_cells',{table:sel.value,add:[row],comment:'row added in the app'});say(msg,'Row added through ABI.');await rowsPage();await history()}catch(e){say(msg,e.message,1)}}))}
  const sc=h('div','scroll all');sc.append(tb);body.append(sc);pager.append(h('span','file',`rows ${off+1}–${off+d.rows.length} of ${d.total}`),
   btn('Previous',async()=>{off=Math.max(0,off-25);await rowsPage()}),btn('Next',async()=>{if(off+25<d.total){off+=25;await rowsPage()}}));{const [pv,nx]=pager.querySelectorAll('.btn');pv.disabled=off===0;nx.disabled=off+25>=d.total;pv.prepend(ic('left'));nx.append(ic('right'))}}
 function sv(){save.innerHTML='';if(!staged.length)return;const cm=h('input','f');cm.placeholder='why (kept in the history)';
  save.append(h('span','',`${staged.length} change${staged.length>1?'s':''} not saved`),cm,btn('Save edits',async()=>{try{await api('edit_cells',{table:sel.value,edits:staged,comment:cm.value||null});say(msg,'Saved through ABI. Every query now sees the edit.');await rowsPage();await history()}catch(e){say(msg,e.message,1)}}))}
 async function history(){hist.querySelectorAll(':scope>:not(h3)').forEach(x=>x.remove());let r;try{r=await api('edit_history',{table:sel.value,limit:50})}catch(e){hist.append(h('p','bad',e.message));return}
  if(!r.rows.length){hist.append(h('p','empty','No edits on this table yet.'));return}const ix=c=>r.columns.indexOf(c),tb=h('table');tb.insertRow().append(...['when','who','row','column','old → new','status','flag',''].map(x=>h('th','',x)));
  const seen=new Set();r.rows.forEach(x=>{const tr=tb.insertRow();[cell(x[ix('timestamp')]),x[ix('user')],x[ix('row_key')],x[ix('column')]||'(row)',`${x[ix('old_value')]??'—'} → ${x[ix('new_value')]??'—'}`,x[ix('status')],x[ix('flag')]||''].forEach(v=>tr.insertCell().textContent=v??'');
   const b=x[ix('batch_id')],c=tr.insertCell();if(MODE==='developer'&&x[ix('status')]==='active'&&!seen.has(b)){seen.add(b);c.append(btn('Undo batch',async()=>{try{await api('undo_edits',{table:sel.value,batch:b});await rowsPage();await history()}catch(e){toast(e.message)}}))}});
  const sc=h('div','scroll all');sc.append(tb);hist.append(sc)}
 async function proposals(){prop.querySelectorAll(':scope>:not(h3)').forEach(x=>x.remove());let r;try{r=await api('edit_proposals',{})}catch(e){prop.append(h('p','bad',e.message));return}
  const list=r.proposals||[];if(!list.length){prop.append(h('p','empty','Nothing waiting. Edits the agent suggests wait here for a person.'));return}
  list.forEach(p=>{const c=h('div','hit');c.append(h('div','',`${p.table} · ${p.changes?.length||p.rows?.length||''} change(s) · ${p.user||''} · ${p.comment||''}`));
   if(MODE==='developer')c.append(btn('Approve',async()=>{try{await api('approve_edit',{table:p.table,batch:p.batch});await proposals();await rowsPage()}catch(e){toast(e.message)}},1),btn('Reject',async()=>{try{await api('reject_edit',{table:p.table,batch:p.batch});await proposals()}catch(e){toast(e.message)}}));prop.append(c)})}
 if(MODE==='developer'){const it=card('New input table','rows entered by people only, e.g. targets or a budget'),nm=h('input','f ed'),cs=h('input','f ed'),ky=h('input','f ed');
  nm.placeholder='e.g. targets_2026';it.querySelector('h3').prepend(ic('plus'));const colsL=h('div','stlist'),TY=[['VARCHAR','Text'],['DOUBLE','Number'],['INTEGER','Whole number'],['DATE','Date'],['BOOLEAN','Yes or no']];
  const addCol=(n,t,k)=>{const r=h('div','row strow'),ni=h('input','f'),ti=h('select','f'),kl=h('label','chk'),kc=h('input');kc.type='checkbox';kc.checked=!!k;kl.append(kc,h('span','','Key'));TY.forEach(([v,l])=>ti.append(new Option(l,v)));ti.value=t||'VARCHAR';ni.placeholder='column name';ni.value=n||'';ni.setAttribute('aria-label','Column name');ti.setAttribute('aria-label','Column type');
   const x=ib('x','Remove this column',()=>r.remove());r.append(ni,ti,kl,x);r.get=()=>({name:ni.value.trim(),type:ti.value,key:kc.checked});colsL.append(r)};addCol('region','VARCHAR',1);addCol('month','DATE',1);addCol('target','DOUBLE');
  it.append(fld('Name',nm),lab('Columns'),colsL,btn('+ Add a column',()=>addCol()));it.style.gridColumn='1/-1';
  act(it,'Create',async()=>{need(nm,/^[a-z][a-z0-9_]*$/.test(nm.value.trim()),'A table name is lowercase letters, digits and underscores, starting with a letter.');const cs2=[...colsL.children].map(r=>r.get()).filter(c=>c.name);
   if(!cs2.length)throw new Error('Add at least one column.');await api('create_input_table',{table:nm.value.trim(),columns:cs2.map(({name,type})=>({name,type})),key:cs2.filter(c=>c.key).map(c=>c.name)});toast(`Created ${nm.value.trim()}: pick it under Table and add its rows.`);return 'Created through ABI.'},1);g.append(it)}
 sel.onchange=()=>{off=0;rowsPage();history()};if(keyed.length){rowsPage();history();proposals()}else box.append(h('p','empty','No keyed tables: a table needs a key to be edited.'))}
// ---------- Export ----------
const dl=(blob,name)=>{const a=h('a');a.href=URL.createObjectURL(blob);a.download=name;a.click()};
async function fileOf(url,body){const r=await fetch(url,{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify(body)});if(!r.ok)throw new Error((await r.json()).error);return r.blob()}
function repPicker(box,onPick,filter){const sel=h('select','f'),list=D.reports.filter(filter||(()=>true));list.forEach(r=>sel.append(new Option(r.name||r.id,r.id)));
 const slot=h('div');box.append(h('label','l','Report'),sel,slot);const go=()=>{slot.innerHTML='';const r=D.reports.find(x=>x.id===sel.value);if(r)onPick(slot,r)};sel.onchange=go;go();
 if(!list.length)box.append(h('p','empty','No reports here yet.'))}
function documentsPage(box){const g=h('div','cards'),c=card('Document','rendered by ABI, as you');repPicker(c,(slot,r)=>{const e=exportCard(r,()=>[],()=>r.pages[0]);e.classList.add('flat');slot.append(e)});g.append(c);box.append(g)}
function onePagerPage(box){const g=h('div','cards top'),c=card('Make a one-pager','the board-pack agent builds it as an ABI report');g.append(c);box.append(g);
 if(MODE==='developer'){const ta=h('textarea','f ed');ta.rows=3;ta.value='A one-pager for the board: the headline numbers for the last complete months, the trend by month, the biggest movers, and the caveats.';
  c.append(ta);act(c,'Ask the agent',async()=>{openChat(true);inp.value='Use the board-pack specialist to build a one-pager report in ABI: '+ta.value;sendChat();return 'Asked: follow it in the chat.'},1)}
 else c.append(h('p','empty',MOBILE||!['editor','owner'].includes(window.ME?.role)?'A builder of this project makes one-pagers in Developer, on a computer.':'Switch to Developer to have one made.'));
 const ex=card('Export a one-pager','reports with a single page');repPicker(ex,(slot,r)=>{const e=exportCard(r,()=>[],()=>r.pages[0]);e.classList.add('flat');slot.append(e)},r=>r.pages.length===1);g.append(ex)}
function dataExportPage(box){const g=h('div','cards top'),c=card('A view\'s rows','ABI\'s export_visual: the same rows the view shows');g.append(c);box.append(g);
 repPicker(c,(slot,r)=>{const pg=h('select','f'),vs=h('select','f'),fm=h('select','f');r.pages.forEach(p=>pg.append(new Option(p.name||p.id,p.id)));['csv','xlsx'].forEach(f=>fm.append(new Option(f.toUpperCase(),f)));
  const fill=()=>{vs.innerHTML='';r.pages.find(p=>p.id===pg.value).visuals.filter(v=>v.text===undefined).forEach(v=>vs.append(new Option(v.title||v.id,v.id)))};pg.onchange=fill;fill();
  slot.append(h('label','l','Page'),pg,h('label','l','View'),vs,h('label','l','Format'),fm);
  act(slot,'Download',async()=>{dl(await fileOf('/data/export',{report:r.id,page:pg.value,visual:vs.value,format:fm.value}),`${r.id}-${vs.value}.${fm.value}`);return 'Downloaded.'},1)});
 const q=card('A query\'s rows','every row the query returns, as CSV'),qs=h('select','f');D.datasets.forEach(d=>qs.append(new Option(d.name||d.id,d.id)));q.append(h('label','l','Query'),qs);
 act(q,'Download CSV',async()=>{const d=await api('dataset_data',{dataset:qs.value});d.id=qs.value;csv(d);return 'Downloaded.'},1);g.append(q)}
function videoPage(box){const c=card('Explainer video','silent, one slide per view, captions from the rows; rendered on this machine');
 if(ME.novideo){c.append(h('p','empty',ME.novideo));box.append(c);return}
 repPicker(c,(slot,r)=>{const pg=h('div','checks'),th=h('select','f'),sz=h('select','f'),sec=h('input','f');
  r.pages.forEach((p,i)=>{const l=h('label','chk'),x=h('input');x.type='checkbox';x.value=p.id;x.checked=i===0;l.append(x,h('span','',p.name||p.id));pg.append(l)});
  [['paper','Light'],['ink','Dark']].forEach(([v,t])=>th.append(new Option(t,v)));th.value=document.documentElement.dataset.theme==='dark'?'ink':'paper';
  [['1920x1080','1080p · 16:9'],['1280x720','720p · 16:9'],['1080x1920','phone · 9:16']].forEach(([v,t])=>sz.append(new Option(t,v)));sec.type='number';sec.min=2;sec.max=15;sec.value=6;
  slot.append(h('label','l','Pages'),pg,h('label','l','Theme'),th,h('label','l','Size'),sz,h('label','l','Seconds per slide'),sec);
  act(slot,'Render and download',async m=>{const pages=[...pg.querySelectorAll('input:checked')].map(x=>x.value);if(!pages.length)return 'Pick at least one page.';say(m,'Rendering… about 5 seconds per slide.');
   dl(await fileOf('/video/export',{report:r.id,pages,theme:th.value,size:sz.value,seconds:+sec.value}),`${r.id}.mp4`);return 'Downloaded.'},1)});box.append(c)}
function boardPage(box){const g=h('div','cards');box.append(g);const P=D.app?.packs||[];
 if(MODE==='developer'){const c=card('Prepare a board pack','the board-pack agent picks, checks, writes and renders; you send'),ta=h('textarea','f ed');ta.rows=4;
  ta.value='For the board review: the overview for the last complete months, a one-pager, the main table as Excel, and a short explainer video. Commentary on what moved and what is open.';
  c.append(ta);act(c,'Ask the agent',async()=>{openChat(true);inp.value='Use the board-pack specialist to prepare a board pack: '+ta.value;sendChat();return 'Asked: follow it in the chat; the pack appears here.'},1);g.append(c)}
 if(!P.length)g.append(emptyBox('boardpack','No board packs yet',MODE==='developer'?'Describe the review above and press Ask the agent: the board-pack agent prepares the documents, data and video, checks every number, and the pack shows here.':'A builder of this project prepares board packs in Developer. When one is ready, its documents, data and video show here to download.'));
 P.slice().reverse().forEach(m=>{const c=card(m.title||m.id,`${m.status} · made ${cell(m.made)}${m.audience?' · for '+m.audience:''}`);
  c.append(h('p',m.notes_grounded?'ok':'bad',m.notes_grounded?'Every number in the notes is in the pack\'s report views.':'Notes not checked.'));
  const fl=h('div','areas');[...m.items,{file:'notes.md',kind:'notes',status:'ok'}].forEach(it=>{const b=h('button','lk');b.append(h('b','',it.kind),h('span','',it.status==='ok'?it.file:'not made: '+(it.error||'')));
   if(it.status==='ok')b.onclick=async()=>{try{dl(await fileOf('/packs/file',{pack:m.id,file:it.file}),it.file)}catch(e){toast(e.message)}};fl.append(b)});c.append(fl);g.append(c)})}
// Users & access (workspace owners): ABI's accounts and roles; the app's sections and reports per person
async function usersPage(box){
 const post=(op,b)=>fetch('/admin/'+op,{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify(b||{})}).then(r=>r.json());
 const r=await post('users');if(r.error){box.append(h('p','bad',r.error));return}const U=r.result,g=h('div','cards');box.append(g);
 const inv=card('Invite','ABI creates the account and emails a link to choose a password'),em=h('input','f');em.type='email';em.placeholder='name@company.com';inv.append(fld('Email',em));
 const ad=card('Add a person','no email needed: you choose a starting password and hand it over; they change it by clicking their name'),an=h('input','f'),ap=h('input','f');an.placeholder='e.g. maria';an.autocomplete='off';ap.type='password';ap.placeholder='at least 8 characters';ap.autocomplete='new-password';ad.append(fld('Name',an),fld('Starting password',ap));
 act(ad,'Add',async()=>{need(an,/^[A-Za-z0-9._@+-]+$/.test(an.value),'A name is letters, digits and . _ - @ +, no spaces.');need(ap,ap.value.length>=8,'The starting password needs at least 8 characters.');const x=await post('adduser',{name:an.value,password:ap.value});if(x.error)throw new Error(x.error);ap.value='';const who=an.value;
  await post('access',{user:who,sections:U.sections.filter(s=>s!=='Govern'),reports:null}).catch(()=>{});PENDING.added=who;toast(`Added ${who}. Give them a role: until then they have no access.`);setTimeout(()=>show(CUR),600);return `Added ${who}.`},1);g.append(ad);
 act(inv,'Invite',async()=>{let x=await post('invite',{email:em.value});if(x.confirm){if(!await ask(x.confirm))return 'Not invited.';x=await fetch('/confirm/'+x.confirm.id,{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:'{}'}).then(y=>y.json())}
  if(x.error)throw new Error(x.error);toast(`Invited ${em.value}: ABI emailed them a link to choose a password.`);return 'Invited through ABI. Give them a role below.'},1);g.append(inv);
 {const bar=h('div','row bar top'),open=(c,f)=>{[ad,inv].forEach(x=>x.hidden=x!==c);c.classList.add('hl');setTimeout(()=>c.classList.remove('hl'),1600);f.focus()};ad.hidden=inv.hidden=true;
  const b1=btn('Add a person',()=>open(ad,an),1);bar.append(b1,btn('Invite by email',()=>open(inv,em)));(box.querySelector(':scope>.pghead')||box).append(bar);[ad,inv].forEach(c=>act(c,'Cancel',()=>{c.hidden=true}))}
 const t=card(`People in ${U.project}`,'role: Viewer reads the published copy; Editor and Owner build (Developer)');t.style.gridColumn='1/-1';g.append(t);
 U.users.forEach(u=>{const row=h('div','hit'),role=(u.roles||{})[U.project]||'none',sel=h('select','f');['none','viewer','editor','owner'].forEach(x=>sel.append(new Option(x==='none'?(u.workspace_owner?'Workspace owner':'No access'):x[0].toUpperCase()+x.slice(1),x)));sel.value=role;
  const a=U.access[u.name]||{},sx=h('div','checks'),rx=h('div','checks'),m=h('div','msg');
  U.sections.forEach(x=>{const l=h('label','chk'),c=h('input');c.type='checkbox';c.value=x;c.checked=!a.sections||a.sections.includes(x);l.append(c,h('span','',x));sx.append(l)});
  U.reports.forEach(x=>{const l=h('label','chk'),c=h('input');c.type='checkbox';c.value=x.id;c.checked=!a.reports||a.reports.includes(x.id);l.append(c,h('span','',x.name));rx.append(l)});
  const save=btn('Save',async()=>{try{if(sel.value!==role){let x=await post('role',{user:u.name,role:sel.value});
    if(x.confirm){if(!await ask(x.confirm))throw new Error('Not changed.');x=await fetch('/confirm/'+x.confirm.id,{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:'{}'}).then(y=>y.json())}if(x.error)throw new Error(x.error)}
   const pick=el=>{const v=[...el.querySelectorAll('input:checked')].map(c=>c.value);return v.length===el.querySelectorAll('input').length?null:v};
   const y=await post('access',{user:u.name,sections:pick(sx),reports:pick(rx)});if(y.error)throw new Error(y.error);row.classList.remove('dirty');say(m,`Saved: ${u.name} is ${sel.value==='none'?'without access':'a'+(sel.value==='owner'?'n ':' ')+sel.options[sel.selectedIndex].text}.`);toast(`Saved ${u.name}'s access.`);row.classList.add('hl');setTimeout(()=>row.classList.remove('hl'),1600)}catch(e){say(m,e.message,1)}},1);
  const np=h('input','f');np.type='password';np.autocomplete='new-password';np.placeholder='a new password for them (8+ characters)';
  const setp=btn('Set password',async()=>{try{const x=await confirmed(await post('setpass',{user:u.name,password:np.value}));np.value='';if(x)say(m,'Set: hand it over; they are signed out everywhere.')}catch(e){say(m,e.message,1)}});
  const rmf=btn('Reset two-step',async()=>{try{const x=await confirmed(await post('mfa/reset',{user:u.name}));if(x)say(m,'Reset: their two-step sign-in is off and they are signed out everywhere; where it is required they set it up again at their next sign-in.')}catch(e){say(m,e.message,1)}});
  // one line per person (who, their role, Save); what they see and their sign-in fold out under it
  row.className='person';row.onchange=row.oninput=()=>row.classList.add('dirty');const top=h('div','ptop'),who=h('div','pwho'),det=h('details','pmore'),pw=h('div','row');
  who.append(h('span','av',u.name.slice(0,1).toUpperCase()),h('b','',u.name));if(u.workspace_owner)who.append(h('span','state on','Workspace owner · sees everything'));if(u.tenant)who.append(h('span','state','Tenant '+u.tenant));
  sel.setAttribute('aria-label','Role of '+u.name+' in this project');top.append(who,sel,save);pw.append(np,setp);
  det.append(h('summary','',`What ${u.name} sees, password and two-step`),h('label','l','Sections'),sx,h('label','l','Reports'),rx,h('label','l','Forgot their password?'),pw,h('label','l','Lost their authenticator app and recovery codes?'),rmf);
  row.append(top,det,m);t.append(row);
  // from a report's Share: each person's reports open, that report marked
  if(PENDING.added===u.name){row.classList.add('just');who.append(h('span','state new','Just added'));setTimeout(()=>{row.scrollIntoView({block:'center',behavior:'smooth'});row.classList.add('hl');setTimeout(()=>row.classList.remove('hl'),2500)},300)}
  if(PENDING.share){const l=[...rx.querySelectorAll('label')].find(x=>x.querySelector('input').value===PENDING.share);if(l){det.open=true;l.classList.add('hl')}}});if(PENDING.share)setTimeout(()=>box.querySelector('label.chk.hl')?.scrollIntoView({block:'center',behavior:'smooth'}),300);PENDING.share=null;PENDING.added=null;
 const mf=card('Two-step sign-in','a code from an authenticator app after the password; each person turns it on by clicking their name');g.append(mf);
 mf.append(h('p','',U.tenants.length?'Require it for everyone in a tenant: the password sessions and personal tokens of its people without it end at once, and at their next sign-in they add the key to an authenticator app before they get in. Single sign-on is not affected.'
  :'Each person turns it on for themselves: they click their name, then Two-step sign-in, and scan a code with an authenticator app.'));
 U.tenants.forEach(x=>act(mf,(x.mfa_required?'Stop requiring it in ':'Require it for everyone in ')+x.name,async()=>{const y=await post('mfa/require',{tenant:x.id,on:!x.mfa_required});if(y.error)throw new Error(y.error);
  setTimeout(()=>show(CUR),1500);return x.mfa_required?'No longer required in '+x.name+'.':`Required in ${x.name}: ${y.result.signed_out} password session(s) ended; they set it up at their next sign-in.`}));
 const note=card('What this controls','and what it does not');note.append(h('p','','Role, sign-in and row-level security are ABI\'s: they decide what data a person can read or change, everywhere. Sections and reports decide what this app shows them and the app\'s own routes refuse the rest. Row and column rules are on the Security page.'));g.append(note)}
// Settings (admins): the agent's model and reasoning, and the providers (API keys go in, never come out)
// a provider's model IDs always come from the provider itself (Fetch models): ABI names none of its own
const PRESETS={openai:['OpenAI','openai-responses','https://api.openai.com/v1'],anthropic:['Anthropic','anthropic-messages','https://api.anthropic.com'],
 openrouter:['OpenRouter','openai-completions','https://openrouter.ai/api/v1'],ollama:['Ollama (local)','openai-completions','http://host.docker.internal:11434/v1'],
 gemini:['Google Gemini','google-generative-ai','https://generativelanguage.googleapis.com/v1beta'],custom:['OpenAI-compatible','openai-completions','https://']};
// Pi computes a cost only for a model with prices: the rest is counted in tokens, never shown as $0
const money=r=>r.cost?(r.cost<0.01?'< $0.01':'$'+r.cost.toFixed(2))+(r.unpriced?' (part tokens only)':''):'—';
async function settingsPage(box){
 const post=(op,b)=>fetch('/admin/'+op,{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify(b||{})}).then(r=>r.json());
 let r=await post('settings');if(r.error){box.append(h('p','bad',r.error));return}let S=r.result;const g=h('div','cards top');box.append(g);
 const draw=()=>{g.innerHTML='';
  const def=card('Agent','the model every chat and specialist uses'),ms=h('select','f'),th=h('select','f');ms.append(new Option('the built-in default',''));
  S.providers.forEach(p=>p.models.forEach(m=>ms.append(new Option(`${p.name} · ${m}`,`${p.id}/${m}`))));ms.value=S.model||'';S.levels.forEach(l=>th.append(new Option(l,l)));th.value=S.thinking||'medium';
  const fb=h('div','checks');S.providers.forEach(p=>p.models.forEach(m=>{const l=h('label','chk'),i=h('input');i.type='checkbox';i.value=`${p.id}/${m}`;i.checked=(S.fallbacks||[]).includes(i.value);l.append(i,h('span','',`${p.name} · ${m}`));fb.append(l)}));
  def.append(h('label','l','Model'),ms,h('label','l','Reasoning'),th,h('label','l','If it fails (quota, outage), try in order'),fb);
  const dm=act(def,'Save',async()=>{const x=await post('settings/default',{model:ms.value||null,thinking:th.value,fallbacks:[...fb.querySelectorAll('input:checked')].map(i=>i.value)});if(x.error)throw new Error(x.error);S=x.result;return 'Saved: new chats and specialists use it.'},1);
  def.addEventListener('change',()=>say(dm,'Not saved yet: click Save to use it.',1));g.append(def);
  const us=card('Usage','the agents\' model work, last 30 days');us.classList.add('wide');g.style.gridAutoFlow='dense';g.append(us);post('usage').then(x=>{const U=x.result;if(!U||!U.model.length){us.append(h('p','empty','No answers yet.'));return}
   const n=v=>Number(v).toLocaleString(),tb=(k,t)=>{us.append(h('label','l',t));const tt=h('table');tt.innerHTML='<tr><th>'+(k==='user'?'Person':'Model')+'</th><th class=n>Answers</th><th class=n>Failed</th><th class=n>Input tokens</th><th class=n>Output tokens</th><th class=n>Cache tokens</th><th class=n>Cost</th></tr>';
    U[k].forEach(r=>{const tr=h('tr');[r.name,n(r.answers),n(r.failed),n(r.input),n(r.output),n(r.cache),money(r)].forEach((v,i)=>tr.append(h('td',i?'n':'',v)));tt.append(tr)});us.append(tt)};tb('user','Per person');tb('model','Per model');
   if(U.model.some(r=>r.unpriced))us.querySelector('h3').after(h('p','note','A Cost of — means the model has no prices yet: set them in US dollars per million tokens on its provider\'s card, and new answers count their cost.'))});
  S.providers.forEach(p=>{const c=card(p.name,`${p.id} · ${S.apis[p.api]||p.api}`);c.append(h('p','file',p.base_url),(()=>{const x=h('div','pmodels');x.append(h('span','file','Models'));p.models.forEach(m=>x.append(h('span','chip',m)));return x})(),h('p',p.key_set?'ok':'',p.key_builtin?'Uses the built-in connection\'s key':p.key_set?`API key set, ends in ${p.key_ends}`:'No API key'));
   // each model's prices, US dollars per million tokens: Pi computes every answer's cost from them; none: tokens only
   const F=['input','output','cacheRead','cacheWrite'],pt=h('table','prices'),pin=p.models.map(m=>{const tr=pt.insertRow(),mc=tr.insertCell();mc.textContent=m;mc.title=m;mc.className='mname';
    return F.map(k=>{const i=h('input','f');i.type='number';i.min='0';i.step='any';i.value=p.prices?.[m]?.[k]??'';i.setAttribute('aria-label',`${m}: ${k} price`);i.placeholder='$ / 1M';tr.insertCell().append(i);return i})});
   pt.createTHead().innerHTML='<tr><th>Model · US $ per 1M tokens</th><th>Input</th><th>Output</th><th>Cache read</th><th>Cache write</th></tr>';c.append(pt);
   act(c,'Save prices',async()=>{const prices={};p.models.forEach((m,j)=>{const v=Object.fromEntries(F.map((k,x)=>[k,pin[j][x].value]).filter(([,v])=>v!=='').map(([k,v])=>[k,+v]));if(Object.keys(v).length)prices[m]=v});
    const x=await post('settings/provider',{provider:{...p,prices}});if(x.error)throw new Error(x.error);S=x.result;return 'Saved: new chats count the cost of these models.'});
   act(c,'Test connection',async()=>{const x=await post('settings/test',{id:p.id});if(x.error)throw new Error(x.error);if(!x.result.ok)throw new Error(x.result.message);return x.result.message});
   act(c,'Remove',async()=>{const x=await post('settings/remove',{id:p.id});S=x.result;draw()});g.append(c)});
  const add=card('Connect a provider','OpenAI, Anthropic, OpenRouter, Ollama, Gemini or any OpenAI-compatible endpoint'),pre=h('select','f'),id=h('input','f'),nm=h('input','f'),api=h('select','f'),base=h('input','f'),key=h('input','f'),mods=h('input','f');
  Object.entries(PRESETS).forEach(([k,v])=>pre.append(new Option(v[0],k)));Object.entries(S.apis).forEach(([k,v])=>api.append(new Option(v,k)));key.type='password';key.autocomplete='off';key.placeholder='stored encrypted; empty keeps the stored key';mods.placeholder='model IDs, comma separated';
  const pick=h('div','checks mlist');mods.type='hidden';
  const fill=()=>{const v=PRESETS[pre.value];id.value=pre.value==='custom'?'':pre.value;nm.value=v[0];api.value=v[1];base.value=v[2];mods.value='';pick.innerHTML='<p class=file>Fetch models to pick from the provider\'s list.</p>'};pre.onchange=fill;fill();
  const fetchM=async()=>{const x=await post('settings/models',{provider:{id:id.value.trim(),api:api.value,base_url:base.value.trim()},key:key.value});if(x.error)throw new Error(x.error);
   if(!x.result.ok)throw new Error(x.result.message);const want=new Set(mods.value.split(',').map(s=>s.trim()).filter(Boolean));pick.innerHTML='';
   x.result.models.forEach(m=>{const l=h('label','chk'),i=h('input');i.type='checkbox';i.value=m;i.checked=want.has(m);i.onchange=()=>{mods.value=[...pick.querySelectorAll('input:checked')].map(i=>i.value).join(',')};l.append(i,document.createTextNode(m));pick.append(l)});
   mods.value=[...pick.querySelectorAll('input:checked')].map(i=>i.value).join(',');return x.result.message+' Tick the ones the agent may use.'};
  add.append(h('label','l','Preset'),pre,h('label','l','ID'),id,h('label','l','Name'),nm,h('label','l','API'),api,h('label','l','Base URL'),base,h('label','l','API key'),key,h('label','l','Models'),pick,mods);
  act(add,'Fetch models',fetchM);
  act(add,'Save provider',async()=>{const x=await post('settings/provider',{provider:{id:id.value.trim(),name:nm.value,api:api.value,base_url:base.value.trim(),models:mods.value.split(',').map(s=>s.trim()).filter(Boolean)},key:key.value});if(!mods.value)throw new Error('Fetch models and tick at least one.');
   key.value='';if(x.error)throw new Error(x.error);S=x.result;draw();return 'Saved.'},1);if(S.providers.length){add.classList.add('fold');const o=h('button','btn opener','Connect another provider');o.prepend(ic('plus'));o.onclick=()=>{add.classList.remove('fold');o.remove()};add.querySelector('h3').after(o)}g.append(add);people()};
 const people=async()=>{const r=(await post('sso')).result||{providers:[]},M=r.mail||{},f=(ph,v,t)=>{const i=h('input','f');i.placeholder=ph;i.value=v??'';if(t)i.type=t;i.autocomplete='off';return i};
  const warn=r.reachable?null:h('p','note','Emailed links and single sign-on need the address people open this app at, such as https://abi.example.com. The administrator sets it once for the install (ABI_PUBLIC_URL).');
  const em=card('Email','invites, "forgot your password" links, and every email of a project without its own mail server (reports, Monitor alerts, flows, the agent; a new domain asks once per project); without it you set passwords in Users & access'),host=f('smtp.example.com',M.host),port=f('587',M.port),from=f('abi@example.com',M.from),sec=h('select','f'),us=f('user name (optional)',M.username),pw=f(M.username?'password (stored in ABI; empty keeps it)':'password','','password');
  ['starttls','tls','none'].forEach(x=>sec.append(new Option({starttls:'STARTTLS (port 587)',tls:'TLS (port 465)',none:'none (a local relay)'}[x],x)));sec.value=M.security||'starttls';
  em.append(...(warn?[warn.cloneNode(true)]:[]),h('p','state'+(M.host?' on':''),M.host?`On: ${M.host}:${M.port} from ${M.from}`:'Not set up yet'),h('label','l','Server'),host,h('label','l','Port'),port,h('label','l','Connection'),sec,h('label','l','From'),from,h('label','l','Sign-in'),us,pw);
  act(em,'Save',async()=>{const x=await confirmed(await post('mail',{host:host.value,port:Number(port.value),security:sec.value,from:from.value,username:us.value,password:pw.value}));pw.value='';if(!x)return 'Not changed.';draw();return 'Saved.'},1);
  if(M.host)act(em,'Turn off',async()=>{const x=await confirmed(await post('mail',{off:true}));if(x)draw();return x?'Off.':'Not changed.'});g.append(em);
  const so=card('Single sign-on','people sign in with your company account (OpenID Connect: Google, Microsoft Entra, Okta, Keycloak, …)'),pid=f('id, e.g. google'),pn=f('button name, e.g. Google'),iss=f('issuer, e.g. https://accounts.google.com'),cl=f('client ID'),cs=f('client secret (stored in ABI)','','password');
  so.append(...(warn?[h('p','note','Single sign-on also needs the address people open this app at (see Email).')]:[]),h('p','file','Redirect URI at your provider: '+(r.callback.startsWith('/')?'(the app\'s address)':'')+r.callback.replace('<id>','{id}')),...r.providers.map(x=>{const row=h('div','row');row.append(h('span','',x.name+' · '+x.id),btn('Remove',async()=>{const y=await confirmed(await post('sso/remove',{id:x.id}));if(y)draw()}));return row}),
   h('h4','','Add a provider'),fld('ID',pid),fld('Button name',pn),fld('Issuer',iss),fld('Client ID',cl),fld('Client secret',cs),h('p','','Each person links it once: they sign in with their password, click their name and Link.'));
  act(so,'Add',async()=>{const x=await confirmed(await post('sso/save',{id:pid.value.trim(),name:pn.value.trim(),issuer:iss.value.trim(),client_id:cl.value.trim(),secret:cs.value}));cs.value='';if(!x)return 'Not added.';draw();return 'Added.'},1);g.append(so)};
 draw()}
function transferPage(box){if(MODE==='viewer'){box.append(h('p','empty','Open this on a computer, in Developer, to export or import the project.'));return}
 const ex=card('Export','the whole project as one zip'),im=card('Import','replace this project with a zip; ABI checks it first'),g=h('div','cards top');
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
 g.append(ex,im);box.append(g);{const [chk,apl]=im.querySelectorAll(':scope>.acts>.btn');chk.disabled=apl.disabled=true;f.addEventListener('change',()=>{chk.disabled=!f.files[0];data=null;apl.disabled=true});
  new MutationObserver(()=>{if(data&&apl.disabled&&!apl.classList.contains('busy'))apl.disabled=false;if(!f.files[0])chk.disabled=true}).observe(out,{childList:true})}}
// Export: ABI renders the report as the person (PDF, PNG, PPTX; several pages in one file)
function exportCard(rep,filters,curPage){const c=card('Export',`rendered by ABI · ${rep.name||rep.id}`),fm=h('select','f'),vs=h('select','f'),pg=h('div','checks'),ap=h('input');
 ['pdf','pptx','png'].forEach(f=>fm.append(new Option({pdf:'PDF',pptx:'PowerPoint (PPTX)',png:'Image (PNG)'}[f],f)));ap.type='checkbox';ap.checked=true;
 rep.pages.forEach(p=>{const l=h('label','chk'),x=h('input');x.type='checkbox';x.value=p.id;x.checked=p.id===curPage().id;l.append(x,h('span','',p.name||p.id));pg.append(l)});
 const fill=()=>{const sel=[...pg.querySelectorAll('input:checked')];vs.innerHTML='';vs.append(new Option('the whole page',''));vs.disabled=sel.length!==1;
  if(sel.length===1)rep.pages.find(p=>p.id===sel[0].value).visuals.forEach(v=>vs.append(new Option(v.title||v.id,v.id)))};pg.onchange=fill;fill();
 const al=h('label','chk');al.append(ap,h('span','','with the filters set now'));
 const vl=h('label','l','One view only (optional)');c.append(h('label','l','Format'),fm,h('label','l','Pages'),pg,vl,vs,al);const vsh=()=>{vl.hidden=vs.hidden=vs.disabled};pg.addEventListener('change',vsh);setTimeout(vsh);
 act(c,'Download',async m=>{const pages=[...pg.querySelectorAll('input:checked')].map(x=>x.value);if(!pages.length)return 'Pick at least one page.';
  say(m,'Rendering… ABI draws two exports at a time, so this one may wait for a turn.');  // M3.2: a third waits, never fails
  const one=pages.length===1,body={report:rep.id,format:fm.value,filters:ap.checked?filters():[],...(one?{page:pages[0],visual:vs.value||null}:{pages})};
  const get=async b=>{const r=await fetch('/report/export',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify(b)});if(!r.ok)throw new Error((await r.json()).error);return r.blob()};
  const save=(blob,name)=>{const a=h('a');a.href=URL.createObjectURL(blob);a.download=name;a.click()};
  try{const fn=`${rep.id}${one?'-'+pages[0]:''}.${fm.value}`;save(await get(body),fn);return `Downloaded ${fn}: it is in your downloads.`}
  catch(e){if(one||fm.value==='pptx')throw e;for(const p of pages)save(await get({...body,pages:undefined,page:p}),`${rep.id}-${p}.${fm.value}`);
   return `This ABI makes one file per page: downloaded ${pages.length}.`}},1);
 c.classList.add('editor');return c}
// Email this report (Developer): ABI's flow email_<report>, a schedule and send_email with every page as one PDF, through
// Settings › Email; saving it asks ABI's confirmation (it runs unattended; a recipient domain new to the project, once)
function mailCard(rep){const f=(D.flows||[]).find(x=>x.id===('email_'+rep.id).slice(0,63)),cr=(f?.trigger?.cron||'0 8 * * 1').split(' '),was=f?.steps?.[0]?.input||{};
 const at=`${cr[1].padStart(2,'0')}:${cr[0].padStart(2,'0')}`,c=card('Email this report',f?`${f.enabled?'on':'off'} · ${at} ${f.trigger.timezone}`:'every page as one PDF, on the days you pick');
 const to=inpt('name@company.com, …',(was.to||[]).join(', ')),days=h('div','checks'),tm=inpt('',at,'time'),tz=inpt('time zone, e.g. Europe/Berlin',f?.trigger?.timezone||Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC'),on=new Set((cr[4]||'').split(',').map(Number));
 [[1,'Mon'],[2,'Tue'],[3,'Wed'],[4,'Thu'],[5,'Fri'],[6,'Sat'],[0,'Sun']].forEach(([v,l])=>{const x=h('label','chk'),i=h('input');i.type='checkbox';i.value=v;i.checked=on.has(v);x.append(i,h('span','',l));days.append(x)});
 // the days as toggles with two presets, the time zone from a list, and the schedule said back in one sentence
 if(f)c.querySelector('h3').append(h('span','state'+(f.enabled?' on':''),f.enabled?'Scheduled':'Off'));
 days.classList.add('days');const pre=h('div','row seg'),setD=vs=>{days.querySelectorAll('input').forEach(i=>i.checked=vs.includes(+i.value));say0()};
 [['Weekdays',[1,2,3,4,5]],['Every day',[0,1,2,3,4,5,6]]].forEach(([l,vs])=>{const b=h('button','chip',l);b.type='button';b.onclick=()=>setD(vs);pre.append(b)});
 const zl=h('datalist');zl.id='tz-list';try{Intl.supportedValuesOf('timeZone').forEach(z=>zl.append(new Option(z)))}catch(e){}tz.setAttribute('list','tz-list');
 const sum=h('p','sum'),mails=()=>to.value.split(',').map(s=>s.trim()).filter(Boolean),picked=()=>[...days.querySelectorAll('input:checked')].map(i=>+i.value);
 const say0=()=>{const d=picked(),n=mails().length,dn=d.length===7?'every day':d.length===5&&!d.includes(0)&&!d.includes(6)?'every weekday':d.length?d.sort((a,b)=>((a+6)%7)-((b+6)%7)).map(x=>DOW[x]).join(', '):'';
  const [hh,mm]=(tm.value||'08:00').split(':').map(Number),now=new Date();let nx=null;for(let k=0;k<8&&!nx;k++){const x=new Date(now);x.setDate(now.getDate()+k);x.setHours(hh,mm,0,0);if(d.includes(x.getDay())&&x>now)nx=x}
  sum.textContent=!d.length?'Pick at least one day.':`Sends ${dn} at ${tm.value||'08:00'} (${tz.value.trim()||'UTC'})${n?` to ${n===1?mails()[0]:n+' people'}`:''}: every page as one PDF.${nx?' Next: '+nx.toLocaleDateString(undefined,{weekday:'long',day:'numeric',month:'short'})+'.':''}`};
 [to,tm,tz].forEach(e=>e.addEventListener('input',say0));days.addEventListener('change',say0);say0();
 c.append(lab('To'),to,lab('Days'),pre,days,h('div','two'),sum,h('p','file','ABI renders it when it sends, as you (your access to the data), and mails it from Settings › Email.'),zl);
 {const two=c.querySelector(':scope>.two');two.append(fld('At',tm),fld('Time zone',tz))}
 // no mail server yet: say so before it is saved (it would fail at its first send)
 if((window.ME||{admin:true}).admin)fetch('/admin/sso',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:'{}'}).then(r=>r.json()).then(r=>{if(r.result&&!r.result.mail?.host){const n=h('p','note');n.append('Email is not set up yet, so a send would fail. ');const go=h('button','flink','Set it up in Settings › Email');go.onclick=()=>show('settings');n.append(go);c.querySelector(':scope>h3').after(n)}}).catch(()=>{});
 const post=b=>fetch('/report/email',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify({report:rep.id,...b})}).then(r=>r.json()).finally(refreshPending);
 act(c,'Save schedule',async m=>{const ms=mails();need(to,ms.length,'Add at least one email address.');const badm=ms.find(x=>!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(x));need(to,!badm,`${badm} is not an email address.`);
  need(days.querySelector('input'),picked().length,'Pick at least one day.');
  const x=await confirmed(await post({to:ms,days:picked(),time:tm.value,timezone:tz.value.trim()}));
  if(!x)return 'Not saved.';const cm=h('button','btn','Commit now');cm.onclick=()=>$('#changes').click();setTimeout(()=>{if(c.isConnected)m.append(' ',cm)});return `Saved in ABI. ${sum.textContent} Commit to keep it.`},1);
 if(f)act(c,'Stop emailing',async()=>(await confirmed(await post({off:true})))?'Stopped.':'Still on.');
 c.classList.add('editor');return c}
function reportsPage(box){newBtn(box,'report');const g=h('div','cards stack');box.append(g);
 if(!D.reports.length){box.append(emptyBox('reports','No reports yet','A report puts headline numbers and charts from your measures on one page, filtered and shared from one place. Pick a few measures and it is made.',MODE==='developer'&&!MOBILE?['New report',()=>$('main .bar.top .btn.pri')?.click()]:null));return}
 D.reports.forEach(r=>{const c=card(r.name||r.id),n=r.pages.reduce((a,p)=>a+p.visuals.length,0);c.querySelector('h3').title=`reports/${r.id}.json`;
  c.append(h('p','',r.spec?.description||r.description||''),h('p','file',`${r.pages.length} page${r.pages.length===1?'':'s'}: ${r.pages.map(p=>p.name||p.id).join(', ')} · ${n} visuals`));
  act(c,'Open',async()=>{show('rep:'+r.id)},1);g.append(c)})}
// Monitor: is anything wrong right now? Only what reports can't show: data freshness, model readiness, ML runs, flows
// a check: alert when a measure crosses a value, a table goes stale, or a flow's last run failed
const CMP={'<':(a,b)=>a<b,'<=':(a,b)=>a<=b,'>':(a,b)=>a>b,'>=':(a,b)=>a>=b,'=':(a,b)=>a==b,'!=':(a,b)=>a!=b};
const OPS=[['<','is below'],['<=','is at most'],['>','is above'],['>=','is at least'],['=','equals'],['!=','is not']],opw=o=>(OPS.find(x=>x[0]===o)||[o,o])[1],mname=id=>D.measures.find(m=>m.id===id)?.name||id;
// a measure check compares the measure with a value, with another measure (a target), or with its previous period (% change);
// optionally filtered, and optionally for each member of a dimension (alerts when any member matches). ABI evaluates it
// (check_condition, the condition its flow checks every minute on the server): the verdict and every figure are its rows
// 1.2.6 M8: a check for each object of a type (its `by` is the type's key) alerts once per object, only to people who may see
// that object: everyone who can, or the people the object names (a property with the meaning email or person, its own
// or of the objects it links to). In words, never ids
const perType=by=>by&&OT().find(T=>T.key.length===1&&by===`${T.table}.${T.key[0]}`),ppl=T=>(T?.properties||[]).filter(p=>['email','person'].includes(p.meaning)),
 toKey=t=>t?JSON.stringify(t.link?{link:t.link,property:t.property}:{property:t.property}):'';
function sendTo(T){const nm=p=>(p.name||colName(p.column)).toLowerCase(),o=[['',`everyone who can see the ${low(T)}`]];
 ppl(T).forEach(p=>o.push([toKey({property:p.column}),`the ${low(T)}'s ${nm(p)}`]));
 (T.links||[]).forEach(l=>ppl(otype(l.to)).filter(p=>!p.column.includes('.')).forEach(p=>o.push([toKey({link:l.link,property:p.column}),`the ${low(T)}'s ${/ \(inverse\)$/.test(l.name)?low(otype(l.to)):l.name} (${nm(p)})`])));return o}
const nobody=r=>r?.status==='stopped'&&/name no project member/.test(r.error||'');  // a per-object alert whose recipients aren't in the project
const toWords=(T,to)=>(sendTo(T).find(o=>o[0]===toKey(to))||[0,'the people it names'])[1];
// a draft check as the condition ABI evaluates (as the server's monitor_condition builds it)
function monCond(c){if(c.kind!=='measure'||!c.measure)return null;const ag=c.against||'value',other=ag==='measure'?c.other:null;
 return {query:{measures:[c.measure,...(other?[other]:[])],filters:c.filters||[],dimensions:c.by&&ag!=='prev'?[c.by]:[]},measure:c.measure,op:c.op,value:other?0:Number(c.value),
  ...(other?{other}:{}),...(ag==='prev'?{change:true}:{}),...(c.period?{period:c.period,skip_latest:!!c.skip_latest}:{})}}
async function monEval(c){if(c.kind==='measure'){const k=c.condition;if(!k)return {ok:false,text:`${c.name}: this check is incomplete; edit it`};
  const T=perType(c.by),td=T&&`${T.table}.${T.title}`;  // an object is named by its title, read beside its key
  const r=await api('check_condition',T&&td!==c.by?{...k,query:{...k.query,dimensions:[c.by,td]}}:k),rs=r.rows||[],ix=x=>r.columns.indexOf(x),nk=k.period?ix(k.period)+1:0,per=(k.period||'').split('.').pop();
  const lbl=v=>v.length>1?(per==='month'?`${MON[v[1]-1]||v[1]} ${v[0]}`:per==='quarter'?`Q${v[1]} ${v[0]}`:`${per} ${v[1]} of ${v[0]}`):cell(v[0]),f=c.filters||[],where=f.length?` (${f.map(x=>`${x.column} = ${x.value}`).join(', ')})`:'',i=ix(c.measure);
  if(k.change){const x=rs[0];if(!x)return {ok:true,text:`${mname(c.measure)}: not enough ${per}s to compare yet${where}`};const p=x[ix('change_pct')];  // the latest (completed) period and the one before
   return {ok:!r.holds,text:`${mname(c.measure)}: ${fmt(x[i])} in ${lbl(x.slice(0,nk))}, ${p>=0?'+':''}${fmt(p)}% on ${lbl(x.slice(i+1,i+1+nk))} (${fmt(x[ix('previous_'+c.measure)])})${where} · alerts when the change ${opw(c.op)} ${fmt(k.value)}%`}}
  const j=k.other?ix(k.other):-1,what=k.other?mname(k.other):fmt(k.value),at=nk&&rs[0]?` in ${lbl(rs[0].slice(0,nk))}`:'';
  if(c.by){const b=ix(T?td:c.by),bad=rs.filter(x=>typeof x[i]==='number'&&CMP[c.op](x[i],j>=0?x[j]:k.value)),who=T?`${T.plural.toLowerCase()}: ${mname(c.measure)}`:c.by.split('.').pop();
   const pre=`${bad.length} of ${rs.length} ${who} ${opw(c.op)} ${what}: `,post=`${bad.length>5?'…':''}${where}${at}`;  // an object's name opens its page (monitorPage)
   return {ok:!r.holds,text:bad.length?pre+bad.slice(0,5).map(x=>x[b]).join(', ')+post:`No ${T?`${low(T)} where ${mname(c.measure)}`:who} ${opw(c.op)} ${what}${where}${at}`,
    ...(T&&bad.length?{pre,post,open:bad.slice(0,5).map(x=>({type:T.id,key:[x[ix(c.by)]],title:x[b]}))}:{})}}
  const x=rs[0]||[];return {ok:!r.holds,text:`${mname(c.measure)}: ${fmt(x[i])}${j>=0?` vs ${what} ${fmt(x[j])}`:''}${where}${at} · alerts when it ${opw(c.op)} ${what}`}}
 if(c.kind==='fresh'){const q=D.quality||{},ix=k=>(q.columns||[]).indexOf(k),row=(q.rows||[]).find(x=>x[0]===c.table),t=row&&row[ix('last_load')];
  const hrs=t?(Date.now()-Date.parse(t))/3600e3:Infinity;return {ok:hrs<=c.hours,text:`${c.table}: last load ${t?Math.round(hrs)+' h ago':'never'} · alerts after ${c.hours} h`}}
 const x=await api('flow_runs',{flow:c.flow}).catch(()=>({}));const one=x.runs?.[0]||x;return {ok:one.status!=='failed',text:`${c.flow}: last run ${one.status||'never'} · alerts when a run fails`}}
function newMonitor(box,ck){box.querySelector('.monform')?.remove();ck=ck||{};const c=card(ck.id?'Edit monitor':'New monitor','what to watch, against what; it turns red when the condition holds');c.classList.add('monform','wide','editor');
 const nm=h('input','f'),kind=h('select','f'),f=h('div');nm.value=ck.name||'';nm.placeholder='e.g. Revenue below target';[['measure','A measure crosses a value, a target or its last period'],['fresh','A table is not loaded in time'],['flow','A flow\'s last run failed']].forEach(([k,l])=>kind.append(new Option(l,k)));kind.value=ck.kind||'measure';
 const s=(x,v)=>{const e=h('select','f');x.forEach(([v,l])=>e.append(new Option(l,v)));if(v!==undefined)e.value=v;return e},L=t=>h('label','l',t);
 const draw=()=>{f.innerHTML='';
  if(kind.value==='measure'){const ms=D.measures.map(x=>[x.id,x.name||x.id]),m=s(ms,ck.measure),vs=s([['value','a value'],['measure','another measure (e.g. a target)'],['prev','its previous period (% change)']],ck.against||'value'),
    op=s(OPS,ck.op),v=h('input','f'),other=s(ms,ck.other),per=s(D.dims.filter(d=>d.startsWith('calendar.')).map(d=>[d,d]),ck.period),by=s([['','the total'],...OT().filter(T=>T.key.length===1).map(T=>[`${T.table}.${T.key[0]}`,`each ${low(T)}, with an alert of its own`]),...D.dims.filter(d=>!perType(d)).map(d=>[d,'each '+colName(d).toLowerCase()+' ('+d.split('.')[0]+')'])],ck.by||''),to=h('select','f'),
    fc=s([['','no filter'],...D.dims.map(d=>[d,`${colName(d)} · ${d.split('.')[0]}`])],ck.filters?.[0]?.column||''),fv=h('input','f');v.type='number';v.placeholder='value';v.value=ck.value??'';fv.placeholder='equals…';fv.value=ck.filters?.[0]?.value??'';
   // completed periods only, unless they untick it: with daily loads the latest period is still filling, so its change on a whole period would alert at every period's start
   const dn=h('label','chk'),dc=h('input');dc.type='checkbox';dc.checked=ck.id?!!ck.skip_latest:true;dn.append(dc,h('span','','Completed periods only (the latest may still be filling)'));
   const lv=L('Value'),lo=L('Other measure'),lp=L('Period'),lb=L('Check'),lt=L('Send to'),th=h('p','file','People are found by their account\'s email or name. Someone it names who isn\'t in this project, or can\'t see that one, gets nothing; your Inbox then says who.');to.onchange=()=>{th.hidden=!to.value};const vis=()=>{const a=vs.value,T=a!=='prev'&&perType(by.value);lv.hidden=v.hidden=a==='measure';lv.textContent=a==='prev'?'Change in %':'Value';lo.hidden=other.hidden=a!=='measure';lp.hidden=per.hidden=dn.hidden=a!=='prev';lb.hidden=by.hidden=a==='prev';
     lt.hidden=to.hidden=!T;if(T&&to.dataset.t!==T.id){to.innerHTML='';sendTo(T).forEach(([x,l])=>to.append(new Option(l,x)));to.dataset.t=T.id;to.value=ck.by===by.value?toKey(ck.to):'';if(to.selectedIndex<0)to.value=''}th.hidden=!T||!to.value};vs.onchange=by.onchange=vis;
   f.append(L('Measure'),m,L('Compare with'),vs,L('Alert when it'),op,lv,v,lo,other,lp,per,dn,lb,by,lt,to,th,L('Only where (optional)'),fc,fv);vis();
   f.get=()=>{const a=vs.value;if(a!=='measure'&&v.value==='')throw new Error('Give the value to compare with.');if(a==='prev'&&!per.value)throw new Error('The model has no calendar to compare periods.');
    return {measure:m.value,against:a,op:op.value,...(a==='measure'?{other:other.value}:{value:Number(v.value)}),...(a==='prev'?{period:per.value,skip_latest:dc.checked}:by.value?{by:by.value,...(perType(by.value)&&to.value?{to:JSON.parse(to.value)}:{})}:{}),...(fc.value?{filters:[{column:fc.value,op:'=',value:fv.value}]}:{})}}}
  else if(kind.value==='fresh'){const t=s((D.tables||[]).map(x=>[x,x]),ck.table),hr=h('input','f');hr.type='number';hr.value=ck.hours??36;f.append(L('Table'),t,L('Alert after (hours without a load)'),hr);f.get=()=>({table:t.value,hours:Number(hr.value)})}
  else{const fl=s((D.flows||[]).map(x=>[x.id,x.name||x.id]),ck.flow);f.append(L('Flow'),fl);f.get=()=>({flow:fl.value})}};kind.onchange=draw;draw();
 const em=h('input','f');em.type='email';em.placeholder='name@company.com (optional)';em.value=ck.id?monMail(ck)||'':/@/.test(window.ME?.name||'')?ME.name:'';
 c.append(L('Name'),nm,L('Watch'),kind,f,L('Also email the alert to'),em,h('p','file','A measure check becomes a flow in ABI: it checks it every minute, also with this page closed, and posts to the Inbox when it starts to hold.'));
 // the check said back in one sentence as it is filled in
 const sum=h('p','sum'),upd=()=>{try{const g=f.get(),nmOf=id=>D.measures.find(x=>x.id===id)?.name||id;
   const T=perType(g.by),rule=`${nmOf(g.measure)} ${(OPS.find(o=>o[0]===g.op)||[0,g.op])[1]} ${g.against==='measure'?nmOf(g.other):g.against==='prev'?`${g.value}% against its previous period`:fmt(g.value)}`;
   sum.textContent=kind.value==='measure'&&T?`For each ${low(T)}: alerts when ${rule}. Send to: ${toWords(T,g.to)}, in the Inbox${em.value.trim()?`. ${em.value.trim()} is emailed too, for the ${T.plural.toLowerCase()} that person can see, if they are in this project`:''}.`
    :kind.value==='measure'?`Alerts when ${rule}${g.by?`, for any ${colName(g.by).toLowerCase()}`:''}. It posts to the Inbox${em.value.trim()?' and emails '+em.value.trim():''}.`
    :kind.value==='fresh'?`Alerts when ${g.table} has gone ${g.hours} hours without a load.`:'Alerts when the flow\'s last run failed.'}catch(e){sum.textContent='Fill in the value to see what it alerts on.'}};
 c.insertBefore(sum,f);c.addEventListener('input',upd);c.addEventListener('change',upd);upd();
 act(c,'Check it now',async m=>{say(m,'Checking with ABI…');const d={id:'preview',name:nm.value||'preview',kind:kind.value,...f.get()};d.condition=monCond(d);const x=await monEval(d);m.className='msg';toast(x.ok?'Checked: quiet right now.':'Checked: it would alert now.');return (x.ok?'Quiet right now: ':'It would alert now: ')+x.text});
 act(c,ck.id?'Save':'Create',async()=>{need(nm,nm.value.trim(),'Give it a name, e.g. Revenue below target.');const nc={id:ck.id||nm.value.trim().toLowerCase().replace(/[^a-z0-9]+/g,'_').replace(/^_|_$/g,'')||'check',name:nm.value.trim(),kind:kind.value,...f.get()};
  if(await monPost({action:'save',check:nc,email:em.value.trim()})){show('monitor');setTimeout(()=>highlight(nc.name),300);toast(`${ck.id?'Saved':'Created'} “${nc.name}”: ABI checks it every minute and posts to the Inbox when it starts to hold.`)}},1);act(c,'Cancel',()=>c.remove());(box.querySelector('.pghead')||box).after(c)}
// a check and its flow in ABI (/monitor asks ABI's confirmation for the flow, then keeps the check); null: they said no
const monFlow=ck=>(D.flows||[]).find(x=>x.id===('monitor_'+ck.id).slice(0,63)),monMail=ck=>monFlow(ck||{})?.steps?.find(s=>s.action==='send_email')?.input?.to?.[0];
async function monPost(b){setTimeout(refreshPending,1500);const r=await confirmed(await fetch('/monitor',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify(b)}).then(x=>x.json()));
 if(r&&Array.isArray(r.result))D.app.monitor.checks=r.result;return r}
function monitorPage(box){const bar=h('div','row bar top');bar.append(btn('New monitor',()=>newMonitor(box),1));box.append(bar);
 const mine=h('div','cards');box.append(mine);(D.app?.monitor?.checks||[]).forEach(ck=>{const c=card(ck.name);c.querySelector('h3').append(pinBtn({kind:'monitor',ref:ck.id,title:ck.name}));const st=h('p','file','checking…');c.append(st);mine.append(c);
  const bd=h('span','state','Checking');c.querySelector('h3').append(bd);
  monEval(ck).then(x=>{bd.className='state '+(x.ok?'':'hold');bd.textContent=x.ok?'Quiet':'Holds now';st.className='mtext';st.textContent=x.open?x.pre:x.text;
   (x.open||[]).forEach((o,i)=>{const b=h('button','flink oopen',String(o.title??o.key[0]));b.title=`Open this ${low(otype(o.type))}'s page`;b.onclick=()=>openObj(o.type,o.key);st.append(i?', ':'',b)});if(x.open)st.append(x.post)}).catch(e=>{bd.className='state bad';bd.textContent='Error';st.className='bad';st.textContent=e.message});
  const fl=monFlow(ck),PT=fl?.trigger?.per&&perType(ck.by);if(MODE==='developer'&&ck.condition)c.append(h('p','file',fl?(fl.enabled?(PT?`For each ${low(PT)}: ABI checks it every minute, also with this page closed. Send to: ${toWords(PT,fl.trigger.recipients)}, in the Inbox${monMail(ck)?'; '+monMail(ck)+' is emailed too':''}.`:`ABI checks it every minute, also with this page closed; when it starts to hold it posts to the Inbox${monMail(ck)?' and emails '+monMail(ck):''}.`):'Background checks are off: checked only while this page is open.'):'Checked only while this page is open.'));
  if(MODE==='developer'&&PT&&fl.trigger.recipients)api('flow_runs',{flow:fl.id,limit:1}).then(x=>{if(nobody(x.runs?.[0]))st.after(h('p','bad',`The last alert reached nobody: the person it names isn't in this project, or can't see that ${low(PT)}. Your Inbox says who.`))}).catch(()=>{});
  if(MODE==='developer'&&ck.condition&&!fl)act(c,'Alert with the page closed',async()=>{if(await monPost({action:'save',check:ck,email:''}))show('monitor')});
  if(MODE==='developer')act(c,'Edit',()=>newMonitor(box,ck));if(MODE==='developer')act(c,'Delete',async()=>{if(await monPost({action:'delete',id:ck.id}))show('monitor')})});
 const g=h('div','cards top');box.append(g);const q=D.quality||{},ix=c=>(q.columns||[]).indexOf(c),rs=q.rows||[],when=t=>t?when2(t):'—';
 const M=D.app?.monitor||{watch:[],fresh_within_hours:36},watch=t=>M.watch?.length?M.watch.includes(t):/stream/.test(t),old=r=>watch(r[0])&&(!r[ix('last_load')]||Date.now()-Date.parse(r[ix('last_load')])>M.fresh_within_hours*3600e3);
 const fr=card('Data freshness',`ABI data quality; watched tables must load within ${M.fresh_within_hours} h (monitor.json)`),bad=rs.filter(r=>(r[ix('problems')]||[]).length||r[ix('missing_periods')]||old(r));
 fr.append(h('p',bad.length?'bad':q.masked?'':'ok',bad.length||!q.masked?freshLine(bad.length,rs.length):`${rs.length} loaded tables`+(MODE==='viewer'?' in the published version':'')));
 const t=h('table'),shown=[...bad,...rs.filter(r=>watch(r[0])&&!bad.includes(r))],probs=r=>plainGap(plur([...(r[ix('problems')]||[]).map(p=>typeof p==='string'?p:p.message||p.kind||JSON.stringify(p)),...(old(r)?[`no load for over ${M.fresh_within_hours} h`]:[])].join('; ')));
 t.insertRow().append(...['Table','Last load','Rows','Missing periods','Problems'].map((x,i)=>h('th',i===2||i===3?'n':'',x)));
 shown.forEach(r=>{const tr=t.insertRow();[r[0],when(r[ix('last_load')]),r[ix('rows')]==null?'not shown':fmt(r[ix('rows')]),r[ix('missing_periods')]??(q.masked?'not shown':0),probs(r)||'—'].forEach((v,i)=>{const td=tr.insertCell();td.textContent=v;if(i===2||i===3)td.className='n'})});
 if(!shown.length)fr.append(h('p','file',q.masked?'The details of each table are not shown with your access.':'No table is watched for freshness yet: every table loaded without problems.'));
 else if(MOBILE){const l=h('div');shown.forEach(r=>{const x=h('div','srow'),n=h('div','sname'),p=probs(r);n.append(h('b','',r[0]),h('div','file',`last load ${when(r[ix('last_load')])}${p?' · '+p:''}`));x.append(n,h('span','state'+(p?' bad':' on'),p?'Needs a look':'OK'));l.append(x)});fr.append(l)}
 else{const sc=h('div','scroll all');sc.append(t);fr.append(h('p','file','Tables with problems first, then the watched tables.'),sc)}
 const rd=D.readiness||{},mo=card('Model readiness');mo.append(h('p','state'+(rd.ready?' on':' bad'),rd.ready?'Ready: every table, join and measure checks out':'Not ready'),...(rd.missing||[]).map(m=>h('p','rdy',plainReady(m))));if(!rd.ready&&MODE==='developer'&&(rd.missing||[]).some(m=>/row-level|row rule/i.test(plainReady(m))))act(mo,'Open Security',()=>show('security'));if(rd.ready&&MODE==='viewer')mo.append(h('p','file','Checked on the published version, as you see it.'));
 const ml=card('ML models','latest training and scoring');[[fr,'freshness'],[mo,'model'],[ml,'ml']].forEach(([c,r])=>c.querySelector('h3').append(pinBtn({kind:'status',ref:r,title:{freshness:'Data freshness',model:'Model readiness',ml:'ML models'}[r]})));if(!D.ml.length)ml.append(h('p','empty','No ML models yet.'));
 D.ml.forEach(m=>{const tr=m.latest_run||{},sc2=m.latest_score||{};ml.append(h('h4','',m.name||m.id),
  h('p',tr.status==='ok'?'ok':tr.status?'bad':'file',tr.status?`Trained ${tr.status==='ok'?'':tr.status+' '}· ${when(tr.finished||tr.created)}`:MODE==='viewer'?'No training run in the published version':'Not trained yet'),h('p',sc2.status==='ok'?'ok':sc2.status?'bad':'file',sc2.status?`Scored ${sc2.status==='ok'?'':sc2.status+' '}· ${when(sc2.finished||sc2.created)}`:MODE==='viewer'?'No scoring run in the published version':'Not scored yet'))});
 const fl=card('Flows','switched on or off, last run');fl.querySelector('h3').append(pinBtn({kind:'status',ref:'flows',title:'Flows'}));if(!D.flows.length)fl.append(h('p','empty',MODE==='viewer'?'None in the published version: flows run in the working project, and what they post reaches your Inbox.':'No flows yet.'));
 D.flows.forEach(f=>{const l=h('div','file','Last run: …'),r0=h('div','srow'),nm=h('div','sname');nm.append(h('b','',f.name||f.id),l);r0.append(nm,h('span','state'+(f.enabled?' on':''),f.enabled?'On':'Off'));fl.append(r0);
  api('flow_runs',{flow:f.id}).then(x=>{const r=(x.runs||[])[0];l.textContent=r?`Last run: ${{ok:'finished',failed:'failed',running:'running'}[r.status]||r.status} · ${when(r.finished||r.started||r.created)}`:'Not run yet'}).catch(e=>l.textContent='Last run: '+e.message)});
 g.append(fr,mo,ml,fl);fr.style.gridColumn='1/-1'}
// Inbox: what flows posted to this project (Monitor alerts, scheduled posts), your subscriptions' new results and your test
// runs, and its alerts (a failed flow, a data problem), as ABI shows them to you: under row-level security a Viewer gets
// their own copy of each post, with their rows. Alerts are read or not as ABI keeps it per person; messages: this browser
const SEEN='abi-inbox-'+D.project,seen=()=>{try{return +localStorage.getItem(SEEN)||0}catch(e){return 0}};let INBOXN='';
const ipost=b=>fetch('/inbox',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify(b||{})}).then(r=>r.json());
async function inboxCount(){const r=await ipost().catch(()=>({}));if(!r.result)return;const s=seen(),b=side.querySelector("button.it[data-k='inbox']");
 INBOXN=new Set(r.result.messages.filter(m=>m.id>s).map(m=>m.text)).size+new Set(r.result.alerts.filter(a=>!a.read&&(MODE!=='viewer'||!/^(Semantic model|Flow )/i.test(a.message||''))).map(a=>String(a.message||'').split(':')[0].slice(0,60))).size||'';if(b){b.querySelector('.n')?.remove();if(INBOXN)b.append(h('span','n',String(INBOXN)))}}
async function inboxPage(box){const r=await ipost();if(r.error){box.append(h('p','bad',r.error));return}const {messages:M,alerts:A}=r.result,s=seen(),g=h('div','cards');box.append(g);
 const mc=card('Messages','posted by flows and Monitor, your subscriptions and your test runs');mc.classList.add('wide');
 if(!M.length)mc.append(h('p','empty','No messages yet. A Monitor check posts here when it starts to hold; flows can post here too.'));
 let lastM=null,foldM=null;M.forEach(m=>{const d=h('div','item'+(m.id>s?' new':'')),fl=h('div','file');fl.append(ic(/^Monitor/.test(m.text)?'monitor':/^Flow/.test(m.text)?'flows':'inbox'),`${cell(m.at)} · ${m.sender}`);d.append(fl,txt(m.text));
  const O=m.data?.object&&otype(m.data.object.type)?m.data.object:null;  // 1.2.6 M8: an alert about one object opens that object's page
  if(m.data?.objects&&otype(m.data.type)){const cs=m.data.columns.filter(c=>!m.data.keys.includes(c)),tb=h('table'),sc=h('div','scroll');  // several objects: each name opens its page
   tb.insertRow().append(...[otype(m.data.type).name,...cs.map(colName)].map((x,i)=>h('th',i?'n':'',x)));
   m.data.objects.forEach((o,i)=>{const tr=tb.insertRow(),b=h('button','flink oopen',String(o.title)),r=m.data.rows[i]||{};b.onclick=()=>openObj(m.data.type,o.key);tr.insertCell().append(b);
    cs.forEach(c=>{const td=tr.insertCell(),v=r[c];td.className='n';td.textContent=typeof v==='number'&&!/(^|[._])year$/.test(c)?fmt(v):String(v??'—')})});sc.append(tb);d.append(sc)}
  else if(m.data?.columns?.length){const t=h('div','mdata'),y=m.data.columns.filter(c=>/(^|[._])year$/.test(c));  // a year reads 2026, not 2,026
   if((m.data.rows||[]).length===1&&m.data.columns.length<=4){const r=m.data.rows[0],f=h('div','figs');m.data.columns.forEach(c=>{const v=Array.isArray(r)?r[m.data.columns.indexOf(c)]:r[c],x=h('span','fig');x.append(h('b','',m.data.measures?.[c]||colName(c)),h('span','',typeof v==='number'&&!y.includes(c)?fmt(v,m.data.format?.[c]):String(v??'—')));f.append(x)});d.append(f)}
   else{rows(t,{...m.data,rows:m.data.rows.map(r=>({...r,...Object.fromEntries(y.map(c=>[c,r[c]==null?r[c]:String(r[c])]))}))});d.append(t)}
   // 1.2.6 M7: rows grouped by an object type's title or key are objects: a row (or Open beside one figure) shows its page
   {const cs=m.data.columns,rs=(m.data.rows||[]).map(r=>Array.isArray(r)?r:cs.map(c=>r[c])),T=!O&&objCol(cs[0]);if(T&&d.contains(t))objClicks(t,cs[0],{rows:rs});
    else if(T&&rs[0]?.[0]!=null){const o=h('button','flink oopen',`Open ${rs[0][0]}`);o.onclick=()=>objOpen(T,cs[0],rs[0][0]);d.append(o)}}}if(O){const o=h('button','flink oopen',`Open ${O.title??'it'}`);o.title=`Open this ${low(otype(O.type))}'s page`;o.onclick=()=>openObj(O.type,O.key);d.append(o)}if(m.snapshot){const a=h('a','',`Open the ${m.snapshot.split('.').pop().toUpperCase()}`);a.href=m.snapshot;a.target='_blank';d.append(a)}
  if(lastM&&lastM.text===m.text){if(!foldM){foldM=h('details','more');foldM.append(h('summary',''));mc.append(foldM)}foldM.append(d);foldM.querySelector('summary').textContent=`${foldM.children.length-1} earlier like it`}else{mc.append(d);foldM=null}lastM=m});
 const ac=card('Alerts','failed flows and data problems, as ABI reported them'),un=A.filter(a=>!a.read);if(!A.length)ac.append(h('p','empty','No alerts. ABI posts here when the model or the data needs a look.'));
 ac.classList.add('wide');
 // alerts that say the same thing (same opening, e.g. 'Semantic model is not Ready') fold under the newest one
 const key=a=>String(a.message||'').split(':')[0].slice(0,60);let last=null,fold=null;const sev=t=>/\b(not ready|fail|failed|error|missing|problem|could not|refused)\b/i.test(t)?'bad':/\b(is ready|recovered|succeeded|finished)\b/i.test(t)?'okay':'info';
 A.filter(a=>MODE!=='viewer'||!/^(Semantic model|Flow )/i.test(a.message||'')).forEach(a=>{const d=h('div','item sev-'+sev(a.message)+(a.read?'':' new')),fl=h('div','file');const am=a.message||'';fl.append(ic(/^Semantic model/.test(am)?'model':/^Flow/.test(am)?'flows':/^Monitor/.test(am)?'monitor':/\b(email|mail|smtp)\b/i.test(am)?'mail':/\b(ML|forecast|scor)/.test(am)?'ml':/\b(fresh|load|refresh|missing (days|periods))/i.test(am)?'refresh':/\b(row rule|row-level|security)/i.test(am)?'security':'alert'),cell(a.at));d.append(fl,txt(plainReady(am)));
  if((window.ME||{admin:true}).admin&&!MOBILE&&/\b(smtp|mail server|send_email|email)\b/i.test(a.message||'')){const go=h('button','flink','Check the mail server in Settings › Email');go.onclick=()=>show('settings');d.append(go)}
  if(last&&key(last)===key(a)){if(!fold){fold=h('details','more');fold.append(h('summary',''));ac.append(fold)}fold.append(d);fold.querySelector('summary').textContent=`${fold.children.length-1} earlier like it`}
  else{ac.append(d);fold=null}last=a});
 if(!ac.querySelector('.item'))ac.querySelector('.empty')||ac.append(h('p','empty',MODE==='viewer'?'No alerts for you: the builders get the model and flow alerts.':'No alerts.'));
 if(un.length||M.some(m=>m.id>s)){const bar=h('div','row bar top'),b=btn('Mark all read',async()=>{if(un.length)await ipost({read:un.map(a=>a.id)});try{localStorage.setItem(SEEN,String(Math.max(0,...M.map(m=>m.id))))}catch(e){}await inboxCount();show('inbox')});b.prepend(ic('check'));bar.append(b);(box.querySelector(':scope>.pghead')||box).append(bar)}
 g.append(mc,ac)}
// Explore: anyone's own analysis on the governed model (reads only, so Viewer can use it); kept analyses stay in this browser
let MINE=D.app?.analyses||[];if(!MINE.length)try{MINE=JSON.parse(localStorage.getItem('abi-explore')||'[]');if(MINE.length)setTimeout(saveMine,0)}catch(e){}
// the same analysis kept again replaces the older one (the agent shows its chart on each answer)
function keepMine(x){const k=JSON.stringify(x.q);MINE=MINE.filter(y=>JSON.stringify(y.q)!==k);MINE.unshift(x)}
function saveMine(){MINE=MINE.slice(0,100);fetch('/analyses',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify({analyses:MINE})}).catch(()=>{})}
var EXQ=null;  // the analysis on screen: a redraw (ABI's data event) shows it again
function explore(box){const q0=PENDING.explore||EXQ||{};PENDING.explore=null;
 const c=card('New analysis','measures by a dimension, from the model'),ms=multi(D.measures.filter(m=>!/_(prev|change|change_pct)$/.test(m.id)||(q0.measures||[]).includes(m.id)).map(m=>m.id),q0.measures),dm=h('select','f'),ty=h('select','f'),
  fc=h('select','f'),fv=h('select','f'),out=h('div','body'),msg=h('div','msg');ms.classList.remove('ed');fv.multiple=true;fv.hidden=true;
 ms.querySelectorAll('option').forEach(o=>o.text=D.measures.find(m=>m.id===o.value)?.name||o.value);
 dm.append(new Option('Total only',''));fc.append(new Option('No filter',''));D.dims.forEach(d=>{const l=`${colName(d)} · ${d.split('.')[0]}`;dm.append(new Option(l,d));fc.append(new Option(l,d))});dm.value=(q0.dimensions||[])[0]||'';
 [['bar','Bars'],['line','Line'],['table','Table']].forEach(([t,l])=>ty.append(new Option(l,t)));ty.value=q0.chart||'bar';let flt=q0.filters||[];
 fc.onchange=async()=>{fv.innerHTML='';fv.hidden=!fc.value;if(!fc.value)return;
  try{const r=await api('semantic_query',{measures:[D.measures[0]?.id],dimensions:[fc.value],order_by:[{field:fc.value}],limit:500});
   r.rows.forEach(x=>fv.append(new Option(String(x[0]),JSON.stringify(x[0]))));fv.size=Math.min(6,r.rows.length)}catch(e){say(msg,e.message,1)}};
 const query=()=>{const f=fc.value&&fv.selectedOptions.length?[{column:fc.value,op:'in',value:[...fv.selectedOptions].map(o=>JSON.parse(o.value))}]:flt;
  return {measures:[...ms.selectedOptions].map(o=>o.value),dimensions:dm.value?[dm.value]:[],filters:f}};
 async function run(q,type){out.innerHTML='';msg.textContent='';EXQ={...q,chart:type};if(!q.measures.length){say(msg,'Pick at least one measure.',1);return}
  // a period variant (revenue_prev) needs the calendar in the query: ask by month, then keep the latest month's rows,
  // so each member's two periods stand side by side (every figure is still ABI's row)
  const pv=q.measures.some(m=>/_(prev|change|change_pct)$/.test(m)),cal=q.dimensions.some(x=>x.startsWith('calendar.'));let per='';
  try{let d;if(pv&&!cal&&D.dims.includes('calendar.year')&&D.dims.includes('calendar.month')){const x=await api('semantic_query',{...q,dimensions:[...q.dimensions,'calendar.year','calendar.month'],limit:5000}),
     iy=x.columns.indexOf('calendar.year'),im=x.columns.indexOf('calendar.month'),k=r=>r[iy]*100+r[im],top=Math.max(...x.rows.map(k));
     d={...x,columns:x.columns.filter((_,i)=>i!==iy&&i!==im),rows:x.rows.filter(r=>k(r)===top).map(r=>r.filter((_,i)=>i!==iy&&i!==im))};const y=Math.floor(top/100),mo=top%100;per=`${MON[mo-1]} ${y} against ${MON[(mo+10)%12]} ${mo===1?y-1:y}`}
   else d=await api('semantic_query',{...q,...(q.dimensions.length?{order_by:[{field:q.dimensions[0]}]}:{}),limit:500});d.format=d.format||{};d.type=type;d.id='analysis';
   out.append(h('p','file',[per,...(q.filters||[]).filter(f=>!per||!f.column.startsWith('calendar.')).map(f=>`${colName(f.column)} ${f.op==='in'?'is':f.op} ${[].concat(f.value).join(', ')}`)].filter(Boolean).join(' · ')||'All rows, no filter'));
   const b=h('div');out.append(b);requestAnimationFrame(()=>type==='table'?table(b,d):chart(b,d));objClicks(b,q.dimensions[0],d);return d}catch(e){say(msg,e.message,1)}}
 const fr=h('div','formrow'),ff=fld('Filter',fc);ff.append(fv);const mp=pickList(ms);fr.append(fld('Measures',mp),fld('By',dm),ff,fld('Chart',ty));c.append(fr);
 act(c,'Run',async()=>{await run(query(),ty.value)},1);
 act(c,'Keep',async()=>{const q=query();if(!q.measures.length)return 'Pick at least one measure.';
  const nm=q.measures.map(m=>D.measures.find(x=>x.id===m)?.name||m).join(', ')+(q.dimensions[0]?' by '+q.dimensions[0]:'');
  keepMine({name:nm,q,type:ty.value,at:new Date().toISOString().slice(0,16).replace('T',' ')});saveMine();mine();return 'Kept in Saved analyses, for everyone on this app.'});
 act(c,'Save as query',async()=>{const q=query();if(!q.measures.length)return 'Pick at least one measure.';
  const id=('q_'+q.measures.join('_')+(q.dimensions[0]?'_by_'+q.dimensions[0].split('.').pop():'')).toLowerCase().replace(/[^a-z0-9_]/g,'_').slice(0,60);
  await KIND.dataset.save({id,name:id,description:'Saved from Explore',query:q});return `Saved as query ${id} through ABI.`});
 c.append(msg,out);if(!out.children.length)out.append(h('p','empty pad hint','Pick one or more measures, and a dimension to split them by, then Run: the chart shows here.'));const g=h('div','cards'),my=card('Saved analyses','kept with the project for everyone; numbers rerun from the model when opened'),list=h('div');my.append(list);c.style.gridColumn=my.style.gridColumn='1/-1';ms.style.width='100%';g.append(c,my);box.append(g);
 function mine(){list.innerHTML='';if(!MINE.length){list.append(h('p','empty','Nothing kept yet. Run an analysis and press Keep; charts the agent shows you land here too.'));return}
  MINE.forEach((a,i)=>{const r=h('div','srow saved');const o=h('button','flink nm',a.name);o.onclick=()=>{[...ms.options].forEach(x=>x.selected=a.q.measures.includes(x.value));mp.sync();dm.value=a.q.dimensions[0]||'';ty.value=a.type;flt=a.q.filters||[];fc.value='';fv.hidden=true;run(a.q,a.type)};
   const n=h('div','sname');n.append(o,h('div','file',[a.by==='agent'?'Shown by the agent':'',a.at?when2(a.at.replace(' ','T')):''].filter(Boolean).join(' · ')));const ac=h('div','row acts');ac.append(pinBtn({kind:'analysis',ref:{q:a.q,type:a.type},title:a.name}));
   const fg=h('button','btn mi danger','Forget it');fg.prepend(ic('trash'));fg.onclick=()=>{MINE.splice(i,1);saveMine();mine();toast(`Forgot “${a.name}”.`)};more(ac).append(fg);r.append(n,ac);list.append(r)})}
 mine();if(q0.measures)run(query(),ty.value)}
function measures(box){modelBar(box);const top=h('div','row bar top'),q=h('input','f');q.placeholder='Search measures';const list=h('div','cards rows');box.append(top,list);
 const edit=(m,isNew)=>{const c=card(isNew?'New measure':(m.name||m.id),isNew?'':m.id),name=h('input','f'),id=h('input','f'),ex=h('textarea','f'),de=h('textarea','f desc');de.rows=1;
  [name,id,ex,de].forEach(e=>e.classList.add('ed'));name.value=m.name||'';id.value=m.id||'';ex.value=m.expression||'';de.value=m.description||'';
  id.placeholder='a-z, 0-9, _';name.placeholder='display name';ex.placeholder='e.g. SUM(sales.revenue)';de.placeholder='what it means';
  if(isNew){// the name first, its ID made from it; the formula with the model's numeric columns one click away
   name.placeholder='e.g. Net revenue';let auto=true;name.oninput=()=>{if(auto)id.value=slug(name.value,'m')};id.oninput=()=>auto=false;
   const cols=h('div','colpick'),mt=D.model?.tables||{};Object.entries(D.columns||{}).filter(([t])=>!Object.keys(mt).length||(t in mt&&!mt[t]?.hidden)).forEach(([t,cs])=>Object.entries(cs).forEach(([k,ty])=>{if(!/INT|DECIMAL|DOUBLE|FLOAT|NUMERIC|REAL/i.test(String(ty))||/(^|_)(id|key|year|month|day|week|quarter)$/.test(k))return;
    const b=h('button','chip',`${t}.${k}`);b.type='button';b.title='Put it in the formula';b.onclick=()=>{const a=ex.selectionStart??ex.value.length,v=ex.value.trim()?`${t}.${k}`:`SUM(${t}.${k})`;ex.value=ex.value.slice(0,a)+v+ex.value.slice(ex.selectionEnd??a);ex.focus();ex.dispatchEvent(new Event('input'))};cols.append(b)}));
   c.append(h('label','l','Name'),name,h('label','l','ID (made from the name)'),id,h('label','l','Formula (MEL)'),ex,...(cols.children.length?[h('div','file','Numeric columns: click one to use it'),cols]:[]),h('label','l','Description'),de)}
  ex.classList.add('code');if(!isNew&&/_(prev|change|change_pct)$/.test(m.id||''))c.classList.add('derived');  /* a starter measure's period variants sit under it */
  if(!isNew){ex.rows=1;rowcard(c,fld('Name',name,m.id),fld('MEL',ex),fld('Description',de))}
  act(c,isNew?'Create':'Save',async()=>{await api('set_measure',{id:id.value,name:name.value,expression:ex.value,description:de.value||null}).catch(e=>{ex.classList.add('invalid');ex.focus();if(!isNew&&m.expression)setTimeout(()=>{const ms=c.querySelector(':scope>.msg'),b=h('button','flink','Put the saved formula back');b.onclick=()=>{ex.value=m.expression;ex.classList.remove('invalid');ms.textContent='';c.classList.remove('dirty')};ms?.append(' ',b)});throw e});
   Object.assign(m,{id:id.value,name:name.value,expression:ex.value,description:de.value});
   if(isNew){D.measures.push(m);show('measures');setTimeout(()=>highlight(m.name||m.id),300);const mid=m.id;
    doneDialog('Measure created',[`${m.name||m.id} (${mid}) is saved in ABI, which checked the formula first.`,'It is in the list, highlighted. It stays an uncommitted change until you commit.'],
     [['Close'],['Try it in Explore',()=>{PENDING.explore={measures:[mid],dimensions:[],filters:[]};show('explore')}]]);return}return 'Saved through ABI, which checked the formula first.'},1);
  // Test: the measure's total through ABI. ABI runs saved measures only, so an edit not yet saved is not what it tests
  act(c,'Test',async msg=>{say(msg,'Testing…');const t0=Date.now(),r=await api('semantic_query',{measures:[id.value]}),v=r.rows?.[0]?.[0];
   return `${r.measures?.[id.value]||id.value} = ${fmt(v,r.format?.[id.value])} · total over all data, ${((Date.now()-t0)/1000).toFixed(1)} s`+(c.classList.contains('dirty')?' · this is the saved version: Save to test your change':'')});
  if(!isNew){act(c,'Edit',()=>{ex.focus();ex.setSelectionRange?.(ex.value.length,ex.value.length);c.classList.add('editing')})}
  if(!isNew)act(c,'Delete',async()=>{await api('delete_measure',{id:id.value});D.measures=D.measures.filter(x=>x!==m);show('measures')});return c};
 // a starter measure's period variants (…_prev, …_change, …_change_pct) fold under it; a search shows them all
 const VAR=/_(prev|change|change_pct)$/,base=id=>id.replace(VAR,'');
 const fill=()=>{list.innerHTML='';list.append(rowhead('Measure','Formula (MEL)','Description',''));const all=D.measures.filter(nmMeasure),t=q.value.toLowerCase(),hit=m=>!t||JSON.stringify(m).toLowerCase().includes(t);
  const ids=new Set(all.map(m=>m.id)),kids=m=>all.filter(x=>VAR.test(x.id)&&base(x.id)===m.id);
  all.filter(m=>!VAR.test(m.id)||!ids.has(base(m.id))).forEach(m=>{const ks=kids(m),shown=ks.filter(hit);if(!hit(m)&&!shown.length)return;const c=edit(m);list.append(c);
   if(!ks.length)return;const rows=ks.map(x=>{const r=edit(x);r.hidden=!t;list.append(r);return r}),tg=h('button','flink vtog',t?`${ks.length} period variants`:`+ ${ks.length} period variants`);tg.type='button';tg.setAttribute('aria-expanded',String(!!t));
   tg.onclick=()=>{const o=rows[0].hidden;rows.forEach(r=>r.hidden=!o);tg.textContent=(o?'− ':'+ ')+`${ks.length} period variants`;tg.setAttribute('aria-expanded',String(o))};c.querySelector('.fld')?.append(tg)})};
 top.append(q,btn('New measure',()=>{const c=edit({},true);c.classList.add('editor','inrows');list.firstChild.after(c)},1));q.oninput=fill;fill()}
function glossary(box){const list=h('div','cards rows');box.append(list);
 const edit=(t,isNew)=>{const c=card(isNew?'New term':(t.term||t.id)),term=h('input','f'),def=h('textarea','f'),ms=h('input','f');[term,def,ms].forEach(e=>e.classList.add('ed'));term.value=t.term||'';def.value=t.definition||'';ms.value=(t.measures||[]).join(', ');
  term.placeholder='business term';def.placeholder='what it means';ms.placeholder=isNew?'e.g. revenue, units':'no measure: a word for a column';
  if(t.term===MASK){term.value=colName(t.id||'term');def.value='';def.placeholder='Not shown with your access: it may quote data.';c.classList.add('masked-row')}
  // 1.2.6 M6: an object type's, property's or link's description, listed as a term; it changes with the ontology, not here
  if(/^(object|property|link):/.test(t.id||'')){[term,def,ms].forEach(e=>e.readOnly=true);ms.value='ontology · '+t.id;rowcard(c,fld('Term',term),fld('Definition',def),fld('Defined by',ms));return c}
  if(isNew)c.append(h('label','l','Term'),term,h('label','l','Definition'),def,h('label','l','Implemented by (measure IDs)'),ms);
  else{def.rows=1;rowcard(c,fld('Term',term),fld('Definition',def),fld('Implemented by',ms))}
  act(c,isNew?'Create':'Save',async()=>{await api('set_glossary_term',{term:term.value,definition:def.value,measures:ms.value.split(',').map(x=>x.trim()).filter(Boolean),columns:(t.columns||[]).map(x=>x.table?x.table+'.'+x.column:x),objects:t.objects||[],links:t.links||[]});
   Object.assign(t,{term:term.value,definition:def.value});if(isNew)D.glossary.push(t);return 'Saved through ABI.'},1);
  if(!isNew)act(c,'Delete',async()=>{await api('delete_glossary_term',{id:t.id});D.glossary=D.glossary.filter(x=>x!==t);show('glossary')});return c};
 const bar=h('div','row bar top');bar.append(btn('New term',()=>list.firstChild.after(edit({},true)),1));box.insertBefore(bar,list);
 list.append(rowhead('Term','Definition','Implemented by',''));D.glossary.forEach(t=>list.append(edit(t)));if(!D.glossary.length)list.append(h('p','empty pad','No terms yet. New term adds the first.'))}

// ---------- Data ----------
async function upload(table,file,sheet){const id=[...crypto.getRandomValues(new Uint8Array(16))].map(b=>b.toString(16).padStart(2,'0')).join(''),N=24<<20;let r;
 for(let o=0;;o+=N){const last=o+N>=file.size,data=await new Promise(res=>{const fr=new FileReader();fr.onload=()=>res(fr.result.split(',')[1]||'');fr.readAsDataURL(file.slice(o,o+N))});  // 24 MB pieces: under the 64 MB request limit
  r=await fetch('/upload',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify({table,name:file.name,data,id,last,sheet})}).then(x=>x.json());
  if(r.error)throw new Error(r.error);if(last)return r.result}}
// 1.2 M3.2: a big load answers at once with its run (status queued) and loads on the server: queued, then rows so far,
// then done or failed with ABI's error, in the card's message (each check is a short call; the page follows when it is in)
async function loaded(r,m,text){if(!r||r.status!=='queued')return text;
 for(let x=r;;){say(m,x.status==='queued'?`${x.table}: queued, it starts when the loads ahead of it are done…`:`${x.table}: loading, about ${fmt(x.rows||0)} rows so far…`);if(m)m.className='msg prog';
  await wait(2000);x=(await api('load_runs',{run:r.run})).runs[0];if(x.status==='done')return `${text} ${fmt(x.rows)} rows.`;if(x.status==='failed')throw new Error(x.error)}}
function sources(box){const q=D.quality||{},ix=c=>(q.columns||[]).indexOf(c),qr=Object.fromEntries((q.rows||[]).map(r=>[r[ix('table')],r]));
 const g=h('div','cards'),bar=h('div','row bar top');box.append(bar,g);
 const add=()=>{const c=card('Add a source','a file you upload (CSV, TSV, Excel, JSON, Parquet, Avro), or a file or folder on S3 (also Delta and Iceberg tables)');c.classList.add('editor');
  const t=h('input','f ed');t.placeholder='table id, e.g. bronze_orders';const loc=h('input','f ed');loc.placeholder='s3://bucket/folder/*.csv  or  https://example.com/data.csv';
  const ep=h('input','f ed');ep.placeholder='blank for AWS; or host[:port] for MinIO, R2, Wasabi…';const file=h('input');file.type='file';file.accept='.csv,.tsv,.xlsx,.json,.jsonl,.ndjson,.parquet,.avro';file.className='f ed';
  const sh=h('input','f ed');sh.placeholder='blank: the first sheet';const fm=h('select','f ed');
  ['','csv','xlsx','json','parquet','avro','delta','iceberg'].forEach(x=>fm.append(new Option(x||'from the file name',x)));
  const rg=h('input','f ed');rg.value='us-east-1';const ki=h('input','f ed');ki.placeholder='access key ID (blank for a public bucket)';ki.autocomplete='off';
  const ks=h('input','f ed');ks.type='password';ks.placeholder='secret access key';ks.autocomplete='new-password';
  const tl=h('label','chk'),tls=h('input');tls.type='checkbox';tls.checked=true;tl.append(tls,h('span','','HTTPS (untick for a plain-HTTP endpoint such as a local MinIO)'));
  // two ways in, one shown at a time: a file from this computer, or a location (S3 or https)
  const fg=h('div','grp'),lg=h('div','grp'),tabs=h('div','row seg'),tb=(l,on)=>{const b=h('button','chip'+(on?' on':''),l);b.type='button';return b},t1=tb('Upload a file',1),t2=tb('S3 or a URL');
  const pick=up=>{fg.hidden=!up;lg.hidden=up;t1.classList.toggle('on',up);t2.classList.toggle('on',!up);if(up)loc.value=''};t1.onclick=()=>pick(true);t2.onclick=()=>pick(false);tabs.append(t1,t2);
  const shl=h('label','l','Excel sheet (optional)');fg.append(h('label','l','File (CSV, TSV, Excel, JSON, Parquet, Avro)'),file,shl,sh);shl.hidden=sh.hidden=true;
  lg.append(h('label','l','Location: an S3 file or folder (a glob, or a Delta or Iceberg table), or an https:// file'),loc,
   h('label','l','Format'),fm,h('label','l','Region'),rg,h('label','l','S3 endpoint'),ep,tl,
   h('label','l','Keys (workspace owners; stored encrypted for this bucket only, never shown again)'),ki,ks);lg.hidden=true;
  const warn=h('p','note warn');warn.hidden=true;const chk=()=>{const x=t.value.trim();warn.hidden=!D.sources.includes(x);warn.replaceChildren(ic('alert'),h('span','',`${x} already exists: adding this replaces its data with the new file's rows.`))};
  let auto=true;t.addEventListener('input',()=>{auto=false;chk()});
  file.onchange=()=>{shl.hidden=sh.hidden=!/\.xlsx$/i.test(file.files[0]?.name||'');if((auto||!t.value)&&file.files[0]){t.value='bronze_'+file.files[0].name.replace(/\.[^.]+$/,'').toLowerCase().replace(/[^a-z0-9]+/g,'_').replace(/^_|_$/g,'');chk()}};
  c.append(tabs,fg,lg,h('label','l','Table name'),t,h('div','file','made from the file name; lowercase letters, digits and underscores'),warn);
  act(c,'Add source',async m=>{need(t,t.value.trim(),'Name the table first, e.g. bronze_orders.');need(t,/^[a-z][a-z0-9_]*$/.test(t.value),'A table name is lowercase letters, digits and underscores, and starts with a letter.');const sheet=sh.value.trim();
   if(file.files[0])need(file,/\.(csv|tsv|xlsx|json|jsonl|ndjson|parquet|avro)$/i.test(file.files[0].name),`${file.files[0].name} is not a file ABI reads: choose a CSV, TSV, Excel, JSON, Parquet or Avro file.`);
   if(D.sources.includes(t.value.trim())&&!await ask({title:`Replace ${t.value.trim()}?`,message:`${t.value.trim()} already exists. Adding this source overwrites its data: every table, measure and report built on it then reads the new rows.`,yes:'Replace it'}))return 'Not added: nothing changed.';
   if(file.files[0]){const fl=file.files[0];m.className='msg prog';m.textContent=(`Uploading ${fl.name} (${fl.size>1e6?(fl.size/1e6).toFixed(1)+' MB':Math.max(1,Math.round(fl.size/1e3))+' KB'}), then ABI loads it…`);const x=await loaded(await upload(t.value,fl,sheet),m,`Uploaded and loaded into ${t.value}.`);return x+' Next: stage or model it under Tables, or ask the agent to build a report from it.'}
   if(!loc.value)throw new Error('Choose a file, or give a location like s3://bucket/folder/*.csv.');
   const s3=loc.value.startsWith('s3://')?{region:rg.value.trim()||'us-east-1',...(ep.value?{endpoint:ep.value.trim(),url_style:'path'}:{}),...(tls.checked?{}:{use_ssl:false})}:null;
   if(s3&&(ki.value||ks.value)){const r=await fetch('/s3key',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify({table:t.value,location:loc.value,...s3,key_id:ki.value,secret:ks.value})}).then(x=>x.json());
    ks.value='';if(r.error)throw new Error(r.error);Object.assign(s3,r.result)}
   return loaded(await api('add_data',{table:t.value,location:loc.value,...(fm.value?{format:fm.value}:{}),...(s3?{s3}:{}),...(sheet?{xlsx:{sheet}}:{})}),m,`Loaded ${t.value}.`)},1);return c};
 // a Postgres or MySQL table: the login goes to the app's admin endpoint only, which stores it as two ABI secrets bound to
 // this host; the source names them (${secret:…}), so neither the page, the agent nor Git ever holds it
 const addDb=()=>{const c=card('Add a database table','a Postgres or MySQL table; it reloads daily, appending past an increasing column if you name one');c.classList.add('editor');
  const t=h('input','f ed'),kd=h('select','f ed'),ho=h('input','f ed'),po=h('input','f ed'),db=h('input','f ed'),sc=h('input','f ed'),tb=h('input','f ed'),us=h('input','f ed'),pw=h('input','f ed'),inc=h('input','f ed');
  [['postgres','Postgres'],['mysql','MySQL']].forEach(([v,l])=>kd.append(new Option(l,v)));kd.onchange=()=>po.placeholder=kd.value==='mysql'?'3306':'5432';
  t.placeholder='table id, e.g. bronze_customers';ho.placeholder='host, e.g. db.example.com';po.placeholder='5432';db.placeholder='database';sc.placeholder='schema (blank: public, or the database for MySQL)';
  tb.placeholder='table';us.placeholder='user';us.autocomplete='off';pw.type='password';pw.placeholder='password';pw.autocomplete='new-password';inc.placeholder='e.g. id or updated_at (blank: the auto-increment key, if any)';
  c.append(h('label','l','Table id'),t,h('label','l','Database'),kd,h('label','l','Host'),ho,h('label','l','Port'),po,h('label','l','Database name'),db,h('label','l','Schema'),sc,h('label','l','Table'),tb,
   h('label','l','Login (workspace owners; stored encrypted for this host only, never shown again)'),us,pw,h('label','l','Increasing column (optional)'),inc);
  act(c,'Add table',async m=>{if(!t.value||!ho.value.trim()||!db.value.trim()||!tb.value.trim())throw new Error('A table id, the host, the database name and the table.');
   const r=await fetch('/admin/dbkey',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify({table:t.value,host:ho.value.trim(),user:us.value,password:pw.value})}).then(x=>x.json());
   pw.value='';if(r.error)throw new Error(r.error);const col=inc.value.trim();
   return loaded(await api('add_data',{table:t.value,location:`${kd.value}://${ho.value.trim()}:${po.value.trim()||po.placeholder}/${db.value.trim()}`,
    database:{table:(sc.value.trim()?sc.value.trim()+'.':'')+tb.value.trim(),...r.result,...(col?{incremental:col}:{})}}),m,`Loaded ${t.value}.`)},1);return c};
 bar.append(btn('Add source',()=>g.prepend(add()),1),...((window.ME||{admin:true}).admin?[btn('Add a database table',()=>g.prepend(addDb()))]:[]));  // its login is a workspace owner's (/admin/dbkey)
 if(!D.sources.length)box.append(h('p','empty','No sources yet: upload a file, add a folder on S3 or a database table.'));
 D.sources.forEach(id=>{const sp=D.source_specs?.[id]||{},r=qr[id],c=card(id),k=h('div','kv'),p=r?(r[ix('problems')]||[]):[];
  const hid=q.masked&&!p.length&&r;c.querySelector('h3').title=`sources/${id}.json`;c.querySelector('h3').append(h('span','state'+(p.length?' bad':hid?'':r?' on':''),p.length?'Needs a look':hid?'Loaded':r?'Loaded':'Not loaded'));
  const loc=String(sp.location||'—'),up=/^uploads\/[0-9a-f]{16,}\//.test(loc),rf={on_change:'when the file changes',daily:'daily',hourly:'hourly',manual:'by hand'},rv=sp.refresh||(r?r[ix('refresh')]:'');
  const mxr=Math.max(1,...(q.rows||[]).map(x=>+x[ix('rows')]||0)),age=t=>{const m=(Date.now()-Date.parse(t))/6e4;return !t||isNaN(m)?'':m<2?'just now':m<90?Math.round(m)+' min ago':m<36*60?Math.round(m/60)+' h ago':Math.round(m/1440)+' days ago'};
  [['source',up?'Uploaded file · '+loc.split('/').pop():loc],['format',sp.format||'from the file'],['refresh',rf[rv]||rv||'—'],...(hid&&r[ix('rows')]===null?[]:[['rows',r&&r[ix('rows')]!==null?fmt(r[ix('rows')]):r?'not shown with your access':'—']]),['last load',r?`${cell(r[ix('last_load')])}${age(r[ix('last_load')])?' · '+age(r[ix('last_load')]):''}`:'—'],...(hid?[[r[ix('rows')]===null?'rows, health':'health','not shown with your access']]:[])]
   .forEach(([a,b])=>{const v=h('span','',String(b));if(a==='source')v.title=loc;if(a==='rows'&&r&&r[ix('rows')]!=null){const bar=h('i','rbar');bar.style.setProperty('--p',Math.max(2,Math.round(+r[ix('rows')]/mxr*100))+'%');v.append(bar)}k.append(h('b','',a),v)});c.append(k);
  if(p.length){const w=h('div','warnnote');w.append(ic('alert'),h('span','',plainGap(plur(p.map(x=>typeof x==='string'?x:x.message||JSON.stringify(x)).join('; ')))));if(MODE==='developer'){const go=h('button','flink','See it in Monitor');go.onclick=()=>show('monitor');w.append(go)}c.append(w)}
  act(c,'Reload',async m=>loaded(await api('refresh',{table:id}),m,'Reloaded: a new snapshot.'));act(c,'Delete',async()=>{await api('delete_source',{source:id});return 'Deleted.'});g.append(c)})}
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
 act(c,'Create',async m=>{await loaded(await api('set_steps',{table:name(),steps:steps()}),m,'');await api('set_table',{table:name(),hidden:true});return `Created ${name()}: staged and hidden from the model. It appears in this list in a few seconds.`},1);
 c.append(h('label','l','Or ask the agent'),how);act(c,'Ask the agent instead',async()=>{openChat(true);inp.value=`Stage the source ${src.value} as a hidden silver table ${name()}: ${how.value.trim()||'clean it for the model'}`;sendChat();return 'Asked: follow it in the chat.'});
 c.append(out);(box.querySelector('.pghead')||box).after(c);look().catch(()=>{})}
function stagedPage(box){const sv=(D.layers||[]).filter(t=>t.layer==='silver').sort((a,b)=>a.table.localeCompare(b.table));
 const bar=h('div','row bar top');bar.append(btn('New staged table',()=>newStaged(box),1));box.append(bar);
 const g=h('div','cards');box.append(g);if(!sv.length){box.append(emptyBox('staged','Nothing staged yet',MODE==='viewer'?vEmpty('The sources go straight to the final tables: nothing is cleaned in between.'):'A staged table cleans a source (types, duplicates, joins) before it reaches the final tables, out of the model.',MODE==='developer'?['New staged table',()=>newStaged(box)]:null));return}
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
 act(c,'Save steps',async m=>loaded(await api('set_steps',{table:t,steps:parse()}),m,'Saved and loaded through the steps.'),1);c.append(out);box.append(c)}
function tablesPage(box){
 const g=h('div','cards'),bar=h('div','row bar top');box.append(bar,g);
 const stepsEd=(t,steps,isNew)=>{const c=card(isNew?'New table (transform)':`Steps of ${t}`,isNew?'starts from a loaded source or table':`tables/${t}.json`);c.classList.add('editor');
  const id=h('input','f ed');id.value=isNew?'':t;id.readOnly=!isNew;id.placeholder='table id, e.g. silver_orders';
  const ta=h('textarea','f ed code');ta.value=JSON.stringify(steps||[{type:'source',source:D.sources[0]||''}],null,2);ta.rows=Math.min(24,ta.value.split('\n').length+1);
  const out=h('div');if(isNew)c.append(h('label','l','Table'),id);c.append(h('label','l','Steps (JSON)'),ta);
  const parse=()=>{try{return JSON.parse(ta.value)}catch(e){throw new Error('Not valid JSON: '+e.message)}};
  act(c,'Preview',async()=>{rows(out,await api('preview_steps',{steps:parse()}))});
  act(c,'Save steps',async m=>loaded(await api('set_steps',{table:id.value,steps:parse()}),m,'Saved and loaded through the steps.'),1);c.append(out);return c};
 bar.append(btn('New table',()=>g.prepend(stepsEd('',null,true)),1));
 const T=D.model?.tables||{};if(!Object.keys(T).length)box.append(h('p','empty','No tables yet: add a source first.'));
 Object.entries(T).forEach(([t,v])=>{const c=card(t);c.querySelector('h3').append(pinBtn({kind:'table',ref:t,title:t}));const kind=h('select','f ed');['fact','dimension'].forEach(x=>kind.append(new Option(x,x)));kind.value=v.kind||'fact';
  const key=h('input','f ed');key.value=(v.key||[]).join(', ');key.placeholder='key columns';const de=h('input','f ed');de.value=v.description||'';
  const hid=h('input','ed');hid.type='checkbox';hid.checked=!!v.hidden;const hl=h('label','l');hl.append(hid,document.createTextNode('  hidden from the model (staging)'));
  const cols=Object.entries(D.columns?.[t]||{});const cl=h('div'),chip=([n,ty])=>{const x=h('span','chip');x.append(n,h('i','',' '+String(ty).toLowerCase()));return x};cols.slice(0,40).forEach(x=>cl.append(chip(x)));
  // kind and key side by side, the columns folded under their count; Save turns primary once something changed
  const two=h('div','two');two.append(fld('Kind',kind),fld('Key',key));cl.className='stepchips';const cd=h('div','cols');{const top=h('div','stepchips');cols.slice(0,6).forEach(x=>top.append(chip(x)));if(cols.length>6){const mo=h('button','flink',`+ ${cols.length-6} more`);mo.onclick=()=>{top.replaceWith(cl)};top.append(mo)}cd.append(h('label','l',`${cols.length} columns`),top)}
  c.classList.add('tcard');c.oninput=c.onchange=()=>c.classList.add('dirty');c.append(two,fld('Description',de),hl,cd);
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
  if(MODE==='developer')act(c,'Delete',async()=>{await api('delete_model',{id:m.id});D.models=D.models.filter(x=>x.id!==m.id);if(NM===m.id)pickNM('');sidebar();show('model')})});if(!(D.models||[]).length&&!MOBILE){pickNM('');box.append(h('h4','sub','The whole model'));modelCanvas(box,1)}}
 // no named models yet: the whole model's diagram right here, not one row on a blank page

function modelBar(box){if(!(D.models||[]).length)return;const bar=h('div','row bar lead seg');
 [['','All tables'],...(D.models||[]).map(m=>[m.id,m.name])].forEach(([id,name])=>{const b=h('button','chip'+(NM===id?' on':''),name);b.onclick=()=>{pickNM(id);show(CUR)};bar.append(b)});box.append(bar)}
function openModel(box,id){pickNM(id);const bar=h('div','row bar lead');bar.append(btn('← Data models',()=>show('model')));box.append(bar);modelCanvas(box)}
// a canvas with zoom (−, +, Fit, Ctrl/⌘ + wheel) and Auto arrange: a scroll viewport > sizer > stage (scaled)
function canvasKit(box,onArrange){const tools=h('div','row cvtools'),wrap=h('div','canvas'),sizer=h('div','sizer'),stage=h('div','stage'),svg=document.createElementNS(NS,'svg');
 svg.classList.add('links');stage.append(svg);sizer.append(stage);wrap.append(sizer);const kit={z:1,w:0,ht:0,wrap,stage,svg,draw:()=>{}};
 const pct=h('span','pct','100%'),size=()=>{sizer.style.width=kit.w*kit.z+'px';sizer.style.height=kit.ht*kit.z+'px';stage.style.width=kit.w+'px';stage.style.height=kit.ht+'px';
  stage.style.transform=`scale(${kit.z})`;wrap.style.height=Math.max(140,Math.min(kit.ht*kit.z+28,innerHeight*.78))+'px';pct.textContent=Math.round(kit.z*100)+'%'};
 kit.setZ=z=>{kit.z=Math.min(2,Math.max(.25,Math.round(z*100)/100));size();kit.draw()};kit.extent=(w,ht)=>{kit.w=w;kit.ht=ht;size();const vh=innerHeight*.78-28;if(!kit.fit&&w>0&&(MOBILE||wrap.clientWidth&&(w>wrap.clientWidth-24||ht>vh))){kit.fit=1;requestAnimationFrame(()=>kit.setZ(Math.min(1,(wrap.clientWidth-24)/Math.max(1,kit.w),MOBILE?1:vh/Math.max(1,kit.ht))))}};  // a phone starts fitted to its width; a canvas bigger than the page, to the page
 const b=(t,title,fn)=>{const x=h('button','btn',t);x.title=title;x.onclick=fn;return x};
 tools.append(b('−','Zoom out',()=>kit.setZ(kit.z-.1)),pct,b('+','Zoom in',()=>kit.setZ(kit.z+.1)),b('Fit','Fit to the window',()=>kit.setZ(Math.min(1,(wrap.clientWidth-24)/Math.max(1,kit.w),(innerHeight*.78)/Math.max(1,kit.ht)))),
  (()=>{const x=b('Auto arrange','Lay the boxes out again',onArrange);x.classList.add('w');return x})());
 wrap.addEventListener('wheel',e=>{if(!(e.ctrlKey||e.metaKey))return;e.preventDefault();kit.setZ(kit.z*(e.deltaY<0?1.1:.9))},{passive:false});
 // a point of the stage, in stage units, from a screen point
 kit.local=(x,y)=>{const r=stage.getBoundingClientRect();return[(x-r.left)/kit.z,(y-r.top)/kit.z]};
 box.append(tools,wrap);return kit}
function modelCanvas(box,fit){const T=D.model?.tables||{},R=(D.model?.relationships||[]).filter(r=>nmTable(r.from.table)&&nmTable(r.to.table)),names=Object.keys(T).filter(nmTable);
 if(!names.length){box.append(h('p','empty','No tables yet: add a source first.'));return}
 // a phone reads the model as a list: each table, its kind and columns, and what it joins to
 if(MOBILE){const g=h('div','cards mlist2');names.sort((a,b)=>(T[b].kind==='fact')-(T[a].kind==='fact')).forEach(t=>{const c=card(t);c.querySelector('h3').append(h('span','state'+(T[t].kind==='fact'?' on':''),T[t].kind==='fact'?'Fact':'Dimension'));
   const js=R.filter(r=>r.from.table===t).map(r=>`${r.from.columns.join(', ')} → ${r.to.table}`);if(js.length)c.append(h('p','mtext','Joins '+js.join(' · ')));
   const cl=h('div','stepchips');Object.entries(D.columns?.[t]||{}).forEach(([n,ty])=>{const x=h('span','chip');x.append(n,h('i','',' '+String(ty).toLowerCase().replace(/\(.*/,'')));cl.append(x)});c.append(cl);g.append(c)});box.append(g);return}
 const help=h('p','file',MODE==='viewer'?'Facts are filled black, dimensions outlined. Point at a join to see its columns.'
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
 requestAnimationFrame(draw);new ResizeObserver(()=>draw()).observe(kit.wrap);if(fit)requestAnimationFrame(()=>requestAnimationFrame(()=>kit.setZ(Math.min(1,(kit.wrap.clientWidth-24)/Math.max(1,kit.w),(innerHeight*.78)/Math.max(1,kit.ht)))))}
// ---------- Objects (1.2.6 M7): the things the business works with (a café, a customer), the model's object types.
// Every screen reads as the person through ABI's list_objects, get_object and object_history (their rows and columns;
// a hidden object reads like a missing one); a form runs run_object_action through ABI's confirmation, never by itself,
// and only in Developer (Viewer and the published copy read). Business names only: a type's, a property's, a
// connection's and an action's display names, never an id ----------
const OT=()=>D.ontology?.objects||[],OA=()=>D.ontology?.actions||[],otype=id=>OT().find(t=>t.id===id),cap=s=>{s=String(s??'');return s.charAt(0).toUpperCase()+s.slice(1)};
// a property's name on a page: the type's own name for its title ("Café"), ID for its one key column, the name the type gives it, else the column's words
const pname=(p,T)=>p.name||(T&&p.column===T.title?T.name:T&&T.key.length===1&&p.column===T.key[0]?'ID':colName(p.column));
// 1.2.7 M0 (9b): a link as a sentence about its two types ("each order has one café", "customer visited cafés"),
// the same words as render.says: never a column, never a cardinality. A the type it is read from, B the one it leads to
const sayLink=(A,B,name,card)=>{const one=/one$/.test(card||''),lo=x=>x.name.toLowerCase(),nm=String(name||'').toLowerCase();
 if(/ \(inverse\)$/.test(nm))return `${A.plural.toLowerCase()} have ${B.plural.toLowerCase()}`;  // a link read backwards with no inverse name of its own
 return [lo(B),B.plural.toLowerCase()].includes(nm)?(one?`each ${lo(A)} has one ${lo(B)}`:`${A.plural.toLowerCase()} have ${B.plural.toLowerCase()}`):`${lo(A)} ${name} ${one?lo(B):B.plural.toLowerCase()}`};
const says=l=>sayLink(otype(l.from),otype(l.to),l.name,l.cardinality);
const low=T=>T.name.toLowerCase(),RANK={viewer:1,editor:2,owner:3},myRank=()=>RANK[(window.ME||{role:'owner'}).role]||0,COPY=()=>MODE==='viewer'&&/_pub$/.test(D.project);
// a property's value as a page shows it: an identifier as it is, a flag as Yes or No, a probability as a percentage, a day as 15 Mar 2022
const dayOf=v=>{const d=new Date(v+'T00:00');return isNaN(d)?v:d.toLocaleDateString(undefined,{day:'numeric',month:'short',year:'numeric'})};
const pval=(T,p,v)=>v===null||v===undefined?null:v===MASK?'hidden for your access':typeof v==='boolean'?(v?'Yes':'No'):typeof v!=='number'?(/^\d{4}-\d\d-\d\d$/.test(v)?dayOf(v):cell(v)):
 p.meaning==='identifier'||T.key.includes(p.column)||p.column===T.title||PLAIN.test(p.column)?String(v):p.meaning==='probability'?fmt(v,{style:'percent',decimals:1}):v;
// obj:<type> is that type's list, obj:<type>:<key> one object's page, the key as JSON. The address bar holds the key
// percent-encoded once (hashOf) and it is read back once (boot, hashchange)
const okey=(t,k)=>'obj:'+t+':'+JSON.stringify(k),hashOf=k=>{const i=k.startsWith('obj:')?k.indexOf(':',4):-1;return '#'+(i<0?k:k.slice(0,i+1)+encodeURIComponent(k.slice(i+1)))};
// the page the address bar names: decoded once; a hand-typed hash that isn't valid percent-encoding is read as it is
function hashKey(){const x=location.hash.slice(1);try{return decodeURIComponent(x)}catch(e){return x}}
// opening an object keeps the page it was opened from in the browser's history: Back returns there
function openObj(t,k){try{history.pushState(null,'',location.href)}catch(e){}show(okey(t,k))}
function objReg(k){const i=k.indexOf(':',4),T=otype(i<0?k.slice(4):k.slice(4,i));if(!T)return;if(i<0){PAGES[k]=[T.plural,b=>objectsPage(b,T)];return}
 let key;try{key=JSON.parse(k.slice(i+1))}catch(e){return}if(Array.isArray(key)&&key.length)PAGES[k]=[T.name,b=>objectPage(b,T,key)]}
function objBar(box,on){const bar=h('div','row bar lead seg');[...(PAGES.objects?[['objects','Overview']]:[]),...OT().map(t=>['obj:'+t.id,t.plural])].forEach(([k,n])=>{const b=h('button','chip'+(k===on?' on':''),n);b.onclick=()=>show(k);bar.append(b)});box.append(bar)}
// a type's objects as a table (the report's table): the title, the named properties and the type's measures; a title
// search, a filter per category and pages. `fixed`: list_objects filters that stay (an object page's connection: the
// objects linked to that one). A row opens its object's page
function objTable(box,T,fixed,n){n=n||50;const bar=h('div','row obar'),q=h('input','f'),out=h('div','pick olist'),foot=h('div','row pager'),head=h('span','row pager'),st={offset:0,flt:{},n:0};let tm;
 q.type='search';q.placeholder=`Search ${T.plural.toLowerCase()}`;q.setAttribute('aria-label',q.placeholder);if(!fixed&&PENDING.osearch){q.value=PENDING.osearch;PENDING.osearch=null}bar.append(q);
 const own=T.properties.filter(p=>!p.column.includes('.')&&p.column!==T.title),named=own.filter(p=>(p.name||p.meaning)&&!T.key.includes(p.column)),P=c=>T.properties.find(p=>p.column===c)||{column:c},
  fkc=(D.ontology.links||[]).filter(l=>l.from===T.id&&l.via.length===1&&otype(l.to)&&!(fixed||[]).some(f=>f.link===l.id)).map(l=>(D.model?.relationships||[]).find(r=>r.id===l.via[0]&&r.from.table===T.table&&r.from.columns.length===1)?.from.columns[0]).filter(c=>c&&!T.key.includes(c)),
  cols=[...new Set([T.title,...fkc,...(named.length?named:own.filter(p=>!T.key.includes(p.column))).map(p=>p.column)])].slice(0,MOBILE?2:6),ms=fixed?[]:(T.measures||[]).slice(0,MOBILE?1:3),m0=(T.measures||[])[0]||D.measures[0]?.id;
 // a filter per category: its members as the model counts them, for this person
 if(!fixed&&m0)own.filter(p=>p.meaning==='category'||p.meaning==='flag').slice(0,3).forEach(p=>{const s=sel([['',`${pname(p,T)}: all`]]);s.setAttribute('aria-label',pname(p,T));s.onchange=()=>{st.flt[p.column]=s.value;st.offset=0;load()};bar.append(s);
  api('semantic_query',{measures:[m0],dimensions:[`${T.table}.${p.column}`],limit:200}).then(r=>r.rows.forEach(x=>{if(x[0]!==null)s.append(new Option(String(x[0]),JSON.stringify(x[0])))})).catch(()=>s.remove())});
 q.oninput=()=>{clearTimeout(tm);tm=setTimeout(()=>{st.offset=0;load()},300)};
 out.onclick=e=>{const i=e.target.closest?.('tr[data-r]')?.dataset.r;if(i!==undefined&&st.keys?.[i])openObj(T.id,st.keys[i])};out.onkeydown=e=>{if(e.key==='Enter')out.onclick(e)};
 async function load(){const my=++st.n,flt=Object.entries(st.flt).filter(([,v])=>v),some=q.value.trim()||flt.length;let r;
  try{r=await api('list_objects',{type:T.id,filters:[...(fixed||[]),...flt.map(([c,v])=>({property:c,op:'=',value:JSON.parse(v)}))],...(q.value.trim()?{search:q.value.trim()}:{}),limit:n,offset:st.offset})}
  catch(e){if(my===st.n){out.replaceChildren(h('p','empty',e.message));foot.textContent=head.textContent='';bar.hidden=!!fixed}return}
  if(my!==st.n)return;const ix=c=>r.columns.indexOf(c);q.hidden=ix(T.title)<0;  // a title hidden from this person can't be searched
  bar.hidden=!!fixed&&!(r.more||st.offset||some);foot.textContent=head.textContent='';
  // nothing to list: say what to do next, and never whether rows exist that this person can't see
  if(!r.rows.length){out.replaceChildren(h('p','empty',some?`No ${low(T)} matches. Clear the search${flt.length?' or the filter':''} to see them all.`:(fixed?'None to show here':`No ${T.plural.toLowerCase()} to show`)+(D.rls?.restricted?' with your access. A project owner sets what you see in Security.':'.')));return}
  const shown=cols.filter(c=>ix(c)>=0);st.keys=r.rows.map(x=>r.key.map(k=>x[ix(k)]));
  // a figure keeps its decimals on every row (fmt's `fixed`): money, and each measure in its own format
  const d={columns:[...shown],measures:Object.fromEntries(shown.map(c=>[c,pname(P(c),T)])),format:Object.fromEntries(shown.filter(c=>P(c).meaning==='money').map(c=>[c,{decimals:2,fixed:true}])),rows:r.rows.map(x=>shown.map(c=>pval(T,P(c),x[ix(c)])))};
  // the type's measures for this page's objects: one read by the key, with each measure's own format (left out when
  // ABI refuses it). ponytail: a type with a several-column key lists no measures; its object pages show them
  if(ms.length&&T.key.length===1){const kc=`${T.table}.${T.key[0]}`;try{const m=await api('visual_data',{query:{measures:ms,dimensions:[kc],filters:[{column:kc,op:'in',value:st.keys.map(k=>k[0])}]},type:'table'}),by=new Map(m.rows.map(x=>[String(x[0]),x.slice(1)]));
    if(my!==st.n)return;ms.forEach(id=>d.format[id]={...m.format?.[id],fixed:!m.format?.[id]?.compact});d.columns.push(...ms);Object.assign(d.measures,m.measures);d.rows.forEach((x,i)=>x.push(...(by.get(String(st.keys[i][0]))||ms.map(()=>null))))}catch(e){}}
  // 1.2.7 M0 (9b): the figures right after the name ...
  if(ms.length&&d.columns.length>shown.length){const k=shown.length,mv=x=>[x[0],...x.slice(k),...x.slice(1,k)];d.columns=mv(d.columns);d.rows=d.rows.map(mv)}
  // ... and a column that points at another object shows that object by its name, which opens it: never its raw key
  const R=D.model?.relationships||[],to={},tt={};shown.forEach(c=>{const l=(D.ontology.links||[]).find(l=>l.from===T.id&&l.via.length===1&&otype(l.to)&&(r=>r&&r.from.table===T.table&&r.from.columns.length===1&&r.from.columns[0]===c)(R.find(r=>r.id===l.via[0])));if(l)to[c]=otype(l.to)});
  const raw={};Object.keys(to).forEach(c=>raw[c]=r.rows.map(x=>x[ix(c)]));  // the keys as read: the table shows them formatted
  await Promise.all(Object.entries(to).map(async([c,Z])=>{const vs=[...new Set(raw[c].filter(v=>v!==null&&v!==MASK))];d.measures[c]=Z.name;tt[c]={};if(!vs.length)return;
   try{const z=await api('list_objects',{type:Z.id,filters:[{property:Z.key[0],op:'in',value:vs}],limit:Math.min(1000,vs.length)}),ki=z.columns.indexOf(Z.key[0]),ti=z.columns.indexOf(Z.title);z.rows.forEach(x=>tt[c][String(x[ki])]=ti<0?null:x[ti])}catch(e){}}));
  if(my!==st.n)return;
  rows(out,d);out.classList.add('pick');
  // after each draw (a sort draws again): no column id in a header's tooltip, a row reachable by keyboard
  const mark=()=>{out.querySelectorAll('th').forEach(th=>th.removeAttribute('title'));out.querySelectorAll('tr[data-r]').forEach(tr=>{tr.tabIndex=0;tr.setAttribute('role','link');
    Object.entries(to).forEach(([c,Z])=>{const j=d.columns.indexOf(c),td=tr.children[j],v=raw[c][tr.dataset.r];if(!td||td.dataset.o||v===null||v===undefined||v===MASK)return;td.dataset.o=1;
     // an object without a name, or one this person can't see: a dash, never its raw key
     const nm=tt[c][String(v)];if(nm===undefined||nm===null||nm===MASK){td.textContent='—';return}const b=h('button','flink oopen',String(nm));b.onclick=e=>{e.stopPropagation();openObj(Z.id,[v])};td.replaceChildren(b)})})};
  mark();new MutationObserver(mark).observe(out.querySelector('table'),{childList:true,subtree:true});
  // the pages: under the table, and for a whole list (50 rows a page) above it too, in reach without scrolling
  if(st.offset||r.more){const b=(t,on,fn)=>{const x=h('button','btn',t);x.disabled=!on;x.onclick=fn;return x};
   (fixed?[foot]:[head,foot]).forEach(f=>f.append(b('Previous',st.offset,()=>{st.offset=Math.max(0,st.offset-n);load()}),h('span','file',`${st.offset+1} to ${st.offset+r.rows.length}`),b('Next',r.more,()=>{st.offset+=n;load()})))}}
 bar.append(head);box.append(bar,out,foot);load()}
function objectsPage(box,T){objBar(box,'obj:'+T.id);if(T.description&&T.description!==MASK)box.querySelector('.pghead .ttl')?.append(h('p','',T.description));sameCard(box,T);const c=h('div','card');box.append(c);objTable(c,T)}
// 1.2.6 M11: objects that look like the same one. ABI proposes them with why (find_same) and merges nothing; the person
// decides (decide_same, through ABI's confirmation: the page never confirms by itself) and can take a decision back.
// For Editors and Owners in Developer. Business names only: never a key, a table or a column. `key`: one object's page
function sameCard(box,T,key){if(!T.resolve||MODE==='viewer'||myRank()<2)return;const c=card('Possible duplicates'),many=T.plural.toLowerCase();c.classList.add('wide');c.hidden=true;box.append(c);
 const nm=o=>{const b=h('button','flink oopen',String(o.title??'(no name)'));b.onclick=()=>openObj(T.id,[o.key]);return b},
  line=(lead,os)=>{const d=h('div','txt');d.append(lead);os.forEach((o,i)=>d.append(i?(i===os.length-1?' and ':', '):' ',nm(o)));return d},
  go=async(args,done,action)=>{try{await api(action||'decide_same',args);toast(done);show(CUR)}catch(e){toast(e.message,!/^You declined/.test(e.message))}},
  doubt=p=>p.why.some(w=>w.startsWith('different '));
 api('find_same',{type:T.id,...(key?{key:key[0]}:{})}).then(r=>{if(!c.isConnected)return;let pri=key?0:1;  // an object's page has its own primary action
  r.groups.forEach(g=>{const d=h('div','item'),a=h('div','row acts'),n=g.objects.length,by=Object.fromEntries(g.objects.map(o=>[o.key,o])),keys=g.objects.map(o=>o.key),unsure=g.pairs.some(doubt);
   d.append(line(`These ${n} ${many} look like the same one:`,g.objects));
   // what speaks against a group is always shown, and such a group never carries the page's one primary button
   [...g.pairs.filter(doubt),...g.pairs.filter(p=>!doubt(p))].slice(0,6).forEach(p=>{const e=h('div','file');if(n>2)e.append(nm(by[p.a]),' and ',nm(by[p.b]),': ');e.append(p.why.join(', '));d.append(e)});
   a.append(btn(`Same ${low(T)}`,()=>go({type:T.id,keys,decision:'same'},`They count as one ${low(T)} now. Undo it under Decided.`),!unsure&&pri),btn('Not the same',()=>go({type:T.id,keys,decision:'different'},'They won\'t be proposed again. Undo it under Decided.')));if(!unsure)pri=0;d.append(a);c.append(d)});
  if(r.more)c.append(h('p','file','There are more: decide these and the next ones show.'));
  if(r.note)c.append(h('p','file',`Some ${many} share a very common detail (an empty postcode, say) and weren't compared through it.`));
  if((r.records||[]).length>1){if(!r.groups.length)c.querySelector('h3').lastChild.textContent=`Records of this ${low(T)}`;else c.append(h('h4','sub',`Made of ${r.records.length} records`));
   if(!r.groups.length)c.append(h('p','file',`Made of ${r.records.length} records`));r.records.forEach(o=>{const d=h('div','item'),a=h('div','row acts');d.append(h('div','txt',[o.title,o.source].filter(Boolean).join(' · ')));
    a.append(btn('Take out',()=>go({type:T.id,keys:[o.key],decision:'split'},`It is its own ${low(T)} again. Undo it under Decided.`)));d.append(a);c.append(d)})}
  c.hidden=!c.querySelector('.item');
  // what was decided: a card of its own, under the type's list (never pushing it down) or under the object's records
  if(r.decisions.length){const dc=card('Decided');dc.classList.add('wide');box.append(dc);if(!key&&r.decisions.length>5)dc.append(h('p','file',`The latest 5. Each ${low(T)}'s page lists the decisions about it.`));
   r.decisions.slice(0,key?20:5).forEach(x=>{const d=h('div','item');
    d.append(h('div','file',[cell(String(x.at).replace(' ','T')+'Z'),x.user,x.undone?'undone':''].filter(Boolean).join(' · ')),
     x.out&&x.of?line(`Taken out of ${x.of.title??'it'}:`,x.out):line({same:'The same one:',different:'Not the same:',split:'Taken apart:'}[x.decision]||'Changed by hand:',x.objects));
    if(!x.undone&&!x.blocked){const a=h('div','row acts');a.append(btn('Undo',()=>go({table:r.table,batch:x.batch},'Taken back.','undo_edits')));d.append(a)}
    else if(x.blocked)d.append(h('div','file',x.blocked_by?`A newer decision builds on this one. Undo “${x.blocked_by}” first.`:'Undo the newer decision about them first.'));dc.append(d)})}}).catch(e=>{if(c.isConnected){c.append(h('p','empty',e.message));c.hidden=false}})}
// Data Model › Objects: the types as boxes and their connections as named lines with how many meet how many. A phone has
// no canvas: it reads the picture ABI draws from the project's files (render.sections) and each type as a card
// 1.2.7 M0 (9a): the entry is always there; before any object type exists the page says what objects are and how they come
const NO_OBJECTS='Objects are the things your business talks about, such as a customer, a café or an order. Each gets a page of its own with its figures, what it is connected to and what can be done with it.';
function objectsHome(box){if(!OT().length){box.append(emptyBox('objects','No objects yet',NO_OBJECTS+(MODE==='viewer'||MOBILE?' A builder of this project makes them on a computer; they show here after Publish.':''),
  MODE==='developer'&&!MOBILE?['Let ALL propose them',()=>{openChat(true);inp.value='Propose objects for this project.';inp.focus()}]:null));return}
 objBar(box,'objects');const L=(D.ontology.links||[]).filter(l=>otype(l.from)&&otype(l.to)),ids=OT().map(t=>t.id);
 // 1.2.7 M0 (9b): a box says what the business needs of a type: how many there are, its figures, what can be done with
 // one; never its columns (that is the Data models picture). A line reads as a sentence
 const facts=T=>[['n'],...(T.measures||[]).slice(0,4).map(id=>['m',id]),...(OA().some(a=>a.object===T.id)?[['a']]:[])];
 if(MOBILE){const pic=h('div','odiag'),g=h('div','cards mlist2');{const t=h('div');t.insertAdjacentHTML('beforeend',D.sections?.Objects||'');const svg=t.querySelector('svg');if(svg){pic.append(svg);box.append(pic)}}
  OT().forEach(T=>{const c=card(T.plural),cl=h('div','stepchips');if(T.description&&T.description!==MASK)c.append(h('p','mtext',T.description));
   if(T.links.length)cl.append(h('span','file','Connections'));T.links.forEach(w=>{const Y=otype(w.to);if(!Y)return;const b=h('button','chip',sayLink(T,Y,w.name,w.cardinality));b.onclick=()=>show('obj:'+w.to);cl.append(b)});if(T.links.length)c.append(cl);
   act(c,'Open',async()=>show('obj:'+T.id));g.append(c)});box.append(g);return}
 box.append(h('h4','sub','Objects and connections'),h('p','file','Each box is a kind of thing the business works with: how many there are, its figures and what you can do with one. Each line is a connection between two of them, read along the line. Drag a box by its title to move it; Open lists its objects.'));
 const W=250,key='abi-opos-'+D.project,hgt=t=>46+facts(otype(t)).length*27,depth={};let pos={};try{pos=JSON.parse(localStorage.getItem(key)||'{}')}catch(e){}
 const save=()=>{try{localStorage.setItem(key,JSON.stringify(pos))}catch(e){}};
 // columns by depth along the connections (no loops back); every second column starts lower, so a line across two columns passes above the one between
 {const s=ids.filter(t=>!L.some(l=>l.to===t&&l.from!==t));(s.length?s:ids.slice(0,1)).forEach(t=>depth[t]=0)}
 for(let i=0;i<ids.length;i++)L.forEach(l=>{const a=l.from,b=l.to;if(a!==b&&depth[a]!==undefined&&(depth[b]===undefined||depth[b]<depth[a]+1)&&depth[a]<ids.length)depth[b]=depth[a]+1});
 const layout=()=>{const cols={};ids.forEach(t=>(cols[depth[t]??0]??=[]).push(t));Object.entries(cols).forEach(([d,list])=>{let y=24+(d%2)*72;list.forEach(t=>{if(!pos[t])pos[t]={x:24+d*(W+200),y};y=Math.max(y,pos[t].y)+hgt(t)+40})})};
 const extent=()=>kit.extent(Math.max(...ids.map(t=>pos[t].x+W))+(L.some(l=>Math.abs(pos[l.from].x-pos[l.to].x)<W)?230:24),Math.max(...ids.map(t=>pos[t].y+hgt(t)+24)));
 const kit=canvasKit(box,()=>{pos={};layout();ids.forEach(t=>{boxes[t].style.left=pos[t].x+'px';boxes[t].style.top=pos[t].y+'px'});save();extent();draw()});
 const {stage,svg}=kit,boxes={};layout();
 ids.forEach(t=>{const T=otype(t),b=h('div','tbl dim'),hd=h('div','th'),go=h('button','badge','Open'),cs=h('div','cols');b.style.left=pos[t].x+'px';b.style.top=pos[t].y+'px';go.title=`List the ${T.plural.toLowerCase()}`;go.onclick=()=>show('obj:'+t);
  hd.append(h('span','',T.plural),go);const cell=k=>{const r=h('div','col'),l=h('span','',k),v=h('span','file','…');l.style.gridColumn='1 / 3';v.style.cssText='grid-column:3;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:130px';r.append(l,v);cs.append(r);return [r,v]};
  facts(T).forEach(([kind,id])=>{if(kind==='n'){const [r,v]=cell(T.event?'Recorded':'How many');r.dataset.f='n';api('list_objects',{type:t,limit:1,count:true}).then(x=>v.textContent=fmt(x.total)).catch(()=>v.textContent='—')}
   else if(kind==='m'){const [r,v]=cell(((D.measures.find(m=>m.id===id)||{}).name||colName(id))+(D.rls?.restricted?', all you can see':', whole project'));r.dataset.f='m';r.dataset.m=id}
   else{const as=OA().filter(a=>a.object===t).map(a=>a.name),[r,v]=cell('You can');r.dataset.f='a';v.textContent=as.slice(0,2).join(', ')+(as.length>2?` +${as.length-2}`:'');v.title=as.join(', ')}});
  if((T.measures||[]).length)api('visual_data',{query:{measures:T.measures.slice(0,4)},type:'kpi'}).then(d=>T.measures.slice(0,4).forEach((id,i)=>{const v=cs.querySelector(`[data-m="${id}"] .file`);if(v)v.textContent=d.rows[0]?.[i]==null?'—':fmt(d.rows[0][i],d.format?.[id])})).catch(()=>cs.querySelectorAll('[data-f=m] .file').forEach(v=>v.textContent='—'));
  b.append(hd,cs);stage.append(b);boxes[t]=b;
  hd.onpointerdown=e=>{if(e.target===go)return;e.preventDefault();const [sx,sy]=kit.local(e.clientX,e.clientY),ox=pos[t].x,oy=pos[t].y;hd.setPointerCapture(e.pointerId);
   hd.onpointermove=m=>{const [x,y]=kit.local(m.clientX,m.clientY);pos[t]={x:Math.max(0,ox+x-sx),y:Math.max(0,oy+y-sy)};b.style.left=pos[t].x+'px';b.style.top=pos[t].y+'px';extent();draw()};
   hd.onpointerup=()=>{hd.onpointermove=null;save()}}});
 // a connection: a line from its type to the one it leads to (the dot), its sentence (sayLink) at the middle; two
 // boxes of one column are joined by an arc on their right, and a second connection of the same two sits lower
 function draw(){svg.innerHTML='';svg.setAttribute('width',kit.w);svg.setAttribute('height',kit.ht);const n={};
  L.forEach(l=>{const A=pos[l.from],B=pos[l.to],ya=A.y+19,yb=B.y+19,pk=[l.from,l.to].sort().join(),k=n[pk]=(n[pk]||0)+1;let d,lx,ly,x2,anc='middle';
   if(Math.abs(A.x-B.x)<W){const x=Math.max(A.x,B.x)+W,r=30+k*28;x2=B.x+W;d=`M${A.x+W},${ya} C${x+r},${ya} ${x+r},${yb} ${x2},${yb}`;lx=x+r*.75+6;ly=(ya+yb)/2+4;anc='start'}
   else{const R=A.x<B.x,x1=R?A.x+W:A.x;x2=R?B.x:B.x+W;const dx=Math.max(40,Math.abs(x2-x1)/2)*(R?1:-1);d=`M${x1},${ya} C${x1+dx},${ya} ${x2-dx},${yb} ${x2},${yb}`;lx=(x1+x2)/2;ly=(ya+yb)/2+4+(k-1)*18}
   el('path',{d,class:'ln'},svg);el('circle',{cx:x2,cy:yb,r:4,class:'one'},svg);const t=el('text',{x:lx,y:ly,class:'lbl','text-anchor':anc},svg);t.textContent=says(l);
   const bb=t.getBBox();svg.insertBefore(el('rect',{x:bb.x-5,y:bb.y-2,width:bb.width+10,height:bb.height+4,class:'lblbg'}),t)})}
 kit.draw=draw;extent();requestAnimationFrame(draw);new ResizeObserver(()=>draw()).observe(kit.wrap)}
// one object: what it is (its properties), its numbers (the type's measures, filtered to it), what it connects to (a
// section per connection; each neighbour opens its own page) and what happened to it. In Developer its actions are
// buttons: the first one the page's main action, the others under More; each opens a form of the action's inputs
async function objectPage(box,T,key){const bar=h('div','row bar lead');bar.append(btn('← '+T.plural,()=>show('obj:'+T.id)));box.append(bar);
 const tt=box.querySelector('.pghead .ttl'),eb=tt.querySelector('.eyebrow')||tt.insertBefore(h('div','eyebrow'),tt.firstChild);eb.textContent=T.name;
 const mine=MODE==='viewer'?[]:OA().filter(a=>a.object===T.id&&RANK[a.role||'editor']<=myRank());let fc=null,tb=null;
 // plain buttons, whatever the action is called (btn() reads "Remove…" as a project delete); the bar hides while a form is open
 if(mine.length){tb=h('div','row bar top');const shut=()=>{fc?.remove();fc=null;tb.hidden=false};mine.forEach((a,i)=>{const b=h('button','btn w'+(i?' mi':' pri'),a.name);b.onclick=()=>{shut();
    fc=actionForm(a,()=>key,async r=>{show(CUR);return actDone(a,r)});act(fc,'Cancel',shut);tb.hidden=true;box.querySelector('.pghead').after(fc);fc.querySelector('.fld input,.fld select,.fld textarea,.acts .btn')?.focus()};
   if(i){b.setAttribute('role','menuitem');more(tb).append(b)}else tb.append(b)});box.append(tb)}
 let o;try{o=await api('get_object',{type:T.id,key})}catch(e){if(box.isConnected){tb?.remove();eb.remove();box.append(emptyBox('lock','Not available',e.message,[`Open ${T.plural.toLowerCase()}`,async()=>show('obj:'+T.id)]))}return}
 if(!box.isConnected)return;key=o.key;  // 1.2.6 M11: a merged record's key opens the object it became: everything below is that object's
 if(COPY())pubNote(box);const title=String(o.title??key.join(' · '));box.querySelector('.pghead h1').textContent=title;$('#pgname').textContent=title;document.title=title+' · '+D.project;if(PAGES[CUR])PAGES[CUR][0]=title;setTimeout(ctx);
 if(T.description&&T.description!==MASK)tt.append(h('p','',T.description));
 // the columns that point at another object aren't properties here: that object is in its connection's section below
 const g=h('div','cards'),pc=card('Properties'),kv=h('div','kv okv'),R=D.model?.relationships||[],there=new Set((D.ontology.links||[]).filter(l=>l.via.length===1&&[l.from,l.to].includes(T.id)).flatMap(l=>R.find(r=>r.id===l.via[0]&&r.from.table===T.table)?.from.columns||[]));
 o.columns.forEach((c,i)=>{const p=T.properties.find(p=>p.column===c);if(!p||c===T.title||there.has(c)&&!T.key.includes(c))return;const v=pval(T,p,o.rows[0][i]),d=h('div');d.append(h('b','',pname(p,T)),h('span','',v===null?'—':typeof v==='number'?fmt(v,p.meaning==='money'?{decimals:2,fixed:true}:null):String(v)));kv.append(d)});
 pc.classList.add('wide');pc.append(kv);
 // its measures as a report's headline numbers: ABI's visual_data, filtered to the object, each in its own format.
 // 1.2.7 M0 (9b): they come first: the figures are what a person opens the page for
 if((T.measures||[]).length){const mc=card('Measures'),ks=h('div','kpis');mc.classList.add('wide');mc.append(ks);g.append(mc);
  api('visual_data',{query:{measures:T.measures,filters:T.key.map((k,i)=>({column:`${T.table}.${k}`,op:'=',value:o.key[i]}))},type:'kpi'}).then(d=>chart(ks,d)).catch(e=>ks.replaceWith(h('p','empty',e.message)))}
 g.append(pc);
 // ponytail: an object with a several-column key asks one condition per key column (each must hold for a linked object)
 sameCard(g,T,o.key);
 T.links.slice(0,12).forEach(w=>{const Y=otype(w.to);if(!Y||w.inverse&&w.to===T.id)return;
  // 1.2.7 M0 (9b): a proposal names a link by a verb ("is for", "has"): the section is then the linked type, its sentence under it
  // (review F2) a name that doesn't name the linked type ("is for", "placed at") is a verb: headed by the type
  const nm=String(w.name||'').toLowerCase(),verb=!nm.includes(Y.name.toLowerCase())&&!nm.includes(Y.plural.toLowerCase()),
   c=card(verb?(/one$/.test(w.cardinality||'')?Y.name:Y.plural):cap(w.name));c.classList.add('wide');
  if(verb)c.querySelector('h3').append(h('span','file',sayLink(T,Y,w.name,w.cardinality)));else if(cap(w.name)!==Y.plural)c.querySelector('h3').append(h('span','file',Y.plural));
  objTable(c,Y,key.map((v,i)=>({link:w.link,property:T.key[i],op:'=',value:v})),10);g.append(c)});
 const hc=card('History');hc.classList.add('wide');g.append(hc);box.append(g);
 // the published copy keeps no history: it is the working project's
 if(COPY())hc.append(h('p','empty',`The history of this ${low(T)} is kept in the working project, not in the published version. The same goes for what an action or an edit wrote: the published version shows the loaded data, so such a value can read “—” here. Editors see both in Developer.`));else objHistory(hc,T,key)}
// what happened to an object, newest first: the actions run on it, the changes to it and what those started
async function objHistory(c,T,key){let x;try{x=await api('object_history',{type:T.id,key,limit:50})}catch(e){c.append(h('p','empty',e.message));return}
 const ts=v=>{const d=new Date(String(v).replace(' ','T'));return isNaN(d)?String(v??''):d.toISOString()},e0=v=>v===null||v===undefined||v===''?'(empty)':v===MASK?'(hidden for your access)':String(v),
  P=c=>pname(T.properties.find(p=>p.column===c||p.column.endsWith('.'+c))||{column:c},T);
 // a whole row added to a table kept beside the object's (its first note, say) is no event of the object: its cells' changes are listed
 const all=[...x.runs.map(r=>{const a=OA().find(a=>a.id===r.action);return [ts(r.at),a?.name||'Action',r.user||'',r.inputs?Object.entries(r.inputs).map(([k,v])=>`${a?.inputs?.properties?.[k]?.title||colName(k)}: ${e0(v)}`).join(', '):'']}),
  ...x.edits.flatMap(e=>{const i=n=>e.columns.indexOf(n);return e.rows.filter(r=>r[i('column')]||e.table===T.table).map(r=>[ts(r[i('reviewed_at')]||r[i('timestamp')]),r[i('column')]?`${P(r[i('column')])} changed`:'Added',r[i('user')]||'',
    (r[i('column')]?`${e0(r[i('old_value')])} → ${e0(r[i('new_value')])}`:'')+({active:'',synced:''}[r[i('status')]]??` (${r[i('status')]})`)])}),
  ...x.flows.map(f=>[ts(f.created),`${(D.flows||[]).find(y=>y.id===f.flow)?.name||'Follow-up'}: ${{ok:'finished',failed:'failed',running:'running',queued:'waiting to start'}[f.status]||f.status}`,'',''])].sort((a,b)=>b[0].localeCompare(a[0]));
 if(!all.length){c.append(h('p','empty',`Nothing has happened to this ${low(T)} yet. Actions run on it and changes to it show here.`));return}
 // a phone reads it as a list (the Inbox's items): when and who, then what
 if(MOBILE){all.forEach(([at,what,who,more])=>{const d=h('div','item');d.append(h('div','file',[cell(at),who].filter(Boolean).join(' · ')),h('div','txt',[what,more].filter(Boolean).join(': ')));c.append(d)});return}
 const t=h('div','olist');c.append(t);rows(t,{columns:['When','What','Who','Details'],rows:all})}
// an action's form: only its inputs (the action type's JSON Schema), each with its description as the hint. The browser
// checks what is required, lengths and ranges; ABI checks the rest: its message shows in the form and marks the field.
// Submit asks ABI, which asks the person to confirm when the action says so (api → ask): a form never confirms by itself
const RULE={maxLength:'is too long (at most $ characters)',minLength:'is too short (at least $ characters)',minimum:'must be at least $',maximum:'must be at most $',exclusiveMinimum:'must be more than $',exclusiveMaximum:'must be less than $',
 multipleOf:'must be a multiple of $',enum:'must be one of the choices',type:'is not the kind of value expected',pattern:'doesn\'t have the expected form',format:'doesn\'t have the expected form',required:'is needed'};
function actionForm(a,getKey,done){const c=card(a.name),S=a.inputs?.properties||{},req=a.inputs?.required||[],F={},m=h('div','msg'),r=h('div','row acts'),go=h('button','btn pri',a.name);c.classList.add('aform','w');go.type='button';
 if(a.description&&a.description!==MASK)c.append(h('p','file',a.description));
 // the fields in the order `required` lists them, then the others (ABI stores a JSON object's keys sorted)
 [...req.filter(k=>S[k]),...Object.keys(S).filter(k=>!req.includes(k))].forEach(k=>{const s=S[k],ty=[].concat(s.type||'string').find(x=>x!=='null'),num=ty==='integer'||ty==='number',lbl=s.title||colName(k);let e;
  if(s.enum)e=sel([['',req.includes(k)?'Choose…':'(none)'],...s.enum.map(v=>[JSON.stringify(v),String(v)])]);
  else if(ty==='boolean'){e=h('input');e.type='checkbox'}
  else if(!num&&(s.maxLength||0)>120){e=h('textarea','f');e.rows=3}
  else{e=h('input','f');e.type=num?'number':{date:'date','date-time':'datetime-local',email:'email',uri:'url'}[s.format]||'text';if(num)e.step=ty==='integer'?1:'any';
   if(s.minimum!==undefined)e.min=s.minimum;if(s.maximum!==undefined)e.max=s.maximum;if(s.pattern)e.pattern=s.pattern}
  if(!s.enum&&ty==='string'){if(s.minLength)e.minLength=s.minLength;if(s.maxLength)e.maxLength=s.maxLength}
  if(ty!=='boolean')e.required=req.includes(k);if(s.default!==undefined){if(ty==='boolean')e.checked=!!s.default;else e.value=s.enum?JSON.stringify(s.default):s.default}
  F[k]=[e,ty,lbl,s];c.append(fld(lbl+(e.required||ty==='boolean'?'':' (optional)'),e,s.description))});
 r.append(go);c.append(r,m);c.addEventListener('input',ev=>{ev.target.classList.remove('invalid');ev.target.removeAttribute('aria-invalid');if(m.classList.contains('bad'))m.textContent=''});
 // its own button, not act(): an action may be called anything ("Remove the visit flag") and is never a project delete
 go.onclick=async()=>{go.disabled=true;c.querySelectorAll('.invalid').forEach(x=>{x.classList.remove('invalid');x.removeAttribute('aria-invalid')});
  try{const inputs={},key=await getKey();Object.entries(F).forEach(([k,[e,ty,lbl,s]])=>{need(e,e.checkValidity(),`${lbl}: ${e.validationMessage}`);
    if(ty==='boolean')inputs[k]=e.checked;else if(e.value!=='')inputs[k]=s.enum?JSON.parse(e.value):ty==='integer'||ty==='number'?+e.value:e.value});
   const t=await done(await api('run_object_action',{type:a.object,key,action:a.id,inputs}));c.isConnected?say(m,t):toast(t)}
  catch(e){if(/^You declined/.test(e.message)){m.className='msg';m.textContent=e.message;return}  // a no is no error
   // ABI's own check of the inputs: each field it names is marked, with the rule in words
   const bad=(e.fields||[]).flatMap(([p,why])=>{const [,rule,val]=/^fails `(\w+)`: ([\s\S]*)$/.exec(why)||[];let ks=[p.replace(/^\$\.?/,'').split(/[.\[]/)[0]];if(rule==='required')try{ks=JSON.parse(val).filter(k=>F[k]?.[0].value==='')}catch(x){}
    return ks.filter(k=>F[k]).map(k=>{F[k][0].classList.add('invalid');F[k][0].setAttribute('aria-invalid','true');return `${F[k][2]} ${(RULE[rule]||'doesn\'t fit').replace('$',val)}.`})}),t=bad.length?[...new Set(bad)].join('\n'):e.message;
   c.isConnected?say(m,t,1):toast(t,1)}
  finally{go.disabled=false}};return c}
// what an action did, in words: saved (or sent for approval) and what it started
function actDone(a,r){const w=r.write?.status,f=a.flow&&(D.flows||[]).find(x=>x.id===a.flow)?.name;
 return [w==='proposed'?'Sent for approval: an editor approves the change under Data › Edits.':w?'Saved. Undo it under Data › Edits.':'',r.run?`Started ${f?`“${f}”`:'its follow-up'}.`:'',
  /couldn't be queued/.test(r.note||'')?'It starts as soon as the queue is back.':''].filter(Boolean).join(' ')||'Done.'}
// ABI's confirmation of an action on an object, in the person's words: the action's name as the question, the object by
// its title, then what was entered and what changes (plainPreview → actWords). The yes stays the person's click. A
// proposal (an approval action, or an agent's call) starts no flow, so none is named
// What the yes does is read from the preview ABI signed (after_yes), never from the message: the message quotes what
// was typed, and typed text must not be able to say "this is only a proposal"
function actAsk(c){let j;try{j=JSON.parse(c.preview)}catch(e){return c}const a=OA().find(x=>x.id===j?.action),T=otype(j?.object?.type),ay=j?.after_yes,prop=ay?.write==='proposal';if(!a||!T)return c;
 return {...c,plain:true,title:`${a.name}?`,yes:a.name,message:'',preview:JSON.stringify(ay&&!ay.starts_flow?{...j,flow:undefined}:j),lead:`${T.name} “${j.object.title??j.object.key.join(', ')}”.`+(prop?' An editor approves the change before it applies.':'')}}
// (j.cut: the harness left part of a long preview out and says so)
function actWords(j){const a=OA().find(x=>x.id===j.action),T=otype(j.object?.type),S=a?.inputs?.properties||{},v=x=>x===null||x===undefined||x===''?'(empty)':x===MASK?'(hidden for your access)':String(x),
  pn=c=>pname((T?.properties||[]).find(p=>p.column===c||p.column===`${j.writes.table}.${c}`)||{column:c},T);
 return [...Object.entries(j.inputs||{}).map(([k,x])=>`${S[k]?.title||colName(k)}: ${v(x)}`),...(j.writes?.changes||[]).map(c=>`${pn(c.column)}: ${v(c.old)} → ${v(c.new)}`),
  ...(j.flow?[`Then starts “${(D.flows||[]).find(f=>f.id===j.flow)?.name||'its follow-up'}”.`]:[]),...(j.cut?['Part of what ABI showed is too long to show here.']:[])].join('\n')}
// the chat agent's step asks (the harness's wording: ABI's message, "Preview:", its JSON, "Proceed?"): an action on an
// object reads as a form's own confirmation does; anything else as before
function chatAsk(e){const m=/^(Run '[\s\S]*?)\n\nPreview:\n([\s\S]*)\n\nProceed\?\s*$/.exec(e.message||''),c=m?actAsk({message:m[1],preview:m[2]}):null;
 return c?.lead?c:{message:e.title||'ABI asks for your confirmation',preview:e.message}}
// ABI's words about objects in the person's: display names for ids, and what to do next
function objWords(t){const L=D.ontology?.links||[],nm=(l,i)=>l.find(x=>x.id===i)?.name||i;
 const out=t.replace(/No visible (.+?) with key [\s\S]*? in project '[^']+'\./,(m,n)=>`This ${n.toLowerCase()} isn't available: it may have been removed, or your access doesn't include it.`)
  .replace(/You can't read the (.+?) of project '[^']+' one by one: what identifies them is hidden from you\./,(m,n)=>`You can't open ${n.toLowerCase()} one by one: what identifies them is hidden for your access.`)
  .replace(/Table '[^']+' has no column '[^']+' you can read\. Use one of: [^\n]*/,'Their names are hidden for your access, so they can\'t be searched or opened by name.')
  .replace(/Measure '([^']+)' follows the many-to-many link '([^']+)', and totals across it are refused[^.]*\./g,(m,i,l)=>`“${nm(D.measures||[],i)}” can't be shown as one total here: the same rows count for several members (“${nm(L,l)}”), so it is shown per member instead.`)
  .replace(/the many-to-many link '([^']+)'/g,(m,l)=>`“${nm(L,l)}”, where the same rows count for several members`)
  .replace(/Link '[^']+' joins on a column you can't read\./,'This isn\'t shown with your access.')
  .replace(/Inputs don't match the `inputs` schema of [^\n]*(\n[\s\S]*)?$/,'Some of what you entered doesn\'t fit. Check each field and try again.')
  .replace(/\baction type '([^']+)'/gi,(m,i)=>`“${nm(OA(),i)}”`).replace(/\bobject type '([^']+)'/gi,(m,i)=>nm(OT(),i)).replace(/\blink '([^']+)'/gi,(m,i)=>`“${nm(L,i)}”`).replace(/:? `ontology` lists [^.]*\./g,'.');
 return out===t?t:out.replace(/ (in|of) project '[^']+'/g,'')}
// a column that names objects: a type's title, or its one key column. A member of it is that object: opened by its key,
// or by its title (the one object of that title, else the type's list searched by it)
const objCol=c=>OT().find(T=>c===`${T.table}.${T.title}`||T.key.length===1&&c===`${T.table}.${T.key[0]}`);
async function objOpen(T,col,v){if(T.key.length===1&&col===`${T.table}.${T.key[0]}`){openObj(T.id,[v]);return}
 try{const x=await api('list_objects',{type:T.id,filters:[{property:T.title,op:'=',value:v}],limit:2});if(x.rows.length===1){openObj(T.id,x.key.map(k=>x.rows[0][x.columns.indexOf(k)]));return}}catch(e){toast(e.message,1);return}
 PENDING.osearch=String(v);show('obj:'+T.id)}
// where a click answers nothing else (Explore, a Home pin, an Inbox message's rows): a row or mark that is an object
// opens its page. A report's click filters, as ever: there the filter's chip gets an Open link (report → draw)
function objClicks(body,dim,d){const T=objCol(dim);if(!T)return;body.classList.add('pick');body.onclick=e=>{const r=d.rows[e.target.closest?.('[data-r]')?.dataset.r];if(r&&r[0]!==null)objOpen(T,dim,r[0])}}
// pick one object by its title (a form card with no fixed object): type to search, choose from the matches; two of
// one title are told apart by a property they differ in (else by what identifies them)
function objPick(T,set){const w=h('div','opick'),q=h('input','f'),ls=h('div','hits');let tm,n=0;q.type='search';q.placeholder=`Search ${T.plural.toLowerCase()}`;w.append(q,ls);
 const find=async()=>{const my=++n;let r;try{r=await api('list_objects',{type:T.id,...(q.value.trim()?{search:q.value.trim()}:{}),limit:8})}catch(e){if(my===n)ls.replaceChildren(h('span','file',e.message));return}
  if(my!==n)return;const ti=r.columns.indexOf(r.title),kx=r.key.map(c=>r.columns.indexOf(c)),id=x=>kx.map(i=>x[i]).join(' · ');
  const apart=x=>{const same=r.rows.filter(y=>y[ti]===x[ti]);if(ti<0||same.length<2)return '';const i=r.columns.findIndex((c,i)=>i!==ti&&!kx.includes(i)&&new Set(same.map(y=>String(y[i]))).size===same.length);const v=i<0?id(x):pval(T,T.properties.find(p=>p.column===r.columns[i])||{},x[i]);return v===null||v===''?'':' · '+v};
  ls.replaceChildren(...r.rows.map(x=>{const b=h('button','sl',String(x[ti]??id(x))+apart(x));b.type='button';b.onclick=()=>{set(kx.map(i=>x[i]));q.value=b.textContent;q.classList.remove('invalid');q.removeAttribute('aria-invalid');ls.replaceChildren()};return b}));
  if(!r.rows.length)ls.append(h('span','file','No match.'))};
 q.oninput=()=>{set(null);clearTimeout(tm);tm=setTimeout(find,250)};q.onfocus=()=>{if(!ls.children.length&&!q.value)find()};return w}
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
 // a new report: say what it should show and the agent builds it in ABI; the JSON is there for those who want it
 if(isNew&&kind==='report'){const say0=h('textarea','f ask0'),row=h('div','row'),det=h('details','json');say0.rows=3;
  say0.placeholder='e.g. Revenue and orders by region and month, a KPI row on top and a filter for the product category';
  row.append(btn('Ask the agent to build it',async()=>{if(!say0.value.trim()){toast('Say what the report should show first.',1);return}openChat(true);inp.value='Build a new report in ABI: '+say0.value.trim();sendChat()},1));
  c.insertBefore(h('label','l','What should it show?'),ta);c.insertBefore(say0,ta);c.insertBefore(row,ta);det.append(h('summary','','Or write the report as JSON'));c.insertBefore(det,ta);det.append(ta)}
 return c}
function editBtns(c,kind,obj){const K=KIND[kind];let ed=null;c.querySelector('h3')?.append(pinBtn({kind,ref:obj.id,title:obj.name||obj.id}));
 act(c,'Edit as JSON',()=>{if(ed){ed.remove();ed=null;return}ed=editor(kind,obj,false);c.after(ed);ed.scrollIntoView({behavior:'smooth',block:'nearest'})});
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
   const sm=h('p','sum'),upd=()=>{try{const g=tb.get();sm.textContent=`Runs ${cronWords(g.cron).replace(/^Every/,'every').replace(/^Day/,'on day')} (${g.timezone}).`}catch(e){sm.textContent=''}};
   const vis=()=>{dw.hidden=fq.value!=='w';dm.hidden=fq.value!=='m';upd()};fq.onchange=vis;tz.setAttribute('list','tz-list2');const zl=h('datalist');zl.id='tz-list2';try{Intl.supportedValuesOf('timeZone').forEach(z=>zl.append(new Option(z)))}catch(e){}
   [dw,dm,tm,tz].forEach(e=>{e.addEventListener('input',upd);e.addEventListener('change',upd)});const r=h('div','row');r.append(fq,dw,dm,lab('at'),tm,tz);tb.append(r,sm,zl);
   tb.get=()=>{const [H,M]=(tm.value||'07:00').split(':').map(Number);return {type:'schedule',cron:`${M} ${H} ${fq.value==='m'?Math.min(28,Math.max(1,+dm.value||1)):'*'} * ${fq.value==='w'?dw.value:'*'}`,timezone:tz.value||'UTC'}};vis()}
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
 list.classList.add('flowsteps');
 return ()=>{need(nm,nm.value.trim(),'Give it a name, e.g. Morning refresh.');const st=[...list.children].map((r,i)=>({id:`step_${i+1}`,...r.get()}));need(add,st.length,'Add at least one step: pick one under Steps, in order.');
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
// what a custom agent may call (ABI: its project's tools, four reads, four actions, other published agents)
const AGENT_TOOLS=[['semantic_query','Query the measures in its scope (read)'],['report_snapshot','Render a report as PDF or PNG (read)'],
 ['get_object','Read one object of the ontology, with its links, within its scope (read)'],['list_objects','List and filter objects of the ontology within its scope (read)'],
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
// a new report from a few choices: headline numbers, one measure broken down, its line by month (the starter's defaults)
function reportForm(c){const nm=inpt('e.g. Sales overview'),de=inpt('what it answers (optional)'),plain=D.measures.filter(m=>!/(_prev|_change|_change_pct)$/.test(m.id)&&!/of ML model '/.test(m.description||'')),
  money=/revenue|sales|amount|total|value|profit|margin|income|spend|cost/i,
  rank=plain.filter(m=>!/^\s*(AVG\(|SUM\(\s*\w+\.\w*(price|rate|ratio|pct|percent|share|score|discount)\w*\s*\)\s*$)/i.test(m.expression||'')).sort((a,b)=>(!money.test(a.id))-(!money.test(b.id))||(!a.id.endsWith('_rows'))-(!b.id.endsWith('_rows'))),
  kp=lmulti(plain.map(m=>[m.id,m.name||m.id]),rank.slice(0,3).map(m=>m.id)),cm=sel(plain.map(m=>[m.id,m.name||m.id]),(rank[0]||plain[0])?.id),
  by=sel([['','no breakdown'],...D.dims.filter(d=>!d.startsWith('calendar.')).map(d=>[d,`${colName(d)} · ${d.split('.')[0]}`])]),ty=sel([['bar','Bar chart'],['pie','Pie chart'],['table','Table']]),
  cal=['calendar.year','calendar.month'].every(d=>D.dims.includes(d)),ln=h('label','chk'),lc=h('input');lc.type='checkbox';lc.checked=cal;lc.disabled=!cal;ln.append(lc,h('span','',cal?'A line of that measure by month':'A line by month (the model has no calendar)'));
 by.value=D.dims.find(d=>/region|category|country|city|segment/.test(d)&&!d.startsWith('calendar.'))||'';
 const two=h('div','two');two.append(fld('Name',nm),fld('Description',de));const tri=h('div','formrow');tri.append(fld('Chart measure',cm),fld('Broken down by',by),fld('As',ty));
 c.append(two,lab('Headline numbers (one KPI each, up to four)'),kp,tri,ln);
 return async()=>{need(nm,nm.value.trim(),'Give it a name, e.g. Sales overview.');need(nm,!D.reports.some(r=>(r.name||r.id).toLowerCase()===nm.value.trim().toLowerCase()),`A report called ${nm.value.trim()} exists: give this one its own name.`);
  let id=slug(nm.value,'report');while(D.reports.some(r=>r.id===id))id+='_2';
  const ri=x=>{const i=rank.findIndex(m=>m.id===x);return i<0?99:i},ks=[...kp.selectedOptions].map(o=>o.value).sort((a,b)=>ri(a)-ri(b)).slice(0,4),N=x=>(D.measures.find(m=>m.id===x)||{}).name||x,
   vs=ks.map((m,i)=>({id:'kpi-'+m,type:'kpi',title:N(m),query:{measures:[m],dimensions:[]},layout:{x:i*(12/ks.length|0),y:0,w:12/ks.length|0,h:2}}));
  const half=by.value&&lc.checked;
  if(by.value)vs.push({id:'by-'+by.value.split('.').pop(),type:ty.value,title:`${N(cm.value)} by ${colName(by.value).toLowerCase()}`,query:{measures:[cm.value],dimensions:[by.value],limit:20,order_by:[{field:cm.value,desc:true}]},layout:{x:0,y:2,w:half?6:12,h:5}});
  if(lc.checked)vs.push({id:'by-month',type:'line',title:`${N(cm.value)} per month`,query:{measures:[cm.value],dimensions:['calendar.year','calendar.month']},layout:{x:half?6:0,y:2,w:half?6:12,h:5}});
  need(kp,vs.length,'Pick a headline number or a breakdown.');
  return {$schema:'https://schema.abi.invalid/v1/report.json',id,name:nm.value.trim(),...(de.value.trim()?{description:de.value.trim()}:{}),pages:[{id:'main',name:'Main',visuals:vs}]}}}
const FORMS={flow:[flowForm,'flows'],ml:[mlForm,'ml'],tool:[toolForm,'tools'],agent:[agentForm,'agents'],report:[reportForm,'reports']};
function newBtn(box,kind){const bar=box.querySelector(':scope>.bar.top')||h('div','row bar top'),L=KIND[kind].label;
 bar.append(btn(`New ${L}`,()=>{if(box.querySelector('.newkind'))return;const c=card(`New ${L}`,'fill it in and create it; ABI checks it before it saves');c.classList.add('newkind','wide','editor');
  const get=FORMS[kind][0](c);
  act(c,`Create ${L}`,async()=>{const o=await get();{const first=c.querySelector('input');const dup=kind!=='report'&&(D[FORMS[kind][1]==='flows'?'flows':kind==='ml'?'ml':kind+'s']||[]).find(x=>x.id===o.id||(x.name||'').toLowerCase()===String(o.name||'').toLowerCase());need(first,!dup,`A ${L} called “${dup?.name||o.name}” exists: give this one its own name, or edit that one.`)}await KIND[kind].save(o);
   if(kind==='report'){PENDING.open=o.id;if(PAGES['rep:'+o.id]){PENDING.open=null;show('rep:'+o.id)}else c.querySelector(':scope>.msg').textContent='Created: it opens in a moment…';toast(`Created ${o.name}. It is an uncommitted change until you commit.`);return}
   show(FORMS[kind][1]);setTimeout(()=>highlight(o.name),300);
   doneDialog(`${L[0].toUpperCase()+L.slice(1)} created`,[`${o.name} is saved.`,kind==='flow'?'It is off: switch it on on its card when you want it to run.':kind==='ml'?'Next: train it on its card.':kind==='tool'?'Next: run its tests, then publish it.':'Next: run its evaluation set on its card.','It stays an uncommitted change until you commit.'],
    [['Close'],...(kind==='flow'?[['Open its canvas',()=>openWhenThere('flw:'+o.id)]]:[])])},1);
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
 const nodes=[{id:T0,title:t.type==='schedule'?'On a schedule':t.type==='data_condition'?'When a check holds':t.type==='after_refresh'?'After a refresh':'Trigger',sub:trigWords(t)+(t.tables?' · '+t.tables.join(', '):''),icon:t.type==='schedule'?'history':'monitor'},
  ...steps.map(x=>{const [ti,icn]=stepWords(x);return {id:x.id,title:ti,sub:stepWhat(x),when:x.when,icon:icn}})];
 const H=x=>x.when?140:118,key='abi-flow2-'+D.project+'-'+f.id;let pos={};try{pos=JSON.parse(localStorage.getItem(key)||'{}')}catch(e){}
 const save=()=>{try{localStorage.setItem(key,JSON.stringify(pos))}catch(e){}};
 // columns by depth, as many to a row as the canvas fits: a long chain wraps like lines of text
 const layout=()=>{const per=Math.max(2,Math.floor(((kit.wrap.clientWidth||1100)-24)/280)),cols={};nodes.forEach(n=>(cols[depth[n.id]]??=[]).push(n));const tall={};Object.entries(cols).forEach(([d,list])=>{const r=Math.floor(d/per);tall[r]=Math.max(tall[r]||0,list.reduce((a,n)=>a+H(n)+28,0))});
  const top=r=>24+[...Array(r).keys()].reduce((a,i)=>a+(tall[i]||0)+40,0);Object.entries(cols).forEach(([d,list])=>{let y=top(Math.floor(d/per));list.forEach(n=>{if(!pos[n.id])pos[n.id]={x:24+(d%per)*280,y};y=Math.max(y,pos[n.id].y)+H(n)+28})})};
 const extent=()=>kit.extent(Math.max(...nodes.map(n=>pos[n.id].x+260)),Math.max(...nodes.map(n=>pos[n.id].y+H(n)+24)));
 {const tb=h('div','row bar top'),st=h('span','state'+(f.enabled?' on':''),f.enabled?'On':'Off'),lr=h('span','file lastrun','Last run: …');
  const mark=one=>{Object.values(els).forEach(e=>{e.classList.remove('ok','failed','skipped','running');e.querySelector('.ns')?.remove()});if(!(one.steps||[]).length)steps.forEach(x=>els[x.id]?.append(h('div','ns','No run yet')));(one.steps||[]).forEach(r=>{const e=els[r.step||r.id];if(!e)return;e.classList.add(r.status);e.append(h('div','ns',r.status==='ok'?'Done':r.status==='failed'?'Failed'+(r.error?': '+String(r.error).slice(0,60):''):r.status))});
   lr.textContent=one.status?`Last run: ${nobody(one)?'reached nobody (your Inbox says who)':{ok:'finished',failed:'failed',running:'running'}[one.status]||one.status}${one.started?' · '+cell(one.started):''}`:'Not run yet';els[T0]?.append(h('div','ns',lr.textContent));
   // 1.2.6 M7: a run an action on an object started says which one (ABI tells its runner, Editors and Owners)
   {const T=one.object&&otype(one.object.type);if(T){const o=h('button','flink oopen',`for this ${low(T)}`);o.onclick=()=>openObj(T.id,one.object.key);lr.append(' ',o)}}};
  tb.append(st,lr,t.per?h('span','file',`Runs by itself, once for each ${low(otype(t.per)||{name:'object'})} that starts to meet its condition.`):btn('Run now',async()=>{const r=await api('run_flow',{flow:f.id});lr.textContent='Running…';for(let i=0;i<150;i++){await wait(2000);const x=await api('flow_runs',{flow:f.id,run:r.run}),one=x.runs?.[0]||x;mark(one);
    if(['ok','failed','stopped','rejected','waiting'].includes(one.status)){toast(`${f.name||f.id}: run ${one.status==='ok'?'finished':one.status}.`,one.status==='failed');return}}toast('Still running: this page follows it.')},1),
   btn(f.enabled?'Disable':'Enable',async()=>{await api('enable_flow',{flow:f.id,enabled:!f.enabled});f.enabled=!f.enabled;toast(f.enabled?'Switched on: it runs on its trigger.':'Switched off.');show('flw:'+f.id)}));box.append(tb);flowMark=mark}
 {const p=h('p','fsum');p.append(`${trigWords(t)}, then ${steps.map(x=>stepWords(x)[0].toLowerCase()).join(', then ')||'no steps yet'}.${f.description?' '+f.description:''}`);box.append(p)}
 const kit=canvasKit(box,()=>{pos={};layout();nodes.forEach(n=>{els[n.id].style.left=pos[n.id].x+'px';els[n.id].style.top=pos[n.id].y+'px'});save();extent();draw()});
 const {stage,svg}=kit,els={};layout();
 nodes.forEach(n=>{const e=h('div','node'+(n.id===T0?' trig':''));e.style.left=pos[n.id].x+'px';e.style.top=pos[n.id].y+'px';const hd=h('div','nh');hd.append(ic(n.icon||'flows'),h('b','',n.title));e.append(hd,h('span','',n.sub));
  if(n.when)e.append(h('div','when',`only if ${n.when.value.replace(/[{}\s]/g,'')} ${n.when.op} ${JSON.stringify(n.when.to)}`));
  e.onpointerdown=ev=>{ev.preventDefault();const [sx,sy]=kit.local(ev.clientX,ev.clientY),ox=pos[n.id].x,oy=pos[n.id].y;e.setPointerCapture(ev.pointerId);
   e.onpointermove=m=>{const [x,y]=kit.local(m.clientX,m.clientY);pos[n.id]={x:Math.max(0,ox+x-sx),y:Math.max(0,oy+y-sy)};e.style.left=pos[n.id].x+'px';e.style.top=pos[n.id].y+'px';extent();draw()};
   e.onpointerup=()=>{e.onpointermove=null;save()}};stage.append(e);els[n.id]=e});
 const side=(id,r)=>{const b=els[id].getBoundingClientRect(),[x,y]=kit.local(r?b.right:b.left,b.top+b.height/2);return[x,y]};
 function draw(){svg.innerHTML='';svg.setAttribute('width',kit.w);svg.setAttribute('height',kit.ht);
  steps.forEach(x=>deps[x.id].forEach(d=>{if(!els[d])return;const [x1,y1]=side(d,1),[x2,y2]=side(x.id,0),dx=Math.max(30,(x2-x1)/2);
   el('path',{d:`M${x1},${y1} C${x1+dx},${y1} ${x2-dx},${y2} ${x2},${y2}`,class:'ln'},svg);el('circle',{cx:x2,cy:y2,r:3.5,class:'one'},svg)}))}
 kit.draw=draw;extent();requestAnimationFrame(draw);new ResizeObserver(()=>draw()).observe(kit.wrap);
 api('flow_runs',{flow:f.id}).then(x=>flowMark(x.runs?.[0]||x)).catch(()=>flowMark({}))}
var flowMark=()=>{};
// a step in words, with its icon, and what it works on (a table, a report, the message), never its id
function stepWords(x){const a=x.action||x.type;return ({refresh:['Refresh a table','refresh'],snapshot:['Export a report','documents'],post_message:['Post to the Inbox','inbox'],send_email:['Send an email','mail'],
  train:['Train an ML model','ml'],score:['Score an ML model','ml'],tool:['Run a tool','tools'],agent:['Ask an agent','agents'],approval:['Wait for approval','user'],condition:['Check a condition','monitor']})[a]||[String(a||'Step').replace(/_/g,' ').replace(/^./,c=>c.toUpperCase()),'flows']}
const cutw=(s,n)=>s.length>n?s.slice(0,n).replace(/\s+\S*$/,'')+'…':s;  // whole words, then an ellipsis
const plainT=s=>String(s).replace(/\{\{\s*data\.trigger\.title\s*\}\}/g,'(its name)').replace(/\{\{\s*data\.trigger\.recipients\s*\}\}/g,'the people it is for');  // a per-object alert's placeholders, in words
function stepWhat(x){const nm=(l,id)=>(l||[]).find(o=>o.id===id)?.name||id;return x.table||(x.report&&nm(D.reports,x.report))||(x.model&&nm(D.ml,x.model))||(x.tool&&nm(D.tools,x.tool))||(x.agent&&nm(D.agents,x.agent))
  ||(x.input?.text?'“'+cutw(plainT(x.input.text),110)+'”':'')||(x.input?.to?'to '+plainT([].concat(x.input.to).join(', ')):'')||cutw(x.message||'',110)||(x.measure?`${mname(x.measure)} ${x.op} ${x.value}`:'')}
// a flow's trigger in words: "Mondays at 07:00", "when Revenue < -20", not cron and action names
const DOW=['Sundays','Mondays','Tuesdays','Wednesdays','Thursdays','Fridays','Saturdays'];
function cronWords(c){const [mi,hr,dm,mo,dw]=String(c||'').split(' ');if(!/^\d+$/.test(mi||'')||!/^\d+$/.test(hr||''))return c;const at=`${hr.padStart(2,'0')}:${mi.padStart(2,'0')}`;
 if(dm==='*'&&mo==='*'&&(dw==='*'||dw==='0,1,2,3,4,5,6'||dw==='0-6'))return `Every day at ${at}`;if(dm==='*'&&mo==='*'&&(dw==='1,2,3,4,5'||dw==='1-5'))return `Weekdays at ${at}`;if(dm==='*'&&mo==='*'&&/^[0-6](,[0-6])*$/.test(dw))return `${dw.split(',').map(d=>DOW[+d]).join(', ')} at ${at}`;
 if(dm==='*'&&mo==='*'&&dw==='1-5')return `Weekdays at ${at}`;if(/^\d+$/.test(dm)&&mo==='*'&&dw==='*')return `Day ${dm} of each month at ${at}`;return c}
function trigWords(t){const m=t.measure&&(D.measures.find(x=>x.id===t.measure)?.name||t.measure);
 const per=String(t.period||'calendar.month').split('.').pop();
 if(t.per){const T=otype(t.per)||{name:'object',plural:'objects'},w=trigWords({...t,per:null});return `For each ${low(T)}: ${w.charAt(0).toLowerCase()+w.slice(1)} · Send to: ${toWords(T,t.recipients)}`}
 return t.type==='schedule'?cronWords(t.cron)+(t.timezone?` (${t.timezone})`:''):t.type==='data_condition'&&t.change?(Number(t.value)<0&&/</.test(t.op)?`When ${m} falls ${-Number(t.value)}% or more on the ${per} before`:Number(t.value)>0&&/>/.test(t.op)?`When ${m} rises ${t.value}% or more on the ${per} before`:`When ${m}'s change ${opw(t.op)} ${t.value}%`)
  :t.type==='data_condition'?`When ${m} ${opw(t.op)} ${fmt(Number(t.value))}`:t.type==='after_refresh'?`After ${t.table||'a table'} refreshes`:
  t.type==='data_quality'?'When data quality finds a problem':t.type?t.type.replace(/_/g,' '):'Run by hand'}
function flows(box){newBtn(box,'flow');const g=h('div','cards');box.append(g);if(!D.flows.length)box.append(emptyBox('flows',MODE==='viewer'?'None in the published version':'No flows yet',MODE==='viewer'?vEmpty('Flows run in the working project; what they post reaches your Inbox.'):'A flow runs steps on a schedule or a trigger: refresh a table, export a report, post to the Inbox.',MODE==='developer'?['New flow',()=>$('main .bar.top .btn.pri')?.click()]:null));
 D.flows.forEach(f=>{const t=f.trigger||{},c=card(f.name||f.id);{const b=h('span','state'+(f.enabled?' on':''),f.enabled?'On':'Off');c.querySelector('h3').append(b)}
  {const w=h('div','when');w.append(ic(t.type==='schedule'?'history':'monitor'),h('span','',trigWords(t)));w.title=`flows/${f.id}.json`+(t.cron?` · ${t.cron}`:'');c.append(w)}
  const st=h('div','stepchips');(f.steps||[]).forEach((s,i)=>{if(i)st.append(h('span','arr','→'));const [w,i2]=stepWords(s),cp=h('span','chip');cp.append(ic(i2),w);st.append(cp)});const tr=h('ol','trace');c.append(st,tr);
  act(c,'Run now',async m=>{tr.innerHTML='';const r=await api('run_flow',{flow:f.id});say(m,`run started: ${r.run}`);
   for(let i=0;i<150;i++){await wait(2000);const x=await api('flow_runs',{flow:f.id,run:r.run}),one=x.runs?.[0]||x;tr.innerHTML='';
    (one.steps||[]).forEach(s=>tr.append(h('li',s.status==='ok'?'ok':s.status==='failed'?'bad':'',`${s.step||s.id}: ${s.status}${s.error?' · '+s.error:''}`)));
    if(['ok','failed','stopped','rejected','waiting'].includes(one.status))return `Run ${one.status==='ok'?'finished':one.status}.`}return 'Still running: see the flow\'s runs on its canvas.'});
  {const t0=c.querySelector('h3>span');const a=h('button','tlink',t0.textContent);a.title='Open its canvas';a.onclick=()=>show('flw:'+f.id);t0.replaceChildren(a)}
  act(c,f.enabled?'Disable':'Enable',async()=>{await api('enable_flow',{flow:f.id,enabled:!f.enabled});f.enabled=!f.enabled;show('flows')});editBtns(c,'flow',f);g.append(c)})}
function ml(box){newBtn(box,'ml');const g=h('div','cards');box.append(g);if(!D.ml.length)box.append(emptyBox('ml','No ML models yet',MODE==='viewer'?vEmpty('Forecasts, segments and anomaly models are set up by the project\'s builders.'):'Forecast a measure, group similar rows or find unusual days. A model learns from a query of your measures.',MODE==='developer'?['New ML model',()=>$('main .bar.top .btn.pri')?.click()]:null));
 D.ml.forEach(m=>{const c=card(m.name||m.id,`ml/${m.id}.json`);{const T={forecast:'Forecasts',regression:'Predicts',classification:'Predicts',clustering:'Groups similar rows',anomaly:'Finds unusual periods'}[m.task]||m.task,dn=(D.datasets||[]).find(x=>x.id===m.dataset)?.name||m.dataset;
  c.append(h('p','mtext',`${T}${m.target?' '+mname(m.target):''}${m.horizon?`, ${m.horizon} periods ahead`:''}, learning from the query ${dn}.`))}
  const st=h('div','kv');st.style.marginTop='12px';const lr=()=>{const r=m.latest_run||{};st.innerHTML='';
   if(!r.kind){st.append(h('b','','Latest run'),h('span','',MODE==='viewer'?'None in the published copy yet':'Not trained yet: press Train'));return}
   [['Latest run',`${{train:'Trained',score:'Scored'}[r.kind]||r.kind}${r.finished?' · '+cell(r.finished):''}`],['Status',r.status||'—'],...Object.entries(r.metrics||{}).map(([a,b])=>{const M={mae:['MAE (mean absolute error)'],wape:['WAPE (weighted % error)',{style:'percent',decimals:1}],mape:['MAPE (mean % error)',{style:'percent',decimals:1}],rmse:['RMSE (root mean squared error)'],r2:['R² (fit)'],accuracy:['Accuracy',{style:'percent',decimals:1}],auc:['AUC'],windows:['Backtest windows']}[a]||[a.replace(/_/g,' ')];return [M[0],typeof b==='number'?fmt(b,M[1]):cell(b)]})].forEach(([a,b])=>st.append(h('b','',a),h('span',a==='Status'?(b==='ok'?'ok':b==='failed'?'bad':''):'',a==='Status'&&b==='ok'?'OK':String(b))))};lr();c.append(st);
  // a forecast: its actuals by month, and once scored, the forecast beside them (both ABI's measures)
  if(m.task==='forecast'&&m.target&&['calendar.year','calendar.month'].every(x=>D.dims.includes(x))){const fm=D.measures.find(x=>(x.description||'').includes(`of ML model '${m.id}'`))?.id,ch=h('div','mlchart');
   c.append(h('div','file',fm?`${mname(m.target)} and its forecast, by month`:MODE==='viewer'?`${mname(m.target)} by month. The forecast shows here once a builder scores the model.`:`${mname(m.target)} by month. Score the model to draw its forecast beside it.`),ch);
   api('semantic_query',{measures:[m.target,...(fm?[fm]:[])],dimensions:['calendar.year','calendar.month'],order_by:[{field:'calendar.year'},{field:'calendar.month'}],limit:240}).then(d=>{d.format=d.format||{};d.type='line';d.id='ml';requestAnimationFrame(()=>chart(ch,d))}).catch(e=>ch.append(h('p','bad',e.message)))}
  const follow=async(kind,m2)=>{await api(kind,{model:m.id});say(m2,`${kind==='train_model'?'training':'scoring'}…`);
   for(let i=0;i<60;i++){await wait(3000);const x=(await api('ml_models',{})).models.find(y=>y.id===m.id);if(x){Object.assign(m,x);lr()}
    if(['ok','failed'].includes(m.latest_run?.status)&&m.latest_run?.finished)return `${m.latest_run.kind}: ${m.latest_run.status}`}return 'still running'};
  act(c,'Train',m2=>follow('train_model',m2),1);act(c,'Score',m2=>follow('score_model',m2));editBtns(c,'ml',m);g.append(c)})}
// 1.2.6 M12: Plans. ABI works out the best plan over what this person sees (run_optimizer, in the worker) and stores it as
// changes waiting for approval (Data > Edits); the page never applies one. Business names only: no id, table or column.
function plans(box){const g=h('div','cards');box.append(g);
 D.optimizers.forEach((o,i)=>{const T=otype(o.over.type),c=card(o.name||o.id),out=h('div','plan');c.classList.add('wide');
  c.append(h('p','mtext',`Decides ${o.decide.name} for each ${T?low(T):'one'} so that ${o.objective.name||'the total'} is as ${o.objective.goal==='maximize'?'high':'low'} as it can be${(o.limits||[]).length?', within these limits: '+o.limits.map(l=>l.name).join(', '):''}.`),out);
  const draw=()=>{const r=o.latest_run;out.innerHTML='';
   if(!r){out.append(h('p','empty',MODE==='developer'?'Not worked out yet.':'No plan to show yet. A plan is worked out in Developer: ask one of the project\'s builders for it.'));return}
   if(['queued','running'].includes(r.status)){out.append(h('p','file','Working it out…'));return}
   if(r.status==='failed'){out.append(h('p','bad',r.error||'It could not be worked out.'));return}
   out.append(h('p','txt',r.text||''));const rows=r.rows||[],objs=rows.filter(x=>x[5]&&x[0]!=='Limit in conflict');  // an object's own two bounds in conflict are in the sentence, not objects of a plan
   // a limit the rounded plan went past first, then the ones that bind
   const near=(a,b)=>Math.abs(a-b)<=1e-6*Math.max(1,Math.abs(b)),past=x=>x[2]!==null&&x[4]!==null&&!near(x[2],x[4])&&(/^At least/.test(x[0])?x[2]<x[4]:x[0]==='Exactly'||x[2]>x[4]),rank=x=>past(x)?2:/used|met/.test(x[0])?1:0;
   const lims=['best','found'].includes(r.outcome)?rows.filter(x=>/^(Limit|At least|Exactly)/.test(x[0])).sort((a,b)=>rank(b)-rank(a)):[];
   lims.slice(0,8).forEach(x=>out.append(h('div','file',/^At least/.test(x[0])?`${x[1]}: at least ${fmt(x[4])}: ${fmt(x[2])}${/just met/.test(x[0])?' (just met)':''}`:
    x[0]==='Exactly'?`${x[1]}: exactly ${fmt(x[4])}`:`${x[1]}: ${fmt(x[2])}${x[4]===null?'':' of '+fmt(x[4])}${x[0]==='Limit fully used'?' (fully used)':''}`)));
   if(lims.length>8)out.append(h('div','file',lims.slice(8).some(past)?'More limits are not shown here; rounding took the plan slightly past some of them.':'The other limits hold too.'));
   if(objs.length){const tb=h('table'),hd=tb.createTHead().insertRow(),bd=tb.createTBody();[T?T.name:'Name','Now',o.decide.name,''].forEach(t=>hd.append(h('th','',t)));
    objs.slice(0,200).forEach(x=>{const tr=bd.insertRow(),nm=h('button','flink oopen',String(x[1]));nm.onclick=()=>openObj(r.type,x[5]);tr.insertCell().append(nm);[x[3]===null?'—':fmt(x[3]),fmt(x[2]),x[4]||''].forEach(v=>tr.insertCell().textContent=v)});
    const sc=h('div','scroll');sc.append(tb);out.append(sc);if(objs.length>200)out.append(h('p','file','The first 200 are shown.'))}
   // the changes wait under Edits until a person decides: once approved or rejected there is nothing left to review
   if(r.proposal&&MODE==='developer'&&PAGES.edits)api('edit_proposals',{}).then(p=>{if(o.latest_run===r&&out.isConnected&&(p.proposals||[]).some(q=>q.batch===r.proposal.batch))out.append(btn('Review the changes',()=>show('edits')))}).catch(()=>{})};
  draw();
  if(MODE==='developer')act(c,'Find the best plan',async m=>{await api('run_optimizer',{optimizer:o.id});say(m,'Working it out…');
   for(let k=0;k<330;k++){await wait(2000);const x=(await api('optimizers',{})).optimizers.find(y=>y.id===o.id);if(x){o.latest_run=x.latest_run;draw()}
    if(['ok','failed'].includes(o.latest_run?.status))return o.latest_run.status!=='ok'?'It could not be worked out.':['best','found'].includes(o.latest_run.outcome)?'Done.':'No plan this time: the reason is above.'}return 'Still working: open this page again in a moment.'},!i);
  g.append(c)})}
function tools(box){newBtn(box,'tool');const g=h('div','cards');box.append(g);if(!D.tools.length)box.append(emptyBox('tools','No tools yet',MODE==='viewer'?vEmpty('A tool is a calculation the agents can call. The project\'s builders set them up.'):'A tool is a calculation your agents can call with inputs, such as store KPIs for a month. It has tests and is published when they pass.',MODE==='developer'?['New tool',()=>$('main .bar.top .btn.pri')?.click()]:['Ask the agent',()=>openChat(true)]));
 D.tools.forEach(t=>{const c=card(t.name||t.id,`tools/${t.id}.json · ${t.status||''}`);c.append(h('div','',t.description||''));const ins={};
  Object.entries(t.inputs?.properties||{}).forEach(([k,s])=>{const i=h('input','f');i.placeholder=s.description||'';ins[k]=i;c.append(h('label','l',k),i)});
  const out=h('div');
  act(c,'Run',async()=>{rows(out,await api('run_tool',{tool:t.id,inputs:Object.fromEntries(Object.entries(ins).filter(([,i])=>i.value).map(([k,i])=>[k,i.value]))}))},1);
  act(c,'Run its tests',async()=>{rows(out,await api('test_tool',{tool:t.id}))});editBtns(c,'tool',t);c.append(out);g.append(c)})}
function agents(box){newBtn(box,'agent');const g=h('div','cards');box.append(g);if(!D.agents.length)box.append(emptyBox('agents','No custom agents yet',MODE==='viewer'?vEmpty('Custom agents answer one kind of question, set up by the builders. Until then, ask the main agent.'):'An agent answers one kind of question with your measures and tools, for example a store manager assistant. It must pass its evaluation set before it is published.',MODE==='developer'?['New agent',()=>$('main .bar.top .btn.pri')?.click()]:['Ask the agent',()=>openChat(true)]));
 D.agents.forEach(a=>{const c=card(a.name||a.id,`agents/${a.id}.json · ${a.status||''}`);c.append(h('div','file',(a.purpose||a.instructions||'').slice(0,240)));
  const q=h('input','f');q.placeholder='e.g. revenue in North?';const out=h('div','msg');c.append(h('label','l','Question'),q);
  act(c,'Ask',async()=>{const r=await api('run_agent',{agent:a.id,question:q.value});out.textContent=r.answer||JSON.stringify(r,null,1)},1);
  act(c,'Evaluation set',()=>evalSet(c,a));
  act(c,'Run evaluation set',async()=>{const r=await api('test_agent',{agent:a.id});out.textContent=(r.cases||[]).map(x=>`${x.passed?'✓':'✗'} ${x.question}${x.missing?.length?' · missing '+x.missing.join(', '):''}`).join('\n')||JSON.stringify(r,null,1)});editBtns(c,'agent',a);c.append(out);g.append(c)})}

// ---------- Govern ----------
function docs(box){if(D.docs==null){const sk=h('div','skel');sk.append(h('div','sk w40'),h('div','sk'),h('div','sk'),h('div','sk w70'));box.append(h('p','file','Reading the documentation from ABI…'),sk);  // asked when shown (the page's data has none: M3.3)
  api('docs',{}).then(r=>{D.docs=r||{};if(CUR==='docs')show('docs')},e=>{box.textContent=e.message});return}
 const d=D.docs?.projects?.[0];if(!d){box.append(h('p','empty','No documentation yet.'));return}
 const bar=h('div','row bar top'),toc=h('nav','toc');toc.setAttribute('aria-label','Sections');box.append(bar,toc);const secs=[];
 const VHIDE=/^(Architecture|Data sources|Tools|Agents|Security|Operations runbook|Open issues)$/i;(d.sections||[]).filter(s=>MODE!=='viewer'||!VHIDE.test(s.title||'')).forEach((s,i)=>{const c=h('details','card dsec'),su=h('summary');su.append(h('span','dn',String(i+1)),h('span','dt',s.title),h('span','file',`${(s.items||[]).length} item${(s.items||[]).length===1?'':'s'}`));c.append(su);c.open=!MOBILE&&i===0;c.id='doc-'+i;
  (s.items||[]).forEach(it=>{c.append(h('h4','',it.title));const t=h('table','facts');
  (it.facts||[]).filter(([k])=>MODE!=='viewer'||!/^Where to look$/i.test(k)).forEach(([k,v])=>{const r=t.insertRow();r.insertCell().textContent=k;const tv=plur(idsToNames(typeof v==='string'?v:JSON.stringify(v))),cl=r.insertCell();if(/ \| /.test(tv)){const ul=h('ul','list');tv.split(/ \| /).forEach(x=>ul.append(h('li','',x.replace(/^\d+\.\s*/,''))));cl.append(ul)}else cl.textContent=tv});c.append(t)});box.append(c);secs.push(c);
  const go=h('button','chip',s.title);go.onclick=()=>{secs.forEach(x=>x.open=x===c);c.scrollIntoView({behavior:'smooth',block:'start'})};toc.append(go)});
 {const all=h('button','flink','Open all');all.onclick=()=>{const o=secs.some(x=>!x.open);secs.forEach(x=>x.open=o);all.textContent=o?'Fold all':'Open all'};toc.append(all)}
 bar.append(btn('Download PDF',async()=>{try{await pdf();toast('Downloaded the documentation PDF.')}catch(e){toast(e.message,1)}},1),(()=>{const b=btn('Regenerate',async()=>{D.docs=await api('docs',{});show('docs')});b.classList.add('w');b.title='Write the documentation again from the project\'s definitions';return b})());
 const pdf=async()=>{const r=await fetch('/docs/pdf',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:'{}'});
  if(!r.ok)throw new Error('The PDF could not be made: '+((await r.json().catch(()=>({}))).error||r.status));const a=h('a');a.href=URL.createObjectURL(await r.blob());a.download=D.project+'-documentation.pdf';a.click()}}
const VERB={add_data:'Loaded a source',refresh:'Reloaded a source',delete_source:'Removed a source',set_steps:'Changed a table\'s steps',set_table:'Changed a table',delete_table:'Deleted a table',build_model:'Built the model',
 add_relationship:'Joined two tables',remove_relationship:'Removed a join',set_measure:'Saved a measure',delete_measure:'Deleted a measure',set_glossary_term:'Saved a glossary term',delete_glossary_term:'Deleted a glossary term',
 save_dataset:'Saved a query',delete_dataset:'Deleted a query',save_model:'Saved a data model',delete_model:'Deleted a data model',save_report:'Saved a report',delete_report:'Deleted a report',save_flow:'Saved a flow',delete_flow:'Deleted a flow',
 enable_flow:'Switched a flow on or off',save_ml_model:'Saved an ML model',delete_ml_model:'Deleted an ML model',save_tool:'Saved a tool',delete_tool:'Deleted a tool',publish_tool:'Published a tool',save_agent:'Saved an agent',delete_agent:'Deleted an agent',
 publish_agent:'Published an agent',set_role:'Changed a role',set_rls_rule:'Saved a row rule',set_column_rule:'Saved a column rule',set_auto_commit:'Changed a project setting',import_project:'Imported the project',revert:'Went back to a version',edit_cells:'Edited rows'};
// a commit message in words: [what, where] ('Loaded a source', 'bronze_trial'); a person's own message stays theirs
const andj=a=>a.length>1?a.slice(0,-1).join(', ')+' and '+a.at(-1):a[0]||'';
const VIC=w=>([[/source/i,'sources'],[/steps|table/i,'tables'],[/join|the model/i,'model'],[/measure/i,'measures'],[/glossary/i,'glossary'],[/query/i,'datasets'],[/data model|ML/,'ml'],[/report/i,'reports'],[/flow/i,'flows'],[/tool/i,'tools'],[/agent/i,'agents'],[/role/i,'people'],[/rule/i,'security'],[/setting/i,'settings'],[/import/i,'transfer'],[/version/i,'history'],[/rows/i,'edits']].find(([r])=>r.test(w))||[0,'commit'])[1];
function plainCommit(m){const ps=String(m||'').split(/\n+|(?<=\.json)(?=[a-z_]+: )/).map(s=>s.trim()).filter(Boolean),one=ps.map(p=>{const x=/^(\w+): (.+)$/.exec(p);if(!x||!VERB[x[1]])return null;
  return [VERB[x[1]],x[2].split(/,\s*/).map(q=>{const r=plainFile('M',q);return idsToNames(r[1]||r[0])}).join(', ')]});
 if(ps.length&&one.every(Boolean)){const w=[...new Set(one.map(x=>x[0]))];return [andj(w.map((x,i)=>i?x[0].toLowerCase()+x.slice(1):x)),andj([...new Set(one.map(x=>x[1]).join(', ').split(', ').filter(Boolean))])||'—',VIC(w[0])]}const p0=String(ps[0]||'').replace(/\bSecuritys rls\b/g,'Row rules').replace(/\brls\b/g,'row rules'),gen=/^[^;]+ (changed|added|deleted)(; [^;]+ (changed|added|deleted))+$/.test(p0);  // the app's own older commit text, joined in words
 const sn=x=>idsToNames(x).replace(/\b([a-z]+_[a-z0-9_]+)\b/g,m=>'“'+m.replace(/_/g,' ').replace(/^./,c=>c.toUpperCase())+'”');
 if(gen){const parts=p0.split('; ').map(sn),subj=parts.map((x,i)=>x.replace(/ changed$/,'').replace(/^./,c=>i?c.toLowerCase():c));return [parts[0]+(parts.length>1?` +${parts.length-1} more`:''),andj(subj),VIC(p0)]}
 return [idsToNames(p0),ps.length>1?`and ${ps.length-1} more`:'—','commit']}
function commits(box){const c=card('Commits','every change is a commit in ABI\'s Git history');if(MODE==='viewer')c.append(h('p','file','The published version\'s history: each publish brings the builders\' commits since the last one.'));const t=h(MOBILE?'div':'table','hist');c.append(t);box.append(c);
 const when=x=>when2(new Date(x.time*1000).toISOString()),who=x=>x.author+(x.by==='agent'?' (agent)':'');
 const fill=()=>{t.innerHTML='';if(!(D.history||[]).length){t.append(h('p','empty','No commits yet.'));return}
  if(MOBILE){let last=null;(D.history||[]).forEach(x=>{const [w,f]=plainCommit(x.message),k=[w,f,who(x)].join('|');if(last&&last.k===k){last.n++;last.c.textContent=`${last.n} times`;return}
   const r=h('div','srow'),n=h('div','sname'),c=h('span','cnt');n.append(h('b','',w),h('div','file',[f,who(x),when(x)].filter(Boolean).join(' · ')));r.append(n,c);r.title=x.message;t.append(r);last={k,t:x.time,n:1,c}});return}
  const hd=t.createTHead().insertRow();['What','Where','Who','When','Version'].forEach(x=>hd.append(h('th','',x)));const tb=t.createTBody();
  // a run of the same change (17 measures saved by the seed) is one row with its count
  // the same change by the same person is one row with its count, newest first (the seed's runs, a measure saved 17 times)
  const byK={};let last=null;(D.history||[]).forEach(x=>{const [w,f,vi]=plainCommit(x.message),k=[w,f,who(x)].join('|');last=byK[k];if(last){last.n++;last.c.textContent=`${last.n} times`;last.v.textContent=`${last.id} +${last.n-1}`;last.v.title='and '+(last.n-1)+' more like it';return}
   const r=tb.insertRow();[w,f,who(x),when(x),x.id.slice(0,7)].forEach((v,i)=>{const td=r.insertCell();td.textContent=v;if(i===4)td.className='code'});r.title=x.message;r.cells[0].prepend(ic(vi||'commit'));const c=h('span','cnt');r.cells[0].append(c);byK[k]={k,t:x.time,n:1,c,v:r.cells[4],id:x.id.slice(0,7)}})};fill();
 act(c,'Reload',async()=>{D.history=(await api('history',{})).commits;fill()})}

function security(box){const g=h('div','cards top');box.append(g);const r=D.rls||{},acc=r.accounts||[],R=x=>x?x[0].toUpperCase()+x.slice(1):'No access';
 const c=card('Row-level rules','who sees which rows');
 if(r.restricted){(r.rules||[]).forEach(x=>{const p=h('p','mtext');p.append(`You see the rows where ${colName(x.column)} (${x.table}) is `,h('b','',(x.values||[]).join(', ')||'nothing'),'.');c.append(p)});
  if(!(r.rules||[]).length)c.append(h('p','empty','No row rule applies to you: you see every row of the published project.'))}
 else{if(!(r.rules||[]).length)c.append(h('p','empty','None yet: everyone with access sees every row.'));
  (r.rules||[]).forEach(x=>{const k=h('div','rule');k.append(h('h4','',`${colName(x.column)} · ${x.table}`));
   (Array.isArray(x.users)?x.users:Object.entries(x.users||{}).map(([id,values])=>({id,name:(acc.find(a=>String(a.id)===id)||{}).name||'A person',values}))).forEach(u=>{const row=h('div','srow'),n=h('div','sname');n.append(h('b','',u.name||'A person'));const vs=h('div','stepchips');(u.values||[]).forEach(v=>vs.append(h('span','chip',String(v))));if(!(u.values||[]).length)vs.append(h('span','file','no rows'));row.append(n,vs);k.append(row)});c.append(k)})}
 g.append(c);
 const cr=card('Hidden columns','hidden from Viewers');const cols=r.columns||[];if(!cols.length)cr.append(h('p','empty','None: Viewers see every column of the tables they may read.'));cols.forEach(x=>cr.append(h('span','chip',typeof x==='string'?x:`${x.table}: ${(x.columns||[]).join(', ')}`)));g.append(cr);
 const ro=card('Roles','set in Users & access');if(acc.length&&MODE!=='viewer'){acc.forEach(a=>{const row=h('div','srow'),n=h('div','sname');n.append(h('b','',a.name));row.append(n,h('span','state',R(a.role)));ro.append(row)})}
 else ro.append(h('p','empty',MODE==='viewer'&&acc.length?'Who has which role is in Users & access, on a computer.':'Only owners see who has which role: Users & access lists them.'));g.append(ro);
 // a row rule as a form: the person, the column, the members they may see (picked from the data); ABI confirms it
 if(MODE==='developer'&&acc.length){const rr=card('Restrict rows for a person','ABI asks you to confirm, then the rule applies to every page, report and answer');rr.classList.add('wide');
  const ppl=acc.filter(a=>a.role!=='owner'),pw=sel(ppl.map(a=>[String(a.id),a.name])),RANK=['region','country','segment','categor','team','state','city'],rk=x=>{const i=RANK.findIndex(w=>x.includes(w));return i<0?99:i},
   col=sel(D.dims.filter(d=>!d.startsWith('calendar.')).sort((a,b)=>rk(a)-rk(b)||a.localeCompare(b)).map(d=>[d,`${colName(d)} · ${d.split('.')[0]}`])),pick=h('div','mpick'),sum=h('p','sum');let picked=new Set();
  const ex=()=>(r.rules||[]).find(x=>`${x.table}.${x.column}`===col.value),users=x=>Array.isArray(x?.users)?x.users:Object.entries(x?.users||{}).map(([id,values])=>({id,values}));
  const upd=()=>{const who=pw.options[pw.selectedIndex]?.text||'';sum.textContent=picked.size?`${who} will see only the rows where ${colName(col.value)} is ${[...picked].join(', ')}.`:`Pick what ${who} may see.`};
  const load=async()=>{pick.replaceChildren(h('span','file','Loading the values…'));picked=new Set((users(ex()).find(u=>String(u.id)===pw.value)||{}).values||[]);
   try{const q=await api('semantic_query',{measures:[D.measures[0]?.id],dimensions:[col.value],order_by:[{field:col.value}],limit:200});pick.replaceChildren();
    q.rows.forEach(x=>{const v=x[0];if(v==null)return;const b=h('button','sl'+(picked.has(v)?' on':''));b.type='button';b.append(h('span','',String(v)));b.setAttribute('aria-pressed',picked.has(v));
     b.onclick=()=>{picked.has(v)?picked.delete(v):picked.add(v);b.classList.toggle('on',picked.has(v));b.setAttribute('aria-pressed',picked.has(v));upd()};pick.append(b)})}catch(e){pick.replaceChildren(h('p','bad',e.message))}upd()};
  pw.onchange=col.onchange=load;const fr=h('div','formrow two-col');fr.append(fld('Person',pw),fld('Only the rows where',col));rr.append(fr,lab('is one of'),pick,sum);load();
  act(rr,'Save rule',async()=>{need(pick,picked.size,'Pick at least one value they may see.');const x=ex(),[table,column]=col.value.split('.'),u=Object.fromEntries(users(x).map(y=>[String(y.id),y.values]));u[pw.value]=[...picked];
   await api('set_rls_rule',{id:x?.id||slug(column+'_rows','rule'),table,column,users:u});D.rls=await api('rls_rules',{}).catch(()=>D.rls);toast(`Saved: ${pw.options[pw.selectedIndex].text} now sees only ${[...picked].join(', ')}.`);show('security');setTimeout(()=>highlight(colName(column)),300)},1);g.after(rr)}}

// ---------- Knowledge: the vault ----------
async function vpost(op,body){const r=await fetch('/vault/'+op,{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify(body||{})}).then(r=>r.json());
 if(r.error)throw new Error(r.error);return r.result}
async function loadVault(){try{VAULT=await vpost('list')}catch(e){VAULT=[]}}
function vaultPage(box){
 const bar=h('div','row bar top'),file=h('input');file.type='file';file.multiple=true;file.accept='.md,.txt,.html,.htm,.docx,.pdf';file.hidden=true;file.id='vault-file';
 bar.append(file,btn('Add document',()=>file.click(),1));box.append(bar);
 file.onchange=async()=>{for(const f of file.files){const data=await new Promise(r=>{const fr=new FileReader();fr.onload=()=>r(fr.result.split(',')[1]);fr.readAsDataURL(f)});
  try{const r=await vpost('add',{name:f.name,data});toast(`Added ${r.name}: ${r.chunks} passages`)}catch(e){toast(e.message)}}file.value=''};
 const sc=card('Search the vault','the passages the agents read'),q=h('input','f');q.placeholder=MOBILE?'e.g. net revenue':'e.g. how is net revenue defined?';q.id='vault-q';q.setAttribute('aria-label','Search the vault');
 const out=h('div','hits'),qr=h('div','row qrow');qr.append(q);sc.append(qr,out);box.append(sc);
 const go=async()=>{out.innerHTML='';if(!q.value.trim())return;const hits=await vpost('search',{query:q.value,k:6});if(!hits.length){out.append(h('p','empty','No passage matches.'));return}
  hits.forEach(x=>{const d=h('div','hit');d.append(h('div','file',`${x.document}${x.section?' · '+x.section:''}`),h('div','',x.text));out.append(d)})};
 q.onkeydown=e=>{if(e.key==='Enter')go()};{const b=btn('Search',go,1);b.prepend(ic('search'));qr.append(b)}
 const g=h('div','cards vault');box.append(g);if(!VAULT.length)box.append(h('p','empty','No documents yet. Add the definitions your measures must follow (.md, .txt, .html, .docx).'));
 VAULT.forEach(d=>{const c=card(d.name,`${Math.max(1,Math.round(d.bytes/1024))} KB · ${d.chunks} passages`);
  if(d.sections.length){const k=h('div');d.sections.forEach(x=>k.append(h('span','chip',x)));c.append(k)}
  const pre=h('pre');pre.hidden=true;c.append(pre);
  act(c,'Read',async()=>{pre.hidden=!pre.hidden;if(!pre.textContent)pre.textContent=await vpost('read',{name:d.name})});
  act(c,'Remove',async()=>{if(await ask({message:`Remove ${d.name} from the vault?`,preview:''})){await vpost('rm',{name:d.name})}});g.append(c)})}

// ---------- Knowledge: the agent team ----------
// ---------- sidebar ----------
const PENDING={filters:null,explore:null,share:null,open:null,added:null,osearch:null};
const LEDE={vault:'Your business definitions, finance rules and KPI specs. The agents check them before they define a measure.',home:'Start here: ask anything, see what needs attention, and every part of the project at a glance.',datasets:'Saved questions to the model: measures by dimensions, with filters. Reports, flows and ML reuse them.',
 measures:'The business numbers. Edit a formula and ABI checks it before it saves; Test shows the total.',model:'Your data models: the whole model and each named model. Open one to see its tables and how they join.',
 chat:'Ask a question, ask where something is, or ask for a change. The agent can open pages for you.',boardpack:'The documents, data and video a review needs, made by the board-pack agent and checked first.',documents:'A report, some of its pages or one view, as ABI renders it: PDF, PNG or PowerPoint.',onepager:'One page for the board: headline numbers, two charts and the caveats, built by the agent.',dataexport:'The rows behind any view, as CSV or Excel, or a query\'s rows as CSV.',video:'A silent explainer of report views, animated from ABI\'s numbers, one caption per slide.',settings:'The agent\'s model and effort, and the providers it may use. Keys are stored encrypted.',people:'Who may sign in, as Viewer, Editor or Owner, and which sections and reports each person sees.',transfer:'The whole project as one zip, to keep, move or rebuild. Imports are checked and undoable.',edits:'Corrections and new rows on the gold tables, kept on top of the loaded data and undoable.',reports:'The saved reports of this project. Open one to filter it and click through.',monitor:'Is anything wrong right now: data freshness, model readiness, ML runs and flows.',inbox:'Alerts and posts from Monitor and flows, with their figures, and the project\'s own alerts.',explore:'Your own analysis on the governed model: the numbers match the reports. Keep the ones you want.',glossary:'What the business words mean and which measures implement them.',sources:'Bronze: the data as it landed, one source per file or bucket folder. Reload, add or delete a source.',
 staged:'Silver: cleaned and combined tables between the sources and the final tables, hidden from the model.',tables:'Gold: the final business tables the model and measures are built on. Edit kind, key, description and steps.',flows:'Automations. Run one now and follow its steps, or switch it on or off.',
 ml:'Forecasts, segments and anomaly models. Train, score and read the latest run.',plans:'The best plan within your limits: hours, units or slots. Working one out changes nothing: it waits for approval under Edits.',tools:'Calculations your agents can call. Run one with inputs or run its tests.',
 agents:'Custom agents for this project. Ask one a question or run its evaluation set.',docs:'The project documentation ABI writes from its definitions.',
 objects:'The things the business works with (a café, a customer): what each one is, its numbers, what it connects to and what you can do with it.',
 security:'Who sees which rows. Roles are set in ABI itself.',environments:'Where each environment reads its data from.',history:'Every change is a commit in ABI. Newest first.'};let PAGES={},CUR='home';const side=$('aside');
// what a page is for, said to a Viewer (who reads, and does not build)
const VLEDE={objects:'The things the business works with (a café, a customer): what each one is, its numbers and what it connects to.',sources:'Bronze: the data as it landed, one source per file or bucket folder, and when each last loaded.',staged:'Silver: the cleaned and combined tables between the sources and the final tables.',
 tables:'Gold: the final business tables the model and measures are built on.',measures:'The business numbers and how each one is calculated.',flows:'Automations and their last runs.',
 ml:'Forecasts, segments and anomaly models, with their latest runs.',plans:'The best plan within the limits, as last worked out for what you see.',tools:'Calculations your agents can call.',agents:'Custom agents for this project: ask one a question.',
 edits:'Corrections to the gold tables, applied on top of the loaded data; every query sees them.',glossary:'What the business words mean and which measures implement them.',
 datasets:'Saved questions to the model (measures by dimensions, with filters): each run returns a fresh table.',model:'The data models: open one to see its tables and how they join.'};
function nav(){const all=navAll(),me=window.ME||{admin:true};return all.filter(([g,items])=>items.length&&(g==='Start'||!me.sections||me.sections.includes(g)))}
function navAll(){return [['Start',[['home','Home',home],['chat','Chat',chatPage]]],
 ['Data',[...(MODE==='viewer'?[]:[['sources','Sources',sources,D.sources.length],['staged','Staged',stagedPage,(D.layers||[]).filter(t=>t.layer==='silver').length]]),['tables','Tables',tablesPage,(D.model?Object.keys(D.model.tables||{}).length:D.tables.length)],...(MODE==='viewer'?[]:[['edits','Edits',editsPage]])]],
 ['Data Model',[['model','Data models',modelsPage,(D.models||[]).length+1],['objects','Objects',objectsHome,OT().length],['measures','Measures',measures,D.measures.length],
 ['glossary','Glossary',glossary,D.glossary.length],['datasets','Queries',datasets,D.datasets.length]]],
 ['Visualizations',[['reports','Reports',reportsPage,D.reports.length],['monitor','Monitor',monitorPage],['inbox','Inbox',inboxPage,INBOXN],['explore','Explore',explore,MINE.length||'']]],
 ['Export',[['boardpack','Board pack',boardPage,(D.app?.packs||[]).length],['documents','Documents',documentsPage],['onepager','One-pager',onePagerPage],['dataexport','Data',dataExportPage],['video','Video',videoPage],['transfer','Project',transferPage]]],
 ['Automate',[['flows','Flows',flows,D.flows.length],['ml','ML models',ml,D.ml.length],...(D.optimizers.length&&!COPY()?[['plans','Plans',plans,D.optimizers.length]]:[]),['tools','Tools',tools,D.tools.length],['agents','Agents',agents,D.agents.length]].filter(x=>MODE!=='viewer'||x[3])],
 ['Knowledge',[['vault','Vault',vaultPage,(VAULT||[]).length],['docs','Docs',docs]]],
 ['Govern',[['security','Security',security],['environments','Environments',b=>{const x=(D.sections.Environments||'').trim(),tmp=h('div');tmp.insertAdjacentHTML('beforeend',x);const has=tmp.innerText.trim().replace(/none yet\.?/i,'').length>10;if(has)b.insertAdjacentHTML('beforeend',x);else b.append(emptyBox('environments','No environments yet','A dev and prod pair lets you try a change on dev data before it reaches production. Each environment says where its sources read from.',MODE==='developer'?['Ask the agent',()=>{openChat(true);inp.value='Set up a dev and a prod environment for this project in ABI.';inp.focus()}]:null))}],['history','History',commits,(D.history||[]).length],...((window.ME||{admin:true}).admin&&!MOBILE?[['people','Users & access',usersPage],['settings','Settings',settingsPage]]:[])]]]}
let MODE='developer',VAULT=[],MODEL='';
let FOLD={};try{FOLD=JSON.parse(localStorage.getItem('abi-fold')||'{}')}catch(e){}
function sidebar(){side.innerHTML='';PAGES={};side.setAttribute('aria-label','Pages');{const g=h('button','goto');g.append(ic('search'),h('span','lb','Go to…'),h('kbd','',/Mac|iPhone|iPad/.test(navigator.platform)?'⌘K':'Ctrl K'));g.onclick=()=>goTo();side.append(g)}
 // a phone has no header crumb: the drawer opens with the workspace and project, which switches them
 if(MOBILE){const sw=$('#switch'),b=h('button','proj');b.append(ic('transfer'),h('span','lb',sw.textContent.replace('▾','').replace('/',' / ')),ic('down'));b.onclick=()=>{document.body.classList.remove('menu-open');sw.click()};side.append(b)}nav().forEach(([g,items])=>{const sec=h('div','sec'),hd=h('button','grp'),list=h('div','items');sec.dataset.g=g;
 hd.append(h('span','',g),h('span','car'));hd.setAttribute('aria-expanded',String(!FOLD[g]));sec.classList.toggle('folded',!!FOLD[g]);
 hd.onclick=()=>{FOLD[g]=!sec.classList.contains('folded');sec.classList.remove('auto');sec.classList.toggle('folded',FOLD[g]);hd.setAttribute('aria-expanded',String(!FOLD[g]));try{localStorage.setItem('abi-fold',JSON.stringify(FOLD))}catch(e){}};
 items.forEach(([k,label,fn,n])=>{PAGES[k]=[label,fn];const b=h('button','it');b.dataset.k=k;
  b.append(ic(k),h('span','lb',label));if(n!==undefined&&n!=='')b.append(h('span','n',String(n)));b.onclick=()=>show(k);list.append(b)});
 sec.append(hd,list);side.append(sec)});D.reports.forEach(r=>PAGES['rep:'+r.id]=[r.name||r.id,b=>report(b,r)]);PAGES['mdl:all']=['Whole model',b=>openModel(b,'')];(D.flows||[]).forEach(f=>PAGES['flw:'+f.id]=[f.name||f.id,b=>flowPage(b,f)]);
 (D.models||[]).forEach(m=>PAGES['mdl:'+m.id]=[m.name,b=>openModel(b,m.id)]);OT().forEach(t=>objReg('obj:'+t.id));
 // AGPL-3.0 §13: every page offers the running version's source (ABI_SOURCE_URL for a changed build)
 if(window.ME?.source){const a=h('a','src',`ABI-PI ${ME.version} · source code (AGPL-3.0)`);a.href=ME.source;a.target='_blank';a.rel='noopener';side.append(a)}}
// the nav opens at the top with the active page in view: when it is taller than the screen, the groups other than
// Start and the active one fold (not remembered; a group the person opened stays open)
function fitNav(act){side.querySelectorAll('.sec.auto').forEach(s=>s.classList.remove('folded','auto'));side.scrollTop=0;if(MOBILE||!act)return;
 requestAnimationFrame(()=>{if(side.scrollHeight<=side.clientHeight+2)return;const mine=act.closest('.sec');[...side.querySelectorAll('.sec')].reverse().forEach(s=>{if(side.scrollHeight<=side.clientHeight+2)return;
  const g=s.dataset.g;if(s===mine||g==='Start'||FOLD[g]===false||s.classList.contains('folded'))return;s.classList.add('folded','auto')});side.scrollTop=0})}
let PREV='home';const navKey=k=>k.startsWith('rep:')?'reports':k.startsWith('mdl:')?'model':k.startsWith('flw:')?'flows':k.startsWith('obj:')?'objects':k;  // a page's entry in the sidebar
function show(k){document.querySelectorAll('body>.toast:not([style])').forEach(t=>{if(Date.now()-(+t.dataset.t||0)>1500)t.remove()});if(k&&!PAGES[k]&&k.startsWith('obj:'))objReg(k);if(!PAGES[k]){if(k&&k!=='home')window.__redir=MOBILE?'That page opens on a computer, in Developer: a phone shows the published project.':MODE==='viewer'?'That page has nothing in the published version, so Home opened instead.':'That page is not here any more, so Home opened instead.';k='home'}if(k!=='chat'&&CUR!=='chat')PREV=k;else if(k==='chat'&&CUR!=='chat')PREV=CUR;{const cb=$('#chat');if(cb.classList.contains('full')){cb.classList.remove('full');wideT(0);document.body.append(cb);cb.classList.remove('open');document.body.classList.remove('chat-open')}}CUR=k;setTimeout(ctx);const sk=navKey(k);side.querySelectorAll('button.it').forEach(b=>{b.classList.toggle('on',b.dataset.k===sk);b.toggleAttribute('aria-current',b.dataset.k===sk)});
 const act=side.querySelector('button.it.on');if(act)act.closest('.sec').classList.remove('folded');fitNav(act);
 const box=$('main');box.innerHTML='';const pg=h('div','pg'),head=h('div','pghead'),pn=$('#pgname');pn.textContent=PAGES[k][0];document.title=PAGES[k][0]+' · '+D.project;
 pn.title=(k.startsWith('rep:')?'Filter the report or click a bar to filter by it. Every number comes from ABI.':LEDE[k])||'';
 // the page says what it is and what it is for, above its actions (the header's crumb says where it sits)
 {const tt=h('div','ttl'),rp=k.startsWith('rep:')&&D.reports.find(r=>'rep:'+r.id===k),sec=nav().find(([,its])=>its.some(([x])=>x===navKey(k)))?.[0];
  if(sec&&sec!=='Start')tt.append(h('div','eyebrow',sec));tt.append(h('h1','',PAGES[k][0]));const ld=rp?(rp.spec?.description||rp.description||(rp.pages.some(p=>p.visuals.some(v=>v.dim&&objCol(v.dim)))?'Filter it, or click a bar or a slice to filter by that member; Open, beside the filter, shows that member\'s own page. Every number comes from ABI.':'Filter it, or click a bar or a slice to filter by that member. Every number comes from ABI.')):(MODE==='viewer'&&VLEDE[k])||LEDE[k];
  if(ld)tt.append(h('p','',ld));head.append(tt)}
 {const h0=box.offsetHeight;if(h0)box.style.minHeight=h0+'px'}pg.append(head);if(window.__redir){const n=h('p','note');n.append(ic('info'),h('span','',window.__redir));pg.append(n);window.__redir=''}box.append(pg);{const r=PAGES[k][1](pg);if(r?.then){const sk=h('div','skel');sk.append(h('div','sk w40'),h('div','sk'),h('div','sk w70'));const tm=setTimeout(()=>pg.append(sk),350);r.finally(()=>{clearTimeout(tm);sk.remove();box.style.minHeight=''})}else requestAnimationFrame(()=>box.style.minHeight='')}setTimeout(()=>names(pg),60);new MutationObserver(()=>{clearTimeout(pg._nm);pg._nm=setTimeout(()=>names(pg),120)}).observe(pg,{childList:true,subtree:true});lay(pg,k);new MutationObserver(()=>lay(pg,k)).observe(pg,{childList:true,subtree:true});
 const top=pg.querySelector(':scope>.bar.top'),lead=pg.querySelector(':scope>.bar.lead');if(lead)lead.classList.contains('seg')?head.append(lead):head.querySelector('.ttl').prepend(lead);if(top)head.append(top);if(MODE==='viewer')pg.querySelectorAll('.ed').forEach(e=>e.readOnly=true);
 try{localStorage.setItem('abi-app-'+D.project,k)}catch(e){}try{window.history.replaceState(null,'',hashOf(k))}catch(e){}}
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
 c.draggable=false;r.setPointerCapture(e.pointerId);r.onpointermove=m=>{c.style.gridColumn='span '+Math.max(1,Math.min(12,Math.round((m.clientX-b.left+gap)/(col+gap))));c.style.height=Math.max(80,Math.round((m.clientY-b.top)/20)*20)+'px';if(c.style.gridRow)c.style.gridRow=`span ${Math.max(1,Math.round((parseInt(c.style.height)+16)/70))}`;c.classList.add('sized')};
 r.onpointerup=r.onpointercancel=()=>{r.onpointermove=r.onpointerup=r.onpointercancel=null;c.draggable=LAYING;layStore()}};return r}
async function layStore(reset){const k=CUR,pg=$('main .pg'),out={};
 grids(pg).forEach((g,gi)=>{if(!g.classList.contains('lay'))return;const o={};layKids(g).forEach((c,i)=>o[lkey(g,c,i)]={o:+c.style.order||0,w:+(c.style.gridColumn.match(/\d+/)||[4])[0],h:c.classList.contains('sized')?parseInt(c.style.height):0});out[gi]=o});
 if(k.startsWith('rep:')){const rep=D.reports.find(r=>'rep:'+r.id===k),g=pg.querySelector('.grid'),spec=structuredClone(rep.spec),sp=spec.pages.find(p=>p.id===g.dataset.page),vp=rep.pages.find(p=>p.id===g.dataset.page);
  let x=0,y=0,rh=0;layKids(g).sort((a,b)=>a.style.order-b.style.order).forEach(c=>{const v=sp.visuals.find(v=>v.id===c.dataset.lk);if(!v)return;const w=+(c.style.gridColumn.match(/\d+/)||[6])[0],
   hh=c.classList.contains('sized')?Math.max(1,Math.round((parseInt(c.style.height)+16)/70)):(v.layout?.h||4);if(x+w>12){x=0;y+=rh;rh=0}v.layout={x,y,w,h:hh};vp.visuals.find(z=>z.id===v.id).layout=v.layout;x+=w;rh=Math.max(rh,hh)});
  try{await api('save_report',{report:spec});rep.spec=spec;toast('Saved in the report: commit, then publish, and Viewer sees it.')}catch(e){toast(e.message)}return}
 const r=await fetch('/layouts',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify({page:k,layout:reset?null:out})}).then(x=>x.json()).catch(e=>({error:e.message}));
 if(r.error)toast(r.error);else{layFile().draft=r.result;vc();if(reset)show(k)}}
function layMode(on){LAYING=on;document.body.classList.toggle('laying',on);const b=$('#layout');b.classList.toggle('on',on);b.replaceChildren(ic(on?'check':'layout'),on?'Done':'Layout');$('#layreset').hidden=!on}
$('#layout').onclick=()=>{layMode(!LAYING);show(CUR);if(LAYING)toast('Drag a card to move it, its corner to resize it. Publish shows it to Viewer.')};
$('#layreset').onclick=()=>{if(CUR.startsWith('rep:')){toast('A report\'s layout is in the report: undo it under Changes.');return}layStore(true)};
// Go to (Ctrl/⌘ K): every page, report, model and flow by name; arrows move, Enter opens
function goTo(){if($('#goto'))return;const d=h('dialog'),q=h('input','f'),list=h('div','golist');d.id='goto';q.placeholder='Go to a page, report, model or flow…';q.setAttribute('aria-label','Go to');
 const groups=Object.fromEntries(nav().flatMap(([g,its])=>its.map(([k])=>[k,g])));let sel=0,hits=[];
 const draw=()=>{const t=q.value.trim().toLowerCase();hits=Object.entries(PAGES).filter(([k,[l]])=>!t||l.toLowerCase().includes(t)||k.toLowerCase().includes(t)).slice(0,12);sel=Math.min(sel,Math.max(0,hits.length-1));list.innerHTML='';
  hits.forEach(([k,[l]],i)=>{const b=h('button','goit'+(i===sel?' on':'')),base=k.split(':')[0];b.append(ic(IC[k]?k:{rep:'reports',mdl:'model',flw:'flows',obj:'objects'}[base]||'open'),h('span','lb',l),h('span','file',groups[k]||{rep:'Report',mdl:'Model',flw:'Flow',obj:'Objects'}[base]||''));
   b.onclick=()=>{d.close();d.remove();show(k)};list.append(b)});if(!hits.length)list.append(h('p','empty pad','Nothing by that name.'))};
 q.oninput=()=>{sel=0;draw()};q.onkeydown=e=>{if(e.key==='ArrowDown'||e.key==='ArrowUp'){e.preventDefault();sel=(sel+(e.key==='ArrowDown'?1:-1)+hits.length)%Math.max(1,hits.length);draw()}
  else if(e.key==='Enter'&&hits[sel]){d.close();d.remove();show(hits[sel][0])}};d.onclose=()=>d.remove();d.append(q,list);document.body.append(d);d.showModal();draw();q.focus()}
document.addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'){e.preventDefault();goTo()}});
{const sk=h('a','skip','Skip to the page');sk.href='#';sk.onclick=e=>{e.preventDefault();const m=$('main');m.tabIndex=-1;m.focus()};document.body.prepend(sk)}  /* keyboard: straight past the header and the sidebar */
const boot=loadVault();sidebar();
const R=document.documentElement,TB=$('#theme');const dark=()=>R.dataset.theme?R.dataset.theme==='dark':matchMedia('(prefers-color-scheme: dark)').matches;
const setTheme=t=>{if(t)R.dataset.theme=t;const d=dark();TB.classList.add('icon');TB.replaceChildren(ic(d?'sun':'moon'));TB.setAttribute('aria-label',d?'Light theme':'Dark theme');TB.title=d?'Switch to the light theme':'Switch to the dark theme'};try{setTheme(localStorage.getItem('abi-theme'))}catch(e){setTheme()}
TB.onclick=()=>{const t=dark()?'light':'dark';setTheme(t);try{localStorage.setItem('abi-theme',t)}catch(e){}};

function setMode(md){if(window.ME&&!['editor','owner'].includes(ME.role))md='viewer';const was=MODE;MODE=md;if(was!==md&&typeof sidebar==='function'&&side.children.length)sidebar();if(md==='viewer'&&LAYING)layMode(false);setTimeout(()=>{ctx();const g=log.querySelector('.hello');if(g)g.replaceWith(hello())});document.body.dataset.mode=md;$('#m-dev').classList.toggle('on',md==='developer');$('#m-view').classList.toggle('on',md==='viewer');
 $('#chat textarea').placeholder=md==='viewer'?'Ask a question. In Viewer the agent only reads.':'Ask a question or for a change.'}
async function pickMode(md){
 await fetch('/mode',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify({mode:md})})}
function vc(){const n=D.pending?.files?.length||0,cb=$('#changes'),pb=$('#publish');cb.replaceChildren(ic('commit'),n?`${n} change${n>1?'s':''}`:'No changes');cb.classList.toggle('pri',!!n);
 const head=(D.history||[])[0]?.id?.slice(0,7),pubc=(D.published?.commit||'').split(' ')[0];const L=D.app?.layouts||{},ld=JSON.stringify(L.draft||{})!==JSON.stringify(L.published||{});pb.disabled=!!n||!head||(head===pubc&&!ld);const live=!n&&head&&head===pubc&&!ld;pb.replaceChildren(ic(live?'check':'publish'),live?'Published':'Publish');pb.classList.toggle('live',!!live);
 pb.title=n?'Commit first: Publish sends the last commit.':head===pubc?(ld?'Show the new page layouts to Viewer.':'Viewer already shows the last commit.'):'Make the last commit what Viewer sees.'}
$('#changes').onclick=()=>{const d=$('#commit'),f=$('.files',d);f.innerHTML='';const fl=D.pending?.files||[];
 $('h3',d).replaceChildren(ic('commit'),fl.length?`${fl.length} change${fl.length>1?'s':''} not committed yet`:'Everything is committed');$('#c-commit').hidden=!fl.length;$('#c-message').hidden=$('label[for=c-message]').hidden=!fl.length;
 if(!fl.length)f.append(h('p','empty',D.pending?.can_undo?'Nothing to commit: every change is in ABI\'s history. Undo last change takes back the last one.':'Nothing to commit: every change is in ABI\'s history.'));fl.forEach(x=>{const det=h('details','chg'),su=h('summary'),[k,n,v]=plainFile(x.status,x.path);su.append(h('span','what',`${k} ${n}`),h('span','state'+(v==='deleted'?' bad':v==='added'?' on':''),v));su.title=x.path;det.append(su,h('pre','',x.patch||''));f.append(det)});
 $('#c-message').value=commitMsg(fl)||'Changes from the app';$('.msg',d).textContent='';$('#c-undo').disabled=$('#c-undo').hidden=!D.pending?.can_undo;$('#c-undo').title=D.pending?.can_undo?'Take back the last change':'Nothing to undo: no change since the last commit';d.showModal()};
$('#c-close').onclick=()=>$('#commit').close();
// a changed file in words: Source bronze_trial · changed (the Git path one hover away, the diff one click)
function plainFile(st,p){const m=/^([^/]+)\/(.+?)(\.json|\.md|\.ya?ml)?$/.exec(p||'')||[],dir=m[1]||'',name=m[2]||p,
  K={sources:'Source',tables:'Table',reports:'Report',flows:'Flow',ml:'ML model',tools:'Tool',agents:'Agent',docs:'Docs',security:'Security',environments:'Environment',app:'App setting',evals:'Evaluation set'},
  M={measures:'Measures',glossary:'Glossary',datasets:'Queries',models:'Data models',model:'Model',relationships:'Joins',calendar:'Calendar'},
  v={M:'changed',A:'added',D:'deleted',R:'renamed','??':'added'}[String(st||'').trim()]||'changed';
 const T={rls:'Row rules',roles:'Roles',project:'Project settings',descriptions:'Descriptions',security:'Security',environments:'Environments'};
 return !dir?[T[name.replace(/\.json$/,'')]||'File',T[name.replace(/\.json$/,'')]?'':p,v]:dir==='model'?[M[name]||'Model',M[name]?'':name,v]:(dir==='docs'||dir==='security')&&T[name]?[T[name],'',v]:[K[dir]||'File',K[dir]?idsToNames(name):p,v]}
function commitMsg(fl){const g={};fl.forEach(x=>{const [k,n,v]=plainFile(x.status,x.path),key=k+'|'+v;(g[key]||=[]).push(n)});
 return andj(Object.entries(g).map(([key,ns])=>{const [k,v]=key.split('|'),xs=ns.filter(Boolean),pl=xs.length>1&&!/s$|Security|Docs|Data models|Glossary|Model/.test(k)?k+'s':k;
  return `${pl}${xs.length?' '+andj(xs):''} ${v}`}).map((x,i)=>i?x[0].toLowerCase()+x.slice(1):x)).replace(/^./,c=>c.toUpperCase())}
$('#c-commit').onclick=async()=>{try{await api('commit',{message:$('#c-message').value||'Changes from the app'});$('#commit').close();toast('Committed')}catch(e){say($('#commit .msg'),e.message,1)}};
$('#c-undo').onclick=async()=>{try{const r=await api('undo',{});say($('#commit .msg'),`Undone: ${r.undone}`);D.pending=await api('changes',{});$('#changes').onclick()}catch(e){say($('#commit .msg'),e.message,1)}};
$('#publish').onclick=async()=>{if(!await ask({message:'Publish the last commit? Viewer will show it from now on.',preview:(D.history||[])[0]?.message||''}))return;
 toast('Publishing: ABI checks the last commit…');const post=(u,b)=>fetch(u,{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify(b||{})}).then(r=>r.json());
 let r=await post('/publish');if(r.confirm){if(!await ask(r.confirm)){toast('Not published.');return}r=await post('/confirm/'+r.confirm.id)}
 if(r.error){toast(`Publish failed: ${r.error}`,1);return}const c0=(D.history||[])[0]||{};D.published={...(D.published||{}),...(r.result||{}),at:r.result?.at||new Date().toISOString(),commit:r.result?.commit||c0.id};
 {const L=D.app?.layouts;if(L)L.published=structuredClone(L.draft||{})}vc();
 doneDialog('Published',[`Viewers now see “${c0.message?commitMsgOne(c0.message):'the last commit'}”, published ${when2(D.published.at)}.`,'Share a report: tick it for each person under Users & access. Viewers read this published copy until the next Publish.'],
  [['Close'],...((window.ME||{admin:true}).admin&&!MOBILE?[['Share a report',()=>show('people')]]:[])])};
const commitMsgOne=m=>{const [w,f]=plainCommit(m);return (w+(f&&f!=='—'?': '+f:'')).slice(0,110)};
const sideOff=on=>{document.body.classList.toggle('side-off',on);try{localStorage.setItem('abi-side-off',on?'1':'')}catch(e){}};try{sideOff(localStorage.getItem('abi-side-off')==='1')}catch(e){}
$('#menu').title='Show or hide the sidebar';$('#menu').classList.add('icon');$('#menu').replaceChildren(ic('menu'));$('#chat-toggle').prepend(ic('spark'));$('#menu').onclick=()=>R.classList.contains('mobile')?document.body.classList.toggle('menu-open'):sideOff(!document.body.classList.contains('side-off'));side.addEventListener('click',e=>{if(e.target.closest('button.it'))document.body.classList.remove('menu-open')});
// the phone drawer: a tap on its backdrop or Escape closes it
document.addEventListener('click',e=>{if(document.body.classList.contains('menu-open')&&!e.target.closest('aside,#menu')){e.preventDefault();e.stopPropagation();document.body.classList.remove('menu-open')}},true);
document.addEventListener('keydown',e=>{if(e.key==='Escape')document.body.classList.remove('menu-open')});
$('#switch').onclick=switcher;
function pwDialog(){const d=h('dialog'),c=h('div','card'),cur=h('input','f'),nw=h('input','f'),m=h('div','msg');c.append(h('h3','','Change your password'));
 cur.type=nw.type='password';cur.placeholder='';cur.autocomplete='current-password';nw.placeholder='at least 8 characters';nw.autocomplete='new-password';
 const ok=btn('Change',async()=>{const r=await fetch('/password',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify({current:cur.value,password:nw.value})}).then(x=>x.json()).catch(e=>({error:e.message}));
  cur.value=nw.value='';if(r.error){say(m,r.error,1);return}say(m,'Changed. Sign in again with the new one.');setTimeout(()=>location.reload(),1500)},1),no=btn('Close',()=>{d.close();d.remove()});
 // your account: who you are, the password, then tokens and two-step as their own rows, and Sign out
 const row=h('div','row acts');row.append(ok);cur.id='pw-cur';nw.id='pw-new';const lc=h('label','l','Current password'),ln=h('label','l','New password');lc.htmlFor='pw-cur';ln.htmlFor='pw-new';
 const me=h('div','pwho');me.append(h('span','av',((window.ME||{}).name||'?').slice(0,1).toUpperCase()),h('b','',(window.ME||{}).name||''),h('span','state',(window.ME||{}).role||''));
 const more=(t,sub,b)=>{const r=h('div','srow'),n=h('div','sname');n.append(h('b','',t),h('div','file',sub));r.append(n,b);return r};
 const so=h('form','so');so.method='post';so.action='/signout';{const b=h('button','btn danger','Sign out');b.prepend(ic('out'));so.append(b)}const foot=h('div','row foot');foot.append(so,no);
 c.querySelector('h3').replaceChildren(ic('user'),'Your account');
 c.append(me,h('h4','','Change your password'),lc,cur,ln,nw,row,m,h('h4','','Sign-in'),more('Personal tokens','read-only sign-in for SQL and MCP clients, as you',btn('Personal tokens',()=>{d.close();d.remove();tokDialog()})),
  more('Two-step sign-in','a code from an authenticator app after your password',btn('Two-step sign-in',()=>{d.close();d.remove();mfaDialog()})),foot);d.append(c);document.body.append(d);d.showModal();
 fetch('/signin/sso').then(r=>r.json()).then(L=>{if(!L.length)return;const l=h('div','row acts');c.insertBefore(h('h3','','Sign in with your company account'),m);c.insertBefore(l,m);
  L.forEach(x=>l.append(btn('Link '+x.name,async()=>{const r=await fetch('/api/oidc/'+encodeURIComponent(x.id)+'/start',{method:'POST',headers:{'Content-Type':'application/json'},body:'{"link":true}'}).then(y=>y.json());
   if(r.url)location.href=r.url;else say(m,r.error||'Not available.',1)})))}).catch(()=>{})}
function tokDialog(){const d=h('dialog'),c=h('div','card'),list=h('div'),nm=h('input','f'),dy=h('input','f'),m=h('div','msg');  // M4.5
 const post=b=>fetch('/tokens',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify(b)}).then(x=>x.json()).catch(e=>({error:e.message}));
 nm.placeholder='what it is for, e.g. Power BI';dy.type='number';dy.min=1;dy.max=365;dy.value=90;dy.title='days until it expires';
 const fill=async()=>{const r=await post({op:'list'});list.innerHTML='';if(r.error){say(m,r.error,1);return}
  if(!r.result.tokens.length)list.append(h('p','empty','No personal tokens yet.'));
  r.result.tokens.forEach(t=>{const row=h('div','row');row.append(h('span','',`${t.name}${t.person!==(window.ME||{}).name?' ('+t.person+')':''} · made ${t.created.slice(0,10)} · last used ${t.last_used?t.last_used.slice(0,10):'never'} · expires ${t.expires.slice(0,10)}`),
   btn('Revoke',async()=>{if(!confirm(`Revoke "${t.name}"? Anything signed in with it stops at once.`))return;const x=await post({op:'revoke',id:t.id});if(x.error)say(m,x.error,1);fill()}));list.append(row)})};
 const make=btn('Make a token',async()=>{const r=await post({op:'mint',name:nm.value.trim(),days:+dy.value||90});if(r.error){say(m,r.error,1);return}
  const out=h('input','f');out.readOnly=true;out.value=r.result.token;c.insertBefore(out,m);out.select();nm.value='';
  say(m,'Copy it now: it is shown only this once. It signs in as you, only to read: the SQL endpoint\'s password and an MCP client\'s bearer token.');fill()},1);
 const row=h('div','row acts'),two=h('div','two');row.append(make,btn('Close',()=>{d.close();d.remove()}));two.append(fld('What it is for',nm),fld('Expires after (days)',dy));
 c.append(h3i('key','Personal tokens'),h('p','file','Read-only sign-in as you for SQL clients (psql, DBeaver, BI tools) and MCP clients: only your rows, nothing changes. Never share one.'),list,h('h4','','Make a token'),two,row,m);
 d.append(c);document.body.append(d);d.showModal();fill()}
// two-step sign-in (ABI's start/confirm/disable_mfa as you): the key and the recovery codes live in this dialog only
function mfaDialog(){const d=h('dialog'),c=h('div','card'),b=h('div'),m=h('div','msg'),close=()=>{d.close();d.remove()};
 const post=x=>fetch('/mfa',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify(x)}).then(y=>y.json()).catch(e=>({error:e.message}));
 const inp=(ph,pw)=>{const i=h('input','f');i.placeholder=ph;if(pw){i.type='password';i.autocomplete='current-password'}else{i.autocomplete='one-time-code';i.inputMode='numeric'}return i};
 const step=(...els)=>{b.innerHTML='';const r=h('div','row acts');els.filter(e=>e.tagName==='BUTTON').forEach(e=>r.append(e));b.append(...els.filter(e=>e.tagName!=='BUTTON'),r);m.textContent=''};
 const codes=list=>{const pre=h('pre','',list.join('\n'));step(h('p','','Your recovery codes: each signs you in once if you lose your authenticator app. Save them now: they are shown only this once.'),pre,
  btn('Copy',async()=>{await navigator.clipboard.writeText(pre.textContent);say(m,'Copied.')}),btn('I saved them',async()=>{await fill();say(m,'Done: from your next sign-in, ABI asks for a code after your password.');toast('Two-step sign-in is on.')},1))};
 const on=async()=>{const pw=inp('your password',1);step(h('p','','Your password, to get a key for your authenticator app.'),pw,btn('Show the key',async()=>{const r=await post({op:'start',password:pw.value});pw.value='';if(r.error){say(m,r.error,1);return}
  const k=h('input','f'),a=h('a','',' Open in an authenticator app'),cd=inp('the 6-digit code it shows');k.readOnly=true;k.value=r.result.secret;a.href=r.result.uri;
  step(h('p','','Add this key to an authenticator app such as 1Password, Google Authenticator or Microsoft Authenticator (or open the link on this device), then enter the 6-digit code it shows.'),k,a,cd,btn('Turn on',async()=>{const x=await post({op:'confirm',code:cd.value});if(x.error){say(m,x.error,1);return}codes(x.result.recovery_codes)},1),btn('Cancel',close))},1),btn('Cancel',close))};
 const off=()=>{const pw=inp('your password',1),cd=inp('a code from your app, or a recovery code');step(pw,cd,btn('Turn off',async()=>{const r=await post({op:'off',password:pw.value,code:cd.value});pw.value=cd.value='';if(r.error){say(m,r.error,1);return}await fill();say(m,'Two-step sign-in is off.')},1),btn('Cancel',close))};
 const fill=async()=>{const r=await post({op:'status'});if(r.error){step(btn('Close',close));say(m,r.error,1);return}const s=r.result;
  step(h('p','state'+(s.on?' on':''),s.on?'On':'Off'),h('p','',s.on?(s.required?'Two-step sign-in is on; your organisation requires it.':'Two-step sign-in is on: after your password you enter a code from your authenticator app.'):'Two-step sign-in is off: your password alone signs you in. Turn it on to add a code from an authenticator app such as 1Password, Google Authenticator or Microsoft Authenticator.'),
   ...(s.on?(s.required?[]:[btn('Turn off',off)]):[btn('Turn on',on,1)]),btn('Close',close))};
 c.append(h3i('lock','Two-step sign-in'),b,m);d.append(c);document.body.append(d);d.showModal();fill()}
{const q=new URLSearchParams(location.search).get('signin');if(q){setTimeout(()=>toast({linked:'Linked: you can sign in with it now.',conflict:'That account is linked to another person here.'}[q]||'Linking didn\'t work: try again.'));history.replaceState(null,'',location.pathname+location.hash)}}
if(window.ME?.signin){const w=h('button','btn who');w.append(h('span','av',(ME.name||'?').slice(0,1).toUpperCase()),h('span','nm',ME.name));w.title='Your password, personal tokens and two-step sign-in';w.onclick=pwDialog;const f=h('form');f.method='post';f.action='/signout';f.className='so';f.hidden=true;f.append(ib('out','Sign out'));const r0=$('header .right');r0.prepend(w,f)}
if(window.ME&&!['editor','owner'].includes(ME.role))$('.modes').hidden=true;/* Viewer access only: no switch at all */$('#m-dev').onclick=()=>pickMode('developer');$('#m-view').onclick=()=>pickMode('viewer');
let first=hashKey();if(!first)try{first=localStorage.getItem('abi-app-'+D.project)}catch(e){}setTimeout(()=>show(first||'home'));  // after the whole script: the chat page's parts are declared further down
boot.then(()=>{sidebar();show(CUR);inboxCount();setTimeout(()=>{if(D.docs==null&&PAGES.docs)api('docs',{}).then(r=>{if(D.docs==null){D.docs=r||{};if(CUR==='docs')show('docs')}}).catch(()=>{})},1200)});setMode('developer');vc();
window.addEventListener('hashchange',()=>{const k=hashKey();if(k&&k!==CUR)show(k)});

// ---------- the agent: it steers this page, the page follows its changes, and the person talks to it here ----------
async function switcher(){let d=$('#switcher');if(d){d.remove();return}
 const r=(await fetch('/projects',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:'{}'}).then(x=>x.json())).result;
 d=h('div','card');d.id='switcher';const ws=h('select','f'),list=h('div','areas');Object.keys(r.workspaces).forEach(w=>ws.append(new Option(w,w)));ws.value=r.workspace;
 const fill=()=>{list.innerHTML='';(r.workspaces[ws.value]||[]).forEach(p=>{const b=h('button','lk');b.append(h('span','pav',(p.name||p.id).slice(0,1).toUpperCase()),h('b','',p.id),h('span','',p.name===p.id?'':p.name));
  if(p.id===r.current)b.classList.add('on');b.onclick=()=>{location.href='/?p='+encodeURIComponent(p.id)+'#home'};list.append(b)})};ws.onchange=fill;fill();
 d.append(h('h3','','Switch project'),h('label','l','Workspace'),ws,h('label','l','Projects in it'),list);document.body.append(d);ws.focus();
 // a popover under the crumb: a click outside or Escape closes it
 const off=e=>{if(e.type==='keydown'?e.key==='Escape':!d.contains(e.target)&&!e.target.closest('#switch,.proj')){d.remove();document.removeEventListener('mousedown',off);document.removeEventListener('keydown',off)}};
 setTimeout(()=>{document.addEventListener('mousedown',off);document.addEventListener('keydown',off)})}
function toast(t,bad,action){const x=h('div','toast'+(bad?' bad':''));x.dataset.t=Date.now();x.append(ic(bad?'alert':action?'info':'check'),h('span','',t));x.setAttribute('role',bad?'alert':'status');document.querySelectorAll('body>.toast:not([style])').forEach(o=>o.remove());
 if(action){const b=h('button','tact',action[0]);b.onclick=async()=>{x.remove();await action[1]()};x.append(b)}document.body.append(x);setTimeout(()=>x.remove(),action?12000:bad?9000:6000)}
// a delete is one uncommitted change in ABI: the toast after it offers to take it back (the Changes dialog's Undo)
function undoToast(what){toast(`${what} deleted.`,0,['Undo',async()=>{try{const r=await api('undo',{});toast(`${what} is back.`);setTimeout(()=>highlight(what),1500)}catch(e){toast(e.message,1)}}])}
function highlight(item){if(!item)return;const want=String(item).toLowerCase();
 const c=[...document.querySelectorAll('main .card')].find(c=>c.textContent.toLowerCase().includes(want));if(c){c.classList.add('hl');c.scrollIntoView({behavior:'smooth',block:'center'});setTimeout(()=>c.classList.remove('hl'),4000)}}
const chatBox=$('#chat'),log=$('#chat .log'),inp=$('#chat textarea'),busy=$('#chat .busy');
// an answer's text as HTML (owner, 5 Nov 2026: "a proper table, bold shown properly"): escaped first, so nothing the
// model writes becomes markup; then code blocks, tables, lists, headings, bold, italics, inline code and http(s) links
function md(t){const C=[],I=x=>x.replace(/`([^`\n]+)`/g,'<code>$1</code>').replace(/\*\*([^*\n]+)\*\*/g,'<b>$1</b>')
  .replace(/(^|[^*\w])\*([^*\s](?:[^*\n]*?[^*\s])?)\*(?![*\w])/g,'$1<i>$2</i>').replace(/(^|[^\w])_([^_\s](?:[^_\n]*?[^_\s])?)_(?!\w)/g,'$1<i>$2</i>')
  .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)"]+)\)/g,'<a href="$2" target=_blank rel="noopener noreferrer">$1</a>')
  // 1.2.6 M7: an object the agent read, named as a link to its page: [its title](#obj:<type>), nothing else after the #
  // (no key: the grounding check reads a key in an answer as a figure). A click finds the object of that title (below)
  .replace(/\[([^\]\n]+)\]\(#(obj:[a-z][a-z0-9_-]{0,62})\)/g,'<a href="#$2" class=olink>$1</a>');
 t=t.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])).replace(/```[^\n]*\n([\s\S]*?)```\n?/g,(_,c)=>{C.push(c);return '\u0000'+(C.length-1)+'\u0000'})
 .replace(/(^|\n)((?:[ \t]*\|[^\n]*\|[ \t]*(?:\n|$))+)/g,(all,pre,block)=>{const rows=block.trim().split('\n').map(r=>r.trim().slice(1,-1).split('|').map(c=>c.trim()))
  .filter(r=>!r.every(c=>/^:?-{2,}:?$/.test(c)));if(rows.length<2)return all;const n=c=>/^[-+]?[\d.,]+\s*%?$/.test(c);
  return pre+'<table class=mdt><tr>'+rows[0].map(c=>'<th>'+I(c)+'</th>').join('')+'</tr>'+rows.slice(1).map(r=>'<tr>'+r.map(c=>'<td'+(n(c)?' class=n':'')+'>'+I(c)+'</td>').join('')+'</tr>').join('')+'</table>'})
 .replace(/(^|\n)((?:[ \t]*(?:[-*+]|\d+[.)])[ \t]+[^\n]*(?:\n|$))+)/g,(all,pre,block)=>{let out='',st=[];
  for(const l of block.replace(/\n$/,'').split('\n')){const m=/^([ \t]*)([-*+]|\d+[.)])[ \t]+(.*)$/.exec(l),d=m[1].replace(/\t/g,'  ').length>=2?1:0,tag=/\d/.test(m[2])?'ol':'ul';
   while(st.length>d+1)out+='</li></'+st.pop()+'>';if(st.length===d+1&&st[d]!==tag)out+='</li></'+st.pop()+'>';
   if(st.length<d+1){if(st.length<d){st.push('ul');out+='<ul><li>'}out+='<'+tag+' class=mdl><li>';st.push(tag)}else out+='</li><li>';out+=I(m[3])}
  while(st.length)out+='</li></'+st.pop()+'>';return pre+out})
 .replace(/(^|\n)#{1,4}[ \t]+([^\n]+)\n?/g,(_,pre,x)=>pre+'<b class=mdh>'+x.replace(/[ \t]+#+[ \t]*$/,'')+'</b>');
 return I(t.replace(/<table class=mdt>[\s\S]*?<\/table>|<ol class=mdl>[\s\S]*?<\/ol>|<ul class=mdl>[\s\S]*?<\/ul>/g,x=>(C.push(x),'\u0001'+(C.length-1)+'\u0001')))
  .replace(/\u0001(\d+)\u0001/g,(_,i)=>C[i]).replace(/\u0000(\d+)\u0000/g,(_,i)=>'<pre class=mdc><code>'+C[i]+'</code></pre>')} // md end
// the grounding check's marks in words: the figure stays hidden, the note says so plainly
const plainMask=x=>x.replace(/\[not verified\]/g,'<span class=unv title="Hidden: no figure in the data backs it">(hidden)</span>').replace(/<i>Grounding: (\d+) figure\(s\) or name\(s\) no tool result backs were masked\.<\/i>/g,(_,n)=>`<span class=gnote>${n==='1'?'1 figure couldn\'t be checked against the data and was hidden.':n+' figures couldn\'t be checked against the data and were hidden.'}</span>`);
function bubble(cls,text){if(cls==='ai'&&TURN)TURN.ai=true;const b=h('div','m '+cls);if(cls==='ai')b.innerHTML=plainMask(md(text));else b.textContent=text;log.append(b);log.scrollTop=log.scrollHeight;return b}
let steps=null;
// Chat page: the same conversation as the side panel, full width; leaving the page (or the agent showing one) puts it back at the side
function chatPage(box){wideT(1);chatBox.classList.add('full','open');document.body.classList.remove('chat-open');box.append(chatBox);chats();inp.focus()}
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
function ctx(){const c=$('#chat .ctx'),g=log.querySelector('.hello');if(c)c.textContent=`${MODE==='viewer'?'Viewer · read-only':'Developer'} · on ${PAGES[CUR]?.[0]||CUR}`;if(g&&CUR!=='chat')g.replaceWith(hello())}
const INTRO='';log.append(hello());
// an object's name in an answer (md's a.olink): the object of that title opens, as a report's Open does
log.addEventListener('click',e=>{const a=e.target.closest?.('a.olink'),T=a&&otype(a.getAttribute('href').slice(5));if(!T)return;e.preventDefault();objOpen(T,`${T.table}.${T.title}`,a.textContent)});
async function cpost(u,b){return fetch(u,{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify(b||{})}).then(r=>r.json())}
async function chats(sel){const r=await cpost('/chat/list'),pk=$('#chat-pick');pk.innerHTML='';
 const cur=sel||r.current;if(!r.chats.some(c=>c.id===cur))pk.append(new Option('New chat',cur||''));
 r.chats.forEach(c=>pk.append(new Option(`${c.title||'Chat'} · ${c.messages}`,c.id)));pk.value=cur||'';
 const del=$('#chat-del');if(del)del.hidden=!r.chats.length}
function paint(c){log.innerHTML='';steps=null;(c.messages||[]).forEach(m=>bubble(m.role==='me'?'me':'ai',m.text));if(!log.children.length)log.append(hello())}
$('#chat-new').onclick=async()=>{const c=await cpost('/chat/new');paint(c);chats(c.id);inp.focus()};
$('#chat-pick').onchange=async e=>{if(!e.target.value)return;const c=await cpost('/chat/open',{id:e.target.value});paint(c);chats(c.id)};
$('#chat-toggle').onclick=()=>openChat();$('#chat .wide-t').onclick=()=>{if(chatBox.classList.contains('full')){show(PREV||'home');openChat(true)}else show('chat')};$('#chat .x').onclick=()=>openChat(false);
// the chat's header: icon buttons, their words as names and tooltips
function wideT(full){const b=$('#chat .wide-t');b.classList.add('icon');b.replaceChildren(ic(full?'narrow':'wide'));b.setAttribute('aria-label',full?'Side panel':'Full page');b.title=full?'Back to the side panel':'Open the chat as a full page'}
if(MOBILE){const t=h('div','chtitle');t.append(ic('spark'),h('b','','Ask the agent'),h('span','file','reads the published version'));$('#chat').insertBefore(t,$('#chat header'))}
{const n=$('#chat-new'),x=$('#chat .x');n.classList.add('icon');n.replaceChildren(ic('plus'));n.setAttribute('aria-label','New chat');n.title='New chat';x.classList.add('icon');x.replaceChildren(ic('x'));x.setAttribute('aria-label','Close');x.title='Close the chat';wideT($('#chat').classList.contains('full'))}
async function sendChat(){const t=inp.value.trim();if(!t)return;
 log.querySelector('.hello')?.remove();log.querySelector('.follow')?.remove();bubble('me',t);inp.value='';steps=null;TURN={shown:false};
 await fetch('/chat',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify({message:t,page:CUR})})}
// ABI's data moved: redraw, but never from under an open filter, menu or dialog (it waits until they close)
window.__redraw=()=>{const keep=[...document.querySelectorAll('main .card.editor')],typed=[...document.querySelectorAll('main [data-u]')].filter(x=>!x.closest('.card.editor')&&x.type!=='file').map(x=>[fkey(x),x.type==='checkbox'?x.checked:x.value]);sidebar();let k=CUR;if(PENDING.open&&PAGES['rep:'+PENDING.open]){k='rep:'+PENDING.open;PENDING.open=null;keep.length=0}show(k);{const hd=$('main .pghead');if(hd)keep.reverse().forEach(c=>{const rh=c.classList.contains('inrows')&&$('main .rows>:first-child');rh?rh.after(c):hd.after(c)})}
   refreshPending();if(typed.length){const at=Object.fromEntries([...document.querySelectorAll('main input,main textarea,main select')].map(x=>[fkey(x),x]));typed.forEach(([k,v])=>{const x=at[k];if(!x)return;if(x.type==='checkbox')x.checked=v;else x.value=v;x.dataset.u=1})}vc()}  // a form being filled in survives the redraw, also outside an editor card
// a redraw waits while the person is using the page: a popover, menu or dialog open, a field focused, or a click or key in the last 3 s
let REDRAW=null,USED=0;['pointerdown','keydown','input','change'].forEach(t=>document.addEventListener(t,()=>{USED=Date.now()},true));
function redrawSoon(){if(REDRAW)return;const go=()=>{const f=document.activeElement;if(document.querySelector('.fpop,.more>.menu:not([hidden]),dialog[open],#switcher')||Date.now()-USED<3000||f&&f.closest?.('main')&&/^(INPUT|SELECT|TEXTAREA)$/.test(f.tagName))return;clearInterval(REDRAW);REDRAW=null;window.__redraw?.()};REDRAW=setInterval(go,400);go()}
// after an answer that opened no page: the next steps people take, one click each
var TURN=null;function followUps(){const d=h('div','follow sugg');[['Compare the two periods','Show that as a chart in Explore: the measure and its previous period side by side as bars, by the dimension that explains the change, for the latest complete month.'],['Break it down further','Break that down by the dimension that explains it most, and show it as a chart.']].forEach(([l,q])=>{const b=h('button','sg',l);b.onclick=()=>{inp.value=q;sendChat()};d.append(b)});log.append(d);log.scrollTop=log.scrollHeight}
$('#chat .send').onclick=sendChat;inp.onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendChat()}};
$('#chat .stop').onclick=()=>fetch('/chat/stop',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:'{}'});
{const es=new EventSource('/events?k='+encodeURIComponent(KEY)+'&c='+CID+'&m='+(MOBILE?1:0)+'&p='+encodeURIComponent(D.project));
 es.onerror=()=>{if(es.readyState===2)stale()};  // the stream was refused: a restart or an ended session
 es.onmessage=async ev=>{const e=JSON.parse(ev.data);
  if(e.type==='show'){if(TURN)TURN.shown=true;PENDING.filters=e.filters||null;if(e.page==='explore'&&e.query){const q={measures:e.query.measures||[],dimensions:(e.query.dimensions||[]).slice(0,1),filters:e.query.filters||[]};PENDING.explore={...q,chart:e.chart};keepMine({name:q.measures.map(m=>D.measures.find(x=>x.id===m)?.name||m).join(' and ')+(q.dimensions[0]?' by '+colName(q.dimensions[0]).toLowerCase():''),by:'agent',q,type:e.chart||'bar',at:new Date().toISOString().slice(0,16).replace('T',' ')});saveMine();sidebar()}const nv=h('div','m nav','→ '+(e.page==='report'?'report '+e.report:e.page)+(e.note?' · '+e.note:''));nv.onclick=()=>show(e.page==='report'?'rep:'+e.report:e.page);log.append(nv);log.scrollTop=log.scrollHeight;const wasFull=chatBox.classList.contains('full');const k=e.page==='report'?'rep:'+e.report:e.page;show(k);if(wasFull)openChat(true);setTimeout(()=>highlight(e.item),400);if(e.note)toast('Agent: '+e.note)}
  else if(e.type==='data'){normQ(e.data?.quality);const moved=Object.keys(e.data||{}).some(k=>JSON.stringify(D[k])!==JSON.stringify(e.data[k]));Object.assign(D,e.data);if(moved)redrawSoon()}  // the same data again (a reconnect, a change elsewhere) redraws nothing
  else if(e.type==='busy'){busy.hidden=!e.on;$('#chat .stop').hidden=!e.on;if(!e.on&&TURN){const t=TURN;TURN=null;setTimeout(()=>{if(!t.shown&&(t.ai||log.querySelector('.m.ai:last-of-type')))followUps()},400)}}
  else if(e.type==='step'){if(!steps){steps=h('div','m steps');log.append(steps)}const last=[...steps.children].reverse().find(x=>x.dataset.t===e.tool&&x.dataset.s==='run');
   if(e.state==='run'||!last){const l=h('div','',`${e.state==='run'?'…':e.state==='ok'?'✓':'✗'} ${stepName(e.tool)}`);l.dataset.t=e.tool;l.dataset.s=e.state;steps.append(l)}
   else{last.textContent=`${e.state==='ok'?'✓':'✗'} ${stepName(e.tool)}`;last.dataset.s=e.state;last.className=e.state==='ok'?'':'bad'}log.scrollTop=log.scrollHeight}
  else if(e.type==='progress'){const r=steps&&[...steps.children].reverse().find(x=>x.dataset.s==='run');if(r)r.textContent=`… ${stepName(r.dataset.t)}${e.text?' · '+e.text:''}`}  // a big load the agent waits for (M3.2)
  else if(e.type==='said')bubble('me',e.text)
  else if(e.type==='answer'){steps=null;if(e.model){MODEL=e.model;ctx()}bubble('ai',e.text);chats();if(!chatBox.classList.contains('open'))toast('The agent answered')}
  else if(e.type==='sys')bubble('sys',String(e.text||''));
  else if(e.type==='ask'){const once=e.title===SHOWN,yes=await ask(once?{title:e.title,message:'',preview:e.message,yes:'I copied it',plain:true,once:String(e.message||'').split('\n\n')[1]||''}:chatAsk(e));bubble('sys',once?'Shown to you once; it is not kept in this chat. If it wasn\'t copied, make a new one (the old one stops working).':yes?'You confirmed.':'You declined.');  // a credential: for the person's eyes only (index.ts shownOnce)
   fetch('/chat/answer',{method:'POST',headers:{'X-Abi-App':KEY,'Content-Type':'application/json'},body:JSON.stringify({id:e.id,yes})})}
  else if(e.type==='open'){location.href='/?p='+encodeURIComponent(e.project)+'#'+encodeURIComponent(e.page)}
  else if(e.type==='mode'){setMode(e.mode);show(CUR);if(e.mode==='viewer'&&!e.published){let seen=0;try{seen=sessionStorage.getItem('abi-unpub');sessionStorage.setItem('abi-unpub','1')}catch(x){}if(!seen)toast('Nothing published yet: Viewer shows the working version until you publish.')}}
  else if(e.type==='vault'){await loadVault();sidebar();show(CUR)}
  else if(e.type==='note'){if(e.chat)bubble('sys',e.text);else toast(e.text)}}}
"""


def read(action: str, project: str, **kw):
    try:
        return abi.call(action, project=project, **kw)
    except abi.ActionError:
        return None


ROLES_SYNCED: set[str] = set()  # projects whose published copy's roles this process has matched to the project's


def pub_id(project: str) -> str:
    """The published copy Viewer reads: a sibling ABI project that changes only by Publish."""
    return f"{project}_pub"[:63]


def db_secret(project: str, table: str) -> str:
    """The name of a database card's two login secrets (+ _USER, _PASSWORD), one per project and table: sales_eu's
    orders and sales' eu_orders were both DB_SALES_EU_ORDERS, so one card overwrote the other's login and host."""
    import hashlib  # noqa: PLC0415
    return f"DB_{hashlib.sha256(f'{project}/{table}'.encode()).hexdigest()[:10]}_{table}".upper()


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


AT_ONCE = 6  # ABI calls one page's data has under way at once: the page's lists and its visuals (M3.3)
ASKED = ("changes", "semantic_model", "list_reports", "list_flows", "ml_models", "list_tools", "list_agents", "readiness",
         "data_quality", "rls_rules", "history", "ontology", "optimizers")  # not docs: its worked examples run a query per measure, so the Docs page asks


class Reads(ThreadPoolExecutor):
    """AT_ONCE threads that ask ABI as the person the creating thread acts as (abi.as_user holds per thread)."""

    def __init__(self):
        super().__init__(AT_ONCE)
        self.token = abi.token()

    def submit(self, fn, /, *args, **kwargs):
        def as_them():
            with abi.as_user(self.token):
                return fn(*args, **kwargs)
        return super().submit(as_them)


def snapshot(dest: Path, project: str, visuals: bool = True) -> dict:
    """What the page shows: ABI's live state, uncommitted changes included. The folder holds commits only, so the
    definitions read from it (sources, staged tables' steps, queries) come from a copy with ABI's pending changes applied.
    Everything it asks ABI is asked at once (Reads), as this person; without `visuals`, the reports' visuals have no rows."""
    import shutil  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    with tempfile.TemporaryDirectory() as tmp, Reads() as ex:
        got = {a: ex.submit(read, a, project) for a in ASKED}
        pending = got["changes"].result()
        patch = "".join((f.get("patch") or "").rstrip("\n") + "\n" for f in (pending or {}).get("files") or [])
        if patch.strip():
            shutil.copytree(dest, tmp, ignore=shutil.ignore_patterns(".git", "app.html"), dirs_exist_ok=True)
            if subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], cwd=tmp, input=patch, text=True, capture_output=True).returncode == 0:
                dest = Path(tmp)  # ponytail: a patch that doesn't apply falls back to the last commit
        return _snapshot(dest, project, pending, lambda a: got[a].result(), ex if visuals else None)


def report_data(project: str, specs: list[dict], measures: list[dict], ex: Reads | None) -> list[dict]:
    """The page's reports: each page's visuals with their rows read through `ex` (None: no rows)."""
    by_id, reports = {m["id"]: m for m in measures}, []
    for rep in specs:
        pages = render.report_pages(project, rep["id"], rep, by_id, ex)
        for p, spec in zip(pages, rep.get("pages", [])):  # the dimension a click filters on
            dims = {v["id"]: ((v.get("query") or {}).get("dimensions") or [None])[0] for v in spec.get("visuals", [])}
            for v in p["visuals"]:
                v["dim"] = dims.get(v["id"])
        reports.append({"id": rep["id"], "name": rep.get("name"), "pages": pages, "spec": rep})
    return reports


def _snapshot(dest: Path, project: str, pending: dict | None, ask, ex: Reads | None) -> dict:
    j = render.j
    sm = ask("semantic_model") or {}
    measures = sm.get("measures") or (j(dest / "model/measures.json", {}) or {}).get("measures", [])
    reports = report_data(project, sorted((ask("list_reports") or {}).get("reports") or [], key=lambda r: r.get("id", "")), measures, ex)
    dims = sorted(f"{t}.{c}" for t, cols in (sm.get("columns") or {}).items() if t in (sm.get("tables") or {})
                  for c, ty in cols.items() if any(k in str(ty) for k in ("VARCHAR", "BOOLEAN")) or t == "calendar")
    g = lambda d: [j(f, {}) for f in sorted((dest / d).glob("*.json")) if not f.name.endswith(".eval.json")]  # noqa: E731
    live = lambda action, key, d: ((ask(action) or {}).get(key)) or g(d)  # noqa: E731 (status from ABI, else files)
    queries = {d["id"]: d for d in (j(dest / "model/datasets.json", {}) or {}).get("datasets", [])}
    table_steps = {f.stem: (j(f, {}) or {}).get("steps") for f in sorted((dest / "tables").glob("*.json"))}
    sources = {f.stem: j(f, {}) for f in sorted((dest / "sources").glob("*.json"))}
    model = {"relationships": sm.get("relationships") or [], "tables": sm.get("tables") or {}, "date_table": sm.get("date_table")}
    sections = render.sections(dest)
    sections.pop("Security")  # the raw rules name every person's values (SE-6): the page draws ABI's rls_rules, as the person
    kinds = {t: (v or {}).get("kind") for t, v in model["tables"].items()}
    sections["Model"] = f"<div class=card>{render.diagram(model, kinds)}</div>"
    ont = ask("ontology") or {}  # 1.2.6 M7: object types, links and action types as this person reads them (none: as before)
    out = {"project": project, "measures": measures, "reports": reports, "dims": dims, "flows": live("list_flows", "flows", "flows"),
           "ml": live("ml_models", "models", "ml"), "optimizers": (ask("optimizers") or {}).get("optimizers") or [],  # 1.2.6 M12: none, as before
           "tools": live("list_tools", "tools", "tools"),
           "agents": live("list_agents", "agents", "agents"),
           "agent_evals": {f.name.removesuffix(".eval.json"): (j(f, {}) or {}).get("cases", []) for f in sorted((dest / "agents").glob("*.eval.json"))}, "glossary": sm.get("glossary") or [], "models": sm.get("models") or [],
           "datasets": [queries.get(d["id"], {}) | d for d in sm.get("datasets") or []], "model": model,
           "columns": sm.get("columns") or {}, "sources": sorted(sources), "source_specs": sources, "table_steps": table_steps,
           "tables": sorted(model["tables"]), "readiness": ask("readiness"), "pending": pending,
           "quality": ask("data_quality"), "rls": ask("rls_rules"), "history": (ask("history") or {}).get("commits", []),
           "docs": None, "sections": sections, "ontology": ont, "published": published(project.removesuffix("_pub")),
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
    mon = MONITOR | (j(app_dir(base) / "monitor.json", {}) or {})  # a measure check's condition: what the page and its flow ask ABI
    mon["checks"] = [c | {"condition": monitor_condition(c)} for c in mon.get("checks") or [] if isinstance(c, dict)]
    out["app"] = {"analyses": j(app_dir(base) / "analyses.json", []) or [], "monitor": mon,
                  "packs": pack.listing(base), "layouts": j(app_dir(base) / "layouts.json", {}) or {}}
    return out


def viewer_docs(docs: dict | None) -> dict | None:
    """The published copy's documentation as Viewer reads it: the copy's owners are people with their own role there
    (publish_roles), so ABI lists their usernames; a viewer reads "the project's owners". Readiness is the builders'
    (its gaps name owner-only tables), so a viewer's copy leaves it out."""
    for d in (docs or {}).get("projects") or []:
        for s in d.get("sections") or []:
            for it in s.get("items") or []:
                facts = []
                for k, v in it.get("facts") or []:
                    if str(k).startswith("Readiness"):
                        continue
                    k = "Things to know first" if str(k).startswith("Five things") else k
                    if k == "Owners":
                        v = "the project's owners"
                    elif isinstance(v, str) and " | " in v:
                        v = " | ".join(x for x in v.split(" | ") if not re.match(r"(\d+\.\s*)?Readiness\b", x.strip()))
                    facts.append([k, v])
                it["facts"] = facts
    return docs


def owner_only(bundle: dict) -> None:
    """Leave out of the published copy what it can't hold: an ML model's prediction table (ml_<model>), its joins, the
    measures over it and the object type properties read from it. The copy never scores, so the table is never loaded there, and its rows come from filtered
    data with no row rule of its own: a restricted viewer got an engine error on ML, four owner-only readiness gaps on
    Home and Monitor, and a forecast chip in Explore (R4). The ML model's definition stays (its page draws the actuals)."""
    outs = {f"ml_{b['id']}" for rel, b in bundle.items() if rel.startswith("ml/") and isinstance(b, dict) and b.get("id")}
    m, ms = bundle.get("model/model.json"), bundle.get("model/measures.json")
    if not outs or not isinstance(m, dict):
        return
    m["tables"] = [t for t in m.get("tables") or [] if t.get("table") not in outs]
    if m.get("entities"):  # its key (1.2.6 M9: a model on an object type keys its table)
        m["entities"] = [e for e in m["entities"] if e.get("table") not in outs]
    m["relationships"] = [r for r in m.get("relationships") or [] if not {r["from"]["table"], r["to"]["table"]} & outs]
    types = [x for x in (o.get("objects") or [])] if isinstance(o := bundle.get("model/ontology.json"), dict) else []
    for x in types:  # 1.2.6 M9: a prediction shown as a property of an object type goes with its table
        if "properties" in x:
            x["properties"] = [p for p in x["properties"] if p["column"].rpartition(".")[0] not in outs]
    if isinstance(ms, dict):
        hit = re.compile(r'(?<!\w)"?(' + "|".join(map(re.escape, sorted(outs))) + r')"?\.')
        gone = {x["id"] for x in ms.get("measures") or [] if hit.search(x.get("expression") or "")}
        ms["measures"] = [x for x in ms.get("measures") or [] if x["id"] not in gone]
        g = bundle.get("model/glossary.json")
        for t in (g or {}).get("terms") or [] if isinstance(g, dict) else []:
            t["measures"] = [x for x in t.get("measures") or [] if x not in gone]
        for x in types:  # a forecast's measure among a type's measures
            if "measures" in x:
                x["measures"] = [i for i in x["measures"] if i not in gone]


def read_only(bundle: dict) -> None:
    """1.2.6 M7: the published copy runs nothing. It gets no flows (publish_bundle), so no action types either (one that
    starts a flow would fail the copy's import) and no form cards for them: Viewer reads objects, actions run in
    Developer, on the project itself."""
    if isinstance(o := bundle.get("model/ontology.json"), dict):
        o.pop("actions", None)
    for rel, r in bundle.items():
        if rel.startswith("reports/") and isinstance(r, dict):
            for p in r.get("pages") or []:
                p["visuals"] = [v for v in p.get("visuals") or [] if v.get("type") != "form"]


def publish_bundle(project: str) -> tuple[str, dict, str]:
    """Commit is ABI's; Publish copies the last commit (never uncommitted work) into the published project, through
    ABI's import checks (and its confirmations). Viewer reads only that copy."""
    abi.sync(project)
    dest = abi.folder(project)
    bundle = {}
    for f in sorted(dest.rglob("*")):
        rel = f.relative_to(dest).as_posix()
        # flows run once, in the project: the copy gets none (each schedule would email twice, each Monitor alert post
        # twice), and the import needn't take a flow someone else saved in the publisher's name (it refuses that).
        # 1.2.6 M12: nor plans (optimizers/): a plan is worked out and approved in the project, and its runs stay there
        if f.is_file() and ".git" not in f.relative_to(dest).parts and f.name not in (".gitkeep", "app.html") and not rel.startswith(("flows/", "optimizers/")):
            bundle[rel] = json.loads(f.read_text()) if rel.endswith(".json") else f.read_text()
    owner_only(bundle)
    read_only(bundle)
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
        (Path(d) / "docs.html").write_text(render.docs_html(name, (viewer_docs if project.endswith("_pub") else lambda d: d)(read("docs", project) or {})))
        subprocess.run([str(py), "-c", code, f"{d}/docs.html", f"{d}/docs.pdf"], check=True, timeout=180,
                       env={**os.environ, "HOME": home if Path(home).exists() else os.environ.get("HOME", "")}, capture_output=True)
        return (Path(d) / "docs.pdf").read_bytes()


def publish_layouts(project: str, by: str = "") -> None:
    """Publish also copies the page layouts Developer arranged (layouts.json "draft") to what Viewer sees ("published")."""
    lay = render.j(app_dir(project) / "layouts.json", {}) or {}
    if lay.get("draft") != lay.get("published"):
        app_save(project, "layouts.json", lay | {"published": lay.get("draft") or {}}, by)


def publish_roles(project: str, pub: str) -> None:
    """Everyone with a role on the project reads its published copy with that same role. A role given before the first
    Publish, or in ABI's own app, left that person an empty Viewer (release run 31 Oct); and Viewer on the copy for
    everyone made ABI's row rules ("a Viewer not listed sees no rows") hide every row from an owner or editor reading the
    copy in Viewer mode or on a phone (1.2.5). Viewer mode is the app's read-only face, never less access. Removing a role
    stays with Users & access (it is destructive and asks first)."""
    try:
        users = abi.call("list_users").get("users") or []
    except abi.ActionError:
        return  # not an owner: the owner's Publish does it
    for u in users:
        role = (u.get("roles") or {}).get(project)
        if role and role != "none" and (u.get("roles") or {}).get(pub) != role and not u.get("workspace_owner"):
            try:
                abi.call("set_role", project=pub, user=u["name"], role=role)
            except abi.ActionError as e:
                print(f"abi-pi: viewer role on {pub} for {u['name']} not set: {e.body.get('message')}", file=sys.stderr)


def published_now(project: str, pub: str, head: str) -> dict:
    """After the import into the copy: its layouts, roles and record. No reload here (1.1 reloaded every source, 40 s at
    7.4M orders even when only a measure changed): the import forgets the fingerprint of each table it changed, so ABI's
    worker loads those within its minute (IN-6), and `watch` refreshes the pages when the copy's data moves (M3.2)."""
    publish_layouts(project)
    publish_roles(project, pub)
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
                        "<input class=f id=c-message><div class='row foot'><button class=btn id=c-undo>Undo last change</button><span class=sp></span>"
                        "<button class=btn id=c-close>Close</button><button class='btn pri' id=c-commit>Commit</button></div><div class=msg></div></dialog>"
                        "<dialog id=confirm><h3>Confirm this change</h3><p id=c-msg></p><p id=c-abi class=abi hidden></p><pre id=c-prev></pre>"
                        "<div class=row><button class=btn id=c-no>Cancel</button><button class=btn id=c-copy hidden>Copy</button><button class='btn pri' id=c-yes>Confirm</button></div></dialog>")
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
        for h in ([self] if here else []) + (self.watchers() if event.get("type") in ("step", "progress", "answer", "busy", "said") else []):
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
        with AGENTS:  # an agent starts only in a free slot (ABI_MAX_AGENTS); else the question waits its turn (admit)
            if not (self.pi and self.pi.poll() is None) and (any(w[0] is self for w in WAITING) or not room(self)):
                WAITING.append((self, self.chat_id, message, page))
                self.emit({"type": "note", "chat": True, "text": f"All {MAX_AGENTS} agents are answering other questions:"
                                                                 " yours starts as soon as one is free."})
                self.emit({"type": "busy", "on": True})
                return
            self.ask(message, page)

    def unwait(self) -> bool:
        """Stop pressed while the question waits for an agent: it goes."""
        with AGENTS:
            n = len(WAITING)
            WAITING[:] = [w for w in WAITING if w[0] is not self]
            return len(WAITING) < n

    def ask(self, message: str, page: str) -> None:
        """The question to this chat's agent, started first if it isn't running (in a slot: chat, admit)."""
        if not self.pi or self.pi.poll() is not None:
            env = {**os.environ, "ABI_TOKEN": self.token or abi.token(), "ABI_APP_URL": self.url, "ABI_APP_PROJECT": self.project,
                   "ABI_APP_AGENT_KEY": self.agent_key, "ABI_APP_CLIENT": self.hid}
            session = str(self.chats_dir / f"{self.chat_id}.session.jsonl")  # Pi's own transcript: this chat's context
            import settings  # noqa: PLC0415 (sibling script): the admin's model and reasoning, also for the specialists
            conf = settings.load()
            model = conf["model"] or (f"server/{os.environ['ABI_PI_LLM_MODEL']}" if os.environ.get("ABI_PI_LLM_MODEL") else None)  # Settings, else the deployment's
            if not model:  # 1.2: no model ID of ABI's own; say where to set one rather than start an agent with none
                self.chat_note("me", message)
                self.emit({"type": "said", "text": message}, here=False)
                self.emit({"type": "answer", "text": NO_MODEL})
                self.chat_note("ai", NO_MODEL)
                return
            flags = ["--model", model, "--thinking", conf["thinking"]]
            env |= {"ABI_PI_MODEL": conf["model"] or "", "ABI_PI_THINKING": conf["thinking"]}
            (abi.ROOT / "logs").mkdir(exist_ok=True)  # a source checkout has none until now (Docker links it to /data/logs)
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
        """One line per model answer in logs/usage.jsonl (the provider's token counts and the cost Pi computed from the
        model's prices in Settings, 0 without): Settings shows the totals per person and model. No text of the chat goes in."""
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
            elif t == "extension_ui_request" and e.get("method") == "notify" and e.get("message"):
                self.emit({"type": "sys", "text": str(e["message"])[:500]})  # the harness's own word to the person (a yes that came too late)
            elif t == "extension_ui_request" and e.get("method") in ("select", "input", "editor"):
                self.send({"type": "extension_ui_response", "id": e["id"], "cancelled": True})
            elif t == "extension_ui_request" and e.get("method") == "setStatus" and e.get("statusKey") == "abi-load":
                self.emit({"type": "progress", "text": e.get("statusText") or ""})  # a big load's run (M3.2), on its step
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
                admit()  # this agent may now make room for a waiting question
        if self.busy and self.pi is proc:  # the agent ended without an answer (it couldn't start, or it crashed): say so
            self.busy = False
            self.emit({"type": "busy", "on": False})
            self.emit({"type": "answer", "text": "The agent stopped before it could answer (details in logs/pi-stderr.log). Try again; if it keeps happening, check the model settings (Govern › Settings)."})
        admit()


HUBS: dict[str, Hub] = {}
USAGE_LOCK = threading.Lock()
NO_MODEL = ("No AI model is set up yet. An admin picks one in Govern › Settings › Models (a provider and its default"
            " model), or the server sets ABI_LLM_BASE, ABI_LLM_KEY and ABI_LLM_MODEL in its .env.")
MAX_AGENTS = int(os.environ.get("ABI_MAX_AGENTS", "8"))  # the chats' Pi processes running at once (M3.3)
AGENTS = threading.Lock()  # a slot is taken or given only under it
WAITING: list[tuple] = []  # (hub, chat ID, message, page): questions waiting for a slot, first asked first


def room(hub: Hub) -> bool:
    """A slot for hub's agent (AGENTS held): fewer than MAX_AGENTS run, or the one idle longest stops for it (its chat
    starts again from its own session the next time, as after the idle sweep)."""
    running = [h for h in list(HUBS.values()) if h is not hub and h.pi and h.pi.poll() is None]
    mine = [h for h in running if hub.user and h.user == hub.user]  # one person (any tabs) runs at most half (M3 review)
    if len(mine) < max(1, MAX_AGENTS // 2) and len(running) < MAX_AGENTS:
        return True
    if quiet := min((h for h in (mine if len(mine) >= max(1, MAX_AGENTS // 2) else running) if not h.busy),
                    key=lambda h: h.last, default=None):
        quiet.pi.terminate()
        quiet.pi = None
    return quiet is not None


def admit() -> None:
    """An answer ended or an agent stopped: waiting questions start in the slots now free, first asked first (one asked
    in a chat the person has since left goes)."""
    with AGENTS:
        i = 0
        while i < len(WAITING):
            hub, cid, message, page = WAITING[i]
            if hub.chat_id == cid and not (hub.pi and hub.pi.poll() is None) and not room(hub):
                i += 1  # its person runs their share (room): the next one's turn
                continue
            WAITING.pop(i)
            if hub.chat_id == cid:
                try:
                    hub.ask(message, page)
                except Exception as e:  # noqa: BLE001 (this thread reads another chat's agent: say it in this one)
                    hub.emit({"type": "busy", "on": False})
                    hub.emit({"type": "note", "chat": True, "text": f"The agent couldn't start ({type(e).__name__}). Try again."})


def usage_totals(days: int = 30, f: Path | None = None) -> dict:
    """logs/usage.jsonl summed per person and per model since `days` ago (answers, tokens, cost, failed answers, and the
    answers with tokens but no cost: their model has no prices in Settings, so Pi computed none)."""
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
            row = by[k].setdefault(r.get(k) or "?", {"answers": 0, "failed": 0, "input": 0, "output": 0, "cache": 0, "cost": 0.0, "unpriced": 0})
            row["answers"] += 1
            row["failed"] += bool(r.get("error"))
            row["input"] += r.get("input", 0)
            row["output"] += r.get("output", 0)
            row["cache"] += r.get("cacheRead", 0) + r.get("cacheWrite", 0)
            row["cost"] = round(row["cost"] + r.get("cost", 0), 6)
            row["unpriced"] += not r.get("cost") and any(r.get(t) for t in ("input", "output", "cacheRead", "cacheWrite"))
    return {"days": days, **{k: [{"name": n, **v} for n, v in sorted(d.items(), key=lambda x: -x[1]["output"])] for k, d in by.items()}}
_SNAP: dict[tuple, dict] = {}  # (project, its version here, role, what of the rules applies) -> the page data of all alike
_FLIGHT: dict[tuple, Future] = {}  # the computations under way: one per key, the others asking wait for it
_GEN: dict[str, int] = {}  # project -> version: changed() moves it on, so nothing computed before is served after
_SNAP_LOCK = threading.Lock()  # these three only, never held while computing
_PAGES = threading.BoundedSemaphore(4)  # page data computed at once, each with AT_ONCE ABI calls under way


class _Moved(Exception):
    """The project or the person's rules changed while page data was computed for them: it is kept for nobody."""


def seen_as(project: str) -> tuple:
    """(role, rules) of the person this thread acts as (abi.as_user), as ABI says now: their role in the project and,
    when row-level or column security restricts them, the rules as they apply to them (rls_rules: their own values,
    the hidden columns); rules None for those who read every row and column."""
    role = abi._open("GET", f"/api/projects/{project}/me")[1].get("role")
    rules = role and abi.call("rls_rules", project=project)  # an error fails the page: never another's rows instead
    return role, rules if rules and rules.get("restricted") else None


def _once(key: tuple, compute, still) -> dict:
    """Single flight: the first asking computes, as themselves; the others with the same key wait for it. It is kept,
    and given to those waiting, only when still() holds after (the version and the person's rules as when the key was
    made); else they get _Moved and ask again."""
    with _SNAP_LOCK:
        if key in _SNAP:
            return _SNAP[key]
        if mine := key not in _FLIGHT:
            _FLIGHT[key] = Future()
        job = _FLIGHT[key]
    if not mine:
        return job.result()
    try:
        with _PAGES:
            out = compute()
        same = still()
    except BaseException as e:
        with _SNAP_LOCK:
            _FLIGHT.pop(key, None)
        job.set_exception(e)
        raise
    with _SNAP_LOCK:
        _FLIGHT.pop(key, None)
        if same:
            _SNAP[key] = out
    job.set_result(out) if same else job.set_exception(_Moved())
    return out  # computed as this person: theirs either way


def cached(project: str, who: str = "") -> dict:
    """The page's data for this person (abi.as_user). People of one role who read every row and column share one
    computation. Those row-level or column security restricts share the rest of the page and get the reports' rows
    and their rules read as them, shared only with people the rules give the same (seen_as). Without a role: their own."""
    while True:
        gen, (role, rules) = _GEN.get(project, 0), seen_as(project)
        still = lambda: (_GEN.get(project, 0), *seen_as(project)) == (gen, role, rules)  # noqa: E731
        dest = abi.folder(project)
        try:
            if not rules:
                return _once((project, gen, role, None if role else who), lambda: snapshot(dest, project), still)
            base = _once((project, gen, role, ""), lambda: snapshot(dest, project, visuals=False), still)
            alike = hashlib.sha256(json.dumps(rules, sort_keys=True, default=str).encode()).hexdigest()
            return _once((project, gen, role, alike), lambda: own(project, base, rules), still)
        except _Moved:  # computed across a change of the project or of the person's rules
            continue


def own(project: str, base: dict, rules: dict) -> dict:
    """A restricted person's page: what every restricted person sees (base, its rows read by none), with the reports'
    rows read as this person and their own rules."""
    with Reads() as ex:
        return base | {"reports": report_data(project, [r["spec"] for r in base["reports"]], base["measures"], ex), "rls": rules,
                       "ontology": read("ontology", project) or {}}  # a type's properties leave out the columns hidden from this person


def changed(project: str) -> None:
    """ABI changed the project: once the folder has its commits, page data computed before is never served again (this
    project's and its published copy's; other projects' stay) and the open pages of the project get the new data."""
    def push(hub):
        try:
            hub.push_data()
        except Exception as e:  # noqa: BLE001 (a failed refresh keeps the page as it was)
            hub.emit({"type": "note", "text": f"Couldn't refresh: {type(e).__name__}"})

    def go():
        try:
            abi.sync(project)  # pull ABI's commits, re-render the folder views (writes app.html too)
        except Exception as e:  # noqa: BLE001
            for hub in list(HUBS.values()):
                hub.emit({"type": "note", "text": f"Couldn't refresh: {type(e).__name__}"})
            return
        both = (project, pub_id(project))
        with _SNAP_LOCK:
            for p in both:
                _GEN[p] = _GEN.get(p, 0) + 1
            for k in [k for k in _SNAP if k[0] in both]:
                _SNAP.pop(k, None)
        for hub in [h for h in list(HUBS.values()) if h.clients and h.project == project]:
            threading.Thread(target=push, args=(hub,), daemon=True).start()
    threading.Thread(target=go, daemon=True).start()


LOOPBACK = re.compile(r"^(localhost|127\.\d+\.\d+\.\d+|\[?::1\]?|0\.0\.0\.0)(:\d+)?$", re.I)


def public_base(public: str, host: str, proto: str = "") -> str:
    """The address other people open this app at, for a link to share: ABI_PUBLIC_URL when set, else the request's own
    Host; never a loopback address (nobody else can open it): then "" and the page says to set ABI_PUBLIC_URL."""
    if public and not LOOPBACK.match(urllib.parse.urlsplit(public).netloc):
        return public.rstrip("/")
    host = host.strip()
    if not host or LOOPBACK.match(host):
        return ""
    return f"{'https' if proto.lower() == 'https' else 'http'}://{host}"


def source_url() -> str:
    """AGPL-3.0 §13: where everyone using this app gets its source; a changed build sets ABI_SOURCE_URL to its own."""
    return os.environ.get("ABI_SOURCE_URL") or "https://github.com/Himanshu8881212/abi-pi"


SIGNIN = """<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Sign in · ABI</title>
<style>%FONTS%:root{--bg:#f7f7f5;--card:#fff;--ink:#0b0b0b;--mute:#62625e;--line:#e4e4e1;--sig:#ff4f1f;color-scheme:light}@media (prefers-color-scheme:dark){:root{--bg:#0b0b0b;--card:#141414;--ink:#f2f2ef;--mute:#9b9b96;--line:#2a2a28;--sig:#ff6a3d;color-scheme:dark}}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--ink);font:14px/1.5 "Inter Tight",system-ui,sans-serif;-webkit-font-smoothing:antialiased}form{display:flex;flex-direction:column;gap:6px;width:min(360px,calc(100vw - 32px));background:var(--card);border:1px solid var(--line);border-radius:8px;padding:32px}b{display:flex;align-items:center;gap:8px;font:700 20px "Inter Tight",sans-serif;letter-spacing:-.02em;margin-bottom:4px}b::before{content:"";width:12px;height:12px;background:var(--sig);border-radius:2px}label{font-size:12px;font-weight:500;color:var(--mute);margin-top:10px}input,button{font:inherit;padding:10px 12px;border:1px solid var(--line);border-radius:4px;background:var(--card);color:var(--ink)}input:focus{outline:none;border-color:var(--ink);box-shadow:0 0 0 3px color-mix(in srgb,var(--sig) 22%,transparent)}button{margin-top:16px;background:var(--ink);border-color:var(--ink);color:var(--card);font-weight:500;cursor:pointer}button.alt{margin-top:8px;width:100%;background:var(--card);color:var(--ink);border-color:var(--line)}#sso{display:contents}a{color:var(--mute);font-size:12px;margin-top:10px}[hidden]{display:none}.m{min-height:1.4em;color:var(--sig);font-size:13px}small{color:var(--mute)}</style>
<form method=post action=/signin id=in><b>ABI</b><small>Sign in with your ABI account.</small><label for=n>Name or email</label>
<input id=n name=name value="%NAME%" autocomplete=username autofocus required><label for=p>Password</label>
<input id=p name=password type=password autocomplete=current-password required>%KEY%<label for=c %CODE%>Authenticator code</label>
<input id=c name=code inputmode=numeric autocomplete=one-time-code %CODE%><button>Sign in</button><span id=sso></span>
<a href=# id=fg>Forgot your password?</a><span class=m>%MSG%</span><a href="%SRC%" target=_blank rel=noopener>ABI-PI %VER% · source code (AGPL-3.0)</a></form>
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
</script>""".replace("%FONTS%", render.FONTS)  # the bundled fonts: the sign-in page asks no one else for anything
NO_PROJECT = """<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>ABI</title>
<body style="font:14px/1.5 system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;margin:0;background:#f7f7f5;color:#0b0b0b">
<div style="max-width:340px"><b>Signed in as %NAME%</b><p>You have no project yet. Ask a workspace owner to give you a role in one.</p>
<form method=post action=/signout><button>Sign out</button></form></div>"""
CODES = """<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Recovery codes · ABI</title>
<body style="font:14px/1.5 system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;margin:0;background:#f7f7f5;color:#0b0b0b">
<div style="max-width:360px"><b>Two-step sign-in is on</b><p>Your recovery codes: each signs you in once if you lose your authenticator app.
Save them now: they are shown only this once.</p><pre id=rc>%CODES%</pre>
<button onclick="navigator.clipboard.writeText(document.getElementById('rc').textContent).then(()=>this.textContent='Copied')">Copy</button>
<a href="/">I saved them: continue</a></div>"""



SECTIONS = ["Data", "Data Model", "Visualizations", "Export", "Automate", "Knowledge", "Govern"]  # Start is everyone's
ROUTE_SECTION = {"/edits/rows": "Data", "/upload": "Data", "/report/export": "Export", "/data/export": "Export", "/video/export": "Export",
                 "/packs/file": "Export", "/transfer/": "Export", "/vault/": "Knowledge", "/analyses": "Visualizations", "/monitor": "Visualizations", "/docs/pdf": "Knowledge",
                 "/report/email": "Export", "/inbox": "Visualizations"}
REPORT_ACTIONS = {"visual_data", "report_snapshot", "export_visual", "save_report", "delete_report"}
_SEC = {"Data": "add_data delete_source delete_table set_steps preview_steps refresh edit_cells approve_edit reject_edit undo_edits create_input_table",
        "Data Model": "add_relationship remove_relationship set_measure delete_measure set_glossary_term delete_glossary_term save_dataset delete_dataset save_model delete_model decide_same",
        "Visualizations": "save_report delete_report", "Export": "export_visual report_snapshot", "Govern": "commit undo set_rls_rule",
        "Automate": "save_flow delete_flow enable_flow run_flow save_ml_model delete_ml_model train_model score_model run_optimizer save_tool publish_tool"
                    " delete_tool run_tool test_tool save_agent publish_agent delete_agent run_agent test_agent"}
ACTION_SECTION: dict[str, tuple] = {a: (sec,) for sec, acts in _SEC.items() for a in acts.split()} | {
    "set_table": ("Data", "Data Model"), "run_object_action": ("Data Model", "Visualizations")}  # an object page's button, a report's form card


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


def monitor_check(c: dict) -> dict:
    """One of Monitor's checks as monitor.json keeps it (its id, else one from its name), or ValueError.
    measure: {measure, op, against: value|measure|prev, value | other | period (calendar column), by?, filters?,
    to? ({property, link?}: who an alert per object is for, when `by` is an object type's key)}; fresh: {table, hours}; flow: {flow}."""
    cid = str(c.get("id") or "")
    if not re.fullmatch(r"[a-z0-9_]{1,60}", cid):
        cid = re.sub(r"[^a-z0-9]+", "_", str(c.get("name") or cid).lower()).strip("_")[:60]
    if not cid:
        raise ValueError("Give the check a name.")
    kind, ag = c.get("kind"), c.get("against", "value")
    need = {"measure": ["measure", "op"] + {"value": ["value"], "measure": ["other"], "prev": ["value", "period"]}.get(ag, ["?"]),
            "fresh": ["table", "hours"], "flow": ["flow"]}.get(kind)
    if need is None or any(c.get(k) in (None, "") for k in need) or (kind == "measure" and c["op"] not in OPS):
        raise ValueError(f"A {kind or '?'} check needs {', '.join(need or ['kind: measure, fresh or flow'])}"
                         + (f"; op is one of {' '.join(OPS)}" if kind == "measure" else "") + ".")
    keep = ("measure", "op", "against", "value", "other", "period", "skip_latest", "by", "filters", "to", "table", "hours", "flow")
    return {"id": cid, "name": str(c.get("name") or cid)[:120], "kind": kind} | {k: c[k] for k in keep if c.get(k) not in (None, "")}


def agent_monitor(project: str, body: dict, by: str) -> list:
    """abi_monitor: list, save or delete one of Monitor's checks (monitor.json "checks", what the page evaluates)."""
    mon = render.j(app_dir(project) / "monitor.json", {}) or {}
    checks = mon.get("checks") or []
    if body.get("action") == "delete":
        checks = [c for c in checks if c.get("id") != body.get("id")]
    elif body.get("action") == "save":
        c = monitor_check(body.get("check") or {})
        checks = [x for x in checks if x.get("id") != c["id"]] + [c]
        if len(checks) > 50:
            raise ValueError("At most 50 checks.")
    if body.get("action") in ("save", "delete"):
        app_save(project, "monitor.json", mon | {"checks": checks}, by)
    return checks


OPW = {"<": "is below", "<=": "is at most", ">": "is above", ">=": "is at least", "=": "equals", "!=": "is not"}
FLOW_SCHEMA = "https://schema.abi.invalid/v1/flow.json"


def monitor_condition(c: dict) -> dict | None:
    """A measure check as the condition ABI evaluates (check_condition, a flow's data_condition trigger): the engine
    computes it, a previous-period change too, so the page's verdict and an alert's figures are its rows. None for a
    fresh or flow check (the page evaluates those) or a broken one."""
    if c.get("kind") != "measure" or not c.get("measure") or c.get("op") not in OPS:
        return None
    ag, other = c.get("against", "value"), c.get("other") if c.get("against") == "measure" else None
    try:
        return ({"query": {"measures": [c["measure"], *([other] if other else [])], "filters": c.get("filters") or [],
                           "dimensions": [c["by"]] if c.get("by") and ag != "prev" else []},
                 "measure": c["measure"], "op": c["op"], "value": 0.0 if other else float(c.get("value"))}
                | ({"other": other} if other else {}) | ({"change": True} if ag == "prev" else {})
                | ({"period": c["period"], "skip_latest": bool(c.get("skip_latest"))} if c.get("period") else {}))
    except (TypeError, ValueError):
        return None


def monitor_flow(c: dict, names: dict, email: str | None = None, types: list | None = None) -> dict | None:
    """A measure check's flow: when its condition starts to hold (the engine checks it every minute, no browser open),
    a message in the project's inbox and, if asked, an email, both carrying the check's rows; the text is the check's
    own words (its name and rule). `names`: measure ID -> display name. 1.2.6 M8, `types` (the ontology's object types):
    a check whose `by` is a type's key alerts once for each object (the trigger's `per`), to the people `to` finds
    through it; ABI sends only to project members who see that object, each with its rows."""
    if not (cond := monitor_condition(c)):
        return None
    n = lambda m: names.get(m) or m  # noqa: E731
    per = (c.get("period") or "").split(".")[-1]
    # ponytail: a type keyed by one column only (a check has one `by`); a flow file takes any key
    obj = next((t for t in types or [] if t.get("key") and cond["query"]["dimensions"] == [f"{t['table']}.{k}" for k in t["key"]]), None)
    rule = (f"the change of {n(c['measure'])} from one {per} to the next {OPW[c['op']]} {cond['value']:g}%" if cond.get("change")
            else f"{n(c['measure'])}{' for any ' + c['by'].split('.')[-1] if c.get('by') and not obj else ''} {OPW[c['op']]} "
            + (n(cond["other"]) if cond.get("other") else f"{cond['value']:g}") + (f" in the latest {per}" if per else ""))
    rule += (" (completed periods)" if c.get("skip_latest") and per else "") + "".join(
        f", where {f['column']} = {f['value']}" for f in cond["query"]["filters"])
    text, subject, data, more = f"Monitor: {c['name']}. It alerts when {rule}.", f"Monitor: {c['name']}", cond, {}
    if obj:  # the alert names its object and carries that object's row, by its title
        text = f"Monitor: {c['name']}: {{{{ data.trigger.title }}}}. It alerts for each {obj['name'].lower()} when {rule}."
        subject += ": {{ data.trigger.title }}"
        data = cond | {"query": cond["query"] | {"dimensions": [f"{obj['table']}.{obj['title']}"]}}
        more = {"per": obj["id"]} | ({"recipients": c["to"]} if c.get("to") else {})
    steps = [{"id": "inbox", "type": "action", "action": "post_message", "input": {"channel": "inbox", "text": text, "data": data}}]
    if email:
        steps.append({"id": "email", "type": "action", "action": "send_email", "input": {
            "to": [email], "subject": subject[:300], "body": text + " Its figures are below and attached.", "data": data}})
    return {"$schema": FLOW_SCHEMA, "id": ("monitor_" + c["id"])[:63], "name": f"Monitor: {c['name']}", "enabled": True,
            "description": "Made by Monitor: ABI checks it every minute and posts to the inbox when it starts to hold.",
            "trigger": {"type": "data_condition", **cond, **more}, "steps": steps}


def report_mail_flow(rep: dict, to: list, days: list, at: str, tz: str) -> dict:
    """Email this report: a schedule (days 0-6 from Sunday, at HH:MM in time zone `tz`) and ABI's send_email with every
    page as one PDF, rendered when it sends as the flow's owner (their row-level security), from the account mail."""
    if not to or len(to) > 50 or not all(isinstance(a, str) and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", a) for a in to):
        raise ValueError("Give up to 50 email addresses.")
    if not days or not all(type(d) is int and 0 <= d <= 6 for d in days) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", at):
        raise ValueError("Pick at least one day and a time (HH:MM).")
    name = rep.get("name") or rep["id"]
    return {"$schema": FLOW_SCHEMA, "id": ("email_" + rep["id"])[:63], "name": f"Email {name}", "enabled": True,
            "description": f"Emails the {name} report as a PDF on a schedule; set on the report's page.",
            "trigger": {"type": "schedule", "cron": f"{int(at[3:])} {int(at[:2])} * * {','.join(map(str, sorted(set(days))))}", "timezone": tz},
            "steps": [{"id": "email", "type": "action", "action": "send_email", "input": {
                "to": to, "subject": name[:300], "body": f"The {name} report is attached as a PDF, as ABI rendered it when this email was sent.",
                "report": {"report": rep["id"], "pages": "all", "format": "pdf"}}}]}


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
    tries: dict[tuple, int] = {}  # wrong sign-ins per client address and name (ABI throttles too, but sees only this server)

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

        def signin_page(self, msg: str = "", name: str = "", code: bool = False, key: dict | None = None) -> None:
            """The sign-in form; `key`: an enrolment's key (ABI's 403 mfa=setup), in this no-store answer only."""
            shown = (f"<small>Key: <code>{html.escape(key['secret'])}</code> · <a href=\"{html.escape(key['uri'])}\">Open in an"
                     " authenticator app</a></small>") if key else ""
            self.reply(200, SIGNIN.replace("%SRC%", html.escape(source_url())).replace("%VER%", html.escape(VERSION))
                       .replace("%MSG%", html.escape(msg)).replace("%NAME%", html.escape(name))
                       .replace("%CODE%", "" if code else "hidden").replace("%KEY%", shown, 1).encode(), "text/html; charset=utf-8")

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

        def mcp(self, raw: bytes | None) -> None:
            """M4.5: outside MCP clients with a personal token get ABI's read-only MCP (/api/mcp/outside) as the token's
            person; the token goes on to ABI and nowhere else (no request log here). Host and Origin go along: ABI
            refuses a browser page of another origin."""
            h = {k: v for k in ("Authorization", "Content-Type", "Accept", "Mcp-Session-Id", "Mcp-Protocol-Version", "Origin",
                                "Host") if (v := self.headers.get(k))}
            try:
                r = urllib.request.urlopen(urllib.request.Request(abi.URL + "/api/mcp/outside", raw, h, method=self.command), timeout=300)
            except urllib.error.HTTPError as e:
                r = e
            with r:
                b = r.read()
                self.send_response(r.status)
                for k in ("Content-Type", "Mcp-Session-Id", "WWW-Authenticate", "Allow"):
                    if v := r.headers.get(k):
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(b)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(b)

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
            if route in ("/mcp", "/mcp/") and self.allowed():
                return self.mcp(None)
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
            if role_of(self.sess, p) == "owner" and p not in ROLES_SYNCED and abi.folder(pub_id(p)):  # an older install's copy: Viewer for all
                ROLES_SYNCED.add(p)
                publish_roles(p, pub_id(p))
            me = {"name": self.sess["name"] if self.sess else None, "admin": bool(not self.sess or self.sess["admin"]), "signin": signin,
                  "role": role_of(self.sess, p), **access(self.sess, p), "source": source_url(), "version": VERSION,
                  "novideo": video.missing(), "base": public_base(os.environ.get("ABI_PUBLIC_URL", ""), self.headers.get("Host", ""),
                                                                   self.headers.get("X-Forwarded-Proto", ""))}
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
                f = urllib.parse.parse_qs(raw.decode(errors="replace"))
                name, code = f.get("name", [""])[0].strip()[:254], (f.get("code", [""])[0].strip() or None)
                who = (self.client_address[0], name.lower())  # and name: behind HTTPS every browser comes from Caddy's address
                status, b = abi.login(name, f.get("password", [""])[0], code)
                f = None  # the password is not kept
                if status != 200:
                    if len(tries) > 10_000:  # ponytail: a flood of names forgets the counts; ABI's own throttle keeps its own
                        tries.clear()
                    tries[who] = tries.get(who, 0) + 1
                    time.sleep(min(20, 2 ** min(tries[who], 4)))
                    # mfa=setup: the tenant requires two-step sign-in and this account has none: enrol here (a new key each time)
                    return self.signin_page(b.get("message") or "Sign-in failed.", name, bool(b.get("mfa")),
                                            b if b.get("secret") and b.get("uri") else None)
                tries.pop(who, None)
                if codes := b.get("recovery_codes"):  # enrolled just now: its recovery codes, in this no-store answer only
                    self.send_response(200)
                    self.start(name, b["token"])
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(CODES.replace("%CODES%", html.escape("\n".join(codes))).encode())
                    return None
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
            if self.path.partition("?")[0] in ("/mcp", "/mcp/") and self.allowed():
                return self.mcp(raw)
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
                        if self.path == "/agent/monitor" and body.get("action") in ("save", "delete"):  # its flow would go stale
                            cid = monitor_check(body.get("check") or {})["id"] if body["action"] == "save" else str(body.get("id") or "")
                            if any(x.get("id") == ("monitor_" + cid)[:63] for x in (read("list_flows", p) or {}).get("flows") or []):
                                raise ValueError(f"Check '{cid}' alerts with the page closed (flow monitor_{cid}): the person changes or"
                                                 " deletes it on the Monitor page, which keeps its flow in step.")
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
            if self.path == "/mfa" and self.sess:  # your two-step sign-in, as you (ABI's mfa_status, start_, confirm_, disable_mfa)
                op, pw, cd = body.get("op"), str(body.get("password") or ""), str(body.get("code") or "")
                need = {"status": (), "start": (pw,), "confirm": (cd,), "off": (pw, cd)}.get(op)
                if need is None or not all(need) or len(pw) > 1024 or len(cd) > 64:  # checked here: never echoed back
                    return self.reply(400, b'{"error":"Your password, and the code from your authenticator app (or a recovery code) where asked."}')
                act, args = {"status": ("mfa_status", {}), "start": ("start_mfa", {"password": pw}), "confirm": ("confirm_mfa", {"code": cd}),
                             "off": ("disable_mfa", {"password": pw, "code": cd})}[op]
                return self.reply(200, json.dumps(run(act, args, None)).encode())  # the key and codes: this answer only
            if self.path.startswith("/admin/"):
                if self.sess and not self.sess["admin"]:
                    return self.reply(403, b'{"error":"Users & access, Settings and database logins are for workspace owners."}')
                return self.reply(*admin_route(hub, self.path.removeprefix("/admin/"), body))
            # the page: a local Host (no DNS rebinding) and the per-start key (a custom header other sites can't send)
            if not secrets.compare_digest(self.headers.get("X-Abi-App", ""), key):
                return self.reply(403, b'{"error":"forbidden"}')
            if self.path == "/tokens":  # M4.5: your personal tokens (a workspace owner: everyone's); a new one is shown once
                op = body.get("op")
                out = (run("create_personal_token", {"name": str(body.get("name") or "")[:100], "days": int(body.get("days") or 90)}, None)
                       if op == "mint" else run("revoke_personal_token", {"id": int(body.get("id") or 0)}, None) if op == "revoke"
                       else run("personal_tokens", {}, None))
                return self.reply(200, json.dumps(out, default=str).encode())
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
                ext, sheet = f.suffix.lower(), str(body.get("sheet") or "")
                try:
                    out = {"result": abi.upload(project, str(body.get("table")), f, abi.csv_options(f) if ext in (".csv", ".tsv") else None,
                                                {"sheet": sheet} if sheet and ext == ".xlsx" else None, wait=False)}  # the page shows a big file's run
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
            if self.path == "/monitor":  # one of Monitor's checks (monitor.json) and, for a measure check, its flow in ABI: the
                # engine checks it every minute and posts to the inbox (and emails, if asked) with no browser open
                if hub.mode == "viewer":
                    return self.reply(403, b'{"error":"Make monitors in Developer mode."}')
                try:
                    c = monitor_check(body.get("check") or {}) if body.get("action") == "save" else None
                except ValueError as e:
                    return self.reply(400, json.dumps({"error": str(e)}).encode())
                cid = c["id"] if c else str(body.get("id") or "")

                def save() -> list:  # the checks as the page holds them
                    out = agent_monitor(project, {"action": "save", "check": c} if c else {"action": "delete", "id": cid}, hub.user)
                    return [x | {"condition": monitor_condition(x)} for x in out]
                names = {m["id"]: m.get("name") for m in (read("semantic_model", project) or {}).get("measures") or [] if isinstance(m, dict)}
                types = (read("ontology", project) or {}).get("objects") or []  # 1.2.6 M8: a check for each object of a type
                if c and (f := monitor_flow(c, names, str(body.get("email") or "").strip() or None, types)):  # with its flow, after ABI's yes
                    return self.reply(200, json.dumps(run("save_flow", {"project": project, "flow": f}, None, save), default=str).encode())
                if any(x.get("id") == ("monitor_" + cid)[:63] for x in (read("list_flows", project) or {}).get("flows") or []):
                    return self.reply(200, json.dumps(run("delete_flow", {"project": project, "flow": ("monitor_" + cid)[:63]}, None, save), default=str).encode())
                out = save()
                changed(project)
                return self.reply(200, json.dumps({"result": out}).encode())
            if self.path == "/report/email":  # Email this report: a flow in ABI, a schedule and send_email with the report's PDF
                if hub.mode == "viewer":
                    return self.reply(403, b'{"error":"Set report emails in Developer mode."}')
                rep = next((r for r in (read("list_reports", project) or {}).get("reports") or [] if r.get("id") == body.get("report")), None)
                if not rep:
                    return self.reply(404, b'{"error":"No such report."}')
                if body.get("off"):
                    return self.reply(200, json.dumps(run("delete_flow", {"project": project, "flow": ("email_" + rep["id"])[:63]}, None, lambda: "off"), default=str).encode())
                try:
                    f = report_mail_flow(rep, body.get("to") or [], body.get("days") or [], str(body.get("time") or ""), str(body.get("timezone") or "UTC"))
                except ValueError as e:
                    return self.reply(400, json.dumps({"error": str(e)}).encode())
                return self.reply(200, json.dumps(run("save_flow", {"project": project, "flow": f}, None, lambda: "saved"), default=str).encode())
            if self.path == "/inbox":  # the project's inbox (flow posts, Monitor alerts, your subscriptions and test runs) and its
                # alerts feed (failed flows, data problems), as this person: ABI applies their row-level security. Flows post
                # in the project itself, so Viewer reads them there (everyone here has a role in it)
                try:
                    if ids := [int(x) for x in body.get("read") or []][:500]:  # never [] here: ABI reads that as "all"
                        abi.call("mark_notifications_read", ids=ids)
                    # ponytail: ABI's newest 100 across the person's projects, this one's kept; a project filter there if that's short
                    feed = [n for n in abi._open("GET", "/api/notifications")[1] if n.get("project") == project]
                    out = {"result": {"messages": abi.call("inbox", project=project)["messages"], "alerts": feed}}
                except abi.ActionError as e:
                    out = {"error": (e.body or {}).get("message") if isinstance(e.body, dict) else str(e.body)}
                return self.reply(200, json.dumps(out, default=str).encode())
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
                if action == "run_object_action":  # an agent's object write is a proposal (ABI's own call); a person's Approve here would make it an active write
                    return self.reply(403, json.dumps({"error": "Approve can't run an action on an object for you. Run it from the object's page, then decline this step."}).encode())
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
                elif hub.unwait():  # still waiting for an agent: the question goes
                    hub.emit({"type": "busy", "on": False})
                return self.reply(200, b"{}")
            if self.path.startswith("/confirm/"):
                cid = self.path.split("/")[-1]
                job = pending.pop(cid, None) if pending.get(cid, (None,) * 5)[4] == hub.hid else None
                if not job:
                    return self.reply(410, b'{"error":"That confirmation expired; try again."}')
                if hub.mode == "viewer" and job[0] not in ACCOUNT:  # asked in Developer, answered after the switch: Viewer changes nothing but your own account
                    return self.reply(403, json.dumps({"error": "Viewer mode is read-only. Switch to Developer in the top bar to change things."}).encode())
                return self.reply(200, json.dumps(run(*job[:4]), default=str).encode())  # job: action, args, token, after, hub
            action = self.path.removeprefix("/api/")
            if action not in ALLOWED or body.get("project") not in (project, pub_id(project)):  # Viewer's page names the published copy
                return self.reply(403, json.dumps({"error": f"'{action}' is not available here; use ABI's app."}).encode())
            if hub.mode == "viewer" and (action in WRITES or action in READS_ONLY):
                return self.reply(403, json.dumps({"error": "Viewer mode is read-only. Switch to Developer in the top bar to change things."}).encode())
            body = {**body, "project": hub.current()}  # Viewer reads the published copy
            out = run(action, body, None)
            if "result" in out and (action in WRITES or action == "load_runs" and body.get("run")  # the page saw a big load land
                                    and out["result"]["runs"][0]["status"] == "done"):
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
            tenants = [{k: t[k] for k in ("id", "name", "mfa_required")} for t in run("list_tenants", {}, None).get("result", {}).get("tenants") or []]
            return 200, json.dumps({"result": {"project": p, "users": users, "sections": SECTIONS, "published": bool(abi.folder(pub)), "tenants": tenants,
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
                            abi.call("set_role", project=pub, user=who, role=role)  # the same role: rules follow it
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
        if op == "mfa/require":  # a tenant's accounts sign in with a code; ABI ends the password sessions of those without one
            if not re.fullmatch(r"[\w-]{1,64}", str(body.get("tenant") or "")) or not isinstance(body.get("on"), bool):
                return 400, b'{"error":"A tenant and on or off."}'
            return 200, json.dumps(run("set_mfa_required", {"tenant": body["tenant"], "on": body["on"]}, None), default=str).encode()
        if op == "mfa/reset":  # a lost authenticator and codes (destructive: ABI asks the owner, then signs them out everywhere)
            if not who:
                return 400, b'{"error":"A person."}'
            return 200, json.dumps(run("reset_mfa", {"name": who}, None), default=str).encode()
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
        if op == "dbkey":  # a database source's login as two ABI secrets bound to its host (never shown again, never the agent's)
            tbl, host = str(body.get("table") or ""), str(body.get("host") or "").strip().lower()
            user, pw = str(body.get("user") or ""), str(body.get("password") or "")
            if not (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", tbl) and re.fullmatch(r"[a-z0-9.-]{1,253}", host) and user and pw):
                return 400, b'{"error":"A table id, the host, and the user and password of the login."}'
            name = db_secret(p, tbl)  # and the project's tenant's: a tenant's project resolves only those
            tenant = next((t["id"] for t in run("list_tenants", {}, None).get("result", {}).get("tenants") or [] if p in t["projects"]), None)
            for suffix, value in (("_USER", user), ("_PASSWORD", pw)):
                r = run("set_secret", {"name": name + suffix, "value": value, "allowed_hosts": [host], **({"tenant": tenant} if tenant else {})}, None)
                if "error" in r:
                    return 200, json.dumps({"error": r["error"]}).encode()
            return 200, json.dumps({"result": {"user": f"${{secret:{name}_USER}}", "password": f"${{secret:{name}_PASSWORD}}"}}).encode()
        if op == "sso":  # the sign-in providers people may use (OIDC), and whether links can reach this app
            st, b, _ = abi.raw("GET", "/api/oidc/providers")
            public = os.environ.get("ABI_PUBLIC_URL", "")
            import settings  # noqa: PLC0415
            return 200, json.dumps({"result": {"providers": json.loads(b) if st == 200 else [], "public_url": public,
                                               "reachable": bool(public) and not re.match(r"https?://(127\.|localhost)", public),
                                               "callback": f"{public_base(public, '')}/api/oidc/<id>/callback", "mail": settings.load().get("mail")}}).encode()
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
            if action == "docs" and str(args.get("project", "")).endswith("_pub"):
                out["result"] = viewer_docs(out["result"])
            if after:
                out["result"] = after()
            if (token and action in WRITES) or after:
                changed(str(args.get("project") or default).removesuffix("_pub"))
            return out
        except abi.ActionError as e:
            b = e.body if isinstance(e.body, dict) else {"message": str(e.body)}
            if e.status == 409 and b.get("confirm_token") and (token is None or action == "decide_same"):  # a decision whose rows changed meanwhile asks again (api() shows the new question)
                cid = secrets.token_urlsafe(12)
                pending[cid] = (action, args, b["confirm_token"], after, getattr(asking, "hid", None))  # the page never sees the token
                preview = json.dumps(b["preview"], indent=1, default=str) if b.get("preview") else ""
                if action != "run_object_action":  # an object action's preview is read as JSON by the page (actAsk): whole, its inputs are bounded by their schema
                    preview = preview[:4000]
                return {"confirm": {"id": cid, "message": b.get("message", "Confirm?"), "preview": preview}}
            at = lambda x: f"{x.get('file')} {x.get('path')}: " if action == "save_report" and x.get("file") else ""  # noqa: E731  a report is written as JSON: where in it
            detail = "\n".join((f"{at(x)}{x['error']} {x.get('fix') or ''}" if x.get("error") else  # ABI's checks; a 422 names the field
                                 f"{'.'.join(map(str, x.get('loc') or []))}: {x.get('msg')}").strip() for x in b.get("errors") or [] if isinstance(x, dict))
            out = {"error": "\n".join(filter(None, [b.get("message"), detail])) or json.dumps(b)[:500]}
            if action == "run_object_action" and e.status == 422:  # an input that doesn't fit: which one and the rule, for the form to mark the field
                out["fields"] = [[str(x.get("path") or ""), str(x.get("error") or "")] for x in b.get("errors") or [] if isinstance(x, dict)]
            return out

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
                            len((abi.call("changes", project=p) or {}).get("files") or []),
                            # and the data (M3.2): the worker loads the published copy after a Publish, and files that changed
                            *(abi.call("list_snapshots", project=x, limit=1)["rows"][0][0] for x in (p, pub_id(p)) if abi.folder(x)))
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

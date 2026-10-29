#!/usr/bin/env python3
"""HTML views of a project folder, written next to it in <workspace>/_views/<project>/ (never inside the project
folder: ABI's import refuses unknown files there).

  index.html          project explorer: sources, tables and steps, model diagram, measures (MEL), glossary, reports,
                      ML, flows, agents, tools, security, history; each card names the JSON file to edit
  reports/<id>.html   interactive report: pages, KPIs and charts (hover, sort, table/chart toggle, CSV download)

Definitions come from the folder; every number comes from ABI's `visual_data` rows (principle 7). Self-contained
files, no CDN, open them straight from disk.

    python scripts/render.py <project folder>
"""
import html
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1f;--mute:#6e6e73;--line:#e3e3e0;--acc:#2f6fdf;--acc2:#e07a2f;--ok:#2e9d5b;--bad:#c9473b}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--card:#1e1e21;--ink:#ececee;--mute:#9a9aa0;--line:#2e2e33;--acc:#6b9cff;--acc2:#f0a060}}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 -apple-system,system-ui,Segoe UI,sans-serif;background:var(--bg);color:var(--ink)}
header{padding:18px 24px;border-bottom:1px solid var(--line);display:flex;gap:16px;align-items:baseline;flex-wrap:wrap}
header h1{font-size:20px;margin:0}header .sub{color:var(--mute)}main{padding:16px 24px;max-width:1400px;margin:auto}
nav.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 14px}nav.tabs button{border:1px solid var(--line);background:var(--card);color:var(--ink);padding:6px 12px;border-radius:999px;cursor:pointer}
nav.tabs button.on{background:var(--acc);color:#fff;border-color:var(--acc)}
.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:12px}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;min-width:0}
.card h3{margin:0 0 8px;font-size:14px;display:flex;justify-content:space-between;gap:8px}.card h3 .tools button{font-size:11px;border:1px solid var(--line);background:none;color:var(--mute);border-radius:6px;cursor:pointer;margin-left:4px}
.kpi .v{font-size:30px;font-weight:650;letter-spacing:-.5px}.kpi .l{color:var(--mute)}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:5px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{cursor:pointer;user-select:none;color:var(--mute);font-weight:600}td.n{text-align:right;font-variant-numeric:tabular-nums}
.scroll{max-height:360px;overflow:auto}svg text{fill:var(--mute);font-size:11px}.tip{position:fixed;pointer-events:none;background:var(--ink);color:var(--bg);padding:4px 8px;border-radius:6px;font-size:12px;display:none;z-index:9}
code,pre{font:12px/1.4 ui-monospace,Menlo,monospace}pre{background:var(--bg);padding:8px;border-radius:8px;overflow:auto;max-height:300px}
.file{color:var(--mute);font-size:12px}.chip{display:inline-block;padding:2px 8px;border-radius:999px;background:var(--bg);border:1px solid var(--line);margin:2px;font-size:12px}
section{margin-bottom:22px}section>h2{font-size:16px;margin:0 0 8px}input.search{padding:7px 10px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--ink);width:260px}
a{color:var(--acc)}.empty{color:var(--mute);font-style:italic}.steps li{margin:2px 0}.ok{color:var(--ok)}.bad{color:var(--bad)}
"""

JS = r"""
const tip=document.querySelector('.tip');
function showTip(e,t){tip.textContent=t;tip.style.display='block';tip.style.left=(e.clientX+12)+'px';tip.style.top=(e.clientY+12)+'px'}
function hideTip(){tip.style.display='none'}
function fmt(v,f){if(v===null||v===undefined)return'—';if(typeof v!=='number')return String(v);f=f||{};
 const o={maximumFractionDigits:f.decimals??2};if(f.compact)o.notation='compact';
 if(f.style==='percent')return new Intl.NumberFormat(undefined,{...o,style:'percent'}).format(v);
 if(f.style==='currency'&&f.currency)return new Intl.NumberFormat(undefined,{...o,style:'currency',currency:f.currency}).format(v);
 return new Intl.NumberFormat(undefined,o).format(v)}
const NS='http://www.w3.org/2000/svg';function el(t,a,p){const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);p&&p.appendChild(e);return e}
const COL=['var(--acc)','var(--acc2)','#7f5bd5','#2e9d5b','#c9473b','#8a8f98'];
function table(box,d){const t=document.createElement('table');const cols=d.columns;let sort=null,desc=false;
 function draw(){t.innerHTML='';const h=t.insertRow();cols.forEach((c,i)=>{const th=document.createElement('th');th.textContent=(d.measures[c]||c)+(sort===i?(desc?' ▼':' ▲'):'');th.onclick=()=>{desc=sort===i?!desc:true;sort=i;draw()};h.appendChild(th)});
  let rows=d.rows.slice();if(sort!==null)rows.sort((a,b)=>((a[sort]>b[sort])-(a[sort]<b[sort]))*(desc?-1:1));
  rows.forEach(r=>{const tr=t.insertRow();r.forEach((v,i)=>{const td=tr.insertCell();td.textContent=fmt(v,d.format[cols[i]]);if(typeof v==='number')td.className='n'})})}
 draw();const s=document.createElement('div');s.className='scroll';s.appendChild(t);box.appendChild(s)}
function chart(box,d){const nd=d.columns.length-Object.keys(d.measures).length;const ms=d.columns.slice(nd);const type=d.type;
 if(type==='kpi'||(nd===0&&d.rows.length===1)){box.innerHTML='';ms.forEach(m=>{const k=document.createElement('div');k.className='kpi';k.innerHTML=`<div class="v">${fmt(d.rows[0]?.[d.columns.indexOf(m)],d.format[m])}</div><div class="l">${d.measures[m]||m}</div>`;box.appendChild(k)});return}
 if(nd!==1||!['bar','line','pie','combo','funnel','treemap','waterfall','scatter','map'].includes(type)){table(box,d);return}
 const W=box.clientWidth||600,H=260,L=60,B=40,svg=el('svg',{viewBox:`0 0 ${W} ${H}`,width:'100%'},box);
 const rows=d.rows.filter(r=>ms.some((m,i)=>typeof r[nd+i]==='number'));const vals=rows.flatMap(r=>ms.map((m,i)=>r[nd+i]||0));
 const max=Math.max(0,...vals),min=Math.min(0,...vals),y=v=>H-B-(v-min)/((max-min)||1)*(H-B-10);
 for(let g=0;g<=4;g++){const v=min+(max-min)*g/4;el('line',{x1:L,x2:W-8,y1:y(v),y2:y(v),stroke:'var(--line)'},svg);el('text',{x:L-6,y:y(v)+4,'text-anchor':'end'},svg).textContent=fmt(v,{compact:true,decimals:1})}
 const n=rows.length,step=(W-L-8)/Math.max(n,1),lab=Math.max(1,Math.ceil(n/Math.max(1,Math.floor((W-L)/80))));
 rows.forEach((r,i)=>{if(i%lab===0){const s=String(r[0]??'—');el('text',{x:L+step*i+step/2,y:H-B+14,'text-anchor':'middle'},svg).textContent=s.length>11?s.slice(0,10)+'…':s}});
 if(type==='line'){ms.forEach((m,j)=>{const pts=rows.map((r,i)=>[L+step*i+step/2,y(r[nd+j]||0)]);el('polyline',{points:pts.map(p=>p.join(',')).join(' '),fill:'none',stroke:COL[j%6],'stroke-width':2},svg);
   pts.forEach((p,i)=>{const c=el('circle',{cx:p[0],cy:p[1],r:Math.max(2,Math.min(4,step/3)),fill:COL[j%6],opacity:.01},svg);c.onmousemove=e=>{c.setAttribute('opacity',1);showTip(e,`${rows[i][0]} · ${d.measures[m]||m}: ${fmt(rows[i][nd+j],{...d.format[m],compact:false})}`)};c.onmouseleave=()=>{c.setAttribute('opacity',.01);hideTip()}})});return}
 const bw=step/ms.length*.8;rows.forEach((r,i)=>ms.forEach((m,j)=>{const v=r[nd+j]||0;const b=el('rect',{x:L+step*i+step*.1+bw*j,y:Math.min(y(v),y(0)),width:Math.max(bw,1),height:Math.abs(y(v)-y(0)),fill:COL[j%6],rx:2},svg);
  b.onmousemove=e=>{b.setAttribute('opacity',.7);showTip(e,`${r[0]} · ${d.measures[m]||m}: ${fmt(v,{...d.format[m],compact:false})}`)};b.onmouseleave=()=>{b.setAttribute('opacity',1);hideTip()}}))}
function csv(d){const q=v=>v===null?'':/[",\n]/.test(String(v))?'"'+String(v).replace(/"/g,'""')+'"':String(v);
 const blob=new Blob([[d.columns.join(','),...d.rows.map(r=>r.map(q).join(','))].join('\n')],{type:'text/csv'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=d.id+'.csv';a.click()}
function render(R){const tabs=document.querySelector('nav.tabs'),grid=document.querySelector('.grid');
 function page(p){grid.innerHTML='';[...tabs.children].forEach(b=>b.classList.toggle('on',b.dataset.id===p.id));
  p.visuals.slice().sort((a,b)=>(a.layout.y-b.layout.y)||(a.layout.x-b.layout.x)).forEach(v=>{const c=document.createElement('div');c.className='card';c.style.gridColumn=`span ${Math.min(12,v.layout.w||6)}`;
   c.innerHTML=`<h3><span>${v.title||v.id}</span><span class="tools"><button data-a="t">table</button><button data-a="c">csv</button><button data-a="h">how</button></span></h3><div class="body"></div><pre class="how" hidden></pre>`;grid.appendChild(c);
   const body=c.querySelector('.body');if(v.text!==undefined){c.querySelector('.tools').remove();body.textContent=v.text;body.style.whiteSpace='pre-wrap';return}if(v.error){body.innerHTML=`<div class="bad">${v.error}</div>`;return}
   let asTable=false;const draw=()=>{body.innerHTML='';asTable?table(body,v.data):chart(body,v.data)};requestAnimationFrame(draw);
   c.querySelector('[data-a=t]').onclick=()=>{asTable=!asTable;draw()};c.querySelector('[data-a=c]').onclick=()=>csv(v.data);
   c.querySelector('[data-a=h]').onclick=()=>{const h=c.querySelector('.how');h.hidden=!h.hidden;h.textContent=v.how}})}
 R.pages.forEach(p=>{const b=document.createElement('button');b.textContent=p.name||p.id;b.dataset.id=p.id;b.onclick=()=>page(p);tabs.appendChild(b)});page(R.pages[0])}
"""


def esc(x) -> str:
    return html.escape(str(x))


def j(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def shell(title: str, sub: str, body: str, script: str = "") -> str:
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{esc(title)}</title><style>{CSS}</style></head><body><div class=tip></div><header><h1>{esc(title)}</h1>"
            f"<span class=sub>{sub}</span></header><main>{body}</main>{script}</body></html>")


def report_html(dest: Path, project: str, rid: str, rep: dict, measures: dict) -> str:
    import abi  # noqa: PLC0415 (sibling; REST as the signed-in person)

    pages = []
    for p in rep.get("pages", []):
        vs = []
        for v in p.get("visuals", []):
            q = v.get("query") or {}
            how = "\n".join(f"{m} = {measures.get(m, {}).get('expression', '?')}" for m in q.get("measures", []))
            how += f"\nby {', '.join(q.get('dimensions', [])) or '—'}" + (f"\nfilters {json.dumps(q['filters'])}" if q.get("filters") else "")
            how += f"\nfile reports/{rid}.json, page {p['id']}, visual {v['id']}"
            item = {"id": v["id"], "title": v.get("title"), "layout": v.get("layout") or {"x": 0, "y": 0, "w": 6}, "how": how}
            if v.get("type") == "text":  # Markdown written by a person or the agent: no numbers from data
                item["text"] = v.get("text") or ""
                vs.append(item)
                continue
            try:
                item["data"] = abi.call("visual_data", project=project, report=rid, page=p["id"], visual=v["id"])
            except abi.ActionError as e:
                item["error"] = str(e.body.get("message"))
            vs.append(item)
        pages.append({"id": p["id"], "name": p.get("name"), "visuals": vs})
    data = json.dumps({"pages": pages}, default=str).replace("</", "<\\/")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return shell(rep.get("name", rid), f"{esc(project)} · numbers from ABI's visual_data, {stamp} · <a href='../index.html'>project</a>",
                 "<nav class=tabs></nav><div class=grid></div>", f"<script>{JS}\nrender({data})</script>")


def diagram(model: dict, kinds: dict) -> str:
    tables = sorted(set(kinds) | {r[s]["table"] for r in model.get("relationships", []) for s in ("from", "to")})
    if not tables:
        return "<p class=empty>No model yet: build_model creates it.</p>"
    facts = [t for t in tables if kinds.get(t) == "fact"] or tables[:1]
    dims = [t for t in tables if t not in facts]
    H = 60 + 56 * max(len(facts), len(dims))
    pos = {t: (40, 40 + 56 * i) for i, t in enumerate(facts)} | {t: (460, 40 + 56 * i) for i, t in enumerate(dims)}
    out = [f"<svg viewBox='0 0 700 {H}' width=100% style='max-width:760px'>"]
    for r in model.get("relationships", []):
        (x1, y1), (x2, y2) = pos[r["from"]["table"]], pos[r["to"]["table"]]
        out.append(f"<path d='M{x1 + 200},{y1 + 18} C{x1 + 300},{y1 + 18} {x2 - 100},{y2 + 18} {x2},{y2 + 18}' fill=none stroke='var(--acc)' stroke-width=1.5>"
                   f"<title>{esc(r['from']['table'])}.{esc(','.join(r['from']['columns']))} → {esc(r['to']['table'])}.{esc(','.join(r['to']['columns']))} (many-to-one)</title></path>")
    for t, (x, y) in pos.items():
        out.append(f"<rect x={x} y={y} width=200 height=36 rx=8 fill='var(--card)' stroke='{'var(--acc2)' if t in facts else 'var(--line)'}'/>"
                   f"<text x={x + 10} y={y + 22} style='fill:var(--ink);font-size:13px'>{esc(t)}</text>"
                   f"<text x={x + 190} y={y + 22} text-anchor=end>{esc(kinds.get(t) or '')}</text>")
    return "".join(out) + "</svg>"


def cards(files: list[Path], dest: Path, fn) -> str:
    if not files:
        return "<p class=empty>None yet.</p>"
    return "".join(f"<div class=card style='margin-bottom:8px'>{fn(f, j(f, {}))}<div class=file>edit: {esc(f.relative_to(dest))}</div></div>"
                   for f in files)


def steps(t: dict) -> str:
    s = t.get("steps") or []
    if not s:
        return "<p class=empty>Input table: rows are entered by hand (write-back).</p>"
    return "<ol class=steps>" + "".join(f"<li><b>{esc(x['type'])}</b> <code>{esc(json.dumps({k: v for k, v in x.items() if k != 'type'}))[:300]}</code></li>" for x in s) + "</ol>"


def flow(f: dict) -> str:
    trig = f.get("trigger") or {}
    chain = " → ".join(f"<span class=chip>{esc(s.get('id'))}: {esc(s.get('action') or s.get('type'))}</span>" for s in f.get("steps", []))
    return (f"<h3>{esc(f.get('name') or f.get('id'))} <span class='{'ok' if f.get('enabled') else 'file'}'>{'enabled' if f.get('enabled') else 'disabled'}</span></h3>"
            f"<div>trigger: <code>{esc(json.dumps(trig))}</code></div><div style='margin-top:6px'>{chain}</div>")


def index_html(dest: Path, project: str, reports: list[str]) -> str:
    pj = j(dest / "project.json", {})
    model = j(dest / "model/model.json", {})
    meas = (j(dest / "model/measures.json", {}) or {}).get("measures", [])
    gloss = (j(dest / "model/glossary.json", {}) or {}).get("terms", [])
    sets = (j(dest / "model/datasets.json", {}) or {}).get("datasets", [])
    kinds = {e["table"]: "dimension" for e in model.get("entities", [])} | {r["from"]["table"]: "fact" for r in model.get("relationships", [])}
    kinds |= {v["table"]: v.get("kind") for v in model.get("tables") or [] if isinstance(v, dict) and not v.get("hidden")}
    log = subprocess.run(["git", "-C", str(dest), "log", "-15", "--format=%h|%ad|%an|%s", "--date=short"], capture_output=True, text=True).stdout
    g = lambda d: sorted(f for f in (dest / d).glob("*.json"))  # noqa: E731
    sec = {
        "Sources": cards(g("sources"), dest, lambda f, x: f"<h3>{esc(x.get('id'))}</h3><code>{esc(x.get('location'))}</code> · refresh {esc(x.get('refresh'))}"),
        "Tables": cards(g("tables"), dest, lambda f, x: f"<h3>{esc(f.stem)}</h3>{steps(x)}") if g("tables") else "",
        "Model": f"<div class=card>{diagram(model, kinds)}<div class=file>edit: model/model.json · date table: {esc(json.dumps(model.get('date_table')))}</div></div>",
        "Measures": ("<div class=card><input class=search placeholder='filter measures' oninput=\"for(const r of this.parentNode.querySelectorAll('tr.m'))r.hidden=!r.textContent.toLowerCase().includes(this.value.toLowerCase())\">"
                     "<table><tr><th>ID</th><th>Name</th><th>MEL</th><th>Description</th></tr>"
                     + "".join(f"<tr class=m><td><code>{esc(m.get('id'))}</code></td><td>{esc(m.get('name'))}</td><td><code>{esc(m.get('expression'))}</code></td><td>{esc(m.get('description') or '')}</td></tr>" for m in meas)
                     + "</table><div class=file>edit: model/measures.json</div></div>") if meas else "<p class=empty>None yet.</p>",
        "Glossary": "".join(f"<span class=chip title='{esc(t.get('definition'))}'>{esc(t.get('term') or t.get('id'))}</span>" for t in gloss) or "<p class=empty>None yet.</p>",
        "Datasets": "".join(f"<span class=chip>{esc(d.get('id'))}</span>" for d in sets) or "<p class=empty>None yet.</p>",
        "Reports": cards(g("reports"), dest, lambda f, x: f"<h3><a href='reports/{esc(f.stem)}.html'>{esc(x.get('name') or f.stem)}</a></h3>"
                         f"{len(x.get('pages', []))} page(s), {sum(len(p.get('visuals', [])) for p in x.get('pages', []))} visual(s)"),
        "ML models": cards(g("ml"), dest, lambda f, x: f"<h3>{esc(x.get('name') or x.get('id'))}</h3>{esc(x.get('task'))}" + (f" of <code>{esc(x.get('target'))}</code>" if x.get("target") else "") +
                           f" on {esc(x.get('dataset'))}" + (f", by {esc(x.get('time'))}, {esc(x.get('horizon'))} ahead" if x.get("horizon") else "")),
        "Flows": cards(g("flows"), dest, lambda f, x: flow(x)),
        "Agents": cards([f for f in g("agents") if not f.name.endswith(".eval.json")], dest, lambda f, x: f"<h3>{esc(x.get('name') or x.get('id'))}</h3>"
                        f"{esc((x.get('instructions') or '')[:300])}<div>tools: {''.join(f'<span class=chip>{esc(t)}</span>' for t in x.get('tools', []))}</div>"),
        "Tools": cards(g("tools"), dest, lambda f, x: f"<h3>{esc(x.get('name') or x.get('id'))}</h3>{esc(x.get('kind') or '')} {esc(x.get('description') or '')}"),
        "Security": cards(g("security"), dest, lambda f, x: f"<pre>{esc(json.dumps(x, indent=1))[:2000]}</pre>"),
        "Environments": cards(g("environments"), dest, lambda f, x: f"<h3>{esc(f.stem)}</h3><pre>{esc(json.dumps(x, indent=1))[:1500]}</pre>"),
        "History": "<div class=card><table>" + "".join(f"<tr><td><code>{esc(a)}</code></td><td>{esc(b)}</td><td>{esc(c)}</td><td>{esc(d)}</td></tr>"
                                                    for a, b, c, d in (l.split("|", 3) for l in log.splitlines() if l.count("|") >= 3)) + "</table></div>",
    }
    plain = [t for t in kinds if not (dest / "tables" / f"{t}.json").exists()]
    if plain:  # ABI writes tables/<id>.json only once a table has steps; a file loaded as-is has none yet
        sec["Tables"] = (sec["Tables"] if g("tables") else "") + "<div class=card>" + "".join(f"<span class=chip>{esc(t)} · {esc(kinds[t] or '')}</span>" for t in plain) + \
            "<div class=file>loaded as is (no transform steps); steps go to tables/&lt;id&gt;.json through set_steps</div></div>"
    sec["Tables"] = sec["Tables"] or "<p class=empty>None yet.</p>"
    nav = "<nav class=tabs>" + "".join(f"<button onclick=\"document.getElementById('{k}').scrollIntoView({{behavior:'smooth'}})\">{k}</button>" for k in sec) + "</nav>"
    body = nav + "".join(f"<section id='{k}'><h2>{k}</h2>{v}</section>" for k, v in sec.items())
    body += ("<section><h2>Edit</h2><div class=card>Every card names its JSON file in <code>" + esc(dest) + "</code>. Edit it, then send it through ABI's import"
             f" checks: <code>scripts/abi.py cli diff {esc(project)}</code>, <code>… cli apply {esc(project)}</code>, or keep <code>… cli watch {esc(project)}</code>"
             " running. Then <code>scripts/abi.py sync " + esc(project) + "</code> refreshes these pages.</div></section>")
    tz = (pj.get("settings") or {}).get("time_zone", "UTC")
    return shell(pj.get("name", project), f"{esc(project)} · time zone {esc(tz)} · {len(meas)} measures · {len(reports)} report(s)", body)


def render(dest: Path, project: str) -> list[str]:
    out = dest.parent / "_views" / project
    (out / "reports").mkdir(parents=True, exist_ok=True)
    measures = {m["id"]: m for m in (j(dest / "model/measures.json", {}) or {}).get("measures", [])}
    written = []
    for f in sorted((dest / "reports").glob("*.json")):
        (out / "reports" / f"{f.stem}.html").write_text(report_html(dest, project, f.stem, j(f, {}), measures))
        written.append(str(out / "reports" / f"{f.stem}.html"))
    (out / "index.html").write_text(index_html(dest, project, written))
    return [str(out / "index.html"), *written]


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))
    d = Path(sys.argv[1]).resolve()
    print(json.dumps(render(d, d.name), indent=1))

#!/usr/bin/env python3
"""The app's shared drawing pieces: the page shell and CSS, charts and tables (JS), report pages with their numbers
from ABI's `visual_data` rows (principle 7), the project sections (sources, steps, model, history) and the docs page."""
import html
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

CSS = """
/* Swiss signal: white (or near-black) ground, hairline rules, an 8-pt grid, Inter Tight for words and IBM Plex Mono for
   code and figures, one red-orange signal colour for what is live or active; radius 4, no shadows */
:root{--paper:#f7f7f5;--sheet:#ffffff;--ink:#0b0b0b;--mute:#62625e;--faint:#e4e4e1;--line:#e4e4e1;--hover:#f1f1ee;
 --signal:#ff4f1f;--signal-ink:#c2380f;--red:#c8311a;--ok:#1d7a45;--c2:#ff4f1f;--c3:#8a8a84;--c4:#c9c9c3;--c5:#ffb49c;
 --display:"Inter Tight","Inter",system-ui,-apple-system,"Segoe UI",sans-serif;--text:var(--display);
 --mono:"IBM Plex Mono",ui-monospace,"SF Mono",Menlo,monospace;--r:6px;--r-lg:10px;--line-strong:#d2d2cd;
 --shadow:0 1px 2px rgb(11 11 11/.04),0 1px 3px rgb(11 11 11/.03);--shadow-pop:0 16px 40px -12px rgb(11 11 11/.22),0 4px 12px rgb(11 11 11/.06);
 --focus:color-mix(in srgb,var(--ink) 62%,transparent);--c1:var(--ink);--bg:var(--paper);--card:var(--sheet);--acc:var(--ink);--acc2:var(--c2);--bad:var(--red);color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--paper:#13110f;--sheet:#1d1a17;--ink:#f1ece4;--mute:#a7a097;--faint:#302c27;--line:#302c27;--hover:#26221e;--line-strong:#433d36;--c1:#d9d2c6;--signal:#ff6a3d;--signal-ink:#ff9068;--red:#ff6b55;--ok:#5cc58a;--c2:#ff6a3d;--c3:#8e877d;--c4:#544d45;--c5:#a1532f;--shadow:0 1px 0 rgb(255 245 230/.03),0 1px 3px rgb(0 0 0/.45);--shadow-pop:0 16px 40px -12px rgb(0 0 0/.75),0 0 0 1px #302c27;color-scheme:dark}}
:root[data-theme="dark"]{--paper:#13110f;--sheet:#1d1a17;--ink:#f1ece4;--mute:#a7a097;--faint:#302c27;--line:#302c27;--hover:#26221e;--line-strong:#433d36;--c1:#d9d2c6;--signal:#ff6a3d;--signal-ink:#ff9068;--red:#ff6b55;--ok:#5cc58a;--c2:#ff6a3d;--c3:#8e877d;--c4:#544d45;--c5:#a1532f;--shadow:0 1px 0 rgb(255 245 230/.03),0 1px 3px rgb(0 0 0/.45);--shadow-pop:0 16px 40px -12px rgb(0 0 0/.75),0 0 0 1px #302c27;color-scheme:dark}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}[hidden]{display:none!important}
body{margin:0;font:14px/1.5 var(--text);background:var(--paper);color:var(--ink);-webkit-font-smoothing:antialiased;font-feature-settings:"ss01","cv11"}
header{display:flex;gap:16px;align-items:center;flex-wrap:wrap;padding-block:12px;padding-left:max(24px,env(safe-area-inset-left,0px));
 padding-right:max(24px,env(safe-area-inset-right,0px));border-bottom:1px solid var(--line);background:var(--sheet)}
header h1{font:600 18px/1.2 var(--display);margin:0;letter-spacing:-.01em}
header .sub{color:var(--mute);font-size:13px}
main{padding:32px max(24px,env(safe-area-inset-right,0px)) 64px max(24px,env(safe-area-inset-left,0px));max-width:1440px;margin:0 auto}
h2,h3,h4{font-family:var(--display);text-wrap:balance;letter-spacing:-.01em}
nav.tabs{display:flex;gap:4px;flex-wrap:wrap;margin:0 0 24px;border-bottom:1px solid var(--line);width:100%;max-width:100%}
nav.tabs button{border:0;border-bottom:2px solid transparent;background:none;color:var(--mute);padding:8px 12px;margin-bottom:-1px;cursor:pointer;font:500 13px var(--text)}
nav.tabs button:hover{color:var(--ink)}nav.tabs button.on{color:var(--ink);border-bottom-color:var(--signal)}
.grid{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:16px}
.card{background:var(--sheet);border:1px solid var(--line);border-radius:var(--r-lg);padding:20px 24px 24px;min-width:0;box-shadow:var(--shadow)}
.card h3{margin:0 0 16px;font:600 15px/1.3 var(--display);display:flex;justify-content:space-between;align-items:baseline;gap:12px}
.card h3 .tools{display:flex;gap:4px;flex-shrink:0}.card h3 .tools button{font:500 12px var(--text);border:1px solid transparent;border-radius:var(--r);background:none;color:var(--mute);padding:2px 8px;cursor:pointer}
.card h3 .tools button:hover{border-color:var(--line);color:var(--ink)}
.kpi .v{font:600 38px/1.05 var(--display);letter-spacing:-.035em;font-variant-numeric:tabular-nums;margin:2px 0 6px}.kpi .l{color:var(--mute);font-size:13px}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:8px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{cursor:pointer;user-select:none;color:var(--mute);font-weight:500;font-size:12px;white-space:nowrap;background:var(--card)}th:hover{color:var(--ink)}th.n{text-align:right}
td.n{text-align:right;font:13px var(--text);font-variant-numeric:tabular-nums;letter-spacing:-.005em}tr:hover td{background:var(--hover)}
.scroll th{position:sticky;top:0;z-index:1}
.scroll{max-height:400px;overflow:auto}svg text{fill:var(--mute);font:11px var(--text);font-variant-numeric:tabular-nums}
svg text.val{fill:var(--ink);font-weight:500;font-size:11px}svg text.tot{fill:var(--ink);font:600 16px var(--display);letter-spacing:-.01em}
svg text.endv{fill:var(--sheet);font-weight:600;font-size:11px}svg rect.endb{fill:var(--ink)}svg text.tl{fill:var(--ink);font-weight:500;font-size:11.5px;pointer-events:none}svg text.tv{fill:var(--ink);opacity:.78;font-size:11px;pointer-events:none}svg text.on{fill:var(--ink);stroke:var(--card);stroke-width:3px;paint-order:stroke;stroke-linejoin:round;pointer-events:none}
.tip{position:fixed;pointer-events:none;background:var(--ink);color:var(--sheet);padding:7px 10px;border-radius:var(--r);font-size:12px;line-height:1.4;display:none;z-index:40;box-shadow:var(--shadow-pop);font-variant-numeric:tabular-nums;max-width:320px}
code,pre{font:12.5px/1.6 var(--mono)}pre{background:var(--paper);border:1px solid var(--line);border-radius:var(--r);padding:12px 16px;overflow:auto;max-height:320px}
.file{color:var(--mute);font:12px var(--mono);margin-top:8px}
.chip{display:inline-block;padding:2px 8px;border:1px solid var(--line);border-radius:999px;margin:2px 4px 2px 0;font-size:12px;background:none}
section{margin-bottom:40px}section>h2{font:600 18px var(--display);margin:0 0 16px}
input.search{padding:8px 12px;border:1px solid var(--line);border-radius:var(--r);background:var(--sheet);color:var(--ink);font:inherit;width:280px;max-width:100%}
a{color:var(--ink);text-underline-offset:3px;text-decoration-color:var(--signal)}.empty{color:var(--mute)}.steps li{margin:2px 0}.ok{color:var(--ok)}.bad{color:var(--red)}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important;scroll-behavior:auto!important}}
"""

JS = r"""
const tip=document.querySelector('.tip');
const E=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));  // text into HTML
function showTip(e,t){tip.textContent=t;tip.style.display='block';tip.style.left=(e.clientX+12)+'px';tip.style.top=(e.clientY+12)+'px'}
function hideTip(){tip.style.display='none'}
function fmt(v,f){if(v===null||v===undefined)return'—';if(typeof v!=='number')return String(v);f=f||{};
 const o={maximumFractionDigits:f.decimals??2};if(f.compact)o.notation='compact';
 if(f.style==='percent')return new Intl.NumberFormat(undefined,{...o,style:'percent'}).format(v);
 if(f.style==='currency'&&f.currency)return new Intl.NumberFormat(undefined,{...o,style:'currency',currency:f.currency}).format(v);
 return new Intl.NumberFormat(undefined,o).format(v)}
const NS='http://www.w3.org/2000/svg';function el(t,a,p){const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);p&&p.appendChild(e);return e}
// neighbours always differ in tone: ink, signal, light grey, mid grey, signal tint, dark grey
const COL=['var(--c1)','var(--c2)','var(--c4)','var(--c3)','var(--c5)','var(--mute)'];
const dimLabel=c=>{const k=String(c).split('.').pop().replace(/_/g,' ');return k.charAt(0).toUpperCase()+k.slice(1)};  // products.product_name: Product name
const MON=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];  // a (year, month) axis reads Jan 2023
const PLAIN=/(^|\.)(year|week_year|quarter|month|week|day_of_week)$/;  // calendar numbers: 2023, not 2,023
function table(box,d){const t=document.createElement('table');const cols=d.columns;let sort=null,desc=false;
 function draw(){t.innerHTML='';const h=t.insertRow();cols.forEach((c,i)=>{const th=document.createElement('th');th.textContent=(d.measures[c]||dimLabel(c))+(sort===i?(desc?' ↓':' ↑'):'');th.title=c;if(typeof d.rows[0]?.[i]==='number')th.className='n';th.onclick=()=>{desc=sort===i?!desc:true;sort=i;draw()};h.appendChild(th)});
  let rows=d.rows.slice();if(sort!==null)rows.sort((a,b)=>((a[sort]>b[sort])-(a[sort]<b[sort]))*(desc?-1:1));
  rows.forEach(r=>{const tr=t.insertRow();tr.dataset.r=d.rows.indexOf(r);r.forEach((v,i)=>{const td=tr.insertCell();td.textContent=PLAIN.test(cols[i])&&typeof v==='number'?String(v):fmt(v,d.format[cols[i]]);if(typeof v==='number')td.className='n'})})}
 draw();const s=document.createElement('div');s.className='scroll';s.appendChild(t);box.appendChild(s)}
const cut=(s,n)=>{s=String(s??'—');return s.length>n?s.slice(0,Math.max(1,n-1))+'…':s};
// a mark is one visual_data row: data-r is the row (a click cross-filters on its first dimension), the tooltip its values
function mark(e,r,t,o=.7){const was=e.getAttribute('opacity')||1;if(r!=null)e.dataset.r=r;
 e.onmousemove=v=>{e.setAttribute('opacity',o);showTip(v,t)};e.onmouseleave=()=>{e.setAttribute('opacity',was);hideTip()};return e}
// the series' names over the plot in their colours, wrapped as measured; three lines at most (two on a short chart),
// then how many more. Returns the plot's top
function legend(svg,W,names,cols){let x=8,y=14,i=0;const k=Math.max(4,Math.min(28,Math.floor((W-38)/7))),last=svg.viewBox.baseVal.height<200?30:46;
 for(;i<names.length;i++){const t=cut(names[i],k),e=el('text',{x,y},svg);el('tspan',{fill:(cols||COL)[i%6]},e).textContent='■ ';e.append(t);
  const w=(e.getComputedTextLength()||t.length*6.5+12)+10;if(x>8&&x+w>W-(y>=last?36:8)){if(y>=last){e.remove();break}x=8;y+=16;e.setAttribute('x',x);e.setAttribute('y',y)}x+=w}
 if(i<names.length)el('text',{x:W-8,y,'text-anchor':'end'},svg).textContent='+'+(names.length-i);return y+12}
// squarified treemap: [x,y,w,h] for each value (largest first) filling the box
function sq(vs,x,y,w,h){const out=[],k=w*h/(vs.reduce((a,b)=>a+b,0)||1),a=vs.map(v=>v*k);
 while(a.length){if(!(w>0&&h>0)){out.push(...a.map(()=>[x,y,0,0]));break}const s=Math.min(w,h);let n=1,best=Infinity;
  for(;n<=a.length;n++){const t=a.slice(0,n).reduce((p,q)=>p+q,0),q=Math.max(s*s*a[0]/(t*t),t*t/(s*s*a[n-1]));if(q>best)break;best=q}
  const r=a.splice(0,n-1),z=r.reduce((p,q)=>p+q,0)/s;let o=0;r.forEach(v=>{out.push(w>=h?[x,y+o,z,v/z]:[x+o,y,v/z,z]);o+=v/z});w>=h?(x+=z,w-=z):(y+=z,h-=z)}
 return out}
// an axis over lo..hi with at most n steps of 1, 2 or 5 x 10^k: its ends (outer ticks) and ticks
function nice(lo,hi,n){const r=hi-lo||Math.abs(hi)||1,e=10**Math.floor(Math.log10(r/n)),s=[1,2,5,10].map(k=>k*e).find(s=>r/s<=n),a=Math.floor(lo/s)*s,b=Math.max(Math.ceil(hi/s)*s,a+s);
 return {lo:a,hi:b,t:Array.from({length:Math.round((b-a)/s)+1},(_,i)=>a+i*s)}}
function chart(box,d){const nd=d.columns.length-Object.keys(d.measures).length;const ms=d.columns.slice(nd);const type=d.type;
 if(type==='kpi'||(nd===0&&d.rows.length===1)){box.innerHTML='';ms.forEach(m=>{const k=document.createElement('div');k.className='kpi';k.innerHTML=`<div class="v">${E(fmt(d.rows[0]?.[d.columns.indexOf(m)],d.format[m]))}</div><div class="l">${E(d.measures[m]||m)}</div>`;box.appendChild(k)});return}
 // RP-8 as the PDF export draws them: a second dimension's members are the series (bars stack, here and in the
 // export), a (month or quarter, year) pair is one time axis; a type or shape not drawn here says so over its rows
 const c=d.columns,two=nd===2&&!c.includes('grouping_id'),yi=+!c[0].endsWith('.year'),
  per=two&&['bar','line','combo'].includes(type)&&c[0].split('.')[0]===c[1].split('.')[0]&&c[yi].endsWith('.year')&&/\.(month|quarter)$/.test(c[1-yi])&&d.rows.every(r=>typeof r[0]==='number'&&typeof r[1]==='number');
 if(!(nd===1&&['bar','line','combo','pie','funnel','waterfall','treemap','scatter','map'].includes(type)||two&&['bar','line','combo','treemap','scatter','heatmap','sankey'].includes(type))){
  if(['bar','line','pie','combo','funnel','treemap','waterfall','scatter','map','heatmap','sankey'].includes(type)){const p=document.createElement('p');p.className='empty';
   p.textContent=`Shown as a table: a ${type} chart can't be drawn from these columns.`;box.appendChild(p)}
  table(box,d);return}
 const sz=box.closest('.sized'),W=box.clientWidth||600,H=sz?Math.max(140,box.clientHeight||260):260,svg=el('svg',{viewBox:`0 0 ${W} ${H}`,width:'100%'},box);
 // drawn for its box: a card made wider, narrower or (sized) taller draws again rather than stretching its text
 if(window.ResizeObserver){const ro=new ResizeObserver(()=>{if(svg.parentNode!==box)return ro.disconnect();if(Math.abs(box.clientWidth-W)>8||(sz&&Math.abs(box.clientHeight-H)>8)){ro.disconnect();svg.remove();chart(box,d)}});ro.observe(box)}
 const at=m=>c.indexOf(m),N=m=>d.measures[m]||m,val=(i,m)=>d.rows[i][at(m)],num=(i,m)=>typeof val(i,m)==='number',idx=d.rows.map((r,i)=>i),
  key=r=>!per?r[0]:c[1-yi].endsWith('.month')?`${MON[r[1-yi]-1]||r[1-yi]} ${r[yi]}`:`Q${r[1-yi]} ${r[yi]}`,tick=(v,m)=>fmt(v,{...d.format[m],compact:true,decimals:2}),
  mk=(e,i,ks,o)=>mark(e,per?null:i,[...(per?[key(d.rows[i])]:d.rows[i].slice(0,nd).map(x=>x??'—')),...ks.map(m=>`${N(m)}: ${fmt(val(i,m),{...d.format[m],compact:false})}`)].join(' · '),o),
  // a label that fits its width: shortened until it does, gone when not even a few letters do
  fit=(t,s,w,strict)=>{t.textContent=s;if(t.getComputedTextLength()<=w)return t;if(strict){t.remove();return null}for(let n=s.length-1;n>2;n--){t.textContent=s.slice(0,n)+'…';if(t.getComputedTextLength()<=w)return t}t.remove();return null},
  dark=k=>[0,5].includes(k%6);  // ink and mute tiles take light text, the rest ink
 // ranked bars across (a map's members, or bars whose names don't fit under them): every name, its value at the end
 const hb=(rs,m,note)=>{const n=rs.length;if(!n)return;const top=note?18:4,rh=Math.max(14,Math.min(30,(H-top-4)/n)),bh=Math.max(8,rh*.62),lw=Math.min(W*.34,Math.max(...rs.map(i=>String(d.rows[i][0]??'—').length))*6.6+10),x0=lw+10,x1=W-64,mx=Math.max(1e-9,...rs.map(i=>Math.abs(val(i,m))));
  if(note)el('text',{x:0,y:11},svg).textContent=note;svg.setAttribute('viewBox',`0 0 ${W} ${Math.max(H,top+n*rh+4)}`);
  rs.forEach((i,k)=>{const y=top+k*rh,v=val(i,m),w=Math.max(1,(x1-x0)*Math.abs(v)/(mx||1));fit(el('text',{x:lw,y:y+rh/2+4,'text-anchor':'end'},svg),String(d.rows[i][0]??'Not given'),lw-4);
   mk(el('rect',{x:x0,y:y+(rh-bh)/2,width:w,height:bh,rx:Math.min(3,bh/4),fill:d.rows[i][0]==null?'var(--c4)':COL[0]},svg),i,[m]);el('text',{x:x0+w+6,y:y+rh/2+4,class:'val'},svg).textContent=tick(v,m)})};
 if(type==='map'){const m=ms[0],ng=idx.filter(i=>d.rows[i][0]==null&&num(i,m)).reduce((a,i)=>a+val(i,m),0);hb(idx.filter(i=>num(i,m)&&d.rows[i][0]!=null).sort((i,j)=>val(j,m)-val(i,m)),m,`${dimLabel(c[0])}, ranked by ${N(m)}${ng?` · not given: ${tick(ng,m)}, not drawn`:''} (the PDF draws the map)`);return}
 // a heatmap: one dimension down, one across, the measure as the cell's shade of the signal colour
 if(type==='heatmap'){const m=ms[0],ys=[...new Set(d.rows.map(r=>r[0]))],xs=[...new Set(d.rows.map(r=>r[1]))],vs=idx.filter(i=>num(i,m)).map(i=>val(i,m)),lo=Math.min(...vs),hi=Math.max(...vs),
   lw=Math.min(W*.3,Math.max(...ys.map(v=>String(v??'—').length))*6.6+10),top=20,cw=(W-lw-4)/Math.max(xs.length,1),chh=Math.max(14,Math.min(44,(H-top-4)/Math.max(ys.length,1)));
  xs.forEach((x,k)=>{const t=el('text',{x:lw+cw*k+cw/2,y:13,'text-anchor':'middle'},svg);fit(t,String(x??'—'),cw-4)});
  ys.forEach((y,k)=>fit(el('text',{x:lw-8,y:top+chh*k+chh/2+4,'text-anchor':'end'},svg),String(y??'—'),lw-10));
  idx.forEach(i=>{if(!num(i,m))return;const v=val(i,m),p=hi>lo?(v-lo)/(hi-lo):1,k=ys.indexOf(d.rows[i][0]),j=xs.indexOf(d.rows[i][1]),x=lw+cw*j,y=top+chh*k,pc=Math.round(10+p*85);
   const r=el('rect',{x:x+1,y:y+1,width:Math.max(cw-2,1),height:Math.max(chh-2,1),rx:3},svg);r.style.fill=`color-mix(in srgb,var(--signal) ${pc}%,var(--sheet))`;mk(r,i,[m]);
   if(cw>34&&chh>16){const t=el('text',{x:x+cw/2,y:y+chh/2+4,'text-anchor':'middle',class:'val'},svg);t.textContent=tick(v,m);if(pc>58)t.style.fill='#fff';fit(t,t.textContent,cw-6)}});
  svg.setAttribute('viewBox',`0 0 ${W} ${Math.max(H,top+chh*ys.length+4)}`);return}
 // a sankey: the first dimension's members on the left flow into the second's on the right, as wide as the measure
 if(type==='sankey'){const m=ms[0],rs=idx.filter(i=>num(i,m)&&val(i,m)>0),tot=rs.reduce((s,i)=>s+val(i,m),0);if(!tot)return;
  const sum=(k,v)=>rs.filter(i=>d.rows[i][k]===v).reduce((s,i)=>s+val(i,m),0),A=[...new Set(rs.map(i=>d.rows[i][0]))].sort((a,b)=>sum(0,b)-sum(0,a)),Bn=[...new Set(rs.map(i=>d.rows[i][1]))].sort((a,b)=>sum(1,b)-sum(1,a)),
   gap=8,lw=Math.min(W*.22,Math.max(...[...A,...Bn].map(v=>String(v??'—').length))*6.6+10),nx=12,x0=lw,x1=W-lw-nx,top=4,k=(H-top-4-gap*Math.max(A.length,Bn.length))/tot,
   lay=(L2,col)=>{let y=top;return Object.fromEntries(L2.map(v=>{const hh=Math.max(2,sum(col,v)*k),o=[v,{y,h:hh,o:0}];y+=hh+gap;return o}))},PA=lay(A,0),PB=lay(Bn,1);
  rs.sort((i,j)=>A.indexOf(d.rows[i][0])-A.indexOf(d.rows[j][0])||Bn.indexOf(d.rows[i][1])-Bn.indexOf(d.rows[j][1])).forEach(i=>{const a=PA[d.rows[i][0]],b=PB[d.rows[i][1]],w=Math.max(1,val(i,m)*k),ya=a.y+a.o,yb=b.y+b.o;a.o+=w;b.o+=w;
   const xa=x0+nx,xm=(xa+x1)/2,p=el('path',{d:`M${xa},${ya}C${xm},${ya} ${xm},${yb} ${x1},${yb}L${x1},${yb+w}C${xm},${yb+w} ${xm},${ya+w} ${xa},${ya+w}Z`,fill:COL[A.indexOf(d.rows[i][0])%6],opacity:.28},svg);mk(p,i,[m],.55)});
  A.forEach((v,j)=>{const a=PA[v];el('rect',{x:x0,y:a.y,width:nx,height:a.h,rx:2,fill:COL[j%6]},svg);fit(el('text',{x:x0-6,y:a.y+a.h/2+4,'text-anchor':'end'},svg),String(v??'—'),lw-8)});
  Bn.forEach(v=>{const b=PB[v];el('rect',{x:x1,y:b.y,width:nx,height:b.h,rx:2,fill:'var(--c3)'},svg);fit(el('text',{x:x1+nx+6,y:b.y+b.h/2+4},svg),String(v??'—'),lw-8)});return}
 if(type==='pie'){const m=ms[0],rs=idx.filter(i=>val(i,m)>0).sort((i,j)=>val(j,m)-val(i,m)),tot=rs.reduce((s,i)=>s+val(i,m),0),T=legend(svg,W,rs.map(i=>`${d.rows[i][0]??'—'} ${Math.round(val(i,m)/tot*100)}%`)),
   R=Math.max(8,Math.min(W-16,H-T-8)/2),r=R*.57,cx=W/2,cy=(H+T)/2,p=(a,q)=>`${cx+q*Math.sin(a)},${cy-q*Math.cos(a)}`;let a=0;
  rs.forEach((i,k)=>{const b=a+Math.min(val(i,m)/tot,.99999)*2*Math.PI,f=+(b-a>Math.PI);
   mk(el('path',{d:`M${p(a,R)}A${R},${R} 0 ${f} 1 ${p(b,R)}L${p(b,r)}A${r},${r} 0 ${f} 0 ${p(a,r)}Z`,fill:COL[k%6],stroke:'var(--card)','stroke-width':2},svg),i,[m]);a=b});
  if(r>26){el('text',{x:cx,y:cy+2,'text-anchor':'middle',class:'tot'},svg).textContent=tick(tot,m);el('text',{x:cx,y:cy+18,'text-anchor':'middle'},svg).textContent='Total'}return}
 if(type==='funnel'){const m=ms[0],rs=idx.filter(i=>val(i,m)>0).sort((i,j)=>val(j,m)-val(i,m)),h=(H-8)/Math.max(rs.length,1),w=i=>(W-16)*val(i,m)/val(rs[0],m),x=W/2;
  rs.forEach((i,k)=>{const y=4+h*k,a=w(i)/2,b=w(rs[k+1]??i)/2;mk(el('path',{d:`M${x-a},${y}L${x+a},${y}L${x+b},${y+h-2}L${x-b},${y+h-2}Z`,fill:COL[k%6]},svg),i,[m]);
   if(h>=14){const t=el('text',{x,y:y+h/2+4,'text-anchor':'middle',class:'tl'},svg);if(dark(k))t.style.fill='var(--sheet)';const fw=Math.max(w(i),w(rs[k+1]??i))-8,full=`${d.rows[i][0]??'—'} · ${tick(val(i,m),m)}${k?' · '+Math.round(val(i,m)/val(rs[0],m)*100)+'%':''}`;t.textContent=full;if(t.getComputedTextLength()>fw&&!fit(t,`${d.rows[i][0]??'—'} · ${tick(val(i,m),m)}`,fw,1)){const t2=el('text',{x,y:y+h/2+4,'text-anchor':'middle',class:'tl'},svg);if(dark(k))t2.style.fill='var(--sheet)';fit(t2,String(d.rows[i][0]??'—'),fw,1)}}});return}
 if(type==='treemap'){const m=ms[0],rs=idx.filter(i=>val(i,m)>0).sort((i,j)=>val(j,m)-val(i,m)),sum=g=>g.reduce((s,i)=>s+val(i,m),0),
   gs=two?[...new Set(rs.map(i=>d.rows[i][0]))].map(g=>rs.filter(i=>d.rows[i][0]===g)).sort((a,b)=>sum(b)-sum(a)):rs.map(i=>[i]),T=two?legend(svg,W,gs.map(g=>d.rows[g[0]][0])):0;
  sq(gs.map(sum),0,T,W,H-T).forEach(([x,y,w,h],k)=>sq(gs[k].map(i=>val(i,m)),x+1,y+1,Math.max(w-2,0),Math.max(h-2,0)).forEach(([x,y,w,h],j)=>{const i=gs[k][j];if(w<3||h<3)return;  // too small to draw: in the rows
   mk(el('rect',{x:x+1,y:y+1,width:Math.max(w-2,0),height:Math.max(h-2,0),fill:COL[k%6]},svg),i,[m]);
   if(w>36&&h>18){const t=el('text',{x:x+7,y:y+16,class:'tl'},svg);if(dark(k))t.style.fill='var(--sheet)';let ok=fit(t,String(d.rows[i][nd-1]??'—'),w-12,1),flat=!!ok;if(!ok&&w>30){const t3=el('text',{x:x+5,y:y+14,class:'tl'},svg);t3.style.fontSize='10px';if(dark(k))t3.style.fill='var(--sheet)';ok=flat=!!fit(t3,String(d.rows[i][nd-1]??'—'),w-8,1)}if(flat&&h>36){const u=el('text',{x:x+7,y:y+31,class:'tv'},svg);if(dark(k))u.style.fill='var(--sheet)';fit(u,tick(val(i,m),m),w-12,1)}}}));return}
 const L=60;let B=40;
 if(type==='scatter'&&ms.length>1){const [mx,my]=ms,rs=idx.filter(i=>num(i,mx)&&num(i,my)),g=two?[...new Set(rs.map(i=>d.rows[i][1]))]:[],T=(two?legend(svg,W,g):8)+16,R=20;
  if(!rs.length)return;const sc=(m,a,b,n)=>{const vs=rs.map(i=>val(i,m)),k=nice(Math.min(...vs),Math.max(...vs),n);return [v=>a+(v-k.lo)/(k.hi-k.lo)*(b-a),k.t]},
   [X,xt]=sc(mx,L,W-R,Math.max(2,Math.floor((W-L-R)/60))),[Y,yt]=sc(my,H-B,T,Math.max(2,Math.floor((H-B-T)/28)));
  yt.forEach(v=>{el('line',{x1:L,x2:W-R,y1:Y(v),y2:Y(v),stroke:'var(--line)'},svg);el('text',{x:L-6,y:Y(v)+4,'text-anchor':'end'},svg).textContent=tick(v,my)});
  xt.forEach(u=>el('text',{x:X(u),y:H-B+16,'text-anchor':'middle'},svg).textContent=tick(u,mx));
  el('text',{x:W-R,y:H-B+32,'text-anchor':'end'},svg).textContent=cut(N(mx),Math.floor((W-L)/7)-2)+' →';el('text',{x:L,y:T-6},svg).textContent='↑ '+cut(N(my),Math.floor((W-L)/7)-2);
  rs.forEach(i=>mk(el('circle',{cx:X(val(i,mx)),cy:Y(val(i,my)),r:4,fill:COL[two?g.indexOf(d.rows[i][1])%6:0]},svg),i,[mx,my]));return}
 // categories (x) and series: one per measure, or per member of the second dimension (its first measure)
 const ENDS=[],ord=idx.filter(i=>ms.some(m=>num(i,m))),ix=new Map,S=[];if(per)ord.sort((i,j)=>d.rows[i][yi]-d.rows[j][yi]||d.rows[i][1-yi]-d.rows[j][1-yi]);
 ord.forEach(i=>ix.has(key(d.rows[i]))||ix.set(key(d.rows[i]),ix.size));const cats=[...ix.keys()];
 ord.forEach(i=>(two&&!per?[[d.rows[i][1],ms[0]]]:ms.map(m=>[m,m])).forEach(([n,m])=>{let s=S.find(s=>s.n===n);if(!s)S.push(s={n,m,v:cats.map(()=>null),r:cats.map(()=>null)});const k=ix.get(key(d.rows[i]));s.v[k]=val(i,m);s.r[k]=i}));
 if(!S.length)return;
 {const lw=Math.max(...cats.map(v=>Math.min(11,String(v??'—').length)))*6.2+8;if(type==='bar'&&!two&&!per&&S.length===1&&cats.length>=2&&cats.length<=24){hb(ord,S[0].m);return}}
 const rot=!per&&cats.length>3&&cats.length<=24&&cats.length*(Math.max(...cats.map(v=>Math.min(11,String(v??'—').length)))*6.2+8)>W-L-(type==='combo'?60:8);if(rot)B=Math.min(84,26+Math.max(...cats.map(v=>Math.min(13,String(v??'—').length)))*5.4);
 const stack=type==='bar'&&two&&!per,combo=type==='combo'&&S.length>1,R=combo?60:type==='line'&&S.length<=2&&!(two&&!per)?58:8,PC=S.length===2&&/_prev$/.test(S[1].m)&&S[1].m.startsWith(S[0].m.replace(/_prev$/,''))?['var(--c1)','var(--c3)']:COL,T=S.length>1&&type!=='waterfall'?legend(svg,W,S.map(s=>two&&!per?s.n:N(s.m)),PC):10,cum=[];  // a measure against its previous period: now in ink, before in grey
 let run=0;if(type==='waterfall')S[0].v.forEach(v=>cum.push([run,run+=v||0]));
 const ax=(vs,m,x,an)=>{const {lo,hi,t}=nice(Math.min(0,...vs),Math.max(0,...vs),Math.max(2,Math.floor((H-B-T)/28))),y=v=>H-B-(v-lo)/(hi-lo)*(H-B-T);
  t.forEach(v=>{an==='end'&&el('line',{x1:L,x2:W-R,y1:y(v),y2:y(v),stroke:'var(--line)'},svg);el('text',{x,y:y(v)+4,'text-anchor':an},svg).textContent=tick(v,m)});return y};
 const y=ax(type==='waterfall'?cum.flat():stack?cats.flatMap((_,k)=>[Math.max,Math.min].map(f=>S.reduce((t,s)=>t+f(0,s.v[k]||0),0))):(combo?S.slice(0,1):S).flatMap(s=>s.v.map(v=>v||0)),S[0].m,L-6,'end'),
  y2=combo?ax(S.slice(1).flatMap(s=>s.v.map(v=>v||0)),S[1].m,W-R+6,'start'):y;
 el('line',{x1:L,x2:W-R,y1:y(0),y2:y(0),stroke:'var(--line-strong)'},svg);  // the zero line, a shade stronger than the grid
 const n=cats.length,step=(W-L-R)/Math.max(n,1),lab=Math.ceil(n*(Math.min(11,Math.max(...cats.map(v=>String(v??'—').length)))*6.2+8)/(W-L-R)),X=k=>L+step*k+step/2;
 let edge=0;if(rot)cats.forEach((v,k)=>{if(step<14&&k%2)return;const t=el('text',{x:X(k)+3,y:H-B+12,'text-anchor':'end',transform:`rotate(-55 ${X(k)+3} ${H-B+12})`},svg);t.textContent=cut(v,13)});else cats.forEach((v,k)=>{if(k%lab)return;const t=el('text',{x:X(k),y:H-B+14,'text-anchor':'middle'},svg);t.textContent=cut(v,11);  // inside the box, none over the last
  const w=t.getComputedTextLength()/2,x=Math.min(X(k),W-w-2);if(x-w<edge)return t.remove();t.setAttribute('x',x);edge=x+w+6});
 if(type==='waterfall'){S[0].v.forEach((v,k)=>{if(v==null)return;const [p,q]=cum[k];  // each step floats from the running total; the tooltip shows the row's own value
   mk(el('rect',{x:L+step*k+step*.15,y:Math.min(y(p),y(q)),width:Math.max(step*.7,1),height:Math.max(Math.abs(y(p)-y(q)),1),rx:2,fill:v<0?'var(--signal)':'var(--ink)'},svg),S[0].r[k],[S[0].m]);
   if(step>=34){const t=el('text',{x:X(k),y:Math.min(y(p),y(q))-5,'text-anchor':'middle',class:'val'},svg);t.textContent=(v>0&&k?'+':'')+tick(v,S[0].m)}
   k&&el('line',{x1:X(k-1)+step*.35,x2:X(k)-step*.35,y1:y(p),y2:y(p),stroke:'var(--mute)','stroke-dasharray':'2 2'},svg)});return}
 const SW=Math.min(step*.8,120);if(stack)cats.forEach((_,k)=>{let p=0,q=0;S.forEach((s,j)=>{const v=s.v[k];if(v==null)return;const a=v<0?q:p,b=a+v;v<0?q=b:p=b;
  mk(el('rect',{x:L+step*k+(step-SW)/2,y:Math.min(y(a),y(b)),width:Math.max(SW,1),height:Math.abs(y(a)-y(b)),fill:COL[j%6]},svg),s.r[k],[s.m])})});
 else{const bs=type==='bar'?S:type==='combo'?S.slice(0,1):[],gw=Math.min(step*.8,72*Math.max(bs.length,1)),bw=gw/Math.max(bs.length,1);  // a few wide categories: bars keep a bar's width
  cats.forEach((_,k)=>bs.forEach((s,j)=>{const v=s.v[k];if(v!=null)mk(el('rect',{x:L+step*k+(step-gw)/2+bw*j,y:Math.min(y(v),y(0)),width:Math.max(bw,1),height:Math.abs(y(v)-y(0)),fill:PC[j%6],rx:Math.min(2,bw/6)},svg),s.r[k],[s.m])}));
  // one series of a few bars: each bar's value over it, compact (the tooltip has it in full)
  if(type==='bar'&&bs.length===1&&n<=16&&step>=34)bs[0].v.forEach((v,k)=>{if(v==null)return;const t=el('text',{x:X(k),y:Math.min(y(v),y(0))-5,'text-anchor':'middle',class:'val'},svg);t.textContent=tick(v,bs[0].m);if(+t.getAttribute('y')<10)t.remove()})}
 (type==='combo'?S.slice(1):type==='line'||type==='scatter'?S:[]).forEach(s=>{const j=S.indexOf(s),yy=combo?y2:y,dot=type==='scatter',pts=s.v.map((v,k)=>v==null?null:[X(k),yy(v)]);
  const ln=pts.map((p,k)=>p?(pts[k-1]?'L':'M')+p:'').join(''),run=pts.filter(Boolean);
  // one series: a faint area under it; a dense one (a point every few pixels, e.g. per day) a thinner line
  if(!dot&&S.length===1&&type==='line'&&run.length>1&&pts.every(Boolean))el('path',{d:`${ln}L${run.at(-1)[0]},${y(0)}L${run[0][0]},${y(0)}Z`,fill:COL[j%6],opacity:.06},svg);
  dot||el('path',{d:ln,fill:'none',stroke:COL[j%6],'stroke-width':step<4?1.25:2,'stroke-linejoin':'round','stroke-linecap':'round',...(step<2?{opacity:.6}:{}),...(/forecast|prediction/i.test(N(s.m))&&j?{'stroke-dasharray':'5 4'}:{})},svg);
  pts.forEach((p,k)=>p&&mk(el('circle',{cx:p[0],cy:p[1],r:dot?4:Math.max(2,Math.min(4,step/3)),fill:COL[j%6],opacity:dot?1:.01},svg),s.r[k],[s.m],dot?.7:1));
  // a line ends on its latest value, marked and labelled
  if(!dot&&type==='line'&&S.length<=2&&run.length){const kk=s.v.map((v,k)=>v==null?-1:k).filter(k=>k>=0).at(-1),[lx,ly]=pts[kk];el('circle',{cx:lx,cy:ly,r:3.5,fill:COL[j%6],stroke:'var(--card)','stroke-width':1.5},svg);
   let yy0=Math.max(T+9,Math.min(H-B-4,ly));const g=el('g',{},svg),t=el('text',{x:lx+16,y:0,class:'endv'},g);t.textContent=tick(s.v[kk],s.m);const tw=t.getComputedTextLength()||30;
   for(const b of ENDS)if(lx+10<b.x+b.w&&lx+10+tw+12>b.x&&Math.abs(yy0-b.y)<20)yy0=b.y+(yy0>=b.y?20:-20);ENDS.push({x:lx+10,w:tw+12,y:yy0});t.setAttribute('y',yy0+4);
   el('rect',{x:lx+10,y:yy0-8.5,width:tw+12,height:17,rx:4,class:'endb'},g).after(t);g.append(t)}});
 // a line over time: a guide follows the pointer to the nearest period and the tooltip has every series there
 if(type==='line'&&(!two||per)){const g=el('line',{y1:T,y2:H-B,stroke:'var(--mute)','stroke-dasharray':'3 3',opacity:0,'pointer-events':'none'},svg);
  svg.addEventListener('mousemove',e=>{const r=svg.getBoundingClientRect();if(!r.width)return;const k=Math.round(((e.clientX-r.left)*W/r.width-L-step/2)/step);if(k<0||k>=n){g.setAttribute('opacity',0);return}
   g.setAttribute('x1',X(k));g.setAttribute('x2',X(k));g.setAttribute('opacity',.7);showTip(e,[cats[k],...S.map(s=>`${N(s.m)}: ${fmt(s.v[k],{...d.format[s.m],compact:false})}`)].join(' · '))});
  svg.addEventListener('mouseleave',()=>{g.setAttribute('opacity',0);hideTip()})}}
function csv(d){const q=v=>v===null?'':/[",\n]/.test(String(v))?'"'+String(v).replace(/"/g,'""')+'"':String(v);
 const blob=new Blob([[d.columns.join(','),...d.rows.map(r=>r.map(q).join(','))].join('\n')],{type:'text/csv'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=d.id+'.csv';a.click()}
"""


def esc(x) -> str:
    return html.escape(str(x))


def j(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


# 1.2 (owner, 5 Nov 2026): the fonts ship with ABI (scripts/fonts, SIL Open Font License), so no page or export makes a
# browser contact Google: Inter Tight and IBM Plex Mono as data URIs (about 100 KB), which also work offline and in
# file:// exports
def _font(file: str, family: str, weight: str) -> str:
    import base64  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415
    b = base64.b64encode((Path(__file__).resolve().parent / "fonts" / file).read_bytes()).decode()
    return f"@font-face{{font-family:'{family}';font-style:normal;font-weight:{weight};font-display:swap;src:url(data:font/woff2;base64,{b}) format('woff2')}}"


FONTS = (_font("inter-tight-latin.woff2", "Inter Tight", "100 900") + _font("ibm-plex-mono-latin-400.woff2", "IBM Plex Mono", "400")
         + _font("ibm-plex-mono-latin-500.woff2", "IBM Plex Mono", "500"))


def shell(title: str, sub: str, body: str, script: str = "") -> str:
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{esc(title)}</title><style>{FONTS}{CSS}</style></head><body><div class=tip></div><header><h1>{esc(title)}</h1>"
            f"<span class=sub>{sub}</span></header><main>{body}</main>{script}</body></html>")


def report_pages(project: str, rid: str, rep: dict, measures: dict, ex=None) -> list[dict]:
    """Each page's visuals with ABI's visual_data rows (or the text, or ABI's error) and a "how" note; the pages
    asked at once through `ex` (an executor asking as the person: app.Reads), each page in one call (M3.6 round 2:
    a page's visuals in one read action); without one, no rows."""
    import abi  # noqa: PLC0415 (sibling; REST as the signed-in person)

    def fetch(items: list[dict], page: str) -> None:
        try:
            got = {d["id"]: d for d in abi.call("visual_data", project=project, report=rid, page=page)["visuals"]}
        except abi.ActionError as e:
            got = {i["id"]: {"error": e.body.get("message")} for i in items}
        for item in items:
            d = got.get(item["id"]) or {"error": f"No visual '{item['id']}' on page '{page}' now."}
            if "error" in d:
                item["error"] = str(d["error"])
            else:
                item["data"] = d

    pages, todo = [], []
    for p in rep.get("pages", []):
        vs, mine = [], []
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
            mine.append(item)
            vs.append(item)
        if mine:
            todo.append((mine, p["id"]))
        pages.append({"id": p["id"], "name": p.get("name"), "visuals": vs})
    if ex:
        list(ex.map(lambda t: fetch(*t), todo))  # all in (another error than ABI's raises here)
    return pages


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
        out.append(f"<path d='M{x1 + 200},{y1 + 18} C{x1 + 300},{y1 + 18} {x2 - 100},{y2 + 18} {x2},{y2 + 18}' fill=none stroke='var(--ink)' stroke-width=1.5>"
                   f"<title>{esc(r['from']['table'])}.{esc(','.join(r['from']['columns']))} → {esc(r['to']['table'])}.{esc(','.join(r['to']['columns']))} (many-to-one)</title></path>")
    for t, (x, y) in pos.items():
        out.append(f"<rect x={x} y={y} width=200 height=36 fill='{'var(--ink)' if t in facts else 'var(--card)'}' stroke='var(--ink)' stroke-width='2'/>"
                   f"<text x={x + 10} y={y + 22} style='fill:{'var(--paper)' if t in facts else 'var(--ink)'};font-size:13px;font-weight:600'>{esc(t)}</text>"
                   f"<text x={x + 190} y={y + 22} text-anchor=end style='fill:{'var(--paper)' if t in facts else 'var(--mute)'}'>{esc(kinds.get(t) or '')}</text>")
    return "".join(out) + "</svg>"


def cards(files: list[Path], dest: Path, fn) -> str:
    if not files:
        return "<p class=empty>None yet.</p>"
    return "".join(f"<div class=card style='margin-bottom:12px'>{fn(f, j(f, {}))}<div class=file>edit: {esc(f.relative_to(dest))}</div></div>"
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


def sections(dest: Path) -> dict[str, str]:
    """The explorer's sections as HTML, by name (index.html and app.html both show them)."""
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
        "Queries": "".join(f"<span class=chip>{esc(d.get('id'))}</span>" for d in sets) or "<p class=empty>None yet.</p>",
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
    return sec


def docs_html(name: str, docs: dict) -> str:
    """The project's documentation (ABI's `docs`) as one printable page: sections, items, their facts."""
    d = (docs.get("projects") or [{}])[0]
    body = "".join(f"<section><h2>{esc(s.get('number', ''))}. {esc(s.get('title', ''))}</h2>" + "".join(
        f"<h3>{esc(it.get('title', ''))}</h3><table>" + "".join(
            f"<tr><th>{esc(k)}</th><td>{esc(v if isinstance(v, str) else json.dumps(v))}</td></tr>" for k, v in it.get("facts") or [])
        + "</table>" for it in s.get("items") or []) + "</section>" for s in d.get("sections") or [])
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8><title>{esc(name)} · documentation</title><style>{CSS}"
            "body{background:#fff;color:#0b0b0b}main{max-width:none;padding:0}h1{font:600 26px var(--display);margin:0 0 4px}"
            "section{break-inside:avoid-page;margin:0 0 28px}h2{font:600 18px var(--display);border-bottom:2px solid var(--signal);padding-bottom:6px}"
            "h3{font:600 14px var(--display);margin:16px 0 6px}th{width:28%;cursor:default;white-space:normal;color:var(--mute)}td,th{font-size:11.5px}"
            f"</style></head><body><main><h1>{esc(name)}</h1><p class=file>Documentation generated by ABI · {when}</p>{body}</main></body></html>")

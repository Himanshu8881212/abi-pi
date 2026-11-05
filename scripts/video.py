#!/usr/bin/env python3
"""A silent explainer video of a report: one slide per view, its numbers from ABI (visual_data, as the person), a
caption per slide, the app's theme (light or dark), drawn frame by frame by the engine's headless Chromium (Playwright,
the one report exports use) from scripts/video.html and encoded by ffmpeg to an H.264 MP4 (yuv420p, 30 fps: plays
everywhere, PowerPoint included).

    video.py <project> <report> [--pages p1,p2] [--views v1,v2] [--theme paper|ink] [--seconds 6] [--size 1920x1080]
             [--captions captions.json] [--out file.mp4]

Captions: by default each one is written from the view's own rows (highest, lowest, first to last). An agent may pass
its own ({view id: text}); every number in them must appear in that view's rows, or the video is refused.
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import abi
from render import FONTS

HERE = Path(__file__).resolve().parent
PY = HERE.parent / "engine/backend/.venv/bin/python"  # the engine's, which has Playwright
FPS = 30
FFMPEG = "ffmpeg"
# the frames: the page's draw(i / fps) in the engine's Chromium, each frame's PNG to stdout (the last one again when
# nothing moved); argv: page fps frames width height
SHOOT = [str(PY), "-c", """import sys
from playwright.sync_api import sync_playwright
page, fps, n, w, h = sys.argv[1], *map(int, sys.argv[2:6])
with sync_playwright() as p:
    b = p.chromium.launch(args=['--disable-dev-shm-usage'])
    pg = b.new_page(viewport={'width': w, 'height': h})
    pg.goto('file://' + page)
    pg.evaluate("document.fonts.load('600 56px Inter Tight')")  # the bundled font before the first frame
    png = b''
    for i in range(n):
        if pg.evaluate(f'draw({i} / {fps})') or not png:
            png = pg.screenshot(type='png')
        sys.stdout.buffer.write(png)
    b.close()
"""]
THEMES = {"paper": {"paper": "#f7f7f5", "sheet": "#ffffff", "ink": "#0b0b0b", "mute": "#62625e", "faint": "#e4e4e1", "accent": "#ff4f1f"},
          "ink": {"paper": "#0b0b0b", "sheet": "#141414", "ink": "#f2f2ef", "mute": "#9b9b96", "faint": "#2a2a28", "accent": "#ff6a3d"}}
NUM = re.compile(r"-?\d[\d,]*(?:\.\d+)?\s*(?:bn|m|k|%)?", re.I)
YEAR = re.compile(r"^(19|20)\d\d$")  # a year is a label, not a figure
# the image always has both; an install from source adds them once (README > Run it from source)
NO_VIDEO = "Video needs {what}, which this install doesn't have yet: {how}."
HOW = {"ffmpeg": "install it once (`apt install ffmpeg` or `brew install ffmpeg`)",
       "the engine's headless Chromium": "run `engine/backend/.venv/bin/playwright install --only-shell chromium` once"}


def home() -> str:
    """The HOME the engine's Playwright keeps its Chromium under (as app.py's docs_pdf)."""
    h = os.environ.get("ENGINE_HOME") or str(HERE.parent / "data/home")
    return h if Path(h).exists() else os.environ.get("HOME", "")


def chromium() -> bool:
    cache = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path(home(), "Library/Caches" if sys.platform == "darwin" else ".cache", "ms-playwright")
    return PY.exists() and any(Path(cache).glob("chromium*"))


def missing() -> str:
    """Why this install can't render a video ('' when it can)."""
    gone = [k for k, ok in (("ffmpeg", shutil.which(FFMPEG)), ("the engine's headless Chromium", chromium())) if not ok]
    return NO_VIDEO.format(what=" and ".join(gone), how="; ".join(HOW[k] for k in gone)) if gone else ""


def compact(v) -> str:
    if v is None:
        return "—"
    a = abs(v)
    return f"{v / 1e9:.1f}bn" if a >= 1e9 else f"{v / 1e6:.1f}m" if a >= 1e6 else f"{v / 1e3:.1f}k" if a >= 1e3 else (str(int(v)) if float(v).is_integer() else f"{v:.2f}")


def slide(v: dict, d: dict) -> dict | None:
    """One view's ABI rows as a slide, and a caption written from those rows only."""
    if "rows" not in d or not d["rows"]:
        return None
    ms = [c for c in d["columns"] if c in d["measures"]]
    dims = [c for c in d["columns"] if c not in d["measures"] and c != "grouping_id"]
    name = lambda m: d["measures"].get(m) or m  # noqa: E731
    title = v.get("title") or d.get("title") or v["id"]
    if not dims:
        row = d["rows"][0]
        kpis = [{"label": name(m), "value": row[d["columns"].index(m)], "text": compact(row[d["columns"].index(m)])} for m in ms][:4]
        return {"title": title, "kind": "kpi", "kpis": kpis, "caption": " · ".join(f"{k['label']}: {k['text']}" for k in kpis)}
    if dims[:2] == ["calendar.year", "calendar.month"] and len(dims) == 2:  # one time axis
        ix = [d["columns"].index(c) for c in dims]
        d = {**d, "columns": ["calendar.period"] + [c for c in d["columns"] if c not in dims],
             "rows": [[f"{r[ix[0]]}-{int(r[ix[1]]):02d}" if r[ix[0]] is not None and r[ix[1]] is not None else None]
                      + [x for i, x in enumerate(r) if i not in ix] for r in d["rows"]], "type": "line"}
        dims = ["calendar.period"]
    tops = [max((abs(r[d["columns"].index(m)]) for r in d["rows"] if isinstance(r[d["columns"].index(m)], (int, float))), default=0) for m in ms]
    one_scale = len(ms) == 1 or (min(tops) > 0 and max(tops) / min(tops) <= 10)  # one axis only for measures of a like size
    if len(dims) == 1 and ms and len(d["rows"]) <= 60 and one_scale:
        rows = [r for r in d["rows"] if r[d["columns"].index(dims[0])] is not None]
        labels = [str(r[d["columns"].index(dims[0])]) for r in rows]
        series = [{"name": name(m), "values": [r[d["columns"].index(m)] for r in rows]} for m in ms[:3]]
        vals = [(x, l) for x, l in zip(series[0]["values"], labels) if x is not None]
        if not vals:
            return None
        hi, lo = max(vals), min(vals)
        line = d.get("type") == "line" or dims[0].startswith("calendar.")
        cap = (f"{series[0]['name']} went from {compact(vals[0][0])} in {vals[0][1]} to {compact(vals[-1][0])} in {vals[-1][1]};"
               f" highest {compact(hi[0])} in {hi[1]}." if line else
               f"{series[0]['name']}: highest {hi[1]} with {compact(hi[0])}, lowest {lo[1]} with {compact(lo[0])}.")
        return {"title": title, "kind": "line" if line else "bar", "labels": labels[:24], "series": [{**s, "values": s["values"][:24]} for s in series], "caption": cap}
    cols = dims + ms
    rows = [[r[d["columns"].index(c)] for c in cols] for r in d["rows"][:7]]
    top = max(rows, key=lambda r: r[len(dims)] if ms and isinstance(r[len(dims)], (int, float)) else float("-inf"))
    return {"title": title, "kind": "table", "dims": len(dims), "columns": [name(c) if c in ms else c.split(".")[-1] for c in cols], "rows": rows,
            "caption": (f"Highest {name(ms[0])}: {top[0]} with {compact(top[len(dims)])}." if ms else f"{len(d['rows'])} rows.")
                       + (f" The first {len(rows)} of {len(d['rows'])} rows are shown." if len(d["rows"]) > len(rows) else "")}


def grounded(text: str, d: dict) -> bool:
    """Every number in an agent's caption appears in the view's rows (as written, or as the compact form)."""
    seen = {compact(x) for r in d.get("rows", []) for x in r if isinstance(x, (int, float))}
    seen |= {str(x) for r in d.get("rows", []) for x in r}
    seen = {s.replace(",", "") for s in seen}
    return all(YEAR.match(n.strip()) or n.strip().replace(",", "") in seen for n in NUM.findall(text))


def build(project: str, report: str, pages=None, views=None, theme="paper", seconds=6, size=(1920, 1080), captions=None) -> dict:
    if missing():
        raise SystemExit(missing())
    rep = abi.call("list_reports", project=project)
    spec = next((r for r in rep.get("reports", rep) if isinstance(r, dict) and r.get("id") == report), None)
    if not spec:
        raise SystemExit(f"No report '{report}' in {project}.")
    slides, refused = [], []
    for p in spec["pages"]:
        if pages and p["id"] not in pages:
            continue
        for v in p["visuals"]:
            if (views and v["id"] not in views) or v.get("text") is not None or v.get("type") == "text":
                continue
            d = abi.call("visual_data", project=project, report=report, page=p["id"], visual=v["id"])
            s = slide(v, d)
            if not s:
                continue
            if captions and v["id"] in captions:
                if not grounded(captions[v["id"]], d):
                    refused.append(v["id"])
                    continue
                s["caption"] = captions[v["id"]]
            if s["kind"] == "kpi" and slides and slides[-1]["kind"] == "kpi" and len(slides[-1]["kpis"]) + len(s["kpis"]) <= 4:
                slides[-1]["kpis"] += s["kpis"]
                slides[-1]["title"], slides[-1]["caption"] = "Headline numbers", slides[-1]["caption"] + " · " + s["caption"]
            else:
                slides.append(s)
    if refused:
        raise SystemExit(f"Captions with numbers that are not in their view's rows: {', '.join(refused)}.")
    if not slides:
        raise SystemExit("No views with data to show.")
    now = datetime.now(timezone.utc).strftime("%d %b %Y")
    return {"title": spec.get("name") or report, "subtitle": spec.get("description", "")[:120], "theme": THEMES[theme], "seconds": seconds,
            "width": size[0], "height": size[1], "slides": slides,
            "footer": f"{spec.get('name') or report} · numbers from ABI ({project}) as of {now} · {len(slides)} views"}


def render(props: dict, out: Path) -> Path:
    """The deck as an MP4: frame n is the page's draw(n / FPS), screenshotted by the engine's Chromium and piped as PNGs
    to ffmpeg. Past ABI_VIDEO_TIMEOUT both are stopped with every process they started."""
    out.parent.mkdir(parents=True, exist_ok=True)
    w, h = (props["width"] // 2 * 2, props["height"] // 2 * 2)  # yuv420p needs even sides
    n = round(props["seconds"] * FPS) * (len(props["slides"]) + 2)  # the title card, the slides, the end card
    limit = float(os.environ.get("ABI_VIDEO_TIMEOUT", "900"))  # 1.2 M3.2: s; 1.1 waited for ever
    with tempfile.TemporaryDirectory() as d:
        page = Path(d) / "deck.html"
        page.write_text((HERE / "video.html").read_text().replace("/*FONTS*/", FONTS)  # the app's fonts, no network
                        .replace("/*PROPS*/null", json.dumps(props).replace("</", "<\\/")))
        shoot = subprocess.Popen([*SHOOT, str(page), str(FPS), str(n), str(w), str(h)], stdout=subprocess.PIPE,
                                 env={**os.environ, "HOME": home()})
        enc = subprocess.Popen([FFMPEG, "-y", "-loglevel", "error", "-f", "image2pipe", "-c:v", "png", "-framerate", str(FPS), "-i", "-",
                                "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out.resolve())],
                               stdin=shoot.stdout)
        shoot.stdout.close()  # ffmpeg holds the pipe: if it stops, the frames stop too
        try:
            enc.wait(timeout=limit)
            shoot.wait(timeout=5)
        except subprocess.TimeoutExpired:
            for pid in tree(shoot.pid) + tree(enc.pid):  # Python, Playwright's driver, its Chromium (a process group of its own), ffmpeg
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:  # ended meanwhile
                    pass
            shoot.wait()
            enc.wait()
            raise SystemExit(f"The video took longer than {limit:g} s (ABI_VIDEO_TIMEOUT) and was stopped.") from None
    for p in (shoot, enc):
        if p.returncode:
            raise subprocess.CalledProcessError(p.returncode, p.args[:3])
    return out


def tree(pid: int) -> list[int]:
    """`pid` and every process under it, from the process table (ps)."""
    kids: dict[int, list[int]] = {}
    for line in subprocess.run(["ps", "-A", "-o", "pid=", "-o", "ppid="], capture_output=True, text=True).stdout.splitlines():
        child, parent = map(int, line.split())
        kids.setdefault(parent, []).append(child)
    out, todo = [], [pid]
    while todo:
        out.append(todo.pop())
        todo += kids.get(out[-1], [])
    return out


def demo() -> None:
    d = {"columns": ["calendar.month", "views"], "measures": {"views": "Views"}, "rows": [[1, 1000.0], [2, 2500000.0]], "type": "line"}
    s = slide({"id": "v", "title": "Views"}, d)
    assert s["kind"] == "line" and s["caption"] == "Views went from 1.0k in 1 to 2.5m in 2; highest 2.5m in 2.", s["caption"]
    assert grounded("Views rose to 2.5m in 2026.", d) and not grounded("Views rose to 3.1m.", d)
    k = slide({"id": "k"}, {"columns": ["rev"], "measures": {"rev": "Revenue"}, "rows": [[37330985.06]]})
    assert k["kind"] == "kpi" and k["kpis"][0]["text"] == "37.3m"
    print("ok")


def main(argv: list[str]) -> None:
    if argv[:1] == ["demo"]:
        return demo()
    if len(argv) < 2:
        raise SystemExit(__doc__)
    opt = lambda k, d=None: argv[argv.index(k) + 1] if k in argv else d  # noqa: E731
    w, h = map(int, opt("--size", "1920x1080").split("x"))
    props = build(argv[0], argv[1], opt("--pages") and opt("--pages").split(","), opt("--views") and opt("--views").split(","),
                  opt("--theme", "paper"), float(opt("--seconds", 6)), (w, h), json.loads(Path(opt("--captions")).read_text()) if opt("--captions") else None)
    out = Path(opt("--out") or f"{argv[1]}.mp4")
    print(json.dumps({"video": str(render(props, out)), "slides": len(props["slides"])}))


if __name__ == "__main__":
    main(sys.argv[1:])

#!/usr/bin/env python3
"""The 1.2.5 UX bench's camera: signs in to the app as a role and runs steps in the engine's headless Chromium (the
Playwright of the engine's venv, as report exports use), writing PNGs and steps.jsonl to --out (bench/UX.md). Run INSIDE
the container; bench/ux_shots.sh does it from the Mac.

    ABI_UX_ACCOUNTS='<accounts JSON>' python3 bench/ux_shot.py --role owner --width 1440 --out DIR steps.json
    ABI_UX_ACCOUNTS='<accounts JSON>' python3 bench/ux_shot.py --tour --out DIR

ABI_UX_ACCOUNTS holds bench/ux_seed.py's output ({role: {name, password}}), never the command line. A step is one of
    {"goto": "measures"}                 a page of the app by its key (rep:overview, mdl:all, flw:<id>...); "/path" a URL
    {"click": "css selector"}            or {"text": "visible text"} to click by text
    {"type": "selector", "value": "x"}   fill an input
    {"upload": "input[type=file]", "file": "/tmp/x.csv"}   choose a file (on the container) in a file input
    {"key": "Escape"}                    press a key
    {"wait": 800} | {"wait": "selector"} milliseconds, or until the selector shows
    {"shot": "name"}                     <width>-<role>-<name>.png (full page; "viewport": true for the screen only)
    {"close": true}                      close any open dialog
steps.jsonl: one line per step with ok or the error, and the console and page errors it caused.
--tour: every page of the sidebar plus the reports, the main dialogs, as owner and viewer at 1440 and 390 px
(--role or --width narrows it).
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path(os.environ.get("ENGINE_HOME", "/opt/abi/home")) / ".cache/ms-playwright"))
from playwright.sync_api import sync_playwright  # noqa: E402

URL = os.environ.get("ABI_UX_URL", "http://127.0.0.1:8790")
P = "brewline"
SETTLE = 1500  # ms after a page or a click: visuals load their rows after the page draws

# the main dialogs, after the page they open on: (page, state, steps)
DIALOGS = [
    ("home", "account", [{"click": "header button.who"}]),
    ("home", "tokens", [{"click": "header button.who"}, {"click": "dialog[open] button:has-text('Personal tokens')"}]),
    ("home", "two-step", [{"click": "header button.who"}, {"click": "dialog[open] button:has-text('Two-step sign-in')"}]),
    ("home", "switcher", [{"click": "#switch"}]),
    ("home", "chat-drawer", [{"click": "#chat-toggle"}]),
    ("home", "changes", [{"click": "#changes"}]),
    ("rep:overview", "filter-picker", [{"click": "button.fadd"}]),
    ("sources", "add-source", [{"click": "main .bar button:has-text('Add source')"}]),
    ("people", "add-person", [{"text": "Add a person"}]),
    ("home", "menu", [{"click": "#menu"}]),
]


def accounts() -> dict:
    raw = os.environ.get("ABI_UX_ACCOUNTS")
    if not raw:
        sys.exit("Set ABI_UX_ACCOUNTS to bench/ux_seed.py's output (the root-only accounts file).")
    return json.loads(raw)


class Shot:
    def __init__(self, browser, role: str, width: int, out: Path, acc: dict):
        self.role, self.width, self.out, self.errs = role, width, out, []
        mobile = width < 600
        self.ctx = browser.new_context(viewport={"width": width, "height": 844 if mobile else 900}, device_scale_factor=2 if mobile else 1,
                                       is_mobile=mobile, has_touch=mobile, locale="en-US")
        self.pg = self.ctx.new_page()
        self.pg.set_default_timeout(10_000)
        self.pg.on("console", lambda m: m.type == "error" and self.errs.append(f"console: {m.text[:300]}"))
        self.pg.on("pageerror", lambda e: self.errs.append(f"page: {str(e)[:300]}"))
        self.log = (out / "steps.jsonl").open("a")
        a = acc[role]
        self.pg.goto(URL + "/")  # signed out, the page is the sign-in form (it posts to /signin)
        self.pg.fill("#n", a["name"])
        self.pg.fill("#p", a["password"])
        self.pg.press("#p", "Enter")
        self.pg.wait_for_selector("aside button.it", timeout=60_000)
        self.home = self.pg.url.split("#")[0]
        if f"p={P}" not in self.home:
            self.pg.goto(f"{URL}/?p={P}")
            self.pg.wait_for_selector("aside button.it", timeout=60_000)
            self.home = self.pg.url.split("#")[0]

    def step(self, s: dict) -> bool:
        self.errs.clear()
        t, pg, err = time.time(), self.pg, None
        try:
            if "goto" in s:
                g = s["goto"]
                if g.startswith("/"):
                    pg.goto(URL + g)
                elif pg.evaluate("typeof show") == "function":  # the app's own navigation, as a sidebar click does
                    pg.evaluate("k => show(k)", g)
                else:
                    pg.goto(f"{self.home}#{g}")
                    pg.reload()
                pg.wait_for_timeout(s.get("settle", SETTLE))
                if (now := pg.evaluate("typeof CUR === 'string' ? CUR : ''")) and now != g and not g.startswith("/"):
                    err = f"opened {now}, not {g}"
            elif "click" in s:
                if not pg.locator(s["click"]).first.is_visible():  # e.g. a Developer button on a phone (Viewer there)
                    raise LookupError(f"not shown here: {s['click']}")
                pg.click(s["click"])
                pg.wait_for_timeout(s.get("settle", SETTLE))
            elif "text" in s:
                pg.get_by_text(s["text"], exact=s.get("exact", False)).first.click()
                pg.wait_for_timeout(s.get("settle", SETTLE))
            elif "type" in s:
                pg.fill(s["type"], s.get("value", ""))
            elif "upload" in s:  # a file on the container, e.g. bench/brewline.py OUT --csv's brewline.csv
                pg.set_input_files(s["upload"], s["file"])
                pg.wait_for_timeout(s.get("settle", SETTLE))
            elif "key" in s:
                pg.keyboard.press(s["key"])
                pg.wait_for_timeout(300)
            elif "wait" in s:
                pg.wait_for_timeout(s["wait"]) if isinstance(s["wait"], (int, float)) else pg.wait_for_selector(s["wait"])
            elif "close" in s:
                pg.evaluate("document.querySelectorAll('dialog[open]').forEach(d => d.close())")
                pg.evaluate("document.body.classList.remove('menu-open'); document.querySelector('#chat.open .x')?.click()")
            elif "shot" in s:
                pg.screenshot(path=str(self.out / f"{self.width}-{self.role}-{s['shot']}.png"), full_page=not s.get("viewport"))
            else:
                err = "unknown step"
        except Exception as e:  # a step that fails is a result; the run goes on
            err = f"{type(e).__name__}: {str(e).splitlines()[0][:300]}"
        rec = {"role": self.role, "width": self.width, "step": s, "ok": err is None, "ms": round((time.time() - t) * 1000)}
        if err:
            rec["error"] = err
        if self.errs:
            rec["errors"] = list(self.errs)
        self.log.write(json.dumps(rec) + "\n")
        self.log.flush()
        return err is None

    def run(self, steps: list) -> None:
        for s in steps:
            self.step(s)

    def close(self) -> None:
        self.log.close()
        self.ctx.close()


def tour(sh: Shot) -> None:
    pg = sh.pg
    pages = pg.eval_on_selector_all("aside button.it", "bs => bs.map(b => b.dataset.k)")
    pages += [k for k in pg.evaluate("Object.keys(PAGES)") if k.split(":")[0] in ("rep", "mdl", "flw") and k not in pages]
    for k in pages:
        sh.step({"goto": k})  # a page that throws is shot as it drew (steps.jsonl has its error)
        sh.step({"shot": k.replace(":", "-")})
    for page, state, steps in DIALOGS:
        if page not in pages or (state == "menu" and sh.width >= 820) or (state == "changes" and sh.role == "viewer"):
            continue
        if sh.step({"goto": page}) and all(sh.step(s) for s in steps):
            sh.step({"shot": f"{page.replace(':', '-')}-{state}", "viewport": True})
        sh.step({"key": "Escape"})
        sh.step({"close": True})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("steps", nargs="?", help="a JSON file with a list of steps")
    ap.add_argument("--role", choices=["owner", "editor", "viewer"])
    ap.add_argument("--width", type=int, choices=[1440, 390])
    ap.add_argument("--out", required=True)
    ap.add_argument("--tour", action="store_true", help="every page and the main dialogs")
    a = ap.parse_args()
    if not a.tour and not a.steps:
        ap.error("give a steps file or --tour")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    acc = accounts()
    roles = [a.role] if a.role else ["owner", "viewer"] if a.tour else ["owner"]
    widths = [a.width] if a.width else [1440, 390] if a.tour else [1440]
    steps = None if a.tour else json.loads(Path(a.steps).read_text())
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--disable-dev-shm-usage"])
        for role in roles:
            for w in widths:
                sh = Shot(b, role, w, out, acc)
                try:
                    tour(sh) if a.tour else sh.run(steps)
                finally:
                    sh.close()
        b.close()
    rows = [json.loads(x) for x in (out / "steps.jsonl").read_text().splitlines()]
    print(json.dumps({"shots": len(list(out.glob("*.png"))), "steps": len(rows), "failed": sum(not r["ok"] for r in rows),
                      "with_errors": sum(bool(r.get("errors")) for r in rows)}))


if __name__ == "__main__":
    main()

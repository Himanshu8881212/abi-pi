#!/usr/bin/env python3
"""Write codegraph/, an Obsidian vault of ABI-PI's own files (not the vendored engine/): one note per file with what it
is for, what is inside it (with line numbers: where to change what), what it uses and what uses it. Open the folder as a
vault in Obsidian; the graph view shows how the files connect. Rerun after changes:  python3 scripts/codegraph.py"""
import ast
import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "codegraph"
KEEP = re.compile(r"\.(py|ts|tsx|sh|md|json|yml)$|^(Dockerfile|bin/abi)$")
SKIP = re.compile(r"^(engine/|bench/|workspaces/|codegraph/)|lock\.json$|^uv\.lock$|^\.(git|docker)ignore$")
GROUPS = [("scripts/", "App and scripts", 0xFF4F1F), (".pi/extensions/", "Agent harness (Pi extensions)", 0x2F6FEB),
          (".pi/agents/", "Specialists", 0x8250DF), (".pi/skills/", "Skills (playbooks)", 0x1A7F37),
          (".pi/", "Agent instructions", 0x0969DA), ("harness/", "Team and tool areas", 0x9A6700),
          ("tests/", "Tests", 0x6E7781), ("docker", "Container", 0x0550AE),
          ("", "Docs and config", 0x57606A)]


def vp(rel: str) -> str:
    """Where a file sits in the vault: Obsidian hides dot folders, so .pi/ shows as pi/."""
    return rel[1:] if rel.startswith(".pi/") else rel


def domain(rel: str) -> str:
    """The Code Graph skill's area vocabulary (3-7 areas, from the folders)."""
    if rel.startswith("tests/"):
        return "tests"
    if rel.startswith((".pi/", "harness/")) or rel in ("scripts/team.py", "scripts/gen_agents.py", "scripts/gen_areas.py"):
        return "agent"
    if rel.startswith("docker") or rel in ("Dockerfile", "bin/abi", "scripts/engine.sh", "scripts/backup.sh"):
        return "deploy"
    return "app"


def header(rel: str, purpose: str, tests: list[str]) -> str:
    """The skill's Tier 1 file header (@file, @domain, @status) and @tested-by links, in the file's comment style."""
    c = "//" if rel.endswith((".ts", ".tsx")) else "#"
    return "\n".join([f"{c} @file {purpose[:120] or rel}", f"{c} @domain {domain(rel)}", f"{c} @status stable",
                      *[f"{c} @tested-by [[{t}]]" for t in tests]]) + "\n"


def group(rel: str) -> str:
    return next(g for p, g, _ in GROUPS if rel.startswith(p) or (p == "docker" and rel in ("Dockerfile", "docker-compose.yml")))


def outline(rel: str, text: str) -> tuple[str, list[str]]:
    """(one-line purpose, what's inside with line numbers)."""
    lines, items, purpose = text.split("\n"), [], ""
    if rel.endswith(".py"):
        try:
            tree = ast.parse(text)
        except SyntaxError:
            tree = None
        if tree:
            purpose = (ast.get_docstring(tree) or "").split("\n\n")[0].replace("\n", " ")
            for n in tree.body:
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    doc = (ast.get_docstring(n) or "").split("\n")[0]
                    items.append(f"L{n.lineno} `{'class ' if isinstance(n, ast.ClassDef) else 'def '}{n.name}`" + (f" — {doc}" if doc else ""))
        items += [f"L{i} route `{m[1]}`" for i, l in enumerate(lines, 1) if (m := re.search(r'self\.path (?:==|\.startswith\() ?"(/[^"]*)"', l))]
    elif rel.endswith((".ts", ".tsx")):
        doc = re.search(r"/\*\*\s*\n?\s*\*?\s*([^\n]+)", "\n".join(lines[:40]))  # the opening doc block, else the first comment
        purpose = doc[1].strip(" */") if doc else next((l.strip("/ -") for l in lines if l.startswith("//")), "")
        items = [f"L{i} tool `{m[1]}`" for i, l in enumerate(lines, 1) if (m := re.search(r'name: "(\w+)"', l)) and "registerTool" in "".join(lines[max(0, i - 3):i])]
        items += [f"L{i} `{m[1]}`" for i, l in enumerate(lines, 1) if (m := re.match(r"\s*(?:export\s+)?(?:async\s+)?function (\w+)", l))]
    elif rel.endswith(".sh") or rel in ("Dockerfile", "bin/abi", "docker-compose.yml"):
        purpose = " ".join(l.lstrip("#! ").strip() for l in lines[:6] if l.startswith("#") and not l.startswith("#!"))[:300]
    elif rel.endswith(".md"):
        fm = re.search(r"^description: (.+)$", text, re.M)
        purpose = fm[1] if fm else next((l.strip("# ") for l in lines if l.strip() and not l.startswith("---")), "")
        items = [f"L{i} {l.strip()}" for i, l in enumerate(lines, 1) if re.match(r"#{1,3} ", l)][:40]
    elif rel.endswith(".json"):
        try:
            d = json.loads(text)
            purpose = d.get("about", "") if isinstance(d, dict) and isinstance(d.get("about"), str) else ""
            items = [f"`{k}`" for k in (d if isinstance(d, dict) else [])][:30]
        except ValueError:
            pass
    if rel == "scripts/app.py":  # the one-page app's JavaScript: its sections
        items += [f"L{i} page section: {m[1]}" for i, l in enumerate(lines, 1) if (m := re.match(r"\s*// -{6,} (.+?) -{6,}", l))]
    return purpose[:400], items


def main() -> None:
    files = [f for f in subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True, check=True).stdout.split()
             if KEEP.search(f) and not SKIP.search(f)]
    text = {f: (ROOT / f).read_text(errors="replace") for f in files}
    base = {}
    for f in files:
        base.setdefault(Path(f).name, []).append(f)
    mods = {Path(f).stem: f for f in files if f.startswith("scripts/") and f.endswith(".py")}
    skills = {f.split("/")[2]: f for f in files if f.startswith(".pi/skills/") and f.endswith("SKILL.md")}
    agents = {Path(f).stem: f for f in files if f.startswith(".pi/agents/")}
    uses: dict[str, set[str]] = {f: set() for f in files}
    for f, t in text.items():
        for name, paths in base.items():  # a file named by its file name ("abi.py", "team.json"), when the name is unique
            if len(paths) == 1 and paths[0] != f and "." in name and re.search(r"(?<![\w.-])" + re.escape(name) + r"(?![\w-])", t):
                uses[f].add(paths[0])
        if f.endswith(".py"):  # sibling imports (import abi, from render import …)
            for m in re.finditer(r"^\s*(?:import (\w+)|from (\w+) import)", t, re.M):
                if (mod := m[1] or m[2]) in mods and mods[mod] != f:
                    uses[f].add(mods[mod])
        if f.startswith((".pi/", "harness/")) or f == "scripts/gen_agents.py":  # skills and specialists by name
            for name, p in {**skills, **agents}.items():
                if p != f and re.search(r"(?<![\w/-])" + re.escape(name) + r"(?![\w-])", t):
                    uses[f].add(p)
        if "bin/abi" in t and f != "bin/abi":
            uses[f].add("bin/abi")
        if "extensions/abi/index.ts" in f:
            uses[f] |= {p for name, p in mods.items() if f"{name}.py" in t}
    used_by: dict[str, set[str]] = {f: set() for f in files}
    for f, us in uses.items():
        for u in us:
            used_by[u].add(f)
    engine_users = sorted(f for f, t in text.items() if re.search(r"engine/|scripts/engine\.sh|ABI_URL|/api/actions", t))

    OUT.mkdir(exist_ok=True)
    for x in OUT.iterdir():  # the vault's own settings and plugins (.obsidian) stay
        if x.name != ".obsidian":
            shutil.rmtree(x) if x.is_dir() else x.unlink()
    for f in files:  # the code itself, for Code Graph to parse (only ABI-PI's own files: never data/, engine/, .venv);
        (OUT / vp(f)).parent.mkdir(parents=True, exist_ok=True)  # code files get the skill's header in this copy only
        t = text[f]
        if re.search(r"\.(py|ts|tsx|sh)$", f) or f in ("Dockerfile", "bin/abi"):
            tests = sorted(u for u in used_by[f] if u.startswith("tests/"))
            first, nl, rest = t.partition("\n") if t.startswith("#!") else ("", "", t)
            t = first + nl + header(f, outline(f, text[f])[0], tests) + rest
        (OUT / vp(f)).write_text(t)
    link = lambda f: f"[[notes/{vp(f)}|{f.split('/')[-1] if not f.endswith('SKILL.md') else f.split('/')[-2]}]]"  # noqa: E731
    for f in files:
        purpose, items = outline(f, text[f])
        n = text[f].count("\n") + 1
        body = [f"---\ntags: [{group(f).split(' ')[0].lower()}]\npath: {f}\nlines: {n}\nrelated-code: \"[[{vp(f)}]]\"\n---", f"# {f}", f"> {purpose}" if purpose else "",
                f"**Group:** {group(f)} · {n} lines", "",
                "**Uses:** " + (", ".join(link(u) for u in sorted(uses[f])) or "—"),
                "**Used by:** " + (", ".join(link(u) for u in sorted(used_by[f])) or "—")]
        if f in engine_users:
            body.append("**Talks to:** [[notes/ABI engine|ABI engine]]")
        if items:
            body += ["", "## What's inside", *[f"- {x}" for x in items[:200]]]
        dest = OUT / "notes" / f"{vp(f)}.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("\n".join(body) + "\n")
    (OUT / "notes/ABI engine.md").write_text(
        "# ABI engine (engine/, vendored)\n> A copy of ABI's backend: Postgres, the semantic engine, actions, flows, ML, custom agents. "
        "ABI-PI changes data and projects only through its actions (`/api/actions/<name>`).\n\n**Used by:** "
        + ", ".join(link(f) for f in engine_users) + "\n")
    by_group = {}
    for f in files:
        by_group.setdefault(group(f), []).append(f)
    start = ["# Start here", "ABI-PI's own files, one note each (generated by `scripts/codegraph.py`; rerun it after changes). "
             "Open **Code Graph** (ribbon icon, or the command 'Code Graph: Open graph view') for the code's imports and calls, "
             "or Obsidian's **Graph view** for these notes; colours are the groups below. Each note says what the file is for, "
             "what is inside it with line numbers, and which files use it.", "",
             "**How it fits together:** a person uses the one-page app ([[notes/scripts/app.py|app.py]]); its chat starts the Pi agent, "
             "whose ABI tools live in [[notes/pi/extensions/abi/index.ts|the abi extension]] and its team of specialists in "
             "[[notes/pi/extensions/subagent/index.ts|the subagent extension]]; everything reads and changes data only through "
             "[[notes/ABI engine|ABI engine]] actions, called from [[notes/scripts/abi.py|abi.py]]. [[notes/Dockerfile|Dockerfile]] "
             "and [[notes/docker/entrypoint.sh|entrypoint.sh]] package it as one container.", ""]
    for _, g, _ in GROUPS:
        if g in by_group:
            start += [f"## {g}", *[f"- {link(f)}" for f in sorted(by_group[g])], ""]
    (OUT / "Start here.md").write_text("\n".join(start))
    (OUT / ".obsidian").mkdir(exist_ok=True)
    (OUT / ".obsidian/graph.json").write_text(json.dumps({"colorGroups": [
        {"query": f'path:"{p.rstrip("/")}"' if p not in ("", "docker") else ('path:docker OR file:Dockerfile' if p == "docker" else "path:/"),
         "color": {"a": 1, "rgb": c}} for p, _, c in GROUPS if p], "showTags": False, "showArrow": True}, indent=1))
    print(f"codegraph/: {len(files)} notes, {sum(map(len, uses.values()))} links; open the folder as an Obsidian vault")


if __name__ == "__main__":
    main()

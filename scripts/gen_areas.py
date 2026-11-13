#!/usr/bin/env python3
"""Map every ABI action to its tool group, exactly as ABI's `agent.area()` does (longest module prefix wins), by reading
ABI's source statically (no import, nothing written there). Writes harness/areas.json.

    python3 scripts/gen_areas.py [~/Work/Working/ABI/backend]
"""
import ast
import json
import re
import sys
from pathlib import Path

SRC = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "engine/backend").expanduser()
OUT = Path(__file__).resolve().parent.parent / "harness" / "areas.json"

# CORE and AREAS (description, module prefixes, question keyword stems matched with \b(...), D-71), read from ABI's
# abi/agent/__init__.py: its pure-literal assignments, never imported
_LIT = {t.id: n.value for n in ast.parse((SRC / "abi/agent/__init__.py").read_text()).body if isinstance(n, ast.Assign)
        for t in n.targets if isinstance(t, ast.Name) and t.id in ("CORE", "AREAS")}
CORE = [ast.literal_eval(e) for e in _LIT["CORE"].elts]  # a set there; its source order here
AREAS = ast.literal_eval(_LIT["AREAS"])
PREFIX = {p: a for a, (_, ps, _) in AREAS.items() for p in ps}
DEF = re.compile(r"^@action\([^\n]*\n(?:@[^\n]*\n)*def (\w+)\(", re.M)


NOT_HERE = {"create_share_link", "create_embed_key"}  # ABI refuses them from an agent (their pages aren't reachable at the app's address)


def area(module: str, name: str) -> str | None:
    parts = f"{module}.{name}".split(".")
    return next((PREFIX[p] for i in range(len(parts), 0, -1) if (p := ".".join(parts[:i])) in PREFIX), None)


def main() -> None:
    groups: dict[str, list[str]] = {a: [] for a in AREAS}
    other = []
    for f in sorted((SRC / "abi").rglob("*.py")):
        rel = f.relative_to(SRC).with_suffix("")
        module = ".".join(rel.parts[:-1] if rel.name == "__init__" else rel.parts)
        for name in DEF.findall(f.read_text()):
            if name in CORE or name in NOT_HERE:
                continue
            (groups[a] if (a := area(module, name)) else other).append(name)
    out = {"core": CORE, "unclassified": sorted(other),
           "areas": {a: {"about": AREAS[a][0], "words": AREAS[a][2], "tools": sorted(t)} for a, t in groups.items()}}
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2) + "\n")
    n = len(CORE) + len(other) + sum(map(len, groups.values()))
    print(f"{n} actions -> {OUT}")
    assert n > 150 and "semantic_query" in CORE and "train_model" in out["areas"]["ml"]["tools"]


if __name__ == "__main__":
    main()

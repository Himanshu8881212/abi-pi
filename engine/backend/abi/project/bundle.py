"""Serializer and bundle. PRD §6.6 PC-2 (folder <-> `project.bundle.json`, lossless), PC-4 (deterministic).

Generic over object types: a bundle is `{relative/path: content}` for every file in the project folder
except `.git/`. `*.json` files hold their parsed JSON value, any other file (tools/*.py, docs/*.md) its
exact text. Every JSON file and the bundle itself are written by `dumps`, so the same objects always
give the same bytes.
"""

import json
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any


def dumps(obj: Any) -> str:
    """PC-4: sorted keys, 2-space indent, UTF-8 text, trailing newline. NaN/Infinity aren't JSON: refused."""
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"


def _unique(pairs: list) -> dict:
    d = dict(pairs)
    if len(d) != len(pairs):  # json.loads would silently keep the last one
        raise ValueError(f"Duplicate key in JSON object: {[k for k, n in Counter(k for k, _ in pairs).items() if n > 1]}")
    return d


def loads(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_unique)


def bundle(folder: Path) -> dict:
    """Folder -> bundle."""
    out = {}
    for p in sorted(folder.rglob("*")):
        rel = p.relative_to(folder).as_posix()
        if ".git" in p.relative_to(folder).parts:  # repos (and nested ones) aren't project content
            continue
        if p.is_symlink():  # could pull a file from outside the project into the bundle
            raise ValueError(f"{rel}: symlinks can't be bundled.")
        if p.is_dir():
            continue
        try:
            text = p.read_bytes().decode()  # bytes, not read_text: keeps \r\n exactly
            out[rel] = loads(text) if rel.endswith(".json") else text  # same test as unbundle (Path.suffix of ".json" is "")
        except ValueError as e:
            raise ValueError(f"{rel}: {e}") from e
        _encode(rel, out[rel])  # accept exactly what unbundle accepts, so the round trip can't fail halfway
    return out


def _encode(rel: Any, content: Any) -> bytes:
    """The file bytes for one bundle entry. Bundles come from external agents: every path must stay
    inside the project folder and out of .git."""
    p = PurePosixPath(rel) if isinstance(rel, str) else PurePosixPath()
    # as_posix() != rel catches "", "a//b", "./a", "a/" (PurePosixPath normalizes them away); no parts: "." (the folder)
    if p.as_posix() != rel or not p.parts or p.is_absolute() or "\\" in rel or {"..", ".git"} & set(p.parts):
        raise ValueError(f"Invalid file path in bundle: {rel!r}")
    if not rel.endswith(".json") and not isinstance(content, str):
        raise ValueError(f"{rel}: a non-JSON file's content must be a string.")
    try:
        return (dumps(content) if rel.endswith(".json") else content).encode()
    except ValueError as e:  # UnicodeEncodeError (lone surrogate, e.g. "\ud800"), NaN
        raise ValueError(f"{rel}: not writable as UTF-8 JSON: {e}") from e


def unbundle(b: dict, folder: Path) -> None:
    """Bundle -> folder. Writes every file into `folder` (meant to be empty; nothing is deleted).
    Everything is checked first, so an invalid bundle writes nothing."""
    if not isinstance(b, dict):
        raise ValueError("A bundle is a JSON object of {path: content}.")  # noqa: TRY004 - one error type for bad input
    files = {rel: _encode(rel, content) for rel, content in b.items()}
    for rel in files:
        if clash := [str(d) for d in PurePosixPath(rel).parents if str(d) in files]:
            raise ValueError(f"{rel}: {clash[0]} can't be both a file and a folder.")
    for rel, data in files.items():
        p = folder / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

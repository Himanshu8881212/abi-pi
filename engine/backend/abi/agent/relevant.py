"""D-117 item 3: the definitions relevant to a Main Agent turn's question, ranked by embeddings. PRD SC-5, SE-6, SC-9.

Callers rank only what they already built for the user (role, RLS/COL-SEC, tenant and masking filters applied), so a
ranked list is a subset of the full one. Only definition texts are embedded, never data rows (SC-5, principle 4), by
the first workspace provider with an `embeddings` model alias (AG-6); vectors are cached in Postgres by sha256(text)
and provider/model. No question, a short list, no such alias or a failed call: the caller keeps its whole list.
"""

import contextvars
import hashlib

import duckdb
import openai

from abi import actions, db
from abi.actions import ActionFailed
from abi.agent import llm
from abi.security import User

QUESTION = contextvars.ContextVar("question", default="")  # the turn's typed text, set around each Main Agent tool call
# ponytail: fixed thresholds; per-provider context sizes if small local models need tighter lists
MIN = 40  # items (in all of a result's lists) before ranking applies
TOP = 12  # items kept per ranked list
NOTE = "Only the items most relevant to the question are listed; call again with all set to true for every item."


def ranked(user: User, project: str, question: str, items: dict[str, str], size: int | None = None) -> set[str] | None:
    """The keys of the TOP `items` ({key: definition text}) closest to `question`, or None to keep them all.
    `size`: the whole result's item count, when `items` is one of several lists."""
    if not question or len(items) <= TOP or (size or len(items)) <= MIN:
        return None
    with db.connect() as conn:
        p = conn.execute("SELECT * FROM llm_providers WHERE models ? 'embeddings' ORDER BY position, id LIMIT 1").fetchone()
    if not p or not (vecs := _vectors(user, project, p, [question, *items.values()])):
        return None
    n, keys = len(vecs[0]), list(items)
    # ponytail: exact scan over a project's definitions (hundreds to thousands); a vss HNSW index past ~100k items
    with duckdb.connect() as con:
        top = con.execute(f"SELECT i FROM (SELECT generate_subscripts($1, 1) i, unnest($1) v) ORDER BY"
                          f" array_cosine_similarity(v::FLOAT[{n}], $2::FLOAT[{n}]) DESC, i LIMIT {TOP}", [vecs[1:], vecs[0]]).fetchall()
    return {keys[i - 1] for (i,) in top}


def _vectors(user: User, project: str, p: dict, texts: list[str]) -> list[list[float]] | None:
    name = p["models"]["embeddings"]
    model, shas = f"{p['id']}/{name}", [hashlib.sha256(t.encode()).hexdigest() for t in texts]
    with db.connect() as conn:
        have = {r["sha"]: r["vector"] for r in conn.execute("SELECT sha, vector FROM embeddings WHERE model = %s AND sha = ANY(%s)",
                                                            [model, shas])}
    if miss := list({s: t for s, t in zip(shas, texts) if s not in have}.items()):
        try:
            resp = llm._send(p, name, {"input": [t for _, t in miss], "encoding_format": "float"}, embed=True)
            got = [d.embedding for d in sorted(resp.data, key=lambda d: d.index)]
            if len(got) != len(miss) or len({len(v) for v in got} | {len(v) for v in have.values()}) != 1:
                raise ValueError("embeddings count or size")
        except (openai.APIError, ActionFailed, ValueError) as e:  # SC-6; the caller keeps its whole list
            with db.connect() as conn:
                actions.audit(conn, user, actions.VIA.get(), "llm", project, {"provider": p["id"], "embeddings": name}, 502,
                              type(e).__name__)
            return None
        resp.effort = None
        llm._usage(user, resp, name, "embeddings", p["id"], p["prices"].get(name), project, [0, 0, 0, 0])  # OB-1, SC-11
        with db.connect() as conn:
            conn.cursor().executemany("INSERT INTO embeddings (sha, model, vector) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                                      [[s, model, v] for (s, _), v in zip(miss, got)])
        have |= {s: v for (s, _), v in zip(miss, got)}
    return [have[s] for s in shas]

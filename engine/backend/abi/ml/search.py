"""Hyperparameter search inside a task's two D-99 candidates: ML-1, ML-3, ML-4, D-135 (work/validation/ML-ZOO.1.md).

A model's `search` ({strategy: grid | random, trials, time_budget_s, seed}) and `space` ({method: {setting: [values] |
{min, max, log?}}}) in ml/<id>.json. Settings come from scikit-learn's own `ParameterGrid` / `ParameterSampler`; each
candidate's first trial is always today's defaults ({}), so a search never keeps worse than the defaults on the folds.
The caller scores a setting on folds INSIDE the training part only (ml._folds: rolling-origin by period or stratified
k-fold; forecast: rolling-origin windows before the backtest), so no trial reads the held-out part. Budget (D-17): each
candidate gets an equal share of `time_budget_s`; trials past it are recorded as skipped; every setting is bounded by
the schema (trees, leaves, rounds), so a trial's memory stays within one default fit's order.
"""

from time import monotonic

import numpy as np

FOLDS = 3  # validation folds per trial
TRIALS = 10  # per candidate, the defaults included
BUDGET = 300  # seconds per training run, shared by its candidates
POINTS = 3  # a {min, max} range in a grid: this many evenly (log-)spaced values
INTS = {"num_leaves", "n_estimators", "min_child_samples", "max_depth", "min_samples_leaf"}
SPACES = {  # each candidate's default space (`space` naming none for it); bounds are the schema's
    "lightgbm": {"num_leaves": [15, 31, 63], "learning_rate": {"min": 0.02, "max": 0.2, "log": True},
                 "n_estimators": [100, 200, 400], "min_child_samples": [5, 20, 50]},
    "random_forest": {"max_depth": [6, 8, 10], "min_samples_leaf": [1, 5, 20], "max_features": [0.33, 0.6, 1.0]},
    "ets": {"model": ["ZZZ", "AAN", "AAA", "ANA", "MAM"], "damped": [None, True]},
}


def _plain(k: str, v):
    """A sampled value as JSON: an int setting as int, numpy numbers as Python."""
    if v is None or isinstance(v, (bool, str)):
        return v
    return round(float(v)) if k in INTS else round(float(v), 6)


def settings(method: str, space: dict, search: dict) -> list[dict]:
    """The settings one candidate tries, `trials` in all: {} (today's defaults) first, then the strategy's."""
    from scipy import stats
    from sklearn.model_selection import ParameterGrid, ParameterSampler

    sp, n = space.get(method) or SPACES[method], search.get("trials", TRIALS)
    if search["strategy"] == "grid":
        def points(k, r):
            v = (np.geomspace if r.get("log") else np.linspace)(r["min"], r["max"], POINTS)
            return list(dict.fromkeys(_plain(k, x) for x in v))
        gen = ParameterGrid({k: v if isinstance(v, list) else points(k, v) for k, v in sp.items()})
    else:
        def dist(k, r):
            if k in INTS:
                return stats.randint(r["min"], r["max"] + 1)
            return (stats.loguniform if r.get("log") else stats.uniform)(r["min"], r["max"] - (0 if r.get("log") else r["min"]))
        gen = ParameterSampler({k: v if isinstance(v, list) else dist(k, v) for k, v in sp.items()}, n_iter=n,
                               random_state=search.get("seed", 0))
    out = [{}]
    for p in gen:
        if len(out) >= n:
            break
        if (p := {k: _plain(k, v) for k, v in p.items()}) not in out:
            out.append(p)
    return out


def run(sets: list[dict], score, budget: float) -> list[dict]:
    """Each setting scored by `score(params) -> (mean, std)` in metric terms until `budget` seconds pass (the defaults
    always run); a setting that raises or scores NaN is failed, one past the budget skipped."""
    # ponytail: the budget is checked between trials, so one trial may run past it; interrupt fits if overruns matter
    end, out = monotonic() + budget, []
    for i, p in enumerate(sets):
        t = monotonic()
        if i and t > end:
            out.append({"params": p, "status": "skipped", "score": None, "score_std": None, "seconds": None})
            continue
        try:
            mean, std = score(p)
        except Exception:  # noqa: BLE001 - a setting the estimator refuses (e.g. multiplicative ETS on zeros) is a failed trial
            mean = std = float("nan")
        ok = bool(np.isfinite(mean))
        out.append({"params": p, "status": "ok" if ok else "failed", "score": mean if ok else None,
                    "score_std": std if ok else None, "seconds": round(monotonic() - t, 3)})
    return out


def best(trials: list[dict], lower: bool) -> dict:
    """The best ok trial, but the defaults (first) unless it beats them by more than one standard error of their fold
    scores: the one-standard-error rule (Hastie et al., ESL 7.10), so fold noise on small data never trades the
    defaults for a setting that is only luckier."""
    ok = [t for t in trials if t["status"] == "ok"]
    if not ok:
        return trials[0]
    b, d = min(ok, key=lambda t: t["score"] if lower else -t["score"]), trials[0]
    return d if d["status"] == "ok" and abs(b["score"] - d["score"]) <= d["score_std"] / np.sqrt(FOLDS) else b


def candidates(methods, space: dict, search: dict, score, lower: bool, metric: str) -> tuple[dict, list[dict]]:
    """Search each candidate: ({method: its best setting}, every trial as an ml_trials row: method, position, params,
    metric, score, score_std, seconds, status, chosen)."""
    params, rows = {}, []
    for k in methods:
        ts = run(settings(k, space, search), lambda p, k=k: score(k, p), search.get("time_budget_s", BUDGET) / len(methods))
        b = best(ts, lower)
        params[k] = b["params"]
        rows += [t | {"method": k, "metric": metric, "chosen": t is b} for t in ts]
    return params, [r | {"position": i} for i, r in enumerate(rows, 1)]


def said(cands: dict, trials: list[dict], how: str) -> str:
    """ML-4: the search in a sentence of code-made counts (each also a candidate figure: `trials`, `trials skipped`)."""
    for k, c in cands.items():
        c["trials"] = sum(t["method"] == k and t["status"] == "ok" for t in trials)
        if n := sum(t["method"] == k and t["status"] == "skipped" for t in trials):
            c["trials skipped"] = n
    from abi.ml import LABEL

    s = " Searched " + " and ".join(f"{c['trials']} settings of {LABEL[k]}" for k, c in cands.items()) + f" on {how}."
    return s + (" Some settings were skipped: the search's time budget ran out." if any("trials skipped" in c for c in cands.values()) else "")

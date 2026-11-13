"""Forecasting, classification, registry. PRD §7.5 ML-1..ML-4, work/validation/X-01.md decisions 1-4.

An ML model is `ml/<id>.json` (schemas/v1/ml.json): task, dataset, target, features, time. No code, no estimator
parameters (§2 non-goal). `train_model` reads the dataset only through `semantic.query` as the caller (ML-2) and runs
in the worker (§9): outside it the action queues a job and returns the registry run ID. ML-3 guards are code: a
time-based holdout when `time` is set (else a seeded stratified split), leakage suspects excluded and listed,
balanced class weights, a baseline and `better_than_baseline`. ML-4: the registry is Postgres `ml_runs`; a fitted
model is stored as text (LightGBM `model_to_string`, a random forest's tree arrays), never pickled, and `predict`
validates it on load. D-99 (ML-CANDIDATES.1): each task trains two candidates (`METHODS`, forecast.METHODS) and keeps
the better on a validation part of the training rows (H-07), never on the holdout it reports; both candidates' metrics are kept on the run. Forecasts: abi.ml.forecast. ML-5: `score_model` writes a run's predictions on fresh dataset rows
as model table ml_<id> with measure <id>_prediction (work/validation/X-01.3.md); `prefill_input_table` writes a
forecast into an input table through edit_cells. ML-6 (work/validation/X-01.4.md): train/score are flow steps; a refresh
queues one scoring per snapshot for the models reading the table (`after_refresh`); every score checks drift (PSI vs
the training profile; a forecast's recent vs backtest error) and posts a change of drift state to the feed.
Clustering and anomaly detection (no target): abi.ml.unsupervised (E-04.1). ML-ZOO.1 (D-135): a model's `search`/`space`
tunes each candidate on folds of the training part before they are compared (abi.ml.search; trials in `ml_trials`).
1.2.6 M9: a model with `object` predicts once per object of that type: ml_<id> is keyed by the type's key (a forecast: key
and period), so it is one-to-one with the type's table, its prediction becomes a property of the type (`_on_object`)
and its rows follow the object's row-level security through that relationship (rls.conditions); a new scoring starts
the flows that trigger on ml_<id>. scikit-learn, LightGBM and statsforecast are imported inside the functions that fit (cold import ~seconds).
"""

import contextlib
import contextvars
import hashlib
import json
import logging
import re
import uuid
import warnings
from collections import Counter
from decimal import Decimal

import numpy as np
from psycopg.types.json import Jsonb
from pydantic import Field

from abi import actions, db, flows, security, semantic, storage, writeback
from abi.actions import ActionFailed, action
from abi.agent import notify_change
from abi.ml import forecast, search, unsupervised
from abi.project import ID, SCHEMA, git, imports
from abi.project.bundle import bundle
from abi.security import Role, User, credentials, role_in
from abi.semantic import lineage, ontology
from abi.semantic import model as semantic_model

ROWS = 100_000  # D-17: the most training rows (the dataset schema's own `limit` cap)
LEVELS = 20  # one-hot columns per category feature; the rest share "other" (all zeros)
HOLDOUT = 0.2
LEAK = 0.99  # a single feature scoring this on the holdout is a leakage suspect (ML-3)
CALENDAR = ("year", "quarter", "month", "week_year", "week", "day")  # date-table columns, coarse to fine: the time order
DRIFT = 0.2  # ML-6: a PSI above this is drift (a forecast: recent error this much above its backtest error)


def references(rel: str, m: dict, files: dict) -> list[dict]:
    """Import checks of one ML file (imports._references, after its schema passed): the dataset exists; target,
    features and time are columns of its query; time is a date-table column."""
    err = imports._err
    ds = {d["id"]: d for d in (files.get("model/datasets.json") or {}).get("datasets", [])}
    if (d := ds.get(m["dataset"])) is None:
        return [err(rel, "$.dataset", f"Unknown dataset '{m['dataset']}'.",
                    f"Add it to model/datasets.json or use one of: {', '.join(sorted(ds)) or 'none yet'}.")]
    dims = d["query"].get("dimensions", [])
    cols = dims + d["query"]["measures"]
    fix = f"Use a column of dataset '{d['id']}': {', '.join(cols)}."
    errs = [] if m.get("target") in cols + [None] else [err(rel, "$.target", f"'{m['target']}' is not a column of dataset '{d['id']}'.", fix)]
    for i, f in enumerate(m.get("features", [])):
        if f == m.get("target"):
            errs.append(err(rel, f"$.features[{i}]", f"'{f}' is the target; a model can't learn from it.", "Remove it from features."))
        elif f not in cols:
            errs.append(err(rel, f"$.features[{i}]", f"'{f}' is not a column of dataset '{d['id']}'.", fix))
    date = ((files.get("model/model.json") or {}).get("date_table") or {}).get("table")
    if (t := m.get("time")) and t not in dims:
        errs.append(err(rel, "$.time", f"'{t}' is not a dimension of dataset '{d['id']}'.",
                        f"Add it to the dataset's dimensions or use one of: {', '.join(dims) or 'none'}."))
    elif t and date and t.split(".", 1)[0] != date:
        errs.append(err(rel, "$.time", f"'{t}' is not a column of the date table '{date}'.", f"Use {date}.year, .quarter, .month, .week or .day."))
    errs += [err(rel, f"$.space.{k}.{p}", f"The range of {p} starts above its end.", "Swap min and max.")
             for k, ps in m.get("space", {}).items() for p, r in ps.items() if isinstance(r, dict) and r["min"] > r["max"]]
    errs += [err(rel, f"$.params.{k}", f"The fixed setting is for {k}, but method is {m.get('method')}.", f"Set method to {k}.")
             for k in m.get("params", {}) if k != m.get("method")]
    if (oid := m.get("object")) is not None:  # 1.2.6 M9: one prediction per object, so one dataset row per object
        o, mj = files.get("model/ontology.json") or {}, files.get("model/model.json") or {}
        got = ontology.keyed(o, mj, oid)
        if got is None:
            errs.append(err(rel, "$.object", f"Unknown object type '{oid}'.", "Use an object type of model/ontology.json: "
                            f"{', '.join(sorted(x['id'] for x in o.get('objects', []))) or 'none yet (save_ontology first)'}."))
        elif len(key := got[1]) != 1:  # ponytail: one key column; a relationship per key column when a type with more needs a model
            errs.append(err(rel, "$.object", f"Object type '{oid}' is identified by {len(key)} columns ({', '.join(key)}); an ML model"
                                             " predicts for types with one key column.", "Use a type with one key column, or remove `object`."))
        else:
            if key[0] not in dims:
                errs.append(err(rel, "$.object", f"Dataset '{d['id']}' isn't grouped by {key[0]}, the key of object type '{oid}'.",
                                f"Add {key[0]} to the dataset's dimensions (save_dataset), or remove `object`."))
            if m.get("task") != "forecast" and (bad := [c for c in dims if c.split(".", 1)[0] == date] + ([t] if t else [])):
                errs.append(err(rel, "$.object", f"A {m.get('task')} on object type '{oid}' predicts once per {got[0]['name']}, but dataset"
                                                 f" '{d['id']}' has a row per {bad[0]} too.", f"Use a dataset without date columns and remove"
                                                 " `time`; only a forecast on an object keeps them (one series per object)."))
            if key[0] in dims and (other := [c for c in dims if c.split(".", 1)[0] not in (got[0]["table"], date)]):
                errs.append(err(rel, "$.object", f"Dataset '{d['id']}' is also grouped by {', '.join(other)}, so it has several rows for one"
                                                 f" {got[0]['name']}.", f"Keep only columns of table '{got[0]['table']}' as its dimensions"
                                                 " (save_dataset); what comes from another table is a measure, reached through `links`."))
    if "positive" in m and not (m.get("object") and m.get("task") == "classification"):
        errs.append(err(rel, "$.positive", "`positive` names the outcome a classification on an object type gives the probability of.",
                        "Remove `positive`, or set task classification and `object`."))
    return errs


# --- training -------------------------------------------------------------------------------------------------------

def _numeric(vals) -> bool:
    return all(isinstance(v, (int, float, Decimal)) and not isinstance(v, bool) for v in vals if v is not None)


def _encoder(col: str, vals: np.ndarray) -> dict:
    """How one feature becomes model columns: a number standardized (missing = the mean), a category one-hot."""
    if _numeric(vals):
        x = np.array([float(v) for v in vals if v is not None])
        return {"column": col, "kind": "number", "mean": float(x.mean()) if x.size else 0.0, "std": float(x.std()) if x.size and x.std() else 1.0}
    # ponytail: top LEVELS categories only (bounds memory at 100k rows); native LightGBM categoricals if accuracy needs it
    return {"column": col, "kind": "category", "levels": [v for v, _ in Counter(str(v) for v in vals if v is not None).most_common(LEVELS)]}


def encode(feats: list[dict], data: dict) -> np.ndarray:
    """rows x model-columns matrix from `data` ({column: values})."""
    n = len(next(iter(data.values())))
    X = np.zeros((n, len(_owners(feats))))
    j = 0
    for f in feats:
        vals = data[f["column"]]
        if f["kind"] == "number":
            X[:, j] = [0.0 if v is None else (float(v) - f["mean"]) / f["std"] for v in vals]
            j += 1
        else:
            s = np.array([None if v is None else str(v) for v in vals], dtype=object)
            for lv in f["levels"]:
                X[:, j] = s == lv
                j += 1
    return X


def _owners(feats: list[dict]) -> list[int]:
    """The feature index of each model column."""
    return [i for i, f in enumerate(feats) for _ in range(1 if f["kind"] == "number" else len(f["levels"]))]


METHODS = ("lightgbm", "random_forest")  # D-99: classification/regression candidates, best first
RETIRED = {"linear": "auto, lightgbm or random_forest", "arima": "auto, lightgbm or ets"}  # D-99: stored runs still score
LABEL = {"lightgbm": "LightGBM", "random_forest": "random forest", "ets": "ETS", "kmeans": "k-means", "gmm": "Gaussian mixture",
         "isolation_forest": "Isolation Forest", "seasonal": "seasonal rule"}


def _estimator(task: str, method: str, params: dict | None = None):
    """An unfitted candidate with today's defaults, overridden by a searched setting (ML-ZOO.1; schema-bounded)."""
    if method == "random_forest":
        from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
        # D-17: at most 100 trees x 2,047 nodes, kept as JSON arrays (100k rows: ~0.7 GB peak, work/validation/ML-CANDIDATES.1)
        kw = {"n_estimators": 100, "max_depth": 10, "min_samples_leaf": 5, "n_jobs": 1, "random_state": 0} | (params or {})
        return RandomForestClassifier(class_weight="balanced", **kw) if task == "classification" else RandomForestRegressor(**kw)
    import lightgbm
    kw = {"n_jobs": 1, "verbose": -1, "random_state": 0} | (params or {})
    return lightgbm.LGBMClassifier(class_weight="balanced", **kw) if task == "classification" else lightgbm.LGBMRegressor(**kw)


def _fit(task: str, method: str, X, y, params: dict | None = None):
    from threadpoolctl import threadpool_limits  # D-17: single-threaded fits (BLAS too)

    with threadpool_limits(1), warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return _estimator(task, method, params).fit(X, y)


def _cv(task: str, method: str, params: dict, X, y, folds: list) -> tuple[float, float]:
    """ML-ZOO.1: a setting's mean and spread of the candidates' metric (balanced accuracy, or MAE) over `folds`."""
    from sklearn.model_selection import cross_validate
    from threadpoolctl import threadpool_limits

    cls = task == "classification"
    with threadpool_limits(1), warnings.catch_warnings():
        warnings.simplefilter("ignore")
        s = cross_validate(_estimator(task, method, params), X, y, cv=folds, n_jobs=1, error_score=np.nan,
                           scoring="balanced_accuracy" if cls else "neg_mean_absolute_error")["test_score"]
    s = s if cls else -s
    return float(s.mean()), float(s.std())


def _score(task: str, y, pred) -> dict:
    from sklearn.metrics import (
        balanced_accuracy_score,
        f1_score,
        mean_absolute_error,
        r2_score,
    )

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        if task == "classification":
            return {"balanced_accuracy": float(balanced_accuracy_score(y, pred)), "macro_f1": float(f1_score(y, pred, average="macro"))}
        return {"mae": float(mean_absolute_error(y, pred)), "r2": float(r2_score(y, pred))}


def _better(task: str, a: dict, b: dict) -> bool:
    return a["balanced_accuracy"] > b["balanced_accuracy"] if task == "classification" else a["mae"] < b["mae"]


def _split(m: dict, data: dict, y) -> tuple[np.ndarray, str, dict]:
    """ML-3: the holdout mask, how to say it and the figures that says (AG-2: stored as the run's `holdout`). With
    `time`: the latest ~20% of periods (order: the query's date-table columns, year before month); else a seeded split,
    stratified by class when every class has 2+ rows."""
    n = len(y)
    if t := m.get("time"):
        tcols, keys, periods = _periods(m, data)
        if len(periods) < 2:
            raise ActionFailed(400, f"Training needs at least 2 periods of {t} to hold the latest out; the dataset has {len(periods)}.")
        k = max(1, round(len(periods) * HOLDOUT))
        last = set(periods[-k:])
        return (np.array([key in last for key in keys]), f"the latest {k} of {len(periods)} periods ({', '.join(tcols)})",
                {"holdout periods": k, "periods": len(periods)})
    from sklearn.model_selection import train_test_split

    strat = y if m["task"] == "classification" and min(Counter(y).values()) >= 2 else None
    try:
        _, test = train_test_split(np.arange(n), test_size=HOLDOUT, random_state=0, stratify=strat)
    except ValueError:  # too few rows per class for a stratified 20%
        _, test = train_test_split(np.arange(n), test_size=HOLDOUT, random_state=0)
    mask = np.zeros(n, dtype=bool)
    mask[test] = True
    return mask, f"a held-out {HOLDOUT:.0%} of the rows", {"holdout share": HOLDOUT}


def _periods(m: dict, data: dict) -> tuple[list[str], list[tuple], list[tuple]]:
    """(the query's date-table columns, coarse to fine; each row's period; the periods in time order)."""
    date = m["time"].split(".", 1)[0]
    tcols = sorted((c for c in data if c.split(".", 1)[0] == date),
                   key=lambda c: CALENDAR.index(c.split(".", 1)[1]) if c.split(".", 1)[1] in CALENDAR else len(CALENDAR))
    keys = [tuple(data[c][i] for c in tcols) for i in range(len(next(iter(data.values()))))]
    return tcols, keys, sorted(set(keys), key=lambda k: [(v is None, v) for v in k])


def _folds(m: dict, data: dict, y) -> list[tuple]:
    """ML-ZOO.1: search folds over the training rows given (never the holdout): with `time` rolling-origin by period
    (scikit-learn's TimeSeriesSplit over the periods), else stratified k-fold (classification, 3+ rows per class) or
    k-fold, shuffled with the search's seed. [] when there are too few periods or rows for them."""
    from sklearn.model_selection import KFold, StratifiedKFold, TimeSeriesSplit

    if m.get("time"):
        _, keys, periods = _periods(m, data)
        if len(periods) < 3:
            return []
        at = {p: i for i, p in enumerate(periods)}
        at = np.array([at[k] for k in keys])
        return [(np.flatnonzero(np.isin(at, a)), np.flatnonzero(np.isin(at, b)))
                for a, b in TimeSeriesSplit(min(search.FOLDS, len(periods) - 1)).split(periods)]
    if len(y) < 2 * search.FOLDS:
        return []
    strat = m["task"] == "classification" and min(Counter(y).values()) >= search.FOLDS
    cv = (StratifiedKFold if strat else KFold)(search.FOLDS, shuffle=True, random_state=m["search"].get("seed", 0))
    return list(cv.split(np.zeros(len(y)), y))


def _names(project: str) -> dict:
    """Business names: measure display names; a dimension column by the glossary term that names it."""
    names = {m["id"]: m["name"] for m in semantic_model._read(project, "measures.json").get("measures", [])}
    for t in semantic_model._read(project, "glossary.json").get("terms", []):
        names |= {f"{c['table']}.{c['column']}": t["term"] for c in t.get("columns", [])}
    return names


def _importance(method: str, est, feats: list[dict], names: dict) -> list[dict]:
    raw = est.feature_importances_ if method == "random_forest" else est.booster_.feature_importance("gain")
    per = np.zeros(len(feats))
    np.add.at(per, _owners(feats), raw)
    per = per / per.sum() if per.sum() else per
    return sorted(({"feature": f["column"], "name": names.get(f["column"], f["column"]), "share": round(float(s), 4)}
                   for f, s in zip(feats, per)), key=lambda x: -x["share"])


def versus(cands: dict, method: str, metric: str, window: str) -> str:
    """ML-4, D-99: the candidates trained and the one kept, in a sentence of code-made numbers (principle 7); H-07:
    the scores it was kept on (`validation_<metric>` when a validation window was used) and that `window`."""
    if len(cands) < 2:
        return f" Method: {LABEL[method]} (set in the model)."
    other = next(k for k in cands if k != method)
    f = "{:.0%}" if metric == "balanced_accuracy" else "{:,.2f}"
    key = f"validation_{metric}" if f"validation_{metric}" in cands[method] else metric
    return (f" Trained {LABEL[method]} and {LABEL[other]} and kept {LABEL[method]}: {metric.replace('_', ' ')}"
            f" {f.format(cands[method][key])} vs {f.format(cands[other][key])} on {window}.")


SHORT = "the held-out data itself (too little data for a separate validation part), so the accuracy above is a little optimistic"


def choose(cands: dict, scores: dict | None, metric: str, better) -> str:
    """H-07: the candidate kept: the first unless another is `better` on the validation `scores` (None: too little data,
    so on the held-out scores in `cands`). Adds each candidate's validation score to `cands` as `validation_<metric>`."""
    for k, sc in (scores or {}).items():
        cands[k][f"validation_{metric}"] = sc[metric]
    scores, first = scores or cands, next(iter(cands))
    return next((k for k in list(cands)[1:] if better(scores[k], scores[first])), first)


def _validation(m: dict, data: dict, y, test) -> tuple[np.ndarray | None, str, dict]:
    """H-07: a validation mask over the training rows (the test rows stay unseen until reporting), how to say it and
    its figures (`validation <name>`), split like the holdout; (None, "", {}) when the training part is too small for one."""
    try:
        val, text, figs = _split(m, {c: v[~test] for c, v in data.items()}, y[~test])
    except ActionFailed:
        return None, "", {}
    if val.all() or not val.any() or (m["task"] == "classification" and len(set(y[~test][~val])) < 2):
        return None, "", {}
    return val, text, {f"validation {k}": v for k, v in figs.items()}


def _leaks(m: dict, data: dict, target, y, test) -> list[str]:
    """ML-3 leakage suspects: a feature equal to the target, or one that alone scores >= LEAK on the holdout
    (balanced accuracy, or R² for regression), with a small tree."""
    from sklearn.tree import DecisionTreeClassifier, DecisionTreeRegressor

    out, tgt = [], [semantic.norm(v) for v in target]
    for c in m["features"]:
        if [semantic.norm(v) for v in data[c]] == tgt:
            out.append(c)
            continue
        X = encode([_encoder(c, data[c][~test])], {c: data[c]})
        tree = (DecisionTreeClassifier if m["task"] == "classification" else DecisionTreeRegressor)(max_depth=8, random_state=0)
        s = _score(m["task"], y[test], tree.fit(X[~test], y[~test]).predict(X[test]))
        if s["balanced_accuracy" if m["task"] == "classification" else "r2"] >= LEAK:
            out.append(c)
    return out


def _dataset(project: str, m: dict) -> dict:
    ds = {d["id"]: d for d in semantic_model._read(project, "datasets.json").get("datasets", [])}
    if m["dataset"] not in ds:
        raise ActionFailed(400, f"ML model '{m['id']}' trains on dataset '{m['dataset']}', which no longer exists.")
    return ds[m["dataset"]]


def _query(project: str, m: dict) -> dict:
    """ML-5 traceability: the exact dataset query a run trains on (rows at the dataset's grain only), kept on the run."""
    return {**_dataset(project, m)["query"], "totals": []}


def _rows(project: str, m: dict, query: dict, user_id: int | None) -> tuple[int, list[str], dict]:
    """(snapshot, columns, {column: values}) of the dataset query, read through the semantic engine as the caller (ML-2)."""
    if semantic.rls.restricted(project, user_id):  # SE-6: a model of filtered rows would become everyone's table
        raise ActionFailed(403, "Row-level security rules restrict your rows in this project, so ML models can't be"
                                " trained or scored as you; ask an Editor.")
    q = {**query, "limit": min(query.get("limit") or ROWS, ROWS)}
    # ponytail: the snapshot is read just before the query, so a refresh in between records the older one; return it from execute if that matters
    snap = storage.snapshot_id(storage.lake(project))
    out = semantic.query(project, q, user_id)
    if len(out["rows"]) >= ROWS and not query.get("limit"):
        raise ActionFailed(400, f"Dataset '{m['dataset']}' has {ROWS:,} rows or more; a model reads at most {ROWS:,} (D-17)."
                                " Use a coarser grain, a filter or a limit in the dataset.")
    cols = list(zip(*out["rows"])) or [()] * len(out["columns"])
    return snap, out["columns"], {c: np.array(v, dtype=object) for c, v in zip(out["columns"], cols)}


def _train(project: str, m: dict, query: dict, user_id: int | None) -> dict:
    """Read, guard, fit, evaluate: the registry fields of one training run."""
    snap, columns, data = _rows(project, m, query, user_id)
    if m["task"] in unsupervised.TASKS:
        return {"commit": str(git.repo(project).head.target), "snapshot": snap, **unsupervised.train(project, m, query, columns, data, _names(project))}
    keep = np.array([v is not None for v in data[m["target"]]], dtype=bool)
    data = {c: v[keep] for c, v in data.items()}
    target = data[m["target"]]
    if m["task"] == "forecast":
        dt = semantic_model._read(project, "model.json").get("date_table") or {}
        return {"commit": str(git.repo(project).head.target), "snapshot": snap, **forecast.train(m, query, data, dt, _names(project))}
    feats = m.get("features") or [c for c in columns if c != m["target"]]
    m = {**m, "features": feats}
    if len(target) < 10:
        raise ActionFailed(400, f"Training needs at least 10 rows with a value for {m['target']}; dataset '{m['dataset']}' has {len(target)}.")
    if m["task"] == "regression":
        if not _numeric(target):
            raise ActionFailed(400, f"Regression predicts a number, but {m['target']} has text values; use task classification.")
        y = np.array([float(v) for v in target])
    else:
        y = np.array([str(v) for v in target], dtype=object)
    test, holdout, sizes = _split(m, data, y)
    if test.all() or not test.any():
        raise ActionFailed(400, "Training needs rows both before and in the holdout.")
    classes = None
    if m["task"] == "classification":
        classes = sorted(set(y[~test]))
        if len(classes) < 2:
            raise ActionFailed(400, f"The training part has only one value of {m['target']} ('{classes[0]}'); there is nothing to tell apart.")
        y = np.array([classes.index(v) if v in classes else -1 for v in y])  # a class only in the holdout is never predicted
    excluded = _leaks(m, data, target, y, test)
    feats = [f for f in feats if f not in excluded]
    if not feats:
        raise ActionFailed(400, f"Every feature was left out as a leakage suspect ({', '.join(excluded)}); add other features.")
    enc = [_encoder(c, data[c][~test]) for c in feats]
    X = encode(enc, data)
    if m["task"] == "classification":
        majority = Counter(y[~test]).most_common(1)[0][0]
        base, base_text = np.full(test.sum(), majority), f"always guessing '{classes[majority]}'"
    else:
        base, base_text = np.full(test.sum(), y[~test].mean()), "always predicting the average"
    baseline = _score(m["task"], y[test], base)
    tries = METHODS if m.get("method", "auto") == "auto" else [m["method"]]
    metric = "balanced_accuracy" if m["task"] == "classification" else "mae"
    params, trials = {k: m.get("params", {}).get(k) for k in tries}, []  # ML-ZOO.2: a fixed setting (use_ml_trial)
    if folds := m.get("search") and _folds(m, {c: v[~test] for c, v in data.items()}, y[~test]):
        Xt, yt = X[~test], y[~test]  # ML-ZOO.1: each candidate's setting is chosen on folds of the training part only
        params, trials = search.candidates(tries, m.get("space", {}), m["search"], lambda k, p: _cv(m["task"], k, p, Xt, yt, folds),
                                           metric == "mae", metric)
    fits = {}
    for k in tries:
        est = _fit(m["task"], k, X[~test], y[~test], params[k])
        fits[k] = (est, _score(m["task"], y[test], est.predict(X[test])))
    cands, sel, window = {k: dict(s) for k, (_, s) in fits.items()}, None, SHORT
    if folds:
        how = f"{len(folds)} {'rolling-origin' if m.get('time') else 'cross-validation'} folds of the training part"
        sizes |= {"search folds": len(folds)}
    if len(tries) > 1 and (val := _validation(m, data, y, test))[0] is not None:  # H-07: choose on rows the report never sees
        Xt, yt, v = X[~test], y[~test], val[0]  # with a search: each candidate at its kept setting
        sel = {k: _score(m["task"], yt[v], _fit(m["task"], k, Xt[~v], yt[~v], params[k]).predict(Xt[v])) for k in tries}
        window, sizes = f"{val[1]} of the training part (validation), not the holdout reported above", sizes | val[2]
    elif folds:  # no validation window: the kept settings' fold scores (a best of several: a little optimistic)
        sel = {k: {metric: next(t["score"] for t in trials if t["method"] == k and t["chosen"])} for k in tries}
        window = f"{how} (search), not the holdout reported above"
    method = choose(cands, sel, metric, lambda a, b: _better(m["task"], a, b))
    est, metrics = fits[method]
    names = _names(project)
    importance = _importance(method, est, enc, names)
    better = _better(m["task"], metrics, baseline)
    fitted = fitted_text(m["task"], method, enc, classes, est)
    summary = _summary(m, names, holdout, metrics, baseline, base_text, better, excluded, importance)
    summary += versus(cands, method, metric, window) + (search.said(cands, trials, how) if folds else "")
    return {"commit": str(git.repo(project).head.target), "snapshot": snap, "method": method, "metrics": metrics,
            "baseline": baseline, "better": better, "importance": importance, "excluded": excluded, "candidates": cands,
            "summary": summary, "holdout": sizes, "trials": trials,
            "fitted": fitted, "profile": profile({c: data[c] for c in feats} | {"prediction": predict(fitted, data)})}


# --- drift (ML-6) ---------------------------------------------------------------------------------------------------

def _shares(vals, spec: dict) -> np.ndarray:
    """Shares of `vals` in the bins of `spec`: number bins split at `edges` (or category `levels`, then "other"),
    and a last bin for missing values."""
    bins = spec.get("edges", spec.get("levels"))
    counts = np.zeros(len(bins) + 2)
    # ponytail: a Python loop over <= 100k values per column (~0.1 s); vectorize with np.searchsorted if scoring time matters
    for v in vals:
        if v is None:
            counts[-1] += 1
        elif "edges" in spec:
            counts[np.searchsorted(bins, float(v), side="right")] += 1
        else:
            counts[bins.index(str(v)) if str(v) in bins else len(bins)] += 1
    return counts / max(len(vals), 1)


def profile(cols: dict) -> dict:
    """ML-6: each column's training distribution: decile edges of a number (top levels of a category) and the shares."""
    out = {}
    for c, vals in cols.items():
        if _numeric(vals):
            x = np.array([float(v) for v in vals if v is not None])
            spec = {"edges": np.unique(np.quantile(x, np.linspace(0.1, 0.9, 9))).tolist() if x.size else []}
        else:
            spec = {"levels": [v for v, _ in Counter(str(v) for v in vals if v is not None).most_common(LEVELS)]}
        out[c] = spec | {"shares": _shares(vals, spec).tolist()}
    return out


def psi(expected, actual) -> float:
    """Population stability index; an empty bin counts as 0.01% so the log stays finite."""
    e, a = np.maximum(np.asarray(expected), 1e-4), np.maximum(np.asarray(actual), 1e-4)
    return float(((a - e) * np.log(a / e)).sum())


def _drift(m: dict, run: dict, query: dict, data: dict, dt: dict, preds: dict) -> dict:
    """ML-6: drift of one scoring against its training run: PSI of each feature and of the prediction (a forecast:
    its recent error vs its backtest error). `drifted` lists what moved past DRIFT; None = unknown."""
    if m["task"] == "forecast":
        f = json.loads(run["fitted"])  # the latest backtest window's error (ML-SEASONAL.1: metrics pool several windows)
        recent, back = forecast.recent_error(m, query, data, dt, f), f.get("latest_mae", run["metrics"]["mae"])
        return {"recent_mae": recent, "backtest_mae": back, "threshold": DRIFT,
                "drifted": None if recent is None else ["forecast error"] if recent > back * (1 + DRIFT) else []}
    if not run["profile"]:  # trained before X-01.4: no reference to compare with
        return {"drifted": None}
    cols = data | preds  # the profile names the features and the output column (prediction, cluster or anomaly)
    out = {c: round(psi(p["shares"], _shares(cols[c], p)), 4) for c, p in run["profile"].items()}
    return {"psi": out, "threshold": DRIFT, "drifted": [c for c, v in out.items() if v > DRIFT]}


def _post_drift(project: str, m: dict, drifted: list | None) -> None:
    """MA-7: one feed post per change of drift state (fixed texts: new PSI values alone post nothing)."""
    if drifted is None:
        return
    what = ("its recent forecast error is more than 20% above its backtest error" if m["task"] == "forecast"
            else "its input data or predictions moved away from its training data (PSI above 0.2)")
    notify_change("ml_drift", project, f"ML model {m['name']}: ", drifted and f"drift detected: {what}. Retrain it (train_model).",
                  "no drift any more.", m["id"])


def booster_text(booster) -> dict:
    """LightGBM's text model plus its sha256: LightGBM aborts the whole process (C++ abort, not an exception) on some
    corrupt texts, such as one cut off mid-tree, so `_restore` checks the hash before LightGBM parses it."""
    s = booster.model_to_string()
    return {"booster": s, "booster_sha256": hashlib.sha256(s.encode()).hexdigest()}


def fitted_text(task: str, method: str, enc: list[dict], classes: list | None, est) -> str:
    """ML-4: the fitted model as text: encoders plus LightGBM's text model or the forest's tree arrays (leaf `value`:
    class shares or the mean target, scored by `predict` like the Isolation Forest, E-04.1). Never a pickle."""
    f = {"method": method, "task": task, "features": enc, "classes": classes}
    if method == "random_forest":
        f["trees"] = [{"feature": t.feature.tolist(), "threshold": t.threshold.tolist(), "left": t.children_left.tolist(),
                       "right": t.children_right.tolist(), "value": t.value[:, 0, :].tolist()} for t in (e.tree_ for e in est.estimators_)]
    else:
        f |= booster_text(est.booster_)
    return json.dumps(f)


def _summary(m, names, holdout, metrics, baseline, base_text, better, excluded, importance) -> str:
    """ML-4: results in business terms, as template sentences filled by code (principle 7)."""
    t = names.get(m["target"], m["target"])
    if m["task"] == "classification":
        s = (f"On {holdout}, it gets {metrics['balanced_accuracy']:.0%} of each {t} value right on average (balanced accuracy),"
             f" vs {baseline['balanced_accuracy']:.0%} for {base_text}.")
    else:
        s = f"On {holdout}, it is off by {metrics['mae']:,.2f} {t} per row on average, vs {baseline['mae']:,.2f} for {base_text}."
    s += " It is better than the baseline." if better else " It is not better than the baseline: don't use its predictions."
    if excluded:
        s += (f" Left out {', '.join(names.get(c, c) for c in excluded)}: each alone predicts {t} almost perfectly,"
              " which suggests leakage.")
    if top := [x["name"] for x in importance[:3] if x["share"] > 0]:
        s += f" Strongest drivers: {', '.join(top)}."
    return s


RETRAIN = "This model was trained before stored models carried a checksum; train it again (train_model), then score it."


def _booster(f: dict):
    """The stored LightGBM model of fitted JSON `f`, parsed only when its checksum matches (booster_text); else None."""
    if "booster_sha256" not in f:
        raise ValueError(RETRAIN)  # X-01.1-era text: unchecked, not known to be corrupt
    if hashlib.sha256(str(f.get("booster")).encode()).hexdigest() == f["booster_sha256"]:
        import lightgbm
        return lightgbm.Booster(model_str=f["booster"])  # LightGBM's text format: parsed, never executed
    return None


def _restore(fitted: str) -> dict:
    """A stored fitted model, validated: JSON with the expected shape, never unpickled."""
    try:
        f = json.loads(fitted)
        width = len(_owners(f["features"]))
        k = 1 if f["task"] == "regression" or len(f["classes"]) == 2 else len(f["classes"])
        if f["method"] == "linear":  # retired (D-99); runs trained before it still score
            coef, icpt = np.array(f["coef"], dtype=float), np.array(f["intercept"], dtype=float)
            ok = coef.shape == (k, width) and icpt.shape == (k,)
            f["model"] = (coef, icpt)
        elif f["method"] == "random_forest":
            f["model"] = unsupervised._trees(f, width, "value")
            ok = bool(f["model"]) and all(t["value"].shape[1:] == (1 if f["task"] == "regression" else len(f["classes"]),) for t in f["model"])
        elif f["method"] == "lightgbm":
            f["model"] = _booster(f)
            ok = f["model"] is not None and f["model"].num_feature() == width
        else:
            ok = False
    except Exception as e:  # any parse failure (LightGBMError too) means the stored text is invalid
        if str(e) == RETRAIN:
            raise
        raise ValueError(f"The stored fitted model is invalid: {type(e).__name__}.") from None
    if not ok:
        raise ValueError("The stored fitted model is invalid: its shape or checksum doesn't match.")
    return f


def predict(fitted: str, data: dict, proba: bool = False) -> list | tuple:
    """Predictions of a registry model for `data` ({column: values}): class labels or numbers. `proba` (1.2.6 M9, a
    classification): (the labels, each row's probability of the second class when there are two classes, else None)."""
    f = _restore(fitted)
    X = encode(f["features"], data)
    if f["method"] == "linear":
        coef, icpt = f["model"]
        z = X @ coef.T + icpt
    elif f["method"] == "random_forest":  # sklearn: the mean over trees of each leaf's value (class shares, normalized)
        X = X.astype(np.float32).astype(float)  # sklearn's trees compare float32 inputs
        z = 0
        for t in f["model"]:
            v = t["value"][unsupervised.leaves(t, X)[0]]
            z = z + (v if f["task"] == "regression" else v / v.sum(1, keepdims=True))
        z = z / len(f["model"])
    else:
        z = f["model"].predict(X, num_threads=1)
        z = z.reshape(len(X), -1)
    if f["task"] == "regression":
        return z[:, 0].tolist()
    idx = z.argmax(1) if z.shape[1] > 1 else (z[:, 0] > (0 if f["method"] == "linear" else 0.5)).astype(int)
    labels = [f["classes"][i] for i in idx]
    if not proba:
        return labels
    if len(f["classes"]) != 2:
        return labels, None
    p = z[:, 1] if z.shape[1] > 1 else 1 / (1 + np.exp(-z[:, 0])) if f["method"] == "linear" else z[:, 0]
    return labels, p.tolist()


# --- registry and actions -------------------------------------------------------------------------------------------

RESULT = ("id::text AS run, kind, trained::text, model, status, created, finished, commit, snapshot, query, method, metrics,"
          " baseline, better AS better_than_baseline, candidates, holdout, importance, excluded, groups, summary, drift, error")


FIGURES = ["kind", "run", "figure", "value"]  # D-114: no model ID (agent-named, save_ml_model): its digits would ground


def figures(r: dict | None) -> list[list]:
    """A registry run's code-made numbers, ids and times as result rows: the only ones the agent may quote (principle 7,
    D-22; GATE-9-fix-8: its run ID, the training run a scoring run used, when it was created, finished and pinned).
    D-123: only structured values under fixed labels; free texts that may quote chosen names (summary, error, drifted
    columns) and labels built from column names stay out (the run's `summary`, `error`, `importance`, `groups`, `drift`)."""
    if not r:
        return []
    d = r["drift"] or {}
    out = [(k, r.get(k)) for k in ("status", "created", "finished", "pinned", "trained", "better_than_baseline")]
    out += [(f"{k} {n}", v) for k in ("metrics", "baseline") for n, v in (r[k] or {}).items()]  # code-made metric names
    out += [(f"candidate {LABEL.get(c, c)} {n}", v) for c, ms in (r["candidates"] or {}).items() for n, v in ms.items()]  # D-99
    out += list((r["holdout"] or {}).items())  # the held-out sizes its summary states (code-made labels)
    out += [("importance", x["share"]) for x in r["importance"] or []]  # its feature's name: `importance`, outside rows
    for g in r["groups"] or []:  # clustering profiles (ML-4); column names in `groups`, outside rows
        out += [(f"cluster {g['cluster']} rows", g["rows"])] + [(f"cluster {g['cluster']} mean", v) for v in g["mean"].values()]
        out += [(f"cluster {g['cluster']} most common", v) for v in g["most_common"].values()]
    out += [("drift psi", v) for v in (d.get("psi") or {}).values()]
    out += [(f"drift {k}", d[k]) for k in ("recent_mae", "backtest_mae", "threshold") if k in d]
    return [[r["kind"], r["run"], f, v] for f, v in out if v is not None]


def hidden(r: dict) -> dict:
    """A registry run without its figures and texts (metrics, drift, summary, error): they come from every row."""
    return r | dict.fromkeys(("metrics", "baseline", "better_than_baseline", "candidates", "holdout", "importance", "groups", "drift")) | {
        "summary": semantic.rls.HIDDEN, "error": r["error"] and semantic.rls.HIDDEN}


def latest(conn, project: str) -> dict:
    """{(model, kind): the latest registry run} of a project, with the user who started it."""
    return {(r["model"], r["kind"]): r for r in conn.execute(
        f"SELECT DISTINCT ON (model, kind) {RESULT}, user_id FROM ml_runs WHERE project = %s ORDER BY model, kind, created DESC",
        [project])}


def trials(conn, run_id: str, hide: bool = False) -> list[dict]:
    """A run's ml_trials rows; `hide` (SE-6, a restricted Viewer): without the fold scores and the pick they decided."""
    ts = conn.execute("SELECT position, method, params, metric, score, score_std, seconds, status, chosen FROM ml_trials"
                      " WHERE run = %s ORDER BY position", [run_id]).fetchall()
    return [t | dict.fromkeys(("score", "score_std", "chosen")) for t in ts] if hide else ts


def trial_rows(conn, run_id: str, hide: bool = False) -> list[list]:
    """ML-ZOO.1/.2: a run's search trials as FIGURES rows (D-22) under code-made labels (position, candidate, metric):
    each trial's status, fold score, its spread, fit time, `chosen`, and its settings when code made them (the default
    space, sampled or not). D-114: a candidate whose space the model file lists (author- or agent-written) keeps its
    settings out of rows: a listed value, or a range's ends, would ground a figure its author chose."""
    spec = conn.execute("SELECT spec FROM ml_runs WHERE id = %s", [run_id]).fetchone()
    own = ((spec and spec["spec"]) or {}).get("space") or {}
    return [["train", run_id, f"trial {t['position']} {LABEL[t['method']]} {lab}", v] for t in trials(conn, run_id, hide)
            for lab, v in (("status", t["status"]), (f"cv {t['metric']}", t["score"]), (f"cv {t['metric']} std", t["score_std"]),
                           ("seconds", t["seconds"]), ("chosen", t["chosen"] or None),
                           *(() if t["method"] in own else t["params"].items()))
            if v is not None]


def _finish(conn, run_id: str, out: dict, error: str | None) -> dict:
    j = {k: Jsonb(out.get(k)) for k in ("metrics", "baseline", "importance", "excluded", "groups", "profile", "drift", "candidates", "holdout")}
    for t in out.get("trials") or []:  # ML-ZOO.1: the search's trials, kept with the run (ML-4)
        conn.execute("INSERT INTO ml_trials (run, position, method, params, metric, score, score_std, seconds, status, chosen)"
                     " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                     [run_id, t["position"], t["method"], Jsonb(t["params"]), t["metric"], t["score"], t["score_std"],
                      t["seconds"], t["status"], t["chosen"]])
    return conn.execute(
        "UPDATE ml_runs SET status = %s, error = %s, finished = now(), commit = %s, snapshot = %s, method = %s, metrics = %s,"
        " baseline = %s, better = %s, importance = %s, excluded = %s, groups = %s, summary = %s, fitted = %s, profile = %s, drift = %s,"
        " candidates = %s, holdout = %s"
        f" WHERE id = %s RETURNING {RESULT}",
        ["failed" if error else "ok", error, out.get("commit"), out.get("snapshot"), out.get("method"), j["metrics"],
         j["baseline"], out.get("better"), j["importance"], j["excluded"], j["groups"], out.get("summary"), out.get("fitted"),
         j["profile"], j["drift"], j["candidates"], j["holdout"], run_id]).fetchone()


def job(run_id: str) -> None:
    """The worker's job (abi.worker.train_model / score_model): run a queued training or scoring run as the user who
    asked for it; scoring's model file changes land as one undoable Git change, like the action's."""
    with db.connect() as conn:
        r = conn.execute("UPDATE ml_runs SET status = 'running' WHERE id = %s AND status = 'queued'"
                         " RETURNING project, kind, spec, query, user_id, trained::text", [run_id]).fetchone()
        u = r and security.user(conn, r["user_id"])
    if r is None:
        return
    out, error = {}, None
    try:
        if r["kind"] == "train":
            out = _train(r["project"], r["spec"], r["query"], r["user_id"])
        elif u is None:
            raise ActionFailed(400, "The user who asked for this scoring run no longer exists.")
        else:
            with git.change(r["project"], u, "score_model"):
                # `object` only while the model's file still has the one this run was queued with: a run queued before
                # the model was taken off its object type, moved to another or deleted writes its predictions (its dataset
                # was read for the queued type) and puts no property on any type
                now = (bundle(git.path(r["project"])).get(f"ml/{r['spec']['id']}.json") or {}).get("object")
                spec = {k: v for k, v in r["spec"].items() if k != "object"} | ({"object": now} if now and now == r["spec"].get("object") else {})
                out = _predictions(r["project"], spec, r["trained"], r["query"], u)
    except Exception as e:  # noqa: BLE001 - any failure is the run's outcome
        error = str(e)
    with db.connect() as conn:
        _finish(conn, run_id, out, error)


def _launch(conn, user: User, project: str, m: dict, kind: str, query: dict, trained: str | None, work,
            key: str | None = None) -> dict | None:
    """A registry run of `kind`: in the worker (a flow step) `work()` runs inline; elsewhere the run is queued for the
    worker (§9) and its ID returned. A failure is kept as a failed run, whatever the error (ML-4). A run `key` the
    model already has starts nothing (None): ML-6 scores once per refresh snapshot."""
    run_id, inline = str(uuid.uuid4()), flows.IN_FLOW.get()  # a flow step already runs in the worker (§9)
    if not conn.execute("INSERT INTO ml_runs (id, project, model, kind, trained, user_id, spec, query, status, key)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                        [run_id, project, m["id"], kind, trained, user.id, Jsonb(m), Jsonb(query),
                         "running" if inline else "queued", key]).rowcount:
        return None
    if not inline:
        conn.commit()  # before the worker can pick the job up
        from abi import worker  # not at top: the worker imports every action module

        try:
            worker.defer(worker.train_model if kind == "train" else worker.score_model, run_id=run_id)
        except Exception:
            conn.execute("DELETE FROM ml_runs WHERE id = %s", [run_id])
            raise
        return {"run": run_id, "status": "queued", "columns": FIGURES, "rows": [[kind, run_id, "status", "queued"]]}
    try:
        out = work()
    except Exception as e:
        _finish(conn, run_id, {}, str(e))
        conn.commit()  # the failed run stays in the registry; the action's own changes roll back
        raise
    row = _finish(conn, run_id, out, None)
    return {**row, "columns": FIGURES, "rows": figures(row) + trial_rows(conn, run_id)}  # D-22: quotable figures


# --- scoring (ML-5) -------------------------------------------------------------------------------------------------

def table_of(mid: str) -> str:
    """The model table holding an ML model's predictions."""
    return f"ml_{mid}"


def measure_of(mid: str) -> str:
    return f"{mid[:52]}_prediction"  # measure IDs are at most 63 characters


ACTIVE = ("SELECT DISTINCT ON (model) model, id::text AS run, query, pinned FROM ml_runs WHERE project = %s AND kind = 'train'"
          " AND status = 'ok' {} ORDER BY model, pinned IS NULL, created DESC")  # pinned first (pin_ml_run), else the latest


def _trained(conn, project: str, mid: str) -> dict:
    """ML-4 (D-99): the training run scoring uses: the pinned one, else the latest successful."""
    r = conn.execute(ACTIVE.format("AND model = %s"), [project, mid]).fetchone()
    if r is None:
        raise ActionFailed(400, f"ML model '{mid}' has no successful training run to score with; train it first (train_model).")
    return r


def _in_place(old: list[dict], new: list[dict], gone) -> list[dict]:
    """`old` without its `gone` entries, each entry of `new` in the place of the old one with its ID (else appended):
    scoring the same model again (ML-6, after every refresh) leaves model.json and measures.json as they were."""
    by = {x["id"]: x for x in new}
    return [by.pop(x["id"], x) for x in old if x["id"] in by or not gone(x)] + list(by.values())


def _predictions(project: str, m: dict, trained: str, query: dict, user: User) -> dict:
    """Predict on fresh dataset rows with a stored training run and write them as model table ml_<id> (one snapshot),
    a fact related to the dataset's keyed dimensions (and the date table for a forecast), with measure <id>_prediction.
    1.2.6 M9, a model on an object type: one row per object (a forecast: per object and period), keyed so, and the
    prediction a property of the type (`_on_object`); then the flows that trigger on ml_<id> start (not inside a flow)."""
    with db.connect() as conn:
        run = conn.execute("SELECT fitted, profile, metrics FROM ml_runs WHERE id = %s", [trained]).fetchone()
    fitted, pos = run["fitted"], None
    _, _, data = _rows(project, m, query, user.id)  # ML-2/ML-5: fresh rows through the semantic engine, as the caller
    mj, types = semantic_model.load(project), semantic_model.lake_columns(project)
    dt = mj["date_table"] or {}
    try:
        if m["task"] == "forecast":
            preds = forecast.predict(m, query, data, dt, json.loads(fitted))
        elif m["task"] in unsupervised.TASKS:
            preds = {c: data[c] for c in query.get("dimensions", [])} | unsupervised.predict(m, query, data, dt, fitted)
        else:
            labels, p = predict(fitted, data, True) if m["task"] == "classification" else (predict(fitted, data), None)
            classes = json.loads(fitted).get("classes")
            pos = positive(m, classes) if m.get("object") and p is not None else None
            p = p if pos is None or pos == classes[1] else [1 - v for v in p]  # `predict` gives the second class's
            preds = {c: data[c] for c in query.get("dimensions", [])} | {"prediction": labels} | ({} if pos is None else {"probability": p})
        n = len(next(iter(preds.values())))
        drift = _drift(m, run, query, data, dt, preds) if n else {}
    except ValueError as e:  # an invalid stored model, or one to retrain
        raise ActionFailed(400, str(e))
    said = _names(project).get(m.get("target")) or ontology._label(m.get("target", "").rsplit(".", 1)[-1])  # the target, in business words
    ours = {"id": f"{m['id'][:51]}_probability", "expression": f"AVG({storage._q(table_of(m['id']))}.probability)"}
    chance = ours | {"name": m["name"], "description": f"How likely {said} is {pos}, from 0 to 1."} if "probability" in preds else None
    # the measure with that ID is this model's when it reads this model's probability column: a person's own expression,
    # name or description over it stays (only a description as scoring writes it follows a new `positive`)
    made = lambda x: x["id"] == ours["id"] and re.search(  # noqa: E731  the table's name exactly, quoted or not
        r'(?<![\w"])"?' + re.escape(table_of(m["id"])) + r'"?\.probability\b', x.get("expression") or "") is not None
    have = next((x for x in semantic_model._read(project, "measures.json").get("measures", []) if x["id"] == ours["id"]), None)
    if chance and have and not made(have):  # before anything is written
        raise ActionFailed(409, f"model/measures.json: measure '{chance['id']}' exists and isn't this model's probability, which takes that"
                                f" ID. Give that measure another ID (set_measure, delete_measure), or this model another `id` (ml/{m['id']}.json).")
    if chance and have:
        own = (have.get("description") or "").startswith("How likely ") and have["description"].endswith(", from 0 to 1.")
        chance = have | ({"description": chance["description"]} if own else {})
    if obj := _object(project, m):
        x, kc = obj
        if kc not in preds:
            raise ActionFailed(400, f"ML model '{m['id']}' was trained before its dataset was grouped by {kc}; train it again (train_model).")
        keep = [v is not None for v in preds[kc]]  # a row of no object (an order without a customer) is nobody's prediction
        preds = {c: [v for v, k in zip(vals, keep) if k] for c, vals in preds.items()}
        ids = list(zip(preds[kc], preds["period"])) if "period" in preds else list(preds[kc])
        if (n := len(ids)) > len(set(ids)):
            raise ActionFailed(400, f"Dataset '{m['dataset']}' has several rows for one {x['name']}, so it would get several predictions."
                                    f" Keep {kc} and columns of table '{x['table']}' as its only dimensions"
                                    + (" besides the date columns." if "period" in preds else "."))
    if not n:
        raise ActionFailed(400, f"Dataset '{m['dataset']}' has no rows to score.")
    t, kind = table_of(m["id"]), "VARCHAR" if m["task"] == "classification" else "DOUBLE"
    name = {c: c.replace(".", "_", 1) for c in preds}  # a dataset dimension table.column → column table_column
    sql = ", ".join(f"CAST(c{i} AS {types[c.split('.', 1)[0]][c.split('.', 1)[1]] if '.' in c else 'DATE' if c == 'period' else 'DOUBLE' if c == 'probability' else unsupervised.OUT.get(c, kind)})"
                    f" AS {storage._q(name[c])}" for i, c in enumerate(preds))
    cols = {f"c{i}": np.array([None if v is None else str(v) for v in vals], dtype=object) for i, vals in enumerate(preds.values())}

    def write(cur):
        cur.register("__abi_predictions", cols)
        cur.execute(f"CREATE OR REPLACE TABLE lake.model.{storage._q(t)} AS {storage._ordered(f'SELECT {sql} FROM __abi_predictions')}")

    snap = storage._commit(storage.lake(project), write, user.name, f"score ML model {m['id']}")
    keys = {tb: i["key"] for tb, i in mj["tables"].items()}
    was = None if (old := mj["tables"].get(t)) is None else (old.get("key"), set(types.get(t, {})))  # M9: its key and columns before
    old = mj["tables"].get(t, {})  # a key set on the table (e.g. to relate a fact to cluster labels) survives rescoring
    kept = {k: old[k] for k in ("kind", "key")} if old.get("key") and set(old["key"]) <= set(name.values()) else {}
    mj["tables"][t] = {"kind": "fact", "key": None,
                       "description": f"Predictions of ML model '{m['name']}' ({m['task']}), written by score_model."} | kept
    if obj:  # one row per object: its key is the table's (with the period, a forecast's), so the table is one-to-one with the type's
        mj["tables"][t]["key"] = [name[kc], *(["period"] if "period" in preds else [])]
    rels = [semantic_model._rel(f"{t}.{name[c]}", c) for c in preds if "." in c and keys.get(c.split(".", 1)[0]) == [c.split(".", 1)[1]]]
    rels += [semantic_model._rel(f"{t}.period", f"{dt['table']}.{dt['column']}")] if "period" in preds and dt else []
    mj["relationships"] = _in_place(mj["relationships"], rels, lambda r: r["from"]["table"] == t)
    semantic_model.save(project, mj)
    if m["task"] in unsupervised.TASKS:
        new, summary = unsupervised.measures(m, t), unsupervised.scored(project, m, preds, t)
    else:
        mid, noun = measure_of(m["id"]), {"forecast": "forecast", "regression": "prediction"}.get(m["task"], "predictions")
        named = m["name"].lower().endswith(("forecast", "prediction", "predictions"))  # "Views forecast", not "... forecast forecast"
        new = [{"id": mid, "name": m["name"] if named else f"{m['name']} {noun}", "expression": f"{'COUNT' if kind == 'VARCHAR' else 'SUM'}({storage._q(t)}.prediction)",
                "description": f"{'Number of predicted rows' if kind == 'VARCHAR' else 'Predicted ' + m['target']} of ML model '{m['id']}'."}]
        summary = f"Wrote {n:,} predictions to table {t} (measure {mid})."
        new += [chance] if chance else []  # what a per-object alert compares: "above 0.8"
    # the model's own probability measure goes when this scoring made no probability (its column is gone)
    ms = _in_place(semantic_model._read(project, "measures.json").get("measures", []), new,
                   lambda x: not chance and made(x))
    semantic_model._write(project, "measures.json", {"measures": ms})
    if "period" in preds:
        semantic_model.extend_calendar(project, t, user.name)  # IN-5: the date table covers the future periods
    if obj and was is not None:  # 1.2.7 M0: put on this type (again) since its last scoring: the property comes (back), also where
        # the table kept its key meanwhile (another type shows it), which read as "a person removed the property"
        with db.connect() as conn:
            last = conn.execute("SELECT spec->>'object' AS object FROM ml_runs WHERE project = %s AND model = %s AND kind = 'score' AND status = 'ok'"
                                " ORDER BY created DESC LIMIT 1", [project, m["id"]]).fetchone()
        was = was if last and last["object"] == x["id"] else None
    _on_object(project, m, obj and x["id"], t, preds, pos and f"How likely {said} is {pos}, from 0 to 1.", said, was)
    _post_drift(project, m, drift["drifted"])
    flows.after_refresh(project, t, snap)  # 1.2.6 M9: flows that trigger on ml_<id>; nothing inside a flow (D-23)
    return {"commit": str(git.repo(project).head.target), "snapshot": snap, "method": json.loads(fitted)["method"], "drift": drift,
            "summary": f"{summary} Training run: {trained}."}


def positive(m: dict, classes: list | None) -> str | None:
    """1.2.6 M9: the outcome a two-class model's probability is for: the model's `positive`; without it, true or 1 of a
    yes/no target; None when nobody chose (no probability is made: it could be the wrong outcome's)."""
    if not classes or len(classes) != 2:
        return None
    if "positive" in m:
        want = str(m["positive"]).lower()
        if (hit := next((c for c in classes if c.lower() == want), None)) is None:
            raise ActionFailed(400, f"ml/{m['id']}.json $.positive: '{m['positive']}' is not an outcome of {m['target']}."
                                    f" Set positive to {json.dumps(classes[0])} or {json.dumps(classes[1])} (save_ml_model).")
        return hit
    return next((c for c in classes if _yes_no(c)), None) if {_yes_no(c) for c in classes} == {True, False} else None


def _yes_no(c: str) -> bool | None:
    """A class label as yes or no: true/false, or the numbers 1 and 0 however they are written (1, 1.0); None: neither."""
    try:
        return {0.0: False, 1.0: True}.get(float(c))
    except ValueError:
        return {"true": True, "false": False}.get(c.lower())


def _object(project: str, m: dict) -> tuple[dict, str] | None:
    """1.2.6 M9: (the object type ML model `m` predicts for, its key as table.column); None without `object`."""
    if not m.get("object"):
        return None
    got = ontology.keyed(ontology.read(project), semantic_model._read(project, "model.json"), m["object"])
    if got is None or len(got[1]) != 1:
        raise ActionFailed(409, f"ML model '{m['id']}' predicts for object type '{m['object']}', which is gone or no longer has one"
                                f" key column; change `object` in ml/{m['id']}.json (save_ml_model), then train it again.")
    return got[0], got[1][0]


def _on_object(project: str, m: dict, oid: str | None, t: str, preds: dict, chance: str | None, said: str, was: tuple | None) -> None:
    """1.2.6 M9: the prediction as a property of object type `oid`, under the model's name: the probability (`chance`:
    what it says, for the outcome `positive` chose), else the predicted class; a regression's number; a clustering's
    group; an anomaly's flag and score. A forecast has a row per period, so no property: its measure joins the type's
    measures (the total ahead, per object). A property is added when its column is new, or the table was not keyed
    like this before (`was`: its key and columns before this scoring; None: no table), or it replaces one this
    scoring dropped: so one a person removed stays away, and one they renamed keeps its name. Where table `t` is no longer one-to-one with a type (the dataset was
    regrouped), the type's properties on it go in the same change. The result is checked like a saved ontology."""
    o, mj = ontology.read(project), semantic_model._read(project, "model.json")
    key = next((e["key"] for e in mj.get("entities", []) if e["table"] == t), None)
    change = False
    cols = semantic_model.lake_columns(project).get(t, {})
    for x in o.get("objects", []):
        # dangling: no longer one row per object of this type, or a column this scoring no longer writes (a probability
        # once `positive` is gone or a third outcome came). And one property per classification: the probability, once
        # there is one, takes the place of the predicted outcome (a model first scored without `positive`)
        keep = [p for p in x.get("properties", []) if (pt := p["column"].rpartition("."))[0] != t or ontology.one_to_one(mj, t, x["table"])
                and pt[2] in cols and not (chance and x["id"] == oid and pt[2] == "prediction")]
        if len(keep) != len(x.get("properties", [])):
            x["properties"], change = keep, True
    if oid:
        x = next(x for x in o["objects"] if x["id"] == oid)
        one = x["name"].lower()
        # ponytail: a regression is a quantity unless its target is a money property of the type; the person sets another meaning
        money = next((p.get("meaning") for p in x.get("properties", []) if f"{x['table']}.{p['column']}" == m.get("target")), None) == "money"
        by = f"Written when the ML model {m['name']} is scored."
        props = {"classification": [("probability", "probability", f"{chance} {by}")] if chance else [("prediction", "category", f"The predicted {said}. {by}")],
                 "regression": [("prediction", "money" if money else "quantity", f"The predicted {said}. {by}")],
                 "clustering": [("cluster", "category", f"The group of similar {x['plural'].lower()} this {one} is in. {by}")],
                 "anomaly": [("anomaly", "flag", f"Whether this {one} is unusual. {by}"),
                             ("score", "quantity", f"How unusual this {one} is: higher is more unusual. {by}")],
                 "forecast": []}[m["task"]]
        fresh = was is None or was[0] != key
        have, taken = {p["column"] for p in x.get("properties", [])}, _taken(project, x)
        for c, mean, d in props:
            if f"{t}.{c}" in have or not (fresh or c not in was[1] or change):  # `change`: it takes the place of one just dropped
                continue
            name = _free((m["name"] + (" score" if c == "score" else ""))[:180], taken)
            x["properties"] = x.get("properties", []) + [{"column": f"{t}.{c}", "name": name, "meaning": mean, "description": d[:2000]}]
            taken, change = taken | {name.lower()}, True
        if m["task"] == "forecast" and fresh and measure_of(m["id"]) not in x.get("measures", []):
            x["measures"], change = x.get("measures", []) + [measure_of(m["id"])], True
    if change:
        _put_ontology(project, o, f"ml/{m['id']}.json")


def _taken(project: str, x: dict) -> set[str]:
    """The names object type `x` shows its properties under (lower-cased): listed names, else the columns' words."""
    return {(p.get("name") or ontology._label(p["column"].rpartition(".")[2])).lower() for p in x.get("properties", [])} | {
        ontology._label(c).lower() for c in semantic_model.lake_columns(project).get(x["table"], {})}


def _free(base: str, taken: set[str]) -> str:
    """`base`, or its first variant no property of the type is named: never two properties of one name."""
    return next(n for n in (base, f"{base} (predicted)", *(f"{base} (predicted {i})" for i in range(2, 99))) if n.lower() not in taken)


def _put_ontology(project: str, o: dict, rel: str, off: bool = False) -> None:
    """Writes the ontology a scoring or a model save changed, checked as save_ontology checks it: a reference that no
    longer holds because of the change fails it (file, field, fix), before anything of the ontology is written."""
    files = bundle(git.path(project))
    before = {e["error"] for e in ontology.check(project, files)}
    o = {"$schema": SCHEMA + "ontology.json", **{k: v for k, v in o.items() if k != "$schema"}}
    if errs := [e for e in ontology.check(project, files | {ontology.FILE: o}) if e["error"] not in before]:
        raise ActionFailed(422, f"{rel}: " + ("the model can't be taken off its object type, because the ontology would no longer hold: " if off
                                              else "its prediction can't be shown on its object type: ")
                                + " ".join(f"{e['file']} {e['path']}: {e['error']} {e['fix']}" for e in errs)
                                + (" Change that in the ontology first (save_ontology), then do this again." if off else ""), errors=errs)
    semantic_model._write(project, "ontology.json", o)


def _off_object(project: str, oid: str, mid: str, to: str | None = None) -> None:
    """1.2.6 M9: a model taken off object type `oid` (its `object` removed or changed, or the model deleted) takes its
    properties and its forecast measure off the type, in the same change. They move to type `to`, the model's new one,
    when it is a type on the same table (the predictions table is one-to-one with both; a later scoring adds nothing),
    under names that type doesn't use yet. Otherwise, once no type shows a property of the table, the table loses its
    key in model.json (its relationship, so its row security, stays): a model put on a type again, or made again under
    the same ID, then starts afresh and gets its property at its next scoring."""
    o, t = ontology.read(project), table_of(mid)
    src, dst = (next((x for x in o.get("objects", []) if x["id"] == i), None) for i in (oid, to))
    props = [p for p in (src or {}).get("properties", []) if p["column"].rpartition(".")[0] == t]
    ms = [i for i in (src or {}).get("measures", []) if i == measure_of(mid)]
    moved = src is not None and dst is not None and dst["table"] == src["table"]
    if props or ms:
        src |= {k: [i for i in src[k] if i not in gone] for k, gone in (("properties", props), ("measures", ms)) if k in src}
        if moved:
            have, taken = {p["column"] for p in dst.get("properties", [])}, _taken(project, dst)
            for p in props:
                if p["column"] not in have:
                    p = p | {"name": _free(p.get("name") or ontology._label(p["column"].rpartition(".")[2]), taken)}
                    dst["properties"], taken = dst.get("properties", []) + [p], taken | {p["name"].lower()}
            if new := [i for i in ms if i not in dst.get("measures", [])]:
                dst["measures"] = dst.get("measures", []) + new
        _put_ontology(project, o, f"ml/{mid}.json", True)
    if not moved and not any(p["column"].rpartition(".")[0] == t for x in o.get("objects", []) for p in x.get("properties", [])):
        mj = semantic_model.load(project)
        if (mj["tables"].get(t) or {}).get("key"):
            mj["tables"][t]["key"] = None
            with contextlib.suppress(ActionFailed):  # an object type or a link still needs that key: it stays
                ontology.guard(project, semantic_model._file(mj), f"Table '{t}'")
                semantic_model.save(project, mj)


def after_refresh(project: str, table: str, snapshot: int) -> None:
    """ML-6: a refresh snapshot of `table` queues one scoring run (key snapshot-<n>) for each model whose dataset reads
    it (lineage) and whose pinned run, else latest finished training, succeeded, as the user who trained it (X-01.4 C-9). Not inside a
    flow (D-23, C-8: the flow scores with a `score` step). Never fails the refresh (D-26): errors go to log and feed."""
    if actions.VIA.get() == "flow" or flows.IN_FLOW.get():
        return
    problems = []
    try:
        files = bundle(git.path(project))
        down = lineage.reach(lineage.graph(files), {f"table:{table}"}, "downstream")
        for mid in (n.split(":", 1)[1] for n in down if n.startswith("ml:")):
            with db.connect() as conn:
                t = conn.execute("SELECT id::text, query, status, user_id FROM ml_runs WHERE project = %s AND model = %s"
                                 " AND kind = 'train' AND status IN ('ok', 'failed') ORDER BY pinned IS NULL, created DESC LIMIT 1",
                                 [project, mid]).fetchone()
                if t is None or t["status"] != "ok":
                    continue
                user, logged = security.user(conn, t["user_id"]), {"project": project, "model": mid, "key": f"snapshot-{snapshot}"}
                if user is None or (role_in(conn, user, project) or Role.viewer) < Role.editor:
                    problems.append(f"'{mid}' isn't scored: the user who trained it is no longer an Editor here; train it again.")
                    actions.audit(conn, user, "worker", "score_model", project, logged, 403, "trainer is no longer an Editor")  # SC-6
                    continue
                m = files[f"ml/{mid}.json"]
                if out := _launch(conn, user, project, m, "score", t["query"] or _query(project, m), t["id"], None, logged["key"]):
                    actions.audit(conn, user, "worker", "score_model", project, logged, 200, f"run {out['run']} after refreshing {table}")  # SC-6
    except Exception as e:  # noqa: BLE001 - the refresh worked (D-26); no raw text (SC-4)
        logging.getLogger(__name__).error("Scoring after refreshing %s/%s failed: %s", project, table, credentials.log_text(e))
        problems.append(f"not queued ({type(e).__name__}).")
    notify_change("ml", project, f"Scoring ML models after refreshing '{table}': ", " ".join(problems) or None, "works again.", table)


class ModelRef(storage.ProjectRef):
    model: str = Field(pattern=ID, description="ML model ID (file name in ml/)")


class SaveModel(storage.ProjectRef):
    ml: dict = Field(description=(
        "The ML model (schemas/v1/ml.json): {id, name, description?, task: classification|regression|forecast|clustering|anomaly, dataset:"
        " a dataset ID, target: a measure ID or table.column of the dataset's query (none for clustering and anomaly), features?: its other columns to"
        " learn from (default all; clustering and anomaly: all but keys and date columns; not for forecast: it forecasts each combination of the query's other dimensions), time?: a date-table dimension of the query (required for forecast and seasonal anomaly; makes the"
        " holdout the latest periods), horizon? (forecast), k? (clustering: fixed groups 2..10, default auto), method?: auto|lightgbm|random_forest"
        " (auto, the default, trains both, keeps the better on a validation part of the training data and reports accuracy on a separate holdout; forecast: auto|lightgbm|ets; clustering: auto|kmeans|gmm, kept by BIC among clearly apart groups;"
        " anomaly: auto|isolation_forest|seasonal, seasonal needs time and is kept when the data's season is real),"
        " search? (forecast, classification, regression: tune each candidate first; {strategy: grid|random, trials?: 2..50 per"
        " candidate incl. its defaults, time_budget_s?: 10..1800, seed?}), space? (needs search; per candidate the settings to"
        " try, each a list of values or {min, max, log?}: lightgbm num_leaves, learning_rate, n_estimators, min_child_samples,"
        " reg_lambda; random_forest n_estimators, max_depth, min_samples_leaf, max_features; ets model, damped; a candidate not"
        " named searches its default space), params? ({<method>: {setting: value}}: a fixed setting of a non-auto method, no"
        " search; use_ml_trial writes it from a trial), object? (an object type ID: one prediction per object; the dataset is grouped by the type's key), positive?"
        " (with object, classification: the outcome the probability is for)}."))


@action(Role.editor, definition=True)
def save_ml_model(conn, user: User, args: SaveModel) -> dict:
    """Creates or replaces an ML model definition (ml/<id>.json). Checked like an import (schema; the dataset exists; target, features and time are columns of its query) and rejected with file, JSON path and fix if invalid. A `note` in the answer names a missing `positive`: ask the person which outcome. A forecast follows the question: time is the grain it asks for (calendar.day, .week, .month, .quarter or .year; by week the dataset needs calendar.week_year and calendar.week, by month or quarter calendar.year too; weekly or monthly when asked), and horizon covers the asked period in full, counted from the period after the data's last one: "next quarter" runs to that quarter's end (by day 90-92, by week 13, by month 3, by quarter 1), "next month" by day 28-31, "next year" by month 12. Next: train_model."""
    m = {**args.ml, "$schema": SCHEMA + "ml.json"}
    rel = f"ml/{imports.object_id(m, 'An ML model')}.json"
    files = bundle(git.path(args.project))
    imports.check(conn, args.project, files | {rel: m})
    note = _outcomes(args.project, m, user.id)
    imports.write(args.project, {rel: m})
    if (was := (files.get(rel) or {}).get("object")) and was != m.get("object"):  # taken off its object type: its property goes too
        _off_object(args.project, was, m["id"], m.get("object"))
    if m.get("object") and was != m["object"]:  # 1.2.7 M0: put on a type (again): its earlier scorings were not for it, so the
        # next one adds its property (`_predictions`), also where the predictions table kept its key meanwhile
        conn.execute("UPDATE ml_runs SET spec = spec - 'object' WHERE project = %s AND model = %s AND kind = 'score' AND status = 'ok'",
                     [args.project, m["id"]])
    return {"id": m["id"]} | ({"note": note} if note else {})


def _outcomes(project: str, m: dict, user_id: int) -> str | None:
    """1.2.6 M9, saving a classification on an object type whose target has two outcomes in the dataset's rows (read
    as the caller): a `positive` that is neither is refused (400: file, field, fix); without `positive`, and unless
    the target is yes/no, the note saying which two to choose from. None otherwise, and before there is data."""
    q = _dataset(project, m)["query"]
    if not (m.get("object") and m["task"] == "classification"):
        return None
    try:
        if m["target"] in q.get("dimensions", []):
            got = semantic.query(project, {k: q[k] for k in ("measures", "filters", "links") if k in q} | {"dimensions": [m["target"]], "limit": 4}, user_id)
        else:  # ponytail: a measure target's outcomes are read from the dataset's first 10,000 rows; training reads them all
            got = semantic.query(project, q | {"totals": [], "limit": 10_000}, user_id)
    except ActionFailed:
        return None
    at = got["columns"].index(m["target"])
    vals = sorted({str(r[at]) for r in got["rows"] if r[at] is not None})
    if len(vals) != 2 or positive(m, vals):
        return None
    return (f"ml/{m['id']}.json $.positive: {m['target']} has the outcomes {json.dumps(vals[0])} and {json.dumps(vals[1])}, and the model doesn't say"
            f" which one its probability is for. Scoring will write the predicted outcome only. For a probability on each object, add"
            f" \"positive\": {json.dumps(vals[0])} or \"positive\": {json.dumps(vals[1])} (save_ml_model).")


@action(Role.editor, definition=True, confirms=True)
def delete_ml_model(conn, user: User, args: ModelRef) -> dict:
    """Deletes an ML model definition. Its training runs stay in the registry. A model on an object type takes its property off the type."""
    m = imports.load(args.project, "ML model", args.model)
    imports.remove(args.project, "ML model", args.model)
    if m.get("object"):
        _off_object(args.project, m["object"], m["id"])
    return {}


@action(Role.editor)
def train_model(conn, user: User, args: ModelRef) -> dict:
    """Trains an ML model on its dataset's current rows (read through the semantic model, with your permissions), in the worker: returns the registry run ID; follow it with ml_models. With method auto it trains the task's two candidates (forecast: LightGBM and ETS; classification and regression: LightGBM and random forest; clustering: k-means and Gaussian mixture; anomaly: Isolation Forest and, with a time column, the seasonal rule) and keeps the better on a validation part of the training data, then reports accuracy on a held-out part never used for choosing (the summary names both); both candidates' metrics stay on the run. Each run reports its accuracy against a simple baseline, the features left out as leakage suspects and the strongest drivers. A forecast runs at the model's time grain for its horizon: to answer another period or grain, change them with save_ml_model first."""
    m = imports.load(args.project, "ML model", args.model)
    if fix := RETIRED.get(m.get("method")):  # D-99: a file saved before the method left the schema
        raise ActionFailed(422, f"Method '{m['method']}' is no longer trained (D-99). Set method to {fix} in ml/{m['id']}.json"
                                " (save_ml_model), then train again; runs trained with it keep scoring until then.")
    q = _query(args.project, m)
    return _launch(conn, user, args.project, m, "train", q, None, lambda: _train(args.project, m, q, user.id))


@action(Role.editor, definition=True)
def score_model(conn, user: User, args: ModelRef) -> dict:
    """Scores an ML model with its latest successful training run on its dataset's current rows (read with your permissions), in the worker: returns the registry run ID; follow it with ml_models. The predictions become model table ml_<id> (a forecast: the next periods per series) with measure <id>_prediction, so reports, tools and flows query them like any data; a classification's predicted class is the dimension column ml_<id>.prediction. With `object`, the prediction becomes a property of the object type, seen only with its object."""
    m = imports.load(args.project, "ML model", args.model)
    t = _trained(conn, args.project, m["id"])
    q = t["query"] or _query(args.project, m)  # an X-01.1-era run kept no query
    return _launch(conn, user, args.project, m, "score", q, t["run"], lambda: _predictions(args.project, m, t["run"], q, user))


class Prefill(ModelRef):
    table: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,62}$", description="the input table to pre-fill; created if missing")


def _value(v):
    """A query value as an edit_cells value (JSON)."""
    return v if v is None or isinstance(v, (str, int, float, bool)) else float(v) if isinstance(v, Decimal) else str(v)


PREFILLED = "Pre-filled from the forecast of ML model '{}'."  # the pre-fill batch's edit comment


def prefill_gaps(project: str) -> list[str]:
    """D-47: tables without a rule holding an applied pre-fill from a forecast that rules filter (now or since)."""
    if not semantic.rls.rules(project):
        return []
    done = semantic.rls.filtered(project)
    filtered = done | set(semantic.rls.unfiltered_outputs(project))
    hot = [PREFILLED.format(p.stem) for p in (git.path(project) / "ml").glob("*.json") if table_of(p.stem) in filtered]
    if not hot:
        return []
    return [t for t in sorted(writeback._logs(storage.lake(project).cursor()) - done) if semantic.execute(
        project, f"SELECT 1 FROM lake.edits.{storage._q(t)} WHERE status = 'active' AND comment IN"
                 f" ({', '.join(f"'{storage._lit(c)}'" for c in hot)}) LIMIT 1", {"readiness": t})]


@action(Role.editor)
def prefill_input_table(conn, user: User, args: Prefill) -> dict:
    """Pre-fills an input table with a scored forecast (WB-2, ML-5): creates the table if missing (its key: the forecast's series columns and period; column `forecast`), then writes every forecast value as one edit batch through edit_cells, so it is in the table's edit history and can be undone. From the Main Agent the batch is a proposal the user approves."""
    m = imports.load(args.project, "ML model", args.model)
    if m["task"] != "forecast":
        raise ActionFailed(400, f"Only a forecast pre-fills an input table; '{m['id']}' is a {m['task']} model.")
    src, cols = table_of(m["id"]), semantic_model.lake_columns(args.project)
    if src not in cols:
        raise ActionFailed(400, f"ML model '{m['id']}' has no forecast yet; score it first (score_model).")
    key, t = [c for c in cols[src] if c != "prediction"], args.table
    # SE-6: the batch holds forecasts of every row (an Editor reads all); the input table needs its own rule first.
    # A rule added to the source later makes it a readiness gap (`prefill_gaps`, D-47).
    if t not in (done := semantic.rls.filtered(args.project)) and (src in done or src in semantic.rls.unfiltered_outputs(args.project)):
        raise ActionFailed(409, f"Row-level security rules filter the rows of forecast {src}, and table '{t}' has no rule, so"
                                f" every Viewer would see every forecast; add a rule on '{t}' (set_rls_rule) first.")
    rows = semantic.query(args.project, {"measures": [measure_of(m["id"])], "dimensions": [f"{src}.{c}" for c in key],
                                         "limit": ROWS}, user.id)["rows"]

    def nested(name: str, payload: dict) -> dict:  # its own audited action call, as this caller (SC-6, WB-9)
        return contextvars.copy_context().run(actions.run, name, {"project": args.project, "table": t, **payload}, user, actions.VIA.get())

    have = set()
    if created := t not in cols:
        nested("create_input_table", {"columns": [{"name": c, "type": cols[src][c]} for c in key] + [{"name": "forecast", "type": "DOUBLE"}],
                                      "key": key, "description": f"Forecast of {m['name']}, pre-filled by ML model '{m['id']}'."})
    elif semantic_model.load(args.project)["tables"].get(t, {}).get("key") != key or "forecast" not in cols[t]:
        raise ActionFailed(409, f"Table '{t}' exists but is not keyed by {', '.join(key)} with a column forecast; use another table name.")
    else:
        have = set(semantic.execute(args.project, f"SELECT {', '.join(f'{storage._q(c)}::VARCHAR' for c in key)} FROM {storage._q(t)}",
                                    {"prefill": t}, user.id))
    edits, add = [], []
    for r in rows:
        k, v = [_value(x) for x in r[:-1]], _value(r[-1])
        if tuple(None if x is None else str(x) for x in r[:-1]) in have:
            edits.append({"key": k, "column": "forecast", "value": v})
        else:
            add.append(dict(zip(key, k)) | {"forecast": v})
    out = nested("edit_cells", {"edits": edits, "add": add, "comment": PREFILLED.format(m["id"])})
    return {"table": t, "created": created, "written": len(rows), **{k: out[k] for k in ("batch", "snapshot", "status")},
            "columns": ["batch", "status", "written"], "rows": [[out["batch"], out["status"], len(rows)]]}  # D-22; D-114: no table name


@action(Role.viewer)
def ml_models(conn, user: User, args: storage.ProjectRef) -> dict:
    """The project's ML models: each definition with its latest training run (status, accuracy metrics vs the baseline, better_than_baseline, features left out as leakage suspects, feature importance by business name, and a plain summary) and its latest scoring run (status, the training run it used, summary, drift), and active_run: the training run scoring uses and why (pinned, or the latest successful). `rows` hold each run's figures (one row per run: kind, run ID, {figure: value}; the active run's has active true): quote numbers only from them; for an agent the runs under `models` keep only their texts and labels."""
    ms = imports.objects(args.project, "ml")
    last = latest(conn, args.project)
    if semantic.rls.restricted(args.project, user.id):  # SE-6: metrics, drift and errors come from every row
        last = {k: hidden(r) for k, r in last.items()}
    runs = [[last.get((m["id"], k)) for k in ("train", "score")] for m in ms]
    active = {r["model"]: _active(r) for r in conn.execute(ACTIVE.format(""), [args.project])}
    rows = {}  # AGENT-CONTEXT-fix-5: one row per run (SC-5 cuts lists at agent.SAMPLE: 6 models' long rows were cut)
    for kind, run, f, v in [row for r in runs for x in r for row in figures(x)] + _actives(active):
        rows.setdefault((kind, run), {}).setdefault(f, []).append(v)
    for x in (x for r in runs for x in r if x and x["groups"]):  # labels are keys now, so the cluster numbers ground here
        rows[(x["kind"], x["run"])]["clusters"] = [g["cluster"] for g in x["groups"]]
    if actions.by_agent():  # the figures once, in rows: runs keep their texts and labels, not the figures again
        runs = [[x and {k: v for k, v in x.items() if k not in AGENT_DROP} for x in r] for r in runs]
        ms = [{k: v for k, v in m.items() if k != "$schema"} for m in ms]
        active = {m: a and {"why": a["why"]} for m, a in active.items()}  # its run is the row with figure `active`
    return {"models": [{**m, "latest_run": t, "latest_score": s, "active_run": active.get(m["id"])} for m, (t, s) in zip(ms, runs)],
            "columns": ["kind", "run", "figures"],
            "rows": [[kind, run, {f: v[0] if len(v) == 1 else v for f, v in d.items()}] for (kind, run), d in rows.items()]}


# figures already in ml_models' rows, and internals (commit, snapshot, query, account), left out of the agent's `models`
AGENT_DROP = {"metrics", "baseline", "better_than_baseline", "candidates", "holdout", "created", "finished", "commit",
              "snapshot", "query", "user_id", "model"}


PINNED = "Pinned with pin_ml_run: scoring uses this run until it is unpinned; later training runs are kept but don't take over."
LATEST = "The latest successful training run (no run is pinned)."


def _active(r: dict) -> dict:
    """ML-4 (D-99): the run scoring uses and why (an ACTIVE row)."""
    return {"run": r["run"], "pinned": r["pinned"], "why": PINNED if r["pinned"] else LATEST}


def _actives(active: dict) -> list[list]:
    """{model: _active} as FIGURES rows (D-22, GATE-9-fix-8): the run ID and pin time are quotable."""
    return [["train", a["run"], f, v] for a in active.values() if a for f, v in (("active", True), ("pinned", a["pinned"]))
            if v is not None]


HISTORY = 50  # ponytail: newest runs only, bounds the agent's result; page with an offset if older runs are asked for


def _changes(now: list[dict], before: list[dict]) -> list[dict]:
    """ML-4: each feature's importance share now vs the previous run, largest change first."""
    a, b = {x["feature"]: x for x in now}, {x["feature"]: x for x in before}
    out = [{"feature": f, "name": (a.get(f) or b[f])["name"], "share": a[f]["share"] if f in a else 0.0,
            "previous": b[f]["share"] if f in b else 0.0} for f in a | b]
    return sorted((x | {"change": round(x["share"] - x["previous"], 4)} for x in out), key=lambda x: -abs(x["change"]))


class Pin(ModelRef):
    run: str | None = Field(description="the training run ID to pin (from ml_history or ml_models); null unpins")


@action(Role.editor)
def pin_ml_run(conn, user: User, args: Pin) -> dict:
    """Pins a successful training run of an ML model, so scoring (score_model, scoring after a refresh, flow score steps) uses it until unpinned, even when a later retrain is newer; run null unpins (scoring goes back to the latest successful run). Use it to keep a good model while a retrain is worse."""
    m = imports.load(args.project, "ML model", args.model)["id"]
    if args.run is not None and not conn.execute(
            "SELECT 1 FROM ml_runs WHERE project = %s AND model = %s AND id::text = %s AND kind = 'train' AND status = 'ok'",
            [args.project, m, args.run]).fetchone():
        raise ActionFailed(400, f"'{args.run}' is not a successful training run of ML model '{m}'; pick one from ml_history.")
    conn.execute("UPDATE ml_runs SET pinned = NULL WHERE project = %s AND model = %s AND pinned IS NOT NULL", [args.project, m])
    conn.execute("UPDATE ml_runs SET pinned = now() WHERE id::text = %s", [args.run])
    r = conn.execute(ACTIVE.format("AND model = %s"), [args.project, m]).fetchone()
    return {"model": m, "active_run": r and _active(r), "columns": FIGURES, "rows": _actives({m: r and _active(r)})}


@action(Role.viewer)
def ml_history(conn, user: User, args: ModelRef) -> dict:
    """An ML model's training runs, newest first (at most 50): status, method, held-out metrics vs the baseline, both candidates' metrics, which run scoring uses (active), and each feature's importance change against the previous successful run. `rows` repeat those figures per run: quote numbers only from them."""
    runs = conn.execute(f"SELECT {RESULT}, pinned FROM ml_runs WHERE project = %s AND model = %s AND kind = 'train'"
                        " ORDER BY created DESC LIMIT %s", [args.project, args.model, HISTORY]).fetchall()
    if semantic.rls.restricted(args.project, user.id):  # SE-6: metrics and importance come from every row
        runs = [hidden(r) for r in runs]
    active = (conn.execute(ACTIVE.format("AND model = %s"), [args.project, args.model]).fetchone() or {}).get("run")
    before = None
    for r in reversed(runs):
        r["active"] = r["run"] == active
        r["importance_change"] = None if r["importance"] is None or before is None else _changes(r["importance"], before)
        before = r["importance"] if r["status"] == "ok" and r["importance"] is not None else before
    rows = []
    for r in runs:
        rows += [[r["run"], f, v] for f, v in (("method", r["method"]), ("active", r["active"]))]
        rows += [[r["run"], f, v] for *_, f, v in figures(r)]
        rows += [[r["run"], "importance change", x["change"]] for x in r["importance_change"] or []]
    return {"model": args.model, "runs": runs, "columns": ["run", "figure", "value"], "rows": [x for x in rows if x[2] is not None]}


class TrialsOf(ModelRef):
    run: str | None = Field(None, description="a training run ID (ml_history, ml_models); default: the run scoring uses, else the latest")


@action(Role.viewer)
def ml_trials(conn, user: User, args: TrialsOf) -> dict:
    """An ML model training run's hyperparameter search (search and space in its file): every trial with its candidate, settings ({} = the candidate's defaults), cross-validation score on folds of the training part and its spread, seconds, status (ok; failed; skipped: past the time budget) and chosen (the setting its candidate kept); method is the candidate the run kept. Default run: the one scoring uses. `rows` hold the figures under fixed labels (trial <n> <candidate> ...): explain the winner and quote numbers only from them; settings the model file lists itself stay out of rows. use_ml_trial makes one trial the model's fixed setting."""
    r = conn.execute("SELECT id::text AS run, method FROM ml_runs WHERE project = %s AND model = %s AND kind = 'train'"
                     " AND (%s::text IS NULL OR id::text = %s) ORDER BY pinned IS NULL, status <> 'ok', created DESC LIMIT 1",
                     [args.project, args.model, args.run, args.run]).fetchone()
    if r is None:
        raise ActionFailed(404, f"ML model '{args.model}' has no training run{'' if args.run is None else f' {args.run}'};"
                                " train it (train_model) or pick a run from ml_history.")
    hide = semantic.rls.restricted(args.project, user.id)  # SE-6: fold scores come from every row
    ts = trials(conn, r["run"], hide)
    return {"run": r["run"], "method": r["method"], "trials": ts, "columns": FIGURES,
            "rows": [["train", r["run"], "trials", len(ts)], *trial_rows(conn, r["run"], hide)]}


class UseTrial(ModelRef):
    run: str = Field(description="the training run ID (ml_trials)")
    trial: int = Field(ge=1, description="the trial's position in that run (ml_trials: trial <n>)")


@action(Role.editor, definition=True, confirms=True)
def use_ml_trial(conn, user: User, args: UseTrial) -> dict:
    """Makes one scored trial of a training run's search (ml_trials) the ML model's fixed setting, rather than the one the search kept: ml/<id>.json gets method = the trial's candidate and params = its settings and loses search and space, so every later training (train_model, flow train steps) trains exactly that setting without searching. Asks for a confirmation showing the old and new settings. Next: train_model."""
    m = imports.load(args.project, "ML model", args.model)
    t = conn.execute("SELECT t.method, t.params, t.status FROM ml_trials t JOIN ml_runs r ON r.id = t.run WHERE r.project = %s"
                     " AND r.model = %s AND r.id::text = %s AND t.position = %s", [args.project, m["id"], args.run, args.trial]).fetchone()
    if t is None:
        raise ActionFailed(400, f"Training run '{args.run}' of ML model '{m['id']}' has no trial {args.trial}; pick one from ml_trials.")
    if t["status"] != "ok":
        raise ActionFailed(400, f"Trial {args.trial} was {t['status']}, so it has no score to go by; pick a scored trial.")
    new = {k: v for k, v in m.items() if k not in ("search", "space", "params")} | {"method": t["method"]}
    if t["params"]:
        new["params"] = {t["method"]: t["params"]}
    said = ", ".join(f"{k} {v}" for k, v in t["params"].items()) or "its defaults"
    actions.require_confirmation(  # the file can change between the ask and the click: the preview signs both states
        f"ML model '{m['id']}' will train {LABEL[t['method']]} with {said} from now on, without a search"
        + (" (its search and space are removed)." if m.get("search") else "."),
        {"model": m["id"], "before": {k: m.get(k) for k in ("method", "params", "search", "space")},
         "after": {"method": new["method"], "params": new.get("params")}})
    rel = f"ml/{m['id']}.json"
    imports.check(conn, args.project, bundle(git.path(args.project)) | {rel: new})
    imports.write(args.project, {rel: new})
    return {"id": m["id"], "method": t["method"]}

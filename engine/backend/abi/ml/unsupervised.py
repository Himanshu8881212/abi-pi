"""Clustering and anomaly detection: ML-1 (P2 part), work/validation/E-04.md decisions 1-2 and E-04.1.md.

No target: clustering is k-means with auto k by silhouette or a Gaussian mixture with auto k by BIC (k = 1, no grouping,
is the baseline); anomaly detection is an Isolation Forest or a seasonal median + MAD rule, each compared with a
per-feature 3-MAD rule (ML-3). D-99 (ML-CANDIDATES.2): method auto trains both candidates and keeps the better
(`_clustering`: BIC, unless only one candidate's groups are clearly apart; `_anomaly`: the seasonal rule when its season is real, `_seasonal`). Fitted models are JSON
(centroids; mixture weights, means and precision Choleskys; the forest's tree arrays, scored by `_forest_scores`),
never pickled (ML-4).
"""

import json
import warnings
from collections import Counter, defaultdict

import numpy as np

from abi import ml, storage
from abi.actions import ActionFailed
from abi.agent import notify_change
from abi.ml import forecast
from abi.semantic import model as semantic_model

TASKS = ("clustering", "anomaly")
METHODS = {"clustering": ("kmeans", "gmm"), "anomaly": ("isolation_forest", "seasonal")}  # D-99: best first
MAX_K = 10  # auto k: 2..10
SAMPLE = 10_000  # D-17: silhouette rows (its pairwise distances are chunked at 64 MB too)
CLEAR = 0.25  # a best silhouette under this: no clear groups
Z = 3  # robust z above this: an anomaly
SHARE = 0.01  # Isolation Forest: the share of training rows it flags (sklearn contamination)
RULES = {"seasonal": f"more than {Z} robust deviations (1.4826 x MAD) from the median of the same period in earlier seasons",
         "isolation_forest": f"among the {SHARE:.0%} of training rows the Isolation Forest isolates fastest (contamination {SHARE})",
         "auto": "the seasonal rule when the data has a time column and a real season (the same period in earlier seasons predicts"
                 " the measures better than the period before), else the Isolation Forest; decided at training"}
OUT = {"cluster": "INTEGER", "anomaly": "BOOLEAN", "score": "DOUBLE"}  # output columns of ml_<id>


def features(m: dict, columns: list[str], mj: dict) -> list[str]:
    """The model's features; default: every dataset column but row identity (a table's single key, date-table columns)."""
    if m.get("features"):
        return m["features"]
    keyed = {f"{t}.{i['key'][0]}" for t, i in mj["tables"].items() if len(i.get("key") or []) == 1}
    date = (mj.get("date_table") or {}).get("table")
    return [c for c in columns if c not in keyed and c.split(".", 1)[0] != date]


def _floats(vals) -> np.ndarray:
    return np.array([np.nan if v is None else float(v) for v in vals])


def _spread(x: np.ndarray) -> tuple[float, float]:
    """(median, robust scale): 1.4826 x MAD; a MAD of 0 falls back to 1.2533 x mean absolute deviation (modified z,
    Iglewicz-Hoaglin), so a spike in a flat series still stands out. NaN when every value is missing."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # all-missing columns
        med = np.nanmedian(x)
        return med, 1.4826 * np.nanmedian(np.abs(x - med)) or 1.2533 * np.nanmean(np.abs(x - np.nanmean(x)))


def robust_z(x: np.ndarray, center: float | None = None) -> np.ndarray:
    """|x - center| / the robust scale (`_spread`), center default the median. Missing values: 0."""
    med, scale = _spread(x)
    with np.errstate(invalid="ignore"):
        z = np.abs(x - (med if center is None else center)) / scale if scale and not np.isnan(scale) else np.zeros(len(x))
    return np.nan_to_num(z)


def rule(cols: list[str], data: dict) -> np.ndarray:
    """ML-3 baseline: rows where any numeric column is more than Z robust deviations from its median."""
    zs = [robust_z(_floats(data[c])) for c in cols if ml._numeric(data[c])]
    return np.any(np.array(zs) > Z, axis=0) if zs else np.zeros(len(next(iter(data.values()))), dtype=bool)


# --- clustering -----------------------------------------------------------------------------------------------------

def _assign(centers: np.ndarray, X: np.ndarray) -> np.ndarray:
    """Cluster numbers 1..k: the nearest centroid."""
    return ((centers ** 2).sum(1) - 2 * X @ centers.T).argmin(1) + 1  # |x - c|² without |x|²: rows x k, not rows x k x columns


def _silhouette(X: np.ndarray, labels: np.ndarray) -> float:
    import sklearn
    from sklearn.metrics import silhouette_score
    from threadpoolctl import threadpool_limits  # D-17: single-threaded

    with threadpool_limits(1), sklearn.config_context(working_memory=64):
        try:
            return float(silhouette_score(X, labels, sample_size=min(SAMPLE, len(X)), random_state=0))
        except ValueError:  # one cluster only (in the sample)
            return -1.0


def _kmeans(X: np.ndarray, ks) -> tuple[float, np.ndarray, dict]:
    """(best silhouette, its centroids numbered by size, {k: silhouette})."""
    from sklearn.cluster import KMeans
    from threadpoolctl import threadpool_limits  # D-17: single-threaded

    best, scores = None, {}
    with threadpool_limits(1), warnings.catch_warnings():
        warnings.simplefilter("ignore")  # fewer distinct points than k
        for k in ks:
            km = KMeans(k, n_init="auto", random_state=0).fit(X)
            s = _silhouette(X, km.labels_)
            scores[k] = round(s, 4)
            if best is None or s > best[0]:
                best = (s, km.cluster_centers_[np.argsort(-np.bincount(km.labels_, minlength=k), kind="stable")])
    return best[0], best[1], scores


def _bic(X: np.ndarray, w: np.ndarray, means: np.ndarray, covs: np.ndarray, params: float) -> float:
    """BIC of the Gaussian mixture (weights, means, full covariances) on X: -2 log-likelihood + params x log(rows)."""
    L = np.linalg.cholesky(covs)
    logp = np.empty((len(X), len(w)))
    with np.errstate(divide="ignore"):  # an empty k-means group: weight 0
        for j in range(len(w)):
            z = np.linalg.solve(L[j], (X - means[j]).T)
            logp[:, j] = np.log(w[j]) - np.log(np.diag(L[j])).sum() - 0.5 * (z ** 2).sum(0)
    top = logp.max(1)
    ll = (top + np.log(np.exp(logp - top[:, None]).sum(1))).sum() - X.size / 2 * np.log(2 * np.pi)
    return float(-2 * ll + params * np.log(len(X)))


def _kmeans_bic(X: np.ndarray, centers: np.ndarray, labels: np.ndarray) -> float:
    """k-means read as the mixture it assumes: round groups of one shared variance, weights = group shares."""
    k, d = centers.shape
    var = max(float(((X - centers[labels - 1]) ** 2).mean()), 1e-6)
    return _bic(X, np.bincount(labels - 1, minlength=k) / len(X), centers, np.broadcast_to(var * np.eye(d), (k, d, d)), k * d + k)


def _gmm(X: np.ndarray, ks, num: list[int]) -> tuple[dict, dict]:
    """(The Gaussian mixture with the best BIC on the numeric columns `num` (`_bic`), components numbered by size, as
    JSON-able arrays (ML-4); {k: BIC})."""
    from sklearn.mixture import GaussianMixture
    from threadpoolctl import threadpool_limits  # D-17: single-threaded

    best, scores, d = None, {}, len(num)
    with threadpool_limits(1), warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for k in ks:
            g = GaussianMixture(k, random_state=0).fit(X)  # reg_covar keeps duplicate-heavy data well-defined
            b = _bic(X[:, num], g.weights_, g.means_[:, num], g.covariances_[:, num][:, :, num], k * d * (d + 3) / 2 + k - 1)
            scores[k] = round(b, 2)
            if best is None or b < best[0]:
                best = (b, g)
    g = best[1]
    o = np.argsort(-np.bincount(g.predict(X), minlength=g.n_components), kind="stable")
    return {"weights": g.weights_[o].tolist(), "means": g.means_[o].tolist(), "precisions": g.precisions_cholesky_[o].tolist()}, scores


def _mixture(f: dict, X: np.ndarray) -> np.ndarray:
    """Cluster numbers 1..k: the most likely component, computed as sklearn's GaussianMixture.predict does."""
    logp = np.empty((len(X), len(f["weights"])))
    for j, (w, mu, P) in enumerate(zip(f["weights"], np.asarray(f["means"]), np.asarray(f["precisions"]))):
        y = X @ P - mu @ P
        logp[:, j] = -0.5 * (y ** 2).sum(1) + np.log(np.diag(P)).sum() + np.log(w)
    return logp.argmax(1) + 1


def _groups(feats: list[str], data: dict, labels: np.ndarray, names: dict) -> list[dict]:
    """ML-4 business terms: each cluster's row count, mean of each numeric feature, most common level of the others."""
    out = []
    for g in range(1, labels.max() + 1):
        rows = labels == g
        mean, common = {}, {}
        for c in feats:
            vals = data[c][rows]
            if ml._numeric(data[c]):
                x = _floats(vals)
                mean[names.get(c, c)] = None if np.isnan(x).all() else float(np.nanmean(x))
            elif seen := Counter(str(v) for v in vals if v is not None):
                common[names.get(c, c)] = seen.most_common(1)[0][0]
        out.append({"cluster": g, "rows": int(rows.sum()), "mean": mean, "most_common": common})
    return out


def _clustering(m: dict, feats: list[str], data: dict, names: dict) -> dict:
    enc = [ml._encoder(c, data[c]) for c in feats]
    X = ml.encode(enc, data)
    n, owners = len(X), ml._owners(enc)
    num = [j for j, o in enumerate(owners) if enc[o]["kind"] == "number"]
    ks = [m["k"]] if m.get("k") else range(2, min(MAX_K, n - 1) + 1)
    tries = METHODS["clustering"] if m.get("method", "auto") == "auto" else (m["method"],)
    if not num:  # a likelihood needs numbers (one-hot columns alone fit a mixture exactly)
        if tries == ("gmm",):
            raise ActionFailed(400, f"A Gaussian mixture needs a number among the features; model '{m['id']}' has categories only. Use k-means.")
        tries = ("kmeans",)
    fits = {}  # method: (fitted, labels, {per-k metric: score}, candidate metrics)
    if "kmeans" in tries:
        s, centers, scores = _kmeans(X, ks)
        labels = _assign(centers, X)
        fits["kmeans"] = ({"centers": centers.tolist()}, labels, {f"silhouette k={j}": v for j, v in scores.items()},
                          {"clusters": len(centers), "silhouette": s} | ({"bic": round(_kmeans_bic(X[:, num], centers[:, num], labels), 2)} if num else {}))
    if "gmm" in tries:
        g, bics = _gmm(X, ks, num)
        labels = _mixture(g, X)
        fits["gmm"] = (g, labels, {f"BIC k={j}": v for j, v in bics.items()},
                       {"clusters": len(g["weights"]), "silhouette": _silhouette(X, labels), "bic": min(bics.values())})
    tried = list(fits)
    # D-99: both read as Gaussian mixtures, compared by BIC on the numeric columns (work/validation/ML-CANDIDATES.2.md),
    # among the candidates whose groups are clearly apart (ML-3) when only one is (work/validation/ML-CANDIDATES.md)
    pool = [j for j in tried if fits[j][3]["silhouette"] >= CLEAR]
    pool = pool if len(pool) == 1 else tried
    method = min(pool, key=lambda j: fits[j][3].get("bic", 0))  # a tie keeps k-means (first)
    f, labels, scores, best = fits[method]
    s, k = best["silhouette"], best["clusters"]
    groups = _groups(feats, data, labels, names)
    clear = s >= CLEAR
    by = "by silhouette" if method == "kmeans" else "by BIC"
    how = f"k = {k}, set in the model" if m.get("k") else f"the best of k = 2..{max(ks)} {by}"
    summary = (f"Found {k} groups ({how}); silhouette {s:.2f}, so the groups are clearly apart. Group 1 is the largest"
               f" with {groups[0]['rows']:,} of {n:,} rows." if clear else
               f"The data shows no clear groups: the best split ({k} groups, {how}) has a silhouette of {s:.2f}, under"
               f" {CLEAR}. It is not better than the baseline (no grouping): don't use its groups.")
    cands = {j: x[3] for j, x in fits.items()}
    if len(cands) == 2:
        o = next(j for j in cands if j != method)
        summary += (f" Trained {ml.LABEL['kmeans']} and {ml.LABEL['gmm']} and kept {ml.LABEL[method]}: " + (
            f"only its groups are clearly apart (silhouette {s:.2f} vs {cands[o]['silhouette']:.2f}), though its BIC is higher"
            f" ({cands[method]['bic']:,.0f} vs {cands[o]['bic']:,.0f})." if len(pool) == 1 and cands[method]["bic"] > cands[o]["bic"] else
            f"BIC {cands[method]['bic']:,.0f} vs {cands[o]['bic']:,.0f} on the numeric columns (lower describes the rows better"
            " for the model's size)."))
    else:
        summary += _single(m, method, "the Gaussian mixture needs a number among the features")
    return {"method": method, "metrics": {"clusters": k, "silhouette": s, "rows": n} | scores, "candidates": cands,
            "baseline": {"clusters": 1}, "better": clear, "summary": summary, "groups": groups,
            "fitted": {"task": "clustering", "method": method, "features": enc} | f,
            "profile": ml.profile({c: data[c] for c in feats} | {"cluster": labels.astype(str).astype(object)})}


def _single(m: dict, method: str, why: str) -> str:
    """D-99: why a run trained one candidate."""
    return f" Method: {ml.LABEL[method]} ({'set in the model' if m.get('method', 'auto') != 'auto' else why})."


# --- anomaly detection ----------------------------------------------------------------------------------------------

def _apl(n) -> np.ndarray:
    """c(n): average path length of an unsuccessful search in a binary tree of n points (Isolation Forest)."""
    n = np.asarray(n, dtype=float)
    out = np.where(n == 2, 1.0, 0.0)
    big = n > 2
    out[big] = 2 * (np.log(n[big] - 1) + np.euler_gamma) - 2 * (n[big] - 1) / n[big]
    return out


def _forest(X: np.ndarray) -> dict:
    """An Isolation Forest as JSON-able arrays (ML-4: text, never a pickle)."""
    from sklearn.ensemble import IsolationForest
    from threadpoolctl import threadpool_limits

    with threadpool_limits(1):
        # contamination "auto" (score > 0.5) flagged half of clean data (E-04.1-fix-1); offset_ = the training scores' 1st percentile
        f = IsolationForest(contamination=SHARE, random_state=0, n_jobs=1).fit(X)
    return {"max_samples": int(f.max_samples_), "offset": float(f.offset_), "trees": [
        {"feature": t.feature.tolist(), "threshold": t.threshold.tolist(), "left": t.children_left.tolist(),
         "right": t.children_right.tolist(), "n": t.n_node_samples.tolist()} for t in (e.tree_ for e in f.estimators_)]}


def _trees(f: dict, width: int, leaf: str = "n") -> list[dict]:
    """The stored trees as arrays, checked: equal lengths, every child after its node (so a walk ends), splits on a column.
    `leaf`: the per-node array scoring reads (Isolation Forest: `n` samples; random forest: `value` rows, ML-CANDIDATES.1)."""
    out = []
    for t in f["trees"]:
        a = {k: np.asarray(t[k], dtype=float if k in ("threshold", "value") else int) for k in ("feature", "threshold", "left", "right", leaf)}
        idx, inner = np.arange(len(a["left"])), a["left"] != -1
        if not (len(idx) and len({len(v) for v in a.values()}) == 1 and all(
                ((a[k][inner] > idx[inner]) & (a[k][inner] < len(idx))).all() for k in ("left", "right"))
                and ((a["feature"][inner] >= 0) & (a["feature"][inner] < width)).all()):
            raise ValueError("tree shape")
        out.append(a)
    return out


def leaves(t: dict, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(each row's leaf node, its depth) in one checked tree (`_trees`); X already float32-rounded like sklearn's."""
    rows, node, depth = np.arange(len(X)), np.zeros(len(X), dtype=int), np.zeros(len(X))
    while (inner := t["left"][node] != -1).any():
        go = X[rows, np.maximum(t["feature"][node], 0)] <= t["threshold"][node]
        node = np.where(inner, np.where(go, t["left"][node], t["right"][node]), node)
        depth += inner
    return node, depth


def _forest_scores(f: dict, X: np.ndarray) -> np.ndarray:
    """sklearn's anomaly score 2^(-mean path length / c(max_samples)); `score_samples` is its negative."""
    X = X.astype(np.float32).astype(float)  # sklearn's trees compare float32 inputs
    trees, depth = _trees(f, X.shape[1]), np.zeros(len(X))
    for t in trees:
        node, d = leaves(t, X)
        depth += d + _apl(t["n"][node])
    return 2 ** (-depth / (len(trees) * _apl([f["max_samples"]])[0]))


def _seasonal(m: dict, query: dict, data: dict, dt: dict, values: list[str]) -> tuple[np.ndarray, tuple[float, float] | None]:
    """(Each row's robust z against its season, the largest over `values`; the D-99 season check: the mean absolute
    error, in robust deviations of each measure, of the seasonal expectation and of the period before, on the rows
    both judge, None if none). Expected = median of the same season position in earlier periods of its series (the
    first season has none: not judged, z 0); z = |residual| / its series' MAD scale."""
    grain, p = forecast.periods(m, data, dt)
    s, table = forecast.SEASON[grain], m["time"].split(".", 1)[0]
    keys = [c for c in query.get("dimensions", []) if c.split(".", 1)[0] != table]
    series = defaultdict(list)
    for i, k in enumerate(zip(*(data[c] for c in keys)) if keys else [()] * len(p)):
        series[k].append(i)
    z, err, judged = np.zeros(len(p)), np.zeros(2), 0
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # all-missing series
        for c in values:
            x = _floats(data[c])
            scale = _spread(x)[1]
            for rows in series.values():
                rows.sort(key=p.__getitem__)
                seen, exp, last = defaultdict(list), np.full(len(x), np.nan), np.full(len(x), np.nan)
                # ponytail: a median per period over a growing list, O(rows x seasons); a running median if one series gets huge
                for prev, i in zip([None, *rows[:-1]], rows):
                    if earlier := [v for v, q in seen[p[i] % s] if q < p[i]]:
                        exp[i] = np.nanmedian(earlier)
                    if prev is not None:
                        last[i] = x[prev]
                    seen[p[i] % s].append((x[i], p[i]))
                z[rows] = np.maximum(z[rows], robust_z(x[rows] - exp[rows], 0.0))
                e = np.abs(x[rows] - exp[rows]), np.abs(x[rows] - last[rows])
                ok = ~np.isnan(e[0]) & ~np.isnan(e[1])
                err += [e[0][ok].sum(), e[1][ok].sum()] / (scale if scale and not np.isnan(scale) else 1.0)
                judged += int(ok.sum())
    return z, tuple(round(float(v), 4) for v in err / judged) if judged else None


def _flags(m: dict, f: dict, query: dict, data: dict, dt: dict) -> tuple[np.ndarray, np.ndarray]:
    """(anomaly flags, scores) of `data` with the fitted model `f`."""
    if f["method"] == "seasonal":
        z, _ = _seasonal(m, query, data, dt, f["values"])
        return z > Z, z
    s = _forest_scores(f, ml.encode(f["features"], data))
    return -s < f["offset"], s


def _anomaly(m: dict, query: dict, feats: list[str], data: dict, dt: dict) -> dict:
    method = m.get("method", "auto")
    values = [c for c in feats if "." not in c and ml._numeric(data[c])]  # measures
    if method == "seasonal" and not values:
        raise ActionFailed(400, f"Seasonal anomaly detection checks measures over time; model '{m['id']}' has no measure"
                                " with numbers among its features.")
    tries = (method,) if method != "auto" else METHODS["anomaly"] if m.get("time") and values else ("isolation_forest",)
    fits, cands = {}, {}
    for k in tries:
        if k == "seasonal":
            f = {"task": "anomaly", "method": "seasonal", "values": values}
            z, err = _seasonal(m, query, data, dt, values)
            fits[k] = (f, z > Z)
            cands[k] = {"season_error": err and err[0], "last_period_error": err and err[1]}
        else:
            enc = [ml._encoder(c, data[c]) for c in feats]
            f = {"task": "anomaly", "method": "isolation_forest", "features": enc} | _forest(ml.encode(enc, data))
            fits[k] = (f, _flags(m, f, query, data, dt)[0])
        cands[k] = {"anomalies": int(fits[k][1].sum())} | cands.get(k, {})
    if len(fits) == 2:
        # D-99: no labels to score either on (work/validation/ML-CANDIDATES.2.md); the seasonal rule is kept when the season
        # is real: the same period in earlier seasons predicts the measures better than the period before
        c = cands["seasonal"]
        method = "seasonal" if c["season_error"] is not None and c["season_error"] < c["last_period_error"] else "isolation_forest"
        why = (f" Trained {ml.LABEL['isolation_forest']} and {ml.LABEL['seasonal']} and kept {ml.LABEL[method]}: " + (
            f"the same period in earlier seasons predicts the measures {'better' if method == 'seasonal' else 'no better'} than the"
            f" period before (off by {c['season_error']:.2f} vs {c['last_period_error']:.2f} robust deviations)."
            if c["season_error"] is not None else "no value has an earlier season to compare with."))
    else:
        [method] = fits
        why = _single(m, method, "the seasonal rule needs a time column and a measure")
    f, flags = fits[method]
    label = ("The seasonal baseline (each value vs the median of the same period in earlier seasons)" if method == "seasonal"
             else f"The Isolation Forest (its {SHARE:.0%} most isolated rows)")
    base = rule(values if method == "seasonal" else feats, data)
    n, a, b, both = len(flags), int(flags.sum()), int(base.sum()), int((flags & base).sum())
    summary = (f"{label} flags {a:,} of {n:,} rows as anomalies; the simple rule (a value more than {Z} robust deviations"
               f" from its median) flags {b:,}; both flag {both:,}. ")
    summary += ("It finds anomalies the simple rule misses." if a > both else "It adds nothing over the simple rule.") + why
    return {"method": method, "metrics": {"rows": n, "anomalies": a, "flagged_by_both": both}, "baseline": {"rows": n, "anomalies": b},
            "candidates": cands, "better": a > both, "summary": summary, "fitted": f,
            "profile": ml.profile({c: data[c] for c in feats} | {"anomaly": flags.astype(str).astype(object)})}


# --- training and scoring (abi.ml) ----------------------------------------------------------------------------------

def train(project: str, m: dict, query: dict, columns: list[str], data: dict, names: dict) -> dict:
    """The registry fields of one clustering or anomaly training run."""
    mj = semantic_model.load(project)
    feats = features(m, columns, mj)
    if not feats:
        raise ActionFailed(400, f"Dataset '{m['dataset']}' has no columns to learn from besides row identity (keys, dates); list features.")
    if (n := len(data[columns[0]])) < 10:
        raise ActionFailed(400, f"Training needs at least 10 rows; dataset '{m['dataset']}' has {n}.")
    out = (_clustering(m, feats, data, names) if m["task"] == "clustering"
           else _anomaly(m, query, feats, data, mj.get("date_table") or {}))
    return out | {"fitted": json.dumps(out["fitted"])}


def _restore(fitted: str) -> dict:
    """A stored clustering or anomaly model, validated: JSON with the expected shape, never unpickled."""
    try:
        f = json.loads(fitted)
        if f["method"] == "kmeans":
            c = np.array(f["centers"], dtype=float)
            ok = c.ndim == 2 and 2 <= len(c) <= MAX_K and c.shape[1] == len(ml._owners(f["features"]))
        elif f["method"] == "gmm":
            w, mu, P = (np.array(f[k], dtype=float) for k in ("weights", "means", "precisions"))
            k, d = len(w), len(ml._owners(f["features"]))
            ok = (w.shape == (k,) and 2 <= k <= MAX_K and (w > 0).all() and mu.shape == (k, d) and P.shape == (k, d, d)
                  and (np.diagonal(P, axis1=1, axis2=2) > 0).all())
        elif f["method"] == "isolation_forest":
            ok = bool(_trees(f, len(ml._owners(f["features"])))) and f["max_samples"] >= 2
        else:
            ok = f["method"] == "seasonal" and all(isinstance(v, str) for v in f["values"])
    except Exception as e:  # noqa: BLE001 - any parse failure means the stored text is invalid
        raise ValueError(f"The stored fitted model is invalid: {type(e).__name__}.") from None
    if not ok:
        raise ValueError("The stored fitted model is invalid: its shape doesn't match.")
    return f


def predict(m: dict, query: dict, data: dict, dt: dict, fitted: str) -> dict:
    """ML-5 output columns for `data`: `cluster`, or `anomaly` and `score`."""
    f = _restore(fitted)
    if f["method"] == "kmeans":
        return {"cluster": _assign(np.array(f["centers"]), ml.encode(f["features"], data))}
    if f["method"] == "gmm":
        return {"cluster": _mixture(f, ml.encode(f["features"], data))}
    flags, score = _flags(m, f, query, data, dt)
    return {"anomaly": flags, "score": score}


def measures(m: dict, t: str) -> list[dict]:
    """The measures of ml_<id>: row count (group it by the cluster or anomaly dimension), and the top anomaly score."""
    col, q = "cluster" if m["task"] == "clustering" else "anomaly", storage._q(t)
    out = [{"id": f"{m['id'][:57]}_rows", "name": f"{m['name']} rows", "expression": f"COUNT({q}.{col})",
            "description": f"Rows scored by ML model '{m['id']}'; group by {t}.{col}."}]
    if col == "anomaly":
        out.append({"id": f"{m['id'][:57]}_score", "name": f"{m['name']} anomaly score", "expression": f"MAX({q}.score)",
                    "description": f"Highest anomaly score of ML model '{m['id']}' (seasonal: robust z; Isolation Forest: its score)."})
    return out


def scored(project: str, m: dict, out: dict, t: str) -> str:
    """The scoring summary; anomalies also post to the feed, counts only (E-02 row 27), once per change (§5 quiet)."""
    if "cluster" in out:
        sizes = Counter(int(g) for g in out["cluster"])
        return f"Wrote {len(out['cluster']):,} rows to table {t}: " + ", ".join(f"group {g} {sizes[g]:,}" for g in sorted(sizes)) + "."
    n, a = len(out["anomaly"]), int(np.sum(out["anomaly"]))
    notify_change("ml_anomaly", project, f"ML model {m['name']}: ", a and f"{a:,} of {n:,} rows flagged as anomalies in the"
                  f" latest scoring (table {t}).", "no anomalies in the latest scoring.", m["id"])
    return f"Wrote {n:,} rows to table {t}; {a:,} flagged as anomalies."

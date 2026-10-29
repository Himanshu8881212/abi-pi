"""Forecast training: ML-1 (LightGBM on lags and statsforecast AutoETS, D-99; ARIMA only scores old runs), ML-3 (time-based backtest, seasonal-naive baseline),
ML-4 (metrics in business terms, text model). work/validation/X-01.2.md, ML-SEASONAL.1.md.

One series per combination of the dataset's non-date dimensions, at the grain of the model's `time` column. The
backtest averages rolling windows of `horizon` periods: the latest one and, when the history holds it, the
forecast period a year earlier (the last seasonal turn); `auto` keeps the method with the lowest MAE on the windows
`horizon` periods before them (H-07; on the backtest itself only when the history is too short). The baseline is the better of the same
period one season earlier and one year earlier, and `better` must beat it. Yearly seasonality: LightGBM gets a lag of
one year and, by day, month / ISO week / day of year; ETS becomes MSTL (season + year) with an AutoETS trend once the
history spans two years. A forecast whose horizon total against the same periods last year departs by more than
PLAUSIBLE (as a ratio) from what the latest periods showed against a year before is called out in the summary, and
`auto` keeps the other candidate when only its forecast passes (ML-SEASONAL). statsforecast and LightGBM are imported inside the functions
(cold import ~seconds); every fit is single-threaded (D-17)."""

import json
import warnings
from datetime import date

import numpy as np

from abi import ml  # circular (abi.ml imports this module): only used at call time
from abi.actions import ActionFailed
from abi.ml import search

SEASON = {"day": 7, "week": 52, "month": 12, "quarter": 4, "year": 1}  # season length per calendar grain
YEAR = {"day": 364, "week": 52, "month": 12, "quarter": 4, "year": 1}  # periods back to the same period last year (52 weeks: same weekday)
PLAUSIBLE = 0.25  # a horizon total whose ratio to last year is this far (share) off the latest ratio is called out (ML-SEASONAL)
PER_YEAR = {"month": 12, "quarter": 4}
LAST = {"day": "the same weekday last week", "week": "the same week last year", "month": "the same month last year",
        "quarter": "the same quarter last year", "year": "last year"}
CYCLE = {"day": "day of the week", "week": "week of the year", "month": "month of the year", "quarter": "quarter of the year"}
HORIZON = {"day": 7, "week": 13, "month": 6, "quarter": 4, "year": 1}  # default horizon when the model sets none
METHODS = ("lightgbm", "ets")  # D-99: best first; ARIMA is retired (its stored runs still score: `_stat`)
MAX_SERIES = 200  # ETS fits per series (~0.1-1 s each): above this a run trains for minutes (ML-FIX-ETS)


def periods(m: dict, data: dict, dt: dict) -> tuple[str, list[int]]:
    """(grain, each row's period): contiguous integers at the grain of `m["time"]` (a year x 12 + month, a day ordinal,
    an ISO week's Monday ordinal // 7). A week, month or quarter column of DATEs (e.g. month starts) is the period itself:
    no year column needed."""
    table, col = m["time"].split(".", 1)
    grain = "day" if col == dt.get("column") else col
    if grain not in SEASON:
        raise ActionFailed(400, f"A forecast can't run by {m['time']}: its time must be {table}.day, .week, .month,"
                                " .quarter or .year.")
    cal = {c.split(".", 1)[1]: data[c] for c in data if c.split(".", 1)[0] == table}
    yc = "week_year" if grain == "week" else "year"  # ISO year: week 1 starting 2024-12-30 belongs to 2025
    dated = grain in ("week", "month", "quarter") and all(isinstance(v, date) for v in cal.get(col, ()) if v is not None)
    need = [col] + ([yc] if grain not in ("day", "year") and not dated and yc in cal else [])
    if nulls := sum(any(cal[c][i] is None for c in need) for i in range(len(cal[col]))):
        raise ActionFailed(400, f"{' or '.join(f'{table}.{c}' for c in need)} has no value on {nulls} row(s) of the dataset;"
                                " a forecast needs a period on every row. Filter those rows out in the dataset.")
    if grain == "day":
        p = [date.fromisoformat(str(v)[:10]).toordinal() for v in cal[col]]
    elif dated and grain == "week":  # a datetime is a date too
        p = [date.fromisocalendar(*v.isocalendar()[:2], 1).toordinal() // 7 for v in cal[col]]
    elif dated:
        p = [v.year * PER_YEAR[grain] + (v.month - 1) * PER_YEAR[grain] // 12 for v in cal[col]]
    elif grain == "year":
        p = _ints(table, "year", cal["year"], grain)
    elif yc not in cal:
        _ints(table, grain, cal[col], grain)  # a text month gets its own column's 400 first, not "add a year column"
        raise ActionFailed(400, f"A forecast by {grain} needs {table}.{yc} in the dataset's dimensions too, so each row is"
                                f" one {grain} and not the same {grain} across years.")
    elif grain == "week":
        try:
            p = [date.fromisocalendar(y, w, 1).toordinal() // 7
                 for y, w in zip(_ints(table, yc, cal[yc], grain), _ints(table, "week", cal["week"], grain))]
        except ValueError:  # week 53 of a year with 52 ISO weeks
            _bad(table, "week", grain)
    else:
        p = [y * PER_YEAR[grain] + v - 1 for y, v in zip(_ints(table, "year", cal["year"], grain), _ints(table, grain, cal[grain], grain))]
    return grain, p


RANGE = {"year": (1, 9999), "week_year": (1, 9999), "week": (1, 53), "month": (1, 12), "quarter": (1, 4)}


def _ints(table: str, col: str, values, grain: str) -> list[int]:
    """`values` as integers in the column's RANGE, else a 400 naming the column and what it must hold (never a value:
    values are data)."""
    lo, hi = RANGE[col]
    try:
        if all(not isinstance(v, (str, bytes, bool, date)) and v == int(v) and lo <= v <= hi for v in values):
            return [int(v) for v in values]
    except (TypeError, ValueError, OverflowError):
        pass
    _bad(table, col, grain)


def _bad(table: str, col: str, grain: str):
    lo, hi = RANGE[col]
    what = "a year number" if col in ("year", "week_year") else f"a {grain} number or a date in that {grain} (e.g. its first day)"
    raise ActionFailed(400, f"A forecast by {grain} can't read {table}.{col}: it must hold {what}, numbers {lo}-{hi}."
                            " Change the column's type or pick another time column.")


def _series(m: dict, query: dict, data: dict, dt: dict) -> tuple[str, int, list, list, np.ndarray]:
    """(grain, first period, key columns, series keys, values: series x periods). Periods are contiguous integers at the grain;
    a period a series has no row for counts as 0. A row with no target value counts as no row (training drops them too,
    so scoring and the drift backtest see the same series; e.g. a gain measure is empty on the first day)."""
    keep = [v is not None for v in data[m["target"]]]
    data = {c: np.asarray(v, dtype=object)[keep] for c, v in data.items()}
    grain, p = periods(m, data, dt)
    table = m["time"].split(".", 1)[0]
    keys = [c for c in query.get("dimensions", []) if c.split(".", 1)[0] != table]
    sid = list(zip(*(data[k] for k in keys))) if keys else [()] * len(p)
    if not p:
        raise ActionFailed(400, "The dataset has no rows with a value to forecast.")
    if any(isinstance(v, (str, bool)) for v in data[m["target"]]):
        raise ActionFailed(400, f"A forecast predicts a number, but {m['target']} has text values.")
    lo, n = min(p), max(p) - min(p) + 1
    idx = {k: i for i, k in enumerate(dict.fromkeys(sid))}
    # ponytail: a missing period is 0 (right for sums like sales); a per-measure fill rule if averages need it
    Y, seen = np.zeros((len(idx), n)), set()
    for k, pi, v in zip(sid, p, data[m["target"]]):
        if (k, pi) in seen:
            raise ActionFailed(400, f"The dataset has several rows for one {grain}; a forecast needs one row per {grain}"
                                    f" (per {', '.join(keys) or 'series'}). Remove finer date columns from its dimensions.")
        seen.add((k, pi))
        Y[idx[k], pi - lo] = float(v)
    return grain, lo, keys, [list(k) for k in idx], Y


def _stat(method: str, Y: np.ndarray, h: int, s: int, year: int | None = None, params: dict | None = None) -> tuple[np.ndarray, list[str]]:
    """statsforecast forecasts of each series, and the chosen model of each (e.g. ETS(M,A,A), ARIMA(1,1,0)(0,1,0)[12]).
    ETS with `year` (periods per year; history >= two years) is MSTL: season and year taken out by STL, AutoETS on the rest.
    ETS `params` (ML-ZOO.1): AutoETS's `model` code and `damped`; its season letter is N where there is no season to fit."""
    from statsforecast.arima import arima_string
    from statsforecast.models import MSTL, AutoARIMA, AutoETS, SeasonalNaive

    p = params or {}
    code = p.get("model", "ZZN" if year else "ZZZ")  # today's defaults when no setting is given
    ets = {"model": code[:2] + "N" if year or (s == 1 and "model" in p) else code, "damped": p.get("damped")}
    preds, specs = [], []
    for y in Y:  # one series at a time in this process: single-threaded (n_jobs=1)
        if method == "ets" and year:
            mdl = MSTL(season_length=[s, year], trend_forecaster=AutoETS(**ets)).fit(y)
            specs.append(f"MSTL({s}, {year}) + {mdl.trend_forecaster.model_.get('method', 'ETS')}")
        else:
            mdl = (AutoETS(season_length=s, **ets) if method == "ets" else
                   {"arima": AutoARIMA, "naive": SeasonalNaive}[method](season_length=s)).fit(y)
            if method != "naive":
                specs.append(mdl.model_.get("method", "ETS") if method == "ets" else arima_string(mdl.model_))
        preds.append(mdl.predict(h)["mean"])
    return np.array(preds), specs


CALENDAR = ["month", "week of the year", "day of the year"]  # LightGBM's calendar features by day (ML-SEASONAL.1)


def _feats(H: np.ndarray, ts: list[int], lags: list[int], s: int, lo: int, cal: bool = False) -> np.ndarray:
    """LightGBM's features of periods `ts` (column indexes of H, whose first period is `lo`): the lags, the position
    in the season and, with `cal` (day grain), CALENDAR; rows series by series, period by period (as H[:, ts].ravel())."""
    cycle = [np.tile((lo + np.asarray(ts)) % s, H.shape[0])] if s > 1 else []
    if cal:
        days = np.array([(d.month, d.isocalendar()[1], d.timetuple().tm_yday) for d in map(date.fromordinal, lo + np.asarray(ts))])
        cycle += [np.tile(c, H.shape[0]) for c in days.T]
    return np.column_stack([H[:, [t - lag for t in ts]].ravel() for lag in lags] + cycle)


def _ahead(booster, Y: np.ndarray, h: int, lags: list[int], s: int, lo: int, cal: bool = False) -> np.ndarray:
    """The next `h` periods of every series, forecast recursively (each forecast feeds the next one's lags)."""
    H = Y
    for _ in range(h):
        H = np.column_stack([H, booster.predict(_feats(H, [H.shape[1]], lags, s, lo, cal), num_threads=1)])
    return H[:, Y.shape[1]:]


def _lgb(Y: np.ndarray, h: int, s: int, lo: int, year: int, cal: bool, params: dict | None = None):
    """LightGBM on lag features (1-3 periods, one season and, with a year and a quarter of history, one year earlier)
    plus the position in the season (and CALENDAR with `cal`), one model across series, forecast recursively; `params`:
    a searched setting (ML-ZOO.1). Returns (forecast, booster, lags)."""
    import lightgbm

    n = Y.shape[1]
    lags = sorted({x for x in (1, 2, 3, s) if x <= n // 2} | ({year} if n >= year + year // 4 else set()))
    ts = list(range(max(lags), n))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        # ponytail: one global model on raw values; scale each series if their sizes differ by orders of magnitude
        est = lightgbm.LGBMRegressor(**{"n_jobs": 1, "verbose": -1, "random_state": 0, "min_child_samples": 5} | (params or {})).fit(
            _feats(Y, ts, lags, s, lo, cal), Y[:, max(lags):].ravel())
    return _ahead(est.booster_, Y, h, lags, s, lo, cal), est.booster_, lags


def _fit(method: str, Y: np.ndarray, h: int, s: int, lo: int, grain: str, params: dict | None = None) -> tuple:
    """(forecast of the next h periods, model fields to store, LightGBM booster or None) of one candidate on history Y,
    with a searched setting `params` (ML-ZOO.1; kept with the model: ETS refits with it when it scores)."""
    year, params = YEAR[grain], params or {}
    if method == "lightgbm":
        cal = grain == "day"  # ponytail: by week the 52-week lag and season position carry the year; CALENDAR if that drifts
        pred, booster, lags = _lgb(Y, h, s, lo, year, cal, params)
        return pred, {"lags": lags, "calendar": cal, "params": params, **ml.booster_text(booster)}, booster
    year = year if method == "ets" and year > s and Y.shape[1] >= 2 * year else None
    pred, specs = _stat(method, Y, h, s, year, params)
    return pred, {"specs": specs, "year": year, "params": params}, None


def _forward(f: dict, Y: np.ndarray, h: int, lo: int) -> np.ndarray:
    """The next h periods of every series in Y with a stored fitted forecast `f` (LightGBM: its booster on Y's lags;
    ETS/ARIMA refit their method, with its searched setting, on Y: statsforecast's fitted state isn't kept as text)."""
    if f["method"] != "lightgbm":
        return _stat(f["method"], Y, h, f["season_length"], f.get("year"), f.get("params"))[0]
    if (booster := ml._booster(f)) is None:
        raise ValueError("The stored fitted model is invalid: its checksum doesn't match.")
    return _ahead(booster, Y, h, f["lags"], f["season_length"], lo, f.get("calendar", False))


def _errors(pred: np.ndarray, actual: np.ndarray) -> dict:
    err, tot = np.abs(pred - actual).sum(), np.abs(actual).sum()
    return {"mae": float(err / actual.size), "wape": float(err / tot) if tot else None}


def _backtest(Y: np.ndarray, origins: list[int], h: int, fit) -> tuple[dict, tuple]:
    """ML-3: the pooled errors of `fit(history) -> (forecast, ...)` over the windows of h periods starting at each
    origin, and the first (latest) window's fit."""
    outs = [fit(Y[:, :o]) for o in origins]
    pred = np.concatenate([x[0] for x in outs], axis=1)
    return _errors(pred, np.concatenate([Y[:, o:o + h] for o in origins], axis=1)), outs[0]


def train(m: dict, query: dict, data: dict, dt: dict, names: dict) -> dict:
    """The registry fields of one forecast training run (ml._train adds commit and snapshot)."""
    grain, lo, keyed, keys, Y = _series(m, query, data, dt)
    s, h, year = SEASON[grain], m.get("horizon") or HORIZON[grain], YEAR[grain]
    n = Y.shape[1]
    if n < 2 * s + h:
        raise ActionFailed(400, f"A forecast by {grain} with a horizon of {h} needs at least {2 * s + h} {grain}s of"
                                f" history (two seasons of {s} plus the {h} held out to test it); the dataset spans {n}.")
    methods = METHODS if m.get("method", "auto") == "auto" else [m["method"]]
    if len(keys) > MAX_SERIES and "ets" in methods:
        raise ActionFailed(400, f"The dataset has {len(keys):,} series; ETS fits each one separately (about a second"
                                f" each), so a forecast with method {m.get('method', 'auto')} takes at most {MAX_SERIES:,}."
                                " Use method lightgbm (one model across every series), or drop a dimension from the dataset.")
    # ML-3 rolling windows: the latest h periods and, when the history holds it, the forecast period a year earlier
    origins = [o for o in dict.fromkeys((n - h, n - year)) if 2 * s <= o <= n - h]
    # H-07: choose on the windows h periods before them, never on one overlapping a backtest window
    val = [o - h for o in origins if o - h >= 2 * s and not any(b - h < o - h < b + h for b in origins)]
    bases = {"season": _backtest(Y, origins, h, lambda Yh: _stat("naive", Yh, h, s))[0]}
    if year > s and min(origins) >= year:
        bases["year"] = _backtest(Y, origins, h, lambda Yh: _stat("naive", Yh, h, year))[0]
    kind = min(bases, key=lambda k: bases[k]["mae"])  # `better` must beat both
    baseline = bases[kind] | ({f"last {k} mae": b["mae"] for k, b in bases.items()} if len(bases) > 1 else {})
    # ML-ZOO.1: rolling-origin windows of h periods inside the history before every backtest window (never read by a trial)
    Yt = Y[:, :min(origins)]
    folds = [o for o in (Yt.shape[1] - h * j for j in range(1, search.FOLDS + 1)) if o >= 2 * s] if m.get("search") else []
    params, trials = {k: m.get("params", {}).get(k) for k in methods}, []  # ML-ZOO.2: a fixed setting (use_ml_trial)
    if folds:
        def score(k, p):
            e = [_errors(_fit(k, Yt[:, :o], h, s, lo, grain, p)[0], Yt[:, o:o + h])["mae"] for o in folds]
            return float(np.mean(e)), float(np.std(e))
        params, trials = search.candidates(methods, m.get("space", {}), m["search"], score, True, "mae")
    fits = {k: _backtest(Y, origins, h, lambda Yh, k=k: _fit(k, Yh, h, s, lo, grain, params[k])) for k in methods}
    cands, sel, window = {k: e for k, (e, _) in fits.items()}, None, ml.SHORT
    how = f"{len(folds)} rolling-origin windows of {h} {grain}s before the backtest"
    if len(methods) > 1 and val:  # H-07; with a search: each candidate at its kept setting
        sel = {k: _backtest(Y, val, h, lambda Yh, k=k: _fit(k, Yh, h, s, lo, grain, params[k]))[0] for k in methods}
        prior = f"earlier windows of {h} {grain}s" if len(val) > 1 else f"the {h} {grain}s before {'the backtest windows' if len(origins) > 1 else 'them'}"
        window = f"{prior} (validation), not the backtest reported above"
    elif folds:  # no validation window: the kept settings' search scores (a best of several: a little optimistic)
        sel = {k: {"mae": next(t["score"] for t in trials if t["method"] == k and t["chosen"])} for k in methods}
        window = f"{how} (search), not the backtest reported above"
    method = ml.choose(cands, sel, "mae", lambda a, b: a["mae"] < b["mae"])  # a tie keeps the first: LightGBM
    # plausibility: each candidate's forecast total against the same periods last year, as a ratio of the ratio the
    # latest periods showed against a year before (growth compounds, so a ratio, not a difference of shares)
    ly = float(Y[:, n - year:n - year + h].sum()) if year >= h and n >= year + h else 0.0
    before = float(Y[:, n - h - year:n - year].sum()) if ly > 0 else 0.0
    growth = float(Y[:, n - h:].sum()) / before if before > 0 else 1.0
    # LightGBM is refit on the whole history (its backtest fit never saw the latest h periods); ETS refits when it forecasts
    full = {k: (_fit(k, Y, h, s, lo, grain, params[k]) if k == "lightgbm" else fits[k][1])[1:] for k in (methods if ly > 0 else [method])}
    totals = {k: float(_forward({"method": k, "season_length": s, **x[0]}, Y, h, lo).sum()) for k, x in full.items()} if ly > 0 else {}
    ok = {k: abs(x / ly / growth - 1) <= PLAUSIBLE for k, x in totals.items()}
    chosen = method
    if len(ok) > 1 and not ok[method] and any(ok.values()):  # auto never keeps a forecast its own check rejects
        method = next(k for k in ok if ok[k])
    model, booster = full[method]
    metrics = {"mae": cands[method]["mae"], "wape": cands[method]["wape"], "windows": len(origins)}
    better = metrics["mae"] < baseline["mae"]
    t = names.get(m["target"], m["target"])
    importance = []
    if booster is not None:
        gain = booster.feature_importance("gain")
        gain = gain / gain.sum() if gain.sum() else gain
        labels = ([f"{t} {lag} {grain}{'s' if lag > 1 else ''} earlier" for lag in model["lags"]]
                  + ([CYCLE[grain]] if s > 1 else []) + (CALENDAR if model["calendar"] else []))
        importance = sorted(({"feature": lab, "name": lab, "share": round(float(g), 4)} for lab, g in zip(labels, gain)),
                            key=lambda x: -x["share"])
    per = f"{grain}" + (f" and {', '.join(names.get(k, k) for k in keyed)}" if keyed else "")
    last = {"season": LAST[grain], "year": f"the same {grain} last year"}
    second = " and on last year's version of the forecast period" if len(origins) > 1 else ""
    summary = (f"Backtest on the latest {h} of {n} {grain}s{second}:"
               f" {method.upper() if method != 'lightgbm' else 'LightGBM'} is off by"
               f" {metrics['mae']:,.2f} {t} per {per} on average, vs {baseline['mae']:,.2f} for {last[kind]}")
    summary += f" (a better baseline than {last[min(set(bases) - {kind})]})." if len(bases) > 1 else "."
    summary += " It is better than the baseline." if better else " It is not better than the baseline: don't use its forecast."
    if method == chosen:
        summary += ml.versus(cands, method, "mae", window)
    else:
        key = "validation_mae" if "validation_mae" in cands[chosen] else "mae"
        summary += (f" Trained {ml.LABEL['lightgbm']} and {ml.LABEL['ets']} and kept {ml.LABEL[method]}: only its forecast"
                    f" passes the plausibility check against last year, though {ml.LABEL[chosen]} scored mae"
                    f" {cands[chosen][key]:,.2f} vs {cands[method][key]:,.2f} on {window}.")
    summary += search.said(cands, trials, how) if folds else ""
    if top := [x["name"] for x in importance[:3] if x["share"] > 0]:
        summary += f" Strongest drivers: {', '.join(top)}."
    # ML-6 drift compares with an out-of-sample error: the backtest fit's on the latest window (the refit model's error
    # there is in-sample, 2-5x lower, so fresh periods of an unchanged process would read as drift)
    fitted = {"task": "forecast", "method": method, "grain": grain, "season_length": s, "horizon": h, "first_period": lo,
              "series": keys, "latest_mae": _errors(fits[method][1][0], Y[:, -h:])["mae"], **model}
    holdout = {"backtest periods": h, "periods": n} | ({"search folds": len(folds)} if folds else {})  # AG-2: the figures its summary states
    if method in totals:
        total = totals[method]
        holdout |= {"forecast total": total, "same periods last year total": ly, "plausible": ok[method]}
        if not ok[method]:
            summary += (f" Check this forecast: its total for the next {h} {grain}s is {total:,.2f} {t}, vs {ly:,.2f} for the"
                        f" same {grain}s last year, a {'bigger' if total / ly > growth else 'smaller'} change than the"
                        f" latest {h} {grain}s showed against a year before.")
    return {"method": method, "metrics": metrics, "baseline": baseline, "better": better, "importance": importance,
            "excluded": [], "candidates": cands, "summary": summary, "fitted": json.dumps(fitted, default=str),
            "holdout": holdout, "trials": trials}


def recent_error(m: dict, query: dict, data: dict, dt: dict, f: dict) -> float | None:
    """ML-6: the backtest rerun on fresh rows: the stored method's MAE on the latest `horizon` periods, forecast from
    the history before them. None when that history is too short to forecast from."""
    _, lo, _, _, Y = _series(m, query, data, dt)
    s, h = f["season_length"], f["horizon"]
    hist = Y[:, :-h]
    if hist.shape[1] < max(2 * s, *f.get("lags", [1]), 2 * (f.get("year") or 0)):
        return None
    return _errors(_forward(f, hist, h, lo), Y[:, -h:])["mae"]


def _date(grain: str, p: int) -> date:
    """The first day of period `p` (a period index of _series)."""
    if grain == "day":
        return date.fromordinal(p)
    if grain == "week":
        return date.fromordinal(p * 7 + 1)  # the week's Monday (ordinal 1 is a Monday)
    if grain == "year":
        return date(p, 1, 1)
    y, i = divmod(p, PER_YEAR[grain])
    return date(y, i * 12 // PER_YEAR[grain] + 1, 1)


def predict(m: dict, query: dict, data: dict, dt: dict, f: dict) -> dict:
    """ML-5: the next `horizon` periods of every series in fresh dataset rows, from a stored fitted forecast `f`
    (`_forward`): {key column: values, "period": first days, "prediction": numbers}."""
    grain, lo, keyed, keys, Y = _series(m, query, data, dt)
    s, h, n = f["season_length"], f["horizon"], Y.shape[1]
    if n < (need := max(2 * s, *f.get("lags", [1]))):
        raise ActionFailed(400, f"Scoring needs at least {need} {grain}s of history; the dataset spans {n}.")
    # ponytail: ETS/ARIMA re-pick their order on the fresh history; keep and refit the exact order if forecasts must match the backtest
    pred = _forward(f, Y, h, lo)
    out = {k: [key[i] for key in keys for _ in range(h)] for i, k in enumerate(keyed)}
    return out | {"period": [_date(grain, lo + n + j) for _ in keys for j in range(h)], "prediction": pred.ravel().tolist()}

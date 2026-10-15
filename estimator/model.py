"""Review-multiple model with shrinkage toward published priors, plus backtest.

units = reviews x multiple
log(multiple) = log(published multiple for the year each review was written) + x . beta

x holds centred features (review volume, price, review score, game age).
beta is fitted on your ground truth with ridge shrinkage, so with little
ground truth the model stays at the published priors and moves away only
as evidence accumulates.
"""
import math
import re
from datetime import datetime, timezone

import numpy as np

FEATURES = ["intercept", "log_reviews", "log_price", "review_score", "log_age"]
CENTRES = [0.0, 7.0, 2.7, 0.85, 2.5]


# ---------------- helpers ----------------
def parse_release(app):
    """Earliest of the store release date and the first month with reviews
    (store pages of games that left Early Access show the 1.0 date)."""
    rd = ((app.get("details") or {}).get("release_date") or {}).get("date") or ""
    store = None
    for fmt in ("%d %b, %Y", "%b %d, %Y", "%d %B, %Y", "%B %d, %Y", "%b %Y", "%B %Y", "%Y"):
        try:
            store = datetime.strptime(rd.strip(), fmt).replace(tzinfo=timezone.utc)
            break
        except ValueError:
            pass
    if store is None:
        m = re.search(r"(19|20)\d\d", rd)
        if m:
            store = datetime(int(m.group(0)), 7, 1, tzinfo=timezone.utc)
    roll = [r for r in ((app.get("history") or {}).get("rollups") or []) if (r[1] or 0) + (r[2] or 0) > 0]
    first = datetime.fromtimestamp(min(r[0] for r in roll), timezone.utc) if roll else None
    cands = [d for d in (store, first) if d]
    return min(cands) if cands else None


def prior_multiple(settings, year):
    table = {int(k): v for k, v in settings["prior_sales_per_review_by_review_year"].items()
             if k.isdigit()}
    if year is None:
        return table[max(table)]
    year = min(max(year, min(table)), max(table))
    return table[year]


def list_price_usd(app):
    po = (app.get("details") or {}).get("price_overview") or {}
    if po.get("currency") == "USD" and po.get("initial"):
        return po["initial"] / 100.0
    return None


def review_score(app):
    s = app.get("summary") or {}
    tot = s.get("total_reviews") or 0
    return (s.get("total_positive", 0) / tot) if tot else None


def review_curve(app):
    """Cumulative reviews over time as [(unix_ts, cumulative)], scaled so the
    end point matches Steam's current total review count."""
    h = app.get("history") or {}
    roll = sorted(h.get("rollups") or [])
    if not roll:
        return []
    step = 7 * 86400 if h.get("rollup_type") == "week" else 30.44 * 86400
    pts = [(roll[0][0], 0.0)]
    cum = 0.0
    for i, (ts, up, down) in enumerate(roll):
        cum += (up or 0) + (down or 0)
        end = roll[i + 1][0] if i + 1 < len(roll) else ts + step
        pts.append((end, cum))
    total = (app.get("summary") or {}).get("total_reviews")
    if total and cum > 0:
        k = total / cum
        pts = [(t, c * k) for t, c in pts]
    return pts


def baseline_curve(settings, curve):
    """Adds cumulative 'baseline units' to the review curve: each period's new
    reviews x the published multiple for the year those reviews were written."""
    out, b = [], 0.0
    for i, (t, c) in enumerate(curve):
        if i:
            t0, c0 = curve[i - 1]
            yr = datetime.fromtimestamp((t0 + t) / 2, timezone.utc).year
            b += (c - c0) * prior_multiple(settings, yr)
        out.append((t, c, b))
    return out


def at_time(bcurve, ts):
    """(cumulative reviews, cumulative baseline units) at a timestamp."""
    if not bcurve:
        return None, None
    if ts <= bcurve[0][0]:
        return 0.0, 0.0
    for (t0, c0, b0), (t1, c1, b1) in zip(bcurve, bcurve[1:]):
        if ts <= t1:
            f = (ts - t0) / max(t1 - t0, 1)
            return c0 + (c1 - c0) * f, b0 + (b1 - b0) * f
    return bcurve[-1][1], bcurve[-1][2]


def reviews_at(curve, ts):
    if not curve:
        return None
    if ts <= curve[0][0]:
        return 0.0
    for (t0, c0), (t1, c1) in zip(curve, curve[1:]):
        if ts <= t1:
            return c0 + (c1 - c0) * (ts - t0) / max(t1 - t0, 1)
    return curve[-1][1]


def feature_vector(reviews, price, score, age_months):
    raw = [1.0,
           math.log(max(reviews, 1.0)),
           math.log1p(price if price is not None else 15.0),
           score if score is not None else 0.85,
           math.log1p(max(age_months, 0.0))]
    return np.array([r - c for r, c in zip(raw, CENTRES)])


# ---------------- fitting ----------------
def fit(rows, ridge):
    """rows: list of dicts with x (vector), resid (log actual - log reviews - log prior), w."""
    k = len(FEATURES)
    if not rows:
        return np.zeros(k)
    X = np.array([r["x"] for r in rows])
    y = np.array([r["resid"] for r in rows])
    W = np.diag([r["w"] for r in rows])
    A = X.T @ W @ X + ridge * np.eye(k)
    return np.linalg.solve(A, X.T @ W @ y)


def training_rows(settings, gt, apps):
    m = settings["model"]
    weights = m["scope_weights"]
    used, reference, skipped = [], [], []
    for g in gt:
        app = apps.get(g["appid"])
        label = f'{g.get("game") or g["appid"]} ({g["date"]})'
        if not app or not app.get("summary"):
            skipped.append((label, "no Steam data yet (fetched on the next run)"))
            continue
        if (app.get("details") or {}).get("is_free"):
            skipped.append((label, "free-to-play: review multiples do not apply"))
            continue
        ts = datetime.strptime(g["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        bc = baseline_curve(settings, review_curve(app))
        R, B = at_time(bc, ts)
        if R is None:
            skipped.append((label, "no review history"))
            continue
        rel = parse_release(app)
        age = (ts - rel.timestamp()) / (30.44 * 86400) if rel else 12
        x = feature_vector(R, list_price_usd(app), review_score(app), age)
        row = {"appid": g["appid"], "game": g.get("game") or app.get("name"), "date": g["date"],
               "metric": g["metric"], "scope": g["scope"], "actual": g["value"],
               "reviews_at_date": R, "x": x,
               "prior": (B / R) if R else 30.0, "source_url": g.get("source_url", ""),
               "notes": g.get("notes", "")}
        if R < m["min_reviews_for_training_row"]:
            skipped.append((label, f"only {R:.0f} reviews at that date"))
            continue
        too_early = age < m.get("min_months_after_release_for_training", 1.0)
        row["early"] = too_early
        w = weights.get(g["scope"], 0.0)
        if g["metric"] in m["metrics_used_for_fitting"] and w > 0 and not too_early:
            row["w"] = w
            row["resid"] = math.log(g["value"]) - math.log(R) - math.log(row["prior"])
            used.append(row)
        elif g["metric"] in ("steam_units", "units", "players"):
            reference.append(row)
        else:
            skipped.append((label, f"metric '{g['metric']}' / scope '{g['scope']}' not used for units"))
    return used, reference, skipped


def _stats(errs):
    if not errs:
        return None
    a = np.abs(np.array(errs))
    pct = np.exp(a) - 1
    return {"n": len(errs),
            "within_30": float(np.mean(pct <= 0.30)),
            "within_50": float(np.mean(pct <= 0.50)),
            "median_abs_pct_error": float(np.median(pct)),
            "bias_pct": float(math.exp(float(np.median(errs))) - 1),
            "rms_log_error": float(math.sqrt(float(np.mean(np.square(errs)))))}


def backtest(settings, used, reference):
    """Leave-one-game-out: each game's milestones are predicted by a model
    that never saw that game."""
    ridge = settings["model"]["ridge_strength"]
    games = sorted({r["appid"] for r in used})
    out_rows, model_err, base_err = [], [], []
    for gid in games:
        train = [r for r in used if r["appid"] != gid]
        beta = fit(train, ridge)
        for r in [r for r in used if r["appid"] == gid]:
            pred = r["reviews_at_date"] * r["prior"] * math.exp(float(r["x"] @ beta))
            base = r["reviews_at_date"] * r["prior"]
            e_m = math.log(pred) - math.log(r["actual"])
            e_b = math.log(base) - math.log(r["actual"])
            model_err.append(e_m)
            base_err.append(e_b)
            out_rows.append(_bt_row(r, pred, base, True))
    beta_all = fit(used, ridge)
    for r in reference:
        pred = r["reviews_at_date"] * r["prior"] * math.exp(float(r["x"] @ beta_all))
        out_rows.append(_bt_row(r, pred, r["reviews_at_date"] * r["prior"], False))
    return {"model": _stats(model_err), "baseline": _stats(base_err), "rows": out_rows}


def _bt_row(r, pred, base, counted):
    return {"appid": r["appid"], "game": r["game"], "date": r["date"], "metric": r["metric"],
            "scope": r["scope"], "actual": r["actual"], "reviews_at_date": round(r["reviews_at_date"]),
            "predicted": pred, "baseline": base,
            "error_pct": pred / r["actual"] - 1, "baseline_error_pct": base / r["actual"] - 1,
            "counted": counted, "early": bool(r.get("early")), "source_url": r["source_url"], "notes": r["notes"]}


def build(settings, gt, apps):
    used, reference, skipped = training_rows(settings, gt, apps)
    m = settings["model"]
    beta = fit(used, m["ridge_strength"])
    bt = backtest(settings, used, reference)
    if bt["model"] and bt["model"]["n"] >= m["min_rows_to_use_backtest_sd"]:
        sd, sd_src = bt["model"]["rms_log_error"], "backtest"
    else:
        sd, sd_src = m["default_log_error_sd"], "default assumption (not enough ground truth yet)"
    if used:
        X = np.array([r["x"] for r in used])
        lo, hi = X.min(axis=0), X.max(axis=0)
    else:
        lo = hi = np.zeros(len(FEATURES))
    return {"beta": beta, "x_lo": lo, "x_hi": hi, "sd": sd, "sd_source": sd_src, "n_used": len(used),
            "n_games": len({r["appid"] for r in used}), "backtest": bt,
            "skipped": [{"row": a, "reason": b} for a, b in skipped],
            "coefficients": {f: float(b) for f, b in zip(FEATURES, beta)}}


def predict_multiple(settings, model, app, at_ts=None):
    """Returns (multiple, effective prior multiple, adjustment factor)."""
    at_ts = at_ts or datetime.now(timezone.utc).timestamp()
    rel = parse_release(app)
    age = (at_ts - rel.timestamp()) / (30.44 * 86400) if rel else 12
    R = (app.get("summary") or {}).get("total_reviews") or 0
    bc = baseline_curve(settings, review_curve(app))
    if bc and bc[-1][1] > 0:
        prior = bc[-1][2] / bc[-1][1]
    else:
        prior = prior_multiple(settings, datetime.now(timezone.utc).year)
    x = feature_vector(R, list_price_usd(app), review_score(app), age)
    # never extrapolate the fitted adjustments beyond the ground truth's range
    x = np.clip(x, model["x_lo"], model["x_hi"])
    adj = math.exp(float(x @ model["beta"]))
    return prior * adj, prior, adj

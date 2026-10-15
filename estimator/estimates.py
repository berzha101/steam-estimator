"""Turns the fitted model into per-game units, revenue and quarterly figures."""
import math
from collections import defaultdict
from datetime import datetime, timezone

from . import model as M

DAY = 86400.0
MONTH = 30.44 * DAY


def regional_factor(settings, app, fx):
    rp = settings["regional_pricing"]
    counts = app.get("lang_counts")
    total = (app.get("summary") or {}).get("total_reviews") or 0
    us = M.list_price_usd(app)
    if not counts or not total or not us:
        return rp["other_languages_factor"], False, {}
    prices = app.get("regional_prices") or {}
    lang_map, fixed, vat = rp["language_to_country"], rp["fixed_factor_languages"], rp["vat_rates"]
    factor, shares, covered = 0.0, {}, 0.0
    for lang, n in counts.items():
        share = n / total
        if share <= 0:
            continue
        shares[lang] = share
        covered += share
        if lang in lang_map:
            cc = lang_map[lang]
            if cc == "us":
                ratio = 1.0
            else:
                p = prices.get(cc)
                usd_per = fx.get((p or {}).get("currency")) if p else None
                if p and p.get("initial") and usd_per:
                    ex_vat = p["initial"] / 100.0 * usd_per / (1 + vat.get(cc, 0.0))
                    ratio = min(max(ex_vat / us, 0.1), 1.5)
                else:
                    ratio = rp["other_languages_factor"]
        elif lang in fixed:
            ratio = fixed[lang]
        else:
            ratio = rp["other_languages_factor"]
        factor += share * ratio
    other = max(0.0, 1.0 - covered)
    if other:
        shares["other"] = other
        factor += other * rp["other_languages_factor"]
    return factor, True, shares


def _discount_for_age(settings, age_m):
    for b in settings["revenue"]["realized_discount_by_age_months"]:
        if b["from_month"] <= age_m < b["to_month"]:
            return b["discount"]
    return 0.0


def _observed_monthly_prices(snaps):
    """month -> (avg paid price, avg list price) from the crawler's own daily snapshots."""
    acc = defaultdict(list)
    for s in snaps:
        try:
            paid, lst = float(s["price_usd"]), float(s["list_price_usd"])
        except (ValueError, TypeError, KeyError):
            continue
        acc[s["date"][:7]].append((paid, lst))
    return {m: (sum(p for p, _ in v) / len(v), sum(l for _, l in v) / len(v)) for m, v in acc.items()}


def steam_cut(cum_gross, settings):
    """Valve's tiered cut on lifetime gross; returns total cut."""
    cut, prev = 0.0, 0.0
    for t in settings["revenue"]["steam_cut_tiers_usd"]:
        upper = float(t["up_to"])
        portion = max(0.0, min(cum_gross, upper) - prev)
        cut += portion * t["cut"]
        prev = upper
        if cum_gross <= upper:
            break
    return cut


def quarter_of(ts):
    d = datetime.fromtimestamp(ts, timezone.utc)
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


def game_estimate(settings, model, app, fx, snaps):
    summ = app.get("summary") or {}
    det = app.get("details") or {}
    R = summ.get("total_reviews") or 0
    rel = M.parse_release(app)
    out = {"appid": app["appid"], "name": app.get("name") or det.get("name") or str(app["appid"]),
           "header_image": det.get("header_image"), "genres": det.get("genres") or [],
           "developers": det.get("developers") or [], "publishers": det.get("publishers") or [],
           "release": rel.strftime("%Y-%m-%d") if rel else None,
           "is_free": bool(det.get("is_free")), "price": M.list_price_usd(app),
           "current_discount": (det.get("price_overview") or {}).get("discount_percent", 0),
           "reviews": R, "positive_pct": M.review_score(app),
           "ccu": app.get("ccu"), "ccu_max_seen": app.get("ccu_max_seen"),
           "last_updated": app.get("summary_fetched_at")}
    recent = (app.get("history") or {}).get("recent") or []
    curve = M.review_curve(app)
    hist_total = sum((u or 0) + (d or 0) for _, u, d in (app.get("history") or {}).get("rollups") or [])
    scale = (R / hist_total) if hist_total else 1.0
    out["reviews_30d"] = round(sum((u or 0) + (d or 0) for _, u, d in recent) * scale) if recent else None

    if not summ:
        out["status"] = "waiting for first crawl"
        return out
    if out["is_free"]:
        out["status"] = "free-to-play: reviews and players only (review multiples do not measure F2P)"
        return out
    if R < settings["model"]["min_reviews_to_estimate"]:
        out["status"] = f"fewer than {settings['model']['min_reviews_to_estimate']} reviews: too few to estimate"
        return out

    mult, prior = M.predict_multiple(settings, model, app)
    sd = math.sqrt(model["sd"] ** 2 + 1.0 / max(R, 1))
    z = settings["model"]["interval_z"]
    units = R * mult
    out.update({"status": "ok", "multiple": mult, "prior_multiple": prior,
                "units": units, "units_lo": units * math.exp(-z * sd), "units_hi": units * math.exp(z * sd),
                "units_30d": out["reviews_30d"] * mult if out["reviews_30d"] is not None else None})

    rf, measured, shares = regional_factor(settings, app, fx)
    out["regional_factor"], out["regional_measured"], out["lang_mix"] = rf, measured, shares
    price_now = out["price"]
    if price_now is None:
        out["status"] = "ok (no USD price on Steam right now, so revenue is not estimated)"
        return out

    observed = _observed_monthly_prices(snaps)
    refund = settings["revenue"]["refund_rate"]
    series, quarterly = [], defaultdict(lambda: [0.0, 0.0])
    cum_gross, prev_units = 0.0, 0.0
    for ts, cum_rev in curve[1:]:
        cum_units = cum_rev * mult
        d_units = max(cum_units - prev_units, 0.0)
        prev_units = cum_units
        mid = ts - MONTH / 2
        month = datetime.fromtimestamp(mid, timezone.utc).strftime("%Y-%m")
        if month in observed:
            paid = observed[month][0]
        else:
            age_m = (mid - rel.timestamp()) / MONTH if rel else 24
            paid = price_now * (1 - _discount_for_age(settings, age_m))
        gross = d_units * paid * rf * (1 - refund)
        cum_gross += gross
        q = quarter_of(mid)
        quarterly[q][0] += d_units
        quarterly[q][1] += gross
        series.append([datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d"),
                       round(cum_units), round(cum_gross)])
    # history may lag the current total slightly; top up to the current estimate
    if series and units > prev_units:
        gap = units - prev_units
        g = gap * price_now * (1 - _discount_for_age(settings, 99)) * rf * (1 - refund)
        cum_gross += g
        q = quarter_of(datetime.now(timezone.utc).timestamp())
        quarterly[q][0] += gap
        quarterly[q][1] += g
    out["gross"] = cum_gross
    out["net"] = cum_gross - steam_cut(cum_gross, settings)
    out["gross_lo"] = cum_gross * math.exp(-z * sd)
    out["gross_hi"] = cum_gross * math.exp(z * sd)
    out["series"] = series
    out["quarterly"] = [[q, round(v[0]), round(v[1])] for q, v in sorted(quarterly.items())]
    return out

"""Writes the dashboard (docs/index.html, one self-contained file) and CSV exports."""
import json
import os

from . import storage as st

TEMPLATE = os.path.join(os.path.dirname(__file__), "dashboard.html")


def _r(v, nd=0):
    return "" if v is None else round(v, nd) if nd else round(v)


def render(settings, model, games, run_info):
    out_dir = os.path.join(st.DOCS, "data")
    os.makedirs(out_dir, exist_ok=True)

    st.write_csv(os.path.join(out_dir, "estimates.csv"), [{
        "appid": g["appid"], "game": g["name"], "group": g.get("group", ""), "release": g.get("release"),
        "price_usd": g.get("price"), "reviews": g.get("reviews"), "reviews_last_30d": g.get("reviews_30d"),
        "positive_pct": _r((g.get("positive_pct") or 0) * 100, 1) if g.get("positive_pct") is not None else "",
        "ccu_now": g.get("ccu"), "ccu_max_seen": g.get("ccu_max_seen"),
        "multiple": _r(g.get("multiple"), 1), "est_units": _r(g.get("units")),
        "est_units_low": _r(g.get("units_lo")), "est_units_high": _r(g.get("units_hi")),
        "est_units_last_30d": _r(g.get("units_30d")),
        "est_gross_usd": _r(g.get("gross")), "est_net_usd": _r(g.get("net")),
        "regional_factor": _r(g.get("regional_factor"), 3), "status": g.get("status")} for g in games],
        ["appid", "game", "group", "release", "price_usd", "reviews", "reviews_last_30d", "positive_pct",
         "ccu_now", "ccu_max_seen", "multiple", "est_units", "est_units_low", "est_units_high",
         "est_units_last_30d", "est_gross_usd", "est_net_usd", "regional_factor", "status"])

    q_rows = []
    for g in games:
        for q, units, gross in g.get("quarterly") or []:
            q_rows.append({"appid": g["appid"], "game": g["name"], "group": g.get("group", ""),
                           "quarter": q, "est_units": units, "est_gross_usd": gross})
    st.write_csv(os.path.join(out_dir, "quarterly.csv"), q_rows,
                 ["appid", "game", "group", "quarter", "est_units", "est_gross_usd"])

    bt = model["backtest"]
    st.write_csv(os.path.join(out_dir, "backtest.csv"), [{
        **{k: r[k] for k in ("appid", "game", "date", "metric", "scope", "reviews_at_date", "source_url", "notes")},
        "actual": _r(r["actual"]), "predicted": _r(r["predicted"]), "baseline_prediction": _r(r["baseline"]),
        "error_pct": _r(r["error_pct"] * 100, 1), "baseline_error_pct": _r(r["baseline_error_pct"] * 100, 1),
        "counted_in_accuracy": "yes" if r["counted"] else "no (reference: different scope or metric)"}
        for r in bt["rows"]],
        ["appid", "game", "date", "metric", "scope", "actual", "reviews_at_date", "predicted",
         "baseline_prediction", "error_pct", "baseline_error_pct", "counted_in_accuracy", "source_url", "notes"])

    payload = {
        "run": run_info,
        "model": {k: model[k] for k in ("sd", "sd_source", "n_used", "n_games", "coefficients", "skipped")},
        "backtest": bt,
        "priors": {k: v for k, v in settings["prior_sales_per_review_by_review_year"].items() if k.isdigit()},
        "assumptions": {"refund_rate": settings["revenue"]["refund_rate"],
                        "discounts": settings["revenue"]["realized_discount_by_age_months"],
                        "interval_z": settings["model"]["interval_z"]},
        "games": games,
    }
    data = json.dumps(payload, default=lambda o: None, separators=(",", ":")).replace("</", "<\\/")
    with open(TEMPLATE, encoding="utf-8") as f:
        html = f.read()
    with open(os.path.join(os.path.dirname(__file__), "vendor", "chart.umd.js"), encoding="utf-8") as f:
        chartjs = f.read().replace("</script", "<\\/script")
    # chart library is bundled so the page also works as a downloaded file
    html = html.replace("__CHARTJS__", chartjs, 1).replace("__DATA__", data, 1)
    with open(os.path.join(st.DOCS, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    # GitHub Pages: serve files as-is
    open(os.path.join(st.DOCS, ".nojekyll"), "w").close()

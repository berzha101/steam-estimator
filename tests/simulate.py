"""Offline end-to-end test: fake Steam responses, real pipeline.

Run from a COPY of the project (it writes data/ and docs/):
    python tests/simulate.py
"""
import math
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import run  # noqa: E402
from estimator import storage as st  # noqa: E402

random.seed(7)
NOW = time.time()
MONTH = 30.44 * 86400


def make_game(appid, name, release_ts, price, total, free=False, weekly=False):
    months = max(1, int((NOW - release_ts) / MONTH))
    # launch-heavy decay curve
    w = [math.exp(-i / 6) + 0.05 for i in range(months)]
    s = sum(w)
    counts = [max(0, int(total * x / s)) for x in w]
    rollups = [{"date": int(release_ts + i * MONTH), "recommendations_up": int(c * .9),
                "recommendations_down": c - int(c * .9)} for i, c in enumerate(counts)]
    return {"appid": appid, "name": name, "release": release_ts, "price": price, "total": total,
            "free": free, "rollups": rollups}


GAMES = {g["appid"]: g for g in [
    make_game(1001, "Alpha Tactics", NOW - 5 * 365 * 86400, 24.99, 42000),
    make_game(1002, "Beta Farm", NOW - 3 * 365 * 86400, 14.99, 9000),
    make_game(1003, "Gamma Shooter", NOW - 400 * 86400, 29.99, 150000),
    make_game(1004, "Delta Free Arena", NOW - 900 * 86400, 0, 80000, free=True),
    make_game(1005, "Epsilon Tiny", NOW - 60 * 86400, 9.99, 6),
    make_game(1006, "Zeta Roguelike", NOW - 2 * 365 * 86400, 19.99, 22000),
]}

CUR = {"us": ("USD", 1), "cn": ("CNY", 3.5), "jp": ("JPY", 110), "kr": ("KRW", 1100),
       "de": ("EUR", 0.95), "fr": ("EUR", 0.95), "es": ("EUR", 0.95), "br": ("BRL", 2.6), "pl": ("PLN", 3.4)}
LANG_SHARE = {"english": .42, "schinese": .25, "russian": .08, "german": .05, "french": .03,
              "spanish": .02, "brazilian": .04, "polish": .02, "japanese": .02, "koreana": .02}


def transport(url, params):
    if "appids" in params:
        appid = int(params["appids"])
    elif "appid" in params:
        appid = int(params["appid"])
    else:
        appid = int(url.rstrip("/").split("/")[-1])
    g = GAMES.get(appid)
    if g is None:
        return {str(appid): {"success": False}}
    if "appdetails" in url:
        cc = params.get("cc", "us")
        cur, k = CUR[cc]
        d = {"type": "game", "name": g["name"], "is_free": g["free"],
             "release_date": {"coming_soon": False,
                              "date": time.strftime("%d %b, %Y", time.gmtime(g["release"]))},
             "genres": [{"description": "Indie"}], "developers": ["Dev " + g["name"]],
             "publishers": ["Pub"], "header_image": ""}
        if not g["free"]:
            d["price_overview"] = {"currency": cur, "initial": int(g["price"] * k * 100),
                                   "final": int(g["price"] * k * 100 * (0.8 if appid == 1003 else 1)),
                                   "discount_percent": 20 if appid == 1003 else 0}
        return {str(appid): {"success": True, "data": d}}
    if "appreviewhistogram" in url:
        return {"success": 1, "results": {"rollup_type": "month", "rollups": g["rollups"],
                                          "recent": [{"date": int(NOW - i * 86400), "recommendations_up": max(1, g["total"] // 4000),
                                                      "recommendations_down": 0} for i in range(30)]}}
    if "appreviews" in url:
        lang = params.get("language")
        tot = g["total"] if lang == "all" else int(g["total"] * LANG_SHARE.get(lang, 0))
        return {"success": 1, "query_summary": {"total_reviews": tot, "total_positive": int(tot * .88)}}
    if "GetNumberOfCurrentPlayers" in url:
        return {"response": {"player_count": g["total"] // 20, "result": 1}}
    return None


def main():
    from estimator.steam import SteamClient
    settings = st.load_settings()
    client = SteamClient(settings, transport=transport)
    st.save_watchlist([{"appid": str(a), "name": "", "group": "Test Co" if a < 1004 else "", "added": st.today()}
                       for a in (1001, 1002, 1003, 1004, 1005)])
    # ground truth: true multiple 38x for 1001, 30x for 1006, 45x for 1002; one all-platform reference row
    def at(g, frac):
        return time.strftime("%Y-%m-%d", time.gmtime(g["release"] + frac * (NOW - g["release"])))
    gt = []
    for appid, mult in ((1001, 38), (1006, 30), (1002, 45), (1003, 33)):
        g = GAMES[appid]
        for frac in (0.3, 0.8):
            from estimator.model import review_curve, reviews_at
            # compute reviews at date using the same curve logic later; approximate here
            gt.append({"appid": appid, "game": g["name"], "date": at(g, frac), "metric": "steam_units",
                       "value": "", "scope": "steam_only", "includes_bundles": "no",
                       "source_url": "https://example.com", "notes": f"simulated {mult}x", "_mult": mult, "_frac": frac})
    gt.append({"appid": 1003, "game": "Gamma Shooter", "date": at(GAMES[1003], .9), "metric": "units",
               "value": "9000000", "scope": "all_platforms", "includes_bundles": "unknown",
               "source_url": "", "notes": "simulated multi-platform", "_mult": None})
    # first pass: crawl so curves exist, then fill simulated values from the curves
    os.environ["ADD_GAMES"] = "https://store.steampowered.com/app/1006/Zeta_Roguelike/"
    st.write_csv(os.path.join(st.CONFIG, "ground_truth.csv"), [], st.GT_COLS)
    run.main(client=client, fx_override=({"USD": 1, "CNY": .14, "JPY": .0067, "KRW": .00073,
                                          "EUR": 1.08, "BRL": .18, "PLN": .25}, "test rates"))
    from estimator.model import review_curve, reviews_at
    import datetime as dt
    for r in gt:
        if r["_mult"]:
            app = st.load_app(r["appid"])
            ts = dt.datetime.strptime(r["date"], "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp()
            r["value"] = str(int(reviews_at(review_curve(app), ts) * r["_mult"] * random.uniform(.85, 1.15)))
    st.write_csv(os.path.join(st.CONFIG, "ground_truth.csv"), gt, st.GT_COLS)
    os.environ["ADD_GAMES"] = ""
    os.environ["GITHUB_REPOSITORY"] = "berzha101/steam-estimator"
    run.main(client=SteamClient(settings, transport=transport),
             fx_override=({"USD": 1, "CNY": .14, "JPY": .0067, "KRW": .00073, "EUR": 1.08, "BRL": .18, "PLN": .25},
                          "test rates"))


if __name__ == "__main__":
    main()

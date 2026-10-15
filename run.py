"""One refresh: crawl Steam, re-fit the model, rebuild the dashboard.

Run by GitHub Actions on a schedule or when you press "Run workflow".
Inputs come from environment variables set by the workflow:
  ADD_GAMES     Steam links or app IDs to start tracking (comma separated)
  ADD_GROUP     optional group label for those games (e.g. a company name)
  FULL_REFRESH  "true" to re-pull language mix and regional prices for every game
  SKIP_CRAWL    "true" to rebuild the dashboard from saved data only
"""
import json
import os
import sys
import time
from collections import defaultdict

from estimator import storage as st
from estimator.crawl import Crawler
from estimator.estimates import game_estimate
from estimator.model import build
from estimator.site import render
from estimator.steam import fx_usd_per_unit


def env_true(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def main(client=None, fx_override=None):
    t0 = time.time()
    settings = st.load_settings()
    watch = st.load_watchlist()
    known = {int(w["appid"]) for w in watch if w.get("appid", "").isdigit()}

    added = []
    for appid in st.parse_appids(os.environ.get("ADD_GAMES", "")):
        if appid not in known:
            watch.append({"appid": str(appid), "name": "", "group": os.environ.get("ADD_GROUP", "").strip(),
                          "added": st.today()})
            known.add(appid)
            added.append(appid)
    if added:
        print(f"Adding {len(added)} game(s): {added}")

    gt = st.load_ground_truth()
    gt_ids = sorted({g["appid"] for g in gt})
    watch_ids = [int(w["appid"]) for w in watch if w.get("appid", "").isdigit()]

    crawl_info = {"store_calls": 0, "notes": []}
    if not env_true("SKIP_CRAWL"):
        crawl_info = Crawler(settings, client=client).run(watch_ids, gt_ids, env_true("FULL_REFRESH"))

    apps = {a: st.load_app(a) for a in set(watch_ids) | set(gt_ids)}
    for w in watch:
        a = apps.get(int(w["appid"])) if w.get("appid", "").isdigit() else None
        if a and a.get("name"):
            w["name"] = a["name"]
    st.save_watchlist(watch)

    mdl = build(settings, gt, apps)
    fx, fx_src = fx_override if fx_override else fx_usd_per_unit(
        settings["regional_pricing"]["fallback_usd_per_unit"])

    snaps = defaultdict(list)
    for s in st.read_csv(os.path.join(st.DATA, "snapshots.csv")):
        snaps[s["appid"]].append(s)

    groups = {int(w["appid"]): w.get("group", "") for w in watch if w.get("appid", "").isdigit()}
    games = []
    for appid in watch_ids:
        g = game_estimate(settings, mdl, apps[appid], fx, snaps.get(str(appid), []))
        g["group"] = groups.get(appid, "")
        g["ground_truth"] = [{"date": r["date"], "value": r["value"], "metric": r["metric"],
                              "scope": r["scope"], "source_url": r.get("source_url", "")}
                             for r in gt if r["appid"] == appid]
        games.append(g)

    run_info = {"generated_at": st.now_utc().isoformat(timespec="seconds"),
                "repo": os.environ.get("GITHUB_REPOSITORY", ""),
                "store_calls": crawl_info["store_calls"], "notes": crawl_info["notes"],
                "seconds": round(time.time() - t0), "fx_source": fx_src, "added": added}
    render(settings, mdl, games, run_info)
    with open(os.path.join(st.DATA, "last_run.json"), "w", encoding="utf-8") as f:
        json.dump(run_info, f, indent=1)
    print(f"Done: {len(games)} games, {mdl['n_used']} ground-truth rows used, "
          f"{crawl_info['store_calls']} store calls, {run_info['seconds']}s")
    for n in crawl_info["notes"]:
        print("NOTE:", n)


if __name__ == "__main__":
    sys.exit(main())

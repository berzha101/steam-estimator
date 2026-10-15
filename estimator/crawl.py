"""Pulls fresh Steam data for every tracked game and ground-truth game."""
from datetime import datetime, timedelta, timezone

from . import storage as st
from .steam import SteamClient, StoreBudgetExhausted

REVIEW_LANGUAGES = ["english", "schinese", "russian", "japanese", "koreana",
                    "german", "french", "spanish", "latam", "brazilian",
                    "polish", "turkish", "tchinese"]


def _age_days(iso):
    if not iso:
        return 1e9
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return 1e9
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (st.now_utc() - t).total_seconds() / 86400


def _trim_details(d):
    keep = {k: d.get(k) for k in ("name", "type", "is_free", "release_date",
                                   "price_overview", "developers", "publishers",
                                   "header_image")}
    keep["genres"] = [g.get("description") for g in d.get("genres") or []]
    keep["recommendations"] = (d.get("recommendations") or {}).get("total")
    return keep


class Crawler:
    def __init__(self, settings, client=None, log=print):
        self.s = settings
        self.client = client or SteamClient(settings)
        self.log = log
        self.stopped = False
        self.notes = []

    def _try(self, fn, *a):
        if self.stopped:
            return None
        try:
            return fn(*a)
        except StoreBudgetExhausted:
            self.stopped = True
            self.notes.append("Steam rate limit or per-run call budget reached; "
                              "remaining games keep their previous data and refresh next run.")
            self.log("  ! store budget exhausted / throttled - stopping store calls this run")
            return None

    # --- daily essentials ---
    def refresh_core(self, appid, include_ccu=True):
        app = st.load_app(appid)
        now = st.now_utc().isoformat()
        d = self._try(self.client.app_details, appid, "us")
        if d:
            app["details"] = _trim_details(d)
            app["name"] = d.get("name") or app.get("name")
            app["details_fetched_at"] = now
        s = self._try(self.client.review_summary, appid, "all")
        if s:
            app["summary"] = s
            app["summary_fetched_at"] = now
        hist_every = self.s["crawl"]["history_refresh_every_days"]
        if _age_days(app.get("history_fetched_at")) >= hist_every - 0.01:
            h = self._try(self.client.review_history, appid)
            if h:
                app["history"] = {"rollup_type": h.get("rollup_type"),
                                  "rollups": [[r["date"], r["recommendations_up"], r["recommendations_down"]]
                                              for r in h.get("rollups") or []],
                                  "recent": [[r["date"], r["recommendations_up"], r["recommendations_down"]]
                                             for r in h.get("recent") or []]}
                app["history_fetched_at"] = now
        if include_ccu:
            try:
                ccu = self.client.current_players(appid)
            except Exception:
                ccu = None
            if ccu is not None:
                app["ccu"] = ccu
                app["ccu_at"] = now
                if ccu >= (app.get("ccu_max_seen") or 0):
                    app["ccu_max_seen"] = ccu
                    app["ccu_max_seen_at"] = now
        st.save_app(app)
        return app

    # --- weekly extras: language mix and regional prices ---
    def refresh_extras(self, appid):
        app = st.load_app(appid)
        now = st.now_utc().isoformat()
        counts = {}
        for lang in REVIEW_LANGUAGES:
            q = self._try(self.client.review_summary, appid, lang)
            if q is None:
                if self.stopped:
                    break
                continue
            counts[lang] = q.get("total_reviews", 0)
        if counts and not self.stopped:
            app["lang_counts"] = counts
            app["lang_fetched_at"] = now
        countries = sorted(set(self.s["regional_pricing"]["language_to_country"].values()) - {"us"})
        prices = {}
        for cc in countries:
            d = self._try(self.client.app_details, appid, cc)
            if self.stopped:
                break
            po = (d or {}).get("price_overview")
            if po:
                prices[cc] = {"currency": po.get("currency"), "initial": po.get("initial"),
                              "final": po.get("final")}
        if prices and not self.stopped:
            app["regional_prices"] = prices
            app["prices_fetched_at"] = now
        st.save_app(app)
        return app

    def run(self, watch_ids, gt_ids, full_refresh=False):
        today = st.today()
        snaps = []
        # 1) daily essentials for tracked games, stalest first
        order = sorted(watch_ids, key=lambda a: -_age_days(st.load_app(a).get("summary_fetched_at")))
        for i, appid in enumerate(order, 1):
            if self.stopped:
                break
            self.log(f"[{i}/{len(order)}] app {appid}")
            app = self.refresh_core(appid)
            snaps.append(snapshot_row(app, today))
        # 2) ground-truth-only games: weekly is enough
        gt_every = self.s["crawl"]["ground_truth_refresh_every_days"]
        for appid in gt_ids:
            if self.stopped:
                break
            if appid in watch_ids:
                continue
            if full_refresh or _age_days(st.load_app(appid).get("summary_fetched_at")) >= gt_every:
                self.log(f"[ground truth] app {appid}")
                self.refresh_core(appid, include_ccu=False)
        # 3) weekly extras, stalest first, until the budget runs out
        every = self.s["regional_pricing"]["refresh_every_days"]
        due = [a for a in watch_ids
               if full_refresh or _age_days(st.load_app(a).get("lang_fetched_at")) >= every]
        due.sort(key=lambda a: -_age_days(st.load_app(a).get("lang_fetched_at")))
        for appid in due:
            if self.stopped:
                break
            app = st.load_app(appid)
            if (app.get("details") or {}).get("is_free"):
                continue
            self.log(f"[language mix + regional prices] app {appid}")
            self.refresh_extras(appid)
        st.append_csv(f"{st.DATA}/snapshots.csv", [s for s in snaps if s], st.SNAP_COLS)
        return {"store_calls": self.client.store_calls, "notes": self.notes}


def snapshot_row(app, today):
    s = app.get("summary") or {}
    if not s or not str(app.get("summary_fetched_at", "")).startswith(today):
        return None  # only record what was actually fetched today
    d = app.get("details") or {}
    po = d.get("price_overview") or {}
    usd = po.get("currency") == "USD"
    return {"date": today, "appid": app["appid"],
            "total_reviews": s.get("total_reviews"), "total_positive": s.get("total_positive"),
            "ccu": app.get("ccu", ""),
            "price_usd": (po.get("final", 0) / 100) if usd else "",
            "list_price_usd": (po.get("initial", 0) / 100) if usd else "",
            "discount_pct": po.get("discount_percent", 0) if po else "",
            "is_free": int(bool(d.get("is_free")))}

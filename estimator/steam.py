"""Polite client for Steam's public JSON endpoints (no API key needed).

Steam throttles the store host at roughly 200 requests per 5 minutes per IP,
so every store call is spaced out, 429s back off, and the run stops calling
the store (and falls back to cached data) if Steam keeps refusing.
"""
import time
import requests

STORE = "https://store.steampowered.com"
API = "https://api.steampowered.com"


class StoreBudgetExhausted(Exception):
    pass


class SteamClient:
    def __init__(self, settings, transport=None):
        c = settings["crawl"]
        self.store_gap = c["store_seconds_between_calls"]
        self.api_gap = c["api_seconds_between_calls"]
        self.max_store_calls = c["max_store_calls_per_run"]
        self.store_calls = 0
        self.consecutive_429 = 0
        self._last = {"store": 0.0, "api": 0.0}
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "steam-estimator (personal research; low request rate)"
        # transport lets tests swap in canned responses
        self.transport = transport

    # ---------- low level ----------
    def _wait(self, host):
        gap = self.store_gap if host == "store" else self.api_gap
        dt = time.time() - self._last[host]
        if dt < gap:
            time.sleep(gap - dt)
        self._last[host] = time.time()

    def _get(self, host, url, params):
        if host == "store":
            if self.store_calls >= self.max_store_calls:
                raise StoreBudgetExhausted()
            self.store_calls += 1
        if self.transport:
            return self.transport(url, params)
        for attempt in range(4):
            self._wait(host)
            try:
                r = self.session.get(url, params=params, timeout=30)
            except requests.RequestException:
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code in (429, 403):
                self.consecutive_429 += 1
                if self.consecutive_429 >= 6:
                    raise StoreBudgetExhausted()
                time.sleep(60 * (attempt + 1))
                continue
            self.consecutive_429 = 0
            if r.status_code != 200:
                return None
            try:
                return r.json()
            except ValueError:
                return None
        return None

    # ---------- endpoints ----------
    def app_details(self, appid, cc="us"):
        j = self._get("store", f"{STORE}/api/appdetails",
                      {"appids": appid, "cc": cc, "l": "english"})
        if not j:
            return None
        node = j.get(str(appid)) or {}
        return node.get("data") if node.get("success") else None

    def review_summary(self, appid, language="all"):
        j = self._get("store", f"{STORE}/appreviews/{appid}",
                      {"json": 1, "language": language, "purchase_type": "all",
                       "filter": "recent", "num_per_page": 0, "cursor": "*"})
        if not j or not j.get("success"):
            return None
        return j.get("query_summary")

    def review_history(self, appid):
        j = self._get("store", f"{STORE}/appreviewhistogram/{appid}",
                      {"l": "english", "review_score_preference": 0})
        if not j or not j.get("success"):
            return None
        return j.get("results")

    def current_players(self, appid):
        j = self._get("api", f"{API}/ISteamUserStats/GetNumberOfCurrentPlayers/v1/",
                      {"appid": appid})
        try:
            resp = j["response"]
            return resp.get("player_count") if resp.get("result") == 1 else None
        except (TypeError, KeyError):
            return None


def fx_usd_per_unit(fallback):
    """USD value of one unit of each currency, from the ECB via Frankfurter."""
    for url in ("https://api.frankfurter.dev/v1/latest?from=USD",
                "https://api.frankfurter.app/latest?from=USD"):
        try:
            r = requests.get(url, timeout=20)
            if r.status_code == 200:
                rates = r.json()["rates"]  # units of currency per 1 USD
                out = {k: 1.0 / v for k, v in rates.items() if v}
                out["USD"] = 1.0
                return out, "ECB via Frankfurter"
        except Exception:
            pass
    return dict(fallback), "fallback rates in settings.json"

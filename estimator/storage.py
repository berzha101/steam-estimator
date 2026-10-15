"""Plain-file storage: CSVs and JSON that open in Excel or any text editor."""
import csv
import json
import os
import re
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config")
DATA = os.path.join(ROOT, "data")
APPS = os.path.join(DATA, "apps")
DOCS = os.path.join(ROOT, "docs")

WATCHLIST_COLS = ["appid", "name", "group", "added"]
GT_COLS = ["appid", "game", "date", "metric", "value", "scope",
           "includes_bundles", "source_url", "notes"]
SNAP_COLS = ["date", "appid", "total_reviews", "total_positive", "ccu",
             "price_usd", "list_price_usd", "discount_pct", "is_free"]


def now_utc():
    return datetime.now(timezone.utc)


def today():
    return now_utc().strftime("%Y-%m-%d")


def load_settings():
    with open(os.path.join(CONFIG, "settings.json"), encoding="utf-8") as f:
        return json.load(f)


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [{k.strip(): (v or "").strip() for k, v in row.items() if k}
                for row in csv.DictReader(f)]


def write_csv(path, rows, cols):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def append_csv(path, rows, cols):
    exists = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if not exists:
            w.writeheader()
        for r in rows:
            w.writerow(r)


def load_watchlist():
    return read_csv(os.path.join(CONFIG, "watchlist.csv"))


def save_watchlist(rows):
    write_csv(os.path.join(CONFIG, "watchlist.csv"), rows, WATCHLIST_COLS)


def load_ground_truth():
    rows = []
    for r in read_csv(os.path.join(CONFIG, "ground_truth.csv")):
        try:
            r["appid"] = int(re.sub(r"\D", "", r.get("appid", "")))
            r["value"] = float(str(r.get("value", "")).replace(",", ""))
            datetime.strptime(r["date"], "%Y-%m-%d")
        except (ValueError, KeyError):
            continue  # skip incomplete rows rather than failing the run
        r["metric"] = (r.get("metric") or "units").lower()
        r["scope"] = (r.get("scope") or "steam_only").lower()
        rows.append(r)
    return rows


def app_path(appid):
    return os.path.join(APPS, f"{int(appid)}.json")


def load_app(appid):
    p = app_path(appid)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {"appid": int(appid)}


def save_app(app):
    os.makedirs(APPS, exist_ok=True)
    with open(app_path(app["appid"]), "w", encoding="utf-8") as f:
        json.dump(app, f, separators=(",", ":"))


def parse_appids(text):
    """Accept app IDs, store URLs, or SteamDB URLs separated by commas/spaces/newlines."""
    ids = []
    for token in re.split(r"[,\s]+", text or ""):
        if not token:
            continue
        m = re.search(r"/app/(\d+)", token) or re.fullmatch(r"(\d{2,9})", token)
        if m:
            ids.append(int(m.group(1)))
    return list(dict.fromkeys(ids))

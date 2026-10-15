# Steam Estimator

Estimates Steam units sold and revenue for any game, in the style of VG Insights and Gamalytic: review counts × a sales-per-review multiple that is calibrated and backtested against publicly announced sales milestones.

It runs itself on GitHub's free servers every morning and publishes a dashboard web page. No coding needed to use it.

## Everyday use

| You want to… | Do this |
|---|---|
| See the numbers | Open the dashboard: `https://berzha101.github.io/steam-estimator/` |
| Refresh right now | **Actions** tab → **Refresh estimates** → **Run workflow** → green **Run workflow** button. Takes 5–30 min. |
| Add games | Same **Run workflow** form: paste Steam store links (comma separated) into the first box. Optional: a group label such as the company name. |
| Remove games / rename groups | Open `config/watchlist.csv` → pencil icon → edit → **Commit changes**. A refresh starts automatically. |
| Add a known sales milestone | Open `config/ground_truth.csv` → pencil icon → add a row → **Commit changes**. A refresh starts automatically. |
| Get the data into Excel | Dashboard buttons: `estimates.csv`, `quarterly.csv`, `backtest.csv`. |
| Change assumptions | `config/settings.json` (review multiples by year, discounts, refund rate, VAT, uncertainty band). |

### Ground-truth rows

One row per announcement:

```
appid,game,date,metric,value,scope,includes_bundles,source_url,notes
892970,Valheim,2021-03-02,steam_units,5000000,steam_only,no,https://...,PC-only at the time
```

- `metric`: `steam_units` or `units` (used to fit the model); `players` and `gross_revenue_usd` are shown for reference only.
- `scope`: `steam_only` (full weight), `pc_all_stores` (70% weight), `all_platforms` (shown in the backtest but not used to fit).
- `date`: when the milestone was reached or announced, `YYYY-MM-DD`. The app rebuilds the game's review count on that exact date from Steam's review history, so old announcements are just as useful as new ones.

## How the estimate works

1. **Crawl** (Steam's public JSON endpoints, no key needed, ~1 request per 1.6 seconds to stay under Steam's limits): store details and price, total reviews, monthly review history, current players; weekly: review counts by language and prices in 8 countries.
2. **Units** = total reviews × sales-per-review multiple. The multiple starts from published averages by release year and is adjusted by a ridge regression on your ground truth (review volume, price, review score, age). With little ground truth the adjustment shrinks to zero.
3. **Backtest**: leave-one-game-out. Every milestone is predicted by a model that never saw that game, and compared with the flat published multiple as a baseline. The uncertainty band comes from these errors once there are 15+ milestones.
4. **Revenue**: monthly units × price paid that month × regional pricing factor (review language mix × Steam country prices converted to USD ex-VAT) × (1 − refunds). Net = gross minus Steam's tiered cut.
5. **Dashboard**: `docs/index.html`, rebuilt every run, with CSV exports.

Free-to-play games show reviews and players only: review multiples don't measure F2P spending.

## Files

- `config/` — the three files you edit (watchlist, ground truth, settings)
- `data/` — raw Steam data per game and daily snapshots (grows into your own price and review history)
- `docs/` — the published dashboard and CSVs
- `estimator/`, `run.py` — the code
- `.github/workflows/refresh.yml` — the schedule and the Run workflow form

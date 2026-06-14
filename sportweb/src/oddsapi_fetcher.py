#!/usr/bin/env python3
"""
OddsAPI (the-odds-api.com) NBA odds fetcher.

Output schema matches sportweb/data/latest_odds.json so the rest of the
autobots_NBA pipeline can consume it identically:
    sportweb/data/oddsapi_latest.json   (always overwritten)
    sportweb/data/oddsapi_<YYYYMMDD_HHMM>.json   (timestamped snapshot)

Reads ODDS_API_KEY from sportweb/.env or env var.

Quota: each call costs (markets × regions) credits.
  markets=h2h,spreads,totals & regions=us → 3 credits/call.
  2 calls/day × 30 days = 180 credits/month (free tier = 500).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"
DATA_DIR = BASE_DIR / "data"
LATEST_FILE = DATA_DIR / "oddsapi_latest.json"

API_URL = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds/"
REGIONS = "us"
MARKETS = "h2h,spreads,totals"
ODDS_FORMAT = "decimal"

# Primary book preference (decreasing). Pinnacle gives sharpest prices,
# DraftKings/FanDuel are the largest US books with reliable lines.
BOOK_PRIORITY = ["pinnacle", "draftkings", "fanduel", "betmgm", "caesars"]


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def fetch_odds(api_key: str) -> list[dict]:
    params = urllib.parse.urlencode({
        "apiKey": api_key,
        "regions": REGIONS,
        "markets": MARKETS,
        "oddsFormat": ODDS_FORMAT,
    })
    url = f"{API_URL}?{params}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        remaining = resp.headers.get("x-requests-remaining")
        used = resp.headers.get("x-requests-used")
        body = resp.read().decode("utf-8")
        print(f"  quota: used={used} remaining={remaining}")
        return json.loads(body)


def _pick_book(bookmakers: list[dict]) -> dict | None:
    if not bookmakers:
        return None
    by_key = {b.get("key", ""): b for b in bookmakers}
    for k in BOOK_PRIORITY:
        if k in by_key:
            return by_key[k]
    return bookmakers[0]


def _extract_market(book: dict, market_key: str) -> dict | None:
    for m in book.get("markets", []) or []:
        if m.get("key") == market_key:
            return m
    return None


def transform_game(game: dict) -> dict | None:
    away = game.get("away_team", "")
    home = game.get("home_team", "")
    if not away or not home:
        return None

    commence = game.get("commence_time", "")
    try:
        dt = datetime.fromisoformat(commence.replace("Z", "+00:00"))
        # Convert UTC commence to TW local date (UTC+8) to match sportweb convention
        from datetime import timezone, timedelta
        tw = dt.astimezone(timezone(timedelta(hours=8)))
        game_date = tw.strftime("%Y%m%d")
        start_time = tw.isoformat(timespec="seconds")
    except Exception:
        game_date = ""
        start_time = commence

    book = _pick_book(game.get("bookmakers", []) or [])
    if book is None:
        return None

    moneyline = {"line": 0.0, "away": 0.0, "home": 0.0}
    spreads: list[dict] = []
    totals: list[dict] = []

    h2h = _extract_market(book, "h2h")
    if h2h:
        for o in h2h.get("outcomes", []):
            name = o.get("name", "")
            price = o.get("price")
            if name == away:
                moneyline["away"] = float(price or 0.0)
            elif name == home:
                moneyline["home"] = float(price or 0.0)

    spread_m = _extract_market(book, "spreads")
    if spread_m:
        away_o = next((o for o in spread_m.get("outcomes", []) if o.get("name") == away), None)
        home_o = next((o for o in spread_m.get("outcomes", []) if o.get("name") == home), None)
        if away_o and home_o and away_o.get("point") is not None:
            spreads.append({
                "line": float(away_o["point"]),  # signed from away perspective
                "away": float(away_o.get("price") or 0.0),
                "home": float(home_o.get("price") or 0.0),
            })

    totals_m = _extract_market(book, "totals")
    if totals_m:
        over_o = next((o for o in totals_m.get("outcomes", []) if o.get("name") == "Over"), None)
        under_o = next((o for o in totals_m.get("outcomes", []) if o.get("name") == "Under"), None)
        if over_o and under_o and over_o.get("point") is not None:
            totals.append({
                "line": float(over_o["point"]),
                "over": float(over_o.get("price") or 0.0),
                "under": float(under_o.get("price") or 0.0),
            })

    return {
        "game_id": game.get("id", ""),
        "away": away,
        "home": home,
        "start_time": start_time,
        "source_url": "",
        "game_date": game_date,
        "game_key": f"{game_date}|{away}|{home}",
        "bookmaker": book.get("key", ""),
        "moneyline": moneyline,
        "spreads": spreads,
        "totals": totals,
    }


def main() -> int:
    load_env_file(ENV_FILE)
    api_key = os.environ.get("ODDS_API_KEY", "").strip()
    if not api_key:
        print(f"❌ ODDS_API_KEY missing (looked in {ENV_FILE} and env)", file=sys.stderr)
        return 1

    fetched_at = datetime.now().isoformat(timespec="seconds")
    print(f"[{fetched_at}] OddsAPI NBA odds fetch starting...")

    try:
        raw_games = fetch_odds(api_key)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
        print(f"❌ HTTP {e.code}: {body[:200]}", file=sys.stderr)
        return 2
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"❌ network error: {e}", file=sys.stderr)
        return 3

    print(f"  raw games returned: {len(raw_games)}")

    games: list[dict] = []
    for g in raw_games:
        out = transform_game(g)
        if out:
            games.append(out)

    snapshot = {
        "fetched_at": fetched_at,
        "league": "NBA",
        "source_url": "https://api.the-odds-api.com/v4/sports/basketball_nba/odds/",
        "cf_passed": True,
        "source": "oddsapi",
        "book_priority": BOOK_PRIORITY,
        "games": games,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    body = json.dumps(snapshot, ensure_ascii=False, indent=2)
    LATEST_FILE.write_text(body, encoding="utf-8")
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    snap_file = DATA_DIR / f"oddsapi_{ts}.json"
    snap_file.write_text(body, encoding="utf-8")

    print(f"✅ saved {len(games)} games → {LATEST_FILE.name}")
    if games:
        sample = games[0]
        spread = sample["spreads"][0]["line"] if sample["spreads"] else "—"
        total = sample["totals"][0]["line"] if sample["totals"] else "—"
        print(f"  sample: {sample['away']} @ {sample['home']} | spread={spread} | total={total} | book={sample['bookmaker']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
NBA Game Predictor -- XGBoost model for Polymarket edge detection
================================================================
Collects NBA team stats, builds Elo ratings, trains XGBoost model,
compares predictions vs Polymarket odds, calculates Brier score.

Usage:
    python nba_predictor.py                    # Today's predictions
    python nba_predictor.py --train            # Train/retrain model
    python nba_predictor.py --backtest         # Backtest vs Polymarket
    python nba_predictor.py --edge             # Show edge opportunities
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import numpy as np

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

# ── Constants ──

STATE_DIR = Path(__file__).parent / "state"
MODEL_PATH = STATE_DIR / "nba_model.json"
CALIBRATION_PATH = STATE_DIR / "nba_calibration.json"
SPREAD_MODEL_PATH = STATE_DIR / "nba_spread_model.xgb"

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
GAMMA = "https://gamma-api.polymarket.com"

NBA_TEAMS = [
    "lakers", "celtics", "warriors", "nets", "knicks", "76ers", "bucks",
    "suns", "nuggets", "heat", "bulls", "mavericks", "clippers", "rockets",
    "grizzlies", "cavaliers", "thunder", "timberwolves", "kings", "pistons",
    "hawks", "hornets", "magic", "pacers", "raptors", "spurs", "jazz",
    "blazers", "pelicans", "wizards", "trail blazers",
]

# Mapping from common short names to ESPN display names
TEAM_ALIASES: dict[str, str] = {
    "lakers": "Los Angeles Lakers",
    "celtics": "Boston Celtics",
    "warriors": "Golden State Warriors",
    "nets": "Brooklyn Nets",
    "knicks": "New York Knicks",
    "76ers": "Philadelphia 76ers",
    "sixers": "Philadelphia 76ers",
    "bucks": "Milwaukee Bucks",
    "suns": "Phoenix Suns",
    "nuggets": "Denver Nuggets",
    "heat": "Miami Heat",
    "bulls": "Chicago Bulls",
    "mavericks": "Dallas Mavericks",
    "mavs": "Dallas Mavericks",
    "clippers": "LA Clippers",
    "rockets": "Houston Rockets",
    "grizzlies": "Memphis Grizzlies",
    "cavaliers": "Cleveland Cavaliers",
    "cavs": "Cleveland Cavaliers",
    "thunder": "Oklahoma City Thunder",
    "timberwolves": "Minnesota Timberwolves",
    "wolves": "Minnesota Timberwolves",
    "kings": "Sacramento Kings",
    "pistons": "Detroit Pistons",
    "hawks": "Atlanta Hawks",
    "hornets": "Charlotte Hornets",
    "magic": "Orlando Magic",
    "pacers": "Indiana Pacers",
    "raptors": "Toronto Raptors",
    "spurs": "San Antonio Spurs",
    "jazz": "Utah Jazz",
    "blazers": "Portland Trail Blazers",
    "trail blazers": "Portland Trail Blazers",
    "pelicans": "New Orleans Pelicans",
    "wizards": "Washington Wizards",
}

# Reverse: full name -> short alias (for Polymarket matching)
NAME_TO_ALIAS: dict[str, str] = {}
for _alias, _full in TEAM_ALIASES.items():
    if _full not in NAME_TO_ALIAS:
        NAME_TO_ALIAS[_full] = _alias


# ── Elo System ──

class EloSystem:
    """Track Elo ratings for all NBA teams."""

    K = 20
    HOME_ADV = 100

    def __init__(self):
        self.ratings: dict[str, float] = {}

    def _get(self, team: str) -> float:
        return self.ratings.setdefault(team, 1500.0)

    def expected(self, ra: float, rb: float) -> float:
        return 1.0 / (1.0 + 10.0 ** ((rb - ra) / 400.0))

    def update(self, winner: str, loser: str, home_team: str | None = None):
        ra = self._get(winner)
        rb = self._get(loser)
        # Apply home-court adjustment
        adj_a = self.HOME_ADV if home_team == winner else (-self.HOME_ADV if home_team == loser else 0)
        ea = self.expected(ra + adj_a, rb - adj_a)
        self.ratings[winner] = ra + self.K * (1.0 - ea)
        self.ratings[loser] = rb + self.K * (0.0 - (1.0 - ea))

    def predict(self, team_a: str, team_b: str, home_team: str | None = None) -> float:
        """Return win probability for team_a."""
        ra = self._get(team_a)
        rb = self._get(team_b)
        adj = self.HOME_ADV if home_team == team_a else (-self.HOME_ADV if home_team == team_b else 0)
        return self.expected(ra + adj, rb - adj)

    def to_dict(self) -> dict:
        return {"ratings": self.ratings}

    def from_dict(self, d: dict):
        self.ratings = d.get("ratings", {})


# ── Data Collection: ESPN ──

def _http() -> httpx.Client:
    return httpx.Client(timeout=15, follow_redirects=True)


def fetch_espn_scoreboard(date_str: str | None = None) -> list[dict]:
    """Fetch NBA games from ESPN for a specific date (or today if None).

    date_str format: YYYYMMDD (e.g. '20260418') — matches ESPN's `dates` query param.
    """
    try:
        with _http() as c:
            url = f"{ESPN_BASE}/scoreboard"
            if date_str:
                url = f"{url}?dates={date_str}"
            r = c.get(url)
            if r.status_code != 200:
                return []
            data = r.json()
    except Exception as exc:
        print(f"  [warn] ESPN scoreboard fetch failed: {exc}")
        return []

    games = []
    for event in data.get("events", []):
        comps = event.get("competitions", [{}])[0]
        teams = comps.get("competitors", [])
        if len(teams) < 2:
            continue
        home = next((t for t in teams if t.get("homeAway") == "home"), teams[0])
        away = next((t for t in teams if t.get("homeAway") == "away"), teams[1])
        games.append({
            "home": home["team"]["displayName"],
            "away": away["team"]["displayName"],
            "home_abbr": home["team"].get("abbreviation", ""),
            "away_abbr": away["team"].get("abbreviation", ""),
            "home_record": home.get("records", [{}])[0].get("summary", ""),
            "away_record": away.get("records", [{}])[0].get("summary", ""),
            "status": event.get("status", {}).get("type", {}).get("description", ""),
            "date": event.get("date", ""),
        })
    return games


def fetch_espn_scoreboard_range(days_ahead: int = 3) -> list[dict]:
    """Fetch today + next N days of NBA games. Dedup by home+away+date.

    days_ahead=0 => today only
    days_ahead=3 => today + 3 days ahead (4 days total)
    """
    all_games = []
    seen = set()
    for offset in range(0, days_ahead + 1):
        date = (datetime.now() + timedelta(days=offset)).strftime("%Y%m%d")
        games = fetch_espn_scoreboard(date_str=date if offset > 0 else None)
        for g in games:
            key = (g.get("home", ""), g.get("away", ""), g.get("date", "")[:10])
            if key in seen:
                continue
            seen.add(key)
            g["_fetched_for_date"] = date
            all_games.append(g)
    return all_games


def fetch_espn_standings() -> dict[str, dict]:
    """Fetch NBA standings from ESPN."""
    try:
        with _http() as c:
            r = c.get("https://site.api.espn.com/apis/v2/sports/basketball/nba/standings")
            if r.status_code != 200:
                return {}
            data = r.json()
    except Exception as exc:
        print(f"  [warn] ESPN standings fetch failed: {exc}")
        return {}

    teams: dict[str, dict] = {}
    for group in data.get("children", []):
        for entry in group.get("standings", {}).get("entries", []):
            name = entry.get("team", {}).get("displayName", "")
            abbr = entry.get("team", {}).get("abbreviation", "")
            stats_map: dict[str, float] = {}
            for s in entry.get("stats", []):
                stats_map[s.get("abbreviation", "")] = s.get("value", 0)
            wins = int(stats_map.get("W", 0))
            losses = int(stats_map.get("L", 0))
            gp = wins + losses or 1
            ppg = float(stats_map.get("PPG", 0)) or float(stats_map.get("PF", 0)) / gp
            oppg = float(stats_map.get("OPPG", 0)) or float(stats_map.get("PA", 0)) / gp
            raw_diff = float(stats_map.get("DIFF", 0))
            # DIFF from ESPN is season total, convert to per-game
            diff_pg = raw_diff / gp if abs(raw_diff) > 50 else raw_diff
            # If no oppg, derive from ppg and diff
            if oppg == 0 and ppg > 0:
                oppg = ppg - diff_pg
            teams[name] = {
                "abbr": abbr,
                "wins": wins,
                "losses": losses,
                "win_pct": float(stats_map.get("PCT", 0.5)),
                "streak": int(stats_map.get("STRK", 0)),
                "ppg": round(ppg, 1),
                "oppg": round(oppg, 1),
                "diff": round(diff_pg, 1),
            }
    return teams


# ── Playoff bracket support ──

CONFERENCE_MAP: dict[str, str] = {
    # East
    "Boston Celtics": "east", "Brooklyn Nets": "east", "New York Knicks": "east",
    "Philadelphia 76ers": "east", "Toronto Raptors": "east", "Chicago Bulls": "east",
    "Cleveland Cavaliers": "east", "Detroit Pistons": "east", "Indiana Pacers": "east",
    "Milwaukee Bucks": "east", "Atlanta Hawks": "east", "Charlotte Hornets": "east",
    "Miami Heat": "east", "Orlando Magic": "east", "Washington Wizards": "east",
    # West
    "Denver Nuggets": "west", "Minnesota Timberwolves": "west",
    "Oklahoma City Thunder": "west", "Portland Trail Blazers": "west",
    "Utah Jazz": "west", "Golden State Warriors": "west", "LA Clippers": "west",
    "Los Angeles Lakers": "west", "Phoenix Suns": "west", "Sacramento Kings": "west",
    "Dallas Mavericks": "west", "Houston Rockets": "west",
    "Memphis Grizzlies": "west", "New Orleans Pelicans": "west", "San Antonio Spurs": "west",
}

TEAM_ABBREV: dict[str, str] = {
    "Boston Celtics": "BOS", "Brooklyn Nets": "BKN", "New York Knicks": "NY",
    "Philadelphia 76ers": "PHI", "Toronto Raptors": "TOR", "Chicago Bulls": "CHI",
    "Cleveland Cavaliers": "CLE", "Detroit Pistons": "DET", "Indiana Pacers": "IND",
    "Milwaukee Bucks": "MIL", "Atlanta Hawks": "ATL", "Charlotte Hornets": "CHA",
    "Miami Heat": "MIA", "Orlando Magic": "ORL", "Washington Wizards": "WAS",
    "Denver Nuggets": "DEN", "Minnesota Timberwolves": "MIN",
    "Oklahoma City Thunder": "OKC", "Portland Trail Blazers": "POR",
    "Utah Jazz": "UTA", "Golden State Warriors": "GS", "LA Clippers": "LAC",
    "Los Angeles Lakers": "LAL", "Phoenix Suns": "PHX", "Sacramento Kings": "SAC",
    "Dallas Mavericks": "DAL", "Houston Rockets": "HOU",
    "Memphis Grizzlies": "MEM", "New Orleans Pelicans": "NO", "San Antonio Spurs": "SA",
}

# Star player per team (one franchise face, used for the bracket card)
TEAM_STARS: dict[str, str] = {
    "Boston Celtics": "Jayson Tatum",
    "Brooklyn Nets": "Cam Thomas",
    "New York Knicks": "Jalen Brunson",
    "Philadelphia 76ers": "Tyrese Maxey",
    "Toronto Raptors": "Scottie Barnes",
    "Chicago Bulls": "Coby White",
    "Cleveland Cavaliers": "Donovan Mitchell",
    "Detroit Pistons": "Cade Cunningham",
    "Indiana Pacers": "Tyrese Haliburton",
    "Milwaukee Bucks": "Giannis Antetokounmpo",
    "Atlanta Hawks": "Trae Young",
    "Charlotte Hornets": "LaMelo Ball",
    "Miami Heat": "Bam Adebayo",
    "Orlando Magic": "Paolo Banchero",
    "Washington Wizards": "Jordan Poole",
    "Denver Nuggets": "Nikola Jokic",
    "Minnesota Timberwolves": "Anthony Edwards",
    "Oklahoma City Thunder": "Shai Gilgeous-Alexander",
    "Portland Trail Blazers": "Scoot Henderson",
    "Utah Jazz": "Lauri Markkanen",
    "Golden State Warriors": "Stephen Curry",
    "LA Clippers": "Kawhi Leonard",
    "Los Angeles Lakers": "Luka Doncic",
    "Phoenix Suns": "Devin Booker",
    "Sacramento Kings": "De'Aaron Fox",
    "Dallas Mavericks": "Anthony Davis",
    "Houston Rockets": "Alperen Sengun",
    "Memphis Grizzlies": "Ja Morant",
    "New Orleans Pelicans": "Zion Williamson",
    "San Antonio Spurs": "Victor Wembanyama",
}


def fetch_espn_injuries() -> dict[str, list[dict]]:
    """Fetch NBA injury report from ESPN. Returns {team_display_name: [injuries]}."""
    try:
        with _http() as c:
            r = c.get(f"{ESPN_BASE}/injuries")
            if r.status_code != 200:
                return {}
            data = r.json()
    except Exception as exc:
        print(f"  [warn] ESPN injuries fetch failed: {exc}", file=sys.stderr)
        return {}

    result: dict[str, list[dict]] = {}
    for team_block in data.get("injuries", []):
        team_name = team_block.get("displayName", "") or team_block.get("name", "")
        if not team_name:
            continue
        items = []
        for inj in team_block.get("injuries", []):
            ath = inj.get("athlete", {}) or {}
            items.append({
                "name": ath.get("displayName") or ath.get("fullName", ""),
                "status": inj.get("status", "Unknown"),
                "detail": (inj.get("type", {}) or {}).get("description", "") or inj.get("details", ""),
            })
        if items:
            result[team_name] = items
    return result


def _injury_buckets(items: list[dict]) -> tuple[list[dict], list[dict]]:
    out_statuses = {"out", "season-ending", "injured reserve"}
    out, gtd = [], []
    for item in items:
        status = (item.get("status") or "").lower()
        if status in out_statuses:
            out.append(item)
        elif any(x in status for x in ("day", "questionable", "probable", "doubtful", "gtd")):
            gtd.append(item)
    return out, gtd


def build_game_injuries(home: str, away: str, injuries_map: dict[str, list[dict]]) -> dict | None:
    home_items = injuries_map.get(home, [])
    away_items = injuries_map.get(away, [])
    if not home_items and not away_items:
        return None

    home_out, home_gtd = _injury_buckets(home_items)
    away_out, away_gtd = _injury_buckets(away_items)
    if not (home_out or home_gtd or away_out or away_gtd):
        return None

    return {
        "home_out": home_out,
        "home_gtd": home_gtd,
        "away_out": away_out,
        "away_gtd": away_gtd,
    }


def normalize_game_date(game: dict, fallback_ymd: str) -> str:
    raw = (game.get("date") or "")[:10]
    if raw:
        return raw.replace("-", "")
    fetched = game.get("_fetched_for_date")
    if fetched:
        return fetched
    return fallback_ymd


def build_recent_team_form(results: list[dict], max_games_per_team: int = 6) -> dict[str, dict]:
    buckets: dict[str, list[tuple[float, float]]] = {}
    for game in sorted(results, key=lambda x: x["date"], reverse=True):
        home = game.get("home_team") or game.get("team_a")
        away = game.get("away_team") or game.get("team_b")
        home_score = game.get("home_score")
        away_score = game.get("away_score")
        if not home or not away:
            continue

        for team, scored, allowed in (
            (home, home_score, away_score),
            (away, away_score, home_score),
        ):
            bucket = buckets.setdefault(team, [])
            if len(bucket) >= max_games_per_team:
                continue
            bucket.append((float(scored or 0.0), float(allowed or 0.0)))

    summary: dict[str, dict] = {}
    for team, games in buckets.items():
        if not games:
            continue
        total_scored = sum(g[0] for g in games)
        total_allowed = sum(g[1] for g in games)
        n = len(games)
        summary[team] = {
            "games": n,
            "ppg": total_scored / n,
            "oppg": total_allowed / n,
        }
    return summary


def _blend_recent_stat(
    season_value: float,
    recent_value: float | None,
    recent_games: int,
    *,
    recent_weight: float = 0.35,
    min_games: int = 3,
) -> float:
    if recent_value is None or recent_games < min_games:
        return season_value
    weight = recent_weight * min(recent_games / 6.0, 1.0)
    return season_value * (1.0 - weight) + float(recent_value) * weight


def _weighted_bias_adjustment(bias: float, samples: int, *, full_samples: int, cap: float) -> float:
    if not samples:
        return 0.0
    weight = min(samples / full_samples, 1.0)
    adj = float(bias) * weight
    return max(-cap, min(cap, adj))


def _default_calibration_snapshot() -> dict:
    return {
        "lookback_days": 21,
        "games_considered": 0,
        "spread_samples": 0,
        "spread_bias": 0.0,
        "spread_mae": 0.0,
        "spread_sign_accuracy": None,
        "moneyline_accuracy": None,
        "total_samples": 0,
        "total_bias": 0.0,
        "total_mae": 0.0,
    }


def save_prediction_calibration_snapshot(snapshot: dict, path: Path | None = None) -> dict:
    path = path or CALIBRATION_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _default_calibration_snapshot()
    for key in payload:
        if key in snapshot:
            payload[key] = snapshot[key]
    payload["saved_at"] = datetime.now().isoformat(timespec="seconds")
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def load_prediction_calibration_snapshot() -> dict:
    try:
        from nba_db import DB_PATH as _db, get_prediction_calibration
        snapshot = get_prediction_calibration(_db)
        if int(snapshot.get("games_considered") or 0) > 0:
            try:
                save_prediction_calibration_snapshot(snapshot)
            except Exception:
                pass
            return snapshot
    except Exception:
        pass

    if CALIBRATION_PATH.exists():
        try:
            payload = json.loads(CALIBRATION_PATH.read_text(encoding="utf-8"))
            snapshot = _default_calibration_snapshot()
            for key in snapshot:
                if key in payload:
                    snapshot[key] = payload[key]
            return snapshot
        except Exception:
            pass

    return _default_calibration_snapshot()


def calibrate_spread_projection(base_margin: float, raw_spread: float | None, calibration: dict | None) -> tuple[float, dict]:
    if raw_spread is None:
        blended = base_margin
        model_weight = 0.0
    else:
        agree = (base_margin == 0) or (raw_spread == 0) or (base_margin * raw_spread >= 0)
        model_weight = 0.35 if agree else 0.15
        blended = base_margin * (1.0 - model_weight) + raw_spread * model_weight
        if abs(base_margin) >= 1.5 and blended * base_margin < 0:
            blended = abs(blended) * (1.0 if base_margin >= 0 else -1.0)

    spread_samples = int((calibration or {}).get("spread_samples") or 0)
    spread_bias = float((calibration or {}).get("spread_bias") or 0.0)
    bias_adj = _weighted_bias_adjustment(spread_bias, spread_samples, full_samples=24, cap=6.0)
    calibrated = blended + bias_adj

    if abs(base_margin) >= 6 and abs(calibrated) < 1.5:
        calibrated = 1.5 if base_margin > 0 else -1.5

    return calibrated, {
        "base_margin": round(base_margin, 2),
        "raw_spread": round(raw_spread, 2) if raw_spread is not None else None,
        "model_weight": round(model_weight, 2),
        "bias_adjustment": round(bias_adj, 2),
    }


def project_total_points(
    home: str,
    away: str,
    standings: dict[str, dict],
    recent_form: dict[str, dict],
    calibration: dict | None,
) -> dict[str, float]:
    h_stats = standings.get(home, {})
    a_stats = standings.get(away, {})
    h_recent = recent_form.get(home, {})
    a_recent = recent_form.get(away, {})

    h_ppg = _blend_recent_stat(h_stats.get("ppg", 110.0), h_recent.get("ppg"), h_recent.get("games", 0))
    a_ppg = _blend_recent_stat(a_stats.get("ppg", 110.0), a_recent.get("ppg"), a_recent.get("games", 0))
    h_oppg = _blend_recent_stat(h_stats.get("oppg", 110.0), h_recent.get("oppg"), h_recent.get("games", 0))
    a_oppg = _blend_recent_stat(a_stats.get("oppg", 110.0), a_recent.get("oppg"), a_recent.get("games", 0))

    away_expected_raw = (a_ppg + h_oppg) / 2.0
    home_expected_raw = (h_ppg + a_oppg) / 2.0
    avg_pace = (a_ppg + a_oppg + h_ppg + h_oppg) / 4.0
    league_avg = 113.0
    pace_adj = (avg_pace - league_avg) * 0.35
    raw_total = away_expected_raw + home_expected_raw + pace_adj

    total_samples = int((calibration or {}).get("total_samples") or 0)
    total_bias = float((calibration or {}).get("total_bias") or 0.0)
    bias_adj = _weighted_bias_adjustment(total_bias, total_samples, full_samples=24, cap=10.0)
    pred_total = max(185.0, min(255.0, raw_total + bias_adj))

    scale = pred_total / raw_total if raw_total > 0 else 1.0
    return {
        "pred_total": pred_total,
        "raw_total": raw_total,
        "away_expected": away_expected_raw * scale,
        "home_expected": home_expected_raw * scale,
        "bias_adjustment": bias_adj,
    }


def _elo_game_prob(elo_a: float, elo_b: float, hca: float = 48.0) -> float:
    """Per-game win probability for team A with home-court advantage."""
    diff = elo_a - elo_b + hca
    return 1.0 / (1.0 + 10.0 ** (-diff / 400.0))


def _series_expected_games(p_a: float, best_of: int = 7) -> float:
    """Expected number of games in a best-of-N series given A's per-game prob p_a."""
    from math import comb
    wins_needed = (best_of + 1) // 2
    ev = 0.0
    for g in range(wins_needed, best_of + 1):
        p_a_ends = comb(g - 1, wins_needed - 1) * p_a ** wins_needed * (1 - p_a) ** (g - wins_needed)
        p_b_ends = comb(g - 1, wins_needed - 1) * (1 - p_a) ** wins_needed * p_a ** (g - wins_needed)
        ev += g * (p_a_ends + p_b_ends)
    return ev


def _simulate_series(team_a: str, team_b: str, elo: dict[str, float],
                     hca: float = 48.0, best_of: int = 7, rng=None) -> str:
    """Simulate a best-of-N series with 2-2-1-1-1 home pattern. Return winner."""
    import random as _random
    rng = rng or _random
    wins_a, wins_b = 0, 0
    wins_needed = (best_of + 1) // 2
    # Home pattern for team A (higher seed): games 0,1,4,6 are home
    a_home_idx = {0, 1, 4, 6}
    i = 0
    while wins_a < wins_needed and wins_b < wins_needed:
        home_bonus = hca if i in a_home_idx else -hca
        diff = elo.get(team_a, 1500) - elo.get(team_b, 1500) + home_bonus
        p_a = 1.0 / (1.0 + 10.0 ** (-diff / 400.0))
        if rng.random() < p_a:
            wins_a += 1
        else:
            wins_b += 1
        i += 1
    return team_a if wins_a >= wins_needed else team_b


def build_playoff_bracket(standings: dict, elo_teams: dict,
                          injuries: dict | None = None,
                          hca: float = 48.0, n_sims: int = 10000,
                          seed: int = 42) -> dict:
    """Build playoff bracket with Monte Carlo advance probabilities.

    Standard bracket pairings (per conference, top 8 by win %):
        R1: 1v8, 4v5, 3v6, 2v7  (display order top→bottom)
        R2: winner(1v8) vs winner(4v5); winner(3v6) vs winner(2v7)
        Conf Finals: winner(R2 upper) vs winner(R2 lower)
        Finals:      West Conf champ vs East Conf champ (neutral court)
    """
    import random as _random
    from collections import defaultdict as _dd
    rng = _random.Random(seed)
    injuries = injuries or {}

    # 1) Pool teams into conferences, sort by wins → win_pct, take top 8
    pool: dict[str, list[dict]] = {"east": [], "west": []}
    for team, info in standings.items():
        conf = CONFERENCE_MAP.get(team)
        if not conf:
            continue
        wins = int(info.get("wins", 0))
        losses = int(info.get("losses", 0))
        wp = float(info.get("win_pct", 0)) or (wins / max(1, wins + losses))
        pool[conf].append({
            "team": team, "wins": wins, "losses": losses,
            "win_pct": wp, "elo": float(elo_teams.get(team, 1500)),
        })
    for conf in pool:
        pool[conf].sort(key=lambda x: (x["win_pct"], x["elo"]), reverse=True)
        pool[conf] = pool[conf][:8]
        for i, e in enumerate(pool[conf]):
            e["seed"] = i + 1
    if len(pool["east"]) < 8 or len(pool["west"]) < 8:
        return {"error": "not enough teams to form bracket",
                "east_count": len(pool["east"]), "west_count": len(pool["west"])}

    # 2) Pair structure (indices into 8-team list)
    R1_PAIRS = [(0, 7), (3, 4), (2, 5), (1, 6)]  # 1v8, 4v5, 3v6, 2v7
    R2_PAIRS = [(0, 1), (2, 3)]  # upper half (R1[0] winner vs R1[1] winner), lower half

    # 3) Monte Carlo
    stages = _dd(lambda: {"r1": 0, "r2": 0, "cf": 0, "finals": 0, "champ": 0})
    for _ in range(n_sims):
        conf_champ = {}
        for conf in ("east", "west"):
            ss = pool[conf]
            r1_win = []
            for (i_top, i_bot) in R1_PAIRS:
                w = _simulate_series(ss[i_top]["team"], ss[i_bot]["team"], elo_teams, hca, rng=rng)
                r1_win.append(w)
                stages[w]["r1"] += 1
            r2_win = []
            for (i_top, i_bot) in R2_PAIRS:
                w = _simulate_series(r1_win[i_top], r1_win[i_bot], elo_teams, hca, rng=rng)
                r2_win.append(w)
                stages[w]["r2"] += 1
            cf_w = _simulate_series(r2_win[0], r2_win[1], elo_teams, hca, rng=rng)
            stages[cf_w]["cf"] += 1
            conf_champ[conf] = cf_w
        # Finals — neutral court, no HCA for the simulation (use 0)
        stages[conf_champ["east"]]["finals"] += 1
        stages[conf_champ["west"]]["finals"] += 1
        champ = _simulate_series(conf_champ["west"], conf_champ["east"], elo_teams, hca=0.0, rng=rng)
        stages[champ]["champ"] += 1

    # 4) Build card entries
    def star_of(team: str) -> dict:
        name = TEAM_STARS.get(team, "")
        status = "Healthy"
        detail = ""
        for inj in injuries.get(team, []):
            if (inj.get("name") or "").lower() == name.lower():
                status = inj.get("status", "Unknown")
                detail = inj.get("detail", "")
                break
        return {"name": name, "status": status, "detail": detail}

    def team_node(info: dict, stage_key: str, wins_denom: int = n_sims) -> dict:
        team = info["team"]
        return {
            "seed": info["seed"],
            "team": team,
            "abbrev": TEAM_ABBREV.get(team, team.split()[-1][:3].upper()),
            "elo": int(info["elo"]),
            "record": f"{info['wins']}-{info['losses']}",
            "advance_prob": round(stages[team][stage_key] / wins_denom * 100, 1),
            "star": star_of(team),
        }

    def most_likely(candidate_teams: list[str], stage_key: str) -> str:
        return max(candidate_teams, key=lambda t: stages[t][stage_key])

    def build_conf(conf: str) -> dict:
        ss = pool[conf]
        # R1
        r1 = []
        for (i_top, i_bot) in R1_PAIRS:
            top_info, bot_info = ss[i_top], ss[i_bot]
            p = _elo_game_prob(top_info["elo"], bot_info["elo"], hca)
            r1.append({
                "top": team_node(top_info, "r1"),
                "bot": team_node(bot_info, "r1"),
                "expected_games": round(_series_expected_games(p), 1),
            })
        # R2 (per slot: pick most likely team out of 2 possible R1 winners)
        r2 = []
        for (i_top, i_bot) in R2_PAIRS:
            half_top_teams = [ss[R1_PAIRS[i_top][0]]["team"], ss[R1_PAIRS[i_top][1]]["team"]]
            half_bot_teams = [ss[R1_PAIRS[i_bot][0]]["team"], ss[R1_PAIRS[i_bot][1]]["team"]]
            t_top = most_likely(half_top_teams, "r2")
            t_bot = most_likely(half_bot_teams, "r2")
            info_top = next(x for x in ss if x["team"] == t_top)
            info_bot = next(x for x in ss if x["team"] == t_bot)
            p = _elo_game_prob(info_top["elo"], info_bot["elo"], hca)
            r2.append({
                "top": team_node(info_top, "r2"),
                "bot": team_node(info_bot, "r2"),
                "expected_games": round(_series_expected_games(p), 1),
            })
        # Conf finals
        upper = [ss[i]["team"] for pair in R1_PAIRS[:2] for i in pair]
        lower = [ss[i]["team"] for pair in R1_PAIRS[2:] for i in pair]
        t_top = most_likely(upper, "cf")
        t_bot = most_likely(lower, "cf")
        info_top = next(x for x in ss if x["team"] == t_top)
        info_bot = next(x for x in ss if x["team"] == t_bot)
        p = _elo_game_prob(info_top["elo"], info_bot["elo"], hca)
        cf = [{
            "top": team_node(info_top, "cf"),
            "bot": team_node(info_bot, "cf"),
            "expected_games": round(_series_expected_games(p), 1),
        }]
        return {
            "seeds": [
                {"seed": x["seed"], "team": x["team"],
                 "abbrev": TEAM_ABBREV.get(x["team"], x["team"][:3].upper()),
                 "record": f"{x['wins']}-{x['losses']}", "elo": int(x["elo"])}
                for x in ss
            ],
            "r1": r1, "r2": r2, "conf_finals": cf,
        }

    # 5) Finals
    west_rep = max((x["team"] for x in pool["west"]), key=lambda t: stages[t]["finals"])
    east_rep = max((x["team"] for x in pool["east"]), key=lambda t: stages[t]["finals"])
    w_info = next(x for x in pool["west"] if x["team"] == west_rep)
    e_info = next(x for x in pool["east"] if x["team"] == east_rep)
    p_finals = _elo_game_prob(w_info["elo"], e_info["elo"], hca=0.0)
    finals = {
        "west": team_node(w_info, "champ"),
        "east": team_node(e_info, "champ"),
        "expected_games": round(_series_expected_games(p_finals), 1),
    }

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "n_sims": n_sims,
        "west": build_conf("west"),
        "east": build_conf("east"),
        "finals": finals,
    }


def fetch_espn_results(last_n_days: int = 60) -> list[dict]:
    """Fetch recent game results for Elo building."""
    games: list[dict] = []
    try:
        with _http() as c:
            for day_offset in range(last_n_days):
                date = (datetime.now() - timedelta(days=day_offset)).strftime("%Y%m%d")
                url = f"{ESPN_BASE}/scoreboard?dates={date}"
                try:
                    r = c.get(url)
                    if r.status_code != 200:
                        continue
                    data = r.json()
                except Exception:
                    continue

                for event in data.get("events", []):
                    completed = event.get("status", {}).get("type", {}).get("completed", False)
                    if not completed:
                        continue
                    comps = event.get("competitions", [{}])[0]
                    teams = comps.get("competitors", [])
                    if len(teams) < 2:
                        continue
                    home = next((t for t in teams if t.get("homeAway") == "home"), teams[0])
                    away = next((t for t in teams if t.get("homeAway") == "away"), teams[1])
                    home_score = int(home.get("score", 0))
                    away_score = int(away.get("score", 0))
                    home_name = home["team"]["displayName"]
                    away_name = away["team"]["displayName"]
                    winner = home_name if home_score > away_score else away_name
                    loser = away_name if home_score > away_score else home_name
                    games.append({
                        "date": date,
                        "home_team": home_name,
                        "away_team": away_name,
                        "team_a": home_name,
                        "team_b": away_name,
                        "winner": winner,
                        "loser": loser,
                        "home_score": home_score,
                        "away_score": away_score,
                    })

                # Rate-limit: be polite to ESPN
                if day_offset % 10 == 9:
                    time.sleep(0.5)
    except Exception as exc:
        print(f"  [warn] ESPN results fetch failed: {exc}")

    return games


# ── Data Collection: Polymarket ──

NON_NBA_KEYWORDS = [
    "nhl", "ipl", "mlb", "nfl", "mls", "cricket", "hockey", "baseball",
    "football", "soccer", "premier league", "la liga", "serie a", "bundesliga",
    "champions league", "europa", "f1", "formula", "tennis", "golf", "ufc",
    "boxing", "rugby", "afl", "cfl", "wwe", "college football",
    "blackhawks", "sharks", "predators", "penguins", "rangers", "flyers",
    "bruins", "canucks", "oilers", "flames", "senators", "canadiens",
    "red wings", "blue jackets", "islanders", "devils", "hurricanes",
    "panthers", "lightning", "maple leafs", "sabres", "kraken", "wild",
    "avalanche", "coyotes", "ducks", "blues", "stars", "jets",
    "knights", "capitals",
    "kolkata", "mumbai", "chennai", "delhi", "punjab", "rajasthan",
    "bengaluru", "hyderabad", "lucknow", "gujarat",
    "juventus", "barcelona", "real madrid", "bayern", "psg", "liverpool",
    "manchester", "arsenal", "chelsea", "tottenham", "inter", "milan",
]


def is_nba_market(m: dict) -> bool:
    """Check if a Polymarket market is NBA-related. Strict filtering."""
    q = (m.get("question", "") + " " + m.get("description", "") + " " + m.get("slug", "")).lower()

    # Explicit NBA mention = always include
    if "nba" in q:
        return True

    # Exclude known non-NBA sports/teams
    if any(x in q for x in NON_NBA_KEYWORDS):
        return False

    # Exclude spread/O-U markets without clear NBA team
    if any(x in q for x in ["o/u ", "spread:", "over/under"]):
        # Only include if has 2 NBA team names
        count = sum(1 for t in NBA_TEAMS if t in q)
        return count >= 2

    # Must contain at least one NBA team name
    return any(t in q for t in NBA_TEAMS)


def fetch_polymarket_nba() -> list[dict]:
    """Fetch current NBA markets from Polymarket Gamma API."""
    try:
        with _http() as c:
            r = c.get(f"{GAMMA}/markets", params={
                "limit": 100,
                "active": "true",
                "order": "volume24hr",
                "ascending": "false",
            })
            if r.status_code != 200:
                return []
            markets = r.json()
    except Exception as exc:
        print(f"  [warn] Polymarket fetch failed: {exc}")
        return []

    return [m for m in markets if is_nba_market(m)]


def get_yes_price(market: dict) -> float:
    """Extract implied probability (YES price) from a Polymarket market."""
    # outcomePrices is a JSON-encoded list like '["0.65","0.35"]'
    prices = market.get("outcomePrices", "")
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except (json.JSONDecodeError, TypeError):
            return 0.5
    if isinstance(prices, list) and len(prices) > 0:
        try:
            return float(prices[0])
        except (ValueError, TypeError):
            return 0.5
    return 0.5


def parse_matchup(question: str) -> dict:
    """Parse team names and potential spread from a Polymarket question.
    Returns: {'team_a': str, 'team_b': str, 'spread': float, 'target_team': str}
    """
    import re
    q = question.lower().strip()
    found: list[str] = []
    # Sort aliases by length
    for alias, full_name in sorted(TEAM_ALIASES.items(), key=lambda x: -len(x[0])):
        if alias in q and full_name not in found:
            found.append(full_name)
        if len(found) >= 2:
            break

    # Look for spread like (-4.5) or (+2.5)
    spread = 0.0
    target_team = found[0] if found else None
    
    # Regex for spread: matches things like (-24.5) or +3
    match = re.search(r'([+-]?\d+\.?\d*)', q.replace('(', '').replace(')', ''))
    if match and any(x in q for x in ['-', '+']):
        try:
            val = float(match.group(1))
            # If the question contains a team name and a spread, assume spread applies to that team
            spread = val
        except ValueError:
            pass

    return {
        "team_a": found[0] if len(found) > 0 else None,
        "team_b": found[1] if len(found) > 1 else None,
        "spread": spread,
        "target_team": target_team
    }


def calculate_kelly(prob: float, poly_price: float, fraction: float = 0.25) -> float:
    """Calculate suggested bet fraction using Kelly Criterion.
    Defaulting to Quarter-Kelly (0.25) for safety.
    """
    if poly_price <= 0 or poly_price >= 0.99 or prob <= poly_price:
        return 0.0
    # decimal_odds = 1.0 / poly_price
    # b (net odds) = decimal_odds - 1
    b = (1.0 - poly_price) / poly_price
    q = 1.0 - prob
    # Kelly % = (bp - q) / b
    k = (b * prob - q) / b
    return max(0.0, k * fraction)


# ── Feature Engineering ──

def build_features(
    team_a: str,
    team_b: str,
    team_stats: dict[str, dict],
    elo: EloSystem,
    is_home: bool = True,
    b2b_a: bool = False,
    b2b_b: bool = False,
) -> dict[str, float]:
    """Build feature vector for a matchup."""
    elo_a = elo.ratings.get(team_a, 1500.0)
    elo_b = elo.ratings.get(team_b, 1500.0)
    sa = team_stats.get(team_a, {})
    sb = team_stats.get(team_b, {})

    return {
        "elo_diff": elo_a - elo_b,
        "elo_a": elo_a,
        "elo_b": elo_b,
        "win_pct_a": sa.get("win_pct", 0.5),
        "win_pct_b": sb.get("win_pct", 0.5),
        "win_pct_diff": sa.get("win_pct", 0.5) - sb.get("win_pct", 0.5),
        "ppg_a": sa.get("ppg", 105.0),
        "ppg_b": sb.get("ppg", 105.0),
        "oppg_a": sa.get("oppg", 105.0),
        "oppg_b": sb.get("oppg", 105.0),
        "diff_a": sa.get("diff", 0.0),
        "diff_b": sb.get("diff", 0.0),
        "streak_a": float(sa.get("streak", 0)),
        "streak_b": float(sb.get("streak", 0)),
        "b2b_a": 1.0 if b2b_a else 0.0,
        "b2b_b": 1.0 if b2b_b else 0.0,
        "b2b_adv": (1.0 if b2b_b else 0.0) - (1.0 if b2b_a else 0.0),  # +1 = opponent B2B advantage
        "both_b2b": 1.0 if (b2b_a and b2b_b) else 0.0,  # neutralizer
        "home_away": 1.0 if is_home else 0.0,
    }


# ── XGBoost Model ──

def _ensure_xgboost():
    """Import xgboost, installing if necessary."""
    try:
        import xgboost as xgb
        return xgb
    except ImportError:
        print("  Installing xgboost...")
        import subprocess
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "xgboost"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        import xgboost as xgb
        return xgb


class NBAPredictor:
    """XGBoost + Elo predictor for NBA games (Regression version)."""

    def __init__(self):
        self.model = None
        self.elo = EloSystem()
        self.feature_names: list[str] = []
        self.team_stats: dict[str, dict] = {}
        self.rmse = 12.0  # Default RMSE for NBA point spreads

    def train(self, games: list[dict], standings: dict[str, dict] | None = None):
        """Train XGBoost on historical game results."""
        xgb = _ensure_xgboost()

        self.team_stats = standings or {}

        # Build Elo from game history (oldest first)
        sorted_games = sorted(games, key=lambda x: x["date"])
        for g in sorted_games:
            self.elo.update(g["winner"], g["loser"], g.get("home_team"))

        # Build feature matrix
        X, y = [], []
        last_game: dict[str, str] = {}  # team -> date_str

        for g in sorted_games:
            date_obj = datetime.strptime(g["date"], "%Y%m%d")
            
            # Detect B2B
            b2b_a = False
            if g["team_a"] in last_game:
                prev_date = datetime.strptime(last_game[g["team_a"]], "%Y%m%d")
                if (date_obj - prev_date).days == 1:
                    b2b_a = True
            
            b2b_b = False
            if g["team_b"] in last_game:
                prev_date = datetime.strptime(last_game[g["team_b"]], "%Y%m%d")
                if (date_obj - prev_date).days == 1:
                    b2b_b = True

            feats = build_features(
                g["team_a"], g["team_b"], self.team_stats, self.elo,
                is_home=True, b2b_a=b2b_a, b2b_b=b2b_b
            )
            X.append(list(feats.values()))
            y.append(float(g["home_score"] - g["away_score"]))
            self.feature_names = list(feats.keys())

            # Update last game date
            last_game[g["team_a"]] = g["date"]
            last_game[g["team_b"]] = g["date"]

        if not X:
            print("  [warn] No training data available")
            return

        X_arr = np.array(X)
        y_arr = np.array(y)

        dtrain = xgb.DMatrix(X_arr, label=y_arr, feature_names=self.feature_names)
        params = {
            "objective": "reg:squarederror",
            "eval_metric": "rmse",
            "max_depth": 5,
            "eta": 0.05,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "verbosity": 0,
        }
        self.model = xgb.train(params, dtrain, num_boost_round=150)

        # Feature importance
        importance = self.model.get_score(importance_type="weight")
        if importance:
            print("\n  Feature Importance:")
            for f, score in sorted(importance.items(), key=lambda x: x[1], reverse=True):
                print(f"    {f}: {score}")

        # Training RMSE
        preds = self.model.predict(dtrain)
        self.rmse = float(np.sqrt(np.mean((preds - y_arr) ** 2)))
        print(f"\n  Training RMSE: {self.rmse:.2f} points (Typical: 11-13)")

    def predict(self, team_a: str, team_b: str, is_home: bool = True, 
                b2b_a: bool = False, b2b_b: bool = False) -> float:
        """Predict WIN PROBABILITY for team_a (calculated from margin)."""
        margin = self.predict_margin(team_a, team_b, is_home, b2b_a, b2b_b)
        return self.margin_to_prob(margin, 0)

    def predict_margin(self, team_a: str, team_b: str, is_home: bool = True,
                       b2b_a: bool = False, b2b_b: bool = False) -> float:
        """Predict point margin (team_a - team_b).
        Uses Elo-based calculation (proven reliable).
        The XGBoost regression model had directional issues and is disabled.
        Use SpreadPredictor separately for spread-specific predictions.
        """
        elo_a = self.elo.ratings.get(team_a, 1500)
        elo_b = self.elo.ratings.get(team_b, 1500)
        h_adj = 100 if is_home else 0

        # Elo-based margin: ~28 Elo points per 1 point of spread
        elo_margin = (elo_a - elo_b + h_adj) / 28.0

        # Adjust with team stats if available
        sa = self.team_stats.get(team_a, {})
        sb = self.team_stats.get(team_b, {})
        diff_a = sa.get("diff", 0)
        diff_b = sb.get("diff", 0)

        if diff_a != 0 or diff_b != 0:
            # Blend Elo margin with point differential advantage
            stats_margin = (diff_a - diff_b) / 2 + 3.5 * (1 if is_home else 0)
            return elo_margin * 0.6 + stats_margin * 0.4
        return elo_margin

    def margin_to_prob(self, margin: float, threshold: float) -> float:
        """Convert predicted margin and threshold to win probability using Normal CDF."""
        import math
        # Z-score = (margin - threshold) / RMSE
        # Prob = 0.5 * (1 + erf(Z / sqrt(2)))
        z = (margin - threshold) / (self.rmse or 12.0)
        return 0.5 * (1 + math.erf(z / math.sqrt(2)))

    def save(self, path: Path | None = None):
        """Persist Elo ratings and model metadata."""
        path = path or MODEL_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "elo": self.elo.to_dict(),
            "feature_names": self.feature_names,
            "has_model": self.model is not None,
            "rmse": self.rmse,
            "saved_at": datetime.now().isoformat(),
        }
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        # Save xgboost model binary if trained
        if self.model is not None:
            model_bin = path.with_suffix(".xgb")
            self.model.save_model(str(model_bin))
        print(f"  Saved state to {path}")

    def load(self, path: Path | None = None):
        """Load persisted state."""
        path = path or MODEL_PATH
        if not path.exists():
            return False
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            self.elo.from_dict(state.get("elo", {}))
            self.feature_names = state.get("feature_names", [])
            self.rmse = state.get("rmse", 12.0)
            # Load xgboost model if available
            model_bin = path.with_suffix(".xgb")
            if state.get("has_model") and model_bin.exists() and self.feature_names:
                xgb = _ensure_xgboost()
                self.model = xgb.Booster()
                self.model.load_model(str(model_bin))
            print(f"  Loaded state from {path}")
            return True
        except Exception as exc:
            print(f"  [warn] Failed to load state: {exc}")
            return False


# ── Rest-day Helper ──

def calc_rest_days(team_name: str, last_game_dict: dict[str, str]) -> int:
    """Calculate days since last game. Returns 3 if unknown."""
    if team_name not in last_game_dict:
        return 3  # assume normal rest
    last_date = datetime.strptime(last_game_dict[team_name], "%Y%m%d")
    days = (datetime.now() - last_date).days
    return min(max(days, 0), 7)


# ── Spread Prediction Model ──

class SpreadPredictor:
    """XGBoost regression model to predict home team margin (home_score - away_score)."""

    def __init__(self):
        self.model = None
        self.feature_names: list[str] = []

    def build_spread_features(
        self,
        home: str,
        away: str,
        standings: dict[str, dict],
        elo: EloSystem,
        rest_days_home: int = 2,
        rest_days_away: int = 2,
    ) -> dict[str, float]:
        """Extended features for spread prediction."""
        h = standings.get(home, {})
        a = standings.get(away, {})
        elo_h = elo.ratings.get(home, 1500)
        elo_a = elo.ratings.get(away, 1500)

        return {
            "elo_diff": elo_h - elo_a,
            "elo_home": elo_h,
            "elo_away": elo_a,
            "win_pct_diff": h.get("win_pct", 0.5) - a.get("win_pct", 0.5),
            "ppg_home": h.get("ppg", 110),
            "ppg_away": a.get("ppg", 110),
            "oppg_home": h.get("oppg", 110),
            "oppg_away": a.get("oppg", 110),
            "diff_home": h.get("diff", 0),
            "diff_away": a.get("diff", 0),
            "net_rating_diff": h.get("diff", 0) - a.get("diff", 0),
            "pace_proxy": (
                h.get("ppg", 110) + h.get("oppg", 110) +
                a.get("ppg", 110) + a.get("oppg", 110)
            ) / 4,
            "streak_home": float(h.get("streak", 0)),
            "streak_away": float(a.get("streak", 0)),
            "rest_days_home": float(rest_days_home),
            "rest_days_away": float(rest_days_away),
            "rest_advantage": float(rest_days_home - rest_days_away),
            "home_court": 3.5,
        }

    def train(self, games: list[dict], standings: dict[str, dict], elo: EloSystem):
        """Train on historical games to predict margin."""
        xgb = _ensure_xgboost()

        X, y = [], []
        last_played: dict[str, str] = {}

        for g in sorted(games, key=lambda x: x["date"]):
            home = g["team_a"]
            away = g["team_b"]
            date_obj = datetime.strptime(g["date"], "%Y%m%d")

            # Rest days
            rest_h = (
                (date_obj - datetime.strptime(last_played[home], "%Y%m%d")).days
                if home in last_played else 3
            )
            rest_a = (
                (date_obj - datetime.strptime(last_played[away], "%Y%m%d")).days
                if away in last_played else 3
            )
            rest_h = min(max(rest_h, 0), 7)
            rest_a = min(max(rest_a, 0), 7)

            feats = self.build_spread_features(home, away, standings, elo, rest_h, rest_a)
            X.append(list(feats.values()))

            margin = g["home_score"] - g["away_score"]
            y.append(margin)

            self.feature_names = list(feats.keys())
            last_played[home] = g["date"]
            last_played[away] = g["date"]

        if not X:
            return

        X_arr = np.array(X)
        y_arr = np.array(y)

        dtrain = xgb.DMatrix(X_arr, label=y_arr, feature_names=self.feature_names)
        params = {
            "objective": "reg:squarederror",
            "eval_metric": "mae",
            "max_depth": 5,
            "eta": 0.08,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "min_child_weight": 3,
            "verbosity": 0,
        }
        self.model = xgb.train(params, dtrain, num_boost_round=150)

        # Training MAE
        preds = self.model.predict(dtrain)
        mae = float(np.mean(np.abs(preds - y_arr)))
        print(f"  Spread Model Training MAE: {mae:.1f} points")

        # Feature importance
        importance = self.model.get_score(importance_type="weight")
        print("  Spread Feature Importance:")
        for f, s in sorted(importance.items(), key=lambda x: x[1], reverse=True)[:8]:
            print(f"    {f}: {s}")

    def predict(
        self,
        home: str,
        away: str,
        standings: dict[str, dict],
        elo: EloSystem,
        rest_days_home: int = 2,
        rest_days_away: int = 2,
    ) -> float:
        """Predict home team margin."""
        if self.model is None:
            elo_h = elo.ratings.get(home, 1500)
            elo_a = elo.ratings.get(away, 1500)
            return (elo_h - elo_a) / 28 + 3.5

        xgb = _ensure_xgboost()
        feats = self.build_spread_features(home, away, standings, elo, rest_days_home, rest_days_away)
        X = np.array([list(feats.values())])
        dtest = xgb.DMatrix(X, feature_names=self.feature_names)
        return float(self.model.predict(dtest)[0])

    def save(self, path: Path | None = None):
        path = path or SPREAD_MODEL_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.model:
            self.model.save_model(str(path))
            meta = path.with_suffix(".json")
            meta.write_text(json.dumps({"feature_names": self.feature_names}))
            print(f"  Spread model saved to {path}")

    def load(self, path: Path | None = None) -> bool:
        path = path or SPREAD_MODEL_PATH
        meta = path.with_suffix(".json")
        if path.exists() and meta.exists():
            xgb = _ensure_xgboost()
            self.model = xgb.Booster()
            self.model.load_model(str(path))
            self.feature_names = json.loads(meta.read_text()).get("feature_names", [])
            return True
        return False


# ── Brier Score & Edge Detection ──

def brier_score(predictions: list[float], outcomes: list[int]) -> float:
    """Calculate Brier score (lower = better, 0 = perfect)."""
    return float(np.mean((np.array(predictions) - np.array(outcomes)) ** 2))


def find_edges(predictor: NBAPredictor, polymarket_nba: list[dict]) -> list[dict]:
    """Compare model predictions vs Polymarket odds to find edges.
    Only processes single-game moneyline markets. Skips championship,
    spread, O/U, and other market types the model can't predict."""
    edges: list[dict] = []

    # Keywords that indicate non-single-game markets
    SKIP_KEYWORDS = [
        "finals", "championship", "win the 2", "mvp", "rookie",
        "o/u ", "over/under", "total points",
        "spread:", "spread (",
        "playoff", "series", "round",
        "season", "regular season",
        "all-star", "draft",
    ]

    for market in polymarket_nba:
        q = (market.get("question", "") or "").lower()

        # Skip non-applicable market types
        if any(kw in q for kw in SKIP_KEYWORDS):
            continue

        m_info = parse_matchup(market.get("question", ""))
        team_a = m_info["team_a"]
        team_b = m_info["team_b"]
        spread = m_info["spread"]

        # Must have two teams (single game matchup)
        if not team_a or not team_b:
            continue

        # Predict margin (team_a - team_b)
        proj_margin = predictor.predict_margin(team_a, team_b, is_home=True)

        # For moneyline: probability team_a wins
        threshold = -spread if spread != 0 else 0
        model_prob = predictor.margin_to_prob(proj_margin, threshold)

        poly_yes = get_yes_price(market)
        edge = model_prob - poly_yes
        kelly = calculate_kelly(model_prob, poly_yes)

        # Determine predicted winner
        pred_winner = team_a if proj_margin > 0 else team_b
        pred_winner_margin = abs(proj_margin)

        edges.append({
            "question": market.get("question", ""),
            "proj_margin": proj_margin,
            "pred_winner": pred_winner,
            "pred_margin": pred_winner_margin,
            "team_a": team_a,
            "team_b": team_b,
            "market_spread": spread,
            "model_prob": model_prob,
            "poly_prob": poly_yes,
            "edge": edge,
            "abs_edge": abs(edge),
            "kelly_pct": kelly * 100,
            "bet": "YES" if edge > 0 else "NO",
            "volume": market.get("volume24hr", 0),
            "liquidity": float(market.get("liquidity", 0) or 0),
            "slug": market.get("slug", ""),
        })

    return sorted(edges, key=lambda x: x["abs_edge"], reverse=True)


# ── CLI ──

def _build_elo_from_recent(predictor: NBAPredictor, days: int = 60):
    """Fetch recent results, build Elo ratings, and load team stats."""
    print("  Fetching recent game results for Elo...")
    games = fetch_espn_results(days)
    print(f"  Loaded {len(games)} completed games from last {days} days")
    for g in sorted(games, key=lambda x: x["date"]):
        predictor.elo.update(g["winner"], g["loser"], g.get("home_team"))
    # Always load standings for team_stats (PPG, win%, etc.)
    standings = fetch_espn_standings()
    if standings:
        predictor.team_stats = standings
        print(f"  Loaded {len(standings)} team stats from ESPN")
    return games


def cmd_today(predictor: NBAPredictor, days_ahead: int = 0):
    """Show upcoming game predictions with B2B awareness and projected margins."""
    print("\n" + "=" * 70)
    title = "Today's Games" if days_ahead == 0 else f"Today + Next {days_ahead} Days"
    print(f"  NBA PREDICTIONS -- {title} (Regression Model)")
    print("=" * 70)

    if days_ahead > 0:
        today = fetch_espn_scoreboard_range(days_ahead=days_ahead)
    else:
        today = fetch_espn_scoreboard()
    if not today:
        print("\n  No games scheduled (or ESPN API unavailable).")
        return

    standings = fetch_espn_standings()
    predictor.team_stats = standings

    recent_results = fetch_espn_results(21)
    recent_form = build_recent_team_form(recent_results)
    calibration = load_prediction_calibration_snapshot()
    last_game: dict[str, str] = {}
    for g in recent_results:
        # Track most recent game date per team (keep latest)
        for team_key in ("team_a", "team_b"):
            tname = g[team_key]
            if tname not in last_game or g["date"] > last_game[tname]:
                last_game[tname] = g["date"]

    yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")

    # Load spread model if available
    spread_model = SpreadPredictor()
    has_spread = spread_model.load()

    for game in today:
        home, away = game["home"], game["away"]
        b2b_home = (home in last_game and last_game[home] == yesterday_str)
        b2b_away = (away in last_game and last_game[away] == yesterday_str)

        rest_h = calc_rest_days(home, last_game)
        rest_a = calc_rest_days(away, last_game)

        base_margin = predictor.predict_margin(home, away, is_home=True,
                                               b2b_a=b2b_home, b2b_b=b2b_away)
        prob = predictor.margin_to_prob(base_margin, 0)

        pick = home if base_margin > 0 else away
        conf = prob if base_margin > 0 else 1 - prob

        home_elo = predictor.elo.ratings.get(home, 1500)
        away_elo = predictor.elo.ratings.get(away, 1500)

        status = f"  [{game['status']}]" if game["status"] else ""
        h_tag = " [B2B]" if b2b_home else ""
        a_tag = " [B2B]" if b2b_away else ""

        print(f"\n  {away}{a_tag} ({game['away_record']}) @ {home}{h_tag} ({game['home_record']}){status}")
        print(f"  Prediction: {pick} by {abs(base_margin):.1f} points ({conf*100:.1f}% confidence)")
        print(f"  Elo Ratings: {home}={home_elo:.0f} | {away}={away_elo:.0f}")

        raw_spread = spread_model.predict(home, away, standings, predictor.elo, rest_h, rest_a) if has_spread else None
        sp_margin, _spread_meta = calibrate_spread_projection(base_margin, raw_spread, calibration)
        total_proj = project_total_points(home, away, standings, recent_form, calibration)
        home_abbr = game.get("home_abbr", home[:3].upper())
        print(f"  Spread: {home_abbr} {sp_margin:+.1f}  |  Total: {total_proj['pred_total']:.0f}")

    print()


def cmd_train(predictor: NBAPredictor, days: int):
    """Train model on recent game results."""
    print("\n  Fetching game history...")
    games = fetch_espn_results(days)
    print(f"  Loaded {len(games)} games from last {days} days")

    print("\n  Fetching team standings...")
    standings = fetch_espn_standings()
    print(f"  Loaded {len(standings)} teams")

    if not games:
        print("  [error] No game data to train on")
        return

    print("\n  Training XGBoost model...")
    predictor.train(games, standings)
    predictor.save()


def cmd_edge(predictor: NBAPredictor):
    """Find edge opportunities vs Polymarket using regression-based probabilities."""
    print("\n" + "=" * 70)
    print("  EDGE DETECTION -- Margin Regression vs Polymarket")
    print("=" * 70)

    nba_markets = fetch_polymarket_nba()
    if not nba_markets:
        print("\n  No active NBA markets on Polymarket.")
        return

    print(f"\n  Found {len(nba_markets)} NBA markets on Polymarket")

    edges = find_edges(predictor, nba_markets)
    if not edges:
        print("  Could not match any markets to teams.")
        return

    for e in edges[:15]:
        arrow = "^" if e["edge"] > 0 else "v"
        vol = f"${float(e['volume']):,.0f}" if e["volume"] else "N/A"
        spr_str = f" ({e['market_spread']:+g})" if e['market_spread'] != 0 else " (Winner)"
        print(f"\n  {e['question'][:60]}{spr_str}")
        print(f"  Proj: {e['proj_margin']:+.1f} pts  Model: {e['model_prob']*100:.1f}%  Poly: {e['poly_prob']*100:.1f}%")
        print(f"  Edge: {arrow} {abs(e['edge']*100):.1f}%  Bet: {e['bet']}  Kelly: {e['kelly_pct']:.1f}%  Vol: {vol}")

    print()


def cmd_backtest(predictor: NBAPredictor, days: int, limit: int | None = None):
    """Backtest XGBoost predictions with walk-forward validation."""
    print("\n" + "=" * 70)
    print("  BACKTEST -- Walk-forward XGBoost Prediction")
    print("=" * 70)

    # Fetch extra days for warm-up
    all_games = fetch_espn_results(days + 30)
    if not all_games:
        print("\n  No game data for backtesting.")
        return

    sorted_games = sorted(all_games, key=lambda x: x["date"])
    standings = fetch_espn_standings()
    
    test_start_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    train_games = [g for g in sorted_games if g["date"] < test_start_date]
    test_games = [g for g in sorted_games if g["date"] >= test_start_date]
    
    if not train_games:
        train_games = sorted_games[:len(sorted_games)//2]
        test_games = sorted_games[len(sorted_games)//2:]

    # Apply limit if specified (last N games)
    if limit and len(test_games) > limit:
        test_games = test_games[-limit:]

    print(f"  Warm-up games: {len(train_games)}")
    print(f"  Test games:    {len(test_games)}")

    # Initial setup
    elobot = EloSystem()
    last_game: dict[str, str] = {}
    for g in train_games:
        elobot.update(g["winner"], g["loser"], g["team_a"])
        last_game[g["team_a"]] = g["date"]
        last_game[g["team_b"]] = g["date"]

    predictor.elo = elobot
    predictor.train(train_games, standings)

    predictions: list[float] = [] # win probs
    outcomes: list[int] = [] # win/loss
    ae_list: list[float] = [] # absolute errors
    correct = 0
    total = 0

    from collections import defaultdict
    games_by_date = defaultdict(list)
    for g in test_games:
        games_by_date[g["date"]].append(g)
    
    sorted_dates = sorted(games_by_date.keys())
    
    for date_str in sorted_dates:
        day_games = games_by_date[date_str]
        date_obj = datetime.strptime(date_str, "%Y%m%d")
        
        for g in day_games:
            b2b_a = (g["team_a"] in last_game and 
                     (date_obj - datetime.strptime(last_game[g["team_a"]], "%Y%m%d")).days == 1)
            b2b_b = (g["team_b"] in last_game and 
                     (date_obj - datetime.strptime(last_game[g["team_b"]], "%Y%m%d")).days == 1)
            
            proj_margin = predictor.predict_margin(g["team_a"], g["team_b"], is_home=True, b2b_a=b2b_a, b2b_b=b2b_b)
            actual_margin = g["home_score"] - g["away_score"]
            prob = predictor.margin_to_prob(proj_margin, 0)
            actual_win = 1 if actual_margin > 0 else 0
            
            predictions.append(prob)
            outcomes.append(actual_win)
            ae_list.append(abs(proj_margin - actual_margin))
            
            if (proj_margin > 0 and actual_margin > 0) or (proj_margin <= 0 and actual_margin <= 0):
                correct += 1
            total += 1
            
        # Update state after the day's games
        for g in day_games:
            predictor.elo.update(g["winner"], g["loser"], g["team_a"])
            train_games.append(g)
            last_game[g["team_a"]] = g["date"]
            last_game[g["team_b"]] = g["date"]
            
        # Retrain for next day
        predictor.train(train_games, standings)

    accuracy = correct / total if total > 0 else 0
    mae = sum(ae_list) / len(ae_list) if ae_list else 0

    print(f"\n✅ Final Regression Results:")
    print(f"  Games evaluated: {total}")
    print(f"  Win Prediction Accuracy: {accuracy*100:.1f}%")
    print(f"  Mean Absolute Error (MAE): {mae:.2f} points")
    print(f"  Final Model RMSE: {predictor.rmse:.2f} points")
    print()


def main():
    parser = argparse.ArgumentParser(description="NBA Game Predictor")
    parser.add_argument("--train", action="store_true", help="Train model on recent games")
    parser.add_argument("--backtest", action="store_true", help="Backtest Elo predictions")
    parser.add_argument("--edge", action="store_true", help="Find edge vs Polymarket")
    parser.add_argument("--days", type=int, default=60, help="Days of history (default: 60)")
    parser.add_argument("--limit", type=int, help="Limit number of games for backtest")
    parser.add_argument("--json", action="store_true", help="Output JSON for dashboard API")
    parser.add_argument("--train-spread", action="store_true", help="Train spread prediction model")
    parser.add_argument("--days-ahead", type=int, default=0,
                        help="Include future N days of upcoming games (default: 0 = today only)")
    args = parser.parse_args()

    predictor = NBAPredictor()

    # Suppress prints in JSON mode
    if args.json:
        import io as _io
        sys.stdout = _io.StringIO()

    if predictor.load():
        if not args.json:
            print("  Using saved Elo ratings")
        # Always load fresh team stats (PPG, win%, etc.)
        standings = fetch_espn_standings()
        if standings:
            predictor.team_stats = standings
            if not args.json:
                print(f"  Loaded {len(standings)} team stats")
    else:
        _build_elo_from_recent(predictor, args.days)

    # JSON mode: restore stdout and output structured data
    if args.json:
        sys.stdout = sys.__stdout__
        sys.stdout.reconfigure(encoding="utf-8")
        import json as _json
        output = {
            "games": [],
            "next_games": [],
            "next_games_date": None,
            "edges": [],
            "elo_teams": {},
            "calibration": {},
        }

        # Today (or today + N days ahead) games
        if args.days_ahead > 0:
            today_games = fetch_espn_scoreboard_range(days_ahead=args.days_ahead)
        else:
            today_games = fetch_espn_scoreboard()
        standings = fetch_espn_standings()
        predictor.team_stats = standings
        injuries_map = fetch_espn_injuries()
        today_ymd = datetime.now().strftime("%Y%m%d")
        first_future_ymd = None

        # Fetch recent results for B2B detection and rest days
        recent_results = fetch_espn_results(21)
        recent_form = build_recent_team_form(recent_results)
        calibration = load_prediction_calibration_snapshot()
        output["calibration"] = calibration
        last_game: dict[str, str] = {}
        for g in recent_results:
            for _tk in ("team_a", "team_b"):
                _tn = g[_tk]
                if _tn not in last_game or g["date"] > last_game[_tn]:
                    last_game[_tn] = g["date"]
        yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")

        # Load spread model for JSON output
        spread_model = SpreadPredictor()
        has_spread = spread_model.load()

        for g in today_games:
            home, away = g["home"], g["away"]
            game_date = normalize_game_date(g, today_ymd)

            # B2B Detection -- only flag if team played exactly yesterday
            b2b_home = (home in last_game and last_game[home] == yesterday_str)
            b2b_away = (away in last_game and last_game[away] == yesterday_str)

            rest_h = calc_rest_days(home, last_game)
            rest_a = calc_rest_days(away, last_game)

            base_margin = predictor.predict_margin(home, away, is_home=True, b2b_a=b2b_home, b2b_b=b2b_away)
            prob = predictor.margin_to_prob(base_margin, 0)

            game_entry = {
                "game_date": game_date,
                "home": home, "away": away,
                "home_record": g.get("home_record", ""),
                "away_record": g.get("away_record", ""),
                "home_prob": round(prob * 100, 1),
                "away_prob": round((1 - prob) * 100, 1),
                "model_margin": round(base_margin, 1),
                "home_elo": round(predictor.elo.ratings.get(home, 1500)),
                "away_elo": round(predictor.elo.ratings.get(away, 1500)),
                "status": g.get("status", ""),
                "b2b_home": b2b_home,
                "b2b_away": b2b_away,
                "rest_home": rest_h,
                "rest_away": rest_a,
                "injuries": build_game_injuries(home, away, injuries_map),
            }

            raw_spread = spread_model.predict(home, away, standings, predictor.elo, rest_h, rest_a) if has_spread else None
            sp_margin, spread_meta = calibrate_spread_projection(base_margin, raw_spread, calibration)
            total_proj = project_total_points(home, away, standings, recent_form, calibration)

            game_entry["pred_spread"] = round(sp_margin, 1)
            game_entry["spread_model_raw"] = round(raw_spread, 1) if raw_spread is not None else None
            game_entry["spread_bias_adj"] = round(spread_meta["bias_adjustment"], 1)
            game_entry["pred_total"] = round(total_proj["pred_total"], 1)
            game_entry["raw_total"] = round(total_proj["raw_total"], 1)
            game_entry["total_bias_adj"] = round(total_proj["bias_adjustment"], 1)
            game_entry["away_expected"] = round(total_proj["away_expected"], 1)
            game_entry["home_expected"] = round(total_proj["home_expected"], 1)

            if game_date == today_ymd:
                output["games"].append(game_entry)
            else:
                output["next_games"].append(game_entry)
                if first_future_ymd is None:
                    first_future_ymd = game_date

        if first_future_ymd:
            output["next_games_date"] = (
                f"{first_future_ymd[:4]}-{first_future_ymd[4:6]}-{first_future_ymd[6:8]}"
            )

        # Edge detection
        nba_markets = fetch_polymarket_nba()
        if nba_markets:
            edges = find_edges(predictor, nba_markets)
            for e in edges[:20]:
                output["edges"].append({
                    "question": e["question"],
                    "model_prob": round(e["model_prob"] * 100, 1),
                    "poly_prob": round(e["poly_prob"] * 100, 1),
                    "edge": round(e["abs_edge"] * 100, 1),
                    "kelly_pct": round(e.get("kelly_pct", 0), 1),
                    "proj_margin": round(e.get("proj_margin", 0), 1),
                    "pred_winner": e.get("pred_winner", ""),
                    "pred_margin": round(e.get("pred_margin", 0), 1),
                    "bet": e["bet"],
                    "volume": float(e.get("volume", 0) or 0),
                    "liquidity": float(e.get("liquidity", 0) or 0),
                    "slug": e.get("slug", ""),
                })

        # Top Elo teams
        sorted_elo = sorted(predictor.elo.ratings.items(), key=lambda x: x[1], reverse=True)
        for name, rating in sorted_elo[:15]:
            output["elo_teams"][name] = round(rating)

        # Backtest results (walk-forward on recent games)
        elo_before_backtest = predictor.elo.ratings.copy()
        try:
            all_games = fetch_espn_results(args.days)
            sorted_g = sorted(all_games, key=lambda x: x["date"])
            warmup = len(sorted_g) // 3
            test_g = sorted_g[warmup:]
            bt_last = {}
            bt_results = {"total": 0, "correct": 0, "strong": 0, "strong_correct": 0,
                          "vstrong": 0, "vstrong_correct": 0, "star3": 0, "star3_correct": 0,
                          "recent": []}

            for g in test_g:
                home = g["team_a"]
                away = g["team_b"]
                d_obj = datetime.strptime(g["date"], "%Y%m%d")
                b2b_a = home in bt_last and (d_obj - datetime.strptime(bt_last[home], "%Y%m%d")).days == 1
                b2b_b = away in bt_last and (d_obj - datetime.strptime(bt_last[away], "%Y%m%d")).days == 1

                margin = predictor.predict_margin(home, away, is_home=True, b2b_a=b2b_a, b2b_b=b2b_b)
                prob = predictor.margin_to_prob(margin, 0)
                conf = max(prob, 1 - prob)
                pick = home if margin > 0 else away
                actual_margin = g["home_score"] - g["away_score"]
                win = (margin > 0 and actual_margin > 0) or (margin <= 0 and actual_margin <= 0)

                bt_results["total"] += 1
                if win:
                    bt_results["correct"] += 1
                if conf > 0.70:
                    bt_results["strong"] += 1
                    if win: bt_results["strong_correct"] += 1
                if conf > 0.85:
                    bt_results["vstrong"] += 1
                    if win: bt_results["vstrong_correct"] += 1

                elo_diff = abs(predictor.elo.ratings.get(home, 1500) - predictor.elo.ratings.get(away, 1500))
                est_spread = (elo_diff + 100) / 28
                if conf > 0.90 and est_spread < 5:
                    bt_results["star3"] += 1
                    if win: bt_results["star3_correct"] += 1

                bt_results["recent"].append({
                    "date": g["date"],
                    "away": away, "home": home,
                    "conf": round(conf * 100),
                    "pick": pick, "winner": g["winner"],
                    "correct": win,
                    "score": f'{g.get("away_score",0)}-{g.get("home_score",0)}',
                })

                bt_last[home] = g["date"]
                bt_last[away] = g["date"]
                predictor.elo.update(g["winner"], home if g["winner"] != home else away, home)

            bt_results["recent"] = bt_results["recent"][-30:]  # last 30

            def _wr(w, t):
                return round(w / t * 100, 1) if t > 0 else 0

            output["backtest"] = {
                "games_tested": bt_results["total"],
                "all_wr": _wr(bt_results["correct"], bt_results["total"]),
                "strong_count": bt_results["strong"],
                "strong_wr": _wr(bt_results["strong_correct"], bt_results["strong"]),
                "vstrong_count": bt_results["vstrong"],
                "vstrong_wr": _wr(bt_results["vstrong_correct"], bt_results["vstrong"]),
                "star3_count": bt_results["star3"],
                "star3_wr": _wr(bt_results["star3_correct"], bt_results["star3"]),
                "recent": bt_results["recent"],
            }
        except Exception:
            output["backtest"] = None
        finally:
            predictor.elo.ratings = elo_before_backtest

        # Playoff bracket (Monte Carlo advance probs + injury-aware star badges)
        try:
            elo_map = {name: rating for name, rating in predictor.elo.ratings.items()}
            injuries_map = fetch_espn_injuries()
            bracket = build_playoff_bracket(standings, elo_map, injuries=injuries_map)
            output["playoff_bracket"] = bracket
        except Exception as _be:
            output["playoff_bracket"] = {"error": str(_be)}

        print(_json.dumps(output))

        # Write to SQLite (alongside JSON, never blocks JSON pipeline)
        try:
            from nba_db import init_db as _db_init, insert_predictions, insert_elo_snapshot
            from nba_db import insert_daily_performance, insert_backtest_results, DB_PATH as _db
            _db_init(_db)
            _today = datetime.now().strftime("%Y%m%d")
            all_pred_games = output["games"] + output.get("next_games", [])
            insert_predictions(_db, all_pred_games, _today)
            insert_elo_snapshot(_db, output["elo_teams"], _today)
            if output.get("backtest"):
                insert_daily_performance(_db, output["backtest"], _today)
                insert_backtest_results(_db, output["backtest"].get("recent", []), _today)
            print(f"[db] wrote {len(all_pred_games)} predictions + elo to {_db.name}",
                  file=sys.stderr)
        except Exception as _db_err:
            print(f"[warn] DB write failed: {_db_err}", file=sys.stderr)

        return

    if args.train:
        cmd_train(predictor, args.days)

    if args.train_spread:
        print("\n  Training spread prediction model...")
        games = fetch_espn_results(args.days)
        standings = fetch_espn_standings()
        predictor.team_stats = standings
        # Ensure Elo is built
        for g in sorted(games, key=lambda x: x["date"]):
            predictor.elo.update(g["winner"], g["loser"], g.get("home_team"))
        spread_model = SpreadPredictor()
        spread_model.train(games, standings, predictor.elo)
        spread_model.save()
        print("  Spread model saved!")

    if args.backtest:
        cmd_backtest(predictor, args.days, limit=args.limit)

    if not (args.train and not args.edge):
        cmd_today(predictor, days_ahead=args.days_ahead)

    if args.edge:
        cmd_edge(predictor)


if __name__ == "__main__":
    main()

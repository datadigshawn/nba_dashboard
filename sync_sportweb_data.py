#!/usr/bin/env python3
"""
Sync sportWeb sportsbook artifacts into deployable autobots_NBA files.

Outputs:
  - tw_odds.json
  - sportbook_report.json
  - pick_stats.json

Also updates local nba.db odds_lines and recommended_picks for local dashboard use.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from nba_db import (
    DB_PATH,
    get_pick_stats,
    init_db,
    save_recommended_picks,
    upsert_odds,
    verify_pending_picks,
)

BASE_DIR = Path(__file__).resolve().parent
AUTOBOT_ROOT = BASE_DIR.parent
SPORTWEB_DIR = AUTOBOT_ROOT / "sportWeb"
SPORTWEB_ODDS = SPORTWEB_DIR / "data" / "latest_odds.json"
SPORTWEB_DB = SPORTWEB_DIR / "sportWeb.db"
NBA_DATA_FILE = BASE_DIR / "nba_data.json"
TW_ODDS_FILE = BASE_DIR / "tw_odds.json"
SPORTBOOK_REPORT_FILE = BASE_DIR / "sportbook_report.json"
PICK_STATS_FILE = BASE_DIR / "pick_stats.json"

sys.path.insert(0, str(SPORTWEB_DIR / "src"))
from sport_db import edge_backtest as sportweb_edge_backtest  # noqa: E402


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _abbrev_team(name: str) -> str:
    parts = (name or "").split()
    return parts[-1] if len(parts) > 1 else (name or "")[:10]


def _as_float(value, default: float = 0.0) -> float:
    try:
        if value in ("", None):
            raise ValueError
        return float(value)
    except (TypeError, ValueError):
        return default


def _js_num(value) -> str:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{numeric:g}"


def _two_way_probs(left_odds: float | None, right_odds: float | None):
    if not left_odds or not right_odds or left_odds <= 0 or right_odds <= 0:
        return None, None, None
    raw_left = 1 / float(left_odds)
    raw_right = 1 / float(right_odds)
    total = raw_left + raw_right
    return raw_left / total, raw_right / total, total - 1.0


def _pick_primary_line(lines: list[dict], left_key: str, right_key: str) -> float | None:
    best_line = None
    best_score = None
    for line in lines or []:
        market_line = line.get("line")
        left_odds = line.get(left_key)
        right_odds = line.get(right_key)
        left_prob, _, _ = _two_way_probs(left_odds, right_odds)
        if market_line is None or left_prob is None:
            continue
        score = (abs(left_prob - 0.5), abs(float(market_line)))
        if best_score is None or score < best_score:
            best_score = score
            best_line = float(market_line)
    return best_line


def build_tw_odds_payload(snapshot: dict) -> dict:
    fetched_at = snapshot.get("fetched_at", "")
    synced_at = datetime.now().isoformat(timespec="seconds")
    entries = []
    odds_map = {}

    for game in snapshot.get("games", []):
        away = game.get("away", "")
        home = game.get("home", "")
        label = f"{away} @ {home}"
        spreads = game.get("spreads") or []
        totals = game.get("totals") or []
        primary_spread = _pick_primary_line(spreads, "away", "home")
        primary_total = _pick_primary_line(totals, "over", "under")
        row = {
            "game": label,
            "away": away,
            "home": home,
            "spread": primary_spread,
            "ou": primary_total,
            "updated_at": fetched_at or synced_at,
            "source": "sportWeb",
            "sportweb_game_id": game.get("game_id", ""),
            "start_time": game.get("start_time", ""),
        }
        entries.append(row)
        odds_map[label] = {
            "spread": primary_spread,
            "ou": primary_total,
            "updated_at": row["updated_at"],
            "source": "sportWeb",
        }

    return {
        "synced_at": synced_at,
        "source": {
            "sportweb_fetched_at": fetched_at,
            "source_url": snapshot.get("source_url", ""),
            "game_count": len(snapshot.get("games", [])),
        },
        "entries": sorted(entries, key=lambda row: row["game"]),
        "odds": odds_map,
    }


def _latest_detected_edges(conn: sqlite3.Connection, limit: int = 10) -> tuple[str | None, list[dict], dict]:
    latest = conn.execute("SELECT MAX(detected_at) FROM edges").fetchone()[0]
    if not latest:
        return None, [], {}

    edge_cols = {row[1] for row in conn.execute("PRAGMA table_info(edges)")}
    match_quality_expr = "match_quality" if "match_quality" in edge_cols else "'' AS match_quality"

    rows = conn.execute("""
        SELECT game_date, away, home, edge_type, side, picked_team,
               line, edge, model_prob, market_prob, odds, expected_roi, {match_quality_expr}
        FROM edges
        WHERE detected_at = ?
        ORDER BY edge DESC, expected_roi DESC, id DESC
        LIMIT ?
    """.format(match_quality_expr=match_quality_expr), (latest, limit)).fetchall()

    counts = conn.execute("""
        SELECT edge_type, COUNT(*) AS n
        FROM edges
        WHERE detected_at = ?
        GROUP BY edge_type
        ORDER BY n DESC
    """, (latest,)).fetchall()

    top_edges = []
    for row in rows:
        top_edges.append({
            "game_date": row["game_date"],
            "away": row["away"],
            "home": row["home"],
            "edge_type": row["edge_type"],
            "side": row["side"],
            "picked_team": row["picked_team"],
            "line": row["line"],
            "edge": row["edge"],
            "model_prob": row["model_prob"],
            "market_prob": row["market_prob"],
            "odds": row["odds"],
            "expected_roi": row["expected_roi"],
            "match_quality": row["match_quality"],
        })

    by_type = {row["edge_type"]: row["n"] for row in counts}
    return latest, top_edges, by_type


def build_sportbook_report(snapshot: dict, db_path: Path) -> dict:
    synced_at = datetime.now().isoformat(timespec="seconds")
    latest_detected = None
    top_edges: list[dict] = []
    by_type: dict[str, int] = {}

    with sqlite3.connect(str(db_path), timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        latest_detected, top_edges, by_type = _latest_detected_edges(conn, limit=12)

    current_count = sum(by_type.values())
    backtest = sportweb_edge_backtest(db_path)

    return {
        "synced_at": synced_at,
        "source": {
            "sportweb_fetched_at": snapshot.get("fetched_at", ""),
            "sportweb_detected_at": latest_detected,
            "game_count": len(snapshot.get("games", [])),
        },
        "current": {
            "count": current_count,
            "by_type": by_type,
            "top_edges": top_edges,
        },
        "backtest": backtest,
    }


def _build_pick_batch(games: list[dict], odds_map: dict, pick_date: str, limit: int = 6) -> tuple[list[dict], list[dict]]:
    candidates = []

    for game in games or []:
        away = game.get("away", "")
        home = game.get("home", "")
        game_key = f"{away} @ {home}"
        tw = odds_map.get(game_key) or {}
        spread = tw.get("spread")
        total_line = tw.get("ou")
        if spread is None and total_line in (None, ""):
            continue

        bets = []
        home_prob = _as_float(game.get("home_prob"))
        away_prob = _as_float(game.get("away_prob"))
        pred_spread = _as_float(game.get("pred_spread"))
        pred_total = _as_float(game.get("pred_total"))
        home_short = _abbrev_team(home)
        away_short = _abbrev_team(away)

        if spread is not None:
            signed_spread = _as_float(spread)
            if signed_spread < 0:
                line = abs(signed_spread)
                if pred_spread < line - 2:
                    bets.append({
                        "text": f"買 {away_short} 受讓 +{_js_num(line)}",
                        "edge": line - pred_spread,
                        "type": "spread",
                        "target": "away",
                        "line": signed_spread,
                    })
                elif pred_spread > line + 2:
                    bets.append({
                        "text": f"買 {home_short} 讓分 {_js_num(signed_spread)}",
                        "edge": pred_spread - line,
                        "type": "spread",
                        "target": "home",
                        "line": signed_spread,
                    })
            else:
                line = signed_spread
                if pred_spread > -line + 2:
                    bets.append({
                        "text": f"買 {home_short} 受讓 +{_js_num(line)}",
                        "edge": line + pred_spread,
                        "type": "spread",
                        "target": "home",
                        "line": signed_spread,
                    })
                elif pred_spread < -line - 2:
                    bets.append({
                        "text": f"買 {away_short} 讓分 -{_js_num(line)}",
                        "edge": abs(pred_spread) - line,
                        "type": "spread",
                        "target": "away",
                        "line": signed_spread,
                    })

        if total_line not in (None, "") and pred_total:
            total_value = _as_float(total_line)
            delta = pred_total - total_value
            if delta > 5:
                bets.append({
                    "text": f"看大 Over {_js_num(total_value)}",
                    "edge": delta,
                    "type": "ou",
                    "target": "over",
                    "line": total_value,
                })
            elif delta < -5:
                bets.append({
                    "text": f"看小 Under {_js_num(total_value)}",
                    "edge": abs(delta),
                    "type": "ou",
                    "target": "under",
                    "line": total_value,
                })

        if not bets:
            continue

        candidates.append({
            "game": game_key,
            "game_date": game.get("game_date", ""),
            "away": away,
            "home": home,
            "bets": bets,
            "max_edge": max(b["edge"] for b in bets),
            "confidence": max(home_prob, away_prob),
            "tw_spread": spread,
            "tw_ou": total_line,
            "model_spread": game.get("pred_spread"),
            "model_total": game.get("pred_total"),
        })

    candidates.sort(key=lambda row: row["max_edge"], reverse=True)
    preferred = [row for row in candidates if len(row["bets"]) >= 2]
    backup = [row for row in candidates if len(row["bets"]) == 1]
    top = preferred[:limit]
    if len(top) < limit:
        top.extend(backup[:limit - len(top)])

    picks = []
    for candidate in top:
        for bet in candidate["bets"]:
            picks.append({
                "pick_date": pick_date,
                "game_date": candidate["game_date"],
                "game_key": candidate["game"],
                "away": candidate["away"],
                "home": candidate["home"],
                "pick_type": bet["type"],
                "pick_target": bet["target"],
                "pick_line": bet["line"],
                "pick_detail": bet["text"],
                "edge": bet["edge"],
                "confidence": candidate["confidence"],
                "tw_spread": candidate["tw_spread"],
                "tw_ou": candidate["tw_ou"],
                "model_spread": candidate["model_spread"],
                "model_total": candidate["model_total"],
            })

    return top, picks


def build_recommended_picks_payload(nba_data: dict, tw_odds_payload: dict) -> dict:
    pick_date = datetime.now().strftime("%Y%m%d")
    odds_map = tw_odds_payload.get("odds") or {}
    today_games = [
        game for game in (nba_data.get("games") or [])
        if "final" not in str(game.get("status", "")).lower()
    ]
    today_source = today_games if today_games else (nba_data.get("next_games") or [])
    next_games = nba_data.get("next_games") or []

    today_top, today_picks = _build_pick_batch(today_source, odds_map, pick_date)
    next_top, next_picks = _build_pick_batch(next_games, odds_map, pick_date)
    combined = today_picks + next_picks
    deduped = []
    seen = set()
    for pick in combined:
        key = (
            pick.get("pick_date", ""),
            pick.get("game_date", ""),
            pick.get("game_key", ""),
            pick.get("pick_type", ""),
            pick.get("pick_detail", ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(pick)

    return {
        "pick_date": pick_date,
        "today_top_games": today_top,
        "next_top_games": next_top,
        "picks": deduped,
        "meta": {
            "today_candidates": len(today_source),
            "today_top_count": len(today_top),
            "next_candidates": len(next_games),
            "next_top_count": len(next_top),
        },
    }


def build_pick_stats_payload(selection: dict, saved: int, verified: dict, stats: dict) -> dict:
    return {
        "synced_at": datetime.now().isoformat(timespec="seconds"),
        "pick_date": selection.get("pick_date", ""),
        "saved": saved,
        "verified": verified,
        "stats": stats,
        "meta": selection.get("meta") or {},
        "current_picks": selection.get("picks") or [],
    }


def write_json(path: Path, payload: dict):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sync_local_odds_db(tw_odds_payload: dict):
    init_db(DB_PATH)
    saved = 0
    for row in tw_odds_payload.get("entries", []):
        upsert_odds(DB_PATH, row["game"], row.get("spread"), row.get("ou"))
        saved += 1
    return saved


def sync_recommended_picks(nba_data: dict, tw_odds_payload: dict) -> tuple[dict, int, dict, dict]:
    init_db(DB_PATH)
    selection = build_recommended_picks_payload(nba_data, tw_odds_payload)
    saved = save_recommended_picks(DB_PATH, selection.get("picks") or [])
    verified = verify_pending_picks(DB_PATH)
    stats = get_pick_stats(DB_PATH)
    return selection, saved, verified, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-db-sync", action="store_true",
                    help="Do not update local nba.db odds_lines.")
    args = ap.parse_args()

    if not SPORTWEB_ODDS.exists():
        raise SystemExit(f"sportWeb odds file not found: {SPORTWEB_ODDS}")
    if not SPORTWEB_DB.exists():
        raise SystemExit(f"sportWeb db not found: {SPORTWEB_DB}")
    if not NBA_DATA_FILE.exists():
        raise SystemExit(f"nba_data.json not found: {NBA_DATA_FILE}")

    snapshot = _load_json(SPORTWEB_ODDS)
    nba_data = _load_json(NBA_DATA_FILE)
    tw_odds_payload = build_tw_odds_payload(snapshot)
    report_payload = build_sportbook_report(snapshot, SPORTWEB_DB)
    pick_selection, picks_saved, verified, pick_stats = sync_recommended_picks(nba_data, tw_odds_payload)
    pick_stats_payload = build_pick_stats_payload(pick_selection, picks_saved, verified, pick_stats)

    write_json(TW_ODDS_FILE, tw_odds_payload)
    write_json(SPORTBOOK_REPORT_FILE, report_payload)
    write_json(PICK_STATS_FILE, pick_stats_payload)

    db_saved = 0
    if not args.skip_db_sync:
        db_saved = sync_local_odds_db(tw_odds_payload)

    print(json.dumps({
        "ok": True,
        "tw_odds_file": str(TW_ODDS_FILE),
        "sportbook_report_file": str(SPORTBOOK_REPORT_FILE),
        "pick_stats_file": str(PICK_STATS_FILE),
        "games_synced": len(tw_odds_payload.get("entries", [])),
        "db_saved": db_saved,
        "edge_count": report_payload.get("current", {}).get("count", 0),
        "picks_detected": len(pick_selection.get("picks") or []),
        "picks_saved": picks_saved,
        "pick_stats_total": pick_stats.get("total", 0),
        "pick_stats_wr": pick_stats.get("wr", 0),
        "picks_verified": verified.get("verified", 0),
        "sportweb_fetched_at": tw_odds_payload.get("source", {}).get("sportweb_fetched_at"),
        "sportweb_detected_at": report_payload.get("source", {}).get("sportweb_detected_at"),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

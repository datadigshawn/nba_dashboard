#!/usr/bin/env python3
"""
Sync sportWeb sportsbook artifacts into deployable autobots_NBA files.

Outputs:
  - tw_odds.json
  - sportbook_report.json

Also updates local nba.db odds_lines for local dashboard use.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from nba_db import DB_PATH, init_db, upsert_odds

BASE_DIR = Path(__file__).resolve().parent
AUTOBOT_ROOT = BASE_DIR.parent
SPORTWEB_DIR = AUTOBOT_ROOT / "sportWeb"
SPORTWEB_ODDS = SPORTWEB_DIR / "data" / "latest_odds.json"
SPORTWEB_DB = SPORTWEB_DIR / "sportWeb.db"
TW_ODDS_FILE = BASE_DIR / "tw_odds.json"
SPORTBOOK_REPORT_FILE = BASE_DIR / "sportbook_report.json"

sys.path.insert(0, str(SPORTWEB_DIR / "src"))
from sport_db import edge_backtest as sportweb_edge_backtest  # noqa: E402


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


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


def write_json(path: Path, payload: dict):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sync_local_odds_db(tw_odds_payload: dict):
    init_db(DB_PATH)
    saved = 0
    for row in tw_odds_payload.get("entries", []):
        upsert_odds(DB_PATH, row["game"], row.get("spread"), row.get("ou"))
        saved += 1
    return saved


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-db-sync", action="store_true",
                    help="Do not update local nba.db odds_lines.")
    args = ap.parse_args()

    if not SPORTWEB_ODDS.exists():
        raise SystemExit(f"sportWeb odds file not found: {SPORTWEB_ODDS}")
    if not SPORTWEB_DB.exists():
        raise SystemExit(f"sportWeb db not found: {SPORTWEB_DB}")

    snapshot = _load_json(SPORTWEB_ODDS)
    tw_odds_payload = build_tw_odds_payload(snapshot)
    report_payload = build_sportbook_report(snapshot, SPORTWEB_DB)

    write_json(TW_ODDS_FILE, tw_odds_payload)
    write_json(SPORTBOOK_REPORT_FILE, report_payload)

    db_saved = 0
    if not args.skip_db_sync:
        db_saved = sync_local_odds_db(tw_odds_payload)

    print(json.dumps({
        "ok": True,
        "tw_odds_file": str(TW_ODDS_FILE),
        "sportbook_report_file": str(SPORTBOOK_REPORT_FILE),
        "games_synced": len(tw_odds_payload.get("entries", [])),
        "db_saved": db_saved,
        "edge_count": report_payload.get("current", {}).get("count", 0),
        "sportweb_fetched_at": tw_odds_payload.get("source", {}).get("sportweb_fetched_at"),
        "sportweb_detected_at": report_payload.get("source", {}).get("sportweb_detected_at"),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

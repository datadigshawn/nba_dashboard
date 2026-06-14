#!/usr/bin/env python3
"""
Edge Outcome Resolver — 比賽結束後填入 edge 勝負結果。

判定邏輯：
  moneyline: winner_side == edge.side → bet_won
  spread:    actual_margin covers line → bet_won
             away covers -line: away_score - home_score > line
             home covers +line: home_score - away_score > -line (i.e. doesn't lose by > line)
  total:     over: home_score + away_score > line
             under: home_score + away_score < line

資料源：ESPN scoreboard API

用法：
    cd /Users/shawnclaw/autobot/investing/sports/autobots_NBA/sportweb
    .venv/bin/python src/sport_resolve.py
    .venv/bin/python src/sport_resolve.py --days 7   # 查更多天
"""
import argparse
import json
import sqlite3
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "src"))
from sport_db import DB_PATH, init_db, bet_line_for_edge
from schema import parse_game_date_ymd

ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"


def fetch_espn_games(days_back: int = 7) -> list[dict]:
    """Fetch completed NBA games from ESPN over the last N days."""
    games = []
    for offset in range(days_back):
        date = (datetime.now() - timedelta(days=offset)).strftime("%Y%m%d")
        url = f"{ESPN_SCOREBOARD}?dates={date}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
        except Exception as e:
            print(f"  [warn] ESPN {date}: {e}", file=sys.stderr)
            continue

        for ev in data.get("events", []):
            status = ev.get("status", {}).get("type", {})
            if not status.get("completed", False):
                continue
            comp = ev.get("competitions", [{}])[0]
            teams = comp.get("competitors", [])
            away = next((t for t in teams if t.get("homeAway") == "away"), {})
            home = next((t for t in teams if t.get("homeAway") == "home"), {})
            games.append({
                "date": date,
                "home": home.get("team", {}).get("displayName", ""),
                "away": away.get("team", {}).get("displayName", ""),
                "home_score": int(home.get("score", 0)),
                "away_score": int(away.get("score", 0)),
            })
    return games


def _latest_snapshot_for_game(conn: sqlite3.Connection, game_id: str):
    row = conn.execute("""
        SELECT o.snapshot_id, s.fetched_at, o.start_time
        FROM odds o
        JOIN snapshots s ON s.id = o.snapshot_id
        WHERE o.game_id = ?
          AND (
            o.start_time IS NULL OR o.start_time = ''
            OR substr(s.fetched_at, 1, 16) <= substr(o.start_time, 1, 16)
          )
        ORDER BY s.fetched_at DESC, o.id DESC
        LIMIT 1
    """, (game_id,)).fetchone()
    if row:
        return row
    return conn.execute("""
        SELECT o.snapshot_id, s.fetched_at, o.start_time
        FROM odds o
        JOIN snapshots s ON s.id = o.snapshot_id
        WHERE o.game_id = ?
        ORDER BY s.fetched_at DESC, o.id DESC
        LIMIT 1
    """, (game_id,)).fetchone()


def _compute_clv(edge_type: str, side: str, placed_line: float | None,
                 placed_odds: float, closing_line: float | None,
                 closing_odds: float | None, exact_closing_odds: float | None = None):
    clv_line = None
    if edge_type == "spread" and placed_line is not None and closing_line is not None:
        clv_line = round(float(placed_line) - float(closing_line), 3)
    elif edge_type == "total" and placed_line is not None and closing_line is not None:
        if side == "over":
            clv_line = round(float(closing_line) - float(placed_line), 3)
        elif side == "under":
            clv_line = round(float(placed_line) - float(closing_line), 3)

    compare_odds = exact_closing_odds if exact_closing_odds is not None else (
        closing_odds if edge_type == "moneyline" else None
    )
    clv_odds = round(float(placed_odds) - float(compare_odds), 4) if compare_odds is not None else None

    if clv_line is not None and abs(clv_line) > 1e-9:
        clv_win = 1 if clv_line > 0 else 0
    elif clv_odds is not None and abs(clv_odds) > 1e-9:
        clv_win = 1 if clv_odds > 0 else 0
    else:
        clv_win = None
    return clv_line, clv_odds, clv_win


def _closing_moneyline(conn: sqlite3.Connection, game_id: str, side: str):
    latest = _latest_snapshot_for_game(conn, game_id)
    if not latest:
        return None
    row = conn.execute("""
        SELECT ml_away, ml_home
        FROM odds
        WHERE game_id = ? AND snapshot_id = ?
        LIMIT 1
    """, (game_id, latest["snapshot_id"])).fetchone()
    if not row:
        return None
    closing_odds = row["ml_home"] if side == "home" else row["ml_away"]
    return {
        "closing_line": None,
        "closing_odds": closing_odds,
        "exact_closing_odds": closing_odds,
        "closing_snapshot_id": latest["snapshot_id"],
        "closing_fetched_at": latest["fetched_at"],
    }


def _closing_spread(conn: sqlite3.Connection, game_id: str, side: str, home_line: float):
    latest = _latest_snapshot_for_game(conn, game_id)
    if not latest:
        return None
    primary = conn.execute("""
        SELECT home_line, away_odds, home_odds
        FROM odds_spreads
        WHERE game_id = ? AND snapshot_id = ?
        ORDER BY is_primary DESC, ABS(COALESCE(away_prob, 0.5) - 0.5), ABS(home_line), id DESC
        LIMIT 1
    """, (game_id, latest["snapshot_id"])).fetchone()
    if not primary:
        return None
    exact = conn.execute("""
        SELECT away_odds, home_odds
        FROM odds_spreads
        WHERE game_id = ? AND snapshot_id = ? AND ABS(home_line - ?) < 0.0001
        LIMIT 1
    """, (game_id, latest["snapshot_id"], float(home_line))).fetchone()
    return {
        "closing_line": bet_line_for_edge("spread", side, primary["home_line"]),
        "closing_odds": primary["home_odds"] if side == "home" else primary["away_odds"],
        "exact_closing_odds": (
            exact["home_odds"] if side == "home" else exact["away_odds"]
        ) if exact else None,
        "closing_snapshot_id": latest["snapshot_id"],
        "closing_fetched_at": latest["fetched_at"],
    }


def _closing_total(conn: sqlite3.Connection, game_id: str, side: str, total_line: float):
    latest = _latest_snapshot_for_game(conn, game_id)
    if not latest:
        return None
    primary = conn.execute("""
        SELECT total_line, over_odds, under_odds
        FROM odds_totals
        WHERE game_id = ? AND snapshot_id = ?
        ORDER BY is_primary DESC, ABS(COALESCE(over_prob, 0.5) - 0.5), ABS(total_line), id DESC
        LIMIT 1
    """, (game_id, latest["snapshot_id"])).fetchone()
    if not primary:
        return None
    exact = conn.execute("""
        SELECT over_odds, under_odds
        FROM odds_totals
        WHERE game_id = ? AND snapshot_id = ? AND ABS(total_line - ?) < 0.0001
        LIMIT 1
    """, (game_id, latest["snapshot_id"], float(total_line))).fetchone()
    return {
        "closing_line": float(primary["total_line"]),
        "closing_odds": primary["over_odds"] if side == "over" else primary["under_odds"],
        "exact_closing_odds": (
            exact["over_odds"] if side == "over" else exact["under_odds"]
        ) if exact else None,
        "closing_snapshot_id": latest["snapshot_id"],
        "closing_fetched_at": latest["fetched_at"],
    }


def backfill_closing_metrics(db_path: Path | str = DB_PATH) -> int:
    """Populate closing-line / CLV fields for already-resolved edges."""
    init_db(db_path)

    updated = 0
    with sqlite3.connect(str(db_path), timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT id, game_id, side, edge_type, line, bet_line, odds
            FROM edges
            WHERE resolved_at IS NOT NULL
              AND (closing_snapshot_id IS NULL OR bet_line IS NULL)
        """).fetchall()
        if not rows:
            return 0

        for edge in rows:
            edge_type = edge["edge_type"]
            side = edge["side"]
            line = edge["line"]
            placed_line = edge["bet_line"]
            if placed_line is None:
                placed_line = bet_line_for_edge(edge_type, side, line)

            closing = None
            if edge_type == "moneyline":
                closing = _closing_moneyline(conn, edge["game_id"], side)
            elif edge_type == "spread":
                closing = _closing_spread(conn, edge["game_id"], side, line)
            elif edge_type == "total":
                closing = _closing_total(conn, edge["game_id"], side, line)
            if not closing:
                continue

            clv_line, clv_odds, clv_win = _compute_clv(
                edge_type,
                side,
                placed_line,
                edge["odds"],
                closing["closing_line"],
                closing["closing_odds"],
                closing["exact_closing_odds"],
            )
            conn.execute("""
                UPDATE edges
                SET bet_line=?,
                    closing_line=?,
                    closing_odds=?,
                    closing_snapshot_id=?,
                    closing_fetched_at=?,
                    clv_line=?,
                    clv_odds=?,
                    clv_win=?
                WHERE id=?
            """, (
                placed_line,
                closing["closing_line"],
                closing["closing_odds"],
                closing["closing_snapshot_id"],
                closing["closing_fetched_at"],
                clv_line,
                clv_odds,
                clv_win,
                edge["id"],
            ))
            updated += 1
    return updated


def resolve_edges(db_path: Path | str = DB_PATH, days_back: int = 7):
    """Resolve unresolved edges using ESPN results."""
    init_db(db_path)

    with sqlite3.connect(str(db_path), timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        unresolved = conn.execute("""
            SELECT id, game_id, game_date, home, away, side, edge_type,
                   line, bet_line, odds, picked_team
            FROM edges WHERE resolved_at IS NULL
        """).fetchall()

    if not unresolved:
        print("[resolve] 無未解析 edge")
        hydrated = backfill_closing_metrics(db_path)
        if hydrated:
            print(f"[resolve] 補寫 {hydrated} 筆 closing line / CLV")
        return 0

    print(f"[resolve] {len(unresolved)} 筆待解析 edge")

    espn_games = fetch_espn_games(days_back)
    print(f"[resolve] ESPN 取得 {len(espn_games)} 場已完成比賽")

    # Index ESPN games by (date, home, away), fallback by matchup only.
    espn_index = {}
    espn_by_matchup = {}
    for g in espn_games:
        key = (g["date"], g["home"], g["away"])
        espn_index[key] = g
        espn_by_matchup.setdefault(frozenset((g["home"], g["away"])), []).append(g)

    now = datetime.now().isoformat(timespec="seconds")
    resolved = 0

    with sqlite3.connect(str(db_path), timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        for edge in unresolved:
            eid = edge["id"]
            e_game_date = parse_game_date_ymd(edge["game_date"] or "")
            e_home = edge["home"]
            e_away = edge["away"]

            game = None
            if e_game_date:
                game = espn_index.get((e_game_date, e_home, e_away))
                if not game:
                    rev = espn_index.get((e_game_date, e_away, e_home))
                    if rev:
                        game = {**rev, "home": e_home, "away": e_away,
                                "home_score": rev["away_score"], "away_score": rev["home_score"]}
            if not game:
                candidates = espn_by_matchup.get(frozenset((e_home, e_away)), [])
                best = None
                best_gap = 999
                for cand in candidates:
                    cand_date = parse_game_date_ymd(cand["date"])
                    gap = abs((datetime.strptime(cand_date, "%Y%m%d") - datetime.strptime(e_game_date, "%Y%m%d")).days) if e_game_date else 0
                    if gap < best_gap:
                        best_gap = gap
                        best = cand
                if best and best_gap <= 3:
                    if best["home"] == e_home and best["away"] == e_away:
                        game = best
                    else:
                        game = {**best, "home": e_home, "away": e_away,
                                "home_score": best["away_score"], "away_score": best["home_score"]}
            if not game:
                continue

            hs, aws = game["home_score"], game["away_score"]
            total_score = hs + aws
            home_margin = hs - aws  # positive = home won
            winner_side = "home" if home_margin > 0 else "away"

            edge_type = edge["edge_type"]
            side = edge["side"]
            line = edge["line"]
            bet_line = edge["bet_line"]
            odds_val = edge["odds"]
            closing = None
            if edge_type == "moneyline":
                closing = _closing_moneyline(conn, edge["game_id"], side)
            elif edge_type == "spread":
                closing = _closing_spread(conn, edge["game_id"], side, line)
            elif edge_type == "total":
                closing = _closing_total(conn, edge["game_id"], side, line)

            closing_line = closing["closing_line"] if closing else None
            closing_odds = closing["closing_odds"] if closing else None
            closing_snapshot_id = closing["closing_snapshot_id"] if closing else None
            closing_fetched_at = closing["closing_fetched_at"] if closing else None
            exact_closing_odds = closing["exact_closing_odds"] if closing else None
            clv_line, clv_odds, clv_win = _compute_clv(
                edge_type,
                side,
                bet_line,
                odds_val,
                closing_line,
                closing_odds,
                exact_closing_odds,
            )

            # Determine bet outcome
            if edge_type == "moneyline":
                bet_won = 1 if side == winner_side else 0
                actual_profit = round((odds_val - 1.0) if bet_won else -1.0, 4)
            elif edge_type == "spread":
                adjusted = home_margin + float(line)
                if adjusted == 0:
                    bet_won = None
                    actual_profit = 0.0
                elif side == "away":
                    bet_won = 1 if adjusted < 0 else 0
                    actual_profit = round((odds_val - 1.0) if bet_won else -1.0, 4)
                else:
                    bet_won = 1 if adjusted > 0 else 0
                    actual_profit = round((odds_val - 1.0) if bet_won else -1.0, 4)
            elif edge_type == "total":
                if side == "over":
                    if total_score == float(line):
                        bet_won = None
                        actual_profit = 0.0
                    else:
                        bet_won = 1 if total_score > line else 0
                        actual_profit = round((odds_val - 1.0) if bet_won else -1.0, 4)
                else:
                    if total_score == float(line):
                        bet_won = None
                        actual_profit = 0.0
                    else:
                        bet_won = 1 if total_score < line else 0
                        actual_profit = round((odds_val - 1.0) if bet_won else -1.0, 4)
            else:
                continue

            conn.execute("""
                UPDATE edges SET
                    actual_winner_side=?, bet_won=?, actual_profit=?, resolved_at=?,
                    closing_line=?, closing_odds=?, closing_snapshot_id=?, closing_fetched_at=?,
                    clv_line=?, clv_odds=?, clv_win=?
                WHERE id=?
            """, (
                winner_side,
                bet_won,
                actual_profit,
                now,
                closing_line,
                closing_odds,
                closing_snapshot_id,
                closing_fetched_at,
                clv_line,
                clv_odds,
                clv_win,
                eid,
            ))

            # Also insert into game_outcomes (if not exists)
            conn.execute("""
                INSERT OR IGNORE INTO game_outcomes
                (game_id, home, away, home_score, away_score, winner_side, resolved_at)
                VALUES (?,?,?,?,?,?,?)
            """, (edge["game_id"], e_home, e_away, hs, aws, winner_side, now))

            resolved += 1
            symbol = "➖" if bet_won is None else ("✅" if bet_won else "❌")
            print(f"  {symbol} [{edge_type}] {edge['picked_team']} → "
                  f"{e_away} {aws}-{hs} {e_home} | profit: {actual_profit:+.2f}")

    print(f"\n[resolve] 解析完成：{resolved}/{len(unresolved)} 筆")
    hydrated = backfill_closing_metrics(db_path)
    if hydrated:
        print(f"[resolve] 補寫 {hydrated} 筆 closing line / CLV")
    return resolved


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="ESPN look-back days (default 7)")
    args = ap.parse_args()
    resolve_edges(DB_PATH, days_back=args.days)


if __name__ == "__main__":
    main()

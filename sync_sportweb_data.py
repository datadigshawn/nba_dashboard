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
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from nba_db import (
    DB_PATH,
    init_db,
    save_recommended_picks,
    upsert_odds,
)
from pick_history import sync_pick_history

BASE_DIR = Path(__file__).resolve().parent
SPORTWEB_DIR = BASE_DIR / "sportweb"
SPORTWEB_ODDS = SPORTWEB_DIR / "data" / "latest_odds.json"
SPORTWEB_DB = SPORTWEB_DIR / "sportWeb.db"
NBA_DATA_FILE = BASE_DIR / "nba_data.json"
TW_ODDS_FILE = BASE_DIR / "tw_odds.json"
SPORTBOOK_REPORT_FILE = BASE_DIR / "sportbook_report.json"
PICK_STATS_FILE = BASE_DIR / "pick_stats.json"
ALERT_LOG_PATH = BASE_DIR / "logs" / "alerts.log"

# 盤口新鮮度上限：fetch 失敗時 blob_fetcher 不覆寫 latest_odds.json，會留下舊線。
# 階段2 walk-forward 發現實盤押到平均差 ~2 分的過期盤口正是虧損主因，
# 故超過此時數的盤口一律不拿來建新 picks（寧可跳過不下注）。
MAX_ODDS_AGE_HOURS = float(os.environ.get("NBA_MAX_ODDS_AGE_HOURS", "18"))


def log_alert(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [sync_sportweb] {msg}"
    print(line, file=sys.stderr)
    try:
        ALERT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with ALERT_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def odds_age_hours(snapshot: dict) -> float | None:
    """latest_odds.json 的 fetched_at 距今幾小時；無法解析回 None。"""
    raw = snapshot.get("fetched_at") or ""
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return (datetime.now() - datetime.strptime(raw[:19], fmt)).total_seconds() / 3600
        except ValueError:
            continue
    return None


def odds_are_fresh(snapshot: dict) -> tuple[bool, str]:
    """判斷盤口是否夠新鮮可用於下注。回傳 (是否新鮮, 原因)。"""
    if snapshot.get("error"):
        return False, f"snapshot error: {str(snapshot['error'])[:80]}"
    age = odds_age_hours(snapshot)
    if age is None:
        return False, "fetched_at 無法解析"
    if age > MAX_ODDS_AGE_HOURS:
        return False, f"盤口已 {age:.1f}h 未更新（上限 {MAX_ODDS_AGE_HOURS:.0f}h）"
    return True, f"{age:.1f}h"

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


def _pick_primary_line_row(lines: list[dict], left_key: str, right_key: str) -> dict | None:
    """選主要盤口線（兩邊機率最接近 50/50），回傳完整 line dict（含賠率）。"""
    best_row = None
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
            best_row = line
    return best_row


def _pick_primary_line(lines: list[dict], left_key: str, right_key: str) -> float | None:
    row = _pick_primary_line_row(lines, left_key, right_key)
    if row is None or row.get("line") is None:
        return None
    return float(row["line"])


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
        primary_spread_row = _pick_primary_line_row(spreads, "away", "home")
        primary_total_row = _pick_primary_line_row(totals, "over", "under")
        primary_spread = float(primary_spread_row["line"]) if primary_spread_row else None
        primary_total = float(primary_total_row["line"]) if primary_total_row else None
        spread_odds = {
            "away": (primary_spread_row or {}).get("away"),
            "home": (primary_spread_row or {}).get("home"),
        }
        ou_odds = {
            "over": (primary_total_row or {}).get("over"),
            "under": (primary_total_row or {}).get("under"),
        }
        row = {
            "game": label,
            "away": away,
            "home": home,
            "spread": primary_spread,
            "ou": primary_total,
            "spread_odds": spread_odds,
            "ou_odds": ou_odds,
            "updated_at": fetched_at or synced_at,
            "source": "sportWeb",
            "sportweb_game_id": game.get("game_id", ""),
            "start_time": game.get("start_time", ""),
        }
        entries.append(row)
        odds_map[label] = {
            "spread": primary_spread,
            "ou": primary_total,
            "spread_odds": spread_odds,
            "ou_odds": ou_odds,
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

        # 補上每個 bet 在下注當下的十進位賠率（供 pnl_units 結算）
        spread_odds = tw.get("spread_odds") or {}
        ou_odds = tw.get("ou_odds") or {}
        for bet in bets:
            if bet["type"] == "spread":
                bet["odds_at_pick"] = spread_odds.get(bet["target"])
            elif bet["type"] == "ou":
                bet["odds_at_pick"] = ou_odds.get(bet["target"])

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
                "odds_at_pick": bet.get("odds_at_pick"),
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


def sync_recommended_picks(nba_data: dict, tw_odds_payload: dict,
                           odds_fresh: bool = True) -> tuple[dict, int, dict, dict]:
    init_db(DB_PATH)
    selection = build_recommended_picks_payload(nba_data, tw_odds_payload)
    # 新鮮度防護：盤口過期時不寫入新 picks，避免基於過期線下注（階段2 發現的虧損主因）。
    # 仍照常結算既有 picks、刷新統計。
    if odds_fresh:
        saved = save_recommended_picks(DB_PATH, selection.get("picks") or [])
    else:
        saved = 0
        selection["picks"] = []
        selection.setdefault("meta", {})["skipped_stale_odds"] = True
    pick_history = sync_pick_history(DB_PATH)
    verified = pick_history["resolved"]
    stats = pick_history["stats"]
    stats["history_sync"] = {
        "imported_from_bets": pick_history.get("imported_from_bets") or {},
        "manual_results": pick_history.get("manual_results", 0),
        "resolved": verified,
    }
    return selection, saved, verified, stats


def enrich_nba_data_with_official_picks(nba_data: dict, tw_odds_payload: dict, pick_stats: dict) -> dict:
    try:
        from nba_predictor import build_official_recommendations
    except Exception as exc:
        print(f"[sync] official picks enrichment skipped: {exc}", file=sys.stderr)
        return nba_data

    games = [
        game for game in (nba_data.get("games") or []) + (nba_data.get("next_games") or [])
        if "final" not in str(game.get("status", "")).lower()
    ]
    nba_data["official_picks"] = build_official_recommendations(
        games,
        tw_odds_payload.get("odds") or {},
        pick_stats,
    )
    nba_data["official_picks_updated_at"] = datetime.now().isoformat(timespec="seconds")
    return nba_data


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

    fresh, reason = odds_are_fresh(snapshot)
    if not fresh:
        log_alert(f"盤口不新鮮，本輪不產生新 picks：{reason}")
    else:
        print(f"[sync] 盤口新鮮度 OK（{reason}）")

    tw_odds_payload = build_tw_odds_payload(snapshot)
    tw_odds_payload["fresh"] = fresh
    report_payload = build_sportbook_report(snapshot, SPORTWEB_DB)
    pick_selection, picks_saved, verified, pick_stats = sync_recommended_picks(
        nba_data, tw_odds_payload, odds_fresh=fresh)
    pick_stats_payload = build_pick_stats_payload(pick_selection, picks_saved, verified, pick_stats)
    # 盤口過期時，dashboard 的 official_picks 也不重建（避免顯示基於舊線的推薦）
    official_odds = (tw_odds_payload.get("odds") or {}) if fresh else {}
    nba_data = enrich_nba_data_with_official_picks(nba_data, {"odds": official_odds}, pick_stats)

    write_json(TW_ODDS_FILE, tw_odds_payload)
    write_json(SPORTBOOK_REPORT_FILE, report_payload)
    write_json(PICK_STATS_FILE, pick_stats_payload)
    write_json(NBA_DATA_FILE, nba_data)

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

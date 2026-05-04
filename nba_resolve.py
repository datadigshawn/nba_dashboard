#!/usr/bin/env python3
"""
Outcome Resolver — 查 ESPN 填入昨日比賽結果到 nba.db。

用法（每天跑一次，放在 nba_daily_update.sh 最後）：
    .venv/bin/python nba_resolve.py
"""
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from nba_db import (
    DB_PATH,
    get_pending_pick_dates,
    get_unresolved_dates,
    init_db,
    resolve_outcomes,
)
from pick_history import sync_pick_history


def main():
    init_db(DB_PATH)
    unresolved_dates = get_unresolved_dates(DB_PATH)
    pending_pick_dates = get_pending_pick_dates(DB_PATH)
    dates = sorted(set(unresolved_dates + pending_pick_dates))
    if not dates:
        print("[resolve] 無未解析的比賽")
        pick_history = sync_pick_history(DB_PATH)
        resolved = pick_history["resolved"]
        if resolved["verified"]:
            print(
                "[resolve] 正式推薦結算 "
                f"{resolved['verified']} 筆 "
                f"(W{resolved['wins']} L{resolved['losses']} P{resolved['pushes']})"
            )
        return

    stale_dates = [d for d in dates if d < datetime.now().strftime("%Y%m%d")]
    if stale_dates:
        print(f"[resolve] 逾期優先處理: {stale_dates}")
    print(f"[resolve] 需解析 {len(dates)} 個日期: {dates}")

    try:
        from nba_predictor import fetch_espn_results
    except ImportError:
        print("[resolve] 無法匯入 nba_predictor，跳過")
        return

    oldest = min(dates) if dates else datetime.now().strftime("%Y%m%d")
    oldest_dt = datetime.strptime(oldest, "%Y%m%d")
    days_back = max(60, (datetime.now() - oldest_dt).days + 7)
    print(f"[resolve] ESPN 查詢視窗: 最近 {days_back} 天")
    results_raw = fetch_espn_results(days_back)
    resolve_list = []
    for g in results_raw:
        resolve_list.append({
            "date": g.get("date", ""),
            "home": g.get("team_a", ""),
            "away": g.get("team_b", ""),
            "home_score": g.get("home_score"),
            "away_score": g.get("away_score"),
            "winner": g.get("winner", ""),
        })

    if resolve_list:
        resolve_outcomes(DB_PATH, resolve_list)
        print(f"[resolve] 處理 {len(resolve_list)} 場 ESPN 結果")
        pick_history = sync_pick_history(DB_PATH, resolve_list)
        pick_stats = pick_history["resolved"]
        print(
            "[resolve] 正式推薦結算 "
            f"{pick_stats['verified']} 筆 "
            f"(W{pick_stats['wins']} L{pick_stats['losses']} P{pick_stats['pushes']}, "
            f"missing {pick_stats['missing_results']})"
        )
        if pick_stats.get("missing_details"):
            print("[resolve] 缺漏原因:")
            for row in pick_stats["missing_details"][:10]:
                print(
                    f"  - {row['game_date']} {row['away']} @ {row['home']} "
                    f"[{row['pick_type']}] {row['reason']}"
                )
    else:
        print("[resolve] ESPN 無結果可用")


if __name__ == "__main__":
    main()

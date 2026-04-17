#!/usr/bin/env python3
"""
Outcome Resolver — 查 ESPN 填入昨日比賽結果到 nba.db。

用法（每天跑一次，放在 nba_daily_update.sh 最後）：
    .venv/bin/python nba_resolve.py
"""
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from nba_db import DB_PATH, init_db, get_unresolved_dates, resolve_outcomes


def main():
    init_db(DB_PATH)
    dates = get_unresolved_dates(DB_PATH)
    if not dates:
        print("[resolve] 無未解析的比賽")
        return

    print(f"[resolve] 需解析 {len(dates)} 個日期: {dates}")

    try:
        from nba_predictor import fetch_espn_results
    except ImportError:
        print("[resolve] 無法匯入 nba_predictor，跳過")
        return

    results_raw = fetch_espn_results(30)
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
    else:
        print("[resolve] ESPN 無結果可用")


if __name__ == "__main__":
    main()

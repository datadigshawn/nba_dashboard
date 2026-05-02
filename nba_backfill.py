#!/usr/bin/env python3
"""
一次性回填：把現有 nba_data.json + ESPN 歷史寫入 nba.db。

用法：
    cd /Users/shawnclaw/autobot/autobots_NBA
    .venv/bin/python nba_backfill.py
"""
import json
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from nba_db import DB_PATH, init_db, insert_predictions, insert_elo_snapshot
from nba_db import insert_daily_performance, insert_backtest_results, db_summary, resolve_recommended_picks


def main():
    print(f"[backfill] 初始化 {DB_PATH}")
    init_db(DB_PATH)

    # 1) 從 nba_data.json 回填
    nba_file = BASE_DIR / "nba_data.json"
    if not nba_file.exists():
        print("[backfill] nba_data.json 不存在，跳過")
    else:
        d = json.loads(nba_file.read_text())
        today = datetime.now().strftime("%Y%m%d")

        games = d.get("games", [])
        if games:
            insert_predictions(DB_PATH, games, today)
            print(f"  predictions: {len(games)} 場")

        elo = d.get("elo_teams", {})
        if elo:
            insert_elo_snapshot(DB_PATH, elo, today)
            print(f"  elo_history: {len(elo)} 隊")

        bt = d.get("backtest")
        if bt:
            insert_daily_performance(DB_PATH, bt, today)
            recent = bt.get("recent", [])
            if recent:
                insert_backtest_results(DB_PATH, recent, today)
                print(f"  backtest_results: {len(recent)} 場")

    # 2) 嘗試用 ESPN 結果回填 outcome
    try:
        from nba_predictor import fetch_espn_results
        from nba_db import resolve_outcomes

        print("[backfill] 抓 ESPN 近 30 天結果做 outcome resolve...")
        results = fetch_espn_results(30)
        resolve_list = []
        for g in results:
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
            print(f"  resolve: {len(resolve_list)} 場歷史結果")
            pick_stats = resolve_recommended_picks(DB_PATH, resolve_list)
            print(
                "  recommended_picks: "
                f"{pick_stats['verified']} 筆正式推薦已結算 "
                f"(W{pick_stats['wins']} L{pick_stats['losses']} P{pick_stats['pushes']}, "
                f"missing {pick_stats['missing_results']})"
            )
    except Exception as e:
        print(f"  [warn] ESPN resolve 失敗: {e}")

    # 3) 摘要
    s = db_summary(DB_PATH)
    print(f"\n[backfill] 完成 — {DB_PATH}")
    for k, v in s.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()

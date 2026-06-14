"""
回填歷史 recommended_picks 的 odds_at_pick 與 pnl_units。

資料來源：sportweb/sportWeb.db 的 odds_spreads / odds_totals 歷史快照。
對齊邏輯：
  1. 先找 pick_date 當天、同隊伍、同盤口線的快照（多筆取最接近 09:20 pipeline 時間者）
  2. 當天沒有 → 放寬到 pick_date ~ game_date 之間的任意快照（取最晚者，最接近實際下注情境）
  3. 仍無 → 保持 NULL（不以猜測賠率污染損益）

執行：
  .venv/bin/python backfill_pick_odds.py            # 實際回填
  .venv/bin/python backfill_pick_odds.py --dry-run  # 只印統計
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from nba_db import DB_PATH, compute_pnl_units

SPORTWEB_DB = Path(__file__).resolve().parent / "sportweb" / "sportWeb.db"
PIPELINE_TIME = "09:20:00"  # picks 由每日 09:20 betting pipeline 產生


def _fetch_odds(sw: sqlite3.Connection, pick: sqlite3.Row) -> tuple[float | None, str]:
    """回傳 (decimal odds, 來源說明)。"""
    if pick["pick_type"] == "spread":
        table, line_col = "odds_spreads", "home_line"
        odds_col = "away_odds" if pick["pick_target"] == "away" else "home_odds"
    elif pick["pick_type"] == "ou":
        table, line_col = "odds_totals", "total_line"
        odds_col = "over_odds" if pick["pick_target"] == "over" else "under_odds"
    else:
        return None, "unsupported_type"

    line = pick["pick_line"]
    if line is None:
        return None, "missing_line"

    pick_day = f"{pick['pick_date'][:4]}-{pick['pick_date'][4:6]}-{pick['pick_date'][6:8]}"
    pick_ts = f"{pick_day}T{PIPELINE_TIME}"

    # 同隊伍 + 同盤口線，取「最接近下注時間」的快照。
    # 時間窗往前放寬 14 天：fetch 失敗時 pipeline 會沿用舊 tw_odds.json，
    # pick 上的線可能來自數天前的快照——那正是當時系統看到的賠率。
    row = sw.execute(f"""
        SELECT t.{odds_col} AS odds, substr(s.fetched_at, 1, 10) AS snap_day
        FROM {table} t JOIN snapshots s ON s.id = t.snapshot_id
        WHERE t.away = ? AND t.home = ? AND t.{line_col} = ?
          AND replace(substr(s.fetched_at, 1, 10), '-', '')
              BETWEEN CAST(strftime('%Y%m%d', ?, '-14 days') AS TEXT) AND ?
          AND t.{odds_col} IS NOT NULL
        ORDER BY ABS(strftime('%s', s.fetched_at) - strftime('%s', ?))
        LIMIT 1
    """, (pick["away"], pick["home"], float(line),
          pick_day, pick["game_date"], pick_ts)).fetchone()
    if row and row["odds"]:
        return float(row["odds"]), ("same_day" if row["snap_day"] == pick_day else "window")

    return None, "not_found"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    nba = sqlite3.connect(str(DB_PATH))
    nba.row_factory = sqlite3.Row
    sw = sqlite3.connect(str(SPORTWEB_DB))
    sw.row_factory = sqlite3.Row

    picks = nba.execute("""
        SELECT * FROM recommended_picks
        WHERE odds_at_pick IS NULL
        ORDER BY pick_date, id
    """).fetchall()

    stats = {"total": len(picks), "same_day": 0, "window": 0, "not_found": 0,
             "missing_line": 0, "unsupported_type": 0, "pnl_updated": 0}
    updates = []
    for pick in picks:
        odds, source = _fetch_odds(sw, pick)
        stats[source] += 1
        if odds is None:
            continue
        pnl = compute_pnl_units(pick["result"], odds)
        updates.append((odds, pnl, pick["id"]))
        if pnl is not None:
            stats["pnl_updated"] += 1

    print(f"待回填 picks: {stats['total']}")
    print(f"  當天快照命中: {stats['same_day']}")
    print(f"  區間快照命中: {stats['window']}")
    print(f"  找不到對應賠率: {stats['not_found']}")
    if stats["missing_line"] or stats["unsupported_type"]:
        print(f"  缺線/不支援: {stats['missing_line']}/{stats['unsupported_type']}")
    print(f"  可計算損益: {stats['pnl_updated']}")

    if args.dry_run:
        print("(dry-run，未寫入)")
        return

    with nba:
        nba.executemany(
            "UPDATE recommended_picks SET odds_at_pick = ?, pnl_units = ? WHERE id = ?",
            updates,
        )
    print(f"已寫入 {len(updates)} 筆 odds_at_pick")

    row = nba.execute("""
        SELECT COUNT(*) AS n, COALESCE(SUM(pnl_units), 0) AS pnl,
               SUM(CASE WHEN pnl_units IS NOT NULL THEN 1 ELSE 0 END) AS with_pnl
        FROM recommended_picks WHERE correct IN (0, 1)
    """).fetchone()
    print(f"\n=== 已結算 picks 損益 ===")
    print(f"已結算 {row['n']} 筆，其中 {row['with_pnl']} 筆有賠率")
    print(f"累積損益: {row['pnl']:+.2f} 單位（每注 1 單位）")
    if row["with_pnl"]:
        print(f"平均每注 ROI: {row['pnl'] / row['with_pnl'] * 100:+.1f}%")


if __name__ == "__main__":
    main()

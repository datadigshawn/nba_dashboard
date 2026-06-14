#!/usr/bin/env python3
"""
一次性回填：把現有 data/odds_*.json 寫入 sportWeb.db。

用法：
    cd /Users/shawnclaw/autobot/investing/sports/autobots_NBA/sportweb
    .venv/bin/python src/backfill_db.py
"""
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "src"))

from sport_db import DB_PATH, init_db, insert_snapshot, db_summary


def main():
    print(f"[backfill] 初始化 {DB_PATH}")
    init_db(DB_PATH)

    data_dir = BASE_DIR / "data"
    files = sorted(data_dir.glob("odds_*.json"))
    print(f"[backfill] 找到 {len(files)} 個 JSON 快照")

    inserted = 0
    skipped = 0
    for f in files:
        try:
            snap = json.loads(f.read_text(encoding="utf-8"))
            sid = insert_snapshot(DB_PATH, snap)
            if sid:
                inserted += 1
            else:
                skipped += 1
        except Exception as e:
            print(f"  [warn] {f.name}: {e}")

    print(f"[backfill] 新增 {inserted} 筆，重複跳過 {skipped} 筆")

    s = db_summary(DB_PATH)
    print(f"\n[backfill] 完成 — {DB_PATH}")
    for k, v in s.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()

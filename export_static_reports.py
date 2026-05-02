#!/usr/bin/env python3
"""Export DB-backed dashboard reports for static hosting."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from nba_db import DB_PATH, get_pick_stats, get_prediction_summary, init_db, resolve_recommended_picks

BASE_DIR = Path(__file__).resolve().parent


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    init_db(DB_PATH)
    try:
        resolve_recommended_picks(DB_PATH)
    except Exception as exc:
        print(f"[warn] pick verification skipped: {exc}")

    generated_at = datetime.now().isoformat(timespec="seconds")

    performance = get_prediction_summary(DB_PATH)
    performance["generated_at"] = generated_at
    _write_json(BASE_DIR / "performance_summary.json", performance)

    pick_stats = get_pick_stats(DB_PATH)
    pick_stats["generated_at"] = generated_at
    pick_stats["synced_at"] = generated_at
    _write_json(BASE_DIR / "pick_stats.json", pick_stats)

    print("[static] wrote performance_summary.json and pick_stats.json")


if __name__ == "__main__":
    main()

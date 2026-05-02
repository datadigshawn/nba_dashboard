#!/usr/bin/env python3
"""Export DB-backed dashboard reports for static hosting."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from nba_db import DB_PATH, get_prediction_summary, init_db
from pick_history import sync_pick_history

BASE_DIR = Path(__file__).resolve().parent


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    init_db(DB_PATH)
    try:
        pick_history = sync_pick_history(DB_PATH)
    except Exception as exc:
        print(f"[warn] pick verification skipped: {exc}")
        pick_history = {"stats": {}, "imported_from_bets": {}, "manual_results": 0, "resolved": {}}

    generated_at = datetime.now().isoformat(timespec="seconds")

    performance = get_prediction_summary(DB_PATH)
    performance["generated_at"] = generated_at
    _write_json(BASE_DIR / "performance_summary.json", performance)

    pick_stats = pick_history["stats"]
    pick_stats["history_sync"] = {
        "imported_from_bets": pick_history.get("imported_from_bets") or {},
        "manual_results": pick_history.get("manual_results", 0),
        "resolved": pick_history.get("resolved") or {},
    }
    pick_stats["generated_at"] = generated_at
    pick_stats["synced_at"] = generated_at
    _write_json(BASE_DIR / "pick_stats.json", pick_stats)

    print("[static] wrote performance_summary.json and pick_stats.json")


if __name__ == "__main__":
    main()

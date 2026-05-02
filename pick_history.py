#!/usr/bin/env python3
"""Synchronize NBA official-pick history from durable local sources."""
from __future__ import annotations

import json
from pathlib import Path

from nba_db import (
    DB_PATH,
    get_pick_stats,
    import_recommended_picks_from_bets,
    init_db,
    resolve_recommended_picks,
)

BASE_DIR = Path(__file__).resolve().parent
OVERRIDES_FILE = BASE_DIR / "pick_result_overrides.json"


def load_result_overrides(path: Path = OVERRIDES_FILE) -> list[dict]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("results") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError(f"{path.name} must be a list or an object with a results list")

    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        game_date = row.get("date") or row.get("game_date") or ""
        home = row.get("home") or row.get("home_team") or row.get("team_a") or ""
        away = row.get("away") or row.get("away_team") or row.get("team_b") or ""
        home_score = row.get("home_score")
        away_score = row.get("away_score")
        if not (game_date and home and away):
            continue
        if home_score is None or away_score is None:
            continue
        home_score = int(home_score)
        away_score = int(away_score)
        normalized.append({
            "date": str(game_date),
            "home": str(home),
            "away": str(away),
            "home_score": home_score,
            "away_score": away_score,
            "winner": row.get("winner") or (home if home_score > away_score else away),
            "source": row.get("source") or "manual_override",
        })
    return normalized


def sync_pick_history(
    db_path: Path | str = DB_PATH,
    results: list[dict] | None = None,
    *,
    include_overrides: bool = True,
    import_bets: bool = True,
    overrides_path: Path = OVERRIDES_FILE,
) -> dict:
    init_db(db_path)
    import_stats = (
        import_recommended_picks_from_bets(db_path)
        if import_bets
        else {"candidates": 0, "imported": 0, "skipped_existing": 0}
    )

    combined_results = []
    if include_overrides:
        combined_results.extend(load_result_overrides(overrides_path))
    combined_results.extend(results or [])

    resolve_stats = resolve_recommended_picks(db_path, combined_results or None)
    stats = get_pick_stats(db_path)
    return {
        "imported_from_bets": import_stats,
        "manual_results": len(load_result_overrides(overrides_path)) if include_overrides else 0,
        "resolved": resolve_stats,
        "stats": stats,
    }


def main() -> None:
    payload = sync_pick_history(DB_PATH)
    imported = payload["imported_from_bets"]
    resolved = payload["resolved"]
    stats = payload["stats"]
    print(
        "[pick-history] "
        f"bets imported={imported['imported']} skipped={imported['skipped_existing']} "
        f"manual_results={payload['manual_results']} "
        f"resolved={resolved['verified']} "
        f"stats={stats['wins']}/{stats['total']} pending={stats['pending']} "
        f"stale={stats['stale_pending']}"
    )


if __name__ == "__main__":
    main()

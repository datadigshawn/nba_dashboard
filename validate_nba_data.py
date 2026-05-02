#!/usr/bin/env python3
"""Validate deployable NBA dashboard JSON snapshots."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _failures_for_nba_data(path: Path, *, allow_empty: bool = False) -> list[str]:
    failures: list[str] = []
    raw = path.read_text(encoding="utf-8")
    if "[warn]" in raw or "Traceback" in raw:
        failures.append(f"{path.name} contains warning/traceback text")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [f"{path.name} is not valid JSON: {exc}"]

    games = data.get("games") or []
    next_games = data.get("next_games") or []
    official_picks = data.get("official_picks") or []
    edges = data.get("edges") or []
    if not isinstance(games, list):
        failures.append("games must be a list")
        games = []
    if not isinstance(next_games, list):
        failures.append("next_games must be a list")
        next_games = []
    if not isinstance(official_picks, list):
        failures.append("official_picks must be a list")
        official_picks = []
    if not isinstance(edges, list):
        failures.append("edges must be a list")
        edges = []

    if not allow_empty and len(games) + len(next_games) == 0:
        failures.append("games + next_games is 0; refusing to deploy an empty schedule")

    if data.get("next_games_date") and not isinstance(data.get("next_games_date"), str):
        failures.append("next_games_date must be a string or null")

    for pick in official_picks:
        if not isinstance(pick, dict):
            failures.append("official_picks entries must be objects")
            continue
        for key in ("game_date", "matchup", "pick_detail", "pick_type", "edge"):
            if key not in pick:
                failures.append(f"official pick missing {key}")
                break

    return failures


def _failures_for_json(path: Path) -> list[str]:
    raw = path.read_text(encoding="utf-8")
    if "[warn]" in raw or "Traceback" in raw:
        return [f"{path.name} contains warning/traceback text"]
    try:
        json.loads(raw)
    except json.JSONDecodeError as exc:
        return [f"{path.name} is not valid JSON: {exc}"]
    return []


def _failures_for_result_overrides(path: Path) -> list[str]:
    failures = _failures_for_json(path)
    if failures:
        return failures
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("results") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return [f"{path.name} must be a list or an object with a results list"]
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            failures.append(f"{path.name} result #{i + 1} must be an object")
            continue
        for key in ("game_date", "home", "away", "home_score", "away_score"):
            if key not in row and not (key == "game_date" and "date" in row):
                failures.append(f"{path.name} result #{i + 1} missing {key}")
                break
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate NBA dashboard deploy data")
    parser.add_argument("--base-dir", default=Path(__file__).resolve().parent)
    parser.add_argument("--allow-empty", action="store_true",
                        help="Allow nba_data.json to have no games. Use only for offseason/manual checks.")
    args = parser.parse_args()

    base = Path(args.base_dir)
    required = ["nba_data.json"]
    optional = ["tw_odds.json", "sportbook_report.json", "pick_stats.json", "performance_summary.json"]
    failures: list[str] = []

    for name in required:
        path = base / name
        if not path.exists():
            failures.append(f"missing required file: {name}")
            continue
        if name == "nba_data.json":
            failures.extend(_failures_for_nba_data(path, allow_empty=args.allow_empty))
        else:
            failures.extend(_failures_for_json(path))

    for name in optional:
        path = base / name
        if path.exists():
            failures.extend(_failures_for_json(path))

    overrides = base / "pick_result_overrides.json"
    if overrides.exists():
        failures.extend(_failures_for_result_overrides(overrides))

    if failures:
        for failure in failures:
            print(f"[validate] ERROR: {failure}", file=sys.stderr)
        return 1

    nba_data = json.loads((base / "nba_data.json").read_text(encoding="utf-8"))
    print(
        "[validate] ok "
        f"games={len(nba_data.get('games') or [])} "
        f"next={len(nba_data.get('next_games') or [])} "
        f"official_picks={len(nba_data.get('official_picks') or [])} "
        f"edges={len(nba_data.get('edges') or [])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

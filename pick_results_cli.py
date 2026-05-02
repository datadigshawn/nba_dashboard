#!/usr/bin/env python3
"""Manage manual result overrides for stale NBA official picks."""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from nba_db import DB_PATH, init_db
from pick_history import OVERRIDES_FILE, sync_pick_history

BASE_DIR = Path(__file__).resolve().parent


def _connect(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def stale_pending(db_path: Path | str = DB_PATH) -> list[dict]:
    today = datetime.now().strftime("%Y%m%d")
    with _connect(db_path) as conn:
        rows = conn.execute("""
            SELECT id, pick_date, game_date, away, home, pick_type, pick_target,
                   pick_line, pick_detail, edge, confidence
            FROM recommended_picks
            WHERE correct IS NULL
              AND (result IS NULL OR result = 'pending')
              AND game_date < ?
            ORDER BY game_date, id
        """, (today,)).fetchall()
    return [dict(row) for row in rows]


def _load_payload(path: Path) -> dict:
    if not path.exists():
        return {"results": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return {"results": payload}
    if isinstance(payload, dict):
        payload.setdefault("results", [])
        return payload
    raise ValueError(f"{path.name} must be a list or an object")


def _result_key(row: dict) -> tuple[str, str, str]:
    game_date = row.get("game_date") or row.get("date") or ""
    home = row.get("home") or row.get("home_team") or row.get("team_a") or ""
    away = row.get("away") or row.get("away_team") or row.get("team_b") or ""
    return (str(game_date), str(home).strip().lower(), str(away).strip().lower())


def add_result(
    *,
    path: Path,
    game_date: str,
    home: str,
    away: str,
    home_score: int,
    away_score: int,
    source: str,
) -> dict:
    payload = _load_payload(path)
    rows = payload["results"]
    new_row = {
        "game_date": game_date,
        "away": away,
        "home": home,
        "away_score": away_score,
        "home_score": home_score,
        "winner": home if home_score > away_score else away,
        "source": source,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    key = _result_key(new_row)
    updated = False
    for idx, row in enumerate(rows):
        if isinstance(row, dict) and _result_key(row) == key:
            rows[idx] = {**row, **new_row}
            updated = True
            break
    if not updated:
        rows.append(new_row)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"updated": updated, "row": new_row, "path": str(path)}


def export_reports() -> None:
    from export_static_reports import main as export_main

    export_main()


def cmd_list(args: argparse.Namespace) -> int:
    init_db(args.db)
    rows = stale_pending(args.db)
    if not rows:
        print("[pick-results] no stale pending picks")
        return 0
    for row in rows:
        line = row["pick_line"]
        line_text = f" {line:+g}" if line is not None and row["pick_type"] == "spread" else (f" {line:g}" if line is not None else "")
        print(
            f"#{row['id']} {row['game_date']} "
            f"{row['away']} @ {row['home']} | "
            f"{row['pick_type']} {row['pick_target']}{line_text} | "
            f"{row['pick_detail']} | edge={row['edge']}"
        )
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    result = add_result(
        path=args.overrides,
        game_date=args.game_date,
        home=args.home,
        away=args.away,
        home_score=args.home_score,
        away_score=args.away_score,
        source=args.source,
    )
    action = "updated" if result["updated"] else "added"
    print(f"[pick-results] {action} override: {result['row']['away']} @ {result['row']['home']} {result['row']['away_score']}-{result['row']['home_score']}")
    if not args.no_sync:
        payload = sync_pick_history(args.db, overrides_path=args.overrides)
        export_reports()
        stats = payload["stats"]
        resolved = payload["resolved"]
        print(
            "[pick-results] synced "
            f"resolved={resolved['verified']} "
            f"stats={stats['wins']}/{stats['total']} "
            f"pending={stats['pending']} stale={stats['stale_pending']}"
        )
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    payload = sync_pick_history(args.db, overrides_path=args.overrides)
    export_reports()
    stats = payload["stats"]
    resolved = payload["resolved"]
    print(
        "[pick-results] synced "
        f"resolved={resolved['verified']} "
        f"stats={stats['wins']}/{stats['total']} "
        f"pending={stats['pending']} stale={stats['stale_pending']}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage stale NBA official-pick result overrides")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--overrides", type=Path, default=OVERRIDES_FILE)
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list-stale", help="List stale pending official picks")
    p_list.set_defaults(func=cmd_list)

    p_add = sub.add_parser("add-result", help="Add or update a manual box-score override")
    p_add.add_argument("--game-date", required=True, help="YYYYMMDD")
    p_add.add_argument("--away", required=True)
    p_add.add_argument("--home", required=True)
    p_add.add_argument("--away-score", required=True, type=int)
    p_add.add_argument("--home-score", required=True, type=int)
    p_add.add_argument("--source", default="manual_box_score")
    p_add.add_argument("--no-sync", action="store_true", help="Only write override JSON; do not sync DB/reports")
    p_add.set_defaults(func=cmd_add)

    p_sync = sub.add_parser("sync", help="Sync DB/reports from current overrides and bet ledger")
    p_sync.set_defaults(func=cmd_sync)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

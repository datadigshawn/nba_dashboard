"""
NBA SQLite 資料庫模組 — 儲存預測、Elo 歷史、回測績效。

資料庫位置：autobots_NBA/nba.db
所有 insert 函式皆冪等（UNIQUE + INSERT OR IGNORE）。
"""
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / "nba.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    prediction_date TEXT NOT NULL,
    game_date       TEXT NOT NULL,
    home            TEXT NOT NULL,
    away            TEXT NOT NULL,
    home_prob       REAL NOT NULL,
    away_prob       REAL NOT NULL,
    home_elo        INTEGER NOT NULL,
    away_elo        INTEGER NOT NULL,
    pred_spread     REAL,
    pred_total      REAL,
    home_expected   REAL,
    away_expected   REAL,
    b2b_home        INTEGER NOT NULL DEFAULT 0,
    b2b_away        INTEGER NOT NULL DEFAULT 0,
    rest_home       INTEGER,
    rest_away       INTEGER,
    status          TEXT,
    home_score      INTEGER,
    away_score      INTEGER,
    winner          TEXT,
    pick_correct    INTEGER,
    margin_error    REAL,
    resolved_at     TEXT,
    UNIQUE(prediction_date, home, away, game_date)
);
CREATE INDEX IF NOT EXISTS idx_pred_game_date ON predictions(game_date);
CREATE INDEX IF NOT EXISTS idx_pred_prediction_date ON predictions(prediction_date);

CREATE TABLE IF NOT EXISTS elo_history (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    team TEXT NOT NULL,
    elo  INTEGER NOT NULL,
    UNIQUE(date, team)
);
CREATE INDEX IF NOT EXISTS idx_elo_date ON elo_history(date);

CREATE TABLE IF NOT EXISTS daily_performance (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    date         TEXT NOT NULL UNIQUE,
    games_tested INTEGER,
    all_wr       REAL,
    strong_count INTEGER,
    strong_wr    REAL,
    vstrong_count INTEGER,
    vstrong_wr   REAL,
    star3_count  INTEGER,
    star3_wr     REAL,
    generated_at TEXT
);

CREATE TABLE IF NOT EXISTS backtest_results (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date  TEXT NOT NULL,
    game_date TEXT NOT NULL,
    home      TEXT NOT NULL,
    away      TEXT NOT NULL,
    conf      INTEGER,
    pick      TEXT NOT NULL,
    winner    TEXT NOT NULL,
    correct   INTEGER NOT NULL,
    score     TEXT,
    UNIQUE(run_date, game_date, home, away)
);

CREATE TABLE IF NOT EXISTS odds_lines (
    game       TEXT PRIMARY KEY,
    spread     REAL,
    ou         REAL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recommended_picks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    pick_date    TEXT NOT NULL,
    game_date    TEXT NOT NULL,
    game_key     TEXT NOT NULL,
    away         TEXT NOT NULL,
    home         TEXT NOT NULL,
    pick_type    TEXT NOT NULL,
    pick_target  TEXT NOT NULL,
    pick_line    REAL,
    pick_detail  TEXT NOT NULL,
    edge         REAL,
    confidence   REAL,
    tw_spread    REAL,
    tw_ou        REAL,
    model_spread REAL,
    model_total  REAL,
    result       TEXT,
    correct      INTEGER,
    verified_at  TEXT,
    UNIQUE(pick_date, game_date, game_key, pick_type, pick_detail)
);
CREATE INDEX IF NOT EXISTS idx_picks_game_date ON recommended_picks(game_date);
CREATE INDEX IF NOT EXISTS idx_picks_pick_date ON recommended_picks(pick_date);

CREATE TABLE IF NOT EXISTS bets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    game_date       TEXT NOT NULL,
    home            TEXT NOT NULL,
    away            TEXT NOT NULL,
    bet_type        TEXT NOT NULL,          -- moneyline / spread / ou
    bet_side        TEXT NOT NULL,          -- home / away / over / under
    bet_line        REAL,                   -- spread or total line
    market_odds     REAL NOT NULL DEFAULT 1.91,
    implied_prob    REAL,
    model_prob      REAL,
    edge            REAL,
    kelly_full      REAL,
    kelly_fraction  REAL,
    stake           REAL NOT NULL DEFAULT 0,
    result          TEXT,                   -- win / loss / push / pending
    pnl             REAL,
    home_score      INTEGER,
    away_score      INTEGER,
    source          TEXT DEFAULT 'paper',   -- paper / live
    created_at      TEXT NOT NULL,
    resolved_at     TEXT,
    UNIQUE(game_date, home, away, bet_type, bet_side, bet_line)
);
CREATE INDEX IF NOT EXISTS idx_bets_game_date ON bets(game_date);
CREATE INDEX IF NOT EXISTS idx_bets_result ON bets(result);

CREATE TABLE IF NOT EXISTS bankroll_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    event       TEXT NOT NULL,             -- init / bet_placed / bet_resolved / adjustment
    bet_id      INTEGER,
    amount      REAL NOT NULL,
    balance     REAL NOT NULL,
    note        TEXT,
    FOREIGN KEY (bet_id) REFERENCES bets(id)
);
CREATE INDEX IF NOT EXISTS idx_bankroll_ts ON bankroll_log(ts);
"""


def init_db(db_path: Path | str = DB_PATH):
    with sqlite3.connect(str(db_path), timeout=10) as conn:
        conn.executescript(SCHEMA)


def _connect(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def insert_predictions(db_path: Path | str, games: list[dict], prediction_date: str):
    with sqlite3.connect(str(db_path), timeout=10) as conn:
        for g in games:
            game_date = g.get("game_date") or prediction_date
            conn.execute("""
                INSERT OR IGNORE INTO predictions
                (prediction_date, game_date, home, away,
                 home_prob, away_prob, home_elo, away_elo,
                 pred_spread, pred_total, home_expected, away_expected,
                 b2b_home, b2b_away, rest_home, rest_away, status)
                VALUES (?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?,?)
            """, (
                prediction_date,
                game_date,
                g.get("home", ""),
                g.get("away", ""),
                g.get("home_prob", 0),
                g.get("away_prob", 0),
                g.get("home_elo", 1500),
                g.get("away_elo", 1500),
                g.get("pred_spread"),
                g.get("pred_total"),
                g.get("home_expected"),
                g.get("away_expected"),
                int(g.get("b2b_home", False)),
                int(g.get("b2b_away", False)),
                g.get("rest_home"),
                g.get("rest_away"),
                g.get("status", ""),
            ))


def insert_elo_snapshot(db_path: Path | str, elo_teams: dict, date: str):
    with sqlite3.connect(str(db_path), timeout=10) as conn:
        for team, elo in elo_teams.items():
            conn.execute(
                "INSERT OR IGNORE INTO elo_history (date, team, elo) VALUES (?,?,?)",
                (date, team, int(elo)),
            )


def insert_daily_performance(db_path: Path | str, backtest: dict, date: str):
    if not backtest:
        return
    with sqlite3.connect(str(db_path), timeout=10) as conn:
        conn.execute("""
            INSERT OR IGNORE INTO daily_performance
            (date, games_tested, all_wr, strong_count, strong_wr,
             vstrong_count, vstrong_wr, star3_count, star3_wr, generated_at)
            VALUES (?,?,?,?,?, ?,?,?,?,?)
        """, (
            date,
            backtest.get("games_tested"),
            backtest.get("all_wr"),
            backtest.get("strong_count"),
            backtest.get("strong_wr"),
            backtest.get("vstrong_count"),
            backtest.get("vstrong_wr"),
            backtest.get("star3_count"),
            backtest.get("star3_wr"),
            datetime.now().isoformat(timespec="seconds"),
        ))


def insert_backtest_results(db_path: Path | str, recent: list[dict], run_date: str):
    with sqlite3.connect(str(db_path), timeout=10) as conn:
        for r in recent:
            conn.execute("""
                INSERT OR IGNORE INTO backtest_results
                (run_date, game_date, home, away, conf, pick, winner, correct, score)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (
                run_date,
                r.get("date", ""),
                r.get("home", ""),
                r.get("away", ""),
                r.get("conf"),
                r.get("pick", ""),
                r.get("winner", ""),
                int(r.get("correct", False)),
                r.get("score", ""),
            ))


def resolve_outcomes(db_path: Path | str, results: list[dict]):
    """Update predictions with actual game results.

    results: list of dicts with keys: home, away, date (YYYYMMDD),
             home_score, away_score, winner.
    """
    now = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(str(db_path), timeout=10) as conn:
        for r in results:
            home_score = r.get("home_score")
            away_score = r.get("away_score")
            winner = r.get("winner", "")
            game_date = r.get("date", "")
            home = r.get("home", "")
            away = r.get("away", "")
            if not (home and away and game_date and winner):
                continue

            # Determine correctness
            row = conn.execute("""
                SELECT id, home_prob, away_prob, pred_spread
                FROM predictions
                WHERE game_date = ? AND home = ? AND away = ? AND resolved_at IS NULL
            """, (game_date, home, away)).fetchone()
            if not row:
                continue

            pid, hp, ap, ps = row
            home_was_pick = hp > ap
            home_won = (winner == home)
            correct = 1 if (home_was_pick == home_won) else 0
            actual_margin = (home_score or 0) - (away_score or 0)
            margin_err = ((ps or 0) - actual_margin) if ps is not None else None

            conn.execute("""
                UPDATE predictions SET
                    home_score=?, away_score=?, winner=?,
                    pick_correct=?, margin_error=?, resolved_at=?
                WHERE id=?
            """, (home_score, away_score, winner, correct, margin_err, now, pid))


def get_unresolved_dates(db_path: Path | str) -> list[str]:
    with sqlite3.connect(str(db_path), timeout=10) as conn:
        rows = conn.execute("""
            SELECT DISTINCT game_date FROM predictions
            WHERE resolved_at IS NULL AND game_date < ?
            ORDER BY game_date
        """, (datetime.now().strftime("%Y%m%d"),)).fetchall()
    return [r[0] for r in rows]


def get_pending_pick_dates(db_path: Path | str) -> list[str]:
    with sqlite3.connect(str(db_path), timeout=10) as conn:
        rows = conn.execute("""
            SELECT DISTINCT game_date FROM recommended_picks
            WHERE correct IS NULL
              AND (result IS NULL OR result = 'pending')
              AND game_date < ?
            ORDER BY game_date
        """, (datetime.now().strftime("%Y%m%d"),)).fetchall()
    return [r[0] for r in rows]


def db_summary(db_path: Path | str = DB_PATH) -> dict:
    with sqlite3.connect(str(db_path), timeout=10) as conn:
        pred_total = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        pred_resolved = conn.execute("SELECT COUNT(*) FROM predictions WHERE resolved_at IS NOT NULL").fetchone()[0]
        elo_dates = conn.execute("SELECT COUNT(DISTINCT date) FROM elo_history").fetchone()[0]
        perf_days = conn.execute("SELECT COUNT(*) FROM daily_performance").fetchone()[0]
        bt_games = conn.execute("SELECT COUNT(*) FROM backtest_results").fetchone()[0]
        odds_lines = conn.execute("SELECT COUNT(*) FROM odds_lines").fetchone()[0]
        picks_total = conn.execute("SELECT COUNT(*) FROM recommended_picks").fetchone()[0]
    return {
        "predictions": pred_total,
        "resolved": pred_resolved,
        "elo_snapshots": elo_dates,
        "performance_days": perf_days,
        "backtest_games": bt_games,
        "odds_lines": odds_lines,
        "recommended_picks": picks_total,
    }


def upsert_odds(db_path: Path | str, game: str, spread: float | None, ou: float | None) -> dict:
    now = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(str(db_path), timeout=10) as conn:
        conn.execute("""
            INSERT INTO odds_lines (game, spread, ou, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(game) DO UPDATE SET
                spread = excluded.spread,
                ou = excluded.ou,
                updated_at = excluded.updated_at
        """, (game, spread, ou, now))
    return {"game": game, "spread": spread, "ou": ou, "updated_at": now}


def list_odds(db_path: Path | str = DB_PATH) -> list[dict]:
    with _connect(db_path) as conn:
        rows = conn.execute("""
            SELECT game, spread, ou, updated_at
            FROM odds_lines
            ORDER BY game
        """).fetchall()
    return [dict(r) for r in rows]


def save_recommended_picks(db_path: Path | str, picks: list[dict]) -> int:
    saved = 0
    with sqlite3.connect(str(db_path), timeout=10) as conn:
        for p in picks:
            cur = conn.execute("""
                INSERT OR IGNORE INTO recommended_picks
                (pick_date, game_date, game_key, away, home,
                 pick_type, pick_target, pick_line, pick_detail,
                 edge, confidence, tw_spread, tw_ou, model_spread, model_total)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                p.get("pick_date", ""),
                p.get("game_date", ""),
                p.get("game_key", ""),
                p.get("away", ""),
                p.get("home", ""),
                p.get("pick_type", ""),
                p.get("pick_target", ""),
                p.get("pick_line"),
                p.get("pick_detail", ""),
                p.get("edge"),
                p.get("confidence"),
                p.get("tw_spread"),
                p.get("tw_ou"),
                p.get("model_spread"),
                p.get("model_total"),
            ))
            if cur.rowcount:
                saved += 1
    return saved


def _pick_date_from_ts(ts: str | None, fallback: str) -> str:
    if ts and len(ts) >= 10:
        return ts[:10].replace("-", "")
    return fallback


def _short_team(name: str) -> str:
    parts = (name or "").split()
    return parts[-1] if parts else ""


def _bet_pick_detail(row: sqlite3.Row) -> str:
    bet_type = row["bet_type"]
    side = row["bet_side"]
    line = row["bet_line"]
    if bet_type == "ou":
        label = "看大 Over" if side == "over" else "看小 Under"
        return f"{label} {line:g}" if line is not None else label
    if bet_type == "spread":
        team = row["home"] if side == "home" else row["away"]
        display_line = line
        if line is not None and side == "away":
            display_line = -float(line)
        line_label = f" {display_line:+g}" if display_line is not None else ""
        return f"買 {_short_team(team)}{line_label}"
    return f"{bet_type} {side}"


def import_recommended_picks_from_bets(db_path: Path | str = DB_PATH) -> dict:
    """Backfill recommended_picks from the tracker bet ledger when no pick row exists.

    The bet ledger stores probability edge, not point edge, so imported rows keep
    `edge` empty to avoid polluting point-edge bucket stats.
    """
    stats = {"candidates": 0, "imported": 0, "skipped_existing": 0}
    with _connect(db_path) as conn:
        rows = conn.execute("""
            SELECT *
            FROM bets
            WHERE bet_type IN ('spread', 'ou')
            ORDER BY id
        """).fetchall()
        stats["candidates"] = len(rows)

        for row in rows:
            line = row["bet_line"]
            existing = conn.execute("""
                SELECT id
                FROM recommended_picks
                WHERE game_date = ?
                  AND home = ?
                  AND away = ?
                  AND pick_type = ?
                  AND pick_target = ?
                  AND (
                    (pick_line IS NULL AND ? IS NULL)
                    OR ABS(COALESCE(pick_line, 0) - COALESCE(?, 0)) < 0.0001
                  )
                LIMIT 1
            """, (
                row["game_date"],
                row["home"],
                row["away"],
                row["bet_type"],
                row["bet_side"],
                line,
                line,
            )).fetchone()
            if existing:
                stats["skipped_existing"] += 1
                continue

            result = row["result"]
            correct = None
            if result == "win":
                correct = 1
            elif result == "loss":
                correct = 0
            confidence = row["model_prob"]
            if confidence is not None and confidence <= 1:
                confidence = confidence * 100

            conn.execute("""
                INSERT INTO recommended_picks
                (pick_date, game_date, game_key, away, home,
                 pick_type, pick_target, pick_line, pick_detail,
                 edge, confidence, result, correct, verified_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                _pick_date_from_ts(row["created_at"], row["game_date"]),
                row["game_date"],
                f"{row['away']} @ {row['home']}",
                row["away"],
                row["home"],
                row["bet_type"],
                row["bet_side"],
                line,
                _bet_pick_detail(row),
                None,
                confidence,
                result,
                correct,
                row["resolved_at"],
            ))
            stats["imported"] += 1

    return stats


def _evaluate_pick_result(row: sqlite3.Row, home_score: int, away_score: int) -> tuple[str, int | None]:
    pick_type = row["pick_type"]
    pick_target = row["pick_target"]
    pick_line = row["pick_line"]
    if pick_line is None:
        pick_line = row["tw_spread"] if pick_type == "spread" else row["tw_ou"]

    if pick_line is None:
        return "missing_line", None

    if pick_type == "spread":
        adjusted_home_margin = (home_score - away_score) + float(pick_line)
        if adjusted_home_margin == 0:
            return "push", None
        if pick_target == "home":
            return ("win", 1) if adjusted_home_margin > 0 else ("loss", 0)
        if pick_target == "away":
            return ("win", 1) if adjusted_home_margin < 0 else ("loss", 0)
        return "invalid_target", None

    if pick_type == "ou":
        total = home_score + away_score
        if total == float(pick_line):
            return "push", None
        if pick_target == "over":
            return ("win", 1) if total > float(pick_line) else ("loss", 0)
        if pick_target == "under":
            return ("win", 1) if total < float(pick_line) else ("loss", 0)
        return "invalid_target", None

    return "invalid_type", None


def _result_key(game_date: str, home: str, away: str) -> tuple[str, str, str]:
    return (str(game_date or ""), str(home or "").strip().lower(), str(away or "").strip().lower())


def _build_result_index(results: list[dict] | None) -> dict[tuple[str, str, str], dict]:
    index: dict[tuple[str, str, str], dict] = {}
    for r in results or []:
        game_date = r.get("date") or r.get("game_date") or ""
        home = r.get("home") or r.get("home_team") or r.get("team_a") or ""
        away = r.get("away") or r.get("away_team") or r.get("team_b") or ""
        home_score = r.get("home_score")
        away_score = r.get("away_score")
        if not (game_date and home and away):
            continue
        if home_score is None or away_score is None:
            continue
        index[_result_key(game_date, home, away)] = {
            "home_score": home_score,
            "away_score": away_score,
            "winner": r.get("winner", ""),
        }
    return index


def resolve_recommended_picks(
    db_path: Path | str = DB_PATH,
    results: list[dict] | None = None,
) -> dict:
    today = datetime.now().strftime("%Y%m%d")
    now = datetime.now().isoformat(timespec="seconds")
    stats = {
        "candidates": 0,
        "verified": 0,
        "wins": 0,
        "losses": 0,
        "pushes": 0,
        "missing_results": 0,
        "ungraded": 0,
    }
    result_index = _build_result_index(results)

    with _connect(db_path) as conn:
        rows = conn.execute("""
            SELECT *
            FROM recommended_picks
            WHERE correct IS NULL
              AND (result IS NULL OR result = 'pending')
              AND game_date <= ?
            ORDER BY game_date, id
        """, (today,)).fetchall()
        stats["candidates"] = len(rows)

        db_results = conn.execute("""
            SELECT game_date, home, away, home_score, away_score, winner
            FROM predictions
            WHERE resolved_at IS NOT NULL
              AND home_score IS NOT NULL
              AND away_score IS NOT NULL
              AND game_date <= ?
            ORDER BY prediction_date DESC
        """, (today,)).fetchall()
        for r in db_results:
            key = _result_key(r["game_date"], r["home"], r["away"])
            result_index.setdefault(key, dict(r))

        for row in rows:
            result_row = result_index.get(_result_key(row["game_date"], row["home"], row["away"]))
            if not result_row:
                stats["missing_results"] += 1
                continue

            result, correct = _evaluate_pick_result(
                row,
                int(result_row["home_score"]),
                int(result_row["away_score"]),
            )
            conn.execute("""
                UPDATE recommended_picks
                SET result = ?, correct = ?, verified_at = ?
                WHERE id = ?
            """, (result, correct, now, row["id"]))
            stats["verified"] += 1
            if correct == 1:
                stats["wins"] += 1
            elif correct == 0:
                stats["losses"] += 1
            elif result == "push":
                stats["pushes"] += 1
            else:
                stats["ungraded"] += 1

    return stats


def verify_pending_picks(db_path: Path | str = DB_PATH) -> dict:
    return resolve_recommended_picks(db_path)


def get_pick_stats(db_path: Path | str = DB_PATH) -> dict:
    with _connect(db_path) as conn:
        total, wins = conn.execute("""
            SELECT COUNT(*),
                   COALESCE(SUM(CASE WHEN correct = 1 THEN 1 ELSE 0 END), 0)
            FROM recommended_picks
            WHERE correct IN (0, 1)
        """).fetchone()

        spread_total, spread_wins = conn.execute("""
            SELECT COUNT(*),
                   COALESCE(SUM(CASE WHEN correct = 1 THEN 1 ELSE 0 END), 0)
            FROM recommended_picks
            WHERE pick_type = 'spread'
              AND correct IN (0, 1)
        """).fetchone()

        ou_total, ou_wins = conn.execute("""
            SELECT COUNT(*),
                   COALESCE(SUM(CASE WHEN correct = 1 THEN 1 ELSE 0 END), 0)
            FROM recommended_picks
            WHERE pick_type = 'ou'
              AND correct IN (0, 1)
        """).fetchone()

        daily_rows = conn.execute("""
            SELECT pick_date AS date,
                   COUNT(*) AS total,
                   COALESCE(SUM(CASE WHEN correct = 1 THEN 1 ELSE 0 END), 0) AS wins
            FROM recommended_picks
            WHERE correct IN (0, 1)
            GROUP BY pick_date
            ORDER BY pick_date DESC
            LIMIT 12
        """).fetchall()

        edge_rows = conn.execute("""
            SELECT
                CASE
                    WHEN ABS(COALESCE(edge, 0)) < 3 THEN '0-3'
                    WHEN ABS(COALESCE(edge, 0)) < 5 THEN '3-5'
                    WHEN ABS(COALESCE(edge, 0)) < 8 THEN '5-8'
                    ELSE '8+'
                END AS bucket,
                COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN correct = 1 THEN 1 ELSE 0 END), 0) AS wins
            FROM recommended_picks
            WHERE correct IN (0, 1)
              AND edge IS NOT NULL
            GROUP BY bucket
        """).fetchall()

        confidence_rows = conn.execute("""
            SELECT
                CASE
                    WHEN COALESCE(confidence, 0) < 60 THEN '<60'
                    WHEN COALESCE(confidence, 0) < 70 THEN '60-70'
                    WHEN COALESCE(confidence, 0) < 80 THEN '70-80'
                    ELSE '80+'
                END AS bucket,
                COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN correct = 1 THEN 1 ELSE 0 END), 0) AS wins
            FROM recommended_picks
            WHERE correct IN (0, 1)
            GROUP BY bucket
        """).fetchall()

        type_edge_rows = conn.execute("""
            SELECT
                pick_type,
                CASE
                    WHEN ABS(COALESCE(edge, 0)) < 3 THEN '0-3'
                    WHEN ABS(COALESCE(edge, 0)) < 5 THEN '3-5'
                    WHEN ABS(COALESCE(edge, 0)) < 8 THEN '5-8'
                    ELSE '8+'
                END AS bucket,
                COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN correct = 1 THEN 1 ELSE 0 END), 0) AS wins
            FROM recommended_picks
            WHERE correct IN (0, 1)
              AND edge IS NOT NULL
            GROUP BY pick_type, bucket
            ORDER BY pick_type, bucket
        """).fetchall()

        recent_rows = conn.execute("""
            SELECT pick_date, game_date, away, home, pick_type, pick_target,
                   pick_line, pick_detail, edge, confidence, result, correct,
                   tw_spread, tw_ou, model_spread, model_total
            FROM recommended_picks
            WHERE correct IN (0, 1)
            ORDER BY game_date DESC, id DESC
            LIMIT 20
        """).fetchall()

        pending_rows = conn.execute("""
            SELECT pick_date, game_date, away, home, pick_type, pick_target,
                   pick_line, pick_detail, edge, confidence,
                   COALESCE(result, 'pending') AS result, correct,
                   tw_spread, tw_ou, model_spread, model_total
            FROM recommended_picks
            WHERE correct IS NULL
              AND (result IS NULL OR result = 'pending')
            ORDER BY game_date ASC, edge DESC, id DESC
            LIMIT 20
        """).fetchall()

        pending_total = conn.execute("""
            SELECT COUNT(*)
            FROM recommended_picks
            WHERE correct IS NULL
              AND (result IS NULL OR result = 'pending')
        """).fetchone()[0]

        stale_pending = conn.execute("""
            SELECT COUNT(*)
            FROM recommended_picks
            WHERE correct IS NULL
              AND (result IS NULL OR result = 'pending')
              AND game_date < ?
        """, (datetime.now().strftime("%Y%m%d"),)).fetchone()[0]

    def _wr(w: int, t: int) -> float:
        return round(w / t * 100, 1) if t else 0.0

    daily = []
    for row in daily_rows:
        daily.append({
            "date": row["date"],
            "wins": row["wins"],
            "total": row["total"],
            "wr": _wr(row["wins"], row["total"]),
        })

    bucket_order = {
        "edge": ["0-3", "3-5", "5-8", "8+"],
        "confidence": ["<60", "60-70", "70-80", "80+"],
    }

    def _bucket_payload(rows: list[sqlite3.Row], order: list[str]) -> list[dict]:
        by_key = {row["bucket"]: row for row in rows}
        result = []
        for key in order:
            row = by_key.get(key)
            wins_v = int(row["wins"]) if row else 0
            total_v = int(row["total"]) if row else 0
            result.append({"bucket": key, "wins": wins_v, "total": total_v, "wr": _wr(wins_v, total_v)})
        return result

    by_type_edge: dict[str, list[dict]] = {}
    for pick_type in ("spread", "ou"):
        rows = [row for row in type_edge_rows if row["pick_type"] == pick_type]
        by_type_edge[pick_type] = _bucket_payload(rows, bucket_order["edge"])

    return {
        "total": total or 0,
        "wins": wins or 0,
        "wr": _wr(wins or 0, total or 0),
        "spread_total": spread_total or 0,
        "spread_wins": spread_wins or 0,
        "spread_wr": _wr(spread_wins or 0, spread_total or 0),
        "ou_total": ou_total or 0,
        "ou_wins": ou_wins or 0,
        "ou_wr": _wr(ou_wins or 0, ou_total or 0),
        "daily": list(reversed(daily)),
        "edge_buckets": _bucket_payload(edge_rows, bucket_order["edge"]),
        "confidence_buckets": _bucket_payload(confidence_rows, bucket_order["confidence"]),
        "by_type_edge": by_type_edge,
        "recent": [dict(r) for r in recent_rows],
        "current_picks": [dict(r) for r in pending_rows],
        "pending": pending_total or 0,
        "stale_pending": stale_pending or 0,
    }


def get_latest_bankroll_balance(db_path: Path | str = DB_PATH) -> float | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT balance FROM bankroll_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return float(row["balance"]) if row else None


def get_pending_bets(db_path: Path | str = DB_PATH) -> list[dict]:
    with _connect(db_path) as conn:
        rows = conn.execute("""
            SELECT * FROM bets WHERE result IS NULL OR result = 'pending'
            ORDER BY game_date, id
        """).fetchall()
    return [dict(r) for r in rows]


def _prediction_window_stats(conn: sqlite3.Connection, start_date: str, end_date: str) -> dict:
    row = conn.execute("""
        WITH latest AS (
            SELECT game_date, home, away, MAX(prediction_date) AS prediction_date
            FROM predictions
            WHERE game_date BETWEEN ? AND ?
            GROUP BY game_date, home, away
        )
        SELECT
            COUNT(*) AS total,
            COALESCE(SUM(CASE WHEN p.resolved_at IS NOT NULL THEN 1 ELSE 0 END), 0) AS resolved,
            COALESCE(SUM(CASE WHEN p.pick_correct = 1 THEN 1 ELSE 0 END), 0) AS wins,
            COALESCE(SUM(CASE WHEN p.pick_correct = 0 THEN 1 ELSE 0 END), 0) AS losses,
            AVG(CASE WHEN p.margin_error IS NOT NULL THEN ABS(p.margin_error) END) AS avg_margin_error
        FROM latest l
        JOIN predictions p
          ON p.game_date = l.game_date
         AND p.home = l.home
         AND p.away = l.away
         AND p.prediction_date = l.prediction_date
    """, (start_date, end_date)).fetchone()

    total = row["total"] or 0
    resolved = row["resolved"] or 0
    wins = row["wins"] or 0
    losses = row["losses"] or 0
    pending = max(total - resolved, 0)
    avg_margin_error = row["avg_margin_error"]

    return {
        "start_date": start_date,
        "end_date": end_date,
        "total": total,
        "resolved": resolved,
        "pending": pending,
        "wins": wins,
        "losses": losses,
        "wr": round(wins / resolved * 100, 1) if resolved else None,
        "avg_margin_error": round(float(avg_margin_error), 1) if avg_margin_error is not None else None,
    }


def get_prediction_summary(db_path: Path | str = DB_PATH, reference_date: str | None = None) -> dict:
    ref_dt = datetime.strptime(reference_date, "%Y%m%d") if reference_date else datetime.now()
    ref_ymd = ref_dt.strftime("%Y%m%d")
    week_start = (ref_dt - timedelta(days=ref_dt.weekday())).strftime("%Y%m%d")
    month_start = ref_dt.replace(day=1).strftime("%Y%m%d")

    with _connect(db_path) as conn:
        today = _prediction_window_stats(conn, ref_ymd, ref_ymd)
        week = _prediction_window_stats(conn, week_start, ref_ymd)
        month = _prediction_window_stats(conn, month_start, ref_ymd)

        season_total = conn.execute("""
            WITH latest AS (
                SELECT game_date, home, away, MAX(prediction_date) AS prediction_date
                FROM predictions
                GROUP BY game_date, home, away
            )
            SELECT
                MIN(p.game_date) AS start_date,
                MAX(p.game_date) AS end_date,
                COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN p.resolved_at IS NOT NULL THEN 1 ELSE 0 END), 0) AS resolved,
                COALESCE(SUM(CASE WHEN p.pick_correct = 1 THEN 1 ELSE 0 END), 0) AS wins,
                AVG(CASE WHEN p.margin_error IS NOT NULL THEN ABS(p.margin_error) END) AS avg_margin_error
            FROM latest l
            JOIN predictions p
              ON p.game_date = l.game_date
             AND p.home = l.home
             AND p.away = l.away
             AND p.prediction_date = l.prediction_date
        """).fetchone()

    season_avg_margin_error = season_total["avg_margin_error"]
    total = season_total["total"] or 0
    resolved = season_total["resolved"] or 0
    wins = season_total["wins"] or 0

    return {
        "reference_date": ref_ymd,
        "today": today,
        "week": week,
        "month": month,
        "season": {
            "start_date": season_total["start_date"],
            "end_date": season_total["end_date"],
            "total": total,
            "resolved": resolved,
            "pending": max(total - resolved, 0),
            "wins": wins,
            "losses": max(resolved - wins, 0),
            "wr": round(wins / resolved * 100, 1) if resolved else None,
            "avg_margin_error": round(float(season_avg_margin_error), 1) if season_avg_margin_error is not None else None,
        },
    }


def get_prediction_calibration(
    db_path: Path | str = DB_PATH,
    lookback_days: int = 21,
    max_games: int = 30,
) -> dict:
    cutoff = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y%m%d")
    with _connect(db_path) as conn:
        rows = conn.execute("""
            WITH latest AS (
                SELECT game_date, home, away, MAX(prediction_date) AS prediction_date
                FROM predictions
                WHERE resolved_at IS NOT NULL
                  AND game_date >= ?
                GROUP BY game_date, home, away
            )
            SELECT
                p.game_date,
                p.home,
                p.away,
                p.home_prob,
                p.away_prob,
                p.pred_spread,
                p.pred_total,
                p.home_score,
                p.away_score,
                (p.home_score - p.away_score) AS actual_margin,
                (p.home_score + p.away_score) AS actual_total
            FROM latest l
            JOIN predictions p
              ON p.game_date = l.game_date
             AND p.home = l.home
             AND p.away = l.away
             AND p.prediction_date = l.prediction_date
            ORDER BY p.game_date DESC, p.id DESC
            LIMIT ?
        """, (cutoff, max_games)).fetchall()

    spread_errors: list[float] = []
    total_errors: list[float] = []
    spread_sign_hits = 0
    spread_sign_total = 0
    moneyline_hits = 0

    for row in rows:
        actual_margin = float(row["actual_margin"] or 0.0)
        if row["pred_spread"] is not None:
            pred_spread = float(row["pred_spread"])
            spread_errors.append(actual_margin - pred_spread)
            if actual_margin != 0:
                spread_sign_total += 1
                if (pred_spread > 0 and actual_margin > 0) or (pred_spread < 0 and actual_margin < 0):
                    spread_sign_hits += 1

        if row["pred_total"] is not None:
            pred_total = float(row["pred_total"])
            actual_total = float(row["actual_total"] or 0.0)
            total_errors.append(actual_total - pred_total)

        home_pick = float(row["home_prob"] or 0.0) >= float(row["away_prob"] or 0.0)
        home_won = actual_margin >= 0
        if home_pick == home_won:
            moneyline_hits += 1

    def _avg(values: list[float]) -> float:
        return round(sum(values) / len(values), 2) if values else 0.0

    def _mae(values: list[float]) -> float:
        return round(sum(abs(v) for v in values) / len(values), 2) if values else 0.0

    return {
        "lookback_days": lookback_days,
        "games_considered": len(rows),
        "spread_samples": len(spread_errors),
        "spread_bias": _avg(spread_errors),
        "spread_mae": _mae(spread_errors),
        "spread_sign_accuracy": round(spread_sign_hits / spread_sign_total, 3) if spread_sign_total else None,
        "moneyline_accuracy": round(moneyline_hits / len(rows), 3) if rows else None,
        "total_samples": len(total_errors),
        "total_bias": _avg(total_errors),
        "total_mae": _mae(total_errors),
    }

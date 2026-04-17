"""
NBA SQLite 資料庫模組 — 儲存預測、Elo 歷史、回測績效。

資料庫位置：autobots_NBA/nba.db
所有 insert 函式皆冪等（UNIQUE + INSERT OR IGNORE）。
"""
import sqlite3
from datetime import datetime
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
"""


def init_db(db_path: Path | str = DB_PATH):
    with sqlite3.connect(str(db_path), timeout=10) as conn:
        conn.executescript(SCHEMA)


def insert_predictions(db_path: Path | str, games: list[dict], prediction_date: str):
    with sqlite3.connect(str(db_path), timeout=10) as conn:
        for g in games:
            conn.execute("""
                INSERT OR IGNORE INTO predictions
                (prediction_date, game_date, home, away,
                 home_prob, away_prob, home_elo, away_elo,
                 pred_spread, pred_total, home_expected, away_expected,
                 b2b_home, b2b_away, rest_home, rest_away, status)
                VALUES (?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?,?)
            """, (
                prediction_date,
                prediction_date,  # game_date = prediction_date for now
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


def db_summary(db_path: Path | str = DB_PATH) -> dict:
    with sqlite3.connect(str(db_path), timeout=10) as conn:
        pred_total = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        pred_resolved = conn.execute("SELECT COUNT(*) FROM predictions WHERE resolved_at IS NOT NULL").fetchone()[0]
        elo_dates = conn.execute("SELECT COUNT(DISTINCT date) FROM elo_history").fetchone()[0]
        perf_days = conn.execute("SELECT COUNT(*) FROM daily_performance").fetchone()[0]
        bt_games = conn.execute("SELECT COUNT(*) FROM backtest_results").fetchone()[0]
    return {
        "predictions": pred_total,
        "resolved": pred_resolved,
        "elo_snapshots": elo_dates,
        "performance_days": perf_days,
        "backtest_games": bt_games,
    }

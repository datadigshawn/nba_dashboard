"""
sportWeb SQLite 資料庫模組 — 儲存賠率快照、Edge 偵測、比賽結果。

資料庫位置：sportWeb/sportWeb.db
所有 insert 函式皆冪等（UNIQUE + INSERT OR IGNORE）。
"""
import sqlite3
from datetime import datetime
from pathlib import Path

from schema import parse_game_date_ymd

DB_PATH = Path(__file__).resolve().parent.parent / "sportWeb.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at TEXT NOT NULL UNIQUE,
    league     TEXT NOT NULL DEFAULT 'NBA',
    source_url TEXT,
    cf_passed  INTEGER NOT NULL DEFAULT 1,
    error      TEXT,
    game_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS odds (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    game_id     TEXT NOT NULL,
    away        TEXT NOT NULL,
    home        TEXT NOT NULL,
    game_date   TEXT,
    start_time  TEXT,
    ml_away     REAL,
    ml_home     REAL,
    ml_away_prob REAL,
    ml_home_prob REAL,
    overround   REAL,
    UNIQUE(snapshot_id, game_id)
);
CREATE INDEX IF NOT EXISTS idx_odds_game_id ON odds(game_id);
CREATE INDEX IF NOT EXISTS idx_odds_snapshot ON odds(snapshot_id);

CREATE TABLE IF NOT EXISTS odds_spreads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    game_id     TEXT NOT NULL,
    away        TEXT NOT NULL,
    home        TEXT NOT NULL,
    game_date   TEXT,
    home_line   REAL NOT NULL,
    away_odds   REAL,
    home_odds   REAL,
    away_prob   REAL,
    home_prob   REAL,
    overround   REAL,
    is_primary  INTEGER NOT NULL DEFAULT 0,
    UNIQUE(snapshot_id, game_id, home_line)
);
CREATE INDEX IF NOT EXISTS idx_odds_spreads_game_id ON odds_spreads(game_id);
CREATE INDEX IF NOT EXISTS idx_odds_spreads_snapshot ON odds_spreads(snapshot_id);

CREATE TABLE IF NOT EXISTS odds_totals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    game_id     TEXT NOT NULL,
    away        TEXT NOT NULL,
    home        TEXT NOT NULL,
    game_date   TEXT,
    total_line  REAL NOT NULL,
    over_odds   REAL,
    under_odds  REAL,
    over_prob   REAL,
    under_prob  REAL,
    overround   REAL,
    is_primary  INTEGER NOT NULL DEFAULT 0,
    UNIQUE(snapshot_id, game_id, total_line)
);
CREATE INDEX IF NOT EXISTS idx_odds_totals_game_id ON odds_totals(game_id);
CREATE INDEX IF NOT EXISTS idx_odds_totals_snapshot ON odds_totals(snapshot_id);

CREATE TABLE IF NOT EXISTS edges (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at     TEXT NOT NULL,
    snapshot_id     INTEGER REFERENCES snapshots(id),
    game_id         TEXT NOT NULL,
    market_key      TEXT,
    game_date       TEXT,
    away            TEXT NOT NULL,
    home            TEXT NOT NULL,
    side            TEXT NOT NULL,
    picked_team     TEXT NOT NULL,
    model_prob      REAL NOT NULL,
    market_prob     REAL NOT NULL,
    odds            REAL NOT NULL,
    edge            REAL NOT NULL,
    kelly           REAL NOT NULL,
    expected_roi    REAL NOT NULL,
    edge_type       TEXT NOT NULL DEFAULT 'moneyline',
    line            REAL NOT NULL DEFAULT 0.0,
    bet_line        REAL,
    min_edge_used   REAL,
    closing_line    REAL,
    closing_odds    REAL,
    closing_snapshot_id INTEGER,
    closing_fetched_at TEXT,
    clv_line        REAL,
    clv_odds        REAL,
    clv_win         INTEGER,
    actual_winner_side TEXT,
    bet_won         INTEGER,
    actual_profit   REAL,
    resolved_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_edges_game_id ON edges(game_id);
CREATE INDEX IF NOT EXISTS idx_edges_detected_at ON edges(detected_at);

CREATE TABLE IF NOT EXISTS game_outcomes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id      TEXT NOT NULL UNIQUE,
    home         TEXT NOT NULL,
    away         TEXT NOT NULL,
    home_score   INTEGER,
    away_score   INTEGER,
    winner_side  TEXT,
    resolved_at  TEXT NOT NULL
);
"""


def init_db(db_path: Path | str = DB_PATH):
    with sqlite3.connect(str(db_path), timeout=10) as conn:
        conn.executescript(SCHEMA)
        _ensure_column(conn, "odds", "game_date", "TEXT")
        _ensure_column(conn, "edges", "snapshot_id", "INTEGER")
        _ensure_column(conn, "edges", "market_key", "TEXT")
        _ensure_column(conn, "edges", "game_date", "TEXT")
        _ensure_column(conn, "edges", "bet_line", "REAL")
        _ensure_column(conn, "edges", "closing_line", "REAL")
        _ensure_column(conn, "edges", "closing_odds", "REAL")
        _ensure_column(conn, "edges", "closing_snapshot_id", "INTEGER")
        _ensure_column(conn, "edges", "closing_fetched_at", "TEXT")
        _ensure_column(conn, "edges", "clv_line", "REAL")
        _ensure_column(conn, "edges", "clv_odds", "REAL")
        _ensure_column(conn, "edges", "clv_win", "INTEGER")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_market_key ON edges(market_key)")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_edges_snapshot_market "
            "ON edges(snapshot_id, market_key)"
        )


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str):
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _two_way_probs(left_odds: float | None, right_odds: float | None):
    if not left_odds or not right_odds or left_odds <= 0 or right_odds <= 0:
        return None, None, None
    raw_left = 1 / float(left_odds)
    raw_right = 1 / float(right_odds)
    total = raw_left + raw_right
    return raw_left / total, raw_right / total, total - 1.0


def _pick_primary_line(lines: list[dict], left_key: str, right_key: str) -> float | None:
    best_line = None
    best_score = None
    for line in lines:
        market_line = line.get("line")
        left_odds = line.get(left_key)
        right_odds = line.get(right_key)
        left_prob, _, _ = _two_way_probs(left_odds, right_odds)
        if market_line is None or left_prob is None:
            continue
        score = (abs(left_prob - 0.5), abs(float(market_line)))
        if best_score is None or score < best_score:
            best_score = score
            best_line = float(market_line)
    return best_line


def bet_line_for_edge(edge_type: str, side: str, line: float | int | None) -> float:
    line_value = float(line or 0.0)
    if edge_type == "spread":
        return line_value if side == "home" else -line_value
    return line_value


def market_key_for_edge(game_id: str, edge_type: str, side: str,
                        line: float | int | None) -> str:
    if edge_type == "moneyline":
        return f"{game_id}|moneyline|{side}"
    return f"{game_id}|{edge_type}|{side}|{bet_line_for_edge(edge_type, side, line):.3f}"


def get_snapshot_id_by_fetched_at(db_path: Path | str,
                                  fetched_at: str) -> int | None:
    if not fetched_at:
        return None
    with sqlite3.connect(str(db_path), timeout=10) as conn:
        row = conn.execute(
            "SELECT id FROM snapshots WHERE fetched_at = ?",
            (fetched_at,),
        ).fetchone()
    return row[0] if row else None


def insert_snapshot(db_path: Path | str, snap: dict) -> int | None:
    """Insert a full odds snapshot. Returns snapshot_id or None if duplicate."""
    fetched_at = snap.get("fetched_at", "")
    if not fetched_at:
        return None

    with sqlite3.connect(str(db_path), timeout=10) as conn:
        cur = conn.execute("""
            INSERT OR IGNORE INTO snapshots
            (fetched_at, league, source_url, cf_passed, error, game_count)
            VALUES (?,?,?,?,?,?)
        """, (
            fetched_at,
            snap.get("league", "NBA"),
            snap.get("source_url", ""),
            1 if snap.get("cf_passed", True) else 0,
            snap.get("error"),
            len(snap.get("games", [])),
        ))
        if cur.rowcount == 0:
            row = conn.execute(
                "SELECT id FROM snapshots WHERE fetched_at = ?",
                (fetched_at,),
            ).fetchone()
            snap_id = row[0] if row else None
            is_new = False
        else:
            snap_id = cur.lastrowid
            is_new = True
        if snap_id is None:
            return None

        for g in snap.get("games", []):
            ml = g.get("moneyline") or {}
            impl = g.get("implied_prob") or {}
            game_id = g.get("game_id", "")
            game_date = g.get("game_date") or parse_game_date_ymd(g.get("start_time", ""))
            away = g.get("away", "")
            home = g.get("home", "")

            conn.execute("""
                INSERT OR IGNORE INTO odds
                (snapshot_id, game_id, away, home, game_date, start_time,
                 ml_away, ml_home, ml_away_prob, ml_home_prob, overround)
                VALUES (?,?,?,?,?, ?, ?,?,?,?,?)
            """, (
                snap_id,
                game_id,
                away,
                home,
                game_date,
                g.get("start_time"),
                ml.get("away"),
                ml.get("home"),
                impl.get("away_norm"),
                impl.get("home_norm"),
                impl.get("overround"),
            ))
            conn.execute("""
                UPDATE odds
                SET away=?, home=?, game_date=?, start_time=?,
                    ml_away=?, ml_home=?, ml_away_prob=?, ml_home_prob=?, overround=?
                WHERE snapshot_id=? AND game_id=?
            """, (
                away,
                home,
                game_date,
                g.get("start_time"),
                ml.get("away"),
                ml.get("home"),
                impl.get("away_norm"),
                impl.get("home_norm"),
                impl.get("overround"),
                snap_id,
                game_id,
            ))

            spreads = g.get("spreads") or []
            primary_home_line = _pick_primary_line(spreads, "away", "home")
            for sp in spreads:
                home_line = sp.get("line")
                away_odds = sp.get("away")
                home_odds = sp.get("home")
                if home_line is None or not (away_odds and home_odds):
                    continue
                away_prob, home_prob, overround = _two_way_probs(away_odds, home_odds)
                conn.execute("""
                    INSERT OR IGNORE INTO odds_spreads
                    (snapshot_id, game_id, away, home, game_date, home_line,
                     away_odds, home_odds, away_prob, home_prob, overround, is_primary)
                    VALUES (?,?,?,?,?, ?,?,?,?,?,?,?)
                """, (
                    snap_id,
                    game_id,
                    away,
                    home,
                    game_date,
                    float(home_line),
                    away_odds,
                    home_odds,
                    away_prob,
                    home_prob,
                    overround,
                    1 if primary_home_line is not None and float(home_line) == primary_home_line else 0,
                ))
                conn.execute("""
                    UPDATE odds_spreads
                    SET away=?, home=?, game_date=?, away_odds=?, home_odds=?,
                        away_prob=?, home_prob=?, overround=?, is_primary=?
                    WHERE snapshot_id=? AND game_id=? AND home_line=?
                """, (
                    away,
                    home,
                    game_date,
                    away_odds,
                    home_odds,
                    away_prob,
                    home_prob,
                    overround,
                    1 if primary_home_line is not None and float(home_line) == primary_home_line else 0,
                    snap_id,
                    game_id,
                    float(home_line),
                ))

            totals = g.get("totals") or []
            primary_total_line = _pick_primary_line(totals, "over", "under")
            for total in totals:
                total_line = total.get("line")
                over_odds = total.get("over")
                under_odds = total.get("under")
                if total_line is None or not (over_odds and under_odds):
                    continue
                over_prob, under_prob, overround = _two_way_probs(over_odds, under_odds)
                conn.execute("""
                    INSERT OR IGNORE INTO odds_totals
                    (snapshot_id, game_id, away, home, game_date, total_line,
                     over_odds, under_odds, over_prob, under_prob, overround, is_primary)
                    VALUES (?,?,?,?,?, ?,?,?,?,?,?,?)
                """, (
                    snap_id,
                    game_id,
                    away,
                    home,
                    game_date,
                    float(total_line),
                    over_odds,
                    under_odds,
                    over_prob,
                    under_prob,
                    overround,
                    1 if primary_total_line is not None and float(total_line) == primary_total_line else 0,
                ))
                conn.execute("""
                    UPDATE odds_totals
                    SET away=?, home=?, game_date=?, over_odds=?, under_odds=?,
                        over_prob=?, under_prob=?, overround=?, is_primary=?
                    WHERE snapshot_id=? AND game_id=? AND total_line=?
                """, (
                    away,
                    home,
                    game_date,
                    over_odds,
                    under_odds,
                    over_prob,
                    under_prob,
                    overround,
                    1 if primary_total_line is not None and float(total_line) == primary_total_line else 0,
                    snap_id,
                    game_id,
                    float(total_line),
                ))

    return snap_id if is_new else None


def insert_edges(db_path: Path | str, edges: list[dict],
                 detected_at: str, min_edge: float = 0.05,
                 snapshot_id: int | None = None):
    with sqlite3.connect(str(db_path), timeout=10) as conn:
        for e in edges:
            edge_type = e.get("edge_type", "moneyline")
            side = e.get("side", "")
            line = e.get("line", 0.0)
            market_key = market_key_for_edge(e.get("game_id", ""), edge_type, side, line)
            bet_line = bet_line_for_edge(edge_type, side, line)
            conn.execute("""
                INSERT OR IGNORE INTO edges
                (detected_at, snapshot_id, game_id, market_key, game_date,
                 away, home, side, picked_team,
                 model_prob, market_prob, odds, edge, kelly, expected_roi,
                 edge_type, line, bet_line, min_edge_used)
                VALUES (?,?,?,?,?,?,?,?,?, ?,?,?,?,?,?, ?,?,?,?)
            """, (
                detected_at,
                snapshot_id,
                e.get("game_id", ""),
                market_key,
                e.get("game_date", ""),
                e.get("away", ""),
                e.get("home", ""),
                side,
                e.get("picked_team", ""),
                e.get("model_prob", 0),
                e.get("market_prob", 0),
                e.get("odds", 0),
                e.get("edge", 0),
                e.get("kelly", 0),
                e.get("expected_roi", 0),
                edge_type,
                line,
                bet_line,
                min_edge,
            ))


def resolve_game(db_path: Path | str, game_id: str,
                 home: str, away: str,
                 home_score: int, away_score: int):
    """Resolve a game outcome and update any matching edges."""
    now = datetime.now().isoformat(timespec="seconds")
    winner_side = "home" if home_score > away_score else "away"

    with sqlite3.connect(str(db_path), timeout=10) as conn:
        conn.execute("""
            INSERT OR IGNORE INTO game_outcomes
            (game_id, home, away, home_score, away_score, winner_side, resolved_at)
            VALUES (?,?,?,?,?,?,?)
        """, (game_id, home, away, home_score, away_score, winner_side, now))

        # Update matching edges
        edge_rows = conn.execute(
            "SELECT id, side, odds FROM edges WHERE game_id = ? AND resolved_at IS NULL",
            (game_id,),
        ).fetchall()
        for eid, side, odds_val in edge_rows:
            bet_won = 1 if side == winner_side else 0
            profit = (odds_val - 1.0) if bet_won else -1.0
            conn.execute("""
                UPDATE edges SET
                    actual_winner_side=?, bet_won=?, actual_profit=?, resolved_at=?
                WHERE id=?
            """, (winner_side, bet_won, round(profit, 4), now, eid))


def get_unresolved_edge_game_ids(db_path: Path | str = DB_PATH) -> list[str]:
    with sqlite3.connect(str(db_path), timeout=10) as conn:
        rows = conn.execute("""
            SELECT DISTINCT game_id FROM edges WHERE resolved_at IS NULL
        """).fetchall()
    return [r[0] for r in rows]


def edge_backtest(db_path: Path | str = DB_PATH) -> dict:
    """Historical edge performance analysis for dashboard display."""
    canonical_key_sql = """
        CASE
            WHEN COALESCE(market_key, '') <> '' THEN market_key
            WHEN edge_type = 'moneyline' THEN game_id || '|moneyline|' || side
            WHEN edge_type = 'spread' THEN game_id || '|spread|' || side || '|' ||
                printf('%.3f', CASE WHEN side = 'away' THEN -line ELSE line END)
            WHEN edge_type = 'total' THEN game_id || '|total|' || side || '|' || printf('%.3f', line)
            ELSE game_id || '|' || edge_type || '|' || side || '|' || printf('%.3f', line)
        END
    """
    canonical_cte = f"""
        WITH canonical AS (
            SELECT MIN(id) AS id
            FROM edges
            GROUP BY {canonical_key_sql}
        ),
        canonical_edges AS (
            SELECT e.*
            FROM edges e
            JOIN canonical c ON c.id = e.id
        )
    """
    with sqlite3.connect(str(db_path), timeout=10) as conn:
        raw_total = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        raw_resolved = conn.execute("SELECT COUNT(*) FROM edges WHERE resolved_at IS NOT NULL").fetchone()[0]
        total = conn.execute(canonical_cte + "SELECT COUNT(*) FROM canonical_edges").fetchone()[0]
        resolved = conn.execute(
            canonical_cte + "SELECT COUNT(*) FROM canonical_edges WHERE resolved_at IS NOT NULL"
        ).fetchone()[0]
        if resolved == 0:
            return {
                "raw_total": raw_total,
                "raw_resolved": raw_resolved,
                "total": total,
                "resolved": 0,
                "by_type": {},
                "by_bucket": [],
                "calibration": [],
                "clv": {"tracked": 0, "avg_clv_line": None, "avg_clv_odds": None, "clv_win_rate": None},
                "recent": [],
            }

        won = conn.execute(
            canonical_cte + "SELECT COUNT(*) FROM canonical_edges WHERE resolved_at IS NOT NULL AND bet_won=1"
        ).fetchone()[0]
        pushes = conn.execute(
            canonical_cte + "SELECT COUNT(*) FROM canonical_edges WHERE resolved_at IS NOT NULL AND bet_won IS NULL"
        ).fetchone()[0]
        lost = resolved - won - pushes
        profit_sum = conn.execute(
            canonical_cte + "SELECT COALESCE(SUM(actual_profit),0) FROM canonical_edges WHERE resolved_at IS NOT NULL"
        ).fetchone()[0]

        # By edge type
        by_type = {}
        for row in conn.execute(canonical_cte + """
            SELECT edge_type, COUNT(*) as n,
                   COALESCE(SUM(CASE WHEN bet_won=1 THEN 1 ELSE 0 END), 0) as wins,
                   COALESCE(SUM(CASE WHEN bet_won IS NULL THEN 1 ELSE 0 END), 0) as pushes,
                   ROUND(
                       COALESCE(SUM(CASE WHEN bet_won=1 THEN 1 ELSE 0 END), 0) * 100.0 /
                       NULLIF(COUNT(bet_won), 0), 1
                   ) as wr,
                   ROUND(SUM(actual_profit), 2) as profit,
                   ROUND(AVG(actual_profit), 4) as avg_profit,
                   ROUND(AVG(clv_line), 3) as avg_clv_line,
                   ROUND(AVG(clv_odds), 3) as avg_clv_odds,
                   ROUND(AVG(clv_win)*100.0, 1) as clv_win_rate
            FROM canonical_edges WHERE resolved_at IS NOT NULL
            GROUP BY edge_type
        """):
            by_type[row[0]] = {
                "count": row[1], "wins": row[2], "pushes": row[3], "win_rate": row[4] or 0,
                "total_profit": row[5], "avg_profit": row[6],
                "avg_clv_line": row[7], "avg_clv_odds": row[8],
                "clv_win_rate": row[9],
            }

        # By edge size bucket
        buckets = []
        for label, lo, hi in [("0-5%", 0, 0.05), ("5-10%", 0.05, 0.10),
                               ("10-20%", 0.10, 0.20), ("20%+", 0.20, 9.0)]:
            row = conn.execute(canonical_cte + """
                SELECT COUNT(*),
                       COALESCE(SUM(CASE WHEN bet_won=1 THEN 1 ELSE 0 END), 0),
                       COALESCE(SUM(CASE WHEN bet_won IS NULL THEN 1 ELSE 0 END), 0),
                       ROUND(COALESCE(SUM(actual_profit),0), 2)
                FROM canonical_edges
                WHERE resolved_at IS NOT NULL AND edge >= ? AND edge < ?
            """, (lo, hi)).fetchone()
            n, w, pushes_bucket, p = row
            graded = n - pushes_bucket
            buckets.append({
                "label": label, "count": n, "wins": w,
                "pushes": pushes_bucket,
                "win_rate": round(w * 100.0 / graded, 1) if graded > 0 else 0,
                "profit": p,
            })

        # Calibration: model_prob buckets vs actual win rate
        calibration = []
        for label, lo, hi in [("40-50%", 0.40, 0.50), ("50-60%", 0.50, 0.60),
                                ("60-70%", 0.60, 0.70), ("70-80%", 0.70, 0.80),
                                ("80%+", 0.80, 1.01)]:
            row = conn.execute(canonical_cte + """
                SELECT COUNT(*),
                       COALESCE(SUM(CASE WHEN bet_won=1 THEN 1 ELSE 0 END), 0),
                       COUNT(bet_won),
                       ROUND(AVG(model_prob)*100, 1)
                FROM canonical_edges
                WHERE resolved_at IS NOT NULL AND model_prob >= ? AND model_prob < ?
            """, (lo, hi)).fetchone()
            n, w, graded_n, avg_mp = row
            calibration.append({
                "label": label, "count": n, "wins": w,
                "actual_wr": round(w * 100.0 / graded_n, 1) if graded_n > 0 else 0,
                "avg_model_prob": avg_mp or 0,
            })

        clv_row = conn.execute(canonical_cte + """
            SELECT COUNT(*),
                   ROUND(AVG(clv_line), 3),
                   ROUND(AVG(clv_odds), 3),
                   ROUND(AVG(clv_win)*100.0, 1)
            FROM canonical_edges
            WHERE resolved_at IS NOT NULL
              AND (clv_line IS NOT NULL OR clv_odds IS NOT NULL)
        """).fetchone()
        clv_summary = {
            "tracked": clv_row[0],
            "avg_clv_line": clv_row[1],
            "avg_clv_odds": clv_row[2],
            "clv_win_rate": clv_row[3],
        }

        # Recent resolved (last 20)
        recent = []
        for row in conn.execute(canonical_cte + """
            SELECT edge_type, side, picked_team, away, home,
                   ROUND(edge*100,1), ROUND(model_prob*100,1), ROUND(market_prob*100,1),
                   odds, bet_won, actual_profit, line, bet_line,
                   closing_line, closing_odds, clv_line, clv_odds, clv_win, resolved_at
            FROM canonical_edges WHERE resolved_at IS NOT NULL
            ORDER BY resolved_at DESC, id DESC LIMIT 20
        """):
            recent.append({
                "edge_type": row[0], "side": row[1], "picked_team": row[2],
                "away": row[3], "home": row[4],
                "edge_pct": row[5], "model_prob": row[6], "market_prob": row[7],
                "odds": row[8], "bet_won": row[9], "actual_profit": row[10],
                "line": row[11], "bet_line": row[12],
                "closing_line": row[13], "closing_odds": row[14],
                "clv_line": row[15], "clv_odds": row[16], "clv_win": row[17],
                "resolved_at": row[18],
            })

        return {
            "raw_total": raw_total,
            "raw_resolved": raw_resolved,
            "total": total,
            "resolved": resolved,
            "won": won,
            "lost": lost,
            "pushes": pushes,
            "win_rate": round(won * 100.0 / (resolved - pushes), 1) if resolved > pushes else 0,
            "total_profit": round(profit_sum, 2),
            "avg_roi": round(profit_sum / resolved, 4) if resolved > 0 else 0,
            "by_type": by_type,
            "by_bucket": buckets,
            "calibration": calibration,
            "clv": clv_summary,
            "recent": recent,
        }


def db_summary(db_path: Path | str = DB_PATH) -> dict:
    with sqlite3.connect(str(db_path), timeout=10) as conn:
        snaps = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        odds_rows = conn.execute("SELECT COUNT(*) FROM odds").fetchone()[0]
        spread_rows = conn.execute("SELECT COUNT(*) FROM odds_spreads").fetchone()[0]
        total_rows = conn.execute("SELECT COUNT(*) FROM odds_totals").fetchone()[0]
        edges_total = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        edges_resolved = conn.execute("SELECT COUNT(*) FROM edges WHERE resolved_at IS NOT NULL").fetchone()[0]
        outcomes = conn.execute("SELECT COUNT(*) FROM game_outcomes").fetchone()[0]
    return {
        "snapshots": snaps,
        "odds_rows": odds_rows,
        "spread_rows": spread_rows,
        "total_rows": total_rows,
        "edges_total": edges_total,
        "edges_resolved": edges_resolved,
        "game_outcomes": outcomes,
    }

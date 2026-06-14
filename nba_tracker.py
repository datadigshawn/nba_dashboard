#!/usr/bin/env python3
"""
NBA 量化追蹤模組 — 紙上下注、校準分析、Kelly 下注。

Phase 1: 紀錄模擬下注、解析結果、追蹤損益
Phase 2: 校準曲線、ROI 分析、場景分析
Phase 3: Kelly 下注策略、場景過濾

用法：
    python nba_tracker.py --init-bankroll 1000
    python nba_tracker.py --place              # 從今日推薦自動下紙上注
    python nba_tracker.py --resolve            # 解析已完成比賽
    python nba_tracker.py --summary            # 損益摘要
    python nba_tracker.py --calibration        # 校準曲線
    python nba_tracker.py --roi                # ROI 分析
    python nba_tracker.py --scenarios          # 場景分析
    python nba_tracker.py --recommend          # Kelly 過濾推薦
    python nba_tracker.py --report             # 完整報告
    python nba_tracker.py --resolve --place    # Pipeline: 先解析再下注
"""
import argparse
import json
import math
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from nba_db import DB_PATH, init_db, get_latest_bankroll_balance, get_pending_bets

DEFAULT_BANKROLL = 1000.0
DEFAULT_STAKE_ODDS = 1.91  # standard -110 juice（僅 fallback；下注一律優先用真實 odds_at_pick）
KELLY_FRACTION = 0.125     # 1/8 Kelly（保守；資料變厚前不放寬）

# ── 階段5 紀律參數 ──────────────────────────────────────────────
MAX_STAKE_PCT = 0.02        # 單注硬上限：銀行 2%（fractional Kelly 之上再加一道保險）
MIN_CALIBRATED_EDGE = 0.03  # 校準後 edge 門檻：model_prob 須超出真實隱含機率 3 個百分點
PICK_TYPE_WHITELIST = {"spread", "ou"}  # 允許下注的類型（保守起見可收窄）

MODEL_STATE_PATH = BASE_DIR / "state" / "nba_model.json"


def _calibrated_sigma() -> float:
    """階段3 校準的 margin sigma；缺檔 fallback 回訓練 RMSE。

    取代過去 place_from_picks / bet_filter 直接用 _load_rmse()（in-sample，過度自信）。
    """
    try:
        from nba_predictor import load_prob_sigma
        s = load_prob_sigma()
        if s:
            return s
    except Exception:
        pass
    return _load_rmse()


def _connect(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load_rmse() -> float:
    try:
        state = json.loads(MODEL_STATE_PATH.read_text())
        return float(state.get("rmse", 12.0))
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return 12.0


# ═══════════════════════════════════════════════════════════════
#  Phase 1: Bet Ledger & Bankroll
# ═══════════════════════════════════════════════════════════════

def kelly_sizing(model_prob: float, market_odds: float,
                 fraction: float = KELLY_FRACTION) -> dict:
    if market_odds <= 1.0 or model_prob <= 0 or model_prob >= 1:
        return {"kelly_full": 0, "kelly_fraction": 0, "stake_pct": 0,
                "edge": 0, "implied_prob": 0}
    b = market_odds - 1.0
    q = 1.0 - model_prob
    kelly_full = (b * model_prob - q) / b
    kelly_frac = max(0.0, kelly_full * fraction)
    implied = 1.0 / market_odds
    return {
        "kelly_full": round(kelly_full, 4),
        "kelly_fraction": round(kelly_frac, 4),
        "stake_pct": round(kelly_frac * 100, 2),
        "edge": round(model_prob - implied, 4),
        "implied_prob": round(implied, 4),
    }


def _margin_to_cover_prob(margin: float, line: float, rmse: float = 12.0) -> float:
    z = (margin - line) / rmse if rmse > 0 else 0
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def init_bankroll(db_path: Path | str = DB_PATH,
                  amount: float = DEFAULT_BANKROLL) -> float:
    now = _now()
    with sqlite3.connect(str(db_path), timeout=10) as conn:
        existing = conn.execute(
            "SELECT balance FROM bankroll_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if existing:
            print(f"[tracker] 銀行已存在，餘額: {existing[0]:.2f}")
            return float(existing[0])
        conn.execute("""
            INSERT INTO bankroll_log (ts, event, amount, balance, note)
            VALUES (?, 'init', ?, ?, '初始資金')
        """, (now, amount, amount))
    print(f"[tracker] 初始化銀行: {amount:.2f}")
    return amount


def place_paper_bet(db_path: Path | str, game_date: str, home: str, away: str,
                    bet_type: str, bet_side: str, bet_line: float | None,
                    model_prob: float, market_odds: float = DEFAULT_STAKE_ODDS,
                    stake: float | None = None,
                    source: str = "paper") -> dict | None:
    now = _now()
    ks = kelly_sizing(model_prob, market_odds)

    balance = get_latest_bankroll_balance(db_path)
    if balance is None:
        balance = init_bankroll(db_path)

    if stake is None:
        stake = round(balance * ks["kelly_fraction"], 2)
    # 單注硬上限：即使 Kelly 算出更大也封頂在銀行的 MAX_STAKE_PCT
    cap = round(balance * MAX_STAKE_PCT, 2)
    if stake > cap:
        stake = cap
    if stake <= 0:
        return None

    implied = ks["implied_prob"]
    edge = ks["edge"]

    with sqlite3.connect(str(db_path), timeout=10) as conn:
        try:
            conn.execute("""
                INSERT INTO bets
                (game_date, home, away, bet_type, bet_side, bet_line,
                 market_odds, implied_prob, model_prob, edge,
                 kelly_full, kelly_fraction, stake, source, created_at)
                VALUES (?,?,?,?,?,?, ?,?,?,?, ?,?,?,?,?)
            """, (
                game_date, home, away, bet_type, bet_side, bet_line,
                market_odds, implied, model_prob, edge,
                ks["kelly_full"], ks["kelly_fraction"], stake, source, now,
            ))
        except sqlite3.IntegrityError:
            return None

        bet_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        new_balance = balance - stake
        conn.execute("""
            INSERT INTO bankroll_log (ts, event, bet_id, amount, balance, note)
            VALUES (?, 'bet_placed', ?, ?, ?, ?)
        """, (now, bet_id, -stake, new_balance,
              f"{bet_type} {bet_side} {home} vs {away}"))

    return {
        "bet_id": bet_id, "game_date": game_date,
        "home": home, "away": away,
        "bet_type": bet_type, "bet_side": bet_side,
        "stake": stake, "model_prob": model_prob,
        "kelly_pct": ks["stake_pct"], "edge": edge,
    }


def place_from_picks(db_path: Path | str = DB_PATH, *, verbose: bool = False) -> list[dict]:
    """從 recommended_picks 下 paper bet，套用階段5 紀律。

    階段5 修正（過去會系統性超注）：
      1. 用真實 odds_at_pick 算 Kelly/edge，而非假的 -110（1.91）
      2. 用階段3 校準 sigma 算機率，而非 in-sample 訓練 RMSE
      3. 閘門：類型白名單、有真實賠率、校準 edge ≥ 門檻
    """
    today = datetime.now().strftime("%Y%m%d")
    sigma = _calibrated_sigma()
    placed, skipped = [], []

    with _connect(db_path) as conn:
        picks = conn.execute("""
            SELECT * FROM recommended_picks
            WHERE game_date >= ? AND correct IS NULL
            ORDER BY edge DESC
        """, (today,)).fetchall()

    for p in picks:
        pick_type = p["pick_type"]
        pick_target = p["pick_target"]
        pick_line = p["pick_line"]
        model_spread = p["model_spread"]
        model_total = p["model_total"]
        odds = p["odds_at_pick"]

        def _skip(reason):
            skipped.append({"pick": p["pick_detail"], "reason": reason})

        # 閘門 1：類型白名單
        if pick_type not in PICK_TYPE_WHITELIST:
            _skip(f"類型 {pick_type} 不在白名單")
            continue

        # 機率（校準 sigma）
        if pick_type == "spread" and model_spread is not None and pick_line is not None:
            model_prob = _margin_to_cover_prob(model_spread, -pick_line, sigma)
        elif pick_type == "ou" and model_total is not None and pick_line is not None:
            if pick_target == "over":
                model_prob = _margin_to_cover_prob(model_total, pick_line, sigma)
            else:
                model_prob = 1 - _margin_to_cover_prob(model_total, pick_line, sigma)
        else:
            _skip("缺 model line")
            continue

        # 閘門 2：必須有真實賠率（無則不下注，不用假賠率猜）
        if not odds or odds <= 1.0:
            _skip("無真實 odds_at_pick")
            continue

        # 閘門 3：校準後 edge 須達門檻（vig-aware）
        implied = 1.0 / odds
        cal_edge = model_prob - implied
        if cal_edge < MIN_CALIBRATED_EDGE:
            _skip(f"校準 edge {cal_edge:+.3f} < {MIN_CALIBRATED_EDGE}")
            continue

        result = place_paper_bet(
            db_path, p["game_date"], p["home"], p["away"],
            pick_type, pick_target, pick_line, model_prob,
            market_odds=odds,
        )
        if result:
            placed.append(result)
        else:
            _skip("已下過或 stake=0")

    if verbose:
        print(f"[tracker] 評估 {len(picks)} 注 → 下 {len(placed)}，跳過 {len(skipped)}")
        for s in skipped:
            print(f"    skip: {s['pick']} — {s['reason']}")

    return placed


def resolve_bets(db_path: Path | str = DB_PATH) -> dict:
    today = datetime.now().strftime("%Y%m%d")
    now = _now()
    stats = {"resolved": 0, "wins": 0, "losses": 0, "pushes": 0, "total_pnl": 0.0}

    with _connect(db_path) as conn:
        pending = conn.execute("""
            SELECT * FROM bets
            WHERE (result IS NULL OR result = 'pending')
              AND game_date < ?
        """, (today,)).fetchall()

        for bet in pending:
            pred = conn.execute("""
                SELECT home_score, away_score
                FROM predictions
                WHERE game_date = ? AND home = ? AND away = ?
                  AND resolved_at IS NOT NULL
                ORDER BY prediction_date DESC LIMIT 1
            """, (bet["game_date"], bet["home"], bet["away"])).fetchone()
            if not pred:
                continue

            hs, aws = pred["home_score"], pred["away_score"]
            if hs is None or aws is None:
                continue

            bt = bet["bet_type"]
            bs = bet["bet_side"]
            bl = bet["bet_line"]
            stake = bet["stake"]
            odds = bet["market_odds"]

            if bt == "moneyline":
                winner = bet["home"] if hs > aws else bet["away"]
                if hs == aws:
                    result, pnl = "push", 0.0
                elif (bs == "home" and winner == bet["home"]) or \
                     (bs == "away" and winner == bet["away"]):
                    result, pnl = "win", stake * (odds - 1)
                else:
                    result, pnl = "loss", -stake

            elif bt == "spread":
                margin = hs - aws
                adj = margin + (bl or 0)
                if bs == "away":
                    adj = -adj
                if adj == 0:
                    result, pnl = "push", 0.0
                elif adj > 0:
                    result, pnl = "win", stake * (odds - 1)
                else:
                    result, pnl = "loss", -stake

            elif bt == "ou":
                total = hs + aws
                if bl is not None and total == bl:
                    result, pnl = "push", 0.0
                elif bs == "over" and bl is not None and total > bl:
                    result, pnl = "win", stake * (odds - 1)
                elif bs == "under" and bl is not None and total < bl:
                    result, pnl = "win", stake * (odds - 1)
                else:
                    result, pnl = "loss", -stake
            else:
                continue

            pnl = round(pnl, 2)
            conn.execute("""
                UPDATE bets SET result=?, pnl=?, home_score=?, away_score=?,
                       resolved_at=? WHERE id=?
            """, (result, pnl, hs, aws, now, bet["id"]))

            balance = conn.execute(
                "SELECT balance FROM bankroll_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
            cur_balance = float(balance["balance"]) if balance else DEFAULT_BANKROLL
            new_balance = round(cur_balance + stake + pnl, 2)
            conn.execute("""
                INSERT INTO bankroll_log (ts, event, bet_id, amount, balance, note)
                VALUES (?, 'bet_resolved', ?, ?, ?, ?)
            """, (now, bet["id"], round(stake + pnl, 2), new_balance, result))

            stats["resolved"] += 1
            stats["total_pnl"] += pnl
            if result == "win":
                stats["wins"] += 1
            elif result == "loss":
                stats["losses"] += 1
            else:
                stats["pushes"] += 1

    stats["total_pnl"] = round(stats["total_pnl"], 2)
    return stats


def get_bankroll_summary(db_path: Path | str = DB_PATH) -> dict:
    with _connect(db_path) as conn:
        agg = conn.execute("""
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN result='win' THEN 1 ELSE 0 END), 0) AS wins,
                COALESCE(SUM(CASE WHEN result='loss' THEN 1 ELSE 0 END), 0) AS losses,
                COALESCE(SUM(CASE WHEN result='push' THEN 1 ELSE 0 END), 0) AS pushes,
                COALESCE(SUM(CASE WHEN result IS NULL OR result='pending'
                             THEN 1 ELSE 0 END), 0) AS pending,
                COALESCE(SUM(stake), 0) AS total_wagered,
                COALESCE(SUM(CASE WHEN pnl IS NOT NULL THEN pnl ELSE 0 END), 0) AS total_pnl
            FROM bets
        """).fetchone()

        balance_row = conn.execute(
            "SELECT balance FROM bankroll_log ORDER BY id DESC LIMIT 1"
        ).fetchone()

        init_row = conn.execute(
            "SELECT balance FROM bankroll_log WHERE event='init' ORDER BY id LIMIT 1"
        ).fetchone()

        peak_row = conn.execute(
            "SELECT MAX(balance) AS peak FROM bankroll_log"
        ).fetchone()

        recent = conn.execute("""
            SELECT id, game_date, home, away, bet_type, bet_side, bet_line,
                   stake, model_prob, edge, result, pnl, created_at
            FROM bets ORDER BY id DESC LIMIT 10
        """).fetchall()

    total_wagered = float(agg["total_wagered"])
    total_pnl = float(agg["total_pnl"])
    resolved = agg["wins"] + agg["losses"] + agg["pushes"]
    current = float(balance_row["balance"]) if balance_row else 0
    initial = float(init_row["balance"]) if init_row else DEFAULT_BANKROLL
    peak = float(peak_row["peak"]) if peak_row else current
    drawdown = round((peak - current) / peak * 100, 2) if peak > 0 else 0

    return {
        "current_balance": round(current, 2),
        "initial_balance": initial,
        "total_bets": agg["total"],
        "pending_bets": agg["pending"],
        "resolved_bets": resolved,
        "wins": agg["wins"],
        "losses": agg["losses"],
        "pushes": agg["pushes"],
        "win_rate": round(agg["wins"] / resolved * 100, 1) if resolved else 0,
        "total_wagered": round(total_wagered, 2),
        "total_pnl": round(total_pnl, 2),
        "roi_pct": round(total_pnl / total_wagered * 100, 2) if total_wagered else 0,
        "peak_balance": round(peak, 2),
        "max_drawdown_pct": drawdown,
        "recent_bets": [dict(r) for r in recent],
    }


# ═══════════════════════════════════════════════════════════════
#  Phase 2: Calibration & Analysis
# ═══════════════════════════════════════════════════════════════

def calibration_curve(db_path: Path | str = DB_PATH,
                      bins: list[tuple[int, int]] | None = None) -> list[dict]:
    if bins is None:
        bins = [(50, 55), (55, 60), (60, 65), (65, 70),
                (70, 75), (75, 80), (80, 85), (85, 90), (90, 100)]

    with _connect(db_path) as conn:
        rows = conn.execute("""
            SELECT home_prob, away_prob, pick_correct
            FROM predictions
            WHERE resolved_at IS NOT NULL AND pick_correct IS NOT NULL
        """).fetchall()

    bucket_data: dict[str, list[int]] = {}
    for lo, hi in bins:
        bucket_data[f"{lo}-{hi}"] = []

    for row in rows:
        conf = max(float(row["home_prob"]), float(row["away_prob"]))
        correct = int(row["pick_correct"])
        for lo, hi in bins:
            if lo <= conf < hi or (hi == 100 and conf == 100):
                bucket_data[f"{lo}-{hi}"].append(correct)
                break

    result = []
    for lo, hi in bins:
        key = f"{lo}-{hi}"
        vals = bucket_data[key]
        count = len(vals)
        wins = sum(vals)
        actual = round(wins / count * 100, 1) if count else 0
        expected_mid = (lo + hi) / 2
        result.append({
            "bin": key,
            "count": count,
            "wins": wins,
            "actual_wr": actual,
            "expected_mid": expected_mid,
            "gap": round(actual - expected_mid, 1),
        })
    return result


def roi_analysis(db_path: Path | str = DB_PATH) -> dict:
    with _connect(db_path) as conn:
        def _agg(where: str = "1=1") -> dict:
            row = conn.execute(f"""
                SELECT COUNT(*) AS n,
                       COALESCE(SUM(stake), 0) AS wagered,
                       COALESCE(SUM(pnl), 0) AS pnl,
                       AVG(edge) AS avg_edge
                FROM bets WHERE result IN ('win','loss','push') AND {where}
            """).fetchone()
            n = row["n"]
            w = float(row["wagered"])
            p = float(row["pnl"])
            return {
                "bets": n,
                "wagered": round(w, 2),
                "pnl": round(p, 2),
                "roi_pct": round(p / w * 100, 2) if w else 0,
                "avg_edge": round(float(row["avg_edge"] or 0) * 100, 2),
            }

        overall = _agg()
        by_type = {}
        for bt in ("spread", "ou", "moneyline"):
            by_type[bt] = _agg(f"bet_type='{bt}'")

        monthly = conn.execute("""
            SELECT SUBSTR(game_date, 1, 6) AS month,
                   COUNT(*) AS n,
                   COALESCE(SUM(stake), 0) AS wagered,
                   COALESCE(SUM(pnl), 0) AS pnl
            FROM bets WHERE result IN ('win','loss','push')
            GROUP BY month ORDER BY month
        """).fetchall()

        by_month = []
        for r in monthly:
            w = float(r["wagered"])
            p = float(r["pnl"])
            by_month.append({
                "month": r["month"],
                "bets": r["n"],
                "wagered": round(w, 2),
                "pnl": round(p, 2),
                "roi_pct": round(p / w * 100, 2) if w else 0,
            })

        edge_buckets = [
            ("0-2%", 0, 0.02), ("2-5%", 0.02, 0.05),
            ("5-10%", 0.05, 0.10), ("10%+", 0.10, 1.0),
        ]
        by_edge = []
        for label, lo, hi in edge_buckets:
            row = conn.execute("""
                SELECT COUNT(*) AS n,
                       COALESCE(SUM(stake), 0) AS wagered,
                       COALESCE(SUM(pnl), 0) AS pnl
                FROM bets
                WHERE result IN ('win','loss','push')
                  AND edge >= ? AND edge < ?
            """, (lo, hi)).fetchone()
            w = float(row["wagered"])
            p = float(row["pnl"])
            by_edge.append({
                "edge_range": label,
                "bets": row["n"],
                "roi_pct": round(p / w * 100, 2) if w else 0,
            })

    return {
        "overall": overall,
        "by_type": by_type,
        "by_month": by_month,
        "by_edge": by_edge,
    }


def scenario_analysis(db_path: Path | str = DB_PATH) -> dict:
    with _connect(db_path) as conn:
        rows = conn.execute("""
            SELECT home_prob, away_prob, pick_correct, margin_error,
                   b2b_home, b2b_away, rest_home, rest_away,
                   home_elo, away_elo, pred_spread
            FROM predictions
            WHERE resolved_at IS NOT NULL AND pick_correct IS NOT NULL
        """).fetchall()

    def _slice(filtered: list) -> dict:
        n = len(filtered)
        if n == 0:
            return {"count": 0, "wins": 0, "wr": 0, "avg_margin_err": None}
        wins = sum(1 for r in filtered if r["pick_correct"])
        errs = [abs(r["margin_error"]) for r in filtered
                if r["margin_error"] is not None]
        return {
            "count": n,
            "wins": wins,
            "wr": round(wins / n * 100, 1),
            "avg_margin_err": round(sum(errs) / len(errs), 1) if errs else None,
        }

    data = [dict(r) for r in rows]

    b2b = {
        "neither_b2b": _slice([d for d in data
                               if not d["b2b_home"] and not d["b2b_away"]]),
        "home_b2b": _slice([d for d in data if d["b2b_home"]]),
        "away_b2b": _slice([d for d in data if d["b2b_away"]]),
    }

    rest_adv_buckets = {}
    for d in data:
        rh = d["rest_home"] or 0
        ra = d["rest_away"] or 0
        diff = rh - ra
        if diff <= -2:
            key = "-2+"
        elif diff == -1:
            key = "-1"
        elif diff == 0:
            key = "0"
        elif diff == 1:
            key = "+1"
        else:
            key = "+2+"
        rest_adv_buckets.setdefault(key, []).append(d)
    rest_advantage = {k: _slice(v) for k, v in sorted(rest_adv_buckets.items())}

    elo_buckets = {"0-50": [], "50-100": [], "100-200": [], "200+": []}
    for d in data:
        gap = abs((d["home_elo"] or 1500) - (d["away_elo"] or 1500))
        if gap < 50:
            elo_buckets["0-50"].append(d)
        elif gap < 100:
            elo_buckets["50-100"].append(d)
        elif gap < 200:
            elo_buckets["100-200"].append(d)
        else:
            elo_buckets["200+"].append(d)
    elo_gap = {k: _slice(v) for k, v in elo_buckets.items()}

    home_pick = _slice([d for d in data
                        if d["home_prob"] >= d["away_prob"]])
    away_pick = _slice([d for d in data
                        if d["home_prob"] < d["away_prob"]])

    spread_buckets = {"0-3": [], "3-7": [], "7-12": [], "12+": []}
    for d in data:
        sp = abs(d["pred_spread"] or 0)
        if sp < 3:
            spread_buckets["0-3"].append(d)
        elif sp < 7:
            spread_buckets["3-7"].append(d)
        elif sp < 12:
            spread_buckets["7-12"].append(d)
        else:
            spread_buckets["12+"].append(d)
    spread_size = {k: _slice(v) for k, v in spread_buckets.items()}

    return {
        "total_games": len(data),
        "b2b": b2b,
        "rest_advantage": rest_advantage,
        "elo_gap": elo_gap,
        "pick_side": {"home": home_pick, "away": away_pick},
        "spread_size": spread_size,
    }


# ═══════════════════════════════════════════════════════════════
#  Phase 3: Kelly Filter & Recommendations
# ═══════════════════════════════════════════════════════════════

# ── 階段5：真錢升級就緒判斷 ────────────────────────────────────────

# 升級真錢的門檻（全部須滿足才 go）
READINESS_MIN_SETTLED = 100      # 已結算 paper bet 數（足夠樣本）
READINESS_MIN_ROI = 0.0          # paper 每注 ROI 須 > 0（真實賠率口徑）
READINESS_MIN_CLV_SAMPLES = 50   # CLV 樣本數（階段4 收盤線累積後才有）
READINESS_MIN_CLV = 0.0          # 平均 CLV 須 > 0


def real_money_readiness(db_path: Path | str = DB_PATH) -> dict:
    """檢查是否達到「從 paper 升級真錢」的門檻，回傳逐項 go/no-go。

    依據 recommended_picks 的真實損益（pnl_units，階段1）+ CLV（階段4，尚未上線）。
    任一項未過即整體 no-go——這是刻意保守，寧可多等資料。
    """
    with _connect(db_path) as conn:
        row = conn.execute("""
            SELECT COUNT(*) AS settled,
                   SUM(CASE WHEN pnl_units IS NOT NULL THEN 1 ELSE 0 END) AS with_pnl,
                   COALESCE(SUM(pnl_units), 0) AS total_pnl
            FROM recommended_picks
            WHERE correct IN (0, 1)
        """).fetchone()
        # CLV 欄位可能尚未建立（階段4），容錯
        try:
            clv = conn.execute("""
                SELECT COUNT(clv) AS n, COALESCE(AVG(clv), 0) AS avg_clv
                FROM recommended_picks WHERE clv IS NOT NULL
            """).fetchone()
            clv_n, clv_avg = int(clv["n"] or 0), float(clv["avg_clv"] or 0)
        except sqlite3.OperationalError:
            clv_n, clv_avg = 0, 0.0

    settled = int(row["settled"] or 0)
    with_pnl = int(row["with_pnl"] or 0)
    total_pnl = float(row["total_pnl"] or 0)
    roi = (total_pnl / with_pnl) if with_pnl else None

    checks = [
        {"name": "樣本數", "pass": settled >= READINESS_MIN_SETTLED,
         "detail": f"{settled}/{READINESS_MIN_SETTLED} 已結算"},
        {"name": "paper ROI>0", "pass": roi is not None and roi > READINESS_MIN_ROI,
         "detail": f"每注 {roi*100:+.1f}%" if roi is not None else "無資料"},
        {"name": "CLV 樣本", "pass": clv_n >= READINESS_MIN_CLV_SAMPLES,
         "detail": f"{clv_n}/{READINESS_MIN_CLV_SAMPLES}（階段4 後累積）"},
        {"name": "CLV>0", "pass": clv_n >= READINESS_MIN_CLV_SAMPLES and clv_avg > READINESS_MIN_CLV,
         "detail": f"平均 {clv_avg:+.3f}" if clv_n else "尚無 CLV 資料"},
    ]
    go = all(c["pass"] for c in checks)
    return {
        "ready": go,
        "verdict": "GO ✅ 可考慮小額真錢" if go else "NO-GO ⛔ 繼續 paper trading",
        "settled": settled, "paper_roi_per_bet": round(roi, 4) if roi is not None else None,
        "total_units": round(total_pnl, 2),
        "clv_samples": clv_n, "avg_clv": round(clv_avg, 4),
        "checks": checks,
    }


def _print_readiness(r: dict):
    print("\n" + "═" * 56)
    print("  真錢升級就緒檢查（階段5）")
    print("═" * 56)
    for c in r["checks"]:
        mark = "✅" if c["pass"] else "⛔"
        print(f"  {mark} {c['name']:<12} {c['detail']}")
    print("─" * 56)
    print(f"  結論：{r['verdict']}")
    print("═" * 56 + "\n")


def bet_filter(db_path: Path | str = DB_PATH,
               candidates: list[dict] | None = None,
               min_sample: int = 10,
               max_gap: float = 0.10) -> list[dict]:
    cal = calibration_curve(db_path)
    cal_map = {c["bin"]: c for c in cal}

    if candidates is None:
        candidates = []
        today = datetime.now().strftime("%Y%m%d")
        rmse = _calibrated_sigma()
        with _connect(db_path) as conn:
            picks = conn.execute("""
                SELECT * FROM recommended_picks
                WHERE game_date >= ? AND correct IS NULL
                ORDER BY edge DESC
            """, (today,)).fetchall()
        for p in picks:
            pt = p["pick_type"]
            ms = p["model_spread"]
            mt = p["model_total"]
            pl = p["pick_line"]

            if pt == "spread" and ms is not None and pl is not None:
                mp = _margin_to_cover_prob(ms, -pl, rmse)
            elif pt == "ou" and mt is not None and pl is not None:
                if p["pick_target"] == "over":
                    mp = _margin_to_cover_prob(mt, pl, rmse)
                else:
                    mp = 1 - _margin_to_cover_prob(mt, pl, rmse)
            else:
                continue

            ks = kelly_sizing(mp, DEFAULT_STAKE_ODDS)
            candidates.append({
                "game_date": p["game_date"],
                "home": p["home"],
                "away": p["away"],
                "bet_type": pt,
                "bet_side": p["pick_target"],
                "bet_line": pl,
                "model_prob": round(mp, 4),
                "edge": ks["edge"],
                "kelly_pct": ks["stake_pct"],
                "detail": p["pick_detail"],
            })

    passed = []
    for c in candidates:
        conf = c["model_prob"] * 100
        matched_bin = None
        for b in cal:
            lo, hi = b["bin"].split("-")
            if float(lo) <= conf < float(hi) or (float(hi) == 100 and conf == 100):
                matched_bin = b
                break

        if matched_bin is None:
            c["filter_reason"] = "no_bin"
            continue
        if matched_bin["count"] < min_sample:
            c["filter_reason"] = f"low_sample ({matched_bin['count']}<{min_sample})"
            continue
        gap = abs(matched_bin["actual_wr"] - matched_bin["expected_mid"]) / 100
        if gap > max_gap:
            c["filter_reason"] = f"poor_cal (gap={gap:.0%})"
            continue
        if c.get("edge", 0) <= 0:
            c["filter_reason"] = "no_edge"
            continue

        c["cal_bin"] = matched_bin["bin"]
        c["cal_actual_wr"] = matched_bin["actual_wr"]
        passed.append(c)

    return passed


# ═══════════════════════════════════════════════════════════════
#  CLI Output Formatting
# ═══════════════════════════════════════════════════════════════

def _print_summary(s: dict):
    print("\n╔══════════════════════════════════════╗")
    print("║        銀行帳戶 / Bankroll           ║")
    print("╠══════════════════════════════════════╣")
    print(f"  初始資金:   {s['initial_balance']:>10.2f}")
    print(f"  目前餘額:   {s['current_balance']:>10.2f}")
    print(f"  峰值:       {s['peak_balance']:>10.2f}")
    print(f"  最大回撤:   {s['max_drawdown_pct']:>9.1f}%")
    print("╠══════════════════════════════════════╣")
    print(f"  總下注:     {s['total_bets']:>10d}")
    print(f"  已結算:     {s['resolved_bets']:>10d}")
    print(f"  待結算:     {s['pending_bets']:>10d}")
    print(f"  勝 / 負 / 平: {s['wins']}W / {s['losses']}L / {s['pushes']}P")
    print(f"  勝率:       {s['win_rate']:>9.1f}%")
    print("╠══════════════════════════════════════╣")
    print(f"  總投注額:   {s['total_wagered']:>10.2f}")
    print(f"  總損益:     {s['total_pnl']:>+10.2f}")
    print(f"  ROI:        {s['roi_pct']:>+9.2f}%")
    print("╚══════════════════════════════════════╝")

    if s["recent_bets"]:
        print("\n  最近下注:")
        for b in s["recent_bets"][:5]:
            res = b["result"] or "pending"
            pnl_str = f"{b['pnl']:+.2f}" if b["pnl"] is not None else "   --"
            print(f"    {b['game_date']} {b['away']}@{b['home']} "
                  f"{b['bet_type']}/{b['bet_side']} "
                  f"${b['stake']:.1f} → {res} {pnl_str}")


def _print_calibration(cal: list[dict]):
    print("\n╔══════════════════════════════════════════════════╗")
    print("║           校準曲線 / Calibration Curve           ║")
    print("╠══════════════════════════════════════════════════╣")
    print(f"  {'Bin':>7}  {'N':>5}  {'Wins':>5}  {'Actual%':>8}  {'Expected%':>9}  {'Gap':>6}")
    print(f"  {'─'*7}  {'─'*5}  {'─'*5}  {'─'*8}  {'─'*9}  {'─'*6}")
    for c in cal:
        marker = ""
        if c["count"] > 0 and abs(c["gap"]) > 5:
            marker = " ⚠" if c["gap"] < 0 else " ★"
        print(f"  {c['bin']:>7}  {c['count']:>5}  {c['wins']:>5}  "
              f"{c['actual_wr']:>7.1f}%  {c['expected_mid']:>8.1f}%  "
              f"{c['gap']:>+5.1f}{marker}")
    print("╚══════════════════════════════════════════════════╝")


def _print_roi(roi: dict):
    print("\n╔══════════════════════════════════════════════════╗")
    print("║              ROI 分析 / ROI Analysis              ║")
    print("╠══════════════════════════════════════════════════╣")
    o = roi["overall"]
    print(f"  整體: {o['bets']}注 | 投注 ${o['wagered']:.0f} | "
          f"損益 ${o['pnl']:+.2f} | ROI {o['roi_pct']:+.2f}% | "
          f"理論EV {o['avg_edge']:.1f}%")
    print()
    for bt, d in roi["by_type"].items():
        if d["bets"]:
            print(f"  {bt:>10}: {d['bets']}注 | ROI {d['roi_pct']:+.2f}% | "
                  f"理論EV {d['avg_edge']:.1f}%")

    if roi["by_month"]:
        print(f"\n  {'月份':>8}  {'注數':>5}  {'ROI':>8}")
        for m in roi["by_month"]:
            print(f"  {m['month']:>8}  {m['bets']:>5}  {m['roi_pct']:>+7.2f}%")

    if roi["by_edge"]:
        print(f"\n  {'Edge區間':>10}  {'注數':>5}  {'ROI':>8}")
        for e in roi["by_edge"]:
            if e["bets"]:
                print(f"  {e['edge_range']:>10}  {e['bets']:>5}  {e['roi_pct']:>+7.2f}%")
    print("╚══════════════════════════════════════════════════╝")


def _print_scenarios(sc: dict):
    print("\n╔══════════════════════════════════════════════════╗")
    print("║            場景分析 / Scenario Analysis           ║")
    print(f"╠══════════════════════════════════════════════════╣")
    print(f"  總已結算: {sc['total_games']} 場\n")

    def _row(label: str, d: dict):
        me = f"{d['avg_margin_err']:.1f}" if d["avg_margin_err"] is not None else "--"
        print(f"    {label:<16} {d['count']:>4}場  {d['wr']:>5.1f}%  MAE {me}")

    print("  ▸ 背靠背 (B2B)")
    for k, v in sc["b2b"].items():
        _row(k, v)

    print("\n  ▸ 休息天數差 (主場-客場)")
    for k, v in sc["rest_advantage"].items():
        _row(f"rest_diff={k}", v)

    print("\n  ▸ Elo 差距")
    for k, v in sc["elo_gap"].items():
        _row(f"gap={k}", v)

    print("\n  ▸ 選邊 (主/客)")
    for k, v in sc["pick_side"].items():
        _row(k, v)

    print("\n  ▸ 預測分差大小")
    for k, v in sc["spread_size"].items():
        _row(f"|spread|={k}", v)
    print("╚══════════════════════════════════════════════════╝")


def _print_recommendations(recs: list[dict]):
    print("\n╔══════════════════════════════════════════════════╗")
    print("║       Kelly 推薦 / Filtered Recommendations      ║")
    print("╠══════════════════════════════════════════════════╣")
    if not recs:
        print("  目前無通過過濾的推薦")
    for r in recs:
        print(f"  {r['game_date']} {r.get('away','')}@{r.get('home','')} "
              f"{r['bet_type']}/{r['bet_side']}")
        print(f"    模型機率: {r['model_prob']:.1%} | Edge: {r['edge']:.1%} | "
              f"Kelly: {r['kelly_pct']:.1f}% | "
              f"校準bin: {r.get('cal_bin','-')} (實際{r.get('cal_actual_wr',0):.0f}%)")
    print("╚══════════════════════════════════════════════════╝")


# ═══════════════════════════════════════════════════════════════
#  Main CLI
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="NBA 量化追蹤")
    parser.add_argument("--init-bankroll", type=float, metavar="AMT",
                        help="初始化銀行帳戶")
    parser.add_argument("--place", action="store_true",
                        help="從推薦自動下紙上注")
    parser.add_argument("--resolve", action="store_true",
                        help="解析已結束比賽的下注")
    parser.add_argument("--summary", action="store_true",
                        help="銀行帳戶摘要")
    parser.add_argument("--calibration", action="store_true",
                        help="校準曲線")
    parser.add_argument("--roi", action="store_true",
                        help="ROI 分析")
    parser.add_argument("--scenarios", action="store_true",
                        help="場景分析")
    parser.add_argument("--recommend", action="store_true",
                        help="Kelly 過濾推薦")
    parser.add_argument("--readiness", action="store_true",
                        help="真錢升級就緒檢查（階段5）")
    parser.add_argument("--report", action="store_true",
                        help="完整報告")
    parser.add_argument("--json", action="store_true",
                        help="JSON 輸出")
    args = parser.parse_args()

    init_db(DB_PATH)

    if args.init_bankroll:
        init_bankroll(DB_PATH, args.init_bankroll)
        return

    if args.report:
        args.summary = args.calibration = args.roi = args.scenarios = True
        args.recommend = True

    if args.resolve:
        stats = resolve_bets(DB_PATH)
        print(f"[tracker] 解析 {stats['resolved']} 注: "
              f"{stats['wins']}W/{stats['losses']}L/{stats['pushes']}P | "
              f"PnL: {stats['total_pnl']:+.2f}")

    if args.place:
        placed = place_from_picks(DB_PATH, verbose=not args.json)
        print(f"[tracker] 下注 {len(placed)} 注")
        for p in placed:
            print(f"  {p['game_date']} {p['away']}@{p['home']} "
                  f"{p['bet_type']}/{p['bet_side']} "
                  f"${p['stake']:.1f} (Kelly {p['kelly_pct']:.1f}%)")

    if args.summary:
        s = get_bankroll_summary(DB_PATH)
        if args.json:
            print(json.dumps(s, indent=2, ensure_ascii=False))
        else:
            _print_summary(s)

    if args.calibration:
        cal = calibration_curve(DB_PATH)
        if args.json:
            print(json.dumps(cal, indent=2, ensure_ascii=False))
        else:
            _print_calibration(cal)

    if args.roi:
        r = roi_analysis(DB_PATH)
        if args.json:
            print(json.dumps(r, indent=2, ensure_ascii=False))
        else:
            _print_roi(r)

    if args.scenarios:
        sc = scenario_analysis(DB_PATH)
        if args.json:
            print(json.dumps(sc, indent=2, ensure_ascii=False))
        else:
            _print_scenarios(sc)

    if args.recommend:
        recs = bet_filter(DB_PATH)
        if args.json:
            print(json.dumps(recs, indent=2, ensure_ascii=False))
        else:
            _print_recommendations(recs)

    if args.readiness:
        r = real_money_readiness(DB_PATH)
        if args.json:
            print(json.dumps(r, indent=2, ensure_ascii=False))
        else:
            _print_readiness(r)

    if not any([args.resolve, args.place, args.summary, args.calibration,
                args.roi, args.scenarios, args.recommend, args.readiness]):
        parser.print_help()


if __name__ == "__main__":
    main()

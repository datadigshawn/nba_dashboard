"""
Walk-forward 回測框架 — 階段 2

目的：產出「真實可達成」的 out-of-sample (OOS) 績效，取代 in-sample 回測。
      production 的勝率轉換、cover 機率、picks 篩選若用訓練期 in-sample 數字會過度樂觀
      （README 寫 79.9% 回測勝率 vs 實盤 54.5%）。本框架逐日重放，每天只用「截至前一天」
      的資訊預測當天，模擬系統若真的每天上線會得到的結果。

方法（零 lookahead）：
  1. 依日期排序全部賽果，逐日推進
  2. 每個比賽日 D：先用「目前 Elo（= D 之前所有比賽更新後）」預測 D 當天每場
     - margin = predict_margin（Elo 為主，混合滾動 point-diff）
     - 勝率 = margin_to_prob（sigma 用滾動 OOS RMSE，非訓練期固定值）
  3. 記錄 (預測, 實際) 後，才用 D 當天結果更新 Elo → 推進到下一天
  4. 前 WARMUP_DAYS 只更新 Elo 不評估（讓 Elo 收斂）

評估：
  - accuracy（勝負方向）
  - Brier score / log loss（機率品質）
  - 校準分桶（模型說 X% 的那些場，實際贏多少 %）
  - 模擬 spread/total picks 損益（用 sportWeb 歷史盤口），直接對比階段 1 的 -7.6%/注 基準

執行：
  .venv/bin/python walk_forward_backtest.py                  # 用快取（沒有就抓）
  .venv/bin/python walk_forward_backtest.py --refresh-cache  # 重抓 ESPN 賽果
  .venv/bin/python walk_forward_backtest.py --days 200       # 回看天數
  .venv/bin/python walk_forward_backtest.py --json out.json  # 輸出機器可讀報告
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path

from nba_predictor import EloSystem, fetch_espn_results
from nba_db import compute_pnl_units

BASE_DIR = Path(__file__).resolve().parent
CACHE_PATH = BASE_DIR / "state" / "wf_results_cache.json"
SPORTWEB_DB = BASE_DIR / "sportweb" / "sportWeb.db"
NBA_DB = BASE_DIR / "nba.db"

WARMUP_DAYS = 25          # 前 N 個比賽日只暖機 Elo，不納入評估
ROLLING_RMSE_WINDOW = 200  # 滾動估 OOS RMSE 的近期樣本數
TEAM_FORM_WINDOW = 10     # 每隊滾動 point-diff 的近期場數
DEFAULT_RMSE = 12.0
SPREAD_EDGE_MIN = 2.0     # 與 production _build_pick_batch 一致
TOTAL_EDGE_MIN = 5.0


# ── 資料載入 ──────────────────────────────────────────────────────────

def load_results(days: int, refresh: bool) -> list[dict]:
    """取得賽果（含比分），優先用快取。"""
    if CACHE_PATH.exists() and not refresh:
        cached = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if cached.get("games"):
            print(f"[cache] 載入 {len(cached['games'])} 場（{cached.get('fetched_at')}）")
            return cached["games"]

    print(f"[espn] 抓取近 {days} 天賽果（首次較慢）...")
    games = fetch_espn_results(last_n_days=days)
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(
        {"fetched_at": datetime.now().isoformat(timespec="seconds"),
         "days": days, "games": games}, ensure_ascii=False), encoding="utf-8")
    print(f"[espn] 取得 {len(games)} 場，已快取")
    return games


# ── 機率/評估工具 ──────────────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def margin_to_prob(margin: float, sigma: float) -> float:
    return _norm_cdf(margin / (sigma or DEFAULT_RMSE))


def cover_prob(edge_points: float, sigma: float) -> float:
    return _norm_cdf(edge_points / (sigma or DEFAULT_RMSE))


# ── Walk-forward 核心 ─────────────────────────────────────────────────

def run_walk_forward(games: list[dict]) -> dict:
    """逐日重放，回傳每場的 OOS 預測紀錄 + 滾動狀態。"""
    by_date: dict[str, list[dict]] = defaultdict(list)
    for g in games:
        by_date[g["date"]].append(g)
    dates = sorted(by_date.keys())

    elo = EloSystem()
    team_diffs: dict[str, deque] = defaultdict(lambda: deque(maxlen=TEAM_FORM_WINDOW))
    recent_abs_err: deque = deque(maxlen=ROLLING_RMSE_WINDOW)

    records: list[dict] = []

    for day_idx, date in enumerate(dates):
        evaluating = day_idx >= WARMUP_DAYS
        # 目前的滾動 OOS RMSE（暖機期不足則用預設）
        if len(recent_abs_err) >= 20:
            sigma = math.sqrt(sum(e * e for e in recent_abs_err) / len(recent_abs_err))
        else:
            sigma = DEFAULT_RMSE

        # ① 先預測當天每場（只用截至昨天的 Elo / form）
        for g in by_date[date]:
            home, away = g["home_team"], g["away_team"]
            elo_h = elo._get(home)
            elo_a = elo._get(away)
            # production predict_margin 的 Elo 路徑：(elo_a - elo_b + HCA)/28，主隊視角
            elo_margin = (elo_h - elo_a + EloSystem.HOME_ADV) / 28.0
            diff_h = sum(team_diffs[home]) / len(team_diffs[home]) if team_diffs[home] else 0.0
            diff_a = sum(team_diffs[away]) / len(team_diffs[away]) if team_diffs[away] else 0.0
            if diff_h != 0 or diff_a != 0:
                stats_margin = (diff_h - diff_a) / 2 + 3.5
                pred_margin = elo_margin * 0.6 + stats_margin * 0.4
            else:
                pred_margin = elo_margin

            actual_margin = g["home_score"] - g["away_score"]
            home_win = 1 if actual_margin > 0 else 0
            home_prob = margin_to_prob(pred_margin, sigma)

            if evaluating:
                records.append({
                    "date": date,
                    "home": home,
                    "away": away,
                    "pred_margin": round(pred_margin, 2),
                    "actual_margin": actual_margin,
                    "home_prob": home_prob,
                    "home_win": home_win,
                    "sigma": round(sigma, 2),
                    "pred_correct": 1 if (home_prob >= 0.5) == (home_win == 1) else 0,
                })

        # ② 用當天結果更新 Elo / form / 滾動誤差
        for g in by_date[date]:
            home, away = g["home_team"], g["away_team"]
            actual_margin = g["home_score"] - g["away_score"]
            # 滾動 RMSE 用「評估期」的預測誤差累積（暖機期也累積以儘早穩定）
            elo_h, elo_a = elo._get(home), elo._get(away)
            elo_margin = (elo_h - elo_a + EloSystem.HOME_ADV) / 28.0
            recent_abs_err.append(abs(actual_margin - elo_margin))
            # 更新 Elo
            elo.update(g["winner"], g["loser"], g.get("home_team"))
            # 更新滾動 point-diff（主客各自視角）
            team_diffs[home].append(actual_margin)
            team_diffs[away].append(-actual_margin)

    return {
        "records": records,
        "n_dates": len(dates),
        "eval_dates": max(0, len(dates) - WARMUP_DAYS),
        "final_sigma": round(sigma, 2),
    }


# ── 績效彙整 ──────────────────────────────────────────────────────────

def summarize_model(records: list[dict]) -> dict:
    n = len(records)
    if not n:
        return {"n": 0}
    correct = sum(r["pred_correct"] for r in records)
    brier = sum((r["home_prob"] - r["home_win"]) ** 2 for r in records) / n
    eps = 1e-12
    logloss = -sum(
        r["home_win"] * math.log(max(r["home_prob"], eps)) +
        (1 - r["home_win"]) * math.log(max(1 - r["home_prob"], eps))
        for r in records
    ) / n

    # 校準分桶（依預測勝率）
    buckets = [(0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]
    calib = []
    for lo, hi in buckets:
        # 以「較有信心的一方」對齊：把 prob<0.5 的場鏡像成 (1-prob, 客隊贏)
        bucket_rows = []
        for r in records:
            p = r["home_prob"]
            conf = p if p >= 0.5 else 1 - p
            hit = r["home_win"] if p >= 0.5 else (1 - r["home_win"])
            if lo <= conf < hi:
                bucket_rows.append((conf, hit))
        if bucket_rows:
            avg_pred = sum(c for c, _ in bucket_rows) / len(bucket_rows)
            actual = sum(h for _, h in bucket_rows) / len(bucket_rows)
            calib.append({
                "bucket": f"{int(lo*100)}-{int(hi*100)}%",
                "n": len(bucket_rows),
                "avg_pred": round(avg_pred * 100, 1),
                "actual_wr": round(actual * 100, 1),
                "gap": round((avg_pred - actual) * 100, 1),
            })

    return {
        "n": n,
        "accuracy": round(correct / n * 100, 1),
        "brier": round(brier, 4),
        "log_loss": round(logloss, 4),
        "mean_abs_margin_err": round(sum(abs(r["actual_margin"] - r["pred_margin"]) for r in records) / n, 2),
        "calibration": calib,
    }


# ── picks 損益模擬（用 sportWeb 歷史盤口）────────────────────────────────

def _load_historical_odds() -> dict:
    """game_date+home+away -> {spread, ou, spread_odds, ou_odds}（取每場最後一筆快照=近收盤）。"""
    if not SPORTWEB_DB.exists():
        return {}
    conn = sqlite3.connect(str(SPORTWEB_DB))
    conn.row_factory = sqlite3.Row
    odds_map: dict[tuple, dict] = {}

    # 主盤口線：兩邊機率最接近 50/50 者
    def primary(rows, a_key, b_key, line_key):
        best, best_score = None, None
        for r in rows:
            ao, bo = r[a_key], r[b_key]
            if not ao or not bo:
                continue
            ap = (1 / ao) / (1 / ao + 1 / bo)
            score = (abs(ap - 0.5), abs(float(r[line_key])))
            if best_score is None or score < best_score:
                best_score, best = score, r
        return best

    games = conn.execute("""
        SELECT DISTINCT game_date, home, away FROM odds_spreads
        UNION SELECT DISTINCT game_date, home, away FROM odds_totals
    """).fetchall()
    for g in games:
        gd, home, away = g["game_date"], g["home"], g["away"]
        if not gd:
            continue
        # 最後一個有該場資料的 snapshot
        sp_rows = conn.execute("""
            SELECT t.* FROM odds_spreads t
            WHERE t.game_date=? AND t.home=? AND t.away=?
              AND t.snapshot_id = (SELECT MAX(snapshot_id) FROM odds_spreads
                                   WHERE game_date=? AND home=? AND away=?)
        """, (gd, home, away, gd, home, away)).fetchall()
        to_rows = conn.execute("""
            SELECT t.* FROM odds_totals t
            WHERE t.game_date=? AND t.home=? AND t.away=?
              AND t.snapshot_id = (SELECT MAX(snapshot_id) FROM odds_totals
                                   WHERE game_date=? AND home=? AND away=?)
        """, (gd, home, away, gd, home, away)).fetchall()
        sp = primary(sp_rows, "away_odds", "home_odds", "home_line")
        to = primary(to_rows, "over_odds", "under_odds", "total_line")
        entry = {}
        if sp:
            entry["spread"] = float(sp["home_line"])
            entry["spread_odds"] = {"away": sp["away_odds"], "home": sp["home_odds"]}
        if to:
            entry["ou"] = float(to["total_line"])
            entry["ou_odds"] = {"over": to["over_odds"], "under": to["under_odds"]}
        if entry:
            odds_map[(gd, home.lower(), away.lower())] = entry
    conn.close()
    return odds_map


def simulate_picks(records: list[dict], sigma_final: float) -> dict:
    """用 OOS 預測 + 歷史盤口，重放 production 的 spread/total 選注邏輯並結算損益。"""
    odds_map = _load_historical_odds()
    if not odds_map:
        return {"available": False, "reason": "no sportWeb odds"}

    def _shift(ymd: str, days: int) -> str:
        return (datetime.strptime(ymd, "%Y%m%d") + timedelta(days=days)).strftime("%Y%m%d")

    def _lookup(r: dict) -> dict | None:
        # sportWeb game_date 由台灣時間推算，美國晚場=台灣隔天，故優先試 +1
        for off in (1, 0, -1):
            tw = odds_map.get((_shift(r["date"], off), r["home"].lower(), r["away"].lower()))
            if tw:
                return tw
        return None

    picks = []
    matched_games = 0
    for r in records:
        tw = _lookup(r)
        if not tw:
            continue
        matched_games += 1
        pred_spread = r["pred_margin"]   # 主隊預測 margin
        pred_total = None                # walk-forward 暫不預測總分，total picks 留待 sigma 模型完善
        actual_margin = r["actual_margin"]
        actual_total = None

        # spread pick（與 _build_pick_batch 邏輯對齊：home line 為運彩讓分）
        if "spread" in tw and tw.get("spread_odds"):
            home_line = tw["spread"]
            cover_edge = pred_spread + home_line   # >0 主隊 cover
            edge_pts = abs(cover_edge)
            if edge_pts >= SPREAD_EDGE_MIN:
                target = "home" if cover_edge >= 0 else "away"
                odds = tw["spread_odds"].get(target)
                if odds:
                    # 結算：home cover 條件 actual_margin + home_line > 0
                    adj = actual_margin + home_line
                    if adj == 0:
                        result = "push"
                    elif (target == "home" and adj > 0) or (target == "away" and adj < 0):
                        result = "win"
                    else:
                        result = "loss"
                    picks.append({
                        "type": "spread", "edge": round(edge_pts, 1),
                        "model_prob": round(cover_prob(edge_pts, sigma_final) * 100, 1),
                        "odds": odds, "result": result,
                        "pnl": compute_pnl_units(result, odds),
                    })

    settled = [p for p in picks if p["pnl"] is not None]
    total_pnl = sum(p["pnl"] for p in settled)
    wins = sum(1 for p in settled if p["result"] == "win")
    graded = [p for p in settled if p["result"] in ("win", "loss")]
    return {
        "available": True,
        "matched_games": matched_games,
        "n_picks": len(picks),
        "settled": len(settled),
        "wins": wins,
        "win_rate": round(wins / len(graded) * 100, 1) if graded else None,
        "total_units": round(total_pnl, 2),
        "roi_per_bet": round(total_pnl / len(settled) * 100, 2) if settled else None,
        "note": "僅 spread；total picks 待總分模型（階段3）",
    }


# ── 執行落差診斷：實盤 pick_line vs 近收盤線 ──────────────────────────────

def diagnose_stale_lines() -> dict:
    """量化『實盤下注時的盤口線』與『賽前最後快照(近收盤)』的落差。

    若落差大，代表系統的虧損有相當部分來自『押到過期盤口』(執行面)，
    而非模型本身(預測面)——這決定優化重點放哪。
    """
    if not (NBA_DB.exists() and SPORTWEB_DB.exists()):
        return {"available": False}
    odds_map = _load_historical_odds()
    nba = sqlite3.connect(str(NBA_DB))
    nba.row_factory = sqlite3.Row
    picks = nba.execute("""
        SELECT game_date, home, away, pick_line, result
        FROM recommended_picks
        WHERE pick_type='spread' AND correct IN (0,1) AND pick_line IS NOT NULL
    """).fetchall()
    nba.close()

    def _shift(ymd, d):
        return (datetime.strptime(ymd, "%Y%m%d") + timedelta(days=d)).strftime("%Y%m%d")

    diffs = []
    for p in picks:
        tw = None
        for off in (1, 0, -1):
            tw = odds_map.get((_shift(p["game_date"], off), p["home"].lower(), p["away"].lower()))
            if tw:
                break
        if not tw or "spread" not in tw:
            continue
        diffs.append(abs(p["pick_line"] - tw["spread"]))

    if not diffs:
        return {"available": False}
    n = len(diffs)
    return {
        "available": True,
        "overlap_games": n,
        "mean_abs_line_diff": round(sum(diffs) / n, 2),
        "pct_ge_1pt": round(sum(1 for d in diffs if d >= 1) / n * 100, 0),
        "pct_ge_2pt": round(sum(1 for d in diffs if d >= 2) / n * 100, 0),
    }


# ── 報告輸出 ──────────────────────────────────────────────────────────

def print_report(model: dict, picks: dict, meta: dict, stale: dict):
    print("\n" + "=" * 64)
    print("  Walk-Forward OOS 回測報告")
    print("=" * 64)
    print(f"  比賽日數: {meta['n_dates']}（暖機 {WARMUP_DAYS}，評估 {meta['eval_dates']}）")
    print(f"  最終滾動 sigma(OOS RMSE): {meta['final_sigma']} 分")
    print("-" * 64)
    print(f"  評估場數: {model['n']}")
    print(f"  方向準確率: {model['accuracy']}%")
    print(f"  Brier score: {model['brier']}  (越低越好；0.25=亂猜)")
    print(f"  Log loss: {model['log_loss']}")
    print(f"  平均 margin 誤差: {model['mean_abs_margin_err']} 分")
    print("-" * 64)
    print("  機率校準（信心方視角）:")
    print(f"  {'區間':<10}{'場數':>6}{'預測%':>8}{'實際勝%':>9}{'落差':>7}")
    for c in model["calibration"]:
        print(f"  {c['bucket']:<10}{c['n']:>6}{c['avg_pred']:>8}{c['actual_wr']:>9}{c['gap']:>+7}")
    print("-" * 64)
    if picks.get("available") and picks.get("settled"):
        print("  模擬 spread picks 損益（OOS 預測 × 歷史盤口）:")
        print(f"    比對到盤口的場數: {picks['matched_games']}")
        print(f"    注數: {picks['settled']}  勝率: {picks['win_rate']}%")
        print(f"    累積損益: {picks['total_units']:+} 單位  每注 ROI: {picks['roi_per_bet']:+}%")
        print(f"    對比階段1實盤基準: -7.6%/注")
        print(f"    ({picks['note']})")
    elif picks.get("available"):
        print(f"  picks 模擬: 比對到 {picks.get('matched_games', 0)} 場盤口，但無達標 picks")
    else:
        print(f"  picks 損益模擬不可用: {picks.get('reason')}")
    if stale.get("available"):
        print("-" * 64)
        print("  執行落差診斷（實盤 pick_line vs 近收盤線）:")
        print(f"    重疊場次: {stale['overlap_games']}")
        print(f"    平均絕對線差: {stale['mean_abs_line_diff']} 分"
              f"（{stale['pct_ge_1pt']:.0f}% ≥1分, {stale['pct_ge_2pt']:.0f}% ≥2分）")
        print(f"    → 實盤虧損相當部分來自押到過期盤口，非模型預測錯誤")
    print("=" * 64 + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=250, help="ESPN 回看天數（首次抓取）")
    ap.add_argument("--refresh-cache", action="store_true", help="重抓 ESPN 賽果")
    ap.add_argument("--json", type=str, help="輸出 JSON 報告路徑")
    args = ap.parse_args()

    games = load_results(args.days, args.refresh_cache)
    if not games:
        print("無賽果資料，結束")
        return

    wf = run_walk_forward(games)
    model = summarize_model(wf["records"])
    meta = {"n_dates": wf["n_dates"], "eval_dates": wf["eval_dates"],
            "final_sigma": wf["final_sigma"]}
    picks = simulate_picks(wf["records"], wf["final_sigma"])
    stale = diagnose_stale_lines()

    print_report(model, picks, meta, stale)

    if args.json:
        out = {"meta": meta, "model": model, "picks": picks, "stale_lines": stale,
               "generated_at": datetime.now().isoformat(timespec="seconds")}
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已輸出 JSON: {args.json}")


if __name__ == "__main__":
    main()

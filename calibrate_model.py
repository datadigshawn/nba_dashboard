"""
機率校準器 — 階段 3

核心洞察：margin_to_prob（勝率, threshold=0）與 _cover_prob（讓分 cover, threshold=line）
是同一個轉換 P(margin > t) = Φ((pred_margin - t) / σ)。因此只需要一個校準良好的 σ
（= margin 預測誤差的標準差），就能同時校準勝率與讓分機率。

現況問題：
  - production 用 self.rmse = 8.14（in-sample 訓練 RMSE）→ σ 太小 → 機率過度自信
  - walk-forward 滾動 σ ≈ 16 → 偏保守（74% 區間實際贏 85%）
  - 最優 σ 在兩者之間。本工具用 walk-forward OOS 資料以 log-loss 擬合最優 σ。

只擬合 1 個參數 → 無過擬合風險，且可解釋（= 模型真實 margin 誤差 std）。

產出：state/prob_calibration.json
  { sigma_margin, n, brier_before, brier_after, logloss_before, logloss_after, ... }

執行：
  .venv/bin/python calibrate_model.py            # 擬合 + 報告 + 寫入 artifact
  .venv/bin/python calibrate_model.py --dry-run  # 只報告不寫入
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path

from walk_forward_backtest import (
    run_walk_forward, load_results, _load_historical_odds,
)

BASE_DIR = Path(__file__).resolve().parent
CALIB_PATH = BASE_DIR / "state" / "prob_calibration.json"
PROD_RMSE = 8.14   # 目前 production self.rmse（state/nba_model.json）

SIGMA_MIN, SIGMA_MAX = 6.0, 22.0


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _prob(margin: float, threshold: float, sigma: float) -> float:
    return _norm_cdf((margin - threshold) / sigma)


def log_loss(pairs: list[tuple[float, int]], sigma: float) -> float:
    """pairs = [(pred_margin - threshold, outcome)]; outcome=1 若 margin>threshold。"""
    eps = 1e-12
    s = 0.0
    for edge, y in pairs:
        p = min(max(_norm_cdf(edge / sigma), eps), 1 - eps)
        s += -(y * math.log(p) + (1 - y) * math.log(1 - p))
    return s / len(pairs)


def brier(pairs: list[tuple[float, int]], sigma: float) -> float:
    return sum((_norm_cdf(edge / sigma) - y) ** 2 for edge, y in pairs) / len(pairs)


def fit_sigma(pairs: list[tuple[float, int]]) -> float:
    """黃金分割搜尋最小化 log-loss 的 sigma。"""
    lo, hi = SIGMA_MIN, SIGMA_MAX
    gr = (math.sqrt(5) - 1) / 2
    c = hi - gr * (hi - lo)
    d = lo + gr * (hi - lo)
    fc, fd = log_loss(pairs, c), log_loss(pairs, d)
    for _ in range(60):
        if fc < fd:
            hi, d, fd = d, c, fc
            c = hi - gr * (hi - lo)
            fc = log_loss(pairs, c)
        else:
            lo, c, fc = c, d, fd
            d = lo + gr * (hi - lo)
            fd = log_loss(pairs, d)
        if abs(hi - lo) < 1e-4:
            break
    return round((lo + hi) / 2, 3)


def reliability(pairs: list[tuple[float, int]], sigma: float) -> list[dict]:
    """信心方視角的校準分桶。"""
    buckets = [(0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]
    out = []
    for lo, hi in buckets:
        rows = []
        for edge, y in pairs:
            p = _norm_cdf(edge / sigma)
            conf = p if p >= 0.5 else 1 - p
            hit = y if p >= 0.5 else (1 - y)
            if lo <= conf < hi:
                rows.append((conf, hit))
        if rows:
            ap = sum(c for c, _ in rows) / len(rows)
            aw = sum(h for _, h in rows) / len(rows)
            out.append({"bucket": f"{int(lo*100)}-{int(hi*100)}%", "n": len(rows),
                        "pred": round(ap*100, 1), "actual": round(aw*100, 1),
                        "gap": round((ap-aw)*100, 1)})
    return out


def collect_spread_pairs(records: list[dict]) -> list[tuple[float, int]]:
    """用歷史收盤讓分線，收集 (cover_edge, home_cover) 配對驗證同一 sigma 是否也校準讓分。"""
    from datetime import timedelta
    odds = _load_historical_odds()
    if not odds:
        return []

    def shift(ymd, d):
        return (datetime.strptime(ymd, "%Y%m%d") + timedelta(days=d)).strftime("%Y%m%d")

    pairs = []
    for r in records:
        tw = None
        for off in (1, 0, -1):
            tw = odds.get((shift(r["date"], off), r["home"].lower(), r["away"].lower()))
            if tw:
                break
        if not tw or "spread" not in tw:
            continue
        home_line = tw["spread"]
        cover_edge = r["pred_margin"] + home_line          # >0 模型認為主隊 cover
        adj = r["actual_margin"] + home_line                # >0 主隊實際 cover
        if adj == 0:
            continue
        pairs.append((cover_edge, 1 if adj > 0 else 0))
    return pairs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=250)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    games = load_results(args.days, refresh=False)
    wf = run_walk_forward(games)
    records = wf["records"]

    # moneyline 配對：(pred_margin - 0, home_win)
    ml_pairs = [(r["pred_margin"], r["home_win"]) for r in records]
    sigma = fit_sigma(ml_pairs)

    print("\n" + "=" * 60)
    print("  機率校準報告（階段 3）")
    print("=" * 60)
    print(f"  樣本（OOS 場數）: {len(ml_pairs)}")
    print(f"  擬合最優 sigma_margin: {sigma} 分")
    print(f"    （對比 production 現用 {PROD_RMSE}，walk-forward 滾動 ~16）")
    print("-" * 60)
    print(f"  {'sigma':>8}{'logloss':>10}{'brier':>9}")
    for label, s in [(f"prod={PROD_RMSE}", PROD_RMSE), ("rolling=16", 16.0), (f"fit={sigma}", sigma)]:
        print(f"  {label:>8}{log_loss(ml_pairs, s):>10.4f}{brier(ml_pairs, s):>9.4f}")
    print("-" * 60)
    print("  校準後 reliability（信心方視角）:")
    print(f"  {'區間':<10}{'場數':>6}{'預測%':>8}{'實際%':>8}{'落差':>7}")
    for b in reliability(ml_pairs, sigma):
        print(f"  {b['bucket']:<10}{b['n']:>6}{b['pred']:>8}{b['actual']:>8}{b['gap']:>+7}")

    # spread 驗證：同一 sigma 是否也校準讓分 cover
    sp_pairs = collect_spread_pairs(records)
    spread_block = {}
    if sp_pairs:
        sp_fit = fit_sigma(sp_pairs)
        print("-" * 60)
        print(f"  讓分 cover 驗證（{len(sp_pairs)} 場有收盤線）:")
        print(f"    用 sigma_margin={sigma}: brier={brier(sp_pairs, sigma):.4f}")
        print(f"    讓分獨立擬合 sigma={sp_fit}: brier={brier(sp_pairs, sp_fit):.4f}")
        print(f"    （兩者接近 → 單一 sigma 同時校準勝率與讓分）")
        spread_block = {"n": len(sp_pairs), "sigma_independent_fit": sp_fit,
                        "brier_with_margin_sigma": round(brier(sp_pairs, sigma), 4)}
    print("=" * 60 + "\n")

    payload = {
        "sigma_margin": sigma,
        "n_samples": len(ml_pairs),
        "logloss_before": round(log_loss(ml_pairs, PROD_RMSE), 4),
        "logloss_after": round(log_loss(ml_pairs, sigma), 4),
        "brier_before": round(brier(ml_pairs, PROD_RMSE), 4),
        "brier_after": round(brier(ml_pairs, sigma), 4),
        "prod_rmse_before": PROD_RMSE,
        "spread_validation": spread_block,
        "reliability_after": reliability(ml_pairs, sigma),
        "fitted_at": datetime.now().isoformat(timespec="seconds"),
        "source": "walk_forward_backtest OOS",
    }

    if args.dry_run:
        print("(dry-run，未寫入 artifact)")
        return
    CALIB_PATH.parent.mkdir(parents=True, exist_ok=True)
    CALIB_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已寫入 {CALIB_PATH}")


if __name__ == "__main__":
    main()

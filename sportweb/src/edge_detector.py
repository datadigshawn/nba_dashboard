"""
邊際偵測器 — 比對我方 NBA 預測 vs 運彩市場賠率，找正期望值機會

資料源：
  - autobots_NBA/nba_data.json（我方 XGBoost + Elo 勝率預測）
  - sportWeb/data/latest_odds.json（運彩 moneyline 賠率）

輸出：
  - 印出 edge 清單
  - --push：邊際 > 閾值時推播 Telegram
  - --json：JSON 輸出（給 dashboard / 其他程式用）

執行：
  cd /Users/shawnclaw/autobot/sportWeb
  .venv/bin/python src/edge_detector.py                    # 一次性報告
  .venv/bin/python src/edge_detector.py --min-edge 0.05    # 閾值 5%
  .venv/bin/python src/edge_detector.py --push             # 推 Telegram
  .venv/bin/python src/edge_detector.py --json             # JSON 輸出
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "src"))
from schema import build_game_key, parse_game_date_ymd, to_espn_name  # noqa: E402

AUTOBOT_ROOT = Path("/Users/shawnclaw/autobot")
NBA_DATA = AUTOBOT_ROOT / "autobots_NBA" / "nba_data.json"
ODDS_DATA = BASE_DIR / "data" / "latest_odds.json"

# ── Telegram 設定載入 ───────────────────────────────────
def _parse_env(path: Path) -> dict:
    """從 .env 檔讀出 key=value 字典。"""
    out: dict = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _load_tg_config() -> dict:
    """讀 Telegram 設定。

    優先順序:
      1. /autobots_NBA/.env 的 NBA_TG_TOKEN + NBA_TG_CHAT_ID
         (專屬 NBA bot @NBA_predict55_bot;本腳本送的是 NBA 邊際機會，應歸 NBA bot)
      2. sportWeb/.env 的 TELEGRAM_TOKEN + TELEGRAM_CHAT_ID
         (歷史 fallback，指向 pi2Trader bot)
      3. 其他歷史 .env 位置 (whalexxx / social_trackers / hermes)
    """
    # Priority 1: NBA-dedicated bot
    nba_env = _parse_env(AUTOBOT_ROOT / "autobots_NBA" / ".env")
    if nba_env.get("NBA_TG_TOKEN") and nba_env.get("NBA_TG_CHAT_ID"):
        return {
            "token": nba_env["NBA_TG_TOKEN"],
            "chat_id": nba_env["NBA_TG_CHAT_ID"],
        }

    # Priority 2+: historical fallback chain
    for candidate in [
        BASE_DIR / ".env",
        AUTOBOT_ROOT / "whalexxx" / ".env",
        AUTOBOT_ROOT / "social_trackers" / "common" / ".env",
        Path.home() / ".hermes" / ".env",
    ]:
        out = _parse_env(candidate)
        if out.get("TELEGRAM_TOKEN") or out.get("TELEGRAM_BOT_TOKEN"):
            return {
                "token": out.get("TELEGRAM_TOKEN") or out.get("TELEGRAM_BOT_TOKEN", ""),
                "chat_id": out.get("TELEGRAM_CHAT_ID") or out.get("TELEGRAM_HOME_CHANNEL", ""),
            }
    return {"token": "", "chat_id": ""}


TG = _load_tg_config()


@dataclass
class Edge:
    """一場比賽一個邊際機會。"""
    game_id: str
    game_date: str
    away: str
    home: str
    side: str            # "home" / "away" / "over" / "under"
    picked_team: str     # 被推薦的那一方（或 "Over"/"Under"）
    model_prob: float    # 模型推算的覆蓋機率（0-1）
    market_prob: float   # 市場隱含機率（去 overround 後）
    odds: float          # 該方的十進制賠率
    edge: float          # model_prob - market_prob
    kelly: float
    expected_roi: float
    edge_type: str = "moneyline"   # "moneyline" / "spread" / "total"
    line: float = 0.0              # 讓分線或大小分線
    match_quality: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ── 載入資料 ────────────────────────────────────────────
def load_nba_predictions() -> list:
    """讀 autobots_NBA 今日 + 未來預測。"""
    if not NBA_DATA.exists():
        return []
    try:
        payload = json.loads(NBA_DATA.read_text(encoding="utf-8"))
        return (payload.get("games") or []) + (payload.get("next_games") or [])
    except Exception as e:
        print(f"⚠️ 讀 nba_data.json 失敗：{e}", file=sys.stderr)
        return []


def load_odds() -> list:
    """讀 sportWeb 抓到的運彩賠率。"""
    if not ODDS_DATA.exists():
        return []
    try:
        return json.loads(ODDS_DATA.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"⚠️ 讀 latest_odds.json 失敗：{e}", file=sys.stderr)
        return {}


def load_odds_games() -> list:
    payload = load_odds()
    return payload.get("games", []) if payload else []


# ── 計算核心 ────────────────────────────────────────────
def implied_prob_with_vig(ml_away: float, ml_home: float) -> tuple:
    """從 moneyline 算去除莊家抽水的真實機率。

    原理：odds 倒數 = 含 vig 機率；正規化後 ≈ 真實市場機率
    回傳 (prob_away, prob_home, vig_pct)
    """
    if not (ml_away and ml_home) or ml_away <= 0 or ml_home <= 0:
        return None, None, None
    raw_a = 1 / ml_away
    raw_h = 1 / ml_home
    total = raw_a + raw_h
    vig = total - 1.0
    return raw_a / total, raw_h / total, vig


def kelly_fraction(p: float, odds: float) -> float:
    """Kelly Criterion：最佳資金比例

    f = (p*(b+1) - 1) / b, where b = odds - 1（淨賠率）
    負數代表不值得投注
    """
    b = odds - 1
    if b <= 0:
        return 0.0
    f = (p * (b + 1) - 1) / b
    return max(0.0, f)


def expected_roi(p: float, odds: float) -> float:
    """每 $1 投注的預期報酬率。

    EV = p * (odds - 1) - (1 - p) * 1
    """
    return p * (odds - 1) - (1 - p)


def actionable_edge(model_prob: float, market_prob: float, odds: float,
                    min_edge: float) -> tuple[float, float, float] | None:
    """Return actionable edge metrics or None when the bet is not +EV."""
    edge = model_prob - market_prob
    if edge < min_edge:
        return None
    roi = expected_roi(model_prob, odds)
    if roi <= 0:
        return None
    return edge, kelly_fraction(model_prob, odds), roi


SPREAD_SIGMA = 12.0   # NBA per-game spread std dev (points)
TOTAL_SIGMA = 18.0    # NBA per-game total std dev (points)


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via math.erf (exact)."""
    from math import erf, sqrt
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def spread_cover_probs(pred_spread: float, home_line: float, sigma: float = SPREAD_SIGMA) -> tuple[float, float]:
    """Return (P(home covers), P(away covers)) using signed home line.

    pred_spread = E[home_score - away_score]
    home_line:
      +7.5 => home +7.5
      -7.5 => home -7.5
    """
    p_home = _norm_cdf((pred_spread + home_line) / sigma)
    return p_home, 1.0 - p_home


def total_over_prob(pred_total: float, line: float, sigma: float = TOTAL_SIGMA) -> float:
    """P(total score > line | model predicts total = pred_total)."""
    return _norm_cdf((pred_total - line) / sigma)


def _swap_injuries(inj: dict | None) -> dict | None:
    if not inj:
        return inj
    return {
        "home_out": inj.get("away_out", []),
        "home_gtd": inj.get("away_gtd", []),
        "away_out": inj.get("home_out", []),
        "away_gtd": inj.get("home_gtd", []),
    }


def _negate(value):
    return None if value is None else -float(value)


def _normalize_prediction_game(g: dict) -> dict:
    home = to_espn_name(g.get("home", ""))
    away = to_espn_name(g.get("away", ""))
    game_date = parse_game_date_ymd(g.get("game_date", ""))
    out = dict(g)
    out["home"] = home
    out["away"] = away
    out["game_date"] = game_date
    out["game_key"] = build_game_key(game_date, away, home)
    return out


def _align_prediction_for_odds(pg: dict, odds_home: str, odds_away: str) -> tuple[dict, bool]:
    pred_home = to_espn_name(pg.get("home", ""))
    pred_away = to_espn_name(pg.get("away", ""))
    if pred_home == odds_home and pred_away == odds_away:
        aligned = dict(pg)
        aligned["home"] = odds_home
        aligned["away"] = odds_away
        aligned["game_key"] = build_game_key(aligned.get("game_date", ""), odds_away, odds_home)
        return aligned, False

    if pred_home == odds_away and pred_away == odds_home:
        aligned = dict(pg)
        aligned["home"] = odds_home
        aligned["away"] = odds_away
        aligned["home_prob"] = pg.get("away_prob")
        aligned["away_prob"] = pg.get("home_prob")
        aligned["home_elo"] = pg.get("away_elo")
        aligned["away_elo"] = pg.get("home_elo")
        aligned["model_margin"] = _negate(pg.get("model_margin"))
        aligned["pred_spread"] = _negate(pg.get("pred_spread"))
        aligned["spread_model_raw"] = _negate(pg.get("spread_model_raw"))
        aligned["home_expected"] = pg.get("away_expected")
        aligned["away_expected"] = pg.get("home_expected")
        aligned["b2b_home"] = pg.get("b2b_away")
        aligned["b2b_away"] = pg.get("b2b_home")
        aligned["rest_home"] = pg.get("rest_away")
        aligned["rest_away"] = pg.get("rest_home")
        aligned["injuries"] = _swap_injuries(pg.get("injuries"))
        aligned["game_key"] = build_game_key(aligned.get("game_date", ""), odds_away, odds_home)
        return aligned, True

    raise ValueError("prediction teams do not match odds teams")


def _date_gap_days(game_date_a: str, game_date_b: str) -> int | None:
    a = parse_game_date_ymd(game_date_a)
    b = parse_game_date_ymd(game_date_b)
    if not (a and b):
        return None
    da = datetime.strptime(a, "%Y%m%d")
    db = datetime.strptime(b, "%Y%m%d")
    return abs((da - db).days)


def _match_predictions_odds(predictions: list, odds: list):
    """Build aligned pairs of (prediction_game, odds_game, quality)."""
    pred_list = [_normalize_prediction_game(g) for g in predictions]
    by_matchup: dict[frozenset[str], list[dict]] = {}
    for g in pred_list:
        by_matchup.setdefault(frozenset((g.get("home", ""), g.get("away", ""))), []).append(g)

    pairs = []
    for og in odds:
        o_home = to_espn_name(og.get("home", ""))
        o_away = to_espn_name(og.get("away", ""))
        odds_date = parse_game_date_ymd(og.get("game_date") or og.get("start_time", ""))
        candidates = by_matchup.get(frozenset((o_home, o_away)), [])
        ranked = []
        for pg in candidates:
            try:
                aligned, reversed_match = _align_prediction_for_odds(pg, o_home, o_away)
            except ValueError:
                continue
            gap = _date_gap_days(aligned.get("game_date", ""), odds_date)
            exact_date = 1 if odds_date and aligned.get("game_date") == odds_date else 0
            ranked.append((
                0 if exact_date else 1,
                gap if gap is not None else 999,
                1 if reversed_match else 0,
                aligned,
            ))

        if not ranked:
            continue

        ranked.sort(key=lambda item: (item[0], item[1], item[2]))
        exact_penalty, gap, reversed_penalty, aligned = ranked[0]
        if odds_date and gap > 3:
            continue

        og_aligned = dict(og)
        og_aligned["home"] = o_home
        og_aligned["away"] = o_away
        og_aligned["game_date"] = odds_date
        og_aligned["game_key"] = build_game_key(aligned.get("game_date", "") or odds_date, o_away, o_home)

        quality = "exact-date" if exact_penalty == 0 else "nearest-date"
        if reversed_penalty:
            quality += "-reversed"
        pairs.append((aligned, og_aligned, quality))
    return pairs


def detect_spread_edges(min_edge: float = 0.05) -> list[Edge]:
    """讓分 edge 偵測：比對 pred_spread vs 市場讓分線。"""
    predictions = load_nba_predictions()
    odds_list = load_odds_games()
    if not predictions or not odds_list:
        return []

    edges = []
    for pg, og, match_quality in _match_predictions_odds(predictions, odds_list):
        pred_spread = pg.get("pred_spread")
        if pred_spread is None:
            continue
        spreads = og.get("spreads") or []
        if not spreads:
            continue

        game_id = str(og.get("game_id", ""))
        game_date = pg.get("game_date") or og.get("game_date", "")
        o_home = og.get("home", "")
        o_away = og.get("away", "")

        for sp in spreads:
            home_line = sp.get("line")
            away_odds = sp.get("away")
            home_odds = sp.get("home")
            if home_line is None or not (away_odds and home_odds):
                continue

            p_home_covers, p_away_covers = spread_cover_probs(float(pred_spread), float(home_line))

            mkt_away, mkt_home, _ = implied_prob_with_vig(away_odds, home_odds)
            if mkt_away is None:
                continue

            away_metrics = actionable_edge(p_away_covers, mkt_away, away_odds, min_edge)
            if away_metrics:
                edge_away, away_kelly, away_roi = away_metrics
                edges.append(Edge(
                    game_id=game_id, game_date=game_date, away=o_away, home=o_home,
                    side="away", picked_team=f"{o_away} {(-float(home_line)):+g}",
                    model_prob=p_away_covers, market_prob=mkt_away,
                    odds=away_odds, edge=edge_away,
                    kelly=away_kelly, expected_roi=away_roi,
                    edge_type="spread", line=float(home_line), match_quality=match_quality,
                ))

            home_metrics = actionable_edge(p_home_covers, mkt_home, home_odds, min_edge)
            if home_metrics:
                edge_home, home_kelly, home_roi = home_metrics
                edges.append(Edge(
                    game_id=game_id, game_date=game_date, away=o_away, home=o_home,
                    side="home", picked_team=f"{o_home} {float(home_line):+g}",
                    model_prob=p_home_covers, market_prob=mkt_home,
                    odds=home_odds, edge=edge_home,
                    kelly=home_kelly, expected_roi=home_roi,
                    edge_type="spread", line=float(home_line), match_quality=match_quality,
                ))

    edges.sort(key=lambda e: e.edge, reverse=True)
    return edges


def detect_total_edges(min_edge: float = 0.05) -> list[Edge]:
    """大小分 edge 偵測：比對 pred_total vs 市場大小線。"""
    predictions = load_nba_predictions()
    odds_list = load_odds_games()
    if not predictions or not odds_list:
        return []

    edges = []
    for pg, og, match_quality in _match_predictions_odds(predictions, odds_list):
        pred_total = pg.get("pred_total")
        if pred_total is None:
            continue
        totals = og.get("totals") or []
        if not totals:
            continue

        game_id = str(og.get("game_id", ""))
        game_date = pg.get("game_date") or og.get("game_date", "")
        o_home = og.get("home", "")
        o_away = og.get("away", "")

        for t in totals:
            line = t.get("line", 0)
            over_odds = t.get("over")
            under_odds = t.get("under")
            if not (over_odds and under_odds and line > 0):
                continue

            p_over = total_over_prob(pred_total, line)
            p_under = 1.0 - p_over

            mkt_over, mkt_under, _ = implied_prob_with_vig(over_odds, under_odds)
            if mkt_over is None:
                continue

            over_metrics = actionable_edge(p_over, mkt_over, over_odds, min_edge)
            if over_metrics:
                edge_over, over_kelly, over_roi = over_metrics
                edges.append(Edge(
                    game_id=game_id, game_date=game_date, away=o_away, home=o_home,
                    side="over", picked_team=f"Over {line}",
                    model_prob=p_over, market_prob=mkt_over,
                    odds=over_odds, edge=edge_over,
                    kelly=over_kelly, expected_roi=over_roi,
                    edge_type="total", line=line, match_quality=match_quality,
                ))

            under_metrics = actionable_edge(p_under, mkt_under, under_odds, min_edge)
            if under_metrics:
                edge_under, under_kelly, under_roi = under_metrics
                edges.append(Edge(
                    game_id=game_id, game_date=game_date, away=o_away, home=o_home,
                    side="under", picked_team=f"Under {line}",
                    model_prob=p_under, market_prob=mkt_under,
                    odds=under_odds, edge=edge_under,
                    kelly=under_kelly, expected_roi=under_roi,
                    edge_type="total", line=line, match_quality=match_quality,
                ))

    edges.sort(key=lambda e: e.edge, reverse=True)
    return edges


def detect_edges(min_edge: float = 0.05) -> list:
    """主偵測邏輯。

    對每場有匹配的比賽，檢查兩側（home/away），有邊際就記錄。
    """
    predictions = load_nba_predictions()
    odds = load_odds_games()

    if not predictions:
        print("⚠️ autobots_NBA 無今日預測", file=sys.stderr)
        return []
    if not odds:
        print("ℹ️ sportWeb 尚無賠率資料（sportWeb fetcher 未執行或網站未開盤）",
              file=sys.stderr)
        return []

    edges = []
    for pg, og, match_quality in _match_predictions_odds(predictions, odds):
        o_home_en = og.get("home", "")
        o_away_en = og.get("away", "")
        game_date = pg.get("game_date") or og.get("game_date", "")
        ml = og.get("moneyline") or {}
        ml_away = ml.get("away")
        ml_home = ml.get("home")
        if not (ml_away and ml_home):
            continue

        # 市場隱含機率（去 vig）
        market_away, market_home, vig = implied_prob_with_vig(ml_away, ml_home)
        if market_away is None:
            continue

        # 我方模型勝率（以 ESPN 視角 home/away 對應）
        model_home = (pg.get("home_prob", 0) or 0) / 100.0
        model_away = (pg.get("away_prob", 0) or 0) / 100.0
        if model_home + model_away < 0.9:  # sanity check
            continue

        # 檢查兩側
        for side, picked, mp, marketp, odd_val in [
            ("home", o_home_en, model_home, market_home, ml_home),
            ("away", o_away_en, model_away, market_away, ml_away),
        ]:
            metrics = actionable_edge(mp, marketp, odd_val, min_edge)
            if not metrics:
                continue
            edge, k, roi = metrics
            edges.append(Edge(
                game_id=str(og.get("game_id", "")),
                game_date=game_date,
                away=o_away_en, home=o_home_en,
                side=side, picked_team=picked,
                model_prob=mp, market_prob=marketp,
                odds=odd_val, edge=edge,
                kelly=k, expected_roi=roi,
                match_quality=match_quality,
            ))

    # 依邊際大小排序
    edges.sort(key=lambda e: e.edge, reverse=True)
    return edges


# ── Telegram 推播 ──────────────────────────────────────
def push_telegram(edges: list) -> bool:
    if not TG["token"] or not TG["chat_id"]:
        print("⚠️ Telegram token/chat_id 未設定", file=sys.stderr)
        return False
    if not edges:
        return True  # 沒 edge 不推

    lines = [
        f"🎯 <b>NBA 邊際機會 ({len(edges)})</b>",
        f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')} 台北",
        "",
    ]
    for i, e in enumerate(edges[:10], 1):  # 最多 10 筆
        lines.append(
            f"<b>{i}. {e.picked_team}</b> ({e.side.upper()})\n"
            f"  模型勝率 <b>{e.model_prob*100:.1f}%</b> "
            f"vs 市場 {e.market_prob*100:.1f}%\n"
            f"  Edge <b>+{e.edge*100:.1f}%</b> · "
            f"賠率 {e.odds:.2f} · Kelly <b>{e.kelly*100:.1f}%</b> · "
            f"ROI {e.expected_roi*100:+.1f}%"
        )

    msg = "\n".join(lines)[:3900]
    data = urllib.parse.urlencode({
        "chat_id": TG["chat_id"],
        "text": msg,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TG['token']}/sendMessage", data=data
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            res = json.loads(r.read())
        return res.get("ok", False)
    except Exception as e:
        print(f"❌ Telegram 推送失敗：{e}", file=sys.stderr)
        return False


# ── CLI ─────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-edge", type=float, default=0.05,
                    help="edge threshold (default 0.05)")
    ap.add_argument("--push", action="store_true",
                    help="push results to Telegram")
    ap.add_argument("--json", action="store_true",
                    help="emit JSON output")
    args = ap.parse_args()

    ml_edges = detect_edges(min_edge=args.min_edge)
    sp_edges = detect_spread_edges(min_edge=args.min_edge)
    tt_edges = detect_total_edges(min_edge=args.min_edge)
    edges = ml_edges + sp_edges + tt_edges
    edges.sort(key=lambda e: e.edge, reverse=True)

    if args.json:
        out = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "min_edge": args.min_edge,
            "count": len(edges),
            "by_type": {"moneyline": len(ml_edges), "spread": len(sp_edges), "total": len(tt_edges)},
            "edges": [e.to_dict() for e in edges],
        }
        try:
            from sport_db import edge_backtest, DB_PATH as _db
            out["backtest"] = edge_backtest(_db)
        except Exception:
            pass
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    # 文字輸出
    type_labels = {"moneyline": "🎰 獨贏", "spread": "📐 讓分", "total": "📊 大小"}
    print(f"{'=' * 70}")
    print(f"  NBA 邊際偵測（閾值 {args.min_edge * 100:.1f}%）")
    print(f"  獨贏 {len(ml_edges)} · 讓分 {len(sp_edges)} · 大小 {len(tt_edges)}")
    print(f"{'=' * 70}")
    if not edges:
        print("  目前無邊際機會（或資料源缺失）")
        return

    for i, e in enumerate(edges, 1):
        lbl = type_labels.get(e.edge_type, e.edge_type)
        line_info = f"  線: {e.line}" if e.line else ""
        print(f"\n  [{i}] {lbl} {e.away} @ {e.home}{line_info}")
        print(f"      推薦：{e.picked_team} ({e.side.upper()})")
        print(f"      模型 {e.model_prob*100:5.1f}% vs 市場 {e.market_prob*100:5.1f}% = "
              f"Edge +{e.edge*100:5.2f}%")
        print(f"      賠率 {e.odds:.2f}  ·  Kelly {e.kelly*100:5.2f}%  ·  "
              f"每 $1 期望 ROI {e.expected_roi*100:+.2f}%")

    if args.push:
        ok = push_telegram(edges)
        print("\n✅ 已推播到 Telegram" if ok else "\n❌ Telegram 推播失敗")

    # Resolve past edge outcomes (before writing new ones)
    try:
        from sport_resolve import resolve_edges
        resolve_edges()
    except Exception as _re:
        print(f"[warn] resolve failed: {_re}", file=sys.stderr)

    # Write edges to SQLite
    try:
        from sport_db import (
            init_db as _db_init,
            insert_edges,
            get_snapshot_id_by_fetched_at,
            DB_PATH as _db,
        )
        from datetime import datetime as _dt
        _db_init(_db)
        odds_payload = load_odds()
        snapshot_id = get_snapshot_id_by_fetched_at(_db, odds_payload.get("fetched_at", ""))
        edge_dicts = [{
            "game_id": e.game_id, "game_date": e.game_date, "away": e.away, "home": e.home,
            "side": e.side, "picked_team": e.picked_team,
            "model_prob": e.model_prob, "market_prob": e.market_prob,
            "odds": e.odds, "edge": e.edge, "kelly": e.kelly,
            "expected_roi": e.expected_roi,
            "edge_type": e.edge_type, "line": e.line,
        } for e in edges]
        insert_edges(
            _db,
            edge_dicts,
            _dt.now().isoformat(timespec="seconds"),
            args.min_edge,
            snapshot_id=snapshot_id,
        )
        snapshot_label = f" snapshot #{snapshot_id}" if snapshot_id else ""
        print(f"\n📊 {len(edges)} edges 寫入 DB{snapshot_label}")
    except Exception as _e:
        print(f"\n[warn] DB write failed: {_e}", file=sys.stderr)


if __name__ == "__main__":
    main()

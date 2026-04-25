"""
Telegram push helper for @NBA_predict55_bot.

- Loads token + chat_id from .env (in this project dir).
- Formats today's nba_data.json into a digest message.
- Formats today's sportsbook-aligned betting alert from pick_stats.json / sportbook_report.json.
- Splits into ≤4096-char chunks (Telegram limit).
- Uses urllib (no extra deps).

Usage:
  # send today's digest
  python telegram_push.py --digest

  # send today's betting alert
  python telegram_push.py --betting-alert

  # send a plain message
  python telegram_push.py --msg "Hello"

  # dry-run (print formatted message without sending)
  python telegram_push.py --betting-alert --dry-run
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
DATA_PATH = BASE_DIR / "nba_data.json"
PICK_STATS_PATH = BASE_DIR / "pick_stats.json"
SPORTBOOK_REPORT_PATH = BASE_DIR / "sportbook_report.json"
TG_LIMIT = 4096  # Telegram message char limit


# ── env loader ────────────────────────────────────────────────────────

def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if not ENV_PATH.exists():
        return env
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def _get_credentials() -> tuple[str, str]:
    env = _load_env()
    token = env.get("NBA_TG_TOKEN", "").strip()
    chat_id = env.get("NBA_TG_CHAT_ID", "").strip()
    if not token:
        print("[telegram_push] ✗ NBA_TG_TOKEN 未在 .env 設定", file=sys.stderr)
        sys.exit(2)
    return token, chat_id


# ── Telegram API helpers (urllib, no deps) ────────────────────────────

def send_message(text: str, chat_id: str | None = None,
                 parse_mode: str = "HTML") -> bool:
    """Post one message to chat_id. Returns True on success."""
    token, default_cid = _get_credentials()
    cid = chat_id or default_cid
    if not cid:
        print("[telegram_push] ✗ chat_id 缺 — 先跑 setup_chat_id.py", file=sys.stderr)
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": cid,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": "true",
    }
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read().decode())
            if not body.get("ok"):
                print(f"[telegram_push] ✗ API error: {body}", file=sys.stderr)
                return False
            return True
    except Exception as e:
        print(f"[telegram_push] ✗ send failed: {e}", file=sys.stderr)
        return False


def send_chunked(text: str, chat_id: str | None = None) -> bool:
    """Split text across 4096-char chunks and send sequentially."""
    if len(text) <= TG_LIMIT:
        return send_message(text, chat_id)
    chunks, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > TG_LIMIT:
            chunks.append(buf.rstrip())
            buf = ""
        buf += line + "\n"
    if buf.strip():
        chunks.append(buf.rstrip())
    ok_all = True
    for i, ch in enumerate(chunks, 1):
        suffix = f"\n\n<i>— 續 {i}/{len(chunks)} —</i>" if len(chunks) > 1 else ""
        ok_all &= send_message(ch + suffix, chat_id)
    return ok_all


# ── Digest formatter ──────────────────────────────────────────────────

def _fmt_game(g: dict) -> str:
    home = g.get("home", "?")
    away = g.get("away", "?")
    hp = g.get("home_prob", 0)
    ap = g.get("away_prob", 0)
    winner = home if hp >= ap else away
    win_prob = max(hp, ap)
    spread = g.get("pred_spread", 0)
    # Mark the predicted winner with emoji
    conf = "🔥" if win_prob >= 75 else ("✅" if win_prob >= 60 else "⚖️")
    b2b_note = ""
    if g.get("b2b_home"):
        b2b_note += " · 主場 B2B"
    if g.get("b2b_away"):
        b2b_note += " · 客場 B2B"
    line = (
        f"{conf} <b>{away}</b> @ <b>{home}</b>\n"
        f"   預測 <b>{winner}</b> 贏 ({win_prob:.0f}%) · "
        f"Spread {spread:+.1f} · O/U {g.get('pred_total', 0):.1f}"
        f"{b2b_note}"
    )
    return line


def _fmt_edge(e: dict) -> str:
    q = e.get("question", "?")
    mp = e.get("model_prob", 0)
    pp = e.get("poly_prob", 0)
    edge = e.get("edge", 0)
    kelly = e.get("kelly_pct", 0)
    bet = e.get("bet", "-")
    winner = e.get("pred_winner", "?")
    vol = e.get("volume", 0) / 1e6
    edge_sign = "🟢" if edge > 0 else ("🔴" if edge < 0 else "⚪")
    return (
        f"{edge_sign} <b>{q}</b>\n"
        f"   模型 {mp:.1f}% vs 市場 {pp:.1f}% → edge {edge:+.1f}%\n"
        f"   建議 <b>{bet}</b> · Kelly {kelly:.1f}% · Vol ${vol:.1f}M · 看好 {winner}"
    )


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _fmt_ymd(ymd: str) -> str:
    if not ymd or len(str(ymd)) != 8:
        return str(ymd or "-")
    ymd = str(ymd)
    return f"{ymd[4:6]}/{ymd[6:8]}"


def _fmt_pick(pick: dict, rank: int) -> str:
    game = html.escape(pick.get("game_key", "?"))
    detail = html.escape(pick.get("pick_detail", "?"))
    edge = float(pick.get("edge") or 0)
    confidence = float(pick.get("confidence") or 0)
    game_date = _fmt_ymd(str(pick.get("game_date") or ""))
    tw_spread = pick.get("tw_spread")
    tw_ou = pick.get("tw_ou")
    odds_bits = []
    if tw_spread not in ("", None):
        odds_bits.append(f"讓分 {tw_spread:g}" if isinstance(tw_spread, float) else f"讓分 {tw_spread}")
    if tw_ou not in ("", None):
        odds_bits.append(f"O/U {tw_ou:g}" if isinstance(tw_ou, float) else f"O/U {tw_ou}")
    odds_text = " · ".join(odds_bits) if odds_bits else "盤口 N/A"
    return (
        f"{rank}. <b>{detail}</b>\n"
        f"   {game} · {game_date}\n"
        f"   Edge +{edge:.1f} 分 · 模型信心 {confidence:.1f}% · {html.escape(odds_text)}"
    )


def _fmt_sportbook_edge(edge_row: dict, rank: int) -> str:
    game = html.escape(f"{edge_row.get('away', '?')} @ {edge_row.get('home', '?')}")
    picked = html.escape(edge_row.get("picked_team", "?"))
    game_date = _fmt_ymd(str(edge_row.get("game_date") or ""))
    edge_pct = float(edge_row.get("edge") or 0) * 100
    roi_pct = float(edge_row.get("expected_roi") or 0) * 100
    model_pct = float(edge_row.get("model_prob") or 0) * 100
    market_pct = float(edge_row.get("market_prob") or 0) * 100
    odds = edge_row.get("odds")
    edge_type = html.escape(str(edge_row.get("edge_type", "")).upper())
    odds_text = f"{float(odds):.2f}" if odds not in (None, "") else "-"
    return (
        f"{rank}. <b>{picked}</b> <i>({edge_type})</i>\n"
        f"   {game} · {game_date}\n"
        f"   Edge +{edge_pct:.1f}% · ROI +{roi_pct:.1f}% · 模型 {model_pct:.1f}% vs 市場 {market_pct:.1f}% · 賠率 {odds_text}"
    )


def format_digest(data: dict | None = None) -> str:
    """Produce the daily digest from nba_data.json."""
    if data is None:
        if not DATA_PATH.exists():
            return f"⚠️ nba_data.json 不存在 ({DATA_PATH})"
        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))

    games = data.get("games") or []
    edges = data.get("edges") or []
    bt = data.get("backtest") or {}

    now = datetime.now().strftime("%Y-%m-%d")
    lines: list[str] = [
        f"🏀 <b>NBA 每日預測</b> · {now}",
        f"<i>@NBA_predict55_bot</i>",
        "",
    ]

    # Backtest performance (if present)
    if bt:
        acc = bt.get("accuracy") or bt.get("acc")
        roi = bt.get("roi")
        n = bt.get("n") or bt.get("games")
        if acc is not None or roi is not None:
            bt_bits = []
            if acc is not None:
                bt_bits.append(f"accuracy {acc*100 if acc <= 1 else acc:.1f}%")
            if roi is not None:
                bt_bits.append(f"ROI {roi:+.1f}%")
            if n:
                bt_bits.append(f"n={n}")
            lines.append(f"📈 <b>Backtest</b>: " + " · ".join(bt_bits))
            lines.append("")

    # Today's games
    if games:
        lines.append(f"📊 <b>今日 {len(games)} 場</b>")
        lines.append("")
        for g in games:
            lines.append(_fmt_game(g))
            lines.append("")
    else:
        lines.append("📊 今日無排定比賽")
        lines.append("")

    # Edges (Polymarket arbitrage)
    if edges:
        lines.append("─────────────")
        lines.append(f"💎 <b>Polymarket Edge 機會 ({len(edges)})</b>")
        lines.append("")
        for e in edges:
            lines.append(_fmt_edge(e))
            lines.append("")

    lines.append("<i>· 09:00 重新訓練 XGBoost + ELO 後自動推送</i>")
    return "\n".join(lines)


def format_betting_alert(topn: int = 5) -> str:
    pick_payload = _load_json(PICK_STATS_PATH)
    report_payload = _load_json(SPORTBOOK_REPORT_PATH)
    current_picks = list(pick_payload.get("current_picks") or [])
    pick_stats = pick_payload.get("stats") or {}
    current_edges = list((report_payload.get("current") or {}).get("top_edges") or [])
    edge_count = (report_payload.get("current") or {}).get("count", 0)
    source = report_payload.get("source") or {}
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines: list[str] = [
        f"🎯 <b>NBA 今日推薦賭局</b> · {now}",
        f"<i>@NBA_predict55_bot</i>",
        "",
    ]

    if pick_stats.get("total", 0):
        lines.append(
            f"📈 <b>歷史推薦</b>: 勝率 {pick_stats.get('wr', 0):.1f}% "
            f"({pick_stats.get('wins', 0)}/{pick_stats.get('total', 0)})"
        )
    else:
        lines.append("📈 <b>歷史推薦</b>: 尚無已結算樣本，先累積中")

    if edge_count:
        lines.append(f"🧭 <b>本輪市場掃描</b>: {edge_count} 個候選 edge")
    fetched_at = source.get("sportweb_fetched_at")
    if fetched_at:
        lines.append(f"🕒 <b>盤口時間</b>: {html.escape(str(fetched_at).replace('T', ' '))}")
    lines.append("")

    if current_picks:
        picks = sorted(current_picks, key=lambda row: float(row.get("edge") or 0), reverse=True)[:topn]
        lines.append(f"✅ <b>正式推薦 Top {len(picks)}</b>")
        lines.append("")
        for idx, pick in enumerate(picks, 1):
            lines.append(_fmt_pick(pick, idx))
            lines.append("")
    elif current_edges:
        edges = sorted(
            current_edges,
            key=lambda row: (float(row.get("expected_roi") or 0), float(row.get("edge") or 0)),
            reverse=True,
        )[:topn]
        lines.append(f"📐 <b>市場候補 Top {len(edges)}</b> <i>(今日正式 picks 為 0，改發 edge 候補)</i>")
        lines.append("")
        for idx, row in enumerate(edges, 1):
            lines.append(_fmt_sportbook_edge(row, idx))
            lines.append("")
    else:
        lines.append("⚪️ 今日沒有可用的推薦賭局或 edge。")
        lines.append("")

    lines.append("<i>· 每日 pipeline 完成後自動推送</i>")
    return "\n".join(lines)


# ── CLI entry ─────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--digest", action="store_true", help="format + send daily digest")
    g.add_argument("--betting-alert", action="store_true",
                   help="format + send sportsbook-aligned betting alert")
    g.add_argument("--msg", type=str, help="send a custom plain message")
    ap.add_argument("--dry-run", action="store_true",
                    help="print message without sending")
    ap.add_argument("--topn", type=int, default=5,
                    help="top N picks/edges to include for --betting-alert (default: 5)")
    args = ap.parse_args()

    if args.digest:
        text = format_digest()
    elif args.betting_alert:
        text = format_betting_alert(topn=max(1, args.topn))
    else:
        text = args.msg or ""

    if args.dry_run:
        print(text)
        print(f"--- length: {len(text)} chars ---")
        return

    ok = send_chunked(text)
    print(f"[telegram_push] {'✓ sent' if ok else '✗ failed'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

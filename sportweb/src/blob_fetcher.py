"""
Blob Fetcher — 抓台灣運彩新版 CDN API 的 NBA 賠率（2026-06-08 網站改版後）

改版說明：
    舊版 POST /services/content/get（fetcher.py，需 Playwright 過 CF）已於
    2026-06-08 失效，未知路徑一律 fallback 回 SPA index.html。
    新版賠率改放在 CDN 靜態 JSON，純 HTTP GET 即可，無 Cloudflare 挑戰：

    GET https://blob3rd.sportslottery.com.tw/apidata/Pre/{sportId}-Games.{lang}.json
        sportId 34765.1 = 籃球（含 NBA / WNBA / 歐洲聯賽）
        NBA 過濾：game["ti"] == "25793"（tn = "NBA - USA"）

資料格式：
    game: id, an(客隊), hn(主隊), kt(開賽時間 ISO+08:00), ti(聯盟id), tn(聯盟名), ms(市場列表)
    market(ms[]): name, mv(盤口線), cs(選項列表)
        "Winner (Incl. Overtime)"       → 不讓分（moneyline）
        "Handicap {mv} (Incl. Overtime)" → 讓分（mv 為主隊讓分線）
        "Total {mv} (Incl. Overtime)"    → 大小分
    selection(cs[]): name, hv, v(A=客/H=主), pd/pu → 十進位賠率 = 1 + pu/pd

執行：
    cd sportweb && .venv/bin/python src/blob_fetcher.py
    .venv/bin/python src/blob_fetcher.py --json   # 印出快照 JSON

輸出（與 fetcher.py 相同，downstream 無痛接手）：
    data/odds_YYYYMMDD_HHMM.json
    data/latest_odds.json
    sportWeb.db snapshots / odds / odds_spreads / odds_totals
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "src"))

from schema import GameOdds, OddsLine, OddsSnapshot

BLOB_URL = "https://blob3rd.sportslottery.com.tw/apidata/Pre/34765.1-Games.en.json"
NBA_TOURNAMENT_ID = "25793"
HEADERS = {
    "Referer": "https://www.sportslottery.com.tw/",
    "Origin": "https://www.sportslottery.com.tw",
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "application/json",
}
TIMEOUT = 30


def fetch_games(url: str = BLOB_URL) -> list[dict]:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def _odds(sel: dict) -> float | None:
    """pd/pu 分數賠率 → 十進位賠率（1 + pu/pd）。"""
    try:
        pd, pu = float(sel["pd"]), float(sel["pu"])
        if pd <= 0:
            return None
        return round(1.0 + pu / pd, 3)
    except (KeyError, TypeError, ValueError):
        return None


def _ah_odds(market: dict) -> tuple[float | None, float | None]:
    """取 market 的 (away, home) 賠率，依 cs[].v 判斷 A/H。"""
    away = home = None
    for sel in market.get("cs", []):
        if sel.get("v") == "A":
            away = _odds(sel)
        elif sel.get("v") == "H":
            home = _odds(sel)
    return away, home


def _over_under_odds(market: dict) -> tuple[float | None, float | None]:
    """取大小分 market 的 (over, under) 賠率，依選項名稱判斷。"""
    over = under = None
    for sel in market.get("cs", []):
        name = (sel.get("name") or "").lower()
        if name.startswith("over"):
            over = _odds(sel)
        elif name.startswith("under"):
            under = _odds(sel)
    return over, under


def parse_game(g: dict) -> GameOdds:
    game = GameOdds(
        game_id=str(g.get("id", "")),
        away=g.get("an", ""),
        home=g.get("hn", ""),
        start_time=g.get("kt", ""),
        source_url=BLOB_URL,
    )
    for m in g.get("ms", []):
        name = m.get("name") or ""
        if "(Incl. Overtime)" not in name:
            continue  # 排除上半場/單節等非全場市場
        mv = m.get("mv")
        if name.startswith("Winner ("):
            away, home = _ah_odds(m)
            if away and home:
                game.moneyline = OddsLine(line=0.0, away=away, home=home)
        elif name.startswith("Handicap ") and mv is not None:
            away, home = _ah_odds(m)
            if away and home:
                game.spreads.append(OddsLine(line=float(mv), away=away, home=home))
        elif name.startswith("Total ") and mv is not None:
            over, under = _over_under_odds(m)
            if over and under:
                game.totals.append(OddsLine(line=float(mv), over=over, under=under))
    game.spreads.sort(key=lambda s: s.line)
    game.totals.sort(key=lambda t: t.line)
    return game


def build_snapshot() -> OddsSnapshot:
    snap = OddsSnapshot.now(league="NBA", source_url=BLOB_URL)
    try:
        raw_games = fetch_games()
    except Exception as e:
        snap.error = f"blob fetch: {e}"
        return snap
    nba = [g for g in raw_games if str(g.get("ti")) == NBA_TOURNAMENT_ID]
    snap.games = [parse_game(g) for g in nba]
    return snap


def save_snapshot(snap: OddsSnapshot) -> Path:
    BASE_DIR.joinpath("data").mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    p_ts = BASE_DIR / "data" / f"odds_{ts}.json"
    p_latest = BASE_DIR / "data" / "latest_odds.json"
    body = json.dumps(snap.to_dict(), ensure_ascii=False, indent=2)
    p_ts.write_text(body, encoding="utf-8")
    p_latest.write_text(body, encoding="utf-8")
    return p_ts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="print snapshot JSON to stdout")
    args = ap.parse_args()

    print(f"[{datetime.now().isoformat(timespec='seconds')}] 抓取 NBA 賠率（blob CDN API）...")
    snap = build_snapshot()

    if snap.error:
        print(f"❌ 失敗: {snap.error}", file=sys.stderr)
        # 失敗時不覆寫 latest_odds.json（保留上次成功的線），但寫告警；
        # 下游 sync_sportweb_data 會用 fetched_at 年齡判斷是否過期而拒用。
        try:
            alert = BASE_DIR.parent / "logs" / "alerts.log"
            alert.parent.mkdir(parents=True, exist_ok=True)
            with alert.open("a", encoding="utf-8") as f:
                f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                        f"[blob_fetcher] ✗ 抓取失敗: {str(snap.error)[:120]}\n")
        except OSError:
            pass
        return 1

    print(f"✅ 取得 {len(snap.games)} 場 NBA 比賽")
    for g in snap.games:
        ml = g.moneyline.to_dict() if g.moneyline else {}
        print(f"  [{g.game_id}] {g.away} @ {g.home} ({g.start_time})")
        print(f"    ML: {ml} | spreads: {len(g.spreads)} | totals: {len(g.totals)}")
        prob = g.implied_prob()
        if prob:
            print(f"    隱含機率: away {prob['away_norm']*100:.1f}% / home {prob['home_norm']*100:.1f}% "
                  f"(vig {prob['overround']*100:.1f}%)")

    path = save_snapshot(snap)
    print(f"💾 存於 {path}")

    if args.json:
        print(json.dumps(snap.to_dict(), ensure_ascii=False, indent=2))

    # 寫入 SQLite（補上 implied_prob 供 odds 表的機率欄位）
    try:
        from sport_db import init_db as _db_init, insert_snapshot, DB_PATH as _db
        payload = snap.to_dict()
        for gd, g in zip(payload["games"], snap.games):
            impl = g.implied_prob()
            if impl:
                gd["implied_prob"] = impl
        _db_init(_db)
        sid = insert_snapshot(_db, payload)
        if sid:
            print(f"📊 寫入 DB snapshot #{sid} ({len(snap.games)} 場)")
    except Exception as e:
        print(f"[warn] DB write failed: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

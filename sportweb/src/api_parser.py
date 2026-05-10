"""
API Parser — 從 /services/content/get 的 marketGroup 回應解析成 OddsSnapshot

關鍵 API 路徑：
  POST /services/content/get
  body: {"contentId": {"type": "marketGroup", "id": "60067.1"},
         "clientContext": {...}}

回傳結構：
  data.markets[i].selections[j] = {
    name: "球隊名",
    competitornumber: "1" | "2",  # 1=home 2=away
    currentpriceup: "23",
    currentpricedown: "50",       # → decimal odds = up/down + 1
  }

已知 marketgroup：
  60067.1 - Main - NBA（獨贏/讓分/大小）
  60066.1 - Outright - NBA（冠軍彩等）
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from schema import GameOdds, OddsLine, OddsSnapshot, to_espn_name


# 已知 marketgroup ID
MARKETGROUP_IDS = {
    "nba_main": "60067.1",     # Main (moneyline + spread + total)
    "nba_outright": "60066.1", # Outright
}

# market type 識別（從 idfomarkettype 或 name）
MARKET_TYPE_MONEYLINE = "不讓分"
MARKET_TYPE_SPREAD    = "讓分"
MARKET_TYPE_TOTAL     = "大/小"

# Full-game prefixes (event-level response has half/Q1/team markets too)
FULL_GAME_SPREAD = "[總分]讓分"
FULL_GAME_TOTAL  = "[總分]大小"


def parse_odds_fraction(up_str: str, down_str: str) -> float | None:
    """英式分數賠率 → decimal odds。"""
    try:
        up = float(up_str)
        down = float(down_str)
        if down <= 0:
            return None
        return round(up / down + 1.0, 3)
    except (ValueError, TypeError):
        return None


def parse_market_group(data: dict) -> list[GameOdds]:
    """解析 marketGroup API response → List[GameOdds]

    關鍵觀察：
    - data.markets[] 是 *多個 market*（可能同一場比賽有多個 market 類型）
    - 每個 market 有 idfoevent（比賽 ID）
    - 我們用 idfoevent 把同一場比賽的多個 market 合併成一個 GameOdds
    """
    if not isinstance(data, dict):
        return []
    d = data.get("data") if "data" in data else data
    if not isinstance(d, dict):
        return []
    markets = d.get("markets") or []
    if not markets:
        return []

    # 以 idfoevent 為 key 合併同一場比賽的多 market
    games_by_event: dict[str, GameOdds] = {}

    for m in markets:
        idfoevent = str(m.get("idfoevent", ""))
        if not idfoevent:
            continue

        # 初始化 GameOdds
        if idfoevent not in games_by_event:
            away_ch = m.get("participantname_away", "")
            home_ch = m.get("participantname_home", "")
            games_by_event[idfoevent] = GameOdds(
                game_id=idfoevent,
                away=to_espn_name(away_ch),
                home=to_espn_name(home_ch),
                start_time=m.get("tsstart", ""),
            )
        g = games_by_event[idfoevent]

        # 解析 selections → 找 odds
        selections = m.get("selections") or []
        if len(selections) < 2:
            continue

        market_name = m.get("name", "")
        # 根據 market name 分類
        if MARKET_TYPE_MONEYLINE in market_name:
            # 獨贏：只有 home/away
            home_odds, away_odds = _split_home_away(selections)
            g.moneyline = OddsLine(line=0.0, away=away_odds, home=home_odds)

        elif MARKET_TYPE_SPREAD in market_name:
            # 讓分：要取 handicap 數字
            line = _extract_handicap(m)
            home_odds, away_odds = _split_home_away(selections)
            g.spreads.append(OddsLine(
                line=abs(line),
                away=away_odds,
                home=home_odds,
            ))

        elif MARKET_TYPE_TOTAL in market_name or "大" in market_name:
            # 大/小
            line = _extract_handicap(m)
            # selections 中 idfoselectiontypecategory 可能 = over/under
            over_odds, under_odds = _split_over_under(selections)
            g.totals.append(OddsLine(
                line=line,
                over=over_odds,
                under=under_odds,
            ))

    return list(games_by_event.values())


def _split_home_away(selections: list) -> tuple:
    """從 selections 分出 home/away 的 decimal odds。"""
    home = away = None
    for s in selections:
        odds = parse_odds_fraction(
            s.get("currentpriceup", ""),
            s.get("currentpricedown", "")
        )
        # hadvalue: H = home, A = away
        had = s.get("hadvalue", "")
        comp = s.get("competitornumber", "")
        if had == "H" or comp == "1":
            home = odds
        elif had == "A" or comp == "2":
            away = odds
    return home, away


def _split_over_under(selections: list) -> tuple:
    """分 over / under。"""
    over = under = None
    for s in selections:
        odds = parse_odds_fraction(
            s.get("currentpriceup", ""),
            s.get("currentpricedown", "")
        )
        had = s.get("hadvalue", "")
        # 大 = Over；小 = Under
        name = s.get("name", "")
        if had == "O" or "大" in name or "Over" in name:
            over = odds
        elif had == "U" or "小" in name or "Under" in name:
            under = odds
    return over, under


def _extract_handicap(market: dict) -> float:
    """從 market 的某欄位抓 handicap 數字（讓分或 O/U 的線）。

    目前嘗試：
    - market["handicap"] / market["overundervalue"]
    - market name 例如 "讓分 -5.5"
    - selections[].name 內的數字
    """
    import re
    # 先試常見欄位
    for key in ("handicap", "overundervalue", "value"):
        v = market.get(key)
        if v:
            try:
                return float(v)
            except (ValueError, TypeError):
                pass
    # 從 market name 提取
    name = market.get("name", "")
    m = re.search(r"([+-]?\d+(?:\.\d+)?)", name)
    if m:
        return float(m.group(1))
    # 從 selection names（取 currenthandicap 欄位或 name 中的數字）
    for s in market.get("selections", []):
        ch = s.get("currenthandicap")
        if ch:
            try:
                return float(ch)
            except (ValueError, TypeError):
                pass
        sn = s.get("name", "")
        m = re.search(r"([+-]?\d+(?:\.\d+)?)", sn)
        if m:
            return float(m.group(1))
    return 0.0


def parse_event_markets(data: dict, existing: GameOdds | None = None) -> GameOdds | None:
    """解析 event-level API response → GameOdds（補充 spreads/totals 到現有物件）。

    Event API 回傳的 markets 包含：
      - 不讓分（moneyline）
      - [總分]讓分 X（full-game spread，多條線）
      - [總分]大小 X（full-game total）
      - [上半場]/[第1節]/隊名 大小 等（非全場，跳過）
    """
    import re as _re

    if not isinstance(data, dict):
        return None
    d = data.get("data") if "data" in data else data
    if not isinstance(d, dict):
        return None
    markets = d.get("markets") or []
    if not markets:
        return existing

    # 從第一個 market 取基本資訊
    m0 = markets[0]
    idfoevent = str(m0.get("idfoevent", ""))

    if existing is None:
        away_ch = m0.get("participantname_away", "")
        home_ch = m0.get("participantname_home", "")
        existing = GameOdds(
            game_id=idfoevent,
            away=to_espn_name(away_ch),
            home=to_espn_name(home_ch),
            start_time=m0.get("tsstart", ""),
        )

    for m in markets:
        name = m.get("name", "")
        selections = m.get("selections") or []
        if len(selections) < 2:
            continue

        # Full-game moneyline
        if name == MARKET_TYPE_MONEYLINE:
            if existing.moneyline is None:
                home_odds, away_odds = _split_home_away(selections)
                existing.moneyline = OddsLine(line=0.0, away=away_odds, home=home_odds)

        # Full-game spread: [總分]讓分 X
        elif name.startswith(FULL_GAME_SPREAD):
            line_match = _re.search(r"([+-]?\d+(?:\.\d+)?)", name[len(FULL_GAME_SPREAD):])
            line = float(line_match.group(1)) if line_match else _extract_handicap(m)
            home_odds, away_odds = _split_home_away(selections)
            existing.spreads.append(OddsLine(line=line, away=away_odds, home=home_odds))

        # Full-game total: [總分]大小 X
        elif name.startswith(FULL_GAME_TOTAL):
            line_match = _re.search(r"(\d+(?:\.\d+)?)", name[len(FULL_GAME_TOTAL):])
            line = float(line_match.group(1)) if line_match else _extract_handicap(m)
            over_odds, under_odds = _split_over_under(selections)
            existing.totals.append(OddsLine(line=line, over=over_odds, under=under_odds))

    return existing

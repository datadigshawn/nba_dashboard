"""
台灣運彩 NBA 頁面 DOM 解析器（骨架版）

⚠️ CSS selectors 是 **placeholder**，需要等 Phase 2（bootstrap 過 CF 後）
   用 DevTools 確認後再替換成真的。

使用方式：
    from playwright.async_api import Page
    from parser import parse_nba_page

    async def scrape(page: Page) -> OddsSnapshot:
        await page.goto(NBA_URL)
        await page.wait_for_load_state("networkidle")
        return await parse_nba_page(page)
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING

from schema import GameOdds, OddsLine, OddsSnapshot, to_espn_name

if TYPE_CHECKING:
    from playwright.async_api import Page, Locator


NBA_URL = (
    "https://www.sportslottery.com.tw/sportsbook/sport/"
    "%E7%B1%83%E7%90%83/%E7%BE%8E%E5%9C%8B/%E7%BE%8E%E5%9C%8B%E8%81%B7%E7%B1%83/34801.1"
)


# ─── Selector 清單（PLACEHOLDER，需用 DevTools 確認）────────────
# 這些是根據截圖推斷的可能 selector。實際可能是 class 帶 hash 如 "sc-abc123"。
# 用 Playwright 的 .get_by_text() 或部分 class 匹配 *= 會更穩。
SELECTORS = {
    # 比賽容器：每場一個
    "game_card":        '[data-event-id], [class*="event-card"], [class*="game-card"], article',
    # game 顯示編號（3 位數 badge）
    "game_id":          '[class*="event-id"], [class*="game-code"], .badge-number',
    # 隊伍名稱
    "team_away":        '[class*="team-away"] [class*="name"], [class*="away-team"]',
    "team_home":        '[class*="team-home"] [class*="name"], [class*="home-team"]',
    # 比賽時間
    "start_time":       '[class*="start-time"], [class*="game-time"], time',
    # market 群組
    "market_group":     '[class*="market"], [data-market-id]',
    "market_title":     '[class*="market-title"], [class*="market-name"]',
    # 賠率按鈕
    "odds_button":      'button[class*="odd"], [role="button"][class*="odds"]',
    "odds_value":       '[class*="value"], [class*="price"]',
}


# ─── Market 類型識別（從中文標題判斷）────────────────────────────
def classify_market(title: str) -> str | None:
    """從 market title 判斷類型。回傳 'moneyline' / 'spread' / 'total' / None。"""
    title = title.strip()
    if "不讓分" in title or "獨贏" in title:
        return "moneyline"
    if "讓分" in title:
        return "spread"
    if "大/小" in title or "大小" in title or "總分" in title:
        return "total"
    return None


def extract_line_number(title: str) -> float:
    """從 'X讓分 -17.5' 或 '大/小 120.5' 擷取數字。"""
    m = re.search(r"([+-]?\d+(?:\.\d+)?)", title)
    return float(m.group(1)) if m else 0.0


# ─── 主解析函式 ────────────────────────────────────────────────
async def parse_nba_page(page) -> OddsSnapshot:
    """解析 NBA 主頁，回傳 OddsSnapshot。"""
    snap = OddsSnapshot.now(league="NBA", source_url=page.url)

    # 檢查是否還在 Cloudflare challenge
    title = await page.title()
    if "just a moment" in title.lower() or "cloudflare" in title.lower():
        snap.cf_passed = False
        snap.error = f"Still on Cloudflare challenge page (title={title})"
        return snap

    try:
        # 等一下讓 JS 載完
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass

    game_cards = page.locator(SELECTORS["game_card"])
    count = await game_cards.count()

    if count == 0:
        snap.error = "No game cards found (selectors may need update)"
        return snap

    for i in range(count):
        card = game_cards.nth(i)
        try:
            game = await parse_game_card(card, snap.source_url)
            if game:
                snap.games.append(game)
        except Exception as e:
            print(f"⚠️ 解析第 {i} 場失敗: {e}")
            continue

    return snap


async def parse_game_card(card, source_url: str) -> GameOdds | None:
    """解析單一比賽卡片。"""
    # Game ID
    try:
        gid = (await card.locator(SELECTORS["game_id"]).first.inner_text()).strip()
    except Exception:
        gid = ""

    # Teams
    try:
        away_ch = (await card.locator(SELECTORS["team_away"]).first.inner_text()).strip()
        home_ch = (await card.locator(SELECTORS["team_home"]).first.inner_text()).strip()
    except Exception:
        return None

    if not away_ch or not home_ch:
        return None

    game = GameOdds(
        game_id=gid,
        away=to_espn_name(away_ch),
        home=to_espn_name(home_ch),
        source_url=source_url,
    )

    # Start time
    try:
        t = (await card.locator(SELECTORS["start_time"]).first.inner_text()).strip()
        game.start_time = t
    except Exception:
        pass

    # Markets
    market_groups = card.locator(SELECTORS["market_group"])
    mg_count = await market_groups.count()

    for j in range(mg_count):
        mg = market_groups.nth(j)
        try:
            title = (await mg.locator(SELECTORS["market_title"]).first.inner_text()).strip()
        except Exception:
            continue

        kind = classify_market(title)
        if not kind:
            continue

        odds_elements = mg.locator(SELECTORS["odds_button"])
        oc = await odds_elements.count()
        if oc < 2:
            continue

        # 通常 away / home 順序：左→右 = away→home
        def _parse_odds(text: str) -> float | None:
            m = re.search(r"\d+\.\d+", text)
            return float(m.group(0)) if m else None

        first_text  = await odds_elements.nth(0).inner_text()
        second_text = await odds_elements.nth(1).inner_text()
        first_odds  = _parse_odds(first_text)
        second_odds = _parse_odds(second_text)

        line_num = extract_line_number(title)

        if kind == "moneyline":
            game.moneyline = OddsLine(
                line=0.0, away=first_odds, home=second_odds
            )
        elif kind == "spread":
            game.spreads.append(OddsLine(
                line=abs(line_num), away=first_odds, home=second_odds
            ))
        elif kind == "total":
            game.totals.append(OddsLine(
                line=line_num, over=first_odds, under=second_odds
            ))

    return game

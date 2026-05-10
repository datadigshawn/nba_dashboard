#!/usr/bin/env python3
"""
API 探索腳本 — 找出讓分/大小分的 marketGroup ID 或 event-level 端點。

用法：
    cd /Users/shawnclaw/autobot/sportWeb
    .venv/bin/python src/api_explore_markets.py
"""
import asyncio
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_FILE = BASE_DIR / "data" / "cf_state.json"
HOME_URL = "https://www.sportslottery.com.tw/sportsbook/sport"


async def evaluate_api(page, content_type: str, content_id: str) -> dict:
    """Call /services/content/get with given type and id."""
    js = """async ([type, id]) => {
        try {
            const r = await fetch('/services/content/get', {
                method: 'POST',
                credentials: 'include',
                headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
                body: JSON.stringify({
                    contentId: {type, id},
                    clientContext: {language: 'ZH', ipAddress: '0.0.0.0'}
                })
            });
            const text = await r.text();
            if (!r.ok) return {status: r.status, err: text.slice(0, 300)};
            try { return {status: r.status, data: JSON.parse(text)}; }
            catch(e) { return {status: r.status, err: 'not json', raw: text.slice(0, 500)}; }
        } catch (e) { return {err: String(e)}; }
    }"""
    return await page.evaluate(js, [content_type, content_id])


async def main():
    if not STATE_FILE.exists():
        print("❌ cf_state.json missing, run bootstrap first")
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True, channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            storage_state=str(STATE_FILE), locale="zh-TW",
            viewport={"width": 1440, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        page = await ctx.new_page()
        await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(5000)

        print("=" * 70)
        print("  API EXPLORE: 尋找讓分 / 大小分 markets")
        print("=" * 70)

        # 1) Get the known moneyline marketgroup to find event IDs
        print("\n[1] 先抓 marketGroup 60067.1 (Main) 取 event IDs...")
        r = await evaluate_api(page, "marketGroup", "60067.1")
        if "err" in r:
            print(f"  ❌ 60067.1 failed: {r.get('err', '')[:200]}")
            await browser.close()
            return

        data = r.get("data", {}).get("data", r.get("data", {}))
        markets = data.get("markets", [])
        event_ids = set()
        for m in markets:
            eid = m.get("idfoevent")
            if eid:
                event_ids.add(str(eid))
        print(f"  找到 {len(event_ids)} 個 event IDs: {list(event_ids)}")

        # Also check what market names are in 60067.1
        market_names = set(m.get("name", "") for m in markets)
        print(f"  60067.1 market names: {market_names}")

        # 2) Try nearby marketgroup IDs
        print("\n[2] 嘗試周邊 marketGroup IDs...")
        for mg_id in ["60067.2", "60067.3", "60068.1", "60069.1", "60070.1",
                       "60067.11", "60067.12", "60067.21",
                       "60071.1", "60072.1", "60073.1", "60074.1", "60075.1"]:
            r = await evaluate_api(page, "marketGroup", mg_id)
            if "err" in r or r.get("status") != 200:
                continue
            d = r.get("data", {})
            if isinstance(d, dict):
                dd = d.get("data", d)
                if isinstance(dd, dict):
                    mks = dd.get("markets", [])
                    if mks:
                        mkt_names = set(m.get("name", "") for m in mks)
                        print(f"  ✅ {mg_id}: {len(mks)} markets — names: {mkt_names}")
                    else:
                        top_keys = list(dd.keys())[:5]
                        print(f"  ⚠️ {mg_id}: has data but no markets. keys: {top_keys}")
                else:
                    print(f"  ⚠️ {mg_id}: data is {type(dd).__name__}")

        # 3) Try event-level queries
        print("\n[3] 嘗試 event-level 查詢...")
        for eid in list(event_ids)[:2]:
            for ct in ["event", "foevent", "eventMarkets", "foEvent",
                        "matchMarkets", "match", "fixture"]:
                r = await evaluate_api(page, ct, eid)
                if "err" in r:
                    continue
                d = r.get("data", {})
                if isinstance(d, dict) and d:
                    dd = d.get("data", d)
                    if isinstance(dd, dict):
                        mks = dd.get("markets", [])
                        if mks:
                            mkt_names = set(m.get("name", "") for m in mks)
                            print(f"  ✅ type={ct} id={eid}: {len(mks)} markets — {mkt_names}")
                        else:
                            top_keys = list(dd.keys())[:8]
                            print(f"  ⚠️ type={ct} id={eid}: keys={top_keys}")

        # 4) Try fetching the navigation tree to find spread/total market groups
        print("\n[4] 查詢 navigation tree (boNavigationList)...")
        for nav_id in ["1355/34801.1"]:
            r = await evaluate_api(page, "boNavigationList", nav_id)
            if "err" in r:
                print(f"  ❌ boNavigationList/{nav_id}: {r.get('err','')[:100]}")
                continue
            d = r.get("data", {})
            dd = d.get("data", d)
            # Print the tree structure looking for spread/total groups
            if isinstance(dd, dict):
                children = dd.get("children", []) or dd.get("items", [])
                for child in children[:15]:
                    name = child.get("name", "")
                    cid = child.get("id", "")
                    ctype = child.get("type", "")
                    mg_ids = child.get("marketGroupIds", [])
                    print(f"  [{ctype}] id={cid} name={name}")
                    if mg_ids:
                        print(f"     marketGroupIds: {mg_ids}")
                    # Go one level deeper
                    sub_children = child.get("children", []) or child.get("items", [])
                    for sc in sub_children[:5]:
                        sname = sc.get("name", "")
                        sid = sc.get("id", "")
                        stype = sc.get("type", "")
                        smg = sc.get("marketGroupIds", [])
                        print(f"    [{stype}] id={sid} name={sname}")
                        if smg:
                            print(f"       marketGroupIds: {smg}")

        # 5) Try event markets with boEventMarketGroups or similar
        print("\n[5] 查詢 event-level market groups...")
        for eid in list(event_ids)[:2]:
            for ct in ["boEventMarketGroups", "eventDetail", "eventMarketGroups",
                        "foEventWithMarketGroups", "foEventMarketList"]:
                r = await evaluate_api(page, ct, eid)
                if "err" in r:
                    continue
                d = r.get("data", {})
                if isinstance(d, dict) and d:
                    print(f"  ✅ type={ct} id={eid}: keys={list(d.keys())[:10]}")
                    dd = d.get("data", d)
                    if isinstance(dd, dict):
                        for k in dd:
                            v = dd[k]
                            if isinstance(v, list) and len(v) > 0:
                                print(f"     {k}: [{len(v)} items], first keys: {list(v[0].keys())[:8] if isinstance(v[0], dict) else type(v[0])}")
                            elif isinstance(v, dict) and v:
                                print(f"     {k}: dict with keys {list(v.keys())[:6]}")

        await browser.close()
        print("\n✅ 探索完成")


if __name__ == "__main__":
    asyncio.run(main())

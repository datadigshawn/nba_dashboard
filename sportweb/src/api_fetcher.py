"""
API Fetcher — 直接打 /services/content/get，完全繞過 DOM 解析

用 Playwright 載入 cf_state 再用 page.request.post 打 API
這樣可以拿到完整 JSON 而不用 JS 渲染

執行：
    cd /Users/shawnclaw/autobot/investing/sports/autobots_NBA/sportweb
    .venv/bin/python src/api_fetcher.py           # 試探性抓完整導航樹
    .venv/bin/python src/api_fetcher.py --nba     # 只抓 NBA 相關 content

產出：
    data/api_raw_<type>_<id>.json
"""
import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_FILE = BASE_DIR / "data" / "cf_state.json"

API_URL_MAIN = "https://www.sportslottery.com.tw/services/content/get"
API_URL_PR   = "https://www-talo-ssb-pr.sportslottery.com.tw/services/content/get"

# 已知 ID 對照（來自 TODO.md + API 實測）
KNOWN_IDS = {
    "top": "1355/top",           # 所有運動
    "basketball": "1355/34765.1", # 籃球
    "usa": "1355/34800.1",       # 美國
    "nba": "1355/34801.1",       # NBA
    "nba_foc": "22064",          # NBA focontentid（舊）
}

# 可能的 content type（從 TODO.md + 實測）
CONTENT_TYPES_TO_TRY = [
    "boNavigationList",   # ✅ 已知有效
    "boNavigationPath",   # ✅ 已知有效（但只回麵包屑）
    "bannerCategoryList",  # banner
    "liveStreamEventList", # 直播
    # 未知但可能有用的
    "eventList", "gameList", "gameGroupList", "marketGroupList",
    "fwEventList", "fwGameList", "fwGameGroupList", "fwMarketGroupList",
    "upcomingEvents", "matchList", "fixtureList",
    "tournamentEventList", "events", "tournament",
]


async def fetch_content(page, content_type: str, content_id: str,
                       use_pr: bool = True) -> dict:
    """在 page 的 JS 環境裡 fetch（瀏覽器同源請求，CF 放行）。"""
    url = "/services/content/get"  # 同源相對路徑
    js = """async ({url, type, id}) => {
        try {
            const r = await fetch(url, {
                method: 'POST',
                credentials: 'include',
                headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
                body: JSON.stringify({
                    contentId: {type, id},
                    clientContext: {language: 'ZH', ipAddress: '0.0.0.0'}
                })
            });
            const text = await r.text();
            return {status: r.status, size: text.length, text: text.slice(0, 80000)};
        } catch (e) {
            return {err: String(e)};
        }
    }"""
    try:
        res = await page.evaluate(js, {"url": url, "type": content_type, "id": content_id})
        if "err" in res:
            return {"error": res["err"]}
        text = res.get("text", "")
        try:
            data = json.loads(text)
        except Exception:
            data = {"_raw": text[:500]}
        return {"status": res.get("status"), "size": res.get("size", 0), "data": data}
    except Exception as e:
        return {"error": str(e)}


def walk_find_nba(node, path="", results=None):
    """遞迴找 navigation tree 裡的 NBA 節點。"""
    if results is None:
        results = []
    if isinstance(node, list):
        for item in node:
            walk_find_nba(item, path, results)
        return results
    if not isinstance(node, dict):
        return results

    name = node.get("name", "")
    nid = node.get("idfwbonavigation", "")
    current = f"{path}/{name}" if name else path

    # 找 NBA 相關
    if any(kw in name.lower() for kw in ["nba", "美國職籃"]) or "34801" in nid:
        results.append({
            "path": current,
            "id": nid,
            "name": name,
            "numfwmarketgroups": node.get("numfwmarketgroups"),
            "numbonavigationchildren": node.get("numbonavigationchildren"),
            "node": node,
        })

    # 遞迴 children
    children = node.get("bonavigationnodes")
    if children:
        for c in children:
            walk_find_nba(c, current, results)

    return results


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nba", action="store_true", help="集中抓 NBA 相關")
    args = ap.parse_args()

    if not STATE_FILE.exists():
        print("❌ 需先跑 bootstrap.py"); sys.exit(1)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            storage_state=str(STATE_FILE),
            locale="zh-TW",
            viewport={"width": 1440, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        # 先開一個頁面讓 CF cookie 生效
        page = await ctx.new_page()
        await page.goto("https://www.sportslottery.com.tw/sportsbook/sport",
                        wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)
        print("✓ 頁面載入完成，開始打 API")
        print()

        # Step 1: 拿完整 top 導航樹
        print("=" * 60)
        print("  1. 抓 boNavigationList 1355/top（完整運動/聯盟樹）")
        print("=" * 60)
        top_result = await fetch_content(page, "boNavigationList", "1355/top")
        if top_result.get("status") == 200:
            print(f"  ✓ {top_result['size']:,} bytes")
            out = BASE_DIR / "data" / "api_nav_top.json"
            out.write_text(json.dumps(top_result["data"], ensure_ascii=False, indent=2))
            print(f"  → 存至 {out}")

            # 找 NBA 節點
            nba_nodes = walk_find_nba(top_result["data"].get("data", {}))
            print(f"\n  找到 {len(nba_nodes)} 個 NBA 相關節點:")
            for n in nba_nodes[:5]:
                print(f"    [{n['id']}] {n['path']}")
                print(f"      marketgroups: {n['numfwmarketgroups']}, children: {n['numbonavigationchildren']}")
        else:
            print(f"  ✗ 失敗: {top_result}")

        # Step 2: 針對 NBA ID 掃 20 種 content type
        print("\n" + "=" * 60)
        print("  2. 針對 NBA ID 掃所有 content type")
        print("=" * 60)
        nba_ids = ["1355/34801.1", "34801.1", "1355/22064", "22064"]

        hits = []
        for nba_id in nba_ids:
            print(f"\n--- 測 NBA ID = {nba_id} ---")
            for ctype in CONTENT_TYPES_TO_TRY:
                r = await fetch_content(page, ctype, nba_id)
                status = r.get("status")
                size = r.get("size", 0)
                data = r.get("data", {})
                err = data.get("errorType") if isinstance(data, dict) else None

                # 認定成功：200 + size > 200 + 無 errorType
                hit = (status == 200) and (size > 200) and not err
                marker = " ⭐" if hit else ""
                print(f"  {ctype:25s}  st={status}  err={str(err)[:15]:15s}  size={size:>6}B{marker}")

                if hit:
                    hits.append({"type": ctype, "id": nba_id, "size": size, "data": data})

                await page.wait_for_timeout(100)  # 避免太快

        print(f"\n🎯 命中 {len(hits)} 組")
        if hits:
            for h in hits:
                print(f"\n  ⭐ {h['type']} / {h['id']}  ({h['size']}B)")
                out = BASE_DIR / "data" / f"api_hit_{h['type']}_{h['id'].replace('/', '_')}.json"
                out.write_text(json.dumps(h["data"], ensure_ascii=False, indent=2))
                # 印 data 前 500 字
                sample = json.dumps(h["data"].get("data"), ensure_ascii=False)[:500]
                print(f"    {sample}")
                print(f"    → {out}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

"""
方案 B v3：用 page.evaluate fetch 同源 URL（www.sportslottery.com.tw，不跨域）
"""
import asyncio
import json
import os
from datetime import datetime
from playwright.async_api import async_playwright

HOMEPAGE = "https://www.sportslottery.com.tw/sportsbook/sport"
BBALL_URL = "https://www.sportslottery.com.tw/sportsbook/sport/%E7%B1%83%E7%90%83/34765.1"
API_URL = "https://www.sportslottery.com.tw/services/content/get"
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "debug")
os.makedirs(OUT_DIR, exist_ok=True)

NBA_IDS = [
    "1355/34801.1",
    "34801.1",
    "1355/22064",
    "22064",
]

CONTENT_TYPES = [
    "eventList", "fwevent", "fwEventList", "fwGameList",
    "fwGameGroupList", "gameList", "gameGroupList",
    "marketGroupList", "marketList", "tournamentEventList",
    "upcomingEvents", "matchList", "fixture", "fixtureList",
    "gamegroups", "marketgroups", "fwmarketgrouplist",
    "events", "tournament", "league",
]


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        ctx = await browser.new_context(locale="zh-TW", viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()

        print("→ 首頁通過 Cloudflare")
        await page.goto(HOMEPAGE, wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)
        await page.goto(BBALL_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)

        # 先驗證同源 fetch（不 parse json，看 raw）
        print("→ 測試同源 fetch (raw)")
        test = await page.evaluate("""async () => {
            const r = await fetch('https://www.sportslottery.com.tw/services/content/get', {
                method: 'POST',
                credentials: 'include',
                headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
                body: JSON.stringify({
                    contentId: {type: 'boNavigationList', id: '1355/top'},
                    clientContext: {language: 'ZH', ipAddress: '0.0.0.0'}
                })
            });
            const text = await r.text();
            return {status: r.status, len: text.length, preview: text.slice(0,300), url: r.url};
        }""")
        print(f"   status={test.get('status')} len={test.get('len')}")
        print(f"   url={test.get('url')}")
        print(f"   preview: {test.get('preview')[:200]}")

        if test.get("status") != 200 or not test.get("preview","").startswith("{"):
            print("❌ 同源 fetch 失敗")
            await browser.close()
            return

        print(f"\n→ Loop {len(CONTENT_TYPES)} × {len(NBA_IDS)} 組")
        results = []
        for ctype in CONTENT_TYPES:
            for cid in NBA_IDS:
                r = await page.evaluate("""async ({type, id}) => {
                    try {
                        const resp = await fetch('/services/content/get', {
                            method: 'POST',
                            credentials: 'include',
                            headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
                            body: JSON.stringify({
                                contentId: {type, id},
                                clientContext: {language: 'ZH', ipAddress: '0.0.0.0'}
                            })
                        });
                        return {status: resp.status, body: await resp.json()};
                    } catch (e) {
                        return {err: String(e)};
                    }
                }""", {"type": ctype, "id": cid})

                status = r.get("status")
                body = r.get("body", {})
                err = body.get("errorType") if isinstance(body, dict) else None
                data = body.get("data") if isinstance(body, dict) else None
                size = len(json.dumps(data, ensure_ascii=False)) if data else 0
                hit = (status == 200) and (size > 200) and not err
                marker = " ⭐" if hit else ""

                results.append({
                    "type": ctype, "id": cid, "status": status,
                    "error": err, "size": size,
                    "data_sample": json.dumps(data, ensure_ascii=False)[:800] if data else None,
                    "full_data": data if hit else None,
                })
                print(f"  {ctype:22s}  {cid:18s}  st={status}  err={str(err)[:15]:15s}  size={size}B{marker}")
                await page.wait_for_timeout(80)

        await browser.close()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join(OUT_DIR, f"try_types_{ts}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    hits = [r for r in results if r["status"] == 200 and r["size"] > 200 and not r["error"]]
    print(f"\n🎯 命中 {len(hits)} 組")
    for h in hits:
        print(f"\n  ⭐ {h['type']} / {h['id']}  ({h['size']}B)")
        print(f"     {h['data_sample']}")

    print(f"\n💾 完整結果: {out}")


if __name__ == "__main__":
    asyncio.run(main())

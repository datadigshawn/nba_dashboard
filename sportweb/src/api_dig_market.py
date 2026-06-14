"""
API Dig Market — 用 marketgroup ID（如 60067.1）嘗試抓實際比賽 markets

基於前一步發現：NBA 在 1355/34801.1 下有兩個 marketgroups
- Main - NBA (idfwmarketgroup: 60067.1)
- Outright - NBA (idfwmarketgroup: 60066.1)

這次針對 marketgroup ID 打所有可能的 content type
"""
import asyncio
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_FILE = BASE_DIR / "data" / "cf_state.json"

MARKET_IDS = [
    "60067.1",          # Main - NBA
    "60066.1",          # Outright - NBA
    "1355/60067.1",     # with prefix
    "1355/60066.1",
]

CONTENT_TYPES = [
    "marketGroup", "marketGroupDetails", "marketGroupContent",
    "fwMarketGroup", "fwMarketGroupDetails",
    "coupon", "couponList", "couponDetails",
    "matchesCoupon", "outrightsCoupon",
    "marketList", "markets",
    "eventList", "events", "gameList",
    "boMarketGroup", "boMarketGroupList",
]


async def fetch_content(page, content_type: str, content_id: str) -> dict:
    js = """async ({type, id}) => {
        try {
            const r = await fetch('/services/content/get', {
                method: 'POST', credentials: 'include',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    contentId: {type, id},
                    clientContext: {language: 'ZH', ipAddress: '0.0.0.0'}
                })
            });
            const text = await r.text();
            return {status: r.status, text: text.slice(0, 100000)};
        } catch (e) { return {err: String(e)}; }
    }"""
    try:
        res = await page.evaluate(js, {"type": content_type, "id": content_id})
        if "err" in res:
            return {"error": res["err"]}
        try:
            return {
                "status": res.get("status"),
                "size": len(res.get("text", "")),
                "data": json.loads(res.get("text", "{}")),
            }
        except Exception:
            return {"status": res.get("status"), "size": 0, "data": {}}
    except Exception as e:
        return {"error": str(e)}


async def main():
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
        page = await ctx.new_page()
        await page.goto("https://www.sportslottery.com.tw/sportsbook/sport",
                        wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)
        print("✓ 頁面載入完成\n")

        hits = []
        for mid in MARKET_IDS:
            print(f"─── MarketGroup ID = {mid} ───")
            for ctype in CONTENT_TYPES:
                r = await fetch_content(page, ctype, mid)
                status = r.get("status")
                size = r.get("size", 0)
                data = r.get("data", {})
                err = data.get("errorType") if isinstance(data, dict) else None
                hit = (status == 200) and (size > 200) and not err
                mark = " ⭐" if hit else ""
                print(f"  {ctype:25s}  st={status}  err={str(err)[:20]:20s}  size={size:>6}B{mark}")
                if hit:
                    hits.append({"type": ctype, "id": mid, "size": size, "data": data})
                await page.wait_for_timeout(80)
            print()

        print(f"\n🎯 命中 {len(hits)} 組\n")
        for h in hits:
            print(f"⭐ {h['type']} / {h['id']}  ({h['size']}B)")
            sample = json.dumps(h["data"].get("data"), ensure_ascii=False)[:500]
            print(f"   {sample}\n")
            out = BASE_DIR / "data" / f"api_market_{h['type']}_{h['id'].replace('/','_')}.json"
            out.write_text(json.dumps(h["data"], ensure_ascii=False, indent=2))
            print(f"   → {out}\n")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

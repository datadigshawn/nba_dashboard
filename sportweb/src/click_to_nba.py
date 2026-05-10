"""
方案 A（備用）：讓瀏覽器真的點擊進入 NBA 頁面，觀察它自己發了什麼 API
"""
import asyncio
import json
import os
from datetime import datetime
from playwright.async_api import async_playwright

HOMEPAGE = "https://www.sportslottery.com.tw/sportsbook/sport"
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "debug")
os.makedirs(OUT_DIR, exist_ok=True)


async def main():
    all_xhr = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        ctx = await browser.new_context(locale="zh-TW", viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()

        async def on_resp(r):
            if "/services/content/get" not in r.url:
                return
            try:
                body = await r.json()
                req_body = r.request.post_data
                all_xhr.append({
                    "t": datetime.now().isoformat(timespec="seconds"),
                    "url": r.url,
                    "req": json.loads(req_body) if req_body else None,
                    "resp_data_sample": json.dumps(
                        body.get("data"), ensure_ascii=False
                    )[:500] if isinstance(body, dict) else None,
                    "resp_size": len(json.dumps(body.get("data"), ensure_ascii=False)) if isinstance(body, dict) else 0,
                    "full_resp": body,
                })
            except Exception:
                pass

        page.on("response", lambda r: asyncio.create_task(on_resp(r)))

        print("→ 1/3 首頁")
        await page.goto(HOMEPAGE, wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)
        homepage_count = len(all_xhr)
        print(f"   首頁 XHR count: {homepage_count}")

        print("\n→ 2/3 點擊「籃球」")
        # 左側 sidebar 的「籃球」link
        try:
            # 找文字為「籃球」且可點擊的 link
            await page.get_by_role("link", name="籃球", exact=True).first.click(timeout=10000)
            await page.wait_for_timeout(5000)
        except Exception as e:
            print(f"   ⚠️ 找不到「籃球」link: {e}")
            # 備援：直接導航
            await page.goto("https://www.sportslottery.com.tw/sportsbook/sport/%E7%B1%83%E7%90%83/34765.1", wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)
        bball_count = len(all_xhr)
        print(f"   籃球頁新增 {bball_count - homepage_count} 個 XHR")

        print("\n→ 3/3 點擊「美國職籃」")
        try:
            await page.get_by_text("美國職籃", exact=False).first.click(timeout=10000)
            await page.wait_for_timeout(15000)
        except Exception as e:
            print(f"   ⚠️ 找不到「美國職籃」link: {e}")
            print(f"   截圖看看現在頁面")
        nba_count = len(all_xhr)
        print(f"   NBA 頁新增 {nba_count - bball_count} 個 XHR")

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        await page.screenshot(path=os.path.join(OUT_DIR, f"click_{ts}.png"), full_page=True)

        await browser.close()

    # 分析
    print("\n=== 所有 content/get 請求（時序） ===")
    for i, x in enumerate(all_xhr):
        cid = (x["req"] or {}).get("contentId", {})
        cid_id = cid.get("id", "")
        cid_type = cid.get("type", "")
        marker = ""
        if "34801" in cid_id or "美國職籃" in cid_id or "22064" in cid_id:
            marker = " 🏀"
        if x["resp_size"] > 5000:
            marker += " 📦"
        print(f"  [{i:2d}] {cid_type:22s}  id={cid_id:30s}  size={x['resp_size']:>7}B{marker}")

    # 存原始
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join(OUT_DIR, f"click_nba_{ts}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(all_xhr, f, ensure_ascii=False, indent=2)

    # 找最後幾個大 response
    big = [x for x in all_xhr if x["resp_size"] > 3000]
    print(f"\n🎯 大 response ({len(big)} 個):")
    for x in big[-5:]:
        cid = (x["req"] or {}).get("contentId", {})
        print(f"\n  type={cid.get('type')}  id={cid.get('id')}  size={x['resp_size']}B")
        print(f"  preview: {x['resp_data_sample']}")


if __name__ == "__main__":
    asyncio.run(main())

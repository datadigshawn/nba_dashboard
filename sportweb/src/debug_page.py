"""
Debug：列出籃球底下所有聯盟與比賽
"""
import asyncio
import json
import os
from datetime import datetime
from playwright.async_api import async_playwright

# 先用籃球 sport 頁
BBALL_URL = "https://www.sportslottery.com.tw/sportsbook/sport/%E7%B1%83%E7%90%83/34765.1"
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "debug")
os.makedirs(OUT_DIR, exist_ok=True)


async def main():
    xhr = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            locale="zh-TW",
            viewport={"width": 1440, "height": 900},
        )
        page = await ctx.new_page()

        async def on_resp(r):
            if "/services/content/get" not in r.url:
                return
            try:
                body = await r.json()
                req = r.request.post_data
                xhr.append({"req": json.loads(req) if req else None, "resp": body})
            except Exception:
                pass

        page.on("response", lambda r: asyncio.create_task(on_resp(r)))

        print("→ 首頁")
        await page.goto("https://www.sportslottery.com.tw/sportsbook/sport", wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)

        print(f"→ 籃球 {BBALL_URL}")
        try:
            await page.goto(BBALL_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"  {e}")
        await page.wait_for_timeout(15000)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        await page.screenshot(path=os.path.join(OUT_DIR, f"bball_{ts}.png"), full_page=True)

        text = await page.evaluate("() => document.body.innerText")
        print(f"\n=== 籃球頁內容前 1000 字元 ===\n{text[:1000]}")

        await browser.close()

    # 分析籃球相關 XHR
    print(f"\n=== 籃球頁 content/get 請求 ===")
    for x in xhr:
        cid = x["req"].get("contentId", {}) if x["req"] else {}
        resp_data = x["resp"].get("data") if isinstance(x["resp"], dict) else None
        size = len(json.dumps(resp_data, ensure_ascii=False)) if resp_data else 0
        cid_id = cid.get("id", "")
        if "34765" in cid_id or "basketball" in cid_id.lower() or size > 5000:
            print(f"  {size:>7}B  type={cid.get('type','')} id={cid_id}")

    # 存完整結果
    with open(os.path.join(OUT_DIR, f"bball_xhr_{ts}.json"), "w", encoding="utf-8") as f:
        json.dump(xhr, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    asyncio.run(main())

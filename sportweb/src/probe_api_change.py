"""
快速診斷：載入運彩首頁，在 SPA 內點進 NBA（美國職籃），
攔截所有請求找出新版賠率 JSON 端點（6/8 改版後 /services/content/get 已失效）。

執行：
    cd sportweb && .venv/bin/python src/probe_api_change.py
"""
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).resolve().parent.parent
HOME_URL = "https://www.sportslottery.com.tw/sportsbook/sport"

captured = []


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        async def on_response(resp):
            url = resp.url
            if "google" in url or url.endswith((".png", ".jpg", ".svg", ".woff2", ".css")):
                return
            body = ""
            ct = resp.headers.get("content-type", "")
            if "json" in ct:
                try:
                    body = (await resp.text())[:300]
                except Exception:
                    body = "<unreadable>"
            captured.append({
                "method": resp.request.method,
                "url": url,
                "status": resp.status,
                "ct": ct,
                "body_head": body,
            })

        page.on("response", on_response)

        print(f"→ 導航 {HOME_URL}")
        try:
            await page.goto(HOME_URL, timeout=45000, wait_until="domcontentloaded")
        except Exception as e:
            print(f"goto error: {e}")
        await page.wait_for_timeout(8000)

        # 嘗試點進籃球 → 美國職籃
        for text in ("籃球", "美國職籃", "NBA"):
            try:
                loc = page.get_by_text(text, exact=False).first
                await loc.click(timeout=5000)
                print(f"  ✓ 點擊 {text!r}")
                await page.wait_for_timeout(6000)
            except Exception as e:
                print(f"  ✗ 點擊 {text!r} 失敗: {str(e)[:80]}")

        print(f"\n當前 URL: {page.url}")
        print(f"\n=== 攔截 {len(captured)} 個請求（只列 JSON）===")
        for c in captured:
            if "json" in c["ct"]:
                print(f"\n[{c['method']}] {c['url']} → {c['status']}")
                print(f"  BODY: {c['body_head']!r}")

        out = BASE_DIR / "data" / "probe_api_change.json"
        out.write_text(json.dumps(captured, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n💾 完整結果存於 {out}")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

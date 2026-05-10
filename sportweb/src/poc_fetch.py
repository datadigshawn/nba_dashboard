"""
POC v2：攔截 XHR + WebSocket 取得運彩 NBA 賠率
"""
import asyncio
import json
import os
from datetime import datetime
from playwright.async_api import async_playwright

NBA_URL = "https://www.sportslottery.com.tw/sportsbook/sport/%E7%B1%83%E7%90%83/%E7%BE%8E%E5%9C%8B/%E7%BE%8E%E5%9C%8B%E8%81%B7%E7%B1%83/34801.1"
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "poc")
os.makedirs(OUT_DIR, exist_ok=True)


async def main():
    xhr_captured = []
    ws_frames = []
    request_payloads = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="zh-TW",
            viewport={"width": 1440, "height": 900},
        )
        page = await ctx.new_page()

        async def on_response(resp):
            url = resp.url
            ct = resp.headers.get("content-type", "")
            if ("application/json" in ct or ".json" in url.lower()) and "/locales/" not in url:
                try:
                    body = await resp.json()
                    req = resp.request
                    try:
                        req_body = req.post_data
                    except Exception:
                        req_body = None
                    xhr_captured.append({
                        "url": url,
                        "method": req.method,
                        "status": resp.status,
                        "request": req_body,
                        "response": body,
                    })
                    if req_body:
                        request_payloads.append({"url": url, "method": req.method, "body": req_body})
                except Exception:
                    pass

        def on_websocket(ws):
            print(f"  [WS] 連線: {ws.url}")
            ws.on("framereceived", lambda payload: ws_frames.append({
                "dir": "recv", "ws_url": ws.url, "len": len(payload),
                "preview": (payload[:500] if isinstance(payload, str) else str(payload[:500])),
            }))
            ws.on("framesent", lambda payload: ws_frames.append({
                "dir": "sent", "ws_url": ws.url, "len": len(payload),
                "preview": (payload[:500] if isinstance(payload, str) else str(payload[:500])),
            }))

        page.on("response", lambda r: asyncio.create_task(on_response(r)))
        page.on("websocket", on_websocket)

        print(f"→ 導航到 {NBA_URL}")
        try:
            await page.goto(NBA_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"⚠️  goto: {e}")

        print(f"→ 等待 20 秒讓 WebSocket 建立 + 接收資料")
        await page.wait_for_timeout(20000)

        title = await page.title()
        print(f"→ 頁面標題: {title}")

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        with open(os.path.join(OUT_DIR, f"xhr_{ts}.json"), "w", encoding="utf-8") as f:
            json.dump(xhr_captured, f, ensure_ascii=False, indent=2)
        with open(os.path.join(OUT_DIR, f"ws_{ts}.json"), "w", encoding="utf-8") as f:
            json.dump(ws_frames, f, ensure_ascii=False, indent=2)
        with open(os.path.join(OUT_DIR, f"reqs_{ts}.json"), "w", encoding="utf-8") as f:
            json.dump(request_payloads, f, ensure_ascii=False, indent=2)

        print(f"\n✅ 攔截 XHR: {len(xhr_captured)} 個")
        print(f"✅ WS frames: {len(ws_frames)} 個")
        print(f"✅ POST bodies: {len(request_payloads)} 個")
        print(f"📦 outputs @ {OUT_DIR}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

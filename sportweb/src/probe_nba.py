"""
探測 NBA 比賽資料的正確 content type
導航到 NBA 頁並記錄所有發出的訂閱請求 / XHR，尤其是 focontentid=22064 的使用
"""
import asyncio
import json
import os
from datetime import datetime
from playwright.async_api import async_playwright

NBA_URL = "https://www.sportslottery.com.tw/sportsbook/sport/%E7%B1%83%E7%90%83/%E7%BE%8E%E5%9C%8B/%E7%BE%8E%E5%9C%8B%E8%81%B7%E7%B1%83/34801.1"
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "debug")
os.makedirs(OUT_DIR, exist_ok=True)


async def main():
    all_requests = []   # 所有 POST request（包含完整 body）
    ws_frames = []

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

        async def on_request(req):
            if req.method == "POST" and "sportslottery.com.tw" in req.url:
                all_requests.append({
                    "url": req.url,
                    "body": req.post_data,
                })

        def on_ws(ws):
            def recv(p):
                if isinstance(p, str):
                    ws_frames.append({"dir": "recv", "payload": p})
            def sent(p):
                if isinstance(p, str):
                    ws_frames.append({"dir": "sent", "payload": p})
            ws.on("framereceived", recv)
            ws.on("framesent", sent)

        page.on("request", lambda r: asyncio.create_task(on_request(r)))
        page.on("websocket", on_ws)

        print("→ 首頁通過 CF")
        await page.goto("https://www.sportslottery.com.tw/sportsbook/sport", wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)

        print("→ 到 NBA 頁")
        try:
            await page.goto(NBA_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"  {e}")
        await page.wait_for_timeout(20000)

        # 截圖看現在頁面狀態
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        await page.screenshot(path=os.path.join(OUT_DIR, f"nba_state_{ts}.png"), full_page=True)

        await browser.close()

    # 列出所有 POST
    print(f"\n=== 所有 POST requests (共 {len(all_requests)}) ===")
    unique = {}
    for r in all_requests:
        body = r["body"] or ""
        try:
            parsed = json.loads(body)
            cid = parsed.get("contentId", {})
            key = f"{cid.get('type','?')} / {cid.get('id','?')}"
            unique[key] = unique.get(key, 0) + 1
        except Exception:
            key = f"[raw] {r['url'][-60:]}"
            unique[key] = unique.get(key, 0) + 1
    for k, cnt in sorted(unique.items(), key=lambda x: -x[1]):
        print(f"  x{cnt}  {k}")

    # 列出 WS sent subscriptions
    print(f"\n=== WS 送出的訊息 ({sum(1 for f in ws_frames if f['dir']=='sent')}) ===")
    for f in ws_frames:
        if f["dir"] != "sent":
            continue
        p = f["payload"]
        if len(p) < 30:
            continue
        try:
            if p.startswith("["):
                arr = json.loads(p)
                for m in arr:
                    parsed = json.loads(m) if isinstance(m, str) else m
                    print(f"  sent: {json.dumps(parsed, ensure_ascii=False)[:300]}")
            else:
                print(f"  sent raw: {p[:200]}")
        except Exception:
            print(f"  sent (err): {p[:200]}")

    # 列出大的 recv 訊息
    print(f"\n=== WS 收到的大訊息 (>1KB) ===")
    for f in ws_frames:
        if f["dir"] != "recv":
            continue
        p = f["payload"]
        if len(p) < 1000:
            continue
        print(f"  recv {len(p)}B: {p[:300]}")

    with open(os.path.join(OUT_DIR, f"probe_{ts}.json"), "w", encoding="utf-8") as f:
        json.dump({"requests": all_requests, "ws": ws_frames}, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    asyncio.run(main())

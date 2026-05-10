"""
階段一 v3：讓瀏覽器正常導航 NBA 頁，攔截所有 content/get 回應
"""
import asyncio
import json
import os
from datetime import datetime
from playwright.async_api import async_playwright

HOMEPAGE = "https://www.sportslottery.com.tw/sportsbook/sport"
NBA_URL = "https://www.sportslottery.com.tw/sportsbook/sport/%E7%B1%83%E7%90%83/%E7%BE%8E%E5%9C%8B/%E7%BE%8E%E5%9C%8B%E8%81%B7%E7%B1%83/34801.1"
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
os.makedirs(OUT_DIR, exist_ok=True)


def walk_gamegroups(node, games, path=""):
    """遞迴尋找所有 gamegroups"""
    if not isinstance(node, dict):
        return
    name = node.get("name", "")
    current_path = f"{path} > {name}" if path else name
    for gg in node.get("gamegroups", []) or []:
        games.append({"path": current_path, "gamegroup": gg})
    for child in node.get("bonavigationnodes", []) or []:
        walk_gamegroups(child, games, current_path)


async def main():
    xhr_captured = []
    ws_frames_all = []  # 完整 WS 訊息

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
            if "/services/content/get" not in resp.url:
                return
            try:
                body = await resp.json()
                req_body = resp.request.post_data
                xhr_captured.append({
                    "url": resp.url,
                    "request": json.loads(req_body) if req_body else None,
                    "response": body,
                })
            except Exception:
                pass

        def on_websocket(ws):
            print(f"   [WS] 連線 {ws.url}")
            def recv(payload):
                if isinstance(payload, str):
                    ws_frames_all.append({"dir": "recv", "ws": ws.url, "payload": payload})
            def sent(payload):
                if isinstance(payload, str):
                    ws_frames_all.append({"dir": "sent", "ws": ws.url, "payload": payload})
            ws.on("framereceived", recv)
            ws.on("framesent", sent)

        page.on("response", lambda r: asyncio.create_task(on_response(r)))
        page.on("websocket", on_websocket)

        print("→ 1/3 首頁通過 Cloudflare")
        await page.goto(HOMEPAGE, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(5000)
        print(f"   首頁載入完成,累計 XHR: {len(xhr_captured)}")

        print("→ 2/3 導航到 NBA 頁")
        try:
            await page.goto(NBA_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"   ⚠️  {e}")
        print("→ 3/3 等待 20 秒接收 NBA 資料")
        await page.wait_for_timeout(20000)
        print(f"   累計 XHR: {len(xhr_captured)}")

        await browser.close()

    # 解析 WS 訊息
    ws_parsed = []
    for f in ws_frames_all:
        p = f["payload"]
        if not p or p in ("o", "h"):
            continue
        if f["dir"] == "sent":
            try:
                ws_parsed.append({"dir": "sent", "content": json.loads(p)})
            except Exception:
                ws_parsed.append({"dir": "sent", "raw": p[:200]})
            continue
        # 伺服器訊息格式: a["{...}"]
        if p.startswith("a"):
            try:
                outer = json.loads(p[1:])
                for inner in outer:
                    ws_parsed.append({"dir": "recv", "content": json.loads(inner)})
            except Exception:
                pass

    # 分析所有 content/get 回應
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    raw_path = os.path.join(OUT_DIR, f"nba_api_{ts}.json")
    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump({"xhr": xhr_captured, "ws": ws_parsed}, f, ensure_ascii=False, indent=2)

    print(f"\n=== 所有 content/get 呼叫 ===")
    nba_related = []
    for x in xhr_captured:
        req = x["request"] or {}
        cid = req.get("contentId", {})
        resp_data = x["response"].get("data") if isinstance(x["response"], dict) else None
        size = len(json.dumps(resp_data, ensure_ascii=False)) if resp_data else 0
        cid_id = cid.get("id", "")
        cid_type = cid.get("type", "")

        # 標記 NBA 相關
        is_nba = "34801" in cid_id or "34765" in cid_id or "美國" in cid_id
        marker = " 🏀" if is_nba else ""
        print(f"  {size:>7}B  type={cid_type:25s} id={cid_id}{marker}")

        if is_nba and size > 100:
            nba_related.append(x)

    # 從 WS 訊息找 NBA 資料
    print(f"\n=== WebSocket 訊息 ({len(ws_parsed)} 筆) ===")
    nba_ws_content = []
    for w in ws_parsed:
        if w["dir"] != "recv":
            continue
        content = w.get("content", {})
        data_list = content.get("data", [])
        if not isinstance(data_list, list):
            continue
        for item in data_list:
            if not isinstance(item, dict):
                continue
            cid = item.get("contentId", {})
            cid_id = cid.get("id", "")
            change = item.get("change", {})
            size = len(json.dumps(change, ensure_ascii=False)) if change else 0
            is_nba = "34801" in cid_id or "34765" in cid_id
            marker = " 🏀" if is_nba else ""
            if size > 200 or is_nba:
                print(f"  {size:>7}B  type={cid.get('type','')} id={cid_id}{marker}")
            if is_nba and size > 100:
                nba_ws_content.append(item)

    # 解析 NBA 節點（含 XHR 與 WS）
    all_games = []
    for x in nba_related:
        data = x["response"].get("data")
        if isinstance(data, dict):
            walk_gamegroups(data, all_games)
    for item in nba_ws_content:
        change = item.get("change")
        if isinstance(change, dict):
            walk_gamegroups(change, all_games)

    print(f"\n🏀 找到 {len(all_games)} 個 gamegroup")
    for i, g in enumerate(all_games[:5]):
        gg = g["gamegroup"]
        keys = list(gg.keys())[:15] if isinstance(gg, dict) else []
        print(f"\n  [{i}] {g['path']}")
        print(f"      gamegroup keys: {keys}")
        # 嘗試顯示關鍵欄位
        for k in ["idfwgamegroup", "name", "startdate", "gamestages", "teams", "games"]:
            if k in gg:
                v = gg[k]
                if isinstance(v, list):
                    print(f"      {k}: list[{len(v)}]")
                else:
                    print(f"      {k}: {str(v)[:100]}")

    print(f"\n💾 原始資料: {raw_path}")


if __name__ == "__main__":
    asyncio.run(main())

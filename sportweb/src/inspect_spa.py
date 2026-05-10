"""
SPA Inspector — 等 JS 跑完 + 監控 API 請求，抓真實渲染後的 DOM

執行：
    cd /Users/shawnclaw/autobot/sportWeb
    .venv/bin/python src/inspect_spa.py
    HEADED=1 .venv/bin/python src/inspect_spa.py  # 看瀏覽器

產出：
    data/nba_spa_rendered.html   JS 跑完後的 DOM
    data/api_requests.json        所有 XHR/fetch 紀錄（含 response）
    data/spa_analysis.json        結構分析
"""
import asyncio
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_FILE = BASE_DIR / "data" / "cf_state.json"
HTML_OUT = BASE_DIR / "data" / "nba_spa_rendered.html"
API_OUT = BASE_DIR / "data" / "api_requests.json"
ANALYSIS_OUT = BASE_DIR / "data" / "spa_analysis.json"

NBA_URL = (
    "https://www.sportslottery.com.tw/sportsbook/sport/"
    "%E7%B1%83%E7%90%83/%E7%BE%8E%E5%9C%8B/%E7%BE%8E%E5%9C%8B%E8%81%B7%E7%B1%83/34801.1"
)

HEADED = os.environ.get("HEADED", "0") == "1"


async def main():
    if not STATE_FILE.exists():
        print(f"❌ 需先跑 bootstrap.py"); sys.exit(1)

    api_log = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=not HEADED, channel="chrome",
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

        # 監控所有 API 回應
        async def on_response(resp):
            try:
                url = resp.url
                if any(seg in url for seg in ["/services/", "/api/", "graphql", "content/get"]):
                    ct = resp.headers.get("content-type", "")
                    body = None
                    if "json" in ct:
                        try:
                            body = await resp.json()
                        except Exception:
                            pass
                    api_log.append({
                        "url": url,
                        "status": resp.status,
                        "method": resp.request.method,
                        "post_data": resp.request.post_data,
                        "resp_size": len(str(body)) if body else 0,
                        "resp_sample": json.dumps(body, ensure_ascii=False)[:500] if body else None,
                    })
            except Exception:
                pass

        page.on("response", lambda r: asyncio.create_task(on_response(r)))

        print(f"→ 載入 NBA 頁（mode: {'headed' if HEADED else 'headless'}）")
        await page.goto(NBA_URL, wait_until="domcontentloaded", timeout=30000)

        # 等 JS + API 跑完（逐步等）
        for phase in [5, 10, 15]:
            print(f"→ 等 {phase} 秒（累計觀察 API 請求）")
            await page.wait_for_timeout(phase * 1000)
            print(f"   目前 API log: {len(api_log)} 筆")

        # 最後嘗試滾動觸發 lazy load
        await page.mouse.wheel(0, 500)
        await page.wait_for_timeout(3000)

        html = await page.content()
        HTML_OUT.write_text(html, encoding="utf-8")
        print(f"📄 rendered HTML: {len(html):,} bytes → {HTML_OUT}")

        # 分析
        analysis = {
            "html_size_kb": round(len(html) / 1024, 1),
            "api_calls": len(api_log),
            "team_found": any(kw in html for kw in ["湖人", "塞提克", "勇士", "Lakers"]),
            "odds_found": bool(re.search(r'\d\.\d{2}', html)),
            "market_found": any(kw in html for kw in ["不讓分", "讓分", "大/小"]),
        }

        # top class 出現
        tokens = []
        for c in re.findall(r'class="([^"]+)"', html):
            tokens.extend(c.split())
        analysis["top_classes"] = Counter(tokens).most_common(30)

        # data-*
        analysis["data_attrs"] = sorted(set(
            m for m, _ in re.findall(r'(data-[a-z][a-z0-9-]*)="([^"]{1,50})"', html)
        ))

        ANALYSIS_OUT.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")

        API_OUT.write_text(
            json.dumps(api_log, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"📡 API requests: {API_OUT} ({len(api_log)} 筆)")
        print(f"🔍 分析: {ANALYSIS_OUT}")

        print("\n" + "=" * 60)
        print("  摘要")
        print("=" * 60)
        for k, v in analysis.items():
            if isinstance(v, list):
                print(f"  {k}: (see file)")
            else:
                print(f"  {k}: {v}")

        print("\n  API 呼叫前 10 筆：")
        for r in api_log[:10]:
            url_tail = r["url"][-80:]
            print(f"    [{r['status']}] {r['method']} ...{url_tail}  size={r['resp_size']}")

        print("\n  top classes：")
        for cls, cnt in analysis["top_classes"][:15]:
            print(f"    [{cnt:>3}] {cls}")

        print("\n  data-* attrs：")
        for a in analysis["data_attrs"][:20]:
            print(f"    {a}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

"""
DOM Inspector — 載入 NBA 頁，把完整 HTML 存檔分析 CSS selector

執行：
    cd /Users/shawnclaw/autobot/sportWeb
    .venv/bin/python src/inspect_dom.py

產出：
    data/nba_page_source.html   完整頁面 HTML
    data/dom_analysis.json       分析結果（candidate selectors）
    data/screenshot.png          截圖（額外參考）
"""
import asyncio
import json
import re
import sys
from collections import Counter
from pathlib import Path

from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_FILE = BASE_DIR / "data" / "cf_state.json"
HTML_OUT = BASE_DIR / "data" / "nba_page_source.html"
PNG_OUT = BASE_DIR / "data" / "screenshot.png"
ANALYSIS_OUT = BASE_DIR / "data" / "dom_analysis.json"

NBA_URL = (
    "https://www.sportslottery.com.tw/sportsbook/sport/"
    "%E7%B1%83%E7%90%83/%E7%BE%8E%E5%9C%8B/%E7%BE%8E%E5%9C%8B%E8%81%B7%E7%B1%83/34801.1"
)


async def main():
    if not STATE_FILE.exists():
        print(f"❌ 找不到 {STATE_FILE}，請先跑 bootstrap.py")
        sys.exit(1)

    import os
    HEADED = os.environ.get("HEADED", "0") == "1"
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=not HEADED,
            channel="chrome",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        ctx = await browser.new_context(
            storage_state=str(STATE_FILE),
            locale="zh-TW",
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = await ctx.new_page()

        print(f"→ 載入 NBA 頁面")
        await page.goto(NBA_URL, wait_until="domcontentloaded", timeout=30000)
        print("→ 等 networkidle（讓 JS 載完）")
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        await page.wait_for_timeout(5000)  # 給 JS 再喘一下

        # 1. 存完整 HTML
        html = await page.content()
        HTML_OUT.write_text(html, encoding="utf-8")
        print(f"📄 HTML 存至 {HTML_OUT} ({len(html):,} bytes)")

        # 2. 截圖
        await page.screenshot(path=str(PNG_OUT), full_page=True)
        print(f"🖼️ 截圖存至 {PNG_OUT}")

        # 3. 分析 DOM
        analysis = await analyze_dom(page, html)
        ANALYSIS_OUT.write_text(
            json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"🔍 分析存至 {ANALYSIS_OUT}")

        # 4. 印出重點
        print("\n" + "=" * 60)
        print("  DOM 分析重點")
        print("=" * 60)
        for k, v in analysis.get("summary", {}).items():
            print(f"  {k}: {v}")

        print("\n  常見 class 名（出現次數 top 20）：")
        for cls, count in analysis.get("top_classes", [])[:20]:
            print(f"    [{count:>4}] {cls}")

        print("\n  data-* attributes（可能是 ID/key）：")
        for attr, sample in analysis.get("data_attrs", [])[:15]:
            print(f"    {attr}  (例: {sample})")

        print("\n  含球隊名的元素（取前 10 個）：")
        for item in analysis.get("team_elements", [])[:10]:
            print(f"    <{item['tag']} class='{item['class'][:60]}'>")
            print(f"      text: {item['text'][:60]}")

        print("\n  含賠率數字的元素（取前 10 個）：")
        for item in analysis.get("odds_elements", [])[:10]:
            print(f"    <{item['tag']} class='{item['class'][:60]}'>  → {item['text']}")

        await browser.close()


async def analyze_dom(page, html: str) -> dict:
    """用 page.evaluate 查詢 DOM，也用 HTML regex 做靜態分析。"""
    result = {"summary": {}, "top_classes": [], "data_attrs": [],
              "team_elements": [], "odds_elements": []}

    # ─── Regex 靜態分析 ───────────────────────────
    # 1. 所有 class 出現次數
    classes = re.findall(r'class="([^"]+)"', html)
    all_class_tokens = []
    for c in classes:
        all_class_tokens.extend(c.split())
    top_classes = Counter(all_class_tokens).most_common(30)
    result["top_classes"] = top_classes

    # 2. data-* attributes
    data_attrs = re.findall(r'(data-[a-z][a-z0-9-]*)="([^"]{1,60})"', html)
    by_attr = {}
    for attr, val in data_attrs:
        by_attr.setdefault(attr, val)  # 只留第一個樣本
    result["data_attrs"] = sorted(by_attr.items())

    # ─── DOM Query 動態分析 ───────────────────────
    # 3. 找含球隊名的元素
    TEAM_KEYWORDS = [
        "湖人", "塞提克", "塞爾提克", "勇士", "獨行俠", "快艇", "鵜鶘",
        "太陽", "熱火", "黃蜂", "馬刺", "拓荒者", "金塊", "尼克",
        "魔術", "76人", "騎士", "活塞", "公鹿",
        "Lakers", "Celtics", "Warriors", "Suns", "Heat"
    ]

    team_elements = await page.evaluate("""
    (keywords) => {
        const results = [];
        const seen = new Set();
        keywords.forEach(kw => {
            const xp = `//*[text()[contains(., '${kw}')]]`;
            const iter = document.evaluate(xp, document, null, XPathResult.ORDERED_NODE_ITERATOR_TYPE, null);
            let node = iter.iterateNext();
            let count = 0;
            while (node && count < 3) {
                const key = (node.tagName || '') + ':' + (node.className || '').slice(0, 50);
                if (!seen.has(key)) {
                    seen.add(key);
                    results.push({
                        tag: node.tagName,
                        class: typeof node.className === 'string' ? node.className : '',
                        text: (node.textContent || '').trim().slice(0, 100),
                    });
                }
                count++;
                node = iter.iterateNext();
            }
        });
        return results;
    }
    """, TEAM_KEYWORDS)
    result["team_elements"] = team_elements

    # 4. 找賠率（小數格式 x.yz 或 x.y）
    odds_elements = await page.evaluate("""
    () => {
        const results = [];
        const seen = new Set();
        // 找文字只含小數賠率的元素（不是很長的段落）
        const all = document.querySelectorAll('*:not(script):not(style)');
        for (const el of all) {
            const text = (el.textContent || '').trim();
            // 像賠率的字串：單純數字 1.56 / 2.10 / 10.50 等
            if (/^\\d{1,2}\\.\\d{1,2}$/.test(text)) {
                const key = el.className + ':' + text;
                if (!seen.has(key)) {
                    seen.add(key);
                    results.push({
                        tag: el.tagName,
                        class: typeof el.className === 'string' ? el.className : '',
                        text: text,
                    });
                    if (results.length >= 30) break;
                }
            }
        }
        return results;
    }
    """)
    result["odds_elements"] = odds_elements

    # ─── Summary ─────────────────────────────────
    result["summary"] = {
        "page_title": await page.title(),
        "total_elements": len(await page.locator("*").all()) if False else "(skip)",
        "html_size_kb": round(len(html) / 1024, 1),
        "unique_classes": len(set(all_class_tokens)),
        "team_elements_found": len(team_elements),
        "odds_elements_found": len(odds_elements),
        "has_cf_challenge": "cf-challenge" in html.lower() or "turnstile" in html.lower(),
    }

    return result


if __name__ == "__main__":
    asyncio.run(main())

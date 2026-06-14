"""
Bootstrap：一次性通過 Cloudflare Turnstile，把 storage_state 存下來。

兩種模式：
  --wait N : 開瀏覽器後等 N 秒自動存檔（你在旁邊視需要點 checkbox）
  (default): 等你按 Enter 才存檔（傳統 interactive 模式）

執行：
    cd /Users/shawnclaw/autobot/investing/sports/autobots_NBA/sportweb
    .venv/bin/python src/bootstrap.py --wait 90
    .venv/bin/python src/bootstrap.py              # interactive

⚠️ 只在 CF **沒有過熱封鎖**時跑，避免頻繁觸發 Turnstile。
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_FILE = BASE_DIR / "data" / "cf_state.json"

# 熱身用 marketgroup ID（與 fetcher 一致）
WARMUP_MG_ID = "60067.1"


async def warmup_api(page, mg_id: str, max_tries: int = 5, wait_s: int = 10) -> bool:
    """
    在 page 的 JS context 打一次 /services/content/get，
    確認新 CF cookie 能走 API（而不只是導航頁）。
    失敗時等 wait_s 秒再試，最多 max_tries 次。
    """
    js = """async (id) => {
        try {
            const r = await fetch('/services/content/get', {
                method: 'POST',
                credentials: 'include',
                headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
                body: JSON.stringify({
                    contentId: {type: 'marketGroup', id},
                    clientContext: {language: 'ZH', ipAddress: '0.0.0.0'}
                })
            });
            const text = await r.text();
            return {status: r.status, ok: r.ok, snippet: text.slice(0, 120)};
        } catch (e) { return {err: String(e)}; }
    }"""
    for i in range(1, max_tries + 1):
        res = await page.evaluate(js, mg_id)
        if "err" in res:
            print(f"  [warmup {i}/{max_tries}] JS error: {res['err']}")
        else:
            snippet = res.get("snippet", "")
            cf_blocked = "<!doctype" in snippet.lower() or "just a moment" in snippet.lower()
            if res.get("ok") and not cf_blocked:
                print(f"  ✅ warmup 通過（第 {i} 次，status={res['status']}）")
                return True
            print(f"  [warmup {i}/{max_tries}] status={res.get('status')} cf_blocked={cf_blocked} snippet={snippet[:60]!r}")
        await page.wait_for_timeout(wait_s * 1000)
    return False

HOMEPAGE = "https://www.sportslottery.com.tw/sportsbook/sport"
NBA_URL = (
    "https://www.sportslottery.com.tw/sportsbook/sport/"
    "%E7%B1%83%E7%90%83/%E7%BE%8E%E5%9C%8B/%E7%BE%8E%E5%9C%8B%E8%81%B7%E7%B1%83/34801.1"
)


async def main(wait_seconds: int | None = None):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    mode = f"auto-wait {wait_seconds}s" if wait_seconds else "interactive"
    print(f"🌐 開啟瀏覽器到台灣運彩網站（模式：{mode}）")
    print("   如果出現 Cloudflare checkbox，請手動勾選")
    print()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        ctx = await browser.new_context(
            locale="zh-TW",
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await ctx.new_page()

        # 1. 先進首頁
        print("→ 1/3 進入首頁")
        await page.goto(HOMEPAGE, wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)

        # 2. 導航到 NBA
        print("→ 2/3 導航到 NBA 頁（可能出現 CF Turnstile）")
        await page.goto(NBA_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        # 3. 等使用者確認
        print()
        print("─" * 60)
        print("📌 請在瀏覽器視窗確認：")
        print("   1. 沒有 Cloudflare checkbox 擋著（如有請勾選）")
        print("   2. 頁面能正常顯示 NBA 比賽清單（至少看到球隊名）")
        print("─" * 60)

        if wait_seconds:
            # 聰明等待：每 3 秒檢查 CF 是否通過
            # 通過標準：頁面 title 不含「請稍候/Just a moment」且 HTML 含球隊字詞
            print(f"⏳ 最多等 {wait_seconds} 秒，每 3 秒檢查一次 CF 是否過關...")
            elapsed = 0
            passed = False
            while elapsed < wait_seconds:
                try:
                    title = await page.title()
                    html = await page.content()
                except Exception:
                    title = ""
                    html = ""
                is_cf = (
                    "請稍候" in title or "Just a moment" in title.lower()
                    or "cf-challenge" in html.lower()
                    or "turnstile" in html.lower()
                )
                has_content = any(kw in html for kw in [
                    "湖人", "塞提克", "勇士", "Lakers", "Celtics",
                    "不讓分", "讓分", "大/小"
                ])
                if not is_cf and has_content:
                    passed = True
                    print(f"  ✅ 已過 CF（{elapsed}s）— 偵測到球隊/市場字詞")
                    break
                print(f"  [{elapsed:>3}s] title={title[:30]!r} is_cf={is_cf} has_content={has_content}")
                await page.wait_for_timeout(3000)
                elapsed += 3

            if not passed:
                print(f"⚠️ 等了 {wait_seconds} 秒 CF 仍在，state 還是會存（但可能無效）")
        else:
            input("按 Enter 儲存 state... ")

        # 3.5 熱身 API — 新 CF cookie 對 API endpoint 不一定立刻被信任，
        #     這邊先打一次 marketgroup 讓 cookie 累積足夠 trust score
        print("→ 熱身 marketgroup API（確保 cookie 可打 API）")
        warmed = await warmup_api(page, WARMUP_MG_ID, max_tries=5, wait_s=10)
        if not warmed:
            print("⚠️ warmup 5 次都失敗，state 還是會存（fetcher 可能要多重試）")

        # 4. 存 cookies + localStorage
        await ctx.storage_state(path=str(STATE_FILE))
        print(f"✅ storage state 已存到 {STATE_FILE}")

        # 5. 印出部分資訊方便 debug
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
        print(f"   cookies: {len(state.get('cookies', []))} 個")
        print(f"   origins: {len(state.get('origins', []))} 個 domain 有 localStorage")

        await browser.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=int, default=None,
                    help="auto-save after N seconds (non-interactive)")
    args = ap.parse_args()
    asyncio.run(main(wait_seconds=args.wait))

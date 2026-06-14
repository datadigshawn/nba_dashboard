"""
Fetcher v2 — 透過 page.evaluate fetch API 抓 NBA 賠率（API-based，非 DOM）

前提：
    已跑過 bootstrap.py，data/cf_state.json 存在。

流程：
    1. 用 storage_state 啟 Playwright Chrome
    2. 導航到運彩任一頁讓 CF 生效
    3. page.evaluate 裡 fetch /services/content/get
       contentId.type = "marketGroup"
       contentId.id   = "60067.1"  (NBA Main)
    4. 把 JSON 丟給 api_parser 解析成 OddsSnapshot

執行：
    cd /Users/shawnclaw/autobot/investing/sports/autobots_NBA/sportweb
    .venv/bin/python src/fetcher.py           # headless
    .venv/bin/python src/fetcher.py --headed  # 看瀏覽器

輸出：
    data/odds_YYYYMMDD_HHMM.json
    data/latest_odds.json
"""
import argparse
import asyncio
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "src"))

from api_parser import parse_market_group, parse_event_markets, MARKETGROUP_IDS
from schema import OddsSnapshot

STATE_FILE = BASE_DIR / "data" / "cf_state.json"
HOME_URL = "https://www.sportslottery.com.tw/sportsbook/sport"


async def fetch_marketgroup(page, mg_id: str) -> dict:
    """在 page 的 JS 環境裡 fetch marketGroup API。"""
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
            return {status: r.status, text: await r.text()};
        } catch (e) { return {err: String(e)}; }
    }"""
    res = await page.evaluate(js, mg_id)
    if "err" in res:
        return {"error": res["err"]}
    try:
        return {"status": res["status"], "data": json.loads(res["text"])}
    except Exception as e:
        return {"error": f"JSON parse: {e}", "raw": res["text"][:500]}


async def fetch_event_detail(page, event_id: str) -> dict:
    """Fetch full event data (includes spreads/totals) via type=event."""
    js = """async (id) => {
        try {
            const r = await fetch('/services/content/get', {
                method: 'POST',
                credentials: 'include',
                headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
                body: JSON.stringify({
                    contentId: {type: 'event', id},
                    clientContext: {language: 'ZH', ipAddress: '0.0.0.0'}
                })
            });
            return {status: r.status, text: await r.text()};
        } catch (e) { return {err: String(e)}; }
    }"""
    res = await page.evaluate(js, event_id)
    if "err" in res:
        return {"error": res["err"]}
    try:
        return {"status": res["status"], "data": json.loads(res["text"])}
    except Exception as e:
        return {"error": f"JSON parse: {e}"}


async def fetch_once(headed: bool = False) -> OddsSnapshot:
    snap = OddsSnapshot.now(league="NBA", source_url=HOME_URL)

    if not STATE_FILE.exists():
        snap.error = f"missing {STATE_FILE}, run bootstrap first"
        snap.cf_passed = False
        return snap

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=not headed,
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

        try:
            await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            snap.error = f"navigation: {e}"
            snap.cf_passed = False
            await browser.close()
            return snap

        # 等 CF 通過（最多 30 秒）
        cf_passed = False
        for wait_s in range(0, 30, 3):
            try:
                title = await page.title()
                html = await page.content()
            except Exception:
                title = html = ""
            is_cf = (
                "請稍候" in title or "just a moment" in title.lower()
                or "cf-challenge" in html.lower()
                or len(html) < 5000  # CF 頁很短
            )
            if not is_cf:
                cf_passed = True
                print(f"  ✓ CF 過關（等了 {wait_s}s）")
                break
            await page.wait_for_timeout(3000)

        if not cf_passed:
            snap.error = "CF challenge not passed in 30s"
            snap.cf_passed = False
            await browser.close()
            return snap

        # 抓 NBA Main marketgroup
        result = await fetch_marketgroup(page, MARKETGROUP_IDS["nba_main"])

        if "error" in result or result.get("status") != 200:
            snap.error = f"marketgroup fetch: {result}"
            snap.cf_passed = False
        else:
            games = parse_market_group(result["data"])
            snap.games = games

            # 抓每場比賽的 event-level 資料（讓分 + 大小分）
            for g in games:
                if not g.game_id:
                    continue
                try:
                    ev = await fetch_event_detail(page, g.game_id)
                    if ev.get("status") == 200 and "data" in ev:
                        parse_event_markets(ev["data"], existing=g)
                except Exception as e:
                    print(f"  [warn] event {g.game_id} detail: {e}")

            # 更新 storage（延長 cookie 壽命）
            try:
                await ctx.storage_state(path=str(STATE_FILE))
            except Exception:
                pass

        await browser.close()

    return snap


def _is_cf_related(snap: OddsSnapshot) -> bool:
    """判斷失敗是否因為 CF cookie 失效。"""
    if snap.cf_passed:
        return False
    err = (snap.error or "").lower()
    markers = [
        "just a moment",
        "cf challenge",
        "cf-challenge",
        "turnstile",
        "doctype",
        f"missing {STATE_FILE}".lower(),
    ]
    return any(m in err for m in markers)


def run_bootstrap_subprocess(wait_seconds: int = 120) -> bool:
    """呼叫 bootstrap.py 重新取得 CF cookie。回傳是否成功。

    需求：
      - Mac mini 有登入、螢幕沒鎖（會開 headed Chrome）
      - launchd 以使用者 agent 身份跑時符合此條件
    """
    py = BASE_DIR / ".venv" / "bin" / "python"
    boot = BASE_DIR / "src" / "bootstrap.py"
    if not py.exists() or not boot.exists():
        print(f"❌ bootstrap 檔案不存在：{py} / {boot}", file=sys.stderr)
        return False

    print(f"⟲ 啟動 bootstrap --wait {wait_seconds}s（headed，Mac mini 需螢幕可用）")
    try:
        r = subprocess.run(
            [str(py), str(boot), "--wait", str(wait_seconds)],
            capture_output=True,
            text=True,
            timeout=wait_seconds + 60,
            cwd=str(BASE_DIR),
        )
    except subprocess.TimeoutExpired:
        print("❌ bootstrap 逾時", file=sys.stderr)
        return False
    except Exception as e:
        print(f"❌ bootstrap 例外：{e}", file=sys.stderr)
        return False

    out = r.stdout.strip()
    if out:
        print(out)
    if r.stderr.strip():
        print(f"(stderr) {r.stderr.strip()}", file=sys.stderr)

    return "已過 CF" in out and STATE_FILE.exists()


def save_snapshot(snap: OddsSnapshot) -> Path:
    BASE_DIR.joinpath("data").mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    p_ts = BASE_DIR / "data" / f"odds_{ts}.json"
    p_latest = BASE_DIR / "data" / "latest_odds.json"
    body = json.dumps(snap.to_dict(), ensure_ascii=False, indent=2)
    p_ts.write_text(body, encoding="utf-8")
    p_latest.write_text(body, encoding="utf-8")
    return p_ts


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    ap.add_argument(
        "--no-auto-bootstrap",
        action="store_true",
        help="disable auto-rebootstrap fallback when CF blocks",
    )
    ap.add_argument(
        "--bootstrap-wait",
        type=int,
        default=120,
        help="seconds to wait during auto-bootstrap (default: 120)",
    )
    args = ap.parse_args()

    print(f"[{datetime.now().isoformat(timespec='seconds')}] 開始抓取 NBA Main marketgroup...")
    snap = await fetch_once(headed=args.headed)

    # 自動恢復：CF cookie 失效時呼叫 bootstrap 重建 state，再重試一次
    if snap.error and _is_cf_related(snap) and not args.no_auto_bootstrap:
        print(f"⚠️ 偵測到 CF 失效：{(snap.error or '')[:150]}")
        ok = run_bootstrap_subprocess(wait_seconds=args.bootstrap_wait)
        if ok:
            print("↻ bootstrap 成功，重試抓取...")
            snap = await fetch_once(headed=args.headed)
        else:
            print("⚠️ bootstrap 失敗，放棄本輪")

    if snap.error:
        print(f"❌ 失敗: {snap.error}")
    else:
        print(f"✅ 取得 {len(snap.games)} 場比賽")
        for g in snap.games:
            ml = g.moneyline.to_dict() if g.moneyline else {}
            spreads = len(g.spreads)
            totals = len(g.totals)
            print(f"  [{g.game_id}] {g.away} @ {g.home}")
            print(f"    ML: {ml}  | spreads: {spreads}  | totals: {totals}")
            if g.moneyline:
                prob = g.implied_prob()
                if prob:
                    print(f"    隱含機率: away {prob['away_norm']*100:.1f}% / home {prob['home_norm']*100:.1f}% (vig {prob['overround']*100:.1f}%)")

    path = save_snapshot(snap)
    print(f"💾 存於 {path}")

    # Write to SQLite
    try:
        from sport_db import init_db as _db_init, insert_snapshot, DB_PATH as _db
        _db_init(_db)
        sid = insert_snapshot(_db, snap.to_dict())
        if sid:
            print(f"📊 寫入 DB snapshot #{sid} ({len(snap.games)} 場)")
    except Exception as _e:
        print(f"[warn] DB write failed: {_e}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())

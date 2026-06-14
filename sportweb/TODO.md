# TODO — 下次繼續

> 👉 **明天開始直接看 `CONTINUE_TOMORROW.md`**（本檔案是舊的技術備忘）

## 🚨 當前狀態（2026-04-15 23:35 更新）

- Cloudflare **Turnstile checkbox** 可能仍在封鎖期（14:40 觸發）
- **Phase A + C 已完成**（環境、schema、parser 骨架、bootstrap/fetcher 程式）
- 等 CF 退溫 → 跑 `python src/bootstrap.py` 進入 Phase 2

## 🎯 核心問題

如何取得 NBA 比賽清單與賠率？

API 結構解碼了，但：
- 直接打 API 一律 403（CF 擋）
- Playwright 的 `page.evaluate(fetch())` 同源也 403
- `context.request.post()` 帶完整 headers 也 403
- 連讓瀏覽器自己點擊 UI 都被 CF Turnstile 擋住

## 📊 今日進度（2026-04-15）

### ✅ 2026-04-15 上午
- POC 證明 Cloudflare 可通過（首次嘗試）
- API 請求/回應格式完整解碼
- 找到 NBA 在運彩的 ID 對照（`34801.1`、`focontentid=22064`）
- 確認 NBA 目前有 2 場開賣比賽
- 找到籃球底下所有 11 個美國聯盟
- 找到所有 27 個國際聯盟

### ✅ 2026-04-15 下午~晚上（Phase A + C）
- 專案 clone 到 `/Users/shawnclaw/autobot/investing/sports/autobots_NBA/sportweb`
- 本地 `.venv` 建立，Playwright + Chromium 驗證
- 5 張截圖（IMG_7268~7274）分析完成
- 產出 `DOM_STRUCTURE.md`：推斷出 3 場比賽、4 種 market、完整資料模型
- 產出 `src/schema.py`：dataclass + 30 個隊名中英對照 + 隱含機率算法
- 產出 `src/parser.py`：解析器骨架（CSS selector 待 DevTools 驗證）
- 產出 `src/bootstrap.py`：一次性過 CF Turnstile 拿 storage_state
- 產出 `src/fetcher.py`：使用 storage_state 的自動化版
- 所有模組 import 測試通過

### ❌ 卡關
| 嘗試 | 結果 |
|------|------|
| 測試 17 種 content type × 4 種 ID = 68 次組合 | 全部 403 |
| `page.evaluate(fetch())` 同源請求 | 403 + CF challenge HTML |
| `ctx.request.post()` 加完整 headers | 403 |
| 讓瀏覽器自然點擊 UI | CF Turnstile 升級擋住 |

### 💡 關鍵發現
- 主 API URL 是 `https://www.sportslottery.com.tw/services/content/get`（同源）
- 另一個 `www-talo-ssb-pr.sportslottery.com.tw/services/content/get` 是 PR 環境
- WebSocket 訂閱端點：`velnt-talo-ssb-pr.sportslottery.com.tw/notification/listen/.../websocket`
- **每個 API POST 都會被 CF 獨立 challenge**（即使 page 已過關）
- CF 在多次嘗試後升級為 Turnstile checkbox（人類驗證）

## 🚀 下一步候選方案（按推薦排序）

### 🥇 方案 1：storage_state cookie 重用 + DOM 文字擷取（最推薦）

**核心思路**：頁面只要能渲染，資料就在 DOM 裡是純文字，根本不用 OCR。

```python
# 第一次（手動過 CF Turnstile）
async with async_playwright() as p:
    browser = await p.chromium.launch(headless=False)
    ctx = await browser.new_context()
    page = await ctx.new_page()
    await page.goto("https://www.sportslottery.com.tw/sportsbook/sport/.../34801.1")
    # ⏸ 暫停等使用者勾 CF checkbox
    input("過完 CF 按 Enter...")
    # ✅ 存 cookie + storage
    await ctx.storage_state(path="cf_state.json")

# 之後每次（全自動，幾天/幾週都有效）
async with async_playwright() as p:
    browser = await p.chromium.launch(headless=True)  # 可 headless
    ctx = await browser.new_context(storage_state="cf_state.json")
    page = await ctx.new_page()
    await page.goto(NBA_URL)
    # 直接抓 DOM 文字
    games = await page.locator(".game-card").all()
    for g in games:
        away = await g.locator(".team-away").inner_text()
        odds = await g.locator(".odds-moneyline").inner_text()
```

**優點**：
- 精度 100%（直接讀字串，非影像）
- 速度快（無圖像處理）
- 可從 `data-*` attribute 拿 game_id、event_id
- cookie 換新只需手動 1 分鐘（每幾天/週）

**前置工作**：
- 用 DevTools 確認 NBA 頁的 CSS selector
  （比賽卡片 / 主隊 / 客隊 / 各種賠率欄位）

### 🥈 方案 2：playwright-stealth + 真 Chrome
```bash
pip install playwright-stealth
```
```python
from playwright_stealth import stealth_async
browser = await p.chromium.launch(channel="chrome", headless=False)
await stealth_async(page)
```
- 加上 stealth 反指紋
- 用系統真 Chrome 而非 Chromium
- 預估 10 分鐘可驗證
- **缺點**：CF 持續升級，stealth 也可能被識破

### 🥉 方案 3：mitmproxy 抓真瀏覽器流量
```bash
pip install mitmproxy
mitmdump -s dump_xhr.py
```
- macOS 用 mitmproxy 當代理
- 真 Chrome 開運彩，手動點到 NBA
- 看完整 HTTP 流量找正確 API pattern
- 預估 30 分鐘

### 📌 方案 4：OCR 截圖（最終備案，**不推薦**）

**只在前三方案都失敗才考慮。**

理由（為什麼不該用 OCR）：

| 問題 | 影響 |
|------|------|
| **精度** | `1.56` 易誤判為 `1.86` / `1.66`，賠率錯 0.1 就盈虧反轉 |
| **結構** | 需用像素位置對應「哪個賠率屬哪個玩法」 |
| **維護** | UI 改版（字體/配色/版面）整個 pipeline 要重訓 |
| **延遲** | 每場 1~3 秒，賠率可能已過期 |
| **無 ID** | 拿不到 game_id，難跟 autobots_NBA 對接 |

**真的需要 OCR 的場景**（運彩都不符合）：
1. 網站 DOM 文字加密混淆 ❌
2. 內容是 canvas/img 畫的 ❌
3. 連登入都要人臉/SMS 驗證 ❌

> 結論：運彩沒有任何一條符合，所以**根本用不到 OCR**。
> 方案 1 的「DOM 文字擷取」幾乎是 OCR 的所有優點 + 無缺點。

## ⏭️ 取得資料後的後續

一旦能穩定取得 gamegroups/marketgroups：

- [ ] 寫 `parser.py` 把原始 JSON 轉成目標 schema
- [ ] 寫 `storage.py` 存 JSON + CSV
- [ ] 寫 `run_fetch.py` 主入口
- [ ] 加排程（cron 或 hermes）每 30 分鐘跑一次
- [ ] 跟 `autobots_NBA` 對接：「預測勝率 - 賠率隱含機率 > 閾值」→ 正期望值投注提示

## 🛠️ 技術備忘

### 已知 ID 對照表
| ID | 名稱 | 說明 |
|----|------|------|
| `1355/top` | 頂層 | 所有運動 |
| `1355/34765.1` | 籃球 | sport |
| `1355/34800.1` | 美國 | 籃球 > 美國（11 聯盟） |
| `1355/34801.1` | **美國職籃 (NBA)** | `focontentid=22064`，目前 2 場 |
| `1355/35008.1` | 國際 | 籃球 > 國際（27 聯盟） |

### 已試過的 content type（全 403）
```
eventList, fwevent, fwEventList, fwGameList, fwGameGroupList,
gameList, gameGroupList, marketGroupList, marketList,
tournamentEventList, upcomingEvents, matchList, fixture, fixtureList,
gamegroups, marketgroups, fwmarketgrouplist, events, tournament, league
```

### 環境
```bash
cd /Users/shawnclaw/autobot/investing/sports/autobots_NBA/sportweb
source .venv/bin/activate
# Python 3.12+, playwright + Chromium（已安裝）
```

### 程式檔案
| 檔案 | 用途 | 狀態 |
|------|------|------|
| **`src/schema.py`** | **資料模型 + 隊名對照** | **✅ 2026-04-15 新增**|
| **`src/parser.py`** | **DOM 解析器骨架** | **⚠️ CSS selector 待驗證** |
| **`src/bootstrap.py`** | **一次性過 CF Turnstile** | **✅ 2026-04-15 新增** |
| **`src/fetcher.py`** | **自動化抓取（用 storage_state）** | **✅ 2026-04-15 新增** |
| `src/poc_fetch.py` | 最早 POC（CF 通過驗證） | 歷史檔 |
| `src/probe_nba.py` | 探測 NBA 頁所有請求 | 歷史檔 |
| `src/debug_page.py` | 截圖 + DOM debug | 歷史檔 |
| `src/try_content_types.py` | 方案 B：loop 試 content type | 歷史檔（已證實失敗）|
| `src/click_to_nba.py` | 方案 A：UI 點擊 | 歷史檔 |
| `src/fetch_nba_games.py` | 早期嘗試（已被 try_content_types 取代） | 歷史檔 |

# sportWeb — 運彩賠率擷取系統

目標：從台灣運彩網站擷取 NBA（以及其他運動）的賭盤賠率，供 `autobots_NBA` 預測系統比對「預測勝率 vs 市場隱含機率」。

---

## 📌 當前狀態（2026-04-15）

**階段**：POC 探索中，**尚未產出可用資料**。

- ✅ Cloudflare 反爬蟲**已通過**（Playwright 有效）
- ✅ API 請求/回應格式**已解碼**
- ✅ 導航樹結構**已釐清**（籃球 → 美國 → NBA）
- ❌ **還無法取得實際比賽 gamegroups / marketgroups 資料**

---

## 🎯 系統目標架構（尚未實作）

```
sportWeb/
├── config/
│   └── targets.json        # 要抓哪些聯盟（NBA、MLB…）
├── src/
│   ├── fetcher.py          # 主抓取邏輯（Playwright）
│   ├── parser.py           # 原始 JSON → 結構化 odds
│   └── storage.py          # 存 JSON / CSV
├── data/
│   ├── odds_YYYYMMDD_HHMM.json    # 快照
│   └── latest_odds.csv            # 最新一批
└── scripts/
    └── run_fetch.py        # 排程進入點
```

### 預期輸出格式（對接 autobots_NBA）

```json
{
  "fetched_at": "2026-04-15T10:30:00+08:00",
  "league": "NBA",
  "games": [
    {
      "game_id": "3459714",
      "away": "Phoenix Suns",
      "home": "LA Lakers",
      "start_time": "2026-04-11T10:30:00",
      "moneyline": {"away": 1.56, "home": 1.88},
      "spreads": [
        {"line": 1.5, "away": 1.63, "home": 1.80}
      ],
      "totals": [
        {"line": 126.5, "over": 1.70, "under": 1.73}
      ]
    }
  ]
}
```

---

## 🔍 已知技術結構

### 網站架構
- **前端**：SPA，走 Cloudflare（`cf-mitigated: challenge`）
- **資料 API**：`https://www-talo-ssb-pr.sportslottery.com.tw/services/content/get`
- **即時推送**：SockJS over WebSocket，端點 `velnt-talo-ssb-pr.sportslottery.com.tw/notification/listen/.../websocket`

### API 請求格式
```json
POST https://www-talo-ssb-pr.sportslottery.com.tw/services/content/get
Content-Type: application/json

{
  "contentId": {"type": "boNavigationList", "id": "1355/top"},
  "clientContext": {"language": "ZH", "ipAddress": "0.0.0.0"}
}
```

### 已知 content types
| type | 用途 | 備註 |
|------|------|------|
| `boNavigationList` | 導航樹（運動/聯盟/比賽） | 可取得 ID 清單 |
| `boNavigationPath` | 麵包屑 | 僅路徑 |
| `bannerCategoryList` | 頁面橫幅 | 不重要 |
| `dbCoreDCParameter` | 系統參數 | 不重要 |
| `liveStreamEventList` | 直播清單 | 賽事影音 |

### 已知 ID 對照
| ID | 中文名 | 說明 |
|----|--------|------|
| `1355/top` | 頂層 | 所有運動 |
| `1355/34765.1` | 籃球 sport | 根籃球節點 |
| `1355/34800.1` | 美國 | 籃球 > 美國（11 個聯盟） |
| `1355/34801.1` | **美國職籃** | **NBA**，`focontentid=22064` |
| `1355/36151.1` | NBA盃 | |
| `1355/36772.1` | 美國女子職籃 | WNBA |
| `1355/35009.1` | 歐洲籃球聯賽 | Euroleague |
| `1355/美國` | 美國（別名） | 用中文名也可以請求 |

### WebSocket 訂閱流程
```
1. 瀏覽器 POST /services/content/subscribe  {contentId, subscriberId}
2. WS 連線建立後傳: {"subscriberId":null,"versionList":[],"clientContext":{...}}
3. 伺服器 push SockJS frames: a["{...json...}"]
4. 格式: {data: [{contentId, changeType: "refreshed", change: {...}}]}
```

---

## ⚠️ 當前卡關點

### 問題描述
導航到 NBA URL (`/sportsbook/sport/藍球/美國/美國職籃/34801.1`) 後：
- `content/get` 只回傳 **boNavigationPath**（麵包屑，59B）
- WebSocket 訂閱 `1355/美國` 但推送的 change 為 **0B**（空）
- 主頁面渲染為空白

### 推論

NBA 的 **實際 game 資料用另一個 content type**，可能是：
- `eventList` + `focontentid=22064`
- `marketGroupList`
- `fwgameList`
- 或其他未知類型

需進一步探測。

### 已試過（都失敗）
- 直接 `page.evaluate(fetch(...))` → CORS 擋
- `page.request.post()` → Cloudflare 403（非瀏覽器 JS context）
- 直接打 `1355/34801.1` → 回傳沒 gamegroups 的空殼

---

## 📋 下一步候選方案

### 方案 A：模擬點擊 UI（推薦）
讓 Playwright 用真實滑鼠點擊「籃球 → 美國 → 美國職籃」，讓前端正常觸發它自己的 loader 邏輯。

### 方案 B：嘗試不同 content type
針對 `focontentid=22064` 試幾種可能的 type：
- `eventList`
- `gameList`
- `marketGroupList`
- `fwevent`
- `fwtournament`

### 方案 C：MITM Proxy 抓真瀏覽器流量
最直接但需另裝 mitmproxy。用真 Chrome 手動瀏覽一次並抓完整 HTTP 流量。

### 方案 D：逆向 JS bundle
下載 SPA 的 JS bundle，搜尋 `content/get` 的呼叫點，看前端用什麼邏輯組 contentId。

---

## 📦 目前檔案說明

### 程式碼（`src/`）
| 檔案 | 用途 | 狀態 |
|------|------|------|
| `poc_fetch.py` | 最早的 POC，證明 Cloudflare 可通過 | ✅ 歷史檔 |
| `fetch_nba_games.py` | 嘗試抓 NBA 比賽清單 | ❌ 目前回傳空 |
| `probe_nba.py` | 探測 NBA 頁面所有請求 | ✅ 探索用 |
| `debug_page.py` | 截圖 + 抓 DOM debug | ✅ 探索用 |

### 原始資料（`data/`）
| 檔案 | 內容 |
|------|------|
| `poc/xhr_*.json` | 最早 POC 的完整 XHR 與 WS 擷取 |
| `nba_api_*.json` | 導航到 NBA 時的完整請求/回應 |
| `debug/bball_xhr_*.json` | 籃球頁的完整 XHR（含 11 個美國聯盟列表） |
| `debug/probe_*.json` | NBA 頁的完整 POST/WS 流量 |
| `debug/*.png` | 對應時點的頁面截圖 |

---

## 🛠️ 開發環境

```bash
cd /Users/shawnclaw/autobot/investing/sports/autobots_NBA/sportweb
source .venv/bin/activate     # 本地 venv（已建立）
# playwright + chromium 已裝（重建時：pip install -r requirements.txt）
```

### 執行範例
```bash
# 探測 NBA 頁所有請求（會開瀏覽器視窗）
python src/probe_nba.py

# 抓籃球所有聯盟與 event 數量
python src/debug_page.py
```

---

## 📅 工作紀錄

| 日期 | 進度 |
|------|------|
| 2026-04-15 | 建立專案、POC 通過 Cloudflare、解碼 API 格式、找到 NBA ID (34801.1) 有 2 場比賽，但還無法取得具體 game/odds 資料 |

---

## 🔗 相關專案

- `/Users/shawnclaw/autobot/autobots_NBA` — NBA 勝負機率預測系統（資料消費方）

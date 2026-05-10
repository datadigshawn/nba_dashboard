# 🏗️ sportWeb 系統架構（Phase 3 完整版）

> 從原始資料 → 預測模型 → 市場對比 → 邊際推播 的完整資料流

---

## 📐 全系統資料流

```
┌──────────────────────────────────────────────────────────────────┐
│                       原始資料源                                   │
├──────────────────────────────────────────────────────────────────┤
│                                                                    │
│    ESPN API                    台灣運彩                           │
│  ┌────────────┐          ┌──────────────────────┐                │
│  │ 賽程        │          │ www.sportslottery.   │                │
│  │ 球隊戰績     │          │   com.tw/sportsbook  │                │
│  │ 近 90 天     │          │  （Cloudflare 守衛） │                │
│  │ 歷史比賽     │          └─────────┬────────────┘                │
│  └────┬───────┘                    │                              │
│       │                             │  ⚠️ CF Turnstile             │
└───────┼─────────────────────────────┼──────────────────────────────┘
        │                             │
        ▼                             ▼
┌────────────────────┐   ┌────────────────────────────────┐
│  autobots_NBA      │   │   sportWeb（本專案）            │
│                    │   │                                │
│  nba_predictor.py  │   │  ┌──────────────────────┐      │
│  ┌──────────────┐  │   │  │ bootstrap.py         │      │
│  │ Elo 評分     │  │   │  │ (一次過 CF 手動)      │      │
│  │ K=20,+100主場 │  │   │  └──────────┬───────────┘      │
│  └──────┬───────┘  │   │             ▼                  │
│         ▼          │   │  ┌──────────────────────┐      │
│  ┌──────────────┐  │   │  │ data/cf_state.json   │      │
│  │ XGBoost      │  │   │  │ (cookies + storage)  │      │
│  │ 18 特徵回歸   │  │   │  └──────────┬───────────┘      │
│  │ 勝率 + 讓分   │  │   │             ▼                  │
│  └──────┬───────┘  │   │  ┌──────────────────────┐      │
│         ▼          │   │  │ fetcher.py           │      │
│  nba_data.json     │   │  │ (headless 自動抓)     │      │
│  ┌──────────────┐  │   │  └──────────┬───────────┘      │
│  │{games: [...], │  │   │             ▼                  │
│  │ home_prob,    │  │   │  ┌──────────────────────┐      │
│  │ away_prob,    │  │   │  │ parser.py            │      │
│  │ pred_spread,  │  │   │  │ DOM → OddsSnapshot   │      │
│  │ pred_total}   │  │   │  └──────────┬───────────┘      │
│  └──────┬───────┘  │   │             ▼                  │
└─────────┼──────────┘   │  ┌──────────────────────┐      │
          │              │  │ data/latest_odds.json│      │
          │              │  │ {games: [{           │      │
          │              │  │   game_id, away,     │      │
          │              │  │   home,              │      │
          │              │  │   moneyline: {...},  │      │
          │              │  │   spreads: [...],    │      │
          │              │  │   totals: [...]      │      │
          │              │  │ }]}                  │      │
          │              │  └──────────┬───────────┘      │
          │              └─────────────┼──────────────────┘
          │                            │
          └────────┬───────────────────┘
                   ▼
       ┌─────────────────────────────────────┐
       │   sportWeb/src/edge_detector.py     │
       │                                      │
       │  1. 隊名對照 (to_espn_name)         │
       │     中文 → ESPN 英文（149 條映射）   │
       │                                      │
       │  2. 比賽配對（home + away 英文名）   │
       │                                      │
       │  3. 去 vig 計算隱含機率              │
       │     market_p = (1/odds) / total      │
       │                                      │
       │  4. Edge = model_p - market_p        │
       │                                      │
       │  5. Kelly Criterion 倉位大小          │
       │     f = (p*(b+1) - 1) / b            │
       │                                      │
       │  6. Expected ROI 預期報酬            │
       │     EV = p*(odds-1) - (1-p)          │
       │                                      │
       │  過濾：Edge > threshold (預設 5%)    │
       └──────────┬──────────────────────────┘
                  │
                  ▼
       ┌──────────────────────────┐
       │  輸出                      │
       ├──────────────────────────┤
       │  • 終端機列表               │
       │  • JSON（--json）           │
       │  • Telegram 推播（--push）  │
       │    → @whale9527_bot 或     │
       │      新開 @edgeAlert_bot   │
       └──────────────────────────┘
```

---

## 🎯 核心組件

### 1. Scraping Pipeline（sportWeb 本身）

| 檔案 | 職責 | 狀態 |
|------|------|------|
| `bootstrap.py` | 手動過 CF Turnstile → 存 storage_state | ✅ 寫好（需手動執行）|
| `fetcher.py` | 載入 state + 自動抓 NBA 頁 | ✅ 骨架完成 |
| `parser.py` | DOM → OddsSnapshot | ⚠️ CSS selector 待 DevTools 驗證 |
| `schema.py` | dataclass + 30 隊名映射 + 機率計算 | ✅ 完成 |

### 2. Prediction Pipeline（autobots_NBA）

| 組件 | 產出 |
|------|------|
| Elo 系統 | 1500 起始、K=20、主場 +100 |
| XGBoost 勝率模型 | 18 特徵 → 勝率 |
| XGBoost 讓分模型 | 18+3 特徵 → pred_spread |
| `nba_data.json` | 每日 09:00 launchd 更新 |

### 3. Integration Layer（sportWeb/edge_detector）

從兩邊拉資料，計算三件事：

| 指標 | 公式 | 意義 |
|------|------|------|
| **隱含機率** | `1/odds ÷ (1/ml_a + 1/ml_h)` | 去除莊家抽水（vig）後的真實市場機率 |
| **Edge** | `model_prob - market_prob` | 我方模型比市場樂觀多少 |
| **Kelly 比例** | `(p*(b+1) - 1) / b` | 最適資金下注比例（b = odds - 1） |
| **Expected ROI** | `p*(odds-1) - (1-p)` | 每 $1 下注的預期回報 |

---

## 📊 資料格式規範

### sportWeb → data/latest_odds.json

```json
{
  "fetched_at": "2026-04-16T10:30:00+08:00",
  "league": "NBA",
  "games": [
    {
      "game_id": "346",
      "away": "鳳凰城太陽",
      "home": "洛杉磯湖人",
      "start_time": "2026-04-17T10:30:00",
      "moneyline": {"away": 1.56, "home": 1.88},
      "spreads": [
        {"line": 1.5, "away": 1.63, "home": 1.80},
        {"line": 2.5, "away": 1.48, "home": 1.95}
      ],
      "totals": [
        {"line": 126.5, "over": 1.70, "under": 1.73}
      ]
    }
  ]
}
```

### autobots_NBA → nba_data.json（已存在）

```json
{
  "games": [
    {
      "away": "Phoenix Suns",
      "home": "Los Angeles Lakers",
      "home_prob": 72.5,
      "away_prob": 27.5,
      "pred_spread": 5.4,
      "pred_total": 232.0,
      "home_record": "48-34",
      "away_record": "40-42"
    }
  ]
}
```

### edge_detector → stdout / JSON / Telegram

```json
{
  "generated_at": "2026-04-16T10:35:00",
  "min_edge": 0.05,
  "count": 2,
  "edges": [
    {
      "game_id": "346",
      "away": "Phoenix Suns",
      "home": "Los Angeles Lakers",
      "side": "home",
      "picked_team": "Los Angeles Lakers",
      "model_prob": 0.725,
      "market_prob": 0.510,
      "odds": 1.88,
      "edge": 0.215,
      "kelly": 0.39,
      "expected_roi": 0.362
    }
  ]
}
```

---

## ⏱️ 執行時序

```
時間          動作                                     產出
──────────────────────────────────────────────────────────────
每日 09:00   autobots_NBA launchd 跑                  → nba_data.json
   ↓
每 30 分鐘   sportWeb fetcher（排程中，未設）         → latest_odds.json
   ↓        （fetcher 用已存的 cf_state 自動執行）
每 15 分鐘   edge_detector 對比兩者                   → Telegram alert
```

### 排程設計（待建）

```xml
<!-- sportWeb fetcher 每 30 分鐘 -->
<key>StartInterval</key>
<integer>1800</integer>

<!-- edge_detector 每 15 分鐘（跑完 fetcher 後 15 分） -->
<key>StartCalendarInterval</key>
<array>
  <dict><key>Minute</key><integer>7</integer></dict>
  <dict><key>Minute</key><integer>22</integer></dict>
  <dict><key>Minute</key><integer>37</integer></dict>
  <dict><key>Minute</key><integer>52</integer></dict>
</array>
```

---

## 🚦 當前進度（2026-04-16）

| Phase | 狀態 | 描述 |
|-------|------|------|
| 1. 環境建置 | ✅ | venv + Playwright + Chromium |
| 2. DOM 分析 | ✅ | 5 張截圖分析完成，schema 定稿 |
| 3. 程式骨架 | ✅ | bootstrap/fetcher/parser/edge_detector |
| 4. 隊名映射 | ✅ | 30 隊 × 149 條映射 |
| 5. CF 突破 | ⏸️ | 等遠端 + Mac mini 環境齊全時執行 bootstrap |
| 6. CSS selector 驗證 | ⏸️ | 需先 bootstrap 成功 |
| 7. 真實資料測試 | ⏸️ | 等 5 + 6 完成 |
| 8. 排程化 | ⏸️ | 等 7 穩定 |
| 9. 對接 Telegram | ✅ | edge_detector 的 push 邏輯已寫好 |
| 10. 儀表板整合 | ⏸️ | 可以加到 quantSignal_clone 的 tab |

---

## 🔒 風險與備案

| 風險 | 影響 | 備案 |
|------|------|------|
| CF Turnstile 升級 | 無法過 bootstrap | 手動+playwright-stealth，或 mitmproxy 抓真 Chrome 流量 |
| 運彩網站改版 | CSS selector 失效 | parser.py 集中 selectors dict，改版只改一處 |
| 開賽時間對不上 | edge_detector 配對失敗 | 用 start_time ± 2 小時 fuzzy match |
| 隊名翻譯缺漏 | 某場比賽被跳過 | to_espn_name fallback 返回原字串，不會 crash |
| Telegram rate limit | 推播失敗 | edge_detector 內建 top 10 截斷 + retry |

---

## 💡 未來延伸

1. **多聯盟支援** — 目前 NBA only，可加 WNBA / Euroleague / MLB
2. **Spreads / Totals 也做 edge** — 目前只比 moneyline，讓分 / 大小分也有套利空間
3. **歷史回測** — 累積 edge 訊號的實盤勝率，校準模型信心
4. **自動下注**（⚠️ 謹慎）— 運彩不支援 API，但可整合 DraftKings / Polymarket
5. **WebSocket 實時** — 運彩有 WS 推送（vel nt-talo-ssb-pr），即時更新賠率

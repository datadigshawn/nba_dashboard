# DOM 結構分析（從 IMG_7268~7274 推斷）

> 這是從 5 張手機/桌面截圖推斷出來的頁面結構，**實際 CSS selector 仍需 DevTools 驗證**。
> 但資料模型（dataclass）已經確定，可以先寫 parser 骨架。

---

## 🎯 已識別的 3 場比賽（2026-04-11）

| 顯示 ID | 客隊 | 主隊 | 開賽時間 |
|---------|-----|-----|---------|
| `335` | New Orleans Pelicans（紐奧良鵜鶘）| Boston Celtics（波士頓塞爾提克）| 07:30 |
| `318` | 獨行俠（達拉斯）| 尼克（紐約）| - |
| `346` | Phoenix Suns（鳳凰城太陽）| LA Lakers（洛杉磯湖人）| 10:30 |

顯示 ID（3 位數）跟 API 的 `focontentid`（如 22064）**不一樣**。前者是玩家看到的短碼，後者是內部 ID。

---

## 📐 頁面 Layout（桌面版）

```
┌─── 頂 nav ──────────────────────────────────────────────────────┐
│  [賽事表] [串關中心] [即時比分] [運動動態] [會員專區] [最新消息]    │
└───────────────────────────────────────────────────────────────┘
┌─ 左側 ─┐┌────── 比賽主區 ─────────────────┐┌──── 右側投注單 ──┐
│ 搜尋   ││  New Orleans  07:30   Boston    ││  投注單(N)        │
│ 熱門   ││                                 ││                  │
│ 場中   ││  [熱門玩法][單節][半場][其他]     ││  塞提克 -5.5 1.62│
│ 日期   ││                                 ││  345 多倫多猛龍   │
│ ...    ││  ▼ 不讓分            客場 主場 ││                  │
│ 運動列 ││  (moneyline)     X / 2.55 1.20  ││                  │
│ [籃球] ││                                 ││  鵜鶘 +17.5 1.63 │
│ [棒球] ││  ▼ 讓分 -17.5                   ││  335 紐奧良鵜鶘   │
│ ...    ││    鵜鶘+17.5 1.63 | 塞提克-17.5 1.80│                  │
│        ││                                 ││                  │
│        ││  ▼ 讓分 -16.5                   ││                  │
│        ││  ▼ 讓分 -15.5                   ││                  │
│        ││                                 ││                  │
│        ││  ▼ 大 / 小 120.5 etc            ││                  │
└────────┘└─────────────────────────────────┘└──────────────────┘
```

---

## 🎲 每場比賽的 4 個 market 類別

### Market 類型（從 IMG_7274 Lakers vs Suns 看得最清楚）

| market | 中文 | 子條目 | 賠率結構 |
|--------|------|-------|---------|
| **moneyline** | 不讓分 | 無（就 1 條）| `{away: 1.56, home: 1.88}` |
| **spread** | 讓分 | 多條（±1.5 / ±2.5 / ±3.5）| 每條 `{line, away: odds, home: odds}` |
| **total** | 大 / 小 | 多條（126.5 / 120.5）| 每條 `{line, over: odds, under: odds}` |
| **其他玩法** | 第一節、半場、讓球半場... | 略（先不做） | - |

---

## 🎯 待確認的 CSS selector（需 DevTools 一次性調查）

這些是 DevTools 開啟後要用 **Elements 面板** 找的：

| 元素 | 猜測 selector | 我需要的資料 |
|------|--------------|-------------|
| 比賽卡片容器 | `[data-event-id]` / `[class*="event"]` / `article` | 每場一個容器 |
| game 顯示編號 | 卡片左側紅色 badge `335`, `318`, `346` | 用於對照 NBA 預測的隊伍 |
| 主 / 客隊名稱 | `[class*="team-away"]` / `[class*="team-home"]` | 中英文名（中文為主）|
| 比賽時間 | `[class*="time"]` / 中央大字 `07:30` | HH:MM |
| market 群組 | `[class*="market"]` / 可展開區塊 | 每個玩法一塊 |
| market 標題 | `不讓分` / `讓分 -17.5` / `大/小 120.5` | 含 line |
| odds 按鈕 | `button[class*="odd"]` / `[role="button"]` | 內文是數字 `1.63` |
| 當前已選 / hover | `[aria-selected="true"]` / `[class*="selected"]` | 不必要但可選 |

---

## 📊 目標資料 schema（已定案，可先寫 parser）

```python
from dataclasses import dataclass, field
from typing import Literal

@dataclass
class OddsLine:
    line: float            # 讓分/總分的 line，如 17.5、120.5；moneyline = 0
    away: float | None     # 客隊賠率（moneyline/spread 用）
    home: float | None     # 主隊賠率
    over: float | None     # 總分大（only total）
    under: float | None    # 總分小（only total）

@dataclass
class GameOdds:
    game_id: str                  # e.g. "335"
    away: str                     # 客隊中文/英文名
    home: str                     # 主隊
    start_time: str               # ISO 格式
    moneyline: OddsLine            # 不讓分
    spreads: list[OddsLine]        # 讓分（多條）
    totals: list[OddsLine]         # 大/小（多條）
    raw_html_snippet: str | None = None  # debug 用

@dataclass
class OddsSnapshot:
    fetched_at: str               # ISO
    league: str                   # "NBA"
    games: list[GameOdds]
    source_url: str               # 截圖當時的 URL
```

這個 schema 符合 README 目標輸出，跟 autobots_NBA 對接時可算：
```python
implied_prob_away = 1 / game.moneyline.away   # 0.641 for 1.56
implied_prob_home = 1 / game.moneyline.home   # 0.568 for 1.88
# 減掉 overround 後得到真實機率，再跟我們的 XGBoost 模型比
```

---

## 🔍 從截圖看出的幾個細節

### 1. Odds 精度
所有賠率都是 **2 位小數**（1.63、1.80、1.56）。直接字串轉 float 無損。

### 2. Line 格式
- 讓分：`+17.5` / `-17.5`（小數點 .0 或 .5）
- 總分：`120.5`、`126.5`（小數點 .5，少數整數）

### 3. 中英混用
- 有些隊名用中文（紐奧良鵜鶘、波士頓塞爾提克、達拉斯獨行俠）
- 有些用英文（New Orleans、Boston、Phoenix、LA Lakers）
- **需要 mapping 表**把中文轉成 ESPN 英文名對照 autobots_NBA

### 4. 主客位置
- **左邊 = 客場**（away）
- **右邊 = 主場**（home）
- 跟 autobots_NBA 一致（away @ home）

### 5. 讓分方向
- spread `-17.5` 跟 `+17.5` 是同一條 line 的兩端
- 主隊讓 `-17.5` = 主隊讓 17.5 分，贏 >17.5 才算贏
- autobots_NBA 的 `pred_spread` 正數 = 主隊贏 X 分，**方向一致**

---

## ❓ 還沒從截圖看出的資訊

| 項目 | 如何取得 |
|------|---------|
| game_id 跟 ESPN game_id 的對照 | 用隊名 + 日期做 fuzzy match |
| 實際 CSS class 名稱 | 需用 DevTools 看 DOM |
| 賠率的 `data-*` 屬性（可能有 odds_id） | 需用 DevTools |
| 更新頻率 | 需長時間監控 WebSocket frame |

---

## 📁 下一步的程式骨架（不觸發 CF 可以先寫）

1. `src/schema.py` — 本文件的 dataclass
2. `src/parser.py` — 接受 page HTML 或 Playwright locator，產出 `OddsSnapshot`（selector 先用 placeholder）
3. `src/bootstrap.py` — 一次性過 CF Turnstile 拿 `storage_state.json`（需手動互動）
4. `src/fetcher.py` — 自動 fetch + parse（等 storage_state 拿到才能跑）

等 CF 退溫 + 做完 bootstrap 後，把 parser.py 的 selector 改為真的即可。

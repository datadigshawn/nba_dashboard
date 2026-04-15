# autobots_NBA — 工作紀錄

> 建立日期：2026-04-12
> 負責人：datadigshawn

---

## 一、專案概覽

**NBA 比賽預測系統**，以 ESPN 公開資料為基礎，結合 Elo 評分與 XGBoost 機器學習模型，預測每日 NBA 比賽勝負、讓分（Spread）與大小分（Total），並與 Polymarket 博彩市場對比找出邊際機會。

### 核心功能
- 每日自動拉取 ESPN 賽程、球隊戰績、近期比賽結果
- Elo 動態評分（主場優勢 +100 分，K=20）
- XGBoost 迴歸模型預測得分差 → 轉換為勝率
- Spread（讓分）專用模型
- 與 Polymarket 即時博彩市場對比，計算 Kelly 建議比例
- Flask 儀表板即時呈現（`/nba`）

---

## 二、資料夾結構

```
autobots_NBA/
├── nba_predictor.py       # 主程式（1329行）
├── nba.html               # 儀表板頁面（Flask 提供服務）
├── nba_data.json          # 今日預測靜態快取
├── nba_daily_update.sh    # 每日更新腳本
├── WORKLOG.md             # 本檔案
└── state/
    ├── nba_model.json     # Elo 評分 + XGBoost 元資料
    ├── nba_model.xgb      # XGBoost 主模型（二進位）
    ├── nba_spread_model.xgb   # Spread 讓分模型（二進位）
    └── nba_spread_model.json  # Spread 模型特徵清單
```

---

## 三、系統架構

```
ESPN API ──→ fetch_espn_scoreboard()   今日賽程
         ──→ fetch_espn_standings()    球隊戰績（勝率/PPG/OPPG/Diff）
         ──→ fetch_espn_results()      近N天歷史比賽（建立 Elo）

Polymarket ──→ fetch_polymarket_nba()  活躍 NBA 博彩市場

                    ↓
         EloSystem（動態評分）
                    ↓
         NBAPredictor（XGBoost + Elo 混合）
         SpreadPredictor（Spread 專用 XGBoost）
                    ↓
         find_edges()（邊際偵測 + Kelly 建議）
                    ↓
    JSON 輸出 → nba_data.json / /api/nba/predictions
                    ↓
              nba.html 儀表板呈現
```

---

## 四、模型說明

### 4.1 Elo 評分系統（EloSystem）

| 參數 | 數值 | 說明 |
|------|------|------|
| 初始 Elo | 1500 | 所有球隊初始值 |
| K 值 | 20 | 每場比賽 Elo 更新幅度 |
| 主場優勢 | +100 Elo | 約等於 +3.5 分讓分 |
| 歷史範圍 | 近 90 天 | 約 600+ 場比賽 |

### 4.2 NBAPredictor（主模型）

| 項目 | 說明 |
|------|------|
| 演算法 | XGBoost Regression（`reg:squarederror`） |
| 預測目標 | 主隊得分差（home_score - away_score） |
| 勝率轉換 | Normal CDF：`P = 0.5 * (1 + erf(margin / (RMSE * √2)))` |
| 訓練輪數 | 150 rounds |
| max_depth | 5 |

**18 個特徵：**

| 特徵 | 重要度排名 |
|------|-----------|
| elo_diff | 1（最重要） |
| win_pct_diff | 2 |
| elo_a / elo_b | 3-4 |
| ppg_a / ppg_b | 5-6 |
| oppg_a / oppg_b | 7-8 |
| diff_a / diff_b | 9-10 |
| streak_a / streak_b | 11-12 |
| b2b_adv | 13 |
| b2b_a / b2b_b | 14-15 |
| both_b2b | 16 |
| win_pct_a / win_pct_b | 17-18 |

### 4.3 SpreadPredictor（讓分模型）

| 項目 | 說明 |
|------|------|
| 演算法 | XGBoost Regression（`reg:squarederror`，eval_metric=MAE） |
| 預測目標 | 主隊讓分（正數＝主隊贏分，負數＝客隊贏分） |
| 額外特徵 | `pace_proxy`（節奏代理）、`rest_days_home/away`、`rest_advantage` |
| 訓練輪數 | 150 rounds |

---

## 五、訓練結果（2026-04-12）

| 指標 | 數值 |
|------|------|
| 訓練資料 | 90天 / 631場歷史比賽 |
| 主模型 Training RMSE | **8.05 分**（典型值 11-13，越低越好） |
| Spread 模型 MAE | **4.7 分** |
| 回測場次 | 269 場 |
| 整體勝率 | **79.9%** |
| 強信號勝率（>70% 信心） | **88.2%** |

### Elo 排名（訓練後）

| 排名 | 球隊 | Elo |
|------|------|-----|
| 1 | San Antonio Spurs | 1837 |
| 2 | Oklahoma City Thunder | 1810 |
| 3 | Boston Celtics | 1744 |
| 4 | Detroit Pistons | 1727 |
| 5 | Cleveland Cavaliers | 1722 |

---

## 六、今日預測範例（2026-04-12，15 場）

| 客隊 | 主隊 | 預測勝隊 | 勝率 | 讓分 | 大小 |
|------|------|----------|------|------|------|
| Orlando Magic | Boston Celtics | Celtics | 84.6% | +10.6 | 227 |
| Washington Wizards | Cleveland Cavaliers | Cavaliers | 98.5% | +19.3 | 239 |
| Detroit Pistons | Indiana Pacers | Pistons | 83.5% | -12.1 | 231 |
| Atlanta Hawks | Miami Heat | Heat | 55.9% | +1.3 | 239 |
| Charlotte Hornets | New York Knicks | Knicks | 72.1% | +5.7 | 227 |
| Phoenix Suns | Oklahoma City Thunder | Thunder | 94.8% | +5.4 | 225 |
| Denver Nuggets | San Antonio Spurs | Spurs | 85.8% | -1.7 | 235 |
| Utah Jazz | Los Angeles Lakers | Lakers | 97.4% | +14.7 | 237 |

---

## 七、API 端點（由 dashboard.py 提供）

| 端點 | 說明 |
|------|------|
| `GET /nba` | nba.html 儀表板頁面 |
| `GET /api/nba/predictions` | 即時執行 nba_predictor.py --json（60秒 timeout） |
| `GET /api/nba/scoreboard` | 代理 ESPN 即時比分 API |
| `GET /nba_data.json` | 靜態快取預測 JSON（前端備援） |

### 前端載入邏輯（nba.html）
1. 先嘗試 `/api/nba/predictions`（即時計算）
2. 失敗則讀取 `/nba_data.json`（靜態快取）
3. 每 5 分鐘自動刷新一次

---

## 八、常用指令

```bash
# 進入專案根目錄
cd /Users/apple/Projects/autoBot_Dawson

# 訓練模型（約 2-3 分鐘）
make nba-train

# 生成今日預測 + 顯示摘要
make nba-predict

# 查 Polymarket 邊際機會
make nba-edge

# 回測（近 60 天）
make nba-backtest

# 直接執行腳本
cd autobots_NBA
python nba_predictor.py              # 今日預測（文字輸出）
python nba_predictor.py --json       # JSON 輸出（給儀表板用）
python nba_predictor.py --edge       # 邊際偵測
python nba_predictor.py --backtest   # 回測
python nba_predictor.py --train      # 重新訓練
python nba_predictor.py --train-spread  # 訓練讓分模型
```

---

## 九、每日自動更新

```bash
# 手動執行（訓練 + 預測）
bash /Users/apple/Projects/autoBot_Dawson/autobots_NBA/nba_daily_update.sh

# 只更新預測，不重新訓練（較快）
bash nba_daily_update.sh --predict-only

# 加入系統排程（每天 09:00 自動更新）
crontab -e
# 加入這行：
0 9 * * * /bin/bash /Users/apple/Projects/autoBot_Dawson/autobots_NBA/nba_daily_update.sh >> /tmp/nba_update.log 2>&1
```

更新 log 路徑：`autobots_NBA/logs/nba_update.log`

---

## 十、系統遷移記錄

| 日期 | 事項 |
|------|------|
| 2026-04-12 | 從 `autobots-teaching/pionex-bot/` 遷移至 `autobots_NBA/` 獨立資料夾 |
| 2026-04-12 | 安裝 `libomp`（`brew install libomp`），解決 macOS XGBoost 依賴問題 |
| 2026-04-12 | 完成模型初次訓練（90天 / 631場） |
| 2026-04-12 | dashboard.py 路由更新，`NBA_DIR` 指向新位置 |

---

## 十一、已知限制與未來改進

| 項目 | 說明 |
|------|------|
| 傷病資訊 | 目前未納入球員傷病（ESPN 有提供），為最大改進空間 |
| 球員出賽資料 | 可加入主力出賽率特徵 |
| 季後賽調整 | Elo K 值在季後賽可調高（賽程更重要） |
| 即時賠率整合 | 可整合 DraftKings / FanDuel 賠率，取代 Polymarket |
| 自動下注 | 邊際 > 閾值時可自動在 Polymarket 下單（需 API key） |

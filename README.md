# 🏀 autobots_NBA — NBA 勝賠率預測系統

基於 ESPN 公開資料 + Elo 評分 + XGBoost 機器學習的 NBA 比賽預測系統。
與 Polymarket 博彩市場對比找出邊際機會。

**自給自足** — 本資料夾包含所有程式、模型與資料，不依賴外部目錄即可運行。

---

## 📁 檔案結構

```
autobots_NBA/
├── README.md                    ← 本檔案
├── WORKLOG.md                   ← 系統說明與模型細節
├── requirements.txt             ← Python 依賴
├── .venv/                       ← 本地 Python venv（含 xgboost）
│
├── 【主程式】
├── nba_predictor.py             ← 核心：Elo + XGBoost + 邊際偵測（1329 行）
├── dashboard.py                 ← Flask 儀表板 API server（本機 port 8090）
├── nba_daily_update.sh          ← 每日自動更新腳本
├── deploy_nba_site.sh           ← 一鍵部署到 nba.shawny-project42.com
│
├── 【Streamlit 雲端版】
├── streamlit_app/
│   ├── app.py                   ← Streamlit Cloud 主程式
│   ├── requirements.txt         ← Streamlit 依賴
│   └── sync_data.py             ← 上傳 nba_data.json 到 GitHub Release
│
├── 【前端 + 輸出】
├── nba.html                     ← 儀表板頁面
├── nba_data.json                ← 今日預測靜態快取
│
├── 【模型狀態】
├── state/
│   ├── nba_model.json           ← Elo + 元資料
│   ├── nba_model.xgb            ← XGBoost 主模型（二進位）
│   ├── nba_calibration.json     ← deploy 用校準快照
│   ├── nba_spread_model.json    ← Spread 模型特徵清單
│   └── nba_spread_model.xgb     ← Spread 模型（二進位）
│
└── 【執行紀錄】
    └── logs/
        ├── nba_update.sh 輸出
        ├── dashboard.log
        └── launchd.error.log
```

---

## 🚀 快速啟動

```bash
cd /Users/shawnclaw/autobot/autobots_NBA
VENV=.venv/bin/python

# 今日預測（文字輸出）
$VENV nba_predictor.py

# 今日預測（JSON）
$VENV nba_predictor.py --json > nba_data.json

# 找 Polymarket 邊際機會
$VENV nba_predictor.py --edge

# 回測（近 60 天）
$VENV nba_predictor.py --backtest

# 重新訓練模型
$VENV nba_predictor.py --train --days 90

# 每日更新（訓練 + 預測）
bash nba_daily_update.sh

# 只更新預測，不重訓
bash nba_daily_update.sh --predict-only

# 一鍵部署到 nba.shawny-project42.com
bash deploy_nba_site.sh -m "Deploy NBA dashboard update"

# 啟動儀表板（本機 http://localhost:8090）
$VENV dashboard.py
```

---

## 🚢 一鍵部署到 `nba.shawny-project42.com`

`deploy_nba_site.sh` 會把本地修改整理成固定流程，並把 `/api/nba/predictions` 需要的模型狀態一起帶上：

1. `nba_resolve.py` 先補過去賽果
2. `nba_predictor.py --days-ahead 3 --json` 重建 `nba_data.json`，同時刷新 `state/nba_calibration.json`
3. 跑 Python / shell / 前端 JS 基本檢查
4. `git add -A`，並明確 stage `nba_data.json`、`state/nba_model.*`、`state/nba_spread_model.*`、`state/nba_calibration.json`
5. `git commit`
6. `git push origin HEAD:main`

前提是假設 `origin/main` 已經綁定到線上的自動部署服務（目前 `https://nba.shawny-project42.com/` 的行為就是這種模式）。

這樣做的目的是讓線上的 `/api/nba/predictions` 在 redeploy 後也直接吃到和本地相同的 Elo、spread model 與 calibration，而不是只同步頁面和 `nba_data.json`。

常用指令：

```bash
# 標準部署：更新資料 + 檢查 + commit + push
bash deploy_nba_site.sh -m "Deploy NBA dashboard calibration update"

# 只部署程式碼，不重抓資料
bash deploy_nba_site.sh --skip-data -m "Deploy UI-only change"

# 連 GitHub Release 的 nba_data.json 一起同步
bash deploy_nba_site.sh --sync-release -m "Deploy + sync data snapshot"

# 只在本地 commit，不 push
bash deploy_nba_site.sh --no-push -m "Prepare deploy locally"
```

---

## ☁️ Streamlit 雲端版（手機任何地方存取）

**公開 URL：** https://nba-dashboard2t.streamlit.app（部署後）

### 架構

```
Mac mini（本機）
  ├─ nba_predictor.py 每日 09:00 產出 nba_data.json
  └─ sync_data.py 自動上傳到 GitHub Release
                         ↓
GitHub Release: datadigshawn/nba_dashboard:data-latest
                         ↓
Streamlit Cloud（讀取 JSON 渲染儀表板）
                         ↓
手機/電腦: https://nba-dashboard2t.streamlit.app
```

### 部署步驟（1 次設定）

1. 開 https://share.streamlit.io
2. 用 GitHub 登入（datadigshawn）
3. **Create app** → Deploy from GitHub
4. 填：

| 欄位 | 值 |
|------|---|
| Repository | `datadigshawn/nba_dashboard` |
| Branch | `main` |
| Main file path | `streamlit_app/app.py` |
| App URL | `nba-dashboard2t` |
| Python version | `3.11` |

5. Deploy → 3 分鐘後可用

### 手動同步（測試用）

```bash
cd /Users/shawnclaw/autobot/autobots_NBA
.venv/bin/python streamlit_app/sync_data.py
```

---

## 🌐 本機儀表板

| URL | 說明 |
|-----|------|
| `http://localhost:8090/` | nba.html 完整儀表板頁面 |
| `http://localhost:8090/api/nba/predictions` | 即時跑預測（約 10 秒） |
| `http://localhost:8090/api/nba/scoreboard` | 代理 ESPN 即時比分 |
| `http://localhost:8090/api/nba/edge` | Polymarket 邊際機會 |
| `http://localhost:8090/nba_data.json` | 靜態快取（速度快，每日更新） |

前端載入邏輯（nba.html）：
1. 先嘗試 `/api/nba/predictions`（即時計算）
2. 失敗則讀取 `/nba_data.json`（靜態快取備援）
3. 每 5 分鐘自動刷新一次

---

## ⏰ 自動化排程

| launchd 服務 | 時機 | 功能 |
|-------------|------|------|
| `com.nba.daily_update` | 每日 **09:00** | 訓練模型 + 生成預測 JSON |
| `com.nba.dashboard` | 開機 & 常駐 | Flask 儀表板 port 8090 |

### 管理

```bash
# 查狀態
launchctl list | grep nba

# 停止 / 啟動
launchctl unload ~/Library/LaunchAgents/com.nba.daily_update.plist
launchctl load ~/Library/LaunchAgents/com.nba.daily_update.plist

# 手動觸發每日更新
launchctl start com.nba.daily_update

# 看 log
tail -f logs/nba_update.log
tail -f logs/dashboard.log
```

---

## 🗡 策略說明（三刀流版 NBA）

### Elo 評分系統

| 參數 | 數值 | 說明 |
|------|------|------|
| 初始 Elo | 1500 | 所有球隊起始值 |
| K 值 | 20 | 每場更新幅度 |
| 主場優勢 | +100 Elo | ≈ +3.5 分讓分 |
| 歷史範圍 | 近 90 天 | ≈ 600+ 場比賽 |

### 模型

**NBAPredictor（主模型）**
- 演算法：XGBoost Regression
- 預測目標：主隊得分差（home - away）
- 勝率轉換：Normal CDF based on RMSE
- 18 個特徵：elo_diff, win_pct_diff, ppg, oppg, streak, b2b 等

**SpreadPredictor（讓分專用）**
- 額外特徵：pace_proxy、rest_days、rest_advantage
- 目標：主隊讓分（正=贏分，負=輸分）

### 訓練成果（2026-04-12 訓練）

| 指標 | 數值 |
|------|------|
| 訓練資料 | 90 天 / 631 場 |
| 主模型 Training RMSE | 8.05 分 |
| Spread 模型 MAE | 4.7 分 |
| 回測勝率 | 79.9% |
| 強信號勝率（>70% 信心） | 88.2% |

---

## 🔧 依賴

- Python 3.11+
- [libomp](https://formulae.brew.sh/formula/libomp)（xgboost macOS 依賴，已裝）：`brew install libomp`
- venv 套件：見 `requirements.txt`

重建 venv：
```bash
cd /Users/shawnclaw/autobot/autobots_NBA
rm -rf .venv
/opt/homebrew/bin/python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

---

## 📊 資料來源

| 來源 | 用途 | 需驗證？ |
|------|------|---------|
| ESPN API | 賽程、戰績、歷史結果 | ❌ 公開 |
| Polymarket Gamma API | 博彩市場賠率（邊際偵測） | ❌ 公開 |

---

## 📝 Changelog

| 日期 | 變更 |
|------|------|
| 2026-04-12 | 從 autobots-teaching/pionex-bot 遷移至獨立資料夾 |
| 2026-04-12 | 完成模型初次訓練（90 天 / 631 場） |
| 2026-04-15 | 本地 .venv 建立（不再依賴外部路徑） |
| 2026-04-15 | dashboard.py 自給自足（取代 pionex-bot/dashboard.py 的 NBA 路由） |
| 2026-04-15 | launchd 排程（每日 09:00 更新 + 儀表板常駐）|

---

## 🐞 常見問題

### Q: `libomp not found` 錯誤
```bash
brew install libomp
```

### Q: Dashboard 無回應
```bash
launchctl list | grep nba
tail -30 logs/dashboard.error.log
```

### Q: 每日預測沒跑
```bash
# 手動觸發
launchctl start com.nba.daily_update
# 看 log
tail -50 logs/nba_update.log
```

### Q: Polymarket 邊際偵測說 "Could not match any markets"
Polymarket NBA 市場名稱常用縮寫或暱稱，模糊匹配時會失敗。可在 `nba_predictor.py` 的 `find_edges()` 加入 alias 對照表改善。

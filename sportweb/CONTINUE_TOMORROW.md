# 📌 下次接續工作指引

> 最後更新：**2026-04-17** · 今日完成 CF 自動恢復 + SQLite DB + Spread/Total Edge + 歷史回測
> 詳細工作紀錄見：`WORKLOG_2026-04-17.md`

---

## 當前狀態（綠燈）

- ✅ CF 自動恢復機制運作中（fetcher 失敗會自動 bootstrap + warmup）
- ✅ Spread / Total Edge 偵測已部署（每小時抓 event-level 資料）
- ✅ SQLite DB 已建立並回填歷史（sportWeb.db）
- ✅ Outcome Resolver 已掛到 edge_detector 每輪自動執行
- ✅ Streamlit Cloud 已顯示三種 edge + 歷史回測區

## 兩個 launchd jobs

```bash
launchctl list | grep sportweb
# com.sportweb.fetcher     每小時 :20（抓賠率 + 自動 CF 恢復）
# com.sportweb.edge_alert  每小時 :35（resolve + 偵測 + 推播 + DB）
```

## 明天可看 / 可做

1. **看第一批 resolved edges** — 今天 4/17 的 10 筆 edge 會在 4/18 比賽結束後被 resolver 填入結果。屆時 Streamlit 的「📈 Edge 歷史回測」區會出現實際勝率 + 校準數據。

2. **edges 表去重**（P1）— 目前沒 UNIQUE constraint，每小時跑會累加。加：
   ```sql
   CREATE UNIQUE INDEX idx_edge_unique
   ON edges(game_id, side, edge_type, line, detected_at);
   ```
   或只保留每場每類型每方向的**最新** edge。

3. **校準 σ 值**（P2）— 累積 ~30 筆 resolved edges 後，可從 calibration 表反推適合的 σ_spread / σ_total。

4. **Fractional Kelly**（P2）— 目前顯示全 Kelly，實盤建議用 1/4 Kelly。可加設定。

5. **半場/單節 edge**（P4）— event API 其實有 `[上半場]` 和 `[第1節]` 市場，可擴充。

## 恢復指令

```bash
# CF 失效（latest_odds.json 空掉）
cd /Users/shawnclaw/autobot/investing/sports/autobots_NBA/sportweb
.venv/bin/python src/bootstrap.py --wait 180
# 或直接跑 fetcher，會自動觸發
.venv/bin/python src/fetcher.py

# 手動 resolve
.venv/bin/python src/sport_resolve.py --days 7

# 手動跑 edge（會連帶 resolve + 寫 DB）
.venv/bin/python src/edge_detector.py --min-edge 0.05

# 看 DB 狀態
python3 -c "
import sys; sys.path.insert(0,'src')
from sport_db import db_summary, edge_backtest
import json
print(json.dumps(db_summary(), indent=2, ensure_ascii=False))
print(json.dumps(edge_backtest(), indent=2, ensure_ascii=False))
"
```

## 資料庫 Schema 快覽

```
sportWeb.db
├── snapshots (每小時抓取記錄)
├── odds (每場比賽的 moneyline)
├── edges (所有偵測到的 edge，含 edge_type / line / bet_won / actual_profit)
└── game_outcomes (ESPN 比賽結果)
```

## 相關檔案位置

- Python 原始碼：`src/`
- 資料：`data/` (JSON 快照 + fetcher log)
- 資料庫：`sportWeb.db`（.gitignore 內）
- 歷史 worklog：`WORKLOG_2026-04-{16,17}.md`

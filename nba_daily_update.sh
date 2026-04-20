#!/bin/bash
# ============================================================
# NBA Daily Prediction Update
# 每天執行一次，重新訓練模型並生成當日預測
#
# 用法：
#   bash nba_daily_update.sh                # 訓練 + 預測
#   bash nba_daily_update.sh --predict-only # 只生成預測，不重新訓練
# ============================================================

set -e

NBA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$NBA_DIR/.venv/bin/python"

# fallback to system python3 if local venv missing
if [ ! -f "$PYTHON" ]; then
    PYTHON="$(which python3)"
fi
LOG="$NBA_DIR/logs/nba_update.log"
DATA_OUT="$NBA_DIR/nba_data.json"

mkdir -p "$NBA_DIR/logs"

echo "======================================" | tee -a "$LOG"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] NBA 每日更新開始" | tee -a "$LOG"
echo "======================================" | tee -a "$LOG"

cd "$NBA_DIR"

# ── 1. 重新訓練模型 ──────────────────────────────
if [ "$1" != "--predict-only" ]; then
    echo "[$(date '+%H:%M:%S')] 訓練 XGBoost 主模型（90天）..." | tee -a "$LOG"
    "$PYTHON" nba_predictor.py --train --days 90 2>&1 | tee -a "$LOG"

    echo "[$(date '+%H:%M:%S')] 訓練 Spread 模型..." | tee -a "$LOG"
    "$PYTHON" nba_predictor.py --train-spread --days 90 2>&1 | tee -a "$LOG"
fi

# ── 2. 生成今日預測 JSON ──────────────────────────
echo "[$(date '+%H:%M:%S')] 生成今日預測..." | tee -a "$LOG"
"$PYTHON" nba_predictor.py --days-ahead 3 --json > "$DATA_OUT" 2>>"$LOG"

GAMES=$("$PYTHON" -c "import json; d=json.load(open('$DATA_OUT')); print(len(d.get('games',[])))" 2>/dev/null || echo "?")
EDGES=$("$PYTHON" -c "import json; d=json.load(open('$DATA_OUT')); print(len(d.get('edges',[])))" 2>/dev/null || echo "?")
echo "[$(date '+%H:%M:%S')] ✅ $GAMES 場比賽 | $EDGES 個邊際機會 → $DATA_OUT" | tee -a "$LOG"

# ── 3. 印出今日預測摘要 ────────────────────────────
echo "" | tee -a "$LOG"
"$PYTHON" nba_predictor.py 2>&1 | grep "Prediction:" | tee -a "$LOG"

# ── 4. 同步到 GitHub Release（供 Streamlit Cloud 讀取） ─
if [ -f "$NBA_DIR/streamlit_app/sync_data.py" ]; then
    echo "[$(date '+%H:%M:%S')] 同步至 GitHub Release..." | tee -a "$LOG"
    "$PYTHON" "$NBA_DIR/streamlit_app/sync_data.py" 2>&1 | tee -a "$LOG"
fi

# ── 5. Resolve 昨日比賽結果（填入 DB outcome）──
echo "[$(date '+%H:%M:%S')] Resolving past game outcomes..." | tee -a "$LOG"
"$PYTHON" nba_resolve.py 2>&1 | tee -a "$LOG"

# ── 6. 推送每日預測 digest 到 @NBA_predict55_bot ──
if [ -f "$NBA_DIR/telegram_push.py" ] && grep -q '^NBA_TG_CHAT_ID=.' "$NBA_DIR/.env" 2>/dev/null; then
    echo "[$(date '+%H:%M:%S')] 推送 Telegram digest..." | tee -a "$LOG"
    "$PYTHON" "$NBA_DIR/telegram_push.py" --digest 2>&1 | tee -a "$LOG"
else
    echo "[$(date '+%H:%M:%S')] ⚠️ Telegram push 略過（chat_id 尚未設定，請跑 setup_chat_id.py）" | tee -a "$LOG"
fi

echo "[$(date '+%H:%M:%S')] 更新完成" | tee -a "$LOG"

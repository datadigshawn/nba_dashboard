#!/bin/bash
# 收盤線快照抓取 — 供 CLV (Closing Line Value) 分析
#
# 由 com.nba.closing_odds launchd 服務在每日多個時段呼叫
# （00:30 / 02:30 / 06:30 / 07:30 / 08:30 / 10:00，對應台灣時間開賽前 30–60 分鐘），
# 把台灣運彩盤口快照寫入 sportweb/sportWeb.db 的 snapshots 相關表。
# CLV 分析時以「每場比賽開賽前最後一筆快照」作為收盤線。

set -uo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SPORTWEB_DIR="$BASE_DIR/sportweb"
LOG_FILE="$BASE_DIR/logs/closing_odds.log"
ALERT_LOG="$BASE_DIR/logs/alerts.log"

mkdir -p "$BASE_DIR/logs"

ts() { date "+%Y-%m-%d %H:%M:%S"; }

echo "[$(ts)] [closing_odds] 開始抓取收盤線快照" >> "$LOG_FILE"

cd "$SPORTWEB_DIR" || {
  echo "[$(ts)] [closing_odds] ✗ 找不到 sportweb 目錄" >> "$ALERT_LOG"
  exit 1
}

if ./.venv/bin/python src/blob_fetcher.py >> "$LOG_FILE" 2>&1; then
  echo "[$(ts)] [closing_odds] ✓ 完成" >> "$LOG_FILE"
else
  rc=$?
  echo "[$(ts)] [closing_odds] ✗ fetcher.py 失敗 (exit $rc)，詳見 logs/closing_odds.log" >> "$ALERT_LOG"
  exit "$rc"
fi

#!/bin/bash
# ============================================================
# Daily NBA betting pipeline
#
# Flow:
#   1. Refresh autobots_NBA predictions and model state
#   2. Fetch latest sportWeb sportsbook odds
#   3. Resolve sportsbook outcomes / CLV and detect fresh edges
#   4. Sync deployable sportsbook artifacts into autobots_NBA
#   5. Deploy nba.shawny-project42.com
#
# Usage:
#   bash run_nba_betting_pipeline.sh
#   bash run_nba_betting_pipeline.sh --predict-only
#   bash run_nba_betting_pipeline.sh --no-deploy
#   bash run_nba_betting_pipeline.sh --no-push -m "Refresh local NBA betting data"
# ============================================================

set -euo pipefail

NBA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUTOBOT_ROOT="$(cd "$NBA_DIR/.." && pwd)"
SPORTWEB_DIR="$AUTOBOT_ROOT/sportWeb"
NBA_PY="$NBA_DIR/.venv/bin/python"
SPORTWEB_PY="$SPORTWEB_DIR/.venv/bin/python"

if [ ! -x "$NBA_PY" ]; then
    NBA_PY="$(command -v python3)"
fi

if [ ! -x "$SPORTWEB_PY" ]; then
    SPORTWEB_PY="$(command -v python3)"
fi

PREDICT_ONLY=0
RUN_DEPLOY=1
PUSH_CHANGES=1
SYNC_RELEASE=0
MIN_EDGE="0.05"
COMMIT_MESSAGE=""

usage() {
    cat <<EOF
Usage:
  bash run_nba_betting_pipeline.sh [options]

Options:
  --predict-only       Skip model retraining and only rebuild predictions.
  --no-deploy          Refresh everything locally but do not run deploy_nba_site.sh.
  --no-push            Pass --no-push to deploy_nba_site.sh.
  --sync-release       Also upload nba_data.json to GitHub Release during deploy.
  --min-edge VALUE     Edge threshold for sportWeb edge detection. Default: ${MIN_EDGE}
  -m, --message TEXT   Deploy commit message.
  -h, --help           Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --predict-only)
            PREDICT_ONLY=1
            shift
            ;;
        --no-deploy)
            RUN_DEPLOY=0
            shift
            ;;
        --no-push)
            PUSH_CHANGES=0
            shift
            ;;
        --sync-release)
            SYNC_RELEASE=1
            shift
            ;;
        --min-edge)
            MIN_EDGE="${2:-}"
            shift 2
            ;;
        -m|--message)
            COMMIT_MESSAGE="${2:-}"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "[pipeline] Unknown option: $1" >&2
            usage
            exit 2
            ;;
    esac
done

log() {
    printf '[pipeline] %s\n' "$*"
}

run() {
    log "$*"
    "$@"
}

run_in_dir() {
    local dir="$1"
    shift
    log "cd $dir && $*"
    (
        cd "$dir"
        "$@"
    )
}

require_file() {
    if [[ ! -f "$1" ]]; then
        echo "[pipeline] Missing required file: $1" >&2
        exit 2
    fi
}

refresh_nba() {
    if [[ "$PREDICT_ONLY" -eq 1 ]]; then
        run_in_dir "$NBA_DIR" bash nba_daily_update.sh --predict-only
    else
        run_in_dir "$NBA_DIR" bash nba_daily_update.sh
    fi
}

refresh_sportbook() {
    run_in_dir "$SPORTWEB_DIR" "$SPORTWEB_PY" src/fetcher.py
    run_in_dir "$SPORTWEB_DIR" "$SPORTWEB_PY" src/sport_resolve.py --days 30
    run_in_dir "$SPORTWEB_DIR" "$SPORTWEB_PY" src/edge_detector.py --min-edge "$MIN_EDGE"
}

sync_sportbook_artifacts() {
    run_in_dir "$NBA_DIR" "$NBA_PY" sync_sportweb_data.py
    run_in_dir "$NBA_DIR" "$NBA_PY" -m json.tool tw_odds.json >/dev/null
    run_in_dir "$NBA_DIR" "$NBA_PY" -m json.tool sportbook_report.json >/dev/null
    run_in_dir "$NBA_DIR" "$NBA_PY" -m json.tool pick_stats.json >/dev/null
}

send_betting_alert() {
    if [ -f "$NBA_DIR/telegram_push.py" ] && grep -q '^NBA_TG_CHAT_ID=.' "$NBA_DIR/.env" 2>/dev/null; then
        log "Sending Telegram betting alert..."
        if ! run_in_dir "$NBA_DIR" "$NBA_PY" telegram_push.py --betting-alert --topn 5; then
            log "Telegram betting alert failed; continue without stopping pipeline"
        fi
    else
        log "Skip Telegram betting alert (chat_id not configured)"
    fi
}

deploy_site() {
    local args=(bash deploy_nba_site.sh --skip-data)
    if [[ "$SYNC_RELEASE" -eq 1 ]]; then
        args+=(--sync-release)
    fi
    if [[ "$PUSH_CHANGES" -eq 0 ]]; then
        args+=(--no-push)
    fi
    if [[ -n "$COMMIT_MESSAGE" ]]; then
        args+=(-m "$COMMIT_MESSAGE")
    fi
    run_in_dir "$NBA_DIR" "${args[@]}"
}

main() {
    require_file "$SPORTWEB_DIR/src/fetcher.py"
    require_file "$SPORTWEB_DIR/src/edge_detector.py"
    require_file "$NBA_DIR/nba_daily_update.sh"
    require_file "$NBA_DIR/deploy_nba_site.sh"

    refresh_nba
    refresh_sportbook
    sync_sportbook_artifacts

    if [[ "$RUN_DEPLOY" -eq 1 ]]; then
        deploy_site
    else
        log "Skip deploy"
    fi

    send_betting_alert

    log "Pipeline complete"
}

main "$@"

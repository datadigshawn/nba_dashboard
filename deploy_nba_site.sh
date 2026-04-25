#!/bin/bash
# ============================================================
# Deploy autobots_NBA to https://nba.shawny-project42.com/
#
# Assumption:
#   pushing to GitHub origin/main triggers the hosting platform
#   (e.g. Railway) to redeploy the site automatically.
#
# Default flow:
#   1. Resolve past game outcomes
#   2. Rebuild nba_data.json + calibration snapshot
#   3. Run lightweight checks
#   4. Stage code + deploy-critical model state
#   5. git commit
#   6. git push origin HEAD:main
#
# Usage:
#   bash deploy_nba_site.sh
#   bash deploy_nba_site.sh -m "Deploy NBA dashboard calibration update"
#   bash deploy_nba_site.sh --skip-data
#   bash deploy_nba_site.sh --sync-release
#   bash deploy_nba_site.sh --no-push
# ============================================================

set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SITE_URL="${SITE_URL:-https://nba.shawny-project42.com/}"
REMOTE="${REMOTE:-origin}"
DEPLOY_BRANCH="${DEPLOY_BRANCH:-main}"
PYTHON="$BASE_DIR/.venv/bin/python"

if [ ! -x "$PYTHON" ]; then
    PYTHON="$(command -v python3)"
fi

REFRESH_DATA=1
SYNC_RELEASE=0
RUN_CHECKS=1
PUSH_CHANGES=1
COMMIT_MESSAGE=""
DEPLOY_STATE_FILES=(
    "state/nba_model.json"
    "state/nba_model.xgb"
    "state/nba_spread_model.json"
    "state/nba_spread_model.xgb"
    "state/nba_calibration.json"
)
OPTIONAL_SYNC_FILES=(
    "tw_odds.json"
    "sportbook_report.json"
)

usage() {
    cat <<EOF
Usage:
  bash deploy_nba_site.sh [options]

Options:
  -m, --message TEXT   Commit message. Default uses timestamp.
  --skip-data          Skip resolve + regenerate nba_data.json.
  --sync-release       Also upload nba_data.json to GitHub Release.
  --skip-checks        Skip py_compile / shell / JS syntax checks.
  --no-push            Commit locally but do not push to ${REMOTE}/${DEPLOY_BRANCH}.
  --remote NAME        Git remote to push. Default: ${REMOTE}
  --branch NAME        Remote branch to push. Default: ${DEPLOY_BRANCH}
  -h, --help           Show this help.

Examples:
  bash deploy_nba_site.sh
  bash deploy_nba_site.sh -m "Deploy next-games + picks stats"
  bash deploy_nba_site.sh --skip-data --no-push
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -m|--message)
            COMMIT_MESSAGE="${2:-}"
            shift 2
            ;;
        --skip-data)
            REFRESH_DATA=0
            shift
            ;;
        --sync-release)
            SYNC_RELEASE=1
            shift
            ;;
        --skip-checks)
            RUN_CHECKS=0
            shift
            ;;
        --no-push)
            PUSH_CHANGES=0
            shift
            ;;
        --remote)
            REMOTE="${2:-}"
            shift 2
            ;;
        --branch)
            DEPLOY_BRANCH="${2:-}"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "[deploy] Unknown option: $1" >&2
            usage
            exit 2
            ;;
    esac
done

cd "$BASE_DIR"

log() {
    printf '[deploy] %s\n' "$*"
}

run() {
    log "$*"
    "$@"
}

require_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "[deploy] Missing required command: $1" >&2
        exit 2
    fi
}

current_branch() {
    git branch --show-current
}

has_worktree_changes() {
    [[ -n "$(git status --porcelain)" ]]
}

require_file() {
    if [[ ! -f "$1" ]]; then
        echo "[deploy] Missing required file: $1" >&2
        exit 2
    fi
}

run_js_check() {
    if ! command -v node >/dev/null 2>&1; then
        log "node not found; skip nba.html JS syntax check"
        return 0
    fi

    local tmp_js
    tmp_js="$(mktemp /tmp/nba_dashboard_script.XXXXXX.js)"
    perl -0ne 'if (/<script>\n(.*)\n<\/script>\n<\/body>/s) { print $1 }' nba.html > "$tmp_js"
    run node --check "$tmp_js"
    rm -f "$tmp_js"
}

refresh_data() {
    log "Refreshing local NBA data snapshot"
    run "$PYTHON" nba_resolve.py
    log "$PYTHON nba_predictor.py --days-ahead 3 --json > nba_data.json"
    "$PYTHON" nba_predictor.py --days-ahead 3 --json > nba_data.json
    run "$PYTHON" -m json.tool nba_data.json >/dev/null

    if [[ "$SYNC_RELEASE" -eq 1 ]]; then
        require_cmd gh
        run "$PYTHON" streamlit_app/sync_data.py
    fi
}

check_deploy_state() {
    local path

    require_file "nba_data.json"

    for path in "${DEPLOY_STATE_FILES[@]}"; do
        if [[ ! -f "$path" ]]; then
            echo "[deploy] Missing deploy-critical model state: $path" >&2
            echo "[deploy] Run nba_daily_update.sh or regenerate predictions before deploy so /api/nba/predictions matches local state." >&2
            exit 2
        fi
    done
}

run_checks() {
    log "Running deploy checks"
    run "$PYTHON" -m py_compile \
        dashboard.py \
        nba_predictor.py \
        nba_db.py \
        nba_resolve.py \
        nba_backfill.py \
        streamlit_app/app.py \
        streamlit_app/sync_data.py \
        sync_sportweb_data.py \
        telegram_push.py \
        setup_chat_id.py
    run bash -n nba_daily_update.sh
    run bash -n deploy_nba_site.sh
    run_js_check
}

stage_deploy_state() {
    local path

    log "Staging deploy-critical model state"
    run git add nba_data.json
    for path in "${DEPLOY_STATE_FILES[@]}"; do
        run git add "$path"
    done
    for path in "${OPTIONAL_SYNC_FILES[@]}"; do
        if [[ -f "$path" ]]; then
            run git add "$path"
        fi
    done
}

stage_and_commit() {
    log "Current git status"
    git status --short

    run git add -A
    stage_deploy_state

    if git diff --cached --quiet; then
        log "No staged changes. Nothing to deploy."
        return 1
    fi

    if [[ -z "$COMMIT_MESSAGE" ]]; then
        COMMIT_MESSAGE="Deploy NBA site $(date '+%Y-%m-%d %H:%M:%S')"
    fi

    run git commit -m "$COMMIT_MESSAGE"
    return 0
}

push_deploy() {
    local branch
    branch="$(current_branch)"
    if [[ "$branch" != "$DEPLOY_BRANCH" ]]; then
        log "Local branch is '$branch'; pushing HEAD to ${REMOTE}/${DEPLOY_BRANCH}"
    fi
    run git push "$REMOTE" "HEAD:${DEPLOY_BRANCH}"
    log "Push complete. Hosting platform should auto-redeploy: ${SITE_URL}"
}

main() {
    require_cmd git

    if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        echo "[deploy] Not inside a git repository" >&2
        exit 2
    fi

    if ! git remote get-url "$REMOTE" >/dev/null 2>&1; then
        echo "[deploy] Git remote '$REMOTE' not found" >&2
        exit 2
    fi

    if [[ "$REFRESH_DATA" -eq 1 ]]; then
        refresh_data
    else
        log "Skip data refresh"
    fi

    check_deploy_state

    if [[ "$RUN_CHECKS" -eq 1 ]]; then
        run_checks
    else
        log "Skip checks"
    fi

    if ! has_worktree_changes; then
        log "Worktree is clean. Nothing to deploy."
        exit 0
    fi

    if stage_and_commit; then
        if [[ "$PUSH_CHANGES" -eq 1 ]]; then
            push_deploy
        else
            log "Skip push. Commit created locally only."
        fi
    fi
}

main "$@"

#!/bin/bash
# ============================================================
# Conditional NBA auto deploy
#
# Intended launchd flow:
#   1. The betting pipeline refreshes generated data/model artifacts.
#   2. This script validates the app and generated artifacts.
#   3. If and only if deployable artifacts changed, commit + push them.
#   4. Restart the local dashboard service after a successful push.
#
# Unlike deploy_nba_site.sh, this script never runs `git add -A`.
# It only stages the deploy-artifact allowlist below.
#
# Usage:
#   bash nba_auto_deploy.sh
#   bash nba_auto_deploy.sh --dry-run
#   bash nba_auto_deploy.sh --run-pipeline
#   bash nba_auto_deploy.sh --no-push
#   bash nba_auto_deploy.sh -m "Auto NBA data deploy"
# ============================================================

set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$BASE_DIR/.venv/bin/python"
REMOTE="${REMOTE:-origin}"
DEPLOY_BRANCH="${DEPLOY_BRANCH:-main}"
DASHBOARD_SERVICE="${DASHBOARD_SERVICE:-com.nba.dashboard}"
LOCAL_DASHBOARD_URL="${LOCAL_DASHBOARD_URL:-http://127.0.0.1:8090/}"
LOCK_DIR="${LOCK_DIR:-/tmp/nba_auto_deploy.lock}"

if [[ ! -x "$PYTHON" ]]; then
    PYTHON="$(command -v python3)"
fi

DRY_RUN=0
RUN_PIPELINE=0
PUSH_CHANGES=1
RESTART_DASHBOARD=1
REMOTE_CHECK=1
COMMIT_MESSAGE=""

REQUIRED_DEPLOY_FILES=(
    "nba_data.json"
    "performance_summary.json"
    "pick_stats.json"
    "state/nba_model.json"
    "state/nba_model.xgb"
    "state/nba_calibration.json"
)

OPTIONAL_DEPLOY_FILES=(
    "state/nba_spread_model.json"
    "state/nba_spread_model.xgb"
    "tw_odds.json"
    "sportbook_report.json"
    "pick_result_overrides.json"
)

PY_COMPILE_FILES=(
    "dashboard.py"
    "nba_predictor.py"
    "nba_db.py"
    "nba_resolve.py"
    "nba_backfill.py"
    "nba_tracker.py"
    "pick_history.py"
    "pick_results_cli.py"
    "export_static_reports.py"
    "sync_sportweb_data.py"
    "validate_nba_data.py"
    "telegram_push.py"
    "setup_chat_id.py"
    "streamlit_app/app.py"
    "streamlit_app/sync_data.py"
)

SHELL_CHECK_FILES=(
    "nba_daily_update.sh"
    "run_nba_betting_pipeline.sh"
    "deploy_nba_site.sh"
    "nba_auto_deploy.sh"
)

usage() {
    cat <<EOF
Usage:
  bash nba_auto_deploy.sh [options]

Options:
  --dry-run            Run gates and show deployable changes without staging/commit/push.
  --run-pipeline       First run run_nba_betting_pipeline.sh --predict-only --no-deploy.
  --no-push            Commit locally but skip git push and dashboard restart.
  --no-restart         Skip dashboard service restart after push.
  --skip-remote-check  Do not require local HEAD to equal ${REMOTE}/${DEPLOY_BRANCH} before committing.
  --remote NAME        Git remote to push. Default: ${REMOTE}
  --branch NAME        Remote branch to push. Default: ${DEPLOY_BRANCH}
  -m, --message TEXT   Commit message. Default uses timestamp.
  -h, --help           Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        --run-pipeline)
            RUN_PIPELINE=1
            shift
            ;;
        --no-push)
            PUSH_CHANGES=0
            shift
            ;;
        --no-restart)
            RESTART_DASHBOARD=0
            shift
            ;;
        --skip-remote-check)
            REMOTE_CHECK=0
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
        -m|--message)
            COMMIT_MESSAGE="${2:-}"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "[auto-deploy] Unknown option: $1" >&2
            usage
            exit 2
            ;;
    esac
done

cd "$BASE_DIR"

log() {
    printf '[auto-deploy] %s\n' "$*"
}

run() {
    log "$*"
    "$@"
}

require_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "[auto-deploy] Missing required command: $1" >&2
        exit 2
    fi
}

require_file() {
    if [[ ! -f "$1" ]]; then
        echo "[auto-deploy] Missing required deploy artifact: $1" >&2
        exit 2
    fi
}

cleanup_lock() {
    rmdir "$LOCK_DIR" 2>/dev/null || true
}

acquire_lock() {
    if ! mkdir "$LOCK_DIR" 2>/dev/null; then
        echo "[auto-deploy] Another auto deploy appears to be running: $LOCK_DIR" >&2
        exit 75
    fi
    trap cleanup_lock EXIT
}

all_deploy_files() {
    printf '%s\n' "${REQUIRED_DEPLOY_FILES[@]}" "${OPTIONAL_DEPLOY_FILES[@]}"
}

existing_deploy_files() {
    local path
    for path in "${REQUIRED_DEPLOY_FILES[@]}"; do
        printf '%s\n' "$path"
    done
    for path in "${OPTIONAL_DEPLOY_FILES[@]}"; do
        if [[ -f "$path" ]]; then
            printf '%s\n' "$path"
        fi
    done
}

is_deploy_file() {
    local needle="$1"
    local path
    while IFS= read -r path; do
        [[ "$path" == "$needle" ]] && return 0
    done < <(all_deploy_files)
    return 1
}

run_js_check() {
    if ! command -v node >/dev/null 2>&1; then
        log "node not found; skip nba.html JS syntax check"
        return 0
    fi

    local tmp_js
    tmp_js="$(mktemp /tmp/nba_auto_deploy_script.XXXXXX.js)"
    perl -0ne 'if (/<script>\n(.*)\n<\/script>\n<\/body>/s) { print $1 }' nba.html > "$tmp_js"
    run node --check "$tmp_js"
    rm -f "$tmp_js"
}

run_pipeline_if_requested() {
    if [[ "$RUN_PIPELINE" -eq 1 ]]; then
        run bash run_nba_betting_pipeline.sh --predict-only --no-deploy
    else
        log "Skip pipeline refresh; expecting scheduled betting pipeline to have refreshed artifacts"
    fi
}

validate_artifacts() {
    local path

    for path in "${REQUIRED_DEPLOY_FILES[@]}"; do
        require_file "$path"
    done

    run "$PYTHON" -m py_compile "${PY_COMPILE_FILES[@]}"
    for path in "${SHELL_CHECK_FILES[@]}"; do
        run bash -n "$path"
    done

    run "$PYTHON" -m json.tool nba_data.json >/dev/null
    run "$PYTHON" -m json.tool performance_summary.json >/dev/null
    run "$PYTHON" -m json.tool pick_stats.json >/dev/null

    for path in "${OPTIONAL_DEPLOY_FILES[@]}"; do
        if [[ "$path" == *.json && -f "$path" ]]; then
            run "$PYTHON" -m json.tool "$path" >/dev/null
        fi
    done

    run "$PYTHON" validate_nba_data.py
    run_js_check
}

require_git_ready() {
    local branch

    require_cmd git
    if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        echo "[auto-deploy] Not inside a git repository" >&2
        exit 2
    fi
    if ! git remote get-url "$REMOTE" >/dev/null 2>&1; then
        echo "[auto-deploy] Git remote '$REMOTE' not found" >&2
        exit 2
    fi

    branch="$(git branch --show-current)"
    if [[ "$branch" != "$DEPLOY_BRANCH" ]]; then
        echo "[auto-deploy] Current branch is '$branch'; expected '$DEPLOY_BRANCH' for unattended deploy" >&2
        exit 2
    fi
}

require_clean_index() {
    local staged
    staged="$(git diff --cached --name-only)"
    if [[ -n "$staged" ]]; then
        echo "[auto-deploy] Refusing to run because the git index already has staged changes:" >&2
        printf '%s\n' "$staged" >&2
        exit 2
    fi
}

require_remote_synced() {
    if [[ "$PUSH_CHANGES" -ne 1 || "$REMOTE_CHECK" -ne 1 ]]; then
        return 0
    fi

    local local_head
    local remote_head

    run git fetch "$REMOTE" "$DEPLOY_BRANCH"
    local_head="$(git rev-parse HEAD)"
    remote_head="$(git rev-parse FETCH_HEAD)"

    if [[ "$local_head" != "$remote_head" ]]; then
        echo "[auto-deploy] Local HEAD does not match ${REMOTE}/${DEPLOY_BRANCH}; aborting unattended deploy." >&2
        echo "[auto-deploy] Review/pull remote changes first, then rerun." >&2
        exit 2
    fi
}

has_deploy_changes() {
    local changed=0
    local path
    while IFS= read -r path; do
        if ! git diff --quiet -- "$path"; then
            changed=1
        fi
        if ! git diff --cached --quiet -- "$path"; then
            changed=1
        fi
    done < <(existing_deploy_files)
    [[ "$changed" -eq 1 ]]
}

show_deploy_changes() {
    log "Deploy artifact status"
    git status --short -- $(existing_deploy_files)
}

stage_deploy_artifacts() {
    local path
    while IFS= read -r path; do
        if is_deploy_file "$path"; then
            run git add "$path"
        fi
    done < <(existing_deploy_files)
}

commit_deploy_artifacts() {
    stage_deploy_artifacts

    if git diff --cached --quiet; then
        log "No staged deploy artifact changes. Nothing to commit."
        return 1
    fi

    if [[ -z "$COMMIT_MESSAGE" ]]; then
        COMMIT_MESSAGE="Auto NBA data deploy $(date '+%Y-%m-%d %H:%M:%S')"
    fi

    run git commit -m "$COMMIT_MESSAGE"
    return 0
}

push_deploy() {
    if [[ "$PUSH_CHANGES" -ne 1 ]]; then
        log "Skip push. Commit created locally only."
        return 0
    fi
    run git push "$REMOTE" "HEAD:${DEPLOY_BRANCH}"
}

restart_dashboard_service() {
    if [[ "$PUSH_CHANGES" -ne 1 || "$RESTART_DASHBOARD" -ne 1 ]]; then
        log "Skip dashboard service restart"
        return 0
    fi
    if ! command -v launchctl >/dev/null 2>&1; then
        log "launchctl not found; skip dashboard service restart"
        return 0
    fi

    local service_target
    service_target="gui/$(id -u)/${DASHBOARD_SERVICE}"
    if ! launchctl print "$service_target" >/dev/null 2>&1; then
        log "launchd service ${service_target} not registered; skip restart"
        return 0
    fi

    run launchctl kickstart -k "$service_target"
}

health_check() {
    if [[ "$PUSH_CHANGES" -ne 1 ]]; then
        return 0
    fi
    if ! command -v curl >/dev/null 2>&1; then
        log "curl not found; skip local dashboard health check"
        return 0
    fi

    local attempt
    local tmp_html
    tmp_html="$(mktemp /tmp/nba_auto_deploy_health.XXXXXX.html)"

    for attempt in {1..15}; do
        if curl -fsS "$LOCAL_DASHBOARD_URL" > "$tmp_html" && grep -q "NBA PREDICTIONS" "$tmp_html"; then
            log "Local dashboard health check passed: $LOCAL_DASHBOARD_URL"
            rm -f "$tmp_html"
            return 0
        fi
        log "Dashboard not ready yet; retry health check ${attempt}/15"
        sleep 2
    done

    echo "[auto-deploy] Local dashboard health check failed: $LOCAL_DASHBOARD_URL" >&2
    rm -f "$tmp_html"
    exit 2
}

main() {
    acquire_lock
    require_git_ready
    require_clean_index

    run_pipeline_if_requested
    validate_artifacts

    if ! has_deploy_changes; then
        log "No deploy artifact changes. Nothing to deploy."
        exit 0
    fi

    show_deploy_changes

    if [[ "$DRY_RUN" -eq 1 ]]; then
        log "Dry run complete. No files staged, committed, pushed, or restarted."
        exit 0
    fi

    require_remote_synced

    if commit_deploy_artifacts; then
        push_deploy
        restart_dashboard_service
        health_check
    fi
}

main "$@"

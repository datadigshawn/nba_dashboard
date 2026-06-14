#!/usr/bin/env python3
"""
把 Mac mini 的 nba_data.json 上傳到 GitHub Release `data-latest`。

執行：
  python3 sync_data.py
  python3 sync_data.py --repo datadigshawn/nba_dashboard
"""
import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_FILE = BASE_DIR / "nba_data.json"
ALERT_LOG_PATH = BASE_DIR / "logs" / "alerts.log"
EXTRA_FILES = [
    BASE_DIR / "tw_odds.json",
    BASE_DIR / "sportbook_report.json",
    BASE_DIR / "performance_summary.json",
    BASE_DIR / "pick_stats.json",
]
GH_RETRIES = 3
RETRY_BASE_DELAY = 5  # seconds; 5 → 15 → 45


def log_alert(msg: str) -> None:
    """失敗告警統一落地到 logs/alerts.log，供人工與 auto_deploy 檢查。"""
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [sync_data] {msg}"
    print(line, file=sys.stderr)
    try:
        ALERT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with ALERT_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _gh_with_retry(args: list[str], label: str) -> subprocess.CompletedProcess | None:
    """跑 gh 指令，網路類失敗時指數退避重試。回傳最後一次結果；全失敗回 None。"""
    last = None
    for attempt in range(1, GH_RETRIES + 1):
        last = subprocess.run(args, capture_output=True, text=True)
        if last.returncode == 0:
            return last
        if attempt < GH_RETRIES:
            delay = RETRY_BASE_DELAY * (3 ** (attempt - 1))
            print(f"[sync] {label} 失敗 (attempt {attempt}/{GH_RETRIES}): "
                  f"{last.stderr.strip()} — {delay}s 後重試", file=sys.stderr)
            time.sleep(delay)
    return last


def gh_release_upload(repo: str, tag: str, path: Path, *, release_name: str = "NBA predictions snapshot"):
    # 先確認 release 存在（view 失敗可能是不存在，也可能是網路問題，交給 create 的重試判斷）
    r = subprocess.run(["gh", "release", "view", tag, "--repo", repo],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[sync] 建立 release {tag}")
        c = _gh_with_retry([
            "gh", "release", "create", tag,
            "--repo", repo,
            "--title", release_name,
            "--notes", "Auto-synced NBA predictions from Mac mini",
        ], f"建立 release {tag}")
        if c is None or c.returncode != 0:
            already = c is not None and "already exists" in (c.stderr or "")
            if not already:
                log_alert(f"✗ 建立 release {tag} 失敗: {(c.stderr if c else 'unknown').strip()}")
                return False

    # 上傳 asset（覆寫）
    print(f"[sync] 上傳 {path.name} 到 {repo}:{tag}")
    c = _gh_with_retry([
        "gh", "release", "upload", tag, str(path),
        "--repo", repo, "--clobber",
    ], f"上傳 {path.name}")
    if c is None or c.returncode != 0:
        log_alert(f"✗ 上傳 {path.name} 失敗: {(c.stderr if c else 'unknown').strip()}")
        return False
    print("[sync] ✅ 完成")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.environ.get("DATA_REPO", "datadigshawn/nba_dashboard"))
    ap.add_argument("--tag",  default=os.environ.get("DATA_TAG",  "data-latest"))
    args = ap.parse_args()

    if not DATA_FILE.exists():
        print(f"[sync] ❌ {DATA_FILE} 不存在；請先執行 nba_daily_update.sh", file=sys.stderr)
        sys.exit(1)

    size_kb = DATA_FILE.stat().st_size / 1024
    print(f"[sync] 準備上傳 {DATA_FILE} ({size_kb:.1f} KB)")

    ok = gh_release_upload(args.repo, args.tag, DATA_FILE)
    for path in EXTRA_FILES:
        if path.exists():
            ok = gh_release_upload(args.repo, args.tag, path) and ok
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

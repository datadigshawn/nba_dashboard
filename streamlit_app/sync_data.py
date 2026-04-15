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
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_FILE = BASE_DIR / "nba_data.json"


def gh_release_upload(repo: str, tag: str, path: Path, *, release_name: str = "NBA predictions snapshot"):
    # 先確認 release 存在
    r = subprocess.run(["gh", "release", "view", tag, "--repo", repo],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[sync] 建立 release {tag}")
        c = subprocess.run([
            "gh", "release", "create", tag,
            "--repo", repo,
            "--title", release_name,
            "--notes", "Auto-synced NBA predictions from Mac mini",
        ], capture_output=True, text=True)
        if c.returncode != 0:
            print(f"[sync] 建立失敗: {c.stderr}", file=sys.stderr)
            return False

    # 上傳 asset（覆寫）
    print(f"[sync] 上傳 {path.name} 到 {repo}:{tag}")
    c = subprocess.run([
        "gh", "release", "upload", tag, str(path),
        "--repo", repo, "--clobber",
    ], capture_output=True, text=True)
    if c.returncode != 0:
        print(f"[sync] 上傳失敗: {c.stderr}", file=sys.stderr)
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
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

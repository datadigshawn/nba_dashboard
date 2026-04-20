"""
One-shot setup: finds the chat_id from bot's getUpdates queue and writes
it back to .env so telegram_push.py can use it.

Prerequisite:
  1. In Telegram, find @NBA_predict55_bot
  2. Tap "START" or send any message to the bot
  3. Run this script

It polls getUpdates once, extracts the most-recent chat_id, updates .env.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"


def _parse_env() -> dict[str, str]:
    out: dict[str, str] = {}
    if ENV_PATH.exists():
        for ln in ENV_PATH.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def _write_chat_id(chat_id: str) -> None:
    """Update or append NBA_TG_CHAT_ID in .env, preserving everything else."""
    lines: list[str] = []
    replaced = False
    if ENV_PATH.exists():
        for ln in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if ln.strip().startswith("NBA_TG_CHAT_ID="):
                lines.append(f"NBA_TG_CHAT_ID={chat_id}")
                replaced = True
            else:
                lines.append(ln)
    if not replaced:
        lines.append(f"NBA_TG_CHAT_ID={chat_id}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    env = _parse_env()
    token = env.get("NBA_TG_TOKEN", "").strip()
    if not token:
        print("✗ NBA_TG_TOKEN 未在 .env 設定", file=sys.stderr)
        sys.exit(2)

    url = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        print(f"✗ getUpdates 失敗: {e}", file=sys.stderr)
        sys.exit(1)

    if not data.get("ok"):
        print(f"✗ API error: {data}", file=sys.stderr)
        sys.exit(1)

    updates = data.get("result") or []
    if not updates:
        print("⚠️ getUpdates 是空的 — 你還沒對 @NBA_predict55_bot 按 START / 傳訊息")
        print("   請到 Telegram 找 @NBA_predict55_bot，按 START，然後再跑一次這個腳本")
        sys.exit(3)

    # Take the chat_id from the most recent update
    last = updates[-1]
    chat = (last.get("message") or last.get("edited_message")
            or last.get("channel_post") or {}).get("chat") or {}
    chat_id = chat.get("id")
    chat_title = chat.get("title") or chat.get("first_name") or chat.get("username") or "?"
    chat_type = chat.get("type", "?")

    if not chat_id:
        print(f"✗ 抓不到 chat_id，原始 update: {json.dumps(last, ensure_ascii=False)[:400]}")
        sys.exit(1)

    _write_chat_id(str(chat_id))
    print(f"✓ chat_id 寫入 .env: {chat_id}")
    print(f"   對象: [{chat_type}] {chat_title}")
    print(f"")
    print(f"下一步測試：")
    print(f"  .venv/bin/python telegram_push.py --msg '✅ Setup 完成'")


if __name__ == "__main__":
    main()

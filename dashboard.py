"""
NBA 預測儀表板 — 自給自足 Flask 伺服器

提供 nba.html 所需的所有 API 端點：
  GET /                         → nba.html
  GET /api/nba/predictions      → 即時跑 nba_predictor.py --json
  GET /api/nba/scoreboard       → 代理 ESPN 即時比分
  GET /nba_data.json            → 靜態快取（每日更新產出）

用法：
  python dashboard.py                # port 8090
  python dashboard.py --port 9000
  python dashboard.py --host 0.0.0.0  # 開放 LAN/Tailscale 存取
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import httpx
from flask import Flask, Response, jsonify, request, send_file

from nba_db import (
    DB_PATH,
    get_pick_stats,
    get_prediction_summary,
    init_db,
    list_odds,
    save_recommended_picks,
    upsert_odds,
    verify_pending_picks,
)

BASE_DIR = Path(__file__).resolve().parent
PYTHON = BASE_DIR / ".venv" / "bin" / "python"
if not PYTHON.exists():
    PYTHON = Path(sys.executable)

app = Flask(__name__)
init_db(DB_PATH)


def _run_predictor(*args: str, timeout: int = 60) -> Response:
    r = subprocess.run(
        [str(PYTHON), str(BASE_DIR / "nba_predictor.py"), *args],
        capture_output=True, text=True, timeout=timeout, cwd=str(BASE_DIR),
        encoding="utf-8",
    )
    if r.returncode == 0:
        return Response(r.stdout, content_type="application/json")
    return jsonify({"error": r.stderr[:500]}), 500


def _parse_float(value):
    if value in ("", None):
        return None
    return float(value)


@app.route("/")
def index():
    path = BASE_DIR / "nba.html"
    if path.exists():
        return send_file(str(path))
    return "<h1>nba.html not found</h1>", 404


@app.route("/nba_data.json")
def nba_data_json():
    path = BASE_DIR / "nba_data.json"
    if path.exists():
        return send_file(str(path), mimetype="application/json")
    return jsonify({"error": "nba_data.json not found"}), 404


@app.route("/api/nba/predictions")
def nba_predictions():
    """即時執行 nba_predictor.py --json（約 10 秒）。"""
    try:
        return _run_predictor("--days-ahead", "3", "--json", timeout=90)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "prediction timeout (90s)"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/nba/scoreboard")
def nba_scoreboard():
    """代理 ESPN 即時比分（讓前端不用跨域）。"""
    try:
        url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
        r = httpx.get(url, timeout=10)
        return Response(r.text, content_type="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/nba/edge")
def nba_edge():
    """執行 nba_predictor.py --edge --json 找 Polymarket 邊際機會。"""
    try:
        return _run_predictor("--edge", "--json", timeout=90)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/nba/odds", methods=["GET", "POST"])
def nba_odds():
    if request.method == "POST":
        try:
            payload = request.get_json(silent=True) or {}
            game = (payload.get("game") or "").strip()
            if not game:
                return jsonify({"error": "game is required"}), 400
            row = upsert_odds(
                DB_PATH,
                game=game,
                spread=_parse_float(payload.get("spread")),
                ou=_parse_float(payload.get("ou")),
            )
            return jsonify({"ok": True, "message": "saved", "row": row})
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    try:
        entries = list_odds(DB_PATH)
        odds_map = {
            row["game"]: {
                "spread": row["spread"],
                "ou": row["ou"],
                "updated_at": row["updated_at"],
            }
            for row in entries
        }
        return jsonify({"odds": odds_map, "entries": entries})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/nba/picks/save", methods=["POST"])
def nba_picks_save():
    try:
        payload = request.get_json(silent=True) or {}
        picks = payload.get("picks") or []
        if not isinstance(picks, list):
            return jsonify({"error": "picks must be a list"}), 400
        saved = save_recommended_picks(DB_PATH, picks)
        return jsonify({"ok": True, "saved": saved})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/nba/picks/verify", methods=["POST"])
def nba_picks_verify():
    try:
        return jsonify({"ok": True, **verify_pending_picks(DB_PATH)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/nba/picks/stats")
def nba_picks_stats():
    try:
        return jsonify(get_pick_stats(DB_PATH))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/nba/performance-summary")
def nba_performance_summary():
    try:
        return jsonify(get_prediction_summary(DB_PATH))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--host", default="127.0.0.1",
                    help="綁定 IP；設 0.0.0.0 允許 LAN/Tailscale 存取")
    args = ap.parse_args()

    print(f"NBA Dashboard running on http://{args.host}:{args.port}")
    print(f"  index: {BASE_DIR / 'nba.html'}")
    print(f"  data:  {BASE_DIR / 'nba_data.json'}")
    app.run(host=args.host, port=args.port, debug=False)

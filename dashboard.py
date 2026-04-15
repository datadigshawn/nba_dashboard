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
from flask import Flask, Response, jsonify, send_file

BASE_DIR = Path(__file__).resolve().parent
PYTHON = BASE_DIR / ".venv" / "bin" / "python"
if not PYTHON.exists():
    PYTHON = Path(sys.executable)

app = Flask(__name__)


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
        r = subprocess.run(
            [str(PYTHON), str(BASE_DIR / "nba_predictor.py"), "--json"],
            capture_output=True, text=True, timeout=60, cwd=str(BASE_DIR),
            encoding="utf-8",
        )
        if r.returncode == 0:
            return Response(r.stdout, content_type="application/json")
        return jsonify({"error": r.stderr[:500]}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"error": "prediction timeout (60s)"}), 504
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
        r = subprocess.run(
            [str(PYTHON), str(BASE_DIR / "nba_predictor.py"), "--edge", "--json"],
            capture_output=True, text=True, timeout=90, cwd=str(BASE_DIR),
            encoding="utf-8",
        )
        if r.returncode == 0:
            return Response(r.stdout, content_type="application/json")
        return jsonify({"error": r.stderr[:500]}), 500
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

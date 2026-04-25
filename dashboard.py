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
SYNCED_TW_ODDS = BASE_DIR / "tw_odds.json"
SPORTBOOK_REPORT = BASE_DIR / "sportbook_report.json"
PICK_STATS_FILE = BASE_DIR / "pick_stats.json"


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


def _load_json_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_synced_tw_odds() -> dict:
    payload = _load_json_file(SYNCED_TW_ODDS)
    entries = payload.get("entries") or []
    odds_map = {}
    normalized_entries = []
    for row in entries:
        game = row.get("game")
        if not game:
            continue
        normalized = {
            "game": game,
            "spread": row.get("spread"),
            "ou": row.get("ou"),
            "updated_at": row.get("updated_at"),
            "source": row.get("source", "sportWeb"),
            "start_time": row.get("start_time"),
            "sportweb_game_id": row.get("sportweb_game_id"),
        }
        normalized_entries.append(normalized)
        odds_map[game] = {
            "spread": normalized["spread"],
            "ou": normalized["ou"],
            "updated_at": normalized["updated_at"],
            "source": normalized["source"],
        }
    return {
        "entries": normalized_entries,
        "odds": odds_map,
        "synced_at": payload.get("synced_at"),
        "source": payload.get("source") or {},
    }


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


@app.route("/tw_odds.json")
def tw_odds_json():
    if SYNCED_TW_ODDS.exists():
        return send_file(str(SYNCED_TW_ODDS), mimetype="application/json")
    return jsonify({"error": "tw_odds.json not found"}), 404


@app.route("/sportbook_report.json")
def sportbook_report_json():
    if SPORTBOOK_REPORT.exists():
        return send_file(str(SPORTBOOK_REPORT), mimetype="application/json")
    return jsonify({"error": "sportbook_report.json not found"}), 404


@app.route("/pick_stats.json")
def pick_stats_json():
    if PICK_STATS_FILE.exists():
        return send_file(str(PICK_STATS_FILE), mimetype="application/json")
    return jsonify({"error": "pick_stats.json not found"}), 404


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
        synced = _load_synced_tw_odds()
        merged = {row["game"]: dict(row) for row in synced.get("entries", [])}
        for row in list_odds(DB_PATH):
            base = merged.get(row["game"], {})
            merged[row["game"]] = {
                **base,
                "game": row["game"],
                "spread": row["spread"],
                "ou": row["ou"],
                "updated_at": row["updated_at"],
                "source": "manual" if base else "manual",
            }

        entries = sorted(merged.values(), key=lambda row: row["game"])
        odds_map = {
            row["game"]: {
                "spread": row.get("spread"),
                "ou": row.get("ou"),
                "updated_at": row.get("updated_at"),
                "source": row.get("source", "manual"),
            }
            for row in entries
        }
        return jsonify({
            "odds": odds_map,
            "entries": entries,
            "synced_at": synced.get("synced_at"),
            "source": synced.get("source"),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/nba/sportbook-report")
def nba_sportbook_report():
    try:
        payload = _load_json_file(SPORTBOOK_REPORT)
        if payload:
            return jsonify(payload)
        return jsonify({"error": "sportbook_report.json not found"}), 404
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
        live_stats = get_pick_stats(DB_PATH)
        fallback_payload = _load_json_file(PICK_STATS_FILE)
        fallback_stats = fallback_payload.get("stats") if isinstance(fallback_payload, dict) else {}
        if (fallback_stats or {}).get("total", 0) > live_stats.get("total", 0):
            return jsonify(fallback_stats)
        return jsonify(live_stats)
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

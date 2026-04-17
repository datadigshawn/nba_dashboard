"""
🏀 NBA 預測儀表板 — Streamlit 雲端版

資料來源：GitHub Release `data-latest` 的 nba_data.json
（Mac mini 每日 09:00 自動更新並上傳）

部署：
  Streamlit Community Cloud → repo datadigshawn/nba_dashboard
  Main file: streamlit_app/app.py
"""
import io
import json
import os
import traceback
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone

import httpx
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# ── 頁面 ────────────────────────────────────────────────
st.set_page_config(
    page_title="NBA 預測",
    page_icon="🏀",
    layout="wide",
    initial_sidebar_state="collapsed",
)

TZ_TAIPEI = timezone(timedelta(hours=8))
TZ_ET     = timezone(timedelta(hours=-4))  # 美東夏令


# ── 樣式（最大可讀性：超大字、高對比、手機友善）──────────
st.markdown("""
<style>
  .stApp { background: #0a1020; color: #ffffff; }

  /* 標題 */
  h1 {
    color: #ff6b35 !important;
    font-size: 36px !important;
    font-weight: 900 !important;
    letter-spacing: 1px;
    margin-bottom: 4px !important;
  }
  h2, h3 {
    color: #ffa500 !important;
    font-size: 24px !important;
    font-weight: 800 !important;
    margin-top: 20px !important;
  }

  /* 一般文字 */
  p, div, span, label { color: #e8f1ff; }
  .stCaption, [data-testid="stCaptionContainer"] { color: #9fb8d0 !important; font-size: 13px !important; }

  /* 比賽卡片 */
  .game-card {
    background: linear-gradient(135deg, #1e2d47 0%, #0d1a2f 100%);
    border: 2px solid #2a4066;
    border-radius: 16px;
    padding: 20px;
    margin-bottom: 16px;
    box-shadow: 0 4px 12px rgba(0,0,0,.3);
  }
  .game-status {
    display: inline-block;
    font-size: 13px;
    font-weight: 700;
    color: #ffa500;
    background: rgba(255,165,0,.15);
    padding: 3px 10px;
    border-radius: 4px;
    margin-bottom: 14px;
    letter-spacing: 1px;
    text-transform: uppercase;
  }
  .teams-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    margin-bottom: 14px;
  }
  .team-box {
    flex: 1;
    text-align: center;
    padding: 10px;
    border-radius: 10px;
    background: rgba(0,0,0,.25);
  }
  .team-name {
    font-size: 18px;
    font-weight: 800;
    color: #ffffff;
    line-height: 1.25;
    margin-bottom: 4px;
  }
  .team-record {
    font-size: 13px;
    font-weight: 600;
    color: #9fb8d0;
    margin-bottom: 8px;
    font-family: monospace;
  }
  .team-prob {
    font-family: monospace;
    font-size: 42px;
    font-weight: 900;
    line-height: 1;
    margin-top: 4px;
    text-shadow: 0 2px 8px rgba(0,0,0,.5);
  }

  .vs-divider {
    font-family: monospace;
    font-size: 24px;
    font-weight: 900;
    color: #ff6b35;
    padding: 0 6px;
  }

  /* 勝方高亮（超顯眼） */
  .winner-box {
    background: linear-gradient(135deg, rgba(0,255,136,.2) 0%, rgba(0,200,100,.1) 100%) !important;
    border: 2px solid #00ff88;
    box-shadow: 0 0 20px rgba(0,255,136,.25);
  }
  .winner-box .team-name { color: #00ff88 !important; }
  .winner-box .team-prob { color: #00ff88 !important; }

  .loser-box {
    background: rgba(0,0,0,.35) !important;
  }
  .loser-box .team-name { color: #7a90a8 !important; }
  .loser-box .team-prob { color: #7a90a8 !important; }

  /* 讓分、大小 徽章（大一點） */
  .spread-badge, .total-badge {
    display: inline-block;
    padding: 8px 14px;
    border-radius: 8px;
    font-family: monospace;
    font-size: 16px;
    font-weight: 800;
    margin-right: 8px;
    margin-top: 4px;
  }
  .spread-badge {
    background: rgba(255,167,0,.2);
    border: 2px solid #ffa500;
    color: #ffcc66;
  }
  .total-badge {
    background: rgba(100,180,255,.2);
    border: 2px solid #64b4ff;
    color: #9fd0ff;
  }

  .b2b-badge {
    display: inline-block;
    padding: 3px 8px;
    border-radius: 4px;
    font-family: monospace;
    font-size: 11px;
    font-weight: 800;
    background: rgba(255,51,102,.3);
    color: #ff88aa;
    margin-left: 6px;
    vertical-align: middle;
  }

  /* Streamlit metrics 放大 */
  [data-testid="stMetric"] {
    background: linear-gradient(135deg, #1a2842 0%, #0d1a2f 100%);
    border: 2px solid #2a4066;
    border-radius: 12px;
    padding: 14px;
  }
  [data-testid="stMetricValue"] {
    font-family: monospace !important;
    font-size: 32px !important;
    font-weight: 900 !important;
    color: #ffffff !important;
  }
  [data-testid="stMetricLabel"] {
    font-size: 15px !important;
    font-weight: 700 !important;
    color: #ffa500 !important;
    margin-bottom: 6px !important;
  }
  [data-testid="stMetricDelta"] {
    font-size: 14px !important;
    font-weight: 700 !important;
  }

  /* DataFrame */
  [data-testid="stDataFrame"] { font-size: 14px !important; }

  /* 成功/資訊提示框 */
  [data-testid="stNotificationContentSuccess"] { font-size: 15px; }

  /* ═════ PLAYOFF BRACKET ═════ */
  .bk-wrap{background:#060b17;border:1px solid #1e293b;border-radius:12px;padding:22px 14px;
    box-shadow:inset 0 2px 20px rgba(0,0,0,.5);overflow-x:auto;}
  .bk-meta-row{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;
    font-family:monospace;font-size:11px;color:#64748b;margin-bottom:12px;padding:0 4px;}
  .bk-meta-row .west{color:#fbbf24;font-weight:700;letter-spacing:2px;}
  .bk-meta-row .east{color:#60a5fa;font-weight:700;letter-spacing:2px;}
  .bk-round-row{display:grid;grid-template-columns:155px 155px 155px 180px 155px 155px 155px;
    gap:12px;margin-bottom:8px;min-width:1120px;}
  .bk-round-row>span{font-family:monospace;font-size:10px;color:#475569;letter-spacing:2px;text-align:center;}
  .bk-grid{display:grid;grid-template-columns:155px 155px 155px 180px 155px 155px 155px;
    gap:12px;min-width:1120px;align-items:center;}
  .bk-col{display:flex;flex-direction:column;}
  .bk-col.r1{gap:10px;}
  .bk-col.r2{gap:90px;justify-content:center;}
  .bk-col.cf,.bk-col.fn{justify-content:center;align-items:center;}
  .bk-card{background:#121826;border:1px solid #1e293b;border-radius:8px;padding:10px;
    min-height:112px;transition:border-color .2s;}
  .bk-card:hover{border-color:#334155;}
  .bk-card.is-finals{background:linear-gradient(135deg,#1a2332,#0f172a);border-color:#fbbf24;
    box-shadow:0 0 24px rgba(251,191,36,.2);}
  .bk-card-hdr{display:flex;justify-content:space-between;align-items:center;
    font-family:monospace;font-size:9px;color:#475569;letter-spacing:1px;margin-bottom:5px;}
  .bk-side{display:grid;grid-template-columns:auto 1fr auto;align-items:center;gap:6px;
    padding:4px 6px;border-radius:5px;margin-bottom:2px;border:1px solid transparent;}
  .bk-side.winner{background:rgba(16,185,129,.08);border-color:rgba(16,185,129,.35);}
  .bk-side.loser{background:rgba(100,116,139,.05);}
  .bk-side .seed{font-family:monospace;font-size:10px;color:#64748b;min-width:20px;}
  .bk-side.winner .seed{color:#34d399;}
  .bk-side .tn{font-size:13px;font-weight:700;color:#e2e8f0 !important;letter-spacing:.5px;}
  .bk-side.loser .tn{color:#64748b !important;}
  .bk-side .tp{font-family:monospace;font-size:13px;font-weight:700;color:#e2e8f0;}
  .bk-side.winner .tp{color:#10b981;}
  .bk-side.loser .tp{color:#64748b;}
  .bk-vs{font-family:monospace;font-size:8px;color:#475569;text-align:center;margin:1px 0;letter-spacing:1px;}
  .bk-metabar{display:flex;justify-content:space-between;align-items:center;margin-top:5px;
    padding-top:5px;border-top:1px solid #1e293b;font-family:monospace;font-size:9px;color:#64748b;}
  .bk-players{margin-top:5px;padding-top:5px;border-top:1px solid #1e293b;
    display:flex;flex-direction:column;gap:3px;}
  .bk-pl{display:flex;justify-content:space-between;align-items:center;font-size:10px;}
  .bk-pl .pnm{color:#cbd5e1 !important;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;}
  .bk-pl .st{color:#fbbf24;margin-right:4px;font-size:9px;}
  .bk-pl .pbd{font-family:monospace;font-size:8px;font-weight:700;padding:1px 5px;border-radius:2px;
    letter-spacing:1px;margin-left:4px;white-space:nowrap;}
  .bk-pl .pbd.healthy{background:rgba(16,185,129,.15);color:#34d399;}
  .bk-pl .pbd.out{background:rgba(239,68,68,.15);color:#f87171;}
  .bk-pl .pbd.d2d{background:rgba(251,191,36,.15);color:#fbbf24;}
  .bk-fn-label{font-family:monospace;font-size:10px;color:#fbbf24;letter-spacing:3px;
    text-align:center;margin-bottom:7px;font-weight:700;}
</style>
""", unsafe_allow_html=True)


# ── 設定 ──────────────────────────────────────────────
def _secret(key, default=""):
    try:
        v = st.secrets.get(key)
        if v:
            return v
    except Exception:
        pass
    return os.environ.get(key, default)

DATA_REPO  = _secret("DATA_REPO",  "datadigshawn/nba_dashboard")
DATA_TAG   = _secret("DATA_TAG",   "data-latest")
DATA_ASSET = "nba_data.json"


# ── 資料載入 ────────────────────────────────────────────
@st.cache_data(ttl=300, show_spinner=False)
def load_nba_data() -> dict:
    """
    從 GitHub Release 抓 nba_data.json。
    優先走直接下載 URL（無 API rate limit），失敗才 fallback 到 API。
    """
    # 方法 1: 直接下載 URL（不走 api.github.com，避免 60req/hr 限制）
    # GitHub releases assets 的直接 URL 格式是：
    #   https://github.com/<owner>/<repo>/releases/download/<tag>/<filename>
    direct_url = f"https://github.com/{DATA_REPO}/releases/download/{DATA_TAG}/{DATA_ASSET}"
    try:
        req = urllib.request.Request(direct_url, headers={
            "User-Agent": "Mozilla/5.0 (Streamlit NBA Dashboard)",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        # 從 HTTP header 找發布時間（備用）
        last_mod = r.headers.get("Last-Modified", "")
        data["_synced_at"] = last_mod or "unknown"
        data["_source"] = "direct"
        return data
    except Exception as direct_err:
        # 方法 2: Fallback → API（有 rate limit 風險但更精確）
        api = f"https://api.github.com/repos/{DATA_REPO}/releases/tags/{DATA_TAG}"
        try:
            req = urllib.request.Request(api, headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "Mozilla/5.0 (Streamlit NBA Dashboard)",
            })
            with urllib.request.urlopen(req, timeout=10) as r:
                release = json.loads(r.read())
            asset_url = None
            for a in release.get("assets", []):
                if a.get("name") == DATA_ASSET:
                    asset_url = a.get("browser_download_url")
                    break
            if not asset_url:
                return {"_error": f"找不到 asset: {DATA_ASSET}"}
            with urllib.request.urlopen(asset_url, timeout=15) as r:
                data = json.loads(r.read())
            data["_synced_at"] = release.get("published_at", "")
            data["_source"] = "api-fallback"
            return data
        except Exception as api_err:
            return {"_error": f"direct: {direct_err} | api: {api_err}"}


@st.cache_data(ttl=60, show_spinner=False)
def load_scoreboard():
    """直接呼叫 ESPN 公開 API 取即時比分。"""
    try:
        url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
        r = httpx.get(url, timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


# ── UI 元件 ────────────────────────────────────────────
def render_game_card(g: dict):
    away = g.get("away", "")
    home = g.get("home", "")
    away_prob = g.get("away_prob", 0)
    home_prob = g.get("home_prob", 0)
    away_rec  = g.get("away_record", "")
    home_rec  = g.get("home_record", "")
    spread    = g.get("pred_spread", 0)
    total     = g.get("pred_total", 0)
    status    = g.get("status", "")
    b2b_away  = g.get("b2b_away", False)
    b2b_home  = g.get("b2b_home", False)

    # 贏隊框加高亮
    if away_prob > home_prob:
        away_cls, home_cls = "winner-box", "loser-box"
    elif home_prob > away_prob:
        away_cls, home_cls = "loser-box", "winner-box"
    else:
        away_cls = home_cls = ""

    away_b2b = '<span class="b2b-badge">B2B</span>' if b2b_away else ""
    home_b2b = '<span class="b2b-badge">B2B</span>' if b2b_home else ""

    # 讓分符號（主隊為基準）
    if spread >= 0:
        spread_txt = f"{home[:3].upper()} -{spread:.1f}"
    else:
        spread_txt = f"{away[:3].upper()} -{abs(spread):.1f}"

    status_txt = status if status else "上場"

    card = f"""
    <div class="game-card">
      <span class="game-status">{status_txt}</span>
      <div class="teams-row">
        <div class="team-box {away_cls}">
          <div class="team-name">{away}{away_b2b}</div>
          <div class="team-record">{away_rec}</div>
          <div class="team-prob">{away_prob:.1f}%</div>
        </div>
        <div class="vs-divider">@</div>
        <div class="team-box {home_cls}">
          <div class="team-name">{home}{home_b2b}</div>
          <div class="team-record">{home_rec}</div>
          <div class="team-prob">{home_prob:.1f}%</div>
        </div>
      </div>
      <div style="margin-top:14px">
        <span class="spread-badge">讓分 {spread_txt}</span>
        <span class="total-badge">大小 {total:.0f}</span>
      </div>
    </div>
    """
    st.markdown(card, unsafe_allow_html=True)


# ── 季後賽版面 ──────────────────────────────────────────
def _bk_badge_class(status: str) -> str:
    s = (status or "").lower()
    if s in ("healthy", "active", ""):
        return "healthy"
    if s in ("out", "season-ending", "injured reserve"):
        return "out"
    if "day" in s or "probable" in s or "questionable" in s:
        return "d2d"
    return "d2d"


def _bk_side(team: dict, is_winner: bool) -> str:
    cls = "winner" if is_winner else "loser"
    return (
        f'<div class="bk-side {cls}">'
        f'<span class="seed">#{team.get("seed","")}</span>'
        f'<span class="tn">{team["abbrev"]}</span>'
        f'<span class="tp">{team["advance_prob"]:.1f}%</span>'
        f'</div>'
    )


def _bk_player_row(team: dict) -> str:
    star = team.get("star") or {}
    name = star.get("name", "")
    if not name:
        return ""
    bc = _bk_badge_class(star.get("status", ""))
    lbl = star.get("status", "") or "Healthy"
    return (
        f'<div class="bk-pl">'
        f'<span class="pnm"><span class="st">★</span>{name}</span>'
        f'<span class="pbd {bc}">{lbl}</span>'
        f'</div>'
    )


def _bk_card(matchup: dict, label: str, show_players: bool = True) -> str:
    top, bot = matchup["top"], matchup["bot"]
    top_wins = top["advance_prob"] >= bot["advance_prob"]
    games = f'{matchup.get("expected_games", 0):.1f} 場' if matchup.get("expected_games") else ""
    elo_line = f'Elo {top.get("elo","?")} vs {bot.get("elo","?")}'
    players_html = ""
    if show_players:
        players_html = f'<div class="bk-players">{_bk_player_row(top)}{_bk_player_row(bot)}</div>'
    return (
        f'<div class="bk-card">'
        f'<div class="bk-card-hdr"><span>{label}</span><span>{games}</span></div>'
        f'{_bk_side(top, top_wins)}'
        f'<div class="bk-vs">— VS —</div>'
        f'{_bk_side(bot, not top_wins)}'
        f'<div class="bk-metabar"><span>{elo_line}</span></div>'
        f'{players_html}'
        f'</div>'
    )


def _bk_finals_card(finals: dict) -> str:
    w, e = finals["west"], finals["east"]
    west_wins = w["advance_prob"] >= e["advance_prob"]
    games = f'{finals.get("expected_games", 0):.1f} 場' if finals.get("expected_games") else ""
    return (
        f'<div class="bk-card is-finals">'
        f'<div class="bk-fn-label">★ TOTAL FINALS ★</div>'
        f'<div class="bk-card-hdr"><span>W vs E</span><span>{games}</span></div>'
        f'{_bk_side(w, west_wins)}'
        f'<div class="bk-vs">— VS —</div>'
        f'{_bk_side(e, not west_wins)}'
        f'<div class="bk-metabar"><span>Elo {w.get("elo","?")} vs {e.get("elo","?")}</span></div>'
        f'</div>'
    )


def render_bracket(bracket: dict):
    if not bracket or bracket.get("error") or not bracket.get("west"):
        return
    west, east, finals = bracket["west"], bracket["east"], bracket["finals"]
    gen = (bracket.get("generated_at") or "").replace("T", " ")
    n_sims = bracket.get("n_sims", 0)

    r1w = "".join(_bk_card(m, f'#{m["top"]["seed"]} vs #{m["bot"]["seed"]}') for m in west["r1"])
    r2w = "".join(_bk_card(m, "西區準決賽") for m in west["r2"])
    cfw = "".join(_bk_card(m, "西區冠軍賽") for m in west["conf_finals"])
    fn  = _bk_finals_card(finals) if finals else ""
    cfe = "".join(_bk_card(m, "東區冠軍賽") for m in east["conf_finals"])
    r2e = "".join(_bk_card(m, "東區準決賽") for m in east["r2"])
    r1e = "".join(_bk_card(m, f'#{m["top"]["seed"]} vs #{m["bot"]["seed"]}') for m in east["r1"])

    html = f"""
    <div class="bk-wrap">
      <div class="bk-meta-row">
        <span class="west">◤ WEST · 西區</span>
        <span>Monte Carlo n={n_sims:,} · {gen}</span>
        <span class="east">EAST · 東區 ◢</span>
      </div>
      <div class="bk-round-row">
        <span>R1 · 首輪</span><span>R2 · 準決賽</span><span>西區冠軍</span>
        <span style="color:#fbbf24;">TOTAL FINALS</span>
        <span>東區冠軍</span><span>R2 · 準決賽</span><span>R1 · 首輪</span>
      </div>
      <div class="bk-grid">
        <div class="bk-col r1">{r1w}</div>
        <div class="bk-col r2">{r2w}</div>
        <div class="bk-col cf">{cfw}</div>
        <div class="bk-col fn">{fn}</div>
        <div class="bk-col cf">{cfe}</div>
        <div class="bk-col r2">{r2e}</div>
        <div class="bk-col r1">{r1e}</div>
      </div>
    </div>
    """
    st.markdown(html, unsafe_allow_html=True)


# ── 主畫面 ─────────────────────────────────────────────
try:
    st.title("🏀 NBA 預測儀表板")
    st.caption(f"Elo + XGBoost 機器學習 · 每日 09:00 自動更新 · {datetime.now(TZ_TAIPEI).strftime('%Y-%m-%d %H:%M')} 台北")

    data = load_nba_data()

    if "_error" in data:
        st.error(f"⚠️ 資料載入失敗：{data['_error']}")
        st.info(f"請確認 Mac mini 端的 sync_data.py 已推送至 `{DATA_REPO}:{DATA_TAG}`")
        st.stop()

    synced = data.get("_synced_at", "?")
    st.success(f"✅ 資料已同步（GitHub Release · published {synced}）")

    games   = data.get("games", [])
    edges   = data.get("edges", [])
    elo     = data.get("elo_teams", {})
    bt      = data.get("backtest", {})
    bracket = data.get("playoff_bracket", {})

    # ─── 模型效能總覽 ───
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("回測場次", bt.get("games_tested", 0))
    c2.metric("整體勝率", f"{bt.get('all_wr', 0):.1f}%")
    c3.metric("強信號勝率", f"{bt.get('strong_wr', 0):.1f}%",
              delta=f"{bt.get('strong_count', 0)} 場")
    c4.metric("超強信號", f"{bt.get('vstrong_wr', 0):.1f}%",
              delta=f"{bt.get('vstrong_count', 0)} 場")

    st.markdown("---")

    # ─── 今日比賽 ───
    st.subheader(f"🎯 今日比賽預測（{len(games)} 場）")

    if not games:
        st.info("今天沒有 NBA 比賽。")
    else:
        for g in games:
            render_game_card(g)

    # ─── 🏆 季後賽版面 ───
    if bracket and not bracket.get("error") and bracket.get("west"):
        st.markdown("---")
        st.subheader("🏆 季後賽版面 — Monte Carlo 推演")
        st.caption("每張卡片顯示該隊挺進下一輪的機率（勝率%），底部顯示該隊球星與 ESPN 傷病狀態。")
        render_bracket(bracket)

    # ─── Polymarket 邊際 ───
    if edges:
        st.markdown("---")
        st.subheader(f"💰 Polymarket 邊際機會（{len(edges)} 個）")
        df_edges = pd.DataFrame(edges)
        st.dataframe(df_edges, use_container_width=True, hide_index=True)
    else:
        st.caption("💬 目前 Polymarket 沒有匹配到的邊際機會")

    # ─── Elo 排名 ───
    st.markdown("---")
    st.subheader("🏆 Elo 評分排名")
    if elo:
        elo_sorted = sorted(elo.items(), key=lambda x: x[1], reverse=True)
        df_elo = pd.DataFrame(elo_sorted, columns=["球隊", "Elo"])
        df_elo.index = df_elo.index + 1
        df_elo.index.name = "排名"

        # 用 Plotly 畫橫向 bar chart（前 15 名）
        top15 = df_elo.head(15)
        fig = go.Figure()
        fig.add_trace(go.Bar(
            y=top15["球隊"][::-1],
            x=top15["Elo"][::-1],
            orientation="h",
            marker=dict(
                color=top15["Elo"][::-1],
                colorscale=[[0, "#4a7a9b"], [0.5, "#ffa500"], [1, "#ff6b35"]],
                showscale=False,
            ),
            text=top15["Elo"][::-1].astype(int),
            textposition="inside",
        ))
        fig.update_layout(
            height=500,
            template="plotly_dark",
            paper_bgcolor="#0a1520",
            plot_bgcolor="#0a1520",
            xaxis=dict(title="Elo Rating", gridcolor="#1a3450"),
            yaxis=dict(title="", gridcolor="#1a3450"),
            margin=dict(l=0, r=0, t=20, b=0),
        )
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

        with st.expander(f"📋 完整 Elo 排名表（{len(df_elo)} 隊）"):
            st.dataframe(df_elo, use_container_width=True)

    # ─── 最近比賽回測 ───
    if bt.get("recent"):
        st.markdown("---")
        st.subheader("🕒 最近比賽回測結果")
        df_recent = pd.DataFrame(bt["recent"])
        # 美化顯示
        if not df_recent.empty and "correct" in df_recent.columns:
            df_recent["結果"] = df_recent["correct"].apply(lambda x: "✅" if x else "❌")
            df_recent = df_recent.rename(columns={
                "date": "日期", "away": "客隊", "home": "主隊",
                "conf": "信心度", "pick": "預測", "winner": "贏家", "score": "比分",
            })
            df_recent = df_recent[["日期","客隊","主隊","預測","信心度","贏家","比分","結果"]]
            st.dataframe(df_recent, use_container_width=True, hide_index=True)

    st.markdown("---")
    st.caption(f"資料來源：ESPN API + Polymarket + GitHub Release `{DATA_REPO}:{DATA_TAG}`")
    st.caption("🏀 本系統僅供娛樂研究，不構成投注建議。")

except Exception as e:
    st.error(f"App 發生未預期錯誤：{e}")
    st.code(traceback.format_exc())

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


# ── 樣式（仿 Pionex 風格，手機友善）──────────────────────
st.markdown("""
<style>
  .stApp { background: #0a1020; }
  h1 { color: #ff6b35 !important; font-size: 32px !important; letter-spacing: 1px; }
  h2, h3 { color: #ffa500 !important; font-size: 22px !important; }

  .game-card {
    background: linear-gradient(135deg, #162031 0%, #0a1520 100%);
    border: 1px solid #2a4060;
    border-radius: 12px;
    padding: 16px;
    margin-bottom: 12px;
  }
  .teams-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 10px;
  }
  .team-box { flex: 1; text-align: center; }
  .team-name { font-size: 18px; font-weight: 700; color: #fff; }
  .team-record { font-size: 12px; color: #88a8c8; margin-top: 2px; }
  .team-prob { font-family: monospace; font-size: 28px; font-weight: 900; color: #fff; margin-top: 4px; }

  .vs-divider {
    font-family: monospace;
    font-size: 14px;
    color: #4a7a9b;
    padding: 0 16px;
  }

  .winner-hilite { color: #00ff88 !important; }
  .loser-dim { color: #88a8c8 !important; }

  .spread-badge {
    display: inline-block;
    padding: 4px 10px;
    border-radius: 4px;
    font-family: monospace;
    font-size: 13px;
    font-weight: 700;
    background: rgba(255,167,0,.15);
    border: 1px solid #ffa500;
    color: #ffa500;
    margin-right: 6px;
  }
  .total-badge {
    display: inline-block;
    padding: 4px 10px;
    border-radius: 4px;
    font-family: monospace;
    font-size: 13px;
    font-weight: 700;
    background: rgba(100,160,220,.15);
    border: 1px solid #64a0dc;
    color: #64a0dc;
  }
  .b2b-badge {
    display: inline-block;
    padding: 2px 6px;
    border-radius: 3px;
    font-family: monospace;
    font-size: 10px;
    background: rgba(255,51,102,.2);
    color: #ff6688;
    margin-left: 4px;
  }

  [data-testid="stMetricValue"] {
    font-family: monospace !important;
    font-size: 22px !important;
    color: #fff !important;
  }
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
    """從 GitHub Release 抓 nba_data.json。"""
    api = f"https://api.github.com/repos/{DATA_REPO}/releases/tags/{DATA_TAG}"
    try:
        req = urllib.request.Request(api, headers={"Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            release = json.loads(r.read())
    except Exception as e:
        return {"_error": f"release API: {e}"}

    asset_url = None
    for a in release.get("assets", []):
        if a.get("name") == DATA_ASSET:
            asset_url = a.get("browser_download_url")
            break
    if not asset_url:
        return {"_error": f"找不到 asset: {DATA_ASSET}"}

    try:
        with urllib.request.urlopen(asset_url, timeout=15) as r:
            data = json.loads(r.read())
        data["_synced_at"] = release.get("published_at", "")
        return data
    except Exception as e:
        return {"_error": f"下載失敗: {e}"}


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

    # 標記勝方
    if away_prob > home_prob:
        away_cls, home_cls = "winner-hilite", "loser-dim"
    elif home_prob > away_prob:
        away_cls, home_cls = "loser-dim", "winner-hilite"
    else:
        away_cls = home_cls = ""

    away_b2b = '<span class="b2b-badge">B2B</span>' if b2b_away else ""
    home_b2b = '<span class="b2b-badge">B2B</span>' if b2b_home else ""

    # 讓分符號
    if spread >= 0:
        spread_txt = f"{home[:3].upper()} -{spread:.1f}"
    else:
        spread_txt = f"{away[:3].upper()} -{abs(spread):.1f}"

    card = f"""
    <div class="game-card">
      <div style="font-size:12px;color:#88a8c8;margin-bottom:8px">{status}</div>
      <div class="teams-row">
        <div class="team-box">
          <div class="team-name {away_cls}">{away}{away_b2b}</div>
          <div class="team-record">{away_rec}</div>
          <div class="team-prob {away_cls}">{away_prob:.1f}%</div>
        </div>
        <div class="vs-divider">@</div>
        <div class="team-box">
          <div class="team-name {home_cls}">{home}{home_b2b}</div>
          <div class="team-record">{home_rec}</div>
          <div class="team-prob {home_cls}">{home_prob:.1f}%</div>
        </div>
      </div>
      <div style="margin-top:12px">
        <span class="spread-badge">讓分 {spread_txt}</span>
        <span class="total-badge">大小 {total:.0f}</span>
      </div>
    </div>
    """
    st.markdown(card, unsafe_allow_html=True)


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

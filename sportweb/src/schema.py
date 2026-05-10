"""
sportWeb 資料結構（與 autobots_NBA 對接格式）

使用方式：
    from schema import GameOdds, OddsLine, OddsSnapshot
    import json
    # 寫入：json.dumps(snap.to_dict(), ensure_ascii=False, indent=2)
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional


@dataclass
class OddsLine:
    """單一賠率 line。根據 market 類型用不同欄位。

    - moneyline: 只用 away / home, line=0
    - spread:    用 away / home + line（如 17.5 代表主隊讓 17.5 分）
    - total:     用 over / under + line（如 120.5 代表 O/U 120.5）
    """
    line: float = 0.0
    away: Optional[float] = None
    home: Optional[float] = None
    over: Optional[float] = None
    under: Optional[float] = None

    def to_dict(self) -> dict:
        d = {"line": self.line}
        for k in ("away", "home", "over", "under"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        return d


@dataclass
class GameOdds:
    """一場比賽的所有賠率。"""
    game_id: str                                        # e.g. "335"
    away: str                                           # 客隊
    home: str                                           # 主隊
    start_time: str = ""                                # ISO 格式 "2026-04-11T07:30:00"
    moneyline: Optional[OddsLine] = None
    spreads: list[OddsLine] = field(default_factory=list)
    totals: list[OddsLine] = field(default_factory=list)
    source_url: str = ""
    raw_html_snippet: Optional[str] = None

    def to_dict(self) -> dict:
        game_date = parse_game_date_ymd(self.start_time)
        d = {
            "game_id": self.game_id,
            "away": self.away,
            "home": self.home,
            "start_time": self.start_time,
            "source_url": self.source_url,
        }
        if game_date:
            d["game_date"] = game_date
            d["game_key"] = build_game_key(game_date, self.away, self.home)
        if self.moneyline:
            d["moneyline"] = self.moneyline.to_dict()
        if self.spreads:
            d["spreads"] = [s.to_dict() for s in self.spreads]
        if self.totals:
            d["totals"] = [t.to_dict() for t in self.totals]
        if self.raw_html_snippet:
            d["_raw"] = self.raw_html_snippet[:500]
        return d

    def implied_prob(self) -> Optional[dict]:
        """從 moneyline 推算市場隱含機率（含 overround）。"""
        if not self.moneyline or self.moneyline.away is None or self.moneyline.home is None:
            return None
        a = 1 / self.moneyline.away
        h = 1 / self.moneyline.home
        total = a + h
        return {
            "away_raw": round(a, 4),
            "home_raw": round(h, 4),
            "overround": round(total - 1.0, 4),
            "away_norm": round(a / total, 4),   # 去掉 overround 的公平機率
            "home_norm": round(h / total, 4),
        }


@dataclass
class OddsSnapshot:
    """單次抓取的快照（可序列化為 JSON）。"""
    fetched_at: str                                     # ISO
    league: str                                         # "NBA"
    games: list[GameOdds] = field(default_factory=list)
    source_url: str = ""
    cf_passed: bool = True
    error: Optional[str] = None

    @classmethod
    def now(cls, league: str = "NBA", source_url: str = "") -> OddsSnapshot:
        return cls(
            fetched_at=datetime.now().isoformat(timespec="seconds"),
            league=league,
            source_url=source_url,
        )

    def to_dict(self) -> dict:
        d = {
            "fetched_at": self.fetched_at,
            "league": self.league,
            "source_url": self.source_url,
            "cf_passed": self.cf_passed,
            "games": [g.to_dict() for g in self.games],
        }
        if self.error:
            d["error"] = self.error
        return d


def parse_game_date_ymd(value: str) -> str:
    """Normalize YYYYMMDD / YYYY-MM-DD / ISO8601 to YYYYMMDD."""
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) == 8 and value.isdigit():
        return value
    if len(value) >= 10 and value[4] == "-" and value[7] == "-":
        return value[:10].replace("-", "")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y%m%d")
    except ValueError:
        return ""


def build_game_key(game_date: str, away: str, home: str) -> str:
    """Stable cross-project key: YYYYMMDD|Away|Home."""
    game_date = parse_game_date_ymd(game_date)
    away = to_espn_name(away)
    home = to_espn_name(home)
    if not (game_date and away and home):
        return ""
    return f"{game_date}|{away}|{home}"


# ── 隊名中 ↔ 英對照（用於對接 autobots_NBA 的 ESPN 命名）────
# 全 30 隊 + 台灣/大陸/英文縮寫/暱稱 多版本
# 命名原則：所有變體 → 標準 ESPN 英文名
TEAM_NAME_MAP = {
    # ═══════ Atlantic Division ═══════
    # Boston Celtics
    "波士頓塞爾提克": "Boston Celtics",
    "波士頓塞提克":   "Boston Celtics",
    "塞爾提克":       "Boston Celtics",
    "塞提克":         "Boston Celtics",
    "Boston":         "Boston Celtics",
    "BOS":            "Boston Celtics",
    # Brooklyn Nets
    "布魯克林籃網":   "Brooklyn Nets",
    "籃網":           "Brooklyn Nets",
    "Brooklyn":       "Brooklyn Nets",
    "BKN":            "Brooklyn Nets",
    # New York Knicks
    "紐約尼克":       "New York Knicks",
    "紐約尼克斯":     "New York Knicks",
    "尼克":           "New York Knicks",
    "尼克斯":         "New York Knicks",
    "New York":       "New York Knicks",
    "NYK":            "New York Knicks",
    # Philadelphia 76ers
    "費城 76 人":     "Philadelphia 76ers",
    "費城76人":       "Philadelphia 76ers",
    "七六人":         "Philadelphia 76ers",
    "76人":           "Philadelphia 76ers",
    "Philly":         "Philadelphia 76ers",
    "PHI":            "Philadelphia 76ers",
    # Toronto Raptors
    "多倫多暴龍":     "Toronto Raptors",
    "多倫多猛龍":     "Toronto Raptors",
    "暴龍":           "Toronto Raptors",
    "猛龍":           "Toronto Raptors",
    "Toronto":        "Toronto Raptors",
    "TOR":            "Toronto Raptors",

    # ═══════ Central Division ═══════
    # Chicago Bulls
    "芝加哥公牛":     "Chicago Bulls",
    "公牛":           "Chicago Bulls",
    "Chicago":        "Chicago Bulls",
    "CHI":            "Chicago Bulls",
    # Cleveland Cavaliers
    "克里夫蘭騎士":   "Cleveland Cavaliers",
    "騎士":           "Cleveland Cavaliers",
    "Cleveland":      "Cleveland Cavaliers",
    "CLE":            "Cleveland Cavaliers",
    # Detroit Pistons
    "底特律活塞":     "Detroit Pistons",
    "活塞":           "Detroit Pistons",
    "Detroit":        "Detroit Pistons",
    "DET":            "Detroit Pistons",
    # Indiana Pacers
    "印第安納溜馬":   "Indiana Pacers",
    "印第安納步行者": "Indiana Pacers",
    "溜馬":           "Indiana Pacers",
    "步行者":         "Indiana Pacers",
    "Indiana":        "Indiana Pacers",
    "IND":            "Indiana Pacers",
    # Milwaukee Bucks
    "密爾瓦基公鹿":   "Milwaukee Bucks",
    "公鹿":           "Milwaukee Bucks",
    "Milwaukee":      "Milwaukee Bucks",
    "MIL":            "Milwaukee Bucks",

    # ═══════ Southeast Division ═══════
    # Atlanta Hawks
    "亞特蘭大老鷹":   "Atlanta Hawks",
    "老鷹":           "Atlanta Hawks",
    "Atlanta":        "Atlanta Hawks",
    "ATL":            "Atlanta Hawks",
    # Charlotte Hornets
    "夏洛特黃蜂":     "Charlotte Hornets",
    "黃蜂":           "Charlotte Hornets",
    "Charlotte":      "Charlotte Hornets",
    "CHA":            "Charlotte Hornets",
    # Miami Heat
    "邁阿密熱火":     "Miami Heat",
    "熱火":           "Miami Heat",
    "Miami":          "Miami Heat",
    "MIA":            "Miami Heat",
    # Orlando Magic
    "奧蘭多魔術":     "Orlando Magic",
    "魔術":           "Orlando Magic",
    "Orlando":        "Orlando Magic",
    "ORL":            "Orlando Magic",
    # Washington Wizards
    "華盛頓巫師":     "Washington Wizards",
    "華盛頓奇才":     "Washington Wizards",
    "巫師":           "Washington Wizards",
    "奇才":           "Washington Wizards",
    "Washington":     "Washington Wizards",
    "WAS":            "Washington Wizards",

    # ═══════ Northwest Division ═══════
    # Denver Nuggets
    "丹佛金塊":       "Denver Nuggets",
    "丹佛掘金":       "Denver Nuggets",
    "金塊":           "Denver Nuggets",
    "掘金":           "Denver Nuggets",
    "Denver":         "Denver Nuggets",
    "DEN":            "Denver Nuggets",
    # Minnesota Timberwolves
    "明尼蘇達灰狼":   "Minnesota Timberwolves",
    "明尼蘇達森林狼": "Minnesota Timberwolves",
    "灰狼":           "Minnesota Timberwolves",
    "森林狼":         "Minnesota Timberwolves",
    "Minnesota":      "Minnesota Timberwolves",
    "MIN":            "Minnesota Timberwolves",
    # Oklahoma City Thunder
    "奧克拉荷馬雷霆": "Oklahoma City Thunder",
    "奧克拉荷馬":     "Oklahoma City Thunder",
    "雷霆":           "Oklahoma City Thunder",
    "Oklahoma":       "Oklahoma City Thunder",
    "OKC":            "Oklahoma City Thunder",
    # Portland Trail Blazers
    "波特蘭拓荒者":   "Portland Trail Blazers",
    "波特蘭開拓者":   "Portland Trail Blazers",
    "拓荒者":         "Portland Trail Blazers",
    "開拓者":         "Portland Trail Blazers",
    "拓荒":           "Portland Trail Blazers",
    "Portland":       "Portland Trail Blazers",
    "POR":            "Portland Trail Blazers",
    # Utah Jazz
    "猶他爵士":       "Utah Jazz",
    "爵士":           "Utah Jazz",
    "Utah":           "Utah Jazz",
    "UTA":            "Utah Jazz",

    # ═══════ Pacific Division ═══════
    # Golden State Warriors
    "金州勇士":       "Golden State Warriors",
    "勇士":           "Golden State Warriors",
    "Golden State":   "Golden State Warriors",
    "GSW":            "Golden State Warriors",
    # LA Clippers
    "洛杉磯快艇":     "LA Clippers",
    "洛杉磯快船":     "LA Clippers",
    "快艇":           "LA Clippers",
    "快船":           "LA Clippers",
    "Clippers":       "LA Clippers",
    "LAC":            "LA Clippers",
    # Los Angeles Lakers
    "洛杉磯湖人":     "Los Angeles Lakers",
    "湖人":           "Los Angeles Lakers",
    "Lakers":         "Los Angeles Lakers",
    "LAL":            "Los Angeles Lakers",
    # Phoenix Suns
    "鳳凰城太陽":     "Phoenix Suns",
    "太陽":           "Phoenix Suns",
    "Phoenix":        "Phoenix Suns",
    "PHX":            "Phoenix Suns",
    # Sacramento Kings
    "沙加緬度國王":   "Sacramento Kings",
    "薩克拉門托國王": "Sacramento Kings",
    "國王":           "Sacramento Kings",
    "Sacramento":     "Sacramento Kings",
    "SAC":            "Sacramento Kings",

    # ═══════ Southwest Division ═══════
    # Dallas Mavericks
    "達拉斯獨行俠":   "Dallas Mavericks",
    "達拉斯小牛":     "Dallas Mavericks",
    "獨行俠":         "Dallas Mavericks",
    "小牛":           "Dallas Mavericks",
    "黑馬":           "Dallas Mavericks",  # 早期譯名
    "Dallas":         "Dallas Mavericks",
    "DAL":            "Dallas Mavericks",
    # Houston Rockets
    "休斯頓火箭":     "Houston Rockets",
    "休士頓火箭":     "Houston Rockets",
    "火箭":           "Houston Rockets",
    "Houston":        "Houston Rockets",
    "HOU":            "Houston Rockets",
    # Memphis Grizzlies
    "孟菲斯灰熊":     "Memphis Grizzlies",
    "灰熊":           "Memphis Grizzlies",
    "Memphis":        "Memphis Grizzlies",
    "MEM":            "Memphis Grizzlies",
    # New Orleans Pelicans
    "紐奧良鵜鶘":     "New Orleans Pelicans",
    "新奧爾良鵜鶘":   "New Orleans Pelicans",
    "鵜鶘":           "New Orleans Pelicans",
    "New Orleans":    "New Orleans Pelicans",
    "NOP":            "New Orleans Pelicans",
    # San Antonio Spurs
    "聖安東尼奧馬刺": "San Antonio Spurs",
    "馬刺":           "San Antonio Spurs",
    "雙塔":           "San Antonio Spurs",  # 俗稱（Robinson + Duncan / Wembanyama）
    "San Antonio":    "San Antonio Spurs",
    "SAS":            "San Antonio Spurs",
}


def to_espn_name(ch: str) -> str:
    """中文/英文隊名 → ESPN 標準英文名；找不到時原樣回傳。

    策略：
    1. 完全匹配（最精準）
    2. 部分包含匹配（例如「洛杉磯湖人（主場）」→ 洛杉磯湖人）
    3. 回傳原字串（讓呼叫方決定怎麼處理）
    """
    ch = ch.strip()
    if not ch:
        return ""
    if ch in TEAM_NAME_MAP:
        return TEAM_NAME_MAP[ch]
    # fuzzy: 部分匹配（優先長 key 以免短詞誤匹配）
    for k in sorted(TEAM_NAME_MAP.keys(), key=len, reverse=True):
        if k in ch or ch in k:
            return TEAM_NAME_MAP[k]
    return ch


def all_teams_espn() -> list:
    """回傳 30 個 ESPN 標準英文隊名（去重）。"""
    return sorted(set(TEAM_NAME_MAP.values()))

import os
import math
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from aiohttp import web


# =========================================================
# CONFIG
# =========================================================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = os.getenv("GUILD_ID", "").strip()

FOOTBALL_DATA_API_KEY = (
    os.getenv("FOOTBALL_DATA_API_KEY", "").strip()
    or os.getenv("FOOTBALL_DATA_TOKEN", "").strip()
    or os.getenv("FOOTBALL_DATA_KEY", "").strip()
)

# Có thể đổi trên Railway Variables.
# Nếu không tạo biến ADMIN_PASSWORD thì mặc định là provip👑
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "provip👑")

# Nếu muốn khóa admin theo Discord User ID:
# ADMIN_USER_IDS=123456789,987654321
ADMIN_USER_IDS_RAW = os.getenv("ADMIN_USER_IDS", "").strip()

API_BASE = "https://api.football-data.org/v4"

# Bot hiển thị giờ Việt Nam.
# Nếu muốn giờ Hàn Quốc đổi thành +9.
DISPLAY_TIMEZONE = timezone(timedelta(hours=7))

STARTING_BALANCE = 100_000
STARTER_BONUS = 1_000
DAILY_BONUS = 50_000
BET_PAYOUT = 2

MANCHESTER_CITY_TEAM_ID = 65

# Railway có persistent volume thường mount ở /data.
DB_PATH = "/data/football_bot.db" if os.path.isdir("/data") else "football_bot.db"

# Tần suất bot tự kiểm tra trận.
# Free API có giới hạn request nên không nên để quá thấp.
MATCH_CHECK_SECONDS = 60

# Báo trước trận bao nhiêu phút.
PRE_MATCH_NOTICE_MINUTES = 30

# Cache API.
API_CACHE_SECONDS = 60
RECENT_FORM_CACHE_SECONDS = 300


# =========================================================
# COLORS / DESIGN
# =========================================================

COLOR_MAIN = 0x5865F2
COLOR_BLUE = 0x2196F3
COLOR_GREEN = 0x2ECC71
COLOR_RED = 0xE74C3C
COLOR_GOLD = 0xFFD700
COLOR_PURPLE = 0x9B59B6
COLOR_ORANGE = 0xF39C12
COLOR_DARK = 0x111827
COLOR_CYAN = 0x00D4FF

FOOTER_TEXT = "⚽ Đá-bóng-24h • Football Data API"


# =========================================================
# LEAGUES
# =========================================================

LEAGUES = {
    "PL": {
        "name": "Premier League",
        "emoji": "🏴",
    },
    "CL": {
        "name": "UEFA Champions League",
        "emoji": "🏆",
    },
    "PD": {
        "name": "La Liga",
        "emoji": "🇪🇸",
    },
    "SA": {
        "name": "Serie A",
        "emoji": "🇮🇹",
    },
    "BL1": {
        "name": "Bundesliga",
        "emoji": "🇩🇪",
    },
    "FL1": {
        "name": "Ligue 1",
        "emoji": "🇫🇷",
    },
    "DED": {
        "name": "Eredivisie",
        "emoji": "🇳🇱",
    },
    "PPL": {
        "name": "Primeira Liga",
        "emoji": "🇵🇹",
    },
    "ELC": {
        "name": "Championship",
        "emoji": "🏴",
    },
    "FAC": {
        "name": "FA Cup",
        "emoji": "🏆",
    },
    "ELCUP": {
        "name": "Carabao Cup",
        "emoji": "🥤",
    },
}

FREE_COMPETITIONS = {
    "PL",
    "CL",
    "PD",
    "SA",
    "BL1",
    "FL1",
    "DED",
    "PPL",
    "ELC",
}


# =========================================================
# VIP
# =========================================================

VIP_LEVELS = [
    (10, 1_000_000),
    (9, 800_000),
    (8, 650_000),
    (7, 500_000),
    (6, 350_000),
    (5, 250_000),
    (4, 175_000),
    (3, 125_000),
    (2, 100_000),
    (1, 50_000),
]

VIP_NAMES = {
    10: "VIP 10 👑",
    9: "VIP 9 💎",
    8: "VIP 8 💎",
    7: "VIP 7 🔥",
    6: "VIP 6 🔥",
    5: "VIP 5 ⭐",
    4: "VIP 4 ⭐",
    3: "VIP 3 🟣",
    2: "VIP 2 🔵",
    1: "VIP 1 🟢",
    0: "Thành viên",
}


def get_vip_level(balance: int) -> int:
    for level, threshold in VIP_LEVELS:
        if balance >= threshold:
            return level
    return 0


def get_next_vip(balance: int):
    current = get_vip_level(balance)

    for level, threshold in reversed(VIP_LEVELS):
        if threshold > balance:
            return level, threshold

    return None, None


# =========================================================
# TIME
# =========================================================

def now_utc():
    return datetime.now(timezone.utc)


def now_display():
    return datetime.now(DISPLAY_TIMEZONE)


def parse_iso_datetime(value: str):
    if not value:
        return None

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt
    except Exception:
        return None


def format_match_time(utc_date: str) -> str:
    dt = parse_iso_datetime(utc_date)

    if not dt:
        return "Không rõ giờ"

    local_dt = dt.astimezone(DISPLAY_TIMEZONE)

    return local_dt.strftime("%d/%m/%Y • %H:%M")


# =========================================================
# DATABASE
# =========================================================

db_lock = asyncio.Lock()


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db_connect()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS wallets (
            user_id INTEGER PRIMARY KEY,
            balance INTEGER NOT NULL DEFAULT 100000,
            starter_claimed INTEGER NOT NULL DEFAULT 0,
            vip_level INTEGER NOT NULL DEFAULT 0
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_claims (
            user_id INTEGER NOT NULL,
            claim_date TEXT NOT NULL,
            PRIMARY KEY (user_id, claim_date)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            match_id INTEGER NOT NULL,
            league TEXT,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            choice TEXT NOT NULL,
            stake INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'OPEN',
            payout INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            settled_at TEXT
        )
        """
    )

    # Migration nếu DB cũ chưa có league.
    columns = [
        row["name"]
        for row in cur.execute("PRAGMA table_info(bets)").fetchall()
    ]

    if "league" not in columns:
        cur.execute("ALTER TABLE bets ADD COLUMN league TEXT")

    conn.commit()
    conn.close()


def ensure_user_sync(user_id: int):
    conn = db_connect()
    cur = conn.cursor()

    row = cur.execute(
        "SELECT * FROM wallets WHERE user_id = ?",
        (user_id,),
    ).fetchone()

    if row is None:
        vip = get_vip_level(STARTING_BALANCE)

        cur.execute(
            """
            INSERT INTO wallets
            (user_id, balance, starter_claimed, vip_level)
            VALUES (?, ?, 0, ?)
            """,
            (user_id, STARTING_BALANCE, vip),
        )

        conn.commit()

    conn.close()


async def ensure_user(user_id: int):
    async with db_lock:
        ensure_user_sync(user_id)


async def get_wallet(user_id: int):
    async with db_lock:
        ensure_user_sync(user_id)

        conn = db_connect()

        row = conn.execute(
            "SELECT * FROM wallets WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        conn.close()

        return dict(row)


async def change_balance(user_id: int, amount: int):
    async with db_lock:
        ensure_user_sync(user_id)

        conn = db_connect()

        old_row = conn.execute(
            "SELECT * FROM wallets WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        old_balance = int(old_row["balance"])
        old_vip = int(old_row["vip_level"])

        new_balance = old_balance + amount

        if new_balance < 0:
            conn.close()
            return False, old_balance, old_vip, old_vip

        new_vip = get_vip_level(new_balance)

        conn.execute(
            """
            UPDATE wallets
            SET balance = ?, vip_level = ?
            WHERE user_id = ?
            """,
            (new_balance, new_vip, user_id),
        )

        conn.commit()
        conn.close()

        return True, new_balance, old_vip, new_vip


async def claim_starter(user_id: int):
    async with db_lock:
        ensure_user_sync(user_id)

        conn = db_connect()

        row = conn.execute(
            "SELECT * FROM wallets WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        if row["starter_claimed"]:
            balance = row["balance"]
            conn.close()
            return False, balance

        new_balance = row["balance"] + STARTER_BONUS
        new_vip = get_vip_level(new_balance)

        conn.execute(
            """
            UPDATE wallets
            SET balance = ?, starter_claimed = 1, vip_level = ?
            WHERE user_id = ?
            """,
            (new_balance, new_vip, user_id),
        )

        conn.commit()
        conn.close()

        return True, new_balance


async def claim_daily(user_id: int):
    today = now_display().strftime("%Y-%m-%d")

    async with db_lock:
        ensure_user_sync(user_id)

        conn = db_connect()

        existing = conn.execute(
            """
            SELECT 1 FROM daily_claims
            WHERE user_id = ? AND claim_date = ?
            """,
            (user_id, today),
        ).fetchone()

        if existing:
            balance = conn.execute(
                "SELECT balance FROM wallets WHERE user_id = ?",
                (user_id,),
            ).fetchone()["balance"]

            conn.close()

            return False, balance

        row = conn.execute(
            "SELECT balance, vip_level FROM wallets WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        new_balance = row["balance"] + DAILY_BONUS
        new_vip = get_vip_level(new_balance)

        conn.execute(
            """
            INSERT INTO daily_claims(user_id, claim_date)
            VALUES (?, ?)
            """,
            (user_id, today),
        )

        conn.execute(
            """
            UPDATE wallets
            SET balance = ?, vip_level = ?
            WHERE user_id = ?
            """,
            (new_balance, new_vip, user_id),
        )

        conn.commit()
        conn.close()

        return True, new_balance


async def create_bet(
    user_id: int,
    match_id: int,
    league: str,
    home_team: str,
    away_team: str,
    choice: str,
    stake: int,
):
    async with db_lock:
        ensure_user_sync(user_id)

        conn = db_connect()

        row = conn.execute(
            "SELECT balance FROM wallets WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        balance = int(row["balance"])

        if stake <= 0:
            conn.close()
            return False, "Số tiền cược không hợp lệ."

        if balance < stake:
            conn.close()
            return False, f"Bạn chỉ có **{balance:,} 🪙**."

        conn.execute(
            """
            UPDATE wallets
            SET balance = balance - ?
            WHERE user_id = ?
            """,
            (stake, user_id),
        )

        conn.execute(
            """
            INSERT INTO bets
            (
                user_id,
                match_id,
                league,
                home_team,
                away_team,
                choice,
                stake,
                status,
                payout,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', 0, ?)
            """,
            (
                user_id,
                match_id,
                league,
                home_team,
                away_team,
                choice,
                stake,
                now_utc().isoformat(),
            ),
        )

        conn.commit()

        new_balance = conn.execute(
            "SELECT balance FROM wallets WHERE user_id = ?",
            (user_id,),
        ).fetchone()["balance"]

        conn.close()

        return True, new_balance


async def get_open_bets_for_match(match_id: int):
    async with db_lock:
        conn = db_connect()

        rows = conn.execute(
            """
            SELECT * FROM bets
            WHERE match_id = ? AND status = 'OPEN'
            """,
            (match_id,),
        ).fetchall()

        conn.close()

        return [dict(row) for row in rows]


async def settle_match_bets(match_id: int, result: str):
    """
    result:
        HOME
        DRAW
        AWAY
    """

    async with db_lock:
        conn = db_connect()

        rows = conn.execute(
            """
            SELECT * FROM bets
            WHERE match_id = ? AND status = 'OPEN'
            """,
            (match_id,),
        ).fetchall()

        settled = []

        for row in rows:
            choice = row["choice"]
            stake = int(row["stake"])

            won = choice == result

            payout = stake * BET_PAYOUT if won else 0

            conn.execute(
                """
                UPDATE bets
                SET status = ?, payout = ?, settled_at = ?
                WHERE id = ?
                """,
                (
                    "WON" if won else "LOST",
                    payout,
                    now_utc().isoformat(),
                    row["id"],
                ),
            )

            if payout > 0:
                conn.execute(
                    """
                    UPDATE wallets
                    SET balance = balance + ?
                    WHERE user_id = ?
                    """,
                    (payout, row["user_id"]),
                )

            settled.append(
                {
                    "bet_id": row["id"],
                    "user_id": row["user_id"],
                    "home_team": row["home_team"],
                    "away_team": row["away_team"],
                    "choice": choice,
                    "stake": stake,
                    "payout": payout,
                    "won": won,
                }
            )

        conn.commit()
        conn.close()

        return settled


# =========================================================
# DISCORD BOT
# =========================================================

intents = discord.Intents.default()
intents.guilds = True
intents.members = True


class FootballBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=intents,
        )

        self.http_session: Optional[aiohttp.ClientSession] = None
        self.api_cache = {}
        self.recent_cache = {}
        self.last_api_remaining = "?"
        self.last_api_reset = "?"

        self.health_runner = None
        self.synced = False

    async def setup_hook(self):
        init_db()

        if GUILD_ID:
            try:
                guild = discord.Object(id=int(GUILD_ID))
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                print("Slash commands synced to guild:", GUILD_ID)
            except Exception as e:
                print("Guild sync error:", e)
        else:
            try:
                await self.tree.sync()
                print("Global slash commands synced.")
            except Exception as e:
                print("Global sync error:", e)

        self.match_monitor.start()

    async def get_http_session(self):
        if self.http_session is None or self.http_session.closed:
            timeout = aiohttp.ClientTimeout(total=20)

            self.http_session = aiohttp.ClientSession(
                timeout=timeout
            )

        return self.http_session

    async def close(self):
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()

        await super().close()

    @tasks.loop(seconds=MATCH_CHECK_SECONDS)
    async def match_monitor(self):
        await monitor_bets()

    @match_monitor.before_loop
    async def before_match_monitor(self):
        await self.wait_until_ready()


bot = FootballBot()


# =========================================================
# API
# =========================================================

async def api_get(
    path: str,
    params: Optional[dict] = None,
    cache_seconds: int = API_CACHE_SECONDS,
):
    if not FOOTBALL_DATA_API_KEY:
        raise RuntimeError(
            "Thiếu FOOTBALL_DATA_API_KEY trên Railway."
        )

    params = params or {}

    cache_key = (
        path,
        tuple(sorted((str(k), str(v)) for k, v in params.items())),
    )

    cached = bot.api_cache.get(cache_key)

    if cached:
        created, data = cached

        if (datetime.now().timestamp() - created) < cache_seconds:
            return data

    session = await bot.get_http_session()

    headers = {
        "X-Auth-Token": FOOTBALL_DATA_API_KEY,
        "Accept": "application/json",
    }

    url = API_BASE + path

    try:
        async with session.get(
            url,
            params=params,
            headers=headers,
        ) as response:

            bot.last_api_remaining = response.headers.get(
                "X-Requests-Available-Minute",
                "?",
            )

            bot.last_api_reset = response.headers.get(
                "X-RequestCounter-Reset",
                "?",
            )

            if response.status == 429:
                raise RuntimeError(
                    "API đang giới hạn request. Hãy chờ một chút rồi thử lại."
                )

            if response.status == 403:
                raise RuntimeError(
                    "API key không có quyền xem dữ liệu competition này "
                    "hoặc competition không nằm trong gói hiện tại."
                )

            if response.status == 404:
                raise RuntimeError(
                    "Không tìm thấy dữ liệu trận đấu."
                )

            if response.status >= 400:
                text = await response.text()

                raise RuntimeError(
                    f"Football API lỗi HTTP {response.status}: {text[:300]}"
                )

            data = await response.json()

            bot.api_cache[cache_key] = (
                datetime.now().timestamp(),
                data,
            )

            return data

    except asyncio.TimeoutError:
        raise RuntimeError("Football API phản hồi quá lâu.")


async def get_upcoming_league_matches(code: str):
    if code not in LEAGUES:
        return []

    if code not in FREE_COMPETITIONS:
        return []

    start = now_utc().date()
    end = start + timedelta(days=45)

    data = await api_get(
        f"/competitions/{code}/matches",
        {
            "dateFrom": start.isoformat(),
            "dateTo": end.isoformat(),
        },
        cache_seconds=60,
    )

    matches = data.get("matches", [])

    allowed = {
        "SCHEDULED",
        "TIMED",
    }

    matches = [
        m for m in matches
        if m.get("status") in allowed
    ]

    matches.sort(
        key=lambda m: m.get("utcDate", "")
    )

    return matches


async def get_upcoming_mancity_matches():
    start = now_utc().date()
    end = start + timedelta(days=60)

    data = await api_get(
        f"/teams/{MANCHESTER_CITY_TEAM_ID}/matches",
        {
            "dateFrom": start.isoformat(),
            "dateTo": end.isoformat(),
            "limit": 100,
        },
        cache_seconds=60,
    )

    matches = data.get("matches", [])

    matches = [
        m for m in matches
        if m.get("status") in {"SCHEDULED", "TIMED"}
    ]

    matches.sort(
        key=lambda m: m.get("utcDate", "")
    )

    return matches


async def get_match(match_id: int):
    return await api_get(
        f"/matches/{match_id}",
        {},
        cache_seconds=30,
    )


async def get_recent_team_matches(team_id: int):
    cache_key = str(team_id)

    cached = bot.recent_cache.get(cache_key)

    if cached:
        created, matches = cached

        if (
            datetime.now().timestamp() - created
            < RECENT_FORM_CACHE_SECONDS
        ):
            return matches

    end = now_utc().date()
    start = end - timedelta(days=120)

    data = await api_get(
        f"/teams/{team_id}/matches",
        {
            "dateFrom": start.isoformat(),
            "dateTo": end.isoformat(),
            "status": "FINISHED",
            "limit": 20,
        },
        cache_seconds=RECENT_FORM_CACHE_SECONDS,
    )

    matches = data.get("matches", [])

    matches.sort(
        key=lambda m: m.get("utcDate", ""),
        reverse=True,
    )

    bot.recent_cache[cache_key] = (
        datetime.now().timestamp(),
        matches,
    )

    return matches


# =========================================================
# PREDICTION ENGINE
# =========================================================

def poisson_probability(lmbda: float, k: int):
    if lmbda <= 0:
        return 0.0

    return (
        math.exp(-lmbda)
        * (lmbda ** k)
        / math.factorial(k)
    )


def team_form_stats(team_id: int, matches: list):
    scored = []
    conceded = []
    points = []

    for match in matches:
        home = match.get("homeTeam", {})
        away = match.get("awayTeam", {})

        score = match.get("score", {}).get("fullTime", {})

        home_goals = score.get("home")
        away_goals = score.get("away")

        if home_goals is None or away_goals is None:
            continue

        if home.get("id") == team_id:
            gf = home_goals
            ga = away_goals

        elif away.get("id") == team_id:
            gf = away_goals
            ga = home_goals

        else:
            continue

        scored.append(gf)
        conceded.append(ga)

        if gf > ga:
            points.append(3)
        elif gf == ga:
            points.append(1)
        else:
            points.append(0)

    if not scored:
        return {
            "games": 0,
            "gf": 1.35,
            "ga": 1.10,
            "ppg": 1.40,
            "wins": 0,
            "draws": 0,
            "losses": 0,
        }

    return {
        "games": len(scored),
        "gf": sum(scored) / len(scored),
        "ga": sum(conceded) / len(conceded),
        "ppg": sum(points) / len(points),
        "wins": sum(1 for x in points if x == 3),
        "draws": sum(1 for x in points if x == 1),
        "losses": sum(1 for x in points if x == 0),
    }


def calculate_prediction(
    home_stats,
    away_stats,
):
    """
    Mô hình đơn giản:
    - recent goals scored
    - recent goals conceded
    - points per game
    - lợi thế sân nhà
    - Poisson 0..6 bàn
    """

    home_attack = home_stats["gf"]
    home_defense = home_stats["ga"]

    away_attack = away_stats["gf"]
    away_defense = away_stats["ga"]

    home_ppg = home_stats["ppg"]
    away_ppg = away_stats["ppg"]

    home_lambda = (
        home_attack * 0.55
        + away_defense * 0.45
    )

    away_lambda = (
        away_attack * 0.55
        + home_defense * 0.45
    )

    # Lợi thế sân nhà nhẹ.
    home_lambda *= 1.08
    away_lambda *= 0.94

    # Điều chỉnh nhẹ theo PPG.
    home_factor = 1 + ((home_ppg - 1.4) * 0.035)
    away_factor = 1 + ((away_ppg - 1.4) * 0.035)

    home_lambda *= home_factor
    away_lambda *= away_factor

    home_lambda = max(0.20, min(home_lambda, 3.80))
    away_lambda = max(0.20, min(away_lambda, 3.80))

    score_probs = {}

    for h in range(0, 7):
        for a in range(0, 7):
            p = (
                poisson_probability(home_lambda, h)
                * poisson_probability(away_lambda, a)
            )

            score_probs[(h, a)] = p

    total = sum(score_probs.values())

    if total <= 0:
        total = 1

    for key in score_probs:
        score_probs[key] /= total

    home_prob = sum(
        p
        for (h, a), p in score_probs.items()
        if h > a
    )

    draw_prob = sum(
        p
        for (h, a), p in score_probs.items()
        if h == a
    )

    away_prob = sum(
        p
        for (h, a), p in score_probs.items()
        if h < a
    )

    top_scores = sorted(
        score_probs.items(),
        key=lambda x: x[1],
        reverse=True,
    )[:5]

    exact_score = top_scores[0][0]

    probabilities = {
        "HOME": home_prob,
        "DRAW": draw_prob,
        "AWAY": away_prob,
    }

    prediction = max(
        probabilities,
        key=probabilities.get,
    )

    return {
        "home_xg": home_lambda,
        "away_xg": away_lambda,
        "home_prob": home_prob,
        "draw_prob": draw_prob,
        "away_prob": away_prob,
        "prediction": prediction,
        "exact_score": exact_score,
        "top_scores": top_scores,
    }


async def analyze_match(match: dict):
    home = match.get("homeTeam", {})
    away = match.get("awayTeam", {})

    home_id = home.get("id")
    away_id = away.get("id")

    try:
        home_matches = await get_recent_team_matches(home_id)
    except Exception:
        home_matches = []

    try:
        away_matches = await get_recent_team_matches(away_id)
    except Exception:
        away_matches = []

    home_stats = team_form_stats(
        home_id,
        home_matches,
    )

    away_stats = team_form_stats(
        away_id,
        away_matches,
    )

    prediction = calculate_prediction(
        home_stats,
        away_stats,
    )

    prediction["home_stats"] = home_stats
    prediction["away_stats"] = away_stats

    return prediction


# =========================================================
# EMBEDS
# =========================================================

def base_embed(
    title: str,
    description: str = "",
    color: int = COLOR_MAIN,
):
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    embed.set_footer(text=FOOTER_TEXT)

    return embed


def match_title(match: dict):
    home = match.get("homeTeam", {}).get(
        "shortName"
    ) or match.get("homeTeam", {}).get(
        "name",
        "Home",
    )

    away = match.get("awayTeam", {}).get(
        "shortName"
    ) or match.get("awayTeam", {}).get(
        "name",
        "Away",
    )

    return f"{home} 🆚 {away}"


def result_text(result: str):
    return {
        "HOME": "🏠 Đội nhà thắng",
        "DRAW": "🤝 Hòa",
        "AWAY": "✈️ Đội khách thắng",
    }.get(result, result)


# =========================================================
# MAIN FOOTBALL VIEW
# =========================================================

class LeagueSelect(discord.ui.Select):
    def __init__(self, mode: str):
        self.mode = mode

        options = []

        for code, info in LEAGUES.items():
            options.append(
                discord.SelectOption(
                    label=info["name"][:100],
                    value=code,
                    emoji=info["emoji"],
                    description=(
                        "Chọn giải để xem các trận sắp tới"
                    )[:100],
                )
            )

        super().__init__(
            placeholder="⚽ Chọn giải đấu...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        code = self.values[0]

        await interaction.response.defer()

        if code not in FREE_COMPETITIONS:
            embed = base_embed(
                "🔒 Dữ liệu chưa có trong gói API",
                (
                    f"**{LEAGUES[code]['name']}** hiện không nằm "
                    "trong coverage Free của football-data.org.\n\n"
                    "Bot vẫn giữ nút giải này để sau này có thể "
                    "đổi API/plan."
                ),
                COLOR_ORANGE,
            )

            await interaction.edit_original_response(
                embed=embed,
                view=FootballMainView(self.mode),
            )

            return

        try:
            matches = await get_upcoming_league_matches(code)
        except Exception as e:
            embed = base_embed(
                "❌ Không lấy được lịch",
                str(e),
                COLOR_RED,
            )

            await interaction.edit_original_response(
                embed=embed,
                view=FootballMainView(self.mode),
            )

            return

        if not matches:
            embed = base_embed(
                f"{LEAGUES[code]['emoji']} {LEAGUES[code]['name']}",
                "Không tìm thấy trận sắp tới trong khoảng thời gian hiện tại.",
                COLOR_ORANGE,
            )

            await interaction.edit_original_response(
                embed=embed,
                view=FootballMainView(self.mode),
            )

            return

        view = MatchListView(
            matches=matches,
            mode=self.mode,
            league_code=code,
            page=0,
        )

        embed = view.make_embed()

        await interaction.edit_original_response(
            embed=embed,
            view=view,
        )


class ManCityButton(discord.ui.Button):
    def __init__(self, mode: str):
        self.mode = mode

        super().__init__(
            label="Các trận Man City sắp tới",
            emoji="🔵",
            style=discord.ButtonStyle.primary,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()

        try:
            matches = await get_upcoming_mancity_matches()
        except Exception as e:
            embed = base_embed(
                "❌ Không lấy được lịch Man City",
                str(e),
                COLOR_RED,
            )

            await interaction.edit_original_response(
                embed=embed,
                view=FootballMainView(self.mode),
            )

            return

        if not matches:
            embed = base_embed(
                "🔵 Man City",
                "Không tìm thấy trận sắp tới.",
                COLOR_ORANGE,
            )

            await interaction.edit_original_response(
                embed=embed,
                view=FootballMainView(self.mode),
            )

            return

        view = MatchListView(
            matches=matches,
            mode=self.mode,
            league_code="MANCITY",
            page=0,
        )

        embed = view.make_embed()

        await interaction.edit_original_response(
            embed=embed,
            view=view,
        )


class FootballMainView(discord.ui.View):
    def __init__(self, mode: str):
        super().__init__(timeout=300)

        self.mode = mode

        self.add_item(
            LeagueSelect(mode)
        )

        self.add_item(
            ManCityButton(mode)
        )


# =========================================================
# MATCH LIST / PAGINATION
# =========================================================

class MatchSelect(discord.ui.Select):
    def __init__(
        self,
        parent_view,
        matches,
    ):
        self.parent_view = parent_view

        options = []

        for match in matches:
            match_id = str(match["id"])

            home = match.get(
                "homeTeam",
                {},
            ).get(
                "shortName"
            ) or match.get(
                "homeTeam",
                {},
            ).get(
                "name",
                "Home",
            )

            away = match.get(
                "awayTeam",
                {},
            ).get(
                "shortName"
            ) or match.get(
                "awayTeam",
                {},
            ).get(
                "name",
                "Away",
            )

            label = f"{home} vs {away}"

            description = (
                format_match_time(
                    match.get("utcDate", "")
                )
            )

            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=match_id,
                    description=description[:100],
                )
            )

        super().__init__(
            placeholder="🎯 Chọn trận đấu...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        match_id = int(self.values[0])

        match = next(
            (
                m
                for m in self.parent_view.all_matches
                if int(m["id"]) == match_id
            ),
            None,
        )

        if not match:
            await interaction.response.send_message(
                "❌ Không tìm thấy trận.",
                ephemeral=True,
            )
            return

        if self.parent_view.mode == "bet":
            await interaction.response.send_modal(
                StakeModal(
                    match=match,
                    league_code=self.parent_view.league_code,
                )
            )

        else:
            await interaction.response.defer()

            try:
                analysis = await analyze_match(match)
            except Exception as e:
                await interaction.followup.send(
                    f"❌ Không phân tích được trận: {e}",
                    ephemeral=True,
                )
                return

            embed = make_prediction_embed(
                match,
                analysis,
            )

            await interaction.edit_original_response(
                embed=embed,
                view=PredictionBackView(
                    self.parent_view.mode,
                    self.parent_view.league_code,
                    self.parent_view.page,
                    self.parent_view.all_matches,
                ),
            )


class MatchListView(discord.ui.View):
    PAGE_SIZE = 20

    def __init__(
        self,
        matches,
        mode,
        league_code,
        page=0,
    ):
        super().__init__(timeout=300)

        self.all_matches = matches
        self.mode = mode
        self.league_code = league_code
        self.page = page

        self.total_pages = max(
            1,
            math.ceil(
                len(matches) / self.PAGE_SIZE
            ),
        )

        start = page * self.PAGE_SIZE
        end = start + self.PAGE_SIZE

        page_matches = matches[start:end]

        if page_matches:
            self.add_item(
                MatchSelect(
                    self,
                    page_matches,
                )
            )

        previous = discord.ui.Button(
            label="⬅️ Trước",
            style=discord.ButtonStyle.secondary,
            disabled=page <= 0,
        )

        next_button = discord.ui.Button(
            label="Sau ➡️",
            style=discord.ButtonStyle.secondary,
            disabled=page >= self.total_pages - 1,
        )

        previous.callback = self.previous_page
        next_button.callback = self.next_page

        self.add_item(previous)
        self.add_item(next_button)

        back = discord.ui.Button(
            label="🏠 Về giải đấu",
            style=discord.ButtonStyle.success,
        )

        back.callback = self.back_main

        self.add_item(back)

    def make_embed(self):
        if self.league_code == "MANCITY":
            title = "🔵 Manchester City — Trận sắp tới"
        else:
            info = LEAGUES.get(
                self.league_code,
                {},
            )

            title = (
                f"{info.get('emoji', '⚽')} "
                f"{info.get('name', 'Lịch thi đấu')}"
            )

        start = self.page * self.PAGE_SIZE
        end = min(
            start + self.PAGE_SIZE,
            len(self.all_matches),
        )

        mode_text = (
            "💰 Chọn trận để đặt cược"
            if self.mode == "bet"
            else "🔎 Chọn trận để soi & dự đoán"
        )

        description = (
            f"{mode_text}\n"
            f"📄 Trang **{self.page + 1}/{self.total_pages}**\n"
            f"📊 Tổng cộng: **{len(self.all_matches)} trận**"
        )

        embed = base_embed(
            title,
            description,
            COLOR_BLUE,
        )

        embed.add_field(
            name="📌 Hiển thị",
            value=f"`{start + 1}` → `{end}`",
            inline=True,
        )

        embed.add_field(
            name="🕐 Múi giờ",
            value="UTC+7",
            inline=True,
        )

        embed.add_field(
            name="⚽ Chọn trận",
            value="Dùng menu bên dưới.",
            inline=False,
        )

        return embed

    async def previous_page(self, interaction):
        await interaction.response.edit_message(
            embed=MatchListView(
                self.all_matches,
                self.mode,
                self.league_code,
                self.page - 1,
            ).make_embed(),
            view=MatchListView(
                self.all_matches,
                self.mode,
                self.league_code,
                self.page - 1,
            ),
        )

    async def next_page(self, interaction):
        await interaction.response.edit_message(
            embed=MatchListView(
                self.all_matches,
                self.mode,
                self.league_code,
                self.page + 1,
            ).make_embed(),
            view=MatchListView(
                self.all_matches,
                self.mode,
                self.league_code,
                self.page + 1,
            ),
        )

    async def back_main(self, interaction):
        await interaction.response.edit_message(
            embed=base_embed(
                "⚽ Khu vực bóng đá",
                (
                    "Chọn giải đấu hoặc xem toàn bộ "
                    "lịch Man City."
                ),
                COLOR_MAIN,
            ),
            view=FootballMainView(self.mode),
        )


class PredictionBackView(discord.ui.View):
    def __init__(
        self,
        mode,
        league_code,
        page,
        matches,
    ):
        super().__init__(timeout=300)

        self.mode = mode
        self.league_code = league_code
        self.page = page
        self.matches = matches

        button = discord.ui.Button(
            label="⬅️ Quay lại danh sách trận",
            style=discord.ButtonStyle.secondary,
        )

        async def callback(interaction):
            view = MatchListView(
                self.matches,
                self.mode,
                self.league_code,
                self.page,
            )

            await interaction.response.edit_message(
                embed=view.make_embed(),
                view=view,
            )

        button.callback = callback
        self.add_item(button)


# =========================================================
# PREDICTION EMBED
# =========================================================

def make_prediction_embed(
    match,
    analysis,
):
    home = match.get(
        "homeTeam",
        {},
    ).get(
        "name",
        "Home",
    )

    away = match.get(
        "awayTeam",
        {},
    ).get(
        "name",
        "Away",
    )

    competition = match.get(
        "competition",
        {},
    ).get(
        "name",
        "Football",
    )

    exact_h, exact_a = analysis["exact_score"]

    home_p = analysis["home_prob"] * 100
    draw_p = analysis["draw_prob"] * 100
    away_p = analysis["away_prob"] * 100

    predicted = analysis["prediction"]

    conclusion = {
        "HOME": f"🏠 Nghiêng về **{home}**",
        "DRAW": "🤝 Nghiêng về **Hòa**",
        "AWAY": f"✈️ Nghiêng về **{away}**",
    }[predicted]

    top_scores_text = "\n".join(
        [
            f"`{h}-{a}` — **{p * 100:.1f}%**"
            for (h, a), p in analysis["top_scores"][:3]
        ]
    )

    hs = analysis["home_stats"]
    aws = analysis["away_stats"]

    embed = base_embed(
        "🔎 SOI TRẬN & DỰ ĐOÁN",
        f"**{home}** 🆚 **{away}**",
        COLOR_PURPLE,
    )

    embed.add_field(
        name="🏆 Giải đấu",
        value=competition,
        inline=True,
    )

    embed.add_field(
        name="🕐 Thời gian",
        value=format_match_time(
            match.get("utcDate", "")
        ),
        inline=True,
    )

    embed.add_field(
        name="📌 Trạng thái",
        value=match.get("status", "?"),
        inline=True,
    )

    embed.add_field(
        name="🤖 Dự đoán tỉ số",
        value=f"**{home} {exact_h} — {exact_a} {away}**",
        inline=False,
    )

    embed.add_field(
        name="📊 Xác suất mô hình",
        value=(
            f"🏠 Home: **{home_p:.1f}%**\n"
            f"🤝 Draw: **{draw_p:.1f}%**\n"
            f"✈️ Away: **{away_p:.1f}%**"
        ),
        inline=True,
    )

    embed.add_field(
        name="🎯 Các tỉ số nổi bật",
        value=top_scores_text or "Không đủ dữ liệu.",
        inline=True,
    )

    embed.add_field(
        name="📈 Form gần đây",
        value=(
            f"**{home}**: "
            f"{hs['wins']}W {hs['draws']}D {hs['losses']}L\n"
            f"⚽ GF {hs['gf']:.2f} / GA {hs['ga']:.2f}\n\n"
            f"**{away}**: "
            f"{aws['wins']}W {aws['draws']}D {aws['losses']}L\n"
            f"⚽ GF {aws['gf']:.2f} / GA {aws['ga']:.2f}"
        ),
        inline=False,
    )

    embed.add_field(
        name="🧠 Kết luận của bot",
        value=conclusion,
        inline=False,
    )

    embed.add_field(
        name="⚠️ Lưu ý",
        value=(
            "Đây là **ước tính của mô hình thống kê nội bộ**, "
            "không phải tỷ lệ nhà cái và không phải xác suất "
            "do football-data.org cung cấp."
        ),
        inline=False,
    )

    return embed


# =========================================================
# BET MODAL
# =========================================================

class StakeModal(discord.ui.Modal):
    def __init__(
        self,
        match,
        league_code,
    ):
        super().__init__(
            title="💰 Nhập tiền cược"
        )

        self.match = match
        self.league_code = league_code

        self.amount = discord.ui.TextInput(
            label="Số coin muốn cược",
            placeholder="Ví dụ: 50000",
            required=True,
            min_length=1,
            max_length=12,
        )

        self.add_item(self.amount)

    async def on_submit(self, interaction):
        try:
            stake = int(
                self.amount.value.replace(",", "").strip()
            )
        except ValueError:
            await interaction.response.send_message(
                "❌ Hãy nhập số nguyên.",
                ephemeral=True,
            )
            return

        if stake <= 0:
            await interaction.response.send_message(
                "❌ Tiền cược phải lớn hơn 0.",
                ephemeral=True,
            )
            return

        status = self.match.get("status")

        if status not in {"SCHEDULED", "TIMED"}:
            await interaction.response.send_message(
                "❌ Trận này không còn nhận cược.",
                ephemeral=True,
            )
            return

        match_time = parse_iso_datetime(
            self.match.get("utcDate", "")
        )

        if match_time and match_time <= now_utc():
            await interaction.response.send_message(
                "❌ Trận đã bắt đầu hoặc đến giờ bắt đầu.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=make_bet_choice_embed(
                self.match,
                stake,
            ),
            view=BetChoiceView(
                self.match,
                self.league_code,
                stake,
            ),
            ephemeral=True,
        )


def make_bet_choice_embed(match, stake):
    home = match.get(
        "homeTeam",
        {},
    ).get(
        "shortName"
    ) or match.get(
        "homeTeam",
        {},
    ).get(
        "name",
        "Home",
    )

    away = match.get(
        "awayTeam",
        {},
    ).get(
        "shortName"
    ) or match.get(
        "awayTeam",
        {},
    ).get(
        "name",
        "Away",
    )

    embed = base_embed(
        "💰 XÁC NHẬN CƯỢC",
        f"**{home}** 🆚 **{away}**",
        COLOR_GOLD,
    )

    embed.add_field(
        name="🪙 Tiền cược",
        value=f"**{stake:,}**",
        inline=True,
    )

    embed.add_field(
        name="💵 Nếu thắng",
        value=f"**{stake * BET_PAYOUT:,}**",
        inline=True,
    )

    embed.add_field(
        name="🕐 Trận đấu",
        value=format_match_time(
            match.get("utcDate", "")
        ),
        inline=False,
    )

    embed.add_field(
        name="🎯 Chọn kết quả",
        value=(
            "🏠 **Home** = đội nhà thắng\n"
            "🤝 **Draw** = hòa\n"
            "✈️ **Away** = đội khách thắng"
        ),
        inline=False,
    )

    return embed


class BetChoiceView(discord.ui.View):
    def __init__(
        self,
        match,
        league_code,
        stake,
    ):
        super().__init__(timeout=120)

        self.match = match
        self.league_code = league_code
        self.stake = stake

    async def place(
        self,
        interaction,
        choice,
    ):
        match_time = parse_iso_datetime(
            self.match.get("utcDate", "")
        )

        if match_time and match_time <= now_utc():
            await interaction.response.send_message(
                "❌ Trận đã bắt đầu. Không thể cược.",
                ephemeral=True,
            )
            return

        home = self.match.get(
            "homeTeam",
            {},
        ).get(
            "name",
            "Home",
        )

        away = self.match.get(
            "awayTeam",
            {},
        ).get(
            "name",
            "Away",
        )

        ok, result = await create_bet(
            user_id=interaction.user.id,
            match_id=int(self.match["id"]),
            league=self.league_code,
            home_team=home,
            away_team=away,
            choice=choice,
            stake=self.stake,
        )

        if not ok:
            await interaction.response.send_message(
                f"❌ {result}",
                ephemeral=True,
            )
            return

        choice_text = {
            "HOME": f"🏠 {home}",
            "DRAW": "🤝 Hòa",
            "AWAY": f"✈️ {away}",
        }[choice]

        embed = base_embed(
            "✅ ĐẶT CƯỢC THÀNH CÔNG",
            (
                f"**{home}** 🆚 **{away}**\n\n"
                f"🎯 Lựa chọn: **{choice_text}**\n"
                f"🪙 Tiền cược: **{self.stake:,}**\n"
                f"💰 Nhận nếu thắng: **{self.stake * BET_PAYOUT:,}**\n\n"
                f"💳 Số dư còn lại: **{result:,}**"
            ),
            COLOR_GREEN,
        )

        await interaction.response.edit_message(
            embed=embed,
            view=None,
        )

    @discord.ui.button(
        label="Home",
        emoji="🏠",
        style=discord.ButtonStyle.success,
    )
    async def home(
        self,
        interaction,
        button,
    ):
        await self.place(interaction, "HOME")

    @discord.ui.button(
        label="Draw",
        emoji="🤝",
        style=discord.ButtonStyle.secondary,
    )
    async def draw(
        self,
        interaction,
        button,
    ):
        await self.place(interaction, "DRAW")

    @discord.ui.button(
        label="Away",
        emoji="✈️",
        style=discord.ButtonStyle.danger,
    )
    async def away(
        self,
        interaction,
        button,
    ):
        await self.place(interaction, "AWAY")


# =========================================================
# ADMIN
# =========================================================

def is_admin_user(user_id: int):
    if not ADMIN_USER_IDS_RAW:
        return True

    allowed = {
        x.strip()
        for x in ADMIN_USER_IDS_RAW.split(",")
        if x.strip()
    }

    return str(user_id) in allowed


class AdminPasswordModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(
            title="🔐 Xác thực Admin"
        )

        self.password = discord.ui.TextInput(
            label="Mật khẩu admin",
            placeholder="Nhập mật khẩu...",
            required=True,
            min_length=1,
            max_length=100,
        )

        self.add_item(self.password)

    async def on_submit(self, interaction):
        if not is_admin_user(interaction.user.id):
            await interaction.response.send_message(
                "❌ Tài khoản Discord này không được phép dùng admin.",
                ephemeral=True,
            )
            return

        if self.password.value != ADMIN_PASSWORD:
            await interaction.response.send_message(
                "❌ Sai mật khẩu admin.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=base_embed(
                "👑 ADMIN PANEL",
                (
                    "Xác thực thành công.\n\n"
                    "Chọn chức năng bên dưới."
                ),
                COLOR_GOLD,
            ),
            view=AdminPanelView(),
            ephemeral=True,
        )


class AdminPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(
        label="Cộng coin",
        emoji="➕",
        style=discord.ButtonStyle.success,
    )
    async def add_coin(
        self,
        interaction,
        button,
    ):
        await interaction.response.send_modal(
            AdminBalanceModal(
                mode="add"
            )
        )

    @discord.ui.button(
        label="Trừ coin",
        emoji="➖",
        style=discord.ButtonStyle.danger,
    )
    async def remove_coin(
        self,
        interaction,
        button,
    ):
        await interaction.response.send_modal(
            AdminBalanceModal(
                mode="remove"
            )
        )


class AdminBalanceModal(discord.ui.Modal):
    def __init__(self, mode: str):
        self.mode = mode

        title = (
            "➕ Cộng coin"
            if mode == "add"
            else "➖ Trừ coin"
        )

        super().__init__(title=title)

        self.user_id_input = discord.ui.TextInput(
            label="Discord User ID",
            placeholder="Ví dụ: 123456789012345678",
            required=True,
            max_length=30,
        )

        self.amount_input = discord.ui.TextInput(
            label="Số coin",
            placeholder="Ví dụ: 50000",
            required=True,
            max_length=15,
        )

        self.add_item(self.user_id_input)
        self.add_item(self.amount_input)

    async def on_submit(self, interaction):
        if not is_admin_user(interaction.user.id):
            await interaction.response.send_message(
                "❌ Không có quyền.",
                ephemeral=True,
            )
            return

        try:
            target_id = int(
                self.user_id_input.value.strip()
            )

            amount = int(
                self.amount_input.value
                .replace(",", "")
                .strip()
            )

        except ValueError:
            await interaction.response.send_message(
                "❌ User ID hoặc số coin không hợp lệ.",
                ephemeral=True,
            )
            return

        if amount <= 0:
            await interaction.response.send_message(
                "❌ Số coin phải lớn hơn 0.",
                ephemeral=True,
            )
            return

        delta = amount if self.mode == "add" else -amount

        ok, balance, old_vip, new_vip = await change_balance(
            target_id,
            delta,
        )

        if not ok:
            await interaction.response.send_message(
                "❌ Không thể trừ quá số dư hiện tại.",
                ephemeral=True,
            )
            return

        vip_text = ""

        if new_vip > old_vip:
            vip_text = (
                f"\n\n👑 Người chơi đã lên "
                f"**{VIP_NAMES[new_vip]}**!"
            )

        embed = base_embed(
            "✅ ADMIN ĐÃ CẬP NHẬT COIN",
            (
                f"👤 User ID: `{target_id}`\n"
                f"💰 Thay đổi: **{delta:+,}** 🪙\n"
                f"💳 Số dư mới: **{balance:,}** 🪙\n"
                f"👑 VIP: **{VIP_NAMES[new_vip]}**"
                f"{vip_text}"
            ),
            COLOR_GOLD,
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
        )

        if new_vip > old_vip:
            await announce_vip(
                interaction.guild,
                target_id,
                new_vip,
                balance,
            )


# =========================================================
# VIP ANNOUNCEMENT
# =========================================================

async def announce_vip(
    guild,
    user_id,
    vip_level,
    balance,
):
    if guild is None:
        return

    member = guild.get_member(user_id)

    mention = (
        member.mention
        if member
        else f"<@{user_id}>"
    )

    embed = base_embed(
        "👑 THĂNG CẤP VIP!",
        (
            f"🎉 Xin chúc mừng {mention}!\n\n"
            f"👑 Cấp mới: **{VIP_NAMES[vip_level]}**\n"
            f"💰 Số dư: **{balance:,} 🪙**\n\n"
            "🔥 Tiếp tục tham gia để nâng VIP!"
        ),
        COLOR_GOLD,
    )

    embed.set_thumbnail(
        url="https://cdn-icons-png.flaticon.com/512/2583/2583344.png"
    )

    channel = guild.system_channel

    if channel is None:
        for ch in guild.text_channels:
            if ch.permissions_for(guild.me).send_messages:
                channel = ch
                break

    if channel:
        try:
            await channel.send(
                embed=embed
            )
        except Exception:
            pass


# =========================================================
# AUTO MATCH NOTIFICATIONS
# =========================================================

match_notice_cache = {}


async def get_channels_for_bet_match(match_id: int):
    """
    Lấy guild/channel từ những người đang cược.
    """

    bets = await get_open_bets_for_match(match_id)

    targets = []

    for bet in bets:
        user_id = bet["user_id"]

        for guild in bot.guilds:
            member = guild.get_member(user_id)

            if member:
                targets.append(
                    (
                        guild,
                        member,
                    )
                )

    unique = []

    seen = set()

    for guild, member in targets:
        key = guild.id

        if key not in seen:
            seen.add(key)
            unique.append(
                (
                    guild,
                    member,
                )
            )

    return unique


async def send_match_notification(
    guild,
    title,
    description,
    color,
):
    channel = guild.system_channel

    if channel is None:
        for ch in guild.text_channels:
            perms = ch.permissions_for(guild.me)

            if perms.send_messages and perms.embed_links:
                channel = ch
                break

    if channel is None:
        return

    embed = base_embed(
        title,
        description,
        color,
    )

    try:
        await channel.send(
            embed=embed
        )
    except Exception:
        pass


async def notify_pre_match(match):
    match_id = int(match["id"])

    cache_key = f"{match_id}:pre"

    if match_notice_cache.get(cache_key):
        return

    home = match.get(
        "homeTeam",
        {},
    ).get(
        "name",
        "Home",
    )

    away = match.get(
        "awayTeam",
        {},
    ).get(
        "name",
        "Away",
    )

    match_time = format_match_time(
        match.get("utcDate", "")
    )

    targets = await get_channels_for_bet_match(
        match_id
    )

    for guild, member in targets:
        await send_match_notification(
            guild,
            "⏰ TRẬN SẮP BẮT ĐẦU",
            (
                f"⚽ **{home}** 🆚 **{away}**\n\n"
                f"🕐 Giờ đá: **{match_time}**\n"
                f"⏳ Còn khoảng **30 phút**\n\n"
                "💰 Hệ thống cược đã ghi nhận trận này."
            ),
            COLOR_ORANGE,
        )

    match_notice_cache[cache_key] = True


async def notify_match_started(match):
    match_id = int(match["id"])

    cache_key = f"{match_id}:started"

    if match_notice_cache.get(cache_key):
        return

    home = match.get(
        "homeTeam",
        {},
    ).get(
        "name",
        "Home",
    )

    away = match.get(
        "awayTeam",
        {},
    ).get(
        "name",
        "Away",
    )

    targets = await get_channels_for_bet_match(
        match_id
    )

    for guild, member in targets:
        await send_match_notification(
            guild,
            "🟢 TRẬN ĐÃ BẮT ĐẦU",
            (
                f"🔴 **{home}** 🆚 **{away}**\n\n"
                "⚽ Trận đấu đã bắt đầu!\n"
                "🎯 Cược của bạn đang được theo dõi."
            ),
            COLOR_GREEN,
        )

    match_notice_cache[cache_key] = True


async def notify_match_finished(
    match,
    settled,
):
    match_id = int(match["id"])

    cache_key = f"{match_id}:finished"

    if match_notice_cache.get(cache_key):
        return

    home = match.get(
        "homeTeam",
        {},
    ).get(
        "name",
        "Home",
    )

    away = match.get(
        "awayTeam",
        {},
    ).get(
        "name",
        "Away",
    )

    score = match.get(
        "score",
        {},
    ).get(
        "fullTime",
        {},
    )

    home_score = score.get("home")
    away_score = score.get("away")

    winner = match.get(
        "score",
        {},
    ).get(
        "winner"
    )

    if home_score is None or away_score is None:
        return

    if home_score > away_score:
        result = "HOME"
    elif home_score < away_score:
        result = "AWAY"
    else:
        result = "DRAW"

    targets = await get_channels_for_bet_match(
        match_id
    )

    # Dùng settled để biết người thắng/thua.
    by_user = {}

    for item in settled:
        by_user.setdefault(
            item["user_id"],
            [],
        ).append(item)

    for guild, member in targets:
        user_bets = by_user.get(
            member.id,
            [],
        )

        lines = []

        for item in user_bets:
            if item["won"]:
                lines.append(
                    f"🎉 **THẮNG** • "
                    f"{item['choice']} • "
                    f"+{item['payout']:,} 🪙"
                )
            else:
                lines.append(
                    f"❌ **THUA** • "
                    f"{item['choice']} • "
                    f"-{item['stake']:,} 🪙"
                )

        if not lines:
            continue

        embed = base_embed(
            "🏁 KẾT THÚC TRẬN — KẾT TOÁN CƯỢC",
            (
                f"⚽ **{home}** "
                f"**{home_score} - {away_score}** "
                f"**{away}**\n\n"
                f"🏆 Kết quả: **{result_text(result)}**\n\n"
                + "\n".join(lines)
            ),
            COLOR_GREEN if any(
                x["won"]
                for x in user_bets
            ) else COLOR_RED,
        )

        channel = guild.system_channel

        if channel is None:
            for ch in guild.text_channels:
                if ch.permissions_for(
                    guild.me
                ).send_messages:
                    channel = ch
                    break

        if channel:
            try:
                await channel.send(
                    content=member.mention,
                    embed=embed,
                )
            except Exception:
                pass

    match_notice_cache[cache_key] = True


async def monitor_bets():
    """
    Chỉ theo dõi các trận đang có cược.
    Không crawl toàn bộ thế giới.
    """

    try:
        async with db_lock:
            conn = db_connect()

            rows = conn.execute(
                """
                SELECT DISTINCT match_id
                FROM bets
                WHERE status = 'OPEN'
                """
            ).fetchall()

            conn.close()

        if not rows:
            return

        match_ids = [
            int(row["match_id"])
            for row in rows
        ]

        for match_id in match_ids:
            try:
                match = await get_match(
                    match_id
                )

                if not match:
                    continue

                status = match.get(
                    "status"
                )

                match_dt = parse_iso_datetime(
                    match.get("utcDate", "")
                )

                if match_dt:
                    minutes_to_start = (
                        match_dt - now_utc()
                    ).total_seconds() / 60

                    if (
                        0
                        <= minutes_to_start
                        <= PRE_MATCH_NOTICE_MINUTES
                    ):
                        await notify_pre_match(
                            match
                        )

                if status in {
                    "LIVE",
                    "IN_PLAY",
                    "PAUSED",
                }:
                    await notify_match_started(
                        match
                    )

                if status == "FINISHED":
                    score = match.get(
                        "score",
                        {},
                    ).get(
                        "fullTime",
                        {},
                    )

                    if (
                        score.get("home") is None
                        or score.get("away") is None
                    ):
                        continue

                    home_score = score["home"]
                    away_score = score["away"]

                    if home_score > away_score:
                        result = "HOME"
                    elif home_score < away_score:
                        result = "AWAY"
                    else:
                        result = "DRAW"

                    settled = await settle_match_bets(
                        match_id,
                        result,
                    )

                    await notify_match_finished(
                        match,
                        settled,
                    )

                    # Clear một số cache.
                    for key in list(
                        match_notice_cache.keys()
                    ):
                        if key.startswith(
                            f"{match_id}:"
                        ):
                            pass

            except Exception as e:
                print(
                    f"[MONITOR] Match {match_id}: {e}"
                )

            # Tránh bắn API liên tục.
            await asyncio.sleep(0.5)

    except Exception as e:
        print("[MONITOR ERROR]", e)


# =========================================================
# COMMANDS
# =========================================================

@bot.tree.command(
    name="cuoc",
    description="⚽ Mở khu vực đặt cược bóng đá",
)
async def cuoc(
    interaction: discord.Interaction,
):
    await ensure_user(
        interaction.user.id
    )

    embed = base_embed(
        "⚽ KHU VỰC ĐẶT CƯỢC",
        (
            "Chọn một giải đấu để xem các trận sắp tới.\n\n"
            "🔵 **Man City** để xem tất cả trận Man City.\n"
            "💰 Chọn trận → nhập tiền → chọn Home/Draw/Away."
        ),
        COLOR_BLUE,
    )

    embed.add_field(
        name="💵 Thanh toán",
        value=f"Thắng = **{BET_PAYOUT}×** tiền cược",
        inline=True,
    )

    embed.add_field(
        name="🪙 Đơn vị",
        value="Coin ảo",
        inline=True,
    )

    await interaction.response.send_message(
        embed=embed,
        view=FootballMainView("bet"),
        ephemeral=True,
    )


@bot.tree.command(
    name="soi",
    description="🔎 Soi trận và dự đoán tỉ số",
)
async def soi(
    interaction: discord.Interaction,
):
    embed = base_embed(
        "🔎 SOI TRẬN BÓNG ĐÁ",
        (
            "Chọn giải đấu hoặc **🔵 Man City**.\n\n"
            "Bot sẽ dùng form gần đây + mô hình Poisson "
            "để ước tính:\n"
            "• Tỉ số\n"
            "• Home / Draw / Away\n"
            "• Form hai đội\n"
            "• Một số tỉ số có xác suất cao"
        ),
        COLOR_PURPLE,
    )

    await interaction.response.send_message(
        embed=embed,
        view=FootballMainView("predict"),
        ephemeral=True,
    )


@bot.tree.command(
    name="vi",
    description="💳 Xem ví coin và cấp VIP",
)
async def vi(
    interaction: discord.Interaction,
):
    wallet = await get_wallet(
        interaction.user.id
    )

    balance = int(wallet["balance"])
    vip = get_vip_level(balance)

    next_level, next_threshold = get_next_vip(
        balance
    )

    if next_level:
        remaining = next_threshold - balance

        next_text = (
            f"**{VIP_NAMES[next_level]}**\n"
            f"🎯 Cần thêm: **{remaining:,} 🪙**\n"
            f"📌 Mốc: **{next_threshold:,} 🪙**"
        )
    else:
        next_text = "👑 Bạn đã đạt VIP 10 — mốc cao nhất."

    embed = base_embed(
        "💳 VÍ CỦA BẠN",
        f"👤 {interaction.user.mention}",
        COLOR_GOLD,
    )

    embed.add_field(
        name="🪙 Số dư",
        value=f"**{balance:,}**",
        inline=True,
    )

    embed.add_field(
        name="👑 VIP hiện tại",
        value=f"**{VIP_NAMES[vip]}**",
        inline=True,
    )

    embed.add_field(
        name="🚀 Cấp tiếp theo",
        value=next_text,
        inline=False,
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
    )


@bot.tree.command(
    name="comat",
    description="🎁 Nhận 50,000 coin mỗi ngày",
)
async def comat(
    interaction: discord.Interaction,
):
    success, balance = await claim_daily(
        interaction.user.id
    )

    if not success:
        await interaction.response.send_message(
            (
                "⏳ Hôm nay bạn đã nhận **50,000 🪙** rồi.\n"
                f"💳 Số dư hiện tại: **{balance:,} 🪙**"
            ),
            ephemeral=True,
        )
        return

    wallet = await get_wallet(
        interaction.user.id
    )

    vip = wallet["vip_level"]

    embed = base_embed(
        "🎁 NHẬN COIN THÀNH CÔNG",
        (
            "💰 Bạn nhận được **50,000 🪙** hôm nay!\n\n"
            f"💳 Số dư: **{balance:,} 🪙**\n"
            f"👑 VIP: **{VIP_NAMES[vip]}**"
        ),
        COLOR_GREEN,
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
    )


@bot.tree.command(
    name="khoinghiep",
    description="🚀 Nhận 1,000 coin khởi nghiệp một lần",
)
async def khoinghiep(
    interaction: discord.Interaction,
):
    success, balance = await claim_starter(
        interaction.user.id
    )

    if not success:
        await interaction.response.send_message(
            (
                "❌ Bạn đã nhận coin khởi nghiệp trước đó.\n"
                f"💳 Số dư: **{balance:,} 🪙**"
            ),
            ephemeral=True,
        )
        return

    embed = base_embed(
        "🚀 KHỞI NGHIỆP THÀNH CÔNG",
        (
            "🎁 Bạn nhận thêm **1,000 🪙**!\n\n"
            f"💳 Số dư: **{balance:,} 🪙**"
        ),
        COLOR_CYAN,
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
    )


@bot.tree.command(
    name="admin",
    description="👑 Mở bảng quản trị",
)
async def admin(
    interaction: discord.Interaction,
):
    if not is_admin_user(
        interaction.user.id
    ):
        await interaction.response.send_message(
            "❌ Bạn không có quyền dùng admin.",
            ephemeral=True,
        )
        return

    await interaction.response.send_modal(
        AdminPasswordModal()
    )


@bot.tree.command(
    name="kettoan",
    description="🏁 Mở xác thực admin để kiểm tra kết toán cược",
)
async def kettoan(
    interaction: discord.Interaction,
):
    if not is_admin_user(
        interaction.user.id
    ):
        await interaction.response.send_message(
            "❌ Bạn không có quyền dùng admin.",
            ephemeral=True,
        )
        return

    await interaction.response.send_modal(
        SettleAdminPasswordModal()
    )


class SettleAdminPasswordModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(
            title="🔐 Xác thực kết toán"
        )

        self.password = discord.ui.TextInput(
            label="Mật khẩu admin",
            placeholder="Nhập mật khẩu...",
            required=True,
        )

        self.add_item(self.password)

    async def on_submit(self, interaction):
        if self.password.value != ADMIN_PASSWORD:
            await interaction.response.send_message(
                "❌ Sai mật khẩu.",
                ephemeral=True,
            )
            return

        async with db_lock:
            conn = db_connect()

            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM bets
                WHERE status = 'OPEN'
                """
            ).fetchone()

            conn.close()

        embed = base_embed(
            "🏁 HỆ THỐNG KẾT TOÁN",
            (
                f"📌 Hiện có **{row['count']} cược đang mở**.\n\n"
                "Bot v3 đã có hệ thống tự kết toán khi API "
                "trả trạng thái **FINISHED**."
            ),
            COLOR_GREEN,
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
        )


@bot.tree.command(
    name="cado",
    description="📊 Xem số cược đang mở",
)
async def cado(
    interaction: discord.Interaction,
):
    async with db_lock:
        conn = db_connect()

        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(stake), 0) AS amount
            FROM bets
            WHERE status = 'OPEN'
            """
        ).fetchone()

        conn.close()

    embed = base_embed(
        "📊 CƯỢC ĐANG MỞ",
        (
            f"🎯 Số cược: **{row['total']}**\n"
            f"🪙 Tổng tiền đang cược: **{row['amount']:,}**"
        ),
        COLOR_BLUE,
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
    )


@bot.tree.command(
    name="ping",
    description="🏓 Kiểm tra bot",
)
async def ping(
    interaction: discord.Interaction,
):
    latency = round(
        bot.latency * 1000
    )

    embed = base_embed(
        "🏓 PONG",
        f"⚡ WebSocket: **{latency} ms**",
        COLOR_GREEN,
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
    )


@bot.tree.command(
    name="apiquota",
    description="📡 Xem trạng thái quota Football API",
)
async def apiquota(
    interaction: discord.Interaction,
):
    embed = base_embed(
        "📡 FOOTBALL API",
        (
            f"📊 Requests còn lại: "
            f"**{bot.last_api_remaining}**\n"
            f"⏳ Reset sau: "
            f"**{bot.last_api_reset}s**"
        ),
        COLOR_CYAN,
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
    )


# =========================================================
# RAILWAY HEALTH SERVER
# =========================================================

async def health_handler(request):
    return web.json_response(
        {
            "status": "ok",
            "bot": str(bot.user) if bot.user else None,
            "time": now_utc().isoformat(),
        }
    )


async def start_health_server():
    global health_runner

    port = int(
        os.getenv("PORT", "8080")
    )

    app = web.Application()

    app.router.add_get(
        "/",
        health_handler,
    )

    app.router.add_get(
        "/health",
        health_handler,
    )

    runner = web.AppRunner(app)

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        port,
    )

    await site.start()

    health_runner = runner

    print(
        f"Health server running on port {port}"
    )


# =========================================================
# READY
# =========================================================

@bot.event
async def on_ready():
    print(
        f"Logged in as {bot.user} "
        f"(ID: {bot.user.id})"
    )

    print(
        f"Guilds: {len(bot.guilds)}"
    )

    if not hasattr(
        bot,
        "_health_started",
    ):
        bot._health_started = True

        try:
            await start_health_server()
        except Exception as e:
            print(
                "Health server error:",
                e,
            )


# =========================================================
# ERROR HANDLER
# =========================================================

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error,
):
    print(
        "Slash command error:",
        repr(error),
    )

    message = (
        "❌ Có lỗi xảy ra khi thực hiện lệnh."
    )

    if isinstance(
        error,
        app_commands.CommandOnCooldown,
    ):
        message = (
            f"⏳ Thử lại sau "
            f"{error.retry_after:.1f}s."
        )

    try:
        if interaction.response.is_done():
            await interaction.followup.send(
                message,
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                message,
                ephemeral=True,
            )
    except Exception:
        pass


# =========================================================
# START
# =========================================================

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError(
            "Thiếu DISCORD_TOKEN."
        )

    if not FOOTBALL_DATA_API_KEY:
        raise RuntimeError(
            "Thiếu FOOTBALL_DATA_API_KEY."
        )

    init_db()

    bot.run(DISCORD_TOKEN)

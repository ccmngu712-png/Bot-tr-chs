import os
import asyncio
import sqlite3
import math
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from aiohttp import web


# =========================================================
# CONFIG
# =========================================================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID_RAW = os.getenv("GUILD_ID", "").strip()

FOOTBALL_DATA_API_KEY = (
    os.getenv("FOOTBALL_DATA_API_KEY", "").strip()
    or os.getenv("FOOTBALL_DATA_TOKEN", "").strip()
    or os.getenv("FOOTBALL_DATA_KEY", "").strip()
)

ADMIN_PASSWORD = os.getenv(
    "ADMIN_PASSWORD",
    "provip👑"
)

ADMIN_USER_IDS_RAW = os.getenv(
    "ADMIN_USER_IDS",
    ""
).strip()

API_BASE = "https://api.football-data.org/v4"

# Korea
TZ = timezone(timedelta(hours=9))

STARTING_BALANCE = 100_000
STARTER_BONUS = 1_000
DAILY_BONUS = 50_000
BET_PAYOUT = 2

MANCHESTER_CITY_TEAM_ID = 65

MATCH_CHECK_SECONDS = 60
API_CACHE_SECONDS = 60
RECENT_FORM_CACHE_SECONDS = 300

DB_PATH = (
    "/data/football_bot.db"
    if os.path.isdir("/data")
    else "football_bot.db"
)


# =========================================================
# DISCORD
# =========================================================

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


# =========================================================
# COLORS
# =========================================================

COLOR_BLUE = discord.Color.blue()
COLOR_GREEN = discord.Color.green()
COLOR_RED = discord.Color.red()
COLOR_ORANGE = discord.Color.orange()
COLOR_GOLD = discord.Color.gold()


# =========================================================
# GUILD
# =========================================================

GUILD = None

if GUILD_ID_RAW.isdigit():
    GUILD = discord.Object(
        id=int(GUILD_ID_RAW)
    )


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

VIP_THRESHOLDS = {
    10: 1_000_000,
    9: 800_000,
    8: 650_000,
    7: 500_000,
    6: 350_000,
    5: 250_000,
    4: 175_000,
    3: 125_000,
    2: 100_000,
    1: 50_000,
    0: 0,
}


VIP_NAMES = {
    10: "VIP 10 👑",
    9: "VIP 9 💎",
    8: "VIP 8 💠",
    7: "VIP 7 🔥",
    6: "VIP 6 ⭐",
    5: "VIP 5 🏆",
    4: "VIP 4 🥇",
    3: "VIP 3 🥈",
    2: "VIP 2 🥉",
    1: "VIP 1 🔵",
    0: "Thành viên",
}


def get_vip(balance):
    balance = int(balance)

    for level in range(10, -1, -1):
        if balance >= VIP_THRESHOLDS[level]:
            return level

    return 0


def vip_name(level):
    return VIP_NAMES.get(
        int(level),
        "Thành viên"
    )


def vip_icon(level):
    icons = {
        10: "👑",
        9: "💎",
        8: "💠",
        7: "🔥",
        6: "⭐",
        5: "🏆",
        4: "🥇",
        3: "🥈",
        2: "🥉",
        1: "🔵",
        0: "👤",
    }

    return icons.get(
        int(level),
        "👤"
    )


def get_next_vip(level):
    level = int(level)

    if level >= 10:
        return None, None

    next_level = level + 1

    return (
        next_level,
        VIP_THRESHOLDS[next_level]
    )


# =========================================================
# DATABASE
# =========================================================

db_lock = asyncio.Lock()


def db_connect():
    conn = sqlite3.connect(
        DB_PATH,
        timeout=30
    )

    conn.row_factory = sqlite3.Row

    return conn


def init_db():
    conn = db_connect()

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wallets (
            user_id INTEGER PRIMARY KEY,
            balance INTEGER NOT NULL DEFAULT 100000,
            starter_claimed INTEGER NOT NULL DEFAULT 0,
            vip_level INTEGER NOT NULL DEFAULT 2
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_claims (
            user_id INTEGER NOT NULL,
            claim_date TEXT NOT NULL,
            PRIMARY KEY(user_id, claim_date)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            match_id INTEGER NOT NULL,
            league TEXT NOT NULL,
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

    conn.commit()

    # Migration nếu database cũ thiếu cột
    columns = {
        row["name"]
        for row in conn.execute(
            "PRAGMA table_info(bets)"
        ).fetchall()
    }

    if "league" not in columns:
        conn.execute(
            """
            ALTER TABLE bets
            ADD COLUMN league TEXT NOT NULL DEFAULT 'PL'
            """
        )

    conn.commit()
    conn.close()


async def ensure_user(user_id):
    async with db_lock:
        conn = db_connect()

        row = conn.execute(
            """
            SELECT *
            FROM wallets
            WHERE user_id=?
            """,
            (user_id,)
        ).fetchone()

        if row is None:
            conn.execute(
                """
                INSERT INTO wallets
                (
                    user_id,
                    balance,
                    starter_claimed,
                    vip_level
                )
                VALUES (?, ?, 0, ?)
                """,
                (
                    user_id,
                    STARTING_BALANCE,
                    get_vip(STARTING_BALANCE)
                )
            )

            conn.commit()

        conn.close()


async def get_wallet(user_id):
    await ensure_user(user_id)

    async with db_lock:
        conn = db_connect()

        row = conn.execute(
            """
            SELECT *
            FROM wallets
            WHERE user_id=?
            """,
            (user_id,)
        ).fetchone()

        conn.close()

    return row


async def get_balance(user_id):
    row = await get_wallet(user_id)

    return int(row["balance"])


async def change_balance(user_id, amount):
    await ensure_user(user_id)

    async with db_lock:
        conn = db_connect()

        old = conn.execute(
            """
            SELECT balance, vip_level
            FROM wallets
            WHERE user_id=?
            """,
            (user_id,)
        ).fetchone()

        old_balance = int(old["balance"])
        old_vip = int(old["vip_level"])

        new_balance = old_balance + int(amount)

        if new_balance < 0:
            conn.close()
            return False, old_balance, old_vip, old_vip

        new_vip = get_vip(new_balance)

        conn.execute(
            """
            UPDATE wallets
            SET balance=?,
                vip_level=?
            WHERE user_id=?
            """,
            (
                new_balance,
                new_vip,
                user_id
            )
        )

        conn.commit()
        conn.close()

    return (
        True,
        old_balance,
        old_vip,
        new_vip
    )


# =========================================================
# VIP ANNOUNCEMENT
# =========================================================

async def get_notification_channel(guild):
    if guild is None:
        return None

    if guild.system_channel:
        perms = guild.system_channel.permissions_for(
            guild.me
        )

        if perms.send_messages:
            return guild.system_channel

    for channel in guild.text_channels:
        perms = channel.permissions_for(
            guild.me
        )

        if perms.send_messages and perms.embed_links:
            return channel

    return None


async def announce_vip(user_id, new_vip):
    if new_vip <= 0:
        return

    for guild in bot.guilds:
        member = guild.get_member(user_id)

        if member is None:
            continue

        channel = await get_notification_channel(
            guild
        )

        if channel is None:
            continue

        embed = discord.Embed(
            title="🎉 THĂNG CẤP VIP!",
            description=(
                f"🎊 {member.mention} đã đạt "
                f"**{vip_name(new_vip)}**!\n\n"
                "🔥 Chúc mừng!"
            ),
            color=COLOR_GOLD
        )

        try:
            await channel.send(
                embed=embed
            )
        except Exception as exc:
            print(
                f"[VIP] {exc}"
            )


async def check_vip(user_id):
    row = await get_wallet(user_id)

    balance = int(row["balance"])
    old_vip = int(row["vip_level"])
    new_vip = get_vip(balance)

    if new_vip != old_vip:
        async with db_lock:
            conn = db_connect()

            conn.execute(
                """
                UPDATE wallets
                SET vip_level=?
                WHERE user_id=?
                """,
                (
                    new_vip,
                    user_id
                )
            )

            conn.commit()
            conn.close()

        if new_vip > old_vip:
            await announce_vip(
                user_id,
                new_vip
            )


# =========================================================
# STARTER / DAILY
# =========================================================

async def claim_starter(user_id):
    await ensure_user(user_id)

    async with db_lock:
        conn = db_connect()

        row = conn.execute(
            """
            SELECT starter_claimed
            FROM wallets
            WHERE user_id=?
            """,
            (user_id,)
        ).fetchone()

        if row["starter_claimed"]:
            conn.close()
            return False

        conn.execute(
            """
            UPDATE wallets
            SET balance=balance+?,
                starter_claimed=1
            WHERE user_id=?
            """,
            (
                STARTER_BONUS,
                user_id
            )
        )

        conn.commit()
        conn.close()

    await check_vip(user_id)

    return True


async def claim_daily(user_id):
    await ensure_user(user_id)

    today = datetime.now(
        TZ
    ).date().isoformat()

    async with db_lock:
        conn = db_connect()

        row = conn.execute(
            """
            SELECT 1
            FROM daily_claims
            WHERE user_id=?
            AND claim_date=?
            """,
            (
                user_id,
                today
            )
        ).fetchone()

        if row:
            conn.close()
            return False

        conn.execute(
            """
            INSERT INTO daily_claims
            (
                user_id,
                claim_date
            )
            VALUES (?, ?)
            """,
            (
                user_id,
                today
            )
        )

        conn.execute(
            """
            UPDATE wallets
            SET balance=balance+?
            WHERE user_id=?
            """,
            (
                DAILY_BONUS,
                user_id
            )
        )

        conn.commit()
        conn.close()

    await check_vip(user_id)

    return True


# =========================================================
# BET DATABASE
# =========================================================

async def create_bet(
    user_id,
    match_id,
    league,
    home_team,
    away_team,
    choice,
    stake
):
    await ensure_user(user_id)

    async with db_lock:
        conn = db_connect()

        row = conn.execute(
            """
            SELECT balance
            FROM wallets
            WHERE user_id=?
            """,
            (user_id,)
        ).fetchone()

        balance = int(
            row["balance"]
        )

        if balance < stake:
            conn.close()
            return False, "NOT_ENOUGH"

        existing = conn.execute(
            """
            SELECT id
            FROM bets
            WHERE user_id=?
            AND match_id=?
            AND status='OPEN'
            """,
            (
                user_id,
                match_id
            )
        ).fetchone()

        if existing:
            conn.close()
            return False, "EXISTS"

        conn.execute(
            """
            UPDATE wallets
            SET balance=balance-?
            WHERE user_id=?
            """,
            (
                stake,
                user_id
            )
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
                datetime.now(TZ).isoformat()
            )
        )

        conn.commit()
        conn.close()

    return True, "OK"


async def get_open_bets():
    async with db_lock:
        conn = db_connect()

        rows = conn.execute(
            """
            SELECT *
            FROM bets
            WHERE status='OPEN'
            ORDER BY id ASC
            """
        ).fetchall()

        conn.close()

    return rows


async def settle_match_bets(
    match_id,
    result
):
    """
    result:
        HOME
        DRAW
        AWAY

    Trả về danh sách cược đã settlement.
    """

    async with db_lock:
        conn = db_connect()

        rows = conn.execute(
            """
            SELECT *
            FROM bets
            WHERE match_id=?
            AND status='OPEN'
            ORDER BY id ASC
            """,
            (match_id,)
        ).fetchall()

        settled = []

        for bet in rows:
            user_id = int(
                bet["user_id"]
            )

            stake = int(
                bet["stake"]
            )

            choice = str(
                bet["choice"]
            )

            won = (
                choice == result
            )

            payout = (
                stake * BET_PAYOUT
                if won
                else 0
            )

            wallet = conn.execute(
                """
                SELECT balance, vip_level
                FROM wallets
                WHERE user_id=?
                """,
                (user_id,)
            ).fetchone()

            old_balance = int(
                wallet["balance"]
            )

            old_vip = int(
                wallet["vip_level"]
            )

            new_balance = (
                old_balance + payout
            )

            new_vip = get_vip(
                new_balance
            )

            conn.execute(
                """
                UPDATE wallets
                SET balance=?,
                    vip_level=?
                WHERE user_id=?
                """,
                (
                    new_balance,
                    new_vip,
                    user_id
                )
            )

            conn.execute(
                """
                UPDATE bets
                SET status=?,
                    payout=?,
                    settled_at=?
                WHERE id=?
                """,
                (
                    "WON" if won else "LOST",
                    payout,
                    datetime.now(
                        TZ
                    ).isoformat(),
                    int(bet["id"])
                )
            )

            settled.append({
                "id": int(bet["id"]),
                "user_id": user_id,
                "match_id": int(match_id),
                "league": bet["league"],
                "home_team": bet["home_team"],
                "away_team": bet["away_team"],
                "choice": choice,
                "stake": stake,
                "payout": payout,
                "won": won,
                "old_balance": old_balance,
                "balance_after": new_balance,
                "old_vip": old_vip,
                "new_vip": new_vip,
            })

        conn.commit()
        conn.close()

    return settled


# =========================================================
# FOOTBALL DATA API
# =========================================================

class FootballAPI:

    def __init__(self):
        self.session = None
        self.cache = {}
        self.cache_lock = asyncio.Lock()

    async def start(self):
        if self.session is None:
            self.session = aiohttp.ClientSession(
                headers={
                    "X-Auth-Token":
                        FOOTBALL_DATA_API_KEY
                }
            )

    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None

    async def request(
        self,
        path,
        params=None,
        cache_seconds=API_CACHE_SECONDS
    ):
        if not FOOTBALL_DATA_API_KEY:
            raise RuntimeError(
                "Chưa có FOOTBALL_DATA_API_KEY."
            )

        await self.start()

        url = (
            f"{API_BASE}{path}"
        )

        key = (
            url,
            tuple(
                sorted(
                    (params or {}).items()
                )
            )
        )

        now = asyncio.get_running_loop().time()

        async with self.cache_lock:
            cached = self.cache.get(key)

            if cached:
                timestamp, data = cached

                if (
                    now - timestamp
                    < cache_seconds
                ):
                    return data

        async with self.session.get(
            url,
            params=params,
            timeout=aiohttp.ClientTimeout(
                total=20
            )
        ) as response:

            text = await response.text()

            if response.status != 200:
                raise RuntimeError(
                    f"Football API {response.status}: "
                    f"{text[:500]}"
                )

            data = await response.json()

        async with self.cache_lock:
            self.cache[key] = (
                now,
                data
            )

        return data

    async def next_league(
        self,
        league_code,
        limit=20
    ):
        if league_code not in FREE_COMPETITIONS:
            return []

        today = datetime.now(
            timezone.utc
        ).date()

        end = today + timedelta(
            days=60
        )

        data = await self.request(
            f"/competitions/{league_code}/matches",
            {
                "dateFrom":
                    today.isoformat(),
                "dateTo":
                    end.isoformat(),
                "status":
                    "SCHEDULED,TIMED",
            }
        )

        matches = data.get(
            "matches",
            []
        )

        matches.sort(
            key=lambda x:
                x.get("utcDate", "")
        )

        return matches[:limit]

    async def next_team(
        self,
        team_id,
        limit=50
    ):
        today = datetime.now(
            timezone.utc
        ).date()

        end = today + timedelta(
            days=90
        )

        data = await self.request(
            f"/teams/{team_id}/matches",
            {
                "dateFrom":
                    today.isoformat(),
                "dateTo":
                    end.isoformat(),
                "status":
                    "SCHEDULED,TIMED",
                "limit":
                    100
            }
        )

        matches = data.get(
            "matches",
            []
        )

        matches.sort(
            key=lambda x:
                x.get("utcDate", "")
        )

        return matches[:limit]

    async def match(
        self,
        match_id
    ):
        return await self.request(
            f"/matches/{match_id}"
        )

    async def recent_team(
        self,
        team_id
    ):
        today = datetime.now(
            timezone.utc
        ).date()

        start = today - timedelta(
            days=120
        )

        data = await self.request(
            f"/teams/{team_id}/matches",
            {
                "dateFrom":
                    start.isoformat(),
                "dateTo":
                    today.isoformat(),
                "status":
                    "FINISHED",
                "limit":
                    20
            },
            cache_seconds=RECENT_FORM_CACHE_SECONDS
        )

        return data.get(
            "matches",
            []
        )


football = FootballAPI()


# =========================================================
# MATCH HELPERS
# =========================================================

def match_home(match):
    return (
        match.get("homeTeam", {})
        .get("name", "Home")
    )


def match_away(match):
    return (
        match.get("awayTeam", {})
        .get("name", "Away")
    )


def match_id(match):
    return int(
        match["id"]
    )


def match_time(match):
    value = match.get(
        "utcDate"
    )

    if not value:
        return None

    try:
        return datetime.fromisoformat(
            value.replace(
                "Z",
                "+00:00"
            )
        )
    except Exception:
        return None


def is_upcoming(match):
    dt = match_time(match)

    if dt is None:
        return False

    return dt > datetime.now(
        timezone.utc
    )


def display_time(match):
    dt = match_time(match)

    if dt is None:
        return "Chưa rõ giờ"

    return dt.astimezone(
        TZ
    ).strftime(
        "%d/%m/%Y %H:%M"
    )


def match_score(match):
    score = match.get(
        "score",
        {}
    )

    full = score.get(
        "fullTime",
        {}
    )

    home = full.get("home")
    away = full.get("away")

    if home is None or away is None:
        return "Chưa đá"

    return f"{home} - {away}"


def match_result(match):
    score = match.get(
        "score",
        {}
    ).get(
        "fullTime",
        {}
    )

    home = score.get("home")
    away = score.get("away")

    if home is None or away is None:
        return None

    if home > away:
        return "HOME"

    if away > home:
        return "AWAY"

    return "DRAW"


def choice_text(
    choice,
    home,
    away
):
    return {
        "HOME":
            f"🏠 {home}",
        "DRAW":
            "🤝 Hòa",
        "AWAY":
            f"✈️ {away}",
    }.get(
        choice,
        choice
    )


def format_money(value):
    return f"{int(value):,}"


def base_embed(
    title,
    description,
    color=COLOR_BLUE
):
    return discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.now(TZ)
    )


# =========================================================
# PREDICTION
# =========================================================

def poisson_probability(
    goals,
    expected
):
    return (
        math.exp(-expected)
        * expected ** goals
        / math.factorial(goals)
    )


async def team_stats(team_id):
    matches = await football.recent_team(
        team_id
    )

    gf = []
    ga = []
    points = []

    for match in matches:
        home_id = (
            match.get("homeTeam", {})
            .get("id")
        )

        away_id = (
            match.get("awayTeam", {})
            .get("id")
        )

        score = (
            match.get("score", {})
            .get("fullTime", {})
        )

        hs = score.get("home")
        aw = score.get("away")

        if hs is None or aw is None:
            continue

        if team_id == home_id:
            gf.append(hs)
            ga.append(aw)

            if hs > aw:
                points.append(3)
            elif hs == aw:
                points.append(1)
            else:
                points.append(0)

        elif team_id == away_id:
            gf.append(aw)
            ga.append(hs)

            if aw > hs:
                points.append(3)
            elif aw == hs:
                points.append(1)
            else:
                points.append(0)

    if not gf:
        return {
            "gf": 1.35,
            "ga": 1.10,
            "ppg": 1.40,
        }

    return {
        "gf": sum(gf) / len(gf),
        "ga": sum(ga) / len(ga),
        "ppg": (
            sum(points) / len(points)
            if points
            else 1.4
        ),
    }


async def predict_match(match):
    home_id = (
        match.get("homeTeam", {})
        .get("id")
    )

    away_id = (
        match.get("awayTeam", {})
        .get("id")
    )

    home_stats = await team_stats(
        home_id
    )

    away_stats = await team_stats(
        away_id
    )

    home_lambda = (
        home_stats["gf"] * 0.55
        + away_stats["ga"] * 0.45
    )

    away_lambda = (
        away_stats["gf"] * 0.55
        + home_stats["ga"] * 0.45
    )

    # sân nhà
    home_lambda *= 1.08
    away_lambda *= 0.94

    # form PPG
    form_difference = (
        home_stats["ppg"]
        - away_stats["ppg"]
    )

    adjustment = max(
        -0.25,
        min(
            0.25,
            form_difference * 0.08
        )
    )

    home_lambda *= (
        1 + adjustment
    )

    away_lambda *= (
        1 - adjustment
    )

    home_lambda = max(
        0.20,
        home_lambda
    )

    away_lambda = max(
        0.20,
        away_lambda
    )

    outcomes = {
        "HOME": 0.0,
        "DRAW": 0.0,
        "AWAY": 0.0,
    }

    scores = []

    for h in range(7):
        for a in range(7):

            probability = (
                poisson_probability(
                    h,
                    home_lambda
                )
                * poisson_probability(
                    a,
                    away_lambda
                )
            )

            scores.append(
                (
                    probability,
                    h,
                    a
                )
            )

            if h > a:
                outcomes["HOME"] += probability

            elif h == a:
                outcomes["DRAW"] += probability

            else:
                outcomes["AWAY"] += probability

    total = sum(
        outcomes.values()
    )

    for key in outcomes:
        outcomes[key] = (
            outcomes[key]
            / total
        )

    scores.sort(
        reverse=True
    )

    top_score = scores[0]

    exact = (
        top_score[1],
        top_score[2]
    )

    return {
        "home_probability":
            outcomes["HOME"],
        "draw_probability":
            outcomes["DRAW"],
        "away_probability":
            outcomes["AWAY"],
        "exact":
            exact,
        "top_scores":
            scores[:3],
    }


# =========================================================
# RESULT DM
# =========================================================

async def send_result_dms(
    match,
    settled
):
    if not settled:
        return

    home = match_home(match)
    away = match_away(match)
    score = match_score(match)
    result = match_result(match)

    result_display = {
        "HOME":
            f"🏠 {home} thắng",
        "DRAW":
            "🤝 Trận đấu hòa",
        "AWAY":
            f"✈️ {away} thắng",
    }.get(
        result,
        "Chưa xác định"
    )

    grouped = defaultdict(list)

    for item in settled:
        grouped[
            item["user_id"]
        ].append(item)

    for user_id, bets in grouped.items():

        won = sum(
            1
            for x in bets
            if x["won"]
        )

        lost = len(bets) - won

        total_stake = sum(
            x["stake"]
            for x in bets
        )

        total_payout = sum(
            x["payout"]
            for x in bets
        )

        net = (
            total_payout
            - total_stake
        )

        final_balance = bets[-1][
            "balance_after"
        ]

        final_vip = bets[-1][
            "new_vip"
        ]

        if won and not lost:
            title = "🎉 BẠN THẮNG CƯỢC!"
            color = COLOR_GREEN

        elif lost and not won:
            title = "❌ BẠN THUA CƯỢC"
            color = COLOR_RED

        else:
            title = "📊 KẾT QUẢ CƯỢC"
            color = COLOR_ORANGE

        lines = []

        for item in bets:

            pick = choice_text(
                item["choice"],
                home,
                away
            )

            if item["won"]:

                lines.append(
                    (
                        f"🎉 **THẮNG** — {pick}\n"
                        f"🪙 Cược: "
                        f"**{format_money(item['stake'])}**\n"
                        f"💰 Nhận: "
                        f"**{format_money(item['payout'])}**"
                    )
                )

            else:

                lines.append(
                    (
                        f"❌ **THUA** — {pick}\n"
                        f"🪙 Mất: "
                        f"**{format_money(item['stake'])}**"
                    )
                )

        description = (
            f"⚽ **{home} "
            f"{score} "
            f"{away}**\n\n"
            f"🏆 Kết quả: **{result_display}**\n\n"
            + "\n\n".join(lines)
        )

        embed = base_embed(
            title,
            description,
            color
        )

        embed.add_field(
            name="💵 Tổng cược",
            value=(
                f"**{format_money(total_stake)} 🪙**"
            ),
            inline=True
        )

        embed.add_field(
            name="💰 Tổng nhận",
            value=(
                f"**{format_money(total_payout)} 🪙**"
            ),
            inline=True
        )

        net_text = (
            f"+{format_money(net)}"
            if net >= 0
            else f"-{format_money(abs(net))}"
        )

        embed.add_field(
            name="📈 Lãi / lỗ",
            value=f"**{net_text} 🪙**",
            inline=True
        )

        embed.add_field(
            name="💳 Số dư",
            value=(
                f"**{format_money(final_balance)} 🪙**"
            ),
            inline=True
        )

        embed.add_field(
            name="👑 VIP",
            value=vip_name(final_vip),
            inline=True
        )

        try:
            user = bot.get_user(
                user_id
            )

            if user is None:
                user = await bot.fetch_user(
                    user_id
                )

            await user.send(
                embed=embed
            )

            print(
                f"[DM] Sent result to {user_id}"
            )

        except discord.Forbidden:
            print(
                f"[DM] DM blocked for {user_id}"
            )

        except Exception as exc:
            print(
                f"[DM] Error {user_id}: {exc}"
            )

        # VIP nếu settlement làm tăng VIP
        max_old = max(
            x["old_vip"]
            for x in bets
        )

        if final_vip > max_old:
            await announce_vip(
                user_id,
                final_vip
            )


# =========================================================
# SERVER FINISHED NOTICE
# =========================================================

async def send_server_finished(
    match,
    settled
):
    if not settled:
        return

    home = match_home(match)
    away = match_away(match)
    score = match_score(match)
    result = match_result(match)

    result_display = {
        "HOME":
            f"🏠 {home} thắng",
        "DRAW":
            "🤝 Hòa",
        "AWAY":
            f"✈️ {away} thắng",
    }.get(
        result,
        "Không xác định"
    )

    total = len(settled)

    wins = sum(
        1
        for x in settled
        if x["won"]
    )

    losses = total - wins

    embed = base_embed(
        "🏁 TRẬN ĐẤU ĐÃ KẾT THÚC",
        (
            f"⚽ **{home} "
            f"{score} "
            f"{away}**\n\n"
            f"🏆 Kết quả: **{result_display}**\n\n"
            f"🎯 Tổng cược đã kết toán: **{total}**\n"
            f"🎉 Cược thắng: **{wins}**\n"
            f"❌ Cược thua: **{losses}**\n\n"
            "📩 Kết quả chi tiết đã được gửi "
            "**DM riêng** cho từng người cược."
        ),
        COLOR_GREEN
    )

    notified = set()

    for item in settled:
        user_id = item["user_id"]

        for guild in bot.guilds:

            if guild.id in notified:
                continue

            member = guild.get_member(
                user_id
            )

            if member is None:
                continue

            channel = await get_notification_channel(
                guild
            )

            if channel is None:
                continue

            try:
                await channel.send(
                    embed=embed
                )

                notified.add(
                    guild.id
                )

            except Exception as exc:
                print(
                    f"[SERVER] {exc}"
                )


# =========================================================
# MONITOR
# =========================================================

monitor_task = None

match_notice_cache = {}


async def monitor_bets():
    await bot.wait_until_ready()

    while not bot.is_closed():

        try:
            open_bets = await get_open_bets()

            match_ids = sorted({
                int(row["match_id"])
                for row in open_bets
            })

            for mid in match_ids:

                try:
                    match = await football.match(
                        mid
                    )

                    status = str(
                        match.get(
                            "status",
                            ""
                        )
                    ).upper()

                    # ==============================
                    # FINISHED
                    # ==============================

                    if status == "FINISHED":

                        result = match_result(
                            match
                        )

                        if result is None:
                            continue

                        settled = await settle_match_bets(
                            mid,
                            result
                        )

                        if settled:

                            await send_result_dms(
                                match,
                                settled
                            )

                            await send_server_finished(
                                match,
                                settled
                            )

                        continue

                    # ==============================
                    # LIVE
                    # ==============================

                    if status in {
                        "IN_PLAY",
                        "PAUSED"
                    }:

                        key = (
                            f"{mid}:started"
                        )

                        if not match_notice_cache.get(
                            key
                        ):

                            match_notice_cache[
                                key
                            ] = True

                            await send_server_started(
                                match
                            )

                    # ==============================
                    # 30 MIN NOTICE
                    # ==============================

                    await check_pre_match_notice(
                        match
                    )

                except Exception as exc:
                    print(
                        f"[MONITOR] Match {mid}: "
                        f"{exc}"
                    )

        except Exception as exc:
            print(
                f"[MONITOR] {exc}"
            )

        await asyncio.sleep(
            MATCH_CHECK_SECONDS
        )


async def check_pre_match_notice(
    match
):
    mid = match_id(match)

    dt = match_time(match)

    if dt is None:
        return

    seconds = (
        dt
        - datetime.now(
            timezone.utc
        )
    ).total_seconds()

    if (
        seconds <= 30 * 60
        and seconds > 0
    ):

        key = (
            f"{mid}:pre"
        )

        if match_notice_cache.get(
            key
        ):
            return

        match_notice_cache[
            key
        ] = True

        embed = base_embed(
            "⏰ TRẬN SẮP BẮT ĐẦU",
            (
                f"⚽ **{match_home(match)}** "
                f"vs "
                f"**{match_away(match)}**\n"
                f"🕐 {display_time(match)}\n\n"
                "🎯 Trận có người đang đặt cược."
            ),
            COLOR_ORANGE
        )

        # Chỉ báo server có người đang cược
        for row in await get_open_bets():
            if int(row["match_id"]) != mid:
                continue

            user_id = int(
                row["user_id"]
            )

            for guild in bot.guilds:
                if guild.get_member(
                    user_id
                ):

                    channel = (
                        await get_notification_channel(
                            guild
                        )
                    )

                    if channel:
                        try:
                            await channel.send(
                                embed=embed
                            )
                        except Exception:
                            pass

                    break


async def send_server_started(
    match
):
    embed = base_embed(
        "🔴 TRẬN ĐẤU ĐÃ BẮT ĐẦU",
        (
            f"⚽ **{match_home(match)}** "
            f"vs "
            f"**{match_away(match)}**\n\n"
            "📡 Trận đang diễn ra."
        ),
        COLOR_RED
    )

    users = {
        int(row["user_id"])
        for row in await get_open_bets()
        if int(row["match_id"])
        == match_id(match)
    }

    notified = set()

    for user_id in users:

        for guild in bot.guilds:

            if guild.id in notified:
                continue

            if guild.get_member(
                user_id
            ) is None:
                continue

            channel = await get_notification_channel(
                guild
            )

            if channel:

                try:
                    await channel.send(
                        embed=embed
                    )

                    notified.add(
                        guild.id
                    )

                except Exception:
                    pass


# =========================================================
# MATCH LIST
# =========================================================

def match_description(
    match
):
    return (
        f"⚽ **{match_home(match)}**\n"
        f"🆚 **{match_away(match)}**\n"
        f"🕐 {display_time(match)}"
    )


class MatchSelect(
    discord.ui.Select
):

    def __init__(
        self,
        matches,
        mode
    ):
        self.matches = matches
        self.mode = mode

        options = []

        for match in matches[:25]:

            label = (
                f"{match_home(match)} "
                f"vs "
                f"{match_away(match)}"
            )

            label = label[:100]

            options.append(
                discord.SelectOption(
                    label=label,
                    description=display_time(
                        match
                    )[:100],
                    value=str(
                        match_id(match)
                    )
                )
            )

        super().__init__(
            placeholder=(
                "⚽ Chọn trận..."
            ),
            options=options
        )

    async def callback(
        self,
        interaction
    ):
        selected = int(
            self.values[0]
        )

        match = next(
            (
                x
                for x in self.matches
                if match_id(x)
                == selected
            ),
            None
        )

        if match is None:
            await interaction.response.send_message(
                "❌ Không tìm thấy trận.",
                ephemeral=True
            )
            return

        if self.mode == "bet":

            await interaction.response.send_modal(
                StakeModal(
                    match,
                    self.mode
                )
            )

        else:

            await interaction.response.defer(
                ephemeral=True
            )

            try:
                prediction = await predict_match(
                    match
                )

                await show_prediction(
                    interaction,
                    match,
                    prediction
                )

            except Exception as exc:
                await interaction.followup.send(
                    f"❌ Không thể soi trận này:\n`{exc}`",
                    ephemeral=True
                )


class MatchView(
    discord.ui.View
):

    def __init__(
        self,
        matches,
        mode
    ):
        super().__init__(
            timeout=300
        )

        self.add_item(
            MatchSelect(
                matches,
                mode
            )
        )


# =========================================================
# STAKE MODAL
# =========================================================

class StakeModal(
    discord.ui.Modal,
    title="💰 Nhập tiền cược"
):

    amount = discord.ui.TextInput(
        label="Số tiền cược",
        placeholder="VD: 10000",
        required=True,
        min_length=1,
        max_length=12
    )

    def __init__(
        self,
        match,
        mode
    ):
        super().__init__()

        self.match = match
        self.mode = mode

    async def on_submit(
        self,
        interaction
    ):
        try:
            amount = int(
                str(
                    self.amount
                )
                .replace(",", "")
                .replace(".", "")
                .strip()
            )
        except ValueError:

            await interaction.response.send_message(
                "❌ Số tiền không hợp lệ.",
                ephemeral=True
            )

            return

        if amount <= 0:

            await interaction.response.send_message(
                "❌ Số tiền phải lớn hơn 0.",
                ephemeral=True
            )

            return

        if amount > 10_000_000:

            await interaction.response.send_message(
                "❌ Tối đa 10.000.000 xu.",
                ephemeral=True
            )

            return

        balance = await get_balance(
            interaction.user.id
        )

        if amount > balance:

            await interaction.response.send_message(
                (
                    "❌ Không đủ xu.\n"
                    f"💰 Ví: **{format_money(balance)} xu**"
                ),
                ephemeral=True
            )

            return

        embed = base_embed(
            "🎯 CHỌN CỬA",
            (
                f"⚽ **{match_home(self.match)}**\n"
                "🆚\n"
                f"⚽ **{match_away(self.match)}**\n\n"
                f"💰 Cược: **{format_money(amount)} xu**"
            ),
            COLOR_GOLD
        )

        await interaction.response.send_message(
            embed=embed,
            view=BetChoiceView(
                self.match,
                amount
            ),
            ephemeral=True
        )


# =========================================================
# BET CHOICE
# =========================================================

class BetChoiceView(
    discord.ui.View
):

    def __init__(
        self,
        match,
        amount
    ):
        super().__init__(
            timeout=120
        )

        self.match = match
        self.amount = amount

    async def place(
        self,
        interaction,
        choice
    ):
        user_id = interaction.user.id

        ok, reason = await create_bet(
            user_id=user_id,
            match_id=match_id(
                self.match
            ),
            league=self.match.get(
                "competition",
                {}
            ).get(
                "code",
                "PL"
            ),
            home_team=match_home(
                self.match
            ),
            away_team=match_away(
                self.match
            ),
            choice=choice,
            stake=self.amount
        )

        if not ok:

            if reason == "EXISTS":
                text = (
                    "❌ M đã cược trận này rồi."
                )
            else:
                text = (
                    "❌ Không đủ xu."
                )

            await interaction.response.send_message(
                text,
                ephemeral=True
            )

            return

        balance = await get_balance(
            user_id
        )

        await interaction.response.send_message(
            (
                "✅ **ĐẶT CƯỢC THÀNH CÔNG**\n\n"
                f"⚽ **{match_home(self.match)} "
                f"vs "
                f"{match_away(self.match)}**\n"
                f"🎯 Cửa: **{choice_text(choice, match_home(self.match), match_away(self.match))}**\n"
                f"💰 Cược: **{format_money(self.amount)} xu**\n"
                f"💵 Còn lại: **{format_money(balance)} xu**"
            ),
            ephemeral=True
        )

    @discord.ui.button(
        label="🏠 Chủ nhà",
        style=discord.ButtonStyle.primary
    )
    async def home(
        self,
        interaction,
        button
    ):
        await self.place(
            interaction,
            "HOME"
        )

    @discord.ui.button(
        label="🤝 Hòa",
        style=discord.ButtonStyle.secondary
    )
    async def draw(
        self,
        interaction,
        button
    ):
        await self.place(
            interaction,
            "DRAW"
        )

    @discord.ui.button(
        label="✈️ Đội khách",
        style=discord.ButtonStyle.success
    )
    async def away(
        self,
        interaction,
        button
    ):
        await self.place(
            interaction,
            "AWAY"
        )


# =========================================================
# LEAGUE HOME
# =========================================================

async def send_league_matches(
    interaction,
    league_code,
    mode
):
    league = LEAGUES[
        league_code
    ]

    await interaction.response.defer(
        ephemeral=True
    )

    if league_code not in FREE_COMPETITIONS:

        await interaction.followup.send(
            (
                f"{league['emoji']} **{league['name']}**\n\n"
                "⚠️ Gói Free của football-data.org "
                "không cung cấp dữ liệu giải này.\n"
                "Nút vẫn được giữ lại để sau này có thể "
                "thay API/nguồn dữ liệu."
            ),
            ephemeral=True
        )

        return

    try:
        matches = await football.next_league(
            league_code,
            25
        )

        matches = [
            x
            for x in matches
            if is_upcoming(x)
        ]

        if not matches:

            await interaction.followup.send(
                (
                    f"❌ Chưa có trận sắp tới "
                    f"trong **{league['name']}**."
                ),
                ephemeral=True
            )

            return

        embed = base_embed(
            f"{league['emoji']} {league['name']}",
            (
                "Chọn trận bên dưới.\n"
                f"📅 Có **{len(matches)}** trận."
            ),
            COLOR_BLUE
        )

        await interaction.followup.send(
            embed=embed,
            view=MatchView(
                matches,
                mode
            ),
            ephemeral=True
        )

    except Exception as exc:

        await interaction.followup.send(
            (
                "❌ Lỗi API:\n"
                f"`{str(exc)[:500]}`"
            ),
            ephemeral=True
        )


class LeagueSelect(
    discord.ui.Select
):

    def __init__(
        self,
        mode
    ):
        self.mode = mode

        options = []

        for code, info in LEAGUES.items():

            options.append(
                discord.SelectOption(
                    label=info["name"],
                    value=code,
                    emoji=info["emoji"]
                )
            )

        super().__init__(
            placeholder=(
                "🏆 Chọn giải..."
            ),
            options=options
        )

    async def callback(
        self,
        interaction
    ):
        await send_league_matches(
            interaction,
            self.values[0],
            self.mode
        )


class MainFootballView(
    discord.ui.View
):

    def __init__(
        self,
        mode
    ):
        super().__init__(
            timeout=300
        )

        self.add_item(
            LeagueSelect(
                mode
            )
        )

        self.mode = mode

    @discord.ui.button(
        label="🔵 Các trận Man City sắp tới",
        style=discord.ButtonStyle.primary,
        row=1
    )
    async def man_city(
        self,
        interaction,
        button
    ):
        await interaction.response.defer(
            ephemeral=True
        )

        try:
            matches = await football.next_team(
                MANCHESTER_CITY_TEAM_ID,
                50
            )

            matches = [
                x
                for x in matches
                if is_upcoming(x)
            ]

            if not matches:

                await interaction.followup.send(
                    "❌ Không có trận Man City sắp tới.",
                    ephemeral=True
                )

                return

            embed = base_embed(
                "🔵 MAN CITY — CÁC TRẬN SẮP TỚI",
                (
                    "Tất cả trận tiếp theo của Man City, "
                    "không phân biệt giải.\n\n"
                    f"📅 **{len(matches)}** trận."
                ),
                COLOR_BLUE
            )

            await interaction.followup.send(
                embed=embed,
                view=MatchView(
                    matches,
                    self.mode
                ),
                ephemeral=True
            )

        except Exception as exc:

            await interaction.followup.send(
                (
                    "❌ Lỗi API:\n"
                    f"`{str(exc)[:500]}`"
                ),
                ephemeral=True
            )


# =========================================================
# PREDICTION DISPLAY
# =========================================================

async def show_prediction(
    interaction,
    match,
    prediction
):
    home = match_home(match)
    away = match_away(match)

    hp = prediction[
        "home_probability"
    ] * 100

    dp = prediction[
        "draw_probability"
    ] * 100

    ap = prediction[
        "away_probability"
    ] * 100

    eh, ea = prediction[
        "exact"
    ]

    top_lines = []

    for probability, h, a in prediction[
        "top_scores"
    ]:
        top_lines.append(
            (
                f"• **{h} - {a}** "
                f"({probability * 100:.1f}%)"
            )
        )

    if hp >= dp and hp >= ap:
        conclusion = (
            f"📌 Mô hình nghiêng về **{home}**."
        )

    elif ap >= hp and ap >= dp:
        conclusion = (
            f"📌 Mô hình nghiêng về **{away}**."
        )

    else:
        conclusion = (
            "📌 Mô hình nghiêng về **Hòa**."
        )

    embed = base_embed(
        "🔮 SOI TRẬN",
        (
            f"⚽ **{home}** vs **{away}**\n"
            f"🕐 {display_time(match)}\n\n"
            "📊 **Xác suất ước tính**\n"
            f"🏠 {home}: **{hp:.1f}%**\n"
            f"🤝 Hòa: **{dp:.1f}%**\n"
            f"✈️ {away}: **{ap:.1f}%**\n\n"
            f"🎯 Tỷ số ước tính: "
            f"**{eh} - {ea}**\n\n"
            "🔥 **Các tỷ số nổi bật**\n"
            + "\n".join(top_lines)
            + "\n\n"
            + conclusion
            + "\n\n"
            "⚠️ Đây là ước tính của bot từ phong độ gần đây, "
            "không phải tỷ lệ cược chính thức của nhà cái."
        ),
        COLOR_ORANGE
    )

    await interaction.followup.send(
        embed=embed,
        ephemeral=True
    )


# =========================================================
# /CUOC
# =========================================================

@bot.tree.command(
    name="cuoc",
    description="Mở bảng cược bóng đá"
)
async def cuoc(
    interaction: discord.Interaction
):
    await ensure_user(
        interaction.user.id
    )

    embed = base_embed(
        "⚽ BẢNG CƯỢC BÓNG ĐÁ",
        (
            "🏆 Chọn một trong 11 giải đấu.\n"
            "🔵 Hoặc chọn các trận Man City.\n\n"
            "💰 Tất cả tiền cược đều là xu ảo."
        ),
        COLOR_BLUE
    )

    await interaction.response.send_message(
        embed=embed,
        view=MainFootballView(
            "bet"
        ),
        ephemeral=True
    )


# =========================================================
# /SOI
# =========================================================

@bot.tree.command(
    name="soi",
    description="Soi và dự đoán bóng đá"
)
async def soi(
    interaction: discord.Interaction
):
    embed = base_embed(
        "🔮 SOI BÓNG ĐÁ",
        (
            "🏆 Chọn giải để xem trận.\n"
            "🔵 Hoặc xem tất cả trận Man City.\n\n"
            "📊 Bot ước tính xác suất và tỷ số "
            "dựa trên dữ liệu trận gần đây."
        ),
        COLOR_ORANGE
    )

    await interaction.response.send_message(
        embed=embed,
        view=MainFootballView(
            "soi"
        ),
        ephemeral=True
    )


# =========================================================
# /VI
# =========================================================

@bot.tree.command(
    name="vi",
    description="Xem ví và VIP"
)
async def vi(
    interaction: discord.Interaction
):
    row = await get_wallet(
        interaction.user.id
    )

    balance = int(
        row["balance"]
    )

    vip = get_vip(
        balance
    )

    next_level, next_money = get_next_vip(
        vip
    )

    if next_level is None:

        next_text = "👑 VIP 10 MAX"

    else:

        missing = max(
            0,
            next_money - balance
        )

        next_text = (
            f"🎯 VIP {next_level}\n"
            f"💰 Cần: **{format_money(next_money)} xu**\n"
            f"📉 Còn thiếu: **{format_money(missing)} xu**"
        )

    embed = base_embed(
        f"{vip_icon(vip)} {vip_name(vip)}",
        "",
        COLOR_GOLD
    )

    embed.add_field(
        name="💰 Số dư",
        value=(
            f"**{format_money(balance)} xu**"
        ),
        inline=False
    )

    embed.add_field(
        name="📈 Cấp tiếp theo",
        value=next_text,
        inline=False
    )

    embed.add_field(
        name="🎁 Xu mặc định",
        value=(
            f"Người mới có "
            f"**{format_money(STARTING_BALANCE)} xu**."
        ),
        inline=False
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# =========================================================
# /KHOINGHIEP
# =========================================================

@bot.tree.command(
    name="khoinghiep",
    description="Nhận 1.000 xu khởi nghiệp một lần"
)
async def khoinghiep(
    interaction: discord.Interaction
):
    ok = await claim_starter(
        interaction.user.id
    )

    if not ok:

        await interaction.response.send_message(
            "❌ M đã nhận 1.000 xu khởi nghiệp rồi.",
            ephemeral=True
        )

        return

    balance = await get_balance(
        interaction.user.id
    )

    await interaction.response.send_message(
        (
            "🎉 **NHẬN XU THÀNH CÔNG**\n\n"
            f"🎁 +{format_money(STARTER_BONUS)} xu\n"
            f"💰 Ví: **{format_money(balance)} xu**"
        ),
        ephemeral=True
    )


# =========================================================
# /COMAT
# =========================================================

@bot.tree.command(
    name="comat",
    description="Nhận 50.000 xu mỗi ngày"
)
async def comat(
    interaction: discord.Interaction
):
    ok = await claim_daily(
        interaction.user.id
    )

    if not ok:

        await interaction.response.send_message(
            (
                "❌ Hôm nay m đã nhận 50.000 xu rồi.\n"
                "⏰ Mai quay lại nhận tiếp."
            ),
            ephemeral=True
        )

        return

    balance = await get_balance(
        interaction.user.id
    )

    await interaction.response.send_message(
        (
            "🎁 **NHẬN XU HẰNG NGÀY**\n\n"
            f"💰 +{format_money(DAILY_BONUS)} xu\n"
            f"💳 Ví: **{format_money(balance)} xu**"
        ),
        ephemeral=True
    )


# =========================================================
# /CADO
# =========================================================

@bot.tree.command(
    name="cado",
    description="Xem thông tin ví cá cược"
)
async def cado(
    interaction: discord.Interaction
):
    row = await get_wallet(
        interaction.user.id
    )

    balance = int(
        row["balance"]
    )

    vip = get_vip(
        balance
    )

    embed = base_embed(
        f"💰 VÍ — {interaction.user.display_name}",
        "",
        COLOR_GREEN
    )

    embed.add_field(
        name="💵 Số dư",
        value=(
            f"**{format_money(balance)} xu**"
        ),
        inline=False
    )

    embed.add_field(
        name="🏆 VIP",
        value=(
            f"{vip_icon(vip)} "
            f"**{vip_name(vip)}**"
        ),
        inline=True
    )

    embed.add_field(
        name="🆔 User ID",
        value=f"`{interaction.user.id}`",
        inline=True
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# =========================================================
# ADMIN CHECK
# =========================================================

def is_admin_user(
    user
):
    if user.guild_permissions.administrator:
        return True

    if ADMIN_USER_IDS_RAW:

        allowed = {
            x.strip()
            for x in ADMIN_USER_IDS_RAW.split(",")
            if x.strip()
        }

        return str(user.id) in allowed

    return False


# =========================================================
# /ADMIN
# =========================================================

@bot.tree.command(
    name="admin",
    description="Admin cộng hoặc trừ xu"
)
@app_commands.describe(
    action="add hoặc remove",
    user="Người cần chỉnh xu",
    amount="Số xu",
    password="Mật khẩu admin",
    reason="Lý do"
)
async def admin(
    interaction: discord.Interaction,
    action: str,
    user: discord.Member,
    amount: int,
    password: str,
    reason: str = "Không ghi"
):
    if not is_admin_user(
        interaction.user
    ):

        await interaction.response.send_message(
            "❌ Không có quyền.",
            ephemeral=True
        )

        return

    if password != ADMIN_PASSWORD:

        await interaction.response.send_message(
            "❌ Sai mật khẩu admin.",
            ephemeral=True
        )

        return

    action = action.lower().strip()

    if action not in {
        "add",
        "remove",
        "cong",
        "tru"
    }:

        await interaction.response.send_message(
            (
                "❌ Action phải là "
                "`add` hoặc `remove`."
            ),
            ephemeral=True
        )

        return

    if amount <= 0:

        await interaction.response.send_message(
            "❌ Amount phải lớn hơn 0.",
            ephemeral=True
        )

        return

    if action in {
        "add",
        "cong"
    }:

        ok, old, old_vip, new_vip = (
            await change_balance(
                user.id,
                amount
            )
        )

        action_text = "Cộng"

    else:

        ok, old, old_vip, new_vip = (
            await change_balance(
                user.id,
                -amount
            )
        )

        action_text = "Trừ"

    if not ok:

        await interaction.response.send_message(
            "❌ Số dư không đủ để trừ.",
            ephemeral=True
        )

        return

    if new_vip > old_vip:
        await announce_vip(
            user.id,
            new_vip
        )

    balance = await get_balance(
        user.id
    )

    await interaction.response.send_message(
        (
            f"✅ **{action_text} "
            f"{format_money(amount)} xu**\n"
            f"👤 {user.mention}\n"
            f"💰 Số dư: **{format_money(balance)} xu**\n"
            f"📝 Lý do: **{reason}**"
        ),
        ephemeral=True
    )


# =========================================================
# /KETTOAN
# =========================================================

@bot.tree.command(
    name="kettoan",
    description="Xem các cược đang chờ kết toán"
)
async def kettoan(
    interaction: discord.Interaction
):
    if not interaction.user.guild_permissions.manage_guild:

        await interaction.response.send_message(
            "❌ Chỉ quản lý server mới được dùng.",
            ephemeral=True
        )

        return

    bets = await get_open_bets()

    if not bets:

        await interaction.response.send_message(
            "ℹ️ Không có cược đang chờ.",
            ephemeral=True
        )

        return

    counts = defaultdict(int)

    for bet in bets:
        counts[
            int(bet["match_id"])
        ] += 1

    lines = []

    for mid, count in list(
        counts.items()
    )[:20]:

        lines.append(
            f"⚽ Match ID `{mid}` — **{count} cược**"
        )

    await interaction.response.send_message(
        (
            "📋 **CƯỢC ĐANG CHỜ**\n\n"
            + "\n".join(lines)
            + "\n\n"
            "ℹ️ Bot sẽ tự động kết toán khi "
            "football-data.org trả trạng thái FINISHED."
        ),
        ephemeral=True
    )


# =========================================================
# /PING
# =========================================================

@bot.tree.command(
    name="ping",
    description="Kiểm tra ping bot"
)
async def ping(
    interaction: discord.Interaction
):
    latency = round(
        bot.latency * 1000
    )

    await interaction.response.send_message(
        f"🏓 Pong! `{latency}ms`",
        ephemeral=True
    )


# =========================================================
# /APIQUOTA
# =========================================================

@bot.tree.command(
    name="apiquota",
    description="Kiểm tra API football-data"
)
async def apiquota(
    interaction: discord.Interaction
):
    if not interaction.user.guild_permissions.manage_guild:

        await interaction.response.send_message(
            "❌ Chỉ quản lý server mới dùng được.",
            ephemeral=True
        )

        return

    embed = base_embed(
        "📡 FOOTBALL-DATA.ORG",
        (
            "⚽ Bot đang dùng API v4.\n\n"
            "📌 Gói Free có giới hạn request/phút.\n"
            "📌 Bot có cache để giảm số request.\n"
            "📌 FA Cup và Carabao Cup không nằm "
            "trong bộ giải Free hiện tại."
        ),
        COLOR_BLUE
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# =========================================================
# RAILWAY HEALTH
# =========================================================

health_runner = None


async def health(
    request
):
    return web.json_response({
        "status": "ok",
        "bot": (
            str(bot.user)
            if bot.user
            else None
        ),
        "time":
            datetime.now(
                TZ
            ).isoformat()
    })


async def start_health_server():
    global health_runner

    if health_runner is not None:
        return

    app = web.Application()

    app.router.add_get(
        "/",
        health
    )

    app.router.add_get(
        "/health",
        health
    )

    health_runner = web.AppRunner(
        app
    )

    await health_runner.setup()

    port = int(
        os.getenv(
            "PORT",
            "8080"
        )
    )

    site = web.TCPSite(
        health_runner,
        "0.0.0.0",
        port
    )

    await site.start()

    print(
        f"[HEALTH] Running on {port}"
    )


# =========================================================
# READY
# =========================================================

sync_done = False


@bot.event
async def on_ready():
    global sync_done
    global monitor_task

    init_db()

    await football.start()

    await start_health_server()

    if not sync_done:

        try:

            if GUILD is not None:

                synced = await bot.tree.sync(
                    guild=GUILD
                )

            else:

                synced = await bot.tree.sync()

            print(
                f"[DISCORD] Synced "
                f"{len(synced)} commands."
            )

            sync_done = True

        except Exception as exc:

            print(
                f"[DISCORD] Sync error: {exc}"
            )

    if (
        monitor_task is None
        or monitor_task.done()
    ):

        monitor_task = asyncio.create_task(
            monitor_bets()
        )

    print(
        f"[BOT] Logged in as {bot.user}"
    )


# =========================================================
# SHUTDOWN
# =========================================================

@bot.event
async def on_disconnect():
    print(
        "[BOT] Disconnected."
    )


# =========================================================
# MAIN
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

    bot.run(
        DISCORD_TOKEN
    )

import os
import asyncio
import sqlite3
import math
import time
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import aiohttp
from aiohttp import web
import discord
from discord import app_commands
from discord.ext import commands


# ============================================================
# CONFIG
# ============================================================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()

GUILD_ID = os.getenv("GUILD_ID", "").strip()

FOOTBALL_DATA_API_KEY = (
    os.getenv("FOOTBALL_DATA_API_KEY", "").strip()
    or os.getenv("FOOTBALL_DATA_TOKEN", "").strip()
    or os.getenv("FOOTBALL_DATA_KEY", "").strip()
)

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "provip👑")

ADMIN_USER_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_USER_IDS", "").split(",")
    if x.strip().isdigit()
}

API_BASE = "https://api.football-data.org/v4"

# Giữ UTC+7 theo hệ thống bot hiện tại
DISPLAY_TIMEZONE = timezone(timedelta(hours=7))

STARTING_BALANCE = 100_000
STARTER_BONUS = 1_000
DAILY_BONUS = 50_000
BET_PAYOUT = 2

# Manchester City trên football-data.org
MANCHESTER_CITY_TEAM_ID = 65

DB_PATH = (
    "/data/football_bot.db"
    if os.path.isdir("/data")
    else "football_bot.db"
)

MATCH_CHECK_SECONDS = 60
PRE_MATCH_NOTICE_MINUTES = 30

API_CACHE_SECONDS = 60
RECENT_FORM_CACHE_SECONDS = 300

# Free football-data.org có giới hạn request/phút.
# Chặn request liên tiếp để giảm nguy cơ 429.
API_MIN_INTERVAL = 6.2


# ============================================================
# LEAGUES
# ============================================================

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


# football-data.org Free hiện hỗ trợ các giải này
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


# ============================================================
# VIP
# ============================================================

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
    (0, 0),
]


# ============================================================
# DISCORD
# ============================================================

intents = discord.Intents.default()

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
)


# ============================================================
# GLOBAL STATE
# ============================================================

db_lock = asyncio.Lock()
api_lock = asyncio.Lock()

api_cache = {}
recent_form_cache = {}

api_last_call = 0.0

http_session = None
health_runner = None

monitor_task = None

synced_once = False

# Tránh spam thông báo khi monitor chạy lại
notice_cache = set()


# ============================================================
# BASIC HELPERS
# ============================================================

def vip_for_balance(balance: int) -> int:
    for level, minimum in VIP_LEVELS:
        if balance >= minimum:
            return level

    return 0


def vip_name(level: int) -> str:
    if level > 0:
        return f"VIP {level}"

    return "Thường"


def next_vip_info(balance: int):
    current = vip_for_balance(balance)

    for level, minimum in reversed(VIP_LEVELS):
        if level > current and minimum > balance:
            return (
                level,
                minimum,
                minimum - balance,
            )

    return None


def money(value: int) -> str:
    return f"{int(value):,}".replace(",", ".")


def parse_api_date(value: str):
    if not value:
        return None

    try:
        dt = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt

    except ValueError:
        return None


def display_time(value: str) -> str:
    dt = parse_api_date(value)

    if not dt:
        return "Không rõ giờ"

    return dt.astimezone(
        DISPLAY_TIMEZONE
    ).strftime("%d/%m/%Y %H:%M")


def result_for_match(match: dict):
    score = match.get("score", {}).get("fullTime", {})

    home = score.get("home")
    away = score.get("away")

    if home is None or away is None:
        return None

    if home > away:
        return "HOME"

    if home < away:
        return "AWAY"

    return "DRAW"


def score_text(match: dict) -> str:
    score = match.get("score", {}).get("fullTime", {})

    home = score.get("home")
    away = score.get("away")

    if home is None or away is None:
        return "Chưa có tỷ số"

    return f"{home} - {away}"


# ============================================================
# DATABASE
# ============================================================

async def db_execute(
    sql,
    params=(),
    fetchone=False,
    fetchall=False,
    commit=True,
):
    async with db_lock:

        def run():
            conn = sqlite3.connect(DB_PATH)

            conn.row_factory = sqlite3.Row

            try:
                cursor = conn.execute(
                    sql,
                    params,
                )

                if commit:
                    conn.commit()

                if fetchone:
                    return cursor.fetchone()

                if fetchall:
                    return cursor.fetchall()

                return cursor.rowcount

            finally:
                conn.close()

        return await asyncio.to_thread(run)


async def init_db():

    async with db_lock:

        def run():

            conn = sqlite3.connect(DB_PATH)

            try:

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
                        user_id INTEGER PRIMARY KEY,
                        last_claim TEXT NOT NULL
                    )
                    """
                )

                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS bets (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        guild_id INTEGER,
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

                columns = {
                    row[1]
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

                if "guild_id" not in columns:
                    conn.execute(
                        """
                        ALTER TABLE bets
                        ADD COLUMN guild_id INTEGER
                        """
                    )

                conn.commit()

            finally:
                conn.close()

        await asyncio.to_thread(run)


# ============================================================
# WALLET
# ============================================================

async def ensure_user(user_id: int):

    row = await db_execute(
        """
        SELECT user_id, balance, vip_level
        FROM wallets
        WHERE user_id=?
        """,
        (user_id,),
        fetchone=True,
    )

    if row:
        return dict(row)

    await db_execute(
        """
        INSERT OR IGNORE INTO wallets(
            user_id,
            balance,
            starter_claimed,
            vip_level
        )
        VALUES(?,?,0,?)
        """,
        (
            user_id,
            STARTING_BALANCE,
            vip_for_balance(STARTING_BALANCE),
        ),
    )

    row = await db_execute(
        """
        SELECT user_id, balance, vip_level
        FROM wallets
        WHERE user_id=?
        """,
        (user_id,),
        fetchone=True,
    )

    return dict(row)


async def get_wallet(user_id: int):
    return await ensure_user(user_id)


async def add_balance(
    user_id: int,
    amount: int,
):

    await ensure_user(user_id)

    async with db_lock:

        def run():

            conn = sqlite3.connect(DB_PATH)

            conn.row_factory = sqlite3.Row

            try:

                old = conn.execute(
                    """
                    SELECT balance, vip_level
                    FROM wallets
                    WHERE user_id=?
                    """,
                    (user_id,),
                ).fetchone()

                new_balance = old["balance"] + amount

                new_vip = vip_for_balance(
                    new_balance
                )

                conn.execute(
                    """
                    UPDATE wallets
                    SET balance=?, vip_level=?
                    WHERE user_id=?
                    """,
                    (
                        new_balance,
                        new_vip,
                        user_id,
                    ),
                )

                conn.commit()

                return (
                    old["balance"],
                    new_balance,
                    old["vip_level"],
                    new_vip,
                )

            finally:
                conn.close()

        return await asyncio.to_thread(run)


# ============================================================
# STARTER BONUS
# ============================================================

async def claim_starter(user_id: int):

    await ensure_user(user_id)

    async with db_lock:

        def run():

            conn = sqlite3.connect(DB_PATH)

            conn.row_factory = sqlite3.Row

            try:

                row = conn.execute(
                    """
                    SELECT balance,
                           starter_claimed,
                           vip_level
                    FROM wallets
                    WHERE user_id=?
                    """,
                    (user_id,),
                ).fetchone()

                if row["starter_claimed"]:
                    return (
                        False,
                        row["balance"],
                        row["vip_level"],
                        row["vip_level"],
                    )

                balance = (
                    row["balance"]
                    + STARTER_BONUS
                )

                vip = vip_for_balance(
                    balance
                )

                conn.execute(
                    """
                    UPDATE wallets
                    SET balance=?,
                        starter_claimed=1,
                        vip_level=?
                    WHERE user_id=?
                    """,
                    (
                        balance,
                        vip,
                        user_id,
                    ),
                )

                conn.commit()

                return (
                    True,
                    balance,
                    row["vip_level"],
                    vip,
                )

            finally:
                conn.close()

        return await asyncio.to_thread(run)


# ============================================================
# DAILY BONUS
# ============================================================

async def claim_daily(user_id: int):

    await ensure_user(user_id)

    today = datetime.now(
        timezone.utc
    ).date().isoformat()

    async with db_lock:

        def run():

            conn = sqlite3.connect(DB_PATH)

            conn.row_factory = sqlite3.Row

            try:

                claim = conn.execute(
                    """
                    SELECT last_claim
                    FROM daily_claims
                    WHERE user_id=?
                    """,
                    (user_id,),
                ).fetchone()

                wallet = conn.execute(
                    """
                    SELECT balance, vip_level
                    FROM wallets
                    WHERE user_id=?
                    """,
                    (user_id,),
                ).fetchone()

                if (
                    claim
                    and claim["last_claim"] == today
                ):
                    return (
                        False,
                        wallet["balance"],
                        wallet["vip_level"],
                        wallet["vip_level"],
                    )

                balance = (
                    wallet["balance"]
                    + DAILY_BONUS
                )

                vip = vip_for_balance(
                    balance
                )

                conn.execute(
                    """
                    INSERT INTO daily_claims(
                        user_id,
                        last_claim
                    )
                    VALUES(?,?)
                    ON CONFLICT(user_id)
                    DO UPDATE SET
                        last_claim=excluded.last_claim
                    """,
                    (
                        user_id,
                        today,
                    ),
                )

                conn.execute(
                    """
                    UPDATE wallets
                    SET balance=?,
                        vip_level=?
                    WHERE user_id=?
                    """,
                    (
                        balance,
                        vip,
                        user_id,
                    ),
                )

                conn.commit()

                return (
                    True,
                    balance,
                    wallet["vip_level"],
                    vip,
                )

            finally:
                conn.close()

        return await asyncio.to_thread(run)


# ============================================================
# BET
# ============================================================

async def create_bet(
    user_id,
    guild_id,
    match,
    league,
    choice,
    stake,
):

    await ensure_user(user_id)

    async with db_lock:

        def run():

            conn = sqlite3.connect(DB_PATH)

            conn.row_factory = sqlite3.Row

            try:

                wallet = conn.execute(
                    """
                    SELECT balance, vip_level
                    FROM wallets
                    WHERE user_id=?
                    """,
                    (user_id,),
                ).fetchone()

                if (
                    stake <= 0
                    or stake > wallet["balance"]
                ):
                    return None

                new_balance = (
                    wallet["balance"]
                    - stake
                )

                new_vip = vip_for_balance(
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
                        user_id,
                    ),
                )

                conn.execute(
                    """
                    INSERT INTO bets(
                        user_id,
                        guild_id,
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
                    VALUES(
                        ?,?,?,?,?,?,?,?,?,?,?
                    )
                    """,
                    (
                        user_id,
                        guild_id or 0,
                        int(match["id"]),
                        league,
                        match["homeTeam"]["name"],
                        match["awayTeam"]["name"],
                        choice,
                        stake,
                        "OPEN",
                        0,
                        datetime.now(
                            timezone.utc
                        ).isoformat(),
                    ),
                )

                conn.commit()

                return {
                    "balance": new_balance,
                    "old_vip": wallet["vip_level"],
                    "new_vip": new_vip,
                }

            finally:
                conn.close()

        return await asyncio.to_thread(run)


async def open_bets_for_match(match_id: int):

    rows = await db_execute(
        """
        SELECT *
        FROM bets
        WHERE match_id=?
          AND status='OPEN'
        ORDER BY user_id, id
        """,
        (match_id,),
        fetchall=True,
    )

    return [
        dict(row)
        for row in rows
    ]


async def open_match_ids():

    rows = await db_execute(
        """
        SELECT DISTINCT match_id
        FROM bets
        WHERE status='OPEN'
        """,
        fetchall=True,
    )

    return [
        int(row["match_id"])
        for row in rows
    ]


# ============================================================
# SETTLEMENT
# ============================================================

async def settle_match_bets(
    match_id: int,
    result: str,
):

    async with db_lock:

        def run():

            conn = sqlite3.connect(DB_PATH)

            conn.row_factory = sqlite3.Row

            try:

                rows = conn.execute(
                    """
                    SELECT *
                    FROM bets
                    WHERE match_id=?
                      AND status='OPEN'
                    ORDER BY id
                    """,
                    (match_id,),
                ).fetchall()

                if not rows:
                    return []

                user_old = {}

                for row in rows:

                    uid = int(
                        row["user_id"]
                    )

                    if uid not in user_old:

                        wallet = conn.execute(
                            """
                            SELECT balance,
                                   vip_level
                            FROM wallets
                            WHERE user_id=?
                            """,
                            (uid,),
                        ).fetchone()

                        user_old[uid] = (
                            wallet["balance"],
                            wallet["vip_level"],
                        )

                now = datetime.now(
                    timezone.utc
                ).isoformat()

                for row in rows:

                    won = (
                        row["choice"]
                        == result
                    )

                    payout = (
                        int(row["stake"])
                        * BET_PAYOUT
                        if won
                        else 0
                    )

                    status = (
                        "WON"
                        if won
                        else "LOST"
                    )

                    if payout:

                        conn.execute(
                            """
                            UPDATE wallets
                            SET balance=balance+?
                            WHERE user_id=?
                            """,
                            (
                                payout,
                                row["user_id"],
                            ),
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
                            status,
                            payout,
                            now,
                            row["id"],
                        ),
                    )

                result_users = []

                for (
                    uid,
                    (
                        old_balance,
                        old_vip,
                    ),
                ) in user_old.items():

                    wallet = conn.execute(
                        """
                        SELECT balance,
                               vip_level
                        FROM wallets
                        WHERE user_id=?
                        """,
                        (uid,),
                    ).fetchone()

                    final_vip = vip_for_balance(
                        wallet["balance"]
                    )

                    conn.execute(
                        """
                        UPDATE wallets
                        SET vip_level=?
                        WHERE user_id=?
                        """,
                        (
                            final_vip,
                            uid,
                        ),
                    )

                    result_users.append(
                        {
                            "user_id": uid,
                            "old_vip": old_vip,
                            "new_vip": final_vip,
                            "balance": int(
                                wallet["balance"]
                            ),
                        }
                    )

                settled = []

                for row in rows:

                    won = (
                        row["choice"]
                        == result
                    )

                    payout = (
                        int(row["stake"])
                        * BET_PAYOUT
                        if won
                        else 0
                    )

                    settled.append(
                        {
                            **dict(row),
                            "won": won,
                            "payout": payout,
                            "status": (
                                "WON"
                                if won
                                else "LOST"
                            ),
                            "guild_id": int(
                                row["guild_id"]
                                or 0
                            ),
                        }
                    )

                conn.commit()

                user_map = {
                    x["user_id"]: x
                    for x in result_users
                }

                for item in settled:
                    item.update(
                        user_map[
                            item["user_id"]
                        ]
                    )

                return settled

            finally:
                conn.close()

        return await asyncio.to_thread(run)


# ============================================================
# FOOTBALL DATA API
# ============================================================

async def api_get(
    path,
    params=None,
    cache_seconds=API_CACHE_SECONDS,
):

    global api_last_call
    global http_session

    if not FOOTBALL_DATA_API_KEY:
        raise RuntimeError(
            "Thiếu FOOTBALL_DATA_API_KEY trên Railway."
        )

    params = params or {}

    cache_key = (
        path,
        tuple(
            sorted(params.items())
        ),
    )

    cached = api_cache.get(
        cache_key
    )

    if (
        cached
        and time.monotonic()
        - cached[0]
        < cache_seconds
    ):
        return cached[1]

    async with api_lock:

        cached = api_cache.get(
            cache_key
        )

        if (
            cached
            and time.monotonic()
            - cached[0]
            < cache_seconds
        ):
            return cached[1]

        wait = (
            API_MIN_INTERVAL
            - (
                time.monotonic()
                - api_last_call
            )
        )

        if wait > 0:
            await asyncio.sleep(wait)

        if (
            http_session is None
            or http_session.closed
        ):
            http_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(
                    total=20
                )
            )

        headers = {
            "X-Auth-Token":
                FOOTBALL_DATA_API_KEY
        }

        async with http_session.get(
            f"{API_BASE}{path}",
            params=params,
            headers=headers,
        ) as response:

            text = await response.text()

            api_last_call = (
                time.monotonic()
            )

            if response.status != 200:

                try:
                    data = await response.json(
                        content_type=None
                    )

                    message = data.get(
                        "message",
                        text[:300],
                    )

                except Exception:
                    message = text[:300]

                raise RuntimeError(
                    f"Football API "
                    f"{response.status}: "
                    f"{message}"
                )

            data = await response.json(
                content_type=None
            )

            api_cache[cache_key] = (
                time.monotonic(),
                data,
            )

            return data


async def get_upcoming_league_matches(
    code,
):

    if code not in FREE_COMPETITIONS:
        return None

    today = datetime.now(
        timezone.utc
    ).date()

    end = (
        today
        + timedelta(days=45)
    )

    data = await api_get(
        f"/competitions/{code}/matches",
        {
            "dateFrom":
                today.isoformat(),
            "dateTo":
                end.isoformat(),
        },
    )

    matches = []

    for match in data.get(
        "matches",
        [],
    ):

        if match.get("status") in {
            "SCHEDULED",
            "TIMED",
        }:
            matches.append(match)

    matches.sort(
        key=lambda x:
            x.get(
                "utcDate",
                "",
            )
    )

    return matches


async def get_upcoming_mancity_matches():

    today = datetime.now(
        timezone.utc
    ).date()

    end = (
        today
        + timedelta(days=60)
    )

    data = await api_get(
        f"/teams/{MANCHESTER_CITY_TEAM_ID}/matches",
        {
            "dateFrom":
                today.isoformat(),
            "dateTo":
                end.isoformat(),
            "limit": 100,
        },
    )

    matches = [
        match
        for match in data.get(
            "matches",
            [],
        )
        if match.get("status") in {
            "SCHEDULED",
            "TIMED",
        }
    ]

    matches.sort(
        key=lambda x:
            x.get(
                "utcDate",
                "",
            )
    )

    return matches


async def get_match(match_id):

    return await api_get(
        f"/matches/{int(match_id)}"
    )


async def get_recent_team_matches(
    team_id,
):

    key = int(team_id)

    cached = recent_form_cache.get(
        key
    )

    if (
        cached
        and time.monotonic()
        - cached[0]
        < RECENT_FORM_CACHE_SECONDS
    ):
        return cached[1]

    today = datetime.now(
        timezone.utc
    ).date()

    start = (
        today
        - timedelta(days=120)
    )

    data = await api_get(
        f"/teams/{key}/matches",
        {
            "dateFrom":
                start.isoformat(),
            "dateTo":
                today.isoformat(),
            "status":
                "FINISHED",
            "limit": 20,
        },
        cache_seconds=
            RECENT_FORM_CACHE_SECONDS,
    )

    matches = data.get(
        "matches",
        [],
    )

    recent_form_cache[key] = (
        time.monotonic(),
        matches,
    )

    return matches


# ============================================================
# PREDICTION ENGINE
# ============================================================

async def team_form(team_id):

    matches = await get_recent_team_matches(
        team_id
    )

    gf = 0
    ga = 0
    points = 0
    games = 0

    for match in matches[:10]:

        home_id = match.get(
            "homeTeam",
            {},
        ).get("id")

        away_id = match.get(
            "awayTeam",
            {},
        ).get("id")

        score = match.get(
            "score",
            {},
        ).get(
            "fullTime",
            {},
        )

        home_score = score.get(
            "home"
        )

        away_score = score.get(
            "away"
        )

        if (
            home_score is None
            or away_score is None
        ):
            continue

        if home_id == team_id:

            gf += home_score
            ga += away_score

            if home_score > away_score:
                points += 3

            elif home_score == away_score:
                points += 1

        elif away_id == team_id:

            gf += away_score
            ga += home_score

            if away_score > home_score:
                points += 3

            elif away_score == home_score:
                points += 1

        else:
            continue

        games += 1

    if games == 0:
        return {
            "gf": 1.35,
            "ga": 1.10,
            "ppg": 1.40,
        }

    return {
        "gf": gf / games,
        "ga": ga / games,
        "ppg": points / games,
    }


def poisson_pmf(
    k,
    lam,
):

    return (
        math.exp(-lam)
        * (lam ** k)
        / math.factorial(k)
    )


def prediction_from_stats(
    home_stats,
    away_stats,
):

    home_attack = max(
        0.35,
        home_stats["gf"],
    )

    home_def = max(
        0.35,
        home_stats["ga"],
    )

    away_attack = max(
        0.35,
        away_stats["gf"],
    )

    away_def = max(
        0.35,
        away_stats["ga"],
    )

    home_lambda = (
        home_attack * 0.55
        + away_def * 0.45
    ) * 1.08

    away_lambda = (
        away_attack * 0.55
        + home_def * 0.45
    ) * 0.94

    home_lambda *= (
        1
        + max(
            -0.12,
            min(
                0.12,
                (
                    home_stats["ppg"]
                    - away_stats["ppg"]
                ) * 0.04,
            ),
        )
    )

    away_lambda *= (
        1
        + max(
            -0.12,
            min(
                0.12,
                (
                    away_stats["ppg"]
                    - home_stats["ppg"]
                ) * 0.04,
            ),
        )
    )

    home_lambda = max(
        0.15,
        min(
            4.5,
            home_lambda,
        ),
    )

    away_lambda = max(
        0.15,
        min(
            4.5,
            away_lambda,
        ),
    )

    scores = []

    home_win = 0.0
    draw = 0.0
    away_win = 0.0

    for home_goals in range(7):

        home_probability = poisson_pmf(
            home_goals,
            home_lambda,
        )

        for away_goals in range(7):

            probability = (
                home_probability
                * poisson_pmf(
                    away_goals,
                    away_lambda,
                )
            )

            scores.append(
                (
                    probability,
                    home_goals,
                    away_goals,
                )
            )

            if home_goals > away_goals:
                home_win += probability

            elif home_goals == away_goals:
                draw += probability

            else:
                away_win += probability

    total = (
        home_win
        + draw
        + away_win
    )

    home_win /= total
    draw /= total
    away_win /= total

    scores.sort(
        reverse=True
    )

    best = scores[0]

    return {
        "exact":
            f"{best[1]} - {best[2]}",
        "home_prob":
            home_win,
        "draw_prob":
            draw,
        "away_prob":
            away_win,
        "top_scores":
            scores[:3],
        "home_lambda":
            home_lambda,
        "away_lambda":
            away_lambda,
    }


def pct(value):

    return f"{value * 100:.1f}%"


async def make_prediction(match):

    home_id = match.get(
        "homeTeam",
        {},
    ).get("id")

    away_id = match.get(
        "awayTeam",
        {},
    ).get("id")

    if not home_id or not away_id:
        raise RuntimeError(
            "Không lấy được ID của 2 đội."
        )

    home_stats, away_stats = await asyncio.gather(
        team_form(home_id),
        team_form(away_id),
    )

    prediction = prediction_from_stats(
        home_stats,
        away_stats,
    )

    return (
        home_stats,
        away_stats,
        prediction,
    )


# ============================================================
# EMBEDS
# ============================================================

def match_embed(
    match,
    title_prefix="⚽ Trận đấu",
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
        "Không rõ giải",
    )

    status = match.get(
        "status",
        "UNKNOWN",
    )

    embed = discord.Embed(
        title=(
            f"{title_prefix}: "
            f"{home} vs {away}"
        ),
        color=discord.Color.blue(),
    )

    embed.add_field(
        name="🏆 Giải",
        value=competition,
        inline=False,
    )

    embed.add_field(
        name="🕐 Giờ",
        value=display_time(
            match.get("utcDate")
        ),
        inline=True,
    )

    embed.add_field(
        name="📌 Trạng thái",
        value=status,
        inline=True,
    )

    if status == "FINISHED":

        embed.add_field(
            name="⚽ Tỷ số",
            value=score_text(match),
            inline=True,
        )

    return embed


# ============================================================
# VIP ANNOUNCEMENT
# ============================================================

async def get_send_channel(guild):

    if guild is None:
        return None

    me = guild.me

    if me is None:
        return None

    channel = guild.system_channel

    if (
        channel
        and channel.permissions_for(
            me
        ).send_messages
    ):
        return channel

    for channel in guild.text_channels:

        if channel.permissions_for(
            me
        ).send_messages:
            return channel

    return None


async def announce_vip_upgrade(
    user_id,
    old_vip,
    new_vip,
    balance,
):

    if new_vip <= old_vip:
        return

    text = (
        "🎉 **VIP THĂNG HẠNG!**\n"
        f"<@{user_id}> đã lên "
        f"**{vip_name(new_vip)}** 👑\n"
        f"Số dư: **{money(balance)} xu**"
    )

    for guild in bot.guilds:

        channel = await get_send_channel(
            guild
        )

        if channel:

            try:
                await channel.send(
                    text
                )
            except Exception:
                pass


# ============================================================
# MATCH RESULT NOTIFICATION
# ============================================================

async def notify_match_finished(
    match,
    settled,
):

    if not settled:
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

    result = result_for_match(
        match
    )

    result_text = {
        "HOME":
            f"🏠 {home} thắng",
        "DRAW":
            "🤝 Hòa",
        "AWAY":
            f"✈️ {away} thắng",
    }.get(
        result,
        "Không xác định",
    )

    # Gom nhiều vé cùng user lại
    by_user = defaultdict(list)

    guild_ids = set()

    for item in settled:

        by_user[
            item["user_id"]
        ].append(item)

        if item.get("guild_id"):
            guild_ids.add(
                item["guild_id"]
            )

    # --------------------------------------------------------
    # DM MỖI USER 1 TIN
    # --------------------------------------------------------

    for user_id, items in by_user.items():

        total_stake = sum(
            int(x["stake"])
            for x in items
        )

        total_payout = sum(
            int(x["payout"])
            for x in items
        )

        wallet = await get_wallet(
            user_id
        )

        lines = []

        for item in items:

            choice_name = {
                "HOME":
                    f"🏠 {home}",
                "DRAW":
                    "🤝 Hòa",
                "AWAY":
                    f"✈️ {away}",
            }.get(
                item["choice"],
                item["choice"],
            )

            if item["won"]:

                lines.append(
                    f"✅ {choice_name} — "
                    f"cược **{money(item['stake'])}** "
                    f"→ nhận **{money(item['payout'])}**"
                )

            else:

                lines.append(
                    f"❌ {choice_name} — "
                    f"mất **{money(item['stake'])}**"
                )

        net = (
            total_payout
            - total_stake
        )

        embed = discord.Embed(
            title="🏁 KẾT QUẢ TRẬN ĐẤU",
            description=(
                f"**{home} "
                f"{score_text(match)} "
                f"{away}**\n"
                f"{result_text}"
            ),
            color=(
                discord.Color.green()
                if total_payout
                else discord.Color.red()
            ),
        )

        embed.add_field(
            name="🎫 Cược của bạn",
            value="\n".join(lines),
            inline=False,
        )

        embed.add_field(
            name="💰 Tổng tiền cược",
            value=money(total_stake),
            inline=True,
        )

        embed.add_field(
            name="💵 Tổng nhận",
            value=money(total_payout),
            inline=True,
        )

        embed.add_field(
            name="📈 Lãi/lỗ",
            value=(
                "+"
                if net >= 0
                else ""
            )
            + money(net),
            inline=True,
        )

        embed.add_field(
            name="💳 Số dư hiện tại",
            value=money(
                wallet["balance"]
            ),
            inline=True,
        )

        embed.add_field(
            name="👑 VIP",
            value=vip_name(
                wallet["vip_level"]
            ),
            inline=True,
        )

        try:

            user = (
                bot.get_user(user_id)
                or await bot.fetch_user(
                    user_id
                )
            )

            await user.send(
                embed=embed
            )

        except discord.Forbidden:

            print(
                f"ℹ️ User {user_id} "
                f"đã tắt DM."
            )

        except Exception as error:

            print(
                f"⚠️ DM {user_id} lỗi: "
                f"{error}"
            )

    # --------------------------------------------------------
    # SERVER RESULT
    # --------------------------------------------------------

    server_embed = discord.Embed(
        title="🏁 KẾT QUẢ TRẬN ĐẤU",
        description=(
            f"**{home} "
            f"{score_text(match)} "
            f"{away}**\n"
            f"{result_text}"
        ),
        color=discord.Color.green(),
    )

    server_embed.add_field(
        name="🎫 Cược",
        value=(
            f"Đã xử lý "
            f"**{len(settled)}** vé cược."
        ),
        inline=False,
    )

    for guild_id in guild_ids:

        guild = bot.get_guild(
            guild_id
        )

        if not guild:
            continue

        channel = await get_send_channel(
            guild
        )

        if channel:

            try:
                await channel.send(
                    embed=server_embed
                )
            except Exception as error:
                print(
                    "⚠️ Server result "
                    f"notification lỗi: {error}"
                )


# ============================================================
# PRE-MATCH / START NOTIFICATION
# ============================================================

async def notify_prematch(
    match,
    bets,
):

    guild_ids = {
        int(x["guild_id"])
        for x in bets
        if x.get("guild_id")
    }

    if not guild_ids:
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

    embed = discord.Embed(
        title="⏰ TRẬN SẮP BẮT ĐẦU",
        description=(
            f"**{home} vs {away}**"
        ),
        color=discord.Color.orange(),
    )

    embed.add_field(
        name="🕐 Giờ",
        value=display_time(
            match.get("utcDate")
        ),
        inline=True,
    )

    embed.add_field(
        name="🎫 Vé đang mở",
        value=str(len(bets)),
        inline=True,
    )

    for guild_id in guild_ids:

        guild = bot.get_guild(
            guild_id
        )

        if not guild:
            continue

        channel = await get_send_channel(
            guild
        )

        if channel:

            try:
                await channel.send(
                    embed=embed
                )
            except Exception:
                pass


async def notify_started(
    match,
    bets,
):

    guild_ids = {
        int(x["guild_id"])
        for x in bets
        if x.get("guild_id")
    }

    if not guild_ids:
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

    embed = discord.Embed(
        title="🔴 TRẬN ĐẤU ĐÃ BẮT ĐẦU",
        description=(
            f"**{home} vs {away}**"
        ),
        color=discord.Color.red(),
    )

    for guild_id in guild_ids:

        guild = bot.get_guild(
            guild_id
        )

        if not guild:
            continue

        channel = await get_send_channel(
            guild
        )

        if channel:

            try:
                await channel.send(
                    embed=embed
                )
            except Exception:
                pass


# ============================================================
# MATCH MONITOR
# ============================================================

async def monitor_loop():

    await bot.wait_until_ready()

    while not bot.is_closed():

        try:

            ids = await open_match_ids()

            now = datetime.now(
                timezone.utc
            )

            for match_id in ids:

                try:

                    bets = await open_bets_for_match(
                        match_id
                    )

                    if not bets:
                        continue

                    match = await get_match(
                        match_id
                    )

                    status = match.get(
                        "status",
                        "",
                    )

                    match_time = parse_api_date(
                        match.get(
                            "utcDate"
                        )
                    )

                    # ----------------------------------------
                    # 30 PHÚT TRƯỚC TRẬN
                    # ----------------------------------------

                    if match_time:

                        minutes = (
                            match_time
                            - now
                        ).total_seconds() / 60

                        key = (
                            "pre",
                            match_id,
                        )

                        if (
                            0
                            <= minutes
                            <= PRE_MATCH_NOTICE_MINUTES
                            and key
                            not in notice_cache
                        ):

                            await notify_prematch(
                                match,
                                bets,
                            )

                            notice_cache.add(
                                key
                            )

                    # ----------------------------------------
                    # TRẬN BẮT ĐẦU
                    # ----------------------------------------

                    if status in {
                        "IN_PLAY",
                        "PAUSED",
                        "LIVE",
                    }:

                        key = (
                            "start",
                            match_id,
                        )

                        if key not in notice_cache:

                            await notify_started(
                                match,
                                bets,
                            )

                            notice_cache.add(
                                key
                            )

                    # ----------------------------------------
                    # TRẬN KẾT THÚC
                    # ----------------------------------------

                    if status == "FINISHED":

                        key = (
                            "finish",
                            match_id,
                        )

                        if key not in notice_cache:

                            result = result_for_match(
                                match
                            )

                            if result:

                                settled = (
                                    await settle_match_bets(
                                        match_id,
                                        result,
                                    )
                                )

                                if settled:

                                    # DM kết quả
                                    await notify_match_finished(
                                        match,
                                        settled,
                                    )

                                    # VIP upgrade
                                    upgrades = {}

                                    for item in settled:

                                        if (
                                            item["new_vip"]
                                            > item["old_vip"]
                                        ):

                                            upgrades[
                                                item["user_id"]
                                            ] = (
                                                item["old_vip"],
                                                item["new_vip"],
                                                item["balance"],
                                            )

                                    for (
                                        uid,
                                        data,
                                    ) in upgrades.items():

                                        old_vip, new_vip, balance = data

                                        await announce_vip_upgrade(
                                            uid,
                                            old_vip,
                                            new_vip,
                                            balance,
                                        )

                            notice_cache.add(
                                key
                            )

                except Exception as error:

                    print(
                        f"⚠️ Monitor match "
                        f"{match_id}: "
                        f"{error}"
                    )

        except Exception as error:

            print(
                f"⚠️ Monitor loop: "
                f"{error}"
            )

        await asyncio.sleep(
            MATCH_CHECK_SECONDS
        )


# ============================================================
# DISCORD VIEWS
# ============================================================

class OwnerView(discord.ui.View):

    def __init__(
        self,
        owner_id,
        timeout=300,
    ):

        super().__init__(
            timeout=timeout
        )

        self.owner_id = owner_id

    async def interaction_check(
        self,
        interaction,
    ):

        if (
            interaction.user.id
            != self.owner_id
        ):

            await interaction.response.send_message(
                "❌ Menu này không phải của bạn.",
                ephemeral=True,
            )

            return False

        return True


# ============================================================
# LEAGUE SELECT
# ============================================================

class LeagueSelect(
    discord.ui.Select
):

    def __init__(
        self,
        owner_id,
        mode,
    ):

        self.owner_id = owner_id
        self.mode = mode

        options = [
            discord.SelectOption(
                label=data["name"],
                value=code,
                emoji=data["emoji"],
            )
            for code, data in LEAGUES.items()
        ]

        super().__init__(
            placeholder="Chọn giải đấu...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(
        self,
        interaction,
    ):

        code = self.values[0]

        await show_matches(
            interaction,
            self.owner_id,
            self.mode,
            code,
            0,
        )


# ============================================================
# MAIN FOOTBALL VIEW
# ============================================================

class FootballMainView(
    OwnerView
):

    def __init__(
        self,
        owner_id,
        mode,
    ):

        super().__init__(
            owner_id
        )

        self.add_item(
            LeagueSelect(
                owner_id,
                mode,
            )
        )

        city_button = discord.ui.Button(
            label="Man City — tất cả trận",
            emoji="🔵",
            style=discord.ButtonStyle.primary,
            row=1,
        )

        async def city_callback(
            interaction,
        ):

            await show_matches(
                interaction,
                owner_id,
                mode,
                "MANCITY",
                0,
            )

        city_button.callback = city_callback

        self.add_item(
            city_button
        )


# ============================================================
# MATCH LIST VIEW
# ============================================================

class MatchListView(
    OwnerView
):

    def __init__(
        self,
        owner_id,
        mode,
        matches,
        code,
        page=0,
    ):

        super().__init__(
            owner_id
        )

        self.mode = mode
        self.matches = matches
        self.code = code
        self.page = page

        self.per_page = 20

        start = (
            page
            * self.per_page
        )

        current = matches[
            start:
            start + self.per_page
        ]

        # Tối đa 20 nút trận
        for match in current:

            home = match.get(
                "homeTeam",
                {},
            ).get(
                "name",
                "?",
            )

            away = match.get(
                "awayTeam",
                {},
            ).get(
                "name",
                "?",
            )

            label = (
                f"{home} vs {away}"
            )

            if len(label) > 75:
                label = (
                    label[:72]
                    + "..."
                )

            button = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.secondary,
                row=min(
                    len(self.children) // 5,
                    3,
                ),
            )

            async def callback(
                interaction,
                selected_match=match,
            ):

                if self.mode == "bet":

                    await interaction.response.send_modal(
                        StakeModal(
                            interaction.user.id,
                            selected_match,
                            self.code,
                        )
                    )

                else:

                    await show_prediction(
                        interaction,
                        selected_match,
                    )

            button.callback = callback

            self.add_item(
                button
            )

        total_pages = max(
            1,
            math.ceil(
                len(matches)
                / self.per_page
            ),
        )

        # ----------------------------------------
        # PREVIOUS
        # ----------------------------------------

        if page > 0:

            previous = discord.ui.Button(
                label="◀ Trang trước",
                style=discord.ButtonStyle.primary,
                row=4,
            )

            async def previous_callback(
                interaction,
            ):

                await show_matches(
                    interaction,
                    self.owner_id,
                    self.mode,
                    self.code,
                    self.page - 1,
                )

            previous.callback = previous_callback

            self.add_item(
                previous
            )

        # ----------------------------------------
        # NEXT
        # ----------------------------------------

        if (
            page + 1
            < total_pages
        ):

            next_button = discord.ui.Button(
                label="Trang sau ▶",
                style=discord.ButtonStyle.primary,
                row=4,
            )

            async def next_callback(
                interaction,
            ):

                await show_matches(
                    interaction,
                    self.owner_id,
                    self.mode,
                    self.code,
                    self.page + 1,
                )

            next_button.callback = next_callback

            self.add_item(
                next_button
            )

        # ----------------------------------------
        # BACK
        # ----------------------------------------

        back = discord.ui.Button(
            label="↩ Chọn giải",
            style=discord.ButtonStyle.success,
            row=4,
        )

        async def back_callback(
            interaction,
        ):

            embed = discord.Embed(
                title="⚽ Chọn giải đấu",
                description=(
                    "Chọn một giải hoặc "
                    "xem tất cả trận Man City."
                ),
                color=discord.Color.blue(),
            )

            await interaction.response.edit_message(
                content=None,
                embed=embed,
                view=FootballMainView(
                    self.owner_id,
                    self.mode,
                ),
            )

        back.callback = back_callback

        self.add_item(
            back
        )


# ============================================================
# STAKE MODAL
# ============================================================

class StakeModal(
    discord.ui.Modal,
    title="💰 Nhập tiền cược",
):

    stake = discord.ui.TextInput(
        label="Số xu muốn cược",
        placeholder="Ví dụ: 10000",
        required=True,
        min_length=1,
        max_length=12,
    )

    def __init__(
        self,
        owner_id,
        match,
        league,
    ):

        super().__init__()

        self.owner_id = owner_id
        self.match = match
        self.league = league

    async def on_submit(
        self,
        interaction,
    ):

        try:

            amount = int(
                str(
                    self.stake.value
                )
                .replace(".", "")
                .replace(",", "")
                .strip()
            )

        except ValueError:

            await interaction.response.send_message(
                "❌ Số tiền không hợp lệ.",
                ephemeral=True,
            )

            return

        wallet = await get_wallet(
            interaction.user.id
        )

        if (
            amount <= 0
            or amount > wallet["balance"]
        ):

            await interaction.response.send_message(
                (
                    "❌ Cược phải từ 1 đến "
                    f"**{money(wallet['balance'])} xu**."
                ),
                ephemeral=True,
            )

            return

        embed = match_embed(
            self.match,
            "🎫 Chọn cửa cược",
        )

        embed.add_field(
            name="💰 Tiền cược",
            value=money(amount),
            inline=False,
        )

        await interaction.response.send_message(
            embed=embed,
            view=BetChoiceView(
                interaction.user.id,
                self.match,
                self.league,
                amount,
            ),
            ephemeral=True,
        )


# ============================================================
# BET CHOICE VIEW
# ============================================================

class BetChoiceView(
    OwnerView
):

    def __init__(
        self,
        owner_id,
        match,
        league,
        stake,
    ):

        super().__init__(
            owner_id
        )

        self.match = match
        self.league = league
        self.stake = stake

        choices = [
            (
                "Đội nhà",
                "HOME",
                "🏠",
            ),
            (
                "Hòa",
                "DRAW",
                "🤝",
            ),
            (
                "Đội khách",
                "AWAY",
                "✈️",
            ),
        ]

        for label, choice, emoji in choices:

            button = discord.ui.Button(
                label=label,
                emoji=emoji,
                style=discord.ButtonStyle.primary,
            )

            async def callback(
                interaction,
                selected=choice,
            ):

                result = await create_bet(
                    interaction.user.id,
                    interaction.guild_id,
                    self.match,
                    self.league,
                    selected,
                    self.stake,
                )

                if not result:

                    await interaction.response.send_message(
                        (
                            "❌ Số dư không đủ "
                            "hoặc tiền cược không hợp lệ."
                        ),
                        ephemeral=True,
                    )

                    return

                await interaction.response.edit_message(
                    content=(
                        f"✅ Đã cược "
                        f"**{money(self.stake)} xu** "
                        f"vào **{selected}**.\n"
                        f"Số dư còn: "
                        f"**{money(result['balance'])} xu**"
                    ),
                    embed=None,
                    view=None,
                )

                if (
                    result["new_vip"]
                    > result["old_vip"]
                ):

                    await announce_vip_upgrade(
                        interaction.user.id,
                        result["old_vip"],
                        result["new_vip"],
                        result["balance"],
                    )

            button.callback = callback

            self.add_item(
                button
            )


# ============================================================
# PREDICTION BACK VIEW
# ============================================================

class PredictionView(
    OwnerView
):

    def __init__(
        self,
        owner_id,
    ):

        super().__init__(
            owner_id
        )

        back = discord.ui.Button(
            label="↩ Về chọn giải",
            style=discord.ButtonStyle.success,
        )

        async def back_callback(
            interaction,
        ):

            embed = discord.Embed(
                title="🔎 Soi bóng đá",
                description=(
                    "Chọn giải hoặc "
                    "xem tất cả trận Man City."
                ),
                color=discord.Color.purple(),
            )

            await interaction.response.edit_message(
                embed=embed,
                view=FootballMainView(
                    self.owner_id,
                    "predict",
                ),
            )

        back.callback = back_callback

        self.add_item(
            back
        )


# ============================================================
# SHOW MATCHES
# ============================================================

async def show_matches(
    interaction,
    owner_id,
    mode,
    code,
    page,
):

    try:

        if code == "MANCITY":

            matches = (
                await get_upcoming_mancity_matches()
            )

            title = (
                "🔵 Man City — "
                "tất cả trận sắp tới"
            )

        else:

            if code not in FREE_COMPETITIONS:

                await interaction.response.edit_message(
                    content=(
                        f"⚠️ **{LEAGUES[code]['name']}** "
                        "hiện không có dữ liệu trên "
                        "gói Free của football-data.org.\n\n"
                        "FA Cup và Carabao Cup không nằm "
                        "trong coverage Free hiện tại của "
                        "nguồn này."
                    ),
                    embed=None,
                    view=FootballMainView(
                        owner_id,
                        mode,
                    ),
                )

                return

            matches = (
                await get_upcoming_league_matches(
                    code
                )
            )

            title = (
                f"{LEAGUES[code]['emoji']} "
                f"{LEAGUES[code]['name']}"
            )

        if not matches:

            await interaction.response.edit_message(
                content=(
                    "Không tìm thấy trận sắp tới "
                    "trong khoảng thời gian hiện tại."
                ),
                embed=None,
                view=FootballMainView(
                    owner_id,
                    mode,
                ),
            )

            return

        total_pages = max(
            1,
            math.ceil(
                len(matches)
                / 20
            ),
        )

        page = max(
            0,
            min(
                page,
                total_pages - 1,
            ),
        )

        embed = discord.Embed(
            title=title,
            description=(
                f"Tìm thấy **{len(matches)} trận**.\n"
                f"Trang **{page + 1}/{total_pages}**."
            ),
            color=discord.Color.blue(),
        )

        await interaction.response.edit_message(
            content=None,
            embed=embed,
            view=MatchListView(
                owner_id,
                mode,
                matches,
                code,
                page,
            ),
        )

    except Exception as error:

        await interaction.response.edit_message(
            content=(
                "❌ Không lấy được lịch bóng đá:\n"
                f"`{str(error)[:800]}`"
            ),
            embed=None,
            view=FootballMainView(
                owner_id,
                mode,
            ),
        )


# ============================================================
# SHOW PREDICTION
# ============================================================

async def show_prediction(
    interaction,
    match,
):

    await interaction.response.defer(
        ephemeral=True
    )

    try:

        home_stats, away_stats, prediction = (
            await make_prediction(
                match
            )
        )

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

        top_scores = "\n".join(
            (
                f"{home_goals}-{away_goals}: "
                f"{pct(probability)}"
            )
            for (
                probability,
                home_goals,
                away_goals,
            )
            in prediction["top_scores"]
        )

        embed = discord.Embed(
            title=(
                f"🔎 SOI: "
                f"{home} vs {away}"
            ),
            color=discord.Color.purple(),
        )

        embed.add_field(
            name="🕐 Giờ",
            value=display_time(
                match.get("utcDate")
            ),
            inline=False,
        )

        embed.add_field(
            name="🎯 Tỷ số dự đoán",
            value=(
                f"**{prediction['exact']}**"
            ),
            inline=True,
        )

        embed.add_field(
            name="📊 1X2",
            value=(
                f"🏠 "
                f"{pct(prediction['home_prob'])}\n"
                f"🤝 "
                f"{pct(prediction['draw_prob'])}\n"
                f"✈️ "
                f"{pct(prediction['away_prob'])}"
            ),
            inline=True,
        )

        embed.add_field(
            name="🔢 Top tỷ số",
            value=top_scores,
            inline=True,
        )

        embed.add_field(
            name="📈 Phong độ gần đây",
            value=(
                f"**{home}**: "
                f"GF {home_stats['gf']:.2f} | "
                f"GA {home_stats['ga']:.2f} | "
                f"PPG {home_stats['ppg']:.2f}\n"
                f"**{away}**: "
                f"GF {away_stats['gf']:.2f} | "
                f"GA {away_stats['ga']:.2f} | "
                f"PPG {away_stats['ppg']:.2f}"
            ),
            inline=False,
        )

        embed.set_footer(
            text=(
                "⚠️ Đây là ước tính của bot "
                "từ dữ liệu gần đây, không phải "
                "odds chính thức."
            )
        )

        await interaction.followup.send(
            embed=embed,
            view=PredictionView(
                interaction.user.id
            ),
            ephemeral=True,
        )

    except Exception as error:

        await interaction.followup.send(
            (
                "❌ Không soi được trận này:\n"
                f"`{str(error)[:800]}`"
            ),
            ephemeral=True,
        )


# ============================================================
# ADMIN PASSWORD
# ============================================================

class PasswordModal(
    discord.ui.Modal,
    title="🔐 Xác thực quản trị",
):

    password = discord.ui.TextInput(
        label="Mật khẩu admin",
        style=discord.TextStyle.short,
        required=True,
    )

    def __init__(
        self,
        action,
    ):

        super().__init__()

        self.action = action

    async def on_submit(
        self,
        interaction,
    ):

        if (
            str(self.password.value)
            != ADMIN_PASSWORD
        ):

            await interaction.response.send_message(
                "❌ Sai mật khẩu.",
                ephemeral=True,
            )

            return

        if (
            ADMIN_USER_IDS
            and interaction.user.id
            not in ADMIN_USER_IDS
        ):

            await interaction.response.send_message(
                (
                    "❌ Tài khoản này không nằm "
                    "trong ADMIN_USER_IDS."
                ),
                ephemeral=True,
            )

            return

        if self.action == "admin":

            await interaction.response.send_message(
                "🛠️ Chọn thao tác quản trị:",
                view=AdminPanelView(
                    interaction.user.id
                ),
                ephemeral=True,
            )

        else:

            count = await settle_all_finished()

            await interaction.response.send_message(
                (
                    f"✅ Đã kiểm tra và tất toán "
                    f"**{count} trận** đã kết thúc."
                ),
                ephemeral=True,
            )


# ============================================================
# ADMIN PANEL
# ============================================================

class AdminPanelView(
    OwnerView
):

    def __init__(
        self,
        owner_id,
    ):

        super().__init__(
            owner_id
        )

        add_button = discord.ui.Button(
            label="➕ Cộng xu",
            style=discord.ButtonStyle.success,
        )

        remove_button = discord.ui.Button(
            label="➖ Trừ xu",
            style=discord.ButtonStyle.danger,
        )

        async def add_callback(
            interaction,
        ):

            await interaction.response.send_modal(
                CoinModal(
                    interaction.user.id,
                    1,
                )
            )

        async def remove_callback(
            interaction,
        ):

            await interaction.response.send_modal(
                CoinModal(
                    interaction.user.id,
                    -1,
                )
            )

        add_button.callback = add_callback
        remove_button.callback = remove_callback

        self.add_item(
            add_button
        )

        self.add_item(
            remove_button
        )


# ============================================================
# COIN ADMIN MODAL
# ============================================================

class CoinModal(
    discord.ui.Modal,
    title="💰 Điều chỉnh xu",
):

    user_id = discord.ui.TextInput(
        label="User ID",
        placeholder="123456789...",
        required=True,
    )

    amount = discord.ui.TextInput(
        label="Số xu",
        placeholder="10000",
        required=True,
    )

    def __init__(
        self,
        owner_id,
        direction,
    ):

        super().__init__()

        self.owner_id = owner_id
        self.direction = direction

    async def on_submit(
        self,
        interaction,
    ):

        try:

            user_id = int(
                str(
                    self.user_id.value
                ).strip()
            )

            amount = abs(
                int(
                    str(
                        self.amount.value
                    )
                    .replace(".", "")
                    .replace(",", "")
                    .strip()
                )
            )

        except ValueError:

            await interaction.response.send_message(
                (
                    "❌ User ID hoặc "
                    "số xu không hợp lệ."
                ),
                ephemeral=True,
            )

            return

        wallet = await get_wallet(
            user_id
        )

        if (
            self.direction < 0
            and amount
            > wallet["balance"]
        ):

            await interaction.response.send_message(
                (
                    "❌ Không thể trừ "
                    "nhiều hơn số dư."
                ),
                ephemeral=True,
            )

            return

        old_balance, new_balance, old_vip, new_vip = (
            await add_balance(
                user_id,
                amount * self.direction,
            )
        )

        await interaction.response.send_message(
            (
                f"✅ User `{user_id}`:\n"
                f"💰 **{money(old_balance)} "
                f"→ {money(new_balance)} xu**\n"
                f"👑 **{vip_name(old_vip)} "
                f"→ {vip_name(new_vip)}**"
            ),
            ephemeral=True,
        )

        if new_vip > old_vip:

            await announce_vip_upgrade(
                user_id,
                old_vip,
                new_vip,
                new_balance,
            )


# ============================================================
# MANUAL SETTLEMENT
# ============================================================

async def settle_all_finished():

    count = 0

    ids = await open_match_ids()

    for match_id in ids:

        try:

            match = await get_match(
                match_id
            )

            if (
                match.get("status")
                != "FINISHED"
            ):
                continue

            result = result_for_match(
                match
            )

            if not result:
                continue

            settled = await settle_match_bets(
                match_id,
                result,
            )

            if not settled:
                continue

            count += 1

            await notify_match_finished(
                match,
                settled,
            )

            upgrades = {}

            for item in settled:

                if (
                    item["new_vip"]
                    > item["old_vip"]
                ):

                    upgrades[
                        item["user_id"]
                    ] = (
                        item["old_vip"],
                        item["new_vip"],
                        item["balance"],
                    )

            for (
                user_id,
                data,
            ) in upgrades.items():

                old_vip, new_vip, balance = data

                await announce_vip_upgrade(
                    user_id,
                    old_vip,
                    new_vip,
                    balance,
                )

        except Exception as error:

            print(
                f"⚠️ Manual settle "
                f"{match_id}: "
                f"{error}"
            )

    return count


# ============================================================
# SLASH COMMANDS
# ============================================================

@bot.tree.command(
    name="cuoc",
    description="Mở menu đặt cược bóng đá",
)
async def cuoc(
    interaction: discord.Interaction,
):

    await ensure_user(
        interaction.user.id
    )

    embed = discord.Embed(
        title="🎫 ĐẶT CƯỢC BÓNG ĐÁ",
        description=(
            "Chọn giải đấu hoặc "
            "xem toàn bộ trận Man City."
        ),
        color=discord.Color.blue(),
    )

    await interaction.response.send_message(
        embed=embed,
        view=FootballMainView(
            interaction.user.id,
            "bet",
        ),
        ephemeral=True,
    )


@bot.tree.command(
    name="soi",
    description="Soi và dự đoán trận bóng đá",
)
async def soi(
    interaction: discord.Interaction,
):

    embed = discord.Embed(
        title="🔎 SOI BÓNG ĐÁ",
        description=(
            "Chọn giải hoặc "
            "xem tất cả trận Man City."
        ),
        color=discord.Color.purple(),
    )

    await interaction.response.send_message(
        embed=embed,
        view=FootballMainView(
            interaction.user.id,
            "predict",
        ),
        ephemeral=True,
    )


@bot.tree.command(
    name="vi",
    description="Xem số dư và VIP",
)
async def vi(
    interaction: discord.Interaction,
):

    wallet = await get_wallet(
        interaction.user.id
    )

    next_vip = next_vip_info(
        wallet["balance"]
    )

    description = (
        f"💰 Số dư: "
        f"**{money(wallet['balance'])} xu**\n"
        f"👑 Hạng: "
        f"**{vip_name(wallet['vip_level'])}**"
    )

    if next_vip:

        level, minimum, remaining = next_vip

        description += (
            f"\n🎯 Còn "
            f"**{money(remaining)} xu** "
            f"để lên **VIP {level}**."
        )

    else:

        description += (
            "\n🏆 Bạn đã đạt VIP cao nhất."
        )

    await interaction.response.send_message(
        description,
        ephemeral=True,
    )


@bot.tree.command(
    name="comat",
    description="Nhận 50.000 xu mỗi ngày",
)
async def comat(
    interaction: discord.Interaction,
):

    ok, balance, old_vip, new_vip = (
        await claim_daily(
            interaction.user.id
        )
    )

    if not ok:

        await interaction.response.send_message(
            (
                "⏳ Hôm nay bạn đã nhận rồi.\n"
                f"💰 Số dư: "
                f"**{money(balance)} xu**"
            ),
            ephemeral=True,
        )

        return

    await interaction.response.send_message(
        (
            f"🎁 Nhận "
            f"**{money(DAILY_BONUS)} xu** "
            "thành công!\n"
            f"💰 Số dư: "
            f"**{money(balance)} xu**\n"
            f"👑 {vip_name(new_vip)}"
        ),
        ephemeral=True,
    )

    if new_vip > old_vip:

        await announce_vip_upgrade(
            interaction.user.id,
            old_vip,
            new_vip,
            balance,
        )


@bot.tree.command(
    name="khoinghiep",
    description="Nhận 1.000 xu khởi nghiệp một lần",
)
async def khoinghiep(
    interaction: discord.Interaction,
):

    ok, balance, old_vip, new_vip = (
        await claim_starter(
            interaction.user.id
        )
    )

    if not ok:

        await interaction.response.send_message(
            (
                "❌ Bạn đã nhận "
                "1.000 xu khởi nghiệp rồi."
            ),
            ephemeral=True,
        )

        return

    await interaction.response.send_message(
        (
            f"🚀 Nhận "
            f"**{money(STARTER_BONUS)} xu**!\n"
            f"💰 Số dư: "
            f"**{money(balance)} xu**"
        ),
        ephemeral=True,
    )

    if new_vip > old_vip:

        await announce_vip_upgrade(
            interaction.user.id,
            old_vip,
            new_vip,
            balance,
        )


@bot.tree.command(
    name="admin",
    description="Mở bảng quản trị",
)
async def admin(
    interaction: discord.Interaction,
):

    await interaction.response.send_modal(
        PasswordModal(
            "admin"
        )
    )


@bot.tree.command(
    name="kettoan",
    description="Kiểm tra và tất toán các trận đã kết thúc",
)
async def kettoan(
    interaction: discord.Interaction,
):

    await interaction.response.send_modal(
        PasswordModal(
            "settle"
        )
    )


@bot.tree.command(
    name="cado",
    description="Xem hướng dẫn bot cá cược",
)
async def cado(
    interaction: discord.Interaction,
):

    embed = discord.Embed(
        title="⚽ BOT CÁ CƯỢC BÓNG ĐÁ",
        description=(
            "`/cuoc` — đặt cược\n"
            "`/soi` — soi trận\n"
            "`/vi` — xem ví/VIP\n"
            "`/comat` — nhận 50.000 xu/ngày\n"
            "`/khoinghiep` — nhận 1.000 xu một lần\n"
            "`/kettoan` — admin kiểm tra tất toán\n"
        ),
        color=discord.Color.green(),
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
    )


@bot.tree.command(
    name="ping",
    description="Kiểm tra bot",
)
async def ping(
    interaction: discord.Interaction,
):

    await interaction.response.send_message(
        (
            "🏓 Pong! "
            f"`{round(bot.latency * 1000)} ms`"
        ),
        ephemeral=True,
    )


@bot.tree.command(
    name="apiquota",
    description="Xem cấu hình nguồn dữ liệu",
)
async def apiquota(
    interaction: discord.Interaction,
):

    configured = bool(
        FOOTBALL_DATA_API_KEY
    )

    await interaction.response.send_message(
        (
            "🔌 **Football-data.org**\n"
            f"Token: **"
            f"{'Đã cấu hình' if configured else 'CHƯA CẤU HÌNH'}"
            "**\n"
            "Free plan: giới hạn khoảng "
            "10 requests/phút; dữ liệu có thể bị trễ.\n"
            "⚠️ `/cuoc` và `/soi` dùng dữ liệu "
            "của football-data.org."
        ),
        ephemeral=True,
    )


# ============================================================
# BOT READY / SLASH COMMAND SYNC
# ============================================================

@bot.event
async def on_ready():

    global monitor_task
    global synced_once

    print(
        f"✅ Bot online: "
        f"{bot.user} | ID={bot.user.id}"
    )

    if not synced_once:

        try:

            if GUILD_ID:

                guild = discord.Object(
                    id=int(GUILD_ID)
                )

                # QUAN TRỌNG:
                # Copy slash commands global
                # sang server trước khi sync.
                bot.tree.copy_global_to(
                    guild=guild
                )

                synced = await bot.tree.sync(
                    guild=guild
                )

                print(
                    f"✅ Synced "
                    f"{len(synced)} slash commands "
                    f"vào guild {GUILD_ID}"
                )

            else:

                synced = await bot.tree.sync()

                print(
                    f"✅ Synced "
                    f"{len(synced)} slash commands "
                    "toàn cục"
                )

            synced_once = True

        except Exception as error:

            print(
                "❌ Sync slash commands lỗi: "
                f"{type(error).__name__}: "
                f"{error}"
            )

    if monitor_task is None:

        monitor_task = asyncio.create_task(
            monitor_loop()
        )

        print(
            "✅ Match monitor started"
        )


# ============================================================
# HEALTH SERVER FOR RAILWAY
# ============================================================

async def health_handler(
    request,
):

    return web.json_response(
        {
            "ok": True,
            "bot": (
                str(bot.user)
                if bot.user
                else None
            ),
        }
    )


async def start_health_server():

    global health_runner

    app = web.Application()

    app.router.add_get(
        "/",
        health_handler,
    )

    app.router.add_get(
        "/health",
        health_handler,
    )

    health_runner = web.AppRunner(
        app
    )

    await health_runner.setup()

    port = int(
        os.getenv(
            "PORT",
            "8080",
        )
    )

    site = web.TCPSite(
        health_runner,
        "0.0.0.0",
        port,
    )

    await site.start()

    print(
        f"🌐 Health server listening "
        f"on {port}"
    )


# ============================================================
# MAIN
# ============================================================

async def main():

    global http_session

    if not DISCORD_TOKEN:

        raise RuntimeError(
            "Thiếu DISCORD_TOKEN "
            "trên Railway."
        )

    await init_db()

    await start_health_server()

    try:

        await bot.start(
            DISCORD_TOKEN
        )

    finally:

        if (
            http_session
            and not http_session.closed
        ):

            await http_session.close()


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        pass

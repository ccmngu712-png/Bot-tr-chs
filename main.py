import os
import math
import sqlite3
import asyncio
from datetime import datetime, timedelta, timezone

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

# TOKEN FOOTBALL-DATA.ORG
# Không ghi token trực tiếp vào code.
FOOTBALL_DATA_API_KEY = (
    os.getenv("FOOTBALL_DATA_API_KEY", "").strip()
    or os.getenv("FOOTBALL_DATA_TOKEN", "").strip()
    or os.getenv("FOOTBALL_DATA_KEY", "").strip()
)

# Mật khẩu admin mặc định.
# Có thể tạo Railway Variable ADMIN_PASSWORD để đổi.
ADMIN_PASSWORD = os.getenv(
    "ADMIN_PASSWORD",
    "provip👑"
)

API_BASE = "https://api.football-data.org/v4"

# Hiển thị giờ Việt Nam.
VN_TZ = timezone(timedelta(hours=7))

# Coin
STARTING_BALANCE = 100_000
STARTER_BONUS = 1_000
DAILY_BONUS = 50_000

# Thắng cược = 2x tiền cược
BET_PAYOUT = 2

# Manchester City - football-data.org
MANCHESTER_CITY_TEAM_ID = 65

# Railway có /data thì lưu DB ở đó.
DB_PATH = (
    "/data/football_bot.db"
    if os.path.isdir("/data")
    else "football_bot.db"
)


# ============================================================
# 11 GIẢI
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


# Free Tier football-data.org hiện hỗ trợ các giải này.
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
}

VIP_EMOJIS = {
    10: "👑💎",
    9: "👑💎",
    8: "💎",
    7: "👑",
    6: "🔱",
    5: "🔥",
    4: "⭐",
    3: "🌟",
    2: "💠",
    1: "✨",
    0: "⚽",
}


# ============================================================
# BOT
# ============================================================

intents = discord.Intents.default()

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
)

http_session = None
health_runner = None

API_CACHE = {}
API_CACHE_TTL = 60

API_LOCK = asyncio.Lock()


# ============================================================
# DATABASE
# ============================================================

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS wallets (
            user_id INTEGER PRIMARY KEY,
            balance INTEGER NOT NULL DEFAULT 100000,
            started INTEGER NOT NULL DEFAULT 0,
            vip INTEGER NOT NULL DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_claims (
            user_id INTEGER NOT NULL,
            claim_date TEXT NOT NULL,
            PRIMARY KEY (user_id, claim_date)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            fixture_id INTEGER NOT NULL,
            league TEXT NOT NULL DEFAULT '',
            choice TEXT NOT NULL,
            stake INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL
        )
    """)

    # DB cũ nếu chưa có league
    columns = {
        row["name"]
        for row in cur.execute(
            "PRAGMA table_info(bets)"
        ).fetchall()
    }

    if "league" not in columns:
        cur.execute(
            """
            ALTER TABLE bets
            ADD COLUMN league TEXT NOT NULL DEFAULT ''
            """
        )

    conn.commit()
    conn.close()


def ensure_user(user_id):
    conn = db()

    conn.execute(
        """
        INSERT OR IGNORE INTO wallets
        (user_id, balance, started, vip)
        VALUES (?, ?, 0, 0)
        """,
        (
            user_id,
            STARTING_BALANCE,
        ),
    )

    conn.commit()
    conn.close()


def get_wallet(user_id):
    ensure_user(user_id)

    conn = db()

    row = conn.execute(
        """
        SELECT *
        FROM wallets
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()

    conn.close()

    return row


def get_balance(user_id):
    return int(
        get_wallet(user_id)["balance"]
    )


def get_vip(balance):
    for level in range(10, 0, -1):
        if balance >= VIP_THRESHOLDS[level]:
            return level

    return 0


def vip_name(level):
    if level <= 0:
        return "Thường"

    return f"VIP {level}"


def next_vip(balance):
    current = get_vip(balance)

    if current >= 10:
        return None, None

    level = current + 1
    return level, VIP_THRESHOLDS[level]


def change_balance(user_id, amount):
    ensure_user(user_id)

    conn = db()

    row = conn.execute(
        """
        SELECT balance, vip
        FROM wallets
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()

    old_balance = int(row["balance"])
    old_vip = int(row["vip"])

    new_balance = max(
        0,
        old_balance + int(amount)
    )

    new_vip = get_vip(new_balance)

    conn.execute(
        """
        UPDATE wallets
        SET balance = ?, vip = ?
        WHERE user_id = ?
        """,
        (
            new_balance,
            new_vip,
            user_id,
        ),
    )

    conn.commit()
    conn.close()

    return (
        old_balance,
        new_balance,
        old_vip,
        new_vip,
    )


# ============================================================
# API FOOTBALL-DATA.ORG
# ============================================================

async def get_http_session():
    global http_session

    if (
        http_session is None
        or http_session.closed
    ):
        http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(
                total=20
            )
        )

    return http_session


async def api_get(
    path,
    params=None,
    cache_seconds=60,
):
    if not FOOTBALL_DATA_API_KEY:
        raise RuntimeError(
            "Chưa có FOOTBALL_DATA_API_KEY trên Railway."
        )

    session = await get_http_session()

    cache_key = (
        path,
        tuple(
            sorted(
                (params or {}).items()
            )
        ),
    )

    now = asyncio.get_running_loop().time()

    cached = API_CACHE.get(cache_key)

    if (
        cached
        and cached["expires"] > now
    ):
        return cached["data"]

    headers = {
        "X-Auth-Token": FOOTBALL_DATA_API_KEY,
        "Accept": "application/json",
        "User-Agent": "Da-Bong-24h-Bot/2.0",
    }

    async with API_LOCK:

        now = asyncio.get_running_loop().time()

        cached = API_CACHE.get(cache_key)

        if (
            cached
            and cached["expires"] > now
        ):
            return cached["data"]

        try:
            async with session.get(
                f"{API_BASE}{path}",
                params=params,
                headers=headers,
            ) as response:

                text = await response.text()

                if response.status == 200:
                    try:
                        data = await response.json()
                    except Exception:
                        raise RuntimeError(
                            "API trả dữ liệu không hợp lệ."
                        )

                    API_CACHE[cache_key] = {
                        "expires": now + cache_seconds,
                        "data": data,
                    }

                    return data

                if response.status == 401:
                    raise RuntimeError(
                        "API key football-data.org không hợp lệ "
                        "hoặc đã hết hiệu lực."
                    )

                if response.status == 403:
                    raise RuntimeError(
                        "API từ chối truy cập. "
                        "Giải/endpoint này có thể không nằm "
                        "trong Free Tier."
                    )

                if response.status == 429:
                    raise RuntimeError(
                        "API đang giới hạn 10 request/phút. "
                        "Chờ khoảng 1 phút rồi thử lại."
                    )

                raise RuntimeError(
                    f"football-data.org HTTP "
                    f"{response.status}: {text[:400]}"
                )

        except asyncio.TimeoutError:
            raise RuntimeError(
                "API phản hồi quá lâu."
            )

        except aiohttp.ClientError as e:
            raise RuntimeError(
                f"Lỗi kết nối API: {e}"
            )


async def get_league_matches(
    league_code,
    days=90,
):
    if league_code not in FREE_COMPETITIONS:
        return None, (
            f"⚠️ **{LEAGUES[league_code]['name']}**\n\n"
            "Giải này vẫn có trong menu 11 giải, "
            "nhưng football-data.org Free Tier hiện "
            "không cung cấp dữ liệu cho giải này."
        )

    today = datetime.now(
        VN_TZ
    ).date()

    end = today + timedelta(
        days=days
    )

    data = await api_get(
        f"/competitions/{league_code}/matches",
        params={
            "dateFrom": today.isoformat(),
            "dateTo": end.isoformat(),
            "status": "SCHEDULED",
            "limit": 100,
        },
        cache_seconds=90,
    )

    matches = data.get(
        "matches",
        []
    )

    matches.sort(
        key=lambda x: x.get(
            "utcDate",
            ""
        )
    )

    return matches, None


async def get_mancity_matches(
    days=180,
):
    today = datetime.now(
        VN_TZ
    ).date()

    end = today + timedelta(
        days=days
    )

    data = await api_get(
        f"/teams/{MANCHESTER_CITY_TEAM_ID}/matches",
        params={
            "dateFrom": today.isoformat(),
            "dateTo": end.isoformat(),
            "status": "SCHEDULED",
            "limit": 100,
        },
        cache_seconds=90,
    )

    matches = data.get(
        "matches",
        []
    )

    matches.sort(
        key=lambda x: x.get(
            "utcDate",
            ""
        )
    )

    return matches


async def get_match(match_id):
    return await api_get(
        f"/matches/{int(match_id)}",
        cache_seconds=30,
    )


async def get_recent_matches(
    team_id,
    limit=10,
):
    data = await api_get(
        f"/teams/{int(team_id)}/matches",
        params={
            "status": "FINISHED",
            "limit": limit,
        },
        cache_seconds=180,
    )

    matches = data.get(
        "matches",
        []
    )

    matches.sort(
        key=lambda x: x.get(
            "utcDate",
            ""
        ),
        reverse=True,
    )

    return matches[:limit]


# ============================================================
# DATE / MATCH HELPERS
# ============================================================

def parse_datetime(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(
            value.replace(
                "Z",
                "+00:00"
            )
        ).astimezone(VN_TZ)

    except Exception:
        return None


def match_time(value):
    dt = parse_datetime(value)

    if not dt:
        return "Chưa có giờ"

    return dt.strftime(
        "%d/%m/%Y %H:%M"
    )


def team_name(team):
    return (
        team.get("shortName")
        or team.get("name")
        or "?"
    )


def match_names(match):
    return (
        team_name(
            match.get(
                "homeTeam",
                {}
            )
        ),
        team_name(
            match.get(
                "awayTeam",
                {}
            )
        ),
    )


def short_match_label(match):
    home, away = match_names(
        match
    )

    dt = parse_datetime(
        match.get(
            "utcDate"
        )
    )

    if dt:
        return (
            f"{dt.strftime('%d/%m %H:%M')} "
            f"• {home} - {away}"
        )[:100]

    return (
        f"{home} - {away}"
    )[:100]


def result_from_match(match):
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
        return None

    if home > away:
        return "HOME"

    if away > home:
        return "AWAY"

    return "DRAW"


# ============================================================
# DỰ ĐOÁN TỶ SỐ
# ============================================================

def team_stats(
    matches,
    team_id,
):
    scored = []
    conceded = []

    for match in matches:

        home_id = match.get(
            "homeTeam",
            {}
        ).get("id")

        away_id = match.get(
            "awayTeam",
            {}
        ).get("id")

        full = match.get(
            "score",
            {}
        ).get(
            "fullTime",
            {}
        )

        home_goals = full.get(
            "home"
        )

        away_goals = full.get(
            "away"
        )

        if (
            home_goals is None
            or away_goals is None
        ):
            continue

        if home_id == team_id:
            scored.append(
                float(home_goals)
            )
            conceded.append(
                float(away_goals)
            )

        elif away_id == team_id:
            scored.append(
                float(away_goals)
            )
            conceded.append(
                float(home_goals)
            )

    if not scored:
        return {
            "scored": 1.20,
            "conceded": 1.20,
            "games": 0,
        }

    return {
        "scored": sum(scored) / len(scored),
        "conceded": sum(conceded) / len(conceded),
        "games": len(scored),
    }


def poisson(
    goals,
    expected,
):
    if expected <= 0:
        return 0

    return (
        math.exp(-expected)
        * expected ** goals
        / math.factorial(goals)
    )


def calculate_prediction(
    home_stats,
    away_stats,
):
    # Mô hình đơn giản:
    #
    # Chủ nhà:
    #   khả năng ghi bàn của chủ
    #   + khả năng thủng lưới của khách
    #   + lợi thế sân nhà
    #
    # Đội khách:
    #   khả năng ghi bàn của khách
    #   + khả năng thủng lưới của chủ

    home_xg = (
        (
            home_stats["scored"]
            + away_stats["conceded"]
        )
        / 2
    ) + 0.15

    away_xg = (
        home_stats["conceded"]
        + away_stats["scored"]
    ) / 2

    home_xg = max(
        0.20,
        min(3.50, home_xg)
    )

    away_xg = max(
        0.20,
        min(3.50, away_xg)
    )

    matrix = {}

    total = 0

    for home_goals in range(0, 8):

        for away_goals in range(0, 8):

            probability = (
                poisson(
                    home_goals,
                    home_xg,
                )
                *
                poisson(
                    away_goals,
                    away_xg,
                )
            )

            matrix[
                (home_goals, away_goals)
            ] = probability

            total += probability

    if total <= 0:
        return {
            "score": (1, 1),
            "home_prob": 0.333,
            "draw_prob": 0.334,
            "away_prob": 0.333,
            "home_xg": home_xg,
            "away_xg": away_xg,
        }

    for key in matrix:
        matrix[key] /= total

    home_prob = sum(
        p
        for (h, a), p in matrix.items()
        if h > a
    )

    draw_prob = sum(
        p
        for (h, a), p in matrix.items()
        if h == a
    )

    away_prob = sum(
        p
        for (h, a), p in matrix.items()
        if h < a
    )

    best_score = max(
        matrix,
        key=matrix.get
    )

    return {
        "score": best_score,
        "home_prob": home_prob,
        "draw_prob": draw_prob,
        "away_prob": away_prob,
        "home_xg": home_xg,
        "away_xg": away_xg,
    }


async def predict_match(match):
    home_team = match.get(
        "homeTeam",
        {}
    )

    away_team = match.get(
        "awayTeam",
        {}
    )

    home_id = home_team.get(
        "id"
    )

    away_id = away_team.get(
        "id"
    )

    if not home_id or not away_id:
        raise RuntimeError(
            "Không có ID đội bóng."
        )

    home_matches, away_matches = await asyncio.gather(
        get_recent_matches(
            home_id,
            10
        ),
        get_recent_matches(
            away_id,
            10
        ),
    )

    home_stats = team_stats(
        home_matches,
        home_id,
    )

    away_stats = team_stats(
        away_matches,
        away_id,
    )

    result = calculate_prediction(
        home_stats,
        away_stats,
    )

    hp = result["home_prob"]
    dp = result["draw_prob"]
    ap = result["away_prob"]

    if (
        hp >= dp
        and hp >= ap
    ):
        conclusion = (
            f"🔵 **{team_name(home_team)} thắng**"
        )

    elif (
        ap >= hp
        and ap >= dp
    ):
        conclusion = (
            f"🔴 **{team_name(away_team)} thắng**"
        )

    else:
        conclusion = "🟡 **Hai đội hòa**"

    return {
        **result,
        "home": team_name(
            home_team
        ),
        "away": team_name(
            away_team
        ),
        "conclusion": conclusion,
        "home_games": home_stats["games"],
        "away_games": away_stats["games"],
    }


# ============================================================
# EMBED DESIGN
# ============================================================

def make_embed(
    title,
    description,
    color=0x5865F2,
):
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.now(
            timezone.utc
        ),
    )

    embed.set_footer(
        text=(
            "⚽ Đá-bóng-24h "
            "• Data provided by football-data.org"
        )
    )

    return embed


def account_embed(
    user,
):
    balance = get_balance(
        user.id
    )

    vip = get_vip(
        balance
    )

    next_level, threshold = next_vip(
        balance
    )

    if next_level is None:
        progress = (
            "👑 **VIP 10 MAX**\n"
            "M đã đạt cấp cao nhất."
        )

    else:
        need = max(
            0,
            threshold - balance
        )

        progress = (
            f"📈 Còn **{need:,}** "
            f"để lên **VIP {next_level}**\n"
            f"🎯 Mốc: **{threshold:,}**"
        )

    return make_embed(
        "👑 THÔNG TIN TÀI KHOẢN",
        (
            f"👤 {user.mention}\n\n"
            f"💰 Số dư: **{balance:,}**\n"
            f"{VIP_EMOJIS.get(vip, '⚽')} "
            f"**{vip_name(vip)}**\n\n"
            f"{progress}"
        ),
        0xF1C40F,
    )


def prediction_embed(
    match,
    prediction,
):
    competition = (
        match.get(
            "competition",
            {}
        ).get(
            "name",
            "Không rõ giải"
        )
    )

    score_h, score_a = prediction[
        "score"
    ]

    embed = make_embed(
        "🔮 SOI TRẬN • DỰ ĐOÁN",
        (
            f"🏟️ **{prediction['home']}**\n"
            f"          🆚\n"
            f"🏟️ **{prediction['away']}**"
        ),
        0x9B59B6,
    )

    embed.add_field(
        name="🏆 Giải",
        value=competition,
        inline=False,
    )

    embed.add_field(
        name="🕐 Thời gian",
        value=(
            f"{match_time(match.get('utcDate'))} "
            "(VN)"
        ),
        inline=True,
    )

    embed.add_field(
        name="📌 Trạng thái",
        value=match.get(
            "status",
            "SCHEDULED"
        ),
        inline=True,
    )

    embed.add_field(
        name="🎯 TỶ SỐ DỰ ĐOÁN",
        value=(
            f"# **{prediction['home']} "
            f"{score_h} - {score_a} "
            f"{prediction['away']}**"
        ),
        inline=False,
    )

    embed.add_field(
        name="📊 XÁC SUẤT",
        value=(
            f"🔵 {prediction['home']}: "
            f"**{prediction['home_prob'] * 100:.1f}%**\n"
            f"🟡 Hòa: "
            f"**{prediction['draw_prob'] * 100:.1f}%**\n"
            f"🔴 {prediction['away']}: "
            f"**{prediction['away_prob'] * 100:.1f}%**"
        ),
        inline=False,
    )

    embed.add_field(
        name="🏆 KẾT LUẬN",
        value=prediction[
            "conclusion"
        ],
        inline=False,
    )

    embed.add_field(
        name="🧮 DỮ LIỆU MÔ HÌNH",
        value=(
            f"Home xG: **{prediction['home_xg']:.2f}**\n"
            f"Away xG: **{prediction['away_xg']:.2f}**\n"
            f"📚 Dữ liệu: "
            f"{prediction['home_games']} trận gần nhất của chủ nhà "
            f"+ {prediction['away_games']} trận gần nhất của đội khách."
        ),
        inline=False,
    )

    embed.add_field(
        name="⚠️ LƯU Ý",
        value=(
            "Đây là **dự đoán do bot tự tính**, "
            "không phải tỷ số chính thức và không đảm bảo kết quả."
        ),
        inline=False,
    )

    return embed


# ============================================================
# VIP ANNOUNCEMENT
# ============================================================

async def announce_vip(
    guild,
    member,
    vip,
):
    possible_names = [
        "vip",
        "thong-bao",
        "thông-báo",
        "admin-log",
        "general",
        "chung",
    ]

    channel = None

    for name in possible_names:

        channel = discord.utils.find(
            lambda c:
                isinstance(
                    c,
                    discord.TextChannel
                )
                and c.name.lower()
                == name.lower(),
            guild.text_channels,
        )

        if channel:
            break

    if channel is None:

        for c in guild.text_channels:

            if (
                guild.me
                and c.permissions_for(
                    guild.me
                ).send_messages
            ):
                channel = c
                break

    if channel is None:
        return

    embed = make_embed(
        f"{VIP_EMOJIS.get(vip, '👑')} 🎉 THĂNG CẤP VIP {vip}",
        (
            f"🎊 Chúc mừng {member.mention}!\n\n"
            f"{VIP_EMOJIS.get(vip, '💎')} "
            f"M đã đạt **VIP {vip}**!\n\n"
            f"💰 Số dư: "
            f"**{get_balance(member.id):,}**\n\n"
            "🔥 Tiếp tục hoạt động để mở cấp tiếp theo!"
        ),
        0xF1C40F,
    )

    try:
        await channel.send(
            embed=embed
        )
    except Exception:
        pass


# ============================================================
# /VI
# ============================================================

@bot.tree.command(
    name="vi",
    description="Xem số dư và cấp VIP",
)
async def vi(
    interaction: discord.Interaction,
):
    await interaction.response.send_message(
        embed=account_embed(
            interaction.user
        ),
        ephemeral=True,
    )


# ============================================================
# /COMAT
# ============================================================

@bot.tree.command(
    name="comat",
    description="Nhận 50,000 coin mỗi ngày",
)
async def comat(
    interaction: discord.Interaction,
):
    user_id = interaction.user.id

    ensure_user(
        user_id
    )

    today = datetime.now(
        VN_TZ
    ).date().isoformat()

    conn = db()

    exists = conn.execute(
        """
        SELECT 1
        FROM daily_claims
        WHERE user_id = ?
        AND claim_date = ?
        """,
        (
            user_id,
            today,
        ),
    ).fetchone()

    if exists:
        conn.close()

        await interaction.response.send_message(
            "⏳ Hôm nay m đã nhận **50,000** rồi.\n"
            "Mai quay lại nhận tiếp nhé.",
            ephemeral=True,
        )
        return

    conn.execute(
        """
        INSERT INTO daily_claims
        (user_id, claim_date)
        VALUES (?, ?)
        """,
        (
            user_id,
            today,
        ),
    )

    conn.commit()
    conn.close()

    (
        old_balance,
        new_balance,
        old_vip,
        new_vip,
    ) = change_balance(
        user_id,
        DAILY_BONUS,
    )

    embed = make_embed(
        "🎁 NHẬN COIN HẰNG NGÀY",
        (
            f"✅ {interaction.user.mention}\n\n"
            f"💰 **+{DAILY_BONUS:,}**\n\n"
            f"💵 **{old_balance:,} → "
            f"{new_balance:,}**\n"
            f"{VIP_EMOJIS.get(new_vip, '⚽')} "
            f"**{vip_name(new_vip)}**"
        ),
        0x2ECC71,
    )

    if new_vip > old_vip:
        embed.add_field(
            name="👑🎉 THĂNG CẤP VIP",
            value=(
                f"Chúc mừng! M đã lên "
                f"**VIP {new_vip}**!"
            ),
            inline=False,
        )

    await interaction.response.send_message(
        embed=embed
    )

    if (
        new_vip > old_vip
        and interaction.guild
    ):
        await announce_vip(
            interaction.guild,
            interaction.user,
            new_vip,
        )


# ============================================================
# /KHOINGHIEP
# ============================================================

@bot.tree.command(
    name="khoinghiep",
    description="Nhận 1,000 coin khởi nghiệp một lần",
)
async def khoinghiep(
    interaction: discord.Interaction,
):
    user_id = interaction.user.id

    ensure_user(
        user_id
    )

    conn = db()

    row = conn.execute(
        """
        SELECT started
        FROM wallets
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()

    if row and int(
        row["started"]
    ) == 1:

        conn.close()

        await interaction.response.send_message(
            "❌ M đã nhận **1,000 coin khởi nghiệp** rồi.",
            ephemeral=True,
        )
        return

    conn.execute(
        """
        UPDATE wallets
        SET started = 1
        WHERE user_id = ?
        """,
        (user_id,),
    )

    conn.commit()
    conn.close()

    (
        old_balance,
        new_balance,
        old_vip,
        new_vip,
    ) = change_balance(
        user_id,
        STARTER_BONUS,
    )

    embed = make_embed(
        "🚀 KHỞI NGHIỆP",
        (
            f"🎉 {interaction.user.mention}\n\n"
            f"💰 **+{STARTER_BONUS:,}** coin\n\n"
            f"💵 **{old_balance:,} → "
            f"{new_balance:,}**\n"
            f"{VIP_EMOJIS.get(new_vip, '⚽')} "
            f"**{vip_name(new_vip)}**"
        ),
        0x3498DB,
    )

    await interaction.response.send_message(
        embed=embed
    )


# ============================================================
# ADMIN MODAL
# ============================================================

class AdminAmountModal(
    discord.ui.Modal
):
    def __init__(
        self,
        target,
        action,
    ):
        self.target = target
        self.action = action

        title = (
            "💰 CỘNG COIN"
            if action == "add"
            else "💸 TRỪ COIN"
        )

        super().__init__(
            title=title
        )

        self.amount = discord.ui.TextInput(
            label="Số coin",
            placeholder="Ví dụ: 100000",
            required=True,
            max_length=15,
        )

        self.add_item(
            self.amount
        )

    async def on_submit(
        self,
        interaction,
    ):
        try:
            amount = int(
                self.amount.value
                .replace(",", "")
                .replace(".", "")
                .strip()
            )

            if amount <= 0:
                raise ValueError

        except Exception:
            await interaction.response.send_message(
                "❌ Số coin không hợp lệ.",
                ephemeral=True,
            )
            return

        if self.action == "add":
            (
                old_balance,
                new_balance,
                old_vip,
                new_vip,
            ) = change_balance(
                self.target.id,
                amount,
            )

            title = "💰 ADMIN • CỘNG COIN"
            color = 0x2ECC71
            amount_text = f"+{amount:,}"

        else:
            (
                old_balance,
                new_balance,
                old_vip,
                new_vip,
            ) = change_balance(
                self.target.id,
                -amount,
            )

            title = "💸 ADMIN • TRỪ COIN"
            color = 0xE74C3C
            amount_text = f"-{amount:,}"

        embed = make_embed(
            title,
            (
                f"👤 {self.target.mention}\n\n"
                f"💰 Thay đổi: **{amount_text}**\n"
                f"💵 **{old_balance:,} → "
                f"{new_balance:,}**\n\n"
                f"{VIP_EMOJIS.get(new_vip, '⚽')} "
                f"**{vip_name(new_vip)}**"
            ),
            color,
        )

        if new_vip > old_vip:
            embed.add_field(
                name="👑🎉 THĂNG CẤP VIP",
                value=(
                    f"{self.target.mention} "
                    f"đã lên **VIP {new_vip}**!"
                ),
                inline=False,
            )

        await interaction.response.send_message(
            embed=embed
        )

        if (
            new_vip > old_vip
            and interaction.guild
        ):
            await announce_vip(
                interaction.guild,
                self.target,
                new_vip,
            )


class AdminPasswordModal(
    discord.ui.Modal
):
    def __init__(
        self,
        target,
    ):
        self.target = target

        super().__init__(
            title="🔐 XÁC THỰC ADMIN"
        )

        self.password = discord.ui.TextInput(
            label="Mật khẩu admin",
            placeholder="Nhập mật khẩu...",
            required=True,
            max_length=100,
        )

        self.add_item(
            self.password
        )

    async def on_submit(
        self,
        interaction,
    ):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "❌ M không có quyền Administrator.",
                ephemeral=True,
            )
            return

        if (
            self.password.value
            != ADMIN_PASSWORD
        ):
            await interaction.response.send_message(
                "❌ **Sai mật khẩu admin.**",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=make_embed(
                "👑 ADMIN • QUẢN LÝ COIN",
                (
                    f"👤 Người được chỉnh: "
                    f"{self.target.mention}\n\n"
                    "Chọn thao tác:"
                ),
                0xF1C40F,
            ),
            view=AdminActionView(
                self.target
            ),
            ephemeral=True,
        )


class AdminActionView(
    discord.ui.View
):
    def __init__(
        self,
        target,
    ):
        super().__init__(
            timeout=120
        )

        self.target = target

    @discord.ui.button(
        label="Cộng coin",
        emoji="💰",
        style=discord.ButtonStyle.success,
    )
    async def add_coin(
        self,
        interaction,
        button,
    ):
        await interaction.response.send_modal(
            AdminAmountModal(
                self.target,
                "add",
            )
        )

    @discord.ui.button(
        label="Trừ coin",
        emoji="💸",
        style=discord.ButtonStyle.danger,
    )
    async def remove_coin(
        self,
        interaction,
        button,
    ):
        await interaction.response.send_modal(
            AdminAmountModal(
                self.target,
                "remove",
            )
        )


# ============================================================
# /ADMIN
# ============================================================

@bot.tree.command(
    name="admin",
    description="Admin quản lý coin/VIP",
)
@app_commands.describe(
    member="Người cần chỉnh coin"
)
async def admin(
    interaction: discord.Interaction,
    member: discord.Member,
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "❌ Chỉ Administrator mới dùng được /admin.",
            ephemeral=True,
        )
        return

    await interaction.response.send_modal(
        AdminPasswordModal(
            member
        )
    )


# ============================================================
# BET AMOUNT
# ============================================================

class BetAmountModal(
    discord.ui.Modal
):
    def __init__(
        self,
        match,
        league_code,
    ):
        self.match = match
        self.league_code = league_code

        super().__init__(
            title="💰 NHẬP TIỀN CƯỢC"
        )

        self.amount = discord.ui.TextInput(
            label="Số tiền cược",
            placeholder="Ví dụ: 10000",
            required=True,
            max_length=15,
        )

        self.add_item(
            self.amount
        )

    async def on_submit(
        self,
        interaction,
    ):
        try:
            stake = int(
                self.amount.value
                .replace(",", "")
                .replace(".", "")
                .strip()
            )

            if stake <= 0:
                raise ValueError

        except Exception:
            await interaction.response.send_message(
                "❌ Số tiền cược không hợp lệ.",
                ephemeral=True,
            )
            return

        balance = get_balance(
            interaction.user.id
        )

        if stake > balance:
            await interaction.response.send_message(
                (
                    f"❌ Không đủ coin.\n"
                    f"💰 M đang có **{balance:,}**."
                ),
                ephemeral=True,
            )
            return

        home, away = match_names(
            self.match
        )

        embed = make_embed(
            "⚽ CHỌN KẾT QUẢ",
            (
                f"🏠 **{home}**\n"
                f"        🆚\n"
                f"✈️ **{away}**\n\n"
                f"💰 Tiền cược: **{stake:,}**\n\n"
                "Chọn kết quả:"
            ),
            0x3498DB,
        )

        await interaction.response.send_message(
            embed=embed,
            view=BetChoiceView(
                self.match,
                self.league_code,
                stake,
            ),
            ephemeral=True,
        )


# ============================================================
# BET CHOICE
# ============================================================

class BetChoiceView(
    discord.ui.View
):
    def __init__(
        self,
        match,
        league_code,
        stake,
    ):
        super().__init__(
            timeout=180
        )

        self.match = match
        self.league_code = league_code
        self.stake = stake

    async def place_bet(
        self,
        interaction,
        choice,
    ):
        user_id = interaction.user.id

        balance = get_balance(
            user_id
        )

        if self.stake > balance:
            await interaction.response.send_message(
                "❌ Số dư hiện tại không đủ.",
                ephemeral=True,
            )
            return

        fixture_id = int(
            self.match["id"]
        )

        conn = db()

        duplicate = conn.execute(
            """
            SELECT 1
            FROM bets
            WHERE user_id = ?
            AND fixture_id = ?
            AND choice = ?
            AND status = 'open'
            """,
            (
                user_id,
                fixture_id,
                choice,
            ),
        ).fetchone()

        if duplicate:
            conn.close()

            await interaction.response.send_message(
                "❌ M đã cược lựa chọn này cho trận đó rồi.",
                ephemeral=True,
            )
            return

        (
            old_balance,
            new_balance,
            old_vip,
            new_vip,
        ) = change_balance(
            user_id,
            -self.stake,
        )

        conn.execute(
            """
            INSERT INTO bets
            (
                user_id,
                fixture_id,
                league,
                choice,
                stake,
                status,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, 'open', ?)
            """,
            (
                user_id,
                fixture_id,
                self.league_code,
                choice,
                self.stake,
                datetime.now(
                    timezone.utc
                ).isoformat(),
            ),
        )

        conn.commit()
        conn.close()

        choices = {
            "HOME": "🔵 Chủ nhà thắng",
            "DRAW": "🟡 Hòa",
            "AWAY": "🔴 Đội khách thắng",
        }

        home, away = match_names(
            self.match
        )

        embed = make_embed(
            "✅ ĐẶT CƯỢC THÀNH CÔNG",
            (
                f"⚽ **{home}** 🆚 **{away}**\n\n"
                f"🎯 Lựa chọn: "
                f"**{choices[choice]}**\n"
                f"💰 Tiền cược: "
                f"**{self.stake:,}**\n"
                f"💵 Số dư: "
                f"**{new_balance:,}**\n"
                f"🏆 Nếu thắng nhận: "
                f"**{self.stake * BET_PAYOUT:,}**"
            ),
            0x2ECC71,
        )

        if new_vip > old_vip:
            embed.add_field(
                name="👑🎉 THĂNG CẤP VIP",
                value=(
                    f"M đã lên **VIP {new_vip}**!"
                ),
                inline=False,
            )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
        )

        if (
            new_vip > old_vip
            and interaction.guild
        ):
            await announce_vip(
                interaction.guild,
                interaction.user,
                new_vip,
            )

    @discord.ui.button(
        label="Chủ nhà",
        emoji="🔵",
        style=discord.ButtonStyle.primary,
    )
    async def home(
        self,
        interaction,
        button,
    ):
        await self.place_bet(
            interaction,
            "HOME",
        )

    @discord.ui.button(
        label="Hòa",
        emoji="🟡",
        style=discord.ButtonStyle.secondary,
    )
    async def draw(
        self,
        interaction,
        button,
    ):
        await self.place_bet(
            interaction,
            "DRAW",
        )

    @discord.ui.button(
        label="Đội khách",
        emoji="🔴",
        style=discord.ButtonStyle.danger,
    )
    async def away(
        self,
        interaction,
        button,
    ):
        await self.place_bet(
            interaction,
            "AWAY",
        )


# ============================================================
# MATCH SELECT
# ============================================================

class MatchSelect(
    discord.ui.Select
):
    def __init__(
        self,
        matches,
        league_code,
        mode,
    ):
        self.matches = matches
        self.league_code = league_code
        self.mode = mode

        options = []

        for index, match in enumerate(
            matches[:25]
        ):
            options.append(
                discord.SelectOption(
                    label=short_match_label(
                        match
                    ),
                    value=str(index),
                    description=(
                        match.get(
                            "competition",
                            {}
                        ).get(
                            "name",
                            "Football"
                        )
                    )[:100],
                )
            )

        if not options:
            options.append(
                discord.SelectOption(
                    label="Không có trận",
                    value="none",
                )
            )

        super().__init__(
            placeholder=(
                "⚽ Chọn trận để cược..."
                if mode == "bet"
                else "🔮 Chọn trận để soi..."
            ),
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(
        self,
        interaction,
    ):
        value = self.values[0]

        if value == "none":
            await interaction.response.send_message(
                "📭 Không có trận sắp tới.",
                ephemeral=True,
            )
            return

        match = self.matches[
            int(value)
        ]

        if self.mode == "bet":
            await interaction.response.send_modal(
                BetAmountModal(
                    match,
                    self.league_code,
                )
            )
            return

        # SOI
        await interaction.response.defer(
            ephemeral=True,
            thinking=True,
        )

        try:
            prediction = await predict_match(
                match
            )

            embed = prediction_embed(
                match,
                prediction,
            )

            await interaction.followup.send(
                embed=embed,
                ephemeral=True,
            )

        except Exception as e:
            await interaction.followup.send(
                (
                    "❌ Không thể dự đoán trận này.\n"
                    f"```{str(e)[:1200]}```"
                ),
                ephemeral=True,
            )


class MatchListView(
    discord.ui.View
):
    def __init__(
        self,
        matches,
        league_code,
        mode,
    ):
        super().__init__(
            timeout=300
        )

        self.add_item(
            MatchSelect(
                matches,
                league_code,
                mode,
            )
        )


# ============================================================
# MAIN FOOTBALL MENU
# ============================================================

class LeagueSelect(
    discord.ui.Select
):
    def __init__(
        self,
        mode,
    ):
        self.mode = mode

        options = []

        for code, info in LEAGUES.items():
            options.append(
                discord.SelectOption(
                    label=info["name"],
                    value=code,
                    emoji=info["emoji"],
                    description=(
                        "Chọn trận để cược"
                        if mode == "bet"
                        else "Xem lịch + dự đoán"
                    ),
                )
            )

        super().__init__(
            placeholder="🏆 Chọn giải đấu...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(
        self,
        interaction,
    ):
        code = self.values[0]

        if code not in FREE_COMPETITIONS:

            await interaction.response.send_message(
                embed=make_embed(
                    f"⚠️ {LEAGUES[code]['name']}",
                    (
                        "Giải này vẫn có trong menu 11 giải.\n\n"
                        "Nhưng **football-data.org Free Tier hiện "
                        "không cung cấp dữ liệu cho giải này**.\n\n"
                        "Bot không giả lịch/kết quả để tránh hiển thị sai."
                    ),
                    0xE67E22,
                ),
                ephemeral=True,
            )

            return

        await interaction.response.defer(
            ephemeral=True,
            thinking=True,
        )

        try:
            matches, error = await get_league_matches(
                code
            )

            if error:
                await interaction.followup.send(
                    error,
                    ephemeral=True,
                )
                return

            if not matches:

                await interaction.followup.send(
                    embed=make_embed(
                        f"{LEAGUES[code]['emoji']} "
                        f"{LEAGUES[code]['name']}",
                        "📭 Chưa có trận sắp tới.",
                        0x95A5A6,
                    ),
                    ephemeral=True,
                )

                return

            embed = make_embed(
                f"{LEAGUES[code]['emoji']} "
                f"{LEAGUES[code]['name']}",
                (
                    "⚽ Chọn một trận bên dưới.\n\n"
                    "📅 Hiển thị tối đa **25 trận**."
                ),
                (
                    0x3498DB
                    if self.mode == "bet"
                    else 0x9B59B6
                ),
            )

            await interaction.followup.send(
                embed=embed,
                view=MatchListView(
                    matches,
                    code,
                    self.mode,
                ),
                ephemeral=True,
            )

        except Exception as e:

            await interaction.followup.send(
                (
                    "❌ Không lấy được lịch.\n"
                    f"```{str(e)[:1200]}```"
                ),
                ephemeral=True,
            )


class ManCityButton(
    discord.ui.Button
):
    def __init__(
        self,
        mode,
    ):
        self.mode = mode

        super().__init__(
            label=(
                "Các trận Man City sắp tới"
                if mode == "bet"
                else "Man City — tất cả trận"
            ),
            emoji="🔵",
            style=discord.ButtonStyle.primary,
            row=1,
        )

    async def callback(
        self,
        interaction,
    ):
        await interaction.response.defer(
            ephemeral=True,
            thinking=True,
        )

        try:
            matches = await get_mancity_matches()

            if not matches:

                await interaction.followup.send(
                    embed=make_embed(
                        "🔵 MAN CITY",
                        "📭 Không có trận sắp tới trong dữ liệu.",
                        0x3498DB,
                    ),
                    ephemeral=True,
                )

                return

            embed = make_embed(
                "🔵 MAN CITY • LỊCH SẮP TỚI",
                (
                    "⚽ Chọn trận bên dưới.\n\n"
                    "📅 Tối đa **25 trận** được hiển thị.\n"
                    "⚠️ Chỉ các giải mà API Free cung cấp "
                    "mới xuất hiện."
                ),
                (
                    0x3498DB
                    if self.mode == "bet"
                    else 0x9B59B6
                ),
            )

            await interaction.followup.send(
                embed=embed,
                view=MatchListView(
                    matches,
                    "MCI",
                    self.mode,
                ),
                ephemeral=True,
            )

        except Exception as e:

            await interaction.followup.send(
                (
                    "❌ Không lấy được lịch Man City.\n"
                    f"```{str(e)[:1200]}```"
                ),
                ephemeral=True,
            )


class FootballMainView(
    discord.ui.View
):
    def __init__(
        self,
        mode,
    ):
        super().__init__(
            timeout=300
        )

        self.add_item(
            LeagueSelect(
                mode
            )
        )

        self.add_item(
            ManCityButton(
                mode
            )
        )


# ============================================================
# /CUOC
# ============================================================

@bot.tree.command(
    name="cuoc",
    description="Mở menu cá cược bóng đá",
)
async def cuoc(
    interaction: discord.Interaction,
):
    ensure_user(
        interaction.user.id
    )

    balance = get_balance(
        interaction.user.id
    )

    embed = make_embed(
        "⚽ ĐÁ-BÓNG-24H • CÁ CƯỢC",
        (
            "🏆 **CHỌN GIẢI** bên dưới để xem trận.\n\n"
            "🔵 **Man City** để xem các trận Man City.\n\n"
            "💰 Chọn trận → nhập tiền → "
            "chọn **Chủ nhà / Hòa / Đội khách**.\n\n"
            f"💵 Số dư của m: **{balance:,}**"
        ),
        0x3498DB,
    )

    await interaction.response.send_message(
        embed=embed,
        view=FootballMainView(
            "bet"
        ),
    )


# ============================================================
# /CADO
# Alias cũ
# ============================================================

@bot.tree.command(
    name="cado",
    description="Mở menu cá cược bóng đá",
)
async def cado(
    interaction: discord.Interaction,
):
    await cuoc.callback(
        interaction
    )


# ============================================================
# /SOI
# ============================================================

@bot.tree.command(
    name="soi",
    description="Soi và dự đoán tỷ số bóng đá",
)
async def soi(
    interaction: discord.Interaction,
):
    embed = make_embed(
        "🔮 ĐÁ-BÓNG-24H • SOI TRẬN",
        (
            "🏆 **CHỌN GIẢI** để xem trận sắp tới.\n\n"
            "🔵 **Man City — tất cả trận** để xem lịch Man City.\n\n"
            "🎯 Chọn trận → bot tính:\n"
            "• Tỷ số chính xác dự đoán\n"
            "• % chủ nhà thắng\n"
            "• % hòa\n"
            "• % đội khách thắng\n"
            "• Kết luận mô hình\n\n"
            "⚠️ Dự đoán do bot tự tính, không đảm bảo kết quả."
        ),
        0x9B59B6,
    )

    await interaction.response.send_message(
        embed=embed,
        view=FootballMainView(
            "soi"
        ),
    )


# ============================================================
# /KETTOAN
# ============================================================

@bot.tree.command(
    name="kettoan",
    description="Kết toán các cược đã kết thúc",
)
async def kettoan(
    interaction: discord.Interaction,
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "❌ Chỉ Administrator mới dùng được /kettoan.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(
        ephemeral=True,
        thinking=True,
    )

    conn = db()

    bets = conn.execute(
        """
        SELECT *
        FROM bets
        WHERE status = 'open'
        ORDER BY id ASC
        LIMIT 100
        """
    ).fetchall()

    conn.close()

    if not bets:

        await interaction.followup.send(
            "📭 Không có cược mở.",
            ephemeral=True,
        )

        return

    settled = 0
    wins = 0
    losses = 0
    pending = 0
    errors = 0

    for bet in bets:

        try:
            match = await get_match(
                int(
                    bet["fixture_id"]
                )
            )

            status = match.get(
                "status"
            )

            if status not in (
                "FINISHED",
                "AWARDED",
            ):
                pending += 1
                continue

            result = result_from_match(
                match
            )

            if result is None:
                pending += 1
                continue

            user_id = int(
                bet["user_id"]
            )

            choice = bet["choice"]
            stake = int(
                bet["stake"]
            )

            won = (
                result == choice
            )

            conn = db()

            if won:

                payout = (
                    stake
                    * BET_PAYOUT
                )

                old_balance = get_balance(
                    user_id
                )

                conn.execute(
                    """
                    UPDATE wallets
                    SET balance = balance + ?
                    WHERE user_id = ?
                    """,
                    (
                        payout,
                        user_id,
                    ),
                )

                conn.execute(
                    """
                    UPDATE bets
                    SET status = 'won'
                    WHERE id = ?
                    """,
                    (
                        bet["id"],
                    ),
                )

                conn.commit()
                conn.close()

                new_balance = (
                    old_balance
                    + payout
                )

                old_vip = get_vip(
                    old_balance
                )

                new_vip = get_vip(
                    new_balance
                )

                conn2 = db()

                conn2.execute(
                    """
                    UPDATE wallets
                    SET vip = ?
                    WHERE user_id = ?
                    """,
                    (
                        new_vip,
                        user_id,
                    ),
                )

                conn2.commit()
                conn2.close()

                wins += 1

                if (
                    new_vip > old_vip
                    and interaction.guild
                ):

                    member = (
                        interaction.guild
                        .get_member(
                            user_id
                        )
                    )

                    if member:
                        await announce_vip(
                            interaction.guild,
                            member,
                            new_vip,
                        )

            else:

                conn.execute(
                    """
                    UPDATE bets
                    SET status = 'lost'
                    WHERE id = ?
                    """,
                    (
                        bet["id"],
                    ),
                )

                conn.commit()
                conn.close()

                losses += 1

            settled += 1

        except Exception:
            errors += 1

    embed = make_embed(
        "🧾 KẾT TOÁN HOÀN TẤT",
        (
            f"✅ Đã xử lý: **{settled}**\n"
            f"🏆 Thắng: **{wins}**\n"
            f"❌ Thua: **{losses}**\n"
            f"⏳ Chưa kết thúc: **{pending}**\n"
            f"⚠️ Lỗi: **{errors}**"
        ),
        0x2ECC71,
    )

    await interaction.followup.send(
        embed=embed,
        ephemeral=True,
    )


# ============================================================
# /PING
# ============================================================

@bot.tree.command(
    name="ping",
    description="Kiểm tra bot",
)
async def ping(
    interaction: discord.Interaction,
):
    latency = round(
        bot.latency * 1000
    )

    await interaction.response.send_message(
        f"🏓 Pong! **{latency}ms**",
        ephemeral=True,
    )


# ============================================================
# /APIQUOTA
# ============================================================

@bot.tree.command(
    name="apiquota",
    description="Xem giới hạn API",
)
async def apiquota(
    interaction: discord.Interaction,
):
    embed = make_embed(
        "📡 FOOTBALL API",
        (
            "⚽ Nguồn: **football-data.org API v4**\n\n"
            "🆓 Free Tier:\n"
            "• 10 requests/phút\n"
            "• Fixtures\n"
            "• Schedules\n"
            "• League tables\n\n"
            "⚠️ FA Cup + Carabao Cup hiện không nằm "
            "trong Free Tier."
        ),
        0x3498DB,
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
    )


# ============================================================
# RAILWAY HEALTH SERVER
# ============================================================

async def health(
    request,
):
    return web.Response(
        text="Đá-bóng-24h bot is online.",
        content_type="text/plain",
    )


async def start_health_server():
    global health_runner

    port = int(
        os.getenv(
            "PORT",
            "8080"
        )
    )

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

    site = web.TCPSite(
        health_runner,
        "0.0.0.0",
        port,
    )

    await site.start()

    print(
        f"[WEB] Health server: 0.0.0.0:{port}"
    )


# ============================================================
# BOT READY
# ============================================================

@bot.event
async def on_ready():

    init_db()

    try:

        if GUILD_ID:

            guild = discord.Object(
                id=int(GUILD_ID)
            )

            bot.tree.copy_global_to(
                guild=guild
            )

            synced = await bot.tree.sync(
                guild=guild
            )

            print(
                f"[DISCORD] Synced "
                f"{len(synced)} guild commands."
            )

        else:

            synced = await bot.tree.sync()

            print(
                f"[DISCORD] Synced "
                f"{len(synced)} global commands."
            )

    except Exception as e:

        print(
            f"[DISCORD] Sync error: {e}"
        )

    print(
        f"[DISCORD] Logged in as "
        f"{bot.user} ({bot.user.id})"
    )


# ============================================================
# CLOSE
# ============================================================

async def close_resources():

    global http_session

    if (
        http_session
        and not http_session.closed
    ):
        await http_session.close()

    if health_runner:
        await health_runner.cleanup()


# ============================================================
# MAIN
# ============================================================

async def main():

    if not DISCORD_TOKEN:
        raise RuntimeError(
            "Thiếu DISCORD_TOKEN trên Railway."
        )

    if not FOOTBALL_DATA_API_KEY:
        raise RuntimeError(
            "Thiếu FOOTBALL_DATA_API_KEY trên Railway."
        )

    init_db()

    await start_health_server()

    try:
        await bot.start(
            DISCORD_TOKEN
        )

    finally:
        await close_resources()


if __name__ == "__main__":

    try:
        asyncio.run(
            main()
        )

    except KeyboardInterrupt:
        pass

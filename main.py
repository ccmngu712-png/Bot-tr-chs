import os
import sqlite3
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

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

# Railway Variables:
# FOOTBALL_DATA_API_KEY = key mới của football-data.org
FOOTBALL_DATA_API_KEY = (
    os.getenv("FOOTBALL_DATA_API_KEY", "").strip()
    or os.getenv("FOOTBALL_DATA_TOKEN", "").strip()
    or os.getenv("FOOTBALL_DATA_KEY", "").strip()
)

API_BASE = "https://api.football-data.org/v4"

# Giờ Việt Nam
TIMEZONE = timezone(timedelta(hours=7))

STARTING_BALANCE = 100_000
STARTER_BONUS = 1_000
DAILY_BONUS = 50_000

BET_PAYOUT = 2

# Manchester City trên football-data.org
MANCHESTER_CITY_TEAM_ID = 65

# Database
if os.path.isdir("/data"):
    DB_PATH = "/data/football_bot.db"
else:
    DB_PATH = "football_bot.db"


# ============================================================
# LEAGUES
# ============================================================

LEAGUES = {
    "PL": ("🏴", "Premier League"),
    "CL": ("🏆", "UEFA Champions League"),
    "PD": ("🇪🇸", "La Liga"),
    "SA": ("🇮🇹", "Serie A"),
    "BL1": ("🇩🇪", "Bundesliga"),
    "FL1": ("🇫🇷", "Ligue 1"),
    "DED": ("🇳🇱", "Eredivisie"),
    "PPL": ("🇵🇹", "Primeira Liga"),
    "ELC": ("🏴", "Championship"),
    "FAC": ("🏆", "FA Cup"),
    "ELCUP": ("🏆", "Carabao Cup"),
}

# Free Tier hiện có các giải này
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
]


def get_vip(balance: int) -> int:
    for level, required in VIP_LEVELS:
        if balance >= required:
            return level
    return 0


def vip_text(level: int) -> str:
    if level <= 0:
        return "👤 Regular"
    return f"💎 VIP {level}"


def next_vip(balance: int):
    for level, required in reversed(VIP_LEVELS):
        if balance < required:
            return level, required
    return None


# ============================================================
# DISCORD
# ============================================================

intents = discord.Intents.default()

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
)


# ============================================================
# GLOBAL HTTP SESSION
# ============================================================

http_session: Optional[aiohttp.ClientSession] = None

health_runner = None

# API cache
API_CACHE = {}

# Cache 60 giây để tránh vượt 10 requests/phút
CACHE_SECONDS = 60


# ============================================================
# DATABASE
# ============================================================

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
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

    # Migration database cũ
    try:
        cur.execute(
            "ALTER TABLE bets ADD COLUMN league TEXT NOT NULL DEFAULT ''"
        )
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()


def ensure_wallet(user_id: int):
    conn = get_db()

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


def get_wallet(user_id: int):
    ensure_wallet(user_id)

    conn = get_db()

    row = conn.execute(
        """
        SELECT *
        FROM wallets
        WHERE user_id=?
        """,
        (user_id,),
    ).fetchone()

    conn.close()

    return row


def change_balance(user_id: int, amount: int):
    ensure_wallet(user_id)

    conn = get_db()

    row = conn.execute(
        """
        SELECT balance, vip
        FROM wallets
        WHERE user_id=?
        """,
        (user_id,),
    ).fetchone()

    old_balance = int(row["balance"])
    old_vip = int(row["vip"])

    new_balance = max(
        0,
        old_balance + amount,
    )

    new_vip = get_vip(new_balance)

    conn.execute(
        """
        UPDATE wallets
        SET balance=?, vip=?
        WHERE user_id=?
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
# VIP ANNOUNCEMENT
# ============================================================

async def announce_vip(
    member: Optional[discord.Member],
    old_vip: int,
    new_vip: int,
):
    if member is None:
        return

    if new_vip <= old_vip:
        return

    guild = member.guild

    channel = guild.system_channel

    if channel is None:
        me = guild.me

        if me is not None:
            for ch in guild.text_channels:
                if ch.permissions_for(me).send_messages:
                    channel = ch
                    break

    if channel is None:
        return

    await channel.send(
        f"🎉 **THÔNG BÁO VIP** 🎉\n"
        f"🔥 Chúc mừng {member.mention} đã đạt "
        f"**{vip_text(new_vip)}**!\n"
        f"💎 Cấp VIP mới đã được lưu vĩnh viễn."
    )


# ============================================================
# API CACHE
# ============================================================

def cache_get(key):
    item = API_CACHE.get(key)

    if item is None:
        return None

    saved_time, value = item

    now = asyncio.get_running_loop().time()

    if now - saved_time > CACHE_SECONDS:
        API_CACHE.pop(key, None)
        return None

    return value


def cache_set(key, value):
    API_CACHE[key] = (
        asyncio.get_running_loop().time(),
        value,
    )


# ============================================================
# FOOTBALL-DATA.ORG REQUEST
# ============================================================

async def api_get(
    endpoint: str,
    params=None,
    cache_key=None,
):
    global http_session

    if not FOOTBALL_DATA_API_KEY:
        raise RuntimeError(
            "❌ Chưa có `FOOTBALL_DATA_API_KEY` trên Railway."
        )

    if cache_key:
        cached = cache_get(cache_key)

        if cached is not None:
            return cached

    if http_session is None or http_session.closed:
        http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20)
        )

    headers = {
        "X-Auth-Token": FOOTBALL_DATA_API_KEY,
        "Accept": "application/json",
    }

    url = f"{API_BASE}{endpoint}"

    async with http_session.get(
        url,
        headers=headers,
        params=params or {},
    ) as response:

        text = await response.text()

        if response.status == 401:
            raise RuntimeError(
                "❌ API key không hợp lệ. "
                "Kiểm tra FOOTBALL_DATA_API_KEY trên Railway."
            )

        if response.status == 403:
            raise RuntimeError(
                "❌ football-data.org từ chối dữ liệu này. "
                "Giải hoặc dữ liệu này không nằm trong quyền của gói hiện tại."
            )

        if response.status == 429:
            raise RuntimeError(
                "⏳ API đang giới hạn 10 requests/phút. "
                "Chờ khoảng 1 phút rồi thử lại."
            )

        if response.status >= 400:
            try:
                data = await response.json()
            except Exception:
                data = text[:500]

            raise RuntimeError(
                f"❌ API HTTP {response.status}: {data}"
            )

        try:
            data = await response.json()
        except Exception:
            raise RuntimeError(
                "❌ API trả về dữ liệu không hợp lệ."
            )

    if cache_key:
        cache_set(
            cache_key,
            data,
        )

    return data


# ============================================================
# GET COMPETITION MATCHES
# ============================================================

async def get_league_matches(
    league_code: str,
    days: int = 60,
):
    if league_code not in FREE_COMPETITIONS:
        raise RuntimeError(
            f"⚠️ **{LEAGUES[league_code][1]}** "
            f"không nằm trong Free Tier hiện tại của "
            f"football-data.org.\n\n"
            f"Token Free không thể lấy lịch giải này."
        )

    today = datetime.now(TIMEZONE).date()

    end_date = today + timedelta(
        days=days
    )

    cache_key = (
        f"league:"
        f"{league_code}:"
        f"{today}:"
        f"{end_date}"
    )

    data = await api_get(
        f"/competitions/{league_code}/matches",
        params={
            "dateFrom": today.isoformat(),
            "dateTo": end_date.isoformat(),
            "status": "SCHEDULED",
        },
        cache_key=cache_key,
    )

    matches = data.get(
        "matches",
        [],
    )

    matches.sort(
        key=lambda m: m.get(
            "utcDate",
            ""
        )
    )

    return matches


# ============================================================
# MAN CITY
# ============================================================

async def get_mancity_matches(
    days: int = 120,
):
    today = datetime.now(TIMEZONE).date()

    end_date = today + timedelta(
        days=days
    )

    cache_key = (
        f"mancity:"
        f"{today}:"
        f"{end_date}"
    )

    data = await api_get(
        f"/teams/{MANCHESTER_CITY_TEAM_ID}/matches",
        params={
            "dateFrom": today.isoformat(),
            "dateTo": end_date.isoformat(),
            "status": "SCHEDULED",
            "limit": 100,
        },
        cache_key=cache_key,
    )

    matches = data.get(
        "matches",
        [],
    )

    matches.sort(
        key=lambda m: m.get(
            "utcDate",
            ""
        )
    )

    return matches


# ============================================================
# SINGLE MATCH
# ============================================================

async def get_match(match_id: int):
    return await api_get(
        f"/matches/{int(match_id)}",
        cache_key=f"match:{int(match_id)}",
    )


# ============================================================
# MATCH HELPERS
# ============================================================

def convert_time(utc_date: str):
    if not utc_date:
        return None

    try:
        dt = datetime.fromisoformat(
            utc_date.replace(
                "Z",
                "+00:00",
            )
        )

        return dt.astimezone(
            TIMEZONE
        )

    except Exception:
        return None


def match_name(match: dict):
    home = (
        match.get(
            "homeTeam",
            {}
        ).get("shortName")
        or
        match.get(
            "homeTeam",
            {}
        ).get("name")
        or "?"
    )

    away = (
        match.get(
            "awayTeam",
            {}
        ).get("shortName")
        or
        match.get(
            "awayTeam",
            {}
        ).get("name")
        or "?"
    )

    dt = convert_time(
        match.get("utcDate")
    )

    if dt:
        time_text = dt.strftime(
            "%d/%m %H:%M"
        )
    else:
        time_text = "Chưa rõ giờ"

    return (
        f"{time_text} • "
        f"{home} - {away}"
    )


def competition_name(match: dict):
    return (
        match.get(
            "competition",
            {}
        ).get(
            "name",
            "Football"
        )
    )


def get_result(match: dict):
    status = match.get(
        "status"
    )

    if status not in {
        "FINISHED",
        "AWARDED",
    }:
        return None

    score = match.get(
        "score",
        {}
    )

    full_time = score.get(
        "fullTime",
        {}
    )

    home = full_time.get(
        "home"
    )

    away = full_time.get(
        "away"
    )

    if home is None or away is None:
        return None

    home = int(home)
    away = int(away)

    if home > away:
        outcome = "home"

    elif away > home:
        outcome = "away"

    else:
        outcome = "draw"

    return (
        outcome,
        home,
        away,
    )


# ============================================================
# SOI TRẬN
# ============================================================

def build_analysis_embed(
    match: dict,
):
    home = (
        match.get(
            "homeTeam",
            {}
        ).get(
            "shortName",
            "?"
        )
    )

    away = (
        match.get(
            "awayTeam",
            {}
        ).get(
            "shortName",
            "?"
        )
    )

    comp = competition_name(
        match
    )

    dt = convert_time(
        match.get("utcDate")
    )

    embed = discord.Embed(
        title=f"🔎 {home} vs {away}",
    )

    embed.add_field(
        name="🏆 Giải",
        value=comp,
        inline=False,
    )

    if dt:
        embed.add_field(
            name="🕒 Thời gian",
            value=(
                dt.strftime(
                    "%d/%m/%Y %H:%M"
                )
                + " (VN)"
            ),
            inline=False,
        )

    status = match.get(
        "status",
        "?"
    )

    embed.add_field(
        name="📌 Trạng thái",
        value=status,
        inline=False,
    )

    result = get_result(
        match
    )

    if result:
        outcome, hs, aws = result

        if outcome == "home":
            outcome_text = f"{home} thắng"

        elif outcome == "away":
            outcome_text = f"{away} thắng"

        else:
            outcome_text = "Hòa"

        embed.add_field(
            name="⚽ Kết quả",
            value=(
                f"**{hs} - {aws}**\n"
                f"{outcome_text}"
            ),
            inline=False,
        )

    # football-data.org Free không cung cấp
    # model xác suất / exact-score prediction.
    embed.add_field(
        name="🤖 Phân tích",
        value=(
            "Nguồn football-data.org cung cấp "
            "lịch và kết quả.\n"
            "API không trả về xác suất thắng/hòa/thua "
            "hoặc tỷ số dự đoán cho trận này."
        ),
        inline=False,
    )

    embed.set_footer(
        text="Nguồn dữ liệu: football-data.org"
    )

    return embed


# ============================================================
# LEAGUE SELECT
# ============================================================

class LeagueSelect(discord.ui.Select):

    def __init__(
        self,
        mode: str,
    ):
        self.mode = mode

        options = []

        for code, (
            flag,
            name,
        ) in LEAGUES.items():

            options.append(
                discord.SelectOption(
                    label=name,
                    value=code,
                    emoji=flag,
                    description=(
                        "Xem các trận sắp tới"
                    ),
                )
            )

        super().__init__(
            placeholder="⚽ Chọn giải đấu...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(
        self,
        interaction: discord.Interaction,
    ):
        league_code = self.values[0]

        await interaction.response.defer(
            ephemeral=True
        )

        try:
            matches = await get_league_matches(
                league_code
            )

        except Exception as e:
            await interaction.followup.send(
                str(e),
                ephemeral=True,
            )
            return

        if not matches:
            await interaction.followup.send(
                f"📭 Không có trận sắp tới "
                f"trong khoảng dữ liệu hiện tại của "
                f"**{LEAGUES[league_code][1]}**.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"📅 **{LEAGUES[league_code][1]}**\n"
            f"Chọn trận bên dưới:",
            view=MatchListView(
                matches,
                self.mode,
                league_code,
            ),
            ephemeral=True,
        )


# ============================================================
# CITY BUTTON
# ============================================================

class ManCityButton(
    discord.ui.Button
):

    def __init__(
        self,
        mode: str,
    ):
        self.mode = mode

        super().__init__(
            label=(
                "Các trận Man City sắp tới"
                if mode == "bet"
                else
                "Man City — tất cả trận"
            ),
            emoji="🔵",
            style=discord.ButtonStyle.primary,
            row=1,
        )

    async def callback(
        self,
        interaction: discord.Interaction,
    ):
        await interaction.response.defer(
            ephemeral=True
        )

        try:
            matches = await get_mancity_matches()

        except Exception as e:
            await interaction.followup.send(
                str(e),
                ephemeral=True,
            )
            return

        if not matches:
            await interaction.followup.send(
                "📭 Không có trận Man City "
                "trong khoảng thời gian hiện tại.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            "🔵 **MANCHESTER CITY**\n"
            "Các trận sắp tới:",
            view=MatchListView(
                matches,
                self.mode,
                None,
            ),
            ephemeral=True,
        )


# ============================================================
# MAIN FOOTBALL VIEW
# ============================================================

class FootballMainView(
    discord.ui.View
):

    def __init__(
        self,
        mode: str,
    ):
        super().__init__(
            timeout=180
        )

        self.add_item(
            LeagueSelect(mode)
        )

        self.add_item(
            ManCityButton(mode)
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
        mode: str,
        league: Optional[str],
    ):
        self.matches = matches
        self.mode = mode
        self.league = league

        options = []

        # Discord select tối đa 25 option.
        for match in matches[:25]:

            description = (
                competition_name(
                    match
                )
            )

            options.append(
                discord.SelectOption(
                    label=match_name(
                        match
                    )[:100],
                    value=str(
                        match["id"]
                    ),
                    description=description[:100],
                )
            )

        super().__init__(
            placeholder="⚽ Chọn trận...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(
        self,
        interaction: discord.Interaction,
    ):
        match_id = int(
            self.values[0]
        )

        selected = None

        for match in self.matches:

            if int(match["id"]) == match_id:
                selected = match
                break

        if selected is None:
            await interaction.response.send_message(
                "❌ Không tìm thấy trận.",
                ephemeral=True,
            )
            return

        # ================================
        # CUOC
        # ================================

        if self.mode == "bet":

            await interaction.response.send_message(
                f"⚽ **{match_name(selected)}**\n"
                f"🏆 {competition_name(selected)}\n\n"
                f"Nhấn nút để nhập tiền cược:",
                view=BetAmountView(
                    selected,
                    self.league,
                ),
                ephemeral=True,
            )

            return

        # ================================
        # SOI
        # ================================

        await interaction.response.defer(
            ephemeral=True
        )

        try:
            fresh = await get_match(
                match_id
            )

        except Exception:
            fresh = selected

        embed = build_analysis_embed(
            fresh
        )

        await interaction.followup.send(
            embed=embed,
            ephemeral=True,
        )


class MatchListView(
    discord.ui.View
):

    def __init__(
        self,
        matches,
        mode,
        league,
    ):
        super().__init__(
            timeout=180
        )

        self.add_item(
            MatchSelect(
                matches,
                mode,
                league,
            )
        )


# ============================================================
# BET AMOUNT
# ============================================================

class BetAmountModal(
    discord.ui.Modal,
    title="Nhập tiền cược",
):

    amount = discord.ui.TextInput(
        label="Số tiền cược",
        placeholder="Ví dụ: 10000",
        required=True,
        min_length=1,
        max_length=15,
    )

    def __init__(
        self,
        match,
        league,
    ):
        super().__init__()

        self.match = match
        self.league = league

    async def on_submit(
        self,
        interaction: discord.Interaction,
    ):
        raw = str(
            self.amount.value
        ).strip()

        raw = raw.replace(
            ",",
            ""
        )

        raw = raw.replace(
            ".",
            ""
        )

        try:
            amount = int(raw)

        except ValueError:
            await interaction.response.send_message(
                "❌ Số tiền không hợp lệ.",
                ephemeral=True,
            )
            return

        if amount <= 0:
            await interaction.response.send_message(
                "❌ Số tiền phải lớn hơn 0.",
                ephemeral=True,
            )
            return

        wallet = get_wallet(
            interaction.user.id
        )

        balance = int(
            wallet["balance"]
        )

        if amount > balance:
            await interaction.response.send_message(
                f"❌ Không đủ tiền.\n"
                f"💰 Ví hiện có: **{balance:,}**",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"⚽ **{match_name(self.match)}**\n"
            f"💰 Tiền cược: **{amount:,}**\n\n"
            f"Chọn cửa:",
            view=BetChoiceView(
                self.match,
                self.league,
                amount,
            ),
            ephemeral=True,
        )


class BetAmountButton(
    discord.ui.Button
):

    def __init__(
        self,
        match,
        league,
    ):
        self.match = match
        self.league = league

        super().__init__(
            label="Nhập tiền cược",
            emoji="💰",
            style=discord.ButtonStyle.success,
        )

    async def callback(
        self,
        interaction: discord.Interaction,
    ):
        await interaction.response.send_modal(
            BetAmountModal(
                self.match,
                self.league,
            )
        )


class BetAmountView(
    discord.ui.View
):

    def __init__(
        self,
        match,
        league,
    ):
        super().__init__(
            timeout=180
        )

        self.add_item(
            BetAmountButton(
                match,
                league,
            )
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
        league,
        amount,
    ):
        super().__init__(
            timeout=180
        )

        self.add_item(
            BetChoiceButton(
                "🏠 Chủ nhà",
                "home",
                match,
                league,
                amount,
            )
        )

        self.add_item(
            BetChoiceButton(
                "🤝 Hòa",
                "draw",
                match,
                league,
                amount,
            )
        )

        self.add_item(
            BetChoiceButton(
                "✈️ Đội khách",
                "away",
                match,
                league,
                amount,
            )
        )


class BetChoiceButton(
    discord.ui.Button
):

    def __init__(
        self,
        label,
        choice,
        match,
        league,
        amount,
    ):
        self.choice = choice
        self.match = match
        self.league = league
        self.amount = amount

        super().__init__(
            label=label,
            style=discord.ButtonStyle.primary,
        )

    async def callback(
        self,
        interaction: discord.Interaction,
    ):
        user_id = interaction.user.id

        ensure_wallet(
            user_id
        )

        conn = get_db()

        wallet = conn.execute(
            """
            SELECT balance
            FROM wallets
            WHERE user_id=?
            """,
            (user_id,),
        ).fetchone()

        if wallet is None:
            conn.close()

            await interaction.response.send_message(
                "❌ Không tìm thấy ví.",
                ephemeral=True,
            )
            return

        balance = int(
            wallet["balance"]
        )

        if balance < self.amount:
            conn.close()

            await interaction.response.send_message(
                "❌ Số dư không đủ.",
                ephemeral=True,
            )
            return

        # Không cho đặt 2 cược mở cùng một trận
        existing = conn.execute(
            """
            SELECT id
            FROM bets
            WHERE user_id=?
              AND fixture_id=?
              AND status='open'
            """,
            (
                user_id,
                int(self.match["id"]),
            ),
        ).fetchone()

        if existing:
            conn.close()

            await interaction.response.send_message(
                "❌ M đã có cược đang mở "
                "cho trận này rồi.",
                ephemeral=True,
            )
            return

        # Trừ tiền
        conn.execute(
            """
            UPDATE wallets
            SET balance=balance-?
            WHERE user_id=?
            """,
            (
                self.amount,
                user_id,
            ),
        )

        # Lưu cược
        league_code = (
            self.league
            or
            self.match.get(
                "competition",
                {}
            ).get(
                "code",
                ""
            )
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
                int(self.match["id"]),
                league_code,
                self.choice,
                self.amount,
                datetime.now(
                    timezone.utc
                ).isoformat(),
            ),
        )

        conn.commit()
        conn.close()

        new_wallet = get_wallet(
            user_id
        )

        await interaction.response.send_message(
            f"✅ **ĐẶT CƯỢC THÀNH CÔNG**\n\n"
            f"⚽ {match_name(self.match)}\n"
            f"🏆 {competition_name(self.match)}\n"
            f"🎯 Cửa: **{self.choice}**\n"
            f"💰 Cược: **{self.amount:,}**\n"
            f"💳 Còn lại: "
            f"**{int(new_wallet['balance']):,}**",
            ephemeral=True,
        )


# ============================================================
# /CUOC
# ============================================================

@bot.tree.command(
    name="cuoc",
    description="Mở menu cược bóng đá",
)
async def cuoc(
    interaction: discord.Interaction,
):
    ensure_wallet(
        interaction.user.id
    )

    embed = discord.Embed(
        title="⚽ CƯỢC BÓNG ĐÁ",
        description=(
            "Chọn một trong 11 giải đấu "
            "hoặc chọn Man City.\n\n"
            "💰 Chọn trận → nhập tiền → "
            "Chủ nhà / Hòa / Đội khách."
        ),
    )

    await interaction.response.send_message(
        embed=embed,
        view=FootballMainView(
            "bet"
        ),
        ephemeral=True,
    )


# ============================================================
# /SOI
# ============================================================

@bot.tree.command(
    name="soi",
    description="Xem thông tin trận bóng",
)
async def soi(
    interaction: discord.Interaction,
):
    embed = discord.Embed(
        title="🔎 SOI TRẬN",
        description=(
            "Chọn giải đấu hoặc Man City "
            "để xem lịch trận.\n\n"
            "📌 Dữ liệu lịch/kết quả lấy trực tiếp "
            "từ football-data.org."
        ),
    )

    await interaction.response.send_message(
        embed=embed,
        view=FootballMainView(
            "soi"
        ),
        ephemeral=True,
    )


# ============================================================
# /VI
# ============================================================

@bot.tree.command(
    name="vi",
    description="Xem ví và VIP",
)
async def vi(
    interaction: discord.Interaction,
):
    wallet = get_wallet(
        interaction.user.id
    )

    balance = int(
        wallet["balance"]
    )

    vip = int(
        wallet["vip"]
    )

    nxt = next_vip(
        balance
    )

    if nxt:
        next_text = (
            f"📈 Còn **{nxt[1] - balance:,}** "
            f"để lên VIP {nxt[0]}"
        )
    else:
        next_text = (
            "👑 Đã đạt VIP 10"
        )

    embed = discord.Embed(
        title="💳 VÍ CỦA BẠN",
        description=(
            f"💰 Số dư: **{balance:,}**\n"
            f"{vip_text(vip)}\n\n"
            f"{next_text}"
        ),
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
    )


# ============================================================
# /COMAT
# ============================================================

@bot.tree.command(
    name="comat",
    description="Nhận 50.000 xu mỗi ngày",
)
async def comat(
    interaction: discord.Interaction,
):
    user_id = interaction.user.id

    ensure_wallet(
        user_id
    )

    today = datetime.now(
        TIMEZONE
    ).date().isoformat()

    conn = get_db()

    exists = conn.execute(
        """
        SELECT 1
        FROM daily_claims
        WHERE user_id=?
          AND claim_date=?
        """,
        (
            user_id,
            today,
        ),
    ).fetchone()

    if exists:
        conn.close()

        await interaction.response.send_message(
            "❌ Hôm nay m đã nhận **50.000** rồi.",
            ephemeral=True,
        )
        return

    row = conn.execute(
        """
        SELECT balance, vip
        FROM wallets
        WHERE user_id=?
        """,
        (user_id,),
    ).fetchone()

    old_balance = int(
        row["balance"]
    )

    old_vip = int(
        row["vip"]
    )

    new_balance = (
        old_balance
        + DAILY_BONUS
    )

    new_vip = get_vip(
        new_balance
    )

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

    conn.execute(
        """
        UPDATE wallets
        SET balance=?, vip=?
        WHERE user_id=?
        """,
        (
            new_balance,
            new_vip,
            user_id,
        ),
    )

    conn.commit()
    conn.close()

    await interaction.response.send_message(
        f"🎁 Nhận **+{DAILY_BONUS:,}** thành công!\n"
        f"💰 Ví: **{new_balance:,}**\n"
        f"{vip_text(new_vip)}",
        ephemeral=True,
    )

    if new_vip > old_vip:
        member = (
            interaction.user
            if isinstance(
                interaction.user,
                discord.Member
            )
            else None
        )

        await announce_vip(
            member,
            old_vip,
            new_vip,
        )


# ============================================================
# /KHOINGHIEP
# ============================================================

@bot.tree.command(
    name="khoinghiep",
    description="Nhận 1.000 xu khởi nghiệp",
)
async def khoinghiep(
    interaction: discord.Interaction,
):
    user_id = interaction.user.id

    wallet = get_wallet(
        user_id
    )

    if int(wallet["started"]) == 1:
        await interaction.response.send_message(
            "❌ M đã nhận tiền khởi nghiệp rồi.",
            ephemeral=True,
        )
        return

    old_balance = int(
        wallet["balance"]
    )

    old_vip = int(
        wallet["vip"]
    )

    new_balance = (
        old_balance
        + STARTER_BONUS
    )

    new_vip = get_vip(
        new_balance
    )

    conn = get_db()

    conn.execute(
        """
        UPDATE wallets
        SET balance=?, vip=?, started=1
        WHERE user_id=?
        """,
        (
            new_balance,
            new_vip,
            user_id,
        ),
    )

    conn.commit()
    conn.close()

    await interaction.response.send_message(
        f"🚀 Nhận **+{STARTER_BONUS:,}**!\n"
        f"💰 Ví: **{new_balance:,}**\n"
        f"{vip_text(new_vip)}",
        ephemeral=True,
    )

    if new_vip > old_vip:
        member = (
            interaction.user
            if isinstance(
                interaction.user,
                discord.Member
            )
            else None
        )

        await announce_vip(
            member,
            old_vip,
            new_vip,
        )


# ============================================================
# /ADMIN
# ============================================================

@bot.tree.command(
    name="admin",
    description="Admin cộng hoặc trừ tiền",
)
@app_commands.describe(
    user="Người cần chỉnh tiền",
    amount="Dương = cộng, âm = trừ",
)
async def admin(
    interaction: discord.Interaction,
    user: discord.Member,
    amount: int,
):
    if not (
        interaction.user.guild_permissions.administrator
        or
        interaction.user.guild_permissions.manage_guild
    ):
        await interaction.response.send_message(
            "❌ M cần quyền Administrator "
            "hoặc Manage Server.",
            ephemeral=True,
        )
        return

    if amount == 0:
        await interaction.response.send_message(
            "❌ Amount không được bằng 0.",
            ephemeral=True,
        )
        return

    (
        old_balance,
        new_balance,
        old_vip,
        new_vip,
    ) = change_balance(
        user.id,
        amount,
    )

    action = (
        "cộng"
        if amount > 0
        else
        "trừ"
    )

    await interaction.response.send_message(
        f"✅ Đã **{action} {abs(amount):,}** "
        f"cho {user.mention}\n\n"
        f"💰 {old_balance:,} → "
        f"{new_balance:,}\n"
        f"{vip_text(new_vip)}",
        ephemeral=True,
    )

    if new_vip > old_vip:
        await announce_vip(
            user,
            old_vip,
            new_vip,
        )


# ============================================================
# /KETTOAN
# ============================================================

@bot.tree.command(
    name="kettoan",
    description="Kết toán cược đã có kết quả",
)
async def kettoan(
    interaction: discord.Interaction,
):
    if not (
        interaction.user.guild_permissions.administrator
        or
        interaction.user.guild_permissions.manage_guild
    ):
        await interaction.response.send_message(
            "❌ Chỉ Admin/Manage Server "
            "được dùng lệnh này.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(
        ephemeral=True
    )

    conn = get_db()

    bets = conn.execute(
        """
        SELECT *
        FROM bets
        WHERE status='open'
        ORDER BY id ASC
        """
    ).fetchall()

    conn.close()

    if not bets:
        await interaction.followup.send(
            "📭 Không có cược đang mở.",
            ephemeral=True,
        )
        return

    processed = 0
    wins = 0
    losses = 0
    waiting = 0
    errors = 0
    total_paid = 0

    for bet in bets:

        try:
            match = await get_match(
                int(bet["fixture_id"])
            )

            result = get_result(
                match
            )

            if result is None:
                waiting += 1
                continue

            outcome, home_score, away_score = result

            user_id = int(
                bet["user_id"]
            )

            choice = bet["choice"]

            stake = int(
                bet["stake"]
            )

            # ============================
            # WIN
            # ============================

            if choice == outcome:

                payout = (
                    stake
                    * BET_PAYOUT
                )

                (
                    old_balance,
                    new_balance,
                    old_vip,
                    new_vip,
                ) = change_balance(
                    user_id,
                    payout,
                )

                conn = get_db()

                conn.execute(
                    """
                    UPDATE bets
                    SET status='won'
                    WHERE id=?
                    """,
                    (
                        int(
                            bet["id"]
                        ),
                    ),
                )

                conn.commit()
                conn.close()

                wins += 1
                total_paid += payout

                if new_vip > old_vip:
                    member = None

                    if interaction.guild:
                        member = (
                            interaction.guild
                            .get_member(
                                user_id
                            )
                        )

                    await announce_vip(
                        member,
                        old_vip,
                        new_vip,
                    )

            # ============================
            # LOSE
            # ============================

            else:

                conn = get_db()

                conn.execute(
                    """
                    UPDATE bets
                    SET status='lost'
                    WHERE id=?
                    """,
                    (
                        int(
                            bet["id"]
                        ),
                    ),
                )

                conn.commit()
                conn.close()

                losses += 1

            processed += 1

        except Exception as e:
            print(
                "KETTOAN ERROR:",
                repr(e),
            )

            errors += 1

    await interaction.followup.send(
        f"🏁 **KẾT TOÁN XONG**\n\n"
        f"📊 Đã xử lý: **{processed}**\n"
        f"🟢 Thắng: **{wins}**\n"
        f"🔴 Thua: **{losses}**\n"
        f"⏳ Chưa có kết quả: **{waiting}**\n"
        f"💰 Tổng tiền trả: **{total_paid:,}**\n"
        f"⚠️ Lỗi: **{errors}**",
        ephemeral=True,
    )


# ============================================================
# /CADO
# ============================================================

@bot.tree.command(
    name="cado",
    description="Thông tin bot",
)
async def cado(
    interaction: discord.Interaction,
):
    await interaction.response.send_message(
        "⚽ **Football Betting Bot**\n\n"
        "📡 Nguồn bóng đá: "
        "**football-data.org API v4**\n"
        "💰 Ví/VIP: SQLite\n"
        "🎁 Daily: 50.000\n"
        "🚀 Khởi nghiệp: 1.000\n"
        "🏆 Cược: 2x khi thắng",
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
    await interaction.response.send_message(
        f"🏓 Pong! "
        f"`{round(bot.latency * 1000)} ms`",
        ephemeral=True,
    )


# ============================================================
# /APIQUOTA
# ============================================================

@bot.tree.command(
    name="apiquota",
    description="Thông tin API",
)
async def apiquota(
    interaction: discord.Interaction,
):
    await interaction.response.send_message(
        "📡 **football-data.org API v4**\n"
        "🆓 Free: **10 requests/phút**\n"
        "📦 Bot có cache để hạn chế gọi API.\n"
        "⚠️ Dữ liệu live không phải tính năng của Free.",
        ephemeral=True,
    )


# ============================================================
# RAILWAY HEALTH CHECK
# ============================================================

async def health(
    request
):
    return web.Response(
        text="OK"
    )


async def start_health_server():
    global health_runner

    if health_runner is not None:
        return

    port = int(
        os.getenv(
            "PORT",
            "8080"
        )
    )

    app = web.Application()

    app.router.add_get(
        "/",
        health,
    )

    app.router.add_get(
        "/health",
        health,
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
        f"Health server running on port {port}"
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
                id=int(
                    GUILD_ID
                )
            )

            bot.tree.copy_global_to(
                guild=guild
            )

            await bot.tree.sync(
                guild=guild
            )

            print(
                f"Discord commands synced "
                f"to guild {GUILD_ID}"
            )

        else:

            await bot.tree.sync()

            print(
                "Global Discord commands synced."
            )

    except Exception as e:

        print(
            "COMMAND SYNC ERROR:",
            repr(e)
        )

    await start_health_server()

    print(
        "================================"
    )

    print(
        f"BOT ONLINE: {bot.user}"
    )

    print(
        "FOOTBALL-DATA.ORG: ENABLED"
    )

    print(
        "================================"
    )


# ============================================================
# SHUTDOWN
# ============================================================

async def cleanup():

    global http_session
    global health_runner

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

def main():

    if not DISCORD_TOKEN:
        raise RuntimeError(
            "❌ Thiếu DISCORD_TOKEN trên Railway."
        )

    if not FOOTBALL_DATA_API_KEY:
        raise RuntimeError(
            "❌ Thiếu FOOTBALL_DATA_API_KEY trên Railway."
        )

    bot.run(
        DISCORD_TOKEN
    )


if __name__ == "__main__":
    main()

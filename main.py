import os
import json
import time
import sqlite3
import asyncio
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web
import discord
from discord import app_commands
from discord.ext import commands


# =========================================================
# CONFIG
# =========================================================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()

FOOTBALL_API_KEY = (
    os.getenv("FOOTBALL_API_KEY", "").strip()
    or os.getenv("API_FOOTBALL_KEY", "").strip()
    or os.getenv("FOOTBALL_KEY", "").strip()
    or os.getenv("API_KEY", "").strip()
)

GUILD_ID = 1551547049600618538

API_BASE = "https://v3.football.api-sports.io"
TIMEZONE = "Asia/Ho_Chi_Minh"
TZ = ZoneInfo(TIMEZONE)

DB_PATH = "/data/football_bot.db" if os.path.isdir("/data") else "football_bot.db"

STARTING_BALANCE = 100000
STARTER_BONUS = 1000
DAILY_BONUS = 50000

MANCHESTER_CITY_TEAM_ID = 50


# =========================================================
# 11 GIẢI
# =========================================================

LEAGUES = {
    "39": ("🏴", "Premier League"),
    "2": ("🏆", "UEFA Champions League"),
    "140": ("🇪🇸", "La Liga"),
    "135": ("🇮🇹", "Serie A"),
    "78": ("🇩🇪", "Bundesliga"),
    "61": ("🇫🇷", "Ligue 1"),
    "88": ("🇳🇱", "Eredivisie"),
    "94": ("🇵🇹", "Primeira Liga"),
    "40": ("🏴", "Championship"),
    "45": ("🏆", "FA Cup"),
    "48": ("🏆", "Carabao Cup"),
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


# =========================================================
# CHECK ENV
# =========================================================

if not DISCORD_TOKEN:
    raise RuntimeError("THIẾU DISCORD_TOKEN")

if not FOOTBALL_API_KEY:
    raise RuntimeError("THIẾU FOOTBALL_API_KEY")


# =========================================================
# BOT
# QUAN TRỌNG:
# bot phải được tạo TRƯỚC tất cả @bot.tree.command
# =========================================================

intents = discord.Intents.default()

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)

GUILD = discord.Object(id=GUILD_ID)


# =========================================================
# DATABASE
# =========================================================

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:

        conn.execute("""
            CREATE TABLE IF NOT EXISTS wallets (
                user_id INTEGER PRIMARY KEY,
                balance INTEGER NOT NULL DEFAULT 100000,
                started INTEGER NOT NULL DEFAULT 0,
                vip INTEGER NOT NULL DEFAULT 0
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_claims (
                user_id INTEGER NOT NULL,
                claim_date TEXT NOT NULL,
                PRIMARY KEY(user_id, claim_date)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                fixture_id INTEGER NOT NULL,
                choice TEXT NOT NULL,
                stake INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                settled_at TEXT
            )
        """)

        conn.commit()


def format_money(number):
    return f"{int(number):,}".replace(",", ".")


def get_vip(balance):
    for level, minimum in VIP_LEVELS:
        if balance >= minimum:
            return level

    return 0


def vip_name(level):
    if level == 0:
        return "Thành viên"

    return f"VIP {level}"


def vip_icon(level):
    if level >= 10:
        return "👑"

    if level >= 7:
        return "💎"

    if level >= 4:
        return "🔥"

    if level >= 1:
        return "⭐"

    return "👤"


def get_next_vip(level):

    for vip, minimum in sorted(VIP_LEVELS):

        if vip > level:
            return vip, minimum

    return None, None


def ensure_wallet(user_id):

    with db() as conn:

        row = conn.execute(
            "SELECT * FROM wallets WHERE user_id=?",
            (user_id,)
        ).fetchone()

        if row is None:

            vip = get_vip(STARTING_BALANCE)

            conn.execute(
                """
                INSERT INTO wallets
                (user_id,balance,started,vip)
                VALUES(?,?,?,?)
                """,
                (
                    user_id,
                    STARTING_BALANCE,
                    0,
                    vip
                )
            )

            conn.commit()

            return STARTING_BALANCE

        return int(row["balance"])


def get_balance(user_id):

    return ensure_wallet(user_id)


def get_wallet(user_id):

    ensure_wallet(user_id)

    with db() as conn:

        return conn.execute(
            "SELECT * FROM wallets WHERE user_id=?",
            (user_id,)
        ).fetchone()


async def check_vip(user_id, channel=None):

    row = get_wallet(user_id)

    balance = int(row["balance"])
    old_vip = int(row["vip"])

    new_vip = get_vip(balance)

    if new_vip == old_vip:
        return

    with db() as conn:

        conn.execute(
            """
            UPDATE wallets
            SET vip=?
            WHERE user_id=?
            """,
            (
                new_vip,
                user_id
            )
        )

        conn.commit()

    # CHỈ THÔNG BÁO KHI LÊN CẤP
    if new_vip > old_vip and channel:

        user = bot.get_user(user_id)

        name = user.display_name if user else str(user_id)

        embed = discord.Embed(
            title=f"🎉 CHÚC MỪNG {vip_name(new_vip).upper()} 🎉",
            description=(
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 **{name}**\n\n"
                f"💰 Số dư: **{format_money(balance)} xu**\n"
                f"{vip_icon(new_vip)} Cấp mới: **{vip_name(new_vip)}**\n"
                "━━━━━━━━━━━━━━━━━━━━"
            ),
            color=discord.Color.gold()
        )

        try:
            await channel.send(embed=embed)

        except Exception:
            pass


async def add_money(user_id, amount, channel=None):

    ensure_wallet(user_id)

    with db() as conn:

        conn.execute(
            """
            UPDATE wallets
            SET balance=balance+?
            WHERE user_id=?
            """,
            (
                amount,
                user_id
            )
        )

        conn.commit()

    await check_vip(user_id, channel)


async def remove_money(user_id, amount, channel=None):

    ensure_wallet(user_id)

    with db() as conn:

        conn.execute(
            """
            UPDATE wallets
            SET balance=MAX(balance-?,0)
            WHERE user_id=?
            """,
            (
                amount,
                user_id
            )
        )

        conn.commit()

    await check_vip(user_id, channel)


# =========================================================
# FOOTBALL API
# =========================================================

class FootballAPI:

    def __init__(self):

        self.session = None

        self.cache = {}

        self.cache_seconds = 30

        self.quota = None

        self.errors = []


    async def start(self):

        if self.session is None or self.session.closed:

            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=25),

                headers={
                    "x-apisports-key": FOOTBALL_API_KEY,
                    "Accept": "application/json"
                }
            )


    async def close(self):

        if self.session and not self.session.closed:

            await self.session.close()

        self.session = None


    async def request(self, endpoint, params):

        await self.start()

        key = (
            endpoint,
            tuple(
                sorted(
                    (str(k), str(v))
                    for k, v in params.items()
                )
            )
        )

        now = time.monotonic()

        cached = self.cache.get(key)

        if cached:

            if now - cached[0] < self.cache_seconds:

                return cached[1]


        url = f"{API_BASE}/{endpoint}"

        last_error = None

        for attempt in range(3):

            try:

                async with self.session.get(
                    url,
                    params=params
                ) as response:

                    raw = await response.text()

                    try:
                        data = json.loads(raw)

                    except Exception:

                        data = {
                            "errors": {
                                "response": raw[:500]
                            }
                        }


                    remaining = response.headers.get(
                        "x-ratelimit-requests-remaining"
                    )

                    if remaining is not None:

                        self.quota = remaining


                    if response.status >= 500:

                        if attempt < 2:

                            await asyncio.sleep(
                                attempt + 1
                            )

                            continue


                    if response.status >= 400:

                        raise RuntimeError(
                            f"HTTP {response.status}: "
                            f"{data.get('errors')}"
                        )


                    errors = data.get("errors")

                    if isinstance(errors, dict):

                        if any(errors.values()):

                            raise RuntimeError(
                                str(errors)
                            )


                    if isinstance(errors, list):

                        if errors:

                            raise RuntimeError(
                                str(errors)
                            )


                    self.cache[key] = (
                        now,
                        data
                    )

                    return data


            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                RuntimeError
            ) as error:

                last_error = error

                self.errors.append(
                    str(error)
                )

                if attempt < 2:

                    await asyncio.sleep(
                        attempt + 1
                    )


        raise RuntimeError(
            str(last_error)
        )


    async def next_team(
        self,
        team_id=50,
        count=20
    ):

        data = await self.request(
            "fixtures",
            {
                "team": team_id,
                "next": count,
                "timezone": TIMEZONE
            }
        )

        return data.get(
            "response",
            []
        )


    async def next_league(
        self,
        league_id,
        count=20
    ):

        data = await self.request(
            "fixtures",
            {
                "league": league_id,
                "next": count,
                "timezone": TIMEZONE
            }
        )

        return data.get(
            "response",
            []
        )


    async def prediction(
        self,
        fixture_id
    ):

        return await self.request(
            "predictions",
            {
                "fixture": fixture_id
            }
        )


football = FootballAPI()


# =========================================================
# FIXTURE HELPERS
# =========================================================

def fixture_id(fixture):

    return (
        fixture.get("fixture") or {}
    ).get("id")


def fixture_status(fixture):

    return (
        (
            fixture.get("fixture") or {}
        ).get("status") or {}
    ).get("short", "")


def fixture_timestamp(fixture):

    return (
        fixture.get("fixture") or {}
    ).get("timestamp", 0)


def is_upcoming(fixture):

    return fixture_status(fixture) in {
        "NS",
        "TBD"
    }


def fixture_time(fixture):

    raw = (
        fixture.get("fixture") or {}
    ).get("date")

    if not raw:

        return "Chưa có giờ"

    try:

        dt = datetime.fromisoformat(
            raw.replace(
                "Z",
                "+00:00"
            )
        )

        dt = dt.astimezone(TZ)

        return dt.strftime(
            "%d/%m/%Y %H:%M"
        )

    except Exception:

        return str(raw)


def team_name(
    fixture,
    side
):

    return (
        (
            fixture.get("teams") or {}
        ).get(side) or {}
    ).get(
        "name",
        "?"
    )


def league_name(fixture):

    return (
        fixture.get("league") or {}
    ).get(
        "name",
        "Giải đấu"
    )


def choice_name(
    choice,
    fixture
):

    if choice == "home":

        return team_name(
            fixture,
            "home"
        )

    if choice == "away":

        return team_name(
            fixture,
            "away"
        )

    return "Hòa"


def parse_prediction_score(prediction):

    score = (
        prediction.get("score")
        or {}
    )

    fulltime = (
        score.get("fulltime")
        or {}
    )

    home = fulltime.get("home")
    away = fulltime.get("away")

    if isinstance(home, int) and isinstance(away, int):

        return home, away


    home = score.get("home")
    away = score.get("away")

    if isinstance(home, int) and isinstance(away, int):

        return home, away


    goals = (
        prediction.get("goals")
        or {}
    )

    home = goals.get("home")
    away = goals.get("away")

    if isinstance(home, int) and isinstance(away, int):

        return home, away


    return None


# =========================================================
# ERROR EMBED
# =========================================================

def api_error(exc):

    return discord.Embed(
        title="❌ API-Football lỗi",
        description=f"`{str(exc)[:900]}`",
        color=discord.Color.red()
    )


# =========================================================
# MATCH ANALYSIS
# =========================================================

async def show_prediction(
    interaction,
    fixture
):

    fid = fixture_id(fixture)

    home = team_name(
        fixture,
        "home"
    )

    away = team_name(
        fixture,
        "away"
    )

    embed = discord.Embed(
        title=f"🔮 {home} vs {away}",

        description=(
            f"🏆 **{league_name(fixture)}**\n"
            f"📅 **{fixture_time(fixture)}**\n"
            f"🆔 `{fid}`"
        ),

        color=discord.Color.blurple()
    )


    try:

        data = await football.prediction(
            fid
        )

        response = data.get(
            "response",
            []
        )


        if not response:

            embed.add_field(
                name="🎯 Dự đoán",
                value=(
                    "API chưa có dữ liệu "
                    "dự đoán trận này."
                ),
                inline=False
            )


        else:

            prediction = (
                response[0].get(
                    "predictions"
                )
                or {}
            )


            score = parse_prediction_score(
                prediction
            )


            if score:

                score_text = (
                    f"**{score[0]} - {score[1]}**"
                )

            else:

                score_text = (
                    "API chưa trả tỷ số chính xác"
                )


            percent = (
                prediction.get(
                    "percent"
                )
                or {}
            )


            winner = (
                prediction.get(
                    "winner"
                )
                or {}
            )


            winner_name = (
                winner.get("name")
                or "Chưa xác định"
            )


            embed.add_field(
                name="🎯 TỶ SỐ DỰ ĐOÁN",
                value=score_text,
                inline=False
            )


            embed.add_field(
                name="📊 XÁC SUẤT",
                value=(
                    f"🏠 Home: **{percent.get('home','?')}%**\n"
                    f"🤝 Draw: **{percent.get('draw','?')}%**\n"
                    f"✈️ Away: **{percent.get('away','?')}%**"
                ),
                inline=False
            )


            embed.add_field(
                name="🏁 KẾT LUẬN",
                value=f"**{winner_name}**",
                inline=False
            )


            advice = prediction.get(
                "advice"
            )

            if advice:

                embed.add_field(
                    name="💡 Nhận định API",
                    value=str(advice)[:900],
                    inline=False
                )


    except Exception as exc:

        embed.add_field(
            name="⚠️ Lỗi dự đoán",
            value=f"`{str(exc)[:700]}`",
            inline=False
        )


    embed.set_footer(
        text="API-Football • Chỉ mang tính tham khảo"
    )


    await interaction.followup.send(
        embed=embed,
        ephemeral=True
    )


# =========================================================
# MATCH SELECT
# =========================================================

class MatchSelect(discord.ui.Select):

    def __init__(
        self,
        fixtures,
        mode
    ):

        self.fixtures = {}

        options = []


        for fixture in fixtures[:25]:

            fid = fixture_id(
                fixture
            )

            if not fid:

                continue


            self.fixtures[
                str(fid)
            ] = fixture


            options.append(
                discord.SelectOption(
                    label=(
                        f"{team_name(fixture,'home')} "
                        f"vs "
                        f"{team_name(fixture,'away')}"
                    )[:100],

                    value=str(fid),

                    description=(
                        fixture_time(
                            fixture
                        )[:100]
                    )
                )
            )


        super().__init__(
            placeholder="⚽ Chọn trận...",
            min_values=1,
            max_values=1,
            options=options
        )


        self.mode = mode


    async def callback(
        self,
        interaction
    ):

        fixture = self.fixtures.get(
            self.values[0]
        )


        if not fixture:

            await interaction.response.send_message(
                "❌ Không tìm thấy trận.",
                ephemeral=True
            )

            return


        if self.mode == "bet":

            await interaction.response.send_modal(
                BetModal(fixture)
            )

        else:

            await interaction.response.defer(
                ephemeral=True
            )

            await show_prediction(
                interaction,
                fixture
            )


class MatchView(discord.ui.View):

    def __init__(
        self,
        fixtures,
        mode
    ):

        super().__init__(
            timeout=180
        )

        if fixtures:

            self.add_item(
                MatchSelect(
                    fixtures,
                    mode
                )
            )


# =========================================================
# BET MODAL
# =========================================================

class BetModal(
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
        fixture
    ):

        super().__init__()

        self.fixture = fixture


    async def on_submit(
        self,
        interaction
    ):

        try:

            amount = int(
                str(
                    self.amount
                )
                .replace(
                    ".",
                    ""
                )
                .replace(
                    ",",
                    ""
                )
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


        balance = get_balance(
            interaction.user.id
        )


        if amount > balance:

            await interaction.response.send_message(
                (
                    "❌ Không đủ xu.\n"
                    f"Ví hiện tại: **{format_money(balance)} xu**"
                ),
                ephemeral=True
            )

            return


        await interaction.response.send_message(
            embed=discord.Embed(
                title="🎯 CHỌN CỬA",

                description=(
                    f"⚽ **{team_name(self.fixture,'home')}**\n"
                    "🆚\n"
                    f"⚽ **{team_name(self.fixture,'away')}**\n\n"
                    f"💰 Cược: **{format_money(amount)} xu**"
                ),

                color=discord.Color.gold()
            ),

            view=BetChoiceView(
                self.fixture,
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
        fixture,
        amount
    ):

        super().__init__(
            timeout=120
        )

        self.fixture = fixture
        self.amount = amount


    async def place(
        self,
        interaction,
        choice
    ):

        user_id = interaction.user.id

        fid = fixture_id(
            self.fixture
        )


        if self.amount > get_balance(
            user_id
        ):

            await interaction.response.send_message(
                "❌ Không đủ xu.",
                ephemeral=True
            )

            return


        with db() as conn:

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
                    fid
                )
            ).fetchone()


            if existing:

                await interaction.response.send_message(
                    "❌ M đã cược trận này rồi.",
                    ephemeral=True
                )

                return


            conn.execute(
                """
                UPDATE wallets
                SET balance=balance-?
                WHERE user_id=?
                """,

                (
                    self.amount,
                    user_id
                )
            )


            conn.execute(
                """
                INSERT INTO bets
                (
                    user_id,
                    fixture_id,
                    choice,
                    stake,
                    status,
                    created_at
                )

                VALUES(?,?,?,?,?,?)
                """,

                (
                    user_id,
                    fid,
                    choice,
                    self.amount,
                    "open",
                    datetime.now(TZ).isoformat()
                )
            )


            conn.commit()


        await interaction.response.send_message(
            (
                "✅ **ĐẶT CƯỢC THÀNH CÔNG**\n\n"
                f"⚽ Trận: **{team_name(self.fixture,'home')} "
                f"vs {team_name(self.fixture,'away')}**\n"
                f"🎯 Cửa: **{choice_name(choice,self.fixture)}**\n"
                f"💰 Tiền cược: **{format_money(self.amount)} xu**\n"
                f"💵 Còn lại: **{format_money(get_balance(user_id))} xu**"
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
            "home"
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
            "draw"
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
            "away"
        )


# =========================================================
# CUOC HOME
# =========================================================

class CuocLeagueSelect(
    discord.ui.Select
):

    def __init__(self):

        options = []

        for league_id, (
            emoji,
            name
        ) in LEAGUES.items():

            options.append(
                discord.SelectOption(
                    label=name,
                    value=league_id,
                    emoji=emoji
                )
            )


        super().__init__(
            placeholder="🏆 Chọn giải để cược...",
            options=options
        )


    async def callback(
        self,
        interaction
    ):

        league_id = int(
            self.values[0]
        )

        emoji, name = LEAGUES[
            str(league_id)
        ]


        await interaction.response.defer(
            ephemeral=True
        )


        try:

            fixtures = await football.next_league(
                league_id,
                20
            )


            fixtures = [
                x
                for x in fixtures
                if is_upcoming(x)
            ]


            fixtures.sort(
                key=fixture_timestamp
            )


            if not fixtures:

                await interaction.followup.send(
                    (
                        f"❌ Chưa có trận sắp tới "
                        f"trong **{name}**."
                    ),
                    ephemeral=True
                )

                return


            await interaction.followup.send(

                embed=discord.Embed(
                    title=f"{emoji} {name}",

                    description=(
                        f"Chọn trận để cược.\n"
                        f"Có **{len(fixtures)}** trận."
                    ),

                    color=discord.Color.green()
                ),

                view=MatchView(
                    fixtures,
                    "bet"
                ),

                ephemeral=True
            )


        except Exception as exc:

            await interaction.followup.send(
                embed=api_error(exc),
                ephemeral=True
            )


class CuocHomeView(
    discord.ui.View
):

    def __init__(self):

        super().__init__(
            timeout=300
        )

        self.add_item(
            CuocLeagueSelect()
        )


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

            fixtures = await football.next_team(
                MANCHESTER_CITY_TEAM_ID,
                20
            )


            fixtures = [
                x
                for x in fixtures
                if is_upcoming(x)
            ]


            fixtures.sort(
                key=fixture_timestamp
            )


            if not fixtures:

                await interaction.followup.send(
                    "❌ Không có trận Man City sắp tới.",
                    ephemeral=True
                )

                return


            await interaction.followup.send(

                embed=discord.Embed(
                    title="🔵 MAN CITY — CÁC TRẬN SẮP TỚI",

                    description=(
                        f"Tất cả trận tiếp theo của Man City.\n"
                        f"**{len(fixtures)}** trận."
                    ),

                    color=discord.Color.blue()
                ),

                view=MatchView(
                    fixtures,
                    "bet"
                ),

                ephemeral=True
            )


        except Exception as exc:

            await interaction.followup.send(
                embed=api_error(exc),
                ephemeral=True
            )


# =========================================================
# SOI HOME
# =========================================================

class SoiLeagueSelect(
    discord.ui.Select
):

    def __init__(self):

        options = []

        for league_id, (
            emoji,
            name
        ) in LEAGUES.items():

            options.append(
                discord.SelectOption(
                    label=name,
                    value=league_id,
                    emoji=emoji
                )
            )


        super().__init__(
            placeholder="🏆 Chọn giải để soi...",
            options=options
        )


    async def callback(
        self,
        interaction
    ):

        league_id = int(
            self.values[0]
        )

        emoji, name = LEAGUES[
            str(league_id)
        ]


        await interaction.response.defer(
            ephemeral=True
        )


        try:

            fixtures = await football.next_league(
                league_id,
                20
            )


            fixtures = [
                x
                for x in fixtures
                if is_upcoming(x)
            ]


            fixtures.sort(
                key=fixture_timestamp
            )


            if not fixtures:

                await interaction.followup.send(
                    (
                        f"❌ Chưa có trận sắp tới "
                        f"trong **{name}**."
                    ),
                    ephemeral=True
                )

                return


            await interaction.followup.send(

                embed=discord.Embed(
                    title=f"{emoji} {name}",

                    description=(
                        "Chọn trận để soi."
                    ),

                    color=discord.Color.blurple()
                ),

                view=MatchView(
                    fixtures,
                    "soi"
                ),

                ephemeral=True
            )


        except Exception as exc:

            await interaction.followup.send(
                embed=api_error(exc),
                ephemeral=True
            )


class SoiHomeView(
    discord.ui.View
):

    def __init__(self):

        super().__init__(
            timeout=300
        )

        self.add_item(
            SoiLeagueSelect()
        )


    @discord.ui.button(
        label="🔵 Man City — tất cả trận",
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

            fixtures = await football.next_team(
                MANCHESTER_CITY_TEAM_ID,
                20
            )


            fixtures = [
                x
                for x in fixtures
                if is_upcoming(x)
            ]


            fixtures.sort(
                key=fixture_timestamp
            )


            await interaction.followup.send(

                embed=discord.Embed(
                    title="🔵 MAN CITY — TẤT CẢ TRẬN",

                    description=(
                        "Tất cả trận Man City sắp tới "
                        "không phân biệt giải.\n"
                        f"**{len(fixtures)}** trận."
                    ),

                    color=discord.Color.blue()
                ),

                view=MatchView(
                    fixtures,
                    "soi"
                ),

                ephemeral=True
            )


        except Exception as exc:

            await interaction.followup.send(
                embed=api_error(exc),
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

    embed = discord.Embed(

        title="⚽ BẢNG CƯỢC BÓNG ĐÁ",

        description=(
            "🏆 Chọn 1 trong 11 giải đấu bên dưới.\n"
            "🔵 Hoặc xem tất cả trận Man City sắp tới.\n\n"
            "💰 Cược bằng xu ảo."
        ),

        color=discord.Color.blue()
    )


    await interaction.response.send_message(

        embed=embed,

        view=CuocHomeView(),

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

    embed = discord.Embed(

        title="🔮 SOI BÓNG ĐÁ",

        description=(
            "🏆 Chọn 1 trong 11 giải đấu.\n"
            "🔵 Hoặc xem tất cả trận Man City.\n\n"
            "Bot sẽ lấy dữ liệu dự đoán từ API-Football "
            "nếu API có dữ liệu."
        ),

        color=discord.Color.blurple()
    )


    await interaction.response.send_message(

        embed=embed,

        view=SoiHomeView(),

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

    row = get_wallet(
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

        next_text = (
            "👑 **VIP 10 MAX**"
        )

    else:

        missing = max(
            0,
            next_money - balance
        )

        next_text = (
            f"🎯 **VIP {next_level}**\n"
            f"Cần: **{format_money(next_money)} xu**\n"
            f"Còn thiếu: **{format_money(missing)} xu**"
        )


    embed = discord.Embed(

        title=(
            f"{vip_icon(vip)} "
            f"{vip_name(vip)}"
        ),

        color=discord.Color.gold()
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

        name="🎁 Tiền mặc định",

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

    user_id = interaction.user.id

    ensure_wallet(
        user_id
    )


    with db() as conn:

        row = conn.execute(

            """
            SELECT started
            FROM wallets
            WHERE user_id=?
            """,

            (
                user_id,
            )

        ).fetchone()


        if row["started"]:

            await interaction.response.send_message(

                "❌ M đã nhận 1.000 xu khởi nghiệp rồi.",

                ephemeral=True
            )

            return


        conn.execute(

            """
            UPDATE wallets

            SET balance=balance+?,
                started=1

            WHERE user_id=?
            """,

            (
                STARTER_BONUS,
                user_id
            )
        )


        conn.commit()


    await check_vip(
        user_id,
        interaction.channel
    )


    await interaction.response.send_message(

        (
            "🎉 Nhận thành công **1.000 xu**!\n"
            f"💰 Ví: **{format_money(get_balance(user_id))} xu**"
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

    user_id = interaction.user.id

    ensure_wallet(
        user_id
    )


    today = datetime.now(
        TZ
    ).date().isoformat()


    with db() as conn:

        claimed = conn.execute(

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


        if claimed:

            await interaction.response.send_message(

                "❌ Hôm nay m đã nhận 50.000 xu rồi.\n"
                "⏰ Mai quay lại nhận tiếp.",

                ephemeral=True
            )

            return


        conn.execute(

            """
            INSERT INTO daily_claims
            (user_id,claim_date)
            VALUES(?,?)
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


    await check_vip(
        user_id,
        interaction.channel
    )


    await interaction.response.send_message(

        (
            "🎁 Nhận **50.000 xu** hôm nay!\n"
            f"💰 Ví: **{format_money(get_balance(user_id))} xu**"
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
    description="Kiểm tra quota API-Football"
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


    quota = football.quota or "Chưa có dữ liệu"


    errors = football.errors[-3:]


    if errors:

        error_text = "\n".join(
            f"• {x[:180]}"
            for x in errors
        )

    else:

        error_text = "Không có lỗi."


    embed = discord.Embed(

        title="📡 API-Football",

        color=discord.Color.blue()
    )


    embed.add_field(

        name="Quota còn lại",

        value=f"`{quota}`",

        inline=False
    )


    embed.add_field(

        name="Lỗi gần đây",

        value=error_text[:1000],

        inline=False
    )


    await interaction.response.send_message(

        embed=embed,

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

    row = get_wallet(
        interaction.user.id
    )


    balance = int(
        row["balance"]
    )

    vip = get_vip(
        balance
    )


    embed = discord.Embed(

        title=(
            f"💰 VÍ — "
            f"{interaction.user.display_name}"
        ),

        color=discord.Color.green()
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
    reason="Lý do"
)
async def admin(
    interaction: discord.Interaction,
    action: str,
    user: discord.Member,
    amount: int,
    reason: Optional[str] = None
):

    if not interaction.user.guild_permissions.administrator:

        await interaction.response.send_message(

            "❌ Chỉ Administrator mới dùng được.",

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

            "❌ Action phải là `add` hoặc `remove`.",

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

        await add_money(
            user.id,
            amount,
            interaction.channel
        )

        action_text = "Cộng"


    else:

        await remove_money(
            user.id,
            amount,
            interaction.channel
        )

        action_text = "Trừ"


    await interaction.response.send_message(

        (
            f"✅ **{action_text} {format_money(amount)} xu**\n"
            f"👤 {user.mention}\n"
            f"💰 Số dư: **{format_money(get_balance(user.id))} xu**\n"
            f"📝 Lý do: **{reason or 'Không ghi'}**"
        ),

        ephemeral=True
    )


# =========================================================
# /KETTOAN
# =========================================================

@bot.tree.command(
    name="kettoan",
    description="Kết toán các cược đã có kết quả"
)
async def kettoan(
    interaction: discord.Interaction
):

    if not interaction.user.guild_permissions.manage_guild:

        await interaction.response.send_message(

            "❌ Chỉ quản lý server mới được kết toán.",

            ephemeral=True
        )

        return


    await interaction.response.defer(
        ephemeral=True
    )


    with db() as conn:

        bets = conn.execute(

            """
            SELECT *
            FROM bets
            WHERE status='open'
            ORDER BY id ASC
            """

        ).fetchall()


    if not bets:

        await interaction.followup.send(

            "ℹ️ Không có cược đang chờ.",

            ephemeral=True
        )

        return


    settled = 0
    pending = 0
    paid = 0


    for bet in bets:

        fixture_id_value = int(
            bet["fixture_id"]
        )


        try:

            data = await football.request(

                "fixtures",

                {
                    "id": fixture_id_value
                }
            )


            fixtures = data.get(
                "response",
                []
            )


            if not fixtures:

                pending += 1

                continue


            fixture = fixtures[0]


            status = fixture_status(
                fixture
            )


            if status in {
                "NS",
                "TBD",
                "PST",
                "CANC",
                "ABD",
                "AWD",
                "WO"
            }:

                pending += 1

                continue


            teams = (
                fixture.get(
                    "teams"
                )
                or {}
            )


            home = (
                teams.get(
                    "home"
                )
                or {}
            )


            away = (
                teams.get(
                    "away"
                )
                or {}
            )


            result = None


            if home.get(
                "winner"
            ) is True:

                result = "home"


            elif away.get(
                "winner"
            ) is True:

                result = "away"


            else:

                goals = (
                    fixture.get(
                        "goals"
                    )
                    or {}
                )


                hg = goals.get(
                    "home"
                )

                ag = goals.get(
                    "away"
                )


                if (
                    isinstance(hg, int)
                    and
                    isinstance(ag, int)
                ):

                    if hg > ag:

                        result = "home"

                    elif ag > hg:

                        result = "away"

                    else:

                        result = "draw"


            if result is None:

                pending += 1

                continue


            user_id = int(
                bet["user_id"]
            )


            stake = int(
                bet["stake"]
            )


            choice = bet["choice"]


            if choice == result:

                payout = stake * 2

                await add_money(
                    user_id,
                    payout,
                    interaction.channel
                )

                paid += payout


            with db() as conn:

                conn.execute(

                    """
                    UPDATE bets

                    SET status='settled',
                        settled_at=?

                    WHERE id=?
                    """,

                    (
                        datetime.now(
                            TZ
                        ).isoformat(),

                        int(
                            bet["id"]
                        )
                    )
                )

                conn.commit()


            settled += 1


        except Exception:

            pending += 1


    await interaction.followup.send(

        (
            "✅ **KẾT TOÁN XONG**\n\n"
            f"🎯 Đã kết toán: **{settled}**\n"
            f"⏳ Chưa có kết quả: **{pending}**\n"
            f"💰 Tổng tiền trả: **{format_money(paid)} xu**"
        ),

        ephemeral=True
    )


# =========================================================
# RAILWAY HEALTH SERVER
# =========================================================

health_runner = None


async def health(request):

    return web.json_response({

        "status": "ok",

        "bot": (
            str(bot.user)
            if bot.user
            else None
        ),

        "time": datetime.now(
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
        f"[HEALTH] Running on port {port}"
    )


# =========================================================
# READY
# =========================================================

sync_done = False


@bot.event
async def on_ready():

    global sync_done


    init_db()


    await football.start()


    await start_health_server()


    if not sync_done:

        try:

            synced = await bot.tree.sync(
                guild=GUILD
            )


            print(
                f"[DISCORD] Synced "
                f"{len(synced)} commands."
            )


            sync_done = True


        except Exception as exc:

            print(
                f"[DISCORD] Sync error: {exc}"
            )


    print(
        f"[BOT] Logged in as {bot.user}"
    )


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":

    init_db()

    bot.run(
        DISCORD_TOKEN
    )

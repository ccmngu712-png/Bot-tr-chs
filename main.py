import os
import json
import time
import sqlite3
import asyncio
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web
import discord
from discord import app_commands
from discord.ext import commands


# ============================================================
# CONFIG
# ============================================================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY", "").strip()

GUILD_ID = 1551547049600618538
GUILD = discord.Object(id=GUILD_ID)

API_BASE = "https://v3.football.api-sports.io"
TIMEZONE = "Asia/Ho_Chi_Minh"
TZ = ZoneInfo(TIMEZONE)

DB_PATH = "/data/football_bot.db" if os.path.isdir("/data") else "football_bot.db"

STARTING_BALANCE = 100000
STARTER_BONUS = 1000
DAILY_BONUS = 50000

MANCHESTER_CITY_TEAM_ID = 50

if not DISCORD_TOKEN:
    raise RuntimeError("Thiếu biến môi trường DISCORD_TOKEN")

if not FOOTBALL_API_KEY:
    raise RuntimeError("Thiếu biến môi trường FOOTBALL_API_KEY")


# ============================================================
# TẤT CẢ GIẢI DÙNG CHO /CUOC + /SOI
# ============================================================

LEAGUES = {
    39: ("🏴", "Premier League"),
    2: ("🏆", "Champions League"),
    140: ("🇪🇸", "La Liga"),
    135: ("🇮🇹", "Serie A"),
    78: ("🇩🇪", "Bundesliga"),
    61: ("🇫🇷", "Ligue 1"),
    88: ("🇳🇱", "Eredivisie"),
    94: ("🇵🇹", "Primeira Liga"),
    40: ("🏴", "Championship"),
    45: ("🏆", "FA Cup"),
    48: ("🏆", "Carabao Cup"),
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
    for level, minimum in VIP_LEVELS:
        if balance >= minimum:
            return level
    return 0


def vip_name(level: int) -> str:
    return f"VIP {level}" if level else "Thành viên"


def vip_icon(level: int) -> str:
    if level >= 10:
        return "👑"
    if level >= 7:
        return "💎"
    if level >= 4:
        return "🔥"
    if level >= 1:
        return "⭐"
    return "👤"


def money(number: int) -> str:
    return f"{number:,}".replace(",", ".")


# ============================================================
# DISCORD
# ============================================================

intents = discord.Intents.default()

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


# ============================================================
# DATABASE
# ============================================================

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
                UNIQUE(user_id, fixture_id)
            )
        """)

        # DB cũ
        for column, definition in [
            ("started", "INTEGER NOT NULL DEFAULT 0"),
            ("vip", "INTEGER NOT NULL DEFAULT 0"),
        ]:

            try:
                conn.execute(
                    f"ALTER TABLE wallets ADD COLUMN {column} {definition}"
                )
            except sqlite3.OperationalError:
                pass

        conn.commit()


def ensure_wallet(user_id: int):

    with db() as conn:

        row = conn.execute(
            "SELECT * FROM wallets WHERE user_id=?",
            (user_id,)
        ).fetchone()

        if row is None:

            conn.execute("""
                INSERT INTO wallets
                (user_id, balance, started, vip)
                VALUES (?, ?, 0, 0)
            """, (user_id, STARTING_BALANCE))

            conn.commit()

            return STARTING_BALANCE

        return int(row["balance"])


def get_balance(user_id: int) -> int:
    return ensure_wallet(user_id)


async def check_vip(user_id: int, channel=None):

    with db() as conn:

        row = conn.execute(
            "SELECT balance, vip FROM wallets WHERE user_id=?",
            (user_id,)
        ).fetchone()

        if not row:
            return

        balance = int(row["balance"])
        old_vip = int(row["vip"] or 0)
        new_vip = get_vip(balance)

        if new_vip != old_vip:

            conn.execute(
                "UPDATE wallets SET vip=? WHERE user_id=?",
                (new_vip, user_id)
            )

            conn.commit()

    # Chỉ thông báo khi tăng VIP
    if new_vip > old_vip and channel:

        user = bot.get_user(user_id)

        username = (
            user.display_name
            if user
            else str(user_id)
        )

        embed = discord.Embed(
            title=f"🎉 CHÚC MỪNG {vip_name(new_vip).upper()} 🎉",
            description=(
                "👑━━━━━━━━━━━━━━━━━━👑\n\n"
                f"👤 **{username}**\n\n"
                f"💰 Số dư: **{money(balance)} xu**\n\n"
                f"{vip_icon(new_vip)} Đã đạt **{vip_name(new_vip)}**\n\n"
                "👑━━━━━━━━━━━━━━━━━━👑"
            ),
            color=discord.Color.gold()
        )

        try:
            await channel.send(embed=embed)
        except Exception:
            pass


async def add_money(
    user_id: int,
    amount: int,
    channel=None
):

    ensure_wallet(user_id)

    with db() as conn:

        conn.execute(
            """
            UPDATE wallets
            SET balance = balance + ?
            WHERE user_id=?
            """,
            (amount, user_id)
        )

        conn.commit()

    await check_vip(user_id, channel)


async def remove_money(
    user_id: int,
    amount: int
):

    ensure_wallet(user_id)

    with db() as conn:

        conn.execute(
            """
            UPDATE wallets
            SET balance = MAX(balance - ?, 0)
            WHERE user_id=?
            """,
            (amount, user_id)
        )

        conn.commit()

    await check_vip(user_id)


# ============================================================
# FOOTBALL API
# ============================================================

class FootballAPI:

    def __init__(self):

        self.session: Optional[aiohttp.ClientSession] = None

        self.cache = {}

        self.cache_ttl = 30

        self.last_quota = None

        self.last_errors = []


    async def start(self):

        if self.session is None or self.session.closed:

            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20),
                headers={
                    "x-apisports-key": FOOTBALL_API_KEY,
                    "Accept": "application/json"
                }
            )


    async def close(self):

        if self.session and not self.session.closed:
            await self.session.close()

        self.session = None


    async def request(
        self,
        endpoint: str,
        params: dict
    ):

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

            created, data = cached

            if now - created < self.cache_ttl:
                return data

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

                    if remaining:
                        self.last_quota = remaining

                    if response.status >= 400:

                        raise RuntimeError(
                            f"HTTP {response.status}: "
                            f"{data.get('errors') or raw[:300]}"
                        )

                    errors = data.get("errors")

                    if errors:

                        if isinstance(errors, dict):

                            if any(errors.values()):
                                raise RuntimeError(str(errors))

                        elif isinstance(errors, list) and errors:

                            raise RuntimeError(str(errors))

                    self.cache[key] = (
                        now,
                        data
                    )

                    return data

            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                RuntimeError
            ) as exc:

                last_error = exc

                if attempt < 2:

                    await asyncio.sleep(
                        0.8 * (attempt + 1)
                    )

        self.last_errors.append(
            str(last_error)
        )

        raise RuntimeError(
            str(last_error)
        )


    async def fixture(
        self,
        fixture_id: int
    ):

        return await self.request(
            "fixtures",
            {
                "id": fixture_id
            }
        )


    async def prediction(
        self,
        fixture_id: int
    ):

        return await self.request(
            "predictions",
            {
                "fixture": fixture_id
            }
        )


    async def league_next(
        self,
        league_id: int,
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

        return data.get("response", [])


    async def team_next(
        self,
        team_id: int,
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

        return data.get("response", [])


football = FootballAPI()


# ============================================================
# HELPERS
# ============================================================

def fixture_status(fixture):

    return (
        (fixture.get("fixture") or {})
        .get("status") or {}
    ).get("short", "")


def is_upcoming(fixture):

    return fixture_status(fixture) in {
        "NS",
        "TBD"
    }


def team_name(
    fixture,
    side
):

    return (
        (fixture.get("teams") or {})
        .get(side) or {}
    ).get("name", "?")


def team_logo(
    fixture,
    side
):

    return (
        (fixture.get("teams") or {})
        .get(side) or {}
    ).get("logo")


def fixture_time(fixture):

    raw = (
        fixture.get("fixture") or {}
    ).get("date")

    if not raw:
        return "Chưa có giờ"

    try:

        dt = datetime.fromisoformat(
            raw.replace("Z", "+00:00")
        )

        dt = dt.astimezone(TZ)

        return dt.strftime(
            "%d/%m/%Y %H:%M"
        )

    except Exception:

        return str(raw)


def result_of(fixture):

    status = fixture_status(fixture)

    if status in {
        "NS",
        "TBD",
        "PST",
        "CANC",
        "ABD"
    }:
        return None

    teams = fixture.get("teams") or {}

    home = teams.get("home") or {}
    away = teams.get("away") or {}

    if home.get("winner") is True:
        return "home"

    if away.get("winner") is True:
        return "away"

    goals = fixture.get("goals") or {}

    hg = goals.get("home")
    ag = goals.get("away")

    if isinstance(hg, int) and isinstance(ag, int):

        if hg > ag:
            return "home"

        if ag > hg:
            return "away"

        return "draw"

    return None


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


def api_error(exc):

    return discord.Embed(
        title="❌ Không lấy được dữ liệu",
        description=f"`{str(exc)[:900]}`",
        color=discord.Color.red()
    )


# ============================================================
# MATCH ANALYSIS
# ============================================================

async def show_prediction(
    interaction,
    fixture,
    league_name
):

    fid = (
        fixture.get("fixture") or {}
    ).get("id")

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
            f"🏆 **{league_name}**\n"
            f"📅 **{fixture_time(fixture)}**\n"
            f"🆔 `{fid}`"
        ),
        color=discord.Color.blurple()
    )

    home_logo = team_logo(
        fixture,
        "home"
    )

    if home_logo:
        embed.set_thumbnail(
            url=home_logo
        )

    try:

        data = await football.prediction(
            int(fid)
        )

        response = data.get(
            "response",
            []
        )

        prediction = (
            response[0].get("predictions")
            if response
            else None
        )

        if not prediction:

            embed.add_field(
                name="🎯 Dự đoán tỷ số",
                value="API chưa có dữ liệu.",
                inline=False
            )

        else:

            winner = (
                prediction.get("winner")
                or {}
            )

            winner_name = (
                winner.get("name")
                or "Chưa xác định"
            )

            percent = (
                prediction.get("percent")
                or {}
            )

            hp = percent.get(
                "home",
                "?"
            )

            dp = percent.get(
                "draw",
                "?"
            )

            ap = percent.get(
                "away",
                "?"
            )

            goals = (
                prediction.get("goals")
                or {}
            )

            gh = goals.get("home")
            ga = goals.get("away")

            if isinstance(gh, int) and isinstance(ga, int):

                exact = (
                    f"**{gh} - {ga}**"
                )

            else:

                exact = (
                    "API chưa trả tỷ số chính xác"
                )

            embed.add_field(
                name="🎯 DỰ ĐOÁN TỶ SỐ",
                value=exact,
                inline=False
            )

            embed.add_field(
                name="📈 XÁC SUẤT",
                value=(
                    f"🏠 Home **{hp}%**\n"
                    f"🤝 Draw **{dp}%**\n"
                    f"✈️ Away **{ap}%**"
                ),
                inline=False
            )

            embed.add_field(
                name="🏁 KẾT LUẬN",
                value=f"**{winner_name}**",
                inline=False
            )

    except Exception:

        embed.add_field(
            name="🎯 Dự đoán",
            value="Không lấy được dữ liệu dự đoán.",
            inline=False
        )

    embed.set_footer(
        text="Dữ liệu từ API-Football • Chỉ mang tính tham khảo"
    )

    await interaction.followup.send(
        embed=embed,
        ephemeral=True
    )


# ============================================================
# MATCH SELECT
# ============================================================

class MatchSelect(discord.ui.Select):

    def __init__(
        self,
        fixtures,
        league_name,
        mode
    ):

        options = []

        for fixture in fixtures[:25]:

            fid = (
                fixture.get("fixture") or {}
            ).get("id")

            if not fid:
                continue

            label = (
                f"{team_name(fixture,'home')} "
                f"vs "
                f"{team_name(fixture,'away')}"
            )

            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=str(fid),
                    description=fixture_time(
                        fixture
                    )[:100]
                )
            )

        super().__init__(
            placeholder="⚽ Chọn trận...",
            min_values=1,
            max_values=1,
            options=options
        )

        self.fixtures = {
            str(
                (x.get("fixture") or {}).get("id")
            ): x
            for x in fixtures
        }

        self.league_name = league_name
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
                fixture,
                self.league_name
            )


class MatchView(discord.ui.View):

    def __init__(
        self,
        fixtures,
        league_name,
        mode
    ):

        super().__init__(
            timeout=180
        )

        if fixtures:

            self.add_item(
                MatchSelect(
                    fixtures,
                    league_name,
                    mode
                )
            )


# ============================================================
# BET
# ============================================================

class BetModal(
    discord.ui.Modal,
    title="💰 Nhập tiền cược"
):

    amount = discord.ui.TextInput(
        label="Số tiền ảo",
        placeholder="VD: 10000",
        required=True,
        max_length=12
    )

    def __init__(self, fixture):

        super().__init__()

        self.fixture = fixture


    async def on_submit(
        self,
        interaction
    ):

        try:

            amount = int(
                str(self.amount)
                .replace(".", "")
                .replace(",", "")
            )

        except ValueError:

            await interaction.response.send_message(
                "❌ Số tiền không hợp lệ.",
                ephemeral=True
            )

            return

        if amount <= 0:

            await interaction.response.send_message(
                "❌ Tiền cược phải lớn hơn 0.",
                ephemeral=True
            )

            return

        if amount > 10_000_000:

            await interaction.response.send_message(
                "❌ Tối đa 10.000.000 xu.",
                ephemeral=True
            )

            return

        if amount > get_balance(
            interaction.user.id
        ):

            await interaction.response.send_message(
                "❌ Không đủ tiền ảo.",
                ephemeral=True
            )

            return

        embed = discord.Embed(
            title="🎯 CHỌN CỬA",
            description=(
                f"⚽ **{team_name(self.fixture,'home')}**\n"
                f"🆚\n"
                f"**{team_name(self.fixture,'away')}**\n\n"
                f"💰 Cược: **{money(amount)} xu**"
            ),
            color=discord.Color.gold()
        )

        await interaction.response.send_message(
            embed=embed,
            view=BetChoiceView(
                self.fixture,
                amount
            ),
            ephemeral=True
        )


class BetChoiceView(
    discord.ui.View
):

    def __init__(
        self,
        fixture,
        stake
    ):

        super().__init__(
            timeout=120
        )

        self.fixture = fixture
        self.stake = stake


    async def place(
        self,
        interaction,
        choice
    ):

        user_id = interaction.user.id

        fid = (
            self.fixture.get("fixture")
            or {}
        ).get("id")

        if not fid:

            await interaction.response.send_message(
                "❌ Fixture không hợp lệ.",
                ephemeral=True
            )

            return

        balance = get_balance(
            user_id
        )

        if self.stake > balance:

            await interaction.response.send_message(
                "❌ Không đủ tiền.",
                ephemeral=True
            )

            return

        with db() as conn:

            exists = conn.execute(
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

            if exists:

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
                    self.stake,
                    user_id
                )
            )

            conn.execute(
                """
                INSERT INTO bets
                (user_id, fixture_id, choice, stake, status, created_at)
                VALUES (?, ?, ?, ?, 'open', ?)
                """,
                (
                    user_id,
                    fid,
                    choice,
                    self.stake,
                    datetime.now(TZ).isoformat()
                )
            )

            conn.commit()

        await interaction.response.send_message(
            (
                f"✅ Đã cược **{money(self.stake)} xu** "
                f"vào **{choice_name(choice,self.fixture)}**.\n"
                f"💰 Còn lại: **{money(get_balance(user_id))} xu**"
            ),
            ephemeral=True
        )


    @discord.ui.button(
        label="Chủ nhà",
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
        label="Hòa",
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
        label="Đội khách",
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


# ============================================================
# /CUOC - TẤT CẢ GIẢI
# ============================================================

class CuocLeagueSelect(
    discord.ui.Select
):

    def __init__(self):

        options = []

        for league_id, data in LEAGUES.items():

            emoji, name = data

            options.append(
                discord.SelectOption(
                    label=name,
                    value=str(league_id),
                    emoji=emoji
                )
            )

        super().__init__(
            placeholder="🏆 Chọn giải để cược...",
            options=options[:25]
        )


    async def callback(
        self,
        interaction
    ):

        league_id = int(
            self.values[0]
        )

        emoji, league_name = LEAGUES[
            league_id
        ]

        await interaction.response.defer(
            ephemeral=True
        )

        try:

            fixtures = await football.league_next(
                league_id,
                20
            )

            fixtures = [
                x for x in fixtures
                if is_upcoming(x)
            ]

            fixtures.sort(
                key=lambda x:
                (
                    x.get("fixture") or {}
                ).get("timestamp", 0)
            )

            if not fixtures:

                await interaction.followup.send(
                    f"❌ Chưa có trận sắp tới trong **{league_name}**.",
                    ephemeral=True
                )

                return

            embed = discord.Embed(
                title=f"{emoji} {league_name}",
                description=(
                    f"📅 Có **{len(fixtures)}** trận sắp tới.\n"
                    "Chọn trận để đặt cược."
                ),
                color=discord.Color.gold()
            )

            await interaction.followup.send(
                embed=embed,
                view=MatchView(
                    fixtures,
                    league_name,
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
            timeout=180
        )

        self.add_item(
            CuocLeagueSelect()
        )


    @discord.ui.button(
        label="🔵 Các trận Man City sắp tới",
        style=discord.ButtonStyle.primary,
        row=1
    )
    async def city(
        self,
        interaction,
        button
    ):

        await interaction.response.defer(
            ephemeral=True
        )

        try:

            fixtures = await football.team_next(
                MANCHESTER_CITY_TEAM_ID,
                20
            )

            fixtures = [
                x for x in fixtures
                if is_upcoming(x)
            ]

            if not fixtures:

                await interaction.followup.send(
                    "❌ Không có trận Man City sắp tới.",
                    ephemeral=True
                )

                return

            embed = discord.Embed(
                title="🔵 MAN CITY",
                description=(
                    f"⚽ **{len(fixtures)}** trận sắp tới\n"
                    "Không phân biệt giải đấu."
                ),
                color=discord.Color.blue()
            )

            await interaction.followup.send(
                embed=embed,
                view=MatchView(
                    fixtures,
                    "Man City",
                    "bet"
                ),
                ephemeral=True
            )

        except Exception as exc:

            await interaction.followup.send(
                embed=api_error(exc),
                ephemeral=True
            )


# ============================================================
# /SOI - TẤT CẢ GIẢI + MAN CITY
# ============================================================

class SoiLeagueSelect(
    discord.ui.Select
):

    def __init__(self):

        options = []

        for league_id, data in LEAGUES.items():

            emoji, name = data

            options.append(
                discord.SelectOption(
                    label=name,
                    value=str(league_id),
                    emoji=emoji
                )
            )

        super().__init__(
            placeholder="🏆 Chọn giải để soi...",
            options=options[:25]
        )


    async def callback(
        self,
        interaction
    ):

        league_id = int(
            self.values[0]
        )

        emoji, league_name = LEAGUES[
            league_id
        ]

        await interaction.response.defer(
            ephemeral=True
        )

        try:

            fixtures = await football.league_next(
                league_id,
                20
            )

            fixtures = [
                x for x in fixtures
                if is_upcoming(x)
            ]

            fixtures.sort(
                key=lambda x:
                (
                    x.get("fixture") or {}
                ).get("timestamp", 0)
            )

            if not fixtures:

                await interaction.followup.send(
                    f"❌ Không có trận sắp tới trong **{league_name}**.",
                    ephemeral=True
                )

                return

            embed = discord.Embed(
                title=f"{emoji} {league_name}",
                description=(
                    f"⚽ Có **{len(fixtures)}** trận.\n"
                    "Chọn trận để soi tỷ số."
                ),
                color=discord.Color.blurple()
            )

            await interaction.followup.send(
                embed=embed,
                view=MatchView(
                    fixtures,
                    league_name,
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
    async def city(
        self,
        interaction,
        button
    ):

        await interaction.response.defer(
            ephemeral=True
        )

        try:

            fixtures = await football.team_next(
                MANCHESTER_CITY_TEAM_ID,
                20
            )

            fixtures = [
                x for x in fixtures
                if is_upcoming(x)
            ]

            if not fixtures:

                await interaction.followup.send(
                    "❌ Không có trận Man City sắp tới.",
                    ephemeral=True
                )

                return

            embed = discord.Embed(
                title="🔵 MAN CITY — TẤT CẢ TRẬN",
                description=(
                    f"⚽ **{len(fixtures)}** trận sắp tới\n"
                    "Không phân biệt giải."
                ),
                color=discord.Color.blue()
            )

            await interaction.followup.send(
                embed=embed,
                view=MatchView(
                    fixtures,
                    "Man City",
                    "soi"
                ),
                ephemeral=True
            )

        except Exception as exc:

            await interaction.followup.send(
                embed=api_error(exc),
                ephemeral=True
            )


# ============================================================
# COMMAND /SOI
# ============================================================

@bot.tree.command(
    name="soi",
    description="Soi tất cả giải và các trận Man City",
    guild=GUILD
)
async def soi(
    interaction: discord.Interaction
):

    embed = discord.Embed(
        title="⚽ SOI BÓNG ĐÁ",
        description=(
            "Chọn **một giải** để xem tất cả trận "
            "hoặc bấm **🔵 Man City** để xem toàn bộ trận của Man City."
        ),
        color=discord.Color.green()
    )

    embed.add_field(
        name="🏆 CÁC GIẢI",
        value=(
            "🏴 Premier League\n"
            "🏆 Champions League\n"
            "🇪🇸 La Liga\n"
            "🇮🇹 Serie A\n"
            "🇩🇪 Bundesliga\n"
            "🇫🇷 Ligue 1\n"
            "🇳🇱 Eredivisie\n"
            "🇵🇹 Primeira Liga\n"
            "🏴 Championship\n"
            "🏆 FA Cup\n"
            "🏆 Carabao Cup"
        ),
        inline=True
    )

    embed.add_field(
        name="🔵 MAN CITY",
        value=(
            "Tất cả trận sắp tới\n"
            "Không phân biệt giải\n\n"
            "👉 Bấm nút **Man City**"
        ),
        inline=True
    )

    embed.set_footer(
        text="Dữ liệu từ API-Football"
    )

    await interaction.response.send_message(
        embed=embed,
        view=SoiHomeView()
    )


# ============================================================
# COMMAND /CUOC
# ============================================================

@bot.tree.command(
    name="cuoc",
    description="Cược tất cả giải hoặc Man City",
    guild=GUILD
)
async def cuoc(
    interaction: discord.Interaction
):

    embed = discord.Embed(
        title="🎰 CƯỢC BÓNG ĐÁ",
        description=(
            "Chọn **giải đấu** để xem các trận "
            "hoặc bấm **🔵 Các trận Man City sắp tới**."
        ),
        color=discord.Color.gold()
    )

    embed.add_field(
        name="🏆 TẤT CẢ GIẢI",
        value=(
            "Premier League • Champions League\n"
            "La Liga • Serie A • Bundesliga\n"
            "Ligue 1 • Eredivisie • Primeira Liga\n"
            "Championship • FA Cup • Carabao Cup"
        ),
        inline=False
    )

    embed.add_field(
        name="🔵 MAN CITY",
        value=(
            "Tất cả trận sắp tới của Man City,\n"
            "không phân biệt giải."
        ),
        inline=False
    )

    await interaction.response.send_message(
        embed=embed,
        view=CuocHomeView(),
        ephemeral=True
    )


# ============================================================
# /KHOINGHIEP
# ============================================================

@bot.tree.command(
    name="khoinghiep",
    description="Nhận 1.000 xu khởi nghiệp",
    guild=GUILD
)
async def khoinghiep(
    interaction: discord.Interaction
):

    user_id = interaction.user.id

    ensure_wallet(user_id)

    with db() as conn:

        row = conn.execute(
            "SELECT started FROM wallets WHERE user_id=?",
            (user_id,)
        ).fetchone()

        if row["started"]:

            await interaction.response.send_message(
                "❌ M đã nhận 1.000 xu khởi nghiệp rồi.",
                ephemeral=True
            )

            return

        conn.execute(
            "UPDATE wallets SET started=1 WHERE user_id=?",
            (user_id,)
        )

        conn.commit()

    await add_money(
        user_id,
        STARTER_BONUS,
        interaction.channel
    )

    await interaction.response.send_message(
        f"🚀 Nhận **{money(STARTER_BONUS)} xu** khởi nghiệp!\n"
        f"💰 Ví: **{money(get_balance(user_id))} xu**"
    )


# ============================================================
# /COMAT
# ============================================================

@bot.tree.command(
    name="comat",
    description="Nhận 50.000 xu mỗi ngày",
    guild=GUILD
)
async def comat(
    interaction: discord.Interaction
):

    user_id = interaction.user.id

    ensure_wallet(user_id)

    today = datetime.now(
        TZ
    ).date().isoformat()

    with db() as conn:

        exists = conn.execute(
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

        if exists:

            await interaction.response.send_message(
                "❌ Hôm nay m đã nhận **50.000 xu** rồi.",
                ephemeral=True
            )

            return

        conn.execute(
            """
            INSERT INTO daily_claims
            (user_id, claim_date)
            VALUES (?,?)
            """,
            (
                user_id,
                today
            )
        )

        conn.commit()

    await add_money(
        user_id,
        DAILY_BONUS,
        interaction.channel
    )

    await interaction.response.send_message(
        f"💰 Nhận **{money(DAILY_BONUS)} xu** hôm nay!\n"
        f"👛 Ví: **{money(get_balance(user_id))} xu**"
    )


# ============================================================
# /VI
# ============================================================

@bot.tree.command(
    name="vi",
    description="Xem số dư và VIP",
    guild=GUILD
)
async def vi(
    interaction: discord.Interaction
):

    user_id = interaction.user.id

    balance = get_balance(
        user_id
    )

    level = get_vip(
        balance
    )

    await check_vip(
        user_id
    )

    with db() as conn:

        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS pending
            FROM bets
            WHERE user_id=?
            """,
            (user_id,)
        ).fetchone()

    next_vip = None

    for lv, minimum in reversed(VIP_LEVELS):

        if minimum > balance:

            next_vip = (
                f"{vip_name(lv)} — "
                f"{money(minimum)} xu"
            )

            break

    if not next_vip:

        next_vip = "👑 Đã đạt VIP 10"

    embed = discord.Embed(
        title="👛 VÍ CỦA BẠN",
        color=discord.Color.gold()
    )

    embed.add_field(
        name="💰 Số dư",
        value=f"**{money(balance)} xu**",
        inline=False
    )

    embed.add_field(
        name="👑 VIP",
        value=(
            f"{vip_icon(level)} "
            f"**{vip_name(level)}**"
        ),
        inline=True
    )

    embed.add_field(
        name="🎯 Tổng cược",
        value=f"**{row['total'] or 0}**",
        inline=True
    )

    embed.add_field(
        name="⏳ Cược đang chờ",
        value=f"**{row['pending'] or 0}**",
        inline=True
    )

    embed.add_field(
        name="📈 VIP tiếp theo",
        value=next_vip,
        inline=False
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# ============================================================
# /ADMIN
# ============================================================

@bot.tree.command(
    name="admin",
    description="Cộng hoặc trừ xu",
    guild=GUILD
)
@app_commands.describe(
    user="Thành viên",
    amount="Số xu (+ cộng / - trừ)",
    password="Mật khẩu admin"
)
async def admin(
    interaction: discord.Interaction,
    user: discord.Member,
    amount: int,
    password: str
):

    admin_password = os.getenv(
        "ADMIN_PASSWORD",
        "provip👑"
    )

    if password != admin_password:

        await interaction.response.send_message(
            "❌ Sai mật khẩu admin.",
            ephemeral=True
        )

        return

    if amount == 0:

        await interaction.response.send_message(
            "❌ Số xu phải khác 0.",
            ephemeral=True
        )

        return

    if amount > 0:

        await add_money(
            user.id,
            amount,
            interaction.channel
        )

    else:

        await remove_money(
            user.id,
            abs(amount)
        )

    balance = get_balance(
        user.id
    )

    level = get_vip(
        balance
    )

    await interaction.response.send_message(
        f"👑 Đã cập nhật **{user.display_name}**\n"
        f"💰 {amount:+,} xu\n"
        f"👛 Ví: **{money(balance)} xu**\n"
        f"{vip_icon(level)} **{vip_name(level)}**",
        ephemeral=True
    )


# ============================================================
# /KETTOAN
# ============================================================

@bot.tree.command(
    name="kettoan",
    description="Kết toán các cược đã có kết quả",
    guild=GUILD
)
async def kettoan(
    interaction: discord.Interaction
):

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
            LIMIT 100
            """
        ).fetchall()

    if not bets:

        await interaction.followup.send(
            "ℹ️ Không có cược đang chờ.",
            ephemeral=True
        )

        return

    settled = 0
    skipped = 0

    for bet in bets:

        try:

            data = await football.fixture(
                int(bet["fixture_id"])
            )

            fixtures = data.get(
                "response",
                []
            )

            if not fixtures:

                skipped += 1
                continue

            fixture = fixtures[0]

            result = result_of(
                fixture
            )

            if result is None:

                skipped += 1
                continue

            if result == bet["choice"]:

                # Cược thắng nhận lại 2x tiền cược
                await add_money(
                    int(bet["user_id"]),
                    int(bet["stake"]) * 2,
                    interaction.channel
                )

                status = "won"

            else:

                status = "lost"

            with db() as conn:

                conn.execute(
                    """
                    UPDATE bets
                    SET status=?
                    WHERE id=?
                    """,
                    (
                        status,
                        bet["id"]
                    )
                )

                conn.commit()

            settled += 1

        except Exception:

            skipped += 1

    await interaction.followup.send(
        f"✅ Đã kết toán **{settled}** cược.\n"
        f"⏳ Chưa thể kết toán: **{skipped}**.",
        ephemeral=True
    )


# ============================================================
# /CADO
# ============================================================

@bot.tree.command(
    name="cado",
    description="Đặt cược bằng fixture ID",
    guild=GUILD
)
@app_commands.describe(
    fixture_id="ID trận từ API-Football",
    stake="Số tiền ảo"
)
async def cado(
    interaction: discord.Interaction,
    fixture_id: int,
    stake: int
):

    if stake <= 0:

        await interaction.response.send_message(
            "❌ Tiền cược phải lớn hơn 0.",
            ephemeral=True
        )

        return

    if stake > 10_000_000:

        await interaction.response.send_message(
            "❌ Tối đa 10.000.000 xu.",
            ephemeral=True
        )

        return

    await interaction.response.defer(
        ephemeral=True
    )

    try:

        data = await football.fixture(
            fixture_id
        )

        fixtures = data.get(
            "response",
            []
        )

        if not fixtures:

            await interaction.followup.send(
                "❌ Không tìm thấy trận.",
                ephemeral=True
            )

            return

        fixture = fixtures[0]

        if not is_upcoming(fixture):

            await interaction.followup.send(
                "❌ Trận này đã bắt đầu hoặc kết thúc.",
                ephemeral=True
            )

            return

        if stake > get_balance(
            interaction.user.id
        ):

            await interaction.followup.send(
                "❌ Không đủ tiền.",
                ephemeral=True
            )

            return

        await interaction.followup.send(
            embed=discord.Embed(
                title="🎯 CHỌN CỬA",
                description=(
                    f"**{team_name(fixture,'home')}**\n"
                    "🆚\n"
                    f"**{team_name(fixture,'away')}**\n\n"
                    f"💰 **{money(stake)} xu**"
                ),
                color=discord.Color.gold()
            ),
            view=BetChoiceView(
                fixture,
                stake
            ),
            ephemeral=True
        )

    except Exception as exc:

        await interaction.followup.send(
            embed=api_error(exc),
            ephemeral=True
        )


# ============================================================
# /PING
# ============================================================

@bot.tree.command(
    name="ping",
    description="Kiểm tra bot",
    guild=GUILD
)
async def ping(
    interaction: discord.Interaction
):

    await interaction.response.send_message(
        f"🏓 Pong! `{round(bot.latency * 1000)}ms`",
        ephemeral=True
    )


# ============================================================
# /APIQUOTA
# ============================================================

@bot.tree.command(
    name="apiquota",
    description="Xem quota API",
    guild=GUILD
)
async def apiquota(
    interaction: discord.Interaction
):

    remaining = (
        football.last_quota
        or "chưa nhận"
    )

    await interaction.response.send_message(
        f"📡 API-Football requests remaining: **{remaining}**",
        ephemeral=True
    )


# ============================================================
# RAILWAY HEALTH SERVER
# ============================================================

async def health_handler(
    request
):

    return web.Response(
        text="OK"
    )


async def start_health_server():

    port = int(
        os.getenv(
            "PORT",
            "8080"
        )
    )

    app = web.Application()

    app.router.add_get(
        "/",
        health_handler
    )

    app.router.add_get(
        "/health",
        health_handler
    )

    runner = web.AppRunner(
        app
    )

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        port
    )

    await site.start()

    print(
        f"[WEB] Health server : {port}"
    )

    return runner


# ============================================================
# DISCORD READY
# ============================================================

@bot.event
async def on_ready():

    print(
        f"[DISCORD] Logged in as {bot.user}"
    )

    print(
        f"[DISCORD] Guild: {GUILD_ID}"
    )


# ============================================================
# MAIN
# ============================================================

async def main():

    init_db()

    await football.start()

    await start_health_server()

    try:

        async with bot:

            await bot.login(
                DISCORD_TOKEN
            )

            synced = await bot.tree.sync(
                guild=GUILD
            )

            print(
                f"[DISCORD] Synced {len(synced)} commands."
            )

            await bot.connect()

    finally:

        await football.close()


if __name__ == "__main__":

    asyncio.run(main())

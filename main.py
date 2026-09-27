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

FOOTBALL_API_KEY = (
    os.getenv("FOOTBALL_API_KEY", "").strip()
    or os.getenv("API_FOOTBALL_KEY", "").strip()
    or os.getenv("FOOTBALL_KEY", "").strip()
    or os.getenv("API_KEY", "").strip()
)

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

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "provip👑")


# ============================================================
# LEAGUES
# ============================================================

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


# ============================================================
# CHECK ENV
# ============================================================

if not DISCORD_TOKEN:
    raise RuntimeError("Thiếu DISCORD_TOKEN trong Railway Variables.")

if not FOOTBALL_API_KEY:
    raise RuntimeError("Thiếu FOOTBALL_API_KEY trong Railway Variables.")


# ============================================================
# DISCORD BOT
# ============================================================

intents = discord.Intents.default()

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


# ============================================================
# DATABASE
# ============================================================

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_connect() as conn:

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
                PRIMARY KEY (user_id, claim_date)
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

        # Migration database cũ
        try:
            conn.execute(
                "ALTER TABLE wallets ADD COLUMN started INTEGER NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass

        try:
            conn.execute(
                "ALTER TABLE wallets ADD COLUMN vip INTEGER NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass

        conn.commit()


# ============================================================
# WALLET
# ============================================================

def ensure_wallet(user_id: int):

    with db_connect() as conn:

        row = conn.execute(
            "SELECT * FROM wallets WHERE user_id = ?",
            (user_id,)
        ).fetchone()

        if row is None:

            conn.execute(
                """
                INSERT INTO wallets(user_id, balance, started, vip)
                VALUES (?, ?, 0, 0)
                """,
                (user_id, STARTING_BALANCE)
            )

            conn.commit()

            return STARTING_BALANCE

        return int(row["balance"])


def get_balance(user_id: int) -> int:
    return ensure_wallet(user_id)


def get_vip(balance: int) -> int:

    for level, minimum in VIP_LEVELS:

        if balance >= minimum:
            return level

    return 0


def get_vip_name(level: int) -> str:

    if level <= 0:
        return "Thành viên"

    return f"VIP {level}"


def get_next_vip(level: int):

    for lv, minimum in sorted(VIP_LEVELS):

        if lv > level:
            return lv, minimum

    return None, None


def fmt_money(value: int) -> str:
    return f"{int(value):,}".replace(",", ".")


async def update_vip(user_id: int, channel=None):

    with db_connect() as conn:

        row = conn.execute(
            "SELECT balance, vip FROM wallets WHERE user_id = ?",
            (user_id,)
        ).fetchone()

        if row is None:
            return

        balance = int(row["balance"])
        old_vip = int(row["vip"])
        new_vip = get_vip(balance)

        if new_vip == old_vip:
            return

        conn.execute(
            "UPDATE wallets SET vip = ? WHERE user_id = ?",
            (new_vip, user_id)
        )

        conn.commit()

    # Chỉ thông báo khi tăng VIP
    if new_vip > old_vip and channel is not None:

        try:

            user = bot.get_user(user_id)

            name = user.display_name if user else str(user_id)

            embed = discord.Embed(
                title="🎉 CHÚC MỪNG THĂNG VIP 🎉",
                description=(
                    "👑━━━━━━━━━━━━━━━━━━👑\n"
                    f"👤 **{name}**\n\n"
                    f"💰 Số dư: **{fmt_money(balance)} xu**\n"
                    f"⭐ Cấp mới: **VIP {new_vip}**\n"
                    "👑━━━━━━━━━━━━━━━━━━👑"
                ),
                color=discord.Color.gold()
            )

            await channel.send(embed=embed)

        except Exception:
            pass


async def add_balance(user_id: int, amount: int, channel=None):

    ensure_wallet(user_id)

    with db_connect() as conn:

        conn.execute(
            """
            UPDATE wallets
            SET balance = balance + ?
            WHERE user_id = ?
            """,
            (amount, user_id)
        )

        conn.commit()

    await update_vip(user_id, channel)


async def remove_balance(user_id: int, amount: int):

    ensure_wallet(user_id)

    with db_connect() as conn:

        conn.execute(
            """
            UPDATE wallets
            SET balance = MAX(balance - ?, 0)
            WHERE user_id = ?
            """,
            (amount, user_id)
        )

        conn.commit()

    await update_vip(user_id)


# ============================================================
# FOOTBALL API
# ============================================================

class FootballAPI:

    def __init__(self):

        self.session: Optional[aiohttp.ClientSession] = None
        self.cache = {}
        self.cache_ttl = 30
        self.remaining = None

    async def start(self):

        if self.session is None or self.session.closed:

            timeout = aiohttp.ClientTimeout(total=20)

            self.session = aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    "x-apisports-key": FOOTBALL_API_KEY,
                    "Accept": "application/json"
                }
            )

    async def close(self):

        if self.session and not self.session.closed:
            await self.session.close()

        self.session = None

    async def request(self, endpoint: str, params: dict):

        await self.start()

        cache_key = (
            endpoint,
            tuple(sorted(
                (str(k), str(v))
                for k, v in params.items()
            ))
        )

        now = time.monotonic()

        cached = self.cache.get(cache_key)

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

                    text = await response.text()

                    try:
                        data = json.loads(text)
                    except Exception:
                        data = {}

                    remaining = response.headers.get(
                        "x-ratelimit-requests-remaining"
                    )

                    if remaining:
                        self.remaining = remaining

                    if response.status >= 500:

                        if attempt < 2:
                            await asyncio.sleep(1)
                            continue

                    if response.status >= 400:

                        raise RuntimeError(
                            f"API HTTP {response.status}: "
                            f"{text[:300]}"
                        )

                    errors = data.get("errors")

                    if isinstance(errors, dict):

                        real_errors = {
                            k: v
                            for k, v in errors.items()
                            if v
                        }

                        if real_errors:
                            raise RuntimeError(str(real_errors))

                    elif isinstance(errors, list) and errors:

                        raise RuntimeError(str(errors))

                    self.cache[cache_key] = (
                        time.monotonic(),
                        data
                    )

                    return data

            except Exception as exc:

                last_error = exc

                if attempt < 2:
                    await asyncio.sleep(1)

        raise RuntimeError(str(last_error))


    async def next_team(self, team_id=50, count=20):

        data = await self.request(
            "fixtures",
            {
                "team": team_id,
                "next": count,
                "timezone": TIMEZONE
            }
        )

        return data.get("response", [])


    async def next_league(self, league_id, count=20):

        data = await self.request(
            "fixtures",
            {
                "league": league_id,
                "next": count,
                "timezone": TIMEZONE
            }
        )

        return data.get("response", [])


    async def predictions(self, fixture_id):

        data = await self.request(
            "predictions",
            {
                "fixture": fixture_id
            }
        )

        return data.get("response", [])


    async def fixture(self, fixture_id):

        data = await self.request(
            "fixtures",
            {
                "id": fixture_id
            }
        )

        return data.get("response", [])


football = FootballAPI()


# ============================================================
# HELPERS
# ============================================================

def fixture_status(fixture):

    return (
        fixture.get("fixture", {})
        .get("status", {})
        .get("short", "")
    )


def is_upcoming(fixture):

    return fixture_status(fixture) in {
        "NS",
        "TBD"
    }


def fixture_time(fixture):

    raw = fixture.get("fixture", {}).get("date")

    if not raw:
        return "Chưa có giờ"

    try:

        dt = datetime.fromisoformat(
            raw.replace("Z", "+00:00")
        ).astimezone(TZ)

        return dt.strftime(
            "%d/%m/%Y %H:%M"
        )

    except Exception:

        return str(raw)


def team_name(fixture, side):

    return (
        fixture.get("teams", {})
        .get(side, {})
        .get("name")
        or "?"
    )


def fixture_id(fixture):

    return fixture.get("fixture", {}).get("id")


def league_name(league_id):

    return LEAGUES.get(
        str(league_id),
        ("🏆", "Giải đấu")
    )[1]


def league_emoji(league_id):

    return LEAGUES.get(
        str(league_id),
        ("🏆", "Giải đấu")
    )[0]


def prediction_score(prediction):

    score = prediction.get("score") or {}

    home = score.get("halftime", {}).get("home")
    away = score.get("halftime", {}).get("away")

    predicted = prediction.get("score") or {}

    if (
        isinstance(predicted.get("home"), int)
        and isinstance(predicted.get("away"), int)
    ):
        return (
            predicted["home"],
            predicted["away"]
        )

    goals = prediction.get("goals") or {}

    if (
        isinstance(goals.get("home"), int)
        and isinstance(goals.get("away"), int)
    ):
        return (
            goals["home"],
            goals["away"]
        )

    return None


def choice_name(choice, fixture):

    if choice == "home":
        return team_name(fixture, "home")

    if choice == "away":
        return team_name(fixture, "away")

    return "Hòa"


# ============================================================
# MATCH ANALYSIS
# ============================================================

async def show_analysis(
    interaction,
    fixture,
    title_prefix="🔮"
):

    fid = fixture_id(fixture)

    home = team_name(fixture, "home")
    away = team_name(fixture, "away")

    league = (
        fixture.get("league", {})
        .get("name")
        or "Giải đấu"
    )

    embed = discord.Embed(
        title=f"{title_prefix} {home} vs {away}",
        description=(
            f"🏆 **{league}**\n"
            f"📅 **{fixture_time(fixture)}**\n"
            f"🆔 Fixture: `{fid}`"
        ),
        color=discord.Color.blurple()
    )

    try:

        predictions = await football.predictions(
            int(fid)
        )

        if not predictions:

            embed.add_field(
                name="🎯 Dự đoán",
                value="API chưa có dữ liệu dự đoán.",
                inline=False
            )

        else:

            prediction = (
                predictions[0]
                .get("predictions")
                or {}
            )

            winner = prediction.get(
                "winner"
            ) or {}

            winner_name = (
                winner.get("name")
                or "Chưa xác định"
            )

            percent = (
                prediction.get("percent")
                or {}
            )

            hp = percent.get("home", "?")
            dp = percent.get("draw", "?")
            ap = percent.get("away", "?")

            exact = prediction_score(
                prediction
            )

            if exact:

                exact_text = (
                    f"**{exact[0]} - {exact[1]}**"
                )

            else:

                exact_text = (
                    "API chưa trả tỷ số chính xác"
                )

            embed.add_field(
                name="🎯 Dự đoán tỷ số",
                value=exact_text,
                inline=False
            )

            embed.add_field(
                name="📈 Xác suất",
                value=(
                    f"🏠 Home **{hp}%**\n"
                    f"🤝 Draw **{dp}%**\n"
                    f"✈️ Away **{ap}%**"
                ),
                inline=False
            )

            embed.add_field(
                name="🏁 Kết luận API",
                value=f"**{winner_name}**",
                inline=False
            )

    except Exception as exc:

        embed.add_field(
            name="⚠️ Dự đoán",
            value=f"`{str(exc)[:500]}`",
            inline=False
        )

    embed.set_footer(
        text="API-Football • Dự đoán chỉ mang tính tham khảo"
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
        mode,
        league_title
    ):

        self.fixtures = {
            str(fixture_id(f)): f
            for f in fixtures
            if fixture_id(f)
        }

        self.mode = mode
        self.league_title = league_title

        options = []

        for f in fixtures[:25]:

            fid = fixture_id(f)

            home = team_name(f, "home")
            away = team_name(f, "away")

            options.append(
                discord.SelectOption(
                    label=f"{home} vs {away}"[:100],
                    value=str(fid),
                    description=fixture_time(f)[:100]
                )
            )

        super().__init__(
            placeholder="⚽ Chọn trận...",
            options=options,
            min_values=1,
            max_values=1
        )


    async def callback(self, interaction):

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
                BetStakeModal(fixture)
            )

        else:

            await interaction.response.defer(
                ephemeral=True
            )

            await show_analysis(
                interaction,
                fixture
            )


class MatchView(discord.ui.View):

    def __init__(
        self,
        fixtures,
        mode,
        league_title
    ):

        super().__init__(timeout=180)

        self.add_item(
            MatchSelect(
                fixtures,
                mode,
                league_title
            )
        )


# ============================================================
# BETTING
# ============================================================

class BetStakeModal(
    discord.ui.Modal,
    title="💰 Nhập tiền cược"
):

    stake = discord.ui.TextInput(
        label="Số tiền cược",
        placeholder="VD: 10000",
        min_length=1,
        max_length=12,
        required=True
    )

    def __init__(self, fixture):

        super().__init__()

        self.fixture = fixture


    async def on_submit(self, interaction):

        try:

            amount = int(
                str(self.stake)
                .replace(".", "")
                .replace(",", "")
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
                "❌ Tiền cược phải lớn hơn 0.",
                ephemeral=True
            )

            return

        if amount > 10_000_000:

            await interaction.response.send_message(
                "❌ Tối đa 10.000.000 xu/lần.",
                ephemeral=True
            )

            return

        balance = get_balance(
            interaction.user.id
        )

        if amount > balance:

            await interaction.response.send_message(
                f"❌ Không đủ xu.\n"
                f"💰 Ví: **{fmt_money(balance)}**",
                ephemeral=True
            )

            return

        embed = discord.Embed(
            title="🎯 CHỌN CỬA",
            description=(
                f"⚽ **{team_name(self.fixture, 'home')}**\n"
                f"🆚\n"
                f"**{team_name(self.fixture, 'away')}**\n\n"
                f"💰 Tiền cược: **{fmt_money(amount)} xu**"
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


class BetChoiceView(discord.ui.View):

    def __init__(
        self,
        fixture,
        stake
    ):

        super().__init__(timeout=120)

        self.fixture = fixture
        self.stake = stake


    async def place(
        self,
        interaction,
        choice
    ):

        user_id = interaction.user.id
        fid = fixture_id(self.fixture)

        balance = get_balance(user_id)

        if self.stake > balance:

            await interaction.response.send_message(
                "❌ Không đủ xu.",
                ephemeral=True
            )

            return

        with db_connect() as conn:

            existing = conn.execute(
                """
                SELECT id
                FROM bets
                WHERE user_id = ?
                AND fixture_id = ?
                AND status = 'open'
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
                SET balance = balance - ?
                WHERE user_id = ?
                """,
                (
                    self.stake,
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
                "✅ **ĐẶT CƯỢC THÀNH CÔNG**\n\n"
                f"⚽ {team_name(self.fixture, 'home')} "
                f"vs {team_name(self.fixture, 'away')}\n"
                f"🎯 Cửa: **{choice_name(choice, self.fixture)}**\n"
                f"💰 Cược: **{fmt_money(self.stake)} xu**\n"
                f"💳 Còn: **{fmt_money(get_balance(user_id))} xu**"
            ),
            ephemeral=True
        )


    @discord.ui.button(
        label="🏠 Chủ nhà",
        style=discord.ButtonStyle.primary
    )
    async def home(self, interaction, button):

        await self.place(
            interaction,
            "home"
        )


    @discord.ui.button(
        label="🤝 Hòa",
        style=discord.ButtonStyle.secondary
    )
    async def draw(self, interaction, button):

        await self.place(
            interaction,
            "draw"
        )


    @discord.ui.button(
        label="✈️ Đội khách",
        style=discord.ButtonStyle.success
    )
    async def away(self, interaction, button):

        await self.place(
            interaction,
            "away"
        )


# ============================================================
# CUOC HOME
# ============================================================

class CuocLeagueSelect(discord.ui.Select):

    def __init__(self):

        options = []

        for lid, (emoji, name) in LEAGUES.items():

            options.append(
                discord.SelectOption(
                    label=name,
                    value=lid,
                    emoji=emoji
                )
            )

        super().__init__(
            placeholder="🏆 Chọn giải để cược...",
            options=options
        )


    async def callback(self, interaction):

        lid = int(self.values[0])
        name = league_name(lid)

        await interaction.response.defer(
            ephemeral=True
        )

        try:

            fixtures = await football.next_league(
                lid,
                20
            )

            fixtures = [
                f for f in fixtures
                if is_upcoming(f)
            ]

            fixtures.sort(
                key=lambda x:
                x.get("fixture", {})
                .get("timestamp", 0)
            )

            if not fixtures:

                await interaction.followup.send(
                    f"❌ Chưa có trận sắp tới trong **{name}**.",
                    ephemeral=True
                )

                return

            embed = discord.Embed(
                title=f"{league_emoji(lid)} {name}",
                description=(
                    f"Chọn trận để cược.\n"
                    f"📊 Có **{len(fixtures)}** trận."
                ),
                color=discord.Color.green()
            )

            await interaction.followup.send(
                embed=embed,
                view=MatchView(
                    fixtures,
                    "bet",
                    name
                ),
                ephemeral=True
            )

        except Exception as exc:

            await interaction.followup.send(
                f"❌ API lỗi:\n`{str(exc)[:800]}`",
                ephemeral=True
            )


class CuocView(discord.ui.View):

    def __init__(self):

        super().__init__(timeout=300)

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
                f for f in fixtures
                if is_upcoming(f)
            ]

            if not fixtures:

                await interaction.followup.send(
                    "❌ Không có trận Man City sắp tới.",
                    ephemeral=True
                )

                return

            embed = discord.Embed(
                title="🔵 MAN CITY — SẮP TỚI",
                description=(
                    f"Có **{len(fixtures)}** trận tiếp theo."
                ),
                color=discord.Color.blue()
            )

            await interaction.followup.send(
                embed=embed,
                view=MatchView(
                    fixtures,
                    "bet",
                    "Man City"
                ),
                ephemeral=True
            )

        except Exception as exc:

            await interaction.followup.send(
                f"❌ API lỗi:\n`{str(exc)[:800]}`",
                ephemeral=True
            )


# ============================================================
# SOI HOME
# ============================================================

class SoiLeagueSelect(discord.ui.Select):

    def __init__(self):

        options = []

        for lid, (emoji, name) in LEAGUES.items():

            options.append(
                discord.SelectOption(
                    label=name,
                    value=lid,
                    emoji=emoji
                )
            )

        super().__init__(
            placeholder="🏆 Chọn giải để soi...",
            options=options
        )


    async def callback(self, interaction):

        lid = int(self.values[0])
        name = league_name(lid)

        await interaction.response.defer(
            ephemeral=True
        )

        try:

            fixtures = await football.next_league(
                lid,
                20
            )

            fixtures = [
                f for f in fixtures
                if is_upcoming(f)
            ]

            fixtures.sort(
                key=lambda x:
                x.get("fixture", {})
                .get("timestamp", 0)
            )

            if not fixtures:

                await interaction.followup.send(
                    f"❌ Chưa có trận trong **{name}**.",
                    ephemeral=True
                )

                return

            embed = discord.Embed(
                title=f"{league_emoji(lid)} {name}",
                description="Chọn trận để soi.",
                color=discord.Color.blurple()
            )

            await interaction.followup.send(
                embed=embed,
                view=MatchView(
                    fixtures,
                    "soi",
                    name
                ),
                ephemeral=True
            )

        except Exception as exc:

            await interaction.followup.send(
                f"❌ API lỗi:\n`{str(exc)[:800]}`",
                ephemeral=True
            )


class SoiView(discord.ui.View):

    def __init__(self):

        super().__init__(timeout=300)

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
                f for f in fixtures
                if is_upcoming(f)
            ]

            embed = discord.Embed(
                title="🔵 MAN CITY — TẤT CẢ TRẬN",
                description=(
                    "Các trận Man City sắp tới "
                    "không phân biệt giải đấu."
                ),
                color=discord.Color.blue()
            )

            if not fixtures:

                embed.description = (
                    "❌ API không trả về trận sắp tới."
                )

            await interaction.followup.send(
                embed=embed,
                view=(
                    MatchView(
                        fixtures,
                        "soi",
                        "Man City"
                    )
                    if fixtures
                    else None
                ),
                ephemeral=True
            )

        except Exception as exc:

            await interaction.followup.send(
                f"❌ API lỗi:\n`{str(exc)[:800]}`",
                ephemeral=True
            )


# ============================================================
# /CUOC
# ============================================================

@bot.tree.command(
    name="cuoc",
    description="Mở bảng cược bóng đá"
)
async def cuoc(interaction):

    embed = discord.Embed(
        title="⚽ KHU VỰC CƯỢC BÓNG ĐÁ",
        description=(
            "🏆 Chọn giải đấu bên dưới.\n\n"
            "🔵 Hoặc chọn riêng các trận Man City.\n\n"
            f"💰 Số dư hiện tại: "
            f"**{fmt_money(get_balance(interaction.user.id))} xu**"
        ),
        color=discord.Color.gold()
    )

    await interaction.response.send_message(
        embed=embed,
        view=CuocView(),
        ephemeral=True
    )


# ============================================================
# /SOI
# ============================================================

@bot.tree.command(
    name="soi",
    description="Soi và dự đoán các trận bóng đá"
)
async def soi(interaction):

    embed = discord.Embed(
        title="🔮 SOI BÓNG ĐÁ",
        description=(
            "Chọn một trong 11 giải đấu hoặc "
            "xem riêng toàn bộ trận Man City.\n\n"
            "Bot sẽ lấy dữ liệu dự đoán từ API-Football."
        ),
        color=discord.Color.blurple()
    )

    await interaction.response.send_message(
        embed=embed,
        view=SoiView(),
        ephemeral=True
    )


# ============================================================
# /VI
# ============================================================

@bot.tree.command(
    name="vi",
    description="Xem ví và cấp VIP"
)
async def vi(interaction):

    user_id = interaction.user.id

    balance = get_balance(user_id)

    with db_connect() as conn:

        row = conn.execute(
            "SELECT vip FROM wallets WHERE user_id=?",
            (user_id,)
        ).fetchone()

    vip = int(row["vip"]) if row else get_vip(balance)

    # Đồng bộ nếu DB cũ
    real_vip = get_vip(balance)

    if real_vip != vip:

        with db_connect() as conn:

            conn.execute(
                "UPDATE wallets SET vip=? WHERE user_id=?",
                (real_vip, user_id)
            )

            conn.commit()

        vip = real_vip

    next_level, next_amount = get_next_vip(vip)

    if next_level:

        progress = (
            f"🎯 Còn **{fmt_money(max(0, next_amount - balance))} xu** "
            f"để lên **VIP {next_level}**."
        )

    else:

        progress = "👑 Đã đạt VIP 10 tối đa."

    embed = discord.Embed(
        title="💰 VÍ CỦA BẠN",
        color=discord.Color.gold()
    )

    embed.add_field(
        name="💵 Số dư",
        value=f"**{fmt_money(balance)} xu**",
        inline=False
    )

    embed.add_field(
        name="⭐ VIP",
        value=f"**{get_vip_name(vip)}**",
        inline=False
    )

    embed.add_field(
        name="📈 Tiến độ",
        value=progress,
        inline=False
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# ============================================================
# /KHOINGHIEP
# ============================================================

@bot.tree.command(
    name="khoinghiep",
    description="Nhận 1.000 xu khởi nghiệp một lần"
)
async def khoinghiep(interaction):

    user_id = interaction.user.id

    ensure_wallet(user_id)

    with db_connect() as conn:

        row = conn.execute(
            "SELECT started FROM wallets WHERE user_id=?",
            (user_id,)
        ).fetchone()

        if row and int(row["started"]) == 1:

            await interaction.response.send_message(
                "❌ M đã nhận 1.000 xu khởi nghiệp rồi.",
                ephemeral=True
            )

            return

        conn.execute(
            """
            UPDATE wallets
            SET started=1,
                balance=balance+?
            WHERE user_id=?
            """,
            (
                STARTER_BONUS,
                user_id
            )
        )

        conn.commit()

    await update_vip(
        user_id,
        interaction.channel
    )

    await interaction.response.send_message(
        (
            "🎁 **NHẬN THÀNH CÔNG**\n"
            f"💰 +{fmt_money(STARTER_BONUS)} xu\n"
            f"💳 Số dư: **{fmt_money(get_balance(user_id))} xu**"
        ),
        ephemeral=True
    )


# ============================================================
# /COMAT
# ============================================================

@bot.tree.command(
    name="comat",
    description="Nhận 50.000 xu mỗi ngày"
)
async def comat(interaction):

    user_id = interaction.user.id

    ensure_wallet(user_id)

    today = datetime.now(TZ).strftime(
        "%Y-%m-%d"
    )

    with db_connect() as conn:

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
                "❌ Hôm nay m nhận Cơm Ăn rồi. Mai quay lại nhé.",
                ephemeral=True
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

    await update_vip(
        user_id,
        interaction.channel
    )

    await interaction.response.send_message(
        (
            "🍚 **CƠM ĂN HÔM NAY**\n\n"
            f"💰 +{fmt_money(DAILY_BONUS)} xu\n"
            f"💳 Số dư: **{fmt_money(get_balance(user_id))} xu**"
        ),
        ephemeral=True
    )


# ============================================================
# /PING
# ============================================================

@bot.tree.command(
    name="ping",
    description="Kiểm tra bot"
)
async def ping(interaction):

    latency = round(
        bot.latency * 1000
    )

    await interaction.response.send_message(
        f"🏓 Pong!\n⚡ `{latency} ms`",
        ephemeral=True
    )


# ============================================================
# /APIQUOTA
# ============================================================

@bot.tree.command(
    name="apiquota",
    description="Kiểm tra API-Football"
)
async def apiquota(interaction):

    remaining = football.remaining

    if remaining is None:
        text = "Chưa có thông tin quota."
    else:
        text = f"Còn khoảng **{remaining} requests**."

    await interaction.response.send_message(
        f"⚽ API-Football\n{text}",
        ephemeral=True
    )


# ============================================================
# /CADO
# ============================================================

@bot.tree.command(
    name="cado",
    description="Xem số dư của bạn"
)
async def cado(interaction):

    balance = get_balance(
        interaction.user.id
    )

    await interaction.response.send_message(
        (
            f"💰 **{interaction.user.display_name}**\n"
            f"Số dư: **{fmt_money(balance)} xu**"
        ),
        ephemeral=True
    )


# ============================================================
# ADMIN
# ============================================================

def is_admin(interaction):

    if interaction.user.guild_permissions.manage_guild:
        return True

    return False


@bot.tree.command(
    name="admin",
    description="Quản lý tiền ảo"
)
@app_commands.describe(
    action="add hoặc remove",
    user="Người chơi",
    amount="Số xu",
    password="Mật khẩu nếu không có quyền Manage Server"
)
@app_commands.choices(
    action=[
        app_commands.Choice(
            name="Cộng xu",
            value="add"
        ),
        app_commands.Choice(
            name="Trừ xu",
            value="remove"
        )
    ]
)
async def admin(
    interaction,
    action: app_commands.Choice[str],
    user: discord.Member,
    amount: int,
    password: Optional[str] = None
):

    allowed = is_admin(interaction)

    if not allowed:

        if password != ADMIN_PASSWORD:

            await interaction.response.send_message(
                "❌ Không có quyền admin.",
                ephemeral=True
            )

            return

    if amount <= 0:

        await interaction.response.send_message(
            "❌ Amount phải lớn hơn 0.",
            ephemeral=True
        )

        return

    if amount > 100_000_000:

        await interaction.response.send_message(
            "❌ Tối đa 100.000.000 xu/lần.",
            ephemeral=True
        )

        return

    if action.value == "add":

        await add_balance(
            user.id,
            amount,
            interaction.channel
        )

        action_text = "Cộng"

    else:

        await remove_balance(
            user.id,
            amount
        )

        action_text = "Trừ"

    await interaction.response.send_message(
        (
            f"✅ **{action_text} xu thành công**\n\n"
            f"👤 {user.mention}\n"
            f"💰 {fmt_money(amount)} xu\n"
            f"💳 Số dư mới: "
            f"**{fmt_money(get_balance(user.id))} xu**"
        ),
        ephemeral=True
    )


# ============================================================
# KẾT TOÁN
# ============================================================

async def settle_one_bet(bet):

    fid = int(bet["fixture_id"])

    fixtures = await football.fixture(fid)

    if not fixtures:
        return "pending"

    fixture = fixtures[0]

    result = None

    status = fixture_status(fixture)

    if status in {
        "NS",
        "TBD",
        "PST"
    }:
        return "pending"

    teams = fixture.get(
        "teams",
        {}
    )

    home = teams.get("home") or {}
    away = teams.get("away") or {}

    if home.get("winner") is True:
        result = "home"

    elif away.get("winner") is True:
        result = "away"

    else:

        goals = fixture.get(
            "goals"
        ) or {}

        hg = goals.get("home")
        ag = goals.get("away")

        if isinstance(hg, int) and isinstance(ag, int):

            if hg > ag:
                result = "home"

            elif ag > hg:
                result = "away"

            else:
                result = "draw"

    if result is None:
        return "pending"

    user_id = int(bet["user_id"])
    stake = int(bet["stake"])
    choice = bet["choice"]

    if choice == result:

        payout = stake * 2

        with db_connect() as conn:

            conn.execute(
                """
                UPDATE wallets
                SET balance=balance+?
                WHERE user_id=?
                """,
                (
                    payout,
                    user_id
                )
            )

            conn.execute(
                """
                UPDATE bets
                SET status='won'
                WHERE id=?
                """,
                (bet["id"],)
            )

            conn.commit()

        return "won"

    else:

        with db_connect() as conn:

            conn.execute(
                """
                UPDATE bets
                SET status='lost'
                WHERE id=?
                """,
                (bet["id"],)
            )

            conn.commit()

        return "lost"


@bot.tree.command(
    name="kettoan",
    description="Kết toán tất cả cược đã có kết quả"
)
async def kettoan(interaction):

    await interaction.response.defer(
        ephemeral=True
    )

    with db_connect() as conn:

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
            "📭 Không có cược đang chờ.",
            ephemeral=True
        )

        return

    won = 0
    lost = 0
    pending = 0

    for bet in bets:

        try:

            result = await settle_one_bet(
                bet
            )

            if result == "won":
                won += 1

            elif result == "lost":
                lost += 1

            else:
                pending += 1

        except Exception:
            pending += 1

    await interaction.followup.send(
        (
            "🏁 **KẾT TOÁN XONG**\n\n"
            f"✅ Thắng: **{won}**\n"
            f"❌ Thua: **{lost}**\n"
            f"⏳ Chưa có kết quả: **{pending}**"
        ),
        ephemeral=True
    )


# ============================================================
# HEALTH SERVER FOR RAILWAY
# ============================================================

async def health(request):

    return web.json_response(
        {
            "status": "ok",
            "bot": str(bot.user) if bot.user else None,
            "guild": GUILD_ID
        }
    )


async def start_health_server():

    app = web.Application()

    app.router.add_get(
        "/",
        health
    )

    app.router.add_get(
        "/health",
        health
    )

    runner = web.AppRunner(app)

    await runner.setup()

    port = int(
        os.getenv(
            "PORT",
            "8080"
        )
    )

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        port
    )

    await site.start()

    print(
        f"Health server running on port {port}"
    )

    return runner


# ============================================================
# BOT READY
# ============================================================

health_runner = None


@bot.event
async def on_ready():

    global health_runner

    print(
        f"Logged in as {bot.user} "
        f"(ID: {bot.user.id})"
    )

    init_db()

    await football.start()

    # Sync slash commands vào server cụ thể
    try:

        synced = await bot.tree.sync(
            guild=GUILD
        )

        print(
            f"Synced {len(synced)} guild commands."
        )

    except Exception as exc:

        print(
            f"Guild sync error: {exc}"
        )

    # Global sync dự phòng
    try:

        await bot.tree.sync()

        print(
            "Global commands synced."
        )

    except Exception as exc:

        print(
            f"Global sync error: {exc}"
        )

    if health_runner is None:

        try:

            health_runner = await start_health_server()

        except Exception as exc:

            print(
                f"Health server error: {exc}"
            )


# ============================================================
# SHUTDOWN
# ============================================================

async def close_resources():

    try:
        await football.close()
    except Exception:
        pass


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    init_db()

    try:

        bot.run(
            DISCORD_TOKEN
        )

    finally:

        try:
            asyncio.run(
                close_resources()
            )
        except Exception:
            pass

import os
import json
import time
import sqlite3
import asyncio
from datetime import datetime
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

GUILD_ID = int(os.getenv("GUILD_ID", "0"))
STARTUP_CHANNEL_ID = int(os.getenv("STARTUP_CHANNEL_ID", "0"))

API_BASE = "https://v3.football.api-sports.io"
TIMEZONE = "Asia/Ho_Chi_Minh"
TZ = ZoneInfo(TIMEZONE)

DB_PATH = "/data/football_bot.db" if os.path.isdir("/data") else "football_bot.db"

STARTING_BALANCE = 100000
STARTER_BONUS = 1000
DAILY_BONUS = 50000
MANCHESTER_CITY_TEAM_ID = 50

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

if not DISCORD_TOKEN:
    raise RuntimeError("THIẾU DISCORD_TOKEN")

if not FOOTBALL_API_KEY:
    raise RuntimeError("THIẾU FOOTBALL_API_KEY")

if not GUILD_ID:
    raise RuntimeError("THIẾU GUILD_ID")


# ============================================================
# BOT
# ============================================================

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
GUILD = discord.Object(id=GUILD_ID)


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
                created_at TEXT NOT NULL
            )
        """)

        conn.commit()


def ensure_wallet(user_id):
    with db() as conn:
        row = conn.execute(
            "SELECT balance FROM wallets WHERE user_id=?",
            (user_id,)
        ).fetchone()

        if row is None:
            conn.execute(
                "INSERT INTO wallets(user_id,balance,started,vip) VALUES(?,?,0,0)",
                (user_id, STARTING_BALANCE)
            )
            conn.commit()
            return STARTING_BALANCE

        return int(row["balance"])


def get_balance(user_id):
    return ensure_wallet(user_id)


def get_vip(balance):
    for level, minimum in VIP_LEVELS:
        if balance >= minimum:
            return level
    return 0


def vip_name(level):
    return f"VIP {level}" if level else "Thành viên"


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


def money(n):
    return f"{int(n):,}"


async def check_vip(user_id, channel=None):
    ensure_wallet(user_id)

    with db() as conn:
        row = conn.execute(
            "SELECT balance,vip FROM wallets WHERE user_id=?",
            (user_id,)
        ).fetchone()

        balance = int(row["balance"])
        old = int(row["vip"] or 0)
        new = get_vip(balance)

        if new != old:
            conn.execute(
                "UPDATE wallets SET vip=? WHERE user_id=?",
                (new, user_id)
            )
            conn.commit()

    if new > old and channel:
        user = bot.get_user(user_id)
        name = user.display_name if user else str(user_id)

        embed = discord.Embed(
            title=f"🎉 CHÚC MỪNG {vip_name(new).upper()} 🎉",
            description=(
                "👑━━━━━━━━━━━━━━━━━━👑\n"
                f"👤 **{name}**\n"
                f"💰 Số dư: **{money(balance)} xu**\n"
                f"{vip_icon(new)} Đã đạt **{vip_name(new)}**\n"
                "👑━━━━━━━━━━━━━━━━━━👑"
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
            "UPDATE wallets SET balance=balance+? WHERE user_id=?",
            (int(amount), user_id)
        )
        conn.commit()

    await check_vip(user_id, channel)


async def remove_money(user_id, amount):
    ensure_wallet(user_id)

    with db() as conn:
        conn.execute(
            "UPDATE wallets SET balance=MAX(balance-?,0) WHERE user_id=?",
            (int(amount), user_id)
        )
        conn.commit()

    await check_vip(user_id)


# ============================================================
# FOOTBALL API
# ============================================================

class FootballAPI:

    def __init__(self):
        self.session = None
        self.cache = {}
        self.last_quota = None

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

    async def request(self, endpoint, params):
        await self.start()

        cache_key = (
            endpoint,
            tuple(sorted((str(k), str(v)) for k, v in params.items()))
        )

        now = time.monotonic()

        if cache_key in self.cache:
            created, data = self.cache[cache_key]

            if now - created < 30:
                return data

        url = f"{API_BASE}/{endpoint}"

        last_error = None

        for attempt in range(3):
            try:
                async with self.session.get(url, params=params) as response:
                    raw = await response.text()

                    try:
                        data = json.loads(raw)
                    except Exception:
                        data = {"errors": {"response": raw[:500]}}

                    quota = response.headers.get(
                        "x-ratelimit-requests-remaining"
                    )

                    if quota:
                        self.last_quota = quota

                    if response.status >= 500:
                        if attempt < 2:
                            await asyncio.sleep(1)
                            continue

                    if response.status >= 400:
                        raise RuntimeError(
                            f"API HTTP {response.status}: "
                            f"{data.get('errors', raw[:300])}"
                        )

                    errors = data.get("errors")

                    if isinstance(errors, dict) and any(errors.values()):
                        raise RuntimeError(str(errors))

                    if isinstance(errors, list) and errors:
                        raise RuntimeError(str(errors))

                    self.cache[cache_key] = (now, data)
                    return data

            except Exception as e:
                last_error = e
                await asyncio.sleep(0.5)

        raise RuntimeError(str(last_error or "API lỗi"))

    async def fixtures_league(self, league):
        return await self.request(
            "fixtures",
            {
                "league": league,
                "next": 20,
                "timezone": TIMEZONE
            }
        )

    async def fixtures_city(self):
        return await self.request(
            "fixtures",
            {
                "team": MANCHESTER_CITY_TEAM_ID,
                "next": 20,
                "timezone": TIMEZONE
            }
        )

    async def fixture(self, fixture_id):
        return await self.request(
            "fixtures",
            {
                "id": fixture_id,
                "timezone": TIMEZONE
            }
        )

    async def prediction(self, fixture_id):
        return await self.request(
            "predictions",
            {
                "fixture": fixture_id
            }
        )


football = FootballAPI()


# ============================================================
# FOOTBALL HELPERS
# ============================================================

UPCOMING = {"NS", "TBD"}

FINISHED = {"FT", "AET", "PEN"}


def fixture_status(fixture):
    return (
        fixture.get("fixture", {})
        .get("status", {})
        .get("short", "")
    )


def fixture_id(fixture):
    return int(fixture.get("fixture", {}).get("id", 0))


def team_name(fixture, side):
    return (
        fixture.get("teams", {})
        .get(side, {})
        .get("name", "?")
    )


def fixture_time(fixture):
    value = fixture.get("fixture", {}).get("date")

    if not value:
        return "Không rõ giờ"

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(TZ).strftime("%d/%m/%Y %H:%M")
    except Exception:
        return value


def league_name(fixture):
    league = fixture.get("league", {})
    return league.get("name", "Không rõ giải")


def result_from_fixture(fixture):
    status = fixture_status(fixture)

    if status not in FINISHED:
        return None

    home = fixture.get("teams", {}).get("home", {})
    away = fixture.get("teams", {}).get("away", {})

    if home.get("winner") is True:
        return "home"

    if away.get("winner") is True:
        return "away"

    goals = fixture.get("goals", {})

    h = goals.get("home")
    a = goals.get("away")

    if h is None or a is None:
        return None

    if h > a:
        return "home"

    if a > h:
        return "away"

    return "draw"


def prediction_data(data):
    response = data.get("response", [])

    if not response:
        return {}

    item = response[0]

    return item.get("predictions") or {}


def prediction_score(pred):
    score = pred.get("score") or {}

    fulltime = score.get("fulltime") or {}

    h = fulltime.get("home")
    a = fulltime.get("away")

    if h is not None and a is not None:
        return h, a

    h = score.get("home")
    a = score.get("away")

    if h is not None and a is not None:
        return h, a

    goals = pred.get("goals") or {}

    h = goals.get("home")
    a = goals.get("away")

    if h is not None and a is not None:
        return h, a

    return None


def api_error(error):
    return discord.Embed(
        title="❌ API lỗi",
        description=f"```{str(error)[:1800]}```",
        color=discord.Color.red()
    )


# ============================================================
# MATCH LIST
# ============================================================

def upcoming_fixtures(data):
    result = []

    for item in data.get("response", []):
        if fixture_status(item) in UPCOMING:
            result.append(item)

    return result


def match_label(fixture):
    return (
        f"{team_name(fixture,'home')} vs "
        f"{team_name(fixture,'away')} "
        f"• {fixture_time(fixture)}"
    )[:100]


class MatchSelect(discord.ui.Select):

    def __init__(self, fixtures, mode="bet"):
        self.fixtures = fixtures
        self.mode = mode

        options = []

        for f in fixtures[:25]:
            options.append(
                discord.SelectOption(
                    label=match_label(f),
                    value=str(fixture_id(f)),
                    description=league_name(f)[:100]
                )
            )

        super().__init__(
            placeholder="⚽ Chọn trận...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction):
        fid = int(self.values[0])

        fixture = next(
            (f for f in self.fixtures if fixture_id(f) == fid),
            None
        )

        if not fixture:
            await interaction.response.send_message(
                "❌ Không tìm thấy trận.",
                ephemeral=True
            )
            return

        if self.mode == "bet":
            await interaction.response.send_modal(
                StakeModal(fixture)
            )
        else:
            await show_prediction(interaction, fixture)


class MatchView(discord.ui.View):

    def __init__(self, fixtures, mode="bet"):
        super().__init__(timeout=180)

        if fixtures:
            self.add_item(MatchSelect(fixtures, mode))


# ============================================================
# CUOC
# ============================================================

class CuocLeagueSelect(discord.ui.Select):

    def __init__(self):
        options = []

        for league_id, (icon, name) in LEAGUES.items():
            options.append(
                discord.SelectOption(
                    label=name,
                    value=league_id,
                    emoji=icon
                )
            )

        super().__init__(
            placeholder="🏆 Chọn giải để cược...",
            options=options,
            min_values=1,
            max_values=1
        )

    async def callback(self, interaction):
        league_id = self.values[0]

        await interaction.response.defer(ephemeral=True)

        try:
            data = await football.fixtures_league(league_id)
            fixtures = upcoming_fixtures(data)

            if not fixtures:
                await interaction.followup.send(
                    "❌ Hiện không có trận sắp tới.",
                    ephemeral=True
                )
                return

            name = LEAGUES[league_id][1]

            embed = discord.Embed(
                title=f"🎰 CƯỢC — {name}",
                description=(
                    f"Có **{len(fixtures)}** trận sắp tới.\n"
                    "Chọn trận bên dưới."
                ),
                color=discord.Color.gold()
            )

            await interaction.followup.send(
                embed=embed,
                view=MatchView(fixtures, "bet"),
                ephemeral=True
            )

        except Exception as e:
            await interaction.followup.send(
                embed=api_error(e),
                ephemeral=True
            )


class CuocHomeView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(CuocLeagueSelect())

    @discord.ui.button(
        label="Các trận Man City sắp tới",
        emoji="🔵",
        style=discord.ButtonStyle.primary,
        row=1
    )
    async def man_city(self, interaction, button):
        await interaction.response.defer(ephemeral=True)

        try:
            data = await football.fixtures_city()
            fixtures = upcoming_fixtures(data)

            if not fixtures:
                await interaction.followup.send(
                    "❌ Không có trận Man City sắp tới.",
                    ephemeral=True
                )
                return

            embed = discord.Embed(
                title="🔵 MAN CITY — CÁC TRẬN SẮP TỚI",
                description="Chọn trận để nhập tiền cược.",
                color=discord.Color.blue()
            )

            await interaction.followup.send(
                embed=embed,
                view=MatchView(fixtures, "bet"),
                ephemeral=True
            )

        except Exception as e:
            await interaction.followup.send(
                embed=api_error(e),
                ephemeral=True
            )


# ============================================================
# SOI
# ============================================================

class SoiLeagueSelect(discord.ui.Select):

    def __init__(self):
        options = []

        for league_id, (icon, name) in LEAGUES.items():
            options.append(
                discord.SelectOption(
                    label=name,
                    value=league_id,
                    emoji=icon
                )
            )

        super().__init__(
            placeholder="🏆 Chọn giải để soi...",
            options=options,
            min_values=1,
            max_values=1
        )

    async def callback(self, interaction):
        league_id = self.values[0]

        await interaction.response.defer(ephemeral=True)

        try:
            data = await football.fixtures_league(league_id)
            fixtures = upcoming_fixtures(data)

            if not fixtures:
                await interaction.followup.send(
                    "❌ Hiện không có trận sắp tới.",
                    ephemeral=True
                )
                return

            await interaction.followup.send(
                embed=discord.Embed(
                    title=f"🔎 SOI — {LEAGUES[league_id][1]}",
                    description="Chọn trận để xem dự đoán.",
                    color=discord.Color.green()
                ),
                view=MatchView(fixtures, "soi"),
                ephemeral=True
            )

        except Exception as e:
            await interaction.followup.send(
                embed=api_error(e),
                ephemeral=True
            )


class SoiHomeView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(SoiLeagueSelect())

    @discord.ui.button(
        label="Man City — tất cả trận",
        emoji="🔵",
        style=discord.ButtonStyle.primary,
        row=1
    )
    async def man_city(self, interaction, button):
        await interaction.response.defer(ephemeral=True)

        try:
            data = await football.fixtures_city()
            fixtures = upcoming_fixtures(data)

            if not fixtures:
                await interaction.followup.send(
                    "❌ Không có trận Man City sắp tới.",
                    ephemeral=True
                )
                return

            await interaction.followup.send(
                embed=discord.Embed(
                    title="🔵 MAN CITY — TẤT CẢ TRẬN SẮP TỚI",
                    description="Không phân biệt giải. Chọn trận để soi.",
                    color=discord.Color.blue()
                ),
                view=MatchView(fixtures, "soi"),
                ephemeral=True
            )

        except Exception as e:
            await interaction.followup.send(
                embed=api_error(e),
                ephemeral=True
            )


# ============================================================
# PREDICTION
# ============================================================

async def show_prediction(interaction, fixture):
    await interaction.response.defer(ephemeral=True)

    try:
        data = await football.prediction(fixture_id(fixture))
        pred = prediction_data(data)

        winner = pred.get("winner") or {}

        winner_name = winner.get("name") or "Chưa xác định"
        winner_comment = winner.get("comment") or ""

        score = prediction_score(pred)

        score_text = (
            f"{score[0]} - {score[1]}"
            if score
            else "API chưa cung cấp tỷ số dự đoán"
        )

        percent = pred.get("percent") or {}

        home_percent = percent.get("home", "?")
        draw_percent = percent.get("draw", "?")
        away_percent = percent.get("away", "?")

        embed = discord.Embed(
            title="🔎 SOI TRẬN",
            description=(
                f"⚽ **{team_name(fixture,'home')}**\n"
                f"🆚 **{team_name(fixture,'away')}**\n\n"
                f"🏆 Giải: **{league_name(fixture)}**\n"
                f"🕐 **{fixture_time(fixture)}**"
            ),
            color=discord.Color.green()
        )

        embed.add_field(
            name="🎯 Dự đoán",
            value=f"**{winner_name}**\n{winner_comment}",
            inline=False
        )

        embed.add_field(
            name="🥅 Tỷ số dự đoán",
            value=f"**{score_text}**",
            inline=True
        )

        embed.add_field(
            name="📊 Xác suất",
            value=(
                f"🏠 Home: **{home_percent}%**\n"
                f"🤝 Draw: **{draw_percent}%**\n"
                f"🚩 Away: **{away_percent}%**"
            ),
            inline=True
        )

        advice = pred.get("advice")

        if advice:
            embed.add_field(
                name="💡 Nhận định API",
                value=str(advice)[:1000],
                inline=False
            )

        embed.set_footer(
            text="Dữ liệu API-Football • Chỉ mang tính tham khảo"
        )

        await interaction.followup.send(
            embed=embed,
            ephemeral=True
        )

    except Exception as e:
        await interaction.followup.send(
            embed=api_error(e),
            ephemeral=True
        )


# ============================================================
# BETTING
# ============================================================

class StakeModal(discord.ui.Modal, title="💰 Nhập tiền cược"):

    stake = discord.ui.TextInput(
        label="Số tiền cược",
        placeholder="Ví dụ: 10000",
        required=True,
        min_length=1,
        max_length=12
    )

    def __init__(self, fixture):
        super().__init__()
        self.fixture = fixture

    async def on_submit(self, interaction):
        try:
            amount = int(str(self.stake.value).replace(",", "").replace(".", ""))
        except Exception:
            await interaction.response.send_message(
                "❌ Nhập số tiền bằng số.",
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

        balance = get_balance(interaction.user.id)

        if amount > balance:
            await interaction.response.send_message(
                f"❌ Không đủ xu.\nVí hiện tại: **{money(balance)}**",
                ephemeral=True
            )
            return

        if fixture_status(self.fixture) not in UPCOMING:
            await interaction.response.send_message(
                "❌ Trận này đã bắt đầu.",
                ephemeral=True
            )
            return

        with db() as conn:
            exists = conn.execute(
                """
                SELECT id FROM bets
                WHERE user_id=? AND fixture_id=? AND status='open'
                """,
                (interaction.user.id, fixture_id(self.fixture))
            ).fetchone()

        if exists:
            await interaction.response.send_message(
                "❌ M đã có cược đang mở cho trận này.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            embed=discord.Embed(
                title="🎯 CHỌN CỬA",
                description=(
                    f"⚽ **{team_name(self.fixture,'home')}**\n"
                    f"🆚 **{team_name(self.fixture,'away')}**\n\n"
                    f"💰 Cược: **{money(amount)} xu**"
                ),
                color=discord.Color.gold()
            ),
            view=BetChoiceView(self.fixture, amount),
            ephemeral=True
        )


class BetChoiceView(discord.ui.View):

    def __init__(self, fixture, stake):
        super().__init__(timeout=120)
        self.fixture = fixture
        self.stake = stake

    async def place(self, interaction, choice):
        user_id = interaction.user.id

        balance = get_balance(user_id)

        if self.stake > balance:
            await interaction.response.send_message(
                "❌ Không đủ xu.",
                ephemeral=True
            )
            return

        with db() as conn:
            exists = conn.execute(
                """
                SELECT id FROM bets
                WHERE user_id=? AND fixture_id=? AND status='open'
                """,
                (user_id, fixture_id(self.fixture))
            ).fetchone()

            if exists:
                await interaction.response.send_message(
                    "❌ M đã cược trận này rồi.",
                    ephemeral=True
                )
                return

            conn.execute(
                """
                INSERT INTO bets
                (user_id,fixture_id,choice,stake,status,created_at)
                VALUES (?,?,?,?,?,?)
                """,
                (
                    user_id,
                    fixture_id(self.fixture),
                    choice,
                    self.stake,
                    "open",
                    datetime.now(TZ).isoformat()
                )
            )

            conn.execute(
                "UPDATE wallets SET balance=balance-? WHERE user_id=?",
                (self.stake, user_id)
            )

            conn.commit()

        names = {
            "home": team_name(self.fixture, "home"),
            "draw": "Hòa",
            "away": team_name(self.fixture, "away")
        }

        await interaction.response.edit_message(
            content=(
                "✅ **ĐẶT CƯỢC THÀNH CÔNG**\n\n"
                f"⚽ {team_name(self.fixture,'home')} vs "
                f"{team_name(self.fixture,'away')}\n"
                f"🎯 Cửa: **{names[choice]}**\n"
                f"💰 Tiền cược: **{money(self.stake)} xu**\n"
                f"👛 Ví còn: **{money(get_balance(user_id))} xu**"
            ),
            embed=None,
            view=None
        )

    @discord.ui.button(
        label="Chủ nhà",
        emoji="🏠",
        style=discord.ButtonStyle.success
    )
    async def home(self, interaction, button):
        await self.place(interaction, "home")

    @discord.ui.button(
        label="Hòa",
        emoji="🤝",
        style=discord.ButtonStyle.secondary
    )
    async def draw(self, interaction, button):
        await self.place(interaction, "draw")

    @discord.ui.button(
        label="Đội khách",
        emoji="🚩",
        style=discord.ButtonStyle.danger
    )
    async def away(self, interaction, button):
        await self.place(interaction, "away")


# ============================================================
# COMMAND /CUOC
# ============================================================

@bot.tree.command(
    name="cuoc",
    description="Cược bóng đá",
    guild=GUILD
)
async def cuoc(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎰 CƯỢC BÓNG ĐÁ",
        description=(
            "🏆 Chọn một trong **11 giải** bên dưới\n"
            "hoặc bấm riêng **🔵 Các trận Man City sắp tới**."
        ),
        color=discord.Color.gold()
    )

    embed.add_field(
        name="🏆 Các giải",
        value=(
            "Premier League\n"
            "Champions League\n"
            "La Liga\n"
            "Serie A\n"
            "Bundesliga\n"
            "Ligue 1\n"
            "Eredivisie\n"
            "Primeira Liga\n"
            "Championship\n"
            "FA Cup\n"
            "Carabao Cup"
        ),
        inline=True
    )

    embed.add_field(
        name="🔵 Man City",
        value="Tất cả trận sắp tới của Man City, không phân biệt giải.",
        inline=True
    )

    await interaction.response.send_message(
        embed=embed,
        view=CuocHomeView(),
        ephemeral=True
    )


# ============================================================
# COMMAND /SOI
# ============================================================

@bot.tree.command(
    name="soi",
    description="Soi bóng đá",
    guild=GUILD
)
async def soi(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🔎 SOI BÓNG ĐÁ",
        description=(
            "🏆 Chọn giải để xem các trận sắp tới.\n"
            "🔵 Hoặc xem **tất cả trận Man City**."
        ),
        color=discord.Color.green()
    )

    embed.add_field(
        name="🏆 11 GIẢI",
        value=(
            "Premier League\n"
            "Champions League\n"
            "La Liga\n"
            "Serie A\n"
            "Bundesliga\n"
            "Ligue 1\n"
            "Eredivisie\n"
            "Primeira Liga\n"
            "Championship\n"
            "FA Cup\n"
            "Carabao Cup"
        ),
        inline=True
    )

    embed.add_field(
        name="🔵 MAN CITY",
        value="Tất cả trận sắp tới, không phân biệt giải.",
        inline=True
    )

    await interaction.response.send_message(
        embed=embed,
        view=SoiHomeView(),
        ephemeral=True
    )


# ============================================================
# /VI
# ============================================================

@bot.tree.command(
    name="vi",
    description="Xem ví và VIP",
    guild=GUILD
)
async def vi(interaction: discord.Interaction):
    user_id = interaction.user.id
    balance = get_balance(user_id)

    await check_vip(user_id)

    level = get_vip(balance)

    next_vip = None

    for lv, minimum in reversed(VIP_LEVELS):
        if minimum > balance:
            next_vip = (lv, minimum)

    if next_vip:
        next_text = (
            f"{vip_icon(next_vip[0])} {vip_name(next_vip[0])}"
            f" — **{money(next_vip[1])} xu**"
        )
    else:
        next_text = "👑 Đã đạt VIP 10"

    with db() as conn:
        bets = conn.execute(
            "SELECT COUNT(*) AS c FROM bets WHERE user_id=?",
            (user_id,)
        ).fetchone()["c"]

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
        value=f"{vip_icon(level)} **{vip_name(level)}**",
        inline=True
    )

    embed.add_field(
        name="🎯 Tổng cược",
        value=f"**{bets}**",
        inline=True
    )

    embed.add_field(
        name="📈 Mốc VIP tiếp theo",
        value=next_text,
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
    description="Nhận 1.000 xu khởi nghiệp",
    guild=GUILD
)
async def khoinghiep(interaction: discord.Interaction):
    user_id = interaction.user.id
    ensure_wallet(user_id)

    with db() as conn:
        row = conn.execute(
            "SELECT started FROM wallets WHERE user_id=?",
            (user_id,)
        ).fetchone()

        if row["started"]:
            await interaction.response.send_message(
                "❌ M đã nhận **1.000 xu khởi nghiệp** rồi.",
                ephemeral=True
            )
            return

        conn.execute(
            "UPDATE wallets SET started=1,balance=balance+? WHERE user_id=?",
            (STARTER_BONUS, user_id)
        )
        conn.commit()

    await check_vip(user_id, interaction.channel)

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
async def comat(interaction: discord.Interaction):
    user_id = interaction.user.id
    ensure_wallet(user_id)

    today = datetime.now(TZ).date().isoformat()

    with db() as conn:
        exists = conn.execute(
            """
            SELECT 1 FROM daily_claims
            WHERE user_id=? AND claim_date=?
            """,
            (user_id, today)
        ).fetchone()

        if exists:
            await interaction.response.send_message(
                "❌ Hôm nay m đã nhận **50.000 xu** rồi.",
                ephemeral=True
            )
            return

        conn.execute(
            """
            INSERT INTO daily_claims(user_id,claim_date)
            VALUES (?,?)
            """,
            (user_id, today)
        )

        conn.execute(
            "UPDATE wallets SET balance=balance+? WHERE user_id=?",
            (DAILY_BONUS, user_id)
        )

        conn.commit()

    await check_vip(user_id, interaction.channel)

    await interaction.response.send_message(
        f"💰 Nhận **{money(DAILY_BONUS)} xu** hôm nay!\n"
        f"👛 Ví: **{money(get_balance(user_id))} xu**"
    )


# ============================================================
# /ADMIN
# ============================================================

@bot.tree.command(
    name="admin",
    description="Admin cộng/trừ xu",
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
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "❌ M không có quyền Administrator.",
            ephemeral=True
        )
        return

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
        await add_money(user.id, amount, interaction.channel)
    else:
        await remove_money(user.id, abs(amount))

    balance = get_balance(user.id)
    level = get_vip(balance)

    await interaction.response.send_message(
        f"👑 Đã cập nhật **{user.display_name}**\n"
        f"💰 Thay đổi: **{amount:+,} xu**\n"
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
async def kettoan(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    with db() as conn:
        bets = conn.execute(
            """
            SELECT * FROM bets
            WHERE status='open'
            ORDER BY id ASC
            LIMIT 100
            """
        ).fetchall()

    if not bets:
        await interaction.followup.send(
            "ℹ️ Không có cược nào đang chờ kết toán.",
            ephemeral=True
        )
        return

    settled = 0
    skipped = 0

    for bet in bets:
        try:
            data = await football.fixture(int(bet["fixture_id"]))
            fixtures = data.get("response", [])

            if not fixtures:
                skipped += 1
                continue

            fixture = fixtures[0]
            result = result_from_fixture(fixture)

            # Chỉ FT/AET/PEN mới được kết toán.
            if result is None:
                skipped += 1
                continue

            if result == bet["choice"]:
                payout = int(bet["stake"]) * 2

                await add_money(
                    int(bet["user_id"]),
                    payout,
                    interaction.channel
                )

                status = "won"
            else:
                status = "lost"

            with db() as conn:
                conn.execute(
                    "UPDATE bets SET status=? WHERE id=?",
                    (status, bet["id"])
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
# /PING
# ============================================================

@bot.tree.command(
    name="ping",
    description="Kiểm tra bot",
    guild=GUILD
)
async def ping(interaction: discord.Interaction):
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
async def apiquota(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"📡 API-Football requests còn lại: "
        f"**{football.last_quota or 'chưa gọi API'}**",
        ephemeral=True
    )


# ============================================================
# /CADO
# ============================================================

@bot.tree.command(
    name="cado",
    description="Xem thông tin tài khoản",
    guild=GUILD
)
async def cado(interaction: discord.Interaction):
    balance = get_balance(interaction.user.id)
    level = get_vip(balance)

    await interaction.response.send_message(
        f"👤 **{interaction.user.display_name}**\n"
        f"💰 Ví: **{money(balance)} xu**\n"
        f"{vip_icon(level)} **{vip_name(level)}**",
        ephemeral=True
    )


# ============================================================
# RAILWAY HEALTH SERVER
# ============================================================

health_runner = None


async def health_handler(request):
    return web.Response(text="OK")


async def start_health_server():
    global health_runner

    if health_runner is not None:
        return

    port = int(os.getenv("PORT", "8080"))

    app = web.Application()

    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)

    health_runner = web.AppRunner(app)

    await health_runner.setup()

    site = web.TCPSite(
        health_runner,
        "0.0.0.0",
        port
    )

    await site.start()

    print(f"[WEB] Health server: {port}")


# ============================================================
# READY + SYNC
# ============================================================

@bot.event
async def on_ready():
    print(
        f"[DISCORD] Logged in as "
        f"{bot.user} ID={bot.user.id}"
    )

    print(f"[DISCORD] Guild ID: {GUILD_ID}")

    try:
        synced = await bot.tree.sync(guild=GUILD)

        print(
            f"[DISCORD] Synced {len(synced)} guild commands."
        )

        print(
            "[DISCORD] Commands: "
            + ", ".join(
                sorted(command.name for command in synced)
            )
        )

    except Exception as e:
        print(
            f"[DISCORD] SYNC ERROR: "
            f"{type(e).__name__}: {e}"
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
            await bot.start(DISCORD_TOKEN)

    finally:
        await football.close()


if __name__ == "__main__":
    asyncio.run(main())

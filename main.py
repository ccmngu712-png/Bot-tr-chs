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

# Guild Discord của m
GUILD_ID = 1551547049600618538
GUILD = discord.Object(id=GUILD_ID)

# API Football
API_BASE = "https://v3.football.api-sports.io"
TIMEZONE = "Asia/Ho_Chi_Minh"
TZ = ZoneInfo(TIMEZONE)

# Database
DB_PATH = "/data/football_bot.db" if os.path.isdir("/data") else "football_bot.db"

# Tiền
STARTING_BALANCE = 100000
STARTER_BONUS = 1000
DAILY_BONUS = 50000

# Man City API-Football team ID
MANCHESTER_CITY_TEAM_ID = 50


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


def get_vip_level(balance: int) -> int:
    for level, minimum in VIP_LEVELS:
        if balance >= minimum:
            return level
    return 0


def vip_name(level: int) -> str:
    if level == 0:
        return "Thành viên"
    return f"VIP {level}"


def vip_emoji(level: int) -> str:
    if level >= 10:
        return "👑"
    if level >= 7:
        return "💎"
    if level >= 4:
        return "🔥"
    if level >= 1:
        return "⭐"
    return "👤"


def fmt_money(number: int) -> str:
    return f"{int(number):,}".replace(",", ".")


def next_vip(level: int):
    for lv, minimum in reversed(VIP_LEVELS):
        if lv > level:
            continue

    for lv, minimum in VIP_LEVELS:
        if lv < level:
            continue
        if lv == level:
            continue
        return lv, minimum

    return None, None


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


def ensure_wallet(user_id: int):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM wallets WHERE user_id=?",
            (user_id,)
        ).fetchone()

        if row is None:
            conn.execute(
                """
                INSERT INTO wallets(user_id, balance, started, vip)
                VALUES (?, ?, 0, ?)
                """,
                (
                    user_id,
                    STARTING_BALANCE,
                    get_vip_level(STARTING_BALANCE),
                )
            )
            conn.commit()

            return STARTING_BALANCE

        # Tự sửa VIP nếu database cũ bị lệch
        correct_vip = get_vip_level(int(row["balance"]))

        if int(row["vip"]) != correct_vip:
            conn.execute(
                "UPDATE wallets SET vip=? WHERE user_id=?",
                (correct_vip, user_id)
            )
            conn.commit()

        return int(row["balance"])


def get_balance(user_id: int) -> int:
    return ensure_wallet(user_id)


async def check_vip_promotion(user_id: int, channel=None):
    ensure_wallet(user_id)

    with db() as conn:
        row = conn.execute(
            "SELECT balance, vip FROM wallets WHERE user_id=?",
            (user_id,)
        ).fetchone()

        if not row:
            return

        balance = int(row["balance"])
        old_vip = int(row["vip"])
        new_vip = get_vip_level(balance)

        if new_vip == old_vip:
            return

        conn.execute(
            "UPDATE wallets SET vip=? WHERE user_id=?",
            (new_vip, user_id)
        )
        conn.commit()

    # Chỉ thông báo khi LÊN VIP
    if new_vip > old_vip and channel:

        user = bot.get_user(user_id)

        name = (
            user.display_name
            if user
            else str(user_id)
        )

        embed = discord.Embed(
            title=f"👑🎉 CHÚC MỪNG {vip_name(new_vip).upper()} 🎉👑",
            description=(
                "╔══════════════════════╗\n"
                f"     {vip_emoji(new_vip)} **{vip_name(new_vip)}**\n"
                "╚══════════════════════╝\n\n"
                f"👤 Thành viên: **{name}**\n"
                f"💰 Số dư: **{fmt_money(balance)} xu**\n\n"
                f"🔥 Bạn đã đạt **{vip_name(new_vip)}**!"
            ),
            color=discord.Color.gold()
        )

        embed.set_footer(
            text="⚽ Football Virtual Betting"
        )

        try:
            await channel.send(embed=embed)
        except Exception:
            pass


async def add_balance(user_id: int, amount: int, channel=None):
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

    await check_vip_promotion(user_id, channel)


async def remove_balance(user_id: int, amount: int):
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

    await check_vip_promotion(user_id)


# ============================================================
# FOOTBALL API
# ============================================================

class FootballAPI:

    def __init__(self):
        self.session = None
        self.cache = {}
        self.cache_ttl = 30
        self.last_quota = None

    async def start(self):

        if self.session is None or self.session.closed:

            timeout = aiohttp.ClientTimeout(total=20)

            self.session = aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    "x-apisports-key": FOOTBALL_API_KEY,
                    "Accept": "application/json",
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
            tuple(
                sorted(
                    (str(k), str(v))
                    for k, v in params.items()
                )
            )
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

                    if response.status >= 500:

                        if attempt < 2:
                            await asyncio.sleep(
                                1 + attempt
                            )
                            continue

                    if response.status >= 400:
                        raise RuntimeError(
                            f"HTTP {response.status}: "
                            f"{data.get('errors', raw[:300])}"
                        )

                    errors = data.get("errors")

                    if isinstance(errors, dict):

                        real_errors = [
                            value
                            for value in errors.values()
                            if value
                        ]

                        if real_errors:
                            raise RuntimeError(
                                str(real_errors)
                            )

                    self.cache[cache_key] = (
                        now,
                        data
                    )

                    return data

            except Exception as exc:

                last_error = exc

                if attempt < 2:
                    await asyncio.sleep(
                        1 + attempt
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

        return data.get("response", [])

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

        return data.get("response", [])

    async def prediction(
        self,
        fixture_id
    ):

        data = await self.request(
            "predictions",
            {
                "fixture": fixture_id
            }
        )

        return data.get("response", [])

    async def fixture(
        self,
        fixture_id
    ):

        data = await self.request(
            "fixtures",
            {
                "id": fixture_id,
                "timezone": TIMEZONE
            }
        )

        return data.get("response", [])


football = FootballAPI()


# ============================================================
# FOOTBALL HELPERS
# ============================================================

def fixture_status(fixture):

    return (
        (fixture.get("fixture") or {})
        .get("status", {})
        .get("short", "")
    )


def upcoming(fixture):

    return fixture_status(fixture) in {
        "NS",
        "TBD"
    }


def fixture_id(fixture):

    return (
        (fixture.get("fixture") or {})
        .get("id")
    )


def fixture_time(fixture):

    value = (
        (fixture.get("fixture") or {})
        .get("date")
    )

    if not value:
        return "Chưa có giờ"

    try:

        dt = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

        dt = dt.astimezone(TZ)

        return dt.strftime(
            "%d/%m/%Y %H:%M"
        )

    except Exception:

        return str(value)


def team_name(fixture, side):

    return (
        (fixture.get("teams") or {})
        .get(side, {})
        .get("name")
        or "?"
    )


def result_from_fixture(fixture):

    status = fixture_status(fixture)

    if status in {
        "NS",
        "TBD",
        "PST",
        "CANC",
        "ABD",
        "AWD",
        "WO"
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


def choice_name(choice, fixture):

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


def api_error_embed(exc):

    text = str(exc)

    if (
        "Free plans" in text
        or "try from 2022 to 2024" in text
    ):

        return discord.Embed(
            title="⚠️ API-Football bị giới hạn",
            description=(
                "API key hiện tại không có quyền "
                "lấy dữ liệu mùa giải này.\n\n"
                "Bot không thể tự vượt giới hạn của API."
            ),
            color=discord.Color.orange()
        )

    return discord.Embed(
        title="❌ Không lấy được dữ liệu",
        description=f"`{text[:900]}`",
        color=discord.Color.red()
    )


# ============================================================
# MATCH ANALYSIS
# ============================================================

async def send_analysis(
    interaction,
    fixture,
    league_name
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
            f"🏆 **{league_name}**\n"
            f"📅 **{fixture_time(fixture)}**\n"
            f"🆔 Fixture: `{fid}`"
        ),
        color=discord.Color.blurple()
    )

    # Tỷ số thật nếu trận đã đá
    goals = fixture.get("goals") or {}

    if (
        goals.get("home") is not None
        or goals.get("away") is not None
    ):

        embed.add_field(
            name="⚽ Tỷ số",
            value=(
                f"**{goals.get('home', 0)}"
                f" - "
                f"{goals.get('away', 0)}**"
            ),
            inline=False
        )

    try:

        responses = await football.prediction(
            int(fid)
        )

        if not responses:

            embed.add_field(
                name="🎯 Dự đoán",
                value=(
                    "API chưa có dữ liệu "
                    "dự đoán cho trận này."
                ),
                inline=False
            )

        else:

            prediction = (
                responses[0]
                .get("predictions")
                or {}
            )

            # Winner
            winner = (
                prediction
                .get("winner")
                or {}
            )

            winner_name = (
                winner.get("name")
                or "Chưa xác định"
            )

            # Probability
            percent = (
                prediction
                .get("percent")
                or {}
            )

            home_percent = percent.get(
                "home",
                "?"
            )

            draw_percent = percent.get(
                "draw",
                "?"
            )

            away_percent = percent.get(
                "away",
                "?"
            )

            # Exact score
            score = (
                prediction
                .get("score")
                or {}
            )

            exact_home = score.get(
                "home"
            )

            exact_away = score.get(
                "away"
            )

            if (
                isinstance(exact_home, int)
                and isinstance(exact_away, int)
            ):

                exact = (
                    f"**{exact_home} - "
                    f"{exact_away}**"
                )

            else:

                goals_prediction = (
                    prediction.get("goals")
                    or {}
                )

                gh = goals_prediction.get(
                    "home"
                )

                ga = goals_prediction.get(
                    "away"
                )

                if (
                    isinstance(gh, int)
                    and isinstance(ga, int)
                ):

                    exact = (
                        f"**{gh} - {ga}**"
                    )

                else:

                    exact = (
                        "API chưa trả "
                        "tỷ số chính xác"
                    )

            embed.add_field(
                name="🎯 DỰ ĐOÁN TỶ SỐ",
                value=exact,
                inline=False
            )

            embed.add_field(
                name="📈 XÁC SUẤT",
                value=(
                    f"🏠 Home: **{home_percent}%**\n"
                    f"🤝 Draw: **{draw_percent}%**\n"
                    f"✈️ Away: **{away_percent}%**"
                ),
                inline=False
            )

            embed.add_field(
                name="🏁 KẾT LUẬN API",
                value=f"**{winner_name}**",
                inline=False
            )

    except Exception:

        embed.add_field(
            name="🎯 Dự đoán",
            value=(
                "Không lấy được dữ liệu "
                "dự đoán lúc này."
            ),
            inline=False
        )

    embed.set_footer(
        text=(
            "API-Football • "
            "Dự đoán chỉ mang tính tham khảo"
        )
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

        self.fixtures = {
            str(fixture_id(f)): f
            for f in fixtures
            if fixture_id(f)
        }

        self.league_name = league_name
        self.mode = mode

        options = []

        for f in fixtures[:25]:

            fid = fixture_id(f)

            if not fid:
                continue

            options.append(
                discord.SelectOption(
                    label=(
                        f"{team_name(f,'home')} "
                        f"vs "
                        f"{team_name(f,'away')}"
                    )[:100],
                    value=str(fid),
                    description=fixture_time(f)[:100]
                )
            )

        super().__init__(
            placeholder="⚽ Chọn trận...",
            min_values=1,
            max_values=1,
            options=options
        )

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

        # SOI
        if self.mode == "soi":

            await interaction.response.defer(
                ephemeral=True
            )

            await send_analysis(
                interaction,
                fixture,
                self.league_name
            )

            return

        # CUOC
        await interaction.response.send_modal(
            BetModal(fixture)
        )


class MatchView(discord.ui.View):

    def __init__(
        self,
        fixtures,
        league_name,
        mode
    ):

        super().__init__(timeout=180)

        self.add_item(
            MatchSelect(
                fixtures,
                league_name,
                mode
            )
        )


# ============================================================
# BET MODAL
# ============================================================

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
                .strip()
            )

        except Exception:

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

        balance = get_balance(
            interaction.user.id
        )

        if amount > balance:

            await interaction.response.send_message(
                (
                    "❌ Không đủ xu.\n"
                    f"💰 Ví hiện tại: "
                    f"**{fmt_money(balance)} xu**"
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
                    f"**{team_name(self.fixture,'away')}**\n\n"
                    f"💰 Tiền cược: "
                    f"**{fmt_money(amount)} xu**"
                ),
                color=discord.Color.gold()
            ),
            view=BetChoiceView(
                self.fixture,
                amount
            ),
            ephemeral=True
        )


# ============================================================
# BET CHOICE
# ============================================================

class BetChoiceView(discord.ui.View):

    def __init__(
        self,
        fixture,
        amount
    ):

        super().__init__(timeout=120)

        self.fixture = fixture
        self.amount = amount

    async def place(
        self,
        interaction,
        choice
    ):

        user_id = interaction.user.id
        fid = fixture_id(self.fixture)

        balance = get_balance(
            user_id
        )

        if self.amount > balance:

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
                INSERT INTO bets(
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
                    self.amount,
                    datetime.now(TZ).isoformat()
                )
            )

            conn.commit()

        await interaction.response.send_message(
            (
                "✅ **ĐẶT CƯỢC THÀNH CÔNG**\n\n"
                f"⚽ Trận: "
                f"**{team_name(self.fixture,'home')} "
                f"vs "
                f"{team_name(self.fixture,'away')}**\n"
                f"🎯 Cửa: **{choice_name(choice,self.fixture)}**\n"
                f"💰 Cược: **{fmt_money(self.amount)} xu**\n"
                f"💳 Còn lại: "
                f"**{fmt_money(get_balance(user_id))} xu**"
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


# ============================================================
# /CUOC
# ============================================================

class CuocLeagueSelect(
    discord.ui.Select
):

    def __init__(self):

        options = []

        for lid, data in LEAGUES.items():

            emoji, name = data

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

    async def callback(
        self,
        interaction
    ):

        league_id = int(
            self.values[0]
        )

        emoji, league_name = LEAGUES[
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
                f for f in fixtures
                if upcoming(f)
            ]

            if not fixtures:

                await interaction.followup.send(
                    f"❌ Chưa có trận sắp tới trong **{league_name}**.",
                    ephemeral=True
                )
                return

            await interaction.followup.send(
                embed=discord.Embed(
                    title=f"{emoji} {league_name}",
                    description=(
                        "Chọn trận → nhập tiền → "
                        "chọn cửa cược."
                    ),
                    color=discord.Color.green()
                ),
                view=MatchView(
                    fixtures,
                    league_name,
                    "bet"
                ),
                ephemeral=True
            )

        except Exception as exc:

            await interaction.followup.send(
                embed=api_error_embed(exc),
                ephemeral=True
            )


class CuocView(
    discord.ui.View
):

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
                if upcoming(f)
            ]

            if not fixtures:

                await interaction.followup.send(
                    "❌ API chưa trả trận Man City sắp tới.",
                    ephemeral=True
                )
                return

            await interaction.followup.send(
                embed=discord.Embed(
                    title="🔵 MAN CITY — CÁC TRẬN SẮP TỚI",
                    description=(
                        f"Tìm thấy **{len(fixtures)}** trận."
                    ),
                    color=discord.Color.blue()
                ),
                view=MatchView(
                    fixtures,
                    "Man City",
                    "bet"
                ),
                ephemeral=True
            )

        except Exception as exc:

            await interaction.followup.send(
                embed=api_error_embed(exc),
                ephemeral=True
            )


# ============================================================
# /SOI
# ============================================================

class SoiLeagueSelect(
    discord.ui.Select
):

    def __init__(self):

        options = []

        for lid, data in LEAGUES.items():

            emoji, name = data

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

    async def callback(
        self,
        interaction
    ):

        league_id = int(
            self.values[0]
        )

        emoji, league_name = LEAGUES[
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
                f for f in fixtures
                if upcoming(f)
            ]

            if not fixtures:

                await interaction.followup.send(
                    f"❌ Chưa có trận sắp tới trong **{league_name}**.",
                    ephemeral=True
                )
                return

            await interaction.followup.send(
                embed=discord.Embed(
                    title=f"{emoji} {league_name}",
                    description=(
                        "Chọn trận để xem dự đoán, "
                        "xác suất và tỷ số."
                    ),
                    color=discord.Color.blurple()
                ),
                view=MatchView(
                    fixtures,
                    league_name,
                    "soi"
                ),
                ephemeral=True
            )

        except Exception as exc:

            await interaction.followup.send(
                embed=api_error_embed(exc),
                ephemeral=True
            )


class SoiView(
    discord.ui.View
):

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
                if upcoming(f)
            ]

            await interaction.followup.send(
                embed=discord.Embed(
                    title="🔵 MAN CITY — TẤT CẢ TRẬN",
                    description=(
                        f"**{len(fixtures)}** trận "
                        "sắp tới trên mọi giải."
                    ),
                    color=discord.Color.blue()
                ),
                view=MatchView(
                    fixtures,
                    "Man City",
                    "soi"
                ),
                ephemeral=True
            )

        except Exception as exc:

            await interaction.followup.send(
                embed=api_error_embed(exc),
                ephemeral=True
            )


# ============================================================
# COMMAND: /CUOC
# ============================================================

@bot.tree.command(
    name="cuoc",
    description="Mở bảng cá cược bóng đá"
)
async def cuoc(
    interaction: discord.Interaction
):

    ensure_wallet(
        interaction.user.id
    )

    embed = discord.Embed(
        title="⚽🎰 CÁ CƯỢC BÓNG ĐÁ",
        description=(
            "🏆 Chọn giải đấu bên dưới\n"
            "🔵 Hoặc xem riêng toàn bộ trận Man City\n\n"
            "💰 Tiền cược là **xu ảo**."
        ),
        color=discord.Color.gold()
    )

    embed.set_footer(
        text="Football Virtual Betting"
    )

    await interaction.response.send_message(
        embed=embed,
        view=CuocView(),
        ephemeral=True
    )


# ============================================================
# COMMAND: /SOI
# ============================================================

@bot.tree.command(
    name="soi",
    description="Xem lịch và dự đoán bóng đá"
)
async def soi(
    interaction: discord.Interaction
):

    embed = discord.Embed(
        title="🔮⚽ SOI BÓNG ĐÁ",
        description=(
            "🏆 Chọn giải đấu để xem các trận sắp tới.\n"
            "🔵 Nút riêng để xem **tất cả trận Man City**."
        ),
        color=discord.Color.blurple()
    )

    embed.set_footer(
        text="Dữ liệu và dự đoán từ API-Football"
    )

    await interaction.response.send_message(
        embed=embed,
        view=SoiView(),
        ephemeral=True
    )


# ============================================================
# COMMAND: /VI
# ============================================================

@bot.tree.command(
    name="vi",
    description="Xem ví và VIP"
)
async def vi(
    interaction: discord.Interaction
):

    user_id = interaction.user.id

    balance = get_balance(
        user_id
    )

    level = get_vip_level(
        balance
    )

    # Đồng bộ VIP
    with db() as conn:
        conn.execute(
            "UPDATE wallets SET vip=? WHERE user_id=?",
            (level, user_id)
        )
        conn.commit()

    # Mốc VIP kế tiếp
    next_level = None
    next_amount = None

    for lv, amount in VIP_LEVELS:

        if lv > level:

            next_level = lv
            next_amount = amount

    if next_level:

        need = max(
            0,
            next_amount - balance
        )

        next_text = (
            f"🎯 {vip_name(next_level)}: "
            f"**{fmt_money(next_amount)} xu**\n"
            f"📌 Còn thiếu: "
            f"**{fmt_money(need)} xu**"
        )

    else:

        next_text = (
            "👑 **M đã đạt VIP cao nhất!**"
        )

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
        name="👑 VIP",
        value=(
            f"{vip_emoji(level)} "
            f"**{vip_name(level)}**"
        ),
        inline=False
    )

    embed.add_field(
        name="📈 VIP tiếp theo",
        value=next_text,
        inline=False
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# ============================================================
# COMMAND: /KHOINGHIEP
# ============================================================

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

    await check_vip_promotion(
        user_id,
        interaction.channel
    )

    await interaction.response.send_message(
        (
            "🎁 **NHẬN THƯỞNG KHỞI NGHIỆP**\n\n"
            f"💰 +**{fmt_money(STARTER_BONUS)} xu**\n"
            f"💳 Số dư: "
            f"**{fmt_money(get_balance(user_id))} xu**"
        ),
        ephemeral=True
    )


# ============================================================
# COMMAND: /COMAT
# ============================================================

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
    ).strftime("%Y-%m-%d")

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
                "❌ Hôm nay m đã nhận **50.000 xu** rồi.",
                ephemeral=True
            )
            return

        conn.execute(
            """
            INSERT INTO daily_claims(
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

    await check_vip_promotion(
        user_id,
        interaction.channel
    )

    await interaction.response.send_message(
        (
            "🎁 **THƯỞNG ĐIỂM DANH**\n\n"
            f"💰 +**{fmt_money(DAILY_BONUS)} xu**\n"
            f"💳 Số dư: "
            f"**{fmt_money(get_balance(user_id))} xu**"
        ),
        ephemeral=True
    )


# ============================================================
# COMMAND: /ADMIN
# ============================================================

@bot.tree.command(
    name="admin",
    description="Admin cộng hoặc trừ xu"
)
@app_commands.describe(
    action="add hoặc remove",
    member="Người cần chỉnh xu",
    amount="Số xu",
    password="Mật khẩu admin"
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
    interaction: discord.Interaction,
    action: app_commands.Choice[str],
    member: discord.Member,
    amount: int,
    password: str
):

    # Mật khẩu lấy từ Railway variable ADMIN_PASSWORD
    admin_password = os.getenv(
        "ADMIN_PASSWORD",
        "provip👑"
    )

    # Có quyền Manage Server hoặc đúng password
    allowed = (
        interaction.user.guild_permissions.manage_guild
        or password == admin_password
    )

    if not allowed:

        await interaction.response.send_message(
            "❌ Không có quyền sử dụng lệnh này.",
            ephemeral=True
        )
        return

    if amount <= 0:

        await interaction.response.send_message(
            "❌ Số xu phải lớn hơn 0.",
            ephemeral=True
        )
        return

    if action.value == "add":

        await add_balance(
            member.id,
            amount,
            interaction.channel
        )

        action_text = "CỘNG"

    else:

        await remove_balance(
            member.id,
            amount
        )

        action_text = "TRỪ"

    await interaction.response.send_message(
        (
            f"✅ **{action_text} XU THÀNH CÔNG**\n\n"
            f"👤 Người chơi: {member.mention}\n"
            f"💰 Số xu: **{fmt_money(amount)}**\n"
            f"💳 Số dư mới: "
            f"**{fmt_money(get_balance(member.id))} xu**\n"
            f"👑 VIP: "
            f"**{vip_name(get_vip_level(get_balance(member.id)))}**"
        ),
        ephemeral=True
    )


# ============================================================
# COMMAND: /KETTOAN
# ============================================================

@bot.tree.command(
    name="kettoan",
    description="Kết toán tất cả cược đã kết thúc"
)
async def kettoan(
    interaction: discord.Interaction
):

    # Chỉ người có quyền Manage Server
    if not interaction.user.guild_permissions.manage_guild:

        await interaction.response.send_message(
            "❌ Chỉ admin/server manager mới dùng được.",
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
            "ℹ️ Hiện không có cược nào cần kết toán.",
            ephemeral=True
        )
        return

    won = 0
    lost = 0
    pending = 0
    total_paid = 0

    for bet in bets:

        try:

            fixtures = await football.fixture(
                int(bet["fixture_id"])
            )

            if not fixtures:

                pending += 1
                continue

            fixture = fixtures[0]

            result = result_from_fixture(
                fixture
            )

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

                # Thắng nhận 2x tổng cược
                payout = stake * 2

                await add_balance(
                    user_id,
                    payout,
                    None
                )

                with db() as conn:

                    conn.execute(
                        """
                        UPDATE bets
                        SET status='won'
                        WHERE id=?
                        """,
                        (bet["id"],)
                    )

                    conn.commit()

                won += 1
                total_paid += payout

            else:

                with db() as conn:

                    conn.execute(
                        """
                        UPDATE bets
                        SET status='lost'
                        WHERE id=?
                        """,
                        (bet["id"],)
                    )

                    conn.commit()

                lost += 1

        except Exception:

            pending += 1

    await interaction.followup.send(
        (
            "⚽ **KẾT TOÁN HOÀN TẤT**\n\n"
            f"✅ Thắng: **{won}**\n"
            f"❌ Thua: **{lost}**\n"
            f"⏳ Chưa kết toán: **{pending}**\n"
            f"💰 Tổng tiền trả: "
            f"**{fmt_money(total_paid)} xu**"
        ),
        ephemeral=True
    )


# ============================================================
# COMMAND: /PING
# ============================================================

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
        f"🏓 Pong! **{latency}ms**",
        ephemeral=True
    )


# ============================================================
# COMMAND: /APIQUOTA
# ============================================================

@bot.tree.command(
    name="apiquota",
    description="Xem quota API-Football gần nhất"
)
async def apiquota(
    interaction: discord.Interaction
):

    quota = football.last_quota

    if quota is None:
        text = (
            "Chưa có response API nào "
            "để kiểm tra quota."
        )
    else:
        text = (
            f"📊 Request còn lại gần nhất: "
            f"**{quota}**"
        )

    await interaction.response.send_message(
        text,
        ephemeral=True
    )


# ============================================================
# COMMAND: /CADO
# ============================================================

@bot.tree.command(
    name="cado",
    description="Mở bảng cá cược"
)
async def cado(
    interaction: discord.Interaction
):

    ensure_wallet(
        interaction.user.id
    )

    embed = discord.Embed(
        title="🎰 CÁ ĐỘ BÓNG ĐÁ",
        description=(
            "Dùng **/cuoc** để chọn giải "
            "và đặt cược.\n\n"
            f"💰 Ví hiện tại: "
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
# RAILWAY HEALTH SERVER
# ============================================================

async def health(request):

    return web.json_response({
        "status": "ok",
        "bot": str(bot.user) if bot.user else None
    })


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

    port = int(
        os.getenv(
            "PORT",
            "8080"
        )
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
        f"Health server running on port {port}"
    )


# ============================================================
# BOT EVENTS
# ============================================================

health_started = False


@bot.event
async def on_ready():

    global health_started

    init_db()

    await football.start()

    # Sync slash commands
    try:

        synced = await bot.tree.sync(
            guild=GUILD
        )

        print(
            f"Synced {len(synced)} guild commands."
        )

    except Exception as exc:

        print(
            "Guild sync error:",
            exc
        )

    if not health_started:

        await start_health_server()

        health_started = True

    print(
        f"Logged in as {bot.user}"
    )


# ============================================================
# SHUTDOWN
# ============================================================

async def shutdown():

    await football.close()


# ============================================================
# RUN
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
                shutdown()
            )
        except Exception:
            pass

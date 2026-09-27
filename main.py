import os
import json
import time
import sqlite3
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from aiohttp import web


# =========================================================
# CONFIG
# =========================================================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY")

GUILD_ID = 1551547049600618538
STARTUP_CHANNEL_ID = 1553673308971797133042

API_BASE = "https://v3.football.api-sports.io"
TZ = ZoneInfo("Asia/Ho_Chi_Minh")

if not DISCORD_TOKEN:
    raise RuntimeError("Thiếu DISCORD_TOKEN trong Railway Variables")

if not FOOTBALL_API_KEY:
    raise RuntimeError("Thiếu FOOTBALL_API_KEY trong Railway Variables")


# =========================================================
# DATABASE
# =========================================================

DB_FILE = "bot.db"

db = sqlite3.connect(DB_FILE, check_same_thread=False)
db.row_factory = sqlite3.Row

db.execute("""
CREATE TABLE IF NOT EXISTS wallets (
    user_id INTEGER PRIMARY KEY,
    balance INTEGER NOT NULL DEFAULT 10000
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS bets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    fixture_id INTEGER NOT NULL,
    choice TEXT NOT NULL,
    amount INTEGER NOT NULL,
    odds REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
)
""")

db.commit()


def get_balance(user_id: int) -> int:
    row = db.execute(
        "SELECT balance FROM wallets WHERE user_id = ?",
        (user_id,)
    ).fetchone()

    if row is None:
        db.execute(
            "INSERT INTO wallets(user_id, balance) VALUES (?, ?)",
            (user_id, 10000)
        )
        db.commit()
        return 10000

    return int(row["balance"])


def set_balance(user_id: int, balance: int):
    db.execute(
        "INSERT INTO wallets(user_id, balance) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET balance = excluded.balance",
        (user_id, balance)
    )
    db.commit()


# =========================================================
# API FOOTBALL
# =========================================================

class APIError(Exception):
    pass


class APIFreeSeasonError(APIError):
    pass


class FootballAPI:

    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None

        self.cache = {}
        self.cache_seconds = 60

        self.last_requests = []

        self.daily_remaining = None
        self.daily_limit = None

    async def start(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={
                    "x-apisports-key": FOOTBALL_API_KEY,
                    "Accept": "application/json"
                },
                timeout=aiohttp.ClientTimeout(total=20)
            )

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def rate_limit(self):
        now = time.time()

        self.last_requests = [
            x for x in self.last_requests
            if now - x < 60
        ]

        if len(self.last_requests) >= 9:
            wait = 60 - (now - self.last_requests[0]) + 0.5

            if wait > 0:
                await asyncio.sleep(wait)

        self.last_requests.append(time.time())

    async def get(self, endpoint: str, params=None, cache=True):

        await self.start()

        if params is None:
            params = {}

        cache_key = endpoint + "?" + json.dumps(
            params,
            sort_keys=True
        )

        if cache:
            cached = self.cache.get(cache_key)

            if cached:
                created, value = cached

                if time.time() - created < self.cache_seconds:
                    return value

        await self.rate_limit()

        try:
            async with self.session.get(
                API_BASE + endpoint,
                params=params
            ) as response:

                text = await response.text()

                try:
                    data = json.loads(text)
                except Exception:
                    raise APIError(
                        f"API trả về dữ liệu không hợp lệ: {text[:300]}"
                    )

                # -----------------------------------------
                # QUOTA
                # -----------------------------------------

                headers = response.headers

                self.daily_remaining = (
                    headers.get("x-ratelimit-requests-remaining")
                    or headers.get("X-RateLimit-Requests-Remaining")
                )

                self.daily_limit = (
                    headers.get("x-ratelimit-requests-limit")
                    or headers.get("X-RateLimit-Requests-Limit")
                )

                # -----------------------------------------
                # API ERROR
                # -----------------------------------------

                errors = data.get("errors")

                if errors:
                    error_text = str(errors)

                    if (
                        "Free plans do not have access" in error_text
                        or "do not have access to this season" in error_text
                        or "season" in error_text.lower()
                        and "free" in error_text.lower()
                    ):
                        raise APIFreeSeasonError(error_text)

                    raise APIError(error_text)

                if response.status == 429:
                    raise APIError(
                        "API đang giới hạn request. Chờ một chút rồi thử lại."
                    )

                if response.status >= 400:
                    raise APIError(
                        f"API HTTP {response.status}: {text[:300]}"
                    )

                result = data.get("response", [])

                if cache:
                    self.cache[cache_key] = (
                        time.time(),
                        result
                    )

                return result

        except asyncio.TimeoutError:
            raise APIError("API timeout.")

        except aiohttp.ClientError as e:
            raise APIError(f"Lỗi kết nối API: {e}")


football = FootballAPI()


# =========================================================
# DISCORD BOT
# =========================================================

intents = discord.Intents.default()

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


# =========================================================
# HELPERS
# =========================================================

def now_vn():
    return datetime.now(TZ)


def season_for_date(date_string: str) -> int:
    """
    Football season thường được tính theo năm bắt đầu.

    Ví dụ:
    2025-10 -> season 2025
    2026-02 -> season 2025
    2026-08 -> season 2026
    """

    dt = datetime.strptime(date_string, "%Y-%m-%d")

    if dt.month >= 7:
        return dt.year

    return dt.year - 1


def format_match_time(utc_string: str) -> str:
    try:
        dt = datetime.fromisoformat(
            utc_string.replace("Z", "+00:00")
        )

        return dt.astimezone(TZ).strftime(
            "%d/%m/%Y %H:%M"
        )

    except Exception:
        return utc_string


def shorten(text: str, max_len: int = 90):
    text = str(text)

    if len(text) <= max_len:
        return text

    return text[:max_len - 3] + "..."


def get_fixture_name(fixture):
    home = fixture.get("teams", {}).get("home", {})
    away = fixture.get("teams", {}).get("away", {})

    return (
        home.get("name", "Home"),
        away.get("name", "Away")
    )


# =========================================================
# EMBEDS
# =========================================================

def error_embed(title, description):
    return discord.Embed(
        title=title,
        description=description,
        color=discord.Color.red()
    )


def success_embed(title, description):
    return discord.Embed(
        title=title,
        description=description,
        color=discord.Color.green()
    )


# =========================================================
# /PING
# =========================================================

@bot.tree.command(
    name="ping",
    description="Kiểm tra bot"
)
async def ping(interaction: discord.Interaction):

    await interaction.response.send_message(
        f"🏓 Pong! `{round(bot.latency * 1000)}ms`"
    )


# =========================================================
# /APIQUOTA
# =========================================================

@bot.tree.command(
    name="apiquota",
    description="Xem quota API-Football"
)
async def apiquota(interaction: discord.Interaction):

    await interaction.response.defer()

    try:
        await football.get(
            "/status",
            cache=False
        )

        remaining = football.daily_remaining
        limit = football.daily_limit

        if remaining is None:
            text = "Không đọc được quota từ header API."

        else:
            text = (
                f"📊 API quota\n"
                f"• Còn lại: `{remaining}`\n"
                f"• Giới hạn: `{limit or '?'}`"
            )

        await interaction.edit_original_response(
            content=text
        )

    except Exception as e:
        await interaction.edit_original_response(
            content=f"❌ {shorten(e)}"
        )


# =========================================================
# LEAGUE DATA
# =========================================================

LEAGUE_ALIASES = {
    "c1": 2,
    "champions league": 2,
    "uefa champions league": 2,

    "epl": 39,
    "premier league": 39,

    "laliga": 140,
    "la liga": 140,

    "serie a": 135,

    "bundesliga": 78,

    "ligue 1": 61,

    "v-league": 340,
    "vietnam": 340
}


async def search_leagues(keyword: str):

    keyword = keyword.strip().lower()

    if keyword in LEAGUE_ALIASES:

        league_id = LEAGUE_ALIASES[keyword]

        try:
            data = await football.get(
                "/leagues",
                {
                    "id": league_id
                }
            )

            return data

        except Exception:
            return []

    try:
        data = await football.get(
            "/leagues",
            {
                "search": keyword
            }
        )

        return data[:25]

    except Exception:
        return []


# =========================================================
# /SOI
# =========================================================

@bot.tree.command(
    name="soi",
    description="Soi bóng đá"
)
async def soi(interaction: discord.Interaction):

    await interaction.response.send_message(
        "⚽ **SOI BÓNG ĐÁ**\n\n"
        "Bấm nút bên dưới để tìm giải đấu.",
        view=LeagueHomeView(),
        ephemeral=True
    )


# =========================================================
# LEAGUE HOME
# =========================================================

class LeagueHomeView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(
        label="🔎 Tìm giải",
        style=discord.ButtonStyle.primary
    )
    async def search(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await interaction.response.send_modal(
            LeagueSearchModal()
        )


# =========================================================
# LEAGUE SEARCH MODAL
# =========================================================

class LeagueSearchModal(discord.ui.Modal):

    def __init__(self):
        super().__init__(
            title="Tìm giải bóng đá"
        )

        self.keyword = discord.ui.TextInput(
            label="Tên giải",
            placeholder="VD: Champions League, Premier League...",
            required=True,
            max_length=80
        )

        self.add_item(self.keyword)

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        await interaction.response.defer(
            ephemeral=True
        )

        results = await search_leagues(
            self.keyword.value
        )

        if not results:

            await interaction.edit_original_response(
                content=(
                    "❌ Không tìm thấy giải.\n\n"
                    "Thử: `C1`, `Premier League`, "
                    "`La Liga`, `Serie A`..."
                )
            )

            return

        await interaction.edit_original_response(
            content="Chọn giải bên dưới:",
            view=LeagueResultsView(results)
        )


# =========================================================
# LEAGUE RESULTS
# =========================================================

class LeagueResultsView(discord.ui.View):

    def __init__(self, leagues):
        super().__init__(timeout=180)

        self.add_item(
            LeagueSelect(leagues)
        )


class LeagueSelect(discord.ui.Select):

    def __init__(self, leagues):

        options = []

        for item in leagues[:25]:

            league = item.get("league", {})

            league_id = league.get("id")
            name = league.get("name", "Unknown")
            country = item.get("country", {}).get(
                "name",
                ""
            )

            label = shorten(name, 90)

            description = shorten(
                f"{country} • ID {league_id}",
                100
            )

            options.append(
                discord.SelectOption(
                    label=label,
                    description=description,
                    value=str(league_id)
                )
            )

        super().__init__(
            placeholder="Chọn giải...",
            options=options
        )

        self.leagues = leagues

    async def callback(
        self,
        interaction: discord.Interaction
    ):

        await interaction.response.defer(
            ephemeral=True
        )

        league_id = int(self.values[0])

        selected = None

        for item in self.leagues:

            league = item.get("league", {})

            if league.get("id") == league_id:
                selected = item
                break

        if selected is None:

            await interaction.edit_original_response(
                content="❌ Không tìm thấy giải."
            )

            return

        league = selected.get("league", {})

        embed = discord.Embed(
            title=f"⚽ {league.get('name', 'League')}",
            description=(
                "Chọn ngày muốn xem lịch trận.\n\n"
                "📅 Ngày phải nhập dạng:\n"
                "`YYYY-MM-DD`\n\n"
                "Ví dụ: `2026-09-27`"
            ),
            color=discord.Color.blue()
        )

        logo = league.get("logo")

        if logo:
            embed.set_thumbnail(url=logo)

        await interaction.edit_original_response(
            content=None,
            embed=embed,
            view=LeagueDateView(
                league_id,
                league.get("name", "League"),
                logo
            )
        )


# =========================================================
# DATE VIEW
# =========================================================

class LeagueDateView(discord.ui.View):

    def __init__(
        self,
        league_id,
        league_name,
        league_logo
    ):
        super().__init__(timeout=300)

        self.league_id = league_id
        self.league_name = league_name
        self.league_logo = league_logo

    @discord.ui.button(
        label="📅 Chọn ngày",
        style=discord.ButtonStyle.primary
    )
    async def date_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await interaction.response.send_modal(
            DateModal(
                self.league_id,
                self.league_name,
                self.league_logo
            )
        )


# =========================================================
# DATE MODAL
# =========================================================

class DateModal(discord.ui.Modal):

    def __init__(
        self,
        league_id,
        league_name,
        league_logo
    ):

        super().__init__(
            title="Chọn ngày xem trận"
        )

        self.league_id = league_id
        self.league_name = league_name
        self.league_logo = league_logo

        self.date_input = discord.ui.TextInput(
            label="Ngày",
            placeholder="2026-09-27",
            required=True,
            max_length=10
        )

        self.add_item(self.date_input)

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        date_string = self.date_input.value.strip()

        try:
            datetime.strptime(
                date_string,
                "%Y-%m-%d"
            )

        except ValueError:

            await interaction.response.send_message(
                "❌ Ngày sai định dạng.\n"
                "Dùng `YYYY-MM-DD`.",
                ephemeral=True
            )

            return

        await interaction.response.defer(
            ephemeral=True
        )

        await show_fixtures(
            interaction,
            self.league_id,
            self.league_name,
            self.league_logo,
            date_string
        )


# =========================================================
# FIXTURES
# =========================================================

async def show_fixtures(
    interaction,
    league_id,
    league_name,
    league_logo,
    date_string
):

    season = season_for_date(
        date_string
    )

    try:

        fixtures = await football.get(
            "/fixtures",
            {
                "league": league_id,
                "season": season,
                "date": date_string,
                "timezone": "Asia/Ho_Chi_Minh"
            },
            cache=True
        )

    except APIFreeSeasonError:

        await interaction.edit_original_response(
            content=(
                "⚠️ **API-Football Free không có quyền "
                "truy cập season này.**\n\n"
                f"Giải: **{league_name}**\n"
                f"Season API: `{season}`\n"
                f"Ngày: `{date_string}`\n\n"
                "🔓 Đây là giới hạn dữ liệu của API Free, "
                "không phải lỗi Railway hay Discord.\n\n"
                "Bot không giả dữ liệu để tránh đưa lịch trận sai."
            ),
            embed=None,
            view=None
        )

        return

    except APIError as e:

        await interaction.edit_original_response(
            content=(
                "❌ API-Football lỗi:\n"
                f"`{shorten(e, 800)}`"
            ),
            embed=None,
            view=None
        )

        return

    except Exception as e:

        await interaction.edit_original_response(
            content=(
                "❌ Lỗi không xác định:\n"
                f"`{shorten(e, 800)}`"
            ),
            embed=None,
            view=None
        )

        return

    if not fixtures:

        await interaction.edit_original_response(
            content=(
                f"📅 **{date_string}**\n\n"
                "Không có trận nào được API trả về."
            ),
            embed=None,
            view=None
        )

        return

    fixtures = fixtures[:25]

    embed = discord.Embed(
        title=f"⚽ {league_name}",
        description=(
            f"📅 `{date_string}`\n"
            f"🎯 Có `{len(fixtures)}` trận"
        ),
        color=discord.Color.blue()
    )

    if league_logo:
        embed.set_thumbnail(
            url=league_logo
        )

    for fixture in fixtures:

        home, away = get_fixture_name(
            fixture
        )

        fixture_data = fixture.get(
            "fixture",
            {}
        )

        time_string = format_match_time(
            fixture_data.get(
                "date",
                ""
            )
        )

        status = fixture_data.get(
            "status",
            {}
        ).get(
            "short",
            "?"
        )

        embed.add_field(
            name=f"{home} vs {away}",
            value=(
                f"🕐 {time_string}\n"
                f"📌 `{status}`"
            ),
            inline=False
        )

    await interaction.edit_original_response(
        content=None,
        embed=embed,
        view=FixtureListView(
            fixtures,
            league_name
        )
    )


# =========================================================
# FIXTURE LIST
# =========================================================

class FixtureListView(discord.ui.View):

    def __init__(
        self,
        fixtures,
        league_name
    ):

        super().__init__(timeout=300)

        self.add_item(
            FixtureSelect(
                fixtures,
                league_name
            )
        )


class FixtureSelect(discord.ui.Select):

    def __init__(
        self,
        fixtures,
        league_name
    ):

        options = []

        for fixture in fixtures[:25]:

            fixture_id = fixture.get(
                "fixture",
                {}
            ).get(
                "id"
            )

            home, away = get_fixture_name(
                fixture
            )

            fixture_time = format_match_time(
                fixture.get(
                    "fixture",
                    {}
                ).get(
                    "date",
                    ""
                )
            )

            options.append(
                discord.SelectOption(
                    label=shorten(
                        f"{home} vs {away}",
                        100
                    ),
                    description=shorten(
                        fixture_time,
                        100
                    ),
                    value=str(fixture_id)
                )
            )

        super().__init__(
            placeholder="Chọn trận để soi...",
            options=options
        )

        self.fixtures = fixtures
        self.league_name = league_name

    async def callback(
        self,
        interaction: discord.Interaction
    ):

        await interaction.response.defer(
            ephemeral=True
        )

        fixture_id = int(
            self.values[0]
        )

        fixture = None

        for item in self.fixtures:

            if item.get(
                "fixture",
                {}
            ).get(
                "id"
            ) == fixture_id:

                fixture = item
                break

        if fixture is None:

            await interaction.edit_original_response(
                content="❌ Không tìm thấy trận."
            )

            return

        await show_analysis(
            interaction,
            fixture,
            self.league_name
        )


# =========================================================
# MATCH ANALYSIS
# =========================================================

async def show_analysis(
    interaction,
    fixture,
    league_name
):

    fixture_info = fixture.get(
        "fixture",
        {}
    )

    teams = fixture.get(
        "teams",
        {}
    )

    home = teams.get(
        "home",
        {}
    )

    away = teams.get(
        "away",
        {}
    )

    home_id = home.get("id")
    away_id = away.get("id")
    fixture_id = fixture_info.get("id")

    home_name = home.get(
        "name",
        "Home"
    )

    away_name = away.get(
        "name",
        "Away"
    )

    home_logo = home.get(
        "logo"
    )

    away_logo = away.get(
        "logo"
    )

    embed = discord.Embed(
        title=f"🔎 SOI TRẬN",
        description=(
            f"🏆 **{league_name}**\n\n"
            f"⚽ **{home_name}**  vs  **{away_name}**\n\n"
            f"🕐 {format_match_time(fixture_info.get('date', ''))}"
        ),
        color=discord.Color.gold()
    )

    if home_logo:
        embed.set_thumbnail(
            url=home_logo
        )

    # -----------------------------------------------------
    # SCORE
    # -----------------------------------------------------

    goals = fixture.get(
        "goals",
        {}
    )

    home_score = goals.get(
        "home"
    )

    away_score = goals.get(
        "away"
    )

    if home_score is not None:

        embed.add_field(
            name="📊 Tỉ số",
            value=(
                f"**{home_score} - {away_score}**"
            ),
            inline=False
        )

    # -----------------------------------------------------
    # PREDICTION
    # -----------------------------------------------------

    prediction_text = (
        "Chưa lấy được dự đoán."
    )

    try:

        predictions = await football.get(
            "/predictions",
            {
                "fixture": fixture_id
            },
            cache=True
        )

        if predictions:

            pred = predictions[0]

            prediction = pred.get(
                "predictions",
                {}
            )

            winner = prediction.get(
                "winner",
                {}
            )

            winner_name = winner.get(
                "name"
            )

            advice = prediction.get(
                "advice"
            )

            percent = prediction.get(
                "percent",
                {}
            )

            home_percent = percent.get(
                "home"
            )

            draw_percent = percent.get(
                "draw"
            )

            away_percent = percent.get(
                "away"
            )

            lines = []

            if winner_name:
                lines.append(
                    f"🏆 Dự đoán: **{winner_name}**"
                )

            if advice:
                lines.append(
                    f"💡 {advice}"
                )

            if (
                home_percent
                or draw_percent
                or away_percent
            ):

                lines.append(
                    f"📈 {home_name}: `{home_percent or '?'}%`"
                )

                lines.append(
                    f"🤝 Hòa: `{draw_percent or '?'}%`"
                )

                lines.append(
                    f"📉 {away_name}: `{away_percent or '?'}%`"
                )

            if lines:
                prediction_text = "\n".join(
                    lines
                )

    except APIFreeSeasonError:

        prediction_text = (
            "⚠️ Season này bị giới hạn "
            "trên API Free."
        )

    except Exception:

        prediction_text = (
            "⚠️ Không lấy được prediction."
        )

    embed.add_field(
        name="🧠 Phân tích API",
        value=prediction_text,
        inline=False
    )

    # -----------------------------------------------------
    # FORM
    # -----------------------------------------------------

    form_text = ""

    try:

        predictions = await football.get(
            "/predictions",
            {
                "fixture": fixture_id
            },
            cache=True
        )

        if predictions:

            teams_data = predictions[0].get(
                "teams",
                {}
            )

            home_pred = teams_data.get(
                "home",
                {}
            )

            away_pred = teams_data.get(
                "away",
                {}
            )

            home_form = home_pred.get(
                "league",
                {}
            ).get(
                "form"
            )

            away_form = away_pred.get(
                "league",
                {}
            ).get(
                "form"
            )

            if home_form:
                form_text += (
                    f"🏠 {home_name}: "
                    f"`{home_form}`\n"
                )

            if away_form:
                form_text += (
                    f"✈️ {away_name}: "
                    f"`{away_form}`"
                )

    except Exception:
        pass

    if not form_text:
        form_text = "Không có dữ liệu form."

    embed.add_field(
        name="📈 Phong độ",
        value=form_text,
        inline=False
    )

    # -----------------------------------------------------
    # LINKS
    # -----------------------------------------------------

    embed.add_field(
        name="🆔 Fixture",
        value=f"`{fixture_id}`",
        inline=True
    )

    if home_id:
        embed.add_field(
            name="🏠 Home ID",
            value=f"`{home_id}`",
            inline=True
        )

    if away_id:
        embed.add_field(
            name="✈️ Away ID",
            value=f"`{away_id}`",
            inline=True
        )

    embed.set_footer(
        text="Dữ liệu lấy từ API-Football • Không phải lời khuyên cá cược"
    )

    await interaction.edit_original_response(
        content=None,
        embed=embed,
        view=BetMatchView(
            fixture_id,
            home_name,
            away_name
        )
    )


# =========================================================
# WALLET
# =========================================================

@bot.tree.command(
    name="khoinghiep",
    description="Nhận vốn khởi nghiệp 10.000 xu"
)
async def khoinghiep(
    interaction: discord.Interaction
):

    balance = get_balance(
        interaction.user.id
    )

    await interaction.response.send_message(
        f"💰 Ví của m đang có **{balance:,} xu**.",
        ephemeral=True
    )


@bot.tree.command(
    name="vi",
    description="Xem số dư"
)
async def vi(
    interaction: discord.Interaction
):

    balance = get_balance(
        interaction.user.id
    )

    await interaction.response.send_message(
        f"💰 **Ví của {interaction.user.display_name}**\n"
        f"Số dư: **{balance:,} xu**",
        ephemeral=True
    )


# =========================================================
# BET VIEW
# =========================================================

class BetMatchView(discord.ui.View):

    def __init__(
        self,
        fixture_id,
        home_name,
        away_name
    ):

        super().__init__(timeout=300)

        self.fixture_id = fixture_id
        self.home_name = home_name
        self.away_name = away_name

    @discord.ui.button(
        label="🏠 Cược Home",
        style=discord.ButtonStyle.primary
    )
    async def home_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await interaction.response.send_modal(
            BetModal(
                self.fixture_id,
                "home",
                self.home_name,
                self.away_name
            )
        )

    @discord.ui.button(
        label="🤝 Cược Hòa",
        style=discord.ButtonStyle.secondary
    )
    async def draw_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await interaction.response.send_modal(
            BetModal(
                self.fixture_id,
                "draw",
                self.home_name,
                self.away_name
            )
        )

    @discord.ui.button(
        label="✈️ Cược Away",
        style=discord.ButtonStyle.success
    )
    async def away_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await interaction.response.send_modal(
            BetModal(
                self.fixture_id,
                "away",
                self.home_name,
                self.away_name
            )
        )


# =========================================================
# BET MODAL
# =========================================================

class BetModal(discord.ui.Modal):

    def __init__(
        self,
        fixture_id,
        choice,
        home_name,
        away_name
    ):

        super().__init__(
            title="Đặt cược xu"
        )

        self.fixture_id = fixture_id
        self.choice = choice
        self.home_name = home_name
        self.away_name = away_name

        self.amount = discord.ui.TextInput(
            label="Số xu",
            placeholder="VD: 100",
            required=True,
            max_length=10
        )

        self.add_item(
            self.amount
        )

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        try:
            amount = int(
                self.amount.value
            )

        except ValueError:

            await interaction.response.send_message(
                "❌ Số xu không hợp lệ.",
                ephemeral=True
            )

            return

        if amount <= 0:

            await interaction.response.send_message(
                "❌ Số xu phải lớn hơn 0.",
                ephemeral=True
            )

            return

        user_id = interaction.user.id

        balance = get_balance(
            user_id
        )

        if amount > balance:

            await interaction.response.send_message(
                f"❌ M không đủ xu.\n"
                f"Ví: **{balance:,} xu**",
                ephemeral=True
            )

            return

        # Odds cố định cho hệ thống xu ảo.
        # Không phải odds cá cược thật.
        odds = {
            "home": 2.0,
            "draw": 3.0,
            "away": 2.0
        }.get(
            self.choice,
            2.0
        )

        new_balance = balance - amount

        set_balance(
            user_id,
            new_balance
        )

        db.execute(
            """
            INSERT INTO bets
            (
                user_id,
                fixture_id,
                choice,
                amount,
                odds,
                status,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                self.fixture_id,
                self.choice,
                amount,
                odds,
                "pending",
                now_vn().isoformat()
            )
        )

        db.commit()

        choice_name = {
            "home": self.home_name,
            "draw": "Hòa",
            "away": self.away_name
        }.get(
            self.choice,
            self.choice
        )

        await interaction.response.send_message(
            (
                "✅ **Đặt cược thành công**\n\n"
                f"⚽ Trận: **{self.home_name} vs {self.away_name}**\n"
                f"🎯 Chọn: **{choice_name}**\n"
                f"💰 Cược: **{amount:,} xu**\n"
                f"📈 Hệ số: **x{odds}**\n\n"
                f"💵 Ví còn: **{new_balance:,} xu**"
            ),
            ephemeral=True
        )


# =========================================================
# /CUOC
# =========================================================

@bot.tree.command(
    name="cuoc",
    description="Xem các cược đang chờ"
)
async def cuoc(
    interaction: discord.Interaction
):

    user_id = interaction.user.id

    rows = db.execute(
        """
        SELECT *
        FROM bets
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 10
        """,
        (user_id,)
    ).fetchall()

    if not rows:

        await interaction.response.send_message(
            "📭 M chưa có cược nào.",
            ephemeral=True
        )

        return

    lines = []

    for row in rows:

        status = row["status"]

        if status == "pending":
            icon = "⏳"
        elif status == "won":
            icon = "✅"
        elif status == "lost":
            icon = "❌"
        else:
            icon = "↔️"

        lines.append(
            f"{icon} `#{row['id']}` • "
            f"Fixture `{row['fixture_id']}` • "
            f"{row['amount']:,} xu • "
            f"`{row['choice']}`"
        )

    await interaction.response.send_message(
        "🎟️ **Cược của m**\n\n" +
        "\n".join(lines),
        ephemeral=True
    )


# =========================================================
# /KETTOAN
# =========================================================

@bot.tree.command(
    name="kettoan",
    description="Xem thống kê cược của bản thân"
)
async def kettoan(
    interaction: discord.Interaction
):

    user_id = interaction.user.id

    rows = db.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(amount) AS total_amount
        FROM bets
        WHERE user_id = ?
        """,
        (user_id,)
    ).fetchone()

    total = rows["total"] or 0
    amount = rows["total_amount"] or 0

    balance = get_balance(
        user_id
    )

    await interaction.response.send_message(
        (
            "📊 **THỐNG KÊ**\n\n"
            f"🎟️ Tổng cược: **{total}**\n"
            f"💸 Tổng tiền đã cược: **{amount:,} xu**\n"
            f"💰 Số dư hiện tại: **{balance:,} xu**"
        ),
        ephemeral=True
    )


# =========================================================
# HEALTH SERVER FOR RAILWAY
# =========================================================

async def health(request):

    return web.json_response({
        "status": "ok",
        "bot": str(bot.user) if bot.user else None,
        "time": now_vn().isoformat()
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
        f"[WEB] Health server running on port {port}"
    )


# =========================================================
# BOT EVENTS
# =========================================================

@bot.event
async def on_ready():

    print(
        f"[BOT] Logged in as {bot.user}"
    )

    print(
        f"[BOT] Guild: {GUILD_ID}"
    )

    # Không sync ở đây liên tục.
    # Sync được thực hiện một lần trong main().
    print(
        "[BOT] Ready."
    )


# =========================================================
# SYNC COMMANDS
# =========================================================

async def sync_commands():

    guild = discord.Object(
        id=GUILD_ID
    )

    try:

        synced = await bot.tree.sync(
            guild=guild
        )

        print(
            f"[SYNC] Đã sync {len(synced)} command(s) vào guild."
        )

    except Exception as e:

        print(
            f"[SYNC ERROR] {e}"
        )


# =========================================================
# MAIN
# =========================================================

async def main():

    await start_health_server()

    await football.start()

    try:

        await bot.login(
            DISCORD_TOKEN
        )

        await sync_commands()

        await bot.connect()

    finally:

        await football.close()


if __name__ == "__main__":

    try:
        asyncio.run(
            main()
        )

    except KeyboardInterrupt:
        print(
            "Bot stopped."
        )

import os
import json
import time
import sqlite3
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional

import aiohttp
import discord
from discord.ext import commands
from aiohttp import web


# =========================================================
# CONFIG
# =========================================================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY")

# SERVER DISCORD CỦA M
GUILD_ID = 1551547049600618538

# API
API_BASE = "https://v3.football.api-sports.io"

# Múi giờ
TZ = ZoneInfo("Asia/Ho_Chi_Minh")

if not DISCORD_TOKEN:
    raise RuntimeError(
        "❌ Thiếu DISCORD_TOKEN trong Railway Variables"
    )

if not FOOTBALL_API_KEY:
    raise RuntimeError(
        "❌ Thiếu FOOTBALL_API_KEY trong Railway Variables"
    )


# =========================================================
# DATABASE
# =========================================================

DB_FILE = "bot.db"

db = sqlite3.connect(
    DB_FILE,
    check_same_thread=False
)

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
        """
        SELECT balance
        FROM wallets
        WHERE user_id = ?
        """,
        (user_id,)
    ).fetchone()

    if row is None:

        db.execute(
            """
            INSERT INTO wallets(user_id, balance)
            VALUES (?, ?)
            """,
            (user_id, 10000)
        )

        db.commit()

        return 10000

    return int(row["balance"])


def set_balance(
    user_id: int,
    balance: int
):

    db.execute(
        """
        INSERT INTO wallets(user_id, balance)
        VALUES (?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET balance = excluded.balance
        """,
        (
            user_id,
            balance
        )
    )

    db.commit()


# =========================================================
# API ERRORS
# =========================================================

class APIError(Exception):
    pass


class APIFreeSeasonError(APIError):
    pass


# =========================================================
# API FOOTBALL
# =========================================================

class FootballAPI:

    def __init__(self):

        self.session: Optional[
            aiohttp.ClientSession
        ] = None

        self.cache = {}

        self.cache_seconds = 60

        self.request_times = []

        self.daily_remaining = None
        self.daily_limit = None

    async def start(self):

        if (
            self.session is None
            or self.session.closed
        ):

            self.session = aiohttp.ClientSession(
                headers={
                    "x-apisports-key":
                        FOOTBALL_API_KEY,
                    "Accept":
                        "application/json"
                },
                timeout=aiohttp.ClientTimeout(
                    total=20
                )
            )

    async def close(self):

        if (
            self.session
            and not self.session.closed
        ):

            await self.session.close()

    async def rate_limit(self):

        now = time.time()

        self.request_times = [
            x for x in self.request_times
            if now - x < 60
        ]

        # Giữ dưới giới hạn 10 req/min
        if len(self.request_times) >= 9:

            wait_time = (
                60
                - (
                    now
                    - self.request_times[0]
                )
                + 0.5
            )

            if wait_time > 0:
                await asyncio.sleep(
                    wait_time
                )

        self.request_times.append(
            time.time()
        )

    async def get(
        self,
        endpoint: str,
        params=None,
        cache=True
    ):

        await self.start()

        if params is None:
            params = {}

        cache_key = (
            endpoint
            + "?"
            + json.dumps(
                params,
                sort_keys=True
            )
        )

        if cache:

            cached = self.cache.get(
                cache_key
            )

            if cached:

                created, value = cached

                if (
                    time.time() - created
                    < self.cache_seconds
                ):

                    return value

        await self.rate_limit()

        try:

            async with self.session.get(
                API_BASE + endpoint,
                params=params
            ) as response:

                raw_text = await response.text()

                try:

                    data = json.loads(
                        raw_text
                    )

                except Exception:

                    raise APIError(
                        "API trả về dữ liệu không hợp lệ."
                    )

                headers = response.headers

                self.daily_remaining = (
                    headers.get(
                        "x-ratelimit-requests-remaining"
                    )
                    or
                    headers.get(
                        "X-RateLimit-Requests-Remaining"
                    )
                )

                self.daily_limit = (
                    headers.get(
                        "x-ratelimit-requests-limit"
                    )
                    or
                    headers.get(
                        "X-RateLimit-Requests-Limit"
                    )
                )

                errors = data.get(
                    "errors"
                )

                if errors:

                    error_text = str(
                        errors
                    )

                    lowered = (
                        error_text.lower()
                    )

                    if (
                        "free plans do not have access"
                        in lowered
                        or
                        (
                            "season"
                            in lowered
                            and "free"
                            in lowered
                        )
                    ):

                        raise APIFreeSeasonError(
                            error_text
                        )

                    raise APIError(
                        error_text
                    )

                if response.status == 429:

                    raise APIError(
                        "API đang giới hạn request. "
                        "Chờ một chút rồi thử lại."
                    )

                if response.status >= 400:

                    raise APIError(
                        f"HTTP {response.status}: "
                        f"{raw_text[:300]}"
                    )

                result = data.get(
                    "response",
                    []
                )

                if cache:

                    self.cache[cache_key] = (
                        time.time(),
                        result
                    )

                return result

        except asyncio.TimeoutError:

            raise APIError(
                "API timeout."
            )

        except aiohttp.ClientError as e:

            raise APIError(
                f"Lỗi kết nối API: {e}"
            )


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


def shorten(
    text,
    max_len=90
):

    text = str(text)

    if len(text) <= max_len:
        return text

    return text[:max_len - 3] + "..."


def season_for_date(
    date_string: str
):

    dt = datetime.strptime(
        date_string,
        "%Y-%m-%d"
    )

    # Season bắt đầu khoảng tháng 7
    if dt.month >= 7:
        return dt.year

    return dt.year - 1


def format_match_time(
    utc_string
):

    try:

        dt = datetime.fromisoformat(
            utc_string.replace(
                "Z",
                "+00:00"
            )
        )

        return dt.astimezone(
            TZ
        ).strftime(
            "%d/%m/%Y %H:%M"
        )

    except Exception:

        return str(
            utc_string
        )


def get_fixture_teams(
    fixture
):

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

    return home, away


# =========================================================
# LEAGUE ALIASES
# =========================================================

LEAGUE_ALIASES = {

    "c1": 2,

    "champions league": 2,

    "uefa champions league": 2,

    "ucl": 2,

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


# =========================================================
# LEAGUE SEARCH
# =========================================================

async def search_leagues(
    keyword: str
):

    keyword = (
        keyword
        .strip()
        .lower()
    )

    # Tìm bằng alias
    if keyword in LEAGUE_ALIASES:

        league_id = (
            LEAGUE_ALIASES[
                keyword
            ]
        )

        return await football.get(
            "/leagues",
            {
                "id": league_id
            }
        )

    # Tìm API
    try:

        return await football.get(
            "/leagues",
            {
                "search": keyword
            }
        )

    except Exception:

        return []


# =========================================================
# /PING
# =========================================================

@bot.tree.command(
    name="ping",
    description="Kiểm tra bot",
    guild=discord.Object(
        id=GUILD_ID
    )
)
async def ping(
    interaction: discord.Interaction
):

    await interaction.response.send_message(
        f"🏓 Pong! "
        f"`{round(bot.latency * 1000)}ms`"
    )


# =========================================================
# /APIQUOTA
# =========================================================

@bot.tree.command(
    name="apiquota",
    description="Xem quota API-Football",
    guild=discord.Object(
        id=GUILD_ID
    )
)
async def apiquota(
    interaction: discord.Interaction
):

    await interaction.response.defer(
        ephemeral=True
    )

    try:

        await football.get(
            "/status",
            cache=False
        )

        remaining = (
            football.daily_remaining
        )

        limit = (
            football.daily_limit
        )

        if remaining is None:

            text = (
                "⚠️ Không đọc được quota "
                "từ API."
            )

        else:

            text = (
                "📊 **API-Football**\n\n"
                f"🟢 Còn: `{remaining}`\n"
                f"📦 Limit: `{limit or '?'}`"
            )

        await interaction.edit_original_response(
            content=text
        )

    except Exception as e:

        await interaction.edit_original_response(
            content=(
                "❌ "
                + shorten(
                    e,
                    800
                )
            )
        )


# =========================================================
# /SOI
# =========================================================

@bot.tree.command(
    name="soi",
    description="Soi bóng đá",
    guild=discord.Object(
        id=GUILD_ID
    )
)
async def soi(
    interaction: discord.Interaction
):

    await interaction.response.send_message(
        "⚽ **SOI BÓNG ĐÁ**\n\n"
        "Bấm **🔎 Tìm giải** để bắt đầu.",
        view=LeagueHomeView(),
        ephemeral=True
    )


# =========================================================
# LEAGUE HOME VIEW
# =========================================================

class LeagueHomeView(
    discord.ui.View
):

    def __init__(self):

        super().__init__(
            timeout=180
        )

    @discord.ui.button(
        label="🔎 Tìm giải",
        style=discord.ButtonStyle.primary
    )
    async def search_button(
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

class LeagueSearchModal(
    discord.ui.Modal
):

    def __init__(self):

        super().__init__(
            title="Tìm giải bóng đá"
        )

        self.keyword = (
            discord.ui.TextInput(
                label="Tên giải",
                placeholder=(
                    "VD: Champions League"
                ),
                required=True,
                max_length=80
            )
        )

        self.add_item(
            self.keyword
        )

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
                    "Thử `C1`, `Premier League`, "
                    "`La Liga`, `Serie A`..."
                )
            )

            return

        await interaction.edit_original_response(
            content="👇 Chọn giải:",
            view=LeagueResultsView(
                results
            )
        )


# =========================================================
# LEAGUE RESULTS VIEW
# =========================================================

class LeagueResultsView(
    discord.ui.View
):

    def __init__(
        self,
        leagues
    ):

        super().__init__(
            timeout=180
        )

        self.add_item(
            LeagueSelect(
                leagues
            )
        )


class LeagueSelect(
    discord.ui.Select
):

    def __init__(
        self,
        leagues
    ):

        options = []

        for item in leagues[:25]:

            league = item.get(
                "league",
                {}
            )

            league_id = league.get(
                "id"
            )

            name = league.get(
                "name",
                "Unknown"
            )

            country = item.get(
                "country",
                {}
            ).get(
                "name",
                ""
            )

            options.append(
                discord.SelectOption(
                    label=shorten(
                        name,
                        100
                    ),
                    description=shorten(
                        f"{country} • ID {league_id}",
                        100
                    ),
                    value=str(
                        league_id
                    )
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

        # ACK ngay lập tức
        await interaction.response.defer(
            ephemeral=True
        )

        league_id = int(
            self.values[0]
        )

        selected = None

        for item in self.leagues:

            league = item.get(
                "league",
                {}
            )

            if league.get(
                "id"
            ) == league_id:

                selected = item
                break

        if selected is None:

            await interaction.edit_original_response(
                content=(
                    "❌ Không tìm thấy giải."
                )
            )

            return

        league = selected.get(
            "league",
            {}
        )

        name = league.get(
            "name",
            "League"
        )

        logo = league.get(
            "logo"
        )

        embed = discord.Embed(
            title=f"⚽ {name}",
            description=(
                "Chọn **📅 Chọn ngày**.\n\n"
                "Nhập ngày dạng:\n"
                "`YYYY-MM-DD`\n\n"
                "Ví dụ:\n"
                "`2026-09-27`"
            ),
            color=discord.Color.blue()
        )

        if logo:
            embed.set_thumbnail(
                url=logo
            )

        await interaction.edit_original_response(
            content=None,
            embed=embed,
            view=LeagueDateView(
                league_id,
                name,
                logo
            )
        )


# =========================================================
# DATE VIEW
# =========================================================

class LeagueDateView(
    discord.ui.View
):

    def __init__(
        self,
        league_id,
        league_name,
        league_logo
    ):

        super().__init__(
            timeout=300
        )

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

class DateModal(
    discord.ui.Modal
):

    def __init__(
        self,
        league_id,
        league_name,
        league_logo
    ):

        super().__init__(
            title="Chọn ngày"
        )

        self.league_id = league_id
        self.league_name = league_name
        self.league_logo = league_logo

        self.date_input = (
            discord.ui.TextInput(
                label="Ngày",
                placeholder="2026-09-27",
                required=True,
                max_length=10
            )
        )

        self.add_item(
            self.date_input
        )

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        date_string = (
            self.date_input.value
            .strip()
        )

        try:

            datetime.strptime(
                date_string,
                "%Y-%m-%d"
            )

        except ValueError:

            await interaction.response.send_message(
                "❌ Sai định dạng.\n"
                "Dùng `YYYY-MM-DD`.",
                ephemeral=True
            )

            return

        # ACK trước API
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
# SHOW FIXTURES
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
                "timezone":
                    "Asia/Ho_Chi_Minh"
            }
        )

    except APIFreeSeasonError:

        await interaction.edit_original_response(
            content=(
                "⚠️ **API-Football Free bị giới hạn season**\n\n"
                f"🏆 Giải: **{league_name}**\n"
                f"📅 Ngày: `{date_string}`\n"
                f"📦 Season API: `{season}`\n\n"
                "Key Free của m không được phép "
                "lấy season này.\n\n"
                "❌ Bot không giả lịch trận."
            ),
            embed=None,
            view=None
        )

        return

    except APIError as e:

        await interaction.edit_original_response(
            content=(
                "❌ **API-Football lỗi**\n\n"
                f"`{shorten(e, 1000)}`"
            ),
            embed=None,
            view=None
        )

        return

    except Exception as e:

        await interaction.edit_original_response(
            content=(
                "❌ **Lỗi bot**\n\n"
                f"`{shorten(e, 1000)}`"
            ),
            embed=None,
            view=None
        )

        return

    if not fixtures:

        await interaction.edit_original_response(
            content=(
                f"📅 `{date_string}`\n\n"
                "Không có trận nào API trả về."
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
            f"🎯 Trận: `{len(fixtures)}`"
        ),
        color=discord.Color.blue()
    )

    if league_logo:

        embed.set_thumbnail(
            url=league_logo
        )

    for fixture in fixtures:

        home, away = (
            get_fixture_teams(
                fixture
            )
        )

        fixture_info = fixture.get(
            "fixture",
            {}
        )

        home_name = home.get(
            "name",
            "Home"
        )

        away_name = away.get(
            "name",
            "Away"
        )

        match_time = format_match_time(
            fixture_info.get(
                "date",
                ""
            )
        )

        status = fixture_info.get(
            "status",
            {}
        ).get(
            "short",
            "?"
        )

        embed.add_field(
            name=(
                f"{home_name} vs {away_name}"
            ),
            value=(
                f"🕐 {match_time}\n"
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

class FixtureListView(
    discord.ui.View
):

    def __init__(
        self,
        fixtures,
        league_name
    ):

        super().__init__(
            timeout=300
        )

        self.add_item(
            FixtureSelect(
                fixtures,
                league_name
            )
        )


class FixtureSelect(
    discord.ui.Select
):

    def __init__(
        self,
        fixtures,
        league_name
    ):

        options = []

        for fixture in fixtures[:25]:

            fixture_id = (
                fixture
                .get("fixture", {})
                .get("id")
            )

            home, away = (
                get_fixture_teams(
                    fixture
                )
            )

            home_name = home.get(
                "name",
                "Home"
            )

            away_name = away.get(
                "name",
                "Away"
            )

            match_time = format_match_time(
                fixture
                .get("fixture", {})
                .get("date", "")
            )

            options.append(
                discord.SelectOption(
                    label=shorten(
                        f"{home_name} vs {away_name}",
                        100
                    ),
                    description=shorten(
                        match_time,
                        100
                    ),
                    value=str(
                        fixture_id
                    )
                )
            )

        super().__init__(
            placeholder="⚽ Chọn trận...",
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

        selected = None

        for fixture in self.fixtures:

            if (
                fixture
                .get("fixture", {})
                .get("id")
                == fixture_id
            ):

                selected = fixture
                break

        if selected is None:

            await interaction.edit_original_response(
                content=(
                    "❌ Không tìm thấy trận."
                )
            )

            return

        await show_analysis(
            interaction,
            selected,
            self.league_name
        )


# =========================================================
# ANALYSIS
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

    home, away = (
        get_fixture_teams(
            fixture
        )
    )

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

    fixture_id = fixture_info.get(
        "id"
    )

    embed = discord.Embed(
        title="🔎 SOI TRẬN",
        description=(
            f"🏆 **{league_name}**\n\n"
            f"⚽ **{home_name}** "
            f"vs "
            f"**{away_name}**\n\n"
            f"🕐 "
            f"{format_match_time(
                fixture_info.get(
                    'date',
                    ''
                )
            )}"
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
                f"**{home_score} - "
                f"{away_score}**"
            ),
            inline=False
        )

    # -----------------------------------------------------
    # PREDICTION
    # -----------------------------------------------------

    prediction_text = (
        "⚠️ Chưa lấy được prediction."
    )

    try:

        predictions = await football.get(
            "/predictions",
            {
                "fixture": fixture_id
            }
        )

        if predictions:

            prediction = predictions[0].get(
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

            hp = percent.get(
                "home"
            )

            dp = percent.get(
                "draw"
            )

            ap = percent.get(
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
                hp
                or dp
                or ap
            ):

                lines.append(
                    f"🏠 {home_name}: `{hp or '?'}%`"
                )

                lines.append(
                    f"🤝 Hòa: `{dp or '?'}%`"
                )

                lines.append(
                    f"✈️ {away_name}: `{ap or '?'}%`"
                )

            if lines:

                prediction_text = (
                    "\n".join(lines)
                )

    except APIFreeSeasonError:

        prediction_text = (
            "⚠️ Prediction của season này "
            "bị giới hạn trên API Free."
        )

    except Exception:

        prediction_text = (
            "⚠️ Không lấy được prediction."
        )

    embed.add_field(
        name="🧠 Phân tích",
        value=prediction_text,
        inline=False
    )

    # -----------------------------------------------------
    # FORM
    # -----------------------------------------------------

    form_text = (
        "Không có dữ liệu form."
    )

    try:

        predictions = await football.get(
            "/predictions",
            {
                "fixture": fixture_id
            }
        )

        if predictions:

            team_data = predictions[0].get(
                "teams",
                {}
            )

            hp = (
                team_data
                .get("home", {})
                .get("league", {})
                .get("form")
            )

            ap = (
                team_data
                .get("away", {})
                .get("league", {})
                .get("form")
            )

            lines = []

            if hp:

                lines.append(
                    f"🏠 {home_name}: `{hp}`"
                )

            if ap:

                lines.append(
                    f"✈️ {away_name}: `{ap}`"
                )

            if lines:

                form_text = (
                    "\n".join(lines)
                )

    except Exception:

        pass

    embed.add_field(
        name="📈 Phong độ",
        value=form_text,
        inline=False
    )

    # -----------------------------------------------------
    # IDS
    # -----------------------------------------------------

    embed.add_field(
        name="🆔 Fixture",
        value=f"`{fixture_id}`",
        inline=True
    )

    if home.get("id"):

        embed.add_field(
            name="🏠 Home ID",
            value=f"`{home['id']}`",
            inline=True
        )

    if away.get("id"):

        embed.add_field(
            name="✈️ Away ID",
            value=f"`{away['id']}`",
            inline=True
        )

    embed.set_footer(
        text=(
            "API-Football • "
            "Dữ liệu bóng đá"
        )
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
# WALLET COMMANDS
# =========================================================

@bot.tree.command(
    name="khoinghiep",
    description="Nhận vốn khởi nghiệp 10.000 xu",
    guild=discord.Object(
        id=GUILD_ID
    )
)
async def khoinghiep(
    interaction: discord.Interaction
):

    balance = get_balance(
        interaction.user.id
    )

    await interaction.response.send_message(
        (
            "💰 Ví của m hiện có "
            f"**{balance:,} xu**."
        ),
        ephemeral=True
    )


@bot.tree.command(
    name="vi",
    description="Xem số dư",
    guild=discord.Object(
        id=GUILD_ID
    )
)
async def vi(
    interaction: discord.Interaction
):

    balance = get_balance(
        interaction.user.id
    )

    await interaction.response.send_message(
        (
            f"💰 **Ví của "
            f"{interaction.user.display_name}**\n\n"
            f"💵 Số dư: **{balance:,} xu**"
        ),
        ephemeral=True
    )


# =========================================================
# BET VIEW
# =========================================================

class BetMatchView(
    discord.ui.View
):

    def __init__(
        self,
        fixture_id,
        home_name,
        away_name
    ):

        super().__init__(
            timeout=300
        )

        self.fixture_id = fixture_id
        self.home_name = home_name
        self.away_name = away_name

    @discord.ui.button(
        label="🏠 Cược Home",
        style=discord.ButtonStyle.primary
    )
    async def home(
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
    async def draw(
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
    async def away(
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

class BetModal(
    discord.ui.Modal
):

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

        self.amount = (
            discord.ui.TextInput(
                label="Số xu",
                placeholder="VD: 100",
                required=True,
                max_length=10
            )
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

        user_id = (
            interaction.user.id
        )

        balance = get_balance(
            user_id
        )

        if amount > balance:

            await interaction.response.send_message(
                (
                    "❌ Không đủ xu.\n"
                    f"💰 Ví: **{balance:,} xu**"
                ),
                ephemeral=True
            )

            return

        odds = {
            "home": 2.0,
            "draw": 3.0,
            "away": 2.0
        }.get(
            self.choice,
            2.0
        )

        new_balance = (
            balance - amount
        )

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
            "home":
                self.home_name,

            "draw":
                "Hòa",

            "away":
                self.away_name
        }.get(
            self.choice,
            self.choice
        )

        await interaction.response.send_message(
            (
                "✅ **Đặt cược thành công**\n\n"
                f"⚽ {self.home_name} vs "
                f"{self.away_name}\n"
                f"🎯 Chọn: **{choice_name}**\n"
                f"💰 Cược: **{amount:,} xu**\n"
                f"📈 Hệ số: **x{odds}**\n\n"
                f"💵 Còn lại: "
                f"**{new_balance:,} xu**"
            ),
            ephemeral=True
        )


# =========================================================
# /CUOC
# =========================================================

@bot.tree.command(
    name="cuoc",
    description="Xem cược của m",
    guild=discord.Object(
        id=GUILD_ID
    )
)
async def cuoc(
    interaction: discord.Interaction
):

    rows = db.execute(
        """
        SELECT *
        FROM bets
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 10
        """,
        (
            interaction.user.id,
        )
    ).fetchall()

    if not rows:

        await interaction.response.send_message(
            "📭 M chưa có cược nào.",
            ephemeral=True
        )

        return

    lines = []

    for row in rows:

        if row["status"] == "pending":
            icon = "⏳"

        elif row["status"] == "won":
            icon = "✅"

        elif row["status"] == "lost":
            icon = "❌"

        else:
            icon = "↔️"

        lines.append(
            (
                f"{icon} `#{row['id']}` "
                f"• Fixture `{row['fixture_id']}` "
                f"• `{row['choice']}` "
                f"• {row['amount']:,} xu"
            )
        )

    await interaction.response.send_message(
        "🎟️ **CƯỢC CỦA M**\n\n"
        + "\n".join(lines),
        ephemeral=True
    )


# =========================================================
# /KETTOAN
# =========================================================

@bot.tree.command(
    name="kettoan",
    description="Xem thống kê cược",
    guild=discord.Object(
        id=GUILD_ID
    )
)
async def kettoan(
    interaction: discord.Interaction
):

    row = db.execute(
        """
        SELECT
            COUNT(*) AS total,
            COALESCE(
                SUM(amount),
                0
            ) AS total_amount
        FROM bets
        WHERE user_id = ?
        """,
        (
            interaction.user.id,
        )
    ).fetchone()

    balance = get_balance(
        interaction.user.id
    )

    await interaction.response.send_message(
        (
            "📊 **THỐNG KÊ**\n\n"
            f"🎟️ Tổng cược: "
            f"**{row['total']}**\n"
            f"💸 Tổng xu đã cược: "
            f"**{row['total_amount']:,}**\n"
            f"💰 Số dư: "
            f"**{balance:,} xu**"
        ),
        ephemeral=True
    )


# =========================================================
# HEALTH SERVER
# =========================================================

async def health(
    request
):

    return web.json_response(
        {
            "status": "ok",
            "bot": (
                str(bot.user)
                if bot.user
                else None
            ),
            "time":
                now_vn().isoformat()
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
        f"[WEB] Health server: {port}"
    )


# =========================================================
# BOT SETUP
# =========================================================

@bot.event
async def on_ready():

    print(
        "================================"
    )

    print(
        f"[BOT] ONLINE: {bot.user}"
    )

    print(
        f"[BOT] GUILD ID: {GUILD_ID}"
    )

    print(
        "================================"
    )


# =========================================================
# SETUP HOOK
# =========================================================

@bot.event
async def setup_hook():

    guild = discord.Object(
        id=GUILD_ID
    )

    try:

        synced = await bot.tree.sync(
            guild=guild
        )

        print(
            f"[SYNC] Đã sync "
            f"{len(synced)} slash command(s)."
        )

        for command in synced:

            print(
                f"[SYNC] /{command.name}"
            )

    except Exception as e:

        print(
            f"[SYNC ERROR] {repr(e)}"
        )


# =========================================================
# MAIN
# =========================================================

async def main():

    await start_health_server()

    await football.start()

    try:

        print(
            "[BOT] Đang đăng nhập Discord..."
        )

        await bot.start(
            DISCORD_TOKEN
        )

    finally:

        await football.close()


if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        print(
            "[BOT] Đã dừng."
        )

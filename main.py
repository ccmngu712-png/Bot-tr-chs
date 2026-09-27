import os
import asyncio
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web

import discord
from discord import app_commands
from discord.ext import commands


# =========================================================
# CONFIG
# =========================================================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY")

# Guild của server Discord
GUILD_ID = int(os.getenv("GUILD_ID", "1551547049600618538"))

# Kênh #bóng-đá⚽
STARTUP_CHANNEL_ID = int(
    os.getenv("STARTUP_CHANNEL_ID", "1553673308971797133042")
)

TIMEZONE = "Asia/Ho_Chi_Minh"
TZ = ZoneInfo(TIMEZONE)

API_BASE = "https://v3.football.api-sports.io"

# Railway / local database
if os.path.isdir("/data"):
    DB_PATH = "/data/football_bot.db"
else:
    DB_PATH = "football_bot.db"


# =========================================================
# CHECK ENV
# =========================================================

if not DISCORD_TOKEN:
    raise RuntimeError("Thiếu DISCORD_TOKEN trong Railway Variables.")

if not FOOTBALL_API_KEY:
    raise RuntimeError("Thiếu FOOTBALL_API_KEY trong Railway Variables.")


# =========================================================
# DISCORD
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

def db_connect():
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = db_connect()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            balance INTEGER NOT NULL DEFAULT 10000
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            fixture_id INTEGER NOT NULL,
            pick TEXT NOT NULL,
            amount INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            payout INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()


# =========================================================
# API-FOOTBALL CLIENT
# =========================================================

class FootballAPIError(Exception):
    pass


class FootballAPI:

    def __init__(self):
        self.session = None

        # cache:
        # key -> (expire_timestamp, data)
        self.cache = {}

        self.last_remaining = None
        self.last_limit = None

        self.lock = asyncio.Lock()

    async def start(self):
        timeout = aiohttp.ClientTimeout(total=15)

        self.session = aiohttp.ClientSession(
            headers={
                "x-apisports-key": FOOTBALL_API_KEY,
                "Accept": "application/json",
            },
            timeout=timeout
        )

    async def close(self):
        if self.session:
            await self.session.close()

    def _cache_get(self, key):
        item = self.cache.get(key)

        if not item:
            return None

        expire, data = item

        if asyncio.get_running_loop().time() >= expire:
            self.cache.pop(key, None)
            return None

        return data

    def _cache_set(self, key, data, ttl):
        self.cache[key] = (
            asyncio.get_running_loop().time() + ttl,
            data
        )

    async def get(self, endpoint, params=None, ttl=0):

        if params is None:
            params = {}

        cache_key = (
            endpoint,
            tuple(sorted((str(k), str(v)) for k, v in params.items()))
        )

        if ttl > 0:
            cached = self._cache_get(cache_key)

            if cached is not None:
                return cached

        if not self.session:
            raise FootballAPIError("API session chưa được khởi động.")

        async with self.lock:

            # kiểm tra cache lại sau khi chờ lock
            if ttl > 0:
                cached = self._cache_get(cache_key)

                if cached is not None:
                    return cached

            url = API_BASE + endpoint

            for attempt in range(2):

                try:
                    async with self.session.get(
                        url,
                        params=params
                    ) as response:

                        remaining = response.headers.get(
                            "x-ratelimit-requests-remaining"
                        )

                        limit = response.headers.get(
                            "x-ratelimit-requests-limit"
                        )

                        if remaining is not None:
                            self.last_remaining = remaining

                        if limit is not None:
                            self.last_limit = limit

                        data = await response.json(
                            content_type=None
                        )

                        # API lỗi
                        errors = data.get("errors")

                        if errors:
                            if isinstance(errors, dict):
                                msg = " | ".join(
                                    f"{k}: {v}"
                                    for k, v in errors.items()
                                )
                            else:
                                msg = str(errors)

                            raise FootballAPIError(msg)

                        # rate limit
                        if response.status == 429:
                            raise FootballAPIError(
                                "API-Football đang hết quota / bị giới hạn."
                            )

                        # server error
                        if response.status >= 500:

                            if attempt == 0:
                                await asyncio.sleep(1.5)
                                continue

                            raise FootballAPIError(
                                f"API server lỗi HTTP {response.status}"
                            )

                        if response.status != 200:
                            raise FootballAPIError(
                                f"API HTTP {response.status}"
                            )

                        if ttl > 0:
                            self._cache_set(
                                cache_key,
                                data,
                                ttl
                            )

                        return data

                except aiohttp.ClientError as e:

                    if attempt == 0:
                        await asyncio.sleep(1)
                        continue

                    raise FootballAPIError(
                        f"Lỗi kết nối API: {e}"
                    )

        raise FootballAPIError("Không lấy được dữ liệu API.")

    # -----------------------------------------------------
    # LEAGUES SEARCH
    # -----------------------------------------------------

    async def search_leagues(self, query):

        return await self.get(
            "/leagues",
            {
                "search": query
            },
            ttl=86400
        )

    # -----------------------------------------------------
    # LEAGUE DETAIL
    # -----------------------------------------------------

    async def get_league(self, league_id):

        return await self.get(
            "/leagues",
            {
                "id": league_id
            },
            ttl=86400
        )

    # -----------------------------------------------------
    # FIXTURES
    # -----------------------------------------------------

    async def get_fixtures(
        self,
        league_id,
        season,
        date
    ):

        return await self.get(
            "/fixtures",
            {
                "league": league_id,
                "season": season,
                "date": date,
                "timezone": TIMEZONE
            },
            ttl=300
        )

    # -----------------------------------------------------
    # PREDICTION
    # -----------------------------------------------------

    async def get_prediction(self, fixture_id):

        return await self.get(
            "/predictions",
            {
                "fixture": fixture_id
            },
            ttl=1800
        )


football = FootballAPI()


# =========================================================
# HELPER
# =========================================================

def now_vn():
    return datetime.now(TZ)


def today_string():
    return now_vn().strftime("%Y-%m-%d")


def format_date(date_obj):
    return date_obj.strftime("%d/%m/%Y")


def safe_text(value, fallback="Không có"):
    if value is None:
        return fallback

    value = str(value).strip()

    return value if value else fallback


def shorten(text, limit=100):
    text = safe_text(text)

    if len(text) <= limit:
        return text

    return text[:limit - 3] + "..."


def parse_date(text):

    try:
        return datetime.strptime(
            text,
            "%Y-%m-%d"
        ).replace(tzinfo=TZ)

    except ValueError:
        return None


# =========================================================
# SEASON FINDER
# =========================================================

async def get_season_for_date(league_id, target_date):

    data = await football.get_league(league_id)

    response = data.get("response", [])

    if not response:
        return target_date.year

    league_data = response[0]

    seasons = league_data.get("seasons", [])

    target = datetime.strptime(
        target_date,
        "%Y-%m-%d"
    ).date()

    matching = []

    for season in seasons:

        year = season.get("year")

        if not year:
            continue

        start = season.get("start")
        end = season.get("end")

        try:
            start_date = (
                datetime.strptime(start, "%Y-%m-%d").date()
                if start else None
            )

            end_date = (
                datetime.strptime(end, "%Y-%m-%d").date()
                if end else None
            )

            if start_date and end_date:
                if start_date <= target <= end_date:
                    matching.append(year)

        except Exception:
            pass

    if matching:
        return max(matching)

    # fallback:
    # lấy mùa gần nhất không vượt quá năm cần tìm
    years = [
        s.get("year")
        for s in seasons
        if isinstance(s.get("year"), int)
        and s.get("year") <= target.year
    ]

    if years:
        return max(years)

    return target.year


# =========================================================
# LEAGUE SEARCH ALIAS
# =========================================================

def normalize_league_search(query):

    q = query.strip().lower()

    aliases = {
        "c1": "champions",
        "ucl": "champions",
        "champions league": "champions",
        "c2": "europa",
        "uel": "europa",
        "c3": "conference",
        "epl": "premier",
        "ngoại hạng": "premier",
        "ngoai hang": "premier",
        "la liga": "la liga",
        "serie a": "serie a",
        "bundesliga": "bundesliga",
        "ligue 1": "ligue 1",
    }

    return aliases.get(q, q)


# =========================================================
# MAIN SOI VIEW
# =========================================================

class LeagueHomeView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=300)

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
# SEARCH MODAL
# =========================================================

class LeagueSearchModal(discord.ui.Modal):

    def __init__(self):
        super().__init__(
            title="🔎 Tìm giải bóng đá"
        )

        self.query = discord.ui.TextInput(
            label="Tên giải",
            placeholder="Ví dụ: Champions League, Premier League...",
            required=True,
            min_length=2,
            max_length=50
        )

        self.add_item(self.query)

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        await interaction.response.defer()

        query = normalize_league_search(
            str(self.query.value)
        )

        if len(query) < 3:

            await interaction.followup.send(
                "❌ API yêu cầu ít nhất 3 ký tự.\n"
                "Ví dụ: `champions`, `premier`, `bundesliga`.",
                ephemeral=True
            )

            return

        try:

            data = await football.search_leagues(query)

            leagues = data.get("response", [])

            if not leagues:

                await interaction.followup.send(
                    f"❌ Không tìm thấy giải nào với `{query}`.",
                    ephemeral=True
                )

                return

            # bỏ trùng ID
            unique = {}

            for item in leagues:

                league = item.get("league", {})
                country = item.get("country", {})

                league_id = league.get("id")

                if not league_id:
                    continue

                unique[league_id] = {
                    "id": league_id,
                    "name": league.get(
                        "name",
                        "Không tên"
                    ),
                    "logo": league.get(
                        "logo"
                    ),
                    "country": country.get(
                        "name",
                        "Không rõ"
                    )
                }

            results = list(unique.values())[:25]

            view = LeagueResultsView(results)

            embed = discord.Embed(
                title="🔎 Kết quả tìm giải",
                description=(
                    f"Tìm kiếm: **{query}**\n\n"
                    "Bấm vào giải muốn xem lịch."
                )
            )

            embed.set_footer(
                text=f"Tìm thấy {len(unique)} giải • hiển thị tối đa 25"
            )

            await interaction.followup.send(
                embed=embed,
                view=view
            )

        except Exception as e:

            await interaction.followup.send(
                f"❌ Không tìm được giải.\n```{shorten(e, 800)}```",
                ephemeral=True
            )


# =========================================================
# LEAGUE RESULTS
# =========================================================

class LeagueResultsView(discord.ui.View):

    def __init__(self, leagues):
        super().__init__(timeout=300)

        self.leagues = {
            str(x["id"]): x
            for x in leagues
        }

        options = []

        for league in leagues:

            name = shorten(
                league["name"],
                90
            )

            country = shorten(
                league["country"],
                80
            )

            options.append(
                discord.SelectOption(
                    label=name,
                    description=country,
                    value=str(league["id"])
                )
            )

        self.add_item(
            LeagueSelect(options, self.leagues)
        )


class LeagueSelect(discord.ui.Select):

    def __init__(self, options, leagues):

        super().__init__(
            placeholder="🏆 Chọn giải đấu...",
            min_values=1,
            max_values=1,
            options=options
        )

        self.leagues = leagues

    async def callback(
        self,
        interaction: discord.Interaction
    ):

        league_id = int(self.values[0])

        league = self.leagues[league_id]

        view = LeagueDateView(
            league=league,
            selected_date=today_string()
        )

        await interaction.response.edit_message(
            embed=build_league_embed(
                league,
                today_string()
            ),
            view=view
        )


# =========================================================
# LEAGUE EMBED
# =========================================================

def build_league_embed(
    league,
    selected_date
):

    embed = discord.Embed(
        title=f"🏆 {league['name']}",
        description=(
            f"🌍 {league['country']}\n"
            f"🆔 League ID: `{league['id']}`\n\n"
            f"📅 Ngày: **{format_date(parse_date(selected_date))}**\n\n"
            "Chọn ngày để xem các đội đá trong giải."
        )
    )

    if league.get("logo"):
        embed.set_thumbnail(
            url=league["logo"]
        )

    return embed


# =========================================================
# DATE VIEW
# =========================================================

class LeagueDateView(discord.ui.View):

    def __init__(
        self,
        league,
        selected_date
    ):

        super().__init__(timeout=600)

        self.league = league
        self.selected_date = selected_date

    async def show_fixtures(
        self,
        interaction,
        target_date
    ):

        await interaction.response.defer()

        try:

            season = await get_season_for_date(
                self.league["id"],
                target_date
            )

            data = await football.get_fixtures(
                self.league["id"],
                season,
                target_date
            )

            fixtures = data.get(
                "response",
                []
            )

            fixtures.sort(
                key=lambda x: x.get(
                    "fixture",
                    {}
                ).get(
                    "timestamp",
                    0
                )
            )

            view = FixtureListView(
                league=self.league,
                selected_date=target_date,
                fixtures=fixtures
            )

            embed = build_fixture_list_embed(
                self.league,
                target_date,
                fixtures
            )

            await interaction.followup.send(
                embed=embed,
                view=view
            )

        except Exception as e:

            await interaction.followup.send(
                f"❌ Lỗi lấy lịch:\n```{shorten(e, 1000)}```",
                ephemeral=True
            )

    @discord.ui.button(
        label="📅 Hôm qua",
        style=discord.ButtonStyle.secondary
    )
    async def yesterday(
        self,
        interaction,
        button
    ):

        d = parse_date(self.selected_date)
        d -= timedelta(days=1)

        await self.show_fixtures(
            interaction,
            d.strftime("%Y-%m-%d")
        )

    @discord.ui.button(
        label="📅 Hôm nay",
        style=discord.ButtonStyle.success
    )
    async def today(
        self,
        interaction,
        button
    ):

        await self.show_fixtures(
            interaction,
            today_string()
        )

    @discord.ui.button(
        label="📅 Ngày mai",
        style=discord.ButtonStyle.secondary
    )
    async def tomorrow(
        self,
        interaction,
        button
    ):

        d = parse_date(self.selected_date)
        d += timedelta(days=1)

        await self.show_fixtures(
            interaction,
            d.strftime("%Y-%m-%d")
        )

    @discord.ui.button(
        label="🔎 Chọn ngày",
        style=discord.ButtonStyle.primary
    )
    async def choose_date(
        self,
        interaction,
        button
    ):

        await interaction.response.send_modal(
            DateModal(
                self.league
            )
        )


# =========================================================
# DATE MODAL
# =========================================================

class DateModal(discord.ui.Modal):

    def __init__(self, league):

        super().__init__(
            title="📅 Chọn ngày"
        )

        self.league = league

        self.date_input = discord.ui.TextInput(
            label="Ngày",
            placeholder="YYYY-MM-DD   Ví dụ: 2026-10-01",
            required=True,
            min_length=10,
            max_length=10
        )

        self.add_item(
            self.date_input
        )

    async def on_submit(
        self,
        interaction
    ):

        date_text = str(
            self.date_input.value
        ).strip()

        if not parse_date(date_text):

            await interaction.response.send_message(
                "❌ Ngày không hợp lệ.\n"
                "Nhập dạng `2026-10-01`.",
                ephemeral=True
            )

            return

        view = LeagueDateView(
            self.league,
            date_text
        )

        await interaction.response.send_message(
            embed=build_league_embed(
                self.league,
                date_text
            ),
            view=view
        )

        # tự động lấy trận
        await view.show_fixtures(
            interaction,
            date_text
        )


# =========================================================
# FIXTURE LIST EMBED
# =========================================================

def build_fixture_list_embed(
    league,
    selected_date,
    fixtures
):

    embed = discord.Embed(
        title=f"⚽ {league['name']}",
        description=(
            f"📅 **{format_date(parse_date(selected_date))}**\n"
            f"🌍 {league['country']}\n\n"
        )
    )

    if not fixtures:

        embed.description += (
            "❌ **Không có trận nào trong ngày này.**\n\n"
            "Bấm `Ngày mai` hoặc `Chọn ngày` để xem ngày khác."
        )

        if league.get("logo"):
            embed.set_thumbnail(
                url=league["logo"]
            )

        return embed

    for i, fixture in enumerate(fixtures[:25], start=1):

        f = fixture.get("fixture", {})
        teams = fixture.get("teams", {})
        status = f.get("status", {})

        home = teams.get("home", {})
        away = teams.get("away", {})

        timestamp = f.get("timestamp")

        if timestamp:
            time_text = datetime.fromtimestamp(
                timestamp,
                TZ
            ).strftime("%H:%M")
        else:
            time_text = "--:--"

        status_text = status.get(
            "short",
            "?"
        )

        embed.add_field(
            name=(
                f"{i}. ⚽ "
                f"{home.get('name', '?')} "
                f"vs "
                f"{away.get('name', '?')}"
            ),
            value=(
                f"🕐 {time_text}  •  "
                f"`{status_text}`\n"
                f"🆔 `{f.get('id')}`"
            ),
            inline=False
        )

    embed.set_footer(
        text=(
            f"{len(fixtures)} trận • "
            "Bấm danh sách bên dưới để soi"
        )
    )

    if league.get("logo"):
        embed.set_thumbnail(
            url=league["logo"]
        )

    return embed


# =========================================================
# FIXTURE SELECT
# =========================================================

class FixtureListView(discord.ui.View):

    def __init__(
        self,
        league,
        selected_date,
        fixtures
    ):

        super().__init__(timeout=600)

        self.league = league
        self.selected_date = selected_date
        self.fixtures = fixtures

        options = []

        for fixture in fixtures[:25]:

            f = fixture.get("fixture", {})
            teams = fixture.get("teams", {})

            fixture_id = f.get("id")

            if not fixture_id:
                continue

            home = teams.get("home", {}).get(
                "name",
                "Home"
            )

            away = teams.get("away", {}).get(
                "name",
                "Away"
            )

            timestamp = f.get("timestamp")

            if timestamp:
                time_text = datetime.fromtimestamp(
                    timestamp,
                    TZ
                ).strftime("%H:%M")
            else:
                time_text = "--:--"

            options.append(
                discord.SelectOption(
                    label=shorten(
                        f"{home} vs {away}",
                        100
                    ),
                    description=shorten(
                        f"{time_text} • ID {fixture_id}",
                        100
                    ),
                    value=str(fixture_id)
                )
            )

        if options:
            self.add_item(
                FixtureSelect(
                    options,
                    fixtures
                )
            )


class FixtureSelect(discord.ui.Select):

    def __init__(
        self,
        options,
        fixtures
    ):

        super().__init__(
            placeholder="⚽ Chọn trận muốn soi...",
            min_values=1,
            max_values=1,
            options=options
        )

        self.fixtures = {
            str(
                x.get("fixture", {}).get("id")
            ): x
            for x in fixtures
        }

    async def callback(
        self,
        interaction
    ):

        fixture_id = self.values[0]

        fixture = self.fixtures.get(
            fixture_id
        )

        if not fixture:

            await interaction.response.send_message(
                "❌ Không tìm thấy trận.",
                ephemeral=True
            )

            return

        await interaction.response.defer()

        try:

            embed = await build_match_analysis(
                fixture
            )

            await interaction.followup.send(
                embed=embed
            )

        except Exception as e:

            await interaction.followup.send(
                f"❌ Không thể soi trận:\n```{shorten(e, 1200)}```",
                ephemeral=True
            )


# =========================================================
# MATCH ANALYSIS
# =========================================================

async def build_match_analysis(
    fixture
):

    f = fixture.get("fixture", {})
    league = fixture.get("league", {})
    teams = fixture.get("teams", {})
    goals = fixture.get("goals", {})

    home = teams.get("home", {})
    away = teams.get("away", {})

    fixture_id = f.get("id")

    home_name = home.get(
        "name",
        "Đội nhà"
    )

    away_name = away.get(
        "name",
        "Đội khách"
    )

    kickoff = f.get("timestamp")

    if kickoff:
        kickoff_text = datetime.fromtimestamp(
            kickoff,
            TZ
        ).strftime(
            "%d/%m/%Y %H:%M"
        )
    else:
        kickoff_text = "Không rõ"

    status = f.get(
        "status",
        {}
    )

    status_short = status.get(
        "short",
        "?"
    )

    embed = discord.Embed(
        title=f"🔎 {home_name} vs {away_name}",
        description=(
            f"🏆 **{league.get('name', 'Không rõ')}**\n"
            f"📅 {kickoff_text}\n"
            f"📊 Trạng thái: `{status_short}`\n"
            f"🆔 Fixture ID: `{fixture_id}`"
        )
    )

    # logo đội nhà
    if home.get("logo"):
        embed.set_thumbnail(
            url=home["logo"]
        )

    # logo links
    home_logo = home.get("logo")
    away_logo = away.get("logo")

    embed.add_field(
        name=f"🏠 {home_name}",
        value=(
            f"[🖼️ Logo]({home_logo})"
            if home_logo
            else "Không có logo"
        ),
        inline=True
    )

    embed.add_field(
        name=f"✈️ {away_name}",
        value=(
            f"[🖼️ Logo]({away_logo})"
            if away_logo
            else "Không có logo"
        ),
        inline=True
    )

    # -----------------------------------------------------
    # PREDICTION
    # -----------------------------------------------------

    prediction_data = None

    try:

        pred_response = await football.get_prediction(
            fixture_id
        )

        pred_list = pred_response.get(
            "response",
            []
        )

        if pred_list:
            prediction_data = pred_list[0]

    except Exception:
        prediction_data = None

    if prediction_data:

        predictions = prediction_data.get(
            "predictions",
            {}
        )

        winner = predictions.get(
            "winner",
            {}
        )

        winner_name = winner.get(
            "name"
        )

        advice = predictions.get(
            "advice"
        )

        under_over = predictions.get(
            "under_over"
        )

        predicted_goals = predictions.get(
            "goals",
            {}
        )

        percent = predictions.get(
            "percent",
            {}
        )

        predicted_home = predicted_goals.get(
            "home"
        )

        predicted_away = predicted_goals.get(
            "away"
        )

        prediction_text = ""

        if winner_name:
            prediction_text += (
                f"🏆 **Winner model:** {winner_name}\n"
            )

        if advice:
            prediction_text += (
                f"💡 **Advice:** {advice}\n"
            )

        if predicted_home is not None or predicted_away is not None:
            prediction_text += (
                f"🎯 **Tỷ số model:** "
                f"{safe_text(predicted_home, '?')} - "
                f"{safe_text(predicted_away, '?')}\n"
            )

        if under_over:
            prediction_text += (
                f"⚽ **T/X:** {under_over}\n"
            )

        if percent:

            prediction_text += (
                "\n**Xác suất model:**\n"
                f"🏠 Home: {safe_text(percent.get('home'), '?')}\n"
                f"🤝 Draw: {safe_text(percent.get('draw'), '?')}\n"
                f"✈️ Away: {safe_text(percent.get('away'), '?')}"
            )

        embed.add_field(
            name="📊 Dữ liệu dự đoán API-Football",
            value=prediction_text[:1024] or "Không có dữ liệu.",
            inline=False
        )

        # -------------------------------------------------
        # FORM
        # -------------------------------------------------

        home_prediction = prediction_data.get(
            "teams",
            {}
        ).get(
            "home",
            {}
        )

        away_prediction = prediction_data.get(
            "teams",
            {}
        ).get(
            "away",
            {}
        )

        home_last5 = home_prediction.get(
            "last_5",
            {}
        )

        away_last5 = away_prediction.get(
            "last_5",
            {}
        )

        home_form = home_last5.get(
            "form"
        )

        away_form = away_last5.get(
            "form"
        )

        form_text = (
            f"🏠 **{home_name}:** "
            f"`{safe_text(home_form, 'N/A')}`\n"
            f"✈️ **{away_name}:** "
            f"`{safe_text(away_form, 'N/A')}`"
        )

        embed.add_field(
            name="📈 Form gần đây",
            value=form_text,
            inline=False
        )

        # -------------------------------------------------
        # H2H
        # -------------------------------------------------

        h2h = prediction_data.get(
            "h2h",
            []
        )

        if h2h:

            h2h_lines = []

            for item in h2h[:5]:

                h2h_teams = item.get(
                    "teams",
                    {}
                )

                h = h2h_teams.get(
                    "home",
                    {}
                ).get(
                    "name",
                    "Home"
                )

                a = h2h_teams.get(
                    "away",
                    {}
                ).get(
                    "name",
                    "Away"
                )

                g = item.get(
                    "goals",
                    {}
                )

                gh = g.get(
                    "home",
                    "-"
                )

                ga = g.get(
                    "away",
                    "-"
                )

                h2h_lines.append(
                    f"• {h} **{gh}-{ga}** {a}"
                )

            embed.add_field(
                name="🤝 5 lần đối đầu gần nhất",
                value="\n".join(h2h_lines)[:1024],
                inline=False
            )

    else:

        embed.add_field(
            name="📊 Dự đoán",
            value=(
                "API chưa có dữ liệu prediction cho trận/giải này."
            ),
            inline=False
        )

    embed.set_footer(
        text=(
            "Dữ liệu lấy trực tiếp từ API-Football • "
            "Prediction là mô hình thống kê, không đảm bảo kết quả."
        )
    )

    return embed


# =========================================================
# /SOI
# =========================================================

@bot.tree.command(
    name="soi",
    description="Tìm giải đấu rồi chọn trận để soi",
    guild=GUILD
)
async def soi(
    interaction: discord.Interaction
):

    embed = discord.Embed(
        title="⚽ SOI BÓNG ĐÁ",
        description=(
            "Muốn tìm giải nào thì bấm nút bên dưới.\n\n"
            "🔎 **Tìm giải**\n"
            "Ví dụ:\n"
            "• `Champions League`\n"
            "• `Premier League`\n"
            "• `La Liga`\n"
            "• `Serie A`\n"
            "• `Bundesliga`\n"
            "• `C1` cũng được\n\n"
            "Sau đó chọn **giải → ngày → trận**."
        )
    )

    embed.set_footer(
        text="API-Football • dữ liệu giải đấu thực"
    )

    await interaction.response.send_message(
        embed=embed,
        view=LeagueHomeView()
    )


# =========================================================
# /PING
# =========================================================

@bot.tree.command(
    name="ping",
    description="Kiểm tra bot",
    guild=GUILD
)
async def ping(
    interaction: discord.Interaction
):

    latency = round(
        bot.latency * 1000
    )

    await interaction.response.send_message(
        f"🏓 Pong!\n"
        f"Discord: `{latency}ms`\n"
        f"API quota còn: `{football.last_remaining or 'chưa gọi API'}`"
    )


# =========================================================
# /APIQUOTA
# =========================================================

@bot.tree.command(
    name="apiquota",
    description="Xem quota API-Football gần nhất",
    guild=GUILD
)
async def apiquota(
    interaction: discord.Interaction
):

    await interaction.response.send_message(
        "📡 API-Football\n"
        f"Requests còn: `{football.last_remaining or 'chưa có dữ liệu'}`\n"
        f"Giới hạn: `{football.last_limit or 'chưa có dữ liệu'}`"
    )


# =========================================================
# /KHOINGHIEP
# =========================================================

@bot.tree.command(
    name="khoinghiep",
    description="Tạo ví tiền ảo",
    guild=GUILD
)
async def khoinghiep(
    interaction: discord.Interaction
):

    user_id = interaction.user.id

    conn = db_connect()
    cur = conn.cursor()

    cur.execute(
        "SELECT balance FROM users WHERE user_id=?",
        (user_id,)
    )

    existing = cur.fetchone()

    if existing:

        balance = existing[0]

        conn.close()

        await interaction.response.send_message(
            f"💰 Bạn đã có tài khoản.\n"
            f"Số dư: **{balance:,}**"
        )

        return

    cur.execute(
        "INSERT INTO users(user_id, balance) VALUES(?, ?)",
        (user_id, 10000)
    )

    conn.commit()
    conn.close()

    await interaction.response.send_message(
        "🎉 Khởi nghiệp thành công!\n"
        "💰 Vốn ban đầu: **10,000** tiền ảo."
    )


# =========================================================
# /VI
# =========================================================

@bot.tree.command(
    name="vi",
    description="Xem ví tiền ảo",
    guild=GUILD
)
async def vi(
    interaction: discord.Interaction
):

    conn = db_connect()
    cur = conn.cursor()

    cur.execute(
        "SELECT balance FROM users WHERE user_id=?",
        (interaction.user.id,)
    )

    row = cur.fetchone()

    conn.close()

    if not row:

        await interaction.response.send_message(
            "❌ Chưa có ví.\n"
            "Dùng `/khoinghiep` trước."
        )

        return

    await interaction.response.send_message(
        f"💰 Ví của bạn: **{row[0]:,}** tiền ảo."
    )


# =========================================================
# /CADO
# =========================================================

@bot.tree.command(
    name="cado",
    description="Cá độ bằng tiền ảo Discord",
    guild=GUILD
)
@app_commands.describe(
    fixture_id="ID trận đấu",
    team="Đội nhà / hòa / đội khách",
    amount="Số tiền ảo muốn cược"
)
@app_commands.choices(
    team=[
        app_commands.Choice(
            name="🏠 Đội nhà",
            value="home"
        ),
        app_commands.Choice(
            name="🤝 Hòa",
            value="draw"
        ),
        app_commands.Choice(
            name="✈️ Đội khách",
            value="away"
        )
    ]
)
async def cado(
    interaction: discord.Interaction,
    fixture_id: int,
    team: app_commands.Choice[str],
    amount: int
):

    if amount <= 0:

        await interaction.response.send_message(
            "❌ Số tiền phải lớn hơn 0."
        )

        return

    if amount > 1_000_000:

        await interaction.response.send_message(
            "❌ Tối đa 1,000,000 tiền ảo/lần."
        )

        return

    await interaction.response.defer()

    # lấy fixture thật
    try:

        data = await football.get(
            "/fixtures",
            {
                "id": fixture_id,
                "timezone": TIMEZONE
            },
            ttl=30
        )

        fixtures = data.get(
            "response",
            []
        )

        if not fixtures:

            await interaction.followup.send(
                "❌ Không tìm thấy fixture ID này."
            )

            return

        fixture = fixtures[0]

    except Exception as e:

        await interaction.followup.send(
            f"❌ Không xác minh được trận:\n```{shorten(e, 800)}```"
        )

        return

    status = fixture.get(
        "fixture",
        {}
    ).get(
        "status",
        {}
    ).get(
        "short"
    )

    if status not in {"NS", "TBD"}:

        await interaction.followup.send(
            f"❌ Trận này không còn nhận cược.\n"
            f"Trạng thái: `{status}`"
        )

        return

    user_id = interaction.user.id

    conn = db_connect()
    cur = conn.cursor()

    cur.execute(
        "SELECT balance FROM users WHERE user_id=?",
        (user_id,)
    )

    row = cur.fetchone()

    if not row:

        conn.close()

        await interaction.followup.send(
            "❌ Dùng `/khoinghiep` trước."
        )

        return

    balance = row[0]

    if amount > balance:

        conn.close()

        await interaction.followup.send(
            f"❌ Không đủ tiền.\n"
            f"Ví hiện tại: **{balance:,}**"
        )

        return

    # Không cho cược trùng trận
    cur.execute(
        """
        SELECT id
        FROM bets
        WHERE user_id=?
        AND fixture_id=?
        AND status='pending'
        """,
        (
            user_id,
            fixture_id
        )
    )

    if cur.fetchone():

        conn.close()

        await interaction.followup.send(
            "❌ Bạn đã có một vé cược đang chờ cho trận này."
        )

        return

    # trừ tiền
    cur.execute(
        """
        UPDATE users
        SET balance = balance - ?
        WHERE user_id=?
        """,
        (
            amount,
            user_id
        )
    )

    cur.execute(
        """
        INSERT INTO bets(
            user_id,
            fixture_id,
            pick,
            amount,
            status,
            created_at
        )
        VALUES (?, ?, ?, ?, 'pending', ?)
        """,
        (
            user_id,
            fixture_id,
            team.value,
            amount,
            datetime.utcnow().isoformat()
        )
    )

    conn.commit()
    conn.close()

    teams = fixture.get(
        "teams",
        {}
    )

    home = teams.get(
        "home",
        {}
    ).get(
        "name",
        "Home"
    )

    away = teams.get(
        "away",
        {}
    ).get(
        "name",
        "Away"
    )

    pick_name = {
        "home": home,
        "draw": "Hòa",
        "away": away
    }[team.value]

    await interaction.followup.send(
        f"🎫 **Đặt cược thành công**\n\n"
        f"⚽ {home} vs {away}\n"
        f"🎯 Chọn: **{pick_name}**\n"
        f"💰 Cược: **{amount:,}**\n"
        f"💵 Còn lại: **{balance - amount:,}**\n\n"
        "💡 Đây chỉ là **tiền ảo Discord**, không phải tiền thật."
    )


# =========================================================
# /CUOC
# =========================================================

@bot.tree.command(
    name="cuoc",
    description="Xem các vé cược đang chờ",
    guild=GUILD
)
async def cuoc(
    interaction: discord.Interaction
):

    conn = db_connect()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT fixture_id, pick, amount, status
        FROM bets
        WHERE user_id=?
        AND status='pending'
        ORDER BY id DESC
        """,
        (interaction.user.id,)
    )

    rows = cur.fetchall()

    conn.close()

    if not rows:

        await interaction.response.send_message(
            "🎫 Bạn không có vé cược đang chờ."
        )

        return

    lines = []

    for fixture_id, pick, amount, status in rows:

        pick_name = {
            "home": "Đội nhà",
            "draw": "Hòa",
            "away": "Đội khách"
        }.get(
            pick,
            pick
        )

        lines.append(
            f"⚽ `{fixture_id}` • "
            f"{pick_name} • "
            f"💰 {amount:,}"
        )

    await interaction.response.send_message(
        "🎫 **Vé cược của bạn**\n\n"
        + "\n".join(lines)
    )


# =========================================================
# SETTLE RESULT
# =========================================================

def get_match_result(fixture):

    teams = fixture.get(
        "teams",
        {}
    )

    goals = fixture.get(
        "goals",
        {}
    )

    home_winner = teams.get(
        "home",
        {}
    ).get(
        "winner"
    )

    away_winner = teams.get(
        "away",
        {}
    ).get(
        "winner"
    )

    if home_winner is True:
        return "home"

    if away_winner is True:
        return "away"

    home_goals = goals.get(
        "home"
    )

    away_goals = goals.get(
        "away"
    )

    if home_goals is None or away_goals is None:
        return None

    if home_goals > away_goals:
        return "home"

    if away_goals > home_goals:
        return "away"

    return "draw"


# =========================================================
# /KETTOAN
# =========================================================

@bot.tree.command(
    name="kettoan",
    description="Admin chốt kết quả trận",
    guild=GUILD
)
@app_commands.describe(
    fixture_id="ID trận cần chốt"
)
async def kettoan(
    interaction: discord.Interaction,
    fixture_id: int
):

    if not interaction.user.guild_permissions.administrator:

        await interaction.response.send_message(
            "❌ Chỉ Admin mới được chốt kèo.",
            ephemeral=True
        )

        return

    await interaction.response.defer()

    try:

        data = await football.get(
            "/fixtures",
            {
                "id": fixture_id,
                "timezone": TIMEZONE
            },
            ttl=0
        )

        fixtures = data.get(
            "response",
            []
        )

        if not fixtures:

            await interaction.followup.send(
                "❌ Không tìm thấy trận."
            )

            return

        fixture = fixtures[0]

    except Exception as e:

        await interaction.followup.send(
            f"❌ API lỗi:\n```{shorten(e, 800)}```"
        )

        return

    status = fixture.get(
        "fixture",
        {}
    ).get(
        "status",
        {}
    ).get(
        "short"
    )

    if status not in {
        "FT",
        "AET",
        "PEN",
        "AWD",
        "WO"
    }:

        await interaction.followup.send(
            f"❌ Trận chưa kết thúc.\n"
            f"Trạng thái hiện tại: `{status}`"
        )

        return

    result = get_match_result(
        fixture
    )

    if result is None:

        await interaction.followup.send(
            "❌ Không xác định được kết quả."
        )

        return

    conn = db_connect()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT id, user_id, pick, amount
        FROM bets
        WHERE fixture_id=?
        AND status='pending'
        """,
        (fixture_id,)
    )

    bets = cur.fetchall()

    if not bets:

        conn.close()

        await interaction.followup.send(
            "ℹ️ Không có vé cược nào đang chờ cho trận này."
        )

        return

    winners = 0
    losers = 0
    total_payout = 0

    for bet_id, user_id, pick, amount in bets:

        if pick == result:

            payout = amount * 2

            cur.execute(
                """
                UPDATE users
                SET balance = balance + ?
                WHERE user_id=?
                """,
                (
                    payout,
                    user_id
                )
            )

            cur.execute(
                """
                UPDATE bets
                SET status='won',
                    payout=?
                WHERE id=?
                """,
                (
                    payout,
                    bet_id
                )
            )

            winners += 1
            total_payout += payout

        else:

            cur.execute(
                """
                UPDATE bets
                SET status='lost',
                    payout=0
                WHERE id=?
                """,
                (bet_id,)
            )

            losers += 1

    conn.commit()
    conn.close()

    teams = fixture.get(
        "teams",
        {}
    )

    home = teams.get(
        "home",
        {}
    ).get(
        "name",
        "Home"
    )

    away = teams.get(
        "away",
        {}
    ).get(
        "name",
        "Away"
    )

    goals = fixture.get(
        "goals",
        {}
    )

    score = (
        f"{goals.get('home', '-')}"
        f" - "
        f"{goals.get('away', '-')}"
    )

    result_name = {
        "home": home,
        "draw": "Hòa",
        "away": away
    }[result]

    await interaction.followup.send(
        f"✅ **Đã kết toán**\n\n"
        f"⚽ {home} vs {away}\n"
        f"🏁 Kết quả: **{result_name}**\n"
        f"🔢 Tỷ số: **{score}**\n\n"
        f"🟢 Vé thắng: **{winners}**\n"
        f"🔴 Vé thua: **{losers}**\n"
        f"💰 Tổng tiền trả: **{total_payout:,}**"
    )


# =========================================================
# ERROR HANDLER
# =========================================================

@bot.tree.error
async def on_app_command_error(
    interaction,
    error
):

    print(
        "APP COMMAND ERROR:",
        repr(error)
    )

    try:

        message = (
            "❌ Bot gặp lỗi khi xử lý lệnh.\n"
            f"```{shorten(error, 1000)}```"
        )

        if interaction.response.is_done():

            await interaction.followup.send(
                message,
                ephemeral=True
            )

        else:

            await interaction.response.send_message(
                message,
                ephemeral=True
            )

    except Exception as e:

        print(
            "ERROR HANDLER FAILED:",
            repr(e)
        )


# =========================================================
# HEALTH SERVER FOR RAILWAY
# =========================================================

async def health(request):

    return web.json_response({
        "status": "online",
        "bot": str(bot.user) if bot.user else None,
        "api_remaining": football.last_remaining
    })


async def start_health_server():

    port = int(
        os.getenv("PORT", "8080")
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

    return runner


# =========================================================
# BOT READY
# =========================================================

startup_sent = False


@bot.event
async def on_ready():

    global startup_sent

    print(
        f"🟢 BOT ONLINE: {bot.user}"
    )

    print(
        f"Guild ID: {GUILD_ID}"
    )

    print(
        f"API remaining: {football.last_remaining}"
    )

    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="/soi • bóng đá ⚽"
        )
    )

    # gửi thông báo online 1 lần mỗi process
    if not startup_sent:

        startup_sent = True

        try:

            channel = bot.get_channel(
                STARTUP_CHANNEL_ID
            )

            if channel:

                await channel.send(
                    "🟢 **Bot bóng đá đã online!**\n"
                    "Dùng `/soi` để tìm giải → chọn ngày → chọn trận."
                )

        except Exception as e:

            print(
                "Không gửi được startup message:",
                repr(e)
            )


# =========================================================
# SETUP
# =========================================================

@bot.event
async def setup_hook():

    init_db()

    await football.start()

    synced = await bot.tree.sync(
        guild=GUILD
    )

    print(
        f"✅ Đã sync {len(synced)} slash commands."
    )


# =========================================================
# SHUTDOWN
# =========================================================

async def shutdown():

    await football.close()


# =========================================================
# MAIN
# =========================================================

async def main():

    health_runner = await start_health_server()

    try:

        async with bot:

            await bot.start(
                DISCORD_TOKEN
            )

    finally:

        await shutdown()

        await health_runner.cleanup()


if __name__ == "__main__":

    asyncio.run(
        main()
    )

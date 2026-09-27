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

GUILD_ID = int(os.getenv("GUILD_ID", "1551547049600618538"))
STARTUP_CHANNEL_ID = int(
    os.getenv("STARTUP_CHANNEL_ID", "1553673308971797133042")
)

TIMEZONE = "Asia/Ho_Chi_Minh"
TZ = ZoneInfo(TIMEZONE)

API_BASE = "https://v3.football.api-sports.io"

DB_PATH = "/data/football_bot.db" if os.path.isdir("/data") else "football_bot.db"


if not DISCORD_TOKEN:
    raise RuntimeError("Thiếu DISCORD_TOKEN")

if not FOOTBALL_API_KEY:
    raise RuntimeError("Thiếu FOOTBALL_API_KEY")


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
# API ERROR
# =========================================================

class FootballAPIError(Exception):
    pass


# =========================================================
# FOOTBALL API
# =========================================================

class FootballAPI:

    def __init__(self):
        self.session = None
        self.cache = {}

        self.last_remaining = None
        self.last_limit = None

    async def start(self):

        timeout = aiohttp.ClientTimeout(total=15)

        self.session = aiohttp.ClientSession(
            headers={
                "x-apisports-key": FOOTBALL_API_KEY,
                "Accept": "application/json"
            },
            timeout=timeout
        )

    async def close(self):

        if self.session:
            await self.session.close()

    def cache_get(self, key):

        item = self.cache.get(key)

        if not item:
            return None

        expires, data = item

        if asyncio.get_running_loop().time() >= expires:
            self.cache.pop(key, None)
            return None

        return data

    def cache_set(self, key, data, ttl):

        self.cache[key] = (
            asyncio.get_running_loop().time() + ttl,
            data
        )

    async def get(
        self,
        endpoint,
        params=None,
        ttl=0
    ):

        if params is None:
            params = {}

        key = (
            endpoint,
            tuple(
                sorted(
                    (str(k), str(v))
                    for k, v in params.items()
                )
            )
        )

        if ttl:
            cached = self.cache_get(key)

            if cached is not None:
                return cached

        if not self.session:
            raise FootballAPIError(
                "API chưa được khởi động."
            )

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

                    if remaining:
                        self.last_remaining = remaining

                    if limit:
                        self.last_limit = limit

                    data = await response.json(
                        content_type=None
                    )

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

                    if response.status == 429:
                        raise FootballAPIError(
                            "API-Football đang hết quota."
                        )

                    if response.status >= 500:

                        if attempt == 0:
                            await asyncio.sleep(1)
                            continue

                        raise FootballAPIError(
                            f"API server lỗi HTTP {response.status}"
                        )

                    if response.status != 200:
                        raise FootballAPIError(
                            f"API HTTP {response.status}"
                        )

                    if ttl:
                        self.cache_set(
                            key,
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

        raise FootballAPIError(
            "Không lấy được dữ liệu API."
        )

    async def search_leagues(self, query):

        return await self.get(
            "/leagues",
            {"search": query},
            ttl=86400
        )

    async def get_league(self, league_id):

        return await self.get(
            "/leagues",
            {"id": league_id},
            ttl=86400
        )

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

    async def get_fixture(self, fixture_id):

        return await self.get(
            "/fixtures",
            {
                "id": fixture_id,
                "timezone": TIMEZONE
            },
            ttl=30
        )

    async def get_prediction(self, fixture_id):

        return await self.get(
            "/predictions",
            {"fixture": fixture_id},
            ttl=1800
        )


football = FootballAPI()


# =========================================================
# HELPERS
# =========================================================

def now_vn():
    return datetime.now(TZ)


def today_string():
    return now_vn().strftime("%Y-%m-%d")


def parse_date(text):

    try:
        return datetime.strptime(
            text,
            "%Y-%m-%d"
        ).replace(tzinfo=TZ)

    except Exception:
        return None


def format_date(date_obj):

    if not date_obj:
        return "Không rõ"

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


# =========================================================
# LEAGUE ALIASES
# =========================================================

def normalize_league_search(query):

    q = query.strip().lower()

    aliases = {
        "c1": "c1",
        "ucl": "c1",
        "champions": "c1",
        "champions league": "c1",
        "uefa champions league": "c1",

        "c2": "europa",
        "uel": "europa",
        "europa league": "europa",

        "c3": "conference",

        "epl": "premier",
        "ngoại hạng": "premier",
        "ngoai hang": "premier",

        "la liga": "la liga",
        "serie a": "serie a",
        "bundesliga": "bundesliga",
        "ligue 1": "ligue 1"
    }

    return aliases.get(q, q)


# =========================================================
# GET SEASON
# =========================================================

async def get_season_for_date(
    league_id,
    target_date
):

    data = await football.get_league(
        league_id
    )

    response = data.get(
        "response",
        []
    )

    if not response:
        return int(target_date[:4])

    seasons = response[0].get(
        "seasons",
        []
    )

    target = datetime.strptime(
        target_date,
        "%Y-%m-%d"
    ).date()

    # Tìm season bao phủ ngày cần xem
    for season in seasons:

        year = season.get("year")
        start = season.get("start")
        end = season.get("end")

        if not isinstance(year, int):
            continue

        try:

            if start and end:

                start_date = datetime.strptime(
                    start,
                    "%Y-%m-%d"
                ).date()

                end_date = datetime.strptime(
                    end,
                    "%Y-%m-%d"
                ).date()

                if start_date <= target <= end_date:
                    return year

        except Exception:
            pass

    # Nếu không có khoảng start/end phù hợp
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
# C1
# =========================================================

async def get_champions_league():

    # UEFA Champions League = ID 2
    try:

        data = await football.get_league(2)

        response = data.get(
            "response",
            []
        )

        if response:

            item = response[0]

            league = item.get(
                "league",
                {}
            )

            country = item.get(
                "country",
                {}
            )

            return {
                "id": 2,
                "name": league.get(
                    "name",
                    "UEFA Champions League"
                ),
                "logo": league.get("logo"),
                "country": country.get(
                    "name",
                    "Europe"
                )
            }

    except Exception as e:

        print(
            "C1 ERROR:",
            repr(e)
        )

    return {
        "id": 2,
        "name": "UEFA Champions League",
        "logo": None,
        "country": "Europe"
    }


# =========================================================
# /SOI HOME
# =========================================================

class LeagueHomeView(
    discord.ui.View
):

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

        # Modal không cần defer
        await interaction.response.send_modal(
            LeagueSearchModal()
        )


# =========================================================
# SEARCH MODAL
# =========================================================

class LeagueSearchModal(
    discord.ui.Modal
):

    def __init__(self):

        super().__init__(
            title="🔎 Tìm giải bóng đá"
        )

        self.query = discord.ui.TextInput(
            label="Tên giải",
            placeholder=(
                "Ví dụ: C1, Champions League, Premier League"
            ),
            required=True,
            min_length=2,
            max_length=50
        )

        self.add_item(self.query)

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        # ACK NGAY
        await interaction.response.defer()

        raw = str(
            self.query.value
        ).strip()

        query = normalize_league_search(raw)

        try:

            # =================================================
            # C1: KHÔNG SEARCH, DÙNG ID 2 LUÔN
            # =================================================

            if query == "c1":

                league = await get_champions_league()

                leagues = [league]

            else:

                data = await football.search_leagues(
                    query
                )

                response = data.get(
                    "response",
                    []
                )

                leagues = []

                seen = set()

                for item in response:

                    league = item.get(
                        "league",
                        {}
                    )

                    country = item.get(
                        "country",
                        {}
                    )

                    league_id = league.get("id")

                    if not league_id:
                        continue

                    if league_id in seen:
                        continue

                    seen.add(league_id)

                    leagues.append({
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
                    })

                    if len(leagues) >= 25:
                        break

            if not leagues:

                await interaction.followup.send(
                    f"❌ Không tìm thấy giải `{raw}`.",
                    ephemeral=True
                )

                return

            view = LeagueResultsView(
                leagues
            )

            embed = discord.Embed(
                title="🔎 Kết quả tìm giải",
                description=(
                    f"Từ khóa: **{raw}**\n\n"
                    "👇 Bấm vào menu bên dưới để chọn giải."
                )
            )

            await interaction.followup.send(
                embed=embed,
                view=view
            )

        except Exception as e:

            print(
                "SEARCH ERROR:",
                repr(e)
            )

            await interaction.followup.send(
                "❌ Lỗi tìm giải:\n"
                f"```{shorten(str(e), 1200)}```",
                ephemeral=True
            )


# =========================================================
# LEAGUE RESULTS
# =========================================================

class LeagueResultsView(
    discord.ui.View
):

    def __init__(self, leagues):

        super().__init__(
            timeout=300
        )

        self.leagues = {
            str(x["id"]): x
            for x in leagues
        }

        options = []

        for league in leagues:

            options.append(
                discord.SelectOption(
                    label=shorten(
                        league["name"],
                        100
                    ),
                    description=shorten(
                        league["country"],
                        100
                    ),
                    value=str(
                        league["id"]
                    )
                )
            )

        if options:

            self.add_item(
                LeagueSelect(
                    options,
                    self.leagues
                )
            )


# =========================================================
# LEAGUE SELECT
# =========================================================

class LeagueSelect(
    discord.ui.Select
):

    def __init__(
        self,
        options,
        leagues
    ):

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

        try:

            # =================================================
            # CỰC KỲ QUAN TRỌNG:
            # ACK NGAY LẬP TỨC
            # =================================================

            await interaction.response.defer()

            selected_id = self.values[0]

            league = self.leagues.get(
                selected_id
            )

            if not league:

                await interaction.followup.send(
                    "❌ Không tìm thấy giải.",
                    ephemeral=True
                )

                return

            selected_date = today_string()

            view = LeagueDateView(
                league,
                selected_date
            )

            embed = build_league_embed(
                league,
                selected_date
            )

            # Sửa message sau khi defer
            await interaction.edit_original_response(
                embed=embed,
                view=view
            )

        except Exception as e:

            print(
                "LEAGUE SELECT ERROR:",
                repr(e)
            )

            try:

                await interaction.followup.send(
                    "❌ Lỗi khi chọn giải:\n"
                    f"```{shorten(str(e), 1200)}```",
                    ephemeral=True
                )

            except Exception as e2:

                print(
                    "FOLLOWUP ERROR:",
                    repr(e2)
                )


# =========================================================
# LEAGUE EMBED
# =========================================================

def build_league_embed(
    league,
    selected_date
):

    parsed = parse_date(
        selected_date
    )

    embed = discord.Embed(
        title=f"🏆 {league['name']}",
        description=(
            f"🌍 {league['country']}\n"
            f"🆔 League ID: `{league['id']}`\n\n"
            f"📅 Ngày đang xem: "
            f"**{format_date(parsed)}**\n\n"
            "Chọn ngày bên dưới để xem lịch trận."
        )
    )

    if league.get("logo"):

        embed.set_thumbnail(
            url=league["logo"]
        )

    return embed


# =========================================================
# API FREE SEASON ERROR
# =========================================================

def free_season_embed(
    league,
    target_date,
    season
):

    parsed = parse_date(
        target_date
    )

    embed = discord.Embed(
        title="⚠️ API Free không có quyền truy cập mùa này",
        description=(
            f"🏆 **{league['name']}**\n"
            f"📅 **{format_date(parsed)}**\n"
            f"📊 Season: **{season}**\n\n"

            "API-Football trả về:\n"
            "`Free plans do not have access to this season`\n\n"

            "Nghĩa là API key Free hiện tại "
            "không được phép lấy dữ liệu của season này.\n\n"

            "❗ Bot **không lấy mùa cũ để giả thành "
            "lịch của ngày hiện tại**."
        ),
        color=discord.Color.orange()
    )

    if league.get("logo"):
        embed.set_thumbnail(
            url=league["logo"]
        )

    return embed


# =========================================================
# DATE VIEW
# =========================================================

class LeagueDateView(
    discord.ui.View
):

    def __init__(
        self,
        league,
        selected_date
    ):

        super().__init__(
            timeout=600
        )

        self.league = league
        self.selected_date = selected_date

    async def load_fixtures(
        self,
        interaction,
        date_text
    ):

        # Nếu callback chưa ACK thì ACK
        if not interaction.response.is_done():
            await interaction.response.defer()

        try:

            season = await get_season_for_date(
                self.league["id"],
                date_text
            )

            print(
                f"[FIXTURE] "
                f"league={self.league['id']} "
                f"date={date_text} "
                f"season={season}"
            )

            try:

                data = await football.get_fixtures(
                    self.league["id"],
                    season,
                    date_text
                )

            except FootballAPIError as e:

                if (
                    "Free plans do not have access to this season"
                    in str(e)
                ):

                    await interaction.followup.send(
                        embed=free_season_embed(
                            self.league,
                            date_text,
                            season
                        )
                    )

                    return

                raise

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

            embed = build_fixture_embed(
                self.league,
                date_text,
                fixtures
            )

            view = FixtureListView(
                self.league,
                date_text,
                fixtures
            )

            await interaction.followup.send(
                embed=embed,
                view=view
            )

        except Exception as e:

            print(
                "LOAD FIXTURE ERROR:",
                repr(e)
            )

            await interaction.followup.send(
                "❌ Không lấy được lịch trận:\n"
                f"```{shorten(str(e), 1200)}```",
                ephemeral=True
            )

    # -----------------------------------------------------
    # HÔM QUA
    # -----------------------------------------------------

    @discord.ui.button(
        label="📅 Hôm qua",
        style=discord.ButtonStyle.secondary
    )
    async def yesterday(
        self,
        interaction,
        button
    ):

        await interaction.response.defer()

        d = parse_date(
            self.selected_date
        ) or now_vn()

        d -= timedelta(days=1)

        self.selected_date = d.strftime(
            "%Y-%m-%d"
        )

        await self.load_fixtures(
            interaction,
            self.selected_date
        )

    # -----------------------------------------------------
    # HÔM NAY
    # -----------------------------------------------------

    @discord.ui.button(
        label="📅 Hôm nay",
        style=discord.ButtonStyle.success
    )
    async def today(
        self,
        interaction,
        button
    ):

        await interaction.response.defer()

        self.selected_date = today_string()

        await self.load_fixtures(
            interaction,
            self.selected_date
        )

    # -----------------------------------------------------
    # NGÀY MAI
    # -----------------------------------------------------

    @discord.ui.button(
        label="📅 Ngày mai",
        style=discord.ButtonStyle.secondary
    )
    async def tomorrow(
        self,
        interaction,
        button
    ):

        await interaction.response.defer()

        d = parse_date(
            self.selected_date
        ) or now_vn()

        d += timedelta(days=1)

        self.selected_date = d.strftime(
            "%Y-%m-%d"
        )

        await self.load_fixtures(
            interaction,
            self.selected_date
        )

    # -----------------------------------------------------
    # CHỌN NGÀY
    # -----------------------------------------------------

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
            DateModal(self.league)
        )


# =========================================================
# DATE MODAL
# =========================================================

class DateModal(
    discord.ui.Modal
):

    def __init__(
        self,
        league
    ):

        super().__init__(
            title="📅 Chọn ngày"
        )

        self.league = league

        self.date_input = discord.ui.TextInput(
            label="Ngày",
            placeholder="YYYY-MM-DD",
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
                "❌ Sai định dạng.\n"
                "Nhập ví dụ: `2026-10-01`",
                ephemeral=True
            )

            return

        # ACK NGAY
        await interaction.response.defer()

        try:

            season = await get_season_for_date(
                self.league["id"],
                date_text
            )

            print(
                f"[DATE] "
                f"league={self.league['id']} "
                f"date={date_text} "
                f"season={season}"
            )

            try:

                data = await football.get_fixtures(
                    self.league["id"],
                    season,
                    date_text
                )

            except FootballAPIError as e:

                if (
                    "Free plans do not have access to this season"
                    in str(e)
                ):

                    await interaction.followup.send(
                        embed=free_season_embed(
                            self.league,
                            date_text,
                            season
                        )
                    )

                    return

                raise

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

            embed = build_fixture_embed(
                self.league,
                date_text,
                fixtures
            )

            view = FixtureListView(
                self.league,
                date_text,
                fixtures
            )

            await interaction.followup.send(
                embed=embed,
                view=view
            )

        except Exception as e:

            print(
                "DATE ERROR:",
                repr(e)
            )

            await interaction.followup.send(
                "❌ Lỗi lấy lịch:\n"
                f"```{shorten(str(e), 1200)}```",
                ephemeral=True
            )


# =========================================================
# FIXTURE EMBED
# =========================================================

def build_fixture_embed(
    league,
    date_text,
    fixtures
):

    parsed = parse_date(date_text)

    embed = discord.Embed(
        title=f"⚽ {league['name']}",
        description=(
            f"📅 **{format_date(parsed)}**\n"
            f"🌍 {league['country']}\n"
        )
    )

    if not fixtures:

        embed.description += (
            "\n❌ **Không có trận nào trong ngày này.**\n\n"
            "Thử ngày khác nhé."
        )

    else:

        for index, fixture in enumerate(
            fixtures[:25],
            1
        ):

            f = fixture.get(
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

            timestamp = f.get(
                "timestamp"
            )

            if timestamp:

                time_text = datetime.fromtimestamp(
                    timestamp,
                    TZ
                ).strftime("%H:%M")

            else:

                time_text = "--:--"

            status = f.get(
                "status",
                {}
            ).get(
                "short",
                "?"
            )

            embed.add_field(
                name=(
                    f"{index}. "
                    f"{home.get('name', '?')} "
                    f"vs "
                    f"{away.get('name', '?')}"
                ),
                value=(
                    f"🕐 {time_text} "
                    f"• `{status}`\n"
                    f"🆔 `{f.get('id')}`"
                ),
                inline=False
            )

    embed.set_footer(
        text=(
            f"{len(fixtures)} trận • "
            "Chọn trận bên dưới để soi"
        )
    )

    if league.get("logo"):
        embed.set_thumbnail(
            url=league["logo"]
        )

    return embed


# =========================================================
# FIXTURE LIST
# =========================================================

class FixtureListView(
    discord.ui.View
):

    def __init__(
        self,
        league,
        selected_date,
        fixtures
    ):

        super().__init__(
            timeout=600
        )

        self.league = league
        self.selected_date = selected_date

        self.fixtures = {
            str(
                x.get(
                    "fixture",
                    {}
                ).get("id")
            ): x
            for x in fixtures
        }

        options = []

        for fixture in fixtures[:25]:

            f = fixture.get(
                "fixture",
                {}
            )

            teams = fixture.get(
                "teams",
                {}
            )

            fixture_id = f.get("id")

            if not fixture_id:
                continue

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

            timestamp = f.get(
                "timestamp"
            )

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
                    self.fixtures
                )
            )


# =========================================================
# FIXTURE SELECT
# =========================================================

class FixtureSelect(
    discord.ui.Select
):

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

        self.fixtures = fixtures

    async def callback(
        self,
        interaction: discord.Interaction
    ):

        try:

            # ACK NGAY
            await interaction.response.defer()

            fixture_id = self.values[0]

            fixture = self.fixtures.get(
                fixture_id
            )

            if not fixture:

                await interaction.followup.send(
                    "❌ Không tìm thấy trận.",
                    ephemeral=True
                )

                return

            embed = await build_match_analysis(
                fixture
            )

            await interaction.followup.send(
                embed=embed
            )

        except Exception as e:

            print(
                "FIXTURE SELECT ERROR:",
                repr(e)
            )

            await interaction.followup.send(
                "❌ Lỗi soi trận:\n"
                f"```{shorten(str(e), 1200)}```",
                ephemeral=True
            )


# =========================================================
# MATCH ANALYSIS
# =========================================================

async def build_match_analysis(
    fixture
):

    f = fixture.get(
        "fixture",
        {}
    )

    league = fixture.get(
        "league",
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

    fixture_id = f.get("id")

    home_name = home.get(
        "name",
        "Home"
    )

    away_name = away.get(
        "name",
        "Away"
    )

    timestamp = f.get(
        "timestamp"
    )

    if timestamp:

        kickoff = datetime.fromtimestamp(
            timestamp,
            TZ
        ).strftime(
            "%d/%m/%Y %H:%M"
        )

    else:

        kickoff = "Không rõ"

    status = f.get(
        "status",
        {}
    ).get(
        "short",
        "?"
    )

    embed = discord.Embed(
        title=(
            f"🔎 {home_name} vs {away_name}"
        ),
        description=(
            f"🏆 {league.get('name', 'Không rõ')}\n"
            f"📅 {kickoff}\n"
            f"📊 `{status}`\n"
            f"🆔 `{fixture_id}`"
        )
    )

    # =====================================================
    # PREDICTION
    # =====================================================

    prediction = None

    try:

        data = await football.get_prediction(
            fixture_id
        )

        response = data.get(
            "response",
            []
        )

        if response:
            prediction = response[0]

    except Exception as e:

        print(
            "PREDICTION ERROR:",
            repr(e)
        )

    if prediction:

        predictions = prediction.get(
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

        goals = predictions.get(
            "goals",
            {}
        )

        percent = predictions.get(
            "percent",
            {}
        )

        text = ""

        if winner_name:

            text += (
                f"🏆 Winner model: **{winner_name}**\n"
            )

        if advice:

            text += (
                f"💡 Advice: **{advice}**\n"
            )

        if (
            goals.get("home") is not None
            or goals.get("away") is not None
        ):

            text += (
                f"🎯 Tỷ số model: "
                f"**{safe_text(goals.get('home'), '?')}"
                f" - "
                f"{safe_text(goals.get('away'), '?')}**\n"
            )

        if percent:

            text += (
                "\n📊 **Xác suất:**\n"
                f"🏠 {safe_text(percent.get('home'), '?')}\n"
                f"🤝 {safe_text(percent.get('draw'), '?')}\n"
                f"✈️ {safe_text(percent.get('away'), '?')}"
            )

        embed.add_field(
            name="📈 Phân tích",
            value=text[:1024] or "Không có dữ liệu.",
            inline=False
        )

    else:

        embed.add_field(
            name="📈 Phân tích",
            value=(
                "API chưa có dữ liệu prediction "
                "cho trận này."
            ),
            inline=False
        )

    if home.get("logo"):

        embed.set_thumbnail(
            url=home["logo"]
        )

    if away.get("logo"):

        embed.set_image(
            url=away["logo"]
        )

    embed.set_footer(
        text=(
            "Dữ liệu từ API-Football • "
            "Prediction chỉ mang tính thống kê."
        )
    )

    return embed


# =========================================================
# /PING
# =========================================================

@bot.tree.command(
    name="ping",
    description="Kiểm tra bot",
    guild=GUILD
)
async def ping(interaction):

    await interaction.response.send_message(
        f"🏓 Pong!\n"
        f"Discord: `{round(bot.latency * 1000)}ms`\n"
        f"API còn: `{football.last_remaining or 'chưa gọi'}`"
    )


# =========================================================
# /APIQUOTA
# =========================================================

@bot.tree.command(
    name="apiquota",
    description="Xem quota API",
    guild=GUILD
)
async def apiquota(interaction):

    await interaction.response.send_message(
        "📡 **API-Football**\n"
        f"Requests còn: "
        f"`{football.last_remaining or 'chưa có'}`\n"
        f"Giới hạn: "
        f"`{football.last_limit or 'chưa có'}`"
    )


# =========================================================
# /KHOINGHIEP
# =========================================================

@bot.tree.command(
    name="khoinghiep",
    description="Tạo ví tiền ảo",
    guild=GUILD
)
async def khoinghiep(interaction):

    user_id = interaction.user.id

    conn = db_connect()
    cur = conn.cursor()

    cur.execute(
        "SELECT balance FROM users WHERE user_id=?",
        (user_id,)
    )

    row = cur.fetchone()

    if row:

        conn.close()

        await interaction.response.send_message(
            f"💰 Bạn đã có ví.\n"
            f"Số dư: **{row[0]:,}**"
        )

        return

    cur.execute(
        """
        INSERT INTO users(user_id, balance)
        VALUES(?, ?)
        """,
        (
            user_id,
            10000
        )
    )

    conn.commit()
    conn.close()

    await interaction.response.send_message(
        "🎉 Tạo ví thành công!\n"
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
async def vi(interaction):

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
        f"💰 Số dư: **{row[0]:,}** tiền ảo."
    )


# =========================================================
# /CADO
# =========================================================

@bot.tree.command(
    name="cado",
    description="Cược bằng tiền ảo Discord",
    guild=GUILD
)
@app_commands.describe(
    fixture_id="ID trận",
    team="Đội nhà / hòa / đội khách",
    amount="Số tiền ảo"
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
    interaction,
    fixture_id: int,
    team: app_commands.Choice[str],
    amount: int
):

    if amount <= 0:

        await interaction.response.send_message(
            "❌ Số tiền phải lớn hơn 0."
        )

        return

    await interaction.response.defer()

    try:

        data = await football.get_fixture(
            fixture_id
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
            f"❌ API lỗi: `{shorten(e, 800)}`"
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

    if status not in ("NS", "TBD"):

        await interaction.followup.send(
            f"❌ Trận không còn nhận cược.\n"
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
            f"Ví: **{balance:,}**"
        )

        return

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
            "❌ Bạn đã cược trận này rồi."
        )

        return

    cur.execute(
        """
        UPDATE users
        SET balance=balance-?
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
            datetime.now().isoformat()
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

    pick = {
        "home": home,
        "draw": "Hòa",
        "away": away
    }.get(
        team.value,
        team.value
    )

    await interaction.followup.send(
        f"🎫 **Đã đặt cược**\n\n"
        f"⚽ {home} vs {away}\n"
        f"🎯 Chọn: **{pick}**\n"
        f"💰 Cược: **{amount:,}**\n"
        f"💵 Còn: **{balance - amount:,}**\n\n"
        "Tiền này chỉ là tiền ảo Discord."
    )


# =========================================================
# /CUOC
# =========================================================

@bot.tree.command(
    name="cuoc",
    description="Xem vé cược",
    guild=GUILD
)
async def cuoc(interaction):

    conn = db_connect()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT fixture_id, pick, amount
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
            "🎫 Không có vé cược đang chờ."
        )

        return

    lines = []

    for fixture_id, pick, amount in rows:

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
        "🎫 **Vé cược đang chờ**\n\n"
        + "\n".join(lines)
    )


# =========================================================
# /KETTOAN
# =========================================================

def match_result(fixture):

    teams = fixture.get(
        "teams",
        {}
    )

    goals = fixture.get(
        "goals",
        {}
    )

    if teams.get("home", {}).get("winner") is True:
        return "home"

    if teams.get("away", {}).get("winner") is True:
        return "away"

    home_goals = goals.get("home")
    away_goals = goals.get("away")

    if home_goals is None or away_goals is None:
        return None

    if home_goals > away_goals:
        return "home"

    if away_goals > home_goals:
        return "away"

    return "draw"


@bot.tree.command(
    name="kettoan",
    description="Admin kết toán trận",
    guild=GUILD
)
@app_commands.describe(
    fixture_id="ID trận"
)
async def kettoan(
    interaction,
    fixture_id: int
):

    if not interaction.user.guild_permissions.administrator:

        await interaction.response.send_message(
            "❌ Chỉ Admin được dùng lệnh này.",
            ephemeral=True
        )

        return

    await interaction.response.defer()

    try:

        data = await football.get_fixture(
            fixture_id
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
            f"❌ API lỗi: `{shorten(e, 800)}`"
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
            f"Trạng thái: `{status}`"
        )

        return

    result = match_result(fixture)

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
            "ℹ️ Không có vé cược đang chờ."
        )

        return

    winners = 0
    losers = 0
    payout_total = 0

    for bet_id, user_id, pick, amount in bets:

        if pick == result:

            payout = amount * 2

            cur.execute(
                """
                UPDATE users
                SET balance=balance+?
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
            payout_total += payout

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

    result_name = {
        "home": home,
        "draw": "Hòa",
        "away": away
    }[result]

    await interaction.followup.send(
        f"✅ **Đã kết toán**\n\n"
        f"⚽ {home} vs {away}\n"
        f"🏁 Kết quả: **{result_name}**\n"
        f"🔢 Tỷ số: "
        f"**{goals.get('home', '-')} - "
        f"{goals.get('away', '-')}**\n\n"
        f"🟢 Thắng: **{winners}**\n"
        f"🔴 Thua: **{losers}**\n"
        f"💰 Tổng trả: **{payout_total:,}**"
    )


# =========================================================
# COMMAND ERROR
# =========================================================

@bot.tree.error
async def command_error(
    interaction,
    error
):

    print(
        "COMMAND ERROR:",
        repr(error)
    )

    try:

        msg = (
            "❌ Bot gặp lỗi:\n"
            f"```{shorten(str(error), 1000)}```"
        )

        if interaction.response.is_done():

            await interaction.followup.send(
                msg,
                ephemeral=True
            )

        else:

            await interaction.response.send_message(
                msg,
                ephemeral=True
            )

    except Exception as e:

        print(
            "ERROR HANDLER:",
            repr(e)
        )


# =========================================================
# HEALTH SERVER
# =========================================================

async def health(request):

    return web.json_response({
        "status": "online",
        "bot": str(bot.user) if bot.user else None,
        "api_remaining": football.last_remaining
    })


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
        health
    )

    app.router.add_get(
        "/health",
        health
    )

    runner = web.AppRunner(app)

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        port
    )

    await site.start()

    print(
        f"Health server running on {port}"
    )

    return runner


# =========================================================
# READY
# =========================================================

startup_sent = False


@bot.event
async def on_ready():

    global startup_sent

    print(
        f"🟢 BOT ONLINE: {bot.user}"
    )

    print(
        f"Guild: {GUILD_ID}"
    )

    try:

        await bot.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="/soi • bóng đá ⚽"
            )
        )

    except Exception as e:

        print(
            "PRESENCE ERROR:",
            repr(e)
        )

    if not startup_sent:

        startup_sent = True

        try:

            channel = bot.get_channel(
                STARTUP_CHANNEL_ID
            )

            if channel:

                await channel.send(
                    "🟢 **Bot bóng đá đã online!**\n"
                    "Dùng `/soi` để bắt đầu."
                )

        except Exception as e:

            print(
                "STARTUP MESSAGE ERROR:",
                repr(e)
            )


# =========================================================
# SETUP HOOK
# =========================================================

@bot.event
async def setup_hook():

    init_db()

    await football.start()

    try:

        synced = await bot.tree.sync(
            guild=GUILD
        )

        print(
            f"✅ Synced {len(synced)} commands."
        )

    except Exception as e:

        print(
            "SYNC ERROR:",
            repr(e)
        )


# =========================================================
# MAIN
# =========================================================

async def main():

    health_runner = await start_health_server()

    try:

        await bot.start(
            DISCORD_TOKEN
        )

    finally:

        await football.close()

        await health_runner.cleanup()


if __name__ == "__main__":

    asyncio.run(
        main()
    )

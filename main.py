import os
import re
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


# ============================================================
# CONFIG
# ============================================================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY", "").strip()

GUILD_ID = 1551547049600618538
STARTUP_CHANNEL_ID = 1553673308971797133042

GUILD = discord.Object(id=GUILD_ID)

API_BASE = "https://v3.football.api-sports.io"

TIMEZONE = "Asia/Ho_Chi_Minh"
TZ = ZoneInfo(TIMEZONE)

DB_PATH = "/data/football_bot.db" if os.path.isdir("/data") else "football_bot.db"

STARTING_BALANCE = 100000


if not DISCORD_TOKEN:
    raise RuntimeError("Thiếu biến môi trường DISCORD_TOKEN")

if not FOOTBALL_API_KEY:
    raise RuntimeError("Thiếu biến môi trường FOOTBALL_API_KEY")


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

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wallets (
                user_id INTEGER PRIMARY KEY,
                balance INTEGER NOT NULL DEFAULT 100000
            )
            """
        )

        conn.execute(
            """
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
            """
        )

        conn.commit()


def ensure_wallet(user_id: int):

    with db_connect() as conn:

        row = conn.execute(
            "SELECT balance FROM wallets WHERE user_id = ?",
            (user_id,)
        ).fetchone()

        if row is None:

            conn.execute(
                "INSERT INTO wallets(user_id, balance) VALUES (?, ?)",
                (user_id, STARTING_BALANCE)
            )

            conn.commit()

            return STARTING_BALANCE

        return int(row["balance"])


def get_balance(user_id: int) -> int:
    return ensure_wallet(user_id)


def set_balance(user_id: int, balance: int):

    with db_connect() as conn:

        conn.execute(
            """
            INSERT INTO wallets(user_id, balance)
            VALUES (?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET balance = excluded.balance
            """,
            (user_id, balance)
        )

        conn.commit()


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

            timeout = aiohttp.ClientTimeout(total=20)

            self.session = aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    "x-apisports-key": FOOTBALL_API_KEY,
                    "Accept": "application/json",
                },
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

        if cached and now - cached[0] < self.cache_ttl:
            return cached[1]

        url = f"{API_BASE}/{endpoint.lstrip('/')}"

        last_exc = None

        for attempt in range(3):

            try:

                async with self.session.get(
                    url,
                    params=params
                ) as resp:

                    raw = await resp.text()

                    try:
                        data = json.loads(raw)

                    except json.JSONDecodeError:

                        data = {
                            "errors": {
                                "response": raw[:500]
                            }
                        }


                    remaining = resp.headers.get(
                        "x-ratelimit-requests-remaining"
                    )

                    if remaining is not None:
                        self.last_quota = remaining


                    if resp.status >= 500 and attempt < 2:

                        await asyncio.sleep(
                            0.8 * (attempt + 1)
                        )

                        continue


                    if resp.status >= 400:

                        raise RuntimeError(
                            f"HTTP {resp.status}: "
                            f"{data.get('errors') or raw[:300]}"
                        )


                    errors = data.get("errors")

                    if errors:

                        if (
                            isinstance(errors, dict)
                            and any(errors.values())
                        ):
                            raise RuntimeError(str(errors))

                        if (
                            isinstance(errors, list)
                            and errors
                        ):
                            raise RuntimeError(str(errors))


                    self.cache[cache_key] = (
                        time.monotonic(),
                        data
                    )

                    return data


            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                RuntimeError
            ) as exc:

                last_exc = exc

                if attempt < 2:

                    await asyncio.sleep(
                        0.8 * (attempt + 1)
                    )

                else:

                    self.last_errors.append(
                        str(exc)
                    )


        raise RuntimeError(
            str(last_exc)
            if last_exc
            else "API request failed"
        )


    async def leagues_search(self, query: str):

        data = await self.request(
            "leagues",
            {
                "search": query
            }
        )

        return data.get("response", [])


    async def fixtures(
        self,
        league_id: int,
        season: int,
        match_date: str
    ):

        return await self.request(
            "fixtures",
            {
                "league": league_id,
                "season": season,
                "date": match_date,
                "timezone": TIMEZONE,
            }
        )


    async def fixture_by_id(self, fixture_id: int):

        return await self.request(
            "fixtures",
            {
                "id": fixture_id
            }
        )


    async def predictions(self, fixture_id: int):

        return await self.request(
            "predictions",
            {
                "fixture": fixture_id
            }
        )


football = FootballAPI()


# ============================================================
# HELPERS
# ============================================================

C1_ALIASES = {

    "c1": (
        2,
        "UEFA Champions League"
    ),

    "ucl": (
        2,
        "UEFA Champions League"
    ),

    "champions league": (
        2,
        "UEFA Champions League"
    ),

    "uefa champions league": (
        2,
        "UEFA Champions League"
    ),

    "champions": (
        2,
        "UEFA Champions League"
    ),
}


def season_for_date(date_text: str) -> int:

    d = datetime.strptime(
        date_text,
        "%Y-%m-%d"
    ).date()

    if d.month >= 7:
        return d.year

    return d.year - 1


def clean_query(text: str) -> str:

    return re.sub(
        r"\s+",
        " ",
        text.strip()
    )


def fmt_money(value: int) -> str:

    return f"{value:,}".replace(",", ".")


def fixture_status(fixture: dict) -> str:

    return (
        (fixture.get("fixture") or {})
        .get("status") or {}
    ).get("short", "")


def fixture_datetime(fixture: dict) -> str:

    raw = (
        (fixture.get("fixture") or {})
        .get("date")
    )

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


def team_name(
    fixture: dict,
    side: str
) -> str:

    return (
        ((fixture.get("teams") or {})
         .get(side) or {})
        .get("name")
        or "?"
    )


def team_logo(
    fixture: dict,
    side: str
) -> Optional[str]:

    return (
        ((fixture.get("teams") or {})
         .get(side) or {})
        .get("logo")
    )


def result_from_fixture(
    fixture: dict
) -> Optional[str]:

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


def choice_label(
    choice: str,
    fixture: dict
) -> str:

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


def free_season_embed(
    league_name: str,
    season: int
) -> discord.Embed:

    embed = discord.Embed(
        title="⚠️ API-Football Free không hỗ trợ mùa này",
        color=discord.Color.orange()
    )

    embed.description = (

        f"Giải **{league_name}** đang được hỏi "
        f"ở mùa **{season}**.\n\n"

        "Key API-Football Free hiện không có "
        "quyền truy cập mùa này.\n\n"

        "Bot **không dùng dữ liệu mùa cũ "
        "để giả làm lịch hiện tại**.\n\n"

        "Muốn lấy lịch thật của mùa này, "
        "cần API plan/key có quyền truy cập season đó."
    )

    return embed


def api_error_embed(
    exc: Exception
) -> discord.Embed:

    msg = str(exc)

    if (
        "Free plans" in msg
        or "try from 2022 to 2024" in msg
    ):

        return discord.Embed(
            title="⚠️ API-Football Free bị giới hạn",

            description=(
                "API key hiện tại không có "
                "quyền lấy mùa giải này.\n"
                "Bot không thể tự vượt "
                "giới hạn của API."
            ),

            color=discord.Color.orange()
        )


    return discord.Embed(
        title="❌ Không lấy được dữ liệu",

        description=f"`{msg[:900]}`",

        color=discord.Color.red()
    )


# ============================================================
# /SOI - HOME
# ============================================================

class LeagueHomeView(
    discord.ui.View
):

    def __init__(self):

        super().__init__(
            timeout=180
        )


    @discord.ui.button(
        label="🔎 Tìm giải đấu",
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


class LeagueSearchModal(
    discord.ui.Modal,
    title="Tìm giải đấu"
):

    query = discord.ui.TextInput(

        label="Tên giải đấu",

        placeholder=(
            "VD: Champions League, "
            "Premier League, La Liga"
        ),

        required=True,

        max_length=80
    )


    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        q = clean_query(
            str(self.query)
        )

        await interaction.response.defer(
            ephemeral=True
        )

        try:

            key = q.lower()

            if key in C1_ALIASES:

                leagues = [
                    {
                        "league": {
                            "id": 2,
                            "name":
                                "UEFA Champions League",
                            "type": "Cup"
                        }
                    }
                ]

            else:

                leagues = await football.leagues_search(
                    q
                )


            if not leagues:

                await interaction.followup.send(
                    "❌ Không tìm thấy giải đấu.",
                    ephemeral=True
                )

                return


            unique = []

            seen = set()


            for item in leagues:

                league = item.get(
                    "league"
                ) or {}

                lid = league.get("id")

                if lid is None:
                    continue

                if lid in seen:
                    continue

                seen.add(lid)

                unique.append(item)

                if len(unique) >= 25:
                    break


            if not unique:

                await interaction.followup.send(
                    "❌ Không tìm thấy giải đấu hợp lệ.",
                    ephemeral=True
                )

                return


            await interaction.followup.send(

                "Chọn giải đấu:",

                view=LeagueResultsView(
                    unique
                ),

                ephemeral=True
            )


        except Exception as exc:

            await interaction.followup.send(

                embed=api_error_embed(exc),

                ephemeral=True
            )


# ============================================================
# LEAGUE RESULTS
# ============================================================

class LeagueResultsView(
    discord.ui.View
):

    def __init__(
        self,
        leagues: list[dict]
    ):

        super().__init__(
            timeout=180
        )

        self.leagues = {}

        options = []


        for item in leagues[:25]:

            league = item.get(
                "league"
            ) or {}

            lid = league.get("id")

            if lid is None:
                continue

            sid = str(lid)

            self.leagues[sid] = league


            options.append(
                discord.SelectOption(

                    label=str(
                        league.get(
                            "name"
                        ) or
                        f"League {lid}"
                    )[:100],

                    value=sid,

                    description=str(
                        league.get(
                            "type"
                        ) or
                        "League"
                    )[:100]
                )
            )


        if options:

            self.add_item(
                LeagueSelect(
                    options,
                    self.leagues
                )
            )


class LeagueSelect(
    discord.ui.Select
):

    def __init__(
        self,
        options,
        leagues
    ):

        super().__init__(
            placeholder="Chọn giải đấu...",
            min_values=1,
            max_values=1,
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


        league = self.leagues.get(
            self.values[0]
        )


        if not league:

            await interaction.edit_original_response(

                content="❌ Không tìm thấy giải.",

                view=None
            )

            return


        view = LeagueDateView(

            int(league["id"]),

            str(
                league.get("name")
                or "League"
            )
        )


        embed = discord.Embed(

            title=(
                f"⚽ "
                f"{league.get('name', 'League')}"
            ),

            description=(
                "Chọn ngày muốn xem lịch trận."
            ),

            color=discord.Color.blue()
        )


        await interaction.edit_original_response(

            content=None,

            embed=embed,

            view=view
        )


# ============================================================
# DATE VIEW
# ============================================================

class LeagueDateView(
    discord.ui.View
):

    def __init__(
        self,
        league_id: int,
        league_name: str
    ):

        super().__init__(
            timeout=180
        )

        self.league_id = league_id
        self.league_name = league_name


    async def load_fixtures(
        self,
        interaction: discord.Interaction,
        date_text: str
    ):

        try:

            season = season_for_date(
                date_text
            )


            data = await football.fixtures(

                self.league_id,

                season,

                date_text
            )


            fixtures = data.get(
                "response",
                []
            )


            if not fixtures:

                embed = discord.Embed(

                    title="📅 Không có trận",

                    description=(

                        f"Không có lịch trong "
                        f"**{date_text}** cho "
                        f"**{self.league_name}**.\n"

                        f"Season API-Football: "
                        f"**{season}**"
                    ),

                    color=discord.Color.orange()
                )


                await interaction.followup.send(

                    embed=embed,

                    ephemeral=True
                )

                return


            fixtures = fixtures[:25]


            view = FixtureListView(

                fixtures,

                self.league_name,

                date_text
            )


            embed = discord.Embed(

                title=(
                    f"📅 {date_text} — "
                    f"{self.league_name}"
                ),

                description=(
                    f"Tìm thấy **{len(fixtures)}** trận.\n"
                    "Chọn trận để xem phân tích."
                ),

                color=discord.Color.green()
            )


            await interaction.followup.send(

                embed=embed,

                view=view,

                ephemeral=True
            )


        except Exception as exc:

            if (
                "Free plans" in str(exc)
                or
                "try from 2022 to 2024" in str(exc)
            ):

                await interaction.followup.send(

                    embed=free_season_embed(
                        self.league_name,
                        season_for_date(date_text)
                    ),

                    ephemeral=True
                )

            else:

                await interaction.followup.send(

                    embed=api_error_embed(exc),

                    ephemeral=True
                )


    @discord.ui.button(
        label="Hôm nay",
        style=discord.ButtonStyle.primary
    )
    async def today(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await interaction.response.defer(
            ephemeral=True
        )

        today = datetime.now(
            TZ
        ).strftime("%Y-%m-%d")

        await self.load_fixtures(
            interaction,
            today
        )


    @discord.ui.button(
        label="Ngày mai",
        style=discord.ButtonStyle.secondary
    )
    async def tomorrow(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await interaction.response.defer(
            ephemeral=True
        )

        from datetime import timedelta

        tomorrow = (
            datetime.now(TZ).date()
            + timedelta(days=1)
        )


        await self.load_fixtures(

            interaction,

            tomorrow.strftime(
                "%Y-%m-%d"
            )
        )


    @discord.ui.button(
        label="📅 Nhập ngày",
        style=discord.ButtonStyle.success
    )
    async def custom_date(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await interaction.response.send_modal(
            DateModal(self)
        )


# ============================================================
# DATE MODAL
# ============================================================

class DateModal(
    discord.ui.Modal,
    title="Nhập ngày xem lịch"
):

    date_input = discord.ui.TextInput(

        label="Ngày (YYYY-MM-DD)",

        placeholder="2026-09-27",

        required=True,

        min_length=10,

        max_length=10
    )


    def __init__(
        self,
        parent_view: LeagueDateView
    ):

        super().__init__()

        self.parent_view = parent_view


    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        value = str(
            self.date_input
        ).strip()


        try:

            parsed = datetime.strptime(
                value,
                "%Y-%m-%d"
            ).date()


            if (
                parsed.year < 2020
                or
                parsed.year > 2035
            ):
                raise ValueError


        except ValueError:

            await interaction.response.send_message(

                "❌ Ngày không hợp lệ. "
                "Ví dụ: `2026-09-27`",

                ephemeral=True
            )

            return


        await interaction.response.defer(
            ephemeral=True
        )


        await self.parent_view.load_fixtures(
            interaction,
            value
        )


# ============================================================
# FIXTURE LIST
# ============================================================

class FixtureListView(
    discord.ui.View
):

    def __init__(
        self,
        fixtures: list[dict],
        league_name: str,
        date_text: str
    ):

        super().__init__(
            timeout=180
        )

        self.fixtures = {}

        options = []


        for fixture in fixtures[:25]:

            fid = (
                fixture.get("fixture") or {}
            ).get("id")


            if not fid:
                continue


            self.fixtures[str(fid)] = fixture


            home = team_name(
                fixture,
                "home"
            )

            away = team_name(
                fixture,
                "away"
            )

            status = fixture_status(
                fixture
            )


            label = (
                f"{home} vs {away}"
            )[:100]


            desc = (
                f"{fixture_datetime(fixture)} "
                f"• {status}"
            )[:100]


            options.append(
                discord.SelectOption(

                    label=label,

                    value=str(fid),

                    description=desc
                )
            )


        if options:

            self.add_item(
                FixtureSelect(

                    options,

                    self.fixtures,

                    league_name,

                    date_text
                )
            )


class FixtureSelect(
    discord.ui.Select
):

    def __init__(
        self,
        options,
        fixtures,
        league_name,
        date_text
    ):

        super().__init__(
            placeholder="Chọn trận...",
            min_values=1,
            max_values=1,
            options=options
        )

        self.fixtures = fixtures

        self.league_name = league_name

        self.date_text = date_text


    async def callback(
        self,
        interaction: discord.Interaction
    ):

        await interaction.response.defer(
            ephemeral=True
        )


        fixture = self.fixtures.get(
            self.values[0]
        )


        if not fixture:

            await interaction.followup.send(

                "❌ Không tìm thấy trận.",

                ephemeral=True
            )

            return


        await send_match_analysis(

            interaction,

            fixture,

            self.league_name,

            self.date_text
        )


# ============================================================
# MATCH ANALYSIS
# ============================================================

async def send_match_analysis(

    interaction: discord.Interaction,

    fixture: dict,

    league_name: str,

    date_text: str

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


    home_logo = team_logo(
        fixture,
        "home"
    )

    away_logo = team_logo(
        fixture,
        "away"
    )


    embed = discord.Embed(

        title=(
            f"⚽ {home} vs {away}"
        ),

        description=(

            f"🏆 **{league_name}**\n"

            f"📅 **{fixture_datetime(fixture)}**\n"

            f"🆔 Fixture: `{fid}`"
        ),

        color=discord.Color.blurple()
    )


    if home_logo:

        embed.set_thumbnail(
            url=home_logo
        )


    if away_logo:

        embed.set_image(
            url=away_logo
        )


    goals = fixture.get(
        "goals"
    ) or {}


    if (
        goals.get("home") is not None
        or
        goals.get("away") is not None
    ):

        embed.add_field(

            name="Tỷ số",

            value=(

                f"**"
                f"{goals.get('home', 0)}"
                f" - "
                f"{goals.get('away', 0)}"
                f"**"
            ),

            inline=False
        )


    try:

        pdata = await football.predictions(
            int(fid)
        )


        response = (
            pdata.get("response")
            or []
        )


        prediction = (
            response[0].get(
                "predictions"
            )
            if response
            else None
        )


        if prediction:

            winner = (
                prediction.get(
                    "winner"
                )
                or {}
            )


            winner_name = (
                winner.get("name")
                or
                "Chưa có dự đoán"
            )


            advice = (
                prediction.get(
                    "advice"
                )
                or
                "Chưa có nhận định"
            )


            percent = (
                prediction.get(
                    "percent"
                )
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


            embed.add_field(

                name="📊 Dự đoán API-Football",

                value=(

                    f"Đội được API chọn: "
                    f"**{winner_name}**\n"

                    f"Tư vấn: `{advice}`\n"

                    f"Home **{home_percent}%** "
                    f"• Draw **{draw_percent}%** "
                    f"• Away **{away_percent}%**"
                ),

                inline=False
            )


        else:

            embed.add_field(

                name="📊 Dự đoán",

                value=(
                    "API chưa có dữ liệu dự đoán."
                ),

                inline=False
            )


    except Exception:

        embed.add_field(

            name="📊 Dự đoán",

            value=(
                "Không lấy được dữ liệu "
                "dự đoán lúc này."
            ),

            inline=False
        )


    embed.set_footer(
        text=(
            "Dữ liệu từ API-Football "
            "• Không phải lời khuyên cá cược"
        )
    )


    await interaction.followup.send(

        embed=embed,

        ephemeral=True
    )


# ============================================================
# VIRTUAL BETTING
# ============================================================

class BetChoiceView(
    discord.ui.View
):

    def __init__(
        self,
        fixture: dict,
        stake: int
    ):

        super().__init__(
            timeout=120
        )

        self.fixture = fixture

        self.stake = stake


    async def place(
        self,
        interaction: discord.Interaction,
        choice: str
    ):

        user_id = interaction.user.id


        fid = (
            self.fixture.get("fixture")
            or {}
        ).get("id")


        if not fid:

            await interaction.response.send_message(

                "❌ Fixture ID không hợp lệ.",

                ephemeral=True
            )

            return


        balance = get_balance(
            user_id
        )


        if self.stake > balance:

            await interaction.response.send_message(

                f"❌ Không đủ tiền ảo. "
                f"Ví hiện có "
                f"**{fmt_money(balance)}**.",

                ephemeral=True
            )

            return


        with db_connect() as conn:

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

                    "❌ M đã đặt cược trận này rồi.",

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
                    self.stake,
                    datetime.now(TZ).isoformat()
                )
            )


            conn.commit()


        await interaction.response.send_message(

            f"✅ Đặt **{fmt_money(self.stake)}** "
            f"vào **{choice_label(choice, self.fixture)}**.\n"

            f"💰 Còn lại: "
            f"**{fmt_money(get_balance(user_id))}**",

            ephemeral=True
        )


    @discord.ui.button(
        label="Chủ nhà",
        style=discord.ButtonStyle.primary
    )
    async def home(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
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
        interaction: discord.Interaction,
        button: discord.ui.Button
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
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await self.place(
            interaction,
            "away"
        )


# ============================================================
# /SOI COMMAND
# ============================================================

@bot.tree.command(
    name="soi",
    description="Soi lịch và phân tích bóng đá",
    guild=GUILD
)
async def soi(
    interaction: discord.Interaction
):

    embed = discord.Embed(

        title="⚽ SOI BÓNG ĐÁ",

        description=(
            "Bấm **🔎 Tìm giải đấu** "
            "để bắt đầu."
        ),

        color=discord.Color.green()
    )


    await interaction.response.send_message(

        embed=embed,

        view=LeagueHomeView()
    )


# ============================================================
# WALLET COMMANDS
# ============================================================

@bot.tree.command(
    name="khoinghiep",
    description="Nhận tiền ảo khởi nghiệp",
    guild=GUILD
)
async def khoinghiep(
    interaction: discord.Interaction
):

    balance = get_balance(
        interaction.user.id
    )


    await interaction.response.send_message(

        f"💰 Ví của m hiện có "
        f"**{fmt_money(balance)}** tiền ảo.",

        ephemeral=True
    )


@bot.tree.command(
    name="vi",
    description="Xem số dư tiền ảo",
    guild=GUILD
)
async def vi(
    interaction: discord.Interaction
):

    balance = get_balance(
        interaction.user.id
    )


    await interaction.response.send_message(

        f"👛 **{interaction.user.display_name}**\n"

        f"💰 Số dư: "
        f"**{fmt_money(balance)}**",

        ephemeral=True
    )


# ============================================================
# BET COMMAND
# ============================================================

async def place_bet_command(

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

            "❌ Tiền cược tối đa là 10.000.000.",

            ephemeral=True
        )

        return


    await interaction.response.defer(
        ephemeral=True
    )


    try:

        data = await football.fixture_by_id(
            fixture_id
        )


        fixtures = data.get(
            "response",
            []
        )


        if not fixtures:

            await interaction.followup.send(

                "❌ Không tìm thấy fixture.",

                ephemeral=True
            )

            return


        fixture = fixtures[0]


        status = fixture_status(
            fixture
        )


        if status not in {
            "NS",
            "TBD"
        }:

            await interaction.followup.send(

                f"❌ Trận này không còn "
                f"ở trạng thái chưa bắt đầu "
                f"(`{status}`).",

                ephemeral=True
            )

            return


        balance = get_balance(
            interaction.user.id
        )


        if stake > balance:

            await interaction.followup.send(

                "❌ Không đủ tiền ảo.",

                ephemeral=True
            )

            return


        embed = discord.Embed(

            title="🎯 Chọn cửa",

            description=(

                f"**{team_name(fixture, 'home')}** "
                f"vs "
                f"**{team_name(fixture, 'away')}**\n\n"

                f"Cược: "
                f"**{fmt_money(stake)}** tiền ảo"
            ),

            color=discord.Color.gold()
        )


        await interaction.followup.send(

            embed=embed,

            view=BetChoiceView(
                fixture,
                stake
            ),

            ephemeral=True
        )


    except Exception as exc:

        await interaction.followup.send(

            embed=api_error_embed(exc),

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


    with db_connect() as conn:

        open_bets = conn.execute(

            """
            SELECT *
            FROM bets
            WHERE status='open'
            ORDER BY id ASC
            LIMIT 100
            """
        ).fetchall()


    if not open_bets:

        await interaction.followup.send(

            "ℹ️ Không có cược nào "
            "đang chờ kết toán.",

            ephemeral=True
        )

        return


    settled = 0

    skipped = 0


    for bet in open_bets:

        try:

            data = await football.fixture_by_id(

                int(
                    bet["fixture_id"]
                )
            )


            fixtures = data.get(
                "response",
                []
            )


            if not fixtures:

                skipped += 1
                continue


            fixture = fixtures[0]


            result = result_from_fixture(
                fixture
            )


            if result is None:

                skipped += 1
                continue


            if result == bet["choice"]:

                payout = (
                    int(bet["stake"]) * 2
                )


                with db_connect() as conn:

                    conn.execute(

                        """
                        UPDATE wallets
                        SET balance =
                            balance + ?
                        WHERE user_id=?
                        """,

                        (
                            payout,
                            bet["user_id"]
                        )
                    )


                    conn.execute(

                        """
                        UPDATE bets
                        SET status='won'
                        WHERE id=?
                        """,

                        (
                            bet["id"],
                        )
                    )


                    conn.commit()


            else:

                with db_connect() as conn:

                    conn.execute(

                        """
                        UPDATE bets
                        SET status='lost'
                        WHERE id=?
                        """,

                        (
                            bet["id"],
                        )
                    )


                    conn.commit()


            settled += 1


        except Exception:

            skipped += 1


    await interaction.followup.send(

        f"✅ Đã kết toán **{settled}** cược.\n"

        f"⏳ Chưa thể kết toán: "
        f"**{skipped}**.",

        ephemeral=True
    )


# ============================================================
# /CUOC
# ============================================================

@bot.tree.command(
    name="cuoc",
    description="Đặt cược tiền ảo cho một fixture",
    guild=GUILD
)

@app_commands.describe(
    fixture_id="ID trận từ API-Football",
    stake="Số tiền ảo"
)

async def cuoc(

    interaction: discord.Interaction,

    fixture_id: int,

    stake: int

):

    await place_bet_command(

        interaction,

        fixture_id,

        stake
    )


# ============================================================
# /CADO
# ============================================================

@bot.tree.command(
    name="cado",
    description="Mở đặt cược ảo bằng fixture ID",
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

    await place_bet_command(

        interaction,

        fixture_id,

        stake
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

        f"🏓 Pong! "
        f"`{round(bot.latency * 1000)}ms`",

        ephemeral=True
    )


# ============================================================
# /APIQUOTA
# ============================================================

@bot.tree.command(
    name="apiquota",
    description="Xem quota API gần nhất",
    guild=GUILD
)
async def apiquota(
    interaction: discord.Interaction
):

    remaining = (
        football.last_quota
        or
        "chưa nhận từ API"
    )


    await interaction.response.send_message(

        f"📡 API-Football requests remaining: "
        f"**{remaining}**",

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
        f"[WEB] Health server listening "
        f"on port {port}"
    )


    return runner


# ============================================================
# EVENTS
# ============================================================

@bot.event
async def on_ready():

    print(
        f"[DISCORD] Logged in as "
        f"{bot.user} "
        f"(ID: {bot.user.id})"
    )

    print(
        f"[DISCORD] Guild: {GUILD_ID}"
    )


# ============================================================
# COMMAND SYNC
# ============================================================

async def sync_commands():

    synced = await bot.tree.sync(
        guild=GUILD
    )


    print(
        f"[DISCORD] Synced "
        f"{len(synced)} guild commands."
    )


    print(
        "[DISCORD] Commands:",
        ", ".join(
            sorted(
                command.name
                for command in synced
            )
        )
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

            await sync_commands()

            await bot.connect()


    finally:

        await football.close()


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    asyncio.run(
        main()
    )

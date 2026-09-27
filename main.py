import os
import time
import sqlite3
import asyncio
from datetime import datetime, timedelta
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
FOOTBALL_DATA_TOKEN = os.getenv("FOOTBALL_DATA_TOKEN")

GUILD_ID = 1551547049600618538

API_BASE = "https://api.football-data.org/v4"

TZ = ZoneInfo("Asia/Ho_Chi_Minh")

CL_CODE = "CL"


if not DISCORD_TOKEN:
    raise RuntimeError(
        "Thiếu DISCORD_TOKEN trong Railway Variables"
    )

if not FOOTBALL_DATA_TOKEN:
    raise RuntimeError(
        "Thiếu FOOTBALL_DATA_TOKEN trong Railway Variables"
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


def get_balance(user_id: int):

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
            (
                user_id,
                10000
            )
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

class FootballDataError(Exception):
    pass


# =========================================================
# FOOTBALL-DATA API
# =========================================================

class FootballDataAPI:

    def __init__(self):

        self.session: Optional[
            aiohttp.ClientSession
        ] = None

        self.cache = {}

        self.cache_seconds = 60

        self.request_times = []

        self.remaining = None

        self.reset_seconds = None

    async def start(self):

        if (
            self.session is None
            or self.session.closed
        ):

            self.session = aiohttp.ClientSession(
                headers={
                    "X-Auth-Token":
                        FOOTBALL_DATA_TOKEN,

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
            x
            for x in self.request_times
            if now - x < 60
        ]

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
        path: str,
        params=None,
        cache=True
    ):

        await self.start()

        if params is None:
            params = {}

        cache_key = (
            path
            + "?"
            + "&".join(
                f"{k}={v}"
                for k, v in sorted(
                    params.items()
                )
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
                API_BASE + path,
                params=params
            ) as response:

                # API headers
                self.remaining = (
                    response.headers.get(
                        "X-Requests-Available-Minute"
                    )
                )

                self.reset_seconds = (
                    response.headers.get(
                        "X-RequestCounter-Reset"
                    )
                )

                if response.status == 429:

                    raise FootballDataError(
                        "Đã vượt giới hạn 10 request/phút."
                    )

                raw = await response.text()

                if response.status >= 400:

                    raise FootballDataError(
                        f"HTTP {response.status}: "
                        f"{raw[:500]}"
                    )

                try:

                    data = await response.json(
                        content_type=None
                    )

                except Exception:

                    raise FootballDataError(
                        "API trả về dữ liệu không hợp lệ."
                    )

                if cache:

                    self.cache[cache_key] = (
                        time.time(),
                        data
                    )

                return data

        except asyncio.TimeoutError:

            raise FootballDataError(
                "API timeout."
            )

        except aiohttp.ClientError as e:

            raise FootballDataError(
                f"Lỗi kết nối API: {e}"
            )


football = FootballDataAPI()


# =========================================================
# HELPERS
# =========================================================

def now_vn():

    return datetime.now(TZ)


def shorten(
    text,
    max_len=100
):

    text = str(text)

    if len(text) <= max_len:
        return text

    return text[:max_len - 3] + "..."


def format_time(
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


def get_team(
    fixture,
    side
):

    return fixture.get(
        f"{side}Team",
        {}
    )


def team_name(
    fixture,
    side
):

    return get_team(
        fixture,
        side
    ).get(
        "name",
        side
    )


def team_logo(
    fixture,
    side
):

    team = get_team(
        fixture,
        side
    )

    return (
        team.get("crest")
        or team.get("logo")
    )


def team_id(
    fixture,
    side
):

    return get_team(
        fixture,
        side
    ).get(
        "id"
    )


# =========================================================
# DISCORD BOT
# =========================================================

intents = discord.Intents.default()

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


GUILD = discord.Object(
    id=GUILD_ID
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

    await interaction.response.send_message(
        (
            f"🏓 Pong! "
            f"`{round(bot.latency * 1000)}ms`"
        )
    )


# =========================================================
# /APIQUOTA
# =========================================================

@bot.tree.command(
    name="apiquota",
    description="Xem quota API bóng đá",
    guild=GUILD
)
async def apiquota(
    interaction: discord.Interaction
):

    await interaction.response.defer(
        ephemeral=True
    )

    try:

        await football.get(
            "/competitions/CL",
            cache=False
        )

        remaining = (
            football.remaining
            or "?"
        )

        reset = (
            football.reset_seconds
            or "?"
        )

        await interaction.edit_original_response(
            content=(
                "📊 **Football-data.org**\n\n"
                f"🟢 Request còn lại/phút: "
                f"`{remaining}`\n"
                f"⏱️ Reset: `{reset}s`\n"
                "📦 Free: 10 requests/phút"
            )
        )

    except FootballDataError as e:

        await interaction.edit_original_response(
            content=(
                "❌ API lỗi:\n"
                f"`{shorten(e, 800)}`"
            )
        )


# =========================================================
# /SOI
# =========================================================

@bot.tree.command(
    name="soi",
    description="Soi bóng đá",
    guild=GUILD
)
async def soi(
    interaction: discord.Interaction
):

    embed = discord.Embed(
        title="⚽ SOI BÓNG ĐÁ",
        description=(
            "Nguồn dữ liệu: Football-data.org\n\n"
            "🆓 Free Tier\n"
            "🏆 Champions League\n"
            "📅 Lịch trận\n"
            "📊 Bảng xếp hạng\n"
            "📈 Kết quả/phong độ\n\n"
            "Bấm nút bên dưới."
        ),
        color=discord.Color.blue()
    )

    await interaction.response.send_message(
        embed=embed,
        view=LeagueHomeView(),
        ephemeral=True
    )


# =========================================================
# LEAGUE HOME
# =========================================================

class LeagueHomeView(
    discord.ui.View
):

    def __init__(self):

        super().__init__(
            timeout=300
        )

    @discord.ui.button(
        label="🏆 Champions League",
        style=discord.ButtonStyle.primary
    )
    async def champions(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await interaction.response.edit_message(
            content=None,
            embed=discord.Embed(
                title="🏆 UEFA CHAMPIONS LEAGUE",
                description=(
                    "Chọn ngày muốn xem lịch trận."
                ),
                color=discord.Color.blue()
            ),
            view=CLDateView()
        )


# =========================================================
# DATE VIEW
# =========================================================

class CLDateView(
    discord.ui.View
):

    def __init__(self):

        super().__init__(
            timeout=300
        )

    @discord.ui.button(
        label="📅 Chọn ngày",
        style=discord.ButtonStyle.primary
    )
    async def choose_date(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await interaction.response.send_modal(
            DateModal()
        )


# =========================================================
# DATE MODAL
# =========================================================

class DateModal(
    discord.ui.Modal
):

    def __init__(self):

        super().__init__(
            title="Chọn ngày trận"
        )

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
                (
                    "❌ Sai định dạng.\n"
                    "Dùng `YYYY-MM-DD`."
                ),
                ephemeral=True
            )

            return

        await interaction.response.defer(
            ephemeral=True
        )

        await get_matches_for_date(
            interaction,
            date_string
        )


# =========================================================
# GET MATCHES
# =========================================================

async def get_matches_for_date(
    interaction,
    date_string
):

    try:

        # Football-data cho phép lọc dateFrom/dateTo
        data = await football.get(
            "/competitions/CL/matches",
            {
                "dateFrom":
                    date_string,

                "dateTo":
                    date_string
            }
        )

    except FootballDataError as e:

        await interaction.edit_original_response(
            content=(
                "❌ **Không lấy được lịch trận**\n\n"
                f"`{shorten(e, 1000)}`"
            ),
            embed=None,
            view=None
        )

        return

    matches = data.get(
        "matches",
        []
    )

    # Lọc thêm để chắc chắn đúng ngày
    filtered = []

    for match in matches:

        utc_date = match.get(
            "utcDate",
            ""
        )

        if utc_date.startswith(
            date_string
        ):

            filtered.append(
                match
            )

    matches = filtered

    if not matches:

        await interaction.edit_original_response(
            content=(
                f"📅 **{date_string}**\n\n"
                "Không có trận Champions League "
                "nào trong ngày này."
            ),
            embed=None,
            view=None
        )

        return

    matches = matches[:25]

    embed = discord.Embed(
        title="🏆 CHAMPIONS LEAGUE",
        description=(
            f"📅 `{date_string}`\n"
            f"⚽ Có **{len(matches)}** trận"
        ),
        color=discord.Color.blue()
    )

    for match in matches:

        home = team_name(
            match,
            "home"
        )

        away = team_name(
            match,
            "away"
        )

        status = match.get(
            "status",
            "?"
        )

        utc_date = match.get(
            "utcDate",
            ""
        )

        score = match.get(
            "score",
            {}
        )

        full_time = score.get(
            "fullTime",
            {}
        )

        home_goals = full_time.get(
            "home"
        )

        away_goals = full_time.get(
            "away"
        )

        if (
            home_goals is not None
            and away_goals is not None
        ):

            score_text = (
                f"⚽ **{home_goals} - "
                f"{away_goals}**"
            )

        else:

            score_text = (
                "⚽ Chưa đá"
            )

        embed.add_field(
            name=(
                f"{home} vs {away}"
            ),
            value=(
                f"🕐 {format_time(utc_date)}\n"
                f"{score_text}\n"
                f"📌 `{status}`"
            ),
            inline=False
        )

    await interaction.edit_original_response(
        content=None,
        embed=embed,
        view=FixtureView(
            matches
        )
    )


# =========================================================
# FIXTURE VIEW
# =========================================================

class FixtureView(
    discord.ui.View
):

    def __init__(
        self,
        matches
    ):

        super().__init__(
            timeout=300
        )

        self.add_item(
            FixtureSelect(
                matches
            )
        )


class FixtureSelect(
    discord.ui.Select
):

    def __init__(
        self,
        matches
    ):

        options = []

        for match in matches[:25]:

            match_id = match.get(
                "id"
            )

            home = team_name(
                match,
                "home"
            )

            away = team_name(
                match,
                "away"
            )

            status = match.get(
                "status",
                "?"
            )

            options.append(
                discord.SelectOption(
                    label=shorten(
                        f"{home} vs {away}",
                        100
                    ),
                    description=shorten(
                        status,
                        100
                    ),
                    value=str(
                        match_id
                    )
                )
            )

        super().__init__(
            placeholder="⚽ Chọn trận để soi...",
            options=options
        )

        self.matches = matches

    async def callback(
        self,
        interaction: discord.Interaction
    ):

        await interaction.response.defer(
            ephemeral=True
        )

        selected_id = int(
            self.values[0]
        )

        selected = None

        for match in self.matches:

            if match.get(
                "id"
            ) == selected_id:

                selected = match
                break

        if selected is None:

            await interaction.edit_original_response(
                content="❌ Không tìm thấy trận."
            )

            return

        await show_analysis(
            interaction,
            selected
        )


# =========================================================
# MATCH ANALYSIS
# =========================================================

async def show_analysis(
    interaction,
    match
):

    match_id = match.get(
        "id"
    )

    home = team_name(
        match,
        "home"
    )

    away = team_name(
        match,
        "away"
    )

    home_id = team_id(
        match,
        "home"
    )

    away_id = team_id(
        match,
        "away"
    )

    home_crest = team_logo(
        match,
        "home"
    )

    away_crest = team_logo(
        match,
        "away"
    )

    utc_date = match.get(
        "utcDate",
        ""
    )

    status = match.get(
        "status",
        "?"
    )

    score = match.get(
        "score",
        {}
    )

    full_time = score.get(
        "fullTime",
        {}
    )

    home_goals = full_time.get(
        "home"
    )

    away_goals = full_time.get(
        "away"
    )

    # -----------------------------------------------------
    # EMBED
    # -----------------------------------------------------

    embed = discord.Embed(
        title="🔎 SOI TRẬN",
        description=(
            "🏆 **UEFA Champions League**\n\n"
            f"⚽ **{home}**\n"
            "vs\n"
            f"**{away}**\n\n"
            f"🕐 {format_time(utc_date)}\n"
            f"📌 Trạng thái: `{status}`"
        ),
        color=discord.Color.gold()
    )

    if home_crest:

        embed.set_thumbnail(
            url=home_crest
        )

    # -----------------------------------------------------
    # SCORE
    # -----------------------------------------------------

    if (
        home_goals is not None
        and away_goals is not None
    ):

        embed.add_field(
            name="📊 Tỉ số",
            value=(
                f"**{home_goals} - "
                f"{away_goals}**"
            ),
            inline=False
        )

    else:

        embed.add_field(
            name="📊 Tỉ số",
            value="Chưa thi đấu",
            inline=False
        )

    # -----------------------------------------------------
    # MATCHDAY / STAGE
    # -----------------------------------------------------

    matchday = match.get(
        "matchday"
    )

    stage = match.get(
        "stage"
    )

    if matchday:

        embed.add_field(
            name="📅 Matchday",
            value=f"`{matchday}`",
            inline=True
        )

    if stage:

        embed.add_field(
            name="🏆 Vòng",
            value=f"`{stage}`",
            inline=True
        )

    # -----------------------------------------------------
    # VENUE
    # -----------------------------------------------------

    venue = match.get(
        "venue"
    )

    if venue:

        embed.add_field(
            name="🏟️ Sân",
            value=shorten(
                venue,
                100
            ),
            inline=False
        )

    # -----------------------------------------------------
    # IDS
    # -----------------------------------------------------

    embed.add_field(
        name="🆔 Match ID",
        value=f"`{match_id}`",
        inline=True
    )

    embed.add_field(
        name="🏠 Home ID",
        value=f"`{home_id}`",
        inline=True
    )

    embed.add_field(
        name="✈️ Away ID",
        value=f"`{away_id}`",
        inline=True
    )

    # -----------------------------------------------------
    # BASIC FORM
    # -----------------------------------------------------

    form_text = await get_team_form(
        home_id,
        away_id
    )

    embed.add_field(
        name="📈 Phong độ gần đây",
        value=form_text,
        inline=False
    )

    embed.set_footer(
        text=(
            "Nguồn: Football-data.org • "
            "Dữ liệu thật"
        )
    )

    await interaction.edit_original_response(
        content=None,
        embed=embed,
        view=MatchActionView(
            match_id
        )
    )


# =========================================================
# TEAM FORM
# =========================================================

async def get_team_form(
    home_id,
    away_id
):

    if not home_id or not away_id:

        return (
            "Không có ID đội."
        )

    try:

        home_data, away_data = (
            await asyncio.gather(

                football.get(
                    f"/teams/{home_id}/matches",
                    {
                        "status":
                            "FINISHED",
                        "limit":
                            5
                    }
                ),

                football.get(
                    f"/teams/{away_id}/matches",
                    {
                        "status":
                            "FINISHED",
                        "limit":
                            5
                    }
                )
            )
        )

    except Exception:

        return (
            "Không lấy được form."
        )

    def make_form(
        data,
        team_id_value
    ):

        matches = data.get(
            "matches",
            []
        )

        if not matches:

            return "N/A"

        results = []

        for match in matches[:5]:

            score = match.get(
                "score",
                {}
            ).get(
                "fullTime",
                {}
            )

            hg = score.get(
                "home"
            )

            ag = score.get(
                "away"
            )

            if (
                hg is None
                or ag is None
            ):

                continue

            home_id_match = (
                match
                .get("homeTeam", {})
                .get("id")
            )

            if home_id_match == team_id_value:

                if hg > ag:
                    results.append("W")

                elif hg == ag:
                    results.append("D")

                else:
                    results.append("L")

            else:

                if ag > hg:
                    results.append("W")

                elif ag == hg:
                    results.append("D")

                else:
                    results.append("L")

        if not results:

            return "N/A"

        return " ".join(
            results
        )

    home_form = make_form(
        home_data,
        home_id
    )

    away_form = make_form(
        away_data,
        away_id
    )

    return (
        f"🏠 Home: `{home_form}`\n"
        f"✈️ Away: `{away_form}`"
    )


# =========================================================
# MATCH ACTIONS
# =========================================================

class MatchActionView(
    discord.ui.View
):

    def __init__(
        self,
        match_id
    ):

        super().__init__(
            timeout=300
        )

        self.match_id = match_id

    @discord.ui.button(
        label="📊 Bảng xếp hạng",
        style=discord.ButtonStyle.primary
    )
    async def standings(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await interaction.response.defer(
            ephemeral=True
        )

        try:

            data = await football.get(
                "/competitions/CL/standings"
            )

        except FootballDataError as e:

            await interaction.edit_original_response(
                content=(
                    "❌ Không lấy được bảng:\n"
                    f"`{shorten(e, 700)}`"
                )
            )

            return

        tables = data.get(
            "standings",
            []
        )

        if not tables:

            await interaction.edit_original_response(
                content=(
                    "❌ Không có bảng xếp hạng."
                )
            )

            return

        table = tables[0]

        rows = table.get(
            "table",
            []
        )

        lines = []

        for row in rows[:20]:

            position = row.get(
                "position",
                "?"
            )

            team = row.get(
                "team",
                {}
            ).get(
                "name",
                "?"
            )

            points = row.get(
                "points",
                0
            )

            played = row.get(
                "playedGames",
                0
            )

            lines.append(
                (
                    f"`{position:>2}` "
                    f"{shorten(team, 25)} "
                    f"• {points}đ "
                    f"• {played} trận"
                )
            )

        embed = discord.Embed(
            title="🏆 CHAMPIONS LEAGUE",
            description=(
                "\n".join(lines)
                if lines
                else "Không có dữ liệu."
            ),
            color=discord.Color.blue()
        )

        await interaction.edit_original_response(
            content=None,
            embed=embed,
            view=None
        )


# =========================================================
# /KHOINGHIEP
# =========================================================

@bot.tree.command(
    name="khoinghiep",
    description="Xem vốn khởi nghiệp",
    guild=GUILD
)
async def khoinghiep(
    interaction: discord.Interaction
):

    balance = get_balance(
        interaction.user.id
    )

    await interaction.response.send_message(
        (
            "💰 **VỐN KHỞI NGHIỆP**\n\n"
            f"Ví của m: **{balance:,} xu**"
        ),
        ephemeral=True
    )


# =========================================================
# /VI
# =========================================================

@bot.tree.command(
    name="vi",
    description="Xem ví xu",
    guild=GUILD
)
async def vi(
    interaction: discord.Interaction
):

    balance = get_balance(
        interaction.user.id
    )

    await interaction.response.send_message(
        (
            f"💰 Ví của "
            f"**{interaction.user.display_name}**\n\n"
            f"💵 **{balance:,} xu**"
        ),
        ephemeral=True
    )


# =========================================================
# /CUOC
# =========================================================

@bot.tree.command(
    name="cuoc",
    description="Xem cược của m",
    guild=GUILD
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

        icon = {
            "pending": "⏳",
            "won": "✅",
            "lost": "❌",
            "void": "↔️"
        }.get(
            row["status"],
            "❔"
        )

        lines.append(
            (
                f"{icon} `#{row['id']}` "
                f"• Match `{row['fixture_id']}` "
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
    guild=GUILD
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
            ) AS amount
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
            f"🎟️ Tổng cược: **{row['total']}**\n"
            f"💸 Tổng xu cược: "
            f"**{row['amount']:,}**\n"
            f"💰 Số dư: "
            f"**{balance:,} xu**"
        ),
        ephemeral=True
    )


# =========================================================
# HEALTH SERVER RAILWAY
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
        f"[WEB] Health server running "
        f"on port {port}"
    )


# =========================================================
# DISCORD READY
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
        f"[BOT] Guild: {GUILD_ID}"
    )

    print(
        "================================"
    )


# =========================================================
# SYNC SLASH COMMANDS
# =========================================================

@bot.event
async def setup_hook():

    print(
        "[SYNC] Đang sync slash commands..."
    )

    try:

        synced = await bot.tree.sync(
            guild=GUILD
        )

        print(
            f"[SYNC] Đã sync "
            f"{len(synced)} command(s)"
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
            "[BOT] Stopped."
        )

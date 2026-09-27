import os
import time
import sqlite3
import asyncio
import math
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
UTC = ZoneInfo("UTC")

# Manchester City - football-data.org
MAN_CITY_ID = 65

CL_CODE = "CL"

if not DISCORD_TOKEN:
    raise RuntimeError(
        "❌ Thiếu DISCORD_TOKEN trong Railway Variables"
    )

if not FOOTBALL_DATA_TOKEN:
    raise RuntimeError(
        "❌ Thiếu FOOTBALL_DATA_TOKEN trong Railway Variables"
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
        (user_id, balance)
    )

    db.commit()


# =========================================================
# API ERROR
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

        self.cache_seconds = 90

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

        # Free = 10 requests/min.
        # Giữ 9 để tránh đụng trần.
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
                        f"{raw[:600]}"
                    )

                try:

                    data = await response.json(
                        content_type=None
                    )

                except Exception:

                    raise FootballDataError(
                        "API trả dữ liệu không hợp lệ."
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
# GENERAL HELPERS
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


def parse_utc(
    utc_string
):

    try:

        return datetime.fromisoformat(
            utc_string.replace(
                "Z",
                "+00:00"
            )
        ).astimezone(
            UTC
        )

    except Exception:

        return None


def format_time(
    utc_string
):

    dt = parse_utc(
        utc_string
    )

    if dt is None:
        return str(utc_string)

    return dt.astimezone(
        TZ
    ).strftime(
        "%d/%m/%Y • %H:%M"
    )


def local_date_from_utc(
    utc_string
):

    dt = parse_utc(
        utc_string
    )

    if dt is None:
        return None

    return dt.astimezone(
        TZ
    ).date()


def get_team(
    match,
    side
):

    return match.get(
        f"{side}Team",
        {}
    )


def team_name(
    match,
    side
):

    return get_team(
        match,
        side
    ).get(
        "name",
        side
    )


def team_logo(
    match,
    side
):

    team = get_team(
        match,
        side
    )

    return (
        team.get("crest")
        or team.get("logo")
    )


def team_id(
    match,
    side
):

    return get_team(
        match,
        side
    ).get(
        "id"
    )


def score_from_match(
    match
):

    score = match.get(
        "score",
        {}
    )

    full = score.get(
        "fullTime",
        {}
    )

    return (
        full.get("home"),
        full.get("away")
    )


def result_for_team(
    match,
    team_id_value
):

    hg, ag = score_from_match(
        match
    )

    if hg is None or ag is None:
        return None

    home_id = team_id(
        match,
        "home"
    )

    if home_id == team_id_value:

        if hg > ag:
            return "W"

        if hg == ag:
            return "D"

        return "L"

    else:

        if ag > hg:
            return "W"

        if ag == hg:
            return "D"

        return "L"


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

    ms = round(
        bot.latency * 1000
    )

    embed = discord.Embed(
        title="🏓 PONG!",
        description=(
            f"Bot đang hoạt động.\n\n"
            f"⚡ Ping: **{ms}ms**"
        ),
        color=discord.Color.green()
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
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

        embed = discord.Embed(
            title="📡 API STATUS",
            description=(
                "🟢 **Football-data.org**\n\n"
                f"📊 Còn lại: **{remaining}** request/phút\n"
                f"⏱️ Reset: **{reset}s**\n"
                "🆓 Free Tier: 10 request/phút"
            ),
            color=discord.Color.green()
        )

        await interaction.edit_original_response(
            content=None,
            embed=embed
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
    description="Mở trung tâm soi bóng đá",
    guild=GUILD
)
async def soi(
    interaction: discord.Interaction
):

    embed = discord.Embed(
        title="╔══ ⚽ SOI BÓNG ĐÁ ══╗",
        description=(
            "Chọn chế độ bên dưới.\n\n"
            "🏆 **CHAMPIONS LEAGUE**\n"
            "Lịch trận • Tỉ số • Form • BXH\n\n"
            "🔵 **FAN MAN CITY**\n"
            "Toàn bộ trận sắp tới mà API có dữ liệu\n"
            "kèm dự đoán tỉ số."
        ),
        color=discord.Color.blue()
    )

    embed.set_footer(
        text="Football-data.org • Giờ hiển thị: Việt Nam"
    )

    await interaction.response.send_message(
        embed=embed,
        view=MainSoiView(),
        ephemeral=True
    )


# =========================================================
# MAIN SOI VIEW
# =========================================================

class MainSoiView(
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

        embed = discord.Embed(
            title="🏆 CHAMPIONS LEAGUE",
            description=(
                "Nhập ngày muốn xem.\n\n"
                "Ví dụ:\n"
                "`2026-09-27`"
            ),
            color=discord.Color.gold()
        )

        await interaction.response.edit_message(
            content=None,
            embed=embed,
            view=CLDateView()
        )

    @discord.ui.button(
        label="🔵 Fan Man City",
        style=discord.ButtonStyle.success
    )
    async def man_city(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await interaction.response.defer(
            ephemeral=True
        )

        await show_mancity_schedule(
            interaction
        )


# =========================================================
# MAN CITY — GET UPCOMING
# =========================================================

async def get_mancity_upcoming():

    data = await football.get(
        f"/teams/{MAN_CITY_ID}/matches",
        {
            "status": "SCHEDULED",
            "limit": 100
        },
        cache=True
    )

    matches = data.get(
        "matches",
        []
    )

    current = datetime.now(
        UTC
    )

    upcoming = []

    for match in matches:

        dt = parse_utc(
            match.get(
                "utcDate",
                ""
            )
        )

        if dt and dt > current:

            upcoming.append(
                match
            )

    upcoming.sort(
        key=lambda x: x.get(
            "utcDate",
            ""
        )
    )

    return upcoming


# =========================================================
# FAN MAN CITY
# =========================================================

async def show_mancity_schedule(
    interaction
):

    try:

        matches = await get_mancity_upcoming()

    except FootballDataError as e:

        await interaction.edit_original_response(
            content=(
                "❌ **Không lấy được lịch Man City**\n\n"
                f"`{shorten(e, 1000)}`"
            ),
            embed=None,
            view=None
        )

        return

    if not matches:

        await interaction.edit_original_response(
            content=(
                "🔵 **MAN CITY**\n\n"
                "API hiện không trả về trận "
                "sắp tới nào."
            ),
            embed=None,
            view=None
        )

        return

    matches = matches[:25]

    embed = discord.Embed(
        title="╔══ 🔵 MAN CITY ══╗",
        description=(
            "🔥 **FAN MODE**\n\n"
            f"📅 **{len(matches)}** trận sắp tới\n"
            "🕐 Giờ Việt Nam\n"
            "📡 Dữ liệu trực tiếp từ API"
        ),
        color=discord.Color.blue()
    )

    embed.set_thumbnail(
        url="https://crests.football-data.org/65.png"
    )

    for i, match in enumerate(
        matches,
        start=1
    ):

        home = team_name(
            match,
            "home"
        )

        away = team_name(
            match,
            "away"
        )

        competition = match.get(
            "competition",
            {}
        )

        league = competition.get(
            "name",
            "Không rõ giải"
        )

        utc_date = match.get(
            "utcDate",
            ""
        )

        venue = match.get(
            "venue"
        )

        value = (
            f"🕐 **{format_time(utc_date)}**\n"
            f"🏆 {shorten(league, 60)}\n"
        )

        if venue:

            value += (
                f"🏟️ {shorten(venue, 55)}\n"
            )

        value += (
            f"📍 {'Nhà' if team_id(match, 'home') == MAN_CITY_ID else 'Khách'}"
        )

        embed.add_field(
            name=(
                f"#{i}  ⚽ {home}  🆚  {away}"
            ),
            value=value,
            inline=False
        )

    embed.set_footer(
        text=(
            "🔵 Manchester City • "
            "Chọn trận bên dưới để xem chi tiết"
        )
    )

    await interaction.edit_original_response(
        content=None,
        embed=embed,
        view=ManCityScheduleView(
            matches
        )
    )


# =========================================================
# MAN CITY SELECT
# =========================================================

class ManCityScheduleView(
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
            ManCitySelect(
                matches
            )
        )


class ManCitySelect(
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

            league = (
                match.get(
                    "competition",
                    {}
                ).get(
                    "name",
                    "Football"
                )
            )

            options.append(
                discord.SelectOption(
                    label=shorten(
                        f"{home} vs {away}",
                        100
                    ),
                    description=shorten(
                        league,
                        100
                    ),
                    value=str(
                        match_id
                    )
                )
            )

        super().__init__(
            placeholder="🔵 Chọn trận Man City...",
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

        selected = next(
            (
                m
                for m in self.matches
                if m.get("id") == selected_id
            ),
            None
        )

        if selected is None:

            await interaction.edit_original_response(
                content="❌ Không tìm thấy trận."
            )

            return

        await show_mancity_match(
            interaction,
            selected
        )


# =========================================================
# MAN CITY MATCH DETAIL
# =========================================================

async def show_mancity_match(
    interaction,
    match
):

    home = team_name(
        match,
        "home"
    )

    away = team_name(
        match,
        "away"
    )

    home_logo = team_logo(
        match,
        "home"
    )

    away_logo = team_logo(
        match,
        "away"
    )

    league = (
        match.get(
            "competition",
            {}
        ).get(
            "name",
            "Football"
        )
    )

    competition_logo = (
        match.get(
            "competition",
            {}
        ).get(
            "emblem"
        )
    )

    utc_date = match.get(
        "utcDate",
        ""
    )

    venue = match.get(
        "venue"
    )

    stage = match.get(
        "stage"
    )

    matchday = match.get(
        "matchday"
    )

    city_home = (
        team_id(match, "home")
        == MAN_CITY_ID
    )

    location = (
        "🏠 Man City đá sân nhà"
        if city_home
        else
        "✈️ Man City đá sân khách"
    )

    embed = discord.Embed(
        title="╔══ 🔵 MATCH CENTER ══╗",
        description=(
            f"🏆 **{league}**\n\n"
            f"⚽ **{home}**\n"
            "        🆚\n"
            f"**{away}**\n\n"
            f"🕐 **{format_time(utc_date)}**\n"
            f"{location}"
        ),
        color=discord.Color.blue()
    )

    if home_logo:

        embed.add_field(
            name="🏠 HOME",
            value=home,
            inline=True
        )

    if away_logo:

        embed.add_field(
            name="✈️ AWAY",
            value=away,
            inline=True
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

    if stage:

        embed.add_field(
            name="🏆 Vòng",
            value=stage,
            inline=True
        )

    if matchday:

        embed.add_field(
            name="📅 Matchday",
            value=str(matchday),
            inline=True
        )

    if competition_logo:

        embed.set_thumbnail(
            url=competition_logo
        )

    embed.add_field(
        name="🆔 Match ID",
        value=f"`{match.get('id')}`",
        inline=False
    )

    embed.set_footer(
        text=(
            "🔵 Fan Man City • "
            "Dữ liệu thật"
        )
    )

    await interaction.edit_original_response(
        content=None,
        embed=embed,
        view=ManCityDetailView(
            match
        )
    )


# =========================================================
# MAN CITY DETAIL VIEW
# =========================================================

class ManCityDetailView(
    discord.ui.View
):

    def __init__(
        self,
        match
    ):

        super().__init__(
            timeout=300
        )

        self.match = match

    @discord.ui.button(
        label="🤖 Dự đoán",
        style=discord.ButtonStyle.success
    )
    async def prediction(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await interaction.response.defer(
            ephemeral=True
        )

        await make_prediction(
            interaction,
            self.match
        )

    @discord.ui.button(
        label="📈 Phong độ",
        style=discord.ButtonStyle.primary
    )
    async def form(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await interaction.response.defer(
            ephemeral=True
        )

        await show_mancity_form(
            interaction
        )


# =========================================================
# GET TEAM RECENT MATCHES
# =========================================================

async def get_team_recent(
    team_id_value,
    limit=5
):

    data = await football.get(
        f"/teams/{team_id_value}/matches",
        {
            "status": "FINISHED",
            "limit": limit
        },
        cache=True
    )

    return data.get(
        "matches",
        []
    )


# =========================================================
# TEAM STATS
# =========================================================

def calculate_team_stats(
    matches,
    team_id_value
):

    wins = 0
    draws = 0
    losses = 0

    scored = 0
    conceded = 0

    valid = 0

    for match in matches:

        hg, ag = score_from_match(
            match
        )

        if hg is None or ag is None:
            continue

        home_id = team_id(
            match,
            "home"
        )

        if home_id == team_id_value:

            gf = hg
            ga = ag

        else:

            gf = ag
            ga = hg

        scored += gf
        conceded += ga

        valid += 1

        if gf > ga:
            wins += 1

        elif gf == ga:
            draws += 1

        else:
            losses += 1

    if valid == 0:

        return {
            "games": 0,
            "wins": 0,
            "draws": 0,
            "losses": 0,
            "gf": 0,
            "ga": 0,
            "avg_gf": 1.2,
            "avg_ga": 1.2
        }

    return {
        "games": valid,
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "gf": scored,
        "ga": conceded,
        "avg_gf": scored / valid,
        "avg_ga": conceded / valid
    }


# =========================================================
# PREDICTION ENGINE
# =========================================================

def poisson_probability(
    lam,
    goals
):

    try:

        return (
            math.exp(-lam)
            * (lam ** goals)
            / math.factorial(goals)
        )

    except Exception:

        return 0.0


def predict_score(
    home_stats,
    away_stats
):

    # Mô hình đơn giản:
    # - sức tấn công gần đây
    # - khả năng phòng ngự gần đây
    # - không tuyên bố đây là xác suất chính thức.

    home_attack = home_stats["avg_gf"]
    away_attack = away_stats["avg_gf"]

    home_defense = home_stats["avg_ga"]
    away_defense = away_stats["avg_ga"]

    expected_home = (
        home_attack * 0.60
        + away_defense * 0.40
    )

    expected_away = (
        away_attack * 0.60
        + home_defense * 0.40
    )

    # Lợi thế sân nhà nhẹ.
    expected_home *= 1.08

    expected_home = max(
        0.25,
        min(
            expected_home,
            4.5
        )
    )

    expected_away = max(
        0.20,
        min(
            expected_away,
            4.0
        )
    )

    outcomes = {
        "home": 0.0,
        "draw": 0.0,
        "away": 0.0
    }

    best_score = (
        1,
        1
    )

    best_score_probability = -1

    for hg in range(0, 7):

        for ag in range(0, 7):

            p_home = poisson_probability(
                expected_home,
                hg
            )

            p_away = poisson_probability(
                expected_away,
                ag
            )

            probability = (
                p_home * p_away
            )

            if hg > ag:

                outcomes["home"] += probability

            elif hg == ag:

                outcomes["draw"] += probability

            else:

                outcomes["away"] += probability

            if probability > best_score_probability:

                best_score_probability = (
                    probability
                )

                best_score = (
                    hg,
                    ag
                )

    winner = max(
        outcomes,
        key=outcomes.get
    )

    return {
        "home_goals":
            best_score[0],

        "away_goals":
            best_score[1],

        "winner":
            winner,

        "home_prob":
            outcomes["home"],

        "draw_prob":
            outcomes["draw"],

        "away_prob":
            outcomes["away"],

        "expected_home":
            expected_home,

        "expected_away":
            expected_away
    }


# =========================================================
# MAKE PREDICTION
# =========================================================

async def make_prediction(
    interaction,
    match
):

    home_id = team_id(
        match,
        "home"
    )

    away_id = team_id(
        match,
        "away"
    )

    home = team_name(
        match,
        "home"
    )

    away = team_name(
        match,
        "away"
    )

    if not home_id or not away_id:

        await interaction.edit_original_response(
            content="❌ Không lấy được ID đội."
        )

        return

    try:

        home_matches, away_matches = (
            await asyncio.gather(

                get_team_recent(
                    home_id,
                    5
                ),

                get_team_recent(
                    away_id,
                    5
                )
            )
        )

    except FootballDataError as e:

        await interaction.edit_original_response(
            content=(
                "❌ Không lấy được dữ liệu để dự đoán.\n"
                f"`{shorten(e, 700)}`"
            )
        )

        return

    home_stats = calculate_team_stats(
        home_matches,
        home_id
    )

    away_stats = calculate_team_stats(
        away_matches,
        away_id
    )

    prediction = predict_score(
        home_stats,
        away_stats
    )

    winner = prediction["winner"]

    if winner == "home":

        result_text = (
            f"🟢 **{home} thắng**"
        )

    elif winner == "away":

        result_text = (
            f"🔵 **{away} thắng**"
        )

    else:

        result_text = (
            "🟡 **Hòa**"
        )

    score_text = (
        f"**{prediction['home_goals']}"
        f" - "
        f"{prediction['away_goals']}**"
    )

    embed = discord.Embed(
        title="╔══ 🤖 DỰ ĐOÁN TRẬN ══╗",
        description=(
            f"⚽ **{home}**\n"
            "       🆚\n"
            f"**{away}**\n\n"
            f"🎯 Kết quả dự đoán:\n"
            f"{result_text}\n\n"
            f"⚽ Tỉ số dự đoán:\n"
            f"## {score_text}"
        ),
        color=discord.Color.green()
    )

    embed.add_field(
        name="📊 Dữ liệu Home",
        value=(
            f"5 trận: "
            f"**{home_stats['wins']}W "
            f"{home_stats['draws']}D "
            f"{home_stats['losses']}L**\n"
            f"⚽ Ghi: **{home_stats['gf']}**\n"
            f"🥅 Thủng: **{home_stats['ga']}**"
        ),
        inline=True
    )

    embed.add_field(
        name="📊 Dữ liệu Away",
        value=(
            f"5 trận: "
            f"**{away_stats['wins']}W "
            f"{away_stats['draws']}D "
            f"{away_stats['losses']}L**\n"
            f"⚽ Ghi: **{away_stats['gf']}**\n"
            f"🥅 Thủng: **{away_stats['ga']}**"
        ),
        inline=True
    )

    embed.add_field(
        name="🧠 Mô hình",
        value=(
            f"⚽ Expected Home: "
            f"`{prediction['expected_home']:.2f}`\n"
            f"⚽ Expected Away: "
            f"`{prediction['expected_away']:.2f}`"
        ),
        inline=False
    )

    embed.add_field(
        name="📈 Tham khảo mô hình",
        value=(
            f"🏠 Home: "
            f"`{prediction['home_prob'] * 100:.1f}%`\n"
            f"🟡 Hòa: "
            f"`{prediction['draw_prob'] * 100:.1f}%`\n"
            f"✈️ Away: "
            f"`{prediction['away_prob'] * 100:.1f}%`"
        ),
        inline=False
    )

    embed.set_footer(
        text=(
            "⚠️ Đây là dự đoán thống kê của bot, "
            "không phải kết quả chắc chắn."
        )
    )

    await interaction.edit_original_response(
        content=None,
        embed=embed,
        view=None
    )


# =========================================================
# MAN CITY FORM
# =========================================================

async def show_mancity_form(
    interaction
):

    try:

        matches = await get_team_recent(
            MAN_CITY_ID,
            5
        )

    except FootballDataError as e:

        await interaction.edit_original_response(
            content=(
                "❌ Không lấy được phong độ:\n"
                f"`{shorten(e, 700)}`"
            )
        )

        return

    if not matches:

        await interaction.edit_original_response(
            content=(
                "❌ Không có dữ liệu phong độ."
            )
        )

        return

    stats = calculate_team_stats(
        matches,
        MAN_CITY_ID
    )

    lines = []

    form_icons = []

    for match in matches:

        result = result_for_team(
            match,
            MAN_CITY_ID
        )

        if result == "W":
            icon = "🟢 W"

        elif result == "D":
            icon = "🟡 D"

        elif result == "L":
            icon = "🔴 L"

        else:
            icon = "⚪ ?"

        form_icons.append(
            icon
        )

        home = team_name(
            match,
            "home"
        )

        away = team_name(
            match,
            "away"
        )

        hg, ag = score_from_match(
            match
        )

        league = (
            match.get(
                "competition",
                {}
            ).get(
                "name",
                "Football"
            )
        )

        lines.append(
            (
                f"{icon} "
                f"**{home} {hg}-{ag} {away}**\n"
                f"🏆 {shorten(league, 60)}"
            )
        )

    embed = discord.Embed(
        title="╔══ 🔵 MAN CITY FORM ══╗",
        description=(
            " ".join(form_icons)
            + "\n\n"
            + "\n\n".join(lines)
        ),
        color=discord.Color.blue()
    )

    embed.add_field(
        name="📊 Tổng quan",
        value=(
            f"🟢 Thắng: **{stats['wins']}**\n"
            f"🟡 Hòa: **{stats['draws']}**\n"
            f"🔴 Thua: **{stats['losses']}**"
        ),
        inline=True
    )

    embed.add_field(
        name="⚽ Bàn thắng",
        value=(
            f"Ghi: **{stats['gf']}**\n"
            f"Thủng: **{stats['ga']}**"
        ),
        inline=True
    )

    embed.set_thumbnail(
        url="https://crests.football-data.org/65.png"
    )

    await interaction.edit_original_response(
        content=None,
        embed=embed,
        view=None
    )


# =========================================================
# CHAMPIONS LEAGUE DATE
# =========================================================

class CLDateView(
    discord.ui.View
):

    def __init__(self):

        super().__init__(
            timeout=300
        )

    @discord.ui.button(
        label="📅 Nhập ngày",
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
            title="📅 Chọn ngày"
        )

        self.date_input = discord.ui.TextInput(
            label="Ngày trận",
            placeholder="2026-09-27",
            required=True,
            max_length=10
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
                "❌ Dùng định dạng `YYYY-MM-DD`.",
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
# CHAMPIONS LEAGUE MATCHES BY LOCAL DATE
# =========================================================

async def get_matches_for_date(
    interaction,
    date_string
):

    try:

        local_day = datetime.strptime(
            date_string,
            "%Y-%m-%d"
        ).replace(
            tzinfo=TZ
        )

        # Lấy rộng hơn một chút rồi lọc
        # chính xác theo giờ Việt Nam.
        utc_start = (
            local_day
            - timedelta(hours=1)
        ).astimezone(
            UTC
        )

        utc_end = (
            local_day
            + timedelta(days=1)
            + timedelta(hours=1)
        ).astimezone(
            UTC
        )

        data = await football.get(
            "/competitions/CL/matches",
            {
                "dateFrom":
                    utc_start.strftime(
                        "%Y-%m-%d"
                    ),

                "dateTo":
                    utc_end.strftime(
                        "%Y-%m-%d"
                    )
            }
        )

    except FootballDataError as e:

        await interaction.edit_original_response(
            content=(
                "❌ Không lấy được lịch C1:\n"
                f"`{shorten(e, 1000)}`"
            ),
            embed=None,
            view=None
        )

        return

    matches = []

    for match in data.get(
        "matches",
        []
    ):

        local_date = local_date_from_utc(
            match.get(
                "utcDate",
                ""
            )
        )

        if (
            local_date
            and str(local_date)
            == date_string
        ):

            matches.append(
                match
            )

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
        title="╔══ 🏆 CHAMPIONS LEAGUE ══╗",
        description=(
            f"📅 `{date_string}`\n"
            f"⚽ **{len(matches)}** trận"
        ),
        color=discord.Color.gold()
    )

    for i, match in enumerate(
        matches,
        start=1
    ):

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

        hg, ag = score_from_match(
            match
        )

        if hg is not None and ag is not None:

            score = (
                f"⚽ **{hg} - {ag}**"
            )

        else:

            score = "⚽ Chưa đá"

        embed.add_field(
            name=(
                f"#{i}  {home}  🆚  {away}"
            ),
            value=(
                f"🕐 {format_time(match.get('utcDate', ''))}\n"
                f"{score}\n"
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
# CL MATCH SELECT
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

            home = team_name(
                match,
                "home"
            )

            away = team_name(
                match,
                "away"
            )

            options.append(
                discord.SelectOption(
                    label=shorten(
                        f"{home} vs {away}",
                        100
                    ),
                    description=shorten(
                        match.get(
                            "status",
                            "?"
                        ),
                        100
                    ),
                    value=str(
                        match.get(
                            "id"
                        )
                    )
                )
            )

        super().__init__(
            placeholder="⚽ Chọn trận...",
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

        selected = next(
            (
                m
                for m in self.matches
                if m.get("id") == selected_id
            ),
            None
        )

        if selected is None:

            await interaction.edit_original_response(
                content="❌ Không tìm thấy trận."
            )

            return

        await show_cl_analysis(
            interaction,
            selected
        )


# =========================================================
# CL MATCH ANALYSIS
# =========================================================

async def show_cl_analysis(
    interaction,
    match
):

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

    home_logo = team_logo(
        match,
        "home"
    )

    away_logo = team_logo(
        match,
        "away"
    )

    hg, ag = score_from_match(
        match
    )

    if hg is None or ag is None:

        score_text = "⚽ Chưa thi đấu"

    else:

        score_text = (
            f"⚽ **{hg} - {ag}**"
        )

    embed = discord.Embed(
        title="╔══ 🔎 SOI TRẬN ══╗",
        description=(
            "🏆 **UEFA Champions League**\n\n"
            f"⚽ **{home}**\n"
            "        🆚\n"
            f"**{away}**\n\n"
            f"🕐 {format_time(match.get('utcDate', ''))}\n"
            f"📌 `{match.get('status', '?')}`"
        ),
        color=discord.Color.gold()
    )

    if home_logo:

        embed.set_thumbnail(
            url=home_logo
        )

    embed.add_field(
        name="📊 Tỉ số",
        value=score_text,
        inline=False
    )

    if match.get("stage"):

        embed.add_field(
            name="🏆 Vòng",
            value=match.get(
                "stage"
            ),
            inline=True
        )

    if match.get("matchday"):

        embed.add_field(
            name="📅 Matchday",
            value=str(
                match.get(
                    "matchday"
                )
            ),
            inline=True
        )

    if match.get("venue"):

        embed.add_field(
            name="🏟️ Sân",
            value=shorten(
                match.get("venue"),
                100
            ),
            inline=False
        )

    try:

        form_text = await get_form_text(
            home_id,
            away_id
        )

    except Exception:

        form_text = "Không lấy được form."

    embed.add_field(
        name="📈 Phong độ gần đây",
        value=form_text,
        inline=False
    )

    embed.set_footer(
        text="Football-data.org • Dữ liệu thật"
    )

    await interaction.edit_original_response(
        content=None,
        embed=embed,
        view=CLMatchActionView()
    )


# =========================================================
# FORM TEXT
# =========================================================

async def get_form_text(
    home_id,
    away_id
):

    if not home_id or not away_id:

        return "N/A"

    home_matches, away_matches = (
        await asyncio.gather(

            get_team_recent(
                home_id,
                5
            ),

            get_team_recent(
                away_id,
                5
            )
        )
    )

    def form(
        matches,
        tid
    ):

        values = []

        for match in matches:

            result = result_for_team(
                match,
                tid
            )

            if result:
                values.append(
                    result
                )

        return (
            " ".join(values)
            if values
            else "N/A"
        )

    return (
        f"🏠 Home: `{form(home_matches, home_id)}`\n"
        f"✈️ Away: `{form(away_matches, away_id)}`"
    )


# =========================================================
# CL ACTIONS
# =========================================================

class CLMatchActionView(
    discord.ui.View
):

    def __init__(self):

        super().__init__(
            timeout=300
        )

    @discord.ui.button(
        label="📊 BXH",
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
                    "❌ Không lấy được BXH:\n"
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
                content="❌ Không có BXH."
            )

            return

        rows = tables[0].get(
            "table",
            []
        )

        lines = []

        for row in rows[:20]:

            pos = row.get(
                "position",
                "?"
            )

            name = (
                row.get(
                    "team",
                    {}
                ).get(
                    "name",
                    "?"
                )
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
                    f"`{pos:>2}` "
                    f"{shorten(name, 25)} "
                    f"• **{points}đ** "
                    f"• {played} trận"
                )
            )

        embed = discord.Embed(
            title="╔══ 🏆 C1 TABLE ══╗",
            description="\n".join(lines),
            color=discord.Color.gold()
        )

        await interaction.edit_original_response(
            content=None,
            embed=embed,
            view=None
        )


# =========================================================
# WALLET COMMANDS
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

    embed = discord.Embed(
        title="💰 VỐN KHỞI NGHIỆP",
        description=(
            f"Ví hiện tại:\n\n"
            f"💵 **{balance:,} xu**"
        ),
        color=discord.Color.green()
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


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

    embed = discord.Embed(
        title="💰 VÍ CỦA M",
        description=(
            f"💵 **{balance:,} xu**"
        ),
        color=discord.Color.green()
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


@bot.tree.command(
    name="cuoc",
    description="Xem lịch sử cược",
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


@bot.tree.command(
    name="kettoan",
    description="Xem thống kê ví",
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

    embed = discord.Embed(
        title="📊 THỐNG KÊ",
        description=(
            f"🎟️ Tổng cược: **{row['total']}**\n"
            f"💸 Tổng xu cược: **{row['amount']:,}**\n"
            f"💰 Số dư: **{balance:,} xu**"
        ),
        color=discord.Color.blurple()
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# =========================================================
# RAILWAY HEALTH
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
# DISCORD READY
# =========================================================

@bot.event
async def on_ready():

    print(
        "=========================================="
    )

    print(
        f"[BOT] ONLINE: {bot.user}"
    )

    print(
        f"[BOT] Guild: {GUILD_ID}"
    )

    print(
        "=========================================="
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
            f"[SYNC] Đã sync {len(synced)} command(s)"
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

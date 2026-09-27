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
from discord.ext import commands, tasks
from aiohttp import web


# ============================================================
# CONFIG
# ============================================================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
FOOTBALL_DATA_TOKEN = os.getenv("FOOTBALL_DATA_TOKEN")

GUILD_ID = 1551547049600618538

API_BASE = "https://api.football-data.org/v4"

TZ = ZoneInfo("Asia/Ho_Chi_Minh")
UTC = ZoneInfo("UTC")

MAN_CITY_ID = 65
DEFAULT_COMPETITION = "CL"

DB_FILE = "bot.db"

# Alert timings
ALERT_60_MIN = 60
ALERT_15_MIN = 15

# API Free Tier safety
API_REQUEST_LIMIT_PER_MINUTE = 9


if not DISCORD_TOKEN:
    raise RuntimeError(
        "❌ Thiếu DISCORD_TOKEN trong Railway Variables"
    )

if not FOOTBALL_DATA_TOKEN:
    raise RuntimeError(
        "❌ Thiếu FOOTBALL_DATA_TOKEN trong Railway Variables"
    )


# ============================================================
# DATABASE
# ============================================================

db = sqlite3.connect(
    DB_FILE,
    check_same_thread=False
)

db.row_factory = sqlite3.Row


# Wallet
db.execute("""
CREATE TABLE IF NOT EXISTS wallets (
    user_id INTEGER PRIMARY KEY,
    balance INTEGER NOT NULL DEFAULT 10000
)
""")


# Bets
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


# Notification settings
db.execute("""
CREATE TABLE IF NOT EXISTS notification_settings (
    guild_id INTEGER PRIMARY KEY,

    channel_id INTEGER,

    man_city_enabled INTEGER NOT NULL DEFAULT 1,

    competition_enabled INTEGER NOT NULL DEFAULT 1,

    competition_code TEXT NOT NULL DEFAULT 'CL',

    alert_60 INTEGER NOT NULL DEFAULT 1,

    alert_15 INTEGER NOT NULL DEFAULT 1,

    alert_start INTEGER NOT NULL DEFAULT 1,

    alert_finished INTEGER NOT NULL DEFAULT 1
)
""")


# Sent alerts
db.execute("""
CREATE TABLE IF NOT EXISTS sent_alerts (
    alert_key TEXT PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    match_id INTEGER NOT NULL,
    alert_type TEXT NOT NULL,
    sent_at TEXT NOT NULL
)
""")


db.commit()


# ============================================================
# DATABASE HELPERS
# ============================================================

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


def set_balance(user_id: int, balance: int):

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


def get_notification_settings(guild_id: int):

    row = db.execute(
        """
        SELECT *
        FROM notification_settings
        WHERE guild_id = ?
        """,
        (guild_id,)
    ).fetchone()

    if row is None:

        db.execute(
            """
            INSERT INTO notification_settings(
                guild_id,
                competition_code
            )
            VALUES (?, ?)
            """,
            (guild_id, DEFAULT_COMPETITION)
        )

        db.commit()

        row = db.execute(
            """
            SELECT *
            FROM notification_settings
            WHERE guild_id = ?
            """,
            (guild_id,)
        ).fetchone()

    return row


def update_notification_setting(
    guild_id: int,
    field: str,
    value
):

    allowed = {
        "channel_id",
        "man_city_enabled",
        "competition_enabled",
        "competition_code",
        "alert_60",
        "alert_15",
        "alert_start",
        "alert_finished"
    }

    if field not in allowed:
        return

    get_notification_settings(guild_id)

    db.execute(
        f"""
        UPDATE notification_settings
        SET {field} = ?
        WHERE guild_id = ?
        """,
        (value, guild_id)
    )

    db.commit()


def alert_already_sent(
    guild_id: int,
    match_id: int,
    alert_type: str
):

    key = f"{guild_id}:{match_id}:{alert_type}"

    row = db.execute(
        """
        SELECT alert_key
        FROM sent_alerts
        WHERE alert_key = ?
        """,
        (key,)
    ).fetchone()

    return row is not None


def mark_alert_sent(
    guild_id: int,
    match_id: int,
    alert_type: str
):

    key = f"{guild_id}:{match_id}:{alert_type}"

    db.execute(
        """
        INSERT OR IGNORE INTO sent_alerts(
            alert_key,
            guild_id,
            match_id,
            alert_type,
            sent_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            key,
            guild_id,
            match_id,
            alert_type,
            datetime.now(UTC).isoformat()
        )
    )

    db.commit()


# ============================================================
# API
# ============================================================

class FootballDataError(Exception):
    pass


class FootballDataAPI:

    def __init__(self):

        self.session: Optional[aiohttp.ClientSession] = None

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
                    "X-Auth-Token": FOOTBALL_DATA_TOKEN,
                    "Accept": "application/json"
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

        if len(self.request_times) >= API_REQUEST_LIMIT_PER_MINUTE:

            wait_time = (
                60
                - (now - self.request_times[0])
                + 0.5
            )

            if wait_time > 0:

                await asyncio.sleep(wait_time)

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
                for k, v in sorted(params.items())
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

                self.remaining = response.headers.get(
                    "X-Requests-Available-Minute"
                )

                self.reset_seconds = response.headers.get(
                    "X-RequestCounter-Reset"
                )

                if response.status == 429:

                    raise FootballDataError(
                        "Đã vượt giới hạn API 10 request/phút."
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


# ============================================================
# HELPERS
# ============================================================

def now_vn():

    return datetime.now(TZ)


def shorten(text, max_len=100):

    text = str(text)

    if len(text) <= max_len:
        return text

    return text[:max_len - 3] + "..."


def parse_utc(value):

    try:

        return datetime.fromisoformat(
            value.replace(
                "Z",
                "+00:00"
            )
        ).astimezone(UTC)

    except Exception:

        return None


def format_time(value):

    dt = parse_utc(value)

    if dt is None:
        return str(value)

    return dt.astimezone(TZ).strftime(
        "%d/%m/%Y • %H:%M"
    )


def minutes_until(value):

    dt = parse_utc(value)

    if dt is None:
        return None

    return (
        dt - datetime.now(UTC)
    ).total_seconds() / 60


def get_team(match, side):

    return match.get(
        f"{side}Team",
        {}
    )


def team_name(match, side):

    return get_team(
        match,
        side
    ).get(
        "name",
        side
    )


def team_logo(match, side):

    team = get_team(
        match,
        side
    )

    return (
        team.get("crest")
        or team.get("logo")
    )


def team_id(match, side):

    return get_team(
        match,
        side
    ).get("id")


def score_from_match(match):

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


# ============================================================
# BOT
# ============================================================

intents = discord.Intents.default()

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)

GUILD = discord.Object(
    id=GUILD_ID
)


# ============================================================
# /PING
# ============================================================

@bot.tree.command(
    name="ping",
    description="Kiểm tra bot",
    guild=GUILD
)
async def ping(interaction):

    ms = round(
        bot.latency * 1000
    )

    embed = discord.Embed(
        title="🏓 PONG!",
        description=(
            "🟢 Bot đang hoạt động.\n\n"
            f"⚡ Ping: **{ms}ms**"
        ),
        color=discord.Color.green()
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# ============================================================
# /APIQUOTA
# ============================================================

@bot.tree.command(
    name="apiquota",
    description="Xem quota API bóng đá",
    guild=GUILD
)
async def apiquota(interaction):

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
                "🛡️ Bot tự giới hạn request để tránh spam."
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


# ============================================================
# /SOI
# ============================================================

@bot.tree.command(
    name="soi",
    description="Mở trung tâm soi bóng đá",
    guild=GUILD
)
async def soi(interaction):

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
        text="Football-data.org • Giờ Việt Nam"
    )

    await interaction.response.send_message(
        embed=embed,
        view=MainSoiView(),
        ephemeral=True
    )


# ============================================================
# SOI MAIN VIEW
# ============================================================

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
        interaction,
        button
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
        interaction,
        button
    ):

        await interaction.response.defer(
            ephemeral=True
        )

        await show_mancity_schedule(
            interaction
        )


# ============================================================
# MAN CITY UPCOMING
# ============================================================

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


async def show_mancity_schedule(
    interaction
):

    try:

        matches = (
            await get_mancity_upcoming()
        )

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
                "API hiện không trả về trận sắp tới."
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
            "🌍 Hiển thị các giải mà API trả về"
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

        value = (
            f"🕐 **{format_time(match.get('utcDate', ''))}**\n"
            f"🏆 {shorten(league, 60)}\n"
        )

        venue = match.get(
            "venue"
        )

        if venue:

            value += (
                f"🏟️ {shorten(venue, 55)}\n"
            )

        value += (
            f"📍 "
            f"{'Nhà' if team_id(match, 'home') == MAN_CITY_ID else 'Khách'}"
        )

        embed.add_field(
            name=(
                f"#{i} ⚽ {home} 🆚 {away}"
            ),
            value=value,
            inline=False
        )

    embed.set_footer(
        text=(
            "🔵 Manchester City • "
            "Chọn trận để xem chi tiết"
        )
    )

    await interaction.edit_original_response(
        content=None,
        embed=embed,
        view=ManCityScheduleView(
            matches
        )
    )


# ============================================================
# MAN CITY SELECT
# ============================================================

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
            ManCitySelect(matches)
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

            home = team_name(
                match,
                "home"
            )

            away = team_name(
                match,
                "away"
            )

            league = match.get(
                "competition",
                {}
            ).get(
                "name",
                "Football"
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
                        match.get("id")
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
        interaction
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


# ============================================================
# MAN CITY MATCH
# ============================================================

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

    league = match.get(
        "competition",
        {}
    ).get(
        "name",
        "Football"
    )

    competition_logo = match.get(
        "competition",
        {}
    ).get(
        "emblem"
    )

    city_home = (
        team_id(
            match,
            "home"
        )
        == MAN_CITY_ID
    )

    location = (
        "🏠 Man City đá sân nhà"
        if city_home
        else "✈️ Man City đá sân khách"
    )

    embed = discord.Embed(
        title="╔══ 🔵 MATCH CENTER ══╗",
        description=(
            f"🏆 **{league}**\n\n"
            f"⚽ **{home}**\n"
            "        🆚\n"
            f"**{away}**\n\n"
            f"🕐 **{format_time(match.get('utcDate', ''))}**\n"
            f"{location}"
        ),
        color=discord.Color.blue()
    )

    if competition_logo:

        embed.set_thumbnail(
            url=competition_logo
        )

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

    stage = match.get(
        "stage"
    )

    if stage:

        embed.add_field(
            name="🏆 Vòng",
            value=stage,
            inline=True
        )

    matchday = match.get(
        "matchday"
    )

    if matchday:

        embed.add_field(
            name="📅 Matchday",
            value=str(matchday),
            inline=True
        )

    embed.add_field(
        name="🆔 Match ID",
        value=f"`{match.get('id')}`",
        inline=False
    )

    embed.set_footer(
        text="🔵 Fan Man City • Dữ liệu API"
    )

    await interaction.edit_original_response(
        content=None,
        embed=embed,
        view=ManCityDetailView(match)
    )


# ============================================================
# TEAM RECENT
# ============================================================

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


# ============================================================
# PREDICTION
# ============================================================

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

    # Small home advantage
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

    best_probability = -1

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

            if probability > best_probability:

                best_probability = probability

                best_score = (
                    hg,
                    ag
                )

    winner = max(
        outcomes,
        key=outcomes.get
    )

    return {
        "home_goals": best_score[0],
        "away_goals": best_score[1],
        "winner": winner,
        "home_prob": outcomes["home"],
        "draw_prob": outcomes["draw"],
        "away_prob": outcomes["away"],
        "expected_home": expected_home,
        "expected_away": expected_away
    }


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

    try:

        home_matches, away_matches = await asyncio.gather(

            get_team_recent(
                home_id,
                5
            ),

            get_team_recent(
                away_id,
                5
            )
        )

    except FootballDataError as e:

        await interaction.edit_original_response(
            content=(
                "❌ Không lấy được dữ liệu dự đoán.\n"
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

    embed = discord.Embed(
        title="╔══ 🤖 DỰ ĐOÁN TRẬN ══╗",
        description=(
            f"⚽ **{home}**\n"
            "       🆚\n"
            f"**{away}**\n\n"

            f"🎯 **Kết quả dự đoán**\n"
            f"{result_text}\n\n"

            f"⚽ **Tỉ số dự đoán**\n"
            f"# {prediction['home_goals']} - "
            f"{prediction['away_goals']}"
        ),
        color=discord.Color.green()
    )

    embed.add_field(
        name="📊 HOME",
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
        name="📊 AWAY",
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
        name="📈 Xác suất mô hình",
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
            "⚠️ Đây là mô hình thống kê của bot, "
            "không phải kết quả chắc chắn."
        )
    )

    await interaction.edit_original_response(
        content=None,
        embed=embed,
        view=None
    )


# ============================================================
# MAN CITY FORM
# ============================================================

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
            content="❌ Không có dữ liệu phong độ."
        )

        return

    stats = calculate_team_stats(
        matches,
        MAN_CITY_ID
    )

    form_icons = []

    lines = []

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

        league = match.get(
            "competition",
            {}
        ).get(
            "name",
            "Football"
        )

        lines.append(
            f"{icon} **{home} {hg}-{ag} {away}**\n"
            f"🏆 {shorten(league, 60)}"
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
        interaction,
        button
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
        interaction,
        button
    ):

        await interaction.response.defer(
            ephemeral=True
        )

        await show_mancity_form(
            interaction
        )


# ============================================================
# CHAMPIONS LEAGUE
# ============================================================

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
        interaction,
        button
    ):

        await interaction.response.send_modal(
            DateModal()
        )


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
        interaction
    ):

        date_string = (
            self.date_input.value.strip()
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

        utc_start = (
            local_day
            - timedelta(hours=1)
        ).astimezone(UTC)

        utc_end = (
            local_day
            + timedelta(days=1)
            + timedelta(hours=1)
        ).astimezone(UTC)

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

        dt = parse_utc(
            match.get(
                "utcDate",
                ""
            )
        )

        if not dt:
            continue

        local_date = (
            dt.astimezone(TZ).date()
        )

        if str(local_date) == date_string:

            matches.append(
                match
            )

    if not matches:

        await interaction.edit_original_response(
            content=(
                f"📅 **{date_string}**\n\n"
                "Không có trận Champions League."
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

        if hg is not None:

            score = (
                f"⚽ **{hg} - {ag}**"
            )

        else:

            score = (
                "⚽ Chưa đá"
            )

        embed.add_field(
            name=(
                f"#{i} {home} 🆚 {away}"
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
        view=FixtureView(matches)
    )


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
            FixtureSelect(matches)
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
                        match.get("id")
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
        interaction
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
                if m.get("id")
                == selected_id
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

    hg, ag = score_from_match(
        match
    )

    if hg is None:

        score_text = (
            "⚽ Chưa thi đấu"
        )

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

    embed.add_field(
        name="📊 Tỉ số",
        value=score_text,
        inline=False
    )

    if match.get("stage"):

        embed.add_field(
            name="🏆 Vòng",
            value=match.get("stage"),
            inline=True
        )

    if match.get("matchday"):

        embed.add_field(
            name="📅 Matchday",
            value=str(
                match.get("matchday")
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

        form_text = (
            "Không lấy được form."
        )

    embed.add_field(
        name="📈 Phong độ gần đây",
        value=form_text,
        inline=False
    )

    embed.set_footer(
        text="Football-data.org • Dữ liệu API"
    )

    await interaction.edit_original_response(
        content=None,
        embed=embed,
        view=CLMatchActionView()
    )


async def get_form_text(
    home_id,
    away_id
):

    if not home_id or not away_id:

        return "N/A"

    home_matches, away_matches = await asyncio.gather(

        get_team_recent(
            home_id,
            5
        ),

        get_team_recent(
            away_id,
            5
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
        interaction,
        button
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

            name = row.get(
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
                f"`{pos:>2}` "
                f"{shorten(name, 25)} "
                f"• **{points}đ** "
                f"• {played} trận"
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


# ============================================================
# 🔔 NOTIFICATION SYSTEM
# ============================================================

def is_man_city_match(match):

    return (
        team_id(match, "home")
        == MAN_CITY_ID
        or
        team_id(match, "away")
        == MAN_CITY_ID
    )


async def get_matches_for_competition(
    code
):

    data = await football.get(
        f"/competitions/{code}/matches",
        {
            "dateFrom": (
                datetime.now(TZ)
                .date()
                .isoformat()
            ),

            "dateTo": (
                datetime.now(TZ)
                .date()
                + timedelta(days=3)
            ).isoformat()
        },
        cache=True
    )

    return data.get(
        "matches",
        []
    )


async def get_matches_for_alerts():

    matches = []

    # --------------------------------------------------------
    # MAN CITY
    # --------------------------------------------------------

    try:

        city_matches = (
            await get_mancity_upcoming()
        )

        matches.extend(
            city_matches
        )

    except Exception as e:

        print(
            "[ALERT] Man City API error:",
            repr(e)
        )


    # --------------------------------------------------------
    # COMPETITION
    # --------------------------------------------------------

    guilds = db.execute(
        """
        SELECT DISTINCT
            competition_code
        FROM notification_settings
        WHERE competition_enabled = 1
        """
    ).fetchall()

    codes = set()

    for row in guilds:

        code = row["competition_code"]

        if code:

            codes.add(
                code.upper()
            )

    for code in codes:

        try:

            competition_matches = (
                await get_matches_for_competition(
                    code
                )
            )

            matches.extend(
                competition_matches
            )

        except Exception as e:

            print(
                f"[ALERT] Competition {code} error:",
                repr(e)
            )

    # Remove duplicates

    unique = {}

    for match in matches:

        match_id = match.get(
            "id"
        )

        if match_id:

            unique[match_id] = match

    return list(
        unique.values()
    )


def match_is_finished(match):

    return match.get(
        "status"
    ) in {
        "FINISHED",
        "AWARDED"
    }


async def send_match_alert(
    guild,
    channel,
    match,
    alert_type
):

    match_id = match.get(
        "id"
    )

    if not match_id:
        return

    if alert_already_sent(
        guild.id,
        match_id,
        alert_type
    ):

        return

    home = team_name(
        match,
        "home"
    )

    away = team_name(
        match,
        "away"
    )

    league = match.get(
        "competition",
        {}
    ).get(
        "name",
        "Football"
    )

    crest = match.get(
        "competition",
        {}
    ).get(
        "emblem"
    )

    utc_date = match.get(
        "utcDate",
        ""
    )

    # --------------------------------------------------------
    # TEXT
    # --------------------------------------------------------

    if alert_type == "60":

        title = (
            "🔔 TRẬN ĐẤU SẮP BẮT ĐẦU"
        )

        description = (
            f"⏰ **Còn khoảng 60 phút!**\n\n"
            f"⚽ **{home}**\n"
            "        🆚\n"
            f"**{away}**"
        )

    elif alert_type == "15":

        title = (
            "🚨 CÒN 15 PHÚT!"
        )

        description = (
            f"🔥 **{home} 🆚 {away}**\n\n"
            "⏰ Trận đấu sẽ bắt đầu trong khoảng "
            "**15 phút**!"
        )

    elif alert_type == "start":

        title = (
            "🏟️ ĐẾN GIỜ THI ĐẤU!"
        )

        description = (
            f"⚽ **{home} 🆚 {away}**\n\n"
            "🔔 Trận đấu đã tới giờ bắt đầu."
        )

    else:

        title = (
            "🏁 FULL TIME"
        )

        hg, ag = score_from_match(
            match
        )

        if hg is None:

            score_text = (
                "Chưa có tỉ số"
            )

        else:

            score_text = (
                f"**{hg} - {ag}**"
            )

        description = (
            f"⚽ **{home}**\n"
            f"        {score_text}\n"
            f"**{away}**\n\n"
            "🏁 Trận đấu đã kết thúc."
        )

    embed = discord.Embed(
        title=title,
        description=description,
        color=(
            discord.Color.blue()
            if is_man_city_match(match)
            else discord.Color.gold()
        )
    )

    embed.add_field(
        name="🏆 Giải",
        value=league,
        inline=True
    )

    embed.add_field(
        name="🕐 Giờ",
        value=format_time(
            utc_date
        ),
        inline=True
    )

    # --------------------------------------------------------
    # PREDICTION FOR PRE-MATCH
    # --------------------------------------------------------

    if alert_type in {
        "60",
        "15"
    }:

        try:

            home_id = team_id(
                match,
                "home"
            )

            away_id = team_id(
                match,
                "away"
            )

            home_matches, away_matches = await asyncio.gather(

                get_team_recent(
                    home_id,
                    5
                ),

                get_team_recent(
                    away_id,
                    5
                )
            )

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

                winner_text = (
                    f"🟢 {home}"
                )

            elif winner == "away":

                winner_text = (
                    f"🔵 {away}"
                )

            else:

                winner_text = (
                    "🟡 Hòa"
                )

            embed.add_field(
                name="🤖 Bot dự đoán",
                value=(
                    f"🎯 **{winner_text}**\n"
                    f"⚽ **"
                    f"{prediction['home_goals']}"
                    f" - "
                    f"{prediction['away_goals']}"
                    f"**\n\n"
                    f"🏠 `{prediction['home_prob'] * 100:.1f}%`\n"
                    f"🟡 `{prediction['draw_prob'] * 100:.1f}%`\n"
                    f"✈️ `{prediction['away_prob'] * 100:.1f}%`"
                ),
                inline=False
            )

        except Exception as e:

            print(
                "[ALERT] Prediction error:",
                repr(e)
            )

    if crest:

        embed.set_thumbnail(
            url=crest
        )

    embed.set_footer(
        text=(
            "⚽ Football Alert • "
            "Dữ liệu Football-data.org"
        )
    )

    try:

        await channel.send(
            embed=embed
        )

        mark_alert_sent(
            guild.id,
            match_id,
            alert_type
        )

        print(
            f"[ALERT] Sent {alert_type}: "
            f"{home} vs {away}"
        )

    except discord.Forbidden:

        print(
            f"[ALERT] Không có quyền gửi "
            f"ở guild {guild.id}"
        )

    except discord.HTTPException as e:

        print(
            "[ALERT] Discord error:",
            repr(e)
        )


async def process_alerts():

    matches = await get_matches_for_alerts()

    now = datetime.now(
        UTC
    )

    for guild in bot.guilds:

        settings = get_notification_settings(
            guild.id
        )

        channel_id = settings["channel_id"]

        if not channel_id:
            continue

        channel = guild.get_channel(
            int(channel_id)
        )

        if channel is None:
            continue

        for match in matches:

            match_id = match.get(
                "id"
            )

            if not match_id:
                continue

            # ------------------------------------------------
            # WHICH ALERT GROUP?
            # ------------------------------------------------

            city_match = is_man_city_match(
                match
            )

            competition_code = match.get(
                "competition",
                {}
            ).get(
                "code"
            )

            city_enabled = (
                bool(settings["man_city_enabled"])
                and city_match
            )

            competition_enabled = (
                bool(settings["competition_enabled"])
                and competition_code
                and competition_code.upper()
                == str(
                    settings["competition_code"]
                ).upper()
            )

            if not (
                city_enabled
                or competition_enabled
            ):

                continue

            # ------------------------------------------------
            # FINISHED
            # ------------------------------------------------

            if (
                match_is_finished(match)
                and settings["alert_finished"]
            ):

                await send_match_alert(
                    guild,
                    channel,
                    match,
                    "finished"
                )

                continue

            # ------------------------------------------------
            # UPCOMING
            # ------------------------------------------------

            dt = parse_utc(
                match.get(
                    "utcDate",
                    ""
                )
            )

            if not dt:
                continue

            diff = (
                dt - now
            ).total_seconds() / 60

            # 60 MIN
            if (
                settings["alert_60"]
                and 0 <= diff <= 60
            ):

                await send_match_alert(
                    guild,
                    channel,
                    match,
                    "60"
                )

            # 15 MIN
            if (
                settings["alert_15"]
                and 0 <= diff <= 15
            ):

                await send_match_alert(
                    guild,
                    channel,
                    match,
                    "15"
                )

            # START
            if (
                settings["alert_start"]
                and -10 <= diff <= 0
            ):

                await send_match_alert(
                    guild,
                    channel,
                    match,
                    "start"
                )


@tasks.loop(
    seconds=60
)
async def football_alert_loop():

    try:

        await process_alerts()

    except Exception as e:

        print(
            "[ALERT LOOP ERROR]",
            repr(e)
        )


@football_alert_loop.before_loop
async def before_alert_loop():

    await bot.wait_until_ready()


# ============================================================
# 🔔 NOTIFICATION UI
# ============================================================

class NotificationView(
    discord.ui.View
):

    def __init__(
        self,
        guild_id
    ):

        super().__init__(
            timeout=300
        )

        self.guild_id = guild_id


    @discord.ui.button(
        label="📢 Chọn Channel",
        style=discord.ButtonStyle.primary
    )
    async def channel_button(
        self,
        interaction,
        button
    ):

        if not interaction.user.guild_permissions.manage_guild:

            await interaction.response.send_message(
                "❌ Cần quyền **Manage Server**.",
                ephemeral=True
            )

            return

        await interaction.response.send_modal(
            ChannelModal()
        )


    @discord.ui.button(
        label="🔵 Man City",
        style=discord.ButtonStyle.success
    )
    async def city_button(
        self,
        interaction,
        button
    ):

        if not interaction.user.guild_permissions.manage_guild:

            await interaction.response.send_message(
                "❌ Cần quyền **Manage Server**.",
                ephemeral=True
            )

            return

        settings = get_notification_settings(
            self.guild_id
        )

        new_value = (
            0
            if settings["man_city_enabled"]
            else 1
        )

        update_notification_setting(
            self.guild_id,
            "man_city_enabled",
            new_value
        )

        await refresh_notification_panel(
            interaction
        )


    @discord.ui.button(
        label="🏆 Bật/Tắt Giải",
        style=discord.ButtonStyle.secondary
    )
    async def competition_button(
        self,
        interaction,
        button
    ):

        if not interaction.user.guild_permissions.manage_guild:

            await interaction.response.send_message(
                "❌ Cần quyền **Manage Server**.",
                ephemeral=True
            )

            return

        settings = get_notification_settings(
            self.guild_id
        )

        new_value = (
            0
            if settings["competition_enabled"]
            else 1
        )

        update_notification_setting(
            self.guild_id,
            "competition_enabled",
            new_value
        )

        await refresh_notification_panel(
            interaction
        )


    @discord.ui.button(
        label="⚙️ Chọn giải",
        style=discord.ButtonStyle.secondary
    )
    async def competition_code(
        self,
        interaction,
        button
    ):

        if not interaction.user.guild_permissions.manage_guild:

            await interaction.response.send_message(
                "❌ Cần quyền **Manage Server**.",
                ephemeral=True
            )

            return

        await interaction.response.send_modal(
            CompetitionModal()
        )


    @discord.ui.button(
        label="🧪 Test",
        style=discord.ButtonStyle.danger
    )
    async def test_button(
        self,
        interaction,
        button
    ):

        if not interaction.user.guild_permissions.manage_guild:

            await interaction.response.send_message(
                "❌ Cần quyền **Manage Server**.",
                ephemeral=True
            )

            return

        await interaction.response.send_message(
            "🧪 Đang gửi tin test...",
            ephemeral=True
        )

        channel_id = get_notification_settings(
            self.guild_id
        )["channel_id"]

        if not channel_id:

            await interaction.followup.send(
                "❌ Chưa chọn channel.",
                ephemeral=True
            )

            return

        channel = interaction.guild.get_channel(
            int(channel_id)
        )

        if not channel:

            await interaction.followup.send(
                "❌ Không tìm thấy channel.",
                ephemeral=True
            )

            return

        embed = discord.Embed(
            title="🧪 FOOTBALL ALERT TEST",
            description=(
                "✅ Hệ thống thông báo đang hoạt động!\n\n"
                "🔵 Man City: ON/OFF theo cài đặt\n"
                "🏆 Giải đấu: ON/OFF theo cài đặt\n"
                "⏰ Alert engine: ONLINE"
            ),
            color=discord.Color.green()
        )

        embed.set_footer(
            text="Football Alert PRO"
        )

        await channel.send(
            embed=embed
        )

        await interaction.followup.send(
            f"✅ Đã gửi test vào {channel.mention}.",
            ephemeral=True
        )


class ChannelModal(
    discord.ui.Modal
):

    def __init__(self):

        super().__init__(
            title="📢 Chọn Channel thông báo"
        )

        self.channel_input = discord.ui.TextInput(
            label="Channel ID",
            placeholder="Ví dụ: 123456789012345678",
            required=True,
            max_length=25
        )

        self.add_item(
            self.channel_input
        )


    async def on_submit(
        self,
        interaction
    ):

        try:

            channel_id = int(
                self.channel_input.value.strip()
            )

        except ValueError:

            await interaction.response.send_message(
                "❌ Channel ID không hợp lệ.",
                ephemeral=True
            )

            return

        channel = interaction.guild.get_channel(
            channel_id
        )

        if channel is None:

            await interaction.response.send_message(
                "❌ Bot không tìm thấy channel này.",
                ephemeral=True
            )

            return

        update_notification_setting(
            interaction.guild.id,
            "channel_id",
            channel_id
        )

        await interaction.response.send_message(
            f"✅ Đã chọn {channel.mention} làm channel thông báo.",
            ephemeral=True
        )


class CompetitionModal(
    discord.ui.Modal
):

    def __init__(self):

        super().__init__(
            title="🏆 Chọn giải đấu"
        )

        self.code_input = discord.ui.TextInput(
            label="Competition Code",
            placeholder="CL / PL / BL1 / SA / PD ...",
            required=True,
            max_length=10
        )

        self.add_item(
            self.code_input
        )


    async def on_submit(
        self,
        interaction
    ):

        code = (
            self.code_input.value.strip()
            .upper()
        )

        if not code.isalnum():

            await interaction.response.send_message(
                "❌ Mã giải không hợp lệ.",
                ephemeral=True
            )

            return

        update_notification_setting(
            interaction.guild.id,
            "competition_code",
            code
        )

        update_notification_setting(
            interaction.guild.id,
            "competition_enabled",
            1
        )

        await interaction.response.send_message(
            (
                f"✅ Đã chọn giải **{code}**.\n"
                "🔔 Bot sẽ theo dõi các trận của giải này "
                "mà API cung cấp."
            ),
            ephemeral=True
        )


async def refresh_notification_panel(
    interaction
):

    settings = get_notification_settings(
        interaction.guild.id
    )

    channel_id = settings["channel_id"]

    if channel_id:

        channel = interaction.guild.get_channel(
            int(channel_id)
        )

        channel_text = (
            channel.mention
            if channel
            else f"`{channel_id}`"
        )

    else:

        channel_text = (
            "❌ Chưa chọn"
        )

    embed = discord.Embed(
        title="╔══ 🔔 FOOTBALL ALERT PRO ══╗",
        description=(
            "Bot sẽ tự động kiểm tra lịch trận "
            "và gửi thông báo.\n\n"

            f"📢 **Channel:** {channel_text}\n\n"

            f"🔵 **Man City:** "
            f"{'🟢 ON' if settings['man_city_enabled'] else '🔴 OFF'}\n"

            f"🏆 **Giải {settings['competition_code']}:** "
            f"{'🟢 ON' if settings['competition_enabled'] else '🔴 OFF'}\n\n"

            "⏰ **60 phút:** "
            f"{'🟢' if settings['alert_60'] else '🔴'}\n"

            "⏰ **15 phút:** "
            f"{'🟢' if settings['alert_15'] else '🔴'}\n"

            "🏟️ **Đến giờ:** "
            f"{'🟢' if settings['alert_start'] else '🔴'}\n"

            "🏁 **Full Time:** "
            f"{'🟢' if settings['alert_finished'] else '🔴'}"
        ),
        color=discord.Color.blurple()
    )

    embed.set_footer(
        text="⚙️ Cần quyền Manage Server để thay đổi cài đặt"
    )

    await interaction.response.edit_message(
        content=None,
        embed=embed,
        view=NotificationView(
            interaction.guild.id
        )
    )


# ============================================================
# /THONGBAO
# ============================================================

@bot.tree.command(
    name="thongbao",
    description="Cài đặt thông báo bóng đá tự động",
    guild=GUILD
)
async def thongbao(
    interaction
):

    settings = get_notification_settings(
        interaction.guild.id
    )

    channel_id = settings["channel_id"]

    if channel_id:

        channel = interaction.guild.get_channel(
            int(channel_id)
        )

        channel_text = (
            channel.mention
            if channel
            else f"`{channel_id}`"
        )

    else:

        channel_text = (
            "❌ Chưa chọn"
        )

    embed = discord.Embed(
        title="╔══ 🔔 FOOTBALL ALERT PRO ══╗",
        description=(
            "🔔 **TỰ ĐỘNG THÔNG BÁO TRẬN ĐẤU**\n\n"

            f"📢 Channel: {channel_text}\n\n"

            f"🔵 Man City: "
            f"{'🟢 ON' if settings['man_city_enabled'] else '🔴 OFF'}\n"

            f"🏆 Giải **{settings['competition_code']}**: "
            f"{'🟢 ON' if settings['competition_enabled'] else '🔴 OFF'}\n\n"

            "⏰ 60 phút trước\n"
            "⏰ 15 phút trước\n"
            "🏟️ Đến giờ thi đấu\n"
            "🏁 Full Time\n\n"

            "🤖 Alert còn có thể kèm "
            "**dự đoán tỉ số + W/D/L**."
        ),
        color=discord.Color.blurple()
    )

    embed.set_footer(
        text="Football Alert PRO"
    )

    await interaction.response.send_message(
        embed=embed,
        view=NotificationView(
            interaction.guild.id
        ),
        ephemeral=True
    )


# ============================================================
# WALLET
# ============================================================

@bot.tree.command(
    name="khoinghiep",
    description="Xem vốn khởi nghiệp",
    guild=GUILD
)
async def khoinghiep(
    interaction
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
    interaction
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
    description="Xem lịch sử cược ảo",
    guild=GUILD
)
async def cuoc(
    interaction
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
            "📭 M chưa có cược ảo nào.",
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
            f"{icon} `#{row['id']}` "
            f"• Match `{row['fixture_id']}` "
            f"• `{row['choice']}` "
            f"• {row['amount']:,} xu"
        )

    await interaction.response.send_message(
        "🎟️ **CƯỢC ẢO CỦA M**\n\n"
        + "\n".join(lines),
        ephemeral=True
    )


@bot.tree.command(
    name="kettoan",
    description="Xem thống kê ví",
    guild=GUILD
)
async def kettoan(
    interaction
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
            f"🎟️ Tổng cược: "
            f"**{row['total']}**\n"

            f"💸 Tổng xu cược: "
            f"**{row['amount']:,}**\n"

            f"💰 Số dư: "
            f"**{balance:,} xu**"
        ),
        color=discord.Color.blurple()
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# ============================================================
# RAILWAY HEALTH
# ============================================================

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
            "time": now_vn().isoformat(),
            "alerts": (
                "running"
                if football_alert_loop.is_running()
                else "stopped"
            )
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


# ============================================================
# BOT EVENTS
# ============================================================

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

    if not football_alert_loop.is_running():

        football_alert_loop.start()

        print(
            "[ALERT] Football Alert PRO: STARTED"
        )


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
            "[SYNC ERROR]",
            repr(e)
        )


# ============================================================
# MAIN
# ============================================================

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

import os
import asyncio
import sqlite3
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord.ext import commands, tasks


# =========================================================
# CONFIG
# =========================================================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
FOOTBALL_DATA_TOKEN = os.getenv("FOOTBALL_DATA_TOKEN")

ALERT_CHANNEL_RAW = os.getenv("FOOTBALL_ALERT_CHANNEL_ID", "").strip()
ALERT_COMPETITION = os.getenv(
    "FOOTBALL_ALERT_COMPETITION", "CL"
).strip().upper()

GUILD_ID = 1551547049600618538

API_BASE = "https://api.football-data.org/v4"

TZ = ZoneInfo("Asia/Ho_Chi_Minh")
UTC = ZoneInfo("UTC")

MAN_CITY_ID = 65

DB_FILE = "bot.db"


if not DISCORD_TOKEN:
    raise RuntimeError("Thiếu DISCORD_TOKEN trong Railway.")

if not FOOTBALL_DATA_TOKEN:
    raise RuntimeError("Thiếu FOOTBALL_DATA_TOKEN trong Railway.")

try:
    ALERT_CHANNEL_ID = int(ALERT_CHANNEL_RAW)
except ValueError:
    raise RuntimeError(
        "FOOTBALL_ALERT_CHANNEL_ID phải là ID channel Discord dạng số."
    )


# =========================================================
# DATABASE
# =========================================================

db = sqlite3.connect(DB_FILE, check_same_thread=False)
db.row_factory = sqlite3.Row

db.execute("""
CREATE TABLE IF NOT EXISTS wallets (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    balance INTEGER NOT NULL DEFAULT 10000,
    PRIMARY KEY (guild_id, user_id)
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS bets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    match_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    choice TEXT NOT NULL,
    result TEXT,
    created_at TEXT NOT NULL
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS sent_alerts (
    match_id INTEGER NOT NULL,
    alert_type TEXT NOT NULL,
    PRIMARY KEY (match_id, alert_type)
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS dashboard (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    message_id INTEGER NOT NULL
)
""")

db.commit()


# =========================================================
# TIME
# =========================================================

def now_vn():
    return datetime.now(TZ)


def parse_utc(value):
    if not value:
        return None

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)

        return dt.astimezone(TZ)

    except Exception:
        return None


def format_time(value):
    dt = parse_utc(value) if isinstance(value, str) else value

    if not dt:
        return "Không rõ"

    return dt.strftime("%d/%m/%Y %H:%M")


def minutes_until(value):
    dt = parse_utc(value) if isinstance(value, str) else value

    if not dt:
        return None

    return (dt - now_vn()).total_seconds() / 60


# =========================================================
# HELPERS
# =========================================================

def shorten(text, length=1024):
    if not text:
        return ""

    text = str(text)

    if len(text) <= length:
        return text

    return text[:length - 3] + "..."


def team_name(team):
    if not team:
        return "Unknown"

    return (
        team.get("shortName")
        or team.get("name")
        or team.get("tla")
        or "Unknown"
    )


def team_id(team):
    if not team:
        return None

    return team.get("id")


def team_logo(team):
    if not team:
        return None

    return team.get("crest")


def score_from_match(match):
    score = match.get("score") or {}

    full_time = score.get("fullTime") or {}

    return (
        full_time.get("home"),
        full_time.get("away")
    )


def result_for_team(match, wanted_team_id):
    home_id = team_id(match.get("homeTeam"))
    away_id = team_id(match.get("awayTeam"))

    home_goals, away_goals = score_from_match(match)

    if home_goals is None or away_goals is None:
        return None

    if wanted_team_id == home_id:
        if home_goals > away_goals:
            return "W"
        if home_goals < away_goals:
            return "L"
        return "D"

    if wanted_team_id == away_id:
        if away_goals > home_goals:
            return "W"
        if away_goals < home_goals:
            return "L"
        return "D"

    return None


# =========================================================
# FOOTBALL DATA API
# =========================================================

class FootballDataAPI:

    def __init__(self):
        self.session = None
        self.cache = {}
        self.request_times = []

    async def start(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={
                    "X-Auth-Token": FOOTBALL_DATA_TOKEN,
                    "Accept": "application/json",
                }
            )

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def _rate_limit(self):
        now = asyncio.get_running_loop().time()

        self.request_times = [
            x for x in self.request_times
            if now - x < 60
        ]

        # Free plan: keep ourselves below 10 requests/minute.
        if len(self.request_times) >= 9:
            wait_time = 60 - (now - self.request_times[0]) + 0.5

            if wait_time > 0:
                await asyncio.sleep(wait_time)

        self.request_times.append(
            asyncio.get_running_loop().time()
        )

    async def get(self, path, params=None, cache_seconds=90):
        await self.start()

        params = params or {}

        key = (
            path,
            tuple(sorted(params.items()))
        )

        current_time = asyncio.get_running_loop().time()

        cached = self.cache.get(key)

        if cached:
            timestamp, data = cached

            if current_time - timestamp < cache_seconds:
                return data

        await self._rate_limit()

        url = API_BASE + path

        try:
            async with self.session.get(
                url,
                params=params,
                timeout=aiohttp.ClientTimeout(total=20)
            ) as response:

                if response.status == 429:
                    print("Football-data API: 429 rate limit")
                    return None

                if response.status == 403:
                    print("Football-data API: 403 forbidden")
                    return None

                if response.status != 200:
                    print(
                        f"Football-data API error: "
                        f"{response.status} {path}"
                    )
                    return None

                data = await response.json()

                self.cache[key] = (
                    current_time,
                    data
                )

                return data

        except Exception as e:
            print("API error:", e)
            return None

    async def quota(self):
        await self.start()

        await self._rate_limit()

        try:
            async with self.session.get(
                API_BASE + "/competitions",
                timeout=aiohttp.ClientTimeout(total=20)
            ) as response:

                return {
                    "status": response.status,
                    "remaining": response.headers.get(
                        "X-Requests-Available-Minute"
                    ),
                    "used": response.headers.get(
                        "X-Requests-Used-Minute"
                    ),
                }

        except Exception as e:
            return {
                "status": 0,
                "error": str(e)
            }


football = FootballDataAPI()


# =========================================================
# DISCORD BOT
# =========================================================

intents = discord.Intents.default()
intents.guilds = True
intents.members = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


# =========================================================
# FOOTBALL FUNCTIONS
# =========================================================

async def get_team(team_id_value):
    return await football.get(
        f"/teams/{team_id_value}",
        cache_seconds=300
    )


async def get_team_recent(team_id_value, limit=5):
    data = await football.get(
        f"/teams/{team_id_value}/matches",
        params={
            "status": "FINISHED",
            "limit": limit
        },
        cache_seconds=180
    )

    if not data:
        return []

    return data.get("matches", [])


async def get_mancity_matches():
    today = now_vn().date()

    date_from = (
        today - timedelta(days=1)
    ).isoformat()

    date_to = (
        today + timedelta(days=3)
    ).isoformat()

    data = await football.get(
        f"/teams/{MAN_CITY_ID}/matches",
        params={
            "dateFrom": date_from,
            "dateTo": date_to,
            "limit": 100
        },
        cache_seconds=90
    )

    if not data:
        return []

    return data.get("matches", [])


async def get_competition_matches(code):
    today = now_vn().date()

    date_from = (
        today - timedelta(days=1)
    ).isoformat()

    date_to = (
        today + timedelta(days=3)
    ).isoformat()

    data = await football.get(
        f"/competitions/{code}/matches",
        params={
            "dateFrom": date_from,
            "dateTo": date_to
        },
        cache_seconds=90
    )

    if not data:
        return []

    return data.get("matches", [])


async def get_alert_matches():
    """
    Lấy:
    - tất cả trận Man City quanh thời điểm hiện tại
    - tất cả trận của giải được cấu hình
    """

    results = []

    city_matches = await get_mancity_matches()

    results.extend(city_matches)

    competition_matches = await get_competition_matches(
        ALERT_COMPETITION
    )

    results.extend(competition_matches)

    # Dedupe theo match ID
    unique = {}

    for match in results:
        match_id = match.get("id")

        if match_id:
            unique[match_id] = match

    return list(unique.values())


# =========================================================
# PREDICTION MODEL
# =========================================================

def poisson_probability(lam, k):
    if lam <= 0:
        return 1.0 if k == 0 else 0.0

    return (
        math.exp(-lam)
        * (lam ** k)
        / math.factorial(k)
    )


async def team_averages(team_id_value):
    matches = await get_team_recent(
        team_id_value,
        limit=5
    )

    goals_for = []
    goals_against = []

    for match in matches:

        home_id = team_id(
            match.get("homeTeam")
        )

        away_id = team_id(
            match.get("awayTeam")
        )

        home_goals, away_goals = score_from_match(
            match
        )

        if home_goals is None or away_goals is None:
            continue

        if team_id_value == home_id:

            goals_for.append(home_goals)
            goals_against.append(away_goals)

        elif team_id_value == away_id:

            goals_for.append(away_goals)
            goals_against.append(home_goals)

    if not goals_for:
        return 1.2, 1.2

    return (
        sum(goals_for) / len(goals_for),
        sum(goals_against) / len(goals_against)
    )


async def predict_match(match):
    home = match.get("homeTeam") or {}
    away = match.get("awayTeam") or {}

    home_id = team_id(home)
    away_id = team_id(away)

    if not home_id or not away_id:
        return None

    home_attack, home_defense = await team_averages(
        home_id
    )

    away_attack, away_defense = await team_averages(
        away_id
    )

    expected_home = (
        home_attack * 0.60
        + away_defense * 0.40
    ) * 1.08

    expected_away = (
        away_attack * 0.60
        + home_defense * 0.40
    )

    expected_home = max(
        0.15,
        min(expected_home, 5)
    )

    expected_away = max(
        0.15,
        min(expected_away, 5)
    )

    home_win = 0
    draw = 0
    away_win = 0

    best_score = None
    best_probability = -1

    for h in range(0, 7):
        for a in range(0, 7):

            probability = (
                poisson_probability(
                    expected_home,
                    h
                )
                *
                poisson_probability(
                    expected_away,
                    a
                )
            )

            if h > a:
                home_win += probability

            elif h == a:
                draw += probability

            else:
                away_win += probability

            if probability > best_probability:
                best_probability = probability
                best_score = f"{h}-{a}"

    total = home_win + draw + away_win

    if total > 0:
        home_win /= total
        draw /= total
        away_win /= total

    return {
        "score": best_score,
        "home": round(home_win * 100),
        "draw": round(draw * 100),
        "away": round(away_win * 100),
        "expected_home": round(expected_home, 2),
        "expected_away": round(expected_away, 2)
    }


# =========================================================
# FORM
# =========================================================

async def get_form(team_id_value):
    matches = await get_team_recent(
        team_id_value,
        limit=5
    )

    result = []

    for match in matches:

        r = result_for_team(
            match,
            team_id_value
        )

        if r:
            result.append(r)

    return result


def form_text(form):
    if not form:
        return "Chưa có dữ liệu"

    return " ".join(form)


# =========================================================
# MATCH EMBED
# =========================================================

async def create_match_embed(
    match,
    alert_type=None
):
    home = match.get("homeTeam") or {}
    away = match.get("awayTeam") or {}

    home_name = team_name(home)
    away_name = team_name(away)

    home_id = team_id(home)
    away_id = team_id(away)

    status = match.get("status", "UNKNOWN")

    utc_date = match.get("utcDate")

    minutes = minutes_until(utc_date)

    if minutes is not None:

        if minutes > 0:
            countdown = f"Còn khoảng **{max(1, round(minutes))} phút**"

        elif minutes >= -10:
            countdown = "🟢 **ĐANG / VỪA BẮT ĐẦU**"

        else:
            countdown = ""

    else:
        countdown = ""

    competition = match.get(
        "competition",
        {}
    )

    competition_name = competition.get(
        "name",
        "Football"
    )

    embed = discord.Embed(
        title=f"⚽ {home_name} vs {away_name}",
        description=(
            f"🏆 **{competition_name}**\n"
            f"🕐 **{format_time(utc_date)}**\n"
            f"{countdown}"
        ),
        timestamp=now_vn()
    )

    embed.add_field(
        name="Trạng thái",
        value=status,
        inline=True
    )

    if alert_type == "pre":
        embed.add_field(
            name="🔔 Thông báo",
            value="Trận đấu sắp bắt đầu.",
            inline=True
        )

    elif alert_type == "15":
        embed.add_field(
            name="⏰ Thông báo",
            value="Còn khoảng 15 phút.",
            inline=True
        )

    elif alert_type == "start":
        embed.add_field(
            name="🚨 LIVE",
            value="Trận đấu đã bắt đầu!",
            inline=True
        )

    elif alert_type == "finished":
        embed.add_field(
            name="🏁 Kết quả",
            value="Trận đấu đã kết thúc.",
            inline=True
        )

    # Score nếu có
    home_score, away_score = score_from_match(
        match
    )

    if home_score is not None:
        embed.add_field(
            name="📊 Tỷ số",
            value=f"**{home_score} - {away_score}**",
            inline=False
        )

    # Chỉ prediction cho Man City để tiết kiệm API quota
    is_city = (
        home_id == MAN_CITY_ID
        or away_id == MAN_CITY_ID
    )

    if is_city and status not in (
        "FINISHED",
        "CANCELLED",
        "POSTPONED"
    ):
        try:
            prediction = await predict_match(
                match
            )

            if prediction:
                embed.add_field(
                    name="🤖 Dự đoán mô hình",
                    value=(
                        f"**Tỷ số:** `{prediction['score']}`\n"
                        f"🏠 Chủ: **{prediction['home']}%**\n"
                        f"🤝 Hòa: **{prediction['draw']}%**\n"
                        f"🚩 Khách: **{prediction['away']}%**"
                    ),
                    inline=False
                )

        except Exception as e:
            print(
                "Prediction error:",
                e
            )

    embed.set_footer(
        text="Bot statistical estimate • Không phải kết quả chắc chắn"
    )

    logo = team_logo(home)

    if logo:
        embed.set_thumbnail(
            url=logo
        )

    return embed


# =========================================================
# SENT ALERT
# =========================================================

def alert_already_sent(
    match_id,
    alert_type
):
    row = db.execute(
        """
        SELECT 1
        FROM sent_alerts
        WHERE match_id = ?
        AND alert_type = ?
        """,
        (
            match_id,
            alert_type
        )
    ).fetchone()

    return row is not None


def mark_alert_sent(
    match_id,
    alert_type
):
    db.execute(
        """
        INSERT OR IGNORE INTO sent_alerts
        (match_id, alert_type)
        VALUES (?, ?)
        """,
        (
            match_id,
            alert_type
        )
    )

    db.commit()


# =========================================================
# ALERT CHANNEL
# =========================================================

async def get_alert_channel():
    channel = bot.get_channel(
        ALERT_CHANNEL_ID
    )

    if channel is None:

        try:
            channel = await bot.fetch_channel(
                ALERT_CHANNEL_ID
            )

        except Exception as e:
            print(
                "Không fetch được alert channel:",
                e
            )

            return None

    if not isinstance(
        channel,
        discord.TextChannel
    ):
        print(
            "FOOTBALL_ALERT_CHANNEL_ID "
            "không phải text channel."
        )

        return None

    if channel.guild.id != GUILD_ID:
        print(
            "Alert channel không thuộc GUILD_ID."
        )

        return None

    return channel


# =========================================================
# SEND ALERT
# =========================================================

async def send_match_alert(
    channel,
    match,
    alert_type
):

    match_id = match.get("id")

    if not match_id:
        return

    if alert_already_sent(
        match_id,
        alert_type
    ):
        return

    try:

        embed = await create_match_embed(
            match,
            alert_type
        )

        await channel.send(
            embed=embed
        )

        mark_alert_sent(
            match_id,
            alert_type
        )

        print(
            f"Sent alert: "
            f"{match_id} / {alert_type}"
        )

    except discord.Forbidden:
        print(
            "Bot không có quyền gửi tin nhắn "
            "vào alert channel."
        )

    except Exception as e:
        print(
            "send_match_alert error:",
            e
        )


# =========================================================
# ALERT PROCESSOR
# =========================================================

async def process_alerts():

    channel = await get_alert_channel()

    if channel is None:
        return

    matches = await get_alert_matches()

    current_time = now_vn()

    for match in matches:

        match_id = match.get("id")

        if not match_id:
            continue

        home_id = team_id(
            match.get("homeTeam")
        )

        away_id = team_id(
            match.get("awayTeam")
        )

        is_mancity = (
            home_id == MAN_CITY_ID
            or away_id == MAN_CITY_ID
        )

        competition = match.get(
            "competition",
            {}
        )

        competition_code = (
            competition.get("code")
            or ""
        ).upper()

        is_selected_competition = (
            competition_code
            == ALERT_COMPETITION
        )

        # Chỉ thông báo:
        # - Man City
        # - hoặc giải được chọn
        if not (
            is_mancity
            or is_selected_competition
        ):
            continue

        status = (
            match.get("status")
            or ""
        ).upper()

        dt = parse_utc(
            match.get("utcDate")
        )

        if not dt:
            continue

        diff_minutes = (
            dt - current_time
        ).total_seconds() / 60

        # =============================================
        # FINISHED
        # =============================================

        if status == "FINISHED":

            minutes_after = (
                current_time - dt
            ).total_seconds() / 60

            # Chỉ báo kết quả trận vừa kết thúc
            # trong khoảng 3 tiếng gần nhất.
            if 0 <= minutes_after <= 180:

                await send_match_alert(
                    channel,
                    match,
                    "finished"
                )

            continue

        # =============================================
        # CANCELLED / POSTPONED
        # =============================================

        if status in (
            "CANCELLED",
            "POSTPONED"
        ):
            continue

        # =============================================
        # START
        # =============================================

        if -10 <= diff_minutes <= 0:

            await send_match_alert(
                channel,
                match,
                "start"
            )

            continue

        # =============================================
        # 15 MIN
        # =============================================

        if 0 < diff_minutes <= 15:

            await send_match_alert(
                channel,
                match,
                "15"
            )

            continue

        # =============================================
        # 60 MIN
        # =============================================

        if 15 < diff_minutes <= 60:

            await send_match_alert(
                channel,
                match,
                "pre"
            )


# =========================================================
# PERSISTENT DASHBOARD
# =========================================================

async def get_next_match():
    matches = await get_alert_matches()

    current = now_vn()

    upcoming = []

    for match in matches:

        dt = parse_utc(
            match.get("utcDate")
        )

        if not dt:
            continue

        if dt < current:
            continue

        status = (
            match.get("status")
            or ""
        ).upper()

        if status in (
            "FINISHED",
            "CANCELLED",
            "POSTPONED"
        ):
            continue

        upcoming.append(match)

    if not upcoming:
        return None

    upcoming.sort(
        key=lambda x: parse_utc(
            x.get("utcDate")
        )
    )

    return upcoming[0]


async def dashboard_embed():

    embed = discord.Embed(
        title="⚽ FOOTBALL ALERT • ONLINE",
        description=(
            "🤖 Bot đang tự động theo dõi trận đấu.\n\n"
            "🔔 **Không cần dùng lệnh `/thongbao`**\n"
            "Bot tự gửi thông báo vào channel đã đặt "
            "trong Railway."
        ),
        timestamp=now_vn()
    )

    embed.add_field(
        name="🏆 Giải đang theo dõi",
        value=f"`{ALERT_COMPETITION}`",
        inline=True
    )

    embed.add_field(
        name="🔵 Man City",
        value="🟢 Đang theo dõi",
        inline=True
    )

    next_match = await get_next_match()

    if next_match:

        home = team_name(
            next_match.get("homeTeam")
        )

        away = team_name(
            next_match.get("awayTeam")
        )

        dt = parse_utc(
            next_match.get("utcDate")
        )

        competition = next_match.get(
            "competition",
            {}
        ).get(
            "name",
            "Football"
        )

        embed.add_field(
            name="⏭️ Trận tiếp theo",
            value=(
                f"**{home} vs {away}**\n"
                f"🏆 {competition}\n"
                f"🕐 {format_time(dt)}"
            ),
            inline=False
        )

    else:

        embed.add_field(
            name="⏭️ Trận tiếp theo",
            value="Chưa lấy được lịch trận.",
            inline=False
        )

    embed.add_field(
        name="🔔 Tự động",
        value=(
            "• Còn ≤ 60 phút\n"
            "• Còn ≤ 15 phút\n"
            "• Bắt đầu\n"
            "• Kết thúc"
        ),
        inline=False
    )

    embed.set_footer(
        text="Football-data.org • Auto Alert"
    )

    return embed


async def update_dashboard():

    channel = await get_alert_channel()

    if channel is None:
        return

    embed = await dashboard_embed()

    row = db.execute(
        """
        SELECT message_id
        FROM dashboard
        WHERE id = 1
        """
    ).fetchone()

    if row:

        try:

            message = await channel.fetch_message(
                row["message_id"]
            )

            await message.edit(
                embed=embed
            )

            return

        except discord.NotFound:
            pass

        except Exception as e:
            print(
                "Dashboard edit error:",
                e
            )

    try:

        message = await channel.send(
            embed=embed
        )

        db.execute(
            """
            INSERT OR REPLACE INTO dashboard
            (id, message_id)
            VALUES (1, ?)
            """,
            (
                message.id,
            )
        )

        db.commit()

    except Exception as e:

        print(
            "Dashboard send error:",
            e
        )


# =========================================================
# AUTOMATIC ALERT LOOP
# =========================================================

last_dashboard_update = 0


@tasks.loop(seconds=60)
async def football_alert_loop():

    global last_dashboard_update

    try:

        await process_alerts()

        current = asyncio.get_running_loop().time()

        # Update dashboard mỗi 5 phút
        if (
            current - last_dashboard_update
            >= 300
        ):

            await update_dashboard()

            last_dashboard_update = current

    except Exception as e:

        print(
            "football_alert_loop error:",
            e
        )


@football_alert_loop.before_loop
async def before_alert_loop():

    await bot.wait_until_ready()

    # Chờ thêm vài giây để Discord cache channel
    await asyncio.sleep(5)


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
    description="Kiểm tra trạng thái Football Data API"
)
async def apiquota(
    interaction: discord.Interaction
):

    await interaction.response.defer(
        ephemeral=True
    )

    result = await football.quota()

    await interaction.followup.send(
        (
            f"📡 API status: `{result.get('status')}`\n"
            f"📊 Used/min: `{result.get('used')}`\n"
            f"📊 Remaining/min: `{result.get('remaining')}`"
        ),
        ephemeral=True
    )


# =========================================================
# WALLET
# =========================================================

def get_wallet(
    guild_id,
    user_id
):

    row = db.execute(
        """
        SELECT balance
        FROM wallets
        WHERE guild_id = ?
        AND user_id = ?
        """,
        (
            guild_id,
            user_id
        )
    ).fetchone()

    if row:
        return row["balance"]

    db.execute(
        """
        INSERT INTO wallets
        (guild_id, user_id, balance)
        VALUES (?, ?, ?)
        """,
        (
            guild_id,
            user_id,
            10000
        )
    )

    db.commit()

    return 10000


# =========================================================
# /KHOINGHIEP
# =========================================================

@bot.tree.command(
    name="khoinghiep",
    description="Nhận 10,000 xu ảo ban đầu"
)
async def khoinghiep(
    interaction: discord.Interaction
):

    guild_id = interaction.guild_id
    user_id = interaction.user.id

    row = db.execute(
        """
        SELECT balance
        FROM wallets
        WHERE guild_id = ?
        AND user_id = ?
        """,
        (
            guild_id,
            user_id
        )
    ).fetchone()

    if row:

        await interaction.response.send_message(
            "❌ M đã có ví rồi.",
            ephemeral=True
        )

        return

    db.execute(
        """
        INSERT INTO wallets
        (guild_id, user_id, balance)
        VALUES (?, ?, 10000)
        """,
        (
            guild_id,
            user_id
        )
    )

    db.commit()

    await interaction.response.send_message(
        "💰 M đã nhận **10,000 xu ảo**."
    )


# =========================================================
# /VI
# =========================================================

@bot.tree.command(
    name="vi",
    description="Xem số dư xu ảo"
)
async def vi(
    interaction: discord.Interaction
):

    balance = get_wallet(
        interaction.guild_id,
        interaction.user.id
    )

    await interaction.response.send_message(
        f"💰 Ví của m: **{balance:,} xu ảo**"
    )


# =========================================================
# /CUOC
# =========================================================

@bot.tree.command(
    name="cuoc",
    description="Xem lịch sử cược ảo"
)
async def cuoc(
    interaction: discord.Interaction
):

    rows = db.execute(
        """
        SELECT *
        FROM bets
        WHERE guild_id = ?
        AND user_id = ?
        ORDER BY id DESC
        LIMIT 10
        """,
        (
            interaction.guild_id,
            interaction.user.id
        )
    ).fetchall()

    if not rows:

        await interaction.response.send_message(
            "📭 Chưa có lịch sử cược ảo."
        )

        return

    lines = []

    for row in rows:

        lines.append(
            f"`#{row['id']}` "
            f"{row['choice']} "
            f"- {row['amount']:,} xu"
        )

    await interaction.response.send_message(
        "📜 **10 cược gần nhất**\n\n"
        + "\n".join(lines)
    )


# =========================================================
# /KETTOAN
# =========================================================

@bot.tree.command(
    name="kettoan",
    description="Xem thống kê cược ảo"
)
async def kettoan(
    interaction: discord.Interaction
):

    row = db.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(amount) AS amount
        FROM bets
        WHERE guild_id = ?
        AND user_id = ?
        """,
        (
            interaction.guild_id,
            interaction.user.id
        )
    ).fetchone()

    total = row["total"] or 0
    amount = row["amount"] or 0

    await interaction.response.send_message(
        (
            "📊 **THỐNG KÊ CƯỢC ẢO**\n\n"
            f"🎫 Số lượt: **{total}**\n"
            f"💰 Tổng xu đã dùng: **{amount:,}**\n\n"
            "⚠️ Đây chỉ là hệ thống xu ảo."
        )
    )


# =========================================================
# SOI COMMAND
# =========================================================

class SoiView(discord.ui.View):

    def __init__(self):
        super().__init__(
            timeout=180
        )

    @discord.ui.button(
        label="⚽ Champions League",
        style=discord.ButtonStyle.primary
    )
    async def champions(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await interaction.response.defer()

        matches = await get_competition_matches(
            "CL"
        )

        today = now_vn().date()

        matches_today = []

        for match in matches:

            dt = parse_utc(
                match.get("utcDate")
            )

            if dt and dt.date() == today:
                matches_today.append(match)

        if not matches_today:

            await interaction.followup.send(
                "❌ Hôm nay chưa lấy được trận Champions League."
            )

            return

        embeds = []

        for match in matches_today[:10]:

            home = team_name(
                match.get("homeTeam")
            )

            away = team_name(
                match.get("awayTeam")
            )

            embed = discord.Embed(
                title=f"⚽ {home} vs {away}",
                description=(
                    f"🏆 Champions League\n"
                    f"🕐 {format_time(match.get('utcDate'))}"
                )
            )

            embeds.append(embed)

        for embed in embeds:

            await interaction.followup.send(
                embed=embed
            )


    @discord.ui.button(
        label="🔵 Man City",
        style=discord.ButtonStyle.success
    )
    async def city(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await interaction.response.defer()

        matches = await get_mancity_matches()

        upcoming = []

        current = now_vn()

        for match in matches:

            dt = parse_utc(
                match.get("utcDate")
            )

            if not dt:
                continue

            if dt < current:
                continue

            if match.get("status") in (
                "FINISHED",
                "CANCELLED",
                "POSTPONED"
            ):
                continue

            upcoming.append(match)

        upcoming.sort(
            key=lambda x: parse_utc(
                x.get("utcDate")
            )
        )

        if not upcoming:

            await interaction.followup.send(
                "❌ Không tìm thấy trận Man City sắp tới."
            )

            return

        match = upcoming[0]

        embed = await create_match_embed(
            match
        )

        await interaction.followup.send(
            embed=embed
        )


@bot.tree.command(
    name="soi",
    description="Xem lịch và phân tích bóng đá"
)
async def soi(
    interaction: discord.Interaction
):

    embed = discord.Embed(
        title="⚽ SOI BÓNG ĐÁ",
        description=(
            "Chọn giải hoặc đội muốn xem.\n\n"
            "🏆 Champions League\n"
            "🔵 Manchester City"
        )
    )

    await interaction.response.send_message(
        embed=embed,
        view=SoiView()
    )


# =========================================================
# READY
# =========================================================

@bot.event
async def on_ready():

    print("=" * 50)

    print(
        f"Logged in as "
        f"{bot.user} ({bot.user.id})"
    )

    print(
        f"Alert channel: "
        f"{ALERT_CHANNEL_ID}"
    )

    print(
        f"Competition: "
        f"{ALERT_COMPETITION}"
    )

    print("=" * 50)

    await football.start()

    # Sync commands
    try:

        guild = discord.Object(
            id=GUILD_ID
        )

        bot.tree.copy_global_to(
            guild=guild
        )

        synced = await bot.tree.sync(
            guild=guild
        )

        print(
            f"Synced {len(synced)} commands."
        )

    except Exception as e:

        print(
            "Command sync error:",
            e
        )

    # Dashboard tự tạo
    try:

        await update_dashboard()

    except Exception as e:

        print(
            "Initial dashboard error:",
            e
        )

    # Alert loop tự chạy
    if not football_alert_loop.is_running():

        football_alert_loop.start()

        print(
            "✅ Football alert loop STARTED"
        )


# =========================================================
# SHUTDOWN
# =========================================================

async def shutdown():

    try:
        await football.close()
    except Exception:
        pass


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

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

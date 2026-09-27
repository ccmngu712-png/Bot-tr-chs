import os
import asyncio
import sqlite3
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands


# ============================================================
# CONFIG
# ============================================================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY")

# Không bắt buộc.
# Nếu có thì command sẽ sync nhanh vào server đó.
GUILD_ID_RAW = os.getenv("GUILD_ID")

GUILD_ID: Optional[int] = None

if GUILD_ID_RAW:
    try:
        GUILD_ID = int(GUILD_ID_RAW)
    except ValueError:
        print("⚠️ GUILD_ID không phải số hợp lệ.")

API_BASE = "https://v3.football.api-sports.io"

DB_FILE = "football_bot.db"

KST = ZoneInfo("Asia/Seoul")

STARTING_MONEY = 10_000

# Cache API để đỡ tốn quota.
# API-Football free plan có giới hạn request/ngày,
# nên không gọi API lặp lại nếu dữ liệu còn mới.
CACHE_SECONDS = 300

api_cache = {}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("football-bot")


# ============================================================
# CHECK ENV
# ============================================================

if not DISCORD_TOKEN:
    raise RuntimeError(
        "Thiếu DISCORD_TOKEN. Hãy thêm DISCORD_TOKEN trong Railway Variables."
    )

if not FOOTBALL_API_KEY:
    raise RuntimeError(
        "Thiếu FOOTBALL_API_KEY. Hãy thêm FOOTBALL_API_KEY trong Railway Variables."
    )


# ============================================================
# DISCORD INTENTS
# ============================================================

intents = discord.Intents.default()

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


# ============================================================
# DATABASE
# ============================================================

db = sqlite3.connect(
    DB_FILE,
    check_same_thread=False
)

db.row_factory = sqlite3.Row

db.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    balance INTEGER NOT NULL DEFAULT 0
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS bets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    fixture_id INTEGER NOT NULL,
    team TEXT NOT NULL,
    amount INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    payout INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
)
""")

db.commit()

db_lock = asyncio.Lock()


# ============================================================
# DATABASE FUNCTIONS
# ============================================================

async def get_balance(user_id: int) -> int:
    async with db_lock:
        row = db.execute(
            "SELECT balance FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()

        if not row:
            return 0

        return int(row["balance"])


async def create_user(user_id: int) -> bool:
    async with db_lock:
        row = db.execute(
            "SELECT user_id FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()

        if row:
            return False

        db.execute(
            "INSERT INTO users (user_id, balance) VALUES (?, ?)",
            (user_id, STARTING_MONEY)
        )

        db.commit()

        return True


async def change_balance(user_id: int, amount: int):
    async with db_lock:
        row = db.execute(
            "SELECT balance FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()

        if not row:
            db.execute(
                "INSERT INTO users (user_id, balance) VALUES (?, ?)",
                (user_id, max(0, amount))
            )
        else:
            new_balance = int(row["balance"]) + amount

            db.execute(
                "UPDATE users SET balance = ? WHERE user_id = ?",
                (new_balance, user_id)
            )

        db.commit()


async def place_bet(
    user_id: int,
    fixture_id: int,
    team: str,
    amount: int
):
    async with db_lock:
        row = db.execute(
            "SELECT balance FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()

        if not row:
            return False, "Bạn chưa dùng `/khoinghiep`."

        balance = int(row["balance"])

        if amount <= 0:
            return False, "Số tiền phải lớn hơn 0."

        if amount > balance:
            return False, "Bạn không đủ xu."

        # Không cho cược 2 lần cùng một trận
        # để tránh nhầm tiền.
        existing = db.execute(
            """
            SELECT id
            FROM bets
            WHERE user_id = ?
              AND fixture_id = ?
              AND status = 'pending'
            """,
            (user_id, fixture_id)
        ).fetchone()

        if existing:
            return False, "Bạn đã cược trận này rồi."

        db.execute(
            """
            UPDATE users
            SET balance = balance - ?
            WHERE user_id = ?
            """,
            (amount, user_id)
        )

        db.execute(
            """
            INSERT INTO bets
            (
                user_id,
                fixture_id,
                team,
                amount,
                status,
                payout,
                created_at
            )
            VALUES (?, ?, ?, ?, 'pending', 0, ?)
            """,
            (
                user_id,
                fixture_id,
                team,
                amount,
                datetime.now(timezone.utc).isoformat()
            )
        )

        db.commit()

        return True, "Đặt cược thành công."


async def get_user_bets(user_id: int):
    async with db_lock:
        rows = db.execute(
            """
            SELECT *
            FROM bets
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 20
            """,
            (user_id,)
        ).fetchall()

        return rows


async def get_pending_bets(fixture_id: int):
    async with db_lock:
        rows = db.execute(
            """
            SELECT *
            FROM bets
            WHERE fixture_id = ?
              AND status = 'pending'
            """,
            (fixture_id,)
        ).fetchall()

        return rows


async def settle_bet(
    bet_id: int,
    won: bool,
    payout: int
):
    async with db_lock:

        row = db.execute(
            """
            SELECT *
            FROM bets
            WHERE id = ?
              AND status = 'pending'
            """,
            (bet_id,)
        ).fetchone()

        if not row:
            return

        if won:
            db.execute(
                """
                UPDATE users
                SET balance = balance + ?
                WHERE user_id = ?
                """,
                (payout, row["user_id"])
            )

        db.execute(
            """
            UPDATE bets
            SET status = ?, payout = ?
            WHERE id = ?
            """,
            (
                "won" if won else "lost",
                payout,
                bet_id
            )
        )

        db.commit()


# ============================================================
# API FOOTBALL
# ============================================================

class FootballAPI:

    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=20)

            self.session = aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    "x-apisports-key": FOOTBALL_API_KEY,
                    "Accept": "application/json"
                }
            )

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def get(
        self,
        endpoint: str,
        params: dict,
        cache_seconds: int = CACHE_SECONDS
    ):

        await self.start()

        # Cache key
        params_key = "&".join(
            f"{key}={params[key]}"
            for key in sorted(params)
        )

        cache_key = f"{endpoint}?{params_key}"

        now = asyncio.get_running_loop().time()

        cached = api_cache.get(cache_key)

        if cached:
            timestamp, data = cached

            if now - timestamp < cache_seconds:
                return data

        url = f"{API_BASE}/{endpoint}"

        try:
            async with self.session.get(
                url,
                params=params
            ) as response:

                text = await response.text()

                if response.status != 200:
                    log.error(
                        "API HTTP %s: %s",
                        response.status,
                        text[:500]
                    )

                    return {
                        "get": endpoint,
                        "errors": {
                            "http": response.status,
                            "message": text[:500]
                        },
                        "results": 0,
                        "response": []
                    }

                try:
                    data = await response.json()
                except Exception:
                    log.error("API trả về dữ liệu không phải JSON.")
                    return {
                        "get": endpoint,
                        "errors": {
                            "message": "Invalid JSON"
                        },
                        "results": 0,
                        "response": []
                    }

                if data.get("errors"):
                    log.warning(
                        "API errors: %s",
                        data.get("errors")
                    )

                api_cache[cache_key] = (
                    now,
                    data
                )

                return data

        except asyncio.TimeoutError:
            log.error("API timeout.")
            return {
                "get": endpoint,
                "errors": {
                    "message": "API timeout"
                },
                "results": 0,
                "response": []
            }

        except aiohttp.ClientError as e:
            log.error(
                "API connection error: %s",
                e
            )

            return {
                "get": endpoint,
                "errors": {
                    "message": str(e)
                },
                "results": 0,
                "response": []
            }

    async def fixtures_by_date(
        self,
        date: str
    ):
        return await self.get(
            "fixtures",
            {
                "date": date,
                "timezone": "Asia/Seoul"
            },
            cache_seconds=120
        )

    async def fixture(
        self,
        fixture_id: int
    ):
        return await self.get(
            "fixtures",
            {
                "id": fixture_id
            },
            cache_seconds=60
        )

    async def prediction(
        self,
        fixture_id: int
    ):
        return await self.get(
            "predictions",
            {
                "fixture": fixture_id
            },
            cache_seconds=900
        )

    async def h2h(
        self,
        home_id: int,
        away_id: int
    ):
        return await self.get(
            "fixtures/headtohead",
            {
                "h2h": f"{home_id}-{away_id}",
                "last": 5
            },
            cache_seconds=1800
        )


football = FootballAPI()


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def clean_text(value, default="Không có"):
    if value is None:
        return default

    text = str(value).strip()

    if not text:
        return default

    return text


def format_time(date_string: str):
    try:
        dt = datetime.fromisoformat(
            date_string.replace("Z", "+00:00")
        )

        dt = dt.astimezone(KST)

        return dt.strftime("%d/%m/%Y %H:%M")

    except Exception:
        return date_string


def get_status(fixture):
    return fixture.get("fixture", {}).get(
        "status", {}
    ).get("short", "?")


def is_upcoming(fixture):
    status = get_status(fixture)

    return status in {
        "NS",
        "TBD",
        "PST"
    }


def team_data(fixture):
    teams = fixture.get("teams", {})

    home = teams.get("home", {})
    away = teams.get("away", {})

    return home, away


def score_text(fixture):
    goals = fixture.get("goals", {})

    home = goals.get("home")
    away = goals.get("away")

    if home is None or away is None:
        return "Chưa có"

    return f"{home} - {away}"


def short_team_name(name: str):
    name = clean_text(name)

    if len(name) <= 22:
        return name

    return name[:19] + "..."


def prediction_info(prediction_data):

    if not prediction_data:
        return None

    responses = prediction_data.get(
        "response",
        []
    )

    if not responses:
        return None

    item = responses[0]

    predictions = item.get(
        "predictions",
        {}
    )

    comparison = item.get(
        "comparison",
        {}
    )

    return {
        "predictions": predictions,
        "comparison": comparison,
        "teams": item.get("teams", {})
    }


def percent_value(value):
    if value is None:
        return "?"

    return str(value)


def result_from_fixture(fixture):

    teams = fixture.get("teams", {})

    home = teams.get("home", {})
    away = teams.get("away", {})

    home_winner = home.get("winner")
    away_winner = away.get("winner")

    if home_winner is True:
        return "home"

    if away_winner is True:
        return "away"

    goals = fixture.get("goals", {})

    hg = goals.get("home")
    ag = goals.get("away")

    if hg is not None and ag is not None:

        if hg > ag:
            return "home"

        if ag > hg:
            return "away"

        if hg == ag:
            return "draw"

    return None


# ============================================================
# MATCH SELECT MENU
# ============================================================

class MatchSelect(discord.ui.Select):

    def __init__(self, fixtures):

        self.fixtures = fixtures

        options = []

        for fixture in fixtures[:25]:

            fixture_id = fixture.get(
                "fixture",
                {}
            ).get("id")

            home, away = team_data(fixture)

            home_name = clean_text(
                home.get("name"),
                "Home"
            )

            away_name = clean_text(
                away.get("name"),
                "Away"
            )

            league_name = clean_text(
                fixture.get("league", {}).get("name"),
                "Football"
            )

            options.append(
                discord.SelectOption(
                    label=(
                        f"{short_team_name(home_name)} "
                        f"vs "
                        f"{short_team_name(away_name)}"
                    )[:100],
                    description=(
                        f"{league_name} • ID {fixture_id}"
                    )[:100],
                    value=str(fixture_id)
                )
            )

        super().__init__(
            placeholder="⚽ Chọn trận muốn soi...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(
        self,
        interaction: discord.Interaction
    ):

        fixture_id = int(
            self.values[0]
        )

        await interaction.response.defer(
            ephemeral=True
        )

        await send_match_analysis(
            interaction,
            fixture_id
        )


class MatchView(discord.ui.View):

    def __init__(self, fixtures):

        super().__init__(
            timeout=300
        )

        self.add_item(
            MatchSelect(fixtures)
        )


# ============================================================
# MATCH ANALYSIS
# ============================================================

async def send_match_analysis(
    interaction: discord.Interaction,
    fixture_id: int
):

    data = await football.fixture(
        fixture_id
    )

    fixtures = data.get(
        "response",
        []
    )

    if not fixtures:

        await interaction.followup.send(
            "❌ Không tìm thấy trận này.",
            ephemeral=True
        )

        return

    fixture = fixtures[0]

    home, away = team_data(fixture)

    home_name = clean_text(
        home.get("name"),
        "Đội nhà"
    )

    away_name = clean_text(
        away.get("name"),
        "Đội khách"
    )

    home_logo = home.get("logo")
    away_logo = away.get("logo")

    league = fixture.get(
        "league",
        {}
    )

    league_name = clean_text(
        league.get("name"),
        "Football"
    )

    country = clean_text(
        league.get("country"),
        ""
    )

    status = get_status(fixture)

    date_string = fixture.get(
        "fixture",
        {}
    ).get(
        "date",
        ""
    )

    venue = fixture.get(
        "fixture",
        {}
    ).get(
        "venue",
        {}
    )

    venue_name = clean_text(
        venue.get("name"),
        "Chưa rõ"
    )

    # --------------------------------------------------------
    # Prediction
    # --------------------------------------------------------

    prediction_data = await football.prediction(
        fixture_id
    )

    prediction = prediction_info(
        prediction_data
    )

    embed = discord.Embed(
        title=f"⚽ {home_name} vs {away_name}",
        description=(
            f"**{league_name}**"
            + (f" • {country}" if country else "")
        ),
        color=discord.Color.blue()
    )

    if home_logo:
        embed.set_thumbnail(
            url=home_logo
        )

    embed.add_field(
        name="🏟️ Trận đấu",
        value=(
            f"**{home_name}** 🆚 **{away_name}**\n"
            f"🕐 {format_time(date_string)} KST\n"
            f"📍 {venue_name}\n"
            f"📌 Trạng thái: `{status}`"
        ),
        inline=False
    )

    # --------------------------------------------------------
    # Prediction
    # --------------------------------------------------------

    if prediction:

        p = prediction["predictions"]

        winner = p.get("winner") or {}

        winner_name = clean_text(
            winner.get("name"),
            "Chưa có"
        )

        advice = clean_text(
            p.get("advice"),
            "Chưa có"
        )

        under_over = clean_text(
            p.get("under_over"),
            "Chưa có"
        )

        goals = p.get(
            "goals",
            {}
        )

        home_goals = clean_text(
            goals.get("home"),
            "?"
        )

        away_goals = clean_text(
            goals.get("away"),
            "?"
        )

        percent = p.get(
            "percent",
            {}
        )

        home_percent = percent_value(
            percent.get("home")
        )

        draw_percent = percent_value(
            percent.get("draw")
        )

        away_percent = percent_value(
            percent.get("away")
        )

        embed.add_field(
            name="🤖 API-Football Prediction",
            value=(
                f"🏆 Dự đoán: **{winner_name}**\n"
                f"💡 Advice: `{advice}`\n"
                f"⚽ Tỷ số dự đoán: "
                f"**{home_goals} - {away_goals}**\n"
                f"📊 Home: **{home_percent}**\n"
                f"🤝 Draw: **{draw_percent}**\n"
                f"📊 Away: **{away_percent}**\n"
                f"📈 Tổng bàn: `{under_over}`"
            ),
            inline=False
        )

        comparison = prediction.get(
            "comparison",
            {}
        )

        if comparison:

            def comp_value(key):

                obj = comparison.get(
                    key,
                    {}
                )

                home_value = obj.get(
                    "home",
                    "?"
                )

                away_value = obj.get(
                    "away",
                    "?"
                )

                return (
                    f"{home_name}: `{home_value}`\n"
                    f"{away_name}: `{away_value}`"
                )

            attack = comp_value("att")
            defense = comp_value("def")
            poisson = comp_value("poisson_distribution")

            embed.add_field(
                name="📊 So sánh",
                value=(
                    f"⚔️ **Attack**\n{attack}\n\n"
                    f"🛡️ **Defense**\n{defense}\n\n"
                    f"🎯 **Poisson**\n{poisson}"
                ),
                inline=False
            )

    else:

        embed.add_field(
            name="🤖 Prediction",
            value=(
                "API-Football hiện không có prediction "
                "cho trận này."
            ),
            inline=False
        )

    # --------------------------------------------------------
    # H2H
    # --------------------------------------------------------

    home_id = home.get("id")
    away_id = away.get("id")

    if home_id and away_id:

        h2h_data = await football.h2h(
            home_id,
            away_id
        )

        h2h_matches = h2h_data.get(
            "response",
            []
        )

        if h2h_matches:

            lines = []

            for match in h2h_matches[:5]:

                mh, ma = team_data(match)

                mh_name = clean_text(
                    mh.get("name"),
                    "Home"
                )

                ma_name = clean_text(
                    ma.get("name"),
                    "Away"
                )

                score = score_text(
                    match
                )

                lines.append(
                    f"• {mh_name} **{score}** {ma_name}"
                )

            embed.add_field(
                name="🆚 5 lần đối đầu gần nhất",
                value="\n".join(lines),
                inline=False
            )

    # --------------------------------------------------------
    # Footer
    # --------------------------------------------------------

    embed.set_footer(
        text=(
            f"Fixture ID: {fixture_id} • "
            "Dữ liệu từ API-Football"
        )
    )

    await interaction.followup.send(
        embed=embed,
        ephemeral=True
    )


# ============================================================
# READY
# ============================================================

@bot.event
async def on_ready():

    log.info(
        "Bot online: %s (%s)",
        bot.user,
        bot.user.id
    )

    try:

        if GUILD_ID:

            guild = discord.Object(
                id=GUILD_ID
            )

            # Copy command tree sang guild
            bot.tree.copy_global_to(
                guild=guild
            )

            synced = await bot.tree.sync(
                guild=guild
            )

            log.info(
                "Synced %s commands vào guild %s",
                len(synced),
                GUILD_ID
            )

        else:

            synced = await bot.tree.sync()

            log.info(
                "Synced %s global commands",
                len(synced)
            )

    except Exception as e:

        log.exception(
            "Không sync được slash commands: %s",
            e
        )


# ============================================================
# /KHOINGHIEP
# ============================================================

@bot.tree.command(
    name="khoinghiep",
    description="Nhận 10.000 xu ảo để bắt đầu."
)
async def khoinghiep(
    interaction: discord.Interaction
):

    created = await create_user(
        interaction.user.id
    )

    if not created:

        balance = await get_balance(
            interaction.user.id
        )

        await interaction.response.send_message(
            (
                "❌ Bạn đã nhận xu khởi nghiệp rồi.\n"
                f"💰 Ví hiện tại: **{balance:,} xu**"
            ),
            ephemeral=True
        )

        return

    await interaction.response.send_message(
        (
            "🎉 **Khởi nghiệp thành công!**\n\n"
            "💰 Bạn nhận được **10.000 xu ảo**.\n"
            "⚽ Dùng `/soi` để xem trận."
        )
    )


# ============================================================
# /VI
# ============================================================

@bot.tree.command(
    name="vi",
    description="Xem số xu ảo hiện tại."
)
async def vi(
    interaction: discord.Interaction
):

    balance = await get_balance(
        interaction.user.id
    )

    embed = discord.Embed(
        title="💰 Ví của bạn",
        description=(
            f"👤 {interaction.user.mention}\n\n"
            f"💵 **{balance:,} xu**"
        ),
        color=discord.Color.gold()
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# ============================================================
# /SOI
# ============================================================

@bot.tree.command(
    name="soi",
    description="Xem các trận bóng thật từ API-Football."
)
@app_commands.describe(
    ngay="Ngày YYYY-MM-DD. Bỏ trống = hôm nay theo giờ Hàn."
)
async def soi(
    interaction: discord.Interaction,
    ngay: Optional[str] = None
):

    await interaction.response.defer()

    # --------------------------------------------------------
    # Date
    # --------------------------------------------------------

    if not ngay:

        ngay = datetime.now(
            KST
        ).strftime("%Y-%m-%d")

    else:

        try:

            datetime.strptime(
                ngay,
                "%Y-%m-%d"
            )

        except ValueError:

            await interaction.followup.send(
                "❌ Ngày phải có dạng `YYYY-MM-DD`.",
                ephemeral=True
            )

            return

    # --------------------------------------------------------
    # API
    # --------------------------------------------------------

    data = await football.fixtures_by_date(
        ngay
    )

    errors = data.get(
        "errors"
    )

    if errors:

        await interaction.followup.send(
            (
                "❌ API-Football báo lỗi:\n"
                f"`{errors}`"
            ),
            ephemeral=True
        )

        return

    fixtures = data.get(
        "response",
        []
    )

    if not fixtures:

        await interaction.followup.send(
            (
                f"⚽ Không tìm thấy trận nào ngày "
                f"**{ngay}**."
            ),
            ephemeral=True
        )

        return

    # --------------------------------------------------------
    # Sort by kickoff
    # --------------------------------------------------------

    fixtures.sort(
        key=lambda x: x.get(
            "fixture",
            {}
        ).get(
            "timestamp",
            0
        )
    )

    # Discord Select chỉ cho tối đa 25 option.
    shown = fixtures[:25]

    embed = discord.Embed(
        title=f"⚽ Bóng đá ngày {ngay}",
        description=(
            f"API trả về **{len(fixtures)} trận**.\n"
            f"Đang hiển thị **{len(shown)} trận đầu tiên**.\n\n"
            "👇 Chọn trận bên dưới để xem phân tích."
        ),
        color=discord.Color.green()
    )

    # Tổng số giải
    leagues = {}

    for fixture in fixtures:

        league_name = clean_text(
            fixture.get(
                "league",
                {}
            ).get("name"),
            "Unknown"
        )

        leagues[league_name] = (
            leagues.get(league_name, 0) + 1
        )

    league_text = "\n".join(
        f"• {name}: **{count}**"
        for name, count in list(
            leagues.items()
        )[:15]
    )

    if league_text:

        embed.add_field(
            name="🏆 Các giải trong ngày",
            value=league_text[:1024],
            inline=False
        )

    embed.set_footer(
        text="Dữ liệu lịch thi đấu: API-Football"
    )

    await interaction.followup.send(
        embed=embed,
        view=MatchView(shown)
    )


# ============================================================
# /CADO
# ============================================================

@bot.tree.command(
    name="cado",
    description="Cược bằng xu Discord ảo."
)
@app_commands.describe(
    fixture_id="ID trận lấy từ /soi",
    team="Nhập tên đội hoặc HOA",
    amount="Số xu muốn cược"
)
async def cado(
    interaction: discord.Interaction,
    fixture_id: int,
    team: str,
    amount: int
):

    await interaction.response.defer(
        ephemeral=True
    )

    if amount <= 0:

        await interaction.followup.send(
            "❌ Số xu phải lớn hơn 0.",
            ephemeral=True
        )

        return

    # --------------------------------------------------------
    # Get fixture
    # --------------------------------------------------------

    data = await football.fixture(
        fixture_id
    )

    fixtures = data.get(
        "response",
        []
    )

    if not fixtures:

        await interaction.followup.send(
            "❌ Không tìm thấy Fixture ID này.",
            ephemeral=True
        )

        return

    fixture = fixtures[0]

    if not is_upcoming(fixture):

        await interaction.followup.send(
            (
                "❌ Trận này đã bắt đầu hoặc đã kết thúc.\n"
                f"Trạng thái: `{get_status(fixture)}`"
            ),
            ephemeral=True
        )

        return

    home, away = team_data(
        fixture
    )

    home_name = clean_text(
        home.get("name"),
        "Home"
    )

    away_name = clean_text(
        away.get("name"),
        "Away"
    )

    team_clean = team.strip()

    # --------------------------------------------------------
    # Determine bet
    # --------------------------------------------------------

    if team_clean.lower() in {
        "hoa",
        "draw",
        "x"
    }:

        selected = "draw"
        display_team = "HÒA"

    elif team_clean.lower() in {
        home_name.lower()
    }:

        selected = "home"
        display_team = home_name

    elif team_clean.lower() in {
        away_name.lower()
    }:

        selected = "away"
        display_team = away_name

    else:

        await interaction.followup.send(
            (
                "❌ Không nhận diện được đội.\n\n"
                f"🏠 `{home_name}`\n"
                f"✈️ `{away_name}`\n"
                "🤝 `HOA`"
            ),
            ephemeral=True
        )

        return

    # --------------------------------------------------------
    # Place bet
    # --------------------------------------------------------

    success, message = await place_bet(
        interaction.user.id,
        fixture_id,
        selected,
        amount
    )

    if not success:

        await interaction.followup.send(
            f"❌ {message}",
            ephemeral=True
        )

        return

    balance = await get_balance(
        interaction.user.id
    )

    embed = discord.Embed(
        title="🎯 Đặt cược thành công",
        color=discord.Color.orange()
    )

    embed.add_field(
        name="⚽ Trận",
        value=(
            f"**{home_name}** 🆚 **{away_name}**"
        ),
        inline=False
    )

    embed.add_field(
        name="🎲 Lựa chọn",
        value=f"**{display_team}**",
        inline=True
    )

    embed.add_field(
        name="💰 Tiền cược",
        value=f"**{amount:,} xu**",
        inline=True
    )

    embed.add_field(
        name="💵 Ví còn lại",
        value=f"**{balance:,} xu**",
        inline=True
    )

    embed.set_footer(
        text="Xu chỉ là tiền ảo trong Discord."
    )

    await interaction.followup.send(
        embed=embed,
        ephemeral=True
    )


# ============================================================
# /CUOC_CUA_TOI
# ============================================================

@bot.tree.command(
    name="cuoc_cua_toi",
    description="Xem các cược của bạn."
)
async def cuoc_cua_toi(
    interaction: discord.Interaction
):

    rows = await get_user_bets(
        interaction.user.id
    )

    if not rows:

        await interaction.response.send_message(
            "Bạn chưa có cược nào.",
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
        else:
            icon = "❌"

        lines.append(
            (
                f"{icon} Fixture `{row['fixture_id']}`\n"
                f"→ `{row['team']}` • "
                f"**{row['amount']:,} xu** • "
                f"`{status}`"
            )
        )

    embed = discord.Embed(
        title="🎯 Cược của bạn",
        description="\n\n".join(lines),
        color=discord.Color.blue()
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# ============================================================
# ADMIN CHECK
# ============================================================

def is_admin(interaction: discord.Interaction):

    if not interaction.guild:
        return False

    member = interaction.user

    return isinstance(
        member,
        discord.Member
    ) and member.guild_permissions.administrator


# ============================================================
# /KETTOAN
# ============================================================

@bot.tree.command(
    name="kettoan",
    description="Admin chốt cược theo kết quả thật của trận."
)
@app_commands.describe(
    fixture_id="ID trận"
)
async def kettoan(
    interaction: discord.Interaction,
    fixture_id: int
):

    if not is_admin(interaction):

        await interaction.response.send_message(
            "❌ Chỉ Admin mới được dùng lệnh này.",
            ephemeral=True
        )

        return

    await interaction.response.defer()

    # --------------------------------------------------------
    # Get fixture
    # --------------------------------------------------------

    data = await football.fixture(
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

    status = get_status(
        fixture
    )

    # Chỉ settle trận đã kết thúc.
    finished_statuses = {
        "FT",
        "AET",
        "PEN"
    }

    if status not in finished_statuses:

        await interaction.followup.send(
            (
                f"❌ Trận chưa kết thúc.\n"
                f"Trạng thái hiện tại: `{status}`"
            )
        )

        return

    result = result_from_fixture(
        fixture
    )

    if result is None:

        await interaction.followup.send(
            (
                "❌ Không xác định được kết quả "
                "từ API."
            )
        )

        return

    home, away = team_data(
        fixture
    )

    home_name = clean_text(
        home.get("name"),
        "Home"
    )

    away_name = clean_text(
        away.get("name"),
        "Away"
    )

    score = score_text(
        fixture
    )

    # --------------------------------------------------------
    # Get bets
    # --------------------------------------------------------

    bets = await get_pending_bets(
        fixture_id
    )

    if not bets:

        await interaction.followup.send(
            (
                f"⚽ **{home_name} {score} {away_name}**\n"
                "Không có cược đang chờ cho trận này."
            )
        )

        return

    # --------------------------------------------------------
    # Settle
    # --------------------------------------------------------

    won_count = 0
    lost_count = 0
    total_paid = 0

    for bet in bets:

        won = (
            bet["team"] == result
        )

        if won:

            # Cược thắng nhận 2x tiền cược.
            payout = bet["amount"] * 2

            await settle_bet(
                bet["id"],
                True,
                payout
            )

            won_count += 1
            total_paid += payout

        else:

            await settle_bet(
                bet["id"],
                False,
                0
            )

            lost_count += 1

    result_name = {
        "home": home_name,
        "away": away_name,
        "draw": "HÒA"
    }.get(
        result,
        result
    )

    embed = discord.Embed(
        title="🏁 Đã kết toán trận",
        color=discord.Color.green()
    )

    embed.add_field(
        name="⚽ Kết quả",
        value=(
            f"**{home_name} {score} {away_name}**\n"
            f"🏆 Kết quả: **{result_name}**"
        ),
        inline=False
    )

    embed.add_field(
        name="🎯 Cược thắng",
        value=f"**{won_count}**",
        inline=True
    )

    embed.add_field(
        name="❌ Cược thua",
        value=f"**{lost_count}**",
        inline=True
    )

    embed.add_field(
        name="💰 Tổng xu trả",
        value=f"**{total_paid:,} xu**",
        inline=True
    )

    embed.set_footer(
        text=f"Fixture ID: {fixture_id}"
    )

    await interaction.followup.send(
        embed=embed
    )


# ============================================================
# /CLEAR_CACHE
# ============================================================

@bot.tree.command(
    name="clear_cache",
    description="Admin xóa cache API."
)
async def clear_cache(
    interaction: discord.Interaction
):

    if not is_admin(interaction):

        await interaction.response.send_message(
            "❌ Chỉ Admin.",
            ephemeral=True
        )

        return

    api_cache.clear()

    await interaction.response.send_message(
        "✅ Đã xóa API cache.",
        ephemeral=True
    )


# ============================================================
# ERROR HANDLER
# ============================================================

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError
):

    log.exception(
        "Slash command error: %s",
        error
    )

    message = (
        "❌ Có lỗi xảy ra khi chạy lệnh.\n"
        "Kiểm tra Railway Logs để xem chi tiết."
    )

    try:

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

    except Exception:
        pass


# ============================================================
# SHUTDOWN
# ============================================================

async def shutdown():

    log.info(
        "Đang đóng bot..."
    )

    await football.close()

    db.close()


# ============================================================
# RUN
# ============================================================

async def main():

    log.info(
        "======================================"
    )

    log.info(
        "Starting Football Discord Bot..."
    )

    log.info(
        "Railway mode: ON"
    )

    log.info(
        "Guild ID: %s",
        GUILD_ID if GUILD_ID else "GLOBAL"
    )

    log.info(
        "======================================"
    )

    try:

        await bot.start(
            DISCORD_TOKEN
        )

    finally:

        await shutdown()


if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        pass

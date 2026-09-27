import os
import sqlite3
import asyncio
from datetime import datetime, date, timedelta
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands


# =========================================================
# CONFIG
# =========================================================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY")

# Server ID của m
GUILD_ID = 1551547049600618538

API_BASE = "https://v3.football.api-sports.io"

DB_FILE = "football_bot.db"

STARTING_BALANCE = 10_000
MIN_BET = 100
MAX_BET = 1_000_000

# Tiền ảo:
# thắng = nhận 2x tiền cược
# thua = mất tiền cược
PAYOUT_MULTIPLIER = 2

# Cache API ngắn để đỡ đốt quota
CACHE_SECONDS = 120


# =========================================================
# CHECK ENV
# =========================================================

if not DISCORD_TOKEN:
    raise RuntimeError("Thiếu DISCORD_TOKEN trong Environment/Secrets.")

if not FOOTBALL_API_KEY:
    raise RuntimeError("Thiếu FOOTBALL_API_KEY trong Environment/Secrets.")


# =========================================================
# DISCORD
# =========================================================

intents = discord.Intents.default()

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)

tree = bot.tree


# =========================================================
# DATABASE
# =========================================================

db = sqlite3.connect(DB_FILE)
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
    choice TEXT NOT NULL,
    choice_name TEXT NOT NULL,
    amount INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
)
""")

db.commit()


# =========================================================
# DB HELPERS
# =========================================================

def get_balance(user_id: int) -> int:
    row = db.execute(
        "SELECT balance FROM users WHERE user_id = ?",
        (user_id,)
    ).fetchone()

    if row is None:
        db.execute(
            "INSERT INTO users (user_id, balance) VALUES (?, ?)",
            (user_id, STARTING_BALANCE)
        )
        db.commit()
        return STARTING_BALANCE

    return int(row["balance"])


def set_balance(user_id: int, balance: int):
    db.execute(
        """
        INSERT INTO users (user_id, balance)
        VALUES (?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET balance = excluded.balance
        """,
        (user_id, balance)
    )
    db.commit()


def add_balance(user_id: int, amount: int):
    current = get_balance(user_id)
    set_balance(user_id, current + amount)


def create_bet(
    user_id: int,
    fixture_id: int,
    choice: str,
    choice_name: str,
    amount: int
):
    db.execute(
        """
        INSERT INTO bets
        (user_id, fixture_id, choice, choice_name, amount, status, created_at)
        VALUES (?, ?, ?, ?, ?, 'pending', ?)
        """,
        (
            user_id,
            fixture_id,
            choice,
            choice_name,
            amount,
            datetime.utcnow().isoformat()
        )
    )
    db.commit()


def get_pending_bets_for_fixture(fixture_id: int):
    return db.execute(
        """
        SELECT *
        FROM bets
        WHERE fixture_id = ?
        AND status = 'pending'
        """,
        (fixture_id,)
    ).fetchall()


def mark_bet(bet_id: int, status: str):
    db.execute(
        "UPDATE bets SET status = ? WHERE id = ?",
        (status, bet_id)
    )
    db.commit()


# =========================================================
# API CACHE
# =========================================================

api_cache = {}


def cache_key(endpoint: str, params: dict):
    items = tuple(sorted(params.items()))
    return endpoint, items


async def football_api(
    endpoint: str,
    params: Optional[dict] = None,
    cache_seconds: int = CACHE_SECONDS
):
    params = params or {}

    key = cache_key(endpoint, params)
    now = datetime.utcnow().timestamp()

    cached = api_cache.get(key)

    if cached:
        timestamp, data = cached

        if now - timestamp < cache_seconds:
            return data

    headers = {
        "x-apisports-key": FOOTBALL_API_KEY,
        "Accept": "application/json",
    }

    url = f"{API_BASE}/{endpoint}"

    timeout = aiohttp.ClientTimeout(total=20)

    async with aiohttp.ClientSession(timeout=timeout) as session:

        try:
            async with session.get(
                url,
                headers=headers,
                params=params
            ) as response:

                if response.status != 200:
                    text = await response.text()

                    raise RuntimeError(
                        f"API HTTP {response.status}: {text[:300]}"
                    )

                data = await response.json()

        except asyncio.TimeoutError:
            raise RuntimeError("API-Football phản hồi quá lâu.")

    errors = data.get("errors")

    if errors:
        raise RuntimeError(str(errors))

    api_cache[key] = (now, data)

    return data


# =========================================================
# FOOTBALL HELPERS
# =========================================================

def get_fixture_team_names(fixture):
    home = fixture["teams"]["home"]
    away = fixture["teams"]["away"]

    return home["name"], away["name"]


def fixture_status(fixture):
    return fixture["fixture"]["status"]["short"]


def is_finished_status(status: str) -> bool:
    return status in {
        "FT",
        "AET",
        "PEN",
    }


def is_not_started(status: str) -> bool:
    return status in {
        "NS",
        "TBD",
    }


def format_kickoff(fixture):
    raw = fixture["fixture"]["date"]

    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))

        return dt.strftime("%d/%m/%Y %H:%M UTC")

    except Exception:
        return raw


def safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


# =========================================================
# GET FIXTURES BY DATE
# =========================================================

async def get_fixtures_by_date(target_date: str):

    data = await football_api(
        "fixtures",
        {
            "date": target_date,
            "timezone": "UTC"
        },
        cache_seconds=60
    )

    return data.get("response", [])


# =========================================================
# TEAM RECENT FORM
# =========================================================

async def get_team_recent(team_id: int):

    data = await football_api(
        "fixtures",
        {
            "team": team_id,
            "last": 5
        },
        cache_seconds=300
    )

    return data.get("response", [])


def form_text(matches, team_id: int):

    if not matches:
        return "Không có dữ liệu"

    results = []

    for match in matches:

        home_id = match["teams"]["home"]["id"]
        away_id = match["teams"]["away"]["id"]

        home_goals = safe_int(
            match["goals"]["home"]
        )

        away_goals = safe_int(
            match["goals"]["away"]
        )

        if home_id == team_id:

            if home_goals > away_goals:
                results.append("🟢 T")

            elif home_goals < away_goals:
                results.append("🔴 B")

            else:
                results.append("🟡 H")

        elif away_id == team_id:

            if away_goals > home_goals:
                results.append("🟢 T")

            elif away_goals < home_goals:
                results.append("🔴 B")

            else:
                results.append("🟡 H")

    return " ".join(results)


# =========================================================
# H2H
# =========================================================

async def get_h2h(home_id: int, away_id: int):

    data = await football_api(
        "fixtures/headtohead",
        {
            "h2h": f"{home_id}-{away_id}",
            "last": 5
        },
        cache_seconds=600
    )

    return data.get("response", [])


def h2h_text(matches, home_id, away_id):

    if not matches:
        return "Không có dữ liệu H2H."

    lines = []

    for match in matches[:5]:

        home = match["teams"]["home"]
        away = match["teams"]["away"]

        gh = safe_int(match["goals"]["home"])
        ga = safe_int(match["goals"]["away"])

        lines.append(
            f"{home['name']} {gh}-{ga} {away['name']}"
        )

    return "\n".join(lines)


# =========================================================
# PREDICTION
# =========================================================

async def get_prediction(fixture_id: int):

    data = await football_api(
        "predictions",
        {
            "fixture": fixture_id
        },
        cache_seconds=600
    )

    response = data.get("response", [])

    if not response:
        return None

    return response[0]


# =========================================================
# PREDICTION FORMAT
# =========================================================

def prediction_text(prediction):

    if not prediction:
        return (
            "⚠️ API không có prediction cho trận này."
        )

    predictions = prediction.get("predictions", {})

    winner = predictions.get("winner") or {}

    winner_name = winner.get("name") or "Không rõ"

    advice = predictions.get("advice") or "Không có"

    under_over = predictions.get("under_over") or "Không có"

    goals = predictions.get("goals") or {}

    home_goals = goals.get("home") or "?"
    away_goals = goals.get("away") or "?"

    percent = predictions.get("percent") or {}

    home_percent = percent.get("home") or "?"
    draw_percent = percent.get("draw") or "?"
    away_percent = percent.get("away") or "?"

    return (
        f"🏆 **API dự đoán:** {winner_name}\n"
        f"💡 **Advice:** {advice}\n"
        f"⚽ **Tỉ số dự kiến:** {home_goals} - {away_goals}\n"
        f"📊 **1X2:** "
        f"{home_percent} / {draw_percent} / {away_percent}\n"
        f"📈 **Over/Under:** {under_over}"
    )


# =========================================================
# MATCH ANALYSIS
# =========================================================

async def analyze_fixture(fixture):

    fixture_id = fixture["fixture"]["id"]

    home = fixture["teams"]["home"]
    away = fixture["teams"]["away"]

    home_id = home["id"]
    away_id = away["id"]

    # 4 API calls tối đa:
    # recent home
    # recent away
    # H2H
    # prediction

    results = await asyncio.gather(
        get_team_recent(home_id),
        get_team_recent(away_id),
        get_h2h(home_id, away_id),
        get_prediction(fixture_id),
        return_exceptions=True
    )

    home_recent = (
        results[0]
        if not isinstance(results[0], Exception)
        else []
    )

    away_recent = (
        results[1]
        if not isinstance(results[1], Exception)
        else []
    )

    h2h = (
        results[2]
        if not isinstance(results[2], Exception)
        else []
    )

    prediction = (
        results[3]
        if not isinstance(results[3], Exception)
        else None
    )

    embed = discord.Embed(
        title=f"⚽ {home['name']} vs {away['name']}",
        description=(
            f"🆔 Fixture ID: `{fixture_id}`\n"
            f"🕐 {format_kickoff(fixture)}\n"
            f"📌 Trạng thái: `{fixture_status(fixture)}`"
        ),
        color=discord.Color.blue()
    )

    # Logos
    home_logo = home.get("logo")
    away_logo = away.get("logo")

    if home_logo:
        embed.set_thumbnail(url=home_logo)

    # Score nếu đã đá
    goals = fixture.get("goals", {})

    gh = goals.get("home")
    ga = goals.get("away")

    if gh is not None or ga is not None:

        embed.add_field(
            name="📊 Tỉ số hiện tại",
            value=f"**{gh} - {ga}**",
            inline=False
        )

    # Form
    embed.add_field(
        name=f"📈 {home['name']} - 5 trận",
        value=form_text(home_recent, home_id),
        inline=True
    )

    embed.add_field(
        name=f"📉 {away['name']} - 5 trận",
        value=form_text(away_recent, away_id),
        inline=True
    )

    # H2H
    embed.add_field(
        name="🤝 H2H gần đây",
        value=h2h_text(h2h, home_id, away_id)[:1024],
        inline=False
    )

    # Prediction
    embed.add_field(
        name="🤖 API-Football Prediction",
        value=prediction_text(prediction)[:1024],
        inline=False
    )

    embed.set_footer(
        text=(
            "Dữ liệu từ API-Football • "
            "Prediction không phải kết quả chắc chắn"
        )
    )

    return embed


# =========================================================
# MATCH SELECT
# =========================================================

class MatchSelect(discord.ui.Select):

    def __init__(self, fixtures):

        options = []

        for fixture in fixtures[:25]:

            fixture_id = fixture["fixture"]["id"]

            home = fixture["teams"]["home"]["name"]
            away = fixture["teams"]["away"]["name"]

            status = fixture_status(fixture)

            label = f"{home} vs {away}"

            if len(label) > 100:
                label = label[:97] + "..."

            options.append(
                discord.SelectOption(
                    label=label,
                    description=f"ID {fixture_id} • {status}",
                    value=str(fixture_id)
                )
            )

        super().__init__(
            placeholder="⚽ Chọn trận để xem phân tích",
            min_values=1,
            max_values=1,
            options=options
        )

        self.fixtures = fixtures

    async def callback(self, interaction: discord.Interaction):

        fixture_id = int(self.values[0])

        fixture = next(
            (
                f for f in self.fixtures
                if f["fixture"]["id"] == fixture_id
            ),
            None
        )

        if fixture is None:

            await interaction.response.send_message(
                "❌ Không tìm thấy trận.",
                ephemeral=True
            )

            return

        await interaction.response.defer()

        try:

            embed = await analyze_fixture(fixture)

            await interaction.followup.send(
                embed=embed
            )

        except Exception as e:

            await interaction.followup.send(
                f"❌ Không thể phân tích trận này.\n"
                f"`{str(e)[:500]}`",
                ephemeral=True
            )


class MatchView(discord.ui.View):

    def __init__(self, fixtures):

        super().__init__(timeout=300)

        self.add_item(
            MatchSelect(fixtures)
        )


# =========================================================
# COMMAND: KHOI NGHIEP
# =========================================================

@tree.command(
    name="khoinghiep",
    description="Nhận 10.000 tiền ảo Discord."
)
async def khoinghiep(interaction: discord.Interaction):

    balance = get_balance(interaction.user.id)

    await interaction.response.send_message(
        f"💰 Số dư của m: **{balance:,} xu**"
    )


# =========================================================
# COMMAND: VI
# =========================================================

@tree.command(
    name="vi",
    description="Xem số dư tiền ảo."
)
async def vi(interaction: discord.Interaction):

    balance = get_balance(interaction.user.id)

    await interaction.response.send_message(
        f"👛 **Ví của {interaction.user.display_name}**\n"
        f"💰 {balance:,} xu"
    )


# =========================================================
# COMMAND: SOI
# =========================================================

@tree.command(
    name="soi",
    description="Xem các trận bóng thật theo ngày."
)
@app_commands.describe(
    ngay="Ngày YYYY-MM-DD. Bỏ trống = hôm nay UTC."
)
async def soi(
    interaction: discord.Interaction,
    ngay: Optional[str] = None
):

    await interaction.response.defer()

    if not ngay:

        target_date = datetime.utcnow().date()

    else:

        try:
            target_date = date.fromisoformat(ngay)

        except ValueError:

            await interaction.followup.send(
                "❌ Ngày phải có dạng `YYYY-MM-DD`.\n"
                "Ví dụ: `2026-09-27`"
            )

            return

    try:

        fixtures = await get_fixtures_by_date(
            target_date.isoformat()
        )

    except Exception as e:

        await interaction.followup.send(
            f"❌ API-Football lỗi:\n`{str(e)[:500]}`"
        )

        return

    if not fixtures:

        await interaction.followup.send(
            f"⚽ Không có trận nào trong ngày "
            f"**{target_date.isoformat()}**."
        )

        return

    # Tối đa 25 trận cho Select
    shown = fixtures[:25]

    embed = discord.Embed(
        title=f"⚽ Trận bóng {target_date.isoformat()}",
        description=(
            f"Tìm thấy **{len(fixtures)} trận**.\n\n"
            f"Chọn một trận bên dưới để xem:\n"
            f"• Logo 2 đội\n"
            f"• 5 trận gần nhất\n"
            f"• H2H\n"
            f"• Prediction của API-Football"
        ),
        color=discord.Color.green()
    )

    if len(fixtures) > 25:

        embed.set_footer(
            text=(
                f"Đang hiển thị 25/{len(fixtures)} trận "
                f"do giới hạn Select của Discord."
            )
        )

    else:

        embed.set_footer(
            text="Dữ liệu trận lấy trực tiếp từ API-Football."
        )

    await interaction.followup.send(
        embed=embed,
        view=MatchView(shown)
    )


# =========================================================
# COMMAND: CADO
# =========================================================

@tree.command(
    name="cado",
    description="Cược tiền ảo vào một trận."
)
@app_commands.describe(
    fixture_id="ID trận lấy từ /soi",
    lua_chon="home = đội nhà | away = đội khách | draw = hòa",
    so_tien="Số xu muốn cược"
)
@app_commands.choices(
    lua_chon=[
        app_commands.Choice(
            name="Đội nhà (HOME)",
            value="home"
        ),
        app_commands.Choice(
            name="Đội khách (AWAY)",
            value="away"
        ),
        app_commands.Choice(
            name="Hòa (DRAW)",
            value="draw"
        ),
    ]
)
async def cado(
    interaction: discord.Interaction,
    fixture_id: int,
    lua_chon: str,
    so_tien: int
):

    if so_tien < MIN_BET:

        await interaction.response.send_message(
            f"❌ Cược tối thiểu là **{MIN_BET:,} xu**.",
            ephemeral=True
        )

        return

    if so_tien > MAX_BET:

        await interaction.response.send_message(
            f"❌ Cược tối đa là **{MAX_BET:,} xu**.",
            ephemeral=True
        )

        return

    balance = get_balance(interaction.user.id)

    if so_tien > balance:

        await interaction.response.send_message(
            f"❌ Không đủ xu.\n"
            f"Ví: **{balance:,}**\n"
            f"Cược: **{so_tien:,}**",
            ephemeral=True
        )

        return

    await interaction.response.defer(ephemeral=True)

    try:

        data = await football_api(
            "fixtures",
            {
                "id": fixture_id
            },
            cache_seconds=30
        )

        fixtures = data.get("response", [])

    except Exception as e:

        await interaction.followup.send(
            f"❌ Không lấy được trận:\n`{str(e)[:400]}`",
            ephemeral=True
        )

        return

    if not fixtures:

        await interaction.followup.send(
            "❌ Fixture ID không tồn tại.",
            ephemeral=True
        )

        return

    fixture = fixtures[0]

    status = fixture_status(fixture)

    if not is_not_started(status):

        await interaction.followup.send(
            "❌ Trận này đã bắt đầu hoặc không còn ở trạng thái "
            "`NS/TBD`, không nhận cược mới.",
            ephemeral=True
        )

        return

    home = fixture["teams"]["home"]
    away = fixture["teams"]["away"]

    if lua_chon == "home":

        choice_name = home["name"]

    elif lua_chon == "away":

        choice_name = away["name"]

    else:

        choice_name = "Hòa"

    # Trừ tiền
    set_balance(
        interaction.user.id,
        balance - so_tien
    )

    create_bet(
        interaction.user.id,
        fixture_id,
        lua_chon,
        choice_name,
        so_tien
    )

    new_balance = balance - so_tien

    embed = discord.Embed(
        title="🎟️ Đặt cược thành công",
        color=discord.Color.orange()
    )

    embed.add_field(
        name="⚽ Trận",
        value=f"{home['name']} vs {away['name']}",
        inline=False
    )

    embed.add_field(
        name="🎯 Lựa chọn",
        value=choice_name,
        inline=True
    )

    embed.add_field(
        name="💰 Tiền cược",
        value=f"{so_tien:,} xu",
        inline=True
    )

    embed.add_field(
        name="👛 Số dư",
        value=f"{new_balance:,} xu",
        inline=True
    )

    embed.set_footer(
        text="Đây chỉ là tiền ảo trong Discord."
    )

    await interaction.followup.send(
        embed=embed,
        ephemeral=True
    )


# =========================================================
# COMMAND: CUOC CUA TOI
# =========================================================

@tree.command(
    name="cuoc_cua_toi",
    description="Xem các cược của bạn."
)
async def cuoc_cua_toi(
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
        (interaction.user.id,)
    ).fetchall()

    if not rows:

        await interaction.response.send_message(
            "📭 M chưa có cược nào."
        )

        return

    lines = []

    for row in rows:

        status = row["status"]

        if status == "pending":
            icon = "⏳"
        elif status == "won":
            icon = "🟢"
        elif status == "lost":
            icon = "🔴"
        elif status == "refund":
            icon = "🟡"
        else:
            icon = "⚪"

        lines.append(
            f"{icon} `#{row['id']}` "
            f"Fixture `{row['fixture_id']}` • "
            f"**{row['choice_name']}** • "
            f"{row['amount']:,} xu"
        )

    await interaction.response.send_message(
        "🎟️ **10 cược gần nhất**\n\n"
        + "\n".join(lines)
    )


# =========================================================
# SETTLEMENT RESULT
# =========================================================

def get_match_result(fixture):

    goals = fixture.get("goals", {})

    home_goals = goals.get("home")
    away_goals = goals.get("away")

    if home_goals is None or away_goals is None:
        return None

    home_goals = safe_int(home_goals)
    away_goals = safe_int(away_goals)

    if home_goals > away_goals:
        return "home"

    if away_goals > home_goals:
        return "away"

    return "draw"


# =========================================================
# COMMAND: KET TOAN
# =========================================================

@tree.command(
    name="kettoan",
    description="Admin chốt kết quả một trận."
)
@app_commands.describe(
    fixture_id="ID trận cần chốt"
)
async def kettoan(
    interaction: discord.Interaction,
    fixture_id: int
):

    # Chỉ admin
    if not interaction.user.guild_permissions.administrator:

        await interaction.response.send_message(
            "❌ Chỉ Administrator mới được dùng lệnh này.",
            ephemeral=True
        )

        return

    await interaction.response.defer()

    try:

        data = await football_api(
            "fixtures",
            {
                "id": fixture_id
            },
            cache_seconds=10
        )

        fixtures = data.get("response", [])

    except Exception as e:

        await interaction.followup.send(
            f"❌ API lỗi:\n`{str(e)[:500]}`"
        )

        return

    if not fixtures:

        await interaction.followup.send(
            "❌ Không tìm thấy fixture."
        )

        return

    fixture = fixtures[0]

    status = fixture_status(fixture)

    if not is_finished_status(status):

        await interaction.followup.send(
            f"❌ Trận chưa kết thúc.\n"
            f"Status hiện tại: `{status}`"
        )

        return

    result = get_match_result(fixture)

    if result is None:

        await interaction.followup.send(
            "❌ Không đọc được kết quả trận."
        )

        return

    bets = get_pending_bets_for_fixture(
        fixture_id
    )

    if not bets:

        await interaction.followup.send(
            "ℹ️ Không có cược pending cho trận này."
        )

        return

    home = fixture["teams"]["home"]["name"]
    away = fixture["teams"]["away"]["name"]

    goals = fixture["goals"]

    gh = goals.get("home")
    ga = goals.get("away")

    winners = 0
    losers = 0
    refunds = 0
    total_paid = 0

    for bet in bets:

        if bet["choice"] == result:

            payout = bet["amount"] * PAYOUT_MULTIPLIER

            add_balance(
                bet["user_id"],
                payout
            )

            mark_bet(
                bet["id"],
                "won"
            )

            winners += 1
            total_paid += payout

        else:

            mark_bet(
                bet["id"],
                "lost"
            )

            losers += 1

    if result == "home":

        result_text = f"🏠 {home}"

    elif result == "away":

        result_text = f"✈️ {away}"

    else:

        result_text = "🤝 Hòa"

    embed = discord.Embed(
        title="📋 Đã kết toán trận",
        color=discord.Color.green()
    )

    embed.add_field(
        name="⚽ Trận",
        value=f"{home} **{gh}-{ga}** {away}",
        inline=False
    )

    embed.add_field(
        name="🏆 Kết quả",
        value=result_text,
        inline=True
    )

    embed.add_field(
        name="🟢 Thắng",
        value=str(winners),
        inline=True
    )

    embed.add_field(
        name="🔴 Thua",
        value=str(losers),
        inline=True
    )

    embed.add_field(
        name="💰 Xu đã trả",
        value=f"{total_paid:,}",
        inline=False
    )

    embed.set_footer(
        text="Tiền thưởng là tiền ảo trong Discord."
    )

    await interaction.followup.send(
        embed=embed
    )


# =========================================================
# COMMAND: PING
# =========================================================

@tree.command(
    name="ping",
    description="Kiểm tra bot."
)
async def ping(interaction: discord.Interaction):

    latency = round(
        bot.latency * 1000
    )

    await interaction.response.send_message(
        f"🏓 Pong!\n"
        f"📡 Ping: **{latency}ms**"
    )


# =========================================================
# READY
# =========================================================

@bot.event
async def on_ready():

    print("=" * 50)

    print(
        f"🤖 Bot: {bot.user} "
        f"(ID: {bot.user.id})"
    )

    print(
        f"🏠 Guild ID: {GUILD_ID}"
    )

    print(
        f"🌐 API: {API_BASE}"
    )

    print("=" * 50)

    try:

        guild = discord.Object(
            id=GUILD_ID
        )

        # Sync riêng server để slash command xuất hiện nhanh
        synced = await tree.sync(
            guild=guild
        )

        print(
            f"✅ Synced {len(synced)} commands "
            f"to guild {GUILD_ID}"
        )

    except Exception as e:

        print(
            f"❌ Sync command lỗi: {e}"
        )


# =========================================================
# GLOBAL ERROR HANDLER
# =========================================================

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError
):

    print(
        f"COMMAND ERROR: {repr(error)}"
    )

    message = (
        "❌ Có lỗi khi chạy lệnh.\n"
        "Kiểm tra console/log của bot."
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


# =========================================================
# START BOT
# =========================================================

if __name__ == "__main__":

    print("🚀 Starting football Discord bot...")

    bot.run(DISCORD_TOKEN)

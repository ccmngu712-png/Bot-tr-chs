import os
import asyncio
import sqlite3
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks


# =========================================================
# CONFIG
# =========================================================

TOKEN = os.getenv("DISCORD_TOKEN")
FOOTBALL_DATA_TOKEN = os.getenv("FOOTBALL_DATA_TOKEN")

GUILD_ID = 1551547049600618538

TIMEZONE = ZoneInfo("Asia/Ho_Chi_Minh")

MAN_CITY_ID = 65
DEFAULT_COMPETITION = os.getenv("FOOTBALL_ALERT_COMPETITION", "CL")

ALERT_CHANNEL_RAW = os.getenv("FOOTBALL_ALERT_CHANNEL_ID", "").strip()

DB_FILE = "football_bot.db"

STARTING_MONEY = 1000

API_BASE = "https://api.football-data.org/v4"


if not TOKEN:
    raise RuntimeError("Thiếu DISCORD_TOKEN")

if not FOOTBALL_DATA_TOKEN:
    raise RuntimeError("Thiếu FOOTBALL_DATA_TOKEN")


# =========================================================
# DATABASE
# =========================================================

db = sqlite3.connect(DB_FILE, check_same_thread=False)
db.row_factory = sqlite3.Row

db.execute("""
CREATE TABLE IF NOT EXISTS wallets (
    user_id INTEGER PRIMARY KEY,
    balance INTEGER NOT NULL DEFAULT 0,
    started INTEGER NOT NULL DEFAULT 0
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS bets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    match_id INTEGER NOT NULL,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    choice TEXT NOT NULL,
    amount INTEGER NOT NULL,
    odds REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    payout INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    settled_at TEXT
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS sent_alerts (
    alert_key TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
)
""")

db.commit()


# =========================================================
# HELPERS
# =========================================================

def now_vn():
    return datetime.now(TIMEZONE)


def parse_utc(value):
    if not value:
        return None

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(TIMEZONE)
    except Exception:
        return None


def format_time(value):
    dt = parse_utc(value) if isinstance(value, str) else value

    if not dt:
        return "Không rõ"

    return dt.strftime("%d/%m/%Y %H:%M")


def get_team_name(team):
    if not team:
        return "Unknown"

    return team.get("name") or team.get("shortName") or "Unknown"


def get_team_id(team):
    if not team:
        return None

    return team.get("id")


def get_logo(team):
    if not team:
        return None

    return team.get("crest")


def match_score(match):
    score = match.get("score", {})

    full_time = score.get("fullTime", {})

    home = full_time.get("home")
    away = full_time.get("away")

    return home, away


def match_status(match):
    return match.get("status", "UNKNOWN")


def is_finished(match):
    return match_status(match) in {
        "FINISHED",
        "AWARDED"
    }


def is_cancelled(match):
    return match_status(match) in {
        "CANCELLED",
        "POSTPONED",
        "SUSPENDED"
    }


def match_datetime(match):
    return parse_utc(
        match.get("utcDate")
    )


def minutes_until(match):
    dt = match_datetime(match)

    if not dt:
        return None

    return int(
        (dt - now_vn()).total_seconds() / 60
    )


def money(value):
    return f"{value:,} 💰"


# =========================================================
# FOOTBALL DATA API
# =========================================================

class FootballDataAPI:

    def __init__(self):
        self.session = None

        self.cache = {}

        self.request_times = []

        self.total_requests = 0

        self.last_quota = None

    async def start(self):
        if not self.session:
            self.session = aiohttp.ClientSession(
                headers={
                    "X-Auth-Token": FOOTBALL_DATA_TOKEN
                }
            )

    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None

    async def rate_limit(self):

        now = asyncio.get_running_loop().time()

        self.request_times = [
            t for t in self.request_times
            if now - t < 60
        ]

        # giữ dưới giới hạn Free Tier 10 request/min
        if len(self.request_times) >= 9:
            wait = 60 - (now - self.request_times[0]) + 0.2

            if wait > 0:
                await asyncio.sleep(wait)

        now = asyncio.get_running_loop().time()

        self.request_times.append(now)

    async def get(
        self,
        endpoint,
        params=None,
        cache_seconds=0
    ):

        if not self.session:
            await self.start()

        params = params or {}

        cache_key = (
            endpoint,
            tuple(sorted(params.items()))
        )

        if cache_seconds > 0:
            cached = self.cache.get(cache_key)

            if cached:
                timestamp, data = cached

                if (
                    asyncio.get_running_loop().time()
                    - timestamp
                    < cache_seconds
                ):
                    return data

        await self.rate_limit()

        url = API_BASE + endpoint

        try:

            async with self.session.get(
                url,
                params=params,
                timeout=20
            ) as response:

                self.total_requests += 1

                remaining = response.headers.get(
                    "X-Requests-Available"
                )

                if remaining is not None:
                    try:
                        self.last_quota = int(remaining)
                    except Exception:
                        pass

                if response.status == 429:
                    print("Football API: 429 rate limit")
                    return None

                if response.status == 403:
                    print("Football API: 403 forbidden")
                    return None

                if response.status >= 400:
                    text = await response.text()

                    print(
                        "Football API error:",
                        response.status,
                        text[:500]
                    )

                    return None

                data = await response.json()

                if cache_seconds > 0:
                    self.cache[cache_key] = (
                        asyncio.get_running_loop().time(),
                        data
                    )

                return data

        except Exception as e:

            print(
                "Football API exception:",
                repr(e)
            )

            return None


football = FootballDataAPI()


# =========================================================
# FOOTBALL FUNCTIONS
# =========================================================

async def get_mancity_matches():

    today = now_vn().date()

    date_from = (
        today - timedelta(days=2)
    ).isoformat()

    date_to = (
        today + timedelta(days=30)
    ).isoformat()

    data = await football.get(
        f"/teams/{MAN_CITY_ID}/matches",
        params={
            "dateFrom": date_from,
            "dateTo": date_to,
            "limit": 100
        },
        cache_seconds=300
    )

    if not data:
        return []

    return data.get("matches", [])


async def get_competition_matches(
    code="CL"
):

    today = now_vn().date()

    date_from = (
        today - timedelta(days=2)
    ).isoformat()

    date_to = (
        today + timedelta(days=30)
    ).isoformat()

    data = await football.get(
        f"/competitions/{code}/matches",
        params={
            "dateFrom": date_from,
            "dateTo": date_to,
            "limit": 100
        },
        cache_seconds=300
    )

    if not data:
        return []

    return data.get("matches", [])


async def get_team_recent(team_id):

    if not team_id:
        return []

    data = await football.get(
        f"/teams/{team_id}/matches",
        params={
            "status": "FINISHED",
            "limit": 5
        },
        cache_seconds=300
    )

    if not data:
        return []

    return data.get("matches", [])


async def get_all_relevant_matches():

    city_matches = await get_mancity_matches()

    cl_matches = await get_competition_matches(
        DEFAULT_COMPETITION
    )

    combined = {}

    for match in city_matches:
        if match.get("id"):
            combined[match["id"]] = match

    for match in cl_matches:
        if match.get("id"):
            combined[match["id"]] = match

    return list(combined.values())


# =========================================================
# SIMPLE PREDICTION MODEL
# =========================================================

def poisson_probability(
    k,
    lam
):

    if lam <= 0:
        return 0

    return (
        math.exp(-lam)
        * (lam ** k)
        / math.factorial(k)
    )


async def team_average_goals(team_id):

    matches = await get_team_recent(team_id)

    if not matches:
        return 1.2, 1.2

    scored = 0
    conceded = 0
    games = 0

    for match in matches:

        home = match.get("homeTeam", {})
        away = match.get("awayTeam", {})

        home_id = home.get("id")
        away_id = away.get("id")

        h, a = match_score(match)

        if h is None or a is None:
            continue

        games += 1

        if team_id == home_id:

            scored += h
            conceded += a

        elif team_id == away_id:

            scored += a
            conceded += h

    if games == 0:
        return 1.2, 1.2

    return (
        scored / games,
        conceded / games
    )


async def predict_match(match):

    home = match.get("homeTeam", {})
    away = match.get("awayTeam", {})

    home_id = get_team_id(home)
    away_id = get_team_id(away)

    home_scored, home_conceded = (
        await team_average_goals(home_id)
    )

    away_scored, away_conceded = (
        await team_average_goals(away_id)
    )

    expected_home = (
        home_scored + away_conceded
    ) / 2

    expected_away = (
        away_scored + home_conceded
    ) / 2

    expected_home = max(
        0.2,
        min(expected_home, 4.5)
    )

    expected_away = max(
        0.2,
        min(expected_away, 4.5)
    )

    home_win = 0
    draw = 0
    away_win = 0

    for h in range(0, 8):

        for a in range(0, 8):

            p = (
                poisson_probability(
                    h,
                    expected_home
                )
                *
                poisson_probability(
                    a,
                    expected_away
                )
            )

            if h > a:
                home_win += p

            elif h == a:
                draw += p

            else:
                away_win += p

    total = (
        home_win
        + draw
        + away_win
    )

    if total <= 0:
        return {
            "home": 33.3,
            "draw": 33.3,
            "away": 33.4,
            "expected_home": expected_home,
            "expected_away": expected_away
        }

    return {
        "home": home_win / total * 100,
        "draw": draw / total * 100,
        "away": away_win / total * 100,
        "expected_home": expected_home,
        "expected_away": expected_away
    }


# =========================================================
# WALLET
# =========================================================

def get_wallet(user_id):

    row = db.execute(
        """
        SELECT *
        FROM wallets
        WHERE user_id = ?
        """,
        (user_id,)
    ).fetchone()

    if not row:

        db.execute(
            """
            INSERT INTO wallets
            (user_id, balance, started)
            VALUES (?, 0, 0)
            """,
            (user_id,)
        )

        db.commit()

        return {
            "balance": 0,
            "started": 0
        }

    return dict(row)


def add_money(
    user_id,
    amount
):

    db.execute(
        """
        UPDATE wallets
        SET balance = balance + ?
        WHERE user_id = ?
        """,
        (amount, user_id)
    )

    db.commit()


def remove_money(
    user_id,
    amount
):

    cur = db.execute(
        """
        UPDATE wallets
        SET balance = balance - ?
        WHERE user_id = ?
        AND balance >= ?
        """,
        (
            amount,
            user_id,
            amount
        )
    )

    db.commit()

    return cur.rowcount > 0


# =========================================================
# BET ODDS
# =========================================================

def calculate_odds(
    probability
):

    probability = max(
        1,
        min(99, probability)
    )

    # margin 8%
    odds = 0.92 / (
        probability / 100
    )

    return round(
        max(1.05, odds),
        2
    )


# =========================================================
# BET VIEW
# =========================================================

class BetAmountModal(discord.ui.Modal):

    def __init__(
        self,
        match,
        choice,
        odds
    ):

        super().__init__(
            title="Nhập tiền cược"
        )

        self.match = match
        self.choice = choice
        self.odds = odds

        self.amount = discord.ui.TextInput(
            label="Số tiền muốn cược",
            placeholder="Ví dụ: 100",
            required=True,
            min_length=1,
            max_length=12
        )

        self.add_item(self.amount)

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        try:
            amount = int(
                self.amount.value.replace(
                    ",",
                    ""
                ).strip()
            )

        except ValueError:

            await interaction.response.send_message(
                "❌ Số tiền không hợp lệ.",
                ephemeral=True
            )

            return

        if amount <= 0:

            await interaction.response.send_message(
                "❌ Số tiền phải lớn hơn 0.",
                ephemeral=True
            )

            return

        if amount > 1_000_000:

            await interaction.response.send_message(
                "❌ Mỗi lượt cược tối đa 1,000,000 💰.",
                ephemeral=True
            )

            return

        user_id = interaction.user.id

        wallet = get_wallet(user_id)

        if wallet["started"] == 0:

            await interaction.response.send_message(
                "❌ M chưa nhận vốn. Dùng `/khoinghiep` trước.",
                ephemeral=True
            )

            return

        if wallet["balance"] < amount:

            await interaction.response.send_message(
                f"❌ Không đủ tiền.\n"
                f"Ví hiện tại: {money(wallet['balance'])}",
                ephemeral=True
            )

            return

        # Không cho cược sau khi trận đã bắt đầu
        mins = minutes_until(self.match)

        if mins is None or mins <= 0:

            await interaction.response.send_message(
                "❌ Trận này đã bắt đầu hoặc không xác định được giờ đá.",
                ephemeral=True
            )

            return

        # Trừ tiền
        if not remove_money(
            user_id,
            amount
        ):

            await interaction.response.send_message(
                "❌ Không thể trừ tiền cược.",
                ephemeral=True
            )

            return

        home = get_team_name(
            self.match.get("homeTeam")
        )

        away = get_team_name(
            self.match.get("awayTeam")
        )

        match_id = self.match.get("id")

        created = now_vn().isoformat()

        db.execute(
            """
            INSERT INTO bets
            (
                user_id,
                match_id,
                home_team,
                away_team,
                choice,
                amount,
                odds,
                status,
                payout,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', 0, ?)
            """,
            (
                user_id,
                match_id,
                home,
                away,
                self.choice,
                amount,
                self.odds,
                created
            )
        )

        db.commit()

        new_balance = get_wallet(
            user_id
        )["balance"]

        potential = int(
            amount * self.odds
        )

        embed = discord.Embed(
            title="🎫 ĐẶT CƯỢC THÀNH CÔNG",
            color=discord.Color.green()
        )

        embed.add_field(
            name="⚽ Trận đấu",
            value=f"**{home}** vs **{away}**",
            inline=False
        )

        embed.add_field(
            name="🎯 Lựa chọn",
            value=self.choice,
            inline=True
        )

        embed.add_field(
            name="💰 Tiền cược",
            value=money(amount),
            inline=True
        )

        embed.add_field(
            name="📈 Tỷ lệ",
            value=f"`{self.odds:.2f}`",
            inline=True
        )

        embed.add_field(
            name="🏆 Nhận tối đa",
            value=money(potential),
            inline=True
        )

        embed.add_field(
            name="💳 Số dư còn lại",
            value=money(new_balance),
            inline=True
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=False
        )


class BetChoiceView(discord.ui.View):

    def __init__(
        self,
        match,
        prediction
    ):

        super().__init__(
            timeout=180
        )

        self.match = match
        self.prediction = prediction

        home = get_team_name(
            match.get("homeTeam")
        )

        away = get_team_name(
            match.get("awayTeam")
        )

        home_odds = calculate_odds(
            prediction["home"]
        )

        draw_odds = calculate_odds(
            prediction["draw"]
        )

        away_odds = calculate_odds(
            prediction["away"]
        )

        self.home_button.label = (
            f"{home[:35]} | {home_odds:.2f}"
        )

        self.draw_button.label = (
            f"Hòa | {draw_odds:.2f}"
        )

        self.away_button.label = (
            f"{away[:35]} | {away_odds:.2f}"
        )

    @discord.ui.button(
        label="Đội nhà",
        style=discord.ButtonStyle.primary
    )
    async def home_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        odds = calculate_odds(
            self.prediction["home"]
        )

        await interaction.response.send_modal(
            BetAmountModal(
                self.match,
                "Đội nhà thắng",
                odds
            )
        )

    @discord.ui.button(
        label="Hòa",
        style=discord.ButtonStyle.secondary
    )
    async def draw_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        odds = calculate_odds(
            self.prediction["draw"]
        )

        await interaction.response.send_modal(
            BetAmountModal(
                self.match,
                "Hòa",
                odds
            )
        )

    @discord.ui.button(
        label="Đội khách",
        style=discord.ButtonStyle.danger
    )
    async def away_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        odds = calculate_odds(
            self.prediction["away"]
        )

        await interaction.response.send_modal(
            BetAmountModal(
                self.match,
                "Đội khách thắng",
                odds
            )
        )


# =========================================================
# MATCH SELECT
# =========================================================

class MatchSelect(
    discord.ui.Select
):

    def __init__(
        self,
        matches
    ):

        options = []

        for match in matches[:25]:

            home = get_team_name(
                match.get("homeTeam")
            )

            away = get_team_name(
                match.get("awayTeam")
            )

            dt = match_datetime(match)

            time_text = (
                dt.strftime("%d/%m %H:%M")
                if dt
                else "?"
            )

            options.append(
                discord.SelectOption(
                    label=f"{home[:45]} vs {away[:45]}",
                    description=time_text,
                    value=str(
                        match.get("id")
                    )
                )
            )

        super().__init__(
            placeholder="⚽ Chọn trận muốn cược...",
            min_values=1,
            max_values=1,
            options=options
        )

        self.matches = {
            str(m.get("id")): m
            for m in matches
        }

    async def callback(
        self,
        interaction: discord.Interaction
    ):

        match = self.matches.get(
            self.values[0]
        )

        if not match:

            await interaction.response.send_message(
                "❌ Không tìm thấy trận.",
                ephemeral=True
            )

            return

        if is_finished(match) or is_cancelled(match):

            await interaction.response.send_message(
                "❌ Trận này không thể cược.",
                ephemeral=True
            )

            return

        prediction = await predict_match(
            match
        )

        home = get_team_name(
            match.get("homeTeam")
        )

        away = get_team_name(
            match.get("awayTeam")
        )

        dt = match_datetime(match)

        embed = discord.Embed(
            title="🎯 CHỌN CỬA CƯỢC",
            description=(
                f"**{home}**\n"
                f"🆚\n"
                f"**{away}**\n\n"
                f"🕐 {format_time(dt)}"
            ),
            color=discord.Color.blurple()
        )

        embed.add_field(
            name="📊 Mô hình thống kê",
            value=(
                f"🏠 {prediction['home']:.1f}%\n"
                f"🤝 {prediction['draw']:.1f}%\n"
                f"✈️ {prediction['away']:.1f}%"
            ),
            inline=True
        )

        embed.add_field(
            name="⚽ Bàn thắng kỳ vọng",
            value=(
                f"{prediction['expected_home']:.2f}"
                f" - "
                f"{prediction['expected_away']:.2f}"
            ),
            inline=True
        )

        embed.set_footer(
            text="Đây là ước tính thống kê, không đảm bảo kết quả."
        )

        await interaction.response.edit_message(
            embed=embed,
            view=BetChoiceView(
                match,
                prediction
            )
        )


class MatchSelectView(
    discord.ui.View
):

    def __init__(
        self,
        matches
    ):

        super().__init__(
            timeout=180
        )

        self.add_item(
            MatchSelect(matches)
        )


# =========================================================
# BOT
# =========================================================

intents = discord.Intents.default()

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


# =========================================================
# /PING
# =========================================================

@bot.tree.command(
    name="ping",
    description="Kiểm tra bot"
)
async def ping(
    interaction: discord.Interaction
):

    await interaction.response.send_message(
        f"🏓 Pong! `{round(bot.latency * 1000)}ms`"
    )


# =========================================================
# /APIQUOTA
# =========================================================

@bot.tree.command(
    name="apiquota",
    description="Xem thông tin API bóng đá"
)
async def apiquota(
    interaction: discord.Interaction
):

    await interaction.response.defer(
        ephemeral=True
    )

    await football.get(
        "/competitions/CL",
        cache_seconds=30
    )

    remaining = football.last_quota

    if remaining is None:
        quota = "Không đọc được"
    else:
        quota = str(remaining)

    await interaction.followup.send(
        f"📡 Football API\n"
        f"Request đã dùng: `{football.total_requests}`\n"
        f"Request còn lại theo header: `{quota}`",
        ephemeral=True
    )


# =========================================================
# /KHOINGHIEP
# =========================================================

@bot.tree.command(
    name="khoinghiep",
    description="Nhận vốn tiền ảo ban đầu"
)
async def khoinghiep(
    interaction: discord.Interaction
):

    user_id = interaction.user.id

    wallet = get_wallet(
        user_id
    )

    if wallet["started"]:

        await interaction.response.send_message(
            f"❌ M đã nhận vốn rồi.\n"
            f"💰 Số dư: {money(wallet['balance'])}",
            ephemeral=True
        )

        return

    db.execute(
        """
        UPDATE wallets
        SET balance = ?,
            started = 1
        WHERE user_id = ?
        """,
        (
            STARTING_MONEY,
            user_id
        )
    )

    db.commit()

    await interaction.response.send_message(
        f"🎉 Chúc mừng!\n\n"
        f"M nhận được **{money(STARTING_MONEY)}** vốn ban đầu.\n"
        f"Dùng `/cuoc` để đặt cược trận đấu.",
        ephemeral=False
    )


# =========================================================
# /VI
# =========================================================

@bot.tree.command(
    name="vi",
    description="Xem số dư và lịch sử cược"
)
async def vi(
    interaction: discord.Interaction
):

    user_id = interaction.user.id

    wallet = get_wallet(
        user_id
    )

    total_bets = db.execute(
        """
        SELECT COUNT(*)
        FROM bets
        WHERE user_id = ?
        """,
        (user_id,)
    ).fetchone()[0]

    pending = db.execute(
        """
        SELECT COUNT(*)
        FROM bets
        WHERE user_id = ?
        AND status = 'PENDING'
        """,
        (user_id,)
    ).fetchone()[0]

    embed = discord.Embed(
        title=f"💳 VÍ CỦA {interaction.user.display_name}",
        color=discord.Color.gold()
    )

    embed.add_field(
        name="💰 Số dư",
        value=f"**{money(wallet['balance'])}**",
        inline=False
    )

    embed.add_field(
        name="🎫 Tổng lượt cược",
        value=str(total_bets),
        inline=True
    )

    embed.add_field(
        name="⏳ Đang chờ",
        value=str(pending),
        inline=True
    )

    if not wallet["started"]:
        embed.set_footer(
            text="Dùng /khoinghiep để nhận vốn ban đầu."
        )

    await interaction.response.send_message(
        embed=embed
    )


# =========================================================
# /CUOC
# =========================================================

@bot.tree.command(
    name="cuoc",
    description="Chọn trận đấu và đặt cược bằng tiền ảo"
)
async def cuoc(
    interaction: discord.Interaction
):

    wallet = get_wallet(
        interaction.user.id
    )

    if not wallet["started"]:

        await interaction.response.send_message(
            "❌ M chưa có vốn.\n"
            "Dùng `/khoinghiep` trước.",
            ephemeral=True
        )

        return

    if wallet["balance"] <= 0:

        await interaction.response.send_message(
            "❌ Ví hết tiền.\n"
            "Không thể đặt cược.",
            ephemeral=True
        )

        return

    await interaction.response.defer()

    matches = await get_all_relevant_matches()

    future = []

    now = now_vn()

    for match in matches:

        dt = match_datetime(match)

        if not dt:
            continue

        if dt <= now:
            continue

        if is_finished(match):
            continue

        if is_cancelled(match):
            continue

        future.append(match)

    future.sort(
        key=lambda m: (
            match_datetime(m)
            or datetime.max.replace(
                tzinfo=TIMEZONE
            )
        )
    )

    if not future:

        await interaction.followup.send(
            "❌ Hiện không tìm thấy trận sắp đá "
            "trong phạm vi dữ liệu API.",
            ephemeral=True
        )

        return

    future = future[:25]

    embed = discord.Embed(
        title="🎰 ĐẶT CƯỢC TRẬN ĐẤU",
        description=(
            f"💰 Số dư của m: "
            f"**{money(wallet['balance'])}**\n\n"
            "Chọn trận ở menu bên dưới."
        ),
        color=discord.Color.green()
    )

    await interaction.followup.send(
        embed=embed,
        view=MatchSelectView(future)
    )


# =========================================================
# /KETTOAN
# =========================================================

@bot.tree.command(
    name="kettoan",
    description="Kiểm tra và thanh toán các cược đã kết thúc"
)
async def kettoan(
    interaction: discord.Interaction
):

    await interaction.response.defer()

    user_id = interaction.user.id

    pending = db.execute(
        """
        SELECT *
        FROM bets
        WHERE user_id = ?
        AND status = 'PENDING'
        """,
        (user_id,)
    ).fetchall()

    if not pending:

        await interaction.followup.send(
            "ℹ️ M không có cược nào đang chờ kết quả."
        )

        return

    results = []

    for bet in pending:

        data = await football.get(
            f"/matches/{bet['match_id']",
            cache_seconds=60
        )

        if not data:
            continue

        match = data.get("match")

        if not match:
            continue

        if not is_finished(match):
            continue

        home_score, away_score = match_score(
            match
        )

        if home_score is None or away_score is None:
            continue

        if home_score > away_score:
            result = "HOME"

        elif home_score < away_score:
            result = "AWAY"

        else:
            result = "DRAW"

        choice = bet["choice"]

        won = False

        if (
            choice == "Đội nhà thắng"
            and result == "HOME"
        ):
            won = True

        elif (
            choice == "Đội khách thắng"
            and result == "AWAY"
        ):
            won = True

        elif (
            choice == "Hòa"
            and result == "DRAW"
        ):
            won = True

        payout = 0

        if won:

            payout = int(
                bet["amount"]
                * bet["odds"]
            )

            add_money(
                user_id,
                payout
            )

            status = "WON"

        else:

            status = "LOST"

        db.execute(
            """
            UPDATE bets
            SET status = ?,
                payout = ?,
                settled_at = ?
            WHERE id = ?
            """,
            (
                status,
                payout,
                now_vn().isoformat(),
                bet["id"]
            )
        )

        db.commit()

        results.append(
            (
                bet,
                home_score,
                away_score,
                won,
                payout
            )
        )

    if not results:

        await interaction.followup.send(
            "⏳ Chưa có trận nào trong cược của m "
            "có kết quả để thanh toán."
        )

        return

    embed = discord.Embed(
        title="📋 KẾT TOÁN CƯỢC",
        color=discord.Color.gold()
    )

    for (
        bet,
        home_score,
        away_score,
        won,
        payout
    ) in results:

        icon = "✅" if won else "❌"

        text = (
            f"{icon} **{bet['home_team']}** "
            f"`{home_score}-{away_score}` "
            f"**{bet['away_team']}**\n"
            f"Cửa: `{bet['choice']}`\n"
            f"Cược: {money(bet['amount'])}"
        )

        if won:

            text += (
                f"\n💰 Nhận: {money(payout)}"
            )

        else:

            text += "\n💸 Mất tiền cược"

        embed.add_field(
            name=f"Cược #{bet['id']}",
            value=text,
            inline=False
        )

    balance = get_wallet(
        user_id
    )["balance"]

    embed.add_field(
        name="💳 Số dư hiện tại",
        value=money(balance),
        inline=False
    )

    await interaction.followup.send(
        embed=embed
    )


# =========================================================
# /SOI
# =========================================================

class SoiView(
    discord.ui.View
):

    def __init__(self):

        super().__init__(
            timeout=180
        )

    @discord.ui.button(
        label="🏆 C1",
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

        now = now_vn()

        future = []

        for match in matches:

            dt = match_datetime(match)

            if not dt:
                continue

            if dt <= now:
                continue

            if is_finished(match):
                continue

            if is_cancelled(match):
                continue

            future.append(match)

        future.sort(
            key=lambda m: (
                match_datetime(m)
                or datetime.max.replace(
                    tzinfo=TIMEZONE
                )
            )
        )

        if not future:

            await interaction.followup.send(
                "❌ Không tìm thấy trận C1 "
                "trong 30 ngày tới."
            )

            return

        embeds = []

        for match in future[:10]:

            home = get_team_name(
                match.get("homeTeam")
            )

            away = get_team_name(
                match.get("awayTeam")
            )

            dt = match_datetime(match)

            embed = discord.Embed(
                title="🏆 UEFA Champions League",
                description=(
                    f"**{home}**\n"
                    f"🆚\n"
                    f"**{away}**"
                ),
                color=discord.Color.blurple()
            )

            embed.add_field(
                name="🕐 Thời gian",
                value=format_time(dt),
                inline=False
            )

            prediction = await predict_match(
                match
            )

            embed.add_field(
                name="📊 Ước tính thống kê",
                value=(
                    f"🏠 {prediction['home']:.1f}%\n"
                    f"🤝 {prediction['draw']:.1f}%\n"
                    f"✈️ {prediction['away']:.1f}%"
                )
            )

            embed.set_footer(
                text="Ước tính thống kê, không đảm bảo kết quả."
            )

            embeds.append(embed)

        await interaction.followup.send(
            embeds=embeds
        )

    @discord.ui.button(
        label="🔵 Man City",
        style=discord.ButtonStyle.success
    )
    async def mancity(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await interaction.response.defer()

        matches = await get_mancity_matches()

        now = now_vn()

        future = []

        for match in matches:

            dt = match_datetime(match)

            if not dt:
                continue

            if dt <= now:
                continue

            if is_finished(match):
                continue

            if is_cancelled(match):
                continue

            future.append(match)

        future.sort(
            key=lambda m: (
                match_datetime(m)
                or datetime.max.replace(
                    tzinfo=TIMEZONE
                )
            )
        )

        if not future:

            await interaction.followup.send(
                "❌ Không tìm thấy trận Man City "
                "trong 30 ngày tới."
            )

            return

        match = future[0]

        home = get_team_name(
            match.get("homeTeam")
        )

        away = get_team_name(
            match.get("awayTeam")
        )

        prediction = await predict_match(
            match
        )

        embed = discord.Embed(
            title="🔵 MAN CITY",
            description=(
                f"**{home}**\n"
                f"🆚\n"
                f"**{away}**"
            ),
            color=discord.Color.blue()
        )

        embed.add_field(
            name="🕐 Thời gian",
            value=format_time(
                match_datetime(match)
            ),
            inline=False
        )

        embed.add_field(
            name="📊 Ước tính",
            value=(
                f"🏠 {prediction['home']:.1f}%\n"
                f"🤝 {prediction['draw']:.1f}%\n"
                f"✈️ {prediction['away']:.1f}%"
            )
        )

        embed.add_field(
            name="⚽ Bàn thắng kỳ vọng",
            value=(
                f"{prediction['expected_home']:.2f}"
                f" - "
                f"{prediction['expected_away']:.2f}"
            )
        )

        embed.set_footer(
            text="Ước tính thống kê, không phải kết quả chắc chắn."
        )

        await interaction.followup.send(
            embed=embed
        )


@bot.tree.command(
    name="soi",
    description="Xem trận C1 hoặc Man City"
)
async def soi(
    interaction: discord.Interaction
):

    embed = discord.Embed(
        title="⚽ SOI TRẬN",
        description=(
            "Chọn mục muốn xem:"
        ),
        color=discord.Color.blue()
    )

    await interaction.response.send_message(
        embed=embed,
        view=SoiView()
    )


# =========================================================
# ALERT SYSTEM
# =========================================================

def get_alert_channel():

    if not ALERT_CHANNEL_RAW:
        return None

    raw = ALERT_CHANNEL_RAW

    if raw.startswith("<#") and raw.endswith(">"):
        raw = raw[2:-1]

    try:
        channel_id = int(raw)
    except ValueError:
        return None

    channel = bot.get_channel(
        channel_id
    )

    return channel


def alert_was_sent(key):

    row = db.execute(
        """
        SELECT 1
        FROM sent_alerts
        WHERE alert_key = ?
        """,
        (key,)
    ).fetchone()

    return row is not None


def mark_alert_sent(key):

    db.execute(
        """
        INSERT OR IGNORE INTO sent_alerts
        (
            alert_key,
            created_at
        )
        VALUES (?, ?)
        """,
        (
            key,
            now_vn().isoformat()
        )
    )

    db.commit()


async def send_alert(
    channel,
    match,
    alert_type
):

    match_id = match.get("id")

    key = f"{match_id}:{alert_type}"

    if alert_was_sent(key):
        return

    home = get_team_name(
        match.get("homeTeam")
    )

    away = get_team_name(
        match.get("awayTeam")
    )

    dt = match_datetime(match)

    embed = discord.Embed(
        color=discord.Color.blue()
    )

    if alert_type == "PRE":

        embed.title = "⏰ TRẬN SẮP BẮT ĐẦU"

        embed.description = (
            f"⚽ **{home}** 🆚 **{away}**\n"
            f"🕐 {format_time(dt)}"
        )

    elif alert_type == "LIVE":

        embed.title = "🔴 TRẬN ĐANG DIỄN RA"

        embed.description = (
            f"⚽ **{home}** 🆚 **{away}**\n"
            f"🕐 Đã bắt đầu"
        )

    elif alert_type == "15":

        embed.title = "⏱️ ĐÃ 15 PHÚT"

        embed.description = (
            f"⚽ **{home}** 🆚 **{away}**\n"
            "⏱️ Trận đã diễn ra khoảng 15 phút."
        )

    elif alert_type == "FINISHED":

        h, a = match_score(match)

        embed.title = "🏁 TRẬN ĐÃ KẾT THÚC"

        embed.description = (
            f"**{home}** `{h}-{a}` **{away}**"
        )

    else:

        return

    await channel.send(
        embed=embed
    )

    mark_alert_sent(key)


async def process_alerts():

    channel = get_alert_channel()

    if not channel:
        return

    matches = await get_all_relevant_matches()

    now = now_vn()

    for match in matches:

        dt = match_datetime(match)

        if not dt:
            continue

        mins = int(
            (now - dt).total_seconds() / 60
        )

        status = match_status(match)

        # Trận sắp bắt đầu 10 phút
        if (
            -10 <= mins < 0
            and status not in {
                "FINISHED",
                "CANCELLED",
                "POSTPONED"
            }
        ):

            await send_alert(
                channel,
                match,
                "PRE"
            )

        # vừa bắt đầu
        elif (
            0 <= mins <= 3
            and status not in {
                "FINISHED",
                "CANCELLED",
                "POSTPONED"
            }
        ):

            await send_alert(
                channel,
                match,
                "LIVE"
            )

        # phút 15
        elif 15 <= mins <= 18:

            await send_alert(
                channel,
                match,
                "15"
            )

        # kết thúc
        elif status in {
            "FINISHED",
            "AWARDED"
        } and 0 <= mins <= 180:

            await send_alert(
                channel,
                match,
                "FINISHED"
            )


@tasks.loop(seconds=60)
async def alert_loop():

    try:

        await process_alerts()

    except Exception as e:

        print(
            "Alert loop error:",
            repr(e)
        )


@alert_loop.before_loop
async def before_alert_loop():

    await bot.wait_until_ready()


# =========================================================
# AUTO SETTLEMENT
# =========================================================

@tasks.loop(minutes=5)
async def auto_settlement():

    try:

        pending = db.execute(
            """
            SELECT DISTINCT user_id
            FROM bets
            WHERE status = 'PENDING'
            """
        ).fetchall()

        for row in pending:

            user_id = row["user_id"]

            bets = db.execute(
                """
                SELECT *
                FROM bets
                WHERE user_id = ?
                AND status = 'PENDING'
                """,
                (user_id,)
            ).fetchall()

            for bet in bets:

                data = await football.get(
                    f"/matches/{bet['match_id']",
                    cache_seconds=300
                )

                if not data:
                    continue

                match = data.get("match")

                if not match:
                    continue

                if not is_finished(match):
                    continue

                h, a = match_score(match)

                if h is None or a is None:
                    continue

                if h > a:
                    result = "HOME"

                elif h < a:
                    result = "AWAY"

                else:
                    result = "DRAW"

                choice = bet["choice"]

                won = (
                    (
                        choice == "Đội nhà thắng"
                        and result == "HOME"
                    )
                    or
                    (
                        choice == "Đội khách thắng"
                        and result == "AWAY"
                    )
                    or
                    (
                        choice == "Hòa"
                        and result == "DRAW"
                    )
                )

                payout = 0

                if won:

                    payout = int(
                        bet["amount"]
                        * bet["odds"]
                    )

                    add_money(
                        user_id,
                        payout
                    )

                    status = "WON"

                else:

                    status = "LOST"

                db.execute(
                    """
                    UPDATE bets
                    SET status = ?,
                        payout = ?,
                        settled_at = ?
                    WHERE id = ?
                    """,
                    (
                        status,
                        payout,
                        now_vn().isoformat(),
                        bet["id"]
                    )
                )

                db.commit()

    except Exception as e:

        print(
            "Auto settlement error:",
            repr(e)
        )


@auto_settlement.before_loop
async def before_auto_settlement():

    await bot.wait_until_ready()


# =========================================================
# READY
# =========================================================

@bot.event
async def on_ready():

    print("=" * 50)

    print(
        f"Bot online: {bot.user}"
    )

    print(
        f"Guild ID: {GUILD_ID}"
    )

    print(
        f"Alert competition: {DEFAULT_COMPETITION}"
    )

    print(
        f"Alert channel: {ALERT_CHANNEL_RAW}"
    )

    print("=" * 50)

    await football.start()

    # test Man City API
    try:

        city = await get_mancity_matches()

        print(
            f"Man City matches found: {len(city)}"
        )

        for match in city[:5]:

            print(
                "CITY:",
                get_team_name(
                    match.get("homeTeam")
                ),
                "vs",
                get_team_name(
                    match.get("awayTeam")
                ),
                match.get("utcDate")
            )

    except Exception as e:

        print(
            "Man City test error:",
            repr(e)
        )

    # test CL
    try:

        cl = await get_competition_matches(
            "CL"
        )

        print(
            f"CL matches found: {len(cl)}"
        )

        for match in cl[:5]:

            print(
                "CL:",
                get_team_name(
                    match.get("homeTeam")
                ),
                "vs",
                get_team_name(
                    match.get("awayTeam")
                ),
                match.get("utcDate")
            )

    except Exception as e:

        print(
            "CL test error:",
            repr(e)
        )

    # sync slash commands
    try:

        guild = discord.Object(
            id=GUILD_ID
        )

        synced = await bot.tree.sync(
            guild=guild
        )

        print(
            f"Synced {len(synced)} guild commands."
        )

    except Exception as e:

        print(
            "Command sync error:",
            repr(e)
        )

    # start loops
    if not alert_loop.is_running():
        alert_loop.start()

    if not auto_settlement.is_running():
        auto_settlement.start()

    print(
        "Alert loop: ON"
    )

    print(
        "Auto settlement: ON"
    )

    print("=" * 50)


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

try:

    bot.run(TOKEN)

finally:

    try:
        asyncio.run(
            shutdown()
        )
    except Exception:
        pass

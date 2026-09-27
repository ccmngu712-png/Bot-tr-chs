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
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "pro")

GUILD_ID = 1551547049600618538

TIMEZONE = ZoneInfo("Asia/Ho_Chi_Minh")

MAN_CITY_ID = 65
DEFAULT_COMPETITION = os.getenv(
    "FOOTBALL_ALERT_COMPETITION",
    "CL"
)

ALERT_CHANNEL_RAW = os.getenv(
    "FOOTBALL_ALERT_CHANNEL_ID",
    ""
).strip()

DB_FILE = "football_bot.db"

STARTING_MONEY = 1000
DAILY_MONEY = 50000

API_BASE = "https://api.football-data.org/v4"


if not TOKEN:
    raise RuntimeError("Thiếu DISCORD_TOKEN")

if not FOOTBALL_DATA_TOKEN:
    raise RuntimeError("Thiếu FOOTBALL_DATA_TOKEN")


# =========================================================
# DATABASE
# =========================================================

db = sqlite3.connect(
    DB_FILE,
    check_same_thread=False
)

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
CREATE TABLE IF NOT EXISTS daily_claims (
    user_id INTEGER PRIMARY KEY,
    last_claim TEXT NOT NULL
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


def today_string():
    return now_vn().strftime("%Y-%m-%d")


def parse_utc(value):
    if not value:
        return None

    try:
        dt = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
        return dt.astimezone(TIMEZONE)
    except Exception:
        return None


def format_time(value):
    dt = parse_utc(value) if isinstance(
        value,
        str
    ) else value

    if not dt:
        return "Không rõ"

    return dt.strftime(
        "%d/%m/%Y %H:%M"
    )


def money(value):
    return f"{int(value):,} 💰"


def get_team_name(team):
    if not team:
        return "Unknown"

    return (
        team.get("name")
        or team.get("shortName")
        or "Unknown"
    )


def get_team_id(team):
    if not team:
        return None

    return team.get("id")


def match_score(match):
    score = match.get("score", {})
    full_time = score.get("fullTime", {})

    return (
        full_time.get("home"),
        full_time.get("away")
    )


def match_status(match):
    return match.get(
        "status",
        "UNKNOWN"
    )


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
        (
            dt - now_vn()
        ).total_seconds() / 60
    )


# =========================================================
# FOOTBALL API
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
                    "X-Auth-Token":
                    FOOTBALL_DATA_TOKEN
                }
            )

    async def close(self):

        if self.session:
            await self.session.close()
            self.session = None

    async def rate_limit(self):

        loop = asyncio.get_running_loop()

        current = loop.time()

        self.request_times = [
            t
            for t in self.request_times
            if current - t < 60
        ]

        if len(self.request_times) >= 9:

            wait = (
                60
                - (current - self.request_times[0])
                + 0.2
            )

            if wait > 0:
                await asyncio.sleep(wait)

        self.request_times.append(
            loop.time()
        )

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
            tuple(
                sorted(params.items())
            )
        )

        loop = asyncio.get_running_loop()

        if cache_seconds > 0:

            cached = self.cache.get(
                cache_key
            )

            if cached:

                timestamp, data = cached

                if (
                    loop.time() - timestamp
                    < cache_seconds
                ):
                    return data

        await self.rate_limit()

        try:

            async with self.session.get(
                API_BASE + endpoint,
                params=params,
                timeout=20
            ) as response:

                self.total_requests += 1

                remaining = response.headers.get(
                    "X-Requests-Available"
                )

                if remaining is not None:

                    try:
                        self.last_quota = int(
                            remaining
                        )
                    except Exception:
                        pass

                if response.status == 429:

                    print(
                        "Football API: 429"
                    )

                    return None

                if response.status == 403:

                    print(
                        "Football API: 403"
                    )

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
                        loop.time(),
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
# FOOTBALL DATA
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

    return data.get(
        "matches",
        []
    )


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

    return data.get(
        "matches",
        []
    )


async def get_all_relevant_matches():

    city = await get_mancity_matches()

    cl = await get_competition_matches(
        DEFAULT_COMPETITION
    )

    result = {}

    for match in city + cl:

        match_id = match.get("id")

        if match_id:
            result[match_id] = match

    return list(
        result.values()
    )


async def get_team_recent(
    team_id
):

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

    return data.get(
        "matches",
        []
    )


# =========================================================
# PREDICTION
# =========================================================

def poisson_probability(
    k,
    lam
):

    if lam <= 0:
        return 0

    return (
        math.exp(-lam)
        * lam ** k
        / math.factorial(k)
    )


async def team_average_goals(
    team_id
):

    matches = await get_team_recent(
        team_id
    )

    if not matches:
        return 1.2, 1.2

    scored = 0
    conceded = 0
    games = 0

    for match in matches:

        home = match.get(
            "homeTeam",
            {}
        )

        away = match.get(
            "awayTeam",
            {}
        )

        h, a = match_score(
            match
        )

        if h is None or a is None:
            continue

        games += 1

        if team_id == home.get("id"):

            scored += h
            conceded += a

        elif team_id == away.get("id"):

            scored += a
            conceded += h

    if games == 0:
        return 1.2, 1.2

    return (
        scored / games,
        conceded / games
    )


async def predict_match(
    match
):

    home = match.get(
        "homeTeam",
        {}
    )

    away = match.get(
        "awayTeam",
        {}
    )

    home_id = home.get("id")
    away_id = away.get("id")

    hs, hc = await team_average_goals(
        home_id
    )

    aws, awc = await team_average_goals(
        away_id
    )

    expected_home = (
        hs + awc
    ) / 2

    expected_away = (
        aws + hc
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

    for h in range(8):

        for a in range(8):

            probability = (
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
                home_win += probability

            elif h == a:
                draw += probability

            else:
                away_win += probability

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


def calculate_odds(
    probability
):

    probability = max(
        1,
        min(99, probability)
    )

    return round(
        max(
            1.05,
            0.92 / (probability / 100)
        ),
        2
    )


# =========================================================
# WALLET
# =========================================================

def get_wallet(
    user_id
):

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
        (
            amount,
            user_id
        )
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
# /COMAT
# =========================================================

@bot.tree.command(
    name="comat",
    description="Nhận 50.000 xu mỗi ngày"
)
async def comat(
    interaction: discord.Interaction
):

    user_id = interaction.user.id
    today = today_string()

    row = db.execute(
        """
        SELECT last_claim
        FROM daily_claims
        WHERE user_id = ?
        """,
        (user_id,)
    ).fetchone()

    if row and row["last_claim"] == today:

        await interaction.response.send_message(
            "❌ Hôm nay m đã nhận **50.000 💰** rồi.\n"
            "⏰ Mai quay lại nhận tiếp nhé.",
            ephemeral=True
        )

        return

    wallet = get_wallet(
        user_id
    )

    if not wallet["started"]:

        db.execute(
            """
            UPDATE wallets
            SET started = 1
            WHERE user_id = ?
            """,
            (user_id,)
        )

    add_money(
        user_id,
        DAILY_MONEY
    )

    db.execute(
        """
        INSERT INTO daily_claims
        (user_id, last_claim)
        VALUES (?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET last_claim = excluded.last_claim
        """,
        (
            user_id,
            today
        )
    )

    db.commit()

    balance = get_wallet(
        user_id
    )["balance"]

    embed = discord.Embed(
        title="🎁 COMAT HÀNG NGÀY",
        description=(
            f"{interaction.user.mention} "
            f"đã nhận được\n\n"
            f"💰 **{money(DAILY_MONEY)}**"
        ),
        color=discord.Color.green()
    )

    embed.add_field(
        name="💳 Số dư hiện tại",
        value=money(balance)
    )

    embed.set_footer(
        text="Mỗi ngày chỉ nhận được 1 lần."
    )

    await interaction.response.send_message(
        embed=embed
    )


# =========================================================
# ADMIN MODAL
# =========================================================

class AdminMoneyModal(
    discord.ui.Modal
):

    def __init__(self):

        super().__init__(
            title="ADMIN - CỘNG XU"
        )

        self.password = discord.ui.TextInput(
            label="Mật khẩu admin",
            placeholder="Nhập mật khẩu",
            required=True,
            min_length=1,
            max_length=100
        )

        self.user_id_input = discord.ui.TextInput(
            label="ID người nhận",
            placeholder="Discord User ID",
            required=True,
            min_length=5,
            max_length=30
        )

        self.amount = discord.ui.TextInput(
            label="Số xu cộng",
            placeholder="Ví dụ: 100000",
            required=True,
            min_length=1,
            max_length=15
        )

        self.add_item(self.password)
        self.add_item(self.user_id_input)
        self.add_item(self.amount)

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        if self.password.value != ADMIN_PASSWORD:

            await interaction.response.send_message(
                "❌ Sai mật khẩu admin.",
                ephemeral=True
            )

            return

        try:

            target_id = int(
                self.user_id_input.value.strip()
            )

            amount = int(
                self.amount.value
                .replace(",", "")
                .strip()
            )

        except ValueError:

            await interaction.response.send_message(
                "❌ ID hoặc số tiền không hợp lệ.",
                ephemeral=True
            )

            return

        if amount <= 0:

            await interaction.response.send_message(
                "❌ Số xu phải lớn hơn 0.",
                ephemeral=True
            )

            return

        if amount > 1_000_000_000:

            await interaction.response.send_message(
                "❌ Số xu quá lớn.",
                ephemeral=True
            )

            return

        # tạo ví nếu chưa có
        get_wallet(target_id)

        add_money(
            target_id,
            amount
        )

        balance = get_wallet(
            target_id
        )["balance"]

        await interaction.response.send_message(
            f"✅ **ADMIN ĐÃ CỘNG XU**\n\n"
            f"👤 User ID: `{target_id}`\n"
            f"💰 Cộng: **{money(amount)}**\n"
            f"💳 Số dư mới: **{money(balance)}**",
            ephemeral=True
        )


# =========================================================
# /ADMIN
# =========================================================

@bot.tree.command(
    name="admin",
    description="Mở bảng admin cộng xu"
)
async def admin(
    interaction: discord.Interaction
):

    # Chỉ người có quyền Administrator
    # mới được mở lệnh admin
    if not interaction.user.guild_permissions.administrator:

        await interaction.response.send_message(
            "❌ M không có quyền Administrator.",
            ephemeral=True
        )

        return

    await interaction.response.send_modal(
        AdminMoneyModal()
    )


# =========================================================
# /KHOINGHIEP
# =========================================================

@bot.tree.command(
    name="khoinghiep",
    description="Nhận vốn ban đầu"
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
            f"💰 Số dư: **{money(wallet['balance'])}**",
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
        f"🎉 M nhận được "
        f"**{money(STARTING_MONEY)}** vốn ban đầu!",
        ephemeral=False
    )


# =========================================================
# /VI
# =========================================================

@bot.tree.command(
    name="vi",
    description="Xem số dư"
)
async def vi(
    interaction: discord.Interaction
):

    user_id = interaction.user.id

    wallet = get_wallet(
        user_id
    )

    total = db.execute(
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
        title="💳 VÍ TIỀN",
        color=discord.Color.gold()
    )

    embed.add_field(
        name="💰 Số dư",
        value=f"**{money(wallet['balance'])}**",
        inline=False
    )

    embed.add_field(
        name="🎫 Tổng cược",
        value=str(total),
        inline=True
    )

    embed.add_field(
        name="⏳ Đang chờ",
        value=str(pending),
        inline=True
    )

    await interaction.response.send_message(
        embed=embed
    )


# =========================================================
# BET MODAL
# =========================================================

class BetAmountModal(
    discord.ui.Modal
):

    def __init__(
        self,
        match,
        choice,
        odds
    ):

        super().__init__(
            title="Nhập số tiền cược"
        )

        self.match = match
        self.choice = choice
        self.odds = odds

        self.amount = discord.ui.TextInput(
            label="Số tiền cược",
            placeholder="Ví dụ: 5000",
            required=True,
            min_length=1,
            max_length=15
        )

        self.add_item(
            self.amount
        )

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        try:

            amount = int(
                self.amount.value
                .replace(",", "")
                .strip()
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

        user_id = interaction.user.id

        wallet = get_wallet(
            user_id
        )

        if wallet["balance"] < amount:

            await interaction.response.send_message(
                f"❌ Không đủ tiền.\n"
                f"💰 Ví: {money(wallet['balance'])}",
                ephemeral=True
            )

            return

        mins = minutes_until(
            self.match
        )

        if mins is None or mins <= 0:

            await interaction.response.send_message(
                "❌ Trận đã bắt đầu.",
                ephemeral=True
            )

            return

        if not remove_money(
            user_id,
            amount
        ):

            await interaction.response.send_message(
                "❌ Không thể trừ tiền.",
                ephemeral=True
            )

            return

        home = get_team_name(
            self.match.get("homeTeam")
        )

        away = get_team_name(
            self.match.get("awayTeam")
        )

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
                self.match.get("id"),
                home,
                away,
                self.choice,
                amount,
                self.odds,
                now_vn().isoformat()
            )
        )

        db.commit()

        balance = get_wallet(
            user_id
        )["balance"]

        potential = int(
            amount * self.odds
        )

        embed = discord.Embed(
            title="🎫 CƯỢC THÀNH CÔNG",
            color=discord.Color.green()
        )

        embed.add_field(
            name="⚽ Trận",
            value=(
                f"**{home}** 🆚 **{away}**"
            ),
            inline=False
        )

        embed.add_field(
            name="🎯 Cửa",
            value=self.choice
        )

        embed.add_field(
            name="💰 Cược",
            value=money(amount)
        )

        embed.add_field(
            name="📈 Tỷ lệ",
            value=f"{self.odds:.2f}"
        )

        embed.add_field(
            name="🏆 Nhận nếu thắng",
            value=money(potential)
        )

        embed.add_field(
            name="💳 Còn lại",
            value=money(balance)
        )

        await interaction.response.send_message(
            embed=embed
        )


# =========================================================
# BET VIEW
# =========================================================

class BetChoiceView(
    discord.ui.View
):

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

        self.home_button.label = (
            f"{home[:25]} "
            f"{calculate_odds(prediction['home']):.2f}"
        )

        self.draw_button.label = (
            f"Hòa "
            f"{calculate_odds(prediction['draw']):.2f}"
        )

        self.away_button.label = (
            f"{away[:25]} "
            f"{calculate_odds(prediction['away']):.2f}"
        )

    @discord.ui.button(
        label="Đội nhà",
        style=discord.ButtonStyle.primary
    )
    async def home_button(
        self,
        interaction,
        button
    ):

        await interaction.response.send_modal(
            BetAmountModal(
                self.match,
                "Đội nhà thắng",
                calculate_odds(
                    self.prediction["home"]
                )
            )
        )

    @discord.ui.button(
        label="Hòa",
        style=discord.ButtonStyle.secondary
    )
    async def draw_button(
        self,
        interaction,
        button
    ):

        await interaction.response.send_modal(
            BetAmountModal(
                self.match,
                "Hòa",
                calculate_odds(
                    self.prediction["draw"]
                )
            )
        )

    @discord.ui.button(
        label="Đội khách",
        style=discord.ButtonStyle.danger
    )
    async def away_button(
        self,
        interaction,
        button
    ):

        await interaction.response.send_modal(
            BetAmountModal(
                self.match,
                "Đội khách thắng",
                calculate_odds(
                    self.prediction["away"]
                )
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

            dt = match_datetime(
                match
            )

            text = (
                dt.strftime(
                    "%d/%m %H:%M"
                )
                if dt
                else "?"
            )

            options.append(
                discord.SelectOption(
                    label=(
                        f"{home[:35]} "
                        f"vs {away[:35]}"
                    ),
                    description=text,
                    value=str(
                        match.get("id")
                    )
                )
            )

        super().__init__(
            placeholder="⚽ Chọn trận...",
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
        interaction
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

        prediction = await predict_match(
            match
        )

        home = get_team_name(
            match.get("homeTeam")
        )

        away = get_team_name(
            match.get("awayTeam")
        )

        embed = discord.Embed(
            title="🎯 CHỌN CỬA",
            description=(
                f"**{home}** 🆚 **{away}**\n"
                f"🕐 {format_time(match_datetime(match))}"
            ),
            color=discord.Color.blurple()
        )

        embed.add_field(
            name="📊 Xác suất thống kê",
            value=(
                f"🏠 {prediction['home']:.1f}%\n"
                f"🤝 {prediction['draw']:.1f}%\n"
                f"✈️ {prediction['away']:.1f}%"
            )
        )

        embed.add_field(
            name="📈 Tỷ lệ cược",
            value=(
                f"🏠 `{calculate_odds(prediction['home']):.2f}`\n"
                f"🤝 `{calculate_odds(prediction['draw']):.2f}`\n"
                f"✈️ `{calculate_odds(prediction['away']):.2f}`"
            )
        )

        embed.set_footer(
            text="Chọn cửa bên dưới rồi nhập số tiền."
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
# /CUOC
# =========================================================

@bot.tree.command(
    name="cuoc",
    description="Đặt cược trận đấu"
)
async def cuoc(
    interaction
):

    wallet = get_wallet(
        interaction.user.id
    )

    if not wallet["started"]:

        await interaction.response.send_message(
            "❌ Dùng `/khoinghiep` hoặc `/comat` trước.",
            ephemeral=True
        )

        return

    if wallet["balance"] <= 0:

        await interaction.response.send_message(
            "❌ Ví đang hết tiền.",
            ephemeral=True
        )

        return

    await interaction.response.defer()

    matches = await get_all_relevant_matches()

    future = []

    for match in matches:

        dt = match_datetime(
            match
        )

        if not dt:
            continue

        if dt <= now_vn():
            continue

        if is_finished(match):
            continue

        if is_cancelled(match):
            continue

        future.append(match)

    future.sort(
        key=lambda m:
        match_datetime(m)
        or datetime.max.replace(
            tzinfo=TIMEZONE
        )
    )

    if not future:

        await interaction.followup.send(
            "❌ Không có trận sắp đá."
        )

        return

    embed = discord.Embed(
        title="🎰 ĐẶT CƯỢC",
        description=(
            f"💰 Số dư: **{money(wallet['balance'])}**\n\n"
            "Chọn trận bên dưới."
        ),
        color=discord.Color.green()
    )

    await interaction.followup.send(
        embed=embed,
        view=MatchSelectView(
            future[:25]
        )
    )


# =========================================================
# /KETTOAN
# =========================================================

async def settle_user_bets(
    user_id
):

    pending = db.execute(
        """
        SELECT *
        FROM bets
        WHERE user_id = ?
        AND status = 'PENDING'
        """,
        (user_id,)
    ).fetchall()

    results = []

    for bet in pending:

        data = await football.get(
            f"/matches/{bet['match_id']}",
            cache_seconds=60
        )

        if not data:
            continue

        match = data.get("match")

        if not match or not is_finished(match):
            continue

        h, a = match_score(
            match
        )

        if h is None or a is None:
            continue

        if h > a:
            result = "HOME"

        elif h < a:
            result = "AWAY"

        else:
            result = "DRAW"

        won = (
            (
                bet["choice"] ==
                "Đội nhà thắng"
                and result == "HOME"
            )
            or
            (
                bet["choice"] ==
                "Đội khách thắng"
                and result == "AWAY"
            )
            or
            (
                bet["choice"] ==
                "Hòa"
                and result == "DRAW"
            )
        )

        payout = (
            int(
                bet["amount"]
                * bet["odds"]
            )
            if won
            else 0
        )

        if won:
            add_money(
                user_id,
                payout
            )

        status = (
            "WON"
            if won
            else "LOST"
        )

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
                h,
                a,
                won,
                payout
            )
        )

    return results


@bot.tree.command(
    name="kettoan",
    description="Thanh toán cược đã có kết quả"
)
async def kettoan(
    interaction
):

    await interaction.response.defer()

    results = await settle_user_bets(
        interaction.user.id
    )

    if not results:

        await interaction.followup.send(
            "⏳ Chưa có cược nào có kết quả."
        )

        return

    embed = discord.Embed(
        title="📋 KẾT TOÁN",
        color=discord.Color.gold()
    )

    for (
        bet,
        h,
        a,
        won,
        payout
    ) in results:

        icon = (
            "✅"
            if won
            else "❌"
        )

        text = (
            f"{icon} "
            f"**{bet['home_team']}** "
            f"`{h}-{a}` "
            f"**{bet['away_team']}**\n"
            f"Cửa: `{bet['choice']}`\n"
            f"Cược: {money(bet['amount'])}"
        )

        if won:

            text += (
                f"\n💰 Nhận: "
                f"{money(payout)}"
            )

        else:

            text += "\n💸 Thua cược"

        embed.add_field(
            name=f"Cược #{bet['id']}",
            value=text,
            inline=False
        )

    embed.add_field(
        name="💳 Số dư",
        value=money(
            get_wallet(
                interaction.user.id
            )["balance"]
        ),
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
        interaction,
        button
    ):

        await interaction.response.defer()

        matches = await get_competition_matches(
            "CL"
        )

        future = [
            m
            for m in matches
            if (
                match_datetime(m)
                and match_datetime(m) > now_vn()
                and not is_finished(m)
                and not is_cancelled(m)
            )
        ]

        future.sort(
            key=lambda m:
            match_datetime(m)
        )

        if not future:

            await interaction.followup.send(
                "❌ Không có trận C1 trong 30 ngày tới."
            )

            return

        embeds = []

        for match in future[:10]:

            prediction = await predict_match(
                match
            )

            home = get_team_name(
                match.get("homeTeam")
            )

            away = get_team_name(
                match.get("awayTeam")
            )

            embed = discord.Embed(
                title="🏆 CHAMPIONS LEAGUE",
                description=(
                    f"**{home}** 🆚 **{away}**"
                ),
                color=discord.Color.blurple()
            )

            embed.add_field(
                name="🕐",
                value=format_time(
                    match_datetime(match)
                )
            )

            embed.add_field(
                name="📊 Ước tính",
                value=(
                    f"🏠 {prediction['home']:.1f}%\n"
                    f"🤝 {prediction['draw']:.1f}%\n"
                    f"✈️ {prediction['away']:.1f}%"
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
        interaction,
        button
    ):

        await interaction.response.defer()

        matches = await get_mancity_matches()

        future = [
            m
            for m in matches
            if (
                match_datetime(m)
                and match_datetime(m) > now_vn()
                and not is_finished(m)
                and not is_cancelled(m)
            )
        ]

        future.sort(
            key=lambda m:
            match_datetime(m)
        )

        if not future:

            await interaction.followup.send(
                "❌ Không tìm thấy trận Man City."
            )

            return

        match = future[0]

        prediction = await predict_match(
            match
        )

        home = get_team_name(
            match.get("homeTeam")
        )

        away = get_team_name(
            match.get("awayTeam")
        )

        embed = discord.Embed(
            title="🔵 MAN CITY",
            description=(
                f"**{home}** 🆚 **{away}**"
            ),
            color=discord.Color.blue()
        )

        embed.add_field(
            name="🕐",
            value=format_time(
                match_datetime(match)
            )
        )

        embed.add_field(
            name="📊 Ước tính",
            value=(
                f"🏠 {prediction['home']:.1f}%\n"
                f"🤝 {prediction['draw']:.1f}%\n"
                f"✈️ {prediction['away']:.1f}%"
            )
        )

        await interaction.followup.send(
            embed=embed
        )


@bot.tree.command(
    name="soi",
    description="Xem trận C1 hoặc Man City"
)
async def soi(
    interaction
):

    embed = discord.Embed(
        title="⚽ SOI TRẬN",
        description="Chọn giải muốn xem:",
        color=discord.Color.blue()
    )

    await interaction.response.send_message(
        embed=embed,
        view=SoiView()
    )


# =========================================================
# /PING
# =========================================================

@bot.tree.command(
    name="ping",
    description="Kiểm tra bot"
)
async def ping(
    interaction
):

    await interaction.response.send_message(
        f"🏓 Pong `{round(bot.latency * 1000)}ms`"
    )


# =========================================================
# /APIQUOTA
# =========================================================

@bot.tree.command(
    name="apiquota",
    description="Xem API quota"
)
async def apiquota(
    interaction
):

    await interaction.response.defer(
        ephemeral=True
    )

    await football.get(
        "/competitions/CL",
        cache_seconds=30
    )

    remaining = (
        football.last_quota
        if football.last_quota is not None
        else "Không rõ"
    )

    await interaction.followup.send(
        f"📡 API requests: "
        f"`{football.total_requests}`\n"
        f"📊 Còn lại: `{remaining}`",
        ephemeral=True
    )


# =========================================================
# AUTO SETTLEMENT
# =========================================================

@tasks.loop(minutes=5)
async def auto_settlement():

    try:

        users = db.execute(
            """
            SELECT DISTINCT user_id
            FROM bets
            WHERE status = 'PENDING'
            """
        ).fetchall()

        for row in users:

            await settle_user_bets(
                row["user_id"]
            )

    except Exception as e:

        print(
            "Auto settlement error:",
            repr(e)
        )


@auto_settlement.before_loop
async def before_auto_settlement():

    await bot.wait_until_ready()


# =========================================================
# ALERT
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

    return bot.get_channel(
        channel_id
    )


def alert_was_sent(
    key
):

    row = db.execute(
        """
        SELECT 1
        FROM sent_alerts
        WHERE alert_key = ?
        """,
        (key,)
    ).fetchone()

    return row is not None


async def send_alert(
    channel,
    match,
    alert_type
):

    key = (
        f"{match.get('id')}:"
        f"{alert_type}"
    )

    if alert_was_sent(key):
        return

    home = get_team_name(
        match.get("homeTeam")
    )

    away = get_team_name(
        match.get("awayTeam")
    )

    embed = discord.Embed(
        color=discord.Color.blue()
    )

    if alert_type == "PRE":

        embed.title = "⏰ TRẬN SẮP BẮT ĐẦU"

        embed.description = (
            f"⚽ **{home}** 🆚 **{away}**\n"
            f"🕐 {format_time(match_datetime(match))}"
        )

    elif alert_type == "LIVE":

        embed.title = "🔴 TRẬN ĐANG DIỄN RA"

        embed.description = (
            f"⚽ **{home}** 🆚 **{away}**"
        )

    elif alert_type == "FINISHED":

        h, a = match_score(
            match
        )

        embed.title = "🏁 TRẬN KẾT THÚC"

        embed.description = (
            f"**{home}** `{h}-{a}` **{away}**"
        )

    else:
        return

    await channel.send(
        embed=embed
    )

    db.execute(
        """
        INSERT OR IGNORE INTO sent_alerts
        (alert_key, created_at)
        VALUES (?, ?)
        """,
        (
            key,
            now_vn().isoformat()
        )
    )

    db.commit()


async def process_alerts():

    channel = get_alert_channel()

    if not channel:
        return

    matches = await get_all_relevant_matches()

    now = now_vn()

    for match in matches:

        dt = match_datetime(
            match
        )

        if not dt:
            continue

        mins = int(
            (
                now - dt
            ).total_seconds() / 60
        )

        status = match_status(
            match
        )

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

        elif (
            status in {
                "FINISHED",
                "AWARDED"
            }
            and 0 <= mins <= 180
        ):

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
            "Alert error:",
            repr(e)
        )


@alert_loop.before_loop
async def before_alert_loop():

    await bot.wait_until_ready()


# =========================================================
# READY
# =========================================================

@bot.event
async def on_ready():

    print("=" * 50)
    print(
        f"BOT ONLINE: {bot.user}"
    )

    print(
        f"Guild: {GUILD_ID}"
    )

    print(
        f"Competition: {DEFAULT_COMPETITION}"
    )

    print("=" * 50)

    await football.start()

    try:

        city = await get_mancity_matches()

        print(
            f"Man City matches: {len(city)}"
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
            "City test error:",
            repr(e)
        )

    try:

        cl = await get_competition_matches(
            "CL"
        )

        print(
            f"CL matches: {len(cl)}"
        )

    except Exception as e:

        print(
            "CL test error:",
            repr(e)
        )

    try:

        guild = discord.Object(
            id=GUILD_ID
        )

        synced = await bot.tree.sync(
            guild=guild
        )

        print(
            f"Synced {len(synced)} commands"
        )

    except Exception as e:

        print(
            "Sync error:",
            repr(e)
        )

    if not alert_loop.is_running():
        alert_loop.start()

    if not auto_settlement.is_running():
        auto_settlement.start()

    print("Alert loop: ON")
    print("Auto settlement: ON")


# =========================================================
# RUN
# =========================================================

bot.run(TOKEN)

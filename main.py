import os
import asyncio
import sqlite3
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
import discord
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
COMPETITION_CODE = os.getenv(
    "FOOTBALL_ALERT_COMPETITION",
    "CL"
)

ALERT_CHANNEL_ID = os.getenv(
    "FOOTBALL_ALERT_CHANNEL_ID",
    ""
).strip()

API_BASE = "https://api.football-data.org/v4"

DB_FILE = "football_bot.db"

STARTING_MONEY = 1000
DAILY_MONEY = 50000


# =========================================================
# CHECK ENV
# =========================================================

if not TOKEN:
    raise RuntimeError(
        "Thiếu DISCORD_TOKEN trên Railway Variables"
    )

if not FOOTBALL_DATA_TOKEN:
    raise RuntimeError(
        "Thiếu FOOTBALL_DATA_TOKEN trên Railway Variables"
    )


# =========================================================
# DISCORD BOT
# =========================================================

intents = discord.Intents.default()

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


# =========================================================
# DATABASE
# =========================================================

db = sqlite3.connect(
    DB_FILE,
    check_same_thread=False
)

db.row_factory = sqlite3.Row

db.executescript(
    """
    CREATE TABLE IF NOT EXISTS wallets (
        user_id INTEGER PRIMARY KEY,
        balance INTEGER NOT NULL DEFAULT 0,
        started INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS daily_claims (
        user_id INTEGER PRIMARY KEY,
        last_claim TEXT NOT NULL
    );

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
        payout INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        settled_at TEXT
    );

    CREATE TABLE IF NOT EXISTS sent_alerts (
        alert_key TEXT PRIMARY KEY,
        created_at TEXT NOT NULL
    );
    """
)

db.commit()


# =========================================================
# GENERAL HELPERS
# =========================================================

def now_vn():
    return datetime.now(TIMEZONE)


def today_vn():
    return now_vn().strftime("%Y-%m-%d")


def money(amount):
    return f"{int(amount):,} 💰"


def parse_datetime(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
        ).astimezone(TIMEZONE)

    except Exception:
        return None


def format_datetime(value):
    if isinstance(value, str):
        value = parse_datetime(value)

    if not value:
        return "Không rõ"

    return value.strftime(
        "%d/%m/%Y %H:%M"
    )


def get_team_name(team):
    if not team:
        return "Unknown"

    return (
        team.get("name")
        or team.get("shortName")
        or "Unknown"
    )


def get_home(match):
    return get_team_name(
        match.get("homeTeam")
    )


def get_away(match):
    return get_team_name(
        match.get("awayTeam")
    )


def get_match_datetime(match):
    return parse_datetime(
        match.get("utcDate")
    )


def get_score(match):
    score = match.get("score") or {}
    full_time = score.get("fullTime") or {}

    return (
        full_time.get("home"),
        full_time.get("away")
    )


def is_finished(match):
    return match.get("status") in {
        "FINISHED",
        "AWARDED"
    }


def is_cancelled(match):
    return match.get("status") in {
        "CANCELLED",
        "POSTPONED",
        "SUSPENDED"
    }


# =========================================================
# FOOTBALL-DATA API
# =========================================================

class FootballAPI:

    def __init__(self):
        self.session = None
        self.cache = {}
        self.request_times = []

        self.total_requests = 0
        self.remaining_requests = None

    async def start(self):

        if self.session is None:

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

    async def request(
        self,
        endpoint,
        params=None,
        cache_seconds=0
    ):

        await self.start()

        params = params or {}

        cache_key = (
            endpoint,
            tuple(
                sorted(
                    params.items()
                )
            )
        )

        loop = asyncio.get_running_loop()

        # CACHE

        if cache_seconds:

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

        # RATE LIMIT

        self.request_times = [
            t
            for t in self.request_times
            if loop.time() - t < 60
        ]

        if len(self.request_times) >= 9:

            wait_time = (
                60
                - (
                    loop.time()
                    - self.request_times[0]
                )
                + 0.2
            )

            if wait_time > 0:
                await asyncio.sleep(
                    wait_time
                )

        self.request_times.append(
            loop.time()
        )

        # REQUEST

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

                if remaining:

                    try:
                        self.remaining_requests = int(
                            remaining
                        )
                    except ValueError:
                        pass

                if response.status == 429:

                    print(
                        "Football API: rate limit"
                    )

                    return None

                if response.status == 403:

                    print(
                        "Football API: 403"
                    )

                    return None

                if response.status >= 400:

                    print(
                        "Football API error:",
                        response.status,
                        (await response.text())[:500]
                    )

                    return None

                data = await response.json()

                if cache_seconds:

                    self.cache[cache_key] = (
                        loop.time(),
                        data
                    )

                return data

        except Exception as error:

            print(
                "Football API exception:",
                repr(error)
            )

            return None


football = FootballAPI()


# =========================================================
# FOOTBALL MATCHES
# =========================================================

async def get_mancity_matches():

    today = now_vn().date()

    data = await football.request(
        f"/teams/{MAN_CITY_ID}/matches",

        params={
            "dateFrom": (
                today
                - timedelta(days=2)
            ).isoformat(),

            "dateTo": (
                today
                + timedelta(days=30)
            ).isoformat(),

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


async def get_competition_matches():

    today = now_vn().date()

    data = await football.request(
        f"/competitions/{COMPETITION_CODE}/matches",

        params={
            "dateFrom": (
                today
                - timedelta(days=2)
            ).isoformat(),

            "dateTo": (
                today
                + timedelta(days=30)
            ).isoformat(),

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

    city_matches = (
        await get_mancity_matches()
    )

    cl_matches = (
        await get_competition_matches()
    )

    matches = {}

    for match in (
        city_matches
        + cl_matches
    ):

        match_id = match.get("id")

        if match_id:
            matches[match_id] = match

    return list(
        matches.values()
    )


# =========================================================
# RECENT TEAM DATA
# =========================================================

async def get_recent_matches(
    team_id
):

    if not team_id:
        return []

    data = await football.request(
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


async def get_average_goals(
    team_id
):

    matches = await get_recent_matches(
        team_id
    )

    scored = 0
    conceded = 0
    games = 0

    for match in matches:

        home_score, away_score = (
            get_score(match)
        )

        if (
            home_score is None
            or away_score is None
        ):
            continue

        games += 1

        home_id = (
            match.get("homeTeam", {})
            .get("id")
        )

        if team_id == home_id:

            scored += home_score
            conceded += away_score

        else:

            scored += away_score
            conceded += home_score

    if games == 0:

        return 1.2, 1.2

    return (
        scored / games,
        conceded / games
    )


# =========================================================
# PREDICTION MODEL
# =========================================================

def poisson_probability(
    goals,
    expected
):

    if expected <= 0:
        return 0

    return (
        math.exp(-expected)
        * expected ** goals
        / math.factorial(goals)
    )


async def predict_match(
    match
):

    home_team = (
        match.get("homeTeam")
        or {}
    )

    away_team = (
        match.get("awayTeam")
        or {}
    )

    home_id = home_team.get(
        "id"
    )

    away_id = away_team.get(
        "id"
    )

    home_scored, home_conceded = (
        await get_average_goals(
            home_id
        )
    )

    away_scored, away_conceded = (
        await get_average_goals(
            away_id
        )
    )

    expected_home = max(
        0.2,
        min(
            (home_scored + away_conceded)
            / 2,
            4.5
        )
    )

    expected_away = max(
        0.2,
        min(
            (away_scored + home_conceded)
            / 2,
            4.5
        )
    )

    home_probability = 0
    draw_probability = 0
    away_probability = 0

    for home_goals in range(8):

        for away_goals in range(8):

            probability = (
                poisson_probability(
                    home_goals,
                    expected_home
                )
                *
                poisson_probability(
                    away_goals,
                    expected_away
                )
            )

            if home_goals > away_goals:

                home_probability += probability

            elif home_goals == away_goals:

                draw_probability += probability

            else:

                away_probability += probability

    total = (
        home_probability
        + draw_probability
        + away_probability
    )

    if total <= 0:

        return {
            "home": 33.3,
            "draw": 33.3,
            "away": 33.4
        }

    return {

        "home":
            home_probability
            / total
            * 100,

        "draw":
            draw_probability
            / total
            * 100,

        "away":
            away_probability
            / total
            * 100,

        "expected_home":
            expected_home,

        "expected_away":
            expected_away
    }


def calculate_odds(
    probability
):

    probability = max(
        1,
        min(
            99,
            probability
        )
    )

    return round(
        max(
            1.05,
            0.92
            / (probability / 100)
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

    if row:

        return dict(row)

    db.execute(
        """
        INSERT INTO wallets(
            user_id,
            balance,
            started
        )
        VALUES (?, 0, 0)
        """,
        (user_id,)
    )

    db.commit()

    return {
        "user_id": user_id,
        "balance": 0,
        "started": 0
    }


def add_money(
    user_id,
    amount
):

    get_wallet(user_id)

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

    get_wallet(user_id)

    cursor = db.execute(
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

    return cursor.rowcount == 1


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
    today = today_vn()

    row = db.execute(
        """
        SELECT last_claim
        FROM daily_claims
        WHERE user_id = ?
        """,
        (user_id,)
    ).fetchone()

    if row:

        if row["last_claim"] == today:

            await interaction.response.send_message(
                "❌ Hôm nay m đã nhận "
                "**50.000 💰** rồi.\n"
                "⏰ Qua 00:00 giờ Việt Nam "
                "mới nhận lại được.",
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
        INSERT INTO daily_claims(
            user_id,
            last_claim
        )
        VALUES (?, ?)

        ON CONFLICT(user_id)
        DO UPDATE SET
            last_claim = excluded.last_claim
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
            f"{interaction.user.mention}\n\n"
            f"Đã nhận **{money(DAILY_MONEY)}**"
        ),
        color=discord.Color.green()
    )

    embed.add_field(
        name="💳 Số dư",
        value=money(balance)
    )

    embed.set_footer(
        text="Mỗi người nhận 1 lần mỗi ngày."
    )

    await interaction.response.send_message(
        embed=embed
    )


# =========================================================
# /KHOINGHIEP
# =========================================================

@bot.tree.command(
    name="khoinghiep",
    description="Nhận vốn ban đầu"
)
async def khoinghiep(
    interaction
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
        f"🎉 Nhận **{money(STARTING_MONEY)}** "
        f"vốn ban đầu!"
    )


# =========================================================
# /VI
# =========================================================

@bot.tree.command(
    name="vi",
    description="Xem ví tiền"
)
async def vi(
    interaction
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
        title="💳 VÍ CỦA BẠN",
        color=discord.Color.gold()
    )

    embed.add_field(
        name="💰 Số dư",
        value=(
            f"**{money(wallet['balance'])}**"
        ),
        inline=False
    )

    embed.add_field(
        name="🎫 Tổng cược",
        value=str(total_bets),
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
# ADMIN MODAL
# =========================================================

class AdminMoneyModal(
    discord.ui.Modal,
    title="ADMIN - CỘNG XU"
):

    password = discord.ui.TextInput(
        label="Mật khẩu admin",
        placeholder="Nhập mật khẩu",
        required=True,
        max_length=100
    )

    target_user = discord.ui.TextInput(
        label="Discord User ID",
        placeholder="123456789012345678",
        required=True,
        max_length=30
    )

    amount = discord.ui.TextInput(
        label="Số xu cộng",
        placeholder="100000",
        required=True,
        max_length=15
    )

    async def on_submit(
        self,
        interaction
    ):

        if (
            self.password.value
            != ADMIN_PASSWORD
        ):

            await interaction.response.send_message(
                "❌ Sai mật khẩu admin.",
                ephemeral=True
            )

            return

        try:

            target_id = int(
                self.target_user.value.strip()
            )

            amount = int(
                self.amount.value
                .replace(",", "")
                .strip()
            )

        except ValueError:

            await interaction.response.send_message(
                "❌ User ID hoặc số xu không hợp lệ.",
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

        get_wallet(
            target_id
        )

        add_money(
            target_id,
            amount
        )

        new_balance = get_wallet(
            target_id
        )["balance"]

        await interaction.response.send_message(
            "✅ **ĐÃ CỘNG XU**\n\n"
            f"👤 User ID: `{target_id}`\n"
            f"💰 Cộng: **{money(amount)}**\n"
            f"💳 Số dư mới: **{money(new_balance)}**",
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
    interaction
):

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
# BET MODAL
# =========================================================

class BetAmountModal(
    discord.ui.Modal,
    title="Nhập số tiền cược"
):

    amount = discord.ui.TextInput(
        label="Số tiền cược",
        placeholder="Ví dụ: 5000",
        required=True,
        max_length=15
    )

    def __init__(
        self,
        match,
        choice,
        odds
    ):

        super().__init__()

        self.match = match
        self.choice = choice
        self.odds = odds

    async def on_submit(
        self,
        interaction
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
                f"❌ Không đủ xu.\n"
                f"💰 Ví: **{money(wallet['balance'])}**",
                ephemeral=True
            )

            return

        match_time = get_match_datetime(
            self.match
        )

        if (
            not match_time
            or match_time <= now_vn()
        ):

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
                "❌ Không thể trừ xu.",
                ephemeral=True
            )

            return

        home = get_home(
            self.match
        )

        away = get_away(
            self.match
        )

        db.execute(
            """
            INSERT INTO bets(
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
            VALUES (
                ?, ?, ?, ?, ?,
                ?, ?, 'PENDING',
                0, ?
            )
            """,
            (
                user_id,
                self.match["id"],
                home,
                away,
                self.choice,
                amount,
                self.odds,
                now_vn().isoformat()
            )
        )

        db.commit()

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
            value=money(
                get_wallet(user_id)["balance"]
            )
        )

        await interaction.response.send_message(
            embed=embed
        )


# =========================================================
# BET BUTTONS
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

        self.home_button.label = (
            f"{get_home(match)[:24]} "
            f"{calculate_odds(prediction['home']):.2f}"
        )

        self.draw_button.label = (
            f"Hòa "
            f"{calculate_odds(prediction['draw']):.2f}"
        )

        self.away_button.label = (
            f"{get_away(match)[:24]} "
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

            dt = get_match_datetime(
                match
            )

            options.append(
                discord.SelectOption(
                    label=(
                        f"{get_home(match)[:32]}"
                        f" vs "
                        f"{get_away(match)[:32]}"
                    ),
                    description=(
                        dt.strftime(
                            "%d/%m %H:%M"
                        )
                        if dt
                        else "Không rõ"
                    ),
                    value=str(
                        match["id"]
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
            str(match["id"]): match
            for match in matches
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

        embed = discord.Embed(
            title="🎯 CHỌN CỬA",
            description=(
                f"**{get_home(match)}** "
                f"🆚 "
                f"**{get_away(match)}**\n"
                f"🕐 "
                f"{format_datetime(get_match_datetime(match))}"
            ),
            color=discord.Color.blurple()
        )

        # FIXED: add_field được đóng đầy đủ
        embed.add_field(
            name="📊 Ước tính",
            value=(
                f"🏠 "
                f"{prediction['home']:.1f}%\n"
                f"🤝 "
                f"{prediction['draw']:.1f}%\n"
                f"✈️ "
                f"{prediction['away']:.1f}%"
            ),
            inline=True
        )

        embed.add_field(
            name="📈 Tỷ lệ",
            value=(
                f"🏠 "
                f"`{calculate_odds(prediction['home']):.2f}`\n"
                f"🤝 "
                f"`{calculate_odds(prediction['draw']):.2f}`\n"
                f"✈️ "
                f"`{calculate_odds(prediction['away']):.2f}`"
            ),
            inline=True
        )

        embed.set_footer(
            text="Chọn cửa rồi nhập số tiền cược."
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
            "❌ Dùng `/khoinghiep` "
            "hoặc `/comat` trước.",
            ephemeral=True
        )

        return

    if wallet["balance"] <= 0:

        await interaction.response.send_message(
            "❌ Ví đang hết xu.",
            ephemeral=True
        )

        return

    await interaction.response.defer()

    matches = await get_all_relevant_matches()

    future_matches = []

    for match in matches:

        dt = get_match_datetime(
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

        future_matches.append(
            match
        )

    future_matches.sort(
        key=lambda m:
        get_match_datetime(m)
    )

    if not future_matches:

        await interaction.followup.send(
            "❌ Không có trận sắp đá "
            "trong dữ liệu hiện tại."
        )

        return

    embed = discord.Embed(
        title="🎰 ĐẶT CƯỢC",
        description=(
            f"💰 Số dư: "
            f"**{money(wallet['balance'])}**\n\n"
            "Chọn trận bên dưới."
        ),
        color=discord.Color.green()
    )

    await interaction.followup.send(
        embed=embed,
        view=MatchSelectView(
            future_matches[:25]
        )
    )


# =========================================================
# SETTLEMENT
# =========================================================

async def settle_user_bets(
    user_id
):

    pending_bets = db.execute(
        """
        SELECT *
        FROM bets
        WHERE user_id = ?
        AND status = 'PENDING'
        """,
        (user_id,)
    ).fetchall()

    results = []

    for bet in pending_bets:

        data = await football.request(
            f"/matches/{bet['match_id']}",
            cache_seconds=60
        )

        if not data:
            continue

        match = data.get(
            "match"
        )

        if not match:
            continue

        if not is_finished(match):
            continue

        home_score, away_score = (
            get_score(match)
        )

        if (
            home_score is None
            or away_score is None
        ):
            continue

        if home_score > away_score:

            result = "HOME"

        elif home_score < away_score:

            result = "AWAY"

        else:

            result = "DRAW"

        won = (

            (
                bet["choice"]
                == "Đội nhà thắng"
                and result == "HOME"
            )

            or

            (
                bet["choice"]
                == "Đội khách thắng"
                and result == "AWAY"
            )

            or

            (
                bet["choice"]
                == "Hòa"
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
                home_score,
                away_score,
                won,
                payout
            )
        )

    return results


# =========================================================
# /KETTOAN
# =========================================================

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
            "⏳ Chưa có cược nào "
            "có kết quả."
        )

        return

    embed = discord.Embed(
        title="📋 KẾT TOÁN",
        color=discord.Color.gold()
    )

    for (
        bet,
        home_score,
        away_score,
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
            f"`{home_score}-{away_score}` "
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
# /SOI VIEW
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

        matches = await get_competition_matches()

        future = []

        for match in matches:

            dt = get_match_datetime(
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

            future.append(
                match
            )

        future.sort(
            key=lambda m:
            get_match_datetime(m)
        )

        if not future:

            await interaction.followup.send(
                "❌ Không có trận C1 "
                "trong 30 ngày tới."
            )

            return

        embeds = []

        for match in future[:10]:

            prediction = await predict_match(
                match
            )

            embed = discord.Embed(
                title="🏆 CHAMPIONS LEAGUE",
                description=(
                    f"**{get_home(match)}** "
                    f"🆚 "
                    f"**{get_away(match)}**"
                ),
                color=discord.Color.blurple()
            )

            embed.add_field(
                name="🕐 Thời gian",
                value=format_datetime(
                    get_match_datetime(match)
                ),
                inline=False
            )

            # FIXED
            embed.add_field(
                name="📊 Ước tính",
                value=(
                    f"🏠 "
                    f"{prediction['home']:.1f}%\n"
                    f"🤝 "
                    f"{prediction['draw']:.1f}%\n"
                    f"✈️ "
                    f"{prediction['away']:.1f}%"
                ),
                inline=True
            )

            embed.set_footer(
                text=(
                    "Ước tính thống kê, "
                    "không đảm bảo kết quả."
                )
            )

            embeds.append(
                embed
            )

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

        future = []

        for match in matches:

            dt = get_match_datetime(
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

            future.append(
                match
            )

        future.sort(
            key=lambda m:
            get_match_datetime(m)
        )

        if not future:

            await interaction.followup.send(
                "❌ Không tìm thấy trận "
                "Man City sắp tới."
            )

            return

        match = future[0]

        prediction = await predict_match(
            match
        )

        embed = discord.Embed(
            title="🔵 MAN CITY",
            description=(
                f"**{get_home(match)}** "
                f"🆚 "
                f"**{get_away(match)}**"
            ),
            color=discord.Color.blue()
        )

        embed.add_field(
            name="🕐 Thời gian",
            value=format_datetime(
                get_match_datetime(match)
            ),
            inline=False
        )

        # FIXED
        embed.add_field(
            name="📊 Ước tính",
            value=(
                f"🏠 "
                f"{prediction['home']:.1f}%\n"
                f"🤝 "
                f"{prediction['draw']:.1f}%\n"
                f"✈️ "
                f"{prediction['away']:.1f}%"
            ),
            inline=True
        )

        await interaction.followup.send(
            embed=embed
        )


# =========================================================
# /SOI
# =========================================================

@bot.tree.command(
    name="soi",
    description="Xem C1 hoặc Man City"
)
async def soi(
    interaction
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
# /PING
# =========================================================

@bot.tree.command(
    name="ping",
    description="Kiểm tra bot"
)
async def ping(
    interaction
):

    latency = round(
        bot.latency * 1000
    )

    await interaction.response.send_message(
        f"🏓 Pong! `{latency}ms`"
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

    await football.request(
        "/competitions/CL",
        cache_seconds=30
    )

    remaining = (
        football.remaining_requests
        if football.remaining_requests
        is not None
        else "Không rõ"
    )

    await interaction.followup.send(
        f"📡 Requests đã dùng: "
        f"`{football.total_requests}`\n"
        f"📊 API còn lại: "
        f"`{remaining}`",
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

        for user in users:

            await settle_user_bets(
                user["user_id"]
            )

    except Exception as error:

        print(
            "Auto settlement error:",
            repr(error)
        )


@auto_settlement.before_loop
async def before_auto_settlement():

    await bot.wait_until_ready()


# =========================================================
# ALERT CHANNEL
# =========================================================

def get_alert_channel():

    raw = ALERT_CHANNEL_ID

    if not raw:
        return None

    if (
        raw.startswith("<#")
        and raw.endswith(">")
    ):

        raw = raw[2:-1]

    try:

        channel_id = int(
            raw
        )

    except ValueError:

        return None

    return bot.get_channel(
        channel_id
    )


# =========================================================
# ALERT SYSTEM
# =========================================================

def alert_already_sent(
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

    if alert_already_sent(
        key
    ):
        return

    home = get_home(
        match
    )

    away = get_away(
        match
    )

    embed = discord.Embed(
        color=discord.Color.blue()
    )

    if alert_type == "PRE":

        embed.title = (
            "⏰ TRẬN SẮP BẮT ĐẦU"
        )

        embed.description = (
            f"⚽ **{home}** "
            f"🆚 "
            f"**{away}**\n"
            f"🕐 "
            f"{format_datetime(get_match_datetime(match))}"
        )

    elif alert_type == "LIVE":

        embed.title = (
            "🔴 TRẬN ĐANG DIỄN RA"
        )

        embed.description = (
            f"⚽ **{home}** "
            f"🆚 "
            f"**{away}**"
        )

    elif alert_type == "FINISHED":

        home_score, away_score = (
            get_score(match)
        )

        embed.title = (
            "🏁 TRẬN KẾT THÚC"
        )

        embed.description = (
            f"**{home}** "
            f"`{home_score}-{away_score}` "
            f"**{away}**"
        )

    else:

        return

    await channel.send(
        embed=embed
    )

    db.execute(
        """
        INSERT OR IGNORE INTO sent_alerts(
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


async def process_alerts():

    channel = get_alert_channel()

    if not channel:
        return

    current_time = now_vn()

    matches = await get_all_relevant_matches()

    for match in matches:

        match_time = get_match_datetime(
            match
        )

        if not match_time:
            continue

        minutes = int(
            (
                current_time
                - match_time
            ).total_seconds()
            / 60
        )

        status = match.get(
            "status"
        )

        if (
            -10 <= minutes < 0
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
            0 <= minutes <= 3
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
            and 0 <= minutes <= 180
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

    except Exception as error:

        print(
            "Alert loop error:",
            repr(error)
        )


@alert_loop.before_loop
async def before_alert_loop():

    await bot.wait_until_ready()


# =========================================================
# COMMAND SYNC
# =========================================================

@bot.event
async def setup_hook():

    guild = discord.Object(
        id=GUILD_ID
    )

    # Copy toàn bộ slash commands
    # vào server rồi sync ngay.
    bot.tree.copy_global_to(
        guild=guild
    )

    synced = await bot.tree.sync(
        guild=guild
    )

    print(
        f"Đã sync {len(synced)} slash commands."
    )


# =========================================================
# BOT READY
# =========================================================

@bot.event
async def on_ready():

    print("=" * 60)

    print(
        "BOT ONLINE:",
        bot.user
    )

    print(
        "GUILD:",
        GUILD_ID
    )

    print(
        "COMPETITION:",
        COMPETITION_CODE
    )

    print(
        "ALERT CHANNEL:",
        ALERT_CHANNEL_ID
    )

    print("=" * 60)

    await football.start()

    try:

        city = await get_mancity_matches()

        print(
            "Man City matches:",
            len(city)
        )

        for match in city[:5]:

            print(
                "CITY:",
                get_home(match),
                "vs",
                get_away(match),
                match.get("utcDate")
            )

    except Exception as error:

        print(
            "Man City test error:",
            repr(error)
        )

    try:

        cl = await get_competition_matches()

        print(
            "CL matches:",
            len(cl)
        )

    except Exception as error:

        print(
            "CL test error:",
            repr(error)
        )

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


# =========================================================
# RUN
# =========================================================

bot.run(TOKEN)

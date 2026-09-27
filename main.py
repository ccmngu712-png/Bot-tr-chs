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

GUILD_ID = 1551547049600618538

TIMEZONE = ZoneInfo("Asia/Ho_Chi_Minh")

FOOTBALL_ALERT_CHANNEL_ID = os.getenv("FOOTBALL_ALERT_CHANNEL_ID")
FOOTBALL_ALERT_COMPETITION = os.getenv(
    "FOOTBALL_ALERT_COMPETITION",
    "CL"
)

ADMIN_PASSWORD = "provip👑"

API_BASE = "https://api.football-data.org/v4"

MAN_CITY_ID = 65
CL_CODE = "CL"

DB_FILE = "football_bot.db"


# =========================================================
# CHECK TOKEN
# =========================================================

if not TOKEN:
    raise RuntimeError("Thiếu DISCORD_TOKEN")

if not FOOTBALL_DATA_TOKEN:
    print("⚠️ Chưa có FOOTBALL_DATA_TOKEN")


# =========================================================
# DISCORD
# =========================================================

intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


# =========================================================
# DATABASE
# =========================================================

db = sqlite3.connect(DB_FILE)
db.row_factory = sqlite3.Row

db.execute("""
CREATE TABLE IF NOT EXISTS wallets (
    user_id INTEGER PRIMARY KEY,
    balance INTEGER NOT NULL DEFAULT 0,
    total_bets INTEGER NOT NULL DEFAULT 0,
    pending_bets INTEGER NOT NULL DEFAULT 0,
    started INTEGER NOT NULL DEFAULT 0
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS daily_claims (
    user_id INTEGER NOT NULL,
    claim_date TEXT NOT NULL,
    PRIMARY KEY(user_id, claim_date)
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS bets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    match_id INTEGER NOT NULL,
    match_name TEXT NOT NULL,
    choice TEXT NOT NULL,
    amount INTEGER NOT NULL,
    odds REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    payout INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    settled_at TEXT
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS sent_alerts (
    match_id INTEGER PRIMARY KEY,
    sent_at TEXT NOT NULL
""")

db.commit()


# =========================================================
# DATABASE HELPERS
# =========================================================

def ensure_wallet(user_id: int):
    db.execute("""
        INSERT OR IGNORE INTO wallets
        (user_id, balance, total_bets, pending_bets, started)
        VALUES (?, 0, 0, 0, 0)
    """, (user_id,))
    db.commit()


def get_wallet(user_id: int):
    ensure_wallet(user_id)

    return db.execute(
        "SELECT * FROM wallets WHERE user_id = ?",
        (user_id,)
    ).fetchone()


def get_balance(user_id: int):
    return get_wallet(user_id)["balance"]


def add_wallet(user_id: int, amount: int):
    ensure_wallet(user_id)

    db.execute("""
        UPDATE wallets
        SET balance = balance + ?
        WHERE user_id = ?
    """, (amount, user_id))

    db.commit()


def remove_wallet(user_id: int, amount: int):
    ensure_wallet(user_id)

    db.execute("""
        UPDATE wallets
        SET balance = balance - ?
        WHERE user_id = ?
    """, (amount, user_id))

    db.commit()


# =========================================================
# FOOTBALL API
# =========================================================

class FootballAPI:

    def __init__(self):
        self.session = None
        self.cache = {}
        self.calls = []

    async def start(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={
                    "X-Auth-Token": FOOTBALL_DATA_TOKEN or ""
                }
            )

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def wait_rate_limit(self):

        now = asyncio.get_running_loop().time()

        self.calls = [
            x for x in self.calls
            if now - x < 60
        ]

        # Free Tier: 10 calls/minute
        # Giữ dưới 10 để an toàn
        if len(self.calls) >= 9:
            wait_time = 60 - (now - self.calls[0]) + 1

            if wait_time > 0:
                print(
                    f"⏳ API rate limit, chờ {wait_time:.1f}s"
                )
                await asyncio.sleep(wait_time)

        self.calls.append(
            asyncio.get_running_loop().time()
        )

    async def get(self, endpoint, params=None):

        await self.start()
        await self.wait_rate_limit()

        url = API_BASE + endpoint

        try:

            async with self.session.get(
                url,
                params=params
            ) as response:

                if response.status == 429:
                    print("⚠️ Football API: 429 rate limit")
                    return None

                if response.status == 403:
                    print("⚠️ Football API: 403 restricted")
                    return None

                if response.status != 200:
                    print(
                        f"⚠️ Football API lỗi {response.status}"
                    )
                    return None

                return await response.json()

        except Exception as e:
            print(
                f"❌ API error: {e}"
            )
            return None

    async def cached_get(
        self,
        endpoint,
        params=None,
        cache_seconds=300
    ):

        key = (
            endpoint,
            tuple(sorted((params or {}).items()))
        )

        now = asyncio.get_running_loop().time()

        if key in self.cache:

            timestamp, data = self.cache[key]

            if now - timestamp < cache_seconds:
                return data

        data = await self.get(
            endpoint,
            params=params
        )

        if data is not None:
            self.cache[key] = (
                now,
                data
            )

        return data

    async def get_mancity_matches(self):

        today = datetime.now(TIMEZONE).date()

        date_from = today - timedelta(days=2)
        date_to = today + timedelta(days=30)

        data = await self.cached_get(
            f"/teams/{MAN_CITY_ID}/matches",
            params={
                "dateFrom": date_from.isoformat(),
                "dateTo": date_to.isoformat(),
                "limit": 100
            },
            cache_seconds=300
        )

        if not data:
            return []

        return data.get("matches", [])

    async def get_cl_matches(self):

        today = datetime.now(TIMEZONE).date()

        date_from = today - timedelta(days=2)
        date_to = today + timedelta(days=30)

        data = await self.cached_get(
            f"/competitions/{CL_CODE}/matches",
            params={
                "dateFrom": date_from.isoformat(),
                "dateTo": date_to.isoformat(),
                "limit": 100
            },
            cache_seconds=300
        )

        if not data:
            return []

        return data.get("matches", [])

    async def get_team_finished(
        self,
        team_id,
        limit=5
    ):

        data = await self.cached_get(
            f"/teams/{team_id}/matches",
            params={
                "status": "FINISHED",
                "limit": limit
            },
            cache_seconds=600
        )

        if not data:
            return []

        return data.get("matches", [])


football = FootballAPI()


# =========================================================
# FOOTBALL HELPERS
# =========================================================

def match_datetime(match):

    raw = match.get("utcDate")

    if not raw:
        return None

    try:
        return datetime.fromisoformat(
            raw.replace("Z", "+00:00")
        )
    except Exception:
        return None


def local_match_time(match):

    dt = match_datetime(match)

    if not dt:
        return "Không rõ giờ"

    return dt.astimezone(TIMEZONE).strftime(
        "%d/%m/%Y %H:%M"
    )


def match_title(match):

    home = match.get("homeTeam", {}).get(
        "name",
        "?"
    )

    away = match.get("awayTeam", {}).get(
        "name",
        "?"
    )

    return f"{home} vs {away}"


def get_match_status(match):

    return match.get(
        "status",
        "SCHEDULED"
    )


def get_score(match):

    score = match.get("score", {})

    full = score.get(
        "fullTime",
        {}
    )

    home = full.get("home")
    away = full.get("away")

    if home is None or away is None:

        return None, None

    return home, away


def get_winner(match):

    score = match.get("score", {})

    winner = score.get("winner")

    return winner


# =========================================================
# CUSTOM PREDICTION
# =========================================================

async def team_average_goals(team_id):

    matches = await football.get_team_finished(
        team_id,
        limit=5
    )

    if not matches:
        return 1.2, 1.2

    scored = 0
    conceded = 0
    count = 0

    for match in matches:

        home_id = match.get(
            "homeTeam",
            {}
        ).get("id")

        away_id = match.get(
            "awayTeam",
            {}
        ).get("id")

        home_score, away_score = get_score(
            match
        )

        if (
            home_score is None
            or away_score is None
        ):
            continue

        if home_id == team_id:

            scored += home_score
            conceded += away_score

        elif away_id == team_id:

            scored += away_score
            conceded += home_score

        else:
            continue

        count += 1

    if count == 0:
        return 1.2, 1.2

    return (
        scored / count,
        conceded / count
    )


def poisson_probability(k, lam):

    try:
        return (
            math.exp(-lam)
            * pow(lam, k)
            / math.factorial(k)
        )
    except Exception:
        return 0


def predict_match(
    home_attack,
    home_defense,
    away_attack,
    away_defense
):

    home_lambda = (
        home_attack + away_defense
    ) / 2

    away_lambda = (
        away_attack + home_defense
    ) / 2

    home_win = 0
    draw = 0
    away_win = 0

    for home_goals in range(0, 7):

        for away_goals in range(0, 7):

            probability = (
                poisson_probability(
                    home_goals,
                    home_lambda
                )
                *
                poisson_probability(
                    away_goals,
                    away_lambda
                )
            )

            if home_goals > away_goals:
                home_win += probability

            elif home_goals == away_goals:
                draw += probability

            else:
                away_win += probability

    total = (
        home_win
        + draw
        + away_win
    )

    if total <= 0:
        return 0.33, 0.34, 0.33

    return (
        home_win / total,
        draw / total,
        away_win / total
    )


def probability_to_odds(probability):

    if probability <= 0:
        return 10.0

    # Custom odds, KHÔNG phải odds nhà cái
    odds = 1 / probability

    # Giới hạn cho bot
    return max(
        1.10,
        min(5.00, round(odds, 2))
    )


async def get_prediction(match):

    home_team = match.get(
        "homeTeam",
        {}
    )

    away_team = match.get(
        "awayTeam",
        {}
    )

    home_id = home_team.get("id")
    away_id = away_team.get("id")

    if not home_id or not away_id:
        return (
            0.33,
            0.34,
            0.33
        )

    (
        home_attack,
        home_defense
    ) = await team_average_goals(
        home_id
    )

    (
        away_attack,
        away_defense
    ) = await team_average_goals(
        away_id
    )

    return predict_match(
        home_attack,
        home_defense,
        away_attack,
        away_defense
    )


# =========================================================
# MATCH FIND
# =========================================================

def upcoming_matches(matches):

    now = datetime.now(
        timezone.utc
    ) if False else datetime.now(
        datetime.now(TIMEZONE).astimezone().tzinfo
    )

    result = []

    for match in matches:

        status = get_match_status(match)

        if status not in (
            "FINISHED",
            "CANCELLED",
            "POSTPONED"
        ):

            dt = match_datetime(match)

            if dt:
                result.append(match)

    result.sort(
        key=lambda x: match_datetime(x)
    )

    return result


# =========================================================
# WALLET COMMANDS
# =========================================================

@bot.tree.command(
    name="comat",
    description="Nhận 50.000 xu mỗi ngày"
)
async def comat(interaction: discord.Interaction):

    user_id = interaction.user.id

    ensure_wallet(user_id)

    today = datetime.now(
        TIMEZONE
    ).date().isoformat()

    existing = db.execute("""
        SELECT 1
        FROM daily_claims
        WHERE user_id = ?
        AND claim_date = ?
    """, (
        user_id,
        today
    )).fetchone()

    if existing:

        await interaction.response.send_message(
            "❌ Hôm nay m nhận xu rồi.",
            ephemeral=True
        )
        return

    db.execute("""
        INSERT INTO daily_claims
        (user_id, claim_date)
        VALUES (?, ?)
    """, (
        user_id,
        today
    ))

    add_wallet(
        user_id,
        50000
    )

    db.execute("""
        UPDATE wallets
        SET started = 1
        WHERE user_id = ?
    """, (user_id,))

    db.commit()

    balance = get_balance(user_id)

    await interaction.response.send_message(
        f"💰 M nhận **50.000 xu**.\n"
        f"💳 Số dư: **{balance:,} xu**",
        ephemeral=True
    )


@bot.tree.command(
    name="khoinghiep",
    description="Nhận 1.000 xu khởi nghiệp"
)
async def khoinghiep(
    interaction: discord.Interaction
):

    user_id = interaction.user.id

    wallet = get_wallet(user_id)

    if wallet["started"]:

        await interaction.response.send_message(
            "❌ M đã nhận tiền khởi nghiệp rồi.",
            ephemeral=True
        )
        return

    add_wallet(
        user_id,
        1000
    )

    db.execute("""
        UPDATE wallets
        SET started = 1
        WHERE user_id = ?
    """, (user_id,))

    db.commit()

    await interaction.response.send_message(
        "🚀 M nhận **1.000 xu khởi nghiệp**.",
        ephemeral=True
    )


@bot.tree.command(
    name="vi",
    description="Xem ví tiền"
)
async def vi(
    interaction: discord.Interaction
):

    wallet = get_wallet(
        interaction.user.id
    )

    await interaction.response.send_message(
        f"💳 **VÍ CỦA {interaction.user.display_name}**\n\n"
        f"💰 Số dư: **{wallet['balance']:,} xu**\n"
        f"🎯 Tổng cược: **{wallet['total_bets']}**\n"
        f"⏳ Đang cược: **{wallet['pending_bets']}**",
        ephemeral=True
    )


# =========================================================
# ADMIN
# =========================================================

class AdminModal(discord.ui.Modal):

    def __init__(self):

        super().__init__(
            title="👑 ADMIN CỘNG / TRỪ TIỀN"
        )

        self.password = discord.ui.TextInput(
            label="Mật khẩu",
            placeholder="Nhập mật khẩu admin",
            required=True,
            max_length=100
        )

        self.target = discord.ui.TextInput(
            label="Username / Nickname",
            placeholder="Nhập tên người cần chỉnh tiền",
            required=True,
            max_length=100
        )

        self.amount = discord.ui.TextInput(
            label="Số tiền",
            placeholder="Ví dụ: 50000 hoặc -50000",
            required=True,
            max_length=20
        )

        self.add_item(self.password)
        self.add_item(self.target)
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
            amount = int(
                self.amount.value.replace(",", "")
            )
        except ValueError:

            await interaction.response.send_message(
                "❌ Số tiền không hợp lệ.",
                ephemeral=True
            )
            return

        if amount == 0:

            await interaction.response.send_message(
                "❌ Số tiền không được bằng 0.",
                ephemeral=True
            )
            return

        if not interaction.guild:

            await interaction.response.send_message(
                "❌ Lệnh này chỉ dùng trong server.",
                ephemeral=True
            )
            return

        target_text = (
            self.target.value
            .strip()
            .lower()
        )

        target_member = None

        for member in interaction.guild.members:

            if (
                member.name.lower()
                == target_text
                or
                member.display_name.lower()
                == target_text
            ):

                target_member = member
                break

        if not target_member:

            await interaction.response.send_message(
                "❌ Không tìm thấy username/nickname đó.",
                ephemeral=True
            )
            return

        add_wallet(
            target_member.id,
            amount
        )

        balance = get_balance(
            target_member.id
        )

        sign = "+" if amount > 0 else ""

        await interaction.response.send_message(
            f"👑 Đã chỉnh tiền cho "
            f"**{target_member.display_name}**\n"
            f"💰 Thay đổi: **{sign}{amount:,} xu**\n"
            f"💳 Số dư mới: **{balance:,} xu**",
            ephemeral=True
        )


@bot.tree.command(
    name="admin",
    description="Admin cộng hoặc trừ tiền"
)
async def admin(
    interaction: discord.Interaction
):

    await interaction.response.send_modal(
        AdminModal()
    )


# =========================================================
# BET MODAL
# =========================================================

class BetAmountModal(discord.ui.Modal):

    def __init__(
        self,
        match,
        choice,
        odds
    ):

        super().__init__(
            title="💰 Nhập tiền cược"
        )

        self.match = match
        self.choice = choice
        self.odds = odds

        self.amount = discord.ui.TextInput(
            label="Số tiền cược",
            placeholder="Ví dụ: 10000",
            required=True,
            max_length=20
        )

        self.add_item(self.amount)

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        try:

            amount = int(
                self.amount.value.replace(",", "")
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

        balance = get_balance(
            user_id
        )

        if amount > balance:

            await interaction.response.send_message(
                f"❌ Không đủ xu.\n"
                f"💳 Số dư: **{balance:,} xu**",
                ephemeral=True
            )
            return

        match_id = self.match["id"]

        existing = db.execute("""
            SELECT id
            FROM bets
            WHERE user_id = ?
            AND match_id = ?
            AND status = 'PENDING'
        """, (
            user_id,
            match_id
        )).fetchone()

        if existing:

            await interaction.response.send_message(
                "❌ M đã cược trận này rồi.",
                ephemeral=True
            )
            return

        remove_wallet(
            user_id,
            amount
        )

        db.execute("""
            INSERT INTO bets
            (
                user_id,
                match_id,
                match_name,
                choice,
                amount,
                odds,
                status,
                payout,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 'PENDING', 0, ?)
        """, (
            user_id,
            match_id,
            match_title(self.match),
            self.choice,
            amount,
            self.odds,
            datetime.now(
                TIMEZONE
            ).isoformat()
        ))

        db.execute("""
            UPDATE wallets
            SET total_bets = total_bets + 1,
                pending_bets = pending_bets + 1
            WHERE user_id = ?
        """, (user_id,))

        db.commit()

        potential = int(
            amount * self.odds
        )

        new_balance = get_balance(
            user_id
        )

        await interaction.response.send_message(
            f"✅ **Đặt cược thành công!**\n\n"
            f"⚽ {match_title(self.match)}\n"
            f"🎯 Kèo: **{self.choice}**\n"
            f"💰 Cược: **{amount:,} xu**\n"
            f"📈 Hệ số: **{self.odds:.2f}**\n"
            f"🏆 Nếu thắng nhận: **{potential:,} xu**\n\n"
            f"💳 Số dư còn: **{new_balance:,} xu**",
            ephemeral=True
        )


# =========================================================
# BET CHOICE
# =========================================================

class BetChoiceView(discord.ui.View):

    def __init__(
        self,
        match,
        probabilities
    ):

        super().__init__(
            timeout=120
        )

        self.match = match

        home_prob, draw_prob, away_prob = probabilities

        self.home_odds = probability_to_odds(
            home_prob
        )

        self.draw_odds = probability_to_odds(
            draw_prob
        )

        self.away_odds = probability_to_odds(
            away_prob
        )

        home = match.get(
            "homeTeam",
            {}
        ).get(
            "name",
            "Đội nhà"
        )

        away = match.get(
            "awayTeam",
            {}
        ).get(
            "name",
            "Đội khách"
        )

        button1 = discord.ui.Button(
            label=f"{home[:30]} ({self.home_odds:.2f})",
            style=discord.ButtonStyle.primary
        )

        button2 = discord.ui.Button(
            label=f"Hòa ({self.draw_odds:.2f})",
            style=discord.ButtonStyle.secondary
        )

        button3 = discord.ui.Button(
            label=f"{away[:30]} ({self.away_odds:.2f})",
            style=discord.ButtonStyle.success
        )

        async def home_callback(interaction):

            await interaction.response.send_modal(
                BetAmountModal(
                    self.match,
                    "HOME",
                    self.home_odds
                )
            )

        async def draw_callback(interaction):

            await interaction.response.send_modal(
                BetAmountModal(
                    self.match,
                    "DRAW",
                    self.draw_odds
                )
            )

        async def away_callback(interaction):

            await interaction.response.send_modal(
                BetAmountModal(
                    self.match,
                    "AWAY",
                    self.away_odds
                )
            )

        button1.callback = home_callback
        button2.callback = draw_callback
        button3.callback = away_callback

        self.add_item(button1)
        self.add_item(button2)
        self.add_item(button3)


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

        self.matches = matches

        options = []

        for match in matches[:25]:

            options.append(
                discord.SelectOption(
                    label=match_title(match)[:100],
                    description=local_match_time(match),
                    value=str(match["id"])
                )
            )

        super().__init__(
            placeholder="⚽ Chọn trận muốn cược",
            options=options
        )

    async def callback(
        self,
        interaction: discord.Interaction
    ):

        match_id = int(
            self.values[0]
        )

        match = next(
            (
                x for x in self.matches
                if x["id"] == match_id
            ),
            None
        )

        if not match:

            await interaction.response.send_message(
                "❌ Không tìm thấy trận.",
                ephemeral=True
            )
            return

        await interaction.response.defer(
            ephemeral=True
        )

        probabilities = await get_prediction(
            match
        )

        home_prob, draw_prob, away_prob = probabilities

        home = match.get(
            "homeTeam",
            {}
        ).get(
            "name",
            "?"
        )

        away = match.get(
            "awayTeam",
            {}
        ).get(
            "name",
            "?"
        )

        embed = discord.Embed(
            title="🎯 CHỌN KÈO",
            description=(
                f"⚽ **{home} vs {away}**\n"
                f"🕐 {local_match_time(match)}\n\n"
                f"📊 Ước tính:\n"
                f"🏠 {home}: **{home_prob * 100:.1f}%**\n"
                f"🤝 Hòa: **{draw_prob * 100:.1f}%**\n"
                f"✈️ {away}: **{away_prob * 100:.1f}%**"
            )
        )

        await interaction.followup.send(
            embed=embed,
            view=BetChoiceView(
                match,
                probabilities
            ),
            ephemeral=True
        )


class MatchSelectView(
    discord.ui.View
):

    def __init__(
        self,
        matches
    ):

        super().__init__(
            timeout=120
        )

        self.add_item(
            MatchSelect(matches)
        )


# =========================================================
# CUOC COMMAND
# =========================================================

@bot.tree.command(
    name="cuoc",
    description="Chọn trận và đặt cược"
)
async def cuoc(
    interaction: discord.Interaction
):

    matches = await football.get_mancity_matches()

    matches = upcoming_matches(
        matches
    )

    # Chỉ lấy các trận Man City
    matches = matches[:25]

    if not matches:

        await interaction.response.send_message(
            "❌ Hiện không tìm thấy trận Man City sắp tới.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        "⚽ **CHỌN TRẬN ĐỂ CƯỢC**",
        view=MatchSelectView(matches),
        ephemeral=True
    )


# =========================================================
# SETTLEMENT
# =========================================================

def determine_bet_result(
    bet_choice,
    winner
):

    if winner == "HOME_TEAM":

        if bet_choice == "HOME":
            return "WIN"

        return "LOSE"

    if winner == "AWAY_TEAM":

        if bet_choice == "AWAY":
            return "WIN"

        return "LOSE"

    if winner == "DRAW":

        if bet_choice == "DRAW":
            return "WIN"

        return "LOSE"

    return None


async def send_bet_dm(
    user_id,
    match_name,
    home_score,
    away_score,
    bet_choice,
    amount,
    odds,
    result,
    payout,
    balance
):

    try:

        user = bot.get_user(
            user_id
        )

        if user is None:

            try:
                user = await bot.fetch_user(
                    user_id
                )
            except Exception:
                return

        if result == "WIN":

            embed = discord.Embed(
                title="🏆 CƯỢC THẮNG!",
                description=(
                    f"⚽ **{match_name}**\n\n"
                    f"📊 Kết quả: "
                    f"**{home_score} - {away_score}**\n"
                    f"🎯 Kèo của bạn: **{bet_choice}**\n\n"
                    f"🟢 **THẮNG**\n"
                    f"💰 Tiền nhận: **{payout:,} xu**\n"
                    f"💳 Số dư mới: **{balance:,} xu**"
                ),
                color=discord.Color.green()
            )

        elif result == "LOSE":

            embed = discord.Embed(
                title="❌ CƯỢC THUA",
                description=(
                    f"⚽ **{match_name}**\n\n"
                    f"📊 Kết quả: "
                    f"**{home_score} - {away_score}**\n"
                    f"🎯 Kèo của bạn: **{bet_choice}**\n\n"
                    f"🔴 **THUA**\n"
                    f"💸 Mất: **{amount:,} xu**\n"
                    f"💳 Số dư mới: **{balance:,} xu**"
                ),
                color=discord.Color.red()
            )

        else:

            embed = discord.Embed(
                title="🤝 CƯỢC HOÀN TIỀN",
                description=(
                    f"⚽ **{match_name}**\n\n"
                    f"📊 Kết quả: "
                    f"**{home_score} - {away_score}**\n"
                    f"🎯 Kèo của bạn: **{bet_choice}**\n\n"
                    f"🟡 Tiền cược được hoàn\n"
                    f"💰 Hoàn: **{payout:,} xu**\n"
                    f"💳 Số dư mới: **{balance:,} xu**"
                ),
                color=discord.Color.gold()
            )

        await user.send(
            embed=embed
        )

    except discord.Forbidden:

        print(
            f"⚠️ Không thể DM user {user_id}"
        )

    except Exception as e:

        print(
            f"⚠️ DM error {user_id}: {e}"
        )


async def settle_bet(
    bet,
    match
):

    winner = get_winner(
        match
    )

    home_score, away_score = get_score(
        match
    )

    if home_score is None or away_score is None:
        return False

    if not winner:
        return False

    result = determine_bet_result(
        bet["choice"],
        winner
    )

    if result is None:
        return False

    user_id = bet["user_id"]
    amount = bet["amount"]
    odds = bet["odds"]

    if result == "WIN":

        payout = int(
            round(amount * odds)
        )

        add_wallet(
            user_id,
            payout
        )

    else:

        # Nếu thua thì payout = 0
        payout = 0

    db.execute("""
        UPDATE bets
        SET status = ?,
            payout = ?,
            settled_at = ?
        WHERE id = ?
    """, (
        result,
        payout,
        datetime.now(
            TIMEZONE
        ).isoformat(),
        bet["id"]
    ))

    db.execute("""
        UPDATE wallets
        SET pending_bets =
            CASE
                WHEN pending_bets > 0
                THEN pending_bets - 1
                ELSE 0
            END
        WHERE user_id = ?
    """, (user_id,))

    db.commit()

    balance = get_balance(
        user_id
    )

    # ================================
    # DM RIÊNG CHO NGƯỜI CƯỢC
    # ================================

    await send_bet_dm(
        user_id=user_id,
        match_name=bet["match_name"],
        home_score=home_score,
        away_score=away_score,
        bet_choice=bet["choice"],
        amount=amount,
        odds=odds,
        result=result,
        payout=payout,
        balance=balance
    )

    return True


# =========================================================
# KETTOAN MANUAL
# =========================================================

@bot.tree.command(
    name="kettoan",
    description="Kết toán các cược đã có kết quả"
)
async def kettoan(
    interaction: discord.Interaction
):

    await interaction.response.defer(
        ephemeral=True
    )

    pending = db.execute("""
        SELECT *
        FROM bets
        WHERE status = 'PENDING'
    """).fetchall()

    if not pending:

        await interaction.followup.send(
            "ℹ️ Không có cược nào đang chờ kết toán.",
            ephemeral=True
        )
        return

    settled = 0

    cache = {}

    for bet in pending:

        match_id = bet["match_id"]

        if match_id not in cache:

            # Tìm trong CL
            cl_matches = await football.get_cl_matches()

            found = next(
                (
                    m for m in cl_matches
                    if m.get("id") == match_id
                ),
                None
            )

            if found is None:

                city_matches = await football.get_mancity_matches()

                found = next(
                    (
                        m for m in city_matches
                        if m.get("id") == match_id
                    ),
                    None
                )

            cache[match_id] = found

        match = cache[match_id]

        if not match:
            continue

        if get_match_status(match) != "FINISHED":
            continue

        ok = await settle_bet(
            bet,
            match
        )

        if ok:
            settled += 1

    await interaction.followup.send(
        f"✅ Đã kết toán **{settled} cược**.\n"
        f"📩 Kết quả đã được gửi DM riêng cho người cược.",
        ephemeral=True
    )


# =========================================================
# SOI
# =========================================================

class SoiView(
    discord.ui.View
):

    def __init__(self):

        super().__init__(
            timeout=120
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

        await interaction.response.defer(
            ephemeral=True
        )

        matches = await football.get_cl_matches()

        matches = upcoming_matches(
            matches
        )

        if not matches:

            await interaction.followup.send(
                "❌ Không tìm thấy trận C1 sắp tới trong khoảng thời gian API trả về.",
                ephemeral=True
            )
            return

        text = "🏆 **CHAMPIONS LEAGUE**\n\n"

        for match in matches[:10]:

            text += (
                f"⚽ **{match_title(match)}**\n"
                f"🕐 {local_match_time(match)}\n"
                f"📌 {get_match_status(match)}\n\n"
            )

        await interaction.followup.send(
            text,
            ephemeral=True
        )

    @discord.ui.button(
        label="🔵 Manchester City",
        style=discord.ButtonStyle.success
    )
    async def mancity(
        self,
        interaction,
        button
    ):

        await interaction.response.defer(
            ephemeral=True
        )

        matches = await football.get_mancity_matches()

        matches = upcoming_matches(
            matches
        )

        if not matches:

            await interaction.followup.send(
                "❌ Không tìm thấy trận Man City sắp tới.",
                ephemeral=True
            )
            return

        match = matches[0]

        home = match.get(
            "homeTeam",
            {}
        ).get(
            "name",
            "?"
        )

        away = match.get(
            "awayTeam",
            {}
        ).get(
            "name",
            "?"
        )

        probabilities = await get_prediction(
            match
        )

        home_prob, draw_prob, away_prob = probabilities

        home_odds = probability_to_odds(
            home_prob
        )

        draw_odds = probability_to_odds(
            draw_prob
        )

        away_odds = probability_to_odds(
            away_prob
        )

        embed = discord.Embed(
            title="🔵 MANCHESTER CITY",
            description=(
                f"⚽ **{home} vs {away}**\n"
                f"🕐 {local_match_time(match)}\n\n"
                f"📊 **Ước tính của bot**\n"
                f"🏠 {home}: "
                f"**{home_prob * 100:.1f}%** "
                f"({home_odds:.2f})\n"
                f"🤝 Hòa: "
                f"**{draw_prob * 100:.1f}%** "
                f"({draw_odds:.2f})\n"
                f"✈️ {away}: "
                f"**{away_prob * 100:.1f}%** "
                f"({away_odds:.2f})"
            )
        )

        embed.set_footer(
            text="Đây là ước tính thống kê của bot, không phải odds nhà cái."
        )

        await interaction.followup.send(
            embed=embed,
            ephemeral=True
        )


@bot.tree.command(
    name="soi",
    description="Xem trận C1 và Manchester City"
)
async def soi(
    interaction: discord.Interaction
):

    await interaction.response.send_message(
        "🔎 **SOI BÓNG ĐÁ**\n\n"
        "Chọn giải đấu muốn xem:",
        view=SoiView(),
        ephemeral=True
    )


# =========================================================
# PING
# =========================================================

@bot.tree.command(
    name="ping",
    description="Kiểm tra bot"
)
async def ping(
    interaction: discord.Interaction
):

    latency = round(
        bot.latency * 1000
    )

    await interaction.response.send_message(
        f"🏓 Pong!\n"
        f"📡 Ping: **{latency}ms**",
        ephemeral=True
    )


# =========================================================
# API QUOTA
# =========================================================

@bot.tree.command(
    name="apiquota",
    description="Xem trạng thái API"
)
async def apiquota(
    interaction: discord.Interaction
):

    now = asyncio.get_running_loop().time()

    football.calls = [
        x for x in football.calls
        if now - x < 60
    ]

    used = len(
        football.calls
    )

    await interaction.response.send_message(
        f"📡 **FOOTBALL API**\n\n"
        f"📊 Calls gần 60 giây: "
        f"**{used}/10**\n"
        f"🟢 API đang hoạt động.",
        ephemeral=True
    )


# =========================================================
# AUTO SETTLEMENT
# =========================================================

@tasks.loop(minutes=5)
async def auto_settlement():

    try:

        pending = db.execute("""
            SELECT *
            FROM bets
            WHERE status = 'PENDING'
        """).fetchall()

        if not pending:
            return

        cl_matches = await football.get_cl_matches()

        city_matches = await football.get_mancity_matches()

        all_matches = (
            cl_matches
            + city_matches
        )

        matches = {
            m.get("id"): m
            for m in all_matches
            if m.get("id")
        }

        for bet in pending:

            match = matches.get(
                bet["match_id"]
            )

            if not match:
                continue

            if get_match_status(match) != "FINISHED":
                continue

            await settle_bet(
                bet,
                match
            )

            # Nghỉ nhẹ để tránh spam API/DM
            await asyncio.sleep(0.2)

    except Exception as e:

        print(
            f"❌ Auto settlement error: {e}"
        )


# =========================================================
# ALERT
# =========================================================

@tasks.loop(minutes=1)
async def football_alert_loop():

    if not FOOTBALL_ALERT_CHANNEL_ID:
        return

    try:

        channel_id = int(
            FOOTBALL_ALERT_CHANNEL_ID
        )

    except ValueError:
        return

    channel = bot.get_channel(
        channel_id
    )

    if not channel:
        return

    try:

        matches = []

        if FOOTBALL_ALERT_COMPETITION == "CL":

            matches = await football.get_cl_matches()

        elif FOOTBALL_ALERT_COMPETITION == "CITY":

            matches = await football.get_mancity_matches()

        else:

            matches = (
                await football.get_cl_matches()
            )

        for match in matches:

            match_id = match.get("id")

            if not match_id:
                continue

            status = get_match_status(
                match
            )

            if status != "SCHEDULED":
                continue

            dt = match_datetime(
                match
            )

            if not dt:
                continue

            now = datetime.now(
                dt.tzinfo
            )

            seconds = (
                dt - now
            ).total_seconds()

            # Báo trước khoảng 30 phút
            if not (
                0 <= seconds <= 30 * 60
            ):
                continue

            exists = db.execute("""
                SELECT 1
                FROM sent_alerts
                WHERE match_id = ?
            """, (
                match_id,
            )).fetchone()

            if exists:
                continue

            embed = discord.Embed(
                title="⏰ SẮP ĐÁ!",
                description=(
                    f"⚽ **{match_title(match)}**\n"
                    f"🕐 {local_match_time(match)}\n\n"
                    f"🔥 Trận đấu sắp bắt đầu!"
                )
            )

            await channel.send(
                embed=embed
            )

            db.execute("""
                INSERT OR IGNORE INTO sent_alerts
                (match_id, sent_at)
                VALUES (?, ?)
            """, (
                match_id,
                datetime.now(
                    TIMEZONE
                ).isoformat()
            ))

            db.commit()

    except Exception as e:

        print(
            f"❌ Alert error: {e}"
        )


# =========================================================
# SETUP HOOK
# =========================================================

@bot.event
async def setup_hook():

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
        f"✅ Đã sync {len(synced)} slash commands."
    )


# =========================================================
# READY
# =========================================================

@bot.event
async def on_ready():

    print(
        f"✅ Bot online: {bot.user}"
    )

    print(
        f"🆔 Guild ID: {GUILD_ID}"
    )

    try:

        await football.start()

        print(
            "🌐 Football API session ready."
        )

    except Exception as e:

        print(
            f"⚠️ API session error: {e}"
        )

    # Test Man City
    try:

        matches = await football.get_mancity_matches()

        print(
            f"🔵 Man City API: {len(matches)} matches"
        )

        for match in matches[:5]:

            print(
                "   -",
                match_title(match),
                local_match_time(match),
                get_match_status(match)
            )

    except Exception as e:

        print(
            f"❌ Man City API test lỗi: {e}"
        )

    # Test Champions League
    try:

        matches = await football.get_cl_matches()

        print(
            f"🏆 Champions League API: {len(matches)} matches"
        )

        for match in matches[:5]:

            print(
                "   -",
                match_title(match),
                local_match_time(match),
                get_match_status(match)
            )

    except Exception as e:

        print(
            f"❌ CL API test lỗi: {e}"
        )

    # Start background tasks
    if not auto_settlement.is_running():

        auto_settlement.start()

    if not football_alert_loop.is_running():

        football_alert_loop.start()


# =========================================================
# SHUTDOWN
# =========================================================

async def shutdown():

    try:
        await football.close()
    except Exception:
        pass

    try:
        db.close()
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

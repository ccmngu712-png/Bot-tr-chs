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

TOKEN = os.getenv("DISCORD_TOKEN")
FOOTBALL_DATA_TOKEN = os.getenv("FOOTBALL_DATA_TOKEN")

GUILD_ID = 1551547049600618538

TZ = ZoneInfo("Asia/Ho_Chi_Minh")

ADMIN_PASSWORD = "provip👑"

API_BASE = "https://api.football-data.org/v4"

MAN_CITY_ID = 65
CL_CODE = "CL"

DB_FILE = "football_bot.db"

ALERT_CHANNEL_ID = os.getenv("FOOTBALL_ALERT_CHANNEL_ID")


# =========================================================
# CHECK
# =========================================================

if not TOKEN:
    raise RuntimeError("❌ Thiếu DISCORD_TOKEN")

if not FOOTBALL_DATA_TOKEN:
    print("⚠️ Thiếu FOOTBALL_DATA_TOKEN")


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
    PRIMARY KEY (user_id, claim_date)
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
)
""")

db.commit()

print("✅ SQLite database OK")


# =========================================================
# WALLET
# =========================================================

def ensure_wallet(user_id):
    db.execute("""
        INSERT OR IGNORE INTO wallets
        (user_id, balance, total_bets, pending_bets, started)
        VALUES (?, 0, 0, 0, 0)
    """, (user_id,))
    db.commit()


def get_wallet(user_id):
    ensure_wallet(user_id)

    return db.execute("""
        SELECT *
        FROM wallets
        WHERE user_id = ?
    """, (user_id,)).fetchone()


def get_balance(user_id):
    return get_wallet(user_id)["balance"]


def add_money(user_id, amount):
    ensure_wallet(user_id)

    db.execute("""
        UPDATE wallets
        SET balance = balance + ?
        WHERE user_id = ?
    """, (amount, user_id))

    db.commit()


def remove_money(user_id, amount):
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
                    "X-Auth-Token": FOOTBALL_DATA_TOKEN
                }
            )

    async def close(self):

        if self.session and not self.session.closed:
            await self.session.close()

    async def rate_limit(self):

        now = asyncio.get_running_loop().time()

        self.calls = [
            x for x in self.calls
            if now - x < 60
        ]

        if len(self.calls) >= 9:

            wait = 60 - (
                now - self.calls[0]
            ) + 1

            print(
                f"⏳ API limit, chờ {wait:.1f}s"
            )

            await asyncio.sleep(wait)

        self.calls.append(
            asyncio.get_running_loop().time()
        )

    async def get(self, endpoint, params=None):

        await self.start()
        await self.rate_limit()

        try:

            async with self.session.get(
                API_BASE + endpoint,
                params=params
            ) as response:

                if response.status == 200:
                    return await response.json()

                if response.status == 403:
                    print("❌ API 403 - token/quyền truy cập")

                elif response.status == 429:
                    print("❌ API 429 - quá giới hạn")

                else:
                    print(
                        f"❌ API HTTP {response.status}"
                    )

        except Exception as e:

            print(
                f"❌ API error: {e}"
            )

        return None

    async def cached_get(
        self,
        endpoint,
        params=None,
        seconds=300
    ):

        key = (
            endpoint,
            tuple(
                sorted(
                    (params or {}).items()
                )
            )
        )

        now = asyncio.get_running_loop().time()

        if key in self.cache:

            saved_time, data = self.cache[key]

            if now - saved_time < seconds:
                return data

        data = await self.get(
            endpoint,
            params
        )

        if data is not None:

            self.cache[key] = (
                now,
                data
            )

        return data

    async def man_city(self):

        today = datetime.now(TZ).date()

        data = await self.cached_get(
            f"/teams/{MAN_CITY_ID}/matches",
            {
                "dateFrom": (
                    today - timedelta(days=2)
                ).isoformat(),
                "dateTo": (
                    today + timedelta(days=30)
                ).isoformat(),
                "limit": 100
            },
            300
        )

        if not data:
            return []

        return data.get(
            "matches",
            []
        )

    async def champions_league(self):

        today = datetime.now(TZ).date()

        data = await self.cached_get(
            f"/competitions/{CL_CODE}/matches",
            {
                "dateFrom": (
                    today - timedelta(days=2)
                ).isoformat(),
                "dateTo": (
                    today + timedelta(days=30)
                ).isoformat(),
                "limit": 100
            },
            300
        )

        if not data:
            return []

        return data.get(
            "matches",
            []
        )

    async def team_finished(
        self,
        team_id,
        limit=5
    ):

        data = await self.cached_get(
            f"/teams/{team_id}/matches",
            {
                "status": "FINISHED",
                "limit": limit
            },
            600
        )

        if not data:
            return []

        return data.get(
            "matches",
            [])


football = FootballAPI()


# =========================================================
# MATCH HELPERS
# =========================================================

def match_datetime(match):

    raw = match.get("utcDate")

    if not raw:
        return None

    try:
        return datetime.fromisoformat(
            raw.replace(
                "Z",
                "+00:00"
            )
        )

    except Exception:
        return None


def match_time(match):

    dt = match_datetime(match)

    if not dt:
        return "Không rõ"

    return dt.astimezone(
        TZ
    ).strftime(
        "%d/%m/%Y %H:%M"
    )


def match_name(match):

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

    return f"{home} vs {away}"


def is_upcoming(match):

    status = match.get(
        "status"
    )

    if status in (
        "FINISHED",
        "CANCELLED",
        "POSTPONED"
    ):
        return False

    return match_datetime(match) is not None


def upcoming(matches):

    result = [
        x for x in matches
        if is_upcoming(x)
    ]

    result.sort(
        key=lambda x: match_datetime(x)
    )

    return result


def score(match):

    full = match.get(
        "score",
        {}
    ).get(
        "fullTime",
        {}
    )

    return (
        full.get("home"),
        full.get("away")
    )


# =========================================================
# PREDICTION
# =========================================================

async def team_average(team_id):

    matches = await football.team_finished(
        team_id,
        5
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

        home_score, away_score = score(
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


def poisson(k, lam):

    return (
        math.exp(-lam)
        * lam ** k
        / math.factorial(k)
    )


def predict(
    home_attack,
    home_defense,
    away_attack,
    away_defense
):

    home_lambda = (
        home_attack
        + away_defense
    ) / 2

    away_lambda = (
        away_attack
        + home_defense
    ) / 2

    home_win = 0
    draw = 0
    away_win = 0

    for h in range(7):

        for a in range(7):

            p = (
                poisson(h, home_lambda)
                *
                poisson(a, away_lambda)
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
        return (
            0.33,
            0.34,
            0.33
        )

    return (
        home_win / total,
        draw / total,
        away_win / total
    )


async def prediction(match):

    home_id = match.get(
        "homeTeam",
        {}
    ).get("id")

    away_id = match.get(
        "awayTeam",
        {}
    ).get("id")

    if not home_id or not away_id:

        return (
            0.33,
            0.34,
            0.33
        )

    ha, hd = await team_average(
        home_id
    )

    aa, ad = await team_average(
        away_id
    )

    return predict(
        ha,
        hd,
        aa,
        ad
    )


def odds(prob):

    if prob <= 0:
        return 5.0

    return max(
        1.10,
        min(
            5.00,
            round(1 / prob, 2)
        )
    )


# =========================================================
# /COMAT
# =========================================================

@bot.tree.command(
    name="comat",
    description="Nhận 50.000 xu mỗi ngày"
)
async def comat(interaction):

    uid = interaction.user.id

    today = datetime.now(
        TZ
    ).date().isoformat()

    exists = db.execute("""
        SELECT 1
        FROM daily_claims
        WHERE user_id = ?
        AND claim_date = ?
    """, (
        uid,
        today
    )).fetchone()

    if exists:

        await interaction.response.send_message(
            "❌ Hôm nay m nhận rồi.",
            ephemeral=True
        )

        return

    ensure_wallet(uid)

    db.execute("""
        INSERT INTO daily_claims
        (user_id, claim_date)
        VALUES (?, ?)
    """, (
        uid,
        today
    ))

    db.execute("""
        UPDATE wallets
        SET balance = balance + 50000
        WHERE user_id = ?
    """, (uid,))

    db.commit()

    await interaction.response.send_message(
        f"💰 M nhận **50.000 xu**!\n"
        f"💳 Số dư: **{get_balance(uid):,} xu**",
        ephemeral=True
    )


# =========================================================
# /KHOINGHIEP
# =========================================================

@bot.tree.command(
    name="khoinghiep",
    description="Nhận 1.000 xu khởi nghiệp"
)
async def khoinghiep(interaction):

    uid = interaction.user.id

    wallet = get_wallet(uid)

    if wallet["started"]:

        await interaction.response.send_message(
            "❌ M đã nhận tiền khởi nghiệp rồi.",
            ephemeral=True
        )

        return

    add_money(
        uid,
        1000
    )

    db.execute("""
        UPDATE wallets
        SET started = 1
        WHERE user_id = ?
    """, (uid,))

    db.commit()

    await interaction.response.send_message(
        "🚀 M nhận **1.000 xu** khởi nghiệp.",
        ephemeral=True
    )


# =========================================================
# /VI
# =========================================================

@bot.tree.command(
    name="vi",
    description="Xem số dư"
)
async def vi(interaction):

    wallet = get_wallet(
        interaction.user.id
    )

    await interaction.response.send_message(
        f"💳 **VÍ CỦA M**\n\n"
        f"💰 Số dư: **{wallet['balance']:,} xu**\n"
        f"🎯 Tổng cược: **{wallet['total_bets']}**\n"
        f"⏳ Đang cược: **{wallet['pending_bets']}**",
        ephemeral=True
    )


# =========================================================
# ADMIN MODAL
# =========================================================

class AdminModal(discord.ui.Modal):

    def __init__(self):

        super().__init__(
            title="👑 ADMIN"
        )

        self.password = discord.ui.TextInput(
            label="Mật khẩu",
            placeholder="Nhập mật khẩu",
            required=True,
            max_length=100
        )

        self.target = discord.ui.TextInput(
            label="Username / Nickname",
            placeholder="Tên người nhận tiền",
            required=True,
            max_length=100
        )

        self.amount = discord.ui.TextInput(
            label="Số tiền",
            placeholder="50000 hoặc -50000",
            required=True,
            max_length=20
        )

        self.add_item(self.password)
        self.add_item(self.target)
        self.add_item(self.amount)

    async def on_submit(self, interaction):

        if self.password.value != ADMIN_PASSWORD:

            await interaction.response.send_message(
                "❌ Sai mật khẩu.",
                ephemeral=True
            )

            return

        try:

            amount = int(
                self.amount.value.replace(
                    ",",
                    ""
                )
            )

        except ValueError:

            await interaction.response.send_message(
                "❌ Số tiền không hợp lệ.",
                ephemeral=True
            )

            return

        if amount == 0:

            await interaction.response.send_message(
                "❌ Không thể nhập 0.",
                ephemeral=True
            )

            return

        if not interaction.guild:

            await interaction.response.send_message(
                "❌ Chỉ dùng trong server.",
                ephemeral=True
            )

            return

        target = self.target.value.strip().lower()

        member = None

        for m in interaction.guild.members:

            if (
                m.name.lower() == target
                or
                m.display_name.lower() == target
            ):
                member = m
                break

        if not member:

            await interaction.response.send_message(
                "❌ Không tìm thấy người này.",
                ephemeral=True
            )

            return

        add_money(
            member.id,
            amount
        )

        sign = "+" if amount > 0 else ""

        await interaction.response.send_message(
            f"👑 **Đã chỉnh tiền**\n"
            f"👤 {member.display_name}\n"
            f"💰 {sign}{amount:,} xu\n"
            f"💳 Số dư: **{get_balance(member.id):,} xu**",
            ephemeral=True
        )


@bot.tree.command(
    name="admin",
    description="Admin cộng hoặc trừ tiền"
)
async def admin(interaction):

    await interaction.response.send_modal(
        AdminModal()
    )


# =========================================================
# BET AMOUNT MODAL
# =========================================================

class BetAmountModal(discord.ui.Modal):

    def __init__(
        self,
        match,
        choice,
        rate
    ):

        super().__init__(
            title="💰 Nhập tiền cược"
        )

        self.match = match
        self.choice = choice
        self.rate = rate

        self.amount = discord.ui.TextInput(
            label="Số tiền",
            placeholder="Ví dụ: 10000",
            required=True,
            max_length=20
        )

        self.add_item(
            self.amount
        )

    async def on_submit(self, interaction):

        try:

            amount = int(
                self.amount.value.replace(
                    ",",
                    ""
                )
            )

        except ValueError:

            await interaction.response.send_message(
                "❌ Số tiền không hợp lệ.",
                ephemeral=True
            )

            return

        if amount <= 0:

            await interaction.response.send_message(
                "❌ Số tiền phải > 0.",
                ephemeral=True
            )

            return

        uid = interaction.user.id

        balance = get_balance(uid)

        if amount > balance:

            await interaction.response.send_message(
                f"❌ Không đủ tiền.\n"
                f"💳 Số dư: **{balance:,} xu**",
                ephemeral=True
            )

            return

        match_id = self.match["id"]

        already = db.execute("""
            SELECT id
            FROM bets
            WHERE user_id = ?
            AND match_id = ?
            AND status = 'PENDING'
        """, (
            uid,
            match_id
        )).fetchone()

        if already:

            await interaction.response.send_message(
                "❌ M đã cược trận này rồi.",
                ephemeral=True
            )

            return

        remove_money(
            uid,
            amount
        )

        db.execute("""
            INSERT INTO bets (
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
            uid,
            match_id,
            match_name(self.match),
            self.choice,
            amount,
            self.rate,
            datetime.now(TZ).isoformat()
        ))

        db.execute("""
            UPDATE wallets
            SET total_bets = total_bets + 1,
                pending_bets = pending_bets + 1
            WHERE user_id = ?
        """, (uid,))

        db.commit()

        potential = int(
            amount * self.rate
        )

        await interaction.response.send_message(
            f"✅ **ĐẶT CƯỢC THÀNH CÔNG**\n\n"
            f"⚽ {match_name(self.match)}\n"
            f"🎯 Kèo: **{self.choice}**\n"
            f"💰 Cược: **{amount:,} xu**\n"
            f"📈 Hệ số: **{self.rate:.2f}**\n"
            f"🏆 Nếu thắng nhận: **{potential:,} xu**\n"
            f"💳 Còn lại: **{get_balance(uid):,} xu**",
            ephemeral=True
        )


# =========================================================
# BET BUTTONS
# =========================================================

class BetView(discord.ui.View):

    def __init__(
        self,
        match,
        probs
    ):

        super().__init__(
            timeout=120
        )

        self.match = match

        hp, dp, ap = probs

        ho = odds(hp)
        do = odds(dp)
        ao = odds(ap)

        home = match.get(
            "homeTeam",
            {}
        ).get(
            "name",
            "Home"
        )

        away = match.get(
            "awayTeam",
            {}
        ).get(
            "name",
            "Away"
        )

        b1 = discord.ui.Button(
            label=f"{home[:30]} ({ho:.2f})",
            style=discord.ButtonStyle.primary
        )

        b2 = discord.ui.Button(
            label=f"Hòa ({do:.2f})",
            style=discord.ButtonStyle.secondary
        )

        b3 = discord.ui.Button(
            label=f"{away[:30]} ({ao:.2f})",
            style=discord.ButtonStyle.success
        )

        async def home_callback(interaction):

            await interaction.response.send_modal(
                BetAmountModal(
                    self.match,
                    "HOME",
                    ho
                )
            )

        async def draw_callback(interaction):

            await interaction.response.send_modal(
                BetAmountModal(
                    self.match,
                    "DRAW",
                    do
                )
            )

        async def away_callback(interaction):

            await interaction.response.send_modal(
                BetAmountModal(
                    self.match,
                    "AWAY",
                    ao
                )
            )

        b1.callback = home_callback
        b2.callback = draw_callback
        b3.callback = away_callback

        self.add_item(b1)
        self.add_item(b2)
        self.add_item(b3)


# =========================================================
# MATCH SELECT
# =========================================================

class MatchSelect(discord.ui.Select):

    def __init__(self, matches):

        self.matches = matches

        options = []

        for match in matches[:25]:

            options.append(
                discord.SelectOption(
                    label=match_name(match)[:100],
                    description=match_time(match),
                    value=str(
                        match["id"]
                    )
                )
            )

        super().__init__(
            placeholder="⚽ Chọn trận",
            options=options
        )

    async def callback(self, interaction):

        mid = int(
            self.values[0]
        )

        match = next(
            (
                x for x in self.matches
                if x["id"] == mid
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

        probs = await prediction(
            match
        )

        hp, dp, ap = probs

        embed = discord.Embed(
            title="🎯 CHỌN KÈO",
            description=(
                f"⚽ **{match_name(match)}**\n"
                f"🕐 {match_time(match)}\n\n"
                f"📊 Ước tính:\n"
                f"🏠 **{hp * 100:.1f}%**\n"
                f"🤝 **{dp * 100:.1f}%**\n"
                f"✈️ **{ap * 100:.1f}%**"
            )
        )

        await interaction.followup.send(
            embed=embed,
            view=BetView(
                match,
                probs
            ),
            ephemeral=True
        )


class MatchView(discord.ui.View):

    def __init__(self, matches):

        super().__init__(
            timeout=120
        )

        self.add_item(
            MatchSelect(matches)
        )


# =========================================================
# /CUOC
# =========================================================

@bot.tree.command(
    name="cuoc",
    description="Đặt cược bóng đá"
)
async def cuoc(interaction):

    matches = await football.man_city()

    matches = upcoming(
        matches
    )[:25]

    if not matches:

        await interaction.response.send_message(
            "❌ Không tìm thấy trận Man City sắp tới.",
            ephemeral=True
        )

        return

    await interaction.response.send_message(
        "⚽ **CHỌN TRẬN ĐỂ CƯỢC**",
        view=MatchView(matches),
        ephemeral=True
    )


# =========================================================
# SETTLEMENT
# =========================================================

def bet_result(choice, winner):

    if winner == "HOME_TEAM":
        return "WIN" if choice == "HOME" else "LOSE"

    if winner == "AWAY_TEAM":
        return "WIN" if choice == "AWAY" else "LOSE"

    if winner == "DRAW":
        return "WIN" if choice == "DRAW" else "LOSE"

    return None


async def send_result_dm(
    bet,
    match,
    result,
    payout,
    balance
):

    try:

        user = bot.get_user(
            bet["user_id"]
        )

        if user is None:

            user = await bot.fetch_user(
                bet["user_id"]
            )

        hs, aws = score(match)

        if result == "WIN":

            title = "🏆 CƯỢC THẮNG!"
            color = discord.Color.green()

            text = (
                f"⚽ **{bet['match_name']}**\n"
                f"📊 Kết quả: **{hs} - {aws}**\n"
                f"🎯 Kèo: **{bet['choice']}**\n\n"
                f"🟢 **THẮNG**\n"
                f"💰 Nhận: **{payout:,} xu**\n"
                f"💳 Số dư: **{balance:,} xu**"
            )

        elif result == "LOSE":

            title = "❌ CƯỢC THUA"
            color = discord.Color.red()

            text = (
                f"⚽ **{bet['match_name']}**\n"
                f"📊 Kết quả: **{hs} - {aws}**\n"
                f"🎯 Kèo: **{bet['choice']}**\n\n"
                f"🔴 **THUA**\n"
                f"💸 Mất: **{bet['amount']:,} xu**\n"
                f"💳 Số dư: **{balance:,} xu**"
            )

        else:

            title = "🟡 CƯỢC HOÀN"
            color = discord.Color.gold()

            text = (
                f"⚽ **{bet['match_name']}**\n"
                f"📊 Kết quả: **{hs} - {aws}**\n"
                f"🎯 Kèo: **{bet['choice']}**\n\n"
                f"🟡 Tiền cược được hoàn\n"
                f"💰 Hoàn: **{payout:,} xu**\n"
                f"💳 Số dư: **{balance:,} xu**"
            )

        embed = discord.Embed(
            title=title,
            description=text,
            color=color
        )

        await user.send(
            embed=embed
        )

        print(
            f"📩 Đã DM kết quả cho {bet['user_id']}"
        )

    except discord.Forbidden:

        print(
            f"⚠️ User {bet['user_id']} chặn DM bot."
        )

    except Exception as e:

        print(
            f"❌ DM error: {e}"
        )


async def settle_one(
    bet,
    match
):

    winner = match.get(
        "score",
        {}
    ).get(
        "winner"
    )

    hs, aws = score(match)

    if hs is None or aws is None:
        return False

    if not winner:
        return False

    result = bet_result(
        bet["choice"],
        winner
    )

    if result is None:
        return False

    payout = 0

    if result == "WIN":

        payout = int(
            round(
                bet["amount"]
                * bet["odds"]
            )
        )

        add_money(
            bet["user_id"],
            payout
        )

    db.execute("""
        UPDATE bets
        SET status = ?,
            payout = ?,
            settled_at = ?
        WHERE id = ?
    """, (
        result,
        payout,
        datetime.now(TZ).isoformat(),
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
    """, (
        bet["user_id"],
    ))

    db.commit()

    balance = get_balance(
        bet["user_id"]
    )

    # CHỈ DM RIÊNG
    await send_result_dm(
        bet,
        match,
        result,
        payout,
        balance
    )

    return True


# =========================================================
# FIND MATCH
# =========================================================

async def find_match(match_id):

    cl = await football.champions_league()

    for match in cl:

        if match.get("id") == match_id:
            return match

    city = await football.man_city()

    for match in city:

        if match.get("id") == match_id:
            return match

    return None


# =========================================================
# /KETTOAN
# =========================================================

@bot.tree.command(
    name="kettoan",
    description="Kết toán cược đã có kết quả"
)
async def kettoan(interaction):

    await interaction.response.defer(
        ephemeral=True
    )

    bets = db.execute("""
        SELECT *
        FROM bets
        WHERE status = 'PENDING'
    """).fetchall()

    if not bets:

        await interaction.followup.send(
            "ℹ️ Không có cược chờ kết toán.",
            ephemeral=True
        )

        return

    settled = 0

    cache = {}

    for bet in bets:

        mid = bet["match_id"]

        if mid not in cache:

            cache[mid] = await find_match(
                mid
            )

        match = cache[mid]

        if not match:
            continue

        if match.get("status") != "FINISHED":
            continue

        if await settle_one(
            bet,
            match
        ):
            settled += 1

    await interaction.followup.send(
        f"✅ Đã kết toán **{settled} cược**.\n"
        f"📩 Kết quả được gửi DM riêng.",
        ephemeral=True
    )


# =========================================================
# /SOI
# =========================================================

class SoiView(discord.ui.View):

    def __init__(self):

        super().__init__(
            timeout=120
        )

    @discord.ui.button(
        label="🏆 Champions League",
        style=discord.ButtonStyle.primary
    )
    async def cl_button(
        self,
        interaction,
        button
    ):

        await interaction.response.defer(
            ephemeral=True
        )

        matches = await football.champions_league()

        matches = upcoming(
            matches
        )

        if not matches:

            await interaction.followup.send(
                "❌ Không tìm thấy trận C1 sắp tới.",
                ephemeral=True
            )

            return

        text = "🏆 **CHAMPIONS LEAGUE**\n\n"

        for match in matches[:10]:

            text += (
                f"⚽ **{match_name(match)}**\n"
                f"🕐 {match_time(match)}\n\n"
            )

        await interaction.followup.send(
            text,
            ephemeral=True
        )

    @discord.ui.button(
        label="🔵 Manchester City",
        style=discord.ButtonStyle.success
    )
    async def city_button(
        self,
        interaction,
        button
    ):

        await interaction.response.defer(
            ephemeral=True
        )

        matches = await football.man_city()

        matches = upcoming(
            matches
        )

        if not matches:

            await interaction.followup.send(
                "❌ Không tìm thấy trận Man City.",
                ephemeral=True
            )

            return

        match = matches[0]

        hp, dp, ap = await prediction(
            match
        )

        embed = discord.Embed(
            title="🔵 MANCHESTER CITY",
            description=(
                f"⚽ **{match_name(match)}**\n"
                f"🕐 {match_time(match)}\n\n"
                f"📊 **Ước tính của bot**\n"
                f"🏠 **{hp * 100:.1f}%**\n"
                f"🤝 **{dp * 100:.1f}%**\n"
                f"✈️ **{ap * 100:.1f}%**"
            )
        )

        embed.set_footer(
            text="Ước tính thống kê, không phải odds nhà cái."
        )

        await interaction.followup.send(
            embed=embed,
            ephemeral=True
        )


@bot.tree.command(
    name="soi",
    description="Xem C1 và Manchester City"
)
async def soi(interaction):

    await interaction.response.send_message(
        "🔎 **SOI BÓNG ĐÁ**\n\n"
        "Chọn giải đấu:",
        view=SoiView(),
        ephemeral=True
    )


# =========================================================
# /PING
# =========================================================

@bot.tree.command(
    name="ping",
    description="Kiểm tra bot"
)
async def ping(interaction):

    ms = round(
        bot.latency * 1000
    )

    await interaction.response.send_message(
        f"🏓 Pong!\n"
        f"📡 Ping: **{ms}ms**",
        ephemeral=True
    )


# =========================================================
# /APIQUOTA
# =========================================================

@bot.tree.command(
    name="apiquota",
    description="Xem API"
)
async def apiquota(interaction):

    now = asyncio.get_running_loop().time()

    football.calls = [
        x for x in football.calls
        if now - x < 60
    ]

    await interaction.response.send_message(
        f"📡 API calls: "
        f"**{len(football.calls)}/10** trong 60 giây.",
        ephemeral=True
    )


# =========================================================
# AUTO SETTLEMENT
# =========================================================

@tasks.loop(minutes=5)
async def auto_settle():

    try:

        bets = db.execute("""
            SELECT *
            FROM bets
            WHERE status = 'PENDING'
        """).fetchall()

        if not bets:
            return

        cl = await football.champions_league()
        city = await football.man_city()

        matches = {}

        for match in cl + city:

            if match.get("id"):
                matches[
                    match["id"]
                ] = match

        for bet in bets:

            match = matches.get(
                bet["match_id"]
            )

            if not match:
                continue

            if match.get("status") != "FINISHED":
                continue

            await settle_one(
                bet,
                match
            )

            await asyncio.sleep(
                0.2
            )

    except Exception as e:

        print(
            f"❌ Auto settle error: {e}"
        )


# =========================================================
# ALERT
# =========================================================

@tasks.loop(minutes=1)
async def alert_loop():

    if not ALERT_CHANNEL_ID:
        return

    try:

        channel = bot.get_channel(
            int(ALERT_CHANNEL_ID)
        )

        if not channel:
            return

        matches = (
            await football.champions_league()
        )

        now = datetime.now(
            timezone.utc
        )

        for match in matches:

            if match.get("status") != "SCHEDULED":
                continue

            dt = match_datetime(match)

            if not dt:
                continue

            seconds = (
                dt.astimezone(timezone.utc)
                - now
            ).total_seconds()

            if not (
                0 <= seconds <= 1800
            ):
                continue

            exists = db.execute("""
                SELECT 1
                FROM sent_alerts
                WHERE match_id = ?
            """, (
                match["id"],
            )).fetchone()

            if exists:
                continue

            embed = discord.Embed(
                title="⏰ TRẬN SẮP BẮT ĐẦU!",
                description=(
                    f"⚽ **{match_name(match)}**\n"
                    f"🕐 {match_time(match)}"
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
                match["id"],
                datetime.now(TZ).isoformat()
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
        f"✅ Đã sync {len(synced)} lệnh."
    )


# =========================================================
# READY
# =========================================================

@bot.event
async def on_ready():

    print(
        f"✅ BOT ONLINE: {bot.user}"
    )

    try:

        await football.start()

        city = await football.man_city()

        print(
            f"🔵 Man City: {len(city)} trận"
        )

        cl = await football.champions_league()

        print(
            f"🏆 Champions League: {len(cl)} trận"
        )

    except Exception as e:

        print(
            f"⚠️ API test: {e}"
        )

    if not auto_settle.is_running():
        auto_settle.start()

    if not alert_loop.is_running():
        alert_loop.start()


# =========================================================
# RUN
# =========================================================

try:

    bot.run(TOKEN)

finally:

    try:
        asyncio.run(
            football.close()
        )
    except Exception:
        pass

    try:
        db.close()
    except Exception:
        pass

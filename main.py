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

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
FOOTBALL_DATA_TOKEN = os.getenv("FOOTBALL_DATA_TOKEN")

GUILD_ID = 1551547049600618538
TIMEZONE = ZoneInfo("Asia/Ho_Chi_Minh")

ALERT_CHANNEL_ID = int(
    os.getenv("FOOTBALL_ALERT_CHANNEL_ID", "0") or 0
)

API_BASE = "https://api.football-data.org/v4"

ADMIN_PASSWORD = "provip👑"

MANCHESTER_CITY_ID = 65


# =========================================================
# CÁC GIẢI FREE CỦA FOOTBALL-DATA
# =========================================================

COMPETITIONS = {
    "PL": ("🏴", "Premier League"),
    "CL": ("🏆", "Champions League"),
    "PD": ("🇪🇸", "La Liga"),
    "SA": ("🇮🇹", "Serie A"),
    "BL1": ("🇩🇪", "Bundesliga"),
    "FL1": ("🇫🇷", "Ligue 1"),
    "DED": ("🇳🇱", "Eredivisie"),
    "PPL": ("🇵🇹", "Primeira Liga"),
    "ELC": ("🏴", "Championship"),
}


# =========================================================
# GIẢI MAN CITY ĐANG DÙNG
# =========================================================

CITY_COMPETITIONS = {
    "PL": ("🏴", "Premier League"),
    "CL": ("🏆", "Champions League"),
}


# =========================================================
# VIP
# =========================================================

VIP_LEVELS = [
    (10, 1_000_000),
    (9, 800_000),
    (8, 650_000),
    (7, 500_000),
    (6, 350_000),
    (5, 250_000),
    (4, 175_000),
    (3, 125_000),
    (2, 100_000),
    (1, 50_000),
]

VIP_EMOJIS = {
    1: "🥉",
    2: "🥉",
    3: "🥈",
    4: "🥈",
    5: "🥇",
    6: "💎",
    7: "💎",
    8: "👑",
    9: "👑",
    10: "🔥",
}

VIP_COLORS = {
    1: 0x95A5A6,
    2: 0x95A5A6,
    3: 0xBDC3C7,
    4: 0xBDC3C7,
    5: 0xF1C40F,
    6: 0x3498DB,
    7: 0x3498DB,
    8: 0x9B59B6,
    9: 0x9B59B6,
    10: 0xE74C3C,
}


def get_vip_level(balance: int) -> int:
    for level, minimum in VIP_LEVELS:
        if balance >= minimum:
            return level
    return 0


def vip_name(level: int) -> str:
    return f"VIP {level}" if level > 0 else "Thường"


# =========================================================
# DATABASE
# =========================================================

DB_FILE = "football_bot.db"

db = sqlite3.connect(
    DB_FILE,
    check_same_thread=False
)

db.row_factory = sqlite3.Row

db.execute("""
CREATE TABLE IF NOT EXISTS wallets (
    user_id INTEGER PRIMARY KEY,
    balance INTEGER NOT NULL DEFAULT 0,
    total_bets INTEGER NOT NULL DEFAULT 0,
    pending_bets INTEGER NOT NULL DEFAULT 0,
    started INTEGER NOT NULL DEFAULT 0,
    vip INTEGER NOT NULL DEFAULT 0
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

try:
    db.execute("""
        ALTER TABLE wallets
        ADD COLUMN vip INTEGER NOT NULL DEFAULT 0
    """)
    db.commit()
except sqlite3.OperationalError:
    pass

db.commit()


# =========================================================
# WALLET
# =========================================================

def ensure_wallet(user_id: int):
    db.execute("""
        INSERT OR IGNORE INTO wallets
        (
            user_id,
            balance,
            total_bets,
            pending_bets,
            started,
            vip
        )
        VALUES (?, 0, 0, 0, 0, 0)
    """, (user_id,))
    db.commit()


def get_wallet(user_id: int):
    ensure_wallet(user_id)

    return db.execute("""
        SELECT *
        FROM wallets
        WHERE user_id = ?
    """, (user_id,)).fetchone()


def get_balance(user_id: int) -> int:
    return int(
        get_wallet(user_id)["balance"]
    )


def change_balance(
    user_id: int,
    amount: int
):
    ensure_wallet(user_id)

    db.execute("""
        UPDATE wallets
        SET balance = balance + ?
        WHERE user_id = ?
    """, (
        amount,
        user_id
    ))

    db.commit()


def set_vip(
    user_id: int,
    level: int
):
    ensure_wallet(user_id)

    db.execute("""
        UPDATE wallets
        SET vip = ?
        WHERE user_id = ?
    """, (
        level,
        user_id
    ))

    db.commit()


# =========================================================
# BOT
# =========================================================

intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


# =========================================================
# FOOTBALL API
# =========================================================

class FootballAPI:

    def __init__(self):

        self.session = None

        self.cache = {}

        self.request_times = []

    async def start(self):

        if (
            self.session is None
            or self.session.closed
        ):

            self.session = aiohttp.ClientSession(
                headers={
                    "X-Auth-Token": FOOTBALL_DATA_TOKEN
                }
            )

    async def close(self):

        if (
            self.session
            and not self.session.closed
        ):

            await self.session.close()

    async def request(
        self,
        endpoint,
        params=None
    ):

        await self.start()

        now = asyncio.get_running_loop().time()

        self.request_times = [
            t for t in self.request_times
            if now - t < 60
        ]

        if len(self.request_times) >= 9:

            wait_time = (
                60
                - (
                    now
                    - self.request_times[0]
                )
                + 1
            )

            await asyncio.sleep(
                max(wait_time, 1)
            )

        self.request_times.append(
            asyncio.get_running_loop().time()
        )

        try:

            async with self.session.get(
                API_BASE + endpoint,
                params=params,
                timeout=aiohttp.ClientTimeout(
                    total=20
                )
            ) as response:

                if response.status == 200:
                    return await response.json()

                if response.status == 429:

                    print(
                        "Football API rate limit."
                    )

                    await asyncio.sleep(10)

                    return None

                text = await response.text()

                print(
                    "Football API error:",
                    response.status,
                    text[:300]
                )

                return None

        except Exception as e:

            print(
                "Football API exception:",
                e
            )

            return None

    # -----------------------------------------------------
    # LẤY TRẬN THEO GIẢI
    # -----------------------------------------------------

    async def get_competition_matches(
        self,
        competition_code: str,
        days_before=2,
        days_after=90
    ):

        cache_key = (
            "competition",
            competition_code
        )

        cached = self.cache.get(
            cache_key
        )

        if cached:

            saved_time, data = cached

            if (
                datetime.now().timestamp()
                - saved_time
                < 300
            ):

                return data

        today = datetime.now(
            TIMEZONE
        ).date()

        date_from = (
            today
            - timedelta(
                days=days_before
            )
        )

        date_to = (
            today
            + timedelta(
                days=days_after
            )
        )

        data = await self.request(
            f"/competitions/{competition_code}/matches",
            {
                "dateFrom": date_from.isoformat(),
                "dateTo": date_to.isoformat()
            }
        )

        if not data:

            return []

        matches = data.get(
            "matches",
            []
        )

        self.cache[cache_key] = (
            datetime.now().timestamp(),
            matches
        )

        return matches

    # -----------------------------------------------------
    # LẤY TRẬN MAN CITY THEO TEAM ID
    # -----------------------------------------------------

    async def man_city_matches(self):

        cache_key = (
            "team",
            MANCHESTER_CITY_ID
        )

        cached = self.cache.get(
            cache_key
        )

        if cached:

            saved_time, data = cached

            if (
                datetime.now().timestamp()
                - saved_time
                < 180
            ):

                return data

        today = datetime.now(
            TIMEZONE
        ).date()

        date_from = (
            today
            - timedelta(days=7)
        )

        date_to = (
            today
            + timedelta(days=90)
        )

        data = await self.request(
            f"/teams/{MANCHESTER_CITY_ID}/matches",
            {
                "dateFrom": date_from.isoformat(),
                "dateTo": date_to.isoformat()
            }
        )

        if not data:

            return []

        matches = data.get(
            "matches",
            []
        )

        self.cache[cache_key] = (
            datetime.now().timestamp(),
            matches
        )

        return matches

    # -----------------------------------------------------
    # CITY UPCOMING
    # -----------------------------------------------------

    async def man_city_upcoming(self):

        matches = await self.man_city_matches()

        now = datetime.now(
            TIMEZONE
        )

        result = []

        for match in matches:

            utc_date = match.get(
                "utcDate"
            )

            if not utc_date:
                continue

            try:

                dt = datetime.fromisoformat(
                    utc_date.replace(
                        "Z",
                        "+00:00"
                    )
                )

                dt = dt.astimezone(
                    TIMEZONE
                )

            except Exception:

                continue

            # Không lấy trận đã bắt đầu/kết thúc
            if dt > now:

                result.append(match)

        result.sort(
            key=lambda x: x.get(
                "utcDate",
                ""
            )
        )

        return result

    # -----------------------------------------------------
    # CHAMPIONS LEAGUE
    # -----------------------------------------------------

    async def champions_league(self):

        return await self.get_competition_matches(
            "CL",
            2,
            120
        )

    # -----------------------------------------------------
    # PREMIER LEAGUE
    # -----------------------------------------------------

    async def premier_league(self):

        return await self.get_competition_matches(
            "PL",
            2,
            120
        )


football = FootballAPI()


# =========================================================
# HELPER
# =========================================================

def now_vn():

    return datetime.now(
        TIMEZONE
    )


def match_display_name(match):

    home = match.get(
        "homeTeam",
        {}
    ).get(
        "name",
        "???"
    )

    away = match.get(
        "awayTeam",
        {}
    ).get(
        "name",
        "???"
    )

    return f"{home} vs {away}"


def format_match_date(match):

    utc_date = match.get(
        "utcDate"
    )

    if not utc_date:

        return "Không rõ giờ"

    try:

        dt = datetime.fromisoformat(
            utc_date.replace(
                "Z",
                "+00:00"
            )
        )

        dt = dt.astimezone(
            TIMEZONE
        )

        return dt.strftime(
            "%d/%m/%Y • %H:%M"
        )

    except Exception:

        return "Không rõ giờ"


def is_future_match(match):

    utc_date = match.get(
        "utcDate"
    )

    if not utc_date:
        return False

    try:

        dt = datetime.fromisoformat(
            utc_date.replace(
                "Z",
                "+00:00"
            )
        )

        dt = dt.astimezone(
            TIMEZONE
        )

        return dt > now_vn()

    except Exception:

        return False


def is_finished(match):

    return match.get(
        "status"
    ) in {
        "FINISHED",
        "AWARDED"
    }


def choice_name(choice):

    if choice == "HOME":
        return "🏠 Đội nhà"

    if choice == "DRAW":
        return "⚖️ Hòa"

    if choice == "AWAY":
        return "✈️ Đội khách"

    return choice


def odds_for_choice(choice):

    if choice == "HOME":
        return 2.00

    if choice == "DRAW":
        return 3.20

    if choice == "AWAY":
        return 2.00

    return 2.00


def get_result_choice(match):

    score = match.get(
        "score",
        {}
    )

    full_time = score.get(
        "fullTime",
        {}
    )

    home = full_time.get(
        "home"
    )

    away = full_time.get(
        "away"
    )

    if home is None or away is None:
        return None

    if home > away:
        return "HOME"

    if home < away:
        return "AWAY"

    return "DRAW"


# =========================================================
# VIP NOTIFICATION
# =========================================================

async def send_vip_notification(
    user_id,
    level,
    balance,
    channel=None
):

    if level <= 0:
        return

    try:

        user = bot.get_user(
            user_id
        )

        if user is None:

            user = await bot.fetch_user(
                user_id
            )

        if channel is None:

            if ALERT_CHANNEL_ID:

                channel = bot.get_channel(
                    ALERT_CHANNEL_ID
                )

        if channel is None:
            return

        emoji = VIP_EMOJIS.get(
            level,
            "👑"
        )

        embed = discord.Embed(
            title=(
                f"{emoji} CHÚC MỪNG "
                f"VIP {level} {emoji}"
            ),
            description=(
                "╔════════════════════╗\n"
                f"🎉 **{user.display_name}**\n"
                "đã thăng cấp VIP!\n"
                "╚════════════════════╝\n\n"
                f"👤 Thành viên: {user.mention}\n"
                f"💰 Số dư: **{balance:,} xu**\n"
                f"🔥 Cấp mới: **VIP {level}**\n\n"
                f"{emoji} Chúc mừng m đã đạt "
                f"**VIP {level}**!"
            ),
            color=VIP_COLORS.get(
                level,
                0xF1C40F
            )
        )

        embed.set_footer(
            text="⚽ Football Betting • VIP System"
        )

        await channel.send(
            embed=embed
        )

    except Exception as e:

        print(
            "VIP notification error:",
            e
        )


async def check_vip(
    user_id,
    announce_channel=None
):

    wallet = get_wallet(
        user_id
    )

    balance = int(
        wallet["balance"]
    )

    old_vip = int(
        wallet["vip"] or 0
    )

    new_vip = get_vip_level(
        balance
    )

    if new_vip != old_vip:

        set_vip(
            user_id,
            new_vip
        )

    if new_vip > old_vip:

        await send_vip_notification(
            user_id,
            new_vip,
            balance,
            announce_channel
        )

    return new_vip


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

    ensure_wallet(
        user_id
    )

    today = now_vn().strftime(
        "%Y-%m-%d"
    )

    exists = db.execute("""
        SELECT 1
        FROM daily_claims
        WHERE user_id = ?
        AND claim_date = ?
    """, (
        user_id,
        today
    )).fetchone()

    if exists:

        await interaction.response.send_message(
            "❌ Hôm nay m nhận cơm rồi. Mai quay lại.",
            ephemeral=True
        )

        return

    db.execute("""
        INSERT INTO daily_claims
        (
            user_id,
            claim_date
        )
        VALUES (?, ?)
    """, (
        user_id,
        today
    ))

    db.commit()

    change_balance(
        user_id,
        50_000
    )

    vip = await check_vip(
        user_id,
        interaction.channel
    )

    await interaction.response.send_message(
        f"🍚 **NHẬN CƠM THÀNH CÔNG!**\n\n"
        f"💰 +50,000 xu\n"
        f"💵 Số dư: **{get_balance(user_id):,} xu**\n"
        f"👑 VIP: **{vip_name(vip)}**"
    )


# =========================================================
# /KHOINGHIEP
# =========================================================

@bot.tree.command(
    name="khoinghiep",
    description="Nhận 1.000 xu khởi nghiệp"
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
            "❌ M đã nhận tiền khởi nghiệp rồi.",
            ephemeral=True
        )

        return

    db.execute("""
        UPDATE wallets
        SET started = 1,
            balance = balance + 1000
        WHERE user_id = ?
    """, (
        user_id,
    ))

    db.commit()

    vip = await check_vip(
        user_id,
        interaction.channel
    )

    await interaction.response.send_message(
        f"🚀 **KHỞI NGHIỆP THÀNH CÔNG!**\n\n"
        f"💰 +1,000 xu\n"
        f"💵 Số dư: **{get_balance(user_id):,} xu**\n"
        f"👑 VIP: **{vip_name(vip)}**"
    )


# =========================================================
# /VI
# =========================================================

@bot.tree.command(
    name="vi",
    description="Xem ví và VIP"
)
async def vi(
    interaction: discord.Interaction
):

    user_id = interaction.user.id

    wallet = get_wallet(
        user_id
    )

    balance = int(
        wallet["balance"]
    )

    vip = get_vip_level(
        balance
    )

    if vip != wallet["vip"]:

        set_vip(
            user_id,
            vip
        )

    emoji = VIP_EMOJIS.get(
        vip,
        "👤"
    )

    embed = discord.Embed(
        title="💰 VÍ CỦA BẠN",
        color=VIP_COLORS.get(
            vip,
            0x3498DB
        )
    )

    embed.add_field(
        name="💵 Số dư",
        value=f"**{balance:,} xu**",
        inline=False
    )

    embed.add_field(
        name=f"{emoji} VIP",
        value=f"**{vip_name(vip)}**",
        inline=True
    )

    embed.add_field(
        name="🎯 Tổng cược",
        value=str(
            wallet["total_bets"]
        ),
        inline=True
    )

    embed.add_field(
        name="⏳ Đang chờ",
        value=str(
            wallet["pending_bets"]
        ),
        inline=True
    )

    next_vip = None

    for level, minimum in reversed(
        VIP_LEVELS
    ):

        if balance < minimum:

            next_vip = (
                level,
                minimum
            )

    if next_vip:

        level, minimum = next_vip

        embed.add_field(
            name="📈 VIP tiếp theo",
            value=(
                f"**VIP {level}**\n"
                f"Còn thiếu: "
                f"**{minimum - balance:,} xu**"
            ),
            inline=False
        )

    else:

        embed.add_field(
            name="🔥 MAX VIP",
            value="**VIP 10**",
            inline=False
        )

    await interaction.response.send_message(
        embed=embed
    )


# =========================================================
# ADMIN
# =========================================================

class AdminModal(
    discord.ui.Modal,
    title="👑 ADMIN - QUẢN LÝ XU"
):

    password = discord.ui.TextInput(
        label="Mật khẩu admin",
        required=True
    )

    target = discord.ui.TextInput(
        label="Username / Nickname",
        required=True
    )

    amount = discord.ui.TextInput(
        label="Số xu (+ thêm / - trừ)",
        placeholder="50000",
        required=True
    )

    async def on_submit(
        self,
        interaction
    ):

        if str(
            self.password
        ) != ADMIN_PASSWORD:

            await interaction.response.send_message(
                "❌ Sai mật khẩu.",
                ephemeral=True
            )

            return

        try:

            amount = int(
                str(
                    self.amount
                ).replace(",", "")
            )

        except ValueError:

            await interaction.response.send_message(
                "❌ Số xu không hợp lệ.",
                ephemeral=True
            )

            return

        target = str(
            self.target
        ).lower()

        member = None

        for m in interaction.guild.members:

            if (
                str(m.name).lower()
                == target
                or
                str(m.display_name).lower()
                == target
                or
                str(m).lower()
                == target
            ):

                member = m
                break

        if member is None:

            await interaction.response.send_message(
                "❌ Không tìm thấy người này.",
                ephemeral=True
            )

            return

        current = get_balance(
            member.id
        )

        if current + amount < 0:

            await interaction.response.send_message(
                "❌ Người này không đủ xu.",
                ephemeral=True
            )

            return

        change_balance(
            member.id,
            amount
        )

        vip = await check_vip(
            member.id,
            interaction.channel
        )

        await interaction.response.send_message(
            f"👑 **ADMIN THÀNH CÔNG**\n\n"
            f"👤 {member.mention}\n"
            f"💰 Thay đổi: `{amount:+,}` xu\n"
            f"💵 Số dư: `{get_balance(member.id):,}` xu\n"
            f"👑 VIP: **{vip_name(vip)}**"
        )


@bot.tree.command(
    name="admin",
    description="Quản lý xu thành viên"
)
async def admin(
    interaction: discord.Interaction
):

    await interaction.response.send_modal(
        AdminModal()
    )


# =========================================================
# BET AMOUNT
# =========================================================

class BetAmountModal(
    discord.ui.Modal,
    title="💰 NHẬP TIỀN CƯỢC"
):

    amount = discord.ui.TextInput(
        label="Số xu muốn cược",
        placeholder="Ví dụ: 10000",
        required=True
    )

    def __init__(
        self,
        match,
        choice
    ):

        super().__init__()

        self.match = match
        self.choice = choice

    async def on_submit(
        self,
        interaction
    ):

        user_id = interaction.user.id

        try:

            amount = int(
                str(
                    self.amount
                )
                .replace(",", "")
                .replace(".", "")
            )

        except ValueError:

            await interaction.response.send_message(
                "❌ Số tiền không hợp lệ.",
                ephemeral=True
            )

            return

        if amount <= 0:

            await interaction.response.send_message(
                "❌ Tiền cược phải lớn hơn 0.",
                ephemeral=True
            )

            return

        if amount > get_balance(
            user_id
        ):

            await interaction.response.send_message(
                f"❌ Không đủ xu.\n"
                f"💰 Ví: **{get_balance(user_id):,} xu**",
                ephemeral=True
            )

            return

        match_id = int(
            self.match["id"]
        )

        exists = db.execute("""
            SELECT id
            FROM bets
            WHERE user_id = ?
            AND match_id = ?
            AND status = 'PENDING'
        """, (
            user_id,
            match_id
        )).fetchone()

        if exists:

            await interaction.response.send_message(
                "❌ M đã cược trận này rồi.",
                ephemeral=True
            )

            return

        match_name = match_display_name(
            self.match
        )

        odds = odds_for_choice(
            self.choice
        )

        change_balance(
            user_id,
            -amount
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
            VALUES
            (?, ?, ?, ?, ?, ?, 'PENDING', 0, ?)
        """, (
            user_id,
            match_id,
            match_name,
            self.choice,
            amount,
            odds,
            now_vn().isoformat()
        ))

        db.execute("""
            UPDATE wallets
            SET total_bets = total_bets + 1,
                pending_bets = pending_bets + 1
            WHERE user_id = ?
        """, (
            user_id,
        ))

        db.commit()

        await check_vip(
            user_id,
            interaction.channel
        )

        embed = discord.Embed(
            title="🎟️ ĐẶT CƯỢC THÀNH CÔNG",
            color=0x2ECC71
        )

        embed.add_field(
            name="⚽ Trận",
            value=match_name,
            inline=False
        )

        embed.add_field(
            name="🎯 Cửa",
            value=choice_name(
                self.choice
            ),
            inline=True
        )

        embed.add_field(
            name="💰 Cược",
            value=f"`{amount:,}` xu",
            inline=True
        )

        embed.add_field(
            name="📈 Odds",
            value=f"`{odds:.2f}`",
            inline=True
        )

        embed.add_field(
            name="💵 Còn lại",
            value=f"`{get_balance(user_id):,}` xu",
            inline=False
        )

        await interaction.response.send_message(
            embed=embed
        )


# =========================================================
# CHỌN CỬA HOME / DRAW / AWAY
# =========================================================

class MatchChoiceView(
    discord.ui.View
):

    def __init__(
        self,
        user_id,
        match
    ):

        super().__init__(
            timeout=180
        )

        self.user_id = user_id
        self.match = match

    async def interaction_check(
        self,
        interaction
    ):

        if (
            interaction.user.id
            != self.user_id
        ):

            await interaction.response.send_message(
                "❌ Menu này không phải của m.",
                ephemeral=True
            )

            return False

        return True

    @discord.ui.button(
        label="Đội nhà",
        emoji="🏠",
        style=discord.ButtonStyle.primary
    )
    async def home(
        self,
        interaction,
        button
    ):

        await interaction.response.send_modal(
            BetAmountModal(
                self.match,
                "HOME"
            )
        )

    @discord.ui.button(
        label="Hòa",
        emoji="⚖️",
        style=discord.ButtonStyle.secondary
    )
    async def draw(
        self,
        interaction,
        button
    ):

        await interaction.response.send_modal(
            BetAmountModal(
                self.match,
                "DRAW"
            )
        )

    @discord.ui.button(
        label="Đội khách",
        emoji="✈️",
        style=discord.ButtonStyle.success
    )
    async def away(
        self,
        interaction,
        button
    ):

        await interaction.response.send_modal(
            BetAmountModal(
                self.match,
                "AWAY"
            )
        )


# =========================================================
# CHỌN TRẬN
# =========================================================

class MatchSelect(
    discord.ui.Select
):

    def __init__(
        self,
        user_id,
        matches
    ):

        self.user_id = user_id
        self.matches = matches

        options = []

        for match in matches:

            name = match_display_name(
                match
            )

            options.append(
                discord.SelectOption(
                    label=name[:100],
                    value=str(
                        match["id"]
                    ),
                    description=format_match_date(
                        match
                    )[:100]
                )
            )

        super().__init__(
            placeholder="⚽ Chọn trận...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(
        self,
        interaction
    ):

        if (
            interaction.user.id
            != self.user_id
        ):

            await interaction.response.send_message(
                "❌ Menu này không phải của m.",
                ephemeral=True
            )

            return

        match_id = int(
            self.values[0]
        )

        match = next(
            (
                m for m in self.matches
                if int(m["id"])
                == match_id
            ),
            None
        )

        if not match:

            await interaction.response.send_message(
                "❌ Không tìm thấy trận.",
                ephemeral=True
            )

            return

        embed = discord.Embed(
            title="🎯 CHỌN KÈO",
            description=(
                f"⚽ **{match_display_name(match)}**\n"
                f"🕐 {format_match_date(match)}\n\n"
                "Chọn cửa:"
            ),
            color=0xF1C40F
        )

        embed.add_field(
            name="🏠 Đội nhà",
            value="Odds `2.00`",
            inline=True
        )

        embed.add_field(
            name="⚖️ Hòa",
            value="Odds `3.20`",
            inline=True
        )

        embed.add_field(
            name="✈️ Đội khách",
            value="Odds `2.00`",
            inline=True
        )

        await interaction.response.send_message(
            embed=embed,
            view=MatchChoiceView(
                self.user_id,
                match
            ),
            ephemeral=True
        )


class MatchSelectView(
    discord.ui.View
):

    def __init__(
        self,
        user_id,
        matches
    ):

        super().__init__(
            timeout=180
        )

        self.add_item(
            MatchSelect(
                user_id,
                matches
            )
        )


# =========================================================
# NÚT MAN CITY RIÊNG
# =========================================================

class CityBetButton(
    discord.ui.Button
):

    def __init__(
        self,
        user_id
    ):

        super().__init__(
            label="Manchester City",
            emoji="🔵",
            style=discord.ButtonStyle.primary,
            row=1
        )

        self.user_id = user_id

    async def callback(
        self,
        interaction
    ):

        if (
            interaction.user.id
            != self.user_id
        ):

            await interaction.response.send_message(
                "❌ Menu này không phải của m.",
                ephemeral=True
            )

            return

        await interaction.response.defer(
            ephemeral=True
        )

        matches = await football.man_city_upcoming()

        if not matches:

            await interaction.followup.send(
                "❌ Hiện API chưa trả về trận sắp tới "
                "của Manchester City.",
                ephemeral=True
            )

            return

        # Discord tối đa 25 lựa chọn
        matches = matches[:25]

        embed = discord.Embed(
            title="🔵 MANCHESTER CITY",
            description=(
                "Các trận **sắp tới của Man City** "
                "để đặt cược:\n\n"
                "⚽ Chọn một trận bên dưới."
            ),
            color=0x3498DB
        )

        await interaction.followup.send(
            embed=embed,
            view=MatchSelectView(
                self.user_id,
                matches
            ),
            ephemeral=True
        )


# =========================================================
# MENU CUOC
# =========================================================

class LeagueSelect(
    discord.ui.Select
):

    def __init__(
        self,
        user_id
    ):

        self.user_id = user_id

        options = [
            discord.SelectOption(
                label="Premier League",
                value="PL",
                emoji="🏴",
                description="Tất cả trận Premier League"
            ),
            discord.SelectOption(
                label="Champions League",
                value="CL",
                emoji="🏆",
                description="Tất cả trận Champions League"
            ),
            discord.SelectOption(
                label="La Liga",
                value="PD",
                emoji="🇪🇸",
                description="Tất cả trận La Liga"
            ),
            discord.SelectOption(
                label="Serie A",
                value="SA",
                emoji="🇮🇹",
                description="Tất cả trận Serie A"
            ),
            discord.SelectOption(
                label="Bundesliga",
                value="BL1",
                emoji="🇩🇪",
                description="Tất cả trận Bundesliga"
            ),
            discord.SelectOption(
                label="Ligue 1",
                value="FL1",
                emoji="🇫🇷",
                description="Tất cả trận Ligue 1"
            ),
            discord.SelectOption(
                label="Eredivisie",
                value="DED",
                emoji="🇳🇱",
                description="Tất cả trận Eredivisie"
            ),
            discord.SelectOption(
                label="Primeira Liga",
                value="PPL",
                emoji="🇵🇹",
                description="Tất cả trận Primeira Liga"
            ),
            discord.SelectOption(
                label="Championship",
                value="ELC",
                emoji="🏴",
                description="Tất cả trận Championship"
            ),
        ]

        super().__init__(
            placeholder="🌍 Chọn giải đấu...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(
        self,
        interaction
    ):

        if (
            interaction.user.id
            != self.user_id
        ):

            await interaction.response.send_message(
                "❌ Menu này không phải của m.",
                ephemeral=True
            )

            return

        code = self.values[0]

        await interaction.response.defer(
            ephemeral=True
        )

        matches = await football.get_competition_matches(
            code,
            2,
            90
        )

        # QUAN TRỌNG:
        # Không dựa vào status SCHEDULED.
        # Chỉ dựa vào thời gian.
        matches = [
            m for m in matches
            if is_future_match(m)
        ]

        matches.sort(
            key=lambda x: x.get(
                "utcDate",
                ""
            )
        )

        if not matches:

            await interaction.followup.send(
                f"❌ Hiện không có trận sắp tới "
                f"trong **{COMPETITIONS[code][1]}** "
                "mà API trả về.",
                ephemeral=True
            )

            return

        matches = matches[:25]

        embed = discord.Embed(
            title=(
                f"{COMPETITIONS[code][0]} "
                f"{COMPETITIONS[code][1]}"
            ),
            description=(
                "⚽ Chọn trận muốn cược:"
            ),
            color=0x3498DB
        )

        await interaction.followup.send(
            embed=embed,
            view=MatchSelectView(
                self.user_id,
                matches
            ),
            ephemeral=True
        )


class CuocView(
    discord.ui.View
):

    def __init__(
        self,
        user_id
    ):

        super().__init__(
            timeout=180
        )

        self.add_item(
            LeagueSelect(
                user_id
            )
        )

        # NÚT MAN CITY RIÊNG
        self.add_item(
            CityBetButton(
                user_id
            )
        )


# =========================================================
# /CUOC
# =========================================================

@bot.tree.command(
    name="cuoc",
    description="Mở trung tâm cá cược bóng đá"
)
async def cuoc(
    interaction: discord.Interaction
):

    embed = discord.Embed(
        title="⚽ TRUNG TÂM CƯỢC",
        description=(
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🌍 **CHỌN GIẢI ĐẤU**\n\n"
            "🏴 Premier League\n"
            "🏆 Champions League\n"
            "🇪🇸 La Liga\n"
            "🇮🇹 Serie A\n"
            "🇩🇪 Bundesliga\n"
            "🇫🇷 Ligue 1\n"
            "🇳🇱 Eredivisie\n"
            "🇵🇹 Primeira Liga\n"
            "🏴 Championship\n\n"
            "🔵 **HOẶC CHỌN MANCHESTER CITY**\n"
            "→ Chỉ hiện các trận sắp tới của City\n"
            "━━━━━━━━━━━━━━━━━━━━"
        ),
        color=0x3498DB
    )

    embed.set_footer(
        text="⚽ Chọn giải → chọn trận → chọn cửa → nhập tiền"
    )

    await interaction.response.send_message(
        embed=embed,
        view=CuocView(
            interaction.user.id
        ),
        ephemeral=True
    )


# =========================================================
# SOI - HIỆN CÁC GIẢI MAN CITY
# =========================================================

class CitySoiView(
    discord.ui.View
):

    def __init__(self):

        super().__init__(
            timeout=180
        )

    @discord.ui.button(
        label="Man City",
        emoji="🔵",
        style=discord.ButtonStyle.primary
    )
    async def city(
        self,
        interaction,
        button
    ):

        await interaction.response.defer()

        matches = await football.man_city_upcoming()

        if not matches:

            await interaction.followup.send(
                "❌ API chưa trả về lịch sắp tới của Man City."
            )

            return

        embed = discord.Embed(
            title="🔵 MANCHESTER CITY",
            description=(
                "Các trận sắp tới của City "
                "ở các giải được API trả về:"
            ),
            color=0x3498DB
        )

        for match in matches[:15]:

            competition = (
                match.get(
                    "competition",
                    {}
                ).get(
                    "name",
                    "Không rõ giải"
                )
            )

            embed.add_field(
                name=match_display_name(
                    match
                ),
                value=(
                    f"🏆 {competition}\n"
                    f"🕐 {format_match_date(match)}"
                ),
                inline=False
            )

        await interaction.followup.send(
            embed=embed
        )

    @discord.ui.button(
        label="Premier League",
        emoji="🏴",
        style=discord.ButtonStyle.secondary
    )
    async def pl(
        self,
        interaction,
        button
    ):

        await self.show_competition(
            interaction,
            "PL"
        )

    @discord.ui.button(
        label="Champions League",
        emoji="🏆",
        style=discord.ButtonStyle.secondary
    )
    async def cl(
        self,
        interaction,
        button
    ):

        await self.show_competition(
            interaction,
            "CL"
        )

    @discord.ui.button(
        label="Tất cả giải City",
        emoji="📋",
        style=discord.ButtonStyle.success
    )
    async def all_city(
        self,
        interaction,
        button
    ):

        await interaction.response.defer()

        matches = await football.man_city_upcoming()

        if not matches:

            await interaction.followup.send(
                "❌ Chưa lấy được lịch City."
            )

            return

        matches.sort(
            key=lambda x: x.get(
                "utcDate",
                ""
            )
        )

        embed = discord.Embed(
            title="📋 MAN CITY — TẤT CẢ GIẢI",
            description=(
                "Tất cả trận sắp tới của Manchester City."
            ),
            color=0x9B59B6
        )

        for match in matches[:20]:

            competition = (
                match.get(
                    "competition",
                    {}
                ).get(
                    "name",
                    "Không rõ giải"
                )
            )

            embed.add_field(
                name=match_display_name(
                    match
                ),
                value=(
                    f"🏆 {competition}\n"
                    f"🕐 {format_match_date(match)}"
                ),
                inline=False
            )

        await interaction.followup.send(
            embed=embed
        )

    async def show_competition(
        self,
        interaction,
        code
    ):

        await interaction.response.defer()

        matches = await football.get_competition_matches(
            code,
            2,
            120
        )

        matches = [
            m for m in matches
            if is_future_match(m)
        ]

        # Chỉ lấy các trận có Man City
        matches = [
            m for m in matches
            if (
                int(
                    m.get(
                        "homeTeam",
                        {}
                    ).get(
                        "id",
                        -1
                    )
                ) == MANCHESTER_CITY_ID
                or
                int(
                    m.get(
                        "awayTeam",
                        {}
                    ).get(
                        "id",
                        -1
                    )
                ) == MANCHESTER_CITY_ID
            )
        ]

        matches.sort(
            key=lambda x: x.get(
                "utcDate",
                ""
            )
        )

        if not matches:

            await interaction.followup.send(
                f"❌ Không lấy được trận Man City "
                f"trong {CITY_COMPETITIONS[code][1]}."
            )

            return

        embed = discord.Embed(
            title=(
                f"{CITY_COMPETITIONS[code][0]} "
                f"MAN CITY — "
                f"{CITY_COMPETITIONS[code][1]}"
            ),
            color=0x3498DB
        )

        for match in matches:

            embed.add_field(
                name=match_display_name(
                    match
                ),
                value=(
                    f"🕐 {format_match_date(match)}"
                ),
                inline=False
            )

        await interaction.followup.send(
            embed=embed
        )


# =========================================================
# /SOI
# =========================================================

@bot.tree.command(
    name="soi",
    description="Soi các giải đấu và lịch Man City"
)
async def soi(
    interaction: discord.Interaction
):

    embed = discord.Embed(
        title="🔮 TRUNG TÂM SOI BÓNG",
        description=(
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🔵 **Manchester City**\n"
            "🏴 **Premier League**\n"
            "🏆 **Champions League**\n"
            "📋 **Tất cả giải của Man City**\n"
            "━━━━━━━━━━━━━━━━━━━━"
        ),
        color=0x9B59B6
    )

    await interaction.response.send_message(
        embed=embed,
        view=CitySoiView()
    )


# =========================================================
# KẾT TOÁN
# =========================================================

async def send_result_dm(
    bet,
    match,
    won,
    payout
):

    try:

        user = bot.get_user(
            bet["user_id"]
        )

        if user is None:

            user = await bot.fetch_user(
                bet["user_id"]
            )

        score = match.get(
            "score",
            {}
        ).get(
            "fullTime",
            {}
        )

        home_score = score.get(
            "home",
            "?"
        )

        away_score = score.get(
            "away",
            "?"
        )

        if won:

            embed = discord.Embed(
                title="🎉 KẾT QUẢ CƯỢC — THẮNG",
                color=0x2ECC71
            )

            embed.description = (
                f"⚽ **{bet['match_name']}**\n"
                f"📊 Tỷ số: **{home_score} - {away_score}**\n\n"
                f"🎯 {choice_name(bet['choice'])}\n"
                f"💰 Cược: `{bet['amount']:,}` xu\n"
                f"📈 Odds: `{bet['odds']:.2f}`\n\n"
                f"🎁 Nhận: **+{payout:,} xu**\n"
                f"💵 Ví: **{get_balance(bet['user_id']):,} xu**"
            )

        else:

            embed = discord.Embed(
                title="😢 KẾT QUẢ CƯỢC — THUA",
                color=0xE74C3C
            )

            embed.description = (
                f"⚽ **{bet['match_name']}**\n"
                f"📊 Tỷ số: **{home_score} - {away_score}**\n\n"
                f"🎯 {choice_name(bet['choice'])}\n"
                f"💸 Mất: `{bet['amount']:,}` xu\n"
                f"💵 Ví: **{get_balance(bet['user_id']):,} xu**"
            )

        await user.send(
            embed=embed
        )

    except Exception as e:

        print(
            "DM result error:",
            e
        )


async def settle_one(
    bet,
    match
):

    result = get_result_choice(
        match
    )

    if result is None:
        return False

    won = (
        result
        == bet["choice"]
    )

    payout = 0

    if won:

        payout = math.floor(
            bet["amount"]
            * bet["odds"]
        )

        change_balance(
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
        "WON" if won else "LOST",
        payout,
        now_vn().isoformat(),
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

    await send_result_dm(
        bet,
        match,
        won,
        payout
    )

    await check_vip(
        bet["user_id"]
    )

    return True


async def settle_pending_bets():

    bets = db.execute("""
        SELECT *
        FROM bets
        WHERE status = 'PENDING'
        ORDER BY id ASC
        LIMIT 50
    """).fetchall()

    checked = {}

    for bet in bets:

        match_id = int(
            bet["match_id"]
        )

        if match_id not in checked:

            checked[match_id] = (
                await football.get_match(
                    match_id
                )
            )

        match = checked[
            match_id
        ]

        if not match:
            continue

        if not is_finished(
            match
        ):
            continue

        await settle_one(
            bet,
            match
        )


# =========================================================
# GET MATCH
# =========================================================

# Thêm hàm này vào class FootballAPI
# nhưng vì code Python đã chạy class ở trên,
# ta gắn method trực tiếp ở đây.

async def football_get_match(
    self,
    match_id
):

    cache_key = (
        "match",
        match_id
    )

    cached = self.cache.get(
        cache_key
    )

    if cached:

        saved_time, data = cached

        if (
            datetime.now().timestamp()
            - saved_time
            < 60
        ):

            return data

    data = await self.request(
        f"/matches/{match_id}"
    )

    if data:

        self.cache[cache_key] = (
            datetime.now().timestamp(),
            data
        )

    return data


FootballAPI.get_match = football_get_match


# =========================================================
# /KETTOAN
# =========================================================

@bot.tree.command(
    name="kettoan",
    description="Kết toán các trận đã đá xong"
)
async def kettoan(
    interaction
):

    await interaction.response.defer(
        ephemeral=True
    )

    before = db.execute("""
        SELECT COUNT(*)
        FROM bets
        WHERE status = 'PENDING'
    """).fetchone()[0]

    await settle_pending_bets()

    after = db.execute("""
        SELECT COUNT(*)
        FROM bets
        WHERE status = 'PENDING'
    """).fetchone()[0]

    await interaction.followup.send(
        f"✅ Đã kiểm tra.\n"
        f"🎟️ Đã xử lý: **{before - after}** vé\n"
        f"⏳ Còn chờ: **{after}** vé",
        ephemeral=True
    )


# =========================================================
# AUTO SETTLEMENT
# =========================================================

@tasks.loop(
    minutes=5
)
async def settlement_loop():

    try:

        await settle_pending_bets()

    except Exception as e:

        print(
            "Settlement loop error:",
            e
        )


# =========================================================
# ALERT
# =========================================================

@tasks.loop(
    minutes=1
)
async def football_alert_loop():

    if not ALERT_CHANNEL_ID:
        return

    channel = bot.get_channel(
        ALERT_CHANNEL_ID
    )

    if channel is None:
        return

    # Chỉ theo dõi PL + CL để không đốt quota
    for code in (
        "PL",
        "CL"
    ):

        try:

            matches = await football.get_competition_matches(
                code,
                0,
                2
            )

            for match in matches:

                if not is_future_match(
                    match
                ):
                    continue

                utc_date = match.get(
                    "utcDate"
                )

                if not utc_date:
                    continue

                try:

                    dt = datetime.fromisoformat(
                        utc_date.replace(
                            "Z",
                            "+00:00"
                        )
                    ).astimezone(
                        TIMEZONE
                    )

                except Exception:

                    continue

                diff = (
                    dt - now_vn()
                ).total_seconds()

                if 0 <= diff <= 600:

                    match_id = int(
                        match["id"]
                    )

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
                        title="🚨 TRẬN SẮP BẮT ĐẦU",
                        description=(
                            f"⚽ **{match_display_name(match)}**\n\n"
                            f"🏆 {COMPETITIONS[code][1]}\n"
                            f"🕐 {format_match_date(match)}\n\n"
                            "🔥 `/cuoc` để đặt kèo!"
                        ),
                        color=0xE67E22
                    )

                    await channel.send(
                        embed=embed
                    )

                    db.execute("""
                        INSERT OR IGNORE INTO sent_alerts
                        (
                            match_id,
                            sent_at
                        )
                        VALUES (?, ?)
                    """, (
                        match_id,
                        now_vn().isoformat()
                    ))

                    db.commit()

        except Exception as e:

            print(
                "Alert error:",
                e
            )


# =========================================================
# PING
# =========================================================

@bot.tree.command(
    name="ping",
    description="Kiểm tra bot"
)
async def ping(
    interaction
):

    await interaction.response.send_message(
        f"🏓 Pong! `{round(bot.latency * 1000)}ms`"
    )


# =========================================================
# API QUOTA
# =========================================================

@bot.tree.command(
    name="apiquota",
    description="Xem quota API"
)
async def apiquota(
    interaction
):

    count = len(
        football.request_times
    )

    await interaction.response.send_message(
        f"⚽ Football API\n"
        f"📡 Request 60 giây gần nhất: `{count}/9`\n"
        f"💾 Cache: `{len(football.cache)}`",
        ephemeral=True
    )


# =========================================================
# READY
# =========================================================

@bot.event
async def on_ready():

    print(
        f"✅ Bot đăng nhập: {bot.user}"
    )

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
            f"✅ Sync {len(synced)} commands"
        )

    except Exception as e:

        print(
            "❌ Sync error:",
            e
        )

    try:

        await football.start()

        test = await football.request(
            "/competitions"
        )

        if test is not None:

            print(
                "✅ Football API OK"
            )

        else:

            print(
                "❌ Football API FAILED"
            )

    except Exception as e:

        print(
            "❌ API test error:",
            e
        )

    if not settlement_loop.is_running():

        settlement_loop.start()

    if not football_alert_loop.is_running():

        football_alert_loop.start()


# =========================================================
# RUN
# =========================================================

if not DISCORD_TOKEN:

    raise RuntimeError(
        "Thiếu DISCORD_TOKEN"
    )

if not FOOTBALL_DATA_TOKEN:

    raise RuntimeError(
        "Thiếu FOOTBALL_DATA_TOKEN"
    )


bot.run(
    DISCORD_TOKEN
)

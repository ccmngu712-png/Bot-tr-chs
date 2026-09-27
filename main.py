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

ALERT_CHANNEL_ID = int(os.getenv("FOOTBALL_ALERT_CHANNEL_ID", "0") or 0)

API_BASE = "https://api.football-data.org/v4"

ADMIN_PASSWORD = "provip👑"

# Các giải dùng trong /cuoc
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
    if level <= 0:
        return "Thường"
    return f"VIP {level}"


# =========================================================
# DATABASE
# =========================================================

DB_FILE = "football_bot.db"

db = sqlite3.connect(DB_FILE, check_same_thread=False)
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

# Migration nếu database cũ chưa có vip
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
        (user_id, balance, total_bets, pending_bets, started, vip)
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
    return get_wallet(user_id)["balance"]


def change_balance(user_id: int, amount: int):
    ensure_wallet(user_id)

    db.execute("""
        UPDATE wallets
        SET balance = balance + ?
        WHERE user_id = ?
    """, (amount, user_id))

    db.commit()


def set_vip(user_id: int, level: int):
    ensure_wallet(user_id)

    db.execute("""
        UPDATE wallets
        SET vip = ?
        WHERE user_id = ?
    """, (level, user_id))

    db.commit()


def get_stored_vip(user_id: int) -> int:
    return int(get_wallet(user_id)["vip"] or 0)


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
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={
                    "X-Auth-Token": FOOTBALL_DATA_TOKEN
                }
            )

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def request(self, endpoint, params=None):

        await self.start()

        # Giữ dưới giới hạn 10 request/phút của free API
        now = asyncio.get_running_loop().time()

        self.request_times = [
            t for t in self.request_times
            if now - t < 60
        ]

        if len(self.request_times) >= 9:
            wait_time = 60 - (now - self.request_times[0]) + 1
            await asyncio.sleep(max(wait_time, 1))

        self.request_times.append(
            asyncio.get_running_loop().time()
        )

        url = API_BASE + endpoint

        try:
            async with self.session.get(
                url,
                params=params,
                timeout=aiohttp.ClientTimeout(total=20)
            ) as response:

                if response.status == 200:
                    return await response.json()

                if response.status == 429:
                    await asyncio.sleep(10)
                    return None

                print(
                    f"Football API lỗi {response.status}: {endpoint}"
                )

                return None

        except Exception as e:
            print("Football API exception:", e)
            return None

    async def get_competition_matches(
        self,
        competition_code: str,
        days_before=1,
        days_after=30
    ):

        cache_key = (
            "competition",
            competition_code
        )

        cached = self.cache.get(cache_key)

        if cached:
            saved_time, data = cached

            if datetime.now().timestamp() - saved_time < 300:
                return data

        today = datetime.now(TIMEZONE).date()

        date_from = today - timedelta(days=days_before)
        date_to = today + timedelta(days=days_after)

        data = await self.request(
            f"/competitions/{competition_code}/matches",
            {
                "dateFrom": date_from.isoformat(),
                "dateTo": date_to.isoformat()
            }
        )

        if not data:
            return []

        matches = data.get("matches", [])

        self.cache[cache_key] = (
            datetime.now().timestamp(),
            matches
        )

        return matches

    async def get_match(self, match_id: int):

        cache_key = ("match", match_id)

        cached = self.cache.get(cache_key)

        if cached:
            saved_time, data = cached

            if datetime.now().timestamp() - saved_time < 60:
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

    async def man_city(self):

        matches = await self.get_competition_matches(
            "PL",
            2,
            30
        )

        result = []

        for match in matches:
            home = match.get("homeTeam", {}).get("name", "")
            away = match.get("awayTeam", {}).get("name", "")

            if "Manchester City" in home or "Manchester City" in away:
                result.append(match)

        return result

    async def champions_league(self):

        return await self.get_competition_matches(
            "CL",
            2,
            30
        )


football = FootballAPI()


# =========================================================
# HELPERS
# =========================================================

def now_vn():
    return datetime.now(TIMEZONE)


def match_status_finished(match):
    status = match.get("status", "")
    return status in {
        "FINISHED",
        "AWARDED"
    }


def format_match_date(match):

    utc_date = match.get("utcDate")

    if not utc_date:
        return "Không rõ giờ"

    try:
        dt = datetime.fromisoformat(
            utc_date.replace("Z", "+00:00")
        )

        local_dt = dt.astimezone(TIMEZONE)

        return local_dt.strftime(
            "%d/%m %H:%M"
        )

    except Exception:
        return "Không rõ giờ"


def match_display_name(match):

    home = match.get(
        "homeTeam",
        {}
    ).get("name", "???")

    away = match.get(
        "awayTeam",
        {}
    ).get("name", "???")

    return f"{home} vs {away}"


def get_result_choice(match):

    score = match.get("score", {})

    full_time = score.get("fullTime", {})

    home_score = full_time.get("home")
    away_score = full_time.get("away")

    if home_score is None or away_score is None:
        return None

    if home_score > away_score:
        return "HOME"

    if home_score < away_score:
        return "AWAY"

    return "DRAW"


def choice_name(choice):

    if choice == "HOME":
        return "🏠 Đội nhà"

    if choice == "DRAW":
        return "⚖️ Hòa"

    if choice == "AWAY":
        return "✈️ Đội khách"

    return choice


def odds_for_choice(choice):

    # Odds nhà cái của bot
    if choice == "HOME":
        return 2.00

    if choice == "DRAW":
        return 3.20

    if choice == "AWAY":
        return 2.00

    return 2.00


# =========================================================
# VIP NOTIFICATION
# =========================================================

async def send_vip_notification(
    user_id: int,
    new_level: int,
    balance: int,
    channel=None
):

    if new_level <= 0:
        return

    try:
        user = bot.get_user(user_id)

        if user is None:
            user = await bot.fetch_user(user_id)

        if channel is None and ALERT_CHANNEL_ID:
            channel = bot.get_channel(ALERT_CHANNEL_ID)

        if channel is None:
            return

        emoji = VIP_EMOJIS.get(
            new_level,
            "👑"
        )

        embed = discord.Embed(
            title=f"{emoji} CHÚC MỪNG {vip_name(new_level)} {emoji}",
            description=(
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"🎉 **{user.display_name}** đã thăng cấp!\n\n"
                f"👤 **Tài khoản:** {user.mention}\n"
                f"💰 **Số dư:** `{balance:,}` xu\n"
                f"🔥 **Cấp hiện tại:** **VIP {new_level}**\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"{emoji} Chúc mừng bạn đã đạt **VIP {new_level}**!"
            ),
            color=VIP_COLORS.get(
                new_level,
                0xF1C40F
            )
        )

        embed.set_footer(
            text="⚽ Football Betting • Hệ thống VIP"
        )

        await channel.send(
            embed=embed
        )

    except Exception as e:
        print("VIP notification error:", e)


async def check_vip(
    user_id: int,
    announce_channel=None
):

    wallet = get_wallet(user_id)

    balance = int(wallet["balance"])

    old_level = int(wallet["vip"] or 0)

    new_level = get_vip_level(balance)

    # Cập nhật VIP hiện tại
    if new_level != old_level:
        set_vip(
            user_id,
            new_level
        )

    # Chỉ thông báo khi THĂNG cấp
    if new_level > old_level:

        await send_vip_notification(
            user_id,
            new_level,
            balance,
            announce_channel
        )

    return new_level


# =========================================================
# DAILY / STARTER
# =========================================================

@bot.tree.command(
    name="comat",
    description="Nhận 50.000 xu mỗi ngày"
)
async def comat(interaction: discord.Interaction):

    user_id = interaction.user.id

    ensure_wallet(user_id)

    today = now_vn().strftime("%Y-%m-%d")

    existing = db.execute("""
        SELECT 1
        FROM daily_claims
        WHERE user_id = ?
        AND claim_date = ?
    """, (user_id, today)).fetchone()

    if existing:

        await interaction.response.send_message(
            "❌ Hôm nay m nhận cơm rồi, mai quay lại nhé.",
            ephemeral=True
        )

        return

    db.execute("""
        INSERT INTO daily_claims
        (user_id, claim_date)
        VALUES (?, ?)
    """, (user_id, today))

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
        f"💵 Số dư: `{get_balance(user_id):,}` xu\n"
        f"👑 VIP: **{vip_name(vip)}**"
    )


@bot.tree.command(
    name="khoinghiep",
    description="Nhận 1.000 xu khởi nghiệp"
)
async def khoinghiep(interaction: discord.Interaction):

    user_id = interaction.user.id

    ensure_wallet(user_id)

    wallet = get_wallet(user_id)

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
    """, (user_id,))

    db.commit()

    vip = await check_vip(
        user_id,
        interaction.channel
    )

    await interaction.response.send_message(
        f"🚀 **KHỞI NGHIỆP THÀNH CÔNG!**\n\n"
        f"💰 +1,000 xu\n"
        f"💵 Số dư: `{get_balance(user_id):,}` xu\n"
        f"👑 VIP: **{vip_name(vip)}**"
    )


# =========================================================
# WALLET
# =========================================================

@bot.tree.command(
    name="vi",
    description="Xem ví và cấp VIP"
)
async def vi(interaction: discord.Interaction):

    user_id = interaction.user.id

    wallet = get_wallet(user_id)

    balance = wallet["balance"]

    vip = get_vip_level(balance)

    # Đồng bộ VIP
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
        name=f"{emoji} Cấp VIP",
        value=f"**{vip_name(vip)}**",
        inline=True
    )

    embed.add_field(
        name="🎯 Tổng lượt cược",
        value=str(wallet["total_bets"]),
        inline=True
    )

    embed.add_field(
        name="⏳ Cược đang chờ",
        value=str(wallet["pending_bets"]),
        inline=True
    )

    # Hiện mốc VIP tiếp theo
    next_level = None

    for level, minimum in reversed(VIP_LEVELS):
        if balance < minimum:
            next_level = (
                level,
                minimum
            )

    if next_level:
        level, minimum = next_level

        embed.add_field(
            name="📈 VIP tiếp theo",
            value=(
                f"**VIP {level}**\n"
                f"Cần `{minimum - balance:,}` xu nữa"
            ),
            inline=False
        )
    else:
        embed.add_field(
            name="🔥 MAX VIP",
            value="**VIP 10** — M đã đạt cấp cao nhất!",
            inline=False
        )

    await interaction.response.send_message(
        embed=embed
    )


# =========================================================
# ADMIN
# =========================================================

class AdminModal(discord.ui.Modal, title="👑 ADMIN - QUẢN LÝ XU"):

    password = discord.ui.TextInput(
        label="Mật khẩu admin",
        placeholder="Nhập mật khẩu...",
        required=True
    )

    target = discord.ui.TextInput(
        label="Username / Nickname",
        placeholder="Nhập tên người nhận...",
        required=True
    )

    amount = discord.ui.TextInput(
        label="Số xu",
        placeholder="Ví dụ: 50000 hoặc -50000",
        required=True
    )

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        if str(self.password) != ADMIN_PASSWORD:

            await interaction.response.send_message(
                "❌ Sai mật khẩu admin.",
                ephemeral=True
            )

            return

        try:
            amount = int(
                str(self.amount).replace(",", "")
            )

        except ValueError:

            await interaction.response.send_message(
                "❌ Số xu không hợp lệ.",
                ephemeral=True
            )

            return

        target_text = str(self.target).lower()

        member = None

        for m in interaction.guild.members:

            if (
                str(m.name).lower() == target_text
                or str(m.display_name).lower() == target_text
                or str(m).lower() == target_text
            ):
                member = m
                break

        if member is None:

            await interaction.response.send_message(
                "❌ Không tìm thấy người này trong server.",
                ephemeral=True
            )

            return

        ensure_wallet(member.id)

        if amount < 0:

            current = get_balance(member.id)

            if current + amount < 0:

                await interaction.response.send_message(
                    "❌ Người này không đủ xu để trừ.",
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
async def admin(interaction: discord.Interaction):

    if not interaction.guild:

        await interaction.response.send_message(
            "❌ Chỉ dùng trong server.",
            ephemeral=True
        )

        return

    await interaction.response.send_modal(
        AdminModal()
    )


# =========================================================
# BETTING - CHỌN TRẬN
# =========================================================

class MatchChoiceView(discord.ui.View):

    def __init__(
        self,
        user_id: int,
        match: dict
    ):

        super().__init__(
            timeout=180
        )

        self.user_id = user_id
        self.match = match

        home = match.get(
            "homeTeam",
            {}
        ).get("name", "Đội nhà")

        away = match.get(
            "awayTeam",
            {}
        ).get("name", "Đội khách")

        self.home_name = home
        self.away_name = away

    async def interaction_check(
        self,
        interaction: discord.Interaction
    ):

        if interaction.user.id != self.user_id:

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
    async def home_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
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
    async def draw_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
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
    async def away_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await interaction.response.send_modal(
            BetAmountModal(
                self.match,
                "AWAY"
            )
        )


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
        interaction: discord.Interaction
    ):

        user_id = interaction.user.id

        try:

            amount = int(
                str(self.amount)
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

        balance = get_balance(user_id)

        if amount > balance:

            await interaction.response.send_message(
                f"❌ M không đủ xu.\n"
                f"💰 Ví hiện tại: `{balance:,}` xu",
                ephemeral=True
            )

            return

        match_id = int(
            self.match["id"]
        )

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
                "❌ M đã cược trận này rồi.\n"
                "Mỗi người chỉ được 1 vé cược / trận.",
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
        """, (user_id,))

        db.commit()

        vip = await check_vip(
            user_id,
            interaction.channel
        )

        embed = discord.Embed(
            title="🎟️ ĐẶT CƯỢC THÀNH CÔNG",
            color=0x2ECC71
        )

        embed.add_field(
            name="⚽ Trận đấu",
            value=match_name,
            inline=False
        )

        embed.add_field(
            name="🎯 Lựa chọn",
            value=choice_name(
                self.choice
            ),
            inline=True
        )

        embed.add_field(
            name="💰 Tiền cược",
            value=f"`{amount:,}` xu",
            inline=True
        )

        embed.add_field(
            name="📈 Odds",
            value=f"`{odds:.2f}`",
            inline=True
        )

        embed.add_field(
            name="💵 Số dư còn lại",
            value=f"`{get_balance(user_id):,}` xu",
            inline=False
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=False
        )


# =========================================================
# LEAGUE SELECT
# =========================================================

class LeagueSelect(
    discord.ui.Select
):

    def __init__(self, user_id: int):

        self.user_id = user_id

        options = []

        for code, (
            emoji,
            name
        ) in COMPETITIONS.items():

            options.append(
                discord.SelectOption(
                    label=name,
                    value=code,
                    emoji=emoji,
                    description=f"Xem trận {name}"
                )
            )

        super().__init__(
            placeholder="⚽ Chọn giải đấu...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(
        self,
        interaction: discord.Interaction
    ):

        if interaction.user.id != self.user_id:

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
            1,
            30
        )

        matches = [
            m for m in matches
            if not match_status_finished(m)
        ]

        # Sắp xếp theo giờ
        matches.sort(
            key=lambda x: x.get("utcDate", "")
        )

        if not matches:

            await interaction.followup.send(
                "❌ Hiện không tìm thấy trận sắp đá trong giải này.",
                ephemeral=True
            )

            return

        # Discord select tối đa 25 option
        matches = matches[:25]

        view = MatchSelectView(
            self.user_id,
            matches
        )

        embed = discord.Embed(
            title="⚽ CHỌN TRẬN ĐẤU",
            description=(
                f"🏆 **{COMPETITIONS[code][1]}**\n\n"
                "Chọn trận muốn cược bên dưới:"
            ),
            color=0x3498DB
        )

        await interaction.followup.send(
            embed=embed,
            view=view,
            ephemeral=True
        )


class LeagueView(
    discord.ui.View
):

    def __init__(self, user_id):

        super().__init__(
            timeout=180
        )

        self.add_item(
            LeagueSelect(user_id)
        )


# =========================================================
# MATCH SELECT
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

            match_id = str(
                match["id"]
            )

            name = match_display_name(
                match
            )

            date = format_match_date(
                match
            )

            options.append(
                discord.SelectOption(
                    label=name[:100],
                    value=match_id,
                    description=date[:100]
                )
            )

        super().__init__(
            placeholder="⚽ Chọn trận muốn cược...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(
        self,
        interaction: discord.Interaction
    ):

        if interaction.user.id != self.user_id:

            await interaction.response.send_message(
                "❌ Menu này không phải của m.",
                ephemeral=True
            )

            return

        selected_id = int(
            self.values[0]
        )

        match = next(
            (
                m for m in self.matches
                if int(m["id"]) == selected_id
            ),
            None
        )

        if match is None:

            await interaction.response.send_message(
                "❌ Không tìm thấy trận.",
                ephemeral=True
            )

            return

        embed = discord.Embed(
            title="🎯 CHỌN KÈO",
            description=(
                f"⚽ **{match_display_name(match)}**\n\n"
                f"🕐 {format_match_date(match)}\n\n"
                "Chọn cửa muốn cược:"
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
# /CUOC
# =========================================================

@bot.tree.command(
    name="cuoc",
    description="Mở trung tâm cá cược bóng đá"
)
async def cuoc(interaction: discord.Interaction):

    embed = discord.Embed(
        title="⚽ TRUNG TÂM CƯỢC",
        description=(
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🎯 **Chọn giải đấu muốn cược**\n\n"
            "🏴 Premier League\n"
            "🏆 Champions League\n"
            "🇪🇸 La Liga\n"
            "🇮🇹 Serie A\n"
            "🇩🇪 Bundesliga\n"
            "🇫🇷 Ligue 1\n"
            "🇳🇱 Eredivisie\n"
            "🇵🇹 Primeira Liga\n"
            "🏴 Championship\n"
            "━━━━━━━━━━━━━━━━━━━━"
        ),
        color=0x3498DB
    )

    embed.set_footer(
        text="⚠️ Mỗi người chỉ được 1 vé cược cho mỗi trận."
    )

    await interaction.response.send_message(
        embed=embed,
        view=LeagueView(
            interaction.user.id
        ),
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
        label="Champions League",
        emoji="🏆",
        style=discord.ButtonStyle.primary
    )
    async def cl(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await interaction.response.defer()

        matches = await football.champions_league()

        upcoming = [
            m for m in matches
            if not match_status_finished(m)
        ]

        upcoming.sort(
            key=lambda x: x.get("utcDate", "")
        )

        upcoming = upcoming[:10]

        if not upcoming:

            await interaction.followup.send(
                "❌ Không có trận Champions League sắp tới."
            )

            return

        embed = discord.Embed(
            title="🏆 SOI CHAMPIONS LEAGUE",
            color=0x9B59B6
        )

        for match in upcoming:

            embed.add_field(
                name=match_display_name(match),
                value=(
                    f"🕐 {format_match_date(match)}\n"
                    "📊 Kèo tham khảo: 1X2"
                ),
                inline=False
            )

        await interaction.followup.send(
            embed=embed
        )

    @discord.ui.button(
        label="Manchester City",
        emoji="🔵",
        style=discord.ButtonStyle.success
    )
    async def city(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await interaction.response.defer()

        matches = await football.man_city()

        upcoming = [
            m for m in matches
            if not match_status_finished(m)
        ]

        upcoming.sort(
            key=lambda x: x.get("utcDate", "")
        )

        upcoming = upcoming[:10]

        if not upcoming:

            await interaction.followup.send(
                "❌ Không có trận Man City sắp tới."
            )

            return

        embed = discord.Embed(
            title="🔵 SOI MANCHESTER CITY",
            color=0x3498DB
        )

        for match in upcoming:

            embed.add_field(
                name=match_display_name(match),
                value=(
                    f"🕐 {format_match_date(match)}\n"
                    "📊 Kèo tham khảo: 1X2"
                ),
                inline=False
            )

        await interaction.followup.send(
            embed=embed
        )


@bot.tree.command(
    name="soi",
    description="Xem lịch và soi bóng đá"
)
async def soi(interaction: discord.Interaction):

    embed = discord.Embed(
        title="🔮 TRUNG TÂM SOI BÓNG",
        description=(
            "Chọn khu vực muốn xem:"
        ),
        color=0x9B59B6
    )

    await interaction.response.send_message(
        embed=embed,
        view=SoiView()
    )


# =========================================================
# KẾT TOÁN 1 BET
# =========================================================

async def send_result_dm(
    bet,
    match,
    won: bool,
    payout: int
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
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"⚽ **{bet['match_name']}**\n"
                f"📊 Tỷ số: **{home_score} - {away_score}**\n\n"
                f"🎯 Cửa cược: **{choice_name(bet['choice'])}**\n"
                f"💰 Tiền cược: `{bet['amount']:,}` xu\n"
                f"📈 Odds: `{bet['odds']:.2f}`\n\n"
                f"🎁 **Tiền nhận: +{payout:,} xu**\n"
                f"💵 Số dư mới: `{get_balance(bet['user_id']):,}` xu\n"
                "━━━━━━━━━━━━━━━━━━━━"
            )

        else:

            embed = discord.Embed(
                title="😢 KẾT QUẢ CƯỢC — THUA",
                color=0xE74C3C
            )

            embed.description = (
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"⚽ **{bet['match_name']}**\n"
                f"📊 Tỷ số: **{home_score} - {away_score}**\n\n"
                f"🎯 Cửa cược: **{choice_name(bet['choice'])}**\n"
                f"💸 Tiền mất: `-{bet['amount']:,}` xu\n"
                f"💵 Số dư mới: `{get_balance(bet['user_id']):,}` xu\n"
                "━━━━━━━━━━━━━━━━━━━━"
            )

        embed.set_footer(
            text="⚽ Football Betting • Kết quả tự động"
        )

        await user.send(
            embed=embed
        )

    except Exception as e:

        print(
            "Không gửi được DM kết quả:",
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
        result == bet["choice"]
    )

    payout = 0

    if won:

        payout = math.floor(
            bet["amount"] * bet["odds"]
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

    # Nếu thắng đủ tiền thì kiểm tra VIP
    await check_vip(
        bet["user_id"]
    )

    return True


# =========================================================
# AUTO SETTLEMENT
# =========================================================

async def settle_pending_bets():

    bets = db.execute("""
        SELECT *
        FROM bets
        WHERE status = 'PENDING'
        ORDER BY id ASC
        LIMIT 50
    """).fetchall()

    if not bets:
        return

    checked_matches = {}

    for bet in bets:

        match_id = int(
            bet["match_id"]
        )

        if match_id not in checked_matches:

            checked_matches[match_id] = (
                await football.get_match(
                    match_id
                )
            )

        match = checked_matches[
            match_id
        ]

        if not match:
            continue

        if not match_status_finished(
            match
        ):
            continue

        await settle_one(
            bet,
            match
        )

        await asyncio.sleep(0.5)


# =========================================================
# /KETTOAN
# =========================================================

@bot.tree.command(
    name="kettoan",
    description="Kiểm tra và kết toán các vé đã đá xong"
)
async def kettoan(
    interaction: discord.Interaction
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

    settled = before - after

    await interaction.followup.send(
        f"✅ Đã kiểm tra kết toán.\n"
        f"🎟️ Vé đã xử lý: **{settled}**\n"
        f"⏳ Vé còn chờ: **{after}**",
        ephemeral=True
    )


# =========================================================
# AUTO LOOPS
# =========================================================

@tasks.loop(minutes=5)
async def settlement_loop():

    try:

        await settle_pending_bets()

    except Exception as e:

        print(
            "Settlement loop error:",
            e
        )


@tasks.loop(minutes=1)
async def football_alert_loop():

    if not ALERT_CHANNEL_ID:
        return

    channel = bot.get_channel(
        ALERT_CHANNEL_ID
    )

    if channel is None:
        return

    try:

        # Chỉ báo các trận sắp diễn ra trong các giải đã cấu hình
        # để tránh spam API.
        for code in list(
            COMPETITIONS.keys()
        ):

            matches = await football.get_competition_matches(
                code,
                0,
                1
            )

            for match in matches:

                if match_status_finished(
                    match
                ):
                    continue

                match_id = int(
                    match["id"]
                )

                utc_date = match.get(
                    "utcDate"
                )

                if not utc_date:
                    continue

                try:

                    match_time = datetime.fromisoformat(
                        utc_date.replace(
                            "Z",
                            "+00:00"
                        )
                    )

                    match_time = match_time.astimezone(
                        TIMEZONE
                    )

                except Exception:
                    continue

                diff = (
                    match_time - now_vn()
                ).total_seconds()

                # Báo trước 10 phút
                if 0 <= diff <= 600:

                    existing = db.execute("""
                        SELECT 1
                        FROM sent_alerts
                        WHERE match_id = ?
                    """, (
                        match_id,
                    )).fetchone()

                    if existing:
                        continue

                    embed = discord.Embed(
                        title="🚨 TRẬN SẮP BẮT ĐẦU",
                        description=(
                            f"⚽ **{match_display_name(match)}**\n\n"
                            f"🕐 **{format_match_date(match)}**\n"
                            f"🏆 **{COMPETITIONS[code][1]}**\n\n"
                            "🔥 Chuẩn bị vào `/cuoc` để đặt kèo!"
                        ),
                        color=0xE67E22
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
                        now_vn().isoformat()
                    ))

                    db.commit()

            await asyncio.sleep(0.3)

    except Exception as e:

        print(
            "Alert loop error:",
            e
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
    description="Xem trạng thái API bóng đá"
)
async def apiquota(
    interaction: discord.Interaction
):

    count = len(
        football.request_times
    )

    await interaction.response.send_message(
        f"⚽ Football API\n"
        f"📡 Request trong 60 giây gần nhất: `{count}/9`\n"
        f"💾 Cache: `{len(football.cache)}` mục",
        ephemeral=True
    )


# =========================================================
# READY
# =========================================================

@bot.event
async def on_ready():

    print(
        f"Đã đăng nhập: {bot.user}"
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
            f"Đã sync {len(synced)} slash commands."
        )

    except Exception as e:

        print(
            "Sync command error:",
            e
        )

    try:

        await football.start()

        test = await football.request(
            "/competitions"
        )

        if test is not None:
            print(
                "Football API: OK"
            )
        else:
            print(
                "Football API: FAILED"
            )

    except Exception as e:

        print(
            "API test error:",
            e
        )

    if not settlement_loop.is_running():
        settlement_loop.start()

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

    db.close()


# =========================================================
# RUN
# =========================================================

if not DISCORD_TOKEN:
    raise RuntimeError(
        "Thiếu DISCORD_TOKEN trong Railway Variables."
    )

if not FOOTBALL_DATA_TOKEN:
    raise RuntimeError(
        "Thiếu FOOTBALL_DATA_TOKEN trong Railway Variables."
    )


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

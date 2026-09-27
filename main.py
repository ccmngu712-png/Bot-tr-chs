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

GUILD_ID = int(os.getenv("GUILD_ID", "1551547049600618538"))
ALERT_CHANNEL_ID = int(os.getenv("FOOTBALL_ALERT_CHANNEL_ID", "0"))

TIMEZONE = ZoneInfo("Asia/Ho_Chi_Minh")

API_BASE = "https://api.football-data.org/v4"

MANCHESTER_CITY_ID = 65
CURRENT_SEASON = 2026

ADMIN_PASSWORD = "provip👑"

# Những giải được hỗ trợ để /cuoc.
# /soi sẽ KHÔNG dùng danh sách này để hiện bừa tất cả.
# /soi lấy trực tiếp các competition mà Man City thực sự có trận.
COMPETITIONS = {
    "PL": ("🏴", "Premier League"),
    "CL": ("🏆", "Champions League"),
    "FA": ("🏆", "FA Cup"),
    "ELC": ("🏴", "Championship"),
    "PD": ("🇪🇸", "La Liga"),
    "SA": ("🇮🇹", "Serie A"),
    "BL1": ("🇩🇪", "Bundesliga"),
    "FL1": ("🇫🇷", "Ligue 1"),
    "DED": ("🇳🇱", "Eredivisie"),
    "PPL": ("🇵🇹", "Primeira Liga"),
}


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

db = sqlite3.connect("football_bot.db", check_same_thread=False)
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

# Migration cho database cũ
try:
    db.execute(
        "ALTER TABLE wallets ADD COLUMN vip INTEGER NOT NULL DEFAULT 0"
    )
    db.commit()
except sqlite3.OperationalError:
    pass

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


def get_vip_level(balance: int) -> int:
    for level, minimum in VIP_LEVELS:
        if balance >= minimum:
            return level
    return 0


def vip_name(level: int) -> str:
    return f"VIP {level}" if level > 0 else "Thành viên"


def vip_emoji(level: int) -> str:
    if level >= 10:
        return "👑"
    if level >= 7:
        return "💎"
    if level >= 4:
        return "🔥"
    if level >= 1:
        return "⭐"
    return "👤"


async def announce_vip(user_id: int, new_level: int, balance: int, channel=None):
    if new_level <= 0:
        return

    try:
        user = bot.get_user(user_id)

        if user is None:
            user = await bot.fetch_user(user_id)

        username = user.display_name if user else str(user_id)

        if channel is None:
            if ALERT_CHANNEL_ID:
                channel = bot.get_channel(ALERT_CHANNEL_ID)

        if channel is None:
            return

        embed = discord.Embed(
            title=f"🎉 CHÚC MỪNG {vip_name(new_level).upper()} 🎉",
            description=(
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"{vip_emoji(new_level)} **{username}** đã đạt cấp "
                f"**{vip_name(new_level)}**!\n\n"
                f"💰 Số dư: **{balance:,} xu**\n"
                f"🔥 Cấp hiện tại: **{vip_name(new_level)}**\n"
                "━━━━━━━━━━━━━━━━━━━━"
            ),
            color=discord.Color.gold()
        )

        await channel.send(embed=embed)

    except Exception as e:
        print("VIP announce error:", e)


async def check_vip_promotion(user_id: int, channel=None):
    row = db.execute(
        "SELECT balance, vip FROM wallets WHERE user_id=?",
        (user_id,)
    ).fetchone()

    if not row:
        return

    balance = row["balance"]
    old_vip = row["vip"]
    new_vip = get_vip_level(balance)

    if new_vip > old_vip:
        db.execute(
            "UPDATE wallets SET vip=? WHERE user_id=?",
            (new_vip, user_id)
        )
        db.commit()

        await announce_vip(
            user_id,
            new_vip,
            balance,
            channel
        )

    elif new_vip != old_vip:
        # Nếu số dư tụt VIP thì cập nhật cấp hiện tại,
        # nhưng không thông báo.
        db.execute(
            "UPDATE wallets SET vip=? WHERE user_id=?",
            (new_vip, user_id)
        )
        db.commit()


def ensure_wallet(user_id: int):
    db.execute("""
        INSERT OR IGNORE INTO wallets
        (user_id, balance, total_bets, pending_bets, started, vip)
        VALUES (?, 0, 0, 0, 0, 0)
    """, (user_id,))
    db.commit()


def get_balance(user_id: int) -> int:
    ensure_wallet(user_id)

    row = db.execute(
        "SELECT balance FROM wallets WHERE user_id=?",
        (user_id,)
    ).fetchone()

    return row["balance"]


async def add_money(user_id: int, amount: int, channel=None):
    ensure_wallet(user_id)

    db.execute(
        "UPDATE wallets SET balance=balance+? WHERE user_id=?",
        (amount, user_id)
    )
    db.commit()

    await check_vip_promotion(user_id, channel)


async def remove_money(user_id: int, amount: int):
    ensure_wallet(user_id)

    db.execute(
        """
        UPDATE wallets
        SET balance=MAX(balance-?, 0)
        WHERE user_id=?
        """,
        (amount, user_id)
    )
    db.commit()

    await check_vip_promotion(user_id)


# =========================================================
# FOOTBALL API
# =========================================================

class FootballAPI:

    def __init__(self):
        self.session = None
        self.cache = {}
        self.request_times = []

    async def init(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={
                    "X-Auth-Token": FOOTBALL_DATA_TOKEN or ""
                }
            )

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def rate_limit(self):
        now = datetime.now(timezone.utc).timestamp()

        self.request_times = [
            x for x in self.request_times
            if now - x < 60
        ]

        # Free plan an toàn dưới 10 requests/minute
        if len(self.request_times) >= 9:
            wait = 60 - (now - self.request_times[0])
            if wait > 0:
                await asyncio.sleep(wait)

            now = datetime.now(timezone.utc).timestamp()

            self.request_times = [
                x for x in self.request_times
                if now - x < 60
            ]

        self.request_times.append(
            datetime.now(timezone.utc).timestamp()
        )

    async def request(self, endpoint, params=None, cache_seconds=300):
        await self.init()

        key = (
            endpoint,
            tuple(sorted((params or {}).items()))
        )

        now = datetime.now(timezone.utc).timestamp()

        cached = self.cache.get(key)

        if cached:
            saved_time, data = cached

            if now - saved_time < cache_seconds:
                return data

        await self.rate_limit()

        url = API_BASE + endpoint

        try:
            async with self.session.get(
                url,
                params=params or {},
                timeout=aiohttp.ClientTimeout(total=20)
            ) as response:

                if response.status == 429:
                    print("Football API rate limited.")
                    return None

                if response.status != 200:
                    text = await response.text()
                    print(
                        "API ERROR:",
                        response.status,
                        text[:300]
                    )
                    return None

                data = await response.json()

                self.cache[key] = (now, data)

                return data

        except Exception as e:
            print("API request error:", e)
            return None

    async def test(self):
        data = await self.request(
            "/competitions",
            cache_seconds=60
        )

        return data is not None

    async def competition_matches(
        self,
        competition_code,
        season=CURRENT_SEASON,
        days_before=7,
        days_after=180
    ):
        today = datetime.now(TIMEZONE).date()

        date_from = today - timedelta(days=days_before)
        date_to = today + timedelta(days=days_after)

        params = {
            "season": season,
            "dateFrom": date_from.isoformat(),
            "dateTo": date_to.isoformat(),
            "limit": 100
        }

        data = await self.request(
            f"/competitions/{competition_code}/matches",
            params=params,
            cache_seconds=300
        )

        if not data:
            return []

        return data.get("matches", [])

    async def man_city_matches(self, season=CURRENT_SEASON):
        today = datetime.now(TIMEZONE).date()

        params = {
            "season": season,
            "dateFrom": (
                today - timedelta(days=30)
            ).isoformat(),
            "dateTo": (
                today + timedelta(days=365)
            ).isoformat(),
            "limit": 100
        }

        data = await self.request(
            f"/teams/{MANCHESTER_CITY_ID}/matches",
            params=params,
            cache_seconds=300
        )

        if not data:
            return []

        return data.get("matches", [])

    async def team_finished(
        self,
        team_id,
        days=180,
        limit=10
    ):
        today = datetime.now(TIMEZONE).date()

        params = {
            "dateFrom": (
                today - timedelta(days=days)
            ).isoformat(),
            "dateTo": today.isoformat(),
            "status": "FINISHED",
            "limit": limit
        }

        data = await self.request(
            f"/teams/{team_id}/matches",
            params=params,
            cache_seconds=1800
        )

        if not data:
            return []

        matches = data.get("matches", [])

        matches = [
            m for m in matches
            if m.get("status") == "FINISHED"
        ]

        matches.sort(
            key=lambda x: x.get("utcDate", ""),
            reverse=True
        )

        return matches[:limit]


football = FootballAPI()


# =========================================================
# MATCH HELPERS
# =========================================================

def parse_dt(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
        ).astimezone(TIMEZONE)
    except Exception:
        return None


def format_match_date(match):
    dt = parse_dt(match.get("utcDate"))

    if not dt:
        return "Chưa rõ thời gian"

    return dt.strftime("%d/%m/%Y %H:%M")


def match_display_name(match):
    home = match.get("homeTeam", {}).get(
        "shortName"
    ) or match.get("homeTeam", {}).get(
        "name", "Home"
    )

    away = match.get("awayTeam", {}).get(
        "shortName"
    ) or match.get("awayTeam", {}).get(
        "name", "Away"
    )

    return f"{home} vs {away}"


def competition_info(match):
    comp = match.get("competition") or {}

    code = comp.get("code")
    name = comp.get("name", "Không rõ giải")

    if code in COMPETITIONS:
        emoji = COMPETITIONS[code][0]
        name = COMPETITIONS[code][1]
    else:
        emoji = "🏆"

    return code, emoji, name


def is_future_match(match):
    dt = parse_dt(match.get("utcDate"))

    if not dt:
        return False

    return dt > datetime.now(TIMEZONE)


def score_value(match, side):
    score = match.get("score") or {}
    full = score.get("fullTime") or {}

    value = full.get(side)

    if isinstance(value, int):
        return value

    return None


# =========================================================
# PREDICTION
# =========================================================

async def get_team_stats(team_id):
    matches = await football.team_finished(
        team_id,
        days=180,
        limit=8
    )

    if not matches:
        return {
            "gf": 1.40,
            "ga": 1.20
        }

    gf = 0
    ga = 0
    count = 0

    for m in matches:
        home_id = m.get("homeTeam", {}).get("id")
        away_id = m.get("awayTeam", {}).get("id")

        hs = score_value(m, "home")
        aws = score_value(m, "away")

        if hs is None or aws is None:
            continue

        if home_id == team_id:
            gf += hs
            ga += aws
            count += 1

        elif away_id == team_id:
            gf += aws
            ga += hs
            count += 1

    if count == 0:
        return {
            "gf": 1.40,
            "ga": 1.20
        }

    return {
        "gf": gf / count,
        "ga": ga / count
    }


def poisson_probability(lam, k):
    if lam <= 0:
        return 0

    return (
        math.exp(-lam)
        * (lam ** k)
        / math.factorial(k)
    )


async def predict_match(match):
    home = match.get("homeTeam") or {}
    away = match.get("awayTeam") or {}

    home_id = home.get("id")
    away_id = away.get("id")

    if not home_id or not away_id:
        return None

    home_stats, away_stats = await asyncio.gather(
        get_team_stats(home_id),
        get_team_stats(away_id)
    )

    expected_home = (
        home_stats["gf"] * 0.60
        + away_stats["ga"] * 0.40
    )

    expected_away = (
        away_stats["gf"] * 0.60
        + home_stats["ga"] * 0.40
    )

    # Lợi thế sân nhà nhẹ
    expected_home *= 1.08

    expected_home = max(
        0.15,
        min(expected_home, 4.5)
    )

    expected_away = max(
        0.15,
        min(expected_away, 4.5)
    )

    best_score = None
    best_probability = -1

    home_win = 0
    draw = 0
    away_win = 0

    for h in range(0, 7):
        for a in range(0, 7):

            p = (
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

            if p > best_probability:
                best_probability = p
                best_score = (h, a)

            if h > a:
                home_win += p
            elif h == a:
                draw += p
            else:
                away_win += p

    return {
        "home_goals": best_score[0],
        "away_goals": best_score[1],
        "home_probability": home_win,
        "draw_probability": draw,
        "away_probability": away_win,
        "expected_home": expected_home,
        "expected_away": expected_away
    }


def result_text(prediction):
    h = prediction["home_probability"]
    d = prediction["draw_probability"]
    a = prediction["away_probability"]

    if h >= d and h >= a:
        return "🏠 Đội nhà thắng"

    if d >= h and d >= a:
        return "🤝 Hòa"

    return "✈️ Đội khách thắng"


# =========================================================
# UI - SOI
# =========================================================

class SoiLeagueSelect(discord.ui.Select):

    def __init__(self, leagues, city_matches):
        options = []

        for code, name, emoji, count in leagues[:25]:
            options.append(
                discord.SelectOption(
                    label=name[:100],
                    value=code,
                    emoji=emoji,
                    description=f"{count} trận Man City"
                )
            )

        if not options:
            options = [
                discord.SelectOption(
                    label="Không có giải",
                    value="none"
                )
            ]

        super().__init__(
            placeholder="🏆 Chọn giải Man City...",
            options=options
        )

        self.city_matches = city_matches

    async def callback(self, interaction):
        code = self.values[0]

        if code == "none":
            await interaction.response.send_message(
                "❌ Chưa tìm thấy giải của Man City.",
                ephemeral=True
            )
            return

        matches = [
            m for m in self.city_matches
            if (m.get("competition") or {}).get("code") == code
        ]

        matches = [
            m for m in matches
            if is_future_match(m)
        ]

        matches.sort(
            key=lambda x: x.get("utcDate", "")
        )

        await show_soi_match_select(
            interaction,
            matches,
            title="🏆 Trận Man City trong giải"
        )


class SoiCityMatchSelect(discord.ui.Select):

    def __init__(self, matches, placeholder):
        options = []

        for m in matches[:25]:
            match_id = str(m.get("id"))

            code, emoji, comp_name = competition_info(m)

            options.append(
                discord.SelectOption(
                    label=match_display_name(m)[:100],
                    value=match_id,
                    emoji=emoji,
                    description=(
                        f"{comp_name[:45]} • "
                        f"{format_match_date(m)}"
                    )[:100]
                )
            )

        if not options:
            options = [
                discord.SelectOption(
                    label="Không có trận sắp tới",
                    value="none"
                )
            ]

        super().__init__(
            placeholder=placeholder,
            options=options
        )

        self.matches = matches

    async def callback(self, interaction):
        if self.values[0] == "none":
            await interaction.response.send_message(
                "❌ Hiện chưa có trận sắp tới.",
                ephemeral=True
            )
            return

        match_id = int(self.values[0])

        match = next(
            (
                m for m in self.matches
                if m.get("id") == match_id
            ),
            None
        )

        if not match:
            await interaction.response.send_message(
                "❌ Không tìm thấy trận.",
                ephemeral=True
            )
            return

        await interaction.response.defer()

        prediction = await predict_match(match)

        if not prediction:
            await interaction.followup.send(
                "❌ Không đủ dữ liệu để dự đoán.",
                ephemeral=True
            )
            return

        home = match.get("homeTeam", {}).get(
            "name", "Home"
        )

        away = match.get("awayTeam", {}).get(
            "name", "Away"
        )

        code, emoji, comp_name = competition_info(match)

        score = (
            f"**{prediction['home_goals']} - "
            f"{prediction['away_goals']}**"
        )

        embed = discord.Embed(
            title="🔮 SOI TRẬN ĐẤU",
            description=(
                f"⚽ **{home}**\n"
                f"        **VS**\n"
                f"⚽ **{away}**"
            ),
            color=discord.Color.blue()
        )

        embed.add_field(
            name="🏆 Giải đấu",
            value=f"{emoji} {comp_name}",
            inline=True
        )

        embed.add_field(
            name="🕐 Thời gian",
            value=format_match_date(match),
            inline=True
        )

        embed.add_field(
            name="🎯 Dự đoán tỷ số",
            value=f"# {score}",
            inline=False
        )

        embed.add_field(
            name="📊 Xác suất",
            value=(
                f"🏠 Đội nhà: "
                f"**{prediction['home_probability'] * 100:.1f}%**\n"
                f"🤝 Hòa: "
                f"**{prediction['draw_probability'] * 100:.1f}%**\n"
                f"✈️ Đội khách: "
                f"**{prediction['away_probability'] * 100:.1f}%**"
            ),
            inline=False
        )

        embed.add_field(
            name="📌 Kết luận",
            value=result_text(prediction),
            inline=False
        )

        # Kết luận riêng cho Man City
        home_id = match.get("homeTeam", {}).get("id")
        away_id = match.get("awayTeam", {}).get("id")

        if home_id == MANCHESTER_CITY_ID:
            if prediction["home_goals"] > prediction["away_goals"]:
                city_result = "🔵 **Man City thắng**"
            elif prediction["home_goals"] == prediction["away_goals"]:
                city_result = "🔵 **Man City hòa**"
            else:
                city_result = "🔵 **Man City thua**"

            embed.add_field(
                name="🔵 Kết luận Man City",
                value=city_result,
                inline=False
            )

        elif away_id == MANCHESTER_CITY_ID:
            if prediction["away_goals"] > prediction["home_goals"]:
                city_result = "🔵 **Man City thắng**"
            elif prediction["away_goals"] == prediction["home_goals"]:
                city_result = "🔵 **Man City hòa**"
            else:
                city_result = "🔵 **Man City thua**"

            embed.add_field(
                name="🔵 Kết luận Man City",
                value=city_result,
                inline=False
            )

        embed.set_footer(
            text="Data provided by football-data.org"
        )

        await interaction.followup.send(
            embed=embed,
            ephemeral=True
        )


class SoiView(discord.ui.View):

    def __init__(self, city_matches):
        super().__init__(timeout=300)

        future = [
            m for m in city_matches
            if is_future_match(m)
        ]

        future.sort(
            key=lambda x: x.get("utcDate", "")
        )

        # Những giải Man City thực sự góp mặt
        league_map = {}

        for m in city_matches:
            code, emoji, name = competition_info(m)

            if code not in league_map:
                league_map[code] = {
                    "name": name,
                    "emoji": emoji,
                    "count": 0
                }

            league_map[code]["count"] += 1

        leagues = []

        for code, data in league_map.items():
            leagues.append(
                (
                    code,
                    data["name"],
                    data["emoji"],
                    data["count"]
                )
            )

        leagues.sort(
            key=lambda x: x[1]
        )

        self.add_item(
            SoiLeagueSelect(
                leagues,
                city_matches
            )
        )

        self.add_item(
            SoiCityMatchSelect(
                future,
                "🔵 Chọn bất kỳ trận nào của Man City..."
            )
        )


async def show_soi_match_select(
    interaction,
    matches,
    title="⚽ Chọn trận"
):
    view = discord.ui.View(timeout=300)

    select = SoiCityMatchSelect(
        matches,
        "⚽ Chọn trận để soi..."
    )

    view.add_item(select)

    await interaction.response.send_message(
        f"**{title}**",
        view=view,
        ephemeral=True
    )


async def build_soi_embed(city_matches):
    future = [
        m for m in city_matches
        if is_future_match(m)
    ]

    future.sort(
        key=lambda x: x.get("utcDate", "")
    )

    # CỘT TRÁI
    league_map = {}

    for m in city_matches:
        code, emoji, name = competition_info(m)

        if code not in league_map:
            league_map[code] = {
                "emoji": emoji,
                "name": name,
                "count": 0
            }

        league_map[code]["count"] += 1

    left_lines = []

    for code, data in sorted(
        league_map.items(),
        key=lambda x: x[1]["name"]
    ):
        left_lines.append(
            f"{data['emoji']} **{data['name']}**\n"
            f"└ {data['count']} trận"
        )

    if not left_lines:
        left_lines.append(
            "❌ Chưa tìm thấy giải của Man City."
        )

    # CỘT PHẢI
    right_lines = []

    for m in future[:12]:
        code, emoji, name = competition_info(m)

        right_lines.append(
            f"{emoji} **{match_display_name(m)}**\n"
            f"└ {format_match_date(m)}"
        )

    if not right_lines:
        right_lines.append(
            "❌ Chưa có trận sắp tới."
        )

    embed = discord.Embed(
        title="🔮 SOI MAN CITY",
        description=(
            "Chọn **giải** ở cột trái hoặc chọn **trận đấu** "
            "ở cột phải để dự đoán tỷ số."
        ),
        color=discord.Color.blue()
    )

    # Hai field inline = giao diện 2 cột
    embed.add_field(
        name="🏆 CÁC GIẢI MAN CITY",
        value="\n\n".join(left_lines)[:1024],
        inline=True
    )

    embed.add_field(
        name="🔵 MAN CITY — TẤT CẢ TRẬN",
        value="\n\n".join(right_lines)[:1024],
        inline=True
    )

    embed.add_field(
        name="📌 Cách dùng",
        value=(
            "🏆 Chọn giải → xem các trận của Man City trong giải đó.\n"
            "🔵 Chọn trận → bot dự đoán **tỷ số chính xác** "
            "và **Man City thắng/hòa/thua**."
        ),
        inline=False
    )

    embed.set_footer(
        text="Data provided by football-data.org"
    )

    return embed


# =========================================================
# CUOC UI
# =========================================================

class BetLeagueSelect(discord.ui.Select):

    def __init__(self):
        options = []

        for code, (emoji, name) in COMPETITIONS.items():
            options.append(
                discord.SelectOption(
                    label=name,
                    value=code,
                    emoji=emoji
                )
            )

        super().__init__(
            placeholder="🏆 Chọn giải để cược...",
            options=options[:25]
        )

    async def callback(self, interaction):
        code = self.values[0]

        await interaction.response.defer(
            ephemeral=True
        )

        matches = await football.competition_matches(
            code,
            season=CURRENT_SEASON
        )

        matches = [
            m for m in matches
            if is_future_match(m)
        ]

        matches.sort(
            key=lambda x: x.get("utcDate", "")
        )

        if not matches:
            await interaction.followup.send(
                "❌ Hiện chưa có trận sắp tới trong giải này.",
                ephemeral=True
            )
            return

        view = BetMatchView(matches)

        await interaction.followup.send(
            f"🏆 **{COMPETITIONS[code][1]}**\n"
            "Chọn trận muốn cược:",
            view=view,
            ephemeral=True
        )


class BetMatchSelect(discord.ui.Select):

    def __init__(self, matches):
        self.matches = matches

        options = []

        for m in matches[:25]:
            code, emoji, comp_name = competition_info(m)

            options.append(
                discord.SelectOption(
                    label=match_display_name(m)[:100],
                    value=str(m["id"]),
                    emoji=emoji,
                    description=(
                        f"{format_match_date(m)}"
                    )[:100]
                )
            )

        super().__init__(
            placeholder="⚽ Chọn trận...",
            options=options
        )

    async def callback(self, interaction):
        match_id = int(self.values[0])

        match = next(
            (
                m for m in self.matches
                if m.get("id") == match_id
            ),
            None
        )

        if not match:
            await interaction.response.send_message(
                "❌ Không tìm thấy trận.",
                ephemeral=True
            )
            return

        view = BetChoiceView(match)

        await interaction.response.send_message(
            embed=make_bet_match_embed(match),
            view=view,
            ephemeral=True
        )


class BetMatchView(discord.ui.View):

    def __init__(self, matches):
        super().__init__(timeout=300)

        self.add_item(
            BetMatchSelect(matches)
        )


def make_bet_match_embed(match):
    home = match.get("homeTeam", {}).get(
        "name", "Home"
    )

    away = match.get("awayTeam", {}).get(
        "name", "Away"
    )

    code, emoji, comp_name = competition_info(match)

    embed = discord.Embed(
        title="⚽ CHỌN KÈO",
        description=(
            f"**{home}**\n"
            "        VS\n"
            f"**{away}**"
        ),
        color=discord.Color.green()
    )

    embed.add_field(
        name="🏆 Giải",
        value=f"{emoji} {comp_name}",
        inline=True
    )

    embed.add_field(
        name="🕐 Thời gian",
        value=format_match_date(match),
        inline=True
    )

    embed.set_footer(
        text="Data provided by football-data.org"
    )

    return embed


class BetChoiceView(discord.ui.View):

    def __init__(self, match):
        super().__init__(timeout=300)
        self.match = match

        self.add_item(
            BetChoiceButton(
                "🏠 Đội nhà",
                "HOME",
                discord.ButtonStyle.primary,
                match
            )
        )

        self.add_item(
            BetChoiceButton(
                "🤝 Hòa",
                "DRAW",
                discord.ButtonStyle.secondary,
                match
            )
        )

        self.add_item(
            BetChoiceButton(
                "✈️ Đội khách",
                "AWAY",
                discord.ButtonStyle.success,
                match
            )
        )


class BetChoiceButton(discord.ui.Button):

    def __init__(
        self,
        label,
        choice,
        style,
        match
    ):
        super().__init__(
            label=label,
            style=style
        )

        self.choice = choice
        self.match = match

    async def callback(self, interaction):
        await interaction.response.send_modal(
            BetAmountModal(
                self.match,
                self.choice
            )
        )


class BetAmountModal(discord.ui.Modal):

    def __init__(self, match, choice):
        super().__init__(
            title="💰 Nhập tiền cược"
        )

        self.match = match
        self.choice = choice

        self.amount = discord.ui.TextInput(
            label="Số xu muốn cược",
            placeholder="Ví dụ: 10000",
            min_length=1,
            max_length=12,
            required=True
        )

        self.add_item(self.amount)

    async def on_submit(self, interaction):
        try:
            amount = int(
                self.amount.value.replace(",", "").replace(".", "")
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

        user_id = interaction.user.id

        ensure_wallet(user_id)

        balance = get_balance(user_id)

        if amount > balance:
            await interaction.response.send_message(
                f"❌ Không đủ xu.\n"
                f"💰 Số dư: **{balance:,} xu**",
                ephemeral=True
            )
            return

        match_id = self.match.get("id")

        existing = db.execute(
            """
            SELECT id FROM bets
            WHERE user_id=?
            AND match_id=?
            AND status='PENDING'
            """,
            (user_id, match_id)
        ).fetchone()

        if existing:
            await interaction.response.send_message(
                "❌ M đã cược trận này rồi. "
                "Chờ trận kết thúc.",
                ephemeral=True
            )
            return

        # Tính odds từ prediction
        prediction = await predict_match(self.match)

        if not prediction:
            await interaction.response.send_message(
                "❌ Không lấy được dữ liệu dự đoán.",
                ephemeral=True
            )
            return

        if self.choice == "HOME":
            probability = prediction["home_probability"]

        elif self.choice == "DRAW":
            probability = prediction["draw_probability"]

        else:
            probability = prediction["away_probability"]

        # Odds bot tự tính, không phải odds nhà cái
        probability = max(
            0.08,
            min(probability, 0.92)
        )

        odds = round(
            1 / probability,
            2
        )

        await remove_money(
            user_id,
            amount
        )

        db.execute(
            """
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
            """,
            (
                user_id,
                match_id,
                match_display_name(self.match),
                self.choice,
                amount,
                odds,
                datetime.now(TIMEZONE).isoformat()
            )
        )

        db.execute(
            """
            UPDATE wallets
            SET total_bets=total_bets+1,
                pending_bets=pending_bets+1
            WHERE user_id=?
            """,
            (user_id,)
        )

        db.commit()

        new_balance = get_balance(user_id)

        await interaction.response.send_message(
            embed=discord.Embed(
                title="✅ ĐẶT CƯỢC THÀNH CÔNG",
                description=(
                    f"⚽ **{match_display_name(self.match)}**\n\n"
                    f"🎯 Cửa: **{self.choice}**\n"
                    f"💰 Tiền cược: **{amount:,} xu**\n"
                    f"📈 Odds bot: **x{odds}**\n"
                    f"💳 Số dư còn: **{new_balance:,} xu**"
                ),
                color=discord.Color.green()
            ),
            ephemeral=True
        )


class BetLeagueView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(BetLeagueSelect())


# =========================================================
# RESULT / SETTLEMENT
# =========================================================

def determine_result(match):
    status = match.get("status")

    if status not in (
        "FINISHED",
        "AWARDED"
    ):
        return None

    home_score = score_value(match, "home")
    away_score = score_value(match, "away")

    if home_score is None or away_score is None:
        return None

    if home_score > away_score:
        return "HOME"

    if home_score < away_score:
        return "AWAY"

    return "DRAW"


async def send_result_dm(
    user_id,
    bet,
    match,
    result,
    payout
):
    try:
        user = bot.get_user(user_id)

        if user is None:
            user = await bot.fetch_user(user_id)

        if user is None:
            return

        home = match.get("homeTeam", {}).get(
            "name", "Home"
        )

        away = match.get("awayTeam", {}).get(
            "name", "Away"
        )

        hs = score_value(match, "home")
        aws = score_value(match, "away")

        if result == bet["choice"]:
            status_text = "🎉 THẮNG"
            color = discord.Color.green()
            description = (
                f"💰 Tiền thắng: **{payout:,} xu**"
            )
        else:
            status_text = "❌ THUA"
            color = discord.Color.red()
            description = (
                f"💸 Mất: **{bet['amount']:,} xu**"
            )

        balance = get_balance(user_id)

        embed = discord.Embed(
            title=status_text,
            description=(
                f"⚽ **{home} {hs} - {aws} {away}**\n\n"
                f"🎯 Cửa đã cược: **{bet['choice']}**\n"
                f"{description}\n\n"
                f"💳 Số dư hiện tại: **{balance:,} xu**"
            ),
            color=color
        )

        embed.set_footer(
            text="Data provided by football-data.org"
        )

        await user.send(embed=embed)

    except Exception as e:
        print("DM result error:", e)


async def settle_one(bet):
    match_id = bet["match_id"]

    data = await football.request(
        "/matches",
        params={
            "ids": match_id
        },
        cache_seconds=60
    )

    if not data:
        return False

    matches = data.get("matches", [])

    if not matches:
        return False

    match = matches[0]

    result = determine_result(match)

    if not result:
        return False

    won = (
        result == bet["choice"]
    )

    payout = 0

    if won:
        payout = int(
            bet["amount"] * bet["odds"]
        )

        await add_money(
            bet["user_id"],
            payout
        )

    db.execute(
        """
        UPDATE bets
        SET status=?,
            payout=?,
            settled_at=?
        WHERE id=?
        """,
        (
            "WON" if won else "LOST",
            payout,
            datetime.now(TIMEZONE).isoformat(),
            bet["id"]
        )
    )

    db.execute(
        """
        UPDATE wallets
        SET pending_bets=MAX(pending_bets-1, 0)
        WHERE user_id=?
        """,
        (bet["user_id"],)
    )

    db.commit()

    await send_result_dm(
        bet["user_id"],
        bet,
        match,
        result,
        payout
    )

    return True


# =========================================================
# SLASH COMMANDS
# =========================================================

@bot.tree.command(
    name="ping",
    description="Kiểm tra bot"
)
async def ping(interaction):
    await interaction.response.send_message(
        f"🏓 Pong! `{round(bot.latency * 1000)}ms`"
    )


@bot.tree.command(
    name="apiquota",
    description="Kiểm tra trạng thái API"
)
async def apiquota(interaction):
    ok = await football.test()

    await interaction.response.send_message(
        "🟢 Football API đang hoạt động."
        if ok
        else
        "🔴 Football API không phản hồi."
    )


@bot.tree.command(
    name="comat",
    description="Nhận 50.000 xu mỗi ngày"
)
async def comat(interaction):
    user_id = interaction.user.id

    ensure_wallet(user_id)

    today = datetime.now(TIMEZONE).date().isoformat()

    claimed = db.execute(
        """
        SELECT 1 FROM daily_claims
        WHERE user_id=? AND claim_date=?
        """,
        (user_id, today)
    ).fetchone()

    if claimed:
        await interaction.response.send_message(
            "❌ Hôm nay m đã nhận **50,000 xu** rồi.",
            ephemeral=True
        )
        return

    db.execute(
        """
        INSERT INTO daily_claims
        (user_id, claim_date)
        VALUES (?, ?)
        """,
        (user_id, today)
    )

    db.commit()

    await add_money(
        user_id,
        50_000,
        interaction.channel
    )

    balance = get_balance(user_id)

    await interaction.response.send_message(
        f"💰 M nhận được **50,000 xu**!\n"
        f"💳 Số dư: **{balance:,} xu**"
    )


@bot.tree.command(
    name="khoinghiep",
    description="Nhận 1.000 xu khởi nghiệp"
)
async def khoinghiep(interaction):
    user_id = interaction.user.id

    ensure_wallet(user_id)

    row = db.execute(
        "SELECT started FROM wallets WHERE user_id=?",
        (user_id,)
    ).fetchone()

    if row["started"]:
        await interaction.response.send_message(
            "❌ M đã nhận xu khởi nghiệp rồi.",
            ephemeral=True
        )
        return

    db.execute(
        """
        UPDATE wallets
        SET started=1
        WHERE user_id=?
        """,
        (user_id,)
    )

    db.commit()

    await add_money(
        user_id,
        1_000,
        interaction.channel
    )

    balance = get_balance(user_id)

    await interaction.response.send_message(
        f"🚀 M nhận **1,000 xu khởi nghiệp**!\n"
        f"💳 Số dư: **{balance:,} xu**"
    )


@bot.tree.command(
    name="vi",
    description="Xem ví và cấp VIP"
)
async def vi(interaction):
    user_id = interaction.user.id

    ensure_wallet(user_id)

    row = db.execute(
        """
        SELECT balance, total_bets, pending_bets, vip
        FROM wallets
        WHERE user_id=?
        """,
        (user_id,)
    ).fetchone()

    current_vip = get_vip_level(row["balance"])

    if current_vip != row["vip"]:
        db.execute(
            "UPDATE wallets SET vip=? WHERE user_id=?",
            (current_vip, user_id)
        )
        db.commit()

    embed = discord.Embed(
        title="💳 VÍ CỦA BẠN",
        color=discord.Color.gold()
    )

    embed.add_field(
        name="💰 Số dư",
        value=f"**{row['balance']:,} xu**",
        inline=False
    )

    embed.add_field(
        name="👑 VIP",
        value=(
            f"{vip_emoji(current_vip)} "
            f"**{vip_name(current_vip)}**"
        ),
        inline=True
    )

    embed.add_field(
        name="🎯 Tổng cược",
        value=f"**{row['total_bets']}**",
        inline=True
    )

    embed.add_field(
        name="⏳ Đang chờ",
        value=f"**{row['pending_bets']}**",
        inline=True
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# =========================================================
# ADMIN
# =========================================================

class AdminModal(discord.ui.Modal):

    def __init__(self):
        super().__init__(
            title="👑 ADMIN QUẢN LÝ XU"
        )

        self.password = discord.ui.TextInput(
            label="Mật khẩu",
            required=True,
            style=discord.TextStyle.short
        )

        self.username = discord.ui.TextInput(
            label="Username / nickname",
            required=True
        )

        self.amount = discord.ui.TextInput(
            label="Số xu (+ thêm / - trừ)",
            required=True
        )

        self.add_item(self.password)
        self.add_item(self.username)
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
                self.amount.value.replace(",", "")
            )
        except ValueError:
            await interaction.response.send_message(
                "❌ Số xu không hợp lệ.",
                ephemeral=True
            )
            return

        target = None

        for member in interaction.guild.members:
            if (
                member.name.lower()
                == self.username.value.lower()
                or
                member.display_name.lower()
                == self.username.value.lower()
            ):
                target = member
                break

        if target is None:
            await interaction.response.send_message(
                "❌ Không tìm thấy thành viên.",
                ephemeral=True
            )
            return

        ensure_wallet(target.id)

        if amount >= 0:
            await add_money(
                target.id,
                amount,
                interaction.channel
            )
        else:
            await remove_money(
                target.id,
                abs(amount)
            )

        balance = get_balance(target.id)

        await interaction.response.send_message(
            f"👑 Đã cập nhật ví của **{target.display_name}**.\n"
            f"💰 Thay đổi: **{amount:+,} xu**\n"
            f"💳 Số dư: **{balance:,} xu**"
        )


@bot.tree.command(
    name="admin",
    description="Admin cộng/trừ xu"
)
async def admin(interaction):
    await interaction.response.send_modal(
        AdminModal()
    )


# =========================================================
# CUOC
# =========================================================

@bot.tree.command(
    name="cuoc",
    description="Cược bóng đá"
)
async def cuoc(interaction):
    await interaction.response.send_message(
        embed=discord.Embed(
            title="🎰 HỆ THỐNG CƯỢC BÓNG ĐÁ",
            description=(
                "🏆 Chọn giải → chọn trận → chọn cửa → "
                "nhập số xu.\n\n"
                "💰 Tiền cược sử dụng xu trong ví."
            ),
            color=discord.Color.green()
        ),
        view=BetLeagueView(),
        ephemeral=True
    )


# =========================================================
# SOI
# =========================================================

@bot.tree.command(
    name="soi",
    description="Soi các giải và toàn bộ trận Man City"
)
async def soi(interaction):
    await interaction.response.defer()

    city_matches = await football.man_city_matches(
        season=CURRENT_SEASON
    )

    if not city_matches:
        await interaction.followup.send(
            "❌ API hiện không trả về dữ liệu trận đấu "
            "của Man City trong mùa 2026."
        )
        return

    embed = await build_soi_embed(
        city_matches
    )

    await interaction.followup.send(
        embed=embed,
        view=SoiView(city_matches)
    )


# =========================================================
# MANUAL SETTLE
# =========================================================

@bot.tree.command(
    name="kettoan",
    description="Kiểm tra và kết toán các cược đã kết thúc"
)
async def kettoan(interaction):
    await interaction.response.defer(
        ephemeral=True
    )

    bets = db.execute(
        """
        SELECT *
        FROM bets
        WHERE status='PENDING'
        ORDER BY id ASC
        LIMIT 30
        """
    ).fetchall()

    settled = 0

    for bet in bets:
        ok = await settle_one(bet)

        if ok:
            settled += 1

        await asyncio.sleep(0.15)

    await interaction.followup.send(
        f"✅ Đã kiểm tra **{len(bets)}** cược.\n"
        f"🎯 Đã kết toán: **{settled}** cược.",
        ephemeral=True
    )


# =========================================================
# AUTO SETTLEMENT
# =========================================================

@tasks.loop(minutes=5)
async def settlement_loop():
    try:
        bets = db.execute(
            """
            SELECT *
            FROM bets
            WHERE status='PENDING'
            ORDER BY id ASC
            LIMIT 20
            """
        ).fetchall()

        for bet in bets:
            await settle_one(bet)
            await asyncio.sleep(0.2)

    except Exception as e:
        print("Settlement loop error:", e)


# =========================================================
# FOOTBALL ALERT
# =========================================================

async def send_match_alert(match):
    if not ALERT_CHANNEL_ID:
        return

    channel = bot.get_channel(
        ALERT_CHANNEL_ID
    )

    if channel is None:
        return

    match_id = match.get("id")

    if not match_id:
        return

    exists = db.execute(
        """
        SELECT 1 FROM sent_alerts
        WHERE match_id=?
        """,
        (match_id,)
    ).fetchone()

    if exists:
        return

    if not is_future_match(match):
        return

    dt = parse_dt(match.get("utcDate"))

    if not dt:
        return

    # Chỉ cảnh báo trước trận tối đa 30 phút
    diff = dt - datetime.now(TIMEZONE)

    if diff.total_seconds() < 0:
        return

    if diff.total_seconds() > 1800:
        return

    code, emoji, comp_name = competition_info(match)

    embed = discord.Embed(
        title="🚨 TRẬN SẮP ĐÁ",
        description=(
            f"{emoji} **{comp_name}**\n\n"
            f"⚽ **{match_display_name(match)}**\n"
            f"🕐 {format_match_date(match)}"
        ),
        color=discord.Color.orange()
    )

    embed.set_footer(
        text="Data provided by football-data.org"
    )

    await channel.send(
        embed=embed
    )

    db.execute(
        """
        INSERT OR IGNORE INTO sent_alerts
        (match_id, sent_at)
        VALUES (?, ?)
        """,
        (
            match_id,
            datetime.now(TIMEZONE).isoformat()
        )
    )

    db.commit()


@tasks.loop(minutes=1)
async def alert_loop():
    try:
        matches = await football.man_city_matches(
            season=CURRENT_SEASON
        )

        for match in matches:
            await send_match_alert(match)

    except Exception as e:
        print("Alert loop error:", e)


# =========================================================
# BOT EVENTS
# =========================================================

@bot.event
async def setup_hook():
    guild = discord.Object(
        id=GUILD_ID
    )

    bot.tree.copy_global_to(
        guild=guild
    )

    await bot.tree.sync(
        guild=guild
    )

    print("Slash commands synced.")


@bot.event
async def on_ready():
    print(
        f"Logged in as {bot.user} "
        f"(ID: {bot.user.id})"
    )

    ok = await football.test()

    print(
        "Football API:",
        "OK" if ok else "FAILED"
    )

    if not settlement_loop.is_running():
        settlement_loop.start()

    if not alert_loop.is_running():
        alert_loop.start()


# =========================================================
# RUN
# =========================================================

if not DISCORD_TOKEN:
    raise RuntimeError(
        "Missing DISCORD_TOKEN environment variable."
    )

if not FOOTBALL_DATA_TOKEN:
    raise RuntimeError(
        "Missing FOOTBALL_DATA_TOKEN environment variable."
    )

try:
    bot.run(DISCORD_TOKEN)
finally:
    try:
        asyncio.run(
            football.close()
        )
    except Exception:
        pass

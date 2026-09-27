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
FOOTBALL_TOKEN = os.getenv("FOOTBALL_DATA_TOKEN")

GUILD_ID = 1551547049600618538

ALERT_CHANNEL_ID_RAW = os.getenv(
    "FOOTBALL_ALERT_CHANNEL_ID",
    ""
)

TIMEZONE = ZoneInfo("Asia/Ho_Chi_Minh")

ADMIN_PASSWORD = "provip👑"

DAILY_MONEY = 50_000
STARTING_MONEY = 1_000

API_BASE = "https://api.football-data.org/v4"

MANCITY_ID = 65
CL_CODE = "CL"

DB_FILE = "football_bot.db"


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
CREATE TABLE IF NOT EXISTS daily_claims (
    user_id INTEGER PRIMARY KEY,
    last_claim TEXT
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
    payout INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
)
""")


db.execute("""
CREATE TABLE IF NOT EXISTS sent_alerts (
    alert_key TEXT PRIMARY KEY
)
""")

db.commit()


def db_commit():
    db.commit()


# =========================================================
# TIME
# =========================================================

def now_vn():
    return datetime.now(TIMEZONE)


def today_string():
    return now_vn().strftime("%Y-%m-%d")


# =========================================================
# WALLET
# =========================================================

def ensure_wallet(user_id):

    row = db.execute(
        """
        SELECT user_id
        FROM wallets
        WHERE user_id = ?
        """,
        (user_id,)
    ).fetchone()

    if row is None:

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

        db_commit()


def get_balance(user_id):

    ensure_wallet(user_id)

    row = db.execute(
        """
        SELECT balance
        FROM wallets
        WHERE user_id = ?
        """,
        (user_id,)
    ).fetchone()

    return int(row["balance"])


def add_wallet(user_id, amount):

    ensure_wallet(user_id)

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

    db_commit()


def remove_wallet(user_id, amount):

    balance = get_balance(user_id)

    if balance < amount:
        return False

    db.execute(
        """
        UPDATE wallets
        SET balance = balance - ?
        WHERE user_id = ?
        """,
        (
            amount,
            user_id
        )
    )

    db_commit()

    return True


# =========================================================
# FOOTBALL API
# =========================================================

class FootballAPI:

    def __init__(self):

        self.session = None

        self.cache = {}

        self.cache_seconds = 300

        self.calls = []

    async def start(self):

        if self.session is None:

            self.session = aiohttp.ClientSession(
                headers={
                    "X-Auth-Token":
                    FOOTBALL_TOKEN or ""
                }
            )

    async def close(self):

        if self.session:

            await self.session.close()

            self.session = None

    async def get(
        self,
        endpoint,
        cache_seconds=None
    ):

        if not FOOTBALL_TOKEN:

            raise RuntimeError(
                "Chưa có FOOTBALL_DATA_TOKEN."
            )

        if self.session is None:
            await self.start()

        if cache_seconds is None:
            cache_seconds = self.cache_seconds

        current = datetime.now().timestamp()

        cached = self.cache.get(endpoint)

        if cached:

            saved_time, data = cached

            if current - saved_time < cache_seconds:
                return data

        # Giới hạn request để tránh vượt Free Tier
        self.calls = [
            x for x in self.calls
            if current - x < 60
        ]

        if len(self.calls) >= 9:

            wait_time = (
                60 - (
                    current - self.calls[0]
                )
            )

            if wait_time > 0:
                await asyncio.sleep(
                    wait_time
                )

        self.calls.append(
            datetime.now().timestamp()
        )

        async with self.session.get(
            API_BASE + endpoint
        ) as response:

            if response.status == 429:

                await asyncio.sleep(5)

                async with self.session.get(
                    API_BASE + endpoint
                ) as retry:

                    if retry.status != 200:

                        raise RuntimeError(
                            f"API lỗi {retry.status}"
                        )

                    data = await retry.json()

            elif response.status != 200:

                text = await response.text()

                raise RuntimeError(
                    f"API lỗi {response.status}: "
                    f"{text[:300]}"
                )

            else:

                data = await response.json()

        self.cache[endpoint] = (
            datetime.now().timestamp(),
            data
        )

        return data


football = FootballAPI()


# =========================================================
# FOOTBALL MATCHES
# =========================================================

async def get_mancity_matches():

    start = (
        now_vn() -
        timedelta(days=2)
    ).strftime("%Y-%m-%d")

    end = (
        now_vn() +
        timedelta(days=30)
    ).strftime("%Y-%m-%d")

    endpoint = (
        f"/teams/{MANCITY_ID}/matches"
        f"?dateFrom={start}"
        f"&dateTo={end}"
    )

    data = await football.get(endpoint)

    return data.get(
        "matches",
        []
    )


async def get_competition_matches(
    code=CL_CODE
):

    start = (
        now_vn() -
        timedelta(days=2)
    ).strftime("%Y-%m-%d")

    end = (
        now_vn() +
        timedelta(days=30)
    ).strftime("%Y-%m-%d")

    endpoint = (
        f"/competitions/{code}/matches"
        f"?dateFrom={start}"
        f"&dateTo={end}"
    )

    data = await football.get(endpoint)

    return data.get(
        "matches",
        []
    )


async def get_match(match_id):

    return await football.get(
        f"/matches/{match_id}",
        cache_seconds=60
    )


def match_is_finished(match):

    return match.get("status") in (
        "FINISHED",
        "AWARDED"
    )


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
        ).astimezone(
            TIMEZONE
        )

    except Exception:

        return None


def is_future_match(match):

    dt = match_datetime(match)

    if not dt:
        return False

    return (
        dt > now_vn()
        and
        not match_is_finished(match)
    )


async def get_all_relevant_matches():

    all_matches = []

    try:

        city = await get_mancity_matches()

        all_matches.extend(city)

    except Exception as e:

        print(
            "Man City API error:",
            e
        )

    try:

        cl = await get_competition_matches(
            CL_CODE
        )

        all_matches.extend(cl)

    except Exception as e:

        print(
            "CL API error:",
            e
        )

    unique = {}

    for match in all_matches:

        match_id = match.get("id")

        if match_id:

            unique[match_id] = match

    return list(
        unique.values()
    )


# =========================================================
# PREDICTION
# =========================================================

async def get_recent_team_matches(
    team_id,
    limit=5
):

    try:

        endpoint = (
            f"/teams/{team_id}/matches"
            f"?status=FINISHED"
            f"&limit={limit}"
        )

        data = await football.get(
            endpoint,
            cache_seconds=600
        )

        return data.get(
            "matches",
            []
        )[-limit:]

    except Exception:

        return []


async def team_average_goals(
    team_id
):

    matches = await get_recent_team_matches(
        team_id,
        5
    )

    if not matches:

        return 1.2, 1.2

    scored = []
    conceded = []

    for match in matches:

        home = match.get(
            "homeTeam",
            {}
        )

        away = match.get(
            "awayTeam",
            {}
        )

        score = match.get(
            "score",
            {}
        ).get(
            "fullTime",
            {}
        )

        hg = score.get("home")
        ag = score.get("away")

        if hg is None or ag is None:
            continue

        if home.get("id") == team_id:

            scored.append(hg)
            conceded.append(ag)

        elif away.get("id") == team_id:

            scored.append(ag)
            conceded.append(hg)

    if not scored:

        return 1.2, 1.2

    return (
        max(
            0.3,
            sum(scored) / len(scored)
        ),
        max(
            0.3,
            sum(conceded) / len(conceded)
        )
    )


def poisson_probability(
    lam,
    goals
):

    try:

        return (
            math.exp(-lam)
            *
            (lam ** goals)
            /
            math.factorial(goals)
        )

    except Exception:

        return 0


def calculate_prediction(
    home_scored,
    home_conceded,
    away_scored,
    away_conceded
):

    home_lambda = (
        home_scored +
        away_conceded
    ) / 2

    away_lambda = (
        away_scored +
        home_conceded
    ) / 2

    home_win = 0
    draw = 0
    away_win = 0

    for hg in range(8):

        for ag in range(8):

            probability = (
                poisson_probability(
                    home_lambda,
                    hg
                )
                *
                poisson_probability(
                    away_lambda,
                    ag
                )
            )

            if hg > ag:

                home_win += probability

            elif hg == ag:

                draw += probability

            else:

                away_win += probability

    total = (
        home_win +
        draw +
        away_win
    )

    if total <= 0:

        return {
            "home": 33.3,
            "draw": 33.3,
            "away": 33.4
        }

    return {
        "home":
            home_win / total * 100,

        "draw":
            draw / total * 100,

        "away":
            away_win / total * 100
    }


async def predict_match(match):

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

    if not home_id or not away_id:

        return {
            "home": 33.3,
            "draw": 33.3,
            "away": 33.4
        }

    hs, hc = await team_average_goals(
        home_id
    )

    aws, awc = await team_average_goals(
        away_id
    )

    return calculate_prediction(
        hs,
        hc,
        aws,
        awc
    )


# =========================================================
# DISPLAY
# =========================================================

def match_title(match):

    home = match.get(
        "homeTeam",
        {}
    ).get(
        "shortName"
    ) or match.get(
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
        "shortName"
    ) or match.get(
        "awayTeam",
        {}
    ).get(
        "name",
        "Away"
    )

    return f"{home} vs {away}"


def competition_name(match):

    return match.get(
        "competition",
        {}
    ).get(
        "name",
        "Football"
    )


def format_match_time(match):

    dt = match_datetime(match)

    if not dt:
        return "Chưa rõ giờ"

    return dt.strftime(
        "%d/%m/%Y %H:%M"
    )


def choice_text(choice):

    return {
        "HOME": "Đội nhà",
        "DRAW": "Hòa",
        "AWAY": "Đội khách"
    }.get(
        choice,
        choice
    )


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

    ensure_wallet(user_id)

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
            "Hôm nay m nhận xu rồi. Mai quay lại nhận tiếp.",
            ephemeral=True
        )

        return

    add_wallet(
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

    db.execute(
        """
        UPDATE wallets
        SET started = 1
        WHERE user_id = ?
        """,
        (user_id,)
    )

    db_commit()

    balance = get_balance(
        user_id
    )

    await interaction.response.send_message(
        f"Đã nhận **{DAILY_MONEY:,} xu**.\n"
        f"Số dư: **{balance:,} xu**.",
        ephemeral=True
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

    ensure_wallet(user_id)

    row = db.execute(
        """
        SELECT started
        FROM wallets
        WHERE user_id = ?
        """,
        (user_id,)
    ).fetchone()

    if row["started"]:

        await interaction.response.send_message(
            "M đã nhận xu khởi nghiệp rồi.",
            ephemeral=True
        )

        return

    add_wallet(
        user_id,
        STARTING_MONEY
    )

    db.execute(
        """
        UPDATE wallets
        SET started = 1
        WHERE user_id = ?
        """,
        (user_id,)
    )

    db_commit()

    await interaction.response.send_message(
        f"Đã nhận **{STARTING_MONEY:,} xu**.",
        ephemeral=True
    )


# =========================================================
# /VI
# =========================================================

@bot.tree.command(
    name="vi",
    description="Xem số dư xu"
)
async def vi(
    interaction: discord.Interaction
):

    user_id = interaction.user.id

    balance = get_balance(
        user_id
    )

    total = db.execute(
        """
        SELECT COUNT(*) AS c
        FROM bets
        WHERE user_id = ?
        """,
        (user_id,)
    ).fetchone()["c"]

    pending = db.execute(
        """
        SELECT COUNT(*) AS c
        FROM bets
        WHERE user_id = ?
        AND status = 'PENDING'
        """,
        (user_id,)
    ).fetchone()["c"]

    await interaction.response.send_message(
        f"**Ví của {interaction.user.display_name}**\n\n"
        f"Xu: **{balance:,}**\n"
        f"Tổng cược: **{total}**\n"
        f"Đang chờ: **{pending}**",
        ephemeral=True
    )


# =========================================================
# ADMIN
# =========================================================

class AdminMoneyModal(
    discord.ui.Modal,
    title="Admin cộng xu"
):

    password = discord.ui.TextInput(
        label="Mật khẩu",
        placeholder="Nhập mật khẩu Admin",
        required=True,
        max_length=100
    )

    target_name = discord.ui.TextInput(
        label="Tên người nhận",
        placeholder="Username hoặc nickname",
        required=True,
        max_length=100
    )

    amount = discord.ui.TextInput(
        label="Số xu",
        placeholder="Ví dụ: 50000",
        required=True,
        max_length=20
    )

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        # ==============================
        # KIỂM TRA MẬT KHẨU
        # ==============================

        if str(self.password.value) != ADMIN_PASSWORD:

            await interaction.response.send_message(
                "Sai mật khẩu Admin.",
                ephemeral=True
            )

            return

        # ==============================
        # LẤY DỮ LIỆU
        # ==============================

        target_name = str(
            self.target_name.value
        ).strip()

        amount_text = str(
            self.amount.value
        ).strip()

        # ==============================
        # KIỂM TRA SỐ XU
        # ==============================

        try:

            amount = int(
                amount_text
            )

        except ValueError:

            await interaction.response.send_message(
                "Số xu phải là số.",
                ephemeral=True
            )

            return

        if amount <= 0:

            await interaction.response.send_message(
                "Số xu phải lớn hơn 0.",
                ephemeral=True
            )

            return

        # ==============================
        # TÌM MEMBER
        # ==============================

        target = None

        for member in interaction.guild.members:

            username = (
                member.name or ""
            ).lower()

            nickname = (
                member.display_name or ""
            ).lower()

            search = target_name.lower()

            if username == search:

                target = member
                break

            if nickname == search:

                target = member
                break

        # ==============================
        # KHÔNG TÌM THẤY
        # ==============================

        if target is None:

            await interaction.response.send_message(
                f"Không tìm thấy `{target_name}` trong server.",
                ephemeral=True
            )

            return

        # ==============================
        # CỘNG XU
        # ==============================

        add_wallet(
            target.id,
            amount
        )

        balance = get_balance(
            target.id
        )

        await interaction.response.send_message(
            f"Đã cộng **{amount:,} xu** cho "
            f"`{target.display_name}`.\n"
            f"Số dư mới: **{balance:,} xu**.",
            ephemeral=True
        )


@bot.tree.command(
    name="admin",
    description="Admin cộng xu"
)
async def admin_command(
    interaction: discord.Interaction
):

    await interaction.response.send_modal(
        AdminMoneyModal()
    )


# =========================================================
# BET AMOUNT MODAL
# =========================================================

class BetAmountModal(
    discord.ui.Modal,
    title="Nhập số xu cược"
):

    amount = discord.ui.TextInput(
        label="Số xu",
        placeholder="Ví dụ: 10000",
        required=True,
        max_length=20
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

        try:

            amount = int(
                str(
                    self.amount.value
                ).strip()
            )

        except ValueError:

            await interaction.response.send_message(
                "Số xu phải là số.",
                ephemeral=True
            )

            return

        if amount <= 0:

            await interaction.response.send_message(
                "Số xu phải lớn hơn 0.",
                ephemeral=True
            )

            return

        balance = get_balance(
            interaction.user.id
        )

        if balance < amount:

            await interaction.response.send_message(
                f"Không đủ xu.\n"
                f"Số dư: **{balance:,}** xu.",
                ephemeral=True
            )

            return

        match_id = self.match.get(
            "id"
        )

        existing = db.execute(
            """
            SELECT id
            FROM bets
            WHERE user_id = ?
            AND match_id = ?
            AND status = 'PENDING'
            """,
            (
                interaction.user.id,
                match_id
            )
        ).fetchone()

        if existing:

            await interaction.response.send_message(
                "M đã cược trận này rồi.",
                ephemeral=True
            )

            return

        prediction = await predict_match(
            self.match
        )

        if self.choice == "HOME":

            probability = prediction[
                "home"
            ]

        elif self.choice == "DRAW":

            probability = prediction[
                "draw"
            ]

        else:

            probability = prediction[
                "away"
            ]

        probability = max(
            5,
            min(90, probability)
        )

        odds = round(
            100 / probability,
            2
        )

        if not remove_wallet(
            interaction.user.id,
            amount
        ):

            await interaction.response.send_message(
                "Không đủ xu.",
                ephemeral=True
            )

            return

        home = self.match.get(
            "homeTeam",
            {}
        ).get(
            "shortName",
            "Home"
        )

        away = self.match.get(
            "awayTeam",
            {}
        ).get(
            "shortName",
            "Away"
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
            VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', 0, ?)
            """,
            (
                interaction.user.id,
                match_id,
                home,
                away,
                self.choice,
                amount,
                odds,
                now_vn().isoformat()
            )
        )

        db_commit()

        new_balance = get_balance(
            interaction.user.id
        )

        await interaction.response.send_message(
            f"Đã cược **{amount:,} xu**.\n\n"
            f"Trận: **{home} vs {away}**\n"
            f"Lựa chọn: **{choice_text(self.choice)}**\n"
            f"Odds ảo: **{odds}**\n\n"
            f"Số dư còn: **{new_balance:,} xu**.",
            ephemeral=True
        )


# =========================================================
# BET BUTTONS
# =========================================================

class BetChoiceView(
    discord.ui.View
):

    def __init__(
        self,
        match
    ):

        super().__init__(
            timeout=180
        )

        self.match = match

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
                "HOME"
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
                "DRAW"
            )
        )

    @discord.ui.button(
        label="Đội khách",
        style=discord.ButtonStyle.success
    )
    async def away_button(
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

            home = match.get(
                "homeTeam",
                {}
            ).get(
                "shortName",
                "Home"
            )

            away = match.get(
                "awayTeam",
                {}
            ).get(
                "shortName",
                "Away"
            )

            label = (
                f"{home} vs {away}"
            )

            if len(label) > 100:

                label = (
                    label[:97] +
                    "..."
                )

            options.append(
                discord.SelectOption(
                    label=label,
                    description=
                        format_match_time(
                            match
                        )[:100],
                    value=str(
                        match.get("id")
                    )
                )
            )

        super().__init__(
            placeholder="Chọn trận để cược",
            options=options
        )

    async def callback(
        self,
        interaction
    ):

        match_id = int(
            self.values[0]
        )

        match = next(
            (
                x
                for x in self.matches
                if x.get("id") ==
                match_id
            ),
            None
        )

        if not match:

            await interaction.response.send_message(
                "Không tìm thấy trận.",
                ephemeral=True
            )

            return

        await interaction.response.edit_message(
            content=(
                f"**{match_title(match)}**\n"
                f"Giải: {competition_name(match)}\n"
                f"Giờ: {format_match_time(match)}\n\n"
                f"Chọn cửa cược:"
            ),
            view=BetChoiceView(match)
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
    description="Chọn trận và đặt cược bằng xu"
)
async def cuoc(
    interaction
):

    await interaction.response.defer(
        ephemeral=True
    )

    try:

        matches = (
            await get_all_relevant_matches()
        )

    except Exception as e:

        await interaction.followup.send(
            f"Không lấy được danh sách trận: {e}",
            ephemeral=True
        )

        return

    future = [
        x
        for x in matches
        if is_future_match(x)
    ]

    future.sort(
        key=lambda x:
            match_datetime(x)
            or now_vn()
    )

    if not future:

        await interaction.followup.send(
            "Hiện không có trận sắp tới.",
            ephemeral=True
        )

        return

    await interaction.followup.send(
        "Chọn trận muốn cược:",
        view=MatchSelectView(
            future[:25]
        ),
        ephemeral=True
    )


# =========================================================
# SETTLEMENT
# =========================================================

async def settle_bet(
    bet,
    match
):

    if not match_is_finished(match):
        return False

    score = match.get(
        "score",
        {}
    ).get(
        "fullTime",
        {}
    )

    home_score = score.get(
        "home"
    )

    away_score = score.get(
        "away"
    )

    if (
        home_score is None
        or
        away_score is None
    ):

        return False

    if home_score > away_score:

        result = "HOME"

    elif home_score == away_score:

        result = "DRAW"

    else:

        result = "AWAY"

    payout = 0

    if bet["choice"] == result:

        payout = int(
            round(
                bet["amount"] *
                bet["odds"]
            )
        )

        add_wallet(
            bet["user_id"],
            payout
        )

    db.execute(
        """
        UPDATE bets
        SET status = ?,
            payout = ?
        WHERE id = ?
        """,
        (
            "WON"
            if payout > 0
            else "LOST",
            payout,
            bet["id"]
        )
    )

    db_commit()

    return True


async def settle_all_bets():

    rows = db.execute(
        """
        SELECT *
        FROM bets
        WHERE status = 'PENDING'
        """
    ).fetchall()

    if not rows:
        return 0

    settled = 0
    cache = {}

    for bet in rows:

        match_id = bet["match_id"]

        try:

            if match_id not in cache:

                cache[match_id] = (
                    await get_match(
                        match_id
                    )
                )

            match = cache[
                match_id
            ]

            if await settle_bet(
                bet,
                match
            ):

                settled += 1

        except Exception as e:

            print(
                "Settlement error:",
                e
            )

    return settled


# =========================================================
# /KETTOAN
# =========================================================

@bot.tree.command(
    name="kettoan",
    description="Kết toán cược đã xong"
)
async def kettoan(
    interaction
):

    await interaction.response.defer(
        ephemeral=True
    )

    user_id = interaction.user.id

    bets = db.execute(
        """
        SELECT *
        FROM bets
        WHERE user_id = ?
        AND status = 'PENDING'
        """,
        (user_id,)
    ).fetchall()

    if not bets:

        await interaction.followup.send(
            "M không có cược nào đang chờ.",
            ephemeral=True
        )

        return

    settled = 0

    for bet in bets:

        try:

            match = await get_match(
                bet["match_id"]
            )

            if await settle_bet(
                bet,
                match
            ):

                settled += 1

        except Exception as e:

            print(
                "Manual settlement error:",
                e
            )

    balance = get_balance(
        user_id
    )

    await interaction.followup.send(
        f"Đã kết toán **{settled}** cược.\n"
        f"Số dư: **{balance:,} xu**.",
        ephemeral=True
    )


# =========================================================
# SOI VIEW
# =========================================================

class SoiView(
    discord.ui.View
):

    def __init__(self):

        super().__init__(
            timeout=120
        )

    @discord.ui.button(
        label="Champions League",
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

        try:

            matches = (
                await get_competition_matches(
                    CL_CODE
                )
            )

        except Exception as e:

            await interaction.followup.send(
                f"Lỗi API: {e}",
                ephemeral=True
            )

            return

        future = [
            x
            for x in matches
            if is_future_match(x)
        ]

        future.sort(
            key=lambda x:
                match_datetime(x)
                or now_vn()
        )

        if not future:

            await interaction.followup.send(
                "Không tìm thấy trận C1 sắp tới.",
                ephemeral=True
            )

            return

        match = future[0]

        prediction = await predict_match(
            match
        )

        await interaction.followup.send(
            f"**CHAMPIONS LEAGUE**\n\n"
            f"**{match_title(match)}**\n"
            f"Thời gian: {format_match_time(match)}\n\n"
            f"Đội nhà: "
            f"**{prediction['home']:.1f}%**\n"
            f"Hòa: "
            f"**{prediction['draw']:.1f}%**\n"
            f"Đội khách: "
            f"**{prediction['away']:.1f}%**\n\n"
            f"Đây là mô hình thống kê của bot.",
            ephemeral=True
        )

    @discord.ui.button(
        label="Manchester City",
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

        try:

            matches = (
                await get_mancity_matches()
            )

        except Exception as e:

            await interaction.followup.send(
                f"Lỗi API: {e}",
                ephemeral=True
            )

            return

        future = [
            x
            for x in matches
            if is_future_match(x)
        ]

        future.sort(
            key=lambda x:
                match_datetime(x)
                or now_vn()
        )

        if not future:

            await interaction.followup.send(
                "Không tìm thấy trận Man City sắp tới.",
                ephemeral=True
            )

            return

        match = future[0]

        prediction = await predict_match(
            match
        )

        await interaction.followup.send(
            f"**MANCHESTER CITY**\n\n"
            f"**{match_title(match)}**\n"
            f"Thời gian: {format_match_time(match)}\n"
            f"Giải: {competition_name(match)}\n\n"
            f"Đội nhà: "
            f"**{prediction['home']:.1f}%**\n"
            f"Hòa: "
            f"**{prediction['draw']:.1f}%**\n"
            f"Đội khách: "
            f"**{prediction['away']:.1f}%**\n\n"
            f"Đây là mô hình thống kê của bot.",
            ephemeral=True
        )


# =========================================================
# /SOI
# =========================================================

@bot.tree.command(
    name="soi",
    description="Xem trận C1 hoặc Manchester City"
)
async def soi(
    interaction
):

    await interaction.response.send_message(
        "Chọn mục:",
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
async def ping(
    interaction
):

    await interaction.response.send_message(
        f"Pong! "
        f"`{round(bot.latency * 1000)}ms`",
        ephemeral=True
    )


# =========================================================
# /APIQUOTA
# =========================================================

@bot.tree.command(
    name="apiquota",
    description="Kiểm tra API"
)
async def apiquota(
    interaction
):

    current = datetime.now().timestamp()

    football.calls = [
        x
        for x in football.calls
        if current - x < 60
    ]

    await interaction.response.send_message(
        f"API request 60 giây gần nhất: "
        f"**{len(football.calls)} / 10**",
        ephemeral=True
    )


# =========================================================
# AUTO SETTLEMENT
# =========================================================

@tasks.loop(minutes=5)
async def settlement_loop():

    try:

        count = await settle_all_bets()

        if count:

            print(
                f"Auto settlement: {count} bets"
            )

    except Exception as e:

        print(
            "Settlement loop error:",
            e
        )


# =========================================================
# ALERT
# =========================================================

def get_alert_channel_id():

    if not ALERT_CHANNEL_ID_RAW:
        return None

    raw = ALERT_CHANNEL_ID_RAW.strip()

    if (
        raw.startswith("<#")
        and
        raw.endswith(">")
    ):

        raw = raw[2:-1]

    try:

        return int(raw)

    except ValueError:

        return None


async def send_alert(
    match,
    alert_type
):

    channel_id = get_alert_channel_id()

    if not channel_id:
        return

    channel = bot.get_channel(
        channel_id
    )

    if not channel:
        return

    match_id = match.get("id")

    key = (
        f"{match_id}:"
        f"{alert_type}"
    )

    existing = db.execute(
        """
        SELECT alert_key
        FROM sent_alerts
        WHERE alert_key = ?
        """,
        (key,)
    ).fetchone()

    if existing:
        return

    title = match_title(match)

    if alert_type == "PRE":

        content = (
            f"⚽ **SẮP ĐÁ**\n"
            f"**{title}**\n"
            f"{format_match_time(match)}"
        )

    elif alert_type == "LIVE":

        content = (
            f"🔴 **ĐANG ĐÁ**\n"
            f"**{title}**"
        )

    elif alert_type == "FINISHED":

        score = match.get(
            "score",
            {}
        ).get(
            "fullTime",
            {}
        )

        content = (
            f"🏁 **KẾT THÚC**\n"
            f"**{title}**\n"
            f"{score.get('home', '?')} - "
            f"{score.get('away', '?')}"
        )

    else:

        return

    try:

        await channel.send(
            content
        )

        db.execute(
            """
            INSERT OR IGNORE INTO sent_alerts(
                alert_key
            )
            VALUES (?)
            """,
            (key,)
        )

        db_commit()

    except Exception as e:

        print(
            "Alert error:",
            e
        )


@tasks.loop(minutes=1)
async def alert_loop():

    try:

        matches = (
            await get_all_relevant_matches()
        )

        current = now_vn()

        for match in matches:

            dt = match_datetime(match)

            if not dt:
                continue

            status = match.get(
                "status"
            )

            minutes_until = (
                dt - current
            ).total_seconds() / 60

            # Báo trước trận 15 phút
            if (
                0 <= minutes_until <= 15
                and
                status not in (
                    "FINISHED",
                    "AWARDED"
                )
            ):

                await send_alert(
                    match,
                    "PRE"
                )

            # Đang đá
            if status in (
                "IN_PLAY",
                "PAUSED"
            ):

                await send_alert(
                    match,
                    "LIVE"
                )

            # Kết thúc
            if status in (
                "FINISHED",
                "AWARDED"
            ):

                await send_alert(
                    match,
                    "FINISHED"
                )

    except Exception as e:

        print(
            "Alert loop error:",
            e
        )


# =========================================================
# COMMAND SYNC
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
        f"Đã sync {len(synced)} commands."
    )


# =========================================================
# READY
# =========================================================

@bot.event
async def on_ready():

    print(
        f"Bot online: {bot.user}"
    )

    if not settlement_loop.is_running():

        settlement_loop.start()

    if not alert_loop.is_running():

        alert_loop.start()

    try:

        await football.start()

        city = (
            await get_mancity_matches()
        )

        cl = (
            await get_competition_matches(
                CL_CODE
            )
        )

        print(
            f"Man City matches: "
            f"{len(city)}"
        )

        print(
            f"Champions League matches: "
            f"{len(cl)}"
        )

        city_future = [
            x
            for x in city
            if is_future_match(x)
        ]

        cl_future = [
            x
            for x in cl
            if is_future_match(x)
        ]

        print(
            "Man City sắp tới:"
        )

        for match in city_future[:5]:

            print(
                "-",
                match_title(match),
                "|",
                format_match_time(match)
            )

        print(
            "C1 sắp tới:"
        )

        for match in cl_future[:5]:

            print(
                "-",
                match_title(match),
                "|",
                format_match_time(match)
            )

    except Exception as e:

        print(
            "Football API startup error:",
            e
        )


# =========================================================
# START
# =========================================================

if not TOKEN:

    raise RuntimeError(
        "Thiếu DISCORD_TOKEN trên Railway."
    )

bot.run(TOKEN)

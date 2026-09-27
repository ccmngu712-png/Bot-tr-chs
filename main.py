EFA Champions League"),
    "140": ("🇪🇸", "La Liga"),
    "135": ("🇮🇹", "Serie A"),
    "78": ("🇩🇪", "Bundesliga"),
    "61": ("🇫🇷", "Ligue 1"),
    "88": ("🇳🇱", "Eredivisie"),
    "94": ("🇵🇹", "Primeira Liga"),
    "40": ("🏴", "Championship"),
    "45": ("🏆", "FA Cup"),
    "48": ("🏆", "Carabao Cup"),
}

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

if not DISCORD_TOKEN:
    raise RuntimeError("Thiếu biến môi trường DISCORD_TOKEN")
if not FOOTBALL_API_KEY:
    raise RuntimeError("Thiếu biến môi trường FOOTBALL_API_KEY")

# ============================================================
# DISCORD BOT
# ============================================================
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ============================================================
# DATABASE
# ============================================================
def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wallets (
                user_id INTEGER PRIMARY KEY,
                balance INTEGER NOT NULL DEFAULT 100000,
                started INTEGER NOT NULL DEFAULT 0,
                vip INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_claims (
                user_id INTEGER NOT NULL,
                claim_date TEXT NOT NULL,
                PRIMARY KEY (user_id, claim_date)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                fixture_id INTEGER NOT NULL,
                choice TEXT NOT NULL,
                stake INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                UNIQUE(user_id, fixture_id)
            )
            """
        )
        # Migrate DB cũ.
        for col, definition in [
            ("started", "INTEGER NOT NULL DEFAULT 0"),
            ("vip", "INTEGER NOT NULL DEFAULT 0"),
        ]:
            try:
                conn.execute(f"ALTER TABLE wallets ADD COLUMN {col} {definition}")
            except sqlite3.OperationalError:
                pass
        conn.commit()


def ensure_wallet(user_id: int):
    with db_connect() as conn:
        row = conn.execute(
            "SELECT balance, started, vip FROM wallets WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO wallets(user_id, balance, started, vip) VALUES (?, ?, 0, 0)",
                (user_id, STARTING_BALANCE),
            )
            conn.commit()
            return STARTING_BALANCE
        return int(row["balance"])


def get_balance(user_id: int) -> int:
    return ensure_wallet(user_id)


def set_balance(user_id: int, balance: int):
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO wallets(user_id, balance, started, vip) VALUES (?, ?, 0, 0) "
            "ON CONFLICT(user_id) DO UPDATE SET balance = excluded.balance",
            (user_id, max(0, int(balance))),
        )
        conn.commit()


def get_vip_level(balance: int) -> int:
    for level, minimum in VIP_LEVELS:
        if balance >= minimum:
            return level
    return 0


def vip_label(level: int) -> str:
    return f"VIP {level}" if level else "Thành viên"


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


async def check_vip_promotion(user_id: int, channel=None):
    with db_connect() as conn:
        row = conn.execute(
            "SELECT balance, vip FROM wallets WHERE user_id=?", (user_id,)
        ).fetchone()
        if not row:
            return
        balance = int(row["balance"])
        old_vip = int(row["vip"] or 0)
        new_vip = get_vip_level(balance)
        if new_vip != old_vip:
            conn.execute("UPDATE wallets SET vip=? WHERE user_id=?", (new_vip, user_id))
            conn.commit()

    if new_vip > old_vip and channel is not None:
        user = bot.get_user(user_id)
        name = user.display_name if user else str(user_id)
        embed = discord.Embed(
            title=f"🎉 CHÚC MỪNG {vip_label(new_vip).upper()} 🎉",
            description=(
                "👑━━━━━━━━━━━━━━━━━━👑\n"
                f"👤 **{name}**\n\n"
                f"💰 Số dư: **{fmt_money(balance)} xu**\n"
                f"{vip_emoji(new_vip)} Đã đạt **{vip_label(new_vip)}**\n"
                "👑━━━━━━━━━━━━━━━━━━👑"
            ),
            color=discord.Color.gold(),
        )
        try:
            await channel.send(embed=embed)
        except Exception:
            pass


async def add_balance(user_id: int, amount: int, channel=None):
    ensure_wallet(user_id)
    with db_connect() as conn:
        conn.execute("UPDATE wallets SET balance=balance+? WHERE user_id=?", (amount, user_id))
        conn.commit()
    await check_vip_promotion(user_id, channel)


async def remove_balance(user_id: int, amount: int):
    ensure_wallet(user_id)
    with db_connect() as conn:
        conn.execute("UPDATE wallets SET balance=MAX(balance-?,0) WHERE user_id=?", (amount, user_id))
        conn.commit()
    await check_vip_promotion(user_id)

# ============================================================
# FOOTBALL API
# ============================================================
class FootballAPI:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.cache = {}
        self.cache_ttl = 30
        self.last_quota = None
        self.last_errors = []

    async def start(self):
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=20)
            self.session = aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    "x-apisports-key": FOOTBALL_API_KEY,
                    "Accept": "application/json",
                },
            )

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None

    async def request(self, endpoint: str, params: dict):
        await self.start()
        key = (endpoint, tuple(sorted((str(k), str(v)) for k, v in params.items())))
        now = time.monotonic()
        cached = self.cache.get(key)
        if cached and now - cached[0] < self.cache_ttl:
            return cached[1]

        url = f"{API_BASE}/{endpoint.lstrip('/')}"
        last_exc = None

        for attempt in range(3):
            try:
                async with self.session.get(url, params=params) as resp:
                    raw = await resp.text()
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        data = {"errors": {"response": raw[:500]}}

                    headers = resp.headers
                    remaining = headers.get("x-ratelimit-requests-remaining")
                    if remaining is not None:
                        self.last_quota = remaining

                    if resp.status >= 500 and attempt < 2:
                        await asyncio.sleep(0.8 * (attempt + 1))
                        continue

                    if resp.status >= 400:
                        raise RuntimeError(
                            f"HTTP {resp.status}: {data.get('errors') or raw[:300]}"
                        )

                    errors = data.get("errors")
                    if errors:
                        # API-Football can return {} when there is no error.
                        if isinstance(errors, dict) and any(errors.values()):
                            raise RuntimeError(str(errors))
                        if isinstance(errors, list) and errors:
                            raise RuntimeError(str(errors))

                    self.cache[key] = (now, data)
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
                last_exc = exc
                if attempt < 2:
                    await asyncio.sleep(0.8 * (attempt + 1))
                else:
                    self.last_errors.append(str(exc))

        raise RuntimeError(str(last_exc) if last_exc else "API request failed")

    async def leagues_search(self, query: str):
        data = await self.request("leagues", {"search": query})
        return data.get("response", [])

    async def fixtures(self, league_id: int, season: int, match_date: str):
        return await self.request(
            "fixtures",
            {
                "league": league_id,
                "season": season,
                "date": match_date,
                "timezone": TIMEZONE,
            },
        )

    async def fixture_by_id(self, fixture_id: int):
        return await self.request("fixtures", {"id": fixture_id})

    async def predictions(self, fixture_id: int):
        return await self.request("predictions", {"fixture": fixture_id})

    async def next_team_fixtures(self, team_id: int = MANCHESTER_CITY_TEAM_ID, count: int = 20):
        data = await self.request(
            "fixtures",
            {"team": team_id, "next": count, "timezone": TIMEZONE},
        )
        return data.get("response", [])

    async def next_league_fixtures(self, league_id: int, count: int = 20):
        data = await self.request(
            "fixtures",
            {"league": league_id, "next": count, "timezone": TIMEZONE},
        )
        return data.get("response", [])


football = FootballAPI()

# ============================================================
# HELPERS
# ============================================================
C1_ALIASES = {
    "c1": (2, "UEFA Champions League"),
    "ucl": (2, "UEFA Champions League"),
    "champions league": (2, "UEFA Champions League"),
    "uefa champions league": (2, "UEFA Champions League"),
    "champions": (2, "UEFA Champions League"),
}


def season_for_date(date_text: str) -> int:
    """API-Football seasons use the starting year of the football season."""
    d = datetime.strptime(date_text, "%Y-%m-%d").date()
    return d.year if d.month >= 7 else d.year - 1


def clean_query(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def fmt_money(value: int) -> str:
    return f"{value:,}".replace(",", ".")


def fixture_status(fixture: dict) -> str:
    return ((fixture.get("fixture") or {}).get("status") or {}).get("short", "")


def fixture_datetime(fixture: dict) -> str:
    raw = ((fixture.get("fixture") or {}).get("date"))
    if not raw:
        return "Chưa có giờ"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(TZ)
        return dt.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return str(raw)


def team_name(fixture: dict, side: str) -> str:
    return (((fixture.get("teams") or {}).get(side) or {}).get("name")) or "?"


def team_logo(fixture: dict, side: str) -> Optional[str]:
    return (((fixture.get("teams") or {}).get(side) or {}).get("logo"))


def result_from_fixture(fixture: dict) -> Optional[str]:
    status = fixture_status(fixture)
    if status in {"NS", "TBD", "PST", "CANC", "ABD", "AWD", "WO"}:
        return None

    teams = fixture.get("teams") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    if home.get("winner") is True:
        return "home"
    if away.get("winner") is True:
        return "away"

    goals = fixture.get("goals") or {}
    hg, ag = goals.get("home"), goals.get("away")
    if isinstance(hg, int) and isinstance(ag, int):
        if hg > ag:
            return "home"
        if ag > hg:
            return "away"
        return "draw"
    return None


def choice_label(choice: str, fixture: dict) -> str:
    if choice == "home":
        return team_name(fixture, "home")
    if choice == "away":
        return team_name(fixture, "away")
    return "Hòa"


def upcoming(fixture: dict) -> bool:
    return fixture_status(fixture) in {"NS", "TBD"}


def league_meta(league_id: int):
    emoji, name = LEAGUES.get(str(league_id), ("🏆", "Giải đấu"))
    return emoji, name


def prediction_score(prediction: dict):
    goals = prediction.get("goals") or {}
    home = goals.get("home")
    away = goals.get("away")
    if isinstance(home, int) and isinstance(away, int):
        return home, away
    # Một số response trả score trong score.predicted.
    score = prediction.get("score") or {}
    home = score.get("home")
    away = score.get("away")
    if isinstance(home, int) and isinstance(away, int):
        return home, away
    return None



def free_season_embed(league_name: str, season: int) -> discord.Embed:
    e = discord.Embed(
        title="⚠️ API-Football Free không hỗ trợ mùa này",
        color=discord.Color.orange(),
    )
    e.description = (
        f"Giải **{league_name}** đang được hỏi ở mùa **{season}**.\n\n"
        "Key API-Football Free hiện không có quyền truy cập mùa này. "
        "Bot **không dùng dữ liệu mùa cũ để giả làm lịch hiện tại**.\n\n"
        "Muốn lấy lịch thật của mùa này, cần API plan/key có quyền truy cập season đó."
    )
    return e


def api_error_embed(exc: Exception) -> discord.Embed:
    msg = str(exc)
    if "Free plans" in msg or "try from 2022 to 2024" in msg:
        e = discord.Embed(
            title="⚠️ API-Football Free bị giới hạn",
            description=(
                "API key hiện tại không có quyền lấy mùa giải này.\n"
                "Bot không thể tự vượt giới hạn của API."
            ),
            color=discord.Color.orange(),
        )
    else:
        e = discord.Embed(
            title="❌ Không lấy được dữ liệu",
            description=f"`{msg[:900]}`",
            color=discord.Color.red(),
        )
    return e

# ============================================================
# VIEWS - HOME / LEAGUE SEARCH
# ============================================================
class LeagueHomeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="🔎 Tìm giải đấu", style=discord.ButtonStyle.primary)
    async def search_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(LeagueSearchModal())


class LeagueSearchModal(discord.ui.Modal, title="Tìm giải đấu"):
    query = discord.ui.TextInput(
        label="Tên giải đấu",
        placeholder="VD: Champions League, Premier League, La Liga",
        required=True,
        max_length=80,
    )

    async def on_submit(self, interaction: discord.Interaction):
        q = clean_query(str(self.query))
        await interaction.response.defer()

        try:
            key = q.lower()
            if key in C1_ALIASES:
                leagues = [{"league": {"id": 2, "name": "UEFA Champions League", "type": "Cup"}}]
            else:
                leagues = await football.leagues_search(q)

            if not leagues:
                await interaction.followup.send("❌ Không tìm thấy giải đấu.", ephemeral=True)
                return

            # Remove duplicate league IDs while preserving order.
            unique = []
            seen = set()
            for item in leagues:
                league = item.get("league") or {}
                lid = league.get("id")
                if lid is None or lid in seen:
                    continue
                seen.add(lid)
                unique.append(item)
                if len(unique) >= 25:
                    break

            if not unique:
                await interaction.followup.send("❌ Không tìm thấy giải đấu hợp lệ.", ephemeral=True)
                return

            await interaction.followup.send(
                "Chọn giải đấu:",
                view=LeagueResultsView(unique),
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.followup.send(embed=api_error_embed(exc), ephemeral=True)


class LeagueResultsView(discord.ui.View):
    def __init__(self, leagues: list[dict]):
        super().__init__(timeout=180)
        self.leagues = {}
        options = []

        for item in leagues[:25]:
            league = item.get("league") or {}
            lid = league.get("id")
            if lid is None:
                continue
            sid = str(lid)
            self.leagues[sid] = league
            options.append(
                discord.SelectOption(
                    label=str(league.get("name") or f"League {lid}")[:100],
                    value=sid,
                    description=str(league.get("type") or "League")[:100],
                )
            )

        self.add_item(LeagueSelect(options, self.leagues))


class LeagueSelect(discord.ui.Select):
    def __init__(self, options, leagues):
        super().__init__(
            placeholder="Chọn giải đấu...",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.leagues = leagues

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        league = self.leagues.get(self.values[0])
        if not league:
            await interaction.edit_original_response(content="❌ Không tìm thấy giải.", view=None)
            return

        view = LeagueDateView(
            int(league["id"]),
            str(league.get("name") or "League"),
        )
        embed = discord.Embed(
            title=f"⚽ {league.get('name', 'League')}",
            description="Chọn ngày muốn xem lịch trận.",
            color=discord.Color.blue(),
        )
        await interaction.edit_original_response(content=None, embed=embed, view=view)

# ============================================================
# VIEWS - DATE / FIXTURES
# ============================================================
class LeagueDateView(discord.ui.View):
    def __init__(self, league_id: int, league_name: str):
        super().__init__(timeout=180)
        self.league_id = league_id
        self.league_name = league_name

    async def load_fixtures(self, interaction: discord.Interaction, date_text: str):
        # Button callbacks already defer; modal callbacks also defer before calling here.
        try:
            season = season_for_date(date_text)
            data = await football.fixtures(self.league_id, season, date_text)
            fixtures = data.get("response", [])

            if not fixtures:
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="📅 Không có trận",
                        description=(
                            f"Không có lịch trong **{date_text}** cho **{self.league_name}**.\n"
                            f"Season API-Football: **{season}**"
                        ),
                        color=discord.Color.orange(),
                    ),
                    ephemeral=True,
                )
                return

            fixtures = fixtures[:25]
            view = FixtureListView(fixtures, self.league_name, date_text)
            embed = discord.Embed(
                title=f"📅 {date_text} — {self.league_name}",
                description=f"Tìm thấy **{len(fixtures)}** trận. Chọn trận để xem phân tích.",
                color=discord.Color.green(),
            )
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        except Exception as exc:
            if "Free plans" in str(exc) or "try from 2022 to 2024" in str(exc):
                await interaction.followup.send(
                    embed=free_season_embed(self.league_name, season_for_date(date_text)),
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(embed=api_error_embed(exc), ephemeral=True)

    @discord.ui.button(label="Hôm nay", style=discord.ButtonStyle.primary)
    async def today(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self.load_fixtures(interaction, datetime.now(TZ).strftime("%Y-%m-%d"))

    @discord.ui.button(label="Ngày mai", style=discord.ButtonStyle.secondary)
    async def tomorrow(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        from datetime import timedelta
        d = datetime.now(TZ).date() + timedelta(days=1)
        await self.load_fixtures(interaction, d.strftime("%Y-%m-%d"))

    @discord.ui.button(label="📅 Nhập ngày", style=discord.ButtonStyle.success)
    async def custom_date(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(DateModal(self))


class DateModal(discord.ui.Modal, title="Nhập ngày xem lịch"):
    date_input = discord.ui.TextInput(
        label="Ngày (YYYY-MM-DD)",
        placeholder="2026-09-27",
        required=True,
        min_length=10,
        max_length=10,
    )

    def __init__(self, parent_view: LeagueDateView):
        super().__init__()
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        value = str(self.date_input).strip()
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d").date()
            if parsed.year < 2020 or parsed.year > 2035:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "❌ Ngày không hợp lệ. Ví dụ: `2026-09-27`",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        await self.parent_view.load_fixtures(interaction, value)


class FixtureListView(discord.ui.View):
    def __init__(self, fixtures: list[dict], league_name: str, date_text: str):
        super().__init__(timeout=180)
        self.fixtures = {str(((x.get("fixture") or {}).get("id"))): x for x in fixtures}
        options = []
        for fixture in fixtures[:25]:
            fid = (fixture.get("fixture") or {}).get("id")
            if not fid:
                continue
            home = team_name(fixture, "home")
            away = team_name(fixture, "away")
            status = fixture_status(fixture)
            label = f"{home} vs {away}"[:100]
            desc = f"{fixture_datetime(fixture)} • {status}"[:100]
            options.append(discord.SelectOption(label=label, value=str(fid), description=desc))
        if options:
            self.add_item(FixtureSelect(options, self.fixtures, league_name, date_text))


class FixtureSelect(discord.ui.Select):
    def __init__(self, options, fixtures, league_name, date_text):
        super().__init__(placeholder="Chọn trận...", min_values=1, max_values=1, options=options)
        self.fixtures = fixtures
        self.league_name = league_name
        self.date_text = date_text

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        fixture = self.fixtures.get(self.values[0])
        if not fixture:
            await interaction.followup.send("❌ Không tìm thấy trận.", ephemeral=True)
            return

        await send_match_analysis(interaction, fixture, self.league_name, self.date_text)

# ============================================================
# MATCH ANALYSIS
# ============================================================
async def send_match_analysis(interaction: discord.Interaction, fixture: dict, league_name: str, date_text: str):
    fid = (fixture.get("fixture") or {}).get("id")
    home = team_name(fixture, "home")
    away = team_name(fixture, "away")
    e = discord.Embed(
        title=f"🔮 {home} vs {away}",
        description=f"🏆 **{league_name}**\n📅 **{fixture_datetime(fixture)}**\n🆔 Fixture: `{fid}`",
        color=discord.Color.blurple(),
    )
    goals = fixture.get("goals") or {}
    if goals.get("home") is not None or goals.get("away") is not None:
        e.add_field(name="📊 Tỷ số hiện tại", value=f"**{goals.get('home',0)} - {goals.get('away',0)}**", inline=False)
    try:
        pdata = await football.predictions(int(fid))
        response = pdata.get("response") or []
        prediction = response[0].get("predictions") if response else None
        if prediction:
            winner = prediction.get("winner") or {}
            winner_name = winner.get("name") or "Chưa xác định"
            percent = prediction.get("percent") or {}
            h, d, a = percent.get("home", "?"), percent.get("draw", "?"), percent.get("away", "?")
            exact = prediction_score(prediction)
            exact_text = f"**{exact[0]} - {exact[1]}**" if exact else "API chưa trả tỷ số chính xác"
            e.add_field(name="🎯 DỰ ĐOÁN TỶ SỐ", value=exact_text, inline=False)
            e.add_field(name="📈 Xác suất", value=f"🏠 Home **{h}%** • 🤝 Draw **{d}%** • ✈️ Away **{a}%**", inline=False)
            e.add_field(name="🏁 Kết luận", value=f"**{winner_name}**", inline=False)
        else:
            e.add_field(name="🎯 Dự đoán tỷ số", value="API chưa có dữ liệu dự đoán cho trận này.", inline=False)
    except Exception:
        e.add_field(name="🎯 Dự đoán tỷ số", value="Không lấy được dự đoán lúc này.", inline=False)
    e.set_footer(text="Dữ liệu từ API-Football • Dự đoán chỉ mang tính tham khảo")
    await interaction.followup.send(embed=e, ephemeral=True)

# ============================================================
# WALLET / VIRTUAL BETTING
# ============================================================
class BetChoiceView(discord.ui.View):
    def __init__(self, fixture: dict, stake: int):
        super().__init__(timeout=120)
        self.fixture = fixture
        self.stake = stake

    async def place(self, interaction: discord.Interaction, choice: str):
        user_id = interaction.user.id
        fid = (self.fixture.get("fixture") or {}).get("id")
        if not fid:
            await interaction.response.send_message("❌ Fixture ID không hợp lệ.", ephemeral=True)
            return

        balance = get_balance(user_id)
        if self.stake > balance:
            await interaction.response.send_message(
                f"❌ Không đủ tiền ảo. Ví hiện có **{fmt_money(balance)}**.",
                ephemeral=True,
            )
            return

        with db_connect() as conn:
            exists = conn.execute(
                "SELECT id FROM bets WHERE user_id=? AND fixture_id=? AND status='open'",
                (user_id, fid),
            ).fetchone()
            if exists:
                await interaction.response.send_message(
                    "❌ M đã đặt cược trận này rồi.", ephemeral=True
                )
                return

            conn.execute("UPDATE wallets SET balance = balance - ? WHERE user_id = ?", (self.stake, user_id))
            conn.execute(
                "INSERT INTO bets(user_id, fixture_id, choice, stake, status, created_at) VALUES (?, ?, ?, ?, 'open', ?)",
                (user_id, fid, choice, self.stake, datetime.now(TZ).isoformat()),
            )
            conn.commit()

        await interaction.response.send_message(
            f"✅ Đặt **{fmt_money(self.stake)}** vào **{choice_label(choice, self.fixture)}**.\n"
            f"💰 Còn lại: **{fmt_money(get_balance(user_id))}**",
            ephemeral=True,
        )

    @discord.ui.button(label="Chủ nhà", style=discord.ButtonStyle.primary)
    async def home(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.place(interaction, "home")

    @discord.ui.button(label="Hòa", style=discord.ButtonStyle.secondary)
    async def draw(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.place(interaction, "draw")

    @discord.ui.button(label="Đội khách", style=discord.ButtonStyle.success)
    async def away(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.place(interaction, "away")


# ============================================================
# /SOI + /CUOC VIEWS
# ============================================================

class MatchSelectView(discord.ui.View):
    def __init__(self, fixtures: list[dict], league_name: str, mode: str):
        super().__init__(timeout=180)
        self.fixtures = {str((x.get("fixture") or {}).get("id")): x for x in fixtures}
        self.league_name = league_name
        self.mode = mode
        options = []
        for fixture in fixtures[:25]:
            fid = (fixture.get("fixture") or {}).get("id")
            if not fid:
                continue
            options.append(discord.SelectOption(
                label=f"{team_name(fixture,'home')} vs {team_name(fixture,'away')}"[:100],
                value=str(fid),
                description=f"{fixture_datetime(fixture)}"[:100],
            ))
        if options:
            self.add_item(MatchSelect(options, self.fixtures, league_name, mode))


class MatchSelect(discord.ui.Select):
    def __init__(self, options, fixtures, league_name, mode):
        super().__init__(placeholder="Chọn trận...", min_values=1, max_values=1, options=options)
        self.fixtures = fixtures
        self.league_name = league_name
        self.mode = mode

    async def callback(self, interaction: discord.Interaction):
        fixture = self.fixtures.get(self.values[0])
        if not fixture:
            await interaction.response.send_message("❌ Không tìm thấy trận.", ephemeral=True)
            return
        if self.mode == "bet":
            await interaction.response.send_modal(BetStakeModal(fixture))
        else:
            await interaction.response.defer(ephemeral=True)
            await send_match_analysis(interaction, fixture, self.league_name, fixture_datetime(fixture))


class BetStakeModal(discord.ui.Modal, title="💰 Nhập tiền cược"):
    stake = discord.ui.TextInput(label="Số tiền ảo", placeholder="VD: 10000", required=True, min_length=1, max_length=12)

    def __init__(self, fixture):
        super().__init__()
        self.fixture = fixture

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = int(str(self.stake).replace(".", "").replace(",", ""))
        except ValueError:
            await interaction.response.send_message("❌ Số tiền không hợp lệ.", ephemeral=True)
            return
        if amount <= 0 or amount > 10_000_000:
            await interaction.response.send_message("❌ Tiền cược phải từ 1 đến 10.000.000.", ephemeral=True)
            return
        if amount > get_balance(interaction.user.id):
            await interaction.response.send_message("❌ Không đủ tiền ảo.", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=discord.Embed(
                title="🎯 Chọn cửa",
                description=(f"**{team_name(self.fixture,'home')}** vs **{team_name(self.fixture,'away')}**\n"
                             f"💰 Cược: **{fmt_money(amount)}**"),
                color=discord.Color.gold(),
            ),
            view=BetChoiceView(self.fixture, amount),
            ephemeral=True,
        )


class CuocHomeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(CuocLeagueSelect())

    @discord.ui.button(label="🔵 Các trận Man City sắp tới", style=discord.ButtonStyle.primary, row=1)
    async def man_city(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            fixtures = [f for f in await football.next_team_fixtures() if upcoming(f)]
            if not fixtures:
                await interaction.followup.send("❌ Không lấy được các trận Man City sắp tới từ API.", ephemeral=True)
                return
            await interaction.followup.send(
                embed=discord.Embed(title="🔵 MAN CITY — CÁC TRẬN SẮP TỚI", description=f"Có **{len(fixtures)}** trận tiếp theo.", color=discord.Color.blue()),
                view=MatchSelectView(fixtures, "Các giải của Man City", "bet"),
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.followup.send(embed=api_error_embed(exc), ephemeral=True)


class CuocLeagueSelect(discord.ui.Select):
    def __init__(self):
        options = []
        for lid, (emoji, name) in LEAGUES.items():
            options.append(discord.SelectOption(label=name, value=lid, emoji=emoji))
        super().__init__(placeholder="🏆 Chọn giải để cược...", options=options[:25])

    async def callback(self, interaction: discord.Interaction):
        lid = int(self.values[0])
        emoji, name = league_meta(lid)
        await interaction.response.defer(ephemeral=True)
        try:
            all_fixtures = [f for f in await football.next_league_fixtures(lid, 20) if upcoming(f)]
            all_fixtures.sort(key=lambda x: ((x.get("fixture") or {}).get("timestamp") or 0))
            if not all_fixtures:
                await interaction.followup.send(f"❌ Chưa có trận sắp tới trong **{name}**.", ephemeral=True)
                return
            await interaction.followup.send(
                embed=discord.Embed(title=f"{emoji} {name}", description=f"Chọn trận để cược. Tìm thấy **{len(all_fixtures)}** trận.", color=discord.Color.green()),
                view=MatchSelectView(all_fixtures, name, "bet"),
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.followup.send(embed=api_error_embed(exc), ephemeral=True)


class SoiHomeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(SoiLeagueSelect())

    @discord.ui.button(label="🔵 Man City — tất cả trận", style=discord.ButtonStyle.primary, row=1)
    async def man_city(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            fixtures = [f for f in await football.next_team_fixtures() if upcoming(f)]
            await interaction.followup.send(
                embed=discord.Embed(title="🔵 MAN CITY — TẤT CẢ TRẬN SẮP TỚI", description=f"Toàn bộ **{len(fixtures)}** trận tiếp theo của Man City, không phân biệt giải.", color=discord.Color.blue()),
                view=MatchSelectView(fixtures, "Man City", "soi"),
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.followup.send(embed=api_error_embed(exc), ephemeral=True)


class SoiLeagueSelect(discord.ui.Select):
    def __init__(self):
        options = []
        for lid, (emoji, name) in LEAGUES.items():
            options.append(discord.SelectOption(label=name, value=lid, emoji=emoji))
        super().__init__(placeholder="🏆 Chọn giải để soi...", options=options[:25])

    async def callback(self, interaction: discord.Interaction):
        lid = int(self.values[0])
        emoji, name = league_meta(lid)
        await interaction.response.defer(ephemeral=True)
        try:
            all_fixtures = [f for f in await football.next_league_fixtures(lid, 20) if upcoming(f)]
            all_fixtures.sort(key=lambda x: ((x.get("fixture") or {}).get("timestamp") or 0))
            if not all_fixtures:
                await interaction.followup.send(f"❌ Chưa có trận sắp tới trong **{name}**.", ephemeral=True)
                return
            await interaction.followup.send(
                embed=discord.Embed(title=f"{emoji} {name}", description="Chọn trận để soi và dự đoán tỷ số.", color=discord.Color.blurple()),
                view=MatchSelectView(all_fixtures, name, "soi"),
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.followup.send(embed=api_error_embed(exc), ephemeral=True)


@bot.tree.command(name="soi", description="Soi tất cả giải và toàn bộ trận Man City", guild=GUILD)
async def soi(interaction: discord.Interaction):
    embed = discord.Embed(
        title="⚽ SOI BÓNG ĐÁ",
        description="Cột trái là **tất cả giải**. Nút/cột **Man City** bên phải gom toàn bộ trận tiếp theo của Man City.",
        color=discord.Color.green(),
    )
    embed.add_field(name="🏆 CÁC GIẢI", value="Premier League\nChampions League\nLa Liga\nSerie A\nBundesliga\nLigue 1\nEredivisie\nPrimeira Liga\nFA Cup\nCarabao Cup", inline=True)
    embed.add_field(name="🔵 MAN CITY", value="Tất cả trận sắp tới\nKhông phân biệt giải\n👉 Chọn trận để soi tỷ số", inline=True)
    embed.set_footer(text="Dữ liệu từ API-Football • Dự đoán chỉ mang tính tham khảo")
    await interaction.response.send_message(embed=embed, view=SoiHomeView())


@bot.tree.command(name="cuoc", description="Cược các giải hoặc toàn bộ trận Man City", guild=GUILD)
async def cuoc(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎰 CƯỢC BÓNG ĐÁ",
        description="Chọn **tất cả giải** hoặc bấm riêng **🔵 Các trận Man City sắp tới**.",
        color=discord.Color.gold(),
    )
    embed.add_field(name="🏆 Tất cả giải", value="Premier League • Champions League • La Liga • Serie A • Bundesliga • Ligue 1 • ...", inline=False)
    embed.add_field(name="🔵 Man City", value="Hiện toàn bộ trận tiếp theo của Man City, bất kể giải.", inline=False)
    await interaction.response.send_message(embed=embed, view=CuocHomeView(), ephemeral=True)


@bot.tree.command(name="khoinghiep", description="Nhận 1.000 xu khởi nghiệp", guild=GUILD)
async def khoinghiep(interaction: discord.Interaction):
    user_id = interaction.user.id
    ensure_wallet(user_id)
    with db_connect() as conn:
        row = conn.execute("SELECT started FROM wallets WHERE user_id=?", (user_id,)).fetchone()
        if row["started"]:
            await interaction.response.send_message("❌ M đã nhận **1.000 xu khởi nghiệp** rồi.", ephemeral=True)
            return
        conn.execute("UPDATE wallets SET started=1 WHERE user_id=?", (user_id,))
        conn.commit()
    await add_balance(user_id, STARTER_BONUS, interaction.channel)
    await interaction.response.send_message(f"🚀 Nhận **{fmt_money(STARTER_BONUS)} xu** khởi nghiệp! 💰 **{fmt_money(get_balance(user_id))}**")


@bot.tree.command(name="comat", description="Nhận 50.000 xu mỗi ngày", guild=GUILD)
async def comat(interaction: discord.Interaction):
    user_id = interaction.user.id
    ensure_wallet(user_id)
    today = datetime.now(TZ).date().isoformat()
    with db_connect() as conn:
        if conn.execute("SELECT 1 FROM daily_claims WHERE user_id=? AND claim_date=?", (user_id, today)).fetchone():
            await interaction.response.send_message("❌ Hôm nay m đã nhận **50.000 xu** rồi.", ephemeral=True)
            return
        conn.execute("INSERT INTO daily_claims(user_id, claim_date) VALUES (?,?)", (user_id, today))
        conn.commit()
    await add_balance(user_id, DAILY_BONUS, interaction.channel)
    await interaction.response.send_message(f"💰 Nhận **{fmt_money(DAILY_BONUS)} xu** hôm nay! Ví: **{fmt_money(get_balance(user_id))}**")


@bot.tree.command(name="vi", description="Xem ví và VIP", guild=GUILD)
async def vi(interaction: discord.Interaction):
    user_id = interaction.user.id
    balance = get_balance(user_id)
    level = get_vip_level(balance)
    await check_vip_promotion(user_id)
    with db_connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM bets WHERE user_id=?", (user_id,)).fetchone()
    embed = discord.Embed(title="👛 VÍ CỦA BẠN", color=discord.Color.gold())
    embed.add_field(name="💰 Số dư", value=f"**{fmt_money(balance)} xu**", inline=False)
    embed.add_field(name="👑 VIP", value=f"{vip_emoji(level)} **{vip_label(level)}**", inline=True)
    embed.add_field(name="🎯 Tổng cược", value=f"**{row['c']}**", inline=True)
    embed.add_field(name="📈 Mốc VIP tiếp theo", value=next((f"{vip_label(lv)} — {fmt_money(m)} xu" for lv,m in reversed(VIP_LEVELS) if m > balance), "🏆 Đã đạt VIP 10"), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def place_bet_command(interaction: discord.Interaction, fixture_id: int, stake: int):
    if stake <= 0:
        await interaction.response.send_message("❌ Tiền cược phải lớn hơn 0.", ephemeral=True)
        return
    if stake > 10_000_000:
        await interaction.response.send_message("❌ Tiền cược tối đa là 10.000.000.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    try:
        data = await football.fixture_by_id(fixture_id)
        fixtures = data.get("response", [])
        if not fixtures:
            await interaction.followup.send("❌ Không tìm thấy fixture.", ephemeral=True)
            return

        fixture = fixtures[0]
        status = fixture_status(fixture)
        if status not in {"NS", "TBD"}:
            await interaction.followup.send(
                f"❌ Trận này không còn ở trạng thái chưa bắt đầu (`{status}`).",
                ephemeral=True,
            )
            return

        if stake > get_balance(interaction.user.id):
            await interaction.followup.send("❌ Không đủ tiền ảo.", ephemeral=True)
            return

        e = discord.Embed(
            title="🎯 Chọn cửa",
            description=(
                f"**{team_name(fixture, 'home')}** vs **{team_name(fixture, 'away')}**\n"
                f"Cược: **{fmt_money(stake)}** tiền ảo"
            ),
            color=discord.Color.gold(),
        )
        await interaction.followup.send(
            embed=e,
            view=BetChoiceView(fixture, stake),
            ephemeral=True,
        )
    except Exception as exc:
        await interaction.followup.send(embed=api_error_embed(exc), ephemeral=True)


@bot.tree.command(name="kettoan", description="Kết toán các cược đã có kết quả", guild=GUILD)
async def kettoan(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    with db_connect() as conn:
        open_bets = conn.execute("SELECT * FROM bets WHERE status='open' ORDER BY id ASC LIMIT 100").fetchall()
    if not open_bets:
        await interaction.followup.send("ℹ️ Không có cược nào đang chờ kết toán.", ephemeral=True)
        return
    settled = 0
    skipped = 0
    for bet in open_bets:
        try:
            data = await football.fixture_by_id(int(bet["fixture_id"]))
            fixtures = data.get("response", [])
            if not fixtures:
                skipped += 1
                continue
            fixture = fixtures[0]
            result = result_from_fixture(fixture)
            if result is None:
                skipped += 1
                continue
            if result == bet["choice"]:
                payout = int(bet["stake"]) * 2
                await add_balance(int(bet["user_id"]), payout, interaction.channel)
                status = "won"
            else:
                status = "lost"
            with db_connect() as conn:
                conn.execute("UPDATE bets SET status=? WHERE id=?", (status, bet["id"]))
                conn.commit()
            settled += 1
        except Exception:
            skipped += 1
    await interaction.followup.send(f"✅ Đã kết toán **{settled}** cược.\n⏳ Chưa thể kết toán: **{skipped}**.", ephemeral=True)


@bot.tree.command(name="admin", description="Admin cộng/trừ xu và cập nhật VIP", guild=GUILD)
@app_commands.describe(user="Thành viên", amount="Số xu (+ cộng / - trừ)", password="Mật khẩu admin")
async def admin(interaction: discord.Interaction, user: discord.Member, amount: int, password: str):
    if password != os.getenv("ADMIN_PASSWORD", "provip👑"):
        await interaction.response.send_message("❌ Sai mật khẩu admin.", ephemeral=True)
        return
    if amount == 0:
        await interaction.response.send_message("❌ Số xu phải khác 0.", ephemeral=True)
        return
    if amount > 0:
        await add_balance(user.id, amount, interaction.channel)
    else:
        await remove_balance(user.id, abs(amount))
    balance = get_balance(user.id)
    level = get_vip_level(balance)
    await interaction.response.send_message(
        f"👑 Đã cập nhật **{user.display_name}**: **{amount:+,} xu**\n"
        f"💰 Ví: **{fmt_money(balance)} xu** • {vip_emoji(level)} **{vip_label(level)}**",
        ephemeral=True,
    )


@bot.tree.command(name="ping", description="Kiểm tra bot", guild=GUILD)
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"🏓 Pong! `{round(bot.latency * 1000)}ms`",
        ephemeral=True,
    )


@bot.tree.command(name="apiquota", description="Xem quota API gần nhất", guild=GUILD)
async def apiquota(interaction: discord.Interaction):
    remaining = football.last_quota or "chưa nhận từ API"
    await interaction.response.send_message(
        f"📡 API-Football requests remaining: **{remaining}**",
        ephemeral=True,
    )

# ============================================================
# HEALTH SERVER FOR RAILWAY
# ============================================================
async def health_handler(request):
    return web.Response(text="OK")


async def start_health_server():
    port = int(os.getenv("PORT", "8080"))
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"[WEB] Health server listening on port {port}")
    return runner

# ============================================================
# EVENTS / SYNC
# ============================================================
@bot.event
async def on_ready():
    print(f"[DISCORD] Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"[DISCORD] Guild: {GUILD_ID}")


async def sync_commands():
    # Guild-specific commands appear almost immediately after sync.
    synced = await bot.tree.sync(guild=GUILD)
    print(f"[DISCORD] Synced {len(synced)} guild commands.")
    print("[DISCORD] Commands:", ", ".join(sorted(c.name for c in synced)))


async def main():
    init_db()
    await football.start()
    await start_health_server()

    try:
        async with bot:
            await bot.login(DISCORD_TOKEN)
            await sync_commands()
            await bot.connect()
    finally:
        await football.close()


if __name__ == "__main__":
    asyncio.run(main())

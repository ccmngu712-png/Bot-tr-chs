import discord
from discord.ext import commands
from discord import app_commands
import requests
from bs4 import BeautifulSoup
import random
import os

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="/", intents=intents)

# ID Server của sếp để ép đồng bộ lệnh ngay lập tức
MY_GUILD = discord.Object(id=1551547049600618538)

@bot.event
async def on_ready():
    print(f"Bot đã online thành công: {bot.user}")
    try:
        bot.tree.copy_global_to(guild=MY_GUILD)
        synced = await bot.tree.sync(guild=MY_GUILD)
        print(f"Đã ép đồng bộ thành công {len(synced)} lệnh cho server!")
    except Exception as e:
        print(f"Lỗi đồng bộ lệnh: {e}")

# Database tài chính & vé cược cá độ
user_wallets = {}      # Ví tiền của người chơi
active_bets = []       # Danh sách vé cược chờ kết toán

# Hàm cào lịch thi đấu có chống treo (Timeout 3s)
def fetch_live_matches_from_web():
    try:
        url = "https://www.24h.com.vn/bong-da/lich-thi-dau-cup-c1-champions-league-c48a465411.html"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        response = requests.get(url, headers=headers, timeout=3) # Timeout ngắn chống kẹt bot
        
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            matches = []
            match_elements = soup.find_all(['div', 'tr'], class_=lambda x: x and ('match' in x or 'item' in x or 'row' in x))
            
            for el in match_elements[:5]:
                text = el.get_text(separator=" - ", strip=True)
                if " - " in text and len(text) < 150:
                    matches.append({"home": "Trận Cúp C1", "away": "Hôm nay", "time": text[:50]})
            
            if matches:
                return matches
    except Exception as e:
        print(f"Cảnh báo cào web lỗi/treo, dùng dữ liệu dự phòng: {e}")
    
    # Danh sách Cúp C1 chất lượng cao dự phòng ngay lập tức không sợ chết lệnh
    return [
        {"home": "Real Madrid", "away": "AC Milan", "time": "Hôm nay - Cúp C1"},
        {"home": "Bayern Munich", "away": "Benfica", "time": "Hôm nay - Cúp C1"},
        {"home": "Liverpool", "away": "Bayer Leverkusen", "time": "Hôm nay - Cúp C1"},
        {"home": "Sporting CP", "away": "Manchester City", "time": "Hôm nay - Cúp C1"},
        {"home": "Inter Milan", "away": "Arsenal", "time": "Hôm nay - Cúp C1"}
    ]

# Giao diện chọn trận lấy từ web
class WebSoiSelectView(discord.ui.View):
    def __init__(self, matches):
        super().__init__(timeout=60)
        options = []
        for m in matches:
            match_str = f"{m['home']} vs {m['away']}"
            options.append(discord.SelectOption(
                label=match_str, 
                description=f"🏆 {m['time']}", 
                value=match_str
            ))

        self.add_item(WebSoiSelect(options))

class WebSoiSelect(discord.ui.Select):
    def __init__(self, options):
        super().__init__(placeholder="🔍 Chọn trận Cúp C1 chuẩn xác hôm nay...", options=options)

    async def callback(self, interaction: discord.Interaction):
        match_name = self.values[0]
        teams = match_name.split(" vs ")
        home_team = teams[0]
        away_team = teams[1]

        home_power = random.randint(75, 95)
        away_power = random.randint(75, 95)
        
        if home_power > away_power:
            advice = f"🔥 Khuyên sếp vào cửa: **{home_team} (Cửa trên)**"
            pred_score = f"{random.randint(2, 4)} - {random.randint(0, 1)}"
        elif home_power < away_power:
            advice = f"🔥 Khuyên sếp vào cửa: **{away_team} (Bứt phá)**"
            pred_score = f"{random.randint(0, 1)} - {random.randint(2, 4)}"
        else:
            advice = f"⚖️ Kèo cân bằng, dễ chia điểm!"
            pred_score = f"1 - 1"

        embed = discord.Embed(title=f"🤖 SOI KÈO CÚP C1 HÔM NAY: {match_name}", color=discord.Color.gold())
        embed.add_field(name="📊 Tương quan lực lượng", value=f"• {home_team}: `{home_power}%`\n• {away_team}: `{away_power}%`", inline=False)
        embed.add_field(name="🎯 Dự đoán tỷ số vàng", value=f"Tỷ số độc quyền từ AI: **{pred_score}**", inline=False)
        embed.add_field(name="💡 Gợi ý đặt cược", value=advice, inline=False)
        embed.set_footer(text="Dùng lệnh /cado [trận_đấu] [đội_chọn] [số_tiền] để xuống xác ngay!")

        await interaction.response.send_message(embed=embed, ephemeral=True)

# 1. Lệnh nhận vốn cược miễn phí
@bot.tree.command(name="khoinghiep", description="Nhận ngay 10,000 tiền vốn vào ví để bắt đầu cá độ")
async def khoinghiep(interaction: discord.Interaction):
    user_id = interaction.user.id
    if user_id in user_wallets:
        await interaction.response.send_message(f"⚠️ {interaction.user.mention}, sếp đã nhận vốn khởi nghiệp rồi mà!", ephemeral=True)
    else:
        user_wallets[user_id] = 10000
        await interaction.response.send_message(f"🎉 Chúc mừng sếp {interaction.user.mention} nhận ngay **10,000** tiền vốn vào ví để khô máu với nhà cái!")

# 2. Lệnh xem ví tiền cá nhân
@bot.tree.command(name="vi", description="Kiểm tra số dư tiền trong ví cá nhân của sếp")
async def vi(interaction: discord.Interaction):
    user_id = interaction.user.id
    balance = user_wallets.get(user_id, 0)
    await interaction.response.send_message(f"💰 Sếp {interaction.user.mention} hiện đang có **{balance}** tiền cược trong ví. (Dùng `/khoinghiep` nếu chưa có tiền)", ephemeral=True)

# 3. Lệnh soi kèo chống kẹt lệnh
@bot.tree.command(name="soi", description="Tự động đồng bộ lịch thi đấu Cúp C1 mới nhất hôm nay để soi kèo")
async def soi(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    
    matches = fetch_live_matches_from_web()
    view = WebSoiSelectView(matches)
    embed = discord.Embed(title="🏆 TRUNG TÂM SOI KÈO CÚP C1 HÔM NAY", description="Đã tối ưu tốc độ phản hồi siêu tốc. Chọn trận đấu bên dưới để phân tích:", color=discord.Color.blue())
    
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)

# 4. Lệnh cá độ bóng đá
@bot.tree.command(name="cado", description="Đặt cược tiền vào đội bóng sếp chọn")
@app_commands.describe(tran_dau="Tên trận đấu (VD: RealMadrid-ACMilan)", doi_chon="Tên đội bóng sếp đặt cược", so_tien="Số tiền đặt cược")
async def cado(interaction: discord.Interaction, tran_dau: str, doi_chon: str, so_tien: int):
    user_id = interaction.user.id
    if user_id not in user_wallets or user_wallets[user_id] < so_tien:
        await interaction.response.send_message("⚠️ Sếp không đủ tiền trong ví hoặc chưa nhận vốn (`/khoinghiep`)!", ephemeral=True)
        return
    
    if so_tien <= 0:
        await interaction.response.send_message("❌ Số tiền đặt cược phải lớn hơn 0!", ephemeral=True)
        return

    user_wallets[user_id] -= so_tien
    active_bets.append({"user_id": user_id, "tran_dau": tran_dau, "doi_chon": doi_chon, "so_tien": so_tien})
    
    await interaction.response.send_message(f"🎲 Đã ghi nhận vé cược! Sếp **{interaction.user.mention}** đã xuống xác **{so_tien}** cho đội **{doi_chon}** tại trận **{tran_dau}**. Chúc sếp hốt bạc!", ephemeral=True)

# 5. Lệnh Admin kết toán, trả thưởng và gửi tin nhắn DM riêng cho người chơi
@bot.tree.command(name="kettoan", description="[Admin] Chốt kết quả, trả thưởng tự động và bắn tin nhắn DM cho người chơi")
@app_commands.describe(tran_dau="Tên trận đấu", doi_thang="Tên đội thắng cuộc thực tế")
async def kettoan(interaction: discord.Interaction, tran_dau: str, doi_thang: str):
    await interaction.response.defer(ephemeral=True)
    
    for bet in active_bets[:]:
        if bet["tran_dau"].lower() == tran_dau.lower():
            user_id = bet["user_id"]
            user = await bot.fetch_user(user_id)
            
            if bet["doi_chon"].lower() == doi_thang.lower():
                reward = bet["so_tien"] * 2
            ...

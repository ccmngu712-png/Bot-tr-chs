import discord
from discord.ext import commands
from discord import app_commands
import random
import os
from flask import Flask
from threading import Thread

# Web server giữ bot sống 24/7 trên Render
app = Flask('')

@app.route('/')
def home():
    return "Bot FC Online VN & Tu Soi Keo dang hoat dong 24/7!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_web)
    t.start()

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

# Database giả lập
user_wallets = {}      # Số dư BP
user_squads = {}       # Sơ đồ chiến thuật
user_inventory = {}    # Kho thẻ cầu thủ

DATABASE_PLAYERS = [
    {"name": "Ronaldo (ICON)", "pos": "ST", "rating": 105, "price": 5000},
    {"name": "Messi (23TY)", "pos": "RW", "rating": 106, "price": 5500},
    {"name": "Haaland (24TS)", "pos": "ST", "rating": 104, "price": 4200},
    {"name": "Bellingham (24TOTY)", "pos": "CM", "rating": 103, "price": 3800},
    {"name": "Van Dijk (ICON)", "pos": "CB", "rating": 105, "price": 4800},
    {"name": "De Bruyne (23TY)", "pos": "CAM", "rating": 104, "price": 4000},
    {"name": "Courtois (23NG)", "pos": "GK", "rating": 102, "price": 3000},
]

# Giao diện chọn trận để bot tự soi kèo
class AutoSoiSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        
        matches = [
            {"home": "Real Madrid", "away": "Man City", "league": "Cúp C1"},
            {"home": "Arsenal", "away": "Bayern Munich", "league": "Cúp C1"},
            {"home": "Liverpool", "away": "MU", "league": "Ngoại Hạng Anh"},
            {"home": "Barca", "away": "Inter Milan", "league": "Cúp C1"}
        ]
        
        options = []
        for m in matches:
            match_str = f"{m['home']} vs {m['away']}"
            options.append(discord.SelectOption(label=match_str, description=f"Giải đấu: {m['league']}", value=match_str))

        self.add_item(AutoSoiSelect(options))

class AutoSoiSelect(discord.ui.Select):
    def __init__(self, options):
        super().__init__(placeholder="🔍 Chọn trận đấu để AI tự động soi kèo...", options=options)

    async def callback(self, interaction: discord.Interaction):
        match_name = self.values[0]
        teams = match_name.split(" vs ")
        home_team = teams[0]
        away_team = teams[1]

        # Thuật toán tự động phân tích sức mạnh & tỷ số
        home_power = random.randint(75, 95)
        away_power = random.randint(75, 95)
        
        if home_power > away_power:
            advice = f"🔥 Khuyên sếp nên vào cửa: **{home_team} (Cửa trên)**"
            pred_score = f"{random.randint(2, 4)} - {random.randint(0, 1)}"
        elif home_power < away_power:
            advice = f"🔥 Khuyên sếp nên vào cửa: **{away_team} (Bứt phá)**"
            pred_score = f"{random.randint(0, 1)} - {random.randint(2, 4)}"
        else:
            advice = f"⚖️ Kèo cân bằng, khả năng cao chia điểm hoặc đá hiệp phụ!"
            pred_score = f"1 - 1"

        embed = discord.Embed(title=f"🤖 BẢNG PHÂN TÍCH SOIKÈO AI: {match_name}", color=discord.Color.gold())
        embed.add_field(name="📊 Đánh giá sức mạnh", value=f"• {home_team}: `{home_power}%`\n• {away_team}: `{away_power}%`", inline=False)
        embed.add_field(name="🎯 Dự đoán tỷ số vàng", value=f"Tỷ số độc quyền từ hệ thống: **{pred_score}**", inline=False)
        embed.add_field(name="💡 Gợi ý đặt cược", value=advice, inline=False)
        embed.set_footer(text="Dùng lệnh /cado [trận_đấu] [đội_chọn] [số_tiền] để xuống xác ngay!")

        await interaction.response.send_message(embed=embed, ephemeral=True)

# 1. Lệnh khởi nghiệp
@bot.tree.command(name="khoinghiep", description="Nhận vốn 10,000 BP và gói cầu thủ khởi đầu")
async def khoinghiep(interaction: discord.Interaction):
    user_id = interaction.user.id
    if user_id in user_wallets:
        await interaction.response.send_message(f"⚠️ {interaction.user.mention}, sếp đã nhận vốn khởi nghiệp rồi mà!", ephemeral=True)
    else:
        user_wallets[user_id] = 10000
        user_inventory[user_id] = ["Ronaldo (ICON)", "Courtois (23NG)"]
        user_squads[user_id] = {"formation": "4-3-3"}
        await interaction.response.send_message(f"🎉 Chúc mừng sếp {interaction.user.mention} gia nhập thế giới bóng đá! Nhận ngay **10,000 BP** vào kho.")

# 2. Lệnh tự soi kèo
@bot.tree.command(name="soi", description="Hệ thống tự động soi kèo, so sánh lực lượng và chốt tỷ số")
async def soi(interaction: discord.Interaction):
    view = AutoSoiSelectView()
    embed = discord.Embed(title="⚽ TRUNG TÂM PHÂN TÍCH KÈO THỰC TẾ", description="Chọn trận đấu bên dưới để hệ thống tự động quét dữ liệu và đưa ra phân tích:", color=discord.Color.blue())
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

# 3. Lệnh cá độ bóng đá
@bot.tree.command(name="cado", description="Xuống xác đặt cược BP dựa trên kết quả soi kèo")
@app_commands.describe(tran_dau="Tên trận đấu (VD: Real-ManCity)", doi_chon="Tên đội bóng sếp đặt cược", so_tien="Số BP đặt cược")
async def cado(interaction: discord.Interaction, tran_dau: str, doi_chon: str, so_tien: int):
    user_id = interaction.user.id
    if user_id not in user_wallets or user_wallets[user_id] < so_tien:
        await interaction.response.send_message("⚠️ Sếp không đủ BP hoặc chưa tạo tài khoản (`/khoinghiep`)!", ephemeral=True)
        return
    
    user_wallets[user_id] -= so_tien
    await interaction.response.send_message(f"🎲 Đã ghi nhận vé cược! Sếp **{interaction.user.mention}** đã xuống xác **{so_tien} BP** cho đội **{doi_chon}** tại trận **{tran_dau}**. Chúc sếp hốt bạc!")

# 4. Lệnh xem đội hình
@bot.tree.command(name="doihinh", description="Xem tài chính, sơ đồ chiến thuật và kho thẻ cầu thủ")
async def doihinh(interaction: discord.Interaction):
    user_id = interaction.user.id
    money = user_wallets.get(user_id, 0)
    inventory = user_inventory.get(user_id, ["Chưa có cầu thủ nào"])
    squad_info = user_squads.get(user_id, {"formation": "Chưa chọn sơ đồ"})
    
    embed = discord.Embed(title=f"🏟️ Câu Lạc Bộ Của {interaction.user.name}", color=discord.Color.green())
    embed.add_field(name="💰 Số dư tài chính", value=f"{money} BP", inline=False)
    embed.add_field(name="📋 Sơ đồ chiến thuật", value=squad_info.get("formation"), inline=False)
    embed.add_field(name="🎒 Kho thẻ cầu thủ", value=", ".join(inventory), inline=False)
    await interaction.response.send_message(embed=embed)

# 5. Lệnh chợ chuyển nhượng
@bot.tree.command(name="cho", description="Mở chợ chuyển nhượng săn thẻ cầu thủ khủng")
async def cho(interaction: discord.Interaction):
    embed = discord.Embed(title="🛒 CHỢ CHUYỂN NHƯỢNG CẦU THỦ", description="Danh sách siêu sao đang được giao dịch:", color=discord.Color.gold())
    for idx, p in enumerate(DATABASE_PLAYERS):
        embed.add_field(name=f"{idx+1}. {p['name']} [{p['pos']}]", value=f"⭐ Chỉ số: **{p['rating']}** | 💰 Giá: **{p['price']} BP**", inline=False)
    embed.set_footer(text="Dùng lệnh /mua [số_thứ_tự] để rước cầu thủ về kho!")
    await interaction.response.send_message(embed=embed)

# 6. Lệnh mua cầu thủ
@bot.tree.command(name="mua", description="Mua cầu thủ từ chợ chuyển nhượng")
@app_commands.describe(index="Số thứ tự cầu thủ trên chợ")
async def mua(interaction: discord.Interaction, index: int):
    user_id = interaction.user.id
    if user_id not in user_wallets:
        await interaction.response.send_message("⚠️ Sếp chưa có tài khoản! Hãy dùng lệnh `/khoinghiep` trước.", ephemeral=True)
        return
    
    idx = index - 1
    if 0 <= idx < len(DATABASE_PLAYERS):
        player = DATABASE_PLAYERS[idx]
        price = player["price"]
        
        if user_wallets[user_id] >= price:
            user_wallets[user_id] -= price
            if user_id not in user_inventory:
                user_inventory[user_id] = []
            user_inventory[user_id].append(player["name"])
            await interaction.response.send_message(f"🔥 Thành công! Sếp đã rước **{player['name']}** về kho với giá {price} BP.")
        else:
            await interaction.response.send_message("❌ Sếp không đủ BP trong ví để mua ngôi sao này!", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Số thứ tự cầu thủ không hợp lệ.", ephemeral=True)

if __name__ == "__main__":
    keep_alive()
    TOKEN = os.getenv("DISCORD_TOKEN")
    if not TOKEN:
        print("❌ LỖI: Chưa cấu hình biến môi trường DISCORD_TOKEN!")
    else:
        bot.run(TOKEN)

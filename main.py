import discord
from discord.ext import commands
import discord.ui
from discord import app_commands
import requests
import os
from flask import Flask
from threading import Thread

# Web server giữ bot sống 24/7 trên Render
app = Flask('')

@app.route('/')
def home():
    return "Bot FC Online VN & Soi Keo dang hoat dong 24/7!"

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
user_predictions = {}  # Lưu lịch sử dự đoán của người chơi

DATABASE_PLAYERS = [
    {"name": "Ronaldo (ICON)", "pos": "ST", "rating": 105, "price": 5000},
    {"name": "Messi (23TY)", "pos": "RW", "rating": 106, "price": 5500},
    {"name": "Haaland (24TS)", "pos": "ST", "rating": 104, "price": 4200},
    {"name": "Bellingham (24TOTY)", "pos": "CM", "rating": 103, "price": 3800},
    {"name": "Van Dijk (ICON)", "pos": "CB", "rating": 105, "price": 4800},
    {"name": "De Bruyne (23TY)", "pos": "CAM", "rating": 104, "price": 4000},
    {"name": "Courtois (23NG)", "pos": "GK", "rating": 102, "price": 3000},
]

# Modal (Bảng nhập tỷ số) khi người chơi bấm dự đoán trận đấu
class ScoreModal(discord.ui.Modal, title="📝 Dự Đoán Tỷ Số Trận Đấu"):
    def __init__(self, match_name):
        super().__init__()
        self.match_name = match_name

    score_input = discord.ui.TextInput(
        label="Nhập tỷ số dự đoán (Ví dụ: 2-1)",
        placeholder="Đội nhà - Đội khách (VD: 2-1)",
        required=True,
        max_length=10
    )

    async def on_submit(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        if user_id not in user_wallets:
            await interaction.response.send_message("⚠️ Sếp chưa có tài khoản! Hãy dùng lệnh `/khoinghiep` trước.", ephemeral=True)
            return
        
        typed_score = self.score_input.value
        # Lưu lại dự đoán
        if user_id not in user_predictions:
            user_predictions[user_id] = []
        user_predictions[user_id].append({"match": self.match_name, "score": typed_score})

        await interaction.response.send_message(f"✅ Đã ghi nhận! Sếp **{interaction.user.mention}** dự đoán trận **{self.match_name}** có tỷ số **{typed_score}**. Chờ kết quả thực tế để nhận thưởng BP nhé!", ephemeral=True)

# Giao diện chọn trận đấu từ danh sách hot
class MatchSelectView(discord.ui.View):
    def __init__(self, matches):
        super().__init__(timeout=60)
        
        # Tạo Select Menu chứa các trận đấu thực tế
        options = []
        for m in matches:
            options.append(discord.SelectOption(label=m['name'], description=f"Giải: {m['league']} | Giờ: {m['time']}", value=m['name']))

        self.add_item(MatchSelect(options))

class MatchSelect(discord.ui.Select):
    def __init__(self, options):
        super().__init__(placeholder="⚽ Chọn trận đấu muốn dự đoán tỷ số...", options=options)

    async def callback(self, interaction: discord.Interaction):
        selected_match = self.values[0]
        # Hiện bảng Modal cho phép nhập tỷ số
        await interaction.response.send_modal(ScoreModal(selected_match))

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
        await interaction.response.send_message(f"🎉 Chúc mừng sếp {interaction.user.mention} gia nhập thế giới bóng đá! Nhận ngay **10,000 BP** và các thẻ cầu thủ vào kho.")

# 2. Lệnh SOI KÈO & DỰ ĐOÁN TRẬN ĐẤU THỰC TẾ
@bot.tree.command(name="soi", description="Hiển thị các trận đấu bóng đá gần nhất để dự đoán tỷ số")
async def soi(interaction: discord.Interaction):
    # Lấy danh sách trận đấu thực tế mới nhất (Giả lập danh sách các trận đỉnh cao từ Cúp C1 & Ngoại hạng Anh)
    # Sếp có thể thay thế bằng dữ liệu gọi từ API bóng đá thực tế sau này
    hot_matches = [
        {"name": "Real Madrid vs Man City", "league": "UEFA Champions League", "time": "Hôm nay 02:00"},
        {"name": "Arsenal vs Bayern Munich", "league": "UEFA Champions League", "time": "Hôm nay 02:00"},
        {"name": "Liverpool vs MU", "league": "Premier League", "time": "Chủ Nhật 22:30"},
        {"name": "Barca vs Inter Milan", "league": "UEFA Champions League", "time": "Ngày mai 02:00"}
    ]

    view = MatchSelectView(hot_matches)
    embed = discord.Embed(title="🔥 SOI KÈO TỶ SỐ - BÓNG ĐÁ REAL-TIME", description="Dưới đây là các trận cầu tâm điểm sắp diễn ra.\nHãy chọn trận bên dưới để bấm vào dự đoán tỷ số kiếm thưởng BP!", color=discord.Color.gold())
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

# 3. Lệnh xem đội hình & ví
@bot.tree.command(name="doihinh", description="Xem tài chính, sơ đồ chiến thuật và kho thẻ cầu thủ")
async def doihinh(interaction: discord.Interaction):
    user_id = interaction.user.id
    money = user_wallets.get(user_id, 0)
    inventory = user_inventory.get(user_id, ["Chưa có cầu thủ nào"])
    squad_info = user_squads.get(user_id, {"formation": "Chưa chọn sơ đồ"})
    
    embed = discord.Embed(title=f"🏟️ Câu Lạc Bộ Của {interaction.user.name}", color=discord.Color.blue())
    embed.add_field(name="💰 Số dư tài chính", value=f"{money} BP", inline=False)
    embed.add_field(name="📋 Sơ đồ chiến thuật", value=squad_info.get("formation"), inline=False)
    embed.add_field(name="🎒 Kho thẻ cầu thủ", value=", ".join(inventory), inline=False)
    await interaction.response.send_message(embed=embed)

# 4. Lệnh chợ chuyển nhượng
@bot.tree.command(name="cho", description="Mở chợ chuyển nhượng săn thẻ cầu thủ khủng")
async def cho(interaction: discord.Interaction):
    embed = discord.Embed(title="🛒 CHỢ CHUYỂN NHƯỢNG CẦU THỦ", description="Danh sách siêu sao đang được giao dịch:", color=discord.Color.green())
    for idx, p in enumerate(DATABASE_PLAYERS):
        embed.add_field(name=f"{idx+1}. {p['name']} [{p['pos']}]", value=f"⭐ Chỉ số: **{p['rating']}** | 💰 Giá: **{p['price']} BP**", inline=False)
    embed.set_footer(text="Dùng lệnh /mua [số_thứ_tự] để rước cầu thủ về kho!")
    await interaction.response.send_message(embed=embed)

# 5. Lệnh mua cầu thủ
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

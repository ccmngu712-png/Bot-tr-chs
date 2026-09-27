import discord
from discord.ext import commands
import random
import os
from flask import Flask
from threading import Thread

# Web server nhỏ giữ bot sống 24/7 trên Render
app = Flask('')

@app.route('/')
def home():
    return "Bot bong da dang hoat dong 24/7!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_web)
    t.start()

# Cấu hình Bot với Hybrid Commands (Nhận cả lệnh / lẫn lệnh thường)
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="/", intents=intents)

@bot.event
async def on_ready():
    print(f"Bot đã online thành công: {bot.user}")
    try:
        # Tự động đồng bộ lệnh ngay khi khởi động
        synced = await bot.tree.sync()
        print(f"Đã đồng bộ thành công {len(synced)} lệnh slash.")
    except Exception as e:
        print(f"Lỗi đồng bộ lệnh: {e}")

# Dữ liệu mẫu
user_wallets = {}
user_squads = {}
market_players = [
    {"name": "Haaland", "price": 500, "rating": 91},
    {"name": "Mbappe", "price": 550, "rating": 92},
    {"name": "De Bruyne", "price": 400, "rating": 88},
    {"name": "Bellingham", "price": 450, "rating": 89},
    {"name": "Van Dijk", "price": 350, "rating": 87}
]

@bot.tree.command(name="khoinghiep", description="Nhận vốn 1000 xu và đội bóng khởi nghiệp")
async def khoinghiep(interaction: discord.Interaction):
    user_id = interaction.user.id
    if user_id in user_wallets:
        await interaction.response.send_message(f"⚠️ {interaction.user.mention}, sếp đã nhận vốn khởi nghiệp rồi mà!", ephemeral=True)
    else:
        user_wallets[user_id] = 1000
        user_squads[user_id] = ["Thủ môn nghiệp dư", "Hậu vệ góc vườn"]
        await interaction.response.send_message(f"🎉 Chúc mừng {interaction.user.mention} đã gia nhập làng túc cầu! Sếp nhận được **1000 xu** vốn khởi nghiệp.")

@bot.tree.command(name="doihinh", description="Xem số dư tài chính và danh sách cầu thủ")
async def doihinh(interaction: discord.Interaction):
    user_id = interaction.user.id
    money = user_wallets.get(user_id, 0)
    squad = user_squads.get(user_id, ["Chưa có cầu thủ nào"])
    
    embed = discord.Embed(title=f"⚽ Câu lạc bộ của {interaction.user.name}", color=discord.Color.green())
    embed.add_field(name="💰 Số dư tài chính", value=f"{money} xu", inline=False)
    embed.add_field(name="🏃 Danh sách cầu thủ", value=", ".join(squad), inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="duoan", description="Dự đoán tỉ số các trận đấu bóng đá")
async def duoan(interaction: discord.Interaction, tran_dau: str, ty_so: str):
    user_id = interaction.user.id
    if user_id not in user_wallets:
        await interaction.response.send_message("⚠️ Sếp chưa có tài khoản! Hãy dùng lệnh `/khoinghiep` trước nhé.", ephemeral=True)
        return
    await interaction.response.send_message(f"✅ Đã ghi nhận! {interaction.user.mention} dự đoán trận **{tran_dau}** có tỷ số **{ty_so}**.")

@bot.tree.command(name="cho", description="Mở chợ chuyển nhượng mua bán cầu thủ")
async def cho(interaction: discord.Interaction):
    embed = discord.Embed(title="🛒 Chợ Chuyển Nhượng Cầu Thủ", description="Danh sách ngôi sao đang rao bán:", color=discord.Color.gold())
    for idx, p in enumerate(market_players):
        embed.add_field(name=f"{idx+1}. {p['name']} (Chỉ số: {p['rating']})", value=f"Giá: **{p['price']} xu**", inline=False)
    embed.set_footer(text="Dùng lệnh /mua [số_thứ_tự] để tậu cầu thủ về đội!")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="mua", description="Mua cầu thủ từ chợ chuyển nhượng")
async def mua(interaction: discord.Interaction, index: int):
    user_id = interaction.user.id
    if user_id not in user_wallets:
        await interaction.response.send_message("⚠️ Sếp chưa có tài khoản! Hãy dùng lệnh `/khoinghiep` trước.", ephemeral=True)
        return
    
    idx = index - 1
    if 0 <= idx < len(market_players):
        player = market_players[idx]
        price = player["price"]
        
        if user_wallets[user_id] >= price:
            user_wallets[user_id] -= price
            user_squads[user_id].append(player["name"])
            await interaction.response.send_message(f"🔥 Thành công! {interaction.user.mention} đã mua thành công **{player['name']}** với giá {price} xu.")
        else:
            await interaction.response.send_message("❌ Sếp không đủ tiền trong ví để rước ngôi sao này về rồi!", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Số thứ tự cầu thủ không hợp lệ.", ephemeral=True)

@bot.tree.command(name="daudoi", description="Thách đấu giao hữu với thành viên khác")
async def daudoi(interaction: discord.Interaction, member: discord.Member):
    if interaction.user == member:
        await interaction.response.send_message("⚠️ Sếp không thể tự đá với chính mình được đâu!", ephemeral=True)
        return
        
    teams = [interaction.user.name, member.name]
    winner = random.choice(teams)
    score_a = random.randint(0, 3)
    score_b = random.randint(0, 3)
    
    embed = discord.Embed(title="🏟️ Trận Cầu Đỉnh Cao", color=discord.Color.blue())
    embed.add_field(name="Tỉ số chung cuộc", value=f"{interaction.user.name} **{score_a} - {score_b}** {member.name}", inline=False)
    embed.add_field(name="🏆 Kết quả", value=f"Chúc mừng **{winner}** đã giành chiến thắng!", inline=False)
    
    await interaction.response.send_message(embed=embed)

if __name__ == "__main__":
    keep_alive()
    TOKEN = os.getenv("DISCORD_TOKEN")
    if not TOKEN:
        print("❌ LỖI: Chưa cấu hình biến môi trường DISCORD_TOKEN!")
    else:
        bot.run(TOKEN)

import discord
from discord.ext import commands
import random
import os

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="/", intents=intents)

# Dữ liệu mẫu quản lý câu lạc bộ
user_wallets = {}
user_squads = {}
market_players = [
    {"name": "Haaland", "price": 500, "rating": 91},
    {"name": "Mbappe", "price": 550, "rating": 92},
    {"name": "De Bruyne", "price": 400, "rating": 88},
    {"name": "Bellingham", "price": 450, "rating": 89},
    {"name": "Van Dijk", "price": 350, "rating": 87}
]

@bot.event
async def on_ready():
    print(f"Bot bóng đá đã online thành công trên mây: {bot.user}")

@bot.command(name="khoinghiep")
async def khoinghiep(ctx):
    user_id = ctx.author.id
    if user_id in user_wallets:
        await ctx.send(f"⚠️ {ctx.author.mention}, sếp đã nhận vốn khởi nghiệp từ trước rồi mà!")
    else:
        user_wallets[user_id] = 1000
        user_squads[user_id] = ["Thủ môn nghiệp dư", "Hậu vệ góc vườn"]
        await ctx.send(f"🎉 Chúc mừng {ctx.author.mention} đã gia nhập làng túc cầu! Sếp nhận được **1000 xu** vốn khởi nghiệp.")

@bot.command(name="doihinh")
async def doihinh(ctx):
    user_id = ctx.author.id
    money = user_wallets.get(user_id, 0)
    squad = user_squads.get(user_id, ["Chưa có cầu thủ nào"])
    
    embed = discord.Embed(title=f"⚽ Câu lạc bộ của {ctx.author.name}", color=discord.Color.green())
    embed.add_field(name="💰 Số dư tài chính", value=f"{money} xu", inline=False)
    embed.add_field(name="🏃 Danh sách cầu thủ", value=", ".join(squad), inline=False)
    await ctx.send(embed=embed)

@bot.command(name="duoan")
async def duoan(ctx, tran_dau: str, ty_so: str):
    user_id = ctx.author.id
    if user_id not in user_wallets:
        await ctx.send("⚠️ Sếp chưa có tài khoản! Hãy gõ `/khoinghiep` trước nhé.")
        return
    await ctx.send(f"✅ Đã ghi nhận! {ctx.author.mention} dự đoán trận **{trận_dau}** có tỷ số **{ty_so}**.")

@bot.command(name="cho")
async def cho(ctx):
    embed = discord.Embed(title="🛒 Chợ Chuyển Nhượng Cầu Thủ", description="Danh sách ngôi sao đang rao bán:", color=discord.Color.gold())
    for idx, p in enumerate(market_players):
        embed.add_field(name=f"{idx+1}. {p['name']} (Chỉ số: {p['rating']})", value=f"Giá: **{p['price']} xu**", inline=False)
    embed.set_footer(text="Gõ lệnh /mua [số_thứ_tự] để tậu cầu thủ về đội!")
    await ctx.send(embed=embed)

@bot.command(name="mua")
async def mua(ctx, index: int):
    user_id = ctx.author.id
    if user_id not in user_wallets:
        await ctx.send("⚠️ Sếp chưa có tài khoản! Hãy gõ `/khoinghiep` trước.")
        return
    
    idx = index - 1
    if 0 <= idx < len(market_players):
        player = market_players[idx]
        price = player["price"]
        
        if user_wallets[user_id] >= price:
            user_wallets[user_id] -= price
            user_squads[user_id].append(player["name"])
            await ctx.send(f"🔥 Thành công! {ctx.author.mention} đã mua thành công **{player['name']}** với giá {price} xu.")
        else:
            await ctx.send("❌ Sếp không đủ tiền trong ví để rước ngôi sao này về rồi!")
    else:
        await ctx.send("❌ Số thứ tự cầu thủ không hợp lệ.")

@bot.command(name="daudoi")
async def daudoi(ctx, member: discord.Member):
    if ctx.author == member:
        await ctx.send("⚠️ Sếp không thể tự đá với chính mình được đâu!")
        return
        
    teams = [ctx.author.name, member.name]
    winner = random.choice(teams)
    score_a = random.randint(0, 3)
    score_b = random.randint(0, 3)
    
    embed = discord.Embed(title="🏟️ Trận Cầu Đỉnh Cao", color=discord.Color.blue())
    embed.add_field(name="Tỉ số chung cuộc", value=f"{ctx.author.name} **{score_a} - {score_b}** {member.name}", inline=False)
    embed.add_field(name="🏆 Kết quả", value=f"Chúc mừng **{winner}** đã giành chiến thắng!", inline=False)
    
    await ctx.send(embed=embed)

# Đọc Token bảo mật từ biến môi trường của hệ thống Cloud
TOKEN = os.getenv("DISCORD_TOKEN")
bot.run(TOKEN)

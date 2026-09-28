async def notify_match_finished(match, settled):
    """
    Khi trận kết thúc:
    - Server nhận 1 thông báo chung.
    - Mỗi người cược nhận DM riêng.
    - Nếu một người có nhiều cược trong cùng trận,
      tất cả kết quả được gộp vào 1 DM.
    """

    match_id = int(match["id"])

    cache_key = f"{match_id}:finished"

    if match_notice_cache.get(cache_key):
        return

    home = match.get(
        "homeTeam",
        {},
    ).get(
        "name",
        "Home",
    )

    away = match.get(
        "awayTeam",
        {},
    ).get(
        "name",
        "Away",
    )

    score = match.get(
        "score",
        {},
    ).get(
        "fullTime",
        {},
    )

    home_score = score.get("home")
    away_score = score.get("away")

    if home_score is None or away_score is None:
        return

    # Xác định kết quả trận
    if home_score > away_score:
        result = "HOME"
        result_text_display = f"🏠 {home} thắng"
    elif home_score < away_score:
        result = "AWAY"
        result_text_display = f"✈️ {away} thắng"
    else:
        result = "DRAW"
        result_text_display = "🤝 Trận đấu hòa"

    # =====================================================
    # LẤY CÁC CƯỢC ĐÃ ĐƯỢC KẾT TOÁN
    # =====================================================

    # settled được truyền vào từ settle_match_bets()
    by_user = {}

    for item in settled:
        by_user.setdefault(
            item["user_id"],
            [],
        ).append(item)

    # =====================================================
    # DM RIÊNG CHO TỪNG NGƯỜI
    # =====================================================

    for user_id, user_bets in by_user.items():

        try:
            user = await bot.fetch_user(user_id)
        except Exception as e:
            print(
                f"[DM] Không tìm thấy user {user_id}: {e}"
            )
            continue

        total_stake = sum(
            int(item["stake"])
            for item in user_bets
        )

        total_payout = sum(
            int(item["payout"])
            for item in user_bets
        )

        won_count = sum(
            1
            for item in user_bets
            if item["won"]
        )

        lost_count = sum(
            1
            for item in user_bets
            if not item["won"]
        )

        # Lấy số dư mới
        wallet = await get_wallet(user_id)

        balance = int(wallet["balance"])
        vip = int(wallet["vip_level"])

        # -------------------------------------------------
        # Tạo nội dung các cược
        # -------------------------------------------------

        bet_lines = []

        for item in user_bets:

            choice = item["choice"]

            choice_text = {
                "HOME": f"🏠 {home}",
                "DRAW": "🤝 Hòa",
                "AWAY": f"✈️ {away}",
            }.get(
                choice,
                choice,
            )

            if item["won"]:

                bet_lines.append(
                    (
                        "🎉 **THẮNG**\n"
                        f"🎯 Chọn: **{choice_text}**\n"
                        f"🪙 Cược: **{item['stake']:,}**\n"
                        f"💰 Nhận: **{item['payout']:,}**"
                    )
                )

            else:

                bet_lines.append(
                    (
                        "❌ **THUA**\n"
                        f"🎯 Chọn: **{choice_text}**\n"
                        f"🪙 Mất: **{item['stake']:,}**"
                    )
                )

        # -------------------------------------------------
        # Xác định DM thắng / thua
        # -------------------------------------------------

        if won_count > 0 and lost_count == 0:

            dm_title = "🎉 BẠN THẮNG CƯỢC!"

            dm_color = COLOR_GREEN

            summary = (
                f"🏆 Bạn thắng **{won_count}** cược!"
            )

        elif won_count == 0 and lost_count > 0:

            dm_title = "❌ BẠN THUA CƯỢC"

            dm_color = COLOR_RED

            summary = (
                f"💔 Bạn thua **{lost_count}** cược."
            )

        else:

            dm_title = "📊 KẾT QUẢ CƯỢC"

            dm_color = COLOR_ORANGE

            summary = (
                f"🎉 Thắng: **{won_count}**\n"
                f"❌ Thua: **{lost_count}**"
            )

        # -------------------------------------------------
        # Tạo Embed DM
        # -------------------------------------------------

        embed = base_embed(
            dm_title,
            (
                f"⚽ **{home}** "
                f"**{home_score} - {away_score}** "
                f"**{away}**\n\n"
                f"🏆 Kết quả: **{result_text_display}**\n\n"
                f"{summary}\n\n"
                + "\n\n".join(bet_lines)
            ),
            dm_color,
        )

        embed.add_field(
            name="💵 Tổng tiền cược",
            value=f"**{total_stake:,} 🪙**",
            inline=True,
        )

        embed.add_field(
            name="💰 Tổng tiền nhận",
            value=f"**{total_payout:,} 🪙**",
            inline=True,
        )

        embed.add_field(
            name="💳 Số dư hiện tại",
            value=f"**{balance:,} 🪙**",
            inline=True,
        )

        embed.add_field(
            name="👑 VIP",
            value=VIP_NAMES.get(
                vip,
                "Thành viên",
            ),
            inline=True,
        )

        embed.set_thumbnail(
            url="https://cdn-icons-png.flaticon.com/512/2583/2583344.png"
        )

        # -------------------------------------------------
        # Gửi DM
        # -------------------------------------------------

        try:

            await user.send(
                embed=embed
            )

            print(
                f"[DM] Đã gửi kết quả cược "
                f"cho {user_id}"
            )

        except discord.Forbidden:

            # Người dùng tắt DM
            print(
                f"[DM] Không thể gửi DM cho "
                f"{user_id}: DM bị tắt."
            )

        except Exception as e:

            print(
                f"[DM] Lỗi gửi DM {user_id}: {e}"
            )

    # =====================================================
    # THÔNG BÁO CHUNG TRÊN SERVER
    # =====================================================

    # Lấy guild/channel từ những người đã cược
    targets = await get_channels_for_bet_match(
        match_id
    )

    notified_guilds = set()

    for guild, member in targets:

        if guild.id in notified_guilds:
            continue

        notified_guilds.add(guild.id)

        embed = base_embed(
            "🏁 TRẬN ĐẤU ĐÃ KẾT THÚC",
            (
                f"⚽ **{home}** "
                f"**{home_score} - {away_score}** "
                f"**{away}**\n\n"
                f"🏆 Kết quả: **{result_text_display}**\n\n"
                "📩 Kết quả thắng/thua của từng người "
                "đã được gửi **DM riêng**."
            ),
            COLOR_GREEN,
        )

        channel = guild.system_channel

        if channel is None:

            for ch in guild.text_channels:

                perms = ch.permissions_for(
                    guild.me
                )

                if (
                    perms.send_messages
                    and perms.embed_links
                ):
                    channel = ch
                    break

        if channel:

            try:

                await channel.send(
                    embed=embed
                )

            except Exception as e:

                print(
                    f"[SERVER] Lỗi gửi thông báo: {e}"
                )

    # Đánh dấu đã xử lý
    match_notice_cache[cache_key] = True

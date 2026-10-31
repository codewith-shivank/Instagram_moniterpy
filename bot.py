import os
import asyncio
import logging
from datetime import datetime, timezone, timedelta
import aiohttp

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from database import Database, AlertConfig
from instagram import InstagramChecker, AccountState
from cards import create_profile_card

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("DISCORD_GUILD_ID")
CHECK_INTERVAL = max(30, int(os.getenv("CHECK_INTERVAL_SECONDS", "120")))
CONFIRMATION_CHECKS = max(1, int(os.getenv("CONFIRMATION_CHECKS", "2")))

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing from .env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("homelander")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
db = Database(os.getenv("STATE_FILE", "monitor.db"))
checker = InstagramChecker()

def utcnow():
    return datetime.now(timezone.utc)

def elapsed_text(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m {s}s"

def clean_username(value: str) -> str:
    value = value.strip()
    value = value.rstrip("/")
    if "instagram.com/" in value:
        value = value.split("instagram.com/", 1)[1].split("?", 1)[0].split("/", 1)[0]
    value = value.lstrip("@").strip()
    return value.lower()

async def fetch_confirmed(username: str, expected: AccountState | None = None):
    """Require repeated identical results to reduce false ban/recovery alerts."""
    last = None
    stable = 0
    for _ in range(CONFIRMATION_CHECKS):
        result = await checker.check(username)
        if result.state == AccountState.UNKNOWN:
            return result
        if last == result.state:
            stable += 1
        else:
            stable = 1
            last = result.state
        if stable >= CONFIRMATION_CHECKS:
            return result
        await asyncio.sleep(5)
    return result

async def send_event(channel: discord.abc.Messageable, monitor, event: str, profile, config: AlertConfig):
    elapsed = elapsed_text(int((utcnow() - monitor.started_at).total_seconds()))
    if event == "banned":
        title = f"🔴 @{monitor.username} unavailable"
        followers = profile.followers_display if profile else f"{monitor.followers:,}"
        line = f"Last confirmed active: {elapsed} ago\\nMonitoring duration: {elapsed}\\nLast known followers: {followers}"
        accent = "red"
    else:
        followers = profile.followers_display if profile else "Unknown"
        title = f"🏆 Account Recovered | @{monitor.username} ✅"
        line = f"👥 Followers: {followers} | ⏱️ Time Elapsed: {elapsed}"
        accent = "green"

    embed = discord.Embed(title=title, description=line, color=discord.Color.red() if accent == "red" else discord.Color.green())
    if config.style == "plain":
        embed = None
    mention = ""
    if config.everyone:
        mention += "@everyone "
    mention += " ".join(f"<@&{role_id}>" for role_id in config.role_ids)
    allowed = discord.AllowedMentions(everyone=config.everyone, roles=True)
    if profile and embed:
        if profile.profile_pic_url:
            embed.set_thumbnail(url=profile.profile_pic_url)
        embed.add_field(name="Posts", value=str(profile.posts), inline=True)
        embed.add_field(name="Followers", value=profile.followers_display, inline=True)
        embed.add_field(name="Following", value=profile.following_display, inline=True)
        embed.set_footer(text="Homelander Instagram Monitor • automatic detection")

    card_path = None
    try:
        card_path = create_profile_card(profile, monitor.username, unavailable=(event == "banned"))
        if card_path:
            file = discord.File(card_path, filename="instagram-profile.png")
            if embed:
                embed.set_image(url="attachment://instagram-profile.png")
            await channel.send(content=(mention + title) if embed is None else (mention or None), embed=embed, file=file, allowed_mentions=allowed)
        else:
            await channel.send(content=(mention + title) if embed is None else (mention or None), embed=embed, allowed_mentions=allowed)
    except Exception:
        log.exception("Failed to create/send profile card")
        await channel.send(content=mention + title, embed=embed, allowed_mentions=allowed)
    if config.webhook_url:
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(config.webhook_url, json={"content": mention + title, "embeds": [embed.to_dict()] if embed else []}, timeout=10)
        except Exception:
            log.exception("Webhook alert failed")

@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)
    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            log.info("Synced %d guild commands.", len(synced))
        else:
            synced = await bot.tree.sync()
            log.info("Synced %d global commands.", len(synced))
    except Exception:
        log.exception("Slash command sync failed")
    if not monitor_loop.is_running():
        monitor_loop.start()
    if not summary_loop.is_running():
        summary_loop.start()

@bot.tree.command(name="ban", description="Monitor an active Instagram account until it becomes unavailable.")
@app_commands.describe(username="Instagram username or profile URL")
async def ban(interaction: discord.Interaction, username: str):
    username = clean_username(username)
    if not username:
        return await interaction.response.send_message("❌ Invalid Instagram username.", ephemeral=True)

    await interaction.response.defer()
    result = await checker.check(username)
    if result.state == AccountState.UNKNOWN:
        return await interaction.followup.send(
            f"⚠️ I couldn't verify @{username} right now. Try again in a moment."
        )
    if result.state == AccountState.UNAVAILABLE:
        return await interaction.followup.send(
            f"⚠️ @{username} already appears unavailable. Use `/unban {username}` to monitor recovery."
        )

    db.add_monitor(username, "ban", interaction.channel_id, interaction.user.id, result, interaction.guild_id)
    await interaction.followup.send(
        f"🔴 **Ban monitoring started** for **@{username}**.\n"
        f"Current status: 🟢 Active\n"
        f"I'll automatically alert this channel when the account becomes unavailable."
    )

@bot.tree.command(name="unban", description="Monitor an unavailable Instagram account until it recovers.")
@app_commands.describe(username="Instagram username or profile URL")
async def unban(interaction: discord.Interaction, username: str):
    username = clean_username(username)
    if not username:
        return await interaction.response.send_message("❌ Invalid Instagram username.", ephemeral=True)

    await interaction.response.defer()
    result = await checker.check(username)
    if result.state == AccountState.UNKNOWN:
        return await interaction.followup.send(
            f"⚠️ I couldn't verify @{username} right now. Try again in a moment."
        )
    if result.state == AccountState.ACTIVE:
        return await interaction.followup.send(
            f"⚠️ @{username} already appears active. Use `/ban {username}` to monitor a future ban."
        )

    db.add_monitor(username, "unban", interaction.channel_id, interaction.user.id, result, interaction.guild_id)
    await interaction.followup.send(
        f"🟢 **Recovery monitoring started** for **@{username}**.\n"
        f"Current status: 🔴 Unavailable\n"
        f"I'll automatically alert this channel when the account becomes available again."
    )

@bot.tree.command(name="stop", description="Stop monitoring an Instagram account.")
@app_commands.describe(username="Instagram username or profile URL")
async def stop(interaction: discord.Interaction, username: str):
    username = clean_username(username)
    removed = db.stop_monitor(username)
    if not removed:
        return await interaction.response.send_message(
            f"⚠️ @{username} is not currently being monitored.", ephemeral=True
        )
    await interaction.response.send_message(f"🛑 Stopped monitoring **@{username}**.")

@bot.tree.command(name="list", description="List all active Instagram monitors.")
async def list_monitors(interaction: discord.Interaction):
    monitors = db.get_monitors()
    if not monitors:
        return await interaction.response.send_message("📱 No active monitors.")

    ban_items = [m for m in monitors if m.mode == "ban"]
    unban_items = [m for m in monitors if m.mode == "unban"]
    lines = ["📱 **Instagram Monitors**", ""]
    lines.append("🔴 **BAN MONITORS**")
    lines.extend([f"• @{m.username}" for m in ban_items] or ["• None"])
    lines.append("")
    lines.append("🟢 **RECOVERY MONITORS**")
    lines.extend([f"• @{m.username}" for m in unban_items] or ["• None"])
    lines.append("")
    lines.append(f"**Total:** {len(monitors)}")
    await interaction.response.send_message("\n".join(lines))

@bot.tree.command(name="alert-channel", description="Add or remove an alert destination channel.")
@app_commands.describe(action="add or remove", channel="Channel to receive alerts")
@app_commands.choices(action=[app_commands.Choice(name="add", value="add"), app_commands.Choice(name="remove", value="remove")])
@app_commands.checks.has_permissions(manage_guild=True)
async def alert_channel(interaction: discord.Interaction, action: app_commands.Choice[str], channel: discord.TextChannel):
    if action.value == "add": db.add_destination(interaction.guild_id, channel.id)
    else: db.remove_destination(interaction.guild_id, channel.id)
    await interaction.response.send_message(f"Alert channel {action.value}d: {channel.mention}", ephemeral=True)

@bot.tree.command(name="alert-config", description="Configure alert style, cooldown, roles, summaries, webhooks and DMs.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(style="embed or plain", cooldown="Cooldown seconds", roles="Comma-separated role IDs", everyone="Allow @everyone", dm="DM monitor owner", webhook="Webhook URL", summary="off, daily or weekly")
async def alert_config(interaction: discord.Interaction, style: str="embed", cooldown: int=0, roles: str="", everyone: bool=False, dm: bool=False, webhook: str="", summary: str="off"):
    if style not in ("embed", "plain") or summary not in ("off", "daily", "weekly") or cooldown < 0:
        return await interaction.response.send_message("Invalid style, summary, or cooldown.", ephemeral=True)
    if everyone and not interaction.channel.permissions_for(interaction.user).mention_everyone:
        return await interaction.response.send_message("You need the Mention Everyone permission to enable @everyone alerts.", ephemeral=True)
    role_ids = tuple(int(x.strip()) for x in roles.split(",") if x.strip().isdigit())
    if role_ids:
        missing = [role_id for role_id in role_ids if interaction.guild.get_role(role_id) is None]
        if missing:
            return await interaction.response.send_message(f"Unknown role ID(s): {', '.join(map(str, missing))}", ephemeral=True)
    cfg = AlertConfig(interaction.guild_id, style, cooldown, role_ids, everyone, dm, webhook or None, summary, interaction.channel_id if summary != "off" else None)
    db.save_config(cfg)
    await interaction.response.send_message("✅ Alert configuration saved.", ephemeral=True)

@bot.tree.command(name="history", description="Show recent monitoring events.")
@app_commands.describe(limit="Number of events to show (1-20)")
async def history(interaction: discord.Interaction, limit: app_commands.Range[int, 1, 20] = 10):
    rows = db.get_history(limit)
    if not rows:
        return await interaction.response.send_message("📜 No monitoring history yet.")
    lines = ["📜 **Monitoring History**", ""]
    for row in rows:
        emoji = "❌" if row["event"] == "banned" else "🏆"
        lines.append(
            f"{emoji} **@{row['username']}** — {row['event'].title()} — "
            f"{elapsed_text(row['elapsed_seconds'])}"
        )
    await interaction.response.send_message("\n".join(lines))

@bot.tree.command(name="status", description="Check the current status of an Instagram account.")
@app_commands.describe(username="Instagram username or profile URL")
async def status(interaction: discord.Interaction, username: str):
    username = clean_username(username)
    await interaction.response.defer()
    result = await checker.check(username)
    if result.state == AccountState.UNKNOWN:
        return await interaction.followup.send(f"⚠️ Could not verify @{username} right now.")
    profile = result.profile
    embed = discord.Embed(
        title=f"📱 Instagram Status — @{username}",
        color=discord.Color.green() if result.state == AccountState.ACTIVE else discord.Color.red()
    )
    embed.add_field(name="Status", value="🟢 Active" if result.state == AccountState.ACTIVE else "🔴 Unavailable", inline=False)
    if profile:
        embed.add_field(name="Posts", value=str(profile.posts), inline=True)
        embed.add_field(name="Followers", value=profile.followers_display, inline=True)
        embed.add_field(name="Following", value=profile.following_display, inline=True)
        if profile.profile_pic_url:
            embed.set_thumbnail(url=profile.profile_pic_url)
    await interaction.followup.send(embed=embed)

@tasks.loop(hours=1)
async def summary_loop():
    now = utcnow()
    for cfg in db.get_summary_configs():
        interval = timedelta(days=1 if cfg.summary_frequency == "daily" else 7)
        if cfg.last_summary_at and now - cfg.last_summary_at < interval:
            continue
        since = cfg.last_summary_at or (now - interval)
        rows = db.summary_rows(cfg.guild_id, since)
        channel = bot.get_channel(cfg.summary_channel_id)
        if not channel:
            continue
        if rows:
            lines = [f"📊 **{cfg.summary_frequency.title()} Instagram Summary**", ""]
            for row in rows[:20]:
                emoji = "❌" if row["event"] == "banned" else "🏆"
                lines.append(f"{emoji} **@{row['username']}** — {row['event'].title()} — {elapsed_text(row['elapsed_seconds'])}")
            await channel.send("\n".join(lines))
        else:
            await channel.send(f"📊 **{cfg.summary_frequency.title()} Instagram Summary**\nNo monitoring events recorded.")
        db.mark_summary_sent(cfg.guild_id, now)

@tasks.loop(seconds=CHECK_INTERVAL)
async def monitor_loop():
    monitors = db.get_monitors()
    for monitor in monitors:
        try:
            result = await checker.check(monitor.username)
            if result.state == AccountState.UNKNOWN:
                log.warning("Could not verify @%s", monitor.username)
                continue

            previous = monitor.last_state
            db.update_last_check(monitor.username, result)

            if monitor.mode == "ban" and previous == AccountState.ACTIVE and result.state == AccountState.UNAVAILABLE:
                # Confirm transition before alerting.
                confirmed = await fetch_confirmed(monitor.username)
                if confirmed.state != AccountState.UNAVAILABLE:
                    continue
                cfg = db.get_config(monitor.guild_id) if monitor.guild_id else AlertConfig(0)
                if monitor.guild_id and cfg.cooldown_seconds and db.recent_event(monitor.guild_id, monitor.username, cfg.cooldown_seconds):
                    continue
                channels = [bot.get_channel(monitor.channel_id)]
                if monitor.guild_id:
                    channels += [bot.get_channel(cid) for cid in db.get_destinations(monitor.guild_id)]
                for channel in {c for c in channels if c}:
                    await send_event(channel, monitor, "banned", confirmed.profile, cfg)
                if cfg.dm_alerts:
                    try: await (await bot.fetch_user(monitor.user_id)).send(f"Instagram alert: @{monitor.username} was unavailable.")
                    except Exception: log.exception("DM alert failed")
                db.add_history(monitor.username, "banned", monitor.started_at, confirmed.profile, monitor.guild_id)
                db.stop_monitor(monitor.username)

            elif monitor.mode == "unban" and previous == AccountState.UNAVAILABLE and result.state == AccountState.ACTIVE:
                confirmed = await fetch_confirmed(monitor.username)
                if confirmed.state != AccountState.ACTIVE:
                    continue
                cfg = db.get_config(monitor.guild_id) if monitor.guild_id else AlertConfig(0)
                if monitor.guild_id and cfg.cooldown_seconds and db.recent_event(monitor.guild_id, monitor.username, cfg.cooldown_seconds):
                    continue
                channels = [bot.get_channel(monitor.channel_id)]
                if monitor.guild_id:
                    channels += [bot.get_channel(cid) for cid in db.get_destinations(monitor.guild_id)]
                for channel in {c for c in channels if c}:
                    await send_event(channel, monitor, "recovered", confirmed.profile, cfg)
                if cfg.dm_alerts:
                    try: await (await bot.fetch_user(monitor.user_id)).send(f"Instagram alert: @{monitor.username} recovered.")
                    except Exception: log.exception("DM alert failed")
                db.add_history(monitor.username, "recovered", monitor.started_at, confirmed.profile, monitor.guild_id)
                db.stop_monitor(monitor.username)
        except Exception:
            log.exception("Monitor error for @%s", monitor.username)

@monitor_loop.before_loop
async def before_monitor_loop():
    await bot.wait_until_ready()

bot.run(TOKEN)

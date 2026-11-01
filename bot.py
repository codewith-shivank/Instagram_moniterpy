import asyncio
import logging
import os
import sqlite3
from datetime import UTC, datetime, timedelta

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from cards import create_profile_card
from database import AlertConfig, Database
from instagram import AccountState, InstagramChecker
from ui import (
    PageView,
    account_url,
    base_embed,
    empty_embed,
    error_embed,
    format_timestamp,
    paged_embeds,
    short_username,
    status_text,
)

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = os.getenv("DISCORD_GUILD_ID")
CHECK_INTERVAL = max(30, int(os.getenv("CHECK_INTERVAL_SECONDS", "120")))
CONFIRMATION_CHECKS = max(1, int(os.getenv("CONFIRMATION_CHECKS", "2")))
COMMAND_SYNC_RETRIES = 3
RECONNECT_MIN_SECONDS = 5
RECONNECT_MAX_SECONDS = 60

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing from .env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("shivannnkk-instagram-monitor")

intents = discord.Intents.default()
# Required only for the legacy prefix command parser; slash commands do not need it.
intents.message_content = True


class ReliableCommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Acknowledge every slash command before its callback performs any I/O.
        await defer_interaction(interaction)
        return True


class MonitorBot(commands.Bot):
    async def setup_hook(self) -> None:
        await sync_commands()


bot = MonitorBot(command_prefix="!", intents=intents, tree_cls=ReliableCommandTree)
db = Database(os.getenv("STATE_FILE", "monitor.db"))
checker = InstagramChecker()
command_sync_lock = asyncio.Lock()
connection_ready = asyncio.Event()

def utcnow():
    return datetime.now(UTC)

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

async def defer_interaction(interaction: discord.Interaction, *, ephemeral: bool = False) -> None:
    """Acknowledge an interaction once, before any potentially slow work."""
    if not interaction.response.is_done():
        await interaction.response.defer(thinking=True, ephemeral=ephemeral)

async def reply(interaction: discord.Interaction, *args, **kwargs):
    """Send exactly one response, using a follow-up after defer()."""
    if interaction.response.is_done():
        return await interaction.followup.send(*args, **kwargs)
    return await interaction.response.send_message(*args, **kwargs)

async def command_error_reply(interaction: discord.Interaction, command_name: str):
    log.error("/%s failed", command_name)
    message = "⚠️ Something went wrong while processing that command. Please try again."
    try:
        await reply(interaction, message, ephemeral=True)
    except discord.HTTPException:
        log.exception("Could not send error response for /%s", command_name)

async def fetch_confirmed(username: str, expected: AccountState | None = None):
    """Require repeated identical results to reduce false ban/recovery alerts."""
    last = None
    stable = 0
    result = None
    for _ in range(CONFIRMATION_CHECKS):
        result = await checker.check(username)
        if result.state == AccountState.UNKNOWN:
            return result
        if expected is not None and result.state != expected:
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
    unavailable = event == "banned"
    status = "unavailable" if unavailable else "active"
    title = f"Account {status_text(status).title()} • @{short_username(monitor.username)}"
    followers = profile.followers_display if profile else (f"{monitor.followers:,}" if unavailable else "Unknown")
    embed = base_embed(
        title,
        f"The latest confirmed check reports this account as **{status_text(status)}**.",
        status,
    )
    embed.add_field(name="Status", value=f"`{status_text(status)}`", inline=True)
    embed.add_field(name="Followers", value=followers, inline=True)
    embed.add_field(name="Monitoring duration", value=elapsed, inline=True)
    embed.add_field(name="Detected", value=format_timestamp(utcnow()), inline=False)
    message_embed: discord.Embed | None = None if config.style == "plain" else embed
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
        if profile.latest_post_url:
            caption = (profile.latest_post_caption or "View the latest post").replace("\n", " ")[:180]
            embed.add_field(name="Latest post", value=f"[{caption}]({profile.latest_post_url})", inline=False)
        embed.set_footer(text="shivannnkk • Instagram Monitor • automatic detection")

    card_path = None
    try:
        card_path = create_profile_card(profile, monitor.username, unavailable=(event == "banned"))
        if card_path:
            file = discord.File(card_path, filename="instagram-profile.png")
            if message_embed:
                message_embed.set_image(url="attachment://instagram-profile.png")
            if message_embed:
                await channel.send(content=mention or None, embed=message_embed, file=file, allowed_mentions=allowed)
            else:
                await channel.send(content=mention + title, file=file, allowed_mentions=allowed)
        else:
            if message_embed:
                await channel.send(content=mention or None, embed=message_embed, allowed_mentions=allowed)
            else:
                await channel.send(content=mention + title, allowed_mentions=allowed)
    except Exception:
        log.exception("Failed to create/send profile card")
        if message_embed:
            await channel.send(content=mention + title, embed=message_embed, allowed_mentions=allowed)
        else:
            await channel.send(content=mention + title, allowed_mentions=allowed)
    if config.webhook_url:
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(
                    config.webhook_url,
                    json={"content": mention + title, "embeds": [message_embed.to_dict()] if message_embed else []},
                    timeout=aiohttp.ClientTimeout(total=10),
                )
        except Exception:
            log.exception("Webhook alert failed")

def is_transient_discord_error(error: BaseException) -> bool:
    if isinstance(error, discord.LoginFailure):
        return False
    if isinstance(error, (aiohttp.ClientError, OSError, TimeoutError)):
        return True
    if isinstance(error, (discord.ConnectionClosed, discord.GatewayNotFound)):
        return True
    status = getattr(error, "status", 0)
    return isinstance(error, discord.HTTPException) and (status == 429 or status >= 500)

async def sync_commands() -> None:
    """Replace the deployed command set with the commands currently defined here."""
    async with command_sync_lock:
        for attempt in range(1, COMMAND_SYNC_RETRIES + 1):
            try:
                if GUILD_ID:
                    guild = discord.Object(id=int(GUILD_ID))
                    bot.tree.clear_commands(guild=guild)
                    bot.tree.copy_global_to(guild=guild)
                    synced = await bot.tree.sync(guild=guild)
                    scope = f"guild {GUILD_ID}"
                else:
                    synced = await bot.tree.sync()
                    scope = "global"
                names = ", ".join(sorted(command.name for command in synced)) or "(none)"
                log.info("Synced %d %s commands: %s", len(synced), scope, names)
                return
            except Exception as error:
                if not is_transient_discord_error(error) or attempt == COMMAND_SYNC_RETRIES:
                    raise
                delay = min(
                    RECONNECT_MAX_SECONDS,
                    RECONNECT_MIN_SECONDS * 2 ** (attempt - 1),
                )
                log.warning(
                    "Command sync attempt %d/%d failed transiently; retrying in %ss.",
                    attempt,
                    COMMAND_SYNC_RETRIES,
                    delay,
                )
                await asyncio.sleep(delay)

@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)
    connection_ready.set()
    if not monitor_loop.is_running():
        monitor_loop.start()
    if not summary_loop.is_running():
        summary_loop.start()

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    command_name = interaction.command.name if interaction.command else "unknown"
    if isinstance(error, app_commands.CommandNotFound):
        log.warning("Received stale slash command registration: /%s", command_name)
        try:
            await reply(
                interaction,
                "⚠️ That command is no longer available. Please refresh Discord and try again.",
                ephemeral=True,
            )
        except Exception as error:
            if isinstance(error, discord.HTTPException):
                log.warning("Could not respond to stale command /%s", command_name)
            else:
                log.exception("Could not respond to stale command /%s", command_name)
        return
    if isinstance(error, app_commands.MissingPermissions):
        log.warning("Permission denied for /%s: %s", command_name, error)
        try:
            await reply(interaction, "⚠️ You do not have permission to use this command.", ephemeral=True)
        except discord.HTTPException:
            log.exception("Could not send permission error for /%s", command_name)
    else:
        log.error("Unhandled application command error for /%s", command_name, exc_info=error)
        try:
            await reply(interaction, "⚠️ The command could not be completed. Please try again.", ephemeral=True)
        except Exception as response_error:
            if isinstance(response_error, discord.HTTPException):
                log.warning("Could not send command error for /%s", command_name)
            else:
                log.exception("Could not send command error for /%s", command_name)

@bot.tree.command(name="ban", description="Monitor an active Instagram account until it becomes unavailable.")
@app_commands.describe(username="Instagram username or profile URL")
async def ban(interaction: discord.Interaction, username: str):
    username = clean_username(username)
    if not username:
        return await reply(
            interaction,
            embed=error_embed("Invalid account", "Enter an Instagram username or profile URL."),
            ephemeral=True,
        )

    await defer_interaction(interaction)
    try:
        result = await checker.check(username)
    except TimeoutError:
        log.exception("/ban Instagram API timeout for @%s", username)
        return await interaction.followup.send("⚠️ Instagram took too long to respond. Please try again later.")
    except Exception:
        log.exception("/ban check failed for @%s", username)
        return await interaction.followup.send("⚠️ Instagram could not be checked right now. Please try again later.")
    if result.state == AccountState.UNKNOWN:
        return await interaction.followup.send(
            f"⚠️ I couldn't verify @{username} right now. Try again in a moment."
        )
    if result.state == AccountState.UNAVAILABLE:
        return await interaction.followup.send(
            f"⚠️ @{username} already appears unavailable. Use `/unban {username}` to monitor recovery."
        )

    db.add_monitor(username, "ban", interaction.channel_id, interaction.user.id, result, interaction.guild_id)
    embed = base_embed(f"Monitoring started • @{short_username(username)}", "The account is currently verified as active.", "monitoring")
    embed.add_field(name="Current status", value="`ACTIVE`", inline=True)
    embed.add_field(name="Watch", value="Until unavailable", inline=True)
    embed.add_field(name="Alerts", value="This channel and configured destinations", inline=False)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="unban", description="Monitor an unavailable Instagram account until it recovers.")
@app_commands.describe(username="Instagram username or profile URL")
async def unban(interaction: discord.Interaction, username: str):
    username = clean_username(username)
    if not username:
        return await reply(
            interaction,
            embed=error_embed("Invalid account", "Enter an Instagram username or profile URL."),
            ephemeral=True,
        )

    await defer_interaction(interaction)
    try:
        result = await checker.check(username)
    except TimeoutError:
        log.exception("/unban Instagram API timeout for @%s", username)
        return await interaction.followup.send("⚠️ Instagram took too long to respond. Please try again later.")
    except Exception:
        log.exception("/unban check failed for @%s", username)
        return await interaction.followup.send("⚠️ Instagram could not be checked right now. Please try again later.")
    if result.state == AccountState.UNKNOWN:
        return await interaction.followup.send(embed=base_embed(f"Unable to verify • @{short_username(username)}", "Instagram did not provide a reliable answer. This does not confirm a ban or deletion.", "neutral"))
    if result.state == AccountState.ACTIVE:
        return await interaction.followup.send(embed=base_embed(f"Already active • @{short_username(username)}", f"Use `/ban {short_username(username)}` to monitor a future unavailable transition.", "active"))

    db.add_monitor(username, "unban", interaction.channel_id, interaction.user.id, result, interaction.guild_id)
    embed = base_embed(f"Recovery monitoring started • @{short_username(username)}", "The account is currently verified as unavailable.", "monitoring")
    embed.add_field(name="Current status", value="`UNAVAILABLE`", inline=True)
    embed.add_field(name="Watch", value="Until active again", inline=True)
    embed.add_field(name="Alerts", value="This channel and configured destinations", inline=False)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="stop", description="Stop monitoring an Instagram account.")
@app_commands.describe(username="Instagram username or profile URL")
async def stop(interaction: discord.Interaction, username: str):
    username = clean_username(username)
    if not username:
        return await reply(
            interaction,
            embed=error_embed("Invalid account", "Enter an Instagram username or profile URL."),
            ephemeral=True,
        )
    await defer_interaction(interaction, ephemeral=True)
    removed = db.stop_monitor(username, interaction.guild_id)
    if not removed:
        return await interaction.followup.send(embed=empty_embed("Monitor not found", f"`@{short_username(username)}` is not currently monitored in this server."), ephemeral=True)
    embed = base_embed(f"Monitoring stopped • @{short_username(username)}", "No further automatic transition alerts will be sent.", "changed")
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="list", description="List all active Instagram monitors.")
async def list_monitors(interaction: discord.Interaction):
    await defer_interaction(interaction)
    try:
        monitors = db.get_monitors(interaction.guild_id)
        if not monitors:
            return await interaction.followup.send(
                embed=empty_embed(
                    "No active monitors",
                    "Use `/ban` or `/unban` to start watching an account.",
                )
            )

        rows = []
        for monitor in monitors:
            watch = "unavailable → active" if monitor.mode == "unban" else "active → unavailable"
            checked = format_timestamp(monitor.last_checked)
            rows.append(
                f"**[@{short_username(monitor.username)}]({account_url(monitor.username)})**\n"
                f"`{status_text(monitor.last_state.value)}` • `{watch}`\n"
                f"Followers: **{monitor.followers:,}** • Checked: {checked}"
            )
        pages = paged_embeds(
            f"Active monitors • {len(monitors)} account(s)",
            rows,
            status="monitoring",
        )
        view = PageView(pages) if len(pages) > 1 else None
        if view:
            await interaction.followup.send(embed=pages[0], view=view)
        else:
            await interaction.followup.send(embed=pages[0])
        if view:
            view.message = await interaction.original_response()
    except Exception:
        log.exception("/list failed")
        message = "⚠️ I couldn't load the monitor list. Check the bot logs for details."
        await interaction.followup.send(message, ephemeral=True)

@bot.tree.command(name="alert-channel", description="Add or remove an alert destination channel.")
@app_commands.describe(action="add or remove", channel="Channel to receive alerts")
@app_commands.choices(action=[app_commands.Choice(name="add", value="add"), app_commands.Choice(name="remove", value="remove")])
@app_commands.checks.has_permissions(manage_guild=True)
async def alert_channel(interaction: discord.Interaction, action: app_commands.Choice[str], channel: discord.TextChannel):
    await defer_interaction(interaction, ephemeral=True)
    try:
        if action.value == "add": db.add_destination(interaction.guild_id, channel.id)
        else: db.remove_destination(interaction.guild_id, channel.id)
    except Exception:
        log.exception("/alert-channel failed")
        return await interaction.followup.send("⚠️ I couldn't update the alert destination.", ephemeral=True)
    embed = base_embed("Alert destination updated", f"{channel.mention} is now {'included in' if action.value == 'add' else 'removed from'} automatic alerts.", "changed")
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="alert-config", description="Configure alert style, cooldown, roles, summaries, webhooks and DMs.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(style="embed or plain", cooldown="Cooldown seconds", roles="Comma-separated role IDs", everyone="Allow @everyone", dm="DM monitor owner", webhook="Webhook URL", summary="off, daily or weekly")
async def alert_config(interaction: discord.Interaction, style: str="embed", cooldown: int=0, roles: str="", everyone: bool=False, dm: bool=False, webhook: str="", summary: str="off"):
    await defer_interaction(interaction, ephemeral=True)
    guild = interaction.guild
    member = interaction.user if isinstance(interaction.user, discord.Member) else None
    channel = guild.get_channel(interaction.channel_id) if guild and interaction.channel_id else None
    if not guild or member is None or channel is None:
        return await interaction.followup.send("This command can only be used in a server.", ephemeral=True)
    if style not in ("embed", "plain") or summary not in ("off", "daily", "weekly") or cooldown < 0:
        return await interaction.followup.send("Invalid style, summary, or cooldown.", ephemeral=True)
    if everyone and not channel.permissions_for(member).mention_everyone:
        return await interaction.followup.send("You need the Mention Everyone permission to enable @everyone alerts.", ephemeral=True)
    role_ids = tuple(int(x.strip()) for x in roles.split(",") if x.strip().isdigit())
    if role_ids:
        missing = [role_id for role_id in role_ids if guild.get_role(role_id) is None]
        if missing:
            return await interaction.followup.send(f"Unknown role ID(s): {', '.join(map(str, missing))}", ephemeral=True)
    cfg = AlertConfig(guild.id, style, cooldown, role_ids, everyone, dm, webhook or None, summary, interaction.channel_id if summary != "off" else None)
    db.save_config(cfg)
    embed = base_embed("Alert configuration saved", "Future automatic notifications will use these settings.", "changed")
    embed.add_field(name="Style", value=f"`{style.upper()}`", inline=True)
    embed.add_field(name="Cooldown", value=f"{cooldown}s", inline=True)
    embed.add_field(name="Summary", value=f"`{summary.upper()}`", inline=True)
    embed.add_field(name="Mentions", value="Everyone enabled" if everyone else "Role mentions only", inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="history", description="Show recent monitoring events.")
@app_commands.describe(limit="Number of events to show (1-20)", username="Optional account filter", oldest_first="Show oldest events first")
async def history(interaction: discord.Interaction, limit: app_commands.Range[int, 1, 20] = 10, username: str = "", oldest_first: bool = False):
    await defer_interaction(interaction)
    try:
        username_filter: str | None = clean_username(username) if username else None
        rows = db.get_history(int(limit), interaction.guild_id, username_filter, oldest_first)
        if not rows:
            return await interaction.followup.send(embed=empty_embed("No monitoring history", "Confirmed account status changes will appear here after monitoring begins."))

        lines = []
        for row in rows:
            status = "UNAVAILABLE" if row["event"] == "banned" else "ACTIVE"
            details = f"{row['followers']:,} followers" if row['followers'] else "Follower count unavailable"
            lines.append(f"**@{short_username(row['username'])}** • `{status}`\n{format_timestamp(row['detected_at'])} • {details} • monitored {elapsed_text(row['elapsed_seconds'])}")
        pages = paged_embeds("Status history", lines, page_size=5, status="changed")
        view = PageView(pages) if len(pages) > 1 else None
        if view:
            await interaction.followup.send(embed=pages[0], view=view)
        else:
            await interaction.followup.send(embed=pages[0])
        if view:
            view.message = await interaction.original_response()
    except Exception:
        log.exception("/history failed")
        message = "⚠️ I couldn't load monitoring history. Check the bot logs for details."
        await interaction.followup.send(message, ephemeral=True)

@bot.tree.command(name="dashboard", description="Open the monitoring dashboard.")
async def dashboard(interaction: discord.Interaction):
    await defer_interaction(interaction)
    try:
        stats = db.get_statistics(interaction.guild_id)
    except sqlite3.Error:
        log.exception("/dashboard failed")
        return await interaction.followup.send(
            "⚠️ The dashboard could not be loaded. Please try again.",
            ephemeral=True,
        )
    embed = base_embed("Instagram Monitor Dashboard", "A quick view of monitoring, account health, and recent activity.", "monitoring")
    embed.add_field(name="Accounts monitored", value=str(stats["monitored"]), inline=True)
    embed.add_field(name="Active", value=str(stats["active"] or 0), inline=True)
    embed.add_field(name="Unavailable", value=str(stats["unavailable"] or 0), inline=True)
    embed.add_field(name="Followers monitored", value=f"{stats['followers']:,}", inline=True)
    embed.add_field(name="Confirmed unavailable events", value=str(stats["unavailable_events"]), inline=True)
    embed.add_field(name="Confirmed recovery events", value=str(stats["recovered_events"]), inline=True)
    embed.add_field(name="Navigation", value="Use `/list` for accounts, `/status` for details, and `/history` for activity.", inline=False)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="account", description="Show the saved monitoring details for an account.")
@app_commands.describe(username="Instagram username or profile URL")
async def account(interaction: discord.Interaction, username: str):
    await defer_interaction(interaction, ephemeral=True)
    username = clean_username(username)
    if not username:
        return await interaction.followup.send(embed=error_embed("Invalid account", "Enter an Instagram username or profile URL."), ephemeral=True)
    monitor = db.get_monitor(username, interaction.guild_id)
    if not monitor:
        return await interaction.followup.send(embed=empty_embed("Account not monitored", f"`@{short_username(username)}` is not currently monitored in this server."), ephemeral=True)
    embed = base_embed(f"Account details • @{short_username(username)}", f"[Open Instagram profile]({account_url(username)})", monitor.last_state.value)
    embed.add_field(name="Status", value=f"`{status_text(monitor.last_state.value)}`", inline=True)
    embed.add_field(name="Followers", value=f"{monitor.followers:,}", inline=True)
    embed.add_field(name="Watch", value="Recovery" if monitor.mode == "unban" else "Availability", inline=True)
    embed.add_field(name="Monitoring started", value=format_timestamp(monitor.started_at), inline=False)
    embed.add_field(name="Last checked", value=format_timestamp(monitor.last_checked), inline=False)
    if monitor.profile_pic_url:
        embed.set_thumbnail(url=monitor.profile_pic_url)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="status", description="Check the current status of an Instagram account.")
@app_commands.describe(username="Instagram username or profile URL")
async def status(interaction: discord.Interaction, username: str):
    username = clean_username(username)
    if not username:
        return await reply(
            interaction,
            embed=error_embed("Invalid account", "Enter an Instagram username or profile URL."),
            ephemeral=True,
        )
    await defer_interaction(interaction)
    try:
        result = await checker.check(username)
    except TimeoutError:
        log.exception("/status Instagram API timeout for @%s", username)
        return await interaction.followup.send("⚠️ Instagram took too long to respond. Please try again later.")
    except Exception:
        log.exception("/status check failed for @%s", username)
        return await interaction.followup.send("⚠️ Instagram could not be checked right now. Please try again later.")
    if result.state == AccountState.UNKNOWN:
        return await interaction.followup.send(embed=base_embed(f"Account status • @{short_username(username)}", "Instagram did not provide a reliable answer. This does not confirm a ban or deletion. Try again later.", "neutral"))
    profile = result.profile
    status = result.state.value
    embed = base_embed(f"Account status • @{short_username(username)}", "Latest verified Instagram check.", status if status in ("active", "unavailable") else "neutral")
    embed.add_field(name="Status", value=f"`{status_text(status)}`", inline=False)
    if profile:
        embed.add_field(name="Posts", value=str(profile.posts), inline=True)
        embed.add_field(name="Followers", value=profile.followers_display, inline=True)
        embed.add_field(name="Following", value=profile.following_display, inline=True)
        if profile.profile_pic_url:
            embed.set_thumbnail(url=profile.profile_pic_url)
        if profile.latest_post_url:
            caption = (profile.latest_post_caption or "View the latest post").replace("\n", " ")[:180]
            embed.add_field(name="Latest post", value=f"[{caption}]({profile.latest_post_url})", inline=False)
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
            embed = base_embed(f"{cfg.summary_frequency.title()} monitoring summary", f"{len(rows)} confirmed status change(s) since the previous summary.", "changed")
            for row in rows[:20]:
                status = "UNAVAILABLE" if row["event"] == "banned" else "ACTIVE"
                embed.add_field(name=f"@{short_username(row['username'])} • {status}", value=f"{format_timestamp(row['detected_at'])}\nMonitored for {elapsed_text(row['elapsed_seconds'])}", inline=False)
            await channel.send(embed=embed)
        else:
            await channel.send(embed=empty_embed(f"{cfg.summary_frequency.title()} monitoring summary", "No confirmed monitoring changes were recorded."))
        db.mark_summary_sent(cfg.guild_id, now)

@tasks.loop(seconds=CHECK_INTERVAL)
async def monitor_loop():
    # Filter by guild here so a server never receives another server's alerts.
    monitors = db.get_monitors()
    for monitor in monitors:
        try:
            result = await checker.check(monitor.username)
            if result.state == AccountState.UNKNOWN:
                log.warning("Could not verify @%s", monitor.username)
                continue

            previous = monitor.last_state
            db.update_last_check(monitor.username, result, monitor.guild_id)

            if monitor.mode == "ban" and previous == AccountState.ACTIVE and result.state == AccountState.UNAVAILABLE:
                # Confirm transition before alerting.
                confirmed = await fetch_confirmed(monitor.username, AccountState.UNAVAILABLE)
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
                db.stop_monitor(monitor.username, monitor.guild_id)

            elif monitor.mode == "unban" and previous == AccountState.UNAVAILABLE and result.state == AccountState.ACTIVE:
                confirmed = await fetch_confirmed(monitor.username, AccountState.ACTIVE)
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
                db.stop_monitor(monitor.username, monitor.guild_id)
        except Exception:
            log.exception("Monitor error for @%s", monitor.username)

@monitor_loop.before_loop
async def before_monitor_loop():
    await bot.wait_until_ready()

async def run_bot() -> None:
    delay = RECONNECT_MIN_SECONDS
    while True:
        connection_ready.clear()
        try:
            await bot.start(TOKEN, reconnect=True)
            log.warning("Discord connection closed; reconnecting in %ss.", delay)
        except discord.LoginFailure:
            log.critical(
                "Discord rejected DISCORD_TOKEN. Replace it with a newly copied bot "
                "token without quotes or extra whitespace."
            )
            raise
        except Exception as error:
            if not is_transient_discord_error(error):
                raise
            log.warning(
                "Transient Discord connection failure (%s); reconnecting in %ss.",
                type(error).__name__,
                delay,
            )
        finally:
            if not bot.is_closed():
                await bot.close()
        if connection_ready.is_set():
            delay = RECONNECT_MIN_SECONDS
        await asyncio.sleep(delay)
        delay = min(RECONNECT_MAX_SECONDS, delay * 2)

if __name__ == "__main__":
    try:
        asyncio.run(run_bot())
    except discord.LoginFailure:
        raise SystemExit(2)

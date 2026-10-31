# 🇺🇸 Homelander Instagram Monitor Bot

A Discord slash-command bot that monitors Instagram usernames and automatically posts a Discord alert when an account changes between **active** and **unavailable**.

## Features

- `/ban username` — monitor an active account until it becomes unavailable.
- `/unban username` — monitor an unavailable account until it becomes active again.
- `/stop username` — stop monitoring.
- `/list` — list active monitors.
- `/history` — show recent events.
- `/status username` — check current status.
- SQLite persistence.
- Auto-resume after bot restart.
- Automatic Discord alerts.
- Instagram-style profile card with profile photo and statistics.
- Confirmation checks to reduce false ban/recovery alerts.
- Multiple alert channels, per-server styles (`embed`/`plain`), cooldowns, selected role mentions, permission-gated `@everyone`, DMs, webhooks, and daily/weekly summary configuration.

## Important Instagram limitation

Instagram does not provide a simple official public API that says "this arbitrary username is banned." The checker therefore treats temporary connection/rate-limit/login errors as **UNKNOWN**, not as a ban. A real ban/unavailability signal is confirmed again before the bot sends a transition alert.

Because Instagram can change its web behavior and rate limits, no third-party checker can guarantee 100% accuracy or uptime.

## 1. Create the Discord application

1. Open the Discord Developer Portal.
2. Create a New Application.
3. Open **Bot** and create the bot user.
4. Copy the bot token.
5. Under OAuth2 / Installation, invite it to your server with:
   - `bot`
   - `applications.commands`
6. Give it permission to send messages and attach files.

Keep the token private.

## 2. Install

Python 3.11+ is recommended.

```bash
python -m venv .venv
```

Windows:
```bash
.venv\Scripts\activate
```

Linux/macOS:
```bash
source .venv/bin/activate
```

Then:

```bash
pip install -r requirements.txt
```

## 3. Configure

Copy:

```text
.env.example
```

to:

```text
.env
```

Set:

```env
DISCORD_TOKEN=your_token
DISCORD_GUILD_ID=your_server_id
```

`DISCORD_GUILD_ID` is optional, but recommended while testing because guild slash commands sync quickly.

## 4. Run

```bash
python bot.py
```

When connected, the bot will load the SQLite database and automatically resume existing monitors.

## 5. Commands

```text
/ban username
/unban username
/stop username
/list
/history
/status username
/alert-channel action:<add|remove> channel:<channel>
/alert-config style:<embed|plain> cooldown:<seconds> roles:<role IDs> everyone:<true|false> dm:<true|false> webhook:<URL> summary:<off|daily|weekly>
```

`/alert-channel` and `/alert-config` require **Manage Server**. Role IDs are comma-separated. `@everyone` is only emitted when explicitly enabled; Discord permissions and channel overwrites still apply. Webhooks should be kept private. Summaries are stored with the selected cadence and channel; schedule delivery can be added without changing monitor behavior.

You can also pass an Instagram profile URL where a username is accepted.

## Hosting

The project is intentionally simple: run `python bot.py` as a long-running worker.

If your host has an ephemeral filesystem, SQLite may be deleted when the instance is recreated. Use persistent storage or replace `database.py` with a hosted database such as PostgreSQL.

### Docker

The included `Dockerfile` runs the bot as a worker:

```bash
docker build -t instagram-monitor .
docker run --env-file .env -v instagram-monitor-data:/app instagram-monitor
```

Configure `DISCORD_TOKEN` and the other environment variables in the host's secret/environment settings. Do not commit `.env` or `monitor.db`.

### Render

The included `render.yaml` defines a Docker background worker with a persistent disk for SQLite. In Render, choose **New → Blueprint**, connect this repository, and apply the blueprint. Set `DISCORD_TOKEN` and `DISCORD_GUILD_ID` when prompted. The service uses Render's Starter worker plan because background workers and persistent disks are not available on the free web-service plan.

## GitHub

Do NOT commit:

```text
.env
monitor.db
```

The included `.gitignore` prevents both.

## Troubleshooting

### Slash commands do not appear

Set `DISCORD_GUILD_ID` to the ID of your Discord server and restart the bot.

### Instagram says UNKNOWN

Instagram may be rate-limiting the host or requiring a logged-in session. Do not interpret UNKNOWN as a ban.

### Too many false alerts

Increase:

```env
CONFIRMATION_CHECKS=3
CHECK_INTERVAL_SECONDS=180
```

A shorter interval increases requests and may increase rate limiting.

### Profile picture/card is missing

The bot will still send the Discord alert. The card generator uses the public profile-picture URL when Instagram provides one.

## License

Use and modify this project for your own Discord server. Respect Instagram's terms, rate limits, and applicable laws.

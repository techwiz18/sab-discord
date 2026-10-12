"""Single-purpose Discord inactivity pinger.

Watches one channel. If no real activity for IDLE_MINUTES or more,
pings one role. Timer resets on any non-self message; the bot's own
pings never reset the timer, so a still-idle channel gets re-pinged
every IDLE_MINUTES.

Designed for CubeCoders AMP -> Python App Runner:
  App Run Mode: Python script
  App Module Name: bot.py
  Packages Install Method: Requirements.txt file
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import sys

# AMP captures stdout through a pipe, where CPython block-buffers by
# default. Force line buffering first so startup errors are visible in
# the AMP console instead of dying silently in the buffer.
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

print("sab-discord: starting...", flush=True)

import discord
from discord.ext import tasks
from dotenv import load_dotenv

print(f"sab-discord: imports ok, cwd={os.getcwd()}", flush=True)
load_dotenv()
print("sab-discord: .env loaded (if present)", flush=True)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("sab-discord")


def get_env(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.getenv(name, default)
    if required and not value:
        msg = f"sab-discord: FATAL: missing required env var: {name}"
        print(msg, flush=True)
        print(msg, file=sys.stderr, flush=True)
        log.error("Missing required env var: %s", name)
        sys.exit(1)
    return value


TOKEN = get_env("DISCORD_TOKEN", required=True)
CHANNEL_ID = int(get_env("CHANNEL_ID", required=True))  # type: ignore[arg-type]
ROLE_ID = int(get_env("ROLE_ID", required=True))  # type: ignore[arg-type]
GUILD_ID_RAW = os.getenv("GUILD_ID")
GUILD_ID = int(GUILD_ID_RAW) if GUILD_ID_RAW else None

IDLE_MINUTES = float(os.getenv("IDLE_MINUTES", "60"))
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))
PING_TEXT = os.getenv("PING_TEXT", "Checking in — anything needed here?")
# How far back to scan to find the last non-self message.
HISTORY_SCAN_LIMIT = int(os.getenv("HISTORY_SCAN_LIMIT", "50"))

if IDLE_MINUTES <= 0:
    log.error("IDLE_MINUTES must be > 0")
    sys.exit(1)
if CHECK_INTERVAL_SECONDS < 30:
    log.warning("CHECK_INTERVAL_SECONDS=%s is very low; using 30s minimum.", CHECK_INTERVAL_SECONDS)
    CHECK_INTERVAL_SECONDS = 30

intents = discord.Intents.default()
intents.guilds = True
intents.guild_messages = True
# No privileged Message Content intent needed: we only use timestamps/authors.

client = discord.Client(intents=intents)


def idle_seconds(last_activity: datetime.datetime, now: datetime.datetime) -> float:
    return (now - last_activity).total_seconds()


async def find_last_human_message(
    channel: discord.TextChannel, self_id: int | None
) -> discord.Message | None:
    """Return newest message in channel not sent by this bot, or None."""
    async for msg in channel.history(limit=HISTORY_SCAN_LIMIT):
        if self_id is not None and msg.author.id == self_id:
            continue
        return msg
    return None


@client.event
async def on_ready() -> None:
    assert client.user is not None
    log.info("Logged in as %s (id=%s)", client.user, client.user.id)

    channel = client.get_channel(CHANNEL_ID)
    if channel is None:
        try:
            channel = await client.fetch_channel(CHANNEL_ID)
        except (discord.NotFound, discord.Forbidden) as exc:
            log.error("Cannot access CHANNEL_ID=%s: %s", CHANNEL_ID, exc)
            return
    log.info(
        "Watching channel_id=%s idle_threshold=%.1fm check_every=%ss role_id=%s",
        CHANNEL_ID,
        IDLE_MINUTES,
        CHECK_INTERVAL_SECONDS,
        ROLE_ID,
    )
    if GUILD_ID is not None and client.get_guild(GUILD_ID) is None:
        log.warning("GUILD_ID=%s not found in cache; continuing with channel-only mode.", GUILD_ID)

    if not inactivity_check.is_running():
        inactivity_check.change_interval(seconds=CHECK_INTERVAL_SECONDS)
        inactivity_check.start()


@tasks.loop(seconds=300)
async def inactivity_check() -> None:
    assert client.user is not None
    now = datetime.datetime.now(datetime.timezone.utc)

    try:
        channel = client.get_channel(CHANNEL_ID)
        if channel is None:
            channel = await client.fetch_channel(CHANNEL_ID)
    except (discord.NotFound, discord.Forbidden) as exc:
        log.error("Channel %s unavailable: %s", CHANNEL_ID, exc)
        return
    except discord.HTTPException as exc:
        log.warning("Fetch channel failed (transient): %s", exc)
        return

    if not isinstance(channel, discord.TextChannel):
        log.error("CHANNEL_ID=%s is not a text channel (got %s).", CHANNEL_ID, type(channel).__name__)
        return

    try:
        last_human = await find_last_human_message(channel, client.user.id)
    except (discord.Forbidden, discord.HTTPException) as exc:
        log.warning("History read failed: %s", exc)
        return

    if last_human is None:
        # Channel has only our own pings (or is empty) within scan window:
        # treat as idle so a first/repeat ping still goes out.
        log.info("No human messages in last %d; treating channel as idle.", HISTORY_SCAN_LIMIT)
        await send_ping(channel, reason="no-human-history")
        return

    last_ts = last_human.created_at
    if last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=datetime.timezone.utc)
    idle_min = idle_seconds(last_ts, now) / 60.0

    if idle_min >= IDLE_MINUTES:
        log.info(
            "Channel idle %.1fm (>= %.1fm, last by %s at %s). Pinging role %s.",
            idle_min,
            IDLE_MINUTES,
            last_human.author,
            last_ts.isoformat(),
            ROLE_ID,
        )
        await send_ping(channel, reason=f"idle-{idle_min:.1f}m")
    else:
        log.debug("Channel active: idle %.1fm (threshold %.1fm).", idle_min, IDLE_MINUTES)


async def send_ping(channel: discord.TextChannel, reason: str) -> None:
    # Role mention requires the role to be mentionable by the bot
    # (or bot has Mention Everyone perm). <@&id> works without fetching.
    content = f"<@&{ROLE_ID}> {PING_TEXT}"
    try:
        await channel.send(content, allowed_mentions=discord.AllowedMentions(roles=True))
        log.info("Sent idle ping (%s).", reason)
    except discord.Forbidden:
        log.error(
            "Missing permission to send/mention in channel %s. Need Send Messages + "
            "role mentionable (or Mention Everyone).",
            channel.id,
        )
    except discord.HTTPException as exc:
        log.warning("Send ping failed (transient): %s", exc)


@inactivity_check.before_loop
async def _wait_ready() -> None:
    await client.wait_until_ready()


def main() -> None:
    assert TOKEN is not None
    try:
        client.run(TOKEN, log_handler=None)
    except discord.LoginFailure:
        log.error("Login failed: bad DISCORD_TOKEN.")
        sys.exit(1)
    except KeyboardInterrupt:
        asyncio.get_event_loop().run_until_complete(client.close())


if __name__ == "__main__":
    main()

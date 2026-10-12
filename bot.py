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
import random
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
from discord import app_commands
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
# Option A: ||-separated messages in .env (takes priority, survives AMP Updates).
PING_MESSAGES_RAW = os.getenv("PING_MESSAGES", "")
MESSAGE_DELIMITER = os.getenv("MESSAGE_DELIMITER", "||")
# Option B: one message per line in this file, reloaded on every ping so
# edits apply without restarting. Blank lines and # comments are ignored.
# This is the LOCAL file (gitignored): auto-created from the shipped
# messages.example.txt on first run if missing, never touched by updates.
MESSAGES_FILE = os.getenv("MESSAGES_FILE", "messages.txt")
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EXAMPLE_FILE = os.path.join(_BASE_DIR, "messages.example.txt")
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
    log.info("Loaded %d ping message(s).", len(load_messages()))

    if not inactivity_check.is_running():
        inactivity_check.change_interval(seconds=CHECK_INTERVAL_SECONDS)
        inactivity_check.start()
    await _sync_commands()


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
    text = pick_message()
    content = f"<@&{ROLE_ID}> {text}"
    try:
        await channel.send(content, allowed_mentions=discord.AllowedMentions(roles=True))
        log.info("Sent idle ping (%s): %r", reason, text[:80])
    except discord.Forbidden:
        log.error(
            "Missing permission to send/mention in channel %s. Need Send Messages + "
            "role mentionable (or Mention Everyone).",
            channel.id,
        )
    except discord.HTTPException as exc:
        log.warning("Send ping failed (transient): %s", exc)


_last_sent_message: str | None = None


_LIVE_HEADER = (
    "# Your ping messages — edit this file freely, one message per line!\n"
    "# Blank lines and lines starting with # are ignored.\n"
    "# Re-read on every ping (no restart needed) and never touched by updates.\n"
)
_STALE_MARKER = "DO NOT EDIT THIS FILE"


def _read_message_lines(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]


def _ensure_messages_file() -> None:
    """Seed MESSAGES_FILE from the shipped example if it doesn't exist.

    This is the "ignore if already exists" rule: an existing file is never
    modified, so local edits survive AMP Updates (the live file is also
    gitignored, so pulls can't conflict with it either).
    """
    if os.path.exists(MESSAGES_FILE):
        _repair_stale_header()
        return
    try:
        messages = _read_message_lines(EXAMPLE_FILE)
    except OSError:
        return  # no example shipped; caller falls back to PING_TEXT
    if not messages:
        return
    try:
        with open(MESSAGES_FILE, "w", encoding="utf-8") as dst:
            dst.write(_LIVE_HEADER + "\n".join(messages) + "\n")
        log.info("Created %s from shipped defaults; edit it freely.", MESSAGES_FILE)
    except OSError as exc:
        log.warning("Could not seed %s: %s", MESSAGES_FILE, exc)


def _repair_stale_header() -> None:
    """Fix live files seeded before the header bug was fixed.

    Only touches files containing the example file's "DO NOT EDIT" marker
    (which could only have gotten there via the old verbatim copy); message
    lines are preserved, only the comment header is replaced.
    """
    try:
        with open(MESSAGES_FILE, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return
    if _STALE_MARKER not in content:
        return
    messages = [line.strip() for line in content.splitlines()
                if line.strip() and not line.strip().startswith("#")]
    if not messages:
        return  # nothing worth preserving; leave the file alone
    try:
        with open(MESSAGES_FILE, "w", encoding="utf-8") as dst:
            dst.write(_LIVE_HEADER + "\n".join(messages) + "\n")
        log.info("Fixed outdated header in %s; your messages were preserved.", MESSAGES_FILE)
    except OSError as exc:
        log.warning("Could not repair header in %s: %s", MESSAGES_FILE, exc)


def load_messages() -> list[str]:
    """Resolve the ping-message pool.

    Priority: PING_MESSAGES env (||-separated) -> MESSAGES_FILE
    (one per line, # comments/blank lines ignored, re-read every ping)
    -> PING_TEXT fallback. Never returns an empty list.
    """
    if PING_MESSAGES_RAW.strip():
        pooled = [m.strip() for m in PING_MESSAGES_RAW.split(MESSAGE_DELIMITER)]
        pooled = [m for m in pooled if m]
        if pooled:
            return pooled
        log.warning("PING_MESSAGES set but parsed empty; falling through to %s.", MESSAGES_FILE)
    _ensure_messages_file()
    try:
        with open(MESSAGES_FILE, "r", encoding="utf-8") as f:
            pooled = []
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                pooled.append(line)
            if pooled:
                return pooled
            log.warning("MESSAGES_FILE=%s has no usable lines; using PING_TEXT.", MESSAGES_FILE)
    except FileNotFoundError:
        log.debug("MESSAGES_FILE=%s not found; using PING_TEXT.", MESSAGES_FILE)
    except OSError as exc:
        log.warning("Could not read MESSAGES_FILE=%s: %s", MESSAGES_FILE, exc)
    return [PING_TEXT]


def pick_message() -> str:
    """Pick a random message, avoiding an immediate back-to-back repeat."""
    global _last_sent_message
    pooled = load_messages()
    if len(pooled) == 1:
        choice = pooled[0]
    else:
        choice = random.choice(pooled)
        if choice == _last_sent_message:
            choice = random.choice(pooled)  # one re-roll is enough
    _last_sent_message = choice
    return choice


@inactivity_check.before_loop
async def _wait_ready() -> None:
    await client.wait_until_ready()


# --- slash-command configuration (no website needed) ---

tree = app_commands.CommandTree(client)
_commands_synced = False

MAX_FILE_MESSAGES = int(os.getenv("MAX_FILE_MESSAGES", "50"))
MAX_MESSAGE_CHARS = 1500
_manager_role_ids = {
    int(x) for x in os.getenv("MANAGER_ROLE_IDS", "").replace(",", " ").split() if x.strip().isdigit()
}


def pool_source() -> str:
    """Where the current pool comes from (for /messages list and /ping-status)."""
    if PING_MESSAGES_RAW.strip():
        return "PING_MESSAGES in .env"
    _ensure_messages_file()
    try:
        if _read_message_lines(MESSAGES_FILE):
            return MESSAGES_FILE
    except OSError:
        pass
    return "PING_TEXT fallback in .env"


def _is_manager(user: discord.abc.User) -> bool:
    if not isinstance(user, discord.Member):
        return False
    if _manager_role_ids and any(r.id in _manager_role_ids for r in user.roles):
        return True
    return bool(user.guild_permissions.administrator)


async def _require_manager(interaction: discord.Interaction) -> bool:
    if _is_manager(interaction.user):
        return True
    await interaction.response.send_message(
        "Only server managers can use this command.", ephemeral=True
    )
    return False


async def _deny_when_env_pool(interaction: discord.Interaction) -> bool:
    """Stop file edits that would be silently shadowed by PING_MESSAGES."""
    if PING_MESSAGES_RAW.strip():
        await interaction.response.send_message(
            "PING_MESSAGES is set in .env and takes priority, so file changes "
            "would have no effect. Clear it (and restart the bot) to manage "
            "messages with these commands.",
            ephemeral=True,
        )
        return True
    return False


def add_file_message(text: str) -> int:
    """Append a message to the file pool; return the new message count."""
    _ensure_messages_file()
    with open(MESSAGES_FILE, "r", encoding="utf-8") as f:
        content = f.read()
    if content and not content.endswith("\n"):
        content += "\n"
    with open(MESSAGES_FILE, "w", encoding="utf-8") as f:
        f.write(content + text.strip() + "\n")
    return len(_read_message_lines(MESSAGES_FILE))


def remove_file_message(index1: int) -> str:
    """Remove the 1-based Nth message from the file pool; return removed text."""
    _ensure_messages_file()
    with open(MESSAGES_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()
    positions = [i for i, line in enumerate(lines) if line.strip() and not line.strip().startswith("#")]
    if not 1 <= index1 <= len(positions):
        raise IndexError(f"There are {len(positions)} message(s); pick 1-{len(positions)}.")
    removed = lines.pop(positions[index1 - 1]).strip()
    with open(MESSAGES_FILE, "w", encoding="utf-8") as f:
        f.writelines(lines)
    return removed


sab_group = app_commands.Group(
    name="sab", description="SAB activity pinger controls (managers only)"
)
msg_group = app_commands.Group(name="messages", description="Manage the random ping messages")


@msg_group.command(name="list", description="Show the ping messages currently in rotation (managers only)")
async def messages_list(interaction: discord.Interaction) -> None:
    if not await _require_manager(interaction):
        return
    pool = load_messages()
    body = "\n".join(f"{i}. {m}" for i, m in enumerate(pool, 1))
    if len(body) > 1800:
        body = body[:1800] + "…"
    await interaction.response.send_message(
        f"Ping messages ({len(pool)}, source: {pool_source()}):\n{body}",
        ephemeral=True,
    )


@msg_group.command(name="add", description="Add a ping message (managers only)")
@app_commands.describe(text="The message to add to the rotation")
async def messages_add(interaction: discord.Interaction, text: str) -> None:
    if not await _require_manager(interaction):
        return
    if await _deny_when_env_pool(interaction):
        return
    text = text.strip()
    if not text:
        await interaction.response.send_message("Message can't be blank.", ephemeral=True)
        return
    if len(text) > MAX_MESSAGE_CHARS:
        await interaction.response.send_message(
            f"Too long ({len(text)} chars, max {MAX_MESSAGE_CHARS}).", ephemeral=True
        )
        return
    try:
        count = add_file_message(text)
    except OSError as exc:
        await interaction.response.send_message(f"Couldn't save: {exc}", ephemeral=True)
        return
    if count > MAX_FILE_MESSAGES:
        try:
            remove_file_message(count)  # roll back
        except (OSError, IndexError):
            pass
        await interaction.response.send_message(
            f"Pool is full ({MAX_FILE_MESSAGES} messages max).", ephemeral=True
        )
        return
    await interaction.response.send_message(
        f"Added as message #{count} — live on the next ping, no restart needed.",
        ephemeral=True,
    )


@msg_group.command(name="remove", description="Remove a ping message by number (managers only)")
@app_commands.describe(index="The message number from /messages list")
async def messages_remove(interaction: discord.Interaction, index: int) -> None:
    if not await _require_manager(interaction):
        return
    if await _deny_when_env_pool(interaction):
        return
    try:
        removed = remove_file_message(index)
    except IndexError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return
    except OSError as exc:
        await interaction.response.send_message(f"Couldn't save: {exc}", ephemeral=True)
        return
    await interaction.response.send_message(f"Removed #{index}: {removed[:200]}", ephemeral=True)


tree.add_command(sab_group)
sab_group.add_command(msg_group)


@sab_group.command(name="status", description="Show the inactivity pinger's status (managers only)")
async def sab_status(interaction: discord.Interaction) -> None:
    if not await _require_manager(interaction):
        return
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        channel = client.get_channel(CHANNEL_ID)
        if channel is None:
            channel = await client.fetch_channel(CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            raise discord.NotFound("watched channel", "not a text channel")
        last_human = await find_last_human_message(channel, client.user.id if client.user else None)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
        await interaction.response.send_message(
            f"Can't read the watched channel: {exc}", ephemeral=True
        )
        return
    pool = load_messages()
    if last_human is None:
        activity = "no human messages found — channel counts as idle"
        verdict = "would ping on the next check"
    else:
        ts = last_human.created_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        mins = idle_seconds(ts, now) / 60
        activity = f"last activity {mins:.0f}m ago by {last_human.author}"
        verdict = "would ping on the next check" if mins >= IDLE_MINUTES else "no ping due yet"
    await interaction.response.send_message(
        f"Watching <#{CHANNEL_ID}> for <@&{ROLE_ID}>\n"
        f"Idle threshold: {IDLE_MINUTES:.0f}m (checked every {CHECK_INTERVAL_SECONDS}s)\n"
        f"{activity} — {verdict}\n"
        f"Messages in rotation: {len(pool)} (source: {pool_source()})",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def _sync_commands() -> None:
    global _commands_synced
    if _commands_synced:
        return
    try:
        if GUILD_ID is not None:
            # Guild-scoped sync lands instantly (global sync can take up to an hour).
            synced = await tree.sync(guild=discord.Object(id=GUILD_ID))
            log.info("Synced %d command(s) to guild %s.", len(synced), GUILD_ID)
        else:
            synced = await tree.sync()
            log.info("Synced %d command(s) globally.", len(synced))
    except (discord.HTTPException, discord.Forbidden) as exc:
        log.warning("Slash command sync failed: %s", exc)
    _commands_synced = True


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

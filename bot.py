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
import json
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


def _optional_id(name: str) -> int | None:
    """Read an optional Discord ID: blank/missing means unconfigured (dormant)."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        log.warning("Ignoring invalid %s=%r (not a number); treating as blank.", name, raw)
        return None
    if value <= 0:
        log.warning("Ignoring invalid %s=%r (not positive); treating as blank.", name, raw)
        return None
    return value


CHANNEL_ID = _optional_id("CHANNEL_ID")
ROLE_ID = _optional_id("ROLE_ID")
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

# Boot defaults: what .env says. Command-set overrides persist in CONFIG_FILE
# (gitignored, survives restarts/updates) and win over these until `sab reset`.
_BOOT_IDLE_MINUTES = IDLE_MINUTES
_BOOT_CHECK_INTERVAL = CHECK_INTERVAL_SECONDS
_BOOT_CHANNEL_ID = CHANNEL_ID
_BOOT_ROLE_ID = ROLE_ID
CONFIG_FILE = os.getenv("CONFIG_FILE", "sab-config.json")
MAX_THRESHOLD_MINUTES = 10080.0  # one week
MIN_INTERVAL_SECONDS = 30
MAX_INTERVAL_SECONDS = 86400  # one day
_config_overrides: dict = {}


def _load_config_overrides() -> dict:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("Ignoring unreadable %s (%s); using .env values.", CONFIG_FILE, exc)
        return {}
    if not isinstance(data, dict):
        log.warning("Ignoring %s (not an object); using .env values.", CONFIG_FILE)
        return {}
    valid: dict = {}
    idle = data.get("idle_minutes")
    if isinstance(idle, (int, float)) and 0 < idle <= MAX_THRESHOLD_MINUTES:
        valid["idle_minutes"] = float(idle)
    elif idle is not None:
        log.warning("Ignoring out-of-range idle_minutes in %s.", CONFIG_FILE)
    interval = data.get("check_interval_seconds")
    if isinstance(interval, int) and MIN_INTERVAL_SECONDS <= interval <= MAX_INTERVAL_SECONDS:
        valid["check_interval_seconds"] = interval
    elif interval is not None:
        log.warning("Ignoring out-of-range check_interval_seconds in %s.", CONFIG_FILE)
    for key in ("channel_id", "role_id"):
        value = data.get(key)
        if isinstance(value, int) and value > 0:
            valid[key] = value
        elif value is not None:
            log.warning("Ignoring invalid %s in %s.", key, CONFIG_FILE)
    return valid


def _save_config_overrides() -> None:
    tmp = CONFIG_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_config_overrides, f, indent=2)
            f.write("\n")
        os.replace(tmp, CONFIG_FILE)
    except OSError as exc:
        log.warning("Could not persist %s: %s", CONFIG_FILE, exc)


def _apply_config_overrides() -> None:
    global IDLE_MINUTES, CHECK_INTERVAL_SECONDS, CHANNEL_ID, ROLE_ID
    if "idle_minutes" in _config_overrides:
        IDLE_MINUTES = _config_overrides["idle_minutes"]
    if "check_interval_seconds" in _config_overrides:
        CHECK_INTERVAL_SECONDS = _config_overrides["check_interval_seconds"]
    if "channel_id" in _config_overrides:
        CHANNEL_ID = _config_overrides["channel_id"]
    if "role_id" in _config_overrides:
        ROLE_ID = _config_overrides["role_id"]


def _set_override(key: str, value: float | int) -> None:
    """Persist one override key and apply it to the matching global."""
    _config_overrides[key] = value
    _save_config_overrides()
    _apply_config_overrides()


def _config_marker(current: float | int, boot: float | int) -> str:
    return " (override — sab reset restores .env)" if current != boot else " (.env)"


def _is_configured() -> bool:
    return CHANNEL_ID is not None and ROLE_ID is not None


_config_overrides = _load_config_overrides()
_apply_config_overrides()
if _config_overrides:
    log.info(
        "Loaded command overrides from %s: %s (.env defaults: %sm/%ss/channel=%s/role=%s)",
        CONFIG_FILE,
        _config_overrides,
        _BOOT_IDLE_MINUTES,
        _BOOT_CHECK_INTERVAL,
        _BOOT_CHANNEL_ID,
        _BOOT_ROLE_ID,
    )

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

    channel = None
    if _is_configured():
        assert CHANNEL_ID is not None
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
    else:
        log.info("Not configured (blank channel/role) — dormant. Point me at a channel with sab setup.")
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

    paused, _ = pause_state()
    if paused:
        log.debug("Monitoring paused; skipping check.")
        return

    if not _is_configured():
        log.debug("Not configured; skipping check.")
        return

    assert CHANNEL_ID is not None
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
    name="sab", description="Pinger controls (server admins only)"
)
msg_group = app_commands.Group(name="messages", description="Ping messages")


@msg_group.command(name="list", description="See the current ping messages")
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


@msg_group.command(name="add", description="Add a new ping message")
@app_commands.describe(text="The new message")
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


@msg_group.command(name="remove", description="Remove a ping message")
@app_commands.describe(index="Message number from the list")
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


@sab_group.command(name="status", description="Check if the pinger is working")
async def sab_status(interaction: discord.Interaction) -> None:
    if not await _require_manager(interaction):
        return
    pool = load_messages()
    _, monitoring = pause_state()
    if not _is_configured():
        await interaction.response.send_message(
            f"Monitoring: {monitoring} but not watching anything — dormant.\n"
            f"Run sab setup to point me at a channel and role.\n"
            f"Messages in rotation: {len(pool)} (source: {pool_source()})",
            ephemeral=True,
        )
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
    _, monitoring = pause_state()
    if last_human is None:
        activity = "no human messages found — channel counts as idle"
        verdict = "on hold while paused" if _monitoring_paused else "would ping on the next check"
    else:
        ts = last_human.created_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        mins = idle_seconds(ts, now) / 60
        activity = f"last activity {mins:.0f}m ago by {last_human.author}"
        if _monitoring_paused:
            verdict = "on hold while paused"
        else:
            verdict = "would ping on the next check" if mins >= IDLE_MINUTES else "no ping due yet"
    await interaction.response.send_message(
        f"Watching <#{CHANNEL_ID}>{_config_marker(CHANNEL_ID or 0, _BOOT_CHANNEL_ID or 0)} for <@&{ROLE_ID}>{_config_marker(ROLE_ID or 0, _BOOT_ROLE_ID or 0)}\n"
        f"Monitoring: {monitoring}\n"
        f"Idle threshold: {IDLE_MINUTES:g}m{_config_marker(IDLE_MINUTES, _BOOT_IDLE_MINUTES)}\n"
        f"Check interval: {CHECK_INTERVAL_SECONDS}s{_config_marker(CHECK_INTERVAL_SECONDS, _BOOT_CHECK_INTERVAL)}\n"
        f"{activity} — {verdict}\n"
        f"Messages in rotation: {len(pool)} (source: {pool_source()})",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


# --- pause / resume (in-memory; a restart always comes back ACTIVE) ---

_monitoring_paused = False
_pause_reason: str | None = None
_pause_until: datetime.datetime | None = None  # aware UTC; None = indefinite


def pause_state() -> tuple[bool, str]:
    """(paused, human-readable description). Timed pauses auto-expire."""
    global _monitoring_paused, _pause_reason, _pause_until
    now = datetime.datetime.now(datetime.timezone.utc)
    if _monitoring_paused and _pause_until is not None and now >= _pause_until:
        _monitoring_paused = False
        _pause_reason = None
        _pause_until = None
        log.info("Pause expired; monitoring resumed.")
    if not _monitoring_paused:
        return False, "ACTIVE"
    desc = "PAUSED"
    if _pause_reason:
        desc += f" ({_pause_reason})"
    if _pause_until is not None:
        mins = max(0, int((_pause_until - now).total_seconds() // 60))
        desc += f", auto-resumes in ~{mins}m"
    else:
        desc += ", indefinite"
    return True, desc


@sab_group.command(name="pause", description="Pause pinging for a while")
@app_commands.describe(
    minutes="Come back after this many minutes (leave empty to stay paused)",
    reason="Note to show why it's paused",
)
async def sab_pause(
    interaction: discord.Interaction,
    minutes: int | None = None,
    reason: str | None = None,
) -> None:
    if not await _require_manager(interaction):
        return
    if minutes is not None and minutes <= 0:
        await interaction.response.send_message("Minutes must be positive.", ephemeral=True)
        return
    global _monitoring_paused, _pause_reason, _pause_until
    _monitoring_paused = True
    _pause_reason = (reason or "").strip() or None
    _pause_until = (
        datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=minutes)
        if minutes is not None
        else None
    )
    _, desc = pause_state()
    log.info("Monitoring paused by %s: %s", interaction.user, desc)
    tail = " Use sab resume to resume early." if minutes is not None else ""
    await interaction.response.send_message(f"Pinging paused ({desc}).{tail}", ephemeral=True)


@sab_group.command(name="resume", description="Start pinging again")
async def sab_resume(interaction: discord.Interaction) -> None:
    if not await _require_manager(interaction):
        return
    global _monitoring_paused, _pause_reason, _pause_until
    was_paused, _ = pause_state()
    _monitoring_paused = False
    _pause_reason = None
    _pause_until = None
    if was_paused:
        log.info("Monitoring resumed by %s.", interaction.user)
        await interaction.response.send_message("Pinging resumed.", ephemeral=True)
    else:
        await interaction.response.send_message("It wasn't paused — already active.", ephemeral=True)


# --- runtime frequency config (persisted to sab-config.json, .env wins on reset) ---


@sab_group.command(name="threshold", description="How long of silence before a ping")
@app_commands.describe(minutes="Minutes of quiet before the bot pings")
async def sab_threshold(interaction: discord.Interaction, minutes: float) -> None:
    if not await _require_manager(interaction):
        return
    if not 0 < minutes <= MAX_THRESHOLD_MINUTES:
        await interaction.response.send_message(
            f"Minutes must be over 0 and at most {MAX_THRESHOLD_MINUTES:.0f}.",
            ephemeral=True,
        )
        return
    global IDLE_MINUTES
    IDLE_MINUTES = float(minutes)
    _set_override("idle_minutes", IDLE_MINUTES)
    log.info("Idle threshold set to %sm by %s (saved to %s).", minutes, interaction.user, CONFIG_FILE)
    await interaction.response.send_message(
        f"Idle threshold now {minutes:g}m — effective on the next check and saved.",
        ephemeral=True,
    )


@sab_group.command(name="interval", description="How often to check the channel")
@app_commands.describe(seconds="Seconds between checks (30 or more)")
async def sab_interval(interaction: discord.Interaction, seconds: int) -> None:
    if not await _require_manager(interaction):
        return
    if not MIN_INTERVAL_SECONDS <= seconds <= MAX_INTERVAL_SECONDS:
        await interaction.response.send_message(
            f"Seconds must be {MIN_INTERVAL_SECONDS}-{MAX_INTERVAL_SECONDS}.",
            ephemeral=True,
        )
        return
    global CHECK_INTERVAL_SECONDS
    CHECK_INTERVAL_SECONDS = seconds
    _set_override("check_interval_seconds", seconds)
    if inactivity_check.is_running():
        inactivity_check.change_interval(seconds=seconds)
    log.info("Check interval set to %ss by %s (saved to %s).", seconds, interaction.user, CONFIG_FILE)
    await interaction.response.send_message(
        f"Check interval now {seconds}s — effective immediately and saved.",
        ephemeral=True,
    )


@sab_group.command(name="reset", description="Go back to the default settings")
async def sab_reset(interaction: discord.Interaction) -> None:
    if not await _require_manager(interaction):
        return
    global IDLE_MINUTES, CHECK_INTERVAL_SECONDS, CHANNEL_ID, ROLE_ID, _config_overrides
    had_any = bool(_config_overrides)
    _config_overrides = {}
    try:
        if os.path.exists(CONFIG_FILE):
            os.remove(CONFIG_FILE)
    except OSError as exc:
        await interaction.response.send_message(f"Couldn't remove {CONFIG_FILE}: {exc}", ephemeral=True)
        return
    IDLE_MINUTES = _BOOT_IDLE_MINUTES
    CHECK_INTERVAL_SECONDS = _BOOT_CHECK_INTERVAL
    CHANNEL_ID = _BOOT_CHANNEL_ID
    ROLE_ID = _BOOT_ROLE_ID
    if inactivity_check.is_running():
        inactivity_check.change_interval(seconds=CHECK_INTERVAL_SECONDS)
    log.info("Config overrides cleared by %s; restored .env values.", interaction.user)
    if _is_configured():
        watching = f"Watching <#{CHANNEL_ID}> for <@&{ROLE_ID}>."
    else:
        watching = "No channel/role in .env — dormant until sab setup."
    if had_any:
        await interaction.response.send_message(
            f"Overrides cleared — back to .env values ({_BOOT_IDLE_MINUTES:g}m / {_BOOT_CHECK_INTERVAL}s). {watching}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    else:
        await interaction.response.send_message(
            f"Nothing was overridden — already on .env values ({_BOOT_IDLE_MINUTES:g}m / {_BOOT_CHECK_INTERVAL}s). {watching}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


# --- guided onboarding: sab setup (one command instead of many) ---

SETUP_TIMEOUT_SECONDS = 300


def _parse_setup_numbers(threshold_s: str, interval_s: str) -> tuple[float, int]:
    """Parse the setup modal's number fields; blank keeps the current value."""
    threshold_s = (threshold_s or "").strip()
    interval_s = (interval_s or "").strip()
    if threshold_s:
        try:
            threshold = float(threshold_s)
        except ValueError:
            raise ValueError(f"Quiet minutes must be a number (got {threshold_s!r}).")
        if not 0 < threshold <= MAX_THRESHOLD_MINUTES:
            raise ValueError(f"Quiet minutes must be over 0 and at most {MAX_THRESHOLD_MINUTES:.0f}.")
    else:
        threshold = IDLE_MINUTES
    if interval_s:
        try:
            interval = int(interval_s)
        except ValueError:
            raise ValueError(f"Check seconds must be a whole number (got {interval_s!r}).")
        if not MIN_INTERVAL_SECONDS <= interval <= MAX_INTERVAL_SECONDS:
            raise ValueError(f"Check seconds must be {MIN_INTERVAL_SECONDS}-{MAX_INTERVAL_SECONDS}.")
    else:
        interval = CHECK_INTERVAL_SECONDS
    return threshold, interval


def _setup_summary(
    channel_id: int,
    role_id: int,
    threshold: float,
    interval: int,
    first_message: str | None,
    role_is_everyone: bool,
    already_configured: bool,
) -> str:
    lines = [
        "Here's what I'll do:",
        f"Watch <#{channel_id}> and ping <@&{role_id}> after {threshold:g}m of quiet.",
        f"Checking the channel every {interval}s.",
    ]
    if first_message:
        lines.append(f"First new ping message: {first_message[:200]}")
    if role_is_everyone:
        lines.append("Note: that's @everyone — I need the Mention Everyone permission for it to notify.")
    if already_configured:
        lines.append("This replaces the current channel/role.")
    lines.append("Start watching?")
    return "\n".join(lines)


def apply_setup(channel_id: int, role_id: int, threshold: float, interval: int) -> None:
    """Persist a full setup and activate it immediately."""
    global IDLE_MINUTES, CHECK_INTERVAL_SECONDS, CHANNEL_ID, ROLE_ID
    CHANNEL_ID = channel_id
    ROLE_ID = role_id
    IDLE_MINUTES = float(threshold)
    CHECK_INTERVAL_SECONDS = interval
    _config_overrides["channel_id"] = channel_id
    _config_overrides["role_id"] = role_id
    _config_overrides["idle_minutes"] = IDLE_MINUTES
    _config_overrides["check_interval_seconds"] = interval
    _save_config_overrides()
    if inactivity_check.is_running():
        inactivity_check.change_interval(seconds=interval)


async def _check_setup_owner(interaction: discord.Interaction, owner_id: int) -> bool:
    if interaction.user.id == owner_id:
        return True
    await interaction.response.send_message(
        "Only the person who ran setup can use these.", ephemeral=True
    )
    return False


class SetupChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, view_ref: SetupView):
        super().__init__(
            placeholder="Pick the channel to watch",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
            row=0,
        )
        self.view_ref = view_ref

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await _check_setup_owner(interaction, self.view_ref.owner_id):
            return
        self.view_ref.channel_id = self.values[0].id
        await self.view_ref.refresh(interaction)


class SetupRoleSelect(discord.ui.RoleSelect):
    def __init__(self, view_ref: SetupView):
        super().__init__(
            placeholder="Pick the role to ping",
            min_values=1,
            max_values=1,
            row=1,
        )
        self.view_ref = view_ref

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await _check_setup_owner(interaction, self.view_ref.owner_id):
            return
        role = self.values[0]
        self.view_ref.role_id = role.id
        self.view_ref.role_is_everyone = bool(role.is_default())
        await self.view_ref.refresh(interaction)


class SetupView(discord.ui.View):
    """Step 1: pick channel + role with native menus (no IDs to type)."""

    def __init__(self, owner_id: int):
        super().__init__(timeout=SETUP_TIMEOUT_SECONDS)
        self.owner_id = owner_id
        self.channel_id: int | None = None
        self.role_id: int | None = None
        self.role_is_everyone = False
        self.add_item(SetupChannelSelect(self))
        self.add_item(SetupRoleSelect(self))

    async def on_timeout(self) -> None:
        try:
            if self.message is not None:
                await self.message.edit(content="Setup timed out — run sab setup again.", view=None)
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def refresh(self, interaction: discord.Interaction) -> None:
        picked = []
        if self.channel_id is not None:
            picked.append(f"Channel: <#{self.channel_id}>")
        if self.role_id is not None:
            picked.append(f"Role: <@&{self.role_id}>")
        body = "\n".join(picked) if picked else "Nothing picked yet."
        await interaction.response.edit_message(
            content=f"Step 1 — pick what to watch (only you can see this):\n{body}",
            view=self,
        )

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.primary, row=2)
    async def continue_setup(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not await _check_setup_owner(interaction, self.owner_id):
            return
        if self.channel_id is None or self.role_id is None:
            await interaction.response.send_message(
                "Pick both a channel and a role first.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            SetupModal(self, self.channel_id, self.role_id, self.role_is_everyone)
        )


class SetupModal(discord.ui.Modal, title="Ping settings"):
    """Step 2: numbers + optional first message (prefilled with current values)."""

    def __init__(
        self, view: SetupView, channel_id: int, role_id: int, role_is_everyone: bool
    ):
        super().__init__(timeout=SETUP_TIMEOUT_SECONDS)
        self.setup_view = view
        self.channel_id = channel_id
        self.role_id = role_id
        self.role_is_everyone = role_is_everyone
        self.threshold_input = discord.ui.TextInput(
            label="Minutes of quiet before a ping",
            default=f"{IDLE_MINUTES:g}",
            max_length=10,
        )
        self.interval_input = discord.ui.TextInput(
            label="Seconds between checks (30+)",
            default=str(CHECK_INTERVAL_SECONDS),
            max_length=10,
        )
        self.message_input = discord.ui.TextInput(
            label="First ping message (optional)",
            style=discord.TextStyle.paragraph,
            required=False,
            placeholder="Leave empty to keep the current messages",
            max_length=MAX_MESSAGE_CHARS,
        )
        self.add_item(self.threshold_input)
        self.add_item(self.interval_input)
        self.add_item(self.message_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            threshold, interval = _parse_setup_numbers(
                self.threshold_input.value or "", self.interval_input.value or ""
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        first_message = (self.message_input.value or "").strip() or None
        summary = _setup_summary(
            self.channel_id,
            self.role_id,
            threshold,
            interval,
            first_message,
            self.role_is_everyone,
            _is_configured(),
        )
        confirm = ConfirmView(
            self.setup_view.owner_id,
            self.channel_id,
            self.role_id,
            threshold,
            interval,
            first_message,
        )
        try:
            await interaction.response.edit_message(content=summary, view=confirm)
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(summary, view=confirm, ephemeral=True)

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        log.warning("Setup modal error: %s", error)
        try:
            await interaction.response.send_message(
                "Something went wrong — run sab setup again.", ephemeral=True
            )
        except (discord.Forbidden, discord.HTTPException):
            pass


class ConfirmView(discord.ui.View):
    """Step 3: confirm (or cancel) — nothing is saved until confirmed."""

    def __init__(
        self,
        owner_id: int,
        channel_id: int,
        role_id: int,
        threshold: float,
        interval: int,
        first_message: str | None,
    ):
        super().__init__(timeout=SETUP_TIMEOUT_SECONDS)
        self.owner_id = owner_id
        self.channel_id = channel_id
        self.role_id = role_id
        self.threshold = threshold
        self.interval = interval
        self.first_message = first_message

    async def on_timeout(self) -> None:
        try:
            if self.message is not None:
                await self.message.edit(
                    content="Setup timed out — nothing was changed. Run sab setup again.",
                    view=None,
                )
        except (discord.Forbidden, discord.HTTPException):
            pass

    @discord.ui.button(label="Start watching", style=discord.ButtonStyle.success)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not await _check_setup_owner(interaction, self.owner_id):
            return
        apply_setup(self.channel_id, self.role_id, self.threshold, self.interval)
        extra = ""
        if self.first_message:
            try:
                count = add_file_message(self.first_message)
                extra = f" Added your message as #{count}."
            except OSError as exc:
                extra = f" Couldn't save your message ({exc}) — the rest is active."
        log.info(
            "Setup completed by %s: channel=%s role=%s threshold=%sm interval=%ss",
            interaction.user,
            self.channel_id,
            self.role_id,
            self.threshold,
            self.interval,
        )
        await interaction.response.edit_message(
            content=(
                f"Done — watching <#{self.channel_id}> for <@&{self.role_id}>: "
                f"ping after {self.threshold:g}m of quiet, checked every {self.interval}s.{extra}"
            ),
            view=None,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not await _check_setup_owner(interaction, self.owner_id):
            return
        await interaction.response.edit_message(
            content="Setup cancelled — nothing was changed.", view=None
        )


@sab_group.command(name="setup", description="Walk through setup step by step")
async def sab_setup(interaction: discord.Interaction) -> None:
    if not await _require_manager(interaction):
        return
    view = SetupView(owner_id=interaction.user.id)
    await interaction.response.send_message(
        "Step 1 — pick what to watch (only you can see this):\nNothing picked yet.",
        view=view,
        ephemeral=True,
    )


@sab_group.command(name="emotes", description="See emotes you can use in messages")
async def sab_emotes(interaction: discord.Interaction) -> None:
    if not await _require_manager(interaction):
        return
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "Use this inside a server, not in DMs.", ephemeral=True
        )
        return
    try:
        emotes = await guild.fetch_emojis()
    except (discord.Forbidden, discord.HTTPException):
        emotes = guild.emojis
    if not emotes:
        await interaction.response.send_message(
            "This server has no custom emotes.", ephemeral=True
        )
        return
    lines = [f"{e}  `{e}`" for e in emotes[:25]]
    if len(emotes) > 25:
        lines.append(f"…and {len(emotes) - 25} more")
    body = "\n".join(lines)
    if len(body) > 1800:
        body = body[:1800] + "…"
    await interaction.response.send_message(
        "Paste a code into a ping message (messages.txt or messages add) and it renders as the emote:\n" + body,
        ephemeral=True,
    )


async def _sync_commands() -> None:
    global _commands_synced
    if _commands_synced:
        return
    names = [c.name for c in tree.get_commands()]
    log.info(
        "Registering %d top-level command(s): %s (discord.py %s)",
        len(names),
        names,
        discord.__version__,
    )
    try:
        if GUILD_ID is not None:
            # Guild-scoped sync lands instantly (global sync can take up to an hour).
            # copy_global_to is required: sync(guild=...) only uploads commands
            # bound to that guild, and ours are registered globally. Without it
            # the payload is empty and Discord stores (and reports) 0 commands.
            tree.copy_global_to(guild=discord.Object(id=GUILD_ID))
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

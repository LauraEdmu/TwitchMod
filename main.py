import asyncio
import json
import logging
import os
import re
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
import aiofiles

from dotenv import load_dotenv

from twitchAPI.chat import Chat, ChatMessage, EventData
from twitchAPI.oauth import UserAuthenticationStorageHelper
from twitchAPI.twitch import Twitch
from twitchAPI.type import AuthScope, ChatEvent
from twitchAPI.eventsub.websocket import EventSubWebsocket
from twitchAPI.object.eventsub import ChannelRaidEvent

from parse_helpers.homoglyphs import advanced_normalise
from parse_helpers.thisis import is_link, contains_non_twitch_link


load_dotenv()


# -----------------------------
# Logging
# -----------------------------

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
logger.propagate = False

log_path = Path("main_logs") / "bot.log"
log_path.parent.mkdir(parents=True, exist_ok=True)

formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

if not logger.handlers:
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)


# -----------------------------
# Config
# -----------------------------

TIMEZONE_REGEX = re.compile(r"\btime.?zones?\b", re.IGNORECASE)

APP_ID = os.environ["TWITCH_CLIENT_ID"]
APP_SECRET = os.environ["TWITCH_CLIENT_SECRET"]

TARGET_CHANNEL = os.environ["TWITCH_CHANNEL"].lower()
BOT_LOGIN = os.getenv("TWITCH_BOT_LOGIN", "").lower()

DRY_RUN = os.getenv("DRY_RUN", "1") == "1"

DATA_PATH = Path("user_data") / f"{TARGET_CHANNEL}.json"

# twitchAPI chat helper uses IRC chat scopes.
# The timeout API needs MODERATOR_MANAGE_BANNED_USERS.
SCOPES = [
    AuthScope.CHAT_READ,
    AuthScope.CHAT_EDIT,
    AuthScope.MODERATOR_MANAGE_BANNED_USERS,
    AuthScope.MODERATOR_MANAGE_SHOUTOUTS,
]

AUDITS_ACTIONS_PATH = Path("audits") / f"{TARGET_CHANNEL}_actions.json"
AUDITS_MESSAGES_PATH = Path("audits") / f"{TARGET_CHANNEL}_messages.json"
AUDITS_ACTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)

audit_log_lock = asyncio.Lock()

# -----------------------------
# Data models
# -----------------------------

@dataclass
class Rule:
    name: str
    pattern: re.Pattern[str]
    duration: int
    reason: str


# -----------------------------
# Global runtime state
# -----------------------------

twitch: Twitch | None = None
broadcaster_id: str | None = None
moderator_id: str | None = None

user_data: dict[str, Any] = {}
regulars: dict[str, dict[str, Any]] = {}


# -----------------------------
# Rules
# -----------------------------

def load_rules(path: str = "rules.json") -> list[Rule]:
    with open(path, "r", encoding="utf-8") as f:
        raw_rules = json.load(f)

    rules: list[Rule] = []

    for raw in raw_rules:
        rules.append(
            Rule(
                name=raw["name"],
                pattern=re.compile(raw["pattern"], re.IGNORECASE),
                duration=int(raw.get("duration", 300)),
                reason=raw.get("reason", raw["name"]),
            )
        )

    return rules


RULES = load_rules()


def find_matching_rule(text: str) -> Rule | None:
    for rule in RULES:
        try:
            if rule.pattern.search(text):
                return rule
        except TimeoutError:
            logger.warning("[REGEX TIMEOUT] Rule took too long: %s", rule.name)

    return None


# -----------------------------
# User data / regulars
# -----------------------------

def load_user_data() -> None:
    global user_data
    global regulars

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)

    if DATA_PATH.exists():
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            user_data = json.load(f)

        logger.debug("Loaded user data for %s: %s", TARGET_CHANNEL, user_data)
    else:
        user_data = {}
        logger.debug("No existing user data for %s; starting fresh.", TARGET_CHANNEL)

    loaded_regulars = user_data.setdefault("regulars", {})

    if not isinstance(loaded_regulars, dict):
        logger.warning("regulars was not a dict; resetting it.")
        loaded_regulars = {}
        user_data["regulars"] = loaded_regulars

    regulars = loaded_regulars
    save_user_data()


def save_user_data() -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)

    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(user_data, f, indent=2, ensure_ascii=False)


def clean_login(raw_login: str) -> str:
    return raw_login.strip().lstrip("@").lower()


async def get_user_id(twitch_api: Twitch, login: str) -> str:
    users = [user async for user in twitch_api.get_users(logins=[login])]

    if not users:
        raise RuntimeError(f"Could not find Twitch user: {login}")

    return users[0].id


async def add_regular_by_login(login: str, added_by_msg: ChatMessage) -> tuple[bool, str]:
    assert twitch is not None

    login = clean_login(login)

    if not login:
        return False, "Usage: !regular username"

    users = [user async for user in twitch.get_users(logins=[login])]

    if not users:
        return False, f"Could not find Twitch user: {login}"

    user = users[0]

    already_regular = user.id in regulars

    regulars[user.id] = {
        "login": user.login,
        "display_name": user.display_name,
        "added_by_id": added_by_msg.user.id,
        "added_by_name": added_by_msg.user.name,
        "added_at": datetime.now(timezone.utc).isoformat(),
    }

    user_data["regulars"] = regulars
    save_user_data()

    logger.info(
        "[REGULAR ADDED] %s (%s) by %s",
        user.display_name,
        user.id,
        added_by_msg.user.name,
    )

    if already_regular:
        return True, f"{user.display_name} was already a regular; updated their record."

    return True, f"{user.display_name} is now a regular."


# -----------------------------
# Permissions / protection
# -----------------------------

def is_command_allowed(msg: ChatMessage) -> bool:
    name = msg.user.name.lower()

    # Broadcaster should always be allowed.
    if name == TARGET_CHANNEL:
        return True

    # Mods can manage regulars.
    if msg.user.mod:
        return True

    return False


def is_protected_user(msg: ChatMessage) -> bool:
    name = msg.user.name.lower()

    if name == TARGET_CHANNEL:
        return True

    if BOT_LOGIN and name == BOT_LOGIN:
        return True

    if msg.user.mod:
        return True

    return False


# -----------------------------
# Moderation helpers
# -----------------------------

async def handle_link_moderation(msg: ChatMessage, normalized_text: str) -> bool:
    user_id = msg.user.id

    if user_id in regulars:
        if contains_non_twitch_link(normalized_text):
            logger.info("[REGULAR LINK] %s: %r", msg.user.name, msg.text)
            await timeout_user(
                msg,
                Rule(
                    name="Regular user posted non-Twitch link",
                    pattern=re.compile(r".*"),
                    duration=300,
                    reason="Regular user posted non-Twitch link",
                ),
            )
            return True

        return False

    if is_link(normalized_text):
        logger.info("[LINK] %s: %r", msg.user.name, msg.text)
        await timeout_user(
            msg,
            Rule(
                name="User posted link",
                pattern=re.compile(r".*"),
                duration=300,
                reason="User posted link",
            ),
        )
        return True

    return False

async def handle_auto_moderation(msg: ChatMessage, normalized_text: str) -> bool:
    if is_protected_user(msg):
        return False

    rule = find_matching_rule(normalized_text)
    if rule:
        await timeout_user(msg, rule)
        await message_to_audit_log(msg, action=f"auto_mod_timeout_{rule.name}")
        return True

    if await handle_link_moderation(msg, normalized_text):
        await message_to_audit_log(msg, action="auto_mod_timeout_link")
        return True

    return False

async def message_to_audit_log(msg: ChatMessage, action: str = "") -> None:
    async with audit_log_lock:
        log_entry = {
            "msg_id": msg.id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": [action] if action else [],
            "user_id": msg.user.id,
            "user_name": msg.user.name,
            "user_display_name": msg.user.display_name,
            "text": msg.text,
        }

        entries = []
        found = False

        if AUDITS_MESSAGES_PATH.exists():
            async with aiofiles.open(AUDITS_MESSAGES_PATH, mode="r", encoding="utf-8") as f:
                async for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    entry = json.loads(line)

                    if entry.get("msg_id") == msg.id:
                        found = True

                        if "action" not in entry or not isinstance(entry["action"], list):
                            entry["action"] = []

                        if action:
                            entry["action"].append(action)

                    entries.append(entry)

        if not found:
            entries.append(log_entry)

        async with aiofiles.open(AUDITS_MESSAGES_PATH, mode="w", encoding="utf-8") as f:
            for entry in entries:
                await f.write(json.dumps(entry, ensure_ascii=False) + "\n")

# -----------------------------
# Twitch actions
# -----------------------------

async def timeout_user(msg: ChatMessage, rule: Rule) -> None:
    assert twitch is not None
    assert broadcaster_id is not None
    assert moderator_id is not None

    logger.info(
        "[MATCH] %s: %r -> rule=%r, duration=%ss",
        msg.user.name,
        msg.text,
        rule.name,
        rule.duration,
    )

    if DRY_RUN:
        logger.info("[DRY RUN] Not timing out.")
        return

    await twitch.ban_user(
        broadcaster_id=broadcaster_id,
        moderator_id=moderator_id,
        user_id=msg.user.id,
        reason=f"AutoMod regex: {rule.reason}",
        duration=rule.duration,
    )

    logger.info("[TIMEOUT] %s for %ss", msg.user.name, rule.duration)


# -----------------------------
# Commands
# -----------------------------

async def handle_regular_command(msg: ChatMessage) -> bool:
    text = msg.text.strip()

    command_aliases = ("!regular", "!addregular")

    command_used = None
    for alias in command_aliases:
        if text == alias or text.startswith(alias + " "):
            command_used = alias
            break

    if command_used is None:
        return False

    if not is_command_allowed(msg):
        logger.info("[DENIED COMMAND] %s: %r", msg.user.name, msg.text)
        await msg.reply("Only mods can use that command.")
        await message_to_audit_log(msg, action="denied_command")
        return True

    parts = text.split(maxsplit=1)

    if len(parts) < 2:
        await msg.reply("Usage: !regular username")
        return True

    target_login = parts[1]
    ok, response = await add_regular_by_login(target_login, msg)

    if ok:
        logger.info("[COMMAND] %s used %s on %s", msg.user.name, command_used, target_login)
        await message_to_audit_log(msg, action="command_success")
    else:
        logger.info("[COMMAND FAILED] %s used %s on %s", msg.user.name, command_used, target_login)
        await message_to_audit_log(msg, action="command_failed")

    await msg.reply(response)
    return True

async def handle_regular_remove_command(msg: ChatMessage) -> bool:
    text = msg.text.strip()

    command_aliases = ("!removeregular", "!delregular", "!removereg", "!delreg", "!dereg")

    command_used = None
    for alias in command_aliases:
        if text == alias or text.startswith(alias + " "):
            command_used = alias
            break

    if command_used is None:
        return False

    if not is_command_allowed(msg):
        logger.info("[DENIED COMMAND] %s: %r", msg.user.name, msg.text)
        await msg.reply("Only mods can use that command.")
        await message_to_audit_log(msg, action="denied_command")
        return True

    parts = text.split(maxsplit=1)

    if len(parts) < 2:
        await msg.reply("Usage: !removeregular username")
        return True

    target_login = clean_login(parts[1])

    user_id_to_remove = None
    for user_id, info in regulars.items():
        if info.get("login") == target_login:
            user_id_to_remove = user_id
            break

    if user_id_to_remove is None:
        await msg.reply(f"{target_login} is not a regular.")
        return True

    removed_info = regulars.pop(user_id_to_remove)
    user_data["regulars"] = regulars
    save_user_data()
    await message_to_audit_log(msg, action="command_success")

    logger.info(
        "[REGULAR REMOVED] %s (%s) by %s",
        removed_info.get("display_name"),
        user_id_to_remove,
        msg.user.name,
    )

    await msg.reply(f"{removed_info.get('display_name')} has been removed from the regulars.")
    return True

async def regular_check(msg: ChatMessage) -> bool: # command for users to check if they are a regular
    text = msg.text.strip()

    command_aliases = ("!isregular", "!checkregular", "!amiregular", "!amireg")

    command_used = None
    for alias in command_aliases:
        if text == alias or text.startswith(alias + " "):
            command_used = alias
            break

    if command_used is None:
        return False

    user_id = msg.user.id
    if user_id in regulars:
        await msg.reply(f"{msg.user.display_name}, you are a regular!")
        await message_to_audit_log(msg, action="regular_check_true")
    else:
        await msg.reply(f"{msg.user.display_name}, you are not a regular.")
        await message_to_audit_log(msg, action="regular_check_false")
    return True

async def lurk_announcement(msg: ChatMessage) -> bool:
    text = msg.text.strip()

    command_aliases = ("!lurk", "!brb", "!afk", "!lurking")

    command_used = None
    for alias in command_aliases:
        if text == alias or text.startswith(alias + " "):
            command_used = alias
            break

    if command_used is None:
        return False

    await msg.reply(f"{msg.user.display_name} is now lurking. See you later!")
    await message_to_audit_log(msg, action="lurk_announcement")
    return True

async def coinflip_command(msg: ChatMessage) -> bool:
    text = msg.text.strip()

    command_aliases = ("!coinflip", "!flipcoin", "!flip", "!coin")

    command_used = None
    for alias in command_aliases:
        if text == alias or text.startswith(alias + " "):
            command_used = alias
            break

    if command_used is None:
        return False

    result = random.randint(0, 1)
    await msg.reply(f"{msg.user.display_name} flipped a coin and got: {'Heads' if result == 0 else 'Tails'}")
    await message_to_audit_log(msg, action=f"coinflip_command_{'heads' if result == 0 else 'tails'}")
    return True

# -----------------------------
# Chat event handlers
# -----------------------------

async def on_ready(event: EventData) -> None:
    logger.info("Bot is ready; joining channel.")
    await event.chat.join_room(TARGET_CHANNEL)

async def on_message(msg: ChatMessage) -> None:
    await message_to_audit_log(msg) # initially log the message before any bot actions
    
    if await handle_regular_command(msg):
        return
    if await handle_regular_remove_command(msg):
        return
    if await regular_check(msg):
        return
    if await lurk_announcement(msg):
        return
    if await coinflip_command(msg):
        return
    normalized_text = advanced_normalise(msg.text)

    if await handle_auto_moderation(msg, normalized_text):
        return
    
    if TIMEZONE_REGEX.search(normalized_text):
        # mention what time it is for me
        now = datetime.now(ZoneInfo("Europe/London"))
        await msg.reply(f"For me the time is: {now.strftime('%Y-%m-%d %H:%M:%S')}")
        await message_to_audit_log(msg, action="timezone_command")

    
async def on_raid(data: ChannelRaidEvent) -> None:
    assert twitch is not None
    assert broadcaster_id is not None
    assert moderator_id is not None

    raid = data.event

    logger.info(
        "[RAID] %s (%s) raided with %s viewers",
        raid.from_broadcaster_user_name,
        raid.from_broadcaster_user_login,
        raid.viewers,
    )

    if DRY_RUN:
        logger.info(
            "[DRY RUN] Would shout out %s",
            raid.from_broadcaster_user_login,
        )
        return

    try:
        await twitch.send_a_shoutout(
            from_broadcaster_id=broadcaster_id,
            to_broadcaster_id=raid.from_broadcaster_user_id,
            moderator_id=moderator_id,
        )

        logger.info(
            "[SHOUTOUT] Sent shoutout to %s",
            raid.from_broadcaster_user_login,
        )

    except Exception:
        logger.exception(
            "[SHOUTOUT FAILED] Could not shout out %s",
            raid.from_broadcaster_user_login,
        )

# -----------------------------
# Main
# -----------------------------

async def main() -> None:
    global twitch
    global broadcaster_id
    global moderator_id

    load_user_data()

    twitch = await Twitch(APP_ID, APP_SECRET)

    helper = UserAuthenticationStorageHelper(twitch, SCOPES)
    await helper.bind()

    broadcaster_id = await get_user_id(twitch, TARGET_CHANNEL)

    moderator_login = BOT_LOGIN or TARGET_CHANNEL
    moderator_id = await get_user_id(twitch, moderator_login)

    logger.info("Broadcaster: %s (%s)", TARGET_CHANNEL, broadcaster_id)
    logger.info("Moderator login: %s (%s)", moderator_login, moderator_id)
    logger.info("Dry run: %s", DRY_RUN)

    chat = await Chat(twitch)
    chat.register_event(ChatEvent.READY, on_ready)
    chat.register_event(ChatEvent.MESSAGE, on_message)

    chat.start()


    eventsub = EventSubWebsocket(twitch)
    eventsub.start()

    await eventsub.listen_channel_raid(
        on_raid,
        to_broadcaster_user_id=broadcaster_id,
    )

    try:
        logger.info("Running. Press Enter to stop.")
        await asyncio.to_thread(input)
    finally:
        chat.stop()
        await eventsub.stop()
        await twitch.close()


if __name__ == "__main__":
    asyncio.run(main())
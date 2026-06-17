import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from twitchAPI.chat import Chat, ChatMessage, EventData
from twitchAPI.oauth import UserAuthenticationStorageHelper
from twitchAPI.twitch import Twitch
from twitchAPI.type import AuthScope, ChatEvent

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
]


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
        return True

    if await handle_link_moderation(msg, normalized_text):
        return True

    return False

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
        return True

    parts = text.split(maxsplit=1)

    if len(parts) < 2:
        await msg.reply("Usage: !regular username")
        return True

    target_login = parts[1]
    ok, response = await add_regular_by_login(target_login, msg)

    if ok:
        logger.info("[COMMAND] %s used %s on %s", msg.user.name, command_used, target_login)
    else:
        logger.info("[COMMAND FAILED] %s used %s on %s", msg.user.name, command_used, target_login)

    await msg.reply(response)
    return True


# -----------------------------
# Chat event handlers
# -----------------------------

async def on_ready(event: EventData) -> None:
    logger.info("Bot is ready; joining channel.")
    await event.chat.join_room(TARGET_CHANNEL)

async def on_message(msg: ChatMessage) -> None:
    if await handle_regular_command(msg):
        return

    normalized_text = advanced_normalise(msg.text)

    if await handle_auto_moderation(msg, normalized_text):
        return

    


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

    try:
        logger.info("Running. Press Enter to stop.")
        await asyncio.to_thread(input)
    finally:
        chat.stop()
        await twitch.close()


if __name__ == "__main__":
    asyncio.run(main())
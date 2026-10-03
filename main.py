import asyncio
import json
import logging
import os
import random
import re
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generic, TypeVar
from zoneinfo import ZoneInfo

import aiofiles
import httpx
from dotenv import load_dotenv
from twitchAPI.chat import Chat, ChatMessage, EventData
from twitchAPI.eventsub.websocket import EventSubWebsocket
from twitchAPI.oauth import UserAuthenticationStorageHelper
from twitchAPI.object.eventsub import (
    ChannelPointsCustomRewardRedemptionAddEvent,
    ChannelRaidEvent,
    ChannelSharedChatBeginEvent,
    ChannelSharedChatEndEvent,
    ChannelSharedChatUpdateEvent,
)
from twitchAPI.twitch import Twitch
from twitchAPI.type import AuthScope, ChatEvent

import diction
from parse_helpers.homoglyphs import advanced_normalise
from parse_helpers.thisis import contains_non_twitch_link, is_link

load_dotenv()
T = TypeVar("T")


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
DISCORD_ASK_REGEX = re.compile(r"\bdiscord\?", re.IGNORECASE)

APP_ID = os.environ["TWITCH_CLIENT_ID"]
APP_SECRET = os.environ["TWITCH_CLIENT_SECRET"]

TARGET_CHANNEL = os.environ["TWITCH_CHANNEL"].lower()
BOT_LOGIN = os.getenv("TWITCH_BOT_LOGIN", "").lower()

DRY_RUN = os.getenv("DRY_RUN", "1") == "1"

DATA_PATH = Path("user_data") / f"{TARGET_CHANNEL}.json"
SHOUTOUTS_PATH = Path("user_data") / "shoutouts.json"
COUNTERS_PATH = Path("counters") / f"{TARGET_CHANNEL}_counters.json"

DISCORD_INVITE_LINK = os.getenv("DISCORD_INVITE_LINK", "")

MAX_TIMEOUT_STACK_SIZE = int(os.getenv("MAX_TIMEOUT_STACK_SIZE", "5"))

UK_TZ = ZoneInfo("Europe/London")

THESAURUS_API_KEY = os.environ["THESAURUS_API_KEY"]

REDEEM_TIMERS_SECONDS = {
    "short": 60,
    "medium": 330,
    "long": 630,
    "glasses_off": 330,
    "sensitivity": 330,
    "in_game_action": 330,
    "ban_word": 330,
    "ad_break": 180,
}

dictionary = diction.Dictionary()
dictionary.read_cache()

# twitchAPI chat helper uses IRC chat scopes.
# The timeout API needs MODERATOR_MANAGE_BANNED_USERS.
SCOPES = [
    AuthScope.CHAT_READ,
    AuthScope.CHAT_EDIT,
    AuthScope.MODERATOR_MANAGE_BANNED_USERS,
    AuthScope.MODERATOR_MANAGE_SHOUTOUTS,
    AuthScope.CHANNEL_READ_REDEMPTIONS,
    AuthScope.CHANNEL_MANAGE_VIPS,
]

AUDITS_ACTIONS_PATH = Path("audits") / f"{TARGET_CHANNEL}_actions.json"
AUDITS_MESSAGES_PATH = Path("audits") / f"{TARGET_CHANNEL}_messages.json"
AUDITS_REDEEMS_PATH = Path("audits") / f"{TARGET_CHANNEL}_redeems.json"
AUDITS_ACTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)

audit_log_lock = asyncio.Lock()
audit_redeem_lock = asyncio.Lock()

COUNTDOWN_URL = os.environ.get(
    "COUNTDOWN_URL",
    "http://YOUR_STREAMING_PC_IP:8765/redeem",
)

COUNTDOWN_SECRET = os.environ["COUNTDOWN_SECRET"]

reward_data_path = Path("reward_data") / f"{TARGET_CHANNEL}_rewards.json"
reward_data_path.parent.mkdir(parents=True, exist_ok=True)

if not reward_data_path.exists():
    logger.warning(
        "Reward data file does not exist: %s. Please run the reward data collection script first.",
        reward_data_path,
    )
    reward_data = {}
else:
    with open(reward_data_path, "r", encoding="utf-8") as f:
        reward_data = json.load(f)

if not SHOUTOUTS_PATH.exists():
    logger.warning(
        "Shoutouts file does not exist: %s.",
        SHOUTOUTS_PATH,
    )
    shoutouts = {}
else:
    with open(SHOUTOUTS_PATH, "r", encoding="utf-8") as f:
        shoutouts = json.load(f)

if not COUNTERS_PATH.exists():
    logger.warning(
        "Counters file does not exist: %s.",
        COUNTERS_PATH,
    )
    counters = {}
else:
    with open(COUNTERS_PATH, "r", encoding="utf-8") as f:
        counters = json.load(f)

CHANNEL_POINT_REWARD_SECONDS = {
    reward_data.get("add_minute", "1_min_id"): 60,
    reward_data.get("add_5_minutes", "5_min_id"): 300,
    reward_data.get("add_10_minutes", "10_min_id"): 600,
}

# -----------------------------
# Data models
# -----------------------------


@dataclass
class Rule:
    name: str
    pattern: re.Pattern[str]
    duration: int
    reason: str


@dataclass(frozen=True, slots=True)
class ReadoutMessage:
    text: str
    user_name: str
    user_display_name: str


# -----------------------------
# Global runtime state
# -----------------------------

twitch: Twitch | None = None
broadcaster_id: str | None = None
moderator_id: str | None = None
shared_chat_session_id: str | None = None
last_readout_message: ReadoutMessage | None = None

user_data: dict[str, Any] = {}
regulars: dict[str, dict[str, Any]] = {}

# -----------------------------
# Helpers/ Wrappers
# -----------------------------


async def get_shoutout_channel_info(
    login: str,
) -> tuple[str, str, str | None] | None:
    """
    Return the canonical login, display name, and current/last category.
    """
    assert twitch is not None

    users = [user async for user in twitch.get_users(logins=[login])]

    if not users:
        return None

    user = users[0]

    channels = await twitch.get_channel_information(user.id)

    category: str | None = None

    if channels:
        category = channels[0].game_name.strip() or None

    return user.login, user.display_name, category


def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


async def handle_vip_redeem(user_id: str, user_name: str = "") -> bool:
    assert twitch is not None
    assert broadcaster_id is not None

    display_name = user_name or user_id

    if DRY_RUN:
        logger.info(
            "[DRY RUN] Would give VIP to %s (%s)",
            display_name,
            user_id,
        )
        return False

    try:
        added = await twitch.add_channel_vip(
            broadcaster_id=broadcaster_id,
            user_id=user_id,
        )
    except ValueError:
        # pyTwitchAPI raises ValueError for things such as:
        # - no available VIP slots
        # - Build a Community not completed
        logger.exception(
            "[VIP FAILED] Could not give VIP to %s (%s)",
            display_name,
            user_id,
        )
        return False
    except Exception:
        logger.exception(
            "[VIP FAILED] Unexpected error while giving VIP to %s (%s)",
            display_name,
            user_id,
        )
        return False

    if not added:
        # pyTwitchAPI returns False when the user is already a VIP
        # or is currently a moderator.
        logger.warning(
            "[VIP NOT ADDED] %s (%s) is already a VIP or is a moderator",
            display_name,
            user_id,
        )
        return False

    logger.info(
        "[VIP ADDED] %s (%s)",
        display_name,
        user_id,
    )
    return True


async def handle_check_in_redeem(user_id: str, user_name: str = "") -> int:
    """
    Increment and save a user's check-in count.

    Stored shape:
    {
        "checkin": {
            "123456789": 1,
            "987654321": 12
        }
    }

    Note: JSON object keys are always strings after loading,
    so the user id is stored as a string key, while the day count is an int.
    """
    checkin = user_data.setdefault("checkin", {})

    if not isinstance(checkin, dict):
        logger.warning("checkin was not a dict; resetting it.")
        checkin = {}
        user_data["checkin"] = checkin

    user_id_key = str(user_id)

    current_days_raw = checkin.get(user_id_key, 0)

    try:
        current_days = int(current_days_raw)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid check-in count for %s: %r; resetting to 0.",
            user_id_key,
            current_days_raw,
        )
        current_days = 0

    new_days = current_days + 1
    checkin[user_id_key] = new_days

    save_user_data()

    logger.info(
        "[CHECK-IN] %s (%s) checked in for day %d",
        user_name or "<unknown>",
        user_id_key,
        new_days,
    )

    return new_days


async def send_tts_message(user_input: str, skip_checks=False) -> tuple[bool, str]:
    """
    Send a TTS message to env: $TTS_ADDRESS with $TTS_SECRET in the header.
    """
    tts_address = os.environ.get("TTS_ADDRESS")
    tts_secret = os.environ.get("TTS_SECRET")

    char_count = len(user_input)
    word_count = len(user_input.split())

    if not tts_address or not tts_secret:
        logger.warning("TTS_ADDRESS or TTS_SECRET not set; skipping TTS message.")
        return False, "TTS_ADDRESS or TTS_SECRET not set"

    normalised_input = advanced_normalise(user_input)
    rule_match = find_matching_rule(normalised_input)
    if not skip_checks:
        if rule_match:
            logger.info(
                "TTS message matches rule %r; skipping TTS message.",
                rule_match.name,
            )
            return False, f"TTS message matches rule {rule_match.name}; skipping TTS message."
        elif char_count > 200 or word_count > 40:
            logger.info(
                "TTS message is too long (%d characters, %d words); skipping TTS message.",
                char_count,
                word_count,
            )
            return (
                False,
                f"TTS message is too long ({char_count} characters, {word_count} words); skipping TTS message.",
            )

    payload = {
        "text": user_input,
    }

    headers = {
        "X-TTS-Secret": tts_secret,
    }

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.post(
                tts_address,
                json=payload,
                headers=headers,
            )

        response.raise_for_status()

        logger.info("Sent TTS message: %r", user_input)
        return True, "TTS message sent successfully."
    except httpx.HTTPStatusError as e:
        logger.error(f"TTS service rejected request: {e.response.status_code} {e.response.text}")
        return False, f"TTS service rejected request: {e.response.status_code} {e.response.text}"

    except httpx.RequestError as e:
        logger.error(f"Could not reach TTS service: {e}")
        return False, f"Could not reach TTS service: {e}"


async def redeem_timer(chat, target_channel: str, duration: str = "medium", finished_text: str = "") -> None:
    try:
        logger.info(f"Starting redeem timer for {duration} seconds in channel {target_channel}.")
        await asyncio.sleep(REDEEM_TIMERS_SECONDS[duration])

        await chat.send_message(
            target_channel, finished_text or f"Timer done! {REDEEM_TIMERS_SECONDS[duration]} seconds have elapsed."
        )
        logger.info(f"Redeem timer for {duration} seconds in channel {target_channel} finished.")

    except asyncio.CancelledError:
        # Only needed if you later add cancellation/reset logic
        logger.info("Redeem timer was cancelled.")
        raise

    except Exception:
        logger.exception("Redeem timer failed.")


async def ad_timer(chat, target_channel: str, duration: str = "ad_break", finished_text: str = "") -> None:
    try:
        logger.info(f"Starting ad timer for {duration} seconds in channel {target_channel}.")
        await asyncio.sleep(REDEEM_TIMERS_SECONDS[duration])

        await chat.send_message(
            target_channel, finished_text or f"Ad break done! {REDEEM_TIMERS_SECONDS[duration]} seconds have elapsed."
        )
        logger.info(f"Ad timer for {duration} seconds in channel {target_channel} finished.")

    except asyncio.CancelledError:
        # Only needed if you later add cancellation/reset logic
        logger.info("Ad timer was cancelled.")
        raise

    except Exception:
        logger.exception("Ad timer failed.")


class LimitedStack(Generic[T]):
    def __init__(self, max_size: int = MAX_TIMEOUT_STACK_SIZE) -> None:
        self._items: deque[T] = deque(maxlen=max_size)

    def push(self, item: T) -> None:
        self._items.append(item)

    def pop(self) -> T:
        return self._items.pop()

    def clear(self) -> None:
        self._items.clear()

    def to_list(self) -> list[T]:
        return list(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self):
        return iter(self._items)


#
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


def save_shoutouts() -> None:
    SHOUTOUTS_PATH.parent.mkdir(parents=True, exist_ok=True)

    with open(SHOUTOUTS_PATH, "w", encoding="utf-8") as f:
        json.dump(shoutouts, f, indent=2, ensure_ascii=False)


def save_counters() -> None:
    COUNTERS_PATH.parent.mkdir(parents=True, exist_ok=True)

    with open(COUNTERS_PATH, "w", encoding="utf-8") as f:
        json.dump(counters, f, indent=2, ensure_ascii=False)


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


async def add_regular_from_redeem(user_id: str, user_name: str, event_id: str) -> tuple[bool, str]:
    assert twitch is not None

    users = [user async for user in twitch.get_users(user_ids=[user_id])]

    if not users:
        return False, f"Could not find Twitch user with ID: {user_id}"

    user = users[0]

    already_regular = user.id in regulars

    regulars[user.id] = {
        "login": user.login,
        "display_name": user.display_name,
        "added_by_id": user.id,
        "added_by_name": f"{user.display_name} (via redeem {event_id})",
        "added_at": datetime.now(timezone.utc).isoformat(),
    }

    user_data["regulars"] = regulars
    save_user_data()

    logger.info(
        "[REGULAR ADDED FROM REDEEM] %s (%s) by %s",
        user.display_name,
        user.id,
        user.display_name,
    )

    if already_regular:
        return True, f"{user.display_name} was already a regular; updated their record."

    return True, f"{user.display_name} is now a regular."


# -----------------------------
# Permissions / protection
# -----------------------------


def is_command_allowed(msg: ChatMessage, allow_vip: bool = False) -> bool:
    name = msg.user.name.lower()

    # Broadcaster should always be allowed.
    if name == TARGET_CHANNEL:
        return True

    if msg.user.mod:
        return True

    if allow_vip and msg.user.vip:
        return True

    return False


def is_protected_user(msg: ChatMessage, protect_vip: bool = False) -> bool:
    name = msg.user.name.lower()

    if name == TARGET_CHANNEL:
        return True

    if BOT_LOGIN and name == BOT_LOGIN:
        return True

    if msg.user.mod:
        return True

    if protect_vip and msg.user.vip:
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


async def redeem_to_audit_log(
    redeem_event: ChannelPointsCustomRewardRedemptionAddEvent,
    action: str = "",
) -> None:
    async with audit_redeem_lock:
        log_entry = {
            "redeem_id": redeem_event.event.id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reward_id": redeem_event.event.reward.id,
            "reward_title": redeem_event.event.reward.title,
            "user_id": redeem_event.event.user_id,
            "user_name": redeem_event.event.user_name,
            "user_input": redeem_event.event.user_input,
            "action": [action] if action else [],
        }

        entries = []
        found = False

        if AUDITS_REDEEMS_PATH.exists():
            async with aiofiles.open(AUDITS_REDEEMS_PATH, mode="r", encoding="utf-8") as f:
                async for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    entry = json.loads(line)

                    if entry.get("redeem_id") == redeem_event.event.id:
                        found = True

                        if "action" not in entry or not isinstance(entry["action"], list):
                            entry["action"] = []

                        if action:
                            entry["action"].append(action)

                    entries.append(entry)

        if not found:
            entries.append(log_entry)

        async with aiofiles.open(AUDITS_REDEEMS_PATH, mode="w", encoding="utf-8") as f:
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


async def timeout_user_by_id(user_id: str, reason: str, duration: int) -> None:
    assert twitch is not None
    assert broadcaster_id is not None
    assert moderator_id is not None

    logger.info("[TIMEOUT] user_id=%s -> reason=%r, duration=%ss", user_id, reason, duration)

    if DRY_RUN:
        logger.info("[DRY RUN] Not timing out.")
        return

    await twitch.ban_user(
        broadcaster_id=broadcaster_id,
        moderator_id=moderator_id,
        user_id=user_id,
        reason=reason,
        duration=duration,
    )


async def ban_user(msg: ChatMessage, reason: str) -> None:
    assert twitch is not None
    assert broadcaster_id is not None
    assert moderator_id is not None

    logger.info("[BAN] %s: %r -> reason=%r", msg.user.name, msg.text, reason)

    if DRY_RUN:
        logger.info("[DRY RUN] Not banning.")
        return

    await twitch.ban_user(
        broadcaster_id=broadcaster_id,
        moderator_id=moderator_id,
        user_id=msg.user.id,
        reason=reason,
    )


async def ban_user_by_id(user_id: str, reason: str) -> None:
    assert twitch is not None
    assert broadcaster_id is not None
    assert moderator_id is not None

    logger.info("[BAN] user_id=%s -> reason=%r", user_id, reason)

    if DRY_RUN:
        logger.info("[DRY RUN] Not banning.")
        return

    await twitch.ban_user(
        broadcaster_id=broadcaster_id,
        moderator_id=moderator_id,
        user_id=user_id,
        reason=reason,
    )


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


async def regular_check(msg: ChatMessage) -> bool:  # command for users to check if they are a regular
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


async def handle_contextual_command(msg: ChatMessage) -> bool:
    text = msg.text.strip()

    if TIMEZONE_REGEX.search(text):
        now = datetime.now(UK_TZ)
        await msg.reply(f"For me the time is: {now.strftime('%Y-%m-%d %H:%M:%S')}")
        await message_to_audit_log(msg, action="timezone_command")
        return True

    if DISCORD_ASK_REGEX.search(text):
        await msg.reply(f"You can join our Discord server here: {DISCORD_INVITE_LINK}")
        await message_to_audit_log(msg, action="discord_command")
        return True

    return False


async def timeout_stack_ban(msg: ChatMessage) -> None:
    """
    Ban the last N users from the timeout stack. Only mods can do this.
    """
    text = msg.text.strip()

    command_aliases = ("!banstack", "!banstacked", "!banlast")

    command_used = None
    for alias in command_aliases:
        if text == alias or text.startswith(alias + " "):
            command_used = alias
            break

    if command_used is None:
        return

    if not is_command_allowed(msg):
        logger.info("[DENIED COMMAND] %s: %r", msg.user.name, msg.text)
        await msg.reply("Only mods can use that command.")
        await message_to_audit_log(msg, action="denied_command")
        return

    parts = text.split(maxsplit=1)

    if len(parts) < 2:
        await msg.reply(f"Usage: !banstack number_of_users_to_ban (1-{MAX_TIMEOUT_STACK_SIZE})")
        return

    try:
        num_to_ban = int(parts[1])
    except ValueError:
        await msg.reply("Please provide a valid number.")
        return

    if num_to_ban < 1 or num_to_ban > MAX_TIMEOUT_STACK_SIZE:
        await msg.reply(f"Please provide a number between 1 and {MAX_TIMEOUT_STACK_SIZE}.")
        return

    users_to_ban = []
    for _ in range(num_to_ban):
        if len(timeout_stack) == 0:
            break
        users_to_ban.append(timeout_stack.pop())

    if not users_to_ban:
        await msg.reply("Timeout stack is empty.")
        return

    for user_id in users_to_ban:
        if DRY_RUN:
            logger.info("[DRY RUN] Would ban user ID: %s", user_id)
            continue

        await ban_user_by_id(user_id, reason="Banned from timeout stack")
        logger.info("[BAN STACK] Banning user ID: %s", user_id)
        await message_to_audit_log(msg, action=f"ban_stack_{user_id}")

    await msg.reply(f"Banned the last {len(users_to_ban)} users from the timeout stack.")


async def clear_timeout_stack(msg: ChatMessage) -> None:
    """
    Clear the timeout stack. Only mods can do this.
    """
    text = msg.text.strip()

    command_aliases = ("!cleartimeoutstack", "!cleartstack", "!clearstack")

    command_used = None
    for alias in command_aliases:
        if text == alias or text.startswith(alias + " "):
            command_used = alias
            break

    if command_used is None:
        return

    if not is_command_allowed(msg):
        logger.info("[DENIED COMMAND] %s: %r", msg.user.name, msg.text)
        await msg.reply("Only mods can use that command.")
        await message_to_audit_log(msg, action="denied_command")
        return

    timeout_stack.clear()
    await msg.reply("Timeout stack has been cleared.")
    await message_to_audit_log(msg, action="clear_timeout_stack")


async def check_timeout_stack(msg: ChatMessage) -> None:
    """
    Check the contents of the timeout stack. Only mods can do this.
    """
    text = msg.text.strip()

    command_aliases = ("!checktimeoutstack", "!checktstack", "!checkstack")

    command_used = None
    for alias in command_aliases:
        if text == alias or text.startswith(alias + " "):
            command_used = alias
            break

    if command_used is None:
        return

    if not is_command_allowed(msg):
        logger.info("[DENIED COMMAND] %s: %r", msg.user.name, msg.text)
        await msg.reply("Only mods can use that command.")
        await message_to_audit_log(msg, action="denied_command")
        return

    if len(timeout_stack) == 0:
        await msg.reply("Timeout stack is empty.")
        return

    user_ids = timeout_stack.to_list()
    await msg.reply(f"Timeout stack contains the following user IDs: {', '.join(user_ids)}")
    await message_to_audit_log(msg, action="check_timeout_stack")


async def handle_shoutout_command(msg: ChatMessage) -> bool:
    text = msg.text.strip()

    command_aliases = ("!shoutout", "!so")

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
        await msg.reply("Usage: !shoutout username")
        return True

    target_login = clean_login(parts[1])
    lookup_login = target_login
    shoutout_info = shoutouts.get(target_login)

    # Resolve stored aliases to the real Twitch login.
    if shoutout_info and shoutout_info.get("isAlias"):
        alias_target = shoutout_info.get("aliasFor")

        if isinstance(alias_target, str):
            lookup_login = clean_login(alias_target)
            shoutout_info = shoutouts.get(lookup_login)
        else:
            shoutout_info = None

    try:
        channel_info = await get_shoutout_channel_info(lookup_login)
    except Exception:
        logger.exception(
            "[SHOUTOUT] Failed to retrieve channel information for %s",
            lookup_login,
        )
        channel_info = None

    if channel_info is not None:
        channel_login, api_display_name, category = channel_info
    else:
        channel_login = lookup_login
        api_display_name = lookup_login
        category = None

    category_text = f" Their latest category was {category}!" if category else ""

    if shoutout_info:
        shoutout_number = int(shoutout_info.get("shoutout_number", 0)) + 1

        shoutout_info["shoutout_number"] = shoutout_number

        display_name = shoutout_info.get(
            "display_name",
            api_display_name,
        )
        custom_message = shoutout_info.get("message", "").strip()

        response = (
            f"Shoutout to {display_name}! "
            f"{custom_message}{category_text} | "
            f"https://www.twitch.tv/{channel_login} | "
            f"This is their {ordinal(shoutout_number)} shoutout!"
        )
        # send tts version
        await send_tts_message(
            f"Shoutout to {display_name}! {custom_message}{category_text} This is their {ordinal(shoutout_number)} shoutout!",
            skip_checks=True,
        )
    else:
        response = (
            f"Shoutout to {api_display_name}! Check them out!{category_text} | https://www.twitch.tv/{channel_login}"
        )
        # send tts version
        await send_tts_message(f"Shoutout to {api_display_name}! Check them out!{category_text}", skip_checks=True)

    await msg.reply(response)
    await message_to_audit_log(msg, action="shoutout_command")

    save_shoutouts()

    return True


async def handle_counters(msg: ChatMessage) -> bool:
    text = msg.text.strip().casefold()

    commands = [
        {"name": "bug", "aliases": ["glitch", "issue"]},
        {
            "name": "mispronounce",
            "aliases": ["mispronunciation", "mispeak"],
        },
    ]

    decrement_words = {"decrement", "remove", "subtract"}

    for command in commands:
        command_aliases = [
            f"!{command['name']}",
            *(f"!{alias}" for alias in command["aliases"]),
        ]

        command_used = next(
            (alias for alias in command_aliases if text == alias or text.startswith(alias + " ")),
            None,
        )

        if command_used is None:
            continue

        counter_name = command["name"]
        current_count = counters.get(counter_name, 0)

        arguments = text[len(command_used) :].strip().split()
        should_decrement = any(argument in decrement_words for argument in arguments)

        if should_decrement:
            if current_count == 0:
                await msg.reply(f"{counter_name.capitalize()} count is already at 0; cannot decrement.")
                return True

            counters[counter_name] = current_count - 1
            action = f"counter_{counter_name}_decremented"
        else:
            counters[counter_name] = current_count + 1
            action = f"counter_{counter_name}_incremented"

        await message_to_audit_log(msg, action=action)
        save_counters()

        await msg.reply(f"{counter_name.capitalize()} count is now {counters[counter_name]}.")

        return True

    return False


async def handle_readout_command(msg: ChatMessage) -> bool:
    """Send the most recent eligible chat message to TTS."""
    text = msg.text.strip().casefold()

    command_aliases = ("!readout", "!readthat")

    command_used = next(
        (alias for alias in command_aliases if text == alias or text.startswith(alias + " ")),
        None,
    )

    if command_used is None:
        return False

    if not is_command_allowed(msg):
        logger.info("[DENIED COMMAND] %s: %r", msg.user.name, msg.text)
        await msg.reply("Only mods can use that command.")
        await message_to_audit_log(msg, action="denied_readout_command")
        return True

    if last_readout_message is None:
        await msg.reply("There is no previous chat message to read.")
        await message_to_audit_log(msg, action="readout_no_message")
        return True

    success, response = await send_tts_message(
        f"{last_readout_message.user_display_name} said: {last_readout_message.text}", skip_checks=True
    )

    if success:
        logger.info(
            "[READOUT] %s requested message from %s: %r",
            msg.user.name,
            last_readout_message.user_name,
            last_readout_message.text,
        )
        await message_to_audit_log(msg, action="readout_command_success")
    else:
        logger.warning(
            "[READOUT FAILED] %s requested message from %s: %s",
            msg.user.name,
            last_readout_message.user_name,
            response,
        )
        await msg.reply(f"Readout failed: {response}")
        await message_to_audit_log(msg, action="readout_command_failed")

    return True


async def handle_ad_announcement(msg: ChatMessage) -> bool:
    """Handle ad announcements in chat."""
    text = msg.text.strip().casefold()

    command_aliases = ("!adbreak", "!advertisement")

    command_used = next(
        (alias for alias in command_aliases if text == alias or text.startswith(alias + " ")),
        None,
    )

    if command_used is None:
        return False

    if not is_command_allowed(msg):
        logger.info("[DENIED COMMAND] %s: %r", msg.user.name, msg.text)
        await msg.reply("Only mods can use that command.")
        await message_to_audit_log(msg, action="denied_ad_announcement")
        return True

    await msg.reply(
        "A 3 minute ad-break has started. By doing a 3 minute ad-break twitch should suppress ads for everyone otherwise for an hour!"
    )
    await message_to_audit_log(msg, action="ad_announcement")

    asyncio.create_task(
        ad_timer(
            msg.chat,
            TARGET_CHANNEL,
            duration="ad_break",
            finished_text="The 3 minute ad-break has ended!",
        )
    )

    return True


async def handle_define_command(msg: ChatMessage) -> bool:
    text = msg.text.strip()

    command_aliases = ("!define",)

    command_used = next(
        (alias for alias in command_aliases if text == alias or text.startswith(alias + " ")),
        None,
    )

    if command_used is None:
        return False

    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply("Please provide a word to define.")
        return True

    word = parts[1]
    definition = await asyncio.to_thread(dictionary.define, word)

    if definition:
        await msg.reply(f"Definition of {word!r}: {definition}")
    else:
        await msg.reply(f"No definition found for {word!r}.")

    await message_to_audit_log(msg, action="define_command")
    return True


async def handle_thesaurus_command(msg: ChatMessage) -> bool:
    text = msg.text.strip()

    command_aliases = ("!thesaurus", "!thesaur")

    command_used = next(
        (alias for alias in command_aliases if text == alias or text.startswith(alias + " ")),
        None,
    )

    if command_used is None:
        return False

    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply("Please provide a word to look up in the thesaurus.")
        return True

    word = parts[1]
    result = await asyncio.to_thread(dictionary.thesaurus, word, THESAURUS_API_KEY)

    if result:
        synonyms, antonyms = result

        synonyms = synonyms[:10]
        antonyms = antonyms[:10]

        await msg.reply(
            f"Synonyms of {word!r}: {', '.join(synonyms) if synonyms else 'None'} | "
            f"Antonyms: {', '.join(antonyms) if antonyms else 'None'}"
        )
    else:
        await msg.reply(f"No thesaurus entry found for {word!r}.")
        # debug print
        print(f"No thesaurus entry found for {word!r}.")
        print(f"Result was: {result}")

    await message_to_audit_log(msg, action="thesaurus_command")
    return True


async def handle_spike_command(msg: ChatMessage) -> bool:  # gag command
    text = msg.text.strip()

    command_aliases = ("!spike",)

    command_used = next(
        (alias for alias in command_aliases if text == alias or text.startswith(alias + " ")),
        None,
    )

    if command_used is None:
        return False

    await msg.reply("Definition of 'spike': A sort of very large nail.")

    await message_to_audit_log(msg, action="spike_command")
    return True


async def handle_lemon_command(msg: ChatMessage) -> bool:  # gag command
    text = msg.text.strip()

    command_aliases = ("!lemon",)

    command_used = next(
        (alias for alias in command_aliases if text == alias or text.startswith(alias + " ")),
        None,
    )

    if command_used is None:
        return False

    await msg.reply("Definition of 'lemon': A sour yellow fruit.")

    await message_to_audit_log(msg, action="lemon_command")
    return True


def get_user_tier(msg: ChatMessage) -> str:
    user = msg.user

    if user.name.casefold() == TARGET_CHANNEL.casefold() or user.mod:
        return "mod"

    if user.vip:
        return "vip"

    if user.subscriber:
        return "sub"

    return "normal"


CHAT_LIST_URL = os.getenv(
    "CHAT_LIST_URL",
    "http://90.203.14.179:8787/message",
)


async def send_to_chat_list(
    user: str, message: str, msg: ChatMessage, with_tier: bool = False, test_mode: bool = False
) -> None:
    if with_tier:  # get the user's tier, as in normal, sub, vip, or mod
        tier = get_user_tier(msg)  # Replace this with actual logic to determine the user's tier

    colour = msg.user.color or ""

    if test_mode:
        if message.split()[0] in ("mod", "vip", "sub", "normal"):
            tier = message.split()[0]

    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.post(
                CHAT_LIST_URL,
                json={
                    "user": user,
                    "message": message,
                    "tier": tier if with_tier else "normal",
                    "colour": colour,
                },
            )

        response.raise_for_status()

    except httpx.HTTPStatusError as e:
        logger.warning(
            "Chat list rejected message: %s %s",
            e.response.status_code,
            e.response.text,
        )

    except httpx.RequestError as e:
        logger.warning(
            "Could not reach chat list: %s",
            e,
        )


async def get_recent_clip_categories(
    login: str,
    days: int = 90,
    max_clips: int = 100,
    max_categories: int = 3,
) -> list[str]:
    """
    Return the most common categories found in a user's recent Twitch clips.

    Example:
        ["Minecraft", "Just Chatting", "Phasmophobia"]
    """
    assert twitch is not None

    login = clean_login(login)

    users = [user async for user in twitch.get_users(logins=[login])]
    if not users:
        return []

    user = users[0]

    ended_at = datetime.now(timezone.utc)
    started_at = ended_at - timedelta(days=days)

    category_counts: Counter[str] = Counter()
    clips_seen = 0

    async for clip in twitch.get_clips(
        broadcaster_id=user.id,
        started_at=started_at,
        ended_at=ended_at,
        first=min(max_clips, 100),
    ):
        if clip.game_id:
            category_counts[clip.game_id] += 1

        clips_seen += 1

        if clips_seen >= max_clips:
            break

    if not category_counts:
        return []

    game_names: dict[str, str] = {}

    async for game in twitch.get_games(game_ids=list(category_counts.keys())):
        game_names[game.id] = game.name

    return [game_names[game_id] for game_id, _ in category_counts.most_common(max_categories) if game_id in game_names]


async def handle_category_command(msg: ChatMessage) -> bool:
    """
    Handle a command to display the most common categories in a user's recent clips.
    Returns True if the command was handled, False otherwise.
    """
    if not msg.text.startswith("!categories"):
        return False

    parts = msg.text.split()
    if len(parts) < 2:
        await msg.reply("Usage: !categories <username>")
        return True

    login = parts[1]
    categories = await get_recent_clip_categories(login)
    if not categories:
        await msg.reply(f"No recent categories found for user {login}.")
    else:
        await msg.reply(f"Most common categories for {login}: {', '.join(categories)}")

    return True


async def handle_category_intersection_command(msg: ChatMessage) -> bool:
    """
    Handle a command to display the intersection of the most common categories in two users' recent clips.
    Returns True if the command was handled, False otherwise.
    """
    if not msg.text.startswith("!category_intersection"):
        return False

    parts = msg.text.split()
    if len(parts) < 3:
        await msg.reply("Usage: !category_intersection <username1> <username2>")
        return True

    login1 = parts[1]
    login2 = parts[2]

    categories1 = await get_recent_clip_categories(login1)
    categories2 = await get_recent_clip_categories(login2)

    intersection = set(categories1) & set(categories2)
    if not intersection:
        await msg.reply(f"No common categories found for users {login1} and {login2}.")
    else:
        await msg.reply(f"Common categories for {login1} and {login2}: {', '.join(intersection)}")

    return True


# -----------------------------
# Chat event handlers
# -----------------------------


async def on_ready(event: EventData) -> None:
    logger.info("Bot is ready; joining channel.")
    await event.chat.join_room(TARGET_CHANNEL)


async def on_message(msg: ChatMessage) -> None:
    global last_readout_message

    await message_to_audit_log(msg)
    await send_to_chat_list(msg.user.name, msg.text, msg=msg, with_tier=True, test_mode=True)

    normalized_text = advanced_normalise(msg.text)
    if await handle_auto_moderation(msg, normalized_text):
        timeout_stack.push(msg.user.id)
        return

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
    if await check_timeout_stack(msg):
        return
    if await clear_timeout_stack(msg):
        return
    if await timeout_stack_ban(msg):
        return
    if await handle_shoutout_command(msg):
        return
    if await handle_counters(msg):
        return
    if await handle_ad_announcement(msg):
        return
    if await handle_readout_command(msg):
        return
    if await handle_define_command(msg):
        return
    if await handle_thesaurus_command(msg):
        return
    if await handle_spike_command(msg):
        return
    if await handle_lemon_command(msg):
        return
    if await handle_category_command(msg):
        return
    if await handle_category_intersection_command(msg):
        return

    await handle_contextual_command(msg)

    # Do not store messages sent by the bot itself.
    if BOT_LOGIN and msg.user.name.casefold() == BOT_LOGIN:
        return

    last_readout_message = ReadoutMessage(
        text=msg.text.strip(),
        user_name=msg.user.name,
        user_display_name=msg.user.display_name,
    )


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


async def on_channel_point_redeem(
    data: ChannelPointsCustomRewardRedemptionAddEvent,
    chat: Chat,
) -> None:
    await redeem_to_audit_log(data)

    event = data.event

    seconds_to_add = CHANNEL_POINT_REWARD_SECONDS.get(event.reward.id)
    logger.info(
        f"Channel point redeem: {event.reward.title!r} by {event.user_name} ({event.user_id}) with reward ID {event.reward.id!r}, seconds to add: {seconds_to_add}"
    )

    if seconds_to_add is not None:
        payload = {
            "redeem_id": event.id,
            "reward_id": event.reward.id,
            "reward_title": event.reward.title,
            "user_id": event.user_id,
            "user_name": event.user_name,
            "seconds": seconds_to_add,
            "user_input": event.user_input,
        }

        headers = {
            "X-Countdown-Secret": COUNTDOWN_SECRET,
        }

        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.post(
                    COUNTDOWN_URL,
                    json=payload,
                    headers=headers,
                )

            response.raise_for_status()

            logger.info(f"Added {seconds_to_add}s to countdown from {event.user_name}'s redeem: {event.reward.title}")

        except httpx.HTTPStatusError as e:
            logger.error(f"Countdown app rejected redeem POST: {e.response.status_code} {e.response.text}")

        except httpx.RequestError as e:
            logger.error(f"Could not reach countdown app: {e}")
        await redeem_to_audit_log(data, action="countdown_post")
    elif event.reward.id == reward_data.get("become_regular", "become_regular_id"):
        logger.info(
            f"Redeem {event.reward.title!r} by {event.user_name} ({event.user_id}) with reward ID {event.reward.id!r} is a 'Become Regular' redeem."
        )
        await add_regular_from_redeem(event.user_id, event.user_name, event.id)
        await redeem_to_audit_log(data, action="become_regular_redeem")

        if chat.is_ready():
            await chat.send_message(
                TARGET_CHANNEL,
                f"Congratulations {event.user_name}! You are now a regular! 💃",
            )
        else:
            logger.warning(
                "Could not announce regular redeem for %s because chat is not ready.",
                event.user_name,
            )
    elif event.reward.id == reward_data.get("glasses_off", "glasses_off_id"):
        logger.info(
            f"Redeem {event.reward.title!r} by {event.user_name} ({event.user_id}) with reward ID {event.reward.id!r} is a 'Glasses Off' redeem."
        )
        await redeem_to_audit_log(data, action="glasses_off_redeem")

        if chat.is_ready():
            await chat.send_message(
                TARGET_CHANNEL,
                f"{event.user_name} has redeemed 'Glasses Off'! Starting 5 minute 30 second timer",
            )
            asyncio.create_task(
                redeem_timer(
                    chat,
                    TARGET_CHANNEL,
                    duration="glasses_off",
                    finished_text=f"{event.user_name}'s 'Glasses Off' timer is done!",
                )
            )
        else:
            logger.warning(
                "Could not announce 'Glasses Off' redeem for %s because chat is not ready.",
                event.user_name,
            )
    elif event.reward.id == reward_data.get("sensitivity", "sensitivity_id"):
        logger.info(
            f"Redeem {event.reward.title!r} by {event.user_name} ({event.user_id}) with reward ID {event.reward.id!r} is a 'Sensitivity' redeem."
        )
        await redeem_to_audit_log(data, action="sensitivity_redeem")

        if chat.is_ready():
            await chat.send_message(
                TARGET_CHANNEL,
                f"{event.user_name} has redeemed 'Sensitivity'! Starting 5 minute 30 second timer",
            )
            asyncio.create_task(
                redeem_timer(
                    chat,
                    TARGET_CHANNEL,
                    duration="sensitivity",
                    finished_text=f"{event.user_name}'s 'Sensitivity' timer is done!",
                )
            )
        else:
            logger.warning(
                "Could not announce 'Sensitivity' redeem for %s because chat is not ready.",
                event.user_name,
            )
    elif event.reward.id == reward_data.get("in_game_action", "in_game_action_id"):
        logger.info(
            f"Redeem {event.reward.title!r} by {event.user_name} ({event.user_id}) with reward ID {event.reward.id!r} is an 'In-Game Action' redeem."
        )
        await redeem_to_audit_log(data, action="in_game_action_redeem")

        if chat.is_ready():
            await chat.send_message(
                TARGET_CHANNEL,
                f"{event.user_name} has redeemed 'In-Game Action'! You cannot \"{event.user_input}\" for 5 minutes. Timer set!",
            )
            asyncio.create_task(
                redeem_timer(
                    chat,
                    TARGET_CHANNEL,
                    duration="in_game_action",
                    finished_text=f"{event.user_name}'s 'In-Game Action' ({event.user_input}) timer is done!",
                )
            )
        else:
            logger.warning(
                "Could not announce 'In-Game Action' redeem for %s because chat is not ready.",
                event.user_name,
            )
    elif event.reward.id == reward_data.get("in_rl_action", "in_rl_action_id"):
        logger.info(
            f"Redeem {event.reward.title!r} by {event.user_name} ({event.user_id}) with reward ID {event.reward.id!r} is an 'In-Real-Life Action' redeem."
        )
        await redeem_to_audit_log(data, action="in_rl_action_redeem")

        if chat.is_ready():
            await chat.send_message(
                TARGET_CHANNEL,
                f"{event.user_name} has redeemed 'In-Real-Life Action'! You cannot \"{event.user_input}\" for 5 minutes. Timer set!",
            )
            asyncio.create_task(
                redeem_timer(
                    chat,
                    TARGET_CHANNEL,
                    duration="in_game_action",
                    finished_text=f"{event.user_name}'s 'In-Real-Life Action' ({event.user_input}) timer is done!",
                )
            )
        else:
            logger.warning(
                "Could not announce 'In-Real-Life Action' redeem for %s because chat is not ready.",
                event.user_name,
            )
    elif event.reward.id == reward_data.get("ban_word", "ban_word_id"):
        logger.info(
            f"Redeem {event.reward.title!r} by {event.user_name} ({event.user_id}) with reward ID {event.reward.id!r} is a 'Ban Word' redeem."
        )
        await redeem_to_audit_log(data, action="ban_word_redeem")

        if chat.is_ready():
            await chat.send_message(
                TARGET_CHANNEL,
                f"{event.user_name} has redeemed 'Ban Word'! The word '{event.user_input}' is now banned for 5 minutes. Timer set!",
            )
            asyncio.create_task(
                redeem_timer(
                    chat,
                    TARGET_CHANNEL,
                    duration="ban_word",
                    finished_text=f"{event.user_name}'s 'Ban Word' ({event.user_input}) timer is done!",
                )
            )
        else:
            logger.warning(
                "Could not announce 'Ban Word' redeem for %s because chat is not ready.",
                event.user_name,
            )
    elif event.reward.id == reward_data.get("tts", "tts_id"):
        logger.info(
            f"Redeem {event.reward.title!r} by {event.user_name} ({event.user_id}) with reward ID {event.reward.id!r} is a 'TTS' redeem."
        )
        await redeem_to_audit_log(data, action="tts_redeem")

        if chat.is_ready():
            await chat.send_message(
                TARGET_CHANNEL,
                f"{event.user_name} has redeemed 'TTS'!",
            )
            success, message = await send_tts_message(event.user_input)
            if not success:
                logger.warning("TTS message failed: %s", message)
                await chat.send_message(TARGET_CHANNEL, f"TTS message failed: {message}")
        else:
            logger.warning(
                "Could not announce 'TTS' redeem for %s because chat is not ready.",
                event.user_name,
            )
    elif event.reward.id == reward_data.get("check_in", "check_in_id"):
        logger.info(
            f"Redeem {event.reward.title!r} by {event.user_name} ({event.user_id}) with reward ID {event.reward.id!r} is a 'Check-In' redeem."
        )
        await redeem_to_audit_log(data, action="check_in_redeem")

        day_num = await handle_check_in_redeem(event.user_id, event.user_name)

        if chat.is_ready():
            await chat.send_message(
                TARGET_CHANNEL, f"{event.user_name} has redeemed 'Check-In'! Thanks for checking in! Day {day_num}."
            )
        else:
            logger.warning(
                "Could not announce 'Check-In' redeem for %s because chat is not ready.",
                event.user_name,
            )
    elif event.reward.id == reward_data.get("vip", "vip_id"):
        logger.info(
            f"Redeem {event.reward.title!r} by {event.user_name} ({event.user_id}) with reward ID {event.reward.id!r} is a 'VIP' redeem."
        )
        await redeem_to_audit_log(data, action="vip_redeem")

        success = await handle_vip_redeem(event.user_id, event.user_name)

        if chat.is_ready():
            if success:
                await chat.send_message(
                    TARGET_CHANNEL, f"{event.user_name} has redeemed 'VIP'! Congratulations on becoming a VIP!"
                )
            else:
                await chat.send_message(TARGET_CHANNEL, f"{event.user_name}'s VIP redeem failed.")
        else:
            logger.warning(
                "Could not announce 'VIP' redeem for %s because chat is not ready.",
                event.user_name,
            )
    else:
        logger.info(
            f"Redeem {event.reward.title!r} by {event.user_name} ({event.user_id}) "
            f"with reward ID {event.reward.id!r} does not have a specific action defined. No action will be taken."
        )


async def on_shared_chat_begin(event: ChannelSharedChatBeginEvent, chat: Chat) -> None:
    global shared_chat_session_id

    data = event.event
    shared_chat_session_id = data.session_id

    participants = ", ".join(p.broadcaster_user_login for p in data.participants)

    logger.info(
        "Shared chat started: session=%s host=%s participants=%s",
        data.session_id,
        data.host_broadcaster_user_login,
        participants,
    )

    await announce(chat, f"Shared chat started! Host: {data.host_broadcaster_user_login}. Participants: {participants}")


async def on_shared_chat_update(event: ChannelSharedChatUpdateEvent, chat: Chat) -> None:
    data = event.event

    participants = ", ".join(p.broadcaster_user_login for p in data.participants)

    logger.info(
        "Shared chat updated: session=%s host=%s participants=%s",
        data.session_id,
        data.host_broadcaster_user_login,
        participants,
    )

    await announce(chat, f"Shared chat updated! Host: {data.host_broadcaster_user_login}. Participants: {participants}")


async def on_shared_chat_end(event: ChannelSharedChatEndEvent, chat: Chat) -> None:
    global shared_chat_session_id

    data = event.event

    logger.info(
        "Shared chat ended: session=%s host=%s",
        data.session_id,
        data.host_broadcaster_user_login,
    )

    if shared_chat_session_id == data.session_id:
        shared_chat_session_id = None

    await announce(chat, f"Shared chat ended! Host: {data.host_broadcaster_user_login}.")


async def announce(chat: Chat, message: str) -> None:
    if chat.is_ready():
        await chat.send_message(TARGET_CHANNEL, message)
    else:
        logger.warning("Could not announce because chat is not ready: %s", message)


# -----------------------------
# Main
# -----------------------------


async def main() -> None:
    global twitch
    global broadcaster_id
    global moderator_id
    global timeout_stack

    timeout_stack = LimitedStack[str](max_size=MAX_TIMEOUT_STACK_SIZE)

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

    async def on_channel_point_redeem_with_chat(
        data: ChannelPointsCustomRewardRedemptionAddEvent,
    ) -> None:
        await on_channel_point_redeem(data, chat)

    eventsub = EventSubWebsocket(twitch)
    eventsub.start()

    await eventsub.listen_channel_raid(
        on_raid,
        to_broadcaster_user_id=broadcaster_id,
    )

    await eventsub.listen_channel_points_custom_reward_redemption_add(
        broadcaster_user_id=broadcaster_id,
        callback=on_channel_point_redeem_with_chat,
    )

    async def on_shared_chat_begin_with_chat(
        event: ChannelSharedChatBeginEvent,
    ) -> None:
        await on_shared_chat_begin(event, chat)

    async def on_shared_chat_update_with_chat(
        event: ChannelSharedChatUpdateEvent,
    ) -> None:
        await on_shared_chat_update(event, chat)

    async def on_shared_chat_end_with_chat(
        event: ChannelSharedChatEndEvent,
    ) -> None:
        await on_shared_chat_end(event, chat)

    await eventsub.listen_channel_shared_chat_begin(
        broadcaster_user_id=broadcaster_id,
        callback=on_shared_chat_begin_with_chat,
    )

    await eventsub.listen_channel_shared_chat_update(
        broadcaster_user_id=broadcaster_id,
        callback=on_shared_chat_update_with_chat,
    )

    await eventsub.listen_channel_shared_chat_end(
        broadcaster_user_id=broadcaster_id,
        callback=on_shared_chat_end_with_chat,
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

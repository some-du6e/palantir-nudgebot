import os
import random
import sqlite3
import threading
import time
import zoneinfo
from datetime import datetime

import requests
import sentry_sdk
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

load_dotenv()


# Env
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")
SENTRY_DSN = os.getenv("SENTRY_DSN")
CMAN_USER_ID = os.getenv("CMAN_USER_ID")
PERSONAL_CHANNEL_ID = os.getenv("PERSONAL_CHANNEL_ID")
PERSONAL_USERGROUP_ID = os.getenv("PERSONAL_USERGROUP_ID")
BOT_TIMEZONE = os.getenv("BOT_TIMEZONE")
ADVERT_CHANNEL = os.getenv("ADVERT_CHANNEL")


# DB Stuff UwU
def init_db():
    conn = sqlite3.connect("nudgebot.db")
    cursor = conn.cursor()
    # Restrict List
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS restrictlist (
        user_id TEXT PRIMARY KEY,
        reason TEXT,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Recap Settings
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS recap_settings (
        user_id TEXT PRIMARY KEY,
        recap_time TEXT DEFAULT '21:00',
        send_hackatime_stats INTEGER DEFAULT 1,
        recaps_disabled INTEGER DEFAULT 0
        )
    """)
    for col, typedef in [
        ("recaps_disabled", "INTEGER DEFAULT 0"),
    ]:
        try:
            cursor.execute(f"ALTER TABLE recap_settings ADD COLUMN {col} {typedef}")
        except sqlite3.OperationalError:
            pass
    # Bot Settings
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bot_settings (
        key TEXT PRIMARY KEY,
        value TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS purge_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id TEXT NOT NULL,
        created_by TEXT NOT NULL,
        start_at TEXT,
        deadline_at TEXT NOT NULL,
        active INTEGER DEFAULT 1,
        started_at TIMESTAMP,
        completed_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    for col, typedef in [
        ("start_at", "TEXT"),
        ("started_at", "TIMESTAMP"),
        ("completed_at", "TIMESTAMP"),
    ]:
        try:
            cursor.execute(f"ALTER TABLE purge_sessions ADD COLUMN {col} {typedef}")
        except sqlite3.OperationalError:
            pass
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS purge_targets (
        session_id INTEGER NOT NULL,
        user_id TEXT NOT NULL,
        dm_channel_id TEXT,
        responded INTEGER DEFAULT 0,
        responded_at TIMESTAMP,
        PRIMARY KEY (session_id, user_id)
        )
    """)
    # Slash command prefix init because Stinky slack likes to take over commands if the name is same
    cursor.execute(
        "INSERT OR IGNORE INTO bot_settings (key, value) VALUES (? , ?)",
        ("slash_prefix", str(random.randint(1000, 9999))),
    )
    conn.commit()
    cursor.execute("SELECT value from bot_settings WHERE key = ?", ("slash_prefix",))
    prefix = cursor.fetchone()[0]
    conn.close()
    return prefix


SLASH_PREFIX = init_db()

print(f"Hi! My Nudgebot prefix is {SLASH_PREFIX}!")

app = App(token=SLACK_BOT_TOKEN)

# fetch channel name
channel_name = app.client.conversations_info(channel=PERSONAL_CHANNEL_ID)["channel"][
    "name"
]
# Sentry so good bruh
if SENTRY_DSN.strip() == "# OPTIONAL (error tracking)":
    SENTRY_DSN = False

if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        enable_logs=True,
    )


@app.error
def global_error_handler(error, body, logger):
    logger.exception(f"Unhandled error: {error}")
    logger.info(f"Request body: {body}")
    if SENTRY_DSN:
        sentry_sdk.capture_exception(error)


def is_user_restricted(user_id: str) -> bool:
    conn = sqlite3.connect("nudgebot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM restrictlist WHERE user_id =?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result is not None


def is_recaps_disabled() -> bool:
    conn = sqlite3.connect("nudgebot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM bot_settings WHERE key = 'recaps_disabled'")
    result = cursor.fetchone()
    conn.close()
    return result is not None and str(result[0]) == "1"


def add_user_to_restrictlist(user_id: str, reason: str = ""):
    conn = sqlite3.connect("nudgebot.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR IGNORE INTO restrictlist (user_id, reason) VALUES (?, ?)",
        (user_id, reason),
    )
    conn.commit()
    conn.close()


def remove_user_from_restrictlist(user_id: str):
    conn = sqlite3.connect("nudgebot.db")
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM restrictlist WHERE user_id = ?",
        (user_id,),
    )
    conn.commit()
    conn.close()


def is_idv_required() -> bool:
    conn = sqlite3.connect("nudgebot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM bot_settings WHERE key = 'require_idv'")
    result = cursor.fetchone()
    conn.close()
    return result is not None and str(result[0]) == "1"


def is_requests_off() -> bool:
    conn = sqlite3.connect("nudgebot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM bot_settings WHERE key = 'is_requests_off'")
    result = cursor.fetchone()
    conn.close()
    return result is not None and str(result[0]) == "0"


def auto_thread_toggle() -> bool:
    conn = sqlite3.connect("nudgebot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM bot_settings WHERE key = 'auto_thread_toggle'")
    result = cursor.fetchone()
    conn.close()
    return result is not None and str(result[0]) == "1"


def contains_auto_thread_trigger(message) -> bool:
    def has_trigger_text(text: str) -> bool:
        if not text:
            return False

        if "<!here>" in text or "@here" in text:
            return True
        if "<!channel>" in text or "@channel" in text:
            return True
        if PERSONAL_USERGROUP_ID and f"<!subteam^{PERSONAL_USERGROUP_ID}" in text:
            return True

        return False

    def scan(value) -> bool:
        if isinstance(value, str):
            return has_trigger_text(value)

        if isinstance(value, list):
            return any(scan(item) for item in value)

        if isinstance(value, dict):
            if value.get("type") == "broadcast" and value.get("range") in {
                "here",
                "channel",
            }:
                return True

            if value.get("type") in {"usergroup", "user_group"}:
                if not PERSONAL_USERGROUP_ID:
                    return True

                if value.get("usergroup_id") == PERSONAL_USERGROUP_ID:
                    return True
                if value.get("subteam_id") == PERSONAL_USERGROUP_ID:
                    return True

            return any(scan(item) for item in value.values())

        return False

    return scan(message.get("text", "")) or scan(message.get("blocks", []))


def is_joining_paused() -> bool:
    conn = sqlite3.connect("nudgebot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM bot_settings WHERE key = 'is_paused'")
    result = cursor.fetchone()
    conn.close()
    return result is not None and str(result[0]) == "1"


@app.command(f"/{SLASH_PREFIX}-about")
def help_command(command, ack, client):
    ack()

    client.chat_postEphemeral(
        channel=command["channel_id"],
        user=command["user_id"],
        text="Nudgebot About",
        blocks=[
            {
                "type": "markdown",
                "text": "Hey, I am Nudgebot! A Slack bot configured for managing personal channels! :yay-wave:",
            },
            {"type": "divider"},
            {
                "type": "markdown",
                "text": f"Bot Info!\n Configured Channel: <#{PERSONAL_CHANNEL_ID}>\n Bot Owner: <@{CMAN_USER_ID}> \n Command Prefix: {SLASH_PREFIX}",
            },
            {"type": "divider"},
            {
                "type": "markdown",
                "text": f"Commands:\n /{SLASH_PREFIX}-advertise-channel - Share your channel to #neighbourhood!\n /{SLASH_PREFIX}-list-restricted-users - List of restricted users for your channel.\n/{SLASH_PREFIX}-restrict-from-channel @user - Restrict a user from joining your channel.\n/{SLASH_PREFIX}-unrestrict-from-channel @user - Unrestrict a user from joining your channel.\n /{SLASH_PREFIX}-channel-purge - Kick out inactive people from your channel\n /{SLASH_PREFIX}-clean-up-group-list -Remove usergroup members who are no longer in the channel.\n /join-channel-{SLASH_PREFIX} - Request to join {channel_name}! \n /{SLASH_PREFIX}-say <text> say something as your nudgebot!",
            },
            {"type": "divider"},
            {
                "type": "markdown",
                "text": "Credits!\n Nudgebot is developed by <@U09PHG7RLGG>! Check out the Nudgebot [repo](https://github.com/Snowflake6413/nudgebot)!",
            },
        ],
    )


@app.action("ignore_recap_action")
def handle_ignore_recap(ack, body, client):
    ack()

    user_id = body["user"]["id"]
    channel_id = body["channel"]["id"]
    message_ts = body["message"]["ts"]

    if user_id != CMAN_USER_ID:
        client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text=f"<@{CMAN_USER_ID}> can only dismiss this recap reminder!",
        )
        return

    try:
        client.chat_delete(channel=channel_id, ts=message_ts)
    except Exception as e:
        sentry_sdk.capture_exception(e)


# RECAP.
@app.action("open_recap_modal")
def handle_recap_button(ack, body, client, logger):
    ack()

    user_id = body["user"]["id"]

    if user_id != CMAN_USER_ID:
        client.chat_postEphemeral(
            channel=body["channel"]["id"],
            users=user_id,
            text=f"Only <@{CMAN_USER_ID}> can only answer the recap prompt, ya goober! :neocat_knives:",
        )
        return

    message_ts = body["message"]["ts"]

    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "recap_view",
            "private_metadata": message_ts,
            "title": {"type": "plain_text", "text": "Recap Form", "emoji": True},
            "submit": {"type": "plain_text", "text": "submit recap!", "emoji": True},
            "close": {"type": "plain_text", "text": "cancel", "emoji": True},
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"heya, <@{user_id}>! hope you are having a nice day! mind filling this out? :neocat_aww:",
                    },
                },
                {
                    "type": "section",
                    "block_id": "feeling_block",
                    "text": {"type": "mrkdwn", "text": "how are you feeling today?"},
                    "accessory": {
                        "type": "static_select",
                        "placeholder": {
                            "type": "plain_text",
                            "text": "Select an item",
                            "emoji": True,
                        },
                        "options": [
                            {
                                "text": {
                                    "type": "plain_text",
                                    "text": ":neocat_happy: excited!",
                                    "emoji": True,
                                },
                                "value": "value-0",
                            },
                            {
                                "text": {
                                    "type": "plain_text",
                                    "text": ":neocat: happy!",
                                    "emoji": True,
                                },
                                "value": "value-1",
                            },
                            {
                                "text": {
                                    "type": "plain_text",
                                    "text": ":neocat_blank: meh/neutral",
                                    "emoji": True,
                                },
                                "value": "value-2",
                            },
                            {
                                "text": {
                                    "type": "plain_text",
                                    "text": ":neocat_sad: sad",
                                    "emoji": True,
                                },
                                "value": "value-3",
                            },
                            {
                                "text": {
                                    "type": "plain_text",
                                    "text": ":neocat_up_sleep: tired",
                                    "emoji": True,
                                },
                                "value": "value-4",
                            },
                            {
                                "text": {
                                    "type": "plain_text",
                                    "text": ":neocat_x_x: stressed",
                                    "emoji": True,
                                },
                                "value": "value-5",
                            },
                            {
                                "text": {
                                    "type": "plain_text",
                                    "text": ":neocat_angry: angry",
                                    "emoji": True,
                                },
                                "value": "value-6",
                            },
                        ],
                        "action_id": "feeling_select",
                    },
                },
                {
                    "type": "input",
                    "block_id": "fortoday_block",
                    "element": {
                        "type": "plain_text_input",
                        "multiline": True,
                        "action_id": "fortoday_input",
                    },
                    "label": {
                        "type": "plain_text",
                        "text": "what did you do today?",
                        "emoji": True,
                    },
                    "optional": False,
                },
            ],
        },
    )


@app.view("recap_view")
def handle_recap_submission(ack, body, client, view, logger):
    ack()

    user_id = body["user"]["id"]

    thread_ts = view.get("private_metadata")

    state_values = view["state"]["values"]
    feeling = state_values["feeling_block"]["feeling_select"]["selected_option"][
        "text"
    ]["text"]
    fortoday = state_values["fortoday_block"]["fortoday_input"]["value"]

    # wait let me see the db if hackatime stats are toggled
    conn = sqlite3.connect("nudgebot.db")
    cursor = conn.cursor()
    cursor.execute(
        "SELECT send_hackatime_stats FROM recap_settings WHERE user_id = ?",
        (user_id,),
    )
    row = cursor.fetchone()
    conn.close()

    send_hackatime_stats = True if row is None else bool(row[0])

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"<@{user_id}>'s recap for today! :yesyes:",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": ":wave1parrot::wave2parrot::wave3parrot::wave4parrot::wave5parrot::wave6parrot:",
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"<@{user_id}> is feeling {feeling}"},
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"what did <@{user_id}> do today?"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": fortoday},
        },
    ]
    if send_hackatime_stats:
        # hackatime things
        now = datetime.now()
        today = now.strftime("%Y%m%d")
        response = requests.get(
            f"https://hackatime.hackclub.com/api/v1/users/{CMAN_USER_ID}/stats?start_date={today}"
        )
        data = response.json()["data"]

        total = data["human_readable_total"]
        streak = data["streak"]
        top_lang = data["languages"][0]
        hackatime = (
            f":clock4: *Total time:* {total}\n"
            f":streak: *Streak:* {streak}\n"
            f"*Top language:*  {top_lang['name']} ({top_lang['text']}, {top_lang['percent']}% "
        )
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"<@{user_id}>'s recap for today! :yesyes:",
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": ":wave1parrot::wave2parrot::wave3parrot::wave4parrot::wave5parrot::wave6parrot:",
                },
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"<@{user_id}> is feeling {feeling}",
                },
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"what did <@{user_id}> do today?"},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"{fortoday}"},
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":hackatime: <@{user_id}>'s hackatime stats for today!",
                },
            },
            {"type": "section", "text": {"type": "mrkdwn", "text": f"{hackatime}"}},
        ]

    client.chat_postMessage(
        channel=PERSONAL_CHANNEL_ID,
        blocks=blocks,
        text=f"<@{user_id}>'s recap for today!",
    )

    if thread_ts:
        try:
            client.chat_delete(channel=PERSONAL_CHANNEL_ID, ts=thread_ts)
        except Exception as e:
            sentry_sdk.capture_exception(e)


# Handle purge responses AND auto threading
@app.message()
def handle_message(message, client, logger):
    subtype = message.get("subtype")
    if subtype in {"message_changed", "message_deleted"}:
        return

    channel_id = message.get("channel")
    user_id = message.get("user")
    channel_type = message.get("channel_type")

    if not channel_id:
        return

    # Purge response handling
    if user_id and (channel_type == "im" or str(channel_id).startswith("D")):
        conn = sqlite3.connect("nudgebot.db")
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT pt.session_id
                FROM purge_targets pt
                JOIN purge_sessions ps ON pt.session_id = ps.id
                WHERE pt.dm_channel_id = ?
                  AND pt.responded = 0
                  AND ps.active = 1
                  AND ps.started_at IS NOT NULL
                ORDER BY ps.started_at DESC
                LIMIT 1
                """,
                (channel_id,),
            )
            row = cursor.fetchone()

            if row is None:
                cursor.execute(
                    """
                    SELECT pt.session_id
                    FROM purge_targets pt
                    JOIN purge_sessions ps ON pt.session_id = ps.id
                    WHERE pt.user_id = ?
                      AND pt.responded = 0
                      AND ps.active = 1
                      AND ps.started_at IS NOT NULL
                    ORDER BY ps.started_at DESC
                    LIMIT 1
                    """,
                    (user_id,),
                )
                row = cursor.fetchone()

            if row:
                cursor.execute(
                    """
                    UPDATE purge_targets
                    SET responded = 1, responded_at = ?
                    WHERE session_id = ? AND user_id = ?
                    """,
                    (datetime.now().isoformat(), row[0], user_id),
                )
                conn.commit()
                client.chat_postMessage(
                    channel=channel_id,
                    text="you're safe. :white_check_mark:",
                )
        except Exception as e:
            sentry_sdk.capture_exception(e)
        finally:
            conn.close()
        return

    # Auto thread handling
    if contains_auto_thread_trigger(message):
        if auto_thread_toggle():
            client.chat_postMessage(channel=channel_id, text=":thread:")


@app.shortcut("delete_bot_msg_shortcut")
def delete_bot_msg_shortcut(ack, shortcut, body, client):
    ack()

    # capture user id
    user_id = shortcut["user"]["id"]

    if user_id != CMAN_USER_ID:
        client.chat_postEphemeral(
            channel=shortcut["channel"]["id"],
            user=user_id,
            text=f"hey, <@{user_id}>! you can't delete this bot message because you aren't the channel manager of that channel!",
        )
        return

    channel_id = shortcut["channel"]["id"]
    message_ts = shortcut["message"]["ts"]

    try:
        client.chat_delete(channel=channel_id, ts=message_ts)
    except Exception as e:
        sentry_sdk.capture_exception(e)


# advertise STUFF uwu
@app.command(f"/{SLASH_PREFIX}-advertise-channel")
def advertise_channel(command, client, ack, respond, logger):
    ack()

    invoker_user_id = command["user_id"]
    trigger_id = command["trigger_id"]
    if invoker_user_id != CMAN_USER_ID:
        respond("You are not authorized to run this command!")
        return

    try:
        client.views_open(
            trigger_id=trigger_id,
            view={
                "type": "modal",
                "callback_id": "advertise_channel_modal",
                "title": {
                    "type": "plain_text",
                    "text": "Advertise Channel",
                    "emoji": True,
                },
                "submit": {"type": "plain_text", "text": "Advertise", "emoji": True},
                "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "plain_text",
                            "text": "Enter your advertisement message! :3cnuke:",
                            "emoji": True,
                        },
                    },
                    {
                        "type": "input",
                        "block_id": "message_input_block",
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "message_input-action",
                        },
                        "label": {
                            "type": "plain_text",
                            "text": "Message",
                            "emoji": True,
                        },
                        "optional": False,
                    },
                ],
            },
        )
    except Exception as e:
        logger.exception(f"Error opening advertise-channel modal: {e}")
        respond("Something went wrong opening the modal. Please try again!")


@app.view("advertise_channel_modal")
def handle_advertise_channel_submission(ack, body, view, client):
    ack()

    user_id = body["user"]["id"]

    message_text = view["state"]["values"]["message_input_block"][
        "message_input-action"
    ]["value"]

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": message_text}},
        {"type": "divider"},
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"sent by: <@{user_id}> "}],
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"an ad to join <#{PERSONAL_CHANNEL_ID}> ({channel_name}) (owned by: <@{CMAN_USER_ID}>)",
                }
            ],
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Join Channel!",
                        "emoji": True,
                    },
                    "value": "join_from_ad",
                    "action_id": "join_pc_button_home",
                }
            ],
        },
    ]

    client.chat_postMessage(
        channel=ADVERT_CHANNEL,  # Soon.
        blocks=blocks,
    )


# :troll:
@app.command(f"/{SLASH_PREFIX}-say")
def speak_command(ack, respond, command, client):
    ack()

    invoker_user_id = command["user_id"]
    if invoker_user_id != CMAN_USER_ID:
        respond("you can't run this command!")
        return

    message_to_say = command.get("text", "").strip()

    if not message_to_say:
        respond(f"Usage: /{SLASH_PREFIX}-say <your message here>")
        return

    try:
        client.chat_postMessage(channel=PERSONAL_CHANNEL_ID, text=message_to_say)

    except Exception as e:
        sentry_sdk.capture_exception(e)


# when a member joins a channel
@app.event("member_joined_channel")
def handle_member_invited_channel_and_channel_join(body, client, context, say):
    channel = body["event"]["channel"]
    new_user = body["event"]["user"]
    bot_user_id = context.get("bot_user_id")

    if is_user_restricted(new_user):
        client.conversations_kick(channel=channel, users=new_user)
        return

    if new_user == bot_user_id:
        if channel != PERSONAL_CHANNEL_ID:
            leave_blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"hi! it seems like you invited me to a channel i am not supposed to be in! this bot is configure for <@{CMAN_USER_ID}>'s channel! :neocat_sad:",
                    },
                },
                {"type": "divider"},
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "if you are looking to deploy a nudgebot for your channel, please see this <https://github.com/Snowflake6413/nudgebot|github repo!> you can deploy your nudgebot on <https://dashboard.hackclub.app|Nest> since it's free!",
                    },
                },
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": "leaving this channel automatically now.",
                        }
                    ],
                },
            ]
            client.chat_postMessage(channel=channel, blocks=leave_blocks)
            client.conversations_leave(channel=channel)
            return

    if channel == PERSONAL_CHANNEL_ID and new_user != bot_user_id:
        client.chat_postMessage(
            channel=CMAN_USER_ID,
            text=f"<@{new_user}> joined your channel! :yay-67:",
        )
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"yo, <@{new_user}>! :oi: welcome to <@{CMAN_USER_ID}>'s channel! we hope you have fun chatting with people in this channel!",
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "welcome them :drgn_wave:",
                            "emoji": True,
                        },
                        "value": "click_me_123",
                        "action_id": "sayhello",
                    }
                ],
            },
            {"type": "divider"},
            {
                "type": "context",
                "elements": [
                    {
                        "type": "plain_text",
                        "text": ":wave1parrot::wave2parrot::wave3parrot::wave4parrot::wave5parrot::wave6parrot:",
                        "emoji": True,
                    }
                ],
            },
        ]

        client.chat_postMessage(
            channel=channel,
            blocks=blocks,
        )


# noo why do you LEAVE
@app.event("member_left_channel")
def handle_member_left_channel(body, client):
    channel = body["event"]["channel"]
    left_user = body["event"]["user"]

    try:
        if channel == PERSONAL_CHANNEL_ID:
            group_info = client.usergroups_users_list(usergroup=PERSONAL_USERGROUP_ID)
            current_users = group_info["users"]

            if left_user in current_users:
                current_users.remove(left_user)

                client.usergroups_user_update(
                    usergroup=PERSONAL_USERGROUP_ID, users=",".join(current_users)
                )

            client.chat_postMessage(
                channel=left_user,
                text=f"Hey! It looks like you left <#{channel}> ({channel}). Because of this, you'll be removed from the user group, thx for coming! :)",
            )

            client.chat_postMessage(
                channel=CMAN_USER_ID,
                text=f"<@{left_user}> left your channel.. :yay-sob:",
            )
    except Exception as e:
        sentry_sdk.capture_exception(e)


# anti spam function >:(
welcomed_users = {}


# welcome button logic!
@app.action("sayhello")
def greet_new_user(ack, say, body):
    ack()

    user_id = body["user"]["id"]
    thread_ts = body["message"]["ts"]

    if thread_ts not in welcomed_users:
        welcomed_users[thread_ts] = set()

    if user_id in welcomed_users[thread_ts]:
        return

    welcomed_users[thread_ts].add(user_id)

    say(text=f"<@{user_id}> says hello :drgn_wave:", thread_ts=thread_ts)


@app.command(f"/{SLASH_PREFIX}-list-restricted-users")
def list_restricted_users_command(ack, respond, command):
    ack()

    invoker_user_id = command["user_id"]
    if invoker_user_id != CMAN_USER_ID:
        respond("You are not authorized to run this command. :nuhuhvro:")
        return

    conn = sqlite3.connect("nudgebot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, reason, added_at FROM restrictlist")
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        respond("No restricted users in this list.")
        return
    msg = "Restricted Users:\n"
    for user_id, reason, added_at in rows:
        msg += f"<@{user_id}> - Reason: {reason} (Added: {added_at})\n"

    respond(msg)


@app.command(f"/{SLASH_PREFIX}-restrict-from-channel")
def restrict_user_command(ack, respond, say, command, client):
    ack()

    invoker_user_id = command["user_id"]

    if invoker_user_id != CMAN_USER_ID:
        respond("You are not authorized to run this command. :nuhuhvro:")
        return

    user_id_text = command.get("text", "").strip()
    if not user_id_text:
        respond("Please provide a user to restrict, e.g., /restrict-from-channel @user")
        return

    user_id = user_id_text.replace("<@", "").replace(">", "").split("|")[0]

    add_user_to_restrictlist(user_id, reason="Restricted via slash command")
    respond(f"Successfully restricted <@{user_id}>!")


@app.command(f"/{SLASH_PREFIX}-unrestrict-from-channel")
def unrestrict_user_command(ack, respond, say, command, client):
    ack()

    invoker_user_id = command["user_id"]

    if invoker_user_id != CMAN_USER_ID:
        respond("You are not authorized to run this command :nuhuhvro:")
        return

    user_id_text = command.get("text", "").strip()
    if not user_id_text:
        respond("Please provide a user to restrict, e.g., /restrict-from-channel @user")
        return

    user_id = user_id_text.replace("<@", "").replace(">", "").split("|")[0]

    remove_user_from_restrictlist(user_id)
    respond(f"Sucessfully unrestricted <@{user_id}>!")


@app.command(f"/{SLASH_PREFIX}-channel-purge")
def channel_purge_command(ack, body, client, respond):
    ack()

    if body["user_id"] != CMAN_USER_ID:
        respond("You are not authorized to run this command :nuhuhvro:")
        return

    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "purge_schedule_modal",
            "title": {
                "type": "plain_text",
                "text": "Channel Purge",
                "emoji": True,
            },
            "submit": {
                "type": "plain_text",
                "text": "Schedule",
                "emoji": True,
            },
            "close": {
                "type": "plain_text",
                "text": "Cancel",
                "emoji": True,
            },
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "plain_text",
                        "text": "Schedule a time for a channel purge.",
                        "emoji": True,
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "plain_text",
                        "text": "After the purge, stats (how many people were safe, kicked, errors) will be sent to you via DM.",
                        "emoji": True,
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "plain_text",
                        "text": ":stop: Anybody who doesn't reply to your DM will get auto-kicked if the deadline passes. :stop:",
                        "emoji": True,
                    },
                },
                {
                    "type": "input",
                    "block_id": "purge_start_block",
                    "element": {
                        "type": "datetimepicker",
                        "action_id": "purge_start_picker",
                    },
                    "label": {
                        "type": "plain_text",
                        "text": "Start Date (when will your purge start?)",
                        "emoji": True,
                    },
                    "optional": False,
                },
                {
                    "type": "input",
                    "block_id": "purge_end_block",
                    "element": {
                        "type": "datetimepicker",
                        "action_id": "purge_end_picker",
                    },
                    "label": {
                        "type": "plain_text",
                        "text": "End Date (when will your purge end?)",
                        "emoji": True,
                    },
                    "optional": False,
                },
            ],
        },
    )


@app.view("purge_schedule_modal")
def handle_purge_schedule_submission(ack, body, view, client):
    ack()

    tz = zoneinfo.ZoneInfo(BOT_TIMEZONE or "America/New_York")

    if body["user"]["id"] != CMAN_USER_ID:
        return

    start_raw = view["state"]["values"]["purge_start_block"]["purge_start_picker"][
        "selected_date_time"
    ]
    end_raw = view["state"]["values"]["purge_end_block"]["purge_end_picker"][
        "selected_date_time"
    ]

    start_at = datetime.fromtimestamp(
        int(start_raw), tz=tz
    )  # TODO: use Env for timezone
    end_at = datetime.fromtimestamp(int(end_raw), tz=tz)  # same as above

    if end_at <= start_at:
        client.chat_postMessage(
            channel=CMAN_USER_ID, text="purge end time must be after the start time!"
        )
        return

    conn = sqlite3.connect("nudgebot.db")
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO purge_sessions (channel_id, created_by, start_at, deadline_at, active)
        VALUES (?, ?, ?, ?, 1)
        """,
        (
            PERSONAL_CHANNEL_ID,
            body["user"]["id"],
            start_at.isoformat(),
            end_at.isoformat(),
        ),
    )
    conn.commit()
    conn.close()

    client.chat_postMessage(
        channel=CMAN_USER_ID,
        text=(
            f"Scheduled purge for <#{PERSONAL_CHANNEL_ID}>\n"
            f"Start: {start_at:%Y-%m-%d %H:%M}\n"
            f"End:  {end_at:%Y-%m-%d %H:%M}\n"
        ),
    )


def schedule_purge_msg(client):
    tz = zoneinfo.ZoneInfo(BOT_TIMEZONE or "America/New_York")
    bot_user_id = client.auth_test()["user_id"]

    while True:
        conn = None
        try:
            now = datetime.now(tz)
            conn = sqlite3.connect("nudgebot.db")
            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT id, channel_id, created_by, start_at, deadline_at, started_at
                FROM purge_sessions
                WHERE active = 1
                ORDER BY start_at
                """
            )
            sessions = cursor.fetchall()

            for (
                session_id,
                channel_id,
                created_by,
                start_raw,
                deadline_raw,
                started_raw,
            ) in sessions:
                start_at = datetime.fromisoformat(start_raw)
                deadline_at = datetime.fromisoformat(deadline_raw)
                if start_at.tzinfo is None:
                    start_at = start_at.replace(tzinfo=tz)
                if deadline_at.tzinfo is None:
                    deadline_at = deadline_at.replace(tzinfo=tz)

                # start the PURGE >:3
                if started_raw is None and now >= start_at:
                    cursor.execute(
                        "UPDATE purge_sessions SET started_at = ? WHERE id = ?",
                        (now.isoformat(), session_id),
                    )
                    conn.commit()

                    members_response = client.conversations_members(channel=channel_id)
                    members = [
                        user_id
                        for user_id in members_response["members"]
                        if user_id not in (bot_user_id, CMAN_USER_ID)
                    ]

                    dm_errors = 0
                    for user_id in members:
                        cursor.execute(
                            """

                            INSERT OR IGNORE into purge_targets
                            (session_id, user_id, dm_channel_id, responded, responded_at)
                            VALUES (?, ?, NULL, 0, NULL)
                            """,
                            (session_id, user_id),
                        )
                        try:
                            dm = client.conversations_open(users=user_id)
                            dm_channel_id = dm["channel"]["id"]
                            cursor.execute(
                                """

                                UPDATE purge_targets
                                SET dm_channel_id = ?
                                WHERE session_id = ? AND user_id = ?
                                """,
                                (dm_channel_id, session_id, user_id),
                            )
                            conn.commit()
                            client.chat_postMessage(
                                channel=dm_channel_id,
                                text=(
                                    f"Hey, <@{user_id}>! :hii: <@{CMAN_USER_ID}> is doing a channel purge in <#{PERSONAL_CHANNEL_ID}>."
                                    f" The deadline is {deadline_at.strftime('%B %d, %Y at %#I:%M %p %Z')} (bot timezone)."
                                    f" Reply anything to me before then to stay safe from this purge!"
                                ),
                            )
                        except Exception as e:
                            dm_errors += 1
                            sentry_sdk.capture_exception(e)

                    client.chat_postMessage(
                        channel=created_by,
                        text=(
                            f"Purge started for <#{channel_id}>!\n"
                            f"DM errors: {dm_errors}"
                        ),
                    )
                elif started_raw is not None and now >= deadline_at:
                    cursor.execute(
                        """
                        SELECT user_id
                        FROM purge_targets
                        WHERE session_id = ? AND responded = 0
                        """,
                        (session_id,),
                    )
                    pending_users = [row[0] for row in cursor.fetchall()]
                    kicked = 0
                    kick_errors = 0

                    for user_id in pending_users:
                        cursor.execute(
                            """
                            SELECT responded
                            FROM purge_targets
                            WHERE session_id = ? AND user_id = ?
                            """,
                            (session_id, user_id),
                        )
                        row = cursor.fetchone()
                        if row and row[0]:
                            continue
                        try:
                            client.conversations_kick(channel=channel_id, users=user_id)
                            kicked += 1
                        except Exception as e:
                            kick_errors += 1
                            sentry_sdk.capture_exception(e)
                    cursor.execute(
                        """
                        UPDATE purge_sessions
                        SET active = 0,
                            completed_at = ?
                        WHERE id = ?
                        """,
                        (now.isoformat(), session_id),
                    )
                    conn.commit()

                    cursor.execute(
                        """
                        SELECT COUNT(*)
                        FROM purge_targets
                        WHERE session_id = ? AND responded = 1
                        """,
                        (session_id,),
                    )
                    safe_count = cursor.fetchone()[0]

                    client.chat_postMessage(
                        channel=created_by,
                        text=(
                            f"Purge finished!\n"
                            f"# of people safe from purge: {safe_count}\n"
                            f"Kicked: {kicked}\n"
                            f"Kick errors: {kick_errors}"
                        ),
                    )

                elif started_raw is None and now >= deadline_at:
                    cursor.execute(
                        """
                        UPDATE purge_sessions
                        SET active = 0,
                            completed_at = ?
                        WHERE id = ?
                        """,
                        (now.isoformat(), session_id),
                    )
                    conn.commit()
        except Exception as e:
            sentry_sdk.capture_exception(e)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        time.sleep(30)


@app.command(f"/{SLASH_PREFIX}-clean-up-group-list")
def usergroup_cleaner(ack, respond, command, client):
    ack()

    invoker_user_id = command["user_id"]
    if invoker_user_id != CMAN_USER_ID:
        respond("sowwy but you can't run this command! :nuhuhvro:")
        return

    try:
        group_info = client.usergroups_users_list(usergroup=PERSONAL_USERGROUP_ID)
        usergroup_users = group_info.get("users", [])

        if not usergroup_users:
            respond("looks like your usergroup is empty, nothing to clean up!")
            return

        members_response = client.conversations_members(channel=PERSONAL_CHANNEL_ID)
        channel_members = members_response.get("members", [])

        to_remove = [uid for uid in usergroup_users if uid not in channel_members]

        if not to_remove:
            respond(
                "looks like everyone in the usergroup is already in your channel, so there's nothing to clean up!"
            )
            return

        updated_users = [uid for uid in usergroup_users if uid in channel_members]
        client.usergroups_users_update(
            usergroup=PERSONAL_USERGROUP_ID, users=",".join(updated_users)
        )
        respond(
            f"Cleaned up the usergroup!\n"
            f"Swept up {len(to_remove)} user(s).\n"
            f"Kept {len(updated_users)} user(s) who are still in this channel."
        )
    except Exception as e:
        sentry_sdk.capture_exception(e)


# Join via Slash command
@app.command(f"/join-channel-{SLASH_PREFIX}")
def joining_guardian(ack, respond, say, command, client, body):
    ack()

    invoker_user_id = command["user_id"]

    members_response = client.conversations_members(channel=PERSONAL_CHANNEL_ID)
    members = members_response["members"]

    if is_user_restricted(invoker_user_id):
        respond(
            f"sorry, but you are unable to join <#{PERSONAL_CHANNEL_ID}> ({channel_name}). :neocat_sad: if you think this is a mistake, please DM <@{CMAN_USER_ID}>."
        )
        client.chat_postMessage(
            channel=CMAN_USER_ID,
            text=f"<@{invoker_user_id}> tried to join your channel but you restricted them from joining your channel!",
        )
        return

    if invoker_user_id in members:
        respond(
            f"you are already in <#{PERSONAL_CHANNEL_ID}> ({channel_name}), you goober :neocat_blank:"
        )
        return

    if is_joining_paused():
        respond(
            "Joining is currently paused, please try again later when joining is unpaused. :neocat_sad:",
        )
        return

    if is_idv_required():
        response = requests.get(
            "https://auth.hackclub.com/api/external/check",
            params={"slack_id": invoker_user_id},
        )
        idv_data = response.json()
        idv_result = idv_data.get("result")
        if idv_result not in ("verified_eligible", "verified_but_over_18"):
            respond(
                f"sorry, but you need to be IDV verified to join <#{PERSONAL_CHANNEL_ID}>! ({channel_name}) :neocat_sad: complete your verifcation and try again later!",
            )

            client.chat_postMessage(
                channel=CMAN_USER_ID,
                text=f"<@{invoker_user_id}> tried to join your channel but they werent IDV verified!",
            )
            return

    if is_requests_off():
        client.conversations_invite(channel=PERSONAL_CHANNEL_ID, users=invoker_user_id)

        client.chat_postMessage(
            channel=invoker_user_id,
            text=f"you have been added to <#{PERSONAL_CHANNEL_ID}>! ({channel_name}) :yay: i've also added you to the user group that <@{CMAN_USER_ID}> configured!",
        )

        group_info = client.usergroups_users_list(usergroup=PERSONAL_USERGROUP_ID)
        current_users = group_info.get("users", [])

        if invoker_user_id not in current_users:
            current_users.append(invoker_user_id)
            client.usergroups_users_update(
                usergroup=PERSONAL_USERGROUP_ID, users=",".join(current_users)
            )

        return

    client.chat_postMessage(
        channel=invoker_user_id,
        text=f":<@{invoker_user_id}>. you requested access to join <@{CMAN_USER_ID}>'s channel! :yay: You should wait for a while for the channel owner to review your request to be invited!",
    )

    response = requests.get(
        "https://auth.hackclub.com/api/external/check",
        params={"slack_id": invoker_user_id},
    )
    idv_data = response.json()
    idv_result = idv_data.get("result")
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "plain_text",
                "text": f"New request to <@{PERSONAL_CHANNEL_ID}>'s personal channel :tm:",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":neocat: User: <@{invoker_user_id}>",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "plain_text",
                "text": f":identity-vault-transparent: IDV Status: {idv_result}",
                "emoji": True,
            },
        },
        {"type": "divider"},
        {
            "type": "context",
            "elements": [
                {
                    "type": "plain_text",
                    "text": "do you accept or deny? OwO",
                    "emoji": True,
                }
            ],
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Accept", "emoji": True},
                    "style": "primary",
                    "value": invoker_user_id,
                    "action_id": "accept_pc_action",
                }
            ],
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Deny", "emoji": True},
                    "style": "danger",
                    "value": invoker_user_id,
                    "action_id": "deny_pc_action",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Restrict", "emoji": True},
                    "style": "danger",
                    "value": invoker_user_id,
                    "action_id": "restrict_user_action",
                },
            ],
        },
    ]

    client.chat_postMessage(
        channel=CMAN_USER_ID,
        text="New PC Request",
        blocks=blocks,
    )


# app home
@app.event("app_home_opened")
def update_home_tab(client, event):
    user_id = event["user"]

    members_response = client.conversations_members(channel=PERSONAL_CHANNEL_ID)
    members = members_response["members"]

    is_member = user_id in members
    is_cm = user_id == CMAN_USER_ID
    is_restricted = is_user_restricted(user_id)

    if is_cm:
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"hi, <@{CMAN_USER_ID}>! :drgn_wave: what settings would you like to configure your nudgebot? ",
                },
            },
            {"type": "divider"},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": ":incoming_envelope: Invitation Settings",
                            "emoji": True,
                        },
                        "value": "click_me_123",
                        "action_id": "invitation_settings_action",
                    }
                ],
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": ":thread: Auto-Thread Settings",
                            "emoji": True,
                        },
                        "value": "click_me_123",
                        "action_id": "actionId-0",
                    }
                ],
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": ":clock4: Recap Settings",
                            "emoji": True,
                        },
                        "value": "click_me_123",
                        "action_id": "recap_config_action",
                    }
                ],
            },
        ]
    elif is_member:
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"you are already in <#{PERSONAL_CHANNEL_ID}> ({channel_name}), you goober! :neocat_happy:",
                },
            },
        ]
    elif is_restricted:
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"sorry, but you are unable to join <#{PERSONAL_CHANNEL_ID}>. ({channel_name}) :neocat_sad: if you think this is a mistake, please DM <@{CMAN_USER_ID}>.",
                },
            }
        ]
    else:
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"hey, it looks like you haven't joined <#{PERSONAL_CHANNEL_ID}> ({channel_name}), you wanna join that channel? :neocat_wink_blep:",
                },
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"this nudgebot and the personal channel (<#{PERSONAL_CHANNEL_ID}>) ({channel_name}) above is owned by <@{CMAN_USER_ID}>!",
                    }
                ],
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "Join Channel",
                            "emoji": True,
                        },
                        "value": "click_me_123",
                        "action_id": "join_pc_button_home",
                    }
                ],
            },
        ]

    client.views_publish(
        user_id=user_id,
        view={
            "type": "home",
            "blocks": blocks,
        },
    )


@app.action("actionId-0")
def configure_auto_thread(ack, client, body):
    ack()

    toggle_value = auto_thread_toggle()

    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "auto_thread_settings_action",
            "title": {
                "type": "plain_text",
                "text": "Auto-Thread Settings",
                "emoji": True,
            },
            "submit": {"type": "plain_text", "text": "Save", "emoji": True},
            "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "what would you like to configure for your Auto-Thread settings? :rac_woah:",
                    },
                },
                {"type": "divider"},
                {
                    "type": "input",
                    "block_id": "auto_thread_input",
                    "element": {
                        "type": "static_select",
                        "placeholder": {
                            "type": "plain_text",
                            "text": "Select...",
                            "emoji": True,
                        },
                        "options": [
                            {
                                "text": {
                                    "type": "plain_text",
                                    "text": "Enabled",
                                    "emoji": True,
                                },
                                "value": "1",
                            },
                            {
                                "text": {
                                    "type": "plain_text",
                                    "text": "Disabled",
                                    "emoji": True,
                                },
                                "value": "0",
                            },
                        ],
                        "initial_option": (
                            {
                                "text": {
                                    "type": "plain_text",
                                    "text": "Enabled",
                                    "emoji": True,
                                },
                                "value": "1",
                            }
                            if toggle_value
                            else {
                                "text": {
                                    "type": "plain_text",
                                    "text": "Disabled",
                                    "emoji": True,
                                },
                                "value": "0",
                            }
                        ),
                        "action_id": "auto_thread_select",
                    },
                    "label": {
                        "type": "plain_text",
                        "text": "Auto Threading",
                        "emoji": True,
                    },
                },
            ],
        },
    )


@app.view("auto_thread_settings_action")
def handle_auto_thread_submission(ack, body, client):
    ack()
    values = body["view"]["state"]["values"]

    auto_thread_value = values["auto_thread_input"]["auto_thread_select"][
        "selected_option"
    ]["value"]

    conn = sqlite3.connect("nudgebot.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO bot_settings (key, value) VALUES ('auto_thread_toggle', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = ?",
        (auto_thread_value, auto_thread_value),
    )
    conn.commit()
    conn.close()


@app.action("invitation_settings_action")
def configure_invitations(ack, client, body):
    ack()
    is_paused = is_joining_paused()
    idv_required = is_idv_required()
    requests_off = is_requests_off()

    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "invitation_settings_action",
            "title": {"type": "plain_text", "text": "Join Settings", "emoji": True},
            "submit": {"type": "plain_text", "text": "Save", "emoji": True},
            "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "what would you like to configure for your invitation settings? :rac_woah:",
                    },
                },
                {"type": "divider"},
                {
                    "type": "input",
                    "block_id": "pause_joining_input",
                    "element": {
                        "type": "static_select",
                        "placeholder": {
                            "type": "plain_text",
                            "text": "Select...",
                            "emoji": True,
                        },
                        "options": [
                            {
                                "text": {
                                    "type": "plain_text",
                                    "text": "Enabled (joining paused)",
                                    "emoji": True,
                                },
                                "value": "1",
                            },
                            {
                                "text": {
                                    "type": "plain_text",
                                    "text": "Disabled (joining allowed)",
                                    "emoji": True,
                                },
                                "value": "0",
                            },
                        ],
                        "initial_option": (
                            {
                                "text": {
                                    "type": "plain_text",
                                    "text": "Enabled (joining paused)",
                                    "emoji": True,
                                },
                                "value": "1",
                            }
                            if is_paused
                            else {
                                "text": {
                                    "type": "plain_text",
                                    "text": "Disabled (joining allowed)",
                                    "emoji": True,
                                },
                                "value": "0",
                            }
                        ),
                        "action_id": "pause_select",
                    },
                    "label": {
                        "type": "plain_text",
                        "text": "Pause Joining",
                        "emoji": True,
                    },
                },
                {
                    "type": "input",
                    "block_id": "idv_required_input",
                    "element": {
                        "type": "static_select",
                        "placeholder": {
                            "type": "plain_text",
                            "text": "Select...",
                            "emoji": True,
                        },
                        "options": [
                            {
                                "text": {
                                    "type": "plain_text",
                                    "text": "Required (block non-IDV)",
                                    "emoji": True,
                                },
                                "value": "1",
                            },
                            {
                                "text": {
                                    "type": "plain_text",
                                    "text": "Not Required (allow all)",
                                    "emoji": True,
                                },
                                "value": "0",
                            },
                        ],
                        "initial_option": (
                            {
                                "text": {
                                    "type": "plain_text",
                                    "text": "Required (block non-IDV)",
                                    "emoji": True,
                                },
                                "value": "1",
                            }
                            if idv_required
                            else {
                                "text": {
                                    "type": "plain_text",
                                    "text": "Not Required (allow all)",
                                    "emoji": True,
                                },
                                "value": "0",
                            }
                        ),
                        "action_id": "idv_select",
                    },
                    "label": {
                        "type": "plain_text",
                        "text": "Require IDV to Join",
                        "emoji": True,
                    },
                },
                {
                    "type": "input",
                    "element": {
                        "type": "static_select",
                        "placeholder": {
                            "type": "plain_text",
                            "text": "Select an item",
                            "emoji": True,
                        },
                        "options": [
                            {
                                "text": {
                                    "type": "plain_text",
                                    "text": "On",
                                    "emoji": True,
                                },
                                "value": "1",
                            },
                            {
                                "text": {
                                    "type": "plain_text",
                                    "text": "Off",
                                    "emoji": True,
                                },
                                "value": "0",
                            },
                        ],
                        "initial_option": (
                            {
                                "text": {
                                    "type": "plain_text",
                                    "text": "Off",
                                    "emoji": True,
                                },
                                "value": "0",
                            }
                            if requests_off
                            else {
                                "text": {
                                    "type": "plain_text",
                                    "text": "On",
                                    "emoji": True,
                                },
                                "value": "1",
                            }
                        ),
                        "action_id": "requests_select",
                    },
                    "label": {
                        "type": "plain_text",
                        "text": "Toggle Joining requests",
                        "emoji": True,
                    },
                    "block_id": "requests_value",
                    "optional": False,
                },
                {
                    "type": "alert",
                    "text": {
                        "type": "mrkdwn",
                        "text": "If you turn off joining requests, everybody that request access to your channel will automatically be added without approval!",
                        "verbatim": False,
                    },
                    "level": "warning",
                },
            ],
        },
    )


@app.view("invitation_settings_action")
def handle_join_settings_submission(ack, body, client):
    ack()
    values = body["view"]["state"]["values"]

    pause_value = values["pause_joining_input"]["pause_select"]["selected_option"][
        "value"
    ]
    idv_value = values["idv_required_input"]["idv_select"]["selected_option"]["value"]

    requests_value = values["requests_value"]["requests_select"]["selected_option"][
        "value"
    ]

    conn = sqlite3.connect("nudgebot.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO bot_settings (key, value) VALUES ('is_paused', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = ?",
        (pause_value, pause_value),
    )
    cursor.execute(
        "INSERT INTO bot_settings (key, value) VALUES ('require_idv', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = ?",
        (idv_value, idv_value),
    )
    cursor.execute(
        "INSERT INTO bot_settings (key, value) VALUES ('is_requests_off', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = ?",
        (requests_value, requests_value),
    )
    conn.commit()
    conn.close()


def toggle_joining_setting(key: str, ack, client, body):
    ack()
    conn = sqlite3.connect("nudgebot.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO bot_settings (key, value) VALUES (? ,'1') "
        "ON CONFLICT(key) DO UPDATE SET value = CASE when value = '1' THEN '0' ELSE '1' END",
        (key,),
    )
    conn.commit()
    conn.close()


@app.action("toggle_pause_action")
def handle_toggle_pause(ack, client, body):
    toggle_joining_setting("is_paused", ack, client, body)


@app.action("toggle_idv_action")
def handle_toggle_idv(ack, client, body):
    toggle_joining_setting("require_idv", ack, client, body)


# Configuring the recap:tm:
@app.action("recap_config_action")
def configure_recaps(ack, body, client):
    ack()

    user_id = body["user"]["id"]
    conn = sqlite3.connect("nudgebot.db")
    cursor = conn.cursor()
    cursor.execute(
        "SELECT recap_time, send_hackatime_stats, recaps_disabled FROM recap_settings WHERE user_id = ?",
        (user_id,),
    )
    result = cursor.fetchone()
    conn.close()

    initial_time = result[0] if result is not None else "21:00"
    send_hackatime_stats = result[1] if result is not None else 1
    recaps_disabled = result[2] if result is not None else 0
    initial_hackatime_option = {
        "text": {
            "type": "plain_text",
            "text": "Yes" if int(send_hackatime_stats) == 1 else "No",
            "emoji": True,
        },
        "value": "value-0" if int(send_hackatime_stats) == 1 else "value-1",
    }

    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "recap_config_modal",
            "title": {
                "type": "plain_text",
                "text": ":clock4: Recap Settings",
                "emoji": True,
            },
            "submit": {
                "type": "plain_text",
                "text": "Submit",
                "emoji": True,
            },
            "close": {
                "type": "plain_text",
                "text": "Cancel",
                "emoji": True,
            },
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "plain_text",
                        "text": "What settings would you like to change about your recaps?",
                        "emoji": True,
                    },
                },
                {"type": "divider"},
                {
                    "type": "input",
                    "block_id": "timepicker_block",
                    "element": {
                        "type": "timepicker",
                        "initial_time": initial_time,
                        "placeholder": {
                            "type": "plain_text",
                            "text": "Select time",
                            "emoji": True,
                        },
                        "action_id": "timepicker-action",
                    },
                    "label": {
                        "type": "plain_text",
                        "text": "Time when your daily recaps should be sent:",
                        "emoji": True,
                    },
                    "optional": False,
                },
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "plain_text",
                            "text": "By default, nudgebot sends recaps in Eastern Time (America/New_York). However, you can change this in your variables.",
                            "emoji": True,
                        }
                    ],
                },
                {"type": "divider"},
                {
                    "type": "input",
                    "block_id": "hackatime_block",
                    "element": {
                        "type": "static_select",
                        "action_id": "static_select-action",
                        "placeholder": {
                            "type": "plain_text",
                            "text": "Select an item",
                            "emoji": True,
                        },
                        "options": [
                            {
                                "text": {
                                    "type": "plain_text",
                                    "text": "Yes",
                                    "emoji": True,
                                },
                                "value": "value-0",
                            },
                            {
                                "text": {
                                    "type": "plain_text",
                                    "text": "No",
                                    "emoji": True,
                                },
                                "value": "value-1",
                            },
                        ],
                        "initial_option": initial_hackatime_option,
                    },
                    "label": {
                        "type": "plain_text",
                        "text": ":hackatime: Send Hackatime stats in your recap message?",
                        "emoji": True,
                    },
                    "optional": False,
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "Recaps Enabled"
                                if int(recaps_disabled) == 0
                                else "Recaps Disabled",
                                "emoji": True,
                            },
                            "value": "click_me_123",
                            "action_id": "toggle_recaps_action",
                            "style": "primary"
                            if int(recaps_disabled) == 0
                            else "danger",
                        }
                    ],
                },
            ],
        },
    )


# modal submission
@app.view("recap_config_modal")
def handle_recap_config_submission(ack, body, view, client):
    ack()
    user_id = body["user"]["id"]

    selected_time = view["state"]["values"]["timepicker_block"]["timepicker-action"][
        "selected_time"
    ]
    selected_stats = view["state"]["values"]["hackatime_block"]["static_select-action"][
        "selected_option"
    ]["value"]

    send_hackatime_stats = 1 if selected_stats == "value-0" else 0

    conn = sqlite3.connect("nudgebot.db")
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO recap_settings (user_id, recap_time, send_hackatime_stats)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
                   recap_time=excluded.recap_time,
                   send_hackatime_stats=excluded.send_hackatime_stats

               """,
        (user_id, selected_time, send_hackatime_stats),
    )
    conn.commit()
    conn.close()

    client.chat_postMessage(channel=user_id, text="Sucessfully changed settings!")


@app.action("toggle_recaps_action")
def handle_toggle_recaps(ack, body, client):
    ack()
    user_id = body["user"]["id"]

    conn = sqlite3.connect("nudgebot.db")
    cursor = conn.cursor()

    cursor.execute(
        "SELECT recaps_disabled FROM recap_settings WHERE user_id = ?",
        (user_id,),
    )
    result = cursor.fetchone()

    new_state = 1 if result is None or int(result[0]) == 0 else 0

    cursor.execute(
        """
        INSERT INTO recap_settings (user_id, recap_time, send_hackatime_stats, recaps_disabled)
        VALUES (?, '21:00', 1, ?)
        ON CONFLICT(user_id) DO UPDATE SET recaps_disabled = excluded.recaps_disabled
        """,
        (user_id, new_state),
    )
    conn.commit()
    conn.close()

    status_text = "enabled" if new_state == 0 else "disabled"
    client.chat_postMessage(channel=user_id, text=f"Recaps have been {status_text}!")


# App Home Logic! Part 2!
@app.action("join_pc_button_home")
# This is the same logic for joining_guardian :3
def handle_join_button_app_home(ack, respond, say, body, client):
    ack()

    user_id = body["user"]["id"]

    members_response = client.conversations_members(channel=PERSONAL_CHANNEL_ID)
    members = members_response["members"]

    if user_id in members:
        client.chat_postMessage(
            channel=user_id,
            text=f"you are already in <#{PERSONAL_CHANNEL_ID}> ({channel_name}), you goober :neocat_blank:",
        )
        return

    if is_user_restricted(user_id):
        client.chat_postMessage(
            channel=user_id,
            text=f"sorry, but you are unable to join <#{PERSONAL_CHANNEL_ID}> ({channel_name}). :neocat_sad: if you think this is a mistake, please DM <@{CMAN_USER_ID}>.",
        )
        return

    if is_joining_paused():
        client.chat_postMessage(
            channel=user_id,
            text="Joining is currently paused, please try again later when joining is unpaused. :neocat_sad:",
        )
        return

    if is_idv_required():
        response = requests.get(
            "https://auth.hackclub.com/api/external/check", params={"slack_id": user_id}
        )
        idv_data = response.json()
        idv_result = idv_data.get("result")
        if idv_result not in ("verified_eligible", "verified_but_over_18"):
            client.chat_postMessage(
                channel=user_id,
                text=f"sorry, but you need to be IDV verified to join <#{PERSONAL_CHANNEL_ID}>! ({channel_name}) :neocat_sad: complete your verifcation and try again later!",
            )

            client.chat_postMessage(
                channel=CMAN_USER_ID,
                text=f"<@{user_id}> tried to join your channel but they werent IDV verified!",
            )
            return

    if is_requests_off():
        client.conversations_invite(channel=PERSONAL_CHANNEL_ID, users=user_id)

        client.chat_postMessage(
            channel=user_id,
            text=f"you have been added to <#{PERSONAL_CHANNEL_ID}>! ({channel_name}) :yay:",
        )

        group_info = client.usergroups_users_list(usergroup=PERSONAL_USERGROUP_ID)
        current_users = group_info.get("users", [])

        if user_id not in current_users:
            current_users.append(user_id)
            client.usergroups_users_update(
                usergroup=PERSONAL_USERGROUP_ID, users=",".join(current_users)
            )
        return

    client.chat_postMessage(
        channel=user_id,
        text=f":<@{user_id}>. you requested access to join <@{CMAN_USER_ID}>'s channel! :yay: You should wait for a while for the channel owner to review your request to be invited!",
    )

    response = requests.get(
        "https://auth.hackclub.com/api/external/check", params={"slack_id": user_id}
    )
    idv_data = response.json()
    idv_result = idv_data.get("result")
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"New request to join {channel_name} :tm:",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":neocat: User: <@{user_id}>",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "plain_text",
                "text": f":identity-vault-transparent: IDV Status: {idv_result}",
                "emoji": True,
            },
        },
        {"type": "divider"},
        {
            "type": "context",
            "elements": [
                {
                    "type": "plain_text",
                    "text": "please select an option!",
                    "emoji": True,
                }
            ],
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Accept", "emoji": True},
                    "style": "primary",
                    "value": user_id,
                    "action_id": "accept_pc_action",
                }
            ],
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Deny", "emoji": True},
                    "style": "danger",
                    "value": user_id,
                    "action_id": "deny_pc_action",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Restrict", "emoji": True},
                    "style": "danger",
                    "value": user_id,
                    "action_id": "restrict_user_action",
                },
            ],
        },
    ]

    client.chat_postMessage(
        channel=CMAN_USER_ID,
        text="New Join Request",
        blocks=blocks,
    )


@app.action("accept_pc_action")
def handle_accept_button(ack, body, client):
    ack()

    requestor_user_id = body["actions"][0]["value"]

    client.chat_update(
        channel=body["channel"]["id"],
        ts=body["message"]["ts"],
        text=f"Accepted request for <@{requestor_user_id}>!",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"Accepted request for <@{requestor_user_id}>!",
                },
            }
        ],
    )

    client.conversations_invite(channel=PERSONAL_CHANNEL_ID, users=requestor_user_id)

    group_info = client.usergroups_users_list(usergroup=PERSONAL_USERGROUP_ID)
    current_users = group_info.get("users", [])

    if requestor_user_id not in current_users:
        current_users.append(requestor_user_id)
        client.usergroups_users_update(
            usergroup=PERSONAL_USERGROUP_ID, users=",".join(current_users)
        )

    client.chat_postMessage(
        channel=requestor_user_id,
        text=f"Yaay! Your request to join <@{CMAN_USER_ID}>'s personal channel has been accepted! Now have fun in the channel! :yay:",
    )


@app.action("restrict_user_action")
def handle_restrict_user_pc(ack, body, client):
    ack()

    restricted_user_id = body["actions"][0]["value"]

    add_user_to_restrictlist(
        restricted_user_id, reason="Channel Manager clicked Restrict"
    )

    client.chat_postMessage(
        channel=CMAN_USER_ID,
        text=f"Restricted <@{restricted_user_id}> sucessfully. They are not allowed to request to join <#{PERSONAL_CHANNEL_ID}> ({channel_name})",
    )


@app.action("deny_pc_action")
def handle_deny_button(ack, body, client, logger):
    ack()

    user_id = body["actions"][0]["value"]

    client.chat_postMessage(
        channel=user_id,
        text=f"hi <@{user_id}>. your request to <@{CMAN_USER_ID}>'s personal channel has been denied. if you think this is a mistake, please resend your request! :(",
    )


def schedule_recap_msg(client):

    if is_recaps_disabled():
        return

    while True:
        try:
            tz = zoneinfo.ZoneInfo(BOT_TIMEZONE or "America/New_York")
            now = datetime.now(tz)

            current_time_str = now.strftime("%H:%M")

            conn = sqlite3.connect("nudgebot.db")
            cursor = conn.cursor()
            cursor.execute(
                "SELECT user_id FROM recap_settings WHERE recap_time = ? AND recaps_disabled != 1",
                (current_time_str,),
            )
            rows = cursor.fetchall()
            conn.close()

            if rows:
                blocks = [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"<@{CMAN_USER_ID}>, it's {current_time_str} so it's time for your daily recap! :neocat_3c:",
                        },
                    },
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {
                                    "type": "plain_text",
                                    "text": "open form!",
                                    "emoji": True,
                                },
                                "value": "answer_recap",
                                "action_id": "open_recap_modal",
                            },
                            {
                                "type": "button",
                                "text": {
                                    "type": "plain_text",
                                    "text": "Dismiss reminder",
                                    "emoji": True,
                                },
                                "value": "ignore_recap",
                                "action_id": "ignore_recap_action",
                            },
                        ],
                    },
                ]
                client.chat_postMessage(
                    channel=PERSONAL_CHANNEL_ID, text="recap time!", blocks=blocks
                )

                time.sleep(61)
            else:
                time.sleep(30)
        except Exception as e:
            sentry_sdk.capture_exception(e)
            time.sleep(60)


# "Watching" feature
@app.event("subteam_members_changed")
def handle_usergroup_watch(event, client):
    print("subteam_members_changed triggered:", event)
    if event.get("subteam_id") != PERSONAL_USERGROUP_ID:
        return

    for user_id in event.get("added_users", []):
        client.chat_postMessage(
            channel=CMAN_USER_ID,
            text=f"<@{user_id}> just joined the usergroup! :yay-67:",
        )

    for user_id in event.get("removed_users", []):
        client.chat_postMessage(
            channel=CMAN_USER_ID,
            text=f"<@{user_id}> just left the usergroup! :saga:",
        )


# Ack
@app.action("feeling_select")
def listen_feeling(ack):
    ack()


if __name__ == "__main__":
    scheduler_thread = threading.Thread(
        target=schedule_recap_msg, args=(app.client,), daemon=True
    )
    scheduler_thread.start()

    purge_thread = threading.Thread(
        target=schedule_purge_msg, args=(app.client,), daemon=True
    )
    purge_thread.start()

    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()

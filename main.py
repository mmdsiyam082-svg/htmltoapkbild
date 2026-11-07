import os
import re
import io
import json
import time
import uuid
import shutil
import zipfile
import tempfile
import logging
import threading
import subprocess
from html import escape

import requests
import telebot
from telebot import types

from PIL import Image, ImageDraw, ImageFilter


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

API_URL = "https://app-creator-x.ueu.workers.dev/api/v1/build"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BANNER_PATH = os.path.join(BASE_DIR, "apk_converter_banner.png")

MAX_HTML_SIZE = 10 * 1024 * 1024
MAX_ZIP_SIZE = 10 * 1024 * 1024
MAX_ICON_SIZE = 6 * 1024 * 1024
MAX_ICON_PIXELS = 4_000_000

MAX_TOTAL_FILES = 16 * 1024 * 1024

MAX_APP_NAME_UTF16 = 80
MAX_APP_NAME_UTF8 = 120

MAX_PACKAGE_NAME = 100


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("apk_converter_bot")


if not BOT_TOKEN or ":" not in BOT_TOKEN:
    raise SystemExit(
        "Set a valid BOT_TOKEN environment variable before running."
    )


bot = telebot.TeleBot(
    BOT_TOKEN,
    parse_mode="HTML",
    threaded=True
)


# =========================================================
# SESSION STORAGE
# =========================================================

sessions = {}

# chat_id -> lock
session_locks = {}

# prevent duplicate processing
processed_messages = set()


def get_lock(chat_id):
    if chat_id not in session_locks:
        session_locks[chat_id] = threading.Lock()

    return session_locks[chat_id]


# =========================================================
# BANNER
# =========================================================

def ensure_banner():

    if os.path.exists(BANNER_PATH):
        return

    w, h = 1200, 675

    im = Image.new("RGB", (w, h), "#10162d")

    px = im.load()

    for y in range(h):
        for x in range(w):

            t = (x / w + y / h) / 2

            px[x, y] = (
                int(15 + 18 * t),
                int(22 + 26 * t),
                int(53 + 55 * t)
            )

    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))

    gd = ImageDraw.Draw(glow)

    gd.ellipse(
        (760, -160, 1320, 400),
        fill=(101, 88, 255, 95)
    )

    gd.ellipse(
        (-180, 410, 430, 1030),
        fill=(0, 207, 191, 55)
    )

    glow = glow.filter(
        ImageFilter.GaussianBlur(70)
    )

    im = Image.alpha_composite(
        im.convert("RGBA"),
        glow
    )

    d = ImageDraw.Draw(im)

    phone = (435, 85, 765, 590)

    d.rounded_rectangle(
        phone,
        radius=42,
        fill="#202a4b",
        outline="#6976b6",
        width=7
    )

    d.rounded_rectangle(
        (458, 132, 742, 535),
        radius=14,
        fill="#0c1329"
    )

    d.rounded_rectangle(
        (555, 101, 645, 119),
        radius=9,
        fill="#6976b6"
    )

    d.ellipse(
        (579, 551, 621, 593),
        fill="#6976b6"
    )

    d.rounded_rectangle(
        (535, 230, 665, 390),
        radius=22,
        fill="#665cff"
    )

    d.polygon(
        [(600, 198), (646, 240), (554, 240)],
        fill="#8d86ff"
    )

    d.line(
        (574, 219, 560, 200),
        fill="#8d86ff",
        width=8
    )

    d.line(
        (626, 219, 640, 200),
        fill="#8d86ff",
        width=8
    )

    d.ellipse(
        (568, 278, 584, 294),
        fill="white"
    )

    d.ellipse(
        (616, 278, 632, 294),
        fill="white"
    )

    d.rounded_rectangle(
        (570, 330, 630, 347),
        radius=8,
        fill="#b9b5ff"
    )

    im.convert("RGB").save(
        BANNER_PATH,
        "PNG",
        optimize=True
    )


# =========================================================
# KEYBOARDS
# =========================================================

def kb_main_menu():

    kb = types.InlineKeyboardMarkup(row_width=2)

    kb.add(
        types.InlineKeyboardButton(
            "🚀 Create APK",
            callback_data="menu:new"
        )
    )

    kb.add(
        types.InlineKeyboardButton(
            "📘 How it works",
            callback_data="menu:help"
        ),
        types.InlineKeyboardButton(
            "ℹ️ About",
            callback_data="menu:about"
        )
    )

    return kb


def kb_cancel():

    kb = types.InlineKeyboardMarkup()

    kb.add(
        types.InlineKeyboardButton(
            "✖️ Cancel",
            callback_data="flow:cancel"
        )
    )

    return kb


def kb_confirm():

    kb = types.InlineKeyboardMarkup(row_width=2)

    kb.add(
        types.InlineKeyboardButton(
            "✅ Build APK",
            callback_data="flow:build"
        ),
        types.InlineKeyboardButton(
            "✏️ Rename",
            callback_data="flow:rename"
        )
    )

    kb.add(
        types.InlineKeyboardButton(
            "✖️ Cancel",
            callback_data="flow:cancel"
        )
    )

    return kb


def kb_permission_info():

    kb = types.InlineKeyboardMarkup()

    kb.add(
        types.InlineKeyboardButton(
            "🔐 Permission Details",
            callback_data="permission:info"
        )
    )

    kb.add(
        types.InlineKeyboardButton(
            "✖️ Cancel",
            callback_data="flow:cancel"
        )
    )

    return kb


# =========================================================
# PROGRESS
# =========================================================

def progress(step, label):

    labels = [
        "App name",
        "Package name",
        "Icon",
        "Website",
        "Review",
        "Build"
    ]

    cells = []

    for i in range(len(labels)):

        if i < step:
            cells.append("✅")

        elif i == step:
            cells.append("🔵")

        else:
            cells.append("⚪")

    return (
        f"<b>Step {step + 1}/{len(labels)} — {label}</b>\n"
        f"{'  '.join(cells)}\n"
    )


# =========================================================
# SESSION
# =========================================================

def reset(chat_id):

    sessions.pop(chat_id, None)


def safe_name(value):

    return escape(value or "")


# =========================================================
# VALIDATE APP NAME
# =========================================================

def validate_app_name(value):

    value = (value or "").strip()

    if not value:
        return "Please send a non-empty app name."

    utf16_units = (
        len(value.encode("utf-16-le")) // 2
    )

    utf8_bytes = len(
        value.encode("utf-8")
    )

    if utf16_units > MAX_APP_NAME_UTF16:
        return (
            f"Name is too long "
            f"({utf16_units}/80 UTF-16 units)."
        )

    if utf8_bytes > MAX_APP_NAME_UTF8:
        return (
            f"Name is too large "
            f"({utf8_bytes}/120 UTF-8 bytes)."
        )

    if any(
        ord(char) < 32 or
        127 <= ord(char) <= 159
        for char in value
    ):
        return "The app name cannot contain control characters."

    return None


# =========================================================
# PACKAGE NAME
# =========================================================

def validate_package_name(value):

    value = (value or "").strip()

    if not value:
        return "Package name cannot be empty."

    if len(value) > MAX_PACKAGE_NAME:
        return "Package name is too long."

    # Android application ID style:
    # com.example.app
    pattern = (
        r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"
    )

    if not re.match(pattern, value):

        return (
            "Invalid package name.\n\n"
            "Example:\n"
            "<code>com.example.myapp</code>\n\n"
            "Use lowercase letters, numbers and underscores."
        )

    parts = value.split(".")

    if len(parts) < 2:
        return "Package name must contain at least two parts."

    return None


# =========================================================
# ICON VALIDATION
# =========================================================

def validate_icon(content):

    if not content:
        return "The icon file is empty."

    try:

        with Image.open(
            io.BytesIO(content)
        ) as image:

            if image.format not in (
                "PNG",
                "JPEG"
            ):
                return (
                    "Only PNG and JPEG icons are supported."
                )

            width, height = image.size

            if width * height > MAX_ICON_PIXELS:
                return (
                    "The icon is too large: "
                    "maximum is 4 megapixels."
                )

            if (
                image.format == "PNG"
                and image.info.get("interlace")
            ):
                return (
                    "Interlaced PNG icons are not supported."
                )

    except Exception:

        return (
            "That file is not a valid PNG or JPEG image."
        )

    return None


# =========================================================
# MESSAGE DEDUPLICATION
# =========================================================

def already_processed(message):

    key = (
        message.chat.id,
        message.message_id
    )

    if key in processed_messages:
        return True

    processed_messages.add(key)

    # Keep memory small
    if len(processed_messages) > 10000:
        processed_messages.clear()

    return False


# =========================================================
# SEND HOME
# =========================================================

def send_home(chat_id):

    text = (
        "<b>APK Converter Studio</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🌐 Website → 📱 Android APK\n\n"
        "Create a test APK from your HTML website.\n\n"
        "<b>Features</b>\n"
        "• App name\n"
        "• Custom package name\n"
        "• Custom icon\n"
        "• HTML / HTM / ZIP\n"
        "• Automatic permission detection\n"
        "• Minimal Android permissions\n"
        "• APK build\n\n"
        "<i>Fast • Clean • Test-build ready</i>"
    )

    if os.path.exists(BANNER_PATH):

        with open(
            BANNER_PATH,
            "rb"
        ) as photo:

            bot.send_photo(
                chat_id,
                photo,
                caption=text,
                reply_markup=kb_main_menu()
            )

    else:

        bot.send_message(
            chat_id,
            text,
            reply_markup=kb_main_menu()
        )


# =========================================================
# UI EDIT
# =========================================================

def edit_ui_message(
    message,
    text,
    markup=None
):

    if not message:
        return

    try:

        if message.content_type == "photo":

            bot.edit_message_caption(
                text,
                message.chat.id,
                message.message_id,
                reply_markup=markup
            )

        else:

            bot.edit_message_text(
                text,
                message.chat.id,
                message.message_id,
                reply_markup=markup
            )

    except Exception as exc:

        logger.debug(
            "UI update failed: %s",
            exc
        )


# =========================================================
# FLOW SCREEN
# =========================================================

def flow_screen(
    chat_id,
    text,
    markup=None,
    source=None
):

    session = sessions.get(chat_id)

    if not session:
        return None

    msg = source

    if msg is None:

        msg = bot.send_message(
            chat_id,
            text,
            reply_markup=markup
        )

    else:

        edit_ui_message(
            msg,
            text,
            markup
        )

    session["ui_message"] = msg

    return msg


# =========================================================
# NEXT STEP
# =========================================================

def flow_next(
    chat_id,
    text,
    handler,
    source=None
):

    return flow_screen(
        chat_id,
        text,
        kb_cancel(),
        source
    )


# =========================================================
# HELP
# =========================================================

HELP_TEXT = (
    "<b>📘 How it works</b>\n\n"
    "<b>1.</b> Enter App Name\n"
    "<b>2.</b> Enter Package Name\n"
    "<b>3.</b> Upload App Icon\n"
    "<b>4.</b> Upload HTML / HTM / ZIP\n"
    "<b>5.</b> Review permissions\n"
    "<b>6.</b> Build APK\n\n"
    "<b>Permission system:</b>\n"
    "The builder scans the website project for common "
    "Android capability requirements and keeps the APK "
    "permission set minimal.\n\n"
    "⚠️ The generated APK is a test build."
)


ABOUT_TEXT = (
    "<b>ℹ️ About</b>\n\n"
    "APK Converter Studio converts HTML website projects "
    "into Android test APK builds.\n\n"
    "<b>Input:</b> HTML, HTM, ZIP\n"
    "<b>Icon:</b> PNG / JPEG\n"
    "<b>Package:</b> Custom Android application ID\n"
    "<b>Permissions:</b> Minimal automatic configuration\n"
)


# =========================================================
# AUTOMATIC PERMISSION DETECTOR
# =========================================================

PERMISSION_RULES = {

    "INTERNET": {
        "permission": "android.permission.INTERNET",
        "patterns": [
            r"https?://",
            r"fetch\s*\(",
            r"axios",
            r"XMLHttpRequest",
            r"WebSocket",
            r"<iframe",
            r"<img[^>]+src\s*=\s*[\"']https?://",
            r"<script[^>]+src\s*=\s*[\"']https?://"
        ]
    },

    "CAMERA": {
        "permission": "android.permission.CAMERA",
        "patterns": [
            r"getUserMedia\s*\(",
            r"facingMode",
            r"environment"
        ]
    },

    "MICROPHONE": {
        "permission": "android.permission.RECORD_AUDIO",
        "patterns": [
            r"getUserMedia\s*\(",
            r"audio\s*:\s*true",
            r"MediaRecorder"
        ]
    },

    "LOCATION": {
        "permission": "android.permission.ACCESS_FINE_LOCATION",
        "patterns": [
            r"navigator\.geolocation",
            r"getCurrentPosition",
            r"watchPosition"
        ]
    },

    "NOTIFICATIONS": {
        "permission": "android.permission.POST_NOTIFICATIONS",
        "patterns": [
            r"Notification\s*\(",
            r"Notification\.requestPermission",
            r"showNotification"
        ]
    }
}


def detect_permissions(files_data):

    joined = ""

    for filename, content in files_data:

        try:

            if filename.lower().endswith(
                (".html", ".htm", ".js", ".css", ".json")
            ):

                joined += "\n"

                joined += content.decode(
                    "utf-8",
                    errors="ignore"
                )

        except Exception:
            pass

    permissions = set()

    reasons = {}

    for key, rule in PERMISSION_RULES.items():

        for pattern in rule["patterns"]:

            try:

                if re.search(
                    pattern,
                    joined,
                    re.IGNORECASE
                ):

                    permissions.add(
                        rule["permission"]
                    )

                    reasons.setdefault(
                        rule["permission"],
                        key
                    )

                    break

            except Exception:
                continue

    # A website loaded inside Android WebView normally
    # requires internet for external resources.
    if re.search(
        r"https?://|fetch\s*\(|axios|XMLHttpRequest",
        joined,
        re.IGNORECASE
    ):

        permissions.add(
            "android.permission.INTERNET"
        )

        reasons.setdefault(
            "android.permission.INTERNET",
            "NETWORK"
        )

    return sorted(permissions), reasons


# =========================================================
# PERMISSION DISPLAY
# =========================================================

def permission_text(permissions):

    if not permissions:

        return (
            "🔐 <b>Android Permissions</b>\n\n"
            "No additional dangerous Android permission "
            "was detected from the website source.\n\n"
            "The APK will use the smallest permission set "
            "possible."
        )

    lines = [
        "🔐 <b>Android Permissions</b>",
        "━━━━━━━━━━━━━━━━━━",
        ""
    ]

    friendly = {

        "android.permission.INTERNET":
            "🌐 Internet",

        "android.permission.CAMERA":
            "📷 Camera",

        "android.permission.RECORD_AUDIO":
            "🎙 Microphone",

        "android.permission.ACCESS_FINE_LOCATION":
            "📍 Location",

        "android.permission.POST_NOTIFICATIONS":
            "🔔 Notifications"
    }

    for permission in permissions:

        label = friendly.get(
            permission,
            permission
        )

        lines.append(
            f"• {label}"
        )

    lines.extend([
        "",
        "<i>Only detected capabilities are requested.</i>"
    ])

    return "\n".join(lines)


# =========================================================
# START FLOW
# =========================================================

def start_flow(
    chat_id,
    edit=None
):

    reset(chat_id)

    sessions[chat_id] = {
        "state": "app_name",
        "created": time.time()
    }

    text = (
        progress(0, "App name") +
        "\n"
        "📛 <b>Enter your App Name</b>\n\n"
        "Example:\n"
        "<code>My Website</code>"
    )

    if edit:

        flow_screen(
            chat_id,
            text,
            kb_cancel(),
            source=edit
        )

    else:

        bot.send_message(
            chat_id,
            text,
            reply_markup=kb_cancel()
        )


# =========================================================
# CALLBACKS
# =========================================================

@bot.callback_query_handler(
    func=lambda c: c.data == "menu:new"
)
def cb_new(call):

    bot.answer_callback_query(call.id)

    start_flow(
        call.message.chat.id,
        call.message
    )


@bot.callback_query_handler(
    func=lambda c: c.data == "menu:home"
)
def cb_home(call):

    bot.answer_callback_query(call.id)

    reset(
        call.message.chat.id
    )

    text = (
        "<b>APK Converter Studio</b>\n\n"
        "Choose an action below:"
    )

    edit_ui_message(
        call.message,
        text,
        kb_main_menu()
    )


@bot.callback_query_handler(
    func=lambda c: c.data == "menu:help"
)
def cb_help(call):

    bot.answer_callback_query(call.id)

    edit_ui_message(
        call.message,
        HELP_TEXT,
        kb_main_menu()
    )


@bot.callback_query_handler(
    func=lambda c: c.data == "menu:about"
)
def cb_about(call):

    bot.answer_callback_query(call.id)

    edit_ui_message(
        call.message,
        ABOUT_TEXT,
        kb_main_menu()
    )


@bot.callback_query_handler(
    func=lambda c: c.data == "permission:info"
)
def cb_permission_info(call):

    chat_id = call.message.chat.id

    session = sessions.get(chat_id)

    if not session:

        bot.answer_callback_query(
            call.id,
            "Session expired."
        )

        return

    bot.answer_callback_query(call.id)

    permissions = session.get(
        "permissions",
        []
    )

    text = permission_text(
        permissions
    )

    kb = types.InlineKeyboardMarkup()

    kb.add(
        types.InlineKeyboardButton(
            "⬅️ Back to Review",
            callback_data="flow:review"
        )
    )

    edit_ui_message(
        call.message,
        text,
        kb
    )


@bot.callback_query_handler(
    func=lambda c: c.data == "flow:review"
)
def cb_review(call):

    chat_id = call.message.chat.id

    if chat_id not in sessions:

        bot.answer_callback_query(
            call.id,
            "Session expired."
        )

        return

    bot.answer_callback_query(call.id)

    show_confirmation(
        chat_id,
        call.message
    )


@bot.callback_query_handler(
    func=lambda c: c.data == "flow:cancel"
)
def cb_cancel(call):

    chat_id = call.message.chat.id

    reset(chat_id)

    bot.answer_callback_query(
        call.id,
        "Cancelled"
    )

    edit_ui_message(
        call.message,
        "✖️ <b>Cancelled</b>\n\nChoose an action below:",
        kb_main_menu()
    )


@bot.callback_query_handler(
    func=lambda c: c.data == "flow:rename"
)
def cb_rename(call):

    chat_id = call.message.chat.id

    session = sessions.get(chat_id)

    if not session:

        bot.answer_callback_query(
            call.id,
            "Session expired."
        )

        return

    bot.answer_callback_query(call.id)

    session["state"] = "app_name"

    session["ui_message"] = call.message

    edit_ui_message(
        call.message,
        progress(0, "App name") +
        "\n📛 <b>Send the new App Name</b>",
        kb_cancel()
    )


@bot.callback_query_handler(
    func=lambda c: c.data == "flow:build"
)
def cb_build(call):

    chat_id = call.message.chat.id

    session = sessions.get(chat_id)

    if not session:

        bot.answer_callback_query(
            call.id,
            "Session expired."
        )

        return

    required = [
        "app_name",
        "package_name",
        "icon",
        "html"
    ]

    if not all(
        key in session
        for key in required
    ):

        bot.answer_callback_query(
            call.id,
            "Some information is missing."
        )

        return

    bot.answer_callback_query(
        call.id,
        "Build started"
    )

    session["state"] = "building"
    session["ui_message"] = call.message

    edit_ui_message(
        call.message,
        progress(5, "Build") +
        "\n⚙️ <b>Preparing APK build…</b>\n\n"
        "Please wait.",
        None
    )

    threading.Thread(
        target=build_apk,
        args=(chat_id,),
        daemon=True
    ).start()


# =========================================================
# MESSAGE ROUTER
# =========================================================

@bot.message_handler(
    commands=["start", "menu"]
)
def cmd_start(message):

    reset(
        message.chat.id
    )

    send_home(
        message.chat.id
    )


@bot.message_handler(
    commands=["help"]
)
def cmd_help(message):

    bot.send_message(
        message.chat.id,
        HELP_TEXT,
        reply_markup=kb_main_menu()
    )


@bot.message_handler(
    commands=["cancel"]
)
def cmd_cancel(message):

    reset(
        message.chat.id
    )

    bot.send_message(
        message.chat.id,
        "✖️ <b>Cancelled</b>",
        reply_markup=kb_main_menu()
    )


@bot.message_handler(
    commands=["newapk"]
)
def cmd_newapk(message):

    start_flow(
        message.chat.id
    )


# =========================================================
# APP NAME
# =========================================================

@bot.message_handler(
    func=lambda m:
        sessions.get(m.chat.id, {}).get("state")
        == "app_name",
    content_types=["text"]
)
def handle_app_name(message):

    if already_processed(message):
        return

    chat_id = message.chat.id

    name = (
        message.text or ""
    ).strip()

    error = validate_app_name(
        name
    )

    if error:

        bot.send_message(
            chat_id,
            progress(0, "App name") +
            "\n⚠️ <b>Invalid App Name</b>\n\n" +
            escape(error),
            reply_markup=kb_cancel()
        )

        return

    session = sessions[chat_id]

    session["app_name"] = name
    session["state"] = "package_name"

    bot.send_message(
        chat_id,
        progress(1, "Package name") +
        "\n📦 <b>Enter Package Name</b>\n\n"
        "Example:\n"
        "<code>com.example.myapp</code>\n\n"
        "Use lowercase letters, numbers and underscores.",
        reply_markup=kb_cancel()
    )


# =========================================================
# PACKAGE NAME
# =========================================================

@bot.message_handler(
    func=lambda m:
        sessions.get(m.chat.id, {}).get("state")
        == "package_name",
    content_types=["text"]
)
def handle_package_name(message):

    if already_processed(message):
        return

    chat_id = message.chat.id

    package_name = (
        message.text or ""
    ).strip()

    error = validate_package_name(
        package_name
    )

    if error:

        bot.send_message(
            chat_id,
            progress(1, "Package name") +
            "\n⚠️ <b>Invalid Package Name</b>\n\n" +
            escape(error),
            reply_markup=kb_cancel()
        )

        return

    session = sessions[chat_id]

    session["package_name"] = package_name
    session["state"] = "icon"

    bot.send_message(
        chat_id,
        progress(2, "Icon") +
        "\n🖼️ <b>Upload App Icon</b>\n\n"
        "Send PNG or JPEG.\n"
        "Maximum 6 MiB / 4 megapixels.",
        reply_markup=kb_cancel()
    )


# =========================================================
# ICON
# =========================================================

@bot.message_handler(
    func=lambda m:
        sessions.get(m.chat.id, {}).get("state")
        == "icon",
    content_types=["document", "photo"]
)
def handle_icon(message):

    if already_processed(message):
        return

    chat_id = message.chat.id

    file_id = None
    filename = "icon.png"

    if message.document:

        doc = message.document

        filename = (
            doc.file_name
            or "icon.png"
        )

        ext = (
            filename.rsplit(".", 1)[-1].lower()
            if "." in filename
            else ""
        )

        if ext not in (
            "png",
            "jpg",
            "jpeg"
        ):

            bot.send_message(
                chat_id,
                "⚠️ Please send PNG or JPEG.",
                reply_markup=kb_cancel()
            )

            return

        if (
            doc.file_size
            and doc.file_size > MAX_ICON_SIZE
        ):

            bot.send_message(
                chat_id,
                "⚠️ Icon is larger than 6 MiB.",
                reply_markup=kb_cancel()
            )

            return

        file_id = doc.file_id

    elif message.photo:

        file_id = (
            message.photo[-1].file_id
        )

        filename = "icon.jpg"

    if not file_id:

        bot.send_message(
            chat_id,
            "⚠️ Please upload an icon.",
            reply_markup=kb_cancel()
        )

        return

    try:

        info = bot.get_file(
            file_id
        )

        content = bot.download_file(
            info.file_path
        )

    except Exception as exc:

        logger.exception(
            "Icon download failed"
        )

        bot.send_message(
            chat_id,
            "❌ Icon download failed.\n\n"
            + escape(str(exc)),
            reply_markup=kb_cancel()
        )

        return

    error = validate_icon(
        content
    )

    if error:

        bot.send_message(
            chat_id,
            "⚠️ <b>Invalid Icon</b>\n\n"
            + escape(error),
            reply_markup=kb_cancel()
        )

        return

    session = sessions[chat_id]

    session["icon"] = (
        filename,
        content
    )

    session["state"] = "website"

    bot.send_message(
        chat_id,
        progress(3, "Website") +
        "\n📄 <b>Upload Website File</b>\n\n"
        "Send:\n"
        "• .html\n"
        "• .htm\n"
        "• .zip\n\n"
        "ZIP should contain index.html.",
        reply_markup=kb_cancel()
    )


# =========================================================
# WEBSITE
# =========================================================

@bot.message_handler(
    func=lambda m:
        sessions.get(m.chat.id, {}).get("state")
        == "website",
    content_types=["document"]
)
def handle_website(message):

    if already_processed(message):
        return

    chat_id = message.chat.id

    doc = message.document

    filename = (
        doc.file_name
        or "index.html"
    )

    ext = (
        filename.rsplit(".", 1)[-1].lower()
        if "." in filename
        else ""
    )

    if ext not in (
        "html",
        "htm",
        "zip"
    ):

        bot.send_message(
            chat_id,
            "⚠️ <b>Unsupported File</b>\n\n"
            "Only HTML, HTM or ZIP is allowed.",
            reply_markup=kb_cancel()
        )

        return

    limit = (
        MAX_ZIP_SIZE
        if ext == "zip"
        else MAX_HTML_SIZE
    )

    if (
        doc.file_size
        and doc.file_size > limit
    ):

        bot.send_message(
            chat_id,
            f"⚠️ File is too large.\n\n"
            f"Maximum: {limit:,} bytes.",
            reply_markup=kb_cancel()
        )

        return

    try:

        info = bot.get_file(
            doc.file_id
        )

        content = bot.download_file(
            info.file_path
        )

    except Exception as exc:

        logger.exception(
            "Website download failed"
        )

        bot.send_message(
            chat_id,
            "❌ Download failed.\n\n"
            + escape(str(exc)),
            reply_markup=kb_cancel()
        )

        return

    if (
        len(content)
        > limit
    ):

        bot.send_message(
            chat_id,
            "⚠️ Downloaded file is too large.",
            reply_markup=kb_cancel()
        )

        return

    session = sessions[chat_id]

    total = (
        len(content)
        +
        len(session["icon"][1])
    )

    if total > MAX_TOTAL_FILES:

        reset(chat_id)

        bot.send_message(
            chat_id,
            "⚠️ <b>Files are too large together.</b>\n\n"
            "Please start again with smaller files.",
            reply_markup=kb_main_menu()
        )

        return

    # -----------------------------------------
    # SAVE WEBSITE
    # -----------------------------------------

    session["html"] = (
        filename,
        content
    )

    # -----------------------------------------
    # DETECT PERMISSIONS
    # -----------------------------------------

    scan_files = [
        (
            filename,
            content
        )
    ]

    if ext == "zip":

        scan_files = extract_files_for_scan(
            content
        )

    permissions, reasons = detect_permissions(
        scan_files
    )

    session["permissions"] = permissions
    session["permission_reasons"] = reasons

    session["state"] = "review"

    show_confirmation(
        chat_id
    )


# =========================================================
# ZIP SCAN
# =========================================================

def extract_files_for_scan(content):

    result = []

    try:

        with zipfile.ZipFile(
            io.BytesIO(content),
            "r"
        ) as z:

            for info in z.infolist():

                if info.is_dir():
                    continue

                name = info.filename

                lower = name.lower()

                if not lower.endswith(
                    (
                        ".html",
                        ".htm",
                        ".js",
                        ".json",
                        ".css"
                    )
                ):
                    continue

                if info.file_size > 2 * 1024 * 1024:
                    continue

                try:

                    data = z.read(
                        info
                    )

                    result.append(
                        (
                            name,
                            data
                        )
                    )

                except Exception:
                    continue

    except Exception:

        pass

    return result


# =========================================================
# REVIEW
# =========================================================

def show_confirmation(
    chat_id,
    source=None
):

    session = sessions.get(
        chat_id
    )

    if not session:
        return

    html_name, html_bytes = (
        session["html"]
    )

    icon_name, icon_bytes = (
        session["icon"]
    )

    permissions = session.get(
        "permissions",
        []
    )

    if permissions:

        permission_summary = "\n".join(
            [
                f"• {escape(p)}"
                for p in permissions
            ]
        )

    else:

        permission_summary = (
            "• No additional permission detected"
        )

    text = (
        progress(4, "Review") +
        "\n"
        "<b>📋 APK BUILD REVIEW</b>\n\n"

        f"📛 <b>App:</b> "
        f"{safe_name(session['app_name'])}\n"

        f"📦 <b>Package:</b> "
        f"<code>{escape(session['package_name'])}</code>\n"

        f"🖼️ <b>Icon:</b> "
        f"<code>{escape(icon_name)}</code>\n"

        f"📄 <b>Website:</b> "
        f"<code>{escape(html_name)}</code>\n\n"

        "<b>🔐 Permissions</b>\n"
        f"{permission_summary}\n\n"

        "<i>Only detected permissions will be included "
        "in the build configuration.</i>\n\n"

        "Everything looks ready. Build APK?"
    )

    kb = types.InlineKeyboardMarkup(
        row_width=2
    )

    kb.add(
        types.InlineKeyboardButton(
            "🔐 Permissions",
            callback_data="permission:info"
        )
    )

    kb.add(
        types.InlineKeyboardButton(
            "✏️ Rename",
            callback_data="flow:rename"
        ),
        types.InlineKeyboardButton(
            "✏️ Package",
            callback_data="flow:package"
        )
    )

    kb.add(
        types.InlineKeyboardButton(
            "✅ Build APK",
            callback_data="flow:build"
        )
    )

    kb.add(
        types.InlineKeyboardButton(
            "✖️ Cancel",
            callback_data="flow:cancel"
        )
    )

    if source:

        edit_ui_message(
            source,
            text,
            kb
        )

    else:

        bot.send_message(
            chat_id,
            text,
            reply_markup=kb
        )


# =========================================================
# PACKAGE EDIT
# =========================================================

@bot.callback_query_handler(
    func=lambda c: c.data == "flow:package"
)
def cb_package(call):

    chat_id = call.message.chat.id

    if chat_id not in sessions:
        bot.answer_callback_query(
            call.id,
            "Session expired."
        )
        return

    bot.answer_callback_query(call.id)

    sessions[chat_id]["state"] = "package_name"

    sessions[chat_id]["ui_message"] = call.message

    edit_ui_message(
        call.message,
        progress(1, "Package name") +
        "\n📦 <b>Send new Package Name</b>\n\n"
        "Example:\n"
        "<code>com.example.myapp</code>",
        kb_cancel()
    )


# =========================================================
# BUILD ANIMATION
# =========================================================

def animate_build(
    message,
    stop_event
):

    if not message:
        return

    frames = [
        "◐",
        "◓",
        "◑",
        "◒"
    ]

    stages = [
        (
            "Checking project",
            20
        ),
        (
            "Analyzing permissions",
            35
        ),
        (
            "Preparing Android WebView",
            50
        ),
        (
            "Compiling APK",
            72
        ),
        (
            "Applying package name",
            84
        ),
        (
            "Signing test APK",
            94
        ),
        (
            "Finalizing APK",
            98
        )
    ]

    i = 0

    while not stop_event.is_set():

        label, percent = (
            stages[i % len(stages)]
        )

        filled = max(
            1,
            percent // 10
        )

        bar = (
            "🟦" * filled
            +
            "▫️" * (10 - filled)
        )

        text = (
            f"<b>{frames[i % len(frames)]} "
            f"APK Converter Studio</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"⚙️ <b>{label}</b>\n"
            f"{bar} <b>{percent}%</b>\n\n"
            "☕ Please wait…"
        )

        edit_ui_message(
            message,
            text
        )

        i += 1

        stop_event.wait(
            1.5
        )


# =========================================================
# API ERROR
# =========================================================

def api_error_message(resp):

    try:

        payload = resp.json()

        detail = payload.get(
            "error",
            resp.text
        )

    except Exception:

        detail = (
            resp.text
            or f"HTTP {resp.status_code}"
        )

    status_help = {

        400:
            "Please check the app name, package name and uploaded files.",

        413:
            "The complete request is too large.",

        415:
            "The upload format was rejected.",

        429:
            "The build service is rate-limited.",

        500:
            "The build service failed unexpectedly."
    }.get(
        resp.status_code,
        "Please try again later."
    )

    return (
        f"{escape(str(detail))}\n\n"
        f"<i>{status_help}</i>"
    )


# =========================================================
# BUILD
# =========================================================

def build_apk(chat_id):

    session = sessions.get(
        chat_id
    )

    if not session:
        return

    ui_message = session.get(
        "ui_message"
    )

    stop_event = threading.Event()

    animation_thread = None

    if ui_message:

        animation_thread = threading.Thread(
            target=animate_build,
            args=(
                ui_message,
                stop_event
            ),
            daemon=True
        )

        animation_thread.start()

    try:

        app_name = session[
            "app_name"
        ]

        package_name = session[
            "package_name"
        ]

        html_name, html_bytes = session[
            "html"
        ]

        icon_name, icon_bytes = session[
            "icon"
        ]

        permissions = session.get(
            "permissions",
            []
        )

        html_mime = (
            "application/zip"
            if html_name.lower().endswith(".zip")
            else "text/html"
        )

        icon_mime = (
            "image/png"
            if icon_name.lower().endswith(".png")
            else "image/jpeg"
        )

        # -------------------------------------------------
        # IMPORTANT
        # -------------------------------------------------
        # These fields are sent to the build API.
        # If the API supports them, it can directly apply
        # package name + permission configuration.
        # -------------------------------------------------

        permission_json = json.dumps(
            permissions
        )

        data = {

            "app_name":
                app_name,

            "package_name":
                package_name,

            "application_id":
                package_name,

            "permissions":
                permission_json,

            "auto_permissions":
                "true"
        }

        files = {

            "html": (
                html_name,
                html_bytes,
                html_mime
            ),

            "icon": (
                icon_name,
                icon_bytes,
                icon_mime
            )
        }

        logger.info(
            "Starting APK build | user=%s | package=%s | permissions=%s",
            chat_id,
            package_name,
            permissions
        )

        resp = requests.post(
            API_URL,
            data=data,
            files=files,
            timeout=180
        )

        stop_event.set()

        if animation_thread:

            animation_thread.join(
                timeout=2
            )

        content_type = (
            resp.headers.get(
                "Content-Type",
                ""
            )
        )

        # =================================================
        # SUCCESS
        # =================================================

        if (
            resp.status_code == 200
            and
            (
                "application/vnd.android.package-archive"
                in content_type
                or
                resp.content[:4] == b"PK\x03\x04"
            )
        ):

            edit_ui_message(
                ui_message,
                "<b>✅ APK BUILD COMPLETE</b>\n\n"
                "📦 Your APK is ready.\n"
                "Sending the file now…"
            )

            disposition = (
                resp.headers.get(
                    "Content-Disposition",
                    ""
                )
            )

            match = re.search(
                r'filename=(?:"([^"]+)"|([^;]+))',
                disposition
            )

            if match:

                apk_name = (
                    match.group(1)
                    or
                    match.group(2)
                ).strip()

            else:

                apk_name = (
                    f"{app_name}.apk"
                )

            if not apk_name.lower().endswith(
                ".apk"
            ):

                apk_name += ".apk"

            caption = (
                "🎉 <b>APK Build Complete</b>\n\n"

                f"📛 <b>App:</b> "
                f"{safe_name(app_name)}\n"

                f"📦 <b>Package:</b> "
                f"<code>{escape(package_name)}</code>\n\n"

                "🔐 <b>Permissions:</b> "
                f"{len(permissions)}\n\n"

                f"📱 <code>{escape(apk_name)}</code>\n\n"

                "⚠️ Test build only."
            )

            bot.send_document(
                chat_id,
                (
                    apk_name,
                    resp.content
                ),
                caption=caption,
                reply_markup=kb_main_menu()
            )

        else:

            error_text = (
                api_error_message(
                    resp
                )
            )

            if ui_message:

                edit_ui_message(
                    ui_message,
                    (
                        f"❌ <b>BUILD FAILED</b> "
                        f"({resp.status_code})\n\n"
                        f"{error_text}"
                    ),
                    kb_main_menu()
                )

            else:

                bot.send_message(
                    chat_id,
                    (
                        f"❌ <b>BUILD FAILED</b>\n\n"
                        f"{error_text}"
                    ),
                    reply_markup=kb_main_menu()
                )

    except requests.RequestException as exc:

        logger.exception(
            "Build API request failed"
        )

        stop_event.set()

        if animation_thread:

            animation_thread.join(
                timeout=2
            )

        text = (
            "❌ <b>BUILD SERVICE UNAVAILABLE</b>\n\n"
            f"{escape(str(exc))}"
        )

        if ui_message:

            edit_ui_message(
                ui_message,
                text,
                kb_main_menu()
            )

        else:

            bot.send_message(
                chat_id,
                text,
                reply_markup=kb_main_menu()
            )

    except Exception as exc:

        logger.exception(
            "Unexpected build error"
        )

        stop_event.set()

        if animation_thread:

            animation_thread.join(
                timeout=2
            )

        text = (
            "❌ <b>Unexpected Build Error</b>\n\n"
            f"{escape(str(exc))}"
        )

        if ui_message:

            edit_ui_message(
                ui_message,
                text,
                kb_main_menu()
            )

        else:

            bot.send_message(
                chat_id,
                text,
                reply_markup=kb_main_menu()
            )

    finally:

        stop_event.set()

        reset(chat_id)


# =========================================================
# FALLBACK
# =========================================================

@bot.message_handler(
    func=lambda m: True
)
def fallback(message):

    # IMPORTANT:
    # If a session is active, do NOT send another reply.
    # The state handlers above own the message.
    if message.chat.id in sessions:

        return

    bot.send_message(
        message.chat.id,
        "Choose an option below:",
        reply_markup=kb_main_menu()
    )


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":

    ensure_banner()

    logger.info(
        "APK Converter Studio starting..."
    )

    bot.infinity_polling(
        skip_pending=True,
        timeout=30,
        long_polling_timeout=30
    )
import os
import re
import json
import time
import mimetypes
import tempfile
import threading
import hashlib
import hmac
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

import requests
from PIL import Image, ImageOps
from html import unescape
from flask import Flask, request, Response

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]  # Apps Script URL
PORT = int(os.environ.get("PORT", "10000"))

VK_APP_ID = os.environ.get("VK_APP_ID", "")
VK_APP_SECRET = os.environ.get("VK_APP_SECRET", "")
VK_REDIRECT_URI = os.environ.get("VK_REDIRECT_URI", "")
VK_AUTH_SECRET = os.environ.get("VK_AUTH_SECRET", "")

ALBUM_DELAY_SEC = float(os.environ.get("ALBUM_DELAY_SEC", "3"))
APPEND_SOURCE_LINK = os.environ.get("APPEND_SOURCE_LINK", "1") == "1"

# ======================
# EDITORIAL CMS SAFE CONFIG
# ======================
# Additive configuration. Existing channel_post -> Sheets -> VK flow remains unchanged.
PREDLOZHKA_CAPTURE_ENABLED = os.environ.get("PREDLOZHKA_CAPTURE_ENABLED", "0") == "1"
PREDLOZHKA_SHEET_NAME = os.environ.get("PREDLOZHKA_SHEET_NAME", "Предложка")
PUBLISHED_SHEET_NAME = os.environ.get("PUBLISHED_SHEET_NAME", "Таблица контента редакции")

# SAFETY FILTER:
# Only messages from the real Predlozhka supergroup are written to the Predlozhka sheet.
# This prevents the bot from capturing other editorial chats/statistics chats.
PREDLOZHKA_ALLOWED_CHAT_IDS_RAW = os.environ.get(
    "PREDLOZHKA_ALLOWED_CHAT_IDS",
    "-1003533638771"
)

PREDLOZHKA_ALLOWED_CHAT_IDS = set()
for _chat_id in PREDLOZHKA_ALLOWED_CHAT_IDS_RAW.split(","):
    _chat_id = _chat_id.strip()
    if _chat_id:
        PREDLOZHKA_ALLOWED_CHAT_IDS.add(_chat_id)

print("PREDLOZHKA_ALLOWED_CHAT_IDS:", sorted(PREDLOZHKA_ALLOWED_CHAT_IDS), flush=True)

# Review engine is disabled by default for safe rollout.
# Enable after Apps Script is updated: REVIEW_ENGINE_ENABLED=1
REVIEW_ENGINE_ENABLED = os.environ.get("REVIEW_ENGINE_ENABLED", "0") == "1"
REVIEW_CHECK_INTERVAL_SEC = int(os.environ.get("REVIEW_CHECK_INTERVAL_SEC", "3600"))

# Digest engine uses the same scheduler. Enabled by default when REVIEW_ENGINE_ENABLED=1.
DIGEST_ENGINE_ENABLED = os.environ.get(
    "DIGEST_ENGINE_ENABLED",
    "1" if REVIEW_ENGINE_ENABLED else "0"
) == "1"
DIGEST_SIZE = int(os.environ.get("DIGEST_SIZE", "5"))


# ======================
# AUTOPUBLISH PREVIEW — SAFE MVP
# ======================
# Disabled by default. Enable on Render only after adding the frame files to the repo.
AUTOPUBLISH_PREVIEW_ENABLED = os.environ.get("AUTOPUBLISH_PREVIEW_ENABLED", "0") == "1"
AUTOPUBLISH_TIMEZONE = os.environ.get("AUTOPUBLISH_TIMEZONE", "Europe/Moscow")
AUTOPUBLISH_CHECK_INTERVAL_SEC = int(os.environ.get("AUTOPUBLISH_CHECK_INTERVAL_SEC", "20"))
AUTOPUBLISH_STORE_PATH = os.environ.get("AUTOPUBLISH_STORE_PATH", "scheduled_posts.json")  # legacy fallback; Sheets is used for new schedules
GENRE_FRAME_DIR = os.environ.get("GENRE_FRAME_DIR", "genre_frames")

# Telegram native copy_text is limited to 256 characters. Long editorial reviews
# use a signed copy page hosted by this same Render web service.
EDITORIAL_COPY_BASE_URL = (
    os.environ.get("EDITORIAL_COPY_BASE_URL")
    or os.environ.get("RENDER_EXTERNAL_URL")
    or ""
).rstrip("/")
EDITORIAL_COPY_LINK_TTL_SEC = int(os.environ.get("EDITORIAL_COPY_LINK_TTL_SEC", str(2 * 24 * 60 * 60)))

# Channel publishing is intentionally paused. The bot only prepares editorial drafts.
AUTOPUBLISH_CHANNEL_ENABLED = os.environ.get("AUTOPUBLISH_CHANNEL_ENABLED", "0") == "1"

# Trusted external bot that sends reviews into Predlozhka topics.
AUTOPUBLISH_TRUSTED_BOT_IDS_RAW = os.environ.get(
    "AUTOPUBLISH_TRUSTED_BOT_IDS",
    "7287549819",
)
AUTOPUBLISH_TRUSTED_BOT_IDS = set()
for _bot_id in AUTOPUBLISH_TRUSTED_BOT_IDS_RAW.split(","):
    _bot_id = _bot_id.strip()
    if _bot_id:
        AUTOPUBLISH_TRUSTED_BOT_IDS.add(_bot_id)

print(
    "EDITORIAL DRAFT CONFIG:",
    {
        "preview_enabled": AUTOPUBLISH_PREVIEW_ENABLED,
        "channel_publish_enabled": AUTOPUBLISH_CHANNEL_ENABLED,
        "trusted_bot_ids": sorted(AUTOPUBLISH_TRUSTED_BOT_IDS),
    },
    flush=True,
)

# V12: exact review/cover pairing; first-line quote; copy button removed.
# Keep the latest parts briefly so the editorial draft is assembled from both.
EDITORIAL_DRAFT_PARTS_TTL_SEC = int(os.environ.get("EDITORIAL_DRAFT_PARTS_TTL_SEC", "1800"))

# Bot service messages are removed shortly before Telegram's 48-hour deletion limit.
BOT_MESSAGE_CLEANUP_ENABLED = os.environ.get("BOT_MESSAGE_CLEANUP_ENABLED", "1") == "1"
BOT_MESSAGE_CLEANUP_AFTER_MINUTES = int(os.environ.get("BOT_MESSAGE_CLEANUP_AFTER_MINUTES", str(47 * 60 + 40)))
BOT_MESSAGE_CLEANUP_CHECK_SEC = int(os.environ.get("BOT_MESSAGE_CLEANUP_CHECK_SEC", "300"))

TARGET_CHANNELS = {
    "rock": "@chto_music_rock",
    "indie": "@chto_music_indie",
    "folk": "@chto_music_folk",
    "pop": "@chto_music_pop",
    "hiphop": "@chto_music_hiphop",
    "electronica": "@chto_music_electronica",
    "glavred": "@chto_music",
}

GENRE_FRAME_FILES = {
    "rock": "rock_frame.png",
    "indie": "indie_frame.png",
    "folk": "folk_frame.png",
    "pop": "pop_frame.png",
    "hiphop": "hiphop_frame.png",
    "glavred": "main_frame.png",
    # electronica intentionally has no frame
}

# Unique channel-disc custom emoji. They are used in editorial drafts before
# “Artist — Track”. All have 💿 as a safe fallback glyph.
GENRE_EMOJI_MAP = {
    "glavred": {"emoji_id": "5332472742916678524", "visible": "💿"},
    "rock": {"emoji_id": "5330416810791559528", "visible": "💿"},
    "pop": {"emoji_id": "5332614176189732263", "visible": "💿"},
    "folk": {"emoji_id": "5330462307380126280", "visible": "💿"},
    "indie": {"emoji_id": "5330524983837875288", "visible": "💿"},
    "hiphop": {"emoji_id": "5330100967486546882", "visible": "💿"},
    "electronica": {"emoji_id": "5332493801141328118", "visible": "💿"},
}

# Editorial section marker before the literal line “Что понравилось?”.
LIKED_SECTION_EMOJI = {
    "emoji_id": "5379970773358227593",
    "visible": "✨",
}

AUTHOR_EMOJI_MAP = {
    "Алексей Журавлев": {
        "emoji_id": "5429396071589642872",
        "visible": "😎",
    },
    "Журавлев Алексей": {
        "emoji_id": "5429396071589642872",
        "visible": "😎",
    },
}

# Custom emoji pack used by the editorial team.
# Telegram link: https://t.me/addemoji/ChtoMusicTeam
CUSTOM_EMOJI_PACK_NAME = os.environ.get("CUSTOM_EMOJI_PACK_NAME", "ChtoMusicTeam").strip() or "ChtoMusicTeam"

# In-memory state for the short dialogue after pressing “Опубликовать позже”.
# Scheduled jobs themselves are also saved to AUTOPUBLISH_STORE_PATH.
awaiting_schedule = {}
scheduled_posts_lock = threading.Lock()
autopublish_scheduler_started = False
autopublish_scheduler_lock = threading.Lock()
preview_dedup = {}
editorial_draft_parts = {}
editorial_draft_parts_lock = threading.Lock()
cleanup_scheduler_started = False
cleanup_scheduler_lock = threading.Lock()

# ======================
# PREDLOZHKA THREAD MAP
# ======================
# Default production map collected from Render logs.
# Can still be overridden/extended from Render env THREAD_MAP_JSON if needed.
DEFAULT_THREAD_MAP = {
    645: "rock",
    641: "indie",
    649: "electronica",
    647: "folk",
    643: "pop",
    653: "underground",
    651: "hiphop",
    3355: "glavred",
}

# Optional override/extension in Render env, format:
# {"645":"rock","641":"indie","649":"electronica"}
THREAD_MAP_JSON = os.environ.get("THREAD_MAP_JSON", "")

THREAD_MAP = DEFAULT_THREAD_MAP.copy()

if THREAD_MAP_JSON:
    try:
        THREAD_MAP.update({int(k): str(v) for k, v in json.loads(THREAD_MAP_JSON).items()})
    except Exception as e:
        print("THREAD_MAP_JSON ERROR:", str(e), flush=True)

print("THREAD_MAP LOADED:", THREAD_MAP, flush=True)

COMMON_LINK_DOMAINS = (
    "band.link",
    "bnd.lc",
    "bfan.link",
    "bff.link",
    "lnk.to",
    "linkfire",
    "ffm.to",
    "song.link",
    "songwhip",
    "onerpm.link",
    "hyperfollow",
    "taplink",
    "mssg.me",
    "clck.ru",
    "vk.cc",
    "zvonko.link",
)


CHANNEL_CURATORS = {
    "chto_music": "Павел Кофф",
    "chto_music_podval": "Цур",
    "chto_music_folk": "Анастасия Викто",
    "chto_music_pop": "Daniel",
    "chto_music_hiphop": "Цур",
    "chto_music_rock": "Андрей Копанев",
    "chto_music_electronica": "Баканов Дмитрий",
    "chto_music_indie": "Вера Чистякова",
}

MONTH_NAMES_RU = [
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь",
]

HTTP_TIMEOUT = 30
DOWNLOAD_TIMEOUT = 120
UPLOAD_TIMEOUT = 120

app = Flask(__name__)

album_lock = threading.Lock()
album_buffer: Dict[str, dict] = {}
processed_media_groups: Dict[str, float] = {}
processed_single_messages: Dict[int, float] = {}

mapping_cache = {}
mapping_cache_time = 0
MAPPING_CACHE_TTL = 60

PROCESSED_TTL_SEC = 6 * 60 * 60


def now_ts() -> float:
    return time.time()


def normalize_channel_username(username: str) -> str:
    return (username or "").strip().lstrip("@")


def extract_month_name(timestamp_value) -> str:
    if not timestamp_value:
        return ""

    try:
        dt = datetime.fromtimestamp(int(timestamp_value))
        return MONTH_NAMES_RU[dt.month - 1]
    except Exception:
        return ""


def cleanup_processed_maps():
    cutoff = now_ts() - PROCESSED_TTL_SEC
    for storage in (processed_media_groups, processed_single_messages):
        old_keys = [k for k, v in storage.items() if v < cutoff]
        for k in old_keys:
            storage.pop(k, None)


def load_channel_mapping():
    global mapping_cache, mapping_cache_time

    if mapping_cache and (time.time() - mapping_cache_time < MAPPING_CACHE_TTL):
        return mapping_cache

    try:
        resp = requests.get(
            WEBHOOK_URL,
            params={"action": "get_mapping"},
            timeout=60
        )
        resp.raise_for_status()
        data = resp.json()
        mapping_cache = data.get("mapping", {})
        mapping_cache_time = time.time()
        return mapping_cache
    except Exception as e:
        print("MAPPING LOAD ERROR:", str(e), flush=True)
        return mapping_cache


def get_vk_config(channel_username: str):
    mapping = load_channel_mapping()
    return mapping.get(channel_username)


def telegram_api(method: str, data: Optional[dict] = None) -> dict:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    response = requests.post(url, data=data or {}, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error in {method}: {result}")
    return result["result"]


def telegram_api_json(method: str, payload: Optional[dict] = None) -> dict:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    response = requests.post(url, json=payload or {}, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error in {method}: {result}")
    return result["result"]


def tg_get_file_url(file_id: str) -> str:
    file_info = telegram_api("getFile", {"file_id": file_id})
    file_path = file_info["file_path"]
    return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"


def tg_download_file(file_id: str) -> str:
    file_url = tg_get_file_url(file_id)
    resp = requests.get(file_url, timeout=DOWNLOAD_TIMEOUT)
    resp.raise_for_status()

    content_type = resp.headers.get("Content-Type", "").split(";")[0].strip()
    guessed_ext = mimetypes.guess_extension(content_type) if content_type else None
    suffix = guessed_ext or ""

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(resp.content)
    tmp.flush()
    tmp.close()
    return tmp.name


def vk_api(method: str, params: dict, token: str) -> dict:
    url = f"https://api.vk.com/method/{method}"
    payload = {
        **params,
        "access_token": str(token).strip(),
        "v": "5.199",
    }

    last_error = None

    for attempt in range(3):
        try:
            resp = requests.post(url, data=payload, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()

            if "error" in data:
                raise RuntimeError(f"VK API error in {method}: {data['error']}")

            return data["response"]

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status in (502, 503, 504) and attempt < 2:
                print(f"VK RETRY {method}: HTTP {status}, attempt={attempt + 1}", flush=True)
                time.sleep(2 * (attempt + 1))
                last_error = e
                continue
            raise

        except requests.exceptions.RequestException as e:
            if attempt < 2:
                print(f"VK RETRY {method}: network error, attempt={attempt + 1}, error={e}", flush=True)
                time.sleep(2 * (attempt + 1))
                last_error = e
                continue
            raise

    if last_error:
        raise last_error

    raise RuntimeError(f"VK API failed in {method} with unknown error")


def vk_upload_wall_photo(file_path: str, group_id: str, token: str) -> str:
    upload_server = vk_api("photos.getWallUploadServer", {
        "group_id": group_id
    }, token)

    with open(file_path, "rb") as photo_file:
        upload_resp = requests.post(
            upload_server["upload_url"],
            files={"photo": (os.path.basename(file_path), photo_file)},
            timeout=UPLOAD_TIMEOUT
        )

    upload_resp.raise_for_status()
    uploaded = upload_resp.json()

    if not all(k in uploaded for k in ("photo", "server", "hash")):
        raise RuntimeError(f"VK upload bad response: {uploaded}")

    saved = vk_api("photos.saveWallPhoto", {
        "group_id": group_id,
        "photo": uploaded["photo"],
        "server": uploaded["server"],
        "hash": uploaded["hash"]
    }, token)

    if not saved:
        raise RuntimeError("VK photos.saveWallPhoto returned empty response")

    photo_obj = saved[0]
    return f"photo{photo_obj['owner_id']}_{photo_obj['id']}"


def vk_post_to_wall(text: str, attachments: Optional[List[str]], group_id: str, token: str) -> dict:
    params = {
        "owner_id": -int(group_id),
        "from_group": 1,
        "message": text,
    }

    if attachments:
        params["attachments"] = ",".join(attachments)

    return vk_api("wall.post", params, token)


def extract_author(text: str) -> str:
    if not text:
        return ""

    patterns = [
        r"(?:Автор|Общалась|Общался)\s*[:\-–—]\s*([^\n\r|]+)",
        r"(?:Автор|Общалась|Общался)\s+([А-ЯA-ZЁ][^\n\r|]+)",
        r"\(\s*(?:Автор|Общалась|Общался)\s*[:\-–—]?\s*([^)]+)\)",
        r"Авторство\s*[:\-–—]\s*([^\n\r|]+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            author = match.group(1).strip()
            author = re.sub(r"(Фото|Источник|©).*", "", author, flags=re.IGNORECASE).strip()
            author = author.strip(" .,:;-–—|")
            return author

    return ""



# ======================
# EDITORIAL CMS SAFE HELPERS
# ======================
def normalize_url(url: str) -> str:
    if not url:
        return ""

    url = str(url).strip()
    if not url:
        return ""

    if url.startswith("www."):
        url = "https://" + url

    try:
        parsed = urlparse(url)
        if not parsed.scheme:
            parsed = urlparse("https://" + url)

        scheme = (parsed.scheme or "https").lower()
        netloc = (parsed.netloc or "").lower()
        path = parsed.path or ""

        clean_query = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            lk = key.lower()
            if lk.startswith("utm_") or lk in (
                "fbclid", "gclid", "yclid", "ysclid",
                "from", "si", "feature", "ref", "ref_src",
            ):
                continue
            clean_query.append((key, value))

        query = "&".join(
            f"{k}={v}" if v != "" else k
            for k, v in clean_query
        )

        return urlunparse((scheme, netloc, path.rstrip("/"), "", query, "")).strip()
    except Exception:
        return url


def is_yandex_music_link(url: str) -> bool:
    if not url:
        return False

    try:
        parsed = urlparse(url if "://" in url else "https://" + url)
        host = (parsed.netloc or "").lower()
        path = (parsed.path or "").lower()
        return "music.yandex" in host or (host == "yandex.ru" and "/music/" in path)
    except Exception:
        return "music.yandex" in url.lower()


def is_common_music_link(url: str) -> bool:
    if not url:
        return False

    low = url.lower().strip()

    if is_yandex_music_link(low):
        return False

    excluded = (
        "t.me/",
        "telegram.me/",
        "telegra.ph/",
        "vk.com/",
        "youtube.com/",
        "youtu.be/",
        "instagram.com/",
        "docs.google.com/",
        "drive.google.com/",
    )
    if any(domain in low for domain in excluded):
        return False

    if any(domain in low for domain in COMMON_LINK_DOMAINS):
        return True

    # Fallback for new smartlink providers:
    # any external http(s) link can be a Common Link if it is not excluded above.
    if low.startswith("http://") or low.startswith("https://"):
        return True

    return False


def tg_entity_slice(text: str, offset: int, length: int) -> str:
    """
    Telegram entity offsets are UTF-16 code units.
    This extracts visible URLs safely even if text contains emoji.
    """
    try:
        raw = text.encode("utf-16-le")
        part = raw[offset * 2:(offset + length) * 2]
        return part.decode("utf-16-le", errors="ignore")
    except Exception:
        try:
            return text[offset:offset + length]
        except Exception:
            return ""


def extract_all_links_from_post(post: dict) -> List[str]:
    text = post.get("text") or post.get("caption") or ""
    links: List[str] = []

    if text:
        links.extend(re.findall(r"https?://[^\s<>\]\)\"']+|www\.[^\s<>\]\)\"']+", text))

    for field in ("entities", "caption_entities"):
        entities = post.get(field) or []
        for entity in entities:
            etype = entity.get("type")
            if etype == "text_link":
                url = entity.get("url")
                if url:
                    links.append(url)
            elif etype == "url":
                offset = entity.get("offset")
                length = entity.get("length")
                if offset is not None and length is not None:
                    url = tg_entity_slice(text, int(offset), int(length))
                    if url:
                        links.append(url)

    result = []
    seen = set()
    for link in links:
        clean = normalize_url(link)
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)

    return result


def extract_music_links_from_post(post: dict):
    yandex_links = []
    common_links = []

    for link in extract_all_links_from_post(post):
        if is_yandex_music_link(link):
            yandex_links.append(link)
        elif is_common_music_link(link):
            common_links.append(link)

    return "\n".join(yandex_links), "\n".join(common_links)


def get_post_text(post: dict) -> str:
    return (post.get("text") or post.get("caption") or "").strip()


def get_thread_id(post: dict):
    return post.get("message_thread_id")


def get_chat_id(post: dict):
    return (post.get("chat") or {}).get("id")


def get_genre_from_thread(thread_id) -> str:
    if thread_id is None:
        return ""

    try:
        return THREAD_MAP.get(int(thread_id), "")
    except Exception:
        return ""


def build_message_link_any_chat(post: dict) -> str:
    chat = post.get("chat") or {}
    username = normalize_channel_username(chat.get("username", ""))
    message_id = post.get("message_id")
    chat_id = chat.get("id")

    if username and message_id:
        return f"https://t.me/{username}/{message_id}"

    if chat_id and message_id:
        chat_id_str = str(chat_id)
        if chat_id_str.startswith("-100"):
            internal_id = chat_id_str[4:]
            return f"https://t.me/c/{internal_id}/{message_id}"

    return ""


def infer_post_type(text: str) -> str:
    low = (text or "").lower()
    if "#сверхновые" in low:
        return "digest"
    return "review"


def safe_status_for_predlozhka(post_type: str, yandex_link: str, common_link: str) -> str:
    if post_type == "digest":
        return "pending"
    if yandex_link or common_link:
        return "pending"
    return "no_link"


def debug_custom_emoji_entities(post: dict):
    """Print custom_emoji_id values from incoming Telegram messages."""
    if not post:
        return

    text = post.get("text") or post.get("caption") or ""
    entities = (post.get("entities") or []) + (post.get("caption_entities") or [])

    for entity in entities:
        if entity.get("type") != "custom_emoji":
            continue

        custom_emoji_id = entity.get("custom_emoji_id")
        offset = entity.get("offset")
        length = entity.get("length")
        visible = ""

        if offset is not None and length is not None:
            visible = tg_entity_slice(text, int(offset), int(length))

        print(
            "CUSTOM EMOJI FOUND:",
            {
                "custom_emoji_id": custom_emoji_id,
                "visible": visible,
                "from_id": (post.get("from") or {}).get("id"),
                "from_username": (post.get("from") or {}).get("username"),
                "chat_id": get_chat_id(post),
                "message_id": post.get("message_id"),
            },
            flush=True,
        )


def _send_emojipack_lines(chat_id, lines: List[str], thread_id=None):
    """Send HTML lines in safe Telegram-sized chunks."""
    chunks = []
    current = []
    current_len = 0

    for line in lines:
        extra = len(line) + (1 if current else 0)
        if current and current_len + extra > 3400:
            chunks.append("\n".join(current))
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += extra

    if current:
        chunks.append("\n".join(current))

    for chunk in chunks:
        payload = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if thread_id:
            try:
                payload["message_thread_id"] = int(thread_id)
            except Exception:
                pass
        result = telegram_api_json("sendMessage", payload)
        _schedule_result_cleanup(result)


def handle_emojipack_command(post: dict) -> bool:
    """
    /emojipack [pack_name]

    Loads the custom emoji sticker set through Bot API getStickerSet and
    prints every custom_emoji_id. Each row also embeds the corresponding
    custom emoji so the editor can visually match an ID to the icon.
    """
    text = get_post_text(post).strip()
    if not text:
        return False

    parts = text.split(maxsplit=1)
    command = parts[0].split("@", 1)[0].casefold()
    if command != "/emojipack":
        return False

    chat_id = get_chat_id(post)
    if not chat_id:
        return True

    pack_name = parts[1].strip() if len(parts) > 1 and parts[1].strip() else CUSTOM_EMOJI_PACK_NAME
    thread_id = get_thread_id(post)

    try:
        sticker_set = telegram_api("getStickerSet", {"name": pack_name})
        stickers = sticker_set.get("stickers") or []

        rows = []
        for index, sticker in enumerate(stickers, start=1):
            custom_emoji_id = str(sticker.get("custom_emoji_id") or "").strip()
            if not custom_emoji_id:
                continue

            visible = str(sticker.get("emoji") or "🙂")
            safe_visible = html_escape(visible)
            rows.append(
                f'{index}. <tg-emoji emoji-id="{html_escape(custom_emoji_id)}">{safe_visible}</tg-emoji> '
                f'<code>{html_escape(custom_emoji_id)}</code>'
            )

            print(
                "EMOJI PACK ITEM:",
                {
                    "pack": pack_name,
                    "index": index,
                    "emoji": visible,
                    "custom_emoji_id": custom_emoji_id,
                },
                flush=True,
            )

        title = sticker_set.get("title") or pack_name
        header = [
            f"<b>Набор:</b> {html_escape(title)}",
            f"<b>Имя:</b> <code>{html_escape(pack_name)}</code>",
            f"<b>Найдено custom emoji:</b> {len(rows)}",
            "",
        ]

        if not rows:
            header.append("В этом наборе Bot API не вернул custom_emoji_id.")

        _send_emojipack_lines(chat_id, header + rows, thread_id)
        print(
            "EMOJI PACK LOADED:",
            {"pack": pack_name, "title": title, "count": len(rows)},
            flush=True,
        )
    except Exception as e:
        print("EMOJI PACK ERROR:", {"pack": pack_name, "error": str(e)}, flush=True)
        payload = {
            "chat_id": chat_id,
            "text": (
                "❌ Не удалось получить набор custom emoji.\n"
                f"Pack: <code>{html_escape(pack_name)}</code>\n"
                f"Ошибка: <code>{html_escape(str(e))}</code>"
            ),
            "parse_mode": "HTML",
        }
        if thread_id:
            try:
                payload["message_thread_id"] = int(thread_id)
            except Exception:
                pass
        result = telegram_api_json("sendMessage", payload)
        _schedule_result_cleanup(result)

    return True


def debug_thread_info(post: dict, source: str):
    try:
        chat = post.get("chat") or {}
        print(
            "🔥 THREAD DEBUG:",
            {
                "source": source,
                "chat_id": chat.get("id"),
                "chat_title": chat.get("title"),
                "chat_username": chat.get("username"),
                "thread_id": post.get("message_thread_id"),
                "message_id": post.get("message_id"),
                "from_id": (post.get("from") or {}).get("id"),
                "from_username": (post.get("from") or {}).get("username"),
                "from_is_bot": (post.get("from") or {}).get("is_bot"),
                "text": (post.get("text") or post.get("caption") or "")[:300],
            },
            flush=True
        )
    except Exception as e:
        print("THREAD DEBUG ERROR:", str(e), flush=True)



def is_allowed_predlozhka_chat(post: dict) -> bool:
    chat_id = get_chat_id(post)

    if chat_id is None:
        return False

    chat_id_str = str(chat_id)

    if not PREDLOZHKA_ALLOWED_CHAT_IDS:
        # Extra safe default: if the env is empty, capture nothing.
        return False

    return chat_id_str in PREDLOZHKA_ALLOWED_CHAT_IDS


def is_allowed_predlozhka_thread(post: dict) -> bool:
    thread_id = get_thread_id(post)

    if thread_id is None:
        return False

    try:
        return int(thread_id) in THREAD_MAP
    except Exception:
        return False


def send_predlozhka_to_sheets(post: dict):
    """
    Writes supergroup/topic messages to the Predlozhka sheet.
    Disabled by default for safety until Apps Script is updated.
    """
    debug_thread_info(post, "message")

    if not is_allowed_predlozhka_chat(post):
        print(
            "PREDLOZHKA CAPTURE SKIPPED: chat_id is not allowed",
            {
                "chat_id": get_chat_id(post),
                "allowed": sorted(PREDLOZHKA_ALLOWED_CHAT_IDS),
                "text": get_post_text(post)[:120],
            },
            flush=True
        )
        return

    if not is_allowed_predlozhka_thread(post):
        print(
            "PREDLOZHKA CAPTURE SKIPPED: thread_id is not allowed",
            {
                "chat_id": get_chat_id(post),
                "thread_id": get_thread_id(post),
                "allowed_threads": sorted(THREAD_MAP.keys()),
                "text": get_post_text(post)[:120],
            },
            flush=True
        )
        return

    if not PREDLOZHKA_CAPTURE_ENABLED:
        print("PREDLOZHKA CAPTURE DISABLED: set PREDLOZHKA_CAPTURE_ENABLED=1 after Apps Script update", flush=True)
        return

    text = get_post_text(post)
    message_id = post.get("message_id")
    chat = post.get("chat") or {}
    date_value = post.get("date")
    media_group_id = post.get("media_group_id", "")
    thread_id = get_thread_id(post)
    chat_id = get_chat_id(post)
    genre = get_genre_from_thread(thread_id)

    yandex_link, common_link = extract_music_links_from_post(post)
    cover_file_id = extract_image_file_id(post) or ""
    cover_url = extract_cover_download_url(post) or str(post.get("_editorial_cover_url") or "")
    post_type = infer_post_type(text)
    status = safe_status_for_predlozhka(post_type, yandex_link, common_link)

    author = extract_author(text)
    month = extract_month_name(date_value)
    link = build_message_link_any_chat(post)

    data = {
        "sheet": PREDLOZHKA_SHEET_NAME,
        "title": text,
        "channel": genre or str(thread_id or ""),
        "genre": genre,
        "date": str(date_value),
        "link": link,
        "media_group_id": str(media_group_id),
        "message_id": str(message_id),
        "chat_id": str(chat_id or ""),
        "thread_id": str(thread_id or ""),
        "author": author,
        "month": month,
        "type": post_type,
        "status": status,
        "yandex_link": yandex_link,
        "common_link": common_link,
        "cover_file_id": cover_file_id,
        "cover_url": cover_url,
    }

    try:
        resp = requests.post(WEBHOOK_URL, json=data, timeout=60)
        print("PREDLOZHKA SHEETS OK:", resp.status_code, resp.text[:300], flush=True)
        try:
            payload = resp.json()
            row_number = payload.get("row")
            if row_number:
                post["_editorial_sheet_row"] = int(row_number)
                print(
                    "EDITORIAL SHEET ROW LINKED:",
                    {"row": row_number, "message_id": message_id, "genre": genre},
                    flush=True,
                )
        except Exception as parse_error:
            print("EDITORIAL SHEET ROW PARSE ERROR:", str(parse_error), flush=True)
    except Exception as e:
        print("PREDLOZHKA SHEETS ERROR:", str(e), flush=True)


def send_to_sheets(post: dict):
    text = post.get("text") or post.get("caption") or ""
    message_id = post.get("message_id")
    channel = normalize_channel_username((post.get("chat") or {}).get("username", ""))
    date_value = post.get("date")
    media_group_id = post.get("media_group_id", "")

    link = f"https://t.me/{channel}/{message_id}" if channel else build_message_link_any_chat(post)
    author = extract_author(text)

    if not author and channel:
        author = CHANNEL_CURATORS.get(channel, "")

    month = extract_month_name(date_value)

    thread_id = get_thread_id(post)
    chat_id = get_chat_id(post)
    genre = get_genre_from_thread(thread_id)
    yandex_link, common_link = extract_music_links_from_post(post)

    data = {
        # Existing fields — keep unchanged for old Apps Script
        "title": text,
        "channel": channel,
        "date": str(date_value),
        "link": link,
        "media_group_id": str(media_group_id),
        "message_id": str(message_id),
        "author": author,
        "month": month,

        # New optional fields — old Apps Script safely ignores them
        "sheet": PUBLISHED_SHEET_NAME,
        "type": "published",
        "status": "done",
        "chat_id": str(chat_id or ""),
        "thread_id": str(thread_id or ""),
        "genre": genre,
        "yandex_link": yandex_link,
        "common_link": common_link,
    }

    try:
        requests.post(WEBHOOK_URL, json=data, timeout=60)
    except Exception as e:
        print("SHEETS ERROR:", str(e), flush=True)

def build_link(post: dict) -> str:
    channel = (post.get("chat") or {}).get("username", "")
    message_id = post.get("message_id")
    if channel and message_id:
        return f"https://t.me/{channel}/{message_id}"
    return ""


def extract_caption_or_text(post: dict) -> str:
    return (post.get("text") or post.get("caption") or "").strip()


def extract_image_file_id(post: dict) -> Optional[str]:
    photos = post.get("photo") or []
    if photos:
        return photos[-1].get("file_id")

    doc = post.get("document") or {}
    mime_type = (doc.get("mime_type") or "").lower()
    file_name = (doc.get("file_name") or "").lower()

    if mime_type.startswith("image/"):
        return doc.get("file_id")

    if file_name.endswith((".jpg", ".jpeg", ".png", ".webp")):
        return doc.get("file_id")

    return None


def append_source_link(text: str, link: str) -> str:
    if not APPEND_SOURCE_LINK or not link:
        return text
    if not text:
        return link
    return f"{text}\n\nИсточник: {link}"


def debug_post_media(post: dict):
    print(
        "MEDIA DEBUG:",
        {
            "message_id": post.get("message_id"),
            "has_photo": bool(post.get("photo")),
            "has_document": bool(post.get("document")),
            "document_mime": (post.get("document") or {}).get("mime_type"),
            "document_name": (post.get("document") or {}).get("file_name"),
            "has_video": bool(post.get("video")),
            "has_animation": bool(post.get("animation")),
            "media_group_id": post.get("media_group_id"),
        },
        flush=True
    )


def publish_single_post_to_vk(post: dict):
    message_id = post.get("message_id")
    if not message_id:
        return

    cleanup_processed_maps()

    if message_id in processed_single_messages:
        print(f"SKIP single duplicate message_id={message_id}", flush=True)
        return

    channel_username = (post.get("chat") or {}).get("username", "")
    vk_config = get_vk_config(channel_username)
    if not vk_config:
        print(f"NO VK CONFIG FOR CHANNEL: {channel_username}", flush=True)
        return

    group_id = str(vk_config["group_id"]).strip()
    token = str(vk_config["token"]).strip()

    print(
        f"VK CONFIG DEBUG channel={channel_username}, group_id={group_id}, token_prefix={token[:12]}, token_len={len(token)}",
        flush=True
    )

    text = extract_caption_or_text(post)
    link = build_link(post)
    final_text = append_source_link(text, link)

    image_file_id = extract_image_file_id(post)

    temp_files = []
    try:
        attachments = []

        if image_file_id:
            local_path = tg_download_file(image_file_id)
            temp_files.append(local_path)
            attachments.append(vk_upload_wall_photo(local_path, group_id, token))

        vk_result = vk_post_to_wall(
            final_text,
            attachments=attachments,
            group_id=group_id,
            token=token
        )

        processed_single_messages[message_id] = now_ts()
        print(f"VK OK single message_id={message_id}, result={vk_result}", flush=True)

    finally:
        for path in temp_files:
            try:
                os.remove(path)
            except OSError:
                pass


def publish_album_to_vk(media_group_id: str, items: List[dict]):
    cleanup_processed_maps()

    if media_group_id in processed_media_groups:
        print(f"SKIP album duplicate media_group_id={media_group_id}", flush=True)
        return

    if not items:
        return

    items_sorted = sorted(items, key=lambda x: x.get("message_id", 0))

    first_post = items_sorted[0]
    channel_username = (first_post.get("chat") or {}).get("username", "")
    vk_config = get_vk_config(channel_username)
    if not vk_config:
        print(f"NO VK CONFIG FOR CHANNEL: {channel_username}", flush=True)
        return

    group_id = str(vk_config["group_id"]).strip()
    token = str(vk_config["token"]).strip()

    print(
        f"VK CONFIG DEBUG channel={channel_username}, group_id={group_id}, token_prefix={token[:12]}, token_len={len(token)}",
        flush=True
    )

    text = ""
    link = ""
    for item in items_sorted:
        candidate = extract_caption_or_text(item)
        if candidate and not text:
            text = candidate
        if not link:
            link = build_link(item)

    final_text = append_source_link(text, link)

    temp_files = []
    try:
        attachments = []

        for item in items_sorted:
            image_file_id = extract_image_file_id(item)
            if not image_file_id:
                continue

            local_path = tg_download_file(image_file_id)
            temp_files.append(local_path)

            vk_attachment = vk_upload_wall_photo(local_path, group_id, token)
            attachments.append(vk_attachment)

        if not attachments and final_text:
            vk_result = vk_post_to_wall(final_text, attachments=None, group_id=group_id, token=token)
        elif attachments:
            vk_result = vk_post_to_wall(final_text, attachments=attachments, group_id=group_id, token=token)
        else:
            print(f"ALBUM {media_group_id} has no supported content; skipped", flush=True)
            return

        processed_media_groups[media_group_id] = now_ts()
        print(f"VK OK album media_group_id={media_group_id}, result={vk_result}", flush=True)

    finally:
        for path in temp_files:
            try:
                os.remove(path)
            except OSError:
                pass


def finalize_album(media_group_id: str):
    with album_lock:
        bundle = album_buffer.pop(media_group_id, None)

    if not bundle:
        return

    items = bundle.get("items", [])
    try:
        publish_album_to_vk(media_group_id, items)
    except Exception as e:
        print(f"VK ALBUM ERROR {media_group_id}: {e}", flush=True)


def buffer_album_post(post: dict):
    media_group_id = str(post.get("media_group_id") or "")
    if not media_group_id:
        return

    with album_lock:
        existing = album_buffer.get(media_group_id)
        if not existing:
            existing = {
                "items": [],
                "timer": None,
            }
            album_buffer[media_group_id] = existing

        existing["items"].append(post)

        old_timer = existing.get("timer")
        if old_timer:
            old_timer.cancel()

        timer = threading.Timer(ALBUM_DELAY_SEC, finalize_album, args=[media_group_id])
        timer.daemon = True
        existing["timer"] = timer
        timer.start()




# ======================
# AUTOPUBLISH PREVIEW HELPERS
# ======================
def get_autopublish_tz():
    try:
        return ZoneInfo(AUTOPUBLISH_TIMEZONE)
    except Exception:
        return ZoneInfo("Europe/Moscow")


def telegram_api_multipart(method: str, data: dict, files: dict) -> dict:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    response = requests.post(url, data=data, files=files, timeout=UPLOAD_TIMEOUT)
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error in {method}: {result}")
    return result["result"]


def resolve_frame_path(genre: str) -> str:
    """Find the PNG frame in configured/common folders or anywhere inside the repo."""
    file_name = GENRE_FRAME_FILES.get(genre)
    if not file_name:
        return ""

    script_dir = os.path.dirname(os.path.abspath(__file__))
    alt_names = [file_name, f"{genre}_frame.png", f"{genre}.png"]

    dir_candidates = []
    raw_dirs = [
        GENRE_FRAME_DIR,
        "genre_frames_ready",
        "genre_frames",
        os.path.join(script_dir, GENRE_FRAME_DIR),
        os.path.join(script_dir, "genre_frames_ready"),
        os.path.join(script_dir, "genre_frames"),
    ]

    for item in raw_dirs:
        if not item:
            continue
        normalized = item if os.path.isabs(item) else os.path.abspath(item)
        if normalized not in dir_candidates:
            dir_candidates.append(normalized)

    if os.path.isfile(GENRE_FRAME_DIR):
        return GENRE_FRAME_DIR

    for directory in dir_candidates:
        for candidate_name in alt_names:
            path = os.path.join(directory, candidate_name)
            if os.path.isfile(path):
                print("AUTOPUBLISH FRAME FOUND:", {"genre": genre, "path": path}, flush=True)
                return path

    # Last-resort recursive search inside the deployed repository.
    for root, _, files in os.walk(script_dir):
        lower_map = {name.casefold(): name for name in files}
        for candidate_name in alt_names:
            actual = lower_map.get(candidate_name.casefold())
            if actual:
                path = os.path.join(root, actual)
                print("AUTOPUBLISH FRAME FOUND RECURSIVELY:", {"genre": genre, "path": path}, flush=True)
                return path

    print(
        "AUTOPUBLISH FRAME SEARCH FAILED:",
        {"genre": genre, "requested_dir": GENRE_FRAME_DIR, "checked_dirs": dir_candidates, "checked_names": alt_names},
        flush=True,
    )
    return ""

def extract_cover_download_url(post: dict) -> str:
    """Find the hyperlink attached to a line such as “скачать обложку”."""
    text = get_post_text(post)
    entities = post.get("entities") or post.get("caption_entities") or []

    cursor = 0
    for raw_line in text.splitlines(keepends=True):
        visible = raw_line.rstrip("\r\n")
        line_start = cursor
        line_end = line_start + len(visible)
        cursor += len(raw_line)

        if "скачать облож" not in visible.casefold():
            continue

        # Prefer the URL embedded into the words “скачать обложку”.
        for entity in entities:
            try:
                start = _utf16_to_py_index(text, int(entity.get("offset", 0)))
                end = _utf16_to_py_index(
                    text,
                    int(entity.get("offset", 0)) + int(entity.get("length", 0)),
                )
            except Exception:
                continue

            if start < line_start or end > line_end:
                continue

            if entity.get("type") == "text_link" and entity.get("url"):
                return str(entity["url"]).strip()
            if entity.get("type") == "url":
                return text[start:end].strip()

        # Fallback when the URL is printed visibly on the same line.
        match = re.search(r"https?://[^\s<>\]\)\"']+", visible)
        if match:
            return match.group(0).rstrip(".,;:!?")

    return ""


def _direct_download_url(url: str) -> str:
    """Convert common Google Drive share links into direct-download links."""
    value = (url or "").strip()
    if not value:
        return ""

    match = re.search(r"drive\.google\.com/file/d/([^/]+)", value)
    if match:
        return f"https://drive.google.com/uc?export=download&id={match.group(1)}"

    parsed = urlparse(value)
    if "drive.google.com" in (parsed.netloc or "").lower():
        params = dict(parse_qsl(parsed.query))
        if params.get("id"):
            return f"https://drive.google.com/uc?export=download&id={params['id']}"

    return value


def download_cover_from_url(url: str) -> str:
    """Download an image from the cover hyperlink and validate it with Pillow."""
    direct_url = _direct_download_url(url)
    if not direct_url:
        raise RuntimeError("Cover URL is empty")

    response = requests.get(
        direct_url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; ChtoMusicBot/1.0)"},
        timeout=DOWNLOAD_TIMEOUT,
        allow_redirects=True,
    )
    response.raise_for_status()

    content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip()
    suffix = mimetypes.guess_extension(content_type) or ".img"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(response.content)
    tmp.flush()
    tmp.close()

    try:
        with Image.open(tmp.name) as image:
            image.verify()
    except Exception:
        try:
            os.remove(tmp.name)
        except OSError:
            pass
        raise RuntimeError(
            f"Ссылка ‘скачать обложку’ не вернула изображение (Content-Type: {content_type or 'unknown'})"
        )

    return tmp.name


def prepare_framed_cover(source_path: str, genre: str) -> str:
    """Create a 1280x1280 cover and apply the genre PNG frame when configured."""
    with Image.open(source_path) as source:
        cover = ImageOps.fit(source.convert("RGB"), (1280, 1280), method=Image.Resampling.LANCZOS)
        result = cover.convert("RGBA")

    frame_path = resolve_frame_path(genre)
    if frame_path:
        with Image.open(frame_path) as frame_source:
            frame = frame_source.convert("RGBA")
            if frame.size != (1280, 1280):
                frame = frame.resize((1280, 1280), Image.Resampling.LANCZOS)
            result = Image.alpha_composite(result, frame)
            print(
                "AUTOPUBLISH FRAME APPLIED:",
                {"genre": genre, "frame_path": frame_path, "source_path": source_path},
                flush=True,
            )
    elif genre in GENRE_FRAME_FILES:
        # Never send an unframed preview for a genre that must have a frame.
        raise RuntimeError(
            f"Не найдена рамка для жанра {genre}. Добавь файл {GENRE_FRAME_FILES.get(genre)} в папку genre_frames."
        )

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    tmp.close()
    result.save(tmp.name, "PNG", optimize=True)
    return tmp.name


def normalize_author_name(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


def author_emoji_html(author: str) -> str:
    normalized = normalize_author_name(author)
    for known_author, item in AUTHOR_EMOJI_MAP.items():
        if normalize_author_name(known_author) == normalized:
            emoji_id = item.get("emoji_id") or ""
            visible = html_escape(item.get("visible") or "🙂")
            if emoji_id:
                return f'<tg-emoji emoji-id="{html_escape(emoji_id)}">{visible}</tg-emoji>'
    return ""


def _utf16_to_py_index(text: str, utf16_offset: int) -> int:
    """Convert a Telegram UTF-16 entity offset to a Python string index."""
    if utf16_offset <= 0:
        return 0
    units = 0
    for index, char in enumerate(text):
        char_units = len(char.encode("utf-16-le")) // 2
        if units + char_units > utf16_offset:
            return index
        units += char_units
        if units == utf16_offset:
            return index + 1
    return len(text)


def _render_entity_line_html(text: str, entities: List[dict], line_start: int, line_end: int) -> str:
    """Render one source line to Telegram HTML while preserving text links and formatting."""
    line_text = text[line_start:line_end]
    relevant = []

    for entity in entities or []:
        try:
            entity_start = _utf16_to_py_index(text, int(entity.get("offset", 0)))
            entity_end = _utf16_to_py_index(
                text,
                int(entity.get("offset", 0)) + int(entity.get("length", 0)),
            )
        except Exception:
            continue

        # Message entities normally do not span multiple lines. Ignore partial overlaps safely.
        if entity_start < line_start or entity_end > line_end or entity_start >= entity_end:
            continue

        relevant.append({
            **entity,
            "start": entity_start - line_start,
            "end": entity_end - line_start,
        })

    opens = {}
    closes = {}

    def tags_for(entity: dict):
        etype = entity.get("type")
        if etype == "bold":
            return "<b>", "</b>"
        if etype == "italic":
            return "<i>", "</i>"
        if etype == "underline":
            return "<u>", "</u>"
        if etype == "strikethrough":
            return "<s>", "</s>"
        if etype == "spoiler":
            return '<span class="tg-spoiler">', "</span>"
        if etype == "code":
            return "<code>", "</code>"
        if etype == "pre":
            language = html_escape(entity.get("language") or "")
            if language:
                return f'<pre><code class="language-{language}">', "</code></pre>"
            return "<pre>", "</pre>"
        if etype == "text_link":
            url = html_escape(entity.get("url") or "")
            return (f'<a href="{url}">', "</a>") if url else ("", "")
        if etype == "url":
            visible = line_text[entity["start"]:entity["end"]]
            url = html_escape(visible)
            return f'<a href="{url}">', "</a>"
        if etype == "custom_emoji":
            emoji_id = html_escape(entity.get("custom_emoji_id") or "")
            return (f'<tg-emoji emoji-id="{emoji_id}">', "</tg-emoji>") if emoji_id else ("", "")
        return "", ""

    for entity in relevant:
        open_tag, close_tag = tags_for(entity)
        if not open_tag:
            continue
        opens.setdefault(entity["start"], []).append((entity["end"], open_tag))
        closes.setdefault(entity["end"], []).append((entity["start"], close_tag))

    parts = []
    for index in range(len(line_text) + 1):
        # Close inner entities first.
        for _, tag in sorted(closes.get(index, []), key=lambda item: item[0], reverse=True):
            parts.append(tag)
        # Open outer entities first.
        for _, tag in sorted(opens.get(index, []), key=lambda item: item[0], reverse=True):
            parts.append(tag)
        if index < len(line_text):
            parts.append(html_escape(line_text[index]))

    return "".join(parts)


def _clean_publish_lines(text: str):
    """Return retained source lines; move public channel @mentions to the bottom."""
    retained = []
    bottom_mentions = []
    cursor = 0

    for raw_line in text.splitlines(keepends=True):
        visible = raw_line.rstrip("\r\n")
        start = cursor
        end = start + len(visible)
        cursor += len(raw_line)

        stripped = visible.strip()
        low = stripped.casefold()

        if not stripped:
            retained.append({"text": "", "start": start, "end": end})
            continue

        # Lines explicitly marked for deletion must never reach the channel.
        if "удалить перед публикацией" in low:
            continue

        # A plain @channel line is public and must remain, but always at the bottom.
        if low.startswith("@"):
            bottom_mentions.append({"text": visible, "start": start, "end": end})
            continue

        # The cover-download control is used internally and removed from the caption.
        if "скачать облож" in low:
            continue

        retained.append({"text": visible, "start": start, "end": end})

    # Remove leading/trailing and repeated empty lines.
    compact = []
    for item in retained:
        if not item["text"].strip():
            if not compact or not compact[-1]["text"].strip():
                continue
        compact.append(item)
    while compact and not compact[-1]["text"].strip():
        compact.pop()

    if bottom_mentions:
        if compact and compact[-1]["text"].strip():
            compact.append({"text": "", "start": 0, "end": 0})
        compact.extend(bottom_mentions)

    return compact


def custom_emoji_html(config: Optional[dict]) -> str:
    config = config or {}
    emoji_id = str(config.get("emoji_id") or "").strip()
    visible = str(config.get("visible") or "✨")
    if not emoji_id:
        return html_escape(visible)
    return f'<tg-emoji emoji-id="{html_escape(emoji_id)}">{html_escape(visible)}</tg-emoji>'


def genre_emoji_html(genre: str) -> str:
    return custom_emoji_html(GENRE_EMOJI_MAP.get((genre or "").strip()))


def liked_section_emoji_html() -> str:
    return custom_emoji_html(LIKED_SECTION_EMOJI)


def _strip_html_for_length(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value or "")
    return unescape(value)


def _line_is_liked_heading(value: str) -> bool:
    clean = re.sub(r"\s+", " ", (value or "").strip()).casefold()
    return bool(re.match(r"^что понравилось\s*[?:]?(?:\s|$)", clean))


def _looks_like_release_title(value: str) -> bool:
    clean = re.sub(r"\s+", " ", (value or "").strip())
    if not clean:
        return False
    return bool(re.search(r"\S\s+[—–-]\s+\S", clean))


def build_publish_caption(post: dict, genre: str = "") -> str:
    """Build a ready editorial draft while preserving source formatting and links."""
    text = get_post_text(post)
    entities = post.get("entities") or post.get("caption_entities") or []
    author = extract_author(text)
    emoji_html = author_emoji_html(author)
    lines = _clean_publish_lines(text)

    meaningful_indexes = [i for i, item in enumerate(lines) if item["text"].strip()]

    # The genre disc belongs ONLY to the Artist — Track line. It must never be
    # inserted into a quote just because the first semantic line is a teaser.
    title_index = next(
        (i for i in meaningful_indexes if _looks_like_release_title(lines[i]["text"])),
        None,
    )

    # Editorial rule: the quote is ALWAYS the first meaningful retained line.
    # The genre disc is independent and stays only on the Artist — Track line.
    quote_index = meaningful_indexes[0] if meaningful_indexes else None

    rendered = []
    author_replaced = False

    for index, item in enumerate(lines):
        if not item["text"].strip():
            if rendered and rendered[-1] != "":
                rendered.append("")
            continue

        raw_line = item["text"]
        line_html = _render_entity_line_html(text, entities, item["start"], item["end"])

        if index == title_index:
            disc = genre_emoji_html(genre)
            if disc and not raw_line.lstrip().startswith("💿"):
                line_html = f"{disc} {line_html}"

        if _line_is_liked_heading(raw_line):
            marker = liked_section_emoji_html()
            if marker:
                line_html = f"{marker} {line_html}"

        if re.match(r"^\s*Автор\s*[:\-–—]", raw_line, flags=re.IGNORECASE):
            if emoji_html:
                line_html = f"{emoji_html} {line_html}"
            author_replaced = True

        if index == quote_index:
            line_html = f"<blockquote>{line_html}</blockquote>"

        rendered.append(line_html)

    if author and emoji_html and not author_replaced:
        if rendered and rendered[-1] != "":
            rendered.append("")
        rendered.append(f"{emoji_html} Автор: {html_escape(author)}")

    return "\n".join(rendered).strip()[:3900]


def _copy_signature(row_number: int, expires_at: int) -> str:
    payload = f"{int(row_number)}:{int(expires_at)}".encode("utf-8")
    return hmac.new(BOT_TOKEN.encode("utf-8"), payload, hashlib.sha256).hexdigest()[:32]


def _build_copy_url(row_number) -> str:
    if not EDITORIAL_COPY_BASE_URL or not row_number:
        return ""
    try:
        row_number = int(row_number)
    except Exception:
        return ""
    expires_at = int(time.time()) + EDITORIAL_COPY_LINK_TTL_SEC
    signature = _copy_signature(row_number, expires_at)
    return f"{EDITORIAL_COPY_BASE_URL}/editorial-copy/{row_number}?exp={expires_at}&sig={signature}"


def _plain_publish_text(html_text: str) -> str:
    return _strip_html_for_length(html_text).strip()


def build_draft_buttons(genre: str = "", html_text: str = "", row_number=None) -> dict:
    pending_data = f"pending:{genre}" if genre else "pending:all"
    return {
        "inline_keyboard": [
            [
                {"text": "⬇️ Скачать обложку", "callback_data": "draft_cover"},
            ],
            [
                {"text": "📚 Невышедшие посты", "callback_data": pending_data},
            ],
        ]
    }

def _draft_parts_key(post: dict) -> str:
    return f"{get_chat_id(post)}:{get_thread_id(post)}"


def _is_trusted_editorial_sender(post: dict) -> bool:
    sender = post.get("from") or {}
    if not sender.get("is_bot"):
        return True
    return str(sender.get("id") or "") in AUTOPUBLISH_TRUSTED_BOT_IDS


def _has_meaningful_publish_text(post: dict) -> bool:
    text = get_post_text(post)
    if not text:
        return False
    if infer_post_type(text) == "digest":
        return False

    try:
        cleaned = _clean_publish_lines(text)
    except Exception:
        cleaned = [{"text": line} for line in text.splitlines()]

    for item in cleaned:
        value = str(item.get("text") or "").strip()
        if not value:
            continue
        if value.startswith("#"):
            continue
        low = value.casefold()
        if "скачать облож" in low or "удалить перед публикацией" in low:
            continue
        return True
    return False


def _post_has_cover_source(post: dict) -> bool:
    return bool(extract_image_file_id(post) or extract_cover_download_url(post))


def _prune_editorial_draft_parts(now_value: Optional[float] = None):
    now_value = now_value or now_ts()
    cutoff = now_value - EDITORIAL_DRAFT_PARTS_TTL_SEC
    for key in [k for k, item in editorial_draft_parts.items() if float(item.get("updated_at") or 0) < cutoff]:
        editorial_draft_parts.pop(key, None)


def _utf16_units(value: str) -> int:
    return len((value or "").encode("utf-16-le")) // 2


def _synthetic_text_link_entities(text: str, yandex_link: str = "", common_link: str = "") -> List[dict]:
    """Rebuild the two editorial music links when a draft has to be recovered from Sheets."""
    entities: List[dict] = []
    targets = [
        ("Яндекс Музыка", first_link(yandex_link)),
        ("Слушать везде", first_link(common_link)),
    ]
    low = (text or "").casefold()
    used = set()
    for label, url in targets:
        if not url:
            continue
        start = low.find(label.casefold())
        if start < 0:
            continue
        key = (start, label)
        if key in used:
            continue
        used.add(key)
        entities.append({
            "type": "text_link",
            "offset": _utf16_units(text[:start]),
            "length": _utf16_units(text[start:start + len(label)]),
            "url": url,
        })
    return entities


def _recover_draft_parts_from_sheet(row_number, current_post: dict):
    """Recover review text/cover from the authoritative Predlozhka row after restart or out-of-order updates."""
    if not row_number:
        return None, None
    try:
        item = sheets_get("get_review", {"row": str(row_number)})
    except Exception as e:
        print("EDITORIAL SHEET RECOVERY ERROR:", {"row": row_number, "error": str(e)}, flush=True)
        return None, None

    if item.get("status") != "ok":
        print("EDITORIAL SHEET RECOVERY SKIP:", {"row": row_number, "response": item}, flush=True)
        return None, None

    raw_text = str(item.get("title") or "").strip()
    text_post = None
    if raw_text and infer_post_type(raw_text) != "digest":
        text_post = {
            "message_id": item.get("message_id") or current_post.get("message_id"),
            "chat": current_post.get("chat") or {"id": item.get("chat_id")},
            "message_thread_id": current_post.get("message_thread_id") or item.get("thread_id"),
            "from": current_post.get("from") or {},
            "text": raw_text,
            "entities": _synthetic_text_link_entities(
                raw_text,
                str(item.get("yandex_link") or ""),
                str(item.get("common_link") or ""),
            ),
            "_editorial_sheet_row": row_number,
        }

    cover_file_id = str(item.get("cover_file_id") or "").strip()
    cover_url = str(item.get("cover_url") or "").strip()
    cover_post = None
    if cover_file_id or cover_url:
        cover_post = {
            "message_id": current_post.get("message_id"),
            "chat": current_post.get("chat") or {"id": item.get("chat_id")},
            "message_thread_id": current_post.get("message_thread_id") or item.get("thread_id"),
            "from": current_post.get("from") or {},
            "text": "",
            "_editorial_sheet_row": row_number,
        }
        if cover_file_id:
            cover_post["photo"] = [{"file_id": cover_file_id}]
        if cover_url:
            cover_post["_editorial_cover_url"] = cover_url

    print(
        "EDITORIAL SHEET RECOVERY:",
        {
            "row": row_number,
            "has_text": bool(text_post),
            "has_cover": bool(cover_post),
            "cover_file": bool(cover_file_id),
            "cover_url": bool(cover_url),
        },
        flush=True,
    )
    return text_post, cover_post


def assemble_editorial_draft_post(post: dict) -> Optional[dict]:
    """Pair only the text and cover that belong to the same review."""
    if not is_allowed_predlozhka_chat(post) or not is_allowed_predlozhka_thread(post):
        return post

    genre = get_genre_from_thread(get_thread_id(post))
    if genre == "underground" or genre not in TARGET_CHANNELS:
        return post

    if infer_post_type(get_post_text(post)) == "digest":
        return post

    key = _draft_parts_key(post)
    now_value = now_ts()
    row_number = post.get("_editorial_sheet_row")
    incoming_text = _is_trusted_editorial_sender(post) and _has_meaningful_publish_text(post)
    incoming_cover = bool(_post_has_cover_source(post) or post.get("_editorial_cover_url"))

    # Best case: the review and cover arrived together. Never mix it with topic cache.
    if incoming_text and incoming_cover:
        direct = dict(post)
        direct["_editorial_source_message_id"] = post.get("message_id")
        direct["_editorial_draft_key"] = f"{key}:direct:{post.get('message_id')}:{row_number or ''}"
        with editorial_draft_parts_lock:
            editorial_draft_parts[key] = {
                "updated_at": now_value,
                "text_post": post,
                "cover_post": post,
                "sheet_row": row_number,
            }
        print("EDITORIAL DRAFT EXACT MESSAGE:", {"key": key, "row": row_number, "message_id": post.get("message_id")}, flush=True)
        return direct

    # Every new meaningful review text starts a fresh pairing slot for this topic.
    # This is the key V11 fix: an old review can never remain as text for a new cover.
    with editorial_draft_parts_lock:
        _prune_editorial_draft_parts(now_value)
        if incoming_text:
            entry = {
                "updated_at": now_value,
                "text_post": post,
                "sheet_row": row_number,
            }
            editorial_draft_parts[key] = entry
        else:
            entry = editorial_draft_parts.setdefault(key, {"updated_at": now_value})

        replied = post.get("reply_to_message") or {}
        if replied and _is_trusted_editorial_sender(replied) and _has_meaningful_publish_text(replied):
            entry["text_post"] = replied

        if incoming_cover:
            entry["cover_post"] = post
        if row_number:
            entry["sheet_row"] = row_number
        entry["updated_at"] = now_value

        text_post = entry.get("text_post")
        cover_post = entry.get("cover_post")
        cached_row = entry.get("sheet_row")

    row_number = row_number or cached_row

    # If Sheets told us the exact row, that row is authoritative. Always recover
    # its text, even if topic memory currently contains something else.
    if row_number:
        recovered_text, recovered_cover = _recover_draft_parts_from_sheet(row_number, post)
        if recovered_text:
            text_post = recovered_text
        # Keep the incoming cover itself when present; otherwise use Sheets copy.
        if not incoming_cover and recovered_cover:
            cover_post = recovered_cover
        elif incoming_cover:
            cover_post = post

        with editorial_draft_parts_lock:
            entry = editorial_draft_parts.setdefault(key, {"updated_at": now_ts()})
            if text_post:
                entry["text_post"] = text_post
            if cover_post:
                entry["cover_post"] = cover_post
            entry["sheet_row"] = row_number
            entry["updated_at"] = now_ts()

    print(
        "EDITORIAL DRAFT PAIR:",
        {
            "key": key,
            "genre": genre,
            "incoming_message_id": post.get("message_id"),
            "sheet_row": row_number,
            "incoming_text": incoming_text,
            "incoming_cover": incoming_cover,
            "text_message_id": (text_post or {}).get("message_id"),
            "cover_message_id": (cover_post or {}).get("message_id"),
        },
        flush=True,
    )

    if not text_post or not cover_post:
        print(
            "EDITORIAL DRAFT WAITING FOR PART:",
            {"key": key, "genre": genre, "sheet_row": row_number, "has_text": bool(text_post), "has_cover": bool(cover_post)},
            flush=True,
        )
        return post

    combined = dict(cover_post)
    combined["chat"] = post.get("chat") or cover_post.get("chat") or text_post.get("chat") or {}
    combined["message_thread_id"] = get_thread_id(post) or get_thread_id(cover_post) or get_thread_id(text_post)
    combined["from"] = text_post.get("from") or cover_post.get("from") or {}

    if text_post.get("text") is not None:
        combined["text"] = text_post.get("text") or ""
        combined["entities"] = text_post.get("entities") or []
        combined.pop("caption", None)
        combined.pop("caption_entities", None)
    else:
        combined["caption"] = text_post.get("caption") or ""
        combined["caption_entities"] = text_post.get("caption_entities") or []
        combined.pop("text", None)
        combined.pop("entities", None)

    cover_url = extract_cover_download_url(cover_post) or str(cover_post.get("_editorial_cover_url") or "")
    if cover_url:
        combined["_editorial_cover_url"] = cover_url

    text_mid = text_post.get("message_id") or ""
    cover_mid = cover_post.get("message_id") or ""
    combined["_editorial_source_message_id"] = text_mid
    combined["_editorial_sheet_row"] = row_number or text_post.get("_editorial_sheet_row") or cover_post.get("_editorial_sheet_row")
    combined["_editorial_draft_key"] = f"{key}:{text_mid}:{cover_mid}:{combined.get('_editorial_sheet_row') or ''}"
    return combined

def _is_cover_service_only_message(post: dict) -> bool:
    if not _post_has_cover_source(post):
        return False
    text = get_post_text(post)
    if not text:
        return True
    try:
        cleaned = _clean_publish_lines(text)
        meaningful = [str(item.get("text") or "").strip() for item in cleaned if str(item.get("text") or "").strip()]
        return not any(not value.startswith("#") for value in meaningful)
    except Exception:
        low = text.casefold()
        return "скачать облож" in low and len(text.strip()) < 250


def attach_cover_to_recent_sheet_row(post: dict) -> bool:
    """Attach a standalone cover to the exact current review whenever possible."""
    if not _is_cover_service_only_message(post):
        return False
    cover_file_id = extract_image_file_id(post) or ""
    cover_url = extract_cover_download_url(post) or ""
    if not cover_file_id and not cover_url:
        return False

    key = _draft_parts_key(post)
    preferred_row = None
    with editorial_draft_parts_lock:
        entry = editorial_draft_parts.get(key) or {}
        preferred_row = entry.get("sheet_row")

    reply_to_message_id = (post.get("reply_to_message") or {}).get("message_id") or ""

    try:
        resp = requests.post(
            WEBHOOK_URL,
            json={
                "action": "attach_cover_to_recent",
                "sheet": PREDLOZHKA_SHEET_NAME,
                "chat_id": str(get_chat_id(post) or ""),
                "thread_id": str(get_thread_id(post) or ""),
                "cover_file_id": cover_file_id,
                "cover_url": cover_url,
                "cover_message_id": str(post.get("message_id") or ""),
                "preferred_row": str(preferred_row or ""),
                "reply_to_message_id": str(reply_to_message_id or ""),
            },
            timeout=60,
        )
        print("PREDLOZHKA COVER ATTACH:", resp.status_code, resp.text[:300], flush=True)
        try:
            data = resp.json()
            if data.get("status") == "ok" and data.get("row"):
                post["_editorial_sheet_row"] = int(data.get("row"))
                print(
                    "EDITORIAL COVER ROW LINKED:",
                    {
                        "row": data.get("row"),
                        "message_id": post.get("message_id"),
                        "match": data.get("match"),
                    },
                    flush=True,
                )
            else:
                print("PREDLOZHKA COVER ATTACH NOT FOUND:", data, flush=True)
        except Exception as parse_error:
            print("PREDLOZHKA COVER ATTACH PARSE ERROR:", str(parse_error), flush=True)
        return True
    except Exception as e:
        print("PREDLOZHKA COVER ATTACH ERROR:", str(e), flush=True)
        return True

def should_build_publish_preview(post: dict) -> bool:
    if not AUTOPUBLISH_PREVIEW_ENABLED:
        return False
    if not is_allowed_predlozhka_chat(post) or not is_allowed_predlozhka_thread(post):
        return False

    sender = post.get("from") or {}
    if sender.get("is_bot") and str(sender.get("id") or "") not in AUTOPUBLISH_TRUSTED_BOT_IDS:
        return False

    genre = get_genre_from_thread(get_thread_id(post))
    if genre == "underground" or genre not in TARGET_CHANNELS:
        return False

    if infer_post_type(get_post_text(post)) == "digest":
        return False

    if not extract_image_file_id(post) and not (extract_cover_download_url(post) or post.get("_editorial_cover_url")):
        return False

    message_id = post.get("message_id")
    if not message_id:
        return False

    key = str(post.get("_editorial_draft_key") or f"{get_chat_id(post)}:{message_id}")
    cutoff = now_ts() - PROCESSED_TTL_SEC
    for old_key in [k for k, ts in preview_dedup.items() if ts < cutoff]:
        preview_dedup.pop(old_key, None)
    if key in preview_dedup:
        return False
    preview_dedup[key] = now_ts()
    return True


def _schedule_result_cleanup(result: Optional[dict]):
    if not result:
        return
    chat_id = (result.get("chat") or {}).get("id")
    message_id = result.get("message_id")
    if chat_id and message_id:
        schedule_bot_message_cleanup(chat_id, message_id)


def _send_draft_photo(chat_id, thread_id, prepared_path: str, html_text: str, genre: str, row_number=None):
    plain_length = len(_strip_html_for_length(html_text))
    buttons = build_draft_buttons(genre, html_text, row_number)
    if plain_length <= 950:
        data = {
            "chat_id": str(chat_id),
            "caption": html_text,
            "parse_mode": "HTML",
            "reply_markup": json.dumps(buttons, ensure_ascii=False),
        }
        if thread_id:
            data["message_thread_id"] = str(thread_id)
        with open(prepared_path, "rb") as photo:
            result = telegram_api_multipart("sendPhoto", data, {"photo": ("ready_post.png", photo, "image/png")})
        _schedule_result_cleanup(result)
        return result

    photo_data = {"chat_id": str(chat_id)}
    if thread_id:
        photo_data["message_thread_id"] = str(thread_id)
    with open(prepared_path, "rb") as photo:
        photo_result = telegram_api_multipart("sendPhoto", photo_data, {"photo": ("ready_post.png", photo, "image/png")})
    _schedule_result_cleanup(photo_result)

    text_payload = {
        "chat_id": chat_id,
        "text": html_text,
        "parse_mode": "HTML",
        "reply_to_message_id": photo_result.get("message_id"),
        "allow_sending_without_reply": True,
        "disable_web_page_preview": True,
        "reply_markup": buttons,
    }
    if thread_id:
        text_payload["message_thread_id"] = thread_id
    text_result = telegram_api_json("sendMessage", text_payload)
    _schedule_result_cleanup(text_result)
    return text_result

def send_publish_preview(post: dict):
    genre = get_genre_from_thread(get_thread_id(post))
    image_file_id = extract_image_file_id(post)
    cover_url = extract_cover_download_url(post) or str(post.get("_editorial_cover_url") or "")
    print(
        "EDITORIAL PREVIEW START:",
        {
            "genre": genre,
            "message_id": post.get("message_id"),
            "source_message_id": post.get("_editorial_source_message_id"),
            "sheet_row": post.get("_editorial_sheet_row"),
            "has_file_id": bool(image_file_id),
            "has_cover_url": bool(cover_url),
            "text_len": len(get_post_text(post)),
        },
        flush=True,
    )
    if not genre or (not image_file_id and not cover_url):
        print("EDITORIAL PREVIEW SKIP: missing genre/cover", flush=True)
        return

    source_path = ""
    prepared_path = ""
    try:
        if image_file_id:
            source_path = tg_download_file(image_file_id)
        else:
            print("DRAFT COVER DOWNLOAD:", {"genre": genre, "url": cover_url}, flush=True)
            source_path = download_cover_from_url(cover_url)
        prepared_path = prepare_framed_cover(source_path, genre)
        caption = build_publish_caption(post, genre)
        if not _strip_html_for_length(caption).strip():
            print(
                "EDITORIAL DRAFT WAITING FOR TEXT:",
                {"genre": genre, "cover_message_id": post.get("message_id"), "source_message_id": post.get("_editorial_source_message_id")},
                flush=True,
            )
            return
        result = _send_draft_photo(get_chat_id(post), get_thread_id(post), prepared_path, caption, genre, post.get("_editorial_sheet_row"))
        print(
            "EDITORIAL DRAFT SENT:",
            {"genre": genre, "draft_message_id": result.get("message_id"), "source_message_id": post.get("message_id")},
            flush=True,
        )
    finally:
        for path in (source_path, prepared_path):
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass



def _utf16_length(value: str) -> int:
    return len((value or "").encode("utf-16-le")) // 2


def ensure_author_custom_emoji_entity(caption: str, entities: List[dict]) -> List[dict]:
    """Force the configured author emoji to remain a Telegram custom emoji on publish."""
    result = [dict(item) for item in (entities or []) if isinstance(item, dict)]

    for author, config in AUTHOR_EMOJI_MAP.items():
        emoji_id = str(config.get("emoji_id") or "").strip()
        visible = str(config.get("visible") or "🙂")
        if not emoji_id or not visible:
            continue

        # We place the emoji before the word "Автор". Find that exact rendered line.
        patterns = [
            f"{visible} Автор: {author}",
            f"{visible} Автор — {author}",
            f"{visible} Автор - {author}",
        ]
        start_index = -1
        for pattern in patterns:
            start_index = caption.find(pattern)
            if start_index >= 0:
                break

        # Fallback: find the visible emoji immediately before any Автор line.
        if start_index < 0:
            match = re.search(re.escape(visible) + r"\s+Автор\s*[:\-–—]", caption, flags=re.IGNORECASE)
            if match:
                start_index = match.start()

        if start_index < 0:
            continue

        offset = _utf16_length(caption[:start_index])
        length = _utf16_length(visible)

        # Remove a stale/duplicate entity at the same position, then add the correct ID.
        result = [
            item for item in result
            if not (
                item.get("type") == "custom_emoji"
                and int(item.get("offset", -1)) == offset
                and int(item.get("length", -1)) == length
            )
        ]
        result.append({
            "type": "custom_emoji",
            "offset": offset,
            "length": length,
            "custom_emoji_id": emoji_id,
        })

    result.sort(key=lambda item: (int(item.get("offset", 0)), -int(item.get("length", 0))))
    return result


def extract_preview_payload(message: dict) -> dict:
    """Extract photo/caption/entities from the preview message for exact re-sending."""
    photos = message.get("photo") or []
    photo_file_id = photos[-1].get("file_id") if photos else ""
    caption = message.get("caption") or ""
    entities = ensure_author_custom_emoji_entity(caption, message.get("caption_entities") or [])
    return {
        "photo_file_id": photo_file_id,
        "caption": caption,
        "caption_entities": entities,
    }


def publish_preview_payload(preview: dict, genre: str) -> dict:
    """Legacy publisher. Disabled while editorial auto-posting is paused."""
    if not AUTOPUBLISH_CHANNEL_ENABLED:
        raise RuntimeError("Автопубликация в каналы временно отключена")
    target = TARGET_CHANNELS.get(genre)
    if not target:
        raise RuntimeError(f"Target channel is not configured for genre={genre}")

    photo_file_id = preview.get("photo_file_id") or ""
    if not photo_file_id:
        raise RuntimeError("Preview photo file_id is missing")

    caption = preview.get("caption") or ""
    caption_entities = ensure_author_custom_emoji_entity(
        caption,
        preview.get("caption_entities") or [],
    )
    payload = {
        "chat_id": target,
        "photo": photo_file_id,
        "caption": caption,
        "caption_entities": caption_entities,
    }
    return telegram_api_json("sendPhoto", payload)


def load_scheduled_posts() -> List[dict]:
    with scheduled_posts_lock:
        try:
            if not os.path.isfile(AUTOPUBLISH_STORE_PATH):
                return []
            with open(AUTOPUBLISH_STORE_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, list) else []
        except Exception as e:
            print("SCHEDULE LOAD ERROR:", str(e), flush=True)
            return []


def save_scheduled_posts(items: List[dict]):
    with scheduled_posts_lock:
        directory = os.path.dirname(os.path.abspath(AUTOPUBLISH_STORE_PATH))
        os.makedirs(directory, exist_ok=True)
        tmp_path = AUTOPUBLISH_STORE_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(items, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_path, AUTOPUBLISH_STORE_PATH)


def parse_schedule_time(value: str):
    raw = (value or "").strip().lower().replace("ё", "е")
    tz = get_autopublish_tz()
    now = datetime.now(tz)

    match = re.fullmatch(r"(сегодня|завтра)\s+(\d{1,2}):(\d{2})", raw)
    if match:
        day_word, hour, minute = match.groups()
        dt = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
        if day_word == "завтра":
            dt += timedelta(days=1)
        if day_word == "сегодня" and dt <= now:
            raise ValueError("Время сегодня уже прошло")
        return dt

    for fmt in ("%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M", "%d.%m %H:%M"):
        try:
            parsed = datetime.strptime(raw, fmt)
            if fmt == "%d.%m %H:%M":
                parsed = parsed.replace(year=now.year)
                if parsed.replace(tzinfo=tz) <= now:
                    parsed = parsed.replace(year=now.year + 1)
            dt = parsed.replace(tzinfo=tz)
            if dt <= now:
                raise ValueError("Указанное время уже прошло")
            return dt
        except ValueError as e:
            if str(e) in ("Указанное время уже прошло",):
                raise
            continue
    raise ValueError("Не понял время. Пример: сегодня 18:30, завтра 12:00 или 06.08.2026 14:00")


def handle_publish_callback(callback: dict) -> bool:
    data = callback.get("data") or ""
    if not data.startswith(("pub_now:", "pub_later:", "pub_cancel:")):
        return False
    callback_id = callback.get("id")
    telegram_api("answerCallbackQuery", {
        "callback_query_id": callback_id,
        "text": "Автопубликация временно отключена. Создай новый предпросмотр.",
        "show_alert": True,
    })
    return True


def _legacy_handle_publish_callback_disabled(callback: dict) -> bool:
    data = callback.get("data") or ""
    if not data.startswith(("pub_now:", "pub_later:", "pub_cancel:")):
        return False

    callback_id = callback.get("id")
    user = callback.get("from") or {}
    user_id = user.get("id")
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    message_id = message.get("message_id")
    thread_id = message.get("message_thread_id")
    genre = data.split(":", 1)[1]

    if data.startswith("pub_now:"):
        result = publish_preview_payload(extract_preview_payload(message), genre)
        telegram_api("answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": "Опубликовано",
            "show_alert": False,
        })
        confirmation = {
            "chat_id": chat_id,
            "text": f"✅ Пост опубликован в {TARGET_CHANNELS.get(genre)}. Message ID: {result.get('message_id')}",
        }
        if thread_id:
            confirmation["message_thread_id"] = thread_id
        telegram_api_json("sendMessage", confirmation)
        return True

    if data.startswith("pub_later:"):
        awaiting_schedule[str(user_id)] = {
            "chat_id": chat_id,
            "thread_id": thread_id,
            "preview_message_id": message_id,
            "genre": genre,
            "preview": extract_preview_payload(message),
            "created_at": now_ts(),
        }
        telegram_api("answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": "Напиши время следующим сообщением",
            "show_alert": False,
        })
        prompt = {
            "chat_id": chat_id,
            "text": "🕒 На когда поставить публикацию?\n\nПримеры:\nсегодня 18:30\nзавтра 12:00\n06.08.2026 14:00",
        }
        if thread_id:
            prompt["message_thread_id"] = thread_id
        telegram_api_json("sendMessage", prompt)
        return True

    telegram_api("answerCallbackQuery", {
        "callback_query_id": callback_id,
        "text": "Предпросмотр отменён",
        "show_alert": False,
    })
    try:
        telegram_api_json("editMessageReplyMarkup", {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": {"inline_keyboard": []},
        })
    except Exception:
        pass
    return True


def handle_schedule_time_message(post: dict) -> bool:
    user_id = str((post.get("from") or {}).get("id") or "")
    state = awaiting_schedule.get(user_id)
    if not state:
        return False
    if get_chat_id(post) != state.get("chat_id") or get_thread_id(post) != state.get("thread_id"):
        return False

    text = get_post_text(post)
    try:
        scheduled_at = parse_schedule_time(text)
    except ValueError as e:
        payload = {"chat_id": get_chat_id(post), "text": f"❗ {e}"}
        if get_thread_id(post):
            payload["message_thread_id"] = get_thread_id(post)
        telegram_api_json("sendMessage", payload)
        return True

    job = {
        "id": f"{state['chat_id']}:{state['preview_message_id']}:{int(scheduled_at.timestamp())}",
        "source_chat_id": state["chat_id"],
        "source_message_id": state["preview_message_id"],
        "preview": state.get("preview") or {},
        "thread_id": state.get("thread_id"),
        "genre": state["genre"],
        "target_channel": TARGET_CHANNELS.get(state["genre"], ""),
        "scheduled_at": scheduled_at.isoformat(),
        "requested_by": user_id,
        "status": "scheduled",
    }

    save_result = sheets_post_action("add_scheduled_post", {
        "job": job,
    })
    if save_result.get("error"):
        raise RuntimeError(save_result.get("error"))

    awaiting_schedule.pop(user_id, None)

    payload = {
        "chat_id": get_chat_id(post),
        "text": f"✅ Поставил публикацию на {scheduled_at.strftime('%d.%m.%Y %H:%M')} ({AUTOPUBLISH_TIMEZONE}).",
    }
    if get_thread_id(post):
        payload["message_thread_id"] = get_thread_id(post)
    telegram_api_json("sendMessage", payload)
    return True


def run_scheduled_posts_once():
    if not AUTOPUBLISH_PREVIEW_ENABLED:
        return

    try:
        response = sheets_get("get_due_scheduled_posts", {"limit": "20"})
    except Exception as e:
        print("SCHEDULE GET DUE ERROR:", str(e), flush=True)
        return

    jobs = response.get("jobs") or []
    if not jobs:
        return

    now = datetime.now(get_autopublish_tz())

    for job in jobs:
        job_id = str(job.get("id") or "")
        try:
            preview = job.get("preview") or {}
            if not preview:
                raise RuntimeError("Scheduled post has no saved preview payload; recreate the schedule")

            result = publish_preview_payload(preview, job.get("genre") or "")
            published_message_id = result.get("message_id")
            published_link = ""
            target = job.get("target_channel") or TARGET_CHANNELS.get(job.get("genre") or "", "")
            if target and published_message_id:
                published_link = f"https://t.me/{str(target).lstrip('@')}/{published_message_id}"

            mark_result = sheets_post_action("mark_scheduled_post_published", {
                "id": job_id,
                "published_message_id": published_message_id or "",
                "published_at": now.isoformat(),
                "published_link": published_link,
            })
            if mark_result.get("error"):
                raise RuntimeError(mark_result.get("error"))

            confirmation = {
                "chat_id": job.get("source_chat_id"),
                "text": f"✅ Отложенный пост опубликован в {target}.",
            }
            if job.get("thread_id"):
                confirmation["message_thread_id"] = job.get("thread_id")
            if confirmation.get("chat_id"):
                telegram_api_json("sendMessage", confirmation)

        except Exception as e:
            error_text = str(e)
            try:
                sheets_post_action("mark_scheduled_post_error", {
                    "id": job_id,
                    "error": error_text,
                    "failed_at": now.isoformat(),
                })
            except Exception as mark_error:
                print("SCHEDULE MARK ERROR FAILED:", str(mark_error), flush=True)
            print("SCHEDULED PUBLISH ERROR:", {"job": job, "error": error_text}, flush=True)


def autopublish_scheduler_loop():
    print("AUTOPUBLISH SCHEDULER STARTED", {"interval_sec": AUTOPUBLISH_CHECK_INTERVAL_SEC}, flush=True)
    time.sleep(10)
    while True:
        try:
            run_scheduled_posts_once()
        except Exception as e:
            print("AUTOPUBLISH SCHEDULER ERROR:", str(e), flush=True)
        time.sleep(AUTOPUBLISH_CHECK_INTERVAL_SEC)


def start_autopublish_scheduler():
    global autopublish_scheduler_started
    if not AUTOPUBLISH_PREVIEW_ENABLED:
        print("AUTOPUBLISH PREVIEW DISABLED: set AUTOPUBLISH_PREVIEW_ENABLED=1", flush=True)
        return
    with autopublish_scheduler_lock:
        if autopublish_scheduler_started:
            return
        thread = threading.Thread(target=autopublish_scheduler_loop, daemon=True)
        thread.start()
        autopublish_scheduler_started = True


# ======================
# EDITORIAL DRAFT BUTTONS / PENDING LIST / CLEANUP
# ======================
def _message_text_and_entities(message: dict):
    if message.get("text") is not None:
        return message.get("text") or "", message.get("entities") or []
    return message.get("caption") or "", message.get("caption_entities") or []


def _draft_photo_message(message: dict) -> dict:
    if message.get("photo"):
        return message
    replied = message.get("reply_to_message") or {}
    if replied.get("photo"):
        return replied
    return {}


def handle_draft_callback(callback: dict) -> bool:
    data = callback.get("data") or ""
    if data == "draft_copy":
        # Compatibility for old draft messages created before V12.
        telegram_api("answerCallbackQuery", {
            "callback_query_id": callback.get("id"),
            "text": "Кнопка копирования отключена",
            "show_alert": False,
        })
        return True
    if data != "draft_cover":
        return False

    callback_id = callback.get("id")
    message = callback.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    thread_id = message.get("message_thread_id")

    photo_message = _draft_photo_message(message)
    photos = photo_message.get("photo") or []
    if not photos:
        telegram_api("answerCallbackQuery", {"callback_query_id": callback_id, "text": "Не нашёл готовую обложку", "show_alert": True})
        return True

    source_path = ""
    png_path = ""
    try:
        source_path = tg_download_file(photos[-1].get("file_id"))
        with Image.open(source_path) as image:
            ready = image.convert("RGB")
            if ready.size != (1280, 1280):
                ready = ImageOps.fit(ready, (1280, 1280), method=Image.Resampling.LANCZOS)
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
            png_path = tmp.name
            tmp.close()
            ready.save(png_path, "PNG", optimize=True)
        data_payload = {"chat_id": str(chat_id)}
        if thread_id:
            data_payload["message_thread_id"] = str(thread_id)
        with open(png_path, "rb") as doc:
            result = telegram_api_multipart("sendDocument", data_payload, {"document": ("cover_1280.png", doc, "image/png")})
        _schedule_result_cleanup(result)
        telegram_api("answerCallbackQuery", {"callback_query_id": callback_id, "text": "Готовая обложка отправлена файлом", "show_alert": False})
    finally:
        for path in (source_path, png_path):
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass
    return True


def _pending_title(raw_text: str) -> str:
    """
    Show a useful pending-list title: prefer the real "Artist — Track" line
    instead of a teaser/quote or the generic "Публикация".
    """
    raw_text = raw_text or ""

    try:
        cleaned = _clean_publish_lines(raw_text)
        candidates = [
            str(item.get("text") or "").strip()
            for item in cleaned
            if str(item.get("text") or "").strip()
        ]
    except Exception:
        candidates = [line.strip() for line in raw_text.splitlines() if line.strip()]

    # First choice: an explicit release title like "Юля Шаврина - Сожги этот дом".
    for line in candidates:
        if _looks_like_release_title(line):
            return re.sub(r"^💿\s*", "", line).strip()[:180]

    # Fallback: use the first meaningful non-service line.
    for line in candidates:
        low = line.casefold()
        if low.startswith("#"):
            continue
        if low.startswith("@"):
            continue
        if re.match(r"^автор\s*[:\-–—]", line, flags=re.IGNORECASE):
            continue
        if "скачать облож" in low or "удалить перед публикацией" in low:
            continue
        return re.sub(r"^💿\s*", "", line).strip()[:180]

    return "Публикация"


def build_pending_reviews_text(items: List[dict], requested_genre: str = "") -> str:
    if not items:
        suffix = f" жанра {genre_label(requested_genre)}" if requested_genre and requested_genre != "all" else ""
        return f"✅ Невышедших постов{suffix} сейчас нет."
    grouped = {}
    for item in items:
        genre = (item.get("genre") or item.get("channel") or "other").strip() or "other"
        grouped.setdefault(genre, []).append(item)
    parts = ["📚 <b>Невышедшие посты</b>", ""]
    ordered = ["glavred", "rock", "indie", "folk", "pop", "hiphop", "electronica", "underground"]
    keys = [g for g in ordered if g in grouped] + [g for g in grouped if g not in ordered]
    for genre in keys:
        parts.append(f"<b>{html_escape(genre_label(genre))} — {len(grouped[genre])}</b>")
        for item in grouped[genre]:
            title = html_escape(_pending_title(item.get("title") or ""))
            link = item.get("link") or item.get("common_link") or item.get("yandex_link") or ""
            parts.append(f'• <a href="{html_escape(first_link(link))}">{title}</a>' if link else f"• {title}")
        parts.append("")
    value = "\n".join(parts).strip()
    if len(_strip_html_for_length(value)) <= 3900:
        return value
    compact = [parts[0], "", f"Всего: {len(items)}. Список сокращён из-за лимита Telegram.", ""]
    for line in parts[2:]:
        compact.append(line)
        if len(_strip_html_for_length("\n".join(compact))) > 3700:
            compact.pop()
            break
    return "\n".join(compact).strip()


def send_pending_reviews(chat_id, thread_id=None, genre: str = "all"):
    params = {"limit": "200"}
    if genre and genre != "all":
        params["genre"] = genre
    result = sheets_get("get_pending_reviews", params)
    payload = {"chat_id": chat_id, "text": build_pending_reviews_text(result.get("items") or [], genre), "parse_mode": "HTML", "disable_web_page_preview": True}
    if thread_id:
        payload["message_thread_id"] = thread_id
    sent = telegram_api_json("sendMessage", payload)
    _schedule_result_cleanup(sent)
    return sent


def handle_pending_callback(callback: dict) -> bool:
    data = callback.get("data") or ""
    if not data.startswith("pending:"):
        return False
    genre = data.split(":", 1)[1] or "all"
    callback_id = callback.get("id")
    context = callback_message_context(callback)
    telegram_api("answerCallbackQuery", {"callback_query_id": callback_id, "text": "Собираю список невышедших постов", "show_alert": False})
    send_pending_reviews(context.get("chat_id"), context.get("thread_id"), genre)
    return True


def handle_editorial_menu_command(post: dict) -> bool:
    text = get_post_text(post).strip()
    command = text.split()[0].split("@")[0].lower() if text else ""
    if command not in ("/menu", "/pending"):
        return False
    chat_id = get_chat_id(post)
    thread_id = get_thread_id(post)
    if command == "/pending":
        send_pending_reviews(chat_id, thread_id, get_genre_from_thread(thread_id) or "all")
        return True
    payload = {"chat_id": chat_id, "text": "Редакционное меню", "reply_markup": {"inline_keyboard": [[{"text": "📚 Невышедшие посты", "callback_data": "pending:all"}]]}}
    if thread_id:
        payload["message_thread_id"] = thread_id
    result = telegram_api_json("sendMessage", payload)
    _schedule_result_cleanup(result)
    return True


def schedule_bot_message_cleanup(chat_id, message_id):
    if not BOT_MESSAGE_CLEANUP_ENABLED or not chat_id or not message_id:
        return
    try:
        delete_at = datetime.now(get_autopublish_tz()) + timedelta(minutes=BOT_MESSAGE_CLEANUP_AFTER_MINUTES)
        result = sheets_post_action("add_cleanup_task", {"task": {"id": f"{chat_id}:{message_id}", "chat_id": str(chat_id), "message_id": str(message_id), "delete_at": delete_at.isoformat(), "status": "scheduled"}})
        if result.get("error"):
            print("CLEANUP QUEUE ERROR:", result, flush=True)
    except Exception as e:
        print("CLEANUP QUEUE ERROR:", str(e), flush=True)


def run_cleanup_once():
    if not BOT_MESSAGE_CLEANUP_ENABLED:
        return
    try:
        result = sheets_get("get_due_cleanup_tasks", {"limit": "50"})
    except Exception as e:
        print("CLEANUP GET ERROR:", str(e), flush=True)
        return
    for task in result.get("tasks") or []:
        task_id = str(task.get("id") or "")
        error_text = ""
        try:
            telegram_api_json("deleteMessage", {"chat_id": task.get("chat_id"), "message_id": int(task.get("message_id"))})
        except Exception as e:
            error_text = str(e)
            print("CLEANUP DELETE ERROR:", {"task": task, "error": error_text}, flush=True)
        try:
            sheets_post_action("mark_cleanup_done", {"id": task_id, "error": error_text})
        except Exception as e:
            print("CLEANUP MARK ERROR:", str(e), flush=True)


def cleanup_scheduler_loop():
    print("CLEANUP SCHEDULER STARTED", {"interval_sec": BOT_MESSAGE_CLEANUP_CHECK_SEC}, flush=True)
    time.sleep(30)
    while True:
        run_cleanup_once()
        time.sleep(BOT_MESSAGE_CLEANUP_CHECK_SEC)


def start_cleanup_scheduler():
    global cleanup_scheduler_started
    if not BOT_MESSAGE_CLEANUP_ENABLED:
        return
    with cleanup_scheduler_lock:
        if cleanup_scheduler_started:
            return
        thread = threading.Thread(target=cleanup_scheduler_loop, daemon=True)
        thread.start()
        cleanup_scheduler_started = True


# ======================
# REVIEW ENGINE — SAFE ADDITION
# ======================
review_scheduler_started = False
review_scheduler_lock = threading.Lock()


def sheets_get(action: str, params: Optional[dict] = None) -> dict:
    payload = {"action": action}
    if params:
        payload.update(params)

    resp = requests.get(WEBHOOK_URL, params=payload, timeout=60)
    resp.raise_for_status()
    return resp.json()


def sheets_post_action(action: str, payload: Optional[dict] = None) -> dict:
    data = {"action": action}
    if payload:
        data.update(payload)

    resp = requests.post(WEBHOOK_URL, json=data, timeout=60)
    resp.raise_for_status()
    return resp.json()


def build_review_buttons(row_number) -> dict:
    return {
        "inline_keyboard": [
            [
                {
                    "text": "👍 ОБЗОР ЕСТЬ НА КАНАЛЕ",
                    "callback_data": f"review_done:{row_number}",
                }
            ],
            [
                {
                    "text": "👀 ПОКАЗАТЬ ОБЗОР В ГРУППЕ БОТА",
                    "callback_data": f"review_show:{row_number}",
                }
            ],
        ]
    }


def send_review_reminder(reminder: dict):
    chat_id = reminder.get("chat_id") or reminder.get("Chat ID")
    if not chat_id:
        print("REVIEW REMINDER SKIP: no chat_id", reminder, flush=True)
        return

    thread_id = reminder.get("thread_id") or reminder.get("Thread ID")
    source_message_id = reminder.get("message_id") or reminder.get("messageId") or reminder.get("Message ID")
    row_number = reminder.get("row") or reminder.get("row_number")
    title = (reminder.get("title") or reminder.get("Title") or "").strip()
    genre = reminder.get("genre") or reminder.get("Genre") or ""
    source_link = reminder.get("link") or reminder.get("Link") or ""
    days_old = reminder.get("days_old") or reminder.get("daysOld") or "14+"
    yandex_link = reminder.get("yandex_link") or reminder.get("Yandex Link") or ""
    common_link = reminder.get("common_link") or reminder.get("Common Link") or ""

    text_parts = [
        "⚠️ Напоминание по обзору",
        "",
        f"Обзор из раздела «{genre or 'неизвестно'}» пока не найден в публикациях.",
        f"Прошло дней: {days_old}",
        "",
    ]

    if title:
        text_parts.append(title[:1200])
        text_parts.append("")

    if source_link:
        text_parts.append(f"Исходный пост: {source_link}")

    if yandex_link:
        text_parts.append(f"Яндекс: {yandex_link}")

    if common_link:
        text_parts.append(f"Общая ссылка: {common_link}")

    payload = {
        "chat_id": chat_id,
        "text": "\n".join(text_parts),
        "reply_markup": build_review_buttons(row_number),
        "disable_web_page_preview": True,
    }

    if thread_id:
        try:
            payload["message_thread_id"] = int(thread_id)
        except Exception:
            pass

    if source_message_id:
        try:
            payload["reply_to_message_id"] = int(source_message_id)
            payload["allow_sending_without_reply"] = True
        except Exception:
            pass

    result = telegram_api_json("sendMessage", payload)
    _schedule_result_cleanup(result)
    print("REVIEW REMINDER SENT:", {"row": row_number, "chat_id": chat_id, "thread_id": thread_id}, flush=True)



def build_telegram_message_link(chat_id, message_id) -> str:
    if not chat_id or not message_id:
        return ""

    chat_id_str = str(chat_id)
    if chat_id_str.startswith("-100"):
        return f"https://t.me/c/{chat_id_str[4:]}/{message_id}"

    return ""


def extract_digest_title(text: str) -> str:
    """
    Keeps the digest compact: first meaningful line, without hashtag-only noise.
    """
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    for line in lines:
        low = line.lower()
        if low.startswith("#сверхновые"):
            continue
        if low in ("яндекс музыка", "слушать везде"):
            continue
        if "яндекс музыка" in low and "слушать" in low and len(line) < 80:
            continue
        return line[:180]
    return "Публикация"


GENRE_LABELS = {
    "rock": "Рок",
    "indie": "Инди",
    "electronica": "Электроника",
    "folk": "Фолк",
    "pop": "Поп",
    "underground": "Андеграунд",
    "hiphop": "Хип-хоп",
    "glavred": "Главред",
}


def html_escape(value: str) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def genre_label(genre: str) -> str:
    genre_clean = (genre or "").strip()
    return GENRE_LABELS.get(genre_clean, genre_clean.capitalize() if genre_clean else "жанра")


def split_digest_post_text(text: str):
    """
    Returns: (artist_song_title, review_text)

    Expected editorial format:
    Исполнитель — Название
    #сверхновые
    Текст обзора
    Автор: ...
    @editor (удалить перед публикацией)
    скачать обложку

    We keep the actual review text, but remove service/editorial lines:
    hashtags, mentions, author, cover-download links, and music-link labels.
    """
    raw_lines = [line.strip() for line in (text or "").splitlines()]
    lines = [line for line in raw_lines if line]

    title = ""
    body_lines = []

    for line in lines:
        low = line.lower()

        if low.startswith("#"):
            continue
        if low.startswith("@"):
            continue
        if low.startswith("автор:") or low.startswith("автор —") or low.startswith("автор -"):
            continue
        if low.startswith("яндекс:") or low.startswith("общая ссылка:"):
            continue
        if low.startswith("http://") or low.startswith("https://"):
            continue
        if "яндекс музыка" in low and ("слушать" in low or "|" in low):
            continue
        if "скачать облож" in low or "удалить перед публикацией" in low:
            continue

        if not title:
            title = line
        else:
            body_lines.append(line)

    if not title:
        title = extract_digest_title(text)

    review_text = "\n".join(body_lines).strip()
    return title, review_text


def first_link(value: str) -> str:
    if not value:
        return ""
    return str(value).split()[0].strip()


def build_digest_text(digest: dict) -> str:
    genre = digest.get("genre") or digest.get("Genre") or ""
    items = digest.get("items") or []
    disc = genre_emoji_html(genre)
    header_text = f"Дайджест сверхновых жанра {html_escape(genre_label(genre))}"
    header = f"{disc} {header_text}" if disc else header_text
    text_parts = [f"<b>{header}</b>", ""]
    for item in items:
        raw_text = item.get("title") or item.get("Title") or ""
        item_title, review_text = split_digest_post_text(raw_text)
        yandex = first_link(item.get("yandex_link") or item.get("Yandex Link") or "")
        common = first_link(item.get("common_link") or item.get("Common Link") or "")
        link_for_title = common or yandex
        safe_title = html_escape(item_title)
        text_parts.append(f'<b><a href="{html_escape(link_for_title)}">{safe_title}</a></b>' if link_for_title else f"<b>{safe_title}</b>")
        if review_text:
            text_parts.append("")
            review_lines = [line.strip() for line in review_text.splitlines() if line.strip()]
            if review_lines:
                text_parts.append(f"<blockquote>{html_escape(review_lines[0])}</blockquote>")
                if len(review_lines) > 1:
                    text_parts.append("")
                    for line in review_lines[1:]:
                        text_parts.append(f"{liked_section_emoji_html()} {html_escape(line)}" if _line_is_liked_heading(line) else html_escape(line))
        text_parts.append("")
    text_parts.append("#сверхновые")
    return "\n".join(text_parts).strip()[:3900]


def _digest_cover_source(item: dict) -> str:
    file_id = str(item.get("cover_file_id") or item.get("Cover File ID") or "").strip()
    cover_url = str(item.get("cover_url") or item.get("Cover URL") or "").strip()
    if file_id:
        return tg_download_file(file_id)
    if cover_url:
        return download_cover_from_url(cover_url)
    return ""


def _digest_placeholder_image() -> Image.Image:
    return Image.new("RGB", (720, 720), (28, 28, 28))


def _apply_frame_to_image(base: Image.Image, genre: str) -> Image.Image:
    result = base.convert("RGBA")
    frame_path = resolve_frame_path(genre)
    if frame_path:
        with Image.open(frame_path) as frame_source:
            frame = frame_source.convert("RGBA")
            if frame.size != result.size:
                frame = frame.resize(result.size, Image.Resampling.LANCZOS)
            result = Image.alpha_composite(result, frame)
    elif genre in GENRE_FRAME_FILES:
        raise RuntimeError(f"Не найдена рамка для жанра {genre}: {GENRE_FRAME_FILES.get(genre)}")
    return result


def prepare_digest_collage(items: List[dict], genre: str) -> str:
    paths = []
    covers = []
    try:
        for item in (items or [])[:5]:
            try:
                path = _digest_cover_source(item)
                if path:
                    paths.append(path)
                    with Image.open(path) as source:
                        covers.append(source.convert("RGB").copy())
                else:
                    covers.append(_digest_placeholder_image())
            except Exception as e:
                print("DIGEST COVER ERROR:", {"item": item.get("row"), "error": str(e)}, flush=True)
                covers.append(_digest_placeholder_image())
        while len(covers) < 5:
            covers.append(_digest_placeholder_image())
        canvas = Image.new("RGB", (1280, 1280), (20, 20, 20))
        for image, pos in zip(covers[:4], [(0, 0), (580, 0), (0, 580), (580, 580)]):
            canvas.paste(ImageOps.fit(image, (700, 700), method=Image.Resampling.LANCZOS), pos)
        center = ImageOps.fit(covers[4], (650, 650), method=Image.Resampling.LANCZOS)
        center = ImageOps.expand(center, border=10, fill=(245, 245, 245))
        canvas.paste(center, ((1280 - center.width) // 2, (1280 - center.height) // 2))
        final_image = _apply_frame_to_image(canvas, genre)
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
        tmp.close()
        final_image.save(tmp.name, "PNG", optimize=True)
        return tmp.name
    finally:
        for path in paths:
            try:
                os.remove(path)
            except OSError:
                pass


def send_ready_digest(digest: dict):
    chat_id = digest.get("chat_id") or digest.get("Chat ID")
    thread_id = digest.get("thread_id") or digest.get("Thread ID")
    rows = digest.get("rows") or []
    genre = digest.get("genre") or digest.get("Genre") or ""
    items = digest.get("items") or []
    if not chat_id:
        print("DIGEST SKIP: no chat_id", digest, flush=True)
        return
    collage_path = ""
    try:
        collage_path = prepare_digest_collage(items, genre)
        result = _send_draft_photo(chat_id, int(thread_id) if str(thread_id).isdigit() else thread_id, collage_path, build_digest_text(digest), genre)
        message_id = result.get("message_id")
        digest_link = build_telegram_message_link(chat_id, message_id) or "digest_sent"
        mark_result = sheets_post_action("mark_digest_done", {"rows": ",".join([str(r) for r in rows]), "found_in": digest_link})
        print("DIGEST COLLAGE SENT:", {"genre": genre, "chat_id": chat_id, "thread_id": thread_id, "rows": rows, "message_id": message_id, "digest_link": digest_link, "mark_result": mark_result}, flush=True)
    finally:
        if collage_path:
            try:
                os.remove(collage_path)
            except OSError:
                pass



def run_digest_check_once():
    if not DIGEST_ENGINE_ENABLED:
        return

    try:
        result = sheets_get("get_ready_digests", {"size": str(DIGEST_SIZE)})
        print("DIGEST CHECK RESULT:", result, flush=True)

        digests = result.get("digests") or []
        for digest in digests:
            try:
                send_ready_digest(digest)
            except Exception as e:
                print("DIGEST SEND ERROR:", str(e), digest, flush=True)

    except Exception as e:
        print("DIGEST CHECK ERROR:", str(e), flush=True)


def run_review_check_once():
    if not REVIEW_ENGINE_ENABLED:
        return

    try:
        result = sheets_get("check_reviews")
        print("REVIEW CHECK RESULT:", result, flush=True)

        reminders = result.get("reminders") or []
        for reminder in reminders:
            try:
                send_review_reminder(reminder)
            except Exception as e:
                print("REVIEW REMINDER ERROR:", str(e), reminder, flush=True)

    except Exception as e:
        print("REVIEW CHECK ERROR:", str(e), flush=True)


def review_scheduler_loop():
    print("REVIEW SCHEDULER STARTED", {"interval_sec": REVIEW_CHECK_INTERVAL_SEC}, flush=True)

    # First check shortly after boot, then by interval.
    time.sleep(20)

    while True:
        run_review_check_once()
        run_digest_check_once()
        time.sleep(REVIEW_CHECK_INTERVAL_SEC)


def start_review_scheduler():
    global review_scheduler_started

    if not REVIEW_ENGINE_ENABLED and not DIGEST_ENGINE_ENABLED:
        print("REVIEW/DIGEST ENGINE DISABLED: set REVIEW_ENGINE_ENABLED=1 after Apps Script update", flush=True)
        return

    with review_scheduler_lock:
        if review_scheduler_started:
            return

        t = threading.Thread(target=review_scheduler_loop, daemon=True)
        t.start()
        review_scheduler_started = True


def callback_message_context(callback: dict) -> dict:
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    return {
        "chat_id": chat.get("id"),
        "thread_id": message.get("message_thread_id"),
    }


def send_review_card(row_number, context: Optional[dict] = None):
    review = sheets_get("get_review", {"row": str(row_number)})

    if review.get("error"):
        text = f"Не смог найти обзор в таблице: {review.get('error')}"
    else:
        text_parts = [
            "👀 Обзор из Предложки",
            "",
            review.get("title") or "",
        ]

        if review.get("link"):
            text_parts.append("")
            text_parts.append(f"Исходный пост: {review.get('link')}")

        if review.get("yandex_link"):
            text_parts.append(f"Яндекс: {review.get('yandex_link')}")

        if review.get("common_link"):
            text_parts.append(f"Общая ссылка: {review.get('common_link')}")

        text = "\n".join([p for p in text_parts if p is not None])[:3900]

    if not context:
        return

    chat_id = context.get("chat_id")
    if not chat_id:
        return

    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }

    thread_id = context.get("thread_id")
    if thread_id:
        try:
            payload["message_thread_id"] = int(thread_id)
        except Exception:
            pass

    result = telegram_api_json("sendMessage", payload)
    _schedule_result_cleanup(result)


def handle_review_callback(callback: dict):
    data = callback.get("data") or ""
    callback_id = callback.get("id")
    user = callback.get("from") or {}
    username = user.get("username") or user.get("first_name") or str(user.get("id") or "unknown")
    context = callback_message_context(callback)

    if data.startswith("review_done:"):
        row_number = data.split(":", 1)[1]
        result = sheets_post_action("mark_review_done", {
            "row": row_number,
            "found_in": f"manual:{username}",
        })

        telegram_api("answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": "Отметил: обзор есть на канале",
            "show_alert": False,
        })

        if context.get("chat_id"):
            payload = {
                "chat_id": context.get("chat_id"),
                "text": f"✅ Отметил обзор как опубликованный вручную. Строка: {row_number}",
            }
            if context.get("thread_id"):
                try:
                    payload["message_thread_id"] = int(context.get("thread_id"))
                except Exception:
                    pass
            sent = telegram_api_json("sendMessage", payload)
            _schedule_result_cleanup(sent)

        print("REVIEW MANUAL DONE:", {"row": row_number, "user": username, "result": result}, flush=True)
        return True

    if data.startswith("review_show:"):
        row_number = data.split(":", 1)[1]

        telegram_api("answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": "Показываю обзор",
            "show_alert": False,
        })

        send_review_card(row_number, context)
        return True

    return False


@app.get("/editorial-copy/<int:row_number>")
def editorial_copy_page(row_number: int):
    return Response(
        "Копирование текста отключено. Используй текст готового черновика прямо в Telegram.",
        status=410,
        content_type="text/plain; charset=utf-8",
    )


@app.get("/vk_auth")
def vk_auth():
    secret = request.args.get("secret", "")

    if not VK_AUTH_SECRET or secret != VK_AUTH_SECRET:
        return "Forbidden", 403

    if not VK_APP_ID or not VK_APP_SECRET or not VK_REDIRECT_URI:
        return "VK OAuth env vars are not configured", 500

    params = {
        "client_id": VK_APP_ID,
        "display": "page",
        "redirect_uri": VK_REDIRECT_URI,
        "scope": "wall,photos,groups",
        "response_type": "code",
        "v": "5.199",
    }

    url = "https://oauth.vk.com/authorize?" + urlencode(params)

    return (
        '<a href="{0}">Получить VK user token через Render</a>'
        '<br><br>'
        '<b>VK_REDIRECT_URI сейчас:</b><br>'
        '<code>{1}</code>'
        '<br><br>'
        '<b>Ссылка авторизации:</b><br>'
        '<textarea style="width:100%;height:160px">{0}</textarea>'
    ).format(url, VK_REDIRECT_URI), 200


def handle_vk_callback_code():
    error = request.args.get("error")
    error_description = request.args.get("error_description")

    if error:
        return f"VK OAuth error: {error}<br>{error_description}", 400

    code = request.args.get("code", "")

    if not code:
        return "ok", 200

    try:
        resp = requests.get(
            "https://oauth.vk.com/access_token",
            params={
                "client_id": VK_APP_ID,
                "client_secret": VK_APP_SECRET,
                "redirect_uri": VK_REDIRECT_URI,
                "code": code,
            },
            timeout=60
        )
        resp.raise_for_status()
        data = resp.json()

        if "access_token" not in data:
            return f"VK token exchange failed: {data}", 400

        token = data["access_token"]
        user_id = data.get("user_id", "")

        return (
            "<h3>VK user token получен</h3>"
            "<p>Скопируй access_token и вставь его в свой конфиг/Google Sheet вместо старого токена.</p>"
            f"<p><b>user_id:</b> {user_id}</p>"
            f"<textarea style='width:100%;height:220px'>{token}</textarea>"
        ), 200

    except Exception as e:
        return f"VK callback error: {e}", 500


@app.get("/")
def home():
    return handle_vk_callback_code()


@app.post(f"/{BOT_TOKEN}")
def telegram_webhook():
    update = request.get_json(silent=True) or {}

    # Callback buttons for review reminders. Safe for other callbacks.
    if "callback_query" in update:
        callback = update.get("callback_query") or {}
        print(
            "CALLBACK DEBUG:",
            {
                "id": callback.get("id"),
                "data": callback.get("data"),
                "from": (callback.get("from") or {}).get("username"),
            },
            flush=True
        )
        try:
            if handle_draft_callback(callback):
                return "ok", 200
            if handle_pending_callback(callback):
                return "ok", 200
            if handle_publish_callback(callback):
                return "ok", 200
            if handle_review_callback(callback):
                return "ok", 200
        except Exception as e:
            print("CALLBACK ERROR:", str(e), flush=True)
            try:
                telegram_api("answerCallbackQuery", {
                    "callback_query_id": callback.get("id"),
                    "text": "Ошибка обработки кнопки",
                    "show_alert": True,
                })
            except Exception:
                pass
        return "ok", 200

    # Supergroup/topic messages: used for Predlozhka and collecting thread_id.
    # This does not affect the existing channel_post -> Sheets -> VK flow.
    # We process normal and edited messages. Telegram Bot API does not send updates
    # for messages sent by the same bot, and usually does not deliver other bots' messages.
    incoming_message = update.get("message") or update.get("edited_message")
    if incoming_message:
        try:
            debug_custom_emoji_entities(incoming_message)
        except Exception as e:
            print("CUSTOM EMOJI DEBUG ERROR:", str(e), flush=True)

        try:
            if handle_emojipack_command(incoming_message):
                return "ok", 200
        except Exception as e:
            print("EMOJI PACK COMMAND ERROR:", str(e), flush=True)

        try:
            if handle_editorial_menu_command(incoming_message):
                return "ok", 200
        except Exception as e:
            print("EDITORIAL MENU COMMAND ERROR:", str(e), flush=True)

        try:
            cover_attached = False
            if is_allowed_predlozhka_chat(incoming_message) and is_allowed_predlozhka_thread(incoming_message):
                cover_attached = attach_cover_to_recent_sheet_row(incoming_message)
            if not cover_attached:
                send_predlozhka_to_sheets(incoming_message)
        except Exception as e:
            print("MESSAGE HANDLER ERROR:", str(e), flush=True)

        try:
            draft_post = assemble_editorial_draft_post(incoming_message)
            if should_build_publish_preview(draft_post):
                send_publish_preview(draft_post)
        except Exception as e:
            print("AUTOPUBLISH PREVIEW ERROR:", str(e), flush=True)

    if "channel_post" in update:
        post = update["channel_post"]

        debug_post_media(post)
        debug_thread_info(post, "channel_post")

        try:
            send_to_sheets(post)
        except Exception as e:
            print("SHEETS ERROR:", str(e), flush=True)

        try:
            media_group_id = post.get("media_group_id")
            if media_group_id:
                buffer_album_post(post)
            else:
                publish_single_post_to_vk(post)
        except Exception as e:
            print("VK ERROR:", str(e), flush=True)

    return "ok", 200


if __name__ == "__main__":
    start_review_scheduler()
    start_cleanup_scheduler()
    app.run(host="0.0.0.0", port=PORT)

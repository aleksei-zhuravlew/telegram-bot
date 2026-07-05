import os
import re
import json
import time
import mimetypes
import tempfile
import threading
from datetime import datetime
from typing import Dict, List, Optional
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

import requests
from flask import Flask, request

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
    "lnk.to",
    "linkfire",
    "ffm.to",
    "song.link",
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

    low = url.lower()

    if is_yandex_music_link(low):
        return False

    excluded = (
        "t.me/",
        "telegram.me/",
        "vk.com/",
        "youtube.com/",
        "youtu.be/",
        "instagram.com/",
    )
    if any(domain in low for domain in excluded):
        return False

    return any(domain in low for domain in COMMON_LINK_DOMAINS)


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
                "text": (post.get("text") or post.get("caption") or "")[:300],
            },
            flush=True
        )
    except Exception as e:
        print("THREAD DEBUG ERROR:", str(e), flush=True)


def send_predlozhka_to_sheets(post: dict):
    """
    Writes supergroup/topic messages to the Predlozhka sheet.
    Disabled by default for safety until Apps Script is updated.
    """
    debug_thread_info(post, "message")

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
    }

    try:
        resp = requests.post(WEBHOOK_URL, json=data, timeout=60)
        print("PREDLOZHKA SHEETS OK:", resp.status_code, resp.text[:300], flush=True)
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

    telegram_api_json("sendMessage", payload)
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


def build_digest_text(digest: dict) -> str:
    genre = digest.get("genre") or digest.get("Genre") or "жанр"
    items = digest.get("items") or []
    count = digest.get("count") or len(items) or DIGEST_SIZE

    title = f"#сверхновые: {genre}"
    text_parts = [
        title,
        "",
        f"Собрали {count} новых публикаций:",
        "",
    ]

    for idx, item in enumerate(items, start=1):
        item_title = extract_digest_title(item.get("title") or item.get("Title") or "")
        source_link = item.get("link") or item.get("Link") or ""
        yandex = item.get("yandex_link") or item.get("Yandex Link") or ""
        common = item.get("common_link") or item.get("Common Link") or ""

        line = f"{idx}. {item_title}"
        if source_link:
            line += f"\n   Исходник: {source_link}"
        if yandex:
            line += f"\n   Яндекс: {str(yandex).split()[0]}"
        if common:
            line += f"\n   Слушать везде: {str(common).split()[0]}"

        text_parts.append(line)
        text_parts.append("")

    return "\n".join(text_parts).strip()[:3900]


def send_ready_digest(digest: dict):
    chat_id = digest.get("chat_id") or digest.get("Chat ID")
    thread_id = digest.get("thread_id") or digest.get("Thread ID")
    rows = digest.get("rows") or []
    genre = digest.get("genre") or digest.get("Genre") or ""

    if not chat_id:
        print("DIGEST SKIP: no chat_id", digest, flush=True)
        return

    text = build_digest_text(digest)

    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }

    if thread_id:
        try:
            payload["message_thread_id"] = int(thread_id)
        except Exception:
            pass

    result = telegram_api_json("sendMessage", payload)
    message_id = result.get("message_id")
    digest_link = build_telegram_message_link(chat_id, message_id) or "digest_sent"

    mark_result = sheets_post_action("mark_digest_done", {
        "rows": ",".join([str(r) for r in rows]),
        "found_in": digest_link,
    })

    print(
        "DIGEST SENT:",
        {
            "genre": genre,
            "chat_id": chat_id,
            "thread_id": thread_id,
            "rows": rows,
            "message_id": message_id,
            "digest_link": digest_link,
            "mark_result": mark_result,
        },
        flush=True
    )


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

    telegram_api_json("sendMessage", payload)


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
            telegram_api_json("sendMessage", payload)

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
    if "message" in update:
        message = update["message"]
        try:
            send_predlozhka_to_sheets(message)
        except Exception as e:
            print("MESSAGE HANDLER ERROR:", str(e), flush=True)

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
    app.run(host="0.0.0.0", port=PORT)

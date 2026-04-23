import os
import re
import time
import mimetypes
import tempfile
import threading
from typing import Dict, List, Optional

import requests
from flask import Flask, request

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]  # Apps Script URL
PORT = int(os.environ.get("PORT", "10000"))

ALBUM_DELAY_SEC = float(os.environ.get("ALBUM_DELAY_SEC", "3"))
APPEND_SOURCE_LINK = os.environ.get("APPEND_SOURCE_LINK", "1") == "1"

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
        r"Автор\s*[:\-–—]\s*([^\n\r|]+)",
        r"Автор\s+([А-ЯA-ZЁ][^\n\r|]+)",
        r"\(\s*Автор\s*[:\-–—]?\s*([^)]+)\)",
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


def send_to_sheets(post: dict):
    text = post.get("text") or post.get("caption") or ""
    message_id = post.get("message_id")
    channel = (post.get("chat") or {}).get("username", "")
    date_value = post.get("date")
    media_group_id = post.get("media_group_id", "")

    link = f"https://t.me/{channel}/{message_id}" if channel else ""
    author = extract_author(text)

    data = {
        "title": text,
        "channel": channel,
        "date": str(date_value),
        "link": link,
        "media_group_id": str(media_group_id),
        "message_id": str(message_id),
        "author": author
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


@app.get("/")
def home():
    return "ok", 200


@app.post(f"/{BOT_TOKEN}")
def telegram_webhook():
    update = request.get_json(silent=True) or {}

    if "channel_post" in update:
        post = update["channel_post"]

        debug_post_media(post)

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
    app.run(host="0.0.0.0", port=PORT)

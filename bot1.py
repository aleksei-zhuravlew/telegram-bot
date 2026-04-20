import os
import requests
from flask import Flask, request

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]

app = Flask(__name__)

@app.get("/")
def home():
    return "ok", 200

@app.post(f"/{BOT_TOKEN}")
def telegram_webhook():
    update = request.get_json(silent=True) or {}

    if "channel_post" in update:
        post = update["channel_post"]

        text = post.get("text") or post.get("caption") or ""
        message_id = post.get("message_id")
        channel = (post.get("chat") or {}).get("username", "")
        date_value = post.get("date")
        media_group_id = post.get("media_group_id", "")

        link = f"https://t.me/{channel}/{message_id}" if channel else ""

        data = {
            "title": text,
            "channel": channel,
            "date": str(date_value),
            "link": link,
            "media_group_id": str(media_group_id),
            "message_id": str(message_id)
        }

        try:
            requests.post(WEBHOOK_URL, json=data, timeout=20)
        except Exception as e:
            print("SHEETS ERROR:", str(e), flush=True)

    return "ok", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

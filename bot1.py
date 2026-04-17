import os
import requests
from flask import Flask, request

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]

app = Flask(__name__)

def send_to_sheets(data: dict):
    r = requests.post(WEBHOOK_URL, json=data, timeout=15)
    r.raise_for_status()

@app.get("/")
def home():
    return "ok", 200

@app.get("/health")
def health():
    return "ok", 200

@app.post(f"/{BOT_TOKEN}")
def telegram_webhook():
    update = request.get_json(force=True, silent=True) or {}

    channel_post = update.get("channel_post")
    if channel_post:
        text = channel_post.get("text") or channel_post.get("caption") or ""
        message_id = channel_post.get("message_id", "")
        chat = channel_post.get("chat", {}) or {}
        channel = chat.get("username", "")
        date_value = channel_post.get("date", "")

        link = f"https://t.me/{channel}/{message_id}" if channel else ""

        data = {
            "title": text,
            "channel": channel,
            "date": str(date_value),
            "link": link,
        }

        try:
            send_to_sheets(data)
        except Exception as e:
            print("Sheets error:", e)

    return "ok", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

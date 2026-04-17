import os
import requests
import telebot

TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]

bot = telebot.TeleBot(TOKEN)

@bot.channel_post_handler(content_types=[
    'text', 'photo', 'video', 'document', 'audio', 'voice'
])
def handle_channel_post(message):
    text = message.text or message.caption or ""
    channel = message.chat.username or ""
    message_id = message.message_id
    date_str = str(message.date)

    link = f"https://t.me/{channel}/{message_id}" if channel else ""

    data = {
        "title": text,
        "channel": channel,
        "date": date_str,
        "link": link
    }

    try:
        r = requests.post(WEBHOOK_URL, json=data, timeout=15)
        print("OK:", r.status_code, data)
    except Exception as e:
        print("ERROR:", e)

print("Bot is running...")
bot.infinity_polling(skip_pending=True)
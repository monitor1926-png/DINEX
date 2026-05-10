import os
import re
import time
import html
import requests
from flask import Flask, request
from openai import OpenAI

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OWNER_TELEGRAM_ID = int(os.getenv("OWNER_TELEGRAM_ID", "0"))
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

client = OpenAI(api_key=OPENAI_API_KEY)

BOT_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# 防重复、防刷屏
processed_messages = set()
last_user_time = {}

COOLDOWN_SECONDS = 3
MAX_TEXT_LENGTH = 500

VIETNAMESE_CHARS = r"ăâđêôơưáàảãạấầẩẫậắằẳẵặéèẻẽẹếềểễệíìỉĩịóòỏõọốồổỗộớờởỡợúùủũụứừửữựýỳỷỹỵ"

SHORT_WORDS = {
    "ok", "okay", "ừ", "uh", "ừm", "um", "dạ", "vâng",
    "好", "嗯", "啊", "哦", "行", "可以", "收到", "哈哈"
}


def has_chinese(text):
    return re.search(r"[\u4e00-\u9fff]", text) is not None


def has_vietnamese(text):
    text_lower = text.lower()

    # 有越南语特殊字母，基本可以判断是越南语
    if re.search(f"[{VIETNAMESE_CHARS}]", text_lower):
        return True

    # 没有声调时，用常见越南语词辅助判断
    common_vi_words = [
        "toi", "em", "anh", "chi", "ban", "hom nay", "ngay mai",
        "khong", "duoc", "lam", "di", "den", "muon", "som",
        "cam on", "xin loi", "bao cao", "khach", "nhan vien"
    ]

    return any(word in text_lower for word in common_vi_words)


def has_link(text):
    return re.search(r"(https?://|www\.|t\.me/|telegram\.me/)", text.lower()) is not None


def is_too_short(text):
    clean = text.strip().lower()
    if len(clean) <= 1:
        return True
    if clean in SHORT_WORDS:
        return True
    return False


def is_pure_symbol_or_emoji(text):
    # 没有中文、越南语、英文字母、数字，基本就是表情/符号
    return re.search(r"[\u4e00-\u9fffA-Za-z0-9" + VIETNAMESE_CHARS + "]", text.lower()) is None


def should_skip_message(message):
    if "text" not in message:
        return True

    if message.get("from", {}).get("is_bot"):
        return True

    text = message.get("text", "").strip()

    if not text:
        return True

    if len(text) > MAX_TEXT_LENGTH:
        return True

    if has_link(text):
        return True

    if is_too_short(text):
        return True

    if is_pure_symbol_or_emoji(text):
        return True

    return False


def translate_with_openai(text, direction):
    if direction == "zh_to_vi":
        system_prompt = """
你是一个中越团队专用翻译助手。
把中文翻译成自然、口语化、越南员工容易理解的越南语。
只输出越南语，不要解释，不要加引号。
"""
    else:
        system_prompt = """
你是一个中越团队专用翻译助手。
把越南语翻译成自然、准确、老板容易理解的中文。
只输出中文，不要解释，不要加引号。
"""

    response = client.responses.create(
        model=OPENAI_MODEL,
        input=[
            {
                "role": "system",
                "content": system_prompt
            },
            {
                "role": "user",
                "content": text
            }
        ]
    )

    return response.output_text.strip()


def send_reply(chat_id, reply_to_message_id, user_id, user_name, translated_text, direction):
    safe_name = html.escape(user_name or "用户")
    safe_text = html.escape(translated_text)

    if direction == "zh_to_vi":
        flag = "🇻🇳"
    else:
        flag = "🇨🇳"

    mention = f'<a href="tg://user?id={user_id}">@{safe_name}</a>'

    final_text = f"{mention}\n\n{flag} {safe_text}"

    requests.post(
        f"{BOT_API}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": final_text,
            "parse_mode": "HTML",
            "reply_to_message_id": reply_to_message_id,
            "disable_web_page_preview": True
        },
        timeout=10
    )


@app.route("/", methods=["GET"])
def home():
    return "Telegram AI Translator Bot is running."


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}

    message = data.get("message")
    if not message:
        return "ok"

    chat = message.get("chat", {})
    from_user = message.get("from", {})

    chat_id = chat.get("id")
    message_id = message.get("message_id")
    user_id = from_user.get("id")
    user_name = from_user.get("first_name") or from_user.get("username") or "用户"
    text = message.get("text", "").strip()

    unique_key = f"{chat_id}:{message_id}"

    # 防重复翻译
    if unique_key in processed_messages:
        return "ok"

    processed_messages.add(unique_key)

    # 防止内存无限变大
    if len(processed_messages) > 5000:
        processed_messages.clear()

    if should_skip_message(message):
        return "ok"

    # 防刷屏：同一个人 3 秒内只翻译一次
    now = time.time()
    last_time = last_user_time.get(user_id, 0)

    if now - last_time < COOLDOWN_SECONDS:
        return "ok"

    last_user_time[user_id] = now

    direction = None

    # 你发中文 → 越南语
    if user_id == OWNER_TELEGRAM_ID and has_chinese(text):
        direction = "zh_to_vi"

    # 别人发越南语 → 中文
    elif user_id != OWNER_TELEGRAM_ID and has_vietnamese(text):
        direction = "vi_to_zh"

    else:
        return "ok"

    try:
        translated_text = translate_with_openai(text, direction)

        if translated_text:
            send_reply(
                chat_id=chat_id,
                reply_to_message_id=message_id,
                user_id=user_id,
                user_name=user_name,
                translated_text=translated_text,
                direction=direction
            )

    except Exception as e:
        print("Error:", e)

    return "ok"


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
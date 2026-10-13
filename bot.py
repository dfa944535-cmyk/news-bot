import os
import time
import asyncio
import logging

import requests
import feedparser
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("gold_bot")

# ================= الإعدادات =================
# تقرأ من متغيرات البيئة أولاً، وإن لم توجد تستخدم القيمة المكتوبة هنا.
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8864816943:AAGNHI7kVhnxrivYwkd5-aho_rAyXKCXX5s")
MY_CHAT_ID = os.environ.get("MY_CHAT_ID", "1434793059")

FRED_API_KEY = os.environ.get("FRED_API_KEY", "390bce7d6e0ab3119030664deb297d74")
OPENROUTER_API_KEY = os.environ.get(
    "OPENROUTER_API_KEY",
    "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",
)

TG_LIMIT = 4000  # الحد الفعلي لتيليجرام 4096

# ================= دوال جلب البيانات =================

# 1. سعر الذهب (تخزين مؤقت 30 ثانية)
_gold_cache = {"price": None, "time": 0}


def get_gold_price():
    now = time.time()
    if now - _gold_cache["time"] > 30:
        try:
            r = requests.get("https://api.gold-api.com/price/XAU/USD", timeout=10)
            if r.status_code == 200:
                _gold_cache["price"] = r.json().get("price")
                _gold_cache["time"] = now
                log.info("Gold price fetched: %s", _gold_cache["price"])
            else:
                log.warning("Gold API status %s: %s", r.status_code, r.text[:200])
        except Exception as e:
            log.error("Gold fetch error: %s", e)
    return _gold_cache["price"]


# 2. بيانات FRED (تخزين مؤقت 30 دقيقة لأنها بيانات يومية)
_fred_cache = {}
FRED_TTL = 30 * 60


def get_fred_series(series_id):
    now = time.time()
    cached = _fred_cache.get(series_id)
    if cached and now - cached[1] < FRED_TTL:
        return cached[0]

    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 5,  # عدة قراءات لتجاوز القيم الفارغة "."
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code == 200:
            for obs in r.json().get("observations", []):
                if obs["value"] != ".":
                    value = float(obs["value"])
                    _fred_cache[series_id] = (value, now)
                    return value
        else:
            log.warning("FRED %s status %s: %s", series_id, r.status_code, r.text[:200])
    except Exception as e:
        log.error("FRED %s error: %s", series_id, e)
    return cached[0] if cached else None


# 3. أخبار ترامب
def get_trump_news():
    try:
        url = "https://news.google.com/rss/search?q=Trump&hl=en-US&gl=US&ceid=US:en"
        feed = feedparser.parse(url)
        items = [f"- {e.title}\n{e.link}" for e in feed.entries[:3]]
        return "\n\n".join(items) if items else None
    except Exception as e:
        log.error("Trump news error: %s", e)
        return None


# 4. التحليل بالذكاء الاصطناعي
def get_ai_analysis(gold, dxy, vix, trump_news):
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }

    news_context = f"\n\nأخبار ترامب الأخيرة:\n{trump_news}" if trump_news else ""

    prompt = f"""
البيانات الاقتصادية الحالية:
- سعر الذهب (XAU/USD): {gold} دولار
- مؤشر الدولار الواسع (FRED DTWEXBGS): {dxy}
- مؤشر الخوف (VIX): {vix}
{news_context}

حلل هذه البيانات باللغة العربية بأسلوب احترافي ومختصر.
اشرح العلاقة بين قوة الدولار ومستوى الخوف وسعر الذهب.
إذا كانت هناك أخبار لترامب، اشرح تأثيرها المحتمل على الأسواق.
اذكر الاتجاه المحتمل للذهب بشكل عام دون أرقام محددة للأهداف أو الدخول والخروج.
علماً أن بيانات الدولار و VIX يومية وقد تتأخر يوماً.
استخدم نصاً بسيطاً بدون رموز تنسيق مثل النجمة أو الشرطة السفلية.
"""

    data = {
        "model": "deepseek/deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
    }

    try:
        r = requests.post(url, headers=headers, json=data, timeout=40)
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"]
        log.warning("OpenRouter status %s: %s", r.status_code, r.text[:200])
        return f"حدث خطأ في الاتصال بالذكاء الاصطناعي (كود {r.status_code})."
    except Exception as e:
        log.error("OpenRouter error: %s", e)
        return f"حدث خطأ أثناء التحليل: {e}"


# ================= أدوات مساعدة =================

def is_owner(update: Update) -> bool:
    return str(update.effective_chat.id) == str(MY_CHAT_ID)


def split_text(text, limit=TG_LIMIT):
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        parts.append(text)
    return parts


# ================= أوامر البوت =================

async def get_my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"معرف المحادثة الخاص بك هو:\n{update.effective_chat.id}"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "مرحباً! 👋\n"
        "أنا مساعدك الاقتصادي الشخصي.\n"
        "أرسل /analyze للحصول على تحليل للذهب والدولار والخوف وأخبار ترامب.\n"
        "أو أرسل /myid لمعرفة معرف المحادثة الخاص بك."
    )


async def analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await update.message.reply_text("⛔ هذا الأمر متاح لصاحب البوت فقط.")
        return

    await update.message.reply_text("⏳ جاري جمع البيانات وتحليلها... يرجى الانتظار.")

    # الدوال الحاجبة تعمل في خيوط منفصلة حتى لا يتجمد البوت
    gold, dxy, vix, trump_news = await asyncio.gather(
        asyncio.to_thread(get_gold_price),
        asyncio.to_thread(get_fred_series, "DTWEXBGS"),
        asyncio.to_thread(get_fred_series, "VIXCLS"),
        asyncio.to_thread(get_trump_news),
    )

    if gold is None or dxy is None or vix is None:
        await update.message.reply_text("❌ فشل في جلب البيانات. حاول مرة أخرى لاحقاً.")
        return

    analysis = await asyncio.to_thread(get_ai_analysis, gold, dxy, vix, trump_news)

    msg = (
        "📊 تقرير السوق\n\n"
        f"🥇 الذهب: {gold} $\n"
        f"💵 مؤشر الدولار الواسع (FRED): {dxy}\n"
        f"😱 مؤشر الخوف (VIX): {vix}\n"
        "(بيانات الدولار و VIX يومية وقد تتأخر يوماً)\n\n"
        f"🤖 تحليل الذكاء الاصطناعي:\n{analysis}"
    )
    if trump_news:
        msg += f"\n\n🗞️ آخر أخبار ترامب:\n{trump_news}"
    msg += "\n\n⚠️ هذا التحليل للمعلومات فقط وليس نصيحة استثمارية."

    try:
        for part in split_text(msg):
            await context.bot.send_message(
                chat_id=MY_CHAT_ID,
                text=part,
                disable_web_page_preview=True,
            )
    except Exception as e:
        log.error("Send error: %s", e)
        await update.message.reply_text(f"حدث خطأ أثناء إرسال التقرير: {e}")


# ================= تشغيل البوت =================

if __name__ == "__main__":
    log.info("البوت يعمل الآن...")
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("analyze", analyze))
    application.add_handler(CommandHandler("myid", get_my_id))

    application.run_polling()

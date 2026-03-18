import os
import base64
import json
import asyncio
import logging
from datetime import date
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

BRANDS = ["Бренд 1", "Бренд 2", "Бренд 3"]
CRITERIA_KEYS = ["facing", "pos", "clean", "oos_score"]
CRITERIA_NAMES = ["Фейсинг / выкладка", "Ценники / POS", "Чистота и порядок", "Наличие (OOS)"]
WEIGHTS = {"facing": 2, "pos": 1.5, "clean": 1, "oos_score": 2}

sessions = {}

def get_session(uid):
    if uid not in sessions:
        sessions[uid] = {"outlet": "", "square": "", "auditor": "", "brand": None, "audits": [], "state": "idle"}
    return sessions[uid]

def calc_pct(scores, oos):
    t, m = 0, 0
    for k, w in WEIGHTS.items():
        v = 0 if (k == "oos_score" and oos) else scores.get(k, 0)
        t += v * w
        m += 5 * w
    return round(t / m * 100) if m else 0

def grade(p):
    return "Хорошо" if p >= 80 else "Удовл." if p >= 60 else "Плохо"

def grade_emoji(p):
    return "🟢" if p >= 80 else "🟡" if p >= 60 else "🔴"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = get_session(update.effective_user.id)
    s["state"] = "idle"
    kb = [[InlineKeyboardButton("📋 Новый аудит", callback_data="new_audit")]]
    if s["audits"]:
        kb.append([InlineKeyboardButton("📊 Показать отчёт", callback_data="show_report")])
        kb.append([InlineKeyboardButton("🗑 Очистить", callback_data="clear")])
    await update.message.reply_text(
        "👋 *Аудит полки — показательные квадраты*\n\nОтправь фото полки и я оценю выставленность автоматически.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = get_session(q.from_user.id)
    d = q.data

    if d == "new_audit":
        s["state"] = "ask_outlet"
        await q.message.reply_text("🏪 Введи название торговой точки:")
    elif d == "show_report":
        await send_report(q.message, s)
    elif d == "clear":
        s["audits"] = []
        await q.message.reply_text("✅ История очищена.")
    elif d.startswith("brand_"):
        idx = int(d.split("_")[1])
        s["brand"] = BRANDS[idx]
        s["state"] = "wait_photo"
        await q.message.reply_text(
            f"📸 Бренд: *{BRANDS[idx]}*\n\nОтправь фото полки — анализирую автоматически!",
            parse_mode="Markdown"
        )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = get_session(update.effective_user.id)
    text = update.message.text.strip()

    if s["state"] == "ask_outlet":
        s["outlet"] = text
        s["state"] = "ask_square"
        await update.message.reply_text("📍 Введи номер квадрата:")
    elif s["state"] == "ask_square":
        s["square"] = text
        s["state"] = "ask_auditor"
        await update.message.reply_text("👤 Введи ФИО ТП / Мерчандайзера:")
    elif s["state"] == "ask_auditor":
        s["auditor"] = text
        s["state"] = "ask_brand"
        kb = [[InlineKeyboardButton(b, callback_data=f"brand_{i}")] for i, b in enumerate(BRANDS)]
        await update.message.reply_text("🏷 Выбери бренд:", reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.message.reply_text("Напиши /start чтобы начать аудит.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = get_session(update.effective_user.id)
    if s["state"] != "wait_photo":
        await update.message.reply_text("Сначала начни аудит — /start")
        return

    await update.message.reply_text("🔍 Анализирую фото полки...")

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    photo_bytes = await file.download_as_bytearray()
    photo_b64 = base64.b64encode(photo_bytes).decode()

    result = await analyze_shelf(photo_b64, s["brand"])

    if result:
        s["audits"].append({
            "outlet": s["outlet"],
            "square": s["square"],
            "auditor": s["auditor"],
            "brand": s["brand"],
            **result
        })

        scores = result["scores"]
        oos = result["oos"]
        total = result["total"]
        g = result["grade"]
        notes = result["notes"]
        em = grade_emoji(total)

        lines = [f"*{s['outlet']}  ·  кв. {s['square']}*", f"Бренд: *{s['brand']}*", ""]
        for i, key in enumerate(CRITERIA_KEYS):
            val = "OOS ❌" if (key == "oos_score" and oos) else f"{scores.get(key, 0)}/5"
            note = notes.get(key, "")
            line = f"• {CRITERIA_NAMES[i]}: *{val}*"
            if note:
                line += f"\n  _{note}_"
            lines.append(line)

        lines += ["", f"{em} *Итог: {total}% — {g}*"]

        kb = [
            [InlineKeyboardButton("📸 Ещё один бренд", callback_data="new_audit")],
            [InlineKeyboardButton("📊 Показать отчёт", callback_data="show_report")]
        ]
        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )
    else:
        await update.message.reply_text("❌ Не удалось проанализировать. Попробуй ещё раз.")

async def analyze_shelf(photo_b64: str, brand: str):
    prompt = f"""Ты эксперт по мерчандайзингу. Проанализируй фото полки для бренда "{brand}".

Оцени от 1 до 5:
- facing: фейсинг/выкладка (блок, количество фейсингов, видимость)
- pos: ценники/POS материалы (наличие, правильность)
- clean: чистота и порядок полки
- oos_score: наличие товара на полке

Определи oos (true если товар отсутствует) и краткие замечания notes.

Верни ТОЛЬКО JSON:
{{"scores":{{"facing":4,"pos":3,"clean":5,"oos_score":4}},"oos":false,"notes":{{"facing":"","pos":"нет шелфтокера","clean":"","oos_score":""}}}}"""

    try:
        async with httpx.AsyncClient(timeout=40) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-opus-4-6",
                    "max_tokens": 500,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": photo_b64}},
                            {"type": "text", "text": prompt}
                        ]
                    }]
                }
            )
        data = resp.json()
        text = data["content"][0]["text"].strip()
        if "```" in text:
            text = text.split("```")[1].replace("json", "").strip()
        result = json.loads(text)
        scores = result["scores"]
        oos = result.get("oos", False)
        total = calc_pct(scores, oos)
        return {"scores": scores, "oos": oos, "notes": result.get("notes", {}), "total": total, "grade": grade(total)}
    except Exception as e:
        logger.error(f"Analysis error: {e}")
        return None

async def send_report(message, s):
    if not s["audits"]:
        await message.reply_text("Нет данных. Сначала сохрани аудит.")
        return

    groups = {}
    for a in s["audits"]:
        k = a["outlet"] + "|" + a["square"]
        if k not in groups:
            groups[k] = {"outlet": a["outlet"], "square": a["square"], "auditor": a["auditor"], "entries": []}
        groups[k]["entries"].append(a)

    lines = [f"📋 *ОТЧЁТ ПО АУДИТУ ПОЛКИ*", f"_{date.today().strftime('%d.%m.%Y')}_", ""]
    for g in groups.values():
        avg = round(sum(e["total"] for e in g["entries"]) / len(g["entries"]))
        lines.append(f"🏪 *{g['outlet']}  ·  кв. {g['square']}*")
        if g["auditor"]:
            lines.append(f"👤 {g['auditor']}")
        for e in g["entries"]:
            em = grade_emoji(e["total"])
            lines.append(f"  {em} {e['brand']}: {e['total']}% — {e['grade']}")
        lines.append(f"  Средний: {grade_emoji(avg)} *{avg}%*")
        lines.append("")

    await message.reply_text("\n".join(lines), parse_mode="Markdown")

def main():
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN not set!")
        return
    logger.info("Starting bot...")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    logger.info("Bot is running!")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
if __name__ == "__main__":
    main()

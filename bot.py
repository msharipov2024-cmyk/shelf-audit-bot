import os
import base64
import httpx
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

BRANDS = ["Бренд 1", "Бренд 2", "Бренд 3"]
CRITERIA = ["Фейсинг / выкладка", "Ценники / POS", "Чистота и порядок", "Наличие (OOS)"]

user_sessions = {}

def get_session(user_id):
    if user_id not in user_sessions:
        user_sessions[user_id] = {
            "outlet": "",
            "square": "",
            "auditor": "",
            "brand": None,
            "audits": [],
            "state": "idle"
        }
    return user_sessions[user_id]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_user.id)
    session["state"] = "idle"
    keyboard = [[InlineKeyboardButton("📋 Новый аудит", callback_data="new_audit")]]
    if session["audits"]:
        keyboard.append([InlineKeyboardButton("📊 Показать отчёт", callback_data="show_report")])
        keyboard.append([InlineKeyboardButton("🗑 Очистить историю", callback_data="clear")])
    await update.message.reply_text(
        "👋 *Аудит полки — показательные квадраты*\n\nОтправь фото полки и я автоматически оценю выставленность по всем критериям.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session = get_session(query.from_user.id)
    data = query.data

    if data == "new_audit":
        session["state"] = "ask_outlet"
        await query.message.reply_text("🏪 Введи название торговой точки:")

    elif data == "show_report":
        await send_report(query.message, session)

    elif data == "clear":
        session["audits"] = []
        await query.message.reply_text("✅ История очищена.")

    elif data.startswith("brand_"):
        idx = int(data.split("_")[1])
        session["brand"] = BRANDS[idx]
        session["state"] = "wait_photo"
        await query.message.reply_text(
            f"📸 Отлично! Бренд: *{BRANDS[idx]}*\n\nТеперь отправь фото полки — я проанализирую выставленность.",
            parse_mode="Markdown"
        )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_user.id)
    text = update.message.text.strip()
    state = session["state"]

    if state == "ask_outlet":
        session["outlet"] = text
        session["state"] = "ask_square"
        await update.message.reply_text("📍 Введи номер квадрата:")

    elif state == "ask_square":
        session["square"] = text
        session["state"] = "ask_auditor"
        await update.message.reply_text("👤 Введи ФИО ТП / Мерчандайзера:")

    elif state == "ask_auditor":
        session["auditor"] = text
        session["state"] = "ask_brand"
        keyboard = [[InlineKeyboardButton(b, callback_data=f"brand_{i}")] for i, b in enumerate(BRANDS)]
        await update.message.reply_text(
            "🏷 Выбери бренд:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text("Отправь /start чтобы начать аудит.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_user.id)

    if session["state"] != "wait_photo":
        await update.message.reply_text("Сначала начни аудит — /start")
        return

    await update.message.reply_text("🔍 Анализирую фото полки...")

    # Download photo
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    photo_bytes = await file.download_as_bytearray()
    photo_b64 = base64.b64encode(photo_bytes).decode()

    # Analyze with Claude
    result = await analyze_shelf(photo_b64, session["brand"])

    if result:
        session["audits"].append({
            "outlet": session["outlet"],
            "square": session["square"],
            "auditor": session["auditor"],
            "brand": session["brand"],
            **result
        })

        scores = result["scores"]
        oos = result["oos"]
        total = result["total"]
        grade = result["grade"]
        notes = result["notes"]

        grade_emoji = "🟢" if grade == "Хорошо" else "🟡" if grade == "Удовл." else "🔴"

        lines = [f"*{session['outlet']}  ·  кв. {session['square']}*", f"Бренд: {session['brand']}", ""]
        for i, crit in enumerate(CRITERIA):
            key = ["facing", "pos", "clean", "oos_score"][i]
            val = "OOS ❌" if (i == 3 and oos) else f"{scores.get(key, 0)}/5"
            note = notes.get(key, "")
            lines.append(f"• {crit}: *{val}*" + (f"\n  _{note}_" if note else ""))

        lines += ["", f"{grade_emoji} *Итог: {total}% — {grade}*"]

        keyboard = [
            [InlineKeyboardButton("📸 Ещё фото (другой бренд)", callback_data="new_audit")],
            [InlineKeyboardButton("📊 Показать отчёт", callback_data="show_report")]
        ]
        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text("❌ Не удалось проанализировать фото. Попробуй ещё раз.")

async def analyze_shelf(photo_b64: str, brand: str) -> dict:
    prompt = f"""Ты эксперт по мерчандайзингу. Проанализируй фото полки в магазине для бренда "{brand}".

Оцени по каждому критерию от 1 до 5:
1. facing — Фейсинг / выкладка (соблюдение блока, количество фейсингов, видимость)
2. pos — Ценники / POS материалы (наличие и правильность ценников, шелфтокеры)
3. clean — Чистота и порядок (чистота полки, ровность выкладки)
4. oos_score — Наличие товара (есть ли товар на полке)

Также определи:
- oos (boolean): true если товар отсутствует (OOS)
- notes для каждого критерия: краткое замечание если есть проблема (или пустая строка)

Верни ТОЛЬКО JSON без пояснений:
{{
  "scores": {{"facing": 4, "pos": 3, "clean": 5, "oos_score": 4}},
  "oos": false,
  "notes": {{"facing": "", "pos": "нет шелфтокера", "clean": "", "oos_score": ""}}
}}"""

    try:
        async with httpx.AsyncClient(timeout=30) as client:
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
        # Clean JSON
        if "```" in text:
            text = text.split("```")[1].replace("json", "").strip()
        import json
        result = json.loads(text)
        scores = result["scores"]
        oos = result.get("oos", False)
        weights = {"facing": 2, "pos": 1.5, "clean": 1, "oos_score": 2}
        t, m = 0, 0
        for k, w in weights.items():
            v = 0 if (k == "oos_score" and oos) else scores.get(k, 0)
            t += v * w; m += 5 * w
        total = round(t / m * 100) if m else 0
        grade = "Хорошо" if total >= 80 else "Удовл." if total >= 60 else "Плохо"
        return {"scores": scores, "oos": oos, "notes": result.get("notes", {}), "total": total, "grade": grade}
    except Exception as e:
        print(f"Analysis error: {e}")
        return None

async def send_report(message, session):
    if not session["audits"]:
        await message.reply_text("Нет данных для отчёта.")
        return
    from datetime import date
    lines = [f"📋 *ОТЧЁТ ПО АУДИТУ ПОЛКИ*", f"_{date.today().strftime('%d.%m.%Y')}_", ""]
    groups = {}
    for a in session["audits"]:
        k = a["outlet"] + "|" + a["square"]
        if k not in groups:
            groups[k] = {"outlet": a["outlet"], "square": a["square"], "auditor": a["auditor"], "entries": []}
        groups[k]["entries"].append(a)
    for g in groups.values():
        avg = round(sum(e["total"] for e in g["entries"]) / len(g["entries"]))
        ge = "🟢" if avg >= 80 else "🟡" if avg >= 60 else "🔴"
        lines.append(f"🏪 *{g['outlet']}  ·  кв. {g['square']}*")
        lines.append(f"👤 {g['auditor']}")
        for e in g["entries"]:
            em = "🟢" if e["total"] >= 80 else "🟡" if e["total"] >= 60 else "🔴"
            lines.append(f"  {em} {e['brand']}: {e['total']}% — {e['grade']}")
        lines.append(f"  Средний: {ge} *{avg}%*")
        lines.append("")
    await message.reply_text("\n".join(lines), parse_mode="Markdown")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    print("Bot started...")
    app.run_polling()

if __name__ == "__main__":
    main()

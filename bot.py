import os
import base64
import json
import logging
import threading
import io
import urllib.request
from datetime import date, datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import httpx

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ID руководителя — установить через /setmanager
MANAGER_IDS = set()

BRANDS = [
    "SOF", "Power SOFT Plus", "ALMIR", "Comfort Baby",
    "SOF Premium", "Amiri", "Makfa", "Konti", "Олейна", "Kent"
]

# Нормативы фейсингов по брендам
FACING_NORMS = {
    "SOF": 4, "Power SOFT Plus": 3, "ALMIR": 3, "Comfort Baby": 3,
    "SOF Premium": 3, "Amiri": 2, "Makfa": 4, "Konti": 3,
    "Олейна": 3, "Kent": 2
}

COMPANY_INFO = """
Компания производит бренды:
- SOF, SOF Premium — стиральные порошки, кондиционеры (норматив: 4 фейса)
- Power SOFT Plus — средства для стирки (норматив: 3 фейса)
- ALMIR — бытовая химия (норматив: 3 фейса)
- Comfort Baby — детские товары (норматив: 3 фейса)
- Amiri — товары для дома (норматив: 2 фейса)
- Makfa — макароны, мука (норматив: 4 фейса)
- Konti — кондитерские изделия (норматив: 3 фейса)
- Олейна — растительное масло (норматив: 3 фейса)
- Kent — гигиена (норматив: 2 фейса)
"""

sessions = {}
_fonts_registered = False

def ensure_fonts():
    global _fonts_registered
    if _fonts_registered:
        return
    try:
        font_path = "/tmp/DejaVuSans.ttf"
        font_bold = "/tmp/DejaVuSans-Bold.ttf"
        if not os.path.exists(font_path):
            urllib.request.urlretrieve(
                "https://github.com/dejavu-fonts/dejavu-fonts/raw/master/ttf/DejaVuSans.ttf",
                font_path
            )
        if not os.path.exists(font_bold):
            urllib.request.urlretrieve(
                "https://github.com/dejavu-fonts/dejavu-fonts/raw/master/ttf/DejaVuSans-Bold.ttf",
                font_bold
            )
        pdfmetrics.registerFont(TTFont("DejaVu", font_path))
        pdfmetrics.registerFont(TTFont("DejaVu-Bold", font_bold))
        _fonts_registered = True
        logger.info("Fonts loaded OK")
    except Exception as e:
        logger.error(f"Font error: {e}")

def get_session(uid):
    if uid not in sessions:
        sessions[uid] = {
            "outlet": "", "square": "", "auditor": "",
            "brand": None, "audits": [], "state": "idle",
            "chat_history": [], "before_photo": None,
            "user_id": uid, "username": ""
        }
    return sessions[uid]

def calc_pct(scores, oos):
    weights = {"facing": 2.5, "pos": 1.5, "clean": 1.0, "oos_score": 2.5, "competitors": 1.5}
    t, m = 0, 0
    for k, w in weights.items():
        v = 0 if (k == "oos_score" and oos) else scores.get(k, 0)
        t += v * w
        m += 5 * w
    return round(t / m * 100) if m else 0

def grade(p):
    return "Отлично" if p >= 90 else "Хорошо" if p >= 80 else "Удовл." if p >= 60 else "Плохо"

def grade_emoji(p):
    return "🟢" if p >= 80 else "🟡" if p >= 60 else "🔴"

def check_facing_norm(brand, facings):
    norm = FACING_NORMS.get(brand, 3)
    total_facings = sum(facings.values()) if facings else 0
    return total_facings, norm, total_facings >= norm


def run_health_server():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        def log_message(self, *args):
            pass
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), H).serve_forever()


# ── /start ─────────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = get_session(update.effective_user.id)
    s["state"] = "idle"
    s["username"] = update.effective_user.first_name or "Мерчандайзер"

    kb = [
        [InlineKeyboardButton("🚀 Начать аудит", callback_data="new_audit")],
    ]
    if s["audits"]:
        kb.append([
            InlineKeyboardButton("📊 Отчёт", callback_data="show_report"),
            InlineKeyboardButton("📄 PDF", callback_data="pdf_report")
        ])
        kb.append([InlineKeyboardButton("🏆 Рейтинг", callback_data="show_rating")])
        kb.append([InlineKeyboardButton("🗑 Очистить", callback_data="clear")])

    kb.append([InlineKeyboardButton("🤖 Спросить ИИ", callback_data="ask_ai")])
    kb.append([InlineKeyboardButton("📋 Нормативы фейсингов", callback_data="show_norms")])

    name = update.effective_user.first_name or "Привет"
    await update.message.reply_text(
        f"👋 Добро пожаловать, *{name}*!\n\n"
        f"Я — *Shelf Audit Bot* 🛒\n\n"
        f"📸 Аудит полки с анализом ИИ\n"
        f"📊 Фото ДО и ПОСЛЕ выкладки\n"
        f"📦 Контроль нормативов фейсингов\n"
        f"🏆 Рейтинг мерчандайзеров\n"
        f"📄 PDF-отчёт руководителю\n"
        f"🤖 ИИ-ассистент по мерчандайзингу\n\n"
        f"Нажми кнопку ниже 👇",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def setmanager_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    MANAGER_IDS.add(update.effective_user.id)
    await update.message.reply_text(
        f"✅ Вы установлены как *руководитель*.\n"
        f"Теперь вы будете получать сводные отчёты от всех мерчандайзеров.\n"
        f"Ваш ID: `{update.effective_user.id}`",
        parse_mode="Markdown"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Команды:*\n\n"
        "/start — главное меню\n"
        "/setmanager — стать руководителем (получать отчёты)\n"
        "/norms — нормативы фейсингов\n"
        "/rating — рейтинг мерчандайзеров\n"
        "/help — помощь\n\n"
        "💬 Просто напиши вопрос — ИИ ответит!",
        parse_mode="Markdown"
    )

async def norms_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["📋 *Нормативы фейсингов:*\n"]
    for brand, norm in FACING_NORMS.items():
        lines.append(f"  • {brand}: *{norm}* фейса минимум")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def rating_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = get_session(update.effective_user.id)
    await send_rating(update.message, s)


# ── Кнопки ─────────────────────────────────────────────────────────────────────
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = get_session(q.from_user.id)
    s["username"] = q.from_user.first_name or "Мерчандайзер"
    d = q.data

    if d == "new_audit":
        s["state"] = "ask_outlet"
        s["before_photo"] = None
        await q.message.reply_text("🏪 Введи название торговой точки:")

    elif d == "show_report":
        await send_report(q.message, s)

    elif d == "pdf_report":
        await send_pdf_report(q.message, s, context)

    elif d == "show_rating":
        await send_rating(q.message, s)

    elif d == "show_norms":
        lines = ["📋 *Нормативы фейсингов:*\n"]
        for brand, norm in FACING_NORMS.items():
            lines.append(f"  • {brand}: *{norm}* фейса минимум")
        await q.message.reply_text("\n".join(lines), parse_mode="Markdown")

    elif d == "clear":
        s["audits"] = []
        s["chat_history"] = []
        await q.message.reply_text("✅ История очищена.")

    elif d == "ask_ai":
        s["state"] = "ai_chat"
        await q.message.reply_text(
            "🤖 *ИИ-ассистент активирован*\n\n"
            "Задай вопрос по мерчандайзингу или брендам.\n\n"
            "_Примеры:_\n"
            "• Как правильно выставить SOF?\n"
            "• Норматив фейсингов для Makfa?\n"
            "• Проанализируй мои аудиты\n"
            "• Что делать если OOS?\n\n"
            "/start — вернуться в меню",
            parse_mode="Markdown"
        )

    elif d == "skip_before":
        s["before_photo"] = None
        s["state"] = "wait_photo"
        await q.message.reply_text(
            f"📸 Бренд: *{s['brand']}*\nОтправь фото полки ПОСЛЕ выкладки:",
            parse_mode="Markdown"
        )

    elif d.startswith("brand_"):
        idx = int(d.split("_")[1])
        s["brand"] = BRANDS[idx]
        norm = FACING_NORMS.get(BRANDS[idx], 3)
        s["state"] = "wait_before_photo"
        await q.message.reply_text(
            f"🏷 Бренд: *{BRANDS[idx]}*\n"
            f"📋 Норматив: *{norm} фейсов* минимум\n\n"
            f"📸 Сначала отправь фото полки *ДО* выкладки\n"
            f"_(или пропусти если не нужно)_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⏭ Пропустить фото ДО", callback_data="skip_before")
            ]])
        )


# ── Текст ──────────────────────────────────────────────────────────────────────
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = get_session(update.effective_user.id)
    s["username"] = update.effective_user.first_name or "Мерчандайзер"
    text = update.message.text.strip()

    if s["state"] == "ask_outlet":
        s["outlet"] = text
        s["state"] = "ask_square"
        await update.message.reply_text("📍 Введи номер квадрата:")

    elif s["state"] == "ask_square":
        s["square"] = text
        s["state"] = "ask_auditor"
        await update.message.reply_text("👤 Введи ФИО мерчандайзера:")

    elif s["state"] == "ask_auditor":
        s["auditor"] = text
        s["state"] = "ask_brand"
        kb = []
        row = []
        for i, b in enumerate(BRANDS):
            row.append(InlineKeyboardButton(b, callback_data=f"brand_{i}"))
            if len(row) == 2:
                kb.append(row)
                row = []
        if row:
            kb.append(row)
        await update.message.reply_text("🏷 Выбери бренд:", reply_markup=InlineKeyboardMarkup(kb))

    elif s["state"] == "ai_chat":
        await handle_ai_chat(update, s, text)

    else:
        await handle_ai_chat(update, s, text)


# ── ИИ чат ─────────────────────────────────────────────────────────────────────
async def handle_ai_chat(update: Update, s: dict, user_text: str):
    msg = await update.message.reply_text("🤖 Думаю...")

    audit_context = ""
    if s["audits"]:
        audit_context = "\n\nПоследние аудиты:\n"
        for a in s["audits"][-5:]:
            total_f = sum(a.get("facings", {}).values())
            norm = FACING_NORMS.get(a["brand"], 3)
            norm_status = "✅" if total_f >= norm else "❌"
            audit_context += (f"- {a['brand']} в {a['outlet']}: {a['total']}% ({a['grade']}), "
                              f"фейсингов: {total_f}/{norm} {norm_status}\n")

    s["chat_history"].append({"role": "user", "content": user_text})
    if len(s["chat_history"]) > 12:
        s["chat_history"] = s["chat_history"][-12:]

    system_prompt = f"""Ты — эксперт по мерчандайзингу и trade marketing.
{COMPANY_INFO}
{audit_context}
Отвечай на русском. Будь конкретным и практичным. 
Если спрашивают про аудиты — анализируй данные выше.
Упоминай нормативы фейсингов когда уместно."""

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_API_KEY,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": "claude-opus-4-6", "max_tokens": 800,
                      "system": system_prompt, "messages": s["chat_history"]}
            )
        answer = resp.json()["content"][0]["text"].strip()
        s["chat_history"].append({"role": "assistant", "content": answer})

        kb = [[InlineKeyboardButton("🚀 Начать аудит", callback_data="new_audit"),
               InlineKeyboardButton("📊 Отчёт", callback_data="show_report")]]
        await msg.delete()
        await update.message.reply_text(f"🤖 {answer}",
                                        reply_markup=InlineKeyboardMarkup(kb))
    except Exception as e:
        logger.error(f"AI chat error: {e}")
        await msg.delete()
        await update.message.reply_text("❌ Ошибка ИИ. Попробуй ещё раз.")


# ── Фото ───────────────────────────────────────────────────────────────────────
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = get_session(update.effective_user.id)
    s["username"] = update.effective_user.first_name or "Мерчандайзер"

    # Фото ДО
    if s["state"] == "wait_before_photo":
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        photo_bytes = await file.download_as_bytearray()
        s["before_photo"] = base64.b64encode(photo_bytes).decode()
        s["state"] = "wait_photo"
        await update.message.reply_text(
            "✅ Фото ДО сохранено!\n\n"
            f"📸 Теперь выложи товар и отправь фото *ПОСЛЕ* выкладки:",
            parse_mode="Markdown"
        )
        return

    # Фото ПОСЛЕ (основной аудит)
    if s["state"] != "wait_photo":
        await update.message.reply_text(
            "Сначала начни аудит — /start",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🚀 Начать аудит", callback_data="new_audit")
            ]])
        )
        return

    msg = await update.message.reply_text("🔍 Анализирую фото полки... ~15 сек")

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    photo_bytes = await file.download_as_bytearray()
    photo_b64 = base64.b64encode(photo_bytes).decode()

    prev_recs = [r for a in s["audits"][-3:]
                 if a.get("brand") == s["brand"]
                 for r in a.get("recommendations", [])]

    result = await analyze_shelf(photo_b64, s["brand"], prev_recs, s.get("before_photo"))

    if result:
        audit_entry = {
            "outlet": s["outlet"], "square": s["square"],
            "auditor": s["auditor"], "brand": s["brand"],
            "datetime": datetime.now().strftime("%d.%m.%Y %H:%M"),
            "has_before": s["before_photo"] is not None,
            **result
        }
        s["audits"].append(audit_entry)

        em = grade_emoji(result["total"])
        norm = FACING_NORMS.get(s["brand"], 3)
        total_facings = sum(result.get("facings", {}).values())
        norm_ok = total_facings >= norm

        lines = [f"*{s['outlet']} · кв.{s['square']}*", f"Бренд: *{s['brand']}*", ""]

        # Норматив фейсингов
        norm_icon = "✅" if norm_ok else "❌"
        lines.append(f"📋 Норматив фейсингов: {norm_icon} {total_facings}/{norm}")
        lines.append("")

        if result.get("facings"):
            lines.append("📦 *Фейсинги по SKU:*")
            for sku, cnt in result["facings"].items():
                icon = "✅" if cnt >= 2 else "⚠️"
                lines.append(f"  {icon} {sku}: {cnt} фейс.")
            lines.append("")

        # Сравнение ДО/ПОСЛЕ
        if s.get("before_photo") and result.get("improvement"):
            lines.append(f"📊 *Улучшение после выкладки:*")
            lines.append(f"  {result['improvement']}")
            lines.append("")

        sc = result["scores"]
        lines.append("📊 *Оценки:*")
        lines.append(f"  • Фейсинг: {sc.get('facing',0)}/5")
        lines.append(f"  • Ценники/POS: {sc.get('pos',0)}/5")
        lines.append(f"  • Чистота: {sc.get('clean',0)}/5")
        lines.append(f"  • Наличие: {'OOS ❌' if result['oos'] else str(sc.get('oos_score',0))+'/5'}")
        lines.append(f"  • vs Конкуренты: {sc.get('competitors',0)}/5")

        if result.get("competitors_found"):
            lines.append("")
            lines.append("🥊 *Конкуренты:*")
            for c in result["competitors_found"]:
                lines.append(f"  • {c}")

        if result.get("recommendations"):
            lines.append("")
            lines.append("💡 *Рекомендации:*")
            for i, r in enumerate(result["recommendations"], 1):
                lines.append(f"  {i}. {r}")

        lines += ["", f"{em} *Итог: {result['total']}% — {result['grade']}*"]

        # Предупреждение о норме
        if not norm_ok:
            lines.append(f"\n⚠️ _Норматив не выполнен! Нужно {norm} фейсов, есть {total_facings}_")

        kb = [
            [InlineKeyboardButton("📸 Ещё бренд", callback_data="new_audit")],
            [InlineKeyboardButton("📊 Отчёт", callback_data="show_report"),
             InlineKeyboardButton("📄 PDF", callback_data="pdf_report")],
            [InlineKeyboardButton("🏆 Рейтинг", callback_data="show_rating"),
             InlineKeyboardButton("🤖 Спросить ИИ", callback_data="ask_ai")]
        ]
        await msg.delete()
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown",
                                        reply_markup=InlineKeyboardMarkup(kb))

        # Отправить отчёт руководителю автоматически
        if MANAGER_IDS:
            await notify_manager(context, s["username"], audit_entry)

        s["before_photo"] = None

    else:
        await msg.delete()
        await update.message.reply_text("❌ Не удалось проанализировать. Попробуй ещё раз.")


# ── Уведомление руководителю ───────────────────────────────────────────────────
async def notify_manager(context, username: str, audit: dict):
    em = grade_emoji(audit["total"])
    norm = FACING_NORMS.get(audit["brand"], 3)
    total_f = sum(audit.get("facings", {}).values())
    norm_icon = "✅" if total_f >= norm else "❌"

    text = (
        f"📬 *Новый аудит от {username}*\n\n"
        f"🏪 {audit['outlet']} · кв.{audit['square']}\n"
        f"👤 {audit['auditor']}\n"
        f"🏷 Бренд: {audit['brand']}\n"
        f"📋 Фейсинги: {norm_icon} {total_f}/{norm}\n"
        f"{em} Итог: *{audit['total']}% — {audit['grade']}*\n"
        f"🕐 {audit.get('datetime','')}"
    )
    if audit.get("has_before"):
        text += "\n📸 _Аудит с фото ДО/ПОСЛЕ_"

    for mgr_id in MANAGER_IDS:
        try:
            await context.bot.send_message(mgr_id, text, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Manager notify error: {e}")


# ── Анализ полки ───────────────────────────────────────────────────────────────
async def analyze_shelf(photo_b64: str, brand: str,
                        prev_recommendations: list = None,
                        before_photo_b64: str = None):
    norm = FACING_NORMS.get(brand, 3)
    prev_str = ""
    if prev_recommendations:
        prev_str = f"\nDO NOT repeat these previous recommendations: {'; '.join(prev_recommendations[:5])}"

    before_str = ""
    if before_photo_b64:
        before_str = '\nAlso compare with the BEFORE photo provided and describe the improvement in "improvement" field.'

    prompt = f"""Analyze shelf photo for brand "{brand}" (minimum facing norm: {norm}).
Return ONLY valid JSON:{before_str}

{{"scores":{{"facing":4,"pos":3,"clean":4,"oos_score":4,"competitors":3}},"oos":false,"facings":{{"SKU name":3}},"competitors_found":["Brand: N facings"],"recommendations":["tip 1","tip 2","tip 3"],"improvement":"Before: X facings, After: Y facings. Shelf share improved by Z%"}}

Rules:
- scores 1-5 each criterion
- oos: true if brand product missing
- facings: actual SKU names and counts visible
- competitors_found: competitor brands with counts
- recommendations: 3 UNIQUE tips in Russian, specific to what you see{prev_str}
- improvement: only if comparing before/after, otherwise omit"""

    try:
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": photo_b64}},
            {"type": "text", "text": prompt}
        ]
        if before_photo_b64:
            content.insert(0, {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": before_photo_b64}})
            content.insert(1, {"type": "text", "text": "This is the BEFORE photo:"})
            content.insert(2, {"type": "text", "text": "This is the AFTER photo:"})

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_API_KEY,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": "claude-opus-4-6", "max_tokens": 1500,
                      "messages": [{"role": "user", "content": content}]}
            )
        text = resp.json()["content"][0]["text"].strip()
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            text = text[start:end]
        result = json.loads(text)
        scores = result.get("scores", {})
        oos = result.get("oos", False)
        total = calc_pct(scores, oos)
        return {
            "scores": scores, "oos": oos,
            "facings": result.get("facings", {}),
            "competitors_found": result.get("competitors_found", []),
            "recommendations": result.get("recommendations", []),
            "improvement": result.get("improvement", ""),
            "total": total, "grade": grade(total)
        }
    except Exception as e:
        logger.error(f"Analysis error: {e}")
        return None


# ── Рейтинг мерчандайзеров ─────────────────────────────────────────────────────
async def send_rating(message, s):
    # Собираем рейтинг из всех сессий
    auditor_stats = {}
    for uid, sess in sessions.items():
        for a in sess.get("audits", []):
            auditor = a.get("auditor", "Неизвестно")
            if auditor not in auditor_stats:
                auditor_stats[auditor] = {"totals": [], "norms_ok": 0, "total_audits": 0}
            auditor_stats[auditor]["totals"].append(a["total"])
            auditor_stats[auditor]["total_audits"] += 1
            total_f = sum(a.get("facings", {}).values())
            norm = FACING_NORMS.get(a.get("brand", ""), 3)
            if total_f >= norm:
                auditor_stats[auditor]["norms_ok"] += 1

    if not auditor_stats:
        await message.reply_text("Нет данных для рейтинга.")
        return

    # Сортируем по среднему баллу
    ranking = sorted(
        auditor_stats.items(),
        key=lambda x: sum(x[1]["totals"]) / len(x[1]["totals"]),
        reverse=True
    )

    lines = [f"🏆 *РЕЙТИНГ МЕРЧАНДАЙЗЕРОВ*", f"_{date.today().strftime('%d.%m.%Y')}_", ""]
    medals = ["🥇", "🥈", "🥉"]

    for i, (auditor, stats) in enumerate(ranking):
        avg = round(sum(stats["totals"]) / len(stats["totals"]))
        norm_pct = round(stats["norms_ok"] / stats["total_audits"] * 100) if stats["total_audits"] > 0 else 0
        medal = medals[i] if i < 3 else f"{i+1}."
        em = grade_emoji(avg)
        lines.append(f"{medal} *{auditor}*")
        lines.append(f"   {em} Средний балл: *{avg}%*")
        lines.append(f"   📋 Норматив выполнен: *{norm_pct}%* аудитов")
        lines.append(f"   📊 Всего аудитов: {stats['total_audits']}")
        lines.append("")

    await message.reply_text("\n".join(lines), parse_mode="Markdown",
                              reply_markup=InlineKeyboardMarkup([[
                                  InlineKeyboardButton("📄 PDF с рейтингом", callback_data="pdf_report")
                              ]]))


# ── Отчёт ──────────────────────────────────────────────────────────────────────
async def send_report(message, s):
    if not s["audits"]:
        await message.reply_text(
            "Нет данных. Сначала проведи аудит.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🚀 Начать аудит", callback_data="new_audit")
            ]])
        )
        return

    lines = [f"📋 *ОТЧЁТ*  |  _{date.today().strftime('%d.%m.%Y')}_", ""]
    groups = {}
    for a in s["audits"]:
        k = f"{a['outlet']}|{a['square']}"
        if k not in groups:
            groups[k] = {"outlet": a["outlet"], "square": a["square"],
                         "auditor": a["auditor"], "entries": []}
        groups[k]["entries"].append(a)

    for g in groups.values():
        avg = round(sum(e["total"] for e in g["entries"]) / len(g["entries"]))
        lines.append(f"🏪 *{g['outlet']} · кв.{g['square']}*")
        lines.append(f"👤 {g['auditor']}")
        for e in g["entries"]:
            total_f = sum(e.get("facings", {}).values())
            norm = FACING_NORMS.get(e["brand"], 3)
            norm_icon = "✅" if total_f >= norm else "❌"
            before_icon = "📸" if e.get("has_before") else ""
            lines.append(f"  {grade_emoji(e['total'])} {e['brand']} {before_icon}: "
                         f"{e['total']}% | фейсов: {norm_icon}{total_f}/{norm}")
            if e.get("recommendations"):
                lines.append(f"  _💡 {e['recommendations'][0]}_")
        lines.append(f"  📊 Средний: *{avg}%*")
        lines.append("")

    all_t = [e["total"] for g in groups.values() for e in g["entries"]]
    overall = round(sum(all_t) / len(all_t))
    norms_ok = sum(1 for a in s["audits"]
                   if sum(a.get("facings",{}).values()) >= FACING_NORMS.get(a["brand"],3))
    norms_pct = round(norms_ok / len(s["audits"]) * 100)

    lines.append(f"🏆 *ИТОГ: {grade_emoji(overall)} {overall}%*")
    lines.append(f"📋 Норматив выполнен: *{norms_pct}%* аудитов")

    kb = [
        [InlineKeyboardButton("📄 PDF", callback_data="pdf_report"),
         InlineKeyboardButton("🏆 Рейтинг", callback_data="show_rating")]
    ]
    await message.reply_text("\n".join(lines), parse_mode="Markdown",
                              reply_markup=InlineKeyboardMarkup(kb))


# ── PDF ────────────────────────────────────────────────────────────────────────
async def send_pdf_report(message, s, context=None):
    if not s["audits"]:
        await message.reply_text("Нет данных для PDF.")
        return
    await message.reply_text("📄 Генерирую PDF...")
    try:
        ensure_fonts()
        pdf_bytes = generate_pdf(s)
        bio = io.BytesIO(pdf_bytes)
        bio.name = f"audit_{date.today().strftime('%Y%m%d')}.pdf"
        bio.seek(0)
        await message.reply_document(document=bio, filename=bio.name,
                                     caption=f"📄 Аудит полки — {date.today().strftime('%d.%m.%Y')}")
        # Отправить PDF руководителю
        if MANAGER_IDS and context:
            bio.seek(0)
            for mgr_id in MANAGER_IDS:
                try:
                    await context.bot.send_document(
                        mgr_id, document=bio,
                        filename=bio.name,
                        caption=f"📄 Отчёт от {s.get('username','Мерчандайзер')} — {date.today().strftime('%d.%m.%Y')}"
                    )
                    bio.seek(0)
                except Exception as e:
                    logger.error(f"PDF to manager error: {e}")
    except Exception as e:
        logger.error(f"PDF error: {e}")
        await message.reply_text("❌ Ошибка PDF.")


def generate_pdf(s):
    try:
        pdfmetrics.getFont("DejaVu")
        F = "DejaVu"
        FB = "DejaVu-Bold"
    except Exception:
        F = "Helvetica"
        FB = "Helvetica-Bold"

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4,
                            rightMargin=1.5*cm, leftMargin=1.5*cm,
                            topMargin=2*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet()
    primary = colors.HexColor("#1a1a2e")
    accent  = colors.HexColor("#4361ee")
    green   = colors.HexColor("#2dc653")
    yellow  = colors.HexColor("#f4a261")
    red     = colors.HexColor("#e63946")
    light   = colors.HexColor("#f8f9fa")
    mid     = colors.HexColor("#dee2e6")
    gray    = colors.HexColor("#6c757d")
    orange  = colors.HexColor("#fd7e14")

    def P(txt, size=10, bold=False, color=None, align=0):
        return Paragraph(str(txt), ParagraphStyle(
            "x", parent=styles["Normal"], fontSize=size,
            fontName=FB if bold else F,
            textColor=color or primary,
            spaceAfter=2, alignment=align, leading=size*1.4
        ))

    story = []
    story.append(P("ОТЧЕТ ПО АУДИТУ ПОЛКИ", 18, True, primary))
    story.append(P(f"Shelf Audit Report  |  {date.today().strftime('%d.%m.%Y')}", 11, False, gray))
    story.append(HRFlowable(width="100%", thickness=2, color=accent, spaceAfter=12))

    all_t = [a["total"] for a in s["audits"]]
    overall = round(sum(all_t)/len(all_t)) if all_t else 0
    norms_ok = sum(1 for a in s["audits"]
                   if sum(a.get("facings",{}).values()) >= FACING_NORMS.get(a["brand"],3))
    norms_pct = round(norms_ok / len(s["audits"]) * 100) if s["audits"] else 0
    before_count = sum(1 for a in s["audits"] if a.get("has_before"))

    summ = [
        [P("Показатель",10,True,colors.white), P("Значение",10,True,colors.white)],
        [P("Всего аудитов",10), P(str(len(s["audits"])),10)],
        [P("Торговых точек",10), P(str(len(set(a["outlet"] for a in s["audits"]))),10)],
        [P("Брендов проверено",10), P(str(len(set(a["brand"] for a in s["audits"]))),10)],
        [P("Норматив выполнен",10), P(f"{norms_pct}% аудитов",10,True)],
        [P("Аудитов с фото ДО/ПОСЛЕ",10), P(str(before_count),10)],
        [P("Средний балл",10), P(f"{overall}%  —  {grade(overall)}",10,True)],
    ]
    st = Table(summ, colWidths=[8*cm,9*cm])
    st.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),accent),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,light]),
        ("GRID",(0,0),(-1,-1),0.5,mid),
        ("PADDING",(0,0),(-1,-1),8),
        ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
    ]))
    story.append(st)
    story.append(Spacer(1,16))

    # Нормативы
    story.append(P("КОНТРОЛЬ НОРМАТИВОВ ФЕЙСИНГОВ",13,True))
    story.append(HRFlowable(width="100%",thickness=1,color=mid,spaceAfter=6))
    norm_rows = [[P("Бренд",9,True,colors.white), P("Норматив",9,True,colors.white),
                  P("Факт",9,True,colors.white), P("Статус",9,True,colors.white)]]
    for a in s["audits"]:
        total_f = sum(a.get("facings",{}).values())
        norm = FACING_NORMS.get(a["brand"],3)
        ok = total_f >= norm
        norm_rows.append([
            P(a["brand"],9), P(str(norm),9),
            P(str(total_f),9,True), P("Выполнен" if ok else "НЕ выполнен",9,True,
                                       green if ok else red)
        ])
    nt = Table(norm_rows, colWidths=[6*cm,3*cm,3*cm,5*cm])
    nt.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),primary),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,light]),
        ("GRID",(0,0),(-1,-1),0.5,mid),
        ("ALIGN",(1,0),(-1,-1),"CENTER"),
        ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ("PADDING",(0,0),(-1,-1),6),
    ]))
    story.append(nt)
    story.append(Spacer(1,16))

    story.append(P("ДЕТАЛИ АУДИТОВ",13,True))
    story.append(HRFlowable(width="100%",thickness=1,color=mid,spaceAfter=8))

    sc_col = lambda p: green if p>=80 else yellow if p>=60 else red

    for i, a in enumerate(s["audits"],1):
        before_tag = "  [ДО/ПОСЛЕ]" if a.get("has_before") else ""
        ht = Table([[P(f"#{i}  {a['brand']}{before_tag}",11,True,colors.white),
                     P(f"{a['total']}%  {grade(a['total'])}",11,True,colors.white,2)]],
                   colWidths=[10*cm,7*cm])
        ht.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),sc_col(a["total"])),
                                 ("PADDING",(0,0),(-1,-1),8)]))
        story.append(ht)

        it = Table([[P(f"Точка: {a['outlet']}  |  Кв: {a['square']}",9),
                     P(f"Мерч: {a['auditor']}  |  {a.get('datetime','')}",9,False,gray)]],
                   colWidths=[9*cm,8*cm])
        it.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),light),
                                 ("GRID",(0,0),(-1,-1),0.5,mid),
                                 ("PADDING",(0,0),(-1,-1),6)]))
        story.append(it)

        # Норматив в деталях
        total_f = sum(a.get("facings",{}).values())
        norm = FACING_NORMS.get(a["brand"],3)
        norm_ok = total_f >= norm
        story.append(P(f"Фейсинги: {total_f}/{norm} — {'Норматив ВЫПОЛНЕН' if norm_ok else 'Норматив НЕ ВЫПОЛНЕН'}",
                       9, True, green if norm_ok else red))

        sc = a["scores"]
        rows = [[P("Критерий",9,True,colors.white),
                 P("Балл",9,True,colors.white),
                 P("Замечание",9,True,colors.white)]]
        for key, name in [("facing","Фейсинг"),("pos","Ценники/POS"),
                           ("clean","Чистота"),("oos_score","Наличие"),
                           ("competitors","vs Конкуренты")]:
            val = "OOS" if (key=="oos_score" and a["oos"]) else f"{sc.get(key,0)}/5"
            rows.append([P(name,9), P(val,9,True), P("—",9)])
        dt = Table(rows, colWidths=[5*cm,2.5*cm,9.5*cm])
        dt.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0),primary),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,light]),
            ("GRID",(0,0),(-1,-1),0.5,mid),
            ("PADDING",(0,0),(-1,-1),6),
            ("ALIGN",(1,0),(1,-1),"CENTER"),
            ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ]))
        story.append(dt)

        if a.get("improvement"):
            story.append(Spacer(1,4))
            story.append(P(f"До/После: {a['improvement']}",9,False,orange))
        if a.get("competitors_found"):
            story.append(P("Конкуренты: " + " | ".join(a["competitors_found"]),9,False,gray))
        if a.get("recommendations"):
            for ri,r in enumerate(a["recommendations"],1):
                story.append(P(f"{ri}. {r}",9,False,accent))
        story.append(Spacer(1,14))

    # Рейтинг в PDF
    story.append(HRFlowable(width="100%",thickness=1,color=mid,spaceAfter=8))
    story.append(P("РЕЙТИНГ МЕРЧАНДАЙЗЕРОВ",13,True))
    story.append(Spacer(1,6))

    auditor_stats = {}
    for a in s["audits"]:
        aud = a.get("auditor","?")
        auditor_stats.setdefault(aud,{"totals":[],"norms_ok":0,"total":0})
        auditor_stats[aud]["totals"].append(a["total"])
        auditor_stats[aud]["total"] += 1
        tf = sum(a.get("facings",{}).values())
        if tf >= FACING_NORMS.get(a.get("brand",""),3):
            auditor_stats[aud]["norms_ok"] += 1

    ranking = sorted(auditor_stats.items(),
                     key=lambda x: sum(x[1]["totals"])/len(x[1]["totals"]), reverse=True)

    rrows = [[P("Место",9,True,colors.white), P("Мерчандайзер",9,True,colors.white),
              P("Средний балл",9,True,colors.white), P("Норматив",9,True,colors.white),
              P("Аудитов",9,True,colors.white)]]
    for rank,(aud,stats) in enumerate(ranking,1):
        avg = round(sum(stats["totals"])/len(stats["totals"]))
        np = round(stats["norms_ok"]/stats["total"]*100)
        rrows.append([P(str(rank),9,True), P(aud,9), P(f"{avg}%",9,True),
                      P(f"{np}%",9), P(str(stats["total"]),9)])

    rt = Table(rrows, colWidths=[2*cm,5*cm,4*cm,3*cm,3*cm])
    rt.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),accent),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,light]),
        ("GRID",(0,0),(-1,-1),0.5,mid),
        ("ALIGN",(0,0),(0,-1),"CENTER"),
        ("ALIGN",(2,0),(-1,-1),"CENTER"),
        ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ("PADDING",(0,0),(-1,-1),7),
    ]))
    story.append(rt)
    story.append(Spacer(1,12))
    story.append(HRFlowable(width="100%",thickness=1,color=mid,spaceAfter=6))
    story.append(P(f"Shelf Audit Bot  |  {datetime.now().strftime('%d.%m.%Y %H:%M')}",8,False,gray))

    doc.build(story)
    return buffer.getvalue()


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN not set!")
        return

    threading.Thread(target=run_health_server, daemon=True).start()
    logger.info("Health server started")
    ensure_fonts()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("setmanager", setmanager_cmd))
    app.add_handler(CommandHandler("norms", norms_cmd))
    app.add_handler(CommandHandler("rating", rating_cmd))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    logger.info("Bot is running!")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

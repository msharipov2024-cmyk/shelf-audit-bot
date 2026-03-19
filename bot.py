import os
import base64
import json
import logging
import threading
import io
import urllib.request
from datetime import date, datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
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

BRANDS = [
    "SOF", "Power SOFT Plus", "ALMIR", "Comfort Baby",
    "SOF Premium", "Amiri", "Makfa", "Konti", "Олейна", "Kent"
]

COMPANY_INFO = """
Компания производит следующие бренды:
- SOF, SOF Premium — бытовая химия (стиральные порошки, кондиционеры)
- Power SOFT Plus — средства для стирки и ухода за тканями
- ALMIR — бытовая химия
- Comfort Baby — детские товары (подгузники, детская химия)
- Amiri — товары для дома
- Makfa — продукты питания (макароны, мука)
- Konti — кондитерские изделия
- Олейна — растительное масло
- Kent — товары для дома/гигиена
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
            "chat_history": []
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
    name = update.effective_user.first_name or "Привет"

    kb = [
        [InlineKeyboardButton("🚀 Начать аудит", callback_data="new_audit")],
    ]
    if s["audits"]:
        kb.append([
            InlineKeyboardButton("📊 Отчёт", callback_data="show_report"),
            InlineKeyboardButton("📄 PDF", callback_data="pdf_report")
        ])
        kb.append([InlineKeyboardButton("🗑 Очистить историю", callback_data="clear")])

    kb.append([InlineKeyboardButton("🤖 Задать вопрос ИИ", callback_data="ask_ai")])

    await update.message.reply_text(
        f"👋 Добро пожаловать, *{name}*!\n\n"
        f"Я — *Shelf Audit Bot* 🛒\n\n"
        f"Помогаю мерчандайзерам проводить аудит полки:\n"
        f"📸 Фотографируй → получай анализ\n"
        f"📦 Считаю фейсинги по SKU\n"
        f"🥊 Сравниваю с конкурентами\n"
        f"💡 Даю уникальные рекомендации\n"
        f"📄 Генерирую PDF-отчёт\n"
        f"🤖 Отвечаю на вопросы по мерчандайзингу\n\n"
        f"Нажми кнопку ниже чтобы начать 👇",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Как пользоваться ботом:*\n\n"
        "1. /start — главное меню\n"
        "2. Нажми *Начать аудит*\n"
        "3. Введи название точки, квадрат, ФИО\n"
        "4. Выбери бренд\n"
        "5. Отправь фото полки\n"
        "6. Получи детальный анализ!\n\n"
        "💬 Можешь также задать любой вопрос по мерчандайзингу прямо в чат.",
        parse_mode="Markdown"
    )


# ── Кнопки ─────────────────────────────────────────────────────────────────────
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
    elif d == "pdf_report":
        await send_pdf_report(q.message, s)
    elif d == "clear":
        s["audits"] = []
        s["chat_history"] = []
        await q.message.reply_text("✅ История очищена.")
    elif d == "ask_ai":
        s["state"] = "ai_chat"
        await q.message.reply_text(
            "🤖 *ИИ-ассистент активирован*\n\n"
            "Задай любой вопрос по мерчандайзингу, брендам или результатам аудитов.\n\n"
            "_Примеры:_\n"
            "• Как правильно выставить SOF на полке?\n"
            "• Почему важен блок-выкладка?\n"
            "• Какой норматив фейсингов для Makfa?\n"
            "• Проанализируй мои последние аудиты\n\n"
            "Для возврата в меню напиши /start",
            parse_mode="Markdown"
        )
    elif d.startswith("brand_"):
        idx = int(d.split("_")[1])
        s["brand"] = BRANDS[idx]
        s["state"] = "wait_photo"
        await q.message.reply_text(
            f"📸 Бренд: *{BRANDS[idx]}*\n\nОтправь фото полки:",
            parse_mode="Markdown"
        )


# ── Текст ──────────────────────────────────────────────────────────────────────
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
        # ИИ-ассистент отвечает на вопрос
        await handle_ai_chat(update, s, text)

    else:
        # Если не в режиме аудита — всё равно отвечает как ИИ
        await handle_ai_chat(update, s, text)


async def handle_ai_chat(update: Update, s: dict, user_text: str):
    msg = await update.message.reply_text("🤖 Думаю...")

    # Собираем контекст аудитов
    audit_context = ""
    if s["audits"]:
        audit_context = "\n\nПоследние аудиты пользователя:\n"
        for a in s["audits"][-5:]:
            audit_context += f"- {a['brand']} в {a['outlet']}: {a['total']}% ({a['grade']})\n"
            if a.get("recommendations"):
                audit_context += f"  Рекомендации: {'; '.join(a['recommendations'][:2])}\n"

    # История чата (последние 6 сообщений)
    s["chat_history"].append({"role": "user", "content": user_text})
    if len(s["chat_history"]) > 12:
        s["chat_history"] = s["chat_history"][-12:]

    system_prompt = f"""Ты — опытный эксперт по мерчандайзингу и trade marketing. 
Ты помогаешь мерчандайзерам и торговым представителям компании.

{COMPANY_INFO}
{audit_context}

Отвечай на русском языке. Будь конкретным и практичным.
Давай actionable советы. Если спрашивают про аудиты — анализируй данные выше.
Ответ должен быть кратким (3-5 предложений) если вопрос простой, 
или развёрнутым если вопрос сложный."""

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
                    "max_tokens": 800,
                    "system": system_prompt,
                    "messages": s["chat_history"]
                }
            )
        data = resp.json()
        answer = data["content"][0]["text"].strip()
        s["chat_history"].append({"role": "assistant", "content": answer})

        kb = [[
            InlineKeyboardButton("🚀 Начать аудит", callback_data="new_audit"),
            InlineKeyboardButton("📊 Отчёт", callback_data="show_report")
        ]]
        await msg.delete()
        await update.message.reply_text(
            f"🤖 {answer}",
            reply_markup=InlineKeyboardMarkup(kb)
        )
    except Exception as e:
        logger.error(f"AI chat error: {e}")
        await msg.delete()
        await update.message.reply_text("❌ Ошибка ИИ. Попробуй ещё раз.")


# ── Фото ───────────────────────────────────────────────────────────────────────
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = get_session(update.effective_user.id)
    if s["state"] != "wait_photo":
        await update.message.reply_text(
            "Сначала начни аудит — нажми /start",
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

    # Передаём историю предыдущих аудитов для уникальных рекомендаций
    prev_recs = []
    for a in s["audits"][-3:]:
        if a.get("brand") == s["brand"] and a.get("recommendations"):
            prev_recs.extend(a["recommendations"])

    result = await analyze_shelf(photo_b64, s["brand"], prev_recs)

    if result:
        s["audits"].append({
            "outlet": s["outlet"], "square": s["square"],
            "auditor": s["auditor"], "brand": s["brand"],
            "datetime": datetime.now().strftime("%d.%m.%Y %H:%M"),
            **result
        })

        em = grade_emoji(result["total"])
        lines = [f"*{s['outlet']} · кв.{s['square']}*", f"Бренд: *{s['brand']}*", ""]

        if result.get("facings"):
            lines.append("📦 *Фейсинги:*")
            for sku, cnt in result["facings"].items():
                icon = "✅" if cnt >= 2 else "⚠️"
                lines.append(f"  {icon} {sku}: {cnt} фейс.")
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

        kb = [
            [InlineKeyboardButton("📸 Ещё бренд", callback_data="new_audit")],
            [InlineKeyboardButton("📊 Отчёт", callback_data="show_report"),
             InlineKeyboardButton("📄 PDF", callback_data="pdf_report")],
            [InlineKeyboardButton("🤖 Спросить ИИ", callback_data="ask_ai")]
        ]
        await msg.delete()
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown",
                                        reply_markup=InlineKeyboardMarkup(kb))
    else:
        await msg.delete()
        await update.message.reply_text(
            "❌ Не удалось проанализировать фото. Попробуй ещё раз.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Попробовать снова", callback_data=f"brand_{BRANDS.index(s['brand']) if s['brand'] in BRANDS else 0}")
            ]])
        )


# ── Анализ полки ───────────────────────────────────────────────────────────────
async def analyze_shelf(photo_b64: str, brand: str, prev_recommendations: list = None):
    prev_str = ""
    if prev_recommendations:
        prev_str = f"\nПредыдущие рекомендации (НЕ повторяй их): {'; '.join(prev_recommendations[:5])}"

    prompt = f"""Analyze shelf photo for brand "{brand}". Return ONLY valid JSON:

{{"scores":{{"facing":4,"pos":3,"clean":4,"oos_score":4,"competitors":3}},"oos":false,"facings":{{"Product 1":3,"Product 2":2}},"competitors_found":["Competitor A: 4 facings"],"recommendations":["Rec 1","Rec 2","Rec 3"]}}

Rules:
- scores 1-5: facing=block display, pos=price tags/POS, clean=cleanliness, oos_score=stock, competitors=shelf share
- oos: true if out of stock
- facings: each visible SKU with count
- competitors_found: competitor brands with facing counts
- recommendations: 3 UNIQUE specific tips in Russian, different each time, based on what you actually see{prev_str}"""

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-opus-4-6",
                    "max_tokens": 1500,
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
            "total": total, "grade": grade(total)
        }
    except Exception as e:
        logger.error(f"Analysis error: {e}")
        return None


# ── Отчёт текст ────────────────────────────────────────────────────────────────
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
            lines.append(f"  {grade_emoji(e['total'])} {e['brand']}: {e['total']}% — {e['grade']}")
            if e.get("recommendations"):
                lines.append(f"  _💡 {e['recommendations'][0]}_")
        lines.append(f"  📊 Средний: *{avg}%*")
        lines.append("")

    all_t = [e["total"] for g in groups.values() for e in g["entries"]]
    overall = round(sum(all_t) / len(all_t))
    lines.append(f"🏆 *ИТОГ: {grade_emoji(overall)} {overall}%*")

    kb = [
        [InlineKeyboardButton("📄 Скачать PDF", callback_data="pdf_report")],
        [InlineKeyboardButton("🤖 Анализ ИИ", callback_data="ask_ai")]
    ]
    await message.reply_text("\n".join(lines), parse_mode="Markdown",
                              reply_markup=InlineKeyboardMarkup(kb))


# ── PDF ────────────────────────────────────────────────────────────────────────
async def send_pdf_report(message, s):
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

    summ = [
        [P("Показатель",10,True,colors.white), P("Значение",10,True,colors.white)],
        [P("Всего аудитов",10), P(str(len(s["audits"])),10)],
        [P("Торговых точек",10), P(str(len(set(a["outlet"] for a in s["audits"]))),10)],
        [P("Брендов проверено",10), P(str(len(set(a["brand"] for a in s["audits"]))),10)],
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
    story.append(P("ДЕТАЛИ АУДИТОВ",13,True))
    story.append(HRFlowable(width="100%",thickness=1,color=mid,spaceAfter=8))

    sc_col = lambda p: green if p>=80 else yellow if p>=60 else red

    for i, a in enumerate(s["audits"],1):
        ht = Table([[P(f"#{i}  {a['brand']}",12,True,colors.white),
                     P(f"{a['total']}%  {grade(a['total'])}",12,True,colors.white,2)]],
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

        sc = a["scores"]
        rows = [[P("Критерий",9,True,colors.white),
                 P("Балл",9,True,colors.white),
                 P("Замечание",9,True,colors.white)]]
        for key, name in [("facing","Фейсинг / выкладка"),("pos","Ценники / POS"),
                           ("clean","Чистота и порядок"),("oos_score","Наличие товара"),
                           ("competitors","vs Конкуренты")]:
            val = "OOS" if (key=="oos_score" and a["oos"]) else f"{sc.get(key,0)}/5"
            note = (a.get("notes") or {}).get(key,"") or "—"
            rows.append([P(name,9), P(val,9,True), P(note,9)])

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

        if a.get("facings"):
            story.append(Spacer(1,4))
            story.append(P("Фейсинги: " + "  |  ".join(
                f"{k}: {v}" for k,v in a["facings"].items()),9,False,gray))
        if a.get("competitors_found"):
            story.append(P("Конкуренты: " + " | ".join(a["competitors_found"]),9,False,gray))
        if a.get("recommendations"):
            for ri, r in enumerate(a["recommendations"],1):
                story.append(P(f"{ri}. {r}",9,False,accent))
        story.append(Spacer(1,14))

    # Сводка по брендам
    story.append(HRFlowable(width="100%",thickness=1,color=mid,spaceAfter=8))
    story.append(P("СВОДКА ПО БРЕНДАМ",13,True))
    story.append(Spacer(1,6))

    brand_groups = {}
    for a in s["audits"]:
        brand_groups.setdefault(a["brand"],[]).append(a["total"])

    brows = [[P("Бренд",10,True,colors.white), P("Аудитов",10,True,colors.white),
              P("Средний",10,True,colors.white), P("Оценка",10,True,colors.white)]]
    for b, totals in brand_groups.items():
        avg = round(sum(totals)/len(totals))
        brows.append([P(b,10), P(str(len(totals)),10), P(f"{avg}%",10,True), P(grade(avg),10)])

    bt = Table(brows, colWidths=[6*cm,3*cm,4*cm,4*cm])
    bt.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),accent),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,light]),
        ("GRID",(0,0),(-1,-1),0.5,mid),
        ("ALIGN",(1,0),(-1,-1),"CENTER"),
        ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ("PADDING",(0,0),(-1,-1),8),
    ]))
    story.append(bt)
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
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    logger.info("Bot is running!")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

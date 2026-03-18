import os
import base64
import json
import logging
import threading
import io
from datetime import date, datetime
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
import urllib.request

def download_font():
    font_path = "/tmp/DejaVuSans.ttf"
    if not os.path.exists(font_path):
        urllib.request.urlretrieve(
            "https://github.com/dejavu-fonts/dejavu-fonts/raw/master/ttf/DejaVuSans.ttf",
            font_path
        )
    font_bold = "/tmp/DejaVuSans-Bold.ttf"
    if not os.path.exists(font_bold):
        urllib.request.urlretrieve(
            "https://github.com/dejavu-fonts/dejavu-fonts/raw/master/ttf/DejaVuSans-Bold.ttf",
            font_bold
        )
    try:
        pdfmetrics.registerFont(TTFont("DejaVu", font_path))
        pdfmetrics.registerFont(TTFont("DejaVu-Bold", font_bold))
    except:
        pass

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

BRANDS = [
    "SOF", "Power SOFT Plus", "ALMIR", "Comfort Baby",
    "SOF Premium", "Amiri", "Makfa", "Konti", "Олейна", "Kent"
]

sessions = {}

def get_session(uid):
    if uid not in sessions:
        sessions[uid] = {"outlet": "", "square": "", "auditor": "", "brand": None, "audits": [], "state": "idle"}
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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = get_session(update.effective_user.id)
    s["state"] = "idle"
    kb = [[InlineKeyboardButton("📋 Новый аудит", callback_data="new_audit")]]
    if s["audits"]:
        kb.append([InlineKeyboardButton("📊 Отчёт", callback_data="show_report"),
                   InlineKeyboardButton("📄 PDF", callback_data="pdf_report")])
        kb.append([InlineKeyboardButton("🗑 Очистить", callback_data="clear")])
    await update.message.reply_text(
        "👋 *Shelf Audit Bot*\n\nОтправь фото полки — получи полный анализ:\n"
        "• Фейсинги по SKU\n• Конкуренты на полке\n• Рекомендации\n• Оценка мерчандайзера",
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
    elif d == "pdf_report":
        await send_pdf_report(q.message, s)
    elif d == "clear":
        s["audits"] = []
        await q.message.reply_text("✅ История очищена.")
    elif d.startswith("brand_"):
        idx = int(d.split("_")[1])
        s["brand"] = BRANDS[idx]
        s["state"] = "wait_photo"
        await q.message.reply_text(
            f"📸 Бренд: *{BRANDS[idx]}*\nОтправь фото полки:",
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
    else:
        await update.message.reply_text("Напиши /start чтобы начать аудит.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = get_session(update.effective_user.id)
    if s["state"] != "wait_photo":
        await update.message.reply_text("Сначала начни аудит — /start")
        return

    msg = await update.message.reply_text("🔍 Анализирую фото... ~15 сек")

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    photo_bytes = await file.download_as_bytearray()
    photo_b64 = base64.b64encode(photo_bytes).decode()

    result = await analyze_shelf(photo_b64, s["brand"])

    if result:
        s["audits"].append({
            "outlet": s["outlet"], "square": s["square"],
            "auditor": s["auditor"], "brand": s["brand"],
            "datetime": datetime.now().strftime("%d.%m.%Y %H:%M"),
            **result
        })

        em = grade_emoji(result["total"])
        lines = [
            f"*{s['outlet']} · кв.{s['square']}*",
            f"Бренд: *{s['brand']}*",
            ""
        ]

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
             InlineKeyboardButton("📄 PDF", callback_data="pdf_report")]
        ]
        await msg.delete()
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown",
                                        reply_markup=InlineKeyboardMarkup(kb))
    else:
        await msg.delete()
        await update.message.reply_text("❌ Не удалось проанализировать. Попробуй ещё раз.")


async def analyze_shelf(photo_b64: str, brand: str):
    # Simplified prompt to avoid token overflow
    prompt = f"""Проанализируй фото полки для бренда "{brand}". Верни ТОЛЬКО JSON без пояснений:

{{"scores":{{"facing":4,"pos":3,"clean":4,"oos_score":4,"competitors":3}},"oos":false,"facings":{{"Товар 1":3,"Товар 2":2}},"competitors_found":["Конкурент А: 4 фейса"],"recommendations":["Рекомендация 1","Рекомендация 2","Рекомендация 3"]}}

Оценки 1-5. facings - SKU видимые на фото и кол-во фейсингов. competitors_found - конкурентные бренды на полке. recommendations - 3 конкретных совета."""

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

        # Extract JSON safely
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            text = text[start:end]
        else:
            if "```" in text:
                text = text.split("```")[1].replace("json", "").strip()

        result = json.loads(text)
        scores = result.get("scores", {})
        oos = result.get("oos", False)
        total = calc_pct(scores, oos)
        return {
            "scores": scores,
            "oos": oos,
            "facings": result.get("facings", {}),
            "competitors_found": result.get("competitors_found", []),
            "recommendations": result.get("recommendations", []),
            "total": total,
            "grade": grade(total)
        }
    except Exception as e:
        logger.error(f"Analysis error: {e}")
        return None


async def send_report(message, s):
    if not s["audits"]:
        await message.reply_text("Нет данных.")
        return
    lines = [f"📋 *ОТЧЁТ*  |  _{date.today().strftime('%d.%m.%Y')}_", ""]
    groups = {}
    for a in s["audits"]:
        k = f"{a['outlet']}|{a['square']}"
        if k not in groups:
            groups[k] = {"outlet": a["outlet"], "square": a["square"], "auditor": a["auditor"], "entries": []}
        groups[k]["entries"].append(a)

    for g in groups.values():
        avg = round(sum(e["total"] for e in g["entries"]) / len(g["entries"]))
        lines.append(f"🏪 *{g['outlet']} · кв.{g['square']}*")
        lines.append(f"👤 {g['auditor']}")
        for e in g["entries"]:
            lines.append(f"  {grade_emoji(e['total'])} {e['brand']}: {e['total']}% — {e['grade']}")
        lines.append(f"  📊 Средний: *{avg}%*")
        lines.append("")

    all_t = [e["total"] for g in groups.values() for e in g["entries"]]
    overall = round(sum(all_t) / len(all_t))
    lines.append(f"🏆 *ИТОГ: {grade_emoji(overall)} {overall}%*")

    kb = [[InlineKeyboardButton("📄 Скачать PDF", callback_data="pdf_report")]]
    await message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))


async def send_pdf_report(message, s):
    if not s["audits"]:
        await message.reply_text("Нет данных для PDF.")
        return
    await message.reply_text("📄 Генерирую PDF...")
    try:
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
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4,
                            rightMargin=1.5*cm, leftMargin=1.5*cm,
                            topMargin=2*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet()
    primary = colors.HexColor("#1a1a2e")
    accent = colors.HexColor("#4361ee")
    green = colors.HexColor("#2dc653")
    yellow = colors.HexColor("#f4a261")
    red = colors.HexColor("#e63946")
    light = colors.HexColor("#f8f9fa")
    mid = colors.HexColor("#dee2e6")

    T = lambda txt, size=10, bold=False, color=None: Paragraph(txt, ParagraphStyle(
        "x", parent=styles["Normal"], fontSize=size,
        fontName="Helvetica-Bold" if bold else "Helvetica",
        textColor=color or primary, spaceAfter=3
    ))

    story = []
    story.append(T("ОТЧЁТ ПО АУДИТУ ПОЛКИ", 18, True, primary))
    story.append(T(f"Shelf Audit  |  {date.today().strftime('%d.%m.%Y')}", 11, False, colors.HexColor("#6c757d")))
    story.append(HRFlowable(width="100%", thickness=2, color=accent, spaceAfter=12))

    all_t = [a["total"] for a in s["audits"]]
    overall = round(sum(all_t) / len(all_t)) if all_t else 0

    summ = [["Показатель", "Значение"],
            ["Всего аудитов", str(len(s["audits"]))],
            ["Торговых точек", str(len(set(a["outlet"] for a in s["audits"])))],
            ["Средний балл", f"{overall}% — {grade(overall)}"]]
    st = Table(summ, colWidths=[8*cm, 9*cm])
    st.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), accent), ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"), ("FONTSIZE", (0,0), (-1,-1), 10),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, light]),
        ("GRID", (0,0), (-1,-1), 0.5, mid), ("PADDING", (0,0), (-1,-1), 8),
    ]))
    story.append(st)
    story.append(Spacer(1, 16))
    story.append(T("ДЕТАЛИ АУДИТОВ", 13, True))
    story.append(HRFlowable(width="100%", thickness=1, color=mid, spaceAfter=8))

    def sc_color(p):
        return green if p >= 80 else yellow if p >= 60 else red

    for i, a in enumerate(s["audits"], 1):
        hdr = [[T(f"#{i}  {a['brand']}", 11, True, colors.white),
                T(f"{a['total']}%  {grade(a['total'])}", 11, True, colors.white)]]
        ht = Table(hdr, colWidths=[10*cm, 7*cm])
        ht.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,-1), sc_color(a["total"])),
                                 ("PADDING", (0,0), (-1,-1), 8)]))
        story.append(ht)

        info = [[T(f"Точка: {a['outlet']}  |  Кв: {a['square']}", 9),
                 T(f"Мерч: {a['auditor']}  |  {a.get('datetime','')}", 9,
                   False, colors.HexColor("#6c757d"))]]
        it = Table(info, colWidths=[9*cm, 8*cm])
        it.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,-1), light),
                                 ("GRID", (0,0), (-1,-1), 0.5, mid),
                                 ("PADDING", (0,0), (-1,-1), 6)]))
        story.append(it)

        sc = a["scores"]
        rows = [["Критерий", "Балл", "Замечание"]]
        crit = [("facing","Фейсинг"),("pos","Ценники/POS"),
                ("clean","Чистота"),("oos_score","Наличие"),("competitors","vs Конкуренты")]
        for key, name in crit:
            val = "OOS ❌" if (key == "oos_score" and a["oos"]) else f"{sc.get(key,0)}/5"
            note = a.get("notes", {}).get(key, "—") or "—"
            rows.append([name, val, note])
        dt = Table(rows, colWidths=[5*cm, 2.5*cm, 9.5*cm])
        dt.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), primary), ("TEXTCOLOR", (0,0), (-1,0), colors.white),
            ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"), ("FONTSIZE", (0,0), (-1,-1), 9),
            ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, light]),
            ("GRID", (0,0), (-1,-1), 0.5, mid), ("PADDING", (0,0), (-1,-1), 6),
            ("ALIGN", (1,0), (1,-1), "CENTER"),
        ]))
        story.append(dt)

        if a.get("facings"):
            story.append(Spacer(1, 4))
            story.append(T("Фейсинги: " + "  |  ".join(f"{k}: {v}" for k,v in a["facings"].items()), 9,
                           False, colors.HexColor("#6c757d")))
        if a.get("competitors_found"):
            story.append(T("Конкуренты: " + ", ".join(a["competitors_found"]), 9,
                           False, colors.HexColor("#6c757d")))
        if a.get("recommendations"):
            story.append(T("Рекомендации: " + " | ".join(
                f"{i}. {r}" for i,r in enumerate(a["recommendations"],1)), 9, False, accent))
        story.append(Spacer(1, 14))

    story.append(HRFlowable(width="100%", thickness=1, color=mid, spaceAfter=6))
    story.append(T(f"Shelf Audit Bot  |  {datetime.now().strftime('%d.%m.%Y %H:%M')}", 8,
                   False, colors.HexColor("#6c757d")))
    doc.build(story)
    return buffer.getvalue()


def main():
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN not set!")
        return
    threading.Thread(target=run_health_server, daemon=True).start()
    logger.info("Health server started")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    logger.info("Bot is running!")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

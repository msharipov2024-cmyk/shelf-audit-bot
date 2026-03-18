import os
import base64
import json
import asyncio
import logging
import threading
import io
from datetime import date, datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import httpx

# PDF
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
    "SOF",
    "Power SOFT Plus",
    "ALMIR",
    "Comfort Baby",
    "SOF Premium",
    "Amiri",
    "Makfa",
    "Konti",
    "Олейна",
    "Kent"
]

CRITERIA_KEYS = ["facing", "pos", "clean", "oos_score", "competitors"]
CRITERIA_NAMES = ["Фейсинг / выкладка", "Ценники / POS", "Чистота и порядок", "Наличие (OOS)", "Позиция vs конкуренты"]
WEIGHTS = {"facing": 2.5, "pos": 1.5, "clean": 1.0, "oos_score": 2.5, "competitors": 1.5}

sessions = {}

def get_session(uid):
    if uid not in sessions:
        sessions[uid] = {
            "outlet": "", "square": "", "auditor": "",
            "brand": None, "audits": [], "state": "idle"
        }
    return sessions[uid]

def calc_pct(scores, oos):
    t, m = 0, 0
    for k, w in WEIGHTS.items():
        v = 0 if (k == "oos_score" and oos) else scores.get(k, 0)
        t += v * w
        m += 5 * w
    return round(t / m * 100) if m else 0

def grade(p):
    return "Отлично" if p >= 90 else "Хорошо" if p >= 80 else "Удовл." if p >= 60 else "Плохо"

def grade_emoji(p):
    return "🟢" if p >= 80 else "🟡" if p >= 60 else "🔴"


# ── Health server ──────────────────────────────────────────────────────────────
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


# ── Handlers ───────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = get_session(update.effective_user.id)
    s["state"] = "idle"
    kb = [[InlineKeyboardButton("📋 Новый аудит", callback_data="new_audit")]]
    if s["audits"]:
        kb.append([InlineKeyboardButton("📊 Показать отчёт", callback_data="show_report")])
        kb.append([InlineKeyboardButton("📄 Скачать PDF", callback_data="pdf_report")])
        kb.append([InlineKeyboardButton("🗑 Очистить", callback_data="clear")])
    await update.message.reply_text(
        "👋 *Аудит полки — Shelf Audit Bot*\n\n"
        "Фотографируй полку и получай детальный анализ:\n"
        "• Фейсинги по SKU\n"
        "• Сравнение с конкурентами\n"
        "• Рекомендации по улучшению\n"
        "• Оценка мерчандайзера\n\n"
        "Нажми *Новый аудит* чтобы начать.",
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
            f"📸 Бренд: *{BRANDS[idx]}*\n\n"
            "Отправь фото полки — анализирую автоматически!\n"
            "_Постарайся сфотографировать всю полку целиком_",
            parse_mode="Markdown"
        )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = get_session(update.effective_user.id)
    text = update.message.text.strip()

    if s["state"] == "ask_outlet":
        s["outlet"] = text
        s["state"] = "ask_square"
        await update.message.reply_text("📍 Введи номер квадрата / зоны:")
    elif s["state"] == "ask_square":
        s["square"] = text
        s["state"] = "ask_auditor"
        await update.message.reply_text("👤 Введи ФИО мерчандайзера / ТП:")
    elif s["state"] == "ask_auditor":
        s["auditor"] = text
        s["state"] = "ask_brand"
        # Show brands as grid
        kb = []
        row = []
        for i, b in enumerate(BRANDS):
            row.append(InlineKeyboardButton(b, callback_data=f"brand_{i}"))
            if len(row) == 2:
                kb.append(row)
                row = []
        if row:
            kb.append(row)
        await update.message.reply_text(
            "🏷 Выбери бренд для аудита:",
            reply_markup=InlineKeyboardMarkup(kb)
        )
    else:
        await update.message.reply_text("Напиши /start чтобы начать аудит.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = get_session(update.effective_user.id)
    if s["state"] != "wait_photo":
        await update.message.reply_text("Сначала начни аудит — /start")
        return

    msg = await update.message.reply_text("🔍 Анализирую фото полки... Это займёт ~15 секунд")

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
            "datetime": datetime.now().strftime("%d.%m.%Y %H:%M"),
            **result
        })

        scores = result["scores"]
        oos = result["oos"]
        total = result["total"]
        g = result["grade"]
        notes = result["notes"]
        facings = result.get("facings", {})
        competitors = result.get("competitors_found", [])
        recommendations = result.get("recommendations", [])
        em = grade_emoji(total)

        lines = [
            f"*{s['outlet']}  ·  кв. {s['square']}*",
            f"Бренд: *{s['brand']}*  |  {datetime.now().strftime('%d.%m.%Y %H:%M')}",
            ""
        ]

        # Facings per SKU
        if facings:
            lines.append("📦 *Фейсинги по SKU:*")
            for sku, count in facings.items():
                status = "✅" if count >= 2 else "⚠️"
                lines.append(f"  {status} {sku}: *{count}* фейс(ов)")
            lines.append("")

        # Scores
        lines.append("📊 *Оценки по критериям:*")
        for i, key in enumerate(CRITERIA_KEYS):
            val = "OOS ❌" if (key == "oos_score" and oos) else f"{scores.get(key, 0)}/5"
            note = notes.get(key, "")
            line = f"• {CRITERIA_NAMES[i]}: *{val}*"
            if note:
                line += f"\n  _{note}_"
            lines.append(line)

        # Competitors
        if competitors:
            lines.append("")
            lines.append("🥊 *Конкуренты на полке:*")
            for c in competitors:
                lines.append(f"  • {c}")

        # Recommendations
        if recommendations:
            lines.append("")
            lines.append("💡 *Рекомендации:*")
            for i, r in enumerate(recommendations, 1):
                lines.append(f"  {i}. {r}")

        lines += [
            "",
            f"{em} *Итог: {total}% — {g}*",
            f"👤 Оценка мерчандайзера: *{total}%*"
        ]

        kb = [
            [InlineKeyboardButton("📸 Ещё один бренд", callback_data="new_audit")],
            [
                InlineKeyboardButton("📊 Отчёт", callback_data="show_report"),
                InlineKeyboardButton("📄 PDF", callback_data="pdf_report")
            ]
        ]
        await msg.delete()
        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )
    else:
        await msg.delete()
        await update.message.reply_text(
            "❌ Не удалось проанализировать фото.\n"
            "Попробуй ещё раз — убедись что полка хорошо освещена."
        )


# ── Claude Vision Analysis ─────────────────────────────────────────────────────
async def analyze_shelf(photo_b64: str, brand: str):
    prompt = f"""Ты эксперт-мерчандайзер с опытом аудита торговых полок в FMCG.

Проанализируй фото торговой полки для бренда "{brand}".

Выполни детальный анализ и верни ТОЛЬКО валидный JSON (без markdown, без пояснений):

{{
  "scores": {{
    "facing": <1-5>,
    "pos": <1-5>,
    "clean": <1-5>,
    "oos_score": <1-5>,
    "competitors": <1-5>
  }},
  "oos": <true/false>,
  "facings": {{
    "<название SKU или товара>": <количество фейсингов>,
    ...
  }},
  "notes": {{
    "facing": "<замечание по фейсингу>",
    "pos": "<замечание по POS>",
    "clean": "<замечание по чистоте>",
    "oos_score": "<замечание по наличию>",
    "competitors": "<замечание по конкурентам>"
  }},
  "competitors_found": [
    "<бренд конкурента>: <кол-во фейсингов> фейсов",
    ...
  ],
  "recommendations": [
    "<конкретная рекомендация 1>",
    "<конкретная рекомендация 2>",
    "<конкретная рекомендация 3>"
  ]
}}

Критерии оценки (1-5):
- facing: блочность, количество фейсингов, уровень полки (глаза=5, руки=4, ноги=3)
- pos: наличие и корректность ценников, шелфтокеров, воблеров
- clean: чистота полки, правильная ориентация товара, нет просрочки
- oos_score: заполненность полки (пустые места снижают оценку)
- competitors: наша доля полки vs конкуренты (больше наша доля = выше оценка)

В facings укажи каждый видимый SKU и количество фейсингов.
В competitors_found перечисли все конкурентные бренды видимые на полке.
Рекомендации должны быть конкретными и actionable."""

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
                    "max_tokens": 2000,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/jpeg",
                                    "data": photo_b64
                                }
                            },
                            {"type": "text", "text": prompt}
                        ]
                    }]
                }
            )
        data = resp.json()
      text = data["content"][0]["text"].strip()
        if "```" in text:
            text = text.split("```")[1].replace("json", "").strip()
        # Find JSON boundaries
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            text = text[start:end]
        result = json.loads(text)
        scores = result["scores"]
        oos = result.get("oos", False)
        total = calc_pct(scores, oos)
        return {
            "scores": scores,
            "oos": oos,
            "facings": result.get("facings", {}),
            "notes": result.get("notes", {}),
            "competitors_found": result.get("competitors_found", []),
            "recommendations": result.get("recommendations", []),
            "total": total,
            "grade": grade(total)
        }
    except Exception as e:
        logger.error(f"Analysis error: {e}")
        return None


# ── Text Report ────────────────────────────────────────────────────────────────
async def send_report(message, s):
    if not s["audits"]:
        await message.reply_text("Нет данных. Сначала сохрани аудит.")
        return

    groups = {}
    for a in s["audits"]:
        k = a["outlet"] + "|" + a["square"]
        if k not in groups:
            groups[k] = {
                "outlet": a["outlet"],
                "square": a["square"],
                "auditor": a["auditor"],
                "entries": []
            }
        groups[k]["entries"].append(a)

    lines = [
        f"📋 *ОТЧЁТ ПО АУДИТУ ПОЛКИ*",
        f"_{date.today().strftime('%d.%m.%Y')}_",
        ""
    ]

    for g in groups.values():
        avg = round(sum(e["total"] for e in g["entries"]) / len(g["entries"]))
        lines.append(f"🏪 *{g['outlet']}  ·  кв. {g['square']}*")
        if g["auditor"]:
            lines.append(f"👤 Мерчандайзер: {g['auditor']}")
        lines.append("")
        for e in g["entries"]:
            em = grade_emoji(e["total"])
            lines.append(f"  {em} *{e['brand']}*: {e['total']}% — {e['grade']}")
            if e.get("recommendations"):
                lines.append(f"  _{e['recommendations'][0]}_")
        lines.append(f"\n  📊 Средний балл: {grade_emoji(avg)} *{avg}%*")
        lines.append("")

    # Overall average
    all_totals = [e["total"] for g in groups.values() for e in g["entries"]]
    overall = round(sum(all_totals) / len(all_totals)) if all_totals else 0
    lines.append(f"🏆 *ОБЩИЙ ИТОГ: {grade_emoji(overall)} {overall}%*")

    kb = [[InlineKeyboardButton("📄 Скачать PDF", callback_data="pdf_report")]]
    await message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )


# ── PDF Report ─────────────────────────────────────────────────────────────────
async def send_pdf_report(message, s):
    if not s["audits"]:
        await message.reply_text("Нет данных для PDF.")
        return

    await message.reply_text("📄 Генерирую PDF-отчёт...")

    try:
        pdf_bytes = generate_pdf(s)
        bio = io.BytesIO(pdf_bytes)
        bio.name = f"shelf_audit_{date.today().strftime('%Y%m%d')}.pdf"
        bio.seek(0)
        await message.reply_document(
            document=bio,
            filename=bio.name,
            caption=f"📄 Отчёт по аудиту полки — {date.today().strftime('%d.%m.%Y')}"
        )
    except Exception as e:
        logger.error(f"PDF error: {e}")
        await message.reply_text("❌ Ошибка генерации PDF. Попробуй ещё раз.")


def generate_pdf(s):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=1.5*cm,
        leftMargin=1.5*cm,
        topMargin=2*cm,
        bottomMargin=2*cm
    )

    styles = getSampleStyleSheet()
    story = []

    # Colors
    primary = colors.HexColor("#1a1a2e")
    accent = colors.HexColor("#4361ee")
    green = colors.HexColor("#2dc653")
    yellow = colors.HexColor("#f4a261")
    red = colors.HexColor("#e63946")
    light_gray = colors.HexColor("#f8f9fa")
    mid_gray = colors.HexColor("#dee2e6")

    # Custom styles
    title_style = ParagraphStyle(
        "CustomTitle",
        parent=styles["Title"],
        fontSize=20,
        textColor=primary,
        spaceAfter=6,
        fontName="Helvetica-Bold"
    )
    subtitle_style = ParagraphStyle(
        "Subtitle",
        parent=styles["Normal"],
        fontSize=11,
        textColor=colors.HexColor("#6c757d"),
        spaceAfter=20,
        fontName="Helvetica"
    )
    heading_style = ParagraphStyle(
        "Heading",
        parent=styles["Heading2"],
        fontSize=13,
        textColor=primary,
        spaceBefore=16,
        spaceAfter=8,
        fontName="Helvetica-Bold"
    )
    body_style = ParagraphStyle(
        "Body",
        parent=styles["Normal"],
        fontSize=10,
        textColor=primary,
        spaceAfter=4,
        fontName="Helvetica"
    )
    small_style = ParagraphStyle(
        "Small",
        parent=styles["Normal"],
        fontSize=9,
        textColor=colors.HexColor("#6c757d"),
        fontName="Helvetica"
    )

    # ── Header ──
    story.append(Paragraph("ОТЧЕТ ПО АУДИТУ ПОЛКИ", title_style))
    story.append(Paragraph(f"Shelf Audit Report  |  {date.today().strftime('%d.%m.%Y')}", subtitle_style))
    story.append(HRFlowable(width="100%", thickness=2, color=accent, spaceAfter=16))

    # ── Summary table ──
    all_totals = [a["total"] for a in s["audits"]]
    overall_avg = round(sum(all_totals) / len(all_totals)) if all_totals else 0

    def score_color(p):
        return green if p >= 80 else yellow if p >= 60 else red

    summary_data = [
        ["Показатель", "Значение"],
        ["Всего аудитов", str(len(s["audits"]))],
        ["Торговых точек", str(len(set(a["outlet"] for a in s["audits"])))],
        ["Брендов проверено", str(len(set(a["brand"] for a in s["audits"])))],
        ["Средний балл", f"{overall_avg}%  —  {grade(overall_avg)}"],
    ]
    summary_table = Table(summary_data, colWidths=[8*cm, 9*cm])
    summary_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), accent),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("BACKGROUND", (0, 1), (-1, -1), light_gray),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, light_gray]),
        ("GRID", (0, 0), (-1, -1), 0.5, mid_gray),
        ("ALIGN", (1, 0), (1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("PADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(summary_table)
    story.append(Spacer(1, 20))

    # ── Per-audit details ──
    story.append(Paragraph("ДЕТАЛИ ПО КАЖДОМУ АУДИТУ", heading_style))
    story.append(HRFlowable(width="100%", thickness=1, color=mid_gray, spaceAfter=12))

    for idx, a in enumerate(s["audits"], 1):
        sc = a["scores"]
        em = "ХОРОШО" if a["total"] >= 80 else "УДОВЛю" if a["total"] >= 60 else "ПЛОХО"

        # Audit header
        header_data = [[
            Paragraph(f"#{idx}  {a['brand']}", ParagraphStyle(
                "AH", parent=styles["Normal"], fontSize=12,
                textColor=colors.white, fontName="Helvetica-Bold"
            )),
            Paragraph(f"{a['total']}%  {em}", ParagraphStyle(
                "AH2", parent=styles["Normal"], fontSize=12,
                textColor=colors.white, fontName="Helvetica-Bold",
                alignment=2
            ))
        ]]
        ht = Table(header_data, colWidths=[10*cm, 7*cm])
        ht.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), score_color(a["total"])),
            ("PADDING", (0, 0), (-1, -1), 8),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        story.append(ht)

        # Info row
        info_data = [[
            Paragraph(f"Точка: {a['outlet']}  |  Кв: {a['square']}", body_style),
            Paragraph(f"Мерч: {a['auditor']}  |  {a.get('datetime','')}", small_style)
        ]]
        it = Table(info_data, colWidths=[9*cm, 8*cm])
        it.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), light_gray),
            ("PADDING", (0, 0), (-1, -1), 6),
            ("GRID", (0, 0), (-1, -1), 0.5, mid_gray),
        ]))
        story.append(it)

        # Scores table
        scores_data = [["Критерий", "Балл", "Замечание"]]
        for key, name in zip(CRITERIA_KEYS, CRITERIA_NAMES):
            val = "OOS ❌" if (key == "oos_score" and a["oos"]) else f"{sc.get(key, 0)}/5"
            note = a.get("notes", {}).get(key, "") or "—"
            scores_data.append([name, val, note])

        st = Table(scores_data, colWidths=[5*cm, 2.5*cm, 9.5*cm])
        st.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), primary),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, light_gray]),
            ("GRID", (0, 0), (-1, -1), 0.5, mid_gray),
            ("ALIGN", (1, 0), (1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("PADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(st)

        # Facings
        if a.get("facings"):
            story.append(Spacer(1, 6))
            story.append(Paragraph("Фейсинги по SKU:", ParagraphStyle(
                "FH", parent=styles["Normal"], fontSize=9,
                textColor=colors.HexColor("#6c757d"), fontName="Helvetica-Bold"
            )))
            facing_items = [f"{sku}: {cnt} фейс(ов)" for sku, cnt in a["facings"].items()]
            story.append(Paragraph("  |  ".join(facing_items), small_style))

        # Competitors
        if a.get("competitors_found"):
            story.append(Spacer(1, 4))
            story.append(Paragraph("Конкуренты на полке:", ParagraphStyle(
                "CH", parent=styles["Normal"], fontSize=9,
                textColor=colors.HexColor("#6c757d"), fontName="Helvetica-Bold"
            )))
            for c in a["competitors_found"]:
                story.append(Paragraph(f"• {c}", small_style))

        # Recommendations
        if a.get("recommendations"):
            story.append(Spacer(1, 4))
            story.append(Paragraph("Рекомендации:", ParagraphStyle(
                "RH", parent=styles["Normal"], fontSize=9,
                textColor=accent, fontName="Helvetica-Bold"
            )))
            for i, r in enumerate(a["recommendations"], 1):
                story.append(Paragraph(f"{i}. {r}", small_style))

        story.append(Spacer(1, 16))

    # ── Brands summary ──
    story.append(HRFlowable(width="100%", thickness=1, color=mid_gray, spaceAfter=12))
    story.append(Paragraph("СВОДКА ПО БРЕНДАМ", heading_style))

    brand_groups = {}
    for a in s["audits"]:
        b = a["brand"]
        if b not in brand_groups:
            brand_groups[b] = []
        brand_groups[b].append(a["total"])

    brand_data = [["Бренд", "Аудитов", "Средний балл", "Оценка"]]
    for b, totals in brand_groups.items():
        avg = round(sum(totals) / len(totals))
        brand_data.append([b, str(len(totals)), f"{avg}%", grade(avg)])

    bt = Table(brand_data, colWidths=[6*cm, 3*cm, 4*cm, 4*cm])
    bt.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), accent),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, light_gray]),
        ("GRID", (0, 0), (-1, -1), 0.5, mid_gray),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("PADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(bt)
    story.append(Spacer(1, 20))

    # ── Footer ──
    story.append(HRFlowable(width="100%", thickness=1, color=mid_gray, spaceAfter=8))
    story.append(Paragraph(
        f"Отчёт сгенерирован автоматически  |  Shelf Audit Bot  |  {datetime.now().strftime('%d.%m.%Y %H:%M')}",
        small_style
    ))

    doc.build(story)
    return buffer.getvalue()


# ── Main ───────────────────────────────────────────────────────────────────────
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

import os
from functools import lru_cache
from html import escape
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle


@lru_cache(maxsize=1)
def document_font():
    path = os.getenv("APP_PDF_FONT", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    pdfmetrics.registerFont(TTFont("Commercial", path))
    return "Commercial"


def rubles(minor):
    whole, fraction = divmod(minor, 100)
    return f"{whole:,}".replace(",", " ") + f",{fraction:02d} ₽"


def render_pdf(quote, kind):
    title = "Коммерческое предложение" if kind == "quote" else "Счёт на оплату · проект"
    output = BytesIO()
    font = document_font()
    text = ParagraphStyle("text", fontName=font, fontSize=9, leading=13)
    amount = ParagraphStyle("amount", parent=text, alignment=TA_RIGHT)
    heading = ParagraphStyle("heading", parent=text, fontSize=18, leading=24, spaceAfter=14)
    story = [Paragraph(title, heading), Paragraph(f"Номер {escape(quote['id'])}", text),
             Paragraph(f"Клиент: {escape(quote['contact'])}", text)]
    if kind == "invoice":
        seller = os.getenv("APP_SELLER_DETAILS", "").strip()
        if seller:
            story.append(Paragraph(escape(seller).replace("\n", "<br/>"), text))
        story.append(Paragraph("Проект для проверки менеджером. Оплата по этому документу не предусмотрена.", text))
    story.append(Spacer(1, 16))

    def row(values):
        return [Paragraph(escape(str(value)), text if index < 2 else amount)
                for index, value in enumerate(values)]

    rows = [row(("Артикул", "Товар", "Кол-во", "Цена", "Сумма"))]
    for item in quote["items"]:
        price, count = item["price_minor"], item["quantity"]
        rows.append(row((item["sku"], item["name"], count, rubles(price), rubles(price * count))))
    table = LongTable(rows, colWidths=(80, 166, 43, 88, 91), repeatRows=1, splitInRow=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e6eff7")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), .4, colors.HexColor("#ccd5de")),
    ]))
    story.extend((table, Spacer(1, 12), Paragraph(f"Итого: {rubles(quote['total_minor'])}", amount)))
    SimpleDocTemplate(output, pagesize=A4, leftMargin=48, rightMargin=48, title=title).build(story)
    return output.getvalue()

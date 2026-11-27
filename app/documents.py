import os
import subprocess
from datetime import date
from functools import lru_cache
from html import escape
from io import BytesIO
from pathlib import Path

from PIL import Image
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
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


@lru_cache(maxsize=2)
def template_branding(path, _modified_ns):
    """Извлекает фирменные элементы шаблона без подписи и печати."""
    raster = subprocess.run(
        ["pdftoppm", "-f", "1", "-l", "1", "-r", "144", "-singlefile", "-png", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=True,
        timeout=10,
    ).stdout
    # Координаты долей страницы рассчитаны по одностраничному шаблону КП формата A4.
    regions = ((.06, .04, .46, .174), (.17, .912, .84, .981))
    with Image.open(BytesIO(raster)) as page:
        width, height = page.size
        branding = []
        for x1, y1, x2, y2 in regions:
            box = (round(x1 * width), round(y1 * height), round(x2 * width), round(y2 * height))
            branding.append((ImageReader(page.crop(box)), x1, y2, x2 - x1, y2 - y1))
    return branding


def branded_page(template, quote, style):
    branding = template_branding(template, template.stat().st_mtime_ns)
    recipient = Paragraph(f"Куда: {escape(quote['contact'])}", style)
    outgoing = Paragraph(
        f"Исх. № {escape(quote['id'])}<br/>Сформировано {date.today():%d.%m.%Y}",
        style,
    )

    def paint(canvas, document):
        width, height = document.pagesize
        for image, x, y, image_width, image_height in branding:
            canvas.drawImage(image, x * width, (1 - y) * height,
                             image_width * width, image_height * height)
        top = height - 55
        for paragraph in (recipient, outgoing):
            _, block = paragraph.wrap(240, 130)
            top -= block
            paragraph.drawOn(canvas, width - 274, top)
            top -= 8

    return paint


def render_pdf(quote, kind):
    """Возвращает PDF КП или проекта счёта из одного снимка позиций."""
    title = "Коммерческое предложение" if kind == "quote" else "Счёт на оплату · проект"
    template_path = os.getenv("APP_QUOTE_TEMPLATE") if kind == "quote" else None
    output = BytesIO()
    font = document_font()
    text = ParagraphStyle("text", fontName=font, fontSize=9, leading=13)
    amount = ParagraphStyle("amount", parent=text, alignment=TA_RIGHT)
    heading = ParagraphStyle("heading", parent=text, fontSize=18, leading=24, spaceAfter=14)
    margin = 48
    top_margin = 72
    page_drawing = {}

    if template_path:
        template = Path(template_path).resolve()
        paint = branded_page(template, quote, amount)
        page_drawing = dict(onFirstPage=paint, onLaterPages=paint)
        margin, top_margin = 34, 189
        centered = ParagraphStyle("centered", parent=text, alignment=TA_CENTER)
        story = [
            Paragraph("Уважаемый заказчик!", centered),
            Spacer(1, 12),
            Paragraph("Коммерческое предложение · проект для проверки менеджером", centered),
        ]
        columns = ("Артикул", "Наименование", "Кол-во", "Цена, руб.", "Сумма, руб.", "В т.ч. НДС, руб.")
        widths = (63, 194, 37, 71, 76, 72)
    else:
        story = [Paragraph(title, heading), Paragraph(f"Номер {escape(quote['id'])}", text),
                 Paragraph(f"Клиент: {escape(quote['contact'])}", text)]
        columns = ("Артикул", "Товар", "Кол-во", "Цена", "Сумма")
        widths = (80, 166, 43, 88, 91)

    if kind == "invoice":
        seller = os.getenv("APP_SELLER_DETAILS", "").strip()
        if seller:
            story.append(Paragraph(escape(seller).replace("\n", "<br/>"), text))
        story.append(Paragraph("Проект для проверки менеджером. Оплата по этому документу не предусмотрена.", text))
    story.append(Spacer(1, 12))

    def row(values):
        return [Paragraph(escape(str(value)), text if index < 2 else amount)
                for index, value in enumerate(values)]

    rows = [row(columns)]
    for item in quote["items"]:
        price, count = item["price_minor"], item["quantity"]
        values = (item["sku"], item["name"], count, rubles(price), rubles(price * count))
        rows.append(row((*values, "—") if template_path else values))

    grid = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e6eff7")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), .4, colors.HexColor("#ccd5de")),
    ]
    if template_path:
        rows.append(row(("", "Итого", "", "", rubles(quote["total_minor"]), "—")))
        grid = [
            ("GRID", (0, 0), (-1, -1), .5, colors.black),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]
    table = LongTable(rows, colWidths=widths, repeatRows=1, splitInRow=1)
    table.setStyle(TableStyle(grid))
    story.extend((table, Spacer(1, 12)))
    if template_path:
        for line in (
            "Ставку и сумму НДС подтверждает менеджер.",
            "Условия оплаты: по согласованию.",
            "Срок поставки: по согласованию.",
            "Срок действия предложения: после согласования с менеджером.",
        ):
            story.append(Paragraph(line, text))
    else:
        story.append(Paragraph(f"Итого: {rubles(quote['total_minor'])}", amount))

    SimpleDocTemplate(output, pagesize=A4, leftMargin=margin, rightMargin=margin,
                      topMargin=top_margin, title=title).build(story, **page_drawing)
    return output.getvalue()

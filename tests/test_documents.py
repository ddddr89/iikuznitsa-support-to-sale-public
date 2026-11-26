import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reportlab.pdfgen.canvas import Canvas

from app.documents import render_pdf


@unittest.skipUnless(shutil.which("pdftoppm") and shutil.which("pdftotext"), "requires Poppler")
class QuoteTemplateTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.template = Path(folder.name) / "template.pdf"
        template = Canvas(str(self.template), pagesize=(595.32, 841.92))
        template.drawString(50, 790, "LETTERHEAD")
        template.drawString(180, 35, "FOOTER")
        template.save()

    def quote(self, count):
        item = dict(sku="SKU-001", name="Сетевой монитор", quantity=2, price_minor=10050)
        return dict(id="new-quote", contact="Тестовый заказчик <test@example.invalid>",
                    items=[item] * count, total_minor=20100 * count)

    def pdf_text(self, document):
        return subprocess.check_output(["pdftotext", "-layout", "-", "-"], input=document).decode()

    def test_template_fills_quote_and_invoice_ignores_template(self):
        with patch.dict("os.environ", {"APP_QUOTE_TEMPLATE": str(self.template)}):
            quote_pdf = render_pdf(self.quote(1), "quote")
            invoice_pdf = render_pdf(self.quote(1), "invoice")
        self.assertTrue(quote_pdf.startswith(b"%PDF"))
        text = self.pdf_text(quote_pdf)
        self.assertIn("Тестовый заказчик <test@example.invalid>", text)
        self.assertIn("SKU-001", text)
        self.assertIn("201,00 ₽", text)
        self.assertIn("НДС подтверждает менеджер", text)
        self.assertIn("проект для проверки менеджером", text)
        self.assertIn("Счёт на оплату", self.pdf_text(invoice_pdf))

    def test_multipage_template_keeps_all_rows_and_total(self):
        with patch.dict("os.environ", {"APP_QUOTE_TEMPLATE": str(self.template)}):
            text = self.pdf_text(render_pdf(self.quote(50), "quote"))
        self.assertEqual(text.count("SKU-001"), 50)
        self.assertIn("10 050,00 ₽", text)
        self.assertGreater(text.count("Наименование"), 1)

    def test_demo_fallback_without_template(self):
        with patch.dict("os.environ", {"APP_QUOTE_TEMPLATE": ""}):
            text = self.pdf_text(render_pdf(self.quote(1), "quote"))
        self.assertIn("Клиент: Тестовый заказчик", text)
        self.assertIn("Итого: 201,00 ₽", text)

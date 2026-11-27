"""HTTP acceptance tests against tests/run-smoke.sh and its demo fixtures."""

import json
import subprocess
import unittest
from io import BytesIO
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4
from zipfile import ZipFile

from smoke import BASE, call


class ContestTests(unittest.TestCase):
    api = "/api/v1"

    def lead(self, **extra):
        payload = {"contact": f"{uuid4().hex}@example.invalid", "message": "Два монитора"}
        return call(self.api + "/leads", {**payload, **extra}, status=201)

    def quote(self, lead_id, items=None, status=201):
        return call(self.api + "/manager/quotes", {
            "lead_id": lead_id,
            "items": items if items is not None else [{"sku": "DEMO-MON-01", "quantity": 2}],
        }, auth=True, status=status)

    def test_01_health(self):
        self.assertEqual(call(self.api + "/health")["status"], "ok")

    def test_02_client_and_manager_pages(self):
        self.assertIn(b'href="/client"', call("/"))
        self.assertIn(b'id="chat-form"', call("/client"))
        self.assertIn(b"<html", call("/manager"))

    def test_03_exact_sku_search(self):
        products = call(self.api + "/products?q=DEMO-MON-01&limit=1")["items"]
        self.assertEqual(products[0]["sku"], "DEMO-MON-01")

    def test_04_catalog_pagination(self):
        first = call(self.api + "/products?limit=1")["items"]
        second = call(self.api + "/products?limit=1&offset=1")["items"]
        self.assertNotEqual(first[0]["sku"], second[0]["sku"])
        snapshot = call(self.api + "/catalog/status")
        self.assertEqual(snapshot["source"], "demo")
        self.assertEqual(snapshot["item_count"], 6)

    def test_05_catalog_chat(self):
        result = call(self.api + "/chat", {"message": "монитор"})
        self.assertEqual(result["provider"], "catalog")
        self.assertTrue(result["products"])

    def test_06_invalid_chat(self):
        call(self.api + "/chat", {"message": " "}, status=422)

    def test_07_lead_validation(self):
        call(self.api + "/leads", {"contact": "x", "message": "x", "sku": "UNKNOWN"}, status=404)

    def test_08_manager_requires_token(self):
        call(self.api + "/manager/leads", status=401)

    def test_09_lead_replay(self):
        first = self.lead()
        body = {key: first[key] for key in ("contact", "message")}
        second = call(self.api + "/leads", body, status=201)
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["duplicate"])

    def test_10_escalation_promotes_prior_lead(self):
        first = self.lead()
        second = call(self.api + "/leads", {
            "contact": first["contact"], "message": first["message"], "escalation": True,
        }, status=201)
        self.assertEqual(first["id"], second["id"])
        items = call(self.api + "/manager/leads", auth=True)["items"]
        self.assertEqual(next(item for item in items if item["id"] == first["id"])["status"], "escalated")

    def test_11_total_and_quote_replay(self):
        lead = self.lead()
        first = self.quote(lead["id"])
        self.assertEqual(first["total_minor"], 4798000)
        self.assertEqual(self.quote(lead["id"])["id"], first["id"])

    def test_12_unpriced_item(self):
        self.quote(self.lead()["id"], [{"sku": "DEMO-BACKUP-01", "quantity": 1}], status=409)

    def test_13_duplicate_quote_rows(self):
        row = {"sku": "DEMO-MON-01", "quantity": 1}
        self.quote(self.lead()["id"], [row, row], status=422)

    def test_14_quote_pdf(self):
        quote = self.quote(self.lead()["id"])
        self.assertTrue(call(quote["quote_pdf_url"], auth=True).startswith(b"%PDF-"))

    def test_15_invoice_pdf_requires_token(self):
        quote = self.quote(self.lead()["id"])
        call(quote["invoice_pdf_url"], status=401)
        self.assertTrue(call(quote["invoice_pdf_url"], auth=True).startswith(b"%PDF-"))


    def test_16_document_zip_matches_authorized_pdfs_and_respects_ownership(self):
        def session_cookie():
            with urlopen(BASE + self.api + "/client/leads", timeout=20) as response:
                return response.headers["Set-Cookie"].split(";", 1)[0]

        owner = session_cookie()
        request = Request(BASE + self.api + "/leads", json.dumps({
            "contact": "bundle@example.invalid", "message": "Нужны документы на два монитора",
            "sku": "DEMO-MON-01",
        }).encode(), {"Content-Type": "application/json", "Cookie": owner})
        with urlopen(request, timeout=20) as response:
            lead = json.load(response)
        package = self.quote(lead["id"])
        path = self.api + "/client/quotes/" + package["id"] + "/documents.zip"
        raw = call(package["documents_url"], auth=True)
        call(package["documents_url"], status=401)
        for headers in ({}, {"Cookie": session_cookie()}):
            with self.assertRaises(HTTPError) as forbidden:
                urlopen(Request(BASE + path, headers=headers), timeout=20)
            self.assertEqual(forbidden.exception.code, 404)
            forbidden.exception.close()
        with urlopen(Request(BASE + path, headers={"Cookie": owner}), timeout=20) as response:
            self.assertEqual(response.headers.get_content_type(), "application/zip")
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            self.assertIn(".zip", response.headers["Content-Disposition"])
            client_zip = response.read()
        for data in (raw, client_zip):
            with ZipFile(BytesIO(data)) as archive:
                self.assertEqual(archive.namelist(), ["Коммерческое предложение.pdf", "Проект счёта.pdf"])
                self.assertIsNone(archive.testzip())
                for name, url in zip(archive.namelist(), (package["quote_pdf_url"], package["invoice_pdf_url"])):
                    pdf = archive.read(name)
                    self.assertEqual(pdf, call(url, auth=True))
                    text = subprocess.run(["pdftotext", "-layout", "-", "-"], input=pdf, check=True,
                                          stdout=subprocess.PIPE).stdout.decode()
                    normalized = "".join(text.split())
                    self.assertRegex(text, r"(?m)^\s*DEMO-MON-01[^\n]*?\s+2\s+23\s+990,00\s+₽\s+47\s+980,00")
                    self.assertIn("47980,00", normalized)
                    if name == "Проект счёта.pdf":
                        self.assertIn("Оплата по этому документу не предусмотрена", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""HTTP acceptance tests against tests/run-smoke.sh and its demo fixtures."""

import unittest
from uuid import uuid4

from smoke import call


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


if __name__ == "__main__":
    unittest.main(verbosity=2)

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from html import escape
from uuid import uuid4


class Commercial:
    def __init__(self, db_path):
        self.db_path = db_path

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        try:
            with db:
                db.execute("PRAGMA foreign_keys = ON")
                yield db
        finally:
            db.close()

    def init(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                CREATE TABLE IF NOT EXISTS leads (
                    id TEXT PRIMARY KEY, contact TEXT NOT NULL,
                    message TEXT NOT NULL, sku TEXT, created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'new'
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS quotes (
                    id TEXT PRIMARY KEY, lead_id TEXT NOT NULL REFERENCES leads(id),
                    items TEXT NOT NULL, total_minor INTEGER NOT NULL
                )
            """)
            fields = {field["name"] for field in db.execute("PRAGMA table_info(leads)")}
            if "status" not in fields:
                db.execute("ALTER TABLE leads ADD COLUMN status TEXT NOT NULL DEFAULT 'new'")
            db.execute("CREATE INDEX IF NOT EXISTS leads_recent ON leads(created_at)")
            db.execute("CREATE INDEX IF NOT EXISTS quotes_by_lead ON quotes(lead_id)")

    def create_lead(self, contact, message, sku=None, escalation=False):
        now = datetime.now(timezone.utc)
        lead = dict(id=uuid4().hex, contact=contact, message=message, sku=sku,
                    created_at=now.isoformat(), status="escalated" if escalation else "new")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("""
                SELECT * FROM leads WHERE contact = ? AND message = ? AND sku IS ?
                AND created_at >= ? ORDER BY created_at DESC LIMIT 1
            """, (contact, message, sku, (now - timedelta(minutes=10)).isoformat())).fetchone()
            if previous:
                lead = dict(previous)
                if escalation and lead["status"] == "new":
                    db.execute("UPDATE leads SET status = 'escalated' WHERE id = ?", (lead["id"],))
                    lead["status"] = "escalated"
                return {**lead, "duplicate": True}
            db.execute(
                "INSERT INTO leads VALUES (:id, :contact, :message, :sku, :created_at, :status)",
                lead,
            )
        return {**lead, "duplicate": False}

    def list_leads(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM leads ORDER BY (status = 'escalated') DESC, created_at DESC LIMIT 100"
            )]

    def create_quote(self, lead_id, items):
        quote_id = uuid4().hex
        total = sum(item["price_minor"] * item["quantity"] for item in items)
        snapshot = json.dumps(sorted(items, key=lambda item: item["sku"]), ensure_ascii=False)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT id FROM quotes WHERE lead_id = ? AND items = ?", (lead_id, snapshot)
            ).fetchone()
            if previous:
                return dict(id=previous["id"], total_minor=total, currency="RUB")
            if not db.execute("SELECT 1 FROM leads WHERE id = ?", (lead_id,)).fetchone():
                raise KeyError(lead_id)
            db.execute("INSERT INTO quotes VALUES (?, ?, ?, ?)",
                       (quote_id, lead_id, snapshot, total))
        return dict(id=quote_id, total_minor=total, currency="RUB")

    def get_quote(self, quote_id):
        with self.connection() as db:
            quote = db.execute("""
                SELECT quotes.*, leads.contact FROM quotes
                JOIN leads ON leads.id = quotes.lead_id WHERE quotes.id = ?
            """, (quote_id,)).fetchone()
        if quote is None:
            raise KeyError(quote_id)

        return {**dict(quote), "items": json.loads(quote["items"])}

    def download_quote(self, quote_id):
        quote = self.get_quote(quote_id)

        def money(amount):
            rubles, kopecks = divmod(amount, 100)
            return f"{rubles},{kopecks:02d} ₽"

        rows = []
        for item in quote["items"]:
            quantity = item["quantity"]
            price = item["price_minor"]
            rows.append(
                f'<tr><td>{escape(item["sku"])}</td><td>{escape(item["name"])}</td>'
                f'<td>{quantity}</td><td>{money(price)}</td>'
                f'<td>{money(quantity * price)}</td></tr>'
            )
        return (
            '<!doctype html><html lang="ru"><meta charset="utf-8">'
            '<title>Коммерческое предложение</title>'
            '<h1>Коммерческое предложение</h1>'
            f'<p>Номер {escape(quote_id)}</p><p>Клиент: {escape(quote["contact"])}</p>'
            '<table border="1" cellpadding="6"><thead><tr><th>Артикул</th>'
            '<th>Товар</th><th>Количество</th><th>Цена</th><th>Сумма</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>'
            f'<p><strong>Итого: {money(quote["total_minor"])}</strong></p></html>'
        )

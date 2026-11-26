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
            db.execute("""
                CREATE TABLE IF NOT EXISTS lead_sessions (
                    lead_id TEXT PRIMARY KEY REFERENCES leads(id), owner TEXT NOT NULL
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS lead_sessions_owner ON lead_sessions(owner)")
            db.execute("""
                CREATE TABLE IF NOT EXISTS demo_sessions (
                    owner TEXT PRIMARY KEY, expires_at TEXT NOT NULL
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS demo_sessions_expiry ON demo_sessions(expires_at)")
            db.execute("""
                CREATE TABLE IF NOT EXISTS session_turns (
                    id INTEGER PRIMARY KEY, owner TEXT NOT NULL,
                    question TEXT NOT NULL, answer TEXT NOT NULL, created_at TEXT NOT NULL
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS session_turns_owner ON session_turns(owner, id)")
            db.execute("""
                CREATE TABLE IF NOT EXISTS lead_activity (
                    id INTEGER PRIMARY KEY, lead_id TEXT NOT NULL REFERENCES leads(id),
                    role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS lead_activity_by_lead ON lead_activity(lead_id, id)")

    def create_lead(self, contact, message, sku=None, escalation=False, owner=None):
        now = datetime.now(timezone.utc)
        lead = dict(id=uuid4().hex, contact=contact, message=message, sku=sku,
                    created_at=now.isoformat(), status="escalated" if escalation else "new")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("""
                SELECT leads.* FROM leads LEFT JOIN lead_sessions ON leads.id = lead_sessions.lead_id
                WHERE contact = ? AND message = ? AND sku IS ? AND lead_sessions.owner IS ?
                AND created_at >= ? ORDER BY created_at DESC LIMIT 1
            """, (contact, message, sku, owner, (now - timedelta(minutes=10)).isoformat())).fetchone()
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
            if owner:
                db.execute("INSERT INTO lead_sessions VALUES (?, ?)", (lead["id"], owner))
                turns = db.execute("""
                    SELECT question, answer, created_at FROM session_turns WHERE owner = ? ORDER BY id
                """, (owner,))
                for turn in turns:
                    db.executemany("""
                        INSERT INTO lead_activity (lead_id, role, content, created_at) VALUES (?, ?, ?, ?)
                    """, [(lead["id"], role, turn[field], turn["created_at"])
                          for role, field in (("client", "question"), ("assistant", "answer"))])
        return {**lead, "duplicate": False}

    def create_session(self, owner, lifetime):
        now = datetime.now(timezone.utc)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                DELETE FROM session_turns WHERE owner IN (
                    SELECT owner FROM demo_sessions WHERE expires_at <= ?
                )
            """, (now.isoformat(),))
            db.execute("DELETE FROM demo_sessions WHERE expires_at <= ?", (now.isoformat(),))
            db.execute("INSERT INTO demo_sessions VALUES (?, ?)",
                       (owner, (now + timedelta(seconds=lifetime)).isoformat()))

    def active_session(self, owner):
        with self.connection() as db:
            return db.execute("""
                SELECT 1 FROM demo_sessions WHERE owner = ? AND expires_at > ?
            """, (owner, datetime.now(timezone.utc).isoformat())).fetchone() is not None

    def chat_history(self, owner):
        with self.connection() as db:
            return [dict(row) for row in db.execute("""
                SELECT question, answer FROM session_turns WHERE owner = ? ORDER BY id
            """, (owner,))]

    def record_turn(self, owner, question, answer):
        with self.connection() as db:
            db.execute("""
                INSERT INTO session_turns (owner, question, answer, created_at) VALUES (?, ?, ?, ?)
            """, (owner, question, answer, datetime.now(timezone.utc).isoformat()))
            db.execute("""
                DELETE FROM session_turns WHERE owner = ? AND id NOT IN (
                    SELECT id FROM session_turns WHERE owner = ? ORDER BY id DESC LIMIT 12
                )
            """, (owner, owner))

    def client_leads(self, owner):
        with self.connection() as db:
            return [dict(row) for row in db.execute("""
                SELECT leads.id, leads.message, leads.sku, leads.created_at, leads.status
                FROM leads JOIN lead_sessions ON leads.id = lead_sessions.lead_id
                WHERE lead_sessions.owner = ? ORDER BY leads.created_at DESC LIMIT 100
            """, (owner,))]

    def lead_detail(self, lead_id, owner=None):
        with self.connection() as db:
            db.execute("BEGIN")
            row = db.execute("""
                SELECT leads.* FROM leads LEFT JOIN lead_sessions ON leads.id = lead_sessions.lead_id
                WHERE leads.id = ? AND (? IS NULL OR lead_sessions.owner = ?)
            """, (lead_id, owner, owner)).fetchone()
            if row is None:
                raise KeyError(lead_id)
            lead = dict(row)
            lead["history"] = [dict(item) for item in db.execute("""
                SELECT role, content, created_at FROM lead_activity WHERE lead_id = ? ORDER BY id
            """, (lead_id,))]
            lead["documents"] = [dict(item) for item in db.execute("""
                SELECT id, total_minor FROM quotes WHERE lead_id = ? ORDER BY rowid DESC
            """, (lead_id,))]
        if owner is not None:
            lead.pop("contact")
        return lead

    def update_lead(self, lead_id, status=None, answer=None):
        now = datetime.now(timezone.utc).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            lead = db.execute("SELECT status FROM leads WHERE id = ?", (lead_id,)).fetchone()
            if lead is None:
                raise KeyError(lead_id)
            if status is not None and status != lead["status"]:
                db.execute("UPDATE leads SET status = ? WHERE id = ?", (status, lead_id))
                db.execute("""
                    INSERT INTO lead_activity (lead_id, role, content, created_at) VALUES (?, 'status', ?, ?)
                """, (lead_id, status, now))
            if answer:
                db.execute("""
                    INSERT INTO lead_activity (lead_id, role, content, created_at) VALUES (?, 'manager', ?, ?)
                """, (lead_id, answer, now))
        return self.lead_detail(lead_id)

    def get_lead(self, lead_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
            if row is None:
                raise KeyError(lead_id)
            return dict(row)

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

    def get_quote(self, quote_id, owner=None):
        with self.connection() as db:
            quote = db.execute("""
                SELECT quotes.*, leads.contact FROM quotes
                JOIN leads ON leads.id = quotes.lead_id
                LEFT JOIN lead_sessions ON lead_sessions.lead_id = leads.id
                WHERE quotes.id = ? AND (? IS NULL OR lead_sessions.owner = ?)
            """, (quote_id, owner, owner)).fetchone()
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

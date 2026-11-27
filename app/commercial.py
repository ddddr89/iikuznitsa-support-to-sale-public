import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from html import escape
from uuid import uuid4


# Старые записи читаются без перезаписи. Для исторического «В работе»
# без ответа невозможно отличить начальную эскалацию от обычной заявки.
WAITING_FOR_REPLY = """
    COALESCE(leads.requires_reply, leads.status IN ('escalated', 'in_progress')
        AND NOT EXISTS (
            SELECT 1 FROM lead_activity
            WHERE lead_id = leads.id AND role = 'manager'
        ))
"""


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
            db.execute("""
                CREATE TABLE IF NOT EXISTS quote_pdfs (
                    quote_id TEXT PRIMARY KEY REFERENCES quotes(id),
                    quote_pdf BLOB NOT NULL, invoice_pdf BLOB NOT NULL
                )
            """)
            fields = {field["name"] for field in db.execute("PRAGMA table_info(leads)")}
            if "status" not in fields:
                db.execute("ALTER TABLE leads ADD COLUMN status TEXT NOT NULL DEFAULT 'new'")
            if "requires_reply" not in fields:
                db.execute("ALTER TABLE leads ADD COLUMN requires_reply INTEGER CHECK (requires_reply IN (0, 1))")
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
        """Сохраняет заявку и копирует историю владельца при создании новой записи."""
        now = datetime.now(timezone.utc)
        lead = dict(id=uuid4().hex, contact=contact, message=message, sku=sku,
                    created_at=now.isoformat(), status="escalated" if escalation else "new",
                    requires_reply=escalation)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("""
                SELECT leads.id, contact, message, sku, created_at, status,
                       {WAITING_FOR_REPLY} AS requires_reply
                FROM leads LEFT JOIN lead_sessions ON leads.id = lead_sessions.lead_id
                WHERE contact = ? AND message = ? AND sku IS ? AND lead_sessions.owner IS ?
                AND created_at >= ? AND (NOT ? OR status IN ('new', 'escalated', 'in_progress'))
                ORDER BY created_at DESC LIMIT 1
            """.format(WAITING_FOR_REPLY=WAITING_FOR_REPLY),
                (contact, message, sku, owner, (now - timedelta(minutes=10)).isoformat(), escalation)).fetchone()
            if previous:
                lead = dict(previous)
                lead["requires_reply"] = bool(lead["requires_reply"])
                if escalation and not lead["requires_reply"]:
                    if lead["status"] == "new":
                        lead["status"] = "escalated"
                    db.execute("UPDATE leads SET status = ?, requires_reply = 1 WHERE id = ?",
                               (lead["status"], lead["id"]))
                    lead["requires_reply"] = True
                return {**lead, "duplicate": True}
            db.execute(
                """INSERT INTO leads (id, contact, message, sku, created_at, status, requires_reply)
                   VALUES (:id, :contact, :message, :sku, :created_at, :status, :requires_reply)""",
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

    def chat_history(self, owner, include_times=False):
        with self.connection() as db:
            turns = [dict(row) for row in db.execute("""
                SELECT question, answer, created_at FROM session_turns WHERE owner = ? ORDER BY id
            """, (owner,))]
        if not include_times:
            for turn in turns:
                turn.pop("created_at")
        return turns

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

    def manager_messages(self, owner, after):
        """Возвращает ответы менеджера по заявкам владельца после указанного ID."""
        with self.connection() as db:
            return [dict(row) for row in db.execute("""
                SELECT activity.id, activity.content, activity.created_at
                FROM lead_activity AS activity
                JOIN lead_sessions AS session ON session.lead_id = activity.lead_id
                WHERE session.owner = ? AND activity.role = 'manager' AND activity.id > ?
                ORDER BY activity.id LIMIT 100
            """, (owner, after))]

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
                SELECT leads.id, contact, message, sku, created_at, status,
                       {WAITING_FOR_REPLY} AS requires_reply
                FROM leads LEFT JOIN lead_sessions ON leads.id = lead_sessions.lead_id
                WHERE leads.id = ? AND (? IS NULL OR lead_sessions.owner = ?)
            """.format(WAITING_FOR_REPLY=WAITING_FOR_REPLY), (lead_id, owner, owner)).fetchone()
            if row is None:
                raise KeyError(lead_id)
            lead = dict(row)
            lead["requires_reply"] = bool(lead["requires_reply"])
            lead["history"] = [dict(item) for item in db.execute("""
                SELECT role, content, created_at FROM lead_activity
                WHERE lead_id = ? AND (? IS NULL OR role != 'closure_reason') ORDER BY id
            """, (lead_id, owner))]
            lead["documents"] = [dict(item) for item in db.execute("""
                SELECT id, total_minor FROM quotes WHERE lead_id = ? ORDER BY rowid DESC
            """, (lead_id,))]
        if owner is not None:
            lead.pop("contact")
        return lead

    def update_lead(self, lead_id, status=None, answer=None, closure_reason=None):
        now = datetime.now(timezone.utc).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            lead = db.execute(f"SELECT status, {WAITING_FOR_REPLY} AS pending FROM leads WHERE id = ?",
                              (lead_id,)).fetchone()
            if lead is None:
                raise KeyError(lead_id)
            waiting = bool(lead["pending"])
            if status == "closed" and waiting and not (closure_reason or answer):
                raise ValueError("Укажите причину закрытия обращения без ответа")
            if status == "answered" and not answer and (waiting or not db.execute("""
                SELECT 1 FROM lead_activity WHERE lead_id = ? AND role = 'manager'
            """, (lead_id,)).fetchone()):
                raise ValueError("Введите ответ клиенту")
            if closure_reason and status != "closed":
                raise ValueError("Причину можно сохранить при закрытии обращения")
            if answer or status == "closed":
                waiting = False
            elif status == "escalated":
                waiting = True
            if answer and status != "closed":
                status = "answered"
            db.execute("UPDATE leads SET status = ?, requires_reply = ? WHERE id = ?",
                       (status or lead["status"], waiting, lead_id))
            if status is not None and status != lead["status"]:
                db.execute("""
                    INSERT INTO lead_activity (lead_id, role, content, created_at) VALUES (?, 'status', ?, ?)
                """, (lead_id, status, now))
            if answer:
                db.execute("""
                    INSERT INTO lead_activity (lead_id, role, content, created_at) VALUES (?, 'manager', ?, ?)
                """, (lead_id, answer, now))
            if closure_reason:
                db.execute("""
                    INSERT INTO lead_activity (lead_id, role, content, created_at)
                    VALUES (?, 'closure_reason', ?, ?)
                """, (lead_id, closure_reason, now))
        return self.lead_detail(lead_id)

    def get_lead(self, lead_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
            if row is None:
                raise KeyError(lead_id)
            return dict(row)

    def lead_queue(self, page=1, limit=100, view="all"):
        """Читает страницу и глобальный счётчик ожидания из одного снимка."""
        where = {
            "all": "1",
            "open": "status != 'closed'",
            "closed": "status = 'closed'",
            "waiting": WAITING_FOR_REPLY,
        }[view]
        with self.connection() as db:
            db.execute("BEGIN")
            count = db.execute(f"SELECT count(*) FROM leads WHERE {WAITING_FOR_REPLY}").fetchone()[0]
            total = count if view == "waiting" else db.execute(
                f"SELECT count(*) FROM leads WHERE {where}"
            ).fetchone()[0]
            pages = max(1, (total + limit - 1) // limit)
            page = min(page, pages)
            items = [dict(row) for row in db.execute(
                f"""SELECT id, contact, message, sku, created_at, status,
                           {WAITING_FOR_REPLY} AS requires_reply
                    FROM leads WHERE {where}
                    ORDER BY requires_reply DESC, created_at DESC, id LIMIT ? OFFSET ?""",
                (limit, (page - 1) * limit),
            )]
            for item in items:
                item["requires_reply"] = bool(item["requires_reply"])
            return {"items": items, "waiting_count": count, "total": total,
                    "page": page, "pages": pages}

    def list_leads(self):
        return self.lead_queue()["items"]

    def create_quote(self, lead_id, items, render):
        """Сохраняет снимок позиций и оба PDF в одной транзакции."""
        total = sum(item["price_minor"] * item["quantity"] for item in items)
        lines = sorted(items, key=lambda item: item["sku"])
        snapshot = json.dumps(lines, ensure_ascii=False)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            lead = db.execute("SELECT contact FROM leads WHERE id = ?", (lead_id,)).fetchone()
            if lead is None:
                raise KeyError(lead_id)
            previous = db.execute(
                "SELECT id FROM quotes WHERE lead_id = ? AND items = ?", (lead_id, snapshot)
            ).fetchone()
            quote_id = previous["id"] if previous else uuid4().hex
            if not previous:
                db.execute("INSERT INTO quotes VALUES (?, ?, ?, ?)", (quote_id, lead_id, snapshot, total))
            if not db.execute("SELECT 1 FROM quote_pdfs WHERE quote_id = ?", (quote_id,)).fetchone():
                payload = dict(id=quote_id, contact=lead["contact"], items=lines, total_minor=total)
                documents = [render(payload, kind) for kind in ("quote", "invoice")]
                db.execute("INSERT INTO quote_pdfs VALUES (?, ?, ?)", (quote_id, *documents))
        return dict(id=quote_id, total_minor=total, currency="RUB")

    def quote_pdf(self, quote_id, kind):
        with self.connection() as db:
            row = db.execute("SELECT quote_pdf, invoice_pdf FROM quote_pdfs WHERE quote_id = ?",
                             (quote_id,)).fetchone()
        return row[kind + "_pdf"] if row is not None else None

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

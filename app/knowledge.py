"""Local support retrieval. Chat records are reduced to coarse topic counts."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.etree import ElementTree
from zipfile import ZipFile

HOSTS = {"docs.softline.com", "docs.softline.ru", "store.softline.ru"}
DELIVERY = "https://store.softline.ru/delivery_info/"
DOCS = "https://docs.softline.com/ru-ru"
PAYMENT = f"{DOCS}/payment"
SUBSCRIPTIONS = f"{DOCS}/subscriptions"
TOPICS = (
    ("Доставка и получение", ("достав", "самовывоз", "пвз", "курьер", "получени"), DELIVERY),
    ("Оплата и заказ", ("оплат", "платёж", "платеж", "заказ", "счёт", "счет"), PAYMENT),
    ("Лицензирование", ("лицензи", "активаци", "подписк", "ключ"), SUBSCRIPTIONS),
    ("Возврат и гарантия", ("возврат", "гаранти", "неисправн", "обмен"), DELIVERY),
    ("Документация и поддержка", ("документац", "инструкци", "настро", "поддержк"), DOCS),
)
STOPWORDS = {"как", "для", "мне", "можно", "будет", "какие", "какой", "когда",
             "это", "если", "где", "найти", "подскажите", "про", "пожалуйста"}
LIMIT_BYTES = 3 * 1024 * 1024
AUTO_METHOD = "official_excerpt_after_completion"
# Для автоматической записи подходят только конкретные стабильные справочные
# факты. Канонические тексты проверяются по уже загруженному первичному источнику.
ORDER_STATUS = "order_status"
ORDER_STATUS_TITLE = "Как узнать о состоянии исполнения заказа?"
ORDER_STATUS_FACT = (
    "Вы сможете перейти по ссылке из письма в Личный кабинет Softline "
    "и узнать, на какой стадии находится ваш заказ."
)
ORDER_START = "order_start"
AUTO_TOPICS = {
    ORDER_STATUS: (ORDER_STATUS_TITLE, ORDER_STATUS_FACT, DELIVERY),
    ORDER_START: (
        "Как оформить заказ на сайте?",
        "Для оформления заказа нужно нажать на кнопку «В корзину» на сайте рядом с выбранным продуктом. "
        "Далее на странице «Корзина» перейти к оформлению заказа через кнопку «Оформить заказ».",
        DELIVERY,
    ),
}
ORDER_STATUS_QUERY = re.compile(
    r"(?:(?:где|как) (?:посмотреть|проверить|узнать|отследить) "
    r"(?:статус|состояние(?: исполнения)?|стадию исполнения) "
    r"(?:(?:моего|своего) )?(?:ранее оформленного |уже оформленного )?заказа|"
    r"где (?:виден|отображается) статус заказа|"
    r"покажите где посмотреть статус заказа|как узнать о состоянии исполнения заказа)"
)
ORDER_START_QUERY = re.compile(
    r"(?:как (?:начать оформление|оформить|начать оформлять) заказ на сайте(?: softline)?|"
    r"как перейти к оформлению заказа из корзины)"
)
COMPLETION = re.compile(
    r"(?:спасибо )?(?:вопрос решен|проблема решена|это помогло|это решило мой вопрос)"
)


def normalize_automatic_request(text):
    return " ".join(re.sub(r"[?!.;,]+", " ", text.casefold().replace("ё", "е")).split())


def automatic_topic(text):
    normalized = normalize_automatic_request(text)
    if ORDER_STATUS_QUERY.fullmatch(normalized):
        return ORDER_STATUS
    if ORDER_START_QUERY.fullmatch(normalized):
        return ORDER_START
    return None


def official_url(url):
    parsed = urlsplit(url)
    return (parsed.scheme == "https" and parsed.netloc in HOSTS and not parsed.query
            and not parsed.fragment and not parsed.username)


class OfficialRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not official_url(newurl):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Sections(HTMLParser):
    """Извлекает видимый справочный текст из основного содержимого страницы."""

    heading_tags = {"h1", "h2", "h3", "h4"}
    blocks = heading_tags | {"p", "li", "td", "dt", "dd"}
    ignored = {"script", "style", "svg", "noscript", "nav", "footer"}

    def __init__(self):
        super().__init__()
        self.main = False
        self.skip = None
        self.block = None
        self.text = []
        self.heading = None
        self.rows = []

    def handle_starttag(self, tag, attrs):
        if tag == "main":
            self.main = True
        if self.skip is not None:
            return
        if tag in self.ignored:
            self.skip = tag
        elif self.main and tag in self.blocks and self.block is None:
            self.block = tag
            self.text = []

    def handle_data(self, data):
        if self.block and self.skip is None:
            self.text.append(data)

    def handle_endtag(self, tag):
        if self.skip is not None:
            if tag == self.skip:
                self.skip = None
            return
        if tag == self.block:
            text = " ".join("".join(self.text).split())
            if tag in self.heading_tags and text:
                self.heading = text[:180]
            elif self.heading and len(text) >= 36:
                for start in range(0, len(text), 420):
                    self.rows.append((self.heading, text[start:start + 420]))
            self.block = None
        elif tag == "main":
            self.main = False


def fetch_sections(url):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"})
    with build_opener(OfficialRedirect()).open(req, timeout=12) as response:
        if response.headers.get_content_type() != "text/html":
            raise ValueError("Unsupported official page format")
        data = response.read(LIMIT_BYTES + 1)
        if len(data) > LIMIT_BYTES:
            raise ValueError("Official page too large")
        html = data.decode(response.headers.get_content_charset() or "utf-8", errors="replace")
    parser = Sections()
    parser.feed(html)
    return parser.rows


def docx_links(path):
    with ZipFile(path) as archive:
        info = archive.getinfo("word/document.xml")
        if info.file_size > LIMIT_BYTES:
            raise ValueError("Knowledge document too large")
        content = ElementTree.fromstring(archive.read(info))
    urls = {}
    for paragraph in content.findall(".//{*}p"):
        text = "".join(node.text or "" for node in paragraph.findall(".//{*}t"))
        for url in re.findall(r"https://[^\s<>]+", text):
            if official_url(url):
                urls[url] = None
    return list(urls)


def chat_topics(paths):
    """Считает темы переписок и возвращает агрегаты без исходных реплик."""
    records = 0
    counts = [0] * len(TOPICS)
    for path in paths:
        with path.open(encoding="utf-8-sig") as source:
            for line in source:
                line = line.strip()
                if not line:
                    continue
                records += 1
                lowered = line.lower().replace("ё", "е")
                for index, (_, terms, _) in enumerate(TOPICS):
                    if any(term.replace("ё", "е") in lowered for term in terms):
                        counts[index] += 1
    return records, counts


class Knowledge:
    def __init__(self, db_path):
        self.db_path = db_path

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def init(self):
        with self.connection() as db:
            db.executescript("""
                CREATE VIRTUAL TABLE IF NOT EXISTS support_knowledge USING fts5(
                    title, excerpt, url UNINDEXED, tokenize='unicode61'
                );
                CREATE TABLE IF NOT EXISTS support_confirmed (
                    id INTEGER PRIMARY KEY, lead_id TEXT NOT NULL UNIQUE,
                    question TEXT NOT NULL, answer TEXT NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS support_confirmed_search USING fts5(
                    title, excerpt, tokenize='unicode61'
                );
                CREATE TABLE IF NOT EXISTS support_intents (
                    topic TEXT PRIMARY KEY, terms TEXT NOT NULL,
                    url TEXT NOT NULL, records INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS support_snapshot (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    pages INTEGER NOT NULL, excerpts INTEGER NOT NULL,
                    chat_lines INTEGER NOT NULL, chat_files INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS support_automatic (
                    topic TEXT PRIMARY KEY,
                    title TEXT NOT NULL, excerpt TEXT NOT NULL, url TEXT NOT NULL,
                    created_at TEXT NOT NULL, method TEXT NOT NULL
                );
            """)

    def ingest(self, directory):
        paths = sorted((directory / "чаты поддержки").glob("*_masked.txt"))
        links = docx_links(directory / "Базы знаний.docx")
        if not links or not paths:
            raise ValueError("Knowledge links or masked chat files missing")
        records, counts = chat_topics(paths)
        if DOCS in links:
            links = list(dict.fromkeys([*links, PAYMENT, SUBSCRIPTIONS]))
        sections = []
        page_count = 0
        for link in links:
            rows = fetch_sections(link)
            sections.extend((title, excerpt, link) for title, excerpt in rows[:150])
            page_count += bool(rows)
        if not sections or not records:
            raise ValueError("No knowledge excerpts or chat records found")
        snapshot = (page_count, len(sections), records, len(paths))
        self.init()
        with self.connection() as db:
            db.execute("DELETE FROM support_knowledge")
            db.execute("DELETE FROM support_intents")
            db.executemany("INSERT INTO support_knowledge VALUES (?, ?, ?)", sections)
            db.executemany("INSERT INTO support_intents VALUES (?, ?, ?, ?)", [
                (topic, " ".join(terms), url, count)
                for (topic, terms, url), count in zip(TOPICS, counts) if count
            ])
            db.execute("INSERT OR REPLACE INTO support_snapshot VALUES (1, ?, ?, ?, ?)", snapshot)
        return self.status()

    def status(self):
        with self.connection() as db:
            row = db.execute(
                "SELECT pages, excerpts, chat_lines, chat_files FROM support_snapshot"
            ).fetchone()
        return dict(row) if row else dict(pages=0, excerpts=0, chat_lines=0, chat_files=0)

    def confirm(self, lead_id, question, answer):
        with self.connection() as db:
            record = db.execute("""
                INSERT INTO support_confirmed (lead_id, question, answer) VALUES (?, ?, ?)
                ON CONFLICT(lead_id) DO UPDATE SET question=excluded.question, answer=excluded.answer
                RETURNING id
            """, (lead_id, question, answer)).fetchone()
            entry_id = record["id"]
            db.execute("DELETE FROM support_confirmed_search WHERE rowid = ?", (entry_id,))
            db.execute("INSERT INTO support_confirmed_search (rowid, title, excerpt) VALUES (?, ?, ?)",
                       (entry_id, question, answer))
            return entry_id

    def _official_fact(self, db, topic):
        title, excerpt, url = AUTO_TOPICS[topic]
        if not official_url(url):
            return None
        excerpts = db.execute("""
            SELECT excerpt FROM support_knowledge WHERE title = ? AND url = ?
        """, (title, url))
        if not any(row["excerpt"] == excerpt or row["excerpt"].startswith(excerpt + " ") for row in excerpts):
            return None
        # Модераторская запись для той же темы имеет приоритет. Расхождение
        # блокирует автоответ до ручной сверки, даже если он уже был сохранён.
        for row in db.execute("SELECT question, answer FROM support_confirmed"):
            if automatic_topic(row["question"]) == topic and row["answer"].strip() != excerpt:
                return None
        return {"title": title, "excerpt": excerpt, "url": url, "kind": "knowledge_automatic"}

    def official_candidate(self, message):
        """Отдаёт сверенный официальный фрагмент для первого обращения."""
        if (topic := automatic_topic(message)) is not None:
            with self.connection() as db:
                if fact := self._official_fact(db, topic):
                    return {**fact, "kind": "knowledge_source"}
        return None

    def learn_from_completion(self, message, trusted_history):
        """Сохраняет канонический факт после явного подтверждения решения.

        История должна читаться сервером для текущей сессии. Последнее
        сообщение клиента, текст диалога и идентификаторы в запись не входят.
        """
        if "?" in message or not COMPLETION.fullmatch(normalize_automatic_request(message)) or not trusted_history:
            return None
        previous = trusted_history[-1]
        question, answer = previous["question"], previous["answer"]
        if (topic := automatic_topic(question)) is None:
            return None
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            fact = self._official_fact(db, topic)
            if fact is None or answer.strip() != fact["excerpt"]:
                return None
            inserted = db.execute("""
                INSERT INTO support_automatic (topic, title, excerpt, url, created_at, method)
                VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(topic) DO NOTHING
            """, (topic, fact["title"], fact["excerpt"], fact["url"],
                  datetime.now(timezone.utc).isoformat(), AUTO_METHOD))
            return fact if inserted.rowcount else None

    def automatic_answer(self, message):
        """Возвращает выученный ответ при повторной проверке факта и источника."""
        if (topic := automatic_topic(message)) is None:
            return None
        with self.connection() as db:
            db.execute("BEGIN")
            fact = self._official_fact(db, topic)
            stored = db.execute("""
                SELECT title, excerpt, url FROM support_automatic WHERE topic = ? AND method = ?
            """, (topic, AUTO_METHOD)).fetchone()
            if fact and stored and all(fact[key] == stored[key] for key in stored.keys()):
                return fact
        return None

    def search(self, message):
        """Подбирает подтверждённый ответ и до трёх справочных выдержек суммарно."""
        tokens = [
            word for word in dict.fromkeys(re.findall(r"[^\W_]+", message.lower()))
            if len(word) >= 3 and word not in STOPWORDS and not word.isnumeric()
        ][:12]
        if not tokens:
            return []
        stems = list(dict.fromkeys(
            term for _, terms, _ in TOPICS for term in terms
            if any(word.startswith(term) and word != term for word in tokens)
        ))
        query = " OR ".join(f'"{word}"*' for word in [*tokens, *stems])
        with self.connection() as db:
            reviewed = db.execute("""
                SELECT title, excerpt FROM support_confirmed_search
                WHERE support_confirmed_search MATCH ?
                ORDER BY bm25(support_confirmed_search, 5, 1) LIMIT 1
            """, (" AND ".join(f'"{word}"*' for word in tokens),)).fetchall()
            rows = db.execute("""
                SELECT title, excerpt, url FROM support_knowledge
                WHERE support_knowledge MATCH ?
                ORDER BY bm25(support_knowledge, 4, 1) LIMIT 3
            """, (query,)).fetchall()
            results = [{"title": row["title"], "excerpt": row["excerpt"], "url": None,
                        "kind": "manager_confirmed"} for row in reviewed]
            results.extend(dict(row) for row in rows[:3 - len(results)])
            if results:
                return results
            for intent in db.execute("SELECT * FROM support_intents ORDER BY records DESC"):
                if any(word.startswith(term) for word in tokens for term in intent["terms"].split()):
                    return [{"title": intent["topic"], "url": intent["url"],
                             "excerpt": "Официальный справочный раздел по теме обращения."}]
        return []


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("materials", type=Path)
    args = parser.parse_args()
    try:
        status = Knowledge(args.database).ingest(args.materials)
    except (OSError, ValueError, KeyError, sqlite3.Error) as error:
        parser.exit(1, f"Не удалось обновить базу знаний: {type(error).__name__}\n")
    print("База знаний обновлена:", status)


if __name__ == "__main__":
    main()

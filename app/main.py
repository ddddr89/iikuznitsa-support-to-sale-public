import os
import re
from collections import deque
from contextlib import asynccontextmanager
from hashlib import sha256
from html import unescape
from pathlib import Path
from secrets import compare_digest, token_urlsafe
from threading import Lock
from time import monotonic
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.ai import reply
from app.catalog import Catalog
from app.commercial import Commercial
from app.documents import render_pdf
from app.knowledge import Knowledge

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
Contact = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=320)]
Sku = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
STATIC = Path(__file__).with_name("static")
SESSION_COOKIE = "__Host-support_session"
SESSION_LIFETIME = 7 * 86400
LeadStatus = Literal["new", "escalated", "in_progress", "answered", "closed"]


def session_digest(request: Request):
    token = request.cookies.get(SESSION_COOKIE, "")
    if re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        return sha256(token.encode()).hexdigest()
    return None



class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Turn(Input):
    question: Text
    answer: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


class Chat(Input):
    message: Text
    history: Annotated[list[Turn], Field(max_length=4)] = Field(default_factory=list)


class Lead(Input):
    message: Text
    contact: Contact
    sku: Sku | None = None
    escalation: bool = False


class QuoteItem(Input):
    sku: Sku
    quantity: Annotated[int, Field(strict=True, ge=1, le=999)]


class Quote(Input):
    lead_id: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")]
    items: Annotated[list[QuoteItem], Field(min_length=1, max_length=50)]


class LeadUpdate(Input):
    status: LeadStatus | None = None
    answer: Text | None = None


class ConfirmedAnswer(Input):
    question: Text
    answer: Text
    anonymized: Literal[True]


def require_manager(
    authorization: Annotated[HTTPAuthorizationCredentials | None, Depends(HTTPBearer(auto_error=False))],
):
    token = os.getenv("MANAGER_TOKEN")
    if not token:
        raise HTTPException(503, "Кабинет менеджера отключён")
    if authorization is None or not compare_digest(authorization.credentials.encode(), token.encode()):
        raise HTTPException(401, "Требуется токен менеджера", {"WWW-Authenticate": "Bearer"})


def create_app(db_path=None):
    path = db_path or os.getenv("APP_DB_PATH", "support.sqlite3")
    catalog = Catalog(path)
    commercial = Commercial(path)
    knowledge = Knowledge(os.getenv("APP_KNOWLEDGE_DB") or path)

    def session_owner(request: Request):
        owner = session_digest(request)
        return owner if owner and commercial.active_session(owner) else None

    def require_session(request: Request):
        owner = session_owner(request)
        if owner is None:
            raise HTTPException(404, "Обращение не найдено")
        return owner
    chat_max = int(os.getenv("APP_CHAT_PER_MINUTE", "0"))
    chat_times = deque()
    chat_lock = Lock()

    @asynccontextmanager
    async def lifespan(_app):
        catalog.init()
        commercial.init()
        knowledge.init()
        yield

    api = FastAPI(title="Support-to-Sale AI", lifespan=lifespan)

    @api.middleware("http")
    async def prevent_private_caching(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith(("/api/v1/client/", "/api/v1/manager/", "/api/v1/leads")):
            response.headers["Cache-Control"] = "no-store"
        return response
    api.mount("/static", StaticFiles(directory=STATIC), name="static")

    @api.get("/", include_in_schema=False)
    def home():
        return FileResponse(STATIC / "index.html")

    @api.get("/client", include_in_schema=False)
    def client():
        return FileResponse(STATIC / "client.html")

    @api.get("/manager", include_in_schema=False)
    def manager():
        return FileResponse(STATIC / "manager.html")

    @api.get("/api/v1/health")
    def health():
        return {"status": "ok"}

    @api.get("/api/v1/catalog/status")
    def catalog_status():
        return catalog.status()

    @api.get("/api/v1/products")
    def products(q: str = Query("", max_length=200),
                 limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=2**63-1)):
        return {"items": catalog.search(q, limit, offset)}

    @api.post("/api/v1/chat")
    def chat(body: Chat, request: Request):
        if chat_max > 0:
            with chat_lock:
                now = monotonic()
                while chat_times and chat_times[0] <= now - 60:
                    chat_times.popleft()
                if len(chat_times) >= chat_max:
                    raise HTTPException(429, "Лимит запросов к помощнику", {"Retry-After": "60"})
                chat_times.append(now)
        products = catalog.search(body.message, 5)
        if not products and body.history:
            products = catalog.search(body.history[-1].question, 5)
        resources = knowledge.search(body.message)
        confirmed = resources[0] if resources and resources[0].get("kind") == "manager_confirmed" else None
        if confirmed and confirmed["title"].casefold() == body.message.casefold():
            result = {"answer": confirmed["excerpt"], "provider": "knowledge", "actions": []}
        else:
            result = reply(
                body.message, products, search=catalog.search, knowledge=resources,
                history=[item.model_dump() for item in body.history],
            )
            if confirmed and result["provider"] == "catalog":
                result["answer"] = confirmed["excerpt"]
        actions = [{"type": "catalog_search", "status": "done"},
                   {"type": "knowledge_search", "status": "done"}]
        owner = session_owner(request)
        if owner:
            commercial.record_turn(owner, body.message, result["answer"])
        if confirmed:
            actions.append({"type": "knowledge_confirmed", "status": "done"})
        return {
            "products": products,
            **result,
            "actions": [*actions, *result["actions"]],
            "sources": [{"title": item["title"], "url": item["url"]}
                        for item in resources if item.get("url")],
        }

    @api.post("/api/v1/leads", status_code=201)
    def create_lead(body: Lead, request: Request):
        if body.sku and catalog.get(body.sku) is None:
            raise HTTPException(404, "Товар не найден")
        return commercial.create_lead(
            body.contact, body.message, body.sku, body.escalation, session_owner(request),
        )

    @api.get("/api/v1/client/leads")
    def client_leads(request: Request, response: Response):
        owner = session_owner(request)
        if owner is None:
            if request.headers.get("sec-fetch-site") in ("cross-site", "same-site"):
                raise HTTPException(403, "Откройте раздел клиента на сайте")
            token = token_urlsafe(32)
            commercial.create_session(sha256(token.encode()).hexdigest(), SESSION_LIFETIME)
            response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_LIFETIME, secure=True,
                                httponly=True, samesite="strict", path="/")
            return {"items": []}
        return {"items": commercial.client_leads(owner)}

    @api.get("/api/v1/client/leads/{lead_id}")
    def client_lead(lead_id: str, owner: Annotated[str, Depends(require_session)]):
        try:
            return commercial.lead_detail(lead_id, owner)
        except KeyError as exc:
            raise HTTPException(404, "Обращение не найдено") from exc

    @api.get("/api/v1/client/quotes/{quote_id}/{kind}.pdf")
    def client_quote(quote_id: str, kind: Literal["quote", "invoice"],
                     owner: Annotated[str, Depends(require_session)]):
        return pdf_response(quote_id, kind, owner)

    @api.get("/api/v1/manager/leads", dependencies=[Depends(require_manager)])
    def leads():
        return {"items": commercial.list_leads()}

    @api.get("/api/v1/manager/leads/{lead_id}", dependencies=[Depends(require_manager)])
    def manager_lead(lead_id: str):
        try:
            return commercial.lead_detail(lead_id)
        except KeyError as exc:
            raise HTTPException(404, "Обращение не найдено") from exc

    @api.patch("/api/v1/manager/leads/{lead_id}", dependencies=[Depends(require_manager)])
    def update_lead(lead_id: str, body: LeadUpdate):
        if body.answer is None and body.status is None:
            raise HTTPException(422, "Укажите ответ или статус")
        try:
            return commercial.update_lead(lead_id, body.status, body.answer)
        except KeyError as exc:
            raise HTTPException(404, "Обращение не найдено") from exc

    @api.put("/api/v1/manager/leads/{lead_id}/knowledge", dependencies=[Depends(require_manager)])
    def confirm_answer(lead_id: str, body: ConfirmedAnswer):
        try:
            lead = commercial.get_lead(lead_id)
        except KeyError as exc:
            raise HTTPException(404, "Заявка не найдена") from exc
        text = f"{body.question} {body.answer}".casefold()
        contact = lead["contact"].strip().casefold()
        if (len(contact) >= 3 and contact in text) or re.search(
            r"[^\s@]+@[^\s@]+\.[^\s@]+|(?<!\d)\+?\d[\d ()-]{8,}\d(?!\d)", text
        ):
            raise HTTPException(422, "Удалите контактные данные из вопроса и ответа")
        return {"id": knowledge.confirm(lead_id, body.question, body.answer), "status": "confirmed"}

    @api.post("/api/v1/manager/quotes", status_code=201, dependencies=[Depends(require_manager)])
    def create_quote(body: Quote):
        skus = [line.sku for line in body.items]
        if len(set(skus)) != len(skus):
            raise HTTPException(422, "Объедините количество для одинаковых артикулов")
        products = catalog.get_many(skus)
        items = []
        for line in body.items:
            product = products.get(line.sku)
            if product is None:
                raise HTTPException(404, "Товар не найден")
            if product["price_minor"] is None:
                raise HTTPException(409, "Цена товара по запросу")
            items.append(dict(sku=line.sku, name=product["name"], quantity=line.quantity,
                              price_minor=product["price_minor"]))
        try:
            quote = commercial.create_quote(body.lead_id, items)
        except KeyError as exc:
            raise HTTPException(404, "Заявка не найдена") from exc
        base = f'/api/v1/manager/quotes/{quote["id"]}'
        return {**quote, "download_url": f"{base}/download",
                "quote_pdf_url": f"{base}/quote.pdf", "invoice_pdf_url": f"{base}/invoice.pdf"}

    @api.get("/api/v1/manager/quotes/{quote_id}/download", dependencies=[Depends(require_manager)])
    def download(quote_id: str):
        try:
            document = commercial.download_quote(quote_id)
        except KeyError as exc:
            raise HTTPException(404, "КП не найдено") from exc
        return HTMLResponse(document, headers={
            "Content-Disposition": f'attachment; filename="quote-{quote_id}.html"',
            "Cache-Control": "no-store",
        })

    @api.get("/api/v1/manager/quotes/{quote_id}/{kind}.pdf", dependencies=[Depends(require_manager)])
    def download_pdf(quote_id: str, kind: Literal["quote", "invoice"]):
        return pdf_response(quote_id, kind)

    def pdf_response(quote_id, kind, owner=None):
        try:
            quote = commercial.get_quote(quote_id, owner)
            for item in quote["items"]:
                item["name"] = unescape(unescape(item["name"]))
        except KeyError as exc:
            raise HTTPException(404, "КП не найдено") from exc
        return Response(render_pdf(quote, kind), media_type="application/pdf", headers={
            "Content-Disposition": f'attachment; filename="{kind}-{quote["id"]}.pdf"',
            "Cache-Control": "no-store",
        })

    return api


app = create_app()

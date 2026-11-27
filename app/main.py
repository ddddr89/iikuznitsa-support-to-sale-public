"""HTTP API чата, каталога, контакта с менеджером и защищённых документов."""

import logging
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
from urllib.parse import quote as url_quote

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.ai import reply
from app.automatic_documents import document_quantity_followup, document_request, quantity_in, recently_shown
from app.catalog import Catalog
from app.catalog_advice import (
    DELIVERY, advisory, checked_model_answer, compare_monitor_models, delivery_details_followup, document_confirmation,
    price_text, purchase_with_delivery, quantity_label, scoped_answer,
)
from app.catalog_matching import MODEL_DETAIL, normalize_codes, product_scope
from app.commercial import Commercial
from app.consultation import FOLLOW_PRODUCT, PURCHASE_CONTINUE, REJECTION, SERVICE_TOPIC, active_turns, mail_consultation
from app.documents import PDF_FILENAMES, package_documents, render_pdf
from app.knowledge import Knowledge
from app.license_advice import NO_MATCH, license_answer

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
Contact = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=320)]
Sku = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
STATIC = Path(__file__).with_name("static")
CHAT_LOG = logging.getLogger("uvicorn.error")
ACTION_NAME = re.compile(r"[a-z_]{1,40}")
SESSION_COOKIE = "__Host-support_session"
SESSION_LIFETIME = 7 * 86400
LeadStatus = Literal["new", "escalated", "in_progress", "answered", "closed"]


OPERATOR_REQUEST = re.compile(
    r"(?<!не )\b(?:позовите|позови|подключите|подключи|соедините|свяжите|переведите|"
    r"передайте|поговорить)\b"
    r"[^.!?]{0,60}\b(?:оператор\w*|менеджер\w*|человек\w*)\b|"
    # «Нужна защита на 40 человек» задаёт размер лицензии, а не просит оператора.
    r"(?<!не )\b(?:нужен|нужна|хочу)\s+"
    r"(?:(?:жив\w*|реальн\w*|помощь|консультаци\w*)\s+)?"
    r"(?:оператор\w*|менеджер\w*|человек(?:а)?)\b|"
    r"^\s*(?:оператор|менеджер)(?:а)?[.!?\s]*$",
    re.IGNORECASE,
)
UNRESOLVED_CATALOG = (
    NO_MATCH,
    "В каталоге не нашлось товаров",
    "Не нашлось ноутбука",
    "Подтверждённого предложения",
    "В каталоге не удалось подтвердить",
    "В каталоге нет подтверждённого совпадения",
    "Не удалось подтвердить все параметры",
    "Каталог не подтверждает одновременно",
    "Подтверждённую поставку лицензии в каталоге найти не удалось",
)
SYSTEM_CONTACT = "Контакт не указан · ответ в чате"
CARD_CAPACITY = re.compile(r"\b(?:на\s+)?(\d+(?:[.,]\d+)?)\s*(тб|tb|гб|gb)\b")


def render_documents(quote, kind):
    lines = [{**item, "name": unescape(unescape(item["name"]))} for item in quote["items"]]
    return render_pdf({**quote, "items": lines}, kind)


def normalize_card_name(text):
    """Сводит «на 2 ТБ» и «2TB» к одной записи, сохраняя значение и единицу ёмкости."""
    units = {"тб": "tb", "гб": "gb"}
    return CARD_CAPACITY.sub(
        lambda match: match[1].replace(",", ".") + units.get(match[2], match[2]),
        normalize_codes(text),
    )


def recommended_products(catalog, candidates, answer):
    skus = list(dict.fromkeys(item["sku"] for item in candidates))
    current = catalog.get_many(skus)
    answer = normalize_card_name(answer)
    answer_models = {match[0]: match.start() for match in MODEL_DETAIL.finditer(answer)}
    positions = []
    for sku in skus:
        item = current.get(sku)
        if item is None:
            continue
        exact = re.search(rf"(?<![\w-]){re.escape(sku.casefold())}(?![\w-])", answer)
        name = normalize_card_name(item["name"])
        name_at = answer.find(name)
        model_at = min((answer_models[code] for code in MODEL_DETAIL.findall(name)
                        if code in answer_models and len(code) >= 5), default=-1)
        if exact or name_at >= 0 or model_at >= 0:
            positions.append((exact.start() if exact else name_at if name_at >= 0 else model_at, item))
    return [item for _, item in sorted(positions, key=lambda entry: entry[0])][:5]


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
    closure_reason: Text | None = None


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
        """Получает внутренний идентификатор только из действующей cookie-сессии."""
        owner = session_digest(request)
        return owner if owner and commercial.active_session(owner) else None

    def require_session(request: Request):
        owner = session_owner(request)
        if owner is None:
            raise HTTPException(404, "Обращение не найдено")
        return owner
    chat_max = int(os.getenv("APP_CHAT_PER_MINUTE", "0"))
    chat_session_max = int(os.getenv("APP_CHAT_PER_SESSION_PER_MINUTE", "0"))
    chat_times = deque()
    chat_lock = Lock()
    require_lead_session = os.getenv("APP_REQUIRE_LEAD_SESSION", "0") == "1"
    lead_attempts = deque()
    lead_lock = Lock()

    def reserve_lead_slot(owner):
        """Ограничивает попытки записи на публичном стенде одним общим окном."""
        if not require_lead_session:
            return True
        with lead_lock:
            now = monotonic()
            while lead_attempts and lead_attempts[0][0] <= now - 60:
                lead_attempts.popleft()
            if len(lead_attempts) >= 30 or sum(key == owner for _, key in lead_attempts) >= 5:
                return False
            lead_attempts.append((now, owner))
            return True


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

    @api.get("/api/v1/catalog/status", dependencies=[Depends(require_manager)])
    def catalog_status():
        return {**catalog.status(), "last_remote_sync": catalog.remote_sync_status()}

    @api.get("/api/v1/products")
    def products(q: str = Query("", max_length=200),
                 limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=2**63-1)):
        return {"items": catalog.search(q, limit, offset)}

    @api.post("/api/v1/chat")
    def chat(body: Chat, request: Request):
        """Возвращает ответ с карточками и создаёт обращение только для действующей сессии.

        Каталог и подтверждённые ответы обрабатываются локально. Для обычной
        консультации используется модель, а трасса обозначает фактический маршрут.
        """
        owner = session_owner(request)
        if chat_max > 0 or (owner and chat_session_max > 0):
            with chat_lock:
                now = monotonic()
                while chat_times and chat_times[0][0] <= now - 60:
                    chat_times.popleft()
                global_limit = chat_max > 0 and len(chat_times) >= chat_max
                session_limit = owner and chat_session_max > 0 and (
                    sum(key == owner for _, key in chat_times) >= chat_session_max
                )
                if global_limit or session_limit:
                    raise HTTPException(429, "Лимит запросов к помощнику", {"Retry-After": "60"})
                chat_times.append((now, owner))
        trusted_history = commercial.chat_history(owner, include_context=True) if owner else []
        incoming_history = trusted_history if owner else [turn.model_dump() for turn in body.history]
        active = active_turns(body.message, incoming_history)
        previous = active[-1]["question"] if active else ""
        earlier = "\n".join(turn["question"] for turn in reversed(active))
        scope = product_scope(body.message, earlier)
        context = f"{earlier}\n{body.message}"
        operator_requested = bool(OPERATOR_REQUEST.search(body.message))
        guidance = advisory(body.message, context)
        resources = knowledge.search(body.message)
        confirmed = resources[0] if resources and resources[0].get("kind") == "manager_confirmed" else None
        current = normalize_codes(body.message).strip()
        product_followup = bool(FOLLOW_PRODUCT.search(current))
        service_only = bool(SERVICE_TOPIC.search(current) and not product_scope(body.message))
        requested_documents = "Подготовьте счёт" if PURCHASE_CONTINUE.fullmatch(current) else body.message
        sources = []
        products = []
        searched = False
        documents = None
        document_handoff = False
        delivery_handoff = None
        consultation_subject = None
        selection = None
        delivery = purchase_with_delivery(body.message, catalog)
        if operator_requested:
            result = {"answer": "Ответ менеджера появится в этом чате.", "provider": "catalog", "actions": []}
        elif REJECTION.search(current):
            result = {"answer": "Хорошо, исключим эту позицию. Назовите другую модель или параметры нового подбора.",
                      "provider": "catalog", "actions": []}
        elif (document := document_request(requested_documents, trusted_history, catalog)) is not None:
            if document.sku and not document.handoff:
                selection = {"sku": document.sku, "quantity": document.quantity}
            if not owner:
                answer = "Откройте клиентский чат, чтобы подготовить документы в своей сессии."
            elif document.missing:
                answer = document.missing
                document_handoff = document.handoff
            elif not reserve_lead_slot(owner):
                answer = "Достигнут лимит подготовки документов. Повторите попытку через минуту."
            else:
                product = document.product
                item = {key: product[key] for key in ("sku", "name", "price_minor")}
                item["quantity"] = document.quantity
                lead = commercial.create_lead(
                    SYSTEM_CONTACT, f"Документы по каталогу: {item['sku']}, {item['quantity']} шт.",
                    sku=item["sku"], owner=owner,
                )
                quote = commercial.create_quote(lead["id"], [item], render_documents, catalog_pricing=True)
                documents = {**quote, "lead_id": lead["id"]}
                answer = document_confirmation(product, item["quantity"])
            result = {"answer": answer, "provider": "catalog",
                      "actions": [{"type": "documents_prepared", "status": "done"}] if documents else []}
            if delivery and document.sku:
                _, next_step, _, subject, sku = delivery
                result["answer"] += "\n\n" + next_step
                delivery_handoff = (subject, sku)
                sources = [{"title": "Условия доставки Softline", "url": DELIVERY}]
        elif confirmed and confirmed["title"].casefold() == body.message.casefold():
            result = {"answer": confirmed["excerpt"], "provider": "knowledge", "actions": []}
            if confirmed.get("url"):
                sources = [{"title": confirmed["title"], "url": confirmed["url"]}]
        elif owner and (learned := knowledge.learn_from_completion(body.message, trusted_history)):
            result = {"answer": "Хорошо, обращайтесь, если возникнут другие вопросы.", "provider": "catalog",
                      "actions": [{"type": "knowledge_learned", "status": "done"}]}
        elif (reference := knowledge.automatic_answer(body.message) or knowledge.official_candidate(body.message)):
            result = {"answer": reference["excerpt"], "provider": "knowledge",
                      "actions": [{"type": reference["kind"], "status": "done"}]}
            if reference.get("url"):
                sources = [{"title": reference["title"], "url": reference["url"]}]
        elif (quantity_update := document_quantity_followup(body.message, trusted_history, catalog)):
            selected, count = quantity_update
            selection = {"sku": selected["sku"], "quantity": count}
            price = selected["price_minor"]
            answer = (f"Пересчитал: {quantity_label(selected, count)} — {price_text(count * price)}."
                      if price is not None else f"Учту {quantity_label(selected, count)}. Цена по запросу.")
            result = {"answer": answer, "provider": "catalog", "actions": []}
        elif re.fullmatch(r"(?:а\s+)?(?:сколько стоит|какая цена|по какой цене)[?!.]?", current) and (
            prior_selection := recently_shown(catalog, trusted_history, None, set())
        )[0]:
            selected, count = prior_selection
            selection = {"sku": selected["sku"], "quantity": count}
            price = selected["price_minor"]
            answer = f"За штуку — {price_text(price)}." if price is not None else "Цена по запросу."
            if price is not None and count and count > 1:
                answer += f" За {quantity_label(selected, count)} — {price_text(count * price)}."
            result = {"answer": answer, "provider": "catalog", "actions": []}
        elif owner and delivery_details_followup(body.message, trusted_history) and commercial.add_delivery_details(owner, body.message):
            result = {"answer": "Добавил данные в обращение. Менеджер уточнит стоимость и дату доставки."
                                "\n\nСтатус обращения: ожидает ответа менеджера.",
                      "provider": "catalog", "actions": [{"type": "delivery_details_added", "status": "done"}]}
        elif delivery is not None:
            opening, next_step, products, subject, sku = delivery
            searched = True
            delivery_handoff = (subject, sku)
            sources = [{"title": "Условия доставки Softline", "url": DELIVERY}]
            result = {"answer": f"{opening}\n\n{next_step}", "provider": "catalog", "actions": []}
        elif (consultation := mail_consultation(
            body.message, incoming_history,
            catalog, new_product=product_scope(body.message) is not None,
        )) is not None:
            answer, products, consultation_subject, searched = consultation
            result = {"answer": answer, "provider": "catalog", "actions": []}
        elif guidance:
            answer, sources = guidance
            result = {"answer": answer, "provider": "catalog", "actions": []}
        elif (license_result := license_answer(body.message, [turn["question"] for turn in incoming_history], catalog)):
            answer, products, searched = license_result
            result = {"answer": answer, "provider": "catalog", "actions": []}
        elif (comparison := compare_monitor_models(body.message, catalog) or (
            compare_monitor_models(previous, catalog) if product_followup and previous else None
        )) is not None:
            answer, products = comparison
            searched = True
            result = {"answer": answer, "provider": "catalog", "actions": []}
        elif scope:
            candidates = catalog.search(body.message, 5, scope=scope)
            searched = True
            answer, products, sources = scoped_answer(scope, candidates, body.message, context)
            result = {"answer": answer, "provider": "catalog", "actions": []}
        else:
            products = [] if service_only else catalog.search(body.message, 5)
            searched = not service_only
            if not products and previous and product_followup and not service_only:
                products = catalog.search(previous, 5)
            result = reply(
                body.message, products, search=None if service_only else catalog.search, knowledge=resources,
                history=[{"question": turn["question"], "answer": turn["answer"]} for turn in active],
            )
            if confirmed and result["provider"] == "catalog":
                result["answer"] = confirmed["excerpt"]
            seen = set()
            for item in resources:
                url = item.get("url")
                if url and url not in seen and url in result["answer"]:
                    seen.add(url)
                    sources.append({"title": item["title"], "url": url})
        tool_products = result.pop("products", [])
        if result["provider"] in {"yandex", "openai"}:
            products = recommended_products(catalog, [*tool_products, *products], result["answer"])
            result["answer"], sources = checked_model_answer(body.message, result["answer"], products, resources)
            result["actions"].append({"type": "commercial_claims_check", "status": "done"})
        elif result["provider"] == "knowledge":
            products = []
        actions = [{"type": "knowledge_search", "status": "done"}]
        if searched:
            actions.insert(0, {"type": "catalog_search", "status": "done"})
        model_unavailable = result["provider"] == "catalog" and any(
            action["status"] == "error" and action["type"] in {"route_n8n", "model_yandex"}
            for action in result["actions"]
        )
        answer = result["answer"]
        model_handoff = result["provider"] in {"yandex", "openai"} and bool(re.search(
            r"(?:не удалось (?:подтвердить|найти)|недостаточно (?:данных|информации)|"
            r"нет (?:информации|подходящ\w* (?:позици|решени|товар)\w*))", answer, re.IGNORECASE,
        )) and bool(re.search(r"менеджер\w*", answer, re.IGNORECASE))
        needs_manager = operator_requested or document_handoff or delivery_handoff is not None or consultation_subject is not None or model_handoff or (
            result["provider"] == "catalog" and any(text in answer for text in UNRESOLVED_CATALOG)
        )
        wants_escalation = model_unavailable or needs_manager
        should_escalate = owner is not None and wants_escalation and reserve_lead_slot(owner)
        if should_escalate:
            result["answer"] += "\n\nСтатус обращения: ожидает ответа менеджера."
        elif wants_escalation and owner is not None:
            result["answer"] = answer if not operator_requested else "Передать обращение пока не удалось."
            result["answer"] += "\n\nДостигнут лимит обращений. Повторите попытку через минуту."
        elif operator_requested:
            result["answer"] = "Откройте форму «Позвать менеджера», чтобы отправить обращение."
        elif model_unavailable:
            result["answer"] += "\n\nОткройте клиентский чат, чтобы передать вопрос менеджеру."
        if owner:
            if selection is None and products and (len(products) == 1 or (
                scope and scope.model and all(scope.matches(item) for item in products)
            )):
                _, count = quantity_in(current)
                selection = {"sku": products[0]["sku"], "quantity": count}
            commercial.record_turn(owner, body.message, result["answer"], quote_id=documents["id"] if documents else None,
                                   product_context=selection)
        if documents:
            result["documents"] = documents
        if confirmed:
            actions.append({"type": "knowledge_confirmed", "status": "done"})
        if should_escalate:
            subject = consultation_subject or body.message
            lead_sku = document.sku if document_handoff else None
            if delivery_handoff:
                subject, lead_sku = delivery_handoff
            if operator_requested:
                subject = next((turn.question for turn in reversed(body.history)
                                if not OPERATOR_REQUEST.search(turn.question)), subject)
            lead = commercial.create_lead(SYSTEM_CONTACT, subject, sku=lead_sku,
                                          escalation=True, owner=owner)
            result["escalation"] = {"status": lead["status"]}
            if owner:
                result["escalation"]["id"] = lead["id"]
            actions.append({"type": "lead_escalation", "status": "done"})
        trace = [*actions, *result["actions"]]
        CHAT_LOG.info("chat_route provider=%s actions=%s source_count=%d", result["provider"],
                      ",".join(f"{item['type']}:{item['status']}" for item in trace
                               if ACTION_NAME.fullmatch(item["type"])
                               and item["status"] in {"done", "error", "skipped"}), len(sources))
        return {
            "products": products,
            **result,
            "actions": trace,
            "sources": sources,
        }

    @api.post("/api/v1/leads", status_code=201)
    def create_lead(body: Lead, request: Request):
        owner = session_owner(request)
        if require_lead_session and owner is None:
            raise HTTPException(403, "Откройте клиентский чат перед отправкой обращения")
        if body.sku and catalog.get(body.sku) is None:
            raise HTTPException(404, "Товар не найден")
        if not reserve_lead_slot(owner):
            raise HTTPException(429, "Лимит обращений", {"Retry-After": "60"})
        return commercial.create_lead(body.contact, body.message, body.sku, body.escalation, owner)

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

    @api.get("/api/v1/client/chat-history")
    def client_chat_history(request: Request, include_times: bool = False):
        owner = session_owner(request)
        return {"items": commercial.chat_history(owner, include_times) if owner else []}

    @api.get("/api/v1/client/manager-messages")
    def manager_messages(request: Request, after: int = Query(0, ge=0, le=2**63-1)):
        owner = session_owner(request)
        return {"items": commercial.manager_messages(owner, after) if owner else []}

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

    @api.get("/api/v1/client/quotes/{quote_id}/documents.zip")
    def client_documents(quote_id: str, owner: Annotated[str, Depends(require_session)]):
        return documents_response(quote_id, owner)

    @api.get("/api/v1/manager/leads", dependencies=[Depends(require_manager)])
    def leads(page: int = Query(1, ge=1), limit: int = Query(100, ge=1, le=100),
              view: Literal["all", "open", "closed", "waiting"] = "all"):
        return commercial.lead_queue(page, limit, view)

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
            return commercial.update_lead(lead_id, body.status, body.answer, body.closure_reason)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
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
        """Проверяет позиции каталога и передаёт один снимок строк двум PDF."""
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
            quote = commercial.create_quote(body.lead_id, items, render_documents)
        except KeyError as exc:
            raise HTTPException(404, "Заявка не найдена") from exc
        base = f'/api/v1/manager/quotes/{quote["id"]}'
        return {**quote, "download_url": f"{base}/download",
                "quote_pdf_url": f"{base}/quote.pdf", "invoice_pdf_url": f"{base}/invoice.pdf",
                "documents_url": f"{base}/documents.zip"}

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

    @api.get("/api/v1/manager/quotes/{quote_id}/documents.zip", dependencies=[Depends(require_manager)])
    def manager_documents(quote_id: str):
        return documents_response(quote_id)

    def authorized_quote(quote_id, owner):
        try:
            return commercial.get_quote(quote_id, owner)
        except KeyError as exc:
            raise HTTPException(404, "КП не найдено") from exc

    def document_pdf(quote, kind):
        """Использует сохранённый PDF; старые снимки рендерит по запросу."""
        content = commercial.quote_pdf(quote["id"], kind)
        return render_documents(quote, kind) if content is None else content

    def pdf_response(quote_id, kind, owner=None):
        quote = authorized_quote(quote_id, owner)
        return Response(document_pdf(quote, kind), media_type="application/pdf", headers={
            "Content-Disposition": f"attachment; filename=\"{kind}.pdf\"; filename*=UTF-8''{url_quote(PDF_FILENAMES[kind])}",
            "Cache-Control": "no-store",
        })

    def documents_response(quote_id, owner=None):
        """Возвращает оба PDF одним архивом после проверки права на комплект."""
        quote = authorized_quote(quote_id, owner)
        archive = package_documents(document_pdf(quote, "quote"), document_pdf(quote, "invoice"))
        return Response(archive, media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="documents-{quote["id"]}.zip"',
            "Cache-Control": "no-store",
        })

    return api


app = create_app()

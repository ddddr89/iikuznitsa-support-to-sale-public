"""HTTP API чата, каталога, контакта с менеджером и защищённых документов."""

import logging
import os
import re
from collections import deque
from dataclasses import asdict
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
    DELIVERY, DELIVERY_REQUEST, DELIVERY_DETAILS, advisory, comparison_details, checked_model_answer, compare_monitor_models, delivery_details_followup, document_confirmation,
    price_text, purchase_with_delivery, quantity_label, scoped_answer,
)
from app.catalog_matching import MODEL_DETAIL, normalize_codes, product_scope
from app.commercial import Commercial
from app.consultation import PURCHASE_CONTINUE, REJECTION, active_turns, mail_consultation
from app.dialogue import Dialogue, DIRECTION, PRICE, SELECTION, STOP, corrected_quantity, memory_followup, route_for
from app.equipment_advice import headset_advice, network_advice
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
        earlier = "\n".join(turn["question"] for turn in reversed(active))
        current = normalize_codes(body.message).strip()
        state = Dialogue.load(trusted_history)
        new_route = route_for(body.message)
        service_only = new_route == "service"
        cancel = bool(REJECTION.search(current) or STOP.search(current) and "на этом все" in current)
        explicit_operator = bool(OPERATOR_REQUEST.search(body.message))
        consent = bool(state.handoff and re.fullmatch(
            r"(?:да|согласен|согласна|давайте|хорошо|передайте|да,? передайте)[.! ]*", current
        ))
        operator_requested = explicit_operator or consent
        continued_query = state.search_query(body.message) if not cancel and not service_only else None
        requested_documents = "Подготовьте счёт" if PURCHASE_CONTINUE.fullmatch(current) else body.message
        # Оформление сначала разрешает ссылку на текущий выбор. Новая товарная
        # задача заводится лишь после определения того, что это не продолжение.
        selection_hint = {"sku": state.sku, "quantity": state.quantity} if state.sku and not cancel else None
        document = document_request(requested_documents, trusted_history, catalog, selection=selection_hint,
                                    pending=False if new_route else True if state.pending == "documents" else None)
        if new_route and not service_only and not continued_query and not document and not operator_requested:
            state = Dialogue(route=new_route, query=body.message)
        if continued_query:
            state.query = continued_query[:2000]
        scope = product_scope(continued_query or body.message)
        context = f"{earlier}\n{body.message}"
        guidance = advisory(body.message, context)
        resources = knowledge.search(body.message)
        verified_reference = knowledge.automatic_answer(body.message) or knowledge.official_candidate(body.message)
        confirmed = resources[0] if resources and resources[0].get("kind") == "manager_confirmed" else None
        chosen = catalog.get(state.sku) if state.sku else None
        count = corrected_quantity(current) if chosen and not new_route else None
        choice = DIRECTION.search(current) if state.options and not new_route and SELECTION.search(current) else None
        compared = [item for sku in state.options if (item := catalog.get(sku)) is not None]
        if choice and len(compared) >= 2 and all(item["price_minor"] is not None for item in compared):
            reverse = bool(re.search(r"дорог|дорож", choice[0]))
            ranked = sorted(compared, key=lambda item: item["price_minor"], reverse=reverse)
            if ranked[0]["price_minor"] != ranked[1]["price_minor"]:
                chosen = ranked[0]
                _, count = quantity_in(current)
        if cancel:
            state = Dialogue()
        elif state.handoff and re.fullmatch(r"нет[. !]*", current):
            state.handoff = None
            cancel = True
        extra_detail = None
        suppress_manager = bool(STOP.search(current))
        if suppress_manager:
            state.pending = None
        sources = []
        products = []
        searched = False
        resolved_catalog_lookup = False
        documents = None
        document_handoff = False
        delivery_handoff = None
        consultation_subject = None
        selection = None
        content_count, quantity = quantity_in(current)
        wants_delivery_for_selection = (not service_only or state.route != "delivery" and (
            content_count or re.search(r"\b(?:этот|этого|выбранн\w*|для него)\b", current)
        ))
        delivery = purchase_with_delivery(
            body.message, catalog,
            selected=chosen if wants_delivery_for_selection and not suppress_manager else None,
            previous_quantity=state.quantity,
        )
        latest_price_question = PRICE.search(current) or re.search(r"\b(?:во сколько|сколько)\b", current)
        if chosen and not new_route and content_count and quantity is not None and latest_price_question:
            count = quantity
        conditional_handoff = operator_requested and bool(re.search(r"\bесли\b", current))
        license_result = (license_answer(body.message, [turn["question"] for turn in incoming_history], catalog)
                          if not operator_requested or conditional_handoff else None)
        if conditional_handoff and license_result and license_result[1]:
            operator_requested = False
        if operator_requested and not (conditional_handoff and license_result):
            result = {"answer": "Ответ менеджера появится в этом чате.", "provider": "catalog", "actions": []}
        elif cancel:
            result = {"answer": "Хорошо, остановимся. Если захотите возобновить подбор, напишите.",
                      "provider": "catalog", "actions": []}
        elif document is not None:
            state.pending = "documents" if document.missing and not document.handoff else None
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
                _, next_step, _, subject, sku, _ = delivery
                result["answer"] += "\n\n" + next_step
                delivery_handoff = (subject, sku)
                sources = [{"title": "Условия доставки Softline", "url": DELIVERY}]
        elif not verified_reference and (re.search(r"\b(?:статус|состояние|стадия|отследить)\b.{0,75}\bзаказ\w*\b", current) or (
            re.search(r"\bномер\s+заказа\b", current)
            and re.search(r"\b(?:где|как|найти|посмотреть)\b", current)
        )):
            result = {"answer": "Номер заказа ищите в письме с подтверждением заказа. По ссылке из письма "
                                "откройте личный кабинет Softline и посмотрите статус. "
                                "Если письмо не нашлось, могу передать вопрос менеджеру.",
                      "provider": "knowledge", "actions": []}
            consultation_subject = body.message
            sources = [{"title": "Проверка статуса заказа в Softline", "url": DELIVERY}]
        elif confirmed and confirmed["title"].casefold() == body.message.casefold():
            result = {"answer": confirmed["excerpt"], "provider": "knowledge", "actions": []}
            if confirmed.get("url"):
                sources = [{"title": confirmed["title"], "url": confirmed["url"]}]
        elif owner and (learned := knowledge.learn_from_completion(body.message, trusted_history)):
            result = {"answer": "Хорошо, обращайтесь, если возникнут другие вопросы.", "provider": "catalog",
                      "actions": [{"type": "knowledge_learned", "status": "done"}]}
        elif (reference := verified_reference):
            result = {"answer": reference["excerpt"], "provider": "knowledge",
                      "actions": [{"type": reference["kind"], "status": "done"}]}
            if reference.get("url"):
                sources = [{"title": reference["title"], "url": reference["url"]}]
        elif chosen and (count is not None or choice or PRICE.search(current)) and not new_route and delivery is None:
            quantity = count if count is not None else state.quantity
            selection = {"sku": chosen["sku"], "quantity": quantity}
            price = chosen["price_minor"]
            if quantity and price is not None:
                answer = f"{'Выбран ' + chosen['name'] + '. ' if choice else ''}{'Пересчитал: ' if count and not choice else ''}{quantity_label(chosen, quantity)} — {price_text(price * quantity)}."
            elif choice:
                answer = f"Выбран {chosen['name']}. Сколько штук нужно?"
            else:
                answer = f"За штуку — {price_text(price)}." if price is not None else "Цена по запросу."
            result = {"answer": answer, "provider": "catalog", "actions": []}
        elif (quantity_update := document_quantity_followup(
            body.message, [{"question": "", "answer": "", "product_context": selection_hint}]
            if selection_hint else trusted_history, catalog,
        )) and not new_route:
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
        elif state.route == "delivery" and state.handoff and DELIVERY_DETAILS.search(current):
            state.handoff["subject"] = (state.handoff["subject"] + "\n" + body.message)[:2000]
            result = {"answer": "Учёл данные доставки. Передать их менеджеру для расчёта стоимости и даты?",
                      "provider": "catalog", "actions": []}
        elif owner and state.lead and state.route == "delivery" and DELIVERY_DETAILS.search(current):
            extra_detail = commercial.append_customer_detail(owner, state.lead, body.message)
            result = {"answer": "Добавил данные в обращение. Менеджер уточнит стоимость и дату доставки."
                                if extra_detail else "Записать данные в прежнее обращение не удалось. Позовите менеджера, если расчёт ещё нужен.",
                      "provider": "catalog", "actions": []}
        elif service_only and DELIVERY_REQUEST.search(current) and state.route == "delivery" and state.lead:
            result = {"answer": "Тариф и дата доставки ещё не подтверждены. Менеджер ответит по существующему обращению в этом чате.",
                      "provider": "catalog", "actions": []}
            sources = [{"title": "Условия доставки Softline", "url": DELIVERY}]
        elif owner and delivery_details_followup(body.message, trusted_history) and commercial.add_delivery_details(owner, body.message):
            result = {"answer": "Добавил данные в обращение. Менеджер уточнит стоимость и дату доставки."
                                "\n\nСтатус обращения: ожидает ответа менеджера.",
                      "provider": "catalog", "actions": [{"type": "delivery_details_added", "status": "done"}]}
        elif delivery is not None:
            state.route = "delivery"
            opening, next_step, products, subject, sku, quantity = delivery
            if sku:
                selection = {"sku": sku, "quantity": quantity}
            searched = bool(products or scope)
            delivery_handoff = (subject, sku)
            sources = [{"title": "Условия доставки Softline", "url": DELIVERY}]
            result = {"answer": f"{opening}\n\n{next_step}", "provider": "catalog", "actions": []}
        elif state.route in {"headset", "network"} and not service_only:
            give_advice = headset_advice if state.route == "headset" else network_advice
            answer, products, sources = give_advice(state, body.message, catalog)
            searched = bool(products)
            result = {"answer": answer, "provider": "catalog", "actions": []}
        elif (consultation := mail_consultation(
            body.message, incoming_history,
            catalog, new_product=product_scope(body.message) is not None, state=None if service_only else state,
        )) is not None:
            state.route = "mail"
            answer, products, consultation_subject, searched = consultation
            result = {"answer": answer, "provider": "catalog", "actions": []}
        elif state.route == "memory" and (memory := memory_followup(
            body.message, [state.query, *[turn["question"] for turn in active]],
        )):
            result = {"answer": memory, "provider": "catalog", "actions": []}
        elif service_only and re.search(r"\b(?:возврат\w*|обмен\w*)\b", current) and "эдо" not in current:
            sources = [{"title": "Получение, приём и возврат товара", "url": DELIVERY}]
            result = {"answer": "По возврату обратитесь через раздел «Получение, приём и возврат товара». "
                                "Для обращения пригодятся номер заказа и описание причины возврата.",
                      "provider": "knowledge", "actions": []}
        elif service_only and "эдо" in current:
            reference = next((item for item in resources if "эдо" in item["title"].casefold()
                              and item.get("kind") == "manager_confirmed"), None)
            answer = (reference["excerpt"] if reference else
                      "Подтверждённого контакта по ЭДО у меня нет. Менеджер уточнит, куда направить документы.")
            if reference and reference.get("url"):
                sources = [{"title": reference["title"], "url": reference["url"]}]
            if not reference:
                consultation_subject = body.message
            result = {"answer": answer, "provider": "knowledge", "actions": []}
        elif guidance:
            answer, sources = guidance
            result = {"answer": answer, "provider": "catalog", "actions": []}
        elif license_result:
            answer, products, searched = license_result
            result = {"answer": answer, "provider": "catalog", "actions": []}
        elif (comparison := compare_monitor_models(body.message, catalog) or (
            compare_monitor_models(state.query if state.route == "comparison" else earlier, catalog)
            if re.search(r"\b(?:разница в цене|чем отличаются|зарядк\w*|проводн\w* сеть|одним кабелем)\b", current)
            and not new_route else None
        )) is not None:
            answer, products = comparison
            if state.route != "comparison":
                state.query = body.message
            state.route = "comparison"
            state.options = [item["sku"] for item in products]
            detail, sources = comparison_details(products)
            if detail:
                answer += "\n\n" + detail
            searched = True
            result = {"answer": answer, "provider": "catalog", "actions": []}
        elif scope:
            # Пустая выдача по определённым условиям завершает проверку каталога.
            # Вопрос менеджеру остаётся отдельным действием клиента.
            resolved_catalog_lookup = True
            query = continued_query or body.message
            candidates = [item for item in catalog.search(query, 5, scope=scope) if scope.matches(item)]
            searched = True
            answer, products, sources = scoped_answer(scope, candidates, body.message, query)
            if continued_query and not products:
                answer = "По уточнённым условиям не нашлось подтверждённого совпадения."
                if scope.max_price is not None:
                    answer += f" Учёл бюджет до {price_text(int(scope.max_price))}."
                if re.search(r"\bподтверд\w*\s+(?:интерфейс|разъем|разрешение|совместимость)\b", current):
                    answer = "Подходящую позицию не нашёл, поэтому подтвердить характеристику конкретного товара пока нельзя. " + answer
            result = {"answer": answer, "provider": "catalog", "actions": []}
        else:
            catalog_allowed = not service_only and not (state.route and not new_route)
            products = catalog.search(body.message, 5) if catalog_allowed else []
            searched = catalog_allowed
            result = reply(
                body.message, products, search=catalog.search if catalog_allowed else None, knowledge=resources,
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
        fallback_clarification = "Подскажите модель или нужную конфигурацию."
        if fallback_clarification in result["answer"]:
            result["answer"] = result["answer"].replace(
                fallback_clarification, "Расскажите, что нужно сделать и какие условия важны."
            ).replace("Если нужна помощь, передам вопрос менеджеру.", "")
        answer = result["answer"].rstrip()
        result["answer"] = answer
        model_handoff = result["provider"] in {"yandex", "openai"} and bool(re.search(
            r"(?:не удалось (?:подтвердить|найти)|недостаточно (?:данных|информации)|"
            r"нет (?:информации|подходящ\w* (?:позици|решени|товар)\w*))", answer, re.IGNORECASE,
        )) and bool(re.search(r"менеджер\w*", answer, re.IGNORECASE))
        needs_manager = operator_requested or document_handoff or delivery_handoff is not None or consultation_subject is not None or model_handoff or (
            not resolved_catalog_lookup and result["provider"] == "catalog"
            and any(text in answer for text in UNRESOLVED_CATALOG)
        )
        offered_subject, offered_sku = delivery_handoff or (
            consultation_subject or (state.query if not service_only else body.message) or body.message,
            document.sku if document_handoff else None if service_only else state.sku,
        )
        requested_handoff = state.handoff if operator_requested else None
        if not operator_requested and not suppress_manager and (model_unavailable or needs_manager):
            state.handoff = {"subject": offered_subject, "sku": offered_sku, "lead": None}
            if not result["answer"].endswith("?") and "могу передать вопрос менеджеру" not in result["answer"].lower():
                result["answer"] += "\n\nПередать вопрос менеджеру?"
        wants_escalation = operator_requested and not suppress_manager
        lead_hint = requested_handoff.get("lead") if requested_handoff else state.lead
        existing_lead = (commercial.append_customer_detail(owner, lead_hint, body.message)
                         if owner and wants_escalation and lead_hint else None)
        should_escalate = owner is not None and wants_escalation and (existing_lead or reserve_lead_slot(owner))
        if should_escalate:
            result["answer"] += "\n\nСтатус обращения: ожидает ответа менеджера."
        elif wants_escalation and owner is not None:
            result["answer"] = "Достигнут лимит обращений. Повторите попытку через минуту."
        elif operator_requested:
            result["answer"] = "Откройте форму «Позвать менеджера», чтобы отправить обращение."
        if selection is None and products and (len(products) == 1 or (
            scope and scope.model and all(scope.matches(item) for item in products)
        )):
            _, count = quantity_in(current)
            quantity = count or (state.quantity if state.sku == products[0]["sku"] or state.route == "headset" else None)
            selection = {"sku": products[0]["sku"], "quantity": quantity}
            price = products[0]["price_minor"]
            if count and scope and not scope.model and price is not None:
                result["answer"] += f"\nЗа {quantity_label(products[0], count)} — {price_text(price * count)}."
        if selection:
            state.sku, state.quantity = selection["sku"], selection["quantity"]
        if documents:
            result["documents"] = documents
        if confirmed:
            actions.append({"type": "knowledge_confirmed", "status": "done"})
        if should_escalate:
            subject, lead_sku = offered_subject, offered_sku
            if requested_handoff:
                subject, lead_sku = requested_handoff["subject"], requested_handoff["sku"]
            elif not state.query and trusted_history and not consultation_subject:
                subject = trusted_history[-1]["question"]
            lead = existing_lead or commercial.create_lead(
                SYSTEM_CONTACT, subject, sku=lead_sku, escalation=True, owner=owner,
            )
            state.lead = lead["id"]
            state.handoff = None
            result["escalation"] = {"status": lead["status"], "id": lead["id"]}
            actions.append({"type": "lead_escalation", "status": "done"})
        if owner:
            commercial.record_turn(owner, body.message, result["answer"], quote_id=documents["id"] if documents else None,
                                   product_context=selection, dialogue=asdict(state))
            if should_escalate:
                commercial.append_customer_detail(owner, lead["id"], body.message)
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
        if owner and body.escalation:
            state = Dialogue.load(commercial.chat_history(owner, include_context=True))
            if state.handoff:
                offer = state.handoff
                result = commercial.create_lead(body.contact, offer["subject"], offer["sku"], True, owner)
                if body.message != offer["subject"]:
                    commercial.append_customer_detail(owner, result["id"], body.message)
                return result
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

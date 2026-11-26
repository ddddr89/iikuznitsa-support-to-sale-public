import os
from contextlib import asynccontextmanager
from pathlib import Path
from secrets import compare_digest
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.ai import reply
from app.catalog import Catalog
from app.commercial import Commercial
from app.documents import render_pdf

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
Contact = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=320)]
Sku = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
STATIC = Path(__file__).with_name("static")


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Chat(Input):
    message: Text


class Lead(Chat):
    contact: Contact
    sku: Sku | None = None
    escalation: bool = False


class QuoteItem(Input):
    sku: Sku
    quantity: Annotated[int, Field(strict=True, ge=1, le=999)]


class Quote(Input):
    lead_id: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")]
    items: Annotated[list[QuoteItem], Field(min_length=1, max_length=50)]


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

    @asynccontextmanager
    async def lifespan(_app):
        catalog.init()
        commercial.init()
        yield

    api = FastAPI(title="Support-to-Sale AI", lifespan=lifespan)
    api.mount("/static", StaticFiles(directory=STATIC), name="static")

    @api.get("/", include_in_schema=False)
    def client():
        return FileResponse(STATIC / "index.html")

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
    def chat(body: Chat):
        products = catalog.search(body.message, 5)
        result = reply(body.message, products, search=catalog.search)
        return {
            "products": products,
            **result,
            "actions": [{"type": "catalog_search", "status": "done"}, *result["actions"]],
        }

    @api.post("/api/v1/leads", status_code=201)
    def create_lead(body: Lead):
        if body.sku and catalog.get(body.sku) is None:
            raise HTTPException(404, "Товар не найден")
        return commercial.create_lead(body.contact, body.message, body.sku, body.escalation)

    @api.get("/api/v1/manager/leads", dependencies=[Depends(require_manager)])
    def leads():
        return {"items": commercial.list_leads()}

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
        try:
            quote = commercial.get_quote(quote_id)
        except KeyError as exc:
            raise HTTPException(404, "КП не найдено") from exc
        return Response(render_pdf(quote, kind), media_type="application/pdf", headers={
            "Content-Disposition": f'attachment; filename="{kind}-{quote["id"]}.pdf"',
            "Cache-Control": "no-store",
        })

    return api


app = create_app()

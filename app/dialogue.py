"""Хранит задачу покупателя отдельно от текста ответа и справочных вопросов."""

from dataclasses import dataclass, field
import re
from uuid import uuid4

from app.automatic_documents import number
from app.catalog_matching import SKU_REFERENCE, budget_limit, normalize_codes, product_scope
from app.consultation import SERVICE_TOPIC, starts_mail
from app.license_advice import product_in

CORRECTION = re.compile(r"\b(?:на|с)\s+(одного|одной|двух|трех|четырех|пяти|шести|семи|восьми|девяти|десяти|\d{1,3})\b")
INFLECTED = dict(zip("одного одной двух трех четырех пяти шести семи восьми девяти десяти".split(),
                     (1, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10)))
CHANGE = re.compile(r"\b(?:останов\w*|нача(?:ть|л\w*)|измен\w*|поменя\w*|все-таки|все таки)\b")
PRICE = re.compile(r"(?:\bсколько (?:получается|выйдет|заплатим)\b|\bкакой итог\b|\bобщая сумма\b)")
DIRECTION = re.compile(r"\b(?:более\s+дешев\w*|подешевле|дешевл\w*|более\s+дорог\w*|подороже|дороже)\b")
SELECTION = re.compile(r"\b(?:возьм\w*|выбира\w*|беру|останов\w*|подойдет)\b")
STOP = re.compile(r"\b(?:на этом все|пока не оформля\w*|не оформля\w*|не предлагайте замену)\b")
NEW_BUDGET = re.compile(r"\b(?:предел|бюджет|лимит)\w*\s+до\s+(\d+)\s*(тысяч\w*|тыс\.?|руб\w*|₽)?")
SPEC_CHECK = re.compile(r"\b(?:что именно|чем подтверж\w*|подтверждает|подтвердить\s+(?:интерфейс|разъем|разрешение|совместимость)|точно ли|остальное обязател\w*)\b")
DDR = re.compile(r"\bddr[345]\b")
MEMORY_CONTEXT = re.compile(r"\b(?:памят\w*|планк\w*|плат\w*|ноутбуков\w*|настольн\w*|dimm)\b")


@dataclass
class Dialogue:
    task: str = field(default_factory=lambda: uuid4().hex)
    route: str = ""
    query: str = ""
    sku: str | None = None
    quantity: int | None = None
    options: list[str] = field(default_factory=list)
    lead: str | None = None
    handoff: dict | None = None
    pending: str | None = None
    service_leads: dict[str, str] = field(default_factory=dict)
    facts: dict = field(default_factory=dict)
    unknown: list[str] = field(default_factory=list)
    last_question: str | None = None

    @classmethod
    def load(cls, history):
        if history and (state := history[-1].get("dialogue")):
            return cls(**state)
        return cls()

    def search_query(self, message):
        """Продолжение фильтруется по исходной задаче, а не по обрывку реплики."""
        if self.route != "search":
            return None
        current = normalize_codes(message)
        if not product_scope(message) and (budget_limit(current) is not None or (match := NEW_BUDGET.search(current))):
            if budget_limit(current) is None:
                multiplier = 1000 if (match[2] or "").startswith(("тысяч", "тыс")) else 1
                current = f"до {int(match[1]) * multiplier} ₽. {current}"
            return f"{current}\n{self.query}"
        if SPEC_CHECK.search(current) and not STOP.search(current.replace("не предлагайте замену", "")):
            return f"{self.query}\n{current}"
        return None


def route_for(message):
    current = normalize_codes(message)
    if SERVICE_TOPIC.search(current) and not (product_scope(message) or SKU_REFERENCE.search(current)):
        return "service"
    if re.search(r"\bгарнитур\w*\b|наушник\w*\s+с\s+микрофон", current):
        return "headset"
    if re.search(r"\b(?:коммутатор\w*|порт\w*)\b", current) and re.search(r"\b(?:компьютер\w*|пк|рабочие станции)\b", current):
        return "network"
    if starts_mail(current):
        return "mail"
    if product_in(current):
        return "license"
    if DDR.search(current) and MEMORY_CONTEXT.search(current):
        return "memory"
    if product_scope(message) or SKU_REFERENCE.search(current):
        return "search"
    return None


def corrected_quantity(text):
    replacements = re.search(r"\bвместо\s+\w+.*?\b(?:возьмем|берем|нужно|нужны|хочу)\s+(\w+)\b", text)
    if replacements:
        return number(replacements[1])
    if CHANGE.search(text) and (match := CORRECTION.search(text)):
        return INFLECTED.get(match[1]) or number(match[1])
    return None


def memory_followup(message, queries):
    """Проверка совместимости требует данных платы, а не случайной карточки."""
    text = normalize_codes(message)
    if not MEMORY_CONTEXT.search(text):
        return None
    joined = normalize_codes(" ".join([*queries, message]))
    supported = re.search(r"\b(?:только|лишь)\s+(?:с\s+)?(ddr[45])\b", joined)
    if not supported or len(set(DDR.findall(joined))) < 2 or DDR.search(text):
        return None
    generation = supported[1].upper()
    if re.search(r"перед покупкой|настольн\w*", text):
        return (f"Для настольного ПК сверяйте форм-фактор с руководством платы: ищите {generation} DIMM, "
                "поддержку нужного объёма на слот и порядок установки модулей. "
                "Модель платы можно посмотреть в сведениях о системе или на самой плате. "
                "До этой проверки совместимость конкретного комплекта не подтверждена.")
    if re.search(r"не знаю|неизвест", text):
        return (f"Для этой платы нужна {generation}. Перед покупкой проверьте в руководстве платы "
                "допустимый объём на слот, форм-фактор и рекомендуемые слоты для пары. "
                "Для настольного ПК проверьте, что плата принимает полноразмерные DIMM. "
                "Модель платы можно посмотреть в сведениях о системе или на самой плате. "
                "Без неё совместимость конкретного комплекта пока не подтверждена.")
    return None

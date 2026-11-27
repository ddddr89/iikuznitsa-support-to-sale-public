"""Apply explicit product requirements to catalog evidence before ranking."""

from dataclasses import dataclass
from decimal import Decimal
from html import unescape
import re

NUMBER = r"\d+(?:[.,]\d+)?"
CAPACITY = re.compile(rf"(?<![\w.,])({NUMBER})\s*(тб|tb|гб|gb)\b")
DIAGONAL = re.compile(
    rf"(?<![\w.,])({NUMBER})(?:\s*[-–—]\s*({NUMBER}))?"
    r'\s*(?:дюйм\w*|["″]|-?\s*inch\b|in\b)'
)
REFRESH = re.compile(r"(?<!\w)(\d{2,3})\s*(?:гц|hz)\b")
DISPLAY_FORMAT = re.compile(r"\b(?:4k|uhd|ips|3840\s*[x×х]\s*2160)\b")
OTHER_SCREEN = re.compile(r"\b(?:телевизор\w*|смарт-тв|проектор\w*|tv|планшет\w*)\b")
GAMING = re.compile(r"\b(?:игров\w*|gaming)\b")
EXTERNAL = re.compile(r"\b(?:внешн\w*|портативн\w*|external|portable)\b")
DRIVE_TYPE = re.compile(r"\b(?:ssd|hdd)\b")
BUDGET = re.compile(
    r"(?:(?:до|бюджет\w*|за|не дороже|максимум)\s*[:—-]?\s*)"
    r"(\d+(?:[ \u00a0\u202f]\d{3})*(?:[.,]\d+)?)(?:\s*(тыс|к)\b\.?)?"
    r"\s*(?:₽|руб(?:лей|ля|ль)?\b|р\.)"
)
CONFUSABLES = str.maketrans("авсенкморстху", "abcehkmopctxy")
MODEL = re.compile(r"\b(?:[a-z]{1,6}\d{2,}[a-z][a-z0-9]*|\d{2,}[a-z]\d[a-z0-9]+)\b")
MODEL_DETAIL = re.compile(
    r"\b(?:[a-z]{1,9}\d{1,}[a-z0-9]*|\d{2,}[a-z][a-z0-9]+)(?:-[a-z0-9]+)*\b"
)
# По этим сериям Dell можно распознать монитор даже без указания категории.
DELL_DISPLAY = re.compile(r"\bdell(?: technologies)?\s+([pu]\d{4}[a-z][a-z0-9]*)\b")
CATALOG_TOOL_REQUEST = re.compile(r"\b(?:вызови(?:те)?\s+поиск|инструмент\w*\s+поиск|поиск\w*\s+по\s+каталог\w*)\b")
SKU_REFERENCE = re.compile(r"\b(?:артикул\w*|код\w*|sku)\s*[:№#]?\s*(\d{4,12})\b")
DRIVE_NAMES = ("western digital", "elements portable", "my book", "dashdrive",
               "seagate", "samsung", "adata", "netac", "kingston")
CATEGORIES = {
    "monitor": ("Мониторы", "Монитор", "Monitors", "Monitor"),
    "external": ("Внешние HDD", "Внешние SSD", "Внешние накопители",
                 "Внешние жесткие диски", "Внешние жёсткие диски"),
    "ssd": ("Внутренние твердотельные накопители (SSD)",),
    "laptop": ("Ноутбуки",),
    "switch": ("Коммутаторы",),
    "gpu": ("Видеокарты",),
    "office": ("Офисные пакеты программ Microsoft Office",),
    "memory": ("Оперативная память",),
}
# Фид не содержит этих характеристик. Данные из спецификаций применяем только
# к точным кодам производителя, для которых проверена ревизия.
VERIFIED_MODELS = (
    ("27e1n1900ae-01", "ips usb-c 65 вт"),
    ("sg3428mp", "управляемый 24 x gigabit poe+"),
    ("tl-sg3428xmp", "управляемый 24 x gigabit poe+"),
)
REQUIREMENTS = {
    "monitor": (
        (r"\b(?:4k|uhd|3840\s*[x×х]\s*2160)\b", r"\b(?:4k|uhd|3840\s*[x×х]\s*2160)\b"),
        (r"\bips\b", r"\bips\b"),
    ),
    "ssd": (
        (r"\bnvme\b", r"\bnvme\b"),
        (r"\bm[. ]?2\b", r"\bm[. ]?2\b"),
        (r"\bpci(?:e|\s+express)\s*(?:gen\s*)?4(?:\.0)?\b",
         r"\bpci(?:e|\s+express)\s*(?:gen\s*)?4(?:\.0)?\b"),
        (r"\bsata(?:\s*(?:iii|3))?\b", r"\bsata(?:\s*(?:iii|3))?\b"),
    ),
    "switch": (
        (r"(?<!не)\bуправляем\w*\b", r"\b(?:управляем\w*|managed|layer\s*2\+?)\b"),
        (r"\bpoe\+|802\.3at\b", r"\bpoe\+|802\.3at\b"),
        (r"\bгигабит|\b1\s*гбит", r"\bgigabit\b|\b1\s*гбит"),
    ),
}
OFFICE_ANNUAL = re.compile(r"\b(?:(?:на|сроком на)\s+(?:1\s+год|год|12\s+месяцев)|годов\w* подписк\w*)\b")
USB_C = r"\b(?:usb[ -]?c|type[ -]?c)\b"
STARTS = (
    ("monitor", r"\b(?:монитор\w*|monitors?)\b"),
    ("laptop", r"\b(?:ноутбук\w*|laptops?)\b"),
    ("switch", r"\b(?:коммутатор\w*|switch)\b"),
    ("gpu", r"\b(?:видеокарт\w*|gpu)\b"),
    ("office", r"\b(?:microsoft|office)\s*365\b"),
    ("memory", r"\bоперативн\w*\s+памят\w*\b"),
    ("drive", r"\b(?:диск\w*|накопител\w*|ssd|hdd)\b"),
)


def normalize(text):
    return unescape(text).casefold().replace("ё", "е")


def normalize_codes(text):
    return re.sub(
        r"\b[\w-]+\b",
        lambda match: match[0].translate(CONFUSABLES)
        if re.search(r"[a-z]", match[0]) and re.search(r"\d", match[0]) else match[0],
        normalize(text),
    )


def number(value):
    return Decimal(value.replace(",", "."))


def capacity(value, unit):
    return number(value) * (1000 if unit in {"тб", "tb"} else 1)


def verified_models(item):
    slug = normalize(item["url"]).rstrip("/").rsplit("/", 1)[-1]
    return [code for code, _ in VERIFIED_MODELS
            if re.search(rf"(?<![\w-]){re.escape(code)}(?![a-z0-9])", slug)]


def budget_limit(text):
    match = BUDGET.search(text)
    if not match:
        return None
    return number(re.sub(r"\s", "", match[1])) * (100_000 if match[2] else 100)


@dataclass(frozen=True)
class ProductScope:
    """Совместные ограничения запроса к цене, каталожной категории и характеристикам."""

    category: str
    capacity_gb: Decimal | None = None
    diagonal: tuple[Decimal, Decimal] | None = None
    refresh_hz: int | None = None
    gaming: bool = False
    drive_type: str | None = None
    max_price: Decimal | None = None
    requirements: tuple[str, ...] = ()
    model: str | None = None
    watts: int | None = None

    def matches(self, item):
        """Пропускает позицию, для которой подтверждены все заданные условия."""
        name, category, description = (normalize(item[key]) for key in ("name", "category", "description"))
        if category not in {normalize(value) for value in CATEGORIES[self.category]}:
            return False
        if self.max_price is not None and (item["price_minor"] is None or item["price_minor"] > self.max_price):
            return False
        identity = normalize_codes(name)
        if self.model and not re.search(rf"\b{re.escape(self.model)}\b", identity):
            return False
        facts = name + " " + description
        matched_models = verified_models(item)
        facts += "".join(f" {extra}" for code, extra in VERIFIED_MODELS if code in matched_models)
        if self.category == "office" and not re.match(r"^(?:microsoft|office)\s*365\b", name):
            return False
        if any(not re.search(pattern, facts) for pattern in self.requirements):
            return False
        if self.watts is not None and not any(
            int(watts) >= self.watts for watts in re.findall(r"\b(\d{2,3})\s*(?:вт|w)\b", facts)
        ):
            return False

        if self.category == "monitor":
            diagonal = DIAGONAL.search(name) or DIAGONAL.search(description)
            if self.diagonal and (not diagonal or diagonal[2]
                                  or not self.diagonal[0] <= number(diagonal[1]) <= self.diagonal[1]):
                return False
            refresh = {int(hz) for hz in REFRESH.findall(facts)}
            return ((not self.refresh_hz or self.refresh_hz in refresh)
                    and (not self.gaming or GAMING.search(facts) or any(hz >= 120 for hz in refresh)))

        if self.category == "ssd" and (EXTERNAL.search(facts) or re.search(r"\busb\b", facts)):
            return False
        if self.category in {"external", "ssd"}:
            # В заголовках внешних SSD встречается HDD, поэтому приоритет у поля типа накопителя.
            media = DRIVE_TYPE.fullmatch(re.split(r"[,•]", description, 1)[0].strip()) or DRIVE_TYPE.search(name)
            if self.drive_type and (not media or media[0] != self.drive_type):
                return False
            sizes = CAPACITY.findall(description) or CAPACITY.findall(name)
            if self.capacity_gb is not None and {capacity(*size) for size in sizes} != {self.capacity_gb}:
                return False
        return True


def product_scope(query, previous=""):
    """Извлекает ограничения из запроса и применимого уточнения покупателя."""
    current = normalize_codes(query)
    prior = normalize_codes(previous)
    mentions = [(match.start(), category) for category, pattern in STARTS if (match := re.search(pattern, current))]
    prior_mentions = [(match.start(), category) for category, pattern in STARTS if (match := re.search(pattern, prior))]
    if not mentions and not CATALOG_TOOL_REQUEST.search(current) and (dell := DELL_DISPLAY.search(current)):
        mentions = [(dell.start(), "monitor")]
    # Диагональ вместе с параметром матрицы помогает распознать запрос без слова «монитор».
    if not (mentions or OTHER_SCREEN.search(current) or CATALOG_TOOL_REQUEST.search(current)):
        if (diagonal := DIAGONAL.search(current)) and (DISPLAY_FORMAT.search(current) or REFRESH.search(current)):
            mentions = [(diagonal.start(), "monitor")]
    family = r"\b(?:elements portable|my book|dashdrive)\b"
    if not mentions and CAPACITY.search(current) and (drive_family := re.search(family, current)):
        mentions = [(drive_family.start(), "external")]
    if not mentions and not prior_mentions:
        return None
    category = min(mentions or prior_mentions)[1]
    if category == "drive":
        # Упоминание комплектующего в запросе о ноутбуке не меняет категорию товара.
        category = "external" if EXTERNAL.search(current or prior) or (not mentions and EXTERNAL.search(prior)) else "ssd"
        if category == "ssd" and not (DRIVE_TYPE.search(current + " " + prior) or re.search(r"\bnvme\b", current)):
            return None
    text = current + " " + prior if not mentions or (prior_mentions and min(prior_mentions)[1] == min(mentions)[1]) else current
    max_price = budget_limit(text)
    requirements = [evidence for request, evidence in REQUIREMENTS.get(category, ()) if re.search(request, text)]
    if category in {"monitor", "external"} and re.search(USB_C, text):
        requirements.append(USB_C)
    if category in {"external", "ssd"}:
        for name in DRIVE_NAMES:
            if re.search(rf"\b{re.escape(name)}\b", text):
                requirements.append(rf"\b{re.escape(name)}\b")
    if category == "external":
        usb = re.search(r"\busb\s*(\d\.\d)\b", text)
        if usb:
            requirements.append(r"\busb\s*" + re.escape(usb[1]) + r"\b")
    if category == "switch":
        ports = re.search(r"\b(\d+)\s+(?:гигабит\w*\s+)?порт\w*", text)
        if ports:
            requirements.append(rf"\b{ports[1]}\s*[x×х]\s*(?:gigabit|1\s*гбит)")
    if category in {"gpu", "laptop"}:
        gpu = re.search(r"\brtx\s*(\d{4})(\s*(?:ti|super)\b)?", text)
        if gpu:
            suffix = rf"\s*{gpu[2].strip()}\b" if gpu[2] else r"(?!\s*(?:ti|super)\b)"
            requirements.append(rf"\brtx\s*{gpu[1]}\b{suffix}")
    if category == "laptop":
        for size, unit in CAPACITY.findall(text):
            ram = r"\s*(?:ram|озу)\b" if unit in {"gb", "гб"} else ""
            requirements.append(rf"\b{re.escape(size)}\s*{unit}\b{ram}")
    if category == "office":
        if not re.search(r"\b(?:standard|стандарт|outlook|word|excel|exchange)\b", text):
            return None
        requirements.extend((r"\b(?:microsoft|office)\s*365\b", r"\b(?:business standard|бизнес стандарт)\b",
                             r"\b(?:за 1 лицензию|на 1 рабочее место)\b"))
        if OFFICE_ANNUAL.search(text):
            requirements.append(r"\b(?:1 год|12 мес)\b")
    if category == "memory":
        memory = re.findall(r"\bddr[345]\b", text)
        if len(set(memory)) != 1 or not re.search(r"\b(?:udimm|so-?dimm|dimm)\b", text):
            return None
        requirements.append(rf"\b{memory[0]}\b")
        for factor in ("udimm", "sodimm", "dimm"):
            if re.search(rf"\b{factor}\b", text):
                requirements.append(rf"\b{factor}\b")
                break
    diagonal = DIAGONAL.search(text) if category == "monitor" else None
    refresh = REFRESH.search(text) if category == "monitor" else None
    gaming = category == "monitor" and bool(GAMING.search(text))
    power = re.search(r"\b(\d{2,3})\s*(?:вт|w)\b", text) if category == "monitor" else None
    model = MODEL.search(text) if category == "monitor" else None
    if category == "monitor":
        for brand in ("dell", "philips", "samsung", "asus", "acer", "msi", "lenovo", "benq", "aoc", "lg"):
            if re.search(rf"\b{brand}\b", text):
                requirements.append(rf"\b{brand}\b")
                break
    drive = category in {"external", "ssd"}
    size = CAPACITY.search(text) if drive else None
    kind = DRIVE_TYPE.search(text) if drive else None
    if not any((diagonal, refresh, gaming, power, model, size, kind, max_price is not None, requirements)):
        return None
    sizes = tuple(sorted((number(diagonal[1]), number(diagonal[2] or diagonal[1])))) if diagonal else None
    return ProductScope(category, capacity(*size.groups()) if size else None, sizes,
                        int(refresh[1]) if refresh else None, gaming,
                        kind[0] if kind else ("ssd" if category == "ssd" else None),
                        max_price, tuple(requirements), model[0] if model else None,
                        int(power[1]) if power else None)

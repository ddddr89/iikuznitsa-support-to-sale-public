"""Verify supported product categories and specifications against catalog fields."""

from dataclasses import dataclass
from decimal import Decimal
import re

NUMBER = r"\d+(?:[.,]\d+)?"
CAPACITY = re.compile(rf"(?<![\w.,])({NUMBER})\s*(тб|tb|гб|gb)\b")
DIAGONAL = re.compile(
    rf"(?<![\w.,])({NUMBER})(?:\s*[-–—]\s*({NUMBER}))?"
    r'\s*(?:дюйм\w*|["″]|-?\s*inch\b|in\b)'
)
REFRESH = re.compile(r"(?<!\w)(\d{2,3})\s*(?:гц|hz)\b")
GAMING = re.compile(r"\b(?:игров\w*|gaming)\b")
DRIVE = re.compile(r"\b(?:диск\w*|накопител\w*|ssd|hdd)\b")
EXTERNAL = re.compile(r"\b(?:внешн\w*|портативн\w*|external|portable)\b")
MONITOR = re.compile(r"\b(?:монитор\w*|monitors?)\b")
DRIVE_TYPE = re.compile(r"\b(?:ssd|hdd)\b")
CATEGORIES = {
    "monitor": ("Мониторы", "Монитор", "Monitors", "Monitor"),
    "external": ("Внешние HDD", "Внешние SSD", "Внешние накопители",
                 "Внешние жесткие диски", "Внешние жёсткие диски"),
    "ssd": ("Внутренние твердотельные накопители (SSD)",),
}


def normalize(text):
    return text.casefold().replace("ё", "е")


def number(value):
    return Decimal(value.replace(",", "."))


def capacity(value, unit):
    return number(value) * (1000 if unit in {"тб", "tb"} else 1)


@dataclass(frozen=True)
class ProductScope:
    category: str
    capacity_gb: Decimal | None = None
    diagonal: tuple[Decimal, Decimal] | None = None
    refresh_hz: int | None = None
    gaming: bool = False
    drive_type: str | None = None

    def matches(self, item):
        name, category, description = (normalize(item[key]) for key in ("name", "category", "description"))
        if category not in {normalize(value) for value in CATEGORIES[self.category]}:
            return False
        if self.category == "monitor":
            # A diagonal belongs to the product itself, so prefer the title to contextual text.
            diagonal = DIAGONAL.search(name) or DIAGONAL.search(description)
            if self.diagonal and (not diagonal or diagonal[2]
                                  or not self.diagonal[0] <= number(diagonal[1]) <= self.diagonal[1]):
                return False
            refresh = {int(hz) for hz in REFRESH.findall(name + " " + description)}
            return ((not self.refresh_hz or self.refresh_hz in refresh)
                    and (not self.gaming or GAMING.search(name) or GAMING.search(description)
                         or any(hz >= 120 for hz in refresh)))

        # The feed labels even some external SSDs as HDD in the category and title.
        # Its first description field is the actual media type when present.
        media = DRIVE_TYPE.fullmatch(description.split(",", 1)[0].strip()) or DRIVE_TYPE.search(name)
        if self.drive_type and (not media or media[0] != self.drive_type):
            return False
        sizes = CAPACITY.findall(name) or CAPACITY.findall(description)
        return self.capacity_gb is None or {capacity(*size) for size in sizes} == {self.capacity_gb}


def product_scope(query):
    query = normalize(query)
    if MONITOR.search(query):
        diagonal = DIAGONAL.search(query)
        refresh = REFRESH.search(query)
        gaming = bool(GAMING.search(query))
        if diagonal or refresh or gaming:
            sizes = tuple(sorted((number(diagonal[1]), number(diagonal[2] or diagonal[1])))) if diagonal else None
            return ProductScope("monitor", diagonal=sizes, refresh_hz=int(refresh[1]) if refresh else None,
                                gaming=gaming)

    if DRIVE.search(query):
        size = CAPACITY.search(query)
        kind = DRIVE_TYPE.search(query)
        if EXTERNAL.search(query) or (size and kind and kind[0] == "ssd"):
            return ProductScope("external" if EXTERNAL.search(query) else "ssd",
                                capacity(*size.groups()) if size else None,
                                drive_type=kind[0] if kind else None)
    return None

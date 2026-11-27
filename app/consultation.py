"""Уточняет запросы по этапам, сохраняя ответы из активного диалога."""

from dataclasses import dataclass, field
import re

from app.catalog_advice import item_text, price_text
from app.catalog_matching import budget_limit, normalize, product_scope

PREFIX = "Для подбора защиты почты"
CONCLUSION = "По защите почты:"
MAIL = re.compile(r"\b(?:почт\w*|письм\w*|ящик\w*|exchange online)\b")
SECURITY = re.compile(r"\b(?:защит\w*|фишинг\w*|антиспам\w*|спам\w*|вредоносн\w*)\b")
SEATS = re.compile(r"(?<!\d)([1-9]\d{0,5})\s+(?:почтов\w+\s+)?(?:ящик\w*|пользовател\w*)\b")
UNKNOWN = re.compile(r"\b(?:не знаю|не уверен\w*|не могу уточнить|неизвестно)\b")
PLATFORMS = {
    "Exchange Online": r"exchange online",
    "Локальный Exchange": r"(?:локальн\w*\s+(?:microsoft\s+)?exchange|exchange\s+server|exchange\s+на\s+сво)",
    "Microsoft 365": r"(?:microsoft|office)\s*365",
    "Яндекс 360": r"яндекс\s*360",
    "Google Workspace": r"google workspace",
}
THREATS = {"спам": r"\b(?:анти)?спам\w*", "фишинг": r"\bфишинг\w*",
           "вредоносные вложения": r"вредоносн\w*\s+вложен\w*"}
SERVICE_TOPIC = re.compile(r"\b(?:достав\w*|самовывоз\w*|возврат\w*|обмен\w*|эдо|"
                           r"оплат\w*|статус\w*\s+(?:моего\s+)?заказ\w*)\b")
FOLLOW_PRODUCT = re.compile(r"\b(?:этот|эту|эти|такой|у него|их|сколько стоит|а цена|"
                             r"а по цене|чем отличаются|разница в цене)\b")
REJECTION = re.compile(r"^(?:нет[,.! ]+)?(?:не\s+подходит|не\s+нужен|не\s+хочу\s+"
                       r"(?:этот|эту|эти)|отказываюсь)(?:\W|$)")
PURCHASE_CONTINUE = re.compile(r"^(?:да[,. ]+)?(?:(?:давайте|хочу)\s+)?"
                               r"(?:оформим|оформить|оформляйте|продолжим оформление)"
                               r"(?:\s+заказ)?[.!]?$")


def active_turns(message, history):
    """Не передаёт старый товар в новую консультацию или справочный вопрос."""
    current = normalize(message)
    if SERVICE_TOPIC.search(current) or product_scope(message) or REJECTION.search(current):
        return []
    turns = []
    for turn in reversed(history[-4:]):
        prior = normalize(turn["question"])
        if REJECTION.search(prior):
            break
        turns.append(turn)
        if product_scope(turn["question"]) or SERVICE_TOPIC.search(prior) or starts_mail(prior):
            break
    return list(reversed(turns))


@dataclass
class Slots:
    values: dict = field(default_factory=dict)
    unknown: set = field(default_factory=set)

    def set(self, key, value):
        if value is not None:
            self.values[key] = value
            self.unknown.discard(key)

    def remember_unknown(self, key):
        if key:
            self.unknown.add(key)


def starts_mail(text):
    return (MAIL.search(text) and SECURITY.search(text) and "microsoft 365 для" not in text)


def prompted_field(answer):
    if "сколько почтовых ящиков" in answer:
        return "seats"
    if "Microsoft 365 или Exchange" in answer or "Где размещена почта" in answer:
        return "platform"
    if "Какие угрозы" in answer or "Нужна защита от" in answer:
        return "threats"
    return None


def collect(slots, text, asked):
    number = SEATS.search(text)
    if not number and asked == "seats":
        number = re.fullmatch(r"(?:примерно\s+)?([1-9]\d{0,5})[.!]?", text)
    slots.set("seats", int(number[1]) if number else None)
    for name, pattern in PLATFORMS.items():
        if re.search(pattern, text):
            slots.set("platform", name)
            break
    else:
        if re.fullmatch(r"(?:microsoft|майкрософт)[.!]?", text):
            slots.set("platform", "Microsoft (сервис не уточнён)")
        elif asked == "platform" and re.fullmatch(r"exchange[.!]?", text):
            slots.set("platform", "Exchange (развёртывание не уточнено)")
    threats = [name for name, pattern in THREATS.items() if re.search(pattern, text)]
    if threats:
        slots.set("threats", threats)
    elif asked == "threats" and re.fullmatch(r"да[.!]?", text):
        slots.set("threats", list(THREATS))
    elif asked == "threats" and re.fullmatch(r"нет[.!]?", text):
        slots.remember_unknown("threats")
    slots.set("budget", budget_limit(text))
    if UNKNOWN.search(text):
        slots.remember_unknown(asked)


def verified_options(catalog, values):
    platform = values["platform"]
    if platform not in PLATFORMS:
        return []
    products = catalog.search("защита почты", 30)
    confirmed = []
    for item in products:
        facts = normalize(item["name"] + " " + item["description"])
        if not MAIL.search(facts) or re.search(r"\b(?:не поддерж\w*|нет защит\w*)\b", facts):
            continue
        budget = values.get("budget")
        if budget is not None and (item["price_minor"] is None or item["price_minor"] > budget):
            continue
        if (re.search(PLATFORMS[platform], facts)
                and all(re.search(THREATS[threat], facts) for threat in values["threats"])
                and {int(count) for count in SEATS.findall(facts)} == {values["seats"]}):
            confirmed.append(item)
    return confirmed[:3]


def mail_consultation(message, history, catalog, *, new_product=False):
    current = normalize(message).strip()
    active = []
    for turn in reversed(history):
        if not turn["answer"].startswith((PREFIX, CONCLUSION)):
            break
        active.append(turn)
    is_start = starts_mail(current)
    if not is_start and (not active or new_product or len(current.split()) > 10 or
                         re.match(r"^(?:нужен|нужна|хочу купить|подберите|найдите)\s", current)):
        return None
    if is_start and (not active or active[0]["answer"].startswith(CONCLUSION)):
        active = []
    if active and active[0]["answer"].startswith(CONCLUSION):
        if "Статус обращения: ожидает ответа менеджера." in active[0]["answer"] or "вопрос уже передан" in active[0]["answer"]:
            return CONCLUSION + " вопрос уже передан менеджеру с уточнениями.", [], None, False
        if "каталог подтверждает" not in active[0]["answer"]:
            return CONCLUSION + " менеджер может проверить варианты по указанным данным.", [], None, False
    slots = Slots()
    asked = None
    for turn in reversed(active):
        collect(slots, normalize(turn["question"]).strip(), asked)
        asked = prompted_field(turn["answer"])
    previous_values = slots.values.copy()
    collect(slots, current, asked)
    progressed = previous_values != slots.values
    platform = slots.values.get("platform")
    if not slots.values.get("seats"):
        question = "сколько почтовых ящиков нужно защитить?"
    elif not platform:
        question = "Где размещена почта: в облачном сервисе или на собственном сервере?"
    elif platform == "Microsoft (сервис не уточнён)":
        question = "У вас Microsoft 365 или Exchange на собственном сервере?"
    elif platform == "Exchange (развёртывание не уточнено)":
        question = "У вас Exchange Online или Exchange на собственном сервере?"
    elif not slots.values.get("threats"):
        question = "Нужна защита от спама, фишинга и вредоносных вложений?"
    else:
        question = None
    prompt = f"{PREFIX}. {question}" if question else None
    previous_prompts = {turn["answer"] for turn in active}
    if prompt and not slots.unknown and (prompt not in previous_prompts or progressed):
        if progressed and prompt in previous_prompts and platform:
            prompt += f" Платформа — {platform}."
        return prompt, [], None, False

    options = verified_options(catalog, slots.values) if question is None and not slots.unknown else []
    if options:
        details = "\n".join(f"• {item_text(item)}. {item['description']}" for item in options)
        return f"{CONCLUSION} каталог подтверждает следующие позиции:\n{details}", options, None, True
    budget = slots.values.get("budget")
    threats = ", ".join(slots.values.get("threats", [])) or "не уточнены"
    subject = (f"Защита корпоративной почты. Ящики: {slots.values.get('seats', 'не уточнено')}. "
               f"Платформа: {platform or 'не уточнена'}. Угрозы: {threats}. "
               f"Бюджет: {price_text(int(budget)) if budget is not None else 'не указан'}.")
    text = (f"{CONCLUSION} для точного подбора нужно подтвердить платформу и условия лицензии. "
            "Менеджер проверит варианты по уже собранным данным.")
    return text, [], subject, question is None and not slots.unknown

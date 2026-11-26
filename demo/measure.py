"""Measure real YandexGPT response usage without recording prompts or credentials."""

import csv
import os
import re
import sys
from unittest.mock import patch

from app import ai
from app.catalog import Catalog
from app.knowledge import Knowledge


def main():
    catalog = Catalog(os.getenv("APP_DB_PATH", ".stand/catalog.sqlite3"))
    knowledge = Knowledge(os.getenv("APP_KNOWLEDGE_DB", ".stand/knowledge.sqlite3"))
    post = ai.post_json
    rows = []
    history = []

    for scenario, question, required, continuation in (
        ("sku_lookup", "Вызови search_catalog с запросом U2424H и опиши найденный монитор.", True, False),
        ("knowledge_delivery", "Как забрать заказ самовывозом?", False, False),
        ("consultation_1", "Нужна защита почты и файлов на 50 человек. Что уточнить?", False, False),
        ("consultation_2", "Облачное решение на 50 человек, защищаем корпоративную почту.", False, True),
    ):
        call = 0

        def measure(url, body, headers, timeout):
            nonlocal call
            response = post(url, body, headers, timeout)
            usage = response.get("usage") or {}
            if not {"input_tokens", "output_tokens", "total_tokens"} <= usage.keys():
                raise ValueError("Provider usage is missing")
            call += 1
            model = response.get("model", "").rsplit("/", 1)[-1]
            if not re.fullmatch(r"[a-z0-9._:-]{1,80}", model):
                raise ValueError("Unknown model identifier format")
            inputs = usage["input_tokens"]
            outputs = usage["output_tokens"]
            details = usage.get("input_tokens_details") or {}
            cached = details.get("cached_tokens", 0)
            rows.append((scenario, call, model, inputs, cached, outputs, usage["total_tokens"]))
            return response

        with patch.object(ai, "post_json", measure):
            response, _ = ai._send_yandex(
                os.environ["YANDEX_API_KEY"], os.environ["YANDEX_FOLDER_ID"], question,
                [] if required else catalog.search(question, 5), [], catalog.search, required,
                knowledge=knowledge.search(question),
                history=history if continuation else (),
            )
        history = [{"question": question, "answer": response}]
    writer = csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(["scenario", "provider_call", "reported_model", "input_tokens", "cached_tokens",
                     "output_tokens", "total_tokens"])
    writer.writerows(rows)


if __name__ == "__main__":
    main()

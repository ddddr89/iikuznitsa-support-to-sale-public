# Каталог Softline

В `data/softline-sample.csv` шесть позиций из открытых карточек Softline Store. Артикулы и цены сверены с JSON-LD карточек 26 ноября 2026 года. Ссылки в CSV ведут к карточкам. Цены отражают момент выгрузки и перед реальными продажами требуют проверки.

```bash
.venv/bin/python -m app.catalog "$APP_DB_PATH" data/softline-sample.csv --source softline_sample --expected-count 6
```

При проверке поиска используйте `U2424H`, «ноутбук», «сервер для виртуализации» и «ИБП». Режим `softline_sample` означает выборку из витрины. Режим `demo` используется для синтетического набора.

## Полная выгрузка и обновление

Softline описывает экспорт товарных данных для клиентов через XML/API. Страница прайс-листов запрашивает контактные данные и согласие на обработку. Публичный URL полного товарного снимка для проекта пока не подтверждён. Нужен экспорт или ссылка, предоставленная Softline либо организаторами. [Интеграция Softline](https://softlinegroup.com/upload/iblock/83b/5eis9ygmgsm52exa4x80gvipncvi5djj/GK-Softline-_2_.pdf), [прайс-листы](https://store.softline.ru/pricelists/).

Для подтверждённого YML снимка передайте число предложений из выгрузки. SKU берётся из `offer@id`. Название берётся из `name` либо собирается из `typePrefix`, `vendor` и `model`. Цена отсутствует у недоступных товаров и для валют за пределами RUB/RUR. CSV принимает тот же формат, что и демонстрационный файл, с ценой в копейках.

```bash
.venv/bin/python -m app.catalog_feed "$APP_DB_PATH" --file "$SOFTLINE_YML_PATH" --expected-count "$SOFTLINE_EXPECTED_COUNT"
.venv/bin/python -m app.catalog_feed "$APP_DB_PATH" --expected-count "$SOFTLINE_EXPECTED_COUNT"
.venv/bin/python -m app.catalog "$APP_DB_PATH" "$SOFTLINE_CSV_PATH" --source softline_feed --expected-count "$SOFTLINE_EXPECTED_COUNT"
```

Используйте команду, подходящую для формата источника. Для загрузки по сети передайте конечную HTTPS-ссылку в `SOFTLINE_FEED_URL` через окружение. Значение ссылки может содержать токен доступа. Скачивание выполняется во временный файл до замены SQLite. Обновление атомарно пересобирает индекс FTS5 и записывает источник, число SKU и время импорта. Дублирующиеся SKU, повреждённая выгрузка и расхождение числа позиций сохраняют предыдущий снимок. Уже созданные КП сохраняют зафиксированные цены.

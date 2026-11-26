# Шлюз ИИ в n8n

1. Импортируйте `workflow.json` в отдельный workflow через Import from File. Откройте узел **Chat webhook** и привяжите credential типа **Header Auth** с именем заголовка `X-Webhook-Secret` и случайным секретом в поле Value.
2. В **YandexGPT** привяжите отдельный Header Auth. В поле Name укажите `Authorization`, в Value задайте `Api-Key <API-ключ>`. Замените `REPLACE_FOLDER_ID` в JSON Body на идентификатор каталога сервисного аккаунта. [Yandex AI Studio](https://aistudio.yandex.ru/ru/docs/ai-studio/api-ref/authentication)
3. В **OpenAI** привяжите отдельный Header Auth. В поле Name укажите `Authorization`, в Value задайте `Bearer <API-ключ>`. [GPT-5.4 mini](https://developers.openai.com/api/docs/models/gpt-5.4-mini)
4. Опубликуйте workflow. Из узла **Chat webhook** возьмите Production URL и передайте его FastAPI через `N8N_WEBHOOK_URL`. Через `N8N_WEBHOOK_SECRET` передайте тот же секрет, который хранится в credential вебхука. Используйте локальную сеть либо HTTPS для транспорта. [Webhook в n8n](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.webhook/)
5. Для проверки отправьте запрос через `POST /api/v1/chat`. При работающем YandexGPT поле `provider` должно быть `yandex`. В тестовой копии workflow временно укажите недоступную модель YandexGPT, чтобы проверить `openai`. Затем отключите webhook и уберите ключи из окружения тестового FastAPI, чтобы проверить `catalog`. Карточки каталога должны остаться в ответе во всех сценариях.

Каждый провайдер получает один запрос с таймаутом 7 секунд, общий лимит workflow 17 секунд, таймаут адаптера 18 секунд. При сбое webhook FastAPI пробует прямые вызовы из раздела ниже. Поле `provider` сообщает, откуда получен ответ. Контакты из формы заявки через webhook не передаются. Сообщение чата передаётся провайдерам, поэтому для проверки используйте сообщения без персональных данных. Хранение успешных, ошибочных и ручных запусков в экспортируемом workflow отключено.

## Прямое подключение для стенда

FastAPI вызывает YandexGPT напрямую через Responses API, если webhook не настроен или завершился ошибкой. Для прямого вызова сервис FastAPI получает `YANDEX_API_KEY` и `YANDEX_FOLDER_ID` из внешнего файла окружения, например через `EnvironmentFile` в systemd. Для GPT‑5.4 mini можно задать `OPENAI_API_KEY`. Без действующих провайдеров чат выдаёт каталожный ответ.

Прямой вызов YandexGPT может один раз обратиться к локальному инструменту `search_catalog`. Функция ограничена пятью товарами и проверяет длину запроса. История действий возвращается через `actions`, без текста запроса и внутренних параметров. Workflow n8n использует карточки, найденные FastAPI до обращения к модели.

Для проверки доступа и вызова инструмента запустите из корня проекта:

```bash
(
  set -a
  . /home/demon/.config/iikuznitsa/secrets.env
  set +a
  python3 -m app.ai
)
```

Проверка выполняет два запроса с тестовым текстом и печатает только результат. Документация сценария доступна в [Yandex AI Studio](https://aistudio.yandex.ru/ru/docs/ai-studio/operations/agents/create-function-text-agent).

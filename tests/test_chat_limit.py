from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from fastapi import HTTPException

from app.catalog import Catalog
from app.commercial import Commercial
from app.knowledge import Knowledge
from app.main import Chat, SESSION_COOKIE, create_app


class ChatLimitTests(TestCase):
    def test_active_session_has_own_limit_and_global_limit_still_applies(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "chat.sqlite3"
            Catalog(path).init()
            Knowledge(path).init()
            store = Commercial(path)
            store.init()
            requests = []
            for token in ("A" * 43, "B" * 43):
                store.create_session(sha256(token.encode()).hexdigest(), 600)
                requests.append(SimpleNamespace(cookies={SESSION_COOKIE: token}))
            anonymous = SimpleNamespace(cookies={})
            with patch.dict("os.environ", {
                "APP_KNOWLEDGE_DB": str(path), "APP_CHAT_PER_MINUTE": "3",
                "APP_CHAT_PER_SESSION_PER_MINUTE": "1",
            }):
                app = create_app(path)
            chat = next(route.endpoint for route in app.routes if route.path == "/api/v1/chat")
            body = Chat(message="Проверка ответа")
            def reply(*args, **kwargs):
                return {"answer": "Ответ", "provider": "catalog", "actions": []}
            with patch("app.main.reply", side_effect=reply) as provider, patch("app.main.monotonic") as clock:
                clock.return_value = 10
                chat(body, requests[0])
                with self.assertRaises(HTTPException) as blocked:
                    chat(body, requests[0])
                self.assertEqual(blocked.exception.status_code, 429)
                chat(body, requests[1])
                chat(body, anonymous)
                with self.assertRaises(HTTPException):
                    chat(body, anonymous)
                self.assertEqual(provider.call_count, 3)
                clock.return_value = 70
                chat(body, requests[0])
                self.assertEqual(provider.call_count, 4)

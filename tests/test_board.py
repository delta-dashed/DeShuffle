import asyncio
import json
import os
import stat
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from board.client import BoardError, ExcaliDash, normalize_url
from board.store import BoardStore, board_message, default_name, list_message
from recorder.core import Session

BASE = "https://board.example:8443"
PASSWORD = "s3cret-pass"


class FakeTransport:
    """Scripted ExcaliDash: answers by (method, path) and records every request."""

    def __init__(self, auth_enabled=True, password=PASSWORD):
        self.auth_enabled = auth_enabled
        self.password = password
        self.requests = []
        self.signed_in = False
        self.expire_session_once = False
        self.down = False
        self.closed = False

    async def request(self, method, url, *, headers=None, json=None):
        assert url.startswith(BASE + "/api/"), url
        path = url[len(BASE) + 4:]
        self.requests.append((method, path, dict(headers or {}), json))
        if self.down:
            raise OSError("connection refused")
        if path == "/auth/status":
            return 200, {"authEnabled": self.auth_enabled, "bootstrapRequired": False}
        if path == "/csrf-token":
            return 200, {"token": "csrf-1", "header": "x-csrf-token"}
        if method != "GET" and (headers or {}).get("x-csrf-token") != "csrf-1":
            return 403, {"error": "CSRF token missing"}
        if path == "/auth/login":
            if json.get("password") != self.password:
                return 401, {"error": "Unauthorized", "message": "Invalid email or password"}
            self.signed_in = True
            return 200, {"user": {"id": "u1", "mustResetPassword": False}}
        if self.auth_enabled and not self.signed_in:
            return 401, {"error": "Unauthorized"}
        if self.expire_session_once:
            self.expire_session_once = False
            self.signed_in = False
            return 401, {"error": "Unauthorized"}
        if method == "POST" and path == "/drawings":
            return 200, {"id": "d-1", "name": json["name"]}
        if method == "POST" and path == "/drawings/d-1/link-shares":
            return 200, {"share": {"id": "s-1", "permission": json["permission"], "expiresAt": json["expiresAt"]}}
        if method == "GET" and path.startswith("/drawings"):
            return 200, {"drawings": [], "totalCount": 3}
        return 404, {"error": "Not found"}

    async def close(self):
        self.closed = True


def run(coro):
    return asyncio.run(coro)


class ClientTests(unittest.TestCase):
    def test_create_board_signs_in_and_opens_an_edit_link_for_seven_days(self):
        transport = FakeTransport()
        client = ExcaliDash(BASE + "/", "bot", PASSWORD, transport=transport)
        now = datetime(2026, 11, 17, 18, 0, tzinfo=timezone.utc)
        board = run(client.create_board("MasterMind 17.11.2026 21:00", now=now))
        self.assertEqual(board["id"], "d-1")
        self.assertEqual(board["url"], BASE + "/shared/d-1")
        share = [r for r in transport.requests if r[1] == "/drawings/d-1/link-shares"][0]
        self.assertEqual(share[3]["permission"], "edit")
        expires = datetime.fromisoformat(share[3]["expiresAt"])
        self.assertEqual(expires - now, timedelta(days=7))
        paths = [r[1] for r in transport.requests]
        self.assertLess(paths.index("/auth/login"), paths.index("/drawings"))

    def test_expired_session_signs_in_again_once(self):
        transport = FakeTransport()
        client = ExcaliDash(BASE, "bot", PASSWORD, transport=transport)
        run(client.check())
        transport.expire_session_once = True
        board = run(client.create_board("x"))
        self.assertEqual(board["id"], "d-1")
        self.assertEqual([r[1] for r in transport.requests].count("/auth/login"), 2)

    def test_wrong_password_is_reported_without_the_password(self):
        transport = FakeTransport(password="other")
        client = ExcaliDash(BASE, "bot", PASSWORD, transport=transport)
        with self.assertRaises(BoardError) as caught:
            run(client.create_board("x"))
        self.assertIn("логин или пароль", str(caught.exception))
        self.assertNotIn(PASSWORD, str(caught.exception))
        self.assertNotIn(PASSWORD, repr(client))

    def test_single_user_board_needs_no_credentials(self):
        transport = FakeTransport(auth_enabled=False)
        board = run(ExcaliDash(BASE, transport=transport).create_board("x"))
        self.assertEqual(board["url"], BASE + "/shared/d-1")
        self.assertNotIn("/auth/login", [r[1] for r in transport.requests])

    def test_missing_credentials_and_network_errors_are_explained(self):
        with self.assertRaises(BoardError) as caught:
            run(ExcaliDash(BASE, transport=FakeTransport()).check())
        self.assertIn("логин и пароль", str(caught.exception))
        transport = FakeTransport()
        transport.down = True
        with self.assertRaises(BoardError) as caught:
            run(ExcaliDash(BASE, "bot", PASSWORD, transport=transport).check())
        self.assertIn("недоступна", str(caught.exception))

    def test_url_is_normalized_and_validated(self):
        self.assertEqual(normalize_url(" https://h:8443/api/ "), "https://h:8443")
        with self.assertRaises(BoardError):
            normalize_url("h:8443")


class StoreTests(unittest.TestCase):
    def test_config_file_is_private_and_status_hides_the_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BoardStore(Path(tmp) / "board.json")
            self.assertFalse(store.configured)
            store.configure(BASE, "bot", PASSWORD)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(os.stat(store.path).st_mode), 0o600)
            status = store.public_status()
            self.assertNotIn(PASSWORD, json.dumps(status))
            self.assertTrue(status["password_set"])
            reloaded = BoardStore(store.path)
            self.assertEqual(reloaded.credentials(), (BASE, "bot", PASSWORD))
            reloaded.clear()
            self.assertFalse(BoardStore(store.path).configured)

    def test_history_lists_live_boards_of_the_server_and_reuses_meeting_boards(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BoardStore(Path(tmp) / "board.json")
            now = time.time()
            future = datetime.fromtimestamp(now + 86400, timezone.utc).isoformat()
            past = datetime.fromtimestamp(now - 60, timezone.utc).isoformat()
            store.remember({"id": "old", "name": "a", "url": "u0", "expires": past}, guild_id=1, channel_id=5, now=now)
            store.remember({"id": "other", "name": "b", "url": "u1", "expires": future}, guild_id=2, channel_id=6,
                           now=now)
            store.remember({"id": "meet", "name": "c", "url": "u2", "expires": future}, guild_id=1, channel_id=5,
                           meeting=True, now=now - 600)
            self.assertEqual([b["id"] for b in store.recent(1, now=now)], ["meet"])
            self.assertEqual(store.meeting_board(5, now=now)["id"], "meet")
            self.assertIsNone(store.meeting_board(5, now=now + 4 * 3600))
            self.assertIsNone(store.meeting_board(6, now=now))


class MessageTests(unittest.TestCase):
    def test_board_message_and_default_name(self):
        tz = ZoneInfo("Europe/Moscow")
        board = {"url": BASE + "/shared/d-1", "name": "MasterMind 17.11.2026 21:00",
                 "expires": "2026-11-24T18:00:00+00:00"}
        text = board_message(board, tz)
        self.assertTrue(text.startswith("🧩 Доска: " + BASE + "/shared/d-1"))
        self.assertIn("до 24.11 21:00", text)
        self.assertTrue(board_message(board, tz, meeting=True).startswith("🧩 Доска для этой встречи:"))
        self.assertEqual(default_name("MasterMind", datetime(2026, 11, 17, 21, 5)), "MasterMind 17.11.2026 21:05")
        self.assertIn(BASE + "/shared/d-1", list_message([board], tz))
        self.assertIn("/board", list_message([], tz))


class CogTests(unittest.TestCase):
    def test_meeting_board_is_created_once_and_failures_return_none(self):
        from board.cog import Boards

        transport = FakeTransport()
        with tempfile.TemporaryDirectory() as tmp:
            cog = Boards(SimpleNamespace(), tmp,
                         client_factory=lambda url, login, password: ExcaliDash(url, login, password,
                                                                                transport=transport))
            channel = SimpleNamespace(id=5, name="MasterMind", guild=SimpleNamespace(id=1))

            async def scenario():
                self.assertIsNone(await cog.board_for_meeting(channel))  # not configured yet
                cog.store.configure(BASE, "bot", PASSWORD)
                first = await cog.board_for_meeting(channel)
                again = await cog.board_for_meeting(channel)
                created = [r[1] for r in transport.requests].count("/drawings")
                transport.down = True
                cog.store.data["boards"] = []
                broken = await cog.board_for_meeting(channel)
                await cog.cog_unload()
                return first, again, broken, created

            first, again, broken, created = run(scenario())
            self.assertEqual(first["id"], "d-1")
            self.assertTrue(first["name"].startswith("MasterMind "))
            self.assertEqual(again["created"], first["created"])
            self.assertEqual(created, 1)
            self.assertIsNone(broken)
            self.assertTrue(transport.closed)
            self.assertIn("🧩 Доска для этой встречи", cog.meeting_message(first))


class MeetingMetaTests(unittest.TestCase):
    def test_board_id_is_saved_in_meta_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(1, "MasterMind", "Guild", Path(tmp))
            session.board = {"id": "d-1", "url": BASE + "/shared/d-1", "name": "MasterMind 17.11.2026 21:00"}
            session.finish()
            meta = json.loads((session.dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["board"], "d-1")
            self.assertEqual(meta["board_url"], BASE + "/shared/d-1")


if __name__ == "__main__":
    unittest.main()

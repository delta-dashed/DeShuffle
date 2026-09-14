"""Webhook identity persistence and additive migration regressions."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from bookclub.store import ClubError, Store


CONFIG = dict(news=11, chat=12, books=13, essays=14, voice=15)


class WebhookMigrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "club.sqlite3"
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript("""
                PRAGMA user_version=7;
                CREATE TABLE legacy_notes(id INTEGER PRIMARY KEY, note TEXT);
                INSERT INTO legacy_notes VALUES(1,'preserved');
                CREATE TABLE bc_migrations(version INTEGER PRIMARY KEY);
                INSERT INTO bc_migrations VALUES(1),(2);
                CREATE TABLE bc_books(
                    id TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, title TEXT NOT NULL,
                    author TEXT NOT NULL, materials TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'proposed', position INTEGER NOT NULL DEFAULT 0,
                    deadline INTEGER, revision INTEGER NOT NULL DEFAULT 0,
                    request_key TEXT NOT NULL, UNIQUE(guild_id,request_key));
                INSERT INTO bc_books(id,guild_id,title,author,request_key)
                    VALUES('old-book',1,'Книга','Автор','old');
                CREATE TABLE bc_essays(
                    guild_id INTEGER NOT NULL, source_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL, book_id TEXT NOT NULL REFERENCES bc_books(id),
                    author_id INTEGER NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL,
                    deleted INTEGER NOT NULL DEFAULT 0, managed INTEGER NOT NULL DEFAULT 0,
                    submitted INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(guild_id,source_id));
                INSERT INTO bc_essays VALUES(1,100,100,'old-book',10,'Черновик','draft-url',0,1,0);
                INSERT INTO bc_essays VALUES(1,101,101,'old-book',11,'Эссе','essay-url',0,1,1);
                INSERT INTO bc_essays VALUES(1,102,102,'old-book',12,'Удалено','deleted-url',1,0,1);
                CREATE TABLE bc_publications(
                    key TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, channel_id INTEGER,
                    message_id INTEGER, state TEXT NOT NULL DEFAULT 'reserved', content_hash TEXT);
                INSERT INTO bc_publications VALUES('book:old-book',1,13,500,'ready','hash');
                INSERT INTO bc_publications(key,guild_id,channel_id) VALUES('essay:pending',1,14);
            """)

    def test_v2_upgrade_preserves_publications_essay_flags_and_legacy_data(self):
        store = Store(self.path)
        self.assertEqual(store.publication("book:old-book"), dict(
            key="book:old-book", guild_id=1, channel_id=13, message_id=500,
            state="ready", content_hash="hash", webhook_id=None))
        self.assertEqual(store.publication("essay:pending")["state"], "reserved")
        self.assertIsNone(store.publication("essay:pending")["webhook_id"])
        self.assertEqual(store.rows("SELECT source_id,deleted,managed,submitted FROM bc_essays ORDER BY source_id"), [
            dict(source_id=100, deleted=0, managed=1, submitted=0),
            dict(source_id=101, deleted=0, managed=1, submitted=1),
            dict(source_id=102, deleted=1, managed=0, submitted=1),
        ])
        self.assertEqual(store.one("SELECT note FROM legacy_notes")["note"], "preserved")
        self.assertEqual(store.one("PRAGMA user_version")["user_version"], 7)
        self.assertEqual(store.rows("SELECT version FROM bc_migrations ORDER BY version"),
                         [{"version": 1}, {"version": 2}, {"version": 3}, {"version": 4}])
        store.save_webhook(1, 14, 900)

        restored = Store(self.path)
        self.assertEqual(restored.publication("book:old-book"), store.publication("book:old-book"))
        self.assertEqual(restored.webhook_binding(1, 14)["webhook_id"], 900)
        self.assertEqual(len(restored.rows("PRAGMA table_info(bc_publications)")), 7)
        self.assertEqual(restored.one("PRAGMA user_version")["user_version"], 7)

    def test_upgrade_accepts_already_added_column_and_webhook_table(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript("""
                ALTER TABLE bc_publications ADD COLUMN webhook_id INTEGER;
                UPDATE bc_publications SET webhook_id=900 WHERE key='book:old-book';
                CREATE TABLE bc_webhooks(
                    guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, webhook_id INTEGER,
                    PRIMARY KEY(guild_id,channel_id));
                INSERT INTO bc_webhooks VALUES(1,14,900);
            """)
        store = Store(self.path)
        self.assertEqual(store.publication("book:old-book")["webhook_id"], 900)
        self.assertEqual(store.webhook_binding(1, 14)["webhook_id"], 900)
        self.assertEqual(store.rows("SELECT version FROM bc_migrations ORDER BY version"),
                         [{"version": 1}, {"version": 2}, {"version": 3}, {"version": 4}])


class WebhookStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / "club.sqlite3")

    def test_reservation_distinguishes_absence_from_unconfirmed_creation(self):
        self.assertIsNone(self.store.webhook_binding(1, 14))
        self.assertTrue(self.store.reserve_webhook(1, 14))
        self.assertEqual(self.store.webhook_binding(1, 14),
                         dict(guild_id=1, channel_id=14, webhook_id=None))
        self.assertFalse(self.store.reserve_webhook(1, 14))
        self.store.save_webhook(1, 14, 900)
        self.assertFalse(self.store.reserve_webhook(1, 14))
        self.assertEqual(self.store.webhook_binding(1, 14)["webhook_id"], 900)

        restored = Store(self.store.path)
        self.assertFalse(restored.reserve_webhook(1, 14))
        self.assertEqual(restored.webhook_binding(1, 14)["webhook_id"], 900)

    def test_bindings_and_forgetting_are_scoped_to_guild_and_channel(self):
        for guild_id, channel_id in ((1, 14), (1, 15), (2, 14)):
            self.assertTrue(self.store.reserve_webhook(guild_id, channel_id))
        self.store.save_webhook(1, 14, 900)
        self.store.save_webhook(1, 15, 901)
        self.store.save_webhook(2, 14, 902)
        self.store.forget_webhook(1, 14)
        self.assertIsNone(self.store.webhook_binding(1, 14))
        self.assertEqual(self.store.webhook_binding(1, 15)["webhook_id"], 901)
        self.assertEqual(self.store.webhook_binding(2, 14)["webhook_id"], 902)
        self.assertTrue(self.store.reserve_webhook(1, 14))
        self.store.forget_webhook(1, 14)
        self.store.forget_webhook(1, 14)

    def test_save_can_adopt_discovered_webhook_and_replace_missing_one(self):
        self.store.save_webhook(1, 14, 900)
        self.store.save_webhook(1, 14, 901)
        self.assertEqual(self.store.webhook_binding(1, 14)["webhook_id"], 901)
        self.assertEqual([row["name"] for row in self.store.rows("PRAGMA table_info(bc_webhooks)")],
                         ["guild_id", "channel_id", "webhook_id"])

    def test_publication_keeps_webhook_origin_before_and_after_acknowledgement(self):
        self.assertTrue(self.store.reserve_publication("essay:1", 1, 14, webhook_id=900))
        pending = self.store.publication("essay:1")
        self.assertEqual((pending["state"], pending["webhook_id"]), ("reserved", 900))
        self.assertFalse(self.store.reserve_publication("essay:1", 2, 15, webhook_id=901))
        self.assertEqual(self.store.publication("essay:1"), pending)
        self.store.save_publication("essay:1", 500, 500, "digest")
        published = self.store.publication("essay:1")
        self.assertEqual((published["state"], published["channel_id"], published["message_id"],
                          published["content_hash"], published["webhook_id"]),
                         ("ready", 500, 500, "digest", 900))
        self.assertFalse(self.store.reserve_publication("essay:1", 1, 14))
        self.store.save_publication("essay:1", 500, 500, "updated")
        self.assertEqual(self.store.publication("essay:1")["webhook_id"], 900)
        self.assertEqual(Store(self.store.path).publication("essay:1"), self.store.publication("essay:1"))

    def test_legacy_publications_have_no_webhook_origin(self):
        self.assertTrue(self.store.reserve_publication("book:1", 1, 13))
        self.assertIsNone(self.store.publication("book:1")["webhook_id"])
        self.store.save_publication("book:1", 400, 400)
        self.assertIsNone(self.store.publication("book:1")["webhook_id"])

    def test_webhook_setting_defaults_to_enabled_and_accepts_boolean_opt_out(self):
        self.store.configure(1, CONFIG)
        self.assertTrue(self.store.settings(1)["essay_webhooks"])
        self.store.configure(1, {**CONFIG, "essay_webhooks": False})
        self.assertFalse(self.store.settings(1)["essay_webhooks"])
        for invalid in (None, 0, 1, "false", []):
            with self.subTest(invalid=invalid), self.assertRaises(ClubError):
                self.store.configure(1, {**CONFIG, "essay_webhooks": invalid})
        self.assertFalse(self.store.settings(1)["essay_webhooks"])


if __name__ == "__main__":
    unittest.main()

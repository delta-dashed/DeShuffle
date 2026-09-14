"""Essay migration and draft lifecycle regressions against a real SQLite database."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from bookclub.store import ClubError, Store


CONFIG = dict(news=11, chat=12, books=13, essays=14, voice=15)


class EssayMigrationTests(unittest.TestCase):
    def test_v1_migration_preserves_existing_essays_and_legacy_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "club.sqlite3"
            with closing(sqlite3.connect(path)) as db, db:
                db.executescript("""
                    PRAGMA user_version=7;
                    CREATE TABLE voice_sessions(id INTEGER PRIMARY KEY, note TEXT);
                    INSERT INTO voice_sessions VALUES(1,'legacy');
                    CREATE TABLE bc_migrations(version INTEGER PRIMARY KEY);
                    INSERT INTO bc_migrations VALUES(1);
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
                        deleted INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(guild_id,source_id));
                    INSERT INTO bc_essays VALUES(1,100,100,'old-book',10,'Эссе','old-url',0);
                    INSERT INTO bc_essays VALUES(1,101,101,'old-book',11,'Удалено','deleted-url',1);
                """)

            migrated = Store(path)
            essay = migrated.essays("old-book")[0]
            self.assertEqual((essay["source_id"], essay["title"], essay["url"]), (100, "Эссе", "old-url"))
            self.assertEqual((essay["managed"], essay["submitted"]), (0, 1))
            self.assertEqual(migrated.one("SELECT deleted FROM bc_essays WHERE source_id=101")["deleted"], 1)
            self.assertEqual(migrated.one("SELECT note FROM voice_sessions")["note"], "legacy")
            self.assertEqual(migrated.one("PRAGMA user_version")["user_version"], 7)
            self.assertEqual(migrated.rows("SELECT version FROM bc_migrations ORDER BY version"),
                             [{"version": version} for version in range(1, 10)])

            migrated.register_essay(1, "old-book", 100, 100, 10, "Черновик", "new-url",
                                    managed=True, submitted=False)
            restored = Store(path)
            self.assertEqual(restored.essays("old-book"), [])
            self.assertEqual(restored.essays("old-book", submitted_only=False),
                             migrated.essays("old-book", submitted_only=False))
            self.assertEqual(restored.one("PRAGMA user_version")["user_version"], 7)
            self.assertEqual(len(restored.rows("PRAGMA table_info(bc_essays)")), 10)
            self.assertEqual(restored.rows("SELECT version FROM bc_migrations ORDER BY version"),
                             [{"version": version} for version in range(1, 10)])


class EssayStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / "club.sqlite3", clock=lambda: 2_000_000_000)
        self.store.configure(1, CONFIG)
        self.book = self.store.create_book(1, "Книга", "Автор", "", "book")["id"]
        for user_id in (1, 2):
            self.store.participant(1, self.book, user_id)

    def register(self, source_id=100, author_id=1, book_id=None, **flags):
        self.store.register_essay(1, book_id or self.book, source_id, source_id, author_id,
                                  "Эссе", f"https://discord.com/channels/1/{source_id}", **flags)

    def missing(self, book_id=None):
        return [row["user_id"] for row in self.store.missing_essays(book_id or self.book)]

    def test_draft_counts_only_after_submission(self):
        self.register(managed=True, submitted=False)
        self.assertEqual(self.missing(), [1, 2])
        self.assertEqual(self.store.essays(self.book), [])
        self.assertEqual(len(self.store.essays(self.book, submitted_only=False)), 1)

        self.register(submitted=True)
        self.assertEqual(self.missing(), [2])
        self.assertEqual(len(self.store.essays(self.book)), 1)
        self.assertEqual(self.store.essays(self.book)[0]["managed"], 1)

        self.register(submitted=False)
        self.assertEqual(self.missing(), [1, 2])

    def test_registration_preserves_flags_and_updates_channel_and_metadata(self):
        self.register(managed=True, submitted=False)
        self.store.register_essay(1, self.book, 100, 200, 1, "Исправленный заголовок", "new-url")
        essay = self.store.essays(self.book, submitted_only=False)[0]
        self.assertEqual((essay["managed"], essay["submitted"]), (1, 0))
        self.assertEqual((essay["channel_id"], essay["title"], essay["url"]),
                         (200, "Исправленный заголовок", "new-url"))

        self.register(managed=False)
        essay = self.store.essays(self.book, submitted_only=False)[0]
        self.assertEqual((essay["managed"], essay["submitted"]), (0, 0))
        self.register(submitted=True)
        self.register()
        essay = self.store.essays(self.book)[0]
        self.assertEqual((essay["managed"], essay["submitted"]), (0, 1))

    def test_new_manual_essay_defaults_to_submitted(self):
        self.register()
        essay = self.store.essays(self.book)[0]
        self.assertEqual((essay["managed"], essay["submitted"]), (0, 1))
        self.assertEqual(self.missing(), [2])

    def test_deletion_requires_another_submitted_essay_not_just_a_draft(self):
        self.register(100)
        self.register(101)
        self.register(102, managed=True, submitted=False)
        self.store.delete_essay(1, source_id=100)
        self.assertEqual(self.missing(), [2])
        self.assertEqual([row["source_id"] for row in self.store.essays(self.book)], [101])
        self.store.delete_essay(1, channel_id=101)
        self.assertEqual(self.missing(), [1, 2])
        self.assertEqual([row["source_id"] for row in self.store.essays(self.book, submitted_only=False)], [102])
        self.store.delete_essay(1, source_id=102)
        self.assertEqual(self.store.essays(self.book, submitted_only=False), [])
        self.register(102)
        self.assertEqual(self.missing(), [1, 2])
        self.assertEqual(self.store.essays(self.book, submitted_only=False)[0]["submitted"], 0)

    def test_active_essays_are_sorted_by_author_then_source(self):
        self.register(104, author_id=2)
        self.register(103)
        self.register(101)
        self.register(102, managed=True, submitted=False)
        self.assertEqual([row["source_id"] for row in self.store.essays(self.book)], [101, 103, 104])
        self.assertEqual([row["source_id"] for row in self.store.essays(self.book, submitted_only=False)],
                         [101, 102, 103, 104])

    def test_explicit_correction_moves_book_and_author_preserving_flags(self):
        second = self.store.create_book(1, "Вторая книга", "Автор", "", "second")["id"]
        self.store.participant(1, second, 2)
        self.register(managed=True, submitted=True)
        with self.assertRaises(ClubError):
            self.register(book_id=second)
        with self.assertRaises(ClubError):
            self.register(author_id=2)
        self.assertEqual(self.missing(), [2])

        self.store.register_essay(1, second, 100, 300, 2, "Исправленная связь", "new-url", correct=True)
        self.assertEqual(self.store.essays(self.book), [])
        self.assertEqual(self.missing(), [1, 2])
        self.assertEqual(self.missing(second), [])
        essay = self.store.essays(second)[0]
        self.assertEqual((essay["author_id"], essay["channel_id"], essay["managed"], essay["submitted"]),
                         (2, 300, 1, 1))


if __name__ == "__main__":
    unittest.main()

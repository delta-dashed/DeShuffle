"""Book list limits, atomic deduplication and durable Discord bindings."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest

from bookclub.book_list import MAX_BOOKS, MAX_BYTES, parse_book_list
from bookclub.store import ClubError, Store


class BookListTests(unittest.TestCase):
    def test_plain_lines_allow_optional_materials_and_keep_line_numbers(self):
        self.assertEqual(parse_book_list("\ufeff\n Дюна | Фрэнк Герберт\n\nСолярис | Станислав Лем | https://example.org\n"), [
            dict(title="Дюна", author="Фрэнк Герберт", materials=""),
            dict(title="Солярис", author="Станислав Лем", materials="https://example.org"),
        ])
        with self.assertRaisesRegex(ClubError, "Строка 3"):
            parse_book_list("\nДюна | Герберт\nнет разделителя")

    def test_json_and_csv_keep_quoted_delimiters_and_multiline_materials(self):
        books = [dict(title="Книга | со знаком", author="Имя, Фамилия", materials="строка 1\nстрока 2")]
        self.assertEqual(parse_book_list(json.dumps(books), "BOOKS.JSON"), books)
        self.assertEqual(parse_book_list('title,author,materials\nКнига | со знаком,"Имя, Фамилия","строка 1\nстрока 2"', "books.csv"), books)

    def test_csv_russian_headers_and_delimiters_are_detected(self):
        expected = [dict(title="Дюна", author="Герберт", materials="")]
        for delimiter in (",", ";", "\t"):
            with self.subTest(delimiter=delimiter):
                self.assertEqual(parse_book_list(f"Название{delimiter}Автор\nДюна{delimiter}Герберт"), expected)
        self.assertEqual(parse_book_list(b"\xef\xbb\xbftitle,author\nBook,Author", "books.csv"),
                         [dict(title="Book", author="Author", materials="")])

    def test_invalid_records_are_rejected_with_location(self):
        for value in ("Название | ", " | Автор", "Название | Автор | ссылка | лишнее"):
            with self.subTest(value=value), self.assertRaisesRegex(ClubError, "Строка 1"):
                parse_book_list(value)
        for value in ({"title": "Название"}, {"title": None, "author": "Автор"},
                      {"title": "Название", "author": "Автор", "unknown": "x"}):
            with self.subTest(value=value), self.assertRaisesRegex(ClubError, "Книга 1"):
                parse_book_list(json.dumps([value]))

    def test_csv_requires_valid_headers_and_matching_rows(self):
        for text in ("Book,Author", "title,title\nBook,Author", "title,author,unknown\nBook,Author,X"):
            with self.subTest(text=text), self.assertRaisesRegex(ClubError, "CSV"):
                parse_book_list(text, "books.csv")
        with self.assertRaisesRegex(ClubError, "Строка 2"):
            parse_book_list("title,author\nBook,Author,extra", "books.csv")
        with self.assertRaisesRegex(ClubError, "кавычки"):
            parse_book_list('title,author\n"Book,Author', "books.csv")

    def test_empty_or_nonlist_json_is_rejected(self):
        for text in ("", "\n", "[]", '{"title": "Book", "author": "Author"}'):
            with self.subTest(text=text), self.assertRaises(ClubError):
                parse_book_list(text)
        with self.assertRaisesRegex(ClubError, "Строка 2"):
            parse_book_list('[\ninvalid]', "books.json")

    def test_size_count_and_encoding_limits(self):
        self.assertEqual(len(parse_book_list("\n".join(f"Book {i} | Author" for i in range(MAX_BOOKS)))), MAX_BOOKS)
        with self.assertRaisesRegex(ClubError, "100"):
            parse_book_list("\n".join(f"Book {i} | Author" for i in range(MAX_BOOKS + 1)))
        for oversized in ("Я" * (MAX_BYTES // 2 + 1), b" " * (MAX_BYTES + 1)):
            with self.subTest(kind=type(oversized)), self.assertRaisesRegex(ClubError, "64"):
                parse_book_list(oversized)
        with self.assertRaisesRegex(ClubError, "UTF-8"):
            parse_book_list(b"\xffBook | Author", "books.txt")
        with self.assertRaisesRegex(ClubError, "TXT"):
            parse_book_list("Book | Author", "books.xlsx")

    def test_field_limits_are_shared_for_every_format(self):
        for key, limit in (("title", 180), ("author", 180), ("materials", 4000)):
            book = dict(title="Book", author="Author", materials="")
            book[key] = "x" * (limit + 1)
            with self.subTest(key=key), self.assertRaisesRegex(ClubError, f"{limit}"):
                parse_book_list(json.dumps([book]))


class CatalogStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "club.sqlite3"
        self.store = Store(self.path)
        self.store.configure(1, dict(news=11, chat=12, books=13, essays=14, voice=15))

    def test_upgrade_from_v5_preserves_existing_records_and_legacy_schema(self):
        book = self.store.create_book(1, "Book", "Author", "", "old")
        self.store.reserve_publication("book:old", 1, 13, webhook_id=123)
        self.store.save_publication("book:old", 130, 130, "old-digest")
        self.store.bind_setup_resource(1, "books", 13, state="ready")
        with self.store.tx() as db:
            db.execute("CREATE TABLE legacy(note TEXT)")
            db.execute("INSERT INTO legacy VALUES('keep')")
            db.execute("PRAGMA user_version=7")
            db.execute("DROP TABLE bc_forum_tags")
            db.execute("ALTER TABLE bc_publications DROP COLUMN managed_name")
            db.execute("ALTER TABLE bc_setup_resources DROP COLUMN name")
            db.execute("DELETE FROM bc_migrations WHERE version>=6")
        migrated = Store(self.path)
        self.assertEqual(migrated.book(1, book["id"]), book)
        self.assertEqual(migrated.one("SELECT note FROM legacy")["note"], "keep")
        self.assertEqual(migrated.one("PRAGMA user_version")["user_version"], 7)
        self.assertEqual(migrated.publication("book:old")["webhook_id"], 123)
        self.assertEqual(migrated.publication("book:old")["content_hash"], "old-digest")
        self.assertIsNone(migrated.publication("book:old")["managed_name"])
        self.assertEqual(migrated.setup_resource(1, "books")["channel_id"], 13)
        self.assertIsNone(migrated.setup_resource(1, "books")["name"])
        self.assertEqual(migrated.bindings(1, 13), [])
        self.assertEqual(Store(self.path).rows("SELECT version FROM bc_migrations ORDER BY version"),
                         [{"version": i} for i in range(1, 11)])

    def test_tag_bindings_survive_restart_and_scope_to_forum(self):
        self.store.bind_tag(1, 13, "catalog", 100)
        self.store.bind_tag(1, 13, "reading", 101)
        self.store.bind_tag(2, 13, "catalog", 100)
        self.store.bind_tag(1, 14, "catalog", 100)
        self.store.bind_tag(1, 13, "catalog", 102)
        restored = Store(self.path)
        self.assertEqual({row["purpose"]: row["tag_id"] for row in restored.bindings(1, 13)},
                         {"catalog": 102, "reading": 101})
        self.assertEqual(restored.bindings(2, 13)[0]["tag_id"], 100)
        self.assertEqual(restored.bindings(1, 14)[0]["tag_id"], 100)
        with self.assertRaises(sqlite3.IntegrityError):
            restored.bind_tag(1, 13, "read", 101)

    def test_remembering_names_does_not_change_content_or_book_revisions(self):
        book = self.store.create_book(1, "Book", "Author", "", "old")
        settings = self.store.settings(1)
        self.store.reserve_publication("book:old", 1, 13)
        self.store.save_publication("book:old", 130, 130, "digest")
        self.store.bind_setup_resource(1, "books", 13, state="ready")
        self.store.remember_publication_name("book:old", "Book · Author")
        self.store.remember_setup_name(1, "books", "Читальня")
        self.store.bind_setup_resource(1, "books", 13, state="ready")
        restored = Store(self.path)
        self.assertEqual(restored.publication("book:old")["managed_name"], "Book · Author")
        self.assertEqual(restored.publication("book:old")["content_hash"], "digest")
        self.assertEqual(restored.setup_resource(1, "books")["name"], "Читальня")
        self.assertEqual(restored.book(1, book["id"]), book)
        self.assertEqual(restored.settings(1), settings)

    def test_batch_deduplicates_existing_and_input_by_normalized_title_author(self):
        existing = self.store.create_book(1, "Пикник  на обочине", "Братья Стругацкие", "оригинал", "old")
        books = parse_book_list("пикник на ОБОЧИНЕ | БРАТЬЯ  Стругацкие | другой материал\n"
                                "Солярис | Лем\nСОЛЯРИС | ЛЕМ\nСолярис | Другой автор")
        created, skipped = self.store.create_books(1, books, "batch")
        self.assertEqual(skipped, 2)
        self.assertEqual([book["author"] for book in created], ["Лем", "Другой автор"])
        self.assertEqual([book["position"] for book in created], [2, 3])
        self.assertEqual(self.store.book(1, existing["id"])["materials"], "оригинал")
        self.assertEqual(self.store.create_books(1, books, "batch"), ([], 4))
        self.assertEqual(len(self.store.books(1)), 3)

    def test_invalid_batch_never_adds_valid_prefix(self):
        with self.assertRaises(ClubError):
            self.store.create_books(1, [dict(title="Good", author="Author"), dict(title="Bad", author="")], "batch")
        self.assertEqual(self.store.books(1), [])

    def test_database_failure_rolls_back_entire_batch(self):
        with self.store.tx() as db:
            db.execute("""CREATE TRIGGER reject_bad_book BEFORE INSERT ON bc_books WHEN NEW.title='Bad'
              BEGIN SELECT RAISE(ABORT, 'fixture failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.create_books(1, parse_book_list("Good | Author\nBad | Author"), "batch")
        self.assertEqual(self.store.books(1), [])

    def test_concurrent_batches_do_not_duplicate_same_book(self):
        books = parse_book_list("Book | Author")
        def create(index):
            return Store(self.path).create_books(1, books, f"batch{index}")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create, range(2)))
        self.assertEqual(sum(len(created) for created, skipped in results), 1)
        self.assertEqual(sum(skipped for created, skipped in results), 1)
        self.assertEqual(len(self.store.books(1)), 1)

    def test_retry_does_not_recreate_book_after_manual_title_change(self):
        books = parse_book_list("Book | Author")
        created, _ = self.store.create_books(1, books, "batch")
        self.store.update_book(1, created[0]["id"], title="Renamed")
        self.assertEqual(Store(self.path).create_books(1, books, "batch"), ([], 1))
        self.assertEqual(len(self.store.books(1)), 1)


if __name__ == "__main__":
    unittest.main()

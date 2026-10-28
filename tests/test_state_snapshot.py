"""Deployment checks inspect a real SQLite file without touching its contents."""
from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from ops import state_snapshot


class StateSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "club.sqlite3"
        self.execute("""
            PRAGMA user_version=42;
            CREATE TABLE bc_migrations(version INTEGER PRIMARY KEY);
            INSERT INTO bc_migrations VALUES(13);
            CREATE TABLE bc_books(
                id TEXT PRIMARY KEY, title TEXT, author TEXT, status TEXT,
                position INTEGER, materials TEXT, reading_meetings INTEGER,
                deleted INTEGER DEFAULT 0, deadline INTEGER, revision INTEGER DEFAULT 0);
            INSERT INTO bc_books VALUES('book-a','Private book','Private author',
                'reading',1,'https://example.org/private',3,0,NULL,0);
            INSERT INTO bc_books VALUES('book-b','Second','Another','queued',2,'',4,0,NULL,0);
            CREATE TABLE bc_essays(guild_id INTEGER, source_id INTEGER,
                book_id TEXT, author_id INTEGER, deleted INTEGER,
                PRIMARY KEY(guild_id,source_id));
            INSERT INTO bc_essays VALUES(1,123,'book-a',456,0);
            CREATE TABLE bc_import_budgets(budget_id TEXT PRIMARY KEY,
                runs_used INTEGER, tokens_reserved INTEGER);
            INSERT INTO bc_import_budgets VALUES('private-budget',1,30714);
            CREATE TABLE bc_import_runs(id TEXT PRIMARY KEY, state TEXT,
                snapshot TEXT, plan TEXT, updated_at INTEGER);
            INSERT INTO bc_import_runs VALUES('private-run','done',
                'PRIVATE ESSAY BODY','PRIVATE APPROVED PLAN',123);
            CREATE TABLE bc_import_sources(guild_id INTEGER,source_id INTEGER,
                run_id TEXT,item_key TEXT,thread_id INTEGER,
                PRIMARY KEY(guild_id,source_id));
            INSERT INTO bc_import_sources VALUES(1,123,'private-run',
                'essay-import:1:123',789);
            CREATE TABLE bc_publications(key TEXT PRIMARY KEY,guild_id INTEGER,
                channel_id INTEGER,message_id INTEGER,state TEXT,content_hash TEXT,
                webhook_id INTEGER,managed_name TEXT);
            INSERT INTO bc_publications VALUES('book:book-a',1,11,12,'ready','old',NULL,'Old title');
            INSERT INTO bc_publications VALUES('essay-import:1:123',1,21,22,'ready','old',23,NULL);
            INSERT INTO bc_publications VALUES('essay-import:1:123:message:123:1:v2',1,21,24,'ready','old',23,NULL);
            INSERT INTO bc_publications VALUES('essay-space:book-a:456',1,31,32,'ready','old',33,NULL);
            INSERT INTO bc_publications VALUES('catalog:1:page:2',1,41,42,'ready','old',NULL,NULL);
            INSERT INTO bc_publications VALUES('book:book-a:page:2',1,11,13,'ready','old',NULL,NULL);
            CREATE TABLE bc_jobs(key TEXT PRIMARY KEY,state TEXT);
            INSERT INTO bc_jobs VALUES('volatile','pending');
        """)

    def execute(self, sql, parameters=None):
        with closing(sqlite3.connect(self.path)) as db, db:
            if parameters is None:
                db.executescript(sql)
            else:
                db.execute(sql, parameters)

    def snapshot(self):
        return state_snapshot.capture(self.path)

    def test_missing_database_is_explicit_and_never_created(self):
        path = Path(self.tmp.name) / "does-not-exist.sqlite3"
        self.assertEqual(state_snapshot.capture(path), {
            "format": 1, "database_exists": False, "schema": None, "active_imports": 0, "tables": {},
        })
        self.assertFalse(path.exists())
        self.assertEqual(state_snapshot.compare(state_snapshot.capture(path), self.snapshot()), [])

    def test_existing_legacy_database_has_no_club_invariants(self):
        path = Path(self.tmp.name) / "legacy.sqlite3"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE voice_activity(user_id INTEGER)")
        snapshot = state_snapshot.capture(path)
        self.assertTrue(snapshot["database_exists"])
        self.assertEqual(snapshot["schema"], 0)
        self.assertEqual(snapshot["tables"], {})

    def test_read_only_capture_preserves_file_and_legacy_user_version(self):
        original = self.path.read_bytes()
        before = self.snapshot()
        self.assertEqual(before, self.snapshot())
        self.assertEqual(before["schema"], 13)
        self.assertEqual(self.path.read_bytes(), original)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 42)

    def test_failed_structural_check_is_rejected_without_sqlite_page_contents(self):
        connect = sqlite3.connect
        results = [("PRIVATE CORRUPTED PAGE CONTENT",)]

        class FailedCheckConnection(sqlite3.Connection):
            def execute(self, sql, *args):
                if sql == "PRAGMA quick_check":
                    return results
                return super().execute(sql, *args)

        def checked_connect(*args, **kwargs):
            return connect(*args, factory=FailedCheckConnection, **kwargs)

        for response in ([('PRIVATE CORRUPTED PAGE CONTENT',)], [('ok',), ('extra',)], []):
            with self.subTest(response=response):
                results = response
                with patch.object(state_snapshot.sqlite3, "connect", side_effect=checked_connect):
                    with self.assertRaisesRegex(state_snapshot.SnapshotError, "structural check failed") as error:
                        self.snapshot()
                self.assertNotIn("PRIVATE CORRUPTED PAGE CONTENT", str(error.exception))

    def test_snapshot_contains_no_titles_import_payloads_or_budget_names(self):
        snapshot = self.snapshot()
        text = json.dumps(snapshot)
        for private in ("Private book", "Private author", "PRIVATE ESSAY BODY",
                        "PRIVATE APPROVED PLAN", "private-budget", "private-run",
                        "https://example.org/private", "book:book-a", "essay-import:1:123"):
            self.assertNotIn(private, text)
        json.loads(text)

    def test_detects_each_critical_book_field_and_essay_binding(self):
        fields = {"title": "Changed", "author": "Changed", "status": "read",
                  "position": 999, "materials": "PRIVATE NEW MATERIALS", "reading_meetings": 5, "deleted": 1}
        for column, value in fields.items():
            with self.subTest(column=column):
                before = self.snapshot()
                with closing(sqlite3.connect(self.path)) as db:
                    old = db.execute('SELECT "' + column + '" FROM bc_books WHERE id=?', ("book-a",)).fetchone()[0]
                self.execute('UPDATE bc_books SET "' + column + '"=? WHERE id=?', (value, "book-a"))
                issues = state_snapshot.compare(before, self.snapshot())
                self.assertIn("Protected data changed: bc_books." + column, issues)
                self.assertNotIn(str(value), json.dumps(issues))
                self.execute('UPDATE bc_books SET "' + column + '"=? WHERE id=?', (old, "book-a"))
        before = self.snapshot()
        self.execute("UPDATE bc_essays SET book_id='book-b'")
        self.assertIn("Protected data changed: bc_essays.book_id", state_snapshot.compare(before, self.snapshot()))

    def test_identity_is_bound_to_values_when_rows_exchange_positions(self):
        before = self.snapshot()
        self.execute("UPDATE bc_books SET position=3-position")
        self.assertIn("Protected data changed: bc_books.position", state_snapshot.compare(before, self.snapshot()))

    def test_meeting_boundaries_and_book_links_survive_event_cache_refresh(self):
        self.execute("""
            CREATE TABLE bc_meetings(id TEXT PRIMARY KEY,book_id TEXT,event_id INTEGER,
                part TEXT,chapter TEXT,plan_kind TEXT,status TEXT,start INTEGER,
                host_id INTEGER,revision INTEGER);
            INSERT INTO bc_meetings VALUES('meeting','book-a',987,'Part 1','Chapters 1-3',
                'reading','draft',123,NULL,0);
        """)
        before = self.snapshot()
        self.execute("UPDATE bc_meetings SET status='active',start=456,host_id=99,revision=1")
        self.assertEqual(state_snapshot.compare(before, self.snapshot()), [])
        self.execute("UPDATE bc_meetings SET chapter='PRIVATE NEW BOUNDARY',book_id='book-b'")
        issues = state_snapshot.compare(before, self.snapshot())
        self.assertIn("Protected data changed: bc_meetings.chapter", issues)
        self.assertIn("Protected data changed: bc_meetings.book_id", issues)
        self.assertNotIn("PRIVATE NEW BOUNDARY", json.dumps(issues))

    def test_import_budget_completed_run_and_source_bindings_are_protected(self):
        cases = (("bc_import_budgets", "runs_used", 0),
                 ("bc_import_budgets", "tokens_reserved", 0),
                 ("bc_import_runs", "state", "review"),
                 ("bc_import_runs", "plan", "changed private plan"),
                 ("bc_import_sources", "thread_id", 999))
        for table, column, value in cases:
            with self.subTest(table=table, column=column):
                before = self.snapshot()
                self.execute('UPDATE "' + table + '" SET "' + column + '"=?', (value,))
                issues = state_snapshot.compare(before, self.snapshot())
                self.assertIn("Protected data changed: " + table + "." + column, issues)

    def test_active_import_count_exposes_no_run_details_or_payloads(self):
        self.assertEqual(self.snapshot()["active_imports"], 0)
        for state in ("running", "applying"):
            with self.subTest(state=state):
                self.execute("UPDATE bc_import_runs SET state=?", (state,))
                self.assertEqual(self.snapshot()["active_imports"], 1)
        self.execute("""
            INSERT INTO bc_import_runs VALUES('second-private-run','running','SECRET',NULL,0);
        """)
        snapshot = self.snapshot()
        self.assertEqual(snapshot["active_imports"], 2)
        self.assertNotIn("second-private-run", json.dumps(snapshot))
        self.assertNotIn("SECRET", json.dumps(snapshot))
        for state in ("done", "failed", "review", "unknown"):
            with self.subTest(state=state):
                self.execute("UPDATE bc_import_runs SET state=?", (state,))
                self.assertEqual(self.snapshot()["active_imports"], 0)

    def test_only_existing_ready_book_and_essay_publication_ids_are_protected(self):
        before = self.snapshot()
        self.assertEqual(before["tables"]["bc_publications"]["count"], 4)
        self.execute("""
            DELETE FROM bc_publications WHERE key IN ('catalog:1:page:2','book:book-a:page:2');
            UPDATE bc_publications SET content_hash='new', managed_name='New title';
            INSERT INTO bc_publications VALUES('book:book-b',1,51,52,'ready','new',NULL,NULL);
        """)
        self.assertEqual(state_snapshot.compare(before, self.snapshot()), [])
        for key in ("book:book-a", "essay-import:1:123", "essay-import:1:123:message:123:1:v2",
                    "essay-space:book-a:456"):
            with self.subTest(key=key):
                current = self.snapshot()
                self.execute("UPDATE bc_publications SET message_id=message_id+1 WHERE key=?", (key,))
                issues = state_snapshot.compare(current, self.snapshot())
                self.assertEqual(issues, ["Existing book/essay publication mappings changed: 0 missing, 1 modified."])
                self.assertNotIn(key, json.dumps(issues))
        current = self.snapshot()
        self.execute("DELETE FROM bc_publications WHERE key='book:book-a'")
        self.assertEqual(state_snapshot.compare(current, self.snapshot()), [
            "Existing book/essay publication mappings changed: 1 missing, 0 modified."])

    def test_schema_growth_and_runtime_refresh_are_allowed(self):
        before = self.snapshot()
        self.execute("""
            ALTER TABLE bc_books ADD COLUMN future_metadata TEXT DEFAULT 'new';
            CREATE TABLE bc_future_feature(id TEXT PRIMARY KEY);
            INSERT INTO bc_migrations VALUES(14);
            UPDATE bc_books SET revision=revision+1,deadline=999;
            UPDATE bc_jobs SET state='sent';
            UPDATE bc_import_runs SET updated_at=999;
        """)
        self.assertEqual(state_snapshot.compare(before, self.snapshot()), [])

    def test_deletions_and_schema_regression_are_reported(self):
        before = self.snapshot()
        self.execute("DELETE FROM bc_books WHERE id='book-b'; DELETE FROM bc_migrations;")
        issues = state_snapshot.compare(before, self.snapshot())
        self.assertIn("Book-club schema version decreased.", issues)
        self.assertIn("Protected row count changed: bc_books", issues)
        self.execute("DROP TABLE bc_import_sources")
        self.assertIn("Protected table disappeared: bc_import_sources", state_snapshot.compare(before, self.snapshot()))
        missing = {"format": 1, "database_exists": False, "schema": None, "tables": {}}
        self.assertEqual(state_snapshot.compare(before, missing), ["Database disappeared after deployment."])

    def test_dropped_column_is_detected_even_when_rows_remain(self):
        before = self.snapshot()
        self.execute("ALTER TABLE bc_books DROP COLUMN materials")
        self.assertIn("Protected column disappeared: bc_books.materials",
                      state_snapshot.compare(before, self.snapshot()))

    def test_single_read_transaction_sees_consistent_wal_state(self):
        writer = sqlite3.connect(self.path)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        expected = self.snapshot()
        original = state_snapshot._capture_table
        changed = False

        def concurrent_capture(db, table, excluded):
            nonlocal changed
            result = original(db, table, excluded)
            if not changed:
                changed = True
                writer.execute("UPDATE bc_import_budgets SET tokens_reserved=99")
                writer.commit()
            return result

        with patch.object(state_snapshot, "_capture_table", side_effect=concurrent_capture):
            snapshot = self.snapshot()
        self.assertTrue(changed)
        self.assertEqual(snapshot, expected)
        self.assertIn("Protected data changed: bc_import_budgets.tokens_reserved",
                      state_snapshot.compare(snapshot, self.snapshot()))

    def test_cli_has_safe_errors_and_comparison_exit_codes(self):
        snapshot = self.snapshot()
        before_path, after_path = Path(self.tmp.name) / "before.json", Path(self.tmp.name) / "after.json"
        before_path.write_text(json.dumps(snapshot), encoding="utf-8")
        after_path.write_text(json.dumps(snapshot), encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(state_snapshot.main(["compare", str(before_path), str(after_path)]), 0)
        self.assertEqual(json.loads(output.getvalue()), [])
        self.execute("UPDATE bc_books SET status='read' WHERE id='book-a'")
        after_path.write_text(json.dumps(self.snapshot()), encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(state_snapshot.main(["compare", str(before_path), str(after_path)]), 1)
        self.path.write_text("PRIVATE CORRUPT DATABASE", encoding="utf-8")
        errors = io.StringIO()
        with redirect_stderr(errors), redirect_stdout(io.StringIO()):
            self.assertEqual(state_snapshot.main(["capture", str(self.path)]), 2)
        self.assertNotIn("PRIVATE CORRUPT DATABASE", errors.getvalue())
        self.assertNotIn(str(self.path), errors.getvalue())

    def test_current_real_store_schema_is_supported(self):
        from bookclub.store import Store
        path = Path(self.tmp.name) / "real.sqlite3"
        store = Store(path, clock=lambda: 1234)
        store.configure(1, dict(news=11, chat=12, books=13, essays=14, voice=15,
                                organizers=[99], organizer_roles=[22]))
        store.create_book(1, "A private title", "An author", "", "request")
        snapshot = state_snapshot.capture(path)
        self.assertEqual(snapshot["schema"], 13)
        self.assertEqual(snapshot["tables"]["bc_books"]["count"], 1)
        self.assertEqual(state_snapshot.compare(snapshot, state_snapshot.capture(path)), [])


if __name__ == "__main__":
    unittest.main()

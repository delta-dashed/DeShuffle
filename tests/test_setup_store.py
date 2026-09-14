"""Setup reservations and external configuration reconciliation in SQLite."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from bookclub.store import ClubError, DEFAULTS, Store


CONFIG = dict(news=11, chat=12, books=13, essays=14, voice=15)


class StoreFixture:
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "club.sqlite3"
        self.store = Store(self.path, clock=lambda: 2_000_000_000)


class SetupStoreTests(StoreFixture, unittest.TestCase):
    def test_reservation_persists_token_through_binding_ready_and_restart(self):
        self.assertIsNone(self.store.setup_resource(1, "books"))
        self.assertTrue(self.store.reserve_setup_resource(1, "books"))
        reservation = self.store.setup_resource(1, "books")
        self.assertEqual(len(reservation["token"]), 32)
        self.assertEqual((reservation["state"], reservation["channel_id"], reservation["managed"]),
                         ("reserved", None, 1))
        self.assertFalse(self.store.reserve_setup_resource(1, "books"))
        self.assertEqual(self.store.setup_resource(1, "books"), reservation)

        self.store.bind_setup_resource(1, "books", 13)
        self.assertEqual(self.store.setup_resource(1, "books"),
                         {**reservation, "channel_id": 13, "state": "created"})
        self.store.ready_setup_resource(1, "books")
        restored = Store(self.path)
        self.assertEqual(restored.setup_resource(1, "books"),
                         {**reservation, "channel_id": 13, "state": "ready"})
        self.assertFalse(restored.reserve_setup_resource(1, "books"))

    def test_concurrent_reservation_has_one_winner(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.store.reserve_setup_resource(1, "essays"), range(8)))
        self.assertEqual(results.count(True), 1)
        self.assertEqual(len(self.store.setup_resources(1)), 1)

    def test_binding_can_adopt_existing_channel_and_preserves_ownership(self):
        self.store.bind_setup_resource(1, "chat", 12, managed=False, state="ready")
        binding = self.store.setup_resource(1, "chat")
        self.assertEqual((binding["managed"], binding["state"], binding["channel_id"]), (0, "ready", 12))
        self.store.bind_setup_resource(1, "chat", 120)
        self.assertEqual(self.store.setup_resource(1, "chat"),
                         {**binding, "channel_id": 120, "managed": 1, "state": "created"})

    def test_guilds_and_purposes_are_isolated_and_forgetting_is_repeatable(self):
        self.store.bind_setup_resource(1, "category", 10)
        self.store.bind_setup_resource(1, "voice", 15)
        self.store.bind_setup_resource(2, "category", 20)
        self.assertEqual([r["purpose"] for r in self.store.setup_resources(1)], ["category", "voice"])
        old_token = self.store.setup_resource(1, "category")["token"]
        self.store.forget_setup_resource(1, "category")
        self.store.forget_setup_resource(1, "category")
        self.assertIsNone(self.store.setup_resource(1, "category"))
        self.assertEqual(self.store.setup_resource(2, "category")["channel_id"], 20)
        self.assertEqual(self.store.setup_resource(1, "voice")["channel_id"], 15)
        self.assertTrue(self.store.reserve_setup_resource(1, "category"))
        self.assertNotEqual(self.store.setup_resource(1, "category")["token"], old_token)

    def test_v3_upgrade_preserves_legacy_and_club_state(self):
        self.store.configure(1, CONFIG)
        book = self.store.create_book(1, "Книга", "Автор", "", "book")
        self.store.reserve_publication("book:1", 1, 13, webhook_id=90)
        self.store.save_publication("book:1", 13, 50, "content")
        self.store.save_webhook(1, 14, 90)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript("""
                DROP TABLE bc_setup_resources;
                DROP TABLE bc_config_imports;
                DELETE FROM bc_migrations WHERE version>=4;
                PRAGMA user_version=7;
                CREATE TABLE legacy_data(note TEXT);
                INSERT INTO legacy_data VALUES('preserved');
            """)
        upgraded = Store(self.path)
        self.assertEqual(upgraded.book(1, book["id"]), book)
        self.assertEqual(upgraded.settings(1), self.store.settings(1))
        self.assertEqual(upgraded.publication("book:1")["webhook_id"], 90)
        self.assertEqual(upgraded.webhook_binding(1, 14)["webhook_id"], 90)
        self.assertEqual(upgraded.one("SELECT note FROM legacy_data")["note"], "preserved")
        self.assertEqual(upgraded.one("PRAGMA user_version")["user_version"], 7)
        self.assertEqual(upgraded.setup_resources(1), [])
        upgraded.bind_setup_resource(1, "news", 11)
        upgraded.import_config(1, CONFIG)
        restored = Store(self.path)
        self.assertEqual(restored.setup_resource(1, "news")["channel_id"], 11)
        self.assertEqual(restored.rows("SELECT version FROM bc_migrations ORDER BY version"),
                         [{"version": i} for i in range(1, 7)])


class ConfigImportTests(StoreFixture, unittest.TestCase):
    def snapshot(self, guild_id=1):
        row = self.store.one("SELECT data FROM bc_config_imports WHERE guild_id=?", (guild_id,))
        return json.loads(row["data"]) if row else None

    def test_initial_import_saves_only_external_values_and_is_idempotent(self):
        external = {**CONFIG, "participant_minutes": 45}
        self.store.import_config(1, external)
        self.assertEqual(self.snapshot(), external)
        self.assertNotIn("scan_after", self.snapshot())
        book = self.store.create_book(1, "Книга", "Автор", "", "book")
        self.store.set_published(1)
        settings = self.store.settings(1)
        self.store.import_config(1, external)
        self.assertEqual(self.store.settings(1), settings)
        self.assertEqual(self.store.book(1, book["id"])["revision"], 0)

    def test_unchanged_file_preserves_repaired_channels_across_restart(self):
        self.store.import_config(1, CONFIG)
        self.store.configure(1, {**self.store.settings(1), "books": 130, "essays": 140})
        self.store = Store(self.path)
        self.store.import_config(1, CONFIG)
        self.assertEqual((self.store.settings(1)["books"], self.store.settings(1)["essays"]), (130, 140))
        self.assertEqual(self.snapshot(), CONFIG)

    def test_interval_file_change_preserves_runtime_channels_and_updates_deadlines(self):
        self.store.import_config(1, CONFIG)
        book = self.store.create_book(1, "Книга", "Автор", "", "book")
        self.store.update_book(1, book["id"], deadline=self.store.clock() + 86400 * 5)
        self.store.configure(1, {**self.store.settings(1), "essays": 140})
        self.store.import_config(1, {**CONFIG, "essay_hours": 48})
        self.assertEqual(self.store.settings(1)["essays"], 140)
        self.assertEqual(self.store.settings(1)["essay_hours"], 48)
        job = self.store.one("SELECT due FROM bc_jobs WHERE entity_id=? AND kind='essay' AND state='pending'",
                             (book["id"],))
        self.assertEqual(job["due"], self.store.clock() + 86400 * 3)

    def test_deliberate_file_channel_change_wins_over_runtime_repair(self):
        self.store.import_config(1, CONFIG)
        self.store.configure(1, {**self.store.settings(1), "essays": 140})
        self.store.import_config(1, {**CONFIG, "essays": 141})
        self.assertEqual(self.store.settings(1)["essays"], 141)
        self.assertEqual(self.snapshot()["essays"], 141)

    def test_changed_file_forgets_superseded_partial_setup_binding(self):
        self.store.import_config(1, CONFIG)
        self.store.bind_setup_resource(1, "books", 130)
        self.store.bind_setup_resource(1, "essays", 140)
        self.store.bind_setup_resource(1, "category", 100)
        self.store.bind_setup_resource(2, "books", 230)
        self.store.import_config(1, {**CONFIG, "books": 131})
        self.assertEqual(self.store.settings(1)["books"], 131)
        self.assertIsNone(self.store.setup_resource(1, "books"))
        self.assertEqual(self.store.setup_resource(1, "essays")["channel_id"], 140)
        self.assertEqual(self.store.setup_resource(1, "category")["channel_id"], 100)
        self.assertEqual(self.store.setup_resource(2, "books")["channel_id"], 230)

    def test_unchanged_channels_preserve_partial_setup_bindings(self):
        self.store.import_config(1, CONFIG)
        self.store.bind_setup_resource(1, "books", 130)
        binding = self.store.setup_resource(1, "books")
        self.store.import_config(1, CONFIG)
        self.assertEqual(self.store.setup_resource(1, "books"), binding)
        self.store.import_config(1, {**CONFIG, "essay_hours": 48})
        self.assertEqual(self.store.setup_resource(1, "books"), binding)
        self.store.configure(1, {**self.store.settings(1), "books": 130})
        self.assertEqual(self.store.setup_resource(1, "books"), binding)

    def test_initial_import_forgets_only_bindings_with_different_channel_ids(self):
        self.store.bind_setup_resource(1, "books", 130)
        self.store.bind_setup_resource(1, "essays", 14)
        matching = self.store.setup_resource(1, "essays")
        self.store.import_config(1, CONFIG)
        self.assertIsNone(self.store.setup_resource(1, "books"))
        self.assertEqual(self.store.setup_resource(1, "essays"), matching)
        self.assertEqual(self.store.settings(1)["books"], 13)

    def test_failed_import_preserves_superseded_bindings_until_success(self):
        self.store.import_config(1, CONFIG)
        self.store.bind_setup_resource(1, "books", 130)
        binding = self.store.setup_resource(1, "books")
        with self.assertRaises(ClubError):
            self.store.import_config(1, {**CONFIG, "books": 131, "essay_hours": -1})
        self.assertEqual(self.store.setup_resource(1, "books"), binding)
        self.assertEqual(self.snapshot(), CONFIG)
        self.assertEqual(self.store.settings(1)["books"], 13)

        self.store.bind_setup_resource(2, "books", 230)
        first_binding = self.store.setup_resource(2, "books")
        with self.assertRaises(ClubError):
            self.store.import_config(2, {**CONFIG, "essay_hours": -1})
        self.assertEqual(self.store.setup_resource(2, "books"), first_binding)
        self.assertIsNone(self.snapshot(2))

    def test_removing_optional_values_resets_defaults_without_reverting_channels(self):
        self.store.import_config(1, {**CONFIG, "essay_hours": 48, "organizers": [99], "essay_webhooks": False})
        self.store.configure(1, {**self.store.settings(1), "books": 130})
        self.store.import_config(1, CONFIG)
        settings = self.store.settings(1)
        for key in ("essay_hours", "organizers", "essay_webhooks"):
            self.assertEqual(settings[key], DEFAULTS[key])
        self.assertEqual(settings["books"], 130)

    def test_absent_file_does_not_change_saved_settings_or_create_import(self):
        self.store.configure(1, CONFIG)
        settings = self.store.settings(1)
        self.store.import_config(1, None)
        self.assertEqual(self.store.settings(1), settings)
        self.assertIsNone(self.snapshot())
        self.store.import_config(1, CONFIG)
        self.store.import_config(1, None)
        self.assertEqual(self.snapshot(), CONFIG)

    def test_failed_import_keeps_snapshot_and_runtime_settings(self):
        self.store.import_config(1, CONFIG)
        self.store.configure(1, {**self.store.settings(1), "books": 130})
        settings = self.store.settings(1)
        for invalid in ({**CONFIG, "essay_hours": -1}, {k: v for k, v in CONFIG.items() if k != "voice"}):
            with self.subTest(invalid=invalid), self.assertRaises(ClubError):
                self.store.import_config(1, invalid)
            self.assertEqual(self.snapshot(), CONFIG)
            self.assertEqual(self.store.settings(1), settings)
        self.store.import_config(1, {**CONFIG, "essay_hours": 48})
        self.assertEqual(self.store.settings(1)["books"], 130)

    def test_failed_first_import_does_not_record_snapshot(self):
        with self.assertRaises(ClubError):
            self.store.import_config(1, {"news": 11})
        self.assertIsNone(self.snapshot())
        self.assertIsNone(self.store.one("SELECT data FROM bc_settings WHERE guild_id=1"))

    def test_type_changes_are_validated_even_when_python_values_compare_equal(self):
        external = {**CONFIG, "essay_webhooks": False, "organizers": [1]}
        self.store.import_config(1, external)
        for invalid in ({**external, "essay_webhooks": 0}, {**external, "organizers": [True]}):
            with self.subTest(invalid=invalid), self.assertRaises(ClubError):
                self.store.import_config(1, invalid)
            self.assertEqual(self.snapshot(), external)

    def test_import_snapshots_are_guild_scoped(self):
        self.store.import_config(1, CONFIG)
        self.store.import_config(2, {**CONFIG, "books": 230})
        self.store.configure(1, {**self.store.settings(1), "books": 130})
        self.store.import_config(1, CONFIG)
        self.store.import_config(2, {**CONFIG, "books": 231})
        self.assertEqual(self.store.settings(1)["books"], 130)
        self.assertEqual(self.store.settings(2)["books"], 231)
        self.assertEqual(self.snapshot(1)["books"], 13)
        self.assertEqual(self.snapshot(2)["books"], 231)


if __name__ == "__main__":
    unittest.main()

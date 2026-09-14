"""Durable budget limits, interrupted import recovery and source deduplication."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from bookclub.import_store import ImportStore
from bookclub.store import ClubError, Store


class ImportStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "club.sqlite3"
        self.store = Store(self.path, clock=lambda: 2_000_000_000)
        self.imports = ImportStore(self.store)

    def reserve(self, **changes):
        args = dict(guild_id=1, actor_id=10, source_channel_id=20,
                    budget_id="migration-2026", request_key="request-1",
                    max_runs=3, reserve_tokens=100, max_tokens=300,
                    snapshot={"messages": [{"id": 100, "content": "Эссе"}]})
        args.update(changes)
        return self.imports.reserve_run(**args)

    def applying(self, **changes):
        run = self.reserve(**changes)
        self.imports.save_plan(run["guild_id"], run["id"], {"items": []}, usage_tokens=50)
        self.assertTrue(self.imports.claim_apply(run["guild_id"], run["id"]))
        return run

    def test_reservation_debits_before_result_and_returns_decoded_snapshot(self):
        run = self.reserve()
        self.assertEqual(run["state"], "running")
        self.assertEqual(run["reserved_tokens"], 100)
        self.assertEqual(run["snapshot"]["messages"][0]["content"], "Эссе")
        self.assertIsNone(run["plan"])
        self.assertEqual(len(run["id"]), 32)
        self.assertEqual(self.imports.budget("migration-2026"),
                         dict(budget_id="migration-2026", runs_used=1, tokens_reserved=100))
        self.assertEqual(self.imports.budget("not-created"),
                         dict(budget_id="not-created", runs_used=0, tokens_reserved=0))
        self.assertEqual(ImportStore(Store(self.path)).run(1, run["id"]), run)

    def test_duplicate_request_key_returns_original_without_debit_or_payload_replacement(self):
        first = self.reserve()
        duplicate = self.reserve(snapshot={"messages": []}, budget_id="another-budget")
        self.assertEqual(duplicate, first)
        self.assertEqual(self.imports.budget("migration-2026")["runs_used"], 1)
        self.assertEqual(self.imports.budget("another-budget")["runs_used"], 0)
        self.imports.fail_run(1, first["id"], "Нет результата.")
        self.assertEqual(self.reserve()["state"], "failed")
        self.assertEqual(self.imports.budget("migration-2026")["runs_used"], 1)

    def test_runs_limit_is_shared_across_guilds_and_does_not_reset_after_restart(self):
        first = self.reserve(max_runs=1)
        self.imports.fail_run(1, first["id"], "Нет результата.")
        self.imports = ImportStore(Store(self.path))
        with self.assertRaisesRegex(ClubError, "Лимит запусков"):
            self.reserve(guild_id=2, max_runs=1)
        self.assertEqual(len(self.store.rows("SELECT id FROM bc_import_runs")), 1)

    def test_reserved_token_limit_survives_failure_and_unknown_usage(self):
        first = self.reserve(max_tokens=199)
        self.imports.fail_run(1, first["id"], "Результат неизвестен.", state="unknown")
        with self.assertRaisesRegex(ClubError, "Лимит токенов"):
            self.reserve(request_key="second", max_tokens=199)
        self.assertEqual(self.imports.budget("migration-2026")["tokens_reserved"], 100)

    def test_actual_usage_never_refunds_reservation_and_excess_is_charged(self):
        first = self.reserve()
        self.imports.save_plan(1, first["id"], {"items": []}, usage_tokens=25)
        self.assertEqual(self.imports.budget("migration-2026")["tokens_reserved"], 100)
        second = self.reserve(request_key="second")
        self.imports.save_plan(1, second["id"], {"items": []}, usage_tokens=201)
        self.assertEqual(self.imports.budget("migration-2026")["tokens_reserved"], 301)
        self.assertEqual(self.imports.run(1, second["id"])["usage_tokens"], 201)
        with self.assertRaisesRegex(ClubError, "Лимит токенов"):
            self.reserve(request_key="third")

    def test_unreported_usage_keeps_full_reservation(self):
        run = self.reserve()
        self.imports.save_plan(1, run["id"], {"items": []})
        self.assertIsNone(self.imports.run(1, run["id"])["usage_tokens"])
        self.assertEqual(self.imports.budget("migration-2026")["tokens_reserved"], 100)

    def test_invalid_plan_failure_records_usage_and_charges_excess(self):
        first = self.reserve()
        self.imports.fail_run(1, first["id"], "Модель вернула некорректный план.", usage_tokens=201)
        self.assertEqual(self.imports.run(1, first["id"])["usage_tokens"], 201)
        self.assertEqual(self.imports.budget("migration-2026")["tokens_reserved"], 201)
        with self.assertRaisesRegex(ClubError, "Лимит токенов"):
            self.reserve(request_key="second")
        with self.assertRaises(ClubError):
            self.imports.fail_run(1, first["id"], "Повтор.", usage_tokens=201)
        self.assertEqual(self.imports.budget("migration-2026")["tokens_reserved"], 201)

    def test_known_failed_usage_below_reservation_does_not_refund(self):
        run = self.reserve()
        self.imports.fail_run(1, run["id"], "План отклонён.", usage_tokens=25)
        self.assertEqual(self.imports.run(1, run["id"])["usage_tokens"], 25)
        self.assertEqual(self.imports.budget("migration-2026")["tokens_reserved"], 100)

    def test_independent_budget_does_not_bypass_global_inflight_limit(self):
        self.reserve()
        with self.assertRaisesRegex(ClubError, "ещё выполняется"):
            self.reserve(guild_id=2, request_key="second", budget_id="new-budget")
        self.assertEqual(self.imports.budget("new-budget")["runs_used"], 0)

    def test_concurrent_connections_admit_only_one_request_and_debit_once(self):
        stores = [ImportStore(Store(self.path)), ImportStore(Store(self.path))]

        def attempt(index):
            try:
                return stores[index].reserve_run(1, 10, 20, "budget", f"request-{index}",
                                                2, 100, 200, {"messages": []})
            except ClubError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.imports.budget("budget")["runs_used"], 1)
        self.assertEqual(self.imports.budget("budget")["tokens_reserved"], 100)

    def test_concurrent_duplicate_request_returns_same_run(self):
        stores = [ImportStore(Store(self.path)), ImportStore(Store(self.path))]
        with ThreadPoolExecutor(max_workers=2) as pool:
            runs = list(pool.map(lambda ledger: ledger.reserve_run(
                1, 10, 20, "budget", "one-request", 2, 100, 200, {"messages": []}), stores))
        self.assertEqual(runs[0]["id"], runs[1]["id"])
        self.assertEqual(self.imports.budget("budget")["runs_used"], 1)

    def test_recovery_marks_unknown_without_retry_and_releases_global_inflight_lock(self):
        first = self.reserve()
        restored = ImportStore(Store(self.path))
        self.assertEqual(restored.recover_runs(), {"unknown": 1, "review": 0})
        self.assertEqual(restored.run(1, first["id"])["state"], "unknown")
        self.assertEqual(restored.recover_runs(), {"unknown": 0, "review": 0})
        self.assertEqual(self.reserve()["state"], "unknown")
        self.assertEqual(self.reserve(request_key="explicit-new")["state"], "running")
        self.assertEqual(restored.budget("migration-2026")["runs_used"], 2)

    def test_review_apply_done_and_repeat_apply_state_transitions(self):
        run = self.reserve()
        with self.assertRaises(ClubError):
            self.imports.claim_apply(1, run["id"])
        self.imports.save_plan(1, run["id"], {"items": [{"title": "Эссе"}]}, usage_tokens=90)
        self.assertEqual(self.imports.run(1, run["id"])["plan"], {"items": [{"title": "Эссе"}]})
        self.assertTrue(self.imports.claim_apply(1, run["id"]))
        with self.assertRaises(ClubError):
            self.imports.claim_apply(1, run["id"])
        self.imports.defer_apply(1, run["id"])
        self.assertTrue(self.imports.claim_apply(1, run["id"]))
        self.imports.finish_apply(1, run["id"])
        self.imports.finish_apply(1, run["id"])
        self.assertFalse(self.imports.claim_apply(1, run["id"]))
        with self.assertRaises(ClubError):
            self.imports.save_plan(1, run["id"], {"items": []})
        with self.assertRaises(ClubError):
            self.imports.fail_run(1, run["id"], "Поздний сбой.")
        with self.assertRaises(ClubError):
            self.imports.defer_apply(1, run["id"])

    def test_recovery_of_apply_preserves_source_claims_and_plan(self):
        run = self.applying()
        self.assertTrue(self.imports.claim_sources(1, run["id"], "essay-one", [100, 101]))
        restored = ImportStore(Store(self.path))
        self.assertEqual(restored.recover_runs(), {"unknown": 0, "review": 1})
        self.assertEqual(restored.run(1, run["id"])["plan"], {"items": []})
        self.assertTrue(restored.claim_apply(1, run["id"]))
        self.assertTrue(restored.claim_sources(1, run["id"], "essay-one", [101, 100]))

    def test_sources_overlap_rejects_atomically_and_same_item_exact_set_resumes(self):
        run = self.applying()
        self.assertTrue(self.imports.claim_sources(1, run["id"], "essay-one", [100, 101]))
        self.assertTrue(self.imports.claim_sources(1, run["id"], "essay-one", [101, 100]))
        self.assertFalse(self.imports.claim_sources(1, run["id"], "essay-two", [102, 101]))
        self.assertIsNone(self.imports.imported_source(1, 102))
        self.assertFalse(self.imports.claim_sources(1, run["id"], "essay-one", [100, 101, 102]))
        self.assertIsNone(self.imports.imported_source(1, 102))

    def test_source_claims_persist_across_runs_and_guilds_are_isolated(self):
        first = self.applying()
        self.assertTrue(self.imports.claim_sources(1, first["id"], "essay-one", [100]))
        self.imports.finish_sources(1, "essay-one", 300)
        self.imports.finish_apply(1, first["id"])
        second = self.applying(request_key="second")
        self.assertTrue(self.imports.claim_sources(1, second["id"], "essay-one", [100]))
        self.assertFalse(self.imports.claim_sources(1, second["id"], "different", [100]))
        self.assertEqual(self.imports.imported_source(1, 100)["run_id"], first["id"])
        self.assertEqual(self.imports.imported_source(1, 100)["thread_id"], 300)
        third = self.applying(guild_id=2, request_key="third")
        self.assertTrue(self.imports.claim_sources(2, third["id"], "essay-one", [100]))
        self.assertIsNone(self.imports.imported_source(2, 100)["thread_id"])

    def test_concurrent_conflicting_sources_have_only_one_winner(self):
        first = self.applying()
        second = self.applying(request_key="second")
        ledgers = [ImportStore(Store(self.path)), ImportStore(Store(self.path))]
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda args: ledgers[args[0]].claim_sources(
                1, args[1], args[2], args[3]), [(0, first["id"], "a", [100, 101]),
                                            (1, second["id"], "b", [101, 102])]))
        self.assertEqual(outcomes.count(True), 1)
        self.assertEqual(len(self.store.rows("SELECT * FROM bc_import_sources")), 2)

    def test_completion_is_idempotent_and_cannot_rebind_an_imported_essay(self):
        run = self.applying()
        self.imports.claim_sources(1, run["id"], "essay", [100, 101])
        self.imports.finish_sources(1, "essay", 300)
        self.imports.finish_sources(1, "essay", 300)
        self.assertEqual(self.imports.imported_source(1, 101)["thread_id"], 300)
        with self.assertRaises(ClubError):
            self.imports.finish_sources(1, "essay", 301)
        with self.assertRaises(ClubError):
            self.imports.finish_sources(1, "missing", 300)

    def test_run_and_mutations_reject_other_guild(self):
        run = self.reserve()
        for action in (lambda: self.imports.run(2, run["id"]),
                       lambda: self.imports.save_plan(2, run["id"], {}),
                       lambda: self.imports.fail_run(2, run["id"], "Причина"),
                       lambda: self.imports.claim_apply(2, run["id"]),
                       lambda: self.imports.finish_apply(2, run["id"]),
                       lambda: self.imports.defer_apply(2, run["id"]),
                       lambda: self.imports.claim_sources(2, run["id"], "essay", [100])):
            with self.assertRaisesRegex(ClubError, "не найден"):
                action()

    def test_invalid_reservations_do_not_spend_any_budget(self):
        for changed in (dict(reserve_tokens=0), dict(max_runs=True), dict(max_tokens=-1),
                        dict(snapshot=[]), dict(snapshot={"x": float("nan")}), dict(request_key="")):
            with self.assertRaises(ClubError):
                self.reserve(**changed)
        self.assertEqual(self.imports.budget("migration-2026")["runs_used"], 0)
        self.assertEqual(self.store.rows("SELECT id FROM bc_import_runs"), [])

    def test_invalid_source_claim_does_not_change_ledger(self):
        run = self.applying()
        for sources in ([], [0], [True], ["100"]):
            with self.assertRaises(ClubError):
                self.imports.claim_sources(1, run["id"], "essay", sources)
        self.assertEqual(self.store.rows("SELECT * FROM bc_import_sources"), [])

    def test_v4_migration_preserves_existing_club_legacy_data_and_user_version(self):
        self.store.configure(1, dict(news=11, chat=12, books=13, essays=14, voice=15))
        book = self.store.create_book(1, "Книга", "Автор", "", "book")
        self.store.bind_setup_resource(1, "books", 13, state="ready")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript("""
                DROP TABLE bc_import_sources;
                DROP TABLE bc_import_runs;
                DROP TABLE bc_import_budgets;
                DELETE FROM bc_migrations WHERE version>=5;
                PRAGMA user_version=7;
                CREATE TABLE legacy_notes(note TEXT);
                INSERT INTO legacy_notes VALUES('preserved');
            """)
        migrated = Store(self.path)
        self.assertEqual(migrated.book(1, book["id"]), book)
        self.assertEqual(migrated.setup_resource(1, "books")["channel_id"], 13)
        self.assertEqual(migrated.settings(1), self.store.settings(1))
        self.assertEqual(migrated.one("SELECT note FROM legacy_notes")["note"], "preserved")
        self.assertEqual(migrated.one("PRAGMA user_version")["user_version"], 7)
        self.assertEqual(migrated.rows("SELECT version FROM bc_migrations ORDER BY version"),
                         [{"version": version} for version in range(1, 9)])
        run = ImportStore(migrated).reserve_run(1, 10, 20, "budget", "key", 1, 100, 100, {})
        restored = ImportStore(Store(self.path))
        self.assertEqual(restored.run(1, run["id"]), run)


if __name__ == "__main__":
    unittest.main()

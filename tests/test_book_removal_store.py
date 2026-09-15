"""Removal plans freeze user consent and keep Discord/import identities intact."""
import copy
import unittest

from bookclub.book_removal import (
    build_removal_plan, commit_removal_plan, complete_removal_resource,
    fail_removal_resource, get_removal_operation, latest_book_removal_operation,
    removal_operations, removal_resources, retry_removal_operation,
    set_removal_resource_state,
)
from bookclub.store import ClubError, Store
from test_bookclub import ClubFixture, CONFIG


class BookRemovalStoreTests(ClubFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.target = self.store.create_book(1, 'Книга без дубликата', 'Автор', '', 'target')
        self.add_essay(1010, author=1)
        self.add_essay(1020, author=2, submitted=False)
        self.add_essay(1030, author=3)
        self.store.delete_essay(1, source_id=1030)
        self.add_essay(1040, author=1, book=self.target)
        self.store.reserve_publication(f'book:{self.book["id"]}', 1, 13)
        self.store.save_publication(f'book:{self.book["id"]}', 900, 900, 'card')
        with self.store.tx() as db:
            db.execute("INSERT INTO bc_import_budgets VALUES('legacy',1,30714)")
            db.execute('''INSERT INTO bc_import_runs
              (id,guild_id,actor_id,source_channel_id,budget_id,request_key,reserved_tokens,
               state,snapshot,created_at,updated_at)
              VALUES('done-run',1,99,987,'legacy','legacy',30714,'done','{}',1,1)''')
            db.execute("INSERT INTO bc_import_sources VALUES(1,1010,'done-run','item',1010)")

    def add_essay(self, source, author=1, submitted=True, book=None, channel=None):
        book = book or self.book
        self.store.register_essay(1, book['id'], source, channel or source, author,
                                  'Эссе', f'https://example.org/{source}', managed=True, submitted=submitted)
        self.store.reserve_publication(f'essay-space:{book["id"]}:{author}:{source}', 1, 14, webhook_id=200)
        self.store.save_publication(f'essay-space:{book["id"]}:{author}:{source}', channel or source, source, 'essay')

    def plan(self, mode='keep'):
        return build_removal_plan(self.store, 1, self.book['id'], mode,
                                  self.target['id'] if mode.startswith('transfer') else None)

    def commit(self, plan=None):
        return commit_removal_plan(self.store, 1, 99, plan or self.plan())

    def snapshot(self, *tables):
        return {table: self.store.rows(f'SELECT * FROM {table} ORDER BY rowid') for table in tables}

    def finish(self, operation):
        for resource in removal_resources(self.store, 1, operation['id']):
            complete_removal_resource(self.store, 1, operation['id'], resource['id'])

    def test_preview_is_read_only_includes_exact_scope_and_same_author_collision(self):
        before = self.snapshot('bc_books', 'bc_essays', 'bc_jobs', 'bc_import_runs',
                               'bc_import_budgets', 'bc_book_removal_operations')
        plan = self.plan('transfer_topic')
        self.assertEqual(plan['source'], self.store.book(1, self.book['id']))
        self.assertEqual(plan['target'], self.target)
        self.assertEqual(len(plan['essays']), 3)
        self.assertEqual(plan['overlapping_authors'], [1])
        self.assertEqual(plan['meetings_count'], 1)
        self.assertEqual([r['kind'] for r in plan['resources']],
                         ['refresh_essay', 'refresh_essay', 'delete_book_topic'])
        self.assertEqual(self.snapshot(*before), before)

    def test_transfer_retains_every_identity_and_import_record_and_duplicate_author(self):
        tables = ('bc_publications', 'bc_import_runs', 'bc_import_sources', 'bc_import_budgets', 'bc_meetings')
        unchanged = self.snapshot(*tables)
        before = self.store.rows('SELECT * FROM bc_essays ORDER BY source_id')
        operation = self.commit(self.plan('transfer'))
        after = self.store.rows('SELECT * FROM bc_essays ORDER BY source_id')
        self.assertEqual(after, [{**essay, 'book_id': self.target['id']} for essay in before])
        self.assertEqual([e['source_id'] for e in self.store.essays(self.target['id']) if e['author_id'] == 1],
                         [1010, 1040])
        self.assertEqual(self.snapshot(*unchanged), unchanged)
        self.assertTrue(self.store.book(1, self.book['id'])['deleted'])
        self.assertEqual(self.store.book(1, self.target['id'])['revision'], self.target['revision'] + 1)
        self.assertEqual(operation['before_state']['essays'], before[:3])
        self.assertEqual(operation['after_state']['essays'], after[:3])

    def test_modes_have_exact_resources_and_keep_has_no_discord_work(self):
        expected = {'keep': [], 'topic': ['delete_book_topic'],
                    'all': ['delete_essay', 'delete_essay', 'delete_book_topic'],
                    'transfer': ['refresh_essay', 'refresh_essay'],
                    'transfer_topic': ['refresh_essay', 'refresh_essay', 'delete_book_topic']}
        for mode, kinds in expected.items():
            with self.subTest(mode=mode):
                self.assertEqual([r['kind'] for r in self.plan(mode)['resources']], kinds)
        operation = self.commit()
        self.assertEqual(operation['state'], 'done')
        self.assertEqual(removal_resources(self.store, 1, operation['id']), [])
        self.assertEqual(self.store.one('SELECT COUNT(*) AS n FROM bc_book_trash_audit')['n'], 1)

    def test_all_marks_tombstones_before_discord_and_never_spends_import_budget(self):
        before = self.snapshot('bc_import_runs', 'bc_import_budgets', 'bc_import_sources', 'bc_publications')
        operation = self.commit(self.plan('all'))
        self.assertEqual(self.store.essays(self.book['id'], submitted_only=False), [])
        self.assertEqual(self.snapshot(*before), before)
        for channel, source in ((1010, 1010), (1010, 1011), (1030, 1030)):
            with self.subTest(source=source), self.assertRaisesRegex(ClubError, 'удалено'):
                self.store.register_essay(1, self.target['id'], source, channel, 1, 'Повтор',
                                          'https://example.org', correct=True)
        self.finish(operation)
        with self.assertRaisesRegex(ClubError, 'удалено'):
            self.store.register_essay(1, self.target['id'], 1010, 1010, 1, 'Повтор',
                                      'https://example.org', correct=True)

    def test_stale_book_revision_or_essay_set_or_binding_rejects_atomic_commit(self):
        for change in ('book', 'target', 'essay', 'target_essay', 'binding'):
            with self.subTest(change=change):
                plan = self.plan('transfer')
                if change == 'book':
                    self.store.update_book(1, self.book['id'], title='Новое название')
                elif change == 'target':
                    self.store.update_book(1, self.target['id'], author='Другой автор')
                elif change == 'essay':
                    self.add_essay(1050)
                elif change == 'target_essay':
                    self.add_essay(1060, book=self.target)
                else:
                    key = next(r['publication_key'] for r in plan['resources'] if r['publication_key'])
                    self.store.save_publication(key, 777, 777, 'replaced')
                before = self.snapshot('bc_books', 'bc_essays', 'bc_jobs', 'bc_book_removal_operations')
                with self.assertRaisesRegex(ClubError, 'изменились'):
                    self.commit(plan)
                self.assertEqual(self.snapshot(*before), before)

    def test_preview_payload_mutation_and_wrong_actor_guild_or_target_are_rejected(self):
        plan = self.plan('all')
        changed = copy.deepcopy(plan)
        changed['resources'][0]['channel_id'] = 9999
        for guild, actor, candidate in ((1, 99, changed), (2, 99, plan), (1, True, plan), (1, 0, plan)):
            with self.subTest(guild=guild, actor=actor), self.assertRaises(ClubError):
                commit_removal_plan(self.store, guild, actor, candidate)
        self.store.configure(2, CONFIG)
        foreign = self.store.create_book(2, 'Чужая', 'Автор', '', 'foreign')
        for target in (self.book['id'], foreign['id'], None):
            with self.subTest(target=target), self.assertRaises(ClubError):
                build_removal_plan(self.store, 1, self.book['id'], 'transfer', target)
        self.assertEqual(removal_operations(self.store, 1, pending_only=False), [])

    def test_repeat_confirm_cannot_move_again_and_pending_books_cannot_be_reused(self):
        plan = self.plan('transfer')
        operation = self.commit(plan)
        with self.assertRaisesRegex(ClubError, 'уже выполнено'):
            self.commit(plan)
        with self.assertRaisesRegex(ClubError, 'не завершена'):
            build_removal_plan(self.store, 1, self.target['id'], 'keep')
        with self.assertRaisesRegex(ClubError, 'не завершена'):
            self.store.remove_book(1, self.target['id'], actor_id=99,
                                   expected_revision=self.store.book(1, self.target['id'])['revision'])
        another = self.store.create_book(1, 'Другой дубликат', 'Автор', '', 'third')
        with self.assertRaisesRegex(ClubError, 'не завершена'):
            build_removal_plan(self.store, 1, another['id'], 'transfer', self.target['id'])
        self.finish(operation)
        self.assertTrue(build_removal_plan(self.store, 1, self.target['id'], 'keep'))

    def test_restoration_waits_for_all_work_including_failed_and_keeps_transferred_essays(self):
        operation = self.commit(self.plan('transfer_topic'))
        source = self.store.book(1, self.book['id'])
        for stage in ('pending', 'failed'):
            if stage == 'failed':
                resource = removal_resources(self.store, 1, operation['id'])[0]
                fail_removal_resource(self.store, 1, operation['id'], resource['id'], 'Forbidden')
            with self.subTest(stage=stage), self.assertRaisesRegex(ClubError, 'не завершена'):
                self.store.restore_book(1, source['id'], actor_id=99, expected_revision=source['revision'])
        retry_removal_operation(self.store, 1, operation['id'], 99)
        self.finish(operation)
        self.store.restore_book(1, source['id'], actor_id=99, expected_revision=source['revision'])
        self.assertEqual(self.store.essays(source['id']), [])
        self.assertEqual(len(self.store.essays(self.target['id'])), 2)

    def test_restart_preserves_inflight_work_and_idempotent_completion(self):
        operation = self.commit(self.plan('all'))
        resource = removal_resources(self.store, 1, operation['id'])[0]
        set_removal_resource_state(self.store, 1, operation['id'], resource['id'], 'deleting')
        self.store = Store(self.path, clock=lambda: self.now)
        self.assertEqual(removal_operations(self.store, 1, self.book['id'])[0]['id'], operation['id'])
        pending = removal_resources(self.store, 1, operation['id'])
        self.assertEqual((pending[0]['state'], pending[0]['attempts']), ('deleting', 1))
        for _ in range(2):
            complete_removal_resource(self.store, 1, operation['id'], resource['id'])
        self.assertEqual(get_removal_operation(self.store, 1, operation['id'])['state'], 'pending')
        self.finish(operation)
        self.assertEqual(get_removal_operation(self.store, 1, operation['id'])['state'], 'done')
        self.assertEqual(latest_book_removal_operation(self.store, 1, self.book['id'])['id'], operation['id'])

    def test_failure_requires_explicit_retry_keeps_completed_ids_and_audits_actor(self):
        operation = self.commit(self.plan('all'))
        resources = removal_resources(self.store, 1, operation['id'])
        complete_removal_resource(self.store, 1, operation['id'], resources[0]['id'])
        fail_removal_resource(self.store, 1, operation['id'], resources[1]['id'], 'Forbidden')
        self.assertEqual(removal_operations(self.store, 1), [])
        self.assertNotIn(resources[1]['id'], [r['id'] for r in removal_resources(self.store, 1, operation['id'])])
        with self.assertRaisesRegex(ClubError, 'явно повторите'):
            complete_removal_resource(self.store, 1, operation['id'], resources[1]['id'])
        retried = retry_removal_operation(self.store, 1, operation['id'], 100)
        self.assertEqual((retried['actor_id'], retried['confirmed_actor_id']), (100, 99))
        self.assertEqual(retried['plan'], operation['plan'])
        self.assertNotIn(resources[0]['id'], [r['id'] for r in removal_resources(self.store, 1, operation['id'])])
        audit = self.store.one('SELECT * FROM bc_book_removal_retries')
        self.assertEqual((audit['actor_id'], audit['previous_actor_id']), (100, 99))

    def test_resource_state_and_retry_are_guild_and_operation_scoped(self):
        operation = self.commit(self.plan('all'))
        resource = removal_resources(self.store, 1, operation['id'])[0]
        with self.assertRaises(ClubError):
            complete_removal_resource(self.store, 2, operation['id'], resource['id'])
        with self.assertRaises(ClubError):
            complete_removal_resource(self.store, 1, operation['id'], 'other-resource')
        fail_removal_resource(self.store, 1, operation['id'], resource['id'], 'Forbidden')
        with self.assertRaises(ClubError):
            retry_removal_operation(self.store, 2, operation['id'], 99)
        self.assertEqual(get_removal_operation(self.store, 1, operation['id'])['state'], 'failed')

    def test_preserve_essays_modes_reject_deleting_book_topic_containing_live_essay(self):
        self.add_essay(901, author=1, channel=900)
        for mode in ('topic', 'transfer_topic'):
            with self.subTest(mode=mode), self.assertRaisesRegex(ClubError, 'В теме книги есть эссе'):
                self.plan(mode)
        self.assertTrue(self.plan('keep'))
        self.assertTrue(self.plan('transfer'))
        self.assertTrue(self.plan('all'))

    def test_ready_publication_intents_are_history_but_reserved_sends_block_confirmation(self):
        key = f'book:{self.book["id"]}'
        with self.store.tx() as db:
            db.execute('''INSERT INTO bc_publication_intents
              (key,guild_id,channel_id,after_id,content,attachments,components,created_at)
              VALUES(?,1,13,0,'Card','[]','[]',0)''', (key,))
        self.assertTrue(self.plan('topic'))
        with self.store.tx() as db:
            db.execute("UPDATE bc_publications SET state='reserved' WHERE key=?", (key,))
        with self.assertRaisesRegex(ClubError, 'ещё не завершена'):
            self.plan('topic')

    def test_migration13_is_repeatable_and_changes_no_existing_state(self):
        before = self.snapshot('bc_books', 'bc_essays', 'bc_meetings', 'bc_jobs',
                               'bc_publications', 'bc_import_runs', 'bc_import_sources', 'bc_import_budgets')
        with self.store.tx() as db:
            for table in ('bc_book_removal_tombstones', 'bc_book_removal_retries',
                          'bc_book_removal_resources', 'bc_book_removal_operations'):
                db.execute(f'DROP TABLE {table}')
            db.execute('DELETE FROM bc_migrations WHERE version=13')
        self.store = Store(self.path, clock=lambda: self.now)
        self.store = Store(self.path, clock=lambda: self.now)
        self.assertEqual(self.snapshot(*before), before)
        self.assertEqual(self.store.one('SELECT MAX(version) AS n FROM bc_migrations')['n'], 13)


if __name__ == '__main__':
    unittest.main()

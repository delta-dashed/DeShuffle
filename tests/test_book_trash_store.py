"""Catalog removal is reversible and cannot mutate archive/import history."""
import json
import unittest

from bookclub.store import ClubError, Store
from test_bookclub import ClubFixture, CONFIG


class BookTrashStoreTests(ClubFixture, unittest.TestCase):
    def current(self):
        return self.store.book(1, self.book['id'])

    def remove(self, **kwargs):
        return self.store.remove_book(1, self.book['id'], actor_id=99,
                                     expected_revision=kwargs.get('revision', self.current()['revision']))

    def restore(self, **kwargs):
        return self.store.restore_book(1, self.book['id'], actor_id=99,
                                      expected_revision=kwargs.get('revision', self.current()['revision']))

    def snapshot(self, *tables):
        return {table: self.store.rows(f'SELECT * FROM {table} ORDER BY rowid') for table in tables}

    def seed_archive(self):
        self.store.register_essay(1, self.book['id'], 123, 124, 1, 'Эссе', 'https://example.org/essay')
        self.store.reserve_publication(f'book:{self.book["id"]}', 1, 13)
        self.store.save_publication(f'book:{self.book["id"]}', 500, 500, 'stable')
        self.store.bind_tag(1, 13, 'reading', 200)
        with self.store.tx() as db:
            db.execute("INSERT INTO bc_import_budgets VALUES('legacy-essays-v1',1,30714)")
            db.execute('''INSERT INTO bc_import_runs
              (id,guild_id,actor_id,source_channel_id,budget_id,request_key,reserved_tokens,
               state,snapshot,created_at,updated_at)
              VALUES('done-run',1,99,987,'legacy-essays-v1','legacy',30714,'done','{}',1,1)''')
            db.execute("INSERT INTO bc_import_sources VALUES(1,123,'done-run','item',124)")

    def test_remove_hides_book_but_preserves_status_position_all_archive_and_meeting_rows(self):
        self.seed_archive()
        self.store.update_book(1, self.book['id'], status='reading', position=7)
        before = self.current()
        tables = ('bc_meetings', 'bc_participants', 'bc_essays', 'bc_publications',
                  'bc_import_runs', 'bc_import_budgets', 'bc_import_sources', 'bc_forum_tags')
        snapshot = self.snapshot(*tables)
        removed = self.remove()
        self.assertEqual(self.store.books(1), [])
        self.assertEqual(self.store.books(1, include_deleted=True), [removed])
        self.assertEqual((removed['status'], removed['position'], removed['deadline']),
                         (before['status'], before['position'], before['deadline']))
        self.assertEqual(removed['revision'], before['revision'] + 1)
        self.assertEqual((removed['deleted'], removed['status_automation'], removed['status_automation_pending']), (1, 0, 0))
        self.assertEqual(self.snapshot(*tables), snapshot)
        self.assertTrue(self.store.essays(self.book['id']))
        audit = self.store.one('SELECT * FROM bc_book_trash_audit')
        self.assertEqual((audit['actor_id'], audit['action'], audit['created_at']), (99, 'remove', self.now))
        self.assertEqual(json.loads(audit['previous_state']), before)

    def test_delete_cancels_only_pending_book_and_meeting_jobs(self):
        self.essay_event(self.now + 5 * 86400)
        with self.store.tx() as db:
            for state in ('sent', 'unknown', 'sending', 'failed'):
                db.execute('''INSERT INTO bc_jobs(key,guild_id,entity_id,revision,kind,target,due,state)
                  VALUES(?,1,?,0,'participants',0,?,?)''', (state, self.meeting['id'], self.now, state))
        old = self.store.rows("SELECT * FROM bc_jobs WHERE state<>'pending' ORDER BY key")
        self.assertTrue(self.store.rows("SELECT 1 FROM bc_jobs WHERE state='pending'"))
        self.remove()
        self.assertFalse(self.store.rows("SELECT 1 FROM bc_jobs WHERE state='pending'"))
        self.assertEqual(self.store.rows("SELECT * FROM bc_jobs WHERE state NOT IN ('pending','cancelled') ORDER BY key"), old)
        self.assertEqual(self.store.due_jobs(1), [])

    def test_remove_and_restore_are_revision_guarded_scoped_and_audited_once(self):
        old = self.current()['revision']
        removed = self.remove()
        with self.assertRaisesRegex(ClubError, 'изменилась'):
            self.remove(revision=old)
        with self.assertRaisesRegex(ClubError, 'изменилась'):
            self.restore(revision=old)
        self.assertEqual(self.remove(), removed)
        self.store.configure(2, CONFIG)
        with self.assertRaises(ClubError):
            self.store.restore_book(2, self.book['id'], actor_id=99, expected_revision=removed['revision'])
        restored = self.restore()
        self.assertEqual(self.restore(), restored)
        self.assertEqual([a['action'] for a in self.store.rows('SELECT * FROM bc_book_trash_audit ORDER BY id')],
                         ['remove', 'restore'])

    def test_restore_uses_original_status_and_position_without_enabling_auto_status(self):
        self.store.update_book(1, self.book['id'], status='queued', position=42)
        self.remove()
        restored = self.restore()
        self.assertEqual((restored['status'], restored['position'], restored['deleted']), ('queued', 42, 0))
        self.assertEqual((restored['status_automation'], restored['status_automation_pending']), (0, 0))
        self.assertEqual(self.store.books(1), [restored])

    def test_removed_current_does_not_block_another_current_but_restore_conflict_is_atomic(self):
        self.store.update_book(1, self.book['id'], status='reading')
        self.remove()
        second = self.store.create_book(1, 'Другая', 'Автор', '', 'second')
        self.store.update_book(1, second['id'], status='reading')
        before = self.snapshot('bc_books', 'bc_meetings', 'bc_jobs', 'bc_book_trash_audit')
        with self.assertRaisesRegex(ClubError, 'другая книга'):
            self.restore()
        self.assertEqual(self.snapshot(*before), before)
        self.store.update_book(1, second['id'], status='read')
        self.assertEqual(self.restore()['status'], 'reading')
        # Reopening a migrated database must retain the partial index, too.
        restarted = Store(self.path, clock=lambda: self.now)
        with self.assertRaises(ClubError):
            restarted.update_book(1, second['id'], status='reading')

    def test_restart_allows_two_historical_reading_rows_but_only_one_active(self):
        self.store.update_book(1, self.book['id'], status='reading')
        self.remove()
        second = self.store.create_book(1, 'Следующая', 'Автор', '', 'second')
        self.store.update_book(1, second['id'], status='reading')
        restarted = Store(self.path, clock=lambda: self.now)
        self.assertEqual(len(restarted.books(1)), 1)
        self.assertEqual(len(restarted.books(1, include_deleted=True)), 2)

    def test_removed_current_does_not_block_automatic_start_of_an_active_book(self):
        self.store.update_book(1, self.book['id'], status='reading')
        self.remove()
        book = self.store.create_book(1, 'Следующая', 'Автор', '', 'second')
        meeting = self.store.draft_meeting(1, book['id'], 'Первая', '1', 'Глава', 'second-event')
        self.store.sync_event(1, meeting['id'], event_id=333, name='Первая', start=self.now,
                              end=self.now + 3600, voice_id=15, status='active')
        self.assertEqual(self.store.book(1, book['id'])['status'], 'reading')
        self.assertEqual(self.current()['deleted'], 1)

    def test_restore_creates_future_jobs_without_reviving_cancelled_rows_or_old_offers(self):
        self.essay_event(self.now + 4 * 86400)
        self.action('offer', actor=99, organizer=True, candidate=1)
        self.remove()
        old = self.store.rows('SELECT * FROM bc_jobs ORDER BY key')
        self.now += 60
        self.restore()
        fresh = self.store.rows("SELECT * FROM bc_jobs WHERE state='pending'")
        self.assertTrue(fresh)
        self.assertTrue(all(j['due'] > self.now for j in fresh))
        self.assertTrue(all(j['kind'] not in ('offer', 'rescheduled') for j in fresh))
        self.assertTrue(any(j['kind'].startswith('essay') for j in fresh))
        self.assertTrue(any(j['kind'] == 'participants' for j in fresh))
        for job in old:
            self.assertEqual(self.store.one('SELECT * FROM bc_jobs WHERE key=?', (job['key'],)), job)

    def test_restore_after_events_are_past_creates_no_historical_notices(self):
        self.essay_event(self.now + 86400)
        self.remove()
        self.now += 30 * 86400
        self.restore()
        self.assertEqual(self.store.rows("SELECT * FROM bc_jobs WHERE state='pending'"), [])

    def test_settings_reconciliation_and_event_updates_leave_removed_book_history_unchanged(self):
        self.essay_event(self.now + 4 * 86400)
        self.action('offer', actor=99, organizer=True, candidate=1)
        self.remove()
        before = self.snapshot('bc_books', 'bc_meetings', 'bc_jobs', 'bc_book_status_audit')
        self.now += 2 * 86400
        self.store.configure(1, {**CONFIG, 'essay_hours': 12})
        self.store.reconcile_book_statuses(1)
        self.assertEqual(self.store.expire_offers(1, {1, 2, 3}), 0)
        self.assertFalse(self.store.sync_event(1, self.meeting['id'], event_id=self.meeting['event_id'],
            name='Позднее событие', start=self.now, end=self.now + 60, voice_id=15, status='completed'))
        settings = self.store.settings(1)
        with self.store.tx() as db:
            self.store._schedule(db, self.meeting['id'], settings)
            self.store._essay_schedule(db, self.book['id'], settings)
        self.assertEqual(self.snapshot(*before), before)

    def test_claim_refuses_stray_pending_jobs_for_removed_book_even_after_restart(self):
        self.remove()
        revision = self.current()['revision']
        with self.store.tx() as db:
            for ident, revision, kind in ((self.book['id'], revision, 'essay'),
                                          (self.meeting['id'], self.meeting['revision'], 'participants')):
                db.execute('''INSERT INTO bc_jobs(key,guild_id,entity_id,revision,kind,target,due)
                  VALUES(?,1,?,?,?,0,?)''', ('stray-' + kind, ident, revision, kind, self.now))
        restarted = Store(self.path, clock=lambda: self.now)
        self.assertFalse(restarted.claim_job('stray-essay'))
        self.assertFalse(restarted.claim_job('stray-participants'))
        self.assertEqual(restarted.one("SELECT COUNT(*) AS n FROM bc_jobs WHERE state='pending'")['n'], 0)

    def test_removed_book_rejects_user_mutations_but_retains_historical_plan_read(self):
        self.action('volunteer')
        plan = self.store.plan(1, self.meeting['id'], 1)
        self.store.save_plan(1, self.meeting['id'], 1, generation=plan['generation'], version=0, topics='Сохранить')
        self.remove()
        actions = [
            lambda: self.store.require_active_book(1, self.book['id']),
            lambda: self.store.update_book(1, self.book['id'], status='read'),
            lambda: self.store.set_book_status_automation(1, self.book['id'], True),
            lambda: self.store.participant(1, self.book['id'], 1),
            lambda: self.store.attendance(1, self.meeting['id'], 1, True),
            lambda: self.store.draft_meeting(1, self.book['id'], 'Новая', '1', 'Глава', 'new'),
            lambda: self.store.set_meeting_plan_kind(1, self.meeting['id'], 'essay'),
            lambda: self.action('replace', actor=99, organizer=True),
            lambda: self.store.save_plan(1, self.meeting['id'], 1, generation=plan['generation'], version=1, notes='Нет'),
            lambda: self.store.handover(1, self.meeting['id'], 99, organizer=True),
            lambda: self.store.register_essay(1, self.book['id'], 111, 111, 1, 'Нет', 'https://example.org'),
        ]
        for action in actions:
            with self.subTest(action=actions.index(action)), self.assertRaisesRegex(ClubError, 'удалена'):
                action()
        self.assertEqual(self.store.plan(1, self.meeting['id'], 1)['topics'], 'Сохранить')

    def test_restore_invalidates_host_buttons_and_plan_forms_from_before_removal(self):
        self.action('volunteer')
        self.save_plan(topics='План')
        meeting = self.store.meeting(1, self.meeting['id'])
        plan = self.store.plan(1, self.meeting['id'], 1)
        self.remove()
        self.restore()
        with self.assertRaisesRegex(ClubError, 'Назначение изменилось'):
            self.action('replace', actor=99, organizer=True, version=meeting['host_version'])
        with self.assertRaisesRegex(ClubError, 'План уже изменён'):
            self.store.save_plan(1, self.meeting['id'], 1, generation=plan['generation'],
                                 version=plan['version'], topics='Старая форма')
        self.assertEqual(self.store.plan(1, self.meeting['id'], 1)['topics'], 'План')

    def test_create_request_retry_and_batch_import_cannot_resurrect_removed_books(self):
        self.remove()
        same = self.store.create_book(1, 'Книга', 'Автор', 'https://example.org/book', 'book1')
        self.assertEqual((same['id'], same['deleted']), (self.book['id'], 1))
        created, skipped = self.store.create_books(1, [{'title': 'Книга', 'author': 'Автор', 'materials': ''}], 'batch')
        self.assertEqual((created, skipped), ([], 1))
        self.assertEqual(self.store.books(1), [])

    def test_invalid_actor_or_missing_revision_makes_no_changes(self):
        before = self.current()
        for actor, revision in ((None, before['revision']), (0, before['revision']), (True, before['revision']), (99, None)):
            with self.subTest(actor=actor, revision=revision), self.assertRaises(ClubError):
                self.store.remove_book(1, before['id'], actor_id=actor, expected_revision=revision)
        self.assertEqual(self.current(), before)
        self.assertEqual(self.store.rows('SELECT * FROM bc_book_trash_audit'), [])

    def test_migration_from_v11_preserves_every_book_and_import_and_is_repeatable(self):
        self.seed_archive()
        self.store.update_book(1, self.book['id'], status='reading', position=5)
        before = self.snapshot('bc_books', 'bc_meetings', 'bc_essays', 'bc_publications',
                               'bc_import_runs', 'bc_import_sources', 'bc_import_budgets', 'bc_forum_tags')
        with self.store.tx() as db:
            db.execute('DROP INDEX bc_one_current')
            db.execute('ALTER TABLE bc_books DROP COLUMN deleted')
            db.execute("CREATE UNIQUE INDEX bc_one_current ON bc_books(guild_id) WHERE status='reading'")
            db.execute('DROP TABLE bc_book_trash_audit')
            db.execute('DELETE FROM bc_migrations WHERE version=12')
            db.execute('PRAGMA user_version=42')
        self.store = Store(self.path, clock=lambda: self.now)
        self.assertEqual(self.snapshot(*before), before)
        self.assertEqual(self.store.one('PRAGMA user_version')['user_version'], 42)
        self.assertEqual(self.store.one('SELECT MAX(version) AS version FROM bc_migrations')['version'], 13)
        self.store = Store(self.path, clock=lambda: self.now)
        self.assertEqual(self.snapshot(*before), before)


if __name__ == '__main__':
    unittest.main()

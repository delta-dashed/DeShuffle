"""Real SQLite regressions for observed-event N+1 status automation."""
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock
from test_bookclub import stub_delivery_transport

import discord

from bookclub.service import Service
from bookclub.store import ClubError, Store
from test_bookclub import CONFIG
from test_bookclub_discord import not_found
from test_forum_integration import tag
from test_webhook_discord import WebhookHarness
from bookclub.forum_tags import TEMPLATES


class AutomaticBookStatusFixture:
    def setUp(self):
        stub_delivery_transport(self)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'club.sqlite3'
        self.now = 2_000_000_000
        self.store = Store(self.path, clock=lambda: self.now)
        self.store.configure(1, CONFIG)
        self.book = self.store.create_book(1, 'Книга', 'Автор', '', 'book')['id']
        self.store.update_book(1, self.book, reading_meetings=2)
        self.serial = 1000

    def current(self, book=None):
        return self.store.book(1, book or self.book)

    def meeting(self, kind='reading', *, book=None, name='Встреча', part='1'):
        self.serial += 1
        return self.store.draft_meeting(1, book or self.book, name, part, 'Глава', str(self.serial), plan_kind=kind)

    def observe(self, meeting, status, *, voice_id=15, **changes):
        current = self.store.meeting(1, meeting['id'])
        values = dict(event_id=current['event_id'] or int(current['request_key']), name=current['name'],
                      start=self.now + 100, end=self.now + 200, voice_id=voice_id, status=status)
        values.update(changes)
        return self.store.sync_event(1, current['id'], **values)

    def complete_plan(self):
        meetings = [self.meeting(), self.meeting(), self.meeting('essay')]
        for meeting in meetings:
            self.observe(meeting, 'completed')
        return meetings

    def audit(self):
        return self.store.rows('SELECT * FROM bc_book_status_audit ORDER BY id')


class AutomaticBookStatusTests(AutomaticBookStatusFixture, unittest.TestCase):
    def test_future_or_past_date_alone_never_starts_reading(self):
        meeting = self.meeting()
        self.observe(meeting, 'scheduled')
        self.now += 10000
        self.observe(meeting, 'scheduled', start=self.now - 1000, end=self.now - 500)
        self.assertFalse(self.store.reconcile_book_statuses(1))
        self.assertEqual(self.current()['status'], 'proposed')
        self.assertEqual(self.audit(), [])

    def test_first_real_active_event_starts_book_without_a_plan(self):
        self.store.update_book(1, self.book, reading_meetings=None)
        meeting = self.meeting()
        self.observe(meeting, 'active')
        self.assertEqual(self.current()['status'], 'reading')
        self.observe(meeting, 'completed')
        self.assertEqual(self.current()['status'], 'reading')

    def test_exact_n_reading_and_one_essay_must_all_be_completed(self):
        first, second, essay = self.meeting(), self.meeting(), self.meeting('essay')
        for meeting in (first, second):
            self.observe(meeting, 'completed')
        self.observe(essay, 'active')
        self.assertEqual(self.current()['status'], 'reading')
        self.observe(essay, 'completed')
        self.assertEqual(self.current()['status'], 'read')
        self.assertEqual([(row['old_status'], row['new_status']) for row in self.audit()],
                         [('proposed', 'reading'), ('reading', 'read')])

    def test_essay_role_is_not_guessed_from_its_name_or_part(self):
        meetings = [self.meeting(name='Обсуждение эссе', part='Эссе') for _ in range(3)]
        for meeting in meetings:
            self.observe(meeting, 'completed')
        self.assertEqual(self.current()['status'], 'reading')
        self.store.set_meeting_plan_kind(1, meetings[-1]['id'], 'essay', actor_id=99)
        self.assertEqual(self.current()['status'], 'reading')
        self.assertTrue(self.store.reconcile_book_statuses(1))
        self.assertEqual(self.current()['status'], 'read')

    def test_cancelled_meeting_does_not_fill_a_slot_but_replacement_can(self):
        first, cancelled, essay = self.meeting(), self.meeting(), self.meeting('essay')
        for meeting in (first, essay):
            self.observe(meeting, 'completed')
        self.observe(cancelled, 'cancelled')
        self.assertEqual(self.current()['status'], 'reading')
        replacement = self.meeting()
        self.observe(replacement, 'completed')
        self.assertEqual(self.current()['status'], 'read')

    def test_cancelled_essay_cannot_be_replaced_by_a_reading_meeting(self):
        for kind, status in (('reading', 'completed'), ('reading', 'completed'),
                             ('essay', 'cancelled'), ('reading', 'completed')):
            self.observe(self.meeting(kind), status)
        self.assertEqual(self.current()['status'], 'reading')

    def test_missing_extra_event_is_not_treated_as_a_confirmed_cancellation(self):
        extra = self.meeting()
        self.observe(extra, 'cancelled', status_confirmed=False)
        self.complete_plan()
        self.assertEqual(self.current()['status'], 'reading')
        self.assertFalse(self.store.reconcile_book_statuses(1))
        self.observe(extra, 'cancelled')
        self.assertEqual(self.current()['status'], 'read')

    def test_late_confirmed_completion_can_recover_an_event_previously_missing_in_rest(self):
        first, second, essay = self.meeting(), self.meeting(), self.meeting('essay')
        for meeting in (first, second):
            self.observe(meeting, 'completed')
        self.observe(essay, 'active')
        self.observe(essay, 'cancelled', status_confirmed=False)
        self.store = Store(self.path, clock=lambda: self.now)
        self.assertEqual(self.current()['status'], 'reading')
        self.observe(essay, 'completed')
        self.assertEqual(self.current()['status'], 'read')
        self.assertEqual(self.store.meeting(1, essay['id'])['event_status_confirmed'], 1)

    def test_extra_non_cancelled_or_unclassified_meeting_blocks_completion(self):
        for mode in ('draft', 'scheduled', 'completed', 'unclassified'):
            with self.subTest(mode=mode):
                self.store.update_book(1, self.book, status='read')
                self.book = self.store.create_book(1, mode, 'Автор', '', mode)['id']
                self.store.update_book(1, self.book, reading_meetings=2)
                extra = self.meeting()
                if mode != 'draft':
                    self.observe(extra, 'completed' if mode == 'unclassified' else mode)
                if mode == 'unclassified':
                    with self.store.tx() as db:
                        db.execute('UPDATE bc_meetings SET plan_kind=NULL WHERE id=?', (extra['id'],))
                self.complete_plan()
                self.assertEqual(self.current()['status'], 'reading')
                # Each case has a separate book: the extra meeting alone must
                # block completion, independent of previous cases' evidence.
                self.assertFalse(self.store.reconcile_book_statuses(1))

    def test_a_draft_or_non_voice_event_is_not_evidence_of_completion(self):
        first, second, essay = self.meeting(), self.meeting(), self.meeting('essay')
        for meeting in (first, second):
            self.observe(meeting, 'completed')
        self.observe(essay, 'completed', voice_id=None)
        self.assertEqual(self.current()['status'], 'reading')

    def test_unsupported_event_alone_does_not_start_book(self):
        self.observe(self.meeting(), 'unsupported', voice_id=None)
        self.assertEqual(self.current()['status'], 'proposed')

    def test_repeated_and_delayed_events_do_not_regress_read_or_duplicate_audit(self):
        meetings = self.complete_plan()
        before_book, before_audit = self.current(), self.audit()
        for meeting in meetings:
            for status in ('completed', 'active', 'scheduled', 'cancelled'):
                self.assertFalse(self.observe(meeting, status))
        self.assertFalse(self.store.reconcile_book_statuses(1))
        self.assertEqual(self.current(), before_book)
        self.assertEqual(self.audit(), before_audit)
        self.assertEqual({row['status'] for row in self.store.rows('SELECT * FROM bc_meetings')}, {'completed'})

    def test_completed_or_cancelled_event_cannot_be_resurrected(self):
        for terminal in ('completed', 'cancelled'):
            meeting = self.meeting()
            self.observe(meeting, terminal)
            before = self.store.meeting(1, meeting['id'])
            for late in ('scheduled', 'active', 'cancelled' if terminal == 'completed' else 'completed'):
                self.assertFalse(self.observe(meeting, late))
                self.assertEqual(self.store.meeting(1, meeting['id']), before)

    def test_active_event_ignores_a_late_scheduled_snapshot(self):
        meeting = self.meeting()
        self.observe(meeting, 'active')
        self.assertFalse(self.observe(meeting, 'scheduled'))
        self.assertEqual(self.store.meeting(1, meeting['id'])['status'], 'active')

    def test_completed_snapshot_after_restart_recovers_missed_start(self):
        first, second, essay = self.meeting(), self.meeting(), self.meeting('essay')
        self.observe(first, 'scheduled')
        self.store = Store(self.path, clock=lambda: self.now)
        self.observe(first, 'completed')
        self.assertEqual(self.current()['status'], 'reading')
        self.observe(second, 'completed')
        self.observe(essay, 'active')
        self.store = Store(self.path, clock=lambda: self.now)
        self.observe(essay, 'completed')
        self.assertEqual(self.current()['status'], 'read')

    def test_cached_completed_plan_recovers_after_restart_without_event_in_rest(self):
        meetings = [self.meeting(), self.meeting(), self.meeting('essay')]
        self.observe(meetings[0], 'active')
        with self.store.tx() as db:
            for meeting in meetings:
                db.execute("UPDATE bc_meetings SET event_id=?,voice_id=15,status='completed' WHERE id=?",
                           (int(meeting['request_key']), meeting['id']))
        self.store = Store(self.path, clock=lambda: self.now)
        self.assertTrue(self.store.reconcile_book_statuses(1))
        self.assertEqual(self.current()['status'], 'read')

    def test_manual_status_even_the_same_value_pauses_automation_durably(self):
        self.store.update_book(1, self.book, status='proposed', status_actor_id=99)
        self.store = Store(self.path, clock=lambda: self.now)
        self.complete_plan()
        self.assertEqual(self.current()['status'], 'proposed')
        self.assertEqual(self.current()['status_automation'], 0)
        self.assertFalse(self.store.reconcile_book_statuses(1))
        self.assertEqual([(row['action'], row['actor_id']) for row in self.audit()], [('manual', 99)])

    def test_manual_read_is_terminal_even_if_automation_is_reenabled(self):
        self.store.update_book(1, self.book, status='read')
        self.store.set_book_status_automation(1, self.book, True)
        self.observe(self.meeting(), 'active')
        self.assertEqual(self.current()['status'], 'read')

    def test_manual_opt_in_waits_for_new_lifecycle_edge_not_replayed_history(self):
        meeting = self.meeting()
        self.store.update_book(1, self.book, status='queued')
        self.observe(meeting, 'active')
        enabled = self.store.set_book_status_automation(1, self.book, True, actor_id=99)
        self.assertEqual(enabled['status_automation_pending'], 1)
        self.assertEqual(enabled['status'], 'queued')
        self.store = Store(self.path, clock=lambda: self.now)
        self.observe(meeting, 'active')
        self.store.reconcile_book_statuses(1)
        self.assertEqual(self.current()['status'], 'queued')
        self.observe(meeting, 'completed')
        self.assertEqual(self.current()['status'], 'reading')
        self.assertEqual(self.current()['status_automation_pending'], 0)

    def test_new_schedule_and_changed_dates_do_not_release_opt_in_pending(self):
        self.store.update_book(1, self.book, status='queued')
        self.store.set_book_status_automation(1, self.book, True)
        meeting = self.meeting()
        self.observe(meeting, 'scheduled')
        self.observe(meeting, 'scheduled', start=self.now + 800, end=self.now + 900)
        self.assertEqual(self.current()['status_automation_pending'], 1)
        self.assertEqual(self.current()['status'], 'queued')

    def test_rest_404_between_replayed_active_snapshots_does_not_release_opt_in_pending(self):
        self.store.update_book(1, self.book, status='queued')
        meeting = self.meeting()
        self.observe(meeting, 'active')
        self.store.set_book_status_automation(1, self.book, True)
        self.observe(meeting, 'cancelled', status_confirmed=False)
        self.store = Store(self.path, clock=lambda: self.now)
        self.observe(meeting, 'active')
        self.assertEqual(self.current()['status'], 'queued')
        self.assertEqual(self.current()['status_automation_pending'], 1)
        self.observe(meeting, 'completed')
        self.assertEqual(self.current()['status'], 'reading')

    def test_manual_edit_of_metadata_or_plan_does_not_pause_automation(self):
        self.store.update_book(1, self.book, title='Другое имя', author='Тот же автор', reading_meetings=1)
        self.observe(self.meeting(), 'completed')
        self.observe(self.meeting('essay'), 'completed')
        self.assertEqual(self.current()['status'], 'read')

    def test_setting_smaller_plan_does_not_immediately_rewrite_status(self):
        self.observe(self.meeting(), 'completed')
        self.observe(self.meeting('essay'), 'completed')
        self.assertEqual(self.current()['status'], 'reading')
        self.store.update_book(1, self.book, reading_meetings=1)
        self.assertEqual(self.current()['status'], 'reading')

    def test_other_current_book_is_never_displaced_and_retry_after_release_works(self):
        other = self.store.create_book(1, 'Другая', 'Автор', '', 'other')['id']
        self.store.update_book(1, other, status='reading')
        meeting = self.meeting()
        self.observe(meeting, 'active')
        self.assertEqual(self.current()['status'], 'proposed')
        self.assertEqual(self.current(other)['status'], 'reading')
        self.store.update_book(1, other, status='read')
        self.assertTrue(self.store.reconcile_book_statuses(1))
        self.assertEqual(self.current()['status'], 'reading')

    def test_reconciliation_completes_current_before_starting_next_in_one_pass(self):
        # Current has a later queue position than the new book; a one-pass
        # position-ordered loop would wait until the next tick to start it.
        other = self.store.create_book(1, 'Текущее', 'Автор', '', 'other')['id']
        self.store.update_book(1, other, reading_meetings=1)
        old_first, old_essay = self.meeting(book=other), self.meeting('essay', book=other)
        self.observe(old_first, 'active')
        self.observe(self.meeting(), 'active')
        with self.store.tx() as db:
            for meeting in (old_first, old_essay):
                db.execute("UPDATE bc_meetings SET event_id=?,voice_id=15,status='completed' WHERE id=?",
                           (int(meeting['request_key']), meeting['id']))
        self.assertTrue(self.store.reconcile_book_statuses(1))
        self.assertEqual(self.current(other)['status'], 'read')
        self.assertEqual(self.current()['status'], 'reading')

    def test_toggle_and_role_changes_use_scoped_revision_guards_and_audit(self):
        before = self.current()
        self.store.set_book_status_automation(1, self.book, False, expected_revision=before['revision'], actor_id=99)
        with self.assertRaises(ClubError):
            self.store.set_book_status_automation(1, self.book, True, expected_revision=before['revision'])
        self.store.configure(2, CONFIG)
        with self.assertRaises(ClubError):
            self.store.set_book_status_automation(2, self.book, True)
        meeting = self.meeting()
        changed = self.store.set_meeting_plan_kind(1, meeting['id'], 'essay', expected_revision=meeting['revision'], actor_id=99)
        self.assertEqual(changed['plan_kind'], 'essay')
        with self.assertRaises(ClubError):
            self.store.set_meeting_plan_kind(1, meeting['id'], 'reading', expected_revision=meeting['revision'])
        with self.assertRaises(ClubError):
            self.store.set_meeting_plan_kind(2, meeting['id'], 'reading')
        audit = self.store.one('SELECT * FROM bc_meeting_plan_audit')
        self.assertEqual((audit['old_kind'], audit['new_kind'], audit['actor_id']), ('reading', 'essay', 99))

    def test_invalid_role_or_toggle_does_not_change_data(self):
        meeting = self.meeting()
        for value in ('Эссе', '', None, []):
            with self.subTest(value=value), self.assertRaises(ClubError):
                self.store.set_meeting_plan_kind(1, meeting['id'], value)
        for value in (1, 0, 'true', None):
            with self.subTest(value=value), self.assertRaises(ClubError):
                self.store.set_book_status_automation(1, self.book, value)
        self.assertEqual(self.audit(), [])

    def test_new_single_and_batch_books_enable_rule_and_request_retry_is_noop(self):
        self.assertEqual(self.current()['status_automation'], 1)
        created, _ = self.store.create_books(1, [{'title': 'Пакет', 'author': 'Автор'}], 'batch')
        self.assertEqual(created[0]['status_automation'], 1)
        self.store.update_book(1, self.book, status='proposed')
        duplicate = self.store.create_book(1, 'Книга', 'Автор', '', 'book')
        self.assertEqual(duplicate['status_automation'], 0)
        self.assertEqual(self.store.create_books(1, [{'title': 'Пакет', 'author': 'Автор'}], 'batch')[0], [])

    def test_v9_migration_preserves_existing_statuses_import_history_and_quota(self):
        meeting = self.meeting()
        self.observe(meeting, 'active')
        cancelled = self.meeting()
        self.observe(cancelled, 'cancelled')
        with self.store.tx() as db:
            db.execute("INSERT INTO bc_import_budgets VALUES('legacy-essays-v1',1,30714)")
            db.execute("""INSERT INTO bc_import_runs(id,guild_id,actor_id,source_channel_id,budget_id,
              request_key,reserved_tokens,state,snapshot,created_at,updated_at)
              VALUES('done-run',1,99,41,'legacy-essays-v1','import',30714,'done','{}',1,1)""")
            db.execute("INSERT INTO bc_import_sources VALUES(1,42,'done-run','essay',43)")
            db.execute('ALTER TABLE bc_books DROP COLUMN status_automation')
            db.execute('ALTER TABLE bc_books DROP COLUMN status_automation_pending')
            db.execute('ALTER TABLE bc_meetings DROP COLUMN plan_kind')
            db.execute('ALTER TABLE bc_meetings DROP COLUMN event_status_confirmed')
            db.execute('ALTER TABLE bc_meetings DROP COLUMN last_confirmed_status')
            db.execute('DELETE FROM bc_migrations WHERE version=10')
        snapshots = {table: self.store.rows('SELECT * FROM ' + table) for table in
                     ('bc_import_runs', 'bc_import_sources', 'bc_import_budgets')}
        migrated = Store(self.path, clock=lambda: self.now)
        book = migrated.book(1, self.book)
        self.assertEqual((book['status'], book['status_automation']), ('reading', 0))
        self.assertIsNone(migrated.meeting(1, meeting['id'])['plan_kind'])
        self.assertEqual(migrated.meeting(1, cancelled['id'])['event_status_confirmed'], 0)
        self.assertFalse(migrated.reconcile_book_statuses(1))
        for table, rows in snapshots.items():
            self.assertEqual(migrated.rows('SELECT * FROM ' + table), rows)
        restarted = Store(self.path, clock=lambda: self.now)
        self.assertEqual(restarted.book(1, self.book), book)
        self.assertEqual(restarted.one('SELECT MAX(version) AS version FROM bc_migrations')['version'], 11)


class AutomaticBookStatusDiscordTests(AutomaticBookStatusFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.h = WebhookHarness()
        self.service = Service(self.h.bot, self.store)

    def event(self, meeting, status):
        return SimpleNamespace(id=int(meeting['request_key']), guild_id=1, name=meeting['name'],
                               start_time=datetime.fromtimestamp(self.now + 100, timezone.utc),
                               end_time=datetime.fromtimestamp(self.now + 200, timezone.utc),
                               status=status, entity_type=discord.EntityType.voice, channel_id=15,
                               description='')

    async def test_rest_404_preserves_completed_and_never_assumes_active_is_completed(self):
        completed, active = self.meeting(), self.meeting()
        self.observe(completed, 'completed')
        self.observe(active, 'active')
        self.h.guild.fetch_scheduled_event = AsyncMock(side_effect=not_found())
        self.assertFalse(await self.service.sync_one(self.h.guild, self.store.meeting(1, completed['id'])))
        self.assertEqual(self.store.meeting(1, completed['id'])['status'], 'completed')
        await self.service.sync_one(self.h.guild, self.store.meeting(1, active['id']))
        self.assertEqual(self.store.meeting(1, active['id'])['status'], 'cancelled')
        self.assertEqual(self.store.meeting(1, active['id'])['event_status_confirmed'], 0)
        self.assertEqual(self.current()['status'], 'reading')

    async def test_restart_retries_unconfirmed_missing_event_before_finishing_plan(self):
        first, second, essay = self.meeting(), self.meeting(), self.meeting('essay')
        self.observe(first, 'completed')
        self.observe(second, 'completed')
        self.observe(essay, 'active')
        self.h.guild.fetch_scheduled_event = AsyncMock(side_effect=not_found())
        await self.service.sync_one(self.h.guild, self.store.meeting(1, essay['id']))
        self.assertEqual(self.current()['status'], 'reading')
        self.store = Store(self.path, clock=lambda: self.now)
        self.service = Service(self.h.bot, self.store)
        self.h.guild.fetch_scheduled_events = AsyncMock(return_value=[])
        self.h.guild.fetch_scheduled_event = AsyncMock(return_value=self.event(essay, discord.EventStatus.completed))
        await self.service.reconcile(self.h.guild)
        self.h.guild.fetch_scheduled_event.assert_awaited_once_with(int(essay['request_key']))
        self.assertEqual(self.current()['status'], 'read')

    async def test_restart_reconcile_uses_completed_cache_when_rest_no_longer_lists_events(self):
        first, second, essay = self.meeting(), self.meeting(), self.meeting('essay')
        self.observe(first, 'active')
        with self.store.tx() as db:
            for meeting in (first, second, essay):
                db.execute("UPDATE bc_meetings SET event_id=?,voice_id=15,status='completed' WHERE id=?",
                           (int(meeting['request_key']), meeting['id']))
        self.store = Store(self.path, clock=lambda: self.now)
        self.service = Service(self.h.bot, self.store)
        self.assertTrue(await self.service.reconcile(self.h.guild))
        self.assertEqual(self.current()['status'], 'read')
        self.h.guild.fetch_scheduled_event.assert_not_awaited()
        self.assertFalse(await self.service.reconcile(self.h.guild))

    async def test_real_lifecycle_sync_reuses_existing_tags_and_publication_ids(self):
        self.store.set_published(1)
        forum = self.h.channels[13]
        labels = {}
        for index, (kind, name) in enumerate(TEMPLATES['books'].items(), 1):
            labels[kind] = tag(13000 + index, name)
            self.store.bind_tag(1, 13, kind, labels[kind].id)
        forum.available_tags = list(labels.values())
        await self.service.refresh(self.h.guild)
        root = self.store.publication(f'book:{self.book}')
        thread = self.h.channels[root['channel_id']]
        before_ids = {key: self.store.publication(key)['message_id'] for key in
                      (f'book:{self.book}', 'catalog:1')}
        first, second, essay = self.meeting(), self.meeting(), self.meeting('essay')
        self.service.sync(self.h.guild, first, self.event(first, discord.EventStatus.active))
        await self.service.refresh(self.h.guild)
        self.assertEqual({value.id for value in thread.applied_tags}, {labels['reading'].id})
        for meeting in (first, second, essay):
            self.service.sync(self.h.guild, meeting, self.event(meeting, discord.EventStatus.completed))
        await self.service.refresh(self.h.guild)
        self.assertEqual(self.current()['status'], 'read')
        self.assertEqual({value.id for value in thread.applied_tags}, {labels['read'].id})
        forum.edit.assert_not_awaited()
        self.assertEqual(forum.create_thread.await_count, 2)
        self.assertEqual({key: self.store.publication(key)['message_id'] for key in before_ids}, before_ids)


if __name__ == '__main__':
    unittest.main()

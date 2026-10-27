"""Book plans are metadata, independent of scheduled Discord meetings."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from bookclub.render import book_pages
from bookclub.store import ClubError, Store
from bookclub.ui import BookView, Club, MeetingView
from test_bookclub import ClubFixture, CONFIG
from test_webhook_discord import WebhookHarness


class BookPlanTests(ClubFixture, unittest.TestCase):
    def test_v7_upgrade_preserves_existing_book_calendar_and_legacy_data(self):
        before_meetings = self.store.rows('SELECT * FROM bc_meetings')
        before_book = self.store.book(1, self.book['id'])
        with self.store.tx() as db:
            db.execute('CREATE TABLE legacy(note TEXT)')
            db.execute("INSERT INTO legacy VALUES('keep')")
            db.execute('PRAGMA user_version=27')
            db.execute('ALTER TABLE bc_books DROP COLUMN reading_meetings')
            db.execute('DELETE FROM bc_migrations WHERE version=8')
        migrated = Store(self.path, clock=lambda: self.now)
        self.assertEqual(migrated.book(1, self.book['id']), before_book)
        self.assertEqual(migrated.rows('SELECT * FROM bc_meetings'), before_meetings)
        self.assertEqual(migrated.one('PRAGMA user_version')['user_version'], 27)
        self.assertEqual(migrated.one('SELECT note FROM legacy')['note'], 'keep')
        self.assertEqual(Store(self.path).rows('SELECT version FROM bc_migrations ORDER BY version'),
                         [{'version': value} for value in range(1, 14)])

    def test_plan_change_and_clear_do_not_create_or_cancel_meetings(self):
        self.action('volunteer')
        self.save_plan(topics='Private preparation', ready=True)
        snapshots = {table: self.store.rows('SELECT * FROM ' + table) for table in
                     ('bc_meetings', 'bc_plans', 'bc_participants', 'bc_publications', 'bc_import_sources')}
        meeting_jobs = self.jobs()
        for count in (4, 2, 100, None):
            self.store.update_book(1, self.book['id'], reading_meetings=count)
            self.assertEqual(self.store.book(1, self.book['id'])['reading_meetings'], count)
            for table, before in snapshots.items():
                self.assertEqual(self.store.rows('SELECT * FROM ' + table), before)
            self.assertEqual(self.jobs(), meeting_jobs)
        restored = Store(self.path, clock=lambda: self.now)
        self.assertIsNone(restored.book(1, self.book['id'])['reading_meetings'])

    def test_plan_validation_and_guild_scope(self):
        original = self.store.book(1, self.book['id'])
        for value in (True, False, 0, -1, 101, 2.5, '3'):
            with self.subTest(value=value), self.assertRaises(ClubError):
                self.store.update_book(1, self.book['id'], reading_meetings=value)
            self.assertEqual(self.store.book(1, self.book['id']), original)
        self.store.configure(2, CONFIG)
        with self.assertRaises(ClubError):
            self.store.update_book(2, self.book['id'], reading_meetings=3)

    def test_concurrent_forms_cannot_overwrite_newer_book_revision(self):
        revision = self.store.book(1, self.book['id'])['revision']
        def change(count):
            try:
                self.store.update_book(1, self.book['id'], expected_revision=revision, reading_meetings=count)
                return True
            except ClubError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(change, (3, 4)))
        self.assertEqual(sorted(results), [False, True])
        current = self.store.book(1, self.book['id'])
        with self.assertRaises(ClubError):
            self.store.update_book(1, self.book['id'], expected_revision=revision, title='Stale title')
        self.assertEqual(self.store.book(1, self.book['id']), current)

    def test_card_displays_reading_plus_one_essay_discussion(self):
        self.store.update_book(1, self.book['id'], reading_meetings=3)
        book = self.store.book(1, self.book['id'])
        text = '\n'.join(book_pages(self.store, book, self.store.settings(1)))
        self.assertIn('3 встречи по книге + обсуждение эссе', text)
        self.assertNotIn('Private preparation', text)


class BookControlWiringTests(ClubFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.h = WebhookHarness()
        self.cog = Club(self.h.bot, self.store)

    async def test_book_and_meeting_controls_survive_restart(self):
        with patch.object(self.cog.worker, 'start'):
            await self.cog.cog_load()
        views = [call.args[0] for call in self.h.bot.add_view.call_args_list]
        book = next(view for view in views if isinstance(view, BookView))
        meeting = next(view for view in views if isinstance(view, MeetingView))
        for view in (book, meeting):
            self.assertTrue(view.is_persistent())
            self.assertTrue(all(len(child.custom_id) <= 100 for child in view.children))
        self.assertTrue(any(button.label == 'Управление книгой' for button in book.children))
        self.assertTrue(any(button.label == 'Встречи' for button in book.children))
        self.assertTrue(any(button.label == 'Управление встречей' for button in meeting.children))

    async def test_public_buttons_route_current_ids_to_private_controls(self):
        interaction = self.h.interaction(99)
        with patch('bookclub.book_controls.open_book_controls', new_callable=AsyncMock) as book_panel, \
             patch('bookclub.meeting_controls.open_book_meetings', new_callable=AsyncMock) as meetings_panel, \
             patch('bookclub.meeting_controls.open_meeting_controls', new_callable=AsyncMock) as meeting_panel:
            for label in ('Управление книгой', 'Встречи'):
                button = next(item for item in BookView(self.cog, self.book).children if item.label == label)
                await button.callback(interaction)
            button = next(item for item in MeetingView(self.cog, self.meeting).children if item.label == 'Управление встречей')
            await button.callback(interaction)
            book_panel.assert_awaited_once_with(self.cog, interaction, self.book['id'])
            meetings_panel.assert_awaited_once_with(self.cog, interaction, self.book['id'])
            meeting_panel.assert_awaited_once_with(self.cog, interaction, self.meeting['id'])

    async def test_existing_book_edit_command_sets_and_clears_n_plus_one_plan(self):
        interaction = self.h.interaction(99)
        ctx = SimpleNamespace(guild=self.h.guild, author=self.h.members[99], interaction=interaction)
        with patch.object(self.cog.service, 'refresh', new_callable=AsyncMock):
            await Club.book_edit.callback(self.cog, ctx, self.book['id'], reading_meetings='3')
            self.assertEqual(self.store.book(1, self.book['id'])['reading_meetings'], 3)
            for invalid in ('0', '101', '3.5'):
                with self.subTest(invalid=invalid), self.assertRaises(ClubError):
                    await Club.book_edit.callback(self.cog, ctx, self.book['id'], reading_meetings=invalid)
            await Club.book_edit.callback(self.cog, ctx, self.book['id'], reading_meetings='-')
            self.assertIsNone(self.store.book(1, self.book['id'])['reading_meetings'])
        self.h.guild.create_scheduled_event.assert_not_awaited()

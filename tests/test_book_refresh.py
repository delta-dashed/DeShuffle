"""Saved edits get priority without overlapping Discord publication writers."""
import asyncio
from datetime import datetime, timezone
from time import perf_counter
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from bookclub.book_controls import BookControlsView, BookDetailsModal
from bookclub.book_trash_ui import RemoveBookConfirmation, RemovedBookView, RestoreBookConfirmation
from bookclub.forum_tags import TEMPLATES
from bookclub.store import ClubError
from bookclub.ui import BookView, Club
from test_bookclub import ClubFixture
from test_bookclub_discord import DiscordHarness


class BookRefreshTests(ClubFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.h = DiscordHarness()
        self.cog = Club(self.h.bot, self.store)
        self.service = self.cog.service
        self.addAsyncCleanup(self.service.close)
        meeting = self.meeting
        self.h.event(meeting['event_id'], name=meeting['name'],
                     start_time=datetime.fromtimestamp(meeting['start'], timezone.utc),
                     end_time=datetime.fromtimestamp(meeting['end'], timezone.utc))
        forum = self.h.channels[13]
        for ident, purpose in enumerate(TEMPLATES['books'], 200):
            forum.available_tags.append(SimpleNamespace(id=ident, name=purpose))
            self.store.bind_tag(1, 13, purpose, ident)

    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 1.0

    def current(self, book_id=None):
        return self.store.book(1, book_id or self.book['id'])

    def root_message(self, book_id=None):
        pub = self.store.publication(f'book:{book_id or self.book["id"]}')
        return self.h.channels[pub['channel_id']].messages[pub['message_id']]

    def more_books(self, total=13):
        for index in range(1, total):
            self.store.create_book(1, f'Книга {index}', 'Автор', '', f'book-{index}')
        return self.store.books(1)

    async def publish(self):
        self.store.set_published(1)
        async with self.service.locks[1]:
            await self.service.refresh(self.h.guild)

    async def finish_queue(self):
        task = self.service.book_updates.tasks.get(1)
        if task:
            await asyncio.wait_for(asyncio.shield(task), timeout=5)

    async def test_burst_coalesces_same_book_and_catalog(self):
        await self.publish()
        self.service.refresh_book = AsyncMock(wraps=self.service.refresh_book)
        self.service.refresh_catalog = AsyncMock(wraps=self.service.refresh_catalog)
        self.store.update_book(1, self.book['id'], title='Последняя версия')
        for _ in range(30):
            self.service.request_book_refresh(self.h.guild, self.book['id'])
        await self.finish_queue()
        self.service.refresh_book.assert_awaited_once_with(self.h.guild, self.book['id'])
        self.service.refresh_catalog.assert_awaited_once_with(self.h.guild)
        self.assertIn('Последняя версия', self.root_message().content)
        self.assertFalse(self.service.book_updates.pending)

    async def test_edit_during_message_update_republishes_latest_status_without_duplicates(self):
        await self.publish()
        message = self.root_message()
        edit = message.edit.side_effect
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_first_edit(**kwargs):
            if not started.is_set():
                started.set()
                await release.wait()
            return await edit(**kwargs)

        message.edit.side_effect = slow_first_edit
        self.service.refresh_book = AsyncMock(wraps=self.service.refresh_book)
        self.store.update_book(1, self.book['id'], status='reading')
        self.service.request_book_refresh(self.h.guild, self.book['id'])
        await asyncio.wait_for(started.wait(), timeout=1)
        self.store.update_book(1, self.book['id'], status='read', title='Итоговое название')
        self.service.request_book_refresh(self.h.guild, self.book['id'])
        release.set()
        await self.finish_queue()
        self.assertEqual(self.service.refresh_book.await_count, 2)
        self.assertIs(self.root_message(), message)
        self.assertIn('Итоговое название', message.content)
        self.assertIn('Прочитано', message.content)
        desired = self.service.forum_tags.bindings(1, self.h.channels[13])['read']
        self.assertEqual({tag.id for tag in message.channel.applied_tags}, {desired})
        self.assertEqual(self.h.channels[13].create_thread.await_count, 2)

    async def test_targeted_refresh_skips_other_twelve_books_meetings_rules_and_members(self):
        books = self.more_books()
        await self.publish()
        before_ids = [(row['key'], row['message_id']) for row in self.store.rows('SELECT * FROM bc_publications')]
        for channel in self.h.channels.values():
            channel.fetch_message.reset_mock()
        self.h.guild.fetch_member.reset_mock()
        self.h.bot.fetch_channel.reset_mock()
        self.h.guild.active_threads.reset_mock()
        self.h.channels[13].create_thread.reset_mock()
        target = books[-1]
        self.store.update_book(1, target['id'], title='Исправлено без обхода архива')
        self.service.request_book_refresh(self.h.guild, target['id'])
        await self.finish_queue()
        self.assertEqual(self.root_message(target['id']).channel.fetch_message.await_count, 1)
        for book in books[:-1]:
            self.root_message(book['id']).channel.fetch_message.assert_not_awaited()
        for channel_id in (11, 12, 14):
            self.h.channels[channel_id].fetch_message.assert_not_awaited()
        self.h.guild.fetch_member.assert_not_awaited()
        self.h.bot.fetch_channel.assert_not_awaited()
        self.h.guild.active_threads.assert_not_awaited()
        self.h.channels[13].create_thread.assert_not_awaited()
        self.assertEqual([(row['key'], row['message_id']) for row in self.store.rows('SELECT * FROM bc_publications')],
                         before_ids)

    async def test_priority_drain_overtakes_full_refresh_and_later_pass_uses_latest_book(self):
        books = self.more_books()
        await self.publish()
        first, target = books[0], books[-1]
        started, release = asyncio.Event(), asyncio.Event()
        channel = self.root_message(first['id']).channel
        fetch = channel.fetch_message.side_effect

        async def slow_first_fetch(ident):
            if not started.is_set():
                started.set()
                await release.wait()
            return await fetch(ident)

        channel.fetch_message.side_effect = slow_first_fetch
        calls = []
        refresh_book = self.service.refresh_book

        async def observe_refresh(guild, book_id):
            calls.append(book_id)
            return await refresh_book(guild, book_id)

        self.service.refresh_book = observe_refresh

        async def periodic_refresh():
            async with self.service.locks[1]:
                await self.service.refresh(self.h.guild)

        periodic = asyncio.create_task(periodic_refresh())
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            self.assertTrue(self.service.locks[1].locked())
            self.store.update_book(1, target['id'], title='Сохранено во время обхода', status='read')
            self.service.request_book_refresh(self.h.guild, target['id'])
            release.set()
            await asyncio.wait_for(periodic, timeout=5)
            await self.finish_queue()
        finally:
            release.set()
            if not periodic.done():
                periodic.cancel()
                await asyncio.gather(periodic, return_exceptions=True)
        self.assertEqual(calls[:2], [first['id'], target['id']])
        self.assertEqual(calls.count(target['id']), 2)
        self.assertIn('Сохранено во время обхода', self.root_message(target['id']).content)
        self.assertIn('Прочитано', self.root_message(target['id']).content)
        self.assertEqual(self.current(target['id'])['status'], 'read')

    async def test_same_guild_lock_serializes_with_publication_repair(self):
        self.store.set_published(1)
        self.service.refresh_book = AsyncMock()
        self.service.refresh_catalog = AsyncMock()
        async with self.service.locks[1]:
            self.service.request_book_refresh(self.h.guild, self.book['id'])
            await asyncio.sleep(0)
            self.service.refresh_book.assert_not_awaited()
            self.assertFalse(self.service.book_updates.tasks[1].done())
        await self.finish_queue()
        self.service.refresh_book.assert_awaited_once_with(self.h.guild, self.book['id'])

    async def test_lost_edit_response_keeps_saved_state_and_full_refresh_recovers_same_id(self):
        await self.publish()
        message = self.root_message()
        edit = message.edit.side_effect

        async def lost_response(**kwargs):
            await edit(**kwargs)
            raise OSError('Private book content must not be logged')

        message.edit.side_effect = lost_response
        self.store.update_book(1, self.book['id'], status='read')
        with self.assertLogs('bookclub.book_refresh', level='WARNING') as captured:
            self.service.request_book_refresh(self.h.guild, self.book['id'])
            await self.finish_queue()
        self.assertNotIn('Private book content', '\n'.join(captured.output))
        self.assertEqual(self.current()['status'], 'read')
        self.assertEqual(self.service.book_updates.failures[(1, self.book['id'])], 'OSError')
        message.edit.side_effect = edit
        await self.publish()
        self.assertIs(self.root_message(), message)
        self.assertIn('Прочитано', message.content)
        self.assertNotIn((1, self.book['id']), self.service.book_updates.failures)
        self.assertEqual(self.h.channels[13].create_thread.await_count, 2)

    async def test_shutdown_cancels_worker_and_restart_reconciles_durable_edit(self):
        await self.publish()
        message = self.root_message()
        edit = message.edit.side_effect
        started = asyncio.Event()

        async def blocked_edit(**kwargs):
            started.set()
            await asyncio.Event().wait()

        message.edit.side_effect = blocked_edit
        self.store.update_book(1, self.book['id'], status='read')
        self.service.request_book_refresh(self.h.guild, self.book['id'])
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.wait_for(self.service.close(), timeout=1)
        self.assertFalse(self.service.locks[1].locked())
        self.assertFalse(self.service.book_updates.tasks)
        self.assertEqual(self.current()['status'], 'read')
        message.edit.side_effect = edit
        restarted = Club(self.h.bot, self.store).service
        self.addAsyncCleanup(restarted.close)
        async with restarted.locks[1]:
            await restarted.refresh(self.h.guild)
        self.assertIs(self.root_message(), message)
        self.assertIn('Прочитано', message.content)
        self.assertEqual(self.h.channels[13].create_thread.await_count, 2)

    async def test_unpublished_or_disabled_club_does_not_publish_queued_updates(self):
        self.service.refresh_book = AsyncMock()
        self.service.refresh_catalog = AsyncMock()
        self.service.request_book_refresh(self.h.guild, self.book['id'])
        await self.finish_queue()
        self.store.set_published(1)
        self.service.guild_ids = {2}
        self.service.request_book_refresh(self.h.guild, self.book['id'])
        await self.finish_queue()
        self.service.refresh_book.assert_not_awaited()
        self.service.refresh_catalog.assert_not_awaited()

    async def test_thirteen_book_transport_cost_is_limited_to_changed_book_and_catalog(self):
        books = self.more_books()
        await self.publish()
        count = [0]
        for channel in self.h.channels.values():
            fetch = channel.fetch_message.side_effect

            async def delayed_fetch(ident, original=fetch):
                count[0] += 1
                await asyncio.sleep(0.1)
                return await original(ident)

            channel.fetch_message.side_effect = delayed_fetch
        start = perf_counter()
        await self.publish()
        full_elapsed, full_count = perf_counter() - start, count[0]
        count[0] = 0
        self.store.update_book(1, books[-1]['id'], title='Измерение точечного обновления')
        start = perf_counter()
        self.service.request_book_refresh(self.h.guild, books[-1]['id'])
        await self.finish_queue()
        targeted_elapsed, targeted_count = perf_counter() - start, count[0]
        self.assertGreaterEqual(full_count, 14)
        self.assertLessEqual(targeted_count, 3)
        print(f'\nMock GET=100 ms, 13 books: full={full_elapsed * 1000:.0f} ms/{full_count} GET; '
              f'targeted={targeted_elapsed * 1000:.0f} ms/{targeted_count} GET')

    def remove(self, book_id=None):
        book = self.current(book_id)
        return self.store.remove_book(1, book['id'], expected_revision=book['revision'], actor_id=99)

    def interaction(self):
        interaction = self.h.interaction(99)
        interaction.edit_original_response = AsyncMock()
        return interaction

    def archived_essay(self):
        thread = self.h.channel(8800, parent_id=14, owner_id=1, name='Сохранённое эссе')
        message = thread.messages[thread.id]
        message.attachments = [SimpleNamespace(id=8801, filename='essay.pdf', size=123)]
        self.store.register_essay(1, self.book['id'], thread.id, thread.id, 1, thread.name, thread.jump_url)
        # This is a disposable test ledger, with no importer or model invocation.
        with self.store.tx() as db:
            db.execute("INSERT INTO bc_import_budgets VALUES('finished-budget',1,30000)")
            db.execute('''INSERT INTO bc_import_runs
              (id,guild_id,actor_id,source_channel_id,budget_id,request_key,reserved_tokens,state,
               snapshot,plan,created_at,updated_at) VALUES(?,1,99,77,?,?,30000,'done','{}','{}',?,?)''',
              ('finished-run', 'finished-budget', 'finished-request', self.now, self.now))
            db.execute("INSERT INTO bc_import_sources VALUES(1,12345,'finished-run','finished-item',8800)")
        return message

    def archive_state(self):
        return {table: self.store.rows(f'SELECT * FROM {table}') for table in (
            'bc_essays', 'bc_import_sources', 'bc_import_runs', 'bc_import_budgets')}

    async def test_remove_and_restore_ui_keep_publication_ids_attachments_and_import_history(self):
        essay = self.archived_essay()
        await self.publish()
        message = self.root_message()
        message.attachments = [SimpleNamespace(id=8900, filename='book.epub', size=456)]
        attachments = list(message.attachments)
        archive = self.archive_state()
        ids = {row['key']: row['message_id'] for row in self.store.rows('SELECT * FROM bc_publications')}
        tags = list(message.channel.applied_tags)
        interaction = self.interaction()
        confirmation = RemoveBookConfirmation(self.cog, self.current(), 99)
        async with self.service.locks[1]:
            await asyncio.wait_for(confirmation.confirm.callback(interaction), timeout=1)
            self.assertTrue(self.current()['deleted'])
            interaction.edit_original_response.assert_awaited_once()
        await self.finish_queue()
        self.assertIs(self.root_message(), message)
        self.assertIn('Книга удалена из каталога', message.content)
        self.assertIsInstance(message.edit.await_args.kwargs['view'], RemovedBookView)
        self.assertEqual(message.attachments, attachments)
        self.assertEqual(message.channel.applied_tags, tags)
        self.assertEqual(self.archive_state(), archive)
        self.assertEqual(essay.attachments[0].filename, 'essay.pdf')
        essay.edit.assert_not_awaited()
        essay.delete.assert_not_awaited()
        catalog = self.store.publication('catalog:1')
        catalog_messages = self.h.channels[catalog['channel_id']].messages.values()
        self.assertNotIn(message.channel.jump_url, '\n'.join(item.content for item in catalog_messages))
        restored = self.interaction()
        await RestoreBookConfirmation(self.cog, self.current(), 99).confirm.callback(restored)
        await self.finish_queue()
        self.assertFalse(self.current()['deleted'])
        self.assertIs(self.root_message(), message)
        self.assertIsInstance(message.edit.await_args.kwargs['view'], BookView)
        self.assertEqual(message.attachments, attachments)
        self.assertEqual(self.archive_state(), archive)
        self.assertEqual({row['key']: row['message_id'] for row in self.store.rows('SELECT * FROM bc_publications')}, ids)
        self.assertIn(message.channel.jump_url, '\n'.join(item.content for item in catalog_messages))
        self.h.events[self.meeting['event_id']].cancel.assert_not_awaited()

    async def test_never_published_removed_book_is_not_created_by_queue_or_restart(self):
        self.remove()
        self.store.set_published(1)
        self.service.request_book_refresh(self.h.guild, self.book['id'])
        await self.finish_queue()
        restarted = Club(self.h.bot, self.store).service
        self.addAsyncCleanup(restarted.close)
        async with restarted.locks[1]:
            await restarted.refresh(self.h.guild)
        self.assertIsNone(self.store.publication(f'book:{self.book["id"]}'))
        self.assertEqual(self.h.channels[13].create_thread.await_count, 1)  # Catalog only.

    async def test_removed_book_missing_root_is_not_recreated(self):
        await self.publish()
        message = self.root_message()
        publication = self.store.publication(f'book:{self.book["id"]}')
        self.remove()
        del message.channel.messages[message.id]
        self.service.request_book_refresh(self.h.guild, self.book['id'])
        await self.finish_queue()
        await self.publish()
        del self.h.channels[message.channel.id]
        restarted = Club(self.h.bot, self.store).service
        self.addAsyncCleanup(restarted.close)
        async with restarted.locks[1]:
            await restarted.refresh(self.h.guild)
        self.assertEqual(self.store.publication(publication['key']), publication)
        self.assertEqual(self.h.channels[13].create_thread.await_count, 2)

    async def test_restart_finishes_removal_and_skips_removed_participants_events_and_essays(self):
        essay = self.archived_essay()
        await self.publish()
        message = self.root_message()
        self.remove()
        archive = self.archive_state()
        meeting = self.store.meeting(1, self.meeting['id'])
        # An event changed externally while this book was removed. Reconciliation
        # must not mutate its stored lifecycle or reactivate book automation.
        self.h.events[self.meeting['event_id']].name = 'Внешнее изменение события'
        self.h.guild.fetch_member.reset_mock()
        self.h.guild.fetch_scheduled_event.reset_mock()
        essay.channel.fetch_message.reset_mock()
        restarted = Club(self.h.bot, self.store).service
        self.addAsyncCleanup(restarted.close)
        async with restarted.locks[1]:
            self.assertEqual(await restarted.live_participants(self.h.guild), set())
            await restarted.reconcile(self.h.guild)
            await restarted.check_essays(self.h.guild)
            await restarted.refresh(self.h.guild)
        self.assertIn('Книга удалена из каталога', message.content)
        self.assertIsInstance(message.edit.await_args.kwargs['view'], RemovedBookView)
        self.assertEqual(self.store.meeting(1, self.meeting['id']), meeting)
        self.assertEqual(self.archive_state(), archive)
        self.h.guild.fetch_member.assert_not_awaited()
        self.h.guild.fetch_scheduled_event.assert_not_awaited()
        essay.channel.fetch_message.assert_not_awaited()

    async def test_stale_book_controls_and_essay_button_cannot_modify_removed_book(self):
        await self.publish()
        public_view = BookView(self.cog, self.current())
        controls = BookControlsView(self.cog, self.current(), 99)
        controls.status._values = ['read']
        modal = BookDetailsModal(self.cog, self.current(), 99)
        modal.fields['title']._value = 'Не должно сохраниться'
        self.remove()
        before = self.current()
        for action in (controls.change_status, modal.on_submit, public_view.children[0].callback):
            with self.subTest(action=action):
                with self.assertRaisesRegex(ClubError, 'удалена'):
                    await action(self.interaction())
        self.assertEqual(self.current(), before)
        self.h.channels[14].create_thread.assert_not_awaited()
        self.assertFalse(self.service.book_updates.tasks)

    async def test_removal_during_member_fetch_stops_new_notification_send(self):
        job = next(job for job in self.jobs(self.meeting) if job['kind'] == 'participants')
        self.now = job['due']
        self.assertTrue(self.store.claim_job(job['key']))
        original_fetch = self.h.guild.fetch_member.side_effect

        async def remove_before_returning_member(user_id):
            member = await original_fetch(user_id)
            self.remove()
            return member

        self.h.guild.fetch_member.side_effect = remove_before_returning_member
        await self.service.deliver(self.h.guild, job)
        self.h.guild.fetch_member.assert_awaited_once()
        for member in self.h.members.values():
            member.send.assert_not_awaited()
        self.assertTrue(self.current()['deleted'])

    async def test_removed_archived_book_card_keeps_archive_and_attachments(self):
        await self.publish()
        message = self.root_message()
        message.attachments = [SimpleNamespace(id=8901, filename='notes.pdf')]
        message.channel.archived = True
        self.remove()
        self.service.request_book_refresh(self.h.guild, self.book['id'])
        await self.finish_queue()
        self.assertTrue(message.channel.archived)
        self.assertIs(self.root_message(), message)
        self.assertEqual(message.attachments[0].id, 8901)
        self.assertIn('Книга удалена из каталога', message.content)
        self.assertNotIn('attachments', message.edit.await_args.kwargs)

    async def test_delete_during_full_refresh_does_not_restore_card_or_publish_meetings(self):
        await self.publish()
        message = self.root_message()
        started, release = asyncio.Event(), asyncio.Event()
        fetch = message.channel.fetch_message.side_effect

        async def blocked_fetch(ident):
            if not started.is_set():
                started.set()
                await release.wait()
            return await fetch(ident)

        message.channel.fetch_message.side_effect = blocked_fetch
        self.service.upsert = AsyncMock(wraps=self.service.upsert)

        async def periodic_refresh():
            async with self.service.locks[1]:
                await self.service.refresh(self.h.guild)

        periodic = asyncio.create_task(periodic_refresh())
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            confirmation = RemoveBookConfirmation(self.cog, self.current(), 99)
            await asyncio.wait_for(confirmation.confirm.callback(self.interaction()), timeout=1)
            release.set()
            await asyncio.wait_for(periodic, timeout=5)
            await self.finish_queue()
        finally:
            release.set()
            if not periodic.done():
                periodic.cancel()
                await asyncio.gather(periodic, return_exceptions=True)
        self.assertTrue(self.current()['deleted'])
        self.assertIn('Книга удалена из каталога', message.content)
        self.assertIsInstance(message.edit.await_args.kwargs['view'], RemovedBookView)
        keys = [call.args[1] for call in self.service.upsert.await_args_list]
        self.assertNotIn(f'meeting:{self.meeting["id"]}', keys)
        self.assertEqual(self.h.channels[13].create_thread.await_count, 2)


if __name__ == '__main__':
    unittest.main()

"""Cleanup touches only known obsolete navigation, with durable DELETE recovery."""
from types import SimpleNamespace
import unittest

import discord

from bookclub.service import Service
from bookclub.store import Store
from bookclub.town_cleanup import cleanup_town_pages, OBSOLETE_NOTICES
from bookclub.town_render import town_sections
from test_bookclub import ClubFixture
from test_bookclub_discord import DiscordHarness


NOTICE = 'Раздел обновлён. Актуальные материалы и переходы — в начале темы.'


class TownCleanupTests(ClubFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.h = DiscordHarness()
        self.service = Service(self.h.bot, self.store)
        self.addAsyncCleanup(self.service.close)
        self.thread = self.h.channel(800, parent_id=13)
        self.root = self.h.message(self.thread, 800, 'Текущая книга')
        self.store.reserve_publication('catalog:1', 1, 800)
        self.store.save_publication('catalog:1', 800, 800)

    def page(self, number, *, content=NOTICE, channel=None, view=None):
        channel = channel or self.thread
        if view is None:
            view = discord.ui.View(timeout=None)
            view.add_item(discord.ui.Button(label='К началу', url=self.root.jump_url))
        message = self.h.message(channel, 800 + number, content, view=view)
        key = f'catalog:1:page:{number}'
        self.store.reserve_publication(key, 1, channel.id)
        self.store.save_publication(key, channel.id, message.id)
        return message

    async def clean(self, active=0):
        return await cleanup_town_pages(self.service, self.h.guild, 'catalog:1', active)

    async def test_only_obsolete_pages_removed_and_no_repeat_or_recreation_after_restart(self):
        active = self.page(1, content='Активная страница')
        obsolete = [self.page(index, content=content)
                    for index, content in enumerate(sorted(OBSOLETE_NOTICES), 2)]
        before = {table: self.store.rows(f'SELECT * FROM {table}') for table in
                  ('bc_books', 'bc_essays', 'bc_import_runs', 'bc_import_budgets', 'bc_import_sources')}
        self.assertEqual(await self.clean(1), 3)
        self.assertIn(active.id, self.thread.messages)
        active.delete.assert_not_awaited()
        self.root.delete.assert_not_awaited()
        for message in obsolete:
            message.delete.assert_awaited_once()
            self.assertNotIn(message.id, self.thread.messages)
        self.store = Store(self.path, clock=lambda: self.now)
        self.service = Service(self.h.bot, self.store)
        self.addAsyncCleanup(self.service.close)
        self.assertEqual(await self.clean(1), 0)
        self.thread.send.assert_not_awaited()
        self.h.channels[13].create_thread.assert_not_awaited()
        self.assertEqual(before, {table: self.store.rows(f'SELECT * FROM {table}') for table in before})

    async def test_manual_content_attachments_author_and_changed_controls_are_preserved(self):
        changed = self.page(1, content='Мои заметки организатора')
        attached = self.page(2)
        attached.attachments = [SimpleNamespace(id=1, filename='book.epub')]
        foreign = self.page(3)
        foreign.author = self.h.members[99]
        custom = discord.ui.View(timeout=None)
        custom.add_item(discord.ui.Button(label='Мой материал', url='https://example.org/book'))
        controls = self.page(4, view=custom)
        action = discord.ui.View(timeout=None)
        action.add_item(discord.ui.Button(label='Действие', custom_id='manual'))
        callback = self.page(5, view=action)
        embedded = self.page(6)
        embedded.embeds = [discord.Embed(title='Материал организатора')]
        with self.assertLogs('bookclub.town_cleanup', level='WARNING'):
            self.assertEqual(await self.clean(), 0)
        for message in (changed, attached, foreign, controls, callback, embedded):
            message.delete.assert_not_awaited()
            message.edit.assert_not_awaited()
            self.assertIsNotNone(self.store.publication(f'catalog:1:page:{message.id - 800}'))

    async def test_wrong_thread_other_forum_and_guild_are_never_cleaned(self):
        foreign = self.h.channel(900, parent_id=14)
        wrong_thread = self.page(1, channel=foreign)
        self.assertEqual(await self.clean(), 0)
        wrong_thread.delete.assert_not_awaited()
        self.store.configure(1, {**self.store.settings(1), 'books': 23})
        normal = self.page(2)
        self.assertEqual(await self.clean(), 0)
        normal.delete.assert_not_awaited()
        for key in ('book:1', 'catalog:2', 'essay-space:1', 'catalog:1:page:1'):
            with self.assertRaises(ValueError):
                await cleanup_town_pages(self.service, self.h.guild, key, 0)

    async def test_missing_message_retires_binding_without_sending_or_deleting_again(self):
        page = self.page(1)
        del self.thread.messages[page.id]
        self.assertEqual(await self.clean(), 1)
        self.assertIsNone(self.store.publication('catalog:1:page:1'))
        page.delete.assert_not_awaited()
        self.thread.send.assert_not_awaited()

    async def test_lost_delete_response_is_confirmed_by_get_before_binding_is_retired(self):
        page = self.page(1)
        async def deleted_but_response_lost():
            self.assertIsNotNone(self.store.publication('catalog:1:page:1'))
            del self.thread.messages[page.id]
            raise OSError('lost acknowledgement')
        page.delete.side_effect = deleted_but_response_lost
        self.assertEqual(await self.clean(), 1)
        self.assertIsNone(self.store.publication('catalog:1:page:1'))
        self.assertEqual(self.thread.fetch_message.await_count, 2)
        self.assertEqual(await self.clean(), 0)
        page.delete.assert_awaited_once()

    async def test_failed_delete_preserves_binding_then_retry_deletes_same_message(self):
        page = self.page(1)
        delete = page.delete.side_effect
        page.delete.side_effect = OSError('request failed')
        with self.assertRaises(OSError):
            await self.clean()
        self.assertIsNotNone(self.store.publication('catalog:1:page:1'))
        self.assertIn(page.id, self.thread.messages)
        page.delete.side_effect = delete
        self.assertEqual(await self.clean(), 1)
        self.assertIsNone(self.store.publication('catalog:1:page:1'))
        self.thread.send.assert_not_awaited()

    async def test_delete_permission_failure_keeps_binding_and_other_pages_can_finish(self):
        forbidden = self.page(1)
        removable = self.page(2)
        forbidden.delete.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason='Forbidden'), {'code': 50013, 'message': 'Missing permissions'})
        with self.assertLogs('bookclub.town_cleanup', level='WARNING'):
            self.assertEqual(await self.clean(), 1)
        self.assertIsNotNone(self.store.publication('catalog:1:page:1'))
        self.assertIsNone(self.store.publication('catalog:1:page:2'))
        forbidden.edit.assert_not_awaited()
        removable.delete.assert_awaited_once()

    async def test_a_page_bound_to_root_message_cannot_delete_the_root(self):
        self.store.reserve_publication('catalog:1:page:1', 1, self.thread.id)
        self.store.save_publication('catalog:1:page:1', self.thread.id, self.root.id)
        self.root.content = NOTICE
        self.assertEqual(await self.clean(), 0)
        self.root.delete.assert_not_awaited()

    async def test_a_message_shared_by_another_publication_is_preserved(self):
        page = self.page(1)
        self.store.reserve_publication('book:other', 1, self.thread.id)
        self.store.save_publication('book:other', self.thread.id, page.id)
        with self.assertLogs('bookclub.town_cleanup', level='WARNING'):
            self.assertEqual(await self.clean(), 0)
        page.delete.assert_not_awaited()
        self.assertIsNotNone(self.store.publication('catalog:1:page:1'))

    async def test_catalog_refresh_cleans_old_notices_preserving_real_pages_and_other_messages(self):
        self.store.set_published(1)
        self.store.update_book(1, self.book['id'], status='reading')
        old = await self.service.paged_post(self.h.guild, 'catalog:1', 13,
            'Старый каталог', ['Начало каталога', 'Старая очередь', 'Старый архив'], inline_first=True)
        notice_view = discord.ui.View(timeout=None)
        notice_view.add_item(discord.ui.Button(label='К началу', url=self.root.jump_url))
        old_pages = []
        for index in (1, 2):
            pub = await self.service.upsert(self.h.guild, f'catalog:1:page:{index}',
                self.thread.id, NOTICE, view=notice_view)
            old_pages.append(self.thread.messages[pub['message_id']])
        attached = self.page(3)
        attached.attachments = [SimpleNamespace(id=777, filename='edition.epub')]
        user_note = self.h.message(self.thread, 9000, 'Заметка читателя')
        user_note.author = self.h.members[1]
        for index in range(18):
            book = self.store.create_book(1, f'{index:02} ' + 'Название очередной книги ' * 6,
                'Автор', '', f'cleanup-book-{index}')
            self.store.update_book(1, book['id'], status='queued')
        plan = await self.service.paged_post(self.h.guild, 'catalog:1:plan', 13,
            'План чтения', town_sections(self.store, 1)['plan'], inline_first=True)
        plan_thread = self.h.channels[plan['channel_id']]
        from bookclub.plan_entries import publish_plan
        await publish_plan(self.service, self.h.guild, plan, verify=True)
        active_pages = self.store.rows("SELECT * FROM bc_publications WHERE key LIKE 'catalog:1:plan:row:%'")
        self.assertTrue(active_pages)

        with self.assertLogs('bookclub.town_cleanup', level='WARNING'):
            await self.service.refresh_catalog(self.h.guild)

        stored = self.store.publication('catalog:1')
        self.assertEqual((stored['channel_id'], stored['message_id']),
                         (old['channel_id'], old['message_id']))
        for index, message in enumerate(old_pages, 1):
            message.delete.assert_awaited_once()
            self.assertIsNone(self.store.publication(f'catalog:1:page:{index}'))
            self.assertNotIn(message.id, self.thread.messages)
        for message in (self.root, attached, user_note):
            message.delete.assert_not_awaited()
            self.assertIn(message.id, self.thread.messages)
        self.assertEqual(user_note.content, 'Заметка читателя')
        self.assertEqual(attached.attachments[0].filename, 'edition.epub')
        for page in active_pages:
            stored_page = self.store.publication(page['key'])
            self.assertEqual(stored_page['message_id'], page['message_id'])
            plan_thread.messages[page['message_id']].delete.assert_not_awaited()
        sends = {channel.id: channel.send.await_count for channel in self.h.channels.values()}
        created = self.h.channels[13].create_thread.await_count
        await self.service.refresh_catalog(self.h.guild)
        self.assertEqual({channel.id: channel.send.await_count for channel in self.h.channels.values()}, sends)
        self.assertEqual(self.h.channels[13].create_thread.await_count, created)

    async def test_shortened_plan_removes_only_verified_old_page_with_obsolete_sync_date(self):
        key = 'catalog:1:plan'
        old_page = '## План чтения · 2/2\n[Прошлый синк](https://discord.com/events/1/999) · старая дата'
        root = await self.service.paged_post(self.h.guild, key, 13, 'План чтения',
            ['## План чтения · 1/2\nСтарая очередь', old_page], inline_first=True)
        self.assertTrue(self.h.channels[13].create_thread.await_args.kwargs['suppress_embeds'])
        pub = self.store.publication(key + ':page:1')
        thread = self.h.channels[root['channel_id']]
        old = thread.messages[pub['message_id']]
        old.edit.reset_mock()
        self.assertEqual(self.service.delivery.intent(pub['key'])['content'], old_page)
        updated = await self.service.paged_post(self.h.guild, key, 13, 'План чтения',
            ['## План чтения\nОчередь книг без расписания'], inline_first=True)
        self.assertEqual(updated['message_id'], root['message_id'])
        self.assertTrue(thread.messages[root['message_id']].edit.await_args.kwargs['suppress'])
        self.assertIsNone(self.store.publication(pub['key']))
        self.assertNotIn(old.id, thread.messages)
        old.delete.assert_awaited_once()
        old.edit.assert_not_awaited()
        sends = thread.send.await_count
        await self.service.paged_post(self.h.guild, key, 13, 'План чтения',
            ['## План чтения\nОчередь книг без расписания'], inline_first=True)
        self.assertEqual(thread.send.await_count, sends)
        self.assertNotIn('https://discord.com/events/', '\n'.join(m.content for m in thread.messages.values()))

    async def test_shortened_plan_preserves_manual_text_files_and_active_pages(self):
        key = 'catalog:1:plan'
        contents = ['## План чтения · 1/4\nОчередь', '## План чтения · 2/4\nАктивная очередь',
                    '## План чтения · 3/4\nПрежнее расписание', '## План чтения · 4/4\nМатериалы']
        root = await self.service.paged_post(self.h.guild, key, 13, 'План чтения', contents, inline_first=True)
        thread = self.h.channels[root['channel_id']]
        active_pub = self.store.publication(key + ':page:1')
        manual_pub = self.store.publication(key + ':page:2')
        attached_pub = self.store.publication(key + ':page:3')
        active = thread.messages[active_pub['message_id']]
        manual = thread.messages[manual_pub['message_id']]
        attached = thread.messages[attached_pub['message_id']]
        manual.content = 'Ручные заметки организатора'
        attached.attachments = [SimpleNamespace(id=4567, filename='schedule.pdf')]
        manual.edit.reset_mock()
        attached.edit.reset_mock()
        with self.assertLogs('bookclub.town_cleanup', level='WARNING'):
            await self.service.paged_post(self.h.guild, key, 13, 'План чтения',
                ['## План чтения · 1/2\nОчередь', '## План чтения · 2/2\nСледующие книги'], inline_first=True)
        self.assertIn('Следующие книги', active.content)
        self.assertEqual(manual.content, 'Ручные заметки организатора')
        self.assertEqual(attached.attachments[0].filename, 'schedule.pdf')
        for message in (active, manual, attached):
            message.delete.assert_not_awaited()
            self.assertIn(message.id, thread.messages)
        manual.edit.assert_not_awaited()
        attached.edit.assert_not_awaited()

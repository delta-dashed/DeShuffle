"""Publication navigation and independent reminder delivery with mocked Discord."""
from datetime import datetime, timezone
import unittest
from unittest.mock import AsyncMock

import discord

from bookclub.store import ClubError
from bookclub.ui import Club
from test_bookclub import ClubFixture
from test_bookclub_discord import DiscordHarness


class PublicationAuditTests(ClubFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.h = DiscordHarness()
        self.cog = Club(self.h.bot, self.store)
        self.service = self.cog.service
        m = self.meeting
        self.h.event(m['event_id'], name=m['name'],
                     start_time=datetime.fromtimestamp(m['start'], timezone.utc),
                     end_time=datetime.fromtimestamp(m['end'], timezone.utc))

    async def asyncSetUp(self):
        import asyncio
        asyncio.get_running_loop().slow_callback_duration = 1.0

    def page_message(self, key, index):
        pub = self.store.publication(f'{key}:page:{index}')
        return self.h.channels[pub['channel_id']].messages[pub['message_id']]

    def link_view(self, **button):
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label='Открыть', **button))
        return view

    async def test_forty_long_pages_are_reachable_and_unchanged_refresh_is_noop(self):
        contents = [f'{index:02d} ' + 'x' * 1697 for index in range(1, 41)]
        key = 'catalog:1'
        root = await self.service.paged_post(self.h.guild, key, 13, 'Каталог', contents)
        thread = self.h.channels[root['channel_id']]
        root_message = thread.messages[root['message_id']]
        root_url = f'https://discord.com/channels/1/{thread.id}/{root_message.id}'
        first = self.page_message(key, 1)
        self.assertIn(first.jump_url, root_message.content)
        self.assertIn(self.page_message(key, 40).jump_url, root_message.content)

        visited = []
        current = first
        while current:
            self.assertNotIn(current.id, visited)
            visited.append(current.id)
            buttons = current.edit.await_args.kwargs['view'].children
            links = {b.label: b.url for b in buttons}
            self.assertEqual(links['К началу'], root_url)
            if len(visited) > 1:
                previous = self.page_message(key, len(visited) - 1)
                self.assertEqual(links['← Предыдущая'], previous.jump_url)
            following = links.get('Следующая →')
            current = thread.messages[int(following.rsplit('/', 1)[1])] if following else None
        self.assertEqual(len(visited), 40)
        self.assertTrue(all(len(m.content) <= 2000 for m in thread.messages.values()))
        for index, content in enumerate(contents, 1):
            self.assertIn(content, self.page_message(key, index).content)

        counts = {m.id: m.edit.await_count for m in thread.messages.values()}
        await self.service.paged_post(self.h.guild, key, 13, 'Каталог', contents)
        self.assertEqual(counts, {m.id: m.edit.await_count for m in thread.messages.values()})
        self.assertEqual(thread.send.await_count, 40)
        self.assertEqual(self.h.channels[13].create_thread.await_count, 1)

    async def test_button_url_style_row_and_removal_invalidate_digest(self):
        variants = [
            dict(url='https://example.org/first', row=0),
            dict(url='https://example.org/second', row=0),
            dict(url='https://example.org/second', row=1),
            dict(custom_id='publication:audit', style=discord.ButtonStyle.primary, row=1),
            dict(custom_id='publication:audit', style=discord.ButtonStyle.secondary, row=1),
        ]
        pub = None
        for index, button in enumerate(variants):
            pub = await self.service.upsert(self.h.guild, 'news:1', 11, 'Одинаковый текст', view=self.link_view(**button))
            message = self.h.channels[11].messages[pub['message_id']]
            self.assertEqual(message.edit.await_count, index)
        await self.service.upsert(self.h.guild, 'news:1', 11, 'Одинаковый текст')
        self.assertEqual(message.edit.await_count, len(variants))
        self.assertIsNone(message.edit.await_args.kwargs['view'])
        await self.service.upsert(self.h.guild, 'news:1', 11, 'Одинаковый текст')
        self.assertEqual(message.edit.await_count, len(variants))

    async def test_managed_book_and_catalog_titles_update_but_essay_title_is_preserved(self):
        for key in (f'book:{self.book["id"]}', 'catalog:1', 'essay-space:book:1'):
            with self.subTest(key=key):
                pub = await self.service.upsert(self.h.guild, key, 13, 'Карточка', forum_name='Исходное название')
                thread = self.h.channels[pub['channel_id']]
                await self.service.upsert(self.h.guild, key, 13, 'Карточка', forum_name='Новое название')
                self.assertEqual(thread.name, 'Исходное название' if key.startswith('essay-space:') else 'Новое название')

    async def test_deleted_root_recovers_without_restart_or_gateway_event(self):
        key = f'book:{self.book["id"]}'
        contents = ['a' * 1700, 'b' * 1700, 'c' * 1700]
        view = self.link_view(custom_id='book:add', style=discord.ButtonStyle.primary)
        old = await self.service.paged_post(self.h.guild, key, 13, 'Книга', contents, view=view)
        del self.h.channels[old['channel_id']]

        recovered = await self.service.paged_post(self.h.guild, key, 13, 'Книга', contents, view=view)

        self.assertNotEqual(recovered['channel_id'], old['channel_id'])
        self.assertEqual(self.h.channels[13].create_thread.await_count, 2)
        for index in range(1, 4):
            page = self.store.publication(f'{key}:page:{index}')
            self.assertEqual(page['channel_id'], recovered['channel_id'])
        root_message = self.h.channels[recovered['channel_id']].messages[recovered['message_id']]
        self.assertIs(root_message.edit.await_args.kwargs['view'], view)

    async def test_recreated_page_repairs_neighbor_links_in_the_same_refresh(self):
        key = 'catalog:1'
        contents = ['Первая', 'Вторая', 'Третья']
        root = await self.service.paged_post(self.h.guild, key, 13, 'Каталог', contents)
        old = self.page_message(key, 2)
        del self.h.channels[root['channel_id']].messages[old.id]
        contents[1] = 'Обновлённая вторая'

        await self.service.paged_post(self.h.guild, key, 13, 'Каталог', contents)

        fresh = self.page_message(key, 2)
        self.assertNotEqual(fresh.id, old.id)
        before = {b.label: b.url for b in self.page_message(key, 1).edit.await_args.kwargs['view'].children}
        after = {b.label: b.url for b in self.page_message(key, 3).edit.await_args.kwargs['view'].children}
        self.assertEqual(before['Следующая →'], fresh.jump_url)
        self.assertEqual(after['← Предыдущая'], fresh.jump_url)

    async def test_projection_failure_does_not_block_meeting_reminders(self):
        self.store.set_published(1)
        job = self.jobs()[0]
        self.now = job['due']
        self.service.refresh = AsyncMock(side_effect=ClubError('Карточка недоступна'))

        with self.assertLogs('bookclub.service', level='ERROR'):
            await self.service.tick(self.h.guild)

        for user in (1, 2, 3):
            self.h.members[user].send.assert_awaited_once()
        self.h.members[99].send.assert_not_awaited()
        self.assertEqual(self.store.one('SELECT state FROM bc_jobs WHERE key=?', (job['key'],))['state'], 'sent')

    async def test_failed_essay_scan_defers_only_essay_reminders_until_success(self):
        self.store.set_published(1)
        meeting_job = self.jobs()[0]
        discussion = self.essay_event(meeting_job['due'] + 86400)
        self.h.event(discussion['event_id'], name=discussion['name'],
                     start_time=datetime.fromtimestamp(discussion['start'], timezone.utc),
                     end_time=datetime.fromtimestamp(discussion['end'], timezone.utc))
        self.now = meeting_job['due']
        essay_job = next(j for j in self.store.due_jobs(1) if j['kind'] == 'essay')
        self.service.scan_essays = AsyncMock(side_effect=ClubError('Форум временно недоступен'))

        with self.assertLogs('bookclub.service', level='ERROR'):
            await self.service.tick(self.h.guild)

        for user in (1, 2, 3):
            self.h.members[user].send.assert_awaited_once()
            self.assertIn('Скоро встреча', self.h.members[user].send.await_args.args[0])
        self.assertEqual(self.store.one('SELECT state FROM bc_jobs WHERE key=?', (essay_job['key'],))['state'], 'pending')
        self.service.scan_essays = AsyncMock()
        self.now += 30
        await self.service.tick(self.h.guild)
        for user in (1, 2, 3):
            self.assertEqual(self.h.members[user].send.await_count, 2)
            self.assertIn('Эссе по книге', self.h.members[user].send.await_args.args[0])
        self.assertEqual(self.store.one('SELECT state FROM bc_jobs WHERE key=?', (essay_job['key'],))['state'], 'sent')

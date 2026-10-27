"""Town navigation reconciles existing Discord publications without duplicates."""
import asyncio
from types import SimpleNamespace
import unittest

import discord

from bookclub.catalog_ui import CatalogView
from bookclub.forum_tags import TEMPLATES
from bookclub.service import Service
from bookclub.store import Store
from bookclub.ui import Club
from test_bookclub import ClubFixture
from test_bookclub_discord import DiscordHarness


KEYS = ('catalog:1', 'catalog:1:plan', 'catalog:1:archive')


class TownProjectionTests(ClubFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.h = DiscordHarness()
        self.new_service()
        self.store.set_published(1)
        self.store.update_book(1, self.book['id'], status='reading')
        forum = self.h.channels[13]
        self.tags = {}
        for index, (purpose, name) in enumerate(TEMPLATES['books'].items(), 1):
            tag = discord.ForumTag(name=name)
            tag.id = 13000 + index
            forum.available_tags.append(tag)
            self.tags[purpose] = tag
            self.store.bind_tag(1, forum.id, purpose, tag.id)
        forum.flags.require_tag = True

    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 1

    def new_service(self):
        self.service = Service(self.h.bot, self.store)
        self.service.catalog_view_factory = lambda guild_id: CatalogView(self.service, guild_id)
        self.addAsyncCleanup(self.service.close)

    def restart(self):
        self.store = Store(self.path, clock=lambda: self.now)
        self.new_service()

    async def seed_catalog(self, *, pinned=True, pages=None):
        root = await self.service.paged_post(self.h.guild, 'catalog:1', 13,
            'Каталог книжного клуба', pages or ['Старый каталог'], inline_first=True,
            view=CatalogView(self.service, 1))
        self.h.channels[root['channel_id']].flags.pinned = pinned
        return root

    def root(self, key):
        pub = self.store.publication(key)
        self.assertIsNotNone(pub, key)
        thread = self.h.channels[pub['channel_id']]
        return thread, thread.messages[pub['message_id']]

    def ids(self):
        return {key: (self.store.publication(key)['channel_id'],
                      self.store.publication(key)['message_id']) for key in KEYS}

    @staticmethod
    def buttons(message):
        return [button for row in message.components for button in row.to_dict()['components']]

    def writes(self):
        return {
            channel.id: (channel.send.await_count, channel.edit.await_count,
                {message.id: (message.edit.await_count, message.delete.await_count)
                 for message in channel.messages.values()})
            for channel in self.h.channels.values()
        }

    def reads(self):
        return (self.h.bot.fetch_channel.await_count, self.h.guild.active_threads.await_count,
                {channel.id: channel.fetch_message.await_count for channel in self.h.channels.values()})

    def assert_clean(self):
        for key in KEYS:
            thread, _ = self.root(key)
            for message in thread.messages.values():
                self.assertNotIn('bc:', message.content)
                self.assertLessEqual(len(message.content), 2000)

    async def test_existing_catalog_becomes_pinned_current_with_two_tagged_topics_and_links(self):
        old = await self.seed_catalog()
        created_before = self.h.channels[13].create_thread.await_count
        await self.service.refresh_catalog(self.h.guild)
        self.assertEqual(self.h.channels[13].create_thread.await_count - created_before, 2)
        self.assertEqual(self.ids()['catalog:1'], (old['channel_id'], old['message_id']))
        guide, message = self.root('catalog:1')
        self.assertTrue(guide.flags.pinned)
        self.assertEqual({button['custom_id'] for button in self.buttons(message) if 'custom_id' in button},
            {f'bc:catalog:1:{action}' for action in ('add', 'import', 'queue', 'deleted')})
        for key in KEYS:
            thread, message = self.root(key)
            self.assertEqual({tag.id for tag in thread.applied_tags}, {self.tags['catalog'].id})
            if key != 'catalog:1':
                self.assertFalse(thread.flags.pinned)
            urls = {button['url'] for button in self.buttons(message) if 'url' in button}
            destinations = KEYS[1:] if key == 'catalog:1' else ('catalog:1',)
            other_roots = {self.root(other)[1].jump_url for other in destinations}
            self.assertTrue(other_roots.issubset(urls), (key, urls))
        self.assertIn(self.book['title'], self.root('catalog:1')[1].content)
        self.assertIn(self.book['title'], guide.name)
        self.assertIsNone(self.store.publication('catalog:1:current'))
        self.assert_clean()

    async def test_repeated_refresh_and_restart_keep_ids_without_further_writes(self):
        await self.seed_catalog()
        await self.service.refresh_catalog(self.h.guild)
        ids, writes = self.ids(), self.writes()
        reads = self.reads()
        created = self.h.channels[13].create_thread.await_count
        await self.service.refresh_catalog(self.h.guild)
        self.assertEqual(self.reads(), reads)
        self.restart()
        await self.service.refresh_catalog(self.h.guild)
        self.assertEqual(self.ids(), ids)
        self.assertEqual(self.writes(), writes)
        self.assertEqual(self.h.channels[13].create_thread.await_count, created)

    async def test_manual_topic_and_catalog_tag_renames_survive_restart(self):
        await self.seed_catalog()
        await self.service.refresh_catalog(self.h.guild)
        ids = self.ids()
        names = {key: f'Название организатора {index}' for index, key in enumerate(KEYS)}
        for key, name in names.items():
            self.root(key)[0].name = name
        self.tags['catalog'].name = 'Навигация города'
        self.restart()
        self.store.update_book(1, self.book['id'], title='Уточнённое название книги')
        await self.service.refresh_catalog(self.h.guild)
        self.assertEqual(self.ids(), ids)
        self.assertEqual({key: self.root(key)[0].name for key in KEYS}, names)
        self.assertEqual(self.tags['catalog'].name, 'Навигация города')
        self.assertIn('Уточнённое название книги', self.root('catalog:1')[1].content)

    async def test_lost_new_topic_response_recovers_after_restart_without_duplicate(self):
        await self.seed_catalog()
        forum = self.h.channels[13]
        send = forum.create_thread.side_effect
        lost = []

        async def create_and_lose(**kwargs):
            result = await send(**kwargs)
            lost.append((result.thread.id, result.message.id))
            raise OSError('lost town topic response')

        forum.create_thread.side_effect = create_and_lose
        with self.assertRaisesRegex(OSError, 'lost town topic response'):
            await self.service.refresh_catalog(self.h.guild)
        self.assertEqual(len(lost), 1)
        forum.create_thread.side_effect = send
        self.restart()
        await self.service.refresh_catalog(self.h.guild)
        await self.service.refresh_catalog(self.h.guild)
        self.assertIn(lost[0], self.ids().values())
        self.assertEqual(forum.create_thread.await_count, 3)
        self.assertEqual(len([thread for thread in self.h.channels.values()
                              if thread.parent_id == forum.id]), 3)
        self.assert_clean()

    async def test_old_catalog_pages_become_redirects_without_replacing_messages(self):
        old = await self.seed_catalog(pages=['Старое начало', 'Старое продолжение очереди', 'Старый архив'])
        pages = [self.store.publication(f'catalog:1:page:{index}') for index in (1, 2)]
        await self.service.refresh_catalog(self.h.guild)
        self.assertEqual(self.ids()['catalog:1'], (old['channel_id'], old['message_id']))
        for page in pages:
            stored = self.store.publication(page['key'])
            self.assertEqual((stored['channel_id'], stored['message_id']),
                             (page['channel_id'], page['message_id']))
            message = self.h.channels[page['channel_id']].messages[page['message_id']]
            self.assertNotIn('Старое', message.content)
            self.assertNotIn('Старый архив', message.content)
            self.assertIn(self.root('catalog:1')[1].jump_url,
                          {button['url'] for button in self.buttons(message) if 'url' in button})
            message.delete.assert_not_awaited()

    async def test_foreign_forum_pin_is_preserved_and_does_not_block_publication(self):
        await self.seed_catalog(pinned=False)
        foreign = self.h.channel(555, parent_id=13, owner_id=99, name='Закреплено организатором')
        foreign.flags.pinned = True
        with self.assertLogs('bookclub.town_projection', level='WARNING'):
            await self.service.refresh_catalog(self.h.guild)
        self.assertTrue(foreign.flags.pinned)
        foreign.edit.assert_not_awaited()
        self.assertFalse(self.root('catalog:1')[0].flags.pinned)
        self.assertEqual(len(self.ids()), 3)
        self.assert_clean()

    async def test_pin_forbidden_keeps_all_navigation_available(self):
        await self.seed_catalog(pinned=False)
        guide = self.root('catalog:1')[0]
        edit = guide.edit.side_effect

        async def no_pin(**kwargs):
            if kwargs.get('pinned'):
                raise discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'),
                                        {'code': 50013, 'message': 'Missing Permissions'})
            return await edit(**kwargs)

        guide.edit.side_effect = no_pin
        with self.assertLogs('bookclub.town_projection', level='WARNING'):
            await self.service.refresh_catalog(self.h.guild)
        self.assertFalse(guide.flags.pinned)
        self.assertEqual(len(self.ids()), 3)
        self.assert_clean()

    async def test_regular_refresh_restores_missing_section_and_repairs_cross_links(self):
        await self.service.refresh(self.h.guild)
        ids = self.ids()
        removed, _ = self.root('catalog:1:plan')
        del self.h.channels[removed.id]
        await self.service.refresh(self.h.guild)
        restored = self.ids()
        self.assertNotEqual(restored['catalog:1:plan'], ids['catalog:1:plan'])
        for key in ('catalog:1', 'catalog:1:archive'):
            self.assertEqual(restored[key], ids[key])
        urls = {button['url'] for button in self.buttons(self.root('catalog:1')[1]) if 'url' in button}
        self.assertIn(self.root('catalog:1:plan')[1].jump_url, urls)
        self.assertNotIn(f'https://discord.com/channels/1/{removed.id}/{removed.id}', urls)
        self.assert_clean()

    async def test_topics_deleted_during_refresh_repair_both_directions_in_same_pass(self):
        await self.service.refresh(self.h.guild)
        before = self.ids()
        guide, guide_message = self.root('catalog:1')
        archive, archive_message = self.root('catalog:1:archive')
        removed_urls = {guide_message.jump_url, archive_message.jump_url}
        current, _ = self.root('catalog:1:plan')
        fetch = current.fetch_message.side_effect
        removed = False

        async def delete_during_current_fetch(message_id):
            nonlocal removed
            if not removed:
                removed = True
                # The initial guide check has finished. The unchanged archive
                # can still hit the fast cache until back links are repaired.
                del self.h.channels[guide.id]
                del self.h.channels[archive.id]
            return await fetch(message_id)

        current.fetch_message.side_effect = delete_during_current_fetch
        self.store.update_book(1, self.book['id'], title='Новое название текущей книги')
        await self.service.refresh_catalog(self.h.guild)

        after = self.ids()
        self.assertTrue(removed)
        for key in ('catalog:1', 'catalog:1:archive'):
            self.assertNotEqual(after[key], before[key])
        for key in ('catalog:1:plan',):
            self.assertEqual(after[key], before[key])
        new_guide = self.root('catalog:1')[1]
        expected = {self.root(key)[1].jump_url for key in KEYS[1:]}
        links = {button['url'] for button in self.buttons(new_guide) if 'url' in button}
        self.assertTrue(expected.issubset(links))
        self.assertTrue(removed_urls.isdisjoint(links))
        for key in KEYS[1:]:
            message = self.root(key)[1]
            back_links = {button['url'] for button in self.buttons(message) if 'url' in button}
            self.assertIn(new_guide.jump_url, back_links)
            self.assertTrue(removed_urls.isdisjoint(back_links))
        self.assert_clean()

        writes = self.writes()
        await self.service.refresh_catalog(self.h.guild)
        self.assertEqual(self.ids(), after)
        self.assertEqual(self.writes(), writes)

    async def test_legacy_current_topic_and_pages_become_redirect_without_new_topic(self):
        await self.seed_catalog()
        legacy = await self.service.paged_post(self.h.guild, 'catalog:1:current', 13,
            'Текущая книга', ['Старая отдельная текущая книга', 'Старая страница'], inline_first=True)
        legacy_page = self.store.publication('catalog:1:current:page:1')
        before_creates = self.h.channels[13].create_thread.await_count
        await self.service.refresh_catalog(self.h.guild)
        stored = self.store.publication('catalog:1:current')
        self.assertEqual((stored['channel_id'], stored['message_id']),
                         (legacy['channel_id'], legacy['message_id']))
        self.assertEqual(self.h.channels[13].create_thread.await_count - before_creates, 2)
        root_url = self.root('catalog:1')[1].jump_url
        thread, message = self.root('catalog:1:current')
        self.assertIn(root_url, {button['url'] for button in self.buttons(message) if 'url' in button})
        self.assertNotIn('Старая отдельная', message.content)
        self.assertNotIn(self.book['title'], message.content)
        message.delete.assert_not_awaited()
        page_after = self.store.publication('catalog:1:current:page:1')
        self.assertEqual(page_after['message_id'], legacy_page['message_id'])
        self.assertNotIn('Старая страница', thread.messages[legacy_page['message_id']].content)
        ids, writes = self.ids(), self.writes()
        self.restart()
        await self.service.refresh_catalog(self.h.guild)
        self.assertEqual(self.ids(), ids)
        self.assertEqual(self.writes(), writes)

    async def test_current_pin_orders_navigation_and_rebinds_book_actions_without_touching_essays(self):
        cog = Club(self.h.bot, self.store)
        self.service = cog.service
        self.addAsyncCleanup(self.service.close)
        await self.service.refresh(self.h.guild)
        old_root = self.root('catalog:1')[1]
        first_row = old_root.components[0].to_dict()['components']
        self.assertEqual([button['label'] for button in first_row], ['План чтения', 'Архив'])
        self.assertTrue(all(button['type'] == 2 and 'url' in button for button in first_row))
        book_actions = {button['custom_id'] for button in self.buttons(old_root)
                        if button.get('custom_id', '').startswith('bc:book:')}
        self.assertEqual(book_actions, {f'bc:book:{self.book["id"]}:{action}'
                                      for action in ('write', 'own', 'list', 'manage', 'meetings')})
        before_ids = self.ids()
        before_history = {table: self.store.rows(f'SELECT * FROM {table}')
                          for table in ('bc_essays', 'bc_import_runs', 'bc_import_sources', 'bc_import_budgets')}
        another = self.store.create_book(1, 'Другая текущая книга', 'Другой автор', '', 'another-current')
        self.store.update_book(1, self.book['id'], status='read')
        self.store.update_book(1, another['id'], status='reading')
        await self.service.refresh(self.h.guild)
        self.assertEqual(self.ids(), before_ids)
        root = self.root('catalog:1')[1]
        self.assertIs(root, old_root)
        self.assertIn(another['title'], root.content)
        self.assertNotIn(self.book['title'], root.content)
        self.assertIn(another['title'], root.channel.name)
        book_actions = {button['custom_id'] for button in self.buttons(root)
                        if button.get('custom_id', '').startswith('bc:book:')}
        self.assertEqual(book_actions, {f'bc:book:{another["id"]}:{action}'
                                      for action in ('write', 'own', 'list', 'manage', 'meetings')})
        self.assertEqual(before_history, {table: self.store.rows(f'SELECT * FROM {table}')
                                         for table in before_history})
        self.assertIsNone(self.store.publication('catalog:1:current'))
        self.assertEqual([button['label'] for button in root.components[0].to_dict()['components']],
                         ['План чтения', 'Архив'])

        self.store.update_book(1, another['id'], status='read')
        await self.service.refresh(self.h.guild)
        root = self.root('catalog:1')[1]
        self.assertIn('Текущая книга ещё не выбрана', root.content)
        self.assertFalse(any(button.get('custom_id', '').startswith('bc:book:')
                             for button in self.buttons(root)))
        self.assertEqual(self.ids(), before_ids)

    async def test_upgrade_recovers_unacknowledged_legacy_current_before_sending_new_topics(self):
        await self.seed_catalog()
        forum = self.h.channels[13]
        send = forum.create_thread.side_effect
        lost = []

        async def create_legacy_and_lose(**kwargs):
            result = await send(**kwargs)
            lost.append((result.thread.id, result.message.id))
            raise OSError('old process lost current topic acknowledgement')

        forum.create_thread.side_effect = create_legacy_and_lose
        with self.assertRaisesRegex(OSError, 'old process lost'):
            await self.service.paged_post(self.h.guild, 'catalog:1:current', 13,
                'Текущая книга', ['Прежняя отдельная тема'], inline_first=True)
        pending = self.store.publication('catalog:1:current')
        self.assertIsNone(pending['message_id'])
        self.assertEqual(len(lost), 1)
        forum.create_thread.side_effect = send
        self.restart()
        await self.service.refresh_catalog(self.h.guild)
        restored = self.store.publication('catalog:1:current')
        self.assertEqual((restored['channel_id'], restored['message_id']), lost[0])
        self.assertEqual(forum.create_thread.await_count, 4)
        self.assertEqual(len([thread for thread in self.h.channels.values()
                              if thread.parent_id == forum.id]), 4)
        legacy = self.root('catalog:1:current')[1]
        self.assertNotIn('Прежняя отдельная тема', legacy.content)
        self.assertIn(self.root('catalog:1')[1].jump_url,
                      {button['url'] for button in self.buttons(legacy) if 'url' in button})
        writes = self.writes()
        await self.service.refresh_catalog(self.h.guild)
        self.assertEqual(self.writes(), writes)
        self.assert_clean()

    async def _upgrade_unacknowledged_old_root(self, *, manual_name=None):
        forum = self.h.channels[13]
        send = forum.create_thread.side_effect
        sent = []

        async def create_old_guide_and_lose(**kwargs):
            result = await send(**kwargs)
            sent.append(result)
            raise OSError('old process lost guide acknowledgement')

        forum.create_thread.side_effect = create_old_guide_and_lose
        with self.assertRaisesRegex(OSError, 'old process lost'):
            await self.service.paged_post(self.h.guild, 'catalog:1', 13,
                'Путеводитель книжного клуба', ['Старая навигация'], inline_first=True)
        pending = self.store.publication('catalog:1')
        self.assertIsNone(pending['message_id'])
        self.assertIsNone(pending['managed_name'])
        self.assertEqual(len(sent), 1)
        if manual_name:
            sent[0].thread.name = manual_name
        forum.create_thread.side_effect = send
        self.restart()
        await self.service.refresh_catalog(self.h.guild)
        thread, message = self.root('catalog:1')
        self.assertEqual((thread.id, message.id), (sent[0].thread.id, sent[0].message.id))
        self.assertEqual(forum.create_thread.await_count, 3)
        self.assertIn(self.book['title'], message.content)
        self.assertNotIn('Старая навигация', message.content)
        self.assert_clean()
        return thread

    async def test_lost_old_guide_root_recovers_with_current_book_title_and_same_ids(self):
        thread = await self._upgrade_unacknowledged_old_root()
        expected = f'Текущая книга · {self.book["title"]}'
        self.assertEqual(thread.name, expected)
        self.assertEqual(self.store.publication('catalog:1')['managed_name'], expected)
        ids, writes = self.ids(), self.writes()
        self.restart()
        await self.service.refresh_catalog(self.h.guild)
        self.assertEqual(self.ids(), ids)
        self.assertEqual(self.writes(), writes)

    async def test_lost_old_guide_root_preserves_manual_title_before_and_after_restart(self):
        manual_name = 'Организатор назвал эту тему'
        thread = await self._upgrade_unacknowledged_old_root(manual_name=manual_name)
        self.assertEqual(thread.name, manual_name)
        ids = self.ids()
        self.store.update_book(1, self.book['id'], title='Новое название читаемой книги')
        self.restart()
        await self.service.refresh_catalog(self.h.guild)
        self.assertEqual(thread.name, manual_name)
        self.assertEqual(self.ids(), ids)
        self.assertIn('Новое название читаемой книги', self.root('catalog:1')[1].content)

    async def test_upgrade_recovers_unacknowledged_legacy_page_before_creating_plan(self):
        await self.seed_catalog()
        forum = self.h.channels[13]
        create = forum.create_thread.side_effect
        lost_pages, legacy_threads, original_sends = [], [], []

        async def legacy_topic_with_lost_page(**kwargs):
            result = await create(**kwargs)
            legacy_threads.append(result.thread)
            send = result.thread.send.side_effect
            original_sends.append(send)

            async def send_page_and_lose(content, **options):
                message = await send(content, **options)
                lost_pages.append(message)
                raise OSError('old process lost page acknowledgement')

            result.thread.send.side_effect = send_page_and_lose
            return result

        forum.create_thread.side_effect = legacy_topic_with_lost_page
        with self.assertRaisesRegex(OSError, 'old process lost page'):
            await self.service.paged_post(self.h.guild, 'catalog:1:current', 13,
                'Текущая книга', ['Прежняя текущая книга', 'Прежнее продолжение'], inline_first=True)
        self.assertEqual(len(lost_pages), 1)
        page_key = 'catalog:1:current:page:1'
        pending = self.store.publication(page_key)
        self.assertIsNone(pending['message_id'])
        old_thread, = legacy_threads
        old_thread.send.side_effect = original_sends[0]
        checked_before_creates = []

        async def require_page_recovered_before_new_topic(**kwargs):
            publication = self.store.publication(page_key)
            self.assertEqual(publication['state'], 'ready')
            self.assertEqual(publication['message_id'], lost_pages[0].id)
            checked_before_creates.append(kwargs['name'])
            return await create(**kwargs)

        forum.create_thread.side_effect = require_page_recovered_before_new_topic
        self.restart()
        await self.service.refresh_catalog(self.h.guild)
        self.assertEqual(checked_before_creates, ['План чтения', 'Архив прочитанного'])
        self.assertEqual(forum.create_thread.await_count, 4)
        old_thread.send.assert_awaited_once()
        publication = self.store.publication(page_key)
        self.assertEqual((publication['channel_id'], publication['message_id']),
                         (old_thread.id, lost_pages[0].id))
        self.assertIn(self.root('catalog:1')[1].jump_url, lost_pages[0].content)
        self.assertNotIn('Прежнее продолжение', lost_pages[0].content)
        lost_pages[0].delete.assert_not_awaited()
        ids, writes = self.ids(), self.writes()
        self.restart()
        await self.service.refresh_catalog(self.h.guild)
        self.assertEqual(self.ids(), ids)
        self.assertEqual(self.writes(), writes)

"""Moving configured channels preserves old history and changes new projections."""
import asyncio
import unittest

import discord

from bookclub.service import Service
from test_bookclub import ClubFixture
from test_bookclub_discord import DiscordHarness


class PublicationDestinationTests(ClubFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.h = DiscordHarness()
        self.service = Service(self.h.bot, self.store)
        self.new_news = self.h.channel(21, discord.TextChannel)
        self.new_forum = self.h.channel(23, discord.ForumChannel)

    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 1.0

    def capture_history(self, channels):
        return {channel.id: (channel.name, channel.edit.await_count,
                             {message.id: (message.content, message.edit.await_count)
                              for message in channel.messages.values()})
                for channel in channels}

    async def test_same_news_content_moves_before_digest_shortcut_and_preserves_original(self):
        original = await self.service.upsert(self.h.guild, 'news:1', 11, 'Вестник')
        before = self.capture_history([self.h.channels[11]])
        moved = await self.service.upsert(self.h.guild, 'news:1', 21, 'Вестник')
        self.assertEqual(moved['channel_id'], 21)
        self.assertNotEqual(moved['message_id'], original['message_id'])
        self.assertEqual(before, self.capture_history([self.h.channels[11]]))
        await self.service.upsert(self.h.guild, 'news:1', 21, 'Вестник')
        self.new_news.send.assert_awaited_once()

    async def test_pending_news_reservation_in_old_channel_is_detached_without_recovery_there(self):
        self.store.reserve_publication('news:1', 1, 11)
        self.h.message(self.h.channels[11], 7777, 'Старый Вестник\n-# bc:news:1')
        before = self.capture_history([self.h.channels[11]])
        moved = await self.service.upsert(self.h.guild, 'news:1', 21, 'Вестник')
        self.assertEqual(moved['channel_id'], 21)
        self.assertNotEqual(moved['message_id'], 7777)
        self.assertEqual(before, self.capture_history([self.h.channels[11]]))

    async def test_paged_forum_move_rebinds_only_current_pages_without_touching_old_thread(self):
        key = f'book:{self.book["id"]}'
        original = await self.service.paged_post(self.h.guild, key, 13, 'Книга', ['Первая', 'Вторая', 'Третья'])
        old_thread = self.h.channels[original['channel_id']]
        before = self.capture_history([old_thread])
        moved = await self.service.paged_post(self.h.guild, key, 23, 'Книга', ['Первая', 'Вторая'])
        self.assertNotEqual(moved['channel_id'], original['channel_id'])
        self.assertEqual(self.h.channels[moved['channel_id']].parent_id, 23)
        for index in (1, 2):
            self.assertEqual(self.store.publication(f'{key}:page:{index}')['channel_id'], moved['channel_id'])
        self.assertIsNone(self.store.publication(f'{key}:page:3'))
        self.assertEqual(before, self.capture_history([old_thread]))
        await self.service.paged_post(self.h.guild, key, 23, 'Книга', ['Первая', 'Вторая'])
        self.new_forum.create_thread.assert_awaited_once()
        self.assertEqual(before, self.capture_history([old_thread]))

    async def test_direct_forum_upsert_moves_even_when_content_hash_is_unchanged(self):
        original = await self.service.upsert(self.h.guild, 'catalog:1', 13, 'Каталог', forum_name='Каталог')
        old_thread = self.h.channels[original['channel_id']]
        before = self.capture_history([old_thread])
        moved = await self.service.upsert(self.h.guild, 'catalog:1', 23, 'Каталог', forum_name='Каталог')
        self.assertEqual(self.h.channels[moved['channel_id']].parent_id, 23)
        self.assertNotEqual(moved['channel_id'], original['channel_id'])
        self.assertEqual(before, self.capture_history([old_thread]))

    async def test_pending_forum_reservation_moves_to_new_forum(self):
        key = 'catalog:1'
        self.store.reserve_publication(key, 1, 13)
        old_thread = self.h.channel(7777, discord.Thread, parent_id=13)
        self.h.message(old_thread, old_thread.id, 'Старый каталог\n-# bc:catalog:1')
        before = self.capture_history([old_thread])
        moved = await self.service.paged_post(self.h.guild, key, 23, 'Каталог', ['Первая', 'Вторая'])
        self.assertEqual(self.h.channels[moved['channel_id']].parent_id, 23)
        self.new_forum.create_thread.assert_awaited_once()
        self.assertEqual(before, self.capture_history([old_thread]))

    async def test_refresh_moves_book_catalog_news_and_meeting_after_configuration_change(self):
        self.store.set_published(1)
        await self.service.refresh(self.h.guild)
        old_book = self.store.publication(f'book:{self.book["id"]}')
        old_catalog = self.store.publication('catalog:1')
        old_meeting = self.store.publication(f'meeting:{self.meeting["id"]}')
        old_channels = [self.h.channels[11], self.h.channels[old_book['channel_id']],
                        self.h.channels[old_catalog['channel_id']]]
        before = self.capture_history(old_channels)
        self.store.configure(1, {**self.store.settings(1), 'books': 23, 'news': 21})
        await self.service.refresh(self.h.guild)
        book = self.store.publication(f'book:{self.book["id"]}')
        catalog = self.store.publication('catalog:1')
        meeting = self.store.publication(f'meeting:{self.meeting["id"]}')
        self.assertEqual(self.h.channels[book['channel_id']].parent_id, 23)
        self.assertEqual(self.h.channels[catalog['channel_id']].parent_id, 23)
        self.assertEqual(meeting['channel_id'], book['channel_id'])
        self.assertNotEqual(meeting['message_id'], old_meeting['message_id'])
        self.assertEqual(self.store.publication('news:1')['channel_id'], 21)
        self.assertEqual(before, self.capture_history(old_channels))
        await self.service.refresh(self.h.guild)
        self.assertEqual(self.new_forum.create_thread.await_count, 2)
        self.assertEqual(self.new_news.send.await_count, 2)
        self.assertEqual(before, self.capture_history(old_channels))


if __name__ == '__main__':
    unittest.main()

"""Forum labels are applied through real Service publication/essay flows."""
import unittest

import discord

from bookclub.forum_tags import TEMPLATES
from bookclub.service import Service
from bookclub.store import ClubError
from test_bookclub import ClubFixture
from test_webhook_discord import WebhookHarness


def tag(ident, name):
    value = discord.ForumTag(name=name)
    value.id = ident
    return value


class ForumIntegrationTests(ClubFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.h = WebhookHarness()
        self.service = Service(self.h.bot, self.store)
        self.store.set_published(1)
        self.labels = {}
        for forum_id, purpose in ((13, 'books'), (14, 'essays')):
            forum = self.h.channels[forum_id]
            forum.flags.require_tag = True
            forum.available_tags = []
            for index, (kind, name) in enumerate(TEMPLATES[purpose].items(), 1):
                value = tag(forum_id * 1000 + index, name)
                forum.available_tags.append(value)
                self.labels[kind] = value
                self.store.bind_tag(1, forum_id, kind, value.id)

    async def asyncSetUp(self):
        import asyncio
        asyncio.get_running_loop().slow_callback_duration = 1.0

    def thread(self, key):
        publication = self.store.publication(key)
        return self.h.channels[publication['channel_id']]

    def ids(self, thread):
        return {value.id for value in thread.applied_tags}

    async def test_required_book_forum_creates_tagged_catalog_and_keeps_status_current(self):
        await self.service.refresh(self.h.guild)
        forum = self.h.channels[13]
        self.assertEqual(forum.create_thread.await_count, 2)
        sent = [call.kwargs['applied_tags'] for call in forum.create_thread.await_args_list]
        self.assertIn([self.labels['catalog']], sent)
        self.assertIn([self.labels['proposed']], sent)
        book_thread = self.thread(f'book:{self.book["id"]}')
        catalog_thread = self.thread('catalog:1')
        self.assertEqual(self.ids(catalog_thread), {self.labels['catalog'].id})
        for status in ('queued', 'reading', 'read'):
            with self.subTest(status=status):
                self.store.update_book(1, self.book['id'], status=status)
                await self.service.refresh(self.h.guild)
                self.assertEqual(self.ids(book_thread), {self.labels[status].id})
        self.assertEqual(forum.create_thread.await_count, 2)

    async def test_book_status_transition_preserves_foreign_tag_and_manual_tag_rename(self):
        await self.service.refresh(self.h.guild)
        custom = tag(13099, 'Любимое')
        self.h.channels[13].available_tags.append(custom)
        thread = self.thread(f'book:{self.book["id"]}')
        thread.applied_tags.append(custom)
        self.labels['reading'].name = 'Читаем вместе'
        self.store.update_book(1, self.book['id'], status='reading')
        await self.service.refresh(self.h.guild)
        self.assertEqual(self.ids(thread), {custom.id, self.labels['reading'].id})
        self.assertEqual(self.labels['reading'].name, 'Читаем вместе')
        self.h.channels[13].edit.assert_not_awaited()

    async def test_required_essay_forum_webhook_draft_becomes_essay_after_author_writes(self):
        row = await self.service.create_essay_space(self.h.guild, self.book['id'], 1)
        hook, = self.h.hooks.values()
        self.assertEqual(hook.send.await_args.kwargs['applied_tags'], [self.labels['draft']])
        thread = self.h.channels[row['source_id']]
        self.assertEqual(self.ids(thread), {self.labels['draft'].id})
        custom = tag(14099, 'Личное')
        self.h.channels[14].available_tags.append(custom)
        thread.applied_tags.append(custom)
        self.h.seq += 1
        body = self.h.message(thread, self.h.seq, 'Мои мысли о книге')
        body.author = self.h.members[1]
        self.assertTrue(await self.service.register_thread(thread, prompt=False))
        self.assertEqual(self.ids(thread), {custom.id, self.labels['essay'].id})
        self.assertTrue(self.store.essays(self.book['id'])[0]['submitted'])

    async def test_required_essay_forum_bot_fallback_creates_tagged_draft(self):
        self.store.configure(1, {**self.store.settings(1), 'essay_webhooks': False})
        row = await self.service.create_essay_space(self.h.guild, self.book['id'], 1)
        forum = self.h.channels[14]
        self.assertEqual(forum.create_thread.await_args.kwargs['applied_tags'], [self.labels['draft']])
        self.assertEqual(self.ids(self.h.channels[row['source_id']]), {self.labels['draft'].id})
        self.assertFalse(self.h.hooks)

    async def test_import_starter_sets_essay_and_archive_for_webhook_and_bot(self):
        forum = self.h.channels[14]
        expected = [self.labels['essay'], self.labels['imported']]
        for webhook in (True, False):
            with self.subTest(webhook=webhook):
                self.store.configure(1, {**self.store.settings(1), 'essay_webhooks': webhook})
                key = f'essay-import:1:source-{webhook}'
                await self.service.publish_essay_starter(self.h.guild, forum, key,
                                                        'Архивный текст', 'Эссе', self.h.members[1])
                if webhook:
                    hook, = self.h.hooks.values()
                    self.assertEqual(hook.send.await_args.kwargs['applied_tags'], expected)
                else:
                    self.assertEqual(forum.create_thread.await_args.kwargs['applied_tags'], expected)

    async def test_missing_tag_fails_before_reserving_then_repair_allows_retry(self):
        forum = self.h.channels[13]
        proposed = self.labels['proposed']
        forum.available_tags.remove(proposed)
        key = f'book:{self.book["id"]}'
        with self.assertRaisesRegex(ClubError, '/club setup'):
            await self.service.refresh(self.h.guild)
        self.assertIsNone(self.store.publication(key))
        forum.available_tags.append(proposed)
        await self.service.refresh(self.h.guild)
        self.assertTrue(self.store.publication(key)['message_id'])


if __name__ == '__main__':
    unittest.main()

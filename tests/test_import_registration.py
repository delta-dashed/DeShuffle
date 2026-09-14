"""Imported essays retain authenticated provenance during scans and correction."""
import asyncio
from types import SimpleNamespace
import unittest

from bookclub.import_store import ImportStore
from bookclub.store import ClubError, Store
from bookclub.ui import Club
from test_bookclub import ClubFixture
from test_webhook_discord import WebhookHarness


class ImportRegistrationTests(ClubFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.h = WebhookHarness()
        self.cog = Club(self.h.bot, self.store)
        self.service = self.cog.service
        self.source_id = 20000

    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 1.0

    async def imported(self, *, webhook=True):
        self.source_id += 1
        self.store.configure(1, dict(self.store.settings(1), essay_webhooks=webhook))
        ledger = ImportStore(self.store)
        run = ledger.reserve_run(1, 99, 12, 'registration-tests', str(self.source_id), 100, 1, 100, {})
        ledger.save_plan(1, run['id'], {})
        ledger.claim_apply(1, run['id'])
        key = f'essay-import:1:{self.source_id}'
        ledger.claim_sources(1, run['id'], key, [self.source_id])
        header = '**Архивное эссе по книге «Книга»**\nАвтор: <@1>.\n[Оригинал](https://discord.com/channels/1/12/55)'
        pub = await self.service.publish_essay_starter(
            self.h.guild, self.h.channels[14], key, 'Книга · Эссе · Участник 1', header, self.h.members[1])
        thread = self.h.channels[pub['channel_id']]
        self.store.register_essay(1, self.book['id'], thread.id, thread.id, 1,
                                  thread.name, thread.jump_url, managed=False, submitted=True)
        ledger.finish_sources(1, key, thread.id)
        ledger.finish_apply(1, run['id'])
        return thread, key

    def row(self, thread):
        return self.store.one('SELECT * FROM bc_essays WHERE guild_id=1 AND source_id=?', (thread.id,))

    def context(self, author_id):
        return SimpleNamespace(guild=self.h.guild, author=self.h.members[author_id],
                               interaction=self.h.interaction(author_id))

    async def test_reconciliation_and_restart_preserve_real_author_and_submission(self):
        for webhook in (False, True):
            with self.subTest(webhook=webhook):
                thread, key = await self.imported(webhook=webhook)
                thread.name = 'Ручное название без книги и имени'
                starter = thread.messages[thread.id]
                if webhook:
                    starter.author.display_name = 'Совсем другой человек'
                self.assertTrue(await self.service.register_thread(thread))
                restored = Club(self.h.bot, Store(self.path, clock=lambda: self.now))
                self.assertTrue(await restored.service.register_thread(thread))
                row = self.row(thread)
                self.assertEqual((row['author_id'], row['managed'], row['submitted'], row['book_id']),
                                 (1, 0, 1, self.book['id']))
                self.assertEqual(row['title'], thread.name)
                self.assertIn('-# bc:' + key, starter.content)

    async def test_correction_allows_real_author_or_organizer_and_preserves_provenance(self):
        thread, key = await self.imported()
        second = self.store.create_book(1, 'Другая книга', 'Автор', '', 'second')
        with self.assertRaises(ClubError):
            await Club.essay.callback(self.cog, self.context(2), second['id'], thread.jump_url, True)
        with self.assertRaises(ClubError):
            await Club.essay.callback(self.cog, self.context(1), second['id'], thread.jump_url, False)
        self.assertEqual(self.row(thread)['book_id'], self.book['id'])
        await Club.essay.callback(self.cog, self.context(1), second['id'], thread.jump_url, True)
        row = self.row(thread)
        self.assertEqual((row['book_id'], row['author_id'], row['managed'], row['submitted']),
                         (second['id'], 1, 0, 1))
        starter = thread.messages[thread.id]
        self.assertIn('«Другая книга»', starter.content)
        self.assertIn('Автор: <@1>', starter.content)
        self.assertIn('https://discord.com/channels/1/12/55', starter.content)
        self.assertIn('-# bc:' + key, starter.content)
        self.assertEqual(ImportStore(self.store).imported_source(1, self.source_id)['thread_id'], thread.id)
        await Club.essay.callback(self.cog, self.context(99), self.book['id'], thread.jump_url, True)
        self.assertEqual((self.row(thread)['book_id'], self.row(thread)['author_id']), (self.book['id'], 1))

    async def test_forged_or_incomplete_provenance_cannot_update_imported_record(self):
        for corruption in ('marker', 'webhook', 'channel', 'guild', 'publication', 'ledger'):
            with self.subTest(corruption=corruption):
                thread, key = await self.imported()
                before = self.row(thread)
                thread.name = 'Пытаемся заменить сохранённое название'
                starter = thread.messages[thread.id]
                if corruption == 'marker':
                    starter.content = starter.content.replace('-# bc:' + key, '-# bc:fake')
                elif corruption == 'webhook':
                    starter.webhook_id += 1000
                else:
                    with self.store.tx() as db:
                        if corruption == 'channel':
                            db.execute('UPDATE bc_publications SET channel_id=14 WHERE key=?', (key,))
                        elif corruption == 'guild':
                            db.execute('UPDATE bc_publications SET guild_id=2 WHERE key=?', (key,))
                        elif corruption == 'publication':
                            db.execute('DELETE FROM bc_publications WHERE key=?', (key,))
                        elif corruption == 'ledger':
                            db.execute('DELETE FROM bc_import_sources WHERE item_key=?', (key,))
                self.assertFalse(await self.service.register_thread(thread))
                self.assertEqual(self.row(thread), before)
                with self.assertRaises(ClubError):
                    await Club.essay.callback(self.cog, self.context(99), self.book['id'], thread.jump_url, True)
                self.assertEqual(self.row(thread), before)

    async def test_completed_source_marker_cannot_invent_missing_real_author_record(self):
        thread, _ = await self.imported()
        with self.store.tx() as db:
            db.execute('DELETE FROM bc_essays WHERE guild_id=1 AND source_id=?', (thread.id,))
        self.assertFalse(await self.service.register_thread(thread))
        self.assertIsNone(self.row(thread))
        self.assertTrue(ImportStore(self.store).imported_source(1, self.source_id)['thread_id'])

    async def test_deleted_import_starter_uses_existing_deletion_lifecycle(self):
        thread, _ = await self.imported()
        del thread.messages[thread.id]
        self.assertFalse(await self.service.register_thread(thread))
        self.assertEqual(self.row(thread)['deleted'], 1)
        self.assertEqual(self.store.essays(self.book['id']), [])
        self.assertEqual(ImportStore(self.store).imported_source(1, self.source_id)['thread_id'], thread.id)


if __name__ == '__main__':
    unittest.main()

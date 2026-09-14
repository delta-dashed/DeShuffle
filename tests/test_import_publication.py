"""Replacement verification must finish before a legacy archive copy can retire."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from bookclub.import_publication import ImportPublisher
from bookclub.import_store import ImportStore
from bookclub.service import Service
from bookclub.store import ClubError
from test_archive_import import ArchiveHarness
from test_bookclub import ClubFixture


class ImportPublicationTests(ClubFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.h = ArchiveHarness()
        self.service = Service(self.h.bot, self.store)
        self.publisher = ImportPublisher(self.service)
        self.thread = self.h.channel(80, parent_id=14)
        self.key = 'essay-import:1:61:message:61:0'
        self.body = 'Сохранённый текст эссе'
        self.store.configure(1, {**self.store.settings(1), 'essay_webhooks': False})
        self.imports = ImportStore(self.store)
        self.run = self.imports.reserve_run(1, 99, 41, 'legacy-publication', 'first', 1, 100, 100, {})

    async def saved_body(self, key=None, body=None):
        key, body = key or self.key, body or self.body
        pub = await self.service.upsert(self.h.guild, key, self.thread.id, body)
        return pub, self.thread.messages[pub['message_id']]

    async def test_changed_legacy_attachment_is_rechecked_before_delete_and_audit(self):
        _, old = await self.saved_body()
        old.attachments = [SimpleNamespace(filename='essay.txt', size=11)]
        replacement, _ = await self.saved_body(self.key + ':v2')
        # Simulate an attachment modification after the restyle preflight.
        old.attachments[0].size = 12
        with self.assertRaisesRegex(ClubError, 'Вложения прежней копии изменились'):
            await self.publisher.retire_legacy(self.h.guild, self.run['id'], 99, self.thread,
                                               self.key, replacement, self.body,
                                               expected_attachments=[('essay.txt', 11)])
        old.delete.assert_not_awaited()
        self.assertIsNotNone(self.store.publication(self.key))
        self.assertEqual(self.store.rows('SELECT * FROM bc_import_restyle_audit'), [])

    async def test_extra_legacy_attachment_is_not_discarded_when_expected_files_empty(self):
        _, old = await self.saved_body()
        old.attachments = [SimpleNamespace(filename='added-later.txt', size=9)]
        replacement, _ = await self.saved_body(self.key + ':v2')
        with self.assertRaises(ClubError):
            await self.publisher.retire_legacy(self.h.guild, self.run['id'], 99, self.thread,
                                               self.key, replacement, self.body, expected_attachments=[])
        old.delete.assert_not_awaited()
        self.assertIs(self.thread.messages[old.id], old)

    async def test_confirmed_exact_legacy_files_can_retire_idempotently(self):
        _, old = await self.saved_body()
        old.attachments = [SimpleNamespace(filename='b.txt', size=9), SimpleNamespace(filename='a.txt', size=5)]
        replacement, _ = await self.saved_body(self.key + ':v2')
        budget = self.imports.budget('legacy-publication')
        for _ in range(2):
            await self.publisher.retire_legacy(self.h.guild, self.run['id'], 99, self.thread,
                                               self.key, replacement, self.body,
                                               expected_attachments=[('a.txt', 5), ('b.txt', 9)])
        old.delete.assert_awaited_once()
        self.assertNotIn(old.id, self.thread.messages)
        self.assertIsNone(self.store.publication(self.key))
        audits = self.store.rows('SELECT action,old_message_id,new_message_id FROM bc_import_restyle_audit ORDER BY id')
        self.assertEqual([row['action'] for row in audits], ['body-replacement-prepared', 'body-replaced'])
        self.assertTrue(all(row['old_message_id'] == old.id and row['new_message_id'] == replacement['message_id']
                            for row in audits))
        self.assertEqual(self.imports.budget('legacy-publication'), budget)

    async def test_fallback_hash_hit_fetches_and_verifies_real_message(self):
        saved, message = await self.saved_body()
        message.attachments = [SimpleNamespace(filename='essay.txt', size=10)]
        self.thread.send.reset_mock()
        self.thread.fetch_message.reset_mock()
        result = await self.publisher.upsert(self.h.guild, self.thread, self.key, self.body, self.h.members[1],
                                             expected_attachments=[('essay.txt', 10)])
        self.assertEqual(result['message_id'], saved['message_id'])
        self.thread.fetch_message.assert_awaited_once_with(message.id)
        self.thread.send.assert_not_awaited()

    async def test_fallback_hash_hit_rejects_missing_or_changed_files(self):
        _, message = await self.saved_body()
        for files in ([], [SimpleNamespace(filename='essay.txt', size=11)],
                      [SimpleNamespace(filename='different.txt', size=10)]):
            message.attachments = files
            with self.subTest(files=files), self.assertRaisesRegex(ClubError, 'Вложения новой копии'):
                await self.publisher.upsert(self.h.guild, self.thread, self.key, self.body, self.h.members[1],
                                             expected_attachments=[('essay.txt', 10)])
        message.delete.assert_not_awaited()

    async def test_fallback_hash_hit_rejects_altered_content_marker_and_human_sender(self):
        _, message = await self.saved_body()
        original = message.content
        for content, author, webhook_id in (
                (self.body, self.h.bot.user, None),
                ('Изменённый текст\n-# bc:' + self.key, self.h.bot.user, None),
                (original, self.h.members[1], None),
                (original, self.h.bot.user, 123)):
            message.content, message.author, message.webhook_id = content, author, webhook_id
            with self.subTest(content=content, sender=author.id), self.assertRaisesRegex(ClubError, 'не совпадает'):
                await self.publisher.upsert(self.h.guild, self.thread, self.key, self.body, self.h.members[1],
                                             expected_attachments=[])
        message.edit.assert_not_awaited()
        message.delete.assert_not_awaited()

    async def test_fallback_foreign_forum_is_rejected_before_publication(self):
        self.thread.parent_id = 13
        with self.assertRaisesRegex(ClubError, 'настроенному форуму эссе'):
            await self.publisher.upsert(self.h.guild, self.thread, self.key, self.body, self.h.members[1])
        self.thread.send.assert_not_awaited()
        self.assertIsNone(self.store.publication(self.key))

    async def test_fallback_does_not_accept_webhook_ledger_or_forged_returned_binding(self):
        saved, message = await self.saved_body()
        with self.store.tx() as db:
            db.execute('UPDATE bc_publications SET webhook_id=123 WHERE key=?', (self.key,))
        with self.assertRaisesRegex(ClubError, 'отправлена вебхуком'):
            await self.publisher.upsert(self.h.guild, self.thread, self.key, self.body, self.h.members[1])
        with self.store.tx() as db:
            db.execute('UPDATE bc_publications SET webhook_id=NULL WHERE key=?', (self.key,))
        self.service.upsert = AsyncMock(return_value={**saved, 'channel_id': 999})
        with self.assertRaisesRegex(ClubError, 'Привязка новой копии'):
            await self.publisher.upsert(self.h.guild, self.thread, self.key, self.body, self.h.members[1])
        message.delete.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()

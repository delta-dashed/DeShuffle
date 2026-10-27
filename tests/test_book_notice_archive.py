"""Removed book cards retain original archive state across failed I/O."""
import asyncio
import json
from types import SimpleNamespace
import unittest

from bookclub.book_notice_archive import FIELD, remember_notice_archive, finish_notice_archive
from bookclub.book_removal import build_removal_plan, commit_removal_plan
from bookclub.service import Service
from bookclub.store import Store
from test_bookclub import ClubFixture
from test_bookclub_discord import DiscordHarness


class BookNoticeArchiveTests(ClubFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.h = DiscordHarness()
        self.store.set_published(1)
        self.service = Service(self.h.bot, self.store)
        self.addAsyncCleanup(self.service.close)

    def journal(self, legacy):
        table, field = ('bc_book_trash_audit', 'previous_state') if legacy else ('bc_book_removal_operations', 'after_state')
        row = self.store.one(f'SELECT {field} AS payload FROM {table} WHERE book_id=? ORDER BY rowid DESC LIMIT 1',
                             (self.book['id'],))
        return json.loads(row['payload'])

    async def scenario(self, *, legacy=False, restart=False, cancelled=False, archived=True):
        await self.service.refresh_book(self.h.guild, self.book['id'])
        publication = self.store.publication(f'book:{self.book["id"]}')
        thread = self.h.channels[publication['channel_id']]
        message = thread.messages[publication['message_id']]
        thread.archived = archived
        message.attachments = [SimpleNamespace(id=55, filename='book.epub', size=123)]
        if legacy:
            book = self.store.book(1, self.book['id'])
            self.store.remove_book(1, book['id'], expected_revision=book['revision'], actor_id=99)
        else:
            plan = build_removal_plan(self.store, 1, self.book['id'], 'keep')
            commit_removal_plan(self.store, 1, 99, plan)
        before = self.journal(legacy)
        original_edit = thread.edit.side_effect
        fail = [archived]
        seen = []

        async def edit(**kwargs):
            if kwargs.get('archived') is False:
                # A fresh database connection sees the journal before the
                # first externally visible operation.
                fresh = Store(self.path, clock=lambda: self.now)
                table, field = ('bc_book_trash_audit', 'previous_state') if legacy else ('bc_book_removal_operations', 'after_state')
                payload = fresh.one(f'SELECT {field} AS data FROM {table} WHERE book_id=? ORDER BY rowid DESC LIMIT 1',
                                    (self.book['id'],))['data']
                seen.append(json.loads(payload)[FIELD]['original_archived'])
            if kwargs.get('archived') is True and fail[0]:
                fail[0] = False
                if cancelled:
                    raise asyncio.CancelledError()
                raise OSError('simulated rearchive failure')
            return await original_edit(**kwargs)

        thread.edit.side_effect = edit
        if archived:
            error = asyncio.CancelledError if cancelled else OSError
            with self.assertRaises(error):
                await self.service.refresh_book(self.h.guild, self.book['id'])
            self.assertFalse(thread.archived)
            self.assertTrue(self.journal(legacy)[FIELD]['original_archived'])
            self.assertEqual(self.store.publication(publication['key'])['content_hash'], publication['content_hash'])
        if restart:
            self.store = Store(self.path, clock=lambda: self.now)
            self.service = Service(self.h.bot, self.store)
            self.addAsyncCleanup(self.service.close)
        await self.service.refresh_book(self.h.guild, self.book['id'])
        self.assertEqual(thread.archived, archived)
        self.assertEqual(before, self.journal(legacy))
        self.assertEqual(message.attachments[0].id, 55)
        self.assertEqual(self.store.publication(publication['key'])['message_id'], message.id)
        self.assertEqual(self.h.channels[13].create_thread.await_count, 1)
        message.delete.assert_not_awaited()
        self.assertEqual(seen, [True] if archived else [])
        writes = message.edit.await_count
        await self.service.refresh_book(self.h.guild, self.book['id'])
        self.assertEqual(message.edit.await_count, writes)

    async def test_failed_rearchive_retries_both_removal_journals(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                # A separate source book avoids overlapping removals.
                if legacy:
                    self.book = self.store.create_book(1, 'Другой источник', 'Автор', '', 'legacy')
                    self.h.channels[13].create_thread.reset_mock()
                await self.scenario(legacy=legacy)

    async def test_restart_restores_original_archive(self):
        await self.scenario(restart=True)

    async def test_cancellation_and_restart_restore_original_archive(self):
        await self.scenario(restart=True, cancelled=True)

    async def test_initially_open_book_stays_open(self):
        await self.scenario(archived=False)

    async def test_legacy_removal_after_restore_uses_its_own_journal(self):
        await self.scenario(archived=False)
        previous = self.journal(False)
        book = self.store.book(1, self.book['id'])
        self.store.restore_book(1, book['id'], expected_revision=book['revision'], actor_id=99)
        book = self.store.book(1, self.book['id'])
        self.store.remove_book(1, book['id'], expected_revision=book['revision'], actor_id=99)
        publication = self.store.publication(f'book:{book["id"]}')
        journal = remember_notice_archive(self.store, 1, book['id'], publication, True)
        self.assertEqual(journal['table'], 'bc_book_trash_audit')
        self.assertEqual(self.journal(False), previous)
        self.assertTrue(self.journal(True)[FIELD]['original_archived'])
        finish_notice_archive(self.store, 1, book['id'], publication, journal)
        self.assertNotIn(FIELD, self.journal(True))


if __name__ == '__main__':
    unittest.main()

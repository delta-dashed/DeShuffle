"""Archive inventory and human metadata approval do not spend model quota."""
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import discord

from bookclub.archive_import import ArchiveImporter
from bookclub.import_config import ImportConfig
from bookclub.import_preparation import ImportPreparation, PreparationStore
from bookclub.service import Service
from bookclub.store import ClubError, Store
from test_archive_import import ArchiveHarness
from test_bookclub import ClubFixture
from test_bookclub_discord import iterate, not_found


class PreparationTests(ClubFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.h = ArchiveHarness()
        self.config = ImportConfig(enabled=True, allowed_user_ids=(99,), allowed_guild_ids=(1,),
                                   allowed_channel_ids=(41,), max_threads=1, max_input_bytes=2000)
        self.runner = SimpleNamespace(login_status=AsyncMock(), analyze=AsyncMock(), close=AsyncMock())
        self.importer = ArchiveImporter(Service(self.h.bot, self.store), self.config, self.runner)
        self.preparation = ImportPreparation(self.importer)

    async def inventory(self):
        return await self.preparation.inventory(self.h.guild, 99, 41)

    async def select(self, ident=51, decision='included', **fields):
        return await self.preparation.select(self.h.guild, 99, 41, ident, decision, **fields)

    def assert_no_provider(self):
        self.runner.login_status.assert_not_awaited()
        self.runner.analyze.assert_not_awaited()

    def completed_run(self):
        snapshot = {'source_id': '41', 'books': [], 'messages': [
            {'id': str(message.id), 'channel_id': '51'} for message in (self.h.first, self.h.second)]}
        ledger = self.importer.ledger
        run = ledger.reserve_run(1, 99, 41, self.config.budget_id, 'legacy', 1, 30714, 100_000, snapshot)
        ledger.save_plan(1, run['id'], {'essays': []})
        ledger.claim_apply(1, run['id'])
        ledger.claim_sources(1, run['id'], 'old-item', [61, 62])
        ledger.finish_sources(1, 'old-item', 999999)
        ledger.finish_apply(1, run['id'])
        return run['id']

    async def test_all_public_archived_pages_are_consumed_despite_model_limits(self):
        for ident in range(100, 230):
            thread = self.h.channel(ident, parent_id=41, name=f'Архив {ident}')
            thread.archived = True
        private = self.h.channel(300, parent_id=41)
        private.is_private.return_value = True
        self.h.channel(301, parent_id=12)
        result = await self.inventory()
        self.h.source.archived_threads.assert_called_once_with(limit=None)
        self.assertTrue(result['complete'])
        self.assertEqual(result['active_count'], 1)
        self.assertEqual(result['archived_count'], 130)
        self.assertEqual(len(result['threads']), 131)
        self.assertEqual({row['decision'] for row in result['threads']}, {'pending'})
        self.assertGreater(len(json.dumps(result, ensure_ascii=False).encode()), self.config.max_input_bytes)
        self.assert_no_provider()

    async def test_archive_iteration_failure_is_reported_without_losing_seen_threads(self):
        archived = self.h.channel(52, parent_id=41)
        archived.archived = True
        async def partial(**kwargs):
            yield archived
            raise not_found()
        self.h.source.archived_threads.side_effect = partial
        result = await self.inventory()
        self.assertFalse(result['complete'])
        self.assertEqual({row['id'] for row in result['threads']}, {'51', '52'})
        self.assertIn('архивные', result['warnings'][0])

    async def test_active_iteration_failure_does_not_claim_full_coverage(self):
        self.h.guild.active_threads.side_effect = not_found()
        result = await self.inventory()
        self.assertFalse(result['complete'])
        self.assertIn('активные', result['warnings'][0])

    async def test_firstline_never_becomes_book_metadata_or_selection(self):
        raw = 'Девять миллиардов имён Бога — Артур Кларк'
        self.h.source.messages[51].content = raw + '\nСюда эссе.'
        self.h.old_thread.name = 'Сочинения'
        before = self.store.books(1)
        row = (await self.inventory())['threads'][0]
        self.assertEqual(row['source_title'], raw)
        self.assertIsNone(row['title'])
        self.assertIsNone(row['author'])
        self.assertEqual(row['decision'], 'pending')
        self.assertEqual(self.store.books(1), before)
        self.assertEqual(self.preparation.store.selections(1, 41), [])

    async def test_selection_requires_confirmation_and_explicit_metadata(self):
        for fields in ({}, {'confirm': True}, {'title': 'Книга', 'confirm': True},
                       {'title': '', 'author': 'Автор', 'confirm': True}):
            with self.subTest(fields=fields), self.assertRaises(ClubError):
                await self.select(**fields)
        result = await self.select(title='Девять миллиардов имён Бога', author='Артур Кларк', confirm=True)
        self.assertEqual(result['title'], 'Девять миллиардов имён Бога')
        self.assertEqual(result['author'], 'Артур Кларк')
        self.assertIsNone(result['book_id'])
        self.assertEqual(len(self.store.books(1)), 1)
        self.assertEqual(self.preparation.store.revision(1, 41), 1)

    async def test_existing_book_mapping_is_guild_scoped_and_survives_thread_rename(self):
        result = await self.select(book_id=self.book['id'], confirm=True)
        self.assertEqual(result['title'], self.book['title'])
        self.h.old_thread.name = 'Полностью новое имя'
        self.h.source.messages[51].content = 'И новое стартовое сообщение'
        row = (await self.inventory())['threads'][0]
        self.assertEqual(row['name'], 'Полностью новое имя')
        self.assertEqual(row['title'], self.book['title'])
        self.assertEqual(row['book_id'], self.book['id'])
        self.store.configure(2, self.store.settings(1))
        other = self.store.create_book(2, 'Другая', 'Автор', '', 'other')
        with self.assertRaises(ClubError):
            await self.select(book_id=other['id'], confirm=True)
        with self.assertRaises(ClubError):
            await self.select(book_id=self.book['id'], title='Иное', confirm=True)

    async def test_nonbook_post_is_explicitly_excluded_without_touching_discord(self):
        self.h.old_thread.name = '@everyone а можно завтра после ММа'
        with self.assertRaisesRegex(ClubError, 'confirm:true'):
            await self.select(decision='excluded')
        result = await self.select(decision='excluded', confirm=True)
        self.assertEqual(result['decision'], 'excluded')
        row = (await self.inventory())['threads'][0]
        self.assertEqual(row['decision'], 'excluded')
        self.assertIsNone(row['title'])
        self.h.old_thread.edit.assert_not_awaited()
        self.h.source.messages[51].edit.assert_not_awaited()
        self.assert_no_provider()

    async def test_completed_sources_use_original_thread_and_preserve_budget_history(self):
        run_id = self.completed_run()
        before = {table: self.store.rows(f'SELECT * FROM {table}') for table in
                  ('bc_import_runs', 'bc_import_budgets', 'bc_import_sources')}
        row = (await self.inventory())['threads'][0]
        self.assertEqual(row['id'], '51')
        self.assertEqual(row['imported_messages'], 2)
        self.assertEqual(row['status'], 'already_imported')
        self.assertEqual(row['decision'], 'excluded')
        # An explicit reviewed mapping can inspect the remaining discussion.
        await self.select(book_id=self.book['id'], confirm=True)
        row = (await self.inventory())['threads'][0]
        self.assertEqual(row['decision'], 'included')
        self.assertEqual(row['imported_messages'], 2)
        for table, rows in before.items():
            self.assertEqual(self.store.rows(f'SELECT * FROM {table}'), rows)
        self.assertEqual(self.importer.ledger.run(1, run_id)['state'], 'done')
        self.assert_no_provider()

    async def test_no_access_entry_is_visible_but_cannot_be_selected(self):
        self.h.old_thread.permissions_for.return_value = SimpleNamespace(view_channel=True, read_message_history=False)
        row = (await self.inventory())['threads'][0]
        self.assertFalse(row['access'])
        self.assertIn('доступа', row['access_error'])
        with self.assertRaisesRegex(ClubError, 'доступа'):
            await self.select(book_id=self.book['id'], confirm=True)
        self.assertEqual(self.preparation.store.revision(1, 41), 0)

    async def test_private_thread_and_changed_source_parent_are_rejected(self):
        self.h.old_thread.is_private.return_value = True
        with self.assertRaisesRegex(ClubError, 'публичный'):
            await self.select(decision='excluded', confirm=True)
        self.h.old_thread.is_private.return_value = False
        self.h.old_thread.parent_id = 12
        with self.assertRaisesRegex(ClubError, 'публичный'):
            await self.select(decision='excluded', confirm=True)

    async def test_switch_and_allowlist_protect_inventory_and_selection(self):
        for config in (replace(self.config, enabled=False), replace(self.config, allowed_user_ids=(1,)),
                       replace(self.config, allowed_channel_ids=(42,))):
            self.importer.config = config
            with self.subTest(config=config), self.assertRaises(ClubError):
                await self.inventory()
        self.h.source.archived_threads.assert_not_called()
        self.assert_no_provider()

    async def test_coverage_persists_without_changing_selection_revision(self):
        await self.select(book_id=self.book['id'], confirm=True)
        revision = self.preparation.store.revision(1, 41)
        self.preparation.store.mark_coverage(1, 41, 51, 'limited')
        row = (await self.inventory())['threads'][0]
        self.assertEqual(row['status'], 'not_viewed_due_to_limit')
        self.assertEqual(row['capture_state'], 'limited')
        self.assertEqual(self.preparation.store.revision(1, 41), revision)
        await self.select(book_id=self.book['id'], confirm=True)
        self.assertEqual(self.preparation.store.revision(1, 41), revision)
        await self.select(title='Уточнённое название', author='Автор', confirm=True)
        self.assertEqual(self.preparation.store.revision(1, 41), revision + 1)
        self.assertEqual((await self.inventory())['threads'][0]['capture_state'], 'unread')
        self.assertEqual(PreparationStore(Store(self.path)).revision(1, 41), revision + 1)

    async def test_missing_previous_selection_prevents_complete_claim(self):
        await self.select(book_id=self.book['id'], confirm=True)
        del self.h.channels[51]
        result = await self.inventory()
        self.assertFalse(result['complete'])
        self.assertIn('51', result['warnings'][0])

    def test_cursor_is_durable_and_scoped_to_guild_and_source(self):
        payload = {'threads': ['51', '52'], 'index': 0, 'after': 61, 'revision': 0}
        self.preparation.store.save_cursor(1, 41, 'random-token', payload)
        restored = PreparationStore(Store(self.path))
        self.assertEqual(restored.load_cursor(1, 41, 'random-token'), payload)
        for guild_id, source_id in ((2, 41), (1, 42)):
            with self.assertRaises(ClubError):
                restored.load_cursor(guild_id, source_id, 'random-token')
        self.assertEqual(restored.revision(1, 41), 0)
        self.assertEqual(self.importer.ledger.budget(self.config.budget_id)['runs_used'], 0)

    def test_earlier_capture_evidence_is_not_downgraded_by_new_page_limits(self):
        for first in ('partial', 'complete'):
            with self.subTest(first=first):
                self.preparation.store.mark_coverage(1, 41, 51, first)
                self.preparation.store.mark_coverage(1, 41, 51, 'limited')
                self.assertEqual(self.preparation.store.coverage(1, 41)['51'], first)

    async def test_finalize_stale_selection_rolls_back_cursor_and_coverage(self):
        revision = self.preparation.store.revision(1, 41)
        await self.select(book_id=self.book['id'], confirm=True)
        with self.assertRaisesRegex(ClubError, 'Выбор книг изменился'):
            self.preparation.store.finalize(1, 41, revision, {'51': 'complete'},
                                            token='stale', payload={'revision': revision})
        with self.assertRaises(ClubError):
            self.preparation.store.load_cursor(1, 41, 'stale')
        self.assertEqual(self.preparation.store.coverage(1, 41), {})

    def test_finalize_persists_cursor_and_coverage_together(self):
        self.preparation.store.finalize(1, 41, 0, {'51': 'partial', '52': 'limited'},
                                        token='page', payload={'index': 0, 'after': 61})
        self.assertEqual(self.preparation.store.load_cursor(1, 41, 'page'), {'index': 0, 'after': 61})
        self.assertEqual(self.preparation.store.coverage(1, 41), {'51': 'partial', '52': 'limited'})
        with self.assertRaises(ClubError):
            self.preparation.store.finalize(1, 41, 0, {'51': 'complete', '53': 'invalid'},
                                            token='bad', payload={})
        self.assertEqual(self.preparation.store.coverage(1, 41), {'51': 'partial', '52': 'limited'})
        with self.assertRaises(ClubError):
            self.preparation.store.load_cursor(1, 41, 'bad')


if __name__ == '__main__':
    unittest.main()

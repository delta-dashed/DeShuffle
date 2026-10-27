"""Bounded archive previews never spend model quota or read unrelated history."""
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import discord

from bookclub.archive_import import ArchiveImporter, validate_plan
from bookclub.import_preparation import PreparationStore
from bookclub.service import Service
from bookclub.store import ClubError
from test_import_recovery import RecoveryFixture


class ImportCaptureTests(RecoveryFixture, unittest.IsolatedAsyncioTestCase):
    async def test_selected_thread_bypasses_unrelated_history_and_thread_inventories(self):
        self.h.guild.active_threads.side_effect = AssertionError('unrelated inventory')
        self.h.source.archived_threads.side_effect = AssertionError('unrelated archives')
        self.h.source.history = Mock(side_effect=AssertionError('unrelated root history'))
        snapshot = await self.importer.preview(self.h.guild, 99, 41, thread_id=51)
        self.assertEqual({m['id'] for m in snapshot['messages']}, {'61', '62', '63'})
        self.h.guild.active_threads.assert_not_awaited()
        self.h.source.archived_threads.assert_not_called()
        self.h.source.history.assert_not_called()
        self.h.source.fetch_message.assert_not_awaited()

    async def test_large_unrelated_catalog_is_excluded_from_selected_thread_context(self):
        for i in range(40):
            self.store.create_book(1, f'Другая книга {i}' + 'я' * 150, 'Автор' + 'ь' * 160, '', str(i))
        self.importer.config = replace(self.config, max_input_bytes=3000)
        snapshot = await self.importer.preview(self.h.guild, 99, 41, thread_id=51)
        self.assertEqual([b['book_id'] for b in snapshot['books']], [self.book['id']])
        self.assertLessEqual(len(json.dumps(snapshot, ensure_ascii=False).encode()), 3000)

    async def test_selected_thread_honors_both_exclusive_message_bounds(self):
        snapshot = await self.importer.preview(self.h.guild, 99, 41, thread_id=51, after_id=61, before_id=63)
        self.assertEqual([m['id'] for m in snapshot['messages']], ['62'])

    async def test_invalid_range_fails_without_reading_any_history(self):
        for options in ({'thread_id': -1}, {'thread_id': 51, 'after_id': 63, 'before_id': 62}, {'after_id': 61}):
            with self.subTest(options=options), self.assertRaises(ClubError):
                await self.importer.preview(self.h.guild, 99, 41, **options)
        self.h.guild.active_threads.assert_not_awaited()
        self.h.source.fetch_message.assert_not_awaited()

    async def test_warning_and_continuation_json_fits_exact_utf8_limit(self):
        self.h.first.content = 'Эссе' * 100
        self.h.second.content = 'Мысли' * 100
        self.h.discussion.content = 'Обсуждение' * 100
        self.importer.config = replace(self.config, max_input_bytes=3100)
        snapshot = await self.importer.preview(self.h.guild, 99, 41, thread_id=51)
        self.assertLessEqual(len(json.dumps(snapshot, ensure_ascii=False).encode()), 3100)
        self.assertEqual(snapshot['messages'][0]['content'], self.h.first.content)
        self.assertLess(len(snapshot['messages']), 3)
        warnings = '\n'.join(snapshot['warnings'])
        self.assertEqual(snapshot['coverage']['after'], snapshot['messages'][-1]['id'])
        self.assertIsInstance(snapshot['continuation'], str)
        self.assertIn('/club import preview', warnings)
        self.assertNotIn('/club import scan', warnings)
        self.runner.analyze.assert_not_awaited()

    async def test_preview_works_after_exhausted_quota_and_without_codex_login(self):
        await self.failed()
        budget = self.importer.ledger.budget(self.config.budget_id)
        snapshot = await self.importer.preview(self.h.guild, 99, 41, thread_id=51)
        self.assertEqual(len(snapshot['messages']), 3)
        self.assertEqual(self.importer.ledger.budget(self.config.budget_id), budget)
        self.runner.login_status.assert_not_awaited()
        self.runner.analyze.assert_not_awaited()
        self.assertEqual(len(self.store.rows('SELECT id FROM bc_import_runs')), 1)

    async def test_bot_without_selected_thread_history_gets_precise_error(self):
        allowed = self.h.old_thread.permissions_for.return_value
        self.h.old_thread.permissions_for.side_effect = lambda member: (
            SimpleNamespace(view_channel=True, read_message_history=False) if member.id == 9999 else allowed)
        with self.assertRaisesRegex(ClubError, 'истории выбранного треда'):
            await self.importer.preview(self.h.guild, 99, 41, thread_id=51)
        self.h.old_thread.fetch_message.assert_not_awaited()
        self.assertEqual(self.importer.ledger.budget(self.config.budget_id)['runs_used'], 0)

    async def test_no_partial_text_is_sent_when_one_message_exceeds_input_limit(self):
        self.h.first.content = 'x' * 10_000
        self.importer.config = replace(self.config, max_input_bytes=2000)
        with self.assertRaisesRegex(ClubError, '2000 байт'):
            await self.importer.preview(self.h.guild, 99, 41, thread_id=51)
        self.runner.analyze.assert_not_awaited()
        self.assertEqual(self.importer.ledger.budget(self.config.budget_id)['runs_used'], 0)

    async def test_closed_import_switch_blocks_free_preview_too(self):
        self.importer.config = replace(self.config, enabled=False)
        with self.assertRaisesRegex(ClubError, 'отключён'):
            await self.importer.preview(self.h.guild, 99, 41, thread_id=51)
        self.h.bot.fetch_channel.assert_not_awaited()

    async def test_unselected_root_messages_are_not_automatically_treated_as_essays(self):
        for ident in range(100, 110):
            self.h.human_message(self.h.source, ident, 'Эссе из корневого канала ' + 'Ж' * 150)
        snapshot = await self.importer.preview(self.h.guild, 99, 41)
        self.assertEqual({m['id'] for m in snapshot['messages']}, {'61', '62', '63'})
        self.assertTrue(snapshot['coverage']['complete'])
        self.runner.analyze.assert_not_awaited()

    async def test_changed_starter_and_thread_name_cannot_override_confirmed_book_metadata(self):
        self.h.source.messages[51].content = 'Девять миллиардов имён Бога — Артур Кларк\nИное описание'
        self.h.old_thread.name = 'Сочинения'
        snapshot = await self.importer.preview(self.h.guild, 99, 41)
        self.assertEqual([(book['title'], book['author']) for book in snapshot['books']], [('Книга', 'Автор')])
        self.assertEqual({m['context_ref'] for m in snapshot['messages']}, {'book:' + self.book['id']})
        self.h.source.messages[51].edit.assert_not_awaited()

    async def additional_thread(self, thread_id, message_ids, *, decision='included', archived=False):
        thread = self.h.channel(thread_id, parent_id=41, name='Ещё одна книга')
        thread.archived = archived
        self.h.human_message(self.h.source, thread_id, 'Старый заголовок — не подтверждённый автор', 99)
        for ident in message_ids:
            self.h.human_message(thread, ident, f'Эссе {ident}: ' + 'Размышления. ' * 35, 2)
        if decision != 'pending':
            fields = {'book_id': self.book['id']} if decision == 'included' else {}
            await self.importer.preparation.select(self.h.guild, 99, 41, thread_id,
                                                   decision=decision, confirm=True, **fields)
        return thread

    async def collect_pages(self, **options):
        snapshots, seen_cursors = [], set()
        for _ in range(30):
            snapshot = await self.importer.preview(self.h.guild, 99, 41, **options)
            snapshots.append(snapshot)
            self.assertLessEqual(len(json.dumps(snapshot, ensure_ascii=False).encode()), self.importer.config.max_input_bytes)
            cursor = snapshot['continuation']
            if cursor is None:
                self.assertTrue(snapshot['coverage']['complete'])
                return snapshots
            self.assertNotIn(cursor, seen_cursors)
            seen_cursors.add(cursor)
            options = {'cursor': cursor}
        self.fail('Continuation never completed a finite archive')

    async def test_size_limit_inside_thread_then_next_thread_has_no_gaps_or_duplicates(self):
        await self.additional_thread(81, [91, 92, 93, 94], archived=True)
        for message in (self.h.first, self.h.second, self.h.discussion):
            message.content = 'Развёрнутое эссе. ' * 35
        self.importer.config = replace(self.config, max_input_bytes=3500)
        pages = await self.collect_pages()
        self.assertGreater(len(pages), 2)
        self.assertEqual(pages[0]['coverage']['current_thread'], '81')
        self.assertNotEqual(pages[0]['coverage']['after'], '94')
        ids = [m['id'] for page in pages for m in page['messages']]
        self.assertEqual(ids, ['91', '92', '93', '94', '61', '62', '63'])
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(pages[-1]['coverage']['completed_threads'], 2)
        self.runner.analyze.assert_not_awaited()
        self.runner.login_status.assert_not_awaited()
        self.assertEqual(self.importer.ledger.budget(self.config.budget_id)['runs_used'], 0)

    async def test_real_48000_byte_cap_covers_every_message_and_then_the_next_book_thread(self):
        await self.additional_thread(31, [32, 33], archived=True)
        for ident in range(64, 85):
            self.h.human_message(self.h.old_thread, ident, '', 1)
        for ident in range(61, 85):
            # Ordinary Discord-sized messages; Cyrillic must count as UTF-8 bytes.
            self.h.old_thread.messages[ident].content = f'Эссе {ident}: ' + 'Я' * 1890
        self.importer.config = replace(self.config, max_input_bytes=48_000)
        pages = await self.collect_pages()
        self.assertGreaterEqual(len(pages), 3)
        self.assertEqual(pages[0]['coverage']['current_thread'], '51')
        self.assertNotEqual(pages[0]['coverage']['after'], '84')
        ids = [message['id'] for page in pages for message in page['messages']]
        self.assertEqual(ids, [str(ident) for ident in range(61, 85)] + ['32', '33'])
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(pages[-1]['coverage']['completed_threads'], 2)
        self.assertEqual(self.store.rows('SELECT * FROM bc_import_runs'), [])
        self.assertEqual(self.importer.ledger.budget(self.config.budget_id)['runs_used'], 0)
        self.runner.analyze.assert_not_awaited()
        self.runner.login_status.assert_not_awaited()

    async def test_thread_page_limit_keeps_empty_bot_only_thread_and_later_thread_in_queue(self):
        await self.additional_thread(31, [])
        await self.additional_thread(21, [22], archived=True)
        self.importer.config = replace(self.config, max_threads=1)
        pages = await self.collect_pages()
        self.assertEqual(len(pages), 3)
        self.assertEqual(pages[1]['messages'], [])
        self.assertIsNotNone(pages[1]['continuation'])
        self.assertEqual([message['id'] for page in pages for message in page['messages']], ['61', '62', '63', '22'])
        self.assertEqual(pages[-1]['coverage']['completed_threads'], 3)

    async def test_cursor_rechecks_thread_permissions_visibility_and_parent_before_reading(self):
        self.importer.config = replace(self.config, max_messages=2)
        first = await self.importer.preview(self.h.guild, 99, 41)
        thread = self.h.old_thread
        thread.history = Mock(side_effect=AssertionError('Inaccessible cursor history read'))
        permissions = thread.permissions_for.return_value
        permissions.read_message_history = False
        with self.assertRaises(ClubError):
            await self.importer.preview(self.h.guild, 99, 41, cursor=first['continuation'])
        permissions.read_message_history = True
        thread.is_private.return_value = True
        with self.assertRaises(ClubError):
            await self.importer.preview(self.h.guild, 99, 41, cursor=first['continuation'])
        thread.is_private.return_value = False
        thread.parent_id = 42
        with self.assertRaises(ClubError):
            await self.importer.preview(self.h.guild, 99, 41, cursor=first['continuation'])
        thread.history.assert_not_called()
        self.runner.analyze.assert_not_awaited()

    async def test_raw_message_limit_at_thread_boundary_reaches_next_thread(self):
        await self.additional_thread(31, [32, 33])
        self.h.old_thread.messages.pop(51)
        self.h.old_thread.messages.pop(63)
        self.h.channels[31].messages.pop(31)
        self.importer.config = replace(self.config, max_messages=2)
        first = await self.importer.preview(self.h.guild, 99, 41)
        self.assertEqual(PreparationStore(self.store).coverage(1, 41), {'51': 'complete', '31': 'limited'})
        pages = [first] + await self.collect_pages(cursor=first['continuation'])
        self.assertEqual([m['id'] for m in pages[0]['messages']], ['61', '62'])
        self.assertEqual([m['id'] for page in pages for m in page['messages']], ['61', '62', '32', '33'])
        self.assertEqual(pages[-1]['coverage']['completed_threads'], 2)

    async def test_excluded_nonbook_and_pending_ambiguous_thread_are_never_read(self):
        excluded = await self.additional_thread(81, [91], decision='excluded')
        excluded.name = '@everyone а можно завтра после ММа'
        pending = await self.additional_thread(71, [72], decision='pending')
        pending.name = 'В это сообщение кидать все эссе долгой'
        pending_history = pending.history
        for thread in (excluded, pending):
            thread.history = Mock(side_effect=AssertionError('Unapproved essay history read'))
        snapshot = await self.importer.preview(self.h.guild, 99, 41)
        self.assertEqual({m['id'] for m in snapshot['messages']}, {'61', '62', '63'})
        self.assertEqual(snapshot['coverage']['total_threads'], 1)
        with self.assertRaises(ClubError):
            await self.importer.preview(self.h.guild, 99, 41, thread_id=excluded.id)
        for thread in (excluded, pending):
            thread.history.assert_not_called()
        pending.history = pending_history
        inspection = await self.importer.preview(self.h.guild, 99, 41, thread_id=pending.id)
        self.assertEqual(inspection['books'], [])
        self.assertEqual([m['id'] for m in inspection['messages']], ['72'])
        self.assertTrue(all(m['context_ref'] is None for m in inspection['messages']))
        self.assertFalse(inspection['preparation_confirmed'])
        self.assertEqual(len(self.store.books(1)), 1)

    async def test_pending_thread_raw_inspection_does_not_guess_book_from_first_line(self):
        thread = await self.additional_thread(71, [72, 73], decision='pending')
        self.h.source.messages[71].content = 'Девять миллиардов имён Бога — Артур Кларк'
        snapshot = await self.importer.preview(self.h.guild, 99, 41, thread_id=thread.id)
        self.assertEqual(snapshot['books'], [])
        self.assertFalse(snapshot['preparation_confirmed'])
        self.assertEqual({m['id'] for m in snapshot['messages']}, {'72', '73'})
        self.assertTrue(all(m['context_ref'] is None for m in snapshot['messages']))
        self.assertEqual(len(self.store.books(1)), 1)
        with self.assertRaises(ClubError):
            await self.importer.capture(self.h.guild, self.h.members[99], self.h.source, thread_id=71)
        self.runner.analyze.assert_not_awaited()
        self.runner.login_status.assert_not_awaited()
        self.assertEqual(self.importer.ledger.budget(self.config.budget_id)['runs_used'], 0)

    async def test_whole_source_requires_at_least_one_human_confirmed_thread(self):
        PreparationStore(self.store).save(1, 41, 51, 'pending', 99)
        self.h.old_thread.history = Mock(side_effect=AssertionError('Unconfirmed history read'))
        with self.assertRaisesRegex(ClubError, 'подтверждённых'):
            await self.importer.preview(self.h.guild, 99, 41)
        self.h.old_thread.history.assert_not_called()
        self.runner.analyze.assert_not_awaited()
        self.runner.login_status.assert_not_awaited()
        self.assertEqual(self.importer.ledger.budget(self.config.budget_id)['runs_used'], 0)

    async def test_inspection_cursor_cannot_be_used_for_model_capture(self):
        await self.additional_thread(71, [72, 73], decision='pending')
        self.importer.config = replace(self.config, max_messages=2)
        inspection = await self.importer.preview(self.h.guild, 99, 41, thread_id=71)
        self.assertIsNotNone(inspection['continuation'])
        with self.assertRaises(ClubError):
            await self.importer.capture(self.h.guild, self.h.members[99], self.h.source,
                                         cursor=inspection['continuation'])
        self.runner.analyze.assert_not_awaited()

    async def test_imported_source_messages_advance_cursor_and_preserve_done_history_and_budget(self):
        snapshot = await self.importer.preview(self.h.guild, 99, 41)
        ledger = self.importer.ledger
        run = ledger.reserve_run(1, 99, 41, self.config.budget_id, 'previously-completed', 1, 30714, 100_000, snapshot)
        plan = validate_plan(snapshot, {'essays': [{'book_ref': 'book:' + self.book['id'], 'message_ids': ['61', '62']}]})
        ledger.save_plan(1, run['id'], plan)
        ledger.claim_apply(1, run['id'])
        ledger.claim_sources(1, run['id'], 'old-essay', [61, 62])
        ledger.finish_sources(1, 'old-essay', 1001)
        ledger.finish_apply(1, run['id'])
        finished, budget = ledger.run(1, run['id']), ledger.budget(self.config.budget_id)
        bindings = self.store.rows('SELECT * FROM bc_import_sources')
        self.importer.config = replace(self.config, max_messages=2)
        pages = await self.collect_pages(thread_id=51)
        self.assertEqual(pages[0]['messages'], [])
        self.assertIsNotNone(pages[0]['continuation'])
        self.assertEqual([m['id'] for page in pages for m in page['messages']], ['63'])
        self.assertEqual(ledger.run(1, run['id']), finished)
        self.assertEqual(ledger.budget(self.config.budget_id), budget)
        self.assertEqual(self.store.rows('SELECT * FROM bc_import_sources'), bindings)
        self.runner.analyze.assert_not_awaited()
        self.runner.login_status.assert_not_awaited()

    async def test_cursor_survives_importer_restart_and_rejects_selection_change(self):
        self.importer.config = replace(self.config, max_messages=2)
        first = await self.importer.preview(self.h.guild, 99, 41)
        config = self.importer.config
        self.importer = ArchiveImporter(Service(self.h.bot, self.store), config, self.runner)
        second = await self.importer.preview(self.h.guild, 99, 41, cursor=first['continuation'])
        self.assertFalse({m['id'] for m in first['messages']} & {m['id'] for m in second['messages']})
        await self.importer.preparation.select(self.h.guild, 99, 41, 51, decision='included',
                                               title='Другая подтверждённая книга', author='Другой автор', confirm=True)
        with self.assertRaises(ClubError):
            await self.importer.preview(self.h.guild, 99, 41, cursor=first['continuation'])
        self.runner.analyze.assert_not_awaited()

    async def test_unknown_and_cross_source_cursor_rejected_without_reading_history(self):
        self.importer.config = replace(self.config, max_messages=2, allowed_channel_ids=(41, 42))
        first = await self.importer.preview(self.h.guild, 99, 41)
        source = self.h.channel(42, discord.TextChannel)
        source.history = Mock(side_effect=AssertionError('Cross-source history read'))
        self.h.old_thread.history = Mock(side_effect=AssertionError('Invalid cursor history read'))
        for source_id, cursor in ((41, '0' * 32), (42, first['continuation']), (41, '../private')):
            with self.subTest(source_id=source_id, cursor=cursor), self.assertRaises(ClubError):
                await self.importer.preview(self.h.guild, 99, source_id, cursor=cursor)
        source.history.assert_not_called()
        self.h.old_thread.history.assert_not_called()
        self.runner.analyze.assert_not_awaited()


    async def test_cursor_rejects_removed_book_before_read_or_model_and_can_resume_after_restore(self):
        self.importer.config = replace(self.config, max_messages=2)
        first = await self.importer.preview(self.h.guild, 99, 41)
        self.assertIsNotNone(first['continuation'])
        book = self.store.book(1, self.book['id'])
        self.store.remove_book(1, book['id'], expected_revision=book['revision'], actor_id=99)
        tables = ('bc_import_preview_cursors', 'bc_import_coverage', 'bc_import_budgets', 'bc_import_runs')
        before = {table: self.store.rows(f'SELECT * FROM {table}') for table in tables}
        history = self.h.old_thread.history
        self.h.old_thread.history = Mock(side_effect=AssertionError('Removed book history read'))
        with self.assertRaisesRegex(ClubError, 'удалена'):
            await self.importer.preview(self.h.guild, 99, 41, cursor=first['continuation'])
        with self.assertRaisesRegex(ClubError, 'удалена'):
            await self.importer.scan(self.h.guild, 99, 41, 'blocked-cursor', cursor=first['continuation'])
        self.h.old_thread.history.assert_not_called()
        self.assertEqual({table: self.store.rows(f'SELECT * FROM {table}') for table in tables}, before)
        self.runner.login_status.assert_not_awaited()
        self.runner.analyze.assert_not_awaited()
        removed = self.store.book(1, book['id'])
        self.store.restore_book(1, book['id'], expected_revision=removed['revision'], actor_id=99)
        self.h.old_thread.history = history
        resumed = await self.importer.preview(self.h.guild, 99, 41, cursor=first['continuation'])
        self.assertFalse({m['id'] for m in first['messages']} & {m['id'] for m in resumed['messages']})

    async def test_removal_during_history_read_does_not_publish_snapshot_or_advance_cursor(self):
        self.importer.config = replace(self.config, max_messages=2)
        first = await self.importer.preview(self.h.guild, 99, 41)
        tables = ('bc_import_preview_cursors', 'bc_import_coverage')
        before = {table: self.store.rows(f'SELECT * FROM {table}') for table in tables}
        history = self.h.old_thread.history
        async def remove_while_reading(**kwargs):
            async for message in history(**kwargs):
                book = self.store.book(1, self.book['id'])
                if not book['deleted']:
                    self.store.remove_book(1, book['id'], expected_revision=book['revision'], actor_id=99)
                yield message
        self.h.old_thread.history = remove_while_reading
        with self.assertRaisesRegex(ClubError, 'удалена'):
            await self.importer.preview(self.h.guild, 99, 41, cursor=first['continuation'])
        self.assertEqual({table: self.store.rows(f'SELECT * FROM {table}') for table in tables}, before)
        self.runner.login_status.assert_not_awaited()
        self.runner.analyze.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()

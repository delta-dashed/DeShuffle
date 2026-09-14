"""Bounded archive previews never spend model quota or read unrelated history."""
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

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
        self.h.source.fetch_message.assert_awaited_once_with(51)

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
        last = snapshot['messages'][-1]['id']
        warnings = '\n'.join(snapshot['warnings'])
        self.assertIn(f'thread:51 after:{last}', warnings)
        self.assertIn('/club import preview', warnings)
        self.assertIn('свободного лимита', warnings)
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

    async def test_plain_channel_continuation_keeps_unread_messages_in_next_page(self):
        for ident in range(100, 110):
            self.h.human_message(self.h.source, ident, 'Эссе из корневого канала ' + 'Ж' * 150)
        # No thread essays: this scenario is only a flat historical channel.
        self.h.old_thread.is_private.return_value = True
        self.importer.config = replace(self.config, max_input_bytes=2500)
        first = await self.importer.preview(self.h.guild, 99, 41)
        first_ids = [int(message['id']) for message in first['messages']]
        self.assertEqual(first_ids, sorted(first_ids, reverse=True))
        cursor = min(first_ids)
        self.assertIn(f'before:{cursor}', '\n'.join(first['warnings']))
        second = await self.importer.preview(self.h.guild, 99, 41, before_id=cursor)
        self.assertEqual(int(second['messages'][0]['id']), cursor - 1)
        self.assertFalse(set(first_ids) & {int(message['id']) for message in second['messages']})


if __name__ == '__main__':
    unittest.main()

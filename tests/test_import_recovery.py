"""Failed analysis recovery is auditable and cannot refund or repeat model use."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import discord
from discord import app_commands
from discord.ext import commands

from bookclub.archive_import import ArchiveImporter
from bookclub.import_config import ImportConfig
from bookclub.service import Service
from bookclub.store import ClubError, Store
from bookclub.ui import Club, interaction_error
from test_archive_import import ArchiveHarness
from test_bookclub import ClubFixture


class RecoveryFixture(ClubFixture):
    def setUp(self):
        super().setUp()
        self.h = ArchiveHarness()
        self.service = Service(self.h.bot, self.store)
        self.config = ImportConfig(enabled=True, allowed_user_ids=(99,), allowed_guild_ids=(1,),
                                   allowed_channel_ids=(41,), max_runs=1, max_accounted_tokens=100_000)
        self.runner = SimpleNamespace(login_status=AsyncMock(return_value=False), analyze=AsyncMock(), close=AsyncMock())
        self.importer = ArchiveImporter(self.service, self.config, self.runner)

    async def failed(self, state='failed'):
        snapshot = await self.importer.preview(self.h.guild, 99, 41, thread_id=51)
        run = self.importer.ledger.reserve_run(1, 99, 41, self.config.budget_id, 'first', 1, 3000, 100_000, snapshot)
        self.importer.ledger.fail_run(1, run['id'], 'Безопасная диагностика', state=state)
        self.output = {'essays': [{'book_ref': snapshot['messages'][0]['context_ref'], 'message_ids': ['61', '62']}]}
        return self.importer.ledger.run(1, run['id'])


class ImportRecoveryTests(RecoveryFixture, unittest.IsolatedAsyncioTestCase):
    async def test_failed_restore_preserves_snapshot_usage_and_old_failure_with_audit(self):
        failed = await self.failed()
        budget = self.importer.ledger.budget(self.config.budget_id)
        restored = await self.importer.restore_plan(self.h.guild, 99, failed['id'], self.output, confirm=True)
        self.assertEqual(restored['state'], 'review')
        for field in ('snapshot', 'usage_tokens', 'reserved_tokens', 'detail', 'created_at'):
            self.assertEqual(restored[field], failed[field])
        self.assertEqual(restored['plan']['essays'][0]['author_id'], 1)
        audit = restored['restoration']
        canonical = json.dumps(restored['plan'], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        self.assertEqual(audit, {'actor_id': 99, 'old_state': 'failed', 'created_at': self.now,
                                 'plan_sha256': hashlib.sha256(canonical.encode()).hexdigest()})
        self.assertEqual(self.importer.ledger.budget(self.config.budget_id), budget)
        self.runner.analyze.assert_not_awaited()
        self.runner.login_status.assert_not_awaited()
        self.assertFalse(self.h.hooks)
        self.assertEqual(Store(self.path).one('SELECT state FROM bc_import_runs')['state'], 'review')

    async def test_unknown_recovery_needs_explicit_confirmation(self):
        run = await self.failed('unknown')
        with self.assertRaisesRegex(ClubError, 'confirm:true'):
            await self.importer.restore_plan(self.h.guild, 99, run['id'], self.output)
        self.assertEqual(self.importer.ledger.run(1, run['id'])['state'], 'unknown')
        self.assertEqual(self.store.rows('SELECT * FROM bc_import_plan_restores'), [])
        restored = await self.importer.restore_plan(self.h.guild, 99, run['id'], self.output, confirm=True)
        self.assertEqual(restored['restoration']['old_state'], 'unknown')

    async def test_restore_rejects_invented_message_author_and_snapshot_replacement(self):
        run = await self.failed()
        invalid = [
            {'essays': [{'book_ref': 'outside', 'message_ids': ['61']}]},
            {'essays': [{'book_ref': self.output['essays'][0]['book_ref'], 'message_ids': ['777']}]},
            {'essays': [{**self.output['essays'][0], 'author_id': 99}]},
            {**self.output, 'snapshot': {'messages': []}},
            {'essays': [{'book_ref': self.output['essays'][0]['book_ref'], 'message_ids': ['61', '63']}]},
        ]
        for output in invalid:
            with self.subTest(output=output), self.assertRaises(ClubError):
                await self.importer.restore_plan(self.h.guild, 99, run['id'], output, confirm=True)
        self.assertEqual(self.importer.ledger.run(1, run['id'])['state'], 'failed')
        self.assertEqual(self.store.rows('SELECT * FROM bc_import_plan_restores'), [])

    async def test_run_in_other_guild_and_unallowlisted_actor_are_not_exposed(self):
        run = await self.failed()
        with self.assertRaises(ClubError):
            await self.importer.restore_plan(self.h.guild, 1, run['id'], self.output, confirm=True)
        with self.assertRaisesRegex(ClubError, 'не найден'):
            self.importer.ledger.restore_plan(2, run['id'], 99, self.output)
        self.assertEqual(self.store.rows('SELECT * FROM bc_import_plan_restores'), [])

    async def test_revoked_source_or_target_access_blocks_restore(self):
        run = await self.failed()
        for channel in (self.h.source, self.h.old_thread, self.h.channels[14]):
            permissions = channel.permissions_for.return_value
            permissions.view_channel = False
            with self.assertRaises(ClubError):
                await self.importer.restore_plan(self.h.guild, 99, run['id'], self.output, confirm=True)
            permissions.view_channel = True
        self.assertEqual(self.importer.ledger.run(1, run['id'])['state'], 'failed')

    async def test_bot_lost_thread_access_also_blocks_snapshot_review(self):
        run = await self.failed()
        allowed = self.h.old_thread.permissions_for.return_value
        self.h.old_thread.permissions_for.side_effect = lambda member: (
            SimpleNamespace(view_channel=False, read_message_history=False) if member.id == 9999 else allowed)
        with self.assertRaisesRegex(ClubError, 'Доступ'):
            await self.importer.review(self.h.guild, 99, run['id'])

    async def test_changed_target_and_all_nonfailed_states_reject_restore(self):
        run = await self.failed()
        self.store.configure(1, {**self.store.settings(1), 'essays': 44})
        with self.assertRaisesRegex(ClubError, 'Форум назначения изменён'):
            await self.importer.restore_plan(self.h.guild, 99, run['id'], self.output, confirm=True)
        self.store.configure(1, {**self.store.settings(1), 'essays': 14})
        for state in ('running', 'review', 'applying', 'done'):
            with self.store.tx() as db:
                db.execute('UPDATE bc_import_runs SET state=? WHERE id=?', (state, run['id']))
            with self.subTest(state=state), self.assertRaisesRegex(ClubError, 'failed/unknown'):
                await self.importer.restore_plan(self.h.guild, 99, run['id'], self.output, confirm=True)
        self.assertEqual(self.store.rows('SELECT * FROM bc_import_plan_restores'), [])

    async def test_concurrent_restore_has_one_atomic_audit_and_no_budget_refund(self):
        run = await self.failed()
        plan = {'essays': [], 'skipped_message_ids': ['61', '62', '63']}
        def attempt():
            try:
                return self.importer.ledger.restore_plan(1, run['id'], 99, plan)['state']
            except ClubError:
                return 'rejected'
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: attempt(), range(2)))
        self.assertCountEqual(results, ['review', 'rejected'])
        self.assertEqual(len(self.store.rows('SELECT * FROM bc_import_plan_restores')), 1)
        self.assertEqual(self.importer.ledger.budget(self.config.budget_id)['tokens_reserved'], 3000)


class ImportRecoveryUITests(RecoveryFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.cog = Club(self.h.bot, self.store)
        self.cog.importer = self.importer
        self.ctx = SimpleNamespace(guild=self.h.guild, author=self.h.members[99], channel=self.h.old_thread,
                                   interaction=SimpleNamespace(id=999, followup=SimpleNamespace(send=AsyncMock())))

    async def test_nested_hybrid_error_exposes_expected_cause_privately(self):
        interaction = SimpleNamespace(response=SimpleNamespace(is_done=lambda: False, send_message=AsyncMock()))
        wrapped = commands.HybridCommandError(app_commands.CommandInvokeError(Mock(), ClubError('Лимит исчерпан')))
        wrapped = commands.CommandInvokeError(wrapped)
        await interaction_error(interaction, wrapped)
        reply = interaction.response.send_message.await_args
        self.assertEqual(reply.args[0], 'Лимит исчерпан')
        self.assertTrue(reply.kwargs['ephemeral'])
        self.assertFalse(reply.kwargs['allowed_mentions'].everyone)

    async def test_restore_attachment_requires_confirmation_before_download(self):
        file = SimpleNamespace(size=20, read=AsyncMock())
        with self.assertRaisesRegex(ClubError, 'confirm:true'):
            await self.cog.import_restore.callback(self.cog, self.ctx, 'run', file)
        file.read.assert_not_awaited()

    async def test_restore_attachment_is_bounded_before_and_after_download(self):
        file = SimpleNamespace(size=65_537, read=AsyncMock(return_value=b'x' * 65_537))
        with self.assertRaisesRegex(ClubError, '64 КиБ'):
            await self.cog.import_restore.callback(self.cog, self.ctx, 'run', file, True)
        file.read.assert_not_awaited()
        file.size = 1
        with self.assertRaisesRegex(ClubError, '64 КиБ'):
            await self.cog.import_restore.callback(self.cog, self.ctx, 'run', file, True)

    async def test_restore_json_routes_to_audit_and_private_review(self):
        run = await self.failed()
        file = SimpleNamespace(size=100, read=AsyncMock(return_value=json.dumps(self.output).encode()))
        await self.cog.import_restore.callback(self.cog, self.ctx, run['id'], file, True)
        self.assertEqual(self.importer.ledger.run(1, run['id'])['state'], 'review')
        self.assertTrue(all(call.kwargs['ephemeral'] for call in self.ctx.interaction.followup.send.await_args_list))
        self.runner.analyze.assert_not_awaited()

    async def test_preview_ui_keeps_snapshot_private_without_login_or_quota(self):
        await self.cog.import_preview.callback(self.cog, self.ctx)
        replies = self.ctx.interaction.followup.send.await_args_list
        self.assertTrue(all(call.kwargs['ephemeral'] for call in replies))
        self.assertTrue(any('Codex не вызван' in call.args[0] for call in replies))
        self.assertEqual(self.importer.ledger.budget(self.config.budget_id)['runs_used'], 0)
        self.runner.analyze.assert_not_awaited()
        self.runner.login_status.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()

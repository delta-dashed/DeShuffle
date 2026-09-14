"""Restyle completed legacy imports without reanalysis or moving author replies."""
import asyncio
from dataclasses import replace
import io
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import discord

from bookclub.archive_import import ArchiveImporter, validate_plan
from bookclub.import_config import ImportConfig
from bookclub.service import Service
from bookclub.store import ClubError, Store
from test_archive_import import ArchiveHarness
from test_bookclub import ClubFixture
from test_bookclub_discord import not_found


class ImportRestyleTests(ClubFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.h = ArchiveHarness()
        self.service = Service(self.h.bot, self.store)
        self.config = ImportConfig(enabled=True, allowed_user_ids=(99,), allowed_guild_ids=(1,),
                                   allowed_channel_ids=(41,), budget_id='legacy-essays-v1',
                                   max_runs=1, max_accounted_tokens=100_000)
        self.runner = SimpleNamespace(analyze=AsyncMock(), login_status=AsyncMock(),
                                      begin_login=AsyncMock(), close=AsyncMock())
        self.importer = ArchiveImporter(self.service, self.config, self.runner)

    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 1.0

    def attachment(self, ident=800, filename='essay.txt', payload=b'original attachment'):
        async def to_file(**kwargs):
            return discord.File(io.BytesIO(payload), filename=filename)
        return SimpleNamespace(id=ident, filename=filename, size=len(payload),
                               to_file=AsyncMock(side_effect=to_file))

    async def legacy_import(self, *, attachment=False, long_body=False, skipped_attachment_boundary=False):
        """Reproduce the old released bot transport using its persisted records."""
        if attachment:
            self.h.first.attachments = [self.attachment()]
        if long_body:
            self.h.first.content = 'Развёрнутое эссе. ' * 180
        if skipped_attachment_boundary:
            oversized = self.attachment()
            oversized.size = self.h.guild.filesize_limit + 1
            self.h.first.attachments = [oversized]
            self.h.first.content = 'x' * (1500 - len('\nВложение: essay.txt — см. оригинал'))
        self.snapshot = await self.importer.capture(self.h.guild, self.h.members[99], self.h.source)
        self.run = self.importer.ledger.reserve_run(1, 99, 41, self.config.budget_id, 'legacy-done',
                                                   1, 30714, 100_000, self.snapshot)
        plan = validate_plan(self.snapshot, {'essays': [{'book_ref': 'book:' + self.book['id'],
                                                       'message_ids': ['61', '62']}]})
        self.importer.ledger.save_plan(1, self.run['id'], plan)
        self.assertTrue(self.importer.ledger.claim_apply(1, self.run['id']))
        self.key = 'essay-import:1:61'
        self.assertTrue(self.importer.ledger.claim_sources(1, self.run['id'], self.key, [61, 62]))
        header = ('**Архивное эссе по книге «Книга»**\nАвтор: <@1>.\n'
                  'Перенесено из старого обсуждения по подтверждённому плану.\n'
                  f'[Первое исходное сообщение]({self.h.first.jump_url})')
        pub = await self.service.publish_essay_starter(self.h.guild, self.h.channels[14], self.key,
                                                       'Книга · Эссе · Участник 1', header, self.h.members[1])
        self.thread = self.h.channels[pub['channel_id']]
        self.header = self.thread.messages[pub['message_id']]
        if not self.header.content.endswith('\n-# bc:' + self.key):
            self.header.content += '\n-# bc:' + self.key
        self.old_messages, self.old_keys = [], []
        for original in (self.h.first, self.h.second):
            body = original.content
            snapshot = next(row for row in self.snapshot['messages'] if row['id'] == str(original.id))
            if original.attachments:
                body += '\nВложение: essay.txt' + ('' if snapshot['attachments'][0]['copy'] else ' — см. оригинал')
            chunks = [body[index:index + 1500] for index in range(0, len(body), 1500)]
            for index, chunk in enumerate(chunks):
                key = f'{self.key}:message:{original.id}:{index}'
                content = chunk + f'\n[Оригинал сообщения]({original.jump_url})'
                copy = await self.service.upsert(self.h.guild, key, self.thread.id, content)
                message = self.thread.messages[copy['message_id']]
                if original.attachments and snapshot['attachments'][0]['copy'] and index == 0:
                    message.attachments = [self.attachment(ident=900)]
                async def delete(*, target=message, **kwargs):
                    if target.id not in self.thread.messages:
                        raise not_found()
                    del self.thread.messages[target.id]
                message.delete = AsyncMock(side_effect=delete)
                self.old_messages.append(message)
                self.old_keys.append(key)
        self.store.register_essay(1, self.book['id'], self.thread.id, self.thread.id, 1,
                                  self.thread.name, self.thread.jump_url, managed=False, submitted=True)
        self.importer.ledger.finish_sources(1, self.key, self.thread.id)
        self.importer.ledger.finish_apply(1, self.run['id'])
        self.h.seq += 1
        self.reply = self.h.human_message(self.thread, self.h.seq, 'Дополнение автора после переноса', 1)
        self.hook, = self.h.hooks.values()
        self.budget_before = self.importer.ledger.budget(self.config.budget_id)
        self.sources_before = self.store.rows('SELECT * FROM bc_import_sources ORDER BY source_id')
        self.reset_discord_writes()

    def reset_discord_writes(self):
        for channel in self.h.channels.values():
            channel.send.reset_mock()
            channel.edit.reset_mock()
            if isinstance(channel, discord.ForumChannel):
                channel.create_thread.reset_mock()
                channel.create_webhook.reset_mock()
            for message in channel.messages.values():
                message.edit.reset_mock()
                message.delete.reset_mock()
        for hook in self.h.hooks.values():
            hook.send.reset_mock()
            hook.edit_message.reset_mock()

    def assert_no_discord_writes(self):
        for channel in self.h.channels.values():
            channel.send.assert_not_awaited()
            channel.edit.assert_not_awaited()
            if isinstance(channel, discord.ForumChannel):
                channel.create_thread.assert_not_awaited()
                channel.create_webhook.assert_not_awaited()
            for message in channel.messages.values():
                message.edit.assert_not_awaited()
                message.delete.assert_not_awaited()
        for hook in self.h.hooks.values():
            hook.send.assert_not_awaited()
            hook.edit_message.assert_not_awaited()

    async def restyle(self, *, confirm=True):
        return await self.importer.restyle(self.h.guild, 99, self.run['id'], confirm=confirm)

    def assert_bookkeeping_unchanged(self):
        self.assertEqual(self.importer.ledger.budget(self.config.budget_id), self.budget_before)
        self.assertEqual(self.importer.ledger.run(1, self.run['id'])['state'], 'done')
        self.assertEqual(self.store.rows('SELECT * FROM bc_import_sources ORDER BY source_id'), self.sources_before)
        self.runner.analyze.assert_not_awaited()
        self.runner.login_status.assert_not_awaited()
        self.runner.begin_login.assert_not_awaited()

    def new_copies(self):
        return [self.thread.messages[self.store.publication(key + ':v2')['message_id']]
                for key in self.old_keys]

    async def test_preview_is_read_only_with_exhausted_model_budget(self):
        await self.legacy_import()
        publications = self.store.rows('SELECT * FROM bc_publications ORDER BY key')
        result = await self.restyle(confirm=False)
        self.assertTrue(result)
        self.assert_no_discord_writes()
        self.assertEqual(self.store.rows('SELECT * FROM bc_publications ORDER BY key'), publications)
        self.assertEqual(self.store.rows('SELECT * FROM bc_import_restyle_audit'), [])
        self.assert_bookkeeping_unchanged()

    async def test_resumed_old_apply_can_finish_with_legacy_header_already_cleaned(self):
        await self.legacy_import()
        # A pre-upgrade apply can resume and remove only its recovery marker
        # while leaving the older generated source footer for restyle.
        self.header.content = self.header.content.removesuffix('\n-# bc:' + self.key)
        await self.restyle()
        self.assertNotIn('Первое исходное сообщение', self.header.content)
        self.assertNotIn('-# bc:', self.header.content)
        self.assert_bookkeeping_unchanged()

    async def test_replaces_body_with_author_identity_in_same_thread_and_removes_generated_links(self):
        await self.legacy_import()
        thread_ids = set(self.h.channels)
        await self.restyle()
        self.assertEqual(set(self.h.channels), thread_ids)
        for original, new in zip((self.h.first, self.h.second), self.new_copies()):
            self.assertEqual(new.channel.id, self.thread.id)
            self.assertEqual(new.webhook_id, self.hook.id)
            self.assertEqual(new.author.display_name, self.h.members[1].display_name)
            self.assertEqual(new.author.display_avatar.url, self.h.members[1].display_avatar.url)
            self.assertIn(original.content, new.content)
            self.assertNotIn('[Оригинал сообщения]', new.content)
            self.assertNotIn(original.jump_url, new.content)
            self.assertNotIn('bc:essay-import:', new.content)
        self.assertNotIn('[Первое исходное сообщение]', self.header.content)
        self.assertNotIn(self.h.first.jump_url, self.header.content)
        self.assertNotIn('bc:essay-import:', self.header.content)
        for key, old in zip(self.old_keys, self.old_messages):
            self.assertNotIn(old.id, self.thread.messages)
            self.assertIsNone(self.store.publication(key))
            old.delete.assert_awaited_once()
        self.assertIs(self.thread.messages[self.reply.id], self.reply)
        self.reply.edit.assert_not_awaited()
        self.reply.delete.assert_not_awaited()
        self.assertEqual(set(self.h.old_thread.messages) & {61, 62, 63}, {61, 62, 63})
        for original in (self.h.first, self.h.second, self.h.discussion):
            original.edit.assert_not_awaited()
            original.delete.assert_not_awaited()
        audits = self.store.rows('SELECT * FROM bc_import_restyle_audit')
        self.assertTrue(audits)
        self.assertTrue(all(row['actor_id'] == 99 and row['run_id'] == self.run['id'] for row in audits))
        self.assert_bookkeeping_unchanged()

    async def test_repeat_after_restart_does_not_duplicate_or_spend_model_quota(self):
        await self.legacy_import()
        await self.restyle()
        first_ids = set(self.thread.messages)
        audit = self.store.rows('SELECT * FROM bc_import_restyle_audit ORDER BY id')
        self.store = Store(self.path, clock=lambda: self.now)
        self.service = Service(self.h.bot, self.store)
        self.importer = ArchiveImporter(self.service, self.config, self.runner)
        self.reset_discord_writes()
        await self.restyle()
        self.assertEqual(set(self.thread.messages), first_ids)
        self.hook.send.assert_not_awaited()
        self.hook.edit_message.assert_not_awaited()
        self.assertEqual(self.store.rows('SELECT * FROM bc_import_restyle_audit ORDER BY id'), audit)
        self.assert_bookkeeping_unchanged()

    async def test_cleans_existing_v2_markers_in_place_without_resending_body_or_files(self):
        await self.legacy_import(attachment=True)
        await self.restyle()
        copies = self.new_copies()
        for key, copy in zip(self.old_keys, copies):
            copy.content += '\n-# bc:' + key + ':v2'
        self.header.content += '\n-# bc:' + self.key
        message_ids = set(self.thread.messages)
        bindings = self.store.rows('SELECT key,channel_id,message_id,webhook_id FROM bc_publications ORDER BY key')
        expected_files = [(a.filename, a.size) for a in copies[0].attachments]
        self.reset_discord_writes()

        await self.restyle(confirm=False)
        self.assert_no_discord_writes()
        await self.restyle()

        self.assertEqual(set(self.thread.messages), message_ids)
        self.assertEqual(self.store.rows('SELECT key,channel_id,message_id,webhook_id FROM bc_publications ORDER BY key'), bindings)
        self.hook.send.assert_not_awaited()
        self.assertEqual(self.hook.edit_message.await_count, 3)
        for copy, original in zip(copies, (self.h.first, self.h.second)):
            self.assertNotIn('bc:essay-import:', copy.content)
            self.assertIn(original.content, copy.content)
            copy.delete.assert_not_awaited()
        self.assertEqual([(a.filename, a.size) for a in copies[0].attachments], expected_files)
        copies[0].attachments[0].to_file.assert_not_awaited()
        self.assertNotIn('bc:essay-import:', self.header.content)
        self.assertTrue(await self.service.register_thread(self.thread, prompt=False))
        self.assertEqual(self.store.essays(self.book['id'])[0]['author_id'], 1)
        self.reply.edit.assert_not_awaited()
        self.reply.delete.assert_not_awaited()
        self.assert_bookkeeping_unchanged()

        self.reset_discord_writes()
        await self.restyle()
        self.hook.send.assert_not_awaited()
        self.hook.edit_message.assert_not_awaited()
        self.assertEqual(set(self.thread.messages), message_ids)
        self.assert_bookkeeping_unchanged()

    async def test_modified_v2_text_is_protected_before_any_marker_is_removed(self):
        await self.legacy_import()
        await self.restyle()
        copies = self.new_copies()
        for key, copy in zip(self.old_keys, copies):
            copy.content += '\n-# bc:' + key + ':v2'
        copies[-1].content = 'Правка участника\n' + copies[-1].content
        self.header.content += '\n-# bc:' + self.key
        self.reset_discord_writes()
        with self.assertRaises(ClubError):
            await self.restyle()
        self.assert_no_discord_writes()
        self.assertIn('Правка участника', copies[-1].content)
        self.assert_bookkeeping_unchanged()

    async def test_only_generated_suffix_is_removed_and_original_marker_like_text_survives(self):
        self.h.first.content += '\n-# bc:essay-import:1:61:message:61:0:v2\nПродолжение эссе.'
        await self.legacy_import()
        await self.restyle()
        self.assertEqual(self.new_copies()[0].content, self.h.first.content)
        self.assert_bookkeeping_unchanged()

    async def test_attachments_are_copied_from_existing_copy_even_if_original_message_disappeared(self):
        await self.legacy_import(attachment=True)
        copied_attachment = self.old_messages[0].attachments[0]
        del self.h.old_thread.messages[61]
        self.h.first.attachments[0].to_file.side_effect = AssertionError('Do not reread original archive attachment')
        await self.restyle()
        first, _ = self.new_copies()
        copied_attachment.to_file.assert_awaited_once()
        self.assertEqual([(a.filename, a.size) for a in first.attachments],
                         [('essay.txt', len(b'original attachment'))])
        file = await first.attachments[0].to_file()
        self.assertEqual(file.fp.read(), b'original attachment')
        file.close()
        self.assert_bookkeeping_unchanged()

    async def test_all_legacy_copies_are_checked_before_any_mutation(self):
        await self.legacy_import()
        self.old_messages[-1].content += '\nРучная правка'
        with self.assertRaises(ClubError):
            await self.restyle()
        self.assert_no_discord_writes()
        self.assert_bookkeeping_unchanged()

    async def test_missing_legacy_copy_without_replacement_is_not_silently_recreated(self):
        await self.legacy_import()
        del self.thread.messages[self.old_messages[-1].id]
        with self.assertRaises(ClubError):
            await self.restyle()
        self.assert_no_discord_writes()
        self.assert_bookkeeping_unchanged()

    async def test_human_owned_copy_is_never_deleted_even_when_content_matches(self):
        await self.legacy_import()
        self.old_messages[-1].author = self.h.members[1]
        with self.assertRaises(ClubError):
            await self.restyle()
        self.assert_no_discord_writes()
        self.assert_bookkeeping_unchanged()

    async def test_changed_root_marker_is_rejected_before_writes(self):
        await self.legacy_import()
        self.header.content = self.header.content.replace('bc:' + self.key, 'bc:unrelated')
        with self.assertRaises(ClubError):
            await self.restyle()
        self.assert_no_discord_writes()
        self.assert_bookkeeping_unchanged()

    async def test_changed_root_sender_is_rejected_before_writes(self):
        await self.legacy_import()
        self.header.webhook_id = 98765
        with self.assertRaises(ClubError):
            await self.restyle()
        self.assert_no_discord_writes()
        self.assert_bookkeeping_unchanged()

    async def test_manual_header_edit_is_not_overwritten_even_with_matching_marker(self):
        await self.legacy_import()
        self.header.content = 'Ручное уточнение\n' + self.header.content
        with self.assertRaises(ClubError):
            await self.restyle()
        self.assert_no_discord_writes()
        self.assertIn('Ручное уточнение', self.header.content)
        self.assert_bookkeeping_unchanged()

    async def test_book_rename_keeps_original_snapshot_header_eligible_for_cleanup(self):
        await self.legacy_import()
        self.store.update_book(1, self.book['id'], title='Исправленное название')
        await self.restyle()
        self.assertEqual(self.header.content, '**Архивное эссе по книге «Исправленное название»**\nАвтор: <@1>.')
        self.assertTrue(await self.service.register_thread(self.thread, prompt=False))
        self.assert_bookkeeping_unchanged()

    async def test_changed_essay_author_is_not_overwritten_by_saved_import_plan(self):
        await self.legacy_import()
        self.store.register_essay(1, self.book['id'], self.thread.id, self.thread.id, 2,
                                  self.thread.name, self.thread.jump_url, correct=True)
        with self.assertRaises(ClubError):
            await self.restyle()
        self.assert_no_discord_writes()
        self.assertEqual(self.store.essays(self.book['id'])[0]['author_id'], 2)
        self.assert_bookkeeping_unchanged()

    async def test_missing_source_binding_is_not_inferred_from_header_text(self):
        await self.legacy_import()
        with self.store.tx() as db:
            db.execute('DELETE FROM bc_import_sources WHERE source_id=62')
        with self.assertRaises(ClubError):
            await self.restyle()
        self.assert_no_discord_writes()
        self.assertEqual(len(self.store.rows('SELECT * FROM bc_import_sources')), 1)
        self.assertEqual(self.importer.ledger.budget(self.config.budget_id), self.budget_before)
        self.runner.analyze.assert_not_awaited()

    async def test_missing_fragment_of_long_legacy_message_is_not_recreated(self):
        await self.legacy_import(long_body=True)
        self.assertGreater(len(self.old_messages), 2)
        del self.thread.messages[self.old_messages[1].id]
        with self.assertRaises(ClubError):
            await self.restyle()
        self.assert_no_discord_writes()
        self.assert_bookkeeping_unchanged()

    async def test_legacy_attachment_tampering_stops_before_send_or_delete(self):
        await self.legacy_import(attachment=True)
        self.old_messages[0].attachments[0].filename = 'different-file.txt'
        with self.assertRaises(ClubError):
            await self.restyle()
        self.assert_no_discord_writes()
        self.assert_bookkeeping_unchanged()

    async def test_changed_legacy_attachment_is_not_deleted_when_replacement_already_exists(self):
        await self.legacy_import(attachment=True)
        original_delete = self.old_messages[0].delete.side_effect
        self.old_messages[0].delete.side_effect = OSError('stop before deleting the legacy copy')
        with self.assertRaises((OSError, ClubError)):
            await self.restyle()
        self.assertEqual(len(self.new_copies()), 2)
        self.assertIn(self.old_messages[0].id, self.thread.messages)
        self.old_messages[0].delete.side_effect = original_delete
        self.old_messages[0].attachments[0].filename = 'user-replaced-attachment.txt'
        self.reset_discord_writes()
        messages_before = set(self.thread.messages)
        audits_before = self.store.rows('SELECT * FROM bc_import_restyle_audit ORDER BY id')
        with self.assertRaises(ClubError):
            await self.restyle()
        self.assert_no_discord_writes()
        self.assertEqual(set(self.thread.messages), messages_before)
        self.assertEqual(self.store.rows('SELECT * FROM bc_import_restyle_audit ORDER BY id'), audits_before)
        self.assert_bookkeeping_unchanged()

    async def test_deleted_header_webhook_rejects_before_replacing_any_body(self):
        await self.legacy_import()
        del self.h.hooks[self.hook.id]
        messages_before = set(self.thread.messages)
        with self.assertRaises((ClubError, discord.NotFound)):
            await self.restyle()
        self.assert_no_discord_writes()
        self.assertEqual(set(self.thread.messages), messages_before)
        self.assertEqual(self.store.rows('SELECT * FROM bc_import_restyle_audit'), [])
        for key in self.old_keys:
            self.assertIsNone(self.store.publication(key + ':v2'))
        self.assert_bookkeeping_unchanged()

    async def test_longer_skipped_attachment_label_can_add_a_chunk_without_losing_text(self):
        await self.legacy_import(skipped_attachment_boundary=True)
        self.assertEqual(len(self.old_messages), 2)
        await self.restyle()
        rows = self.store.rows('SELECT * FROM bc_publications WHERE key LIKE ? ORDER BY key',
                               (self.key + ':message:61:%:v2',))
        self.assertEqual(len(rows), 2)
        contents = [self.thread.messages[row['message_id']].content.split('\n-# bc:', 1)[0] for row in rows]
        self.assertEqual(''.join(contents), self.h.first.content +
                         '\nВложение: essay.txt — файл превышает лимит переноса')
        self.h.first.attachments[0].to_file.assert_not_awaited()
        self.assertEqual(len(self.thread.messages), 5)
        self.assert_bookkeeping_unchanged()

    async def test_lost_send_ack_keeps_attachments_when_recovering_by_marker(self):
        await self.legacy_import(attachment=True)
        original_send = self.hook.send.side_effect
        async def send_then_lose_ack(*args, **kwargs):
            await original_send(*args, **kwargs)
            raise OSError('simulated lost response')
        self.hook.send.side_effect = send_then_lose_ack
        with self.assertRaises((OSError, ClubError)):
            await self.restyle()
        self.hook.send.side_effect = original_send
        await self.restyle()
        first, _ = self.new_copies()
        self.assertEqual([(a.filename, a.size) for a in first.attachments],
                         [('essay.txt', len(b'original attachment'))])
        self.assertEqual(self.hook.send.await_count, 2)
        self.assertEqual(len(self.thread.messages), 4)
        self.assert_bookkeeping_unchanged()

    async def test_live_source_and_destination_acl_are_required_for_preview_and_apply(self):
        await self.legacy_import()
        for channel in (self.h.source, self.h.channels[14]):
            channel.permissions_for.return_value.view_channel = False
            for confirm in (False, True):
                with self.subTest(channel=channel.id, confirm=confirm), self.assertRaises(ClubError):
                    await self.restyle(confirm=confirm)
            channel.permissions_for.return_value.view_channel = True
        self.assert_no_discord_writes()
        self.assert_bookkeeping_unchanged()

    async def test_configuration_guard_still_applies_without_model_call(self):
        await self.legacy_import()
        for changed in (replace(self.config, enabled=False), replace(self.config, allowed_user_ids=(1,)),
                        replace(self.config, allowed_channel_ids=(42,))):
            self.importer.config = changed
            with self.assertRaises(ClubError):
                await self.restyle()
        self.importer.config = self.config
        self.assert_no_discord_writes()
        self.assert_bookkeeping_unchanged()

    async def test_archived_thread_is_restored_after_restyle(self):
        await self.legacy_import()
        self.thread.archived = True
        await self.restyle()
        self.assertTrue(self.thread.archived)
        self.assertEqual(len(self.new_copies()), 2)
        self.assert_bookkeeping_unchanged()

    async def test_original_archive_state_survives_restart_after_process_loses_cleanup(self):
        await self.legacy_import()
        self.thread.archived = True
        original_send, original_edit = self.hook.send.side_effect, self.thread.edit.side_effect

        async def send_then_lose_ack(*args, **kwargs):
            await original_send(*args, **kwargs)
            raise OSError('process stops after the first copied body')

        async def process_cannot_restore_archive(**kwargs):
            if kwargs.get('archived') is True:
                raise OSError('process is gone before archive cleanup can complete')
            return await original_edit(**kwargs)

        self.hook.send.side_effect = send_then_lose_ack
        self.thread.edit.side_effect = process_cannot_restore_archive
        with self.assertRaises((OSError, ClubError)):
            await self.restyle()
        self.assertFalse(self.thread.archived)
        pending = self.store.rows('SELECT * FROM bc_import_restyle_threads')
        self.assertEqual(pending, [{'guild_id': 1, 'run_id': self.run['id'], 'thread_id': self.thread.id}])

        self.hook.send.side_effect, self.thread.edit.side_effect = original_send, original_edit
        self.store = Store(self.path, clock=lambda: self.now)
        self.service = Service(self.h.bot, self.store)
        self.importer = ArchiveImporter(self.service, self.config, self.runner)
        await self.restyle()
        self.assertTrue(self.thread.archived)
        self.assertEqual(len(self.new_copies()), 2)
        self.assertEqual(len(self.thread.messages), 4)
        self.assertEqual(self.hook.send.await_count, 2)
        self.assertEqual(self.store.rows('SELECT * FROM bc_import_restyle_threads'), [])
        self.assert_bookkeeping_unchanged()

    async def test_lost_webhook_send_ack_recovers_without_duplicate_body(self):
        await self.legacy_import()
        original_send = self.hook.send.side_effect
        async def send_then_lose_ack(*args, **kwargs):
            await original_send(*args, **kwargs)
            raise OSError('simulated lost response')
        self.hook.send.side_effect = send_then_lose_ack
        with self.assertRaises((OSError, ClubError)):
            await self.restyle()
        for old in self.old_messages:
            self.assertIn(old.id, self.thread.messages)
        self.hook.send.side_effect = original_send
        await self.restyle()
        new = self.new_copies()
        self.assertEqual(len({m.id for m in new}), 2)
        self.assertEqual(len(self.thread.messages), 4)  # starter + two bodies + human reply
        self.assertEqual(self.hook.send.await_count, 2)
        self.assert_bookkeeping_unchanged()

    async def test_lost_delete_ack_recovers_from_audited_replacement(self):
        await self.legacy_import()
        old = self.old_messages[0]
        async def delete_then_lose_ack(**kwargs):
            del self.thread.messages[old.id]
            raise OSError('simulated lost delete response')
        old.delete.side_effect = delete_then_lose_ack
        with self.assertRaises((OSError, ClubError)):
            await self.restyle()
        self.assertNotIn(old.id, self.thread.messages)
        self.assertIsNotNone(self.store.publication(self.old_keys[0] + ':v2')['message_id'])
        self.assertTrue(self.store.rows('SELECT * FROM bc_import_restyle_audit WHERE old_message_id=?', (old.id,)))
        await self.restyle()
        self.assertEqual(len(self.thread.messages), 4)
        self.assertEqual(self.hook.send.await_count, 2)
        self.assert_bookkeeping_unchanged()

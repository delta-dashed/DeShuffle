"""Archive scans and approved copies use real SQLite with simulated Discord I/O."""
import asyncio
from dataclasses import replace
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import discord

from bookclub.archive_import import ArchiveImporter, validate_plan
from bookclub.import_config import ImportConfig
from bookclub.import_preparation import PreparationStore
from bookclub.import_publication import archive_chunks, archive_header
from bookclub.service import Service
from bookclub.store import ClubError, Store
from test_bookclub import ClubFixture
from test_bookclub_discord import iterate
from test_webhook_discord import WebhookHarness


def confirm_book_thread(store, book, *, source_id=41, thread_id=51):
    """Seed a human-approved decision without exercising Discord UI in fixtures."""
    return PreparationStore(store).save(1, source_id, thread_id, 'included', 99,
                                         title=book['title'], author=book['author'], book_id=book['id'])


class ArchiveHarness(WebhookHarness):
    def __init__(self):
        super().__init__()
        self.guild.filesize_limit = 10_000_000
        for member in self.members.values():
            member.guild_permissions = discord.Permissions.none()
        self.members[9999] = SimpleNamespace(id=9999, bot=True)
        self.source = self.channel(41, discord.TextChannel, name='Старые эссе')
        self.source.archived_threads.side_effect = lambda **_: iterate([
            c for c in self.channels.values()
            if isinstance(c, discord.Thread) and c.parent_id == 41 and c.archived])
        self.old_thread = self.channel(51, parent_id=41, name='Обсуждение книги')
        root = self.human_message(self.source, 51, '# Книга', 99)
        root.thread = self.old_thread
        root.flags.has_thread = True
        self.first = self.human_message(self.old_thread, 61, 'Первая часть моего эссе. @everyone', 1)
        self.second = self.human_message(self.old_thread, 62, 'Продолжение эссе: мои выводы.', 1)
        self.discussion = self.human_message(self.old_thread, 63, 'Согласен!', 2)

    def channel(self, ident, kind=discord.Thread, **kwargs):
        result = super().channel(ident, kind, **kwargs)
        if isinstance(result, discord.Thread):
            result.is_private.return_value = False
        def history(*, limit=100, before=None, after=None, oldest_first=False):
            rows = sorted(result.messages.values(), key=lambda m: m.id, reverse=not oldest_first)
            rows = [m for m in rows if (before is None or m.id < before.id)
                    and (after is None or m.id > after.id)]
            return iterate(rows[:limit] if limit is not None else rows)
        result.history = history
        return result

    def message(self, channel, ident, content, **kwargs):
        result = super().message(channel, ident, content, **kwargs)
        result.thread = None
        result.flags = SimpleNamespace(has_thread=False)
        return result

    def human_message(self, channel, ident, content, author=1):
        result = self.message(channel, ident, content)
        result.author = self.members[author]
        return result


class ArchiveImportTests(ClubFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.h = ArchiveHarness()
        self.service = Service(self.h.bot, self.store)
        self.config = ImportConfig(enabled=True, allowed_user_ids=(99,),
                                   allowed_guild_ids=(1,), allowed_channel_ids=(41,),
                                   max_runs=3, max_accounted_tokens=500_000)
        self.runner = SimpleNamespace(login_status=AsyncMock(return_value=True),
                                      begin_login=AsyncMock(), close=AsyncMock(),
                                      analyze=AsyncMock(side_effect=self.classify))
        self.importer = ArchiveImporter(self.service, self.config, self.runner)
        confirm_book_thread(self.store, self.book)

    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 1.0

    async def classify(self, prompt, schema):
        payload = json.loads(prompt.split('\n', 1)[1])
        first = next(m for m in payload['messages'] if m['id'] == '61')
        return {'output': {'essays': [{'book_ref': first['context_ref'], 'message_ids': ['62', '61']}]},
                'usage_tokens': 321}

    async def scan(self, key='scan-1'):
        return await self.importer.scan(self.h.guild, 99, 41, key)

    async def apply(self, run):
        return await self.importer.apply(self.h.guild, 99, run['id'], confirm=True)

    def assert_no_discord_writes(self):
        self.assertFalse(self.h.hooks)
        for channel in self.h.channels.values():
            channel.send.assert_not_awaited()
            channel.edit.assert_not_awaited()
            if isinstance(channel, discord.ForumChannel):
                channel.create_thread.assert_not_awaited()
                channel.create_webhook.assert_not_awaited()
            for message in channel.messages.values():
                message.edit.assert_not_awaited()
        self.assertEqual(self.store.essays(self.book['id']), [])

    async def test_disabled_switch_blocks_all_entrypoints_without_runner_or_scan(self):
        self.importer.config = replace(self.config, enabled=False)
        actions = [self.importer.scan(self.h.guild, 99, 41, 'blocked'),
                   self.importer.login(self.h.guild, 99),
                   self.importer.status(self.h.guild, 99),
                   self.importer.review(self.h.guild, 99, 'missing'),
                   self.importer.apply(self.h.guild, 99, 'missing', confirm=True)]
        for action in actions:
            with self.assertRaisesRegex(ClubError, 'отключён'):
                await action
        self.runner.login_status.assert_not_awaited()
        self.runner.analyze.assert_not_awaited()
        self.h.bot.fetch_channel.assert_not_awaited()

    async def test_user_guild_channel_allowlists_and_live_admin_rights_precede_model(self):
        cases = [dict(allowed_user_ids=(1,)), dict(allowed_guild_ids=(2,)),
                 dict(allowed_channel_ids=(42,))]
        for overrides in cases:
            self.importer.config = replace(self.config, **overrides)
            with self.assertRaises(ClubError):
                await self.scan()
        self.importer.config = replace(self.config, allowed_user_ids=(1,))
        with self.assertRaisesRegex(ClubError, 'Manage Server'):
            await self.importer.scan(self.h.guild, 1, 41, 'regular-member')
        self.runner.analyze.assert_not_awaited()
        self.runner.login_status.assert_not_awaited()
        self.assertEqual(self.importer.ledger.budget(self.config.budget_id)['runs_used'], 0)

    async def test_source_history_access_is_checked_before_model(self):
        self.h.source.permissions_for.return_value.read_message_history = False
        with self.assertRaisesRegex(ClubError, 'истории'):
            await self.scan()
        self.runner.analyze.assert_not_awaited()
        self.assert_no_discord_writes()

    async def test_scan_builds_review_plan_from_confirmed_book_without_discord_writes(self):
        run = await self.scan()
        self.assertEqual(run['state'], 'review')
        self.assertEqual(run['plan']['essays'], [{'book_ref': 'book:' + self.book['id'],
                                                'message_ids': ['61', '62'], 'author_id': 1}])
        self.assertEqual(run['plan']['skipped_message_ids'], ['63'])
        self.assertEqual({m['id'] for m in run['snapshot']['messages']}, {'61', '62', '63'})
        self.assertEqual(len(self.store.books(1)), 1)
        self.assert_no_discord_writes()
        self.runner.analyze.assert_awaited_once()
        report = '\n'.join(self.importer.report(run))
        self.assertIn('Участник 1', report)
        self.assertIn('confirm:true', report)

    async def test_public_archived_threads_are_included_private_and_other_channels_excluded(self):
        self.h.old_thread.archived = True
        private = self.h.channel(71, parent_id=41)
        private.is_private.return_value = True
        self.h.human_message(private, 72, 'Секретный текст')
        foreign = self.h.channel(81, parent_id=12)
        self.h.human_message(foreign, 82, 'Пост в другом канале')
        self.h.human_message(self.h.old_thread, 64, 'Вебхук').webhook_id = 999
        self.h.message(self.h.old_thread, 65, 'Пост бота')
        run = await self.scan()
        self.assertEqual({m['id'] for m in run['snapshot']['messages']}, {'61', '62', '63'})

    async def test_confirmed_new_book_is_created_only_when_reviewed_plan_is_applied(self):
        self.h.source.messages[51].content = '# Неизвестная книга'
        await self.importer.preparation.select(self.h.guild, 99, 41, 51, decision='included',
                                               title='Неизвестная книга', author='Подтверждённый автор', confirm=True)
        run = await self.scan()
        self.assertEqual(run['plan']['essays'][0]['book_ref'], 'source:51')
        self.assertEqual(len(self.store.books(1)), 1)
        self.assert_no_discord_writes()
        await self.apply(run)
        added = next(b for b in self.store.books(1) if b['title'] == 'Неизвестная книга')
        self.assertEqual(added['author'], 'Подтверждённый автор')
        essay, = self.store.essays(added['id'])
        self.assertEqual(essay['author_id'], 1)

    async def test_forum_uses_confirmed_book_and_preserves_starter_essay(self):
        source = self.h.channel(42, discord.ForumChannel)
        topic = self.h.channel(71, parent_id=42, name='Произвольное имя обсуждения')
        starter = self.h.human_message(topic, 71, 'Полное эссе начинается в стартовом посте.', 1)
        self.h.human_message(topic, 72, 'Продолжение.', 1)
        self.importer.config = replace(self.config, allowed_channel_ids=(41, 42))
        await self.importer.preparation.select(self.h.guild, 99, 42, 71, decision='included',
                                               book_id=self.book['id'], confirm=True)
        snapshot = await self.importer.capture(self.h.guild, self.h.members[99], source)
        self.assertEqual({m['id'] for m in snapshot['messages']}, {'71', '72'})
        self.assertTrue(all(m['context_ref'] == 'book:' + self.book['id'] for m in snapshot['messages']))
        self.assertEqual(snapshot['messages'][0]['content'], starter.content)
        self.assertEqual(len(snapshot['books']), 1)

    async def test_explicit_thread_cursor_advances_past_already_imported_messages(self):
        self.h.human_message(self.h.old_thread, 64, 'Ещё одно полноценное эссе.', 2)
        # The thread fixture also contains a bot starter, which consumes one raw-history slot.
        self.importer.config = replace(self.config, max_messages=3)
        first = await self.scan()
        self.assertEqual({m['id'] for m in first['snapshot']['messages']}, {'61', '62'})
        self.assertIsInstance(first['snapshot']['continuation'], str)
        self.assertEqual(first['snapshot']['coverage']['after'], '62')
        await self.apply(first)
        self.h.old_thread.archived = True
        self.h.source.archived_threads.side_effect = lambda **_: iterate([])
        next_page = await self.importer.capture(self.h.guild, self.h.members[99], self.h.source,
                                                thread_id=51, after_id=62)
        self.assertEqual({m['id'] for m in next_page['messages']}, {'63', '64'})

    async def test_model_cannot_invent_ids_authors_actions_or_mix_authors(self):
        run = await self.scan()
        ref = 'book:' + self.book['id']
        invalid = [
            {'essays': [{'book_ref': ref, 'message_ids': ['999']} ]},
            {'essays': [{'book_ref': 'unknown', 'message_ids': ['61']}]},
            {'essays': [{'book_ref': ref, 'message_ids': ['61', '63']}]},
            {'essays': [{'book_ref': ref, 'message_ids': ['61', '61']}]},
            {'essays': [{'book_ref': ref, 'message_ids': ['61'], 'author_id': 99}]},
            {'essays': [], 'command': 'delete-channel'},
            {'essays': [{'book_ref': ref, 'message_ids': ['61']},
                        {'book_ref': ref, 'message_ids': ['61']}]},
        ]
        for output in invalid:
            with self.subTest(output=output), self.assertRaises(ClubError):
                validate_plan(run['snapshot'], output)
        self.assert_no_discord_writes()

    async def test_model_cannot_reassign_messages_between_human_confirmed_books(self):
        other = self.store.create_book(1, 'Вторая книга', 'Другой автор', '', 'second-book')
        thread = self.h.channel(71, parent_id=41, name='Вторая книга')
        self.h.human_message(thread, 72, 'Тот же участник пишет об иной книге.', 1)
        await self.importer.preparation.select(self.h.guild, 99, 41, 71, decision='included',
                                               book_id=other['id'], confirm=True)
        snapshot = await self.importer.preview(self.h.guild, 99, 41)
        first_ref, other_ref = 'book:' + self.book['id'], 'book:' + other['id']
        for entries in ([{'book_ref': other_ref, 'message_ids': ['61']}],
                        [{'book_ref': first_ref, 'message_ids': ['61', '72']}]):
            with self.subTest(entries=entries), self.assertRaises(ClubError):
                validate_plan(snapshot, {'essays': entries})
        accepted = validate_plan(snapshot, {'essays': [
            {'book_ref': first_ref, 'message_ids': ['61']},
            {'book_ref': other_ref, 'message_ids': ['72']}]})
        self.assertEqual([item['author_id'] for item in accepted['essays']], [1, 1])
        self.runner.analyze.assert_not_awaited()
        self.assert_no_discord_writes()

    async def test_invalid_model_result_is_failed_and_budget_stays_charged(self):
        self.runner.analyze.side_effect = None
        self.runner.analyze.return_value = {'output': {'essays': [{'book_ref': 'outside', 'message_ids': ['61']}]},
                                            'usage_tokens': 101}
        with self.assertRaises(ClubError):
            await self.scan()
        run, = self.store.rows('SELECT state,usage_tokens FROM bc_import_runs')
        self.assertEqual((run['state'], run['usage_tokens']), ('failed', 101))
        budget = self.importer.ledger.budget(self.config.budget_id)
        self.assertEqual(budget['runs_used'], 1)
        self.assertGreater(budget['tokens_reserved'], 101)
        self.assert_no_discord_writes()

    async def test_runner_failure_is_not_retried_or_refunded(self):
        self.runner.analyze.side_effect = OSError('provider internal detail')
        with self.assertRaises(ClubError) as caught:
            await self.scan()
        self.assertNotIn('provider internal detail', str(caught.exception))
        same = await self.scan()
        self.assertEqual(same['state'], 'failed')
        self.runner.analyze.assert_awaited_once()
        self.assertEqual(self.importer.ledger.budget(self.config.budget_id)['runs_used'], 1)

    async def test_cancelled_analysis_keeps_charge_and_is_not_automatically_restarted(self):
        self.runner.analyze.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.scan()
        replay = await self.scan()
        self.assertEqual(replay['state'], 'unknown')
        self.assertEqual(self.importer.ledger.budget(self.config.budget_id)['runs_used'], 1)
        self.runner.analyze.assert_awaited_once()
        self.assert_no_discord_writes()

    async def test_same_request_and_restart_preserve_plan_and_max_run_cap(self):
        self.importer.config = replace(self.config, max_runs=1)
        run = await self.scan()
        self.store = Store(self.path, clock=lambda: self.now)
        self.service = Service(self.h.bot, self.store)
        self.importer = ArchiveImporter(self.service, replace(self.config, max_runs=1), self.runner)
        replay = await self.scan()
        self.assertEqual(replay['id'], run['id'])
        with self.assertRaisesRegex(ClubError, 'Лимит запусков'):
            await self.scan('different-request')
        self.runner.analyze.assert_awaited_once()

    async def test_insufficient_token_reservation_never_calls_model(self):
        self.importer.config = replace(self.config, max_accounted_tokens=1)
        with self.assertRaisesRegex(ClubError, 'резерва'):
            await self.scan()
        self.runner.analyze.assert_not_awaited()
        self.assertEqual(self.importer.ledger.budget(self.config.budget_id)['runs_used'], 0)

    async def test_confirmation_is_required_before_any_copy(self):
        run = await self.scan()
        with self.assertRaisesRegex(ClubError, 'confirm:true'):
            await self.importer.apply(self.h.guild, 99, run['id'])
        self.assert_no_discord_writes()
        self.assertEqual(self.importer.ledger.run(1, run['id'])['state'], 'review')

    async def test_any_changed_original_rejects_whole_plan_before_first_copy(self):
        run = await self.scan()
        self.h.second.content = 'Отредактированный текст'
        with self.assertRaisesRegex(ClubError, 'изменено'):
            await self.apply(run)
        self.assert_no_discord_writes()
        self.assertEqual(self.importer.ledger.run(1, run['id'])['state'], 'review')

    async def test_changed_attachment_rejects_reviewed_plan_without_download_or_copy(self):
        attachment = SimpleNamespace(id=701, filename='essay.txt', size=13, to_file=AsyncMock())
        self.h.first.attachments = [attachment]
        run = await self.scan()
        attachment.id = 702
        with self.assertRaisesRegex(ClubError, 'изменено'):
            await self.apply(run)
        attachment.to_file.assert_not_awaited()
        self.assert_no_discord_writes()

    async def test_removed_allowlist_or_admin_permission_blocks_existing_plan(self):
        run = await self.scan()
        self.importer.config = replace(self.config, allowed_channel_ids=(42,))
        with self.assertRaisesRegex(ClubError, 'не разрешён'):
            await self.apply(run)
        self.importer.config = self.config
        self.h.guild.owner_id = 100
        with self.assertRaisesRegex(ClubError, 'Manage Server'):
            await self.apply(run)
        self.assert_no_discord_writes()

    async def test_review_refuses_to_reveal_snapshot_after_thread_access_revocation(self):
        run = await self.scan()
        self.h.old_thread.permissions_for.return_value.read_message_history = False
        with self.assertRaisesRegex(ClubError, 'Доступ'):
            await self.importer.review(self.h.guild, 99, run['id'])
        self.runner.analyze.assert_awaited_once()
        self.assert_no_discord_writes()

    async def test_destination_change_after_review_refuses_old_plan(self):
        run = await self.scan()
        settings = self.store.settings(1)
        self.store.configure(1, {**settings, 'essays': 44})
        with self.assertRaisesRegex(ClubError, 'Форум назначения изменён'):
            await self.apply(run)
        self.assert_no_discord_writes()

    async def test_approved_copy_preserves_text_files_and_author_without_generated_source_links(self):
        attachment = SimpleNamespace(id=701, filename='essay.txt', size=13,
                                     to_file=AsyncMock(side_effect=lambda: discord.File(io.BytesIO(b'original-file'), filename='essay.txt')))
        self.h.first.attachments = [attachment]
        run = await self.scan()
        await self.apply(run)
        essay, = self.store.essays(self.book['id'])
        self.assertEqual((essay['author_id'], essay['submitted'], essay['managed']), (1, 1, 0))
        target = self.h.channels[essay['source_id']]
        self.assertEqual(target.parent_id, 14)
        starter = target.messages[target.id]
        self.assertIn('<@1>', starter.content)
        self.assertNotIn(self.h.first.jump_url, starter.content)
        hook, = self.h.hooks.values()
        self.assertEqual(hook.send.await_count, 3)
        for message in target.messages.values():
            self.assertEqual(message.webhook_id, hook.id)
            self.assertEqual(message.author.display_name, self.h.members[1].display_name)
            self.assertEqual(message.author.display_avatar.url, str(self.h.members[1].display_avatar.url))
            self.assertNotIn('bc:essay-import:', message.content)
        self.assertEqual(starter.content, archive_header(self.book, 1))
        for snapshot in run['snapshot']['messages']:
            if snapshot['id'] not in ('61', '62'):
                continue
            for index, chunk in enumerate(archive_chunks(snapshot)):
                pub = self.store.publication(f'essay-import:1:61:message:{snapshot["id"]}:{index}:v2')
                self.assertEqual(target.messages[pub['message_id']].content, chunk)
        target.send.assert_not_awaited()
        body_calls = [call for call in hook.send.await_args_list if call.kwargs.get('thread') is not None]
        contents = [call.args[0] for call in body_calls]
        self.assertIn(self.h.first.content, contents[0])
        self.assertIn(self.h.second.content, contents[1])
        self.assertTrue(all(self.h.first.jump_url not in content and self.h.second.jump_url not in content for content in contents))
        self.assertEqual(body_calls[0].kwargs['files'][0].filename, 'essay.txt')
        copied = next(message.attachments[0] for message in target.messages.values() if message.attachments)
        file = await copied.to_file()
        self.assertEqual(file.fp.read(), b'original-file')
        file.close()
        attachment.to_file.assert_awaited_once()
        for call in hook.send.await_args_list:
            self.assertEqual(call.kwargs['username'], self.h.members[1].display_name)
            self.assertEqual(call.kwargs['avatar_url'], str(self.h.members[1].display_avatar.url))
            self.assertFalse(call.kwargs['allowed_mentions'].everyone)
            self.assertFalse(call.kwargs['allowed_mentions'].users)
        self.h.first.edit.assert_not_awaited()
        self.h.second.edit.assert_not_awaited()
        self.assertEqual(self.importer.ledger.run(1, run['id'])['state'], 'done')

    async def test_all_long_body_chunks_are_clean_and_preserve_author_bc_text(self):
        author_text = 'Автор обсуждает bc:essay-import:example.\n-# bc:author-note\n'
        self.h.first.content = author_text + 'Размышления о книге. ' * 170
        run = await self.scan()
        await self.apply(run)
        essay, = self.store.essays(self.book['id'])
        target = self.h.channels[essay['source_id']]
        first_snapshot = next(item for item in run['snapshot']['messages'] if item['id'] == '61')
        chunks = archive_chunks(first_snapshot)
        self.assertGreater(len(chunks), 1)
        actual = []
        for index, expected in enumerate(chunks):
            key = f'essay-import:1:61:message:61:{index}:v2'
            pub = self.store.publication(key)
            message = target.messages[pub['message_id']]
            self.assertEqual(message.content, expected)
            self.assertNotIn('\n-# bc:' + key, message.content)
            actual.append(message.content)
        self.assertEqual(''.join(actual), self.h.first.content)
        self.assertIn(author_text, actual[0])
        self.assertEqual(target.messages[target.id].content, archive_header(self.book, 1))

    async def test_oversize_attachments_explain_limit_without_source_link_or_download(self):
        attachment = SimpleNamespace(id=701, filename='large.zip', size=20_000_000, to_file=AsyncMock())
        self.h.first.attachments = [attachment]
        run = await self.scan()
        self.assertFalse(run['snapshot']['messages'][0]['attachments'][0]['copy'])
        self.assertTrue(run['snapshot']['warnings'])
        await self.apply(run)
        attachment.to_file.assert_not_awaited()
        essay, = self.store.essays(self.book['id'])
        target = self.h.channels[essay['source_id']]
        body = next(message for message in target.messages.values() if message.id != target.id)
        self.assertIn('large.zip', body.content)
        self.assertIn('лимит', body.content)
        self.assertNotIn(self.h.first.jump_url, body.content)
        target.send.assert_not_awaited()

    async def test_repeated_apply_and_scan_do_not_duplicate_or_recharge(self):
        run = await self.scan()
        await self.apply(run)
        essay, = self.store.essays(self.book['id'])
        target = self.h.channels[essay['source_id']]
        hook, = self.h.hooks.values()
        before_sends = hook.send.await_count
        await self.apply(run)
        await self.scan()
        self.assertEqual(hook.send.await_count, before_sends)
        target.send.assert_not_awaited()
        self.assertEqual(len(self.store.essays(self.book['id'])), 1)
        self.assertEqual(hook.send.await_count, 3)
        self.runner.analyze.assert_awaited_once()

    async def test_lost_body_send_ack_recovers_existing_message_without_duplicates(self):
        run = await self.scan()
        hook = self.h.webhook(14)
        self.store.save_webhook(1, 14, hook.id)
        original_send = hook.send.side_effect
        async def send_then_lose_ack(content, **fields):
            if fields.get('thread') is not None:
                hook.send.side_effect = original_send
                await original_send(content, **fields)
                raise OSError('lost acknowledgement')
            return await original_send(content, **fields)
        hook.send.side_effect = send_then_lose_ack
        with self.assertRaisesRegex(OSError, 'lost acknowledgement'):
            await self.apply(run)
        self.assertEqual(self.importer.ledger.run(1, run['id'])['state'], 'review')
        await self.apply(run)
        essay, = self.store.essays(self.book['id'])
        target = self.h.channels[essay['source_id']]
        self.assertEqual(len(target.messages), 3)
        self.assertTrue(all('bc:essay-import:' not in message.content for message in target.messages.values()))
        target.send.assert_not_awaited()
        self.assertEqual(hook.send.await_count, 3)
        self.runner.analyze.assert_awaited_once()

    async def test_lost_starter_ack_recovers_same_webhook_thread_after_restart(self):
        run = await self.scan()
        hook = self.h.webhook(14)
        self.store.save_webhook(1, 14, hook.id)
        original_send = hook.send.side_effect
        async def send_then_lose_ack(*args, **kwargs):
            hook.send.side_effect = original_send
            await original_send(*args, **kwargs)
            raise OSError('lost starter acknowledgement')
        hook.send.side_effect = send_then_lose_ack
        with self.assertRaisesRegex(OSError, 'lost starter acknowledgement'):
            await self.apply(run)
        self.store = Store(self.path, clock=lambda: self.now)
        self.service = Service(self.h.bot, self.store)
        self.importer = ArchiveImporter(self.service, self.config, self.runner)
        await self.apply(run)
        self.assertEqual(hook.send.await_count, 3)
        essay, = self.store.essays(self.book['id'])
        target = self.h.channels[essay['source_id']]
        self.assertEqual(len(target.messages), 3)
        self.assertTrue(all('bc:essay-import:' not in message.content for message in target.messages.values()))
        target.send.assert_not_awaited()
        self.runner.analyze.assert_awaited_once()

    async def test_lost_starter_cleanup_ack_reuses_known_clean_thread_after_restart(self):
        run = await self.scan()
        hook = self.h.webhook(14)
        self.store.save_webhook(1, 14, hook.id)
        key = 'essay-import:1:61'
        self.store.reserve_publication(key, 1, 14, webhook_id=hook.id)
        old = await hook.send(archive_header(self.book, 1) + '\n-# bc:' + key,
                              thread_name='Старый импорт', username='Автор', avatar_url='', wait=True, allowed_mentions=discord.AllowedMentions.none())
        self.store.save_publication(key, old.channel.id, old.id)
        original_edit = hook.edit_message.side_effect
        observed_binding = []
        async def edit_then_lose_ack(message_id, **kwargs):
            if kwargs['content'] == archive_header(self.book, 1):
                observed_binding.append(self.store.publication('essay-import:1:61')['message_id'])
                hook.edit_message.side_effect = original_edit
                await original_edit(message_id, **kwargs)
                raise OSError('lost starter cleanup acknowledgement')
            return await original_edit(message_id, **kwargs)
        hook.edit_message.side_effect = edit_then_lose_ack
        with self.assertRaisesRegex(OSError, 'lost starter cleanup acknowledgement'):
            await self.apply(run)
        pub = self.store.publication('essay-import:1:61')
        self.assertEqual(observed_binding, [pub['message_id']])
        thread_id = pub['channel_id']
        self.assertEqual(self.h.channels[thread_id].messages[thread_id].content, archive_header(self.book, 1))
        self.store = Store(self.path, clock=lambda: self.now)
        self.service = Service(self.h.bot, self.store)
        self.importer = ArchiveImporter(self.service, self.config, self.runner)
        await self.apply(run)
        essay, = self.store.essays(self.book['id'])
        self.assertEqual(essay['source_id'], thread_id)
        self.assertEqual(len(self.h.channels[thread_id].messages), 3)
        self.assertTrue(all('bc:essay-import:' not in message.content
                            for message in self.h.channels[thread_id].messages.values()))
        self.assertEqual(hook.send.await_count, 3)
        self.runner.analyze.assert_awaited_once()

    async def test_partial_copy_deleted_before_retry_is_recreated_with_attachment(self):
        attachment = SimpleNamespace(id=701, filename='essay.txt', size=13,
                                     to_file=AsyncMock(side_effect=lambda: discord.File(io.BytesIO(b'original-file'), filename='essay.txt')))
        self.h.first.attachments = [attachment]
        run = await self.scan()
        original_upsert = self.importer.publisher.upsert
        async def interrupt_second(guild, thread, key, *args, **kwargs):
            if ':message:62:' in key:
                raise OSError('interrupted before second part')
            return await original_upsert(guild, thread, key, *args, **kwargs)
        self.importer.publisher.upsert = interrupt_second
        with self.assertRaisesRegex(OSError, 'interrupted before second part'):
            await self.apply(run)
        first_copy = self.store.publication('essay-import:1:61:message:61:0:v2')
        target = self.h.channels[first_copy['channel_id']]
        del target.messages[first_copy['message_id']]
        self.importer.publisher.upsert = original_upsert
        await self.apply(run)
        attachment.to_file.assert_awaited()
        self.assertEqual(attachment.to_file.await_count, 2)
        self.assertEqual(len(target.messages), 3)
        hook, = self.h.hooks.values()
        body_calls = [call for call in hook.send.await_args_list if call.kwargs.get('thread') is not None]
        self.assertEqual(body_calls[1].kwargs['files'][0].filename, 'essay.txt')
        target.send.assert_not_awaited()
        self.assertEqual(len(self.store.essays(self.book['id'])), 1)
        self.runner.analyze.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()

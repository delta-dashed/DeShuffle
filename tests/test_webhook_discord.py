"""Webhook essay transport and provenance with no Discord connection or real token."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import discord

from bookclub.store import ClubError, Store
from bookclub.ui import BookView, Club
from test_bookclub import ClubFixture
from test_bookclub_discord import DiscordHarness, not_found


class WebhookHarness(DiscordHarness):
    """Keep ordinary fetched Messages distinct from returned WebhookMessages."""

    def __init__(self):
        super().__init__()
        self.hooks = {}
        for channel in self.channels.values():
            if not isinstance(channel, discord.ForumChannel):
                continue

            async def webhooks(forum=channel):
                return [hook for hook in self.hooks.values() if hook.channel_id == forum.id]

            async def create_webhook(*, name, forum=channel, **kwargs):
                return self.webhook(forum.id, name=name)

            channel.webhooks = AsyncMock(side_effect=webhooks)
            channel.create_webhook = AsyncMock(side_effect=create_webhook)

        async def fetch_webhook(webhook_id):
            if webhook_id not in self.hooks:
                raise not_found()
            return self.hooks[webhook_id]

        self.bot.fetch_webhook = AsyncMock(side_effect=fetch_webhook)

    def webhook(self, channel_id=14, *, name='Test essay webhook', creator=None):
        self.seq += 1
        hook = Mock(spec=discord.Webhook)
        hook.id, hook.type = self.seq, discord.WebhookType.incoming
        hook.name, hook.channel_id, hook.guild_id = name, channel_id, self.guild.id
        hook.user = self.bot.user if creator is None else creator
        hook.token = 'mock-transport-token-not-a-real-credential'

        async def send(content, *, thread_name=None, thread=None, username, avatar_url, wait, allowed_mentions, **kwargs):
            import io
            assert wait is True, 'Discord must return the created forum post'
            assert (thread_name is None) != (thread is None), 'Choose a new or an existing forum thread'
            self.seq += 1
            if thread is None:
                thread = self.channel(self.seq, parent_id=channel_id, owner_id=hook.id, name=thread_name)
            else:
                thread = self.channels[thread.id]
                assert thread.parent_id == channel_id
            starter = self.message(thread, self.seq, content)
            starter.webhook_id = hook.id
            starter.author = SimpleNamespace(id=hook.id, bot=True, display_name=username,
                                             display_avatar=SimpleNamespace(url=avatar_url))
            starter.attachments = []
            for index, file in enumerate(kwargs.get('files') or []):
                position = file.fp.tell()
                payload = file.fp.read()
                file.fp.seek(position)
                async def to_file(*, data=payload, filename=file.filename, **options):
                    return discord.File(io.BytesIO(data), filename=filename)
                starter.attachments.append(SimpleNamespace(
                    id=starter.id * 100 + index, filename=file.filename, size=len(payload),
                    to_file=AsyncMock(side_effect=to_file)))
            async def delete(**options):
                if starter.id not in thread.messages:
                    raise not_found()
                del thread.messages[starter.id]
            starter.delete = AsyncMock(side_effect=delete)
            partial = Mock(spec=discord.PartialMessageable)
            partial.id, partial.guild = thread.id, self.guild
            response = Mock(spec=discord.WebhookMessage)
            response.id, response.channel = starter.id, partial
            response.content, response.author, response.webhook_id = content, starter.author, hook.id
            response.attachments, response.delete = starter.attachments, starter.delete
            return response

        async def edit_message(message_id, *, thread, content, **kwargs):
            actual_thread = self.channels[thread.id]
            if message_id not in actual_thread.messages:
                raise not_found()
            message = actual_thread.messages[message_id]
            assert message.webhook_id == hook.id
            message.content = content
            return message

        hook.send = AsyncMock(side_effect=send)
        hook.edit_message = AsyncMock(side_effect=edit_message)
        self.hooks[hook.id] = hook
        return hook


class WebhookEssayTests(ClubFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.h = WebhookHarness()
        self.h.members[1].display_name = 'Анна Петрова'
        self.h.members[2].display_name = 'Сергей'
        self.cog = Club(self.h.bot, self.store)
        self.service = self.cog.service
        self.store.set_published(1)

    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 1.0

    async def create(self, actor_id=1, book_id=None):
        return await self.service.create_essay_space(self.h.guild, book_id or self.book['id'], actor_id)

    def row(self, thread_id):
        return self.store.one('SELECT * FROM bc_essays WHERE guild_id=1 AND source_id=?', (thread_id,))

    def starter(self, essay):
        thread = self.h.channels[essay['source_id']]
        return thread.messages[thread.id]

    def body(self, thread_id, text='Моё эссе', actor_id=1):
        self.h.seq += 1
        message = self.h.message(self.h.channels[thread_id], self.h.seq, text)
        message.author = self.h.members[actor_id]
        return message

    def restart(self):
        self.cog = Club(self.h.bot, Store(self.path, clock=lambda: self.now))
        self.service = self.cog.service

    def context(self, actor_id=1):
        return SimpleNamespace(guild=self.h.guild, author=self.h.members[actor_id],
                               interaction=self.h.interaction(actor_id))

    async def test_default_uses_clickers_nickname_avatar_and_wait_for_forum_post(self):
        self.assertTrue(self.store.settings(1)['essay_webhooks'])
        view = BookView(self.cog, self.book)
        button = next(child for child in view.children if child.custom_id.endswith(':write'))
        interaction = self.h.interaction(1)
        await button.callback(interaction)
        essay, = self.store.essays(self.book['id'], submitted_only=False)
        hook, = self.h.hooks.values()
        sent = hook.send.await_args
        self.assertEqual(sent.kwargs['username'], self.h.members[1].display_name)
        self.assertEqual(sent.kwargs['avatar_url'], self.h.members[1].display_avatar.url)
        self.assertIs(sent.kwargs['wait'], True)
        self.assertIn('Книга', sent.kwargs['thread_name'])
        self.assertIn('Анна Петрова', sent.kwargs['thread_name'])
        self.assertFalse(sent.kwargs['allowed_mentions'].everyone)
        self.assertFalse(sent.kwargs['allowed_mentions'].users)
        self.assertEqual(essay['author_id'], 1)
        self.assertEqual(self.starter(essay).webhook_id, hook.id)
        self.assertEqual(self.h.channels[essay['source_id']].parent_id, 14)
        pub = self.store.publication(f'essay-space:{self.book["id"]}:1')
        self.assertEqual((pub['webhook_id'], pub['message_id']), (hook.id, essay['source_id']))
        self.assertEqual(interaction.followup.send.await_args.kwargs['view'].children[0].url, essay['url'])
        self.h.channels[14].create_thread.assert_not_awaited()

    async def test_concurrent_authors_share_one_hook_with_independent_display_identity(self):
        first, second = await asyncio.gather(self.create(1), self.create(2))
        hook, = self.h.hooks.values()
        self.h.channels[14].create_webhook.assert_awaited_once()
        self.assertEqual(hook.send.await_count, 2)
        self.assertNotEqual(first['source_id'], second['source_id'])
        for essay in (first, second):
            member = self.h.members[essay['author_id']]
            message = self.starter(essay)
            self.assertEqual(message.author.display_name, member.display_name)
            self.assertEqual(message.author.display_avatar.url, member.display_avatar.url)
        hook.edit.assert_not_called()

    async def test_same_author_double_click_restart_and_archive_reuse_post(self):
        first, second = await asyncio.gather(self.create(), self.create())
        self.assertEqual(first['source_id'], second['source_id'])
        self.restart()
        thread = self.h.channels[first['source_id']]
        thread.archived = True
        self.h.members[1].display_name = 'Новое имя'
        third = await self.create()
        self.assertEqual(third['source_id'], first['source_id'])
        self.assertFalse(thread.archived)
        hook, = self.h.hooks.values()
        hook.send.assert_awaited_once()
        self.h.channels[14].create_webhook.assert_awaited_once()

    async def test_service_header_is_draft_and_only_authors_own_body_submits(self):
        essay = await self.create()
        starter = self.starter(essay)
        await self.cog.on_message(starter)
        await self.cog.on_message(self.body(essay['source_id'], actor_id=2))
        self.assertEqual(self.row(essay['source_id'])['submitted'], 0)
        message = self.body(essay['source_id'])
        await self.cog.on_message(message)
        self.assertEqual((self.row(essay['source_id'])['author_id'], self.row(essay['source_id'])['submitted']), (1, 1))
        self.assertNotIn(1, {p['user_id'] for p in self.store.missing_essays(self.book['id'])})
        book_pub = self.store.publication(f'book:{self.book["id"]}')
        book_text = '\n'.join(m.content for m in self.h.channels[book_pub['channel_id']].messages.values())
        self.assertIn(essay['url'], book_text)
        self.assertIn('<@1>', book_text)
        message.edit.assert_not_awaited()

    async def test_lost_webhook_creation_ack_reuses_listed_hook_without_duplicate(self):
        forum = self.h.channels[14]
        original = forum.create_webhook.side_effect

        async def create_then_lose_ack(**kwargs):
            await original(**kwargs)
            raise OSError('lost webhook creation acknowledgement')

        forum.create_webhook.side_effect = create_then_lose_ack
        with self.assertRaises(OSError):
            await self.create()
        self.restart()
        essay = await self.create()
        self.assertEqual(essay['author_id'], 1)
        forum.create_webhook.assert_awaited_once()
        hook, = self.h.hooks.values()
        hook.send.assert_awaited_once()
        self.assertGreaterEqual(forum.webhooks.await_count, 1)

    async def test_lost_post_ack_recovers_on_click_after_restart(self):
        first = await self.create(2)
        hook, = self.h.hooks.values()
        original = hook.send.side_effect

        async def send_then_lose_ack(*args, **kwargs):
            await original(*args, **kwargs)
            raise OSError('lost post acknowledgement')

        hook.send.side_effect = send_then_lose_ack
        with self.assertRaises(OSError):
            await self.create(1)
        pub = self.store.publication(f'essay-space:{self.book["id"]}:1')
        self.assertEqual(pub['webhook_id'], hook.id)
        self.assertIsNone(pub['message_id'])
        self.restart()
        recovered = await self.create(1)
        self.assertNotEqual(first['source_id'], recovered['source_id'])
        self.assertEqual((recovered['author_id'], recovered['managed'], recovered['submitted']), (1, 1, 0))
        self.assertEqual(hook.send.await_count, 2)
        self.assertEqual(self.store.publication(pub['key'])['state'], 'ready')

    async def test_scan_recovers_lost_post_ack_and_body_without_another_click(self):
        await self.create(2)
        hook, = self.h.hooks.values()
        original = hook.send.side_effect

        async def send_then_lose_ack(*args, **kwargs):
            message = await original(*args, **kwargs)
            self.body(message.channel.id, 'Уже написанное эссе')
            raise OSError('lost post acknowledgement')

        hook.send.side_effect = send_then_lose_ack
        with self.assertRaises(OSError):
            await self.create(1)
        self.restart()
        await self.service.scan_essays(self.h.guild)
        await self.service.scan_essays(self.h.guild)
        submitted, = self.store.essays(self.book['id'])
        self.assertEqual((submitted['author_id'], submitted['managed']), (1, 1))
        self.assertEqual(hook.send.await_count, 2)
        self.assertEqual(self.store.publication(f'essay-space:{self.book["id"]}:1')['state'], 'ready')

    async def test_correction_edits_header_through_webhook_and_preserves_authors_body(self):
        essay = await self.create()
        message = self.body(essay['source_id'], 'Авторский текст не меняется')
        await self.cog.on_message(message)
        second = self.store.create_book(1, 'Вторая книга', 'Другой автор', '', 'second')
        await Club.essay.callback(self.cog, self.context(), second['id'], essay['url'], True)
        moved = self.row(essay['source_id'])
        self.assertEqual((moved['book_id'], moved['author_id'], moved['submitted']), (second['id'], 1, 1))
        hook, = self.h.hooks.values()
        self.assertGreaterEqual(hook.edit_message.await_count, 1)
        self.assertTrue(all(call.args[0] == essay['source_id'] for call in hook.edit_message.await_args_list))
        starter = self.starter(essay)
        self.assertIn('«Вторая книга»', starter.content)
        starter.edit.assert_not_awaited()
        message.edit.assert_not_awaited()
        self.assertEqual(message.content, 'Авторский текст не меняется')
        same = await self.create(book_id=second['id'])
        self.assertEqual(same['source_id'], essay['source_id'])
        hook.send.assert_awaited_once()

    async def test_deleted_webhook_preserves_existing_attribution_and_diagnoses(self):
        essay = await self.create()
        await self.cog.on_message(self.body(essay['source_id']))
        hook, = self.h.hooks.values()
        del self.h.hooks[hook.id]
        self.restart()
        await self.service.scan_essays(self.h.guild)
        reused = await self.create()
        self.assertEqual((reused['source_id'], reused['author_id'], reused['submitted']), (essay['source_id'], 1, 1))
        hook.send.assert_awaited_once()
        self.h.channels[14].create_webhook.assert_awaited_once()
        diagnostics = '\n'.join(await self.service.diagnose(self.h.guild)).lower()
        self.assertRegex(diagnostics, 'webhook|вебхук')
        self.assertRegex(diagnostics, 'недоступ|удал|не найден')

    async def test_missing_manage_webhooks_permission_is_explained_without_bot_fallback(self):
        for permission in ('manage_webhooks', 'manage_threads'):
            with self.subTest(permission=permission):
                setattr(self.h.channels[14].permissions_for.return_value, permission, False)
                with self.assertRaises(ClubError) as raised:
                    await self.create()
                self.assertIn(permission.replace('_', ' '), str(raised.exception).lower())
                setattr(self.h.channels[14].permissions_for.return_value, permission, True)
        self.h.channels[14].create_webhook.assert_not_awaited()
        self.h.channels[14].create_thread.assert_not_awaited()
        self.assertEqual(self.store.essays(self.book['id'], submitted_only=False), [])

    async def test_forbidden_webhook_creation_does_not_fall_back_to_bot_post(self):
        forbidden = discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'),
                                      {'code': 50013, 'message': 'Missing Permissions'})
        self.h.channels[14].create_webhook.side_effect = forbidden
        with self.assertRaises((ClubError, discord.Forbidden)):
            await self.create()
        self.h.channels[14].create_thread.assert_not_awaited()
        self.assertEqual(self.store.essays(self.book['id'], submitted_only=False), [])

    async def test_foreign_webhook_cannot_recover_a_reserved_human_identity(self):
        await self.create(2)
        trusted, = self.h.hooks.values()

        async def no_post(*args, **kwargs):
            raise OSError('no acknowledged post')

        trusted.send.side_effect = no_post
        with self.assertRaises(OSError):
            await self.create(1)
        foreign = self.h.webhook(creator=SimpleNamespace(id=3, bot=False))
        forged = await foreign.send(f'-# bc:essay-space:{self.book["id"]}:1',
                                    thread_name='Книга · Анна Петрова', username='Анна Петрова',
                                    avatar_url=self.h.members[1].display_avatar.url, wait=True,
                                    allowed_mentions=discord.AllowedMentions.none())
        thread = self.h.channels[forged.channel.id]
        self.assertFalse(await self.service.register_thread(thread, prompt=False))
        self.assertIsNone(self.row(thread.id))
        self.restart()
        with self.assertRaises(ClubError):
            await self.create(1)
        self.assertIsNone(self.store.publication(f'essay-space:{self.book["id"]}:1')['message_id'])

    async def test_changed_webhook_on_registered_post_cannot_retain_managed_identity(self):
        essay = await self.create()
        starter = self.starter(essay)
        foreign = self.h.webhook(creator=SimpleNamespace(id=2, bot=False))
        starter.webhook_id = foreign.id
        starter.author = SimpleNamespace(id=foreign.id, bot=True)
        thread = self.h.channels[essay['source_id']]
        self.assertFalse(await self.service.register_thread(thread, prompt=False))
        with self.assertRaises(ClubError):
            await self.create()
        foreign.send.assert_not_awaited()

    async def test_legacy_bot_post_reuses_and_edits_after_enabling_webhooks(self):
        self.store.configure(1, dict(self.store.settings(1), essay_webhooks=False))
        legacy = await self.create()
        self.assertIsNone(self.starter(legacy).webhook_id)
        self.store.configure(1, dict(self.store.settings(1), essay_webhooks=True))
        self.restart()
        await self.service.scan_essays(self.h.guild)
        reused = await self.create()
        self.assertEqual(reused['source_id'], legacy['source_id'])
        second = self.store.create_book(1, 'Новая книга', 'Автор', '', 'second')
        await Club.essay.callback(self.cog, self.context(), second['id'], legacy['url'], True)
        self.assertEqual(self.row(legacy['source_id'])['book_id'], second['id'])
        self.assertIn('«Новая книга»', self.starter(legacy).content)
        self.assertGreaterEqual(self.starter(legacy).edit.await_count, 1)
        self.h.channels[14].create_webhook.assert_not_awaited()
        self.h.channels[14].create_thread.assert_awaited_once()

    async def test_webhook_token_is_never_persisted_in_database(self):
        await self.create()
        hook, = self.h.hooks.values()
        self.assertNotIn(hook.token.encode(), self.path.read_bytes())

    async def test_webhook_deleted_between_fetch_and_edit_does_not_delete_essay_or_duplicate_post(self):
        essay = await self.create()
        message = self.body(essay['source_id'])
        await self.cog.on_message(message)
        hook, = self.h.hooks.values()
        self.store.update_book(1, self.book['id'], title='Переименованная книга')

        async def deleted_during_edit(*args, **kwargs):
            del self.h.hooks[hook.id]
            raise discord.NotFound(SimpleNamespace(status=404, reason='Not Found'),
                                   {'code': 10015, 'message': 'Unknown Webhook'})

        hook.edit_message.side_effect = deleted_during_edit
        with self.assertLogs('bookclub.service', level='WARNING'):
            reused = await self.create()
        self.assertEqual(reused['source_id'], essay['source_id'])
        self.assertEqual((reused['deleted'], reused['submitted'], reused['author_id']), (0, 1, 1))
        hook.send.assert_awaited_once()
        self.h.channels[14].create_webhook.assert_awaited_once()

    async def test_repair_recovers_webhook_post_without_forgetting_origin_or_duplicate(self):
        await self.create(2)
        hook, = self.h.hooks.values()
        send = hook.send.side_effect

        async def lost_ack(*args, **kwargs):
            await send(*args, **kwargs)
            raise OSError('lost response')

        hook.send.side_effect = lost_ack
        with self.assertRaises(OSError):
            await self.create(1)
        key = f'essay-space:{self.book["id"]}:1'
        self.assertIsNone(self.store.publication(key)['message_id'])
        await Club.repair.callback(self.cog, self.context(99), True)
        recovered = self.store.publication(key)
        self.assertEqual(recovered['webhook_id'], hook.id)
        self.assertEqual(recovered['state'], 'ready')
        await self.create(1)
        self.assertEqual(hook.send.await_count, 2)
        self.assertEqual(self.row(recovered['message_id'])['author_id'], 1)


if __name__ == '__main__':
    unittest.main()

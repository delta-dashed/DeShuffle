"""Lost Discord responses recover clean publications without a second send."""
import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import discord

from bookclub.import_publication import ImportPublisher, archive_header
from bookclub.service import Service
from bookclub.store import ClubError, Store
from test_archive_import import ArchiveHarness
from test_bookclub import ClubFixture


class CleanDeliveryTests(ClubFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.h = ArchiveHarness()
        self.service = Service(self.h.bot, self.store)
        self.publisher = ImportPublisher(self.service)

    def restart(self):
        self.store = Store(self.path, clock=lambda: self.now)
        self.service = Service(self.h.bot, self.store)
        self.publisher = ImportPublisher(self.service)

    def view(self, custom_id='club:catalog:manage'):
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label='Управление книгой', custom_id=custom_id))
        view.add_item(discord.ui.Button(label='Открыть', url='https://discord.com/channels/1/13/80'))
        return view

    def hook(self):
        hook = self.h.webhook(14)
        self.store.save_webhook(1, 14, hook.id)
        return hook

    def lose_response(self, method):
        original = method.side_effect
        async def send_then_lose(*args, **kwargs):
            await original(*args, **kwargs)
            raise OSError('lost Discord response')
        method.side_effect = send_then_lose

    async def test_catalog_page_is_clean_on_first_send_and_recovers_after_restart(self):
        channel, key, body = self.h.channels[11], 'catalog:1:page:1', 'Первая страница каталога'
        view = self.view()
        earlier = self.h.message(channel, 900, body, view=view)
        self.lose_response(channel.send)
        with self.assertRaisesRegex(OSError, 'lost Discord response'):
            await self.service.upsert(self.h.guild, key, channel.id, body, view=view)
        self.assertIsNone(self.store.publication(key)['message_id'])
        sent = next(message for message in channel.messages.values() if message.id != earlier.id)
        self.assertEqual(sent.content, body)
        self.assertEqual(channel.send.await_args.args[0], body)
        self.assertEqual(str(sent.nonce), self.service.delivery.intent(key)['nonce'])
        self.assertNotIn('bc:', sent.content)
        self.restart()
        for _ in range(2):
            recovered = await self.service.upsert(self.h.guild, key, channel.id, body, view=view)
            self.assertEqual(recovered['message_id'], sent.id)
        self.assertEqual(set(channel.messages), {earlier.id, sent.id})
        channel.send.assert_awaited_once()
        self.assertEqual([component.to_dict() for component in sent.components], view.to_components())
        earlier.edit.assert_not_awaited()

    async def test_lost_forum_card_response_recovers_archived_renamed_thread_and_buttons(self):
        forum, key, body = self.h.channels[13], 'book:' + self.book['id'], 'Карточка книги'
        view = self.view('club:book:controls')
        self.lose_response(forum.create_thread)
        with self.assertRaisesRegex(OSError, 'lost Discord response'):
            await self.service.upsert(self.h.guild, key, forum.id, body, forum_name='Книга', view=view)
        new_thread, = [channel for channel in self.h.channels.values() if channel.parent_id == forum.id]
        message = new_thread.messages[new_thread.id]
        self.assertEqual(forum.create_thread.await_args.kwargs['content'], body)
        self.assertEqual(message.content, body)
        new_thread.archived, new_thread.name = True, 'Название изменено организатором'
        self.restart()
        recovered = await self.service.upsert(self.h.guild, key, forum.id, body, forum_name='Книга', view=view)
        self.assertEqual((recovered['channel_id'], recovered['message_id']), (new_thread.id, message.id))
        self.assertEqual(new_thread.name, 'Название изменено организатором')
        self.assertEqual([component.to_dict() for component in message.components], view.to_components())
        forum.create_thread.assert_awaited_once()

    async def test_lost_webhook_header_response_recovers_same_author_thread_without_visible_code(self):
        hook = self.hook()
        key, body = 'essay-import:1:61', archive_header(self.book, 1)
        self.lose_response(hook.send)
        with self.assertRaisesRegex(OSError, 'lost Discord response'):
            await self.service.publish_essay_starter(self.h.guild, self.h.channels[14], key,
                                                      'Книга · Эссе · Участник 1', body, self.h.members[1])
        thread, = [channel for channel in self.h.channels.values() if channel.parent_id == 14]
        message = thread.messages[thread.id]
        self.assertEqual(hook.send.await_args.args[0], body)
        self.assertEqual(message.content, body)
        self.assertEqual(message.author.display_name, self.h.members[1].display_name)
        self.assertEqual(message.author.display_avatar.url, self.h.members[1].display_avatar.url)
        self.restart()
        recovered = await self.service.publish_essay_starter(self.h.guild, self.h.channels[14], key,
                                                             'Книга · Эссе · Участник 1', body, self.h.members[1])
        self.assertEqual((recovered['channel_id'], recovered['message_id']), (thread.id, message.id))
        self.assertNotIn('bc:', message.content)
        hook.send.assert_awaited_once()
        hook.edit_message.assert_not_awaited()

    async def test_lost_webhook_body_response_recovers_edge_spaces_and_original_attachment(self):
        hook, thread = self.hook(), self.h.channel(80, parent_id=14)
        existing_ids = set(thread.messages)
        key, body = 'essay-import:1:61:message:61:0:v2', ' \tТекст эссе с вложением. \t'
        self.lose_response(hook.send)
        file = discord.File(io.BytesIO(b'original file'), filename='essay.txt')
        try:
            with self.assertRaisesRegex(OSError, 'lost Discord response'):
                await self.publisher.upsert(self.h.guild, thread, key, body, self.h.members[1],
                                             files=[file], expected_attachments=[('essay.txt', 13)])
        finally:
            file.close()
        message, = [item for item in thread.messages.values() if item.webhook_id == hook.id]
        self.assertEqual(message.content, body)
        self.assertEqual(hook.send.await_args.args[0], body)
        message.content = body.strip(' \t')  # Discord's observed normalization.
        self.restart()
        recovered = await self.publisher.upsert(self.h.guild, thread, key, body, self.h.members[1],
                                                 expected_attachments=[('essay.txt', 13)])
        self.assertEqual(recovered['message_id'], message.id)
        attachment, = message.attachments
        saved = await attachment.to_file()
        try:
            self.assertEqual(saved.fp.read(), b'original file')
        finally:
            saved.close()
        self.assertEqual(set(thread.messages), existing_ids | {message.id})
        hook.send.assert_awaited_once()
        hook.edit_message.assert_not_awaited()

    async def test_failed_binding_save_recovers_clean_page_without_second_send(self):
        channel, key, body = self.h.channels[11], 'catalog:1:page:2', 'Следующая страница'
        with patch.object(self.store, 'save_publication', side_effect=OSError('database unavailable')):
            with self.assertRaisesRegex(OSError, 'database unavailable'):
                await self.service.upsert(self.h.guild, key, channel.id, body)
        message, = channel.messages.values()
        self.assertEqual(message.content, body)
        self.assertIsNone(self.store.publication(key)['message_id'])
        self.restart()
        recovered = await self.service.upsert(self.h.guild, key, channel.id, body)
        self.assertEqual(recovered['message_id'], message.id)
        channel.send.assert_awaited_once()

    async def test_recovered_id_is_durable_before_changed_card_edit_loses_its_response(self):
        channel, key = self.h.channels[11], 'catalog:1:page:7'
        self.lose_response(channel.send)
        with self.assertRaisesRegex(OSError, 'lost Discord response'):
            await self.service.upsert(self.h.guild, key, channel.id, 'Старое состояние каталога')
        message, = channel.messages.values()
        self.restart()
        original_edit = message.edit.side_effect
        async def edit_then_lose_response(**kwargs):
            await original_edit(**kwargs)
            raise OSError('lost edit response')
        message.edit.side_effect = edit_then_lose_response
        new_view = self.view('club:updated:controls')
        with self.assertRaisesRegex(OSError, 'lost edit response'):
            await self.service.upsert(self.h.guild, key, channel.id, 'Обновлённый каталог', view=new_view)
        self.assertEqual(message.content, 'Обновлённый каталог')
        self.assertEqual(self.store.publication(key)['message_id'], message.id)
        self.restart()
        message.edit.side_effect = original_edit
        recovered = await self.service.upsert(self.h.guild, key, channel.id, 'Обновлённый каталог', view=new_view)
        self.assertEqual(recovered['message_id'], message.id)
        channel.send.assert_awaited_once()
        self.assertEqual([component.to_dict() for component in message.components], new_view.to_components())

    async def test_pending_send_without_matching_message_fails_closed_on_each_retry(self):
        channel, key, body = self.h.channels[11], 'catalog:1:page:3', 'Не подтверждённая страница'
        earlier = self.h.message(channel, 900, body)
        await self.service.delivery.reserve(self.h.guild, channel, key, body)
        self.restart()
        for _ in range(2):
            with self.assertRaisesRegex(ClubError, 'не подтверждена'):
                await self.service.upsert(self.h.guild, key, channel.id, body)
        self.assertIsNone(self.store.publication(key)['message_id'])
        self.assertEqual(set(channel.messages), {earlier.id})
        channel.send.assert_not_awaited()
        earlier.edit.assert_not_awaited()

    async def test_other_key_waits_for_unacknowledged_same_channel_publication(self):
        channel, first_key, next_key = self.h.channels[11], 'catalog:1:page:8', 'catalog:1:page:9'
        body = 'Одинаковый текст разных карточек'
        original_send = channel.send.side_effect
        self.lose_response(channel.send)
        with self.assertRaisesRegex(OSError, 'lost Discord response'):
            await self.service.upsert(self.h.guild, first_key, channel.id, body)
        first, = channel.messages.values()
        self.restart()
        with self.assertRaises(ClubError):
            await self.service.upsert(self.h.guild, next_key, channel.id, body)
        self.assertIsNone(self.store.publication(next_key))
        self.assertEqual(set(channel.messages), {first.id})
        channel.send.assert_awaited_once()
        recovered = await self.service.upsert(self.h.guild, first_key, channel.id, body)
        self.assertEqual(recovered['message_id'], first.id)
        channel.send.side_effect = original_send
        following = await self.service.upsert(self.h.guild, next_key, channel.id, body)
        self.assertNotEqual(following['message_id'], first.id)
        self.assertEqual(set(channel.messages), {first.id, following['message_id']})
        self.assertEqual(channel.send.await_count, 2)

    async def test_other_webhook_body_key_cannot_obscure_pending_sender_recovery(self):
        hook, thread = self.hook(), self.h.channel(80, parent_id=14)
        first_key, next_key = 'essay-import:1:61:message:61:0:v2', 'essay-import:1:61:message:61:1:v2'
        body, original_send = 'Повторяющийся абзац эссе', hook.send.side_effect
        self.lose_response(hook.send)
        with self.assertRaisesRegex(OSError, 'lost Discord response'):
            await self.publisher.upsert(self.h.guild, thread, first_key, body, self.h.members[1])
        first, = [message for message in thread.messages.values() if message.webhook_id == hook.id]
        self.restart()
        with self.assertRaises(ClubError):
            await self.publisher.upsert(self.h.guild, thread, next_key, body, self.h.members[1])
        self.assertIsNone(self.store.publication(next_key))
        hook.send.assert_awaited_once()
        recovered = await self.publisher.upsert(self.h.guild, thread, first_key, body, self.h.members[1])
        self.assertEqual(recovered['message_id'], first.id)
        hook.send.side_effect = original_send
        following = await self.publisher.upsert(self.h.guild, thread, next_key, body, self.h.members[1])
        self.assertNotEqual(following['message_id'], first.id)
        self.assertEqual(hook.send.await_count, 2)

    async def test_ambiguous_webhook_bodies_fail_closed_without_any_additional_copy(self):
        hook, thread = self.hook(), self.h.channel(80, parent_id=14)
        key, body = 'essay-import:1:61:message:61:0:v2', 'Одинаковое эссе'
        await self.service.delivery.reserve(self.h.guild, thread, key, body, webhook_id=hook.id)
        for _ in range(2):
            await hook.send(body, thread=discord.Object(thread.id), username=self.h.members[1].display_name,
                            avatar_url=self.h.members[1].display_avatar.url, wait=True,
                            allowed_mentions=discord.AllowedMentions.none())
        ids = set(thread.messages)
        self.restart()
        for _ in range(2):
            with self.assertRaisesRegex(ClubError, 'несколько совпадающих'):
                await self.publisher.upsert(self.h.guild, thread, key, body, self.h.members[1])
        self.assertEqual(set(thread.messages), ids)
        self.assertEqual(hook.send.await_count, 2)
        self.assertIsNone(self.store.publication(key)['message_id'])
        hook.edit_message.assert_not_awaited()

    async def test_controls_nonce_sender_and_files_must_match_before_recovery(self):
        channel, key, body = self.h.channels[11], 'catalog:1:page:4', 'Проверяемая страница'
        view = self.view()
        await self.service.delivery.reserve(self.h.guild, channel, key, body, view=view)
        candidate = self.h.message(channel, 1001, body, view=view)
        expected_components = candidate.components
        changes = (
            {'author': self.h.members[1]},
            {'nonce': 'nonce-from-another-send'},
            {'components': []},
            {'attachments': [SimpleNamespace(filename='unexpected.txt', size=7)]},
            {'content': body + ' ручная правка'},
        )
        for change in changes:
            candidate.author, candidate.nonce = self.h.bot.user, None
            candidate.content, candidate.components, candidate.attachments = body, expected_components, []
            for field, value in change.items():
                setattr(candidate, field, value)
            with self.subTest(change=tuple(change)), self.assertRaisesRegex(ClubError, 'не подтверждена'):
                await self.service.upsert(self.h.guild, key, channel.id, body, view=view)
        channel.send.assert_not_awaited()
        candidate.edit.assert_not_awaited()
        self.assertIsNone(self.store.publication(key)['message_id'])

    async def test_another_publications_message_is_not_claimed_as_recovery(self):
        channel, key, body = self.h.channels[11], 'catalog:1:page:5', 'Одинаковая страница'
        await self.service.delivery.reserve(self.h.guild, channel, key, body)
        message = self.h.message(channel, 1001, body)
        other_key = 'other-book:page:1'
        self.store.reserve_publication(other_key, 1, channel.id)
        self.store.save_publication(other_key, channel.id, message.id)
        with self.assertRaisesRegex(ClubError, 'не подтверждена'):
            await self.service.upsert(self.h.guild, key, channel.id, body)
        self.assertEqual(self.store.publication(other_key)['message_id'], message.id)
        self.assertIsNone(self.store.publication(key)['message_id'])
        channel.send.assert_not_awaited()
        message.edit.assert_not_awaited()

    async def test_historical_catalog_marker_is_cleaned_in_place_with_controls_and_files(self):
        channel, key, body = self.h.channels[11], 'catalog:1:page:1', 'Каталог'
        view = self.view()
        message = self.h.message(channel, 1001, body + '\n-# bc:' + key, view=view)
        attachment = SimpleNamespace(filename='existing.txt', size=7)
        message.attachments = [attachment]
        self.store.reserve_publication(key, 1, channel.id)
        self.store.save_publication(key, channel.id, message.id, 'old-render-hash')
        for _ in range(2):
            result = await self.service.upsert(self.h.guild, key, channel.id, body, view=view)
            self.assertEqual(result['message_id'], message.id)
        self.assertEqual(message.content, body)
        self.assertEqual([component.to_dict() for component in message.components], view.to_components())
        self.assertEqual(message.attachments, [attachment])
        channel.send.assert_not_awaited()
        message.edit.assert_awaited_once()
        self.assertNotIn('attachments', message.edit.await_args.kwargs)

    async def test_legacy_unacknowledged_marker_still_recovers_without_new_send(self):
        channel, key, body = self.h.channels[11], 'catalog:1:page:6', 'Старая отправка'
        self.store.reserve_publication(key, 1, channel.id)
        message = self.h.message(channel, 1001, body + '\n-# bc:' + key)
        self.assertIsNone(self.service.delivery.intent(key))
        result = await self.service.upsert(self.h.guild, key, channel.id, body)
        self.assertEqual(result['message_id'], message.id)
        self.assertEqual(message.content, body)
        channel.send.assert_not_awaited()

    async def test_legacy_duplicate_markers_do_not_bind_or_edit_arbitrary_copy(self):
        channel, key, body = self.h.channels[11], 'catalog:1:page:6', 'Старая отправка'
        self.store.reserve_publication(key, 1, channel.id)
        copies = [self.h.message(channel, ident, body + '\n-# bc:' + key) for ident in (1001, 1002)]
        with self.assertRaisesRegex(ClubError, 'несколько прежних'):
            await self.service.upsert(self.h.guild, key, channel.id, body)
        self.assertIsNone(self.store.publication(key)['message_id'])
        channel.send.assert_not_awaited()
        for message in copies:
            message.edit.assert_not_awaited()

    async def test_legacy_duplicate_forum_markers_include_archived_copy(self):
        forum, key, body = self.h.channels[13], 'book:' + self.book['id'], 'Карточка книги'
        self.store.reserve_publication(key, 1, forum.id)
        copies = []
        for ident in (1001, 1002):
            thread = self.h.channel(ident, parent_id=forum.id)
            thread.archived = ident == 1002
            copies.append(self.h.message(thread, ident, body + '\n-# bc:' + key))
        with self.assertRaisesRegex(ClubError, 'несколько прежних'):
            await self.service.upsert(self.h.guild, key, forum.id, body, forum_name='Книга')
        self.assertIsNone(self.store.publication(key)['message_id'])
        forum.create_thread.assert_not_awaited()
        for message in copies:
            message.edit.assert_not_awaited()

    async def test_historical_webhook_body_marker_keeps_id_and_attachment_after_cleanup(self):
        hook, thread = self.hook(), self.h.channel(80, parent_id=14)
        key, body = 'essay-import:1:61:message:61:0:v2', 'Текст автора'
        self.store.reserve_publication(key, 1, thread.id, webhook_id=hook.id)
        old = await hook.send(body + '\n-# bc:' + key, thread=discord.Object(thread.id),
                              username=self.h.members[1].display_name, avatar_url=self.h.members[1].display_avatar.url,
                              wait=True, allowed_mentions=discord.AllowedMentions.none())
        self.store.save_publication(key, thread.id, old.id)
        actual = thread.messages[old.id]
        attachment = SimpleNamespace(filename='existing.txt', size=7)
        actual.attachments = [attachment]
        hook.send.reset_mock()
        for _ in range(2):
            result = await self.publisher.upsert(self.h.guild, thread, key, body, self.h.members[1],
                                                  expected_attachments=[('existing.txt', 7)])
            self.assertEqual(result['message_id'], actual.id)
        self.assertEqual(actual.content, body)
        self.assertEqual(actual.attachments, [attachment])
        hook.send.assert_not_awaited()
        hook.edit_message.assert_awaited_once()
        self.assertNotIn('attachments', hook.edit_message.await_args.kwargs)

"""Essay buttons and lifecycle with real discord.py views and a mocked transport."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bookclub.store import ClubError, Store
from bookclub.ui import BookView, Club, MeetingView
from test_bookclub import ClubFixture
from test_bookclub_discord import DiscordHarness


class EssayDiscordTests(ClubFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.store.configure(1, dict(self.store.settings(1), essay_webhooks=False))
        self.h = DiscordHarness()
        self.h.bot.user.bot = True
        for member in self.h.members.values():
            member.display_name = f"Участник {member.id}"
        self.h.members[1].display_name = "Анна Петрова"
        self.cog = Club(self.h.bot, self.store)
        self.service = self.cog.service
        self.store.set_published(1)

    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 1.0

    async def create(self, actor_id=1, book_id=None):
        return await self.service.create_essay_space(self.h.guild, book_id or self.book['id'], actor_id)

    def row(self, thread_id):
        return self.store.one('SELECT * FROM bc_essays WHERE guild_id=1 AND source_id=?', (thread_id,))

    def body(self, thread_id, text="Моё эссе", author_id=1, attachments=None):
        self.h.seq += 1
        message = self.h.message(self.h.channels[thread_id], self.h.seq, text)
        message.guild = self.h.guild
        message.author = self.h.members[author_id]
        message.author.bot = False
        message.attachments = attachments or []
        return message

    def button(self, view, action):
        return next(child for child in view.children if child.custom_id.endswith(':' + action))

    def context(self, author_id=1):
        return SimpleNamespace(guild=self.h.guild, author=self.h.members[author_id],
                               interaction=self.h.interaction(author_id))

    async def test_create_uses_essay_forum_and_human_name_but_header_is_only_a_draft(self):
        await self.service.refresh(self.h.guild)
        essay = await self.create()
        thread = self.h.channels[essay['source_id']]
        starter = thread.messages[thread.id]
        self.assertEqual(thread.parent_id, 14)
        self.assertEqual(thread.owner_id, self.h.bot.user.id)
        self.assertIn('Книга', thread.name)
        self.assertIn('Анна Петрова', thread.name)
        self.assertLessEqual(len(thread.name), 100)
        self.assertEqual((essay['author_id'], essay['managed'], essay['submitted']), (1, 1, 0))
        self.assertIn('Автор: <@1>', starter.content)
        self.assertIn('Карточка книги и другие эссе', starter.content)
        self.assertIn(f'-# bc:essay-space:{self.book["id"]}:1', starter.content)
        self.assertEqual(self.store.essays(self.book['id']), [])
        self.assertIn(1, {p['user_id'] for p in self.store.missing_essays(self.book['id'])})
        self.assertFalse(self.h.channels[14].create_thread.await_args.kwargs['allowed_mentions'].everyone)
        for member in self.h.members.values():
            member.send.assert_not_awaited()

    async def test_repeated_concurrent_buttons_and_restart_reuse_one_post(self):
        forum = self.h.channels[14]
        original = forum.create_thread.side_effect

        async def yielding_create(**kwargs):
            await asyncio.sleep(0)
            return await original(**kwargs)

        forum.create_thread.side_effect = yielding_create
        first, second = self.h.interaction(1), self.h.interaction(1)
        view = BookView(self.cog, self.book)
        await asyncio.gather(self.button(view, 'write').callback(first),
                             self.button(view, 'write').callback(second))
        forum.create_thread.assert_awaited_once()
        row = self.store.essays(self.book['id'], submitted_only=False)[0]
        for interaction in (first, second):
            interaction.response.defer.assert_awaited_once_with(ephemeral=True)
            reply = interaction.followup.send.await_args
            self.assertTrue(reply.kwargs['ephemeral'])
            self.assertEqual(reply.kwargs['view'].children[0].url, row['url'])

        restarted = Club(self.h.bot, Store(self.path, clock=lambda: self.now))
        result = await restarted.service.create_essay_space(self.h.guild, self.book['id'], 1)
        self.assertEqual(result['source_id'], row['source_id'])
        forum.create_thread.assert_awaited_once()
        thread = self.h.channels[row['source_id']]
        thread.archived = True
        await self.create()
        self.assertFalse(thread.archived)
        forum.create_thread.assert_awaited_once()

    async def test_author_text_attachment_and_link_each_publish(self):
        for author_id, content, attachments in ((1, 'Самостоятельный текст', []),
                                                (2, '', [SimpleNamespace(filename='essay.pdf')]),
                                                (3, 'https://example.org/my-essay', [])):
            with self.subTest(author_id=author_id):
                essay = await self.create(author_id)
                message = self.body(essay['source_id'], content, author_id, attachments)
                await self.cog.on_message(message)
                self.assertEqual(self.row(essay['source_id'])['submitted'], 1)
                self.assertNotIn(author_id, {p['user_id'] for p in self.store.missing_essays(self.book['id'])})
                publication = self.store.publication(f'book:{self.book["id"]}')
                book_thread = self.h.channels[publication['channel_id']]
                public_text = '\n'.join(m.content for m in book_thread.messages.values())
                self.assertIn(essay['url'], public_text)
                self.assertIn(f'<@{author_id}>', public_text)

    async def test_foreign_comments_empty_author_messages_and_bot_header_do_not_publish(self):
        essay = await self.create()
        thread = self.h.channels[essay['source_id']]
        foreign = self.body(thread.id, 'Комментарий другого читателя', author_id=2)
        await self.cog.on_message(foreign)
        empty = self.body(thread.id, '  \n\t ')
        await self.cog.on_message(empty)
        starter = thread.messages[thread.id]
        starter.guild, starter.attachments = self.h.guild, []
        await self.cog.on_message(starter)
        self.assertEqual(self.row(thread.id)['submitted'], 0)
        self.assertEqual(self.store.essays(self.book['id']), [])

    async def test_rename_retains_book_and_human_author(self):
        essay = await self.create()
        thread = self.h.channels[essay['source_id']]
        self.store.create_book(1, 'Другая книга', 'Другой автор', '', 'second')
        thread.name = 'Другая книга · Имя другого человека'
        await self.cog.on_raw_thread_update(SimpleNamespace(guild_id=1, thread_id=thread.id))
        result = self.row(thread.id)
        self.assertEqual(result['book_id'], self.book['id'])
        self.assertEqual(result['author_id'], 1)
        self.assertEqual(result['title'], thread.name)

    async def test_delete_last_author_body_returns_draft_and_thread_delete_allows_recreation(self):
        essay = await self.create()
        thread_id = essay['source_id']
        first, second = self.body(thread_id), self.body(thread_id, 'Продолжение')
        await self.cog.on_message(first)
        del self.h.channels[thread_id].messages[first.id]
        await self.cog.on_raw_message_delete(SimpleNamespace(guild_id=1, channel_id=thread_id, message_id=first.id))
        self.assertEqual(self.row(thread_id)['submitted'], 1)
        del self.h.channels[thread_id].messages[second.id]
        await self.cog.on_raw_message_delete(SimpleNamespace(guild_id=1, channel_id=thread_id, message_id=second.id))
        self.assertEqual((self.row(thread_id)['submitted'], self.row(thread_id)['deleted']), (0, 0))
        self.assertEqual(self.store.essays(self.book['id']), [])

        del self.h.channels[thread_id]
        await self.cog.on_raw_thread_delete(SimpleNamespace(guild_id=1, thread_id=thread_id))
        self.assertEqual(self.row(thread_id)['deleted'], 1)
        replacement = await self.create()
        self.assertNotEqual(replacement['source_id'], thread_id)
        self.assertEqual(self.h.channels[14].create_thread.await_count, 2)

    async def test_editing_author_body_to_empty_and_back_updates_submission(self):
        essay = await self.create()
        message = self.body(essay['source_id'])
        await self.cog.on_message(message)
        payload = SimpleNamespace(guild_id=1, channel_id=essay['source_id'], message_id=message.id)
        message.content = ''
        await self.cog.on_raw_message_edit(payload)
        self.assertEqual(self.row(essay['source_id'])['submitted'], 0)
        message.content = 'Исправленный текст'
        await self.cog.on_raw_message_edit(payload)
        self.assertEqual(self.row(essay['source_id'])['submitted'], 1)

    async def test_managed_correction_keeps_author_and_rebinds_creation_to_correct_book(self):
        essay = await self.create()
        second = self.store.create_book(1, 'Другая книга', 'Автор', '', 'second')
        with self.assertRaises(ClubError):
            await Club.essay.callback(self.cog, self.context(2), second['id'], essay['url'], True)
        with self.assertRaises(ClubError):
            await Club.essay.callback(self.cog, self.context(1), second['id'], essay['url'], False)
        self.assertEqual(self.row(essay['source_id'])['book_id'], self.book['id'])
        await Club.essay.callback(self.cog, self.context(1), second['id'], essay['url'], True)
        moved = self.row(essay['source_id'])
        self.assertEqual((moved['book_id'], moved['author_id'], moved['submitted']), (second['id'], 1, 0))
        same = await self.create(book_id=second['id'])
        self.assertEqual(same['source_id'], essay['source_id'])
        original_book_essay = await self.create()
        self.assertEqual(original_book_essay['book_id'], self.book['id'])
        self.assertNotEqual(original_book_essay['source_id'], essay['source_id'])

    async def test_unacknowledged_creation_recovers_marker_after_restart(self):
        forum = self.h.channels[14]
        original = forum.create_thread.side_effect

        async def create_then_lose_ack(**kwargs):
            await original(**kwargs)
            raise OSError('connection lost after Discord created thread')

        forum.create_thread.side_effect = create_then_lose_ack
        with self.assertRaises(OSError):
            await self.create()
        self.assertEqual(self.store.essays(self.book['id'], submitted_only=False), [])
        forum.create_thread.side_effect = original
        restarted = Club(self.h.bot, Store(self.path, clock=lambda: self.now))
        recovered = await restarted.service.create_essay_space(self.h.guild, self.book['id'], 1)
        forum.create_thread.assert_awaited_once()
        self.assertEqual((recovered['author_id'], recovered['managed'], recovered['submitted']), (1, 1, 0))
        self.assertEqual(self.store.publication(f'essay-space:{self.book["id"]}:1')['state'], 'ready')

    async def test_scan_recovers_unacknowledged_creation_and_author_body_without_click(self):
        forum = self.h.channels[14]
        original = forum.create_thread.side_effect

        async def create_then_lose_ack(**kwargs):
            created = await original(**kwargs)
            self.body(created.thread.id, 'Текст, написанный до перезапуска')
            raise OSError('connection lost after Discord created thread')

        forum.create_thread.side_effect = create_then_lose_ack
        with self.assertRaises(OSError):
            await self.create()
        restarted = Club(self.h.bot, Store(self.path, clock=lambda: self.now))
        await restarted.service.scan_essays(self.h.guild)
        await restarted.service.scan_essays(self.h.guild)
        recovered = self.store.essays(self.book['id'])
        self.assertEqual(len(recovered), 1)
        self.assertEqual((recovered[0]['author_id'], recovered[0]['managed']), (1, 1))
        forum.create_thread.assert_awaited_once()
        self.assertEqual(self.store.publication(f'essay-space:{self.book["id"]}:1')['state'], 'ready')

    async def test_missing_thread_without_gateway_event_is_recreated_after_restart(self):
        essay = await self.create()
        del self.h.channels[essay['source_id']]
        restarted = Club(self.h.bot, Store(self.path, clock=lambda: self.now))
        replacement = await restarted.service.create_essay_space(self.h.guild, self.book['id'], 1)
        self.assertNotEqual(replacement['source_id'], essay['source_id'])
        self.assertEqual(self.row(essay['source_id'])['deleted'], 1)
        self.assertEqual(len(self.store.essays(self.book['id'], submitted_only=False)), 1)
        self.assertEqual(self.h.channels[14].create_thread.await_count, 2)

    async def test_correcting_to_book_with_own_essay_preserves_both_works_and_reuses_one(self):
        first = await self.create()
        second_book = self.store.create_book(1, 'Другая книга', 'Автор', '', 'second')
        second = await self.create(book_id=second_book['id'])
        await self.cog.on_message(self.body(first['source_id'], 'Первое эссе'))
        await self.cog.on_message(self.body(second['source_id'], 'Второе эссе'))
        await Club.essay.callback(self.cog, self.context(99), second_book['id'], first['url'], True)
        works = self.store.essays(second_book['id'])
        self.assertEqual({work['source_id'] for work in works}, {first['source_id'], second['source_id']})
        self.assertTrue(all(work['author_id'] == 1 for work in works))
        self.assertEqual(self.store.essays(self.book['id']), [])
        restarted = Club(self.h.bot, Store(self.path, clock=lambda: self.now))
        await restarted.service.scan_essays(self.h.guild)
        chosen = await restarted.service.create_essay_space(self.h.guild, second_book['id'], 1)
        self.assertIn(chosen['source_id'], {first['source_id'], second['source_id']})
        self.assertEqual(chosen['book_id'], second_book['id'])
        self.assertEqual(self.h.channels[14].create_thread.await_count, 2)
        self.assertEqual(len(self.store.essays(second_book['id'])), 2)
        starter = self.h.channels[first['source_id']].messages[first['source_id']]
        self.assertIn('«Другая книга»', starter.content)
        self.assertIn(f'-# bc:essay-space:{second_book["id"]}:1:{first["source_id"]}', starter.content)

    async def test_long_book_and_display_name_fit_discord_forum_title(self):
        title = 'Очень длинное название книги ' * 6
        self.store.update_book(1, self.book['id'], title=title)
        self.h.members[1].display_name = '  Имя\n  читателя ' * 8
        essay = await self.create()
        thread = self.h.channels[essay['source_id']]
        author = ' '.join(self.h.members[1].display_name.split())[:40]
        self.assertLessEqual(len(thread.name), 100)
        self.assertTrue(thread.name.endswith(author))
        self.assertIn('Эссе', thread.name)
        self.assertNotIn('\n', thread.name)
        self.assertEqual(essay['author_id'], 1)

    async def test_missing_first_corrected_post_reuses_another_existing_corrected_post(self):
        first = await self.create()
        second_book = self.store.create_book(1, 'Вторая книга', 'Автор', '', 'second')
        destination = self.store.create_book(1, 'Нужная книга', 'Автор', '', 'destination')
        second = await self.create(book_id=second_book['id'])
        for essay in (first, second):
            await Club.essay.callback(self.cog, self.context(99), destination['id'], essay['url'], True)
        del self.h.channels[first['source_id']]
        chosen = await self.create(book_id=destination['id'])
        self.assertEqual(chosen['source_id'], second['source_id'])
        self.assertEqual(self.h.channels[14].create_thread.await_count, 2)
        self.assertEqual(self.row(first['source_id'])['deleted'], 1)

    async def test_arbitrary_bot_threads_and_unreserved_markers_are_not_human_essays(self):
        for ident, content in ((700, 'Обычная карточка бота'),
                               (701, f'-# bc:essay-space:{self.book["id"]}:1')):
            thread = self.h.channel(ident, parent_id=14, name='Книга · Анна Петрова')
            starter = self.h.message(thread, ident, content)
            starter.guild, starter.attachments = self.h.guild, []
            self.assertFalse(await self.service.register_thread(thread))
        await self.service.scan_essays(self.h.guild)
        self.assertEqual(self.store.essays(self.book['id'], submitted_only=False), [])

    async def test_disabled_unpublished_and_inaccessible_guild_reject_creation(self):
        disabled = Club(self.h.bot, self.store, guild_ids=set())
        with self.assertRaises(ClubError):
            await disabled.service.create_essay_space(self.h.guild, self.book['id'], 1)
        with self.store.tx() as db:
            db.execute("UPDATE bc_settings SET data=json_set(data,'$.published',0) WHERE guild_id=1")
        with self.assertRaises(ClubError):
            await self.create()
        self.store.set_published(1)
        for channel_id, permission in ((13, 'view_channel'), (14, 'view_channel'), (14, 'send_messages_in_threads')):
            channel = self.h.channels[channel_id]
            permissions = channel.permissions_for.return_value
            with self.subTest(channel=channel_id, permission=permission):
                setattr(permissions, permission, False)
                try:
                    with self.assertRaises(ClubError):
                        await self.create()
                finally:
                    setattr(permissions, permission, True)
        self.h.channels[14].create_thread.assert_not_awaited()
        self.assertEqual(self.store.essays(self.book['id'], submitted_only=False), [])

    async def test_meeting_buttons_write_and_list_all_submitted_essays(self):
        view = MeetingView(self.cog, self.meeting)
        await self.button(view, 'write').callback(self.h.interaction(1))
        draft = self.store.essays(self.book['id'], submitted_only=False)[0]
        self.assertEqual(draft['author_id'], 1)
        links = []
        for source_id in range(8000, 8025):
            link = f'https://discord.com/channels/1/{source_id}'
            links.append(link)
            self.store.register_essay(1, self.book['id'], source_id, source_id, 2, 'Эссе ' + 'А' * 160, link)
        interaction = self.h.interaction(1)
        await self.button(view, 'list').callback(interaction)
        replies = interaction.followup.send.await_args_list
        contents = '\n'.join(call.args[0] for call in replies)
        self.assertTrue(all(link in contents for link in links))
        self.assertNotIn(draft['url'], contents)
        self.assertGreater(len(replies), 1)
        self.assertTrue(all(len(call.args[0]) <= 2000 and call.kwargs['ephemeral'] for call in replies))

    async def test_persistent_book_and_meeting_buttons_restore_on_load(self):
        book_view, meeting_view = BookView(self.cog, self.book), MeetingView(self.cog, self.meeting)
        self.assertTrue(book_view.is_persistent())
        self.assertTrue(meeting_view.is_persistent())
        expected_ids = {child.custom_id for view in (book_view, meeting_view) for child in view.children}
        expected_ids.update({'bc:catalog:1:add', 'bc:catalog:1:import'})
        self.assertTrue(all(len(custom_id) <= 100 for custom_id in expected_ids))
        restarted = Club(self.h.bot, Store(self.path, clock=lambda: self.now))
        with patch.object(restarted.worker, 'start'):
            await restarted.cog_load()
        restored_views = [call.args[0] for call in self.h.bot.add_view.call_args_list]
        self.assertEqual(len(restored_views), 3)
        self.assertEqual({child.custom_id for view in restored_views for child in view.children}, expected_ids)
        self.assertTrue(all(view.is_persistent() for view in restored_views))

    async def test_buttons_recheck_guild_and_live_member_access(self):
        book_view = BookView(self.cog, self.book)
        interaction = self.h.interaction(1)
        interaction.guild_id = 2
        with self.assertRaises(ClubError):
            await self.button(book_view, 'write').callback(interaction)
        interaction = self.h.interaction(1)
        del self.h.members[1]
        with self.assertRaises(ClubError):
            await self.button(book_view, 'list').callback(interaction)
        self.h.channels[14].create_thread.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()

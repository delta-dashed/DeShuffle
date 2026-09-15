"""Reversible removal controls use real storage and mocked Discord interactions."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import discord

from bookclub.book_trash_ui import (DeletedBooksView, RemoveBookConfirmation, RemovedBookView,
                                    RestoreBookConfirmation, open_delete_book, open_deleted_books,
                                    open_restore_book)
from bookclub.store import ClubError
from bookclub.ui import Club
from test_bookclub import ClubFixture, CONFIG
from test_bookclub_discord import DiscordHarness


class BookTrashUITests(ClubFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.h = DiscordHarness()
        self.cog = Club(self.h.bot, self.store)
        self.service = self.cog.service
        self.service.request_book_refresh = Mock()
        self.service.refresh = AsyncMock()

    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 1.0

    def interaction(self, user=99):
        interaction = self.h.interaction(user)
        interaction.edit_original_response = AsyncMock()
        acknowledged = False
        async def defer(**kwargs):
            nonlocal acknowledged
            if acknowledged:
                raise discord.InteractionResponded(interaction)
            acknowledged = True
        interaction.response.defer = AsyncMock(side_effect=defer)
        interaction.response.is_done = lambda: acknowledged
        return interaction

    def current(self):
        return self.store.book(1, self.book['id'])

    def remove(self, book=None):
        book = book or self.current()
        self.store.remove_book(1, book['id'], expected_revision=book['revision'], actor_id=99)
        return self.store.book(1, book['id'])

    def revoke_rest(self):
        self.h.guild.owner_id = 90
        self.store.configure(1, {**CONFIG, 'organizers': [], 'organizer_roles': [22]})
        member = Mock(spec=discord.Member)
        member.id, member.bot, member.guild, member.roles = 99, False, self.h.guild, []
        self.h.guild.fetch_member.side_effect = None
        self.h.guild.fetch_member.return_value = member

    async def test_removal_opens_private_counted_confirmation_without_writing_or_rest(self):
        self.store.register_essay(1, self.book['id'], 501, 501, 1, 'Работа', 'https://example.org/1')
        self.store.register_essay(1, self.book['id'], 502, 502, 2, 'Черновик', 'https://example.org/2', submitted=False)
        before = self.current()
        interaction = self.interaction()
        async with self.service.locks[1]:
            view = await asyncio.wait_for(open_delete_book(self.cog, interaction, self.book['id']), .5)
        self.assertIsInstance(view, RemoveBookConfirmation)
        sent = interaction.followup.send.await_args
        self.assertTrue(sent.kwargs['ephemeral'])
        self.assertIn('Связано встреч: 1; работ и черновиков эссе: 2', sent.args[0])
        self.assertIn('События Discord не отменяются', sent.args[0])
        self.assertEqual(self.current(), before)
        self.h.guild.fetch_member.assert_not_awaited()
        self.h.bot.fetch_channel.assert_not_awaited()
        self.service.request_book_refresh.assert_not_called()

    async def test_removal_commits_before_publication_request_and_avoids_refresh_lock(self):
        view = RemoveBookConfirmation(self.cog, self.current(), 99)
        self.store.register_essay(1, self.book['id'], 501, 501, 1, 'Работа', 'https://example.org/1')
        before_essays = self.store.essays(self.book['id'])
        event_id = self.meeting['event_id']
        queued = []
        def request(guild, book_id):
            queued.append((guild.id, book_id, self.store.book(1, book_id)['deleted']))
        self.service.request_book_refresh.side_effect = request
        interaction = self.interaction()
        async with self.service.locks[1]:
            await asyncio.wait_for(view.confirm.callback(interaction), .5)
        self.assertEqual(queued, [(1, self.book['id'], 1)])
        self.assertEqual(self.store.books(1), [])
        self.assertEqual(self.store.essays(self.book['id']), before_essays)
        self.assertEqual(self.store.meeting(1, self.meeting['id'])['event_id'], event_id)
        self.service.refresh.assert_not_awaited()
        self.h.guild.fetch_member.assert_awaited_once_with(99)
        self.h.bot.fetch_channel.assert_awaited_once_with(13)
        self.h.channels[13].create_thread.assert_not_awaited()
        self.assertIsNone(interaction.edit_original_response.await_args.kwargs['view'])
        self.assertTrue(view.is_finished())

    async def test_repeat_removal_cannot_commit_twice(self):
        view = RemoveBookConfirmation(self.cog, self.current(), 99)
        await view.confirm.callback(self.interaction())
        before = self.current()
        with self.assertRaises(ClubError):
            await view.confirm.callback(self.interaction())
        self.assertEqual(self.current(), before)
        self.service.request_book_refresh.assert_called_once()

    async def test_stale_delete_confirmation_preserves_newer_title(self):
        view = RemoveBookConfirmation(self.cog, self.current(), 99)
        self.store.update_book(1, self.book['id'], title='Обновлено')
        with self.assertRaisesRegex(ClubError, 'изменена'):
            await view.confirm.callback(self.interaction())
        self.assertFalse(self.current()['deleted'])
        self.assertEqual(self.current()['title'], 'Обновлено')
        self.service.request_book_refresh.assert_not_called()

    async def test_revoked_actual_member_cannot_delete_or_restore(self):
        delete = RemoveBookConfirmation(self.cog, self.current(), 99)
        interaction = self.interaction()
        self.revoke_rest()
        self.assertTrue(self.service.interaction_organizer(interaction))
        with self.assertRaisesRegex(ClubError, 'организатор'):
            await delete.confirm.callback(interaction)
        self.assertFalse(self.current()['deleted'])
        removed = self.remove()
        with self.assertRaisesRegex(ClubError, 'организатор'):
            await RestoreBookConfirmation(self.cog, removed, 99).confirm.callback(self.interaction())
        self.assertTrue(self.current()['deleted'])
        self.service.request_book_refresh.assert_not_called()

    async def test_fresh_forum_access_required_before_removal(self):
        view = RemoveBookConfirmation(self.cog, self.current(), 99)
        self.h.channels[13].permissions_for.return_value.read_message_history = False
        with self.assertRaisesRegex(ClubError, 'доступа'):
            await view.confirm.callback(self.interaction())
        self.assertFalse(self.current()['deleted'])
        self.h.bot.fetch_channel.assert_awaited_once_with(13)

    async def test_confirmation_actor_and_guild_bound_before_response(self):
        removed = self.remove()
        views = (RemoveBookConfirmation(self.cog, self.book, 99),
                 RestoreBookConfirmation(self.cog, removed, 99))
        for view in views:
            for callback in (view.confirm.callback, view.cancel.callback):
                for wrong in ('actor', 'guild', 'guild_object', 'dm'):
                    with self.subTest(view=type(view).__name__, callback=callback, wrong=wrong):
                        interaction = self.interaction(1 if wrong == 'actor' else 99)
                        if wrong == 'guild':
                            interaction.guild_id = 2
                        elif wrong == 'guild_object':
                            interaction.guild = SimpleNamespace(id=2)
                        elif wrong == 'dm':
                            interaction.guild = None
                        with self.assertRaises(ClubError):
                            await callback(interaction)
                        interaction.response.defer.assert_not_awaited()

    async def test_cancel_removal_or_restore_does_not_write(self):
        for view in (RemoveBookConfirmation(self.cog, self.current(), 99),
                     RestoreBookConfirmation(self.cog, self.current(), 99)):
            before = self.current()
            interaction = self.interaction()
            await view.cancel.callback(interaction)
            self.assertEqual(self.current(), before)
            self.assertTrue(view.is_finished())
            self.assertIsNone(interaction.edit_original_response.await_args.kwargs['view'])
        self.service.request_book_refresh.assert_not_called()

    async def test_deleted_list_is_private_and_ignores_active_and_other_guild_books(self):
        removed = self.remove()
        self.store.create_book(1, 'Активная', 'Автор', '', 'active')
        self.store.configure(2, CONFIG)
        foreign = self.store.create_book(2, 'Чужая', 'Автор', '', 'foreign')
        self.store.remove_book(2, foreign['id'], expected_revision=foreign['revision'], actor_id=99)
        interaction = self.interaction()
        async with self.service.locks[1]:
            view = await asyncio.wait_for(open_deleted_books(self.cog, interaction), .5)
        self.assertEqual(list(view.visible), [removed['id']])
        self.assertTrue(interaction.followup.send.await_args.kwargs['ephemeral'])
        self.h.guild.fetch_member.assert_not_awaited()
        self.h.bot.fetch_channel.assert_not_awaited()

    async def test_empty_deleted_list_shows_no_picker(self):
        interaction = self.interaction()
        self.assertIsNone(await open_deleted_books(self.cog, interaction))
        self.assertEqual(interaction.followup.send.await_args.args[0], 'Удалённых книг нет.')
        self.assertNotIn('view', interaction.followup.send.await_args.kwargs)

    async def test_deleted_slash_command_uses_already_deferred_response_from_cog_hook(self):
        removed = self.remove()
        interaction = self.interaction()
        # Exercise discord.py's real is_done/InteractionResponded behavior,
        # without using its network adapter for the initial context defer.
        interaction.response = discord.InteractionResponse(interaction)
        async def context_defer(**kwargs):
            self.assertEqual(kwargs, {'ephemeral': True})
            self.assertFalse(interaction.response.is_done())
            interaction.response._response_type = discord.InteractionResponseType.deferred_channel_message
        ctx = SimpleNamespace(interaction=interaction, guild=self.h.guild,
                              author=interaction.user, command=self.cog.deleted_books,
                              defer=AsyncMock(side_effect=context_defer))
        await self.cog.cog_before_invoke(ctx)
        await self.cog.deleted_books.callback(self.cog, ctx)
        ctx.defer.assert_awaited_once_with(ephemeral=True)
        sent = interaction.followup.send.await_args
        self.assertTrue(sent.kwargs['ephemeral'])
        self.assertEqual(list(sent.kwargs['view'].visible), [removed['id']])
        # A regression to an unconditional defer in open_deleted_books would
        # throw this actual SDK error before sending the private book list.
        with self.assertRaises(discord.InteractionResponded):
            await interaction.response.defer(ephemeral=True)

    async def test_remove_and_restore_entrypoints_accept_previously_deferred_interaction(self):
        interaction = self.interaction()
        await interaction.response.defer(ephemeral=True)
        view = await open_delete_book(self.cog, interaction, self.book['id'])
        self.assertIsInstance(view, RemoveBookConfirmation)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        self.assertTrue(interaction.followup.send.await_args.kwargs['ephemeral'])
        self.remove()
        interaction = self.interaction()
        await interaction.response.defer(ephemeral=True)
        view = await open_restore_book(self.cog, interaction, self.book['id'])
        self.assertIsInstance(view, RestoreBookConfirmation)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        self.assertTrue(interaction.followup.send.await_args.kwargs['ephemeral'])

    async def test_listing_paginates_all_books_in_selects_of_at_most_25(self):
        self.remove()
        for index in range(26):
            book = self.store.create_book(1, f'Удалённая {index}', 'Автор', '', f'deleted:{index}')
            self.remove(book)
        view = await open_deleted_books(self.cog, self.interaction())
        first_ids = set(view.visible)
        self.assertEqual(len(first_ids), 25)
        self.assertTrue(view.previous.disabled)
        self.assertFalse(view.next_page.disabled)
        await view.next_page.callback(self.interaction())
        self.assertEqual(len(view.visible), 2)
        self.assertFalse(first_ids & set(view.visible))
        self.assertEqual(len(view.select.options), 2)
        self.assertTrue(view.next_page.disabled)
        await view.previous.callback(self.interaction())
        self.assertEqual(set(view.visible), first_ids)

    async def test_select_confirms_only_visible_snapshot_and_does_not_restore_yet(self):
        removed = self.remove()
        view = await open_deleted_books(self.cog, self.interaction())
        view.select._values = [removed['id']]
        interaction = self.interaction()
        await view.choose(interaction)
        confirmation = interaction.edit_original_response.await_args.kwargs['view']
        self.assertIsInstance(confirmation, RestoreBookConfirmation)
        self.assertEqual(confirmation.book['revision'], removed['revision'])
        self.assertTrue(self.current()['deleted'])
        self.assertTrue(view.is_finished())
        self.service.request_book_refresh.assert_not_called()

    async def test_select_rejects_forged_selection_or_stale_deleted_book(self):
        removed = self.remove()
        view = await open_deleted_books(self.cog, self.interaction())
        for values in ([], ['not-on-page'], [removed['id'], 'other']):
            view.select._values = values
            with self.assertRaisesRegex(ClubError, 'этой странице'):
                await view.choose(self.interaction())
        self.store.restore_book(1, removed['id'], expected_revision=removed['revision'], actor_id=99)
        view.select._values = [removed['id']]
        with self.assertRaisesRegex(ClubError, 'изменена'):
            await view.choose(self.interaction())
        self.service.request_book_refresh.assert_not_called()

    async def test_pagination_reloads_after_last_deleted_book_is_restored(self):
        removed = self.remove()
        view = await open_deleted_books(self.cog, self.interaction())
        self.store.restore_book(1, removed['id'], expected_revision=removed['revision'], actor_id=99)
        await view.next_page.callback(self.interaction())
        self.assertTrue(view.select.disabled)
        self.assertTrue(view.previous.disabled)
        self.assertTrue(view.next_page.disabled)
        self.assertEqual(view.visible, {})
        self.assertIn('Удалённые книги: 0', view.content())

    async def test_list_callbacks_are_bound_to_owner_and_guild(self):
        removed = self.remove()
        view = DeletedBooksView(self.cog, 1, 99, [removed])
        view.select._values = [removed['id']]
        for callback in (view.choose, view.previous.callback, view.next_page.callback):
            for wrong in ('actor', 'guild'):
                interaction = self.interaction(1 if wrong == 'actor' else 99)
                if wrong == 'guild':
                    interaction.guild_id = 2
                with self.assertRaises(ClubError):
                    await callback(interaction)
                interaction.response.defer.assert_not_awaited()

    async def test_restore_commits_without_refresh_lock_and_links_existing_publication(self):
        self.store.reserve_publication(f'book:{self.book["id"]}', 1, 700)
        self.store.save_publication(f'book:{self.book["id"]}', 700, 701, 'digest')
        removed = self.remove()
        view = RestoreBookConfirmation(self.cog, removed, 99)
        interaction = self.interaction()
        async with self.service.locks[1]:
            await asyncio.wait_for(view.confirm.callback(interaction), .5)
        current = self.current()
        self.assertFalse(current['deleted'])
        self.assertEqual((current['status'], current['position']), (removed['status'], removed['position']))
        self.service.request_book_refresh.assert_called_once_with(self.h.guild, current['id'])
        self.service.refresh.assert_not_awaited()
        result_view = interaction.edit_original_response.await_args.kwargs['view']
        self.assertEqual(result_view.children[0].url, 'https://discord.com/channels/1/700/701')
        self.h.channels[13].create_thread.assert_not_awaited()
        self.assertTrue(view.is_finished())

    async def test_repeat_or_stale_restore_never_reverses_subsequent_removal(self):
        removed = self.remove()
        view = RestoreBookConfirmation(self.cog, removed, 99)
        await view.confirm.callback(self.interaction())
        self.remove()
        before = self.current()
        with self.assertRaisesRegex(ClubError, 'изменена'):
            await view.confirm.callback(self.interaction())
        self.assertEqual(self.current(), before)
        self.service.request_book_refresh.assert_called_once()

    async def test_open_restore_and_persistent_removed_view_do_not_restore_immediately(self):
        removed = self.remove()
        persistent = RemovedBookView(self.cog, removed)
        self.assertTrue(persistent.is_persistent())
        self.assertEqual(persistent.children[0].custom_id, f'bc:book:{removed["id"]}:restore')
        interaction = self.interaction()
        await persistent.restore(interaction)
        self.assertIsInstance(interaction.followup.send.await_args.kwargs['view'], RestoreBookConfirmation)
        self.assertTrue(self.current()['deleted'])
        self.service.request_book_refresh.assert_not_called()
        self.h.guild.fetch_member.assert_not_awaited()

    async def test_all_entrypoints_require_organizer_and_correct_book_state(self):
        with self.assertRaises(ClubError):
            await open_restore_book(self.cog, self.interaction(), self.book['id'])
        for callback in (lambda i: open_delete_book(self.cog, i, self.book['id']),
                         lambda i: open_restore_book(self.cog, i, self.book['id']),
                         lambda i: open_deleted_books(self.cog, i)):
            interaction = self.interaction(1)
            with self.assertRaisesRegex(ClubError, 'организатор'):
                await callback(interaction)
            interaction.followup.send.assert_not_awaited()
        self.remove()
        with self.assertRaises(ClubError):
            await open_delete_book(self.cog, self.interaction(), self.book['id'])

    async def test_deleted_list_and_entrypoints_fail_closed_when_guild_or_access_disabled(self):
        self.remove()
        self.h.channels[13].permissions_for.return_value.view_channel = False
        interaction = self.interaction()
        with self.assertRaisesRegex(ClubError, 'доступа'):
            await open_deleted_books(self.cog, interaction)
        interaction.followup.send.assert_not_awaited()
        self.service.guild_ids = {2}
        with self.assertRaisesRegex(ClubError, 'отключён'):
            await open_restore_book(self.cog, self.interaction(), self.book['id'])

    async def test_foreign_book_cannot_be_removed_or_restored(self):
        self.store.configure(2, CONFIG)
        foreign = self.store.create_book(2, 'Чужая книга', 'Автор', '', 'foreign')
        for action in (open_delete_book, open_restore_book):
            interaction = self.interaction()
            with self.assertRaises(ClubError):
                await action(self.cog, interaction, foreign['id'])
            interaction.followup.send.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()

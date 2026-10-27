"""Reversible removal controls use real storage and mocked Discord interactions."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import discord

from bookclub.book_trash_ui import (DeletedBooksView, RemoveBookConfirmation, RemovedBookView,
                                    RemovalModeView, RemovalOperationView, RemovalTitleModal, TransferTargetView,
                                    RestoreBookConfirmation, open_delete_book, open_deleted_books,
                                    open_restore_book, removal_content)
from bookclub.book_removal import (build_removal_plan, commit_removal_plan, fail_removal_resource,
                                   get_removal_operation, removal_resources, complete_removal_resource)
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
        self.assertIsInstance(view, RemovalModeView)
        sent = interaction.followup.send.await_args
        self.assertTrue(sent.kwargs['ephemeral'])
        self.assertIn('Связано встреч: 1; работ и черновиков эссе: 2', sent.args[0])
        self.assertIn('События Discord не отменяются', sent.args[0])
        self.assertEqual(self.current(), before)
        self.h.guild.fetch_member.assert_not_awaited()
        self.h.bot.fetch_channel.assert_not_awaited()
        self.service.request_book_refresh.assert_not_called()

    async def test_removal_commits_before_publication_request_and_avoids_refresh_lock(self):
        self.store.register_essay(1, self.book['id'], 501, 501, 1, 'Работа', 'https://example.org/1')
        view = RemoveBookConfirmation(self.cog, self.current(), 99)
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
        self.assertIsInstance(interaction.edit_original_response.await_args.kwargs['view'], RemovalOperationView)
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
        delete = RemoveBookConfirmation(self.cog, self.current(), 99)
        removed = self.remove()
        views = (delete,
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
        self.assertIsInstance(view, RemovalModeView)
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

    def target_book(self, key='target'):
        return self.store.create_book(1, 'Та же книга', 'Другой автор', '', key)

    def add_essay(self, book=None, source_id=501, author_id=1):
        book = book or self.current()
        self.store.register_essay(1, book['id'], source_id, source_id, author_id,
                                   'Работа', f'https://example.org/{source_id}')

    def confirmation(self, mode, target=None):
        book = self.current()
        plan = build_removal_plan(self.store, 1, book['id'], mode,
                                  target['id'] if target else None)
        return RemoveBookConfirmation(self.cog, book, 99, plan=plan)

    async def test_mode_picker_requires_choice_then_displays_frozen_preview_without_mutation(self):
        self.add_essay()
        view = await open_delete_book(self.cog, self.interaction(), self.book['id'])
        self.assertEqual({option.value for option in view.mode.options},
                         {'keep', 'topic', 'all', 'transfer', 'transfer_topic'})
        view.mode._values = ['all']
        interaction = self.interaction()
        await view.choose(interaction)
        confirmation = interaction.edit_original_response.await_args.kwargs['view']
        self.assertIsInstance(confirmation, RemoveBookConfirmation)
        self.assertEqual(confirmation.plan['mode'], 'all')
        text = interaction.edit_original_response.await_args.kwargs['content']
        self.assertIn('безвозвратному удалению: 1', text)
        self.assertIn('не вернёт удалённые сообщения и вложения', text)
        self.assertFalse(self.current()['deleted'])
        self.assertFalse(self.store.one('SELECT COUNT(*) AS n FROM bc_book_removal_operations')['n'])
        self.service.request_book_refresh.assert_not_called()

    async def test_target_picker_paginates_only_active_local_other_books_with_author(self):
        for number in range(26):
            self.store.create_book(1, 'Одинаковое название', f'Автор {number}', '', f'target:{number}')
        excluded = self.target_book('removed')
        self.remove(excluded)
        self.store.configure(2, CONFIG)
        foreign = self.store.create_book(2, 'Чужая', 'Автор', '', 'foreign')
        view = TransferTargetView(self.cog, self.current(), 99, 'transfer')
        first = set(view.visible)
        self.assertEqual(len(first), 25)
        self.assertTrue(all(option.description.startswith('Автор ') for option in view.select.options))
        self.assertFalse(first & {self.book['id'], excluded['id'], foreign['id']})
        await view.next_page.callback(self.interaction())
        self.assertEqual(len(view.visible), 1)
        self.assertFalse(first & set(view.visible))
        self.assertTrue(view.next_page.disabled)
        await view.previous.callback(self.interaction())
        self.assertEqual(set(view.visible), first)

    async def test_no_target_keeps_back_and_cancel_available(self):
        mode = RemovalModeView(self.cog, self.current(), 99)
        mode.mode._values = ['transfer']
        interaction = self.interaction()
        await mode.choose(interaction)
        view = interaction.edit_original_response.await_args.kwargs['view']
        self.assertTrue(view.select.disabled)
        self.assertIn('сначала добавьте другую книгу', view.content())
        back = self.interaction()
        await view.back.callback(back)
        self.assertIsInstance(back.edit_original_response.await_args.kwargs['view'], RemovalModeView)
        self.assertFalse(self.current()['deleted'])

    async def test_target_selection_previews_transfer_preserves_overlapping_authors_and_queues_both(self):
        target = self.target_book()
        self.add_essay()
        self.add_essay(target, 502)
        view = TransferTargetView(self.cog, self.current(), 99, 'transfer')
        view.select._values = [target['id']]
        selected = self.interaction()
        await view.choose(selected)
        confirmation = selected.edit_original_response.await_args.kwargs['view']
        text = selected.edit_original_response.await_args.kwargs['content']
        self.assertIn('Обе работы сохранятся отдельно', text)
        self.assertIn('Другой автор', text)
        self.assertFalse(self.current()['deleted'])
        result = self.interaction()
        await confirmation.confirm.callback(result)
        self.assertTrue(self.current()['deleted'])
        self.assertEqual({essay['source_id'] for essay in self.store.essays(target['id'])}, {501, 502})
        self.assertEqual([call.args[1] for call in self.service.request_book_refresh.call_args_list],
                         [self.book['id'], target['id']])
        self.assertIn('изменения в Discord выполняются', result.edit_original_response.await_args.kwargs['content'])
        status = result.edit_original_response.await_args.kwargs['view']
        self.assertEqual(status.operation['state'], 'pending')
        self.h.channels[13].create_thread.assert_not_awaited()

    async def test_nonkeep_commit_defers_then_waits_for_existing_writer_and_revalidates(self):
        target = self.target_book()
        self.add_essay()
        confirmation = self.confirmation('transfer', target)
        interaction = self.interaction()
        async with self.service.locks[1]:
            pending = asyncio.create_task(confirmation.confirm.callback(interaction))
            await asyncio.sleep(0)
            self.assertTrue(interaction.response.is_done())
            self.assertFalse(pending.done())
            self.assertFalse(self.current()['deleted'])
            self.add_essay(source_id=502)
        with self.assertRaisesRegex(ClubError, 'изменились'):
            await pending
        self.assertFalse(self.current()['deleted'])
        self.assertEqual(len(self.store.essays(self.book['id'])), 2)
        self.service.request_book_refresh.assert_not_called()

    async def test_stale_target_or_new_source_essay_invalidates_review(self):
        target = self.target_book()
        self.add_essay()
        confirmation = self.confirmation('transfer', target)
        self.store.update_book(1, target['id'], title='Переименовано')
        with self.assertRaisesRegex(ClubError, 'изменились'):
            await confirmation.confirm.callback(self.interaction())
        self.assertFalse(self.current()['deleted'])
        confirmation = self.confirmation('keep')
        self.add_essay(source_id=502)
        with self.assertRaisesRegex(ClubError, 'изменились'):
            await confirmation.confirm.callback(self.interaction())
        self.assertFalse(self.current()['deleted'])
        self.service.request_book_refresh.assert_not_called()

    async def test_picker_rejects_hidden_target_and_changed_target_snapshot(self):
        target = self.target_book()
        view = TransferTargetView(self.cog, self.current(), 99, 'transfer')
        for values in ([], [self.book['id']], ['invented'], [target['id'], target['id']]):
            view.select._values = values
            with self.assertRaisesRegex(ClubError, 'этой странице'):
                await view.choose(self.interaction())
        view.select._values = [target['id']]
        self.store.update_book(1, target['id'], author='Уточнено')
        with self.assertRaisesRegex(ClubError, 'изменена'):
            await view.choose(self.interaction())
        self.assertFalse(self.current()['deleted'])

    async def test_physical_deletion_requires_title_and_fresh_authorization(self):
        self.add_essay()
        confirmation = self.confirmation('all')
        interaction = self.interaction()
        await confirmation.confirm.callback(interaction)
        modal = interaction.response.send_modal.await_args.args[0]
        self.assertIsInstance(modal, RemovalTitleModal)
        self.assertFalse(self.current()['deleted'])
        self.h.guild.fetch_member.assert_not_awaited()
        modal.book_title._value = 'неверное название'
        with self.assertRaisesRegex(ClubError, 'Название не совпадает'):
            await modal.on_submit(self.interaction())
        self.assertFalse(self.current()['deleted'])
        modal.book_title._value = self.book['title']
        self.revoke_rest()
        with self.assertRaisesRegex(ClubError, 'организатор'):
            await modal.on_submit(self.interaction())
        self.assertFalse(self.current()['deleted'])
        self.service.request_book_refresh.assert_not_called()

    async def test_title_confirmation_commits_only_reviewed_plan_and_exposes_pending_status(self):
        self.add_essay()
        confirmation = self.confirmation('all')
        modal = RemovalTitleModal(confirmation)
        modal.book_title._value = self.book['title']
        interaction = self.interaction()
        await modal.on_submit(interaction)
        self.assertTrue(self.current()['deleted'])
        status = interaction.edit_original_response.await_args.kwargs['view']
        self.assertEqual(status.operation['state'], 'pending')
        self.assertNotIn('удалены из Discord', interaction.edit_original_response.await_args.kwargs['content'])
        self.assertTrue(self.store.one('SELECT deleted FROM bc_essays WHERE guild_id=1 AND source_id=501')['deleted'])
        with self.assertRaises(ClubError):
            await modal.on_submit(self.interaction())
        self.service.request_book_refresh.assert_called_once()

    async def test_new_views_and_modal_are_bound_before_any_ack_or_mutation(self):
        target = self.target_book()
        mode = RemovalModeView(self.cog, self.current(), 99)
        mode.mode._values = ['transfer']
        targets = TransferTargetView(self.cog, self.current(), 99, 'transfer')
        targets.select._values = [target['id']]
        confirmation = self.confirmation('all')
        modal = RemovalTitleModal(confirmation)
        modal.book_title._value = self.book['title']
        callbacks = (mode.choose, mode.cancel.callback, targets.choose, targets.next_page.callback,
                     targets.back.callback, confirmation.back.callback, modal.on_submit)
        for callback in callbacks:
            for wrong in ('actor', 'guild'):
                interaction = self.interaction(1 if wrong == 'actor' else 99)
                if wrong == 'guild':
                    interaction.guild_id = 2
                with self.assertRaises(ClubError):
                    await callback(interaction)
                interaction.response.defer.assert_not_awaited()
        self.assertFalse(self.current()['deleted'])

    async def test_pending_restore_opens_status_from_list_and_persistent_book(self):
        self.add_essay()
        plan = build_removal_plan(self.store, 1, self.book['id'], 'all')
        operation = commit_removal_plan(self.store, 1, 99, plan)
        direct = self.interaction()
        view = await open_restore_book(self.cog, direct, self.book['id'])
        self.assertIsInstance(view, RemovalOperationView)
        self.assertIn('Восстановление пока недоступно', direct.followup.send.await_args.args[0])
        listing = await open_deleted_books(self.cog, self.interaction())
        listing.select._values = [self.book['id']]
        choice = self.interaction()
        await listing.choose(choice)
        self.assertIsInstance(choice.edit_original_response.await_args.kwargs['view'], RemovalOperationView)
        persistent = RemovedBookView(self.cog, self.current())
        status = self.interaction()
        await persistent.status(status)
        self.assertEqual(status.followup.send.await_args.kwargs['view'].operation_id, operation['operation_id'])

    async def test_failed_operation_retry_rechecks_rights_requeues_same_resources_and_reports_progress(self):
        self.add_essay()
        self.add_essay(source_id=502)
        operation = commit_removal_plan(self.store, 1, 99,
                                        build_removal_plan(self.store, 1, self.book['id'], 'all'))
        resources = removal_resources(self.store, 1, operation['operation_id'])
        complete_removal_resource(self.store, 1, operation['operation_id'], resources[0]['id'])
        fail_removal_resource(self.store, 1, operation['operation_id'], resources[1]['id'], 'Не хватает прав')
        view = RemovalOperationView(self.cog, self.current(), 99, operation['operation_id'])
        refresh = self.interaction()
        await view.status.callback(refresh)
        text = refresh.edit_original_response.await_args.kwargs['content']
        self.assertIn('1/2 завершено', text)
        self.assertIn('Не хватает прав', text)
        self.assertFalse(view.retry.disabled)
        await view.retry.callback(self.interaction())
        after = removal_resources(self.store, 1, operation['operation_id'], pending_only=False)
        self.assertEqual([r['id'] for r in after], [r['id'] for r in resources])
        self.assertEqual([r['state'] for r in after], ['done', 'pending'])
        self.service.request_book_refresh.assert_called_once_with(self.h.guild, self.book['id'])
        self.h.guild.fetch_member.assert_awaited_once_with(99)
        self.assertTrue(view.retry.disabled)

    async def test_revoked_organizer_cannot_retry_failed_operation(self):
        self.add_essay()
        operation = commit_removal_plan(self.store, 1, 99,
                                        build_removal_plan(self.store, 1, self.book['id'], 'all'))
        resource = removal_resources(self.store, 1, operation['operation_id'])[0]
        fail_removal_resource(self.store, 1, operation['operation_id'], resource['id'], 'Нет прав')
        view = RemovalOperationView(self.cog, self.current(), 99, operation['operation_id'])
        self.revoke_rest()
        with self.assertRaisesRegex(ClubError, 'организатор'):
            await view.retry.callback(self.interaction())
        self.assertEqual(get_removal_operation(self.store, 1, operation['operation_id'])['state'], 'failed')
        self.service.request_book_refresh.assert_not_called()

    async def test_finished_transfer_restore_preview_does_not_promise_to_reverse_transfer_or_restore_deleted_files(self):
        target = self.target_book()
        self.add_essay()
        operation = commit_removal_plan(self.store, 1, 99,
                                        build_removal_plan(self.store, 1, self.book['id'], 'transfer_topic', target['id']))
        for resource in removal_resources(self.store, 1, operation['operation_id']):
            complete_removal_resource(self.store, 1, operation['operation_id'], resource['id'])
        interaction = self.interaction()
        view = await open_restore_book(self.cog, interaction, self.book['id'])
        self.assertIsInstance(view, RestoreBookConfirmation)
        text = interaction.followup.send.await_args.args[0]
        self.assertIn('эссе останутся у другой книги', text)
        self.assertIn('вложения не восстановятся', text)

    async def test_long_escaped_book_names_keep_complete_destructive_warning_in_discord_message(self):
        self.store.update_book(1, self.book['id'], title='*' * 180, author='_' * 180)
        target = self.store.create_book(1, '[' * 180, '~' * 180, '', 'long')
        self.add_essay()
        self.add_essay(target, 502)
        plan = build_removal_plan(self.store, 1, self.book['id'], 'transfer_topic', target['id'])
        text = removal_content(plan)
        self.assertLessEqual(len(text), 2000)
        self.assertIn('безвозвратному удалению', text)
        self.assertIn('не вернёт удалённые сообщения и вложения', text)


if __name__ == '__main__':
    unittest.main()

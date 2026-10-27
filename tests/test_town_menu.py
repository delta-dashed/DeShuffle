"""The compact pin opens private, current and permission-checked actions."""
import asyncio
from unittest.mock import AsyncMock, patch
import unittest

from bookclub.catalog_ui import AddBookModal, TownMenuView
from bookclub.store import ClubError
from bookclub.ui import Club
from test_bookclub import ClubFixture
from test_bookclub_discord import DiscordHarness


class TownMenuTests(ClubFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.h = DiscordHarness()
        self.cog = Club(self.h.bot, self.store)
        self.service = self.cog.service
        self.addAsyncCleanup(self.service.close)
        self.store.update_book(1, self.book['id'], status='reading')

    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 1

    async def menu(self, user=99):
        interaction = self.h.interaction(user)
        await TownMenuView(self.service, 1).children[0].callback(interaction)
        call = interaction.followup.send.await_args
        self.assertTrue(call.kwargs['ephemeral'])
        return call.kwargs['view']

    def action(self, view, action):
        return next(button for button in view.children if button.custom_id.rsplit(':', 1)[-1] == action)

    async def test_persistent_menu_registered_after_restart(self):
        view = TownMenuView(self.service, 1)
        self.assertEqual(len(view.children), 1)
        self.assertEqual(view.children[0].label, 'Ещё…')
        self.assertTrue(view.is_persistent())
        with patch.object(type(self.cog.worker), 'start'):
            await self.cog.cog_load()
        registered = [call.args[0] for call in self.h.bot.add_view.call_args_list]
        self.assertEqual(len([entry for entry in registered if isinstance(entry, TownMenuView)]), 1)

    async def test_organizer_gets_full_actions_in_private_valid_rows(self):
        view = await self.menu()
        self.assertEqual({button.custom_id.rsplit(':', 1)[-1] for button in view.children},
                         {'write', 'own', 'list', 'manage', 'meetings', 'add', 'import', 'queue', 'deleted'})
        self.assertEqual([len(row['components']) for row in view.to_components()], [3, 2, 4])
        self.h.guild.fetch_member.assert_awaited_once_with(99)
        self.h.bot.fetch_channel.assert_awaited_once_with(13)

    async def test_member_only_gets_allowed_actions_and_add_modal(self):
        view = await self.menu(1)
        actions = {button.custom_id.rsplit(':', 1)[-1] for button in view.children}
        self.assertEqual(actions, {'write', 'own', 'list', 'meetings', 'add'})
        interaction = self.h.interaction(1)
        await self.action(view, 'add').callback(interaction)
        self.assertIsInstance(interaction.response.send_modal.await_args.args[0], AddBookModal)

    async def test_read_only_member_does_not_get_add_action(self):
        self.h.channels[13].permissions_for.return_value.send_messages = False
        view = await self.menu(1)
        self.assertNotIn('add', {button.custom_id.rsplit(':', 1)[-1] for button in view.children})

    async def test_menu_open_checks_guild_history_and_member_access(self):
        interaction = self.h.interaction()
        interaction.guild_id = 2
        with self.assertRaises(ClubError):
            await TownMenuView(self.service, 1).open_menu(interaction)
        self.h.guild.fetch_member.assert_not_awaited()
        self.h.channels[13].permissions_for.return_value.read_message_history = False
        with self.assertRaises(ClubError):
            await self.menu()

    async def test_actor_scoped_menu_rejects_other_member(self):
        view = await self.menu()
        with self.assertRaisesRegex(ClubError, 'другим участником'):
            await self.action(view, 'add').callback(self.h.interaction(1))

    async def test_revoked_organizer_cannot_use_old_private_controls(self):
        view = await self.menu()
        self.store.configure(1, {**self.store.settings(1), 'organizers': [], 'organizer_roles': []})
        self.h.guild.owner_id = 90
        with self.assertRaisesRegex(ClubError, 'организатору'):
            await self.action(view, 'import').callback(self.h.interaction(99))

    async def test_current_book_is_resolved_at_open_and_old_menu_rejects_switch(self):
        old = await self.menu()
        self.store.update_book(1, self.book['id'], status='read')
        other = self.store.create_book(1, 'Новая текущая', 'Автор', '', 'next-current')
        self.store.update_book(1, other['id'], status='reading')
        with self.assertRaisesRegex(ClubError, 'изменилась'):
            await self.action(old, 'own').callback(self.h.interaction(99))
        current = await self.menu()
        book_actions = [button for button in current.children if button.custom_id.startswith('bc:book:')]
        self.assertEqual(len(book_actions), 5)
        self.assertTrue(all(f':{other["id"]}:' in button.custom_id for button in book_actions))
        self.store.update_book(1, other['id'], status='read')
        empty = await self.menu()
        self.assertFalse(any(button.custom_id.startswith('bc:book:') for button in empty.children))

    async def test_revoked_forum_access_blocks_old_private_menu(self):
        view = await self.menu()
        self.h.channels[13].permissions_for.return_value.view_channel = False
        with self.assertRaises(ClubError):
            await self.action(view, 'add').callback(self.h.interaction(99))


if __name__ == '__main__':
    unittest.main()

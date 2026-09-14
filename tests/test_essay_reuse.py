"""Imported and manually registered work remains reachable from persistent cards."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import discord

from bookclub.store import ClubError, Store
from bookclub.ui import BookView, Club, MeetingView
from test_bookclub import ClubFixture
from test_bookclub_discord import DiscordHarness


class EssayReuseTests(ClubFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.store.configure(1, dict(self.store.settings(1), essay_webhooks=False))
        self.store.set_published(1)
        self.h = DiscordHarness()
        self.cog = Club(self.h.bot, self.store)
        self.service = self.cog.service

    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 1.0

    def button(self, view, action):
        return next(child for child in view.children if child.custom_id.endswith(':' + action))

    def imported(self, ident=901, actor=1):
        """Represent an already completed production import, with its source ledger."""
        thread = self.h.channel(ident, parent_id=14, name=f'Книга · эссе · Участник {actor}')
        key = f'essay-import:1:{ident + 10000}'
        self.h.message(thread, ident, f'Архивное эссе\nАвтор: <@{actor}>\n-# bc:{key}')
        self.store.reserve_publication(key, 1, 14)
        self.store.save_publication(key, ident, ident)
        self.store.register_essay(1, self.book['id'], ident, ident, actor, thread.name, thread.jump_url)
        with self.store.tx() as db:
            db.execute("""INSERT INTO bc_import_runs(id,guild_id,actor_id,source_channel_id,budget_id,
              request_key,reserved_tokens,state,snapshot,created_at,updated_at)
              VALUES(?,1,99,41,'spent-budget',?,30000,'done','{}',?,?)""",
                       (key, key, self.now, self.now))
            db.execute('INSERT INTO bc_import_sources(guild_id,source_id,run_id,item_key,thread_id) VALUES(1,?,?,?,?)',
                       (ident + 10000, key, key, ident))
        return thread

    def human(self, ident=902, actor=1):
        thread = self.h.channel(ident, parent_id=14, owner_id=actor, name=f'Эссе {ident}')
        self.store.register_essay(1, self.book['id'], ident, ident, actor, thread.name, thread.jump_url)
        return thread

    async def test_add_reuses_completed_import_and_preserves_author_after_restart(self):
        thread = self.imported()
        restarted = Club(self.h.bot, Store(self.path, clock=lambda: self.now))
        result = await restarted.service.create_essay_space(self.h.guild, self.book['id'], 1)
        self.assertEqual((result['source_id'], result['author_id'], result['submitted']), (thread.id, 1, 1))
        self.assertEqual(result['managed'], 0)
        self.h.channels[14].create_thread.assert_not_awaited()
        self.assertEqual(len(self.store.essays(self.book['id'], submitted_only=False)), 1)

    async def test_import_takes_precedence_over_accidental_empty_draft_without_deleting_it(self):
        draft = await self.service.create_essay_space(self.h.guild, self.book['id'], 1)
        imported = self.imported()
        chosen = await self.service.create_essay_space(self.h.guild, self.book['id'], 1)
        self.assertEqual(chosen['source_id'], imported.id)
        self.assertIn(draft['source_id'], self.h.channels)
        self.assertEqual(len(self.store.essays(self.book['id'], submitted_only=False)), 2)
        self.h.channels[14].create_thread.assert_awaited_once()

    async def test_add_reuses_manually_registered_author_thread(self):
        thread = self.human()
        result = await self.service.create_essay_space(self.h.guild, self.book['id'], 1)
        self.assertEqual((result['source_id'], result['author_id']), (thread.id, 1))
        self.h.channels[14].create_thread.assert_not_awaited()

    async def test_deleted_import_is_marked_deleted_then_a_new_draft_can_be_created(self):
        deleted = self.imported()
        del self.h.channels[deleted.id]
        result = await self.service.create_essay_space(self.h.guild, self.book['id'], 1)
        self.assertNotEqual(result['source_id'], deleted.id)
        self.assertEqual(self.store.one('SELECT deleted FROM bc_essays WHERE source_id=?', (deleted.id,))['deleted'], 1)
        self.h.channels[14].create_thread.assert_awaited_once()

    async def test_forbidden_existing_import_does_not_trigger_another_post(self):
        thread = self.imported()
        thread.fetch_message.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason='Forbidden'), {'code': 50013, 'message': 'Missing permissions'})
        with self.assertRaises(discord.Forbidden):
            await self.service.create_essay_space(self.h.guild, self.book['id'], 1)
        self.h.channels[14].create_thread.assert_not_awaited()
        self.assertEqual(self.store.one('SELECT deleted FROM bc_essays WHERE source_id=?', (thread.id,))['deleted'], 0)

    async def test_open_includes_all_own_works_but_no_other_authors(self):
        first, second = self.imported(), self.human()
        other = self.imported(903, actor=2)
        interaction = self.h.interaction(1)
        await self.button(BookView(self.cog, self.book), 'own').callback(interaction)
        call = interaction.followup.send.await_args
        self.assertTrue(call.kwargs['ephemeral'])
        self.assertEqual({button.url for button in call.kwargs['view'].children}, {first.jump_url, second.jump_url})
        self.assertNotIn(other.jump_url, call.args[0])
        self.h.channels[14].create_thread.assert_not_awaited()

    async def test_open_read_only_archived_work_does_not_change_discord(self):
        thread = self.imported()
        thread.archived = True
        self.h.channels[14].permissions_for.return_value.send_messages = False
        self.h.channels[14].permissions_for.return_value.send_messages_in_threads = False
        thread.permissions_for.return_value.send_messages_in_threads = False
        interaction = self.h.interaction(1)
        await self.button(MeetingView(self.cog, self.meeting), 'own').callback(interaction)
        self.assertEqual(interaction.followup.send.await_args.kwargs['view'].children[0].url, thread.jump_url)
        self.assertTrue(thread.archived)
        thread.edit.assert_not_awaited()
        thread.send.assert_not_awaited()
        thread.messages[thread.id].edit.assert_not_awaited()
        self.h.channels[14].create_thread.assert_not_awaited()

    async def test_open_without_work_only_explains_create_button(self):
        interaction = self.h.interaction(1)
        await self.button(BookView(self.cog, self.book), 'own').callback(interaction)
        self.assertIn('пока нет', interaction.followup.send.await_args.args[0])
        self.h.channels[14].create_thread.assert_not_awaited()
        self.assertEqual(self.store.essays(self.book['id'], submitted_only=False), [])

    async def test_open_rechecks_member_and_current_permissions(self):
        self.imported()
        self.h.channels[14].permissions_for.return_value.view_channel = False
        with self.assertRaises(ClubError):
            await self.service.own_essays(self.h.guild, self.book['id'], 1)
        self.h.channels[14].permissions_for.return_value.view_channel = True
        del self.h.members[1]
        with self.assertRaises(ClubError):
            await self.service.own_essays(self.h.guild, self.book['id'], 1)
        self.h.channels[14].create_thread.assert_not_awaited()

    async def test_own_buttons_are_persistent_and_restore_for_both_cards(self):
        restarted = Club(self.h.bot, Store(self.path, clock=lambda: self.now))
        with patch.object(restarted.worker, 'start'):
            await restarted.cog_load()
        views = [call.args[0] for call in self.h.bot.add_view.call_args_list]
        essay_views = [view for view in views if isinstance(view, (BookView, MeetingView))]
        self.assertEqual(len(essay_views), 2)
        for view in essay_views:
            self.assertTrue(view.is_persistent())
            self.assertEqual(self.button(view, 'own').label, 'Открыть моё эссе')

    async def test_multiple_own_works_are_paginated_within_discord_button_limit(self):
        for ident in range(2000, 2026):
            self.human(ident)
        interaction = self.h.interaction(1)
        await self.button(BookView(self.cog, self.book), 'own').callback(interaction)
        calls = interaction.followup.send.await_args_list
        self.assertEqual([len(call.kwargs['view'].children) for call in calls], [25, 1])
        self.assertTrue(all(call.kwargs['ephemeral'] for call in calls))


if __name__ == '__main__':
    unittest.main()

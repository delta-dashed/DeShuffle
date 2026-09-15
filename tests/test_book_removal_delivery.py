"""Permanent removal obeys frozen IDs, current access and recoverable DELETEs."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import discord

from bookclub import book_removal as plans
from bookclub.book_removal_delivery import process_disposal
from bookclub.service import Service
from bookclub.store import Store
from test_bookclub import ClubFixture
from test_bookclub_discord import DiscordHarness, not_found


class BookRemovalDeliveryTests(ClubFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.h = DiscordHarness()
        self.h.members[9999] = SimpleNamespace(id=9999, guild=self.h.guild, bot=True)
        self.service = Service(self.h.bot, self.store)
        self.addAsyncCleanup(self.service.close)
        self.topic = self.thread(700, parent=13)
        self.h.message(self.topic, 700, 'Карточка книги')
        self.pubkey = f'book:{self.book["id"]}'
        self.store.reserve_publication(self.pubkey, 1, 13)
        self.store.save_publication(self.pubkey, 700, 700, 'stable')

    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 1.0

    def thread(self, ident, *, parent=14, owner=9999):
        channel = self.h.channel(ident, parent_id=parent, owner_id=owner)

        async def delete(**kwargs):
            if ident not in self.h.channels:
                raise not_found()
            del self.h.channels[ident]

        channel.delete = AsyncMock(side_effect=delete)
        return channel

    def essay(self, ident=800, *, book=None, channel=None, managed=False):
        book = book or self.book
        if channel is None:
            channel = self.thread(ident, owner=1)
        else:
            self.h.message(channel, ident, 'Полный текст эссе').author = self.h.members[1]
        self.store.register_essay(1, book['id'], ident, channel.id, 1, 'Эссе',
                                  f'https://discord.com/channels/1/{channel.id}/{ident}', managed=managed)
        return channel

    def commit(self, mode='topic', target=None):
        plan = plans.build_removal_plan(self.store, 1, self.book['id'], mode,
                                        target['id'] if target else None)
        self.operation = plans.commit_removal_plan(self.store, 1, 99, plan)
        return self.operation

    def state(self):
        return plans.get_removal_operation(self.store, 1, self.operation['id'])['state']

    def resources(self):
        return plans.removal_resources(self.store, 1, self.operation['id'], pending_only=False)

    async def run_plan(self):
        async with self.service.locks[1]:
            await process_disposal(self.service, self.h.guild, self.book['id'])

    async def test_topic_removal_preserves_essays_bindings_and_does_not_repeat_delete(self):
        essay = self.essay()
        before = self.store.publication(self.pubkey)
        self.commit()
        await self.run_plan()
        self.assertEqual(self.state(), 'done')
        self.topic.delete.assert_awaited_once()
        essay.delete.assert_not_awaited()
        self.assertEqual(self.store.publication(self.pubkey), before)
        self.assertFalse(self.store.one('SELECT deleted FROM bc_essays WHERE source_id=800')['deleted'])
        await self.run_plan()
        self.topic.delete.assert_awaited_once()

    async def test_all_removes_essay_thread_and_exact_text_message_before_book_topic(self):
        essay_thread = self.essay()
        mixed = self.h.channels[12]
        self.essay(801, channel=mixed)
        self.h.message(mixed, 802, 'Чужое сообщение')
        text = mixed.messages[801]
        self.commit('all')
        original_delete = self.topic.delete.side_effect

        async def book_last(**kwargs):
            self.assertNotIn(800, self.h.channels)
            self.assertNotIn(801, mixed.messages)
            return await original_delete(**kwargs)

        self.topic.delete.side_effect = book_last
        mixed.permissions_for.return_value.manage_messages = True
        await self.run_plan()
        self.assertEqual(self.state(), 'done')
        essay_thread.delete.assert_awaited_once()
        text.delete.assert_awaited_once()
        self.assertIn(12, self.h.channels)
        self.assertIn(802, mixed.messages)
        self.assertEqual(len(self.store.rows('SELECT * FROM bc_essays WHERE deleted=1')), 2)

    async def test_lost_delete_response_restart_completes_with_get_without_second_delete(self):
        self.commit()

        async def lost_response(**kwargs):
            del self.h.channels[700]
            raise OSError('private transport detail')

        self.topic.delete.side_effect = lost_response
        await self.run_plan()
        self.assertEqual((self.state(), self.resources()[0]['state']), ('pending', 'deleting'))
        self.store = Store(self.path, clock=lambda: self.now)
        self.service.store = self.store
        await self.run_plan()
        self.assertEqual(self.state(), 'done')
        self.topic.delete.assert_awaited_once()
        self.assertIsNotNone(self.store.publication(self.pubkey))

    async def test_uncertain_delete_resource_still_exists_fresh_authorization_before_retry(self):
        self.commit()
        original = self.topic.delete.side_effect
        self.topic.delete.side_effect = OSError('connection reset')
        await self.run_plan()
        self.topic.delete.side_effect = original
        self.h.guild.fetch_member.reset_mock()
        await self.run_plan()
        self.assertEqual(self.state(), 'done')
        self.assertEqual(self.topic.delete.await_count, 2)
        self.assertIn(99, [call.args[0] for call in self.h.guild.fetch_member.await_args_list])

    async def test_http_500_after_delete_recovers_as_uncertain_without_duplicate(self):
        self.commit()

        async def server_error(**kwargs):
            del self.h.channels[700]
            raise discord.HTTPException(SimpleNamespace(status=502, reason='Bad Gateway'), 'private response')

        self.topic.delete.side_effect = server_error
        await self.run_plan()
        self.assertEqual(self.resources()[0]['state'], 'deleting')
        await self.run_plan()
        self.assertEqual(self.state(), 'done')
        self.topic.delete.assert_awaited_once()

    async def test_already_missing_exact_topic_finishes_without_permissions_or_delete(self):
        self.commit()
        del self.h.channels[700]
        del self.h.members[99]
        await self.run_plan()
        self.assertEqual(self.state(), 'done')
        self.topic.delete.assert_not_awaited()
        self.h.guild.fetch_member.assert_not_awaited()

    async def test_missing_parent_does_not_treat_existing_topic_as_deleted(self):
        self.commit()
        del self.h.channels[13]
        await self.run_plan()
        self.assertEqual(self.state(), 'failed')
        self.topic.delete.assert_not_awaited()
        self.assertIn(700, self.h.channels)

    async def test_wrong_guild_and_changed_parent_are_rejected(self):
        self.commit()
        self.topic.guild = SimpleNamespace(id=2)
        await self.run_plan()
        self.assertEqual(self.state(), 'failed')
        self.topic.delete.assert_not_awaited()
        plans.retry_removal_operation(self.store, 1, self.operation['id'], 99)
        self.topic.guild, self.topic.parent_id = self.h.guild, 14
        await self.run_plan()
        self.assertEqual(self.state(), 'failed')
        self.topic.delete.assert_not_awaited()

    async def test_replaced_publication_during_actor_lookup_stops_delete(self):
        self.commit()
        fetch = self.h.guild.fetch_member.side_effect

        async def change_binding(ident):
            if ident == 99:
                self.store.save_publication(self.pubkey, 999, 999, 'replacement')
            return await fetch(ident)

        self.h.guild.fetch_member.side_effect = change_binding
        await self.run_plan()
        self.assertEqual(self.state(), 'failed')
        self.topic.delete.assert_not_awaited()
        self.assertIn('Привязка', self.resources()[0]['last_error'])

    async def test_configured_forum_change_after_confirmation_stops_without_discord_write(self):
        self.commit()
        config = self.store.settings(1)
        config['books'] = 17
        self.store.configure(1, config)
        await self.run_plan()
        self.assertEqual(self.state(), 'failed')
        self.topic.delete.assert_not_awaited()

    async def test_catalog_or_foreign_book_binding_prevents_thread_deletion(self):
        self.commit()
        self.store.reserve_publication('catalog:1', 1, 13)
        self.store.save_publication('catalog:1', 700, 700)
        await self.run_plan()
        self.assertEqual(self.state(), 'failed')
        self.topic.delete.assert_not_awaited()

    async def test_foreign_essay_in_same_thread_blocks_all_and_preserves_book_topic(self):
        shared = self.essay()
        other = self.store.create_book(1, 'Другая', 'Автор', '', 'other')
        self.essay(801, book=other, channel=shared)
        self.commit('all')
        await self.run_plan()
        self.assertEqual(self.state(), 'failed')
        shared.delete.assert_not_awaited()
        self.topic.delete.assert_not_awaited()
        self.assertFalse(self.store.one('SELECT deleted FROM bc_essays WHERE source_id=801')['deleted'])

    async def test_other_book_publication_inside_essay_thread_blocks_all(self):
        essay = self.essay()
        other = self.store.create_book(1, 'Другая', 'Автор', '', 'other')
        self.store.reserve_publication(f'book:{other["id"]}', 1, 13)
        self.store.save_publication(f'book:{other["id"]}', 800, 850)
        self.commit('all')
        await self.run_plan()
        self.assertEqual(self.state(), 'failed')
        essay.delete.assert_not_awaited()
        self.topic.delete.assert_not_awaited()

    async def test_deleted_actor_pauses_and_does_not_automatically_retry_when_access_returns(self):
        self.commit()
        member = self.h.members.pop(99)
        await self.run_plan()
        self.assertEqual(self.state(), 'failed')
        self.h.members[99] = member
        await self.run_plan()
        self.topic.delete.assert_not_awaited()
        plans.retry_removal_operation(self.store, 1, self.operation['id'], 99)
        await self.run_plan()
        self.assertEqual(self.state(), 'done')
        self.topic.delete.assert_awaited_once()

    async def test_organizer_role_is_rechecked_before_delete(self):
        self.commit()
        self.h.guild.owner_id = 98
        self.h.members[99].roles = []
        config = self.store.settings(1)
        config['organizers'] = []
        self.store.configure(1, config)
        await self.run_plan()
        self.assertEqual(self.state(), 'failed')
        self.topic.delete.assert_not_awaited()

    async def test_bot_manage_threads_missing_stops_and_keeps_truthful_error(self):
        self.commit()
        self.h.channels[13].permissions_for.return_value.manage_threads = False
        await self.run_plan()
        self.assertEqual(self.state(), 'failed')
        self.assertIn('Manage Threads', self.resources()[0]['last_error'])
        self.topic.delete.assert_not_awaited()

    async def test_message_only_essay_requires_manage_messages(self):
        channel = self.essay(801, channel=self.h.channels[12])
        message = channel.messages[801]
        self.commit('all')
        await self.run_plan()
        self.assertEqual(self.state(), 'failed')
        self.assertIn('Manage Messages', self.resources()[0]['last_error'])
        message.delete.assert_not_awaited()
        self.topic.delete.assert_not_awaited()

    async def test_transfer_topic_waits_for_all_essay_projections(self):
        essay = self.essay()
        target = self.store.create_book(1, 'Основная книга', 'Автор', '', 'target')
        self.commit('transfer_topic', target)
        await self.run_plan()
        self.topic.delete.assert_not_awaited()
        self.assertEqual(self.state(), 'pending')
        for resource in self.resources():
            if resource['kind'] == 'refresh_essay':
                plans.complete_removal_resource(self.store, 1, self.operation['id'], resource['id'])
        await self.run_plan()
        self.assertEqual(self.state(), 'done')
        self.topic.delete.assert_awaited_once()
        essay.delete.assert_not_awaited()

    async def test_essay_rebound_during_permission_check_is_not_deleted(self):
        essay = self.essay()
        target = self.store.create_book(1, 'Другая', 'Автор', '', 'target')
        self.commit('all')
        fetch = self.h.guild.fetch_member.side_effect

        async def rebind(ident):
            if ident == 99:
                with self.store.tx() as db:
                    db.execute('UPDATE bc_essays SET book_id=? WHERE source_id=800', (target['id'],))
            return await fetch(ident)

        self.h.guild.fetch_member.side_effect = rebind
        await self.run_plan()
        self.assertEqual(self.state(), 'failed')
        essay.delete.assert_not_awaited()
        self.topic.delete.assert_not_awaited()

    async def test_starter_sender_changed_stops_bound_book_topic_deletion(self):
        self.commit()
        self.topic.messages[700].author = self.h.members[1]
        await self.run_plan()
        self.assertEqual(self.state(), 'failed')
        self.topic.delete.assert_not_awaited()

    async def test_lost_message_delete_response_does_not_remove_adjacent_message(self):
        channel = self.essay(801, channel=self.h.channels[12])
        channel.permissions_for.return_value.manage_messages = True
        message = channel.messages[801]
        self.h.message(channel, 802, 'Оставить')
        self.commit('all')

        async def lost():
            del channel.messages[801]
            raise OSError('lost acknowledgement')

        message.delete.side_effect = lost
        await self.run_plan()
        self.assertEqual(self.state(), 'pending')
        await self.run_plan()
        self.assertEqual(self.state(), 'done')
        message.delete.assert_awaited_once()
        self.assertIn(802, channel.messages)

    async def test_forbidden_response_is_sanitized_and_not_claimed_done(self):
        self.commit()
        self.topic.delete.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason='Forbidden'), 'private response must not appear')
        await self.run_plan()
        self.assertEqual(self.state(), 'failed')
        self.assertNotIn('private response', self.resources()[0]['last_error'])
        self.assertIn(700, self.h.channels)

    async def test_managed_webhook_essay_deletes_using_saved_sender_and_retains_binding(self):
        essay = self.thread(800)
        starter = self.h.message(essay, 800, 'Шапка от имени участника')
        starter.webhook_id = 77
        self.store.register_essay(1, self.book['id'], 800, 800, 1, 'Эссе', essay.jump_url, managed=True)
        key = f'essay-space:{self.book["id"]}:1'
        self.store.reserve_publication(key, 1, 14)
        self.store.save_publication(key, 800, 800, 'existing')
        with self.store.tx() as db:
            db.execute('UPDATE bc_publications SET webhook_id=77 WHERE key=?', (key,))
        before = self.store.publication(key)
        self.commit('all')
        await self.run_plan()
        self.assertEqual(self.state(), 'done')
        essay.delete.assert_awaited_once()
        self.assertEqual(self.store.publication(key), before)

    async def test_completed_import_ledgers_and_body_bindings_survive_forum_deletion(self):
        essay = self.thread(800)
        starter = self.h.message(essay, 800, 'Архивное эссе')
        starter.webhook_id = 77
        self.store.register_essay(1, self.book['id'], 800, 800, 1, 'Эссе', essay.jump_url, managed=True)
        key = 'essay-import:1:400'
        for pub_key, ident in [(key, 800), (key + ':message:400:1:v2', 801)]:
            self.store.reserve_publication(pub_key, 1, 14)
            self.store.save_publication(pub_key, 800, ident, 'existing')
            with self.store.tx() as db:
                db.execute('UPDATE bc_publications SET webhook_id=77 WHERE key=?', (pub_key,))
        with self.store.tx() as db:
            db.execute("INSERT INTO bc_import_budgets VALUES('legacy-essays-v1',1,30714)")
            db.execute('''INSERT INTO bc_import_runs
              (id,guild_id,actor_id,source_channel_id,budget_id,request_key,reserved_tokens,
               state,snapshot,created_at,updated_at)
              VALUES('done-run',1,99,987,'legacy-essays-v1','legacy',30714,'done','{}',1,1)''')
            db.execute("INSERT INTO bc_import_sources VALUES(1,400,'done-run',?,800)", (key,))
        tables = ('bc_import_runs', 'bc_import_budgets', 'bc_import_sources', 'bc_publications')
        before = {table: self.store.rows(f'SELECT * FROM {table} ORDER BY rowid') for table in tables}
        self.commit('all')
        await self.run_plan()
        self.assertEqual(self.state(), 'done')
        essay.delete.assert_awaited_once()
        self.assertEqual(before, {table: self.store.rows(f'SELECT * FROM {table} ORDER BY rowid') for table in tables})

    async def test_message_author_mismatch_is_not_deleted(self):
        channel = self.essay(801, channel=self.h.channels[12])
        message = channel.messages[801]
        channel.permissions_for.return_value.manage_messages = True
        self.commit('all')
        message.author = self.h.members[2]
        await self.run_plan()
        self.assertEqual(self.state(), 'failed')
        message.delete.assert_not_awaited()

    async def test_successful_partial_removal_is_not_repeated_after_explicit_retry(self):
        first = self.essay(800)
        second = self.essay(801)
        second_delete = second.delete.side_effect
        second.delete.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'denied')
        self.commit('all')
        await self.run_plan()
        self.assertEqual(self.state(), 'failed')
        self.assertEqual([row['state'] for row in self.resources()], ['done', 'failed', 'pending'])
        second.delete.side_effect = second_delete
        plans.retry_removal_operation(self.store, 1, self.operation['id'], 99)
        await self.run_plan()
        self.assertEqual(self.state(), 'done')
        first.delete.assert_awaited_once()
        self.assertEqual(second.delete.await_count, 2)
        self.topic.delete.assert_awaited_once()

    async def test_message_registered_as_essay_cannot_delete_catalog_publication(self):
        channel = self.essay(801, channel=self.h.channels[12])
        channel.permissions_for.return_value.manage_messages = True
        self.store.reserve_publication('catalog:1', 1, channel.id)
        self.store.save_publication('catalog:1', channel.id, 801)
        self.commit('all')
        await self.run_plan()
        self.assertEqual(self.state(), 'failed')
        channel.messages[801].delete.assert_not_awaited()
        self.topic.delete.assert_not_awaited()

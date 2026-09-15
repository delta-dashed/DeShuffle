"""Duplicate-book reconciliation preserves messages, author identity and ledgers."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from bookclub.book_removal import (build_removal_plan, commit_removal_plan,
                                  get_removal_operation, removal_resources,
                                  retry_removal_operation)
from bookclub.import_publication import archive_header
from bookclub.store import Store
from bookclub.ui import Club
from test_archive_import import ArchiveHarness
from test_bookclub import ClubFixture


class RemovalProjectionTests(ClubFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.h = ArchiveHarness()
        self.cog = Club(self.h.bot, self.store)
        self.service = self.cog.service
        self.addAsyncCleanup(self.service.close)
        self.target = self.store.create_book(1, 'Основная книга', 'Тот же автор', '', 'destination')
        self.store.set_published(1)

    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 1

    async def publish(self):
        async with self.service.locks[1]:
            await self.service.refresh(self.h.guild)

    async def move(self, mode='transfer'):
        async with self.service.locks[1]:
            plan = build_removal_plan(self.store, 1, self.book['id'], mode, self.target['id'])
            return commit_removal_plan(self.store, 1, 99, plan)

    def body(self, essay):
        self.h.seq += 1
        message = self.h.human_message(self.h.channels[essay['channel_id']], self.h.seq,
                                       'Авторский текст и выводы сохраняются.', 1)
        message.attachments = [SimpleNamespace(id=9090, filename='my-essay.pdf', size=123)]
        return message

    def hooks_sent(self):
        return sum(hook.send.await_count for hook in self.h.hooks.values())

    async def test_same_author_two_books_keeps_both_essays_and_creation_opens_existing_after_restart(self):
        await self.publish()
        first = await self.service.create_essay_space(self.h.guild, self.book['id'], 1)
        second = await self.service.create_essay_space(self.h.guild, self.target['id'], 1)
        body = self.body(first)
        await self.service.register_thread(self.h.channels[first['channel_id']], prompt=False)
        thread = self.h.channels[first['channel_id']]
        thread.archived = True
        starter = thread.messages[thread.id]
        author, avatar, webhook = starter.author, starter.author.display_avatar, starter.webhook_id
        sent = self.hooks_sent()
        meeting = self.store.meeting(1, self.meeting['id'])
        operation = await self.move()
        await self.publish()
        self.assertEqual(get_removal_operation(self.store, 1, operation['id'])['state'], 'done')
        self.assertEqual(self.store.essays(self.book['id'], submitted_only=False), [])
        works = self.store.essays(self.target['id'], submitted_only=False)
        self.assertEqual({work['source_id'] for work in works}, {first['source_id'], second['source_id']})
        self.assertEqual(starter.webhook_id, webhook)
        self.assertIs(starter.author, author)
        self.assertIs(starter.author.display_avatar, avatar)
        self.assertIn('Основная книга', starter.content)
        self.assertIn('Основная книга', thread.name)
        self.assertTrue(thread.archived)
        self.assertEqual(body.attachments[0].id, 9090)
        body.edit.assert_not_awaited()
        body.delete.assert_not_awaited()
        self.assertEqual(self.store.meeting(1, self.meeting['id']), meeting)
        restarted = Club(self.h.bot, Store(self.path, clock=lambda: self.now))
        self.addAsyncCleanup(restarted.service.close)
        own = await restarted.service.own_essays(self.h.guild, self.target['id'], 1)
        self.assertEqual({work['source_id'] for work in own}, {first['source_id'], second['source_id']})
        opened = await restarted.service.create_essay_space(self.h.guild, self.target['id'], 1)
        self.assertIn(opened['source_id'], {first['source_id'], second['source_id']})
        self.assertEqual(self.hooks_sent(), sent)

    async def test_imported_header_changes_in_place_with_budget_and_source_ledger_untouched(self):
        await self.publish()
        key = 'essay-import:1:6061'
        pub = await self.service.publish_essay_starter(self.h.guild, self.h.channels[14], key,
            'Книга · Эссе · Участник 1', archive_header(self.book, 1), self.h.members[1])
        thread = self.h.channels[pub['channel_id']]
        self.store.register_essay(1, self.book['id'], thread.id, thread.id, 1, thread.name, thread.jump_url)
        with self.store.tx() as db:
            db.execute("INSERT INTO bc_import_budgets VALUES('finished-budget',1,30714)")
            db.execute('''INSERT INTO bc_import_runs
              (id,guild_id,actor_id,source_channel_id,budget_id,request_key,reserved_tokens,state,
               snapshot,plan,created_at,updated_at) VALUES('finished-run',1,99,41,'finished-budget',
               'finished-request',30714,'done','{}','{}',?,?)''', (self.now, self.now))
            db.execute("INSERT INTO bc_import_sources VALUES(1,6061,'finished-run',?,?)", (key, thread.id))
        before = {table: self.store.rows(f'SELECT * FROM {table}') for table in
                  ('bc_import_budgets', 'bc_import_runs', 'bc_import_sources')}
        starter = thread.messages[thread.id]
        starter.attachments = [SimpleNamespace(id=222, filename='attached.pdf', size=30)]
        identity = starter.author
        sent = self.hooks_sent()
        operation = await self.move()
        await self.publish()
        self.assertEqual(get_removal_operation(self.store, 1, operation['id'])['state'], 'done')
        self.assertIs(thread.messages[thread.id], starter)
        self.assertIs(starter.author, identity)
        self.assertEqual(starter.attachments[0].id, 222)
        self.assertEqual(starter.content, archive_header(self.target, 1))
        self.assertEqual(self.store.publication(key)['message_id'], thread.id)
        self.assertEqual({table: self.store.rows(f'SELECT * FROM {table}') for table in before}, before)
        self.assertTrue(await self.service.register_thread(thread, prompt=False))
        self.assertEqual(self.store.essays(self.target['id'])[0]['source_id'], thread.id)
        self.assertEqual(self.hooks_sent(), sent)

    async def test_lost_header_edit_response_retries_same_id_without_new_post(self):
        await self.publish()
        essay = await self.service.create_essay_space(self.h.guild, self.book['id'], 1)
        thread = self.h.channels[essay['channel_id']]
        starter = thread.messages[thread.id]
        hook = self.h.hooks[starter.webhook_id]
        edit = hook.edit_message.side_effect

        async def lost_response(*args, **kwargs):
            await edit(*args, **kwargs)
            raise OSError('simulated response loss')

        hook.edit_message.side_effect = lost_response
        sent = self.hooks_sent()
        operation = await self.move()
        await self.publish()
        self.assertEqual(get_removal_operation(self.store, 1, operation['id'])['state'], 'failed')
        self.assertIn('Основная книга', starter.content)
        retry_removal_operation(self.store, 1, operation['id'], 99)
        hook.edit_message.side_effect = edit
        edits = hook.edit_message.await_count
        await self.publish()
        self.assertEqual(get_removal_operation(self.store, 1, operation['id'])['state'], 'done')
        self.assertEqual(hook.edit_message.await_count, edits)
        self.assertEqual(self.hooks_sent(), sent)
        self.assertIs(thread.messages[thread.id], starter)

    async def test_native_post_and_original_text_message_are_only_rebound(self):
        await self.publish()
        thread = self.h.channel(8000, parent_id=14, owner_id=1, name='Мой собственный заголовок')
        starter = thread.messages[thread.id]
        original = self.h.human_message(self.h.source, 8001, 'Оригинальный текст эссе', 2)
        self.store.register_essay(1, self.book['id'], thread.id, thread.id, 1, thread.name, thread.jump_url)
        self.store.register_essay(1, self.book['id'], original.id, self.h.source.id, 2, 'Личное эссе', original.jump_url)
        operation = await self.move()
        await self.publish()
        self.assertEqual(get_removal_operation(self.store, 1, operation['id'])['state'], 'done')
        self.assertEqual(thread.name, 'Мой собственный заголовок')
        starter.edit.assert_not_awaited()
        original.edit.assert_not_awaited()
        original.delete.assert_not_awaited()
        self.assertEqual({row['source_id'] for row in self.store.essays(self.target['id'])}, {8000, 8001})

    async def test_archived_thread_edit_returns_new_snapshot_like_discord_sdk(self):
        await self.publish()
        essay = await self.service.create_essay_space(self.h.guild, self.book['id'], 1)
        thread = self.h.channels[essay['channel_id']]
        thread.archived = True
        snapshots = []

        async def new_snapshot(**changes):
            old = self.h.channels[thread.id]
            fresh = self.h.channel(old.id, parent_id=old.parent_id, owner_id=old.owner_id,
                                   name=changes.get('name', old.name))
            fresh.archived = changes.get('archived', old.archived)
            fresh.messages = old.messages
            fresh.edit = AsyncMock(side_effect=new_snapshot)
            snapshots.append(fresh)
            return fresh

        thread.edit = AsyncMock(side_effect=new_snapshot)
        operation = await self.move()
        await self.publish()
        self.assertEqual(get_removal_operation(self.store, 1, operation['id'])['state'], 'done')
        self.assertTrue(snapshots)
        self.assertIsNot(self.h.channels[thread.id], thread)
        self.assertTrue(self.h.channels[thread.id].archived)
        self.assertIn('Основная книга', self.h.channels[thread.id].name)
        moved = self.store.one('SELECT * FROM bc_essays WHERE guild_id=1 AND source_id=?', (thread.id,))
        self.assertEqual(moved['title'], self.h.channels[thread.id].name)

    async def archived_retry(self, *, restart=False, cancelled=False):
        await self.publish()
        essay = await self.service.create_essay_space(self.h.guild, self.book['id'], 1)
        thread = self.h.channels[essay['channel_id']]
        body = self.body(essay)
        starter = thread.messages[thread.id]
        identity, attachment = starter.author, body.attachments[0]
        thread.archived = True
        with self.store.tx() as db:
            db.execute("INSERT INTO bc_import_budgets VALUES('already-spent',1,30714)")
        ledger = {table: self.store.rows(f'SELECT * FROM {table}') for table in
                  ('bc_import_budgets', 'bc_import_runs', 'bc_import_sources')}
        sent = self.hooks_sent()
        channel_ids = set(self.h.channels)
        operation = await self.move()
        requests, before_unarchive = [], []
        failure_remaining = True

        async def new_snapshot(**changes):
            nonlocal failure_remaining
            requests.append(changes)
            old = self.h.channels[thread.id]
            if changes.get('archived') is False and old.archived:
                # Read SQLite afresh before simulating the outbound PATCH.
                disk_store = Store(self.path, clock=lambda: self.now)
                resource = removal_resources(disk_store, 1, operation['id'], False)[0]
                before_unarchive.append(resource.get('original_archived'))
            if changes.get('archived') is True and failure_remaining:
                failure_remaining = False
                if cancelled:
                    raise asyncio.CancelledError()
                raise OSError('simulated rearchive failure')
            fresh = self.h.channel(old.id, parent_id=old.parent_id, owner_id=old.owner_id,
                                   name=changes.get('name', old.name))
            fresh.archived = changes.get('archived', old.archived)
            fresh.messages = old.messages
            fresh.edit = AsyncMock(side_effect=new_snapshot)
            return fresh

        thread.edit = AsyncMock(side_effect=new_snapshot)
        if cancelled:
            with self.assertRaises(asyncio.CancelledError):
                await self.publish()
        else:
            await self.publish()
        self.assertFalse(self.h.channels[thread.id].archived)
        state = 'pending' if cancelled else 'failed'
        self.assertEqual(get_removal_operation(self.store, 1, operation['id'])['state'], state)
        self.assertNotEqual(removal_resources(self.store, 1, operation['id'], False)[0]['state'], 'done')
        if restart:
            await self.service.close()
            self.store = Store(self.path, clock=lambda: self.now)
            self.cog = Club(self.h.bot, self.store)
            self.service = self.cog.service
            self.addAsyncCleanup(self.service.close)
        if not cancelled:
            retry_removal_operation(self.store, 1, operation['id'], 99)
        await self.publish()
        self.assertEqual(get_removal_operation(self.store, 1, operation['id'])['state'], 'done')
        self.assertTrue(self.h.channels[thread.id].archived)
        self.assertEqual(before_unarchive, [True])
        resource = removal_resources(self.store, 1, operation['id'], False)[0]
        self.assertIs(resource['original_archived'], True)
        self.assertEqual(resource['state'], 'done')
        self.assertEqual(sum(change.get('archived') is True for change in requests), 2)
        self.assertIs(self.h.channels[thread.id].messages[starter.id], starter)
        self.assertIs(starter.author, identity)
        self.assertIs(self.h.channels[thread.id].messages[body.id], body)
        self.assertIs(body.attachments[0], attachment)
        body.edit.assert_not_awaited()
        body.delete.assert_not_awaited()
        publication = self.store.one('SELECT * FROM bc_publications WHERE message_id=?', (starter.id,))
        self.assertEqual((publication['channel_id'], publication['message_id']), (thread.id, starter.id))
        self.assertEqual(self.hooks_sent(), sent)
        self.assertEqual(set(self.h.channels), channel_ids)
        self.assertEqual({table: self.store.rows(f'SELECT * FROM {table}') for table in ledger}, ledger)

    async def test_rearchive_failure_explicit_retry_restores_original_archived_state(self):
        await self.archived_retry()

    async def test_rearchive_failure_restart_and_retry_restore_original_archived_state(self):
        await self.archived_retry(restart=True)

    async def test_cancelled_rearchive_resumes_pending_operation_after_restart(self):
        await self.archived_retry(restart=True, cancelled=True)

    async def test_transfer_keeps_originally_open_thread_open(self):
        await self.publish()
        essay = await self.service.create_essay_space(self.h.guild, self.book['id'], 1)
        thread = self.h.channels[essay['channel_id']]
        self.assertFalse(thread.archived)
        operation = await self.move()
        await self.publish()
        self.assertEqual(get_removal_operation(self.store, 1, operation['id'])['state'], 'done')
        self.assertFalse(self.h.channels[thread.id].archived)
        resource = removal_resources(self.store, 1, operation['id'], False)[0]
        self.assertIs(resource['original_archived'], False)
        self.assertFalse(any(call.kwargs.get('archived') is True for call in thread.edit.await_args_list))

    async def test_gateway_deletion_keeps_frozen_publications_and_imported_identifiers(self):
        await self.publish()
        essay = await self.service.create_essay_space(self.h.guild, self.book['id'], 1)
        book_pub = self.store.publication(f'book:{self.book["id"]}')
        frozen = self.store.rows('SELECT * FROM bc_publications ORDER BY key')
        plan = build_removal_plan(self.store, 1, self.book['id'], 'all')
        operation = commit_removal_plan(self.store, 1, 99, plan)
        await self.cog.on_raw_thread_delete(SimpleNamespace(guild_id=1, thread_id=book_pub['channel_id']))
        await self.cog.on_raw_message_delete(SimpleNamespace(guild_id=1, channel_id=essay['channel_id'],
                                                            message_id=essay['source_id']))
        self.assertEqual(self.store.rows('SELECT * FROM bc_publications ORDER BY key'), frozen)
        self.assertEqual(get_removal_operation(self.store, 1, operation['id'])['state'], 'pending')


if __name__ == '__main__':
    unittest.main()

"""Real discord.py command/view models with a fully mocked Discord transport.

No token, network, messages to real users, or production data are used.
"""
from datetime import datetime, timezone
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord
from discord.ext import commands

from bookclub.service import Service
from bookclub.store import ClubError, Store
from bookclub.ui import Club, MeetingView, PlanView, PlanModal, OfferView
from test_bookclub import ClubFixture, CONFIG


def not_found():
    return discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), {'code': 10003, 'message': 'Unknown resource'})


async def iterate(values):
    for item in values:
        yield item


class DiscordHarness:
    def __init__(self):
        self.seq = 1000
        self.bot = Mock(spec=commands.Bot)
        self.bot.user = SimpleNamespace(id=9999, bot=True)
        self.guild = Mock(spec=discord.Guild)
        self.guild.id, self.guild.owner_id = 1, 99
        self.guild.features = ['COMMUNITY']
        self.guild.me = SimpleNamespace(id=9999)
        self.channels, self.events, self.members = {}, {}, {}
        self.guild.scheduled_events = []
        self.guild.get_channel_or_thread.side_effect = lambda ident: self.channels.get(ident)
        self.guild.get_channel.side_effect = lambda ident: self.channels.get(ident)
        self.bot.get_guild.side_effect = lambda ident: self.guild if ident == 1 else None
        async def fetch_channel(ident):
            if ident not in self.channels:
                raise not_found()
            return self.channels[ident]
        async def fetch_event(ident):
            if ident not in self.events:
                raise not_found()
            return self.events[ident]
        async def fetch_events():
            return list(self.events.values())
        async def active_threads():
            return [c for c in self.channels.values() if isinstance(c, discord.Thread) and not c.archived]
        async def fetch_member(ident):
            if ident not in self.members:
                raise not_found()
            return self.members[ident]
        self.bot.fetch_channel = AsyncMock(side_effect=fetch_channel)
        self.guild.fetch_scheduled_event = AsyncMock(side_effect=fetch_event)
        self.guild.fetch_scheduled_events = AsyncMock(side_effect=fetch_events)
        self.guild.active_threads = AsyncMock(side_effect=active_threads)
        self.guild.fetch_member = AsyncMock(side_effect=fetch_member)
        self.guild.fetch_members.side_effect = lambda **_: iterate(list(self.members.values()))
        self.guild.get_role.side_effect = lambda ident: SimpleNamespace(id=ident) if ident == 22 else None
        for user in (1, 2, 3, 99):
            member = Mock(spec=discord.Member)
            member.id, member.bot, member.guild = user, False, self.guild
            member.display_name = f'Участник {user}'
            member.display_avatar = SimpleNamespace(url=f'https://cdn.discordapp.com/avatars/{user}/test.png')
            member.roles = [SimpleNamespace(id=22)] if user == 99 else []
            member.send = AsyncMock()
            self.members[user] = member
        for ident, kind in [(11, discord.TextChannel), (12, discord.TextChannel), (13, discord.ForumChannel), (14, discord.ForumChannel), (15, discord.VoiceChannel)]:
            self.channel(ident, kind)
        async def create_event(**fields):
            self.seq += 1
            event = self.event(self.seq, **fields)
            return event
        self.guild.create_scheduled_event = AsyncMock(side_effect=create_event)

    def channel(self, ident, kind=discord.Thread, parent_id=None, owner_id=9999, name='Тема'):
        channel = Mock(spec=kind)
        channel.id, channel.guild, channel.name = ident, self.guild, name
        channel.parent_id, channel.owner_id, channel.archived = parent_id, owner_id, False
        channel.created_at = datetime.fromtimestamp(2_000_000_001, timezone.utc)
        channel.archive_timestamp = None
        channel.flags = SimpleNamespace(pinned=False, require_tag=False)
        channel.available_tags, channel.applied_tags = [], []
        channel.parent = self.channels.get(parent_id)
        channel.jump_url = f'https://discord.com/channels/1/{ident}'
        channel.messages = {}
        channel.permissions_for.return_value = SimpleNamespace(**dict.fromkeys(('view_channel','send_messages','read_message_history','send_messages_in_threads','manage_threads','manage_webhooks','manage_channels','connect','create_events','manage_events'), True))
        async def fetch_message(message_id):
            if message_id not in channel.messages:
                raise not_found()
            return channel.messages[message_id]
        channel.fetch_message = AsyncMock(side_effect=fetch_message)
        def history(*, limit=100, before=None, after=None, oldest_first=False):
            rows = sorted(channel.messages.values(), key=lambda m: m.id, reverse=not oldest_first)
            rows = [m for m in rows if (before is None or m.id < before.id) and (after is None or m.id > after.id)]
            return iterate(rows[:limit] if limit is not None else rows)
        channel.history = history
        async def send(content, **kwargs):
            self.seq += 1
            return self.message(channel, self.seq, content, **kwargs)
        channel.send = AsyncMock(side_effect=send)
        async def edit(**kwargs):
            for key, value in kwargs.items():
                if key == 'pinned':
                    channel.flags.pinned = value
                else:
                    setattr(channel, key, value)
            return channel
        channel.edit = AsyncMock(side_effect=edit)
        if kind == discord.ForumChannel:
            channel.archived_threads.side_effect = lambda **_: iterate([c for c in self.channels.values() if c.parent_id == ident and c.archived])
            async def create_thread(name, content, **kwargs):
                self.seq += 1
                thread = self.channel(self.seq, parent_id=ident, name=name)
                thread.applied_tags = kwargs.get('applied_tags', [])
                message = self.message(thread, thread.id, content, **kwargs)
                return SimpleNamespace(thread=thread, message=message)
            channel.create_thread = AsyncMock(side_effect=create_thread)
        self.channels[ident] = channel
        if kind == discord.Thread and owner_id in self.members:
            starter = self.message(channel, ident, name)
            starter.author = self.members[owner_id]
        return channel

    def message(self, channel, ident, content, **kwargs):
        message = Mock(spec=discord.Message)
        message.id, message.channel, message.content = ident, channel, content
        message.author = self.bot.user
        message.guild, message.webhook_id, message.attachments = self.guild, None, []
        message.nonce = kwargs.get('nonce')
        message.pinned = False
        async def pin(**kwargs):
            message.pinned = True
        message.pin = AsyncMock(side_effect=pin)
        view = kwargs.get('view')
        message.components = [SimpleNamespace(to_dict=lambda value=value: value) for value in view.to_components()] if view else []
        message.jump_url = f'https://discord.com/channels/1/{channel.id}/{ident}'
        async def edit(**kwargs):
            if 'content' in kwargs:
                message.content = kwargs['content']
            if 'view' in kwargs:
                view = kwargs['view']
                message.components = [SimpleNamespace(to_dict=lambda value=value: value) for value in view.to_components()] if view else []
            return message
        message.edit = AsyncMock(side_effect=edit)
        async def delete():
            if ident not in channel.messages:
                raise not_found()
            del channel.messages[ident]
        message.delete = AsyncMock(side_effect=delete)
        channel.messages[ident] = message
        return message

    def event(self, ident, **fields):
        event = SimpleNamespace(id=ident, guild_id=1, guild=self.guild,
            name=fields['name'], start_time=fields['start_time'], end_time=fields.get('end_time'),
            channel_id=getattr(fields.get('channel'), 'id', 15), status=discord.EventStatus.scheduled,
            entity_type=discord.EntityType.voice, description=fields.get('description', ''), creator_id=self.bot.user.id)
        async def edit(**updates):
            for key, value in updates.items():
                setattr(event, key, value)
            return event
        async def cancel():
            event.status = discord.EventStatus.cancelled
            return event
        event.edit, event.cancel = AsyncMock(side_effect=edit), AsyncMock(side_effect=cancel)
        self.events[ident] = event
        self.guild.scheduled_events = list(self.events.values())
        return event

    def interaction(self, user=1):
        result = SimpleNamespace(id=54321, guild=self.guild, guild_id=1, user=self.members[user])
        result.response = SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), send_modal=AsyncMock(), is_done=lambda: True)
        result.followup = SimpleNamespace(send=AsyncMock())
        return result


class DiscordIntegrationTests(ClubFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.h = DiscordHarness()
        self.cog = Club(self.h.bot, self.store)
        self.service = self.cog.service
        m = self.meeting
        self.event = self.h.event(m['event_id'], name=m['name'], start_time=datetime.fromtimestamp(m['start'], timezone.utc), end_time=datetime.fromtimestamp(m['end'], timezone.utc))

    async def asyncSetUp(self):
        # SQLite intentionally uses short synchronous transactions. Windows
        # filesystem latency under asyncio debug is not a task timeout.
        import asyncio
        asyncio.get_running_loop().slow_callback_duration = 1.0

    async def test_disabled_publication_causes_no_messages(self):
        await self.service.tick(self.h.guild)
        self.h.channels[13].create_thread.assert_not_awaited()
        self.h.channels[11].send.assert_not_awaited()
        for member in self.h.members.values():
            member.send.assert_not_awaited()

    async def test_publication_repetition_creates_one_catalog_book_news_and_card(self):
        self.store.set_published(1)
        await self.service.refresh(self.h.guild)
        await self.service.refresh(self.h.guild)
        self.assertEqual(self.h.channels[13].create_thread.await_count, 2)
        self.assertEqual(self.h.channels[11].send.await_count, 2)
        root = self.store.publication(f'book:{self.book["id"]}')
        self.assertEqual(self.h.channels[root['channel_id']].send.await_count, 1)
        for channel in self.h.channels.values():
            for message in channel.messages.values():
                self.assertNotIn('topics', message.content)

    async def test_lost_send_ack_recovers_marker_without_duplicate(self):
        channel = self.h.channels[11]
        async def send_then_fail(content, **kwargs):
            self.h.message(channel, 8888, content)
            raise OSError('simulated lost acknowledgement')
        channel.send.side_effect = send_then_fail
        with self.assertRaises(OSError):
            await self.service.upsert(self.h.guild, 'news:1', 11, 'Вестник')
        pub = await self.service.upsert(self.h.guild, 'news:1', 11, 'Вестник')
        self.assertEqual(pub['message_id'], 8888)
        self.assertEqual(channel.send.await_count, 1)

    async def test_uncertain_absent_publication_does_not_blindly_retry(self):
        self.store.reserve_publication('news:1', 1, 11)
        with self.assertRaises(ClubError):
            await self.service.upsert(self.h.guild, 'news:1', 11, 'Вестник')
        self.h.channels[11].send.assert_not_awaited()

    async def test_paged_post_preserves_all_content_and_does_not_edit_on_every_tick(self):
        content = ['a' * 1700, 'b' * 1700, 'c' * 1700]
        root = await self.service.paged_post(self.h.guild, 'catalog:1', 13, 'Каталог', content)
        thread = self.h.channels[root['channel_id']]
        counts = {m.id: m.edit.await_count for m in thread.messages.values()}
        await self.service.paged_post(self.h.guild, 'catalog:1', 13, 'Каталог', content)
        self.assertEqual(self.h.channels[13].create_thread.await_count, 1)
        self.assertEqual(thread.send.await_count, 3)
        self.assertEqual(counts, {m.id: m.edit.await_count for m in thread.messages.values()})
        self.assertTrue(all(len(m.content) <= 2000 for m in thread.messages.values()))
        for page in content:
            self.assertTrue(any(page in m.content for m in thread.messages.values()))

    async def test_gateway_move_and_cancel_refresh_cards_and_invalidate_jobs(self):
        self.store.set_published(1)
        self.action('volunteer')
        await self.service.refresh(self.h.guild)
        old = self.jobs()
        self.event.start_time = datetime.fromtimestamp(self.meeting['start'] + 86400, timezone.utc)
        await self.cog.on_scheduled_event_update(None, self.event)
        moved = self.store.meeting(1, self.meeting['id'])
        self.assertEqual(moved['host_state'], 'pending')
        self.assertTrue(all(not self.store.claim_job(j['key']) for j in old))
        self.event.status = discord.EventStatus.cancelled
        await self.cog.on_scheduled_event_update(None, self.event)
        self.assertEqual(self.store.meeting(1, moved['id'])['status'], 'cancelled')
        card = self.store.publication(f'meeting:{moved["id"]}')
        self.assertIn('отменена', self.h.channels[card['channel_id']].messages[card['message_id']].content)

    async def test_api_move_immediately_before_dispatch_prevents_stale_reminder(self):
        self.store.set_published(1)
        job = self.jobs()[0]
        self.now = job['due']
        self.event.start_time = datetime.fromtimestamp(self.meeting['start'] + 86400, timezone.utc)
        await self.service.tick(self.h.guild)
        for member in self.h.members.values():
            member.send.assert_not_awaited()
        self.assertFalse(self.store.claim_job(job['key']))

    async def test_offer_and_reminders_reach_only_intended_people(self):
        self.store.set_published(1)
        self.action('offer', organizer=True, candidate=2)
        await self.service.tick(self.h.guild)
        self.h.members[2].send.assert_awaited_once()
        self.h.members[1].send.assert_not_awaited()
        self.h.members[3].send.assert_not_awaited()
        self.action('accept', actor=2)
        self.store.attendance(1, self.meeting['id'], 3, True)
        self.now = self.meeting['start'] - 1800
        await self.service.tick(self.h.guild)
        self.h.members[1].send.assert_awaited_once()
        self.assertEqual(self.h.members[2].send.await_count, 2)
        self.h.members[3].send.assert_not_awaited()
        for call in self.h.members[2].send.await_args_list:
            self.assertFalse(call.kwargs['allowed_mentions'].everyone)

    async def test_private_modal_rechecks_replacement_and_current_organizer_roles(self):
        self.action('volunteer')
        plan = self.save_plan(topics='PRIVATE CONTENT')
        modal = PlanModal(self.cog, self.store.meeting(1, self.meeting['id']), plan, 'edit')
        view = PlanView(self.cog, self.store.meeting(1, self.meeting['id']), plan, 1)
        self.action('replace', organizer=True)
        self.action('volunteer', actor=2)
        with self.assertRaises(ClubError):
            await modal.on_submit(self.h.interaction(1))
        with self.assertRaises(ClubError):
            await view.children[0].callback(self.h.interaction(1))
        # A previously authorized organizer loses their role before opening a form.
        self.h.members[3].roles = [SimpleNamespace(id=22)]
        current = self.store.plan(1, self.meeting['id'], 3, organizer=True)
        organizer_view = PlanView(self.cog, self.store.meeting(1, self.meeting['id']), current, 3)
        self.h.members[3].roles = []
        with self.assertRaises(ClubError):
            await organizer_view.children[0].callback(self.h.interaction(3))

    async def test_card_member_picker_checks_organizer_and_reading_participation(self):
        view = OfferView(self.cog, self.meeting, 99)
        selection = SimpleNamespace(values=[self.h.members[2]])
        with self.assertRaises(ClubError):
            await OfferView.select_member(view, self.h.interaction(1), selection)
        self.store.participant(1, self.book['id'], 2, joined=False)
        with self.assertRaises(ClubError):
            await OfferView.select_member(view, self.h.interaction(99), selection)
        self.store.participant(1, self.book['id'], 2)
        await OfferView.select_member(view, self.h.interaction(99), selection)
        self.assertEqual(self.store.meeting(1, self.meeting['id'])['host_id'], 2)
        self.assertEqual(self.store.meeting(1, self.meeting['id'])['host_state'], 'pending')

    async def test_essay_creation_rename_correction_delete_and_offline_scan(self):
        thread = self.h.channel(701, parent_id=14, owner_id=1, name='Книга · Мой текст')
        await self.cog.on_thread_create(thread)
        self.assertEqual(self.store.one('SELECT book_id FROM bc_essays WHERE source_id=701')['book_id'], self.book['id'])
        thread.name = 'Полностью другое название'
        await self.cog.on_raw_thread_update(SimpleNamespace(guild_id=1, thread_id=701))
        self.assertEqual(self.store.one('SELECT book_id FROM bc_essays WHERE source_id=701')['book_id'], self.book['id'])
        second = self.store.create_book(1, 'Другая книга', 'Автор', '', 'second')
        await self.service.register_thread(thread, book_id=second['id'], correct=True)
        await self.cog.on_raw_thread_delete(SimpleNamespace(guild_id=1, thread_id=701))
        self.assertTrue(self.store.one('SELECT deleted FROM bc_essays WHERE source_id=701')['deleted'])
        del self.h.channels[701]
        offline = self.h.channel(702, parent_id=14, owner_id=2, name='Книга · За время перерыва')
        offline.archived = True
        await self.service.scan_essays(self.h.guild)
        self.assertEqual(self.store.one('SELECT author_id FROM bc_essays WHERE source_id=702')['author_id'], 2)

    async def test_deleted_event_reconciles_after_restart(self):
        del self.h.events[self.event.id]
        await self.service.reconcile(self.h.guild)
        self.assertEqual(self.store.meeting(1, self.meeting['id'])['status'], 'cancelled')
        self.assertFalse(self.jobs())

    async def test_completed_gateway_is_preserved_when_rest_has_removed_event(self):
        self.action('volunteer')
        self.event.status = discord.EventStatus.active
        await self.cog.on_scheduled_event_update(None, self.event)
        self.event.status = discord.EventStatus.completed
        del self.h.events[self.event.id]
        await self.cog.on_scheduled_event_update(None, self.event)
        self.assertEqual(self.store.meeting(1, self.meeting['id'])['status'], 'completed')
        self.assertEqual(self.store.one('SELECT user_id FROM bc_host_history')['user_id'], 1)

    async def test_recreate_deleted_paged_forum_post_after_restart(self):
        root = await self.service.paged_post(self.h.guild, 'catalog:1', 13, 'Каталог', ['a' * 1700, 'b' * 1700])
        del self.h.channels[root['channel_id']]
        with self.store.tx() as db:
            db.execute('UPDATE bc_publications SET content_hash=NULL')
        replacement = await self.service.paged_post(self.h.guild, 'catalog:1', 13, 'Каталог', ['a' * 1700, 'b' * 1700])
        self.assertNotEqual(root['channel_id'], replacement['channel_id'])
        self.assertEqual(self.h.channels[13].create_thread.await_count, 2)

    async def test_essay_reminders_exclude_nonparticipants_and_already_published(self):
        self.store.set_published(1)
        deadline = self.now + 2 * 86400
        self.essay_event(deadline)
        self.store.register_essay(1, self.book['id'], 800, 800, 1, 'Готово', 'url')
        job = next(j for j in self.jobs(self.book) if j['kind'] == 'essay')
        self.now = job['due']
        self.assertTrue(self.store.claim_job(job['key']))
        await self.service.deliver(self.h.guild, job)
        self.h.members[1].send.assert_not_awaited()
        self.h.members[2].send.assert_awaited_once()
        self.h.members[3].send.assert_awaited_once()
        self.h.members[99].send.assert_not_awaited()

    async def test_missing_permissions_do_not_turn_into_false_cancellation(self):
        self.h.guild.fetch_scheduled_event.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'missing permissions')
        with self.assertRaises(discord.Forbidden):
            await self.service.sync_one(self.h.guild, self.meeting)
        self.assertEqual(self.store.meeting(1, self.meeting['id'])['status'], 'scheduled')

    async def test_main_entry_hook_registers_club_without_discord_login(self):
        from test_persistence import Shuffle
        config = self.path.parent / 'bookclub.json'
        config.write_text(json.dumps({'guilds': {'1': CONFIG}}), encoding='utf-8')
        async with commands.Bot(command_prefix='!', intents=discord.Intents.default()) as bot:
            with patch.object(Shuffle, 'bot', bot), patch.object(Shuffle, 'VOICE_STATS_DB_FILE', str(self.path)), patch.dict(os.environ, BOOKCLUB_ENABLED='true', BOOKCLUB_CONFIG_FILE=str(config)):
                await Shuffle.setup_hook()
                self.assertIsNotNone(bot.get_cog('Club'))
                self.assertIsNotNone(bot.tree.get_command('club'))
                await bot.remove_cog('Club')

    async def test_legacy_scheduled_event_cancellation_cancels_old_task(self):
        from test_persistence import Shuffle
        key = Shuffle.event_occurrence_key(self.event.id, self.event.start_time)
        task = Mock()
        before = SimpleNamespace(id=self.event.id, start_time=self.event.start_time)
        self.event.status = discord.EventStatus.cancelled
        with patch.dict(Shuffle.scheduled_event_tasks, {key: task}, clear=True):
            await Shuffle.on_scheduled_event_update(before, self.event)
            task.cancel.assert_called_once()
            self.assertNotIn(key, Shuffle.scheduled_event_tasks)

    async def test_event_create_crash_recovers_by_marker(self):
        m = self.store.draft_meeting(1, self.book['id'], 'Новая', '2', '9', 'new')
        original = self.service.sync
        self.service.sync = Mock(side_effect=OSError('crash before SQLite bind'))
        with self.assertRaises(OSError):
            await self.service.create_event(self.h.guild, m, self.now + 3600, self.now + 7200)
        self.service.sync = original
        await self.service.reconcile(self.h.guild)
        await self.service.reconcile(self.h.guild)
        self.assertIsNotNone(self.store.meeting(1, m['id'])['event_id'])
        self.h.guild.create_scheduled_event.assert_awaited_once()

    async def test_real_sdk_registration_autocomplete_and_persistent_views(self):
        async with commands.Bot(command_prefix='!', intents=discord.Intents.default()) as bot:
            bot.fetch_channel = self.h.bot.fetch_channel
            cog = Club(bot, self.store)
            await bot.add_cog(cog)
            payload = bot.tree.get_command('club').to_dict(bot.tree)
            self.assertLessEqual(len(payload['options']), 25)
            self.assertEqual(len(payload['options']), len(Club.club.commands) + 1)
            for option in payload['options']:
                for parameter in option.get('options', []):
                    if parameter['name'] in ('book', 'meeting', 'event'):
                        self.assertTrue(parameter['autocomplete'])
            choices = await cog.book_autocomplete(self.h.interaction(), 'Кни')
            self.assertEqual(choices[0].value, self.book['id'])
            view = MeetingView(cog, self.meeting)
            self.assertTrue(view.is_persistent())
            self.assertTrue(all(len(c.custom_id) <= 100 for c in view.children))
            await bot.remove_cog('Club')


if __name__ == '__main__':
    unittest.main()

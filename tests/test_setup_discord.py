"""Provisioning regressions with disposable SQLite and a simulated Discord guild.

Channels, permission overwrites, and transport failures are local test doubles;
no Discord token or connection is used.
"""
import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import discord

from bookclub.service import Service
from bookclub.store import ClubError, Store
from test_bookclub import CONFIG
from test_bookclub_discord import DiscordHarness, not_found


PURPOSES = ('news', 'chat', 'books', 'essays', 'voice')
KINDS = {'category': discord.CategoryChannel, 'news': discord.TextChannel,
         'chat': discord.TextChannel, 'books': discord.ForumChannel,
         'essays': discord.ForumChannel, 'voice': discord.VoiceChannel}
NAMES = {'category': 'Книжный клуб', 'news': 'вестник', 'chat': 'площадь',
         'books': 'город', 'essays': 'либрариум', 'voice': 'Ротонда'}


def forbidden():
    return discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'),
                             {'code': 50013, 'message': 'Missing Permissions'})


def copy_overwrite(overwrite):
    return discord.PermissionOverwrite.from_pair(*overwrite.pair())


class SetupHarness(DiscordHarness):
    def __init__(self):
        super().__init__()
        self.channels.clear()
        self.denied_channels = set()
        self.default_role = Mock(spec=discord.Role)
        self.default_role.id, self.default_role.guild = 1, self.guild
        self.default_role.permissions = discord.Permissions.none()
        self.organizer_role = Mock(spec=discord.Role)
        self.organizer_role.id, self.organizer_role.guild = 22, self.guild
        self.organizer_role.permissions = discord.Permissions.none()
        self.guild.default_role = self.default_role
        self.bot_member = Mock(spec=discord.Member)
        self.bot_member.id, self.bot_member.bot = self.bot.user.id, True
        self.bot_member.guild, self.bot_member.roles = self.guild, [self.default_role]
        self.bot_member.guild_permissions = discord.Permissions.all()
        self.bot_member.guild_permissions.administrator = False
        self.members[self.bot_member.id] = self.bot_member
        self.guild.me = self.bot_member
        self.guild.channels = []
        self.guild.categories = []
        for member in self.members.values():
            if member is not self.bot_member:
                member.guild_permissions = discord.Permissions.none()
        self.guild.get_member.side_effect = self.members.get
        self.guild.get_role.side_effect = {1: self.default_role, 22: self.organizer_role}.get

        async def fetch_channel(ident):
            if ident in self.denied_channels:
                raise forbidden()
            if ident not in self.channels:
                raise not_found()
            return self.channels[ident]

        async def fetch_channels():
            return [channel for ident, channel in self.channels.items()
                    if ident not in self.denied_channels]

        self.bot.fetch_channel = AsyncMock(side_effect=fetch_channel)
        self.guild.fetch_channel = AsyncMock(side_effect=fetch_channel)
        self.guild.fetch_channels = AsyncMock(side_effect=fetch_channels)
        for method, kind in [('create_category', discord.CategoryChannel),
                             ('create_text_channel', discord.TextChannel),
                             ('create_forum', discord.ForumChannel),
                             ('create_voice_channel', discord.VoiceChannel)]:
            async def create(name, _kind=kind, **kwargs):
                self.seq += 1
                category = kwargs.get('category')
                overwrites = kwargs.get('overwrites')
                if overwrites is None:
                    overwrites = category.overwrites if category is not None else {}
                return self.make_channel(self.seq, _kind, name=name, category=category,
                                         topic=kwargs.get('topic'), overwrites=overwrites)
            setattr(self.guild, method, AsyncMock(side_effect=create))
        self.guild.create_category_channel = self.guild.create_category

    def make_channel(self, ident, kind, *, name='Существующий канал', category=None,
                     topic=None, overwrites=None):
        channel = super().channel(ident, kind, name=name)
        channel.type = {discord.CategoryChannel: discord.ChannelType.category,
                        discord.TextChannel: discord.ChannelType.text,
                        discord.ForumChannel: discord.ChannelType.forum,
                        discord.VoiceChannel: discord.ChannelType.voice}[kind]
        channel.mention = f'<#{ident}>'
        channel.category = category
        channel.category_id = category.id if category is not None else None
        channel.topic = topic
        channel.nsfw = False
        channel.overwrites = {target: copy_overwrite(value)
                              for target, value in (overwrites or {}).items()}
        channel.overwrites_for.side_effect = lambda target: copy_overwrite(
            channel.overwrites.get(target, discord.PermissionOverwrite()))

        def permissions_for(member):
            permissions = discord.Permissions(member.guild_permissions.value)
            if permissions.administrator:
                return discord.Permissions.all()
            everyone = channel.overwrites.get(self.default_role)
            if everyone is not None:
                allow, deny = everyone.pair()
                permissions.handle_overwrite(allow.value, deny.value)
            role_allow = role_deny = 0
            for role in member.roles:
                if role is self.default_role:
                    continue
                overwrite = channel.overwrites.get(role)
                if overwrite is not None:
                    allow, deny = overwrite.pair()
                    role_allow |= allow.value
                    role_deny |= deny.value
            permissions.handle_overwrite(role_allow, role_deny)
            specific = channel.overwrites.get(member)
            if specific is not None:
                allow, deny = specific.pair()
                permissions.handle_overwrite(allow.value, deny.value)
            return permissions

        channel.permissions_for.side_effect = permissions_for

        async def set_permissions(target, *, overwrite=None, reason=None, **permissions):
            if overwrite is None:
                overwrite = discord.PermissionOverwrite(**permissions)
            channel.overwrites[target] = copy_overwrite(overwrite)

        channel.set_permissions = AsyncMock(side_effect=set_permissions)

        async def edit(**changes):
            if 'category' in changes:
                selected = changes['category']
                channel.category = selected
                channel.category_id = selected.id if selected is not None else None
                if changes.get('sync_permissions') and selected is not None:
                    channel.overwrites = {target: copy_overwrite(value)
                                          for target, value in selected.overwrites.items()}
            if 'overwrites' in changes:
                channel.overwrites = {target: copy_overwrite(value)
                                      for target, value in changes['overwrites'].items()}
            for key in ('name', 'topic', 'nsfw'):
                if key in changes:
                    setattr(channel, key, changes[key])
            if 'available_tags' in changes:
                tags = changes['available_tags']
                for tag in tags:
                    if not tag.id:
                        self.seq += 1
                        tag.id = self.seq
                channel.available_tags = tags
            return channel

        channel.edit = AsyncMock(side_effect=edit)
        self.guild.channels = list(self.channels.values())
        self.guild.categories = [c for c in self.channels.values()
                                 if isinstance(c, discord.CategoryChannel)]
        if category is not None:
            category.channels = [c for c in self.channels.values()
                                 if c.category_id == category.id]
        if kind is discord.CategoryChannel:
            channel.channels = []
        return channel

    def create_count(self):
        return sum(getattr(self.guild, method).await_count for method in
                   ('create_category', 'create_text_channel', 'create_forum', 'create_voice_channel'))

    def clear_writes(self):
        for method in ('create_category', 'create_text_channel', 'create_forum', 'create_voice_channel'):
            getattr(self.guild, method).reset_mock()
        for channel in self.channels.values():
            channel.edit.reset_mock()
            channel.set_permissions.reset_mock()


class SetupDiscordTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'club.sqlite3'
        self.store = Store(self.path, clock=lambda: 2_000_000_000)
        self.h = SetupHarness()
        self.make_service()

    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 1.0

    def make_service(self, guild_ids=None):
        self.service = Service(self.h.bot, self.store, guild_ids)
        self.service.refresh = AsyncMock()
        self.service.essay_webhook = AsyncMock()

    async def setup(self, actor_id=99, **kwargs):
        return await self.service.setup_server(self.h.guild, actor_id, **kwargs)

    async def failed_setup(self, **kwargs):
        """Either an actionable report or a surfaced transport/domain error is valid."""
        try:
            return await self.setup(**kwargs)
        except (ClubError, OSError, discord.HTTPException) as exc:
            return [str(exc)]

    def database_snapshot(self):
        with closing(sqlite3.connect(self.path)) as db:
            return '\n'.join(db.iterdump())

    def assert_no_writes(self):
        self.assertEqual(self.h.create_count(), 0)
        for channel in self.h.channels.values():
            channel.edit.assert_not_awaited()
            channel.set_permissions.assert_not_awaited()
        self.service.refresh.assert_not_awaited()
        self.service.essay_webhook.assert_not_awaited()

    def assert_ready(self):
        settings = self.store.settings(1)
        self.assertEqual(len({settings[key] for key in PURPOSES}), 5)
        for key in PURPOSES:
            self.assertIsInstance(self.h.channels[settings[key]], KINDS[key])
            binding = self.store.setup_resource(1, key)
            self.assertEqual(binding['channel_id'], settings[key])
            self.assertEqual(binding['state'], 'ready')
        self.assertTrue(settings['published'])
        return settings

    def existing_configuration(self, category=None):
        for purpose in PURPOSES:
            self.h.make_channel(CONFIG[purpose], KINDS[purpose], name=f'Мой {purpose}',
                                category=category, overwrites=category.overwrites if category is not None else {})
        self.store.configure(1, CONFIG)

    def private_category(self, ident=500):
        return self.h.make_channel(ident, discord.CategoryChannel, name='Закрытый клуб', overwrites={
            self.h.default_role: discord.PermissionOverwrite(view_channel=False, send_messages=False),
            self.h.members[2]: discord.PermissionOverwrite(view_channel=True, send_messages=False),
            self.h.bot_member: discord.PermissionOverwrite(view_channel=True, read_message_history=True),
        })

    @staticmethod
    def acl_snapshot(channel):
        return {target.id: tuple(permission.value for permission in overwrite.pair())
                for target, overwrite in channel.overwrites.items()}

    async def test_first_run_creates_complete_structure_and_publishes(self):
        result = await self.setup()
        self.assertIsInstance(result, list)
        settings = self.assert_ready()
        self.assertIn(99, settings['organizers'])
        self.assertEqual(self.h.create_count(), 6)
        category = self.h.channels[self.store.setup_resource(1, 'category')['channel_id']]
        self.assertEqual(category.name, NAMES['category'])
        for purpose in PURPOSES:
            channel = self.h.channels[settings[purpose]]
            self.assertEqual(channel.name, NAMES[purpose])
            self.assertEqual(channel.category_id, category.id)
        self.service.refresh.assert_awaited()
        self.service.essay_webhook.assert_awaited()

    async def test_concurrent_and_repeated_setup_do_not_duplicate_channels(self):
        await asyncio.gather(self.setup(), self.setup())
        first = self.assert_ready()
        await self.setup()
        self.assertEqual(self.h.create_count(), 6)
        self.assertEqual(first, self.assert_ready())

    async def test_restart_preserves_bindings_and_manual_names(self):
        await self.setup()
        first = self.assert_ready()
        self.h.channels[first['books']].name = 'Мои книги'
        self.store = Store(self.path, clock=lambda: 2_000_000_100)
        self.make_service()
        self.h.clear_writes()
        await self.setup()
        self.assertEqual(first, self.assert_ready())
        self.assertEqual(self.h.channels[first['books']].name, 'Мои книги')
        self.assertEqual(self.h.create_count(), 0)

    async def test_deleted_single_channel_is_replaced_without_touching_other_ids(self):
        await self.setup()
        first = self.assert_ready()
        del self.h.channels[first['essays']]
        self.h.clear_writes()
        await self.setup()
        current = self.assert_ready()
        self.assertNotEqual(first['essays'], current['essays'])
        self.assertEqual(self.h.create_count(), 1)
        for purpose in ('news', 'chat', 'books', 'voice'):
            self.assertEqual(first[purpose], current[purpose])

    async def test_forbidden_saved_channel_is_not_treated_as_deleted(self):
        await self.setup()
        first = self.assert_ready()
        self.h.denied_channels.add(first['books'])
        self.h.clear_writes()
        result = await self.failed_setup()
        self.assertTrue(result)
        self.assertEqual(self.h.create_count(), 0)
        self.assertEqual(self.store.settings(1)['books'], first['books'])

    async def test_identical_existing_names_are_not_adopted(self):
        unrelated = {purpose: self.h.make_channel(100 + index, KINDS[purpose], name=NAMES[purpose])
                     for index, purpose in enumerate(KINDS)}
        await self.setup()
        settings = self.assert_ready()
        self.assertEqual(self.h.create_count(), 6)
        for purpose, channel in unrelated.items():
            self.assertNotEqual(self.store.setup_resource(1, purpose)['channel_id'], channel.id)
            channel.edit.assert_not_awaited()
            channel.set_permissions.assert_not_awaited()
        self.assertNotIn(settings['books'], {channel.id for channel in unrelated.values()})

    async def test_wrong_type_saved_channel_is_reported_without_replacement(self):
        self.existing_configuration()
        self.h.make_channel(13, discord.TextChannel, name='Здесь чужая переписка')
        result = await self.failed_setup()
        self.assertTrue(result)
        self.assertEqual(self.store.settings(1)['books'], 13)
        self.h.guild.create_forum.assert_not_awaited()
        self.h.channels[13].edit.assert_not_awaited()

    async def test_private_human_overwrites_survive_bot_permission_repair(self):
        self.existing_configuration()
        channel = self.h.channels[13]
        role = Mock(spec=discord.Role)
        role.id, role.guild = 200, self.h.guild
        channel.overwrites = {
            self.h.default_role: discord.PermissionOverwrite(view_channel=False, send_messages=False),
            role: discord.PermissionOverwrite(view_channel=True, manage_messages=False),
            self.h.members[2]: discord.PermissionOverwrite(view_channel=False, send_messages=True),
            self.h.bot_member: discord.PermissionOverwrite(view_channel=False, manage_threads=False,
                                                          mention_everyone=False),
        }
        humans = {target: value.pair() for target, value in channel.overwrites.items()
                  if target is not self.h.bot_member}
        await self.setup(repair_permissions=True)
        self.assertEqual({target: value.pair() for target, value in channel.overwrites.items()
                          if target is not self.h.bot_member}, humans)
        self.assertTrue(channel.permissions_for(self.h.bot_member).view_channel)
        self.assertTrue(channel.permissions_for(self.h.bot_member).manage_threads)
        self.assertIs(channel.overwrites[self.h.bot_member].mention_everyone, False)
        channel.set_permissions.assert_awaited()
        self.assertTrue(all(call.args[0] is self.h.bot_member
                            for call in channel.set_permissions.await_args_list))

    async def test_repaired_acl_publishes_before_gateway_updates_cached_channel(self):
        self.existing_configuration()
        cached = self.h.channels[CONFIG['books']]
        cached.overwrites[self.h.bot_member] = discord.PermissionOverwrite(manage_threads=False)
        fresh = self.h.make_channel(cached.id, discord.ForumChannel, name=cached.name,
                                    overwrites=cached.overwrites)
        # Cache lookups continue returning the pre-update object. Only a new
        # REST listing sees the successful permission change until Gateway fires.
        self.h.channels[cached.id] = cached
        transport_channels = dict(self.h.channels)

        async def set_permissions_without_gateway(target, *, overwrite, **kwargs):
            fresh.overwrites[target] = copy_overwrite(overwrite)
            transport_channels[cached.id] = fresh

        async def fetch_current_channels():
            return list(transport_channels.values())

        cached.set_permissions.side_effect = set_permissions_without_gateway
        self.h.guild.fetch_channels.side_effect = fetch_current_channels
        self.h.bot.fetch_channel.side_effect = lambda ident: transport_channels[ident]
        await self.setup(repair_permissions=True)
        cached.set_permissions.assert_awaited_once()
        self.assertFalse(cached.permissions_for(self.h.bot_member).manage_threads)
        self.assertTrue(fresh.permissions_for(self.h.bot_member).manage_threads)
        self.assertIs(self.h.guild.get_channel_or_thread(cached.id), cached)
        self.assertTrue(self.store.settings(1)['published'])
        self.service.refresh.assert_awaited_once()

    async def test_check_only_on_empty_server_makes_no_database_or_discord_writes(self):
        before = self.database_snapshot()
        result = await self.setup(check_only=True)
        self.assertTrue(result)
        self.assertEqual(self.database_snapshot(), before)
        self.assert_no_writes()

    async def test_check_only_reports_deleted_channel_without_mutating_ready_setup(self):
        await self.setup()
        settings = self.assert_ready()
        del self.h.channels[settings['chat']]
        self.h.clear_writes()
        self.service.refresh.reset_mock()
        self.service.essay_webhook.reset_mock()
        before = self.database_snapshot()
        self.assertTrue(await self.setup(check_only=True))
        self.assertEqual(self.database_snapshot(), before)
        self.assert_no_writes()

    async def test_check_only_reports_required_forum_tag_without_writes(self):
        self.existing_configuration()
        self.h.channels[13].flags.require_tag = True
        before = self.database_snapshot()
        report = await self.setup(check_only=True)
        self.assertIn('тег', '\n'.join(report).lower())
        self.assertEqual(self.database_snapshot(), before)
        self.assertTrue(self.h.channels[13].flags.require_tag)
        self.assert_no_writes()

    async def test_missing_forum_uses_survivors_private_category_without_mutating_its_acl(self):
        category = self.private_category()
        self.existing_configuration(category)
        original_acl = self.acl_snapshot(category)
        del self.h.channels[CONFIG['books']]
        await self.setup(repair_permissions=True)
        settings = self.assert_ready()
        forum = self.h.channels[settings['books']]
        self.assertNotEqual(forum.id, CONFIG['books'])
        self.assertEqual(forum.category_id, category.id)
        self.assertIs(forum.overwrites[self.h.default_role].view_channel, False)
        self.assertEqual(forum.overwrites[self.h.members[2]].pair(),
                         category.overwrites[self.h.members[2]].pair())
        self.assertEqual(self.acl_snapshot(category), original_acl)
        category.edit.assert_not_awaited()
        category.set_permissions.assert_not_awaited()
        self.h.guild.create_category.assert_not_awaited()
        self.assertEqual(self.h.create_count(), 1)

    async def test_missing_configured_child_needs_explicit_category_when_survivors_are_unparented(self):
        self.existing_configuration()
        category = self.private_category()
        original_acl = self.acl_snapshot(category)
        del self.h.channels[CONFIG['essays']]
        before = self.database_snapshot()
        report = await self.setup()
        self.assertIn('category', '\n'.join(report).lower())
        self.assertEqual(self.database_snapshot(), before)
        self.assert_no_writes()
        await self.setup(category=category)
        settings = self.assert_ready()
        forum = self.h.channels[settings['essays']]
        self.assertEqual(forum.category_id, category.id)
        self.assertIs(forum.overwrites[self.h.default_role].view_channel, False)
        self.assertEqual(self.acl_snapshot(category), original_acl)
        for purpose in ('news', 'chat', 'books', 'voice'):
            self.assertEqual(settings[purpose], CONFIG[purpose])
            self.assertIsNone(self.h.channels[settings[purpose]].category_id)
        self.h.guild.create_category.assert_not_awaited()
        self.assertEqual(self.h.create_count(), 1)

    async def test_deleted_managed_category_requires_replacement_and_preserves_orphan_acls(self):
        await self.setup()
        settings = self.assert_ready()
        category_id = self.store.setup_resource(1, 'category')['channel_id']
        del self.h.channels[category_id]
        original_acls = {}
        for purpose in PURPOSES:
            channel = self.h.channels[settings[purpose]]
            channel.category, channel.category_id = None, None
            channel.overwrites[self.h.default_role].view_channel = False
            channel.overwrites[self.h.members[1]] = discord.PermissionOverwrite(
                view_channel=True, mention_everyone=False)
            original_acls[channel.id] = self.acl_snapshot(channel)
        replacement = self.private_category(501)
        replacement_acl = self.acl_snapshot(replacement)
        self.h.clear_writes()
        self.service.refresh.reset_mock()
        self.service.essay_webhook.reset_mock()
        before = self.database_snapshot()
        report = await self.setup()
        self.assertIn('category', '\n'.join(report).lower())
        self.assertEqual(self.database_snapshot(), before)
        self.assert_no_writes()
        await self.setup(category=replacement)
        current = self.assert_ready()
        self.assertEqual(self.store.setup_resource(1, 'category')['channel_id'], replacement.id)
        self.assertFalse(self.store.setup_resource(1, 'category')['managed'])
        for purpose in PURPOSES:
            self.assertEqual(current[purpose], settings[purpose])
            channel = self.h.channels[current[purpose]]
            self.assertEqual(channel.category_id, replacement.id)
            self.assertEqual(self.acl_snapshot(channel), original_acls[channel.id])
            moves = [call for call in channel.edit.await_args_list if 'category' in call.kwargs]
            self.assertEqual(len(moves), 1)
            self.assertIs(moves[0].kwargs.get('sync_permissions'), False)
        self.assertEqual(self.acl_snapshot(replacement), replacement_acl)
        self.assertEqual(self.h.create_count(), 0)

    async def test_no_community_stops_before_any_changes(self):
        self.h.guild.features = []
        before = self.database_snapshot()
        self.assertTrue(await self.failed_setup())
        self.assertEqual(self.database_snapshot(), before)
        self.assert_no_writes()

    async def test_missing_manage_channels_stops_before_partial_creation(self):
        self.h.bot_member.guild_permissions.manage_channels = False
        before = self.database_snapshot()
        self.assertTrue(await self.failed_setup())
        self.assertEqual(self.database_snapshot(), before)
        self.assert_no_writes()

    async def test_fresh_setup_does_not_require_manage_roles_for_create_overwrites(self):
        self.h.bot_member.guild_permissions.manage_roles = False
        await self.setup()
        self.assert_ready()
        for channel in self.h.channels.values():
            channel.set_permissions.assert_not_awaited()

    async def test_regular_member_cannot_bootstrap(self):
        before = self.database_snapshot()
        with self.assertRaises(ClubError):
            await self.setup(actor_id=1)
        self.assertEqual(self.database_snapshot(), before)
        self.assert_no_writes()

    async def test_manage_guild_can_bootstrap_and_becomes_organizer(self):
        self.h.members[2].guild_permissions.manage_guild = True
        await self.setup(actor_id=2)
        self.assertIn(2, self.assert_ready()['organizers'])

    async def test_allowlist_exclusion_blocks_even_server_owner(self):
        self.make_service(guild_ids={2})
        before = self.database_snapshot()
        with self.assertRaises(ClubError):
            await self.setup()
        self.assertEqual(self.database_snapshot(), before)
        self.assert_no_writes()

    async def test_departed_member_and_bot_cannot_bootstrap(self):
        for actor_id in (123456, self.h.bot_member.id):
            with self.subTest(actor_id=actor_id):
                with self.assertRaises(ClubError):
                    await self.setup(actor_id=actor_id)
        self.assert_no_writes()

    async def test_setup_actor_rejects_private_context(self):
        with self.assertRaises(ClubError):
            await self.service.setup_actor(None, 99)
        self.assert_no_writes()

    async def test_lost_create_ack_recovers_same_category_after_restart(self):
        original_create = self.h.guild.create_category.side_effect

        async def created_without_ack(name, **kwargs):
            await original_create(name, **kwargs)
            raise OSError('The server created the category but its response was lost')

        self.h.guild.create_category.side_effect = created_without_ack
        await self.failed_setup()
        created, = [channel for channel in self.h.channels.values()
                    if isinstance(channel, discord.CategoryChannel)]
        self.store = Store(self.path, clock=lambda: 2_000_000_001)
        self.make_service()
        self.h.guild.create_category.side_effect = original_create
        await self.setup()
        self.assert_ready()
        self.assertEqual(self.h.guild.create_category.await_count, 1)
        self.assertEqual(self.store.setup_resource(1, 'category')['channel_id'], created.id)
        news = self.h.channels[self.store.settings(1)['news']]
        self.assertIs(news.overwrites[self.h.default_role].send_messages, False)

    async def test_uncertain_missing_create_waits_for_explicit_retry(self):
        self.store.reserve_setup_resource(1, 'category')
        before = self.store.setup_resource(1, 'category')
        self.assertTrue(await self.failed_setup())
        self.assertEqual(self.h.create_count(), 0)
        self.assertEqual(self.store.setup_resource(1, 'category'), before)
        await self.setup(retry_missing=True)
        self.assert_ready()
        self.assertEqual(self.h.guild.create_category.await_count, 1)

    async def test_lost_forum_ack_recovers_topic_marker_even_after_rename(self):
        original_create = self.h.guild.create_forum.side_effect
        lost_forum = []

        async def created_without_ack(name, **kwargs):
            forum = await original_create(name, **kwargs)
            self.assertTrue(forum.topic)
            forum.name = 'Администратор уже переименовал форум'
            lost_forum.append(forum)
            raise OSError('The forum was created but the response was lost')

        self.h.guild.create_forum.side_effect = created_without_ack
        await self.failed_setup()
        self.assertEqual(len(lost_forum), 1)
        self.store = Store(self.path, clock=lambda: 2_000_000_001)
        self.make_service()
        self.h.guild.create_forum.side_effect = original_create
        await self.setup()
        settings = self.assert_ready()
        self.assertIn(lost_forum[0].id, (settings['books'], settings['essays']))
        self.assertEqual(self.h.guild.create_forum.await_count, 2)

    async def test_failed_friendly_rename_resumes_existing_channel(self):
        original_create = self.h.guild.create_category.side_effect

        async def create_with_failed_rename(name, **kwargs):
            category = await original_create(name, **kwargs)
            category.edit.side_effect = forbidden()
            return category

        self.h.guild.create_category.side_effect = create_with_failed_rename
        await self.failed_setup()
        binding = self.store.setup_resource(1, 'category')
        self.assertIsNotNone(binding['channel_id'])
        category = self.h.channels[binding['channel_id']]

        async def repair_rename(**kwargs):
            category.name = kwargs.get('name', category.name)
            return category

        category.edit.side_effect = repair_rename
        self.h.guild.create_category.side_effect = original_create
        await self.setup()
        self.assert_ready()
        self.assertEqual(category.name, NAMES['category'])
        self.assertEqual(self.h.guild.create_category.await_count, 1)
        news = self.h.channels[self.store.settings(1)['news']]
        self.assertIs(news.overwrites[self.h.default_role].send_messages, False)

    async def test_complete_manual_configuration_is_adopted_without_channel_changes(self):
        self.existing_configuration()
        original = {ident: (channel.name, channel.category_id) for ident, channel in self.h.channels.items()}
        await self.setup()
        settings = self.assert_ready()
        for purpose in PURPOSES:
            self.assertEqual(settings[purpose], CONFIG[purpose])
            self.assertFalse(self.store.setup_resource(1, purpose)['managed'])
        self.assertEqual(original, {ident: (self.h.channels[ident].name, self.h.channels[ident].category_id)
                                    for ident in original})
        self.h.guild.create_text_channel.assert_not_awaited()
        self.h.guild.create_forum.assert_not_awaited()
        self.h.guild.create_voice_channel.assert_not_awaited()
        self.h.guild.create_category.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()

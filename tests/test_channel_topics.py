"""Channel topic cleanup preserves durable identity, custom text and permissions."""
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord

from bookclub.provision import DESCRIPTIONS, OLD_DESCRIPTIONS, Provisioner
from bookclub.service import Service
from bookclub.store import Store
from test_setup_discord import SetupHarness, forbidden


TOPICS = ('news', 'chat', 'books', 'essays')


class ChannelTopicTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'club.sqlite3'
        self.store = Store(self.path)
        self.h = SetupHarness()
        self.make_service()

    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 1.0

    def make_service(self):
        self.service = Service(self.h.bot, self.store)
        self.service.refresh = AsyncMock()
        self.service.essay_webhook = AsyncMock()

    async def setup(self, **kwargs):
        return await self.service.setup_server(self.h.guild, 99, **kwargs)

    def channel(self, purpose='chat'):
        return self.h.channels[self.store.settings(1)[purpose]]

    def marker(self, purpose='chat'):
        return Provisioner.marker(self.store.setup_resource(1, purpose))

    @staticmethod
    def acl(channel):
        return {target.id: tuple(value.value for value in overwrite.pair())
                for target, overwrite in channel.overwrites.items()}

    async def test_new_channels_finish_with_human_descriptions_only(self):
        await self.setup()
        for purpose in TOPICS:
            with self.subTest(purpose=purpose):
                self.assertEqual(self.channel(purpose).topic, DESCRIPTIONS[purpose])
                self.assertNotIn('bookclub:', self.channel(purpose).topic)
        self.assertEqual(self.h.create_count(), 6)

    async def test_legacy_ready_channels_clean_automatically_by_saved_id(self):
        await self.setup()
        settings = self.store.settings(1)
        for purpose in TOPICS:
            channel = self.channel(purpose)
            channel.topic = DESCRIPTIONS[purpose] + '\n' + self.marker(purpose)
            channel.name = {'news': '📰 объявления', 'chat': '💬 беседка',
                            'books': '🏙️ книжный-город', 'essays': '✍️ работы'}[purpose]
        before = {purpose: (self.channel(purpose).id, self.channel(purpose).name,
                            self.acl(self.channel(purpose))) for purpose in TOPICS}
        self.h.clear_writes()
        self.service.refresh.reset_mock()
        self.h.guild.fetch_channels.reset_mock()
        await Provisioner(self.service).cleanup_topics(self.h.guild)
        self.assertEqual(self.store.settings(1), settings)
        for purpose in TOPICS:
            channel = self.channel(purpose)
            self.assertEqual(channel.topic, DESCRIPTIONS[purpose])
            self.assertEqual((channel.id, channel.name, self.acl(channel)), before[purpose])
            channel.set_permissions.assert_not_awaited()
            for call in channel.edit.await_args_list:
                self.assertEqual(set(call.kwargs) - {'reason'}, {'topic'})
        self.assertEqual(self.h.create_count(), 0)
        self.h.guild.fetch_channels.assert_not_awaited()
        self.service.refresh.assert_not_awaited()

    async def test_cleanup_is_idempotent_after_restart(self):
        await self.setup()
        channel = self.channel()
        channel.topic = DESCRIPTIONS['chat'] + '\n' + self.marker()
        self.h.clear_writes()
        await Provisioner(self.service).cleanup_topics(self.h.guild)
        self.assertEqual(channel.edit.await_count, 1)
        self.store = Store(self.path)
        self.make_service()
        self.h.clear_writes()
        await Provisioner(self.service).cleanup_topics(self.h.guild)
        for item in self.h.channels.values():
            item.edit.assert_not_awaited()
        self.assertEqual(self.h.create_count(), 0)

    async def test_successful_cleanup_is_not_polled_again_in_same_process(self):
        await self.setup()
        self.channel().topic = DESCRIPTIONS['chat'] + '\n' + self.marker()
        await Provisioner(self.service).cleanup_topics(self.h.guild)
        self.h.bot.fetch_channel.reset_mock()
        self.h.clear_writes()
        await Provisioner(self.service).cleanup_topics(self.h.guild)
        self.h.bot.fetch_channel.assert_not_awaited()
        for channel in self.h.channels.values():
            channel.edit.assert_not_awaited()

    async def test_permission_or_network_failure_defers_then_retries_without_acl_repair(self):
        await self.setup()
        for error in (forbidden(), OSError('Reply not received')):
            with self.subTest(error=type(error).__name__):
                self.make_service()
                channel = self.channel()
                marker = self.marker()
                channel.topic = DESCRIPTIONS['chat'] + '\n' + marker
                edit = channel.edit.side_effect
                channel.edit.side_effect = error
                self.h.clear_writes()
                with patch('bookclub.provision.time.monotonic', return_value=1000):
                    await Provisioner(self.service).cleanup_topics(self.h.guild)
                self.assertIn(marker, channel.topic)
                channel.set_permissions.assert_not_awaited()
                self.h.bot.fetch_channel.reset_mock()
                channel.edit.reset_mock()
                with patch('bookclub.provision.time.monotonic', return_value=1001):
                    await Provisioner(self.service).cleanup_topics(self.h.guild)
                channel.edit.assert_not_awaited()
                self.h.bot.fetch_channel.assert_not_awaited()
                channel.edit.side_effect = edit
                with patch('bookclub.provision.time.monotonic', return_value=1301):
                    await Provisioner(self.service).cleanup_topics(self.h.guild)
                self.assertEqual(channel.topic, DESCRIPTIONS['chat'])
                channel.set_permissions.assert_not_awaited()

    async def test_background_cleanup_uses_fresh_topic_instead_of_gateway_cache(self):
        await self.setup()
        stale = self.channel()
        marker = self.marker()
        stale.topic = OLD_DESCRIPTIONS['chat'] + '\n' + marker
        fresh = self.h.make_channel(stale.id, discord.TextChannel, name=stale.name,
                                    category=stale.category, overwrites=stale.overwrites,
                                    topic='Организатор только что изменил описание.\n' + marker)
        self.h.bot.get_channel = Mock(return_value=stale)
        await Provisioner(self.service).cleanup_topics(self.h.guild)
        self.assertEqual(fresh.topic, 'Организатор только что изменил описание.')
        self.assertIn(marker, stale.topic)
        self.h.bot.get_channel.assert_not_called()

    async def test_settings_binding_mismatch_is_not_cleaned(self):
        await self.setup()
        original = self.channel()
        original.topic = DESCRIPTIONS['chat'] + '\n' + self.marker()
        replacement = self.h.make_channel(9999, discord.TextChannel, topic='Описание другого канала')
        self.store.configure(1, {**self.store.settings(1), 'chat': replacement.id})
        self.h.clear_writes()
        await Provisioner(self.service).cleanup_topics(self.h.guild)
        self.assertIn('bookclub:', original.topic)
        self.assertEqual(replacement.topic, 'Описание другого канала')
        original.edit.assert_not_awaited()
        replacement.edit.assert_not_awaited()

    async def test_wrong_guild_or_channel_type_is_never_edited(self):
        await self.setup()
        channel = self.channel()
        marker = self.marker()
        channel.topic = DESCRIPTIONS['chat'] + '\n' + marker
        channel.guild = SimpleNamespace(id=2)
        self.h.clear_writes()
        await Provisioner(self.service).cleanup_topics(self.h.guild)
        channel.edit.assert_not_awaited()
        self.assertIn(marker, channel.topic)
        self.make_service()
        wrong = self.h.make_channel(channel.id, discord.VoiceChannel,
                                    topic=DESCRIPTIONS['chat'] + '\n' + marker)
        await Provisioner(self.service).cleanup_topics(self.h.guild)
        wrong.edit.assert_not_awaited()
        self.assertIn(marker, wrong.topic)

    async def test_regular_publication_refresh_performs_migration_without_setup(self):
        await self.setup()
        channel = self.channel()
        channel.topic = OLD_DESCRIPTIONS['chat'] + '\n' + self.marker()
        settings = self.store.settings(1)
        rules = self.h.message(self.channel('news'), 9001, 'Правила клуба')
        rules.pinned = True
        self.service.upsert = AsyncMock(return_value={'channel_id': settings['news'], 'message_id': rules.id})
        self.service.refresh_catalog = AsyncMock()
        self.h.clear_writes()
        await Service.refresh(self.service, self.h.guild)
        self.assertEqual(channel.topic, DESCRIPTIONS['chat'])
        self.assertEqual(self.h.create_count(), 0)
        self.assertEqual(self.store.settings(1), settings)

    async def test_ready_topic_cleanup_failure_does_not_block_explicit_bot_permission_repair(self):
        await self.setup()
        channel = self.channel()
        settings = self.store.settings(1)
        marker = self.marker()
        channel.topic = DESCRIPTIONS['chat'] + '\n' + marker
        channel.overwrites[self.h.bot_member].send_messages = False
        humans = {target.id: tuple(value.value for value in overwrite.pair())
                  for target, overwrite in channel.overwrites.items() if target.id != self.h.bot_member.id}
        channel.edit.side_effect = forbidden()
        self.h.clear_writes()
        report = await self.setup(repair_permissions=True)
        self.assertTrue(channel.permissions_for(self.h.bot_member).send_messages)
        channel.set_permissions.assert_awaited_once()
        self.assertEqual(humans, {target.id: tuple(value.value for value in overwrite.pair())
                                 for target, overwrite in channel.overwrites.items()
                                 if target.id != self.h.bot_member.id})
        self.assertIn(marker, channel.topic)
        self.assertEqual(self.store.settings(1), settings)
        self.assertEqual(self.h.create_count(), 0)
        self.assertTrue(any('Готово:' in line for line in report))

    async def test_known_old_descriptions_upgrade_while_removing_own_marker(self):
        await self.setup()
        for purpose, description in OLD_DESCRIPTIONS.items():
            self.channel(purpose).topic = description + '\n' + self.marker(purpose)
        await Provisioner(self.service).cleanup_topics(self.h.guild)
        for purpose in OLD_DESCRIPTIONS:
            self.assertEqual(self.channel(purpose).topic, DESCRIPTIONS[purpose])

    async def test_custom_topic_text_and_spacing_survive_own_marker_cleanup(self):
        await self.setup()
        channel = self.channel()
        custom = '  Наше описание.\n\nВторая строка  '
        channel.topic = custom + '\n' + self.marker()
        await Provisioner(self.service).cleanup_topics(self.h.guild)
        self.assertEqual(channel.topic, custom)

    async def test_old_description_with_custom_addition_is_not_rewritten(self):
        await self.setup()
        custom = OLD_DESCRIPTIONS['chat'] + '\nНаша дополнительная договорённость.'
        self.channel().topic = custom + '\n' + self.marker()
        await Provisioner(self.service).cleanup_topics(self.h.guild)
        self.assertEqual(self.channel().topic, custom)

    async def test_foreign_and_inline_or_modified_markers_are_preserved(self):
        await self.setup()
        channel = self.channel()
        marker = self.marker()
        for custom in ('Наш текст про ' + marker, marker + ':additional',
                       marker.replace(':chat:', ':news:'), 'bookclub:999:chat:other-token'):
            with self.subTest(custom=custom):
                self.make_service()
                channel.topic = custom
                self.h.clear_writes()
                await Provisioner(self.service).cleanup_topics(self.h.guild)
                self.assertEqual(channel.topic, custom)
                channel.edit.assert_not_awaited()

    async def test_foreign_marker_remains_when_exact_own_marker_is_removed(self):
        await self.setup()
        foreign = 'bookclub:999:chat:other-token'
        self.channel().topic = 'Наш текст\n' + foreign + '\n' + self.marker()
        await Provisioner(self.service).cleanup_topics(self.h.guild)
        self.assertEqual(self.channel().topic, 'Наш текст\n' + foreign)

    async def test_empty_description_after_marker_removal_clears_topic(self):
        await self.setup()
        self.channel().topic = self.marker()
        await Provisioner(self.service).cleanup_topics(self.h.guild)
        self.assertFalse(self.channel().topic)

    async def test_unmanaged_channel_and_dry_run_leave_topics_untouched(self):
        await self.setup()
        channel = self.channel()
        marked = DESCRIPTIONS['chat'] + '\n' + self.marker()
        channel.topic = marked
        self.h.clear_writes()
        await self.setup(check_only=True)
        self.assertEqual(channel.topic, marked)
        channel.edit.assert_not_awaited()
        self.store.bind_setup_resource(1, 'chat', channel.id, managed=False, state='ready')
        await Provisioner(self.service).cleanup_topics(self.h.guild)
        self.assertEqual(channel.topic, marked)
        channel.edit.assert_not_awaited()

    async def test_missing_durable_channel_identity_keeps_recovery_marker(self):
        await self.setup()
        channel = self.channel()
        self.store.forget_setup_resource(1, 'chat')
        self.store.reserve_setup_resource(1, 'chat')
        marker = self.marker()
        channel.topic = DESCRIPTIONS['chat'] + '\n' + marker
        self.h.clear_writes()
        await Provisioner(self.service).cleanup_topics(self.h.guild)
        self.assertIn(marker, channel.topic)
        channel.edit.assert_not_awaited()

    async def test_lost_cleanup_ack_uses_saved_identity_after_restart(self):
        create_text = self.h.guild.create_text_channel.side_effect
        interrupted = []

        async def created_with_lost_cleanup(name, **kwargs):
            channel = await create_text(name, **kwargs)
            edit = channel.edit.side_effect

            async def lost_cleanup(**changes):
                if 'topic' in changes and not interrupted:
                    binding = next(row for row in self.store.setup_resources(1)
                                   if row['channel_id'] == channel.id)
                    self.assertTrue(binding['managed'])
                    await edit(**changes)
                    interrupted.append(channel.id)
                    raise OSError('Cleaned topic response was lost')
                return await edit(**changes)

            channel.edit.side_effect = lost_cleanup
            return channel

        self.h.guild.create_text_channel.side_effect = created_with_lost_cleanup
        await self.setup()
        self.assertEqual(len(interrupted), 1)
        known_id = interrupted[0]
        self.assertNotIn('bookclub:', self.h.channels[known_id].topic)
        self.store = Store(self.path)
        self.make_service()
        self.h.guild.create_text_channel.side_effect = create_text
        await self.setup()
        self.assertEqual(self.store.settings(1)['news'], known_id)
        self.assertEqual(self.h.create_count(), 6)

    async def test_lost_friendly_rename_after_cleanup_keeps_durable_binding(self):
        create_text = self.h.guild.create_text_channel.side_effect
        interrupted = []

        async def created_with_lost_rename(name, **kwargs):
            channel = await create_text(name, **kwargs)
            edit = channel.edit.side_effect

            async def lost_rename(**changes):
                if 'name' in changes and not interrupted:
                    self.assertNotIn('bookclub:', channel.topic)
                    await edit(**changes)
                    interrupted.append(channel.id)
                    raise OSError('Friendly rename response was lost')
                return await edit(**changes)

            channel.edit.side_effect = lost_rename
            return channel

        self.h.guild.create_text_channel.side_effect = created_with_lost_rename
        await self.setup()
        self.assertEqual(len(interrupted), 1)
        known_id = interrupted[0]
        self.store = Store(self.path)
        self.make_service()
        self.h.guild.create_text_channel.side_effect = create_text
        await self.setup()
        self.assertEqual(self.store.settings(1)['news'], known_id)
        self.assertEqual(self.h.create_count(), 6)
        self.assertEqual(self.channel('news').topic, DESCRIPTIONS['news'])


if __name__ == '__main__':
    unittest.main()

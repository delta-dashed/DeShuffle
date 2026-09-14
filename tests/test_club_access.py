"""Native Discord ACLs guard commands, private forms and reminder recipients."""
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import discord

from bookclub.store import ClubError
from bookclub.ui import Club, PlanView, PlanModal, MeetingView
from test_bookclub import ClubFixture
from test_bookclub_discord import DiscordHarness


class NativeClubAccessTests(ClubFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.h = DiscordHarness()
        self.cog = Club(self.h.bot, self.store)
        self.service = self.cog.service
        self.h.guild.fetch_channels = AsyncMock(side_effect=lambda: list(self.h.channels.values()))
        self.h.event(self.meeting['event_id'], name=self.meeting['name'],
                     start_time=datetime.fromtimestamp(self.meeting['start'], timezone.utc),
                     end_time=datetime.fromtimestamp(self.meeting['end'], timezone.utc))

    def deny(self, channel_id=13, users=(1,), permission='view_channel'):
        channel = self.h.channels[channel_id]
        original = channel.permissions_for.return_value
        def permissions(member):
            values = vars(original).copy()
            if member.id in users:
                values[permission] = False
            return SimpleNamespace(**values)
        channel.permissions_for.side_effect = permissions

    def context(self, command, user=1, *, slash=True, channel_id=12):
        return SimpleNamespace(guild=self.h.guild, author=self.h.members[user], command=command,
                               channel=self.h.channels[channel_id], interaction=self.h.interaction(user) if slash else None,
                               message=SimpleNamespace(id=4321), defer=AsyncMock(), send=AsyncMock())

    async def test_actor_checks_fresh_channel_and_read_history(self):
        stale = self.h.channels[13]
        fresh = self.h.channel(13, discord.ForumChannel)
        self.h.guild.get_channel.return_value = stale
        self.h.guild.get_channel.side_effect = lambda ident: stale if ident == 13 else self.h.channels.get(ident)
        self.deny(permission='read_message_history')
        self.assertTrue(stale.permissions_for(self.h.members[1]).read_message_history)
        self.assertFalse(fresh.permissions_for(self.h.members[1]).read_message_history)
        with self.assertRaisesRegex(ClubError, 'доступа'):
            await self.service.actor(self.h.guild, 1)

    async def test_maintenance_keeps_organizer_auth_when_forum_is_missing(self):
        del self.h.channels[13]
        for command in (self.cog.diagnose, self.cog.publish, self.cog.repair):
            ctx = self.context(command, 99)
            await self.cog.cog_before_invoke(ctx)
            await self.cog.organizer(ctx)
            with self.assertRaisesRegex(ClubError, 'организатору'):
                await self.cog.organizer(self.context(command, 1))

    async def test_command_guard_blocks_private_club_join_and_meeting(self):
        self.deny()
        for command in (self.cog.join, self.cog.meeting, self.cog.essays):
            with self.subTest(command=command.name), self.assertRaisesRegex(ClubError, 'доступа'):
                await self.cog.cog_before_invoke(self.context(command))
        self.assertEqual(len(self.store.participants(self.book['id'])), 3)

    async def test_prefix_outside_club_requires_private_slash_response(self):
        self.h.channel(100, discord.TextChannel)
        with self.assertRaisesRegex(ClubError, 'slash'):
            await self.cog.cog_before_invoke(self.context(self.cog.meeting, slash=False, channel_id=100))
        await self.cog.cog_before_invoke(self.context(self.cog.meeting, slash=True, channel_id=100))
        await self.cog.cog_before_invoke(self.context(self.cog.meeting, slash=False, channel_id=12))

    async def test_autocomplete_returns_no_private_or_disabled_guild_content(self):
        self.deny()
        interaction = self.h.interaction()
        for callback in (self.cog.book_autocomplete, self.cog.meeting_autocomplete, self.cog.event_autocomplete):
            self.assertEqual(await callback(interaction, ''), [])
        self.h.channels[13].permissions_for.side_effect = None
        self.service.guild_ids = {2}
        self.assertEqual(await self.cog.book_autocomplete(interaction, ''), [])

    async def test_event_autocomplete_filters_unrelated_private_voice(self):
        self.h.channel(16, discord.VoiceChannel)
        hidden = self.h.event(999, name='Скрытая встреча', channel=self.h.channels[16],
                              start_time=datetime.fromtimestamp(self.meeting['start'], timezone.utc))
        self.deny(16)
        results = await self.cog.event_autocomplete(self.h.interaction(), '')
        self.assertEqual([choice.value for choice in results], [str(self.meeting['event_id'])])
        self.assertNotIn(str(hidden.id), [choice.value for choice in results])

    async def test_closed_member_is_excluded_from_rotation_without_losing_participation(self):
        self.deny()
        self.assertEqual(await self.service.live_participants(self.h.guild), {2, 3})
        self.assertTrue(next(p for p in self.store.participants(self.book['id']) if p['user_id'] == 1)['present'])
        self.h.channels[13].permissions_for.side_effect = None
        self.assertEqual(await self.service.live_participants(self.h.guild), {1, 2, 3})

    async def test_reminders_stop_after_access_revocation(self):
        self.deny()
        job = dict(key='test', guild_id=1, entity_id=self.meeting['id'], kind='participants', target=0)
        await self.service.deliver(self.h.guild, job)
        self.h.members[1].send.assert_not_awaited()
        self.h.members[2].send.assert_awaited_once()
        self.h.members[3].send.assert_awaited_once()

    async def test_essay_reminders_also_require_essay_forum_access(self):
        self.deny(14)
        await self.service.deliver(self.h.guild, dict(key='test', entity_id=self.book['id'], kind='essay_due', target=0))
        self.h.members[1].send.assert_not_awaited()
        self.h.members[2].send.assert_awaited_once()

    async def test_removing_participant_still_works_after_role_revocation(self):
        self.deny()
        self.service.refresh = AsyncMock()
        await Club.participant.callback(self.cog, self.context(self.cog.participant, 99),
                                        self.book['id'], self.h.members[1], remove=True)
        self.assertNotIn(1, {p['user_id'] for p in self.store.participants(self.book['id'])})

    async def test_existing_meeting_and_plan_controls_reject_revoked_access(self):
        self.store.host_action(1, self.meeting['id'], 1, 'volunteer', version=self.meeting['host_version'], live_ids={1})
        meeting = self.store.meeting(1, self.meeting['id'])
        plan = self.store.plan(1, meeting['id'], 1)
        view = PlanView(self.cog, meeting, plan, 1)
        modal = PlanModal(self.cog, meeting, plan, 'edit')
        self.deny()
        interaction = self.h.interaction()
        with self.assertRaisesRegex(ClubError, 'доступа'):
            await view.children[0].callback(interaction)
        interaction.response.send_modal.assert_not_awaited()
        with self.assertRaisesRegex(ClubError, 'доступа'):
            await modal.on_submit(interaction)
        with self.assertRaisesRegex(ClubError, 'доступа'):
            await MeetingView(self.cog, meeting).act(interaction, 'plan')

    async def test_essay_list_and_registration_require_both_forums(self):
        self.deny(14)
        with self.assertRaisesRegex(ClubError, 'доступа'):
            await Club.essays.callback(self.cog, self.context(self.cog.essays), self.book['id'])
        with self.assertRaisesRegex(ClubError, 'доступа'):
            await Club.essay.callback(self.cog, self.context(self.cog.essay), self.book['id'],
                                      'https://discord.com/channels/1/12/555')

    async def test_essay_registration_cannot_read_inaccessible_source(self):
        self.deny(12)
        channel = self.h.channels[12]
        with self.assertRaisesRegex(ClubError, 'исходному'):
            await Club.essay.callback(self.cog, self.context(self.cog.essay), self.book['id'],
                                      'https://discord.com/channels/1/12/555')
        channel.fetch_message.assert_not_awaited()

    async def test_new_essay_requires_permission_to_create_forum_posts(self):
        self.store.set_published(1)
        self.deny(14, permission='send_messages')
        with self.assertRaisesRegex(ClubError, 'создавать публикации'):
            await self.service.create_essay_space(self.h.guild, self.book['id'], 1)
        self.h.channels[14].create_thread.assert_not_awaited()
        self.assertIsNone(self.store.publication(f'essay-space:{self.book["id"]}:1'))

    async def test_existing_essay_can_reopen_when_only_post_creation_is_disabled(self):
        self.store.configure(1, {**self.store.settings(1), 'essay_webhooks': False})
        self.store.set_published(1)
        original = await self.service.create_essay_space(self.h.guild, self.book['id'], 1)
        self.deny(14, permission='send_messages')
        reopened = await self.service.create_essay_space(self.h.guild, self.book['id'], 1)
        self.assertEqual(reopened['source_id'], original['source_id'])
        self.h.channels[14].create_thread.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()

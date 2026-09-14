"""Meeting forms exercise real Discord views with a disposable event transport."""
from datetime import datetime, timezone
import unittest
from unittest.mock import AsyncMock

import discord

from bookclub.meeting_controls import (
    BookMeetings, CancelConfirmation, MeetingControls, MeetingModal,
    open_book_meetings, open_meeting_controls,
)
from bookclub.store import ClubError
from bookclub.ui import Club
from test_bookclub import ClubFixture, CONFIG
from test_bookclub_discord import DiscordHarness


class MeetingControlsTests(ClubFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.h = DiscordHarness()
        self.cog = Club(self.h.bot, self.store)
        self.cog.service.refresh = AsyncMock()
        self.event = self.add_event(self.meeting)

    async def asyncSetUp(self):
        import asyncio
        asyncio.get_running_loop().slow_callback_duration = 1.0

    def add_event(self, meeting):
        return self.h.event(meeting['event_id'], name=meeting['name'],
                            start_time=datetime.fromtimestamp(meeting['start'], timezone.utc),
                            end_time=datetime.fromtimestamp(meeting['end'], timezone.utc))

    @staticmethod
    def button(view, label):
        return next(child for child in view.children if getattr(child, 'label', None) == label)

    @staticmethod
    def fill(modal, **fields):
        for name, value in fields.items():
            modal.inputs[name]._value = str(value)

    async def test_book_panel_is_private_and_shows_plan_without_creating_events(self):
        with self.store.tx() as db:
            db.execute('UPDATE bc_books SET reading_meetings=4 WHERE id=?', (self.book['id'],))
        interaction = self.h.interaction(99)
        await open_book_meetings(self.cog, interaction, self.book['id'])
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        reply = interaction.followup.send.call_args
        self.assertTrue(reply.kwargs['ephemeral'])
        self.assertIn('4 встреч по книге + 1 обсуждение эссе', reply.args[0])
        self.assertIn('Предстоящих: 1', reply.args[0])
        self.assertIsInstance(reply.kwargs['view'], BookMeetings)
        self.h.guild.create_scheduled_event.assert_not_awaited()

    async def test_open_requires_fresh_organizer_and_forum_access(self):
        for target in (open_book_meetings, open_meeting_controls):
            interaction = self.h.interaction(1)
            with self.assertRaises(ClubError):
                await target(self.cog, interaction, self.book['id'] if target is open_book_meetings else self.meeting['id'])
            interaction.followup.send.assert_not_awaited()
        self.h.channels[13].permissions_for.return_value.view_channel = False
        with self.assertRaises(ClubError):
            await open_book_meetings(self.cog, self.h.interaction(99), self.book['id'])

    async def test_foreign_book_or_meeting_does_not_disclose_details(self):
        self.store.configure(2, CONFIG)
        foreign = self.store.create_book(2, 'Private foreign title', 'Author', '', 'foreign')
        meeting = self.store.draft_meeting(2, foreign['id'], 'Secret', 'part', 'chapter', 'foreign')
        for target, ident in ((open_book_meetings, foreign['id']), (open_meeting_controls, meeting['id'])):
            interaction = self.h.interaction(99)
            with self.assertRaises(ClubError):
                await target(self.cog, interaction, ident)
            interaction.followup.send.assert_not_awaited()

    async def test_prefilled_modal_opens_without_rest_and_has_timezone(self):
        view = MeetingControls(self.cog, self.meeting, 99)
        interaction = self.h.interaction(99)
        await self.button(view, 'Дата и длительность').callback(interaction)
        modal = interaction.response.send_modal.call_args.args[0]
        self.assertEqual(set(modal.inputs), {'date', 'minutes'})
        self.assertIn('Europe/Moscow', modal.inputs['date'].label)
        self.assertTrue(modal.inputs['date'].default.endswith('+03:00'))
        self.assertEqual(modal.inputs['minutes'].default, '90')
        self.h.guild.fetch_member.assert_not_awaited()
        self.h.bot.fetch_channel.assert_not_awaited()

    async def test_private_views_and_modals_are_bound_to_actor_and_guild(self):
        parent = MeetingControls(self.cog, self.meeting, 99)
        modal = MeetingModal(parent, 'move')
        for interaction in (self.h.interaction(1), self.h.interaction(99)):
            if interaction.user.id == 99:
                interaction.guild_id = 2
            with self.assertRaises(ClubError):
                await modal.on_submit(interaction)
            interaction.response.defer.assert_not_awaited()
            with self.assertRaises(ClubError):
                await self.button(parent, 'Дата и длительность').callback(interaction)
            interaction.response.send_modal.assert_not_awaited()

    async def test_revoked_organizer_cannot_submit_open_form(self):
        self.store.configure(1, dict(CONFIG, organizers=[1]))
        view = MeetingControls(self.cog, self.meeting, 1)
        modal = MeetingModal(view, 'move')
        self.fill(modal, date=datetime.fromtimestamp(self.now + 10 * 86400, timezone.utc).isoformat(), minutes='60')
        self.store.configure(1, dict(CONFIG, organizers=[]))
        with self.assertRaises(ClubError):
            await modal.on_submit(self.h.interaction(1))
        self.event.edit.assert_not_awaited()

    async def test_add_form_keeps_request_key_and_retry_reuses_event(self):
        parent = BookMeetings(self.cog, self.book, 99, [self.meeting])
        interaction = self.h.interaction(99)
        await self.button(parent, 'Добавить встречу').callback(interaction)
        modal = interaction.response.send_modal.call_args.args[0]
        self.assertEqual(len(modal.children), 5)
        self.assertEqual(modal.request_key, f'meeting-form:{interaction.id}')
        self.fill(modal, name='Обсуждение эссе', date=datetime.fromtimestamp(self.now + 10 * 86400, timezone.utc).isoformat(),
                  minutes='60', part='Вся книга', chapter='Последняя')
        await modal.on_submit(self.h.interaction(99))
        await modal.on_submit(self.h.interaction(99))
        self.h.guild.create_scheduled_event.assert_awaited_once()
        result = self.store.one('SELECT * FROM bc_meetings WHERE request_key=?', (modal.request_key,))
        self.assertEqual(result['end'] - result['start'], 3600)
        self.assertEqual(result['name'], 'Обсуждение эссе')

    async def test_invalid_duration_rejected_before_event_mutation(self):
        parent = MeetingControls(self.cog, self.meeting, 99)
        for value in ('90.5', 'abc', '0', '1441'):
            modal = MeetingModal(parent, 'move')
            self.fill(modal, date=datetime.fromtimestamp(self.now + 10 * 86400, timezone.utc).isoformat(), minutes=value)
            with self.assertRaises(ClubError):
                await modal.on_submit(self.h.interaction(99))
        self.event.edit.assert_not_awaited()

    async def test_open_form_cannot_overwrite_manual_native_move(self):
        modal = MeetingModal(MeetingControls(self.cog, self.meeting, 99), 'move')
        self.fill(modal, date=datetime.fromtimestamp(self.now + 10 * 86400, timezone.utc).isoformat(), minutes='60')
        self.event.start_time = datetime.fromtimestamp(self.now + 5 * 86400, timezone.utc)
        with self.assertRaisesRegex(ClubError, 'изменилась'):
            await modal.on_submit(self.h.interaction(99))
        self.event.edit.assert_not_awaited()
        self.assertEqual(self.store.meeting(1, self.meeting['id'])['start'], self.now + 5 * 86400)

    async def test_cancel_requires_specific_confirmation_and_preserves_history(self):
        parent = MeetingControls(self.cog, self.meeting, 99)
        interaction = self.h.interaction(99)
        await self.button(parent, 'Отменить встречу').callback(interaction)
        self.event.cancel.assert_not_awaited()
        result = interaction.followup.send.call_args
        self.assertIn(self.meeting['name'], result.args[0])
        confirmation = result.kwargs['view']
        self.assertIsInstance(confirmation, CancelConfirmation)
        await self.button(confirmation, 'Отменить эту встречу').callback(self.h.interaction(99))
        current = self.store.meeting(1, self.meeting['id'])
        self.assertEqual(current['status'], 'cancelled')
        self.assertEqual(current['event_id'], self.meeting['event_id'])
        self.assertEqual(self.store.rows("SELECT * FROM bc_jobs WHERE entity_id=? AND state='pending'", (current['id'],)), [])

    async def test_changed_meeting_cannot_be_cancelled_from_old_confirmation(self):
        confirmation = CancelConfirmation(self.cog, self.meeting, 99)
        self.event.name = 'Новое название'
        with self.assertRaisesRegex(ClubError, 'изменилась'):
            await self.button(confirmation, 'Отменить эту встречу').callback(self.h.interaction(99))
        self.event.cancel.assert_not_awaited()

    async def test_closed_meeting_has_no_enabled_mutation_buttons(self):
        self.event.status = discord.EventStatus.completed
        interaction = self.h.interaction(99)
        await open_meeting_controls(self.cog, interaction, self.meeting['id'])
        view = interaction.followup.send.call_args.kwargs['view']
        for label in ('Дата и длительность', 'Название и главы', 'Отменить встречу'):
            self.assertTrue(self.button(view, label).disabled)

    async def test_pagination_covers_all_meetings_with_upcoming_first(self):
        for days in range(4, 30):
            self.add_event(self.make_meeting(days))
        self.event.status = discord.EventStatus.completed
        interaction = self.h.interaction(99)
        await open_book_meetings(self.cog, interaction, self.book['id'])
        first = interaction.followup.send.call_args.kwargs['view']
        select = next(child for child in first.children if isinstance(child, discord.ui.Select))
        self.assertEqual(len(select.options), 25)
        self.assertTrue(all('Запланирована' in option.description for option in select.options))
        next_interaction = self.h.interaction(99)
        await self.button(first, 'Далее').callback(next_interaction)
        second = next_interaction.followup.send.call_args.kwargs['view']
        next_select = next(child for child in second.children if isinstance(child, discord.ui.Select))
        self.assertEqual(len(next_select.options), 2)
        ids = {option.value for option in select.options + next_select.options}
        self.assertEqual(len(ids), 27)
        self.assertEqual(next_select.options[-1].value, self.meeting['id'])
        self.assertTrue(self.button(second, 'Далее').disabled)

    async def test_selection_cannot_open_unlisted_meeting(self):
        view = BookMeetings(self.cog, self.book, 99, [self.meeting])
        select = next(child for child in view.children if isinstance(child, discord.ui.Select))
        select._values = ['unlisted']
        with self.assertRaises(ClubError):
            await select.callback(self.h.interaction(99))


if __name__ == '__main__':
    unittest.main()

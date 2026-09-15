"""Organizer mutations use native events, persistent requests and fresh rights."""
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import discord

from bookclub.meeting_actions import create_meeting, move_meeting, edit_meeting, cancel_meeting
from bookclub.service import Service
from bookclub.store import ClubError
from test_bookclub import ClubFixture, CONFIG
from test_bookclub_discord import DiscordHarness, not_found


class MeetingActionTests(ClubFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.h = DiscordHarness()
        self.service = Service(self.h.bot, self.store)
        self.service.refresh = AsyncMock()
        self.guild = self.h.guild
        self.event = self.h.event(self.meeting['event_id'], name=self.meeting['name'],
            start_time=datetime.fromtimestamp(self.meeting['start'], timezone.utc),
            end_time=datetime.fromtimestamp(self.meeting['end'], timezone.utc),
            description=f'{self.meeting["part"]}; до главы {self.meeting["chapter"]} включительно.\n[bookclub:{self.meeting["id"]}]')

    def date(self, days=5):
        return datetime.fromtimestamp(self.now + days * 86400, timezone.utc).isoformat()

    async def create(self, **changes):
        fields = dict(name='Обсуждение', date=self.date(), minutes=90,
                      part='Часть 2', chapter='10', request_key='interaction:1')
        fields.update(changes)
        return await create_meeting(self.service, self.guild, 99, self.book['id'], **fields)

    async def move(self, **changes):
        fields = dict(date=self.date(), minutes=60)
        fields.update(changes)
        return await move_meeting(self.service, self.guild, 99, self.meeting['id'], **fields)

    async def edit(self, **changes):
        fields = dict(part='Часть 2', chapter='11')
        fields.update(changes)
        return await edit_meeting(self.service, self.guild, 99, self.meeting['id'], **fields)

    async def cancel(self, **changes):
        return await cancel_meeting(self.service, self.guild, 99, self.meeting['id'], **changes)

    async def test_all_actions_recheck_organizer(self):
        for action, args in (
            (create_meeting, dict(book_id=self.book['id'], name='Встреча', date=self.date(), minutes=60, part='1', chapter='2', request_key='denied')),
            (move_meeting, dict(meeting_id=self.meeting['id'], date=self.date(), minutes=60)),
            (edit_meeting, dict(meeting_id=self.meeting['id'], part='1', chapter='2')),
            (cancel_meeting, dict(meeting_id=self.meeting['id'])),
        ):
            with self.subTest(action=action.__name__), self.assertRaisesRegex(ClubError, 'организатор'):
                await action(self.service, self.guild, 1, **args)
        self.guild.create_scheduled_event.assert_not_awaited()
        self.event.edit.assert_not_awaited()
        self.event.cancel.assert_not_awaited()
        self.service.refresh.assert_not_awaited()

    async def test_explicit_essay_kind_is_saved_without_changing_book_status_on_scheduling(self):
        before = self.store.book(1, self.book['id'])['status']
        result = await self.create(plan_kind='essay')
        self.assertEqual(result['plan_kind'], 'essay')
        self.assertEqual(result['status'], 'scheduled')
        self.assertEqual(self.store.book(1, self.book['id'])['status'], before)
        replay = await self.create(plan_kind='essay')
        self.assertEqual(replay['id'], result['id'])
        self.guild.create_scheduled_event.assert_awaited_once()

    async def test_invalid_kind_is_rejected_before_discord_creation(self):
        with self.assertRaises(ClubError):
            await self.create(plan_kind='guessed-from-title')
        self.guild.create_scheduled_event.assert_not_awaited()

    async def test_revoked_club_access_blocks_organizer(self):
        self.h.channels[13].permissions_for.return_value.view_channel = False
        with self.assertRaises(ClubError):
            await self.move()
        self.event.edit.assert_not_awaited()

    async def test_foreign_book_cannot_receive_meeting(self):
        self.store.configure(2, CONFIG)
        foreign = self.store.create_book(2, 'Чужая книга', 'Автор', '', 'foreign')
        with self.assertRaises(ClubError):
            await create_meeting(self.service, self.guild, 99, foreign['id'], name='Встреча',
                date=self.date(), minutes=90, part='1', chapter='2', request_key='foreign')
        self.guild.create_scheduled_event.assert_not_awaited()

    async def test_create_repeated_request_has_one_event(self):
        first = await self.create()
        second = await self.create()
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(first['event_id'], second['event_id'])
        self.guild.create_scheduled_event.assert_awaited_once()
        self.assertEqual(first['start'], self.now + 5 * 86400)
        self.assertEqual(first['end'] - first['start'], 90 * 60)

    async def test_same_request_cannot_be_reused_for_another_book(self):
        first = await self.create()
        other = self.store.create_book(1, 'Другая книга', 'Автор', '', 'another')
        with self.assertRaisesRegex(ClubError, 'другой книге'):
            await create_meeting(self.service, self.guild, 99, other['id'], name='Встреча',
                date=self.date(), minutes=90, part='1', chapter='2', request_key='interaction:1')
        self.guild.create_scheduled_event.assert_awaited_once()
        self.assertEqual(self.store.meeting(1, first['id'])['book_id'], self.book['id'])

    async def test_lost_creation_response_recovers_native_marker(self):
        create = self.guild.create_scheduled_event.side_effect
        async def create_then_fail(**kwargs):
            await create(**kwargs)
            raise OSError('lost acknowledgement')
        self.guild.create_scheduled_event.side_effect = create_then_fail
        first = await self.create()
        second = await self.create()
        self.assertEqual(first['event_id'], second['event_id'])
        self.guild.create_scheduled_event.assert_awaited_once()

    async def test_unconfirmed_creation_is_not_repeated(self):
        self.guild.create_scheduled_event.side_effect = OSError('offline')
        with self.assertRaisesRegex(ClubError, 'recover_event'):
            await self.create()
        with self.assertRaisesRegex(ClubError, 'recover_event'):
            await self.create()
        self.guild.create_scheduled_event.assert_awaited_once()
        draft = self.store.one('SELECT * FROM bc_meetings WHERE request_key=?', ('interaction:1',))
        self.assertIsNone(draft['event_id'])

    async def test_duplicate_marker_requires_explicit_recovery(self):
        draft = self.store.draft_meeting(1, self.book['id'], 'Встреча', '1', '2', 'interaction:1')
        for ident in (555, 556):
            self.h.event(ident, name='Встреча', start_time=self.event.start_time,
                         description=f'[bookclub:{draft["id"]}]')
        with self.assertRaisesRegex(ClubError, 'recover_event'):
            await self.create()
        self.guild.create_scheduled_event.assert_not_awaited()
        self.assertIsNone(self.store.meeting(1, draft['id'])['event_id'])

    async def test_input_validation_creates_no_draft(self):
        for change in ({'name': ' '}, {'chapter': 'x' * 251}, {'minutes': 0},
                       {'minutes': 1441}, {'minutes': True}, {'date': self.date(-1)}):
            with self.subTest(change=change), self.assertRaises(ClubError):
                await self.create(**change)
        self.assertIsNone(self.store.one('SELECT id FROM bc_meetings WHERE request_key=?', ('interaction:1',)))
        self.guild.create_scheduled_event.assert_not_awaited()

    async def test_move_native_change_requires_host_reconfirmation(self):
        host = self.action('volunteer')
        self.save_plan(topics='Вопросы')
        self.save_plan(ready=True)
        result = await self.move(expected_revision=host['revision'])
        self.assertEqual(result['start'], self.now + 5 * 86400)
        self.assertEqual(result['host_state'], 'pending')
        self.assertGreater(result['host_version'], host['host_version'])
        self.assertFalse(self.store.plan(1, result['id'], 99, organizer=True)['ready'])
        self.assertEqual(len([job for job in self.jobs() if job['kind'] == 'rescheduled']), 1)

    async def test_lost_move_response_is_confirmed_once(self):
        edit = self.event.edit.side_effect
        async def edit_then_fail(**kwargs):
            await edit(**kwargs)
            raise OSError('lost acknowledgement')
        self.event.edit.side_effect = edit_then_fail
        result = await self.move()
        self.assertEqual(result['start'], self.now + 5 * 86400)
        await self.move()
        self.event.edit.assert_awaited_once()

    async def test_false_successful_edit_does_not_update_requested_values(self):
        self.event.edit = AsyncMock(return_value=self.event)
        with self.assertRaisesRegex(ClubError, 'не подтвердил'):
            await self.move()
        self.assertEqual(self.store.meeting(1, self.meeting['id'])['start'], self.meeting['start'])
        self.service.refresh.assert_not_awaited()

    async def test_native_rename_invalidates_stale_form(self):
        self.event.name = 'Название изменили в Discord'
        with self.assertRaisesRegex(ClubError, 'изменилась'):
            await self.move(expected_revision=self.meeting['revision'])
        self.event.edit.assert_not_awaited()
        self.assertEqual(self.store.meeting(1, self.meeting['id'])['name'], self.event.name)

    async def test_deleted_event_never_becomes_another_event(self):
        del self.h.events[self.event.id]
        with self.assertRaisesRegex(ClubError, 'недоступно'):
            await self.edit()
        self.assertEqual(self.store.meeting(1, self.meeting['id'])['status'], 'cancelled')
        self.guild.create_scheduled_event.assert_not_awaited()

    async def test_unconfirmed_draft_cannot_be_moved(self):
        draft = self.store.draft_meeting(1, self.book['id'], 'Встреча', '1', '2', 'draft')
        with self.assertRaisesRegex(ClubError, 'recover_event'):
            await move_meeting(self.service, self.guild, 99, draft['id'], date=self.date(), minutes=60)
        self.guild.fetch_scheduled_event.assert_not_awaited()

    async def test_edit_boundary_resets_plan_and_revises_reminders(self):
        self.action('volunteer')
        self.save_plan(topics='Вопросы')
        self.save_plan(ready=True)
        previous = self.store.meeting(1, self.meeting['id'])
        result = await self.edit(expected_revision=previous['revision'])
        self.assertEqual(result['part'], 'Часть 2')
        self.assertEqual(result['chapter'], '11')
        self.assertEqual(result['host_state'], 'confirmed')
        self.assertEqual(result['host_version'], previous['host_version'])
        self.assertGreater(result['revision'], previous['revision'])
        self.assertFalse(self.store.plan(1, result['id'], 1)['ready'])
        self.assertTrue(all(job['revision'] == result['revision'] for job in self.jobs()))
        self.assertNotIn('[bookclub:', self.event.description)

    async def test_boundary_keeps_invitation_original_due(self):
        self.action('offer', organizer=True, candidate=1)
        original = next(job for job in self.jobs() if job['kind'] == 'offer')
        self.now += 15
        await self.edit()
        current = [job for job in self.jobs() if job['kind'] == 'offer']
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]['due'], original['due'])

    async def test_name_only_preserves_ready_plan_and_custom_description(self):
        self.action('volunteer')
        self.save_plan(topics='Вопросы')
        self.save_plan(ready=True)
        self.event.description = 'Ручное описание события'
        result = await self.edit(name='Новое название', part=self.meeting['part'], chapter=self.meeting['chapter'])
        self.assertEqual(result['name'], 'Новое название')
        self.assertEqual(self.event.description, 'Ручное описание события')
        self.assertTrue(self.store.plan(1, result['id'], 1)['ready'])

    async def test_false_boundary_edit_leaves_plan_and_range_unchanged(self):
        self.action('volunteer')
        self.save_plan(topics='Вопросы')
        self.save_plan(ready=True)
        self.event.edit = AsyncMock(return_value=self.event)
        with self.assertRaisesRegex(ClubError, 'не подтвердил'):
            await self.edit()
        self.assertEqual(self.store.meeting(1, self.meeting['id'])['part'], self.meeting['part'])
        self.assertTrue(self.store.plan(1, self.meeting['id'], 1)['ready'])

    async def test_lost_boundary_response_finishes_confirmed_plan_reset(self):
        self.action('volunteer')
        self.save_plan(topics='Вопросы')
        self.save_plan(ready=True)
        edit = self.event.edit.side_effect
        async def edit_then_fail(**kwargs):
            await edit(**kwargs)
            raise OSError('lost acknowledgement')
        self.event.edit.side_effect = edit_then_fail
        first = await self.edit()
        second = await self.edit()
        self.assertEqual(first, second)
        self.event.edit.assert_awaited_once()
        self.assertFalse(self.store.plan(1, first['id'], 1)['ready'])

    async def confirmed_boundary_after_fetch_failure(self, failure):
        self.action('volunteer')
        self.save_plan(topics='Вопросы')
        self.save_plan(ready=True)
        edit = self.event.edit.side_effect
        fetch = self.guild.fetch_scheduled_event.side_effect
        async def edit_then_fetch_fails(**kwargs):
            response = await edit(**kwargs)
            self.guild.fetch_scheduled_event.side_effect = failure
            return response
        self.event.edit.side_effect = edit_then_fetch_fails
        first = await self.edit()
        self.assertEqual((first['part'], first['chapter']), ('Часть 2', '11'))
        self.assertFalse(self.store.plan(1, first['id'], 1)['ready'])
        self.service.refresh.assert_awaited_once()
        self.guild.fetch_scheduled_event.side_effect = fetch
        second = await self.edit()
        self.assertEqual(first, second)
        self.event.edit.assert_awaited_once()

    async def test_acknowledged_boundary_survives_followup_server_error(self):
        await self.confirmed_boundary_after_fetch_failure(discord.HTTPException(
            SimpleNamespace(status=503, reason='Unavailable'), {'message': 'temporary'}))

    async def test_acknowledged_boundary_survives_followup_timeout(self):
        await self.confirmed_boundary_after_fetch_failure(TimeoutError('GET timeout'))

    async def test_acknowledged_boundary_survives_followup_network_error(self):
        await self.confirmed_boundary_after_fetch_failure(OSError('connection closed'))

    async def test_unconfirmed_response_and_failed_get_leave_boundary_unchanged(self):
        self.action('volunteer')
        self.save_plan(topics='Вопросы')
        self.save_plan(ready=True)
        fetch = self.guild.fetch_scheduled_event.side_effect
        for response in (None, False, self.event, SimpleNamespace(
                guild_id=2, id=self.event.id, entity_type=discord.EntityType.voice,
                status=discord.EventStatus.scheduled, description='Часть 2; до главы 11 включительно.')):
            with self.subTest(response=response):
                self.guild.fetch_scheduled_event.side_effect = fetch
                async def unconfirmed_edit(**kwargs):
                    self.guild.fetch_scheduled_event.side_effect = TimeoutError('GET timeout')
                    return response
                self.event.edit.side_effect = unconfirmed_edit
                with self.assertRaisesRegex(ClubError, 'Не удалось подтвердить'):
                    await self.edit()
                self.assertEqual(self.store.meeting(1, self.meeting['id'])['part'], self.meeting['part'])
                self.assertTrue(self.store.plan(1, self.meeting['id'], 1)['ready'])
        self.service.refresh.assert_not_awaited()

    async def test_failed_edit_and_failed_get_do_not_reset_plan(self):
        self.action('volunteer')
        self.save_plan(topics='Вопросы')
        self.save_plan(ready=True)
        async def edit_and_fetch_fail(**kwargs):
            self.guild.fetch_scheduled_event.side_effect = TimeoutError('GET timeout')
            raise OSError('edit response lost')
        self.event.edit.side_effect = edit_and_fetch_fail
        with self.assertRaisesRegex(ClubError, 'Не удалось подтвердить'):
            await self.edit()
        self.assertEqual(self.store.meeting(1, self.meeting['id'])['part'], self.meeting['part'])
        self.assertTrue(self.store.plan(1, self.meeting['id'], 1)['ready'])
        self.event.edit.assert_awaited_once()

    async def test_edit_response_does_not_override_later_not_found_or_forbidden(self):
        self.action('volunteer')
        self.save_plan(topics='Вопросы')
        self.save_plan(ready=True)
        edit = self.event.edit.side_effect
        fetch = self.guild.fetch_scheduled_event.side_effect
        for failure in (not_found(), discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'),
                                                      {'message': 'access revoked'})):
            with self.subTest(failure=failure):
                self.guild.fetch_scheduled_event.side_effect = fetch
                async def edit_then_fetch_fails(**kwargs):
                    response = await edit(**kwargs)
                    self.guild.fetch_scheduled_event.side_effect = failure
                    return response
                self.event.edit.side_effect = edit_then_fetch_fails
                with self.assertRaisesRegex(ClubError, 'Не удалось подтвердить'):
                    await self.edit()
                self.assertEqual(self.store.meeting(1, self.meeting['id'])['part'], self.meeting['part'])
                self.assertTrue(self.store.plan(1, self.meeting['id'], 1)['ready'])

    async def test_noop_boundary_preserves_ready_plan(self):
        self.action('volunteer')
        self.save_plan(topics='Вопросы')
        self.save_plan(ready=True)
        previous = self.store.meeting(1, self.meeting['id'])
        result = await self.edit(part=previous['part'], chapter=previous['chapter'])
        self.assertEqual(result['revision'], previous['revision'])
        self.assertTrue(self.store.plan(1, result['id'], 1)['ready'])
        self.event.edit.assert_not_awaited()

    async def test_active_event_allows_boundary_but_not_move_or_cancel(self):
        self.event.status = discord.EventStatus.active
        result = await self.edit()
        self.assertEqual(result['status'], 'active')
        for action in (self.move, self.cancel):
            with self.assertRaisesRegex(ClubError, 'не начавшуюся'):
                await action()
        self.event.cancel.assert_not_awaited()

    async def test_finished_event_cannot_be_rewritten(self):
        for status in (discord.EventStatus.completed, discord.EventStatus.cancelled):
            self.event.status = status
            with self.subTest(status=status), self.assertRaisesRegex(ClubError, 'будущую или текущую'):
                await self.edit()
        self.event.edit.assert_not_awaited()

    async def test_cancel_keeps_history_and_removes_pending_reminders(self):
        result = await self.cancel(expected_revision=self.meeting['revision'])
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['id'], self.meeting['id'])
        self.assertEqual(self.jobs(), [])
        self.assertEqual(result['event_id'], self.event.id)

    async def test_successful_cancel_response_survives_immediate_native_removal(self):
        async def cancel_then_remove():
            self.event.status = discord.EventStatus.cancelled
            del self.h.events[self.event.id]
            return self.event
        self.event.cancel.side_effect = cancel_then_remove
        result = await self.cancel()
        self.assertEqual(result['status'], 'cancelled')

    async def test_lost_cancel_response_reconciles_native_cancellation(self):
        async def cancel_then_fail():
            self.event.status = discord.EventStatus.cancelled
            raise OSError('lost acknowledgement')
        self.event.cancel.side_effect = cancel_then_fail
        result = await self.cancel()
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(self.jobs(), [])
        self.event.cancel.assert_awaited_once()

    async def test_false_cancel_response_preserves_meeting(self):
        self.event.cancel = AsyncMock(return_value=self.event)
        with self.assertRaisesRegex(ClubError, 'не подтвердил'):
            await self.cancel()
        self.assertEqual(self.store.meeting(1, self.meeting['id'])['status'], 'scheduled')
        self.assertTrue(self.jobs())

    async def test_stale_cancel_does_not_cancel_native_event(self):
        self.event.start_time = datetime.fromtimestamp(self.now + 10 * 86400, timezone.utc)
        with self.assertRaisesRegex(ClubError, 'изменилась'):
            await self.cancel(expected_revision=self.meeting['revision'])
        self.event.cancel.assert_not_awaited()
        self.assertEqual(self.store.meeting(1, self.meeting['id'])['start'], self.now + 10 * 86400)

    async def test_forbidden_native_fetch_leaves_cache_unchanged(self):
        self.guild.fetch_scheduled_event.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason='Forbidden'), {'message': 'no access'})
        with self.assertRaises(discord.Forbidden):
            await self.cancel()
        self.assertEqual(self.store.meeting(1, self.meeting['id'])['status'], 'scheduled')
        self.event.cancel.assert_not_awaited()

"""Book organizer buttons exercise real storage with mocked Discord transport."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import discord

from bookclub.book_controls import (BookAutomationConfirmation, BookControlsView, BookDetailsModal, PlanMeetingsModal,
                                    open_book_controls, panel_content)
from bookclub.forum_tags import TEMPLATES
from bookclub.store import ClubError, parse_time
from bookclub.ui import Club
from test_bookclub import ClubFixture, CONFIG
from test_bookclub_discord import DiscordHarness


class BookControlsTests(ClubFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.h = DiscordHarness()
        self.cog = Club(self.h.bot, self.store)
        self.service = self.cog.service
        # Publication is queued after the durable write; these controls tests
        # inspect the queue request and flush transport explicitly when needed.
        self.service.request_book_refresh = Mock()
        m = self.meeting
        self.h.event(m['event_id'], name=m['name'],
                     start_time=datetime.fromtimestamp(m['start'], timezone.utc),
                     end_time=datetime.fromtimestamp(m['end'], timezone.utc))

    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 1.0

    def current(self):
        return self.store.book(1, self.book['id'])

    def interaction(self, user=99):
        interaction = self.h.interaction(user)
        interaction.edit_original_response = AsyncMock()
        return interaction

    def view(self):
        return BookControlsView(self.cog, self.current(), 99)

    def modal(self):
        return BookDetailsModal(self.cog, self.current(), 99)

    def revoke(self):
        self.h.guild.owner_id = 90
        self.h.members[99].roles = []
        self.store.configure(1, {**CONFIG, 'organizers': [], 'organizer_roles': []})

    async def test_panel_is_private_and_authorized_from_current_interaction(self):
        interaction = self.interaction()
        view = await open_book_controls(self.cog, interaction, self.book['id'])
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        self.h.guild.fetch_member.assert_not_awaited()
        self.h.bot.fetch_channel.assert_not_awaited()
        sent = interaction.followup.send.await_args
        self.assertTrue(sent.kwargs['ephemeral'])
        self.assertIs(sent.kwargs['view'], view)
        self.assertEqual(len(view.status.options), 4)
        self.assertIn('3 встречи по книге + обсуждение эссе', sent.args[0])
        self.h.channels[13].create_thread.assert_not_awaited()

    async def test_panel_opens_while_refresh_holds_lock_and_rest_is_blocked(self):
        rest_gate = asyncio.Event()
        async def blocked_rest(_):
            await rest_gate.wait()
        self.h.guild.fetch_member.side_effect = blocked_rest
        self.h.bot.fetch_channel.side_effect = blocked_rest
        interaction = self.interaction()
        lock = self.service.locks[1]
        async with lock:
            # A timeout only guards against a regression hanging the test. The
            # assertion is structural: the panel completes while refresh cannot.
            view = await asyncio.wait_for(
                open_book_controls(self.cog, interaction, self.book['id']), timeout=0.25)
            self.assertTrue(lock.locked())
            self.assertIs(interaction.followup.send.await_args.kwargs['view'], view)
        self.h.guild.fetch_member.assert_not_awaited()
        self.h.bot.fetch_channel.assert_not_awaited()

    async def test_panel_uses_interaction_roles_instead_of_stale_cached_member(self):
        self.h.guild.owner_id = 90
        self.store.configure(1, {**CONFIG, 'organizers': [], 'organizer_roles': [22]})
        interaction = self.interaction()
        current_member = Mock(spec=discord.Member)
        current_member.id, current_member.bot = 99, False
        current_member.guild, current_member.roles = self.h.guild, []
        interaction.user = current_member
        self.assertEqual(self.h.members[99].roles[0].id, 22)
        with self.assertRaisesRegex(ClubError, 'организатор'):
            await open_book_controls(self.cog, interaction, self.book['id'])
        interaction.followup.send.assert_not_awaited()
        self.h.guild.fetch_member.assert_not_awaited()

    async def test_panel_fails_closed_without_cached_forum_access(self):
        self.h.channels[13].permissions_for.return_value.view_channel = False
        interaction = self.interaction()
        with self.assertRaisesRegex(ClubError, 'доступа'):
            await open_book_controls(self.cog, interaction, self.book['id'])
        interaction.followup.send.assert_not_awaited()
        del self.h.channels[13]
        with self.assertRaisesRegex(ClubError, 'доступа'):
            await open_book_controls(self.cog, interaction, self.book['id'])
        interaction.followup.send.assert_not_awaited()
        self.h.bot.fetch_channel.assert_not_awaited()

    async def test_panel_rejects_disabled_guild_without_showing_book(self):
        self.service.guild_ids = {2}
        interaction = self.interaction()
        with self.assertRaisesRegex(ClubError, 'отключён'):
            await open_book_controls(self.cog, interaction, self.book['id'])
        interaction.followup.send.assert_not_awaited()

    async def test_member_cannot_open_panel(self):
        interaction = self.interaction(1)
        with self.assertRaisesRegex(ClubError, 'организатор'):
            await open_book_controls(self.cog, interaction, self.book['id'])
        interaction.followup.send.assert_not_awaited()

    async def test_private_controls_bind_guild_and_actor_before_any_response(self):
        view, modal = self.view(), self.modal()
        confirmation = BookAutomationConfirmation(self.cog, self.current(), 99)
        for action in (view.change_status, view.edit_details.callback, view.edit_plan.callback,
                       view.meetings.callback, view.automation.callback, confirmation.confirm.callback,
                       confirmation.back.callback, modal.on_submit,
                       PlanMeetingsModal(self.cog, self.current(), 99).on_submit):
            for wrong in ('actor', 'guild', 'guild_object'):
                with self.subTest(action=action, wrong=wrong):
                    interaction = self.interaction(1 if wrong == 'actor' else 99)
                    if wrong == 'guild':
                        interaction.guild_id = 2
                    if wrong == 'guild_object':
                        interaction.guild = SimpleNamespace(id=2)
                    with self.assertRaises(ClubError):
                        await action(interaction)
                    interaction.response.defer.assert_not_awaited()
                    interaction.response.send_modal.assert_not_awaited()

    async def test_panel_rejects_foreign_book_without_showing_it(self):
        self.store.configure(2, CONFIG)
        book = self.store.create_book(2, 'Секретная книга', 'Автор', '', 'foreign')
        interaction = self.interaction()
        with self.assertRaises(ClubError):
            await open_book_controls(self.cog, interaction, book['id'])
        interaction.followup.send.assert_not_awaited()

    async def test_forms_open_without_http_and_use_configured_timezone(self):
        deadline = parse_time('2034-01-01 12:30', 'Europe/Moscow')
        self.store.update_book(1, self.book['id'], deadline=deadline)
        view = self.view()
        interaction = self.interaction()
        await view.edit_details.callback(interaction)
        modal = interaction.response.send_modal.await_args.args[0]
        self.assertIsInstance(modal, BookDetailsModal)
        self.assertEqual(len(modal.children), 4)
        self.assertNotIn('deadline', modal.fields)
        await view.edit_plan.callback(interaction)
        self.assertIsInstance(interaction.response.send_modal.await_args.args[0], PlanMeetingsModal)
        self.h.guild.fetch_member.assert_not_awaited()
        self.h.bot.fetch_channel.assert_not_awaited()

    async def test_form_opening_rechecks_current_interaction_access(self):
        view = self.view()
        self.h.channels[13].permissions_for.return_value.view_channel = False
        for action in (view.edit_details.callback, view.edit_plan.callback):
            with self.assertRaises(ClubError):
                await action(self.interaction())
        self.h.channels[13].permissions_for.return_value.view_channel = True
        self.revoke()
        with self.assertRaisesRegex(ClubError, 'организатор'):
            await view.edit_details.callback(self.interaction())

    async def test_revoked_organizer_cannot_submit_any_change(self):
        view, modal = self.view(), self.modal()
        plan = PlanMeetingsModal(self.cog, self.current(), 99)
        view.status._values = ['read']
        modal.fields['title']._value = 'Изменено'
        plan.count._value = '4'
        self.revoke()
        before = self.current()
        for action in (view.change_status, modal.on_submit, plan.on_submit):
            with self.assertRaisesRegex(ClubError, 'организатор'):
                await action(self.interaction())
        self.assertEqual(self.current(), before)

    async def test_write_rechecks_rest_permissions_when_open_panel_member_is_stale(self):
        interaction = self.interaction()
        view = await open_book_controls(self.cog, interaction, self.book['id'])
        self.h.guild.owner_id = 90
        self.store.configure(1, {**CONFIG, 'organizers': [], 'organizer_roles': [22]})
        fresh_member = Mock(spec=discord.Member)
        fresh_member.id, fresh_member.bot, fresh_member.roles = 99, False, []
        self.h.guild.fetch_member.side_effect = None
        self.h.guild.fetch_member.return_value = fresh_member
        before = self.current()
        view.status._values = ['reading']
        self.assertTrue(self.service.interaction_organizer(interaction))
        with self.assertRaisesRegex(ClubError, 'организатор'):
            await view.change_status(interaction)
        self.assertEqual(self.current(), before)
        self.h.guild.fetch_member.assert_awaited_once_with(99)
        self.h.bot.fetch_channel.assert_awaited_once_with(13)
        interaction.edit_original_response.assert_not_awaited()

    async def test_submit_rechecks_forum_access_through_rest(self):
        modal = self.modal()
        self.h.channels[13].permissions_for.return_value.read_message_history = False
        with self.assertRaisesRegex(ClubError, 'доступа'):
            await modal.on_submit(self.interaction())
        self.h.bot.fetch_channel.assert_awaited_once_with(13)

    async def test_stale_forms_and_status_do_not_overwrite_newer_book(self):
        view, modal = self.view(), self.modal()
        plan = PlanMeetingsModal(self.cog, self.current(), 99)
        view.status._values = ['reading']
        modal.fields['title']._value = 'Старое исправление'
        plan.count._value = '4'
        self.store.update_book(1, self.book['id'], title='Изменил другой организатор')
        before = self.current()
        for action in (view.change_status, view.edit_details.callback, view.edit_plan.callback,
                       modal.on_submit, plan.on_submit):
            with self.assertRaisesRegex(ClubError, 'изменена'):
                await action(self.interaction())
        self.assertEqual(self.current(), before)

    async def test_status_updates_existing_card_and_tag_preserving_custom_tags(self):
        forum = self.h.channels[13]
        for index, purpose in enumerate(TEMPLATES['books'], 200):
            tag = SimpleNamespace(id=index, name=f'Свое название {purpose}')
            forum.available_tags.append(tag)
            self.store.bind_tag(1, 13, purpose, index)
        custom = SimpleNamespace(id=300, name='Любимый жанр')
        forum.available_tags.append(custom)
        self.store.set_published(1)
        await self.service.refresh(self.h.guild)
        pub = self.store.publication(f'book:{self.book["id"]}')
        thread = self.h.channels[pub['channel_id']]
        thread.applied_tags.append(custom)
        before_count = forum.create_thread.await_count
        view = self.view()
        view.status._values = ['reading']
        interaction = self.interaction()
        await view.change_status(interaction)
        self.assertEqual(self.current()['status'], 'reading')
        self.service.request_book_refresh.assert_called_once_with(self.h.guild, self.book['id'])
        await self.service.refresh(self.h.guild)
        self.assertEqual(self.store.publication(pub['key'])['channel_id'], pub['channel_id'])
        self.assertEqual(forum.create_thread.await_count, before_count)
        desired = self.service.forum_tags.bindings(1, forum)['reading']
        self.assertEqual({tag.id for tag in thread.applied_tags}, {desired, custom.id})
        rendered = '\n'.join(message.content for message in thread.messages.values())
        self.assertIn('Читаем', rendered)
        edited = interaction.edit_original_response.await_args
        self.assertIn('**Читаем**', edited.kwargs['content'])
        self.assertEqual(edited.kwargs['view'].book['revision'], self.current()['revision'])

    async def test_second_current_book_requires_finishing_first(self):
        other = self.store.create_book(1, 'Другая', 'Автор', '', 'other')
        self.store.update_book(1, other['id'], status='reading')
        view = self.view()
        view.status._values = ['reading']
        with self.assertRaisesRegex(ClubError, 'текущее чтение'):
            await view.change_status(self.interaction())
        self.assertEqual(self.current()['status'], 'proposed')

    async def test_same_status_is_noop_and_invalid_selection_is_rejected(self):
        self.store.set_book_status_automation(1, self.book['id'], False)
        view = self.view()
        before = self.current()
        view.status._values = ['proposed']
        await view.change_status(self.interaction())
        self.assertEqual(self.current(), before)
        self.service.request_book_refresh.assert_not_called()
        for invalid in ([], ['read', 'reading'], ['deleted']):
            with self.subTest(invalid=invalid):
                view = self.view()
                view.status._values = invalid
                with self.assertRaises(ClubError):
                    await view.change_status(self.interaction())
        self.assertEqual(self.current(), before)

    async def test_edit_preserves_book_id_essays_meetings_and_participants(self):
        self.store.register_essay(1, self.book['id'], 567, 567, 1, 'Эссе', 'https://example.org/essay')
        before_meeting = self.store.meeting(1, self.meeting['id'])
        before_essays = self.store.essays(self.book['id'], submitted_only=False)
        before_members = self.store.participants(self.book['id'])
        modal = self.modal()
        for name, value in dict(title='Новое название', author='Уточнённый автор', position='5',
                                materials='https://example.org/new').items():
            modal.fields[name]._value = value
        await modal.on_submit(self.interaction())
        current = self.current()
        self.assertEqual((current['title'], current['author'], current['position']),
                         ('Новое название', 'Уточнённый автор', 5))
        self.assertIsNone(current['deadline'])
        self.assertEqual(self.store.meeting(1, self.meeting['id']), before_meeting)
        self.assertEqual(self.store.essays(self.book['id'], submitted_only=False), before_essays)
        self.assertEqual(self.store.participants(self.book['id']), before_members)

    async def test_empty_deadline_and_materials_clear_without_changing_title(self):
        self.store.update_book(1, self.book['id'], deadline=self.now + 5000)
        modal = self.modal()
        modal.fields['materials']._value = ''
        await modal.on_submit(self.interaction())
        self.assertIsNone(self.current()['deadline'])
        self.assertEqual(self.current()['materials'], '')
        self.assertEqual(self.current()['title'], 'Книга')

    async def test_invalid_modal_fields_leave_book_unchanged(self):
        before = self.current()
        for field, value in [('position', '--1'), ('position', '+-1'), ('position', '1.5'),
                             ('position', '1000001'), ('position', '-1000001'), ('position', '١'),
                             ('title', '  '), ('author', 'a' * 181), ('materials', 'x' * 4001)]:
            with self.subTest(field=field, value=value[:30]):
                modal = self.modal()
                modal.fields[field]._value = value
                with self.assertRaises(ClubError):
                    await modal.on_submit(self.interaction())
        self.assertEqual(self.current(), before)

    async def test_edit_description_preserves_signed_and_legacy_queue_positions(self):
        for position in (-1, 0, -1_000_001, 1_000_001, -9_223_372_036_854_775_808):
            with self.subTest(position=position):
                self.store.update_book(1, self.book['id'], position=position)
                modal = self.modal()
                modal.fields['materials']._value = f'Описание для порядка {position}'
                await modal.on_submit(self.interaction())
                self.assertEqual(self.current()['position'], position)
                self.assertEqual(self.current()['materials'], f'Описание для порядка {position}')

    async def test_queue_form_accepts_zero_and_negative_priorities(self):
        other = self.store.create_book(1, 'Позже', 'Автор', '', 'later')
        for value in ('0', '-1', '+3', '-1000000', '1000000'):
            with self.subTest(value=value):
                modal = self.modal()
                modal.fields['position']._value = value
                await modal.on_submit(self.interaction())
                self.assertEqual(self.current()['position'], int(value))
                expected_first = self.book['id'] if int(value) < other['position'] else other['id']
                self.assertEqual(self.store.books(1)[0]['id'], expected_first)

    async def test_reading_plan_has_one_extra_essay_meeting_without_changing_events(self):
        meetings = self.store.rows('SELECT * FROM bc_meetings WHERE book_id=?', (self.book['id'],))
        plan = PlanMeetingsModal(self.cog, self.current(), 99)
        plan.count._value = '4'
        interaction = self.interaction()
        await plan.on_submit(interaction)
        self.assertEqual(self.current()['reading_meetings'], 4)
        self.assertIn('4 встречи по книге + обсуждение эссе', panel_content(self.cog, self.current()))
        self.assertEqual(self.store.rows('SELECT * FROM bc_meetings WHERE book_id=?', (self.book['id'],)), meetings)
        self.h.guild.create_scheduled_event.assert_not_awaited()
        self.h.events[self.meeting['event_id']].edit.assert_not_awaited()
        self.h.events[self.meeting['event_id']].cancel.assert_not_awaited()
        plan = PlanMeetingsModal(self.cog, self.current(), 99)
        plan.count._value = ''
        await plan.on_submit(self.interaction())
        self.assertIsNone(self.current()['reading_meetings'])
        self.assertEqual(self.store.rows('SELECT * FROM bc_meetings WHERE book_id=?', (self.book['id'],)), meetings)

    async def test_invalid_reading_plan_cannot_change_events_or_book(self):
        before = self.current()
        for invalid in ('0', '-1', '101', '1.5', 'все', '١'):
            with self.subTest(invalid=invalid):
                modal = PlanMeetingsModal(self.cog, self.current(), 99)
                modal.count._value = invalid
                with self.assertRaises(ClubError):
                    await modal.on_submit(self.interaction())
        self.assertEqual(self.current(), before)
        self.h.guild.create_scheduled_event.assert_not_awaited()

    async def test_automation_requires_plan_and_explicit_rule_confirmation(self):
        self.store.set_book_status_automation(1, self.book['id'], False)
        view = self.view()
        self.assertEqual(view.automation.label, 'Включить автостатус')
        self.assertIn('Автостатус выключен', panel_content(self.cog, self.current()))
        initial = self.interaction()
        await view.automation.callback(initial)
        self.assertIn('3 встреч по книге', initial.followup.send.await_args.args[0])
        self.store.update_book(1, self.book['id'], reading_meetings=4)
        before = self.current()
        view, interaction = self.view(), self.interaction()
        await view.automation.callback(interaction)
        sent = interaction.followup.send.await_args
        self.assertTrue(sent.kwargs['ephemeral'])
        self.assertIn('4 встреч по книге + 1 обсуждение эссе', sent.args[0])
        self.assertIn('Отменённые встречи не считаются', sent.args[0])
        self.assertIn('явно укажите её роль', sent.args[0])
        confirmation = sent.kwargs['view']
        self.assertIsInstance(confirmation, BookAutomationConfirmation)
        self.assertEqual(self.current(), before)
        confirmed = self.interaction()
        await confirmation.confirm.callback(confirmed)
        current = self.current()
        self.assertTrue(current['status_automation'])
        self.assertTrue(current['status_automation_pending'])
        self.assertEqual(current['status'], before['status'])
        self.assertEqual(confirmed.edit_original_response.await_args.kwargs['view'].automation.label,
                         'Выключить автостатус')
        self.h.guild.create_scheduled_event.assert_not_awaited()
        self.h.events[self.meeting['event_id']].edit.assert_not_awaited()

    async def test_automation_confirmation_rechecks_authorization_and_plan_revision(self):
        self.store.set_book_status_automation(1, self.book['id'], False)
        self.store.update_book(1, self.book['id'], reading_meetings=4)
        confirmation = BookAutomationConfirmation(self.cog, self.current(), 99)
        self.store.update_book(1, self.book['id'], reading_meetings=3)
        with self.assertRaisesRegex(ClubError, 'изменена'):
            await confirmation.confirm.callback(self.interaction())
        self.assertFalse(self.current()['status_automation'])
        confirmation = BookAutomationConfirmation(self.cog, self.current(), 99)
        self.revoke()
        with self.assertRaisesRegex(ClubError, 'организатор'):
            await confirmation.confirm.callback(self.interaction())
        self.assertFalse(self.current()['status_automation'])

    async def test_automation_can_be_disabled_without_changing_status(self):
        self.store.update_book(1, self.book['id'], reading_meetings=4)
        self.store.set_book_status_automation(1, self.book['id'], True)
        before = self.current()
        interaction = self.interaction()
        await self.view().automation.callback(interaction)
        current = self.current()
        self.assertFalse(current['status_automation'])
        self.assertEqual(current['status'], before['status'])
        self.assertIn('Автостатус выключен', interaction.edit_original_response.await_args.kwargs['content'])

    async def test_manual_confirmation_of_same_status_disables_automation(self):
        self.store.update_book(1, self.book['id'], reading_meetings=4)
        self.store.set_book_status_automation(1, self.book['id'], True)
        before = self.current()
        view = self.view()
        view.status._values = [before['status']]
        await view.change_status(self.interaction())
        self.assertEqual(self.current()['status'], before['status'])
        self.assertFalse(self.current()['status_automation'])
        self.service.request_book_refresh.assert_called_once_with(self.h.guild, self.book['id'])

    async def test_all_saves_finish_while_guild_refresh_lock_is_held(self):
        gate = asyncio.Event()

        async def blocked_refresh(_):
            await gate.wait()

        self.service.refresh = AsyncMock(side_effect=blocked_refresh)
        for kind in ('status', 'details', 'plan', 'automation_off', 'automation_on'):
            with self.subTest(kind=kind):
                if kind == 'automation_off':
                    self.store.set_book_status_automation(1, self.book['id'], True)
                elif kind == 'automation_on':
                    self.store.set_book_status_automation(1, self.book['id'], False)
                before = self.current()
                if kind == 'status':
                    view = self.view()
                    view.status._values = ['read']
                    action = view.change_status
                elif kind == 'details':
                    modal = self.modal()
                    modal.fields['title']._value = 'Быстрое изменение'
                    action = modal.on_submit
                elif kind == 'plan':
                    modal = PlanMeetingsModal(self.cog, self.current(), 99)
                    modal.count._value = '4'
                    action = modal.on_submit
                elif kind == 'automation_off':
                    action = self.view().automation.callback
                else:
                    action = BookAutomationConfirmation(self.cog, self.current(), 99).confirm.callback
                self.service.request_book_refresh.reset_mock()
                interaction = self.interaction()

                async def observe_response(**kwargs):
                    self.service.request_book_refresh.assert_called_once_with(self.h.guild, self.book['id'])
                    self.assertGreater(self.current()['revision'], before['revision'])
                    self.assertEqual(kwargs['view'].book, self.current())

                interaction.edit_original_response.side_effect = observe_response
                async with self.service.locks[1]:
                    await asyncio.wait_for(action(interaction), timeout=0.25)
                    self.assertTrue(self.service.locks[1].locked())
                    interaction.edit_original_response.assert_awaited_once()
                self.service.refresh.assert_not_awaited()
        # Every write still fetches fresh member and forum permissions.
        self.assertEqual(self.h.guild.fetch_member.await_count, 5)
        self.assertEqual(self.h.bot.fetch_channel.await_count, 5)

    async def test_revision_change_during_rest_authorization_rejects_submission(self):
        view = self.view()
        view.status._values = ['read']
        actor = self.service.actor

        async def racing_authorization(guild, actor_id):
            result = await actor(guild, actor_id)
            self.store.update_book(1, self.book['id'], title='Правка другого организатора')
            return result

        self.service.actor = racing_authorization
        interaction = self.interaction()
        async with self.service.locks[1]:
            with self.assertRaisesRegex(ClubError, 'изменена'):
                await asyncio.wait_for(view.change_status(interaction), timeout=0.25)
        self.assertEqual(self.current()['title'], 'Правка другого организатора')
        self.assertEqual(self.current()['status'], 'proposed')
        self.service.request_book_refresh.assert_not_called()
        interaction.edit_original_response.assert_not_awaited()

    async def test_slash_book_edit_saves_and_replies_while_publication_lock_is_held(self):
        before = self.current()
        interaction = self.interaction()
        ctx = SimpleNamespace(guild=self.h.guild, author=self.h.members[99],
                              interaction=interaction, command=self.cog.book_edit)
        update_book = self.store.update_book
        self.store.update_book = Mock(wraps=update_book)

        async def blocked_refresh(_):
            await asyncio.Event().wait()

        self.service.refresh = AsyncMock(side_effect=blocked_refresh)

        async def observe_response(content, **kwargs):
            self.assertEqual(self.current()['status'], 'read')
            self.assertEqual(self.current()['title'], 'Правка slash-командой')
            self.assertTrue(kwargs['ephemeral'])
            self.assertIn('Сохранено', content)
            self.service.request_book_refresh.assert_called_once_with(self.h.guild, self.book['id'])

        interaction.followup.send.side_effect = observe_response
        async with self.service.locks[1]:
            await asyncio.wait_for(self.cog.book_edit.callback(
                self.cog, ctx, self.book['id'], status='Прочитано', title='Правка slash-командой',
                reading_meetings='4'), timeout=0.25)
            self.assertTrue(self.service.locks[1].locked())
        interaction.followup.send.assert_awaited_once()
        self.service.refresh.assert_not_awaited()
        self.assertEqual(self.store.update_book.call_args.kwargs['expected_revision'], before['revision'])
        self.assertEqual(self.store.update_book.call_args.kwargs['status_actor_id'], 99)
        self.assertFalse(self.current()['status_automation'])
        self.h.guild.fetch_member.assert_awaited_once_with(99)
        self.h.bot.fetch_channel.assert_awaited_once_with(13)

    async def test_slash_book_edit_rechecks_fresh_organizer_permissions_before_saving(self):
        interaction = self.interaction()
        ctx = SimpleNamespace(guild=self.h.guild, author=self.h.members[99],
                              interaction=interaction, command=self.cog.book_edit)
        self.h.guild.owner_id = 90
        self.store.configure(1, {**CONFIG, 'organizers': [], 'organizer_roles': [22]})
        fresh_member = Mock(spec=discord.Member)
        fresh_member.id, fresh_member.bot, fresh_member.roles = 99, False, []
        self.h.guild.fetch_member.side_effect = None
        self.h.guild.fetch_member.return_value = fresh_member
        self.assertTrue(self.service.interaction_organizer(interaction))
        before = self.current()
        async with self.service.locks[1]:
            with self.assertRaisesRegex(ClubError, 'организатор'):
                await asyncio.wait_for(self.cog.book_edit.callback(
                    self.cog, ctx, self.book['id'], status='Прочитано'), timeout=0.25)
        self.assertEqual(self.current(), before)
        self.service.request_book_refresh.assert_not_called()
        interaction.followup.send.assert_not_awaited()
        self.h.guild.fetch_member.assert_awaited_once_with(99)
        self.h.bot.fetch_channel.assert_awaited_once_with(13)


if __name__ == '__main__':
    unittest.main()

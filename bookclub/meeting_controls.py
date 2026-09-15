"""Private organizer controls backed by native Discord scheduled events."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import discord

from .render import meeting_lines, safe, time_label, plan_label
from .store import ClubError
from .meeting_actions import create_meeting, move_meeting, edit_meeting, cancel_meeting, _organizer, _current


async def _reply(interaction, text, **kwargs):
    from .ui import reply
    await reply(interaction, text, **kwargs)


async def _error(interaction, error):
    from .ui import interaction_error
    await interaction_error(interaction, error)


class OrganizerView(discord.ui.View):
    def __init__(self, cog, guild_id, actor_id):
        super().__init__(timeout=300)
        self.cog, self.guild_id, self.actor_id = cog, guild_id, actor_id

    def bound(self, interaction):
        if (interaction.guild_id != self.guild_id or interaction.guild is None
                or interaction.guild.id != self.guild_id):
            raise ClubError('Откройте управление на сервере этой книги.')
        if interaction.user.id != self.actor_id:
            raise ClubError('Это управление открыто другим организатором.')

    def modal_access(self, interaction):
        self.bound(interaction)
        self.cog.service.interaction_access(interaction)
        if not self.cog.service.interaction_organizer(interaction):
            raise ClubError('Управлять встречами может организатор клуба.')

    async def on_error(self, interaction, error, item):
        await _error(interaction, error)


class MeetingModal(discord.ui.Modal):
    def __init__(self, parent, mode, *, request_key=None):
        super().__init__(title={'create': 'Добавить встречу', 'move': 'Дата и длительность', 'edit': 'Название и граница чтения'}[mode], timeout=600)
        self.parent, self.mode, self.request_key = parent, mode, request_key
        self.inputs = {}
        self.current = dict(parent.meeting) if hasattr(parent, 'meeting') else None
        zone = parent.cog.store.settings(parent.guild_id)['timezone']
        if mode in ('create', 'edit'):
            default = self.current['name'] if self.current else f'Обсуждение: {parent.book["title"]}'[:100]
            self.field('name', 'Название встречи', default=default, max_length=100)
        if mode in ('create', 'move'):
            default = datetime.fromtimestamp(self.current['start'], ZoneInfo(zone)).isoformat(' ', timespec='minutes') if self.current else None
            self.field('date', f'Дата · {zone}'[:45], default=default, placeholder='ГГГГ-ММ-ДД ЧЧ:ММ', max_length=40)
            duration = max(1, ((self.current['end'] or self.current['start'] + 5400) - self.current['start']) // 60) if self.current else 90
            self.field('minutes', 'Длительность, минут (1–1440)', default=str(duration), max_length=4)
        if mode in ('create', 'edit'):
            self.field('part', 'Часть книги', default=self.current['part'] if self.current else None,
                       placeholder='Например: первая половина', max_length=200)
            self.field('chapter', 'Последняя глава включительно', default=self.current['chapter'] if self.current else None,
                       placeholder='Например: 5 · Возвращение', max_length=250)

    def field(self, key, label, **kwargs):
        item = discord.ui.TextInput(label=label, **kwargs)
        self.inputs[key] = item
        self.add_item(item)

    async def on_error(self, interaction, error):
        await _error(interaction, error)

    async def on_submit(self, interaction):
        self.parent.bound(interaction)
        await interaction.response.defer(ephemeral=True)
        fields = {key: str(value) for key, value in self.inputs.items()}
        if 'minutes' in fields:
            try:
                fields['minutes'] = int(fields['minutes'])
            except ValueError:
                raise ClubError('Длительность — целое число от 1 до 1440 минут.') from None
        cog = self.parent.cog
        if self.mode == 'create':
            meeting = await create_meeting(cog.service, interaction.guild, interaction.user.id,
                                           self.parent.book['id'], request_key=self.request_key, **fields)
            note = 'Встреча добавлена. Её даты синхронизируются с событием Discord.'
        else:
            mutate = move_meeting if self.mode == 'move' else edit_meeting
            meeting = await mutate(cog.service, interaction.guild, interaction.user.id,
                                   self.current['id'], expected_revision=self.current['revision'], **fields)
            note = ('Дата обновлена. При переносе ведущему потребуется подтвердить новые условия.' if self.mode == 'move'
                    else 'Встреча обновлена. Если граница чтения изменилась, ведущему нужно проверить план и снова отметить готовность.')
        await _reply(interaction, note + '\n\n' + '\n'.join(meeting_lines(cog.store, meeting, cog.store.settings(interaction.guild_id))),
                     view=MeetingControls(cog, meeting, interaction.user.id))


class CancelConfirmation(OrganizerView):
    def __init__(self, cog, meeting, actor_id):
        super().__init__(cog, meeting['guild_id'], actor_id)
        self.meeting = dict(meeting)

    @discord.ui.button(label='Отменить эту встречу', style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        self.bound(interaction)
        await interaction.response.defer(ephemeral=True)
        await cancel_meeting(self.cog.service, interaction.guild, interaction.user.id,
                             self.meeting['id'], expected_revision=self.meeting['revision'])
        self.stop()
        await _reply(interaction, 'Встреча отменена, напоминания остановлены. История и планы сохранены.',
                     view=BackToMeetings(self.cog, self.meeting, interaction.user.id))

    @discord.ui.button(label='Оставить встречу', style=discord.ButtonStyle.secondary)
    async def keep(self, interaction, button):
        self.bound(interaction)
        await interaction.response.defer(ephemeral=True)
        await _organizer(self.cog.service, interaction.guild, interaction.user.id)
        self.stop()
        await _reply(interaction, 'Встреча сохранена без изменений.')


class BackToMeetings(OrganizerView):
    def __init__(self, cog, meeting, actor_id):
        super().__init__(cog, meeting['guild_id'], actor_id)
        self.meeting = dict(meeting)

    @discord.ui.button(label='Все встречи книги', style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction, button):
        self.bound(interaction)
        await open_book_meetings(self.cog, interaction, self.meeting['book_id'])


class MeetingControls(BackToMeetings):
    def __init__(self, cog, meeting, actor_id):
        super().__init__(cog, meeting, actor_id)
        for mode, label in [('move', 'Дата и длительность'), ('edit', 'Название и главы')]:
            button = discord.ui.Button(label=label, row=0, disabled=meeting['status'] not in (('scheduled', 'active') if mode == 'edit' else ('scheduled',)))
            async def callback(interaction, selected=mode):
                self.modal_access(interaction)
                current = self.cog.store.meeting(self.guild_id, self.meeting['id'])
                if current['status'] not in (('scheduled', 'active') if selected == 'edit' else ('scheduled',)) or current['revision'] != self.meeting['revision']:
                    raise ClubError('Встреча изменилась. Откройте управление заново.')
                await interaction.response.send_modal(MeetingModal(self, selected))
            button.callback = callback
            self.add_item(button)
        if meeting['event_id']:
            self.add_item(discord.ui.Button(label='Событие Discord', url=f'https://discord.com/events/{meeting["guild_id"]}/{meeting["event_id"]}', row=1))
        cancel = discord.ui.Button(label='Отменить встречу', style=discord.ButtonStyle.danger,
                                   row=1, disabled=meeting['status'] != 'scheduled')
        async def ask_cancel(interaction):
            self.bound(interaction)
            await interaction.response.defer(ephemeral=True)
            async with cog.service.locks[self.guild_id]:
                await _organizer(cog.service, interaction.guild, interaction.user.id)
                current, _ = await _current(cog.service, interaction.guild, self.meeting['id'], self.meeting['revision'])
                if current['status'] != 'scheduled':
                    raise ClubError('Отменять можно только ещё не начавшуюся встречу.')
            text = (f'Отменить «{safe(current["name"])}» — {time_label(current["start"], cog.store.settings(self.guild_id)["timezone"])}?\n'
                    'Напоминания остановятся. История встречи и планы сохранятся. Остальные встречи книги останутся.')
            await _reply(interaction, text, view=CancelConfirmation(cog, current, interaction.user.id))
        cancel.callback = ask_cancel
        self.add_item(cancel)
        self.plan_kind = discord.ui.Select(placeholder='Роль встречи в плане N+1', row=3, options=[
            discord.SelectOption(label='Встреча по книге', value='reading',
                                 default=meeting.get('plan_kind') == 'reading'),
            discord.SelectOption(label='Обсуждение эссе', value='essay',
                                 default=meeting.get('plan_kind') == 'essay'),
        ])
        self.plan_kind.callback = self.change_plan_kind
        self.add_item(self.plan_kind)

    async def change_plan_kind(self, interaction):
        self.bound(interaction)
        await interaction.response.defer(ephemeral=True)
        if len(self.plan_kind.values) != 1 or self.plan_kind.values[0] not in ('reading', 'essay'):
            raise ClubError('Выберите роль встречи: по книге или по эссе.')
        async with self.cog.service.locks[self.guild_id]:
            await _organizer(self.cog.service, interaction.guild, interaction.user.id)
            current = self.cog.store.set_meeting_plan_kind(
                self.guild_id, self.meeting['id'], self.plan_kind.values[0],
                expected_revision=self.meeting['revision'], actor_id=self.actor_id)
            label = 'встреча по книге' if current['plan_kind'] == 'reading' else 'обсуждение эссе'
            text = (f'Роль в плане: **{label}**. Изменение роли само по себе не меняет статус книги.\n\n' +
                    '\n'.join(meeting_lines(self.cog.store, current, self.cog.store.settings(self.guild_id))))
            await _reply(interaction, text, view=MeetingControls(self.cog, current, self.actor_id))
        self.stop()


class BookMeetings(OrganizerView):
    def __init__(self, cog, book, actor_id, meetings, page=0):
        super().__init__(cog, book['guild_id'], actor_id)
        self.book, self.meetings = dict(book), meetings
        self.page_count = max(1, (len(meetings) + 24) // 25)
        self.page = max(0, min(page, self.page_count - 1))
        if meetings:
            options = []
            labels = {'scheduled': 'Запланирована', 'active': 'Идёт', 'completed': 'Завершена',
                      'cancelled': 'Отменена', 'draft': 'Не подтверждена', 'unsupported': 'Требует проверки'}
            for meeting in meetings[self.page * 25:(self.page + 1) * 25]:
                label = labels.get(meeting['status'], meeting['status'])
                if meeting['status'] == 'cancelled' and not meeting.get('event_status_confirmed', 1):
                    label = 'Недоступна · отмена не подтверждена'
                description = f'{label} · {time_label(meeting["start"], cog.store.settings(self.guild_id)["timezone"])}'
                options.append(discord.SelectOption(label=meeting['name'][:100], value=meeting['id'], description=description[:100]))
            select = discord.ui.Select(placeholder='Выберите встречу для изменения', options=options, row=0)
            async def selected(interaction):
                self.bound(interaction)
                allowed = {item['id'] for item in self.meetings[self.page * 25:(self.page + 1) * 25]}
                if len(select.values) != 1 or select.values[0] not in allowed:
                    raise ClubError('Выберите встречу из открытого списка.')
                await open_meeting_controls(cog, interaction, select.values[0])
            select.callback = selected
            self.add_item(select)
        for offset, label in [(-1, 'Назад'), (1, 'Далее')]:
            if self.page_count <= 1:
                break
            button = discord.ui.Button(label=label, row=2,
                                       disabled=not 0 <= self.page + offset < self.page_count)
            async def navigate(interaction, step=offset):
                self.bound(interaction)
                await open_book_meetings(cog, interaction, self.book['id'], page=self.page + step)
            button.callback = navigate
            self.add_item(button)

    @discord.ui.button(label='Добавить встречу', style=discord.ButtonStyle.success, row=1)
    async def add(self, interaction, button):
        self.modal_access(interaction)
        self.cog.store.require_active_book(self.guild_id, self.book['id'])
        await interaction.response.send_modal(MeetingModal(self, 'create', request_key=f'meeting-form:{interaction.id}'))


async def open_book_meetings(cog, interaction, book_id, *, page=0):
    if interaction.guild is None:
        raise ClubError('Откройте книгу на сервере клуба.')
    await interaction.response.defer(ephemeral=True)
    async with cog.service.locks[interaction.guild_id]:
        await _organizer(cog.service, interaction.guild, interaction.user.id)
        book = cog.store.require_active_book(interaction.guild_id, book_id)
        await cog.service.reconcile(interaction.guild)
        meetings = cog.store.rows('''SELECT * FROM bc_meetings WHERE guild_id=? AND book_id=?
            ORDER BY CASE status WHEN 'scheduled' THEN 0 WHEN 'active' THEN 1 WHEN 'draft' THEN 2 ELSE 3 END,
            CASE WHEN status IN ('scheduled','active','draft') THEN start ELSE -start END,id''',
                                  (interaction.guild_id, book_id))
        view = BookMeetings(cog, book, interaction.user.id, meetings, page)
    count = {state: sum(m['status'] == state for m in meetings) for state in ('scheduled', 'active', 'completed', 'cancelled', 'draft')}
    unavailable = sum(m['status'] == 'cancelled' and not m.get('event_status_confirmed', 1) for m in meetings)
    count['cancelled'] -= unavailable
    plan = f'План: {plan_label(book)}.\n'
    text = (f'**Встречи · {safe(book["title"])}**\n' + plan +
            f'Предстоящих: {count["scheduled"]} · идут: {count["active"]} · завершённых: {count["completed"]} · отменённых: {count["cancelled"]}.\n'
            'Добавьте встречу или выберите существующую, чтобы изменить дату, длительность, главы или отменить её.\n'
            f'Время: {cog.store.settings(interaction.guild_id)["timezone"]}. Страница {view.page + 1}/{view.page_count}.')
    if count['draft']:
        text += f'\nНеподтверждённых созданий: {count["draft"]}. Перед повтором проверьте /club recover_event.'
    if unavailable:
        text += (f'\nНедоступных событий: {unavailable}; их отмена не подтверждена. '
                 'Они требуют проверки и не исключены из правила автостатуса.')
    await _reply(interaction, text, view=view)


async def open_meeting_controls(cog, interaction, meeting_id):
    if interaction.guild is None:
        raise ClubError('Откройте встречу на сервере клуба.')
    await interaction.response.defer(ephemeral=True)
    async with cog.service.locks[interaction.guild_id]:
        await _organizer(cog.service, interaction.guild, interaction.user.id)
        current = cog.store.meeting(interaction.guild_id, meeting_id)
        await cog.service.sync_one(interaction.guild, current)
        current = cog.store.meeting(interaction.guild_id, meeting_id)
        text = '\n'.join(meeting_lines(cog.store, current, cog.store.settings(interaction.guild_id)))
    await _reply(interaction, text, view=MeetingControls(cog, current, interaction.user.id))

"""Private organizer controls opened from a persistent book card."""
from __future__ import annotations

from datetime import datetime
import re
from zoneinfo import ZoneInfo

import discord

from .render import safe, time_label
from .service import NO_MENTIONS
from .store import ClubError, STATUSES, parse_time
from .ui import GuardedModal, GuardedView, reply


def _bound(interaction, book, actor_id):
    if (interaction.guild is None or interaction.guild_id != book['guild_id']
            or interaction.guild.id != book['guild_id']):
        raise ClubError('Откройте управление на сервере этой книги.')
    if interaction.user.id != actor_id:
        raise ClubError('Эта панель открыта другим организатором.')


def _current(cog, book):
    current = cog.store.book(book['guild_id'], book['id'])
    if current['revision'] != book['revision']:
        raise ClubError('Книга уже изменена. Откройте «Управление книгой» заново.')
    return current


async def _authorized(cog, interaction, book, actor_id):
    _bound(interaction, book, actor_id)
    _, organizer = await cog.service.actor(interaction.guild, actor_id)
    if not organizer:
        raise ClubError('Управлять книгой может только организатор клуба.')
    return _current(cog, book)


def panel_content(cog, book):
    settings = cog.store.settings(book['guild_id'])
    reading = book.get('reading_meetings')
    plan = ('План встреч не задан.' if reading is None else
            f'План: {reading} встреч по книге + 1 встреча по эссе = {reading + 1} всего.')
    automation = 'Автостатус выключен.'
    if book.get('status_automation'):
        automation = ('Автостатус включён; ожидается новое событие Discord.'
                      if book.get('status_automation_pending') else 'Автостатус включён.')
    return '\n'.join([
        f'**Управление книгой: {safe(book["title"])}**',
        f'Автор: {safe(book["author"])}',
        f'Статус: **{STATUSES[book["status"]]}** · место в очереди: {book["position"]}',
        f'Срок эссе: {time_label(book["deadline"], settings["timezone"])}',
        plan,
        automation + ' Ручной выбор статуса выключает автоматику.',
        'Выберите статус ниже или измените название, автора, очередь, срок и материалы.',
    ])


async def _show_updated(cog, interaction, book_id):
    book = cog.store.book(interaction.guild_id, book_id)
    view = BookControlsView(cog, book, interaction.user.id)
    await interaction.edit_original_response(content=panel_content(cog, book), view=view,
                                             allowed_mentions=NO_MENTIONS)
    return view


async def open_book_controls(cog, interaction, book_id):
    if interaction.guild is None or interaction.guild_id != interaction.guild.id:
        raise ClubError('Откройте управление на сервере клуба.')
    await interaction.response.defer(ephemeral=True)
    # Opening only reads a book snapshot. The current interaction member and
    # gateway channel permissions suffice, just as when opening a modal; do not
    # wait behind a full Discord refresh. Every submission takes the guild lock
    # and repeats REST authorization and the snapshot revision check.
    cog.service.interaction_access(interaction)
    if not cog.service.interaction_organizer(interaction):
        raise ClubError('Управлять книгой может только организатор клуба.')
    book = cog.store.book(interaction.guild_id, book_id)
    view = BookControlsView(cog, book, interaction.user.id)
    await reply(interaction, panel_content(cog, book), view=view)
    return view


class BookControlsView(GuardedView):
    def __init__(self, cog, book, actor_id):
        super().__init__(timeout=600)
        self.cog, self.book, self.actor_id = cog, dict(book), actor_id
        self.status = discord.ui.Select(placeholder='Изменить статус книги', options=[
            discord.SelectOption(label=label, value=key, default=key == book['status'])
            for key, label in STATUSES.items()
        ], row=0)
        self.status.callback = self.change_status
        self.add_item(self.status)
        self.automation.label = 'Выключить автостатус' if book.get('status_automation') else 'Включить автостатус'

    async def change_status(self, interaction):
        _bound(interaction, self.book, self.actor_id)
        await interaction.response.defer(ephemeral=True)
        async with self.cog.service.locks[interaction.guild_id]:
            current = await _authorized(self.cog, interaction, self.book, self.actor_id)
            if len(self.status.values) != 1 or self.status.values[0] not in STATUSES:
                raise ClubError('Выберите один из предложенных статусов книги.')
            status = self.status.values[0]
            if status != current['status'] or current.get('status_automation'):
                self.cog.store.update_book(interaction.guild_id, current['id'], status=status,
                                           expected_revision=current['revision'], status_actor_id=self.actor_id)
                await self.cog.service.refresh(interaction.guild)
            await _show_updated(self.cog, interaction, current['id'])
        self.stop()

    @discord.ui.button(label='Изменить описание', style=discord.ButtonStyle.secondary, row=1)
    async def edit_details(self, interaction, button):
        _bound(interaction, self.book, self.actor_id)
        # Send a modal within Discord's response deadline. The submission repeats
        # authorization with fresh member and forum data before any write.
        self.cog.service.interaction_access(interaction)
        if not self.cog.service.interaction_organizer(interaction):
            raise ClubError('Управлять книгой может только организатор клуба.')
        current = _current(self.cog, self.book)
        await interaction.response.send_modal(BookDetailsModal(self.cog, current, self.actor_id))

    @discord.ui.button(label='План встреч', style=discord.ButtonStyle.secondary, row=1)
    async def edit_plan(self, interaction, button):
        _bound(interaction, self.book, self.actor_id)
        self.cog.service.interaction_access(interaction)
        if not self.cog.service.interaction_organizer(interaction):
            raise ClubError('Управлять книгой может только организатор клуба.')
        current = _current(self.cog, self.book)
        await interaction.response.send_modal(PlanMeetingsModal(self.cog, current, self.actor_id))

    @discord.ui.button(label='Встречи', style=discord.ButtonStyle.secondary, row=1)
    async def meetings(self, interaction, button):
        _bound(interaction, self.book, self.actor_id)
        _current(self.cog, self.book)
        from .meeting_controls import open_book_meetings
        await open_book_meetings(self.cog, interaction, self.book['id'])

    @discord.ui.button(label='Включить автостатус', style=discord.ButtonStyle.secondary, row=2)
    async def automation(self, interaction, button):
        _bound(interaction, self.book, self.actor_id)
        await interaction.response.defer(ephemeral=True)
        async with self.cog.service.locks[interaction.guild_id]:
            current = await _authorized(self.cog, interaction, self.book, self.actor_id)
            if current.get('status_automation'):
                self.cog.store.set_book_status_automation(
                    interaction.guild_id, current['id'], False,
                    expected_revision=current['revision'], actor_id=self.actor_id)
                await _show_updated(self.cog, interaction, current['id'])
                self.stop()
                return
            if current.get('reading_meetings') is None:
                raise ClubError('Сначала задайте «План встреч»: количество встреч по книге плюс одна по эссе.')
            reading = current['reading_meetings']
            text = (
                f'**Автостатус · {safe(current["title"])}**\n'
                f'Проверьте правило: {reading} встреч по книге + 1 обсуждение эссе. '
                'В управлении каждой встречей явно укажите её роль в плане.\n'
                'После нового фактического старта встречи книга станет «Читаем». '
                'Когда завершатся все встречи плана, включая одну по эссе, — «Прочитано». '
                'Отменённые встречи не считаются завершёнными; назначение даты статус не меняет.\n'
                'Включение сохранит текущий статус. Уже известные события повторно его не изменят. '
                'Ручной выбор статуса выключит автоматику.')
            await reply(interaction, text, view=BookAutomationConfirmation(self.cog, current, self.actor_id))


class BookAutomationConfirmation(GuardedView):
    def __init__(self, cog, book, actor_id):
        super().__init__(timeout=300)
        self.cog, self.book, self.actor_id = cog, dict(book), actor_id

    @discord.ui.button(label='Подтвердить правило N+1', style=discord.ButtonStyle.success)
    async def confirm(self, interaction, button):
        _bound(interaction, self.book, self.actor_id)
        await interaction.response.defer(ephemeral=True)
        async with self.cog.service.locks[interaction.guild_id]:
            current = await _authorized(self.cog, interaction, self.book, self.actor_id)
            if current.get('reading_meetings') is None:
                raise ClubError('Сначала задайте план встреч книги.')
            self.cog.store.set_book_status_automation(
                interaction.guild_id, current['id'], True,
                expected_revision=current['revision'], actor_id=self.actor_id)
            await _show_updated(self.cog, interaction, current['id'])
        self.stop()

    @discord.ui.button(label='Назад', style=discord.ButtonStyle.secondary)
    async def back(self, interaction, button):
        _bound(interaction, self.book, self.actor_id)
        await open_book_controls(self.cog, interaction, self.book['id'])
        self.stop()


class BookDetailsModal(GuardedModal):
    def __init__(self, cog, book, actor_id):
        super().__init__(title='Название, очередь и срок эссе', timeout=600)
        self.cog, self.book, self.actor_id = cog, dict(book), actor_id
        zone = cog.store.settings(book['guild_id'])['timezone']
        deadline = (datetime.fromtimestamp(book['deadline'], ZoneInfo(zone)).isoformat(timespec='minutes')
                    if book['deadline'] is not None else '')
        self.fields = {}
        for key, label, default, required, limit in [
            ('title', 'Название книги', book['title'], True, 180),
            ('author', 'Автор книги', book['author'], True, 180),
            ('position', 'Порядок в очереди (меньше — раньше)', str(book['position']), True, 20),
            ('deadline', 'Срок эссе (пусто — убрать)', deadline, False, 40),
            ('materials', 'Материалы и ссылки', book['materials'], False, 4000),
        ]:
            field = discord.ui.TextInput(label=label, default=default, required=required, max_length=limit,
                                         style=discord.TextStyle.paragraph if key == 'materials' else discord.TextStyle.short,
                                         placeholder=f'ГГГГ-ММ-ДД ЧЧ:ММ · {zone}' if key == 'deadline' else None)
            self.fields[key] = field
            self.add_item(field)

    async def on_submit(self, interaction):
        _bound(interaction, self.book, self.actor_id)
        await interaction.response.defer(ephemeral=True)
        async with self.cog.service.locks[interaction.guild_id]:
            current = await _authorized(self.cog, interaction, self.book, self.actor_id)
            fields = {key: str(field.value).strip() for key, field in self.fields.items()}
            raw_position = fields['position']
            if not re.fullmatch(r'[+-]?[0-9]+', raw_position):
                raise ClubError('Порядок в очереди: целое число от -1000000 до 1000000. Меньшее число идёт раньше.')
            fields['position'] = int(raw_position)
            if abs(fields['position']) > 1_000_000 and fields['position'] != current['position']:
                raise ClubError('Новый порядок в очереди: целое число от -1000000 до 1000000.')
            fields['deadline'] = (parse_time(fields['deadline'], self.cog.store.settings(interaction.guild_id)['timezone'])
                                  if fields['deadline'] else None)
            self.cog.store.update_book(interaction.guild_id, current['id'], **fields,
                                       expected_revision=current['revision'])
            await self.cog.service.refresh(interaction.guild)
            await _show_updated(self.cog, interaction, current['id'])
        self.stop()


class PlanMeetingsModal(GuardedModal):
    def __init__(self, cog, book, actor_id):
        super().__init__(title='План: встречи по книге + 1 по эссе', timeout=600)
        self.cog, self.book, self.actor_id = cog, dict(book), actor_id
        current = book.get('reading_meetings')
        self.count = discord.ui.TextInput(label='Встреч по книге (без встречи по эссе)',
                                         default='' if current is None else str(current), required=False,
                                         max_length=3, placeholder='1–100; пусто — убрать план. Даты не меняются.')
        self.add_item(self.count)

    async def on_submit(self, interaction):
        _bound(interaction, self.book, self.actor_id)
        await interaction.response.defer(ephemeral=True)
        async with self.cog.service.locks[interaction.guild_id]:
            current = await _authorized(self.cog, interaction, self.book, self.actor_id)
            raw = str(self.count.value).strip()
            if raw and (not raw.isascii() or not raw.isdecimal() or not 1 <= int(raw) <= 100):
                raise ClubError('План: от 1 до 100 встреч по книге; ещё одна встреча посвящена эссе. Пустое поле убирает план.')
            reading_meetings = int(raw) if raw else None
            self.cog.store.update_book(interaction.guild_id, current['id'], reading_meetings=reading_meetings,
                                       expected_revision=current['revision'])
            await self.cog.service.refresh(interaction.guild)
            await _show_updated(self.cog, interaction, current['id'])
        self.stop()

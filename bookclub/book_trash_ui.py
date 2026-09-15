"""Private removal choices, frozen previews and organizer confirmations."""
from __future__ import annotations

from copy import deepcopy

import discord

from .book_removal import (build_removal_plan, commit_removal_plan, get_removal_operation,
                           latest_book_removal_operation, removal_resources, retry_removal_operation)
from .render import book_url, safe
from .service import NO_MENTIONS
from .store import ClubError, STATUSES
from .ui import GuardedModal, GuardedView, reply


PAGE_SIZE = 25
REMOVAL_MODES = {
    'keep': ('Убрать из каталога', 'Сохранить тему книги и все эссе'),
    'topic': ('Удалить тему книги', 'Сохранить эссе, удалить только тему книги'),
    'all': ('Удалить тему книги и эссе', 'Безвозвратно удалить связанные темы в Discord'),
    'transfer': ('Перенести эссе к другой книге', 'Убрать дубликат из каталога, сохранить его тему'),
    'transfer_topic': ('Перенести эссе и удалить тему', 'Эссе сохранить у другой книги, тему дубликата удалить'),
}
TRANSFER_MODES = {'transfer', 'transfer_topic'}
DESTRUCTIVE_MODES = {'topic', 'all', 'transfer_topic'}


def _short(value, limit):
    return safe(value if len(value) <= limit else value[:limit - 1] + '…')


def _bound(interaction, guild_id, actor_id=None):
    if (interaction.guild is None or interaction.guild_id != guild_id
            or interaction.guild.id != guild_id):
        raise ClubError('Откройте управление на сервере этой книги.')
    if actor_id is not None and interaction.user.id != actor_id:
        raise ClubError('Эта панель открыта другим организатором.')


def _preflight(cog, interaction):
    _bound(interaction, interaction.guild_id)
    cog.service.interaction_access(interaction)
    if not cog.service.interaction_organizer(interaction):
        raise ClubError('Удалять и восстанавливать книги может только организатор клуба.')


def _current(cog, book, *, deleted):
    current = cog.store.book(book['guild_id'], book['id'])
    if current['revision'] != book['revision']:
        raise ClubError('Книга уже изменена. Откройте управление или /club library deleted заново.')
    if bool(current.get('deleted')) != deleted:
        raise ClubError('Книга уже восстановлена.' if deleted else 'Книга уже удалена из каталога.')
    return current


async def _authorized(cog, interaction, book, actor_id, *, deleted):
    _bound(interaction, book['guild_id'], actor_id)
    # The view is only a snapshot. Check the actual member and forum access
    # immediately before the synchronous, optimistic database write.
    _, organizer = await cog.service.actor(interaction.guild, actor_id)
    if not organizer:
        raise ClubError('Удалять и восстанавливать книги может только организатор клуба.')
    return _current(cog, book, deleted=deleted)


def _delete_content(cog, book):
    meetings = cog.store.one('SELECT COUNT(*) AS count FROM bc_meetings WHERE guild_id=? AND book_id=?',
                             (book['guild_id'], book['id']))['count']
    essays = len(cog.store.essays(book['id'], submitted_only=False))
    return (
        f'**Удаление книги «{safe(book["title"])}»**\n'
        f'Автор: {safe(book["author"])}\n'
        f'Связано встреч: {meetings}; работ и черновиков эссе: {essays}.\n\n'
        'Выберите, что сделать с темой и эссе. Если это дубликат, эссе можно '
        'привязать к другой книге с сохранением авторов и сообщений.\n'
        'В любом режиме книга исчезнет из каталога и очереди, а её напоминания остановятся. '
        'События Discord не отменяются. Перед выполнением появится точный план.')


def removal_content(plan):
    book, target = plan['source'], plan.get('target')
    count = sum(not essay.get('deleted') for essay in plan['essays'])
    lines = [f'**Проверка удаления: «{safe(book["title"])}»**',
             f'Автор: {_short(book["author"], 80)} · статус: **{STATUSES[book["status"]]}**',
             f'Действие: **{REMOVAL_MODES[plan["mode"]][0]}**.',
             f'Связано встреч: {plan["meetings_count"]}; работ и черновиков эссе: {count}.']
    if target:
        lines += [f'Перенести все {count} эссе в **«{_short(target["title"], 120)}»** '
                  f'({_short(target["author"], 80)}). Статус этой книги: **{STATUSES[target["status"]]}**.',
                  'Сообщения, вложения, авторы и ссылки на темы эссе сохранятся. '
                  'Ссылки в карточках и привязки эссе обновятся.']
        if plan.get('overlapping_authors'):
            lines.append('У некоторых авторов уже есть эссе в обеих книгах. '
                         'Обе работы сохранятся отдельно; «Открыть моё эссе» покажет их все.')
    if plan['mode'] == 'all':
        lines.append(f'Эссе и их вложения будут удалены из Discord вместе со связанными темами: {count} работ.')
    elif not target:
        lines.append('Эссе и их вложения останутся на месте.')
    if plan['mode'] in DESTRUCTIVE_MODES:
        lines.append(f'**Тем и отдельных сообщений Discord к безвозвратному удалению: {len(plan["delete_threads"])}.** '
                     'Восстановление книги в каталоге не вернёт удалённые сообщения и вложения.')
    else:
        lines.append('Тема исходной книги сохранится.')
    lines += ['Книга будет убрана из каталога. Её напоминания остановятся; '
              'статусы книг, встречи и события Discord сохранятся. События не отменяются.',
              'План зафиксирован. Если книга или набор эссе изменится, потребуется новое подтверждение.']
    return '\n\n'.join(lines)


def _restore_content(book, operation=None):
    text = (
        f'**Восстановить «{safe(book["title"])}»?**\n'
        f'Автор: {safe(book["author"])}\n\n'
        'Книга вернётся в каталог с сохранёнными статусом и порядком чтения. '
        'Сохранившиеся тема, эссе и встречи останутся привязанными к ней. '
        'Будущие напоминания бота возобновятся по актуальному плану. '
        'Автостатус останется выключенным; его можно включить в управлении книгой.')
    if operation and operation['mode'] in TRANSFER_MODES:
        text += '\nПеренесённые эссе останутся у другой книги; восстановление не перенесёт их обратно.'
    if operation and operation['mode'] in DESTRUCTIVE_MODES:
        text += ('\nУдалённые из Discord темы, сообщения и вложения не восстановятся. '
                 'Если тема книги была удалена, при публикации будет создана новая тема с новым ID.')
    return text


async def open_delete_book(cog, interaction, book_id):
    _bound(interaction, interaction.guild_id)
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)
    _preflight(cog, interaction)
    book = cog.store.book(interaction.guild_id, book_id)
    if book.get('deleted'):
        raise ClubError('Книга уже удалена из каталога. Восстановление: /club library deleted.')
    view = RemovalModeView(cog, book, interaction.user.id)
    await reply(interaction, _delete_content(cog, book), view=view)
    return view


async def open_restore_book(cog, interaction, book_id):
    _bound(interaction, interaction.guild_id)
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)
    _preflight(cog, interaction)
    book = cog.store.book(interaction.guild_id, book_id)
    if not book.get('deleted'):
        raise ClubError('Книга уже находится в каталоге.')
    view, content = _restore_panel(cog, book, interaction.user.id)
    await reply(interaction, content, view=view)
    return view


class BookTrashConfirmation(GuardedView):
    def __init__(self, cog, book, actor_id):
        super().__init__(timeout=300)
        self.cog, self.book, self.actor_id = cog, dict(book), actor_id

    @discord.ui.button(label='Отмена', style=discord.ButtonStyle.secondary, row=1)
    async def cancel(self, interaction, button):
        _bound(interaction, self.book['guild_id'], self.actor_id)
        await interaction.response.defer(ephemeral=True)
        _preflight(self.cog, interaction)
        self.stop()
        await interaction.edit_original_response(content='Действие отменено. Книга не изменена.',
                                                 view=None, allowed_mentions=NO_MENTIONS)


class RemovalModeView(BookTrashConfirmation):
    def __init__(self, cog, book, actor_id):
        super().__init__(cog, book, actor_id)
        self.mode = discord.ui.Select(placeholder='Выберите действие с темой и эссе', row=0,
                                     options=[discord.SelectOption(label=label, value=mode,
                                                                    description=description)
                                              for mode, (label, description) in REMOVAL_MODES.items()])
        self.mode.callback = self.choose
        self.add_item(self.mode)

    async def choose(self, interaction):
        _bound(interaction, self.book['guild_id'], self.actor_id)
        await interaction.response.defer(ephemeral=True)
        _preflight(self.cog, interaction)
        book = _current(self.cog, self.book, deleted=False)
        if len(self.mode.values) != 1 or self.mode.values[0] not in REMOVAL_MODES:
            raise ClubError('Выберите один из предложенных вариантов удаления.')
        mode = self.mode.values[0]
        if mode in TRANSFER_MODES:
            view = TransferTargetView(self.cog, book, self.actor_id, mode)
            content = view.content()
        else:
            view = RemoveBookConfirmation(self.cog, book, self.actor_id,
                                          plan=build_removal_plan(self.cog.store, book['guild_id'], book['id'], mode))
            content = removal_content(view.plan)
        self.stop()
        await interaction.edit_original_response(content=content, view=view, allowed_mentions=NO_MENTIONS)


class TransferTargetView(BookTrashConfirmation):
    def __init__(self, cog, book, actor_id, mode):
        super().__init__(cog, book, actor_id)
        self.mode, self.index = mode, 0
        self.select = discord.ui.Select(placeholder='К какой книге привязать эссе?', row=0)
        self.select.callback = self.choose
        self.add_item(self.select)
        self._update()

    def _update(self):
        self.books = [book for book in self.cog.store.books(self.book['guild_id'])
                      if book['id'] != self.book['id']]
        self.page_count = max(1, (len(self.books) + PAGE_SIZE - 1) // PAGE_SIZE)
        self.index = max(0, min(self.index, self.page_count - 1))
        self.visible = {book['id']: book for book in
                        self.books[self.index * PAGE_SIZE:(self.index + 1) * PAGE_SIZE]}
        counts = {row['book_id']: row['count'] for row in self.cog.store.rows(
            'SELECT book_id,COUNT(*) AS count FROM bc_essays WHERE guild_id=? AND deleted=0 GROUP BY book_id',
            (self.book['guild_id'],))}
        self.select.options = [discord.SelectOption(label=book['title'][:100], value=book['id'],
                                                    description=(f'{book["author"][:60]} · '
                                                                 f'{STATUSES[book["status"]]} · '
                                                                 f'эссе: {counts.get(book["id"], 0)}')[:100])
                               for book in self.visible.values()]
        self.select.disabled = not self.visible
        if not self.visible:
            self.select.options = [discord.SelectOption(label='Других активных книг нет', value='empty')]
        self.previous.disabled = self.index == 0
        self.next_page.disabled = self.index + 1 >= self.page_count

    def content(self):
        return (f'**Перенести эссе из «{safe(self.book["title"])}»**\n'
                'Выберите существующую книгу по названию и автору. '
                'Эссе разных книг одного автора сохранятся отдельно.\n'
                + ('Для переноса сначала добавьте другую книгу в каталог.\n' if not self.books else '')
                + f'Страница {self.index + 1}/{self.page_count}.')

    async def _navigate(self, interaction, offset):
        _bound(interaction, self.book['guild_id'], self.actor_id)
        await interaction.response.defer(ephemeral=True)
        _preflight(self.cog, interaction)
        _current(self.cog, self.book, deleted=False)
        self.index += offset
        self._update()
        await interaction.edit_original_response(content=self.content(), view=self, allowed_mentions=NO_MENTIONS)

    @discord.ui.button(label='Предыдущие', style=discord.ButtonStyle.secondary, row=1)
    async def previous(self, interaction, button):
        await self._navigate(interaction, -1)

    @discord.ui.button(label='Следующие', style=discord.ButtonStyle.secondary, row=1)
    async def next_page(self, interaction, button):
        await self._navigate(interaction, 1)

    @discord.ui.button(label='К выбору действия', style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction, button):
        await _show_modes(self, interaction)

    async def choose(self, interaction):
        _bound(interaction, self.book['guild_id'], self.actor_id)
        await interaction.response.defer(ephemeral=True)
        _preflight(self.cog, interaction)
        book = _current(self.cog, self.book, deleted=False)
        if len(self.select.values) != 1 or self.select.values[0] not in self.visible:
            raise ClubError('Выберите книгу на этой странице списка.')
        target = _current(self.cog, self.visible[self.select.values[0]], deleted=False)
        plan = build_removal_plan(self.cog.store, book['guild_id'], book['id'], self.mode,
                                  target_id=target['id'])
        view = RemoveBookConfirmation(self.cog, book, self.actor_id, plan=plan)
        self.stop()
        await interaction.edit_original_response(content=removal_content(plan), view=view, allowed_mentions=NO_MENTIONS)


async def _show_modes(view, interaction):
    _bound(interaction, view.book['guild_id'], view.actor_id)
    await interaction.response.defer(ephemeral=True)
    _preflight(view.cog, interaction)
    book = _current(view.cog, view.book, deleted=False)
    replacement = RemovalModeView(view.cog, book, view.actor_id)
    view.stop()
    await interaction.edit_original_response(content=_delete_content(view.cog, book), view=replacement,
                                             allowed_mentions=NO_MENTIONS)


class RemoveBookConfirmation(BookTrashConfirmation):
    def __init__(self, cog, book, actor_id, *, plan=None):
        super().__init__(cog, book, actor_id)
        self.plan = deepcopy(plan) if plan is not None else build_removal_plan(cog.store, book['guild_id'], book['id'], 'keep')
        self.confirm.label = ('Подтвердить перенос эссе' if self.plan['mode'] in TRANSFER_MODES
                              else 'Убрать из каталога')
        if self.plan['mode'] in DESTRUCTIVE_MODES:
            self.confirm.label = f'Удалить тем/сообщений: {len(self.plan["delete_threads"])}'
        target = self.plan.get('target')
        if target:
            url = book_url(cog.store, target)
            if url:
                self.add_item(discord.ui.Button(label='Открыть выбранную книгу', url=url, row=2))

    @discord.ui.button(label='Подтвердить', style=discord.ButtonStyle.danger, row=0)
    async def confirm(self, interaction, button):
        _bound(interaction, self.book['guild_id'], self.actor_id)
        if self.plan['mode'] in DESTRUCTIVE_MODES:
            _preflight(self.cog, interaction)
            _current(self.cog, self.book, deleted=False)
            await interaction.response.send_modal(RemovalTitleModal(self))
            return
        await self.commit(interaction)

    async def commit(self, interaction):
        _bound(interaction, self.book['guild_id'], self.actor_id)
        await interaction.response.defer(ephemeral=True)
        async def save():
            await _authorized(self.cog, interaction, self.book, self.actor_id, deleted=False)
            return commit_removal_plan(self.cog.store, interaction.guild_id, self.actor_id, self.plan)
        if self.plan['mode'] == 'keep':
            operation = await save()
        else:
            # An import or essay creation already awaiting Discord may still
            # acquire a publication binding. Freeze after that writer finishes;
            # the transaction then verifies every resource in the reviewed plan.
            async with self.cog.service.locks[interaction.guild_id]:
                operation = await save()
        self.stop()
        self.cog.service.request_book_refresh(interaction.guild, self.book['id'])
        if operation.get('target_book_id'):
            self.cog.service.request_book_refresh(interaction.guild, operation['target_book_id'])
        if self.plan['mode'] == 'keep':
            text = (f'«{safe(self.book["title"])}» удалена из каталога. Напоминания бота остановлены. '
                    'Тема, эссе и события сохранены. Восстановление: /club library deleted.')
        else:
            text = (f'Решение для «{safe(self.book["title"])}» сохранено. '
                    'Привязки в каталоге обновлены; изменения в Discord выполняются. '
                    'События Discord не отменены.')
        view = RemovalOperationView(self.cog, self.book, self.actor_id, operation['operation_id'])
        await interaction.edit_original_response(
            content=text,
            view=view, allowed_mentions=NO_MENTIONS)

    @discord.ui.button(label='Назад к выбору', style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction, button):
        await _show_modes(self, interaction)


class RemovalTitleModal(GuardedModal):
    def __init__(self, confirmation):
        super().__init__(title='Подтвердить удаление тем', timeout=300)
        self.confirmation = confirmation
        self.book_title = discord.ui.TextInput(label='Введите название удаляемой книги',
                                               placeholder=confirmation.book['title'][:100],
                                               required=True, max_length=4000)
        self.add_item(self.book_title)

    async def on_submit(self, interaction):
        view = self.confirmation
        _bound(interaction, view.book['guild_id'], view.actor_id)
        if str(self.book_title).strip() != view.book['title'].strip():
            raise ClubError('Название не совпадает. Удаление не выполнено; введите полное название книги.')
        await view.commit(interaction)


def operation_content(operation):
    labels = {'pending': 'Выполняется', 'failed': 'Нужно исправить причину и повторить', 'done': 'Завершено'}
    plan = operation['plan']
    resources = operation.get('resources', [])
    completed = sum(resource['state'] == 'done' for resource in resources)
    lines = [f'**Удаление «{safe(plan["source"]["title"])}»: {labels.get(operation["state"], "Проверяется")}**',
             f'Действие: {REMOVAL_MODES[operation["mode"]][0]}.',
             f'Действия в Discord: {completed}/{len(resources)} завершено.']
    errors = list(dict.fromkeys(resource.get('last_error') for resource in resources
                               if resource['state'] == 'failed' and resource.get('last_error')))
    if errors:
        # Only executor-provided sanitized domain errors are persisted here.
        lines.append('Причина остановки: ' + '; '.join(safe(error)[:300] for error in errors[:3]) + '.')
    if operation['state'] == 'pending':
        lines.append('Изменения книги сохранены. Обновите статус через некоторое время.')
    elif operation['state'] == 'failed':
        lines.append('После исправления причины нажмите «Повторить незавершённое». '
                     'Повтор затронет только тот же подтверждённый набор тем; выполненные действия не повторяются.')
    else:
        lines.append('Выполнение подтверждённого плана завершено. '
                     'Восстановление книги в каталоге не отменяет перенос эссе и не возвращает удалённые темы Discord.')
    return '\n\n'.join(lines)


class RemovalOperationView(GuardedView):
    def __init__(self, cog, book, actor_id, operation_id):
        super().__init__(timeout=600)
        self.cog, self.book, self.actor_id = cog, dict(book), actor_id
        self.operation_id = operation_id
        self._update()

    def _update(self):
        self.operation = get_removal_operation(self.cog.store, self.book['guild_id'], self.operation_id)
        self.operation['resources'] = removal_resources(self.cog.store, self.book['guild_id'],
                                                         self.operation_id, pending_only=False)
        self.retry.disabled = self.operation['state'] != 'failed'

    async def show(self, interaction, *, prefix=''):
        self._update()
        await interaction.edit_original_response(content=prefix + operation_content(self.operation), view=self,
                                                 allowed_mentions=NO_MENTIONS)

    @discord.ui.button(label='Статус операции', style=discord.ButtonStyle.secondary)
    async def status(self, interaction, button):
        _bound(interaction, self.book['guild_id'], self.actor_id)
        await interaction.response.defer(ephemeral=True)
        _preflight(self.cog, interaction)
        await self.show(interaction)

    @discord.ui.button(label='Повторить незавершённое', style=discord.ButtonStyle.primary)
    async def retry(self, interaction, button):
        _bound(interaction, self.book['guild_id'], self.actor_id)
        await interaction.response.defer(ephemeral=True)
        async with self.cog.service.locks[interaction.guild_id]:
            _, organizer = await self.cog.service.actor(interaction.guild, self.actor_id)
            if not organizer:
                raise ClubError('Повторить удаление может только организатор клуба.')
            operation = retry_removal_operation(self.cog.store, interaction.guild_id,
                                                self.operation_id, self.actor_id)
        self.cog.service.request_book_refresh(interaction.guild, operation['source_book_id'])
        if operation.get('target_book_id'):
            self.cog.service.request_book_refresh(interaction.guild, operation['target_book_id'])
        await self.show(interaction, prefix='Повтор незавершённых действий сохранён.\n\n')


def _restore_panel(cog, book, actor_id):
    operation = latest_book_removal_operation(cog.store, book['guild_id'], book['id'])
    if operation and operation['state'] != 'done':
        view = RemovalOperationView(cog, book, actor_id, operation['operation_id'])
        return view, ('Сначала завершите удаление или перенос. Восстановление пока недоступно.\n\n'
                      + operation_content(view.operation))
    return RestoreBookConfirmation(cog, book, actor_id), _restore_content(book, operation)


class RestoreBookConfirmation(BookTrashConfirmation):
    @discord.ui.button(label='Восстановить книгу', style=discord.ButtonStyle.success, row=0)
    async def confirm(self, interaction, button):
        _bound(interaction, self.book['guild_id'], self.actor_id)
        await interaction.response.defer(ephemeral=True)
        current = await _authorized(self.cog, interaction, self.book, self.actor_id, deleted=True)
        self.cog.store.restore_book(interaction.guild_id, current['id'],
                                    expected_revision=current['revision'], actor_id=self.actor_id)
        self.stop()
        self.cog.service.request_book_refresh(interaction.guild, current['id'])
        current = self.cog.store.book(interaction.guild_id, current['id'])
        text = f'«{safe(current["title"])}» восстановлена в каталоге.'
        url = book_url(self.cog.store, current)
        view = None
        if url:
            view = discord.ui.View(timeout=300)
            view.add_item(discord.ui.Button(label='Открыть книгу', url=url))
        await interaction.edit_original_response(content=text, view=view, allowed_mentions=NO_MENTIONS)


class RemovedBookView(GuardedView):
    """Persistent entry point kept on the existing removed book's forum card."""
    def __init__(self, cog, book):
        super().__init__(timeout=None)
        self.cog, self.book = cog, dict(book)
        button = discord.ui.Button(label='Восстановить книгу',
                                   custom_id=f'bc:book:{book["id"]}:restore',
                                   style=discord.ButtonStyle.secondary)
        button.callback = self.restore
        self.add_item(button)
        status = discord.ui.Button(label='Статус удаления',
                                   custom_id=f'bc:book:{book["id"]}:removal-status',
                                   style=discord.ButtonStyle.secondary)
        status.callback = self.status
        self.add_item(status)

    async def restore(self, interaction):
        _bound(interaction, self.book['guild_id'])
        await open_restore_book(self.cog, interaction, self.book['id'])

    async def status(self, interaction):
        _bound(interaction, self.book['guild_id'])
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        _preflight(self.cog, interaction)
        operation = latest_book_removal_operation(self.cog.store, self.book['guild_id'], self.book['id'])
        if operation is None:
            await reply(interaction, 'Книга убрана из каталога; тема и эссе сохранены. '
                        'Незавершённых действий удаления нет.')
            return
        view = RemovalOperationView(self.cog, self.book, interaction.user.id, operation['operation_id'])
        await reply(interaction, operation_content(view.operation), view=view)


def _deleted_books(cog, guild_id):
    return [book for book in cog.store.books(guild_id, include_deleted=True) if book.get('deleted')]


async def open_deleted_books(cog, interaction):
    _bound(interaction, interaction.guild_id)
    # Hybrid slash commands were deferred by Club.cog_before_invoke, while
    # catalog buttons arrive with a new interaction that still needs an ACK.
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)
    _preflight(cog, interaction)
    books = _deleted_books(cog, interaction.guild_id)
    if not books:
        await reply(interaction, 'Удалённых книг нет.')
        return None
    view = DeletedBooksView(cog, interaction.guild_id, interaction.user.id, books)
    await reply(interaction, view.content(), view=view)
    return view


class DeletedBooksView(GuardedView):
    def __init__(self, cog, guild_id, actor_id, books):
        super().__init__(timeout=600)
        self.cog, self.guild_id, self.actor_id = cog, guild_id, actor_id
        self.books, self.index = [dict(book) for book in books], 0
        self.select = discord.ui.Select(placeholder='Выберите книгу для восстановления', row=0)
        self.select.callback = self.choose
        self.add_item(self.select)
        self._update()

    def _update(self):
        self.page_count = max(1, (len(self.books) + PAGE_SIZE - 1) // PAGE_SIZE)
        self.index = max(0, min(self.index, self.page_count - 1))
        self.visible = {book['id']: book for book in
                        self.books[self.index * PAGE_SIZE:(self.index + 1) * PAGE_SIZE]}
        self.select.options = [discord.SelectOption(label=book['title'][:100], value=book['id'],
                                                    description=book['author'][:100])
                               for book in self.visible.values()]
        self.select.disabled = not self.visible
        if not self.visible:
            self.select.options = [discord.SelectOption(label='Удалённых книг нет', value='empty')]
        self.previous.disabled = self.index == 0
        self.next_page.disabled = self.index + 1 >= self.page_count

    def content(self):
        return (f'**Удалённые книги: {len(self.books)}**\n'
                'Выберите книгу ниже. Перед восстановлением потребуется подтверждение.\n'
                f'Страница {self.index + 1}/{self.page_count}.')

    async def _navigate(self, interaction, offset):
        _bound(interaction, self.guild_id, self.actor_id)
        await interaction.response.defer(ephemeral=True)
        _preflight(self.cog, interaction)
        self.books = _deleted_books(self.cog, self.guild_id)
        self.index += offset
        self._update()
        await interaction.edit_original_response(content=self.content(), view=self,
                                                 allowed_mentions=NO_MENTIONS)

    @discord.ui.button(label='Назад', style=discord.ButtonStyle.secondary, row=1)
    async def previous(self, interaction, button):
        await self._navigate(interaction, -1)

    @discord.ui.button(label='Далее', style=discord.ButtonStyle.secondary, row=1)
    async def next_page(self, interaction, button):
        await self._navigate(interaction, 1)

    async def choose(self, interaction):
        _bound(interaction, self.guild_id, self.actor_id)
        await interaction.response.defer(ephemeral=True)
        _preflight(self.cog, interaction)
        if len(self.select.values) != 1 or self.select.values[0] not in self.visible:
            raise ClubError('Выберите книгу на этой странице списка.')
        book = _current(self.cog, self.visible[self.select.values[0]], deleted=True)
        view, content = _restore_panel(self.cog, book, self.actor_id)
        self.stop()
        await interaction.edit_original_response(content=content, view=view,
                                                 allowed_mentions=NO_MENTIONS)

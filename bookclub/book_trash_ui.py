"""Reversible book removal with private, revision-bound organizer confirmations."""
from __future__ import annotations

import discord

from .render import book_url, safe
from .service import NO_MENTIONS
from .store import ClubError
from .ui import GuardedView, reply


PAGE_SIZE = 25


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
        f'**Удалить из каталога «{safe(book["title"])}»?**\n'
        f'Автор: {safe(book["author"])}\n'
        f'Связано встреч: {meetings}; работ и черновиков эссе: {essays}.\n\n'
        'Книга исчезнет из каталога и очереди. Напоминания бота по ней остановятся. '
        'Тема книги, эссе, события Discord и история сохранятся. '
        'События Discord не отменяются.\n'
        'Книгу можно восстановить через /club library deleted или кнопку в её теме.')


def _restore_content(book):
    return (
        f'**Восстановить «{safe(book["title"])}»?**\n'
        f'Автор: {safe(book["author"])}\n\n'
        'Книга вернётся в каталог с сохранёнными статусом и порядком чтения. '
        'Существующие тема, эссе и встречи останутся привязанными к ней. '
        'Будущие напоминания бота возобновятся по актуальному плану. '
        'Автостатус останется выключенным; его можно включить в управлении книгой.')


async def open_delete_book(cog, interaction, book_id):
    _bound(interaction, interaction.guild_id)
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)
    _preflight(cog, interaction)
    book = cog.store.book(interaction.guild_id, book_id)
    if book.get('deleted'):
        raise ClubError('Книга уже удалена из каталога. Восстановление: /club library deleted.')
    view = RemoveBookConfirmation(cog, book, interaction.user.id)
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
    view = RestoreBookConfirmation(cog, book, interaction.user.id)
    await reply(interaction, _restore_content(book), view=view)
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


class RemoveBookConfirmation(BookTrashConfirmation):
    @discord.ui.button(label='Удалить из каталога', style=discord.ButtonStyle.danger, row=0)
    async def confirm(self, interaction, button):
        _bound(interaction, self.book['guild_id'], self.actor_id)
        await interaction.response.defer(ephemeral=True)
        current = await _authorized(self.cog, interaction, self.book, self.actor_id, deleted=False)
        self.cog.store.remove_book(interaction.guild_id, current['id'],
                                   expected_revision=current['revision'], actor_id=self.actor_id)
        self.stop()
        self.cog.service.request_book_refresh(interaction.guild, current['id'])
        await interaction.edit_original_response(
            content=f'«{safe(current["title"])}» удалена из каталога. Напоминания бота остановлены. '
                    'Тема, эссе и события сохранены. Восстановление: /club library deleted.',
            view=None, allowed_mentions=NO_MENTIONS)


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

    async def restore(self, interaction):
        _bound(interaction, self.book['guild_id'])
        await open_restore_book(self.cog, interaction, self.book['id'])


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
        view = RestoreBookConfirmation(self.cog, book, self.actor_id)
        self.stop()
        await interaction.edit_original_response(content=_restore_content(book), view=view,
                                                 allowed_mentions=NO_MENTIONS)

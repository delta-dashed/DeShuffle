"""Persistent catalog actions and a bounded, reviewed book-list import."""
from __future__ import annotations

import asyncio

import discord

from .book_list import parse_book_list
from .render import book_url, pages, safe
from .store import ClubError


MAX_FILE_BYTES = 64 * 1024


async def _reply(interaction, content, **kwargs):
    # Import lazily: the main command module wires this view into the service.
    from .ui import reply
    await reply(interaction, content, **kwargs)


class CatalogGuardedView(discord.ui.View):
    async def on_error(self, interaction, error, item):
        from .ui import interaction_error
        await interaction_error(interaction, error)


class CatalogGuardedModal(discord.ui.Modal):
    async def on_error(self, interaction, error):
        from .ui import interaction_error
        await interaction_error(interaction, error)


async def catalog_access(service, guild, user_id, *, organizer=False, write=True):
    """Honor the current member and the forum's manually configured access."""
    member, is_organizer = await service.actor(guild, user_id)
    if organizer and not is_organizer:
        raise ClubError('Загружать список книг может организатор клуба.')
    forum_id = service.store.settings(guild.id)['books']
    forum = await service.bot.fetch_channel(forum_id)
    if not isinstance(forum, discord.ForumChannel) or forum.guild.id != guild.id:
        raise ClubError('Форум книг недоступен. Организатору: проверьте /club diagnose.')
    permissions = forum.permissions_for(member)
    if not permissions.view_channel:
        raise ClubError('У вас нет доступа к форуму книг. Попросите организатора настроить доступ в Discord.')
    if write and not permissions.send_messages:
        raise ClubError('Для добавления книг нужен доступ к форуму книг и право создавать в нём публикации.')
    return forum


def _same_guild(interaction, guild_id):
    if interaction.guild is None or interaction.guild_id != guild_id:
        raise ClubError('Откройте каталог на сервере клуба.')


class AddBookModal(CatalogGuardedModal):
    def __init__(self, service, guild_id):
        super().__init__(title='Добавить книгу и создать её тему')
        self.service, self.guild_id = service, guild_id
        self.book_title = discord.ui.TextInput(label='Название книги', max_length=180)
        self.author = discord.ui.TextInput(label='Автор', max_length=180)
        self.materials = discord.ui.TextInput(label='Ссылки и материалы', required=False,
                                              style=discord.TextStyle.paragraph, max_length=4000)
        for field in (self.book_title, self.author, self.materials):
            self.add_item(field)

    async def on_submit(self, interaction):
        _same_guild(interaction, self.guild_id)
        await interaction.response.defer(ephemeral=True)
        async with self.service.locks[self.guild_id]:
            await catalog_access(self.service, interaction.guild, interaction.user.id)
            book = self.service.store.create_book(self.guild_id, self.book_title.value,
                self.author.value, self.materials.value, f'catalog-book:{interaction.id}')
            await self.service.refresh(interaction.guild)
        url = book_url(self.service.store, book)
        text = f'Книга «{safe(book["title"])}» добавлена как предложение.'
        text += f' [Открыть тему книги]({url})' if url else ' Для публикации темы организатору нужно выполнить /club publish.'
        await _reply(interaction, text)


class BookListModal(CatalogGuardedModal):
    def __init__(self, service, guild_id):
        super().__init__(title='Загрузить список книг')
        self.service, self.guild_id = service, guild_id
        self.text = discord.ui.TextInput(label='По одной книге: Название | Автор',
            placeholder='Мастер и Маргарита | Михаил Булгаков\nДюна | Фрэнк Герберт',
            style=discord.TextStyle.paragraph, max_length=4000)
        self.add_item(self.text)

    async def on_submit(self, interaction):
        _same_guild(interaction, self.guild_id)
        await preview_book_text(interaction, self.service, self.text.value)


class CatalogView(CatalogGuardedView):
    def __init__(self, service, guild_id):
        super().__init__(timeout=None)
        self.service, self.guild_id = service, guild_id
        for action, label in [('add', 'Добавить книгу'), ('import', 'Загрузить список'), ('queue', 'Порядок чтения'),
                              ('deleted', 'Удалённые книги')]:
            button = discord.ui.Button(label=label, custom_id=f'bc:catalog:{guild_id}:{action}',
                style=discord.ButtonStyle.primary if action == 'add' else discord.ButtonStyle.secondary)
            async def callback(interaction, selected=action):
                _same_guild(interaction, self.guild_id)
                if selected == 'deleted':
                    if self.service.deleted_books_handler is None:
                        raise ClubError('Откройте /club library deleted для восстановления книги.')
                    await self.service.deleted_books_handler(interaction)
                    return
                if selected == 'queue':
                    from .queue_controls import open_queue
                    await open_queue(self.service, interaction)
                    return
                # Modal opening must acknowledge within three seconds. Only the
                # submitted action reads private state and performs fresh REST checks.
                if selected == 'import' and not self.service.interaction_organizer(interaction):
                    raise ClubError('Загружать список книг может организатор клуба.')
                modal = AddBookModal(self.service, self.guild_id) if selected == 'add' else BookListModal(self.service, self.guild_id)
                await interaction.response.send_modal(modal)
            button.callback = callback
            self.add_item(button)


class BookListPreview(CatalogGuardedView):
    def __init__(self, service, guild_id, actor_id, request_key, books):
        super().__init__(timeout=600)
        self.service, self.guild_id, self.actor_id = service, guild_id, actor_id
        self.request_key, self.books = request_key, books
        self.completed, self.index = False, 0
        lines = []
        for number, book in enumerate(books, 1):
            line = f'{number}. **{safe(book["title"])}** · {safe(book["author"])}'
            if book.get('materials'):
                excerpt = book['materials'][:180]
                line += '\nМатериалы: ' + safe(excerpt) + ('…' if len(book['materials']) > 180 else '')
            lines.append(line)
        self.preview_pages = pages(lines, limit=1400)
        self._update_navigation()

    def _check_actor(self, interaction):
        _same_guild(interaction, self.guild_id)
        if interaction.user.id != self.actor_id:
            raise ClubError('Этот список открыт другим организатором.')

    def _update_navigation(self):
        self.previous.disabled = self.index == 0
        self.next_page.disabled = self.index == len(self.preview_pages) - 1

    def content(self):
        return (f'**Предпросмотр: {len(self.books)} книг**\n'
                'После подтверждения новые книги и их темы появятся в каталоге. '
                'Повторы по названию и автору будут пропущены.\n\n'
                + self.preview_pages[self.index]
                + f'\n\nСтраница {self.index + 1}/{len(self.preview_pages)}. '
                  'Проверьте список и нажмите «Добавить книги».')

    async def _navigate(self, interaction, offset):
        self._check_actor(interaction)
        await interaction.response.defer(ephemeral=True)
        await catalog_access(self.service, interaction.guild, interaction.user.id, organizer=True)
        self.index = max(0, min(len(self.preview_pages) - 1, self.index + offset))
        self._update_navigation()
        await interaction.edit_original_response(content=self.content(), view=self,
                                                 allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label='Назад', style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        await self._navigate(interaction, -1)

    @discord.ui.button(label='Далее', style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction, button):
        await self._navigate(interaction, 1)

    @discord.ui.button(label='Добавить книги', style=discord.ButtonStyle.success)
    async def confirm(self, interaction, button):
        self._check_actor(interaction)
        await interaction.response.defer(ephemeral=True)
        async with self.service.locks[self.guild_id]:
            if self.completed:
                raise ClubError('Этот список уже добавлен. Откройте каталог через /club books.')
            await catalog_access(self.service, interaction.guild, interaction.user.id, organizer=True)
            created, skipped = self.service.store.create_books(self.guild_id, self.books, self.request_key)
            # The fixed request key survives a failed Discord refresh and permits
            # retrying the same preview without inserting duplicate books.
            await self.service.refresh(interaction.guild)
            self.completed = True
        self.stop()
        text = f'Добавлено книг: {len(created)}. Пропущено повторов: {skipped}.'
        if self.service.store.settings(self.guild_id)['published']:
            text += ' Темы книг доступны в каталоге: /club books.'
        else:
            text += ' Для публикации тем организатору нужно выполнить /club publish.'
        await _reply(interaction, text)


async def _show_preview(interaction, service, books):
    preview = BookListPreview(service, interaction.guild_id, interaction.user.id,
        f'catalog-list:{interaction.guild_id}:{interaction.id}', books)
    await _reply(interaction, preview.content(), view=preview)
    return preview


async def preview_book_text(interaction, service, text, filename=None):
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)
    await catalog_access(service, interaction.guild, interaction.user.id, organizer=True)
    return await _show_preview(interaction, service, parse_book_list(text, filename=filename))


async def preview_book_file(interaction, service, attachment):
    """Read a bounded Discord attachment only after current authorization."""
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)
    await catalog_access(service, interaction.guild, interaction.user.id, organizer=True)
    if attachment.size > MAX_FILE_BYTES:
        raise ClubError('Список слишком большой: максимум 64 КиБ и 100 книг.')
    try:
        raw = await asyncio.wait_for(attachment.read(), timeout=20)
    except asyncio.TimeoutError as exc:
        raise ClubError('Не удалось загрузить файл за 20 секунд. Попробуйте ещё раз.') from exc
    if len(raw) > MAX_FILE_BYTES:
        raise ClubError('Список слишком большой: максимум 64 КиБ и 100 книг.')
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise ClubError('Сохраните список в UTF-8: TXT, CSV или JSON.') from exc
    return await _show_preview(interaction, service, parse_book_list(text, filename=attachment.filename))

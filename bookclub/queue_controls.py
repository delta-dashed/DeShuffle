"""Organizer queue controls, preserving the current book and the archive."""
import discord

from .catalog_ui import CatalogGuardedView, catalog_access, _reply, _same_guild
from .render import safe
from .store import ClubError


def queue_books(store, guild_id):
    books = store.books(guild_id)
    return [b for status in ('queued', 'proposed') for b in books if b['status'] == status]


def move_book(store, guild_id, snapshot, book_id, action, actor_id):
    settings = store.settings(guild_id)
    with store.tx() as db:
        rows = [dict(row) for row in db.execute(
            "SELECT * FROM bc_books WHERE guild_id=? AND status IN ('queued','proposed') ORDER BY position,id", (guild_id,))]
        if {b['id']: b['revision'] for b in rows} != {b['id']: b['revision'] for b in snapshot}:
            raise ClubError('Очередь уже изменилась. Откройте управление заново.')
        book = next((b for b in rows if b['id'] == book_id), None)
        if book is None:
            raise ClubError('Выберите книгу из очереди или предложений.')
        queued = [b for b in rows if b['status'] == 'queued']
        if action == 'enqueue':
            if book['status'] != 'proposed':
                raise ClubError('Книга уже в очереди.')
            position = max((b['position'] for b in rows), default=0) + 1
            db.execute("""UPDATE bc_books SET status='queued',position=?,revision=revision+1,
                        status_automation=0,status_automation_pending=0 WHERE id=?""", (position, book_id))
            store._status_audit(db, book, 'manual', 'queued', actor_id=actor_id)
            store._essay_schedule(db, book_id, settings)
            return
        if action not in ('up', 'down') or book['status'] != 'queued':
            raise ClubError('Сначала добавьте книгу в очередь.')
        index = next(i for i, b in enumerate(queued) if b['id'] == book_id)
        target = index + (-1 if action == 'up' else 1)
        if not 0 <= target < len(queued):
            return
        queued[index], queued[target] = queued[target], queued[index]
        positions = sorted({b['position'] for b in queued})
        if len(positions) != len(queued):
            positions = list(range(1, len(queued) + 1))
        for b, position in zip(queued, positions):
            if b['position'] != position:
                db.execute('UPDATE bc_books SET position=?,revision=revision+1 WHERE id=?', (position, b['id']))
                store._essay_schedule(db, b['id'], settings)


async def open_queue(service, interaction):
    await interaction.response.defer(ephemeral=True)
    await catalog_access(service, interaction.guild, interaction.user.id, organizer=True)
    view = QueueView(service, interaction.guild_id, interaction.user.id)
    await _reply(interaction, view.content(), view=view)
    return view


class QueueView(CatalogGuardedView):
    def __init__(self, service, guild_id, actor_id, *, index=0, selected=None):
        super().__init__(timeout=600)
        self.service, self.guild_id, self.actor_id = service, guild_id, actor_id
        self.books = queue_books(service.store, guild_id)
        self.index = min(index, max(0, (len(self.books) - 1) // 25))
        self.selected = selected
        options = [discord.SelectOption(label=safe(b['title'])[:100], value=b['id'],
                   description=('В очереди · ' if b['status'] == 'queued' else 'Предложение · ') + safe(b['author'])[:70],
                   default=b['id'] == selected) for b in self.books[self.index * 25:(self.index + 1) * 25]]
        self.select = discord.ui.Select(placeholder='Выберите книгу', row=0,
            options=options or [discord.SelectOption(label='Очередь пуста', value='empty')], disabled=not options)
        self.select.callback = self.choose
        self.add_item(self.select)

    def content(self):
        book = next((b for b in self.books if b['id'] == self.selected), None)
        self.up.disabled = self.down.disabled = not book or book['status'] != 'queued'
        self.enqueue.disabled = not book or book['status'] != 'proposed'
        self.previous.disabled = self.index == 0
        self.next_page.disabled = (self.index + 1) * 25 >= len(self.books)
        text = '**Порядок чтения**\nВыберите книгу и переместите её выше или ниже. Предложения можно добавить в конец очереди.'
        text += '\n\n' + (f'Выбрана: **{safe(book["title"])}**' if book else 'Книга пока не выбрана.')
        if not self.books:
            text += '\nДобавьте книгу через каталог — она появится здесь как предложение.'
        return text

    def check(self, interaction):
        _same_guild(interaction, self.guild_id)
        if interaction.user.id != self.actor_id:
            raise ClubError('Эта очередь открыта другим организатором.')

    async def choose(self, interaction):
        self.check(interaction)
        self.selected = self.select.values[0]
        await interaction.response.edit_message(content=self.content(), view=self,
                                                allowed_mentions=discord.AllowedMentions.none())

    async def action(self, interaction, action):
        self.check(interaction)
        await interaction.response.defer(ephemeral=True)
        async with self.service.locks[self.guild_id]:
            await catalog_access(self.service, interaction.guild, self.actor_id, organizer=True)
            move_book(self.service.store, self.guild_id, self.books, self.selected, action, self.actor_id)
            await self.service.refresh(interaction.guild)
        await self.replace(interaction, self.index)

    async def replace(self, interaction, index):
        view = QueueView(self.service, self.guild_id, self.actor_id, index=index, selected=self.selected)
        await interaction.edit_original_response(content=view.content(), view=view,
                                                 allowed_mentions=discord.AllowedMentions.none())
        self.stop()

    @discord.ui.button(label='Выше', row=1)
    async def up(self, interaction, button):
        await self.action(interaction, 'up')

    @discord.ui.button(label='Ниже', row=1)
    async def down(self, interaction, button):
        await self.action(interaction, 'down')

    @discord.ui.button(label='В очередь', row=1)
    async def enqueue(self, interaction, button):
        await self.action(interaction, 'enqueue')

    async def navigate(self, interaction, offset):
        self.check(interaction)
        await interaction.response.defer(ephemeral=True)
        await catalog_access(self.service, interaction.guild, self.actor_id, organizer=True)
        await self.replace(interaction, max(0, self.index + offset))

    @discord.ui.button(label='Назад', row=2)
    async def previous(self, interaction, button):
        await self.navigate(interaction, -1)

    @discord.ui.button(label='Далее', row=2)
    async def next_page(self, interaction, button):
        await self.navigate(interaction, 1)

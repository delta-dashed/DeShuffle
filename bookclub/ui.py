"""Hybrid commands and Discord forms. Private drafts only use ephemeral replies."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .store import ClubError, STATUSES, parse_time
from .render import pages, safe, meeting_lines, book_url, essay_lines
from .service import NO_MENTIONS, ESSAY_WEBHOOK_NAME, Service
from .archive_import import ArchiveImporter
from .catalog_ui import CatalogView, catalog_access, preview_book_file

log = logging.getLogger(__name__)
HELP = '''**Архивариус · книжный клуб**
Участнику: `/club books`, `/club book_add`, `/club join`, `/club essay`.
В каталоге и `/club books`: «Добавить книгу» создаёт книгу и её тему. Организатору: «Загрузить список» или `/club library import` с файлом TXT, CSV, JSON.
В карточке книги: «Управление книгой» — статус, данные и план N + 1; «Встречи» — конкретные даты и переносы.
Ведущему: откройте `/club meeting`, нажмите «Провести встречу» и подтвердите. Кнопка «Мой план» открывает личный черновик.
Организатору: `/club book_edit`, `/club participant`, `/club meeting_add`, `/club meeting_attach`, `/club move`, `/club cancel`, `/club offer`, `/club replace`, `/club handover`.
Настройка сервера: `/club setup` создаёт недостающие каналы и проверяет существующие; `check_only=True` — только проверка.
Организатору после настройки: `/club diagnose`, затем `/club publish`. Перечень и инструкция — в docs/BOOK_CLUB.md.
Вестник — организационные объявления вручную. Площадь — флуд и общение. Даты и границы чтения задаются в карточках и событиях.'''


async def reply(interaction, content, **kwargs):
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True, allowed_mentions=NO_MENTIONS, **kwargs)
    else:
        await interaction.response.send_message(content, ephemeral=True, allowed_mentions=NO_MENTIONS, **kwargs)


async def interaction_error(interaction, error):
    # Hybrid commands can wrap an expected error more than once. Do not follow
    # arbitrary exception chains or loop on a malformed wrapper.
    seen = set()
    wrappers = (commands.CommandInvokeError, commands.HybridCommandError, app_commands.CommandInvokeError)
    for _ in range(8):
        if not isinstance(error, wrappers) or id(error) in seen:
            break
        seen.add(id(error))
        original = getattr(error, 'original', None)
        if not isinstance(original, Exception):
            break
        error = original
    if isinstance(error, ClubError):
        text = str(error)
    elif isinstance(error, discord.Forbidden):
        text = 'Discord отказал в доступе. Организатору: проверьте /club diagnose и права бота.'
    elif isinstance(error, discord.NotFound):
        text = 'Канал, сообщение или событие удалено. Обновите карточку через /club publish.'
    else:
        log.error('Book club interaction failed', exc_info=(type(error), error, error.__traceback__))
        text = 'Не удалось завершить действие. Изменение могло сохраниться; проверьте карточку и /club diagnose перед повтором.'
    await reply(interaction, text)


class GuardedView(discord.ui.View):
    async def on_error(self, interaction, error, item):
        await interaction_error(interaction, error)


class GuardedModal(discord.ui.Modal):
    async def on_error(self, interaction, error):
        await interaction_error(interaction, error)


class BookView(GuardedView):
    def __init__(self, cog, book):
        super().__init__(timeout=None)
        for action, label in [('write', 'Добавить своё эссе'), ('own', 'Открыть моё эссе'), ('list', 'Эссе участников')]:
            button = discord.ui.Button(label=label, custom_id=f'bc:book:{book["id"]}:{action}',
                                       style=discord.ButtonStyle.primary if action == 'write' else discord.ButtonStyle.secondary)
            async def callback(interaction, selected=action):
                await cog.book_essay_action(interaction, book, selected)
            button.callback = callback
            self.add_item(button)
        for action, label in [('manage', 'Управление книгой'), ('meetings', 'Встречи')]:
            button = discord.ui.Button(label=label, custom_id=f'bc:book:{book["id"]}:{action}', row=1)
            async def manage_callback(interaction, selected=action):
                if interaction.guild_id != book['guild_id']:
                    raise ClubError('Откройте карточку на сервере клуба.')
                if selected == 'manage':
                    from .book_controls import open_book_controls
                    await open_book_controls(cog, interaction, book['id'])
                else:
                    from .meeting_controls import open_book_meetings
                    await open_book_meetings(cog, interaction, book['id'])
            button.callback = manage_callback
            self.add_item(button)


class ConfirmHost(GuardedView):
    def __init__(self, cog, meeting, actor_id, action):
        super().__init__(timeout=180)
        self.cog, self.meeting, self.actor_id, self.action = cog, meeting, actor_id, action

    @discord.ui.button(label='Подтверждаю', style=discord.ButtonStyle.success)
    async def confirm(self, interaction, button):
        if interaction.user.id != self.actor_id:
            raise ClubError('Подтверждение адресовано другому участнику.')
        await interaction.response.defer(ephemeral=True)
        service, store = self.cog.service, self.cog.store
        async with service.locks[interaction.guild_id]:
            _, organizer = await service.actor(interaction.guild, interaction.user.id)
            await service.sync_one(interaction.guild, store.meeting(interaction.guild_id, self.meeting['id']))
            live = await service.live_participants(interaction.guild)
            store.host_action(interaction.guild_id, self.meeting['id'], interaction.user.id, self.action,
                              version=self.meeting['host_version'], organizer=organizer, live_ids=live)
            await service.refresh(interaction.guild)
        self.stop()
        await reply(interaction, 'Назначение обновлено.')


class OfferView(GuardedView):
    def __init__(self, cog, meeting, actor_id):
        super().__init__(timeout=180)
        self.cog, self.meeting, self.actor_id = cog, meeting, actor_id

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder='Кому предложить провести встречу?', min_values=1, max_values=1)
    async def select_member(self, interaction, select):
        if interaction.user.id != self.actor_id:
            raise ClubError('Выбор открыт другим организатором.')
        await interaction.response.defer(ephemeral=True)
        service, store = self.cog.service, self.cog.store
        async with service.locks[interaction.guild_id]:
            _, organizer = await service.actor(interaction.guild, interaction.user.id)
            candidate, _ = await service.actor(interaction.guild, select.values[0].id)
            await service.sync_one(interaction.guild, store.meeting(interaction.guild_id, self.meeting['id']))
            store.host_action(interaction.guild_id, self.meeting['id'], interaction.user.id, 'offer',
                              version=self.meeting['host_version'], organizer=organizer,
                              candidate=candidate.id, live_ids={candidate.id})
            await service.refresh(interaction.guild)
        self.stop()
        await reply(interaction, 'Предложение сохранено; ждём принятия участником.')


class MeetingView(GuardedView):
    def __init__(self, cog, meeting):
        super().__init__(timeout=None)
        self.cog, self.meeting = cog, meeting
        actions = [('volunteer', 'Провести встречу'), ('accept', 'Принять предложение'),
                   ('decline', 'Отказаться'), ('pick', 'Подобрать ведущего'),
                   ('offer', 'Предложить участнику'), ('plan', 'Мой план')]
        for action, label in actions:
            button = discord.ui.Button(label=label, custom_id=f'bc:{meeting["id"]}:{meeting["host_version"]}:{action}', row=0 if action in ('volunteer', 'accept', 'decline') else 1)
            async def callback(interaction, selected=action):
                await self.act(interaction, selected)
            button.callback = callback
            self.add_item(button)
        book = cog.store.book(meeting['guild_id'], meeting['book_id'])
        for action, label in [('write', 'Добавить своё эссе'), ('own', 'Открыть моё эссе'), ('list', 'Эссе участников')]:
            button = discord.ui.Button(label=label, custom_id=f'bc:{meeting["id"]}:essay:{action}', row=2)
            async def essay_callback(interaction, selected=action):
                await cog.book_essay_action(interaction, book, selected)
            button.callback = essay_callback
            self.add_item(button)
        manage = discord.ui.Button(label='Управление встречей', custom_id=f'bc:{meeting["id"]}:manage', row=3)
        async def manage_callback(interaction):
            if interaction.guild_id != meeting['guild_id']:
                raise ClubError('Откройте карточку на сервере клуба.')
            from .meeting_controls import open_meeting_controls
            await open_meeting_controls(cog, interaction, meeting['id'])
        manage.callback = manage_callback
        self.add_item(manage)

    async def act(self, interaction, action):
        if interaction.guild_id != self.meeting['guild_id']:
            raise ClubError('Откройте карточку на сервере клуба.')
        service, store = self.cog.service, self.cog.store
        await interaction.response.defer(ephemeral=True)
        async with service.locks[interaction.guild_id]:
            _, organizer = await service.actor(interaction.guild, interaction.user.id)
            await service.sync_one(interaction.guild, store.meeting(interaction.guild_id, self.meeting['id']))
            current = store.meeting(interaction.guild_id, self.meeting['id'])
            if action == 'plan':
                plan = store.plan(interaction.guild_id, current['id'], interaction.user.id, organizer)
                await self.cog.show_plan(interaction, current, plan)
            elif current['host_version'] != self.meeting['host_version']:
                raise ClubError('Карточка устарела. Откройте /club meeting заново.')
            elif action == 'offer':
                if not organizer:
                    raise ClubError('Предлагать ведущего может организатор.')
                await reply(interaction, 'Выберите участника этого чтения. Ему потребуется подтвердить предложение.', view=OfferView(self.cog, current, interaction.user.id))
            elif action in ('volunteer', 'accept', 'decline'):
                if action == 'volunteer' and interaction.user.id not in {p['user_id'] for p in store.participants(current['book_id'])}:
                    raise ClubError('Сначала присоединитесь к чтению: /club join.')
                if action != 'volunteer' and current['host_id'] != interaction.user.id:
                    raise ClubError('Это предложение адресовано другому участнику.')
                await reply(interaction, '\n'.join(meeting_lines(store, current, store.settings(interaction.guild_id))) + '\nПодтвердите выбранное действие.', view=ConfirmHost(self.cog, current, interaction.user.id, action))
            elif action == 'pick':
                live = await service.live_participants(interaction.guild)
                store.host_action(interaction.guild_id, current['id'], interaction.user.id, 'pick', version=current['host_version'], organizer=organizer, live_ids=live)
                await service.refresh(interaction.guild)
                await reply(interaction, 'Подбор выполнен. Предложение и его состояние видны в карточке.')


class PlanModal(GuardedModal):
    def __init__(self, cog, meeting, plan, mode):
        super().__init__(title={'edit': 'План встречи', 'summary': 'Короткие итоги', 'publish': 'Опубликовать выбранные вопросы'}[mode])
        self.cog, self.meeting, self.plan, self.mode = cog, meeting, plan, mode
        self.fields = {}
        labels = [('topics', 'Темы'), ('questions', 'Открытые вопросы'), ('excerpts', 'Сцены, фрагменты, цитаты'), ('notes', 'Заметки и порядок обсуждения')]
        if mode == 'summary':
            labels = [('summary', 'Итоги, спорные места, к чему вернуться')]
        elif mode == 'publish':
            labels = [('selected', 'Только вопросы, выбранные для публикации')]
        for key, label in labels:
            field = discord.ui.TextInput(label=label, style=discord.TextStyle.paragraph,
                default=plan.get(key, ''), required=mode == 'publish', max_length=1700 if mode == 'publish' else 4000,
                placeholder='Разные обоснованные ответы; без подсказки и спойлеров.' if key == 'questions' else None)
            self.fields[key] = field
            self.add_item(field)

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        service, store = self.cog.service, self.cog.store
        async with service.locks[interaction.guild_id]:
            _, organizer = await service.actor(interaction.guild, interaction.user.id)
            current = store.plan(interaction.guild_id, self.meeting['id'], interaction.user.id, organizer)
            if current['generation'] != self.plan['generation'] or current['version'] != self.plan['version']:
                raise ClubError('План изменился. Откройте его заново.')
            fields = {k: str(v.value) for k, v in self.fields.items()}
            if self.mode == 'publish':
                root = store.publication(f'book:{self.meeting["book_id"]}')
                if not root or not root['message_id']:
                    raise ClubError('Сначала организатор публикует карточку книги: /club publish.')
                await service.upsert(interaction.guild, f'questions:{self.meeting["id"]}:{interaction.id}', root['channel_id'], '**Вопросы к встрече**\n' + fields['selected'])
            else:
                store.save_plan(interaction.guild_id, self.meeting['id'], interaction.user.id,
                                generation=self.plan['generation'], version=self.plan['version'], organizer=organizer, **fields)
                await service.refresh(interaction.guild)
        await reply(interaction, 'Выбранные вопросы опубликованы.' if self.mode == 'publish' else 'Сохранено в личном плане.')


class PlanView(GuardedView):
    def __init__(self, cog, meeting, plan, actor_id):
        super().__init__(timeout=600)
        self.cog, self.meeting, self.plan, self.actor_id = cog, meeting, plan, actor_id
        for mode, label in [('edit', 'Дополнить план'), ('ready', 'План готов'), ('publish', 'Опубликовать вопросы'), ('summary', 'Сохранить итоги')]:
            button = discord.ui.Button(label=label)
            async def callback(interaction, selected=mode):
                if interaction.user.id != self.actor_id:
                    raise ClubError('Этот план открыт другим участником.')
                # No Discord HTTP before send_modal: acknowledge within the 3s deadline.
                # This access check uses current domain state; REST authorization is repeated on submit.
                self.cog.service.interaction_access(interaction)
                organizer = self.cog.service.interaction_organizer(interaction)
                current = self.cog.store.plan(interaction.guild_id, self.meeting['id'], interaction.user.id, organizer)
                if (current['generation'], current['version']) != (self.plan['generation'], self.plan['version']):
                    raise ClubError('План изменился. Откройте его заново.')
                if selected == 'ready':
                    await interaction.response.defer(ephemeral=True)
                    async with self.cog.service.locks[interaction.guild_id]:
                        _, organizer = await self.cog.service.actor(interaction.guild, interaction.user.id)
                        self.cog.store.save_plan(interaction.guild_id, self.meeting['id'], interaction.user.id,
                            generation=self.plan['generation'], version=self.plan['version'], organizer=organizer, ready=True)
                        await self.cog.service.refresh(interaction.guild)
                    await reply(interaction, 'План отмечен готовым.')
                else:
                    await interaction.response.send_modal(PlanModal(self.cog, self.meeting, current, selected))
            button.callback = callback
            self.add_item(button)


class Club(commands.Cog):
    def __init__(self, bot, store, guild_ids=None, import_config=None):
        self.bot, self.store = bot, store
        self.service = Service(bot, store, guild_ids)
        self.service.view_factory = lambda meeting: MeetingView(self, meeting)
        self.service.book_view_factory = lambda book: BookView(self, book)
        self.service.catalog_view_factory = lambda guild_id: CatalogView(self.service, guild_id)
        from .format_controls import FormatView
        self.service.format_view_factory = lambda guild_id: FormatView(self.service, guild_id)
        self.importer = ArchiveImporter(self.service, import_config)

    async def cog_load(self):
        self.store.recover_jobs()
        self.importer.ledger.recover_runs()
        with self.store.tx() as db:
            db.execute('UPDATE bc_publications SET content_hash=NULL')
        for m in self.store.rows('SELECT * FROM bc_meetings WHERE event_id IS NOT NULL'):
            self.bot.add_view(MeetingView(self, m))
        for book in self.store.rows('SELECT * FROM bc_books'):
            self.bot.add_view(BookView(self, book))
        for row in self.store.rows('SELECT guild_id FROM bc_settings'):
            self.bot.add_view(CatalogView(self.service, row['guild_id']))
            from .format_controls import FormatView
            self.bot.add_view(FormatView(self.service, row['guild_id']))
        self.worker.start()

    async def cog_unload(self):
        self.worker.cancel()
        await self.importer.close()

    @tasks.loop(seconds=30)
    async def worker(self):
        for row in self.store.rows('SELECT guild_id FROM bc_settings'):
            if self.service.guild_ids is not None and row['guild_id'] not in self.service.guild_ids:
                continue
            guild = self.bot.get_guild(row['guild_id'])
            if guild:
                try:
                    await self.service.tick(guild)
                except Exception:
                    log.exception('Book club reconciliation failed for guild %s; will retry', guild.id)

    @worker.before_loop
    async def before_worker(self):
        await self.bot.wait_until_ready()

    async def cog_before_invoke(self, ctx):
        if ctx.interaction:
            await ctx.defer(ephemeral=True)
        if ctx.command is self.setup or ctx.command.qualified_name == 'club setup':
            await self.service.setup_actor(ctx.guild, ctx.author.id)
        else:
            maintenance = ctx.command.qualified_name in {'club diagnose', 'club publish', 'club repair'}
            await self.service.actor(ctx.guild, ctx.author.id, require_access=not maintenance)
            if not ctx.interaction and not maintenance:
                settings = self.store.settings(ctx.guild.id)
                club_channels = {settings[key] for key in ('news', 'chat', 'books', 'essays', 'voice')}
                origin = ctx.channel.parent_id if isinstance(ctx.channel, discord.Thread) else ctx.channel.id
                if origin not in club_channels:
                    raise ClubError('Вне каналов клуба используйте slash-команду /club: ответ будет виден только вам.')

    async def cog_command_error(self, ctx, error):
        error = getattr(error, 'original', error)
        if ctx.interaction:
            await interaction_error(ctx.interaction, error)
        else:
            await ctx.send(str(error) if isinstance(error, ClubError) else 'Ошибка клуба. Проверьте /club diagnose и журнал бота.', allowed_mentions=NO_MENTIONS)

    async def say(self, ctx, content, **kwargs):
        if ctx.interaction:
            await ctx.interaction.followup.send(content, ephemeral=True, allowed_mentions=NO_MENTIONS, **kwargs)
        else:
            await ctx.send(content, allowed_mentions=NO_MENTIONS, **kwargs)

    async def organizer(self, ctx):
        maintenance = getattr(getattr(ctx, 'command', None), 'qualified_name', '') in {'club diagnose', 'club publish', 'club repair'}
        _, allowed = await self.service.actor(ctx.guild, ctx.author.id, require_access=not maintenance)
        if not allowed:
            raise ClubError('Это действие доступно организатору клуба.')

    async def book_essay_action(self, interaction, book, action):
        if interaction.guild_id != book['guild_id']:
            raise ClubError('Откройте карточку на сервере клуба.')
        await interaction.response.defer(ephemeral=True)
        if action == 'write':
            essay = await self.service.create_essay_space(interaction.guild, book['id'], interaction.user.id)
            view = discord.ui.View(timeout=180)
            view.add_item(discord.ui.Button(label='Открыть моё эссе', url=essay['url']))
            text = ('Ваше эссе уже опубликовано. Кнопка открывает существующий пост.' if essay['submitted'] else
                    'Ваш пост для эссе готов. Откройте его и напишите текст, отправьте файл или ссылку '
                    'обычным сообщением от своего имени. Повторное нажатие открывает этот же пост.')
            await reply(interaction, text, view=view)
        elif action == 'own':
            essays = await self.service.own_essays(interaction.guild, book['id'], interaction.user.id)
            if not essays:
                await reply(interaction, 'У вас пока нет поста для эссе по этой книге. Нажмите «Добавить своё эссе», чтобы создать его.')
                return
            for offset in range(0, len(essays), 25):
                view = discord.ui.View(timeout=180)
                for index, essay in enumerate(essays[offset:offset + 25], offset + 1):
                    label = 'Открыть моё эссе' if len(essays) == 1 else f'{index}. {essay["title"]}'
                    view.add_item(discord.ui.Button(label=label[:80], url=essay['url']))
                await reply(interaction, 'Ваши эссе и черновики по этой книге:', view=view)
        else:
            _, current, _ = await self.service.essay_access(interaction.guild, book['id'], interaction.user.id)
            for page in pages([f'**{safe(current["title"])}**', *essay_lines(self.store, current)]):
                await reply(interaction, page)

    def resolve_book(self, guild_id, value):
        books = self.store.books(guild_id)
        matches = [b for b in books if b['id'] == value or b['title'].casefold() == value.casefold()]
        if len(matches) != 1:
            raise ClubError('Выберите книгу из подсказок; название отсутствует или неоднозначно.')
        return matches[0]

    def resolve_meeting(self, guild_id, value):
        rows = self.store.rows('SELECT * FROM bc_meetings WHERE guild_id=?', (guild_id,))
        matches = [m for m in rows if m['id'] == value or m['name'].casefold() == value.casefold()]
        if len(matches) != 1:
            raise ClubError('Выберите встречу из подсказок; название отсутствует или неоднозначно.')
        return matches[0]

    @commands.hybrid_group(name='club', description='Книжный клуб', fallback='help')
    @commands.guild_only()
    async def club(self, ctx):
        await self.say(ctx, HELP)

    @club.command(name='books', description='Книги и порядок чтения')
    async def books(self, ctx):
        await catalog_access(self.service, ctx.guild, ctx.author.id, write=False)
        from .render import catalog_pages
        for index, page in enumerate(catalog_pages(self.store, ctx.guild.id)):
            await self.say(ctx, page, view=CatalogView(self.service, ctx.guild.id) if index == 0 else None)

    @club.command(name='book_add', description='Предложить книгу')
    async def book_add(self, ctx, title: str, author: str, materials: str = ''):
        async with self.service.locks[ctx.guild.id]:
            await catalog_access(self.service, ctx.guild, ctx.author.id)
            b = self.store.create_book(ctx.guild.id, title, author, materials, ctx.interaction.id if ctx.interaction else ctx.message.id)
            await self.service.refresh(ctx.guild)
        await self.say(ctx, f'Книга «{safe(b["title"])}» добавлена как предложение.')

    @club.group(name='library', description='Загрузка списка книг', invoke_without_command=True)
    async def library(self, ctx):
        await self.say(ctx, 'Нажмите «Загрузить список» в каталоге или прикрепите файл к /club library import. '
                       'Формат TXT: Название | Автор, по одной книге в строке. Также поддерживаются CSV и JSON.')

    @library.command(name='import', description='Разобрать файл со списком книг и показать предпросмотр')
    async def library_import(self, ctx, file: discord.Attachment):
        if ctx.interaction is None:
            raise ClubError('Прикрепите файл к slash-команде /club library import: предпросмотр виден только вам.')
        await preview_book_file(ctx.interaction, self.service, file)

    @club.command(name='book_edit', description='Изменить книгу, очередь и план встреч')
    async def book_edit(self, ctx, book: str, status: Optional[Literal['Предложено', 'В очереди', 'Читаем', 'Прочитано']] = None,
                        position: Optional[int] = None, title: Optional[str] = None,
                        author: Optional[str] = None, materials: Optional[str] = None, reading_meetings: Optional[str] = None):
        async with self.service.locks[ctx.guild.id]:
            await self.organizer(ctx)
            b = self.resolve_book(ctx.guild.id, book)
            fields = {k: v for k, v in dict(position=position, title=title, author=author, materials=materials).items() if v is not None}
            if status:
                fields['status'] = next(k for k, v in STATUSES.items() if v == status)
            if reading_meetings is not None:
                value = reading_meetings.strip()
                if value == '-':
                    fields['reading_meetings'] = None
                elif value.isdecimal() and len(value) <= 3:
                    fields['reading_meetings'] = int(value)
                else:
                    raise ClubError('План: целое число встреч по книге от 1 до 100; «-» возвращает стандартные 3. Разбор эссе добавляется отдельно: +1.')
            self.store.update_book(ctx.guild.id, b['id'], status_actor_id=ctx.author.id, **fields)
            await self.service.refresh(ctx.guild)
        await self.say(ctx, 'Книга обновлена; обсуждение и история сохранены.')

    @club.command(name='join', description='Участвовать в чтении и указать готовность вести')
    async def join(self, ctx, book: str, willing: bool = False, leave: bool = False):
        async with self.service.locks[ctx.guild.id]:
            b = self.resolve_book(ctx.guild.id, book)
            self.store.participant(ctx.guild.id, b['id'], ctx.author.id, joined=not leave, willing=willing)
            await self.service.refresh(ctx.guild)
        await self.say(ctx, 'Участие в чтении обновлено.' + (' Вы в пуле готовых вести.' if willing and not leave else ''))

    @club.command(name='participant', description='Изменить участие человека в чтении')
    async def participant(self, ctx, book: str, member: discord.Member, remove: bool = False):
        async with self.service.locks[ctx.guild.id]:
            await self.organizer(ctx)
            await self.service.actor(ctx.guild, member.id, require_access=not remove)
            b = self.resolve_book(ctx.guild.id, book)
            old = next((p for p in self.store.participants(b['id']) if p['user_id'] == member.id), None)
            self.store.participant(ctx.guild.id, b['id'], member.id, joined=not remove, willing=bool(old and old['willing']))
            await self.service.refresh(ctx.guild)
        await self.say(ctx, 'Участие обновлено. Готовность к ротации человек отмечает сам через /club join.')

    @club.command(name='meeting_add', description='Создать встречу книги и событие Discord')
    async def meeting_add(self, ctx, book: str, name: str, date: str, part: str, chapter: str, minutes: int = 90,
                          plan_kind: Literal['reading', 'essay'] = 'reading'):
        from .meeting_actions import create_meeting
        b = self.resolve_book(ctx.guild.id, book)
        request = str(ctx.interaction.id if ctx.interaction else ctx.message.id)
        await create_meeting(self.service, ctx.guild, ctx.author.id, b['id'], name=name, date=date,
                             part=part, chapter=chapter, minutes=minutes, request_key=request, plan_kind=plan_kind)
        await self.say(ctx, 'Встреча создана. Время теперь берётся из события Discord; ведущего выбирают в /club meeting.')

    @club.command(name='meeting_attach', description='Связать существующее событие с книгой')
    async def meeting_attach(self, ctx, book: str, event: str, part: str, chapter: str,
                             plan_kind: Literal['reading', 'essay'] = 'reading'):
        async with self.service.locks[ctx.guild.id]:
            await self.organizer(ctx)
            b = self.resolve_book(ctx.guild.id, book)
            events = await ctx.guild.fetch_scheduled_events()
            matches = [e for e in events if str(e.id) == event or e.name.casefold() == event.casefold()]
            if len(matches) != 1 or matches[0].entity_type != discord.EntityType.voice:
                raise ClubError('Выберите одно голосовое событие из подсказок.')
            e = matches[0]
            old = self.store.one('SELECT * FROM bc_meetings WHERE event_id=?', (e.id,))
            if old and old['book_id'] != b['id']:
                raise ClubError('Событие уже связано с другой книгой.')
            m = old or self.store.draft_meeting(ctx.guild.id, b['id'], e.name, part, chapter, f'attach:{e.id}',
                                               plan_kind=plan_kind)
            self.service.sync(ctx.guild, m, e)
            await self.service.refresh(ctx.guild)
        await self.say(ctx, 'Событие связано с книгой.')

    @club.command(name='meeting', description='Открыть карточку встречи с кнопками')
    async def meeting(self, ctx, meeting: str):
        async with self.service.locks[ctx.guild.id]:
            m = self.resolve_meeting(ctx.guild.id, meeting)
            await self.service.sync_one(ctx.guild, m)
            m = self.store.meeting(ctx.guild.id, m['id'])
            await self.say(ctx, '\n'.join(meeting_lines(self.store, m, self.store.settings(ctx.guild.id))), view=MeetingView(self, m))

    @club.command(name='attendance', description='Отметить отсутствие на конкретной встрече')
    async def attendance(self, ctx, meeting: str, absent: bool = True, member: Optional[discord.Member] = None):
        async with self.service.locks[ctx.guild.id]:
            target = member or ctx.author
            if target.id != ctx.author.id:
                await self.organizer(ctx)
            m = self.resolve_meeting(ctx.guild.id, meeting)
            self.store.attendance(ctx.guild.id, m['id'], target.id, absent)
            await self.service.refresh(ctx.guild)
        await self.say(ctx, 'Участие во встрече обновлено.')

    @club.command(name='boundary', description='Уточнить часть и последнюю главу встречи')
    async def boundary(self, ctx, meeting: str, part: str, chapter: str):
        from .meeting_actions import edit_meeting
        m = self.resolve_meeting(ctx.guild.id, meeting)
        await edit_meeting(self.service, ctx.guild, ctx.author.id, m['id'], part=part, chapter=chapter)
        await self.say(ctx, 'Граница чтения уточнена. Ведущему нужно проверить план и вновь отметить готовность.')

    @club.command(name='move', description='Перенести встречу в событии Discord')
    async def move(self, ctx, meeting: str, date: str, minutes: int = 90):
        from .meeting_actions import move_meeting
        m = self.resolve_meeting(ctx.guild.id, meeting)
        await move_meeting(self.service, ctx.guild, ctx.author.id, m['id'], date=date, minutes=minutes)
        await self.say(ctx, 'Событие перенесено. Ведущему требуется подтвердить новые условия.')

    @club.command(name='cancel', description='Отменить будущую встречу и её напоминания')
    async def cancel(self, ctx, meeting: str):
        from .meeting_actions import cancel_meeting
        m = self.resolve_meeting(ctx.guild.id, meeting)
        await cancel_meeting(self.service, ctx.guild, ctx.author.id, m['id'])
        await self.say(ctx, 'Встреча отменена; история сохранена.')

    @club.command(name='offer', description='Предложить участнику провести встречу')
    async def offer(self, ctx, meeting: str, member: discord.Member):
        async with self.service.locks[ctx.guild.id]:
            await self.organizer(ctx)
            await self.service.actor(ctx.guild, member.id)
            m = self.resolve_meeting(ctx.guild.id, meeting)
            await self.service.sync_one(ctx.guild, m)
            m = self.store.meeting(ctx.guild.id, m['id'])
            self.store.host_action(ctx.guild.id, m['id'], ctx.author.id, 'offer', version=m['host_version'], organizer=True, candidate=member.id, live_ids={member.id})
            await self.service.refresh(ctx.guild)
        await self.say(ctx, 'Предложение сохранено; требуется принятие участником.')

    @club.command(name='replace', description='Снять назначение; затем выбрать нового ведущего')
    async def replace(self, ctx, meeting: str):
        async with self.service.locks[ctx.guild.id]:
            await self.organizer(ctx)
            m = self.resolve_meeting(ctx.guild.id, meeting)
            await self.service.sync_one(ctx.guild, m)
            m = self.store.meeting(ctx.guild.id, m['id'])
            self.store.host_action(ctx.guild.id, m['id'], ctx.author.id, 'replace', version=m['host_version'], organizer=True)
            await self.service.refresh(ctx.guild)
        await self.say(ctx, 'Назначение снято. Выберите нового ведущего; предыдущий черновик сохранён для передачи организатором.')

    @club.command(name='handover', description='Передать прежний план подтверждённому новому ведущему')
    async def handover(self, ctx, meeting: str):
        async with self.service.locks[ctx.guild.id]:
            await self.organizer(ctx)
            m = self.resolve_meeting(ctx.guild.id, meeting)
            self.store.handover(ctx.guild.id, m['id'], ctx.author.id, organizer=True)
            await self.service.refresh(ctx.guild)
        await self.say(ctx, 'Предыдущий план скопирован новому ведущему. Проверьте его и отметьте готовность заново.')

    async def show_plan(self, interaction, meeting, plan):
        lines = ['**Личный план встречи**', 'Вопросы допускают разные обоснованные ответы, не подсказывают правильную интерпретацию и не выходят за границу чтения.']
        for key, label in [('topics', 'Темы'), ('questions', 'Вопросы'), ('excerpts', 'Фрагменты'), ('notes', 'Заметки'), ('summary', 'Итоги')]:
            lines.extend((f'**{label}**', plan[key] or 'Пока пусто.'))
        chunks = pages(lines)
        for index, chunk in enumerate(chunks):
            await reply(interaction, chunk, view=PlanView(self, meeting, plan, interaction.user.id) if index == len(chunks) - 1 else None)

    @club.command(name='essay', description='Зарегистрировать эссе или исправить связь с книгой')
    async def essay(self, ctx, book: str, link: str, correct: bool = False):
        async with self.service.locks[ctx.guild.id]:
            member, organizer = await self.service.actor(ctx.guild, ctx.author.id)
            b = self.resolve_book(ctx.guild.id, book)
            await self.service.forum_access(ctx.guild, member, 'essays')
            match = re.fullmatch(r'https://(?:(?:www|canary|ptb)\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)(?:/(\d+))?/?', link.strip())
            if not match or int(match[1]) != ctx.guild.id:
                raise ClubError('Нужна ссылка на пост или сообщение этого сервера.')
            channel = await self.bot.fetch_channel(int(match[2]))
            if getattr(channel, 'guild', None) is None or channel.guild.id != ctx.guild.id:
                raise ClubError('Канал принадлежит другому серверу.')
            if not self.service.can_read(channel, member):
                raise ClubError('Нет доступа к исходному сообщению или истории его канала.')
            source_id = int(match[3] or match[2])
            verified_thread = False
            if isinstance(channel, discord.Thread) and channel.parent_id == self.store.settings(ctx.guild.id)['essays']:
                verified_thread = await self.service.register_thread(channel, prompt=False)
            managed = self.store.one('SELECT * FROM bc_essays WHERE guild_id=? AND channel_id=? AND managed=1',
                                     (ctx.guild.id, channel.id))
            imported = self.store.one('SELECT * FROM bc_import_sources WHERE guild_id=? AND thread_id=?',
                                      (ctx.guild.id, channel.id)) if verified_thread else None
            if imported and not managed:
                managed = self.store.one('SELECT * FROM bc_essays WHERE guild_id=? AND source_id=?',
                                          (ctx.guild.id, channel.id))
            if managed:
                if not organizer and ctx.author.id != managed['author_id']:
                    raise ClubError('Регистрировать чужую работу может организатор.')
                if not await self.service.register_thread(channel, book_id=b['id'], correct=correct, prompt=False):
                    raise ClubError('Не удалось подтвердить происхождение темы эссе; связь не изменена.')
                if b['id'] != self.store.one('SELECT book_id FROM bc_essays WHERE guild_id=? AND source_id=?',
                                            (ctx.guild.id, channel.id))['book_id']:
                    raise ClubError('Работа уже связана с другой книгой; используйте correct=True.')
                await self.service.refresh(ctx.guild)
                await self.say(ctx, 'Связь архивного эссе с книгой сохранена. Оригиналы и авторство сохранены.' if imported else
                                   'Связь поста с книгой сохранена. Эссе учитывается после собственного сообщения автора.')
                return
            if isinstance(channel, discord.Thread) and source_id == channel.id:
                message = await channel.fetch_message(channel.id)
                if message.webhook_id is not None or message.author.bot:
                    raise ClubError('Нужен пост участника или тема, созданная кнопкой клуба; неизвестный вебхук не подтверждает автора.')
                author_id, title, url = message.author.id, channel.name, channel.jump_url
            else:
                message = await channel.fetch_message(source_id)
                if message.webhook_id is not None or message.author.bot:
                    raise ClubError('Нужен оригинальный текст участника, а не сообщение вебхука или бота.')
                author_id, title, url = message.author.id, f'{b["title"]} · эссе', message.jump_url
            if not organizer and ctx.author.id != author_id:
                raise ClubError('Регистрировать чужую работу может организатор.')
            if author_id == self.bot.user.id or author_id is None:
                raise ClubError('Нужен оригинальный пост автора, а не карточка бота.')
            self.store.register_essay(ctx.guild.id, b['id'], source_id, channel.id, author_id, title, url, correct=correct)
            await self.service.refresh(ctx.guild)
        await self.say(ctx, 'Эссе связано с книгой. Оригинал и авторство сохранены.')

    @club.command(name='essays', description='Работы участников и кому ещё нужно эссе')
    async def essays(self, ctx, book: str):
        b = self.resolve_book(ctx.guild.id, book)
        await self.service.essay_access(ctx.guild, b['id'], ctx.author.id)
        missing = self.store.missing_essays(b['id'])
        label = 'Ещё нет зарегистрированного эссе' if not b['deadline'] or b['deadline'] > self.store.clock() else 'Срок наступил, эссе ещё не зарегистрировано'
        lines = [f'**{safe(b["title"])}**', label + ': ' + (', '.join(f'<@{p["user_id"]}>' for p in missing) or 'у всех участников есть работа')]
        lines.extend(essay_lines(self.store, b))
        for page in pages(lines):
            await self.say(ctx, page)

    @club.command(name='setup', description='Создать или проверить каналы книжного клуба')
    async def setup(self, ctx, check_only: bool = False, retry_missing: bool = False,
                    category: Optional[discord.CategoryChannel] = None, repair_permissions: bool = False):
        result = await self.service.setup_server(ctx.guild, ctx.author.id,
                                                 check_only=check_only, retry_missing=retry_missing, category=category,
                                                 repair_permissions=repair_permissions)
        for page in pages(result):
            await self.say(ctx, page)

    async def import_context(self, ctx):
        if ctx.interaction is None:
            raise ClubError('Используйте slash-команды /club import: коды входа и планы видны только вам.')
        await self.importer.guard(ctx.guild, ctx.author.id)

    async def show_import(self, ctx, run):
        for page in pages(self.importer.report(run)):
            await self.say(ctx, page)
        data = json.dumps({'run_id': run['id'], 'snapshot': run['snapshot'], 'plan': run['plan']}, ensure_ascii=False, indent=2).encode()
        await self.say(ctx, 'Полный снимок и план для проверки:',
                       file=discord.File(io.BytesIO(data), filename=f'import-{run["id"]}.json'))

    @club.group(name='import', description='Временный импорт архива через Codex', invoke_without_command=True)
    async def archive(self, ctx):
        await self.import_context(ctx)
        await self.say(ctx, 'Подготовка архива без модели: inventory → prepare → preview. '
                           'inventory показывает все доступные публичные треды, prepare сохраняет ваш выбор и данные книги. '
                           'preview канала читает подтверждённую очередь; preview с thread позволяет сначала ознакомиться '
                           'с неподтверждённым тредом. Продолжайте по cursor до конца выбранного списка. '
                           'Анализ через scan и новый лимит запусков согласуются отдельно. '
                           'restore восстанавливает failed/unknown по проверенному JSON без нового анализа. '
                           'Настройки и лимиты меняются на машине бота с перезапуском.')

    @archive.command(name='inventory', description='Все публичные треды архива и их подготовка, без модели')
    async def import_inventory(self, ctx, source: Optional[discord.TextChannel | discord.ForumChannel] = None,
                               page: int = 1):
        await self.import_context(ctx)
        if page < 1:
            raise ClubError('Номер страницы должен быть не меньше 1.')
        source, _ = self.import_range(ctx, source, None, None, None)
        inventory = await self.importer.preparation.inventory(ctx.guild, ctx.author.id, source.id)
        threads = inventory['threads']
        page_count = max(1, (len(threads) + 9) // 10)
        if page > page_count:
            raise ClubError(f'В перечне {page_count} стр.; выберите существующую страницу.')
        active = sum(not thread['archived'] for thread in threads)
        archived = len(threads) - active
        completeness = 'Полный перечень доступных публичных тредов' if inventory['complete'] else 'Неполный перечень публичных тредов'
        lines = [f'**{completeness}**: {len(threads)}; активных {active}, архивных {archived}. '
                 f'Страница {page}/{page_count}.',
                 'Это перечень тредов, а не число книг или прочитанных эссе. '
                 'Статусы просмотра относятся к сохранённым фрагментам; новые сообщения могут появиться позже. '
                 'Codex не вызван; квота запусков и токенов не изменена.']
        decisions = {'included': 'включён', 'excluded': 'исключён', 'pending': 'нужен выбор человека'}
        capture_states = {'unread': 'ещё не просмотрен', 'partial': 'просмотрен частично',
                          'complete': 'предпросмотр завершён', 'limited': 'не просмотрен из-за лимита'}
        for thread in threads[(page - 1) * 10:page * 10]:
            status = [decisions.get(thread['decision'], 'нужен выбор человека')]
            if thread.get('imported_messages', 0):
                status.append(f'уже импортирован: {thread["imported_messages"]} исходных сообщений')
            if thread.get('access') is False:
                status.append('нет доступа к истории')
            else:
                status.append(capture_states.get(thread.get('capture_state', 'unread'), 'ещё не просмотрен'))
            name = safe(thread['name'])
            state = 'архивный' if thread['archived'] else 'активный'
            lines.append(f'`{thread["id"]}` · **{name}** ({state}) — {"; ".join(status)}. '
                         f'[Открыть тред](https://discord.com/channels/{ctx.guild.id}/{thread["id"]})')
            if thread.get('title'):
                metadata = f'Книга: {safe(thread["title"])} · {safe(thread.get("author") or "автор не подтверждён")}'
                if thread.get('book_id'):
                    metadata += f' · ID `{thread["book_id"]}`'
                lines.append(metadata)
            elif thread.get('source_title'):
                lines.append(f'Первая строка источника (данные книги не подтверждены): {safe(thread["source_title"][:500])}')
        lines.extend(inventory.get('warnings', []))
        lines.append('Подтвердить выбор: /club import prepare thread:<ID> decision:included '
                     'title:<название> author:<автор> confirm:true; '
                     'для существующей книги укажите book вместо title/author. '
                     'decision:excluded исключает тред, pending возвращает на проверку.')
        if page < page_count:
            lines.append(f'Следующая страница: /club import inventory source:<#{source.id}> page:{page + 1}.')
        for text in pages(lines):
            await self.say(ctx, text)
        data = json.dumps(inventory, ensure_ascii=False, indent=2).encode()
        await self.say(ctx, 'Полный полученный перечень тредов для проверки:',
                       file=discord.File(io.BytesIO(data), filename='import-inventory.json'))

    @archive.command(name='prepare', description='Подтвердить книжный тред и его данные или исключить его')
    async def import_prepare(self, ctx, thread: str, decision: Literal['included', 'excluded', 'pending'],
                             source: Optional[discord.TextChannel | discord.ForumChannel] = None,
                             title: Optional[str] = None, author: Optional[str] = None,
                             book: Optional[str] = None, confirm: bool = False):
        await self.import_context(ctx)
        if not thread.isdecimal() or not 0 < int(thread) < 2**63:
            raise ClubError('thread: укажите числовой ID публичного треда Discord, в том числе архивного.')
        if not confirm:
            raise ClubError('Проверьте выбранный тред и данные книги, затем укажите confirm:true. '
                            'Включение требует title и author либо сопоставления с существующей book.')
        source, _ = self.import_range(ctx, source, None, None, None)
        book_id = self.resolve_book(ctx.guild.id, book)['id'] if book else None
        result = await self.importer.preparation.select(
            ctx.guild, ctx.author.id, source.id, int(thread), decision,
            title=title, author=author, book_id=book_id, confirm=True)
        labels = {'included': 'Тред включён в подготовку', 'excluded': 'Тред исключён из подготовки',
                  'pending': 'Тред возвращён на проверку человеком'}
        text = f'{labels[decision]}: `{thread}`.'
        if decision == 'included':
            text += f' Книга: **{safe(result["title"])}** · {safe(result["author"])}.'
        await self.say(ctx, text + ' Выбор сохранён по ID треда; переименование его не сбросит. '
                       'Исходные сообщения не изменены. Codex не вызван; квота не изменена.')

    @archive.command(name='login', description='Войти в отдельный профиль Codex по одноразовому коду')
    async def import_login(self, ctx):
        await self.import_context(ctx)
        login = await self.importer.login(ctx.guild, ctx.author.id)
        await self.say(ctx, f'Откройте {login["verification_uri"]} и введите код **{login["user_code"]}**.\n'
                       'Войдите в свой аккаунт на странице OpenAI. Пароль и токены боту не отправляйте. '
                       'После подтверждения проверьте /club import status. Код временный.')

    @archive.command(name='status', description='Проверить вход Codex и сохранённые лимиты импорта')
    async def import_status(self, ctx):
        await self.import_context(ctx)
        for page in pages(await self.importer.status(ctx.guild, ctx.author.id)):
            await self.say(ctx, page)

    @archive.command(name='scan', description='Составить план переноса эссе из выбранного канала и тредов')
    async def import_scan(self, ctx, source: Optional[discord.TextChannel | discord.ForumChannel] = None,
                          before: Optional[str] = None, thread: Optional[str] = None,
                          after: Optional[str] = None, cursor: Optional[str] = None):
        await self.import_context(ctx)
        source, range_options = self.import_range(ctx, source, before, thread, after, cursor)
        run = await self.importer.scan(ctx.guild, ctx.author.id, source.id, str(ctx.interaction.id), **range_options)
        await self.show_import(ctx, run)

    @staticmethod
    def import_range(ctx, source, before, thread, after, cursor=None):
        if cursor is not None and (thread is not None or before is not None or after is not None):
            raise ClubError('cursor уже содержит точное продолжение; не совмещайте его с thread/before/after.')
        source = source or (thread.parent if isinstance(thread, discord.Thread) else ctx.channel)
        if isinstance(source, discord.Thread):
            if cursor is None and thread is None:
                thread = source
            source = source.parent
        def message_id(value, field='before/after'):
            if value is not None and (not value.isdecimal() or not 0 < int(value) < 2**63):
                raise ClubError(f'{field}: укажите числовой ID Discord.')
            return int(value) if value else None
        if source is None:
            raise ClubError('Не удалось определить исходный канал.')
        options = {'before_id': message_id(before),
                   'thread_id': thread.id if isinstance(thread, discord.Thread) else message_id(thread, 'thread'),
                   'after_id': message_id(after)}
        if cursor is not None:
            options['cursor'] = cursor
        return source, options

    @archive.command(name='preview', description='Проверить выбранный фрагмент архива без расхода Codex')
    async def import_preview(self, ctx, source: Optional[discord.TextChannel | discord.ForumChannel] = None,
                             before: Optional[str] = None, thread: Optional[str] = None,
                             after: Optional[str] = None, cursor: Optional[str] = None):
        await self.import_context(ctx)
        source, range_options = self.import_range(ctx, source, before, thread, after, cursor)
        snapshot = await self.importer.preview(ctx.guild, ctx.author.id, source.id, **range_options)
        data = json.dumps(snapshot, ensure_ascii=False).encode()
        lines = [f'**Предпросмотр архива**: {len(snapshot["messages"])} сообщений, '
                 f'{len(snapshot["books"])} подтверждённых книг в этом фрагменте, '
                 f'{len(data)}/{self.importer.config.max_input_bytes} байт.',
                 'Codex не вызван; квота запусков и токенов не изменена. '
                 'Полный перечень тредов и оставшиеся проверки — /club import inventory. '
                 'Новый лимит запусков и анализ моделью согласуются отдельно.']
        coverage = snapshot.get('coverage', {})
        if snapshot.get('preparation_confirmed') is False:
            lines.append('Ознакомительный фрагмент: название книги, автор и включение треда ещё не подтверждены. '
                         'Проверьте источник и сохраните решение через /club import prepare; '
                         'этот фрагмент не разрешает анализ моделью.')
        if 'total_threads' in coverage:
            scope = 'Ознакомительный просмотр' if snapshot.get('preparation_confirmed') is False else 'Подтверждённая очередь'
            lines.append(f'{scope}: завершено {coverage["completed_threads"]} '
                         f'из {coverage["total_threads"]} тредов. Это не охват всего архива.')
        if coverage.get('range_limited'):
            lines.append('Выбран диапазон сообщений; завершение этого диапазона не означает просмотр всего треда.')
        if snapshot.get('continuation'):
            lines.append(f'Продолжение: /club import preview source:<#{source.id}> cursor:{snapshot["continuation"]}')
        elif coverage.get('complete'):
            if snapshot.get('preparation_confirmed') is False:
                lines.append('Ознакомительный просмотр выбранного треда завершён; решение о книге ещё требуется.')
            else:
                lines.append('Предпросмотр выбранных подтверждённых тредов завершён. '
                             'Исключённые и неподтверждённые треды в этот охват не входят.')
        lines.extend(snapshot['warnings'])
        for page in pages(lines):
            await self.say(ctx, page)
        await self.say(ctx, 'Снимок выбранного фрагмента для проверки:',
                       file=discord.File(io.BytesIO(data), filename='import-preview.json'))

    @archive.command(name='restore', description='Восстановить неудачный анализ по проверенному JSON без Codex')
    async def import_restore(self, ctx, run: str, file: discord.Attachment, confirm: bool = False):
        await self.import_context(ctx)
        if not confirm:
            raise ClubError('Сначала проверьте JSON плана, затем укажите confirm:true. '
                            'Файл содержит только {"essays":[{"book_ref":"…","message_ids":["…"]}]}.')
        if file.size > 65_536:
            raise ClubError('План JSON должен быть не больше 64 КиБ.')
        data = await asyncio.wait_for(file.read(), timeout=30)
        if len(data) > 65_536:
            raise ClubError('План JSON должен быть не больше 64 КиБ.')
        try:
            output = json.loads(data.decode('utf-8-sig'))
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise ClubError('Не удалось прочитать план: нужен JSON в UTF-8.') from exc
        restored = await self.importer.restore_plan(ctx.guild, ctx.author.id, run, output, confirm=True)
        await self.show_import(ctx, restored)

    @archive.command(name='review', description='Показать сохранённый план без нового запроса к Codex')
    async def import_review(self, ctx, run: str):
        await self.import_context(ctx)
        await self.show_import(ctx, await self.importer.review(ctx.guild, ctx.author.id, run))

    @archive.command(name='apply', description='Перенести архивные эссе по проверенному плану')
    async def import_apply(self, ctx, run: str, confirm: bool = False):
        await self.import_context(ctx)
        for page in pages(await self.importer.apply(ctx.guild, ctx.author.id, run, confirm=confirm)):
            await self.say(ctx, page)

    @archive.command(name='restyle', description='Исправить оформление перенесённых эссе без нового анализа')
    async def import_restyle(self, ctx, run: str, confirm: bool = False):
        await self.import_context(ctx)
        for page in pages(await self.importer.restyle(ctx.guild, ctx.author.id, run, confirm=confirm)):
            await self.say(ctx, page)

    @club.command(name='diagnose', description='Проверить настройку без отправок и создания каналов')
    async def diagnose(self, ctx):
        await self.organizer(ctx)
        for page in pages(await self.service.diagnose(ctx.guild)):
            await self.say(ctx, page)

    @club.command(name='publish', description='Опубликовать или обновить каталог и карточки')
    async def publish(self, ctx):
        async with self.service.locks[ctx.guild.id]:
            await self.organizer(ctx)
            diagnostics = await self.service.diagnose(ctx.guild)
            failures = [x for x in diagnostics if 'не хватает' in x or 'недоступен' in x or 'Community' in x or 'не найдена' in x]
            if failures:
                raise ClubError('\n'.join(failures))
            await self.service.reconcile(ctx.guild)
            self.store.set_published(ctx.guild.id)
            # Verify saved messages even when the content hash is unchanged.
            with self.store.tx() as db:
                db.execute('UPDATE bc_publications SET content_hash=NULL WHERE guild_id=?', (ctx.guild.id,))
            await self.service.refresh(ctx.guild)
        await self.say(ctx, 'Каталог, карточки и Вестник обновлены. Напоминания включены.')

    @club.command(name='repair', description='Восстановить неподтверждённые публикации после ручной проверки')
    async def repair(self, ctx, checked_absent: bool = False):
        async with self.service.locks[ctx.guild.id]:
            await self.organizer(ctx)
            if not checked_absent:
                raise ClubError('Сначала /club diagnose: проверьте, что перечисленные неподтверждённые публикации отсутствуют. Затем checked_absent=True. Существующие карточки не удаляются.')
            for pub in self.store.rows("SELECT * FROM bc_publications WHERE guild_id=? AND state='reserved'", (ctx.guild.id,)):
                channel = await self.service.channel(ctx.guild, pub['channel_id'])
                found = await self.service.delivery.recover(ctx.guild, channel, pub['key'],
                    forum=isinstance(channel, discord.ForumChannel), webhook_id=pub['webhook_id'])
                if found:
                    self.store.save_publication(pub['key'], found[0].id, found[1].id)
                else:
                    self.store.forget_publication(pub['key'])
            for binding in self.store.rows('SELECT * FROM bc_webhooks WHERE guild_id=? AND webhook_id IS NULL', (ctx.guild.id,)):
                forum = await self.service.channel(ctx.guild, binding['channel_id'], discord.ForumChannel)
                hooks = [h for h in await forum.webhooks() if h.name == ESSAY_WEBHOOK_NAME
                         and self.service.valid_essay_webhook(h, ctx.guild, forum.id)]
                if len(hooks) > 1:
                    raise ClubError('В форуме несколько вебхуков эссе. Организатору нужно проверить интеграции.')
                if hooks:
                    self.store.save_webhook(ctx.guild.id, forum.id, hooks[0].id)
                else:
                    self.store.forget_webhook(ctx.guild.id, forum.id)
            await self.service.refresh(ctx.guild)
        await self.say(ctx, 'Публикации восстановлены. Неопределённые напоминания автоматически не повторяются.')

    @club.command(name='recover_event', description='Привязать событие после неподтверждённого создания')
    async def recover_event(self, ctx, meeting: str, event: str):
        async with self.service.locks[ctx.guild.id]:
            await self.organizer(ctx)
            m = self.resolve_meeting(ctx.guild.id, meeting)
            events = await ctx.guild.fetch_scheduled_events()
            matches = [e for e in events if str(e.id) == event or e.name.casefold() == event.casefold()]
            if len(matches) != 1 or matches[0].entity_type != discord.EntityType.voice:
                raise ClubError('Выберите существующее голосовое событие. Если создание точно не произошло, создайте событие в Discord вручную и выберите его здесь.')
            self.service.sync(ctx.guild, m, matches[0])
            await self.service.refresh(ctx.guild)
        await self.say(ctx, 'Связь восстановлена.')

    async def book_autocomplete(self, interaction, current):
        try:
            await asyncio.wait_for(self.service.actor(interaction.guild, interaction.user.id), timeout=2)
        except (ClubError, discord.HTTPException, asyncio.TimeoutError):
            return []
        return [app_commands.Choice(name=f'{b["title"]} · {b["author"]} (№{b["position"]})'[:100], value=b['id']) for b in self.store.books(interaction.guild_id) if current.casefold() in (b['title'] + ' ' + b['author']).casefold()][:25]

    async def meeting_autocomplete(self, interaction, current):
        try:
            await asyncio.wait_for(self.service.actor(interaction.guild, interaction.user.id), timeout=2)
        except (ClubError, discord.HTTPException, asyncio.TimeoutError):
            return []
        return [app_commands.Choice(name=f'{m["name"]} · {m["part"]} / {m["chapter"]}'[:100], value=m['id']) for m in self.store.rows('SELECT * FROM bc_meetings WHERE guild_id=? ORDER BY start DESC,id', (interaction.guild_id,)) if current.casefold() in m['name'].casefold()][:25]

    async def event_autocomplete(self, interaction, current):
        async def accessible_channels():
            member, _ = await self.service.actor(interaction.guild, interaction.user.id)
            channels = {c.id: c for c in await interaction.guild.fetch_channels()}
            return member, channels

        try:
            member, channels = await asyncio.wait_for(accessible_channels(), timeout=2)
        except (ClubError, discord.HTTPException, asyncio.TimeoutError):
            return []
        return [app_commands.Choice(name=e.name[:100], value=str(e.id))
                for e in interaction.guild.scheduled_events
                if current.casefold() in e.name.casefold() and e.entity_type == discord.EntityType.voice
                and e.channel_id in channels and channels[e.channel_id].permissions_for(member).view_channel][:25]

    @commands.Cog.listener()
    async def on_scheduled_event_update(self, before, after):
        await self.event_changed(after)

    @commands.Cog.listener()
    async def on_scheduled_event_create(self, event):
        await self.event_changed(event)

    async def event_changed(self, event):
        if self.service.guild_ids is not None and event.guild_id not in self.service.guild_ids:
            return
        m = self.store.one('SELECT * FROM bc_meetings WHERE event_id=?', (event.id,))
        if not m:
            return  # create operation/reconciliation binds draft markers
        guild = self.bot.get_guild(event.guild_id)
        if guild:
            try:
                async with self.service.locks[guild.id]:
                    current = self.store.meeting(guild.id, m['id'])
                    if event.status in (discord.EventStatus.completed, discord.EventStatus.cancelled):
                        # Terminal Gateway state remains authoritative even when
                        # Discord immediately removes the event from REST.
                        self.service.sync(guild, current, event)
                    else:
                        # Fetch current state: gateway updates can arrive out of order.
                        await self.service.sync_one(guild, current)
                    await self.service.refresh(guild)
            except Exception:
                log.exception('Book club event update failed; periodic reconciliation will retry')

    @commands.Cog.listener()
    async def on_scheduled_event_delete(self, event):
        await self.event_changed(event)

    @commands.Cog.listener()
    async def on_thread_create(self, thread):
        if self.service.guild_ids is not None and thread.guild.id not in self.service.guild_ids:
            return
        if not self.store.one('SELECT 1 FROM bc_settings WHERE guild_id=?', (thread.guild.id,)):
            return
        try:
            async with self.service.locks[thread.guild.id]:
                if await self.service.register_thread(thread):
                    await self.service.refresh(thread.guild)
        except Exception:
            log.exception('Essay registration failed; author can register with /club essay')

    @commands.Cog.listener()
    async def on_raw_thread_update(self, payload):
        # Do not derive book identity from a rename. Resolve thread only for known essay forums.
        settings = self.store.one('SELECT 1 FROM bc_settings WHERE guild_id=?', (payload.guild_id,))
        if not settings:
            return
        try:
            thread = await self.bot.fetch_channel(payload.thread_id)
            await self.on_thread_create(thread)
        except discord.NotFound:
            self.store.delete_essay(payload.guild_id, channel_id=payload.thread_id)

    @commands.Cog.listener()
    async def on_raw_thread_delete(self, payload):
        async with self.service.locks[payload.guild_id]:
            self.store.delete_essay(payload.guild_id, channel_id=payload.thread_id)
            with self.store.tx() as db:
                db.execute('DELETE FROM bc_publications WHERE guild_id=? AND channel_id=?', (payload.guild_id, payload.thread_id))

    @commands.Cog.listener()
    async def on_message(self, message):
        if not message.guild or message.author.bot or not isinstance(message.channel, discord.Thread):
            return
        essay = self.store.one('SELECT * FROM bc_essays WHERE guild_id=? AND channel_id=? AND managed=1',
                               (message.guild.id, message.channel.id))
        if essay and essay['author_id'] == message.author.id:
            await self.on_thread_create(message.channel)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload):
        await self.refresh_essay_message(payload)

    async def refresh_essay_message(self, payload):
        if payload.guild_id and self.store.one('SELECT 1 FROM bc_essays WHERE guild_id=? AND channel_id=? AND managed=1',
                                               (payload.guild_id, payload.channel_id)):
            try:
                channel = await self.bot.fetch_channel(payload.channel_id)
                await self.on_thread_create(channel)
            except discord.NotFound:
                self.store.delete_essay(payload.guild_id, channel_id=payload.channel_id)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload):
        if payload.guild_id:
            async with self.service.locks[payload.guild_id]:
                self.store.delete_essay(payload.guild_id, source_id=payload.message_id)
                with self.store.tx() as db:
                    db.execute('DELETE FROM bc_publications WHERE guild_id=? AND message_id=?', (payload.guild_id, payload.message_id))
            if getattr(payload, 'channel_id', None) is not None:
                await self.refresh_essay_message(payload)

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        async with self.service.locks[member.guild.id]:
            for b in self.store.books(member.guild.id):
                if any(p['user_id'] == member.id for p in self.store.participants(b['id'])):
                    self.store.participant(member.guild.id, b['id'], member.id, joined=False)


# Attach autocomplete callbacks before Cog copies the hybrid commands.
Club.import_prepare.autocomplete('book')(Club.book_autocomplete)
for _command in Club.club.commands:
    if _command.app_command:
        for _param, _callback in [('book', Club.book_autocomplete), ('meeting', Club.meeting_autocomplete), ('event', Club.event_autocomplete)]:
            if _param in _command.clean_params:
                _command.autocomplete(_param)(_callback)

"""Discord adapter, reconciliation and durable delivery worker.

Run a single bot process per token/database. SQLite guards domain races; a guild
lock serializes Discord projections with actions in this process.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from collections import defaultdict
import hashlib
import logging
import json

import discord

from .store import ClubError
from .forum_tags import ForumTags
from .publication_delivery import DeliveryJournal
from .single_delivery import channel_send_once, create_thread_once, webhook_send_once, create_event_once
from .event_delivery import event_description, find_draft_events
from .render import book_pages, catalog_pages, news_content, meeting_lines, pages, safe, book_url
from .club_format import format_content, get_format

log = logging.getLogger(__name__)
NO_MENTIONS = discord.AllowedMentions.none()
ESSAY_WEBHOOK_NAME = 'Архивариус · эссе'


class Service:
    def __init__(self, bot, store, guild_ids=None):
        self.bot, self.store = bot, store
        self.guild_ids = guild_ids
        self.locks = defaultdict(asyncio.Lock)
        self.view_factory = None
        self.book_view_factory = None
        self.catalog_view_factory = None
        self.format_view_factory = None
        self.forum_tags = ForumTags(self)
        self.delivery = DeliveryJournal(self)
        self.last_essay_scan = {}

    async def setup_actor(self, guild, user_id):
        if guild is None:
            raise ClubError('Настройка доступна только на сервере.')
        if self.guild_ids is not None and guild.id not in self.guild_ids:
            raise ClubError('Этот сервер отключён в конфигурации бота.')
        try:
            member = await guild.fetch_member(user_id)
        except discord.NotFound as exc:
            raise ClubError('Участник больше не состоит на сервере.') from exc
        if member.bot or not (member.id == guild.owner_id or member.guild_permissions.manage_guild
                              or member.guild_permissions.administrator):
            raise ClubError('Настройка каналов доступна владельцу сервера или участнику с правом Manage Server.')
        return member

    async def setup_server(self, guild, actor_id, *, check_only=False, retry_missing=False, category=None,
                           repair_permissions=False):
        if guild is None:
            raise ClubError('Настройка доступна только на сервере.')
        from .provision import Provisioner
        return await Provisioner(self).run(guild, actor_id, check_only=check_only,
                                           retry_missing=retry_missing, category=category,
                                           repair_permissions=repair_permissions)

    async def actor(self, guild, user_id, *, require_access=True):
        if guild is None:
            raise ClubError('Команда доступна только на сервере.')
        if self.guild_ids is not None and guild.id not in self.guild_ids:
            raise ClubError('Клуб на этом сервере отключён в конфигурации.')
        settings = self.store.settings(guild.id)
        try:
            member = await guild.fetch_member(user_id)
        except discord.NotFound as exc:
            raise ClubError('Участник больше не состоит на сервере.') from exc
        if member.bot:
            raise ClubError('Нужно выбрать участника, а не бота.')
        organizer = (member.id == guild.owner_id or member.id in settings['organizers']
                     or bool({r.id for r in member.roles} & set(settings['organizer_roles'])))
        if require_access:
            await self.forum_access(guild, member)
        return member, organizer

    @staticmethod
    def can_read(channel, member):
        permissions = channel.permissions_for(member)
        return bool(permissions.view_channel and permissions.read_message_history)

    async def fresh_forum(self, guild, purpose='books'):
        forum = await self.bot.fetch_channel(self.store.settings(guild.id)[purpose])
        if not isinstance(forum, discord.ForumChannel) or forum.guild.id != guild.id:
            raise ClubError('Форум клуба недоступен. Организатору: проверьте /club diagnose.')
        return forum

    async def forum_access(self, guild, member, purpose='books'):
        forum = await self.fresh_forum(guild, purpose)
        if not self.can_read(forum, member):
            raise ClubError('Нет доступа к форуму клуба или его истории. Обратитесь к организатору.')
        return forum

    def interaction_access(self, interaction):
        """Check the current interaction member before opening a prefilled modal.

        Channel overwrites come from the gateway cache to meet Discord's modal
        response deadline; every submission repeats authorization through REST.
        """
        if not interaction.guild or not isinstance(interaction.user, discord.Member) or interaction.user.bot:
            raise ClubError('Откройте действие на сервере клуба.')
        if self.guild_ids is not None and interaction.guild_id not in self.guild_ids:
            raise ClubError('Клуб на этом сервере отключён в конфигурации.')
        forum = interaction.guild.get_channel(self.store.settings(interaction.guild_id)['books'])
        if not isinstance(forum, discord.ForumChannel) or not self.can_read(forum, interaction.user):
            raise ClubError('Нет доступа к форуму клуба или его истории. Обратитесь к организатору.')

    def interaction_organizer(self, interaction):
        """The interaction carries a fresh Discord Member, without an extra HTTP wait."""
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            raise ClubError('Откройте действие на сервере клуба.')
        settings = self.store.settings(interaction.guild_id)
        return (interaction.user.id == interaction.guild.owner_id
                or interaction.user.id in settings['organizers']
                or bool({r.id for r in interaction.user.roles} & set(settings['organizer_roles'])))

    async def live_participants(self, guild):
        ids = self.store.rows('''SELECT DISTINCT p.user_id FROM bc_participants p
          JOIN bc_books b ON b.id=p.book_id WHERE b.guild_id=? AND p.present=1''', (guild.id,))
        live = set()
        forum = await self.fresh_forum(guild) if ids else None
        for row in ids:
            try:
                member = await guild.fetch_member(row['user_id'])
                if not member.bot and self.can_read(forum, member):
                    live.add(member.id)
            except discord.NotFound:
                for b in self.store.books(guild.id):
                    if any(p['user_id'] == row['user_id'] for p in self.store.participants(b['id'])):
                        self.store.participant(guild.id, b['id'], row['user_id'], joined=False)
        return live

    async def channel(self, guild, channel_id, expected=None):
        channel = guild.get_channel_or_thread(channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(channel_id)
        if getattr(channel, 'guild', None) is None or channel.guild.id != guild.id:
            raise ClubError('Канал принадлежит другому серверу.')
        if expected and not isinstance(channel, expected):
            raise ClubError('Тип настроенного канала не соответствует его назначению.')
        return channel

    async def diagnose(self, guild, *, channels=None, bot_member=None):
        settings = self.store.settings(guild.id)
        result = []
        for key, expected in [('news', discord.TextChannel), ('chat', discord.TextChannel),
                              ('books', discord.ForumChannel), ('essays', discord.ForumChannel),
                              ('voice', discord.VoiceChannel)]:
            try:
                if channels is None:
                    channel = await self.channel(guild, settings[key], expected)
                else:
                    channel = next((c for c in channels if c.id == settings[key]), None)
                    if channel is None:
                        channel = await self.bot.fetch_channel(settings[key])
                    if not isinstance(channel, expected) or channel.guild.id != guild.id:
                        raise ClubError('Неверный канал.')
                permissions = channel.permissions_for(bot_member or guild.me)
                required = ['view_channel']
                if key != 'voice':
                    required += ['send_messages', 'read_message_history']
                if key in ('books', 'essays'):
                    required += ['send_messages_in_threads']
                if key in ('books', 'essays'):
                    required += ['manage_threads']
                if key == 'essays' and settings['essay_webhooks']:
                    required += ['manage_webhooks']
                if key == 'voice':
                    required += ['connect', 'create_events', 'manage_events']
                missing = [p for p in required if not getattr(permissions, p, False)]
                if key in ('books', 'essays'):
                    try:
                        kinds = ('catalog', 'proposed', 'queued', 'reading', 'read') if key == 'books' else ('draft', 'essay', 'imported')
                        for kind in kinds:
                            self.forum_tags.creation_tags(guild.id, channel, kind)
                    except ClubError as exc:
                        missing.append(str(exc))
                result.append(f'{key}: ' + ('не хватает: ' + ', '.join(missing) if missing else 'OK'))
            except (discord.HTTPException, ClubError):
                result.append(f'{key}: канал недоступен или имеет неверный тип')
        if 'COMMUNITY' not in guild.features:
            result.append('Для форумов включите Community вручную в настройках сервера.')
        for role_id in settings['organizer_roles'] + ([settings['reminder_role']] if settings['reminder_role'] else []):
            if guild.get_role(role_id) is None:
                result.append(f'Роль {role_id} не найдена.')
        pending = self.store.rows("SELECT key,state FROM bc_publications WHERE guild_id=? AND state<>'ready'", (guild.id,))
        result.extend(f'Незавершённая публикация: {p["key"]}. /club repair после проверки.' for p in pending)
        drafts = self.store.rows("SELECT name FROM bc_meetings WHERE guild_id=? AND event_id IS NULL", (guild.id,))
        result.extend(f'Событие не подтверждено: {safe(m["name"])}. /club recover_event.' for m in drafts)
        failures = self.store.rows("SELECT kind,state,detail FROM bc_jobs WHERE guild_id=? AND state IN ('failed','unknown') ORDER BY due DESC LIMIT 10", (guild.id,))
        result.extend(f'Напоминание {j["kind"]}: {j["state"]} · {j["detail"]}' for j in failures)
        for binding in self.store.rows('SELECT * FROM bc_webhooks WHERE guild_id=?', (guild.id,)):
            if binding['webhook_id'] is None:
                result.append('Создание вебхука эссе не подтверждено. Проверьте интеграции форума, затем /club repair.')
        for row in self.store.rows('SELECT DISTINCT webhook_id FROM bc_publications WHERE guild_id=? AND webhook_id IS NOT NULL', (guild.id,)):
            try:
                await self.bot.fetch_webhook(row['webhook_id'])
            except discord.NotFound:
                result.append(f'Вебхук {row["webhook_id"]} удалён: старые эссе учитываются, но их шапки нельзя обновить.')
            except discord.HTTPException:
                result.append(f'Не удалось проверить вебхук {row["webhook_id"]}. Проверьте Manage Webhooks и повторите диагностику.')
        return result

    def sync(self, guild, meeting, event):
        if event.guild_id != guild.id:
            raise ClubError('Событие принадлежит другому серверу.')
        status = 'cancelled' if event.status == discord.EventStatus.cancelled else event.status.name
        if status in ('scheduled', 'active') and (event.entity_type != discord.EntityType.voice or event.channel_id is None):
            status = 'unsupported'
        return self.store.sync_event(guild.id, meeting['id'], event_id=event.id, name=event.name,
                                     start=int(event.start_time.timestamp()),
                                     end=int(event.end_time.timestamp()) if event.end_time else None,
                                     voice_id=event.channel_id,
                                     status=status)

    async def sync_one(self, guild, meeting):
        if not meeting['event_id']:
            return False
        try:
            event = await guild.fetch_scheduled_event(meeting['event_id'])
        except discord.NotFound:
            if meeting['status'] in ('cancelled', 'completed'):
                return False
            return self.store.sync_event(guild.id, meeting['id'], event_id=meeting['event_id'],
                                         name=meeting['name'], start=meeting['start'], end=meeting['end'],
                                         voice_id=meeting['voice_id'], status='cancelled', status_confirmed=False)
        return self.sync(guild, meeting, event)

    async def reconcile(self, guild):
        events = await guild.fetch_scheduled_events()
        self.cache_schedule(guild, events)
        by_id = {e.id: e for e in events}
        changed = False
        for m in self.store.rows('SELECT * FROM bc_meetings WHERE guild_id=?', (guild.id,)):
            if not m['event_id']:
                matches = find_draft_events(self.store, guild, m, events, self.bot.user.id)
                if len(matches) == 1:
                    changed = self.sync(guild, m, matches[0]) or changed
                elif len(matches) > 1:
                    log.error('Multiple Discord events for book club meeting %s; manual repair required', m['id'])
            elif m['event_id'] in by_id:
                changed = self.sync(guild, m, by_id[m['event_id']]) or changed
            elif m['status'] not in ('cancelled', 'completed') or not m.get('event_status_confirmed', 1):
                changed = await self.sync_one(guild, m) or changed
        return self.store.reconcile_book_statuses(guild.id) or changed

    def cache_schedule(self, guild, events):
        selected = set(json.loads(get_format(self.store, guild.id)['schedule_ids']))
        with self.store.tx() as db:
            db.execute('DELETE FROM bc_schedule_events WHERE guild_id=?', (guild.id,))
            for event in events:
                if (event.id in selected and event.guild_id == guild.id
                        and event.entity_type == discord.EntityType.voice):
                    db.execute('INSERT INTO bc_schedule_events VALUES(?,?,?,?,?,?,?)', (
                        guild.id, event.id, event.name, int(event.start_time.timestamp()),
                        int(event.end_time.timestamp()) if event.end_time else None,
                        event.channel_id, event.status.name))

    async def create_event(self, guild, meeting, start, end):
        if meeting['event_id']:
            return meeting
        if start <= self.store.clock() or end <= start:
            raise ClubError('Укажите будущую дату и окончание позже начала.')
        settings = self.store.settings(guild.id)
        voice = await self.channel(guild, settings['voice'], discord.VoiceChannel)
        description = event_description(self.store, guild.id, meeting)
        events = await guild.fetch_scheduled_events()
        with self.store.tx() as db:
            if db.execute('SELECT 1 FROM bc_event_intents WHERE meeting_id=?', (meeting['id'],)).fetchone():
                raise ClubError('Создание события уже начато. Проверьте /club recover_event перед повтором.')
            db.execute('INSERT INTO bc_event_intents VALUES(?,?,?)', (meeting['id'],
                       max((e.id for e in events), default=0), json.dumps(dict(name=meeting['name'],
                       description=description, voice_id=voice.id, start=start, end=end))))
        event = await create_event_once(guild,
            name=meeting['name'], start_time=datetime.fromtimestamp(start, timezone.utc),
            end_time=datetime.fromtimestamp(end, timezone.utc),
            channel=voice, entity_type=discord.EntityType.voice, privacy_level=discord.PrivacyLevel.guild_only,
            description=description,
            reason='Встреча книжного клуба')
        self.sync(guild, meeting, event)
        return self.store.meeting(guild.id, meeting['id'])

    def owns_starter(self, message, webhook_id=None):
        actual = getattr(message, 'webhook_id', None)
        if webhook_id is not None:
            return actual == webhook_id
        return actual is None and message.author.id == self.bot.user.id

    async def _find_marker(self, guild, channel, marker, forum=False, *, webhook_id=None):
        matches = {}
        if forum:
            threads = [t for t in await guild.active_threads() if t.parent_id == channel.id]
            threads.extend([t async for t in channel.archived_threads(limit=None)])
            for thread in {t.id: t for t in threads}.values():
                if webhook_id is None and thread.owner_id != self.bot.user.id:
                    continue
                try:
                    message = await thread.fetch_message(thread.id)
                except discord.NotFound:
                    continue
                if marker.strip('\n') in message.content.splitlines() and self.owns_starter(message, webhook_id):
                    matches[message.id] = (thread, message)
        else:
            async for message in channel.history(limit=None):
                if marker.strip('\n') in message.content.splitlines() and self.owns_starter(message, webhook_id):
                    matches[message.id] = (channel, message)
        if len(matches) > 1:
            raise ClubError('Найдено несколько прежних сообщений с одним маркером. '
                            'Автоматическая привязка запрещена; организатору нужно проверить копии.')
        return next(iter(matches.values()), None)

    async def upsert(self, guild, key, channel_id, content, *, forum_name=None, view=None, files=None):
        if len(content) > 2000:
            raise ClubError('Карточка слишком длинная; требуется разбивка на страницы.')
        managed_name = forum_name[:100] if forum_name and key.startswith(('book:', 'catalog:')) else None
        signature = 'clean-delivery-v1\n' + content + repr([(c.to_component_dict(), c.row) for c in view.children] if view else []) + repr(managed_name)
        digest = hashlib.sha256(signature.encode()).hexdigest()
        pub = self.store.publication(key)
        channel = None
        message = None
        created_message = False
        if pub:
            if forum_name and pub['message_id']:
                try:
                    channel = await self.channel(guild, pub['channel_id'])
                except discord.NotFound:
                    channel = None
                destination_changed = (not isinstance(channel, discord.Thread)
                                       or channel.parent_id != channel_id)
            else:
                destination_changed = pub['channel_id'] != channel_id
            if destination_changed:
                # Configuration controls the destination even when content is
                # unchanged. Leave the old Discord message and history intact.
                self.store.forget_publication(key)
                pub = None
                channel = None
        if pub and pub['message_id']:
            if pub['content_hash'] == digest:
                return pub
            try:
                if channel is None:
                    channel = await self.channel(guild, pub['channel_id'])
                message = await channel.fetch_message(pub['message_id'])
            except discord.NotFound:
                self.store.forget_publication(key)
                pub = None
        if message is None:
            channel = await self.channel(guild, channel_id, discord.ForumChannel if forum_name else None)
            tags = []
            if forum_name:
                channel = await self.forum_tags.fresh(guild, channel)
                tags = self.publication_tags(guild, channel, key)
            fresh = await self.delivery.reserve(guild, channel, key, content, forum_name=forum_name, view=view, files=files)
            if not fresh:
                found = await self.delivery.recover(guild, channel, key, forum=bool(forum_name))
                if not found:
                    raise ClubError('Предыдущая отправка не подтверждена. Организатору: /club diagnose и /club repair.')
                channel, message = found
            elif forum_name:
                created = await create_thread_once(channel, name=forum_name[:100], content=content, view=view or discord.utils.MISSING,
                                                      applied_tags=tags, allowed_mentions=NO_MENTIONS)
                channel, message = created.thread, created.message
                created_message = True
            else:
                if isinstance(channel, discord.Thread) and channel.archived:
                    channel = await channel.edit(archived=False)
                extra = {'files': files} if files else {}
                message = await channel_send_once(channel, content, view=view, allowed_mentions=NO_MENTIONS,
                                                  nonce=self.delivery.intent(key)['nonce'], **extra)
                created_message = True
        if not self.owns_starter(message):
            raise ClubError('Сохранённая карточка принадлежит другому автору; бот её не редактирует.')
        # The recovered ID must survive a second lost response while editing
        # changed content; the old intent then no longer matches the message.
        self.store.save_publication(key, channel.id, message.id)
        if managed_name and isinstance(channel, discord.Thread) and channel.owner_id == self.bot.user.id:
            # Only rename a title that still matches our last assignment. Native
            # Discord renames are a deliberate override, including after restart.
            previous_name = pub.get('managed_name') if pub else None
            if channel.name != managed_name and previous_name and channel.name == previous_name:
                options = {'name': managed_name}
                if channel.archived:
                    options['archived'] = False
                channel = await channel.edit(**options)
            if channel.name == managed_name:
                self.store.remember_publication_name(key, managed_name)
        if not created_message:
            if isinstance(channel, discord.Thread) and channel.archived:
                channel = await channel.edit(archived=False)
            await message.edit(content=content, view=view, allowed_mentions=NO_MENTIONS)
        self.store.save_publication(key, channel.id, message.id, digest)
        return self.store.publication(key)

    def publication_tags(self, guild, forum, key):
        if key.startswith('book:'):
            book = self.store.book(guild.id, key.split(':')[1])
            return self.forum_tags.creation_tags(guild.id, forum, 'book', book['status'])
        kind = ('catalog' if key.startswith('catalog:') else
                'imported' if key.startswith('essay-import:') else 'draft')
        return self.forum_tags.creation_tags(guild.id, forum, kind)

    async def paged_post(self, guild, key, forum_id, name, contents, *, view=None, inline_first=False):
        # One forum post; pages are ordinary bot replies inside it. Only catalog is pinned.
        root = self.store.publication(key)
        if root:
            detach = root['channel_id'] != forum_id if not root['message_id'] else False
            if root['message_id']:
                try:
                    thread = await self.channel(guild, root['channel_id'])
                    detach = not isinstance(thread, discord.Thread) or thread.parent_id != forum_id
                    if not detach:
                        await thread.fetch_message(root['message_id'])
                except discord.NotFound:
                    detach = True
            if detach:
                # A forum change detaches the root and its pages atomically.
                # Meeting projections follow the new root in ordinary upsert;
                # no messages in the old forum are edited or removed.
                with self.store.tx() as db:
                    db.execute('DELETE FROM bc_publications WHERE guild_id=? AND (key=? OR key LIKE ?)',
                               (guild.id, key, key + ':page:%'))
                root = None
        heading = f'**{safe(name[:100])}**'
        if not root or not root['message_id']:
            initial = heading + '\nПодготавливаем страницы…' if len(contents) > 1 else contents[0]
            root = await self.upsert(guild, key, forum_id, initial, forum_name=name, view=view)

        def url(publication):
            return f'https://discord.com/channels/{guild.id}/{publication["channel_id"]}/{publication["message_id"]}'

        def navigation(index, publications):
            controls = discord.ui.View(timeout=None)
            if index > 0:
                controls.add_item(discord.ui.Button(label='← Предыдущая', url=url(publications[index - 1]), row=0))
            controls.add_item(discord.ui.Button(label='К началу', url=url(root), row=0))
            if index + 1 < len(publications):
                controls.add_item(discord.ui.Button(label='Следующая →', url=url(publications[index + 1]), row=0))
            return controls

        if inline_first:
            # The catalog opens on the current reading and next ten books.
            # Remaining sections stay in the same thread and keep stable IDs.
            sections = []
            for index, content in enumerate(contents[1:], 1):
                section = self.store.publication(f'{key}:page:{index}')
                if not section or not section['message_id']:
                    section = await self.upsert(guild, f'{key}:page:{index}', root['channel_id'], content)
                sections.append(section)
            changed_ids = False
            for index, content in enumerate(contents[1:]):
                section = await self.upsert(guild, f'{key}:page:{index + 1}', root['channel_id'], content,
                                           view=navigation(index, sections))
                changed_ids = changed_ids or section['message_id'] != sections[index]['message_id']
                sections[index] = section
            if changed_ids:
                for index, content in enumerate(contents[1:]):
                    await self.upsert(guild, f'{key}:page:{index + 1}', root['channel_id'], content,
                                      view=navigation(index, sections))
            first = contents[0]
            links = []
            for index, pub in enumerate(sections[:2]):
                heading = contents[index + 1].splitlines()[0]
                label = safe(heading.lstrip('# '))[:70] if heading.startswith('#') else f'Продолжение {index + 1}'
                links.append(f'[{label}]({url(pub)})')
            if links:
                first += '\n\n' + ' · '.join(links)
            root = await self.upsert(guild, key, forum_id, first, forum_name=name, view=view)
            for old in self.store.rows('SELECT * FROM bc_publications WHERE key LIKE ?', (key + ':page:%',)):
                if int(old['key'].rsplit(':', 1)[1]) > len(sections):
                    await self.upsert(guild, old['key'], root['channel_id'],
                        'Каталог обновлён. Текущая книга и очередь — в начале темы.', view=navigation(0, []))
            return root

        if len(contents) > 1:
            publications = []
            for index, content in enumerate(contents, 1):
                page_key = f'{key}:page:{index}'
                page = self.store.publication(page_key)
                if not page or not page['message_id']:
                    page = await self.upsert(guild, page_key, root['channel_id'], content)
                publications.append(page)
            # Forward links require every page ID. Existing pages skip the
            # provisional write above, so their navigation never toggles on ticks.
            changed_ids = False
            for index, content in enumerate(contents):
                page = await self.upsert(guild, f'{key}:page:{index + 1}', root['channel_id'], content,
                                         view=navigation(index, publications))
                changed_ids = changed_ids or page['message_id'] != publications[index]['message_id']
                publications[index] = page
            if changed_ids:
                # A missing page discovered during an edit got a new ID. Repair
                # its neighbours now instead of leaving links stale until a tick.
                for index, content in enumerate(contents):
                    await self.upsert(guild, f'{key}:page:{index + 1}', root['channel_id'], content,
                                      view=navigation(index, publications))
            content = (heading + f'\nСтраниц: {len(contents)}. На страницах есть кнопки перехода вперёд, назад и к началу.\n'
                       + f'[Первая страница]({url(publications[0])}) · [Последняя страница]({url(publications[-1])})')
            root = await self.upsert(guild, key, forum_id, content, forum_name=name, view=view)
        else:
            root = await self.upsert(guild, key, forum_id, contents[0], forum_name=name, view=view)
        old = self.store.rows("SELECT * FROM bc_publications WHERE key LIKE ?", (key + ':page:%',))
        for pub in old:
            index = int(pub['key'].rsplit(':', 1)[1])
            if index > len(contents) or (len(contents) == 1 and index == 1):
                await self.upsert(guild, pub['key'], root['channel_id'], 'Эта страница больше не нужна. Актуальная карточка — в начале темы.', view=navigation(0, []))
        return root

    async def refresh(self, guild):
        settings = self.store.settings(guild.id)
        if not settings['published']:
            return
        for b in self.store.books(guild.id):
            view = self.book_view_factory(b) if self.book_view_factory else None
            await self.paged_post(guild, f'book:{b["id"]}', settings['books'], f'{b["title"]} · {b["author"]}', book_pages(self.store, b, settings), view=view)
            root = self.store.publication(f'book:{b["id"]}')
            thread = await self.channel(guild, root['channel_id'], discord.Thread)
            await self.forum_tags.sync_thread_tags(guild, thread, [b['status']])
            for m in self.store.rows('SELECT * FROM bc_meetings WHERE book_id=? AND event_id IS NOT NULL', (b['id'],)):
                view = self.view_factory(m) if self.view_factory else None
                await self.upsert(guild, f'meeting:{m["id"]}', root['channel_id'], '\n'.join(meeting_lines(self.store, m, settings)), view=view)
        catalog_view = self.catalog_view_factory(guild.id) if self.catalog_view_factory else None
        catalog = await self.paged_post(guild, f'catalog:{guild.id}', settings['books'], 'Каталог книжного клуба',
                                        catalog_pages(self.store, guild.id), view=catalog_view, inline_first=True)
        thread = await self.channel(guild, catalog['channel_id'], discord.Thread)
        await self.forum_tags.sync_thread_tags(guild, thread, ['catalog'])
        if not thread.flags.pinned:
            # Never replace somebody else's forum pin implicitly.
            threads = await guild.active_threads()
            forum = await self.channel(guild, settings['books'], discord.ForumChannel)
            threads.extend([t async for t in forum.archived_threads(limit=None)])
            other_pin = any(t.parent_id == settings['books'] and t.id != thread.id and t.flags.pinned for t in threads)
            if other_pin:
                raise ClubError('В форуме уже закреплён другой пост. Организатор должен освободить закрепление каталога.')
            await thread.edit(pinned=True)
        await self.upsert(guild, f'news:{guild.id}', settings['news'], news_content(self.store, guild.id, settings))
        await self.upsert(guild, f'chat:{guild.id}', settings['chat'],
                          '**Площадь клуба**\nМесто для флуда, свободного общения и разговоров о книгах и обо всём остальном.\n'
                          f'Организационные объявления — в <#{settings["news"]}>; книги и их обсуждения — в <#{settings["books"]}>.')
        rules_view = self.format_view_factory(guild.id) if self.format_view_factory else None
        rules = await self.upsert(guild, f'format:{guild.id}', settings['news'],
                                  format_content(self.store, guild.id), view=rules_view)
        channel = await self.channel(guild, rules['channel_id'])
        message = await channel.fetch_message(rules['message_id'])
        if not message.pinned:
            await message.pin(reason='Общий формат книжного клуба')

    async def essay_access(self, guild, book_id, actor_id):
        member, _ = await self.actor(guild, actor_id)
        book = self.store.book(guild.id, book_id)
        forum = await self.forum_access(guild, member, 'essays')
        return member, book, forum

    def essay_starter(self, book, actor_id):
        url = book_url(self.store, book)
        return (f'**Эссе по книге «{safe(book["title"])}»**\nАвтор: <@{actor_id}>\n'
                + (f'[Карточка книги и другие эссе]({url})\n' if url else '')
                + 'Напишите эссе ниже обычным сообщением от своего имени. Можно отправить несколько '
                'сообщений, приложить файл или ссылку; свои сообщения можно редактировать.\n'
                'После вашего первого сообщения работа появится в списке эссе книги. '
                'Эта служебная шапка сама по себе не считается опубликованным эссе.')

    def valid_essay_webhook(self, hook, guild, channel_id):
        return (hook.guild_id == guild.id and hook.channel_id == channel_id
                and hook.type == discord.WebhookType.incoming
                and hook.user is not None and hook.user.id == self.bot.user.id)

    async def essay_webhook(self, guild, forum):
        """Persist only the ID; authenticate webhook lookups with the existing bot token."""
        if not forum.permissions_for(guild.me).manage_webhooks:
            raise ClubError('Для ника и аватарки автора боту нужно право Manage Webhooks в форуме эссе.')
        binding = self.store.webhook_binding(guild.id, forum.id)
        if binding and binding['webhook_id']:
            try:
                hook = await self.bot.fetch_webhook(binding['webhook_id'])
            except discord.NotFound:
                self.store.forget_webhook(guild.id, forum.id)
            else:
                if not self.valid_essay_webhook(hook, guild, forum.id) or not hook.token:
                    raise ClubError('Сохранённый вебхук эссе не принадлежит боту или этому форуму. Организатору: /club diagnose.')
                return hook
        hooks = [h for h in await forum.webhooks()
                 if h.name == ESSAY_WEBHOOK_NAME and self.valid_essay_webhook(h, guild, forum.id)]
        if len(hooks) > 1:
            raise ClubError('В форуме несколько вебхуков «Архивариус · эссе». Организатору нужно проверить интеграции.')
        if hooks:
            self.store.save_webhook(guild.id, forum.id, hooks[0].id)
            return hooks[0]
        if not self.store.reserve_webhook(guild.id, forum.id):
            raise ClubError('Предыдущее создание вебхука не подтверждено. Организатору: /club diagnose и /club repair.')
        hook = await forum.create_webhook(name=ESSAY_WEBHOOK_NAME, reason='Авторские шапки эссе книжного клуба')
        self.store.save_webhook(guild.id, forum.id, hook.id)
        return hook

    async def publish_essay_starter(self, guild, forum, key, name, content, member):
        forum = await self.forum_tags.fresh(guild, forum)
        marker = f'\n-# bc:{key}'
        pub = self.store.publication(key)
        imported = key.startswith('essay-import:')
        if pub and pub['message_id']:
            if pub['guild_id'] != guild.id:
                raise ClubError('Сохранённая шапка импорта относится к другому серверу.')
            thread = await self.channel(guild, pub['channel_id'], discord.Thread)
            if thread.parent_id != forum.id:
                raise ClubError('Сохранённая шапка импорта относится к другому форуму.')
            message = await thread.fetch_message(pub['message_id'])
        elif pub:
            if imported and (pub['guild_id'] != guild.id or pub['channel_id'] != forum.id):
                raise ClubError('Резервирование шапки импорта относится к другому форуму или серверу.')
            found = await self.delivery.recover(guild, forum, key, forum=True, webhook_id=pub['webhook_id'])
            if not found:
                raise ClubError('Предыдущая отправка не подтверждена. Организатору: /club diagnose и /club repair.')
            thread, message = found
        elif not self.store.settings(guild.id)['essay_webhooks']:
            pub = await self.upsert(guild, key, forum.id, content, forum_name=name)
            if not imported:
                return pub
            thread = await self.channel(guild, pub['channel_id'], discord.Thread)
            message = await thread.fetch_message(pub['message_id'])
        else:
            if not forum.permissions_for(guild.me).manage_threads:
                raise ClubError('Для работы с темами вебхука боту нужно право Manage Threads в форуме эссе.')
            tags = self.publication_tags(guild, forum, key)
            hook = await self.essay_webhook(guild, forum)
            fresh = await self.delivery.reserve(guild, forum, key, content, forum_name=name, webhook_id=hook.id)
            if not fresh:
                raise ClubError('Отправка шапки уже начата. Повторите проверку сохранённой отправки.')
            message = await webhook_send_once(hook, content, thread_name=name, username=member.display_name[:80],
                                      avatar_url=str(member.display_avatar.url), wait=True,
                                      applied_tags=tags, allowed_mentions=NO_MENTIONS)
            thread = await self.channel(guild, message.channel.id, discord.Thread)
        if imported and (message.id != thread.id or message.content not in (content, content + marker)
                         or not self.owns_starter(message, self.store.publication(key)['webhook_id'])):
            raise ClubError('Не удалось подтвердить содержимое и автора шапки импортированного эссе.')
        self.store.save_publication(key, thread.id, message.id)
        if imported:
            await self.finish_import_header(thread, message, content, self.store.publication(key))
        elif message.content.endswith(marker):
            await self.edit_essay_starter(thread, message, content, self.store.publication(key))
        return self.store.publication(key)

    async def finish_import_header(self, thread, starter, content, pub):
        """Clean historical markers in place; new headers are sent clean."""
        key = pub['key']
        marker = f'\n-# bc:{key}'
        if (not key.startswith(f'essay-import:{thread.guild.id}:')
                or pub['guild_id'] != thread.guild.id or pub['channel_id'] != thread.id
                or pub['message_id'] != starter.id or starter.id != thread.id
                or not self.owns_starter(starter, pub['webhook_id'])
                or starter.content not in (content, content + marker)):
            raise ClubError('Не удалось подтвердить содержимое и привязку шапки импортированного эссе.')
        # A crash or lost edit acknowledgement can now resume by ID. The marker
        # is needed only between the initial send and this committed binding.
        self.store.save_publication(key, thread.id, starter.id)
        if starter.content != content:
            await self.edit_essay_starter(thread, starter, content, pub)
        confirmed = await thread.fetch_message(starter.id)
        if (confirmed.content != content or not self.owns_starter(confirmed, pub['webhook_id'])):
            raise ClubError('Не удалось убрать служебную отметку из шапки эссе. Повторите исправление после проверки вебхука.')
        self.store.save_publication(key, thread.id, starter.id, hashlib.sha256(content.encode()).hexdigest())

    async def edit_essay_starter(self, thread, starter, content, pub):
        if pub['webhook_id'] is None:
            if thread.archived:
                thread = await thread.edit(archived=False)
            await starter.edit(content=content, allowed_mentions=NO_MENTIONS)
            return
        try:
            hook = await self.bot.fetch_webhook(pub['webhook_id'])
        except discord.NotFound:
            # A deleted webhook cannot edit old messages. Keep the original text and ID binding.
            log.warning('Essay webhook %s deleted; header %s remains unchanged', pub['webhook_id'], starter.id)
            return
        if not self.valid_essay_webhook(hook, thread.guild, thread.parent_id):
            raise ClubError('Вебхук шапки больше не принадлежит этому форуму и боту. Организатору: /club diagnose.')
        if thread.archived:
            thread = await thread.edit(archived=False)
        try:
            await hook.edit_message(starter.id, thread=thread, content=content, allowed_mentions=NO_MENTIONS)
        except discord.NotFound as exc:
            if exc.code != 10015:  # Unknown Webhook, distinct from a deleted message/thread.
                raise
            log.warning('Essay webhook %s deleted during edit; header %s remains unchanged', hook.id, starter.id)

    async def _own_essay_candidates(self, guild, book, member, forum):
        """Resolve durable author bindings using current Discord access, never a nickname."""
        candidates = []
        essays = sorted((essay for essay in self.store.essays(book['id'], submitted_only=False)
                         if essay['author_id'] == member.id),
                        key=lambda essay: (not essay['submitted'], essay['source_id']))
        for essay in essays:
            try:
                thread = await self.bot.fetch_channel(essay['channel_id'])
                if getattr(thread, 'guild', None) is None or thread.guild.id != guild.id:
                    raise ClubError('Сохранённое эссе относится к другому серверу. Организатору: /club diagnose.')
                if not isinstance(thread, discord.Thread) or thread.parent_id != forum.id:
                    if essay['managed']:
                        raise ClubError('Форум эссе изменён. Организатору нужно проверить существующую тему автора.')
                    continue
                if not self.can_read(thread, member):
                    raise ClubError('Нет доступа к существующему эссе или истории его темы. Обратитесь к организатору.')
                await thread.fetch_message(essay['source_id'])
            except discord.NotFound:
                self.store.delete_essay(guild.id, source_id=essay['source_id'])
                continue
            candidates.append((essay, thread))
        return candidates

    async def own_essays(self, guild, book_id, actor_id):
        """Open existing work without sending messages, unarchiving, or creating a draft."""
        member, book, forum = await self.essay_access(guild, book_id, actor_id)
        return [essay for essay, _ in await self._own_essay_candidates(guild, book, member, forum)]

    async def create_essay_space(self, guild, book_id, actor_id):
        """One recoverable workspace per book/author; the author writes their own messages."""
        async with self.locks[guild.id]:
            member, book, forum = await self.essay_access(guild, book_id, actor_id)
            if not self.store.settings(guild.id)['published']:
                raise ClubError('Организатору нужно сначала опубликовать клуб через /club publish.')
            if not forum.permissions_for(member).send_messages_in_threads:
                raise ClubError('Для своего эссе нужно право отправлять сообщения в постах форума.')
            if not all(getattr(forum.permissions_for(guild.me), p, False) for p in
                       ('view_channel', 'send_messages', 'send_messages_in_threads', 'read_message_history')):
                raise ClubError('Боту не хватает прав в форуме эссе. Организатору: /club diagnose.')
            # Imported and manually registered works have managed=0. They still
            # belong to this author and take precedence over an accidental draft.
            existing = await self._own_essay_candidates(guild, book, member, forum)
            for essay, thread in existing:
                if essay['source_id'] == thread.id:
                    if not await self.register_thread(thread, prompt=False):
                        raise ClubError('Не удалось подтвердить пост автора. Организатору: /club diagnose.')
                    essay = self.store.one('SELECT * FROM bc_essays WHERE guild_id=? AND source_id=?',
                                           (guild.id, thread.id))
                    if essay['book_id'] != book_id or essay['author_id'] != actor_id:
                        raise ClubError('Связь эссе изменилась. Обновите карточку книги.')
                if thread.archived:
                    await thread.edit(archived=False)
                return essay
            key = f'essay-space:{book_id}:{actor_id}'
            pub = self.store.publication(key)
            existing = [e for e in self.store.essays(book_id, submitted_only=False)
                        if e['managed'] and e['author_id'] == actor_id]
            candidates = {}
            for essay in existing:
                saved = self.store.one("SELECT * FROM bc_publications WHERE guild_id=? AND channel_id=? AND key LIKE 'essay-space:%'",
                                       (guild.id, essay['channel_id']))
                if saved:
                    candidates[saved['key']] = saved
            if pub:
                candidates[pub['key']] = pub
            for pub in candidates.values():
                if not pub['message_id']:
                    continue
                try:
                    thread = await self.channel(guild, pub['channel_id'], discord.Thread)
                    if thread.parent_id != forum.id:
                        raise ClubError('Форум эссе изменён. Организатору нужно проверить существующую тему автора.')
                    await thread.fetch_message(pub['message_id'])
                    if thread.archived:
                        thread = await thread.edit(archived=False)
                    if not await self.register_thread(thread, prompt=False):
                        raise ClubError('Не удалось подтвердить пост автора. Организатору: /club diagnose.')
                    return self.store.one('SELECT * FROM bc_essays WHERE guild_id=? AND source_id=?', (guild.id, thread.id))
                except discord.NotFound:
                    self.store.delete_essay(guild.id, channel_id=pub['channel_id'])
                    self.store.forget_publication(pub['key'])
            forum = await self.forum_tags.fresh(guild, forum)
            if not forum.permissions_for(member).send_messages:
                raise ClubError('Для нового эссе нужно право создавать публикации в форуме эссе.')
            self.forum_tags.creation_tags(guild.id, forum, 'draft')
            author_name = ' '.join(member.display_name.split())[:40]
            book_name = ' '.join(book['title'].split())
            name = f'{book_name[:100 - len(author_name) - 10]} · Эссе · {author_name}'
            content = self.essay_starter(book, actor_id)
            pub = await self.publish_essay_starter(guild, forum, key, name, content, member)
            thread = await self.channel(guild, pub['channel_id'], discord.Thread)
            await self.register_thread(thread, prompt=False)
            return self.store.one('SELECT * FROM bc_essays WHERE guild_id=? AND source_id=?', (guild.id, thread.id))

    async def register_thread(self, thread, *, book_id=None, correct=False, prompt=True):
        settings = self.store.settings(thread.guild.id)
        if thread.parent_id != settings['essays']:
            return False
        old = self.store.one('SELECT * FROM bc_essays WHERE guild_id=? AND source_id=?', (thread.guild.id, thread.id))
        try:
            starter = await thread.fetch_message(thread.id)
        except discord.NotFound:
            if old:
                self.store.delete_essay(thread.guild.id, source_id=thread.id)
            return False
        imported = self.store.rows(
            'SELECT DISTINCT item_key FROM bc_import_sources WHERE guild_id=? AND thread_id=?',
            (thread.guild.id, thread.id))
        if imported:
            # A completed copy retains the real author recorded at import time.
            # Neither a displayed webhook name nor an unbound marker proves it.
            if not old or len(imported) != 1:
                return False
            key = imported[0]['item_key']
            pub = self.store.publication(key)
            if (not key.startswith(f'essay-import:{thread.guild.id}:') or not pub
                    or pub['guild_id'] != thread.guild.id or pub['channel_id'] != thread.id
                    or pub['message_id'] != thread.id
                    or not self.owns_starter(starter, pub['webhook_id'])):
                return False
            marker = '\n-# bc:' + key
            if any(line.startswith('-# bc:') for line in starter.content.splitlines()):
                if not starter.content.endswith(marker) or any(
                        line.startswith('-# bc:') for line in starter.content[:-len(marker)].splitlines()):
                    return False
            # content_hash is a projection cache cleared on restart/publish.
            # Provenance rests on the source ledger, exact IDs and real sender;
            # a clean header must not require a visible marker or cached hash.
            target_book = self.store.book(thread.guild.id, book_id if correct and book_id else old['book_id'])
            if correct and target_book['id'] != old['book_id']:
                # Keep original attribution while correcting the heading. A
                # completed import remains authenticated without a public marker.
                _, separator, remainder = starter.content.partition('\n')
                content = f'**Архивное эссе по книге «{safe(target_book["title"])}»**'
                content += separator + remainder
                if content.endswith(marker):
                    content = content[:-len(marker)]
                try:
                    await self.edit_essay_starter(thread, starter, content, pub)
                except Exception:
                    confirmed = await thread.fetch_message(starter.id)
                    if confirmed.content != content or not self.owns_starter(confirmed, pub['webhook_id']):
                        raise
                confirmed = await thread.fetch_message(starter.id)
                await self.finish_import_header(thread, confirmed, content, pub)
            self.store.register_essay(thread.guild.id, target_book['id'], thread.id, thread.id,
                                      old['author_id'], thread.name, thread.jump_url, correct=correct,
                                      managed=old['managed'], submitted=old['submitted'])
            await self.forum_tags.sync_thread_tags(thread.guild, thread, ['essay', 'imported'])
            return True
        managed_pub = self.store.one("SELECT * FROM bc_publications WHERE guild_id=? AND channel_id=? AND key LIKE 'essay-space:%'",
                                     (thread.guild.id, thread.id))
        if managed_pub is None:
            keys = [line.removeprefix('-# bc:') for line in starter.content.splitlines()
                    if line.startswith('-# bc:essay-space:')]
            if len(keys) == 1:
                managed_pub = self.store.publication(keys[0])
        if starter.webhook_id is not None or starter.author.bot or (old and old['managed']) or managed_pub:
            # Authenticate the actual sender against the reservation, never the displayed nickname.
            if (not managed_pub or not self.owns_starter(starter, managed_pub['webhook_id'])
                    or managed_pub['guild_id'] != thread.guild.id
                    or managed_pub['channel_id'] not in (thread.id, thread.parent_id)
                    or managed_pub['message_id'] not in (None, thread.id)):
                return False
            if old and old['managed']:
                author_id = old['author_id']
                book_id = book_id if correct else old['book_id']
            else:
                key = managed_pub['key']
                parts = key.split(':')
                bound = managed_pub['channel_id'] == thread.id and managed_pub['message_id'] == starter.id
                if (len(parts) not in (3, 4) or not parts[2].isdecimal()
                        or (not bound and '-# bc:' + key not in starter.content.splitlines())):
                    return False
                book_id, author_id = parts[1], int(parts[2])
                self.store.book(thread.guild.id, book_id)
                self.store.save_publication(key, thread.id, starter.id)
            submitted = False
            async for message in thread.history(limit=None):
                if message.author.id == author_id and not message.author.bot and (message.content.strip() or message.attachments):
                    submitted = True
                    break
            self.store.register_essay(thread.guild.id, book_id, thread.id, thread.id, author_id,
                                      thread.name, thread.jump_url, correct=correct, managed=True, submitted=submitted)
            pub = self.store.one("SELECT * FROM bc_publications WHERE guild_id=? AND channel_id=? AND key LIKE 'essay-space:%'",
                                 (thread.guild.id, thread.id))
            if pub:
                key = pub['key']
                if key.split(':')[1] != book_id:
                    key = f'essay-space:{book_id}:{author_id}:{thread.id}'
                    with self.store.tx() as db:
                        db.execute('UPDATE bc_publications SET key=?,content_hash=NULL WHERE key=?', (key, pub['key']))
                content = self.essay_starter(self.store.book(thread.guild.id, book_id), author_id)
                if content != starter.content:
                    await self.edit_essay_starter(thread, starter, content, pub)
            await self.forum_tags.sync_thread_tags(thread.guild, thread, ['essay' if submitted else 'draft'])
            return True
        if old and not correct:
            # The confirmed ID binding always wins over a renamed title.
            book_id = old['book_id']
        if book_id is None:
            matches = self.store.match_essay_title(thread.guild.id, thread.name)
            if len(matches) != 1:
                if settings['published'] and prompt:
                    await self.upsert(thread.guild, f'essay-choice:{thread.id}', thread.id,
                                      'Не удалось однозначно определить книгу. Автор или организатор: /club essay, выберите книгу и укажите ссылку на этот пост.')
                return False
            book_id = matches[0]['id']
        if thread.owner_id == self.bot.user.id or thread.owner_id is None:
            return False
        # Historical essays retain their author even after that person leaves.
        self.store.register_essay(thread.guild.id, book_id, thread.id, thread.id, thread.owner_id, thread.name, thread.jump_url, correct=correct)
        await self.forum_tags.sync_thread_tags(thread.guild, thread, ['essay'])
        return True

    async def scan_essays(self, guild):
        settings = self.store.settings(guild.id)
        forum = await self.channel(guild, settings['essays'], discord.ForumChannel)
        for pub in self.store.rows("SELECT * FROM bc_publications WHERE guild_id=? AND channel_id=? "
                                   "AND state='reserved' AND key LIKE 'essay-space:%'", (guild.id, forum.id)):
            found = await self.delivery.recover(guild, forum, pub['key'], forum=True, webhook_id=pub['webhook_id'])
            if found:
                thread, message = found
                self.store.save_publication(pub['key'], thread.id, message.id)
        threads = [t for t in await guild.active_threads() if t.parent_id == forum.id]
        # Archived posts are sorted by archive time. Stop after the enable boundary.
        async for thread in forum.archived_threads(limit=None):
            if thread.archive_timestamp and thread.archive_timestamp.timestamp() < settings['scan_after']:
                break
            threads.append(thread)
        for thread in {t.id: t for t in threads}.values():
            if thread.created_at.timestamp() >= settings['scan_after']:
                await self.register_thread(thread, prompt=False)
        self.last_essay_scan[guild.id] = self.store.clock()

    async def check_essays(self, guild):
        for e in self.store.rows('SELECT * FROM bc_essays WHERE guild_id=? AND deleted=0', (guild.id,)):
            try:
                channel = await self.channel(guild, e['channel_id'])
                if isinstance(channel, discord.Thread) and e['source_id'] == channel.id:
                    await self.register_thread(channel)
                else:
                    await channel.fetch_message(e['source_id'])
            except discord.NotFound:
                self.store.delete_essay(guild.id, e['source_id'])

    async def organizers(self, guild):
        settings = self.store.settings(guild.id)
        result = {guild.owner_id, *settings['organizers']}
        if settings['organizer_roles']:
            async for member in guild.fetch_members(limit=None):
                if not member.bot and {r.id for r in member.roles} & set(settings['organizer_roles']):
                    result.add(member.id)
        return result

    async def deliver(self, guild, job):
        settings = self.store.settings(guild.id)
        kind = job['kind']
        if kind.startswith('essay'):
            book = self.store.book(guild.id, job['entity_id'])
            recipients = {r['user_id'] for r in self.store.missing_essays(book['id'])}
            text = f'Эссе по книге «{safe(book["title"])}»: ' + ('срок наступил.' if kind == 'essay_due' else 'приближается срок.')
            url = book_url(self.store, book)
            text += ' Нажмите «Добавить своё эссе» в карточке книги и напишите своё сообщение в созданном посте.'
            text += f'\n{url}' if url else f' Форум эссе: <#{settings["essays"]}>.'
        else:
            meeting = self.store.meeting(guild.id, job['entity_id'])
            labels = dict(participants='Скоро встреча книжного клуба.', prepare='Напоминание ведущему: подготовьте план встречи.',
                          escalate='План пока не отмечен готовым. Ведущему и организатору: проверьте подготовку.',
                          offer='Вам предложили провести встречу. Примите предложение или откажитесь в карточке.',
                          rescheduled='Время или место встречи изменилось. Подтвердите новые условия или откажитесь в карточке.')
            text = labels[kind] + '\n' + '\n'.join(meeting_lines(self.store, meeting, settings))
            card = self.store.publication(f'meeting:{meeting["id"]}')
            if card and card['message_id']:
                text += f'\nhttps://discord.com/channels/{guild.id}/{card["channel_id"]}/{card["message_id"]}'
            recipients = {job['target']}
            if kind == 'participants':
                recipients = {p['user_id'] for p in self.store.meeting_participants(meeting['id'])}
            elif kind == 'escalate':
                recipients |= await self.organizers(guild)
        failures = []
        books_forum = await self.fresh_forum(guild) if recipients else None
        essays_forum = await self.fresh_forum(guild, 'essays') if recipients and kind.startswith('essay') else None
        for user_id in sorted(recipients):
            try:
                member = await guild.fetch_member(user_id)
                if member.bot or not self.can_read(books_forum, member):
                    continue
                if essays_forum is not None and not self.can_read(essays_forum, member):
                    continue
                if kind == 'participants' and settings['reminder_role'] and settings['reminder_role'] not in {r.id for r in member.roles}:
                    continue
                await member.send(text[:2000], allowed_mentions=NO_MENTIONS)
            except discord.NotFound:
                continue
            except discord.Forbidden:
                failures.append(f'{user_id}: личные сообщения закрыты')
            except discord.HTTPException as exc:
                failures.append(f'{user_id}: Discord HTTP {exc.status}; доставка не подтверждена')
        self.store.finish_job(job['key'], 'failed' if failures else 'sent', '; '.join(failures) or None)

    async def tick(self, guild):
        async with self.locks[guild.id]:
            await self.reconcile(guild)
            live = await self.live_participants(guild)
            self.store.expire_offers(guild.id, live)
            if not self.store.settings(guild.id)['published']:
                return
            due = self.store.due_jobs(guild.id)
            essays_ready = True
            try:
                if (self.store.clock() - self.last_essay_scan.get(guild.id, 0) >= 300
                        or any(j['kind'].startswith('essay') for j in due)):
                    await self.scan_essays(guild)
                await self.check_essays(guild)
            except Exception:
                essays_ready = False
                log.exception('Essay reconciliation failed for guild %s; essay reminders deferred', guild.id)
            try:
                await self.refresh(guild)
            except Exception:
                log.exception('Book club cards failed for guild %s; meeting reminders remain enabled', guild.id)
            for job in self.store.due_jobs(guild.id):
                if job['kind'].startswith('essay'):
                    if not essays_ready:
                        continue
                else:
                    try:
                        await self.sync_one(guild, self.store.meeting(guild.id, job['entity_id']))
                    except Exception:
                        log.exception('Meeting %s could not be checked; its reminder deferred', job['entity_id'])
                        continue
                if self.store.claim_job(job['key']):
                    try:
                        await self.deliver(guild, job)
                    except Exception:
                        self.store.finish_job(job['key'], 'unknown', 'Не удалось подтвердить отправку; проверьте журнал бота.')
                        log.exception('Book club delivery %s failed', job['key'])

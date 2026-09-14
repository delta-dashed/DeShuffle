"""Explicit, restartable server setup. Never runs from the background worker."""
from __future__ import annotations

import logging

import discord

from .store import ClubError, DEFAULTS


log = logging.getLogger(__name__)
SLOTS = {
    'category': ('Книжный клуб', discord.CategoryChannel),
    'news': ('вестник', discord.TextChannel),
    'chat': ('площадь', discord.TextChannel),
    'books': ('город', discord.ForumChannel),
    'essays': ('либрариум', discord.ForumChannel),
    'voice': ('Ротонда', discord.VoiceChannel),
}
REASON = 'Настройка книжного клуба через /club setup'


def required_permissions(purpose, settings):
    result = ['view_channel']
    if purpose == 'category':
        return result + ['read_message_history']
    if purpose == 'voice':
        return result + ['connect', 'create_events', 'manage_events']
    result += ['send_messages', 'read_message_history', 'send_messages_in_threads']
    if purpose in ('books', 'essays'):
        result += ['manage_threads']
    if purpose == 'essays' and settings['essay_webhooks']:
        result += ['manage_webhooks']
    return result


class Provisioner:
    def __init__(self, service):
        self.service, self.store = service, service.store

    @staticmethod
    def temporary_name(resource):
        return 'bc-setup-' + resource['token']

    @staticmethod
    def marker(resource):
        return f'bookclub:{resource["guild_id"]}:{resource["purpose"]}:{resource["token"]}'

    async def check_publications(self, guild, *, repair=False):
        result = []
        for publication in self.store.rows("SELECT * FROM bc_publications WHERE guild_id=? AND state='ready'", (guild.id,)):
            if not publication['key'].startswith(('book:', 'catalog:', 'meeting:', 'news:')):
                continue
            try:
                destination = await self.service.channel(guild, publication['channel_id'])
                await destination.fetch_message(publication['message_id'])
            except discord.NotFound:
                result.append(f'Карточка {publication["key"]} удалена; требуется восстановление.')
                if repair:
                    self.store.forget_publication(publication['key'])
            except (discord.HTTPException, ClubError):
                result.append(f'Карточка {publication["key"]} недоступна для проверки; привязка сохранена.')
        return result

    async def resolve(self, guild, channels, purpose, settings):
        """A successful channel list is useful for recovery, but absence is not a 404."""
        resource = self.store.setup_resource(guild.id, purpose)
        configured_id = settings.get(purpose) if purpose != 'category' else None
        channel_id = resource['channel_id'] if resource else None
        if configured_id and (not resource or resource['state'] == 'ready'):
            channel_id = configured_id
        if channel_id:
            channel = next((c for c in channels if c.id == channel_id), None)
            if channel is None:
                try:
                    channel = await self.service.bot.fetch_channel(channel_id)
                except discord.NotFound:
                    return resource, None, 'deleted'
            if getattr(channel, 'guild', None) is None or channel.guild.id != guild.id:
                raise ClubError(f'{purpose}: сохранённый канал принадлежит другому серверу.')
            if not isinstance(channel, SLOTS[purpose][1]):
                raise ClubError(f'{purpose}: у сохранённого канала неверный тип. Исправьте привязку; замена не создана.')
            return resource, channel, 'existing'
        if resource:
            matches = [c for c in channels if c.name == self.temporary_name(resource)
                       or self.marker(resource) in (getattr(c, 'topic', None) or '').splitlines()]
            if len(matches) > 1:
                raise ClubError(f'{purpose}: найдено несколько каналов незавершённой настройки. Нужна проверка организатора.')
            if matches:
                if not isinstance(matches[0], SLOTS[purpose][1]):
                    raise ClubError(f'{purpose}: у канала незавершённой настройки неверный тип.')
                return resource, matches[0], 'recovered'
            return resource, None, 'uncertain'
        return None, None, 'missing'

    def overwrites(self, guild, bot_member, purpose, settings, category=None):
        # Nonempty overwrites must explicitly preserve the category's ACL.
        result = {target: discord.PermissionOverwrite.from_pair(*overwrite.pair())
                  for target, overwrite in category.overwrites.items()} if category is not None else {}
        everyone = result.get(guild.default_role, discord.PermissionOverwrite())
        if purpose == 'category':
            everyone.update(view_channel=True, read_message_history=True)
        elif purpose == 'voice':
            everyone.update(connect=True)
        else:
            everyone.update(send_messages=purpose != 'news', send_messages_in_threads=purpose != 'news')
        result[guild.default_role] = everyone
        own = result.get(bot_member, discord.PermissionOverwrite())
        own.update(**dict.fromkeys(required_permissions(purpose, settings), True))
        result[bot_member] = own
        if purpose == 'news':
            for role_id in settings['organizer_roles']:
                role = guild.get_role(role_id)
                if role is not None:
                    overwrite = result.get(role, discord.PermissionOverwrite())
                    overwrite.update(send_messages=True)
                    result[role] = overwrite
            for user_id in settings['organizers']:
                member = guild.get_member(user_id)
                if member is not None:
                    overwrite = result.get(member, discord.PermissionOverwrite())
                    overwrite.update(send_messages=True)
                    result[member] = overwrite
        return result

    async def create(self, guild, bot_member, purpose, settings, category, resource):
        kwargs = dict(name=self.temporary_name(resource),
                      overwrites=self.overwrites(guild, bot_member, purpose, settings, category), reason=REASON)
        if purpose != 'category':
            kwargs['category'] = category
        if purpose in ('news', 'chat', 'books', 'essays'):
            descriptions = dict(news='Текущая книга и ближайшая встреча.', chat='Общение и предложения книжного клуба.',
                                books='Каталог книг, встречи и обсуждения.', essays='Эссе участников и обсуждение работ.')
            kwargs['topic'] = descriptions[purpose] + '\n' + self.marker(resource)
        if purpose == 'category':
            return await guild.create_category(**kwargs)
        if purpose == 'voice':
            return await guild.create_voice_channel(**kwargs)
        if purpose in ('books', 'essays'):
            kwargs['default_layout'] = discord.ForumLayoutType.list_view
            return await guild.create_forum(**kwargs)
        return await guild.create_text_channel(**kwargs)

    async def run(self, guild, actor_id, *, check_only=False, retry_missing=False, category=None):
        async with self.service.locks[guild.id]:
            actor = await self.service.setup_actor(guild, actor_id)
            bot_member = await guild.fetch_member(self.service.bot.user.id)
            saved = self.store.one('SELECT 1 FROM bc_settings WHERE guild_id=?', (guild.id,))
            settings = self.store.settings(guild.id) if saved else {**DEFAULTS, 'organizers': [actor.id]}
            report = ['**Проверка книжного клуба**' if check_only else '**Настройка книжного клуба**']
            try:
                channels = await guild.fetch_channels()
                plan, errors = {}, []
                for purpose in SLOTS:
                    try:
                        if purpose == 'category' and category is not None:
                            selected = next((c for c in channels if c.id == category.id), None)
                            if selected is None:
                                selected = await self.service.bot.fetch_channel(category.id)
                            if not isinstance(selected, discord.CategoryChannel) or selected.guild.id != guild.id:
                                raise ClubError('Выберите категорию на этом сервере.')
                            resource = self.store.setup_resource(guild.id, purpose)
                            if resource and resource['channel_id'] != selected.id:
                                resource = None
                            plan[purpose] = resource, selected, 'existing'
                            continue
                        plan[purpose] = await self.resolve(guild, channels, purpose, settings)
                    except (ClubError, discord.HTTPException) as exc:
                        errors.append(str(exc) if isinstance(exc, ClubError) else
                                      f'{purpose}: канал не удалось проверить (Discord HTTP {exc.status}); замена не создана.')
                category_resource = self.store.setup_resource(guild.id, 'category')
                old_category_deleted = False
                if category_resource and category_resource['channel_id'] and not any(c.id == category_resource['channel_id'] for c in channels):
                    try:
                        await self.service.bot.fetch_channel(category_resource['channel_id'])
                    except discord.NotFound:
                        old_category_deleted = True
                    except discord.HTTPException:
                        errors.append('Не удалось проверить прежнюю категорию; её каналы не перемещены.')
                category_plan = plan.get('category')
                if category_plan and category_plan[1] is None and (saved or category_plan[2] == 'deleted'):
                    children = [item[1] for key, item in plan.items() if key != 'category' and item[1] is not None]
                    parent_ids = {child.category_id for child in children}
                    # Adopt an existing common parent by ID, never by its friendly name.
                    if len(parent_ids) == 1 and None not in parent_ids:
                        try:
                            parent_id = parent_ids.pop()
                            parent = next((c for c in channels if c.id == parent_id), None)
                            if parent is None:
                                parent = await self.service.bot.fetch_channel(parent_id)
                            if not isinstance(parent, discord.CategoryChannel) or parent.guild.id != guild.id:
                                raise ClubError('Неверная общая категория.')
                            plan['category'] = None, parent, 'existing'
                        except (ClubError, discord.HTTPException):
                            errors.append('Не удалось проверить общую категорию существующих каналов.')
                    elif category_resource is None and len(children) == 5:
                        # A complete manually configured club need not use a category at all.
                        del plan['category']
                    else:
                        errors.append('Не удалось определить права прежней категории. Выберите существующую категорию '
                                      'с нужными доступами: /club setup category:категория. Каналы не созданы.')
                needs_forums = any(plan.get(p, (None, None, None))[1] is None for p in ('books', 'essays'))
                if needs_forums and 'COMMUNITY' not in guild.features:
                    errors.append('Для создания форумов сначала включите Community в настройках сервера. Каналы не созданы.')
                for purpose, (resource, channel, status) in plan.items():
                    if channel is None:
                        report.append(f'{SLOTS[purpose][0]}: ' + ('создание не подтверждено' if status == 'uncertain' else 'нужно создать'))
                        if status == 'uncertain' and not retry_missing:
                            errors.append(f'{purpose}: предыдущий запрос мог создать канал. Проверьте структуру сервера; '
                                          'если канала точно нет, повторите /club setup retry_missing:true.')
                        needed = ['manage_channels', *required_permissions(purpose, settings)]
                    else:
                        missing = [p for p in required_permissions(purpose, settings)
                                   if not getattr(channel.permissions_for(bot_member), p, False)]
                        report.append(f'{SLOTS[purpose][0]}: <#{channel.id}> · ' +
                                      ('не хватает прав бота: ' + ', '.join(missing) if missing else 'OK'))
                        needed = ['manage_roles', *missing] if missing else []
                        if resource and resource['state'] != 'ready' and channel.name == self.temporary_name(resource):
                            needed.append('manage_channels')
                        if purpose != 'category' and old_category_deleted and resource and resource['managed'] and channel.category_id is None:
                            needed.append('manage_channels')
                    absent = [p for p in needed if not getattr(bot_member.guild_permissions, p, False)]
                    if absent:
                        errors.append(f'{purpose}: выдайте боту права на сервере: {", ".join(sorted(set(absent)))}.')
                if check_only or errors:
                    report.extend(errors)
                    if check_only and saved:
                        report.extend(await self.service.diagnose(guild, channels=channels, bot_member=bot_member))
                        report.extend(await self.check_publications(guild))
                    if not errors:
                        report.append('Проверка завершена. Изменений нет.' if check_only else 'Структура проверена.')
                    return report

                category, resolved = None, {}
                for purpose, (resource, channel, status) in plan.items():
                    if channel is None:
                        if resource:
                            self.store.forget_setup_resource(guild.id, purpose)
                        self.store.reserve_setup_resource(guild.id, purpose)
                        resource = self.store.setup_resource(guild.id, purpose)
                        try:
                            channel = await self.create(guild, bot_member, purpose, settings, category, resource)
                        except discord.HTTPException as exc:
                            if exc.status in (400, 401, 403):
                                self.store.forget_setup_resource(guild.id, purpose)
                            raise
                        # Commit identity before cosmetic updates; a lost rename must never create another channel.
                        self.store.bind_setup_resource(guild.id, purpose, channel.id, managed=True, state='created')
                        resource = self.store.setup_resource(guild.id, purpose)
                        report.append(f'Создан: <#{channel.id}>.')
                    elif status == 'recovered':
                        self.store.bind_setup_resource(guild.id, purpose, channel.id, managed=True, state='created')
                        resource = self.store.setup_resource(guild.id, purpose)
                        report.append(f'Восстановлена связь: <#{channel.id}>.')
                    elif not resource or resource['channel_id'] != channel.id:
                        self.store.bind_setup_resource(guild.id, purpose, channel.id, managed=False, state='created')
                        resource = self.store.setup_resource(guild.id, purpose)
                    if resource['managed'] and resource['state'] != 'ready' and channel.name == self.temporary_name(resource):
                        channel = await channel.edit(name=SLOTS[purpose][0], reason=REASON)
                    missing = [p for p in required_permissions(purpose, settings)
                               if not getattr(channel.permissions_for(bot_member), p, False)]
                    if missing:
                        own = channel.overwrites_for(bot_member)
                        own.update(**dict.fromkeys(missing, True))
                        await channel.set_permissions(bot_member, overwrite=own, reason=REASON)
                        report.append(f'<#{channel.id}>: восстановлены права бота.')
                    if purpose == 'category':
                        category = channel
                    else:
                        if old_category_deleted and resource['managed'] and channel.category_id is None and category is not None:
                            channel = await channel.edit(category=category, sync_permissions=False, reason=REASON)
                            report.append(f'<#{channel.id}>: возвращён в категорию с сохранением доступов.')
                        resolved[purpose] = channel.id
                    if purpose in ('books', 'essays') and getattr(channel.flags, 'require_tag', False):
                        report.append(f'<#{channel.id}>: обязательный тег мешает публикации; измените эту настройку форума вручную.')

                # Keep the initiating administrator able to operate the new club; existing organizers remain unchanged.
                self.store.configure(guild.id, {**settings, **resolved})
                for purpose in plan:
                    self.store.ready_setup_resource(guild.id, purpose)
                # set_permissions is acknowledged before the Gateway cache is updated.
                diagnostics = await self.service.diagnose(guild, channels=await guild.fetch_channels(), bot_member=bot_member)
                failures = [x for x in diagnostics if 'не хватает' in x or 'недоступен' in x or 'Community' in x or 'не найдена' in x]
                if failures:
                    return report + failures + ['Каналы и настройки сохранены. После исправлений повторите /club setup.']
                if settings['essay_webhooks']:
                    forum = await self.service.channel(guild, resolved['essays'], discord.ForumChannel)
                    await self.service.essay_webhook(guild, forum)
                self.store.set_published(guild.id)
                report.extend(await self.check_publications(guild, repair=True))
                await self.service.refresh(guild)
                report.append('Готово: структура, каталог и Вестник настроены. Повторный /club setup проверит их и восстановит недостающее.')
                return report
            except (discord.HTTPException, ClubError, OSError) as exc:
                log.warning('Book club setup interrupted for guild %s: %s', guild.id, type(exc).__name__)
                if isinstance(exc, ClubError):
                    report.append(str(exc))
                elif isinstance(exc, discord.HTTPException):
                    report.append(f'Discord HTTP {exc.status}: настройка прервана. Проверьте права и лимиты сервера.')
                else:
                    report.append('Ответ Discord не получен: часть настройки могла сохраниться.')
                report.append('Сохранённые каналы не удаляются. Повторите /club setup для продолжения без дубликатов.')
                return report

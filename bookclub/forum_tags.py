"""Forum labels are identified by Discord IDs; their visible names are editable.

Only explicit setup creates tags. Normal publication updates the club's status
labels while preserving labels people added themselves.
"""
from __future__ import annotations

import discord

from .store import ClubError


TEMPLATES = {
    'books': {'catalog': 'Каталог', 'proposed': 'Предложено', 'queued': 'В очереди',
              'reading': 'Читаем', 'read': 'Прочитано'},
    'essays': {'draft': 'Черновик', 'essay': 'Эссе', 'imported': 'Архив'},
}
STATUS_PURPOSES = frozenset({'catalog', 'proposed', 'queued', 'reading', 'read', 'draft', 'essay'})
ALL_PURPOSES = STATUS_PURPOSES | {'imported'}
REASON = 'Теги книжного клуба'


class ForumTags:
    def __init__(self, service):
        self.service, self.store = service, service.store
        self._updated = {}

    def current(self, forum):
        """Return an edit's response until the caller has a newer channel object.

        discord.py channel.edit returns a new object before its gateway cache
        necessarily receives that update. Never replace a newly fetched object.
        """
        while id(forum) in self._updated:
            previous, updated = self._updated[id(forum)]
            if forum is not previous or updated is forum:
                break
            forum = updated
        return forum

    def bindings(self, guild_id, forum):
        return {row['purpose']: row['tag_id'] for row in self.store.bindings(guild_id, forum.id)}

    async def fresh(self, guild, forum, *, tag_ids=()):
        """Refetch when a gateway cache cannot yet resolve our persisted IDs."""
        forum = self.current(forum)
        if not isinstance(forum, discord.ForumChannel) or forum.guild.id != guild.id:
            raise ClubError('Для тегов нужен форум этого сервера.')
        bindings = self.bindings(guild.id, forum)
        required_ids = set(bindings.values()) | set(tag_ids)
        if required_ids and not required_ids.issubset({tag.id for tag in forum.available_tags}):
            forum = await self.service.bot.fetch_channel(forum.id)
            if not isinstance(forum, discord.ForumChannel) or forum.guild.id != guild.id:
                raise ClubError('Discord вернул другой сервер или неверный тип форума тегов.')
        return forum

    def get(self, guild_id, forum, purpose):
        forum = self.current(forum)
        if forum.guild.id != guild_id:
            raise ClubError('Форум тегов принадлежит другому серверу.')
        ident = self.bindings(guild_id, forum).get(purpose)
        if ident is None:
            return None
        return next((tag for tag in forum.available_tags if tag.id == ident), None)

    async def ensure(self, guild, forum, purpose, *, check_only=False):
        """Create/adopt missing defaults with one edit, retaining every other tag.

        Pass a current forum from fetch_channels/fetch_channel during setup.
        A lost HTTP acknowledgement is recovered by a subsequent explicit setup:
        its fresh tag list is matched by exact default name before any creation.
        """
        if purpose not in TEMPLATES:
            raise ClubError('Теги настраиваются только для форумов books и essays.')
        forum = self.current(forum)
        if not isinstance(forum, discord.ForumChannel) or forum.guild.id != guild.id:
            raise ClubError('Для тегов нужен форум этого сервера.')
        available = list(forum.available_tags)
        by_id = {tag.id: tag for tag in available}
        bindings = self.bindings(guild.id, forum)
        selected, missing = {}, []
        used = set()
        for kind, name in TEMPLATES[purpose].items():
            bound = by_id.get(bindings.get(kind))
            if bound is not None:
                if bound.id in used:
                    raise ClubError(f'Теги форума {forum.id}: один ID привязан к нескольким назначениям.')
                selected[kind], used = bound, used | {bound.id}
        for kind, name in TEMPLATES[purpose].items():
            if kind in selected:
                continue
            matches = [tag for tag in available if tag.name == name]
            if len(matches) > 1 or (matches and matches[0].id in used):
                raise ClubError(f'Теги форума {forum.id}: название «{name}» неоднозначно; '
                                'переименуйте лишний тег и повторите /club setup.')
            if matches:
                selected[kind] = matches[0]
                used.add(matches[0].id)
            else:
                missing.append(kind)
        if len(available) + len(missing) > 20:
            raise ClubError(f'В форуме {forum.id} не хватает места для {len(missing)} тегов клуба '
                            '(лимит Discord — 20). Освободите место и повторите /club setup.')
        if check_only:
            if missing:
                result = [f'Теги форума {forum.id}: будут добавлены '
                          + ', '.join(TEMPLATES[purpose][kind] for kind in missing) + '.']
                if guild.me is not None and not forum.permissions_for(guild.me).manage_channels:
                    result.append(f'Для создания тегов форума {forum.id} боту не хватает права Manage Channels.')
                return result
            return [f'Теги форума {forum.id}: все найдены; названия сохраняются.']
        if missing:
            if guild.me is not None and not forum.permissions_for(guild.me).manage_channels:
                raise ClubError(f'Для создания тегов форума {forum.id} боту нужно право Manage Channels.')
            updated = await forum.edit(available_tags=available + [
                discord.ForumTag(name=TEMPLATES[purpose][kind]) for kind in missing], reason=REASON)
            if updated is None:
                raise ClubError('Discord не вернул теги после настройки. Повторите /club setup для проверки.')
            if updated is not forum:
                self._updated[id(forum)] = (forum, updated)
            # Resolve the entire response before saving anything. Existing IDs
            # retain authority even when a human has renamed the label.
            by_id = {tag.id: tag for tag in updated.available_tags}
            for kind, previous in list(selected.items()):
                if previous.id not in by_id:
                    raise ClubError('Набор тегов изменился во время настройки. Повторите /club setup.')
                selected[kind] = by_id[previous.id]
            for kind in missing:
                matches = [tag for tag in updated.available_tags if tag.name == TEMPLATES[purpose][kind]]
                if len(matches) != 1 or not matches[0].id:
                    raise ClubError('Не удалось однозначно получить ID новых тегов. Повторите /club setup.')
                selected[kind] = matches[0]
        for kind, tag in selected.items():
            if bindings.get(kind) != tag.id:
                self.store.bind_tag(guild.id, forum.id, kind, tag.id)
        return [f'Теги форума {forum.id}: готовы; добавлено {len(missing)}.']

    def _selected(self, guild_id, forum, purposes):
        forum = self.current(forum)
        purposes = list(dict.fromkeys(purposes))
        if any(purpose not in ALL_PURPOSES for purpose in purposes):
            raise ClubError('Неизвестное назначение тега книжного клуба.')
        selected = [self.get(guild_id, forum, purpose) for purpose in purposes]
        if any(tag is None for tag in selected):
            if self.bindings(guild_id, forum) or forum.flags.require_tag:
                raise ClubError(f'В форуме {forum.id} не найдены нужные теги клуба. Выполните /club setup.')
            return []  # Existing manually configured clubs may opt in later.
        if not selected and forum.flags.require_tag:
            raise ClubError(f'Форум {forum.id} требует тег. Выполните /club setup.')
        if len(selected) > 5:
            raise ClubError('На одной теме Discord может быть не больше 5 тегов.')
        return selected

    def creation_tags(self, guild_id, forum, kind, book_status=None):
        if kind == 'book':
            purposes = [book_status or 'proposed']
            if purposes[0] not in TEMPLATES['books'] or purposes[0] == 'catalog':
                raise ClubError('Неизвестный статус книги для тега.')
        elif kind == 'imported':
            purposes = ['essay', 'imported']
        else:
            purposes = [kind]
        return self._selected(guild_id, forum, purposes)

    async def sync_thread_tags(self, guild, thread, desired_purposes):
        if thread.guild.id != guild.id:
            raise ClubError('Тема тегов принадлежит другому серверу.')
        forum = thread.parent
        if forum is None:
            forum = await self.service.channel(guild, thread.parent_id, discord.ForumChannel)
        if not isinstance(forum, discord.ForumChannel):
            return False
        # discord.py's public applied_tags property silently filters IDs through
        # the cached parent. A fetched thread can contain newer tag IDs than that
        # cache, so preserve the transport's complete set before resolving names.
        raw_ids = getattr(thread, '_applied_tags', None)
        if isinstance(raw_ids, (list, tuple)) and all(isinstance(ident, int) for ident in raw_ids):
            previous_ids = list(dict.fromkeys(raw_ids))
        else:
            previous_ids = list(dict.fromkeys(tag.id for tag in thread.applied_tags))
        forum = await self.fresh(guild, forum, tag_ids=previous_ids)
        desired = self._selected(guild.id, forum, desired_purposes)
        if not desired:
            return False
        bindings = self.bindings(guild.id, forum)
        replaceable = {ident for purpose, ident in bindings.items() if purpose in STATUS_PURPOSES}
        by_id = {tag.id: tag for tag in forum.available_tags}
        if not set(previous_ids).issubset(by_id):
            raise ClubError('Теги темы изменились. Повторите /club setup для проверки; пользовательские теги сохранены.')
        previous = [by_id[ident] for ident in previous_ids]
        # imported is provenance, not a status: publishing an essay must retain it.
        result = [tag for tag in previous if tag.id not in replaceable]
        result_ids = {tag.id for tag in result}
        result.extend(tag for tag in desired if tag.id not in result_ids)
        if any(tag.id not in by_id for tag in result):
            raise ClubError('Теги темы изменились. Повторите /club setup для проверки.')
        if len(result) > 5:
            raise ClubError(f'В теме {thread.id} нет места для тегов клуба (лимит Discord — 5); '
                            'пользовательские теги сохранены.')
        if {tag.id for tag in previous} == {tag.id for tag in result}:
            return False
        options = {'applied_tags': result, 'reason': REASON}
        archived = thread.archived
        if archived:
            options['archived'] = False
        updated = await thread.edit(**options)
        if archived:
            await (updated or thread).edit(archived=True, reason=REASON)
        return True

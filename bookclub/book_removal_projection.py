"""Project confirmed essay reassignment without copying messages or their authors."""
from __future__ import annotations

import hashlib

import discord

from .book_removal import (complete_removal_resource, fail_removal_resource,
                           remember_removal_archive_state, removal_operations, removal_resources)
from .render import safe
from .store import ClubError


def retained_removal_publication(store, guild_id, *, channel_id=None, message_id=None):
    """Gateway DELETE events must keep the frozen disposal's recovery evidence."""
    return store.one('''SELECT 1 FROM bc_book_removal_resources WHERE guild_id=?
      AND kind IN ('delete_book_topic','delete_essay')
      AND ((channel_id=? AND (kind='delete_book_topic' OR source_id=channel_id))
        OR message_id=?) LIMIT 1''', (guild_id, channel_id, message_id)) is not None


def _current_essay(store, guild_id, resource):
    essay = store.one('SELECT * FROM bc_essays WHERE guild_id=? AND source_id=?',
                      (guild_id, resource['source_id']))
    if (not essay or essay['deleted'] or essay['book_id'] != resource['target_book_id']
            or essay['channel_id'] != resource['channel_id']
            or essay['author_id'] != resource['essay']['author_id']):
        raise ClubError('Привязка переносимого эссе изменилась. Проверьте операцию удаления.')
    book = store.require_active_book(guild_id, essay['book_id'])
    return essay, book


async def _project_essay(service, guild, operation, resource):
    store = service.store
    essay, book = _current_essay(store, guild.id, resource)
    channel = await service.bot.fetch_channel(essay['channel_id'])
    if channel.guild.id != guild.id:
        raise ClubError('Эссе находится на другом сервере; оформление не изменено.')
    if essay['source_id'] != essay['channel_id']:
        # A participant's original message is their essay, not our template.
        # Rebinding its durable source ID is the entire operation.
        await channel.fetch_message(essay['source_id'])
        return
    if not isinstance(channel, discord.Thread) or channel.parent_id != store.settings(guild.id)['essays']:
        raise ClubError('Тема эссе находится вне настроенного форума; оформление не изменено.')
    pub = store.one('''SELECT * FROM bc_publications WHERE guild_id=? AND channel_id=?
      AND message_id=? AND (key LIKE 'essay-space:%' OR key LIKE 'essay-import:%')''',
      (guild.id, channel.id, channel.id))
    if not pub:
        # A native participant-owned post keeps its text and custom title.
        await channel.fetch_message(essay['source_id'])
        return
    member, organizer = await service.actor(guild, operation['actor_id'])
    if not organizer or not service.can_read(channel, member):
        raise ClubError('Для оформления переноса нужен организатор с доступом к теме эссе.')
    starter = await channel.fetch_message(essay['source_id'])
    if not service.owns_starter(starter, pub['webhook_id']):
        raise ClubError('Сохранённая шапка эссе принадлежит другому отправителю.')
    essay, book = _current_essay(store, guild.id, resource)
    if pub['key'].startswith('essay-space:'):
        # A thread suffix preserves both works when the author has essays in
        # both duplicate books. No primary book/author reservation is replaced.
        key = f'essay-space:{book["id"]}:{essay["author_id"]}:{channel.id}'
        if pub['key'] != key:
            with store.tx() as db:
                other = db.execute('SELECT * FROM bc_publications WHERE key=?', (key,)).fetchone()
                if other:
                    raise ClubError('У эссе уже есть другая привязка публикации.')
                db.execute('UPDATE bc_publications SET key=?,content_hash=NULL WHERE key=?', (key, pub['key']))
            pub = store.publication(key)
        content = service.essay_starter(book, essay['author_id'])
    else:
        # Imported bodies, attachments and author styling remain untouched.
        # Only the known bot/webhook heading names the selected destination.
        first, separator, remainder = starter.content.partition('\n')
        if not first.startswith('**Архивное эссе по книге «') or not first.endswith('»**'):
            raise ClubError('Шапка эссе изменена вручную; автоматическое оформление остановлено.')
        content = f'**Архивное эссе по книге «{safe(book["title"])}»**' + separator + remainder
    archived = remember_removal_archive_state(store, guild.id, operation['id'],
                                               resource['id'], channel.archived)
    try:
        if channel.archived:
            channel = await channel.edit(archived=False)
        if starter.content != content:
            await service.edit_essay_starter(channel, starter, content, pub)
            confirmed = await channel.fetch_message(starter.id)
            if confirmed.content != content or not service.owns_starter(confirmed, pub['webhook_id']):
                raise ClubError('Шапку эссе не удалось обновить. Привязка сохранена; проверьте вебхук.')
        _current_essay(store, guild.id, resource)
        store.save_publication(pub['key'], channel.id, starter.id, hashlib.sha256(content.encode()).hexdigest())
        prefix, separator, suffix = channel.name.partition(' · Эссе · ')
        old_title = operation['plan']['source']['title']
        if separator and (prefix == old_title or (len(prefix) >= 10 and old_title.startswith(prefix))):
            name = f'{book["title"][:max(1, 100 - len(separator) - len(suffix))]}{separator}{suffix}'
            if name != channel.name:
                channel = await channel.edit(name=name, archived=False)
        store.register_essay(guild.id, book['id'], essay['source_id'], essay['channel_id'],
                             essay['author_id'], channel.name, essay['url'],
                             managed=essay['managed'], submitted=essay['submitted'])
    finally:
        if archived:
            restored = await channel.edit(archived=True)
            if not restored.archived:
                raise ClubError('Discord не подтвердил восстановление архива темы. Повторите незавершённое действие.')


async def process_essay_transfers(service, guild, book_id):
    """Caller holds the guild publication lock; each step is an edit on an ID."""
    for operation in removal_operations(service.store, guild.id, book_id=book_id):
        for resource in removal_resources(service.store, guild.id, operation['id']):
            if resource['kind'] != 'refresh_essay':
                continue
            try:
                await _project_essay(service, guild, operation, resource)
            except Exception as exc:
                reason = str(exc) if isinstance(exc, ClubError) else type(exc).__name__
                fail_removal_resource(service.store, guild.id, operation['id'], resource['id'], reason)
                break
            else:
                complete_removal_resource(service.store, guild.id, operation['id'], resource['id'])

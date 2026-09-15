"""Execute a confirmed, immutable removal plan without forgetting its bindings.

Caller holds the guild publication lock. Every irreversible request repeats the
live permission and binding checks. A lost DELETE response leaves ``deleting``;
the next attempt first GETs the same resource, so 404 completes recovery without
another DELETE. Permission/integrity failures require an explicit human retry.
"""
from __future__ import annotations

import asyncio
import logging

import aiohttp
import discord

from . import book_removal as plans
from .store import ClubError

log = logging.getLogger(__name__)
_TRANSPORT_ERRORS = (OSError, asyncio.TimeoutError, aiohttp.ClientError)


def _binding(publication):
    return tuple(publication.get(key) for key in
                 ('key', 'guild_id', 'channel_id', 'message_id', 'webhook_id'))


def _bound(service, guild, operation, resource):
    """Pure SQLite checks are repeated after the final await before DELETE."""
    store = service.store
    book = store.book(guild.id, operation['book_id'])
    if not book.get('deleted') or operation['guild_id'] != guild.id:
        raise ClubError('Книга больше не удалена или операция относится к другому серверу.')
    purpose = 'books' if resource['kind'] == 'delete_book_topic' else 'essays'
    if resource.get('forum_id') != store.settings(guild.id)[purpose]:
        raise ClubError('Настроенный форум изменился после подтверждения. Проверьте операцию заново.')
    if resource['kind'] == 'delete_book_topic':
        if operation['mode'] not in ('topic', 'all', 'transfer_topic'):
            raise ClubError('Удаление темы не входит в подтверждённый вариант.')
        key = f'book:{book["id"]}'
        old = resource.get('publication')
        current = store.publication(key)
        if (not old or not current or old.get('key') != key
                or resource.get('publication_key') != key
                or _binding(current) != _binding(old)
                or current['guild_id'] != guild.id
                or current['channel_id'] != resource['channel_id']
                or current['message_id'] != resource['message_id']
                or resource['message_id'] != resource['channel_id']):
            raise ClubError('Привязка темы книги изменилась. Тема не удалена.')
        allowed = {key}
        allowed.update(f'meeting:{row["id"]}' for row in store.rows(
            'SELECT id FROM bc_meetings WHERE book_id=?', (book['id'],)))
        for pub in store.rows('SELECT key,guild_id FROM bc_publications WHERE channel_id=?',
                              (resource['channel_id'],)):
            if pub['guild_id'] != guild.id or not any(
                    pub['key'] == prefix or pub['key'].startswith(prefix + ':page:') for prefix in allowed):
                raise ClubError('В теме есть публикации другого назначения. Тема сохранена.')
        # Even a topic-only choice must never remove an essay it promised to keep.
        if store.one('SELECT 1 FROM bc_essays WHERE channel_id=? AND deleted=0',
                     (resource['channel_id'],)):
            raise ClubError('В теме книги остались эссе. Сначала проверьте их перенос или удаление.')
        return
    if resource['kind'] != 'delete_essay' or operation['mode'] != 'all':
        raise ClubError('Удаление эссе не входит в подтверждённый вариант.')
    essay = resource.get('essay')
    current = store.one('SELECT * FROM bc_essays WHERE guild_id=? AND source_id=?',
                        (guild.id, resource['source_id']))
    fields = ('guild_id', 'source_id', 'channel_id', 'book_id', 'author_id')
    if (not essay or not current or any(current[key] != essay[key] for key in fields)
            or current['book_id'] != book['id']
            or current['channel_id'] != resource['channel_id']):
        raise ClubError('Привязка эссе изменилась. Эссе не удалено.')
    old = resource.get('publication')
    imported = {row['item_key'] for row in store.rows(
        'SELECT item_key FROM bc_import_sources WHERE guild_id=? AND thread_id=?',
        (guild.id, resource['channel_id']))}

    def allowed_essay_publication(pub):
        key = pub['key']
        return pub['guild_id'] == guild.id and (
            key.startswith(f'essay-space:{book["id"]}:')
            or key == f'essay-choice:{resource["channel_id"]}'
            or any(key == prefix or key.startswith(prefix + ':message:') for prefix in imported))

    if old:
        pub = store.publication(old['key'])
        if not pub or _binding(pub) != _binding(old) or not allowed_essay_publication(pub):
            raise ClubError('Привязка публикации эссе изменилась. Эссе не удалено.')
    if resource['source_id'] == resource['channel_id']:
        if store.one('SELECT 1 FROM bc_essays WHERE channel_id=? AND book_id<>?',
                     (resource['channel_id'], book['id'])):
            raise ClubError('В теме есть эссе другой книги. Общая тема сохранена.')
        for pub in store.rows('SELECT key,guild_id FROM bc_publications WHERE channel_id=?',
                              (resource['channel_id'],)):
            if not allowed_essay_publication(pub):
                raise ClubError('В теме эссе есть публикации другой книги или назначения. Тема сохранена.')
    else:
        for pub in store.rows('SELECT * FROM bc_publications WHERE message_id=?',
                              (resource['source_id'],)):
            if not old or _binding(pub) != _binding(old):
                raise ClubError('Сообщение также связано с другой публикацией. Оно сохранено.')


async def _channel(service, guild, resource):
    channel = await service.bot.fetch_channel(resource['channel_id'])
    if getattr(getattr(channel, 'guild', None), 'id', None) != guild.id:
        raise ClubError('Канал принадлежит другому серверу. Удаление остановлено.')
    whole_thread = (resource['kind'] == 'delete_book_topic'
                    or resource['source_id'] == resource['channel_id'])
    if whole_thread:
        purpose = 'books' if resource['kind'] == 'delete_book_topic' else 'essays'
        parent_id = service.store.settings(guild.id)[purpose]
        if not isinstance(channel, discord.Thread) or channel.parent_id != parent_id:
            raise ClubError('Тема больше не принадлежит настроенному форуму. Удаление остановлено.')
        parent = await service.bot.fetch_channel(parent_id)
        if not isinstance(parent, discord.ForumChannel) or parent.guild.id != guild.id:
            raise ClubError('Родительский форум недоступен или принадлежит другому серверу.')
        return channel, parent, True
    if isinstance(channel, discord.Thread):
        parent = await service.bot.fetch_channel(channel.parent_id)
        if (not isinstance(parent, (discord.TextChannel, discord.ForumChannel))
                or parent.guild.id != guild.id):
            raise ClubError('Родительский канал сообщения не подтверждён. Сообщение сохранено.')
    elif isinstance(channel, discord.TextChannel):
        parent = channel
    else:
        raise ClubError('Тип канала эссе не допускает безопасное удаление сообщения.')
    return channel, parent, False


async def _authorize(service, guild, operation, channel, parent, whole_thread):
    member, organizer = await service.actor(guild, operation['actor_id'])
    if not organizer:
        raise ClubError('Организатор больше не имеет права выполнять удаление. Требуется новое подтверждение.')
    if not service.can_read(parent, member) or not service.can_read(channel, member):
        raise ClubError('У организатора больше нет доступа к удаляемой теме или сообщению.')
    bot_member = await guild.fetch_member(service.bot.user.id)
    if bot_member.id != service.bot.user.id or bot_member.guild.id != guild.id:
        raise ClubError('Не удалось подтвердить участника-бота на этом сервере.')
    permissions = parent.permissions_for(bot_member)
    required = 'manage_threads' if whole_thread else 'manage_messages'
    if not service.can_read(parent, bot_member) or not getattr(permissions, required, False):
        name = 'Manage Threads' if whole_thread else 'Manage Messages'
        raise ClubError(f'Боту не хватает доступа или права {name}. Удаление приостановлено.')


def _complete(store, guild, operation, resource):
    # Retain every source/publication/import binding for audit and recovery.
    if resource['kind'] == 'delete_essay':
        store.delete_essay(guild.id, source_id=resource['source_id'])
    plans.complete_removal_resource(store, guild.id, operation['id'], resource['id'])


async def _delete_resource(service, guild, operation, resource):
    store = service.store
    _bound(service, guild, operation, resource)
    try:
        channel, parent, whole_thread = await _channel(service, guild, resource)
    except discord.NotFound:
        # Check the exact ID again: a missing parent does not mean its live child
        # was deleted. An absent resource is already the requested final state.
        try:
            await service.bot.fetch_channel(resource['channel_id'])
        except discord.NotFound:
            _bound(service, guild, operation, resource)
            _complete(store, guild, operation, resource)
            return
        raise ClubError('Родительский канал не найден; существующая тема сохранена.') from None
    target = channel
    if whole_thread:
        try:
            starter = await channel.fetch_message(channel.id)
        except discord.NotFound:
            raise ClubError('Стартовое сообщение темы не найдено. Её привязку нужно проверить вручную.') from None
        pub = resource.get('publication')
        if resource['kind'] == 'delete_book_topic' or pub:
            if not pub or not service.owns_starter(starter, pub.get('webhook_id')):
                raise ClubError('Автор стартового сообщения не совпадает с сохранённой публикацией.')
        elif (getattr(channel, 'owner_id', None) != resource['essay']['author_id']
              or starter.author.id != resource['essay']['author_id']):
            raise ClubError('Автор темы эссе не совпадает с сохранённой привязкой.')
    else:
        try:
            target = await channel.fetch_message(resource['source_id'])
        except discord.NotFound:
            _bound(service, guild, operation, resource)
            _complete(store, guild, operation, resource)
            return
        if (target.id != resource['source_id'] or target.channel.id != channel.id
                or target.guild.id != guild.id):
            raise ClubError('Discord вернул другое сообщение. Удаление остановлено.')
        pub = resource.get('publication')
        if ((pub and not service.owns_starter(target, pub.get('webhook_id')))
                or (not pub and target.author.id != resource['essay']['author_id'])):
            raise ClubError('Автор сообщения не совпадает с сохранённой привязкой эссе.')
    await _authorize(service, guild, operation, channel, parent, whole_thread)
    # Authorization made REST calls: bindings may have changed while awaiting.
    _bound(service, guild, operation, resource)
    current = next((row for row in plans.removal_resources(
        store, guild.id, operation['id'], pending_only=False) if row['id'] == resource['id']), None)
    if not current or current['state'] not in ('pending', 'deleting'):
        return
    plans.set_removal_resource_state(store, guild.id, operation['id'], resource['id'], 'deleting')
    try:
        if whole_thread:
            await target.delete(reason='Подтверждённое удаление книги организатором клуба')
        else:
            await target.delete()
    except discord.NotFound:
        pass
    except _TRANSPORT_ERRORS:
        # Keep the durable uncertainty and inspect the same ID on the next pass.
        raise
    _complete(store, guild, operation, resource)


async def process_disposal(service, guild, book_id):
    """Process pending DELETEs only; transfer projections are owned by Service."""
    if service.guild_ids is not None and guild.id not in service.guild_ids:
        return
    for operation in plans.removal_operations(service.store, guild.id, book_id=book_id):
        resources = plans.removal_resources(service.store, guild.id, operation['id'], pending_only=False)
        resources.sort(key=lambda row: row['kind'] == 'delete_book_topic')
        for resource in resources:
            if resource['kind'] not in ('delete_book_topic', 'delete_essay'):
                continue
            if resource['state'] not in ('pending', 'deleting'):
                continue
            if resource['kind'] == 'delete_book_topic':
                others = plans.removal_resources(service.store, guild.id, operation['id'], pending_only=False)
                if any(row['id'] != resource['id'] and row['state'] != 'done' for row in others):
                    continue
            try:
                await _delete_resource(service, guild, operation, resource)
            except asyncio.CancelledError:
                raise
            except _TRANSPORT_ERRORS as exc:
                log.warning('Book removal awaits Discord confirmation guild=%s operation=%s error=%s',
                            guild.id, operation['id'], type(exc).__name__)
                # Do not continue deleting more resources during transport loss.
                break
            except (discord.HTTPException, ClubError) as exc:
                if isinstance(exc, discord.HTTPException) and exc.status >= 500:
                    log.warning('Book removal awaits Discord recovery guild=%s operation=%s error=%s',
                                guild.id, operation['id'], type(exc).__name__)
                    break
                reason = str(exc) if isinstance(exc, ClubError) else 'Discord отклонил удаление. Проверьте права и повторите подтверждение.'
                plans.fail_removal_resource(service.store, guild.id, operation['id'], resource['id'], reason)
                log.warning('Book removal paused guild=%s operation=%s error=%s',
                            guild.id, operation['id'], type(exc).__name__)
                break
            except Exception as exc:
                plans.fail_removal_resource(
                    service.store, guild.id, operation['id'], resource['id'],
                    'Не удалось безопасно завершить удаление. Проверьте журнал и повторите подтверждение.')
                log.warning('Book removal paused guild=%s operation=%s error=%s',
                            guild.id, operation['id'], type(exc).__name__)
                break

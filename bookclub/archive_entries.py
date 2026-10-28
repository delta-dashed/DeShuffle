"""One plain archive message per book, with stable IDs and safe retirement."""
from __future__ import annotations

import hashlib
import json
import logging
import re

import discord

from .store import ClubError
from .town_cleanup import OBSOLETE_NOTICES
from .town_render import archive_entries, legacy_archive_pages

log = logging.getLogger(__name__)
PENDING_BODY = 'archive-entry-edit-v1\n'


def _bodies(intent):
    if not intent:
        return []
    content = intent['content']
    if not content.startswith(PENDING_BODY):
        return [content]
    try:
        bodies = json.loads(content[len(PENDING_BODY):])
    except ValueError:
        return []
    return bodies if (isinstance(bodies, list) and 1 <= len(bodies) <= 2
                      and all(isinstance(body, str) for body in bodies)) else []


def _save_ready_body(store, guild_id, pub, content):
    with store.tx() as db:
        db.execute('''UPDATE bc_publication_intents SET content=? WHERE key=? AND EXISTS
          (SELECT 1 FROM bc_publications p WHERE p.key=? AND p.guild_id=?
           AND p.state='ready' AND p.channel_id=? AND p.message_id=?)''',
          (content, pub['key'], pub['key'], guild_id, pub['channel_id'], pub['message_id']))


def _plain_digest(content):
    return hashlib.sha256(('clean-delivery-v1\n' + content + '[]None\nsuppress-embeds').encode()).hexdigest()


def _safe_message(service, message, thread):
    embeds = getattr(message, 'embeds', ())
    return (message.channel.id == thread.id and service.owns_starter(message) and not message.attachments
            and not (isinstance(embeds, (list, tuple)) and embeds))


def _unshared(store, guild_id, pub):
    return not store.one('''SELECT key FROM bc_publications WHERE guild_id=? AND message_id=?
      AND key<>? LIMIT 1''', (guild_id, pub['message_id'], pub['key']))


async def _delete(service, thread, pub, message):
    # A lost DELETE reply never turns into another send. Keep the binding
    # until success or a GET confirms that this exact message is absent.
    try:
        await message.delete()
    except discord.NotFound:
        pass
    except discord.Forbidden:
        log.warning('Archive message retained: missing delete permission message=%s', pub['message_id'])
        return
    except (discord.HTTPException, OSError, TimeoutError):
        try:
            await thread.fetch_message(pub['message_id'])
        except discord.NotFound:
            pass
        else:
            raise
    service.store.forget_publication(pub['key'])


async def _recover_pending(service, guild, prefix):
    for pub in service.store.rows('''SELECT * FROM bc_publications WHERE guild_id=?
      AND key LIKE ? AND message_id IS NULL ORDER BY key''', (guild.id, prefix + '%')):
        try:
            channel = await service.channel(guild, pub['channel_id'], discord.Thread)
        except discord.NotFound:
            service.store.forget_publication(pub['key'])  # Deleted topic proves its messages absent.
            continue
        found = await service.delivery.recover(guild, channel, pub['key'])
        if not found:
            raise ClubError('Отправка книги в архив не подтверждена. Проверьте /club diagnose и /club repair.')
        service.store.save_publication(pub['key'], found[0].id, found[1].id)


async def _retire_pages(service, guild, root, thread):
    """Remove only demonstrably generated former archive pages after migration."""
    key = root['key']
    expected = set(legacy_archive_pages(service.store, guild.id)[1:]) | set(OBSOLETE_NOTICES)
    publications = service.store.rows('SELECT * FROM bc_publications WHERE guild_id=? AND key LIKE ?',
                                       (guild.id, key + ':page:%'))
    page_urls = {f'https://discord.com/channels/{guild.id}/{p["channel_id"]}/{p["message_id"]}'
                 for p in publications if p['message_id']}
    page_urls.add(f'https://discord.com/channels/{guild.id}/{thread.id}/{root["message_id"]}')
    for pub in publications:
        if not pub['key'][len(key + ':page:'):].isdecimal() or pub['channel_id'] != thread.id:
            continue
        if not pub['message_id']:
            found = await service.delivery.recover(guild, thread, pub['key'])
            if not found:
                raise ClubError('Прежняя страница архива не подтверждена. Проверьте /club repair.')
            service.store.save_publication(pub['key'], found[0].id, found[1].id)
            pub = service.store.publication(pub['key'])
        if pub['message_id'] == root['message_id'] or not _unshared(service.store, guild.id, pub):
            continue
        try:
            message = await thread.fetch_message(pub['message_id'])
        except discord.NotFound:
            service.store.forget_publication(pub['key'])
            continue
        intent = service.delivery.intent(pub['key'])
        original = intent['content'] if intent else ''
        original_page = (original == message.content and bool(re.match(
            r'^## Архив прочитанного(?: · \d+/\d+)?\n', original)))
        controls_ok = True
        for row in message.components:
            value = row.to_dict()
            if value.get('type') != 1:
                controls_ok = False
            for button in value.get('components', []):
                if (button.get('type') != 2 or button.get('style') != 5 or button.get('custom_id')
                        or button.get('url') not in page_urls
                        or button.get('label') not in {'К началу', '← Предыдущая', 'Следующая →'}):
                    controls_ok = False
        if (_safe_message(service, message, thread) and controls_ok
                and (message.content in expected or original_page)):
            await _delete(service, thread, pub, message)
        else:
            log.warning('Former archive page retained after manual change message=%s', pub['message_id'])


async def publish_archive(service, guild, root, *, verify=False):
    """Caller holds the guild publication lock. Never changes books or essays."""
    store = service.store
    key = f'catalog:{guild.id}:archive'
    current = store.publication(key)
    if (root['key'] != key or root['guild_id'] != guild.id or not current
            or any(root[field] != current[field] for field in ('channel_id', 'message_id'))):
        raise ClubError('Тема архива изменилась. Повторите обновление.')
    thread = await service.channel(guild, root['channel_id'], discord.Thread)
    if thread.parent_id != store.settings(guild.id)['books']:
        raise ClubError('Архив находится вне настроенного форума книг.')
    prefix = key + ':book:'
    await _recover_pending(service, guild, prefix)
    # A former page's uncertain send must be resolved before another POST in
    # the same channel, even though pages are no longer used.
    for pub in store.rows('''SELECT * FROM bc_publications WHERE guild_id=? AND key LIKE ?
      AND channel_id=? AND message_id IS NULL''', (guild.id, key + ':page:%', thread.id)):
        found = await service.delivery.recover(guild, thread, pub['key'])
        if not found:
            raise ClubError('Прежняя страница архива не подтверждена. Проверьте /club repair.')
        store.save_publication(pub['key'], found[0].id, found[1].id)

    entries = archive_entries(store, guild.id)
    active_keys = {prefix + str(book_id) for book_id in entries}
    for book_id, content in entries.items():
        entry_key = prefix + str(book_id)
        pub = store.publication(entry_key)
        known_message = None
        if (pub and pub['message_id'] and pub['channel_id'] == thread.id
                and (verify or pub['content_hash'] != _plain_digest(content))):
            try:
                known_message = await thread.fetch_message(pub['message_id'])
            except discord.NotFound:
                store.forget_publication(entry_key)
            else:
                intent = service.delivery.intent(entry_key)
                confirmed = known_message.content in _bodies(intent) or known_message.content == content
                confirmed = confirmed or _plain_digest(known_message.content) == pub['content_hash']
                if not (_safe_message(service, known_message, thread) and not known_message.components and confirmed):
                    log.warning('Archive entry retained after manual change message=%s', pub['message_id'])
                    continue
                if known_message.content != content:
                    # A READY publication already has a bound ID. Retain both
                    # the observed and desired body before PATCH; either can
                    # remain after a lost edit reply or subsequent status change.
                    pending = PENDING_BODY + json.dumps([known_message.content, content], ensure_ascii=False)
                    if intent and intent['content'] != pending:
                        _save_ready_body(store, guild.id, pub, pending)
        pub = await service.upsert(guild, entry_key, thread.id, content, suppress_embeds=True, known_message=known_message)
        # Only this new namespace uses a READY intent's content as the last
        # confirmed body for later deletion. Reserved intents remain immutable;
        # their original body still protects recovery after a lost POST reply.
        intent = service.delivery.intent(entry_key)
        if intent and intent['content'] != content:
            _save_ready_body(store, guild.id, pub, content)

    for pub in store.rows('SELECT * FROM bc_publications WHERE guild_id=? AND key LIKE ?',
                          (guild.id, prefix + '%')):
        if pub['key'] in active_keys or pub['channel_id'] != thread.id or not pub['message_id']:
            continue
        if not _unshared(store, guild.id, pub) or pub['message_id'] == root['message_id']:
            continue
        try:
            message = await thread.fetch_message(pub['message_id'])
        except discord.NotFound:
            store.forget_publication(pub['key'])
            continue
        intent = service.delivery.intent(pub['key'])
        proven = message.content in _bodies(intent) or _plain_digest(message.content) == pub['content_hash']
        if _safe_message(service, message, thread) and not message.components and proven:
            await _delete(service, thread, pub, message)
        else:
            log.warning('Archive entry retained after manual change message=%s', pub['message_id'])
    await _retire_pages(service, guild, root, thread)
    return {book_id: store.publication(prefix + str(book_id)) for book_id in entries}

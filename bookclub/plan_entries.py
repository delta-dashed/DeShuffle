"""A chronological message list whose positional rows follow the reading queue."""
from __future__ import annotations

import json
import logging
import re
import uuid

import discord

from .archive_entries import (_bodies, _delete, _plain_digest, _recover_pending,
                              _safe_message, _save_ready_body, _unshared, PENDING_BODY)
from .store import ClubError
from .town_cleanup import OBSOLETE_NOTICES
from .town_render import legacy_plan_pages, plan_entries

log = logging.getLogger(__name__)


def _rows(store, guild_id, prefix):
    return [pub for pub in store.rows('SELECT * FROM bc_publications WHERE guild_id=? AND key LIKE ?',
                                      (guild_id, prefix + '%'))
            if pub['key'][len(prefix):].isdecimal()]


async def _compact(service, guild, root, thread, entries, *, verify):
    """Keep surviving messages in Discord order, then reassign queue slots.

    Recover uncertain sends first. Key changes and their intent rows commit in
    one transaction through ON UPDATE CASCADE; no messages are deleted merely
    to repair a missing row or to reorder books.
    """
    store, prefix = service.store, root['key'] + ':row:'
    publications = _rows(store, guild.id, prefix)
    valid, retained, known = [], [], {}
    for pub in publications:
        if not pub['message_id']:
            raise ClubError('Строка плана ещё не подтверждена. Проверьте /club repair.')
        if pub['channel_id'] != thread.id:
            retained.append(pub)  # Preserve the old forum's history.
            continue
        if pub['message_id'] == root['message_id'] or not _unshared(store, guild.id, pub):
            retained.append(pub)
            continue
        desired = entries.get(int(pub['key'][len(prefix):]))
        if verify or desired is None or pub['content_hash'] != _plain_digest(desired):
            try:
                message = await thread.fetch_message(pub['message_id'])
            except discord.NotFound:
                store.forget_publication(pub['key'])
                continue
            proof = message.content in _bodies(service.delivery.intent(pub['key']))
            proof = proof or _plain_digest(message.content) == pub['content_hash'] or message.content == desired
            if not (_safe_message(service, message, thread) and not message.components and proof):
                retained.append(pub)
                log.warning('Plan row retained after manual change message=%s', pub['message_id'])
                continue
            known[message.id] = message
        valid.append(pub)
    valid.sort(key=lambda pub: pub['message_id'])
    if not verify and any(pub['key'] != prefix + str(index) and pub['message_id'] not in known
                          for index, pub in enumerate(valid, 1)):
        # A hole can move an otherwise unchanged cached message into a new
        # position. Read it before changing its key so the following PATCH
        # always records both possible bodies before a lost edit response.
        return await _compact(service, guild, root, thread, entries, verify=True)
    moves = [(pub, prefix + str(index)) for index, pub in enumerate(valid, 1) if pub['key'] != prefix + str(index)]
    moves.extend((pub, f'{root["key"]}:retained:{pub["message_id"]}') for pub in retained)
    if moves:
        token = uuid.uuid4().hex
        with store.tx() as db:
            for pub, destination in moves:
                conflict = db.execute('SELECT key FROM bc_publications WHERE key=? AND guild_id=?',
                                      (destination, guild.id)).fetchone()
                if conflict and destination not in {item['key'] for item, _ in moves}:
                    raise ClubError('Неоднозначная привязка строки плана. Проверьте /club diagnose.')
            for index, (pub, _) in enumerate(moves):
                db.execute('UPDATE bc_publications SET key=? WHERE guild_id=? AND key=?',
                           (f'plan-reindex:{token}:{index}', guild.id, pub['key']))
            for index, (_, destination) in enumerate(moves):
                db.execute('UPDATE bc_publications SET key=? WHERE guild_id=? AND key=?',
                           (destination, guild.id, f'plan-reindex:{token}:{index}'))
    return known


async def _retire_pages(service, guild, root, thread):
    store, key = service.store, root['key']
    expected = set(legacy_plan_pages(store, guild.id)[1:]) | set(OBSOLETE_NOTICES)
    publications = store.rows('SELECT * FROM bc_publications WHERE guild_id=? AND key LIKE ?',
                              (guild.id, key + ':page:%'))
    urls = {f'https://discord.com/channels/{guild.id}/{p["channel_id"]}/{p["message_id"]}'
            for p in publications if p['message_id'] and p['channel_id'] == thread.id}
    urls.add(f'https://discord.com/channels/{guild.id}/{thread.id}/{root["message_id"]}')
    for pub in publications:
        if (not pub['message_id'] or pub['channel_id'] != thread.id
                or not pub['key'][len(key + ':page:'):].isdecimal()
                or pub['message_id'] == root['message_id'] or not _unshared(store, guild.id, pub)):
            continue
        try:
            message = await thread.fetch_message(pub['message_id'])
        except discord.NotFound:
            store.forget_publication(pub['key'])
            continue
        intent = service.delivery.intent(pub['key'])
        original = intent['content'] if intent else ''
        proof = (message.content in expected or (message.content == original and bool(re.match(
            r'^## План чтения(?: · \d+/\d+)?\n', original))))
        controls = all(row.to_dict().get('type') == 1 and all(
            button.get('type') == 2 and button.get('style') == 5 and not button.get('custom_id')
            and button.get('url') in urls
            and button.get('label') in {'К началу', '← Предыдущая', 'Следующая →'}
            for button in row.to_dict().get('components', [])) for row in message.components)
        if _safe_message(service, message, thread) and proof and controls:
            await _delete(service, thread, pub, message)
        else:
            log.warning('Former plan page retained after manual change message=%s', pub['message_id'])


async def publish_plan(service, guild, root, *, verify=False):
    """Caller holds the publication lock. Books and essays are read-only here."""
    store, key = service.store, f'catalog:{guild.id}:plan'
    current = store.publication(key)
    if (root['key'] != key or root['guild_id'] != guild.id or not current
            or any(root[field] != current[field] for field in ('channel_id', 'message_id'))):
        raise ClubError('Тема плана изменилась. Повторите обновление.')
    thread = await service.channel(guild, root['channel_id'], discord.Thread)
    if thread.parent_id != store.settings(guild.id)['books']:
        raise ClubError('План находится вне настроенного форума книг.')
    prefix = key + ':row:'
    await _recover_pending(service, guild, prefix)
    await _recover_pending(service, guild, key + ':page:')
    entries = plan_entries(store, guild.id)
    known = await _compact(service, guild, root, thread, entries, verify=verify)
    for index, content in entries.items():
        row_key = prefix + str(index)
        pub = store.publication(row_key)
        message = known.get(pub['message_id']) if pub else None
        if pub and message is not None and message.content != content:
            pending = PENDING_BODY + json.dumps([message.content, content], ensure_ascii=False)
            intent = service.delivery.intent(row_key)
            if intent and intent['content'] != pending:
                _save_ready_body(store, guild.id, pub, pending)
        pub = await service.upsert(guild, row_key, thread.id, content, suppress_embeds=True, known_message=message)
        intent = service.delivery.intent(row_key)
        if intent and intent['content'] != content:
            _save_ready_body(store, guild.id, pub, content)
    for pub in _rows(store, guild.id, prefix):
        if int(pub['key'][len(prefix):]) <= len(entries) or pub['channel_id'] != thread.id:
            continue
        try:
            message = known.get(pub['message_id']) or await thread.fetch_message(pub['message_id'])
        except discord.NotFound:
            store.forget_publication(pub['key'])
            continue
        proof = message.content in _bodies(service.delivery.intent(pub['key']))
        proof = proof or _plain_digest(message.content) == pub['content_hash']
        if (_safe_message(service, message, thread) and not message.components and proof
                and _unshared(store, guild.id, pub) and pub['message_id'] != root['message_id']):
            await _delete(service, thread, pub, message)
    await _retire_pages(service, guild, root, thread)
    return {index: store.publication(prefix + str(index)) for index in entries}

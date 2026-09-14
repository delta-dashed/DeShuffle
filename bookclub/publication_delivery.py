"""Persist send intent before Discord I/O; recover without public identifiers.

An uncertain send is never repeated automatically. Recovery requires exactly one
unbound message from the expected sender after the saved history boundary, with
the original content, attachments and controls. Ambiguity requires human review.
Old reservations without an intent continue to use their historical markers.
"""
from __future__ import annotations

import json
import secrets

import discord

from .store import ClubError


def normalized_content(value):
    return value.strip(' \t\r\n')


def _components(value):
    if isinstance(value, dict):
        component = type(value.get('type')) is int
        return {k: _components(v) for k, v in value.items()
                if not (k == 'id' and component) and v is not None}
    if isinstance(value, list):
        return [_components(v) for v in value]
    return value


def controls(view):
    return _components(view.to_components()) if view else []


def file_manifest(files):
    result = []
    for file in files or []:
        position = file.fp.tell()
        file.fp.seek(0, 2)
        size = file.fp.tell()
        file.fp.seek(position)
        result.append([file.filename, size])
    return sorted(result)


class DeliveryJournal:
    def __init__(self, service):
        self.service, self.store = service, service.store

    def intent(self, key):
        return self.store.one('SELECT * FROM bc_publication_intents WHERE key=?', (key,))

    async def threads(self, guild, forum):
        found = {t.id: t for t in await guild.active_threads() if t.parent_id == forum.id}
        async for thread in forum.archived_threads(limit=None):
            if thread.parent_id == forum.id:
                found[thread.id] = thread
        return list(found.values())

    async def reserve(self, guild, channel, key, content, *, forum_name=None, view=None,
                      files=None, webhook_id=None, expected_attachments=None):
        if self.store.publication(key):
            return False
        if forum_name:
            after_id = max((t.id for t in await self.threads(guild, channel)), default=0)
        else:
            after_id = 0
            async for message in channel.history(limit=1):
                after_id = max(after_id, message.id)
        attachments = (sorted([list(a) for a in expected_attachments]) if expected_attachments is not None
                       else file_manifest(files))
        # Publication and intent are committed together before the first send.
        with self.store.tx() as db:
            existing = db.execute('SELECT key FROM bc_publications WHERE key=?', (key,)).fetchone()
            if existing:
                return False
            if db.execute('''SELECT 1 FROM bc_publication_intents i JOIN bc_publications p ON p.key=i.key
                WHERE i.guild_id=? AND i.channel_id=? AND i.webhook_id IS ? AND p.state='reserved' LIMIT 1''',
                (guild.id, channel.id, webhook_id)).fetchone():
                raise ClubError('В этом канале ещё не подтверждена предыдущая отправка этого отправителя. '
                                'Сначала восстановите её; следующая отправка не выполнена.')
            db.execute('INSERT INTO bc_publications(key,guild_id,channel_id,webhook_id) VALUES(?,?,?,?)',
                       (key, guild.id, channel.id, webhook_id))
            db.execute('''INSERT INTO bc_publication_intents
                (key,guild_id,channel_id,webhook_id,after_id,content,attachments,components,forum_name,created_at,nonce)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                (key, guild.id, channel.id, webhook_id, after_id, content,
                 json.dumps(attachments), json.dumps(controls(view), sort_keys=True),
                 forum_name, self.store.clock(), str(secrets.randbits(63))))
        return True

    async def recover(self, guild, channel, key, *, forum=False, webhook_id=None):
        intent = self.intent(key)
        if intent is None:
            return await self.service._find_marker(guild, channel, '\n-# bc:' + key, forum, webhook_id=webhook_id)
        if (intent['guild_id'] != guild.id or intent['channel_id'] != channel.id
                or intent['webhook_id'] != webhook_id or bool(intent['forum_name']) != forum):
            raise ClubError('Сохранённая отправка относится к другому каналу или отправителю. Повтор не выполнен.')
        attachments = json.loads(intent['attachments'])
        expected_controls = json.loads(intent['components'])
        matches = []

        def accept(message):
            if message.id <= intent['after_id'] or not self.service.owns_starter(message, webhook_id):
                return
            if normalized_content(message.content) != normalized_content(intent['content']):
                return
            if sorted([a.filename, a.size] for a in message.attachments) != attachments:
                return
            actual_controls = _components([component.to_dict() for component in message.components])
            if actual_controls != expected_controls:
                return
            nonce = getattr(message, 'nonce', None)
            if nonce is not None and str(nonce) != intent['nonce']:
                return
            if self.store.one('SELECT key FROM bc_publications WHERE guild_id=? AND message_id=? AND key<>?',
                              (guild.id, message.id, key)):
                return
            matches.append(message)

        if forum:
            for thread in await self.threads(guild, channel):
                if thread.id <= intent['after_id']:
                    continue
                try:
                    message = await thread.fetch_message(thread.id)
                except discord.NotFound:
                    continue
                accept(message)
        else:
            async for message in channel.history(limit=None, after=discord.Object(id=intent['after_id']), oldest_first=True):
                accept(message)
        if len(matches) > 1:
            raise ClubError('Найдено несколько совпадающих отправок. Автоматическая привязка и повтор запрещены; '
                            'организатору нужно проверить сообщения вручную.')
        if not matches:
            return None
        message = matches[0]
        return message.channel, message

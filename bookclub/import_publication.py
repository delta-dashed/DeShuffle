"""Author-styled archive copies, with durable webhook identity and recovery."""
from __future__ import annotations

import hashlib

import discord

from .render import safe
from .service import NO_MENTIONS
from .store import ClubError


def archive_header(book, author_id):
    return f'**Архивное эссе по книге «{safe(book["title"])}»**\nАвтор: <@{author_id}>.'


def archive_chunks(snapshot, *, legacy=False):
    body = snapshot['content']
    attachments = [f'Вложение: {safe(a["filename"])}' +
                   ('' if a['copy'] else (' — см. оригинал' if legacy else ' — файл превышает лимит переноса'))
                   for a in snapshot['attachments']]
    if attachments:
        body += ('\n' if body else '') + '\n'.join(attachments)
    return [body[index:index + 1500] for index in range(0, len(body), 1500)] or ['']


class ImportPublisher:
    def __init__(self, service):
        self.service, self.store = service, service.store

    def audit(self, guild_id, run_id, actor_id, action, thread_id, old_id, new_id):
        with self.store.tx() as db:
            db.execute('''INSERT OR IGNORE INTO bc_import_restyle_audit
                (guild_id,run_id,actor_id,action,thread_id,old_message_id,new_message_id,created_at)
                VALUES(?,?,?,?,?,?,?,?)''',
                (guild_id, run_id, actor_id, action, thread_id, old_id, new_id, self.store.clock()))

    async def retire_legacy(self, guild, run_id, actor_id, thread, key, replacement, expected, *, expected_attachments=None):
        old = self.store.publication(key)
        if not old:
            return
        if old['guild_id'] != guild.id or old['channel_id'] != thread.id or not old['message_id']:
            raise ClubError('Прежняя часть эссе имеет неподтверждённую привязку; она сохранена.')
        try:
            message = await thread.fetch_message(old['message_id'])
        except discord.NotFound:
            self.audit(guild.id, run_id, actor_id, 'old-copy-missing', thread.id, old['message_id'], replacement['message_id'])
            self.store.forget_publication(key)
            return
        if (not self.service.owns_starter(message, old['webhook_id'])
                or message.content != expected + '\n-# bc:' + key):
            raise ClubError('Прежняя копия отличается от сохранённого импорта. Она не удалена; проверьте тему.')
        if expected_attachments is not None:
            actual = sorted((attachment.filename, attachment.size) for attachment in message.attachments)
            if actual != sorted(expected_attachments):
                raise ClubError('Вложения прежней копии изменились. Она не удалена; проверьте тему.')
        self.audit(guild.id, run_id, actor_id, 'body-replacement-prepared', thread.id, message.id, replacement['message_id'])
        await message.delete()
        self.audit(guild.id, run_id, actor_id, 'body-replaced', thread.id, message.id, replacement['message_id'])
        self.store.forget_publication(key)

    async def upsert(self, guild, thread, key, content, member, *, files=None, expected_attachments=None):
        marker = '\n-# bc:' + key
        if len(content + marker) > 2000:
            raise ClubError('Часть эссе слишком длинная для Discord.')
        if thread.guild.id != guild.id or thread.parent_id != self.store.settings(guild.id)['essays']:
            raise ClubError('Тема копии больше не принадлежит настроенному форуму эссе.')
        pub, message, hook = self.store.publication(key), None, None
        if pub and (pub['guild_id'] != guild.id or pub['channel_id'] != thread.id):
            raise ClubError('Сохранённая часть эссе находится в другой теме.')
        if pub and pub['message_id']:
            try:
                message = await thread.fetch_message(pub['message_id'])
            except discord.NotFound:
                self.store.forget_publication(key)
                pub = None
        if not self.store.settings(guild.id)['essay_webhooks']:
            if pub and pub['webhook_id'] is not None:
                raise ClubError('Сохранённая часть отправлена вебхуком. Обычный бот не меняет её отправителя.')
            if message is None and pub:
                found = await self.service._find_marker(guild, thread, marker)
                if not found:
                    raise ClubError('Отправка части эссе не подтверждена. Проверьте /club diagnose и /club repair; повторная копия не создана.')
                _, message = found
            if message is None:
                result = await self.service.upsert(guild, key, thread.id, content, files=files)
                if (result['guild_id'] != guild.id or result['channel_id'] != thread.id
                        or result['webhook_id'] is not None or not result['message_id']):
                    raise ClubError('Привязка новой копии бота не подтверждена. Прежние сообщения сохранены.')
                message = await thread.fetch_message(result['message_id'])
        else:
            if pub and pub['webhook_id'] is None:
                raise ClubError('Эта часть была отправлена ботом. Для смены автора используйте /club import restyle.')
            if pub:
                hook = await self.service.bot.fetch_webhook(pub['webhook_id'])
                if not self.service.valid_essay_webhook(hook, guild, thread.parent_id):
                    raise ClubError('Вебхук сохранённой части эссе принадлежит другому форуму или приложению.')
            else:
                forum = await self.service.fresh_forum(guild, 'essays')
                hook = await self.service.essay_webhook(guild, forum)
            if message is None:
                if pub:
                    found = await self.service._find_marker(guild, thread, marker, webhook_id=hook.id)
                    if not found:
                        raise ClubError('Отправка части эссе не подтверждена. Проверьте /club diagnose и /club repair; повторная копия не создана.')
                    _, message = found
                else:
                    if thread.archived:
                        thread = await thread.edit(archived=False)
                    self.store.reserve_publication(key, guild.id, thread.id, webhook_id=hook.id)
                    try:
                        message = await hook.send(content + marker, thread=discord.Object(id=thread.id),
                                                  username=member.display_name[:80], avatar_url=str(member.display_avatar.url),
                                                  files=files or discord.utils.MISSING, wait=True, allowed_mentions=NO_MENTIONS)
                    except discord.HTTPException as exc:
                        if exc.status in (400, 401, 403):
                            self.store.forget_publication(key)
                        raise

        def verify(value, *, clean=False):
            if (not self.service.owns_starter(value, hook.id if hook else None)
                    or value.channel.id != thread.id
                    or value.content not in ((content,) if clean else (content, content + marker))):
                raise ClubError('Копия эссе не совпадает с сохранённым текстом и отправителем. Прежние сообщения сохранены.')
            if expected_attachments is not None:
                actual = sorted((a.filename, a.size) for a in value.attachments)
                if actual != sorted(expected_attachments):
                    raise ClubError('Вложения новой копии не подтверждены. Прежние сообщения сохранены.')

        verify(message)
        # Keep the recovery marker until the acknowledged Discord ID is durable.
        # A crash after this save resumes by ID, including a lost edit response;
        # a crash before it can still find the original send by its marker.
        digest = hashlib.sha256(content.encode()).hexdigest()
        self.store.save_publication(key, thread.id, message.id, digest)
        if message.content != content:
            if thread.archived:
                thread = await thread.edit(archived=False)
            if hook:
                await hook.edit_message(message.id, thread=discord.Object(id=thread.id), content=content,
                                        allowed_mentions=NO_MENTIONS)
            else:
                await message.edit(content=content, allowed_mentions=NO_MENTIONS)
            verify(await thread.fetch_message(message.id), clean=True)
        return self.store.publication(key)

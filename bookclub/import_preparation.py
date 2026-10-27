"""Human archive selection and complete thread metadata, without provider calls.

Selections use Discord IDs, so renaming a source thread never changes its book
mapping. This module neither reserves budgets nor changes completed import runs.
"""
from __future__ import annotations

import asyncio
import json

import discord

from .store import ClubError, checked_text


def _id(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise ClubError('Укажите числовой ID канала или треда Discord.')
    return value


class PreparationStore:
    def __init__(self, store):
        self.db = store

    @staticmethod
    def _selection(row):
        result = dict(row)
        result['id'] = result['thread_id'] = str(result['thread_id'])
        result['source_id'] = str(result['source_id'])
        return result

    def selections(self, guild_id, source_id):
        return [self._selection(row) for row in self.db.rows(
            'SELECT * FROM bc_import_selections WHERE guild_id=? AND source_id=? ORDER BY thread_id DESC',
            (guild_id, source_id))]

    def revision(self, guild_id, source_id):
        row = self.db.one('SELECT revision FROM bc_import_preparation WHERE guild_id=? AND source_id=?',
                          (guild_id, source_id))
        return row['revision'] if row else 0

    def save(self, guild_id, source_id, thread_id, decision, actor_id, *, title=None, author=None, book_id=None):
        """A decision change invalidates outstanding cursors, including after restart."""
        if decision not in {'pending', 'included', 'excluded'}:
            raise ClubError('Решение: included, excluded или pending.')
        with self.db.tx() as db:
            previous = db.execute('SELECT * FROM bc_import_selections WHERE guild_id=? AND source_id=? AND thread_id=?',
                                  (guild_id, source_id, thread_id)).fetchone()
            values = dict(decision=decision, title=title, author=author, book_id=book_id)
            if previous is not None and all(previous[key] == value for key, value in values.items()):
                return self._selection(previous)
            db.execute('''INSERT INTO bc_import_selections
                (guild_id,source_id,thread_id,decision,title,author,book_id,actor_id,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(guild_id,source_id,thread_id) DO UPDATE SET
                decision=excluded.decision,title=excluded.title,author=excluded.author,
                book_id=excluded.book_id,actor_id=excluded.actor_id,updated_at=excluded.updated_at''',
                (guild_id, source_id, thread_id, decision, title, author, book_id, actor_id, self.db.clock()))
            db.execute('''INSERT INTO bc_import_preparation(guild_id,source_id,revision) VALUES(?,?,1)
                ON CONFLICT(guild_id,source_id) DO UPDATE SET revision=revision+1''', (guild_id, source_id))
            # Coverage describes the current selection, not an earlier book mapping.
            db.execute('DELETE FROM bc_import_coverage WHERE guild_id=? AND source_id=? AND thread_id=?',
                       (guild_id, source_id, thread_id))
            return self._selection(db.execute(
                'SELECT * FROM bc_import_selections WHERE guild_id=? AND source_id=? AND thread_id=?',
                (guild_id, source_id, thread_id)).fetchone())

    def mark_coverage(self, guild_id, source_id, thread_id, state):
        with self.db.tx() as db:
            self._mark_coverage(db, guild_id, source_id, thread_id, state)

    def _mark_coverage(self, db, guild_id, source_id, thread_id, state):
        rank = {'unread': 0, 'limited': 1, 'partial': 2, 'complete': 3}
        if state not in rank:
            raise ClubError('Некорректный статус просмотра треда.')
        previous = db.execute('SELECT state FROM bc_import_coverage WHERE guild_id=? AND source_id=? AND thread_id=?',
                              (guild_id, source_id, thread_id)).fetchone()
        if previous is not None and rank[previous['state']] >= rank[state]:
            return
        db.execute('''INSERT INTO bc_import_coverage(guild_id,source_id,thread_id,state,updated_at)
            VALUES(?,?,?,?,?) ON CONFLICT(guild_id,source_id,thread_id) DO UPDATE SET
            state=excluded.state,updated_at=excluded.updated_at''',
            (guild_id, source_id, thread_id, state, self.db.clock()))

    def coverage(self, guild_id, source_id):
        return {str(row['thread_id']): row['state'] for row in self.db.rows(
            'SELECT thread_id,state FROM bc_import_coverage WHERE guild_id=? AND source_id=?',
            (guild_id, source_id))}

    def save_cursor(self, guild_id, source_id, token, payload):
        if not isinstance(token, str) or not token or len(token) > 128 or not isinstance(payload, dict):
            raise ClubError('Некорректное продолжение предпросмотра.')
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        with self.db.tx() as db:
            db.execute('''INSERT INTO bc_import_preview_cursors(id,guild_id,source_id,payload,created_at)
                VALUES(?,?,?,?,?)''', (token, guild_id, source_id, encoded, self.db.clock()))

    def load_cursor(self, guild_id, source_id, token):
        row = self.db.one('SELECT payload FROM bc_import_preview_cursors WHERE id=? AND guild_id=? AND source_id=?',
                          (token, guild_id, source_id))
        if row is None:
            raise ClubError('Продолжение предпросмотра не найдено для этого сервера и источника.')
        return json.loads(row['payload'])

    def finalize(self, guild_id, source_id, expected_revision, coverage, *, token=None, payload=None):
        """Persist a bounded page atomically unless human selections changed."""
        if (token is None) != (payload is None):
            raise ClubError('Некорректное продолжение предпросмотра.')
        if token is not None and (not isinstance(token, str) or not token or len(token) > 128
                                  or not isinstance(payload, dict)):
            raise ClubError('Некорректное продолжение предпросмотра.')
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False) if payload is not None else None
        with self.db.tx() as db:
            row = db.execute('SELECT revision FROM bc_import_preparation WHERE guild_id=? AND source_id=?',
                              (guild_id, source_id)).fetchone()
            if (row['revision'] if row else 0) != expected_revision:
                raise ClubError('Выбор книг изменился во время предпросмотра. Начните новый preview.')
            if token is not None:
                db.execute('''INSERT INTO bc_import_preview_cursors(id,guild_id,source_id,payload,created_at)
                    VALUES(?,?,?,?,?)''', (token, guild_id, source_id, encoded, self.db.clock()))
            for thread_id, state in coverage.items():
                self._mark_coverage(db, guild_id, source_id, thread_id, state)

    def imported_counts(self, guild_id, source_id):
        """Resolve ORIGINAL channel IDs from immutable snapshots, never target IDs.

        A positive count means some source messages have already been published;
        it does not claim all discussion messages were imported or reviewed.
        """
        runs = self.db.rows('''SELECT DISTINCT r.id,r.snapshot FROM bc_import_runs r
            JOIN bc_import_sources s ON s.run_id=r.id AND s.guild_id=r.guild_id
            WHERE r.guild_id=? AND r.source_channel_id=? AND s.thread_id IS NOT NULL''',
            (guild_id, source_id))
        bindings = {str(row['source_id']) for row in self.db.rows(
            'SELECT source_id FROM bc_import_sources WHERE guild_id=? AND thread_id IS NOT NULL', (guild_id,))}
        counts, seen = {}, set()
        for run in runs:
            for message in json.loads(run['snapshot']).get('messages', []):
                ident, channel = str(message['id']), str(message['channel_id'])
                if ident in bindings and ident not in seen:
                    seen.add(ident)
                    counts[channel] = counts.get(channel, 0) + 1
        return counts


class ImportPreparation:
    def __init__(self, importer):
        self.importer = importer
        self.store = PreparationStore(importer.store)

    async def _context(self, guild, actor_id, source_id):
        _id(source_id)
        actor = await self.importer.guard(guild, actor_id, source_id)
        source = await self.importer.source(guild, actor, source_id)
        bot_member = await guild.fetch_member(self.importer.service.bot.user.id)
        return actor, source, bot_member

    @staticmethod
    def _public(thread, guild, source):
        return (isinstance(thread, discord.Thread) and thread.guild.id == guild.id
                and thread.parent_id == source.id and not thread.is_private())

    @staticmethod
    def _readable(thread, actor, bot_member):
        return all((perms := thread.permissions_for(member)).view_channel and perms.read_message_history
                   for member in (actor, bot_member))

    async def select(self, guild, actor_id, source_id, thread_id, decision,
                     title=None, author=None, book_id=None, confirm=False):
        _id(thread_id)
        actor, source, bot_member = await self._context(guild, actor_id, source_id)
        thread = await self.importer.service.bot.fetch_channel(thread_id)
        if not self._public(thread, guild, source):
            raise ClubError('Выберите публичный тред внутри разрешённого исходного канала.')
        if not self._readable(thread, actor, bot_member):
            raise ClubError('У вас или бота нет доступа к истории выбранного треда.')
        if not confirm:
            raise ClubError('Подтвердите выбор и метаданные: confirm:true.')
        if decision not in {'pending', 'included', 'excluded'}:
            raise ClubError('Решение: included, excluded или pending.')
        if decision == 'included':
            if book_id is not None:
                if title is not None or author is not None:
                    raise ClubError('Укажите либо существующую книгу, либо название и автора.')
                book = self.importer.store.require_active_book(guild.id, book_id)
                book_id, title, author = book['id'], book['title'], book['author']
            else:
                if not isinstance(title, str) or not isinstance(author, str):
                    raise ClubError('Подтвердите отдельно название и автора книги либо выберите существующую книгу.')
                title = checked_text(title, 'Название книги', 180)
                author = checked_text(author, 'Автор книги', 180)
        else:
            if title is not None or author is not None or book_id is not None:
                raise ClubError('Метаданные книги задаются только для включённого треда.')
            title = author = book_id = None
        return self.store.save(guild.id, source_id, thread_id, decision, actor_id,
                               title=title, author=author, book_id=book_id)

    async def inventory(self, guild, actor_id, source_id):
        actor, source, bot_member = await self._context(guild, actor_id, source_id)
        found, warnings, complete = {}, [], True
        try:
            for thread in await guild.active_threads():
                if self._public(thread, guild, source):
                    found[thread.id] = thread
        except (discord.HTTPException, asyncio.TimeoutError):
            complete = False
            warnings.append('Не удалось получить все активные публичные треды; перечень неполный.')
        try:
            # discord.py's iterator exhausts every archive API page; model caps
            # do not apply to this metadata-only inventory.
            async for thread in source.archived_threads(limit=None):
                if self._public(thread, guild, source):
                    found[thread.id] = thread
        except (discord.HTTPException, asyncio.TimeoutError):
            complete = False
            warnings.append('Не удалось получить все архивные публичные треды; перечень неполный.')
        selections = {row['thread_id']: row for row in self.store.selections(guild.id, source_id)}
        imported = self.store.imported_counts(guild.id, source_id)
        coverage = self.store.coverage(guild.id, source_id)
        entries = []
        for ident, listed in sorted(found.items(), reverse=True):
            access, source_title, access_error = False, None, None
            thread = listed
            try:
                thread = await self.importer.service.bot.fetch_channel(ident)
                access = self._public(thread, guild, source) and self._readable(thread, actor, bot_member)
                if not access:
                    access_error = 'Нет доступа к истории треда у участника или бота.'
                elif isinstance(source, discord.TextChannel):
                    try:
                        starter = await source.fetch_message(ident)
                        source_title = starter.content.splitlines()[0][:2000] if starter.content else ''
                    except (discord.HTTPException, asyncio.TimeoutError):
                        access_error = 'Стартовое сообщение недоступно; название нужно проверить вручную.'
            except (discord.HTTPException, asyncio.TimeoutError):
                access_error = 'Не удалось проверить текущий доступ к треду.'
            key = str(ident)
            selection = selections.get(key, {})
            # Existing imported sources are closed by default; a human may
            # explicitly include them to inspect remaining, unclaimed messages.
            decision = selection.get('decision', 'excluded' if imported.get(key) else 'pending')
            capture_state = coverage.get(key, 'unread')
            status = ('already_imported' if imported.get(key) and not selection else decision)
            if decision == 'included' and capture_state == 'limited':
                status = 'not_viewed_due_to_limit'
            entry = dict(id=key, name=str(thread.name), archived=bool(thread.archived),
                         decision=decision, title=selection.get('title'), author=selection.get('author'),
                         book_id=selection.get('book_id'), imported_messages=imported.get(key, 0),
                         capture_state=capture_state, access=access, status=status, source_title=source_title)
            if access_error:
                entry['access_error'] = access_error
            entries.append(entry)
        missing = set(selections) - {str(ident) for ident in found}
        if missing:
            complete = False
            warnings.append('Ранее выбранные треды не найдены в текущем перечне: ' + ', '.join(sorted(missing, key=int)))
        return dict(source_id=str(source_id), complete=complete, threads=entries, warnings=warnings,
                    revision=self.store.revision(guild.id, source_id),
                    active_count=sum(not entry['archived'] for entry in entries),
                    archived_count=sum(entry['archived'] for entry in entries))

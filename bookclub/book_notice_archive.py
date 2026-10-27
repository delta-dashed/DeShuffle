"""Durable archive restoration for the removed-book publication only."""
from __future__ import annotations

import json

from .store import ClubError


FIELD = '_book_notice_archive'


def _binding(db, guild_id, book_id, publication):
    book = db.execute('SELECT deleted FROM bc_books WHERE guild_id=? AND id=?',
                      (guild_id, book_id)).fetchone()
    current = db.execute('SELECT * FROM bc_publications WHERE guild_id=? AND key=?',
                         (guild_id, f'book:{book_id}')).fetchone()
    if (not book or not book['deleted'] or not current
            or any(current[key] != publication[key] for key in ('key', 'channel_id', 'message_id'))):
        raise ClubError('Привязка удалённой книги изменилась. Повторите обновление карточки.')


def _journal(db, guild_id, book_id):
    operation = db.execute('''SELECT rowid AS journal_id,after_state AS payload
      FROM bc_book_removal_operations WHERE guild_id=? AND book_id=?
      ORDER BY created_at DESC,rowid DESC LIMIT 1''', (guild_id, book_id)).fetchone()
    book = db.execute('SELECT revision FROM bc_books WHERE guild_id=? AND id=?',
                      (guild_id, book_id)).fetchone()
    if operation and json.loads(operation['payload']).get('source', {}).get('revision') == book['revision']:
        return 'bc_book_removal_operations', 'after_state', operation
    # The older reversible-removal API also has a durable audit row. Only
    # execution metadata is added; original book fields remain unchanged.
    audit = db.execute('''SELECT id AS journal_id,previous_state AS payload
      FROM bc_book_trash_audit WHERE guild_id=? AND book_id=? AND action='remove'
      ORDER BY id DESC LIMIT 1''', (guild_id, book_id)).fetchone()
    if not audit:
        raise ClubError('Журнал удаления книги не найден. Карточка сохранена без изменения архива.')
    return 'bc_book_trash_audit', 'previous_state', audit


def remember_notice_archive(store, guild_id, book_id, publication, archived):
    """Commit the first state before opening the thread, including on retry."""
    if type(archived) is not bool:
        raise ValueError('Archive state must be a boolean')
    with store.tx() as db:
        _binding(db, guild_id, book_id, publication)
        table, column, row = _journal(db, guild_id, book_id)
        data = json.loads(row['payload'])
        pending = data.get(FIELD)
        if pending is None:
            pending = dict(channel_id=publication['channel_id'], message_id=publication['message_id'],
                           original_archived=archived)
            data[FIELD] = pending
            db.execute(f'UPDATE {table} SET {column}=? WHERE rowid=?',
                       (json.dumps(data, ensure_ascii=False, sort_keys=True), row['journal_id']))
        if (not isinstance(pending, dict) or type(pending.get('original_archived')) is not bool
                or any(pending.get(key) != publication[key] for key in ('channel_id', 'message_id'))):
            raise ClubError('Журнал архива удалённой книги не совпадает с её карточкой. Проверьте привязку.')
        return dict(table=table, column=column, journal_id=row['journal_id'], pending=pending)


def finish_notice_archive(store, guild_id, book_id, publication, journal):
    """Forget only the successfully restored attempt, allowing future overrides."""
    with store.tx() as db:
        _binding(db, guild_id, book_id, publication)
        table, column, row = _journal(db, guild_id, book_id)
        if (table, column, row['journal_id']) != (journal['table'], journal['column'], journal['journal_id']):
            raise ClubError('Журнал удаления книги изменился. Повторите обновление карточки.')
        data = json.loads(row['payload'])
        if data.get(FIELD) != journal['pending']:
            raise ClubError('Состояние архива изменилось. Повторите обновление карточки.')
        del data[FIELD]
        # Match the existing JSON serialization of each journal. The frozen
        # confirmation plan is never changed.
        options = {'separators': (',', ':')} if table == 'bc_book_removal_operations' else {}
        db.execute(f'UPDATE {table} SET {column}=? WHERE rowid=?',
                   (json.dumps(data, ensure_ascii=False, sort_keys=True, **options), row['journal_id']))

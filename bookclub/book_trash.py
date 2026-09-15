"""Reversible catalog removal; Discord posts and imported history stay intact."""
from __future__ import annotations

import json
import sqlite3

from .store import ClubError


def migrate(db):
    columns = {row['name'] for row in db.execute('PRAGMA table_info(bc_books)')}
    if 'deleted' not in columns:
        db.execute('ALTER TABLE bc_books ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0 CHECK(deleted IN (0,1))')
    db.execute('DROP INDEX IF EXISTS bc_one_current')
    db.execute("CREATE UNIQUE INDEX bc_one_current ON bc_books(guild_id) WHERE status='reading' AND deleted=0")
    db.execute('''CREATE TABLE IF NOT EXISTS bc_book_trash_audit(
      id INTEGER PRIMARY KEY, book_id TEXT NOT NULL REFERENCES bc_books(id),
      guild_id INTEGER NOT NULL, actor_id INTEGER NOT NULL,
      action TEXT NOT NULL CHECK(action IN ('remove','restore')),
      previous_state TEXT NOT NULL, created_at INTEGER NOT NULL)''')
    db.execute('INSERT INTO bc_migrations(version) VALUES(12)')


def change_book_removal(store, guild_id, book_id, *, removed, expected_revision, actor_id):
    if type(actor_id) is not int or actor_id <= 0:
        raise ClubError('Для удаления и восстановления нужен участник-организатор.')
    if type(expected_revision) is not int:
        raise ClubError('Откройте управление книгой заново перед подтверждением.')
    settings = store.settings(guild_id)
    with store.tx() as db:
        book = store._get(db, 'bc_books', guild_id, book_id)
        if book['revision'] != expected_revision:
            raise ClubError('Книга изменилась. Откройте управление книгой заново.')
        if bool(book['deleted']) == removed:
            return book
        try:
            db.execute('''UPDATE bc_books SET deleted=?,status_automation=0,
              status_automation_pending=0,revision=revision+1 WHERE id=?''', (int(removed), book_id))
        except sqlite3.IntegrityError as exc:
            raise ClubError('Сейчас читается другая книга. Сначала завершите её или верните в очередь, '
                            'затем восстановите удалённую книгу.') from exc
        db.execute('''INSERT INTO bc_book_trash_audit
          (book_id,guild_id,actor_id,action,previous_state,created_at) VALUES(?,?,?,?,?,?)''',
          (book_id, guild_id, actor_id, 'remove' if removed else 'restore',
           json.dumps(book, ensure_ascii=False, sort_keys=True), store.clock()))
        if removed:
            # Sent, failed and uncertain delivery records remain an audit trail.
            # Delivery checks removal before each recipient; a Discord request
            # already in flight may finish, so its record is not rewritten here.
            db.execute('''UPDATE bc_jobs SET state='cancelled' WHERE guild_id=? AND state='pending'
              AND (entity_id=? OR entity_id IN (SELECT id FROM bc_meetings WHERE book_id=?))''',
              (guild_id, book_id, book_id))
        else:
            # New revisions allow only future thresholds to be scheduled again.
            # Old host buttons and plan forms must not become valid on restore.
            for meeting in db.execute('SELECT id FROM bc_meetings WHERE book_id=?', (book_id,)).fetchall():
                db.execute('''UPDATE bc_meetings SET revision=revision+1,
                  host_version=host_version+1 WHERE id=?''', (meeting['id'],))
                db.execute('UPDATE bc_plans SET version=version+1 WHERE meeting_id=?', (meeting['id'],))
                store._schedule(db, meeting['id'], settings)
            store._essay_schedule(db, book_id, settings)
        return store._get(db, 'bc_books', guild_id, book_id)

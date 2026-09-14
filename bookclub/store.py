"""Transactional club state. No Discord I/O and no second calendar.

Event timestamps are a cache of Discord's state, refreshed before dispatch.
All mutations are short BEGIN IMMEDIATE transactions in the existing database.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sqlite3
import uuid
from zoneinfo import ZoneInfo


STATUSES = {"proposed": "Предложено", "queued": "В очереди", "reading": "Читаем", "read": "Прочитано"}
DEFAULTS = dict(timezone="Europe/Moscow", participant_minutes=30, preparation_hours=24,
                escalation_hours=3, offer_hours=24, essay_hours=24, reminder_role=None,
                organizer_roles=[], organizers=[], published=False, essay_webhooks=True)


class ClubError(ValueError):
    """Safe, user-facing domain error."""


def now_ts():
    return int(datetime.now(timezone.utc).timestamp())


def parse_time(value: str, zone: str = "Europe/Moscow") -> int:
    try:
        parsed = datetime.fromisoformat(value.strip())
        if parsed.tzinfo is None:
            local = ZoneInfo(zone)
            a, b = parsed.replace(tzinfo=local, fold=0), parsed.replace(tzinfo=local, fold=1)
            if a.utcoffset() != b.utcoffset():
                raise ValueError("ambiguous local time")
            parsed = a
        return int(parsed.timestamp())
    except (ValueError, KeyError) as exc:
        raise ClubError("Дата: ГГГГ-ММ-ДД ЧЧ:ММ, при необходимости с UTC-смещением.") from exc


def checked_text(value, label, limit, required=True):
    value = value.strip()
    if (required and not value) or len(value) > limit:
        raise ClubError(f"{label}: нужно от {1 if required else 0} до {limit} символов.")
    return value


class Store:
    def __init__(self, path, clock=now_ts):
        self.path, self.clock = str(path), clock
        with self.tx() as db:
            # A namespaced migration ledger avoids touching legacy user_version.
            db.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS bc_migrations(version INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS bc_settings(guild_id INTEGER PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS bc_books(
              id TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, title TEXT NOT NULL, author TEXT NOT NULL,
              materials TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'proposed'
                CHECK(status IN ('proposed','queued','reading','read')),
              position INTEGER NOT NULL DEFAULT 0, deadline INTEGER, revision INTEGER NOT NULL DEFAULT 0,
              request_key TEXT NOT NULL, UNIQUE(guild_id,request_key));
            CREATE UNIQUE INDEX IF NOT EXISTS bc_one_current ON bc_books(guild_id) WHERE status='reading';
            CREATE TABLE IF NOT EXISTS bc_participants(
              book_id TEXT NOT NULL REFERENCES bc_books(id), user_id INTEGER NOT NULL,
              willing INTEGER NOT NULL DEFAULT 0, present INTEGER NOT NULL DEFAULT 1,
              PRIMARY KEY(book_id,user_id));
            CREATE TABLE IF NOT EXISTS bc_meetings(
              id TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, book_id TEXT NOT NULL REFERENCES bc_books(id),
              event_id INTEGER UNIQUE, name TEXT NOT NULL, part TEXT NOT NULL, chapter TEXT NOT NULL,
              start INTEGER, end INTEGER, voice_id INTEGER, status TEXT NOT NULL DEFAULT 'draft',
              revision INTEGER NOT NULL DEFAULT 0, host_id INTEGER,
              host_state TEXT NOT NULL DEFAULT 'none', offer_until INTEGER,
              host_version INTEGER NOT NULL DEFAULT 0, plan_generation INTEGER NOT NULL DEFAULT 0,
              exhausted INTEGER NOT NULL DEFAULT 0, request_key TEXT NOT NULL,
              UNIQUE(guild_id,request_key));
            CREATE TABLE IF NOT EXISTS bc_declines(
              meeting_id TEXT NOT NULL REFERENCES bc_meetings(id), user_id INTEGER NOT NULL,
              PRIMARY KEY(meeting_id,user_id));
            CREATE TABLE IF NOT EXISTS bc_absences(
              meeting_id TEXT NOT NULL REFERENCES bc_meetings(id), user_id INTEGER NOT NULL,
              PRIMARY KEY(meeting_id,user_id));
            CREATE TABLE IF NOT EXISTS bc_plans(
              meeting_id TEXT NOT NULL REFERENCES bc_meetings(id), generation INTEGER NOT NULL,
              owner_id INTEGER NOT NULL, topics TEXT NOT NULL DEFAULT '', questions TEXT NOT NULL DEFAULT '',
              excerpts TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
              ready INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY(meeting_id,generation));
            CREATE TABLE IF NOT EXISTS bc_host_history(
              meeting_id TEXT PRIMARY KEY REFERENCES bc_meetings(id), guild_id INTEGER NOT NULL,
              user_id INTEGER NOT NULL, completed_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS bc_essays(
              guild_id INTEGER NOT NULL, source_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
              book_id TEXT NOT NULL REFERENCES bc_books(id), author_id INTEGER NOT NULL,
              title TEXT NOT NULL, url TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY(guild_id,source_id));
            CREATE TABLE IF NOT EXISTS bc_jobs(
              key TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, entity_id TEXT NOT NULL,
              revision INTEGER NOT NULL, kind TEXT NOT NULL, target INTEGER NOT NULL,
              due INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending', detail TEXT);
            CREATE INDEX IF NOT EXISTS bc_jobs_due ON bc_jobs(state,due);
            CREATE TABLE IF NOT EXISTS bc_publications(
              key TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, channel_id INTEGER,
              message_id INTEGER, state TEXT NOT NULL DEFAULT 'reserved', content_hash TEXT);
            INSERT OR IGNORE INTO bc_migrations VALUES(1);
            """)
            if not db.execute("SELECT 1 FROM bc_migrations WHERE version=2").fetchone():
                columns = {row["name"] for row in db.execute("PRAGMA table_info(bc_essays)")}
                for name, default in (("managed", 0), ("submitted", 1)):
                    if name not in columns:
                        db.execute(f"ALTER TABLE bc_essays ADD COLUMN {name} INTEGER NOT NULL DEFAULT {default}")
                db.execute("INSERT INTO bc_migrations(version) VALUES(2)")
            if not db.execute("SELECT 1 FROM bc_migrations WHERE version=3").fetchone():
                columns = {row["name"] for row in db.execute("PRAGMA table_info(bc_publications)")}
                if "webhook_id" not in columns:
                    db.execute("ALTER TABLE bc_publications ADD COLUMN webhook_id INTEGER")
                db.execute("""CREATE TABLE IF NOT EXISTS bc_webhooks(
                  guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, webhook_id INTEGER,
                  PRIMARY KEY(guild_id,channel_id))""")
                db.execute("INSERT INTO bc_migrations(version) VALUES(3)")
            if not db.execute("SELECT 1 FROM bc_migrations WHERE version=4").fetchone():
                db.execute("""CREATE TABLE IF NOT EXISTS bc_setup_resources(
                  guild_id INTEGER NOT NULL, purpose TEXT NOT NULL, token TEXT NOT NULL,
                  channel_id INTEGER, managed INTEGER NOT NULL DEFAULT 1,
                  state TEXT NOT NULL DEFAULT 'reserved', PRIMARY KEY(guild_id,purpose))""")
                db.execute("""CREATE TABLE IF NOT EXISTS bc_config_imports(
                  guild_id INTEGER PRIMARY KEY, data TEXT NOT NULL)""")
                db.execute("INSERT INTO bc_migrations(version) VALUES(4)")

    @contextmanager
    def tx(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def rows(self, sql, args=()):
        with self.tx() as db:
            return [dict(r) for r in db.execute(sql, args)]

    def one(self, sql, args=()):
        rows = self.rows(sql, args)
        return rows[0] if rows else None

    def settings(self, guild_id):
        row = self.one("SELECT data FROM bc_settings WHERE guild_id=?", (guild_id,))
        if not row:
            raise ClubError("Клуб на этом сервере ещё не настроен.")
        return {**DEFAULTS, **json.loads(row["data"])}

    def configure(self, guild_id, values):
        data = {**DEFAULTS, **values}
        ZoneInfo(data["timezone"])
        if type(data["essay_webhooks"]) is not bool:
            raise ClubError("essay_webhooks: нужно true или false.")
        for key in ("news", "chat", "books", "essays", "voice"):
            if not isinstance(data.get(key), int) or data[key] <= 0:
                raise ClubError(f"В настройке нужен ID канала {key}.")
        if len({data[k] for k in ("news", "chat", "books", "essays", "voice")}) != 5:
            raise ClubError("Для пяти назначений нужны разные каналы.")
        for key in ("participant_minutes", "preparation_hours", "escalation_hours", "offer_hours", "essay_hours"):
            if not isinstance(data[key], (int, float)) or not 0 < data[key] <= 10080:
                raise ClubError(f"Некорректный интервал {key}.")
        for key in ("organizer_roles", "organizers"):
            if not isinstance(data[key], list) or any(type(x) is not int or x <= 0 for x in data[key]):
                raise ClubError(f"{key}: нужен список положительных ID.")
        if data["reminder_role"] is not None and (type(data["reminder_role"]) is not int or data["reminder_role"] <= 0):
            raise ClubError("reminder_role: нужен ID роли или null.")
        with self.tx() as db:
            old = db.execute("SELECT data FROM bc_settings WHERE guild_id=?", (guild_id,)).fetchone()
            if old:
                data["published"] = json.loads(old[0]).get("published", False)
                data["scan_after"] = json.loads(old[0]).get("scan_after", self.clock())
                if json.loads(old[0]) == data:
                    return
            else:
                data["published"] = False  # Publication is an explicit Discord command.
                data["scan_after"] = self.clock()
            db.execute("INSERT OR REPLACE INTO bc_settings VALUES(?,?)", (guild_id, json.dumps(data)))
            # Settings affect deadlines too; new generations invalidate all old pending jobs.
            for m in db.execute("SELECT * FROM bc_meetings WHERE guild_id=?", (guild_id,)).fetchall():
                db.execute("UPDATE bc_meetings SET revision=revision+1 WHERE id=?", (m["id"],))
                self._schedule(db, m["id"], data)
            for b in db.execute("SELECT id FROM bc_books WHERE guild_id=?", (guild_id,)).fetchall():
                db.execute("UPDATE bc_books SET revision=revision+1 WHERE id=?", (b[0],))
                self._essay_schedule(db, b[0], data)

    def set_published(self, guild_id):
        data = self.settings(guild_id)
        data["published"] = True
        with self.tx() as db:
            db.execute("UPDATE bc_settings SET data=? WHERE guild_id=?", (json.dumps(data), guild_id))

    def import_config(self, guild_id, values):
        """Apply file edits without undoing channels repaired by /club setup.

        The import snapshot contains only external values, never the merged
        runtime settings. None means no external configuration is available.
        """
        if values is None:
            return
        snapshot = json.dumps(values)
        channel_keys = {"news", "chat", "books", "essays", "voice"}
        replaced_channels = set()
        previous = self.one("SELECT data FROM bc_config_imports WHERE guild_id=?", (guild_id,))
        if previous:
            previous = json.loads(previous["data"])
            merged = self.settings(guild_id)
            for key in previous.keys() - values.keys():
                merged.pop(key, None)
            for key, value in values.items():
                # JSON comparison distinguishes false from 0, including list
                # members, so changed invalid inputs still reach validation.
                if (key not in previous
                        or json.dumps(previous[key], sort_keys=True) != json.dumps(value, sort_keys=True)):
                    merged[key] = value
                    if key in channel_keys:
                        replaced_channels.add(key)
        else:
            merged = values
            replaced_channels = {
                resource["purpose"] for resource in self.setup_resources(guild_id)
                if resource["purpose"] in channel_keys
                and resource["purpose"] in values
                and resource["channel_id"] != values[resource["purpose"]]
            }
        # configure validates the complete configuration and keeps publication
        # state; failed imports must not advance the external snapshot.
        self.configure(guild_id, merged)
        with self.tx() as db:
            db.execute("INSERT OR REPLACE INTO bc_config_imports(guild_id,data) VALUES(?,?)",
                       (guild_id, snapshot))
            # Explicit file changes supersede even an interrupted setup's
            # created binding. Forget only the journal, never Discord channels.
            db.executemany("DELETE FROM bc_setup_resources WHERE guild_id=? AND purpose=?",
                           ((guild_id, purpose) for purpose in replaced_channels))

    def setup_resource(self, guild_id, purpose):
        return self.one("SELECT * FROM bc_setup_resources WHERE guild_id=? AND purpose=?",
                        (guild_id, purpose))

    def setup_resources(self, guild_id):
        return self.rows("SELECT * FROM bc_setup_resources WHERE guild_id=? ORDER BY purpose", (guild_id,))

    def reserve_setup_resource(self, guild_id, purpose):
        with self.tx() as db:
            return db.execute("""INSERT OR IGNORE INTO bc_setup_resources(guild_id,purpose,token)
              VALUES(?,?,?)""", (guild_id, purpose, uuid.uuid4().hex)).rowcount == 1

    def bind_setup_resource(self, guild_id, purpose, channel_id, *, managed=True, state="created"):
        with self.tx() as db:
            db.execute("""INSERT INTO bc_setup_resources(guild_id,purpose,token,channel_id,managed,state)
              VALUES(?,?,?,?,?,?) ON CONFLICT(guild_id,purpose) DO UPDATE SET
              channel_id=excluded.channel_id,managed=excluded.managed,state=excluded.state""",
              (guild_id, purpose, uuid.uuid4().hex, channel_id, int(managed), state))

    def ready_setup_resource(self, guild_id, purpose):
        with self.tx() as db:
            db.execute("UPDATE bc_setup_resources SET state='ready' WHERE guild_id=? AND purpose=?",
                       (guild_id, purpose))

    def forget_setup_resource(self, guild_id, purpose):
        with self.tx() as db:
            db.execute("DELETE FROM bc_setup_resources WHERE guild_id=? AND purpose=?", (guild_id, purpose))

    def _get(self, db, table, guild_id, ident):
        if table not in ("bc_books", "bc_meetings"):
            raise ValueError(table)
        row = db.execute(f"SELECT * FROM {table} WHERE guild_id=? AND id=?", (guild_id, ident)).fetchone()
        if not row:
            raise ClubError("Книга или встреча не найдена на этом сервере.")
        return dict(row)

    def book(self, guild_id, ident):
        with self.tx() as db:
            return self._get(db, "bc_books", guild_id, ident)

    def meeting(self, guild_id, ident):
        with self.tx() as db:
            return self._get(db, "bc_meetings", guild_id, ident)

    def books(self, guild_id):
        return self.rows("SELECT * FROM bc_books WHERE guild_id=? ORDER BY position,id", (guild_id,))

    def create_book(self, guild_id, title, author, materials, request_key):
        self.settings(guild_id)
        title = checked_text(title, "Название", 180)
        author = checked_text(author, "Автор", 180)
        materials = checked_text(materials, "Материалы", 4000, False)
        with self.tx() as db:
            db.execute("""INSERT OR IGNORE INTO bc_books(id,guild_id,title,author,materials,position,request_key)
                       VALUES(?,?,?,?,?,(SELECT COALESCE(MAX(position),0)+1 FROM bc_books WHERE guild_id=?),?)""",
                       (uuid.uuid4().hex, guild_id, title, author, materials, guild_id, str(request_key)))
            return dict(db.execute("SELECT * FROM bc_books WHERE guild_id=? AND request_key=?", (guild_id, str(request_key))).fetchone())

    def update_book(self, guild_id, book_id, **fields):
        if not fields or not set(fields) <= {"status", "position", "deadline", "title", "author", "materials"}:
            raise ClubError("Неизвестное изменение книги.")
        if "status" in fields and fields["status"] not in STATUSES:
            raise ClubError("Неизвестный статус книги.")
        for name, limit in (("title", 180), ("author", 180), ("materials", 4000)):
            if name in fields:
                fields[name] = checked_text(fields[name], name, limit, name != "materials")
        settings = self.settings(guild_id)
        with self.tx() as db:
            self._get(db, "bc_books", guild_id, book_id)
            try:
                db.execute("UPDATE bc_books SET " + ",".join(f"{k}=?" for k in fields) + ",revision=revision+1 WHERE id=?", (*fields.values(), book_id))
            except sqlite3.IntegrityError as exc:
                raise ClubError("Сначала завершите или верните в очередь текущее чтение.") from exc
            self._essay_schedule(db, book_id, settings)

    def participant(self, guild_id, book_id, user_id, *, joined=True, willing=False):
        settings = self.settings(guild_id)
        with self.tx() as db:
            self._get(db, "bc_books", guild_id, book_id)
            db.execute("INSERT INTO bc_participants VALUES(?,?,?,?) ON CONFLICT(book_id,user_id) DO UPDATE SET willing=excluded.willing,present=excluded.present", (book_id, user_id, int(willing and joined), int(joined)))
            if not joined:
                meetings = db.execute("SELECT id FROM bc_meetings WHERE book_id=? AND host_id=? AND status IN ('scheduled','active')", (book_id, user_id)).fetchall()
                for m in meetings:
                    self._clear_host(db, m[0])
                    self._schedule(db, m[0], settings)
            self._essay_schedule(db, book_id, settings)

    def participants(self, book_id):
        return self.rows("SELECT * FROM bc_participants WHERE book_id=? AND present=1 ORDER BY user_id", (book_id,))

    def _eligible(self, db, m, user_id):
        if not db.execute("SELECT 1 FROM bc_participants WHERE book_id=? AND user_id=? AND present=1", (m["book_id"], user_id)).fetchone():
            raise ClubError("Ведущий должен участвовать в этом чтении и состоять на сервере.")
        if db.execute("SELECT 1 FROM bc_absences WHERE meeting_id=? AND user_id=?", (m["id"], user_id)).fetchone():
            raise ClubError("Участник отметил отсутствие на этой встрече.")

    def attendance(self, guild_id, meeting_id, user_id, absent):
        settings = self.settings(guild_id)
        with self.tx() as db:
            m = self._get(db, "bc_meetings", guild_id, meeting_id)
            if not db.execute("SELECT 1 FROM bc_participants WHERE book_id=? AND user_id=? AND present=1", (m["book_id"], user_id)).fetchone():
                raise ClubError("Сначала присоединитесь к чтению.")
            if absent:
                db.execute("INSERT OR IGNORE INTO bc_absences VALUES(?,?)", (meeting_id, user_id))
                if m["host_id"] == user_id and m["status"] == "scheduled":
                    self._clear_host(db, meeting_id)
                    self._schedule(db, meeting_id, settings)
            else:
                db.execute("DELETE FROM bc_absences WHERE meeting_id=? AND user_id=?", (meeting_id, user_id))

    def meeting_participants(self, meeting_id):
        return self.rows("""SELECT p.* FROM bc_participants p JOIN bc_meetings m ON p.book_id=m.book_id
          WHERE m.id=? AND p.present=1 AND NOT EXISTS(
          SELECT 1 FROM bc_absences a WHERE a.meeting_id=m.id AND a.user_id=p.user_id) ORDER BY p.user_id""", (meeting_id,))

    def draft_meeting(self, guild_id, book_id, name, part, chapter, request_key):
        name = checked_text(name, "Название встречи", 100)
        part = checked_text(part, "Часть", 200)
        chapter = checked_text(chapter, "Последняя глава", 250)
        with self.tx() as db:
            self._get(db, "bc_books", guild_id, book_id)
            db.execute("INSERT OR IGNORE INTO bc_meetings(id,guild_id,book_id,name,part,chapter,request_key) VALUES(?,?,?,?,?,?,?)", (uuid.uuid4().hex, guild_id, book_id, name, part, chapter, str(request_key)))
            return dict(db.execute("SELECT * FROM bc_meetings WHERE guild_id=? AND request_key=?", (guild_id, str(request_key))).fetchone())

    def sync_event(self, guild_id, meeting_id, *, event_id, name, start, end, voice_id, status):
        settings = self.settings(guild_id)
        with self.tx() as db:
            m = self._get(db, "bc_meetings", guild_id, meeting_id)
            if m["event_id"] is not None and m["event_id"] != event_id:
                raise ClubError("Встреча уже связана с другим событием.")
            values = dict(event_id=event_id, name=name, start=start, end=end, voice_id=voice_id, status=status)
            if all(m[k] == v for k, v in values.items()):
                return False
            moved = m["start"] is not None and any(m[k] != values[k] for k in ("start", "end", "voice_id"))
            db.execute("UPDATE bc_meetings SET " + ",".join(f"{k}=?" for k in values) + ",revision=revision+1 WHERE id=?", (*values.values(), meeting_id))
            if moved and m["host_id"] and status == "scheduled" and start > self.clock():
                until = min(start, self.clock() + int(settings["offer_hours"] * 3600))
                db.execute("UPDATE bc_meetings SET host_state='pending',host_version=host_version+1,offer_until=? WHERE id=?", (until, meeting_id))
                db.execute("UPDATE bc_plans SET ready=0,version=version+1 WHERE meeting_id=? AND generation=?", (meeting_id, m["plan_generation"]))
                fresh = dict(db.execute("SELECT * FROM bc_meetings WHERE id=?", (meeting_id,)).fetchone())
                self._job(db, fresh, "rescheduled", m["host_id"], self.clock())
            if status == "completed" and m["status"] == "active" and m["host_state"] == "confirmed":
                db.execute("INSERT OR IGNORE INTO bc_host_history VALUES(?,?,?,?)", (meeting_id, guild_id, m["host_id"], self.clock()))
            self._schedule(db, meeting_id, settings)
            return True

    def _job(self, db, m, kind, target, due):
        key = f'{m["id"]}:{m["revision"]}:{kind}:{target}'
        db.execute("INSERT OR IGNORE INTO bc_jobs(key,guild_id,entity_id,revision,kind,target,due) VALUES(?,?,?,?,?,?,?)", (key, m["guild_id"], m["id"], m["revision"], kind, target, int(due)))

    def _schedule(self, db, meeting_id, settings):
        m = dict(db.execute("SELECT * FROM bc_meetings WHERE id=?", (meeting_id,)).fetchone())
        # Renaming an event or changing settings revises its reminder jobs, but
        # must not lose a host invitation that is still waiting for delivery.
        # Keep its original due time; a revision must not revive stale notices.
        if (m["status"] == "scheduled" and m["start"] and m["start"] > self.clock()
                and m["host_state"] == "pending" and (m["offer_until"] or 0) > self.clock()):
            current_notice = db.execute("""SELECT 1 FROM bc_jobs WHERE entity_id=? AND revision=?
              AND kind IN ('offer','rescheduled')""", (meeting_id, m["revision"])).fetchone()
            previous_notice = db.execute("""SELECT * FROM bc_jobs WHERE entity_id=? AND revision<>?
              AND kind IN ('offer','rescheduled') AND target=? AND state='pending'
              ORDER BY revision DESC LIMIT 1""", (meeting_id, m["revision"], m["host_id"])).fetchone()
            if previous_notice and not current_notice:
                self._job(db, m, previous_notice["kind"], previous_notice["target"], previous_notice["due"])
        db.execute("UPDATE bc_jobs SET state='cancelled' WHERE entity_id=? AND state='pending' AND revision<>?", (meeting_id, m["revision"]))
        if m["status"] != "scheduled" or not m["start"] or m["start"] <= self.clock():
            db.execute("UPDATE bc_jobs SET state='cancelled' WHERE entity_id=? AND state='pending' AND kind<>'rescheduled'", (meeting_id,))
            return
        timings = [("participants", 0, m["start"] - int(settings["participant_minutes"] * 60))]
        if m["host_state"] == "confirmed":
            timings.extend((kind, m["host_id"], m["start"] - int(settings[setting] * 3600)) for kind, setting in (("prepare", "preparation_hours"), ("escalate", "escalation_hours")))
        for kind, target, due in timings:
            if due > self.clock():
                self._job(db, m, kind, target, due)

    def _clear_host(self, db, meeting_id):
        db.execute("UPDATE bc_meetings SET host_id=NULL,host_state='none',offer_until=NULL,host_version=host_version+1,revision=revision+1,plan_generation=plan_generation+1 WHERE id=?", (meeting_id,))

    def host_action(self, guild_id, meeting_id, actor_id, action, *, version, organizer=False, candidate=None, live_ids=()):
        settings = self.settings(guild_id)
        with self.tx() as db:
            m = self._get(db, "bc_meetings", guild_id, meeting_id)
            if m["status"] != "scheduled" or not m["start"] or m["start"] <= self.clock():
                raise ClubError("Назначение доступно только для будущей встречи.")
            if m["host_version"] != version:
                raise ClubError("Назначение изменилось. Откройте карточку заново.")
            if action in ("offer", "pick", "replace") and not organizer:
                raise ClubError("Это действие доступно организатору.")
            if action == "replace":
                self._clear_host(db, meeting_id)
            elif action in ("accept", "decline"):
                if m["host_id"] != actor_id:
                    raise ClubError("Это предложение адресовано другому участнику.")
                self._eligible(db, m, actor_id)
                if m["host_state"] == "pending" and (m["offer_until"] or 0) <= self.clock():
                    raise ClubError("Предложение истекло. Откройте карточку заново.")
                if action == "accept":
                    if m["host_state"] != "pending":
                        raise ClubError("Предложение уже обработано.")
                    db.execute("UPDATE bc_meetings SET host_state='confirmed',offer_until=NULL,host_version=host_version+1,revision=revision+1 WHERE id=?", (meeting_id,))
                else:
                    db.execute("INSERT OR IGNORE INTO bc_declines VALUES(?,?)", (meeting_id, actor_id))
                    self._clear_host(db, meeting_id)
                    self._pick(db, meeting_id, settings, live_ids)
            elif action == "volunteer":
                if m["host_state"] != "none":
                    raise ClubError("У встречи уже есть ведущий или открытое предложение.")
                self._eligible(db, m, actor_id)
                db.execute("UPDATE bc_meetings SET host_id=?,host_state='confirmed',host_version=host_version+1,revision=revision+1,exhausted=0 WHERE id=?", (actor_id, meeting_id))
            elif action in ("offer", "pick"):
                if m["host_state"] != "none":
                    raise ClubError("Сначала снимите прежнее назначение через замену ведущего.")
                if action == "pick":
                    self._pick(db, meeting_id, settings, live_ids)
                else:
                    self._eligible(db, m, candidate)
                    if candidate not in live_ids:
                        raise ClubError("Участник недоступен на сервере.")
                    self._offer(db, m, candidate, settings)
            else:
                raise ClubError("Неизвестное действие.")
            self._schedule(db, meeting_id, settings)
            return dict(db.execute("SELECT * FROM bc_meetings WHERE id=?", (meeting_id,)).fetchone())

    def _offer(self, db, m, user_id, settings):
        until = min(m["start"], self.clock() + int(settings["offer_hours"] * 3600))
        db.execute("UPDATE bc_meetings SET host_id=?,host_state='pending',offer_until=?,host_version=host_version+1,revision=revision+1,exhausted=0 WHERE id=?", (user_id, until, m["id"]))
        fresh = dict(db.execute("SELECT * FROM bc_meetings WHERE id=?", (m["id"],)).fetchone())
        self._job(db, fresh, "offer", user_id, self.clock())

    def _pick(self, db, meeting_id, settings, live_ids):
        m = dict(db.execute("SELECT * FROM bc_meetings WHERE id=?", (meeting_id,)).fetchone())
        candidates = db.execute("""SELECT p.user_id,
          (SELECT COUNT(*) FROM bc_meetings x WHERE x.guild_id=? AND x.id<>? AND x.host_id=p.user_id
           AND x.host_state IN ('confirmed','pending') AND x.status='scheduled' AND x.start>?) AS upcoming,
          COALESCE((SELECT MAX(h.completed_at) FROM bc_host_history h WHERE h.guild_id=? AND h.user_id=p.user_id),0) AS last
          FROM bc_participants p WHERE p.book_id=? AND p.willing=1 AND p.present=1
          AND NOT EXISTS(SELECT 1 FROM bc_declines d WHERE d.meeting_id=? AND d.user_id=p.user_id)
          AND NOT EXISTS(SELECT 1 FROM bc_absences a WHERE a.meeting_id=? AND a.user_id=p.user_id)
          ORDER BY upcoming,last,p.user_id""", (m["guild_id"], meeting_id, self.clock(), m["guild_id"], m["book_id"], meeting_id, meeting_id)).fetchall()
        candidate = next((r["user_id"] for r in candidates if r["user_id"] in live_ids), None)
        if candidate is None:
            db.execute("UPDATE bc_meetings SET exhausted=1 WHERE id=?", (meeting_id,))
        else:
            self._offer(db, m, candidate, settings)

    def expire_offers(self, guild_id, live_ids):
        settings = self.settings(guild_id)
        with self.tx() as db:
            expired = db.execute("SELECT * FROM bc_meetings WHERE guild_id=? AND host_state='pending' AND offer_until<=?", (guild_id, self.clock())).fetchall()
            for m in expired:
                db.execute("INSERT OR IGNORE INTO bc_declines VALUES(?,?)", (m["id"], m["host_id"]))
                self._clear_host(db, m["id"])
                if m["status"] == "scheduled" and m["start"] > self.clock():
                    self._pick(db, m["id"], settings, live_ids)
                self._schedule(db, m["id"], settings)
            return len(expired)

    def _plan_access(self, db, guild_id, meeting_id, actor_id, organizer):
        m = self._get(db, "bc_meetings", guild_id, meeting_id)
        if not organizer and (m["host_id"] != actor_id or m["host_state"] != "confirmed"):
            raise ClubError("Черновик доступен подтверждённому ведущему и организаторам.")
        return m

    def plan(self, guild_id, meeting_id, actor_id, organizer=False):
        with self.tx() as db:
            m = self._plan_access(db, guild_id, meeting_id, actor_id, organizer)
            row = db.execute("SELECT * FROM bc_plans WHERE meeting_id=? AND generation=?", (meeting_id, m["plan_generation"])).fetchone()
            return dict(row) if row else dict(meeting_id=meeting_id, generation=m["plan_generation"], owner_id=m["host_id"], topics="", questions="", excerpts="", notes="", summary="", ready=0, version=0)

    def save_plan(self, guild_id, meeting_id, actor_id, *, generation, version, organizer=False, **fields):
        if not fields or not set(fields) <= {"topics", "questions", "excerpts", "notes", "summary", "ready"}:
            raise ClubError("Неизвестное поле плана.")
        for key in fields:
            if key != "ready":
                fields[key] = checked_text(fields[key], key, 4000, False)
        with self.tx() as db:
            m = self._plan_access(db, guild_id, meeting_id, actor_id, organizer)
            if generation != m["plan_generation"] or not m["host_id"]:
                raise ClubError("Ведущий изменился. Откройте новый план.")
            db.execute("INSERT OR IGNORE INTO bc_plans(meeting_id,generation,owner_id) VALUES(?,?,?)", (meeting_id, generation, m["host_id"]))
            row = db.execute("SELECT * FROM bc_plans WHERE meeting_id=? AND generation=?", (meeting_id, generation)).fetchone()
            if row["version"] != version:
                raise ClubError("План уже изменён. Откройте свежую версию.")
            if fields.get("ready") and not any((fields.get(k, row[k]) or "").strip() for k in ("topics", "questions", "excerpts", "notes")):
                raise ClubError("Сначала добавьте содержимое плана.")
            if set(fields) & {"topics", "questions", "excerpts", "notes"}:
                fields["ready"] = 0
            db.execute("UPDATE bc_plans SET " + ",".join(f"{k}=?" for k in fields) + ",version=version+1 WHERE meeting_id=? AND generation=?", (*fields.values(), meeting_id, generation))

    def handover(self, guild_id, meeting_id, actor_id, organizer=False):
        if not organizer:
            raise ClubError("Передать черновик может организатор.")
        with self.tx() as db:
            m = self._plan_access(db, guild_id, meeting_id, actor_id, True)
            if not m["host_id"] or m["host_state"] != "confirmed":
                raise ClubError("Сначала подтвердите нового ведущего.")
            if db.execute("SELECT 1 FROM bc_plans WHERE meeting_id=? AND generation=?", (meeting_id, m["plan_generation"])).fetchone():
                raise ClubError("У нового ведущего уже есть план; его нельзя перезаписать передачей.")
            old = db.execute("SELECT * FROM bc_plans WHERE meeting_id=? AND generation<? ORDER BY generation DESC LIMIT 1", (meeting_id, m["plan_generation"])).fetchone()
            if not old:
                raise ClubError("Нет предыдущего плана для передачи.")
            db.execute("INSERT INTO bc_plans(meeting_id,generation,owner_id,topics,questions,excerpts,notes,summary) VALUES(?,?,?,?,?,?,?,?)", (meeting_id, m["plan_generation"], m["host_id"], old["topics"], old["questions"], old["excerpts"], old["notes"], old["summary"]))

    def register_essay(self, guild_id, book_id, source_id, channel_id, author_id, title, url, *, correct=False,
                       managed=None, submitted=None):
        with self.tx() as db:
            self._get(db, "bc_books", guild_id, book_id)
            old = db.execute("SELECT * FROM bc_essays WHERE guild_id=? AND source_id=?", (guild_id, source_id)).fetchone()
            if old and (old["book_id"] != book_id or old["author_id"] != author_id) and not correct:
                raise ClubError("Работа уже связана с книгой; используйте исправление связи.")
            managed = old["managed"] if managed is None and old else int(managed) if managed is not None else 0
            submitted = old["submitted"] if submitted is None and old else int(submitted) if submitted is not None else 1
            db.execute("""INSERT INTO bc_essays(
              guild_id,source_id,channel_id,book_id,author_id,title,url,deleted,managed,submitted)
              VALUES(?,?,?,?,?,?,?,0,?,?) ON CONFLICT(guild_id,source_id) DO UPDATE SET
              channel_id=excluded.channel_id,book_id=excluded.book_id,author_id=excluded.author_id,
              title=excluded.title,url=excluded.url,deleted=0,managed=excluded.managed,submitted=excluded.submitted""",
              (guild_id, source_id, channel_id, book_id, author_id, title[:200], url, managed, submitted))

    def essays(self, book_id, submitted_only=True):
        return self.rows("SELECT * FROM bc_essays WHERE book_id=? AND deleted=0"
                         + (" AND submitted=1" if submitted_only else "")
                         + " ORDER BY author_id,source_id", (book_id,))

    def match_essay_title(self, guild_id, title):
        prefix = title.split(" · ", 1)[0].strip().casefold()
        return [b for b in self.books(guild_id) if b["title"].casefold() == prefix]

    def delete_essay(self, guild_id, source_id=None, channel_id=None):
        with self.tx() as db:
            if source_id is not None:
                db.execute("UPDATE bc_essays SET deleted=1 WHERE guild_id=? AND source_id=?", (guild_id, source_id))
            if channel_id is not None:
                db.execute("UPDATE bc_essays SET deleted=1 WHERE guild_id=? AND channel_id=?", (guild_id, channel_id))

    def missing_essays(self, book_id):
        return self.rows("""SELECT p.user_id FROM bc_participants p WHERE p.book_id=? AND p.present=1
          AND NOT EXISTS(SELECT 1 FROM bc_essays e WHERE e.book_id=p.book_id AND e.author_id=p.user_id
          AND e.deleted=0 AND e.submitted=1) ORDER BY p.user_id""", (book_id,))

    def _essay_schedule(self, db, book_id, settings):
        b = dict(db.execute("SELECT * FROM bc_books WHERE id=?", (book_id,)).fetchone())
        db.execute("UPDATE bc_jobs SET state='cancelled' WHERE entity_id=? AND state='pending' AND revision<>?", (book_id, b["revision"]))
        if b["deadline"]:
            for kind, due in (("essay", b["deadline"] - int(settings["essay_hours"] * 3600)), ("essay_due", b["deadline"])):
                if due > self.clock():
                    self._job(db, b, kind, 0, due)

    def recover_jobs(self):
        with self.tx() as db:
            db.execute("UPDATE bc_jobs SET state='unknown',detail='Process stopped during delivery; not retried automatically' WHERE state='sending'")
            db.execute("UPDATE bc_jobs SET state='skipped' WHERE state='pending' AND due<?", (self.clock(),))

    def due_jobs(self, guild_id):
        return self.rows("SELECT * FROM bc_jobs WHERE guild_id=? AND state='pending' AND due<=? ORDER BY due,key", (guild_id, self.clock()))

    def claim_job(self, key):
        with self.tx() as db:
            row = db.execute("SELECT * FROM bc_jobs WHERE key=? AND state='pending'", (key,)).fetchone()
            if not row:
                return False
            if row["due"] > self.clock():
                return False
            if row["due"] < self.clock() - 120:
                db.execute("UPDATE bc_jobs SET state='skipped' WHERE key=?", (key,))
                return False
            table = "bc_books" if row["kind"].startswith("essay") else "bc_meetings"
            entity = db.execute(f"SELECT * FROM {table} WHERE id=? AND revision=?", (row["entity_id"], row["revision"])).fetchone()
            if not entity or (table == "bc_meetings" and (entity["status"] != "scheduled" or entity["start"] <= self.clock())):
                db.execute("UPDATE bc_jobs SET state='cancelled' WHERE key=?", (key,))
                return False
            if row["kind"] in ("prepare", "escalate"):
                plan = db.execute("SELECT ready FROM bc_plans WHERE meeting_id=? AND generation=?", (entity["id"], entity["plan_generation"])).fetchone()
                if entity["host_state"] != "confirmed" or entity["host_id"] != row["target"] or (plan and plan[0]):
                    db.execute("UPDATE bc_jobs SET state='skipped' WHERE key=?", (key,))
                    return False
            db.execute("UPDATE bc_jobs SET state='sending' WHERE key=?", (key,))
            return True

    def finish_job(self, key, state="sent", detail=None):
        with self.tx() as db:
            db.execute("UPDATE bc_jobs SET state=?,detail=? WHERE key=? AND state='sending'", (state, detail, key))

    def publication(self, key):
        return self.one("SELECT * FROM bc_publications WHERE key=?", (key,))

    def reserve_publication(self, key, guild_id, channel_id, *, webhook_id=None):
        with self.tx() as db:
            return db.execute("""INSERT OR IGNORE INTO bc_publications(
              key,guild_id,channel_id,webhook_id) VALUES(?,?,?,?)""",
              (key, guild_id, channel_id, webhook_id)).rowcount == 1

    def save_publication(self, key, channel_id, message_id, digest=None):
        with self.tx() as db:
            db.execute("UPDATE bc_publications SET channel_id=?,message_id=?,state='ready',content_hash=? WHERE key=?", (channel_id, message_id, digest, key))

    def forget_publication(self, key):
        with self.tx() as db:
            db.execute("DELETE FROM bc_publications WHERE key=?", (key,))

    def webhook_binding(self, guild_id, channel_id):
        return self.one("SELECT * FROM bc_webhooks WHERE guild_id=? AND channel_id=?", (guild_id, channel_id))

    def reserve_webhook(self, guild_id, channel_id):
        with self.tx() as db:
            return db.execute("INSERT OR IGNORE INTO bc_webhooks(guild_id,channel_id) VALUES(?,?)",
                              (guild_id, channel_id)).rowcount == 1

    def save_webhook(self, guild_id, channel_id, webhook_id):
        with self.tx() as db:
            db.execute("""INSERT INTO bc_webhooks(guild_id,channel_id,webhook_id) VALUES(?,?,?)
              ON CONFLICT(guild_id,channel_id) DO UPDATE SET webhook_id=excluded.webhook_id""",
              (guild_id, channel_id, webhook_id))

    def forget_webhook(self, guild_id, channel_id):
        with self.tx() as db:
            db.execute("DELETE FROM bc_webhooks WHERE guild_id=? AND channel_id=?", (guild_id, channel_id))

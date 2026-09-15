"""Durable import budgets and publication recovery, independent of providers.

A reservation is charged before any external model call and is never refunded:
after an interrupted request there is no reliable way to establish its cost.
Source claims survive incomplete publication so the caller can resume using the
same deterministic item key without publishing a second copy of a message.
"""
from __future__ import annotations

import hashlib
import json
import uuid

from .store import ClubError


def _positive(value, label):
    if type(value) is not int or value <= 0:
        raise ClubError(f"{label}: нужно положительное целое число.")
    return value


def _key(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ClubError(f"{label}: нужно от 1 до 256 символов.")
    return value


def _json(value, label):
    if not isinstance(value, dict):
        raise ClubError(f"{label}: нужен объект.")
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ClubError(f"{label}: некорректные данные.") from exc


class ImportStore:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def _decode(row):
        if row is None:
            raise ClubError("Запуск импорта не найден на этом сервере.")
        result = dict(row)
        result["snapshot"] = json.loads(result["snapshot"])
        result["plan"] = json.loads(result["plan"]) if result["plan"] is not None else None
        return result

    @staticmethod
    def _row(db, guild_id, run_id):
        row = db.execute("SELECT * FROM bc_import_runs WHERE guild_id=? AND id=?",
                         (guild_id, run_id)).fetchone()
        if row is None:
            raise ClubError("Запуск импорта не найден на этом сервере.")
        return row

    def reserve_run(self, guild_id, actor_id, source_channel_id, budget_id, request_key,
                    max_runs, reserve_tokens, max_tokens, snapshot):
        """Atomically debit a persistent budget and admit one global model call."""
        for value, label in ((guild_id, "Сервер"), (actor_id, "Участник"),
                             (source_channel_id, "Канал"), (max_runs, "Лимит запусков"),
                             (reserve_tokens, "Резерв токенов"), (max_tokens, "Лимит токенов")):
            _positive(value, label)
        _key(budget_id, "Идентификатор бюджета")
        _key(request_key, "Ключ запроса")
        encoded = _json(snapshot, "Снимок сообщений")
        with self.store.tx() as db:
            previous = db.execute("SELECT * FROM bc_import_runs WHERE guild_id=? AND request_key=?",
                                  (guild_id, request_key)).fetchone()
            if previous is not None:
                return self._decode(previous)
            if db.execute("SELECT 1 FROM bc_import_runs WHERE state='running'").fetchone():
                raise ClubError("Другой запрос импорта ещё выполняется. Дождитесь его завершения.")
            budget = db.execute("SELECT * FROM bc_import_budgets WHERE budget_id=?",
                                (budget_id,)).fetchone()
            runs_used = budget["runs_used"] if budget else 0
            tokens_reserved = budget["tokens_reserved"] if budget else 0
            if runs_used >= max_runs:
                raise ClubError("Лимит запусков импорта исчерпан. Изменение доступно только в конфигурации.")
            if reserve_tokens > max_tokens - tokens_reserved:
                raise ClubError("Лимит токенов импорта исчерпан: для запроса недостаточно резерва.")
            db.execute("""INSERT INTO bc_import_budgets(budget_id,runs_used,tokens_reserved)
                          VALUES(?,?,?) ON CONFLICT(budget_id) DO UPDATE SET
                          runs_used=excluded.runs_used,tokens_reserved=excluded.tokens_reserved""",
                       (budget_id, runs_used + 1, tokens_reserved + reserve_tokens))
            run_id, timestamp = uuid.uuid4().hex, self.store.clock()
            db.execute("""INSERT INTO bc_import_runs(
                          id,guild_id,actor_id,source_channel_id,budget_id,request_key,
                          reserved_tokens,snapshot,created_at,updated_at)
                          VALUES(?,?,?,?,?,?,?,?,?,?)""",
                       (run_id, guild_id, actor_id, source_channel_id, budget_id, request_key,
                        reserve_tokens, encoded, timestamp, timestamp))
            return self._decode(self._row(db, guild_id, run_id))

    def run(self, guild_id, run_id):
        return self._decode(self.store.one("SELECT * FROM bc_import_runs WHERE guild_id=? AND id=?",
                                           (guild_id, run_id)))

    def budget(self, budget_id):
        return self.store.one("SELECT * FROM bc_import_budgets WHERE budget_id=?", (budget_id,)) or {
            "budget_id": budget_id, "runs_used": 0, "tokens_reserved": 0}

    def stage_reviewed_plan(self, guild_id, actor_id, source_channel_id, budget_id,
                            request_key, snapshot, plan):
        """Stage a human-approved plan without a model call or budget debit.

        This is an operator recovery path for a complete, reviewed archive
        snapshot. The caller must validate the plan and current source access.
        The existing apply path rechecks every source fingerprint before writes.
        """
        for value, label in ((guild_id, "Сервер"), (actor_id, "Участник"),
                             (source_channel_id, "Канал")):
            _positive(value, label)
        _key(budget_id, "Идентификатор бюджета")
        _key(request_key, "Ключ запроса")
        encoded_snapshot = _json(snapshot, "Снимок сообщений")
        encoded_plan = _json(plan, "План импорта")
        canonical = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        with self.store.tx() as db:
            previous = db.execute("SELECT * FROM bc_import_runs WHERE guild_id=? AND request_key=?",
                                  (guild_id, request_key)).fetchone()
            if previous is not None:
                if previous['snapshot'] != encoded_snapshot or previous['plan'] != encoded_plan:
                    raise ClubError('Ключ ручного плана уже занят другим снимком или планом.')
                return self._decode(previous)
            if db.execute("SELECT 1 FROM bc_import_runs WHERE state IN ('running','applying')").fetchone():
                raise ClubError('Другой импорт ещё выполняется.')
            run_id, timestamp = uuid.uuid4().hex, self.store.clock()
            db.execute("""INSERT INTO bc_import_runs(
                          id,guild_id,actor_id,source_channel_id,budget_id,request_key,
                          reserved_tokens,usage_tokens,state,detail,snapshot,plan,created_at,updated_at)
                          VALUES(?,?,?,?,?,?,0,0,'review',?,?,?,?,?)""",
                       (run_id, guild_id, actor_id, source_channel_id, budget_id, request_key,
                        'План проверен человеком; Codex не вызывался.', encoded_snapshot,
                        encoded_plan, timestamp, timestamp))
            db.execute("""INSERT INTO bc_import_plan_restores
                          (run_id,guild_id,actor_id,old_state,plan_sha256,created_at)
                          VALUES(?,?,?,?,?,?)""",
                       (run_id, guild_id, actor_id, 'human-approved', digest, timestamp))
            return self._decode(self._row(db, guild_id, run_id))

    def save_plan(self, guild_id, run_id, plan, usage_tokens=None):
        encoded = _json(plan, "План импорта")
        if usage_tokens is not None and (type(usage_tokens) is not int or usage_tokens < 0):
            raise ClubError("Расход токенов: нужно неотрицательное целое число.")
        with self.store.tx() as db:
            row = self._row(db, guild_id, run_id)
            if row["state"] != "running":
                raise ClubError("Этот запуск уже завершил анализ; повторный ответ не принимается.")
            # Unexpected excess usage also consumes the budget. Never credit
            # unused reservations: absent/partial usage is not proof of a refund.
            if usage_tokens is not None and usage_tokens > row["reserved_tokens"]:
                db.execute("UPDATE bc_import_budgets SET tokens_reserved=tokens_reserved+? WHERE budget_id=?",
                           (usage_tokens - row["reserved_tokens"], row["budget_id"]))
            db.execute("""UPDATE bc_import_runs SET state='review',plan=?,usage_tokens=?,updated_at=?
                          WHERE id=?""", (encoded, usage_tokens, self.store.clock(), run_id))

    def fail_run(self, guild_id, run_id, detail, state="failed", usage_tokens=None):
        if state not in {"failed", "unknown"}:
            raise ClubError("Некорректное состояние неудачного импорта.")
        if not isinstance(detail, str):
            raise ClubError("Причина сбоя должна быть текстом.")
        if usage_tokens is not None and (type(usage_tokens) is not int or usage_tokens < 0):
            raise ClubError("Расход токенов: нужно неотрицательное целое число.")
        # Callers pass a safe explanation, never provider exception bodies or secrets.
        detail = detail[:1000]
        with self.store.tx() as db:
            row = self._row(db, guild_id, run_id)
            if row["state"] != "running":
                raise ClubError("Завершённый анализ нельзя пометить как неудачный.")
            if usage_tokens is not None and usage_tokens > row["reserved_tokens"]:
                db.execute("UPDATE bc_import_budgets SET tokens_reserved=tokens_reserved+? WHERE budget_id=?",
                           (usage_tokens - row["reserved_tokens"], row["budget_id"]))
            db.execute("UPDATE bc_import_runs SET state=?,detail=?,usage_tokens=?,updated_at=? WHERE id=?",
                       (state, detail, usage_tokens, self.store.clock(), run_id))

    def restore_plan(self, guild_id, run_id, actor_id, plan):
        """Recover a human-reviewed failed result without changing usage or sources.

        The caller validates the plan against the immutable snapshot and rechecks
        current Discord access. Both the state transition and audit are atomic.
        """
        _positive(actor_id, "Участник")
        encoded = _json(plan, "План импорта")
        canonical = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        with self.store.tx() as db:
            row = self._row(db, guild_id, run_id)
            if row['state'] not in {'failed', 'unknown'}:
                raise ClubError('Восстановление допускается только для failed/unknown; готовый или выполняющийся план не заменяется.')
            if db.execute('SELECT 1 FROM bc_import_sources WHERE run_id=?', (run_id,)).fetchone():
                raise ClubError('У запуска уже есть привязки публикаций; замена плана запрещена.')
            timestamp = self.store.clock()
            db.execute('''INSERT INTO bc_import_plan_restores
                          (run_id,guild_id,actor_id,old_state,plan_sha256,created_at) VALUES(?,?,?,?,?,?)''',
                       (run_id, guild_id, actor_id, row['state'], digest, timestamp))
            db.execute("UPDATE bc_import_runs SET state='review',plan=?,updated_at=? WHERE id=?",
                       (encoded, timestamp, run_id))
            return self._decode(self._row(db, guild_id, run_id))

    def recover_runs(self):
        """Run only at process startup, when no model request is still in flight."""
        with self.store.tx() as db:
            unknown = db.execute("""UPDATE bc_import_runs SET state='unknown',detail=?,updated_at=?
                                    WHERE state='running'""",
                                 ("Бот перезапущен до получения результата. Резерв сохранён; автоматического повтора нет.",
                                  self.store.clock())).rowcount
            review = db.execute("UPDATE bc_import_runs SET state='review',updated_at=? WHERE state='applying'",
                                (self.store.clock(),)).rowcount
            return {"unknown": unknown, "review": review}

    def claim_apply(self, guild_id, run_id):
        with self.store.tx() as db:
            row = self._row(db, guild_id, run_id)
            if row["state"] == "done":
                return False
            if row["state"] != "review":
                raise ClubError("Этот импорт пока недоступен для публикации или уже публикуется.")
            db.execute("UPDATE bc_import_runs SET state='applying',updated_at=? WHERE id=?",
                       (self.store.clock(), run_id))
            return True

    def finish_apply(self, guild_id, run_id):
        with self.store.tx() as db:
            row = self._row(db, guild_id, run_id)
            if row["state"] == "done":
                return
            if row["state"] != "applying":
                raise ClubError("Импорт не находится на этапе публикации.")
            db.execute("UPDATE bc_import_runs SET state='done',updated_at=? WHERE id=?",
                       (self.store.clock(), run_id))

    def defer_apply(self, guild_id, run_id):
        with self.store.tx() as db:
            row = self._row(db, guild_id, run_id)
            if row["state"] != "applying":
                raise ClubError("Импорт не находится на этапе публикации.")
            db.execute("UPDATE bc_import_runs SET state='review',updated_at=? WHERE id=?",
                       (self.store.clock(), run_id))

    def claim_sources(self, guild_id, run_id, item_key, source_ids):
        _key(item_key, "Ключ эссе")
        if not isinstance(source_ids, list) or not source_ids:
            raise ClubError("У эссе должен быть хотя бы один исходный пост.")
        for source_id in source_ids:
            _positive(source_id, "Исходный пост")
        requested = set(source_ids)
        with self.store.tx() as db:
            row = self._row(db, guild_id, run_id)
            if row["state"] != "applying":
                raise ClubError("Привязка исходных постов доступна только при публикации импорта.")
            existing = db.execute("SELECT source_id FROM bc_import_sources WHERE guild_id=? AND item_key=?",
                                  (guild_id, item_key)).fetchall()
            if existing and {r["source_id"] for r in existing} != requested:
                return False
            for source_id in requested:
                claimed = db.execute("SELECT item_key FROM bc_import_sources WHERE guild_id=? AND source_id=?",
                                     (guild_id, source_id)).fetchone()
                if claimed and claimed["item_key"] != item_key:
                    return False
            db.executemany("""INSERT OR IGNORE INTO bc_import_sources(guild_id,source_id,run_id,item_key)
                              VALUES(?,?,?,?)""", ((guild_id, source_id, run_id, item_key) for source_id in requested))
            return True

    def imported_source(self, guild_id, source_id):
        return self.store.one("SELECT * FROM bc_import_sources WHERE guild_id=? AND source_id=?",
                              (guild_id, source_id))

    def finish_sources(self, guild_id, item_key, thread_id):
        _positive(thread_id, "Тема эссе")
        with self.store.tx() as db:
            rows = db.execute("SELECT thread_id FROM bc_import_sources WHERE guild_id=? AND item_key=?",
                              (guild_id, item_key)).fetchall()
            if not rows:
                raise ClubError("Исходные посты для эссе ещё не зарезервированы.")
            if any(row["thread_id"] is not None and row["thread_id"] != thread_id for row in rows):
                raise ClubError("Это эссе уже привязано к другой теме.")
            db.execute("UPDATE bc_import_sources SET thread_id=? WHERE guild_id=? AND item_key=?",
                       (thread_id, guild_id, item_key))

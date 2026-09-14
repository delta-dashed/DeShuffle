"""Domain regressions use a real disposable SQLite database and a fixed clock."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from bookclub.store import ClubError, Store, parse_time
from bookclub.render import book_pages, catalog_pages, news_content


CONFIG = dict(news=11, chat=12, books=13, essays=14, voice=15, organizers=[99], organizer_roles=[22])


def stub_delivery_transport(test):
    # Domain/adapter tests simulate Discord at the SDK call boundary.
    # test_single_delivery separately exercises the real SDK HTTP retries.
    async def forum_send(forum, **kwargs):
        return await forum.create_thread(**kwargs)
    async def webhook_send(hook, *args, **kwargs):
        return await hook.send(*args, **kwargs)
    async def channel_send(channel, *args, **kwargs):
        return await channel.send(*args, **kwargs)
    for target, effect in (('bookclub.service.create_thread_once', forum_send),
                           ('bookclub.service.channel_send_once', channel_send),
                           ('bookclub.service.webhook_send_once', webhook_send),
                           ('bookclub.import_publication.webhook_send_once', webhook_send)):
        transport = patch(target, side_effect=effect)
        transport.start()
        test.addCleanup(transport.stop)


class ClubFixture:
    def setUp(self):
        stub_delivery_transport(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'voice_activity.sqlite3'
        self.now = 2_000_000_000
        self.store = Store(self.path, clock=lambda: self.now)
        self.store.configure(1, CONFIG)
        self.book = self.store.create_book(1, 'Книга', 'Автор', 'https://example.org/book', 'book1')
        for user in (1, 2, 3):
            self.store.participant(1, self.book['id'], user, willing=True)
        self.event_id = 100
        self.meeting = self.make_meeting()

    def make_meeting(self, days=3):
        self.event_id += 1
        m = self.store.draft_meeting(1, self.book['id'], f'Встреча {self.event_id}', 'Часть 1', '5 · Возвращение', str(self.event_id))
        self.sync(m, start=self.now + days * 86400, event_id=self.event_id)
        return self.store.meeting(1, m['id'])

    def sync(self, m=None, **changes):
        m = self.store.meeting(1, (m or self.meeting)['id'])
        values = dict(event_id=m['event_id'], name=m['name'], start=m['start'], end=m['end'], voice_id=15, status='scheduled')
        values.update(changes)
        values['end'] = values['end'] or values['start'] + 5400
        return self.store.sync_event(1, m['id'], **values)

    def action(self, action, actor=1, m=None, **kwargs):
        m = self.store.meeting(1, (m or self.meeting)['id'])
        return self.store.host_action(1, m['id'], actor, action, version=kwargs.pop('version', m['host_version']), live_ids=kwargs.pop('live_ids', {1, 2, 3}), **kwargs)

    def jobs(self, m=None, state='pending'):
        return self.store.rows('SELECT * FROM bc_jobs WHERE entity_id=? AND state=? ORDER BY due,key', ((m or self.meeting)['id'], state))

    def save_plan(self, actor=1, **fields):
        p = self.store.plan(1, self.meeting['id'], actor)
        self.store.save_plan(1, self.meeting['id'], actor, generation=p['generation'], version=p['version'], **fields)
        return self.store.plan(1, self.meeting['id'], actor)


class ClubStateTests(ClubFixture, unittest.TestCase):
    def test_migration_preserves_legacy_data_and_is_repeatable(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE voice_sessions(id INTEGER PRIMARY KEY, note TEXT)')
            db.execute("INSERT INTO voice_sessions VALUES(1,'legacy')")
            db.execute('PRAGMA user_version=7')
        restored = Store(self.path)
        self.assertEqual(restored.one('SELECT note FROM voice_sessions')['note'], 'legacy')
        self.assertEqual(restored.one('PRAGMA user_version')['user_version'], 7)
        self.assertEqual(len(restored.books(1)), 1)

    def test_book_request_idempotency_and_single_current_per_guild(self):
        same = self.store.create_book(1, 'Книга', 'Автор', '', 'book1')
        self.assertEqual(same['id'], self.book['id'])
        second = self.store.create_book(1, 'Вторая', 'Автор', '', 'book2')
        self.store.update_book(1, self.book['id'], status='reading')
        with self.assertRaises(ClubError):
            self.store.update_book(1, second['id'], status='reading')
        self.store.configure(2, CONFIG)
        foreign = self.store.create_book(2, 'Книга', 'Автор', '', 'book1')
        self.store.update_book(2, foreign['id'], status='reading')
        with self.assertRaises(ClubError):
            self.store.book(1, foreign['id'])
        with self.assertRaises(ClubError):
            self.store.register_essay(2, self.book['id'], 555, 555, 1, 'test', 'url')

    def test_identical_config_does_not_invalidate_assignments_or_jobs(self):
        m = self.action('offer', organizer=True, candidate=1)
        before = self.jobs()
        self.store.configure(1, CONFIG)
        self.assertEqual(before, self.jobs())
        self.assertEqual(m, self.store.meeting(1, m['id']))

    def test_repeated_event_is_noop_and_event_cannot_be_bound_twice(self):
        before = self.jobs()
        self.assertFalse(self.sync())
        self.assertEqual(before, self.jobs())
        second = self.store.draft_meeting(1, self.book['id'], 'Другой', '1', '1', 'second')
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.sync_event(1, second['id'], event_id=self.meeting['event_id'], name='test', start=self.meeting['start'], end=self.meeting['end'], voice_id=15, status='scheduled')

    def test_reschedule_invalidates_old_jobs_and_requires_new_acceptance(self):
        old = self.action('volunteer')
        self.save_plan(topics='Тема', questions='Почему?')
        self.save_plan(ready=True)
        old_jobs = self.jobs()
        self.sync(start=old['start'] + 86400, end=old['end'] + 86400)
        m = self.store.meeting(1, old['id'])
        self.assertEqual(m['host_state'], 'pending')
        self.assertFalse(self.store.plan(1, m['id'], 99, organizer=True)['ready'])
        for job in old_jobs:
            self.assertFalse(self.store.claim_job(job['key']))
        self.assertIn('rescheduled', {j['kind'] for j in self.jobs()})
        with self.assertRaises(ClubError):
            self.action('accept', version=old['host_version'])
        self.action('accept')
        due = {j['kind']: j['due'] for j in self.jobs()}
        self.assertEqual(due['prepare'], m['start'] - 86400)
        self.assertEqual(due['escalate'], m['start'] - 10800)

    def test_cancel_prevents_every_reminder_and_does_not_count_rotation(self):
        self.action('volunteer')
        old = self.jobs()
        self.sync(status='cancelled')
        self.now = self.meeting['start'] - 60
        self.assertFalse(self.jobs())
        self.assertTrue(all(not self.store.claim_job(j['key']) for j in old))
        self.assertFalse(self.store.rows('SELECT * FROM bc_host_history'))

    def test_restart_skips_overdue_and_never_retries_uncertain_delivery(self):
        self.action('offer', organizer=True, candidate=1)
        immediate = next(j for j in self.jobs() if j['kind'] == 'offer')
        self.assertTrue(self.store.claim_job(immediate['key']))
        self.now += 3 * 86400
        restored = Store(self.path, lambda: self.now)
        restored.recover_jobs()
        self.assertEqual(restored.one('SELECT state FROM bc_jobs WHERE key=?', (immediate['key'],))['state'], 'unknown')
        self.assertFalse(restored.due_jobs(1))

    def test_sent_job_is_not_sent_again_and_long_outage_skips_batch(self):
        job = self.jobs()[0]
        self.now = job['due']
        self.assertTrue(self.store.claim_job(job['key']))
        self.store.finish_job(job['key'])
        self.assertFalse(self.store.claim_job(job['key']))
        m = self.make_meeting()
        late = self.jobs(m)[0]
        self.now = late['due'] + 121
        self.assertFalse(self.store.claim_job(late['key']))
        self.assertEqual(self.store.one('SELECT state FROM bc_jobs WHERE key=?', (late['key'],))['state'], 'skipped')

    def test_two_simultaneous_volunteers_have_one_winner(self):
        original_version = self.meeting['host_version']
        def attempt(user):
            try:
                self.action('volunteer', actor=user, version=original_version)
                return True
            except ClubError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, (1, 2)))
        self.assertEqual(sum(results), 1)
        self.assertIn(self.store.meeting(1, self.meeting['id'])['host_id'], (1, 2))

    def test_expired_offer_does_not_overwrite_next_candidate(self):
        offered = self.action('pick', organizer=True)
        self.assertEqual(offered['host_id'], 1)
        self.now = offered['offer_until']
        with self.assertRaises(ClubError):
            self.action('accept', actor=1)
        self.store.expire_offers(1, {1, 2, 3})
        newer = self.store.meeting(1, self.meeting['id'])
        self.assertEqual(newer['host_id'], 2)
        with self.assertRaises(ClubError):
            self.action('accept', actor=1, version=offered['host_version'])

    def test_rotation_avoids_upcoming_assignments_and_uses_completed_history(self):
        self.action('volunteer', actor=1)
        second = self.make_meeting(4)
        selected = self.action('pick', organizer=True, m=second)
        self.assertEqual(selected['host_id'], 2)
        self.sync(status='active')
        self.sync(status='completed')
        self.assertEqual(self.store.one('SELECT user_id FROM bc_host_history')['user_id'], 1)
        third = self.make_meeting(5)
        self.assertEqual(self.action('pick', organizer=True, m=third)['host_id'], 3)

    def test_decline_absence_and_departed_members_are_not_selected(self):
        self.store.attendance(1, self.meeting['id'], 1, True)
        self.assertEqual(self.action('pick', organizer=True, live_ids={1, 2})['host_id'], 2)
        declined = self.action('decline', actor=2, live_ids={1, 2})
        self.assertEqual(declined['host_state'], 'none')
        self.assertTrue(declined['exhausted'])
        with self.assertRaises(ClubError):
            self.action('volunteer', actor=1)

    def test_removing_participant_cancels_host_jobs(self):
        self.action('volunteer')
        old = self.jobs()
        self.store.participant(1, self.book['id'], 1, joined=False)
        self.assertEqual(self.store.meeting(1, self.meeting['id'])['host_state'], 'none')
        self.assertTrue(all(not self.store.claim_job(j['key']) for j in old))

    def test_private_plan_and_old_host_actions_are_denied_after_replacement(self):
        self.action('volunteer')
        plan = self.save_plan(topics='СЕКРЕТНЫЙ ЧЕРНОВИК', questions='Почему?', excerpts='Фрагмент')
        for user in (2, 3):
            with self.assertRaises(ClubError):
                self.store.plan(1, self.meeting['id'], user)
        public = '\n'.join(book_pages(self.store, self.book, self.store.settings(1)))
        self.assertNotIn('СЕКРЕТНЫЙ', public)
        self.action('replace', actor=99, organizer=True)
        self.action('volunteer', actor=2)
        with self.assertRaises(ClubError):
            self.store.save_plan(1, self.meeting['id'], 1, generation=plan['generation'], version=plan['version'], topics='Перезапись')
        self.assertEqual(self.store.plan(1, self.meeting['id'], 2)['topics'], '')
        self.store.handover(1, self.meeting['id'], 99, organizer=True)
        self.assertEqual(self.store.plan(1, self.meeting['id'], 2)['topics'], 'СЕКРЕТНЫЙ ЧЕРНОВИК')
        with self.assertRaises(ClubError):
            self.store.handover(1, self.meeting['id'], 99, organizer=True)

    def test_concurrent_plan_edit_and_empty_ready_are_rejected(self):
        self.action('volunteer')
        empty = self.store.plan(1, self.meeting['id'], 1)
        with self.assertRaises(ClubError):
            self.save_plan(ready=True)
        self.save_plan(topics='Есть тема')
        with self.assertRaises(ClubError):
            self.store.save_plan(1, self.meeting['id'], 1, generation=empty['generation'], version=empty['version'], notes='stale')

    def test_late_appointment_only_schedules_future_thresholds(self):
        self.now = self.meeting['start'] - 2 * 3600
        self.action('volunteer')
        self.assertEqual({j['kind'] for j in self.jobs()}, {'participants'})
        self.sync(start=self.now + 2 * 86400, end=self.now + 2 * 86400 + 5400)
        self.action('accept')
        self.assertTrue({'prepare', 'escalate', 'participants'} <= {j['kind'] for j in self.jobs()})

    def test_ready_plan_suppresses_preparation_escalation(self):
        self.action('volunteer')
        self.save_plan(topics='Подготовленное')
        self.save_plan(ready=True)
        for j in self.jobs():
            if j['kind'] in ('prepare', 'escalate'):
                self.now = j['due']
                self.assertFalse(self.store.claim_job(j['key']))

    def test_essays_rename_correct_delete_multiple_and_ambiguous_titles(self):
        self.store.register_essay(1, self.book['id'], 501, 501, 1, 'Книга · эссе', 'url1')
        self.store.register_essay(1, self.book['id'], 501, 501, 1, 'Новое имя', 'url1')
        self.store.register_essay(1, self.book['id'], 502, 502, 1, 'Вторая работа', 'url2')
        self.store.delete_essay(1, source_id=501)
        self.assertNotIn(1, {r['user_id'] for r in self.store.missing_essays(self.book['id'])})
        second = self.store.create_book(1, 'Книга', 'Другой автор', '', 'other')
        self.assertEqual(len(self.store.match_essay_title(1, 'Книга · Эссе')), 2)
        with self.assertRaises(ClubError):
            self.store.register_essay(1, second['id'], 502, 502, 1, 'Другой', 'url2')
        self.store.register_essay(1, second['id'], 502, 502, 1, 'Другой', 'url2', correct=True)
        self.assertIn(1, {r['user_id'] for r in self.store.missing_essays(self.book['id'])})
        self.store.delete_essay(1, channel_id=502)
        self.assertTrue(self.store.one('SELECT deleted FROM bc_essays WHERE source_id=502')['deleted'])

    def test_essay_deadline_is_explicit_and_config_replans_it(self):
        self.assertFalse(self.store.rows("SELECT * FROM bc_jobs WHERE kind LIKE 'essay%'"))
        self.store.update_book(1, self.book['id'], deadline=self.now + 86400 * 3)
        old = self.jobs(self.book)
        self.store.configure(1, {**CONFIG, 'essay_hours': 12})
        fresh = self.jobs(self.book)
        self.assertEqual(next(j['due'] for j in fresh if j['kind'] == 'essay'), self.now + 86400 * 3 - 43200)
        self.assertTrue(all(not self.store.claim_job(j['key']) for j in old))
        self.store.update_book(1, self.book['id'], deadline=None)
        self.assertFalse(self.jobs(self.book))

    def test_long_catalog_and_book_cards_fit_discord_and_order_is_stable(self):
        for i in range(35):
            self.store.create_book(1, 'Длинное название ' * 8 + str(i), 'Автор', '', str(i))
        self.store.update_book(1, self.book['id'], materials='a' * 4000, position=-1, status='read')
        rendered = catalog_pages(self.store, 1)
        self.assertGreater(len(rendered), 1)
        self.assertTrue(all(len(p) <= 1750 for p in rendered))
        self.assertEqual(self.store.books(1)[0]['id'], self.book['id'])
        self.assertTrue(all(len(p) <= 1750 for p in book_pages(self.store, self.store.book(1, self.book['id']), self.store.settings(1))))

    def test_full_book_to_completion_path(self):
        self.store.update_book(1, self.book['id'], status='reading', deadline=self.now + 7 * 86400)
        offered = self.action('pick', organizer=True)
        self.action('accept', actor=offered['host_id'])
        self.save_plan(topics='Свобода выбора', questions='Какие мотивы можно обосновать?', excerpts='Глава 2')
        self.save_plan(ready=True)
        self.sync(start=self.meeting['start'] + 3600, end=self.meeting['end'] + 3600)
        self.action('accept')
        self.save_plan(ready=True)
        self.sync(status='active')
        self.sync(status='completed')
        self.save_plan(summary='Вернуться к спору о мотивах.')
        self.store.register_essay(1, self.book['id'], 901, 901, 1, 'Книга · Мотивы', 'https://discord.com/channels/1/901')
        self.store.update_book(1, self.book['id'], status='read')
        b = self.store.book(1, self.book['id'])
        self.assertEqual(b['status'], 'read')
        self.assertEqual(len(self.store.rows('SELECT * FROM bc_host_history')), 1)
        self.assertIn('Мотивы', '\n'.join(book_pages(self.store, b, self.store.settings(1))))
        self.assertNotIn('Свобода выбора', news_content(self.store, 1, self.store.settings(1)))

    def test_moscow_and_explicit_offset_are_same_instant(self):
        self.assertEqual(parse_time('2027-01-05 18:30'), parse_time('2027-01-05T15:30:00+00:00'))
        with self.assertRaises(ClubError):
            parse_time('2027-10-31 02:30', 'Europe/Berlin')
        with self.assertRaises(ClubError):
            parse_time('завтра наполовину')


if __name__ == '__main__':
    unittest.main()

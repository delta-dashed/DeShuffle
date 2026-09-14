"""Regressions found while auditing host notifications and active meetings."""
import unittest

from test_bookclub import ClubFixture, CONFIG


class MeetingAuditTests(ClubFixture, unittest.TestCase):
    def test_renamed_event_keeps_pending_invitation_and_original_due_time(self):
        offered = self.action('offer', organizer=True, candidate=1)
        original = next(j for j in self.jobs() if j['kind'] == 'offer')
        self.now += 10

        self.sync(name='Уточнённое название встречи')

        notices = [j for j in self.jobs() if j['kind'] == 'offer']
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]['due'], original['due'])
        self.assertNotEqual(notices[0]['key'], original['key'])
        current = self.store.meeting(1, offered['id'])
        self.assertEqual(current['host_version'], offered['host_version'])
        self.assertFalse(self.store.claim_job(original['key']))
        self.assertTrue(self.store.claim_job(notices[0]['key']))

    def test_configuration_change_preserves_pending_reschedule_notice(self):
        self.action('volunteer')
        self.sync(start=self.meeting['start'] + 86400, end=self.meeting['end'] + 86400)
        original = next(j for j in self.jobs() if j['kind'] == 'rescheduled')
        self.now += 15

        self.store.configure(1, {**CONFIG, 'participant_minutes': 20})

        notices = [j for j in self.jobs() if j['kind'] in ('offer', 'rescheduled')]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]['kind'], 'rescheduled')
        self.assertEqual(notices[0]['due'], original['due'])
        self.assertTrue(self.store.claim_job(notices[0]['key']))

    def test_rename_does_not_repeat_an_already_sent_invitation(self):
        self.action('offer', organizer=True, candidate=1)
        original = next(j for j in self.jobs() if j['kind'] == 'offer')
        self.assertTrue(self.store.claim_job(original['key']))
        self.store.finish_job(original['key'])

        self.sync(name='Уточнённое название встречи')

        self.assertFalse([j for j in self.jobs() if j['kind'] in ('offer', 'rescheduled')])

    def test_reschedule_replaces_pending_invitation_with_one_current_notice(self):
        self.action('offer', organizer=True, candidate=1)

        self.sync(start=self.meeting['start'] + 86400, end=self.meeting['end'] + 86400)

        notices = [j for j in self.jobs() if j['kind'] in ('offer', 'rescheduled')]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]['kind'], 'rescheduled')

    def test_rename_does_not_make_an_overdue_invitation_fresh(self):
        self.action('offer', organizer=True, candidate=1)
        self.now += 121

        self.sync(name='Уточнённое название встречи')

        notice = next(j for j in self.jobs() if j['kind'] == 'offer')
        self.assertFalse(self.store.claim_job(notice['key']))
        self.assertEqual(self.store.one('SELECT state FROM bc_jobs WHERE key=?', (notice['key'],))['state'], 'skipped')

    def test_extending_active_meeting_keeps_host_and_private_plan_access(self):
        original = self.action('volunteer')
        self.save_plan(topics='Темы разговора')
        self.save_plan(ready=True)
        self.now = original['start'] + 60
        self.sync(status='active')

        self.sync(status='active', end=original['end'] + 3600)
        self.store.expire_offers(1, {1, 2, 3})

        current = self.store.meeting(1, original['id'])
        self.assertEqual(current['host_state'], 'confirmed')
        self.assertEqual(current['host_id'], original['host_id'])
        self.assertEqual(current['host_version'], original['host_version'])
        plan = self.store.plan(1, original['id'], 1)
        self.assertEqual(plan['topics'], 'Темы разговора')
        self.assertTrue(plan['ready'])
        self.assertFalse(self.jobs())

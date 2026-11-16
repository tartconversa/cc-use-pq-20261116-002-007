import tempfile
import unittest
from pathlib import Path

from community_fitness.attendance import Attendance
from community_fitness.competition import Competition
from community_fitness.errors import DomainError
from community_fitness.pilot_analytics import PilotAnalytics
from community_fitness.pilot_plans import PilotPlans
from community_fitness.publication import Publication
from community_fitness.scoring_sessions import ScoringSessions
from community_fitness.services import Services
from community_fitness.storage import Store


# Future-dated event: every pilot decision must be recorded before its slot.
EVENT_START = '2027-09-10T08:00:00+08:00'
EVENT_END = '2027-09-10T19:00:00+08:00'
TREAT_START = '2027-09-10T10:00:00+08:00'
TREAT_END = '2027-09-10T11:00:00+08:00'
CTRL_START = '2027-09-10T14:00:00+08:00'
CTRL_END = '2027-09-10T15:00:00+08:00'


class PilotScenario(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'pilot.sqlite3')
        self.competition = Competition(self.store)
        self.services = Services(self.store)
        self.sessions = ScoringSessions(self.store)
        self.attendance = Attendance(self.store)
        self.plans = PilotPlans(self.store)
        self.analytics = PilotAnalytics(self.store)
        self.publication = Publication(self.store)

    def revision(self):
        return self.store.inspect('evt')['revision']

    def create_event(self, *, teams=4, member_count=6):
        self.competition.create_event('evt', 'Pilot showcase', EVENT_START, EVENT_END,
                                      'Asia/Shanghai')
        self.competition.add_group('evt', 'dance', 'Dance', {'form': 60, 'rhythm': 40}, 2, 3)
        for n in range(teams):
            self.competition.register_team('evt', f't{n}', 'dance', f'Team {n}',
                                           'Qingpu', member_count)
        for n in range(2):
            self.competition.assign_judge('evt', f'j{n}', f'Judge {n}', 'dance')
        self.services.add_zone('evt', 'tour-a', '文旅咨询A', 'tourism',
                               '2027-09-10T09:00:00+08:00', '2027-09-10T18:00:00+08:00')

    def open_event(self):
        self.competition.change_state('evt', 'open', self.revision())
        self.sessions.open('evt', 'dance', self.revision())

    def score_and_close(self):
        for team in ('t0', 't1', 't2', 't3'):
            for judge in ('j0', 'j1'):
                self.competition.submit_score('evt', team, judge,
                                              {'form': 52, 'rhythm': 34}, f'{team}-{judge}')
        self.sessions.close('evt', 'dance', self.revision())
        self.competition.change_state('evt', 'closed', self.revision())

    def publish(self):
        preview = self.publication.preview('evt')
        return self.publication.publish('evt', preview['head_version'],
                                        preview['event']['revision'])

    def check_in_many(self, times):
        for index, arrived in enumerate(times):
            team = f't{index // 6}'
            member = f'm{index}'
            self.attendance.check_in('evt', team, member, 'north', arrived, f'r{index}')


class PilotPlanTests(PilotScenario):
    def test_register_versioning_and_append_only_history(self):
        self.create_event()
        first = self.plans.register('evt', 'p1', 'treatment', ['tour-a'],
                                    TREAT_START, TREAT_END, '上午引导员试点')
        self.assertEqual(first['version'], 1)
        self.assertEqual(first['state'], 'active')
        second = self.plans.register('evt', 'p1', 'treatment', ['tour-a'],
                                     '2027-09-10T10:30:00+08:00', TREAT_END,
                                     '推迟半小时开始')
        self.assertEqual(second['version'], 2)
        history = self.plans.history('evt', 'p1')
        self.assertEqual([row['version'] for row in history], [1, 2])
        self.assertEqual(history[0]['state'], 'superseded')
        self.assertEqual(history[0]['superseded_by'], 2)
        self.assertEqual(history[1]['state'], 'active')
        # Original window is preserved verbatim.
        self.assertEqual(history[0]['slot_start'], TREAT_START)

    def test_no_lookahead_refuses_slot_that_already_started(self):
        self.create_event()
        with self.assertRaises(DomainError) as caught:
            self.plans.register('evt', 'late', 'treatment', ['tour-a'],
                                '2026-01-01T10:00:00+08:00', '2026-01-01T11:00:00+08:00',
                                '赛后补分组')
        self.assertEqual(caught.exception.code, 'pilot_slot_started')

    def test_amendment_refused_after_original_slot_started(self):
        self.create_event()
        self.plans.register('evt', 'p1', 'treatment', ['tour-a'],
                            TREAT_START, TREAT_END, '试点')
        with self.assertRaises(DomainError) as caught:
            self.plans.register('evt', 'p1', 'treatment', ['tour-a'],
                                '2026-01-01T10:00:00+08:00', TREAT_END, '倒写')
        self.assertEqual(caught.exception.code, 'pilot_slot_started')

    def test_overlapping_live_plans_on_same_zone_rejected(self):
        self.create_event()
        self.plans.register('evt', 'p1', 'treatment', ['tour-a'],
                            TREAT_START, TREAT_END, '试点')
        with self.assertRaises(DomainError) as caught:
            self.plans.register('evt', 'p2', 'control', ['tour-a'],
                                '2027-09-10T10:30:00+08:00',
                                '2027-09-10T11:30:00+08:00', '重叠对照')
        self.assertEqual(caught.exception.code, 'pilot_overlap')

    def test_cancel_keeps_history_and_blocks_amendment(self):
        self.create_event()
        self.plans.register('evt', 'p1', 'treatment', ['tour-a'],
                            TREAT_START, TREAT_END, '试点')
        cancelled = self.plans.cancel('evt', 'p1', '天气原因取消')
        self.assertEqual(cancelled['state'], 'cancelled')
        history = self.plans.history('evt', 'p1')
        self.assertEqual([row['state'] for row in history], ['cancelled', 'cancelled'])
        with self.assertRaises(DomainError) as caught:
            self.plans.register('evt', 'p1', 'treatment', ['tour-a'],
                                TREAT_START, TREAT_END, '复活')
        self.assertEqual(caught.exception.code, 'pilot_plan_cancelled')

    def test_only_tourism_zones_allowed(self):
        self.create_event()
        self.services.add_zone('evt', 'tea', '茶饮', 'tea',
                               '2027-09-10T09:00:00+08:00', '2027-09-10T18:00:00+08:00')
        with self.assertRaises(DomainError) as caught:
            self.plans.register('evt', 'p1', 'treatment', ['tea'],
                                TREAT_START, TREAT_END, '错专区')
        self.assertEqual(caught.exception.code, 'invalid_pilot_zone_kind')

    def test_unknown_zone_and_bad_arm(self):
        self.create_event()
        with self.assertRaises(DomainError) as caught:
            self.plans.register('evt', 'p1', 'treatment', ['nope'],
                                TREAT_START, TREAT_END, 'x')
        self.assertEqual(caught.exception.code, 'unknown_zone')
        with self.assertRaises(DomainError) as caught:
            self.plans.register('evt', 'p2', 'maybe', ['tour-a'],
                                TREAT_START, TREAT_END, 'x')
        self.assertEqual(caught.exception.code, 'invalid_pilot_arm')

    def test_registration_blocked_after_event_closes(self):
        self.create_event()
        self.open_event()
        self.score_and_close()
        with self.assertRaises(DomainError) as caught:
            self.plans.register('evt', 'p1', 'treatment', ['tour-a'],
                                TREAT_START, TREAT_END, '关门后登记')
        self.assertEqual(caught.exception.code, 'event_locked')


class PilotAnalysisTests(PilotScenario):
    def _seed_plans_and_data(self, *, repeat=False, concurrent_zone=False,
                             no_attendance=False):
        self.create_event()
        self.plans.register('evt', 'treat1', 'treatment', ['tour-a'],
                            TREAT_START, TREAT_END, '上午引导员')
        self.plans.register('evt', 'ctrl1', 'control', ['tour-a'],
                            CTRL_START, CTRL_END, '下午无引导员对照')
        if concurrent_zone:
            # Another zone open during the treatment slot => confound flag.
            self.services.add_zone('evt', 'tea', '茶饮', 'tea',
                                   '2027-09-10T09:30:00+08:00',
                                   '2027-09-10T10:30:00+08:00')
        self.open_event()
        # 10 arrivals per slot.
        times = [f'2027-09-10T10:{m:02d}:00+08:00' for m in range(0, 50, 5)]
        times += [f'2027-09-10T14:{m:02d}:00+08:00' for m in range(0, 50, 5)]
        if not no_attendance:
            self.check_in_many(times)
        # Treatment: 6 unique visitors; control: 4 unique visitors.
        treatment_visitors = ['v1', 'v2', 'v3', 'v4', 'v5', 'v6']
        for i, token in enumerate(treatment_visitors):
            self.services.record_encounter(
                'evt', f'te{i}', 'tour-a',
                f'2027-09-10T10:{5 + i * 8:02d}:00+08:00', token, 1)
        if repeat:
            # v1 contacts the desk a second time in the treatment slot.
            self.services.record_encounter(
                'evt', 'te-repeat', 'tour-a', '2027-09-10T10:55:00+08:00', 'v1', 1)
        control_visitors = ['w1', 'w2', 'w3', 'w4']
        for i, token in enumerate(control_visitors):
            self.services.record_encounter(
                'evt', f'ce{i}', 'tour-a',
                f'2027-09-10T14:{5 + i * 10:02d}:00+08:00', token, 1)
        self.score_and_close()
        return self.publish()

    def test_end_to_end_uplift_on_published_snapshot(self):
        snapshot = self._seed_plans_and_data()
        self.assertIn('pilot_data', snapshot)
        self.assertTrue(snapshot['pilot_data']['attendance_present'])
        report = self.analytics.run('evt', 'a1', snapshot['version'])
        self.assertEqual(report['ruleset_version'], 'pilot-v1')
        arms = report['comparison']['arms']
        self.assertEqual(arms['treatment']['pooled_participation_rate'], '0.6000')
        self.assertEqual(arms['control']['pooled_participation_rate'], '0.4000')
        uplift = report['comparison']['uplift']
        self.assertEqual(uplift['participation_rate_difference_pp'], '0.20')
        self.assertTrue(report['comparison']['rate_comparison_available'])
        self.assertEqual(report['summary']['excluded_slots'], [])

    def test_analysis_is_frozen_and_reproducible(self):
        snapshot = self._seed_plans_and_data()
        first = self.analytics.run('evt', 'a1', snapshot['version'])
        # A later snapshot with more data must not change the stored analysis.
        self.services.record_encounter('evt', 'late1', 'tour-a',
                                       '2027-09-10T14:30:00+08:00', 'w9', 1)
        second_snapshot = self.publish()
        self.assertEqual(second_snapshot['version'], 2)
        stored = self.analytics.get('evt', 'a1')
        self.assertEqual(stored, first)
        self.assertEqual(stored['snapshot_version'], 1)
        with self.assertRaises(DomainError) as caught:
            self.analytics.run('evt', 'a1', 2)
        self.assertEqual(caught.exception.code, 'duplicate_analysis')

    def test_repeat_contact_flag_and_deduped_rate(self):
        snapshot = self._seed_plans_and_data(repeat=True)
        report = self.analytics.run('evt', 'a1', snapshot['version'])
        treatment = next(slot for slot in report['slots'] if slot['arm'] == 'treatment')
        self.assertIn('repeat_contact', treatment['comparability_flags'])
        self.assertEqual(treatment['repeat_contacts'], 1)
        # Unique visitors stay at 6 despite the extra encounter; visits rise.
        self.assertEqual(treatment['unique_visitors'], 6)
        self.assertEqual(treatment['visits'], 7)
        self.assertEqual(treatment['rates']['participation_rate'], '0.6000')

    def test_multi_zone_confound_excludes_slot_from_rates(self):
        snapshot = self._seed_plans_and_data(concurrent_zone=True)
        report = self.analytics.run('evt', 'a1', snapshot['version'])
        treatment = next(slot for slot in report['slots'] if slot['arm'] == 'treatment')
        self.assertIn('multi_zone_same_slot', treatment['comparability_flags'])
        self.assertFalse(treatment['rate_comparable'])
        self.assertFalse(report['comparison']['rate_comparison_available'])
        excluded = report['comparison']['excluded_slots']
        self.assertEqual([row['plan_id'] for row in excluded], ['treat1'])
        # Counts are still reported for the excluded slot.
        self.assertEqual(treatment['visits'], 6)

    def test_missing_attendance_keeps_counts_but_drops_rates(self):
        snapshot = self._seed_plans_and_data(no_attendance=True)
        self.assertFalse(snapshot['pilot_data']['attendance_present'])
        report = self.analytics.run('evt', 'a1', snapshot['version'])
        for slot in report['slots']:
            self.assertIn('missing_attendance', slot['comparability_flags'])
            self.assertIsNone(slot['rates']['participation_rate'])
            self.assertGreaterEqual(slot['visits'], 1)
        self.assertFalse(report['comparison']['rate_comparison_available'])

    def test_slot_adjustment_flag_uses_frozen_assignment(self):
        self.create_event()
        self.plans.register('evt', 'p1', 'treatment', ['tour-a'],
                            TREAT_START, TREAT_END, '初版')
        # Amend before the slot starts; the old version remains stored.
        self.plans.register('evt', 'p1', 'treatment', ['tour-a'],
                            '2027-09-10T10:30:00+08:00', TREAT_END, '推迟开始')
        self.plans.register('evt', 'c1', 'control', ['tour-a'],
                            CTRL_START, CTRL_END, '对照')
        self.open_event()
        self.check_in_many([f'2027-09-10T10:{m:02d}:00+08:00' for m in range(0, 30, 5)])
        self.services.record_encounter('evt', 'e1', 'tour-a',
                                       '2027-09-10T10:45:00+08:00', 'v1', 1)
        self.score_and_close()
        snapshot = self.publish()
        report = self.analytics.run('evt', 'a1', snapshot['version'])
        active = next(slot for slot in report['slots'] if slot['plan_id'] == 'p1')
        self.assertIn('slot_adjusted', active['comparability_flags'])
        self.assertEqual(active['slot_start'], '2027-09-10T10:30:00+08:00')
        used_versions = {(p['plan_id'], p['version']) for p in report['plans_used']}
        self.assertIn(('p1', 1), used_versions)
        self.assertIn(('p1', 2), used_versions)

    def test_decisions_recorded_after_snapshot_are_not_backfilled(self):
        snapshot = self._seed_plans_and_data()
        # New plan decided only after publication cannot enter this analysis.
        self.competition.change_state  # event is closed already; emulate late row
        with self.store.transaction() as db:
            db.execute('INSERT INTO pilot_plans VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       ('evt', 'late1', 1, 'control', '["tour-a"]',
                        '2027-09-10T16:00:00+08:00', '2027-09-10T17:00:00+08:00',
                        '赛后才登记', 'active', None, '2030-01-01T00:00:00+00:00',
                        '2030-01-01T00:00:00+00:00'))
        report = self.analytics.run('evt', 'a1', snapshot['version'])
        plan_ids = {slot['plan_id'] for slot in report['slots']}
        self.assertNotIn('late1', plan_ids)
        self.assertEqual(report['excluded_decisions'][0]['plan_id'], 'late1')
        self.assertEqual(report['excluded_decisions'][0]['reason'], 'decided_after_snapshot')

    def test_snapshot_without_pilot_data_is_rejected(self):
        self.create_event()
        self.plans.register('evt', 'p1', 'treatment', ['tour-a'],
                            TREAT_START, TREAT_END, '试点')
        self.open_event()
        self.score_and_close()
        # Hand-craft a legacy snapshot payload without pilot_data.
        import json
        with self.store.transaction() as db:
            event = self.store.one(db, 'SELECT * FROM events WHERE id=?', ('evt',))
            legacy = {'version': 1, 'event': event, 'legacy': True}
            db.execute('INSERT INTO snapshots VALUES(?,?,?,?,?,?)',
                       ('evt', 1, event['revision'], '2027-09-10T18:00:00+00:00',
                        'published', json.dumps(legacy)))
        with self.assertRaises(DomainError) as caught:
            self.analytics.run('evt', 'a1', 1)
        self.assertEqual(caught.exception.code, 'pilot_data_unavailable')

    def test_no_eligible_decisions_rejected(self):
        self.create_event()
        self.open_event()
        self.score_and_close()
        snapshot = self.publish()
        with self.assertRaises(DomainError) as caught:
            self.analytics.run('evt', 'a1', snapshot['version'])
        self.assertEqual(caught.exception.code, 'no_pilot_decisions')

    def test_analysis_requires_published_snapshot(self):
        self.create_event()
        self.plans.register('evt', 'p1', 'treatment', ['tour-a'],
                            TREAT_START, TREAT_END, '试点')
        with self.assertRaises(DomainError) as caught:
            self.analytics.run('evt', 'a1', 99)
        self.assertEqual(caught.exception.code, 'snapshot_not_found')

    def test_list_analyses_metadata(self):
        snapshot = self._seed_plans_and_data()
        self.analytics.run('evt', 'a1', snapshot['version'])
        listing = self.analytics.list_analyses('evt')
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]['analysis_id'], 'a1')
        self.assertEqual(listing[0]['ruleset_version'], 'pilot-v1')


if __name__ == '__main__':
    unittest.main()

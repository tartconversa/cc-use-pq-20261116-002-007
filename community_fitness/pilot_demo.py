"""End-to-end demonstration of the tourism-guide pilot registry and analysis.

Run from the repository root:

    python -m community_fitness.pilot_demo

The scenario uses a future-dated synthetic event so every pilot decision is
recorded before its slot starts (the no-lookahead rule). It shows a treatment
slot with a guide versus an unstaffed control slot, a repeat-contact visitor,
and an event variant with no attendance data to show how the missing-attendance
flag drives exclusion. The overlapping-zone confound is covered by the tests.
"""
import json
import tempfile
from pathlib import Path

from .attendance import Attendance
from .competition import Competition
from .pilot_analytics import PilotAnalytics
from .pilot_plans import PilotPlans
from .publication import Publication
from .scoring_sessions import ScoringSessions
from .services import Services
from .storage import Store

EVENT = 'pilot2027'
START = '2027-09-10T08:00:00+08:00'
END = '2027-09-10T19:00:00+08:00'


def _build_event(store: Store, *, with_attendance: bool) -> int:
    competition = Competition(store)
    services = Services(store)
    sessions = ScoringSessions(store)
    plans = PilotPlans(store)
    attendance = Attendance(store)

    competition.create_event(EVENT, '文旅引导员试点活动', START, END, 'Asia/Shanghai')
    competition.add_group(EVENT, 'dance', '健身操',
                          {'form': 60, 'rhythm': 40}, 2, 3)
    for n in range(4):
        competition.register_team(EVENT, f't{n}', 'dance', f'队伍{n}', '青浦', 6)
    for n in range(2):
        competition.assign_judge(EVENT, f'j{n}', f'裁判{n}', 'dance')
    services.add_zone(EVENT, 'tour-a', '文旅咨询台', 'tourism',
                      '2027-09-10T09:00:00+08:00', '2027-09-10T18:00:00+08:00')
    # Tea counter is open between the two pilot slots, so it does not confound
    # either comparison window. Overlapping zones are exercised in the tests.
    services.add_zone(EVENT, 'tea', '中医茶饮', 'tea',
                      '2027-09-10T12:00:00+08:00', '2027-09-10T13:00:00+08:00')

    # Decisions are all made before the event opens.
    plans.register(EVENT, 'morning-guide', 'treatment', ['tour-a'],
                   '2027-09-10T10:00:00+08:00', '2027-09-10T11:00:00+08:00',
                   '上午安排文旅咨询引导员')
    plans.register(EVENT, 'afternoon-plain', 'control', ['tour-a'],
                   '2027-09-10T14:00:00+08:00', '2027-09-10T15:00:00+08:00',
                   '下午不安排引导员，作为对照')

    revision = store.inspect(EVENT)['revision']
    competition.change_state(EVENT, 'open', revision)
    sessions.open(EVENT, 'dance', store.inspect(EVENT)['revision'])

    if with_attendance:
        for index in range(20):
            hour = 10 if index < 10 else 14
            minute = (index % 10) * 5
            attendance.check_in(EVENT, f't{index // 6}', f'm{index}', 'north',
                                 f'2027-09-10T{hour:02d}:{minute:02d}:00+08:00',
                                 f'r{index}')

    # Treatment slot: six unique visitors, one of them returns (repeat contact).
    for i, token in enumerate(['v1', 'v2', 'v3', 'v4', 'v5', 'v6']):
        services.record_encounter(
            EVENT, f'te{i}', 'tour-a',
            f'2027-09-10T10:{5 + i * 7:02d}:00+08:00', token, 1)
    services.record_encounter(EVENT, 'te-repeat', 'tour-a',
                              '2027-09-10T10:55:00+08:00', 'v1', 1)
    # Control slot: four unique visitors.
    for i, token in enumerate(['w1', 'w2', 'w3', 'w4']):
        services.record_encounter(
            EVENT, f'ce{i}', 'tour-a',
            f'2027-09-10T14:{5 + i * 10:02d}:00+08:00', token, 1)

    for team in ('t0', 't1', 't2', 't3'):
        for judge in ('j0', 'j1'):
            competition.submit_score(EVENT, team, judge,
                                     {'form': 52, 'rhythm': 34}, f'{team}-{judge}')
    sessions.close(EVENT, 'dance', store.inspect(EVENT)['revision'])
    competition.change_state(EVENT, 'closed', store.inspect(EVENT)['revision'])
    publication = Publication(store)
    preview = publication.preview(EVENT)
    snapshot = publication.publish(EVENT, preview['head_version'],
                                   preview['event']['revision'])
    return snapshot['version']


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / 'pilot.sqlite3')
        version = _build_event(store, with_attendance=True)
        analytics = PilotAnalytics(store)
        report = analytics.run(EVENT, 'analysis-1', version)
        summary = {
            'snapshot_version': report['snapshot_version'],
            'ruleset_version': report['ruleset_version'],
            'slot_flags': {slot['plan_id']: slot['comparability_flags']
                           for slot in report['slots']},
            'rates': {arm: data['pooled_participation_rate']
                      for arm, data in report['comparison']['arms'].items()},
            'uplift_pp': report['comparison']['uplift'][
                'participation_rate_difference_pp'],
            'rate_comparison_available':
                report['comparison']['rate_comparison_available'],
            'excluded_slots': report['summary']['excluded_slots'],
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))

        # A second event with no attendance records: counts survive, rates do
        # not, and every slot is flagged as non-comparable.
        store2_path = Path(directory) / 'pilot-no-attendance.sqlite3'
        store2 = Store(store2_path)
        no_att_version = _build_event(store2, with_attendance=False)
        no_att_report = PilotAnalytics(store2).run(EVENT, 'analysis-x', no_att_version)
        print(json.dumps({
            'attendance_present': False,
            'rate_comparison_available':
                no_att_report['comparison']['rate_comparison_available'],
            'treatment_visits_still_reported':
                no_att_report['comparison']['arms']['treatment']['visits'],
        }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

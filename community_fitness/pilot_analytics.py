import json
import sqlite3
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

from .errors import DomainError, require
from .storage import Store
from .validation import identifier, instant, utc_now

RULESET_VERSION = 'pilot-v1'
RATE_PLACES = Decimal('0.0001')
PP_PLACES = Decimal('0.01')


def _quantize(value: Decimal, places: Decimal = RATE_PLACES) -> str:
    return str(value.quantize(places, rounding=ROUND_HALF_UP))


class PilotAnalytics:
    """Explainable treatment/control analysis of tourism-guide pilot slots.

    Analysis only reads an immutable published snapshot (never the mutable live
    tables) and only pilot plan versions decided before that snapshot was
    published. The resulting report is itself frozen, including the exact plan
    rows, ruleset version and source revision it used, so later corrections can
    never rewrite an earlier conclusion.
    """

    def __init__(self, store: Store):
        self.store = store

    def run(self, event_id: str, analysis_id: str, snapshot_version: int) -> dict:
        identifier(analysis_id, 'analysis id')
        require(type(snapshot_version) is int and snapshot_version > 0,
                'invalid_version', 'Snapshot version must be positive')
        now = utc_now()
        with self.store.transaction() as db:
            self.store.one(db, 'SELECT id FROM events WHERE id=?', (event_id,))
            snapshot = db.execute(
                'SELECT * FROM snapshots WHERE event_id=? AND version=? AND status=?',
                (event_id, snapshot_version, 'published')).fetchone()
            require(snapshot is not None, 'snapshot_not_found',
                    'Published snapshot does not exist', 404)
            payload = json.loads(snapshot['payload'])
            require('pilot_data' in payload, 'pilot_data_unavailable',
                    'This snapshot predates pilot data publication; publish a new version', 400)
            pilot_data = payload['pilot_data']
            published_at = instant(snapshot['created_at'])
            all_plans = self.store.many(
                db, 'SELECT * FROM pilot_plans WHERE event_id=? ORDER BY plan_id,version',
                (event_id,))
            eligible, excluded_decisions = [], []
            for row in all_plans:
                item = PilotPlansRow(row)
                if item.decided_at > published_at:
                    excluded_decisions.append({'plan_id': item.plan_id, 'version': item.version,
                                               'reason': 'decided_after_snapshot',
                                               'decided_at': item.decided_at_iso})
                else:
                    eligible.append(item)
            require(eligible, 'no_pilot_decisions',
                    'No pilot plan was decided before this snapshot was published', 400)
            report = self._build(payload, pilot_data, eligible)
            report.update({
                'event_id': event_id, 'analysis_id': analysis_id,
                'snapshot_version': snapshot_version,
                'source_revision': snapshot['source_revision'],
                'ruleset_version': RULESET_VERSION,
                'excluded_decisions': excluded_decisions,
                'created_at': now,
            })
            try:
                db.execute('INSERT INTO pilot_analyses(event_id,analysis_id,snapshot_version,'
                           'source_revision,ruleset_version,payload,created_at) '
                           'VALUES(?,?,?,?,?,?,?)',
                           (event_id, analysis_id, snapshot_version,
                            snapshot['source_revision'], RULESET_VERSION,
                            json.dumps(report, ensure_ascii=False, sort_keys=True), now))
            except sqlite3.IntegrityError as exc:
                raise DomainError('duplicate_analysis', 'Analysis id already exists', 409) from exc
            self.store.audit(db, event_id, 'pilot_analysis_run', 'pilot_analysis', analysis_id,
                             {'snapshot_version': snapshot_version,
                              'ruleset_version': RULESET_VERSION,
                              'comparable_slots': report['summary']['comparable_slots'],
                              'excluded_slots': report['summary']['excluded_slots']}, now)
            return report

    def _build(self, snapshot_payload: dict, pilot_data: dict,
               plans: list) -> dict:
        event = snapshot_payload['event']
        zone_meta = {row['code']: row for row in pilot_data['zones']}
        attendance_present = pilot_data['attendance_present']
        arrivals = [(instant(row['arrived_at']),) for row in pilot_data['arrivals']]
        encounters = pilot_data['encounters']

        slots = []
        for item in plans:
            if item.state != 'active':
                continue
            flags = set()
            notes = []
            if self._has_earlier_versions(item, plans):
                flags.add('slot_adjusted')
                notes.append('该时段方案在快照发布前被修订过，分组按当前有效版本冻结，旧版本仍可追溯')
            concurrent = self._concurrent_other_zones(item, zone_meta)
            if concurrent:
                flags.add('multi_zone_same_slot')
                notes.append('该时段同时开放其他专区 ' + '、'.join(concurrent)
                             + '，访客可能被分流，剔除出比率对照')
            for code in item.zone_codes:
                if code not in zone_meta:
                    flags.add('missing_attendance')
                    notes.append(f'专区 {code} 在快照中不存在')
            if not attendance_present:
                flags.add('missing_attendance')
                notes.append('整场缺少匿名到场数据，参与率无法计算，仅保留服务计数并剔除出比率对照')
            stats = self._slot_stats(item, encounters, arrivals, attendance_present)
            if stats['repeat_contacts'] > 0:
                flags.add('repeat_contact')
                notes.append(f'存在 {stats["repeat_contacts"]} 次同一匿名访客重复接触，'
                             '参与率按去重访客计算，接触次数另行报告')
            slot_tokens = stats.pop('_tokens')
            slots.append({
                'plan_id': item.plan_id, 'version': item.version, 'arm': item.arm,
                'zone_codes': item.zone_codes,
                'slot_start': item.slot_start, 'slot_end': item.slot_end,
                'decided_at': item.decided_at_iso,
                'comparability_flags': sorted(flags), 'comparability_notes': notes,
                'rate_comparable': attendance_present
                and 'multi_zone_same_slot' not in flags,
                '_tokens': slot_tokens,
                **stats,
            })
        comparison = self._comparison(slots)
        for slot in slots:
            slot.pop('_tokens', None)
        return {
            'timezone': event['timezone'],
            'methodology': {
                'data_source': 'published_snapshot',
                'participation_rate': 'unique tourism visitors in slot / arrivals during slot',
                'exposure_rate': 'service visits in slot / arrivals during slot',
                'arm_assignment': 'only plan versions decided before snapshot publication',
                'rate_exclusion': 'slots with missing attendance or overlapping zone plans',
                'interpretation': '描述性前后/组间对照，不构成随机实验的因果推断',
            },
            'slots': slots, 'comparison': comparison,
            'summary': {
                'slot_count': len(slots),
                'comparable_slots': comparison['comparable_slots'],
                'excluded_slots': comparison['excluded_slots'],
                'rate_comparison_available': comparison['rate_comparison_available'],
            },
            'plans_used': [item.to_dict() for item in plans],
        }

    @staticmethod
    def _has_earlier_versions(item: 'PilotPlansRow', plans: list) -> bool:
        return any(other.plan_id == item.plan_id and other.version < item.version
                   for other in plans)

    @staticmethod
    def _concurrent_other_zones(item: 'PilotPlansRow', zone_meta: dict) -> list:
        """Other service zones open at any point during the plan slot."""
        start, end = instant(item.slot_start), instant(item.slot_end)
        own = set(item.zone_codes)
        concurrent = []
        for code, zone_row in sorted(zone_meta.items()):
            if code in own:
                continue
            opens = instant(zone_row['opens_at'])
            closes = instant(zone_row['closes_at'])
            if opens < end and start < closes:
                concurrent.append(code)
        return concurrent

    def _slot_stats(self, item: 'PilotPlansRow', encounters: list,
                    arrivals: list, attendance_present: bool) -> dict:
        start, end = instant(item.slot_start), instant(item.slot_end)
        zones = set(item.zone_codes)
        in_slot = [row for row in encounters
                   if row['zone_code'] in zones
                   and start <= instant(row['occurred_at']) < end]
        visits = sum(row['count'] for row in in_slot)
        tokens = [row['visitor_token'] for row in in_slot]
        unique = set(tokens)
        token_encounters = defaultdict(int)
        for token in tokens:
            token_encounters[token] += 1
        repeat_contacts = sum(count - 1 for count in token_encounters.values() if count > 1)
        slot_arrivals = sum(1 for (arrived,) in arrivals if start <= arrived < end)
        rates = None
        if attendance_present and slot_arrivals > 0:
            rates = {
                'arrivals': slot_arrivals,
                'participation_rate': _quantize(Decimal(len(unique)) / Decimal(slot_arrivals)),
                'exposure_rate': _quantize(Decimal(visits) / Decimal(slot_arrivals)),
            }
        elif attendance_present:
            rates = {'arrivals': 0, 'participation_rate': None, 'exposure_rate': None,
                     'note': '该时段到场为零，比率分母为零，不参与比率对照'}
        else:
            rates = {'arrivals': None, 'participation_rate': None, 'exposure_rate': None,
                     'note': '缺少到场数据，比率无法计算'}
        return {'encounters': len(in_slot), 'visits': visits,
                'unique_visitors': len(unique), 'repeat_contacts': repeat_contacts,
                'rates': rates, '_tokens': unique}

    def _comparison(self, slots: list) -> dict:
        by_arm = defaultdict(list)
        for slot in slots:
            by_arm[slot['arm']].append(slot)
        arms = {}
        excluded = []
        for arm, rows in by_arm.items():
            rate_rows = [row for row in rows if row['rate_comparable']
                         and row['rates']['participation_rate'] is not None]
            total_arrivals = sum(row['rates']['arrivals'] for row in rate_rows)
            total_unique = sum(row['unique_visitors'] for row in rate_rows)
            total_visits = sum(row['visits'] for row in rate_rows)
            arms[arm] = {
                'slots': len(rows), 'rate_comparable_slots': len(rate_rows),
                'visits': sum(row['visits'] for row in rows),
                'encounters': sum(row['encounters'] for row in rows),
                'unique_visitors': len(set().union(*(row['_tokens'] for row in rows))),
                'pooled_arrivals': total_arrivals,
                'pooled_participation_rate': _quantize(
                    Decimal(total_unique) / Decimal(total_arrivals)) if total_arrivals else None,
                'pooled_exposure_rate': _quantize(
                    Decimal(total_visits) / Decimal(total_arrivals)) if total_arrivals else None,
            }
            for row in rows:
                if not row['rate_comparable'] or row['rates']['participation_rate'] is None:
                    excluded.append({'plan_id': row['plan_id'], 'version': row['version'],
                                     'arm': row['arm'],
                                     'flags': row['comparability_flags']})

        def pooled(arm: str, field: str):
            arm_row = arms.get(arm)
            if not arm_row or arm_row[field] is None:
                return None
            return Decimal(arm_row[field])

        uplift = None
        treatment = pooled('treatment', 'pooled_participation_rate')
        control = pooled('control', 'pooled_participation_rate')
        if treatment is not None and control is not None:
            pp = (treatment - control).quantize(PP_PLACES, rounding=ROUND_HALF_UP)
            uplift = {
                'participation_rate_difference_pp': str(pp),
                'treatment_rate': _quantize(treatment),
                'control_rate': _quantize(control),
                'explanation': '差值为试点（引导员）时段合并参与率减去对照时段合并参与率，单位百分点；'
                               '已剔除非可比时段，仅描述同时段口径下的差异',
            }
        else:
            uplift = {'participation_rate_difference_pp': None,
                      'explanation': '试点或对照缺少带有效到场分母的可比时段，无法给出参与率差值；'
                                     '请参考服务计数与剔除原因'}
        return {'arms': arms, 'uplift': uplift,
                'rate_comparison_available': treatment is not None and control is not None,
                'comparable_slots': sum(row['rate_comparable_slots']
                                        for row in arms.values()),
                'excluded_slots': excluded}

    def get(self, event_id: str, analysis_id: str) -> dict:
        with self.store.connection() as db:
            self.store.one(db, 'SELECT id FROM events WHERE id=?', (event_id,))
            row = db.execute('SELECT payload FROM pilot_analyses WHERE event_id=? AND analysis_id=?',
                             (event_id, analysis_id)).fetchone()
            require(row is not None, 'not_found', 'Pilot analysis does not exist', 404)
            return json.loads(row['payload'])

    def list_analyses(self, event_id: str) -> list[dict]:
        with self.store.connection() as db:
            self.store.one(db, 'SELECT id FROM events WHERE id=?', (event_id,))
            rows = self.store.many(
                db, 'SELECT event_id,analysis_id,snapshot_version,source_revision,'
                    'ruleset_version,created_at FROM pilot_analyses WHERE event_id=? '
                    'ORDER BY created_at,analysis_id', (event_id,))
            return rows


class PilotPlansRow:
    def __init__(self, row: dict):
        self.plan_id = row['plan_id']
        self.version = row['version']
        self.arm = row['arm']
        self.zone_codes = json.loads(row['zone_codes'])
        self.slot_start = row['slot_start']
        self.slot_end = row['slot_end']
        self.rationale = row['rationale']
        self.state = row['state']
        self.superseded_by = row['superseded_by']
        self.decided_at_iso = row['decided_at']
        self.decided_at = instant(row['decided_at'])

    def to_dict(self) -> dict:
        return {'plan_id': self.plan_id, 'version': self.version, 'arm': self.arm,
                'zone_codes': self.zone_codes, 'slot_start': self.slot_start,
                'slot_end': self.slot_end, 'rationale': self.rationale,
                'state': self.state, 'superseded_by': self.superseded_by,
                'decided_at': self.decided_at_iso}

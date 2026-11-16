import json
import sqlite3

from .errors import DomainError, require
from .storage import Store
from .validation import identifier, instant, nonempty, order, utc_now


class PilotPlans:
    """Append-only, timestamped registry of tourism-guide pilot plans.

    A plan binds one or more tourism zones to a pilot arm for a single time
    slot. Every change creates a new immutable version; superseded and
    cancelled rows are retained so historical analyses can be reproduced.
    Writes are refused once a covered slot has started, which prevents pilot
    assignments from being backfilled from results observed afterwards.
    """

    def __init__(self, store: Store):
        self.store = store

    def register(self, event_id: str, plan_id: str, arm: str, zone_codes: list[str],
                 slot_start: str, slot_end: str, rationale: str) -> dict:
        identifier(plan_id, 'plan id')
        require(arm in {'treatment', 'control'}, 'invalid_pilot_arm',
                'Pilot arm must be treatment or control')
        require(isinstance(zone_codes, list) and zone_codes, 'invalid_pilot_zones',
                'At least one zone code is required')
        zone_codes = [identifier(code, 'zone code') for code in zone_codes]
        require(len(zone_codes) == len(set(zone_codes)), 'invalid_pilot_zones',
                'Zone codes must not repeat')
        order(slot_start, slot_end)
        rationale = nonempty(rationale, 'rationale', 500)
        now = utc_now()
        now_instant = instant(now)
        with self.store.transaction() as db:
            event = self.store.one(db, 'SELECT * FROM events WHERE id=?', (event_id,))
            require(event['state'] in {'draft', 'open'}, 'event_locked',
                    'Pilot plans cannot change after the event closes', 409)
            # No-lookahead gate first: a slot that has already begun can never
            # receive a pilot decision, regardless of zone configuration.
            require(now_instant < instant(slot_start), 'pilot_slot_started',
                    'Slot already started; pilot grouping cannot be decided in hindsight', 409)
            zones = {row['code']: row for row in self.store.many(
                db, 'SELECT * FROM service_zones WHERE event_id=?', (event_id,))}
            for code in zone_codes:
                zone_row = zones.get(code)
                require(zone_row is not None, 'unknown_zone', f'Zone {code} does not exist')
                require(zone_row['kind'] == 'tourism', 'invalid_pilot_zone_kind',
                        'Pilot plans only apply to tourism consultation zones')
                require(instant(zone_row['opens_at']) <= instant(slot_start)
                        and instant(slot_end) <= instant(zone_row['closes_at']),
                        'outside_zone_hours', 'Pilot slot must lie inside every selected zone hours')
            prior = self.store.many(
                db, 'SELECT * FROM pilot_plans WHERE event_id=? AND plan_id=? ORDER BY version',
                (event_id, plan_id))
            version = (prior[-1]['version'] + 1) if prior else 1
            if prior:
                last = prior[-1]
                require(last['state'] != 'cancelled', 'pilot_plan_cancelled',
                        'Cancelled plans cannot be amended; register a new plan id', 409)
                require(last['state'] == 'active', 'pilot_plan_not_active',
                        'Only the active plan version can be superseded', 409)
                require(now_instant < instant(last['slot_start']), 'pilot_slot_started',
                        'Original slot already started; its grouping is frozen', 409)
            # No two live plan versions may cover the same zone at the same
            # instant, otherwise treatment/control status would be ambiguous.
            live = [row for row in self.store.many(
                db, 'SELECT * FROM pilot_plans WHERE event_id=? AND state=?',
                (event_id, 'active')) if row['plan_id'] != plan_id]
            for row in live:
                overlap_zones = set(json.loads(row['zone_codes'])) & set(zone_codes)
                if overlap_zones and instant(row['slot_start']) < instant(slot_end) \
                        and instant(slot_start) < instant(row['slot_end']):
                    raise DomainError('pilot_overlap',
                                      f'Zone {sorted(overlap_zones)[0]} already has a live plan '
                                      'covering this slot', 409)
            try:
                db.execute('INSERT INTO pilot_plans VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                           (event_id, plan_id, version, arm,
                            json.dumps(zone_codes, sort_keys=True), slot_start, slot_end,
                            rationale, 'active', None, now, now))
            except sqlite3.IntegrityError as exc:
                raise DomainError('duplicate_pilot_plan', 'Pilot plan version already exists',
                                  409) from exc
            if prior:
                db.execute('UPDATE pilot_plans SET state=?,superseded_by=? '
                           'WHERE event_id=? AND plan_id=? AND version=?',
                           ('superseded', version, event_id, plan_id, version - 1))
            self.store.bump(db, event_id)
            self.store.audit(db, event_id, 'pilot_plan_registered', 'pilot_plan', plan_id,
                             {'version': version, 'arm': arm, 'zones': zone_codes,
                              'slot_start': slot_start, 'slot_end': slot_end}, now)
            return self._render(event_id, plan_id, version, arm, zone_codes, slot_start,
                                slot_end, rationale, 'active', None, now)

    def cancel(self, event_id: str, plan_id: str, rationale: str) -> dict:
        identifier(plan_id, 'plan id')
        rationale = nonempty(rationale, 'rationale', 500)
        now = utc_now()
        with self.store.transaction() as db:
            self.store.one(db, 'SELECT id FROM events WHERE id=?', (event_id,))
            last = db.execute('SELECT * FROM pilot_plans WHERE event_id=? AND plan_id=? '
                              'ORDER BY version DESC LIMIT 1', (event_id, plan_id)).fetchone()
            require(last is not None, 'not_found', 'Pilot plan does not exist', 404)
            require(dict(last)['state'] == 'active', 'pilot_plan_not_active',
                    'Only an active plan version can be cancelled', 409)
            require(instant(now) < instant(last['slot_start']), 'pilot_slot_started',
                    'Slot already started; the decision cannot be withdrawn in hindsight', 409)
            version = last['version'] + 1
            zones = json.loads(last['zone_codes'])
            db.execute('UPDATE pilot_plans SET state=? WHERE event_id=? AND plan_id=? AND version=?',
                       ('cancelled', event_id, plan_id, last['version']))
            try:
                db.execute('INSERT INTO pilot_plans VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                           (event_id, plan_id, version, last['arm'], last['zone_codes'],
                            last['slot_start'], last['slot_end'], rationale, 'cancelled',
                            None, now, now))
            except sqlite3.IntegrityError as exc:
                raise DomainError('duplicate_pilot_plan', 'Pilot plan version already exists',
                                  409) from exc
            self.store.bump(db, event_id)
            self.store.audit(db, event_id, 'pilot_plan_cancelled', 'pilot_plan', plan_id,
                             {'version': version, 'zones': zones}, now)
            return self._render(event_id, plan_id, version, last['arm'], zones,
                                last['slot_start'], last['slot_end'], rationale,
                                'cancelled', None, now)

    def history(self, event_id: str, plan_id: str) -> list[dict]:
        with self.store.connection() as db:
            self.store.one(db, 'SELECT id FROM events WHERE id=?', (event_id,))
            rows = self.store.many(
                db, 'SELECT * FROM pilot_plans WHERE event_id=? AND plan_id=? ORDER BY version',
                (event_id, plan_id))
            require(rows, 'not_found', 'Pilot plan does not exist', 404)
            return [self._row_to_dict(row) for row in rows]

    def list_plans(self, event_id: str) -> list[dict]:
        with self.store.connection() as db:
            self.store.one(db, 'SELECT id FROM events WHERE id=?', (event_id,))
            rows = self.store.many(
                db, 'SELECT * FROM pilot_plans WHERE event_id=? ORDER BY plan_id,version',
                (event_id,))
            return [self._row_to_dict(row) for row in rows]

    @staticmethod
    def _row_to_dict(row: dict) -> dict:
        return {'plan_id': row['plan_id'], 'version': row['version'], 'arm': row['arm'],
                'zone_codes': json.loads(row['zone_codes']), 'slot_start': row['slot_start'],
                'slot_end': row['slot_end'], 'rationale': row['rationale'],
                'state': row['state'], 'superseded_by': row['superseded_by'],
                'decided_at': row['decided_at'], 'created_at': row['created_at']}

    @staticmethod
    def _render(event_id, plan_id, version, arm, zone_codes, slot_start, slot_end,
                rationale, state, superseded_by, decided_at) -> dict:
        return {'event_id': event_id, 'plan_id': plan_id, 'version': version, 'arm': arm,
                'zone_codes': zone_codes, 'slot_start': slot_start, 'slot_end': slot_end,
                'rationale': rationale, 'state': state, 'superseded_by': superseded_by,
                'decided_at': decided_at}

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from community_fitness.api import create_app
from tests.helpers import Scenario, START, END, TEN


class ApiTests(Scenario):
    def setUp(self):
        super().setUp()
        self.client = TestClient(create_app(Path(self.temp.name) / 'api.sqlite3'))

    def test_create_and_get_event(self):
        body = {'id': 'api1', 'name': 'Open day', 'starts_at': START,
                'ends_at': END, 'timezone': 'Asia/Shanghai'}
        response = self.client.post('/events', json=body)
        self.assertEqual(response.status_code, 201)
        loaded = self.client.get('/events/api1')
        self.assertEqual(loaded.json()['name'], 'Open day')

    def test_error_payload(self):
        response = self.client.get('/events/not-there')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()['error'], 'not_found')

    def test_api_full_scoring_and_export(self):
        self.client.post('/events', json={'id': 'api1', 'name': 'Open day',
                                         'starts_at': START, 'ends_at': END,
                                         'timezone': 'Asia/Shanghai'})
        group = {'code': 'dance', 'label': 'Dance', 'criteria': {'form': 60, 'music': 40},
                 'min_judges': 2, 'max_judges': 2}
        self.assertEqual(self.client.post('/events/api1/groups', json=group).status_code, 201)
        team = {'id': 't1', 'group_code': 'dance', 'name': 'Local team',
                'district': 'Qingpu', 'member_count': 2}
        self.assertEqual(self.client.post('/events/api1/teams', json=team).status_code, 201)
        for judge in ('j1', 'j2'):
            body = {'id': judge, 'name': judge, 'group_code': 'dance'}
            self.assertEqual(self.client.post('/events/api1/judges', json=body).status_code, 201)
        revision = self.client.get('/events/api1').json()['revision']
        self.client.post('/events/api1/state', json={'target': 'open',
                                                     'expected_revision': revision})
        revision = self.client.get('/events/api1').json()['revision']
        self.client.post('/events/api1/groups/dance/session',
                         json={'expected_event_revision': revision})
        for judge in ('j1', 'j2'):
            response = self.client.post('/events/api1/scores', json={
                'team_id': 't1', 'judge_id': judge,
                'scores': {'form': 55, 'music': 35}, 'request_key': judge})
            self.assertEqual(response.status_code, 200)
        revision = self.client.get('/events/api1').json()['revision']
        self.client.post('/events/api1/groups/dance/session/close',
                         json={'expected_event_revision': revision})
        revision = self.client.get('/events/api1').json()['revision']
        self.client.post('/events/api1/state', json={'target': 'closed',
                                                     'expected_revision': revision})
        preview = self.client.get('/events/api1/preview').json()
        published = self.client.post('/events/api1/publish', json={
            'expected_head': 0, 'expected_revision': preview['event']['revision']})
        self.assertEqual(published.status_code, 200)
        self.assertEqual(published.json()['scores']['groups'][0]['teams'][0]['score'], '90.00')
        exported = self.client.get('/events/api1/export/standings.csv')
        self.assertEqual(exported.status_code, 200)
        self.assertIn('Local team', exported.text)

    def test_api_validation_error(self):
        response = self.client.post('/events', json={'id': 'x'})
        self.assertEqual(response.status_code, 422)

    def test_api_health(self):
        self.assertEqual(self.client.get('/health').json(), {'status': 'ok'})

    def test_api_pilot_plan_and_analysis_flow(self):
        start = '2027-09-10T08:00:00+08:00'
        end = '2027-09-10T19:00:00+08:00'
        self.client.post('/events', json={'id': 'api2', 'name': 'Pilot day',
                                          'starts_at': start, 'ends_at': end,
                                          'timezone': 'Asia/Shanghai'})
        group = {'code': 'dance', 'label': 'Dance', 'criteria': {'form': 60, 'music': 40},
                 'min_judges': 2, 'max_judges': 2}
        self.assertEqual(self.client.post('/events/api2/groups', json=group).status_code, 201)
        team = {'id': 't1', 'group_code': 'dance', 'name': 'Local team',
                'district': 'Qingpu', 'member_count': 4}
        self.assertEqual(self.client.post('/events/api2/teams', json=team).status_code, 201)
        for judge in ('j1', 'j2'):
            self.assertEqual(self.client.post('/events/api2/judges',
                                              json={'id': judge, 'name': judge,
                                                    'group_code': 'dance'}).status_code, 201)
        zone = {'code': 'tour-a', 'label': '文旅咨询', 'kind': 'tourism',
                'opens_at': '2027-09-10T09:00:00+08:00',
                'closes_at': '2027-09-10T18:00:00+08:00'}
        self.assertEqual(self.client.post('/events/api2/zones', json=zone).status_code, 201)
        plan = {'plan_id': 'p1', 'arm': 'treatment', 'zone_codes': ['tour-a'],
                'slot_start': '2027-09-10T10:00:00+08:00',
                'slot_end': '2027-09-10T11:00:00+08:00', 'rationale': '引导员试点'}
        self.assertEqual(self.client.post('/events/api2/pilot/plans',
                                          json=plan).status_code, 201)
        listed = self.client.get('/events/api2/pilot/plans').json()
        self.assertEqual(listed[0]['plan_id'], 'p1')
        self.assertEqual(self.client.get('/events/api2/pilot/plans/p1').json()[0]['version'], 1)
        # Backdated decisions are refused through the API.
        bad = dict(plan, plan_id='p2', slot_start='2026-01-01T10:00:00+08:00',
                   slot_end='2026-01-01T11:00:00+08:00')
        refused = self.client.post('/events/api2/pilot/plans', json=bad)
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.json()['error'], 'pilot_slot_started')
        revision = self.client.get('/events/api2').json()['revision']
        self.client.post('/events/api2/state', json={'target': 'open',
                                                     'expected_revision': revision})
        revision = self.client.get('/events/api2').json()['revision']
        self.client.post('/events/api2/groups/dance/session',
                         json={'expected_event_revision': revision})
        for judge in ('j1', 'j2'):
            self.client.post('/events/api2/scores', json={
                'team_id': 't1', 'judge_id': judge,
                'scores': {'form': 55, 'music': 35}, 'request_key': judge})
        for member in range(4):
            self.client.post('/events/api2/attendance', json={
                'team_id': 't1', 'member_ref': f'm{member}', 'gate': 'north',
                'arrived_at': f'2027-09-10T10:{member * 10:02d}:00+08:00',
                'request_key': f'r{member}'})
        self.client.post('/events/api2/encounters', json={
            'id': 'e1', 'zone_code': 'tour-a',
            'occurred_at': '2027-09-10T10:30:00+08:00',
            'visitor_token': 'v1', 'count': 1})
        revision = self.client.get('/events/api2').json()['revision']
        self.client.post('/events/api2/groups/dance/session/close',
                         json={'expected_event_revision': revision})
        revision = self.client.get('/events/api2').json()['revision']
        self.client.post('/events/api2/state', json={'target': 'closed',
                                                     'expected_revision': revision})
        preview = self.client.get('/events/api2/preview').json()
        published = self.client.post('/events/api2/publish', json={
            'expected_head': 0, 'expected_revision': preview['event']['revision']})
        self.assertEqual(published.status_code, 200)
        version = published.json()['version']
        analysis = self.client.post('/events/api2/pilot/analyses',
                                    json={'analysis_id': 'a1', 'snapshot_version': version})
        self.assertEqual(analysis.status_code, 201)
        body = analysis.json()
        self.assertEqual(body['slots'][0]['unique_visitors'], 1)
        self.assertEqual(body['ruleset_version'], 'pilot-v1')
        fetched = self.client.get('/events/api2/pilot/analyses/a1')
        self.assertEqual(fetched.json()['analysis_id'], 'a1')
        self.assertEqual(len(self.client.get('/events/api2/pilot/analyses').json()), 1)

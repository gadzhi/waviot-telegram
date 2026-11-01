from datetime import datetime, timedelta, timezone
import json

from access_helpers import employee
from waviot_telegram.access import Access
from waviot_telegram.delivery import deliver
from waviot_telegram.storage import Store
from waviot_telegram.telegram_actions import BotActions

NOW = datetime(2026, 10, 31, 13, tzinfo=timezone.utc)


class API:
    def __init__(self, status='administrator'):
        self.calls = []
        self.status = status

    def call(self, method, **payload):
        self.calls.append((method, payload))
        return [{'status': self.status, 'user': {'id': 1}}] if method == 'getChatAdministrators' else {'message_id': len(self.calls)}


def message(i, actor, text=None, contact=None, chat=None):
    data = {'from': {'id': actor}, 'chat': {'id': actor if chat is None else chat, 'type': 'private' if chat is None else 'supergroup'}}
    if text is not None:
        data['text'] = text
    if contact:
        data['contact'] = contact
    return {'update_id': i, 'message': data}


def run(store, api, *updates, now=NOW):
    store.ingest(updates)
    BotActions(store, api).process(now)
    deliver(store, api, now)


def test_registration_own_contact_code_one_use_and_no_plaintext_inbox(tmp_path):
    with Store(tmp_path/'state.db') as s:
        a = Access(s)
        eid, code = a.create('Иван', '+12025550101')
        # Invite expiry in the test's clock.
        code = a.invite(eid, now=NOW)
        api = API()
        run(s, api, message(1, 42, '/start'), message(2, 42, contact={'user_id': 999, 'phone_number': '+12025550101'}), message(3, 42, code))
        assert a.employee(42) is None
        run(s, api, message(4, 42, contact={'user_id': 42, 'phone_number': '12025550101'}))
        s.ingest([message(5, 42, code)])
        persisted = s.db.execute('SELECT payload FROM updates WHERE id=5').fetchone()[0]
        assert code not in persisted and code.replace('-', '') not in persisted
        BotActions(s, api).process(NOW)
        assert a.employee(42)['id'] == eid
        assert not a.register(99, code, NOW)
        row = s.db.execute('SELECT invite_hash,invite_until FROM employees WHERE id=?', (eid,)).fetchone()
        assert row['invite_hash'] is None and row['invite_until'] is None
        assert all(code not in r[0] for r in s.db.execute('SELECT payload FROM updates'))


def test_expired_reissued_and_throttled_invitations(tmp_path):
    with Store(tmp_path/'state.db') as s:
        a = Access(s)
        eid, _ = a.create('Иван', '+12025550101')
        old = a.invite(eid, now=NOW-timedelta(days=2))
        with s.atomic():
            a.remember_contact(42, {'user_id': 42, 'phone_number': '+12025550101'}, NOW)
        assert not a.register(42, old, NOW)
        code = a.invite(eid, now=NOW)
        assert not a.register(42, old, NOW)
        for _ in range(3):
            assert not a.register(42, 'wrong', NOW)
        assert not a.register(42, code, NOW)
        later = NOW+timedelta(minutes=16)
        with s.atomic():
            a.remember_contact(42, {'user_id': 42, 'phone_number': '+12025550101'}, later)
        assert a.register(42, code, later)


def test_group_requires_registered_telegram_admin_and_invitation(tmp_path):
    with Store(tmp_path/'state.db') as s:
        employee(s, 1)
        a = Access(s)
        code = a.group_invite(now=NOW)
        run(s, API(), message(1, 2, '/connect '+code, chat=-100))
        assert s.get('group_id') is None
        run(s, API('member'), message(2, 1, '/connect '+code, chat=-100))
        assert s.get('group_id') is None
        run(s, API(), message(3, 1, '/connect '+code, chat=-100))
        assert s.get('group_id') == -100
        run(s, API(), message(4, 1, '/connect '+code, chat=-200))
        assert s.get('group_id') == -100
        assert all(code not in r[0] for r in s.db.execute('SELECT payload FROM updates'))


def test_revocation_blocks_callbacks_and_already_queued_personal_readings(tmp_path):
    with Store(tmp_path/'state.db') as s:
        employee(s, 1)
        s.set('group_id', -100)
        item = s.open_incident('gas', 'Квартира 1', 'no_data', 'Тест', now=NOW)
        api = API()
        deliver(s, api, NOW)
        s.enqueue('private', 'message', {'text': 'СЕКРЕТНОЕ ПОКАЗАНИЕ', 'chat_id': 1, 'actor_id': 1})
        Access(s).disable(1)
        cb = {'update_id': 1, 'callback_query': {'id': 'cb', 'from': {'id': 1}, 'message': {'chat': {'id': -100, 'type': 'supergroup'}, 'message_id': s.incident(item['id'])['message_id']}, 'data': f"case:{item['id']}:1:claim"}}
        run(s, api, cb)
        assert s.incident(item['id'])['assignee'] is None
        assert not any('СЕКРЕТНОЕ' in p.get('text', '') for _,p in api.calls)
        assert s.db.execute("SELECT state FROM outbox WHERE key='private'").fetchone()[0] == 'skipped'


def test_two_personal_chats_have_distinct_recipients_and_no_group_readings(tmp_path):
    with Store(tmp_path/'state.db') as s:
        employee(s, 1)
        employee(s, 2)
        s.set('group_id', -100)
        s.set('inventory', [{'element_id':'10','apartment':'211','corpus':'Корпус 1'}])
        api = API()
        run(s, api, message(1, 1, '211'), message(2, 2, '211'), message(3, 1, '211', chat=-100))
        jobs = [json.loads(r[0]) for r in s.db.execute("SELECT payload FROM outbox WHERE kind='flat_lookup'")]
        assert [(j['chat_id'], j['actor_id']) for j in jobs] == [(1,1), (2,2)]
        assert any(p.get('chat_id') == -100 and 'личный чат' in p.get('text','') for _,p in api.calls)


def test_rebind_revokes_old_account_before_new_registration(tmp_path):
    with Store(tmp_path/'state.db') as s:
        employee(s, 1)
        a = Access(s)
        code = a.invite(1, rebind=True, now=NOW)
        assert a.employee(1) is None
        with s.atomic():
            a.remember_contact(2, {'user_id':2,'phone_number':'+12025550101'}, NOW)
        assert a.register(2, code, NOW)
        assert a.employee(2)['id'] == 1


def test_old_group_delivery_is_not_reused_after_reconnect(tmp_path):
    with Store(tmp_path/'state.db') as s:
        employee(s, 1)
        a = Access(s)
        code = a.group_invite(now=NOW)
        assert a.connect_group(-100, code, 1, NOW)
        item = s.open_incident('gas', 'Квартира', 'no_data', 'Тест', now=NOW)
        job_id = s.db.execute("SELECT id FROM outbox WHERE kind='card'").fetchone()[0]
        s.job_state(job_id, 'unknown')
        a.disconnect_group()
        code = a.group_invite(now=NOW)
        assert a.connect_group(-200, code, 1, NOW)
        api = API()
        deliver(s, api, NOW)
        cards = [p for _,p in api.calls if 'Счётчик' in p.get('text','')]
        assert len(cards) == 1 and cards[0]['chat_id'] == -200
        assert s.incident(item['id'])['message_id']


def test_registration_response_not_starved_by_cases_without_group(tmp_path):
    with Store(tmp_path/'state.db') as s:
        for index in range(40):
            s.open_incident(str(index), 'Квартира', 'no_data', 'Тест', now=NOW)
        api = API()
        run(s, api, message(1, 42, '/start'))
        assert any(p.get('chat_id') == 42 and 'Для доступа' in p.get('text','') for _,p in api.calls)
        assert not any('Счётчик' in p.get('text','') for _,p in api.calls)


def test_telegram_block_by_one_employee_does_not_stop_other_deliveries(tmp_path):
    from waviot_telegram.telegram_client import TelegramError
    class Selective(API):
        def call(self, method, **payload):
            if payload.get('chat_id') == 1:
                raise TelegramError(403)
            return super().call(method, **payload)
    with Store(tmp_path/'state.db') as s:
        employee(s,1)
        employee(s,2)
        s.enqueue('one','message',{'text':'Ответ 1','chat_id':1,'actor_id':1})
        s.enqueue('two','message',{'text':'Ответ 2','chat_id':2,'actor_id':2})
        api=Selective()
        deliver(s,api,NOW)
        assert len(api.calls) == 1 and api.calls[0][1]['chat_id'] == 2
        assert s.db.execute("SELECT state FROM outbox WHERE key='one'").fetchone()[0] == 'failed'

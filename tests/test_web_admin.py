import os
import pytest

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'waviot_telegram.web.settings')
os.environ.setdefault('ADMIN_SECRET_KEY', 'test-only-secret-not-for-deployment')
import django
django.setup()
from django.conf import settings
from django.core.management import call_command
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from waviot_telegram.access import Access
from waviot_telegram.storage import Store


@pytest.fixture(scope='module', autouse=True)
def admin_db(tmp_path_factory):
    from django.db import connections
    connections.close_all()
    settings.DATABASES['default']['NAME'] = tmp_path_factory.mktemp('admin')/'web.sqlite3'
    call_command('migrate', interactive=False, verbosity=0)
    yield
    connections.close_all()


@pytest.fixture
def web(tmp_path):
    with override_settings(BOT_DATABASE=tmp_path/'state.sqlite3', ALLOWED_HOSTS=['testserver'], PUBLIC=False, SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False):
        yield Client(enforce_csrf_checks=True)


def post(client, url, data, **headers):
    client.get('/login/')
    return client.post(url, data, HTTP_X_CSRFTOKEN=client.cookies['csrftoken'].value, HTTP_ORIGIN='http://testserver', **headers)


def account(name, staff=True):
    return get_user_model().objects.create_user(name, password='test-password', is_staff=staff)


def test_login_uses_password_only_and_keeps_same_origin_referrer(web):
    user = account('password-admin')
    response = web.get('/login/')
    assert response['Referrer-Policy'] == 'same-origin'
    assert b'name="token"' not in response.content
    assert post(web, '/login/', {'username':user.username,'password':'wrong'}).status_code == 200
    assert web.get('/').status_code == 302
    assert post(web, '/login/', {'username':user.username,'password':'test-password'}).status_code == 302
    assert web.get('/').status_code == 200
    assert web.get('/group/').status_code == 200
    assert web.get('/audit/').status_code == 200
    post(web, '/logout/', {})
    assert web.get('/').status_code == 302


def test_admin_creates_disables_and_rebinds_employee(web):
    user = account('employee-admin')
    post(web, '/login/', {'username':user.username,'password':'test-password'})
    response = post(web, '/', {'name':'Иван','phone':'+12025550101'})
    assert response.status_code == 200 and 'Приглашение создано' in response.content.decode()
    assert 'no-store' in response['Cache-Control']
    with Store(settings.BOT_DATABASE) as s:
        row = Access(s).employees()[0]
        assert row['invite_hash'] and row['invite_hash'] not in response.content.decode()
    assert post(web, f"/employees/{row['id']}/", {'action':'disable'}).status_code == 200
    with Store(settings.BOT_DATABASE) as s:
        assert not Access(s).employees()[0]['active']
    assert post(web, f"/employees/{row['id']}/", {'action':'rebind'}).status_code == 200
    with Store(settings.BOT_DATABASE) as s:
        assert Access(s).employees()[0]['active']


def test_login_throttling_and_nonstaff_permissions(web):
    user = account('limited-admin')
    for _ in range(10):
        assert post(web, '/login/', {'username':user.username,'password':'wrong'}).status_code == 200
    assert post(web, '/login/', {'username':user.username,'password':'test-password'}).status_code == 200
    assert web.get('/').status_code == 302
    ordinary = account('ordinary', staff=False)
    web.force_login(ordinary)
    assert web.get('/').status_code == 302


def test_csrf_and_anonymous_actions_are_rejected(web):
    assert web.post('/', {'name':'Иван','phone':'+12025550101'}).status_code == 403
    assert post(web, '/employees/1/', {'action':'disable'}).status_code == 302
    assert web.get('/employees/1/').status_code == 405
    response = web.get('/login/')
    assert response['X-Frame-Options'] == 'DENY'
    assert web.post('/login/', {'username':'admin','password':'x'}, HTTP_X_CSRFTOKEN=web.cookies['csrftoken'].value, HTTP_ORIGIN='null').status_code == 403
    with Store(settings.BOT_DATABASE) as s:
        assert not Access(s).employees()


def test_stylesheet_is_same_origin_without_inline_style_exception(web):
    response = web.get('/login/')
    assert "style-src 'self'" in response['Content-Security-Policy']
    assert 'unsafe-inline' not in response['Content-Security-Policy']
    assert b'<style>' not in response.content
    css = web.get('/assets/admin.css')
    assert css.status_code == 200 and css['Content-Type'].startswith('text/css')
    assert b'font' in css.content


def test_proxy_client_limit_does_not_lock_other_clients(web):
    user = account('proxy-admin')
    with override_settings(PUBLIC=True, ADMIN_TRUSTED_PROXY='192.0.2.10'):
        for number in range(10):
            post(web, '/login/', {'username': f'unknown-{number}', 'password': 'wrong'},
                 REMOTE_ADDR='192.0.2.10', HTTP_X_FORWARDED_FOR='198.51.100.1')
        # Same proxy, different client and account: correct credentials still work.
        response = post(web, '/login/', {'username': user.username, 'password': 'test-password'},
                        REMOTE_ADDR='192.0.2.10', HTTP_X_FORWARDED_FOR='198.51.100.2')
        assert response.status_code == 302
        assert web.get('/').status_code == 200


def test_untrusted_forwarded_header_cannot_bypass_source_limit(web):
    user = account('untrusted-proxy-admin')
    with override_settings(PUBLIC=True, ADMIN_TRUSTED_PROXY='192.0.2.10'):
        for number in range(10):
            post(web, '/login/', {'username': f'unknown-{number}', 'password': 'wrong'},
                 REMOTE_ADDR='192.0.2.20', HTTP_X_FORWARDED_FOR=f'198.51.100.{number+1}')
        response = post(web, '/login/', {'username': user.username, 'password': 'test-password'},
                        REMOTE_ADDR='192.0.2.20', HTTP_X_FORWARDED_FOR='198.51.100.200')
        assert response.status_code == 200
        assert web.get('/').status_code == 302


def test_account_limit_still_applies_across_proxy_clients(web):
    user = account('proxy-account-limit-admin')
    with override_settings(PUBLIC=True, ADMIN_TRUSTED_PROXY='192.0.2.10'):
        for number in range(10):
            post(web, '/login/', {'username': user.username, 'password': 'wrong'},
                 REMOTE_ADDR='192.0.2.10', HTTP_X_FORWARDED_FOR=f'198.51.100.{number+1}')
        response = post(web, '/login/', {'username': user.username, 'password': 'test-password'},
                        REMOTE_ADDR='192.0.2.10', HTTP_X_FORWARDED_FOR='198.51.100.200')
        assert response.status_code == 200
        assert web.get('/').status_code == 302


@pytest.mark.parametrize('public,peer,forwarded,expected', [
    (True, '192.0.2.10', '198.51.100.2', '198.51.100.2'),
    (True, '192.0.2.20', '198.51.100.2', '192.0.2.20'),
    (True, '192.0.2.10', '198.51.100.2, 198.51.100.3', '192.0.2.10'),
    (True, '192.0.2.10', 'invalid', '192.0.2.10'),
    (True, '192.0.2.10', '2001:db8::1', '2001:db8::1'),
    (False, '192.0.2.10', '198.51.100.2', '192.0.2.10'),
])
def test_proxy_hostname_and_untrusted_headers(web, monkeypatch, public, peer, forwarded, expected):
    import socket
    from django.test import RequestFactory
    from waviot_telegram.web.views import client_address
    def resolve(host, port, **kwargs):
        assert host == 'proxy'
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.0.2.10', 0))]
    monkeypatch.setattr('waviot_telegram.web.views.socket.getaddrinfo', resolve)
    with override_settings(PUBLIC=public, ADMIN_TRUSTED_PROXY='proxy'):
        request = RequestFactory().get('/login/', REMOTE_ADDR=peer, HTTP_X_FORWARDED_FOR=forwarded)
        assert client_address(request) == expected


def test_proxy_dns_failure_does_not_trust_forwarded_address(web, monkeypatch):
    import socket
    from django.test import RequestFactory
    from waviot_telegram.web.views import client_address
    def unresolved(*args, **kwargs):
        raise socket.gaierror('synthetic DNS failure')
    monkeypatch.setattr('waviot_telegram.web.views.socket.getaddrinfo', unresolved)
    with override_settings(PUBLIC=True, ADMIN_TRUSTED_PROXY='proxy'):
        request = RequestFactory().get('/login/', REMOTE_ADDR='192.0.2.10', HTTP_X_FORWARDED_FOR='198.51.100.2')
        assert client_address(request) == '192.0.2.10'

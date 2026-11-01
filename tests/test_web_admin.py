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


def post(client, url, data):
    client.get('/login/')
    return client.post(url, data, HTTP_X_CSRFTOKEN=client.cookies['csrftoken'].value, HTTP_ORIGIN='http://testserver')


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
